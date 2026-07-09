# refs 后端：files、packed 与 reftable

## 1. 本讲目标

上一讲（u5-l1）我们建立了引用读写的「公共抽象」：所有引用操作都通过 `struct ref_store` 的虚表分派，上层完全不关心引用到底存在哪里。本讲就回答紧接着的那个问题——

> 引用到底以什么格式存在磁盘上？git 又是怎么把它们「装」进同一个统一接口的？

学完本讲你应该能够：

- 说清楚 **files** 后端如何用「松散文件 + `packed-refs`」两层结构存储引用，并理解松散优先、packed 兜底的读路径。
- 说清楚 **packed** 后端的 `packed-refs` 文件格式（记录行、peeled 行、sorted/peeled 特性头），以及它为什么不是独立可选后端而是 files 的内部助手。
- 说清楚 **reftable** 这种二进制块格式为何在超大仓库（数十万引用）下能提供「近常数时间查找」，以及它用「栈 + tombstone + 压缩」实现原地不可变更新。
- 看懂 git 如何用一个 `refs_backends[]` 数组把三种后端注册、按名查表、按枚举选择。

## 2. 前置知识

本讲默认你已经学过 u5-l1（`ref_store` 抽象、`refs_resolve_ref_unsafe`、`refs_for_each_ref`）与 u3 对象模型。为便于理解，先复习/补充几个关键概念：

- **引用（ref）**：给对象哈希起的命名指针。直接引用存一个 oid；符号引用（symref）存一段 `ref: <另一个引用>` 的文本（或一个符号链接）。
- **后端（backend）**：引用的物理存储方案。git 的设计是「一套公共 API，多套可插拔后端」，靠函数指针表（虚表）分派。
- **松散引用（loose ref）**：每个引用是 `.git/refs/` 下一个小文件，内容为 40（SHA-1）或 64（SHA-256）位十六进制哈希。
- **`packed-refs`**：把大量引用拼进单个文本文件，省 inode、利于批量读，但不支持原子单条更新。
- **快照（snapshot）**：把磁盘文件内容（通常经 mmap）缓存进内存一份，配 `stat_validity` 判断是否过期，避免反复解析。
- **reftable**：专为引用存储设计的二进制格式，引用按名排序、分块存储、前缀压缩，支持二分查找与范围扫描。

一个贯穿全讲的直觉是：**files 是「文件系统友好」的旧格式，reftable 是「数据库友好」的新格式**，二者用同一套虚表挂在 git 主框架下。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `refs.c` | 后端注册表 `refs_backends[]`、按名/按枚举查表 |
| `refs/refs-internal.h` | 虚表 `struct ref_storage_be` 定义、`struct ref_store` 基类、三个 `refs_be_*` 外部声明 |
| `refs/files-backend.c` | files 后端：松散引用读写 + 持有并委托给 packed_ref_store |
| `refs/packed-backend.c` | packed 后端：`packed-refs` 文件解析、快照缓存（也是 files 的内部助手） |
| `refs/reftable-backend.c` | reftable 后端：把 `reftable/` 库接入 git 的 ref_store 接口 |
| `repository.h` | `enum ref_storage_format` 与默认格式宏 |
| `Documentation/technical/reftable.adoc` | reftable 二进制格式的权威规范 |

## 4. 核心概念与源码讲解

### 4.1 files 后端：松散引用 + packed-refs 的组合

#### 4.1.1 概念说明

files 后端是 git 历史最悠久、也是当前默认的引用存储方式。它**不是单一存储，而是两层叠加**：

1. **松散层**：每个引用对应 `.git/refs/<name>` 下的一个文件，内容是哈希或符号引用文本。优点是单条更新只需改一个文件、原子性好；缺点是引用多了会消耗大量 inode 和目录遍历开销。
2. **packed 层**：大量「长期不变」的引用被 `git pack-refs` 压进单个 `.git/packed-refs` 文本文件。

读一个引用时，files 后端的规则是**「松散优先，packed 兜底」**：若该引用有松散文件，用它；否则去 `packed-refs` 里找。这样 `git pack-refs` 之后，原本的松散文件可以被删掉而不丢失引用——因为同名引用已经在 packed-refs 里了。

注意：files 后端**内部持有一个 `packed_ref_store`**。换句话说，packed 后端在代码里是 files 的「子组件」，而不是和 files 平起平坐的独立可选后端（这一点 4.4 会再强调）。

#### 4.1.2 核心流程

读单个引用（`read_ref_internal`）的伪代码：

```
path = $GIT_DIR/refs/<refname>
lstat(path):
  if 文件不存在(ENOENT):
      return 读 packed_ref_store  # 兜底
  if 是符号链接 且 指向合法 refname:
      返回符号引用 (REF_ISSYMREF)
  if 是目录:
      return 读 packed_ref_store  # 兜底
  否则打开文件读内容，parse_loose_ref_contents() 解析
```

关键细节有两个：一是「文件不存在」和「路径是目录」两种情况都要回退去查 packed；二是存在一个**重试循环**，防止在 `lstat` 与真正 `open` 之间文件类型被别的进程改掉（file↔dir↔symlink）造成误报。

遍历所有引用时（`files_ref_iterator_begin`）则是**合并**两层的迭代器：用一个 `overlay_ref_iterator` 把松散迭代器叠在 packed 迭代器之上，重名时取松散那份——这与「松散优先」的读规则一致。

#### 4.1.3 源码精读

files 后端的内存结构 [`struct files_ref_store`](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/files-backend.c#L82-L102) 三字段最能说明它的「组合」本质：`loose`（松散引用的内存缓存）、`packed_ref_store`（指向 packed 后端）、`gitcommondir`（公共目录）。

初始化时 files 直接顺手创建好 packed 后端，见 [`files_ref_store_init`](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/files-backend.c#L162-L188)：其中 `refs->packed_ref_store = packed_ref_store_init(...)` 这一行就是「files 内嵌 packed」的物证。

读引用主逻辑 [`read_ref_internal`](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/files-backend.c#L513-L645) 是本模块最值得逐行读的函数。看两个兜底点：松散文件 `ENOENT` 时回退 packed（[第 553-565 行](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/files-backend.c#L553-L565)），以及路径是目录时也回退 packed（[第 593-608 行](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/files-backend.c#L593-L608)）。真正读到文件后由 [`parse_loose_ref_contents`](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/files-backend.c#L633-L634) 统一解析（同时支持 oid 行与 `ref:` 符号引用行）。公共入口 [`files_read_raw_ref`](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/files-backend.c#L647-L652) 只是它的薄包装（`skip_packed_refs=0`）。

遍历时两层合并的关键在 [`files_ref_iterator_begin`](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/files-backend.c#L1075-L1114)：注释明确要求**先读松散、再读 packed**，以避免「松散引用正被并发迁移进 packed-refs」的竞态；最后 `overlay_ref_iterator_begin(loose_iter, packed_iter)` 把两者叠成一路，松散覆盖 packed。

#### 4.1.4 代码实践

1. 实践目标：直观看到 files 后端的「松散 + packed」两层与覆盖关系。
2. 操作步骤：
   - 在一个有多条历史的仓库里运行 `git pack-refs --all --prune`。
   - 用 `ls .git/refs/heads` 与 `cat .git/packed-refs` 分别观察。
   - 再手动改一个分支：`git update-ref refs/heads/test HEAD`，然后 `ls -l .git/refs/heads/test`。
3. 需要观察的现象：`pack-refs --prune` 后大多数分支的松散文件消失，对应引用出现在 `packed-refs` 里；但只要再更新一次该分支，git 会重新在 `.git/refs/heads/` 下写一个松散文件（因为 packed 不支持单条原子更新）。
4. 预期结果：松散文件回归，且 `git rev-parse refs/heads/test` 返回新值——验证了「松散优先，packed 兜底」。
5. 若无法确定运行结果，明确写「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 files 后端读到一个「目录」（而非文件）时也要回退去查 packed-refs？

> 参考答案：因为 `refs/heads/foo`（松散文件）和 `refs/heads/foo/bar`（更深一层）可以同时存在。当请求 `refs/heads/foo` 时，若 `foo` 是个目录，说明它没有自己的松散文件，但仍可能被收进了 packed-refs，所以必须兜底查一次。

**练习 2**：`files_ref_iterator_begin` 为什么强制「先读松散、再读 packed」？

> 参考答案：防止与并发的 `git pack-refs` 竞态——若先读了 packed 再读松散，可能正好夹在「松散被删、packed 已写」之间，导致某个引用既不在松散视图也不在 packed 视图里，凭空消失。

### 4.2 packed 后端：packed-refs 文件格式与快照缓存

#### 4.2.1 概念说明

packed 后端只负责那一个 `packed-refs` 文本文件。它的特点与局限（见源码注释）值得记牢：

- 不能存符号引用（symref 只能用松散文件表达）。
- 不能存 reflog。
- 不支持 rename（虽然技术上可以加）。

它的价值在于**批量只读**：成千上万条引用拼成一个文件，一次 mmap 即可遍历，省去海量小文件的 inode 与 syscall。

#### 4.2.2 核心流程

**文件格式**（纯文本）：

```
# pack-refs with: peeled fully-peeled sorted     ← 特性头
<oid> <refname>                                  ← 一条引用记录
^<peeled-oid>                                    ← 可选：上面那条的「剥皮」对象
<oid> <refname>
...
```

- 第一行 `# pack-refs with: ...` 声明这个文件具备哪些「特性（traits）」：`peeled`/`fully-peeled`（标注 tag 是否已剥皮）、`sorted`（是否按 refname 排好序）。读者据此决定能否信任排序、能否信任「没有 peeled 行就一定剥不动」。
- 每条引用一行 `<oid> <refname>`。如果它指向一个 annotated tag，紧跟一行以 `^` 开头记录「剥皮后」指向的真实对象——这样以后查 tag 指向的 commit 就不必真去解 tag 对象。
- 记录按 refname 排序时，可以二分查找；未排序则读入内存后排序。

**快照机制**：把整个文件 mmap（或读入）内存一份，配一个 `stat_validity`（记录文件的 stat 信息）。每次 `get_snapshot` 先校验文件是否被换过：没换就直接用缓存，换了才重建。快照是**引用计数**的，迭代器持有时不会被回收。

读取单条引用（`packed_read_raw_ref`）：在快照里二分定位 `<refname>`，定位到则解析其 oid 并打上 `REF_ISPACKED` 标记。

#### 4.2.3 源码精读

快照结构 [`struct snapshot`](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/packed-backend.c#L74-L122) 把 `buf/start/eof` 三指针、`peeled` 状态、`referrers` 引用计数、`validity` 有效性校验都聚在一起，注释说明了「已排序则直接 mmap、未排序则堆内排序后存放」的取舍。后端本身 [`struct packed_ref_store`](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/packed-backend.c#L139-L158) 持有 `path`（packed-refs 路径）、`snapshot`、以及一把 `lock`（事务外也能加锁，写完后仍保持锁住）。

特性头的权威解释见 [create_snapshot 上方注释](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/packed-backend.c#L687-L719)，它逐条解释 `peeled`/`fully-peeled`/`sorted` 的语义；写出的头部字符串是常量 [`PACKED_REFS_HEADER`](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/packed-backend.c#L1338-L1339)：`"# pack-refs with: peeled fully-peeled sorted \n"`。

记录解析循环（[第 399-434 行](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/packed-backend.c#L399-L434)）逐行扫描，关键是把 `^` 开头的 peeled 行**黏在它上面那条引用记录上**（第 408-420 行），并在扫描时顺便判定文件是否真的排好序（第 427-431 行）。读取入口 [`packed_read_raw_ref`](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/packed-backend.c#L830-L854) 很短：拿快照→二分定位→解析 oid→置 `REF_ISPACKED`。

#### 4.2.4 代码实践

1. 实践目标：看懂 packed-refs 文本格式，尤其是 `^` 剥皮行。
2. 操作步骤：
   - `git pack-refs --all`。
   - `cat .git/packed-refs`。
   - 找一个指向 annotated tag 的引用（如某个 `refs/tags/v*`），观察它下面紧跟的 `^<oid>` 行。
   - 用 `git cat-file -t <上面 ^ 的 oid>` 与 `git cat-file -t <tag 记录的 oid>` 对比类型。
3. 需要观察的现象：tag 记录的 oid 类型是 `tag`，而 `^` 行的 oid 类型是 `commit`（被剥皮后的真实对象）。
4. 预期结果：你会直观理解「peeled 行就是提前算好的 tag 解引用，省去一次对象库访问」。
5. 若本地无 annotated tag，可先 `git tag -a v0.0.1 -m test` 再重做，否则标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：packed 后端为什么不能存符号引用？

> 参考答案：符号引用需要表达「这个 ref 指向另一个 ref 名字」，而 packed-refs 的每行格式是 `<oid> <refname>`，存的是对象哈希；要让 packed 支持 symref 得改格式，git 选择把 symref 一律以松散文件（或符号链接）保存，确保符号引用永远是最新的。

**练习 2**：快照里的 `stat_validity` 起什么作用？

> 参考答案：它记录上一次读取 packed-refs 时的文件 stat（大小/mtime/inode 等）。下次访问前比对，若不一致说明文件被别的进程重写了，旧快照作废、需要重建，从而让读路径自动感知到并发写入。

### 4.3 reftable 后端：二进制块格式、栈与 tombstone

#### 4.3.1 概念说明

reftable 是为「超大仓库」设计的引用存储格式。它的动机（见规范）很直接：android 有 86 万条引用，`packed-refs` 文件达 62MB 且**单条查找要线性扫描**；而 reftable 把引用按名排序后分块存储，单条查找是**二分**，接近常数时间。

reftable 的三个核心思想：

1. **排序 + 分块 + 前缀压缩**：引用按名排序，写入定长「块（block）」；块内相邻引用做前缀压缩（公共前缀只存一次），块尾有 `restart` 间断点表支持块内二分。
2. **不可变文件 + 栈式更新**：单个 reftable 文件写好后永不修改。更新不是改文件，而是**写一个新文件，原子地追加到栈**（由 `tables.list` 列出）。删除用 **tombstone**（一条 type=0 的记录）覆盖旧值。
3. **自动压缩**：栈里小文件积累到一定数量后，合并（merge join）成一个大文件，老文件删除。整个过程由「文件名唯一」+ 锁协议保证读不阻塞写。

每个 reftable 文件自带 24/28 字节文件头（魔数 `REFT`、版本、块大小、`min/max_update_index`、v2 还多一个 `hash_id`），末尾有 footer，整体可随机访问。

#### 4.3.2 核心流程

更新一个引用（事务提交）的简化流程：

```
1. 加锁 tables.list.lock，读取当前栈
2. 新 update_index = 栈顶文件的 max_update_index + 1
3. 写临时 reftable tmp_XXXXXX（含本次改的 ref + 对应 reflog）
   - 新值: 写一条 ref 记录
   - 删除: 写一条 type=0 的 tombstone
4. 原子改名成 <min_update_index>-<max_update_index>-<rand>.ref
5. 把新文件追加进 tables.list.lock
6. 原子改名 tables.list.lock → tables.list
```

读一个引用时，git 打开栈里所有 `.ref` 文件，从**最新到最老**合并读取：tombstone 表示「在更老的层里也被删了」，最新出现的真实记录即为当前值。这就是「栈」的语义——后写的覆盖先写的。

> 文件头里的 `update_index` 是单调递增的事务序号，决定了栈中文件的先后；`min_update_index`/`max_update_index` 标注本文件覆盖的序号区间。压缩时新区间取被合并文件区间的外包。

查找复杂度方面，若一个 reftable 有 \(N\) 条引用、块大小为 \(B\)，则定位一条引用需先在 footer 的索引里二分找块（约 \(O(\log N)\) 次），再在块内 restart 表二分+线性扫描，整体远快于 packed-refs 的全文件线性扫描：

\[
T_{\text{reftable lookup}} \approx O(\log N) + O(\text{block 内}),\qquad
T_{\text{packed-refs lookup}} = O(N)
\]

#### 4.3.3 源码精读

reftable 后端在 git 侧的胶水层是 [`struct reftable_ref_store`](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/reftable-backend.c#L123-L141)：它持有 `main_backend`（公共目录里的主栈）和 `worktree_backend`（per-worktree 栈），还有一个按 worktree 名字懒加载的 `worktree_backends` 映射。

真正干活的是 `reftable/` 子库。每个 backend 包装一个 reftable 栈，见 [`struct reftable_backend`](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/reftable-backend.c#L36-L55)，它的 `init` 调用库函数 `reftable_new_stack(&be->stack, path, &opts)` 打开栈，并注册 `on_reload` 回调（栈被并发改写后重建迭代器）。读单条引用 [`reftable_backend_read_ref`](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/reftable-backend.c#L64-L90) 的套路是：在栈上 `seek_ref`→`next_ref`→比对 refname→按 `value_type`（`REFTABLE_REF_SYMREF` 走符号引用分支，否则取 oid）。

后端初始化 [`reftable_be_init`](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/reftable-backend.c#L407-L449) 把 git 的哈希算法（SHA-1/SHA-256）翻译成 reftable 的 `hash_id`，然后在 `$GIT_COMMON_DIR/reftable` 目录建主栈。

格式的权威定义在 [`Documentation/technical/reftable.adoc`](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Documentation/technical/reftable.adoc)：其中 [Problem statement](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Documentation/technical/reftable.adoc#L20-L35) 给出了 packed-refs 不 scale 的动机；[Header (version 2)](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Documentation/technical/reftable.adoc#L235-L255) 描述 28 字节文件头；[Update transactions](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Documentation/technical/reftable.adoc#L963-L990) 描述栈式原子更新；[Compaction](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Documentation/technical/reftable.adoc#L991-L1015) 描述合并压缩。栈操作 API 声明在 [`reftable/reftable-stack.h`](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/reftable/reftable-stack.h#L30-L162)（`reftable_new_stack`、`reftable_stack_add`、`reftable_stack_reload`、`reftable_stack_auto_compact` 等）。

#### 4.3.4 代码实践

1. 实践目标：直观对比 reftable 与 files 两种后端的磁盘布局差异。
2. 操作步骤：
   - 创建一个 reftable 仓库：`git init -b main rt-reftable --ref-format=reftable`（注意：本 HEAD 的开关是 `--ref-format`，不是旧文档里的 `--ref-storage`）。
   - `cd rt-reftable && git commit --allow-empty -m one && git commit --allow-empty -m two`。
   - `ls -la .git/`，重点看 `.git/reftable/` 目录与 `.git/tables.list`。
   - 再创建一个 files 仓库对比：`git init -b main rt-files`，做同样两次提交，观察 `.git/refs/`、`.git/logs/`、`.git/packed-refs`。
3. 需要观察的现象：reftable 仓库**没有** `.git/refs/` 目录和 `.git/packed-refs`，取而代之的是 `.git/reftable/` 下的若干 `*.ref` 文件和一个 `tables.list`；每次提交通常新增一个小 `.ref` 文件并被列入 `tables.list`。files 仓库则有 `.git/refs/heads/main`（或被收进 `packed-refs`）和 `.git/logs/refs/heads/main`。
4. 预期结果：你会亲眼看到「栈式追加」——多次提交产生多个 `.ref`，`tables.list` 按序号递增列出它们。
5. 若 `--ref-format` 选项不可用（旧版本），改用环境变量 `GIT_DEFAULT_REF_FORMAT=reftable git init`；仍不行则标注「待本地验证」。

> 想观察 tombstone，可在 reftable 仓库里 `git update-ref refs/heads/x HEAD` 再 `git update-ref -d refs/heads/x`，用 `git count-objects` 之外的工具（如 `xxd .git/reftable/<最新文件>`）会看到删除被写成一条记录而非文件消失——删除是逻辑覆盖。

#### 4.3.5 小练习与答案

**练习 1**：reftable 为什么用「写新文件追加到栈」而不是直接改老文件？

> 参考答案：单文件原地改写在并发与崩溃恢复上极难做到原子（要么整文件重写，要么加复杂的日志）。reftable 让每个文件不可变，更新=追加新文件+原子换 `tables.list`，写者互斥靠单个锁文件，读者无需加锁即可看到一致的栈快照，崩溃最坏只丢一个未完成临时文件。

**练习 2**：reftable 里删除一个引用后，老文件里那条记录还在吗？读取时如何得到正确结果？

> 参考答案：老文件不可变，记录仍在。删除会在最新文件里写一条 tombstone（type=0）。读取从新到老合并，遇到 tombstone 即认为该引用不存在，从而「逻辑删除」覆盖了物理上仍存在的旧记录。压缩时才会真正把 tombstone 与其目标一起清掉。

### 4.4 后端注册与选择机制

#### 4.4.1 概念说明

三种后端怎么挂进 git？答案是**一个按枚举索引的数组 + 一张函数指针虚表**。这里有一个容易被忽视的事实：注册表里**只有 files 和 reftable 两个槽**，packed 没有独立槽位——它通过 files 的 `packed_ref_store` 字段被间接使用。这也解释了为什么用户能选的后端只有 `files`/`reftable`，而 `packed` 只是 `git pack-refs` 触发的内部整理结果。

后端选择发生在 `git init` 时：根据命令行 `--ref-format`、配置 `init.defaultrefformat`、环境变量 `GIT_DEFAULT_REF_FORMAT`、或 `feature.experimental` 决定；选定后把结果写进仓库配置的 `extensions.refstorage`，以后打开该仓库就据此恢复后端。默认仍是 files，但未来 Git 3.0（开启 `WITH_BREAKING_CHANGES` 编译开关）会把默认切到 reftable。

#### 4.4.2 核心流程

```
git init --ref-format=reftable
  └─ ref_format = "reftable"
  └─ ref_storage_format = ref_storage_format_by_name("reftable")  → REF_STORAGE_FORMAT_REFTABLE
  └─ init_db(...) 把格式写入 repo
  └─ setup.c: 写入 extensions.refstorage = reftable

之后任何命令打开该仓库：
  └─ 读 extensions.refstorage → ref_storage_format
  └─ refs_backends[ref_storage_format] 取出虚表（files 或 reftable）
  └─ get_main_ref_store() 懒加载进 repository.refs_private
```

#### 4.4.3 源码精读

注册表本身在 [`refs_backends[]`](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L38-L41)：仅有 `FILES` 和 `REFTABLE` 两个下标。按枚举取虚表见 [`find_ref_storage_backend`](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L43-L49)，按名字取枚举见 [`ref_storage_format_by_name`](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L51-L57)（线性扫数组比 `name`），反向取名字见 [`ref_storage_format_to_name`](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L59-L64)。

虚表类型 [`struct ref_storage_be`](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/refs-internal.h#L567-L601) 列出全部回调槽（init/release/transaction_*/iterator_begin/read_raw_ref/reflog_*/fsck 等），三个实例声明在 [refs-internal.h:603-605](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/refs-internal.h#L603-L605)（`refs_be_files`/`refs_be_reftable`/`refs_be_packed`）。三张虚表实例对照着看最能体会「能力差异」：

- files 虚表 [`refs_be_files`](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/files-backend.c#L4081-L4110)：能力最全，所有槽都填了。
- packed 虚表 [`refs_be_packed`](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/packed-backend.c#L2149-L2179)：很多槽是 `NULL`——不能 rename、不能 copy、不存 reflog（`for_each_reflog_ent` 等全 NULL）、不能存 symref（`read_symbolic_ref = NULL`）。
- reftable 虚表 [`refs_be_reftable`](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/reftable-backend.c#L2836-L2866)：与 files 同样能力齐全。

枚举与默认值见 [`enum ref_storage_format` 与 `REF_STORAGE_FORMAT_DEFAULT`](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/repository.h#L19-L29)：当前默认 `FILES`，`WITH_BREAKING_CHANGES`（Git 3.0）下默认 `REFTABLE`。`git init` 的命令行开关在 [init-db.c 的 `--ref-format` 选项](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/init-db.c#L112)，解析见 [init-db.c:176-180](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/init-db.c#L176-L180)。

选择优先级链在 [`setup.c` 的格式判定逻辑](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L2764-L2782)：命令行 `--ref-format` > 已有仓库配置 > 环境变量 `GIT_DEFAULT_REF_FORMAT` > 配置 `init.defaultrefformat` > 默认。其中配置项 `init.defaultrefformat` 的读取见 [setup.c:2698-2706](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L2698-L2706)，`feature.experimental` 会顺带启用 reftable（[setup.c:2712-2718](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L2712-L2718)）。选定后写入仓库配置的代码见 [`extensions.refstorage` 写入](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L2438-L2450)：注意 files 是默认故不写、reftable 才写 `extensions.refstorage = reftable`。

#### 4.4.4 代码实践

1. 实践目标：验证「后端由仓库配置决定」，并看清 packed 不在可选列表里。
2. 操作步骤：
   - 分别创建 files 与 reftable 仓库（见 4.3.4）。
   - 各自 `cat .git/config`，对比是否多出 `extensions.refstorage`。
   - 故意输入错误格式试错：`git init --ref-format=packed sample`，观察报错（应来自 [init-db.c:179](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/init-db.c#L176-L180) 的 `unknown ref storage format`）。
3. 需要观察的现象：files 仓库的 config 里**没有** `extensions.refstorage`（默认即 files，省略）；reftable 仓库有 `extensions.refstorage = reftable`；尝试 `packed` 会被拒绝，证明它不是用户可选后端。
4. 预期结果：直观印证「注册表只暴露 files/reftable，packed 是 files 内部助手」。
5. 若报错文案不同，以本地版本为准，否则标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `git init --ref-format=packed` 会失败？

> 参考答案：`refs_backends[]` 数组只有 `FILES` 和 `REFTABLE` 两个下标，`ref_storage_format_by_name("packed")` 找不到匹配项返回 `REF_STORAGE_FORMAT_UNKNOWN`，于是 `git init` 报 `unknown ref storage format`。packed 后端没有在注册表里登记，因为它只作为 files 的内部组件被使用。

**练习 2**：files 仓库的 `.git/config` 里为什么通常看不到 `extensions.refstorage`？

> 参考答案：files 是默认后端。setup.c 的写入逻辑对 files 分支做了特殊处理：只有非 files 才写 `extensions.refstorage`；files 时省略，表示「用默认」。这样既能向前兼容老仓库，又能在需要时显式标注。

## 5. 综合实践

设计一个把三种存储「串起来」的小任务：**在同一个项目上对比 files 与 reftable 两种后端的行为与磁盘表现。**

1. 用 `git clone --bare` 准备一个有真实历史的源仓库 `origin.git`（或直接用 git 自身仓库）。
2. 分别克隆出两个工作副本，但用不同后端：
   - `git -c init.defaultrefformat=files clone origin.git work-files`
   - `git -c init.defaultrefformat=reftable clone origin.git work-reftable`
   - （克隆会按目标仓库的后端复制；若行为不符预期，可改为 `git init --ref-format=...` 后 `git fetch`。）
3. 在两个副本里各做一组相同操作并记录磁盘变化：
   - 列出引用：`git for-each-ref | wc -l`。
   - 新建分支并删除：`git branch tmp && git branch -d tmp`。
   - 触发整理：files 侧 `git pack-refs --all`；reftable 侧观察 `.git/reftable/` 与 `tables.list`。
4. 用 `du -sh .git` 对比两者 `.git` 体积；用 `find .git -name '*.ref'` 与 `cat .git/tables.list` 看 reftable 的栈。
5. 写一段总结：哪种后端在「引用多、更新少」的场景更省空间/更快查找？哪种在「频繁单条更新」更友好？分别对应源码里的哪个机制（packed-refs 快照 vs reftable 栈+tombstone）？

预期：你会看到 reftable 的二进制块在大量引用时体积更小、查找更快（前缀压缩 + 二分），而 files 的松散引用对单条原子更新更自然；两者通过同一张虚表（`refs_be_files` / `refs_be_reftable`）挂接，上层命令完全无感。若某些命令在本地版本上行为不同，以实际为准并标注「待本地验证」。

## 6. 本讲小结

- files 后端是「松散引用 + packed-refs」两层组合：读时**松散优先、packed 兜底**，遍历时用 overlay 迭代器把两层合并，松散覆盖 packed。
- packed 后端只管那一个 `packed-refs` 文本文件，特性头声明 `peeled/fully-peeled/sorted`，靠 mmap 快照 + `stat_validity` 缓存；它能力受限（无 symref、无 reflog），且**不在用户可选注册表里**，是 files 的内部助手。
- reftable 是排序分块、前缀压缩的二进制格式，靠「不可变文件 + 栈 + tombstone + 压缩」实现超大仓库下的近常数查找与原子更新。
- 三种后端通过 `struct ref_storage_be` 虚表接入；注册表 `refs_backends[]` 只有 files/reftable 两槽，按 `enum ref_storage_format` 选择，默认 files（Git 3.0 计划默认 reftable）。
- 后端选择发生在 `git init`，受 `--ref-format` / `init.defaultrefformat` / `GIT_DEFAULT_REF_FORMAT` / `feature.experimental` 控制，结果写入仓库配置 `extensions.refstorage`。

## 7. 下一步学习建议

- 本讲只讲了「后端如何存/读引用」，**更新引用的事务机制**（加锁、原子提交、回滚）留给了 u5-l3「ref 事务与 ref-cache」。建议接着读 `refs/refs-internal.h` 里的 `struct ref_transaction`、`enum ref_transaction_state`，以及 files 后端的 `files_transaction_prepare/finish/abort`。
- 想深入 reftable 二进制细节，可直接读 `reftable/` 子库：`writer.c`（如何写块与前缀压缩）、`record.c`（记录编码）、`stack.c`（栈与压缩协议）、`merged.c`（多层合并读取）。
- 想理解 packed-refs 与 ref-cache 的内存组织，结合 u5-l3 读 `refs/ref-cache.c` 与 `refs/iterator.c`，看 `overlay_ref_iterator`/`merge_ref_iterator` 如何把多个后端的迭代器编织成一路。
- 关于快照有效性 `stat_validity` 与文件锁的崩溃一致性，可与 u14-l2「临时文件与文件锁」交叉阅读。

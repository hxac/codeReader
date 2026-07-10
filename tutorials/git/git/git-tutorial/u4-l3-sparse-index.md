# 稀疏索引 sparse-index

## 1. 本讲目标

本讲是「索引 index」单元的第三讲，承接 u4-l1（`index_state` 与 `cache_entry`）建立的「工作树／索引／对象数据库」三层模型，以及 u4-l2（cache-tree、split-index）的「用空间换时间」缓存思想。

学完本讲后，你应该能够：

- 说清楚**普通索引在巨型单仓库（monorepo）下的性能瓶颈**，以及 sparse-index 用什么思路解决它。
- 在源码层面描述一个**稀疏目录条目（sparse directory entry）**长什么样、它如何把成千上万个文件条目「折叠」成一条记录。
- 读懂 `convert_to_sparse`（折叠）与 `expand_index`（展开）这两条核心路径，理解折叠的前提条件与展开的三种程度。
- 理解 sparse-index 与 cone 模式 sparse-checkout、`index.sparse` 配置、以及「命令是否需要完整索引」之间的耦合关系。

---

## 2. 前置知识

在进入本讲前，请确认你已经理解下面这些来自前两讲（u4-l1、u4-l2）的概念：

- **`cache_entry`**：索引里的一条记录，核心字段是「路径名 + `oid`（指向 blob）+ `ce_mode` + stat 快照」。
- **`CE_SKIP_WORKTREE`**：cache_entry 的一个标志位，表示「这个文件按 sparse-checkout 规则不应出现在工作树里」。u4-l1 已经讲过，子模块目录用 `S_IFGITLINK` 表示。
- **cache-tree**：u4-l2 讲的目录哈希缓存——每个目录子树都能折叠成一个 `tree` 对象并算出一个 `oid`。这是 sparse-index 能成立的物理基础：**一个目录既然能折叠成 tree，自然也能在索引里用「一条指向该 tree 的条目」来代表整个目录**。
- **sparse-checkout**：一种只把仓库部分目录检出到工作树的工作方式。本讲还会补充它的 **cone 模式（锥模式）**这一关键前提。
- **split-index**：u4-l2 讲的共享基 + 增量索引机制。本讲会确认它**与 sparse-index 互斥**。

> 关键直觉：sparse-index 的本质是「把 cache-tree 已经算好的目录折叠，从**提交时的临时行为**升级为**索引磁盘格式的常态**」。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [sparse-index.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/sparse-index.c) | sparse-index 的全部核心实现：折叠（collapse）、展开（expand）、稀疏目录条目构造、与工作树的同步。 |
| [sparse-index.h](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/sparse-index.h) | 对外接口声明：`convert_to_sparse`、`expand_index`、`ensure_full_index`、`ensure_correct_sparsity`、`expand_to_path`。 |
| [read-cache-ll.h](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache-ll.h) | 定义 `CE_SKIP_WORKTREE` 标志位与 `enum sparse_index_mode`（索引的三种稀疏状态）。 |
| [object.h](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/object.h) | 定义 `S_ISSPARSEDIR` 宏与 `create_ce_mode`，是「稀疏目录条目」在 mode 字段上的编码方式。 |
| [dir.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/dir.c) | cone 模式 sparse-checkout 的路径匹配：`path_in_sparse_checkout`、`path_matches_pattern_list`、`init_sparse_checkout_patterns`。 |
| [read-cache.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c) | 索引读/写时对稀疏状态的调度：读时 `ensure_correct_sparsity`、写时 `convert_to_sparse`。 |
| [builtin/ls-files.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/ls-files.c) | 一个典型的「需要完整索引」命令：遇到稀疏目录条目时调用 `ensure_full_index` 展开，并提供 `--sparse` 选项原样展示。 |

---

## 4. 核心概念与源码讲解

### 4.1 稀疏目录条目表示

#### 4.1.1 概念说明：为什么要「折叠」

先回顾一个矛盾：

- 你用 sparse-checkout 只关心仓库里的一个子目录（你的「cone」），工作树里也只有这一小块文件。
- 但 u4-l1 讲过，**索引 `.git/index` 列出的是整个提交里的全部文件**——即便某个文件挂着 `CE_SKIP_WORKTREE`（不该出现在工作树），它**仍然占用一条索引记录**。

对一个有几十万乃至上百万文件的巨型单仓库（例如微软 Windows 仓库），这意味着：你只编辑了 5 个文件，`git status` / `git add` 却要先从磁盘读、向磁盘写一份包含百万条目的索引，开销是 \(O(N)\)（\(N\) = 全仓库文件数），与你实际工作的范围无关。

sparse-index 的解决办法是：**既然 cone 模式按「整目录」决定检出范围，那么 cone 之外、连续属于同一个目录的所有 SKIP_WORKTREE 文件条目，就可以用「一条代表该目录的条目」代替**。这一条记录指向该目录对应的 `tree` 对象（cache-tree 早就帮你算好了 `oid`）。

效果上，索引条目数从 \(O(N)\) 降到近似：

\[
O(\text{cone 内文件数} \;+\; \text{cone 外的顶层稀疏目录数})
\]

对一个典型使用场景，这能把百万条目压缩到几千条，索引读写随之快上一两个数量级。

#### 4.1.2 「稀疏目录条目」的编码

稀疏目录条目本质上**还是一个 `cache_entry`**，只是用了三个特殊约定来「伪装成目录」：

1. **mode 为 `S_IFDIR`**（即 `040000`）。普通文件是 `S_IFREG`，子模块是 `S_IFGITLINK`，而 `S_IFDIR` 在普通索引里不会出现，正好被复用为「这是个稀疏目录」的标记。判定宏 [object.h:124](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/object.h#L124)：

   ```c
   #define S_ISSPARSEDIR(m) ((m) == S_IFDIR)
   ```

2. **`oid` 指向该目录的 `tree` 对象**（而非 blob）。展开时就是去读这棵 tree。

3. **路径名带末尾斜杠**，例如 `folder2/`。这个斜杠是给 cone 模式匹配逻辑用的（见 4.1.4）。

4. **带 `CE_SKIP_WORKTREE` 标志**，因为整个目录都不在工作树里。

构造函数在 [sparse-index.c:44-55](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/sparse-index.c#L44-L55)：

```c
static struct cache_entry *construct_sparse_dir_entry(
                struct index_state *istate,
                const char *sparse_dir,
                struct cache_tree *tree)
{
    struct cache_entry *de;
    de = make_cache_entry(istate, S_IFDIR, &tree->oid, sparse_dir, 0, 0);
    de->ce_flags |= CE_SKIP_WORKTREE;
    return de;
}
```

可以看到 `tree->oid` 直接被取来当作条目的 `oid`——这正是 u4-l2 cache-tree「目录折叠成 tree」的成果被复用。

> 一个容易踩的细节：`S_IFDIR` 这个 mode 平时不会出现在普通 `cache_entry` 里（普通目录在索引里是展开成一个个文件的）。所以 `S_ISSPARSEDIR(ce->ce_mode)` 为真，**当且仅当**这是一条稀疏目录条目。

#### 4.1.3 索引的三种稀疏状态

索引的「稀疏程度」用一个枚举 [read-cache-ll.h:143-164](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache-ll.h#L143-L164) 描述，记录在 `istate->sparse_index` 字段里：

| 取值 | 含义 |
| --- | --- |
| `INDEX_EXPANDED = 0` | 完全展开：索引里没有任何稀疏目录条目，所有文件都是独立条目。**没有 cone 模式 sparse-checkout 的仓库永远是这个状态。** |
| `INDEX_COLLAPSED` | 完全折叠：能折叠的目录都已折叠成稀疏目录条目。 |
| `INDEX_PARTIALLY_SPARSE` | 部分稀疏：展开 `expand_index` 时带了一个 cone 模式 pattern list，只展开了「现在进入 cone」的目录，其余仍为稀疏目录条目——所以是「半展开」。 |

把 `INDEX_EXPANDED = 0` 设成零值是有意为之：这样 `istate->sparse_index` 用 `xcalloc` 清零初始化时，默认就是「完整索引」，与历史行为完全兼容。

#### 4.1.4 cone 模式：sparse-index 的硬前提

不是任何 sparse-checkout 都能用 sparse-index。`is_sparse_index_allowed` 在 [sparse-index.c:153-199](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/sparse-index.c#L153-L199) 列出了一串前置条件，最核心的是 **cone（锥）模式**：

```c
if (!cfg->apply_sparse_checkout || !cfg->core_sparse_checkout_cone)
    return 0;
...
if (!istate->sparse_checkout_patterns->use_cone_patterns)
    return 0;
```

为什么必须是 cone 模式？因为 cone 模式的语义是「**整目录**要么全检出、要么全不检出」，sparse-index 才能放心地用一条目录条目代表整片文件。而传统的「非 cone」模式允许写任意 glob（比如 `*.c`），可以只检出某个目录里的部分文件——这时一个目录就**不能**整体折叠成一条记录了。

cone 模式的匹配逻辑在 [dir.c:1477-1550](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/dir.c#L1477-L1550) 的 `path_matches_pattern_list`，里面有一段正好解释了稀疏目录条目「末尾斜杠」的用途——给目录拼一个假文件名 `/-`，复用文件匹配逻辑：

```c
if (parent_pathname.len > 0 &&
    parent_pathname.buf[parent_pathname.len - 1] == '/') {
    slash_pos = parent_pathname.len - 1;
    strbuf_add(&parent_pathname, "-", 1);   // "folder2/" -> "folder2/-"
}
```

`path_in_sparse_checkout` ([dir.c:1612-1616](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/dir.c#L1612-L1616)) 就是判断「这个路径按当前 sparse-checkout 是否该出现在工作树」，折叠算法要用它来决定一个目录「该不该折叠」（见 4.2）。

#### 4.1.5 代码实践：看见一条稀疏目录条目

1. **实践目标**：亲手制造一条稀疏目录条目，看清它的 mode、`oid`、带斜杠的路径名。
2. **操作步骤**（在临时目录里，不影响源码树）：

   ```sh
   mkdir spdemo && cd spdemo
   git init -q
   mkdir cone outside
   echo a > cone/a.txt
   echo b > outside/b.txt
   echo c > outside/deep/c.txt 2>/dev/null || { mkdir outside/deep; echo c > outside/deep/c.txt; }
   git add -A && git commit -qm init

   # 启用 cone 模式 sparse-checkout + sparse-index，只检出 cone/
   git sparse-checkout init --cone --sparse-index
   git sparse-checkout set cone
   ```

3. **需要观察的现象**：
   - `git ls-files --sparse` 会列出类似 `cone/a.txt` 和 `outside/`——注意 `outside/` 这一行**末尾有斜杠**，它就是一条稀疏目录条目，代表 `outside/` 下原本的 `b.txt`、`deep/c.txt` 等多条记录。
   - `git ls-files`（不带 `--sparse`）则会**展开**索引，列出 `outside/b.txt`、`outside/deep/c.txt` 等完整路径——这与 [builtin/ls-files.c:425-433](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/ls-files.c#L425-L433) 里「遇到稀疏目录就 `ensure_full_index`」的逻辑一致。
4. **预期结果**：`--sparse` 模式下条目数明显更少，且 cone 外的目录以「目录 + 斜杠」形式出现。`--sparse` 选项声明见 [builtin/ls-files.c:661-662](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/ls-files.c#L661-L662)。
5. 若本地未编译 git，命令行为「待本地验证」。

#### 4.1.6 小练习与答案

**练习 1**：为什么 `INDEX_EXPANDED` 的枚举值要特意写成 `= 0`？
**参考答案**：因为 `index_state` 经常用 `xcalloc` 清零分配，把「完整索引」设为 0 值，能让任何没显式初始化 `sparse_index` 的代码自动得到「完整索引」这一最安全、最兼容历史的状态。

**练习 2**：如果用户手动编辑 `.git/info/sparse-checkout` 写入了一个非 cone 的 glob（如 `*.md`），sparse-index 会怎样？
**参考答案**：`is_sparse_index_allowed` 会发现 `use_cone_patterns` 为假而返回 0（见 [sparse-index.c:195-196](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/sparse-index.c#L195-L196)），于是 `ensure_correct_sparsity` 走 `ensure_full_index` 分支，索引退化为完整索引。pattern 解析阶段已会对「坏的」非 cone 模式给出告警。

---

### 4.2 展开与折叠操作

#### 4.2.1 概念说明：两个方向

sparse-index 的核心动作只有两个方向：

- **折叠（collapse）**：完整索引 → 稀疏索引。入口 `convert_to_sparse`，递归核心 `convert_to_sparse_rec`。
- **展开（expand）**：稀疏索引 → 更完整的索引。入口 `expand_index`（可指定 pattern list 控制展开程度）和它的特例 `ensure_full_index`（展开到彻底完整）。

为什么需要反复横跳？因为**不是所有 git 命令都理解稀疏目录条目**。许多老命令（包括一些 builtin 子命令的内部逻辑）假设「索引里每条都是一个真实文件」。所以策略是：读索引时按需展开，写索引时尽量再折叠回去。

#### 4.2.2 核心流程：折叠 convert_to_sparse

折叠的递归算法 `convert_to_sparse_rec` 在 [sparse-index.c:60-130](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/sparse-index.c#L60-L130)。它依赖 cache-tree 提供的目录子树信息，伪代码如下：

```
convert_to_sparse_rec(区间 [start, end), 当前目录路径 ct_path, cache_tree 节点 ct):
    can_convert = 1
    # 前提 1：当前目录本身不在 sparse cone 内（cone 内的目录要保留全部文件）
    if path_in_sparse_checkout(ct_path): can_convert = 0

    # 前提 2：区间内所有条目都必须「可安全折叠」
    for 每个条目 ce in [start, end):
        if ce 处于合并冲突(stage!=0) 或 是子模块(gitlink) 或 未挂 SKIP_WORKTREE:
            can_convert = 0

    if can_convert:
        # 用一条稀疏目录条目替换整个区间
        se = construct_sparse_dir_entry(ct_path, ct)
        cache[num_converted++] = se
        return 1
    else:
        # 不能整体折叠：对每个子目录递归
        for 子目录 span in 区间（借助 cache_tree_subtree_pos 定位）:
            convert_to_sparse_rec(子区间, 子路径, ct->down[pos])
```

四个前提条件值得记住：

1. **目录不在 cone 内**：cone 内的目录必须保留全部文件条目（它们在工作树里是真实的）。
2. **无合并冲突条目**：冲突条目有 stage 1/2/3，不能折叠——`index_has_unmerged_entries` 还会做一次整表扫描兜底（见下）。
3. **无子模块（gitlink）**：gitlink 不能被 tree 折叠吞掉。
4. **全部条目都挂 `CE_SKIP_WORKTREE`**：只要有一个文件其实在工作树里，这个目录就不能整体消失。

外层入口 `convert_to_sparse` ([sparse-index.c:201-258](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/sparse-index.c#L201-L258)) 负责准备工作和善后：

```c
if (istate->sparse_index == INDEX_COLLAPSED || !istate->cache_nr ||
    !is_sparse_index_allowed(istate, flags))
    return 0;                          // 已折叠 / 空 / 不允许，直接返回
...
if (index_has_unmerged_entries(istate))   // 有冲突条目就不能折叠
    return 0;

if (!cache_tree_fully_valid(istate->cache_tree)) {   // 必须有有效的 cache-tree
    cache_tree_free(&istate->cache_tree);
    if (cache_tree_update(istate, WRITE_TREE_MISSING_OK))
        return 0;                      // cache-tree 重建失败就静默放弃
}
...
istate->cache_nr = convert_to_sparse_rec(istate, 0, 0, istate->cache_nr,
                                         "", 0, istate->cache_tree);
...
istate->sparse_index = INDEX_COLLAPSED;   // 标记为「已折叠」
```

注意它**对失败非常宽容**：任何一步不满足都「静默 return 0」，让索引保持完整状态，而不是报错中断命令——因为 sparse-index 只是优化，绝不应让命令失败。

#### 4.2.3 核心流程：展开 expand_index

展开 `expand_index` 在 [sparse-index.c:323-460](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/sparse-index.c#L323-L460)。它**构造一个全新的 `index_state`**，逐条搬运旧索引，遇到稀疏目录条目时把它「拆」回文件：

```
expand_index(istate, pl):     # pl: cone 模式 pattern list，可为 NULL
    if istate 已是 INDEX_EXPANDED: return            # 已经是完整索引，无需做
    if pl 非 cone 模式: pl = NULL                    # 退化成「彻底展开」
    full = 拷贝 istate 的元数据，新建空的 cache 数组
    full->sparse_index = pl ? PARTIALLY_SPARSE : EXPANDED

    for 每个条目 ce in istate:
        if ce 不是稀疏目录条目:
            直接搬到 full                          # 普通文件原样保留
        else:                                       # 遇到稀疏目录条目
            if pl 存在 且 该目录仍不在新 cone 内:
                原样搬到 full（不展开）             # 只展开「现在进入 cone」的目录
            else:
                tree = lookup_tree(ce->oid)          # 取出目录对应的 tree 对象
                read_tree_at(tree, ..., add_path_to_index)  # 遍历 tree，逐个建条目
                discard_cache_entry(ce)              # 旧目录条目用完即弃

    把 full 的 cache 数组、哈希表等字段拷回 istate
    重建 cache-tree
```

`add_path_to_index` ([sparse-index.c:268-321](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/sparse-index.c#L268-L321)) 是 `read_tree_at` 的回调，每访问到 tree 里的一个对象就 `make_cache_entry` 一条，并给重建出来的条目补上 `CE_SKIP_WORKTREE`（因为它们仍在 cone 外，只是被临时展开供命令使用）。这里有一处巧妙的 path 拼接：给目录补一个 `/-` 假文件名，再用 `path_matches_pattern_list` 判断该目录是否进入了新 cone——和 4.1.4 里 cone 匹配的把戏完全一致。

`ensure_full_index` ([sparse-index.c:462-467](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/sparse-index.c#L462-L467)) 只是 `expand_index(istate, NULL)` 的语法糖——传 NULL 表示「无条件彻底展开」。

#### 4.2.4 调度入口：读时修正、写时折叠

折叠/展开平时由谁触发？答案是**索引的读和写**两条钩子：

**读索引时**——`do_read_index` 在读完磁盘索引后，根据命令需求修正稀疏程度（[read-cache.c:2318-2330](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L2318-L2330)）：

```c
prepare_repo_settings(istate->repo);
if (istate->repo->settings.command_requires_full_index)
    ensure_full_index(istate);          // 本命令要完整索引 → 展开
else
    ensure_correct_sparsity(istate);    // 本命令兼容稀疏索引 → 按设置折叠/展开
```

其中 `ensure_correct_sparsity` ([sparse-index.c:469-479](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/sparse-index.c#L469-L479)) 是个分发器：允许稀疏就 `convert_to_sparse`，否则 `ensure_full_index`。

**写索引时**——`do_write_locked_index` 在落盘前调用 `convert_to_sparse`（[read-cache.c:3128-3130](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L3128-L3130)），尽量让磁盘上的 `.git/index` 保持折叠形态：

```c
int was_full = istate->sparse_index == INDEX_EXPANDED;
ret = convert_to_sparse(istate, 0);
```

关键开关是 `command_requires_full_index`。它默认为 1（[repo-settings.c:140](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/repo-settings.c#L140)）——也就是说**默认情况下所有命令都会强制展开索引**。只有那些**显式声明已适配 sparse-index 的命令**（在 `setup.c` 的命令表里清掉这个标志）才享受折叠带来的加速。这是一个谨慎的渐进式迁移策略：注释里明说「等所有用到索引的地方都加好保护后，这个开关会被移除」。

#### 4.2.5 按路径懒展开：expand_to_path

有些代码只是想查「索引里有没有某个路径」，没必要为这一个查询把整个索引展开。`expand_to_path` ([sparse-index.c:693-755](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/sparse-index.c#L693-L755)) 做的就是「**只展开目标路径所在的那个稀疏目录**」：

- 若 `path` 已在索引里 → 什么都不做。
- 否则沿 `path` 的各级父目录在 name-hash 里查；只要某个父目录命中（稀疏目录条目带斜杠，能被 name-hash 索引到），就说明 `path` 可能藏在这个稀疏目录里，于是 `ensure_full_index` 展开。

它用一个静态全局 `in_expand_to_path` 防止与 `index_file_exists` 互相递归。这是一个「为单点查询付出局部代价」的典型优化。

#### 4.2.6 代码实践：用 trace2 观察「展开后又折叠」

这是本讲的主实践，能让你亲眼看到 4.2.4 描述的「读时展开、写时折叠」循环。

1. **实践目标**：用结构化事件追踪 `GIT_TRACE2_EVENT`，验证 `git reset` 这类「既读又写」的命令会触发 `ensure_full_index`（展开）和 `convert_to_sparse`（折叠）两个事件。
2. **操作步骤**（接续 4.1.5 的 `spdemo` 仓库）：

   ```sh
   # 1) 触发一次读+写的命令，记录 trace2 事件
   GIT_TRACE2_EVENT="$(pwd)/trace.txt" \
       git reset -- outside/b.txt 2>/dev/null || GIT_TRACE2_EVENT="$(pwd)/trace.txt" git reset

   # 2) 在事件文件里找这两个 region
   grep '"region_enter".*"convert_to_sparse"' trace.txt
   grep '"region_enter".*"ensure_full_index"' trace.txt
   ```

   > region 名字来自源码里的 `trace2_region_enter("index", "convert_to_sparse", ...)` ([sparse-index.c:241](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/sparse-index.c#L241)) 和 `expand_index` 里的 `tr_region`（[sparse-index.c:375](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/sparse-index.c#L375)）。
3. **需要观察的现象**：`reset`（写索引）会同时出现 `ensure_full_index` 与 `convert_to_sparse` 两个 region；而 `git ls-files`（只读、不写）只出现 `ensure_full_index`。这与测试用例 [t1092-sparse-checkout-compatibility.sh:1409-1422](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/t/t1092-sparse-checkout-compatibility.sh#L1409-L1422) 的断言一致。
4. **预期结果**：写类命令有「展开 + 折叠」两个事件，纯读命令只有「展开」一个事件。
5. 若本地未编译 git 或无 trace2 输出，记为「待本地验证」。

#### 4.2.7 小练习与答案

**练习 1**：为什么 `convert_to_sparse` 在任何前提不满足时都「静默 return 0」而不是 `die()`？
**参考答案**：sparse-index 是纯粹的优化，不是正确性的一部分。如果某次折叠做不到（如有冲突条目、cache-tree 重建失败），最坏结果只是「索引保持完整、慢一点」，命令仍应正常完成。报错退出会把性能优化变成可靠性问题。

**练习 2**：`expand_index(istate, pl)` 传非 NULL 的 `pl` 与传 NULL（即 `ensure_full_index`）有何行为差异？
**参考答案**：传 NULL 是彻底展开，结果标记为 `INDEX_EXPANDED`；传 cone 模式 `pl` 是「按需展开」，只把「现在进入了新 cone 的稀疏目录」展开成文件，其余稀疏目录条目原样保留，结果标记为 `INDEX_PARTIALLY_SPARSE`。前者用于不认识稀疏目录的命令，后者用于 sparse-checkout 调整范围时。

---

### 4.3 与 sparse-checkout 的交互

#### 4.3.1 概念说明：sparse-index 是 sparse-checkout 的加速器

要把两者的关系一句话讲清：

- **sparse-checkout** 决定「**工作树里有哪些文件**」（一种使用方式）。
- **sparse-index** 决定「**索引里要不要为 cone 外的目录保留每一条文件记录**」（一种存储优化）。

sparse-index **依赖** cone 模式 sparse-checkout 才能成立（见 4.1.4），但二者并非同一件事：你可以用 cone 模式 sparse-checkout 而不开 sparse-index（索引仍是完整的，只是工作树小）；也可以反过来理解——sparse-index 只是把 sparse-checkout 已经在概念上「整目录忽略」的事实，在索引格式上落到实处。

#### 4.3.2 开关：index.sparse 配置

sparse-index 受仓库配置 `index.sparse` 控制。读取见 [repo-settings.c:80](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/repo-settings.c#L80)：

```c
repo_cfg_bool(r, "index.sparse", &r->settings.sparse_index, 0);   // 默认 false
```

默认是 **false（关闭）**，这是出于稳定性考虑的渐进策略。用户通常不直接写这个配置，而是通过 sparse-checkout 子命令来打开，因为打开它需要同步重写一次索引。

#### 4.3.3 通过 sparse-checkout 命令启停

用户启停 sparse-index 的正常入口是 `git sparse-checkout` 子命令的 `--sparse-index` 开关，例如：

```sh
git sparse-checkout init --cone --sparse-index   # init/set/reapply 都支持该开关
```

底层在 [builtin/sparse-checkout.c:418-444](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/sparse-checkout.c#L418-L444) 的 `update_modes`：

```c
if (*sparse_index >= 0) {
    if (set_sparse_index_config(repo, *sparse_index) < 0)   // 写 index.sparse 配置
        die(_("failed to modify sparse-index config"));
    repo_read_index(repo);                       // 重读索引
    repo->index->updated_workdir = 1;            // 标记需要重写
    if (!*sparse_index)
        ensure_full_index(repo->index);          // 关闭时强制展开一次
}
```

而 `set_sparse_index_config` ([sparse-index.c:132-140](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/sparse-index.c#L132-L140)) 同时写配置文件和更新运行时 `repo->settings.sparse_index`：

```c
int set_sparse_index_config(struct repository *repo, int enable)
{
    int res = repo_config_set_worktree_gently(repo, "index.sparse",
                                              enable ? "true" : "false");
    prepare_repo_settings(repo);
    repo->settings.sparse_index = enable;
    return res;
}
```

#### 4.3.4 折叠作为「数据结构」被复用：sparse-checkout clean

一个有趣的现象：sparse-index 的折叠能力本身被 sparse-checkout 子命令拿来当**工具**用。`git sparse-checkout clean` 用 `convert_to_sparse(..., SPARSE_INDEX_MEMORY_ONLY)` 把索引折叠到内存（不落盘），借此快速找到「哪些 cone 外目录还残留在工作树里、可以安全删除」（[builtin/sparse-checkout.c:1004-1006](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/sparse-checkout.c#L1004-L1006)）：

```c
if (convert_to_sparse(repo->index, SPARSE_INDEX_MEMORY_ONLY) ||
    repo->index->sparse_index == INDEX_EXPANDED)
    die(_("failed to convert index to a sparse index; ..."));
```

`SPARSE_INDEX_MEMORY_ONLY` 标志（[sparse-index.h:12](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/sparse-index.h#L12)）让 `is_sparse_index_allowed` 跳过「是否落盘、是否与 split-index 冲突、`index.sparse` 是否打开」等检查，纯粹把折叠当一次内存中的目录扫描来用。

#### 4.3.5 与 split-index 互斥

`is_sparse_index_allowed` 明确拒绝 split-index（[sparse-index.c:166-167](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/sparse-index.c#L166-L167)）：

```c
if (istate->split_index || git_env_bool("GIT_TEST_SPLIT_INDEX", 0))
    return 0;
```

这与 u4-l2 讲过的 split-index（共享基 + 增量位图）是两套正交的索引优化，叠加会让「哪些条目算数」变得过于复杂，所以目前互斥。读者可把这一点和 u4-l2 的 split-index 章节对照记忆。

#### 4.3.6 处理「cone 外却出现在磁盘上的文件」

一个边角情况：你 sparse-checkout 排除了某目录，但某个工具（编辑器、构建系统）又在磁盘上创建了该目录里的文件。这时索引里这些条目挂着 `CE_SKIP_WORKTREE`，但磁盘上其实有内容。`clear_skip_worktree_from_present_files` ([sparse-index.c:673-685](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/sparse-index.c#L673-L685)）负责把这类条目的 `CE_SKIP_WORKTREE` 清掉，让后续 `git status` 能如实报告它们。它对稀疏索引会先尝试在折叠形态下扫描，一旦发现某个稀疏目录在磁盘上真实存在，就返回「需要展开」信号，再退回完整索引扫描（[sparse-index.c:681-684](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/sparse-index.c#L681-L684)）。这是 sparse-index 与真实工作树保持一致的「自我修正」机制。

#### 4.3.7 代码实践：观察配置与磁盘布局的耦合

1. **实践目标**：把 `index.sparse` 配置、磁盘 `.git/index` 大小、`git ls-files --sparse` 三者对应起来。
2. **操作步骤**（接续 `spdemo`）：

   ```sh
   # 启用稀疏索引
   git sparse-checkout init --cone --sparse-index
   git sparse-checkout set cone
   cp .git/index .git/index.sparse

   # 关闭稀疏索引，强制完整
   git sparse-checkout set --no-sparse-index cone 2>/dev/null || git -c index.sparse=false read-tree -mu HEAD
   cp .git/index .git/index.full

   ls -l .git/index.sparse .git/index.full
   git config --get index.sparse
   ```

3. **需要观察的现象**：通常 `.git/index.sparse` 比 `.git/index.full` 小（条目更少）。`git config index.sparse` 在两种状态下分别应为 `true` / `false`。
4. **预期结果**：稀疏索引文件更小；配置开关与磁盘形态一致。
5. 实际压缩幅度与仓库大小相关，小仓库差异可能不明显，记为「待本地验证」。

#### 4.3.8 小练习与答案

**练习 1**：`SPARSE_INDEX_MEMORY_ONLY` 这个 flag 改变了 `is_sparse_index_allowed` 的哪些判断？
**参考答案**：它跳过了所有「与磁盘/配置相关」的检查——不检查 split-index、不读 `GIT_TEST_SPLIT_INDEX`、不读 `index.sparse` 配置是否打开。这让调用方可以把折叠纯粹当作一次内存中的目录聚合来用（如 sparse-checkout clean 找可删目录），而不要求仓库真的启用了 sparse-index。

**练习 2**：为什么 sparse-checkout `set` 改变 cone 范围后，需要重写一次索引？
**参考答案**：cone 范围变了，哪些目录该折叠、哪些该展开也随之改变。`update_modes` 在改完配置后设 `updated_workdir = 1` 并重读索引，正是为了让后续的写索引流程按**新**的 cone 范围重新折叠（或展开），保证磁盘索引与 sparse-checkout 定义一致。

---

## 5. 综合实践

把本讲三条主线串起来：**表示（4.1）、折叠/展开（4.2）、与 sparse-checkout 交互（4.3）**。

任务：在一个有多层目录的小仓库里，完整走一遍 sparse-index 的生命周期，并用 trace2 在关键节点验证你的判断。

建议步骤：

1. 建一个含至少两个顶层目录（如 `keep/`、`hide/`，且 `hide/` 下有嵌套子目录和若干文件）的仓库并提交。
2. 执行 `git sparse-checkout init --cone --sparse-index` 后 `git sparse-checkout set keep`。
3. **预测**再**验证**：用 `git ls-files --sparse` 看 `hide/` 是否变成一条带斜杠的稀疏目录条目（4.1）。
4. 用 `GIT_TRACE2_EVENT` 追踪一次 `git status`（一个已适配 sparse-index 的命令）：预测它**不会**触发 `ensure_full_index`，再用 grep 验证（4.2.4 的 `command_requires_full_index` 在此命令上应为 0）。
5. 用 `GIT_TRACE2_EVENT` 追踪一次 `git ls-files`（不带 `--sparse`）：预测它会触发 `ensure_full_index`（因为 ls-files 命中稀疏目录就展开，见 [builtin/ls-files.c:425-433](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/ls-files.c#L425-L433)），并验证。
6. 在 `hide/` 下手工创建一个新文件，运行 `git status`，观察 `clear_skip_worktree_from_present_files` 是否让该文件浮现为 untracked（4.3.6）。
7. 把你的预测和实际 trace2 结果列成对照表；任何不一致都回去重读对应源码段落，解释差异来源。

> 提示：如果你改造的 git 是自己编译的，可以在 `sparse-index.c` 的 `convert_to_sparse` 和 `expand_index` 入口各加一行 `printf`（仅用于学习，勿提交），让「展开/折叠」肉眼可见——这比读 trace2 更直观。

---

## 6. 本讲小结

- sparse-index 解决的是**巨型单仓库下索引 \(O(N)\) 条目的读写瓶颈**：把 cone 模式 sparse-checkout 中「整目录被忽略」的事实，在索引格式上落实为**一条稀疏目录条目**，把条目数压到接近 cone 内文件数。
- 一条**稀疏目录条目**就是普通 `cache_entry` 的三种伪装：mode 为 `S_IFDIR`、`oid` 指向目录的 tree 对象、路径名带末尾斜杠，并挂 `CE_SKIP_WORKTREE`。
- 索引稀疏程度由 `enum sparse_index_mode`（`INDEX_EXPANDED` / `INDEX_COLLAPSED` / `INDEX_PARTIALLY_SPARSE`）刻画；`INDEX_EXPANDED=0` 保证零初始化即「完整索引」，向后兼容。
- **折叠** `convert_to_sparse` 借助 cache-tree 递归，前提是：目录不在 cone 内、无冲突条目、无 gitlink、全部条目挂 `CE_SKIP_WORKTREE`；任何前提不满足都静默放弃。
- **展开** `expand_index` 重建一个新索引，遇稀疏目录条目就读其 tree 拆回文件；传 NULL（`ensure_full_index`）彻底展开，传 cone pattern list 则只展开进入新 cone 的目录（部分稀疏）。
- 调度在索引的**读**（`do_read_index` → `ensure_correct_sparsity` 或 `ensure_full_index`，受 `command_requires_full_index` 控制）和**写**（`do_write_locked_index` → `convert_to_sparse`）两个钩子上；默认所有命令强制完整索引，只有显式适配的命令才享受折叠加速，这是一项渐进式迁移。
- sparse-index 依赖 **cone 模式 sparse-checkout** 与 `index.sparse` 配置，通过 `git sparse-checkout --sparse-index` 启停；它与 **split-index 互斥**。

---

## 7. 下一步学习建议

- **顺接本单元**：如果你还没读透 cache-tree，回头重读 u4-l2，重点看 `cache_tree_update` 如何为 sparse-index 提供「目录 → tree oid」的折叠依据；本讲的折叠算法完全建立在它之上。
- **看真实测试**：[t/t1092-sparse-checkout-compatibility.sh](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/t/t1092-sparse-checkout-compatibility.sh) 是 sparse-index 最完整的端到端测试集，里面用 `test_region` 检查 `convert_to_sparse` / `ensure_full_index` 事件的本讲 4.2.6 实践就取材于此，值得通读。
- **跟进命令适配**：在 `setup.c` 的命令表里搜索哪些命令清掉了 `command_requires_full_index`，可以追踪「哪些命令已经享受 sparse-index 加速」这一渐进迁移的进度。
- **下一单元**：本单元（u4 索引）已完结。下一单元 u5 进入**引用 refs**——`ref_store` 抽象与三种后端（files / packed / reftable）。索引回答「工作树与提交之间有哪些文件」，引用回答「提交图上的命名指针指向哪里」，二者共同构成 git 的可寻址数据底座。

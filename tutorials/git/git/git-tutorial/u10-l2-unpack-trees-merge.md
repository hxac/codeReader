# unpack-trees 三方合并与冲突

## 1. 本讲目标

本讲是「合并 merge」单元的第二讲，承接 u10-l1（merge-ort 与合并策略）。u10-l1 讲的是 `git merge` 这条 porcelain 命令如何用 ort 策略在内存里算出合并结果；本讲往下走一层，聚焦 `unpack-trees.c` 这个**通用的「多路树 + 索引同步遍历」引擎**在合并中扮演的角色，以及与它配套的两个机制：内容级三方合并（`merge-blobs.c`）和冲突撤销记录（`resolve-undo.c`）。

学完后你应该能够：

- 说清 `unpack-trees.c` 的合并回调（`threeway_merge` / `twoway_merge`）在合并链路中的位置，以及它**只做文件级（entry 级）判定、不做内容合并**这一关键边界。
- 追踪一个「双方都修改了同一个文件」的条目如何走进 `threeway_merge` 的「no merge」分支，并以 stage 1/2/3 三条记录的形式落进索引。
- 区分三层合并职责：`unpack-trees`（文件级冲突判定）→ `merge-blobs`（blob 级内容合并，服务于 plumbing `git merge-tree`）→ `merge-ll/xdiff`（真正生成 `<<<<<<<` 标记的行级合并，u10-l1 已讲）。
- 理解 `resolve-undo` 如何在冲突**被解决**时记录原始各 stage 的 mode/oid，从而支撑 `git checkout --merge`、`git update-index --unresolve`、`rerere` 把已解决的冲突「还原」回去。

## 2. 前置知识

本讲默认你已掌握 u9-l2（checkout/switch 与 unpack-trees）中讲过的内容，重点回顾三个概念：

- **三层数据模型**：工作树、索引（`index_state`）、对象数据库。索引里的每条 `cache_entry` 除了路径/模式/oid，还有一个 **stage** 字段（占用 `ce_flags` 的高 2 位，见 `CE_STAGEMASK`）。stage 0 表示「已合并/正常」；stage 1/2/3 表示三方合并的 base / ours / theirs。
- **unpack-trees 两阶段引擎**：阶段一是多路遍历合并（`traverse_trees` 驱动回调，只产生决策与内存标志），阶段二是 `check_updates` 物理写工作树（先删 `CE_WT_REMOVE`、后写 `CE_UPDATE`）。本讲聚焦阶段一里的合并回调。
- **`unpack_trees_options`**：贯穿整个合并过程的状态结构，其中 `fn` 字段是一个函数指针 `merge_fn_t`，决定「拿到一组同名条目后用什么策略裁决」。

再补一个本讲要用到的小术语：

- **stage 编号约定**：stage 1 = 共同祖先（base），stage 2 = 我们这边（ours / HEAD），stage 3 = 对方那边（theirs / remote）。这个编号贯穿 `unpack-trees`、索引磁盘格式、`git ls-files -u` 输出。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [unpack-trees.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/unpack-trees.c) | 通用多路树合并引擎。本讲重点：`unpack_single_entry`（为每个路径组装 stages 数组）、`threeway_merge`（三方文件级裁决）、`merged_entry`/`deleted_entry`/`keep_entry`（三种裁决动作）、`same`（条目相等判定）。 |
| [unpack-trees.h](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/unpack-trees.h) | `merge_fn_t` 回调类型与 `struct unpack_trees_options` 定义。 |
| [merge-blobs.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-blobs.c) | blob 级三方内容合并：读三个 blob 对象的内容，调 `ll_merge` 产出合并后缓冲区（含冲突标记）。服务于 plumbing `git merge-tree`。 |
| [resolve-undo.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/resolve-undo.c) | 冲突撤销信息：`record_resolve_undo`（解决冲突时记录原始 stage）、`resolve_undo_write/read`（索引 `REUC` 扩展的序列化）、`unmerge_index`（把已解决冲突还原回 stage 1/2/3）。 |
| [resolve-undo.h](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/resolve-undo.h) | `struct resolve_undo_info` 与相关函数声明。 |
| [read-cache.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c) | `remove_index_entry_at` 里调用 `record_resolve_undo`，是撤销信息的实际触发点。 |
| [builtin/read-tree.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/read-tree.c) | `git read-tree -m` 如何据树的数量挑选 `threeway_merge`，是本讲实践最直接的入口。 |

## 4. 核心概念与源码讲解

### 4.1 unpack-trees 合并回调

#### 4.1.1 概念说明

`unpack-trees` 是一个**与具体合并策略无关的遍历引擎**：它把「N 棵 tree + 当前索引」按路径名同步遍历，每遇到一个路径就把各路里同名的那组条目打包成一个 `stages[]` 数组，交给一个回调函数 `o->fn` 去裁决。裁决回调返回「这一组条目该怎么处理」，引擎据此把结果写进一张全新的 `o->internal.result` 索引，最后再由 `check_updates` 把变更落到工作树。

不同的合并语义靠**换回调**实现，而不是换引擎：

- `threeway_merge`：三方合并（base/ours/theirs），`git read-tree -m base ours theirs`、`git merge` 的部分路径走它。
- `twoway_merge`：两方合并（oldtree/newtree），`git checkout`/`git switch` 切分支走它（u9-l2 已讲）。
- `oneway_merge` / `bind_merge`：单树覆盖。

本讲的核心问题是：**当 `threeway_merge` 拿到「ours 和 theirs 都改了同一个文件、且改得不一样」的一组条目时，它做什么？** 答案是——它不合并文件内容，只把 base/ours/theirs 三个版本以 stage 1/2/3 的形式**原样留在索引里**，标记为冲突。真正的内容合并（生成带 `<<<<<<<` 标记的文件）是更上层的事（`git merge` 走 merge-ort；`git merge-tree` 走 merge-blobs）。

> 一句话边界：`unpack-trees` 的合并回调做的是**文件级（entry 级）的「这个文件是增/删/改/冲突」判定**，不做**内容级**的行合并。

#### 4.1.2 核心流程

一次 `git read-tree -m base ours theirs`（三方）在 `unpack-trees` 里的执行过程：

```text
cmd_read_tree
  └─ unpack_trees(trees=[base, ours, theirs], o)        # 入口
       ├─ o->internal.merge_size = 3                     # 记录树的数量
       ├─ o->fn = threeway_merge                         # 按树数挑选回调
       ├─ traverse_trees(...)                            # 同步遍历 N 棵 tree
       │     └─ 每个路径调用 unpack_callback
       │           └─ unpack_single_entry(n, mask, dirmask, names, info)
       │                 ├─ 为每棵树造一条 cache_entry，按 i+1 与 head_idx 比较
       │                 │   决定 stage：1=base / 2=ours / 3=theirs
       │                 ├─ src[0]        = 索引里同名条目（若有）
       │                 ├─ src[1..n]     = 各树的 stage 条目
       │                 └─ call_unpack_fn(src, o)  →  o->fn(src, o)
       │                       └─ threeway_merge(stages, o)
       │                             ├─ 命中 trivial 分支 → merged_entry / deleted_entry
       │                             └─ 「no merge」分支 → keep_entry(stage1/2/3)  ← 冲突
       ├─ 遍历结束，o->internal.result 就是合并后的新索引
       └─ check_updates(result)                          # 阶段二：写工作树
```

`threeway_merge` 内部的判定顺序（从上到下短路）：

1. 若 `head` 与 `remote` 完全相同（`same`）→ 直接取 `head`，`merged_entry`（双方一致改同一样）。
2. 若只有一方相对 base 改了（`head_match` 或 `remote_match`）→ 取改了的那一方，`merged_entry`（单方修改）。
3. 若 base 缺失且双方相同新增 → `merged_entry`。
4. `aggressive` 模式下额外处理「双方都删」「一方删一方未改」等 trivial 删除。
5. **以上都不命中** → 进入「no merge」分支（源码注释里的 #2/#3/#4/#6/#7/#9/#10/#11 等情形）：用 `keep_entry` 把 base（stage 1）、ours（stage 2）、theirs（stage 3）逐条保留进结果索引 → **冲突被记录**。

#### 4.1.3 源码精读

**先看 stages 数组怎么拼出来**——`unpack_single_entry` 给每棵树的条目分配 stage：

[unpack-trees.c:1211-1239](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/unpack-trees.c#L1211-L1239) 这段循环把第 `i` 棵树的条目放进 `src[i + o->merge]`，并用 `i+1` 与 `o->head_idx` 的大小关系决定 stage：小于 `head_idx` 的是祖先（stage 1），等于的是 ours（stage 2），大于的是 theirs（stage 3）。`o->merge` 是为 `src[0]` 预留的索引条目槽位。

stage 最终落在 `ce_flags` 上，由 `create_ce_entry` 写入：

[unpack-trees.c:1066-1095](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/unpack-trees.c#L1066-L1095) `create_ce_entry` 用 `create_ce_flags(stage)` 把 stage 编码进 `ce_flags`，再设 mode/oid/路径。注意它是「transient」条目——合并回调看完就丢弃，不直接进索引；真正进索引要靠下面的 `merged_entry`/`keep_entry` 再 `dup` 一份。

**条目相等判定 `same`** 是所有 trivial 分支的基础：

[unpack-trees.c:2204-2214](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/unpack-trees.c#L2204-L2214) `same(a,b)` 只比 `ce_mode` 与 `oid`；任一方带 `CE_CONFLICTED` 就视为不等。也就是说「文件级相同」= 同模式 + 同内容哈希，与 stat、时间戳无关。

**三种裁决动作**：

[unpack-trees.c:2559-2642](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/unpack-trees.c#L2559-L2642) `merged_entry(ce, old, o)`：把 `ce` 作为已合并结果加入索引（stage 0）。若 `old` 存在且与之相同则直接复用旧条目（省一次 stat）；否则校验 `old` 未被本地改动（`verify_uptodate`）后用新值覆盖。返回 1 表示「这条已处理」。

[unpack-trees.c:2675-2693](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/unpack-trees.c#L2675-L2693) `deleted_entry`：标记删除（`CE_REMOVE`，阶段二升级为 `CE_WT_REMOVE` 真正删文件）。

[unpack-trees.c:2695-2702](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/unpack-trees.c#L2695-L2702) `keep_entry(ce, o)`：**原样保留**条目，stage 不动。关键是它调 `add_entry(o, ce, 0, 0)`——`set=0, clear=0`，stage 字段原封不动保留。冲突时正是靠它把 stage 1/2/3 三条都留下。注意 `if (ce_stage(ce)) invalidate_ce_path(...)`：保留冲突条目会让 cache-tree 失效（因为目录树不再能用单一 oid 代表）。

`add_entry` → `do_add_entry` 最终落到 `add_index_entry(&o->internal.result, ce, ...)`：

[unpack-trees.c:217-228](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/unpack-trees.c#L217-L228) `do_add_entry` 用 `ce->ce_flags = (ce->ce_flags & ~clear) | set` 合并标志位，stage 因此被保留；`ADD_CACHE_OK_TO_REPLACE` 允许同名同 stage 替换。

**核心：`threeway_merge` 的「no merge」尾部**——冲突就在这里落地：

[unpack-trees.c:2877-2899](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/unpack-trees.c#L2877-L2899) 设 `o->internal.nontrivial_merge = 1` 标记遇到非平凡合并，然后：若没有 `head_match`/`remote_match`，先从祖先里 `keep_entry` 一个 base（stage 1）；再 `keep_entry(head)`（stage 2）、`keep_entry(remote)`（stage 3）。三者全留 = 冲突。返回 `count`（保留了几条）。

要理解为什么能走到尾部，看前面的短路条件：

[unpack-trees.c:2761-2805](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/unpack-trees.c#L2761-L2805) 先用 `same(remote, head)` 判断双方是否一致；再遍历祖先算 `head_match`/`remote_match`（ours 或 theirs 是否与某个祖先相同，即「这一方其实没改」）。若 `same(head, remote)` 或单方未改，就 `merged_entry` 提前返回。**只有双方都改了、且改得不同**（既不 `same(head,remote)`，又 neither `head_match` nor `remote_match`）才会一路落到尾部 `keep_entry` 三阶段。

注释里的 `#1`…`#21` 案例编号对应 [t/t1000-read-tree-m-3way.sh](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/t/t1000-read-tree-m-3way.sh)，那是三方合并各种文件级情形的权威测试集。

最后看 `read-tree` 如何挑选回调：

[builtin/read-tree.c:237-258](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/read-tree.c#L237-L258) 按传入树的数量 `stage-1` 分派：1 棵→`oneway_merge`/`bind_merge`，2 棵→`twoway_merge`，3 棵及以上→`threeway_merge`；并对 3 棵的情况算出 `head_idx = stage - 2 = 2`（base 在树 0/stage 1，ours 在树 1/stage 2，theirs 在树 2/stage 3）。

#### 4.1.4 代码实践

**实践目标**：用 plumbing 命令 `git read-tree -m` 直接驱动 `threeway_merge`，复现「双方都修改同一文件」的冲突，观察它如何以 stage 1/2/3 落进索引，再对照源码确认走的就是 `keep_entry` 尾部分支。

**操作步骤**（在一个临时空目录里做，不要在 git 源码仓库里做）：

```bash
mkdir /tmp/uu-test && cd /tmp/uu-test && git init -q
export GIT_AUTHOR_NAME=T GIT_AUTHOR_EMAIL=t@e GIT_COMMITTER_NAME=T GIT_COMMITTER_EMAIL=t@e

# 1) 造 base：f.txt 内容为 line1
printf 'line1\n' > f.txt
git add f.txt && git commit -q -m base

# 2) 在 ours 分支改 f.txt
git branch ours
git checkout -q ours
printf 'line1-ours\n' > f.txt && git commit -qam ours

# 3) 在 theirs 分支改同一行 f.txt（改成不同内容）
git checkout -q master
git branch theirs
git checkout -q theirs
printf 'line1-theirs\n' > f.txt && git commit -qam theirs

# 4) 回到 base，用 read-tree 做三方合并（直接驱动 threeway_merge）
git checkout -q master
git read-tree -m master ours theirs

# 5) 查看索引里的 stage
git ls-files -u      # 只列未合并（stage>0）条目
git ls-files -s      # 列全部条目及 stage
```

**需要观察的现象**：`git ls-files -u` 应为 `f.txt` 输出**三行**，每行第二列是 stage 号 1/2/3，第三列是各自的 oid（base/ours/theirs 三个不同哈希）。

**预期结果**（精确 oid 待本地验证，结构应如下）：

```text
100644 <oid-base>   1	f.txt
100644 <oid-ours>   2	f.txt
100644 <oid-theirs> 3	f.txt
```

`git read-tree -m` 不会写出工作树文件（没加 `-u`），也不会做内容合并——它只是把 `threeway_merge` 的裁决结果写进索引。三条 stage 同时存在，正是源码 `keep_entry(stages[1])` + `keep_entry(head)` + `keep_entry(remote)` 的直接产物。

**对照源码**：回到 [unpack-trees.c:2877-2899](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/unpack-trees.c#L2877-L2899)，确认 `f.txt` 满足「双方都改且改得不同」：`same(head, remote)` 为假（ours/theirs 内容不同），`head_match`/`remote_match` 都为 0（ours 与 base 不同、theirs 与 base 也不同），故所有 trivial 分支都不命中，落到尾部三条 `keep_entry`，留下 stage 1/2/3。

> 若想看 `threeway_merge` 的 trivial 分支长什么样，可把第 4 步的 ours 改成与 theirs **相同**的内容，重跑后 `git ls-files -u` 应无输出（`same(head,remote)` 命中 → `merged_entry` 只留 stage 0）。这一对比待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`threeway_merge` 里 `head_match` 和 `remote_match` 各表示什么？为什么有它们就能避免把「只有一方改了」的文件误判为冲突？

参考答案：`head_match` 表示 ours 与某个祖先（base）相同——即 ours 这一方**没改**这个文件；`remote_match` 同理表示 theirs 没改。若只有一方改了文件，必然有一方 `*_match` 命中，于是 `threeway_merge` 在 [unpack-trees.c:2798-2805](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/unpack-trees.c#L2798-L2805) 提前 `merged_entry` 取改了的那一方，不会落到尾部 `keep_entry` 三阶段。只有双方都改（两个 match 都不命中）且改得不同（`!same(head,remote)`）才进冲突分支。

**练习 2**：为什么 `keep_entry` 对 `ce_stage(ce)` 非零的条目要调 `invalidate_ce_path`？

参考答案：`keep_entry` 保留了 stage 1/2/3 的冲突条目，这意味着该路径处于未合并状态，不能再用一个 tree oid 代表其所在目录。`invalidate_ce_path` 沿父链把 cache-tree 节点置为无效（`entry_count = -1`），确保后续 `write-tree` 不会误用一个陈旧的目录哈希（见 u4-l2 cache-tree 的惰性失效机制）。

**练习 3**：`git read-tree -m base ours theirs` 与 `git merge` 处理同一个「双方都改了 f.txt」的场景，结果有何本质区别？

参考答案：`read-tree -m` 只走 `unpack-trees` 的文件级 `threeway_merge`，结果是把 stage 1/2/3 三条原样写进索引、**不生成合并后的工作树文件**。`git merge` 则由 merge-ort 在内存里做内容级合并（调 merge-ll/xdiff），生成带 `<<<<<<<` 标记的文件写进工作树，再把 stage 1/2/3 与 AUTO_MERGE 一起记录进索引（见 u10-l1）。前者只判定「是否冲突」，后者还会尝试合并内容并产出冲突标记。

### 4.2 merge-blobs 三方内容合并

#### 4.2.1 概念说明

4.1 节强调 `unpack-trees` 只做文件级判定。那么「真把两个版本的内容合并成一个带冲突标记的文件」这件事，git 里由谁做？答案分两条路：

- porcelain `git merge`：merge-ort 在内存里调 `merge-ll.c::ll_merge` → xdiff，自己产出合并内容（u10-l1 已讲）。
- plumbing `git merge-tree`：走 `merge-blobs.c::merge_blobs`，以三个 **blob 对象**为输入，读出内容后同样调 `ll_merge` 产出合并缓冲区。

本节讲第二条路。`merge_blobs` 是一个非常薄的适配层：它把「blob 对象」翻译成 `ll_merge` 需要的 `mmfile_t`（内存文件），处理「一方删除」的退化情形，然后把真正的行级合并交给 `merge-ll`。它的存在让 `git merge-tree` 这个 plumbing 命令能在不碰工作树、不碰索引的前提下，纯粹在对象数据库上算出三方合并结果。

> 关键区分：`unpack-trees` 回答「这个文件是不是冲突」；`merge-blobs` 回答「冲突文件合并后的字节长什么样」。两者是上下游关系，不是替代关系。

#### 4.2.2 核心流程

```text
git merge-tree <base> <ours> <theirs>          # plumbing
  └─ 对每个需要合并的路径，拿到 base/our/their 三个 blob
     └─ merge_blobs(istate, path, base, our, their, &size)
           ├─ 若 our 或 their 为空（一方删除）：
           │     base 也空 → 取存在的一方内容（双方各自新增同路径）
           │     base 非空 → 返回 NULL（删/改冲突，交给上层判定）
           ├─ fill_mmfile_blob(our)  → 从对象库读 blob 内容进 mmfile_t
           ├─ fill_mmfile_blob(their)
           ├─ fill_mmfile_blob(base)（base 为空则用空串 ""）
           └─ three_way_filemerge(common, our, their)
                 └─ ll_merge(...)            # merge-ll.c
                       └─ xdl_merge(...)      # xdiff，产出 <<<<<<< 标记
                 返回合并后缓冲区 + LL_MERGE_OK / _CONFLICT / _BINARY_CONFLICT
```

`ll_merge` 的返回值是 `enum ll_merge_result`：

[merge-ll.h:91-94](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ll.h#L91-L94) `LL_MERGE_ERROR = -1`、`LL_MERGE_OK = 0`、`LL_MERGE_CONFLICT`（有冲突但已产出带标记的内容）、`LL_MERGE_BINARY_CONFLICT`（二进制文件无法合并）。注意即便 `LL_MERGE_CONFLICT`，`merge_blobs` 仍返回合并后的缓冲区（含 `<<<<<<<`/`=======`/`>>>>>>>` 标记），由调用方决定如何处理。

#### 4.2.3 源码精读

**入口 `merge_blobs`**：

[merge-blobs.c:62-106](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-blobs.c#L62-L106) 先处理「一方删除」的退化情形：若 `!our || !their`，且 `base` 也为空（双方都新増同路径），直接读存在一方的 blob 内容返回；若 `base` 非空（一方删一方改），返回 `NULL`（注释明确说明这依赖调用方已就「删了一个被对方改过的文件」发过警告）。正常三方情形下，把三个 blob 经 `fill_mmfile_blob` 装进 `mmfile_t`，base 为空时用空串兜底，最后调 `three_way_filemerge`。

**把 blob 读成内存文件**：

[merge-blobs.c:9-26](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-blobs.c#L9-L26) `fill_mmfile_blob` 用 `odb_read_object` 从对象库读出 blob 的原始字节，校验类型确为 `OBJ_BLOB`，填进 `mmfile_t { ptr, size }`。这一步把「按 oid 寻址的对象」变成「内存里的字节缓冲」，是对象数据库与行级 diff 库之间的标准桥接。

**真正的合并**：

[merge-blobs.c:33-60](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-blobs.c#L33-L60) `three_way_filemerge` 调 `ll_merge`，把 base/our/their 三个 `mmfile_t` 交给 merge-ll。注释指出此函数只被 `cmd_merge_tree` 用，不读 `merge.conflictstyle` 配置，故标签硬编码为 `.our`/`.their`。`LL_MERGE_BINARY_CONFLICT` 时只发 `warning`，仍返回缓冲区。

**调用方**：

[builtin/merge-tree.c:72-97](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/merge-tree.c#L72-L97) `result()` 按阶段号拼出 base/our/their 三个 blob（stage 1→base、2→our、3→their），调 `merge_blobs` 拿到合并后内容，供 `git merge-tree` 输出冲突文件的实际合并结果。

#### 4.2.4 代码实践

**实践目标**：用 plumbing `git merge-tree` 直接驱动 `merge_blobs`，观察一个「双方都改了同一行」的文件合并后产生的冲突标记，确认内容级合并确实由 `merge_blobs → ll_merge → xdiff` 完成。

**操作步骤**（接 4.1.4 的临时仓库，或另建一个同样的 base/ours/theirs 三分支仓库）：

```bash
cd /tmp/uu-test
# 用 plumbing 三方合并：git merge-tree <base> <ours> <theirs>
git merge-tree master ours theirs
```

**需要观察的现象**：输出里会有一个「merged」文件段落，内容包含 `<<<<<<< .our`、`=======`、`>>>>>>> .their` 三组标记，分别包住 ours 与 theirs 的版本。

**预期结果**（待本地验证具体换行，结构应如下）：

```text
<<<<<<< .our
line1-ours
=======
line1-theirs
>>>>>>> .their
```

**对照源码**：标记里的 `.our`/`.their` 正来自 [merge-blobs.c:49-51](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-blobs.c#L49-L51) 传给 `ll_merge` 的标签参数。这说明你看到的冲突标记就是 `three_way_filemerge` 经 `ll_merge` 产出的，而 `git merge-tree` 没有经过 `unpack-trees` 的索引/工作树流程——它是纯对象库上的合并。

#### 4.2.5 小练习与答案

**练习 1**：`merge_blobs` 在 `!our || !their` 时为什么有时返回 NULL、有时返回某个 blob 的内容？

参考答案：见 [merge-blobs.c:76-84](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-blobs.c#L76-L84)。一方为空表示那一方删除了文件：若 base 也为空，说明双方都新増了同路径文件（一方有内容一方没有的「新增/新增」退化），取存在一方的内容即可；若 base 非空，说明是一方删除、一方修改的冲突，无法用内容合并解决，返回 NULL 交上层判定，且注释提醒调用方须先就「删了一个被改过的文件」发过警告。

**练习 2**：为什么说 `merge-blobs` 是「非常薄的适配层」？它自己实现了行级 diff 算法吗？

参考答案：没有。`merge_blobs` 只做三件事：处理删除退化、用 `fill_mmfile_blob` 把 blob 对象读成 `mmfile_t`、调 `ll_merge`。真正的行级 LCS/Myers 算法在 xdiff 里（u8-l2），由 `ll_merge` 转交。`merge-blobs` 的职责仅是「对象数据库 ↔ 行级合并库」的格式桥接。

### 4.3 resolve-undo 撤销信息

#### 4.3.1 概念说明

合并产生冲突后，索引里会留下 stage 1/2/3。你解决冲突（编辑文件、`git add`）后，stage 1/2/3 被一条 stage 0 的已合并条目替换。但 git 允许你**反悔**——把已经解决的冲突「还原」回未合并状态，重新看三路差异。这就是 `git checkout --merge`、`git update-index --unresolve`、以及 `rerere` 机制背后的能力。

要能还原，就必须在「解决」的那一刻**记下原来的三个 stage 各是什么**（mode 与 oid）。这个记录就是 `resolve-undo`，在磁盘上以索引的 `REUC` 扩展区形式存在（见 u4-l1 的索引扩展 TLV 设计）。

> 关键时间点：`resolve-undo` 记录是在冲突**被解决**（stage 条目从索引移除）时创建的，而不是在冲突**被产生**时。冲突产生靠 `keep_entry`（4.1）；冲突解决靠上层合并工具；解决时 stage 条目被删除，`remove_index_entry_at` 触发 `record_resolve_undo` 把它们存进 REUC；之后想还原就调 `unmerge_index` 把 REUC 里的信息变回 stage 1/2/3。

#### 4.3.2 核心流程

```text
冲突已存在：索引有 f.txt 的 stage 1/2/3
  │
  │  用户解决冲突并 git add f.txt
  ▼
解决路径：stage 1/2/3 被移除，写入 stage 0
  └─ remove_index_entry_at(istate, pos)             # read-cache.c
        └─ record_resolve_undo(istate, ce)          # 对每条 stage>0 的被删条目
              └─ 把 ce 的 mode/oid 存进 istate->resolve_undo[path]
  │
  │  索引落盘：resolve_undo_write 把 istate->resolve_undo 序列化进 REUC 扩展
  ▼
后来用户执行 git checkout --merge（或 update-index --unresolve / rerere）
  └─ unmerge_index(istate, pathspec, CE_MATCHED)
        └─ 对每个匹配的已记录路径：
              └─ unmerge_index_entry(istate, path, ru, flags)
                    ├─ 移除当前 stage 0 条目
                    └─ 按 ru->mode[i]/ru->oid[i] 重建 stage 1/2/3 条目
```

数据结构很小：

```c
struct resolve_undo_info {
    unsigned int mode[3];       // stage 1/2/3 的模式；0 表示该 stage 不存在
    struct object_id oid[3];    // stage 1/2/3 的对象哈希
};
```

它挂在 `istate->resolve_undo`（一个以路径为键的 `string_list`）上，每条路径对应一个 `resolve_undo_info`。

#### 4.3.3 源码精读

**数据结构与接口**：

[resolve-undo.h:11-23](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/resolve-undo.h#L11-L23) 声明 `struct resolve_undo_info`（`mode[3]` + `oid[3]`）及 `record_resolve_undo`/`resolve_undo_write`/`resolve_undo_read`/`resolve_undo_clear_index`/`unmerge_index*` 等接口。`mode[i]==0` 是「该 stage 缺席」的哨兵，序列化时据此省略对应 oid。

**记录：`record_resolve_undo`**：

[resolve-undo.c:12-34](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/resolve-undo.c#L12-L34) 这是撤销信息的生成点。核心逻辑：`stage = ce_stage(ce)`，若 `stage==0` 直接返回（只记录未合并条目）；否则在 `istate->resolve_undo` 这个 string_list 里按 `ce->name` 插入一项，把 `ce->oid` 与 `ce->ce_mode` 写进 `ui->oid[stage-1]` / `ui->mode[stage-1]`。注意是**按 stage 索引累加**——同一路径的 stage 1/2/3 在三次删除里分别填进同一个 `ui`，最终拼成完整的三方信息。

**触发点：`remove_index_entry_at`**：

[read-cache.c:583-597](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L583-L597) 每次从索引移除一条 `cache_entry` 都会先 `record_resolve_undo(istate, ce)`。由于 `record_resolve_undo` 对 stage 0 直接返回，所以只有移除未合并条目（stage 1/2/3）时才真正记录。这正是「解决冲突」时 stage 条目被逐条删除、撤销信息被逐条攒下的现场。

**序列化：`resolve_undo_write`**：

[resolve-undo.c:36-56](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/resolve-undo.c#L36-L56) 写成 TLV：路径 + `\0` + 三个八进制 mode 各跟 `\0` + （对 `mode[i]!=0` 的）写 `rawsz` 字节 oid。这是索引 `REUC` 扩展的磁盘格式，遵循 u4-l1 讲过的「4 字母签名 + TLV」扩展区约定。

**反序列化：`resolve_undo_read`**：

[resolve-undo.c:58-111](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/resolve-undo.c#L58-L111) `resolve_undo_write` 的逆过程，按同样顺序解析 mode 与 oid；格式不符则 `error("Index records invalid resolve-undo information")`。哈希长度取自 `algop->rawsz`，故兼容 SHA-1/SHA-256（u3-l2）。

**清除：`resolve_undo_clear_index`**：

[resolve-undo.c:113-122](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/resolve-undo.c#L113-L122) 释放并清空 `istate->resolve_undo`，置 `RESOLVE_UNDO_CHANGED` 让索引重写时丢弃 `REUC` 扩展。`git read-tree` 在合并开始前就调它（[builtin/read-tree.c:207](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/read-tree.c#L207)）——因为新的合并会产生自己的一套冲突，旧的撤销信息不再有意义。

**还原：`unmerge_index_entry` 与 `unmerge_index`**：

[resolve-undo.c:124-153](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/resolve-undo.c#L124-L153) `unmerge_index_entry` 先用 `index_name_pos` 定位路径：若已是 stage 0（已合并），`remove_index_entry_at` 移除它；若已是未合并则跳过。然后对 `ru->mode[i]!=0` 的每个 stage，用 `make_cache_entry(..., i+1, 0)` 重建 stage 1/2/3 条目并 `add_index_entry`，可附带 `ce_flags`（如 `CE_MATCHED`）。

[resolve-undo.c:155-179](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/resolve-undo.c#L155-L179) `unmerge_index` 遍历 `istate->resolve_undo` 里所有记录，按 `pathspec` 过滤后调 `unmerge_index_entry`，并 `free(ru)` + 置 `item->util = NULL` 清掉已用的记录。注意开头的 `ensure_full_index(istate)`——稀疏索引下需先展开（见 u4-l3）。

**调用方**：`unmerge_index` 被 `git checkout --merge`（[builtin/checkout.c:638](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/checkout.c#L638)，传 `CE_MATCHED`）与 `rerere`（[rerere.c:1145](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/rerere.c#L1145)）调用；`unmerge_index_entry` 还被 `git update-index --unresolve`（[builtin/update-index.c:646](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/update-index.c#L646)）直接调用。

#### 4.3.4 代码实践

**实践目标**：亲手制造一次冲突、解决它、再把它「还原」回未合并状态，观察 `REUC` 扩展的出现与消失，验证 `record_resolve_undo` / `unmerge_index` 的时序。

**操作步骤**（接前述临时仓库，或新建一个能产生冲突的合并）：

```bash
cd /tmp/uu-test
git checkout -q master
# 制造真实冲突并写出工作树（用 porcelain merge，会经 merge-ort 产出冲突标记）
git merge ours theirs -m "try merge"   # 预期 f.txt 冲突（待本地验证）
git ls-files -u                        # 看到 stage 1/2/3

# 解决冲突：取 ours 版本并 add
git checkout --ours f.txt
git add f.txt
git ls-files -u                        # 应为空（已无 stage>0）
ls-file-REUC() { git ls-files --debug 2>/dev/null | grep -i reuc; }

# 还原冲突（撤销解决）
git checkout -m f.txt        # 或 git update-index --unresolve f.txt
git ls-files -u              # 重新看到 stage 1/2/3
```

> 说明：`git ls-files` 本身不直接打印 `REUC` 扩展名；要确认扩展存在与消失，可用 `git ls-files --debug`（部分信息）或直接 `od -c .git/index` 观察是否含 `REUC` 四字节签名。具体输出待本地验证。

**需要观察的现象**：

1. 合并冲突后 `git ls-files -u` 有三行 stage 1/2/3。
2. `git add` 解决后 `git ls-files -u` 为空，此时索引里应含 `REUC` 扩展（记下了原 stage 1/2/3 的 mode/oid）。
3. `git checkout -m f.txt` 后 `git ls-files -u` **重新出现**三行 stage 1/2/3——这正是 `unmerge_index_entry` 用 REUC 重建出来的。

**对照源码**：第 2 步的「解决」对应 [read-cache.c:587](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L587) 的 `record_resolve_undo` 调用（stage 条目被移除时记录）；第 3 步的「还原」对应 [resolve-undo.c:124-153](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/resolve-undo.c#L124-L153) 的 `unmerge_index_entry`（按记录重建 stage）。若在第 2 步与第 3 步之间又跑一次 `git read-tree -m`，[builtin/read-tree.c:207](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/read-tree.c#L207) 会先 `resolve_undo_clear_index` 把 REUC 清掉，届时 `git checkout -m` 将无法还原——这正好验证清除时机。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `record_resolve_undo` 对 `stage == 0` 的条目直接 return？

参考答案：`resolve_undo_info` 的语义是「记录一个未合并路径的三方 stage 信息」，只有 stage 1/2/3 才是未合并条目。stage 0 表示已合并/正常条目，把它移除只是普通的索引删除（如 `git rm`），不涉及冲突还原，故无需记录。见 [resolve-undo.c:17-20](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/resolve-undo.c#L17-L20)。

**练习 2**：`resolve_undo_info` 用 `mode[i]==0` 表示「该 stage 不存在」。为什么这种表示是自洽的、不会与真实文件模式冲突？

参考答案：合法的 git 文件 `ce_mode` 一定带有类型位（普通文件 `0100644`、可执行 `0100755`、符号链接 `0120000`、gitlink `0160000`），最低位永远非零；而 `0` 不对应任何合法模式，故可安全用作「缺席」哨兵。序列化时 [resolve-undo.c:51-53](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/resolve-undo.c#L51-L53) 也据此跳过缺失 stage 的 oid，节省空间。

**练习 3**：`git read-tree -m` 开始时为什么要调 `resolve_undo_clear_index`？

参考答案：`read-tree -m` 会用新的合并结果整个替换索引，旧的冲突撤销信息对应的 stage 已经不再存在，留着不仅无用还会误导后续 `git checkout --merge` 还原出与新索引不符的状态。故 [builtin/read-tree.c:207](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/read-tree.c#L207) 在合并前清除 REUC 并置 `RESOLVE_UNDO_CHANGED`，让重写时丢弃该扩展。

## 5. 综合实践

把三个最小模块串起来，完成一次「冲突的生命周期」追踪：

**场景**：用 `git read-tree -m` 在索引里造一个 stage 1/2/3 冲突，再用 plumbing `git merge-tree` 看同一冲突的内容合并结果，最后用 `resolve-undo` 机制体会「解决→还原」。

**步骤**：

1. 按 4.1.4 建好 base/ours/theirs 三分支仓库（`f.txt` 在 ours 和 theirs 改成不同内容）。
2. `git read-tree -m master ours theirs`，用 `git ls-files -u` 确认 stage 1/2/3 三条记录。**对应 4.1**：这是 `threeway_merge` 的 `keep_entry` 尾部分支产物。
3. `git merge-tree master ours theirs`，找到 `f.txt` 的合并输出，确认含 `<<<<<<< .our` / `>>>>>>> .their` 标记。**对应 4.2**：这是 `merge_blobs → ll_merge → xdiff` 产出的内容级合并，与步骤 2 的索引状态互不依赖。
4. 用 `git read-tree --reset -u HEAD` 清空索引回到干净状态，然后 `git merge ours theirs -m m` 走 porcelain 产生真实冲突，`git add f.txt` 解决。此时索引应含 `REUC` 扩展（`record_resolve_undo` 在 stage 条目被移除时记录）。
5. `git checkout -m f.txt` 还原冲突，`git ls-files -u` 重新出现 stage 1/2/3。**对应 4.3**：这是 `unmerge_index_entry` 用 REUC 重建的结果。
6. 写一段话：对比步骤 2（`unpack-trees` 文件级冲突）与步骤 3（`merge-blobs` 内容级合并）的输出，说明二者职责的区别；并解释为什么步骤 4→5 能还原而步骤 2 之后直接 `git checkout -m` 不一定能还原（提示：`read-tree` 是否调用了 `resolve_undo_clear_index`？步骤 2 的冲突是「解决」时记录的吗？）。

**预期结果**：你能清晰说出——`unpack-trees` 决定「是不是冲突」、`merge-blobs` 决定「冲突合并后长什么样」、`resolve-undo` 决定「解决后能否还原」，三者分别在合并的不同时机起作用。第 6 问的答案要点：`resolve-undo` 只在冲突**被解决**（stage 被移除）时记录；`read-tree -m` 产生的 stage 1/2/3 从未被「解决」，且 `read-tree` 还会清除旧 REUC，故此时 `git checkout -m` 无可还原信息。

## 6. 本讲小结

- `unpack-trees` 是通用的「多路树 + 索引同步遍历」引擎，合并语义靠换 `merge_fn_t` 回调实现；三方合并用 `threeway_merge`，由 `git read-tree -m` 等命令按树数挑选。
- `threeway_merge` 是**文件级（entry 级）**裁决：用 `same()` 比 (mode, oid)，命中 trivial 情形就 `merged_entry`/`deleted_entry`；只有「双方都改且改得不同」才落到尾部，用 `keep_entry` 把 base/ours/theirs 以 stage 1/2/3 原样保留进索引——这就是冲突的记录方式。它**不做内容合并**。
- stage 编号由 `unpack_single_entry` 按 `i+1` 与 `head_idx` 的大小关系分配（1=base、2=ours、3=theirs），经 `create_ce_entry` 的 `create_ce_flags(stage)` 写进 `ce_flags`。
- 内容级三方合并是另一层：`merge-blobs.c::merge_blobs` 把三个 blob 读成 `mmfile_t` 后调 `ll_merge → xdiff`，产出带 `<<<<<<<` 标记的合并缓冲区；它服务于 plumbing `git merge-tree`，porcelain `git merge` 则走 merge-ort（u10-l1）。
- `resolve-undo` 在冲突**被解决**（stage 条目经 `remove_index_entry_at` 移除）时由 `record_resolve_undo` 记录原 stage 的 mode/oid，序列化为索引 `REUC` 扩展；`unmerge_index` 据此把 stage 1/2/3 重建出来，支撑 `git checkout --merge`、`git update-index --unresolve`、`rerere`。
- `resolve_undo_info` 用 `mode[i]==0` 作「该 stage 缺席」哨兵（合法文件模式最低位必非零）；`git read-tree` 合并前会 `resolve_undo_clear_index` 清掉旧撤销信息。

## 7. 下一步学习建议

- 冲突解决后，`rerere`（reuse recorded resolution）能记住你上次的解决方案、下次自动套用。建议阅读 [rerere.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/rerere.c)，它正是 `unmerge_index` 的调用方之一，与本讲 4.3 紧密相连。
- 想看 porcelain 合并如何把 `unpack-trees` 与内容合并串起来，回看 u10-l1 的 merge-ort：注意 `merge-ort.c` 里 `unpack_opts.head_idx = -1`，说明它用 `unpack-trees` 仅做结果检出（oneway），合并逻辑自己在内存完成——可与本讲的 `threeway_merge` 路径对照。
- 索引扩展的统一机制（`TREE`/`REUC`/`link`/`UNTR` 等 TLV 设计）在 u4-l1 已讲，建议结合本讲的 `REUC` 序列化再读一遍 `read-cache.c` 的扩展区分派，巩固「大写签名=必须识别、小写签名=可安全忽略」的前后向兼容约定。
- 下一个单元（u11）进入传输与协议，将用到本讲的合并概念（fetch/send 的对象协商不涉及合并，但 `git merge-tree` 的输出格式与协议层有渊源），可作为衔接。

# merge-ort 与合并策略

## 1. 本讲目标

本讲讲解 git 合并（merge）的源码实现。读完本讲，你应当能够：

1. 说清楚 `merge-ort.c` 的「递归 + 内存中三方合并」算法是怎么运转的：它如何收集三方树信息、如何处理多合并基（merge base）、如何为每条路径做决策、如何把冲突写回索引。
2. 说清楚 `merge-ll.c` 这个「单文件三方合并」底层驱动：它如何按 `.gitattributes` 选驱动、内置 `text/binary/union` 三种驱动如何工作、用户自定义驱动如何被调用、冲突标记 `<<<<<<<` 是从哪条代码路径冒出来的。
3. 说清楚 `builtin/merge.c` 如何调度多种合并策略：fast-forward 与真实合并的分叉、策略表与标志位、`ort` 为何是默认、以及「多策略择优」循环如何挑选结果。

本讲承接 [u9-l2 checkout/unpack-trees](u9-l2-checkout-unpack-trees.md)（`unpack_trees`、`twoway_merge`、`entry.c`）与 [u8-l1 diff 引擎](u8-l1-diff-core.md)（重命名检测、`diff_filepair`）。合并基的计算逻辑已在 [u7-l2 commit 可达性与 commit-graph](u7-l2-commit-graph-reach.md) 讲过（`paint_down_to_common`），本讲直接引用其结论。

## 2. 前置知识

在进入源码前，先用大白话建立三个直觉。

**什么是三方合并。** 合并不是「把两份代码叠在一起」。git 知道两条分支是从一个共同祖先分叉出去的，于是它手里有三棵树：

- **base**（合并基，common ancestor）：两条分支分叉前的状态。
- **ours**（本侧）：当前所在分支的状态。
- **theirs**（对侧）：被合并进来的分支的状态。

对每一个路径，git 比较 base / ours / theirs 三者。关键判定是：**只有一方相对 base 改了，就采用那一方（这叫平凡合并 trivial merge）；两方都改了且改得不一样，才需要真正做行级合并或报冲突。** 这个「谁改了、改没改」的信息，在源码里被压缩成两个 3 位掩码 `filemask`/`match_mask`。

**什么是「递归」合并。** 两条分支的合并基可能不止一个（菱形分叉、即 criss-cross 历史）。ort 的做法是：先把多个合并基两两合并成一个**虚拟提交**（virtual commit），用这个虚拟提交当作「合并基」，再去做真正的那次合并。这种「合并合并基」是递归进行的，故叫递归策略（recursive）。注意：合并合并基时本身也可能冲突，所以这些中间结果以「带冲突标记的树」形式存在内存里，而不真正落盘。

**什么是「in-core」合并。** ort 的设计要点是：**整个合并计算都在内存里完成，期间不碰工作树和索引**。算完得到一棵结果树（可能含冲突文件）；只有到最后一步 `merge_switch_to_result` 才把结果 checkout 到工作树、把冲突条目写进索引。这样做的好处是可重入、可被 rebase/cherry-pick 复用、且便于在内存里嵌套（合并合并基时）。

一个贯穿本讲的术语表：

| 术语 | 含义 |
|------|------|
| merge base（合并基） | 两条分支的共同最近祖先 |
| 三方合并（3-way） | base / ours / theirs 三棵树参与 |
| 虚拟提交（virtual commit） | 合并合并基时构造的、不真正落盘的中间提交 |
| stage（阶段） | 索引中冲突条目的来源：1=base，2=ours，3=theirs（见 [u4-l1](u4-l1-index-state.md)） |
| in-core | 纯内存、不碰工作树/索引 |
| 冲突标记 | `<<<<<<<`、`=======`、`>>>>>>>` 三段 |

## 3. 本讲源码地图

| 文件 | 体量 | 作用 |
|------|------|------|
| `merge-ort.c` | ~5600 行 | ort 策略的全部实现：递归合并基、三方树收集、重命名、逐路径决策、冲突记录 |
| `merge-ort.h` | 头文件 | 公开 API：`struct merge_options`、`struct merge_result`、`merge_incore_*` |
| `merge-ort-wrappers.c` | 小 | 给 porcelain 命令用的薄包装：加 `unclean()` 安全检查 + 把结果落到工作树/索引 |
| `merge-ll.c` | ~470 行 | 单文件三方合并的低层驱动引擎 |
| `merge-ll.h` | 头文件 | `struct ll_merge_options`、`enum ll_merge_result`、`ll_merge()` |
| `builtin/merge.c` | ~1900 行 | `git merge` 命令：策略调度、fast-forward 分叉、多策略择优 |
| `merge.c` | 小 | `try_merge_command`（子进程派发非 ort 策略）、`checkout_fast_forward` |
| `commit-reach.c` | 见 u7-l2 | `repo_get_merge_bases` / `paint_down_to_common`，本讲直接引用 |

> **重要事实**：在本版本的 git 源码里，**已不存在 `merge-recursive.c`**。文件顶部的注释把 ort 定义为「Ostensibly Recursive's Twin」（名义上是 recursive 的孪生兄弟）——它是旧 recursive 策略的替代品（[merge-ort.c:1-14](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ort.c#L1-L14)）。更关键的是，`builtin/merge.c` 在派发时把名字为 `recursive`、`subtree`、`ort` 三者**全部路由到同一份 ort 实现**（见 [4.3 节](#43-合并策略调度)），所以 `git merge -s recursive` 今天其实跑的就是 ort。

## 4. 核心概念与源码讲解

### 4.1 ort 递归合并核心

#### 4.1.1 概念说明

ort 解决的问题是：给定两条提交（side1、side2）以及它们的合并基，计算出一棵「合并后的结果树」。

它的工程结构是三层 API（从外到内）：

1. **porcelain 包装层**（`merge-ort-wrappers.c`）：`merge_ort_recursive` / `merge_ort_nonrecursive` / `merge_ort_generic`。负责合并前的安全检查（索引必须与 HEAD 一致，否则报「本地改动会被覆盖」）、合并后把结果落到工作树与索引、持索引锁原子写入。`builtin/merge.c` 调用的就是这一层。
2. **公开 in-core 层**（`merge-ort.h`）：`merge_incore_recursive` / `merge_incore_nonrecursive`。只算结果，不碰工作树/索引。这是 rebase、cherry-pick 等需要「算完先看看、不立刻落盘」的场景的入口。
3. **内部算法层**（`merge-ort.c` static）：`merge_ort_internal`（处理递归合并基）和 `merge_ort_nonrecursive_internal`（单合并基的三方合并主体）。

「递归」二字体现在第 3 层：当合并基不止一个时，`merge_ort_internal` 会**递归地调用自己**，把多个合并基两两合并成虚拟提交，最终塌缩成单一合并基再交给 `merge_ort_nonrecursive_internal`。

#### 4.1.2 核心流程

整个非递归三方合并主体 `merge_ort_nonrecursive_internal` 是三段流水线，外加一个重命名后重做（redo）的回环：

```
merge_incore_recursive(opt, bases, side1, side2, result)
  └─ merge_ort_internal           # 递归塌缩多个合并基为单一虚拟基
  │    └─ (若 bases>1) 递归 merge_ort_internal(prev, next) → 虚拟提交
  │    └─ merge_ort_nonrecursive_internal(merge_base, side1, side2)
  │
  └─ merge_ort_nonrecursive_internal   # 主体三段：
       1. collect_merge_info           # 三方 traverse_trees，填 opt->priv->paths
       2. detect_and_process_renames   # 复用 diff 引擎做重命名/目录重命名
            └─ 若 redo_after_renames==2 → clear → goto redo
       3. process_entries              # 逐路径决策，写出结果树
            └─ process_entry           # 单路径：filemask/match_mask 决策
                 └─ handle_content_merge → merge_3way → ll_merge  # 真正行级合并
  └─ (调用方) merge_switch_to_result   # checkout 结果 + 记录冲突到索引 + 写 AUTO_MERGE
```

单路径决策的「数学」可以压成一句话：用 3 位 `match_mask` 表示 base/ours/theirs 三者中**哪几个彼此相同**（bit0=base，bit1=ours，bit2=theirs）。

- 若三方全同（`match_mask==7`）：在收集阶段已直接判定为干净，根本不进 `process_entry`。
- 若两方相同、第三方不同：取「改过的那一方」。例如 `match_mask==6`（ours==theirs，base 不同）表示两边做了相同改动，直接取该值；`match_mask==3`（base==ours，theirs 不同）则取 theirs。
- 若没有任何两方相同：两方都相对 base 改了，进入真正的内容合并或冲突。

用集合符号表达平凡性判定——只有当三者中出现「至少一对相等」时才可能平凡合并：

\[
\text{可能平凡} \iff \exists\, i\neq j,\; s_i = s_j \quad (s_0=\text{base},\, s_1=\text{ours},\, s_2=\text{theirs})
\]

只有当三者两两皆不同时，才需要调用 `merge_3way` 做行级合并。

#### 4.1.3 源码精读

**结果结构 `struct merge_result`** —— 合并的对外产物。注意 `clean` 字段的三态语义：`1` 干净、`0` 有冲突、`<0` 中途失败（[merge-ort.h:12-46](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ort.h#L12-L46)）。`tree` 是结果树；即使不干净，它也代表「将要写进工作树的版本」（可能含冲突标记）。`priv` 是仅供内部用的私有数据（带冲突详情、待写索引信息）。

**配置结构 `struct merge_options`** —— 装载所有合并选项（[merge-ort.h:49-92](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ort.h#L49-L92)）。关键字段：`branch1`/`branch2` 是冲突标记里的标签名；`detect_renames`/`rename_score`/`rename_limit` 控制重命名检测；`xdl_opts` 透传给 xdiff；`conflict_style` 决定冲突标记风格；`recursive_variant`（`NORMAL`/`OURS`/`THEIRS`）对应 `-Xours`/`-Xtheirs`。`priv` 指向 `struct merge_options_internal`，是算法运行期的全部状态。

**路径的数据表示** 是理解算法的钥匙。干净路径用 `struct merged_info`，冲突路径用 `struct conflict_info`，后者把前者放在首个成员位置实现 C 风格继承（[merge-ort.c:436-528](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ort.c#L436-L528)）：

```c
struct version_info { struct object_id oid; unsigned short mode; };

struct merged_info {                 /* 干净路径 */
    struct version_info result;
    unsigned is_null:1;
    unsigned clean:1;
    size_t basename_offset;
    const char *directory_name;
};

struct conflict_info {               /* 冲突路径：merged_info 在首成员 */
    struct merged_info merged;       /* WARNING: merged.clean==1 后禁读其余字段 */
    struct version_info stages[3];   /* base/ours/theirs 三方的 oid+mode */
    const char *pathnames[3];        /* 三方各自的路径（重命名后可能不同） */
    unsigned df_conflict:1;          /* 目录/文件冲突 */
    unsigned path_conflict:1;        /* 非内容冲突（rename/rename 等） */
    unsigned filemask:3;             /* bit i：第 i 方在此路径是文件 */
    unsigned dirmask:3;              /* bit i：第 i 方在此路径是目录 */
    unsigned match_mask:3;           /* bit i：哪几方彼此相同 */
};
```

`merged.clean` 是一个**安全闸门**：分配的 `merged_info` 总是 `clean=1`，分配的 `conflict_info` 初始 `clean=0`；代码约定「只有 `clean==0` 才允许读 `stages` 等字段」（见结构体上方注释 [merge-ort.c:472-485](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ort.c#L472-L485)）。

**冲突类型枚举** —— 决定了你看到的那行 `CONFLICT (contents)` 文案（[merge-ort.c:530-615](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ort.c#L530-L615)）。它把所有冲突/信息分类：`CONFLICT_CONTENTS`、`CONFLICT_MODIFY_DELETE`、`CONFLICT_RENAME_RENAME`、`CONFLICT_FILE_DIRECTORY`……并有配套的 `type_short_descriptions[]` 字符串表，外部工具依赖这些字符串不变。

**合并主体三段流水线**（[merge-ort.c:5245-5308](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ort.c#L5245-L5308)），核心骨架：

```c
redo:
    collect_merge_info(opt, merge_base, side1, side2);   /* 阶段 1：收集 */
    result->clean = detect_and_process_renames(opt);     /* 阶段 2：重命名 */
    if (opt->priv->renames.redo_after_renames == 2) {
        clear_or_reinit_internal_opts(opt->priv, 1);
        goto redo;                                        /* 重命名改变了图，重做 */
    }
    process_entries(opt, &working_tree_oid);             /* 阶段 3：决策+建树 */

    if (result->clean >= 0) {
        result->tree = repo_parse_tree_indirect(..., &working_tree_oid);
        result->clean &= strmap_empty(&opt->priv->conflicted);  /* 有冲突即不干净 */
    }
```

三个 `trace2_region_enter/leave` 把三段切成可测量的性能区间（见 [u13-l3 trace2](u13-l3-trace2-profiling.md)）。`redo` 回环是 ort 的一个特色：重命名检测可能反过来影响目录内容（目录重命名），于是清空内部 map 重跑一次收集。

**递归合并基塌缩**（[merge-ort.c:5313-5401](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ort.c#L5313-L5401)）。若调用方未给合并基，先用 `repo_get_merge_bases` 算（来自 commit-reach.c，见 u7-l2），并**反转成「最旧在前」**（注释引用了经验结论：按最旧到最新顺序递归合并表现更好）。然后弹出一个 `merged_merge_bases`，若还有剩余，就在循环里**递归调用自身**把 `prev` 与 `next` 合并成虚拟提交：

```c
for (next = pop_commit(&merge_bases); next; next = pop_commit(&merge_bases)) {
    opt->priv->call_depth++;
    opt->branch1 = "Temporary merge branch 1";
    opt->branch2 = "Temporary merge branch 2";
    merge_ort_internal(opt, NULL, prev, next, result);   /* 递归 */
    opt->priv->call_depth--;
    merged_merge_bases = make_virtual_commit(opt->repo, result->tree, "merged tree");
    /* 给虚拟提交挂两个 parent，使其看上去像个真合并 */
    commit_list_insert(prev,    &merged_merge_bases->parents);
    commit_list_insert(next, &merged_merge_bases->parents->next);
    clear_or_reinit_internal_opts(opt->priv, 1);
}
```

`call_depth>0` 这一标志会在多处改变行为（例如 `merge_3way` 里设 `virtual_ancestor=1`、`variant=0`；符号链接冲突时回退到 base）——因为合并合并基时不需要真冲突，只要「尽量合并出一个可用的基」。没有共同祖先时则用空树兜底（[merge-ort.c:5336-5343](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ort.c#L5336-L5343)）。

**逐路径决策 `process_entry`**（[merge-ort.c:4079-4198](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ort.c#L4079-L4198)）开头先处理目录/文件冲突（`dirmask`、`df_conflict`），随后进入一段长长的 if-elseif 决策链。其中最核心的「平凡合并」分支（[merge-ort.c:4205-4225](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ort.c#L4205-L4225)）：

```c
if (ci->match_mask) {
    ci->merged.clean = !ci->df_conflict && !ci->path_conflict;
    if (ci->match_mask == 6) {                 /* ours==theirs */
        ci->merged.result.mode = ci->stages[1].mode;
        oidcpy(&ci->merged.result.oid, &ci->stages[1].oid);
    } else {                                   /* 取「改过的那一方」 */
        unsigned int othermask = 7 & ~ci->match_mask;
        int side = (othermask == 4) ? 2 : 1;
        ci->merged.result.mode = ci->stages[side].mode;
        ci->merged.is_null = !ci->merged.result.mode;   /* mode==0 即删除 */
        if (ci->merged.is_null) ci->merged.clean = 1;
        oidcpy(&ci->merged.result.oid, &ci->stages[side].oid);
    }
}
```

`7 & ~match_mask` 是个巧妙的小技巧：`match_mask` 标出「相同的两方」，取反后剩下的那一位就是「改过的第三方」。当两方类型不同（一个文件一个符号链接）时走「改名分流」分支，把各自改名到独立路径（[merge-ort.c:4226-4278](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ort.c#L4226-L4278)）。

**内容合并 `handle_content_merge`**（[merge-ort.c:2179-2325](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ort.c#L2179-L2325)）。当两方都改了内容、且都是普通文件时，先做平凡的 mode/oid 合并（`oideq` 判等取值），剩下的才调 `merge_3way`：

```c
/* 平凡 oid 合并：任一方等于 base 即取另一方 */
if (oideq(&a->oid, &b->oid) || oideq(&a->oid, &o->oid))
    oidcpy(&result->oid, &b->oid);
else if (oideq(&b->oid, &o->oid))
    oidcpy(&result->oid, &a->oid);
else if (S_ISREG(a->mode)) {
    merge_status = merge_3way(opt, path, &o->oid, &a->oid, &b->oid,
                              pathnames, extra_marker_size, &result_buf);
    ...
    if (merge_status > 0) clean = 0;          /* >0 表示有冲突 */
}
```

注意 `extra_marker_size`：当发生「内容合并的内容合并」（如 rename/rename(2to1)）时，嵌套的冲突标记需要加长，否则无法区分层级。

**桥到低层驱动 `merge_3way`**（[merge-ort.c:2107-2177](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ort.c#L2107-L2177)）填好 `struct ll_merge_options`，把 oid 读成内存缓冲 `mmfile_t`，再调 `ll_merge`（下一节详述）。冲突标记的标签来自 `opt->ancestor`/`branch1`/`branch2`，路径不一致时拼成 `branch:path`。

**主循环 `process_entries`**（[merge-ort.c:4498-4600](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ort.c#L4498-L4600)）。它把 `opt->priv->paths` 倒进 `plist`，用特殊比较器 `sort_dirs_next_to_their_children` 排序，然后**逆序遍历**——先处理目录下的文件、再处理目录本身，这样写出子树时子内容已就绪，也能正确判断目录/文件冲突时「目录是否还挡在路中」。干净条目直接 `record_entry_for_tree`，冲突条目交给 `process_entry`。最后由 `write_tree` 把累积的 `directory_versions` 转成一棵结果树。

**把结果落到工作树与索引 `merge_switch_to_result`**（[merge-ort.c:4927-4977](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ort.c#L4927-L4977)）做了三件事：

```c
checkout(opt, head, result->tree);                 /* 把工作树/索引切到结果树 */
record_conflicted_index_entries(opt);              /* 冲突路径写回 stage 1/2/3 */
refs_update_ref(..., "AUTO_MERGE", &result->tree->object.oid, ...);  /* 记 AUTO_MERGE */
```

**冲突写索引 `record_conflicted_index_entries`**（[merge-ort.c:4648-4702](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ort.c#L4648-L4702)）：`checkout` 已经为每个冲突路径写了一条「尽可能合并好的版本」（stage=0）；本函数再把它替换成 base/ours/theirs 各自的 stage 1/2/3 条目（这就是 `git ls-files -u` 能看到三阶段的原因）。配套的 `merge_get_conflicted_files`（[merge-ort.c:4896-4925](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ort.c#L4896-L4925)）则把同样的阶段信息导出给外部。

> 注：`AUTO_MERGE` 是较新的机制——即使合并有冲突，git 也把「自动合并出的那一版结果树」记成一个引用，供 `git mergetool`、`git diff AUTO_MERGE` 等使用。

#### 4.1.4 代码实践

**实践目标：构造一次真实的文本冲突，验证冲突区段是如何被标记并记录进索引的。**

操作步骤（在任意临时仓库执行）：

```bash
mkdir mg && cd mg && git init -q
printf 'line1\nline2\n' > greeting.txt
git add greeting.txt && git -c user.email=a@b.c -c user.name=A commit -qm base

git checkout -q -b feature
printf 'line1\nFEATURE\n' > greeting.txt
git add greeting.txt && git -c user.email=a@b.c -c user.name=A commit -qm feature

git checkout -q master
printf 'MAIN\nline2\n' > greeting.txt
git add greeting.txt && git -c user.email=a@b.c -c user.name=A commit -qm main

git merge feature        # 触发冲突
```

需要观察的现象：

1. 控制台应打印一行 `Auto-merging greeting.txt`（对应 `merge-ort.c` 里 `INFO_AUTO_MERGING` 的 `path_msg`）与 `CONFLICT (contents): Merge conflict in greeting.txt`（对应 `CONFLICT_CONTENTS`）。
2. 文件 `greeting.txt` 内出现三段标记：
   ```
   <<<<<<< HEAD
   MAIN
   line2
   =======
   line1
   FEATURE
   >>>>>>> feature
   ```
3. `git ls-files -u greeting.txt` 会列出**三个阶段**的条目：stage 1 = base，stage 2 = ours（HEAD），stage 3 = theirs（feature）。

阅读源码对照：`process_entry` 的平凡分支因为「base/ours/theirs 两两皆不同」不命中，落到 `handle_content_merge` → `merge_3way` → `ll_merge`，`ll_merge` 返回 `LL_MERGE_CONFLICT`（`>0`），于是 `clean=0`；随后 `record_conflicted_index_entries` 把三方各写成一个 stage 条目。

4. 用 `git rev-parse AUTO_MERGE` 查看 git 是否记下了自动合并结果树（待本地验证：较旧版本的 git 可能没有此引用）。

若运行后未出现上述现象（例如自动合并成功），说明两方改动恰好可自动合并——调整文本使两方改了**同一行不同内容**即可复现冲突。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `struct conflict_info` 把 `struct merged_info` 放在第一个成员？这样设计后，`process_entries` 里 `struct merged_info *mi = entry->util;` 与 `struct conflict_info *ci = (struct conflict_info *)mi;` 之间的强转为何是安全的？

**参考答案**：这是 C 语言里模拟继承的惯用法——基类结构体放在首成员位置，基类指针与派生类指针的二进制地址相同，因此可以在二者间安全强转。安全的前提是先检查 `mi->clean`：只有 `clean==0` 时 `mi` 实际指向一个 `conflict_info`，此时访问其 `stages`/`filemask` 等字段才合法（这正是结构体注释里强调的 WARNING）。

**练习 2**：`process_entries` 为什么要逆序遍历排好序的路径列表？

**参考答案**：排序规则是「把目录紧挨其子项」。逆序意味着「先处理子项、再处理目录」。这样在处理一个目录时，它下面的文件已经处理完毕、子树内容已就绪，可以直接写出该目录对应的 tree；同时也让目录/文件冲突（D/F conflict）能正确判断「此刻目录是否还挡在文件路径上」。

---

### 4.2 merge-ll 低层合并驱动

#### 4.2.1 概念说明

`merge-ll.c` 是**单文件**层面的三方合并引擎。ort 在「两方都改了同一个文件」时，最终都汇流到这里。它解决两件事：

1. **选驱动**：按 `.gitattributes` 里的 `merge` 属性，决定这个文件该用哪种合并方式。内置三种：`text`（默认，调 xdiff 做行级三方合并）、`binary`（二进制不能合并，按规则取一方）、`union`（把两方简单拼接）。还支持用户自定义驱动（`[merge "<name>"] driver = ...`）。
2. **生成冲突标记**：当 `text` 驱动发现两方在同一区域都改了且无法自动合并时，由底层 xdiff 的 `xdl_merge` 产出 `<<<<<<<` / `=======` / `>>>>>>>` 三段，返回 `LL_MERGE_CONFLICT`。

它被设计成与 ort **解耦**的薄层：`ll_merge()` 只关心「三个内存缓冲 + 路径 + 选项」，不关心你是在做分支合并、cherry-pick 还是 rebase。这也正是 `git merge-file`、`git apply --3way` 等命令都能复用它的原因。

#### 4.2.2 核心流程

```
ll_merge(result, path, ancestor, ours, theirs, istate, opts)
  ├─ git_check_attr(path)                # 查 merge 属性 + conflict-marker-size
  ├─ find_ll_merge_driver(attr_value)    # 选驱动：text/binary/union/自定义
  │     └─ 若 opts->virtual_ancestor 且 driver->recursive 非空 → 换成 recursive 驱动
  ├─ marker_size += opts->extra_marker_size
  └─ driver->fn(...)                     # 分派到具体驱动：
        ├─ ll_xdl_merge → xdl_merge       # text：xdiff 行级三方合并，产出冲突标记
        ├─ ll_binary_merge                # binary：取 ours/theirs/ancestor
        └─ ll_union_merge                 # union：variant=FAVOR_UNION 后走 xdl_merge
```

`find_ll_merge_driver` 的选择规则值得记（[merge-ll.c:363-394](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ll.c#L363-L394)）：`merge` 属性为 `true` → `text`；`false` → `binary`；未设且无 `merge.default` → `text`；设成具体名字则按名字在「用户驱动链 → 内置三驱动」里查，查不到也兜底 `text`。

#### 4.2.3 源码精读

**入口 `ll_merge`**（[merge-ll.c:406-451](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ll.c#L406-L451)）：

```c
git_check_attr(istate, path, check);          /* 查 merge 属性与 marker-size */
ll_driver_name = check->items[0].value;
...                                            /* 解析 conflict-marker-size */
driver = find_ll_merge_driver(ll_driver_name);

if (opts->virtual_ancestor) {                 /* 合并合并基时：换 recursive 驱动 */
    if (driver->recursive)
        driver = find_ll_merge_driver(driver->recursive);
}
if (opts->extra_marker_size)
    marker_size += opts->extra_marker_size;
return driver->fn(driver, result_buf, path, ancestor, ancestor_label,
                  ours, our_label, theirs, their_label, opts, marker_size);
```

注意 `virtual_ancestor` 分支：这正是 4.1 节里「合并合并基时 `call_depth>0`」在底层驱动的对应物——某些文件在「合成祖先」时可能需要换一种合并方式（如二进制文件），由 `[merge "<driver>"] recursive` 配置指定。

**text 驱动 `ll_xdl_merge`**（[merge-ll.c:103-147](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ll.c#L103-L147)）。它先做两道安全检查：任一缓冲过大（`MAX_XDIFF_SIZE`）或含 NUL 字节（`buffer_is_binary`）就降级为 `ll_binary_merge`；否则填好 `xmparam_t`（合并级别 `XDL_MERGE_ZEALOUS`、`favor` 来自 `-Xours/-Xtheirs`、`style` 来自 conflict_style、`marker_size`），调用 xdiff 的 `xdl_merge`：

```c
status = xdl_merge(orig, src1, src2, &xmp, result);
ret = (status > 0) ? LL_MERGE_CONFLICT : status;   /* >0 = 有冲突 */
```

`xdl_merge` 在 [u8-l2 xdiff](u8-l2-xdiff-library.md) 讲过的 Myers/直方图算法基础上做三方合并，当两方改动重叠无法自动调和时，把两段连同标记写进 `result`。这就是 `<<<<<<<` 的真正出处。

**binary 驱动 `ll_binary_merge`**（[merge-ll.c:58-101](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ll.c#L58-L101)）。二进制无法做行级合并，规则是：合成祖先时取 base；否则默认报 `LL_MERGE_BINARY_CONFLICT` 并取 ours（`-Xours`/`-Xtheirs` 可改取 theirs）。

**union 驱动 `ll_union_merge`**（[merge-ll.c:149-166](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ll.c#L149-L166)）：把 `variant` 改成 `XDL_MERGE_FAVOR_UNION` 后复用 `ll_xdl_merge`，结果是两方内容都保留、用 `=======` 分隔但不报冲突——常用于 `CHANGELOG` 这类「两边都追加」的文件。

**驱动注册表**（[merge-ll.c:168-175](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ll.c#L168-L175)）：

```c
static struct ll_merge_driver ll_merge_drv[] = {
    { "binary", "built-in binary merge",      ll_binary_merge },
    { "text",   "built-in 3-way text merge",  ll_xdl_merge },
    { "union",  "built-in union merge",       ll_union_merge },
};
```

**用户自定义驱动 `ll_ext_merge`**（[merge-ll.c:191-269](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ll.c#L191-L269)）。它把三方内容各写进一个临时文件，然后用 `strbuf_expand_step` 把命令行模板里的占位符替换成路径：

| 占位符 | 含义 |
|--------|------|
| `%O` | base 的临时文件 |
| `%A` | ours 的临时文件（驱动要把结果写回这里） |
| `%B` | theirs 的临时文件 |
| `%L` | 冲突标记长度 |
| `%P` | 原始路径（已转义） |
| `%S` / `%X` / `%Y` | base / ours / theirs 的版本标识 |

退出码语义：0=干净、`<=128`=冲突、`>128`（被信号杀死）=错误（[merge-ll.c:261-268](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ll.c#L261-L268)）。

**配置解析 `read_merge_config`**（[merge-ll.c:277-353](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ll.c#L277-L353)）：监听 `merge.<name>.driver` / `.name` / `.recursive` 与 `merge.default`，把用户定义的驱动挂进 `ll_user_merge` 链表。这印证了 [u6-l1 config](u6-l1-config-parsing.md) 讲的「回调驱动解析」模型。

#### 4.2.4 代码实践

**实践目标：体验自定义合并驱动，并观察它如何接管某个文件类型的合并。**

操作步骤：

```bash
mkdir mll && cd mll && git init -q
# 注册一个自定义驱动 "fillet"：遇到冲突就保留两段
git config merge.fillet.name "two-way keep both"
git config merge.fillet.driver "cp -f %B %A && printf '\n===\n' >> %A && cat %O >> %A"
# 让所有 .log 文件使用该驱动
printf '*.log merge=fillet\n' > .gitattributes
git add .gitattributes
git -c user.email=a@b.c -c user.name=A commit -qm attrs

printf 'v1\n' > app.log; git add app.log
git -c user.email=a@b.c -c user.name=A commit -qm base

git checkout -q -b b1; printf 'v1\nFROM_B1\n' > app.log
git add app.log && git -c user.email=a@b.c -c user.name=A commit -qm b1

git checkout -q master; printf 'v1\nFROM_MAIN\n' > app.log
git add app.log && git -c user.email=a@b.c -c user.name=A commit -qm main

git merge b1
cat app.log       # 观察结果是否被自定义驱动接管（而非出现 <<<<<<< 标记）
```

需要观察的现象：

- 因为 `app.log` 命中了 `merge=fillet`，`ll_merge` 经 `find_ll_merge_driver("fillet")` 选中自定义驱动 `ll_ext_merge`，它执行了你配置的命令，把结果写回 `%A`。文件内容应是「theirs + `===` + base」的拼接，而**不是** `<<<<<<<` 标记。
- 若改用内置的 union 驱动（直接 `printf '*.log merge=union\n'`），则会看到两方内容以 `=======` 分隔。

对照源码：`ll_ext_merge` 里 `run_command(&child)` 执行命令、退出码 `<=128` 被判为 `LL_MERGE_CONFLICT`（[merge-ll.c:241-268](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ll.c#L241-L268)）。如果驱动退出码非 0，ort 仍会把这个路径记为冲突（写回 stage）。

> 此实践依赖自定义 shell 命令正确执行；若环境无 `cp`/`cat`/`printf`，可仅做源码阅读：在 `ll_ext_merge` 的 `strbuf_expand_step` 循环处确认 `format` 串里 `%A`/`%B`/`%O` 被替换为实际临时文件路径（待本地验证具体命令输出）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ll_xdl_merge` 在调用 `xdl_merge` 之前要先检查 `buffer_is_binary`？

**参考答案**：xdiff 是**行级**文本差异算法，依赖「按行切分」。二进制文件含 NUL 字节、没有稳定的「行」概念，强行做行级合并会产生乱码甚至截断。因此检测到二进制内容时直接降级到 `ll_binary_merge`，按 ours/theirs/ancestor 规则取一方并报 `LL_MERGE_BINARY_CONFLICT`，避免产出无意义结果。`MAX_XDIFF_SIZE` 则防止把超大文件喂给差异算法拖垮性能。

**练习 2**：`opts->virtual_ancestor` 为真时，`ll_merge` 会做什么额外动作？它和 ort 的 `call_depth` 有什么关系？

**参考答案**：会检查当前驱动是否配了 `recursive` 字段（`[merge "<driver>"] recursive = <other>`），若配了就换成那个「合成祖先专用驱动」。这是因为 ort 在递归合并合并基时（`call_depth>0`，见 4.1 节 `merge_3way` 里设 `virtual_ancestor=1`），某些文件——尤其二进制——可能需要换一种更宽松或不同的合并方式来得到一个可用的虚拟基，而不是像最终合并那样严格报冲突。

---

### 4.3 合并策略调度

#### 4.3.1 概念说明

`builtin/merge.c` 是 `git merge` 命令的总指挥。它要在合并前回答几个问题：

- 这次合并能不能**快进（fast-forward）**？即当前 HEAD 是否是对方的祖先——若是，只需把指针往前挪、更新工作树，根本不需要做三方合并。
- 用哪个**策略（strategy）**？默认是 `ort`；多分支合并可能用 `octopus`；用户可用 `-s` 指定。
- 如果配了多个策略（`-s ours -s ort`），按什么规则挑出最终结果？

理解调度的关键是一张 `all_strategy[]` 策略表和它上面的标志位。标志位把「能否快进」「是否参与默认选择」「是否需要 trivial 检查」等横切属性集中描述，由 `cmd_merge` 统一读取。

#### 4.3.2 核心流程

```
cmd_merge
  ├─ 解析参数、加载配置、确定策略集 use_strategies[]
  ├─ 若当前 HEAD 是对方祖先 → checkout_fast_forward()（unpack_trees 的 twoway_merge）
  ├─ 若对方是 HEAD 祖先 → "Already up to date."，结束
  └─ 否则进入真实合并：
       for (i in use_strategies):
           save_state / restore_state        # 多策略时保存/恢复工作树
           ret = try_merge_strategy(name)
             ├─ name ∈ {ort, recursive, subtree} → 进程内 merge_ort_recursive()
             └─ 其他(octopus/resolve/ours)   → try_merge_command() 子进程 git merge-<name>
           若 ret==0（干净）→ 记录 best_strategy 并 break
           否则按 evaluate_result()（未合并条目数 + 差异文件数）取「最干净」者为 best
       最终：用 best_strategy 的结果落到工作树（或回滚重跑一次 best）
```

**进程内 vs 子进程**是个重要分野：`ort`（以及别名 `recursive`/`subtree`）直接在当前进程里调 `merge_ort_recursive`，无 fork 开销、能共享内存中的对象缓存；`octopus`/`resolve`/`ours` 则通过 `try_merge_command` 派生 `git merge-<name>` 子进程（[merge.c:22-50](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge.c#L22-L50)），跑完再重读索引。

#### 4.3.3 源码精读

**策略表与标志位**（[builtin/merge.c:55-63](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/merge.c#L55-L63)、[builtin/merge.c:101-108](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/merge.c#L101-L108)）：

```c
#define DEFAULT_TWOHEAD  (1<<0)   /* 双头合并的默认策略 */
#define DEFAULT_OCTOPUS  (1<<1)   /* 多头合并的默认策略 */
#define NO_FAST_FORWARD  (1<<2)   /* 不允许快进 */
#define NO_TRIVIAL       (1<<3)   /* 跳过 trivial（快进式）优化 */

struct strategy { const char *name; unsigned attr; };

static struct strategy all_strategy[] = {
    { "recursive",  NO_TRIVIAL },
    { "octopus",    DEFAULT_OCTOPUS },
    { "ort",        DEFAULT_TWOHEAD | NO_TRIVIAL },   /* 双头默认 */
    { "resolve",    0 },
    { "ours",       NO_FAST_FORWARD | NO_TRIVIAL },
    { "subtree",    NO_FAST_FORWARD | NO_TRIVIAL },
};
```

`ort` 同时带 `DEFAULT_TWOHEAD`（所以双头合并默认就是它）和 `NO_TRIVIAL`（即使能快进也不走快进优化，因为 ort 要做完整三方合并）。

**策略名查找 `get_strategy`**（[builtin/merge.c:170-189](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/merge.c#L170-L189)）：在 `all_strategy[]` 里按名字线性查找；找不到时还会用 `load_command_list("git-merge-", ...)` 去 `PATH` 里找外部 `git-merge-*` 程序，使策略可扩展。

**派发核心 `try_merge_strategy`**（[builtin/merge.c:789-851](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/merge.c#L789-L851)）。最关键的一段证明了「recursive 其实是 ort」：

```c
if (!strcmp(strategy, "recursive") || !strcmp(strategy, "subtree") ||
    !strcmp(strategy, "ort")) {
    ...
    init_ui_merge_options(&o, the_repository);
    if (!strcmp(strategy, "subtree")) o.subtree_shift = "";
    for (x = 0; x < xopts.nr; x++) parse_merge_opt(&o, xopts.v[x]);  /* -X 选项 */
    o.branch1 = head_arg;
    o.branch2 = merge_remote_util(remoteheads->item)->name;
    ...
    clean = merge_ort_recursive(&o, head, remoteheads->item, reversed, &result);
    ...
    return clean ? 0 : 1;
} else {
    return try_merge_command(..., strategy, ...);   /* 子进程 git merge-<name> */
}
```

`-X` 选项（如 `-Xours`/`-Xtheirs`/`-Xpatience`/`-Xignore-space-change`）由 `parse_merge_opt` 解析进 `merge_options`（其中 `recursive_variant` 决定 ours/theirs，`xdl_opts` 决定 diff 算法）。`subtree` 策略只是给 ort 设了 `subtree_shift`，本质也是 ort。

**多策略择优循环**（[builtin/merge.c:1781-1820](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/merge.c#L1781-L1820)）。当用户给了多个策略（`-s a -s b`）时，逐个尝试：每个策略跑之前若不是第一个就 `restore_state` 把工作树复位，跑完若干净（`ret==0`）立刻记录 `best_strategy` 并 `break`；否则用 `evaluate_result()` 量化「这个策略留下了多少烂摊子」，取最小者：

```c
for (i = 0; i < use_strategies_nr; i++) {
    ...
    ret = try_merge_strategy(wt_strategy, common, remoteheads, head_commit);
    /* 后端：1=有冲突待解，2=根本不处理这种合并 */
    if (ret < 2) {
        if (!ret) { merge_was_ok = 1; best_strategy = wt_strategy; break; }
        cnt = (use_strategies_nr > 1) ? evaluate_result() : 0;
        if (best_cnt <= 0 || cnt <= best_cnt) { best_strategy = wt_strategy; best_cnt = cnt; }
    }
}
```

**择优量化 `evaluate_result`**（[builtin/merge.c:1070-1092](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/merge.c#L1070-L1092)）：它跑一次「索引 vs 工作树」的 diff（`run_diff_files` 配 `count_diff_files` 回调）数差异文件，再加上 `count_unmerged_entries()` 数出的未合并（stage>0）条目，和越小代表该策略「自动解决得越干净」。循环结束后若没有策略产出干净结果，就选 `best_strategy` 重跑一次，把结果留给用户手工解决（[builtin/merge.c:1838-1859](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/merge.c#L1838-L1859)）。

**快进路径** `checkout_fast_forward`（[merge.c:52-113](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge.c#L52-L113)）：当可以快进时，它不碰策略，直接用 `unpack_trees` 的 `twoway_merge`（见 [u9-l2](u9-l2-checkout-unpack-trees.md)）把工作树从 HEAD 树切到目标树——这也是为什么 fast-forward 合并在历史里是线性的、不产生合并提交。

#### 4.3.4 代码实践

**实践目标：观察策略选择、`-X` 选项的效果，以及 recursive 与 ort 的等价性。**

操作步骤（沿用 4.1.4 的冲突仓库，或新建）：

```bash
# 1) 递归策略其实是 ort：两种写法产生的合并结果应一致
git checkout -q master
git merge -s ort feature 2>&1 | head -3 ; git merge --abort 2>/dev/null
git checkout -q master
git merge -s recursive feature 2>&1 | head -3 ; git merge --abort 2>/dev/null
# 二者都应打印同样的 "Auto-merging greeting.txt" / "CONFLICT (contents)"

# 2) -Xours：冲突时自动偏向我方
git checkout -q master
git merge -Xours feature
cat greeting.txt    # 冲突区域应只保留 HEAD(ours) 的内容，且无未解决冲突
git merge --abort 2>/dev/null

# 3) octopus 多头合并（>2 个分支）
git checkout -q master
git merge feature b_oct 2>&1 | head    # 多头默认走 octopus 策略
```

需要观察的现象：

1. `-s ort` 与 `-s recursive` 输出一致——因为 `try_merge_strategy` 把两者都路由到 `merge_ort_recursive`（[builtin/merge.c:800-801](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/merge.c#L800-L801)）。
2. `-Xours` 时 `parse_merge_opt` 把 `recursive_variant` 设为 `MERGE_VARIANT_OURS`，传递到 `merge_3way` 的 `ll_opts.variant = XDL_MERGE_FAVOR_OURS`，于是冲突处自动取 ours，合并干净。
3. 合并超过两个分支时，`DEFAULT_OCTOPUS` 标志使 `octopus` 成为默认，它经 `try_merge_command` 派生 `git merge-octopus` 子进程。

> 若手头没有现成的多头历史，第 3 步可改为源码阅读：在 `try_merge_strategy` 的 `else` 分支确认 `strategy=="octopus"` 会走 `try_merge_command`，并对照 `merge.c` 的 `strvec_pushf(&cmd.args, "merge-%s", strategy)`。

#### 4.3.5 小练习与答案

**练习 1**：`all_strategy[]` 里 `ort` 带 `DEFAULT_TWOHEAD | NO_TRIVIAL` 两个标志，分别影响什么？

**参考答案**：`DEFAULT_TWOHEAD` 让 `ort` 成为「双头合并」的默认策略——用户不指定 `-s` 时就选它。`NO_TRIVIAL` 表示即使检测到「对方相对 base 只有一方改动」这种可平凡合并的情形，也不走 fast-forward 式的捷径，而是老老实实跑完整三方合并（ort 本身就很快，且能正确处理重命名/目录重命名等复杂情况）。

**练习 2**：为什么 `ort` 走进程内、而 `octopus`/`resolve`/`ours` 走子进程？这种差异在源码哪里体现？

**参考答案**：ort 是 git 最核心、最常用的合并路径，做成进程内调用可避免 fork 开销、复用内存中的对象与索引缓存、并支持 in-core 嵌套（合并合并基）。其余策略历史上是独立脚本，保留子进程派发既能复用既有实现、又保持了「策略可外部扩展」的能力。差异体现在 `try_merge_strategy`：`recursive/subtree/ort` 命中 `if` 分支直接调 `merge_ort_recursive`，其余落入 `else` 调 `try_merge_command`（后者用 `run_command` 执行 `git merge-<name>`）。

## 5. 综合实践

把三个模块串起来，做一次「从策略调度到冲突标记」的完整跟踪。

任务：在仓库里制造一个**两方都改了同一文件、且其中一方还重命名了它**的合并，然后：

1. 用 `GIT_TRACE2_PERF=1 git merge feature` 跑一次合并，观察 trace2 输出里的 `collect_merge_info`、`renames`、`process_entries`、`checkout`、`record_conflicted` 等区间——它们正好对应 4.1 节的三段流水线加 `merge_switch_to_result` 的落盘步骤（trace2 机制见 [u13-l3](u13-l3-trace2-profiling.md)）。
2. 在冲突文件里查看冲突标记，确认它的标签是 `branch1`/`branch2`（即 `o.branch1`/`o.branch2`，被 `try_merge_strategy` 设成 `HEAD` 与远程分支名）。
3. 用 `git ls-files -u` 确认 stage 1/2/3 三阶段条目，并思考：为什么 stage 2 的路径可能与 stage 3 不同？（提示：重命名检测让 `pathnames[3]` 三方可能不一致，见 `merge_3way` 里 `branch:path` 标签拼接）。
4. 给该文件配一个 `[merge "<name>"] driver` 并在 `.gitattributes` 声明 `merge=<name>`，重跑合并，确认冲突被自定义驱动接管（不再出现 `<<<<<<<`）。
5. 最后用 `git merge -s octopus feature another` 触发多头合并，对照 `try_merge_command` 确认它派生了 `git merge-octopus` 子进程。

预期：你能用一句话说清「一次 `git merge` 从 `cmd_merge` → 策略表 → `merge_ort_recursive` → `merge_ort_internal`（合并基塌缩）→ `merge_ort_nonrecursive_internal`（收集/重命名/决策）→ `merge_3way` → `ll_merge` → `xdl_merge`（冲突标记）→ `record_conflicted_index_entries`（写 stage）」的完整链路。

## 6. 本讲小结

- **ort 是当前 git 的默认与核心合并策略**，旧 `merge-recursive.c` 已被移除；`git merge -s recursive` 今天跑的就是 ort（`try_merge_strategy` 把 recursive/subtree/ort 路由到同一份 `merge_ort_recursive`）。
- **ort 是「递归 + in-core」**：`merge_ort_internal` 递归地把多个合并基两两合并成虚拟提交（用空树兜底无祖先情况），`merge_ort_nonrecursive_internal` 是「收集三方树信息 → 检测重命名 → 逐路径决策」三段流水线，全程在内存完成，最后才由 `merge_switch_to_result` 落到工作树与索引。
- **逐路径决策靠两个 3 位掩码**：`filemask`/`dirmask` 标记各方在此路径是文件还是目录，`match_mask` 标记哪几方彼此相同；两方相同取第三方、三方全同在收集阶段就短路，只有两两皆不同才进 `handle_content_merge`。
- **冲突写回索引靠 `record_conflicted_index_entries`**：它把 `checkout` 先写入的 stage=0 条目替换成 base/ours/theirs 的 stage 1/2/3；同时还会写一个 `AUTO_MERGE` 引用记录「自动合并结果树」。
- **`merge-ll.c` 是单文件三方合并引擎**：`ll_merge` 按 `.gitattributes` 的 `merge` 属性经 `find_ll_merge_driver` 选驱动（内置 text/binary/union，或用户自定义），`ll_xdl_merge` 调 xdiff 的 `xdl_merge` 产出 `<<<<<<<` 标记并返回 `LL_MERGE_CONFLICT`。
- **`builtin/merge.c` 是策略调度总指挥**：`all_strategy[]` 表 + 标志位（`DEFAULT_TWOHEAD` 等）决定默认策略与能否快进；多策略时用 `evaluate_result`（未合并条目数 + 差异文件数）择优；快进走 `checkout_fast_forward`（`unpack_trees` 的 `twoway_merge`）。

## 7. 下一步学习建议

- 想深入「冲突在工作树与索引层面的三方合并与回滚」，读 [u10-l2 unpack-trees 三方合并与冲突](u10-l2-unpack-trees-merge.md)（`unpack-trees.c` 的合并回调、`merge-blobs.c`、`resolve-undo.c`）。
- 想理解合并基是如何被算出来的，回头精读 [u7-l2](u7-l2-commit-graph-reach.md) 的 `commit-reach.c`：`paint_down_to_common`、世代号剪枝、`repo_get_merge_bases`。
- 想理解冲突标记背后的行级算法，精读 [u8-l2 xdiff](u8-l2-xdiff-library.md)：Myers 算法、`xdl_merge` 的三方扩展。
- 对重命名/目录重命名如何影响合并感兴趣，可继续读 `merge-ort.c` 的 `detect_and_process_renames`、`compute_collisions`、`handle_path_level_conflicts`（本讲仅作流程引用，未展开）。
- 想看合并性能如何被观测，结合 [u13-l3 trace2](u13-l3-trace2-profiling.md)：ort 内部大量 `trace2_region_enter/leave` 埋点正是为此服务。

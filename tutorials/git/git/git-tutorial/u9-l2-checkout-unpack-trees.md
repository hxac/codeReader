# checkout/switch 与 unpack-trees

## 1. 本讲目标

`git checkout` / `git switch` 是日常使用频率最高的命令之一：切换分支、检出文件、还原工作树。但从源码看，它牵动了 git 几乎所有核心子系统——对象库（读 tree）、索引（重建 `index_state`）、工作树（写文件、删文件）、合并引擎（多方比较）。本讲学完后你应当能够：

1. 说清 `git switch <branch>` 在源码里走过的「两阶段」：先在内存里重建一个新索引，再据此把工作树对齐。
2. 读懂 `unpack_trees()` 的「多路合并 + 工作树更新」骨架，理解 `twoway_merge` 这张合并判定表。
3. 解释 `entry.c` 如何把一个 `cache_entry` 真正写成磁盘上的文件（普通文件 / 符号链接 / gitlink）。
4. 回答核心实践问题：**旧内容移除与新内容写入是如何协调的**——为什么 git 不会误删你未提交的修改。

## 2. 前置知识

本讲建立在前两讲之上，请确认你已经理解：

- **三层数据模型**（u4-l1）：工作树（working tree）／索引（index）／对象数据库（object database）。索引是工作树与对象库之间的「桥梁」，每条 `cache_entry` 记录「路径 → oid + stat 快照」。
- **四种对象类型**（u3-l1）：`tree` 对象记录一个目录的清单（一组 `{mode, name, oid}` 条目），`commit` 指向一棵顶层 `tree`。checkout 的本质就是把某棵 `tree` 「铺」到工作树和索引里。
- **`cache_entry` 与标志位**（u4-l1）：`ce_flags` 分「磁盘标志」与「内存标志」两段。本讲大量用到内存标志，尤其是 `CE_UPDATE`（需写入工作树）、`CE_WT_REMOVE`（需从工作树删除）、`CE_MATCHED`（被 pathspec 命中）。

补充两个本讲的关键直觉：

- **checkout 是「按 tree 重建索引 + 对齐工作树」**，不是「逐文件复制」。git 永远先把目标 tree 折算成一份新索引，再用这份索引去驱动工作树的增删改。
- **「安全」优先于「彻底」**。checkout 在覆盖或删除文件前，会用 `verify_absent` / `verify_uptodate` 反复确认「我不会毁掉用户没存进对象库的内容」，确认失败就拒绝并报错（如 `error: Your local changes ... would be overwritten`）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `builtin/checkout.c` | checkout/switch/restore 三个子命令的统一实现，包含分支切换主流程 `merge_working_tree` 与路径检出主流程 `checkout_paths` |
| `unpack-trees.c` | 通用「把 N 棵 tree 合并进索引并更新工作树」引擎，是 checkout / merge / reset / pull 的公共底座 |
| `entry.c` | 把单条 `cache_entry` 物理写进工作树：建目录、读 blob、转换、落盘、回填 stat |
| `read-cache-ll.h` | 定义 `CE_UPDATE` / `CE_WT_REMOVE` 等内存标志位 |
| `entry.h` | 定义贯穿三层调用的 `struct checkout` 状态结构 |

入口提示（u1-l4 已建立）：`git checkout` 的 C 入口是 `builtin/checkout.c` 的 `cmd_checkout`，`git switch` 是 `cmd_switch`，二者最终都汇入 `checkout_main`。

## 4. 核心概念与源码讲解

### 4.1 checkout 索引与工作树更新

#### 4.1.1 概念说明

`git checkout` 其实是「一个命令、两种模式」：

- **分支模式**（branch mode）：`git switch <branch>`、`git checkout <branch>`。目标是把整个工作树连同索引一起搬到另一棵 tree 上。
- **路径模式**（path mode）：`git checkout -- <paths>`、`git checkout <tree> -- <paths>`、`git restore`。目标是只动指定的若干路径，不切换分支。

区分这两种模式是理解源码的第一把钥匙。`checkout_main` 在解析完所有参数后，用下面这段二选一的分发决定走向（[builtin/checkout.c:2079-2082](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/checkout.c#L2079-L2082)）：有 `--patch` 或显式 pathspec 就走 `checkout_paths`，否则走 `checkout_branch`。这里的 `opts->pathspec.nr` 是否为 0 是判据。

```c
if (opts->patch_mode || opts->pathspec.nr)
    ret = checkout_paths(opts, &new_branch_info);
else
    ret = checkout_branch(opts, &new_branch_info);
```

注意：路径模式里还藏着一个「覆盖语义」的小分支。当 source 是一棵 tree、且没指定 `--no-overlay` 时，源 tree 里**没有**的路径不会被删除（overlay，叠加）；而分支模式默认是「全量替换」。`mark_ce_for_checkout_overlay` 里有句关键注释说明这一点（见 4.1.3）。

#### 4.1.2 核心流程

分支模式（`checkout_branch` → `switch_branches` → `merge_working_tree`）的主干：

```
git switch feature
   │
   ├─ repo_hold_locked_index()        # 拿索引写锁 .git/index.lock
   ├─ repo_read_index_preload()       # 读入当前索引
   ├─ 解析 old_tree（当前 HEAD）与 new_tree（目标分支的 commit）
   │
   ├─ init_topts(&topts, ...)         # 配置 unpack_trees_options：
   │       fn = twoway_merge          #   用 2-way 合并回调
   │       update = 1, merge = 1      #   要更新工作树、要做合并
   │
   ├─ init_tree_desc(trees[0..1])     # 把两棵 tree 包成 tree_desc 数组
   ├─ unpack_trees(2, trees, &topts)  # ★ 核心：重建索引 + 对齐工作树
   │
   ├─ cache_tree_update()             # 修补 cache-tree
   └─ write_locked_index(... COMMIT_LOCK)  # 原子提交新索引
```

这段流程把「找到 old/new 两棵 tree → 交给 `unpack_trees` → 落锁」串成一条线，全部发生在 `merge_working_tree` 里（见 4.1.3）。

路径模式（`checkout_paths`）则短得多：它**不走 `unpack_trees` 的合并回调**，而是直接把目标 tree 的条目用 `read_tree_some` 读进当前索引（每条打上 `CE_UPDATE`），再给命中的条目打 `CE_MATCHED`，最后由 `checkout_worktree` 逐条 `checkout_entry`。这是两条模式在实现上的根本差异。

#### 4.1.3 源码精读

**分支模式的两棵 tree。** `merge_working_tree` 先持锁、读索引、解析新 tree，然后构造 old/new 两个 `tree_desc` 喂给 `unpack_trees`（[builtin/checkout.c:838-922](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/checkout.c#L838-L922)）。关键几句：

```c
/* 2-way merge to the new branch */
init_topts(&topts, opts->show_progress,
           opts->overwrite_ignore, quiet);
init_checkout_metadata(&topts.meta, ...);

old_commit_oid = old_branch_info->commit ?
    &old_branch_info->commit->object.oid :
    the_hash_algo->empty_tree;          /* 旧端点：当前 HEAD 或空 tree */
...
init_tree_desc(&trees[0], &tree->object.oid, tree->buffer, tree->size);
...
init_tree_desc(&trees[1], &tree->object.oid, tree->buffer, tree->size);

ret = unpack_trees(2, trees, &topts);
```

`trees[0]` 是**旧 tree**（当前 HEAD），`trees[1]` 是**新 tree**（目标分支）。为什么要传旧 tree？因为 2-way 合并需要知道「这条路径在 HEAD 里是什么」才能判断「用户在本地改过没有」。这正是 checkout 能保护本地修改的依据。

**`init_topts` 设定的合并策略。** [builtin/checkout.c:818-836](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/checkout.c#L818-L836)：

```c
topts->head_idx = -1;
topts->src_index = the_repository->index;
topts->dst_index = the_repository->index;
...
topts->initial_checkout = is_index_unborn(the_repository->index);
topts->update = 1;          /* 合并后要把变化写进工作树 */
topts->merge = 1;           /* 启用合并语义（而非单纯覆盖） */
topts->fn = twoway_merge;   /* ★ 逐路径合并用这张表 */
```

`topts->fn` 就是 4.2 要讲的合并回调。注意 `src_index == dst_index`：unpack 完成后会把内存里的结果索引**原地替换**回 `the_repository->index`。

**路径模式的「读 tree 进索引」。** `update_some` 把目标 tree 的每条记录做成带 `CE_UPDATE` 的 `cache_entry` 加进索引（[builtin/checkout.c:193-234](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/checkout.c#L193-L234)）。若索引里已有相同 oid/mode 的旧条目，它只给旧条目补一个 `CE_UPDATE` 标记、丢弃新建的 ce——避免无谓的重复写入：

```c
ce->ce_flags = create_ce_flags(0) | CE_UPDATE;   /* 新条目：待写入工作树 */
...
if (ce->ce_mode == old->ce_mode && ... && oideq(&ce->oid, &old->oid)) {
    old->ce_flags |= CE_UPDATE;                   /* 命中已有：只打标记 */
    discard_cache_entry(ce);
    return 0;
}
```

**overlay 语义。** `mark_ce_for_checkout_overlay` 解释了「源 tree 没有的路径在 overlay 模式下不动」（[builtin/checkout.c:387-419](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/checkout.c#L387-L419)）：当 `opts->source_tree` 非空且条目没被打 `CE_UPDATE`（即不是来自目标 tree），直接 return，不给它打 `CE_MATCHED`，于是 `checkout_worktree` 不会处理它。

**路径模式的写出循环。** `checkout_worktree` 遍历整个索引，只对 `CE_MATCHED` 的条目操作（[builtin/checkout.c:440-492](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/checkout.c#L440-L492)）：

```c
for (pos = 0; pos < the_repository->index->cache_nr; pos++) {
    struct cache_entry *ce = the_repository->index->cache[pos];
    if (ce->ce_flags & CE_MATCHED) {
        if (!ce_stage(ce)) {                       /* 非冲突条目：直接检出 */
            errs |= checkout_entry(ce, &state, NULL, &nr_checkouts);
            continue;
        }
        if (opts->writeout_stage)                  /* --ours/--theirs */
            errs |= checkout_stage(opts->writeout_stage, ce, pos, ...);
        else if (opts->merge)                      /* --merge 合并冲突 */
            errs |= checkout_merged(pos, &state, ...);
        pos = skip_same_name(ce, pos) - 1;         /* 跳过同路径的其它 stage */
    }
}
```

这里的 `state` 就是 `struct checkout`（见 4.3.1），`checkout_entry` 进入 `entry.c` 的写出逻辑。

#### 4.1.4 代码实践

**实践目标：** 直观看到分支切换「先重建索引、再写工作树」，并体会 `unpack_trees` 对本地修改的保护。

**操作步骤：**

1. 建一个练习仓库并造两棵不同的 tree：
   ```sh
   git init chk-practice && cd chk-practice
   printf 'main-v1\n' > a.txt
   git add a.txt && git commit -m base
   git switch -c feature
   printf 'feature-v1\n' > a.txt
   git commit -am feature
   git switch master            # 回到 a.txt = main-v1
   ```
2. 在工作树里**手动**改 `a.txt`（不 add，不 commit），制造「本地未提交修改」：
   ```sh
   printf 'main-v1\nMY-LOCAL-EDIT\n' > a.txt
   ```
3. 开启 trace2 性能追踪后切换分支，观察耗时分布：
   ```sh
   GIT_TRACE2_PERF=1 git switch feature
   ```
4. 重复第 2 步再次制造本地修改，然后尝试切回 master。

**需要观察的现象：**

- 第 3 步：因为你的本地编辑与 `feature` 分支对该文件的改动**冲突**，git 会拒绝切换并报 `error: Your local changes to the following files would be overwritten by checkout`，工作树与索引**纹丝不动**。这正是 4.2 里 `verify_uptodate` 的作用。
- trace2 输出里能看到 `region_enter ... unpack_trees`、`traverse_trees`、`check_updates` 等若干 region，对应本讲讲的「合并阶段」与「工作树更新阶段」。

**预期结果：** 切换被安全拒绝，文件内容仍是你手改的版本。若想强制，需 `git stash` 或 `git switch -m`（启用 `checkout_merged`）。如果你手动撤销本地编辑（`git restore a.txt`）再 `git switch feature`，切换会成功，`a.txt` 变成 `feature-v1`。

> 待本地验证：trace2 region 的具体耗时数值依机器而定；重点是能否看到 `unpack_trees` 与 `check_updates` 两个 region 名。

#### 4.1.5 小练习与答案

**练习 1：** 为什么 `merge_working_tree` 要把**旧 tree**（`trees[0]`）也传给 `unpack_trees`，而不是只传新 tree？

**参考答案：** 2-way 合并需要三个输入来判断「这条路径该怎么处理」：索引里的当前内容（`current`）、HEAD 里的旧内容（`oldtree`）、目标分支的新内容（`newtree`）。只有知道 oldtree，才能判断「用户在本地是否相对 HEAD 做过修改」——若 `current` 与 `oldtree` 相同说明没改过，可以放心换成 `newtree`；若不同则说明有未提交修改，需进一步核对会不会被覆盖。少了旧 tree 就无法安全地区分「该路径是用户主动改的」还是「只是分支差异」。

**练习 2：** 路径模式下 `update_some` 给条目打的 `CE_UPDATE` 标志，最终被谁消费？

**参考答案：** 在 `checkout_worktree` 里，命中 `CE_MATCHED` 的非冲突条目会调用 `checkout_entry`；而 `checkout_entry`→`write_entry` 正是依据该条目需要落到工作树来执行的（`CE_UPDATE` 在 `checkout_paths` 路径里更多是「语义提醒」，真正驱动写出的是 `CE_MATCHED` 与 `checkout_entry` 本身，见 4.3）。

---

### 4.2 unpack-trees 多路遍历

#### 4.2.1 概念说明

`unpack-trees.c` 是 git 里最底层的「tree 操作引擎」。它的职责用一句话概括：**给定 N 棵 tree（加上当前索引），按某种合并规则产出一个新索引，并据此更新工作树。** checkout 只是用其中「N=2、`fn=twoway_merge`」的一种用法；`git read-tree`、`git merge`、`git reset`、`git pull` 都复用同一引擎，只是换 N 和换 `fn`（合并回调）。

它的核心设计是一个**两阶段**模型，这也是回答「旧内容移除与新内容写入如何协调」的关键：

- **阶段一——合并（merge）：** 多路遍历所有来源 tree 与索引，对**每一个路径**调用合并回调（如 `twoway_merge`）。回调只做「决策」：这条路径该 `keep`（保留）、`merge`（换成新内容）、还是 `delete`（删除）。决策结果连同一组**内存标志**写入一个全新的结果索引 `o->internal.result`。标志告诉系统「这条路径之后要不要动工作树」。
- **阶段二——工作树更新（check_updates）：** 遍历结果索引，按标志物理执行：`CE_WT_REMOVE` 的文件删掉、`CE_UPDATE` 的文件写出。**删除永远先于写出**，避免「要写的文件落在即将删除的目录里」之类的冲突。

两个阶段用同一份标志位通信，标志位定义在 [read-cache-ll.h:48-62](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache-ll.h#L48-L62)：

```c
#define CE_UPDATE            (1 << 16)  /* 内容需要在工作树更新（写出） */
#define CE_REMOVE            (1 << 17)  /* 将从索引移除 */
#define CE_UPTODATE          (1 << 18)
#define CE_ADDED             (1 << 19)
#define CE_WT_REMOVE         (1 << 22)  /* 需要从工作目录删除 */
...
```

#### 4.2.2 核心流程

`unpack_trees()` 的整体骨架（伪代码）：

```
unpack_trees(len, trees, o):
    # —— 准备 ——
    建 o->internal.result（一个空的 index_state）
    mark_all_ce_unused(src_index)
    若启用 sparse-checkout：先给现有条目算 SKIP_WORKTREE

    # —— 阶段一：多路遍历 + 合并回调 ——
    setup_traverse_info(&info); info.fn = unpack_callback
    traverse_trees(src_index, len, trees, &info)
        # 内部按路径字典序推进多棵 tree 的游标，
        # 对每个路径收集 src[0..len] 后调用 o->fn（=twoway_merge）

    # 收尾：合并模式下把索引里剩下的条目也喂给回调
    while (ce = next_cache_entry(o)): unpack_index_entry(ce, o)

    # —— 阶段二：把 result 对齐到工作树 ——
    check_updates(o, &o->internal.result)
        # 先删 CE_WT_REMOVE，再写 CE_UPDATE

    # —— 用 result 替换目标索引 ——
    discard_index(o->dst_index)
    *o->dst_index = o->internal.result
```

其中 `twoway_merge` 对单条路径的判定表（`current`=索引、`oldtree`=HEAD、`newtree`=目标），核心几条规则：

| current | oldtree | newtree | 判定 | 含义 |
| --- | --- | --- | --- | --- |
| 有，无冲突，== oldtree | 有 | 有，≠ current | `merged_entry` | 该路径你本地没动，直接快进到新分支版本 |
| 有 | 有 | 无，且 current==oldtree | `deleted_entry` | 新分支删了这个文件，你也没改 → 删 |
| 有 | 有 | 有，== oldtree | `keep_entry` | 新旧分支该路径相同 → 保持现状（含本地修改） |
| 有，本地改过(≠oldtree) | 有 | 有，≠ oldtree 且 ≠ current | `reject_merge` | 三方都不同 → 拒绝，避免覆盖本地修改 |
| 无 | （无/有） | 有 | `merged_entry` | 新出现的文件 → 检出 |

这张表的精髓是：**只要 `current` 与 `oldtree` 一致，就说明本地干净，可以无条件接受 `newtree` 的变化**；否则要小心翼翼地核对，对不上的就 `reject_merge`。

#### 4.2.3 源码精读

**`unpack_trees` 主函数的「阶段一」。** 它把两棵 tree 的遍历交给 `traverse_trees`，回调设为 `unpack_callback`（[unpack-trees.c:1982-2016](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/unpack-trees.c#L1982-L2016)）：

```c
if (len) {
    const char *prefix = o->prefix ? o->prefix : "";
    struct traverse_info info;

    setup_traverse_info(&info, prefix);
    info.fn = unpack_callback;     /* 每个路径都会回调到这里 */
    info.data = o;
    ...
    ret = traverse_trees(o->src_index, len, t, &info);
    ...
}
```

`traverse_trees`（定义在 `tree-walk.c`）以字典序同时推进多棵 tree 的游标，把同一路径在各来源里的 `cache_entry` 收集成 `src[]` 数组，再调用 `info.fn`——最终落到我们配置的 `o->fn = twoway_merge`。

**`twoway_merge` 决策表。** [unpack-trees.c:2911-2986](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/unpack-trees.c#L2911-L2986) 把上面的判定表直接写成 `if/else` 链。挑两条最能体现安全性的看：

```c
const struct cache_entry *current = src[0];
const struct cache_entry *oldtree = src[1];
const struct cache_entry *newtree = src[2];
...
if (current) {
    ...
    } else if ((!oldtree && !newtree) ||                 /* 4 and 5 */
         (!oldtree && newtree && same(current, newtree)) || /* 6 and 7 */
         (oldtree && newtree && same(oldtree, newtree)) || /* 14 and 15 */
         (oldtree && newtree && !same(oldtree, newtree) && /* 18 and 19 */
          same(current, newtree))) {
        return keep_entry(current, o);     /* 保持：不动工作树，保留本地内容 */
    } else if (oldtree && !newtree && same(current, oldtree)) {
        /* 10 or 11 */
        return deleted_entry(oldtree, current, o);   /* 本地没改 + 新分支删了 → 删 */
    } else if (oldtree && newtree &&
             same(current, oldtree) && !same(current, newtree)) {
        /* 20 or 21 */
        return merged_entry(newtree, current, o);    /* 本地没改 + 新分支改了 → 换成新的 */
    }
    ...
    return reject_merge(current, o);  /* 兜底：拒绝，避免覆盖本地修改 */
}
```

注释里的编号 `4/5/6/7/...` 对应 git 源码注释里那张经典的 2-way merge 真值表（共约 21 种 old/new/current 的组合）。注意 `same(a, b)` 比较的是 oid 与 mode。

**`merged_entry`：把「换成新内容」翻译成标志。** [unpack-trees.c:2559-2642](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/unpack-trees.c#L2559-L2642) 决定给结果条目打什么标志。它的关键是：若旧条目存在且与待合并条目**完全相同**，就复用旧条目的 stat、并清掉 `update`，于是该路径**不会**被写到工作树（保护本地内容）：

```c
int update = CE_UPDATE;                          /* 默认：需要写出 */
struct cache_entry *merge = dup_cache_entry(ce, &o->internal.result);

if (!old) {                                      /* 新文件 */
    update |= CE_ADDED;
    ...
    if (verify_absent(merge, ERROR_WOULD_LOSE_UNTRACKED_OVERWRITTEN, o))
        return -1;                               /* 工作树有未跟踪同名文件 → 拒绝 */
} else if (!(old->ce_flags & CE_CONFLICTED)) {
    if (same(old, merge)) {                      /* 与索引里完全一样 */
        copy_cache_entry(merge, old);
        update = 0;                              /* ★ 不写工作树，保住本地修改 */
    } else {
        if (verify_uptodate(old, o))             /* 本地有改动 → 拒绝 */
            return -1;
        ...
    }
}
if (do_add_entry(o, merge, update, CE_STAGEMASK) < 0)
    return -1;
```

`verify_uptodate`（同文件上方）会检查旧条目是否 `CE_UPTODATE`；若工作树文件被改过、索引还标记着「不是最新」，就拒绝合并并报「local changes would be overwritten」。这是 checkout 保护未提交修改的**真正落点**。

**`deleted_entry`：把「删除」翻译成 `CE_WT_REMOVE`。** [unpack-trees.c:2675-2693](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/unpack-trees.c#L2675-L2693)：

```c
static int deleted_entry(const struct cache_entry *ce,
                         const struct cache_entry *old,
                         struct unpack_trees_options *o)
{
    if (!old) {                                  /* 索引里都没有 */
        if (verify_absent(ce, ERROR_WOULD_LOSE_UNTRACKED_REMOVED, o))
            return -1;                           /* 但工作树有未跟踪同名 → 拒绝删除 */
        return 0;
    } else if (verify_absent_if_directory(...)) { return -1; }

    if (!(old->ce_flags & CE_CONFLICTED) && verify_uptodate(old, o))
        return -1;                               /* 本地改过 → 拒绝删除 */
    add_entry(o, ce, CE_REMOVE, 0);              /* 进结果索引，带 CE_REMOVE */
    ...
}
```

**`CE_REMOVE` 如何变成「从工作树删除」。** 关键在 `do_add_entry`：只要 set 里有 `CE_REMOVE`，就自动补上 `CE_WT_REMOVE`（[unpack-trees.c:217-228](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/unpack-trees.c#L217-L228)）：

```c
static int do_add_entry(struct unpack_trees_options *o, struct cache_entry *ce,
                        unsigned int set, unsigned int clear)
{
    clear |= CE_HASHED;
    if (set & CE_REMOVE)
        set |= CE_WT_REMOVE;                     /* ★ 删除索引条目 ⇒ 同时删工作树文件 */
    ce->ce_flags = (ce->ce_flags & ~clear) | set;
    return add_index_entry(&o->internal.result, ce, ...);
}
```

**阶段二 `check_updates`：先删后写。** [unpack-trees.c:424-515](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/unpack-trees.c#L424-L515) 是「协调」二字的本体。它分两段循环，中间夹着一次 `remove_marked_cache_entries`：

```c
/* 第一段：删除所有 CE_WT_REMOVE */
for (i = 0; i < index->cache_nr; i++) {
    const struct cache_entry *ce = index->cache[i];
    if (ce->ce_flags & CE_WT_REMOVE) {
        display_progress(progress, ++cnt);
        unlink_entry(ce, o->super_prefix);       /* 从工作树删文件 */
    }
}
remove_marked_cache_entries(index, 0);           /* 从索引删条目 */
remove_scheduled_dirs();                         /* 清理空目录 */
...
/* 第二段：写出所有需要更新的条目 */
for (i = 0; i < index->cache_nr; i++) {
    struct cache_entry *ce = index->cache[i];
    if (must_checkout(ce)) {                     /* must_checkout ⇔ CE_UPDATE */
        ...
        ce->ce_flags &= ~CE_UPDATE;
        errs |= checkout_entry(ce, &state, NULL, NULL);  /* 写工作树 */
        ...
    }
}
```

其中 `must_checkout` 就是判断 `CE_UPDATE`（[unpack-trees.c:419-422](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/unpack-trees.c#L419-L422)）：

```c
static int must_checkout(const struct cache_entry *ce)
{
    return ce->ce_flags & CE_UPDATE;
}
```

**「先删后写」为什么重要：** 如果先写新文件再删旧文件，遇到「把目录 `d/` 整体换成文件 `d`」（典型的目录/文件冲突场景）就会卡住——要先删掉 `d/` 目录才能创建文件 `d`。先删后写正是 `deleted_entry`/`merged_entry` 产出的 `CE_WT_REMOVE` 与 `CE_UPDATE` 在两段循环里被分别消费的结果。这正是实践任务要找的答案。

> 说明：上面引用的 `twoway_merge` 注释编号、`merged_entry` 的 sparse-checkout 分支等细节，是为突出主干而略去的；sparse 目录的合并另有 `merged_sparse_dir` 处理，超出本讲范围。

#### 4.2.4 代码实践

**实践目标：** 在真实切换中观察「先删后写」的协调，并跟踪 `check_updates` 的两个循环。

**操作步骤：**

1. 沿用 4.1.4 的仓库，先确保工作树干净：
   ```sh
   git restore a.txt        # 撤销本地修改
   git switch master        # 回到 base（a.txt=main-v1）
   ```
2. 制造一个「分支差异 + 删除」场景：
   ```sh
   printf 'x\n' > only-on-feature.txt
   git add only-on-feature.txt && git commit -m "add file on master"
   git switch feature       # feature 里没有 only-on-feature.txt
   ```
3. 把 `only-on-feature.txt` 改名（模拟另一类差异），再切回 master：
   ```sh
   git mv a.txt renamed-on-feature.txt 2>/dev/null || mv a.txt renamed-on-feature.txt
   git switch master
   ```
4. （可选）用调试钩子看标志位流转——在 `check_updates` 的两段循环里，git 没有「打印标志」的官方开关，但你可以借助 `GIT_TRACE2_PERF=1` 看到 `check_updates` region 的耗时与进度计数。

**需要观察的现象：**

- 第 2 步切到 `feature` 后，`only-on-feature.txt` 从工作树**消失**——这是 `deleted_entry` 产出 `CE_WT_REMOVE`、`check_updates` 第一段循环 `unlink_entry` 的结果。
- 第 3 步切回 `master` 后，`a.txt`（master 版本）重新出现、`renamed-on-feature.txt` 消失——一次切换里同时发生了「删」和「写」，且删先于写。

**预期结果：** 切换前后用 `ls` 对照，能清楚看到文件的增删。结合 4.2.3 的源码，你能把每个文件的变化对应到 `merged_entry`/`deleted_entry` 产出的标志、再到 `check_updates` 的某一段循环。

> 待本地验证：第 3 步若 `git mv` 因状态不干净而失败，可先 `git restore --staged . && git restore .` 重置后再操作。

#### 4.2.5 小练习与答案

**练习 1：** 假设 `current == oldtree` 且 `newtree` 与二者都不同，`twoway_merge` 会走哪条分支？为什么不担心覆盖本地修改？

**参考答案：** 走 `merged_entry(newtree, current, o)`（注释「20 or 21」分支）。因为 `current == oldtree` 说明索引里的内容与 HEAD 完全一致，即用户**没有**对该路径做本地修改，所以可以安全地把内容换成 `newtree`，无需 `verify_uptodate` 拦截。

**练习 2：** `check_updates` 为什么必须「先删 `CE_WT_REMOVE`、后写 `CE_UPDATE`」，而不是边遍历边处理？

**参考答案：** 先删可以腾空目录，避免后续写出时遇到目录/文件名冲突（例如把目录 `d/` 整体替换成文件 `d`，必须先删除 `d/` 才能创建 `d`）。此外先统一删除、`remove_scheduled_dirs` 清理空目录，再统一写出，逻辑更简单、错误更少。把删与写混在同一遍里，需要额外处理「同路径先删后建」的特殊顺序，得不偿失。

**练习 3：** `do_add_entry` 里「`set & CE_REMOVE` 就补 `CE_WT_REMOVE`」这一行，如果删掉会发生什么？

**参考答案：** 被合并回调判为「删除」的路径只会从索引里移除（`CE_REMOVE` 只影响索引），但工作树里的文件不会被删（`CE_WT_REMOVE` 才驱动 `unlink_entry`）。结果就是索引与工作树不一致：`git status` 会把这些本该删除的文件显示为 `Untracked files`。这正是该行把「索引删除」与「工作树删除」绑定的原因。

---

### 4.3 entry 写出文件

#### 4.3.1 概念说明

阶段二确定「这条路径要写」之后，具体怎么把一个 `cache_entry` 变成磁盘上的文件，就是 `entry.c` 的事。它是 checkout 与工作树之间的最后一公里。

贯穿调用的状态结构是 `struct checkout`（[entry.h:9-22](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/entry.h#L9-L22)），它不是「一次 checkout 命令的选项」，而是「一次写出操作的工作参数」：

```c
struct checkout {
    struct index_state *istate;     /* 关联的索引（用于 ie_match_stat 等） */
    const char *base_dir;           /* 工作树根目录前缀，默认 "" */
    int base_dir_len;
    const char *super_prefix;       /* --super-prefix，递归操作时用 */
    struct delayed_checkout *delayed_checkout;  /* 延迟过滤器（如 git-lfs） */
    struct checkout_metadata meta;  /* 传给 smudge 过滤器的元数据 */
    unsigned force:1,               /* 是否强制覆盖已有文件 */
             quiet:1,
             not_new:1,             /* 只读模式：不创建新文件 */
             clone:1,               /* 是否处于 clone（用于冲突检测） */
             refresh_cache:1;       /* 写完后是否回填 stat 到索引 */
};
```

`checkout_entry` 只是个薄包装，转发给 `checkout_entry_ca`（`ca` 指预加载的 `conv_attrs`，可为 NULL）。真正的逻辑在 `checkout_entry_ca` 与 `write_entry` 两层。

#### 4.3.2 核心流程

写出一个 `cache_entry` 的主干：

```
checkout_entry(ce, state)
 ├─ checkout_entry_ca(ce, NULL, state, ...)
 │    ├─ 若 CE_WT_REMOVE → unlink_entry(ce) 直接返回   # 删除走这里
 │    ├─ 拼出工作树完整路径 path = base_dir + ce->name
 │    ├─ check_path(path) → lstat 看文件是否已存在
 │    │    ├─ 存在且内容未变(ie_match_stat) → return 0  # 无需写
 │    │    ├─ 存在且变了 & !force → 报错拒绝             # 保护本地修改
 │    │    └─ 存在且变了 & force → unlink 旧文件
 │    ├─ create_directories(path)                       # mkdir -p 前导目录
 │    ├─ enqueue_checkout(...) → 并行检出队列（若启用）
 │    └─ write_entry(ce, path, ca, state, ...)
 │         ├─ 按 ce_mode 分派：
 │         │    S_IFLNK  → symlink(target, path)
 │         │    S_IFREG  → read_blob_entry + convert_to_working_tree
 │         │              + open + write_in_full          # 含 CRLF/smudge 转换
 │         │    S_IFGITLINK → mkdir + submodule_move_head # 子模块
 │         └─ 若 refresh_cache：lstat + update_ce_after_write  # 回填 stat
```

两个要点：① 内容转换（`convert_to_working_tree`）发生在写出前，把 git 内部格式（LF、干净字节）转成工作树格式（如 CRLF、应用 `.gitattributes` 的 smudge 过滤器）；② 写完立刻 `lstat` 新文件并把 stat 快照回填进 `cache_entry`，这样索引里的 stat 就与工作树一致，下次 `git status` 不必重比内容。

#### 4.3.3 源码精读

**`checkout_entry_ca`：单文件写出的总调度。** [entry.c:481-592](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/entry.c#L481-L592)。先看它如何分情况：

```c
int checkout_entry_ca(struct cache_entry *ce, struct conv_attrs *ca,
                      const struct checkout *state, char *topath,
                      int *nr_checkouts)
{
    ...
    if (ce->ce_flags & CE_WT_REMOVE) {        /* ★ 删除分支：与写出共用入口 */
        ...
        unlink_entry(ce, state->super_prefix);
        return 0;
    }
    ...
    strbuf_add(&path, state->base_dir, state->base_dir_len);
    strbuf_add(&path, ce->name, ce_namelen(ce));

    if (!check_path(path.buf, path.len, &st, state->base_dir_len)) {  /* 已存在？ */
        unsigned changed = ie_match_stat(state->istate, ce, &st, ...);
        ...
        if (!changed)
            return 0;                          /* 内容没变：不写，省 IO */
        if (!state->force) {
            ... fprintf(stderr, "%s already exists, no checkout\n", path.buf);
            return -1;                         /* 有本地修改且非 force：拒绝 */
        }
        ...
        if (unlink(path.buf))                  /* force：删旧再写新 */
            return error_errno("unable to unlink old '%s'", path.buf);
    } else if (state->not_new)
        return 0;                              /* 只读模式：不建新文件 */

    create_directories(path.buf, path.len, state);   /* mkdir -p */
    ...
    if (!enqueue_checkout(ce, ca, nr_checkouts))     /* 进并行检出队列（若启用） */
        return 0;
    return write_entry(ce, path.buf, ca, state, 0, nr_checkouts);
}
```

这里能看到 entry 层的**第二道安全闸**（第一道在 4.2 的 `verify_uptodate`）：即使合并阶段放行了，落盘前若发现工作树文件已被改且 `!state->force`，仍会拒绝。`state->force` 在 `check_updates` 里被设为 1（见 4.2.3 的 `state.force = 1`），因为能走到这里说明合并阶段已确认安全；但路径模式的 `checkout_worktree` 也设 `state.force = 1`。

**`write_entry`：按文件类型分派落盘。** [entry.c:283-422](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/entry.c#L283-L422)，核心是 `switch (ce_mode_s_ifmt)`：

```c
switch (ce_mode_s_ifmt) {
case S_IFLNK:                       /* 符号链接 */
    new_blob = read_blob_entry(ce, &size);
    ...
    if (!has_symlinks || to_tempfile) goto write_file_entry;  /* 无 symlink 支持退化为普通文件 */
    ret = symlink(new_blob, path);
    break;

case S_IFREG:                       /* 普通文件 */
    ...
    new_blob = read_blob_entry(ce, &size);   /* 从对象库读 blob */
    ...
    /* 把 git 内部格式转成工作树格式（CRLF、smudge 等） */
    ret = convert_to_working_tree_ca(ca, ce->name, new_blob, size, &buf, &meta);
    if (ret) { new_blob = strbuf_detach(&buf, &newsize); size = newsize; }

write_file_entry:
    fd = open_output_fd(path, ce, to_tempfile);
    wrote = write_in_full(fd, new_blob, size);
    if (!to_tempfile)
        fstat_done = fstat_checkout_output(fd, state, &st);   /* 立即 stat */
    close(fd);
    break;

case S_IFGITLINK:                   /* 子模块（gitlink） */
    if (mkdir(path, 0777) < 0) ...
    sub = submodule_from_ce(ce);
    if (sub)
        return submodule_move_head(ce->name, ...);            # 交给子模块流程
    break;
}
finish:
if (state->refresh_cache) {
    ...
    update_ce_after_write(state, ce, &st);   /* ★ 回填 stat，避免下次重比内容 */
}
```

**stat 回填的意义（呼应 u4-l1）：** `update_ce_after_write` 把刚写出文件的 `lstat` 结果写进 `ce->ce_stat_data`。由于刚写、文件大小与时间戳都是「当下」的，索引里记的 stat 与磁盘一致，下次 `git status` 走「先 stat 后内容」的快速路径就能直接判定「未变更」。（u4-l1 提到的 racy clean 问题也是在这里埋下时间戳。）

**`unlink_entry`：删除也要走子模块流程。** [entry.c:594-607](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/entry.c#L594-L607)——若该条目是 gitlink，先 `submodule_move_head` 再删文件，并 `schedule_dir_for_removal` 让 `remove_scheduled_dirs` 在合适时机清空空目录。

> 说明：`write_entry` 还涉及 `streaming_write_entry`（流式写出大文件，避免全量读进内存）与 `delayed_checkout`（延迟过滤器，如 git-lfs 把真实内容推迟到 `finish_delayed_checkout` 才写入），本讲只点出它们的存在，不展开。

#### 4.3.4 代码实践

**实践目标：** 观察 entry 层的「内容转换」与「stat 回填」，体会 git 写文件不是简单 `cp`。

**操作步骤：**

1. 新建仓库，配置 `core.autocrlf` 并造一个带 LF 的文件：
   ```sh
   git init entry-practice && cd entry-practice
   printf 'line1\nline2\n' > crlf.txt       # 工作树是 LF
   git add crlf.txt && git commit -m base    # 进对象库的是 LF（git 内部格式）
   ```
2. 配置 autocrlf 后，先删工作树文件再用 checkout 从索引恢复，对比字节：
   ```sh
   git config core.autocrlf input
   rm crlf.txt
   git checkout -- crlf.txt
   xxd crlf.txt | head                        # 看 line endings
   ```
3. 查看索引里记录的 stat 是否与磁盘一致（用 `git ls-files --debug`）：
   ```sh
   git ls-files --debug crlf.txt
   ```
4. （选做）如果你想验证「stat 回填」，可在 `update_ce_after_write`（`entry.c`）里读它的实现，理解它如何把刚写文件的 `st` 拷进 `ce->ce_stat_data`。

**需要观察的现象：**

- 第 2 步：`crlf.txt` 被恢复后，内容来自对象库里的 blob（LF）；`autocrlf=input` 下检出不会改行尾，故仍是 LF。若把配置改成 `core.autocrlf=true`（在 Windows 语义下）再恢复，理论上能看到 CRLF——但具体行尾行为依赖平台与 `.gitattributes`。
- 第 3 步：`git ls-files --debug` 输出里的 `ctime/mtime/dev/ino/size` 应与磁盘上 `crlf.txt` 的真实 stat 一致，这正是 `update_ce_after_write` 回填的结果。

**预期结果：** 恢复出的文件内容来自对象库（经 `convert_to_working_tree` 处理），索引里的 stat 与磁盘一致。这说明 checkout 写文件是「读 blob → 转换 → 落盘 → 回填 stat」的完整流水，而非直接复制。

> 待本地验证：autocrlf 在 Linux 上的具体行尾效果以你本地实测为准；重点是理解转换发生在 `write_entry` 的 `convert_to_working_tree_ca` 这一步。

#### 4.3.5 小练习与答案

**练习 1：** 为什么 `write_entry` 写完文件后要立刻 `lstat` 并调用 `update_ce_after_write`？

**参考答案：** 为了把磁盘文件的真实 stat（大小、mtime、ctime、inode 等）回填进索引的 `cache_entry`。这样下次 `git status` 走 u4-l1 讲的「先 stat 后内容」快速路径时，索引记录与磁盘一致，能直接判定「未变更」，避免重新读文件比内容。不回填的话，每次 status 都要重比内容，性能不可接受。

**练习 2：** `checkout_entry_ca` 在什么情况下会「什么都不写就返回 0」？

**参考答案：** 至少两种：(1) `check_path` 发现文件已存在，且 `ie_match_stat` 判定内容未变（`!changed`）——磁盘已是目标内容，无需写；(2) `state->not_new` 为真且文件不存在——只读模式不建新文件。两者都是 entry 层为减少无谓 IO 而设的短路。

**练习 3：** 子模块（gitlink）的「写出」和普通文件有何不同？

**参考答案：** 普通文件是「读 blob → 转换 → 写字节」；gitlink 不写文件内容，而是 `mkdir` 出子模块目录，再调用 `submodule_move_head` 让子模块自己切换到对应 commit。删除 gitlink 时 `unlink_entry` 也会先走 `submodule_move_head`。子模块的内容管理由其自身仓库负责，主仓库只记录它应处的 commit（OID）。

---

## 5. 综合实践

把三个模块串起来，跟踪一次**带文件增删改**的分支切换，画出从命令行到磁盘的全链路。

**场景构造：**

```sh
git init integration && cd integration
printf 'base\n'        > keep.txt
printf 'will-go\n'     > del.txt
printf 'v1\n'          > mod.txt
mkdir sub && printf 's1\n' > sub/x.txt
git add . && git commit -m base

git switch -c topic
git rm del.txt
printf 'v2\n'          > mod.txt      # 修改
printf 'new\n'         > added.txt    # 新增
git add . && git commit -m topic

git switch master       # 切回：del.txt 应回来、mod.txt 回到 v1、added.txt 库消失
```

**你的任务：**

1. 在 `git switch master` 这一步，对四个文件（`keep.txt`/`del.txt`/`mod.txt`/`added.txt`，外加目录 `sub/`）逐一说明：
   - 它在 `twoway_merge` 里命中哪条分支（`keep_entry` / `merged_entry` / `deleted_entry`）；
   - 结果索引里它被打上了什么标志（无 / `CE_UPDATE` / `CE_WT_REMOVE`）；
   - 它在 `check_updates` 的哪一段循环被处理（第一段删除 / 第二段写出 / 都不处理）。
2. 用 `GIT_TRACE2_PERF=1 git switch master` 抓 trace，确认能看到 `unpack_trees`、`traverse_trees`、`check_updates` 三个 region。
3. （进阶）阅读 `merged_entry` 后回答：为什么 `keep.txt` 这次切换**不会**触发任何工作树写操作？

**参考分析要点：**

- `keep.txt`：old/new/current 三者相同 → `keep_entry` → 无标志 → 不在任一循环处理（trace 里也不计数）。
- `del.txt`：master 有、topic（当前所在）也有且相同，切回 master 时它本就存在……注意场景方向：你是从 `topic` 切到 `master`。在 `topic` 里 `del.txt` 已被删除，切回 `master` 它要**恢复**，命中 `merged_entry`（newtree 有、current 无或不同）→ `CE_UPDATE` → 第二段写出。
- `mod.txt`：从 topic 的 v2 切回 master 的 v1，current(topic)=v2 与 oldtree(topic 的 tree，但这里 oldtree 应取切换前的 HEAD）需结合 `current==oldtree` 判定。若工作树与 topic 一致（干净），则 `current==oldtree`、`newtree` 不同 → `merged_entry` → `CE_UPDATE` → 写出 v1。
- `added.txt`：topic 有、master 没有 → 切回 master 命中 `deleted_entry` → `CE_REMOVE`/`CE_WT_REMOVE` → 第一段 `unlink_entry` 删除。
- `keep.txt` 不触发写：因为 `merged_entry` 里 `same(old, merge)` 成立，`update=0`，不打 `CE_UPDATE`，`must_checkout` 为假。

> 提示：oldtree/newtree 的精确归属取决于你站在哪一侧，关键是先确定「current 对应哪棵 tree、oldtree 是切换前 HEAD、newtree 是目标分支」。若分析时拿不准某条具体命中，标注「待本地验证」并说明你追踪到的调用链。

## 6. 本讲小结

- `git checkout` 在 `checkout_main` 处二选一：有 pathspec/`--patch` 走 `checkout_paths`（路径模式），否则走 `checkout_branch` → `merge_working_tree`（分支模式）。
- 分支模式的本质是把「旧 tree + 新 tree」交给 `unpack_trees(2, trees, &topts)`，`topts.fn = twoway_merge`；路径模式则用 `read_tree_some` 把目标条目读进索引、打 `CE_MATCHED` 后直接 `checkout_entry`。
- `unpack_trees` 是「**阶段一合并 → 阶段二工作树更新**」的两阶段引擎：合并回调 `twoway_merge`/`merged_entry`/`deleted_entry` 只产决策与标志（`CE_UPDATE`/`CE_WT_REMOVE`），`check_updates` 据此物理执行。
- **旧内容移除与新内容写入的协调**落在 `check_updates`：**先删 `CE_WT_REMOVE`、后写 `CE_UPDATE`**，且 `do_add_entry` 把 `CE_REMOVE` 自动升级为 `CE_WT_REMOVE`，保证「索引删除」与「工作树删除」绑定。
- checkout 之所以不误伤本地修改，靠两道闸：合并阶段的 `verify_uptodate`/`verify_absent`，以及 entry 层 `checkout_entry_ca` 里 `ie_match_stat` + `!state->force` 的拒绝。
- `entry.c` 的写出不是 `cp`：读 blob → `convert_to_working_tree` 转换 → 按 mode（reg/lnk/gitlink）落盘 → `update_ce_after_write` 回填 stat，让下次 status 走快速路径。

## 7. 下一步学习建议

- **u9-l3（git status 与 wt-status）**：本讲反复提到的 `ie_match_stat`、stat 回填、racy clean，正是 `git status` 快速判定「索引 vs 工作树」的基础，下一讲会把三层对比（HEAD/索引/工作树）讲透。
- **u10-l1/u10-l2（merge）**：`unpack-trees` 的 3-way 合并回调（`threeway_merge`）与 `merge-ort` 是本讲 `twoway_merge` 的自然升级，理解了 2-way 再看 3-way 会非常顺。
- **u13-l2（并行检出与 fsmonitor）**：本讲点到的 `enqueue_checkout` / `run_parallel_checkout`（并行检出）与 stat 快速路径，在大型仓库性能优化里是主角。
- 继续阅读源码建议：精读 `unpack-trees.c` 顶部的长篇注释（解释各合并回调的设计意图），以及 `tree-walk.c` 的 `traverse_trees`，理解多路 tree 游标如何按字典序同步推进。

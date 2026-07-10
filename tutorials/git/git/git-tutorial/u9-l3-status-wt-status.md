# git status 与 wt-status

## 1. 本讲目标

`git status` 是日常用得最多的命令，但它「为什么这样输出」背后藏着一个精巧的设计：git 从来不会真的去「扫描整个仓库」，而是**做两次 diff**——把「HEAD 与索引」对比一次，再把「索引与工作树」对比一次——然后按路径把两次结果缝在一起。本讲学完后你应当能够：

1. 说清 git 的**三层数据模型**（HEAD／索引／工作树）以及 `git status` 是如何用「两次 diff」把它折叠成你看到的那三栏（`Changes to be committed` / `Changes not staged for commit` / `Untracked files`）。
2. 读懂 `wt-status.c` 的状态收集骨架：`wt_status_collect()` 依次跑 worktree、index、untracked 三个收集器，每条变更以「路径」为键合并进同一个 `struct wt_status_change_data`，同时携带 `index_status`（已暂存）与 `worktree_status`（未暂存）两个状态位。
3. 理解 `git status` 为什么在大仓库里慢——它最贵的开销是「把索引里每一条 `cache_entry` 都对工作树做一次 `lstat`」——以及 `preload-index.c` 如何用**多线程并行 lstat** 把这步摊平。

## 2. 前置知识

本讲建立在前面几讲之上，请确认你已经理解：

- **三层数据模型**（u4-l1）：工作树（working tree）是你看得见的目录；索引（index，`.git/index`）是一张「路径 → oid + stat 快照」的表；对象数据库（object database）存所有 blob/tree/commit。`HEAD` 指向当前提交，而提交的内容就是「把索引冻结成一棵 tree」。
- **diff 引擎与 diffcore**（u8-l1）：git diff 的核心是一个**回调驱动**的管线——上游喂入 `diff_filepair`，每个 pair 携带一个 `status`（`A`/`D`/`M`/`R`/`C`/`T`/`U`），下游用一个 `format_callback` 接收整条队列。本讲里 `wt-status.c` 就是把自己写成 diff 管线的一个**回调消费者**。
- **`cache_entry` 与 `ce_uptodate`**（u4-l1）：索引里每条记录都缓存了一份文件的 `stat` 快照。当工作树文件没动时，比较「索引 vs 工作树」只要 `lstat` 一下发现 stat 没变即可跳过——这正是 `ce_uptodate` 标志的意义，也是 preload-index 的用武之地。
- **命令分发**（u1-l4）：`git status` 的 C 入口是 `builtin/commit.c` 的 `cmd_status`，它复用 `wt-status.c` 的库函数。`git commit` 在弹出编辑器前也会调用同一套 `run_status()` 把状态写进提交模板。

补充一个本讲的关键直觉：

- **`git status` 不发明新算法，它只是把 diff 引擎跑两遍。**「已暂存」一栏来自 `git diff --cached`（HEAD vs 索引），「未暂存」一栏来自 `git diff`（索引 vs 工作树）。理解了这一点，status 的源码就读懂了一大半。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `wt-status.c` | `git status` 的全部逻辑：状态收集（跑两次 diff）、状态存储、长格式／短格式／porcelain／porcelain-v2 输出 |
| `wt-status.h` | 定义 `struct wt_status`（总上下文）与 `struct wt_status_change_data`（单条变更的状态聚合） |
| `builtin/commit.c` | `cmd_status` 入口，负责解析参数、读索引、刷新、然后调用 `wt_status_collect()` + `wt_status_print()` |
| `preload-index.c` | 多线程并行 `lstat` 预刷新索引，加速「索引 vs 工作树」的比较 |
| `diff.h` | 声明 `run_diff_files` / `run_diff_index`，以及 `DIFF_STATUS_*` 状态码 |
| `read-cache-ll.h` | 声明 `refresh_index`，定义刷新标志位 |

入口提示（u1-l4 已建立）：`git status` → `cmd_status`（[builtin/commit.c:1537](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/commit.c#L1537)）。

## 4. 核心概念与源码讲解

### 4.1 三层差异对比：两次 diff 折叠成三栏

#### 4.1.1 概念说明

`git status` 输出的核心是三栏（去掉分支信息行后）：

```
Changes to be committed:        # 已暂存：HEAD 与索引的差异
        modified:   foo.c

Changes not staged for commit:  # 未暂存：索引与工作树的差异
        modified:   bar.c

Untracked files:                # 未跟踪：工作树里、索引里都没有的文件
        baz.c
```

这三栏正好对应三层之间的三个「间隙」：

| 对比的两侧 | 命令行等价 | 说明 |
| --- | --- | --- |
| HEAD ↔ 索引 | `git diff --cached`（`--staged`） | 已暂存的改动，将进入下一次提交 |
| 索引 ↔ 工作树 | `git diff` | 已跟踪但未暂存的改动 |
| 工作树里索引没有的文件 | 无 diff，靠目录遍历 | 未跟踪文件 |

注意一个容易混淆的点：HEAD 与索引、索引与工作树这两次 diff，**都是针对「已跟踪文件」的**。未跟踪文件不是任何一次 diff 的产物，它需要单独遍历目录（4.3 节）。

#### 4.1.2 核心流程

把两次 diff 想象成两条独立的流，最后按「文件路径」合并：

```text
                     ┌─────────────────────────────┐
   HEAD commit ──┐   │  run_diff_index(CACHED)     │── 产生 diff_filepair 队列
                 ├──▶│  比较 HEAD.tree 与 索引      │    每项 status ∈ {A,D,M,R,C,T}
   索引(index) ──┘   └──────────────┬──────────────┘    ↓ 回调 wt_status_collect_updated_cb
                                     │                  写入 d->index_status
                                     ▼
                     ┌─────────────────────────────┐
   索引(index) ──┐   │  run_diff_files             │── 产生 diff_filepair 队列
                 ├──▶│  比较 索引 与 工作树(磁盘)    │    每项 status ∈ {A,D,M,R,C,T}
   工作树(磁盘)─┘   └──────────────┬──────────────┘    ↓ 回调 wt_status_collect_changed_cb
                                     │                  写入 d->worktree_status
                                     ▼
        两条流都以「路径 path」为键插入同一个 string_list s->change
        于是一个 wt_status_change_data 同时携带 index_status 与 worktree_status
```

关键洞察：**同一个文件可以同时出现在「已暂存」和「未暂存」两栏里。** 例如你 `git add foo.c` 后又改了 `foo.c`，那么：

- HEAD vs 索引：`foo.c` 不同 → `index_status = 'M'`（已暂存）。
- 索引 vs 工作树：`foo.c` 又不同 → `worktree_status = 'M'`（未暂存）。

于是在 `git status` 里 `foo.c` 会同时出现在两栏。这正是「按路径合并两次 diff」设计带来的自然结果。状态码的定义来自 [diff.h:674-681](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/diff.h#L674-L681)：

```c
#define DIFF_STATUS_ADDED       'A'
#define DIFF_STATUS_COPIED      'C'
#define DIFF_STATUS_DELETED     'D'
#define DIFF_STATUS_MODIFIED    'M'
#define DIFF_STATUS_RENAMED     'R'
#define DIFF_STATUS_TYPE_CHANGED 'T'
#define DIFF_STATUS_UNMERGED    'U'
```

#### 4.1.3 源码精读

先看「单条变更」的状态聚合结构 [wt-status.h:58-69](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/wt-status.h#L58-L69)：

```c
struct wt_status_change_data {
    int worktree_status;   // 索引 vs 工作树 的状态码（未暂存那一栏）
    int index_status;      // HEAD vs 索引 的状态码（已暂存那一栏）
    int stagemask;         // 合并冲突时的 stage 位掩码（见 4.2）
    int mode_head, mode_index, mode_worktree;
    struct object_id oid_head, oid_index;
    int rename_status;
    int rename_score;
    char *rename_source;
    ...
};
```

注意它**同时**有 `index_status` 和 `worktree_status` 两个字段——这就是「两次 diff 折叠」在数据结构上的落点。一个 `wt_status_change_data` 实例代表「一个路径上的全部状态」。

再看「两次 diff」是怎么各自触发的。worktree 那一栏的收集器 [wt-status.c:639-664](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/wt-status.c#L639-L664) 把自己挂成 diff 的回调，然后调用 `run_diff_files`：

```c
static void wt_status_collect_changes_worktree(struct wt_status *s)
{
    struct rev_info rev;
    repo_init_revisions(s->repo, &rev, NULL);
    setup_revisions(0, NULL, &rev, NULL);
    rev.diffopt.output_format |= DIFF_FORMAT_CALLBACK;   // 不打印，回调消费
    ...
    rev.diffopt.format_callback = wt_status_collect_changed_cb;  // 见 4.2
    rev.diffopt.format_callback_data = s;
    ...
    run_diff_files(&rev, 0);   // 索引 vs 工作树
    release_revisions(&rev);
}
```

`run_diff_files` 的声明在 [diff.h:701](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/diff.h#L701)。`DIFF_FORMAT_CALLBACK`（[diff.h:118](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/diff.h#L118)）是关键：它告诉 diff 引擎「别输出到屏幕，把配好的 `diff_filepair` 队列整条交给我注册的 `format_callback`」。这样 wt-status 就复用了整套 diffcore 配对/重命名检测能力，只接管「结果怎么用」。

index 那一栏的收集器 [wt-status.c:666-710](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/wt-status.c#L666-L710) 结构几乎一样，但用 `run_diff_index` 且默认 `def = HEAD`：

```c
static void wt_status_collect_changes_index(struct wt_status *s)
{
    ...
    opt.def = s->is_initial ? empty_tree_oid_hex(...) : s->reference;  // HEAD
    setup_revisions(0, NULL, &rev, &opt);
    ...
    rev.diffopt.format_callback = wt_status_collect_updated_cb;  // 见 4.2
    rev.diffopt.format_callback_data = s;
    ...
    run_diff_index(&rev, DIFF_INDEX_CACHED);   // HEAD vs 索引
    release_revisions(&rev);
}
```

`DIFF_INDEX_CACHED`（[diff.h:703](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/diff.h#L703)）告诉 `run_diff_index`「比较的是索引的 cached 内容，而不是工作树」——这正是 `git diff --cached` 的语义。注意初始提交（`is_initial`，即尚无 HEAD）时，比较对象换成「空树」，于是索引里所有文件都表现为「新增」。

#### 4.1.4 代码实践

> **实践目标**：亲手制造出「同一个文件同时出现在两栏」的状态，验证「两次 diff 折叠」模型。
>
> **操作步骤**（在一个临时仓库里）：
> 1. `git init demo && cd demo && echo v1 > f.txt && git add f.txt && git commit -m init`
> 2. `echo v2 >> f.txt && git add f.txt`（暂存一次修改 → `index_status='M'`）
> 3. `echo v3 >> f.txt`（不 add → `worktree_status='M'`）
> 4. `git status`
>
> **需要观察的现象**：`f.txt` 会**同时**出现在 `Changes to be committed:` 和 `Changes not staged for commit:` 两栏，且各有一次 `modified:`。
>
> **预期结果**：这正是 4.1.2 图中「两条流按路径合并」的结果——`d->index_status` 与 `d->worktree_status` 都被填成了 `'M'`。如果你用 `git diff --cached` 看到一次改动、`git diff` 又看到一次改动，就佐证了 status 把这两个 diff 拼在一起。

#### 4.1.5 小练习与答案

**练习 1**：如果只运行 `git diff`（不带参数）和 `git diff --cached`，能不能完全还原 `git status` 的「已暂存」与「未暂存」两栏？缺了什么？

**答案**：两栏内容可以还原（「已暂存」= `--cached` 的结果，「未暂存」= 无参 diff 的结果），但缺了「未跟踪文件」一栏——未跟踪文件不属于任何 diff，要靠目录遍历（4.3）。

**练习 2**：为什么初始仓库（`is_initial`）里所有文件都显示为 `new file:` 而不是 `modified:`？

**答案**：初始仓库没有 HEAD，`wt_status_collect_changes_index` 把比较对象换成空树（[wt-status.c:673](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/wt-status.c#L673)），索引里任何文件相对空树都是 `DIFF_STATUS_ADDED`，于是标签是 `new file:`。

### 4.2 wt_status 状态收集：把两条流缝进同一张表

#### 4.2.1 概念说明

知道「跑两次 diff」之后，下一个问题是：结果存到哪里、怎么用？答案是一个叫 `struct wt_status` 的总上下文，外加一个以路径为键的 `string_list`。整个收集过程可以概括为一句话：

> **两条 diff 流都把每个变更路径插入同一个 `s->change` 表；表项的 `util` 指针指向一个 `wt_status_change_data`，worktree 流填它的 `worktree_status`，index 流填它的 `index_status`。**

`struct wt_status` 是贯穿「收集 → 打印」全程的状态容器，定义在 [wt-status.h:104-148](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/wt-status.h#L104-L148)。它既装配置（`show_untracked_files`、`status_format`、`detect_rename` 等），也装收集结果（`change`、`untracked`、`ignored` 三个 `string_list`），还装派生标志（`committable`、`workdir_dirty`）。`wt_status_prepare()` 负责把它清零并设上安全默认值（[wt-status.c:144-168](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/wt-status.c#L144-L168)），其中关键默认值包括 `reference = "HEAD"`、`show_untracked_files = SHOW_NORMAL_UNTRACKED_FILES`、以及把 `s->change.strdup_strings = 1`（让 `string_list` 自己复制路径字符串）。

#### 4.2.2 核心流程

收集的顶层函数 `wt_status_collect()` 是个清晰的「三段式」([wt-status.c:863-886](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/wt-status.c#L863-L886))：

```text
wt_status_collect(s):
  1. wt_status_collect_changes_worktree(s)   # 索引 vs 工作树  → 填 worktree_status
  2. if is_initial:
         wt_status_collect_changes_initial(s) # 无 HEAD：直接遍历索引，全标 ADDED
     else:
         wt_status_collect_changes_index(s)   # HEAD vs 索引    → 填 index_status
  3. wt_status_collect_untracked(s)           # 目录遍历        → 填 s->untracked
  4. wt_status_get_state(...)                 # 探测 rebase/merge 等进行中状态
```

每一步都被 `trace2_region_enter/leave` 包裹（用于性能埋点，u13-l3 会讲），说明这三步是 status 的主要耗时来源。

**两条流如何缝合**——这是本模块的核心。worktree 流的回调 [wt-status.c:460-525](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/wt-status.c#L460-L525)：

```c
static void wt_status_collect_changed_cb(struct diff_queue_struct *q,
                                         struct diff_options *options, void *data)
{
    struct wt_status *s = data;
    ...
    s->workdir_dirty = 1;                    // 有任何工作树改动就置位
    for (i = 0; i < q->nr; i++) {
        struct diff_filepair *p = q->queue[i];
        it = string_list_insert(&s->change, p->two->path);  // 以路径为键插入
        d = it->util;
        if (!d) { CALLOC_ARRAY(d, 1); it->util = d; }       // 首次见该路径则新建
        if (!d->worktree_status)
            d->worktree_status = p->status;                  // ← 填 worktree_status
        ...
    }
}
```

index 流的回调 [wt-status.c:547-613](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/wt-status.c#L547-L613) 几乎同构，只是把状态写进 `d->index_status`，并在 `ADDED/MODIFIED/DELETED` 等情况下把 `s->committable = 1`（标记「有可提交的改动」）。注意两者都调用同一个 `string_list_insert(&s->change, path)`——**正是这个共享的键让两次 diff 在同一行 `wt_status_change_data` 上汇合**。先到的回调新建 `d`，后到的回调复用它、只填自己负责的那个字段。

**冲突文件**怎么处理？合并冲突时，索引里同一个路径会有多个带 stage（1/2/3）的 `cache_entry`（u4-l1 讲过 stage）。`wt_status_collect_updated_cb` 收到 `DIFF_STATUS_UNMERGED` 时，调用 `unmerged_mask()`（[wt-status.c:527-545](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/wt-status.c#L527-L545)）把各 stage 折叠成一个 3 位掩码 `stagemask`，存进 `d->stagemask`，供打印时区分 `both added`/`both modified` 等（4.2.4 会看到打印端如何用 `stagemask` 选 `DD`/`AA`/`UU` 等双字母码）。

#### 4.2.3 源码精读

收集完之后是打印。`wt_status_print()` 按输出格式分发（[wt-status.c:2693-2722](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/wt-status.c#L2693-L2722)）：

```c
void wt_status_print(struct wt_status *s)
{
    ...
    switch (s->status_format) {
    case STATUS_FORMAT_SHORT:       wt_shortstatus_print(s);      break;
    case STATUS_FORMAT_PORCELAIN:   wt_porcelain_print(s);       break;
    case STATUS_FORMAT_PORCELAIN_V2: wt_porcelain_v2_print(s);   break;
    ...
    case STATUS_FORMAT_LONG:        wt_longstatus_print(s);      break;
    }
}
```

长格式 `wt_longstatus_print()`（[wt-status.c:1990-2114](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/wt-status.c#L1990-L2114)）依次调用四个打印函数，每个都**遍历同一张 `s->change` 表，按字段过滤**：

```c
wt_longstatus_print_updated(s);    // "Changes to be committed"  筛 d->index_status
wt_longstatus_print_unmerged(s);   // "Unmerged paths"           筛 d->stagemask
wt_longstatus_print_changed(s);    // "Changes not staged..."    筛 d->worktree_status
...                                // "Untracked files"          来自 s->untracked
```

以「已暂存」一栏为例，`wt_longstatus_print_updated()`（[wt-status.c:924-945](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/wt-status.c#L924-L945)）：

```c
for (i = 0; i < s->change.nr; i++) {
    ...
    d = it->util;
    if (!d->index_status || d->index_status == DIFF_STATUS_UNMERGED)
        continue;                  // 只挑「有 index_status 且非冲突」的条目
    ...
    wt_longstatus_print_change_data(s, WT_STATUS_UPDATED, it);
}
```

「未暂存」一栏 `wt_longstatus_print_changed()`（[wt-status.c:976-997](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/wt-status.c#L976-L997)）则是筛 `d->worktree_status`。两者最终都落到 `wt_longstatus_print_change_data()`（[wt-status.c:368-447](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/wt-status.c#L368-L447)），由 `change_type`（`WT_STATUS_UPDATED` 或 `WT_STATUS_CHANGED`）决定取 `d->index_status` 还是 `d->worktree_status`，再用 `wt_status_diff_status_string()`（[wt-status.c:304-326](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/wt-status.c#L304-L326)）把状态码翻译成 `modified:` / `new file:` / `deleted:` 等人类可读标签：

```c
const char *wt_status_diff_status_string(int status)
{
    switch (status) {
    case DIFF_STATUS_ADDED:       return _("new file:");
    case DIFF_STATUS_DELETED:     return _("deleted:");
    case DIFF_STATUS_MODIFIED:    return _("modified:");
    case DIFF_STATUS_RENAMED:     return _("renamed:");
    ...
    }
}
```

短格式（`git status -s`）则更直接：`wt_shortstatus_status()`（[wt-status.c:2143-2175](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/wt-status.c#L2143-L2175)）把 `d->index_status` 与 `d->worktree_status` 各打成一个字符，拼成经典的两列 `XY path`（如 `MM f.txt` 表示既已暂存又未暂存），未跟踪文件打 `??`。短格式下的冲突码在 `wt_shortstatus_unmerged()`（[wt-status.c:2116-2141](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/wt-status.c#L2116-L2141)）里由 `stagemask` 映射出 `DD`/`AU`/`UD`/`UA`/`DU`/`AA`/`UU` 七种双字母码。

最后看入口怎么把收集与打印串起来。`cmd_status()`（[builtin/commit.c:1537](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/commit.c#L1537)）在读索引并 `refresh_index` 之后，干的就是这两行（[builtin/commit.c:1655-1664](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/commit.c#L1655-L1664)）：

```c
wt_status_collect(&s);          // 跑两次 diff + 遍历未跟踪
...
wt_status_print(&s);            // 按 status_format 输出
wt_status_collect_free_buffers(&s);
```

`git commit` 弹编辑器前调用的 `run_status()`（[builtin/commit.c:563-590](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/commit.c#L563-L590)）走的是完全一样的 `collect → print → free` 三步，只是把输出重定向到提交模板文件，并返回 `s->committable` 用于判断是否真的有东西可提交。

#### 4.2.4 代码实践

> **实践目标**：制造 staged / unstaged / untracked 三类变更，对照源码说明每类被哪个回调、哪个字段识别。
>
> **操作步骤**：
> 1. 准备仓库：`git init s && cd s && git commit --allow-empty -m init`
> 2. **staged**：`echo a > staged.txt && git add staged.txt`（新增并暂存）
> 3. **unstaged**：`echo 1 > u.txt && git add u.txt && git commit -m x && echo 2 >> u.txt`（已跟踪文件改了不 add）
> 4. **untracked**：`echo b > new.txt`（不 add）
> 5. 运行 `git status`，再运行 `git status -s`。
>
> **需要观察的现象**：长格式里 `staged.txt` 在 `Changes to be committed`（`new file:`）、`u.txt` 在 `Changes not staged`（`modified:`）、`new.txt` 在 `Untracked files`。短格式里分别是 `A  staged.txt`、` M u.txt`、`?? new.txt`。
>
> **预期结果 / 源码对照**：
> - `staged.txt` 的 `d->index_status='A'`、`d->worktree_status=0` → 由 `wt_status_collect_updated_cb` 写入（[wt-status.c:547](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/wt-status.c#L547)），只进 `wt_longstatus_print_updated` 那一栏。
> - `u.txt` 的 `d->worktree_status='M'`、`d->index_status=0` → 由 `wt_status_collect_changed_cb` 写入（[wt-status.c:460](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/wt-status.c#L460)），只进 `wt_longstatus_print_changed` 那一栏。
> - `new.txt` 不在任何 diff 里 → 由 `wt_status_collect_untracked` 遍历目录得到，进 `s->untracked`（[wt-status.c:806](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/wt-status.c#L806)）。

#### 4.2.5 小练习与答案

**练习 1**：`wt_status_collect_changed_cb` 和 `wt_status_collect_updated_cb` 都用 `string_list_insert(&s->change, path)` 插入。如果 worktree 流先跑、index 流后跑，index 流第二次见到同一路径时会怎样？

**答案**：`it->util` 已经非空（worktree 流建好的 `d`），所以 index 流**不会**新建 `d`，只执行 `if (!d->index_status) d->index_status = p->status;` 在同一个 `d` 上补字段。这就是「按路径缝合」。

**练习 2**：`git status -s` 输出 `RM foo.c`（第一列 `R`、第二列 `M`）表示什么？

**答案**：第一列是 `index_status`（已暂存为重命名 `R`），第二列是 `worktree_status`（未暂存为修改 `M`）。即该文件相对 HEAD 被重命名并已暂存，但重命名后的工作树副本又被进一步改动未暂存。这两列分别由 `wt_shortstatus_status` 打印（[wt-status.c:2148-2155](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/wt-status.c#L2148-L2155)）。

### 4.3 预加载索引加速：多线程 lstat

#### 4.3.1 概念说明

回到「索引 vs 工作树」这次 diff。它要把索引里**每一条** `cache_entry` 都对磁盘上的真实文件做一次 `lstat`（取 inode/mtime/size 等元数据），再与缓存的 `ce_stat_data` 比对，才能判断「这个文件改没改」。对一个有几十万条目的巨型仓库，这就是**几十万次系统调用**，是 `git status` 在大仓库里慢的首要原因。

`preload-index.c` 的思路很直接：**`lstat` 之间互不依赖，完全可以并行**。于是把整个 `cache[]` 数组切成若干段，每段交给一个线程去 `lstat`，谁发现「stat 没变」就把对应 `cache_entry` 标记成 `ce_uptodate`（u4-l1 讲过的「索引条目已知是最新的」标志）。等真正的 diff 跑起来时，`run_diff_files` 看到 `ce_uptodate` 就直接跳过，不再重复 `lstat`。

这背后的关键判等函数是 `ie_match_stat()`：它比较 `cache_entry` 的 stat 快照与刚取到的 `lstat` 结果，若匹配（并处理了 u4-l1 提到的「racy clean」边界）就判定文件未变。preload 线程在循环里正是用它来决定要不要标 `ce_uptodate`。

#### 4.3.2 核心流程

`preload_index()` 的骨架（[preload-index.c:106-179](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/preload-index.c#L106-L179)）：

```text
preload_index(index, pathspec, refresh_flags):
  若没有线程支持 或 core.preloadindex=false → 直接返回（不并行）
  threads = cache_nr / 500            # 每 500 条目才值得开一个线程
  若 threads < 2 → 返回（太少不值得）
  threads = min(threads, MAX_PARALLEL=20)
  work = ceil(cache_nr / threads)     # 每个线程负责 work 条
  for i in 0..threads:
      data[i].offset = i*work; data[i].nr = work
      pthread_create(preload_thread, &data[i])   # 各跑各的
  for i: pthread_join(data[i])                   # 等全部完成
```

每个线程的循环体 `preload_thread()`（[preload-index.c:47-104](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/preload-index.c#L47-L104)）对自己那一段 `cache[]` 逐条处理，关键在**先跳过、后 lstat**：

```text
对每条 ce（cache_entry）:
  跳过：有 stage（冲突条目）/ gitlink（子模块）/ 已 ce_uptodate /
        CE_SKIP_WORKTREE（稀疏检出，u4-l3）/ CE_FSMONITOR_VALID（fsmonitor 已背书，u13-l2）
  跳过：不匹配 pathspec
  跳过：路径是符号链接前缀（threaded_has_symlink_leading_path，省一次无谓 lstat）
  lstat(ce->name, &st)               # ← 真正的系统调用
  若 ie_match_stat(...) 判定未变:
      ce_mark_uptodate(ce)            # 标记，后续 diff 直接跳过
      mark_fsmonitor_valid(index, ce)
```

几个值得记的常量在 [preload-index.c:29-30](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/preload-index.c#L29-L30)：`MAX_PARALLEL = 20`（线程数上限）、`THREAD_COST = 500`（每线程至少 500 次 lstat 才回本）。这两个「魔数」是 Linus 在 2008 年加入该机制时凭经验定的（文件头版权注释也署了他的名字）。

#### 4.3.3 源码精读

`preload_index` 的开关与分片逻辑（[preload-index.c:106-133](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/preload-index.c#L106-L133)）：

```c
void preload_index(struct index_state *index, const struct pathspec *pathspec,
                   unsigned int refresh_flags)
{
    int threads, i, work, offset;
    ...
    repo_config_get_bool(index->repo, "core.preloadindex", &core_preload_index);
    if (!HAVE_THREADS || !core_preload_index)
        return;                       // 线程支持或开关任一缺失则不并行
    threads = index->cache_nr / THREAD_COST;   // 每 500 条目一个线程
    ...
    if (threads < 2)
        return;                       // 太少不值得开线程
    if (threads > MAX_PARALLEL)
        threads = MAX_PARALLEL;       // 上限 20
    work = DIV_ROUND_UP(index->cache_nr, threads);   // 每线程负责的条数
    ...
}
```

线程主循环里的跳过条件与 lstat（[preload-index.c:60-93](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/preload-index.c#L60-L93)）：

```c
do {
    struct cache_entry *ce = *cep++;
    struct stat st;
    if (ce_stage(ce))            continue;   // 冲突条目不碰
    if (S_ISGITLINK(ce->ce_mode)) continue;  // 子模块交给专门逻辑
    if (ce_uptodate(ce))         continue;   // 已知最新
    if (ce_skip_worktree(ce))    continue;   // 稀疏检出跳过
    if (ce->ce_flags & CE_FSMONITOR_VALID) continue;  // fsmonitor 已背书
    ...
    if (!ce_path_match(index, ce, &p->pathspec, NULL)) continue;  // 不在 pathspec 内
    if (threaded_has_symlink_leading_path(&cache, ce->name, ce_namelen(ce)))
        continue;                            // 路径整段是符号链接，跳过省 lstat
    p->t2_nr_lstat++;
    if (lstat(ce->name, &st))    continue;   // ← 真正的系统调用
    if (ie_match_stat(index, ce, &st, CE_MATCH_RACY_IS_DIRTY|CE_MATCH_IGNORE_FSMONITOR))
        continue;                            // stat 不匹配，说明改动了，留给 diff 处理
    ce_mark_uptodate(ce);                    // stat 匹配 → 标记已知最新
    mark_fsmonitor_valid(index, ce);
} while (--nr > 0);
```

注意一个反直觉的细节：`ie_match_stat` 返回**非零**时反而 `continue`（不标记）。原因是 `ie_match_stat` 返回 0 表示「完全匹配、文件未变」，此时才值得标 `ce_uptodate`；若返回非零则说明 stat 已变化，文件确实被改过，那种情况**故意留给**后续 `run_diff_files` 去做内容级比对，preload 只负责「快速排除掉没变的绝大多数」。

对外的封装是 `repo_read_index_preload()`（[preload-index.c:181-189](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/preload-index.c#L181-L189)）：先 `repo_read_index` 读入索引，紧接着 `preload_index` 并行预刷新。`git commit` 的 `prepare_index()` 路径正是用它（[builtin/commit.c:392](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/commit.c#L392)），这样随后 `run_status()` 里那次「索引 vs 工作树」diff 就能大量命中 `ce_uptodate` 而跳过 lstat。`cmd_status` 自身则走 `repo_read_index` + `refresh_index`（[builtin/commit.c:1629-1632](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/commit.c#L1629-L1632)，`refresh_index` 声明于 [read-cache-ll.h:455](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache-ll.h#L455)）——两者都是「在真正 diff 之前先把索引条目的 stat 刷新到最新」这一加速思路的不同入口。

#### 4.3.4 代码实践

> **实践目标**：观察 preload-index 的线程数决策与开关效果。
>
> **操作步骤**（待本地验证，因为行为依赖机器核数与仓库规模）：
> 1. 在一个较大的仓库（例如 git 自身的源码树，约数千条目）里运行：
>    `GIT_TRACE2_PERF=1 git status`，在输出中查找 `region_enter`/`region_leave` 含 `"index","preload"` 的事件，以及 `preload/sum_lstat` 数据项。
> 2. 关闭开关对比：`git -c core.preloadindex=false GIT_TRACE2_PERF=1 git status`，观察是否还有 preload region。
>
> **需要观察的现象**：开启时 trace2 里有 `preload` region 且记录了总 lstat 次数；关闭时该 region 消失，整体 `status` region 耗时通常变大。
>
> **预期结果**：`preload_index` 的 `core.preloadindex` 为 false 时会在 [preload-index.c:118](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/preload-index.c#L118) 直接 `return`，不创建任何线程。
>
> ⚠️ 注意：在小仓库里 `threads = cache_nr/500 < 2`，会在 [preload-index.c:124](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/preload-index.c#L124) 提前返回——此时无论开关与否都看不到多线程。要观察并行效果需用一个条目数足够多的仓库，或设置测试变量 `GIT_TEST_PRELOAD_INDEX=1` 强制至少 2 个线程（[preload-index.c:122](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/preload-index.c#L122)）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 preload 线程要跳过 `CE_FSMONITOR_VALID` 和 `CE_SKIP_WORKTREE` 的条目？

**答案**：`CE_FSMONITOR_VALID` 表示 fsmonitor（u13-l2）已确认该文件自上次以来未变，无需再 lstat；`CE_SKIP_WORKTREE` 表示该路径在稀疏检出（u4-l3）下根本不在工作树里，磁盘上没有文件可 lstat。两者都已「必然未变」，再 lstat 是浪费。

**练习 2**：`preload_index` 为什么要有 `THREAD_COST=500` 这个阈值，而不是「条目越多线程越多」直接成正比？

**答案**：开线程本身有创建/同步成本（`pthread_create`、`pthread_join`、pathspec 深拷贝）。若每个线程只处理很少条目，并行收益抵不过线程开销，反而更慢。500 是经验值，保证「至少有足够 lstat 量来摊销线程成本」。

## 5. 综合实践

把本讲三个模块串起来，完成一次「源码阅读 + 行为验证」的综合任务：

1. **准备一个四种状态齐全的仓库**：
   ```bash
   git init zx && cd zx
   git commit --allow-empty -m init
   echo A > a.txt && git add a.txt                      # staged: new file
   echo B > b.txt && git add b.txt && git commit -m x
   echo B2 >> b.txt                                     # unstaged: modified
   echo C > c.txt                                       # untracked
   ```
2. **用 porcelain v2 验证数据模型**：运行 `git status --porcelain=v2`。你会看到每行以 `1`/`2`/`u`/`?` 开头，分别对应普通变更/重命名或拷贝/冲突/未跟踪；普通变更行的字段是 `1 <XY> <sub> <mH> <mI> <mW> <hH> <hI> <path>`，其中 `XY` 正是 `index_status`/`worktree_status` 两列，`<mH>/<mI>/<mW>` 正是 `wt_status_change_data` 里的 `mode_head/mode_index/mode_worktree`，`<hH>/<hI>` 是 `oid_head/oid_index`。把这些字段与 [wt-status.h:58-69](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/wt-status.h#L58-L69) 的结构体成员一一对照。
3. **跟踪收集顺序**：打开 `wt-status.c` 的 `wt_status_collect()`（[wt-status.c:863](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/wt-status.c#L863)），按 worktree → index → untracked 的顺序，分别指出 `b.txt` 的 `worktree_status`、`a.txt` 的 `index_status`、`c.txt` 进 `s->untracked` 分别由哪个函数写入。
4. **观察预加载**：`GIT_TRACE2_PERF=1 git status`，在输出里找到 `index/preload` region 与 `status/worktrees`、`status/index`、`status/untracked`、`status/print` 几个 region，体会「预刷新索引」与「真正收集」的时间分布。

> 如果无法运行（环境无编译产物），以上命令标注为「待本地验证」；第 2、3 步的源码对照部分不依赖运行，可直接完成。

## 6. 本讲小结

- `git status` 的本质是**两次 diff**：「HEAD vs 索引」产出「已暂存」一栏，「索引 vs 工作树」产出「未暂存」一栏，未跟踪文件则靠目录遍历单独收集。
- 两条 diff 流都以**路径为键**插入同一个 `string_list s->change`，于是同一文件可同时出现在两栏——其根源是 `wt_status_change_data` 同时持有 `index_status` 与 `worktree_status` 两个字段。
- 收集骨架 `wt_status_collect()` 是 worktree → index（或 initial）→ untracked 三段式，每段都用 `DIFF_FORMAT_CALLBACK` 把自己挂成 diff 管线的回调消费者，复用整套 diffcore 配对/重命名能力。
- 长格式 `wt_longstatus_print()` 遍历**同一张** `s->change` 表四次，分别按 `index_status`/`stagemask`/`worktree_status` 过滤出「已暂存/冲突/未暂存」三栏；短格式则把两个字段压成 `XY` 两列。
- 状态码（`A/D/M/R/C/T/U`）经 `wt_status_diff_status_string()` 翻译成 `new file:`/`modified:` 等标签；冲突的 stage 掩码 `stagemask` 映射成 `DD/AA/UU` 等双字母码。
- 性能瓶颈在「索引 vs 工作树」那次 diff 要对每条 `cache_entry` 做 `lstat`；`preload-index.c` 用多线程并行 lstat 预先标记 `ce_uptodate`，让真正 diff 跳过未变文件，受 `core.preloadindex` 开关、`MAX_PARALLEL=20`、`THREAD_COST=500` 三者节制。

## 7. 下一步学习建议

- **`git commit` 的完整链路**：本讲只看了 `cmd_status` 与 `run_status`。下一步可读 `builtin/commit.c` 的 `cmd_commit` 与 `prepare_index`，看「收集状态 → 生成提交模板 → 写出 commit 对象」如何衔接（对应大纲 u9-l4）。
- **diff 引擎内部**：本讲把 diff 当黑盒（只用回调消费结果）。要理解 `run_diff_files`/`run_diff_index` 内部如何 `lstat`、如何配对，应回到 u8-l1（diffcore 管线）。
- **更大规模的状态加速**：preload-index 是「多线程 lstat」层面的优化；要了解「减少 lstat 总量」层面的优化（fsmonitor 让操作系统告诉 git 哪些文件变过），以及「未跟踪文件枚举」的 `untracked cache`，可进入 u13-l2（位图、fsmonitor 与并行检出）。
- **冲突状态深入**：本讲的 `stagemask` 只是入口。要理解 stage 1/2/3 的来源与三方合并如何产生它们，应阅读 u10-l2（unpack-trees 三方合并与冲突）。

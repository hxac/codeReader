# revision.c 遍历机制

## 1. 本讲目标

本讲是「版本遍历 revision walk」单元的第一讲。git 里凡是「按历史顺序罗列提交」的命令——`git log`、`git rev-list`、`git shortlog`、`git format-patch`、`git blame` 的底层——都共享同一套遍历引擎，它就住在 `revision.c`。学完本讲你应当能够：

- 理解「遍历的起点与终点」是如何表达的：用户给出的提交里，有的叫 **positive**（要展示），有的叫 **negative**（标记为 `UNINTERESTING`，作裁剪边界）。
- 看懂这套引擎的**标准调用四步曲**：`repo_init_revisions` → `setup_revisions` → `prepare_revision_walk` → 反复 `get_revision`。
- 掌握**优先队列驱动的提交遍历**：git 用一个小顶堆（二叉堆）按提交时间（或拓扑代数 generation number）从新到旧不断「弹出一个、压入它的父亲」，从而做到**边走边发现**，而不必先把整棵历史树读进内存。
- 区分 git 的两种遍历策略：**懒惰式 streaming**（默认，靠 `prio_queue` 即时排序）与**预先式 limited**（当需要路径过滤等复杂裁剪时，先用 `limit_list` 把整段历史吃下来再回放）。
- 读懂优先队列 `prio_queue` 这一通用数据结构本身：它既能当优先队列，也能当 LIFO 栈，是 git 里极精炼的一段代码。

本讲只讲**遍历主框架**；上层的 diff 输出、pretty 格式化（u9-l4）、历史简化（`simplify_commit`/`try_to_simplify_commit`）只点到为止。`commit-graph` 对遍历的加速留给 u7-l2。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**遍历的本质是在一张有向无环图（DAG）上做搜索。** 在 u3-l1 我们学到，每个 `commit` 对象都持有一个 `tree`（这次快照）和若干 `parent`（它的父提交）。把所有 commit 按 parent 边连起来，就是一张从新到旧的 DAG。`git log` 要做的事，就是从某些「起点 commit」出发，沿着 parent 边往回走，把能到达的 commit 按某种顺序（默认按时间从新到旧）逐一展示。

**起点 positive 与终点 negative。** `git log A` 表示「从 A 往回走，全要」——A 是 positive。而 `git log A..B`（即 `^A B`）表示「从 B 往回走，但走到 A 或 A 能到达的提交就停」——B 是 positive，A 前面加 `^` 表示 negative。git 的实现很朴素：给 negative 提交打上 `UNINTERESTING` 标志，遍历时一旦发现某个提交「不可达任何 positive，或只可达 negative」，就裁掉它。裁剪是会**传播**的：一个提交被标记为 `UNINTERESTING`，它的所有祖先也会被传染成 `UNINTERESTING`。

**为什么需要优先队列？** 想让输出「按时间从新到旧」，最朴素的做法是先收集全部 commit 再排序——但大型仓库可能有上百万 commit，全读进内存既慢又费。git 的做法是：维护一个**小顶堆**，里面放着「已发现、尚未输出」的 commit，比较键就是提交时间。每次弹出堆顶（当前最新的一个），输出它，再把它的父亲塞进堆里。因为新提交的父亲时间一定不晚于自己，整个序列自然保持时间有序，而且**只用了与「前沿宽度」成正比的内存**。这个堆就是本讲的 `prio_queue`。

> 术语速查：positive/negative（正/负端点）、`UNINTERESTING`（无趣，裁剪标志）、`SEEN`（已入队，避免重复处理）、`ADDED`（父亲已展开）、walk mode（遍历模式）、generation number（提交代数，commit-graph 提供的拓扑层数，越大越新）、`prio_queue`（优先队列，二叉堆）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [revision.h](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/revision.h) | 遍历引擎的**公共头文件**。定义核心结构 `struct rev_info`、全部对象标志位（`SEEN`/`UNINTERESTING`/…）、`enum commit_action`，以及四步曲的函数声明。 |
| [revision.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/revision.c) | 遍历引擎**实现主体**。包含 `repo_init_revisions`、`setup_revisions`、`prepare_revision_walk`、`get_revision`、`limit_list`、`process_parents`、`mark_parents_uninteresting`，以及拓扑遍历 `init_topo_walk` 等。 |
| [builtin/rev-list.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/rev-list.c) | `git rev-list` 子命令实现，是遍历引擎最直接的**调用范例**：在这里能看到 `repo_init_revisions → setup_revisions → prepare_revision_walk → traverse_commit_list_filtered` 的完整骨架。 |
| [prio-queue.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/prio-queue.c) / [prio-queue.h](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/prio-queue.h) | 通用**优先队列**（二叉堆）实现，遍历、拓扑排序、合并基计算都靠它。约 120 行，是 git 里值得逐行读的小数据结构。 |
| [commit.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/commit.c) | 提供 `prio_queue` 的比较函数 `compare_commits_by_commit_date` 与 `compare_commits_by_gen_then_commit_date`，定义了「从新到旧」这个顺序到底比的是什么。 |
| [list-objects.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/list-objects.c) | 把 `get_revision` 循环包成 `traverse_commit_list`，是 `git rev-list`/`git pack-objects` 共用的「遍历并产出对象」的高层入口。 |

## 4. 核心概念与源码讲解

### 4.1 struct rev_info 配置

#### 4.1.1 概念说明

`struct rev_info` 是整个遍历的**上下文容器**：一次 `git log` 从头到尾，几乎所有状态都挂在这一个结构体上——起点提交、命令行参数解析结果、各种开关（要不要 diff、要不要拓扑序、要不要路径过滤……）、运行时的待办队列、计数器。

`revision.h` 顶部的一段注释给出了使用约定（**调用顺序**）：

```c
/**
 * The revision walking API offers functions to build a list of revisions
 * and then iterate over that list.
 *
 * Calling sequence
 * ----------------
 *  first you need to initialize a rev_info structure, then add revisions
 *  to control what kind of revision list do you want to get, finally you
 *  can iterate over the revision list.
 */
```

> 见 [revision.h:18-29](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/revision.h#L18-L29)：这里规定了「先初始化、再加端点、最后迭代」的三段式契约。

对应到三步函数：

1. `repo_init_revisions(r, &revs, prefix)` —— 把 `revs` 填成一组**安全默认值**。
2. `setup_revisions(argc, argv, &revs, opt)` —— 吃掉命令行参数，把 `HEAD`、`^A`、`--since`、`-- <path>` 等翻译成 `revs` 里的字段与起点提交。
3. `prepare_revision_walk(&revs)` —— 把起点提交装进待办队列、做必要的预遍历。
4. 之后反复 `get_revision(&revs)`，每次返回一个 `struct commit *`，返回 `NULL` 表示结束。

承接 u3-l1：起点提交就是 `struct commit`（其首成员是 `struct object`，故可被打标志位）。承接 u5-l1：命令行上的分支名 / 标签名先经引用解析成 oid，再 `add_pending_object` 进 `revs.pending`，最后在 `prepare_revision_walk` 里转成 commit。

#### 4.1.2 核心流程

把 `struct rev_info` 想成一台「配置好了、还没开机的遍历机」。开机流程：

```text
   repo_init_revisions()          # 装填默认值（commit_queue 的比较函数也在此时设好）
            │
            ▼
   setup_revisions(argc, argv)    # 解析参数：
            │                       #   HEAD / A..B  → 加进 pending（positive）
            │                       #   ^A           → 加进 pending 并打 UNINTERESTING（negative）
            │                       #   --since / -n / -- <path> / --topo-order … → 置位对应 flag
            ▼
   prepare_revision_walk()        # pending → commits 链表
            │                       #   按“是否需要预遍历”选择策略（见 4.2）
            ▼
   while ((c = get_revision()))   # 每次弹出一个 commit，直到 NULL
            │
            ▼
        输出 / 处理 c
```

**positive 与 negative 如何落地成标志位。** 关键在 [revision.h:31-45](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/revision.h#L31-L45) 定义的一组对象标志位。注意注释里的提醒「Remember to update object flag allocation in object.h」——这些位是**全局共享**的（u3-l1 讲过 29 位 flags 是操作级临时资源）：

| 标志位 | 含义 |
| --- | --- |
| `SEEN` (1u<<0) | 这个对象已进入待办队列/已被处理过，防止重复入队 |
| `UNINTERESTING` (1u<<1) | negative 端点及其祖先；遍历结果要**裁掉** |
| `TREESAME` (1u<<2) | 相对某父亲，此提交的 tree 没变化（历史简化用） |
| `SHOWN` (1u<<3) | 已作为结果返回过 |
| `ADDED` (1u<<7) | 此 commit 的父亲已被展开并入队 |
| `BOTTOM` (1u<<10) | 反向遍历的底端 |

裁剪的传播靠 `mark_parents_uninteresting`：一旦判定一个提交无趣，就**递归**把它的所有祖先都染成 `UNINTERESTING`。

#### 4.1.3 源码精读

先看 `struct rev_info` 最核心的几个字段（完整定义在 [revision.h:126-397](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/revision.h#L126-L397)）：

```c
struct rev_info {
	/*
	 * Work queue of commits, stored as either a linked list or a
	 * priority queue, but never both at the same time.
	 * rev_info_commit_list_to_queue() converts list to queue.
	 */
	struct commit_list *commits;        /* 待办：链表形式（limited/no_walk 模式用） */
	struct prio_queue commit_queue;     /* 待办：优先队列形式（streaming 模式用） */

	struct object_array pending;        /* setup_revisions 攒下的“原始端点”，待转成 commits */
	struct repository *repo;
	...
	/* topo-sort */
	enum rev_sort_order sort_order;     /* IN_GRAPH / BY_COMMIT_DATE / BY_AUTHOR_DATE */
	...
	unsigned int ... prune:1,           /* 启用路径过滤 */
			no_walk:1, ... topo_order:1, simplify_merges:1,
			limited:1, ...                /* limited=1 表示需要预先 limit_list() */
	...
	timestamp_t max_age;                /* --since：只展示不早于此时间的提交 */
	timestamp_t min_age;                /* --until */
	int min_parents, max_parents;
	...
	struct diff_options diffopt;        /* 路径过滤与 diff 都走它 */
};
```

> 见 [revision.h:126-148](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/revision.h#L126-L148)（`commits`/`commit_queue`/`pending`）、[revision.h:171-242](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/revision.h#L171-L242)（`sort_order` 与一大组 `:1` 标志位）、[revision.h:318-327](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/revision.h#L318-L327)（`max_age`/`min_age` 等限额）。注释特别强调 `commits` 与 `commit_queue` **二选一**，这正是 4.2 要讲的「链表 vs 优先队列」两种待办容器。

`rev_info` 不能简单 `memset` 成零再用，因为有些默认值不是 0。git 提供了一个宏 `REV_INFO_INIT` 来表达这些非零默认，再用 `repo_init_revisions` 一次性拷进结构体：

```c
#define REV_INFO_INIT { \
	.commit_queue = { .compare = compare_commits_by_commit_date }, \
	.abbrev = DEFAULT_ABBREV, \
	.simplify_history = 1, \
	.pruning.flags.recursive = 1, \
	.pruning.flags.quick = 1, \
	.sort_order = REV_SORT_IN_GRAPH_ORDER, \
	.dense = 1, \
	.max_age = -1, ... \
}
```

> 见 [revision.h:416-434](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/revision.h#L416-L434)。注意第一行：**默认就把 `commit_queue` 的比较函数设成了「按提交日期」**——这就是为什么默认 `git log` 是按时间倒序的根源。`max_age = -1` 表示「未设上限」。

`repo_init_revisions` 本体非常薄，主体就是把 `REV_INFO_INIT` 这份「空白样板」拷过去，再补上几个需要 `repo` 上下文的字段（grep 过滤器、diff 选项、notes 等）：

```c
void repo_init_revisions(struct repository *r, struct rev_info *revs, const char *prefix)
{
	struct rev_info blank = REV_INFO_INIT;
	memcpy(revs, &blank, sizeof(*revs));

	revs->repo = r;
	revs->pruning.repo = r;
	revs->pruning.add_remove = file_add_remove;
	revs->pruning.change = file_change;
	revs->pruning.change_fn_data = revs;
	revs->prefix = prefix;

	grep_init(&revs->grep_filter, revs->repo);
	revs->grep_filter.status_only = 1;

	repo_diff_setup(revs->repo, &revs->diffopt);
	...
}
```

> 见 [revision.c:1935-1962](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/revision.c#L1935-L1962)。`pruning.add_remove`/`change` 是路径过滤时比较两棵 tree 用的回调，本讲不展开。

裁剪传播函数 `mark_parents_uninteresting` 很短，但体现了 git 遍历的一个常驻套路——用一个**显式栈**（`commit_stack`）替代递归，避免深历史递归爆栈：

```c
void mark_parents_uninteresting(struct rev_info *revs, struct commit *commit)
{
	struct commit_stack pending = COMMIT_STACK_INIT;
	struct commit_list *l;

	for (l = commit->parents; l; l = l->next) {
		mark_one_parent_uninteresting(revs, l->item, &pending);
		if (revs && revs->exclude_first_parent_only)
			break;
	}

	while (pending.nr > 0)
		mark_one_parent_uninteresting(revs, commit_stack_pop(&pending), &pending);

	commit_stack_clear(&pending);
}
```

> 见 [revision.c:278-294](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/revision.c#L278-L294)。先把直接父亲压栈，再循环弹栈、把每个被染无趣的提交的祖父继续压栈——于是整条祖先链都被染成 `UNINTERESTING`。

#### 4.1.4 代码实践

**目标**：亲眼看到 positive/negative 如何变成标志位。

**步骤**：

1. 在任意 git 仓库里制造一条分叉历史，便于看到 `A..B` 的裁剪：

```bash
git init revwalk-demo && cd revwalk-demo
git commit --allow-empty -m base
git commit --allow-empty -m "on main A"
git checkout -b topic HEAD~1        # 从 base 开分支
git commit --allow-empty -m "on topic"
git checkout master 2>/dev/null || git checkout main
git merge --no-ff topic -m "merge topic"
```

2. 用 plumbing 命令观察标志位。`git rev-list` 可以用 `--children` 之外的方式看端点标记，但最直观的是带 `^`：

```bash
git rev-list HEAD                # positive：从 HEAD 往回全部输出
git rev-list topic..HEAD         # ^topic HEAD：只输出“HEAD 能到、topic 不能到”的提交
git rev-list --boundary HEAD~3.. # 配合 --boundary 可在输出里看到裁剪边界（标记为 -）
```

3. 阅读源码：在 [revision.c:1935](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/revision.c#L1935) 的 `repo_init_revisions` 确认 `commit_queue.compare` 默认值来自 `REV_INFO_INIT`；再到 [revision.c:278](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/revision.c#L278) 的 `mark_parents_uninteresting` 理解 negative 的 `UNINTERESTING` 如何传播。

**观察现象**：`topic..HEAD` 只列出合并提交本身（以及 main 上 topic 没有的部分），topic 分支上的提交不会出现——它们被 `^topic` 染成 `UNINTERESTING` 并传染裁掉了。

**预期结果**：`git rev-list HEAD` 列出 4 个左右提交；`git rev-list topic..HEAD` 只列出 main 侧独有的提交。具体哈希因仓库而异，重点是**条数变少**。

> 如本地无法构造上述历史，待本地验证输出条数。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `repo_init_revisions` 不直接 `memset(revs, 0, sizeof(*revs))`？

**答案**：因为多个字段的安全默认值**不是零**——例如 `commit_queue.compare` 必须是 `compare_commits_by_commit_date`（否则默认 `git log` 不会按时间排序）、`max_age`/`skip_count`/`max_count` 是 `-1` 表示「未设置」、`simplify_history = 1`、`dense = 1`。零值会让这些开关进入错误的「禁用/无限」语义，所以必须用 `REV_INFO_INIT` 这份样板。

**练习 2**：`SEEN` 和 `SHOWN` 有什么区别？

**答案**：`SEEN` 表示「已进入待办处理流程」（防止同一个 commit 被多次入队、多次展开父亲），在遍历**早期**被打上；`SHOWN` 表示「已经作为最终结果返回给调用方」，在 `get_revision_internal` 返回前打上（见 [revision.c:4555-4556](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/revision.c#L4555-L4556)）。一个 commit 可以 `SEEN` 但最终因 `UNINTERESTING`/`TREESAME` 被 `commit_ignore` 而从未 `SHOWN`。

### 4.2 提交遍历主循环

#### 4.2.1 概念说明

`get_revision()` 是遍历的对外迭代器：调用一次返回一个 commit，返回 `NULL` 表示结束。但「下一个该返回谁」背后有**五种遍历模式**，由 `get_walk_mode` 当场判定。理解这五种模式，就理解了 git 遍历的全貌：

```c
enum rev_walk_mode {
	REV_WALK_REFLOG,     /* git log -g：按 reflog 顺序，不走 parent 边 */
	REV_WALK_TOPO,       /* --topo-order/--graph：保证拓扑合法，用 generation number */
	REV_WALK_LIMITED,    /* 需要路径过滤等复杂裁剪：先 limit_list() 吃完整段历史 */
	REV_WALK_NO_WALK,    /* --no-walk：只展示给定端点，不往父辈走 */
	REV_WALK_STREAMING,  /* 默认：边走边按时间排序，靠 prio_queue 即时输出 */
};
```

> 见 [revision.c:4356-4362](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/revision.c#L4356-L4362)。

本讲聚焦最常见的两种：**streaming**（默认的懒惰遍历）与 **limited**（路径过滤触发的预先遍历）。它们的根本差异在于「何时把祖先读进来」：

- **streaming**：不预先读祖先。每次从优先队列弹出当前最新 commit，输出后再把它的父亲塞进队列。内存占用 ≈ 历史的「前沿宽度」，非常适合线性或窄分叉历史下的 `git log HEAD`。
- **limited**：当命令带有路径过滤（`git log -- path`）、`--children`、`--simplify-merges` 等，**必须**先跑一遍 `limit_list` 把整段（或足够深）历史读进来，因为「这个 commit 相对该路径是否无趣」要等到它的父亲被处理后才能确定。此时设 `revs->limited = 1`，`prepare_revision_walk` 就会调 `limit_list`。

#### 4.2.2 核心流程

`get_revision` 的调用链其实有三层包装，每层各管一件事：

```text
get_revision()                 # 最外层：处理 --reverse（反转整段输出）、--boundary 等
   │
   ├─(reverse 时) 先把全部结果收进链表再倒序回放
   │
   └─ get_revision_internal()  # 中层：处理 max_count（-n）、skip、boundary 边界提交
         │
         └─ get_revision_1()   # 内层：真正的“取下一个”核心，按 walk_mode 分派
               │  从容器（链表 / prio_queue / topo_queue）取出一个 commit
               │  做 max_age 时间下限裁剪
               │  调 process_parents() 展开父亲并入队
               │  调 simplify_commit() 决定 show / ignore / error
               └ 返回 commit（或 continue 继续取下一个）
```

`get_walk_mode` 的判定顺序也很讲究（见 [revision.c:4364-4375](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/revision.c#L4364-L4375)）：reflog 优先，其次拓扑（`topo_walk_info` 非空），再是 limited，再是 no_walk，最后兜底 streaming。注意 **streaming 是「没有特殊要求」时的默认**——这正是 git 日常 `git log` 又快又省内存的原因。

streaming 模式下，`revs->commits`（链表）会在第一次调用时被一次性「倒进」`commit_queue`（优先队列），之后就只用队列了：

```c
if (mode == REV_WALK_STREAMING && revs->commits)
    rev_info_commit_list_to_queue(revs);
```

#### 4.2.3 源码精读

**`prepare_revision_walk`：把端点变成可迭代状态。** 它是 `setup_revisions` 与 `get_revision` 之间的桥梁。关键逻辑：把 `pending` 里的端点对象转成 commit 链表，然后**根据标志位选择策略**：

```c
int prepare_revision_walk(struct rev_info *revs)
{
	...
	for (i = 0; i < old_pending.nr; i++) {
		struct object_array_entry *e = old_pending.objects + i;
		struct commit *commit = handle_commit(revs, e);   /* 端点 → commit */
		if (commit) {
			if (!(commit->object.flags & SEEN)) {
				commit->object.flags |= SEEN;
				next = commit_list_append(commit, next);
			}
		}
	}
	...
	if (!revs->unsorted_input)
		commit_list_sort_by_date(&revs->commits);          /* 链表按时间排序 */
	if (revs->no_walk)
		return 0;                                            /* NO_WALK：到此为止 */
	if (revs->limited) {
		if (limit_list(revs) < 0)                            /* LIMITED：预先吃完整段历史 */
			return -1;
		if (revs->topo_order)
			sort_in_topological_order(&revs->commits, revs->sort_order);
	} else if (revs->topo_order)
		init_topo_walk(revs);                                /* TOPO：建拓扑遍历结构 */
	...
	return 0;
}
```

> 见 [revision.c:3976-4035](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/revision.c#L3976-L4035)。这里能清楚看到三种命运的分支：`no_walk` 直接返回、`limited` 调 `limit_list`、仅 `topo_order` 调 `init_topo_walk`；**都不命中（默认）就什么都不预先做**，留给 `get_revision_1` 的 streaming 模式即时处理。`commit_list_sort_by_date` 保证链表头是最新提交。

**`get_revision_1`：取出下一个并展开父亲。** 这是遍历心脏：

```c
static struct commit *get_revision_1(struct rev_info *revs)
{
	enum rev_walk_mode mode = get_walk_mode(revs);

	if (mode == REV_WALK_STREAMING && revs->commits)
		rev_info_commit_list_to_queue(revs);

	while (1) {
		struct commit *commit;

		switch (mode) {
		...
		case REV_WALK_LIMITED:
		case REV_WALK_NO_WALK:
			commit = pop_commit(&revs->commits);   /* 从链表头弹 */
			break;
		case REV_WALK_STREAMING:
			commit = prio_queue_get(&revs->commit_queue);  /* 从优先队列弹最新的 */
			break;
		}

		if (!commit)
			return NULL;
		...
		/* streaming 模式：当场展开父亲并入队 */
		case REV_WALK_STREAMING:
			if (process_parents(revs, commit, &revs->commit_queue) < 0) {
				...
			}
			break;
		...
		switch (simplify_commit(revs, commit)) {   /* 历史简化与过滤裁决 */
		case commit_ignore:
			continue;
		case commit_error:
			die(...);
		default:
			return commit;                          /* 最终交付 */
		}
	}
}
```

> 见 [revision.c:4377-4451](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/revision.c#L4377-L4451)。三个要点：(1) 容器二选一（链表 vs `commit_queue`）；(2) streaming 时用 `process_parents(..., &revs->commit_queue)` 把父亲塞回**同一个队列**，形成「弹一个、压若干」的滚动；(3) `simplify_commit` 才是「这个 commit 到底展不展示」的最终裁决（含路径过滤、`--no-merges` 等）。

**`process_parents`：展开父亲、传播 UNINTERESTING、入队。** 它是「图搜索里访问邻居」的对应物，处理两条相反的路径：

```c
static int process_parents(struct rev_info *revs, struct commit *commit,
			   struct prio_queue *queue)
{
	...
	if (commit->object.flags & ADDED)
		return 0;                                  /* 父亲已展开过，幂等 */
	commit->object.flags |= ADDED;
	...
	/* 分支一：本 commit 无趣 → 把无趣传染给父亲，并继续入队 */
	if (commit->object.flags & UNINTERESTING) {
		while (parent) {
			...
			p->object.flags |= UNINTERESTING | CHILD_VISITED;
			...
			if (p->parents)
				mark_parents_uninteresting(revs, p);
			if (p->object.flags & SEEN)
				continue;
			p->object.flags |= (SEEN | NOT_USER_GIVEN);
			if (queue)
				prio_queue_put(queue, p);
			...
		}
		return 0;
	}
	...
	/* 分支二：本 commit 有趣 → 逐个解析父亲，没见过的入队 */
	for (parent = commit->parents; parent; parent = parent->next) {
		struct commit *p = parent->item;
		...
		p->object.flags |= pass_flags | CHILD_VISITED;
		if (!(p->object.flags & SEEN)) {
			p->object.flags |= (SEEN | NOT_USER_GIVEN);
			if (queue)
				prio_queue_put(queue, p);
		}
		if (revs->first_parent_only)
			break;                                 /* --first-parent：只跟第一父亲 */
	}
	return 0;
}
```

> 见 [revision.c:1119-1216](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/revision.c#L1119-L1216)。注意 `ADDED` 标志保证幂等（同一 commit 的父亲只展开一次）；`SEEN` 保证每个 commit 只入队一次；`first_parent_only` 在两处都 `break`，实现 `--first-parent`。

**`limit_list`：limited 模式的预先遍历。** 它是 streaming 的「老大哥」——主动把整段历史读进来，逐个判定有趣/无趣，并支持一个很聪明的**提前终止**优化：

```c
static int limit_list(struct rev_info *revs)
{
	int slop = SLOP;
	...
	struct prio_queue queue = { .compare = compare_commits_by_commit_date };

	while (original_list) {
		struct commit *commit = pop_commit(&original_list);
		prio_queue_put(&queue, commit);                  /* 起点全部入堆 */
	}

	while (queue.nr) {
		struct commit *commit = prio_queue_get(&queue);  /* 按时间弹最新的 */
		...
		if (revs->max_age != -1 && (commit->date < revs->max_age))
			obj->flags |= UNINTERESTING;                 /* 早于 --since → 无趣 */
		if (process_parents(revs, commit, &queue) < 0)   /* 展开父亲，传染无趣 */
			...
		if (obj->flags & UNINTERESTING) {
			mark_parents_uninteresting(revs, commit);
			slop = still_interesting(&queue, date, slop, &interesting_cache);
			if (slop)
				continue;
			break;                                        /* 提前终止！ */
		}
		...
		date = commit->date;
		p = &commit_list_insert(commit, p)->next;        /* 有趣的接进结果链表 */
	}
	...
	revs->commits = newlist;
	return 0;
}
```

> 见 [revision.c:1439-1516](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/revision.c#L1439-L1516)。`limit_list` 自己也用一个 `prio_queue`！因为它要从新到旧处理，正好复用了这个堆。`SLOP` 是个**缓冲常数**（见 [revision.c:1297](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/revision.c#L1297)，值为 5）。

**为什么需要 SLOP 提前终止？** 因为提交时间戳并不严格单调（合并、改时钟、rebase 都可能让「较新」的提交时间戳更小），仅靠 `UNINTERESTING` 传播判断「后面还有没有有趣的提交」可能误判。git 的策略是：当队列里已经全是无趣提交时，**不立即停**，而是再多走 `SLOP = 5` 个，给「时间戳错位的有趣提交」一个追赶的机会，再真正停止。判定逻辑在 [revision.c:1299-1321](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/revision.c#L1299-L1321) 的 `still_interesting`：

```c
static int still_interesting(struct prio_queue *src, timestamp_t date, int slop, ...)
{
	struct commit *commit = prio_queue_peek(src);
	if (!commit)
		return 0;                 /* 队列空 → 返回 0 → limit_list 的 break 生效 */
	if (date <= commit->date)
		return SLOP;              /* 还有更新/同时间的 → 继续看 */
	if (!everybody_uninteresting(src, ...))
		return SLOP;              /* 队列里还有有趣的 → 继续 */
	return slop-1;                /* 全无趣 → slop 递减，减到 0 才停 */
}
```

**`get_commit_action`：单个 commit 的最终裁决。** 它被 `simplify_commit` 调用，集中了「要不要展示这个 commit」的全部廉价判断：

```c
enum commit_action get_commit_action(struct rev_info *revs, struct commit *commit)
{
	if (commit->object.flags & SHOWN)
		return commit_ignore;
	...
	if (commit->object.flags & UNINTERESTING)
		return commit_ignore;                  /* 裁剪边界外 */
	...
	if (revs->min_age != -1 &&
	    comparison_date(revs, commit) > revs->min_age)
		return commit_ignore;                  /* 晚于 --until */
	...
	if (revs->min_parents || (revs->max_parents >= 0)) {
		int n = commit_list_count(commit->parents);
		if ((n < revs->min_parents) ||
		    ((revs->max_parents >= 0) && (n > revs->max_parents)))
			return commit_ignore;              /* --no-merges / --merges */
	}
	if (!commit_match(commit, revs))
		return commit_ignore;                  /* grep / pickaxe 不匹配 */
	...
	return commit_show;
}
```

> 见 [revision.c:4178-4250](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/revision.c#L4178-L4250)。注意裁剪规则是**层层 AND**：`UNINTERESTING`、时间上下限、父数限制、grep 匹配，任一不满足就 `commit_ignore`（被 `get_revision_1` 的 `continue` 跳过）。

**真实的调用骨架：`git rev-list`。** `builtin/rev-list.c` 是这套 API 最干净的教科书式用法：

```c
int cmd_rev_list(int argc, ...)
{
	struct rev_info revs;
	...
	repo_init_revisions(the_repository, &revs, prefix);     /* ① 初始化 */
	...
	argc = setup_revisions(argc, argv, &revs, &s_r_opt);    /* ② 解析参数 */
	...
	if (prepare_revision_walk(&revs))                        /* ③ 准备遍历 */
		die("revision walk setup failed");
	...
	traverse_commit_list_filtered(                           /* ④ 迭代输出 */
		&revs, show_commit, show_object, &info, ...);
	...
cleanup:
	release_revisions(&revs);
	return ret;
}
```

> 见 [builtin/rev-list.c:714](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/rev-list.c#L714)、[builtin/rev-list.c:765](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/rev-list.c#L765)、[builtin/rev-list.c:932-933](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/rev-list.c#L932-L933)、[builtin/rev-list.c:985-987](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/rev-list.c#L985-L987)。`traverse_commit_list_filtered` 内部就是一个最朴素的 `while` 循环：

```c
while ((commit = get_revision(ctx->revs)) != NULL) {
	...
}
```

> 见 [list-objects.c:383](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/list-objects.c#L383)。这就是「四步曲」最内层的真身。

#### 4.2.4 代码实践

**目标**：观察 streaming（默认时间倒序）与 limited（路径过滤）两种模式的实际行为，并对照源码确认遍历顺序的来源。

**步骤**：

1. 在 git 自己的源码仓库（或任意有历史的仓库）运行：

```bash
# (a) 默认 streaming：按时间倒序，无路径过滤
git rev-list HEAD | head -5

# (b) 加路径过滤 → 触发 limited 模式（注意 prepare_revision_walk 会调 limit_list）
git rev-list HEAD -- README.md | head -5

# (c) 时间下限裁剪：--since 触发 max_age 过滤
git rev-list --since="6 months ago" HEAD | wc -l

# (d) --topo-order：切换到 REV_WALK_TOPO 模式（需 commit-graph 提供 generation number）
git rev-list --topo-order HEAD | head -5
```

2. 对照源码理解：
   - (a) 的「时间倒序」来自 `REV_INFO_INIT` 里 `commit_queue.compare = compare_commits_by_commit_date`（[revision.h:417](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/revision.h#L417)），streaming 模式靠 `prio_queue` 滚动维持（[revision.c:4398-4400](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/revision.c#L4398-L4400)）。
   - (b) 的路径过滤使 `revs->limited = 1`（[revision.c:2038](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/revision.c#L2038)），于是走 `limit_list`（[revision.c:4013-4017](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/revision.c#L4013-L4017)）。
   - (c) 的 `--since` 映射到 `max_age`，裁剪发生在 `get_commit_action`（[revision.c:4212-4214](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/revision.c#L4212-L4214)）与 streaming 的内联检查（[revision.c:4414-4417](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/revision.c#L4414-L4417)）。
   - (d) 的 `--topo-order` 置 `topo_order`（[revision.c:2438-2439](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/revision.c#L2438-L2439)），有 generation number 时进 `init_topo_walk`。

**观察现象**：
- (a) 与 (b) 的前几条通常相同，但 (b) 条数远少——因为只有改动过 `README.md` 的提交才被保留。
- (c) 给出一个有限计数，反映「半年内」的提交规模。
- (d) 与 (a) 在分叉处顺序可能不同：拓扑序保证「子提交总在父提交之前」，而时间序只按时间戳，可能违反拓扑（一个时间戳更小的子提交可能排到父提交后面）。

**预期结果**：四个命令都正常输出哈希列表，(b) 是 (a) 的子集。若仓库历史很短，(a)(d) 可能完全一致（无分叉时两种序等价）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `git log -- path` 不能用 streaming 模式，必须先 `limit_list`？

**答案**：路径过滤的判定依赖「该提交相对路径的 tree 是否有变化」（`TREESAME`），而这又取决于它的父亲是否也无趣。streaming 模式是「走到哪算哪」，无法保证在输出某提交前已经把决定它去留所需的祖先都处理完。`limit_list` 把整段（到 `SLOP` 提前终止为止）历史先吃进来，才能正确传播 `UNINTERESTING` 并判定 `TREESAME`，最后回放出准确的结果链表。

**练习 2**：`get_revision`、`get_revision_internal`、`get_revision_1` 三层各负责什么？

**答案**：`get_revision` 最外层，处理 `--reverse`（整段反转）和 `--graph` 更新等横切；`get_revision_internal` 中层，处理 `-n/--max-count`、`--skip` 与 `--boundary` 的边界提交收集；`get_revision_1` 最内层，是「真正取下一个 commit」的核心，按 `walk_mode` 从容器取值、展开父亲、跑 `simplify_commit`。

**练习 3**：`SLOP` 这个常数解决了什么问题？

**答案**：提交时间戳不严格单调（合并/rebase/时钟漂移），导致「队列当前全无趣」不一定代表「后面真的再没有有趣提交」。`SLOP=5` 让 `limit_list` 在看似可以停止时**再多探索 5 个**，吸收时间戳错位，避免提前终止漏掉本该出现的提交。

### 4.3 优先队列 prio-queue

#### 4.3.1 概念说明

`prio_queue` 是 git 自带的极简优先队列，基于**二叉小顶堆**（binary min-heap）实现，用一个数组承载。它在遍历引擎里反复出现：streaming 模式的 `commit_queue`、limited 模式 `limit_list` 内部的临时堆、拓扑遍历的 `explore_queue`/`indegree_queue`/`topo_queue` 三个堆，乃至合并基计算都用它。

它有一个**双模式**设计很巧妙（见头文件注释 [prio-queue.h:4-13](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/prio-queue.h#L4-L13)）：

- 若提供比较函数 `compare` → 行为是**优先队列**：`prio_queue_get` 总返回「最小」元素。
- 若 `compare == NULL` → 行为退化为 **LIFO 栈**：`prio_queue_get` 返回最后压入的元素。

一个数据结构两用，省掉了一套单独的栈实现。遍历里 streaming/limited 用优先队列模式，而拓扑遍历的 `topo_queue` 在默认 `REV_SORT_IN_GRAPH_ORDER` 时**故意把 `compare` 设成 NULL**（[revision.c:3858](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/revision.c#L3858)），把它当栈用。

#### 4.3.2 核心流程

二叉堆用一个连续数组存元素，把「第 i 个元素」的两个孩子放在数组下标 \(2i+1\) 与 \(2i+2\)，父节点在 \(\lfloor (i-1)/2 \rfloor\)。小顶堆的不变式是：任一节点的键 ≤ 它孩子的键。

- **入队 `prio_queue_put`**：先把新元素追加到数组末尾，再沿父链**上浮（bubble up）**，直到它不小于父亲，恢复堆序。
- **出队 `prio_queue_get`**：堆顶（下标 0）就是最小元，取出它；把数组末尾元素搬到下标 0，再**下沉（sift down）**，沿较小的孩子下行，直到它不大于孩子。

两种操作的代价都是与树高成正比：

\[
T_{\text{put}},\,T_{\text{get}} = O(\log n),\quad n \text{ 为队列当前元素数}
\]

遍历里队列规模 ≈ 历史的「前沿宽度」（同时活跃的分叉数），通常远小于总提交数，所以 `get_revision` 的单步开销很低。

还有一个**平局打破（tie-breaker）**细节：当两个 commit 时间戳相等（`compare` 返回 0），堆无法只靠键区分先后，git 引入一个单调递增的 `ctr`（插入计数器）作为次序键，保证「先插入的先出」，让排序稳定（见 [prio-queue.c:4-12](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/prio-queue.c#L4-L12)）。

#### 4.3.3 源码精读

**结构定义。** 极简——一个比较函数指针、一个插入计数器、可选回调数据、容量/元素数、数组：

```c
typedef int (*prio_queue_compare_fn)(const void *one, const void *two, void *cb_data);

struct prio_queue_entry {
	size_t ctr;        /* 插入序号，用于平局打破 */
	void *data;        /* 实际载荷（commit 指针等） */
};

struct prio_queue {
	prio_queue_compare_fn compare;
	size_t insertion_ctr;
	void *cb_data;
	size_t alloc, nr;
	struct prio_queue_entry *array;
};
```

> 见 [prio-queue.h:22-35](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/prio-queue.h#L22-L35)。注意它存的是 `void *`，与具体类型解耦——任何指针都能塞进来，比较逻辑由调用方注入。

**统一的比较包装。** 每次比较先调用户函数，相等则用 `ctr` 决出先后：

```c
static inline int compare(struct prio_queue *queue, size_t i, size_t j)
{
	int cmp = queue->compare(queue->array[i].data, queue->array[j].data,
				 queue->cb_data);
	if (!cmp)
		cmp = (queue->array[i].ctr > queue->array[j].ctr) -
		      (queue->array[i].ctr < queue->array[j].ctr);
	return cmp;
}
```

> 见 [prio-queue.c:4-12](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/prio-queue.c#L4-L12)。

**入队：末尾追加 + 上浮。** 注意开头若是 LIFO（`!compare`）则直接返回，不做堆维护：

```c
void prio_queue_put(struct prio_queue *queue, void *thing)
{
	size_t ix, parent;

	ALLOC_GROW(queue->array, queue->nr + 1, queue->alloc);
	queue->array[queue->nr].ctr = queue->insertion_ctr++;
	queue->array[queue->nr].data = thing;
	queue->nr++;
	if (!queue->compare)
		return; /* LIFO */

	for (ix = queue->nr - 1; ix; ix = parent) {     /* 从末尾上浮 */
		parent = (ix - 1) / 2;
		if (compare(queue, parent, ix) <= 0)
			break;
		swap(queue, parent, ix);
	}
}
```

> 见 [prio-queue.c:39-59](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/prio-queue.c#L39-L59)。`parent = (ix-1)/2` 正是上文父节点公式。

**出队：取堆顶 + 末尾补位 + 下沉。**

```c
void *prio_queue_get(struct prio_queue *queue)
{
	void *result;

	if (!queue->nr)
		return NULL;
	if (!queue->compare)
		return queue->array[--queue->nr].data; /* LIFO：弹栈顶 */

	result = queue->array[0].data;              /* 优先队列：取堆顶（最小） */
	if (!--queue->nr)
		return result;

	queue->array[0] = queue->array[queue->nr];  /* 末尾搬去补堆顶 */
	sift_down_root(queue);                       /* 下沉恢复堆序 */
	return result;
}
```

> 见 [prio-queue.c:79-95](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/prio-queue.c#L79-L95)。下沉逻辑在 [prio-queue.c:61-77](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/prio-queue.c#L61-L77) 的 `sift_down_root`：每次选两个孩子中较小者比较，大于孩子就交换下行。

**比较函数：到底比什么？** 「从新到旧」这个顺序由 commit.c 里两个函数定义：

```c
int compare_commits_by_commit_date(const void *a_, const void *b_, void *unused)
{
	const struct commit *a = a_, *b = b_;
	/* newer commits with larger date first */
	if (a->date < b->date) return 1;
	else if (a->date > b->date) return -1;
	return 0;
}

int compare_commits_by_gen_then_commit_date(const void *a_, const void *b_, void *unused)
{
	const struct commit *a = a_, *b = b_;
	const timestamp_t generation_a = commit_graph_generation(a),
			  generation_b = commit_graph_generation(b);
	/* newer commits first */
	if (generation_a < generation_b) return 1;
	else if (generation_a > generation_b) return -1;
	/* use date as a heuristic when generations are equal */
	if (a->date < b->date) return 1;
	else if (a->date > b->date) return -1;
	return 0;
}
```

> 见 [commit.c:930-940](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/commit.c#L930-L940) 与 [commit.c:909-928](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/commit.c#L909-L928)。注意返回值约定：**返回负数表示 a 更优先（更靠前/更小）**，所以「a 更新就返回 -1」。`compare_commits_by_gen_then_commit_date` 主比 generation number（拓扑代数，严格反映祖先关系）、退化为比 date——这正是拓扑遍历用的比较器，它能在时间戳错位时仍保证拓扑合法。

**三队列拓扑遍历。** 拓扑模式（`--topo-order`/`--graph`）是 `prio_queue` 的「高阶用法」，`init_topo_walk` 同时建起三个堆（见 [revision.c:3840-3913](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/revision.c#L3840-L3913)）：

```c
struct topo_walk_info {
	timestamp_t min_generation;
	struct prio_queue explore_queue;     /* 向下探索，按 generation+date 排序 */
	struct prio_queue indegree_queue;    /* 计算入度，按 generation+date 排序 */
	struct prio_queue topo_queue;        /* 实际输出队列：IN_GRAPH_ORDER 时为栈(NULL) */
	struct indegree_slab indegree;       /* 每提交的入度（未被输出的孩子数） */
	struct author_date_slab author_date;
};
```

> 见 [revision.c:3699-3706](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/revision.c#L3699-L3706)。拓扑遍历的精髓是「**入度法**」：给每个提交算「还有多少个孩子没被输出」（入度），入度降到 0 的提交才进 `topo_queue` 可被输出；`expand_topo_walk` 在输出一个提交时把其父亲的入度减一，谁先归零谁就绪（[revision.c:3958-3962](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/revision.c#L3958-L3962)）。`explore_queue`/`indegree_queue` 的比较器都是 `compare_commits_by_gen_then_commit_date`（[revision.c:3870-3871](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/revision.c#L3870-L3871)），让 generation number 引导「先处理更新的提交」。

#### 4.3.4 代码实践

**目标**：通过 trace 观察 streaming 模式下 `prio_queue` 的滚动，并理解 `topo_queue` 何时是栈、何时是堆。

**步骤**：

1. 启用 trace2 的 perf 输出，跑一次默认（streaming）和一次拓扑遍历：

```bash
# streaming 模式：commit_queue 按 commit_date 排序
GIT_TRACE2_PERF=1 git rev-list HEAD >/dev/null

# 拓扑模式：触发 topo_walk，统计三个队列各处理了多少 commit
GIT_TRACE2_PERF=1 git rev-list --topo-order HEAD >/dev/null
```

2. 阅读源码确认三件事：
   - `topo_queue.compare` 在 `REV_SORT_IN_GRAPH_ORDER` 时为 `NULL`（栈），在 `BY_COMMIT_DATE`/`BY_AUTHOR_DATE` 时才是比较函数（堆）——见 [revision.c:3856-3868](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/revision.c#L3856-L3868)。
   - streaming 模式靠 `rev_info_commit_list_to_queue` 把链表一次性灌进 `commit_queue`（[revision.c:3969-3973](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/revision.c#L3969-L3973)），之后 `get_revision_1` 用 `prio_queue_get`/`process_parents` 的 `prio_queue_put` 滚动。
   - `init_topo_walk` 结尾若为图序会把 `topo_queue` 反转（[revision.c:3906-3907](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/revision.c#L3906-L3907)），因为栈是 LIFO，需要反转才能按期望顺序输出。

**观察现象**：`--topo-order` 那次，trace2 的 atexit 统计（[revision.c:3713-3726](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/revision.c#L3713-L3726)）会打印 `count_explore_walked`/`count_indegree_walked`/`count_topo_walked` 三个计数，反映三个堆各处理了多少提交。

**预期结果**：拓扑遍历的三个计数之和通常大于实际输出提交数（探索阶段会多走一些）。streaming 遍历不触发 topo 统计。具体数值待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`prio_queue` 如何在「不写第二份代码」的前提下同时支持优先队列和栈？

**答案**：靠 `compare` 是否为 `NULL` 来分支。`prio_queue_put` 在 `!compare` 时只追加到数组末尾就返回（不做上浮）；`prio_queue_get` 在 `!compare` 时返回 `array[--nr]`（栈顶），否则取堆顶并下沉。同一套数组、同一个 `nr`，两种行为靠一个指针的有无切换，所以没有重复代码。

**练习 2**：为什么拓扑遍历用 generation number 而不是 commit date 作为 `explore_queue` 的主键？

**答案**：commit date 只是时间戳，可能因时钟/rebase 出现「孩子比父亲还旧」的错位，不能可靠反映 DAG 的祖先关系；用它排序无法保证「先处理更新的提交」，会让入度计算走偏。generation number（拓扑层数）严格满足「孩子代数 > 父亲代数」，按它排序能保证探索时永远先看到更接近起点的提交，从而正确推进入度，输出合法的拓扑序。这正是 `--topo-order` 比单纯 `--date-order` 更「正确」的根源。

## 5. 综合实践

把本讲三块知识（rev_info 配置、遍历主循环、prio_queue）串起来，完成一次「**用源码解释命令行为**」的训练：

**任务**：解释 `git rev-list --since="1 year ago" -- README.md HEAD` 这条命令在 `revision.c` 里的完整执行路径，并预测它走的是哪种 walk mode。

**建议步骤**：

1. **判定模式**：因为有路径 `README.md`，`setup_revisions` 末尾会把 `revs->limited` 置 1（[revision.c:2038](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/revision.c#L2038)）。又因无 `--topo-order`、无 reflog，`get_walk_mode` 返回 `REV_WALK_LIMITED`。
2. **追踪 prepare_revision_walk**：`revs->limited` 命中，调 `limit_list`（[revision.c:4013-4017](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/revision.c#L4013-L4017)）。`limit_list` 内部用一个 `prio_queue`（`compare_commits_by_commit_date`）从新到旧遍历（[revision.c:1447](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/revision.c#L1447)）。
3. **追踪 `--since` 的落点**：`--since` 设 `revs->max_age`，在 `limit_list` 里 `commit->date < max_age` 的提交被染 `UNINTERESTING`（[revision.c:1468-1469](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/revision.c#L1468-L1469)），再由 `mark_parents_uninteresting` 传染。
4. **追踪路径过滤的落点**：路径过滤发生在 `simplify_commit` → `get_commit_action` 之外的历史简化阶段（`TREESAME` 判定，本讲点到为止，u9-l4 详讲），最终只有「改动过 README.md 且在一年内」的提交进入结果链表。
5. **验证**：实际运行该命令，对比去掉 `--since` 与去掉 `-- README.md` 的输出条数，验证你对「时间裁剪」与「路径裁剪」各自作用的判断。

**交付物**：一段 300 字以内的文字，按 `setup_revisions → prepare_revision_walk(limit_list) → get_revision_1(limited) → simplify_commit` 的顺序，说明每个环节各裁掉了哪些提交，并指出全程用了几次 `prio_queue`（答案：`limit_list` 内部 1 次；由于是 limited 模式，`commit_queue` 不启用）。

## 6. 本讲小结

- git 的历史遍历是一套**统一引擎**，对外暴露标准四步曲：`repo_init_revisions` → `setup_revisions` → `prepare_revision_walk` → `get_revision` 循环，`git log`/`git rev-list`/`git shortlog` 等都复用它。
- 端点分 **positive**（要展示）与 **negative**（标 `UNINTERESTING` 作裁剪边界，靠 `mark_parents_uninteresting` 向祖先传染）；`struct rev_info` 是承载这一切状态的上下文容器，安全默认值由 `REV_INFO_INIT` 表达。
- 遍历有五种 **walk mode**，默认是 **streaming**：靠 `prio_queue`「弹一个最新 commit、把它父亲压回堆」滚动维持时间倒序，内存只与前沿宽度成正比，所以日常 `git log` 又快又省。
- 带路径过滤/`--children`/`--simplify-merges` 等复杂裁剪时切到 **limited** 模式，先由 `limit_list` 用一个堆吃完整段历史，正确传播 `UNINTERESTING` 并判定 `TREESAME`，靠 `SLOP=5` 吸收时间戳错位做提前终止。
- 最终「这个 commit 展不展示」由 `get_commit_action` 层层 AND 裁决：`UNINTERESTING`、时间上下限、父数限制、grep 匹配，任一不满足即 `commit_ignore`。
- `prio_queue` 是 git 自带的二叉小顶堆，一个数据结构靠 `compare` 是否为 `NULL` 双模式复用为「优先队列 / LIFO 栈」，上浮入队、下沉出队，各 \(O(\log n)\)，并用 `ctr` 计数器打破平局。

## 7. 下一步学习建议

- **继续本单元 u7-l2「commit 可达性与 commit-graph」**：本讲反复出现的 generation number、`commit_graph_generation`、`sort_in_topological_order` 都依赖 commit-graph 缓存文件；u7-l2 会讲清可达性判定、merge-base 算法与 commit-graph 如何把 `--topo-order` 从「慢」变「快」。
- **延伸到 u9-l4「commit 创建与 log/pretty 展示」**：本讲的 `get_revision` 只是「拿出 commit」，怎么格式化成 `git log` 那样的输出（`pretty.c`、`log-tree.c`、`--graph` 的 ASCII 渲染）在那里详讲。
- **延伸到 u8-l1「diff 核心引擎」**：路径过滤的底层（比较两棵 tree、`TREESAME` 判定）其实是 diff 管线在做，读完 u8 能补全本讲略过的 `simplify_commit`/`try_to_simplify_commit` 细节。
- **源码深读顺序建议**：先读 `prio-queue.c`（120 行，最独立），再读 `revision.c` 的 `get_revision_1`→`get_revision_internal`→`get_revision` 三层，最后挑战 `limit_list` 与 `init_topo_walk`/`expand_topo_walk` 的拓扑入度法。

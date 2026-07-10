# 核心结构 struct repository

## 1. 本讲目标

上一讲（[u2-l1](u2-l1-repository-discovery.md)）我们弄清楚了 git 是如何从当前目录一路向上「找到」仓库的 `gitdir`，并把结果写进 `GIT_DIR` 环境变量的。但环境变量只是「字符串路径」，命令真正运行时需要的是一整套已加载、已校验、可复用的运行时上下文——对象数据库是否已打开？引用存储用哪种后端？索引读进内存了吗？当前用 SHA-1 还是 SHA-256？

本讲要回答：**git 把这一切都装在哪个 C 结构里？**

学完本讲，你应当能够：

1. 说清楚 `struct repository` 作为「运行时仓库上下文」的角色，以及它和上一讲找到的 `gitdir` 字符串之间的关系。
2. 看懂 `struct repository` 里对象数据库（`objects`）、引用存储（`refs_private`）、索引（`index`）等关键指针是如何组织的，以及它们各自是「急切初始化」还是「懒加载」的。
3. 理解 `enum ref_storage_format` 这个枚举的含义、默认值是怎么决定的，以及它与 `init.refStorage` 配置的对应。
4. 解释 `the_repository` 这个全局变量从何而来，它和「主仓库」是什么关系，以及 git 正在进行的「把仓库显式当参数传递」的迁移。

---

## 2. 前置知识

在进入源码前，先用通俗语言建立三个直觉。

### 2.1 从「路径字符串」到「运行时对象」

可以这样类比：上一讲的 `setup_git_directory()` 像是你拿到了一张写着「图书馆地址」的纸条（`gitdir` 路径）；而 `struct repository` 则是你**亲自走进图书馆之后**手里握着的那张「读者证 + 借阅卡 + 储物柜钥匙」——它不仅记着地址，还持有「书库已开门」「目录柜已编目」「我的暂存柜在第几号」这些**活的、可用的状态**。

git 里几乎每一个上层命令最终都要在这个上下文里干活：读对象、写引用、改索引。所以 `struct repository` 是连接「命令分发」（[u1-l4](u1-l4-command-dispatch.md)）和「底层数据结构」（对象/索引/引用）的关键枢纽。

### 2.2 急切初始化 vs 懒加载

git 非常在意启动速度（每次 `git status` 都是一次进程启动），所以 `struct repository` 里的字段并不是一次性全部填满的：

- **急切初始化**：发现仓库后立刻就要用的、很轻量的东西，例如 `gitdir`、`commondir`、哈希算法。
- **懒加载（lazy）**：又重又可能根本用不上的东西，例如引用存储（很多命令根本不碰 ref）、配置缓存、索引内容。它们在被第一次访问时才真正分配和打开。

记住这一点，后面看到 `if (!r->refs_private) ...` 这种写法就不会觉得奇怪。

### 2.3 「主仓库」与「子仓库」共存

git 要支持子模块（submodule）和多工作树（worktree）。这意味着一个进程里可能**同时存在多个 `struct repository` 实例**：一个是主仓库，另外还有几个是子模块或链接工作树的仓库。git 正在进行的重构，目标就是让每一段代码都**明确知道自己操作的是哪个 `struct repository`**，而不是偷偷依赖一个全局的「当前仓库」。

---

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [repository.h](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/repository.h) | 定义 `struct repository`、`enum ref_storage_format`，以及一组 `repo_*` 操作函数的声明。本讲的主战场。 |
| [repository.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/repository.c) | `struct repository` 的实现：全局 `the_repository` 的定义、初始化、路径设置、清理等。 |
| [repo-settings.h](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/repo-settings.h) | `struct repo_settings` 定义——它作为 `settings` 字段嵌在 `struct repository` 里，集中存放从配置解析出的「行为开关」。 |
| [environment.h](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/environment.h) | 所有 `GIT_*` 环境变量名宏、`local_repo_env[]` 数组声明，以及与「全局状态 vs 仓库状态」迁移相关的 TODO 注释。 |

补充参考（非本讲主战场，但会被引用以串起流程）：

| 文件 | 作用 |
| --- | --- |
| [odb.h](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/odb.h) | `struct object_database` 定义，即 `repository.objects` 指针所指类型。 |
| [refs.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c) | `get_main_ref_store()`——`refs_private` 的懒加载入口。 |
| [setup.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c) | 在仓库格式校验阶段把对象库、引用存储格式等装填进 `struct repository`。 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. `struct repository` 字段总览——先建立全景。
2. 对象数据库与引用存储指针——看最关键的两个子系统指针。
3. 引用存储格式 `REF_STORAGE_FORMAT`——看一个典型「磁盘格式枚举」是如何在结构体里表示并被选定的。

### 4.1 struct repository 字段总览

#### 4.1.1 概念说明

`struct repository` 是 git 在内存里对「一个仓库」的完整表示。你可以把它理解为**一次 git 命令运行期间的「仓库环境」对象**：它持有路径、持有各子系统的指针、持有从配置解析出的开关、还持有一个表示「我自己是否已经初始化完毕」的标志位。

它最重要的设计特点有三个：

- **聚合而非继承**：对象库、引用、索引、配置、远程信息……都是作为**指针字段**挂在这个结构上，彼此解耦，各自管理自己的生命周期。
- **懒加载**：重资源延迟到首次访问。
- **可多实例**：可以有主仓库之外的实例（子模块、工作树）。

#### 4.1.2 核心流程

一个 `struct repository` 实例的典型生命周期：

```
发现 gitdir (setup_git_directory, 见 u2-l1)
        │
        ▼
repo_set_gitdir()        ── 写入 gitdir / commondir / graft_file / index_file
        │
        ▼
initialize_repository()  ── 分配 remote_state / parsed_objects / index，置 initialized=true
        │
        ▼
check_repository_format()── 读 .git/config，装填 objects(odb)、hash_algo、ref_storage_format ...
        │
        ▼
命令运行期间按需懒加载： get_main_ref_store()、prepare_repo_settings()、repo_read_index() ...
        │
        ▼
repo_clear()             ── 释放所有子系统与字符串
```

注意：`gitdir` 一旦设好就不能为 `NULL`（否则大量 `repo_get_*` 取值函数会直接 `BUG()`）。

#### 4.1.3 源码精读

结构体本身定义在 [repository.h:41-212](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/repository.h#L41-L212)。为了便于记忆，我们把它的字段按「职责」分成几组：

**① 路径字段**——存放上一讲找到的那些路径，把它们从「环境变量里的字符串」固化成「结构体里持有的所有权字符串」：

[repository.h:47-53](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/repository.h#L47-L53) 定义了 `gitdir` 与 `commondir`。`gitdir` 是当前工作树（或 bare 仓库）的 `.git` 目录；`commondir` 是「共享公共目录」——对于 linked worktree，各工作树有自己的 `gitdir`，但公共对象库、配置、引用在 `commondir`。

> 关于 `gitdir` 与 `commondir` 的区别，可类比：`gitdir` 是「我这间阅览室」，`commondir` 是「整座图书馆的公共书库」。普通仓库里两者相同；多工作树场景下，多个 `gitdir` 共享一个 `commondir`。结构体用 [repository.h:205](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/repository.h#L205) 的位域 `different_commondir:1` 记录二者是否不同。

其余路径字段还包括 `graft_file`、`index_file`（[repository.h:104-110](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/repository.h#L104-L110)）、`worktree`（[repository.h:116](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/repository.h#L116)）。

**② 子系统指针字段**——这是本讲的重头戏，后面 4.2 详讲。先列出来：

| 字段 | 类型 | 指向 |
| --- | --- | --- |
| `objects` | `struct object_database *` | 原始对象内容访问（松散对象、pack、alternates） |
| `parsed_objects` | `struct parsed_object_pool *` | 本仓库已解析对象的内存池 |
| `refs_private` | `struct ref_store *` | 引用存储后端（files/packed/reftable） |
| `index` | `struct index_state *` | 内存中的暂存区（索引） |
| `config` | `struct config_set *` | 已解析的配置键值集合 |
| `submodule_cache` | `struct submodule_cache *` | `.gitmodules` 解析结果 |
| `remote_state` | `struct remote_state *` | 远程与分支跟踪信息 |

对应源码见 [repository.h:58-74](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/repository.h#L58-L74) 与 [repository.h:142-154](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/repository.h#L142-L154)。

**③ 行为与算法字段**：

- `hash_algo` / `compat_hash_algo`（[repository.h:157-160](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/repository.h#L157-L160)）：当前仓库使用的哈希算法（SHA-1 或 SHA-256），以及兼容算法。
- `settings`（[repository.h:134](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/repository.h#L134)）：`struct repo_settings`，集中存放从配置解析出的开关（见 4.1 后续）。
- `bare_cfg`（[repository.h:125](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/repository.h#L125)）：是否 bare 的缓存值，`-1` 表示未知。

**④ 多仓库/多工作树支持**：`submodule_ref_stores` 与 `worktree_ref_stores` 是两个 `strmap`，分别按子模块名、按工作树 id 缓存额外的 `ref_store`（[repository.h:87-93](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/repository.h#L87-L93)）。这是「一个 `struct repository` 还能附带若干子仓库的引用库」的体现。

**⑤ 杂项与运行辅助**：`trace2_repo_id`（[repository.h:174](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/repository.h#L174)，给 trace2 追踪用的唯一 id）、`commit_graph_disabled`、hook 相关缓存等。

**⑥ 两个状态标志**：

- `initialized`（[repository.h:211](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/repository.h#L211)）：本实例是否已完成 `initialize_repository()`。很多取值函数（如 `repo_config_values()`）会先检查它，未初始化就 `BUG()`。
- `different_commondir`（[repository.h:205](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/repository.h#L205)）：上面提到的位域。

`initialize_repository()` 的实现见 [repository.c:65-100](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/repository.c#L65-L100)：它把 `initialized` 置真，分配 `remote_state`、`parsed_objects`、`index`，并给 `bare_cfg` 一个「未知」的初值 `-1`。注意它**不会**去读配置或打开对象库——那是后面 `check_repository_format` 阶段的事。也就是说，「初始化」在这里特指「把结构体本身从零状态变成可用的空壳」。

关于 `settings` 字段所引用的 `struct repo_settings`，定义在 [repo-settings.h:19-66](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/repo-settings.h#L19-L66)。它把一大批原本散落的全局配置变量（`core.commitGraph`、`index.version`、`pack.*`、`core.sharedRepository`、fsmonitor 设置等）收拢成一个仓库级结构，并有 `REPO_SETTINGS_INIT` 宏给出默认值（[repo-settings.h:67-77](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/repo-settings.h#L67-L77)）。`prepare_repo_settings()` 负责按需把它真正填充——又一个典型的懒加载。

#### 4.1.4 代码实践

**实践目标**：用源码 + 命令行观察，亲手把 `struct repository` 的关键字段和磁盘上的真实文件对应起来。

**操作步骤**：

1. 在任意 git 仓库里执行下面三条命令，记下输出：

   ```sh
   git rev-parse --git-dir          # 对应 struct repository.gitdir
   git rev-parse --git-common-dir   # 对应 struct repository.commondir
   git rev-parse --show-toplevel    # 对应 struct repository.worktree
   ```

2. 打开 [repository.h:41-212](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/repository.h#L41-L212)，把上面三条命令的输出分别标到 `gitdir`、`commondir`、`worktree` 三个字段旁边。

3. 如果你的仓库是普通仓库（非 worktree），`--git-dir` 与 `--git-common-dir` 应当相同；这时结构体里的 `different_commondir` 位域应为 0。可选地用 `git worktree add ../wt-test` 创建一个链接工作树，进入它再跑一次前两条命令，观察 `gitdir` 变了而 `commondir` 不变。

**需要观察的现象**：

- 普通仓库：两条 `--git-dir` 系列输出一致。
- 链接工作树：`--git-dir` 指向 `.git/worktrees/<id>`，`--git-common-dir` 仍指向主仓库的 `.git`。

**预期结果**：你能用一张表把「磁盘路径 ↔ 结构体字段」一一对应，并理解为什么 git 要区分「每工作树路径」与「公共路径」。

> 本实践为「源码阅读 + 命令观察」型；具体输出取决于你的仓库布局，路径数值为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`struct repository` 里 `gitdir` 字段的注释说「`Cannot be NULL after initialization`」。请找到源码中哪个函数会在它为 `NULL` 时主动报错？

> **答案**：`repo_get_git_dir()`，见 [repository.c:112-117](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/repository.c#L112-L117)，里面写了 `if (!repo->gitdir) BUG("repository hasn't been set up");`。同理 `repo_get_common_dir()`（[repository.c:119-124](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/repository.c#L119-L124)）也守护 `commondir`。

**练习 2**：`initialize_repository()` 给 `bare_cfg` 赋的初值是多少？为什么不是直接给 0（非 bare）？

> **答案**：赋的是 `-1`（见 [repository.c:76](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/repository.c#L76)）。字段注释（[repository.h:120-125](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/repository.h#L120-L125)）说明 `-1` 表示「未设置/未知」，0 表示非 bare，1 表示 bare。给 `-1` 是为了区分「还没读到配置」与「读到了且是非 bare」，避免把「未知」误当成「确定非 bare」。

---

### 4.2 对象数据库与引用存储指针

#### 4.2.1 概念说明

git 的三大底层数据结构——**对象**、**引用**、**索引**——在 `struct repository` 里分别由 `objects`、`refs_private`、`index` 三个指针承载。本模块聚焦前两者，索引留到第 4 单元（[u4-l1](u4-l1-index-state.md)）详讲。

- `objects`（`struct object_database *`）：封装「如何访问原始对象内容」。它管理一个或多个 **source**（对象来源），主来源是 `.git/objects`，其余是 alternates（借用别的仓库的对象）。它还持有 commit-graph 缓存、replace map（`git replace` 的替换表）等。
- `refs_private`（`struct ref_store *`）：引用存储后端。git 支持多种 ref 后端（files、packed、reftable，见 [u5-l2](u5-l2-refs-backends.md)），`ref_store` 是它们的统一抽象。字段名带 `private`，注释明确提示「应通过 `get_main_ref_store()` 访问，因为它会懒加载」。

#### 4.2.2 核心流程

**对象库的装填**是「急切」的：在仓库格式校验阶段（`check_repository_format` → `verify_repository_format`）就调用 `odb_new()` 创建好 `objects`，把主对象目录和 alternates 传进去。源码在 [setup.c:1779-1784](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L1779-L1784)：

```c
repo->objects = odb_new(repo, object_directory,
                        alternate_object_directories);
repo_set_compat_hash_algo(repo, format->compat_hash_algo);
repo_set_ref_storage_format(repo,
                            format->ref_storage_format,
                            format->ref_storage_payload);
```

**引用存储则是「懒加载」的**：`struct repository` 被装填时只记录「用哪种格式」，真正打开 ref 后端推迟到第一次有人要读/写 ref 时。入口是 `get_main_ref_store()`，伪代码如下：

```
get_main_ref_store(r):
    if r->refs_private != NULL:           # 已加载，直接返回
        return r->refs_private
    if r->gitdir == NULL: BUG(...)        # 没在仓库里就别调我
    r->refs_private = ref_store_init(r,   # 按 r->ref_storage_format 选后端并打开
                          r->ref_storage_format,
                          r->gitdir, REF_STORE_ALL_CAPS)
    r->refs_private = maybe_debug_wrap_ref_store(...)  # 可选的调试包装
    return r->refs_private
```

这种「先记格式、后开实例」的拆分，正是为了让那些根本不碰 ref 的命令（比如纯对象层的 `git cat-file`）不必付出打开 ref 后端的代价。

#### 4.2.3 源码精读

先看字段声明与注释，[repository.h:55-74](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/repository.h#L55-L74)：

```c
/* Holds any information related to accessing the raw object content. */
struct object_database *objects;
...
/* The store in which the refs are held. This should generally only be
 * accessed via get_main_ref_store(), as that will lazily initialize ... */
struct ref_store *refs_private;
```

`object_database` 的结构见 [odb.h:38-100](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/odb.h#L38-L100)。其中关键字段：

- `sources`（[odb.h:54](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/odb.h#L54)）：对象来源链表，主目录排第一，其后是 alternates。
- `replace_map`（[odb.h:71](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/odb.h#L71)）：`git replace` 的对象替换表。
- `commit_graph`（[odb.h:75](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/odb.h#L75)）：提交图缓存（见 [u7-l2](u7-l2-commit-graph-reach.md) / [u13-l1](u13-l1-commit-graph-midx.md)）。

取值函数 `repo_get_object_directory()`（[repository.c:126-131](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/repository.c#L126-L131)）返回 `repo->objects->sources->path`，即主对象目录路径——这正是 `git rev-parse --git-path objects` 背后所取的值。

引用存储的懒加载入口 `get_main_ref_store()` 见 [refs.c:2360-2379](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L2360-L2379)。注意它用一个 `static bool initializing` 做重入保护：如果初始化 ref store 的过程中又递归地调用了自己，会 `BUG("initialization of main ref store is recursing")`。最终它调用 `ref_store_init(r, r->ref_storage_format, ...)`——**读取的就是本仓库记录的 `ref_storage_format` 字段**，这就是为什么 4.3 节要专门讲这个枚举。

清理时两者都要释放：`repo_clear()` 里 `odb_free(repo->objects)`（[repository.c:385-386](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/repository.c#L385-L386)）释放对象库；`ref_store_release(repo->refs_private)`（[repository.c:425-428](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/repository.c#L425-L428)）释放引用存储，并遍历释放 `submodule_ref_stores`、`worktree_ref_stores` 两个 map 里的附加 ref store（[repository.c:430-436](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/repository.c#L430-L436)）。

#### 4.2.4 代码实践

**实践目标**：观察对象库「急切装填」与引用存储「懒加载」的差异。

**操作步骤**：

1. 在一个仓库里查看对象目录与引用目录的实际布局：

   ```sh
   ls "$(git rev-parse --git-path objects)"     # 对应 objects->sources->path
   ls "$(git rev-parse --git-dir)/refs" 2>/dev/null || echo "（可能是 reftable 后端，无 refs/ 目录）"
   cat "$(git rev-parse --git-dir)/config"      # 看 [core] 与 extensions 段
   ```

2. 打开 [setup.c:1779-1784](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L1779-L1784)，确认对象库是在格式校验阶段一次性建好的。

3. 再打开 [refs.c:2360-2379](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L2360-L2379)，确认 ref store 是首次访问时才建。

**需要观察的现象**：

- `objects/` 目录在仓库一发现时就已存在并可被 `odb_new()` 打开。
- 即便你看到的仓库磁盘上根本没有 `refs/` 目录（reftable 后端），git 命令仍能正常读 ref——因为 `refs_private` 是按需、按格式打开的，与磁盘上是否有传统 `refs/` 无关。

**预期结果**：你能用自己的话说明——为什么把「打开对象库」放在启动早期、把「打开 ref 后端」推迟到首次访问，是对启动性能更友好的选择。

> 本实践以源码阅读与磁盘观察为主；具体目录内容「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `refs_private` 的注释强调「应通过 `get_main_ref_store()` 访问」，而不是直接读 `repo->refs_private`？

> **答案**：因为 `refs_private` 默认是 `NULL`，真正的 `ref_store` 实例要由 `get_main_ref_store()` 在首次访问时懒加载创建（[refs.c:2360-2379](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L2360-L2379)）。直接读字段只会得到 `NULL`，必须走这个 getter 才能触发初始化并拿到可用实例。

**练习 2**：对象库 `objects` 是什么时候被创建的？给出函数名与源码位置。

> **答案**：在仓库格式校验阶段，由 `odb_new()` 创建并赋给 `repo->objects`，见 [setup.c:1779](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L1779)。与 ref store 不同，这是「急切」创建的。

---

### 4.3 引用存储格式 REF_STORAGE_FORMAT

#### 4.3.1 概念说明

git 的引用可以用不同的磁盘格式存储：

- **files**：传统的「松散文件 + `packed-refs`」格式，每个 ref 是 `.git/refs/` 下的一个文件。这是历史上唯一、也是当前的默认格式。
- **reftable**：一种二进制块格式，把大量 ref 集中存进少量文件，在巨型仓库下更省 inode、更快扫描。

`enum ref_storage_format` 就是用来在代码里标记「这个仓库用的是哪种」。

#### 4.3.2 核心流程

一个仓库的引用存储格式按如下优先级确定（在 `git init` 与每次仓库格式读取时都会走一遍）：

```
显式传入的 ref_format (命令行/函数参数)        ── 最高
    │ 否则
    ▼
环境变量 GIT_REFERENCE_BACKEND 传入的名字       ── 注意还有 URI 解析分支
    │ 否则
    ▼
配置文件里 init.refStorage / extensions.* 读到的 cfg.ref_format
    │ 否则
    ▼
REF_STORAGE_FORMAT_DEFAULT                      ── 兜底默认
```

确定后，它被存进 `struct repository_format`，最终通过 `repo_set_ref_storage_format()` 写进 `struct repository.ref_storage_format` 字段。之后 `get_main_ref_store()` 就读这个字段来决定实例化哪个后端。

#### 4.3.3 源码精读

枚举定义见 [repository.h:19-23](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/repository.h#L19-L23)：

```c
enum ref_storage_format {
    REF_STORAGE_FORMAT_UNKNOWN,
    REF_STORAGE_FORMAT_FILES,
    REF_STORAGE_FORMAT_REFTABLE,
};
```

默认值的宏见 [repository.h:25-29](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/repository.h#L25-L29)，它带一个条件编译分支：

```c
#ifdef WITH_BREAKING_CHANGES /* Git 3.0 */
# define REF_STORAGE_FORMAT_DEFAULT REF_STORAGE_FORMAT_REFTABLE
#else
# define REF_STORAGE_FORMAT_DEFAULT REF_STORAGE_FORMAT_FILES
#endif
```

也就是说，**当前（未启用 `WITH_BREAKING_CHANGES`）默认是 files**；为未来的 Git 3.0 预留了把默认切成 reftable 的开关。这正好解释了为什么你现在 `git init` 出来的仓库磁盘上还有传统的 `refs/` 目录。

「确定格式」的优先级链在 [setup.c:2766-2799](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L2766-L2799)。关键几行：显式参数优先（[setup.c:2769-2770](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L2769-L2770)）；其次环境变量（[setup.c:2771-2777](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L2771-L2777)）；其次配置（[setup.c:2778-2779](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L2778-L2779)）；最后兜底（[setup.c:2781](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L2781)）落到 `REF_STORAGE_FORMAT_DEFAULT`。如果你试图用一个不同的格式去重新初始化已有仓库，[setup.c:2767-2768](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L2767-L2768) 会直接 `die("attempt to reinitialize repository with different reference storage format")`。

「把格式装进 repository」的函数是 `repo_set_ref_storage_format()`，见 [repository.c:212-219](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/repository.c#L212-L219)：

```c
void repo_set_ref_storage_format(struct repository *repo,
                                 enum ref_storage_format format,
                                 const char *payload)
{
    repo->ref_storage_format = format;
    free(repo->ref_storage_payload);
    repo->ref_storage_payload = xstrdup_or_null(payload);
}
```

它在仓库格式校验阶段被调用（[setup.c:1782-1784](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L1782-L1784)），同时还会保存一个可选的 `ref_storage_payload`（来自 `GIT_REFERENCE_BACKEND` 的 URI 形式里去掉 schema 后的部分，见 [repository.h:167-171](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/repository.h#L167-L171) 的注释）。

最后，这个字段在 `get_main_ref_store()` 实例化后端时被消费（[refs.c:2373-2374](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L2373-L2374)），形成一条完整的链：**磁盘格式 → 枚举 → repository 字段 → ref 后端实例**。

#### 4.3.4 代码实践

**实践目标**：亲手创建一个 reftable 后端的仓库，对比它与传统 files 后端的磁盘布局差异，并理解 `REF_STORAGE_FORMAT_DEFAULT` 在其中的作用。

**操作步骤**：

1. 创建两个对照仓库：

   ```sh
   git init files-repo                                   # 默认 files
   git init -c init.refStorage=reftable reftable-repo    # 显式 reftable
   ```

2. 对比 `.git` 目录布局：

   ```sh
   ls files-repo/.git/refs 2>/dev/null && echo "files 后端有 refs/ 目录"
   ls reftable-repo/.git/reftable 2>/dev/null && echo "reftable 后端有 reftable/ 目录"
   grep -E 'refStorage|repositoryformatversion' reftable-repo/.git/config
   ```

3. 打开 [repository.h:25-29](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/repository.h#L25-L29) 确认：因为当前源码未定义 `WITH_BREAKING_CHANGES`，所以你**不带** `-c init.refStorage=...` 时拿到的是 files。这正是第 1 条命令得到 files 的原因。

**需要观察的现象**：

- `files-repo/.git` 下有 `refs/` 目录与 `HEAD` 文件；
- `reftable-repo/.git` 下出现 `reftable/` 目录，且 `config` 里能看到 `init.refStorage` 或 `extensions.refStorage` 的记录。

**预期结果**：你能解释「为什么默认是 files」来自源码里的条件编译宏，并知道如何显式切换。两种后端的深入对比留到 [u5-l2](u5-l2-refs-backends.md)。

> 本实践依赖较新版本的 git 支持 reftable；若你的构建版本较旧可能不支持，此时该步骤为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`REF_STORAGE_FORMAT_DEFAULT` 当前等于哪个枚举值？为什么？

> **答案**：等于 `REF_STORAGE_FORMAT_FILES`（[repository.h:28](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/repository.h#L28)），因为当前没有定义 `WITH_BREAKING_CHANGES` 宏。只有未来启用该宏（标注为 Git 3.0）后，默认才会变成 reftable（[repository.h:26](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/repository.h#L26)）。

**练习 2**：如果你想用 `git init` 在一个**已经存在**的仓库上把 ref 后端从 files 改成 reftable，会发生什么？依据源码说明。

> **答案**：会失败并报错 `attempt to reinitialize repository with different reference storage format`。依据是 [setup.c:2766-2768](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L2766-L2768)：当传入的格式既非 UNKNOWN、又与磁盘上记录的格式不同时，直接 `die()`。引用后端不是可以随便切换的，因为它决定了 ref 在磁盘上的物理存在形式。

---

### 4.4 the_repository 全局变量与显式参数迁移

> 这一节是「字段总览」的延伸，专门回答本讲实践任务里关于 `the_repository` 的部分。

绝大多数 git 命令只在一个仓库里运行，于是 git 提供了一个全局指针 `the_repository`，指向「当前进程的主仓库实例」。它的定义在 [repository.c:30-32](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/repository.c#L30-L32)：

```c
/* The main repository */
static struct repository the_repo;
struct repository *the_repository = &the_repo;
```

也就是说，`the_repository` 是一个文件级静态变量 `the_repo` 的地址，进程启动时就存在。仓库发现流程最终把 `the_repo` 这个实例填好，之后全局就通过 `the_repository` 访问它。

但 git 正在逐步消除「偷偷用全局仓库」的写法。注意到 `the_repository` 的 `extern` 声明被包在 `#ifdef USE_THE_REPOSITORY_VARIABLE` 里（[repository.h:214-216](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/repository.h#L214-L216)）。这个宏**不是**全局定义的，而是由每个 `.c` 文件在顶部**自行** `#define`（例如 `read-cache.c`、`wt-status.c`、`transport.c` 等文件第 1 行都有 `#define USE_THE_REPOSITORY_VARIABLE`）。其含义是：「这个文件还在使用旧式的、隐式依赖 `the_repository` 的接口」。

[environment.h:151-163](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/environment.h#L151-L163) 的一段 TODO 注释把这套迁移的意图说得很清楚：

> All the below state either explicitly or implicitly relies on `the_repository`. We should eventually get rid of these and make the dependency on a repository explicit ... Please do not add new global config variables here.

换句话说，git 的长期方向是：**每一段代码都把 `struct repository *r` 当作显式参数传来传去**，`the_repository` 只是「当前还未迁移的代码」的过渡桥梁。这也解释了为什么 `repository.c` 自己**故意不**定义 `USE_THE_REPOSITORY_VARIABLE`（见 [repository.c:22-28](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/repository.c#L22-L28) 的注释）——它不想依赖那些隐式使用 `the_repository` 的函数，而是通过比较指针（如 `if (repo != the_repository)`，见 [repository.c:58](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/repository.c#L58)）来保持正确性。

所以「`the_repository` 与 `struct repository` 的关系」一句话总结：**`the_repository` 是一个指向「主仓库实例」的全局指针；`struct repository` 是它所指的类型；多实例场景（子模块/工作树）下还会有其它独立的 `struct repository` 实例，它们不等于 `the_repository`。**

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「画图 + 标注」任务。

**任务**：阅读 [repository.h:41-212](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/repository.h#L41-L212) 中 `struct repository` 的完整定义，画一张结构示意图，达到以下要求：

1. 用一个大方框表示 `struct repository` 实例。
2. 在方框内标出至少这些字段，并各用一句话注明它们持有什么：
   - 路径类：`gitdir`、`commondir`、`index_file`、`worktree`
   - 子系统指针：`objects`、`refs_private`、`index`、`config`
   - 行为/格式：`hash_algo`、`ref_storage_format`、`settings`
   - 状态：`initialized`、`different_commondir`
3. 从 `objects` 引一条线指向旁边的 `struct object_database` 小框（参考 [odb.h:38-100](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/odb.h#L38-L100)），从 `refs_private` 引一条线指向 `struct ref_store` 小框，并在连线旁标注「懒加载：经 `get_main_ref_store()`」。
4. 在图外画一个全局指针箭头 `the_repository → struct repository 实例`，并写明：`the_repository` 定义于 [repository.c:30-32](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/repository.c#L30-L32)，指向主仓库；其它子模块/工作树仓库是**另外的** `struct repository` 实例，不由它指向。
5. 用一条虚线把 `ref_storage_format` 字段连到 `get_main_ref_store()` 里读它的那一行（[refs.c:2373](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L2373)），说明「格式字段如何决定后端实例」。

**自检**：画完后，你应该能对着图回答——「一个普通仓库和一个 linked worktree 的 `struct repository`，在 `gitdir`/`commondir` 上有什么不同？」「为什么 `refs_private` 默认是 NULL 而 `objects` 不是？」「默认 ref 后端由哪个宏决定？」如果都能答上来，本讲就过关了。

> 这是「源码阅读型实践」，不要求运行命令，重点是把字段、指针、全局变量三者的关系理清。图的具体样式由你自选（手绘、mermaid、文本框图均可）。

---

## 6. 本讲小结

- `struct repository`（[repository.h:41-212](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/repository.h#L41-L212)）是 git 对「一个仓库」的运行时表示，聚合了路径、对象库、引用、索引、配置、远程信息等所有上下文。
- 它的字段分四类：**路径**（`gitdir`/`commondir`/`index_file`/`worktree`）、**子系统指针**（`objects`/`refs_private`/`index`/`config`）、**行为与格式**（`hash_algo`/`settings`/`ref_storage_format`）、**状态**（`initialized`/`different_commondir`）。
- 对象库 `objects` 在仓库格式校验阶段由 `odb_new()` **急切**创建（[setup.c:1779](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L1779)）；引用存储 `refs_private` 则**懒加载**，首次经 `get_main_ref_store()` 访问时才实例化（[refs.c:2360-2379](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L2360-L2379)）。
- `enum ref_storage_format` 有 `UNKNOWN`/`FILES`/`REFTABLE` 三值；当前默认是 `FILES`，由 `REF_STORAGE_FORMAT_DEFAULT` 宏经条件编译决定（[repository.h:25-29](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/repository.h#L25-L29)），未来 Git 3.0 可能切到 reftable。
- 格式的确定有「显式参数 → 环境变量 → 配置 → 默认」的优先级链（[setup.c:2766-2782](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L2766-L2782)），定下来后由 `repo_set_ref_storage_format()` 写入字段（[repository.c:212-219](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/repository.c#L212-L219)）。
- `the_repository`（[repository.c:30-32](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/repository.c#L30-L32)）是指向主仓库实例的全局指针；git 正通过「每个 `.c` 文件自行 `#define USE_THE_REPOSITORY_VARIABLE`」的机制，逐步把代码迁移到「显式传 `struct repository *` 参数」的写法。

---

## 7. 下一步学习建议

到这里，你已经把「命令执行前必须先建好的运行时上下文」看清了。接下来按依赖关系推荐三条路：

1. **顺理成章的下一站：对象模型**（[u3-l1](u3-l1-object-types.md)）。`struct repository.objects` 指向的 `object_database` 到底装的是什么？四种对象类型（blob/tree/commit/tag）如何挂在它上面？这是理解一切上层命令的根基。
2. **横向对照：索引**（[u4-l1](u4-l1-index-state.md)）。本讲只提到 `index` 字段是懒加载的 `struct index_state *`；索引的内部结构（cache_entry、扩展区）值得单独一讲。
3. **深入引用后端**（[u5-l1](u5-l1-refs-api.md)、[u5-l2](u5-l2-refs-backends.md)）。本讲的 `ref_storage_format` 只是「选哪种后端」；`ref_store` 抽象、files/packed/reftable 三种实现的事务与缓存机制，是专家层的内容。

建议先把第 1 条（对象模型）走完，因为索引和引用都会频繁回指对象的概念。

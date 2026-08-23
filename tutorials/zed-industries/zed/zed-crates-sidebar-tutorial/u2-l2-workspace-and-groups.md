# 工作区与项目分组：ThreadEntryWorkspace 与 ProjectGroupKey

## 1. 本讲目标

学完本讲，你应该能够：

- 说出 `ThreadEntryWorkspace` 的 `Open` 与 `Closed` 两种形态各自的字段、适用场景，以及「同一行从 Open 变成 Closed（或反过来）」发生在什么时候。
- 解释 `ProjectGroupKey` 为什么用「主 worktree 路径 + 远程主机」做分组键，以及 linked worktree 的工作区为什么和它的主仓库算作**同一组**。
- 说明 `PathList` 的相等语义（与提供顺序无关、但保留重复），以及它为什么能直接当 `HashMap` 的键用。
- 跟踪 `workspace_path_list` → `root_repository_snapshots` → `linked_worktree_path_lists_for_workspaces` 这条调用链，理解一个含 linked worktree 的多根工作区如何被映射成若干 `PathList`，并最终变成 `Closed` 条目。

上一讲（u2-l1）我们弄清了「列表里的一行是什么 Rust 值」。本讲继续拆数据模型，但聚焦其中最容易被忽视、又最容易出错的一个字段：`ThreadEntry.workspace`。它回答的问题是——**这一行属于哪个工作区？如果那个工作区现在没打开，行还怎么活下来？**

## 2. 前置知识

- **`Entity<Workspace>` 与多工作区窗口（u1-l1、u1-l3 已建立）**：Zed 的一个窗口可以同时「持有」多个 `Workspace`（多个项目），由 `MultiWorkspace` 统一管理。侧边栏挂在 `MultiWorkspace` 上，所以它的列表覆盖窗口内**所有**项目分组，而不只是当前激活的那个。
- **linked worktree（git 工作树）**：`git worktree add` 可以为同一个仓库检出多个工作目录。主仓库在 `/project`，一个 feature 分支可能检出在 `/worktrees/project/feature-a/project`。对 git 来说它们是同一个仓库的两份检出；对文件系统来说是两个不同目录。Zed 里用 `RepositorySnapshot::linked_worktrees()` 表示挂在某个仓库下的工作树列表。
- **主 worktree（main worktree）与 folder path 的区别**：Zed 在 `WorktreePaths` 里为每个「用户打开的目录」（folder path）配对一个「它所属主仓库的路径」（main worktree path）。非 linked worktree 时两者相同；linked worktree 时 folder 是工作树目录、main 是原仓库目录。
- **远程项目（remote project）**：通过 SSH 等方式连接到远程机器打开的项目。`Project::remote_connection_options(cx)` 返回 `Option<RemoteConnectionOptions>`，`None` 表示本地项目。路径相同但主机不同的两个项目是**两个不同的项目**。
- **「每次全量重推导」（u1-l3 已建立）**：侧边栏不缓存行的归属关系，每次 `rebuild_contents` 都重新回答「这个线程属于哪个工作区、那个工作区开没开」。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [crates/sidebar/src/sidebar.rs](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs) | 侧边栏库根 | `ThreadEntryWorkspace` 定义、`workspace_path_list` / `root_repository_snapshots` / `linked_worktree_path_lists_for_workspaces` 三个自由函数、`rebuild_contents` 中 `resolve_workspace` 的判定 |
| [crates/project/src/project.rs](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/project/src/project.rs) | 项目与分组键 | `ProjectGroupKey` 的定义、构造与比较 |
| [crates/project/src/worktree_store.rs](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/project/src/worktree_store.rs) | worktree 路径簿记 | `WorktreePaths`：folder 与 main 的平行列表 |
| [crates/util/src/path_list.rs](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/util/src/path_list.rs) | 路径集合类型 | `PathList` 的相等语义与序信息 |
| [crates/workspace/src/multi_workspace.rs](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/workspace/src/multi_workspace.rs) | 多工作区宿主 | 分组状态的登记、查询与折叠的真正存放地 |
| [crates/workspace/src/workspace.rs](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/workspace/src/workspace.rs) | 工作区实体 | `root_paths` 与 `visible_worktrees`：`PathList` 的原始原料 |
| [crates/agent_ui/src/thread_metadata_store.rs](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/agent_ui/src/thread_metadata_store.rs) | 线程元数据存储 | `entries_for_path` / `entries_for_main_worktree_path`：`PathList` 作为索引键被消费 |
| [crates/sidebar/src/sidebar_tests.rs](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar_tests.rs) | 测试 | linked worktree 产出 `Closed` 条目的端到端样例 |

## 4. 核心概念与源码讲解

### 4.1 PathList：一个「与顺序无关」的路径集合

#### 4.1.1 概念说明

侧边栏里到处需要回答同一个问题：「**这两组路径是不是同一组目录？**」

- 这条线程元数据记录的 `folder_paths`，和窗口里某个工作区的根路径集合，是不是同一组？
- 数据库索引 `threads_by_paths` 怎么按路径集合做键？

难点在于：用户往工作区里添加文件夹是有顺序的，先加 `/a` 再加 `/b` 和先加 `/b` 再加 `/a`，得到的是**同一个工作区**。如果直接拿 `Vec<PathBuf>` 做键，顺序不同就会判不相等。`PathList` 就是为解决这个问题而生的值类型：

- **相等性**：只看「排序后的路径序列」是否完全一致（注意：保留重复，不去重）。
- **展示顺序**：额外记录一份「当初提供的顺序」，供显示时还原（比如分组标题里多个目录的排列）。

#### 4.1.2 核心流程

`PathList::new` 的构造流程：

```text
输入: &[P]（带插入顺序的路径）
  1. 逐个过 SanitizedPath 规范化
  2. 按字典序排序，得到 paths: Arc<[PathBuf]>   ← 相等与哈希只看它
  3. 记录 order: Arc<[usize]>（每个排序后元素原来的下标） ← 展示用
```

由此得到三条推论：

1. `PathList::new(&["a","b"]) == PathList::new(&["b","a"])`，但两者的 `ordered_paths()` 不同。
2. 重复路径会被保留：`new(&["x","x"]) != new(&["x"])`（后面 4.2 会看到一条测试专门锁定这一点）。
3. 因为 `paths` 是排序后的 `Arc<[PathBuf]>`，`PartialEq` 和 `Hash` 都只依赖它，所以 `PathList` 可以直接做 `HashMap` 的键——这正是元数据存储里 `threads_by_paths` / `threads_by_main_paths` 两个索引的键类型。

#### 4.1.3 源码精读

结构体与文档注释：[crates/util/src/path_list.rs:L11-L25](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/util/src/path_list.rs#L11-L25)

```rust
/// A list of absolute paths, with an associated display order.
///
/// Two `PathList` values are considered equal if they contain the same paths,
/// regardless of the order in which those paths were originally provided.
pub struct PathList {
    /// The paths, in lexicographic order.
    paths: Arc<[PathBuf]>,
    /// The order in which the paths were provided.
    order: Arc<[usize]>,
}
```

这段代码把「身份」（排序后的 `paths`）和「展示」（`order`）拆进两个字段，相等语义由前者单独决定。

相等与哈希只看排序序列：[crates/util/src/path_list.rs:L27-L39](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/util/src/path_list.rs#L27-L39)

```rust
impl PartialEq for PathList {
    fn eq(&self, other: &Self) -> bool {
        self.paths == other.paths
    }
}
impl Hash for PathList {
    fn hash<H: Hasher>(&self, state: &mut H) {
        self.paths.hash(state);
    }
}
```

`eq` 与 `hash` 必须遵守的契约（相等则哈希必同）在这里由「都只读 `paths`」天然满足——这是让一个类型可作 `HashMap` 键的关键写法。

构造与还原顺序：[crates/util/src/path_list.rs:L48-L62](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/util/src/path_list.rs#L48-L62) 与 [crates/util/src/path_list.rs:L94-L100](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/util/src/path_list.rs#L94-L100)

```rust
pub fn new<P: AsRef<Path>>(paths: &[P]) -> Self {
    // … enumerate -> SanitizedPath -> sort_by -> 拆出 order 与 paths
}
pub fn ordered_paths(&self) -> impl Iterator<Item = &PathBuf> {
    self.order.iter().zip(self.paths.iter())
        .sorted_by_key(|(i, _)| **i)
        .map(|(_, path)| path)
}
```

`ordered_paths()` 用 `order` 里的原始下标把排序后的序列重新按提供顺序排回来。同文件的单测 [crates/util/src/path_list.rs:L156-L187](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/util/src/path_list.rs#L156-L187) 正好断言了「顺序不同仍相等、`order` 各自正确、序列化往返一致」这三件事。

#### 4.1.4 代码实践

1. **实践目标**：用真实测试确认 `PathList` 的相等语义。
2. **操作步骤**：在仓库根目录运行（`util` 是 [crates/util/Cargo.toml](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/util/Cargo.toml) 里的包名，测试名按子串过滤会同时命中 `test_path_list` 与 `test_path_list_ordering`）：

   ```bash
   cargo test -p util path_list
   ```

3. **需要观察的现象**：两个测试通过；其中 `test_path_list` 明确断言 `PathList::new(&["a/d", "a/c"]) == PathList::new(&["a/c", "a/d"])`，且前者的 `order()` 是 `[1, 0]`。
4. **预期结果**：输出 `ok` 计数 ≥ 2。若你想进一步验证「保留重复」这一点，`util` 里没有现成断言，可参考 4.2.4 中 `agent_ui` 的 `test_thread_worktree_paths_main_deduplicates_linked_worktrees`。
5. 编译或运行环境受限时，此步骤**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`PathList::new(&["/b", "/a"])` 与 `PathList::new(&["/a", "/b"])` 相等吗？各自的 `paths()` 与 `order()` 是什么？

答案：相等。两者 `paths()` 都是 `["/a", "/b"]`。`order()[i]` 的含义是「排序后位于位置 `i` 的路径当初是第几个提供的」：前者先提供 `/b` 再提供 `/a`，排序后位置 0 是 `/a`（当初第 1 个提供）、位置 1 是 `/b`（当初第 0 个提供），故 `order() == [1, 0]`；后者是 `[0, 1]`。

**练习 2**：为什么 `PathList` 的 `Hash` 实现不能把 `order` 也哈希进去？

答案：Rust 要求相等的值必须有相同的哈希。`PathList` 的相等性只由排序后的 `paths` 决定（顺序不同的两个列表相等），如果把 `order` 混入哈希，两个相等但提供顺序不同的列表会得到不同哈希，放进 `HashMap` 后会查不到对方——`threads_by_paths` 这类按路径集合索引的查询就会失效。

**练习 3**：`PathList::new(&["/a", "/a"])` 与 `PathList::new(&["/a"])` 相等吗？

答案：不相等。`new` 只排序不去重，前者的 `paths` 是 `["/a", "/a"]`，后者是 `["/a"]`，切片不等则 `PathList` 不等。这个细节在「同一个主仓库挂两个 linked worktree」的场景里会真实出现（见 4.2.3 的测试）。

### 4.2 ProjectGroupKey 与 WorktreePaths：按「主 worktree」分组

#### 4.2.1 概念说明

`ProjectGroupKey` 是侧边栏里一个项目分组的**身份**：分组头的折叠状态、分组下的行归属、跨窗口的分组匹配，都以它为键。它的定义只有两个字段：

```rust
pub struct ProjectGroupKey {
    paths: PathList,                       // 主 worktree 路径集合
    host: Option<RemoteConnectionOptions>, // 远程主机（None = 本地）
}
```

关键在于 `paths` 存的是**主 worktree 路径**，而不是用户实际打开的目录。这来自 `WorktreePaths` 的配对关系：工作区里每个打开的目录（folder path）都记着它所属的主仓库路径。于是：

- 直接打开 `/project` 的工作区，key 的 paths 是 `["/project"]`。
- 打开 linked worktree `/worktrees/project/feature-a/project` 的工作区，key 的 paths **仍是 `["/project"]`**。

两者 key 相同 → 归入同一分组 → 侧边栏上它们共享一个分组头。这是「linked worktree 与主仓库算同一组」的机制本质。

#### 4.2.2 核心流程

一个工作区得到自己分组键的路径：

```text
Entity<Workspace>
  → workspace.project()                     (crates/workspace/src/workspace.rs:L2676)
  → project.worktree_paths(cx)              得到 WorktreePaths（平行两列表）
  → .main_worktree_path_list()              取「主仓库」那列
  → 加上 project.remote_connection_options(cx) 作为 host
  → ProjectGroupKey { paths, host }
```

分组的登记与查询发生在 `MultiWorkspace`：

```text
pin(workspace, key)                       首次固定一个工作区时
  → ensure_project_group_state(key)        key 不存在则 insert(0, ProjectGroupState{key, expanded:true})
侧边栏 rebuild_contents
  → mw.project_groups(cx)                  每个 ProjectGroupState → ProjectGroup{key, workspaces, expanded}
  → 对每个 group：workspaces_for_project_group(key) 过滤 held 中 pinned 且 key 匹配的工作区
```

注意 `MultiWorkspace.project_groups: Vec<ProjectGroupState>` 只存 `key + expanded`，**不持有工作区句柄**。分组一旦登记，即使组内所有工作区都被关闭（例如一个 pinned 的项目被移走），key 仍留在列表里——这就是「已关闭的项目」还能出现在侧边栏里的原因。折叠状态（`expanded`）也住在这里而不是 `Sidebar` 上，所以侧边栏整表重建时折叠不会丢。

#### 4.2.3 源码精读

`WorktreePaths` 的文档与结构：[crates/project/src/worktree_store.rs:L38-L49](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/project/src/worktree_store.rs#L38-L49)

```rust
/// For non-linked worktrees, the main path and folder path are identical.
/// For linked worktrees, the main path is the original repo and the folder
/// path is the linked worktree location.
pub struct WorktreePaths {
    paths: PathList,      // folder paths
    main_paths: PathList, // main worktree paths
}
```

两条平行列表按下标一一配对，`ordered_pairs()`（[crates/project/src/worktree_store.rs:L101-L106](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/project/src/worktree_store.rs#L101-L106)）按插入顺序迭代出 `(main, folder)` 对；`folder_path_list()` 与 `main_worktree_path_list()`（[crates/project/src/worktree_store.rs:L91-L99](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/project/src/worktree_store.rs#L91-L99)）分别取出两列，注释标明了各自的用途：folder 列用于工作区匹配与 `threads_by_paths` 索引，main 列用于分组键与 `threads_by_main_paths` 索引。

`ProjectGroupKey` 的定义与构造：[crates/project/src/project.rs:L6415-L6452](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/project/src/project.rs#L6415-L6452)

```rust
/// Paths are mapped to their main worktree path first so we can group
/// workspaces by main repos.
pub struct ProjectGroupKey {
    paths: PathList,
    host: Option<RemoteConnectionOptions>,
}
impl ProjectGroupKey {
    pub fn from_project(project: &Project, cx: &App) -> Self {
        let paths = project.worktree_paths(cx);
        let host = project.remote_connection_options(cx);
        Self { paths: paths.main_worktree_path_list().clone(), host }
    }
    pub fn from_worktree_paths(paths: &WorktreePaths, host: Option<RemoteConnectionOptions>) -> Self { /* 同样取 main 列 */ }
}
```

文档注释一句话点破了设计意图：「路径先映射到主 worktree，这样工作区按主仓库分组」。

两套相等语义：[crates/project/src/project.rs:L6484-L6491](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/project/src/project.rs#L6484-L6491)

```rust
pub fn matches(&self, other: &ProjectGroupKey) -> bool {
    self.paths == other.paths
        && same_remote_connection_identity(self.host.as_ref(), other.host.as_ref())
}
```

派生的 `PartialEq/Eq/Hash` 直接比较 `host`，用于进程内（`MultiWorkspace` 各处 `group.key == *key`）；而 `matches()` 用 `same_remote_connection_identity` 做**归一化后的远程身份**比较，用于把数据库/最近项目里反序列化出来的键和活跃工作区的键对齐（例如 [crates/recent_projects/src/sidebar_recent_projects.rs:L187](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/recent_projects/src/sidebar_recent_projects.rs#L187)），因为同一台远程主机的连接配置细节（如端口写法）可能随时间变化。

分组状态在宿主上的登记与查询：[crates/workspace/src/multi_workspace.rs:L670-L684](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/workspace/src/multi_workspace.rs#L670-L684)

```rust
fn ensure_project_group_state(&mut self, key: ProjectGroupKey) {
    if key.path_list().paths().is_empty() { return; }          // 空路径不建组
    if self.project_groups.iter().any(|group| group.key == key) { return; }
    self.project_groups.insert(0, ProjectGroupState { key, expanded: true });
}
```

`pin` 在固定工作区时调用它（[crates/workspace/src/multi_workspace.rs:L766-L779](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/workspace/src/multi_workspace.rs#L766-L779)）；空路径的键被显式拒绝，这解释了 `rebuild_contents` 里那道「空路径分组直接 `continue`」的守卫（见 4.3.3）。

侧边栏消费分组的方式：[crates/sidebar/src/sidebar.rs:L1390](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L1390) 取 `mw.project_groups(cx)`；`project_groups` 与 `workspaces_for_project_group` 的实现在 [crates/workspace/src/multi_workspace.rs:L855-L864](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/workspace/src/multi_workspace.rs#L855-L864) 与 [crates/workspace/src/multi_workspace.rs:L938-L949](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/workspace/src/multi_workspace.rs#L938-L949)——后者过滤 `held` 中 `pinned` 且 `project_group_key(cx) == key` 的工作区。折叠读取则经 [crates/sidebar/src/sidebar.rs:L930-L950](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L930-L950) 的 `is_group_collapsed` / `set_group_expanded`，本质是读写 `MultiWorkspace::group_state_by_key`（[crates/workspace/src/multi_workspace.rs:L879-L890](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/workspace/src/multi_workspace.rs#L879-L890)）。

最后看两条锁定 `WorktreePaths` 配对语义的测试：[crates/agent_ui/src/thread_metadata_store.rs:L3970-L4019](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/agent_ui/src/thread_metadata_store.rs#L3970-L4019)。第一条构造 folder=`["/worktrees/selectric/zed", "/projects/cloud"]`、main=`["/projects/zed", "/projects/cloud"]`，断言 `ordered_pairs()` 仍保持 `(main, folder)` 的对应关系；第二条（`test_thread_worktree_paths_main_deduplicates_linked_worktrees`）给同一个 main 仓库挂两个 linked worktree，断言 `main_worktree_path_list()` 是 `["/projects/zed", "/projects/zed"]`——**重复被保留**，呼应 4.1 的相等语义。

#### 4.2.4 代码实践

1. **实践目标**：用测试验证「folder/main 平行配对」与「main 列保留重复」两个断言。
2. **操作步骤**：在仓库根目录运行（包名见 [crates/agent_ui/Cargo.toml:L2](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/agent_ui/Cargo.toml#L2)）：

   ```bash
   cargo test -p agent_ui thread_worktree_paths
   ```

   会按子串命中 `test_thread_worktree_paths_from_path_lists_preserves_association`、`test_thread_worktree_paths_main_deduplicates_linked_worktrees`、`test_thread_worktree_paths_mismatched_lengths_returns_error` 等测试。

3. **需要观察的现象**：测试通过；阅读第三条测试可知两列长度不一致时 `from_path_lists` 返回 `Err`（数据库损坏数据被显式拦截而不是静默错位）。
4. **预期结果**：全部 `ok`。随后做一个纸面推演：工作区打开 `["/worktrees/selectric/zed", "/projects/cloud"]`，其中前者是 `/projects/zed` 的 linked worktree——写出它的 `ProjectGroupKey.paths`。参考答案：`PathList::new(&["/projects/zed", "/projects/cloud"])`。
5. 编译或运行环境受限时，此步骤**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ProjectGroupKey.paths` 存主 worktree 路径而不是 folder 路径？如果存 folder 路径会发生什么？

答案：为了让「主仓库工作区」和「它的 linked worktree 工作区」落入同一分组。若存 folder 路径，在 `/worktrees/feature-a` 里打开的工作区会自成一个分组，与主仓库分家；而线程元数据又是按主仓库路径归档的（`entries_for_main_worktree_path`），分组一分裂，线程就找不到自己的组了。

**练习 2**：`Sidebar` 把折叠状态放在 `MultiWorkspace` 的 `ProjectGroupState.expanded` 里而不是自己的字段里，有什么好处？

答案：侧边栏的架构约束是「每次全量重推导、不维护增量状态」（u1-l3）。`SidebarContents` 每次重建都会被整体丢弃，若折叠存在 `Sidebar` 的派生字段里就得在重建间手工搬运；放在宿主 `MultiWorkspace` 上，它是独立于列表重建的生命周期状态，rebuild 时通过 `is_group_collapsed(key)` 现查即可，还能随宿主一起序列化持久化。

**练习 3**：`ensure_project_group_state` 为什么直接拒绝空路径的键？

答案：空路径意味着这个工作区还没有任何可见 worktree（比如刚创建还没加载完，或是个纯空工作区），为它建组会出现一个没有身份的分组头。`rebuild_contents` 侧也有一道对应的守卫（分组键路径为空直接 `continue`，不产生分组头与行），两侧语义一致。

### 4.3 ThreadEntryWorkspace：Open 与 Closed 两种形态

#### 4.3.1 概念说明

一个线程/终端行所「属于」的工作区，有两种可能：

- **`Open(Entity<Workspace>)`**：这个工作区当前就在窗口里打开着。行上挂着真实的实体句柄，激活、关闭、读面板状态都可以直接经句柄操作。
- **`Closed { folder_paths, project_group_key }`**：工作区现在**没有**打开。行不挂任何实体，只携带两样「身份材料」：它所在的目录集合（可能指向 linked worktree），和它所属的项目分组键。

为什么需要 `Closed`？因为侧边栏承诺展示的是**窗口全部项目分组**的历史线程与终端，而不是「当前开着的工作区」的。用户昨天在某个 linked worktree 里开的终端，今天那个工作树目录没有打开——行还得在，点了再把它打开。`Closed` 携带的两个字段恰好是「重新打开」所需的全部信息：用 `folder_paths` 找/建工作区，用 `project_group_key` 保证它归回正确的分组。

#### 4.3.2 核心流程

`rebuild_contents` 里每一行得到 `Open` 或 `Closed` 的判定只有一步——**拿元数据里存的 `folder_paths` 去本组打开着的工作区里查户口**：

```text
对每个 group:
  workspace_by_path_list = { workspace_path_list(ws) → ws }   ← 本组所有打开工作区的 PathList 索引
  resolve_workspace(folder_paths):
      命中  → ThreadEntryWorkspace::Open(ws)
      未命中 → ThreadEntryWorkspace::Closed { folder_paths, project_group_key: group.key }
```

而行的候选来自四路查询（线程与终端各有一套）：

| 查询 | 以什么为键 | 得到的形态 |
| --- | --- | --- |
| ① `entries_for_main_worktree_path(group_key.path_list(), host)` | 分组键的主路径 | `resolve_workspace` 判定，Open 或 Closed |
| ② `entries_for_path(group_key.path_list(), host)`（兼容旧数据） | 分组键的主路径 | 同上 |
| ③ 对组内每个打开的工作区：`entries_for_path(ws_paths, host)` | 工作区根路径 | 强制 `Open(ws)` |
| ④ 对每个 linked worktree 路径：`entries_for_path(worktree_path_list, host)` | 单个 linked worktree 目录 | 强制 `Closed` |

①是主通道：新线程入库时 `main_worktree_paths` 记的是分组的主路径，不管它实际开在哪个工作树里。②兜住没写 `main_worktree_paths` 的旧数据。③专门拯救「存储的 main 路径与分组键对不上」的脏行（源码注释举的例子：linked worktree 工作区上的一条旧记录，其 main 路径等于 folder 路径）。④把「开在 linked worktree 里、而该工作树当前未打开」的行以 `Closed` 形态捞出——这就是本讲标题里那条链路的终点。

对渲染与交互的影响：

- **渲染**：`Open` 行能问到实时状态（例如 `is_remote` 直接问 `project().is_local()`）；`Closed` 行只能用随身携带的 `project_group_key.host()` 推断是否远程。
- **交互**：`Open` 行可以直接经句柄激活/关闭；`Closed` 行必须先按 `folder_paths` 打开工作区再继续（下述 `close_terminal` 的例子）。

#### 4.3.3 源码精读

枚举定义：[crates/sidebar/src/sidebar.rs:L211-L220](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L211-L220)

```rust
#[derive(Clone)]
enum ThreadEntryWorkspace {
    Open(Entity<Workspace>),
    Closed {
        /// The paths this entry uses (may point to linked worktrees).
        folder_paths: PathList,
        /// The project group this entry belongs to.
        project_group_key: ProjectGroupKey,
    },
}
```

注意字段注释明确写着「may point to linked worktrees」——`Closed` 的 `folder_paths` 与分组键的 `paths` **可以不同**，前者是工作树目录，后者是主仓库目录。

两种形态下「是否远程」的分流：[crates/sidebar/src/sidebar.rs:L222-L233](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L222-L233)

```rust
fn is_remote(&self, cx: &App) -> bool {
    match self {
        ThreadEntryWorkspace::Open(workspace) => {
            !workspace.read(cx).project().read(cx).is_local()
        }
        ThreadEntryWorkspace::Closed { project_group_key, .. } => project_group_key.host().is_some(),
    }
}
```

这个方法在渲染行时被消费（[crates/sidebar/src/sidebar.rs:L6145-L6150](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L6145-L6150) 取值，L6170 传给 `ThreadItem::is_remote`）——`Closed` 形态没有活体项目可问，只能依赖分组键里冻存的 host 信息。

判定本体：[crates/sidebar/src/sidebar.rs:L1443-L1455](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L1443-L1455)

```rust
let workspace_by_path_list: HashMap<PathList, &Entity<Workspace>> = group_workspaces
    .iter()
    .map(|ws| (workspace_path_list(ws, cx), ws))
    .collect();
let resolve_workspace = |folder_paths: &PathList| -> ThreadEntryWorkspace {
    workspace_by_path_list
        .get(folder_paths)
        .map(|ws| ThreadEntryWorkspace::Open((*ws).clone()))
        .unwrap_or_else(|| ThreadEntryWorkspace::Closed {
            folder_paths: folder_paths.clone(),
            project_group_key: group_key.clone(),
        })
};
```

这正是 4.1 讲的 `PathList` 相等语义的消费现场：HashMap 以 `PathList` 为键，元数据里乱序存储的 `folder_paths` 也能命中乱序打开的工作区。

第④路查询产出 `Closed` 线程行：[crates/sidebar/src/sidebar.rs:L1651-L1669](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L1651-L1669)

```rust
// Load any legacy threads for any single linked worktree of this project group.
for worktree_path_list in &linked_worktree_path_lists {
    for row in thread_store.read(cx)
        .entries_for_path(worktree_path_list, group_host.as_ref()).cloned()
    {
        if !seen_thread_ids.insert(row.thread_id) { continue; }
        threads.push(make_thread_entry(
            row,
            ThreadEntryWorkspace::Closed {
                folder_paths: worktree_path_list.clone(),
                project_group_key: group_key.clone(),
            },
        ));
    }
}
```

终端一侧有完全对称的一段：[crates/sidebar/src/sidebar.rs:L1512-L1526](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L1512-L1526)。另注意 [crates/sidebar/src/sidebar.rs:L1537-L1539](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L1537-L1539) 的守卫：分组键路径为空时直接 `continue`，该组不产生分组头与行（与 4.2 的 `ensure_project_group_state` 拒绝空键相呼应）。

`Closed` 行的交互必须「先开后做」，以关闭终端为例：[crates/sidebar/src/sidebar.rs:L4957-L4986](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L4957-L4986)

```rust
if let ThreadEntryWorkspace::Closed { folder_paths, project_group_key } = workspace
    && self.should_load_closed_workspace_for_archive(/* … */)
{
    self.open_workspace_for_archive(
        folder_paths.clone(), project_group_key.clone(), window, cx,
        move |this, workspace, window, cx| {
            this.close_terminal(&metadata, &ThreadEntryWorkspace::Open(workspace), window, cx);
        },
    );
    return;
}
```

递归式的写法很能说明问题：带着 `Closed` 进来 → 打开工作区 → 拿着 `Open` 重新调用自己。激活链路（u6-l1 会展开）也是同一个模式。

测试辅助函数里两种形态的直接对比：[crates/sidebar/src/sidebar.rs:L442-L460](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L442-L460) 的 `reachable_workspaces` 对 `Open` 返回句柄、对 `Closed` 返回空——测试用它断言「这行没有可达的打开工作区」。

#### 4.3.4 代码实践

1. **实践目标**：用一个端到端测试确认「linked worktree 上关闭的终端 → `Closed` 条目，且身份字段被完整保留」。
2. **操作步骤**：在仓库根目录运行：

   ```bash
   cargo test -p sidebar thread_switcher_preserves_closed_terminal
   ```

   然后阅读 [crates/sidebar/src/sidebar_tests.rs:L3249-L3390](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar_tests.rs#L3249-L3390) 这个测试。
3. **需要观察的现象**：测试通过；断言段（L3357-L3389）匹配到 `ThreadEntryWorkspace::Closed { folder_paths, project_group_key }` 后，逐一验证 `folder_paths == ["/worktrees/project/feature-a/project"]`、`project_group_key.path_list() == ["/project"]`，并且专门写了 `ThreadEntryWorkspace::Open(_) => panic!(…)` 分支防止形态退化。
4. **预期结果**：测试 `ok`；你能在脑中复述：元数据以 `main=["/project"]`、`folder=["/worktrees/…/project"]` 入库（L3310-L3324），分组键按 main 列算出 `/project`，工作树目录没有打开的工作区，于是第④路查询捞出它并标成 `Closed`。
5. 编译或运行环境受限时，此步骤**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：同一行会在什么时候从 `Open` 变成 `Closed`？

答案：任何触发 `update_entries` 的变化都会整表重判。具体场景：用户关闭了某个 linked worktree 工作区（但它所属分组仍被宿主记住），下一次重建时 `workspace_by_path_list` 里不再有那个 `PathList`，同样的元数据就解析成 `Closed { folder_paths: 工作树路径, project_group_key }`。反过来重开工作区则变回 `Open`。行的「归属」从来不被缓存，每次重算。

**练习 2**：`Closed` 变体为什么要随身带 `project_group_key`，而不是只带 `folder_paths`？

答案：`folder_paths` 只能定位「打开哪个目录」，不能回答「归入哪个分组」。分组键里的 paths 是**主仓库**路径，与 folder 路径可能不同；重新打开工作区时要按正确的键把它 pin 回原分组（`ensure_project_group_state`），渲染 `is_remote`、归档判定等也都要用键里的 host 与主路径。两样材料合起来才是完整的「重新出现」身份。

**练习 3**：③号查询（按组内打开工作区的根路径查）为什么要把结果强制标成 `Open(ws)` 而不走 `resolve_workspace`？

答案：这条查询的键本来就是「这个打开工作区的根路径」，查到的行必然属于它，直接绑定句柄既省一次查表，也表达出「这是为了拯救脏数据的兜底通道，行必须落到活着的工作区上」的语义。源码注释（[crates/sidebar/src/sidebar.rs:L1620-L1630](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L1620-L1630)）说明这些行会在下一次 `handle_conversation_event` 时被改写回正确形状。

### 4.4 root_repository_snapshots 与 linked worktree 路径收集

#### 4.4.1 概念说明

4.3 的第④路查询需要一份清单：**本组还有哪些 linked worktree 目录可能藏着线程/终端，而这些目录当前没有打开**。这份清单由三个小自由函数接力产出，全部住在 sidebar.rs 的模块层（不属于 `Sidebar` 实体——它们是纯函数，只读世界状态）：

- `workspace_path_list(ws, cx)`：一个打开工作区的根路径集合 → `PathList`。
- `root_repository_snapshots(ws, cx)`：这个工作区里「根目录上正好是个 git 仓库」的那些仓库快照。
- `linked_worktree_path_lists_for_workspaces(workspaces, cx)`：把上述快照挂着的 linked worktree 逐个转成单元素 `PathList`，排序返回。

#### 4.4.2 核心流程

```text
linked_worktree_path_lists_for_workspaces(group_workspaces)
  对每个 workspace:
    前置过滤: visible_worktrees(cx).count() != 1 → 跳过     ← 只处理「单根」工作区
    └─ root_repository_snapshots(workspace)
         ├─ path_list = workspace_path_list(workspace)        ← 根路径集合
         ├─ 遍历 project.repositories() 的每个仓库快照
         └─ 只保留 work_directory_abs_path 出现在 path_list 里的   ← 「根上」的仓库
    └─ 对每个快照: snapshot.linked_worktrees() 逐个
         → PathList::new([linked_worktree.path])              ← 单元素列表
  最后按首个路径排序返回 Vec<PathList>
```

两个值得咀嚼的细节：

- **为什么只处理单根工作区？** 一个工作区开了多个目录时，仓库与「分组归属」的对应关系变得模糊（多目录里哪个目录的仓库算「这个组的根仓库」？），源头代码选择保守跳过。`root_repository_snapshots` 顶部的 TODO 注释也承认：workspace 根路径 → git 仓库的映射在代码库里有多处实现，需要统一。
- **为什么 `is_root` 要拿快照的工作目录和根路径精确比对？** 项目里可能加载着大量嵌套仓库（子目录里的仓库），只有工作目录恰好等于某个根路径的才是「根仓库」；根仓库的 `linked_worktrees()` 才是分组意义下的工作树清单。

#### 4.4.3 源码精读

`workspace_path_list`：[crates/sidebar/src/sidebar.rs:L533-L535](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L533-L535)

```rust
fn workspace_path_list(workspace: &Entity<Workspace>, cx: &App) -> PathList {
    PathList::new(&workspace.read(cx).root_paths(cx))
}
```

原料来自 [crates/workspace/src/workspace.rs:L7136-L7142](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/workspace/src/workspace.rs#L7136-L7142) 的 `root_paths`——注意它取的是 `visible_worktrees` 的 `abs_path()`，即「用户看得见的顶层目录」，不含隐藏工作树。

`root_repository_snapshots` 与它顶部的 TODO：[crates/sidebar/src/sidebar.rs:L511-L531](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L511-L531)

```rust
// TODO: The mapping from workspace root paths to git repositories needs a
// unified approach across the codebase: this function, `AgentPanel::classify_worktrees`,
// thread persistence (which PathList is saved to the database), and thread
// querying (which PathList is used to read threads back). …
fn root_repository_snapshots(
    workspace: &Entity<Workspace>,
    cx: &App,
) -> impl Iterator<Item = project::git_store::RepositorySnapshot> {
    let path_list = workspace_path_list(workspace, cx);
    let project = workspace.read(cx).project().read(cx);
    project.repositories(cx).values().filter_map(move |repo| {
        let snapshot = repo.read(cx).snapshot();
        let is_root = path_list.paths().iter()
            .any(|p| p.as_path() == snapshot.work_directory_abs_path.as_ref());
        is_root.then_some(snapshot)
    })
}
```

这条 TODO 是本讲最诚实的注脚：路径 → 仓库的映射策略目前**没有全局统一**，读源码时要把「持久化时存哪份 PathList、查询时用哪份 PathList、分组时按哪份 PathList」三件事对照着看。

`linked_worktree_path_lists_for_workspaces`：[crates/sidebar/src/sidebar.rs:L537-L557](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L537-L557)

```rust
fn linked_worktree_path_lists_for_workspaces(
    workspaces: &[Entity<Workspace>],
    cx: &App,
) -> Vec<PathList> {
    let mut linked_worktree_paths = Vec::new();
    for workspace in workspaces {
        if workspace.read(cx).visible_worktrees(cx).count() != 1 {
            continue;                       // 只处理单根工作区
        }
        for snapshot in root_repository_snapshots(workspace, cx) {
            linked_worktree_paths.extend(
                snapshot.linked_worktrees().iter().map(|linked_worktree| {
                    PathList::new(std::slice::from_ref(&linked_worktree.path))
                }),
            );
        }
    }
    linked_worktree_paths.sort_by(|a, b| a.paths()[0].cmp(&b.paths()[0]));
    linked_worktree_paths
}
```

`std::slice::from_ref` 把 `&PathBuf` 变成单元素切片，避免一次 `Vec` 分配——小而地道的写法。返回的列表在 `rebuild_contents` 中于 [crates/sidebar/src/sidebar.rs:L1456-L1457](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L1456-L1457) 生成一次，随后同时喂给终端的④路（[L1512-L1526](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L1512-L1526)）和线程的④路（[L1651-L1669](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L1651-L1669)）。

查询侧的另一半——`PathList` 作为数据库内存索引的键：[crates/agent_ui/src/thread_metadata_store.rs:L621-L655](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/agent_ui/src/thread_metadata_store.rs#L621-L655)

```rust
pub fn entries_for_path<'a>(&'a self, path_list: &PathList, …) -> … {
    self.threads_by_paths.get(path_list)…
}
pub fn entries_for_main_worktree_path<'a>(&'a self, path_list: &PathList, …) -> … {
    self.threads_by_main_paths.get(path_list)…
}
```

两个方法长得几乎一样，差别只在用哪个索引：`threads_by_paths`（按 folder 路径）或 `threads_by_main_paths`（按主仓库路径）。到这里，`PathList` 从「值类型」一路走到了「索引键」，4.1 的相等语义支撑着整条链。

#### 4.4.4 代码实践

1. **实践目标**：完成规格指定的调用链跟踪，把「多根 + linked worktree 的工作区 → 若干 `PathList` → `Closed` 条目」画成示意图（完整任务见第 5 节综合实践，本步先做函数级跟踪）。
2. **操作步骤**：
   - 在 IDE 里对 `workspace_path_list`、`root_repository_snapshots`、`linked_worktree_path_lists_for_workspaces` 各做一次 Find Usages（或用 `git grep -n "root_repository_snapshots" crates/sidebar/src/sidebar.rs`）。
   - 确认调用点只有两处模式：`workspace_path_list` 被 `rebuild_contents`（建索引、算 `has_open_projects`）与 `root_repository_snapshots` 等处使用；`linked_worktree_path_lists_for_workspaces` 只被 `rebuild_contents` 的 L1457 调用。
   - 手工回答：一个双根工作区 `["/project", "/docs"]`（`/project` 是 git 仓库且挂了一个 linked worktree `/wt/feature-a`）会产出几个 `PathList`？
3. **需要观察的现象**：`linked_worktree_path_lists_for_workspaces` 的第一道过滤 `visible_worktrees(cx).count() != 1` 会把双根工作区**整个跳过**——答案应是 **0 个**。把 `/docs` 移出工作区（变成单根）后才产出 1 个：`PathList::new(&["/wt/feature-a"])`。
4. **预期结果**：你能不看源码说出三道过滤的顺序（单根 → 根上仓库 → linked worktree 展开）以及每道过滤淘汰什么。
5. 行为推演基于对源码的静态阅读；如需运行验证，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`root_repository_snapshots` 为什么要用「快照的工作目录 == 某个根路径」来判根，而不是取「项目里所有仓库」？

答案：项目加载的仓库包含子目录里的嵌套仓库。若不判根，一个子目录仓库挂的 linked worktree 也会被算进分组的工作树清单，而这些目录并不是该分组的身份来源（分组键只由根路径的主仓库决定）。精确比对根路径才能保证清单与分组语义一致。

**练习 2**：返回的 `Vec<PathList>` 为什么要排序？

答案：`rebuild_contents` 的输出必须稳定：同样的世界状态应产出同样顺序的行（`EntryShape` 与列表测量保留机制依赖这一点，见 u3-l3）。收集顺序取决于仓库在 HashMap 里的遍历顺序（不确定），末尾按首个路径排序把它变成确定序。

**练习 3**：如果未来想让多根工作区也支持 linked worktree 收集，除了去掉 `count() != 1` 这道过滤，还需要小心什么？

答案：至少要处理归属问题——多根工作区里每个根仓库的工作树应该只归属它自己的主仓库分组，而不是笼统归属「当前分组键」（多根工作区的键本身包含多条主路径）；还要与 `root_repository_snapshots` 顶部 TODO 列出的其他三处路径→仓库映射（`AgentPanel::classify_worktrees`、线程持久化、线程查询）保持一致，否则行的写入键与读取键会对不上。

## 5. 综合实践

**任务**：画出「一个多根工作区（含 linked worktree）如何映射为若干 `PathList`，并最终变成 `Closed` 条目」的示意图。这是本讲规格指定的实践，把 4.1–4.4 四个模块串成一条线。

### 步骤

1. **构造场景**（纸面）：窗口里 pin 了两个项目分组——
   - 分组 A：单根工作区，打开 `/project`（git 仓库，linked worktree 在 `/wt/feature-a`）；
   - 分组 B：双根工作区，打开 `["/repo2", "/docs"]`。
   数据库里有一条终端元数据：`folder_paths = ["/wt/feature-a"]`、`main_worktree_paths = ["/project"]`。
2. **沿调用链走一遍**（对照源码）：
   - `rebuild_contents` 先取 `mw.project_groups(cx)`（[crates/sidebar/src/sidebar.rs:L1390](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L1390)），每组建 `workspace_by_path_list`（L1443-L1446）；
   - 每组调 `linked_worktree_path_lists_for_workspaces`（L1456-L1457）→ 内部走 `root_repository_snapshots`（L517-L531）→ `workspace_path_list`（L533-L535）；
   - 四路查询（L1483-L1526 终端 / L1592-L1669 线程）。
3. **标注每条路径的命运**：哪些查询命中、`resolve_workspace` 判成什么形态、哪一路兜底。
4. **运行验证**（可选）：`cargo test -p sidebar thread_switcher_preserves_closed_terminal`，对照 [crates/sidebar/src/sidebar_tests.rs:L3342-L3389](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar_tests.rs#L3342-L3389) 的断言核对图中 `Closed` 条目的两个字段。

### 参考示意图（答案）

```text
窗口 (MultiWorkspace)
 ├─ 分组 A  key.paths = ["/project"]            ← 主 worktree 路径
 │   └─ 打开的工作区: ["/project"]  (单根)
 │        │ workspace_path_list → PathList{"/project"}
 │        │ root_repository_snapshots → 快照{work_dir=/project}   (is_root ✓)
 │        │ linked_worktrees → ["/wt/feature-a"]
 │        └─ linked_worktree_path_lists → [ PathList{"/wt/feature-a"} ]
 │                 │
 │                 ▼ ④ entries_for_path(PathList{"/wt/feature-a"})
 │        终端元数据 (folder=/wt/feature-a) 命中
 │                 │
 │                 ▼ resolve_workspace?  否 —— workspace_by_path_list 只有 "/project"
 │        ThreadEntryWorkspace::Closed {
 │            folder_paths:     PathList{"/wt/feature-a"},
 │            project_group_key: ProjectGroupKey{ paths: ["/project"], host: None },
 │        }
 │   同时 ① entries_for_main_worktree_path(["/project"]) 也会查到这条元数据，
 │        但 seen_terminal_ids / seen_thread_ids 去重保证行只出现一次。
 └─ 分组 B  key.paths = ["/repo2", "/docs"]
     └─ 打开的工作区: ["/repo2", "/docs"]  (双根)
          └─ visible_worktrees().count() == 2 ≠ 1 → linked worktree 收集被跳过，产出 []
```

**预期观察**：分组 A 下出现一行 `Closed` 的终端（它的 `folder_paths` 指向工作树、分组键指向主仓库，两者不同）；分组 B 不产出任何 linked worktree 相关行。如果你把图讲给别人听时能自然说出「为什么两份路径不一样」，本讲就通了。

## 6. 本讲小结

- `PathList` 是「排序后序列定身份、order 记展示」的路径集合值：相等与顺序无关、保留重复，因 `eq`/`hash` 同源而可直接作 `HashMap` 键——它是本讲一切匹配的基石。
- `ProjectGroupKey = 主 worktree 路径 + 远程主机`，由 `WorktreePaths` 的 main 列推导；linked worktree 工作区因此与主仓库同组。分组状态（含折叠）由 `MultiWorkspace::project_groups` 持有，分组在组内工作区全部关闭后仍保留键。
- `ThreadEntryWorkspace::Open` 挂活体句柄、能问实时状态；`Closed` 只带 `folder_paths + project_group_key` 两样「重新打开」的身份材料。判定就是一次以 `PathList` 为键的 HashMap 查找，每次重建现查，从不缓存。
- 行的候选来自四路查询（主路径、旧版 folder、各打开工作区、各 linked worktree），后者强制产出 `Closed`；`seen_thread_ids`/`seen_terminal_ids` 负责跨路去重。
- `workspace_path_list → root_repository_snapshots → linked_worktree_path_lists_for_workspaces` 三函数接力产出「未打开工作树」清单，且只处理单根工作区；`root_repository_snapshots` 顶部的 TODO 提醒：路径→仓库的映射在代码库里尚未统一，读写两侧必须对照。

## 7. 下一步学习建议

- 下一讲 **u2-l3（选中与活跃：selection、ActiveEntry 与身份匹配）**：继续数据模型专题，辨析键盘选中下标与全局活跃条目，并看 `session_id` 如何在 `thread_id` 变化时维持身份——其中 `ActiveEntry` 与工作区形态的配合会用到本讲的 `Open`/`Closed` 判定。
- 若你更想先看这条数据流的源头，可跳到 **u3-l4（rebuild_contents 全景）**：本讲 4.3 的四路查询在那一讲会被放回完整的重建管线（排序、过滤、通知、草屑判定）中审视。
- 延伸阅读源码：`MultiWorkspace::workspace_for_paths`（[crates/workspace/src/multi_workspace.rs:L1065-L1083](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/workspace/src/multi_workspace.rs#L1065-L1083)）是 `resolve_workspace` 判定的「会变更者」版本——找不到时创建工作区，正好对照体会侧边栏为何把「只读判定」与「打开动作」分开。

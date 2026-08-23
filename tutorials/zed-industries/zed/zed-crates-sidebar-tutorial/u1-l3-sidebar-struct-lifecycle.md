# Sidebar 实体：字段全景与构造生命周期

## 1. 本讲目标

学完本讲，你应该能够：

1. 逐组说出 `Sidebar` 结构体全部 32 个字段各自承担的状态，并能区分「派生状态」「记忆状态」「交互状态」「句柄与订阅」四类。
2. 按执行顺序走读 `Sidebar::new`，列出它在构造期注册的全部订阅与观察者（`on_focus_in`、`subscribe_in`、`subscribe`、`observe`、`spawn`），以及每个回调的副作用。
3. 解释 `WeakEntity<MultiWorkspace>` 与 `cx.defer_in` 在初始化中的作用：为什么宿主要用弱引用持有、为什么「补订已有工作区 + 首次刷新」要推迟到构造完成之后。
4. 说出从 `Sidebar::new` 出发延伸出去的二级订阅网络（`subscribe_to_workspace`、`subscribe_to_agent_panel`、`observe_docks`、`refresh_draft_editor_observations`）分别在哪里被挂接。

## 2. 前置知识

本讲承接 u1-l1（crate 定位与装配）和 u1-l2（构建与测试）。在开始之前，你需要理解以下 GPUI 概念——它们是本讲的主角：

- **实体与上下文**：`Entity<T>` 是对类型 `T` 的共享句柄；`cx.new(|cx| ...)` 构造实体时，闭包里拿到的是 `&mut Context<T>`，它既能访问被构造中的 `T`，也能注册订阅、派发任务。
- **事件与订阅（EventEmitter + cx.subscribe）**：类型 `T` 声明 `impl EventEmitter<E> for T {}` 后，就可以在更新实体时用 `cx.emit(event)` 发出 `E` 事件；其他实体用 `cx.subscribe(&entity, |this, entity, event, cx| ...)` 接收。`cx.subscribe_in` 是带窗口的版本，回调额外接收 `window: &mut Window`，适合需要焦点、弹出层等窗口操作的场合。
- **观察（cx.observe）**：`cx.observe(&entity, |this, entity, cx| ...)` 不监听具体事件，而是在被观察实体每次调用 `cx.notify()`（即「我的状态变了，请重新渲染我」）之后触发。它适合监听「Store 型实体」——你只关心它变了，不关心变了什么。
- **弱引用 WeakEntity**：`Entity<T>` 是强引用，会延长目标生命周期；`WeakEntity<T>` 是弱引用，用 `.upgrade()` 得到 `Option<Entity<T>>`。若两个实体互持强引用，谁也释放不了（内存泄漏），所以「子组件持有宿主」时一律用弱引用。
- **延迟执行 cx.defer_in**：`cx.defer_in(window, |this, window, cx| ...)` 把闭包排到当前效果周期（effect cycle）的末尾执行——即等当前这轮构造/更新完全结束之后再跑。

不熟悉以上概念没关系，本讲会结合真实代码逐个演示。

## 3. 本讲源码地图

| 文件 | 行数 | 本讲关注点 |
| --- | --- | --- |
| [crates/sidebar/src/sidebar.rs](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs) | 8208 | `Sidebar` 结构体定义（L734-L792）、`Sidebar::new`（L795-L924）、二级订阅方法（L958-L1245）、`WorkspaceSidebar` trait 实现（L7677-L7750） |
| [crates/workspace/src/multi_workspace.rs](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/workspace/src/multi_workspace.rs) | — | `MultiWorkspaceEvent` 事件枚举（L109-L116）、`Sidebar` 契约 trait（L122-L161）、`register_sidebar`（L393-L405） |

另外会零星提到 `crates/zed/src/zed.rs` 中的装配代码——那是 u1-l1 已经走读过的内容，本讲只引用结论，不再展开。

## 4. 核心概念与源码讲解

### 4.1 Sidebar 结构体：32 个字段的全景

#### 4.1.1 概念说明

`Sidebar` 是一个 GPUI 实体，它把侧边栏需要的**全部**可变状态放在同一个 struct 里。这个 struct 有 32 个字段，乍看吓人，但按职责分组后只有六组：

1. **宿主与几何**——`multi_workspace`、`width`、`focus_handle`；
2. **两个子编辑器**——`filter_editor`（搜索框）、`thread_rename_editor`（行内重命名框），它们是独立的 `Entity<Editor>`，各有自己的事件流；
3. **列表状态与内容**——`list_state`（gpui 虚拟列表的滚动/测量状态）、`contents`（重建产物：可见行、通知集合、项目头索引）；
4. **选中与活跃**——`selection`（键盘焦点下标）、`active_entry`（全局高亮条目）、`hovered_thread_index`（鼠标悬停）；
5. **各种「进行中」的交互**——重命名三件套、线程切换器、MRU 时间戳、恢复任务；
6. **句柄、缓存与订阅**——弹出菜单句柄、默认分支缓存、三个订阅容器和 `update_task`。

更重要的是理解这些状态的**三种性质**，它直接决定了字段该放在哪、什么时候被重置：

- **派生状态**：`contents`（以及 `list_state` 中随重建拼接的部分）。它们由「当前世界状态」全量重推导而来，随时可以丢弃重建——这正是 struct 顶部文档注释规定的架构约束。
- **记忆状态**：`live_thread_statuses`、`draft_kinds`、`thread_last_accessed` 等。全量重建有一个天然缺陷——重建时只能看到「现在」，看不到「上一刻」。凡是需要跨重建对比才能得出的信息（比如「线程从 Running 变成了 Completed」），必须显式存字段。
- **交互状态**：`selection`、`hovered_thread_index`、`renaming_thread_id` 等。它们描述用户当下正在做什么，与数据无关。

#### 4.1.2 核心流程

字段全景可以画成这样一张分组图：

```text
Sidebar（32 个字段）
├── 宿主与几何
│   ├── multi_workspace: WeakEntity<MultiWorkspace>   ← 弱引用宿主
│   ├── width: Pixels                                  ← 200..800 之间钳制
│   └── focus_handle: FocusHandle
├── 子编辑器（独立 Entity）
│   ├── filter_editor: Entity<Editor>          ← 搜索过滤
│   └── thread_rename_editor: Entity<Editor>   ← 行内重命名
├── 列表（派生状态）
│   ├── list_state: ListState          ← gpui 虚拟列表状态
│   └── contents: SidebarContents      ← 重建产物（entries + 通知集 + 头索引）
├── 选中 / 活跃 / 悬停（交互状态）
│   ├── selection: Option<usize>       ← 键盘焦点下标，≠ active_entry
│   ├── active_entry: Option<ActiveEntry>
│   └── hovered_thread_index: Option<usize>
├── 重命名（交互状态）
│   ├── renaming_thread_id / regenerating_titles / suppress_next_rename_edit
├── MRU 与切换器（记忆 + 交互）
│   ├── thread_last_accessed / terminal_last_accessed   ← 仅显式用户动作更新
│   ├── thread_switcher + _thread_switcher_subscriptions
│   └── pending_thread_activation
├── 跨重建缓存（记忆状态）
│   ├── live_thread_statuses / draft_kinds
├── 视图与恢复
│   ├── view: SidebarView（ThreadList | Archive）/ restoring_tasks
├── 菜单句柄与缓存
│   ├── recent_projects_popover_handle / project_header_menu_handles / ...
│   └── worktree_default_branches: HashMap<ProjectGroupKey, DefaultBranchCache>
└── 订阅容器与刷新任务
    ├── _subscriptions / _draft_editor_observations
    ├── update_task: Option<Task<()>>
    └── import_banners_use_verbose_labels / cross_channel_import_channels
```

#### 4.1.3 源码精读

结构体定义本体，连同那段全 crate 最重要的架构注释：

> [sidebar.rs:L730-L792](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L730-L792) —— `Sidebar` 结构体定义。开头的文档注释写道：「侧边栏在每次变化时通过 `update_entries` → `rebuild_contents` 从零重推导整个条目列表，不要添加增量或跨事件协调状态——凡是能从当前世界状态算出来的，都在 rebuild 里算。」这句话就是「派生状态 vs 记忆状态」分界的官方依据：往这个 struct 里加字段前，先问自己「它能不能在重建时算出来」。

几个字段的注释本身就是最好的文档，值得逐条读：

> [sidebar.rs:L742-L747](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L742-L747) —— `selection` 的注释明确提醒「这是**当前持有键盘焦点**的列表项下标，**不等于**活跃条目」。二者混淆是初读此 crate 最常见的错误（u2-l3 会专门辨析）。

> [sidebar.rs:L757-L761](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L757-L761) —— `thread_last_accessed` 的注释：「只因**显式用户动作**（点击线程、在线程切换器里确认等）而更新——绝不因后台数据变化而更新。用于给线程切换器弹窗排序。」这是 MRU 排序正确性的关键约定。

> [sidebar.rs:L765-L772](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L765-L772) —— `live_thread_statuses` 与 `draft_kinds` 是「记忆状态」的典型：前者把活跃线程状态留存在重建之间，使分组折叠（线程行不在列表里）时也能检测 Running→Completed 转换；后者记住每个草稿上一次渲染为空还是有内容。

宽度字段的合法范围由三个常量决定：

> [sidebar.rs:L104-L106](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L104-L106) —— `DEFAULT_WIDTH = 300px`、`MIN_WIDTH = 200px`、`MAX_WIDTH = 800px`。

> [sidebar.rs:L7682-L7685](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L7682-L7685) —— `WorkspaceSidebar::set_width` 实现：`width.unwrap_or(DEFAULT_WIDTH).clamp(MIN_WIDTH, MAX_WIDTH)`。任何来源的宽度（含反序列化恢复）都会被钳进这个区间。

`contents` 字段的类型 `SidebarContents` 是重建管线的产物容器：

> [sidebar.rs:L476-L482](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L476-L482) —— `entries`（可见行列表）、`notified_threads` / `notified_terminals`（通知徽标集合）、`project_header_indices`（项目头在 entries 中的下标，粘性头部定位用）、`has_open_projects`（空态判定用）。

最后看三个订阅容器字段的「填充时机」——它们空着出生，生命周期各不相同：

- `_subscriptions`：构造时为空，仅在打开归档视图时 push 进对 `ThreadsArchiveView` 事件的订阅（见 [sidebar.rs:L7595](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L7595)，位于 `show_archive` 内）；
- `_draft_editor_observations`：每次 `update_entries` 后整体清空重建（见 4.4.3）；
- `_thread_switcher_subscriptions`：线程切换器弹出时赋值、关闭时清空（见 [sidebar.rs:L6089](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L6089)）。

#### 4.1.4 代码实践

**实践目标**：把 32 个字段按「派生 / 记忆 / 交互 / 句柄与订阅」四类归类，建立字段全景的心理地图。

**操作步骤**：

1. 打开 [sidebar.rs:L734-L792](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L734-L792)，从上到下逐个字段抄进一张四列表格。
2. 参考分类示例（已填 6 项）：

   | 字段 | 分类 | 一句话理由 |
   | --- | --- | --- |
   | `contents` | 派生 | 每次重建从世界状态重推导 |
   | `selection` | 交互 | 描述键盘焦点，与数据无关 |
   | `live_thread_statuses` | 记忆 | 重建时看不到「上一刻」，必须留存 |
   | `thread_last_accessed` | 记忆 | 仅显式用户动作更新，跨重建存活 |
   | `worktree_default_branches` | 缓存（记忆的特殊形态） | 避免菜单打开期间做 git I/O |
   | `project_header_menu_handles` | 句柄 | 指向外部组件的控制句柄 |

3. 对拿不准的字段，用 Grep 查它的所有写入点来验证：写入只出现在 `update_entries` / `rebuild_contents` 一带的，是派生状态；出现在事件回调里的，多半是交互或记忆状态。

**需要观察的现象**：你会发现「记忆状态」字段的注释几乎都在解释*为什么要留存量*——这正是全量重建架构的成本所在。

**预期结果**：32 个字段全部落位，其中派生状态极少（约 2 个），记忆与交互状态占大头。

**待本地验证**：无（纯源码阅读型实践）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `thread_last_accessed` 不能在 `rebuild_contents` 里顺便刷新？

**参考答案**：因为重建会被后台数据变化（元数据存储 observe、git 事件等）频繁触发，若在重建中刷新时间戳，「MRU 最近使用」就变成了「最近被数据变更波及」。注释（[sidebar.rs:L757-L760](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L757-L760)）明确约定它只由显式用户动作更新，这类信息无法从世界状态推导，所以必须作为记忆状态存字段。

**练习 2**：`width` 字段和 `contents.entries` 都是「列表看起来什么样」的一部分，为什么前者不需要重建管线维护？

**参考答案**：`width` 是用户直接拖拽设置的几何属性，来源单一且与数据无关，只需在 `set_width` 里钳制并 `cx.notify()`；而 `entries` 依赖工作区、项目、git、元数据、面板等一大片外部状态，任何一处变化都要重推导，所以交给重建管线（u3 单元展开）。

**练习 3**：`_subscriptions` 字段为什么构造时是空的，而不是把 `Sidebar::new` 里注册的订阅都存进去？

**参考答案**：构造期注册的订阅都调用了 `.detach()`，生命周期不依赖字段持有（见 4.2.3）；只有「需要动态成对增删」的订阅才值得占一个字段，比如归档视图的订阅（视图关闭时要随视图一起失效）与草稿编辑器观察（每次重建后要重连）。

### 4.2 构造函数 Sidebar::new：构造期注册的全部订阅

#### 4.2.1 概念说明

`Sidebar::new` 是整个 crate 的**接线图**。它做的事情可以概括为一句话：把 Sidebar 实体接到四个事件源上，然后把剩下的事交给重建管线。

四个直接事件源是：

1. **宿主 `MultiWorkspace`**——用 `cx.subscribe_in` 订阅其 `MultiWorkspaceEvent`；
2. **搜索框 `filter_editor`**——用 `cx.subscribe` 订阅其 `EditorEvent`；
3. **重命名框 `thread_rename_editor`**——同样订阅 `EditorEvent`；
4. **两个全局元数据存储**——`ThreadMetadataStore` 与 `TerminalThreadMetadataStore`，用 `cx.observe` 观察其任何变更。

此外还有三件不属于订阅但同属构造期的杂务：注册焦点进入回调（`cx.on_focus_in`）、启动一个查询「其他发布通道有哪些可导入线程」的异步任务（`cx.spawn`）、以及一个延迟到构造结束后的补订+首刷（`cx.defer_in`，4.3 专门讲）。

#### 4.2.2 核心流程

`Sidebar::new` 从上到下的执行顺序：

```text
1. cx.focus_handle() 创建焦点句柄
2. cx.on_focus_in(&focus_handle, ..., Self::focus_in)   ← 焦点进入回调
3. AgentThreadWorktreeLabelFlag::watch(cx)              ← 监视 feature flag
4. 创建两个子编辑器（filter_editor / thread_rename_editor）
5. subscribe_in(&multi_workspace)      ← 宿主四类事件
6. subscribe(&filter_editor)           ← BufferEdited → 过滤刷新
7. subscribe_in(&thread_rename_editor) ← 重命名编辑器事件
8. observe(&ThreadMetadataStore::global)          ← 线程元数据任何变更
9. observe(&TerminalThreadMetadataStore::global)  ← 终端元数据任何变更
10. cx.spawn(channels_with_threads...)  ← 异步填充导入横幅数据
11. cx.defer_in(window, ...)            ← 延迟：补订已有工作区 + 首次刷新
12. 返回字段全部就位的 Self
```

宿主事件的语义（四个变体）：

| MultiWorkspaceEvent 变体 | 何时发出 | Sidebar 的响应 |
| --- | --- | --- |
| `ActiveWorkspaceChanged` | 活跃工作区切换时 | 同步活跃条目 + 替换已归档的面板线程 + 排刷新 |
| `WorkspaceAdded(Entity<Workspace>)` | 新工作区加入窗口 | 对该工作区建立二级订阅 + 排刷新 |
| `WorkspaceRemoved(EntityId)` | 工作区移除 | 排刷新 |
| `ProjectGroupsChanged` | 项目分组键变化 | 排刷新 |

#### 4.2.3 源码精读

构造函数入口与签名：

> [sidebar.rs:L795-L811](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L795-L811) —— 注意参数形态：`multi_workspace: Entity<MultiWorkspace>`（强引用，只在构造期间使用）、`window: &mut Window`、`cx: &mut Context<Self>`。第一件事就是 `cx.focus_handle()` 拿焦点句柄并注册 `focus_in` 回调；随后创建两个单行 `Editor`，搜索框带占位文本 `"Search threads…"`。

焦点进入回调做的是「焦点转发」：

> [sidebar.rs:L800-L802](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L800-L802) 与 [sidebar.rs:L3266-L3279](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L3266-L3279) —— 当侧边栏容器本身获得焦点时：归档视图下若没有选中项，把焦点交给归档视图的过滤编辑器；线程列表下若 `selection` 为空，把焦点交给搜索框。也就是说「侧边栏获得焦点」默认落在搜索框上，而不是某一行。

宿主订阅——本 crate 最重要的一条订阅：

> [sidebar.rs:L813-L832](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L813-L832) —— `cx.subscribe_in(&multi_workspace, window, ...)`，回调参数依次是 `this`（Sidebar 自身）、`_multi_workspace`（事件源实体）、`event`、`window`、`cx`。四个变体的响应见表格。注意 `WorkspaceAdded` 分支调用 `this.subscribe_to_workspace(...)`——二级订阅网络的入口（4.4 详述）。

事件枚举定义在宿主一侧：

> [multi_workspace.rs:L109-L116](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/workspace/src/multi_workspace.rs#L109-L116) —— `MultiWorkspaceEvent` 的四个变体。发射点分散在 multi_workspace.rs 各处：`WorkspaceAdded` 在 L776、`ActiveWorkspaceChanged` 在 L1368、`WorkspaceRemoved` 在 L1421、`ProjectGroupsChanged` 在 L910/L932/L975。

搜索框订阅——「输入即过滤」：

> [sidebar.rs:L834-L843](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L834-L843) —— 只关心 `EditorEvent::BufferEdited`（文本变化）。读到最新查询文本后：若非空，先 `this.selection.take()` 清掉键盘选中（过滤后的列表下标已经失效），再 `schedule_update_entries(!query.is_empty(), cx)`——第二个参数是「刷新后选中第一条」，让输入完立刻有高亮行。

重命名框订阅与两个存储观察：

> [sidebar.rs:L845-L865](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L845-L865) —— 重命名框的事件统一转给 `handle_thread_rename_editor_event`（状态机在 u5-l4 展开）。两个 `cx.observe` 则是「存储变了就刷新」：`ThreadMetadataStore` / `TerminalThreadMetadataStore` 都是全局实体（`::global(cx)`），线程标题、归档标记、路径归属等任何增删改都会 notify，从而触发观察回调。

异步任务与延迟块：

> [sidebar.rs:L867-L887](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L867-L887) —— 先看 `cx.spawn` 段：`channels_with_threads(cx)` 返回一个 future，`cx.spawn(async move |this, cx| ...)` 在前台异步等待它完成，然后 `this.update(cx, ...)` 把结果写进 `cross_channel_import_channels` 并 `cx.notify()`——这是「导入横幅」数据的来源。这里的 `this` 是 `WeakEntity<Sidebar>`，`update` 返回 `Result`，`.ok()` 静默忽略「实体已释放」的情况。`cx.defer_in` 段留到 4.3 精读。

最后，构造函数返回完整的字段初始化：

> [sidebar.rs:L889-L923](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L889-L923) —— 所有交互/记忆状态从零值出发：`selection: None`、`active_entry: None`、各 HashMap 为空、`width: DEFAULT_WIDTH`（300px）、`list_state` 以 0 个条目初始化、`view` 取默认值（`SidebarView::ThreadList`）。

**关于 `.detach()`**：上面每条 `subscribe` / `observe` / `on_focus_in` 的返回值（`Subscription`）都立刻调用了 `.detach()`。`Subscription` 被 drop 时会注销回调，`.detach()` 让订阅摆脱这个「谁持有谁负责」的约束，其存活与订阅双方的实体生命周期绑定。效果上等价于「构造期注册、与实体共存亡」；代价是你不能手动注销它——这正是需要动态增删的订阅（草稿编辑器观察等）必须存进字段的原因。

#### 4.2.4 代码实践

**实践目标**：用日志验证构造期订阅在真实测试运行中是否被触发、按什么顺序触发。

**操作步骤**：

1. 在 [sidebar.rs:L817](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L817) 起的 `match event` 四个分支里各加一行 `dbg!("MultiWorkspaceEvent", event);`（或 `log::info!`）。
2. 在两个 `cx.observe` 回调（[sidebar.rs:L854-L865](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L854-L865)）里各加一行 `dbg!("metadata store observed");`。
3. 在 `defer_in` 闭包（[sidebar.rs:L879-L887](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L879-L887)）末尾加 `dbg!("deferred init ran");`。
4. 运行 u1-l2 学过的命令：`cargo test -p sidebar --lib test_single_workspace_no_threads -- --nocapture`。
5. 观察完**还原全部改动**（这是本地实验，不要提交）。

**需要观察的现象**：`deferred init ran` 应当出现且早于/伴随首次元数据 observe；`MultiWorkspaceEvent` 各分支是否出现取决于测试场景里是否发生工作区增删。

**预期结果**：你能亲眼确认「构造 → 延迟补订 → 首次刷新」这条链在测试里真实跑过。

**待本地验证**：具体输出顺序依赖测试场景，请以本机运行结果为准。

#### 4.2.5 小练习与答案

**练习 1**：订阅 `multi_workspace` 用 `subscribe_in`，订阅 `filter_editor` 却用 `subscribe`，为什么？

**参考答案**：`subscribe_in` 的回调多一个 `window: &mut Window` 参数。宿主事件的响应里要做窗口级操作（`replace_archived_panel_thread` 需要在窗口里创建草稿、`subscribe_to_workspace` 需要把 window 继续传下去）；而搜索框回调只改自身状态和排刷新，不需要窗口。

**练习 2**：两个元数据存储用 `observe` 而不是 `subscribe`，差别在哪？

**参考答案**：`cx.subscribe` 接收**具名事件**（`EventEmitter<E>` 发出的 `E`），`cx.observe` 在被观察实体每次 `cx.notify()` 后触发，不关心具体事件。元数据存储的变更类型很多（标题改了、归档了、路径迁了……），Sidebar 对每一种的响应都一样——「排一次刷新」，所以 observe 更贴切也更省事。

**练习 3**：如果 `WorkspaceAdded` 分支忘记调用 `subscribe_to_workspace`，会出现什么症状？

**参考答案**：新加入的工作区的项目事件（工作树增删、路径变化）、git 事件、面板事件都收不到，列表内容会「卡」在加入前的状态，直到某个其他事件源（比如元数据存储变更）碰巧触发一次全量重建。由于重建是从世界状态重推导的，数据不会永久丢失，但刷新会明显滞后——这正是「订阅决定何时刷新、重建决定内容」分层的体现。

### 4.3 WeakEntity 与 defer_in：初始化为什么要「弱」和「迟」

#### 4.3.1 概念说明

**为什么宿主是弱引用？** `Sidebar` 是 `MultiWorkspace` 的子组件。装配关系（u1-l1 已走读）是：zed.rs 里 `observe_new(MultiWorkspace)` → 创建 `Entity<Sidebar>` → `MultiWorkspace::register_sidebar` 把它存进自己的字段。也就是说**宿主持有子的强引用**。若 `Sidebar` 再强引用回宿主，就形成强引用环，两个实体都无法释放。所以字段声明是 `multi_workspace: WeakEntity<MultiWorkspace>`，每次使用都要 `.upgrade()` 并处理 `None`。

**为什么初始化要延迟一拍？** 有两个原因，都源于「Sidebar 构造时世界已经存在」这个事实：

1. **补订错过的事件**。侧边栏是在 `MultiWorkspace`（及其工作区）之后创建的。构造之前就已经加入的工作区，它们的 `WorkspaceAdded` 事件早就发出去了，Sidebar 无从收到。延迟闭包遍历 `multi_workspace.read(cx).workspaces()`，为每个已有工作区补上二级订阅。
2. **首刷时机**。构造函数还在 `cx.new(|cx| ...)` 闭包内部，实体尚未返回、尚未 `register_sidebar`。把首次 `schedule_update_entries` 推迟到本轮效果周期结束后，保证它运行时 Sidebar 已经是「已注册、可更新」的完整实体，避免在构造中途重入。

#### 4.3.2 核心流程

初始化时序：

```text
zed.rs: observe_new(MultiWorkspace)
  └─ defer → cx.new(Sidebar::new)
       ├─ 注册 4 类直接订阅（此时只能收到「未来」的事件）
       ├─ defer_in(闭包 A) 入队          ← 闭包 A 捕获 WeakEntity
       └─ 返回 Self（字段零值初始化）
  └─ register_sidebar(sidebar)           ← 契约 trait 装配
...本轮效果周期结束...
闭包 A 执行：
  ├─ deferred_multi_workspace.upgrade() 成功？
  │    ├─ 是 → 遍历已有 workspaces → subscribe_to_workspace（补订）
  │    └─ 否 → 跳过（宿主已释放，安全退出）
  └─ schedule_update_entries(false)      ← 首次全量重建
```

弱引用的日常使用模式（贯穿整个 sidebar.rs）：

```text
需要宿主时：
  self.multi_workspace.upgrade() → Option<Entity<MultiWorkspace>>
    ├─ Some(mw) → mw.read(cx) 只读 / mw.update(cx, ...) 修改
    └─ None     → 宿主已释放，安静返回（不 panic）
```

#### 4.3.3 源码精读

字段声明与延迟块：

> [sidebar.rs:L735](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L735) —— `multi_workspace: WeakEntity<MultiWorkspace>`，结构体的第一个字段就是弱引用宿主。

> [sidebar.rs:L878-L887](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L878-L887) —— `let deferred_multi_workspace = multi_workspace.downgrade();` 先复制一份弱引用给闭包捕获（强引用参数 `multi_workspace` 在 L890 还要用于初始化返回值 `Self` 的同名字段，不能被闭包 move 走；弱引用同时解决了所有权与生命周期两个问题）。`cx.defer_in(window, move |this, window, cx| ...)` 中 `upgrade()` 成功才补订，最后无条件 `schedule_update_entries(false, cx)` 触发首刷。

弱引用的三种典型用法，都在构造函数附近：

> [sidebar.rs:L930-L939](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L930-L939) —— 只读 + 默认值：`upgrade().and_then(|mw| ...).unwrap_or(false)`，宿主没了就当「未折叠」处理。

> [sidebar.rs:L941-L950](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L941-L950) —— 修改宿主：`if let Some(mw) = self.multi_workspace.upgrade() { mw.update(cx, ...) }`。这里顺便能看到一个设计决策：**折叠状态存在 MultiWorkspace 上**（`group_state_by_key`），不在 Sidebar 上——这样折叠能被宿主持久化，且多个观察者共享。

> [sidebar.rs:L7374-L7378](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L7374-L7378) —— 便捷方法 `active_workspace`：`upgrade().map(|w| w.read(cx).workspace().clone())`，返回 `Option<Entity<Workspace>>`。

作为对照，看宿主一侧怎么「强持有」侧边栏：

> [multi_workspace.rs:L393-L405](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/workspace/src/multi_workspace.rs#L393-L405) —— `register_sidebar`：宿主 observe 侧边栏（任何 notify 都让宿主重渲染）、订阅 `SidebarEvent::SerializeNeeded`（侧边栏要持久化时宿主负责落盘），最后 `self.sidebar = Some(Box::new(sidebar))` 以 `Box<dyn SidebarHandle>` 强持有。子弱父强，环不存在。

`schedule_update_entries` 的合并语义也值得在此一提（首刷走的就是它）：

> [sidebar.rs:L1974-L1990](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L1974-L1990) —— 若已有排队的刷新且本次不要求「选中首项」，直接返回（合并）；否则 `cx.spawn` 一个任务，任务体内先清 `update_task` 再执行 `update_entries`。由于任务要等当前效果周期结束后才被泵到，密集事件天然被合并成一次重建。

#### 4.3.4 代码实践

**实践目标**：体会在构造函数里「直接执行」与「defer 执行」的差异，并统计弱引用的使用密度。

**操作步骤**：

1. 用 Grep 在 `crates/sidebar/src/sidebar.rs` 中搜索 `multi_workspace.upgrade()`，统计出现次数并记录每处所在的函数名。
2. 阅读其中三处（`is_group_collapsed`、`set_group_expanded`、`active_workspace`），确认它们都对 `None` 做了处理。
3. （可选本地实验）把 [sidebar.rs:L879-L887](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L879-L887) 的 `cx.defer_in(window, move |this, window, cx| {...})` 改成直接内联执行其函数体（去掉 defer 包装），运行 `cargo test -p sidebar --lib`，观察哪些测试失败或行为变化；观察完还原。

**需要观察的现象**：步骤 1 应得到 20 处以上的匹配（本 crate 对宿主的访问全部经过弱引用升级）；步骤 3 中内联执行可能导致构造期间重入更新（`update_task` 的 spawn 会在实体尚未完成注册时运行），测试行为可能异常。

**预期结果**：你会确信「弱引用 + upgrade 判空」是这个 crate 访问宿主的唯一姿势，没有任何 `unwrap` 赌宿主一定存活。

**待本地验证**：步骤 3 的具体失败形态请以本机运行结果为准。

#### 4.3.5 小练习与答案

**练习 1**：`defer_in` 的闭包为什么捕获的是 `downgrade()` 之后的弱引用，而不是直接 move 强引用参数？

**参考答案**：其一，`multi_workspace: Entity<MultiWorkspace>` 参数在函数末尾还要用来初始化 `Self` 的构造（L890 `multi_workspace: multi_workspace.downgrade()`），move 进闭包会引发所有权冲突；其二，强引用会延长宿主生命周期，即便宿主已被销毁闭包仍吊着它，与弱引用设计初衷相悖。弱引用让闭包可以安全地「迟到」——宿主没了就 `upgrade()` 失败跳过。

**练习 2**：把首刷从 `defer_in` 挪到 `update_entries` 被动等待第一个事件触发，行不行？

**参考答案**：不行。打开侧边栏时世界可能完全静止——没有任何新事件会到来，列表将一直空白，直到用户做出某个动作。初始化必须主动推一次重建，才能把「已存在」的工作区与线程渲染出来。

**练习 3**：`Sidebar` 持有 `filter_editor: Entity<Editor>` 是强引用，为什么不怕环？

**参考答案**：方向决定一切。子编辑器由 Sidebar 强持有，但子编辑器并不持有 Sidebar（编辑器对宿主一无所知，通信靠 Sidebar 单方面订阅编辑器事件）。环只出现在「互相强持有时」；单向强持有是 GPUI 中的标准父子关系，随父销毁、无泄漏。

### 4.4 延伸订阅网络：subscribe_to_workspace 与它的朋友们

#### 4.4.1 概念说明

`Sidebar::new` 只订阅了宿主和两个子编辑器。真正「细粒度」的事件——某个工作树加进了项目、git 分支切了、Agent 面板换了活跃视图——来自每个 `Workspace` 内部。这些订阅不是构造期建立的，而是**动态建立**的：

- 收到 `WorkspaceAdded` 事件时，对该新工作区调用 `subscribe_to_workspace`；
- `defer_in` 补订已有工作区时，同样调用它；
- 工作区的 `PanelAdded` 事件带来 `AgentPanel` 时，调用 `subscribe_to_agent_panel`；
- 每次重建后，`refresh_draft_editor_observations` 重连所有草稿消息编辑器的观察。

这构成一张**二级订阅网络**：一级订阅（构造期）负责「世界结构变了」（哪些工作区、哪些面板），二级订阅（动态）负责「世界内容变了」（工作树、git、面板视图、草稿内容）。

#### 4.4.2 核心流程

```text
Sidebar::new（一级）
├── MultiWorkspaceEvent::WorkspaceAdded ──→ subscribe_to_workspace(ws)
└── defer_in：遍历已有 workspaces ──────→ subscribe_to_workspace(ws)

subscribe_to_workspace(ws)（二级，每个工作区一次）
├── subscribe(project)      ProjectEvent::Worktree* → 排刷新
│                            WorktreePathsChanged → move_entry_paths + 排刷新
├── subscribe(git_store)    RepositoryUpdated(GitWorktreeListChanged|HeadChanged) → 排刷新
├── subscribe(workspace)    Event::PanelAdded(AgentPanel) → subscribe_to_agent_panel + 排刷新
├── observe_docks(workspace) 每个 dock 变化 → （仅活跃工作区）cx.notify
└── 若已存在 AgentPanel ────→ subscribe_to_agent_panel(立即)

subscribe_to_agent_panel（三级，每个面板一次）
├── ActiveViewChanged / ActiveViewFocused / EntryChanged → 同步活跃条目 + 排刷新
├── TerminalCloseRequested → close_terminal
└── ThreadInteracted → record_thread_interacted + 排刷新

refresh_draft_editor_observations（每次 update_entries 后重连）
└── 对每个活跃会话视图：订阅 message_editor 的 Edited + 订阅 ConversationView 的 StateChange
```

#### 4.4.3 源码精读

`subscribe_to_workspace` 的完整接线：

> [sidebar.rs:L958-L985](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L958-L985) —— 开头有个守卫：`is_via_collab()` 的项目（他人远程共享的项目）直接跳过订阅。随后订阅 `ProjectEvent`：工作树增删/重排只排刷新；`WorktreePathsChanged` 特殊——先 `move_entry_paths` 把两个元数据存储里的路径归属迁移到新路径，再排刷新（否则重建时会按旧路径查不到线程）。`move_entry_paths` 本体在 [sidebar.rs:L1028-L1088](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L1028-L1088)，它对比新旧路径集合，把增删分别应用到 `ThreadMetadataStore` 与 `TerminalThreadMetadataStore` 的 `change_worktree_paths`。

> [sidebar.rs:L987-L1005](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L987-L1005) —— git 订阅只关心两种 `RepositoryUpdated` 子事件：worktree 列表变化（影响 linked worktree 行）与 HEAD 变化（影响分支名标签）。

> [sidebar.rs:L1007-L1025](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L1007-L1025) —— 订阅工作区事件只为 `PanelAdded`：只有当新面板能向下转型为 `AgentPanel` 时才接三级订阅。末尾的 `if let Some(agent_panel)` 处理「面板先于侧边栏存在」的情况——与补订工作区同理，错过的事件要靠主动查询追回。

面板订阅与停靠区观察：

> [sidebar.rs:L1090-L1121](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L1090-L1121) —— `subscribe_to_agent_panel`：面板的活跃视图变化驱动 `sync_active_entry_from_panel`（维护 `active_entry` 高亮）；`TerminalCloseRequested` 走关闭终端链路；`ThreadInteracted` 记录交互时间戳（MRU 数据来源之一）。

> [sidebar.rs:L1223-L1245](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L1223-L1245) —— `observe_docks`：对工作区的每个 dock 注册 `cx.observe`，dock 一变且该工作区是**活跃**工作区就 `cx.notify()`。这让停靠区布局变化（例如面板尺寸）即时反映到侧边栏渲染，而不必等全量重建。

草稿编辑器观察——唯一「存字段」的动态订阅：

> [sidebar.rs:L2113-L2147](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L2113-L2147) —— `refresh_draft_editor_observations` 先 `clear()` 掉旧订阅，再遍历所有会话视图，对每个活跃线程的消息编辑器订阅 `MessageEditorEvent::Edited`（草稿有内容了 → 影响草稿行的显示与排序），**同时**订阅会话视图本身的 `StateChange`——注释解释了原因：生命周期转换（Loading → Connected）时编辑器实体会被**替换**，只订旧编辑器会失联，必须连视图一起订才能感知换编辑器这件事。这个方法在每次 `update_entries` 里被调用（见 [sidebar.rs:L2005-L2007](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L2005-L2007)），这就是 `_draft_editor_observations` 字段存在的原因：订阅集合要整体换血，不能 detach 了之。

#### 4.4.4 代码实践

**实践目标**：完整跟踪一条二级订阅调用链，理解「事件源 → 回调 → 副作用」如何落到代码行。

**操作步骤**：

1. 从 [sidebar.rs:L978-L981](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L978-L981) 的 `ProjectEvent::WorktreePathsChanged` 分支出发，进入 `move_entry_paths`（L1028 起），一路读到两个 `change_worktree_paths` 调用（L1072-L1087）。
2. 写下这条链的时序说明：谁发出事件 → 回调先做什么 → 再做什么 → 最终谁触发重建。
3. 用 Grep 在 `sidebar_tests.rs` 中搜索 `WorktreePathsChanged` 或相关测试名（如含 `rename_worktree`、`move` 字样的测试），找到覆盖这条链的测试并阅读其断言。

**需要观察的现象**：`move_entry_paths` 里对「新增对」和「移除路径」的差集计算（L1041-L1057）——只有路径真的变了才会去动两个存储，否则提前 return。

**预期结果**：你能用四五行文字讲清「工作树路径变化后，线程元数据的路径归属如何跟随迁移、列表如何随之刷新」。

**待本地验证**：步骤 3 的具体测试名以仓库当前内容为准。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `refresh_draft_editor_observations` 要在每次重建后重跑，而不是构造时跑一次？

**参考答案**：草稿消息编辑器是会话视图的内部实体，会随会话生命周期（Loading → Connected、切换线程）不断创建和替换，「当前有哪些编辑器」本身就是随世界变化的集合。构造时订一次只会订到当时那批编辑器，之后的都失联。所以每次重建清空重连，并用 `_draft_editor_observations` 字段持有这批短命订阅。

**练习 2**：`subscribe_to_workspace` 开头为什么要跳过 `is_via_collab()` 的项目？

**参考答案**：collab（他人远程共享）项目的工作树与 git 状态由远端权威决定，本地订阅这些细粒度事件没有意义（且这些项目在多项目侧边栏中的呈现方式不同，不参与本地线程归属）。跳过订阅避免无意义的回调与刷新。

**练习 3**：二级订阅全部 `.detach()` 了，那么工作区被移除后，它上面的旧订阅怎么办？

**参考答案**：GPUI 的订阅在**任一端实体释放**后自动失效——工作区销毁，挂在它上面的订阅随之消亡，不会泄漏也不会回调。这也是 `detach` 模式安全的前提：订阅的生命周期由订阅双方实体的存活期兜底，而不是靠手动注销。

## 5. 综合实践

**任务**：手绘一张「事件源 → 订阅回调 → 副作用」总表，把本讲遇到的**全部** 12 个订阅/观察点整理进去，并标注每个订阅的注册时机（构造期 / `defer_in` 补订 / 事件驱动动态注册 / 每次重建重连）。

**事件源清单**（按出场顺序，全部要在表中出现）：

1. `focus_handle`（`on_focus_in`）
2. `MultiWorkspace`（`subscribe_in`）
3. `filter_editor`（`subscribe`）
4. `thread_rename_editor`（`subscribe_in`）
5. `ThreadMetadataStore::global`（`observe`）
6. `TerminalThreadMetadataStore::global`（`observe`）
7. `channels_with_threads` future（`spawn`，严格说是任务不是订阅，也请列出）
8. `project`（`subscribe_in`，位于 `subscribe_to_workspace`）
9. `git_store`（`subscribe_in`）
10. `workspace` 的 `PanelAdded`（`subscribe_in`）
11. `AgentPanel`（`subscribe_in`，位于 `subscribe_to_agent_panel`）
12. dock（`observe`，位于 `observe_docks`）与草稿消息编辑器 / `ConversationView`（`subscribe`，位于 `refresh_draft_editor_observations`）

**表格模板**（示例两行）：

| 注册时机 | 事件源 | 事件/触发条件 | 回调位置 | 副作用 |
| --- | --- | --- | --- | --- |
| 构造期 | `MultiWorkspace` | `WorkspaceAdded` | sidebar.rs:L822-L825 | `subscribe_to_workspace` + 排刷新 |
| `defer_in` 补订 | 每个 `project` | `WorktreePathsChanged` | sidebar.rs:L978-L981 | `move_entry_paths` 迁移两个存储的路径归属 + 排刷新 |

**验收标准**：

- 12 行全部填满，回调位置能给到行号；
- 「注册时机」列能正确区分：1-7 在 `Sidebar::new` 构造期注册；8-10 经 `subscribe_to_workspace` 注册，而该函数的调用点只有两处——`WorkspaceAdded` 回调（事件驱动）与 `defer_in` 闭包（补订）；11 在 `PanelAdded` 回调或 `subscribe_to_workspace` 末尾的「面板已存在」分支注册；12 中的草稿编辑器观察每次 `update_entries` 后重连；
- 能口头回答：为什么 5、6 两个观察者足以让「线程改标题」这类变化最终反映到列表？（答：存储 notify → observe 触发 → 排刷新 → 全量重建从存储读最新值。）

**待本地验证**：无需运行命令；若想验证某行，可参照 4.2.4 的日志法抽查。

## 6. 本讲小结

- `Sidebar` 结构体共 32 个字段，按「派生 / 记忆 / 交互 / 句柄与订阅」四类理解最清晰：派生状态（`contents` 等）随时可重建，记忆状态（`live_thread_statuses`、`thread_last_accessed` 等）弥补全量重建「看不到上一刻」的盲区，交互状态（`selection`、重命名三件套等）描述用户当下的操作。
- `Sidebar::new` 是全 crate 的接线图：注册焦点回调、监视 feature flag、创建两个子编辑器、订阅宿主四类事件、订阅两个子编辑器、观察两个全局元数据存储、启动导入通道查询任务，最后经 `defer_in` 补订已有工作区并触发首刷。
- 订阅 API 的选型有规律：需要窗口操作的用 `subscribe_in`，只改自身状态的用 `subscribe`，对「变了就行」的 Store 用 `observe`；构造期订阅一律 `.detach()`，需要动态增删的才存进 `_subscriptions` / `_draft_editor_observations` 等字段。
- 宿主以 `WeakEntity` 持有：子弱父强避免引用环，全部访问经 `upgrade()` 判空；`defer_in` 则解决「构造时世界已存在」的两个问题——补订错过的 `WorkspaceAdded`、把首刷推到实体注册完成之后。
- 二级订阅网络（`subscribe_to_workspace` → `subscribe_to_agent_panel` / `observe_docks` / `refresh_draft_editor_observations`）负责细粒度事件；一级订阅决定「世界结构」，二级订阅决定「世界内容」，而所有路径殊途同归于 `schedule_update_entries`。

## 7. 下一步学习建议

下一讲（u2-l1「列表行模型」）进入数据侧：`ListEntry` 的三种变体（项目分组头、线程、终端）、`ThreadEntry` / `TerminalEntry` 的字段构成，以及 `SidebarContents` 如何在重建时承载它们。本讲你已经知道 `contents: SidebarContents` 是重建的产物，下一讲就去看产物长什么样。

后续两条线索可以提前 bookmark：本讲的订阅清单将在 u3-l1「事件订阅网络」中逐条深挖（尤其是 `move_entry_paths` 的路径迁移与 `refresh_draft_editor_observations` 的重连时机）；`selection` 与 `active_entry` 的辨析将在 u2-l3 展开。阅读源码时，建议把 [sidebar.rs:L730-L792](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L730-L792) 的结构体定义和 [multi_workspace.rs:L122-L161](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/workspace/src/multi_workspace.rs#L122-L161) 的契约 trait 定义放在手边，它们分别是「侧边栏有什么」和「宿主要什么」的两张速查表。

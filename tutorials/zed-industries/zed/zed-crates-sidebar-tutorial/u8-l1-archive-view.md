# 归档视图切换与 ThreadsArchiveView 集成

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `SidebarView` 两种形态（`ThreadList` / `Archive`）各自的含义，以及归档视图作为**子实体**嵌入渲染的方式。
2. 逐条列出 `show_archive` / `show_thread_list` 切换时发生的全部副作用（创建实体、订阅事件、焦点迁移、触发持久化）。
3. 讲清 `open_thread_from_archive` 如何把一条历史线程重新拉回活跃状态，包括恢复被归档工作树的慢路径。
4. 解释 `replace_archived_panel_thread` 这个一致性护栏解决的工作区切换问题。
5. 理解 `SerializedSidebarView` 上的 `#[serde(alias = "Archive")]` 背后的向后兼容策略，并能验证它。

## 2. 前置知识

本讲是 advanced 层第一讲，默认你已完成单元一~单元七。用到的前置概念：

- **实体与弱引用**（u1-l3）：`Entity<T>` 是状态句柄，`WeakEntity<T>` 防引用环；子实体被 `child()` 进元素树后由 gpui 渲染。
- **render() 四层骨架**（u4-l1）：`Sidebar::render` 是「头部 → 列表主体 → 导入横幅 → 底部栏」的竖排树；本讲的关键正是「列表主体」这一层会被归档视图整体替换。
- **重建管线**（u3-l2）：主列表的一切变化经 `schedule_update_entries` 汇入 `update_entries` 全量重推导。归档视图**不走**这条管线——它自带一套迷你刷新循环，这是本讲的一个重要对比点。
- **线程激活三岔决策**（u6-l1）：`activate_thread_locally`（本窗口）、`activate_thread_in_other_window`（其他窗口）、`open_workspace_and_activate_thread`（先开工作区）三条路径。`open_thread_from_archive` 复用的正是这套决策。
- **序列化链路**（u1-l1）：侧边栏不自己持久化，`cx.emit(SidebarEvent::SerializeNeeded)` 通知宿主 `MultiWorkspace` 落盘。
- **serde 基础**：Rust 枚举单元变体默认序列化为字符串（如 `"History"`）；`#[serde(alias = "...")]` 只扩大**反序列化**能接受的名字集合，不改变序列化输出。

**一个必须先澄清的命名陷阱**：「archive」在 Zed 里有两个含义——

| 层面 | 名字 | 含义 |
|---|---|---|
| UI 文案 | Thread History（线程历史） | 面向用户的历史列表视图 |
| 运行时枚举 | `SidebarView::Archive` | 侧边栏当前形态的变体名 |
| 序列化枚举 | `SerializedSidebarView::History` | 持久化用的变体名 |
| 动作 | `ToggleThreadHistory` | 切换动作（原名 `ViewAllThreads`） |

之所以这么乱，是因为 2026-04 的提交 `3dfbfc8fff`（"Rename Archive view to Thread History (#54075)"）把视图在**用户可见层面**改名为 Thread History——理由是这张列表同时展示活跃与已归档线程，叫 "Archive" 名不副实；但「归档线程」这个动词/状态（`ThreadMetadata.archived`）保持不变，运行时枚举也没跟着改。本讲遵循源码现状：谈视图时用「归档视图 / 历史视图」，谈动作时用 `Archive`。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| `crates/sidebar/src/sidebar.rs` | 主战场：`SidebarView` 枚举、`show_archive` / `show_thread_list` / `toggle_archive`、`open_thread_from_archive`、`replace_archived_panel_thread`、序列化实现 |
| `crates/agent_ui/src/threads_archive_view.rs` | 被嵌入的子实体 `ThreadsArchiveView`：自带过滤框、虚拟列表、时间分桶、五类事件 |
| `crates/sidebar/src/sidebar_tests.rs` | `test_serialization_round_trip` 与 `test_restore_serialized_archive_view_does_not_panic` 两个防回归测试 |
| `crates/workspace/src/multi_workspace.rs` | `Sidebar` 契约 trait、`SidebarEvent::SerializeNeeded` 的消费点、`sidebar_state` 落盘 |
| `crates/workspace/src/workspace.rs` | 窗口恢复时调用 `restore_serialized_state` 的时序环境 |

## 4. 核心概念与源码讲解

### 4.1 SidebarView：侧边栏的两种形态

#### 4.1.1 概念说明

`Sidebar` 是一个实体（u1-l3），但它的「主体内容」有两种互斥形态：默认的线程列表（含项目分组头、线程行、终端行——单元二~四的全部内容），以及历史视图（把所有线程按时间平铺、可搜索）。用枚举表达这种互斥：

```rust
#[derive(Debug, Default)]
enum SidebarView {
    #[default]
    ThreadList,
    Archive(Entity<ThreadsArchiveView>),
}
```

关键设计决策：`Archive` 变体持有的是**子实体的句柄**，而不是把归档视图的字段平铺进 `Sidebar`。这意味着：

- 归档视图是一个自治组件：有自己的 `ListState`、`selection`、`filter_editor`、订阅集合，甚至自己的刷新管线。
- `Sidebar` 对它只做三件事：创建（`cx.new`）、订阅（转发事件）、作为孩子渲染（`child(entity.clone())`）。
- 切回 `ThreadList` 时只需丢弃句柄——旧实体连同它的订阅一起被回收，不留任何残留状态。

注意 `SidebarView` 只派生 `Debug, Default`，没有 `Clone`、没有 `PartialEq`——它不是数据，是「当前挂着一个什么实体」的运行时标记。

#### 4.1.2 核心流程

`view` 字段如何影响一帧的渲染：

```text
render()
  ├─ 根容器（key_context、焦点、24 个 on_action）        ← 两形态共享
  ├─ match self.view
  │    ├─ ThreadList → 头部 + (空态 | 虚拟列表+粘性头+滚动条)  ← 主列表全家桶
  │    └─ Archive(e)  → child(e.clone())                    ← 整体替换为子实体
  ├─ 导入横幅（ACP / 跨通道）                              ← 两形态共享
  └─ 底部栏（折叠按钮、历史按钮、最近项目）                  ← 两形态共享
```

除了渲染，`view` 还渗透进三个交互通道：

1. **键位上下文**：归档视图的过滤框获得焦点时，`dispatch_context` 同样报告 `searching` 身份——两套搜索框对键位表呈现同一副面孔。
2. **默认焦点路由**：外部切入侧边栏时（`focus_in`），若当前是归档视图且没有键盘选中行，默认焦点给归档过滤框；否则给主过滤框。
3. **宿主查询**：`is_threads_list_view_active` 把当前形态暴露给 `MultiWorkspace`（trait 默认值恒为 `true`，只有本 crate 覆写）。

#### 4.1.3 源码精读

枚举与字段定义——[crates/sidebar/src/sidebar.rs:130-135](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L130-L135) 定义两种形态；[crates/sidebar/src/sidebar.rs:773-774](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L773-L774) 是 `Sidebar` 结构体上的 `view` 字段（紧挨着 `restoring_tasks`，本讲 4.3 会用到）；构造时以默认形态起步——[crates/sidebar/src/sidebar.rs:911](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L911) 写入 `SidebarView::default()`（即 `ThreadList`）。

渲染分派点——[crates/sidebar/src/sidebar.rs:7852-7886](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L7852-L7886)：对根容器 `.map(|this| match &self.view { ... })`。`ThreadList` 分支组装头部与（空态或）虚拟列表；`Archive` 分支只有一行 `this.child(archive_view.clone())`——把子实体直接挂为孩子，主体渲染完全委托。随后的 [crates/sidebar/src/sidebar.rs:7887-7902](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L7887-L7902)（导入横幅）与 `render_sidebar_bottom_bar` 在 `match` 之外，两种形态共享。

键位上下文感知归档搜索框——[crates/sidebar/src/sidebar.rs:3240-3264](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L3240-L3264)：`dispatch_context` 用 `matches!(&self.view, SidebarView::Archive(archive) if archive.read(cx).is_filter_editor_focused(...))` 判断归档搜索框是否聚焦，与主过滤框一视同仁地给出 `searching` 身份。

默认焦点路由——[crates/sidebar/src/sidebar.rs:3266-3279](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L3266-L3279)：`focus_in` 在归档形态下，若归档视图没有选中行，就把焦点转给它的过滤框——复刻了主列表「打开即搜索」的行为（u5-l1）。`FocusSidebarFilter` 动作同理——[crates/sidebar/src/sidebar.rs:3313-3330](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L3313-L3330) 清空选中后按形态分流到两套过滤框之一。

对宿主的形态汇报——[crates/sidebar/src/sidebar.rs:7691-7693](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L7691-L7693) 实现 `is_threads_list_view_active`；契约定义在 [crates/workspace/src/multi_workspace.rs:122-161](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/workspace/src/multi_workspace.rs#L122-L161) 的 `Sidebar` trait 上（默认恒 `true`，见 L128-L130）——宿主可以用它决定某些行为（如通知点）是否适用于列表视图。

#### 4.1.4 代码实践

**实践目标**：用源码清单的方式固化「共享层 vs 独占层」的边界，为下一模块理解切换副作用打基础。

1. 打开 [crates/sidebar/src/sidebar.rs:7760-7903](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L7760-L7903)，通读 `render()`。
2. 手工绘制（或抄写）成一棵缩进树，标出：
   - 根容器上注册的 `.on_action` 中，哪些在归档形态下仍然有效（提示：动作处理器集中在根容器，归档子实体有自己的 `key_context`，嵌套上下文中越靠近焦点越优先——见 u5-l1）。
   - `match &self.view` 两个分支各自的 children。
3. 回答：归档形态下 `render_sidebar_header`（含主过滤框）还渲染吗？`no_search_results` 覆盖层呢？（都渲染——它们在 `ThreadList` 分支内部。）

**预期结果**：一张两列对照表——左列「ThreadList 独占」（头部、空态/列表/粘性头/无结果覆盖层）、右列「两形态共享」（根容器与动作、导入横幅、底栏）。若想眼见为实，可在本地 `cargo run -p zed` 打开 Zed，点底栏时钟按钮观察主体切换（UI 观察「待本地验证」）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `SidebarView::Archive` 持有 `Entity<ThreadsArchiveView>` 而不是把归档列表的字段平铺进 `Sidebar`？

**答案**：归档视图是自治组件，自带过滤框、`ListState`、选中态、hover 态和一整套订阅；平铺会让 `Sidebar` 同时背两套列表状态，互相污染。持句柄则「创建即接管、丢弃即回收」，切回列表时旧实体连同订阅整体消失，符合本 crate「不维护跨视图协调状态」的一贯风格（u3-l2 的全量重推导教义）。

**练习 2**：切到归档视图后，主列表的 `schedule_update_entries` 管线还会跑吗？

**答案**：会。订阅网络（u3-l1）挂在 `Sidebar` 与全局 Store 上，与 `view` 无关——数据事件仍会触发主列表全量重建，只是结果不可见。归档视图自己 `observe` `ThreadMetadataStore` 刷新 `items`（见 4.2.3）。两条管线并行，同一时刻只有一条可见。

**练习 3**：`SidebarView` 为什么不派生 `Clone` / `PartialEq`？

**答案**：它不是值数据，是「当前挂着哪个实体」的运行时标记；`Entity` 本身可克隆，但没有克隆整个视图标记的场景，切形态永远走 `show_archive` / `show_thread_list` 的完整副作用路径（下一模块），不做无声的 `view = ...` 赋值——除这两个方法外全 crate 只有 `restore` 的延迟回调会写 `view`。

### 4.2 show_archive / show_thread_list：切换动作与完整副作用

#### 4.2.1 概念说明

切换不是一句 `self.view = ...`，而是一组精心排序的副作用。`show_archive` 要做的事：

1. **发遥测**（`Thread History Viewed`，带左右侧信息）。
2. **解析前置条件**：从宿主 `MultiWorkspace` 取活跃工作区，再取其 `AgentPanel`，从两者掏出 `agent_server_store` 与 `agent_connection_store` 的弱句柄——归档视图渲染行时要用它们查外部 agent 图标。
3. **创建子实体** `ThreadsArchiveView::new(...)`。
4. **订阅子实体的五类事件**，把归档视图内的用户意图翻译成侧边栏动作。
5. **登记订阅**到 `_subscriptions` 字段（不 `detach`！）。
6. **切换 `view`**，把默认焦点交给归档过滤框。
7. **触发持久化**（`serialize()` → 宿主落盘），最后 `cx.notify()`。

`show_thread_list` 是对称的收尾：切回 `ThreadList`、**清空 `_subscriptions`**（注销对旧归档实体的订阅）、焦点回主过滤框、再次持久化、通知。

为什么订阅必须存字段？因为归档实体会被反复创建：每次进入历史视图都是**新建**实体，旧实体只有当订阅被注销、句柄被替换后才会被回收。若 `detach` 订阅，切回列表后旧实体的事件仍会打进 `Sidebar`——幽灵回调。

`ThreadsArchiveView` 的事件契约（定义在 agent_ui，本 crate 消费）：

| 事件 | 载荷 | 侧边栏反应 |
|---|---|---|
| `Close` | — | `show_thread_list`（从归档视图内部请求退出） |
| `Activate` | `ThreadMetadata` | `open_thread_from_archive`（4.3 的主角） |
| `CancelRestore` | `ThreadId` | `restoring_tasks.remove`（取消进行中的恢复任务） |
| `Import` | — | `show_thread_import_modal("thread_history")` |
| `NewThread` | — | 先 `show_thread_list` 再 `create_new_entry` |

#### 4.2.2 核心流程

`show_archive` 的流程（含两个提前返回的守卫）：

```text
show_archive(window, cx):
    发遥测 Thread History Viewed(side)
    active_workspace = 宿主.upgrade()?.workspace()        ← 守卫 1：宿主已死则放弃
    agent_panel     = active_workspace.panel::<AgentPanel>()? ← 守卫 2：面板未就绪则放弃
    agent_server_store / agent_connection_store 取弱句柄
    archive_view    = cx.new(|cx| ThreadsArchiveView::new(...))
    订阅五类事件 → 存入 _subscriptions
    view = SidebarView::Archive(archive_view)
    archive_view.focus_filter_editor(window, cx)
    serialize(cx)          ← emit SerializeNeeded，宿主持久化「当前停在历史视图」
    cx.notify()
```

切换的四个入口 + 一个自动出口：

| 入口 | 代码位置 |
|---|---|
| 底栏时钟按钮 `on_click` | `render_sidebar_bottom_bar` |
| `ToggleThreadHistory` 动作（键位） | render 根容器 `on_action` → `toggle_archive` |
| ACP 导入横幅的 Import 按钮 | `render_acp_import_onboarding` 的 `on_import` |
| 窗口恢复（序列化状态里是 `History`） | `restore_serialized_state` 的延迟回调 |
| 自动切回 | `open_thread_from_archive` 各完成分支调用 `show_thread_list` |

#### 4.2.3 源码精读

动作定义——[crates/sidebar/src/sidebar.rs:86-94](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L86-L94)：`gpui::actions!(agents_sidebar, [NewThreadInGroup, ToggleThreadHistory])`，doc 注释说明后者「在线程列表与线程历史之间切换」。

`toggle_archive` 分流——[crates/sidebar/src/sidebar.rs:7519-7531](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L7519-L7531)：按当前形态把同一动作路由到 `show_archive` 或 `show_thread_list`。动作在根容器注册——[crates/sidebar/src/sidebar.rs:7795](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L7795)（`.on_action(cx.listener(Self::toggle_archive))`）。

`show_archive` 全文——[crates/sidebar/src/sidebar.rs:7533-7600](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L7533-L7600)：遥测在 L7538；两个守卫在 L7540-L7549；store 弱句柄与实体创建在 L7551-L7568；事件订阅在 L7570-L7593（上表五类事件的一一对应就在这个 `match` 里）；L7595-L7599 依次完成登记订阅、赋值 `view`、聚焦归档过滤框、`serialize(cx)`、`cx.notify()`。

`show_thread_list` 全文——[crates/sidebar/src/sidebar.rs:7602-7609](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L7602-L7609)：注意 `_subscriptions.clear()` 是第二行——先注销再聚焦，旧归档实体从此不再有事件通道。

订阅槽位字段——[crates/sidebar/src/sidebar.rs:780-782](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L780-L782)：`_subscriptions: Vec<gpui::Subscription>`。全 crate 对它只有两处写：`show_archive` 里 `push`（L7595）、`show_thread_list` 里 `clear`（L7604）——这个字段就是「当前归档视图的事件通道」的化身。

底栏入口——[crates/sidebar/src/sidebar.rs:7343-7372](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L7343-L7372)：`render_sidebar_bottom_bar` 用 `is_archive = matches!(self.view, SidebarView::Archive(..))` 驱动时钟按钮的 `toggle_state` 与两态 tooltip（"Show Thread History" / "Hide Thread History"），点击转发 `toggle_archive`。

ACP 横幅入口——[crates/sidebar/src/sidebar.rs:7448-7451](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L7448-L7451)：导入横幅的 `on_import` 先 `show_archive` 再弹导入模态——历史视图本身带 Import 工具栏，两处入口汇合。

子实体的构造——[crates/agent_ui/src/threads_archive_view.rs:162-238](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/threads_archive_view.rs#L162-L238)：`ThreadsArchiveView::new` 创建自己的单行过滤框（占位符 "Search all threads…"，L171-L175）、订阅其 `BufferEdited` 触发 `update_items`、`observe` 全局 `ThreadMetadataStore` 同步列表（L197-L203）、订阅焦点事件清空选中（L185-L209）。对照 [threads_archive_view.rs:140-159](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/threads_archive_view.rs#L140-L159) 的字段表：`items`（`ArchiveListItem` = 时间分桶头或线程条目）、`restoring`（恢复中线程集合）、`thread_filter`（All / ArchivedOnly，定义在 [threads_archive_view.rs:49-54](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/threads_archive_view.rs#L49-L54)）——这是一套与主列表平行的迷你状态机，时间分桶 `TimeBucket`（Today/Yesterday/ThisWeek/PastWeek/Older）见 [threads_archive_view.rs:65-72](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/threads_archive_view.rs#L65-L72)。

事件枚举——[crates/agent_ui/src/threads_archive_view.rs:130-138](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/threads_archive_view.rs#L130-L138)：`ThreadsArchiveViewEvent` 五变体及 `EventEmitter` 实现；`Activate` 只携带 `ThreadMetadata` 这一份最小载荷——激活的重活在侧边栏侧（4.3）。

`serialize` 与宿主消费——[crates/sidebar/src/sidebar.rs:926-928](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L926-L928)：`serialize()` 只是 `cx.emit(workspace::SidebarEvent::SerializeNeeded)`；宿主在 [crates/workspace/src/multi_workspace.rs:393-405](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/workspace/src/multi_workspace.rs#L393-L405) 的 `register_sidebar` 里订阅该事件并调 `this.serialize(cx)` 落盘（事件枚举见 [multi_workspace.rs:118-120](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/workspace/src/multi_workspace.rs#L118-L120)）——这就是「切换视图会被记住」的完整回路。

#### 4.2.4 代码实践

**实践目标**：证明视图切换路径真的被走到（本讲总实践的第三步）。

1. 在本地工作副本中打开 `crates/sidebar/src/sidebar.rs`，找到 `show_archive`（L7533），在函数体第一行加一条日志（**示例代码**，实验后还原，不要提交）：

   ```rust
   fn show_archive(&mut self, window: &mut Window, cx: &mut Context<Self>) {
       log::info!("sidebar: opening thread history view");
       // ...原有代码...
   ```

2. 运行 `test_restore_serialized_archive_view_does_not_panic`（它会经延迟恢复路径调用 `show_archive`）：

   ```bash
   cargo test -p sidebar --lib test_restore_serialized_archive_view_does_not_panic
   ```

3. 观察测试输出中的日志行。若 `log::info!` 不可见（测试环境未必初始化 logger），改用 `eprintln!("sidebar: opening thread history view")` 重试——它会直接写到 stderr。

**需要观察的现象**：日志在 `cx.run_until_parked()` 期间打印一次（延迟回调执行时），随后测试断言 `view` 为 `Archive` 通过。

**预期结果**：测试通过 + 日志出现，证明「restore → defer → show_archive」链路完整。若想覆盖另一个入口，可在 `toggle_archive` 处再加一条日志并运行任何触发 `ToggleThreadHistory` 的测试。日志捕获方式与测试输出「待本地验证」（取决于环境的 logger 初始化）。

#### 4.2.5 小练习与答案

**练习 1**：`show_archive` 在哪两种情况下会「什么都不做」直接返回？此时 `view` 是什么？

**答案**：宿主 `MultiWorkspace` 已释放（`upgrade()` 失败，拿不到活跃工作区），或活跃工作区上还没有 `AgentPanel`。两种情况都提前 `return`，`view` 保持 `ThreadList` 不变——这是「归档视图必须依附于一个带面板的工作区」这一前置条件的防御式表达。

**练习 2**：为什么归档视图的订阅存 `_subscriptions` 字段，而主列表对 `MultiWorkspace` 的订阅却 `detach`？

**答案**：宿主订阅与 `Sidebar` 同生共死（u1-l3），detach 无害；归档实体则会被反复创建与替换，旧实体的订阅若不显式注销，事件会继续打进 `Sidebar` 形成幽灵回调。字段持有让生命周期受控：`show_thread_list` 的 `clear()` 一次性切断通道，旧实体随后可被回收。对照：thread switcher 的订阅也存字段 `_thread_switcher_subscriptions`（u7-l2），同一模式。

**练习 3**：连续两次点底栏时钟按钮（进入→退出历史视图），第二次进入时的 `ThreadsArchiveView` 实体和第一次是同一个吗？

**答案**：不是。每次 `show_archive` 都 `cx.new` 一个全新实体；退回列表时订阅被注销、`view` 被替换，旧实体失去所有强引用后由 gpui 回收。历史视图不保留「上次浏览位置」这类跨会话状态（过滤词、选中行都是新的）。

### 4.3 open_thread_from_archive：把历史线程拉回活跃

#### 4.3.1 概念说明

历史视图里选中一条线程按回车（或点击），归档视图会 `emit(ThreadsArchiveViewEvent::Activate { thread })`；把这条线程「拉回活跃」的全部编排落在侧边栏的 `open_thread_from_archive`。它要回答的核心问题是：**这条线程关联的工作区/工作树还在吗？**

- **快路径**：线程没有任何 `folder_paths`（比如从未绑定目录的对话）——直接在元数据存储上 `unarchive`，然后走 u6-l1 的三岔激活决策，最后 `show_thread_list` 切回列表。
- **慢路径**：线程有关联目录，且元数据显示它曾被归档——归档时它的 worktree 可能被一并归档掉了（u8-l2 的主题）。此时必须先查 `get_archived_worktrees_for_thread`，若有，逐个用 git 把工作树恢复回磁盘、清理归档记录、把元数据里的旧路径改写为新路径，**然后**才能激活。

整个过程是异步的（git 恢复要 await），所以任务句柄存进 `restoring_tasks: HashMap<ThreadId, Task<()>>`——这正是 u6-l1 见过的模式：以 ThreadId 为键，drop 即取消。归档视图侧配合维护 `restoring` 集合，把该行渲染成 Running 状态并挂出 Cancel Restore 按钮；用户取消时发 `CancelRestore` 事件，侧边栏 `remove` 掉任务即中止。

#### 4.3.2 核心流程

```text
open_thread_from_archive(metadata):
    weak_archive_view = 从 self.view 里取归档实体弱引用（供异步错误路径回写状态）

    若 metadata.folder_paths 为空（快路径）:
        store.unarchive(thread_id)
        三岔激活: 本窗口? 其他窗口? 先开工作区?
        show_thread_list
        return

    task = metadata.archived ? get_archived_worktrees_for_thread : 空列表
    spawn 异步恢复任务:
        archived_worktrees = task.await
        若为空:
            unarchive → 三岔激活 → show_thread_list
        否则对每个归档 worktree:
            restore_worktree_via_git(row)          ← git 恢复
            cleanup_archived_worktree_record(row)  ← 清归档记录
            收集 (旧路径, 新路径) 替换对
            失败 → toast 报错 + 清恢复状态 + return
        若有替换对:
            store.update_restored_worktree_paths(替换对)
            store.unarchive
            open_workspace_and_activate_thread + show_thread_list
    restoring_tasks.insert(thread_id, 任务)
```

状态机视角（`restoring` / `restoring_tasks` 的联合迁移）：

\[ \text{Idle} \xrightarrow{\text{Activate}} \text{Restoring} \xrightarrow{\text{成功}} \text{Activated + 切回列表} \quad;\quad \text{Restoring} \xrightarrow{\text{CancelRestore / git 失败}} \text{Idle} \]

#### 4.3.3 源码精读

弱引用预捕获——[crates/sidebar/src/sidebar.rs:4054-4064](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L4054-L4064)：函数开头从 `&self.view` 匹配出 `Some(view.downgrade())`，因为后面的异步闭包不能再借用 `self`，而错误路径需要更新归档视图（`clear_restoring`）。

快路径——[crates/sidebar/src/sidebar.rs:4066-4094](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L4066-L4094）：无目录时先 `store.unarchive`，再按「活跃工作区 → 其他窗口 → 打开工作区再激活」的顺序三岔分流（与 u6-l1 `activate_thread` 同构），收尾 `show_thread_list`。

慢路径的查询——[crates/sidebar/src/sidebar.rs:4096-4103](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L4096-L4103)：只有 `metadata.archived` 为真才查归档工作树，否则 `Task::ready(Ok(Vec::new()))`——未归档线程走同一条代码路径但零查询。

无归档工作树的分支——[crates/sidebar/src/sidebar.rs:4110-4149](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L4110-L4149)：`this.update_in` 里先 `restoring_tasks.remove`，再 `unarchive`，再走同样的三岔激活与 `show_thread_list`。

git 恢复与错误处理——[crates/sidebar/src/sidebar.rs:4151-4200](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L4151-L4200)：逐行调 `thread_worktree_archive::restore_worktree_via_git`，成功则清理记录并收集 `(旧路径, 新路径)`；失败则 `log::error`、清 `restoring_tasks`、经 `weak_archive_view` 调 `clear_restoring`，并在活跃工作区弹 `Toast`（"Failed to restore worktree: …"）——注意这里把 UI 反馈放在恢复方（侧边栏）而不是子实体，因为 toast 挂在 workspace 上。

路径改写与最终激活——[crates/sidebar/src/sidebar.rs:4203-4247](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L4203-L4247)：`update_restored_worktree_paths` 把元数据里的旧路径迁移到恢复后的新路径，重新读取元数据、`unarchive`，最后 `open_workspace_and_activate_thread` + `show_thread_list`；L4247 `restoring_tasks.insert(thread_id, restore_task)` 把任务存档——`restoring_tasks` 字段声明在 [crates/sidebar/src/sidebar.rs:774](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L774)。

取消通道——[crates/sidebar/src/sidebar.rs:7580-7582](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L7580-L7582)：`CancelRestore` 事件只做 `restoring_tasks.remove(thread_id)`；归档视图侧行内按钮在 [threads_archive_view.rs:690-707](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/threads_archive_view.rs#L690-L707)——恢复中的行显示 Running 状态 + Cancel 按钮，点击先 `clear_restoring` 再 emit `CancelRestore` 并 `stop_propagation`。

触发侧——[threads_archive_view.rs:443-462](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/threads_archive_view.rs#L443-L462)：`unarchive_thread` 是归档视图的出口：已在恢复中则忽略；`folder_paths` 为空则弹项目选择器（让用户给线程指定目录）；否则 `mark_restoring` + 清选中 + 清过滤词 + `emit(Activate)`。键盘 `Confirm` 在 [threads_archive_view.rs:585-592](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/threads_archive_view.rs#L585-L592) 转发到这里。

#### 4.3.4 代码实践

**实践目标**：把 `restoring_tasks` 的生命周期画成可核对的状态图（源码阅读型实践）。

1. 在 `sidebar.rs` 中搜索 `restoring_tasks`，你会找到全部写点：
   - `insert`：`open_thread_from_archive` 末尾（L4247）。
   - `remove`：三处——无归档工作树分支（L4112）、git 失败分支（L4172）、成功激活分支（L4227），外加 `CancelRestore` 事件（L7581）。
2. 为每个写点标注触发条件（成功 / 失败 / 用户取消），画出 `Idle ⇄ Restoring` 的迁移图。
3. 回答：为什么 `CancelRestore` 只需 `remove` 就能中止 git 恢复？

**预期结果**：一张四条出边（成功×2、失败×1、取消×1）一条入边的状态图；`remove` 之所以足够，是因为 gpui 的 `Task` 被 drop 即取消（u6-l1 的 `restoring_tasks` 防重入语义），而触发方归档视图已在 emit 前自行 `clear_restoring`，行的 UI 状态同步恢复。git 恢复的中断粒度「待本地验证」（取决于 `restore_worktree_via_git` 内部取消点的分布）。

#### 4.3.5 小练习与答案

**练习 1**：快路径与慢路径的本质区别是什么？

**答案**：快路径（`folder_paths` 为空）没有磁盘上的牵挂，`unarchive` 只是元数据标志位翻转，随后立即激活；慢路径可能关联着归档时一并归档的 worktree，必须先查 `get_archived_worktrees_for_thread`，必要时 git 恢复工作树、清理记录、改写元数据路径，才能激活。区别在于「线程的身份材料是否完整可用」。

**练习 2**：为什么 `weak_archive_view` 要在函数最开头捕获？

**答案**：恢复任务是 `cx.spawn_in` 出去的异步闭包，只持有 `this`（`WeakEntity<Sidebar>`）与局部变量，不能再借用 `self.view`；而 git 失败分支需要更新归档视图（`clear_restoring` 清除行的恢复态）。同步阶段先把弱引用抓成局部值，是 gpui 异步代码的惯用法（也符合 CLAUDE.md 里「用变量遮蔽收窄克隆生命周期」的风格）。

**练习 3**：恢复成功后为什么调用的是 `open_workspace_and_activate_thread` 而不是另外两条激活路径？

**答案**：走到慢路径成功分支意味着线程目录刚从归档恢复——通常没有已打开的工作区与之对应（有就不会走到归档恢复），所以固定走「先打开工作区再激活」的第三条路径；而快路径与无归档工作树分支保留完整三岔，因为那里无法预知工作区状态。

### 4.4 replace_archived_panel_thread：工作区切换后的一致性护栏

#### 4.4.1 概念说明

这是一个容易被忽略的小函数，但它守护的是一条真实的不变量。场景：用户在多项目工作区里，把 A 项目里的线程归档了，然后切换到 B 项目——此时 `AgentPanel` 上**仍然显示着那条已归档的线程**（归档只动元数据，不立即换面板内容）。于是：

- 面板的活跃线程已归档 → 它不会再出现在主列表里；
- 侧边栏的 `active_entry` 若还指向它，高亮就成了「指向幽灵」。

`replace_archived_panel_thread` 在每次活跃工作区变化时检查这一情况：若面板当前线程在元数据存储里 `archived == true`，就调用 `create_new_thread`（u6-l3 的新建草稿链路）给面板一个干净的默认态，`active_entry` 随之有有效指向。

#### 4.4.2 核心流程

```text
MultiWorkspaceEvent::ActiveWorkspaceChanged:
    sync_active_entry_from_active_workspace()   ← 先按新面板对账 active_entry（u2-l3）
    replace_archived_panel_thread()            ← 再检查「面板线程是否已被归档」
        panel.active_thread_id → ThreadMetadataStore.entry(id).archived?
            是 → create_new_thread(workspace)   ← 新草稿接管面板
    schedule_update_entries(false)             ← 最后常规刷新列表
```

三道防线按顺序执行：对账 → 护栏 → 重建。顺序很重要——若护栏先跑，对账会把 `active_entry` 又设回归档线程。

#### 4.4.3 源码精读

调用点——[crates/sidebar/src/sidebar.rs:813-832](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L813-L832)：构造期对宿主的订阅里，`ActiveWorkspaceChanged` 分支（L817-L821）依次调用对账、护栏、调度刷新。

函数本体——[crates/sidebar/src/sidebar.rs:1132-1153](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L1132-L1153)：doc 注释（L1132-L1135）直接写明动机——「切换工作区时，面板可能仍显示从别的工作区归档来的线程；此时创建新草稿让面板有有效内容、`active_entry` 有可指向的对象」。实现是四连守卫（活跃工作区、面板、活跃线程 id）加一次 `ThreadMetadataStore` 查询（L1146-L1149 的 `entry(thread_id).is_some_and(|m| m.archived)`），命中才 `create_new_thread`。

#### 4.4.4 代码实践

**实践目标**：通过场景推演检验对护栏触发条件的理解（源码阅读型实践）。

判断以下三种场景中 `create_new_thread` 是否会被调用，然后对照源码验证：

1. 切换工作区后，面板显示的是一条**活跃**（未归档）线程。
2. 切换工作区后，面板显示的是一条刚被用户归档的线程。
3. 切换工作区后，面板当前没有活跃线程（如刚打开的空面板）。

**操作步骤**：先写下你的答案，再逐条对照 [sidebar.rs:1136-1153](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L1136-L1153) 的守卫链。

**预期结果**：场景 1 不调用（`archived == false`）；场景 2 调用（正是设计场景）；场景 3 不调用（`active_thread_id` 返回 `None`，第三个守卫挡下）。若想在测试里复现场景 2，可参考 `sidebar_tests.rs` 中调用 `sidebar.update_in` 后断言面板状态的既有测试写法（具体测试「待确认」，可用 Grep 搜 `archived` 在测试文件中的用法）。

#### 4.4.5 小练习与答案

**练习 1**：这个护栏为什么挂在 `ActiveWorkspaceChanged` 而不是归档动作本身？

**答案**：归档发生时面板显示归档线程是**有意保留**的——用户可能正看着它收尾；问题只在「切到别的工作区后仍停留在归档线程」时才出现（新工作区的面板不该继承旧工作区已归档的上下文）。挂在工作区切换事件上，恰好只在出问题的边界上修整。

**练习 2**：它为什么选择新建草稿而不是激活列表里的相邻线程？

**答案**：新建草稿是无害默认态，不需要猜测用户意图；挑相邻线程则要重复 `close_terminal` 系列里「邻居选择」的复杂逻辑（u6-l2 的 `neighboring_activatable_entry`），且可能把用户没点的线程变成活跃。这与 u6-l3「空项目守卫后创建草稿」的默认策略一致。

### 4.5 SerializedSidebarView：持久化与 serde alias 兼容

#### 4.5.1 概念说明

侧边栏的持久化状态只有两个字段：宽度与当前视图。运行时枚举 `SidebarView` 持有实体句柄、不可序列化，所以另设一个纯数据枚举 `SerializedSidebarView`：

```rust
#[derive(Default, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
enum SerializedSidebarView {
    #[default]
    ThreadList,
    #[serde(alias = "Archive")]
    History,
}
```

`#[serde(alias = "Archive")]` 是本讲的点睛之笔。2026-04 的提交 `3dfbfc8fff`（"Rename Archive view to Thread History (#54075)"）把变体从 `Archive` 改名为 `History`——但用户的数据库里已经存着 `"active_view":"Archive"` 的旧 JSON。serde 对改名的单元变体默认**只认新名字**，旧 blob 会反序列化失败。加 alias 后，反序列化接受的名字集合变为 \( \{"History", "Archive"\} \)，而序列化**永远只输出** `"History"`——等价于一次懒加载的单向迁移：旧状态读取即升级，落盘一次后世上再无 `"Archive"`。

配套的容错在 `restore_serialized_state`：`serde_json::from_str::<SerializedSidebar>(state).log_err()`——解析失败只记日志不 panic，宽度与视图双双回退默认值（300px、ThreadList）。持久化格式演进的第一原则：**坏状态不应该带走窗口**。

#### 4.5.2 核心流程

完整的持久化回路（结合 u1-l1 的装配链）：

```text
保存: show_archive / show_thread_list / set_width
      → serialize() → emit SidebarEvent::SerializeNeeded
      → MultiWorkspace::serialize → SerializedMultiWorkspace { sidebar_state: Some(json) }
      → 工作区数据库

恢复: 窗口重建 (workspace.rs)
      → restore_open_sidebar()                  ← 先把侧边栏本体挂出来
      → sidebar.restore_serialized_state(json)  ← 再灌状态
          ├─ width 钳制到 [MIN_WIDTH, MAX_WIDTH]
          └─ 若 active_view == History:
                 cx.defer_in → show_archive      ← 延迟到当前更新循环末尾
      → multi_workspace.serialize(cx)           ← 立即回写一次
```

序列化映射是单向压扁：`SidebarView::Archive(Entity<…>)` → `SerializedSidebarView::History`——实体身份不持久化，恢复时**重建**一个全新的归档视图实体（这解释了 4.2 练习 3 的「不保留浏览位置」）。

`defer_in` 的用意：恢复调用发生在窗口重建的中途（`restore_open_sidebar` 刚把侧边栏挂出来，面板等设施未必完全就绪），而 `show_archive` 有「活跃工作区 + AgentPanel」两个前置守卫；推迟到当前更新循环结束后执行，让周边设施先落位。测试注释也印证了这一契约——断言放在 `run_until_parked()` 之后。

#### 4.5.3 源码精读

序列化类型——[crates/sidebar/src/sidebar.rs:108-114](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L108-L114) 是 `SerializedSidebarView`（注意 L112 的 alias）；[crates/sidebar/src/sidebar.rs:122-128](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L122-L128) 是外层 `SerializedSidebar`（两个字段都 `#[serde(default)]`，旧版本缺字段的 blob 也能读）。

导出——[crates/sidebar/src/sidebar.rs:7721-7730](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L7721-L7730)：`serialized_state` 把 `view` 压扁成 `SerializedSidebarView`（`Archive(_)` → `History`）再 `serde_json::to_string`。

导入——[crates/sidebar/src/sidebar.rs:7732-7749](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L7732-L7749)：`restore_serialized_state` 解析失败走 `.log_err()` 静默回退；宽度钳制在 L7740；`History` 分支用 `cx.defer_in` 包住 `show_archive`（L7742-L7746）。

契约与默认实现——[crates/workspace/src/multi_workspace.rs:148-160](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/workspace/src/multi_workspace.rs#L148-L160)：trait `Sidebar` 给 `serialized_state` / `restore_serialized_state` 提供了空默认值——其他侧边栏实现（如项目面板）不持久化也无需写这两个方法。

落盘字段——[crates/workspace/src/multi_workspace.rs:1466](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/workspace/src/multi_workspace.rs#L1466)：`MultiWorkspace` 序列化时把 `sidebar.serialized_state(cx)` 存进 `sidebar_state`。

恢复时序——[crates/workspace/src/workspace.rs:9866-9883](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/workspace/src/workspace.rs#L9866-L9883)：先 `restore_open_sidebar`（侧边栏须处于打开状态才恢复），再 `restore_serialized_state`，随即 `multi_workspace.serialize(cx)` 回写——迁移后的新格式立刻落盘。

防回归测试一（往返）——[crates/sidebar/src/sidebar_tests.rs:753-791](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L753-L791)：`test_serialization_round_trip` 设置 420px 宽度并折叠分组，取 `serialized_state`，灌进一个**全新** `Sidebar`，断言两侧宽度一致（且恰为 420）。

防回归测试二（归档恢复）——[crates/sidebar/src/sidebar_tests.rs:793-824](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L793-L824)：`test_restore_serialized_archive_view_does_not_panic` 手工构造 `SerializedSidebar { width: 400, active_view: History }` 的 JSON，经宿主句柄恢复，`run_until_parked` 后断言 `view` 为 `Archive(_)`——L817 的注释明确点出「延迟的 `show_archive` 跑完之后」这一时序契约。

改名证据——提交 `3dfbfc8fff`（可用 `git show 3dfbfc8fff` 复核）把 `ViewAllThreads` 动作改名 `ToggleThreadHistory`、`SerializedSidebarView::Archive` 改名 `History` 并加 alias，提交说明写道：视图同时列出活跃与已归档线程，故更名；「归档线程」这一动词/状态不变。

#### 4.5.4 代码实践

**实践目标**：亲手跑通两个防回归测试，并用一段文字固化对 alias 的理解（本讲总实践的第一、二步）。

1. 在仓库根目录运行：

   ```bash
   cargo test -p sidebar --lib test_serialization_round_trip
   cargo test -p sidebar --lib test_restore_serialized_archive_view_does_not_panic
   ```

2. 阅读两个测试的断言，回答：
   - 往返测试为什么新建第二个 `Sidebar` 而不是在原实体上恢复？（排除「状态本来就在」的假阳性。）
   - 归档恢复测试为什么要 `AgentRegistryStore::init_test_global(cx, vec![])`？（`show_archive` 创建 `ThreadsArchiveView` 的路径会触碰全局 store；归档视图渲染行时查 agent 图标。）
3. 写一段 3~5 句的文字解释：若删除 `#[serde(alias = "Archive")]`，从旧版本升级过来的用户重开窗口会发生什么？

**预期结果**：两个测试全绿。解释要点：旧 blob 里的 `"active_view":"Archive"` 反序列化失败 → `.log_err()` 记一条错误 → 整个 `if let Some(serialized)` 跳过 → 宽度与视图都不恢复，回退默认（300px、ThreadList）→ 用户「停在历史视图」的偏好静默丢失；且不会崩溃。首次运行输出「待本地验证」（取决于编译环境）。

#### 4.5.5 小练习与答案

**练习 1**：alias 影响序列化输出吗？给出迁移的方向性。

**答案**：不影响。序列化永远输出 `"History"`；alias 只扩大反序列化的接受集合 \( \{"History", "Archive"\} \)。方向是单向的：旧格式读入即升级，恢复后宿主立即回写（workspace.rs L9880 的 `serialize`），旧名字在一个写周期后即消失。

**练习 2**：`SerializedSidebar` 的两个字段为什么都标 `#[serde(default)]`？

**答案**：向前兼容的另一端：未来（或过去）版本的 blob 可能缺字段——`#[serde(default)]` 让缺失字段取默认值（`None` / `ThreadList`）而不是解析失败。与 alias 一起构成「旧读新、新读旧」的双向宽容。

**练习 3**：`restore_serialized_state` 里的 `show_archive` 为什么必须 `defer_in`，而 `set_width` 不用？

**答案**：`set_width` 是纯字段赋值 + 钳制，无前置条件；`show_archive` 需要「活跃工作区存在且有 `AgentPanel`」，而恢复发生在窗口重建中途，这些设施可能尚未就绪（恢复顺序：`restore_open_sidebar` 在前）。`defer_in` 把执行推到当前更新循环末尾，让设施先落位——`test_restore_serialized_archive_view_does_not_panic` 正是靠 `run_until_parked` 等这个延迟回调跑完才断言。

## 5. 综合实践

**任务：写一个双别名恢复测试，亲手验证迁移语义。**

本任务贯穿本讲全部模块：序列化格式（4.5）、恢复时序（4.5）、切换副作用（4.2）、渲染分派（4.1）。

1. **跑基线**：先运行两个既有测试确认环境正常（见 4.5.4 的命令）。

2. **写新测试**：在 `sidebar_tests.rs` 中模仿 `test_restore_serialized_archive_view_does_not_panic`（L793-L824）新增一个测试（**示例代码**，本地实验用）：

   ```rust
   #[gpui::test]
   async fn test_restore_accepts_legacy_archive_alias(cx: &mut TestAppContext) {
       let project = init_test_project_with_agent_panel("/my-project", cx).await;
       let (multi_workspace, cx) =
           cx.add_window_view(|window, cx| MultiWorkspace::test_new(project.clone(), window, cx));
       let (sidebar, _panel) = setup_sidebar_with_agent_panel(&multi_workspace, cx);
       cx.update(|_window, cx| {
           AgentRegistryStore::init_test_global(cx, vec![]);
       });

       for legacy_name in ["Archive", "History"] {
           let serialized = format!("{{\"width\":400.0,\"active_view\":\"{legacy_name}\"}}");
           multi_workspace.update_in(cx, |multi_workspace, window, cx| {
               if let Some(sidebar) = multi_workspace.sidebar() {
                   sidebar.restore_serialized_state(&serialized, window, cx);
               }
           });
           cx.run_until_parked();

           sidebar.read_with(cx, |sidebar, _cx| {
               assert!(
                   matches!(sidebar.view, SidebarView::Archive(_)),
                   "legacy alias \"{legacy_name}\" should restore the archive view"
               );
           });

           // 切回列表，为下一轮迭代复位
           sidebar.update_in(cx, |sidebar, window, cx| sidebar.show_thread_list(window, cx));
           cx.run_until_parked();
       }
   }
   ```

3. **核对预期**：两个名字都应恢复出 `Archive` 视图。若把 `"Archive"` 那轮单独删掉 alias（本地临时删 `#[serde(alias = "Archive")]`）再跑，该轮应失败在断言上（`view` 停在 `ThreadList`）而**不是** panic——验证 4.5 的容错论述。

4. **加观测**：结合 4.2.4 的日志实践，在 `show_archive` 加一条 `eprintln!`，确认两轮迭代各触发一次切换路径。

5. **收尾**：还原所有本地修改（日志、alias 删除），重跑两个既有测试确认全绿。

**预期结果**：新测试通过；你得到一份可复述的证据链——`"Archive"`（旧世界的名字）与 `"History"`（新世界的名字）经由同一个 alias 集合汇入同一个恢复路径，最终都走到 `defer_in → show_archive → ThreadsArchiveView 创建 → 订阅 → 焦点 → 持久化` 这条完整副作用链。编译与运行结果「待本地验证」。

## 6. 本讲小结

- `SidebarView` 是侧边栏主体的互斥二形态：`ThreadList`（默认）与 `Archive(Entity<ThreadsArchiveView>)`；归档视图是**自治子实体**，`render()` 在 `match` 点把「头部+列表」整体替换为 `child(entity)`，根容器、导入横幅与底栏两形态共享。
- 切换是一组有序副作用：`show_archive` = 遥测 → 双守卫（活跃工作区 + AgentPanel）→ 新建子实体 → 订阅五类事件（`Close`/`Activate`/`CancelRestore`/`Import`/`NewThread`）→ 存 `_subscriptions` → 换 `view` → 聚焦归档过滤框 → `serialize()`；`show_thread_list` = 换回 `ThreadList` + `clear()` 订阅（切断幽灵回调）+ 焦点回主过滤框 + `serialize()`。
- `open_thread_from_archive` 把历史线程拉回活跃：无目录走快路径（`unarchive` + u6-l1 三岔激活）；有关联目录先查归档工作树，必要时 git 恢复、清理记录、改写元数据路径再激活；异步任务存 `restoring_tasks`（ThreadId 为键，drop 即取消），行内 Cancel 按钮经 `CancelRestore` 事件中止。
- `replace_archived_panel_thread` 是工作区切换时的一致性护栏：面板若仍停在已归档线程上，就新建草稿接管，保证 `active_entry` 永不指向幽灵。
- 持久化经 `SerializedSidebarView`：`#[serde(alias = "Archive")]` 让改名（`3dfbfc8fff`，Archive → History）后旧 blob 仍可读，序列化只出 `"History"`，配合 `log_err()` 容错与 `defer_in` 延迟恢复，构成「坏状态不带走窗口、旧状态读入即升级」的兼容策略。

## 7. 下一步学习建议

- **下一讲（u8-l2）工作树归档流水线**：本讲 4.3 只消费了 `get_archived_worktrees_for_thread` / `restore_worktree_via_git` 的结果；下一讲逆向追「归档是怎么发生的」——`roots_to_archive_for_paths`、`thread_blocks_worktree_archive`、`delete_empty_drafts_for_archive_*` 与级联关闭，把本讲的恢复路径与归档路径拼成完整生命周期。
- **u8-l3 序列化与恢复**：本讲只碰了 `Sidebar` 一侧的两个方法；下一讲系统走读 `WorkspaceSidebar` 契约、`SerializedSidebar` 的宽度钳制与 `SerializeNeeded` 的产生/消费全景。
- **源码延伸阅读**：`crates/agent_ui/src/threads_archive_view.rs` 的 `update_items`（L270 起）与 `render_header`/`render_toolbar`——看时间分桶、`ThreadFilter` 与 `fuzzy_match_positions`（与主列表同名函数的孪生实现）如何组装出历史列表；`crates/workspace/src/multi_workspace.rs` 的 `serialize`/`restore` 全文——理解 `sidebar_state` 在窗口持久化中的位置。

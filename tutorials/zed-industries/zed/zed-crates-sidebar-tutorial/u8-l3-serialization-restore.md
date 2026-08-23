# 序列化与恢复：WorkspaceSidebar 契约

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `workspace` crate 与 `sidebar` crate 之间的持久化契约：`Sidebar` trait（在 sidebar.rs 中以 `WorkspaceSidebar` 别名导入）、对象安全的 `SidebarHandle` 包装，以及为什么需要这样一层抽象。
2. 说出 `SerializedSidebar` 到底持久化了哪两个字段、以什么 JSON 形态落盘、`#[serde(alias = "Archive")]` 解决了什么兼容问题。
3. 解释宽度恢复时的钳制逻辑，以及为什么归档视图的恢复要用 `cx.defer_in` 延迟一拍。
4. 跟踪 `SidebarEvent::SerializeNeeded` 的完整生命周期：谁发出它、谁消费它、最终写到磁盘的哪个位置、启动时又从哪里读回来。

## 2. 前置知识

本讲会用到以下概念，若不熟悉请先补充：

- **trait 与对象安全**：Rust 中 `dyn Trait`（动态分发）要求 trait 是对象安全的——不能有返回 `Self` 的方法、不能带泛型方法。带泛型的方法只能走 `impl Trait for T` 的静态分发路径。本讲的 `Sidebar` trait 因为声明了 `Sized` 相关约束，需要再配一个对象安全的 `SidebarHandle` 才能装进 `Box<dyn ...>`。
- **serde 基础**：`#[derive(Serialize, Deserialize)]` 会为结构体生成 JSON 转换代码；`#[serde(default)]` 让反序列化时缺失的字段取默认值（容忍旧版本数据）；`#[serde(alias = "...")]` 让反序列化额外接受一个旧名字，但序列化永远只输出新名字。
- **gpui 实体事件**：`cx.emit(event)` 发出事件、`cx.subscribe` 订阅事件（u1-l3 已详细讲过）。本讲的 `SerializeNeeded` 就是一个只有单变体的实体事件。
- **任务替换即去抖**：`MultiWorkspace` 把序列化任务存进 `_serialize_task` 字段，新任务写入时旧任务被 drop、即被取消——这与 u3-l2 讲过的 `update_task` 合并是同一套机制。
- **全量重推导教义**（u1-l1 建立）：凡是可以从当前世界状态算出来的东西都不该持久化。本讲是这条教义的最佳注脚——32 个字段只存 2 个。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/sidebar/src/sidebar.rs` | `SerializedSidebar`/`SerializedSidebarView` 定义、`serialize()`、`show_archive`/`show_thread_list`、`impl WorkspaceSidebar for Sidebar` |
| `crates/sidebar/src/sidebar_tests.rs` | `test_serialization_round_trip`、`test_restore_serialized_archive_view_does_not_panic` 两个关键测试，以及实践要用到的脚手架 |
| `crates/workspace/src/multi_workspace.rs` | 契约的定义方：`SidebarEvent`、`Sidebar` trait、`SidebarHandle`、`register_sidebar`、`MultiWorkspace::serialize` |
| `crates/workspace/src/persistence/model.rs` | `MultiWorkspaceState`——`sidebar_state` 字符串所在的外层信封 |
| `crates/workspace/src/persistence.rs` | `write_multi_workspace_state`——真正写 KV 存储 |
| `crates/workspace/src/workspace.rs` | 启动恢复链：`restore_multiworkspace` → `apply_restored_multiworkspace_state` |

依赖方向提醒：`sidebar` 依赖 `workspace`（Cargo.toml 中有 `workspace` 依赖），`workspace` **不**依赖 `sidebar`。持久化契约全部定义在 `workspace` 一侧，`sidebar` 只是实现方。

## 4. 核心概念与源码讲解

### 4.1 WorkspaceSidebar trait：跨 crate 的持久化契约

#### 4.1.1 概念说明

`MultiWorkspace` 需要在会话结束时保存侧边栏状态、在下次启动时恢复它。但 `workspace` crate 根本不知道 `sidebar` crate 的存在（依赖箭头是单向的），它不可能调用 `Sidebar::serialized_state` 这样的具体方法。

解法是经典的依赖倒置：**定义方（workspace）声明"我需要一个能持久化自己的东西"，实现方（sidebar）来满足这个声明**。这个声明就是 `Sidebar` trait。

在 sidebar.rs 中你会看到一个容易困惑的写法：

```rust
use workspace::{
    ..., Sidebar as WorkspaceSidebar, ...
};
```

[sidebar.rs:65-70](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L65-L70) 中把 workspace 的 `Sidebar` trait 导入并改名为 `WorkspaceSidebar`——因为 sidebar crate 自己的实体类型就叫 `Sidebar`，两个名字撞车了，必须给 trait 起个别名。**「WorkspaceSidebar 是 trait，Sidebar 是我们的实体」**，这是读本 crate 代码时的一个必备常识。

#### 4.1.2 核心流程

契约分成两层：

```
workspace crate                          sidebar crate
────────────────                         ────────────
trait Sidebar（泛型，静态分发）
  ↑ impl
  │                              impl WorkspaceSidebar for Sidebar
  │
Entity<T: Sidebar>
  ↓ 自动转发
trait SidebarHandle（对象安全）
  ↑
Box<dyn SidebarHandle>  ←──── MultiWorkspace.sidebar 字段持有
```

1. `MultiWorkspace::register_sidebar<T: Sidebar>(entity)` 接收一个具体类型的实体句柄；
2. `Entity<T>` 因为已有 `impl<T: Sidebar> SidebarHandle for Entity<T>` 的转发实现，可以直接装箱为 `Box<dyn SidebarHandle>` 存进字段；
3. 之后 `MultiWorkspace` 只通过 `dyn SidebarHandle` 动态分发调用，完全不知道背后的具体类型。

#### 4.1.3 源码精读

先看契约的三个定义，全部在 multi_workspace.rs：

```rust
pub enum SidebarEvent {
    SerializeNeeded,
}

pub trait Sidebar: Focusable + Render + EventEmitter<SidebarEvent> + Sized {
    fn width(&self, cx: &App) -> Pixels;
    fn set_width(&mut self, width: Option<Pixels>, cx: &mut Context<Self>);
    ...
    /// Return an opaque JSON blob of sidebar-specific state to persist.
    fn serialized_state(&self, _cx: &App) -> Option<String> {
        None
    }

    /// Restore sidebar state from a previously-serialized blob.
    fn restore_serialized_state(
        &mut self,
        _state: &str,
        _window: &mut Window,
        _cx: &mut Context<Self>,
    ) {
    }
}
```

[multi_workspace.rs:118-161](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/workspace/src/multi_workspace.rs#L118-L161) 定义了事件与 trait。注意三点：trait 要求实现者同时是 `Focusable + Render + EventEmitter<SidebarEvent>`（既是可渲染视图又能报告"我需要序列化"）；`serialized_state` 与 `restore_serialized_state` **都有默认实现**（返回 `None` / 什么都不做），所以不关心持久化的实现者可以完全无视这两个方法；doc comment 明确说返回值是"opaque JSON blob"——定义方不解释格式。

`SidebarHandle` 是对象安全的镜像 trait：

```rust
pub trait SidebarHandle: 'static + Send + Sync {
    ...
    fn serialized_state(&self, cx: &App) -> Option<String>;
    fn restore_serialized_state(&self, state: &str, window: &mut Window, cx: &mut App);
}
```

[multi_workspace.rs:163-181](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/workspace/src/multi_workspace.rs#L163-L181) 中所有方法都用 `&mut App` 而非泛型 `Context<T>`，这正是对象安全的代价。`Entity<T>` 上的转发实现把两个世界连起来：

```rust
    fn serialized_state(&self, cx: &App) -> Option<String> {
        self.read(cx).serialized_state(cx)
    }

    fn restore_serialized_state(&self, state: &str, window: &mut Window, cx: &mut App) {
        self.update(cx, |this, cx| {
            this.restore_serialized_state(state, window, cx)
        })
    }
```

[multi_workspace.rs:261-269](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/workspace/src/multi_workspace.rs#L261-L269) 就是简单的读/更新转发。`MultiWorkspace` 的字段 `sidebar: Option<Box<dyn SidebarHandle>>`（[multi_workspace.rs:317](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/workspace/src/multi_workspace.rs#L317)）持有的就是这个装箱句柄。

#### 4.1.4 代码实践

**实践目标**：验证"契约在 workspace、实现在 sidebar"这条依赖方向。

**操作步骤**：

1. 打开 `crates/sidebar/Cargo.toml`，确认依赖列表里有 `workspace`；
2. 打开 `crates/workspace/Cargo.toml`，在依赖列表里搜索 `sidebar`——确认搜不到；
3. 在编辑器里对 `impl WorkspaceSidebar for Sidebar` 中的 `serialized_state` 做"查找所有引用"（或全局搜索 `serialized_state`），观察调用方分布在哪两个 crate。

**需要观察的现象**：trait 定义与 `dyn SidebarHandle` 的调用点全在 `crates/workspace`；`impl` 块只在 `crates/sidebar/src/sidebar.rs` 出现一次。

**预期结果**：你会看到一条清晰的单向依赖边。这也是为什么 `serialized_state` 的返回值只能是 `Option<String>`——workspace 没法引用 sidebar 的任何具体类型，只能接受一个不透明字符串。

#### 4.1.5 小练习与答案

**练习 1**：为什么不直接让 `MultiWorkspace` 泛型化为 `MultiWorkspace<T: Sidebar>`，省掉 `SidebarHandle` 这一层？

**参考答案**：`MultiWorkspace` 自身是一个被到处存储的 gpui 实体（`Entity<MultiWorkspace>`），如果它带泛型参数，每个持有它的地方都要传染这个泛型参数，且无法把不同种侧边栏存进同一个字段。装箱为 `Box<dyn SidebarHandle>` 把泛型固定在注册那一刻，之后统一走动态分发。此外 trait 上的 `Sized` supertrait 与 `Context<Self>` 参数使它本身不适合直接作 trait 对象，所以才需要单独的对象安全镜像。

**练习 2**：`SidebarEvent` 为什么只有一个变体 `SerializeNeeded`，而没有 `WidthChanged` 之类的事件？

**参考答案**：因为宿主对侧边栏的绝大多数变化只需重渲染（这由 gpui 的 `cx.notify` + `cx.observe` 机制覆盖，见 register_sidebar 中的 observe），只有"需要落盘"这件事必须显式告诉宿主。事件是按宿主需要采取的**行动**设计的，不是按侧边栏内部状态变化设计的。

### 4.2 SerializedSidebar：真正落盘的两个字段

#### 4.2.1 概念说明

u1-l3 数过 `Sidebar` 结构体的 32 个字段——但真正被持久化的只有两个：宽度、当前视图。看定义：

```rust
#[derive(Default, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
enum SerializedSidebarView {
    #[default]
    ThreadList,
    #[serde(alias = "Archive")]
    History,
}

#[derive(Default, Serialize, Deserialize)]
struct SerializedSidebar {
    #[serde(default)]
    width: Option<f32>,
    #[serde(default)]
    active_view: SerializedSidebarView,
}
```

[sidebar.rs:108-128](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L108-L128) 是本讲的"落盘数据模型"，全部加起来不到 20 行。

为什么不存列表内容、选中项、搜索词？因为这些都能从世界状态全量重推导（u3-l4 的 `rebuild_contents`），存了反而会造成两份需要同步的真相。真正必须持久化的是**无法从世界推导的用户偏好**：你把侧边栏拖多宽、你最后停在哪个视图。

#### 4.2.2 核心流程

序列化后的 JSON 形态（ serde 对单元变体枚举输出字符串）：

```json
{"width":420.0,"active_view":"History"}
```

它不会独立落盘，而是作为一个**字符串字段**嵌进外层信封 `MultiWorkspaceState`：

```rust
pub struct MultiWorkspaceState {
    pub active_workspace_id: Option<WorkspaceId>,
    pub sidebar_open: bool,
    #[serde(alias = "project_group_keys")]
    pub project_groups: Vec<SerializedProjectGroup>,
    #[serde(default)]
    pub sidebar_state: Option<String>,
}
```

[persistence/model.rs:108-117](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/workspace/src/persistence/model.rs#L108-L117) 定义了这个"每个窗口一份"的信封。也就是说磁盘上是 **JSON 里套 JSON 字符串** 的双层结构：外层由 workspace crate 定义并解析，内层 `sidebar_state` 对 workspace 完全不透明。

两个 serde 属性各有使命：

- `#[serde(default)]`（两个字段都有）：老版本 Zed 写出的数据可能没有这些字段，缺失时取默认值（`None` / `ThreadList`）而不是反序列化失败——**向前兼容旧数据**。
- `#[serde(alias = "Archive")]`（在 `History` 变体上）：枚举变体曾经叫 `Archive`（与运行时枚举 `SidebarView::Archive` 对齐），后来序列化名改成了 `History`。alias 让反序列化**同时接受** `"Archive"` 与 `"History"`，而序列化**永远输出** `"History"`——旧会话数据能读，新数据统一用新名。这是一次单向改名迁移的最小成本方案。

#### 4.2.3 源码精读

对照运行时视图枚举，看序列化视图如何"瘦身"：

```rust
#[derive(Debug, Default)]
enum SidebarView {
    #[default]
    ThreadList,
    Archive(Entity<ThreadsArchiveView>),
}
```

[sidebar.rs:130-135](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L130-L135) 中运行时形态 `Archive(Entity<ThreadsArchiveView>)` 持有子实体句柄——实体句柄当然是进程内存态，无法序列化。所以序列化侧只需要一个"你当时在归档视图"的布尔级事实，`SerializedSidebarView::History` 这个单元变体就够了。恢复时再现场新建一个 `ThreadsArchiveView` 实体（u8-l1 讲过它的构造需要活跃工作区、连接存储等多个依赖）。

宽度方面，`width: Option<f32>` 用 `f32` 而非 `Pixels`：`Pixels` 是 gpui 的 UI 类型（newtype 包装 f32），让它直接参与 serde 会引入不必要的耦合，`f32::from(self.width)` 与 `px(width)` 一对朴素转换即可。

#### 4.2.4 代码实践

**实践目标**：亲手看清序列化产物的 JSON 形态与 alias 的双向行为。

**操作步骤**：

1. 阅读测试 `test_restore_serialized_archive_view_does_not_panic` 开头的手工构造：

```rust
    let serialized = serde_json::to_string(&SerializedSidebar {
        width: Some(400.0),
        active_view: SerializedSidebarView::History,
    })
    .expect("serialization should succeed");
```

   [sidebar_tests.rs:804-808](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L804-L808) 绕过 `serialized_state()` 直接构造结构体再 `serde_json::to_string`——因为测试模块是 `#[cfg(test)] mod sidebar_tests;`（[sidebar.rs:83-84](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L83-L84)），能访问 crate 私有类型。

2. 在本地（可选）写一个迷你 Rust 程序或在测试里临时加断言，验证：
   - `serde_json::to_string(&SerializedSidebar::default())` 输出 `{"width":null,"active_view":"ThreadList"}`；
   - `serde_json::from_str::<SerializedSidebar>("{}")` 成功且字段全为默认值；
   - `serde_json::from_str::<SerializedSidebar>(r#"{"width":1.0,"active_view":"Archive"}"#)` 成功且 `active_view == History`。

**需要观察的现象**：缺失字段的 JSON 能解析；`"Archive"` 与 `"History"` 都能解析为 `History`。

**预期结果**：三个断言全部成立（第 2 步为可选练习，未在本机验证过输出细节的部分标注待本地验证）。

#### 4.2.5 小练习与答案

**练习**：如果把 `SerializedSidebar` 的 `width` 字段从 `Option<f32>` 改成 `f32`（非可选），会发生什么？

**参考答案**：第一，语义上失去"宽度未持久化"与"宽度为 0"的区分（当前 `None` 表示"沿用默认宽度 300"）；第二，兼容性上，尽管有 `#[serde(default)]`，老数据里显式的 `"width":null` 会让非 Option 的 `f32` 反序列化**失败**，整个 `sidebar_state` 解析失败进而走 `log_err` 静默丢弃——所有持久化状态一次性全丢。Option + default 的组合是刻意的防御。

### 4.3 serialized_state / restore_serialized_state：写入与恢复的对称实现

#### 4.3.1 概念说明

trait 的两个持久化方法在 `impl WorkspaceSidebar for Sidebar` 里成对出现。写入侧是一次纯投影：把运行时字段翻译成可序列化结构体；恢复侧则要多做三件事——**容错、钳制、延迟**。

#### 4.3.2 核心流程

写入：

```
self.width (Pixels) ──f32::from──► width: Some(f32)
self.view (SidebarView) ──match──► active_view (SerializedSidebarView)
                       ──serde_json::to_string(...).ok()──► Option<String>
```

恢复：

```
state: &str
  │ serde_json::from_str::<SerializedSidebar>(state).log_err()
  ├─ 解析失败 → 静默保留默认值（不 panic）
  └─ 解析成功
       ├─ width 有值 → clamp 到 [MIN_WIDTH, MAX_WIDTH] 后赋给 self.width
       └─ active_view == History → cx.defer_in(... show_archive ...)  延迟一拍
  最后无条件 cx.notify()
```

宽度钳制的数学含义：

\[
w_{\text{restore}} = \min(\max(w_{\text{serialized}},\ 200),\ 800)
\]

其中 200 与 800 分别是 `MIN_WIDTH` 与 `MAX_WIDTH`（[sidebar.rs:104-106](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L104-L106)，默认值 `DEFAULT_WIDTH = 300` 同处）。存盘的数据可能来自旧版本（当时的合法范围不同）、手改的数据库或另一块分辨率差异巨大的屏幕，恢复入口是**不可信输入的边界**，必须钳制。

#### 4.3.3 源码精读

写入侧，[sidebar.rs:7721-7730](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L7721-L7730) 把 `SidebarView` 映射到可序列化枚举：

```rust
    fn serialized_state(&self, _cx: &App) -> Option<String> {
        let serialized = SerializedSidebar {
            width: Some(f32::from(self.width)),
            active_view: match self.view {
                SidebarView::ThreadList => SerializedSidebarView::ThreadList,
                SidebarView::Archive(_) => SerializedSidebarView::History,
            },
        };
        serde_json::to_string(&serialized).ok()
    }
```

注意 `Archive(_)` 的下划线——实体句柄被有意丢弃，只有"在归档视图"这个事实被留下。

恢复侧，[sidebar.rs:7732-7749](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L7732-L7749)：

```rust
    fn restore_serialized_state(
        &mut self,
        state: &str,
        window: &mut Window,
        cx: &mut Context<Self>,
    ) {
        if let Some(serialized) = serde_json::from_str::<SerializedSidebar>(state).log_err() {
            if let Some(width) = serialized.width {
                self.width = px(width).clamp(MIN_WIDTH, MAX_WIDTH);
            }
            if serialized.active_view == SerializedSidebarView::History {
                cx.defer_in(window, |this, window, cx| {
                    this.show_archive(window, cx);
                });
            }
        }
        cx.notify();
    }
```

四个细节值得咀嚼：

1. **`.log_err()` 容错**：解析失败返回 `None` 并记日志，整个 `if let` 跳过——坏数据不会 panic，侧边栏以全新默认状态启动。持久化恢复代码的铁律：宁可丢状态不可崩窗口。
2. **宽度钳制**：`px(width).clamp(MIN_WIDTH, MAX_WIDTH)`，与 `set_width` 的钳制范围一致。
3. **`cx.defer_in` 延迟**：归档视图恢复不是立即执行而是推迟到当前 update 循环结束后。原因有两层：其一，`show_archive` 内部有两道守卫（活跃工作区必须存在、工作区里必须有 `AgentPanel`），启动恢复时这些依赖可能尚未就绪，立即调用可能踩空；其二，defer 让 `restore_serialized_state` 先把宽度等简单字段设置完，再做视图切换这种有级联副作用的操作。守卫失败时 `show_archive` 直接 `return`（[sidebar.rs:7540-7549](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L7540-L7549)），静默降级为线程列表视图，不 panic。
4. **末尾无条件 `cx.notify()`**：即使什么都没恢复（解析失败），也标记重渲染一次，保证 UI 与状态一致。

宽度钳制在 trait 实现里还有第二个入口，[sidebar.rs:7682-7685](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L7682-L7685)：

```rust
    fn set_width(&mut self, width: Option<Pixels>, cx: &mut Context<Self>) {
        self.width = width.unwrap_or(DEFAULT_WIDTH).clamp(MIN_WIDTH, MAX_WIDTH);
        cx.notify();
    }
```

`None` 表示"重置为默认"（用户双击拖拽手柄时触发），同样是先默认后钳制。**同一个不变式（宽度 ∈ [200, 800]）在所有写入口都被维护**——这是防御式编程在多个入口的一致应用。

#### 4.3.4 代码实践

**实践目标**：通过阅读现成测试确认"延迟恢复"真的在 defer 之后才生效。

**操作步骤**：

1. 阅读 [sidebar_tests.rs:793-824](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L793-L824) 的 `test_restore_serialized_archive_view_does_not_panic`。它手工构造了一个 `History` 视图的 blob，恢复后断言 `sidebar.view` 是 `Archive(_)`。
2. 注意断言前有一行 `cx.run_until_parked()`——它把 defer 队列里的 `show_archive` 泵出来执行。如果删掉这一行再运行（本地实验），断言大概率失败，因为 defer 的闭包还没跑，视图仍是默认的 `ThreadList`。

**需要观察的现象**：恢复调用返回时视图尚是 `ThreadList`；`run_until_parked` 之后才变成 `Archive`。

**预期结果**：证明 `defer_in` 的时序；删除 `run_until_parked` 后的失败现象为推断，待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `serialized_state` 只做"投影"而不顺带触发落盘？

**参考答案**：职责分离。`serialized_state` 是被动的数据出口（谁问给谁），落盘时机由拥有持久化职责的 `MultiWorkspace` 决定（见 4.4）。若投影方法自带副作用，多次调用会产生不必要的写放大，也无法在组装 `MultiWorkspaceState` 信封时保持纯粹。

**练习 2**：假设旧版本 `MAX_WIDTH` 是 500，用户存了 460 的宽度，新版本恢复后是多少？

**参考答案**：460。因为 460 落在当前 [200, 800] 区间内，钳制不改变它。只有超出区间的值才会被拉回边界——钳制保护的是"越界值"，不追溯历史合法值。

### 4.4 SidebarEvent::SerializeNeeded：谁触发落盘

#### 4.4.1 概念说明

侧边栏状态变了，谁来决定"现在该写磁盘"？答案是一套精巧的事件接力：**sidebar 只在自己无法回头的变化点发一个轻量事件，其余全部交给宿主**。

#### 4.4.2 核心流程

完整时序（从视图切换到磁盘）：

```
用户点击底部栏「历史」按钮
  └─ Sidebar::show_archive
       ├─ 新建 ThreadsArchiveView 子实体、接订阅、设 self.view
       └─ self.serialize(cx)                    ← 只是 cx.emit(SerializeNeeded)
            └─ MultiWorkspace（register_sidebar 时已订阅）
                 └─ MultiWorkspace::serialize(cx)
                      └─ _serialize_task = cx.spawn(...)   ← 新任务替换旧任务 = 去抖
                           ├─ 组装 MultiWorkspaceState
                           │    └─ sidebar_state = s.serialized_state(cx)  ← 此刻才拉取 blob
                           └─ write_multi_workspace_state(kvp, window_id, state)
                                └─ KVP: scope "multi_workspace_state", key = window_id
```

启动时的反向链路：

```
restore_multiworkspace (workspace.rs:9736)
  └─ apply_restored_multiworkspace_state (workspace.rs:9815)
       ├─ sidebar_open 为真 → multi_workspace.restore_open_sidebar(cx)
       └─ sidebar_state 存在 → sidebar.restore_serialized_state(sidebar_state, window, cx)
            └─ 紧接着 multi_workspace.serialize(cx)   ← 恢复后立即回写一次，防止丢
```

#### 4.4.3 源码精读

**产生侧**。侧边栏的"请求落盘"方法只有两行：

```rust
    fn serialize(&mut self, cx: &mut Context<Self>) {
        cx.emit(workspace::SidebarEvent::SerializeNeeded);
    }
```

[sidebar.rs:926-928](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L926-L928) 不写任何存储，只发事件。它的调用点全 crate 仅两处：`show_archive` 末尾（[sidebar.rs:7598](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L7598)）与 `show_thread_list` 末尾（[sidebar.rs:7607](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L7607)）。即**只有视图切换**由侧边栏主动请求持久化——因为视图是它私有的、宿主无从感知的变化。

那宽度呢？拖拽结束时机由宿主的拖拽手柄掌握，所以持久化也由宿主触发：[multi_workspace.rs:2027-2043](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/workspace/src/multi_workspace.rs#L2027-L2043) 在 `on_mouse_up` 里（双击时先 `set_width(None)` 重置）显式调用 `this.serialize(cx)`。

**消费侧**。注册时接线：

```rust
    pub fn register_sidebar<T: Sidebar>(&mut self, sidebar: Entity<T>, cx: &mut Context<Self>) {
        self._subscriptions
            .push(cx.observe(&sidebar, |_this, _, cx| {
                cx.notify();
            }));
        self._subscriptions
            .push(cx.subscribe(&sidebar, |this, _, event, cx| match event {
                SidebarEvent::SerializeNeeded => {
                    this.serialize(cx);
                }
            }));
        self.sidebar = Some(Box::new(sidebar));
    }
```

[multi_workspace.rs:393-405](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/workspace/src/multi_workspace.rs#L393-L405) 同时挂了两条线：`observe` 把侧边栏的每次重渲染传导为宿主重渲染（不落盘）；`subscribe` 只对 `SerializeNeeded` 落盘。泛型参数 `T: Sidebar` 在装箱为 `Box<dyn SidebarHandle>` 的那一刻被擦除。

**落盘执行**。[multi_workspace.rs:1449-1477](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/workspace/src/multi_workspace.rs#L1449-L1477)：

```rust
    pub fn serialize(&mut self, cx: &mut Context<Self>) {
        self._serialize_task = Some(cx.spawn(async move |this, cx| {
            let Some((window_id, state)) = this
                .read_with(cx, |this, cx| {
                    let state = MultiWorkspaceState {
                        ...
                        sidebar_open: this.sidebar_open,
                        sidebar_state: this.sidebar.as_ref().and_then(|s| s.serialized_state(cx)),
                    };
                    (this.window_id, state)
                })
                .ok()
            else {
                return;
            };
            let kvp = cx.update(|cx| db::kvp::KeyValueStore::global(cx));
            crate::persistence::write_multi_workspace_state(&kvp, window_id, state).await;
        }));
    }
```

三个要点：`_serialize_task` 字段被新任务覆盖时旧任务即被取消——短时间内多次 `SerializeNeeded` 合并为最后一次写入（去抖）；`sidebar_state` 是在组信封的**那一刻**才调用 `s.serialized_state(cx)` 现拉数据，事件本身不携带数据；最终 [persistence.rs:314-325](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/workspace/src/persistence.rs#L314-L325) 的 `write_multi_workspace_state` 把整个 `MultiWorkspaceState` 序列化后写入 KV 存储，scope 为 `"multi_workspace_state"`、key 为窗口 ID 的十进制字符串——**按窗口一份数据**。

紧随其后的 [multi_workspace.rs:1482-1484](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/workspace/src/multi_workspace.rs#L1482-L1484) `flush_serialization` 取出在途任务供退出处理器 await，保证进程退出前最后的写盘完成——异步写与进程生命周期的经典竞态处理。

**启动恢复**。[workspace.rs:9866-9883](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/workspace/src/workspace.rs#L9866-L9883)：

```rust
    if *sidebar_open {
        window_handle
            .update(cx, |multi_workspace, _, cx| {
                multi_workspace.restore_open_sidebar(cx);
            })
            .ok();
    }

    if let Some(sidebar_state) = sidebar_state {
        window_handle
            .update(cx, |multi_workspace, window, cx| {
                if let Some(sidebar) = multi_workspace.sidebar() {
                    sidebar.restore_serialized_state(sidebar_state, window, cx);
                }
                multi_workspace.serialize(cx);
            })
            .ok();
    }
```

先恢复"开着"这个事实（`restore_open_sidebar`，[multi_workspace.rs:502-519](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/workspace/src/multi_workspace.rs#L502-L519)，无遥测版本），再恢复内容 blob，**恢复完立刻回写一次**——若恢复过程中用户就关窗，状态也不至于丢失。

#### 4.4.4 代码实践

**实践目标**：跟踪一次 `SerializeNeeded` 的接力全过程。

**操作步骤**：

1. 在 `show_archive`（[sidebar.rs:7533](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L7533)）里找到 `self.serialize(cx)`，记住它只是 emit；
2. 跳到 `register_sidebar` 的 subscribe 闭包，确认事件处理器是 `this.serialize(cx)`（注意两个 `serialize` 是**不同类型上的不同方法**：一个是 `Sidebar::serialize`，一个是 `MultiWorkspace::serialize`）；
3. 进入 `MultiWorkspace::serialize`，找到 `sidebar_state` 赋值行，确认它调用的是 `SidebarHandle` trait 上的 `serialized_state`；
4. 最后进入 `write_multi_workspace_state` 看落盘的 scope 与 key。

**需要观察的现象**：整条链上没有任何一处搬运数据——事件只携带"需要落盘"的信号，数据在最后组信封时现拉。

**预期结果**：你能画出 4.4.2 节那张时序图，并指出两个 `serialize` 方法各自所属的类型。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `SerializeNeeded` 事件不直接携带 `SerializedSidebar` 数据？

**参考答案**：携带数据就要在 workspace crate 中定义或暴露这个类型，破坏"不透明 blob"契约；而且从 emit 到真正写盘之间状态可能又变了（去抖窗口内），携带数据会写出过期快照。事件只发信号、数据落盘时现拉，既解耦又新鲜。

**练习 2**：拖拽改变宽度后如果不松鼠标直接按 Cmd+Q 退出，宽度会丢吗？

**参考答案**：宽度持久化挂在 `on_mouse_up` 的 `this.serialize(cx)` 上，未松鼠标准确说不会走到该回调；但退出处理器会 await `flush_serialization`，它只能冲刷**已创建**的写盘任务。拖拽中确实没有新任务，所以最后一次落盘的仍是拖拽前宽度——本次拖拽会丢。（结论基于源码推理，待本地验证。）

## 5. 综合实践

**任务**：对照 `test_serialization_round_trip`，编写一个新测试 `test_serialization_round_trip_custom_width`：把侧边栏设为 `px(500.)` 宽并切到归档视图，序列化后恢复到一个**全新**的 `Sidebar` 实体，断言宽度与视图都被还原。

**操作步骤**：

1. 打开 [sidebar_tests.rs:753-791](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L753-L791) 的原测试，注意它的三段式结构：准备（项目 + 多工作区 + 侧边栏 + 线程元数据）→ 变更（`set_width` + `toggle_collapse`）→ 序列化与恢复 → 断言。
2. 在其下方添加以下测试（**示例代码**，模仿 `test_restore_serialized_archive_view_does_not_panic` 的环境要求——恢复归档视图需要 `AgentPanel` 与 `AgentRegistryStore`）：

```rust
#[gpui::test]
async fn test_serialization_round_trip_custom_width(cx: &mut TestAppContext) {
    let project = init_test_project_with_agent_panel("/my-project", cx).await;
    let (multi_workspace, cx) =
        cx.add_window_view(|window, cx| MultiWorkspace::test_new(project.clone(), window, cx));
    let (sidebar, _panel) = setup_sidebar_with_agent_panel(&multi_workspace, cx);
    cx.update(|_window, cx| {
        AgentRegistryStore::init_test_global(cx, vec![]);
    });

    // 自定义宽度并切到归档视图。
    sidebar.update_in(cx, |sidebar, window, cx| {
        sidebar.set_width(Some(px(500.0)), cx);
        sidebar.show_archive(window, cx);
    });
    cx.run_until_parked();

    // 序列化第一个侧边栏。
    let serialized = sidebar.read_with(cx, |sidebar, cx| sidebar.serialized_state(cx));
    let serialized = serialized.expect("serialized_state should return Some");

    // 新建一个侧边栏并恢复。
    let sidebar2 =
        cx.update(|window, cx| cx.new(|cx| Sidebar::new(multi_workspace.clone(), window, cx)));
    cx.run_until_parked();
    sidebar2.update_in(cx, |sidebar, window, cx| {
        sidebar.restore_serialized_state(&serialized, window, cx);
    });
    cx.run_until_parked();

    // 断言宽度与视图都被还原。
    assert_eq!(sidebar2.read_with(cx, |s, _| s.width), px(500.0));
    assert!(sidebar2.read_with(cx, |s, _| matches!(s.view, SidebarView::Archive(_))));
}
```

3. 运行：

```bash
cargo test -p sidebar test_serialization_round_trip_custom_width
```

**需要观察的现象**：

- `serialized` 字符串里应包含 `"width":500.0` 与 `"active_view":"History"`（可在恢复前加一行打印确认）；
- 恢复后的第二个断言能通过，说明 `defer_in` 的 `show_archive` 在两次 `run_until_parked` 之间被泵出执行；
- 额外实验：把 `set_width` 的值改为 `px(5000.0)`，恢复后断言实际宽度是 `px(800.0)`——钳制生效。

**预期结果**：测试通过。本实践代码基于两个现有测试的拼装（`test_serialization_round_trip` 的三段式骨架 + `test_restore_serialized_archive_view_does_not_panic` 的 agent panel 环境），逻辑上应当通过，但**尚未在本机运行，待本地验证**。若 `show_archive` 因某全局存储未初始化而 panic，对照 panic 信息在 `init_test_project_with_agent_panel` 与 `AgentRegistryStore::init_test_global` 附近补齐环境即可。

## 6. 本讲小结

- **契约分层**：`workspace` crate 定义 `Sidebar` trait（含默认实现的 `serialized_state`/`restore_serialized_state`）与对象安全的 `SidebarHandle` 镜像，`sidebar` crate 以 `impl WorkspaceSidebar for Sidebar` 实现；依赖单向（sidebar → workspace），数据以不透明 `Option<String>` 跨界。
- **只存两个**：`SerializedSidebar` 仅持久化宽度（`Option<f32>`）与活跃视图（`SerializedSidebarView`）——其余 30 个字段都能从世界状态全量重推导，这是 u1-l1 建立的教义在持久化层的直接体现。
- **serde 防御**：`#[serde(default)]` 容忍旧数据缺字段，`#[serde(alias = "Archive")]` 让枚举改名后旧会话仍可读、新数据统一输出 `History`。
- **恢复三原则**：`log_err()` 容错不 panic；宽度 `clamp(200, 800)` 把不可信输入拉回合法区间；归档视图恢复走 `cx.defer_in` 延迟一拍，配合 `show_archive` 内部的双守卫静默降级。
- **事件接力**：`SerializeNeeded` 只在视图切换时由侧边栏发出；`register_sidebar` 的订阅把它转为 `MultiWorkspace::serialize`；`_serialize_task` 任务替换实现去抖；数据在组 `MultiWorkspaceState` 信封时才现拉，最终按窗口 ID 写入 KVP 的 `multi_workspace_state` scope。
- **恢复即回写**：启动链 `restore_multiworkspace → apply_restored_multiworkspace_state → restore_serialized_state` 的最后一步是立刻再 serialize 一次，防止恢复后立即退出丢状态。

## 7. 下一步学习建议

本讲是单元八（归档与持久化）的收官。至此你已经看完了 sidebar crate 的全部主干：数据模型（单元二）、重建管线（单元三）、渲染（单元四）、交互（单元五）、激活与创建（单元六）、切换器与归档（单元七、八上半）。建议接下来：

1. 进入单元九的测试主题，从 `u9-l1-test-harness.md` 开始系统学习 `init_test_project`/`setup_sidebar` 脚手架——本讲的综合实践正是它们的直接应用。
2. 若对持久化机制本身感兴趣，可延伸阅读 `crates/workspace/src/persistence.rs` 中 `MultiWorkspaceState` 的读取路径（`read_multi_workspace_state` 与按 `WindowId` 的清理逻辑），以及 `db::kvp::KeyValueStore` 的实现。
3. 对照阅读 `crates/workspace/src/dock.rs` 的 `restore_serialized_state` / `replay_pending_serialized_state`（[dock.rs:828-865](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/workspace/src/dock.rs#L828-L865)），看 dock 面板如何处理"面板实体晚于序列化数据到达"的竞态——与本讲的 `defer_in` 是同一类问题的另一种解法。

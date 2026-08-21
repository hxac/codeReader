# Wayland 客户端：协议对象、layer_shell 与弹出层

## 1. 本讲目标

本讲是「Linux 平台深入」单元的第四讲，深入 `gpui_linux` 三大后端中的 Wayland 后端。学完本讲，你应该能够：

1. 描述 `WaylandClient` 的初始化流水线：连接合成器、从 `wl_registry` 绑定协议对象、把 Wayland socket 与调度通道挂进 calloop 主循环。
2. 理解 Wayland「无全局坐标」安全模型对窗口定位接口的系统性影响：为什么 `primary_display` 返回 `None`、为什么弹出层要靠 positioner「描述」而非「指定」位置、为什么拖动窗口要走 `xdg_toplevel._move` 协商。
3. 说明 `serial.rs` 为什么要按五种 `SerialKind` 分别跟踪最后一次输入序列号，以及 `selection_serial` 的特殊地位。
4. 读懂 `wayland/window.rs` 的「一个 `wl_surface`、三种角色」设计：`Xdg`、`LayerShell`、`Popup` 三态如何由 `WindowKind` 决定，configure/ack 协商如何驱动首帧。
5. 掌握 `layer_shell` 与 `popup` 两组协议映射：锚定位掩码、独占区、键盘交互性，以及锚定弹出层的 anchor/gravity/constraint 三元组。

## 2. 前置知识

### 2.1 Wayland 协议基础：对象、请求、事件

X11（上一讲）把窗口系统做成一台服务器；Wayland 反过来，把**合成器（compositor）**做成唯一有权决定屏幕内容与窗口位置的进程。你的应用是一个客户端，通过 Unix socket 与合成器通信。三个核心概念：

- **协议对象（protocol object）**：Wayland 的一切能力都定义成带接口名的对象，由 `wl_registry` 广播的 **global** 列出。客户端用 `bind` 请求把需要的 global 变成自己的对象（如 `wl_compositor`、`wl_seat`、`xdg_wm_base`）。对象有整数 id，`ObjectId` 就是 Rust 侧对它的封装。
- **请求（request）与事件（event）**：请求是客户端→合成器的调用，事件是合成器→客户端的推送。`wayland-client` crate 用 `Dispatch` trait 把事件分发到你的状态类型上。
- **`wl_surface` 与角色（role）**：`wl_compositor.create_surface` 造出来的 surface 只是一块画布，必须再赋予一个角色才能成为「窗口」：`xdg_toplevel`（普通窗口）、`xdg_popup`（弹出层）、`zwlr_layer_surface_v1`（钉在屏幕边缘的面板）。surface 的状态是**双缓冲**的——修改后要 `commit` 才生效。

### 2.2 无全局坐标：Wayland 最重要的安全决定

X11 客户端可以查询并设置任何窗口的绝对屏幕坐标；Wayland 刻意拿掉了这个能力——**客户端根本不知道自己的窗口在屏幕上的哪里**，也不知道别的窗口在哪里。好处是屏幕信息不会泄漏给恶意应用。代价是一连串接口变形，本讲会逐一遇到：

| 想做的事 | X11 的做法 | Wayland 的做法 |
| --- | --- | --- |
| 把窗口移到 (x, y) | 直接设置坐标 | 不可能；只能拖动时请求 `xdg_toplevel._move(seat, serial)` 让合成器代劳 |
| 在鼠标旁弹菜单 | 算出全局坐标后 CreateWindow | 用 `xdg_positioner` 描述「锚在父窗口某矩形旁边」，合成器决定最终位置 |
| 查询哪个是主显示器 | 读 RandR 输出列表 | 协议没有主显示器概念，`primary_display` 只能返回 `None` |
| 做一个 dock/面板 | override-redirect 窗口 + 定位 | `layer_shell` 协议：声明层、锚边、独占区，由合成器放置 |

### 2.3 serial：输入事件的「防伪凭证」

Wayland 合成器给每个输入事件附带一个单调递增的 32 位序号 **serial**。凡是「可能抢焦点/抢所有权」的请求——设置光标、声明剪贴板所有权、弹出层抓取（grab）、激活窗口——都必须**引用一个来自真实用户交互的 serial**，否则合成器直接忽略。这是防止后台应用伪造用户意图的机制，也是本讲 `serial.rs` 存在的全部理由。注意 serial 会在 `u32` 上界回绕（rollover），所以「最新」要按事件到达顺序判断而不是数值大小判断。

### 2.4 承接前几讲的认知

- **u5-l1**：`LinuxPlatform<P>` 外壳、`LinuxClient` 契约（定义在 [../gpui_linux/src/linux/platform.rs:L51](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L51) 起）、`LinuxCommon` 公共状态。本讲的 `WaylandClient` 就是该契约的 Wayland 实现。
- **u4-l3**：`LinuxDispatcher` 不拥有主循环；主循环归各客户端所有。本讲会看到 `WaylandClient::new` 里 `LinuxCommon::new(event_loop.get_signal())` 的接线处。
- **u3-l2**：`PlatformWindow` 契约（含 raw-window-handle 双 supertrait）；本讲的 `RawWindow` 用 `wl_surface` 指针兑现它。
- **u2-l3 / u2-l4**：显示器契约与剪贴板契约；本讲看它们在 Wayland 上的形态。
- **u5-l3**：X11 后端的对照。本讲多处会以「X11 这样、Wayland 那样」加深理解。

## 3. 本讲源码地图

本讲涉及的关键文件（路径相对于 `crates/gpui_platform/`，兄弟 crate 用 `../` 前缀）：

| 文件 | 作用 |
| --- | --- |
| [../gpui_linux/src/linux/wayland/client.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs) | Wayland 后端主体：`Globals` 协议对象集合、`WaylandClientState` 状态全集、`WaylandClient::new()` 初始化流水线、`LinuxClient` 契约实现、约 30 个 `Dispatch` 事件处理实现。约 2900 行，是 Wayland 后端的心脏 |
| [../gpui_linux/src/linux/wayland/window.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs) | 窗口实现：`WaylandWindowState` 与三态 `WaylandSurfaceState`、configure 协商、帧回调、CSD 装饰、`PlatformWindow` 契约实现 |
| [../gpui_linux/src/linux/wayland/serial.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/serial.rs) | 输入序号跟踪器：`SerialKind`、`SerialTracker`、`selection_serial` 规则，附 5 个单元测试 |
| [../gpui_linux/src/linux/wayland/display.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/display.rs) | `WaylandDisplay`：`PlatformDisplay` 契约的最小实现，uuid 由显示器名推导 |
| [../gpui_linux/src/linux/wayland/popup.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/popup.rs) | popup 枚举值到 `xdg_positioner` 协议值的纯映射函数 |
| [../gpui_linux/src/linux/wayland/layer_shell.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/layer_shell.rs) | layer_shell 枚举值到 `zwlr_layer_shell_v1` 协议值的纯映射函数 |
| [../gpui_linux/src/linux/wayland/cursor.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/cursor.rs) | XCursor 主题加载与「无 cursor-shape-v1 时的曲面临时光标」后备路径 |
| [../gpui_linux/src/linux/wayland/clipboard.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/clipboard.rs) | Wayland 剪贴板：选区所有权模型下的本地暂存 + data offer 读取 |
| [../gpui_linux/src/linux/wayland.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland.rs) | 模块聚合文件：声明 7 个子模块，并定义 `to_shape`（`CursorStyle` → cursor-shape-v1 `Shape`） |
| [../gpui/src/platform/layer_shell.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui/src/platform/layer_shell.rs) | 平台无关的 layer_shell 数据模型：`Layer`、`Anchor`、`KeyboardInteractivity`、`LayerShellOptions` |
| [../gpui/src/platform/popup.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui/src/platform/popup.rs) | 平台无关的 popup 数据模型：`PopupOptions`、`PopupAnchor`、`PopupGravity`、`PopupConstraintAdjustment` |
| [../gpui/examples/layer_shell.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui/examples/layer_shell.rs) | 官方 layer_shell 示例：屏幕底部一块半透明的时钟面板 |
| [../gpui/examples/window.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui/examples/window.rs) 与 [../gpui/examples/input.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui/examples/input.rs) | 综合实践的运行载体：开窗/光标切换用前者，剪贴板读写用后者 |

## 4. 核心概念与源码讲解

### 4.1 WaylandClient：连接、globals 绑定与 calloop 主循环

#### 4.1.1 概念说明

`WaylandClient` 是 `LinuxClient` 契约在 Wayland 上的实现，外形与 X11 后端同构——一个包装了共享可变状态的元组结构体：

```rust
#[derive(Clone)]
pub struct WaylandClient(Rc<RefCell<WaylandClientState>);
```

定义于 [../gpui_linux/src/linux/wayland/client.rs:L648-L649](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L648-L649)（原文即如此，右括号在源码中为 `>)`）。它的独特之处是一个**双指针结构**：`WaylandClient` 持有 `Rc`，而事件分发用的 `WaylandClientStatePtr` 持有 `Weak`：

```rust
/// This struct is required to conform to Rust's orphan rules, so we can dispatch on the state but hand the
/// window to GPUI.
#[derive(Clone)]
pub struct WaylandClientStatePtr(Weak<RefCell<WaylandClientState>>);
```

见 [../gpui_linux/src/linux/wayland/client.rs:L428-L431](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L428-L431)。注释点破了设计动机：`wayland-client` 的 `Dispatch<State>` 要求 State 作为分发上下文，而孤儿规则不允许为外部类型 `Rc<RefCell<WaylandClientState>>` 实现外部 trait，所以包一层本地新类型；用 `Weak` 则让事件循环不强制延续客户端生命。窗口（交给 GPUI 的 `Box<dyn PlatformWindow>`）经 `WaylandClientStatePtr` 反向持有客户端，形成可断开的环。

`WaylandClientState`（[L305-L361](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L305-L361)）聚合了后端全部状态，可按职责分组：

| 分组 | 代表字段 | 用途 |
| --- | --- | --- |
| 序号 | `serial_tracker` | 4.2 的主角 |
| 协议对象 | `globals`、`wl_seat`、`wl_pointer`、`wl_keyboard`、`data_device`、`primary_selection`、`text_input` | 与合成器的会话资源 |
| 窗口表 | `windows: HashMap<ObjectId, WaylandWindowStatePtr>`、`mouse_focused_window`、`keyboard_focused_window` | surface id → 窗口；焦点镜像 |
| 显示器 | `outputs`、`in_progress_outputs`、`wl_outputs` | 4.1.3(4) 的被动累积 |
| 输入状态 | `modifiers`、`capslock`、`click`、`repeat`、`keymap_state`、`compose_state` | 修饰键、双击计数、按键重复、xkb |
| 光标 | `cursor_style`、`cursor_hidden_window`、`cursor`、`cursor_shape_device` | 样式缓存 + 两条设置路径 |
| 剪贴板 | `clipboard`、`data_offers`、`primary_data_offer` | 所有权与接收 |
| 公共 | `common: LinuxCommon`、`loop_handle`、`event_loop` | u5-l1 讲过的外壳借出物 |

#### 4.1.2 核心流程

`WaylandClient::new()`（[L702-L921](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L702-L921)）的初始化流水线：

```text
1. 取走环境变量 XDG_ACTIVATION_TOKEN（启动激活令牌，用后即删防止传给子进程）
2. Connection::connect_to_env()              ← 连接 $WAYLAND_DISPLAY 指定的合成器
3. registry_queue_init()                     ← 拿到 wl_registry 快照(GlobalList) + 事件队列
4. 遍历 global 列表：
     wl_seat   → 手工 bind（版本钳制 5..=9，低于 5 直接 panic，因为依赖 wl_pointer.frame）
     wl_output → 手工 bind（版本钳制 2..=4），插入 in_progress_outputs / wl_outputs
5. EventLoop::try_new() + LinuxCommon::new(signal)   ← u4-l3/u5-l1 的调度接线
6. 注册 main_receiver：前台 runnable 经 insert_idle 执行（保证输入事件优先）
7. 注册 wake_receiver：系统唤醒回调
8. detect_compositor_gpu()                   ← 用一条临时连接探测 zwp_linux_dmabuf_v1
9. data_device / primary_selection / Cursor::new / XDPEventSource
10. 组装 WaylandClientState
11. WaylandSource::insert(handle)            ← 把 Wayland socket 注册为 calloop 事件源
```

运行期事件循环 `run()`（[L1135-L1150](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L1135-L1150)）只是 `event_loop.run(None, &mut WaylandClientStatePtr(..), |_| {})`——阻塞在 calloop 上，直到 `LinuxCommon` 的 quit 路径拉动 `LoopSignal`（u5-l1）。循环上挂着的事件源：Wayland socket（协议事件）、main_receiver（前台任务）、wake_receiver（系统唤醒）、XDP 事件源（外观/按钮布局/光标主题变化）、剪贴板发送用的 generic fd 源、以及键盘重复等计时器。

#### 4.1.3 源码精读

**(1) Globals：一次 bind 出全部协议对象**

```rust
#[derive(Clone)]
pub struct Globals {
    pub qh: QueueHandle<WaylandClientStatePtr>,
    pub activation: Option<xdg_activation_v1::XdgActivationV1>,
    pub compositor: wl_compositor::WlCompositor,
    ...
    pub layer_shell: Option<zwlr_layer_shell_v1::ZwlrLayerShellV1>,
    ...
}
```

`Globals`（[L202-L225](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L202-L225)）把 bind 结果分成两类：`Option` 的是**可选协议**（缺失时对应功能降级），非 `Option` 的 `compositor`/`shm`/`seat`/`wm_base` 是最小渲染与 shell 依赖、bind 失败直接 `unwrap` 崩溃。绑定逻辑在 `Globals::new`（[L227-L269](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L227-L269)），每个 `globals.bind(&qh, 版本区间, ())` 都显式声明能接受的协议版本区间，可选的一律 `.ok()`。本讲后续模块的三个主角都在这里露面：`layer_shell`、`wm_base`（popup 也要它）、`decoration_manager`。

**(2) 前台任务的接法：insert_idle**

```rust
handle
    .insert_source(main_receiver, {
        let handle = handle.clone();
        move |event, _, _: &mut WaylandClientStatePtr| {
            if let calloop::channel::Event::Msg(runnable) = event {
                handle.insert_idle(|_| {
                    let location = runnable.metadata().location;
                    let spawned = runnable.metadata().spawned;
                    profiler::update_running_task(spawned, location);
                    runnable.run();
                    profiler::save_task_timing();
                });
            }
        }
    })
    .unwrap();
```

见 [L746-L761](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L746-L761)。这与 u4-l3 讲过的结构一致：调度器投递的 runnable 不立即执行，而是排成 idle 回调，让已就绪的 Wayland 协议事件（输入）先被处理。对比 X11 侧的增强（每个 runnable 后排空 x11rb 缓冲），Wayland 侧不需要——`WaylandSource` 直接监听 socket，协议事件与 calloop 的感知范围一致。

**(3) 显示器：被动累积，直到 Done 才完整**

Wayland 不允许客户端主动查询屏幕布局，显示器几何只能等合成器推事件。`InProgressOutput`（[L272-L295](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L272-L295)）逐字段收集 `name`/`scale`/`position`/`size`/`subpixel`，`complete()` 要求 position 与 size 都到齐才产出 `Output`。事件侧：

```rust
wl_output::Event::Geometry { x, y, subpixel, .. } => {
    in_progress_output.position = Some(point(DevicePixels(x), DevicePixels(y)));
    ...
}
wl_output::Event::Mode { width, height, .. } => {
    in_progress_output.size = Some(size(DevicePixels(width), DevicePixels(height)))
}
wl_output::Event::Done => {
    if let Some(complete) = in_progress_output.complete() {
        state.outputs.insert(output.id(), complete);
    }
    state.in_progress_outputs.remove(&output.id());
}
```

见 [L1451-L1472](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L1451-L1472)——`wl_output.Done` 是「这批信息发完了」的边界。之后 `displays()`（[L929-L942](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L929-L942)）把每个 `Output` 包成 `WaylandDisplay`，物理像素除以 scale 换算逻辑像素（u2-l3 讲过该约定）。而 `primary_display()` 直接返回 `None`（[L960-L962](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L960-L962)），`window_stack()` 也返回 `None`（[L1219-L1221](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L1219-L1221)）——这两个「拿不到」都是 2.2 节安全模型的直接后果。

`WaylandDisplay` 本体在 [../gpui_linux/src/linux/wayland/display.rs:L12-L42](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/display.rs#L12-L42)：`id()` 用 `ObjectId::protocol_id`（连接期内稳定、跨连接无意义），`uuid()` 用显示器名做 v5 DNS 命名空间哈希——协议没有给稳定硬件标识，name 是能拿到的最好材料，name 为空则报错。对照 u2-l3：X11 侧干脆给全零占位 uuid，Wayland 至少能从 name 推导。

**(4) 拖动窗口为什么要 serial**

`PlatformWindow::start_window_move` 的实现最能体现「无全局坐标」：

```rust
fn start_window_move(&self) {
    let state = self.borrow();
    let serial = state.client.get_serial(SerialKind::MousePress);
    if let Some(toplevel) = state.surface_state.toplevel() {
        toplevel._move(&state.globals.seat, serial.as_raw());
    }
}
```

见 [../gpui_linux/src/linux/wayland/window.rs:L1769-L1775](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L1769-L1775)。客户端不发坐标，只说「用户正在我的 surface 上按着鼠标，请代为开始交互式移动」，并附上那一次按下的 serial 作凭证；之后的位置全程由合成器掌握。`start_window_resize`（[L1786-L1795](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L1786-L1795)）同理，只是多带一个 `ResizeEdge`。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 `WaylandClient::new` 的绑定流水线在真实协议流里的样子。

**操作步骤**：

1. 确认处于 Wayland 会话（`echo $WAYLAND_DISPLAY` 非空）。
2. 运行 `WAYLAND_DEBUG=1 cargo run -p gpui --example window 2> protocol.log`。`WAYLAND_DEBUG` 是 libwayland 的调试开关（`wayland-backend` 在 gpui_linux 里启用 `client_system` + `dlopen` 特性走系统库，见 [../gpui_linux/Cargo.toml:L94-L97](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/Cargo.toml#L94-L97)），会把双向协议消息打进 stderr。
3. 在 `protocol.log` 里依次找：`wl_registry.global` 一串广播、对 `wl_seat`/`wl_output` 的 `wl_registry.bind`、`xdg_wm_base`/`wl_compositor`/`wl_shm` 的 bind。
4. 对照 4.1.3(1) 的 `Globals` 字段清单，标记日志里出现/缺席的协议：例如你的合成器若没有 `zwlr_layer_shell_v1`，日志中就不会有它的 bind。

**需要观察的现象**：绑定请求携带版本号，且 `wl_seat` 的版本落在 5..=9 区间。

**预期结果**：日志中 global 列表与你桌面环境的合成器实现一致（GNOME 的 Mutter、KDE 的 KWin、wlroots 系的 sway 支持的协议各不相同），缺席的协议恰好对应 `Globals` 里的 `None` 字段。具体输出依赖本机合成器，「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `wl_seat` 版本低于 5 时 `wl_seat_version` 直接 panic，而 `wl_output` 用 clamp？

答案：见 [L673-L700](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L673-L700) 的注释——`wl_pointer.frame` 事件自 wl_seat 版本 5 起才存在，而指针事件的处理依赖 frame 做批量结算，缺了它功能性地残废，不如启动期 fail-fast；wl_output 的老版本只是信息少一点，钳到 2 仍可用。

**练习 2**：`Drop for WaylandClient`（[L651-L669](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L651-L669)）里为什么先 `state.windows.clear()` 再逐个 release 输入设备？

答案：先清窗口表会触发各 `WaylandWindow` 的 `Drop`（若没有其他持有者），而窗口析构会调度异步的 `drop_window` 清理；先把客户端侧的窗口引用断掉，能保证后续 `wl_pointer.release()` 等销毁请求发出时不再有窗口事件会引用这些设备对象。

**练习 3**：`detect_compositor_gpu`（[L1287-L1304](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L1287-L1304)）为什么要另开一条连接，而不是复用主连接？

答案：它只需要一次 roundtrip 读取 `zwp_linux_dmabuf_v1` 的默认 feedback 拿到合成器所用 GPU 的设备号（用于渲染器选择同一 GPU，避免跨设备拷贝）；用独立的 `DmabufProbeState` 临时连接可以把这次探测与主连接的 `WaylandClientState` 分发状态完全隔离，探测失败也只是返回 `None` 不影响启动。

### 4.2 wayland::serial：输入序号跟踪器

#### 4.2.1 概念说明

2.3 节说过：改光标、设剪贴板、弹菜单抓键盘，都要向合成器出示「这确实来自用户操作」的 serial。但 Wayland 协议对不同请求认的 serial 来源并不相同——`wl_pointer.set_cursor` 认指针进入/按键类事件的 serial，`wl_data_device.set_selection` 认任意**按键或指针按下**的 serial，`xdg_popup.grab` 认**按下**事件的 serial（release 的不行）。于是 gpui_linux 不存「一个最新 serial」，而是按用途分五类跟踪（[serial.rs:L3-L10](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/serial.rs#L3-L10)）：

| SerialKind | 更新时机 | 典型消费者 |
| --- | --- | --- |
| `MouseEnter` | `wl_pointer.enter` 事件 | `set_cursor_style`、隐藏/恢复光标 |
| `MousePress` | `wl_pointer.button` **按下**沿 | 拖动/改尺寸/弹出层抓取/激活令牌 |
| `KeyPress` | `wl_keyboard.key` **按下**沿 | 弹出层抓取的备选 |
| `InputMethod` | `zwp_text_input_v3` 事件 | 输入法相关提交 |
| `DataDevice` | `wl_data_device` 事件 | 数据设备交互 |

其中只有 `KeyPress` 与 `MousePress` 会同时写入 `selection_serial`（剪贴板/主选区所有权专用），其余类别一概不算数——这是 `wl_data_device.set_selection` 对「用户驱动事件」的要求。

#### 4.2.2 核心流程

```text
事件到达（client.rs 各 Dispatch 实现）
    │ serial 随事件携带
    ▼
SerialTracker::update(kind, serial)
    ├── KeyPress/MousePress → 同时刷新 selection_serial（按到达顺序，不看数值大小）
    └── 存入 serials[kind]
    ▼
消费者按需读取
    ├── get(MouseEnter)  → wl_pointer.set_cursor
    ├── get(MousePress)  → toplevel._move / resize / popup grab / activation token
    ├── selection_serial() → data_device.set_selection / primary_selection.set_selection
    └── get(kind) 未跟踪时返回 Serial(0)（调用方自行判断是否可用）
```

回绕问题的处理：`selection_serial` 只在 `update` 里被「最后一个写入者」覆盖，从不做数值比较，所以 `0xffff_fff0` 之后再来 `0x0000_0010`，后者即是最新——单元测试 [serial.rs:L97-L104](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/serial.rs#L97-L104) 专门固化了这一点。注意一个对比：4.4 将看到的弹出层抓取处用了两次 serial 的 `u32::max()` 挑较大者，那是「同一时刻取更近的一次按下」的局部近似，与 `selection_serial` 的到达序模型是两回事。

#### 4.2.3 源码精读

**(1) SerialTracker 本体**

```rust
pub(crate) struct SerialTracker {
    serials: HashMap<SerialKind, Serial>,
    selection_serial: Option<SelectionSerial>,
}

impl SerialTracker {
    pub fn update(&mut self, kind: SerialKind, value: u32) {
        let serial = Serial(value);

        if matches!(&kind, SerialKind::KeyPress | SerialKind::MousePress) {
            self.selection_serial = Some(SelectionSerial(serial));
        }

        self.serials.insert(kind, serial);
    }

    pub fn get(&self, kind: SerialKind) -> Serial {
        self.serials.get(&kind).copied().unwrap_or(Serial(0))
    }
```

见 [serial.rs:L30-L60](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/serial.rs#L30-L60)。`Serial`/`SelectionSerial` 是两个不含行为的新类型（[L12-L28](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/serial.rs#L12-L28)），类型隔离防止把「任意 serial」误传给只认选区 serial 的接口。`get` 对未跟踪类别返回 0，调用方以 `serial != 0` 判断可用性。

**(2) 生产者：两个典型写入点**

键盘按下沿写入 `KeyPress`（[client.rs:L1795-L1803](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L1795-L1803)）：

```rust
wl_keyboard::Event::Key { serial, key, state: WEnum::Value(key_state), .. } => {
    if key_state == wl_keyboard::KeyState::Pressed {
        state.serial_tracker.update(SerialKind::KeyPress, serial);
    }
    ...
```

指针按下沿写入 `MousePress`，且源码注释明确说明为什么只记按下（[client.rs:L2150-L2160](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L2150-L2160)）：

```rust
wl_pointer::Event::Button { serial, button, state: WEnum::Value(button_state), .. } => {
    // Record presses only. Requests referencing this serial (popup grabs,
    // interactive moves) are declined when given a release serial.
    if button_state == wl_pointer::ButtonState::Pressed {
        state.serial_tracker.update(SerialKind::MousePress, serial);
    }
```

**(3) 消费者一：set_cursor_style（走 MouseEnter）**

```rust
let serial = state.serial_tracker.get(SerialKind::MouseEnter);
if let Some(cursor_shape_device) = &state.cursor_shape_device {
    cursor_shape_device.set_shape(serial.as_raw(), to_shape(style));
} else if let Some(focused_window) = &state.mouse_focused_window {
    // cursor-shape-v1 isn't supported, set the cursor using a surface.
    ...
    state.cursor.set_icon(&wl_pointer, serial.as_raw(),
        cursor_style_to_icon_names(style), scale);
}
```

见 [client.rs:L1068-L1084](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L1068-L1084)。两条路径：优先 `wp_cursor_shape_device_v1.set_shape`（传枚举，合成器自己渲染），后备是把 XCursor 主题里的位图 attach 到一块专用 surface 再 `wl_pointer.set_cursor`（[cursor.rs:L94-L151](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/cursor.rs#L94-L151)，含热点坐标换算与 `set_buffer_scale`）。`CursorStyle` 到协议枚举的翻译表 `to_shape` 在 [wayland.rs:L18-L42](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland.rs#L18-L42)，主题名后备链在 `set_icon` 里：指定名 → `DEFAULT_CURSOR_ICON_NAME` → 放弃并告警。

**(4) 消费者二：write_to_clipboard（走 selection_serial）**

```rust
if state.mouse_focused_window.is_some() || state.keyboard_focused_window.is_some() {
    state.clipboard.set(item);
    let Some(serial) = state.serial_tracker.selection_serial() else {
        log::warn!(
            "Skipping Wayland clipboard ownership request because no keyboard or pointer press serial has been received"
        );
        return;
    };
    let data_source = data_device_manager
        .create_data_source(&state.globals.qh, DataSourceKind::Clipboard);
    for mime_type in TEXT_MIME_TYPES {
        data_source.offer(mime_type.to_string());
    }
    data_source.offer(state.clipboard.self_mime());
    data_device.set_selection(Some(&data_source), serial.as_raw());
}
```

见 [client.rs:L1177-L1200](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L1177-L1200)。这段浓缩了 Wayland 剪贴板与 X11 的同与不同：同为**选区所有权模型**（数据留在拥有者进程，`write_to_clipboard` 只是把 `ClipboardItem` 存进本地 `state.clipboard.set`，见 [clipboard.rs:L161-L167](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/clipboard.rs#L161-L167)）；不同的是无需常驻服务线程与隐藏窗口——合成器直接把读取方的文件描述符经 `wl_data_source.send` 递过来，`Clipboard::send`（[clipboard.rs:L183-L197](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/clipboard.rs#L183-L197)）把本地文本写进 fd（经 calloop generic 源异步写，[L235 起](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/clipboard.rs#L235)）。还有一个省 IPC 的小技巧：offer 列表里混入 `self_mime()`（值为 `pid/<进程号>`，[clipboard.rs:L149](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/clipboard.rs#L149)）；读取时若发现对方 offer 带着自己的 pid 串，说明「拥有者还是我自己」，直接返回本地缓存（[clipboard.rs:L205-L207](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/clipboard.rs#L205-L207)）。

#### 4.2.4 代码实践

**实践目标**：验证 `selection_serial` 的到达序规则与类别过滤规则。

**操作步骤**：

1. 运行仓库内现成单元测试：`cargo test -p gpui_linux serial`（过滤器会命中 serial.rs 测试模块里的全部 5 个测试）。
2. 打开 [serial.rs:L77-L125](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/serial.rs#L77-L125)，逐个测试对照断言：哪个测试证明 `InputMethod`/`MouseEnter`/`DataDevice` 不影响 selection_serial？哪个证明回绕后按到达序取胜？哪个证明 0 也是合法的选区 serial？
3. （选做）在 `WaylandClientState` 上人为构造场景：把 4.2.3(4) 的 `write_to_clipboard` 在应用一启动、尚未收到任何按键时调用一次（例如在示例的 `run` 回调里直接 `cx.write_to_clipboard(...)`），观察日志中的 warn。

**需要观察的现象**：第 1 步测试全绿；第 3 步出现 "Skipping Wayland clipboard ownership request..." 告警。

**预期结果**：测试通过是源码保证的确定行为（但测试编译需拉起 gpui_linux 全部依赖，环境缺系统库时可能失败，「待本地验证」）；第 3 步的告警在按任意键或点一次鼠标后消失，因为 `selection_serial` 从 `None` 变为 `Some`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `set_cursor_style` 用 `MouseEnter` 的 serial 而不是 `MousePress`？

答案：`wl_pointer.set_cursor`（以及 `set_shape`）语义上是「在指针位于我的 surface 期间改光标」，合成器校验的是与指针 enter 同源交互链的 serial；gpui 在 `wl_pointer.enter` 事件里记录 enter serial（[client.rs:L2044](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L2044)），改样式时直接取用；且 enter 后每次重新应用样式（如 2055-2067 行在 enter 事件里重设）都用事件自带 serial，保证凭证新鲜。`MousePress` 的 serial 理论上晚于 enter 也可用，但按类别取 enter 更贴合「指针交互」语义。

**练习 2**：`hide_cursor_until_mouse_moves` 与 `restore_cursor_after_hide`（[client.rs:L594-L645](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L594-L645)）是怎么把光标藏起来又还回来的？

答案：藏：`wl_pointer.set_cursor(serial, None, 0, 0)`——buffer 传 `None` 即不显示任何光标图像，同时记下 `cursor_hidden_window` 防止后续 `set_cursor_style` 覆盖掉「不可见」状态。还：从 `cursor_style` 缓存读回样式（所以 `set_cursor_style` 在隐藏期间仍照常更新缓存），再走 4.2.3(3) 的两条路径之一重新 set。这呼应 u3-l4 讲过的「打字藏光标」链路。

**练习 3**：如果把 `Serial` 与 `SelectionSerial` 合并成一个类型，最可能引发什么 bug？

答案：类型不隔离后，任何 serial（比如 `DataDevice` 的）都能被传给 `set_selection`，编译器不再拦截；合成器会因凭证不是用户按键/按下事件而忽略请求，剪贴板「写了却读不到」且无错误返回——正是 Rust 新类型模式要消灭的那类静默错误。

### 4.3 wayland::window：一个 wl_surface，三种角色，configure 协商

#### 4.3.1 概念说明

`WaylandWindow` 的核心设计是**三态合一**：`WaylandWindowState` 承载所有窗口公共状态（渲染器、bounds、scale、回调、焦点镜像……），而「这个窗口在社会上是什么角色」由 `surface_state` 枚举决定：

```rust
pub enum WaylandSurfaceState {
    Xdg(WaylandXdgSurfaceState),
    LayerShell(WaylandLayerSurfaceState),
    Popup(WaylandPopupSurfaceState),
}
```

见 [window.rs:L135-L139](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L135-L139)。三态分别对应 `WindowKind::Normal/Floating/Dialog`、`WindowKind::LayerShell`、`WindowKind::AnchoredPopup`（契约枚举见 [../gpui/src/platform.rs:L2043-L2070](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui/src/platform.rs#L2043-L2070)，其中 `LayerShell` 变体带 `#[cfg(all(target_os = "linux", feature = "wayland"))]` 门控）。后续 4.4、4.5 分别展开后两个角色，本模块先把公共骨架讲清：创建链路、configure/ack 协商、帧回调与装饰。

另一个贯穿性概念是 **CSD（客户端装饰）**：Wayland 上窗口标题栏要么由合成器画（SSD，需 `xdg-decoration` 协议支持），要么由应用自己画（CSD）。GPUI 默认 CSD——`WaylandWindowState::new` 里 `decorations: WindowDecorations::Client`（[window.rs:L610](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L610)），窗口用 `client_inset` 向合成器申报「边缘这圈是我的 chrome」（`inset()`，[L664-L669](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L664-L669)；运行期由 `set_client_inset` 更新，[L1884](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L1884)）。Zed 在 Wayland 上的自绘标题栏走的就是这条路；4.5 的 layer_shell 则是另一种「自绘 chrome」——不是装饰普通窗口，而是把整个表面做成 dock/面板。

#### 4.3.2 核心流程

窗口创建到首帧的完整链路：

```text
LinuxClient::open_window (client.rs L982)
  ├── AnchoredPopup → 查父窗口 + 组装 grab serial（4.4）
  ├── 其他 kind     → 父 = keyboard_focused_window
  ├── display_id    → 找目标 wl_output（layer_shell 用）
  ▼
WaylandWindow::new (window.rs L738)
  ├── compositor.create_surface            ← 画布
  ├── WaylandSurfaceState::new             ← 按 WindowKind 赋角色（三选一）
  ├── fractional_scale_manager.get_fractional_scale（可选，分数缩放）
  ├── viewporter.get_viewport（可选）
  └── surface.commit                       ← 第一拍：提交角色
  ▼
合成器异步回应
  xdg_toplevel.configure / layer_surface.configure / xdg_popup.configure
  ▼
handle_toplevel_event 等 → 暂存 in_progress_configure
  ▼
xdg_surface.configure(serial)
  ▼
handle_xdg_surface_event (window.rs L882)
  ├── 应用尺寸/全屏/最大化/平铺状态
  ├── ack_configure(serial)                ← 必须先应答才能提交新 buffer
  ├── set_window_geometry（扣除 CSD inset）
  └── 首次 configure → frame() → 请求 wl_surface.frame 回调 → 绘制
```

此后每次绘制都遵循「frame 回调 → 绘制 → attach/damage/commit → 下一个 frame 回调」的节拍，`frame()` 见 [window.rs:L841-L857](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L841-L857)，回调到达处见 [client.rs:L1383-L1403](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L1383-L1403)（`wl_callback.done` 再触发下一轮 `window.frame()`）。

#### 4.3.3 源码精读

**(1) open_window：外壳里的分发点**

```rust
let (window, surface_id) = WaylandWindow::new(
    handle,
    state.globals.clone(),
    state.gpu_context.clone(),
    compositor_gpu,
    WaylandClientStatePtr(Rc::downgrade(&self.0)),
    params,
    appearance,
    parent,
    popup_grab,
    target_output,
)?;

if window.0.toplevel().is_some() {
    state.consume_startup_activation_token(&window.0.surface());
}
state.windows.insert(surface_id, window.0.clone());
```

见 [client.rs:L1026-L1044](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L1026-L1044)。窗口造好后以 surface 的 `ObjectId` 为键登记进客户端的 `windows` 表——此后所有协议事件都靠这个表路由到窗口（`get_window`，[L1405-L1410](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L1405-L1410)）。`toplevel().is_some()` 时消费启动激活令牌（用 `XDG_ACTIVATION_TOKEN` 环境变量换一次 `activation.activate`，让「从启动器点开」的窗口获得焦点，[L417-L425](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L417-L425)）。

**(2) WaylandWindow::new 与渲染器挂接**

```rust
let surface = globals.compositor.create_surface(&globals.qh, ());
let surface_state = WaylandSurfaceState::new(&surface, &globals, &params,
    parent.clone(), popup_grab, target_output)?;
...
// Kick things off
surface.commit();
```

见 [window.rs:L750-L789](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L750-L789)。渲染器在 `WaylandWindowState::new` 里创建：把 `wl_surface` 的原生指针包进 `RawWindow`（实现 raw-window-handle 的 `HasWindowHandle`/`HasDisplayHandle`，[window.rs:L60-L85](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L60-L85)），交给 `WgpuRenderer::new` 建 GPU surface（[L555-L575](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L555-L575)，present mode 优先 Mailbox）。这兑现了 u3-l2 讲的契约：`PlatformWindow` 必须交出 raw handle 供 wgpu 生态使用。xdg 分支还会顺手 `set_title`/`set_app_id`，并用渲染器的 `max_texture_size` 反向设置窗口 `set_max_size`（[L577-L592](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L577-L592)）。

**(3) configure 协商：ack 是提交新帧的前置条件**

`handle_xdg_surface_event`（[window.rs:L882-L954](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L882-L954)）处理 `xdg_surface.configure`，做四件事：把暂存的 `in_progress_configure` 落到状态上（含「每 vblank 至多一次交互式 resize」的节流）；`ack_configure(serial)`；按 `inset` 扣边后 `set_window_geometry`（CSD 申报）；首次 configure 时 `acknowledged_first_configure = true` 并发起首帧。三态的 ack 与 set_geometry 有各自的实现（[L366-L378](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L366-L378)、[L418-L431](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L418-L431)），注意 layer 分支的注释：**layer surface 不能设位置**，只能 `set_size`——又一处「无全局坐标」的体现。

**(4) 装饰协商与销毁顺序**

`request_decorations`（[window.rs:L1860-L1878](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L1860-L1878)）：若合成器支持 `xdg-decoration` 就 `set_mode` 申请 SSD，否则记一条日志并**留在 CSD**。合成器的实际裁决经 `handle_toplevel_decoration_event`（[L957-L985](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L957-L985)）回流，ClientSide 分支会触发 `appearance_changed` 回调让上层重绘透明背景。`Drop for WaylandWindow`（[L680-L727](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L680-L727)）则严格遵守协议规定的析构顺序：blur → decoration → 角色对象（toplevel/xdg_popup 先于 xdg_surface）→ viewport → `wl_surface` 最后销毁，每一步都带协议文档链接注释；收尾再经前台执行器异步 `close()` + `drop_window` 清理客户端侧登记。

#### 4.3.4 代码实践

**实践目标**：在协议流里辨认一次完整的 configure 协商。

**操作步骤**：

1. `WAYLAND_DEBUG=1 cargo run -p gpui --example window 2> window.log`。
2. 只看**主窗口**（忽略后续点击开的子窗口）的日志段，按顺序摘出：`wl_compositor.create_surface`、`xdg_wm_base.get_xdg_surface`、`xdg_surface.get_toplevel`、`xdg_toplevel.set_title`/`set_app_id`/`set_max_size`、（若有）`zxdg_decoration_manager_v1.get_toplevel_decoration`、`wl_surface.commit`、`xdg_toplevel.configure`、`xdg_surface.ack_configure`、`xdg_surface.set_window_geometry`、`wl_surface.frame`、`wl_surface.attach`/`commit`。
3. 拖动窗口边缘触发 resize，再摘一段 configure/ack，对照 4.3.3(3) 的节流逻辑。
4. 点击 "Custom Titlebar" 按钮开一个无标题栏子窗口，对比它与普通子窗口在 decoration 相关请求上的差异。

**需要观察的现象**：每次 configure 后必有一条 ack_configure；resize 高频时 ack 依旧逐条出现但 `set_window_geometry` 的提交节奏受节流影响。

**预期结果**：请求顺序与 4.3.2 流程图一致是协议规定的确定行为；具体字段值（configure 里的 states 位掩码、尺寸）取决于合成器，「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么首帧必须等 configure，而不能 create_surface 后立刻画？

答案：xdg-shell 的双缓冲状态模型要求：角色（toplevel 等）在首次 configure 到达并被告知初始尺寸/状态之前，客户端不应提交带 buffer 的最终状态；而且 Wayland 上窗口初始尺寸可能由合成器决定（如 maximized 或 layer surface 的锚定尺寸）。`acknowledged_first_configure` 标志（[window.rs:L948-L953](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L948-L953)）就是这条协议要求的状态化。

**练习 2**：`handle_fractional_scale_event` 里 `scale as f32 / 120.0`（[window.rs:L987-L991](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L987-L991)）的 120 是什么？

答案：fractional-scale-v1 协议用 120 为分母的定点数表达分数缩放（120 = 100%，180 = 150%，240 = 200%），避免浮点协商；除以 120 还原成倍率后交给 `rescale`。它配合 viewporter 使用：buffer 保持整数倍率，用 viewport 做分数缩放变换。

**练习 3**：`window_decorations()` 返回的 `Decorations::Client { tiling }` 为什么要带 tiling 信息？

答案：见 [window.rs:L1850-L1858](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L1850-L1858)。窗口被平铺（贴边半屏）时，合成器不会再画任何边框，相邻窗口直接相接；上层需要知道哪些边已被平铺，才能只在与自由边相邻处绘制阴影/圆角等 CSD 效果（`inset_by_tiling` 辅助函数即为此服务）。

### 4.4 wayland::popup：锚定弹出层与 positioner

#### 4.4.1 概念说明

「在鼠标旁边弹一个菜单」在无全局坐标世界里怎么做？答案是把问题反过来陈述：**不说弹在哪里，而是描述锚定关系**，让合成器解算。这就是 xdg-shell 的 popup 子协议：客户端构造一个一次性的 `xdg_positioner`，填入锚矩形（父窗口坐标系里的参考矩形）、锚点（anchor：矩形九个位置之一）、重力（gravity：弹出层朝哪个方向生长）、约束调整（constraint_adjustment：出屏时允许滑动/翻转/收缩）与偏移，然后用 `xdg_surface.get_popup(parent, positioner)` 创建弹出层。合成器综合屏幕布局后给出最终位置。

平台无关的数据模型在 gpui 主 crate 的 [../gpui/src/platform/popup.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui/src/platform/popup.rs)：`PopupOptions`（[L15-L53](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui/src/platform/popup.rs#L15-L53)）的文档注释写得非常清楚——`anchor_rect` 是父窗口坐标系的矩形（下拉菜单就是那个按钮的 bounds）、`grab` 为 true 时弹层表现为菜单（接管键盘、点外部即收）。`grab` 的注释还点出关键时序：**grab 必须在触发它的按下事件还活跃时请求**，所以要从 mouse-down 处理器开窗，而不是 click 处理器。另一条重要边界写在文件末尾的 `PopupNotSupportedError`（[L127-L134](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui/src/platform/popup.rs#L127-L134)）：原生 popup 与 gpui 窗口内绘制的 popover 是两回事，平台不支持原生 popup 时调用方应回退到窗口内渲染（u3-l1 提过 macOS 正是显式报此错）。目前仓库中 `WindowKind::AnchoredPopup` 的构造点只在契约与平台实现层面，gpui 自身的菜单仍是窗口内 popover 渲染——原生 popup 是为需要系统级弹出表面的调用方预留的能力，Wayland 是实现最完整的一侧。

#### 4.4.2 核心流程

```text
App 层: WindowOptions { kind: WindowKind::AnchoredPopup(PopupOptions { parent, anchor_rect, anchor, gravity, constraint_adjustment, offset, grab }) }
    ▼
LinuxClient::open_window（client.rs L989-L1012）
  ├── 在 windows 表里按 options.parent 找父窗口（找不到 → Err）
  ├── grab 时取 max(MousePress, KeyPress) serial（为 0 则放弃 grab）
  ▼
WaylandSurfaceState::new 的 Popup 分支（window.rs L197-L243）
  ├── build_popup_positioner：anchor_rect 从「gpui 窗口坐标」平移到
  │   「父窗口 geometry 坐标」，并夹紧到 geometry 内至少 1px（协议禁零尺寸/越界）
  ├── wm_base.get_xdg_surface → xdg_surface
  ├── 父是 layer surface ? get_popup(None) + layer_surface.get_popup(xdg_popup)
  │                    : get_popup(parent.xdg_surface, positioner)
  ├── positioner.destroy()（一次性对象即弃）
  ├── grab ? xdg_popup.grab(seat, serial)
  └── parent.add_child(surface_id, /* blocks= */ false)   ← 弹层不阻塞父窗口输入
    ▼
合成器解算位置 → xdg_popup.configure + xdg_surface.configure
    ▼
handle_popup_event → 复用 xdg_surface.configure 协商（尺寸记入 in_progress_configure）
    ▼
运行期父/弹层尺寸变化 → reposition_popup：重建 positioner + xdg_popup.reposition(token)
```

#### 4.4.3 源码精读

**(1) grab serial 的组装**

```rust
// A popup grab must reference a press event or the compositor declines it and
// immediately dismisses the popup, so use the most recent press serial, or no
// grab before any press.
let popup_grab = options.grab.then(|| {
    let serial = state
        .serial_tracker
        .get(SerialKind::MousePress)
        .as_raw()
        .max(state.serial_tracker.get(SerialKind::KeyPress).as_raw());
    (serial != 0).then(|| (serial, state.wl_seat.clone()))
});
```

见 [client.rs:L998-L1009](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L998-L1009)。这是 4.2 提到的第三个消费者：抓取凭证取「最近一次鼠标按下与键盘按下中较大者」，还没收到任何按下事件（serial 为 0）就干脆不 grab。

**(2) positioner 的坐标换算**

```rust
// The protocol wants the anchor rect relative to the parent's window geometry, while
// `options.anchor_rect` is in gpui window coordinates (surface-local). A rect extending
// outside the geometry or with a zero size is a protocol error, so translate, then clamp
// to at least one pixel inside the geometry, pulling the origin inward at the edges.
let anchor_rect = Bounds {
    origin: options.anchor_rect.origin - parent_geometry.origin,
    size: options.anchor_rect.size,
};
```

见 `build_popup_positioner`（[window.rs:L314-L363](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L314-L363)）的开头。两个坐标系的差（gpui 的 surface 局部坐标 vs 协议的父窗口 geometry 坐标）与协议的两条禁区（零尺寸、越界）都在这一段里化解；锚点/重力/约束的枚举翻译来自 [popup.rs:L5-L38](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/popup.rs#L5-L38) 的三个纯函数（`wayland_anchor`/`wayland_gravity`/`wayland_constraint_adjustment`，后者按注释直接按位映射）。

**(3) 两种父对象与再锚定**

popup 的父可以是普通 xdg surface，也可以是 layer surface——后者的挂接方式不同：

```rust
// A layer-shell parent takes a null xdg parent and is attached via the layer
// surface. Every other surface kind has an xdg_surface to parent to directly.
let xdg_popup = if let Some(parent_layer_surface) = parent.layer_surface() {
    let xdg_popup = xdg_surface.get_popup(None, &positioner, &globals.qh, surface.id());
    parent_layer_surface.get_popup(&xdg_popup);
    xdg_popup
} else {
    xdg_surface.get_popup(parent.xdg_surface().as_ref(), &positioner, &globals.qh, surface.id())
};
```

见 [window.rs:L213-L226](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L213-L226)。父窗口登记子表面时用 `add_child(surface.id(), false)`（[L235](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L235)）——注释说明弹层**不阻塞**父窗口输入（对比 Dialog 的 `true`：对话框模态阻塞），这样点击父窗口自身还能用于「点外面收起菜单」。父或弹层尺寸变化后的再锚定走 `reposition_popup`（[L433-L456](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L433-L456)）：重建 positioner、携带自增 token 调 `xdg_popup.reposition`；注释强调对未映射（首次 configure 前）的弹层 reposition 是协议错误，所以 `WaylandPopupSurfaceState` 里保存 `options` 与 `next_reposition_token`（[L306-L312](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L306-L312)）正是为此。合成器回应 `xdg_popup.configure` 时，`handle_popup_event`（[L1142-L1159](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L1142-L1159)）只取尺寸——注释点明「位置是合成器的」，随后借道 `xdg_surface.configure` 走 4.3 的同一套协商。

#### 4.4.4 代码实践

**实践目标**：把 AnchoredPopup 的完整调用链走一遍，并理解 grab 时序约束。

**操作步骤**：

1. 阅读型跟踪：从 [client.rs:L989-L1012](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L989-L1012) 出发，依次抄下经过的每个函数名与协议请求，画出时序图。
2. （可选，示例代码）写一个最小验证程序：主窗口里放一个按钮，在按钮的 **mouse-down** 处理器（不是 click）里 `cx.open_window` 一个 `WindowKind::AnchoredPopup` 窗口，`PopupOptions` 里 `anchor_rect` 填按钮 bounds、`anchor: PopupAnchor::BottomLeft`、`gravity: PopupGravity::BottomRight`、`grab: true`：

```rust
// 示例代码：仅示意 AnchoredPopup 的最小用法，未在本讲环境中运行
cx.open_window(
    WindowOptions {
        window_bounds: Some(WindowBounds::Windowed(Bounds {
            origin: point(px(0.), px(0.)),
            size: size(px(160.), px(120.)),
        })),
        kind: WindowKind::AnchoredPopup(PopupOptions {
            parent: window.window_handle().downcast::<MyView>().unwrap(),
            anchor_rect: button_bounds,
            anchor: PopupAnchor::BottomLeft,
            gravity: PopupGravity::BottomRight,
            constraint_adjustment: PopupConstraintAdjustment::SLIDE_Y | PopupConstraintAdjustment::FLIP_Y,
            offset: point(px(0.), px(0.)),
            grab: true,
        }),
        ..Default::default()
    },
    |_, cx| cx.new(|_| PopupView),
)
```

3. 把 `grab` 换 `false` 再跑一次，对比点弹层外部时的收起行为。

**需要观察的现象**：grab 版本点窗口其他区域时父窗口仍收到点击（`blocks=false` 的效果），由你的应用负责收起弹层；点其他**应用**时合成器直接收起弹层。

**预期结果**：锚定与翻转行为由合成器解算，属协议确定行为；具体交互手感依合成器而异，运行结果「待本地验证」。示例代码中 parent 句柄的获取方式（`window_handle().downcast`）依你的视图类型调整，若类型不符需改为保存好的 `AnyWindowHandle`。

#### 4.5 4.4.5 小练习与答案 → 见下（纠正：本节为 4.4.5）

#### 4.4.5 小练习与答案

**练习 1**：为什么 anchor_rect 必须从 gpui 窗口坐标平移到「父窗口 geometry」坐标？

答案：xdg_positioner 协议规定锚矩形相对父的 **window geometry**（`set_window_geometry` 申报的、含 CSD 的可见区域），而 gpui 上层给的 `anchor_rect` 在窗口内容坐标系（surface 局部）。Wayland 上开 CSD 时两者差一个 inset，不平移弹层会整体偏移标题栏高度；X11 等有全局坐标的平台则由各自的平台实现自行换算。

**练习 2**：`PopupConstraintAdjustment` 的 SLIDE 与 FLIP 有什么区别？

答案：见 [../gpui/src/platform/popup.rs:L106-L125](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui/src/platform/popup.rs#L106-L125) 的位定义：SLIDE 是把弹层沿该轴**平移**进屏（锚点关系不变，菜单整体挪进来）；FLIP 是把 anchor 与 gravity **翻转**到参考矩形另一侧（下拉变上拉）。典型菜单两者都开：先试翻转，翻不下再滑。RESIZE 则允许压缩弹层尺寸。

**练习 3**：若把 `xdg_popup.grab` 的 serial 换成 `SerialKind::MouseEnter` 的，会发生什么？

答案：enter 事件不是按下事件，合成器会拒绝 grab 并立即收起弹层——这正是 [client.rs:L998-L1000](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L998-L1000) 注释所写的拒绝条件，也是 4.2 练习 1「按类别取 serial」的反面教材。

### 4.5 wayland::layer_shell：钉在屏幕边缘的表面

#### 4.5.1 概念说明

`zwlr_layer_shell_v1` 出自 wlroots 生态的 wlr-protocols（GNOME 需装对应扩展支持），专门服务 dock、状态栏、通知、壁纸、屏幕键盘这类「不是普通窗口」的表面。它回答的问题是：**无全局坐标世界里如何占住屏幕边缘**——客户端声明四件事，位置由合成器解算：

- **层（Layer）**：`Background < Bottom < Top < Overlay`，层间严格压盖，同层内顺序不定（[../gpui/src/platform/layer_shell.rs:L8-L22](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui/src/platform/layer_shell.rs#L8-L22)，默认 Overlay）。
- **锚（Anchor）**：位掩码 `TOP|BOTTOM|LEFT|RIGHT`，可组合——`LEFT | RIGHT | BOTTOM` 就是「贴住底边并横向拉满」，这正是 dock 的形态（[L24-L39](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui/src/platform/layer_shell.rs#L24-L39)）。
- **独占区（exclusive zone / edge）**：告诉合成器「请别让普通窗口盖住我占的这条带」，任务栏所以能常驻；`exclusive_edge` 指明独占哪条边（[L67-L71](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui/src/platform/layer_shell.rs#L67-L71)）。
- **键盘交互性（KeyboardInteractivity）****：`None`（收不到键盘）/`Exclusive`（独占键盘，适合输入法候选窗）/`OnDemand`（像普通窗口一样可聚焦，[L41-L55](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui/src/platform/layer_shell.rs#L41-L55)）。

外加 `namespace`（合成器据它应用规则，创建后不可改）与 `margin`（CSS 顺序四边距）。这些全部装进 `LayerShellOptions`（[L57-L77](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui/src/platform/layer_shell.rs#L57-L77)）。要分清两种「自绘 chrome」：普通窗口的 Z 风格自绘标题栏是 **CSD**（4.3：xdg_toplevel + `client_inset` + `set_window_geometry`，把标题栏画进自己的 surface）；layer_shell 则是把整个表面升级为面板级表面，适合 dock/通知/overlay。两者都能让「界面完全由应用绘制」，但管辖的是不同种类的窗口。协议不存在时（`Globals.layer_shell` 为 `None`）创建即失败，错误类型 `LayerShellNotSupportedError`（[L79-L83](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui/src/platform/layer_shell.rs#L79-L83)）。

#### 4.5.2 核心流程

```text
App 层: WindowKind::LayerShell(LayerShellOptions)
    ▼
open_window → target_output（按 display_id 选 wl_output；None = 当前输出）
    ▼
WaylandSurfaceState::new 的 LayerShell 分支（window.rs L150-L195）
  ├── globals.layer_shell 为 None → Err(LayerShellNotSupportedError)
  ├── layer_shell.get_layer_surface(surface, output, layer, namespace, qh, surface.id)
  ├── set_size（请求尺寸；锚定拉满的维度上会被合成器的 configure 覆盖）
  ├── set_anchor(位掩码)
  ├── set_keyboard_interactivity
  ├── set_margin（可选）
  ├── set_exclusive_zone / set_exclusive_edge（可选）
  └── surface.commit
    ▼
zwlr_layer_surface.configure(width, height, serial)
    ▼
handle_layersurface_event（window.rs L1105-L1139）
  └── 组装 in_progress_configure 后「照 xdg_surface 的老路走」：
      复用 handle_xdg_surface_event 完成 ack + set_size + 首帧
```

运行期还可以经 `PlatformWindow::set_exclusive_zone` / `set_exclusive_edge`（[window.rs:L1797-L1816](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L1797-L1816)）动态调整并立即 commit 生效。

#### 4.5.3 源码精读

**(1) 枚举映射层**

[wayland/layer_shell.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/layer_shell.rs) 全文不足 30 行：第一行 `pub use gpui::layer_shell::*;` 把平台无关模型原样再导出，随后三个纯函数把 `Layer`、`Anchor`（按位直转）、`KeyboardInteractivity` 映射到协议枚举（[L5-L26](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/layer_shell.rs#L5-L26)）。模块声明的注释（[wayland.rs:L9-L10](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland.rs#L9-L10)）说明它是 `pub mod`——因为 `LayerShellOptions` 等类型要暴露给上层 gpui 使用，而 client/window 等子模块是私有 `mod`。

**(2) 创建分支与独占边校验**

```rust
if let WindowKind::LayerShell(options) = &params.kind {
    let Some(layer_shell) = globals.layer_shell.as_ref() else {
        return Err(LayerShellNotSupportedError.into());
    };

    let layer_surface = layer_shell.get_layer_surface(
        &surface,
        target_output.as_ref(),
        super::layer_shell::wayland_layer(options.layer),
        options.namespace.clone(),
        &globals.qh,
        surface.id(),
    );

    let width = f32::from(params.bounds.size.width);
    let height = f32::from(params.bounds.size.height);
    layer_surface.set_size(width as u32, height as u32);

    layer_surface.set_anchor(super::layer_shell::wayland_anchor(options.anchor));
    layer_surface.set_keyboard_interactivity(
        super::layer_shell::wayland_keyboard_interactivity(options.keyboard_interactivity),
    );
    ...
```

见 [window.rs:L150-L181](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L150-L181)（margin/exclusive_zone/exclusive_edge 的传递在同一分支续至 L195）。独占边有一条协议级校验：

```rust
/// An exclusive edge must be a single edge that the surface is anchored to,
/// otherwise the compositor raises a fatal `invalid_exclusive_edge` protocol
/// error. An invalid edge is logged and ignored. Returns whether it applied.
fn apply_exclusive_edge(...) -> bool {
    if edge.bits().count_ones() == 1 && anchor.contains(edge) {
        layer_surface.set_exclusive_edge(super::layer_shell::wayland_anchor(edge));
        true
    } else {
        log::warn!("ignoring exclusive edge {edge:?}: must be a single edge of the surface anchor {anchor:?}");
        false
    }
}
```

见 [window.rs:L469-L486](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L469-L486)——用「单个位 + 被锚包含」两道检查把致命协议错误拦成一条警告，这是防御式翻译的好样本。

**(3) configure 复用与事件路由**

```rust
zwlr_layer_surface_v1::Event::Configure { width, height, serial } => {
    ...
    state.in_progress_configure = Some(InProgressConfigure { size, fullscreen: false, maximized: false, resizing: false, tiling: Tiling::default() });
    drop(state);

    // just do the same thing we'd do as an xdg_surface
    self.handle_xdg_surface_event(xdg_surface::Event::Configure { serial });

    false
}
zwlr_layer_surface_v1::Event::Closed => {
    // unlike xdg, we don't have a choice here: the surface is closing.
    true
}
```

见 [window.rs:L1105-L1139](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L1105-L1139)。layer surface 的 configure 语义比 xdg 简单（没有最大化/平铺），所以填好 `InProgressConfigure` 后直接借道 4.3 的协商管线；`Closed` 注释则点出与 xdg `close` 的差异——xdg 的关闭请求可以无视，layer surface 的 closed 是合成器的既成事实。事件从协议到窗口的路由在 [client.rs:L1522-L1545](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L1522-L1545)（以 `surface.id()` 作 user_data 找回窗口，返回 true 则调 `window.close()`）。

**(4) 官方示例**

[../gpui/examples/layer_shell.rs:L76-L100](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui/examples/layer_shell.rs#L76-L100)：一块 500×200 的透明面板，`Anchor::LEFT | Anchor::RIGHT | BOTTOM` 横向拉满贴底、底边距 40px、`KeyboardInteractivity::None`、每 500ms 重绘的时钟。这是 layer_shell 用法最紧凑的范本。

#### 4.5.4 代码实践

**实践目标**：跑通一个 layer_shell 表面，观察合成器如何解算锚定。

**操作步骤**：

1. 在 Wayland 会话运行 `WAYLAND_DEBUG=1 cargo run -p gpui --example layer_shell 2> layer.log`。
2. 观察屏幕底部出现的时钟面板：横向是否拉满？底边是否悬空 40px（margin 的效果）？普通窗口最大化时是否避开它（没设 exclusive_zone，理论上会被盖住）。
3. 在 `layer.log` 中找 `zwlr_layer_shell_v1.get_layer_surface`、`set_anchor`、`set_margin`、`set_keyboard_interactivity`、`zwlr_layer_surface_v1.configure`、`ack_configure`。
4. 修改实验：把示例中 `anchor` 改为 `Anchor::TOP`、`margin` 去掉、`Layer` 保持默认（Overlay），重新运行观察位置变化；再给 `LayerShellOptions` 加 `exclusive_zone: Some(px(40.))`，观察最大化窗口是否让出这条带。

**需要观察的现象**：锚定与拉满由合成器执行；configure 回报的 width 可能不等于请求的 500（因为左右锚定拉满时宽度由合成器决定）。

**预期结果**：支持 wlr-layer-shell 的合成器（sway、KDE 等）上面板如期出现；GNOME 无扩展支持时 `Globals.layer_shell` 为 `None`，`open_window` 返回 `LayerShellNotSupportedError`，示例 `unwrap` 直接报错退出——这本身就是 4.5.1 所述失败路径的验证。具体表现「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：为什么示例把 `keyboard_interactivity` 设为 `None` 而不是 `OnDemand`？

答案：时钟面板不接收输入，`None` 让合成器把键盘留给真正聚焦的窗口；`Exclusive` 会从当前窗口抢走键盘（适合输入法面板），`OnDemand` 则允许点击聚焦。对一个纯展示型 overlay，`None` 是唯一不打扰用户的选择。

**练习 2**：`set_exclusive_zone(-1)` 与 `0` 与正数各是什么语义？

答案：按 layer-shell 协议：正数 = 独占相应像素宽的带；`0` = 不独占但把自己锚定的边让出给其他 layer surface（跟随默认）；`-1` = 完全不改动独占区。gpui 侧 `set_exclusive_zone(zone: Pixels)` 传像素值（[window.rs:L1797-L1807](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L1797-L1807)），`LayerShellOptions::exclusive_zone` 未设时创建分支不会发这个请求，由合成器按默认规则处理。

**练习 3**：layer_shell 弹出的 popup（4.4 的 layer 父分支）与普通 popup 有什么不同？

答案：父是 layer surface 时没有可用的 xdg 父对象，`get_popup` 的 xdg_parent 参数传 `None`，改用 `layer_surface.get_popup(&xdg_popup)` 挂接（[window.rs:L213-L226](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L213-L226)）。这是输入法面板（layer surface）上弹候选窗的典型结构。

## 5. 综合实践

**任务**：在 Wayland 会话下运行 GPUI 示例，对照源码回答「窗口创建、光标切换、剪贴板写入三个操作各自经过哪些 Wayland 协议对象」，输出一份调用路径笔记。

**步骤**：

1. **准备**：确认 `echo $WAYLAND_DISPLAY` 非空；准备两个终端日志文件。
2. **窗口创建与光标切换**：`WAYLAND_DEBUG=1 cargo run -p gpui --example window 2> window.log`。
   - 操作 A（创建）：点击 "Normal" 按钮开一个子窗口。
   - 操作 B（光标）：把鼠标悬停到任意按钮上（按钮有 `.cursor_pointer()`，会触发 u3-l4 讲过的帧末 `set_cursor_style`）。
3. **剪贴板写入**：`WAYLAND_DEBUG=1 cargo run -p gpui --example input 2> input.log`，在输入框里打几个字、选中一段、按 Ctrl+C（[input.rs:L145-L160](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui/examples/input.rs#L145-L160) 是剪贴板动作的入口）。
4. **整理笔记**：对三个操作各写一行「协议对象序列 → 源码位置」，参考答案骨架（以源码为准核对日志）：

| 操作 | 协议对象序列（预期骨架） | 关键源码位置 |
| --- | --- | --- |
| 创建窗口 | `wl_compositor.create_surface` → `xdg_wm_base.get_xdg_surface` → `get_toplevel`（+ `set_title`/`set_app_id`/`set_max_size`，dialog 再加 `xdg_wm_dialog_v1`）→ （可选 `zxdg_decoration_manager_v1.get_toplevel_decoration`）→ `commit` → `configure` → `ack_configure` → `set_window_geometry` → `frame`/`attach`/`commit` | client.rs `open_window` [L982-L1045](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L982-L1045)；window.rs `WaylandSurfaceState::new` [L245-L291](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L245-L291) |
| 切换光标 | `wp_cursor_shape_manager_v1.get_cursor_shape_device`（一次性）→ 每次 `wp_cursor_shape_device_v1.set_shape(serial)`；无该协议时后备为 `wl_pointer.set_cursor(serial, surface, hot_x, hot_y)` + 光标 surface 的 `attach`/`commit` | client.rs `set_cursor_style` [L1047-L1085](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L1047-L1085)；cursor.rs `set_icon` [L94-L151](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/cursor.rs#L94-L151) |
| 写剪贴板 | `wl_data_device_manager.create_data_source` → 多条 `offer`（各文本 MIME + `pid/<n>`）→ `wl_data_device.set_selection(serial)`；之后若他进程来取：`wl_data_source.send`（fd） | client.rs `write_to_clipboard` [L1177-L1201](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L1177-L1201)；clipboard.rs `send` [L183-L197](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/clipboard.rs#L183-L197) |

5. **核对**：逐条在日志里找到对应请求；找不到的（如 cursor_shape_device）判断是合成器缺协议走了后备路径，还是时序未触发。

**预期结果**：三行骨架是源码推导的确定路径；日志中的具体协议集合、serial 数值、MIME 列表依赖本机合成器与剪贴板管理器，「待本地验证」。把笔记存档，它就是你这台机器上 GPUI Wayland 后端的「协议对象速查表」。

## 6. 本讲小结

- **WaylandClient** 以 `Rc<RefCell<WaylandClientState>>` 为体、`WaylandClientStatePtr(Weak)` 为分发代理；初始化流水线依次完成连接、`wl_registry` global 绑定（`Globals` 区分必需/可选协议）、`LinuxCommon` 调度接线（前台任务经 `insert_idle` 让位输入事件）、DMABUF GPU 探测与 `WaylandSource` 注册，`run()` 阻塞在自有的 calloop 主循环上。
- **无全局坐标**是理解一切接口变形的钥匙：显示器几何只能被动累积（`InProgressOutput` 到 `Done` 才完整）、`primary_display`/`window_stack` 返回 `None`、窗口不能设位置只能协商（`_move(seat, serial)`）、layer surface 连 `set_window_geometry` 都不可用。
- **serial.rs** 用五种 `SerialKind` 分别跟踪最后一次输入序号：改光标用 `MouseEnter`、拖动/抓取用 `MousePress`、剪贴板所有权用只认按下事件的 `selection_serial`（按到达序处理回绕，测试固化）。
- **wayland/window.rs** 是「一个 `wl_surface`、三种角色」：`WindowKind` 决定 `Xdg`/`LayerShell`/`Popup` 三态；configure/ack 协商是提交新 buffer 的前置条件，首帧等在 `acknowledged_first_configure` 上；CSD 默认开启，`client_inset` + `set_window_geometry` 申报自绘 chrome，析构严格按协议顺序。
- **popup** 用 positioner「描述而非指定」位置（anchor/gravity/constraint 三元组 + grab serial），坐标要平移进父窗口 geometry 并防零尺寸/越界协议错误；父为 layer surface 时改走 `layer_surface.get_popup` 挂接。
- **layer_shell** 把表面钉在屏幕边缘：层、锚位掩码、独占区、键盘交互性四件套由合成器解算位置，协议缺失时报 `LayerShellNotSupportedError`；它服务 dock/面板/overlay，与普通窗口的 CSD 标题栏是两种不同层面的「自绘 chrome」。

## 7. 下一步学习建议

- **下一讲 u5-l5** 讲 xdg-desktop-portal：本讲 4.1 里 `XDPEventSource` 处理的外观、按钮布局、光标主题/尺寸事件，以及 `window_identifier()`（[client.rs:L1227-L1239](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L1227-L1239)，从 `wl_surface` 生成 portal 窗口标识符）都将在那里闭环。
- **横向对照**：带着本讲的三个问题重读 u5-l3 的 X11 侧——X11 怎么定位窗口（全局坐标）、怎么设光标（XCursor，无需 serial）、剪贴板为何要常驻服务线程（没有合成器递 fd）；再预习 u6-l1/u6-l2 看 macOS/Windows 如何用系统服务消解同样的问题。
- **纵向深入渲染**：`WaylandWindow` 里 `WgpuRenderer`、`wp_viewport` 与 fractional-scale 的配合（4.3 练习 2）是 u8-l2「PlatformAtlas 与渲染后端」的入口，可提前阅读 [../gpui_linux/src/linux/wayland/window.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs) 中 `rescale` 与 `handle_surface_event` 对 `preferred_buffer_scale`/`preferred_buffer_transform` 的处理。
- **动手验证**：完成第 5 节综合实践后，可尝试给 layer_shell 示例加上 `exclusive_zone` 并观察普通窗口的避让，或把 4.4 的 popup 示例接到 layer 面板上（练习 3 的结构），检验自己对三种角色组合关系的理解。

# Wayland 客户端：协议对象、layer_shell 与按需驱动渲染循环

## 1. 本讲目标

本讲是 Linux 三后端之旅的最后一站。读完本讲，你应该能够：

1. 说出 `WaylandClient` 的整体结构：它如何管理协议对象（`Globals`）、如何把 calloop 事件循环复用为主循环、以及 `LinuxClient` 契约在它身上如何落地。
2. 理解 Wayland「无全局坐标」模型对窗口定位接口的影响：为什么 popup 要用 positioner 锚定父表面、为什么 layer_shell 要锚定屏幕边缘。
3. 说明 `serial` 模块为何要专门跟踪「最后一次输入序列号」，以及剪贴板所有权、popup grab、光标切换分别消费哪一类 serial。
4. 描述 layer_shell 如何支撑 Z 风格的自绘标题栏与面板。
5. **重点**：解释 eb354c8d50 重写后的按需驱动渲染循环——`FrameLoop` 八态状态机如何取代旧的「持续心跳」模型，窗口空闲时如何停泊（Parked）在零 CPU 状态，`schedule_frame` 又如何通过 calloop `frame_ping` 唤醒它。

## 2. 前置知识

### 2.1 Wayland 一页纸

- **显示服务器模型**：Wayland 客户端不直接向屏幕绘制，而是把自己的像素放进 `wl_buffer`，挂到 `wl_surface` 上，用 `commit` 请求提交；合成器（compositor，如 mutter、KWin、sway）决定这块表面何时、何地、以多大比例上屏。
- **协议对象与 request/event**：Wayland 是一套对象协议。客户端创建对象（如 `xdg_surface`）、向对象发 request（如 `ack_configure`）；合成器向对象发 event（如 `configure`）。Rust 侧由 `wayland-client` crate 的生成代码表示这些对象。
- **serial**：几乎每个输入事件都携带一个单调递增的 u32 序列号。凡是「证明发生过用户交互」的请求（抓取 popup、声明剪贴板所有权、移动窗口）都要回传一个 serial，合成器会校验它确实对应某个真实事件，防止程序凭空抢焦点。
- **frame callback**：客户端对 `wl_surface` 调用 `frame` 请求后，合成器会在该表面下一次提交真正上屏时回调它。这是 Wayland 的垂直同步节拍器，也是本讲渲染循环的核心。
- **xdg_shell**：普通窗口的标准协议（`xdg_surface` + `xdg_toplevel` + `xdg_popup`），负责窗口装饰协商、最大化和弹出层定位。
- **zwlr_layer_shell_v1**：wlroots 系扩展协议，允许表面挂到背景/底部/顶部/覆盖四个层级并锚定屏幕边缘——面板、Dock、通知区都靠它。

### 2.2 承接前几讲的认知

- u5-l1 讲过 `LinuxPlatform` 是外壳、`LinuxClient` 是契约、Wayland/X11/headless 是三个可替换后端；本讲进入 Wayland 后端内部。
- u4-l3 讲过 calloop 的 `ping`、`Timer`、`insert_idle` 原语；本讲的 `frame_ping` 与帧重试定时器就是它们的直接应用。
- u3-l2 讲过 `PlatformWindow::schedule_frame` 契约（原 `completed_frame`，eb354c8d50 更名并改语义）；本讲看它唯一的真实平台实现。

## 3. 本讲源码地图

| 文件 | 行数规模 | 作用 |
| --- | --- | --- |
| `../gpui_linux/src/linux/wayland/client.rs` | 最大 | `WaylandClientState`、`Globals` 协议对象集合、事件循环组装、`LinuxClient` 实现、各协议对象的 `Dispatch` 实现 |
| `../gpui_linux/src/linux/wayland/window.rs` | 大 | `WaylandWindowState`、三种表面状态、`FrameLoop`/`PresentationState` 状态机、`PlatformWindow` 实现 |
| `../gpui_linux/src/linux/wayland/serial.rs` | 小 | `SerialKind`/`Serial`/`SerialTracker` 序列号跟踪 |
| `../gpui_linux/src/linux/wayland/layer_shell.rs` | 极小 | 平台无关 layer_shell 枚举到 wlr 协议枚举的映射 |
| `../gpui_linux/src/linux/wayland/popup.rs` | 极小 | popup 的 anchor/gravity/constraint 位标志到 xdg_positioner 的映射 |
| `../gpui_linux/src/linux/wayland/display.rs` | 小 | `WaylandDisplay`：显示器上报 |
| `../gpui/src/platform/layer_shell.rs`（gpui 主 crate） | 小 | 平台无关的 `Layer`/`Anchor`/`LayerShellOptions` 模型 |
| `../gpui/src/platform.rs`（gpui 主 crate） | 契约 | `PlatformWindow::schedule_frame` 默认空实现 |
| `../gpui/src/window.rs`（gpui 主 crate） | 帧调度 | GPUI 侧调用 `schedule_frame` 的四个位置与三个新测试 |

## 4. 核心概念与源码讲解

### 4.1 WaylandClient 与 Globals：连接、协议对象与事件循环

#### 4.1.1 概念说明

`WaylandClient` 是 `LinuxClient` 契约（见 u5-l1）的 Wayland 实现。它的全部可变状态收在 `Rc<RefCell<WaylandClientState>>` 里，而 `WaylandClientStatePtr`（内部是 `Weak`）是传给 `wayland-client` 派发系统的句柄——源码注释说明了原因：孤儿规则（orphan rules）要求 `Dispatch` 实现里的状态类型是本地类型，但又必须能把窗口交还给 GPUI。

`Globals` 结构体集中持有一次连接里绑定到的全部协议管理器对象。它体现了一个重要工程决策：**哪些协议是必需的、哪些尽力而为**——`compositor`、`shm`、`seat`、`wm_base` 用 `unwrap()`（缺了直接 panic，这些是 Wayland 桌面的底线）；layer_shell、cursor_shape、viewporter、decoration、blur 等全部用 `.ok()` 绑定成 `Option`，运行期逐项降级。

#### 4.1.2 核心流程

初始化流程（`WaylandClientState::new` 内）：

```text
连接 wl_display → 收集 registry 全局对象
→ 绑定 wl_seat / wl_output
→ 创建 calloop EventLoop（主循环）
→ LinuxCommon::new(LoopSignal)          ← 承接 u5-l1 的公共状态
→ 注册 main_receiver（前台 runnable → insert_idle）
→ 注册 wake_receiver（系统唤醒）
→ 创建 frame_ping（calloop::ping::make_ping）并注册
→ Globals::new(...)                      ← 绑定全部协议管理器
→ data_device / primary_selection / cursor 初始化
→ 注册 XDPEventSource（外观/按钮布局变化）
```

运行期，一个 GPUI 前台任务或一次 `frame_ping` 到达时的路径：

```text
calloop 事件循环唤醒
  ├─ main_receiver：Msg(runnable) → insert_idle → runnable.run()（GPUI 逻辑）
  ├─ frame_ping   → client.dispatch_scheduled_frames() → 逐窗口 scheduled_frame_fired()
  ├─ Timer(重试)  → window.retry_timer_fired()
  └─ Wayland socket 可读 → 协议 event 派发到各 Dispatch 实现
```

#### 4.1.3 源码精读

**`WaylandClientState` 的字段全景**（[../gpui_linux/src/linux/wayland/client.rs:L313-L369](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L313-L369)）：串口跟踪器、globals、GPU 上下文、seat/pointer/keyboard、surface 到窗口的映射表 `windows: HashMap<ObjectId, WaylandWindowStatePtr>`、输出设备表、xkb 键盘状态、拖拽/点击/按键重复状态、calloop `loop_handle`、剪贴板、光标、`LinuxCommon`。注意 `windows` 以 `ObjectId`（wl_surface 的对象 id）为键——这是 Wayland 事件路由回窗口的索引。

**`Globals` 结构体**（[../gpui_linux/src/linux/wayland/client.rs:L207-L231](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L207-L231)）：除协议管理器外还有两个非协议字段——`executor: ForegroundExecutor` 与 `frame_ping: Ping`。后者是**全客户端唯一**的帧唤醒 ping，被 `clone` 进每个窗口（`Ping` 内部是 `Arc`）。

**`Globals::new` 的绑定策略**（[../gpui_linux/src/linux/wayland/client.rs:L233-L278](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L233-L278)）：对照每一行可以看到 `unwrap()` 与 `.ok()` 的分界：合成器没有 layer_shell？没关系，Zed 的面板功能降级；没有 `wm_base`？整个桌面模型不成立，直接失败。

**frame_ping 的创建与注册**（[../gpui_linux/src/linux/wayland/client.rs:L824-L830](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L824-L830)）：

```rust
let (frame_ping, frame_ping_source) =
    calloop::ping::make_ping().expect("Failed to create the frame ping");
handle
    .insert_source(frame_ping_source, |_, _, client| {
        client.dispatch_scheduled_frames();
    })
    .unwrap();
```

ping 一旦触发，就广播给所有窗口（详见 4.4）。紧接着 `Globals::new` 把这个 ping 存进 `Globals`（[../gpui_linux/src/linux/wayland/client.rs:L833-L839](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L833-L839)）。

**`LinuxClient::open_window`**（[../gpui_linux/src/linux/wayland/client.rs:L1038-L1101](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L1038-L1101)）：三个看点。第一，popup 显式按 `options.parent` 找父窗口，其余窗口的父亲是当前键盘焦点窗口。第二，popup 的 grab serial 取 `MousePress` 与 `KeyPress` 两类中的较大者——注释言明：grab 必须引用一次真实的按下事件，否则合成器会当场拒绝并立刻关闭 popup。第三，窗口构造成功后 `state.windows.insert(surface_id, window.0.clone())` 登记进路由表。

**`run` 与 `compositor_name`**（[../gpui_linux/src/linux/wayland/client.rs:L1191-L1206](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L1191-L1206)）：`run` 把 `event_loop` 从状态里 take 出来（`expect("App is already running")` 保证只跑一次）后交给 `event_loop.run` 阻塞。`compositor_name` 返回 `"Wayland"`（[L1279-L1281](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L1279-L1281)），正是 u1-l4 提到的观测出口。

**显示器**（[../gpui_linux/src/linux/wayland/display.rs:L12-L42](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/display.rs#L12-L42)）：`WaylandDisplay` 的 `uuid` 用输出名生成 v5 UUID（承接 u2-l3 的「uuid 才是跨重启身份」）；而 `primary_display` 直接返回 `None`（[../gpui_linux/src/linux/wayland/client.rs:L1016-L1018](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L1016-L1018)）——Wayland 协议根本没有「主显示器」概念。

#### 4.1.4 代码实践

**实践：用 WAYLAND_DEBUG 观察 `Globals` 的绑定结果。**

1. 实践目标：把「协议对象」从抽象概念变成肉眼可见的 request/event 流。
2. 操作步骤（在 Linux + Wayland 会话下）：
   - 在 Zed 仓库根目录运行 `WAYLAND_DEBUG=1 cargo run -p gpui --example window 2>wayland.log`（`libwayland` 会把全部协议流量打到 stderr）；
   - 打开 `wayland.log`，开头几百行是 `wl_registry` 的 `global` 事件——逐行找出 `zwlr_layer_shell_v1`、`wp_cursor_shape_manager_v1`、`zxdg_decoration_manager_v1` 是否出现；
   - 对照 `Globals::new` 的绑定清单，确认你的合成器缺哪个协议。
3. 需要观察的现象：不同合成器（GNOME 的 mutter 不支持 layer_shell；KWin/sway 支持）日志里的 global 列表不同。
4. 预期结果：缺失的协议在代码里对应 `Globals` 的 `None` 字段，相关功能运行期降级。**待本地验证**（取决于你所用的合成器）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `WaylandClientStatePtr` 包的是 `Weak` 而不是 `Rc`？
答案：`Dispatch` 实现要求状态类型可传入 `EventLoop::run`；`Weak` 让事件循环持有状态指针却不延长 `WaylandClientState` 的生命周期，客户句柄与状态之间不形成强引用环（承接 u5-l1 的外壳-后端关系），取用时 `upgrade()` 失败即安静返回。

**练习 2**：如果合成器不支持 `zwlr_layer_shell_v1`，用 `WindowKind::LayerShell` 开窗会发生什么？
答案：`WaylandSurfaceState::new` 里 `globals.layer_shell.as_ref()` 为 `None`，返回 `LayerShellNotSupportedError`（见 4.6 源码），开窗失败由调用方处理。

**练习 3**：`frame_ping` 为什么只建一个、而不是每窗口一个？
答案：calloop 源是稀缺资源；ping 只负责「唤醒事件循环」这一个事实，具体哪些窗口需要 tick 由 `dispatch_scheduled_frames` 广播后各窗口按自身状态（`Scheduled`）自行裁决（见 4.4）。

### 4.2 wayland::window：WaylandWindowState 与三种表面

#### 4.2.1 概念说明

每个窗口的全部状态在 `WaylandWindowState`，而它的「表面身份」由 `WaylandSurfaceState` 枚举表达，对应三种协议形态：

1. `Xdg`——普通窗口（`xdg_surface` + `xdg_toplevel`），可选装饰协商与模态对话框；
2. `LayerShell`——wlroots 层表面，用于面板/Dock 类 UI；
3. `Popup`——锚定父表面的弹出层（`xdg_popup`）。

**无全局坐标模型**是贯穿本模块的背景：Wayland 客户端既查不到也设不了自己「在屏幕上的绝对位置」，窗口摆放完全归合成器管。于是：普通窗口的 `resize` 只改表面局部几何；popup 用 positioner 相对**父窗口几何**声明锚点；layer_shell 用 anchor 相对**屏幕边缘**声明锚点；`primary_display`、`window_stack` 干脆返回 `None`。

#### 4.2.2 核心流程

窗口的诞生与首帧：

```text
open_window
→ wl_compositor.create_surface
→ WaylandSurfaceState::new（按 WindowKind 三分支）
→ 申请 fractional_scale / viewport
→ WaylandWindow::new（frame_loop = Unconfigured，保存 frame_ping）
→ surface.commit()                 ← 把初始状态发给合成器，仅此而已
   ... 合成器异步回应 ...
→ xdg_surface.Configure 事件到达
→ ack_configure + set_geometry
→ 首次 Configure：frame()          ← 渲染循环由此启动（见 4.3）
→ 后续 Configure：request_redraw()
```

窗口的死亡（`Drop`）按依赖逆序销毁协议对象，任何顺序错误都是协议错误。

#### 4.2.3 源码精读

**`WaylandWindowState` 字段**（[../gpui_linux/src/linux/wayland/window.rs:L97-L134](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L97-L134)）：留意渲染循环相关的四个字段——`redraw_requested`（脏标记）、`presentation`（呈现状态，L126）、`pending_frame_callback`（在途的 wl_callback，L127）与 `parent`/`children`（父子表面关系，`children: FxHashMap<ObjectId, bool>` 的布尔值标记「是否阻塞父窗口输入」——对话框阻塞、popup 不阻塞）。

**三种表面的构造分支**（[../gpui_linux/src/linux/wayland/window.rs:L142-L293](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L142-L293)）：`WaylandSurfaceState::new` 按 `params.kind` 三分。LayerShell 分支（L151-196）在 4.6 精读；popup 分支（L198-244）在 4.6 与 4.5 精读；其余一切 `WindowKind` 都落到 `get_xdg_surface` + `get_toplevel`（L246-291），`Floating`/`Dialog` 会调用 `toplevel.set_parent` 建立父子关系。

**首帧的点火处**（[../gpui_linux/src/linux/wayland/window.rs:L1059-L1131](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L1059-L1131)）：`handle_xdg_surface_event` 处理 `Configure`——先 `ack_configure`、上报 `set_geometry`，然后是关键一行（L1125-1131）：

```rust
let initial_configure = self.frame_loop.get() == FrameLoop::Unconfigured;
if initial_configure {
    self.frame();
} else {
    self.request_redraw();
}
```

第一次 Configure 之前客户端不许提交缓冲区（协议规定），所以首帧要等这一刻才由 `frame()` 点燃；之后的每次 Configure 只标脏请求重绘。这就是实践任务里「首帧如何由 Unconfigured 进入 FrameLoop」的答案。

**无全局坐标的代码证据**（[../gpui_linux/src/linux/wayland/window.rs:L1679-L1681](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L1679-L1681)）：`resize` 的注释直言「On Wayland, window geometry is surface-local: resizing should not attempt to translate the window; the compositor controls placement」——resize 只改表面局部几何，从不移动窗口。popup 更特殊（L1662-1664 注释）：位置是合成器的裁决，resize 会重新跑一遍 positioner。

**`Drop` 的销毁顺序**（[../gpui_linux/src/linux/wayland/window.rs:L752-L801](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L752-L801)）：第一行先把 `frame_loop` 置为 `Parked`（让任何在途唤醒对这个已死窗口失效），随后按 renderer → blur → decoration → surface_state（toplevel/xdg_surface）→ viewport → wl_surface 的顺序销毁，注释逐条引用协议文档说明为何这个顺序不可乱。

#### 4.2.4 代码实践

**实践：跟踪一次窗口创建的全链路（源码阅读型）。**

1. 实践目标：验证 4.2.2 的流程图与真实代码一致。
2. 操作步骤：
   - 从 u3-l1 讲过的 `App::open_window` 出发，沿 `LinuxPlatform::open_window` → `WaylandClient::open_window`（L1038）→ `WaylandWindow::new`（L813）→ `WaylandSurfaceState::new`（L142）一路抄下函数名；
   - 在 [L860](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L860) 处确认初始状态是 `FrameLoop::Unconfigured`；
   - 继续追 `handle_xdg_surface_event`（L1059）确认首帧入口。
3. 需要观察的现象：`open_window` 返回时**没有任何像素提交**，只做了一次 `surface.commit()`（L865）。
4. 预期结果：你的调用链笔记应与 4.2.2 的流程图逐行对应；首帧确实发生在收到第一个 `Configure` 之后。

#### 4.2.5 小练习与答案

**练习 1**：`children` 表为什么对话框标 `true`、popup 标 `false`？
答案：`is_blocked()`（L914-917）用它判断父窗口是否被挡住输入。模态对话框期间父窗口不该响应；而 popup（如右键菜单）出现时父窗口仍要能收到点击以便「点旁边关闭菜单」，popup 分支注释明确写了 "Non-blocking"。

**练习 2**：为什么 `WaylandWindow::new` 最后只做一次 `surface.commit()` 而不画第一帧？
答案：xdg_shell 协议要求客户端在第一次 `configure` 之前不得提交带缓冲区的表面状态；`App::open_window` 的「强制首帧」在 Wayland 上实际要推迟到 `handle_xdg_surface_event` 收到初始 Configure 时的 `frame()` 调用。

### 4.3 按需驱动渲染循环（上）：FrameLoop 与 PresentationState 状态机

#### 4.3.1 概念说明

eb354c8d50 之前，Wayland 后端用「每帧心跳」驱动渲染：窗口持续无条件地请求 frame callback，靠 `acknowledged_first_configure`/`force_render_after_recovery` 等补丁维持节拍，空闲窗口也在空转。重写后改为**按需驱动**：渲染循环是一个显式状态机，没有需求时停泊（Parked）在零唤醒状态，需求出现时由外部唤醒。

两个正交的状态变量：

- **`PresentationState`**（4 态）：跟踪「这块表面的像素有没有成功上过屏」。`Unpresented` → `Presented` 是正常路径；呈现失败经 `failed()` 进入 `RetryBeforeFirstPresent` 或 `RetryAfterPresent`——区分的意义在于：从未上过屏的表面可能永远等不到合成器回调（合成器认为它无内容可显示），必须靠**本地定时器**重试；已经上过屏的表面则可以把重试托付给合成器节拍。
- **`FrameLoop`**（8 态）：渲染循环本身的调度状态，回答「下一次 tick 从哪来」。

#### 4.3.2 核心流程

`FrameLoop` 八态（[../gpui_linux/src/linux/wayland/window.rs:L732-L742](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L732-L742)）：

| 状态 | 含义 | 下一次 tick 的来源 |
| --- | --- | --- |
| `Unconfigured` | 还没收到首个 Configure | 合成器的 configure（→ frame()） |
| `Ticking` | 一次 frame() 正在进行 | complete_frame() 的裁决 |
| `RescheduleRequested` | tick 期间又来了新需求 | 本地重试定时器 |
| `PresentationFailed` | 上次呈现失败 | complete_frame() 的裁决 |
| `AwaitingCallback` | 像素已提交，等合成器回调 | wl_callback::Done |
| `Scheduled` | 已 ping，等待事件循环分发 | frame_ping |
| `RetryScheduled` | 已排定重试定时器 | calloop Timer（约 16.7ms） |
| `Parked` | 空闲停泊，无任何唤醒源 | schedule_frame() 的 ping |

一次完整帧的转移：

```text
frame()                        [→ Ticking]
  ├─ 调 GPUI 的 request_frame 回调（场景构建 + present）
  │    └─ present → PlatformWindow::draw(scene)
  │         ├─ 成功: presentation=Presented,  frame_loop=AwaitingCallback
  │         └─ 失败: presentation=failed(),   frame_loop=PresentationFailed
  └─ complete_frame()
       ├─ 当前已是 AwaitingCallback → 直接返回（在等回调）
       ├─ requires_presentation（被节流没画/从未上屏）
       │    ├─ 已上过屏且失败 → 请求 frame callback + commit → AwaitingCallback
       │    └─ 否则 → RetryScheduled + client.schedule_frame_retry()
       ├─ RescheduleRequested 或 redraw_requested → RetryScheduled + 重试定时器
       └─ 否则 → Parked

wl_callback::Done 到达 → frame_callback_fired()  [AwaitingCallback → frame() → …]
```

#### 4.3.3 源码精读

**`PresentationState`**（[../gpui_linux/src/linux/wayland/window.rs:L675-L697](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L675-L697)）：`requires_presentation()` 只对两个 Retry 态为真；`failed()` 把失败映射到「记住是否曾上屏」的 Retry 态。这两条语义有专门的单元测试（[L699-L729](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L699-L729)），断言「失败要记住是否呈现过」「只有 Retry 态需要呈现」。

**`frame()`**（[../gpui_linux/src/linux/wayland/window.rs:L919-L942](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L919-L942)）：进入即置 `Ticking`；读走 `redraw_requested`（注释解释：GPUI 可能只 tick 不画，强制渲染请求要「锁存」到真正到达渲染器的那次 draw）；然后同步调用 GPUI 注册的 `request_frame` 回调，把 `force_render` 与 `require_presentation` 两个诉求装进 `RequestFrameOptions` 传过去。若回调不存在（窗口尚未接入 GPUI），直接 `Parked` 返回。

**`PlatformWindow::draw`**（[../gpui_linux/src/linux/wayland/window.rs:L1901-L1943](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L1901-L1943)）：这是像素真正进 GPU 的地方。三步：① 若 GPU 设备丢失先尝试 `renderer.recover`，失败则标脏下帧再试；② 若没有在途回调则请求 `state.surface.frame(...)`（L1928-1931）——**frame callback 在呈现前请求，附在本次提交上**；③ `renderer.draw(scene)` 成功 → `Presented` + `AwaitingCallback`，失败 → `presentation.failed()` + `PresentationFailed`（L1932-1938）。注意 GPUI 侧的 `Window::present()`（[../gpui/src/window.rs:L3016-L3021](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui/src/window.rs#L3016-L3021)）最终就是调 `platform_window.draw(&self.rendered_frame.scene)`——所以 frame() 里那次回调的执行会同步走到这里。

**`complete_frame()`**（[../gpui_linux/src/linux/wayland/window.rs:L944-L986](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L944-L986)）：frame() 的收尾裁决，按 4.3.2 伪代码逐分支对应。最微妙的是 L952-966 的注释：首帧之前或被节流跳过绘制时，合成器可能**永远不回** frame callback，此时若已经上过屏（`PresentationFailed` 且 `RetryAfterPresent`）就主动补一次 `frame` 请求 + `commit` 转 `AwaitingCallback`，让合成器来定重试节奏——「被遮挡的窗口不该一直轮询」；否则交给本地定时器。最后的兜底是 `Parked`。

#### 4.3.4 代码实践

**实践：手工填写状态转移表（源码阅读型）。**

1. 实践目标：把 4.3.2 的伪代码变成自己推导出的结论。
2. 操作步骤：
   - 只读 `frame()`、`draw()`、`complete_frame()` 三个函数（L919-L986、L1901-L1943）；
   - 画一张 8×4 表格：行是 `FrameLoop` 八态，列是「正常画完」「画失败」「被节流没画」「无回调」，格子里填 complete_frame 出口状态；
   - 用 `PresentationState` 的两个 Retry 态解释表格中「被节流」列的两种不同出口。
3. 需要观察的现象：`AwaitingCallback` 是 complete_frame 里唯一「直接 return」的入口状态。
4. 预期结果：与 4.3.2 的转移图一致；特别能说清 `RetryBeforeFirstPresent`（本地定时器重试）与 `RetryAfterPresent`（补 frame callback 等合成器）的分野。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `redraw_requested` 在 `frame()` 里被读走后仍可能为真？
答案：`draw()` 里 `renderer.needs_redraw()`（L1940-1942）或 GPU 恢复失败会把它重新置真——同一帧内产生的新需求留给下一次 tick，配合 `RescheduleRequested` 保证不丢帧。

**练习 2**：旧模型的 `acknowledged_first_configure` 解决的问题，新模型用什么解决？
答案：旧模型要靠标志位记住「第一个 configure 是否已确认」来决定心跳从何时开始；新模型里 `FrameLoop::Unconfigured` 本身就是这一事实（`is_configured()`，L1009-1011），首个 Configure 直接调用 `frame()` 启动循环，之后由需求驱动，无需心跳。

### 4.4 按需驱动渲染循环（下）：四个唤醒入口、frame_ping 与重试定时器

#### 4.4.1 概念说明

状态机停在 `Parked` 后，必须有东西能把它叫醒。唤醒入口共有四个，对应三种唤醒源加一个协议回调：

1. **`scheduled_frame_fired`**——`frame_ping` 到达（GPUI 主动说「我有新需求」）；
2. **`retry_timer_fired`**——本地重试定时器到期（被节流/首帧前的兜底节拍）；
3. **`frame_callback_fired`**——`wl_callback::Done`（合成器说「你上一帧上屏了」，eb354c8d50 之前这里直接调 `frame()`，现在先过状态机裁决）；
4. **`retry_timer` 的排定者 `schedule_frame_retry`** 与 GPUI 侧 `schedule_frame` 契约。

设计要点是**幂等广播 + 状态过滤**：ping 是全局的、不含目标窗口信息，每个窗口收到广播后检查自己的状态是否匹配，不匹配就忽略——这让唤醒机制天然多路复用且免于竞态。

#### 4.4.2 核心流程

```text
GPUI 侧产生需求（窗口变脏 / on_next_frame / 需要呈现）
→ platform_window.schedule_frame()
   ├─ Parked           → Scheduled + frame_ping.ping()   ← 唤醒停泊的循环
   ├─ Ticking          → RescheduleRequested             ← 本帧结束后再来一次
   └─ 其余状态          → 什么都不做（唤醒已武装：
                          ping 在途 / 定时器在途 / 已提交像素必有回调）

frame_ping 触发 → dispatch_scheduled_frames() 广播
→ 每个窗口 scheduled_frame_fired(): 仅 Scheduled 态执行 frame()

节流/首帧兜底 → schedule_frame_retry(surface_id)
→ calloop Timer 16_667µs 后 → retry_timer_fired(): 仅 RetryScheduled 态执行 frame()

合成器上屏 → wl_callback::Done → frame_callback_fired()
→ 清 pending_frame_callback；仅 AwaitingCallback 态执行 frame()
```

#### 4.4.3 源码精读

**`schedule_frame` 的三分支**（[../gpui_linux/src/linux/wayland/window.rs:L1013-L1026](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L1013-L1026)）：

```rust
pub fn schedule_frame(&self) {
    match self.frame_loop.get() {
        FrameLoop::Parked => {
            self.frame_loop.set(FrameLoop::Scheduled);
            self.frame_ping.ping();
        }
        FrameLoop::Ticking => {
            self.frame_loop.set(FrameLoop::RescheduleRequested);
        }
        // A wake is already armed: a ping or retry timer is in flight, or a
        // presented buffer guarantees a compositor frame callback.
        _ => {}
    }
}
```

这是 `PlatformWindow::schedule_frame` 契约在 Wayland 上的全部实现（trait 实现在 [L1945-L1947](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L1945-L1947) 直接转发到这里）。契约本身在 gpui 主 crate 是个**默认空实现**（[../gpui/src/platform.rs:L864](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui/src/platform.rs#L864)，`fn schedule_frame(&self) {}`）——含义是「平台自己有帧驱动就忽略我」；macOS/Windows/X11/Web 都不覆写，只有 Wayland（与测试替身 TestWindow，[../gpui/src/platform/test/window.rs:L353](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui/src/platform/test/window.rs#L353)）覆写。

**广播器 `dispatch_scheduled_frames`**（[../gpui_linux/src/linux/wayland/client.rs:L448-L463](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L448-L463)）：先克隆出全部窗口句柄再逐个 `scheduled_frame_fired()`——注释点明必须先释放 client 的借用，因为 tick 会重入 GPUI（如 IME 更新）再借 client。

**三个 fired 入口**（[../gpui_linux/src/linux/wayland/window.rs:L988-L1007](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L988-L1007)）：三个函数体结构相同——先查状态匹配再 `frame()`。`frame_callback_fired` 多一步清空 `pending_frame_callback`，其注释解释了一个微妙竞态：「另一次 wl_surface commit 可能捎带了同一个 callback，而唤醒权当时在重试定时器手里」——回调可能在非 AwaitingCallback 状态到达，此时忽略即可，去重靠清 pending 完成。

**`wl_callback::Done` 的派发实现**（[../gpui_linux/src/linux/wayland/client.rs:L1439-L1459](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L1439-L1459)）：`Dispatch<WlCallback, ObjectId>` 的 user data 是 surface 的 `ObjectId`，经 `get_window`（[L1461-L1466](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L1461-L1466)）路由回窗口，`Event::Done` 触发 `frame_callback_fired()`——这正是规格里强调的「Done 改触 frame_callback_fired 而非直接 frame()」。

**重试定时器**（[../gpui_linux/src/linux/wayland/client.rs:L191-L193](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L191-L193) 与 [L465-L485](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L465-L485)）：

```rust
const FRAME_RETRY_INTERVAL: Duration = Duration::from_micros(16_667);
```

文档注释说明取值理由：固定 60Hz 节拍即可——重试只发生在被节流或呈现失败的帧上，匹配输出真实刷新率并不可观测。`schedule_frame_retry` 为该 surface 插一个一次性 `Timer`，到期回调里 `retry_timer_fired()`，`TimeoutAction::Drop` 用后即焚。注释也解释了为何不立即重试：那会顶着把 draw 推迟掉的那道帧率节流空转。

**GPUI 侧的四个调用点**（谁会调 `schedule_frame`）：

- `on_next_frame` 注册回调后立即调（[../gpui/src/window.rs:L2354-L2361](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui/src/window.rs#L2354-L2361)，注释：next-frame 回调制造了帧需求却不弄脏窗口，必须显式唤醒平台帧源）；
- 帧被节流推迟时调（[../gpui/src/window.rs:L1591-L1609](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui/src/window.rs#L1591-L1609)，「Deferred by throttling: ask demand-driven platforms to retry」）；
- 一帧结束后窗口仍脏或仍有 next-frame 回调时调（[../gpui/src/window.rs:L1652-L1660](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui/src/window.rs#L1652-L1660)）；
- `App` 运行循环把待处理效应清空后，对每个脏/待呈现/有 next-frame 回调的窗口调（[../gpui/src/app.rs:L1708-L1716](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui/src/app.rs#L1708-L1716)）——这是「停泊窗口被新工作唤醒」的总入口。

**确定性测试佐证**（[../gpui/src/window.rs:L7092-L7164](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui/src/window.rs#L7092-L7164)）：三个新测试用 TestWindow 的 `simulate_scheduled_frame`/`frame_scheduled` 模拟设施把同一状态机在测试里跑通：`queued_frame_callback_wakes_a_parked_render_loop`（停泊窗口收到 on_next_frame 必须 `frame_scheduled()`）、`pending_presentation_wakes_a_parked_render_loop`（画完待呈现的场面必须唤醒）、`callback_queued_during_a_frame_requests_a_follow_up`（帧内排队的新回调必须在停泊前排定后续帧）。u8-l4 会展开测试平台设施。

#### 4.4.4 代码实践

**实践：跑通状态机的确定性测试。**

1. 实践目标：亲眼看到「停泊—唤醒—再停泊」循环在测试里复现。
2. 操作步骤（任意平台，无需 Wayland 会话）：
   - `cargo test -p gpui --lib queued_frame_callback_wakes_a_parked_render_loop`；
   - 再跑 `cargo test -p gpui --lib pending_presentation_wakes_a_parked_render_loop callback_queued_during_a_frame_requests_a_follow_up`；
   - 打开 [L7092-L7164](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui/src/window.rs#L7092-L7164)，把每个 `assert!` 映射回 4.4.2 流程图的某一步。
3. 需要观察的现象：三个测试全部通过；第一个测试里两次 `simulate_scheduled_frame` 后 `frame_scheduled()` 变 false（停泊），`on_next_frame` 后立刻变 true（唤醒）。
4. 预期结果：如上。**待本地验证**（gpui 编译较重，首次构建需要几分钟）。

#### 4.4.5 小练习与答案

**练习 1**：窗口处于 `AwaitingCallback` 时 GPUI 又调了 `schedule_frame`，会发生什么？
答案：落入 `_ => {}` 分支，什么都不做——已提交的像素保证合成器终将送来 `wl_callback::Done`，唤醒已武装，再 ping 是浪费。

**练习 2**：为什么 `retry_timer_fired` 里状态不是 `RetryScheduled` 就直接忽略，而不是报错？
答案：定时器在途期间窗口可能已被 ping 唤醒并完成了一帧（状态漂移到 `AwaitingCallback` 甚至又回到 `Parked`）。忽略过期唤醒是状态机「幂等广播 + 状态过滤」设计的必然结果；唤醒多扣一次只是空转一个 tick，漏掉才是 bug。

**练习 3**：`FRAME_RETRY_INTERVAL` 为什么不必匹配显示器的实际刷新率？
答案：源码注释（client.rs L191-192）：重试只发生在被节流或失败的帧上，「匹配输出真实刷新率」在效果上不可观测；固定 60Hz（16_667µs）已是最坏情况的合适节拍。

### 4.5 wayland::serial：SerialTracker 与输入序列号

#### 4.5.1 概念说明

Wayland 的安全模型之一是「序列号证明交互」：`set_selection`（剪贴板）、`grab`（popup 抓取）、`set_shape`（光标）这类敏感请求都要带一个 serial，合成器验证它对应真实发生过的输入事件。客户端因此必须**记住自己见过的各类事件序列号**，在发请求时挑对的那个用——这就是 `serial.rs` 存在的全部理由。

关键设计：**选区序列号（selection_serial）只认按键按下与鼠标按下**。声明剪贴板/主选区所有权是「用户复制了东西」的动作，协议要求它引用一次 press 事件；悬浮进入（MouseEnter）、输入法、数据设备这类与「用户按下」无关的 serial 一律不认。

#### 4.5.2 核心流程

```text
合成器 event（带 serial）→ client.rs 的 Dispatch 实现
  → serial_tracker.update(kind, serial)
       ├─ kind ∈ {KeyPress, MousePress} → 同时记录 selection_serial
       └─ 存入 serials[kind]

发请求时按用途取：
  popup grab        → max(MousePress, KeyPress)     （open_window）
  set_cursor_shape  → MouseEnter                    （指针在表面上才有效）
  show_window_menu / start_window_move → MousePress （必须是按住时刻）
  set_selection     → selection_serial              （无则放弃并告警）
```

#### 4.5.3 源码精读

**类型与跟踪器**（[../gpui_linux/src/linux/wayland/serial.rs:L3-L65](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/serial.rs#L3-L65)）：`SerialKind` 五类（DataDevice/InputMethod/MouseEnter/MousePress/KeyPress）；`Serial(u32)` 与 `SelectionSerial(Serial)` 是两个新类型，后者把「可用于选区请求」的序列号在类型层面与其他序列号隔开——不经过 `selection_serial()` 就拿不到它。`update()`（L45-53）只在 KeyPress/MousePress 时更新选区 serial；`get()` 对未跟踪的种类返回 0（popup grab 逻辑据此判断「尚无按下事件时不发 grab」，见 client.rs L1057-1064）。

**五处喂入点**（client.rs）：KeyPress（[L1858](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L1858)）、InputMethod（[L2031](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L2031)）、MouseEnter（[L2100](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L2100)，指针 Enter 事件处理内）、MousePress（[L2215](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L2215)）、DataDevice（[L2593](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L2593)）。

**消费点一：光标切换**（[../gpui_linux/src/linux/wayland/client.rs:L1103-L1141](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L1103-L1141)）：`set_cursor_style` 取 `SerialKind::MouseEnter` 的 serial，优先走 `cursor_shape_device.set_shape(serial, ...)`（cursor-shape-v1 协议）；不支持时回退到加载 XCursor 主题、把光标图像挂到独立 wl_surface 上再 `wl_pointer.set_cursor(serial, ...)`（[../gpui_linux/src/linux/wayland/cursor.rs:L94-L146](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/cursor.rs#L94-L146)）。指针 Enter 事件里也用同一 serial 重设光标（[client.rs:L2092-L2123](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L2092-L2123)）。

**消费点二：剪贴板所有权**（[../gpui_linux/src/linux/wayland/client.rs:L1233-L1257](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L1233-L1257)）：`write_to_clipboard` 先把数据存进本地 `Clipboard`，创建 `wl_data_source`、offer 出文本 MIME 类型与「自描述」MIME，最后 `data_device.set_selection(Some(&data_source), serial.as_raw())`。serial 取 `selection_serial()`；**一个都没有时打警告并放弃所有权请求**——这解释了 u2-l4 讲过的现象：Wayland 下程序刚启动、用户还没按过任何键时，复制可能「无效」。`write_to_primary`（[L1208-L1231](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L1208-L1231)）走 zwp_primary_selection 设备，同样的 serial 逻辑。

**回绕测试**（[../gpui_linux/src/linux/wayland/serial.rs:L98-L104](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/serial.rs#L98-L104)）：`test_uses_event_arrival_order_across_rollover` 故意让 KeyPress serial 为 `0xffff_fff0`、MousePress 为 `0x10`，断言选区 serial 是后者——证明跟踪器按**到达顺序**而非**数值大小**取舍，u32 回绕（约 42.9 亿次事件后）不会破坏语义。

#### 4.5.4 代码实践

**实践：运行 serial 单元测试。**

1. 实践目标：验证「到达顺序优先」与「只认 press」两条规则。
2. 操作步骤：`cargo test -p gpui_linux serial`（gpui_linux 默认 feature 即含 wayland/x11，无需额外开关）。
3. 需要观察的现象：5 个测试全绿，其中 `test_selection_serial_ignores_unrelated_serial_kinds` 断言后到的 InputMethod/MouseEnter/DataDevice serial 不会覆盖已记录的选区 serial。
4. 预期结果：全部通过；这是纯逻辑模块，不依赖图形环境。

#### 4.5.5 小练习与答案

**练习 1**：为什么 popup grab 取 `max(MousePress, KeyPress)` 而不是其中一个？
答案：菜单可能由鼠标右键也可能由键盘快捷键打开，grab 必须引用「打开它的那次交互」；两类 serial 都是 u32 且单调递增，取较大者即最近一次按下（回绕窗口极小，此处按数值取大是务实选择）。

**练习 2**：把 `SelectionSerial` 做成独立新类型（而不是直接用 `Serial`）防止了什么？
答案：防止调用方拿 MouseEnter 之类的 serial 去发 `set_selection` 请求被合成器拒绝——只有经 `SerialTracker::selection_serial()` 这一个受控出口才能得到该类型，「哪些 serial 合法」的规则被编码进了类型系统。

### 4.6 layer_shell 与 popup：Z 风格面板与父子锚定

#### 4.6.1 概念说明

**layer_shell**（`zwlr_layer_shell_v1`）解决「普通窗口语义装不下的表面」：任务栏、启动器、通知、输入法候选框需要钉在某个屏幕层级、贴着屏幕边缘、还能预留独占区（exclusive zone，让别的窗口避开自己）。Zed 用它实现面板/Dock 类 UI；同时它配合**客户端装饰（CSD）**构成 Z 风格界面——`WaylandWindowState` 初始 `decorations: WindowDecorations::Client`（window.rs L612），标题栏由 GPUI 自绘，layer_shell 提供把这种表面钉在 Overlay 层的能力。

**popup**（`xdg_popup`）解决「相对定位」：菜单、下拉框必须贴着触发它的元素。由于无全局坐标，客户端不能说「放在屏幕 (x, y)」，只能说「以父表面几何内的这个矩形为锚、按这个 gravity 伸展、越界时按这个策略调整」——合成器算出最终位置并回执。

两套模型先在 gpui 主 crate 定义为**平台无关类型**（`Layer`、`Anchor` 位标志、`KeyboardInteractivity`、`LayerShellOptions`，[../gpui/src/platform/layer_shell.rs:L9-L83](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui/src/platform/layer_shell.rs#L9-L83)），Wayland 侧的两个小文件只做枚举到位标志的机械映射（[../gpui_linux/src/linux/wayland/layer_shell.rs:L5-L26](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/layer_shell.rs#L5-L26)、[../gpui_linux/src/linux/wayland/popup.rs:L5-L38](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/popup.rs#L5-L38)）。

#### 4.6.2 核心流程

layer_shell 表面的构造（`WaylandSurfaceState::new` 的第一分支）：

```text
globals.layer_shell 为 None？ → 返回 LayerShellNotSupportedError
get_layer_surface(surface, target_output, layer, namespace)
set_size / set_anchor / set_keyboard_interactivity
set_margin（CSS 顺序：上右下左）/ set_exclusive_zone / set_exclusive_edge
→ 返回 WaylandSurfaceState::LayerShell
```

popup 的构造与重锚定：

```text
必须先有父窗口 → build_popup_positioner：
  set_size（钳到 ≥1，0 或负数是协议错误）
  锚矩形从 gpui 表面局部坐标换算到「父窗口几何」坐标并钳到几何内
  set_anchor / set_gravity / set_constraint_adjustment / set_offset
→ get_xdg_surface + get_popup(父 xdg_surface 或经 layer_surface 挂靠)
→ 有 grab 则 xdg_popup.grab(seat, serial)
→ parent.add_child(surface.id(), blocking=false)

父窗口 resize 后 → reposition_popup：重跑 positioner + xdg_popup.reposition
```

#### 4.6.3 源码精读

**LayerShell 分支**（[../gpui_linux/src/linux/wayland/window.rs:L151-L196](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L151-L196)）：`WindowKind::LayerShell(options)` 时先检查协议存在（L153-155，返回 `LayerShellNotSupportedError`）；随后把 `LayerShellOptions` 的每个字段翻译成 `zwlr_layer_surface_v1` 的 request。对照平台无关模型（[../gpui/src/platform/layer_shell.rs:L59-L77](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui/src/platform/layer_shell.rs#L59-L77)）读：`namespace` 给合成器下规则用、创建后不可改；`Anchor` 是位标志，`LEFT | RIGHT` 同时置位即可横向铺满整屏。

**popup 分支与 positioner**（[../gpui_linux/src/linux/wayland/window.rs:L198-L244](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L198-L244) 与 [L315-L364](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L315-L364)）：两个细节值得抄进笔记。其一，锚矩形坐标系换算（L328-346）：协议要的是「相对父窗口几何」的坐标，而 GPUI 的 `anchor_rect` 是表面局部坐标，先平移再整体钳进父几何内至少一像素——伸出几何外或零尺寸都是协议错误，这是客户端防御性编程的典型样本。其二，父子挂靠的两种形态（L216-227）：父是普通窗口走 `xdg_surface.get_popup(Some(parent_xdg_surface), ...)`；父是 layer_shell 表面则传 `None` 再由 `parent_layer_surface.get_popup(&xdg_popup)` 挂靠——layer 表面没有 xdg 身份，协议为此提供了专门入口。grab 逻辑（L230-232）承接 4.5：serial 必须引用一次 press。

**重锚定状态**（[../gpui_linux/src/linux/wayland/window.rs:L307-L313](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L307-L313)）：`WaylandPopupSurfaceState` 特意保留 `options: PopupOptions` 与 `next_reposition_token`——注释说明保留 options 就是为了父窗口尺寸变化后能用 `xdg_popup.reposition` 重新锚定；token 用来把合成器的 `repositioned` 回执与请求配对。调用处即 `resize` 里的 popup 分支（[L1662-L1677](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L1662-L1677)）：首个 Configure 之前 popup 未映射、不能 reposition，初始尺寸由第一次 positioner 携带。

#### 4.6.4 代码实践

**实践：写一个最小 layer-shell 骨架（示例代码，待本地验证）。**

1. 实践目标：体会 `LayerShellOptions` 各字段如何落到协议请求。
2. 操作步骤：参照 u1-l2 的最小窗口程序，把 `WindowOptions` 的 `window_kind` 换成 layer-shell（以下为示例代码，非项目原有代码）：

```rust
use gpui::{layer_shell::{Anchor, KeyboardInteractivity, Layer, LayerShellOptions}, WindowKind, px};

let options = WindowOptions {
    window_kind: WindowKind::LayerShell(LayerShellOptions {
        namespace: "my-panel".into(),
        layer: Layer::Top,
        anchor: Anchor::TOP | Anchor::LEFT | Anchor::RIGHT, // 横向铺满顶部
        exclusive_zone: Some(px(32.)),                       // 其他窗口避开这条带
        keyboard_interactivity: KeyboardInteractivity::OnDemand,
        ..Default::default()
    }),
    ..Default::default()
};
```

3. 需要观察的现象：在不支持 layer_shell 的合成器（如 GNOME）上开窗失败（`LayerShellNotSupportedError`）；在 sway/KWin 上出现贴顶横条，且普通窗口不会与它重叠。
4. 预期结果：如上。**待本地验证**（需要 wlroots 系合成器会话）。

#### 4.6.5 小练习与答案

**练习 1**：`Anchor::LEFT | Anchor::RIGHT` 与只设一个方向的 anchor 有何行为差异？
答案：位标志可组合。双向锚定时合成器把表面拉伸到贴满左右两缘（宽度由合成器定）；单侧锚定时表面保持请求的 `set_size` 尺寸、仅贴住那一侧——这是用 anchor 表达「铺满 vs 靠边」的手段。

**练习 2**：popup 的 `add_child(..., false)` 与对话框的 `add_child(..., true)` 在输入分发上分别意味着什么？
答案：popup 非阻塞，父窗口继续接收输入，用于「点击别处关闭菜单」；对话框阻塞，`is_blocked()` 为真期间父窗口的输入被拦截，实现模态。

## 5. 综合实践

在 Wayland 会话下完成一份「调用路径笔记」，把本讲四个模块串起来（Linux 环境可用 `echo $XDG_SESSION_TYPE` 确认是 wayland）：

1. **准备**：`WAYLAND_DEBUG=1 cargo run -p gpui --example window 2>frames.log`；另外开一个终端准备 `tail -f frames.log`。
2. **窗口创建路径**：从日志开头的 `wl_compositor.create_surface`、`xdg_wm_base.get_xdg_surface`、`xdg_surface.get_toplevel`、首次 `xdg_surface.configure` + `ack_configure`，对照 4.2 首帧流程，确认「像素提交发生在首次 configure 之后」。
3. **光标切换路径**：把鼠标移进移出窗口，在日志里找 `wl_pointer.enter/leave`（serial 出现在请求参数里）与 `wp_cursor_shape_device_v1.set_shape`（或回退路径的 `wl_pointer.set_cursor`），对照 4.5 的 serial 消费点，记下用的是哪一类 serial。
4. **剪贴板写入路径**：选中文本按 Ctrl+C，在日志里找 `wl_data_device_manager.create_data_source`、`data_source.offer`（逐个 MIME）、`wl_data_device.set_selection(serial=...)`，对照 4.5 消费点二，抄下那个 serial 并回日志里找它对应的是哪次按键事件。
5. **空闲停泊验证**：停止一切交互 30 秒，`grep -c "wl_surface.frame" frames.log` 每隔 10 秒采样一次——数字应不再增长（无新的 frame callback 请求、无空帧提交），这正是 `FrameLoop::Parked` 的可观测投影；再动一下鼠标触发一帧，数字恢复增长。
6. **产出**：一份 Markdown 笔记，含三张「操作 → 协议对象序列」对照表和 Parked 验证结论；若第 5 步数字仍在增长，回到 4.4 检查你的合成器是否走在「被节流重试」路径上并记录原因。

## 6. 本讲小结

- `WaylandClient` 的骨架是 `Rc<RefCell<WaylandClientState>>` + calloop 主循环：`Globals` 集中绑定协议对象并区分「必需（unwrap）」与「尽力而为（Option）」，唯一的 `frame_ping` 存在 `Globals` 里共享给每个窗口。
- Wayland 无全局坐标：`primary_display`/`window_stack` 返回 None，resize 只改表面局部几何，popup 靠 positioner 相对父窗口几何锚定并支持 `reposition` 重锚定，layer_shell 靠 anchor 相对屏幕边缘定位——三种定位策略各对应一种表面形态。
- 渲染循环是**按需驱动**的显式状态机：`FrameLoop` 八态回答「下一次 tick 从哪来」，`PresentationState` 四态记住「像素是否上过屏」；`Parked` 是零唤醒的稳态，首帧由 `Unconfigured` 收到首次 Configure 时的 `frame()` 点燃。
- 四个唤醒入口各司其职：`schedule_frame`（Parked→Scheduled+ping）、`frame_callback_fired`（合成器回调）、`scheduled_frame_fired`（ping 广播）、`retry_timer_fired`（16_667µs 定时器，兜底被节流与首帧前场景）；所有入口都先过滤状态再 tick，多唤醒与过期唤醒都被幂等吸收。
- `SerialTracker` 按「事件到达顺序」跟踪五类序列号，选区序列号只认 press 事件；popup grab、光标、窗口菜单/移动、剪贴板所有权各取所需，类型层面的 `SelectionSerial` 把合法性规则编码进签名。
- layer_shell 与 popup 的平台无关模型定义在 gpui 主 crate，Wayland 侧只做枚举映射与防御性钳制（锚矩形至少一像素、尺寸至少为 1），配合 CSD 默认值支撑 Z 风格自绘界面。

## 7. 下一步学习建议

- **u5-l5（xdg-desktop-portal）**：Linux 单元收官，看文件选择器等系统对话框如何在 Wayland 之上再借一层 portal 协议完成。
- **回读 GPUI 帧调度**：带着本讲的状态机视角重读 `../gpui/src/window.rs` 的帧节流与 `invalidator.wake_platform()`，以及 `../gpui/src/app.rs:L1693-L1721` 的效应排空循环，理解「契约两侧」如何配合。
- **u8-l4（test-support）**：深入 TestWindow 的 `schedule_frame`/`simulate_scheduled_frame`/`frame_scheduled` 模拟设施，学会在没有 Wayland 会话的机器上确定性地测试按需渲染循环。
- **延伸阅读**：wayland.app 上的 [xdg-shell](https://wayland.app/protocols/xdg-shell-unstable-v1)、[wlr-layer-shell](https://wayland.app/protocols/wlr-layer-shell-unstable-v1) 与 [frame callback](https://wayland.app/protocols/wayland#wl_surface:request:frame) 协议文本，逐条对照本讲引用的请求与事件。

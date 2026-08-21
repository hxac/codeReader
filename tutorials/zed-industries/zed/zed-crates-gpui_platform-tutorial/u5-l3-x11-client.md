# X11 客户端:XCB 连接、事件翻译与 XIM 输入法

## 1. 本讲目标

本讲是「Linux 平台深入」单元的第三讲,深入 `gpui_linux` 三大后端中的 X11 后端。学完本讲,你应该能够:

1. 描述 `X11Client` 如何用 XCB 建立连接、协商扩展(XInput/XKB/RandR/DRI3)并初始化全部平台状态。
2. 解释事件循环的关键盲区:calloop 只监听底层 socket、看不到 x11rb 内部缓冲队列,以及 `f4178619ac` 引入的「前台 runnable 执行完毕后主动排空」机制如何修复 X11 首开窗口空白帧。
3. 读懂事件翻译层:`process_x11_events` 的双层循环、键盘自动重复去重、`x11/event.rs` 中的纯函数翻译助手,以及 `handle_event` 如何把 XCB 事件变成 `gpui::PlatformInput`。
4. 理解 XIM 输入法集成为何需要独立的 `XimHandler` 模块与「信封暂存 + 借用舞蹈」。
5. 说明 X11 剪贴板与主选区的实现要点:所有权模型、跨进程选择协商、INCR 增量传输与服务线程。

## 2. 前置知识

### 2.1 X11 是一个「客户端-服务器」协议

与 Wayland(下一讲的主角)不同,X11 把窗口系统做成一台「服务器」:你的应用是客户端,通过 Unix socket 或 TCP 连接到 X Server,用协议消息创建窗口、绘图、接收输入事件。理解三件事就够用了:

- **请求(request)与应答(reply)**:客户端发出请求;需要数据的请求(如查询属性)会有应答。x11rb 把应答做成 cookie,可以异步发出多个请求再统一收答复(批量化,减少往返)。
- **事件(event)**:服务器主动推给客户端的消息(按键、窗口尺寸变化、暴露区域等),进入客户端连接里的**事件队列**。
- **原子(atom)**:服务器端的字符串驻留表。协议消息里用整数 ID 引用名字(如 `WM_DELETE_WINDOW`、`UTF8_STRING`),避免反复传字符串。`intern_atom` 请求把名字换成 ID。

`x11rb` 是纯 Rust 的 XCB(X C Binding)实现,`XCBConnection`(来自 `x11rb::xcb_ffi`)是它对 libxcb C 库的封装,也是 `X11Client` 使用的连接类型。

### 2.2 X11 的剪贴板:没有守护进程,只有「选区所有权」

X11 的剪贴板不是一个系统服务,而是一组**选区(selection)**:`CLIPBOARD`(Ctrl+C/V 用的)、`PRIMARY`(鼠标选中即写入、中键粘贴)、`SECONDARY`(罕见)。复制数据的进程通过 `set_selection_owner` 声明「我拥有这个选区」,数据本身**留在拥有者进程的内存里**;其他进程来取时,拥有者必须活着应答。这就是为什么本讲的 clipboard 模块需要一个常驻服务线程——细节在 4.4 展开。

### 2.3 XIM:输入法框架

XIM(X Input Method)是 X11 的输入法协议:一个独立进程(如 fcitx、ibus 的 XIM 前端)充当输入法服务器,客户端把按键事件**转发**给它,它决定「吃掉」(用于组字)还是**发回**(直接上屏)。Zed 使用 `xim` crate(纯 Rust 实现),它要求使用者在过滤事件时实现 `ClientHandler` 回调 trait——这个回调风格与 GPUI 自己的事件循环不同步,正是 `XimHandler` 独立成模块的根源(4.3 展开)。

### 2.4 承接前几讲的认知

- **u5-l1**:`LinuxPlatform<P>` 是外壳,`LinuxClient` 是 crate 私有契约,`LinuxCommon` 承载双执行器、`LoopSignal` 与公共回调。本讲的 `X11Client` 就是该契约的 X11 实现。
- **u4-l3**:`LinuxDispatcher` 不拥有主事件循环;主循环归各客户端所有,调度器只通过 `main_sender`(优先级队列 + ping)唤醒它。本讲会看到 `X11Client` 的 calloop 主循环里如何接住这些投递。
- **u3-l3**:键盘布局与 `Keystroke` 模型、`keystroke_from_xkb` 的翻译规则已讲过,本讲只看它在 X11 事件流里的调用时机。
- **u3-l2**:`PlatformWindow` 契约与 X11 窗口(`x11/window.rs`)已讲过,本讲只在开窗链路上引用,不重复。

## 3. 本讲源码地图

本讲涉及的关键文件(路径相对于 `crates/gpui_platform/`,兄弟 crate 用 `../` 前缀):

| 文件 | 作用 |
| --- | --- |
| [../gpui_linux/src/linux/x11/client.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs) | X11 后端主体:`X11ClientState` 状态集合、`X11Client::new()` 连接与初始化、`process_x11_events` 事件泵、`handle_event` 翻译中枢、`LinuxClient` 契约实现、缩放因子与合成器探测等辅助函数。约 3100 行,是 X11 后端的心脏 |
| [../gpui_linux/src/linux/x11/event.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/event.rs) | 事件翻译的**纯函数助手**:按钮编号→`MouseButton`、modifier 掩码→`Modifiers`、valuator 位掩码→轴下标等。无状态、可单测 |
| [../gpui_linux/src/linux/x11/xim_handler.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/xim_handler.rs) | XIM 输入法处理器:实现 `xim` crate 的 `ClientHandler` 回调 trait,暂存回调产物 `XimCallbackEvent` |
| [../gpui_linux/src/linux/x11/clipboard.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/clipboard.rs) | X11 剪贴板:移植自 arboard 项目,`Clipboard` 门面 + 独立连接的常驻服务线程 + INCR 增量读取 |
| ../gpui_linux/src/linux/x11.rs | 模块聚合文件,仅 13 行,把上述子模块全部 `pub(crate) use` 导出 |
| ../gpui_linux/src/linux/x11/window.rs、display.rs | X11 窗口与显示器的 X 封装(u3-l2、u2-l3 已覆盖,本讲只在开窗/枚举链路上经过) |
| [../gpui/examples/window.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui/examples/window.rs) | GPUI 官方窗口示例,本讲代码实践的运行载体;其中已内置 `observe_window_bounds` 打印逻辑 |

## 4. 核心概念与源码讲解

### 4.1 X11Client:XCB 连接建立、初始化与事件源注册

#### 4.1.1 概念说明

`X11Client` 是 `LinuxClient` 契约在 X11 上的实现。它的外形非常简单——一个包装了 `Rc<RefCell<X11ClientState>>` 的元组结构体:

```rust
#[derive(Clone)]
pub(crate) struct X11Client(pub(crate) Rc<RefCell<X11ClientState>>);
```

定义于 [../gpui_linux/src/linux/x11/client.rs:L305-L306](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L305-L306)。全部真实状态都在 `X11ClientState` 里,可以按职责分成九组:

| 分组 | 代表字段 | 用途 |
| --- | --- | --- |
| 事件循环 | `loop_handle`、`event_loop` | calloop 主循环与注册句柄 |
| XCB 连接 | `xcb_connection: Rc<XCBConnection>`、`atoms: XcbAtoms`、`resource_database`、`x_root_index` | 与 X Server 的连接、驻留原子、资源库、默认屏幕编号 |
| 键盘 | `xkb_context`、`xkb: xkbc::State`、`keyboard_layout`、`compose_state`、`xkb_device_id` | XKB 键位状态机、布局身份、死键 compose |
| 输入法 | `ximc: Option<X11rbClient<...>>`、`xim_handler: Option<XimHandler>`、`pre_edit_text`、`composing` | XIM 会话与组字上下文 |
| 修饰键 | `modifiers`、`capslock`、`last_modifiers_changed_event`、`last_capslock_changed_event` | 当前修饰键状态与去重快照 |
| 窗口表 | `windows: HashMap<xproto::Window, WindowRef>`、`mouse_focused_window`、`keyboard_focused_window` | X 窗口 id → 窗口状态,鼠标/键盘焦点 |
| 光标 | `cursor_handle`、`cursor_styles`、`cursor_cache`、`invisible_cursor_cache`、`cursor_hidden_window` | XCursor 主题加载与样式缓存 |
| 滚动 | `pointer_device_states: BTreeMap<xinput::DeviceId, PointerDeviceState>` | 每指针设备的滚动轴换算状态 |
| 公共 | `common: LinuxCommon`、`clipboard`、`clipboard_item`、`xdnd_state` | u5-l1 讲过的公共状态、剪贴板、XDnD 拖放 |

完整定义见 [../gpui_linux/src/linux/x11/client.rs:L171-L225](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L171-L225)。

`Option<X11rbClient>` 值得注意:XIM 服务器可能不存在(比如没装输入法,或 `XMODIFIERS` 未指向有效 IM),`X11rbClient::init(...).ok()` 会失败,此时 `ximc` 为 `None`,整条输入法链路退化为直通——这是 X11 后端里「能力探测型 Optional」的典型形态。

#### 4.1.2 核心流程

`X11Client::new()` 是一条约 260 行的初始化流水线,顺序如下(伪代码):

```text
1. EventLoop::try_new()                     # 创建 calloop 主循环
2. LinuxCommon::new(loop_signal)            # 拿到 common、main_receiver、wake_receiver
3. 注册 main_receiver 源                     # 前台任务 → insert_idle → 排空 x11rb 队列 ★
4. 注册 wake_receiver 源                     # 系统唤醒(login1)回调
5. XCBConnection::connect(None)             # 连接 X Server($DISPLAY)
6. prefetch 四个扩展信息                     # xkb / randr / render / xinput
7. XiQueryVersion(2, 4)                     # XInput 版本协商 → 是否支持手势
8. XcbAtoms::new(...).reply()               # 批量 intern 原子
9. 合成器探测 + GTK frame extents 探测       # 决定能否客户端装饰(CSD)
10. xkb_use_extension + xkb_select_events   # 启用 XKB,订阅布局/映射变化事件
11. new_xkb_context + keymap/state_from_device + compose_state
12. resource_database + get_scale_factor    # Xft.dpi / RandR / 环境变量三路缩放
13. cursor::Handle::new                     # XCursor 主题句柄
14. Clipboard::new()                        # 剪贴板单例与服务线程
15. detect_compositor_gpu                   # DRI3 打开渲染设备,给 wgpu 选设备用
16. X11rbClient::init + XimHandler::new     # XIM 输入法(可失败)
17. FdWrapper(fd) 注册为 calloop Generic 源  # socket 可读 → process_x11_events ★
18. 注册 XDPEventSource                     # xdg-desktop-portal 外观/按钮布局事件
19. xcb_flush + 构造 X11ClientState
```

其中标 ★ 的第 3、17 步是事件进入 GPUI 的两条主干道,4.1.3 重点精读。

#### 4.1.3 源码精读

**(1) 建立连接与扩展协商**

```rust
let (xcb_connection, x_root_index) = XCBConnection::connect(None)?;
xcb_connection.prefetch_extension_information(xkb::X11_EXTENSION_NAME)?;
xcb_connection.prefetch_extension_information(randr::X11_EXTENSION_NAME)?;
xcb_connection.prefetch_extension_information(render::X11_EXTENSION_NAME)?;
xcb_connection.prefetch_extension_information(xinput::X11_EXTENSION_NAME)?;
```

这段在 [../gpui_linux/src/linux/x11/client.rs:L351-L355](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L351-L355):`connect(None)` 表示用 `$DISPLAY` 环境变量选择服务器并返回默认屏幕号 `x_root_index`;四个 `prefetch_extension_information` 提前异步查询扩展是否存在(XKB 键盘扩展、RandR 显示器/缩放、Render 图形、XInput 输入),cookie 后面用到时再收答复,不阻塞。

紧接着是 XInput 版本协商([../gpui_linux/src/linux/x11/client.rs:L357-L376](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L357-L376)):

```rust
let xinput_version = get_reply(
    || "XInput XiQueryVersion failed",
    xcb_connection.xinput_xi_query_version(2, 4),
)?;
assert!(xinput_version.major_version >= 2, "XInput version >= 2 required.");
let supports_xinput_gestures = xinput_version.major_version > 2
    || (xinput_version.major_version == 2 && xinput_version.minor_version >= 4);
```

客户端宣告「我支持到 XInput 2.4」,服务器回应它实际支持的最高版本;若低于 2.4 就拿不到触控板捏合手势事件(`XinputGesturePinchBegin/Update/End`),`supports_xinput_gestures` 会被记下并在开窗时传给 `X11Window`,决定是否请求手势掩码。鼠标/按键事件本身走的是 XInput 2 的 `XinputButtonPress`、`XinputMotion` 等事件,而非老的核心协议事件——这也是 4.2 翻译表里全是 `Xinput*` 前缀的原因。

**(2) 原子、合成器与客户端装饰探测**

[../gpui_linux/src/linux/x11/client.rs:L381-L395](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L381-L395) 用 `XcbAtoms::new(...).reply()` 一次性 intern 全部需要的原子(`XcbAtoms` 由 `x11rb::atom_manager!` 宏在 [../gpui_linux/src/linux/x11/window.rs:L37-L38](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/window.rs#L37-L38) 生成),随后:

- `check_compositor_present`([../gpui_linux/src/linux/x11/client.rs:L2189-L2256](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L2189-L2256)):用三种方法探测合成器是否存在——查 `_NET_WM_CM_S{root}` 选区的拥有者、查 `_NET_WM_CM_OWNER` 属性、查 `_NET_SUPPORTING_WM_CHECK` 属性,任一命中即认为有合成器。
- `check_gtk_frame_extents_supported`([../gpui_linux/src/linux/x11/client.rs:L2258-L2285](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L2258-L2285)):读根窗口的 `_NET_SUPPORTED` 原子列表,看窗口管理器是否支持 `_GTK_FRAME_EXTENTS`。

两者同时满足时 `client_side_decorations_supported` 才为真——只有跑在合成器上、且 WM 理解 GTK 的帧扩展协议,Zed 才敢自己画标题栏(阴影需要合成器合成,`_GTK_FRAME_EXTENTS` 用来告诉 WM 内容真实的边界)。这个布尔随后在 `open_window` 里传给 `X11Window::new`([../gpui_linux/src/linux/x11/client.rs:L1617](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L1617))。

**(3) XKB 初始化**

[../gpui_linux/src/linux/x11/client.rs:L397-L425](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L397-L425) 先 `xkb_use_extension` 启用服务器端 XKB 并断言 `supported`,再用 `xkb_select_events` 订阅三类事件:`STATE_NOTIFY`(修饰键/布局组状态变化)、`MAP_NOTIFY`(键位映射变化)、`NEW_KEYBOARD_NOTIFY`(物理键盘更换),并声明关心的映射组成部分(键类型、键符号、修饰键映射等)。这保证 4.2 里的 `Event::XkbStateNotify`、`Event::XkbMapNotify` 分支有事件可收。

接着 [../gpui_linux/src/linux/x11/client.rs:L427-L444](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L427-L444) 从**设备**而非文件构造键位映射:`keymap_new_from_device` + `state_new_from_device` 直接读取服务器对该物理键盘的描述,再 `serialize_layout(STATE_LAYOUT_EFFECTIVE)` 取出当前布局名包成 `LinuxKeyboardLayout`。`get_xkb_compose_state(&xkb_context)` 则初始化死键/组合输入的 compose 状态机。

**(4) 缩放因子三路探测**

[../gpui_linux/src/linux/x11/client.rs:L2530-L2603](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L2530-L2603) 的 `get_scale_factor` 按优先级决定 UI 缩放:

1. 环境变量 `GPUI_X11_SCALE_FACTOR`:数字则直接用;字符串 `randr` 则强制走 RandR 物理尺寸推算;非法值 panic。
2. 资源数据库的 `Xft.dpi`:除以基准 96 得到缩放。
3. RandR 推算(`get_randr_scale_factor`,用显示器物理毫米尺寸与像素数算 DPI)。
4. 兜底 1.0。

RandR 路径的 DPI 量化在 `get_dpi_factor`([../gpui_linux/src/linux/x11/client.rs:L2742-L2769](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L2742-L2769)):先算每毫米像素数 ppmm,再量化到 1/12 步长并夹在 \[1.0, 20.0\]:

\[
\text{dpi\_factor} \;=\; \frac{\operatorname{round}\!\left(\text{ppmm} \times \dfrac{25.4 \times 12}{96}\right)}{12}, \qquad
\text{ppmm} \;=\; \sqrt{\frac{w_{px} \cdot h_{px}}{w_{mm} \cdot h_{mm}}}
\]

量化到 1/12 是为了得到 1.25、1.5、1.75、2.0 这类「正常」缩放档位,而不是 1.237 之类的碎值。

**(5) ★ 两条事件主干道与 f4178619ac 的排空机制**

这是本讲最重要的源码。第一条主干道是 **calloop 的 fd 源**——把 XCB 连接的底层 socket 注册进主循环,socket 可读(服务器发来数据)时就泵事件:

```rust
// Safety: Safe if xcb::Connection always returns a valid fd
let fd = unsafe { FdWrapper::new(Rc::clone(&xcb_connection)) };

handle
    .insert_source(
        Generic::new_with_error::<EventHandlerError>(
            fd, calloop::Interest::READ, calloop::Mode::Level,
        ),
        {
            let xcb_connection = xcb_connection.clone();
            move |_readiness, _, client| {
                client.process_x11_events(&xcb_connection)?;
                Ok(calloop::PostAction::Continue)
            }
        },
    )
```

见 [../gpui_linux/src/linux/x11/client.rs:L468-L486](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L468-L486)。`FdWrapper` 是 x11rb 为 calloop 提供的适配器,从 `Rc<XCBConnection>` 里安全地交出原始 fd;`Mode::Level` 是电平触发——只要 socket 还有未读数据,每次循环都会报告。

问题在于:**x11rb 在连接对象内部还维护着自己的事件缓冲队列**。`poll_for_event()`(4.2 精读)从这个内部队列取事件;而事件的到达路径是「socket 数据 → x11rb 的读取逻辑 → 内部队列」。存在这样的时序:某次读取把 socket 里的数据一次性搬进了 x11rb 内部队列,socket 空了;calloop 只监听 socket,**看不到内部队列里还有存货**,不会再触发 fd 回调。如果此时 GPUI 正忙于执行一串前台 runnable(典型场景:应用启动、首开窗口时排队的一批初始化/绘制任务),而这批 runnable 执行期间 X Server 发来的 `MapNotify`、`Expose`、`ConfigureNotify` 已经躺在 x11rb 内部队列里,那么没有任何代码会去处理它们——窗口显示出来却停留在空白帧。

`f4178619ac` 引入的修复是让第二条主干道「顺带排空」。第二条主干道是 u4-l3 讲过的 `main_receiver`(前台调度器投递任务的通道),它把每个 runnable 包成 calloop 的 idle 回调:

```rust
handle
    .insert_source(main_receiver, {
        let handle = handle.clone();
        move |event, _, _: &mut X11Client| {
            if let calloop::channel::Event::Msg(runnable) = event {
                // Insert the runnables as idle callbacks, so we make sure that user-input and X11
                // events have higher priority and runnables are only worked off after the event
                // callbacks.
                handle.insert_idle(|client| {
                    let location = runnable.metadata().location;
                    let spawned = runnable.metadata().spawned;
                    profiler::update_running_task(spawned, location);
                    runnable.run();
                    profiler::save_task_timing();

                    let xcb_connection = client.0.borrow().xcb_connection.clone();
                    client.process_x11_events(&xcb_connection).log_err();
                });
            }
        }
    })
```

见 [../gpui_linux/src/linux/x11/client.rs:L316-L339](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L316-L339)。两层设计各有用意:

- **`insert_idle` 保证优先级**:idle 回调在 calloop 的一轮迭代里排在所有就绪事件源之后执行。也就是说,fd 源报告的 X11 事件(用户输入、窗口事件)先被处理,前台 runnable 后执行——注释里写明了这是刻意的「input first, tasks second」。
- **执行完每个 runnable 后主动 `process_x11_events`**:即注释之外的第三行 `client.process_x11_events(&xcb_connection)`。它不依赖任何就绪信号,直接去问 x11rb「你内部还有事件吗」,有就处理。于是即使 calloop 因为 socket 已空而「失明」,每跑完一个前台任务都会把内部队列清一遍——首开窗口时排队的 runnable 跑完的同时,`Expose` 等事件也被消化掉,空白帧问题消失。

同样的排空还出现在第三个位置:每窗口的刷新定时器。`start_refresh_loop` 用 calloop timer 按显示器刷新率驱动重绘,每次 `window.refresh(...)` 之后同样调用 `process_x11_events`,见 [../gpui_linux/src/linux/x11/client.rs:L1979-L2015](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L1979-L2015)(第 2004 行)。这样三处调用点——fd 可读、每个 runnable 之后、每帧刷新之后——共同保证 x11rb 内部队列不会长时间积压。

对比 Wayland 侧(u4-l3 已讲):Wayland 后端的 `insert_idle` 回调没有这行排空,因为 wayland-client 的队列模型不同,事件源直接挂在协议队列上。这是「同一契约、不同宿主原语」的又一次体现。

**(6) 其余事件源**

- `wake_receiver`([../gpui_linux/src/linux/x11/client.rs:L341-L349](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L341-L349)):接收 u5-l1 讲过的系统唤醒信号(login1 `PrepareForSleep` 经 DBus 与通道转发),回调 `common.handle_system_wake()`。
- `XDPEventSource`([../gpui_linux/src/linux/x11/client.rs:L488-L511](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L488-L511)):xdg-desktop-portal 推送的外观(color-scheme)与窗口按钮布局变化。外观变化会同步更新 `common.appearance` 并逐窗口 `set_appearance`;按钮布局变化解析后逐窗口 `set_button_layout`。注意 `CursorTheme`/`CursorSize` 事件是显式 no-op——X11 上光标主题由 XCursor 自己管理,不需要 GPUI 干预。

#### 4.1.4 代码实践

**实践目标**:亲手验证 `get_scale_factor` 的环境变量优先级,并确认程序确实运行在 X11 后端上。

**操作步骤**:

1. 在 X11 会话(或清空 Wayland 变量)下运行 GPUI 的 window 示例:

   ```bash
   cd <zed 仓库根目录>
   WAYLAND_DISPLAY= cargo run -p gpui --example window
   ```

   说明:清空 `WAYLAND_DISPLAY` 是为了让 u1-l4 讲过的 `guess_compositor()` 落到 X11 分支(`DISPLAY` 非空即选 X11)。gpui 的 dev-dependencies 已为 `gpui_platform` 启用 `font-kit`、`wayland`、`x11` 三个 feature(见 [../gpui/Cargo.toml:L135](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui/Cargo.toml#L135)),无需额外 feature 参数。

2. 窗口出现后,按 Ctrl+C 结束,再设缩放变量重跑:

   ```bash
   WAYLAND_DISPLAY= GPUI_X11_SCALE_FACTOR=2 cargo run -p gpui --example window
   ```

3. 换成 RandR 模式再跑一次:

   ```bash
   WAYLAND_DISPLAY= GPUI_X11_SCALE_FACTOR=randr cargo run -p gpui --example window
   ```

**需要观察的现象**:

- 第 1 步:一个 800×600 的按钮面板窗口,标题栏由 WM 提供;拖拽边缘可 resize,终端会打印 `Window bounds changed: ...`(示例自带的 `observe_window_bounds` 打印,见 [../gpui/examples/window.rs:L322-L325](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui/examples/window.rs#L322-L325))。
- 第 2 步:整个 UI(文字、按钮、间距)按 2 倍放大,窗口逻辑尺寸不变——即内容变「大」而非窗口变「大」。
- 第 3 步:缩放值取决于显示器的物理尺寸推算(普通 96 DPI 全高清屏上通常仍是 1.0,HiDPI 屏上会大于 1)。

**预期结果**:第 2 步的 2 倍缩放是确定行为(`get_scale_factor` 第一优先级直接返回该值);第 3 步的具体数值依赖硬件,「待本地验证」。若在第 1 步看到的不是 X11 窗口(例如出现在 Wayland 任务栏),说明环境变量未生效,检查 `$DISPLAY` 是否非空。

#### 4.1.5 小练习与答案

**练习 1**:`X11Client` 为什么把 `xcb_connection` 包成 `Rc<XCBConnection>` 而不是直接持有?

**参考答案**:连接要被多方共享引用——calloop 的 fd 源回调、`insert_idle` 闭包、XIM 的 `X11rbClient<Rc<XCBConnection>>`、每个 `X11Window` 都持有连接的克隆。`Rc` 让这些引用零成本共享同一底层 socket 与 x11rb 内部队列;单线程(主线程)使用也不需要 `Arc` 的原子开销。对应的 `FdWrapper::new(Rc::clone(&xcb_connection))` 正是利用 `Rc` 保证 fd 在源存活期间有效。

**练习 2**:`supports_xinput_gestures` 为假时,X11 后端缺了什么能力?去哪里能找到它被消费的位置?

**参考答案**:缺触控板双指捏合(zoom)手势——XInput 2.4 才引入 `GesturePinchBegin/Update/End` 事件。它作为参数传入 `X11Window::new`([../gpui_linux/src/linux/x11/client.rs:L1623-L1644](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L1623-L1644) 中 `supports_xinput_gestures` 实参),由窗口侧决定是否向服务器请求手势事件掩码(具体在 `x11/window.rs`,u3-l2 已覆盖)。

**练习 3**:为什么 `client_side_decorations_supported` 需要同时满足「有合成器」和「WM 支持 `_GTK_FRAME_EXTENTS`」两个条件,缺一不可?

**参考答案**:客户端自绘标题栏的阴影依赖合成器把半透明阴影与下层窗口合成;没有合成器(纯 WM)阴影区域会成为难看的黑块。`_GTK_FRAME_EXTENTS` 则是向 WM 声明「窗口表面边缘 N 像素是装饰,别把内容布局算进去」的协议——WM 不认识它时,最大化和边框计算会出错。所以 `compositor_present && gtk_frame_extents_supported` 是 [../gpui_linux/src/linux/x11/client.rs:L386-L395](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L386-L395) 的与逻辑,失败则回退为服务器端装饰(SSD)。

### 4.2 process_x11_events 与事件翻译层:x11::event 纯函数助手 + handle_event 分发中枢

#### 4.2.1 概念说明

X11 事件到 GPUI 事件的翻译分两层,跨两个文件:

- **`x11/event.rs` 是无状态的纯函数助手层**。它回答「一个位域/编号如何解释」:按钮 detail 1-9 各是什么、`KeyButMask` 掩码里哪些位代表 Ctrl/Alt/Shift/Super、valuator 位掩码如何映射到 `axisvalues` 数组下标。纯函数意味着可以像该文件末尾的 `test_get_valuator_axis_index` 一样直接写单元测试断言——在事件翻译这种容易出 off-by-one 的地方,可测性是真金白银。
- **`client.rs` 的 `process_x11_events`(泵)+ `handle_event`(分发)是有状态的编排层**。它决定「这批事件按什么顺序处理、哪些要合并、哪些先过输入法、翻译结果送给哪个窗口」。

为什么需要「泵(pump)」?因为 calloop 的回调只告诉你「该看一眼了」,事件可能积压了一批;`process_x11_events` 负责把 x11rb 内部队列一次性排空(呼应 4.1 的排空机制),并在排空过程中做三类整理:键盘自动重复去重、键盘映射变更事件的排序、XIM 过滤。

#### 4.2.2 核心流程

```text
process_x11_events(xcb_connection):
  loop:                                     # 外层:处理中产生的新事件再排一轮
    events = []; windows_to_refresh = {}
    last_key_release = None                 # 缓存上一个 KeyRelease
    last_keymap_change_event = None         # 缓存 keymap 变更事件
    loop:                                   # 内层:poll_for_event 排空 x11rb 队列
      e = xcb_connection.poll_for_event()
      None → break
      IoError → 上抛(连接死了)
      Expose(w)          → windows_to_refresh += w     # 不进 events
      KeyRelease(e)      → 先把缓存的 keymap 事件和上一个 release 落盘,再缓存 e
      KeyPress(e)        → 若缓存的 release 与本 press 同键且间隔 ≤ 20ms,
                           判定为 X11 自动重复,丢弃 release;push(press)
      XkbNewKeyboardNotify / XkbMapNotify
                           → 缓存(去重),压到批次末尾
      其他                → push(e)
    落盘残留在缓存里的 release / keymap 事件
    events 与 windows_to_refresh 都空 → break(真正排空)
    for event in events:
      无 XIM            → handle_event(event)           # 直通翻译
      有 XIM:
        (handled, cb) = ximc.filter_event(event, &mut xim_handler)
        cb 有值        → handle_xim_callback_event(cb)  # 输入法产物
        handled        → continue(被输入法吃掉)
        xim 已连接     → xim_handle_event(event)        # 转发给输入法服务器
        否则           → handle_event(event)            # 退化直通
        filter 出错    → 摘除 XIM,handle_event(event)   # 输入法崩了,降级
    for w in windows_to_refresh:
      w.refresh(require_presentation: true)             # Expose → 重绘
```

#### 4.2.3 源码精读

**(1) 键盘自动重复的 20ms 启发式**

X11 协议对按键按住不放的表现是:服务器持续生成 `KeyRelease` + `KeyPress` 对。客户端要自己识别哪些是自动重复。Zed 的做法在 [../gpui_linux/src/linux/x11/client.rs:L601-L623](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L601-L623):

```rust
if let Some(Event::KeyRelease(key_release)) = last_key_release.take() {
    // We ignore that last KeyRelease if it's too close to this KeyPress,
    // suggesting that it's auto-generated by X11 as a key-repeat event.
    if key_release.detail != key_press.detail
        || key_press.time.saturating_sub(key_release.time) > 20
    {
        events.push(Event::KeyRelease(key_release));
    }
}
```

判定规则:缓存的 release 与新来的 press **同键**(detail 相同)且时间差 ≤ 20ms,则这个 release 是自动重复的伴生事件,丢弃;否则(不同键,或间隔太长——用户真的很快地松了又按)保留 release。`saturating_sub` 防御时间戳回绕。

**(2) XIM 三态过滤**

[../gpui_linux/src/linux/x11/client.rs:L663-L711](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L663-L711) 是每个事件必经的输入法闸门。注意开头的借用舞蹈:由于 `filter_event` 需要 `&mut XimHandler`,而两者都存在 `RefCell` 里,先 `take_xim()` 取出、drop 状态借用、过滤、`restore_xim()` 放回:

```rust
let xim_filtered = ximc.filter_event(&event, &mut xim_handler);
let xim_callback_event = xim_handler.last_callback_event.take();
```

`filter_event` 返回 `Ok(handled)`:true 表示输入法吃掉了(比如组字中的按键),`continue` 跳过常规翻译;false 则视 `xim_handler.connected` 决定走 `xim_handle_event`(把按键转发给输入法服务器)还是 `handle_event`(直通)。返回 `Err` 意味着输入法服务器崩溃——代码注释坦承会丢 1-2 个键,但 X Server 后续会报 window not found 错误,于是 `take_xim()` 摘掉输入法、降级为直通,保证键盘不至于全哑([../gpui_linux/src/linux/x11/client.rs:L699-L709](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L699-L709))。

**(3) handle_event:分支总表**

`handle_event` 是一个约 620 行的巨型 `match`,把 `x11rb::protocol::Event` 分发到窗口或状态更新。全表如下(行号均为 `client.rs`):

| XCB 事件 | 分支位置 | 翻译产物 / 动作 |
| --- | --- | --- |
| `UnmapNotify` / `MapNotify` / `VisibilityNotify` | [L799-L819](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L799-L819) | 更新 `WindowRef` 的 `is_mapped`/`last_visibility`,`update_refresh_loop` 启停周期刷新 |
| `ClientMessage` | [L820-L907](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L820-L907) | `WM_DELETE_WINDOW` → 关窗;`_NET_WM_SYNC_REQUEST` → 记 sync counter;`Xdnd*` 四连 → 文件拖放状态机 |
| `SelectionNotify` | [L908-L946](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L908-L946) | XDnD 数据就绪 → 读 `XDND_DATA` 属性 → `FileDropEvent::Entered` |
| `ConfigureNotify` | [L947-L963](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L947-L963) | x/y/width/height → `Bounds` → `window.set_bounds()`(resize/移动) |
| `PropertyNotify` | [L964-L970](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L964-L970) | 交给窗口解析属性变化(如 `_GTK_FRAME_EXTENTS`) |
| `FocusIn` / `FocusOut` | [L971-L997](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L971-L997) | `set_active` + 记录键盘焦点窗口;Focus 时 `enable_ime`,失焦时清 compose、复位滚动、`reset_ime` |
| `XkbNewKeyboardNotify` / `XkbMapNotify` | [L998-L1016](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L998-L1016) | 从设备重建 xkb 状态 → `handle_keyboard_layout_change` |
| `XkbStateNotify` | [L1017-L1055](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L1017-L1055) | 更新 xkb 掩码 → 与上次快照比对,变了才发 `PlatformInput::ModifiersChanged` |
| `KeyPress` | [L1056-L1119](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L1056-L1119) | `PlatformInput::KeyDown`(含 compose 死键状态机,见下) |
| `KeyRelease` | [L1120-L1141](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L1120-L1141) | `PlatformInput::KeyUp` |
| `XinputButtonPress` | [L1142-L1218](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L1142-L1218) | 按钮 → `MouseDown`(双击计数);滚轮键 → `ScrollWheel`(模拟滚动需跳过 emulated 标志) |
| `XinputButtonRelease` | [L1219-L1243](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L1219-L1243) | `MouseUp` |
| `XinputMotion` | [L1244-L1304](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L1244-L1304) | `MouseMove` + 平滑滚动增量(`ScrollWheel`) |
| `XinputEnter` / `XinputLeave` | [L1305-L1334](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L1305-L1334) | `set_hovered(true/false)` + `MouseExited`;离窗时复位滚动基线 |
| `XinputHierarchy` | [L1335-L1350](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L1335-L1350) | 设备插拔 → 重建 `pointer_device_states`(滚动值失效) |
| `XinputDeviceChanged` | [L1351-L1356](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L1351-L1356) | 设备能力变化 → 复位该设备滚动基线 |
| `XinputGesturePinchBegin/Update/End` | [L1357-L1414](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L1357-L1414) | `PlatformInput::Pinch`,scale 为 FP16.16 定点(除以 65536) |

**(4) KeyPress 分支:compose 死键状态机**

这是 `handle_event` 里最精巧的分支([../gpui_linux/src/linux/x11/client.rs:L1056-L1119](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L1056-L1119))。流程:

1. `modifiers_from_state(event.state)`(event.rs 助手)拿修饰键,更新 `state.modifiers`;
2. `xkb_state_for_key_event(&state.xkb, event.state)` 构造一个**按键事件专属的 xkb 状态副本**(不污染服务器同步来的长期状态,辅助函数在 [../gpui_linux/src/linux/x11/client.rs:L2776-L2807](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L2776-L2807),文件末尾的一整组单元测试都在验证「不改变服务器状态、正确处理 CapsLock 锁定/Neo2 布局/宏延迟修饰键」等历史 bug);
3. `keystroke_from_xkb(...)`(u3-l3 讲过)翻译出 `Keystroke`;若 keysym 是纯修饰键直接返回,不产生事件;
4. 死键处理:`compose_state.feed(keysym)` 后按状态分三路——
   - `Composed`:死键序列完成(如 `'` + `a` → `á`),用 compose 结果覆写 `keystroke.key_char` 与 `key`;
   - `Composing`:正在组合中,清空 `key_char`,把当前可显示的组合前缀发给 `window.handle_ime_preedit`(界面上出现的那个提示字符串);
   - `Cancelled`:用户按了无法组合的键,先提交已有的 preedit,再开始新一轮;
5. 最终 `window.handle_input(PlatformInput::KeyDown(...))` 送入 GPUI 事件管线。

注意这条 compose 路径**不经过 XIM**——死键组合是 xkbcommon 在本地完成的,只有外部输入法服务器(fcitx/ibus)介入时才走 4.3 的 XIM 链路。

**(5) XinputButtonPress:归一化坐标与双击计数**

[../gpui_linux/src/linux/x11/client.rs:L1142-L1218](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L1142-L1218)。XInput 事件里的 `event_x`/`event_y` 是 **u16 归一化坐标**:0 到 65535 线性映射到窗口的整个宽高。换算成 GPUI 逻辑像素:

\[
x_{\text{logical}} \;=\; \frac{\text{event\_x}}{65535 \times \text{scale\_factor}}
\]

对应代码 `px(event.event_x as f32 / u16::MAX as f32 / state.scale_factor)`。双击检测是经典的时间+距离启发式:距上次点击小于 `DOUBLE_CLICK_INTERVAL`、同一按钮、且距离在 `is_within_click_distance` 之内,`current_count` 递增,否则归 1——`click_count` 就是 `MouseDownEvent` 里的那个「第几连击」。滚轮键(detail 4-7)翻译成 `ScrollWheel`,但带 `POINTER_EMULATED` 标志的要跳过:平滑滚动的触控板会同时发真实 `XinputMotion` 增量与模拟的按钮事件,两边只处理 motion 一边,否则会双倍滚动。

**(6) XinputMotion:valuator 位掩码解码**

鼠标移动与平滑滚动共用 `XinputMotion` 事件。位置换算同上;滚动增量的计算在 [../gpui_linux/src/linux/x11/client.rs:L1292-L1303](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L1292-L1303) 调 `get_scroll_delta_and_update_state`(定义于 [L2455-L2484](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L2455-L2484)):XInput 把滚动位置放在 **valuator 轴**里,事件只携带 `valuator_mask`(哪些轴有值)与 `axisvalues`(按轴号升序排列的值)。解码规则是 event.rs 的核心算法:

```rust
pub(crate) fn get_valuator_axis_index(
    valuator_mask: &Vec<u32>,
    valuator_number: u16,
) -> Option<usize> {
    if bit_is_set_in_vec(valuator_mask, valuator_number) {
        Some(popcount_upto_bit_index(valuator_mask, valuator_number) as usize)
    } else {
        None
    }
}
```

见 [../gpui_linux/src/linux/x11/event.rs:L68-L81](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/event.rs#L68-L81):若目标轴的位有值,它在 `axisvalues` 里的下标 = 掩码中**低于该位的 1 的个数**(`popcount_upto_bit_index`,[L83-L101](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/event.rs#L83-L101)),因为 `axisvalues` 从最低轴号排到最高。滚动**增量**是本轴当前值减上次值的差乘以换算系数 `multiplier`(初始化时按设备 scroll class 的 `increment` 算出 `SCROLL_LINES / increment`);差值基线(`scroll_value`)在窗口失焦、设备变更时主动置 `None` 失效——注释解释了取舍:失效最多丢一次滚动增量,不失效则可能产生一个巨大的假增量,后者用户可见得多([L157-L169](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L157-L169) 的 `ScrollAxisState` 文档)。

**(7) event.rs 的其余纯函数助手**

- `button_or_scroll_from_event_detail`([../gpui_linux/src/linux/x11/event.rs:L20-L33](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/event.rs#L20-L33)):X 按钮编号固定语义——1/2/3 是左/中/右,4-7 是四个方向的滚轮「键」,8/9 是前进/后退侧键。
- `modifiers_from_state`([L35-L43](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/event.rs#L35-L43)):把核心事件的 `KeyButMask` 翻成 `Modifiers`。注意 X11 的修饰键是**语义槽位**而非具体键:`MOD1` 惯例是 Alt、`MOD4` 惯例是 Super/Win,所以映射到 GPUI 的 `alt`/`platform` 位。
- `modifiers_from_xinput_info`([L45-L54](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/event.rs#L45-L54)):XInput 事件的 `ModifierInfo.effective` 位域版,掩码判等而不是位与判真(`bits & M == M`),语义相同。
- `pressed_button_from_mask`([L56-L66](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/event.rs#L56-L66)):从 button_mask 位域判断拖拽中按住的是哪个键。

#### 4.2.4 代码实践

**实践目标**:亲手做一次「原始 X11 事件 → GPUI 事件」的翻译对照,验证 4.2.3 的翻译表。

**操作步骤**:

1. 确保 X11 会话与工具:`sudo apt install x11-utils`(Debian/Ubuntu;`xev` 来自 x11-utils;若已装可跳过)。
2. 打开一个独立的 `xev` 观察窗,先采集「原始事件」样例:

   ```bash
   xev -event keyboard -event structure
   ```

   在弹出的白色小窗里:按一次 `a` 键;再用鼠标拖拽窗口右下角改变尺寸。终端会分别打出 `KeyPress`/`KeyRelease` 事件块(注意其中的 `keycode`、`state`、`time` 字段)和 `ConfigureNotify` 事件块(注意 `x`、`y`、`width`、`height` 字段)。
3. 再跑 GPUI 窗口,采集「翻译后」样例:

   ```bash
   WAYLAND_DISPLAY= cargo run -p gpui --example window
   ```

   拖拽窗口边缘 resize,终端打印 `Window bounds changed: Bounds { origin: Point { x: ..., y: ... }, size: Size { width: ..., height: ... } }`。
4. 填写对照表(见「预期结果」的模板)。
5. (可选进阶)把 `xev` 附着到 GPUI 窗口上直接对照:`xev -id "$(xdotool getactivewindow)"`,需要 `xdotool` 工具。

**需要观察的现象**:

- `xev` 里按住 `a` 不放,会看到重复的 `KeyRelease`+`KeyPress` 对,且同一对的时间差远小于 20ms——正是 4.2.3(1) 丢弃的对象。
- `xev` 的 `ConfigureNotify` 中 `width/height` 是物理像素;GPUI 打印的 `Window bounds changed` 是逻辑像素,两者比值即 `scale_factor`(未设置 `GPUI_X11_SCALE_FACTOR` 时通常为 1.0)。

**预期结果**(对照表模板,「xev 侧字段」列以实际输出为准):

| 原始 X11 事件 | xev 侧字段 | 翻译位置 | GPUI 侧产物 |
| --- | --- | --- | --- |
| `KeyPress`,keycode 38(即 `a`) | `state 0x0`,`time ...` | [client.rs:L1056-L1119](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L1056-L1119) + [event.rs:L35-L43](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/event.rs#L35-L43) | `PlatformInput::KeyDown`,`Keystroke { key: "a", key_char: Some("a"), modifiers: 默认 }` |
| 按住 `a` 的伴生 `KeyRelease` | 与下一 `KeyPress` 同 keycode、间隔 < 20ms | [client.rs:L611-L621](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L611-L621) | 被丢弃,不产生 `KeyUp` |
| `ConfigureNotify`(resize) | `x`,`y`,`width`,`height` | [client.rs:L947-L963](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L947-L963) | `window.set_bounds(Bounds{...})` → `observe_window_bounds` 打印 |
| `ButtonPress`,detail 1 | `state 0x100` 等 | [client.rs:L1142-L1194](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L1142-L1194) | `PlatformInput::MouseDown { button: Left, click_count: 1 }` |

**待本地验证**:keycode 具体数值依赖键盘与布局(xev 会同时打印 keysym 名,以它为准);`state` 掩码数值依修饰键而变。对照表中「GPUI 侧产物」一列在 X11 无输入法时会精确成立;若系统运行 fcitx/ibus 且设置了 `XMODIFIERS`,按键会先进入 4.3 的 XIM 链路,`KeyDown` 的产生时机与内容可能不同——这本身就是观察 XIM 闸门的好实验。

#### 4.2.5 小练习与答案

**练习 1**:为什么 `Expose` 事件不进 `events` 列表,而是塞进 `windows_to_refresh` 集合,等批次的其余事件处理完才统一 refresh?

**参考答案**:`Expose` 表示「窗口某块区域需要重画」。X11 在窗口露出、移动时可能连发多个 `Expose`(每块脏区域一个),若每个都触发一次重绘,一轮就会重绘 N 次。放进 `HashSet<xproto::Window>`(按窗口去重)后,一轮排空只对每个受影响窗口调用一次 `window.refresh(RequestFrameOptions { require_presentation: true, .. })`([../gpui_linux/src/linux/x11/client.rs:L713-L726](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L713-L726)),脏区域由 GPUI 内部合成器合并,天然批量化。

**练习 2**:`XkbStateNotify` 分支为什么要保存 `last_modifiers_changed_event` / `last_capslock_changed_event` 两个「上次事件快照」?

**参考答案**:服务器可能对同一个修饰键状态发多次通知(掩码更新与事件到达存在时序重叠),若每次都照发 `ModifiersChanged`,GPUI 上层会收到重复的修饰键事件,导致快捷键状态机误触发。该分支([../gpui_linux/src/linux/x11/client.rs:L1031-L1050](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L1031-L1050))先 `update_mask` 同步 xkb 状态,再与快照比对,只有真的变了才向焦点窗口投递 `PlatformInput::ModifiersChanged` 并更新快照。源码里 `// TODO: Can the other updates to modifiers be removed...` 的注释也表明这是对冗余通知的防御性去重。

**练习 3**:`process_x11_events` 的外层 `loop` 什么时候会转第二圈?

**参考答案**:处理本轮 `events` 的过程中又产生了新 X11 事件的场合。典型路径:翻译某事件时向服务器发了请求并 `flush`,而处理 `XinputHierarchy`(重查设备)、`XkbMapNotify`(重建 keymap)或窗口 `refresh` 后的排空调用([client.rs:L2004](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L2004))都可能让应答/事件进入队列。内层 `poll_for_event` 返回 `None` 且无待刷新窗口时,外层 `if events.is_empty() && windows_to_refresh.is_empty() { break; }`([L659-L661](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L659-L661))才真正退出。

### 4.3 XimHandler:输入法集成为何需要独立 handler 模块

#### 4.3.1 概念说明

XIM 的困难不在协议本身,而在**控制流的倒置**。GPUI 的事件处理是「我来泵事件、我来分发」;而 `xim` crate 的工作方式是:你把原始 XCB 事件交给 `ximc.filter_event(&event, &mut handler)`,库在**它自己的调用栈里**解析协议、必要时回调你 `ClientHandler` 上的方法(`handle_commit`、`handle_preedit_draw`、`handle_forward_event`……)。这意味着:

1. 回调发生时你正持有 `X11ClientState` 的借用(`filter_event` 需要同时拿到 `ximc` 与 `xim_handler`),回调里不可能再去借用 client 状态做窗口分发——**借用冲突是结构性的**。
2. `filter_event` 返回后,「这个事件被吃了吗」「输入法额外产生了什么」必须以**值**的形式带出来,而不是在回调里直接处理。

所以 `XimHandler` 被设计成一个极小的状态盒 + 一个出料口:

- 状态:`im_id`(输入法会话 id)、`ic_id`(输入上下文 id)、`connected`(握手完成与否)、`window`(当前焦点窗口);
- 出料口:`last_callback_event: Option<XimCallbackEvent>`——回调只把产物**暂存**进这个信封,由 `process_x11_events` 在借用释放后统一取出处理。

`XimCallbackEvent` 三种信封定义于 [../gpui_linux/src/linux/x11/xim_handler.rs:L6-L10](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/xim_handler.rs#L6-L10):

```rust
pub enum XimCallbackEvent {
    XimXEvent(x11rb::protocol::Event),   // 输入法发回的原始按键(直通上屏)
    XimPreeditEvent(xproto::Window, String), // 组字预编辑文本变化
    XimCommitEvent(xproto::Window, String),  // 组字完成,文本上屏
}
```

#### 4.3.2 核心流程

XIM 生命周期分两阶段。

**握手阶段**(由 `enable_ime` 与库回调链驱动,全部在 [../gpui_linux/src/linux/x11/xim_handler.rs:L33-L67](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/xim_handler.rs#L33-L67)):

```text
ximc 连接输入法服务器
  → handle_connect: client.open("C")                    # 打开输入法,声明 locale
    → handle_open(im_id): 查询 QueryInputStyle          # 记下 im_id
      → handle_get_im_values: build_ic_attributes()
          .push(InputStyle, PREEDIT_CALLBACKS)           # 要求"回调式预编辑"
          .push(ClientWindow, self.window)
          .push(FocusWindow, self.window)
          create_ic(im_id, attrs)
        → handle_create_ic(ic_id): connected = true      # 握手完成
```

注意每一步都是「发请求 → 服务器应答 → 库回调你下一步」,链式推进,这正是回调 trait 的形状。`PREEDIT_CALLBACKS` 输入风格表示:组字过程中的预编辑文本通过 `preedit_draw` 回调同步给客户端,由客户端自己画(而不是输入法用一个浮动窗口画)——GPUI 选择它以便把组字文本画进自己的编辑器 UI。

**运行阶段**(每个键盘事件):

```text
KeyPress 到达 process_x11_events
  → ximc.filter_event(event, &mut handler)
    输入法决定:
      a) 吃掉(组字中)             → Ok(true),GPUI 不再翻译
      b) 需要看到原始按键          → handle_forward_event 回调
                                      → 信封 XimXEvent(KeyPress)
      c) 组字有进展/完成            → handle_preedit_draw / handle_commit
                                      → 信封 XimPreeditEvent / XimCommitEvent
  → filter 返回后:
    取出信封 → handle_xim_callback_event 分发
    Ok(false) 且已连接 → xim_handle_event: forward_event 把按键发给输入法服务器
```

#### 4.3.3 源码精读

**(1) 三个业务回调**

- `handle_commit`([../gpui_linux/src/linux/x11/xim_handler.rs:L69-L81](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/xim_handler.rs#L69-L81)):组字完成(用户敲了回车/空格选定候选词),把最终文本装进 `XimCommitEvent` 信封。上屏动作在 client 侧的 `xim_handle_commit`([../gpui_linux/src/linux/x11/client.rs:L1466-L1476](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L1466-L1476)):清 `composing` 标志、`window.handle_ime_commit(text)`。
- `handle_forward_event`([../gpui_linux/src/linux/x11/xim_handler.rs:L83-L102](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/xim_handler.rs#L83-L102)):输入法不拦截、把按键原样发回,客户端按普通按键处理——只认 KeyPress/KeyRelease 两种 response_type,包成 `XimXEvent` 信封,最终仍走 `handle_event` 翻译。
- `handle_preedit_draw`([../gpui_linux/src/linux/x11/xim_handler.rs:L108-L132](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/xim_handler.rs#L108-L132)):组字文本变化,装 `XimPreeditEvent` 信封。函数开头的大段注释列出了 XIM 反馈掩码(选中/下划线/高亮)的语义并说明「目前无法支持这些样式」——只取纯文本。

**(2) client 侧的对接**

`xim_handle_event`([../gpui_linux/src/linux/x11/client.rs:L1435-L1464](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L1435-L1464)):对 KeyPress/KeyRelease,先记 `pre_key_char_down`(输入法可能改写字符的对照基线),再 `ximc.forward_event(...)` 把原始事件发给输入法服务器;其它事件直通 `handle_event`。`xim_handle_preedit`([L1478-L1516](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L1478-L1516))除了把文本交给 `window.handle_ime_preedit`,还会按窗口报告的 IME 区域重建 `SpotLocation`(光标位置)属性——组字候选窗要跟着光标走。`enable_ime`/`reset_ime`([L731-L786](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L731-L786))分别由 `FocusIn`/`FocusOut` 事件驱动(见 4.2.3 分支表):得焦时(重新)创建输入上下文,失焦时 `reset_ic` 并清组字状态。

**(3) 借用舞蹈的封装**

`take_xim`/`restore_xim`([../gpui_linux/src/linux/x11/client.rs:L1878-L1896](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L1878-L1896))是这对操作的原语:取走 `(ximc, xim_handler)` 两个 Option、用完原样放回。4.2.3(2) 的 `filter_event`、`enable_ime`、`xim_handle_preedit`、`X11ClientStatePtr::update_ime_position`([L260-L302](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L260-L302))全都遵循同一套「取出 → drop 状态借用 → 操作 → 放回」的舞步。取走失败(其中一个 Option 已是 None)时会把已取出的那个放回去并打日志,保证状态不半空。

#### 4.3.4 代码实践

**实践目标**:搞清你的机器上 XIM 链路是否激活,并跟踪一次组字(或直通)的完整事件旅程。

**操作步骤**(源码阅读型 + 可选运行验证):

1. 查环境:`echo $XMODIFIERS $GTK_IM_MODULE $QT_IM_MODULE`。`XMODIFIERS` 形如 `@im=fcitx` 或 `@im=ibus` 时,`xim` crate 才可能连上输入法服务器;为空通常意味着 `X11rbClient::init` 失败,`ximc == None`,整条链路直通。
2. 画序列图:对照 4.3.2 的两个阶段,把 `XimHandler` 的五个回调(`handle_connect`/`handle_open`/`handle_get_im_values`/`handle_create_ic` 与运行期的 `handle_forward_event`/`handle_commit`/`handle_preedit_draw`)与 client 侧的四个函数(`enable_ime`/`xim_handle_event`/`xim_handle_commit`/`xim_handle_preedit`)画进一张时序图,标注每条消息经过的文件与行号。
3. (可选,需中文输入法)在 X11 会话下运行 4.2.4 的 window 示例,切到中文输入法,在窗口里键入 `nihao`:观察候选窗跟随(或不跟随)光标、确认上屏后 GPUI 无崩溃;再按 Esc 取消组字,对照 `handle_preedit_draw` 注释思考哪些视觉反馈缺失了。
4. 断链实验:对比运行 `XMODIFIERS= WAYLAND_DISPLAY= cargo run -p gpui --example window` 与不带 `XMODIFIERS=` 的两次,按键行为应完全一致(都直通)——验证 4.2.3(2) 的降级路径。

**需要观察的现象**:步骤 3 中,组字期间输入的字母不应出现在 GPUI 界面上(被 `filter_event` 吃掉);上屏瞬间整段中文文本一次性出现(`XimCommitEvent`)。候选窗位置由输入法服务器自己决定,若不跟光标,是因为该输入法未用 `SpotLocation`。

**预期结果**:步骤 1/2/4 是确定性的源码事实;步骤 3 的具体表现依赖所装输入法,「待本地验证」。若你的发行版默认 Wayland,请在登录界面选择 X11 会话,否则 `XMODIFIERS` 检查无意义。

#### 4.3.5 小练习与答案

**练习 1**:为什么 `XimHandler.window` 要在 `FocusIn` 事件里手动更新([client.rs:L976-L978](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L976-L978)),而不是每次用时现查 `keyboard_focused_window`?

**参考答案**:XIM 的输入上下文在创建时就绑定了 `ClientWindow`/`FocusWindow` 属性,组字回调携带的窗口号也来自会话建立时的绑定。`FocusIn` 时同步 `handler.window = event.event` 并随即 `enable_ime()` 重建输入上下文,保证「信封里的窗口号」与「真正的焦点窗口」一致;若回调发生时才现查焦点,组字途中焦点可能已变,文本会送到错误的窗口。

**练习 2**:`filter_event` 返回 `Err` 时为什么选择**永久摘除** XIM(`state.take_xim()`),而不是重试?

**参考答案**:错误几乎总意味着输入法服务器进程崩溃。XIM 协议没有会话恢复机制,旧 `im_id`/`ic_id` 已失效,重试只会连着失败;注释指出后续按键会触发 X Server 的 window not found 错误,这本身就是崩溃的信号。摘除后 `has_xim()` 为假,所有事件直通 `handle_event`,键盘立即可用——「丢 1-2 个键换整条键盘链路存活」是明确的取舍([../gpui_linux/src/linux/x11/client.rs:L699-L709](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L699-L709))。重启应用才能重新连上输入法,这是当前实现的已知局限。

**练习 3**:`composing` 布尔与 `pre_edit_text: Option<String>` 分别属于哪一层?为什么 compose 死键(4.2.3(4))也写 `pre_edit_text` 却不碰 `composing`?

**参考答案**:两者都是 `X11ClientState` 的字段,但服务的对象不同:`composing` 专指 **XIM 组字进行中**(由 `xim_handle_preedit` 按文本是否为空设置、`xim_handle_commit`/`reset_ime` 清除),它被 `XinputButtonPress` 用来决定「点击时先提交还是取消组字」,以及被 `update_ime_position` 用来在组字中避免频繁挪 `SpotLocation`;`pre_edit_text` 是「当前预编辑内容」,XIM 与本地 compose 死键两条链都会写它(死键路径见 [client.rs:L1084-L1094](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L1084-L1094))。死键不需要 `composing` 是因为它的「取消」语义由 compose 状态机自己的 `Cancelled` 分支处理,且不存在与输入法服务器交互。

### 4.4 x11::clipboard:剪贴板、主选区与服务线程

#### 4.4.1 概念说明

X11 剪贴板三个反直觉的真相,构成这个模块全部复杂度的来源:

1. **数据住在拥有者进程里**。复制不是把字节交给系统,而是声明所有权(`set_selection_owner`);真正的数据传输发生在别人来取时——请求者发 `convert_selection` 请求,拥有者把数据写进**请求者窗口的一个属性**里,再发 `SelectionNotify` 通知。没有拥有者应答,数据就取不到;拥有者退出,数据就没了(所以要有 clipboard manager 移交)。
2. **拥有者必须 pump 事件**。应答 `SelectionRequest` 是事件驱动的,拥有者进程需要一个活着的连接和事件循环。GPUI 的主连接忙于渲染不适合干这个,所以 clipboard 模块自建了一条**独立的 `RustConnection` 与一个 1×1 的隐藏窗口**,专事应答。
3. **大数据要增量传输**。属性单次写入有尺寸上限,大数据(图片)走 INCR 协议:拥有者先在属性里放个 `INCR` 原子和总长度,然后双方用 `PropertyNotify` 你一段我一段地接力。

该文件移植自开源剪贴板库 arboard(文件头版权注释写明),结构上是「一个进程级单例 + 两条连接」:

- `CLIPBOARD: Mutex<Option<GlobalClipboard>>` 全局单例([../gpui_linux/src/linux/x11/clipboard.rs:L55](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/clipboard.rs#L55));
- `Inner` 持有 `server: XContext`(常驻服务连接)与三个 `Selection`(CLIPBOARD/PRIMARY/SECONDARY 的数据槽);
- 服务线程 `serve_requests` 阻塞在 `wait_for_event` 上应答世界;
- 读操作每次新建一条临时 `XContext`(读是「请求者」角色,用完即弃)。

#### 4.4.2 核心流程

**写**(以 `set_text` → `Inner::write` 为例,[../gpui_linux/src/linux/x11/clipboard.rs:L230-L280](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/clipboard.rs#L230-L280)):

```text
set_selection_owner(server_win, atom(kind), CURRENT_TIME)   # 声明所有权
flush
selection.data = [ClipboardData { bytes, format: UTF8_STRING }]  # 数据进自己内存
notify_all(data_changed)                                    # 唤醒可能等待的写入者
(此后所有 SelectionRequest 由服务线程异步应答)
```

**读**(`Inner::read` → `read_single`,[L285-L364](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/clipboard.rs#L285-L364) 与 [L373-L464](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/clipboard.rs#L373-L464)):

```text
is_owner? 是 → 直接读自己内存(快路径,保留 metadata)
否:
  新建临时 XContext(reader)
  convert_selection(target = TARGETS)        # 先问拥有者"你有哪些格式"
    → SelectionNotify → 解析格式列表,挑出我方支持且最优的
  convert_selection(target = 选中的格式)      # 再要真数据
    → SelectionNotify → get_property 读属性
      属性类型 == INCR?
        是 → 进入 PropertyNotify 接力循环,拼到 value_len == 0 为止
        否 → 一次拿全
  超时保护:LONG_TIMEOUT 4s(图像慢),INCR 每段刷新 SHORT_TIMEOUT 10ms
```

**服务线程**([../gpui_linux/src/linux/x11/clipboard.rs:L834-L946](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/clipboard.rs#L834-L946))事件循环处理三种事件:`SelectionRequest`(别人来取 → `handle_selection_request` 把数据写进对方属性并回 `SelectionNotify`)、`SelectionClear`(别人夺走了所有权 → 清自己的数据槽)、`DestroyNotify`(服务窗口被毁 → 线程收尾)。

#### 4.4.3 源码精读

**(1) 三个选区与格式清单**

`ClipboardKind` 三成员的注释说清了语义分工([../gpui_linux/src/linux/x11/clipboard.rs:L1148-L1160](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/clipboard.rs#L1148-L1160)):`Clipboard` 是显式复制粘贴(Ctrl+C/V)、`Primary` 是鼠标选区(选中即写、中键粘贴)、`Secondary` 罕见。`atom_manager!` 宏([L57-L96](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/clipboard.rs#L57-L96))批量 intern 了协议原子与全部支持的格式原子:文本六种(UTF8_STRING、两种 MIME 写法、STRING/Latin-1、TEXT、未知 text/plain)+ 九种图片 MIME(PNG/JPEG/WebP/GIF/SVG/BMP/TIFF/ICO/PNM)。`get_any`([L1022-L1069](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/clipboard.rs#L1022-L1069))读时把图片格式排在文本前面——「更具体者优先」,文本是万能兜底;`STRING`(Latin-1)按单字节扩展成 char,其余按 UTF-8 解码,分别包成 `ClipboardItem::new_image` / `new_string`。

**(2) 服务窗口与生命周期**

`XContext::new`([../gpui_linux/src/linux/x11/clipboard.rs:L142-L183](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/clipboard.rs#L142-L183))每次建一条新连接和一个 1×1、属性从父窗口拷贝的不可见窗口,只订阅 `PROPERTY_CHANGE | STRUCTURE_NOTIFY` 两个事件掩码——前者为了 INCR 接力与「删除属性以确认请求送达」的技巧,后者为了收到 `DestroyNotify` 知道何时收摊。`Clipboard::new`([L952-L978](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/clipboard.rs#L952-L978))用全局 `Mutex` 保证进程内单例:已存在就共享 `Arc<Inner>`,否则创建并 spawn 名为 `"Clipboard"` 的服务线程。

**(3) TARGETS 协商与 INCR 接力**

`read_single` 的两阶段在 4.4.2 已概述,细节值得注意三点:

- 请求前先 `delete_property` 把自己的 `ARBOARD_CLIPBOARD` 属性清掉([L379-L384](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/clipboard.rs#L379-L384)):拥有者写完数据后属性会触发 `PropertyNotify`,删除是为了能区分「新数据到了」;`convert_selection` 之后紧跟 `sync()` 而不是 flush——sync 会阻塞等待服务器处理完已发请求,确保后续 poll 能收到回应。
- `handle_read_selection_notify`([L534-L616](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/clipboard.rs#L534-L616))发现属性类型是 `INCR` 原子时,再次 `get_property`(带删除语义,表示「我准备好接下一段了」),预分配 `min_data_len` 容量,进入增量模式。
- `handle_read_property_notify`([L618-L666](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/clipboard.rs#L618-L666)):每收到一段 `PropertyNotify(NEW_VALUE)` 就 get_property 追加,`value_len == 0` 的空段是传输结束标志;每段刷新 10ms 短超时,避免长传输被总超时误杀。

**(4) client.rs 侧的四个契约方法**

`LinuxClient for X11Client` 的剪贴板四件套直接映射到 `ClipboardKind`([../gpui_linux/src/linux/x11/client.rs:L1742-L1793](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L1742-L1793)):`write_to_primary`/`read_from_primary` 用 `ClipboardKind::Primary`,`write_to_clipboard`/`read_from_clipboard` 用 `ClipboardKind::Clipboard`。特别注意 `read_from_clipboard` 的快路径:

```rust
// if the last copy was from this app, return our cached item
// which has metadata attached.
if state.clipboard.is_owner(clipboard::ClipboardKind::Clipboard) {
    return state.clipboard_item.clone();
}
```

若本应用就是当前拥有者,直接返回 `clipboard_item` 缓存(写入时 `write_to_clipboard` 存的原始 `ClipboardItem`,[L1766](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L1766))。原因在 u2-l4 讲过:X11 的属性传输只搬字节,`ClipboardItem` 里的元数据(文本哈希,用于保留语法高亮)过不了协议——只有自己读自己时才保得住。

**(5) 退出移交**

`Drop for Clipboard`([../gpui_linux/src/linux/x11/clipboard.rs:L1076-L1133](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/clipboard.rs#L1076-L1133)):当强引用计数降到「全局单例 + 服务线程 + 自己」三个最小值时,调用 `ask_clipboard_manager_to_request_our_data`([L770-L831](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/clipboard.rs#L770-L831)):向 `CLIPBOARD_MANAGER` 选区发 `SAVE_TARGETS` 转换请求,请 clipboard manager(如 clippy/klipper/GNOME 的守护进程)把数据要过去替我们保管,最多等 100ms;然后销毁服务窗口、join 服务线程。这样应用退出后用户还能粘贴最后复制的内容——这是 X11 剪贴板模型「数据在拥有者进程里」的必然补丁。

#### 4.4.4 代码实践

**实践目标**:用系统工具直观验证「三个选区」与「拥有者应答」模型,并把观察与源码对上。

**操作步骤**:

1. 安装工具:`sudo apt install xclip`(Debian/Ubuntu)。
2. 在 X11 会话下运行任意 GPUI/Zed 窗口(可复用 4.2.4 的示例),在里面用鼠标选中一段文本(不按 Ctrl+C)。
3. 在终端分别读取两个选区:

   ```bash
   xclip -selection primary -o     # 主选区:选中即写入
   xclip -selection clipboard -o   # CLIPBOARD:只有显式复制才有内容
   ```

4. 在 GPUI/Zed 里按 Ctrl+C,再执行第 3 步的两条命令。
5. 观察「拥有者应答」:保持 GPUI 程序开着复制一段文字,然后在终端执行 `xclip -selection clipboard -o | head -c 20`;接着**关闭 GPUI 程序**,再执行一次同样的读取(如果你的桌面有 clipboard manager,内容仍在;在纯 WM 无 manager 的环境里会读不到)。

**需要观察的现象**:

- 第 3 步:primary 输出选中文本,clipboard 为空(xclip 报 `Error: target STRING not available` 或类似)。
- 第 4 步:两个选区都有内容(取决于应用是否同时写两处)。
- 第 5 步:有无 clipboard manager 行为不同——这正是 `Drop` 移交逻辑([clipboard.rs:L1076-L1133](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/clipboard.rs#L1076-L1133))存在的理由。

**预期结果**:第 3、4 步在标准 X11 环境是确定的协议行为;第 5 步依赖桌面环境是否跑着 clipboard manager,「待本地验证」。另可用 `xprop -root | grep -i clip` 或 `xclip -selection clipboard -o -t TARGETS` 列出当前拥有者支持的所有格式,对照 4.4.3(1) 的格式清单。

#### 4.4.5 小练习与答案

**练习 1**:`Inner::read` 为什么要先 `convert_selection(target = TARGETS)` 问一遍格式列表,而不是直接按自己想要的格式挨个试?

**参考答案**:直接试也能成功(代码里确实保留了逐格式试探的兜底分支,[../gpui_linux/src/linux/x11/clipboard.rs:L338-L363](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/clipboard.rs#L338-L363)),但每次试探都是一轮完整的「convert → 等 SelectionNotify → get_property」往返,还有超时风险(拥有者不认这个格式时 property 为 NONE)。TARGETS 一次往返拿到拥有者的全部格式,客户端按**自己的优先级**(图片在前、文本在后)选出双方都支持的最优格式,只需再一轮往返拿数据——把 O(格式数) 次协商压成 2 次。

**练习 2**:服务线程与读操作用的是**不同的连接**,为什么不会出现「两个连接都想当 CLIPBOARD 拥有者」的冲突?

**参考答案**:只有 `Inner::write` 走 `server` 连接执行 `set_selection_owner(server_win, ...)`,所有权落在服务窗口 `server.win_id` 上([clipboard.rs:L242-L249](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/clipboard.rs#L242-L249));读操作用的临时 `reader` 连接从不声明所有权,只发 `convert_selection` 当请求者。`is_owner` 也只比对 `server.win_id`。X11 服务器按窗口(而非按客户端连接)记录拥有者,所以即便同进程两条连接,角色也是清晰分离的:server 连接是「拥有者代言人」,reader 连接是「取件人」。

**练习 3**:`WaitConfig::Forever`/`Until` 两个变体为什么标着 `#[allow(unused)]`?

**参考答案**:`write` 的 `wait` 参数控制「写入后是否阻塞等待所有权被夺走」——arboard 原始用途是「程序退出前复制内容并等到别的程序接管才退出」。GPUI 调用处全部传 `WaitConfig::None`(见 [client.rs:L1742-L1767](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L1742-L1767) 两处 `clipboard::WaitConfig::None`),注释说明「现在不在应用关闭时等待剪贴板同步,但将来可能要」——退出同步改由 `Drop` 里的 clipboard manager 移交承担,等待变体因此保留但未使用。

## 5. 综合实践:一张完整的「X11 → GPUI」事件翻译对照表

把本讲四个模块串起来的最好方式,是亲手产出一份覆盖**输入、窗口、剪贴板**三类操作的翻译对照表。任务:

1. **准备**:X11 会话(或 `WAYLAND_DISPLAY=` 强制),安装 `x11-utils`(xev)与 `xclip`;克隆 zed 仓库。
2. **运行**:`WAYLAND_DISPLAY= cargo run -p gpui --example window`。
3. **采集原始事件**(三组):
   - 键盘:另开终端跑 `xev -event keyboard`,按 `a`、按住 `a`、按 `Ctrl+a`,记录 keycode/state/time;
   - 窗口:拖拽 GPUI 示例窗口边缘与标题栏,终端的 `Window bounds changed: ...` 打印即是 `ConfigureNotify` 翻译后的结果;用 `xev -event structure` 对照原始事件;
   - 剪贴板:在示例窗口外的编辑器里选中文字,`xclip -selection primary -o`;复制后 `xclip -selection clipboard -o`。
4. **填表**:为每个操作写一行「原始 X11 事件(关键字段) → 处理位置(client.rs/event.rs 文件与行号) → GPUI 产物(PlatformInput 变体或动作)」,翻译依据以 4.2.3 的分支总表为索引,逐行回到源码核实。
5. **验证排空机制**(阅读 + 推理):指出 `process_x11_events` 的全部三个调用点(4.1.3(5)),并回答:假如删掉 `insert_idle` 回调里那行排空,首开窗口为什么仍可能正常、但**偶发**空白帧?(提示:fd 源在 socket 又来数据时仍会触发;空白只出现在「事件已进 x11rb 内部队列而 socket 已空、且当时恰有一串前台 runnable 在跑」的时序里。)

**预期产出**:一份不少于 8 行的对照表 + 一段对排空机制时序的文字解释。所有「GPUI 产物」列都能在源码中找到对应行号;凡是依赖本机环境(keycode、输入法、剪贴板管理器)的观察项,如与预期不符,把差异记录下来——那通常正是 X11 生态多样性的证据。

## 6. 本讲小结

- `X11Client` 是一个 `Rc<RefCell<X11ClientState>>` 大状态机:`new()` 按固定流水线完成 calloop 循环、XCB 连接、扩展协商(XInput 2.4 定手势、XKB 定键盘、RandR/DRI3 定缩放与 GPU)、原子 intern、合成器与 CSD 探测、XIM 与剪贴板初始化。
- **calloop 只监听 socket、看不见 x11rb 内部队列**。`f4178619ac` 之后,前台 runnable 的 `insert_idle` 回调在 `runnable.run()` 之后主动调用 `process_x11_events` 排空内部队列(fd 可读与每帧刷新是另外两个排空点),修复了 X11 首开窗口停留在空白帧的问题。
- 事件翻译分两层:`x11/event.rs` 是无状态纯函数助手(按钮编号、modifier 位域、valuator 位掩码→轴下标,可单测);`process_x11_events` + `handle_event` 是有状态编排(键盘 20ms 自动重复去重、Expose 按窗口合并、修饰键变更快照去重、compose 死键状态机)。
- XIM 的控制流是倒置的(库在 `filter_event` 的调用栈里回调你),所以需要独立的 `XimHandler`:`last_callback_event` 信封暂存回调产物,`take_xim`/`restore_xim` 借用舞蹈绕开 `RefCell` 冲突;输入法崩溃时摘除会话、事件直通降级。
- X11 剪贴板是「数据在拥有者进程里」的所有权模型:写入即声明所有权,服务线程用独立连接与 1×1 隐藏窗口应答 `SelectionRequest`;读取是新开连接做 `TARGETS` 协商 + `convert_selection` + INCR 增量接力;主选区(`PRIMARY`)与 `CLIPBOARD` 是两个平行选区;自读自写时走内存快路径以保留 `ClipboardItem` 元数据;退出时向 clipboard manager `SAVE_TARGETS` 移交。

## 7. 下一步学习建议

本讲跑完了 X11 后端的完整纵深。下一讲 **u5-l4「Wayland 客户端:协议对象、layer_shell 与弹出层」**是天然的对照组:把本讲的每个主题在 Wayland 侧再问一遍——没有全局坐标的窗口如何定位(对比 `ConfigureNotify`)、`wl_seat` 的 serial 为何要专门跟踪(对比 XIM 的窗口绑定)、数据源/数据Offer 模型如何取代选区所有权(对比本讲的 SelectionRequest/INCR)、layer_shell 如何让客户端自绘标题栏(对比 `_GTK_FRAME_EXTENTS` 协商)。建议带着 4.2.3 的翻译总表去读 `wayland/client.rs`,逐行找到每个 X11 概念的 Wayland 对位物。之后 u5-l5 的 xdg-desktop-portal 会补上 X11/Wayland 共用的文件对话框链路;若对键盘翻译意犹未尽,可回读 `../gpui_linux/src/linux/keyboard.rs` 中 `keystroke_from_xkb` 与本讲 `xkb_state_for_key_event` 的配合(以及 client.rs 文件末尾那一整组针对真实 issue 的键盘状态单元测试,它们是理解 X11 键盘边界的最佳索引)。

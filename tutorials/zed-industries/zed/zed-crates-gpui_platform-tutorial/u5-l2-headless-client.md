# u5-l2 headless 客户端：无显示环境下的平台实现

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `gpui_platform::headless()` 从门面到 `HeadlessClient` 的完整调用路径，以及另一条经由 `ZED_HEADLESS` 环境变量进入 headless 的隐式路径。
2. 把 `HeadlessClient` 对 `LinuxClient` 契约的实现方法按「空操作、返回 None、返回假数据、显式报错、真实工作」五类归类，并解释每一类背后的工程理由。
3. 解释 headless 窗口为什么「存在但不显示」：首帧由谁强制绘制、后续帧为什么没有驱动者、`cx.notify()` 为什么不会触发重绘。
4. 理解 headless 后端的工程意义：CI、远程服务器与 `ZED_HEADLESS` 场景下如何驱动真实的 GPUI 布局、文本整形与实体逻辑。

## 2. 前置知识

### 什么是「无头（headless）运行」

普通图形程序依赖一个「显示服务器」（Linux 上是 Wayland 合成器或 X Server）才能创建窗口、提交帧。但很多场景根本没有显示服务器：

- CI 容器里跑 UI 相关的自动化测试；
- 远程开发机上只做计算、不做展示；
- 命令行工具想复用一套基于 GPUI `Window` 的渲染/布局代码，却不需要真的画出任何东西。

「无头」指的是：**程序照常执行窗口、布局、实体这些逻辑，只是所有与显示服务器的交互都被替换成空操作或假数据**。这样同一份代码在有屏和无屏环境下都能编译运行。

### 本讲要承接的前置认知

- **u5-l1**：`LinuxPlatform<P>` 是外壳，真正的后端是实现 `LinuxClient` 契约的三选一：Wayland、X11、headless。本讲深入第三个后端。
- **u4-l3**：`LinuxDispatcher` 基于 calloop；`LinuxCommon::new` 会返回 `main_receiver`（优先级任务通道）与 `wake_receiver`（系统唤醒通道），由**拥有主事件循环的一方**注册进 calloop。Wayland/X11 客户端各自拥有主循环；headless 客户端也拥有一个——只是它的事件循环里除任务外没有任何窗口系统事件。
- **u1-l2**：`App::open_window` 在返回前会强制绘制第一帧；`Application::run` 把启动回调交给平台事件循环并阻塞。

### calloop 速览

calloop 是一个事件循环库，核心三件套：`EventLoop::try_new()` 创建循环、`handle().insert_source(源, 回调)` 注册事件源、`event_loop.run(None, &mut 数据, |_| {})` 阻塞运行直到 `LoopSignal::stop()` 被调用（`None` 表示无限期阻塞，只被事件唤醒）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/gpui_platform.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/src/gpui_platform.rs) | 门面 crate：`headless()`、`current_platform()` 入口 |
| [../gpui_linux/src/linux.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux.rs) | 第二层分发：按 headless 参数与 `guess_compositor()` 选后端 |
| [../gpui_linux/src/linux/headless.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/headless.rs) | headless 模块声明，仅导出 client 子模块 |
| [../gpui_linux/src/linux/headless/client.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/headless/client.rs) | `HeadlessClient`：`LinuxClient` 契约的无头实现与本模块的事件循环 |
| [../gpui_linux/src/linux/headless/window.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/headless/window.rs) | `HeadlessDisplay`（假屏）、`HeadlessWindow`（无合成器窗口）、`HeadlessAtlas`（只分配不上传的图集） |
| [../gpui_linux/src/linux/platform.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/platform.rs) | `LinuxClient` 契约、`LinuxCommon` 公共状态、`LinuxPlatform` 外壳（u5-l1 已精读，本讲引用片段） |
| [../gpui/src/platform.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/platform.rs) | `guess_compositor()` 环境探测；`PlatformWindow::frame_waker` 默认实现 |
| [../gpui/src/app.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app.rs) | `App::open_window` 的「返回前至少绘制一帧」 |
| [../gpui/src/platform/test/window.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/platform/test/window.rs) | `TestWindow`，headless 窗口的「同门参照物」 |

## 4. 核心概念与源码讲解

### 4.1 gpui_platform::headless()：入口与两条通往 headless 的路径

#### 4.1.1 概念说明

`gpui_platform::headless()` 是公开文档化的无头入口。它做的事情少到只有一行：用 `current_platform(true)` 构造平台，注入 `Application`。注意「headless」在这里是一个**布尔参数**，沿两层分发一路传下去。

但进入 headless 后端其实有两条路：

1. **显式路径**：调用方直接调 `headless()`（即 `current_platform(true)`），第二层分发看到 `headless == true` 直接短路。
2. **隐式路径**：调用方正常调 `application()`（`current_platform(false)`），但运行环境中没有 Wayland/X11（或设置了 `ZED_HEADLESS`），`guess_compositor()` 探测失败，match 落到 `"Headless"` 兜底分支。

这两条路最终构造的是完全相同的 `LinuxPlatform { inner: HeadlessClient::new() }`。

#### 4.1.2 核心流程

```text
显式路径:
  gpui_platform::headless()
    → current_platform(true)                    # 门面层 #[cfg] 分发
      → gpui_linux::current_platform(true)      # Linux/FreeBSD 分支
        → headless 参数为 true → 短路
          → LinuxPlatform { inner: HeadlessClient::new() }
            → Application::with_platform(Rc<dyn Platform>)

隐式路径:
  gpui_platform::application()
    → current_platform(false)
      → gpui_linux::current_platform(false)
        → gpui::guess_compositor()
          → ZED_HEADLESS 已设置?          → "Headless"
          → WAYLAND_DISPLAY 非空且开 feature? → "Wayland"
          → DISPLAY 非空且开 feature?       → "X11"
          → 都不满足                      → "Headless"（兜底）
        → match 到 "Headless" 分支
          → LinuxPlatform { inner: HeadlessClient::new() }
```

#### 4.1.3 源码精读

门面入口，一行核心逻辑：

- [src/gpui_platform.rs:L23-L25](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/src/gpui_platform.rs#L23-L25) —— `headless()` 把 `true` 传给 `current_platform`，再用 `Application::with_platform` 注入。与 `application()`（L13-L21）的唯一区别就是这个布尔值。

门面层的 `current_platform` 在 Linux/FreeBSD 目标上只是转发：

- [src/gpui_platform.rs:L71-L74](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/src/gpui_platform.rs#L71-L74) —— 第二层分发的入口。u1-l4 已精读过四组 `#[cfg]` 分支，这里只关注 Linux 分支。

第二层分发中 headless 参数的短路优先级最高，甚至先于环境探测：

- [../gpui_linux/src/linux.rs:L34-L38](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux.rs#L34-L38) —— `if headless` 直接返回 `LinuxPlatform { inner: HeadlessClient::new() }`，完全不读环境变量。也就是说显式 `headless()` 即使在图形工作站上也会得到无头后端。
- [../gpui_linux/src/linux.rs:L53-L55](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux.rs#L53-L55) —— 隐式路径的落点：`guess_compositor()` 返回 `"Headless"` 时走同一个构造。两条路在此汇合。

环境探测逻辑在 gpui 主 crate：

- [../gpui/src/platform.rs:L96-L123](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/platform.rs#L96-L123) —— `guess_compositor()`。L99-L101：`ZED_HEADLESS` 只要**存在**（哪怕是空字符串）就返回 `"Headless"`；L113-L122：`WAYLAND_DISPLAY`/`DISPLAY` 必须非空且对应 feature 已启用，优先级 Wayland > X11 > 兜底 headless。注意 L103-L111 的 feature 门控：如果编译时没开 `wayland`/`x11` feature，对应环境变量根本不会被读取——**零 feature 的 gpui_linux 永远落入 headless**。

一个容易被忽略的细节：门面 crate 还有一个「隐藏的 headless 消费者」：

- [src/gpui_platform.rs:L9-L11](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/src/gpui_platform.rs#L9-L11) —— `background_executor()` 用 `current_platform(true)`（即 headless 平台） merely 为了拿到一个后台执行器。无头平台在这里被当作「最便宜的可构造平台」使用。

#### 4.1.4 代码实践

1. **实践目标**：验证两条路径确实到达同一后端。
2. **操作步骤**：
   - 在 Linux 机器上写一个小程序（可复用综合实践的工程骨架），把入口换成 `gpui_platform::application()`，在 `run` 回调里打印 `cx.compositor_name()`。
   - 第一次在有图形会话的环境变量下运行，记录输出。
   - 第二次加前缀运行：`ZED_HEADLESS=1 cargo run`，再记录输出。
   - 第三次清空显示变量运行：`env -u WAYLAND_DISPLAY -u DISPLAY cargo run`，记录输出。
3. **需要观察的现象**：`compositor_name()` 的返回值在三次运行间如何变化。
4. **预期结果**：分别为真实合成器名（如 `"Wayland"`）、`"headless"`、`"headless"`（第三种情况依赖你启用了 wayland/x11 feature；若未启用任何 feature，三种情况都是 `"headless"`）。返回值来源见 4.2.3 中 `compositor_name` 的实现。具体输出待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `headless()` 的短路判断写在 `guess_compositor()` 之前？如果调换顺序会出什么问题？

**答案**：`guess_compositor()` 只看环境变量，不知道调用方的意图。如果先探测，那么一台设置了 `WAYLAND_DISPLAY` 的机器上 `headless()` 会意外连上真合成器，调用方「我要无头」的显式语义就被环境覆盖了。先短路保证显式参数永远优先于隐式环境。

**练习 2**：`ZED_HEADLESS=""`（设了空字符串）会让程序进入 headless 吗？

**答案**：会。[../gpui/src/platform.rs:L99-L101](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/platform.rs#L99-L101) 用的是 `var_os(...).is_some()`，只判断存在性；与之对照，`WAYLAND_DISPLAY` 的判断是 `is_some_and(|display| !display.is_empty())`，空字符串不算数。两个环境变量的判空策略是刻意不同的。

### 4.2 HeadlessClient：把 LinuxClient 契约「最小化」的分类学

#### 4.2.1 概念说明

u5-l1 已经建立：`LinuxClient` 是 gpui_linux 内部的后端契约（crate 私有 trait），`LinuxPlatform` 外壳把 `Platform` trait 的方法分发到后端。Wayland/X11 后端各自有几百上千行真实系统交互；`HeadlessClient` 全文不到 150 行，它是理解「契约最小实现长什么样」的最佳教材。

关键认知：**契约里这些方法大多是必需方法（没有默认实现），所以 headless 必须「写出空体」，而不是「可以不写」**。留空不是偷懒，而是把「无显示环境下此能力不存在」这一事实编码进类型系统——调用方拿到 `None` 后自行降级。

对 `LinuxClient` 的实现可以分成五类姿态：

| 类别 | 代表方法 | 行为 | 理由 |
| --- | --- | --- | --- |
| 空操作 no-op | `set_cursor_style`、`open_uri`、`reveal_path`、`write_to_*` | 什么都不做 | 动作没有可作用的对象（没有光标、没有文件管理器、没有剪贴板守护进程连接） |
| 返回 None | `read_from_clipboard`、`read_from_primary`、`active_window`、`window_stack` | 返回 `None` | 「查无此物」是诚实答案；调用方按 capability 探测降级 |
| 返回假/合成数据 | `displays`、`primary_display`、`keyboard_layout`、`compositor_name` | 返回固定假值 | 下游代码假设「至少有一块屏、至少有一个布局」，给假数据比给 `None` 让更多代码路径能跑通 |
| 显式报错 | `screen_capture_sources` | 立即回 `Err` | 能力名义上存在（feature 开了）但物理上不可能，让调用方拿到明确错误而非静默空列表 |
| 真实工作 | `open_window`、`run`、`with_common` | 干活 | 无头模式的核心价值所在 |

#### 4.2.2 核心流程

构造流程（`HeadlessClient::new`）：

```text
EventLoop::try_new()                      # 自己的 calloop 事件循环（headless 自己就是宿主）
  ↓
event_loop.get_signal() 交给 LinuxCommon::new
  ├── 返回 common        # 双执行器共享一个 LinuxDispatcher（u4-l3）
  ├── 返回 main_receiver # 前台任务通道（优先级队列包装的 calloop 源）
  └── 返回 wake_receiver # 系统唤醒通道
  ↓
handle().insert_source(main_receiver, …)  # 收到 runnable 就执行
handle().insert_source(wake_receiver, …)  # 收到唤醒就调 common.handle_system_wake()
  ↓
HeadlessClientState { event_loop, _loop_handle, common, display: 假屏 }
```

注意与 Wayland/X11 的结构性差异：那两个后端把 `main_receiver` 插进**自己**的合成器事件循环里；headless 后端没有合成器循环，于是自建一个纯 calloop 循环，里面**只有**这两个源——没有任何窗口系统事件。

#### 4.2.3 源码精读

状态与构造：

- [../gpui_linux/src/linux/headless/client.rs:L14-L22](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/headless/client.rs#L14-L22) —— 状态四件套：`event_loop`（用 `Option` 包裹是为了 `run` 时 `take` 走，防止二次运行）、`_loop_handle`（保活插入的源）、`common`（LinuxCommon 公共状态）、`display`（一块假屏，所有窗口共享同一个实例）。
- [../gpui_linux/src/linux/headless/client.rs:L24-L55](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/headless/client.rs#L24-L55) —— 构造全过程。L26 创建 calloop 循环；L28 把 `event_loop.get_signal()` 交给 `LinuxCommon::new`，这意味着后续 `Platform::quit` 调 `common.signal.stop()` 停的就是这个循环；L32-L38 注册前台任务源（`runnable.run()` 直接同步执行——正是 u4-l3 说的「headless 直跑」，没有 Wayland/X11 那种 `insert_idle` 排空逻辑，因为根本没有其他事件源需要让路）；L40-L46 注册系统唤醒源。
- [../gpui_linux/src/linux/platform.rs:L280-L282](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/platform.rs#L280-L282) —— 外壳的 `quit` 实现：`common.signal.stop()`。闭环于此。

五类姿态逐一对应到实现：

- [../gpui_linux/src/linux/headless/client.rs:L115-L123](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/headless/client.rs#L115-L123) —— 空操作类：`set_cursor_style`、`open_uri`、`reveal_path`、`write_to_primary`、`write_to_clipboard` 五连空体。对照契约 [../gpui_linux/src/linux/platform.rs:L83-L93](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/platform.rs#L83-L93)：这些全是**必需方法**，Wayland/X11 都得真实现，headless 只能交空体。
- [../gpui_linux/src/linux/headless/client.rs:L92-L98](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/headless/client.rs#L92-L98) 与 [L125-L131](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/headless/client.rs#L125-L131) —— 返回 None 类：没有活动窗口、没有窗口栈、剪贴板与主选区都读不出东西。
- [../gpui_linux/src/linux/headless/client.rs:L62-L77](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/headless/client.rs#L62-L77) —— 假数据类：键盘布局报告为 `"unknown"`；显示器三连（`displays`/`primary_display`/`display`）都返回那块唯一的 `HeadlessDisplay`，且 `display(id)` 用 `(display.id() == id).then_some(...)` 做 id 匹配——因为假屏 id 恒为 0，任何非 0 的 `DisplayId` 查询都会得到 `None`。
- [../gpui_linux/src/linux/headless/client.rs:L79-L90](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/headless/client.rs#L79-L90) —— 显式报错类：`screen_capture_sources`（仅 `screen-capture` feature 下编译）立刻通过 oneshot 通道送回 `Err("Headless mode does not support screen capture.")`。对比契约的默认实现 [../gpui_linux/src/linux/platform.rs:L65-L76](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/platform.rs#L65-L76)（「编译时没开 feature」的报错），headless 覆盖它换成「运行时无头」的报错——两种失败原因分开报告。
- [../gpui_linux/src/linux/headless/client.rs:L100-L109](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/headless/client.rs#L100-L109) —— 真实工作类之一：`open_window` 无条件成功，返回 `HeadlessWindow`。没有合成器意味着开窗永远不失败——这是无头模式能跑通「打开逻辑窗口」全链路的根基。
- [../gpui_linux/src/linux/headless/client.rs:L111-L113](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/headless/client.rs#L111-L113) —— `compositor_name()` 返回小写的 `"headless"`（注意 Wayland 返回 `"Wayland"`、X11 返回 `"X11"`，大小写不统一），这就是 4.1.4 实践中打印的值经外壳转发后的来源。

模块声明本身也说明了可见性设计：

- [../gpui_linux/src/linux/headless.rs:L1-L5](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/headless.rs#L1-L5) —— 只 `pub(crate) use client::*`，而 `window` 子模块保持私有（由 client.rs 通过 `crate::linux::headless::window::` 路径引用）。headless 的窗口类型不泄漏给 crate 外。

#### 4.2.4 代码实践

1. **实践目标**：亲手整理「契约 vs 三后端」的实现对照表，体会最小实现的边界。
2. **操作步骤**：
   - 打开 [../gpui_linux/src/linux/platform.rs:L51-L104](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/platform.rs#L51-L104) 的 `LinuxClient` 契约，列出全部方法。
   - 对每个方法，在 `headless/client.rs`、`wayland/client.rs`、`x11/client.rs` 三个文件里用编辑器的「跳转到实现」找到对应 `impl LinuxClient` 块中的条目。
   - 按 4.2.1 的五类给 headless 侧每个方法打标签，并记录另外两个后端该方法的大致行数。
3. **需要观察的现象**：哪些方法在 headless 是一行、在 Wayland/X11 是几十行；三个后端有没有都返回固定值的方法（例如 `compositor_name`）。
4. **预期结果**：得到一张三列对照表；你会发现 `with_common`、`run`、`open_window` 是三个后端都「真实工作」的最小公共集，而剪贴板、光标、URL 全部是 headless 的空体。

#### 4.2.5 小练习与答案

**练习 1**：`read_from_clipboard` 返回 `None` 和 `write_to_clipboard` 是空体，这两种「无能力」表达为什么不一样？

**答案**：读是有返回值的查询——`None` 是类型系统里现成的「查无」答案，调用方（如粘贴逻辑）本来就要处理 `None`；写是无返回值的动作——没有 `()` 之外的「失败」通道可选（契约没让它返回 `Result`），只能静默丢弃。契约签名决定了降级姿势。

**练习 2**：`screen_capture_sources` 为什么不像剪贴板一样静默，而要立即 `Err`？

**答案**：屏幕捕获是调用方**主动发起并期待结果**的异步操作，返回的是 oneshot 通道；如果送回空的 `Ok(vec![])`，调用方会以为「这台机器上没有可捕获的源」并可能据此做 UI 决策。`Err` 把「无头模式根本不支持」这个事实与「没有源」区分开，错误信息也直接写明了原因。

**练习 3**：`HeadlessClientState.event_loop` 为什么是 `Option`？

**答案**：calloop 的 `EventLoop::run` 拿走 `&mut` 自身并且设计为一次性运行。`run()` 里 `take().expect("App is already running")` 把循环从 `Option` 里取出来按值运行，同时用 `expect` 把「二次调用 run」变成显式 panic 而不是静默重入。参见 [../gpui_linux/src/linux/headless/client.rs:L133-L142](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/headless/client.rs#L133-L142)。

### 4.3 headless::window：无合成器世界里的窗口、假屏与图集

#### 4.3.1 概念说明

先读这个文件自己的模块文档，它是全模块的纲领：

> A headless window has no compositor surface and no GPU: layout, text shaping, and entity plumbing run normally, `draw` discards the scene, and the sprite atlas hands out tiles without uploading pixels (mirroring GPUI's `TestWindow`/`TestAtlas`). This lets command-line tools drive real `Window`-based code paths without a display server.

翻译成要点：布局、文本整形、实体管线**照常运行**；只有最后「把像素交给合成器/GPU」这一步被丢弃。这意味着无头窗口是有真实计算量的——它不是什么都不做，而是做到提交前的最后一厘米。

文件里有三个类型，分工如下：

- `HeadlessDisplay`：一块 1920×1080 的假屏；
- `HeadlessWindow`：实现 `PlatformWindow` 契约的无合成器窗口；
- `HeadlessAtlas`：实现 `PlatformAtlas` 契约的「只记账不上传」图集。

#### 4.3.2 核心流程

一个 headless 窗口的完整生命：

```text
App::open_window(options, build_root_view)
  → Window::new 翻译 WindowParams（u3-l1）
    → Platform::open_window → LinuxPlatform → HeadlessClient::open_window
      → HeadlessWindow::new(params, 假屏)      # 纯内存构造，无系统调用
  → 构建根视图
  → 强制绘制第一帧：
      Window::draw → render() → 布局/文本整形照常
        → platform_window.draw(&scene)          # ← 空体，场景在此被丢弃
        → 字形/精灵进 HeadlessAtlas             # ← 只分配 tile id，不上传像素
  → 返回 WindowHandle

之后的帧：
  cx.notify() → 窗口标脏 → platform_waker 为 None（frame_waker 未覆盖）
    → 没有任何东西发起帧请求 → 永远停在脏状态
  （除非用户显式调用 window.draw(cx)，见 4.3.4）
```

#### 4.3.3 源码精读

**假屏**：

- [../gpui_linux/src/linux/headless/window.rs:L26-L36](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/headless/window.rs#L26-L36) —— `HeadlessDisplay` 固定 1920×1080 逻辑像素。
- [../gpui_linux/src/linux/headless/window.rs:L38-L51](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/headless/window.rs#L38-L51) —— `id()` 恒返回 `DisplayId::new(0)`；`uuid()` 返回 `Uuid::nil()`，L44 的注释点明理由：「恰好只有一块 headless 屏」，全零 uuid 就是它的稳定身份（对照 u2-l3：真平台上 uuid 用来跨重启匹配显示器，headless 下只有一块屏，nil 足矣）。

**没有原生窗口句柄**：

- [../gpui_linux/src/linux/headless/window.rs:L63-L78](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/headless/window.rs#L63-L78) —— raw-window-handle 的两个 supertrait 方法都返回 `Err(HandleError::NotSupported)`。u3-l2 讲过 X11 交出窗口 id、Wayland 交出 `wl_surface` 指针；headless 什么都没有，`NotSupported` 正是 raw-window-handle 为此准备的变体。这意味着 wgpu 之类的外部渲染器无法在 headless 窗口上创建 surface——符合预期。

**窗口状态：三类方法的真实分布**。`HeadlessWindowState`（[L53-L59](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/headless/window.rs#L53-L59)）只有五个字段，全部服务于「真实记账」组：

| 分组 | 方法（行号） | 行为 |
| --- | --- | --- |
| 真实记账 | `resize` [L109-L111](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/headless/window.rs#L109-L111)、`set_title`/`get_title` [L170-L176](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/headless/window.rs#L170-L176)、`toggle_fullscreen`/`is_fullscreen` [L184-L191](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/headless/window.rs#L184-L191)、`set_input_handler`/`take_input_handler` [L137-L143](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/headless/window.rs#L137-L143) | 修改并回读内存状态。注意 `toggle_fullscreen` 真的会翻转 `is_fullscreen`——调用方读回的状态与自己的操作自洽 |
| 返回假数据 | `scale_factor` 恒 1.0 [L113-L115](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/headless/window.rs#L113-L115)、`appearance` 恒 Dark [L117-L119](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/headless/window.rs#L117-L119)、`mouse_position` 恒 (0,0) [L125-L127](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/headless/window.rs#L125-L127)、`modifiers`/`capslock` 恒默认 [L129-L135](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/headless/window.rs#L129-L135)、`is_active`/`is_hovered` 恒 false [L158-L164](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/headless/window.rs#L158-L164) | 给出确定性的默认世界：1 倍缩放、暗色外观、鼠标在左上角、无修饰键、窗口不活跃 |
| 空操作 | `activate` [L156](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/headless/window.rs#L156)、`set_background_appearance` [L178](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/headless/window.rs#L178)、`minimize`/`zoom` [L180-L182](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/headless/window.rs#L180-L182)、`update_ime_position` [L226](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/headless/window.rs#L226) | 动作无处投递 |

两个值得单独点出的设计：

- [../gpui_linux/src/linux/headless/window.rs:L145-L154](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/headless/window.rs#L145-L154) —— `prompt` 返回 `None`，L152 注释写明用意：「回退到 GPUI 的渲染提示」。`PlatformWindow::prompt` 的契约语义是「如果平台能弹原生对话框就返回通道，否则返回 None 让 GPUI 自己画一个」。headless 返回 `None` 的结果是提示框走进 GPUI 的自绘路径——而无头模式下自绘同样只是布局计算。这是一个「降级到渲染层」而非「报错」的典型。
- [../gpui_linux/src/linux/headless/window.rs:L193-L214](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/headless/window.rs#L193-L214) —— 全文件最重要的一段：11 个 `on_*` 回调注册方法全部空体，L193-L194 的注释是理解无头帧模型的钥匙：**「没有合成器驱动帧循环，所以帧与状态回调被丢弃：任何 await 一帧的代码在无头环境下永远不会再被唤醒」**。对照 u3-l2：真平台上 gpui 在 `Window::new` 里注册 `on_request_frame` 等回调（[../gpui/src/window.rs:L1533](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs#L1533) 起），回调被调时执行 `window.draw(cx)`；headless 把回调扔掉，等于切断了整条重绘触发链。

**重绘链为什么断了——gpui 侧的证据**：

- [../gpui/src/window.rs:L1669](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs#L1669) —— gpui 用 `platform_window.frame_waker()` 设置「标脏唤醒器」。
- [../gpui/src/platform.rs:L849-L851](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/platform.rs#L849-L851) —— `frame_waker` 契约默认返回 `None`，而 `HeadlessWindow` 没有覆盖它。于是 `cx.notify()` 标脏后唤醒器为空，无人发起帧请求；即使发起，`on_request_frame` 的回调也已被丢弃。双重断路，无头窗口在首帧之后不会再自发绘制。

**首帧从哪来**：

- [../gpui/src/app.rs:L1264-L1269](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app.rs#L1264-L1269) —— `App::open_window` 在返回前 `window.draw(cx)`，注释说明这是为了窗口「至少绘制过一次」（最初为修复 Windows 上输掉 `on_request_frame` 竞态导致空树断言的崩溃）。无头模式下这一帧就是窗口一生中唯一自发的一帧。
- [../gpui/src/window.rs:L3021](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs#L3021) —— `Window::draw` 的最后一站：`self.platform_window.draw(&self.rendered_frame.scene)`。headless 的 `draw` 空体（[window.rs:L216](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/headless/window.rs#L216)）让场景在这里被静默丢弃——但注意到达这里之前，render、布局、文本整形都已经真实发生过。

**图集**：

- [../gpui_linux/src/linux/headless/window.rs:L218-L220](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/headless/window.rs#L218-L220) —— `sprite_atlas()` 每次调用新建一个 `HeadlessAtlas`。谁调用？[../gpui/src/window.rs:L1413](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs#L1413)：gpui 的 `Window::new` 只在构造时取一次并自己持有，所以每个窗口恰好一个图集实例，tile 缓存在窗口生命周期内是持久的。
- [../gpui_linux/src/linux/headless/window.rs:L233-L287](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/headless/window.rs#L233-L287) —— `HeadlessAtlas`：`get_or_insert_with` 先查 `HashMap` 缓存（L252-L257，先释放锁再执行 `build`，缩小锁区间），未命中则执行传入的 `build` 拿到尺寸（L259）——**像素数据被 `_` 直接丢弃**——然后分配递增的 `texture_id`/`tile_id` 并登记 tile（L263-L281）。L233-L234 的注释总结：「只分配 tile 不上传像素，让字形与精灵绘制在无头环境下也能走完」。

**与 TestWindow 的对照**（模块文档明说二者同源）：

| 维度 | HeadlessWindow | TestWindow |
| --- | --- | --- |
| raw-window-handle | `NotSupported`（[L63-L78](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/headless/window.rs#L63-L78)） | 同样 `NotSupported`（[test/window.rs:L53-L67](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/platform/test/window.rs#L53-L67)） |
| `on_*` 回调 | 丢弃 | **存进字段**（如 [request_frame_callback](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/platform/test/window.rs#L39)），测试可以主动触发它们模拟事件 |
| 图集 | `HeadlessAtlas` 只记账 | `TestAtlas` 同样只记账，但若注入 `PlatformHeadlessRenderer` 则用渲染器的图集（[test/window.rs:L77-L80](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/platform/test/window.rs#L77-L80)） |
| 定位 | 生产代码的无头后端 | 测试替身（u8-l4 会展开） |

一句话：TestWindow 是「记录并回放」，HeadlessWindow 是「直接丢弃」——前者要向测试暴露触发点，后者只需要不出错。

#### 4.3.4 代码实践

1. **实践目标**：在无头环境下手动驱动一个逻辑窗口画出多帧，验证「notify 不重绘、draw 可手动驱动」。
2. **操作步骤**：
   - 按综合实践（第 5 节）搭好独立 crate 后，把 `main.rs` 换成下面的代码（示例代码）：

   ```rust
   use gpui::{App, Bounds, Context, Window, WindowBounds, WindowOptions, div, prelude::*, px, rgb, size};
   use gpui_platform::headless;

   struct FrameCounter {
       frames: usize,
   }

   impl Render for FrameCounter {
       fn render(&mut self, _window: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
           self.frames += 1;
           println!("[render] 第 {} 帧布局完成", self.frames);
           div().size_full().bg(rgb(0x2e7d32)).child(format!("headless frame #{}", self.frames))
       }
   }

   fn main() {
       let app = headless();
       app.run(|cx: &mut App| {
           println!("[boot] compositor_name = {:?}", cx.compositor_name());

           let handle = cx
               .open_window(
                   WindowOptions {
                       window_bounds: Some(WindowBounds::Windowed(Bounds::centered(
                           None, size(px(800.), px(600.)), cx,
                       ))),
                       ..Default::default()
                   },
                   |_, cx| cx.new(|_| FrameCounter { frames: 0 }),
               )
               .unwrap();
           // open_window 返回前已强制画过第 1 帧（app.rs:L1264-L1269）

           // cx.notify() 只标脏、不重绘；手动再驱动 3 帧：
           for i in 1..=3 {
               handle
                   .update(cx, |_, window: &mut Window, cx: &mut Context<FrameCounter>| {
                       cx.notify();
                       window.draw(cx).clear(cx);
                   })
                   .unwrap();
               println!("[drive] 手动绘制第 {} 帧", i);
           }

           println!("[quit] 请求退出事件循环");
           cx.quit();
       });
       println!("[main] app.run 已返回");
   }
   ```

   - 运行：`cargo run`（无需任何显示服务器；若你的依赖开启了 wayland/x11 feature，用 `ZED_HEADLESS=1 cargo run` 强制无头）。
3. **需要观察的现象**：`[render]` 与 `[drive]` 的交替顺序；`[main]` 是否在 `cx.quit()` 之后被打印。
4. **预期结果**：`[render]` 出现 4 次（1 次来自开窗 + 3 次手动），说明 `render()`/布局确实在无显示环境下真实执行；`cx.quit()` 后事件循环退出、`app.run` 返回、`[main]` 被打印。若把 `window.draw(...)` 那行注释掉只留 `cx.notify()`，`[render]` 只会出现 1 次——验证 4.3.3 讲的双重断路。精确输出待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：在 headless 下调用 `window.update(..., |_, window, _| window.request_animation_frame())`（或任何「等下一帧」的异步代码）会怎样？

**答案**：永远挂起。帧请求依赖 `on_request_frame` 注册的回调被平台调用，而 headless 把回调丢弃了（[L193-L195](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/headless/window.rs#L193-L195) 的注释原文就是「anything that awaits a frame will never resolve headlessly」）。写无头程序时要避免把业务逻辑挂在帧回调上。

**练习 2**：`HeadlessWindow::scale_factor()` 恒返回 1.0，这对文本渲染意味着什么？

**答案**：所有逻辑像素与物理像素 1:1，`is_subpixel_rendering_supported()` 也恒为 false（[L222-L224](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/headless/window.rs#L222-L224)），布局测量完全确定。这让无头运行的布局结果可复现——正是 CI 里跑视觉/布局断言需要的性质；代价是不能模拟 HiDPI 屏幕（那是 `TestWindow` 配合参数化 scale 才能做的事）。

**练习 3**：`HeadlessAtlas::get_or_insert_with` 为什么要先释放锁、执行 `build`、再重新加锁，而不是全程持锁？

**答案**：`build` 闭包可能做昂贵的字形光栅化（真后端里如此）。若全程持锁，一个线程整形文本会阻塞其他线程的 tile 查询。先查缓存（快路径）、放锁执行慢操作、再持锁写入，是读-算-写模式的标准加锁姿势。headless 里 build 结果其实只用尺寸，但结构保留了下来。

### 4.4 headless 事件循环与工程意义：谁在驱动，驱动到哪为止

#### 4.4.1 概念说明

u4-l3 讲过 Linux 调度器的宿主问题：`LinuxDispatcher` 不拥有主循环，谁持有 `main_receiver` 谁就是宿主。Wayland/X11 的宿主是合成器事件循环；headless 的宿主是 `HeadlessClient::new` 自建的纯 calloop 循环。这个循环里**只有两种事件**：前台任务和系统唤醒。没有Expose 事件、没有输入事件、没有帧回调。

由此推出无头应用的运行形态：它本质上是一个「被 GPUI 实体系统与执行器驱动的守护进程」——你 spawn 的任务照常被调度，窗口的布局与文本整形在开窗（以及你手动 `draw`）时真实发生，但没有任何来自外部的输入会改变它的状态。

还有一个容易被误解的点：**headless 不等于「没有文本」**。文本系统的选择发生在 `LinuxCommon::new`，取决于编译 feature 而非运行期后端。

#### 4.4.2 核心流程

```text
Application::run(on_finish_launching)
  → LinuxPlatform::run:
      on_finish_launching()          # 同步先执行（用户回调：开窗、spawn 任务…）
      LinuxClient::run(&self.inner)  # 阻塞进入 headless calloop 循环
          循环体内只有:
            - main_receiver 事件 → runnable.run()   # 前台任务
            - wake_receiver 事件 → handle_system_wake()
      （直到 Platform::quit → common.signal.stop()）
      → 取出并执行 on_quit 回调
  → run 返回，进程可以正常退出
```

#### 4.4.3 源码精读

- [../gpui_linux/src/linux/headless/client.rs:L133-L142](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/headless/client.rs#L133-L142) —— `run` 的全部：`take()` 取走事件循环（`expect("App is already running")` 防重入），`event_loop.run(None, &mut self.clone(), |_| {})` 无限期阻塞，退出后 `log_err()` 记录可能的循环错误。`None` 超时意味着空闲时进程安静睡眠，不耗 CPU。
- [../gpui_linux/src/linux/platform.rs:L267-L278](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/platform.rs#L267-L278) —— 外壳的 `run` 三段式：同步执行启动回调 → 阻塞进后端循环 → 循环退出后触发 `on_quit` 观察者。u2-l2 讲过 Linux/Windows 在进循环前同步执行回调、macOS 等 AppKit 通知——headless 沿用 Linux 的这一姿态，所以你在回调里写的 `open_window`、`spawn` 都是立刻生效的。
- [../gpui_linux/src/linux/platform.rs:L139-L178](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/platform.rs#L139-L178) —— `LinuxCommon::new`。L149-L152 是关键分支：只要 `wayland`/`x11` 任一 feature 开启，`text_system` 就是 `CosmicTextSystem`（基于 font-kit，**不需要连接显示服务器**）；两个都没开才是 `NoopTextSystem`。L156-L162 构造共享同一个 `LinuxDispatcher` 的前台/后台执行器——u4-l3 的全部调度行为原样可用。
- [../gpui_linux/src/linux/platform.rs:L180-L195](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/platform.rs#L180-L195) —— `start_wake_listener`：注意 L182 的 cfg 门控是 `all(target_os = "linux", any(feature = "wayland", feature = "x11"))`——严格说 headless 后端（无 feature 场景）不会真正启动 login1 监听，`wake_receiver` 只是保持管线完整。这是「feature 决定能力、后端决定行为」的又一处体现。

**工程意义小结**（对应本讲学习目标的第一条）：

- **CI**：`ZED_HEADLESS=1` 或直接 `headless()` 让基于 GPUI 的程序在无显示容器里跑通开窗、布局、任务调度全链路；布局结果确定（固定假屏、1 倍缩放、暗色外观），适合做断言。
- **远程/服务器**：想在无头机器上执行「渲染到内存再编码」这类工作，headless 提供了除最后提交之外的全部管线；真正离屏出图要配合 `PlatformHeadlessRenderer`（目前仅 macOS，u8-l3 展开）。
- **依赖面最小化**：[Cargo.toml:L14-L21](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/Cargo.toml#L14-L21) 里 `gpui_platform` 的 default feature 为空——不开启 wayland/x11 时，Linux 上**只能**得到 headless 后端（`guess_compositor` 的环境检查被 cfg 掉了），构建产物也不引入 wayland-client/x11rb 等重依赖。

#### 4.4.4 代码实践

1. **实践目标**：观察 headless 事件循环的阻塞与退出行为。
2. **操作步骤**：
   - 复用 4.3.4 的程序，把 `cx.quit()` 临时注释掉，运行后用 `Ctrl+C` 杀掉，观察程序自身是否会退出。
   - 恢复 `cx.quit()`，并在 `app.run(...)` 回调里追加一个延时任务：

   ```rust
   // （示例代码）放在 run 回调末尾、cx.quit() 之前
   cx.background_executor()
       .timer(std::time::Duration::from_secs(1))
       .detach();
   cx.spawn(|cx| async move {
       cx.background_executor().timer(std::time::Duration::from_millis(500)).await;
       println!("[task] 延时任务在事件循环里被调度了");
   })
   .detach();
   ```

   - 把 `cx.quit()` 移进这个延时任务末尾再运行。
3. **需要观察的现象**：注释掉 quit 时进程是否挂住；quit 移进任务后，`[task]` 与 `[quit]`、`[main]` 的先后顺序。
4. **预期结果**：不调 `quit` 则 calloop 循环永不退出（空闲睡眠，不是忙转）；quit 移进任务后顺序为 `[task]` → `[quit]` → `[main]`，证明前台/后台任务确实经由 `main_receiver` 在这个无头循环里被调度。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：headless 的事件循环与 Wayland 后端的事件循环最本质的差别是什么？

**答案**：事件源的构成。Wayland 循环里有 wl_display 的事件源（输入、帧回调、输出变化）、X11 有 XCB 连接的事件源；headless 只有 `main_receiver`（任务）和 `wake_receiver`（唤醒）两个源（[client.rs:L32-L46](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/headless/client.rs#L32-L46)）。因此 u4-l3 里 X11 需要的「执行 runnable 后排空 x11rb 缓冲」这类协调问题在 headless 中根本不存在——没有需要让路的外部事件。

**练习 2**：一个 headless 程序里 `cx.spawn` 的任务在 `app.run` 回调返回之后还能执行吗？

**答案**：能。`run` 回调只是「启动完成」钩子，`LinuxPlatform::run` 在回调返回后才进入事件循环（[platform.rs:L267-L270](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/platform.rs#L267-L270)）；回调里 spawn 的任务之后经 `main_receiver` 到达循环体内执行。这也是为什么 `cx.quit()` 可以放在延时任务里——它在循环运行期间被调度执行。

**练习 3**：为什么 `HeadlessClient::run` 里用 `self.clone()` 而不是 `self` 作为循环数据？

**答案**：`run(&self)` 只拿到 `&HeadlessClient`，而 calloop 的 `EventLoop::run` 需要按值拥有 `&mut HeadlessClient` 作为循环数据。`HeadlessClient` 是 `Rc<RefCell<State>>` 的轻量克隆（[client.rs:L21-L22](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/headless/client.rs#L21-L22)），克隆一个新句柄交给循环即可，两个句柄共享同一份状态。

## 5. 综合实践

**任务**：在无显示环境（或 `ZED_HEADLESS=1`）下运行一个使用 `headless()` 的程序，打开一个逻辑窗口并手动驱动几帧渲染，产出一份「静默处理清单」。

**步骤**：

1. 在 zed 仓库之外新建独立 crate（示例代码）：

```toml
# Cargo.toml（示例代码）
[package]
name = "headless-drive"
version = "0.1.0"
edition = "2021"

[dependencies]
# 按你的 zed 检出路径调整；default feature 为空 = 纯 headless 构建
gpui = { path = "../zed/crates/gpui" }
gpui_platform = { path = "../zed/crates/gpui_platform" }
```

> 若想启用真实文本整形，给 `gpui_platform` 加 `features = ["x11"]`（文本系统随 feature 走，与是否 headless 无关，见 4.4.3）；此时记得用 `ZED_HEADLESS=1` 运行以强制无头。

2. 把 4.3.4 的 `main.rs` 抄进去，`cargo run` 跑通，确认 4 次 `[render]`。
3. 逐行对照下表，在程序里或阅读中验证每一项（打勾并注明证据行号）：

| 平台调用 | headless 行为 | 证据 |
| --- | --- | --- |
| `open_window` | 成功，返回内存中的 `HeadlessWindow` | client.rs L100-L109 |
| 首帧绘制 | `App::open_window` 返回前强制执行一次 | app.rs L1264-L1269 |
| `cx.notify()` | 标脏但永不触发重绘 | platform.rs L849-L851 + window.rs L193-L195 |
| `window.draw(cx)` | 布局/整形真实执行，场景在 `platform_window.draw` 处丢弃 | window.rs L3021 + headless/window.rs L216 |
| `displays()`/`primary_display()` | 返回唯一 1920×1080 假屏，uuid 为 nil | headless/window.rs L26-L51 |
| `read_from_clipboard()` | `None` | client.rs L129-L131 |
| `write_to_clipboard(item)` | 静默丢弃 | client.rs L123 |
| `compositor_name()` | `"headless"` | client.rs L111-L113 |
| 等待下一帧的 future | 永不 resolve | headless/window.rs L193-L194 注释 |
| `cx.quit()` | `signal.stop()` 结束 calloop 循环 | platform.rs L280-L282 |

4. 把验证过的表连同运行日志输出，写成学习笔记。

**预期结果**：你将得到一份可运行的无头 GPUI 最小程序，以及一张标满源码行号的「哪些平台调用被静默处理」清单——这份清单就是后续把任何 GPUI 程序搬进 CI 的排障地图。

## 6. 本讲小结

- 进入 headless 有两条路：显式 `gpui_platform::headless()`（参数短路，优先级最高）与隐式 `application()` 落入 `guess_compositor()` 的 `"Headless"` 兜底（`ZED_HEADLESS` 存在即触发、空串也算）；两条路最终都构造 `LinuxPlatform { inner: HeadlessClient::new() }`。
- `HeadlessClient` 是 `LinuxClient` 契约的最小实现，其方法呈五类姿态：空操作（写剪贴板、开 URL）、返回 None（读剪贴板、活动窗口）、返回假数据（唯一假屏、`"unknown"` 键盘布局、`"headless"` 合成器名）、显式报错（屏幕捕获）、真实工作（`open_window`、`run`、`with_common`）。
- 无头窗口「存在但不显示」：布局、文本整形、实体管线照常运行，首帧由 `App::open_window` 强制绘制；此后重绘链双重断路——`frame_waker` 默认 `None` 且 11 个 `on_*` 回调被丢弃——所以 `cx.notify()` 只标脏，`await 一帧`的代码永不 resolve，多帧只能手动 `window.draw(cx)` 驱动。
- `HeadlessAtlas` 只分配 tile id 不上传像素，让字形/精灵路径在无 GPU 环境走完；`HeadlessWindow` 与 gpui 的 `TestWindow` 同源但取向不同：一个丢弃回调，一个存下回调供测试回放。
- headless 自建 calloop 循环作宿主，事件源只有前台任务与系统唤醒两种；文本系统取决于编译 feature（开了 wayland/x11 就是 CosmicTextSystem，无头也能真实整形），与运行期后端无关。
- 工程意义：CI 无显示跑通全链路、远程服务器复用 GPUI 代码、`gpui_platform` 零 feature 构建天然 headless 且依赖面最小。

## 7. 下一步学习建议

- 下一讲 **u5-l3（X11 客户端：XCB 连接、事件翻译与 XIM 输入法）** 将进入第一个「真」图形后端：对照本讲的「空实现清单」，逐项看 X11Client 如何把 `set_cursor_style`、剪贴板、事件翻译做成真实实现，并理解 u4-l3 遗留的「执行 runnable 后排空 x11rb 缓冲」在事件循环结构上的位置。
- 想先补「测试替身」这条线，可提前阅读 [../gpui/src/platform/test/window.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/platform/test/window.rs)，对照本讲 4.3.3 的异同表，为 u8-l4 做准备。
- 若关心「无头 + 真实出图」，可顺带浏览 [src/gpui_platform.rs:L84-L97](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/src/gpui_platform.rs#L84-L97) 的 `current_headless_renderer()`——目前仅 macOS 返回 Metal 实现，u8-l3 会展开。

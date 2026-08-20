# Platform 抽象与跨平台架构

## 1. 本讲目标

学完本讲，你应该能够：

1. 列举 `Platform` trait 承担的全部职责，并说出每类职责对应的方法族。
2. 区分 `PlatformWindow`、`PlatformDispatcher`、`PlatformTextSystem`、`PlatformAtlas` 各自的契约边界，理解「新增一个平台要实现哪些 trait」。
3. 理解 test platform（`TestPlatform` / `TestWindow` / `TestDispatcher`）如何在无真实窗口的环境里创建实体、手动驱动帧。
4. 解释 `read_from_clipboard` 与 `read_from_clipboard_async` 的关系，以及 web 平台的异步权限模型（`ClipboardReadError`）。
5. 沿着 `Application::with_restart_arguments` → `App::restart` → `Platform::restart(binary_path, arguments)` 说清重启参数的转发链路，并会用 `expect_restart` 验证它。

## 2. 前置知识

本讲是第 7 单元（专家层）的入口，默认你已完成 u1–u6。用到的前置概念快速回顾：

- **`App` 是根上下文**（u2-l1）：应用启动时，`Application` 只是一层配置包装，真正全局状态在 `App` 里；`App` 内部持有一个 `Rc<dyn Platform>`——本讲的主角就是它。
- **窗口绘制管线**（u4-l3）：`cx.notify()` 标脏窗口 → 平台请求帧 → `Window::draw` 走完三阶段 → `present`。当时我们说「平台帧回调」，本讲看清这个回调在平台侧长什么样（`on_request_frame`）。
- **双执行器并发模型**（u2-l5）：`ForegroundExecutor` 在主线程轮询、`BackgroundExecutor` 送线程池，两者共享一个 `PlatformDispatcher` 适配器。本讲会看到这个 trait 的完整契约。
- **文本系统分层**（u6-l5）：`TextSystem` 把整形与光栅化委托给 `PlatformTextSystem`——平台侧的字体后端。
- **crate 分工**（u1-l1）：`gpui` 定义 Platform 各 trait；真正实现散落在兄弟 crate `gpui_macos` / `gpui_windows` / `gpui_linux` / `gpui_web`，由门面 crate `gpui_platform` 按 `target_os` 挑选。

一个值得先建立的心智模型：**GPUI 把「操作系统」抽象成了一个 trait 对象**。上层代码（元素、样式、实体）对操作系统一无所知，只在边界处通过 `Rc<dyn Platform>`（应用级）、`Box<dyn PlatformWindow>`（窗口级）等几个窄接口与外界说话。macOS 的 AppKit、Linux 的 Wayland/X11、Windows 的 Win32、浏览器的 DOM，都被压进同一组方法签名里。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/platform.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform.rs) | 平台抽象总纲（约 3000 行）：定义 `Platform`、`PlatformWindow`、`PlatformDispatcher`、`PlatformTextSystem`、`PlatformAtlas`、`PlatformDisplay`、`PlatformHeadlessRenderer` 等 trait，以及 `ClipboardItem`、`WindowParams`、`AppLifecyclePhase` 等配套类型 |
| [src/platform/test.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform/test.rs) | test platform 的模块汇总（仅 11 行），重导出 `dispatcher` / `display` / `platform` / `window` 四个子模块 |
| [src/platform/test/platform.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform/test/platform.rs) | `TestPlatform`：供测试用的 `Platform` 实现，含 `expect_restart` 通道、内存剪贴板、prompt 队列 |
| [src/platform/test/window.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform/test/window.rs) | `TestWindow`：`PlatformWindow` 的测试实现，`simulate_frame_request` 手动驱动帧 |
| [src/platform/test/dispatcher.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform/test/dispatcher.rs) | `TestDispatcher`：背靠 `scheduler` crate 的 `TestScheduler`，假时钟 + 确定性调度 |
| [src/app.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/app.rs) | `Application::with_platform` / `with_restart_arguments` / `run`、`App::restart`、`set_restart_path`，以及 `test_restart_preserves_path_and_arguments` 测试 |
| [src/app/test_context.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/app/test_context.rs) | `TestAppContext`：把 `TestPlatform` 组装成可用测试上下文的地方，含 `expect_restart` |
| [src/app/headless_app_context.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/app/headless_app_context.rs) | `HeadlessAppContext`：`TestPlatform::with_platform` 注入真实字体后端与可选 GPU 渲染器的官方范例 |
| ../gpui_web/src/platform.rs（兄弟 crate） | web 平台对 `read_from_clipboard_async` 的覆写，异步剪贴板权限模型的实例 |

## 4. 核心概念与源码讲解

### 4.1 Platform trait：操作系统门户的完整职责清单

#### 4.1.1 概念说明

`Platform` 是「应用级」的平台抽象：一个进程一份，由 `App` 持有。它回答的问题是——**除了计算与绘制，一个 GUI 应用还需要操作系统提供什么？** 答案被分成约十四组职责（事件循环、窗口创建、显示器、剪贴板、菜单、通知、凭据、键盘布局……），全部收进一个约 215 行的 trait。

注意两点设计约束：

- trait 本身**没有 `Send`/`Sync` 约束**——它住在主线程，内部可以自由使用 `Rc`、`RefCell`（回顾 u2-l1 的单前台线程模型）。
- 大量方法带**默认实现**，这是跨平台 trait 的减负手段，且默认实现分两种语义：返回 `None`/`false`/空体，表示「这个平台没有该能力」（能力缺失）；或委托给另一个方法，表示「默认等价于某种简单行为」（向后兼容的扩展点）。下文会各举一例。

#### 4.1.2 核心流程

App 与 Platform 的协作时序：

```text
Application::with_platform(Rc<dyn Platform>)   ← 注入平台（app.rs:177）
        │
        ▼
App::new_app(platform, assets, http_client)    ← App 持有 Rc<dyn Platform>
        │
        ▼
Application::run(callback)
        │  调 platform.run(on_finish_launching) ← 控制权交给平台事件循环（阻塞）
        ▼
平台事件循环驱动一切：
  - 帧回调 → PlatformWindow::on_request_frame 注册的闭包 → Window::draw
  - 输入   → PlatformWindow::on_input 注册的闭包 → 事件派发
  - 定时   → PlatformDispatcher::dispatch_after
        │
        ▼
cx.quit() / cx.restart()
        │  转发 platform.quit() / platform.restart(path, args)
        ▼
进程结束 / 新进程拉起
```

#### 4.1.3 源码精读

trait 声明与三个基础设施入口——平台还要负责交出自己的两个执行器与字体系统：

[src/platform.rs:L125-L129](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform.rs#L125-L129)
这段声明 `Platform` trait 并要求提供 `background_executor`、`foreground_executor`、`text_system` 三个访问器——平台包办线程池、主线程调度与字体后端。

生命周期方法族：

[src/platform.rs:L131-L137](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform.rs#L131-L137)
`run` 把控制权交给平台事件循环（回调只在启动完成后执行一次，回顾 u1-l2）；`quit` 请求退出；`restart(binary_path, arguments)` 请求重启（4.6 节详解）；`activate`/`hide` 系列操作应用在操作系统任务切换器中的可见性。

「能力缺失」型默认实现的两例：

[src/platform.rs:L141-L160](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform.rs#L141-L160)
`window_stack` 默认返回 `None`（只有 macOS 能报告窗口叠放顺序）；`is_screen_capture_supported` 默认 `false`，`screen_capture_sources` 默认立即返回一个「未编译 screen-capture feature」的错误——没有屏幕捕获能力的平台什么都不用写。

开窗入口：

[src/platform.rs:L162-L166](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform.rs#L162-L166)
`open_window` 接收 `AnyWindowHandle`（类型擦除的窗口句柄，上层已建好 `Window` 逻辑对象）与纯配置 `WindowParams`，返回 `Box<dyn PlatformWindow>`——这是 u7-l2 多窗口管理要用的底层原语。

面向「只有部分操作系统才有」的功能，方法按平台条件编译出现：

[src/platform.rs:L324-L332](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform.rs#L324-L332)
Linux/FreeBSD 的 primary selection（中键粘贴选择区）与 macOS 的 find pasteboard 是 `cfg` 门控的条件方法——trait 本身也随平台伸缩。

移动端生命周期是近几年扩展出的整块职责：

[src/platform.rs:L207-L229](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform.rs#L207-L229)
`on_app_lifecycle`、`on_memory_warning`、`gestures` 三个带默认空实现的方法服务移动端：桌面平台永不调用。相位词汇表是 [src/platform.rs:L758-L769](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform.rs#L758-L769) 的 `AppLifecyclePhase`（Active/Inactive/Background/Foreground，注释里给出了与 iOS/Android 回调的映射表）。

trait 的收尾——凭据与键盘布局：

[src/platform.rs:L334-L341](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform.rs#L334-L341)
`write_credentials`/`read_credentials`/`delete_credentials` 对接系统钥匙串（macOS Keychain、Windows 凭据管理器）；`keyboard_layout`/`keyboard_mapper`/`on_keyboard_layout_change` 服务键位派发（u5-l4）。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：把 `Platform` trait 的方法按职责分组，形成你自己的「操作系统职责清单」。
2. **操作步骤**：打开 [src/platform.rs:L126-L341](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform.rs#L126-L341)，用注释把方法切成：基础设施 / 生命周期 / 显示器与窗口 / 外观 / URL 与文件 / 事件回调 / 移动端 / 菜单 / 热状态 / 身份与通知 / 路径与光标 / 剪贴板 / 凭据 / 键盘。再统计哪些方法有默认实现。
3. **需要观察的现象**：默认实现占比相当高；带 `cfg(target_os = ...)` 的方法只有寥寥数个。
4. **预期结果**：得到一张约 14 组的分组表。你会发现「新增平台」的必修课集中在 `run`/`open_window`/剪贴板/执行器等约 30 个无默认方法上，其余都可以先用默认实现撑起来。

#### 4.1.5 小练习与答案

**练习 1**：`Platform` trait 为什么不加 `Send + Sync` 约束，而 `PlatformDispatcher` 必须加？

**答案**：`Platform` 只在主线程被借用（`App` 持有它，单前台线程模型，u2-l1），内部实现大量使用 `Rc`/`RefCell` 等非线程安全类型；而 `PlatformDispatcher` 要同时被主线程的 `ForegroundExecutor` 与后台线程池里的 `BackgroundExecutor` 共享（u2-l5），必须 `Send + Sync`（见 [src/platform.rs:L1028-L1029](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform.rs#L1028-L1029)）。

**练习 2**：`window_stack` 的默认实现返回 `None`，`read_from_clipboard_async` 的默认实现调用 `read_from_clipboard`——这两种默认实现的语义差别是什么？

**答案**：前者是「能力缺失」声明——未覆写的平台明确表示不支持该功能，调用方要处理 `None`；后者是「等价简化」——对剪贴板本来就能同步读取的平台，异步版本退化为包装同步结果（[src/platform.rs:L313-L322](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform.rs#L313-L322)），只有权限模型不同的平台（web）才需要覆写。前者是可选插槽，后者是向后兼容的扩展点。

### 4.2 PlatformWindow：单个窗口的平台侧契约

#### 4.2.1 概念说明

`App` 管应用级事务，每个 `Window`（u4-l3 的绘制管线主体）背后还有一个平台侧窗口对象 `Box<dyn PlatformWindow>`。它承接三类事情：

1. **查询**：边界、缩放系数、外观、鼠标位置、修饰键状态——上层的 `window.bounds()`、`window.scale_factor()` 最终都落到这里。
2. **命令**：改标题、最小化、全屏、缩放、激活。
3. **回调注册**：u4-l3 说「平台帧回调」`on_request_frame`、u5-l1 说「输入事件从平台涌入」`on_input`——注册点就在这个 trait 上。

此外它要求实现 `HasWindowHandle + HasDisplayHandle`（[src/platform.rs:L816](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform.rs#L816)），这是 `raw-window-handle` 生态的互操作接口——让 GPU 渲染器等第三方库能拿到原生窗口句柄。

这个 trait 是「平台特性重灾区」：macOS 标签页、Windows 窗口装饰、Wayland layer shell、移动端返回键与软键盘，各自是一组带默认空实现的方法，只被对应平台覆写。

#### 4.2.2 核心流程

一帧的平台侧协作（衔接 u4-l3 的管线）：

```text
上层 cx.notify() → WindowInvalidator 标脏 → frame_waker() 唤醒平台帧源
        │
        ▼
平台 vsync 到来 → 调用 on_request_frame 注册的闭包（带 RequestFrameOptions）
        │
        ▼
Window::draw 编排元素三阶段，产出 Scene
        │
        ▼
PlatformWindow::draw(scene) 把场景提交给平台渲染器
        │
        ▼
PlatformWindow::completed_frame()（默认空）帧收尾通知
```

#### 4.2.3 源码精读

查询与命令方法（无默认实现，每个平台必修）：

[src/platform.rs:L817-L830](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform.rs#L817-L830)
从 `bounds` 到 `take_input_handler`：窗口几何（`bounds`/`content_size`/`scale_factor`）、输入态（`mouse_position`/`modifiers`/`capslock`）、输入法对接（`set_input_handler`/`take_input_handler`，u5-l1 的 IME 协议入口）都是平台必须如实报告的。

帧与输入回调注册，以及绘制出口：

[src/platform.rs:L849-L866](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform.rs#L849-L866)
`frame_waker` 默认 `None`，平台的帧源需要主动唤醒时才覆写；`on_request_frame`/`on_input` 是 GPUI 与平台之间最重要的两个回调；`draw(&Scene)` 是每帧绘制的终点站；`sprite_atlas` 交出该窗口的精灵图集（4.3 节）。

「平台特有插槽」的三个样本：

- macOS 标签页：[src/platform.rs:L872-L896](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform.rs#L872-L896)（`tabbed_windows`、`merge_all_windows` 等，全部默认空/None）
- Windows 装饰与原生句柄：[src/platform.rs:L899-L907](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform.rs#L899-L907)（`get_raw_handle` 返回 `HWND`、`start_window_move`）
- 移动端：[src/platform.rs:L951-L964](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform.rs#L951-L964)（`set_back_handler`、`show_soft_keyboard`、`text_input_state_changed`）

测试转型口与无头渲染：

[src/platform.rs:L978-L988](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform.rs#L978-L988)
`as_test`（仅 test-support 编译）让测试代码把 `dyn PlatformWindow` downcast 回 `TestWindow`，是 4.4 节手动驱动帧的类型通道；`render_to_image` 把场景渲染成像素图供视觉测试。

配套的无头渲染器 trait：

[src/platform.rs:L991-L1010](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform.rs#L991-L1010)
`PlatformHeadlessRenderer` 定义「无窗口也能真渲染」的协议：`render_scene_to_image` 回读像素，`render_scene` 只提交不回读（注释说明它是 present 的无头等价物），`sprite_atlas` 交出图集。它让测试平台可以插上真实 GPU 渲染。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：算出「最小可行平台窗口」需要实现多少方法。
2. **操作步骤**：从 [src/platform.rs:L816](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform.rs#L816) 读到 L988，把方法分成「无函数体（必修）」与「带默认体（可选）」两栏计数；再对照 [src/platform/test/window.rs:L180-L390](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform/test/window.rs#L180-L390) 看 `TestWindow` 实际实现了哪些、忽略了哪些。
3. **需要观察的现象**：`TestWindow` 对大量「平台特有插槽」直接不覆写（继承空默认），必修方法多数只有一两行。
4. **预期结果**：必修方法约 35 个左右；这正是移植一个新平台时 `PlatformWindow` 侧的最小工作量基线。

#### 4.2.5 小练习与答案

**练习 1**：u4-l3 讲过「web 平台对空闲窗口停止请求帧」。结合本讲的 `frame_waker`，说明这个机制的接口面。

**答案**：`PlatformWindow::frame_waker` 默认返回 `None`（[src/platform.rs:L849-L851](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform.rs#L849-L851)）；平台覆写它返回一个 `Rc<dyn Fn()>`，GPUI 在「产生新帧需求」（如视图被 notify）时调用该 waker 唤醒平台帧源。测试平台用它来计数唤醒次数（[src/platform/test/window.rs:L313-L321](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform/test/window.rs#L313-L321)），窗口不主动请求帧的平台（web）靠它保证不漏帧。

**练习 2**：为什么 `draw` 的参数是 `&Scene` 而不是元素树或视图？

**答案**：`Scene` 是 GPUI 的绘制中间表示（图元批次 + 图集引用，u4-l3 讲过 `BoundsTree` 排序）。平台只消费场景、不认识元素/视图，这保证绘制管线的平台无关部分尽可能长，平台代码只做「场景 → GPU/Canvas」的最后一步翻译。

### 4.3 PlatformDispatcher、PlatformTextSystem 与 PlatformAtlas：调度、字体与图集

#### 4.3.1 概念说明

这三个是 `Platform` 交出的「子系统级」trait，分别解决：

- **`PlatformDispatcher`**：任务派发。前台/后台执行器共用它（u2-l5 已讲使用侧），这里是契约侧。全 trait 标注 `Send + Sync`。
- **`PlatformTextSystem`**：字体加载、字形查询、光栅化、行整形（u6-l5 讲过 `TextSystem` 如何委托它）。
- **`PlatformAtlas`**：精灵图集。字形、SVG、图片都要进图集变成可贴图的瓦片；窗口持有一份。

#### 4.3.2 核心流程

`PlatformAtlas` 的写入协议是一个「查或建」回调：

```text
调用方: atlas.get_or_insert_with(&key, &mut build)
  ├─ key 已存在 → 直接返回 AtlasTile（命中，不调用 build）
  └─ key 不存在 → 调用 build() 生成 (尺寸, 像素字节)
                     → 平台把像素上传进纹理图集 → 返回新 AtlasTile
调用方不再需要时: atlas.remove(&key)（如图集淘汰，u6-l7 的图片缓存释放）
```

#### 4.3.3 源码精读

[src/platform.rs:L1026-L1069](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform.rs#L1026-L1069)
`PlatformDispatcher` 的必修面：`is_main_thread`、`dispatch`（任意线程）、`dispatch_on_main_thread`、`dispatch_after`（定时）、`spawn_realtime`。两个值得注意的默认：`dispatch_on_main_thread_when_idle` 默认降级为 `dispatch_on_main_thread(Low)`（[L1035-L1042](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform.rs#L1035-L1042)，只有支持空闲调度的平台受益）；`now()` 默认返回真实时钟（[L1050-L1052](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform.rs#L1050-L1052)），test 平台覆写成假时钟——这正是 `#[gpui::test]` 能「快进时间」的根源（u7-l4 展开）。

[src/platform.rs:L1071-L1101](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform.rs#L1071-L1101)
`PlatformTextSystem` 从 `add_fonts` 到 `layout_line` 的完整字体后端契约。u6-l5 讲过 macOS 用 CoreText、Linux 用 cosmic-text 等；测试平台默认用 `NoopTextSystem`。

[src/platform.rs:L1323-L1336](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform.rs#L1323-L1336)
`PlatformAtlas` 只有两个必修方法：`get_or_insert_with` 与 `remove`。图集键 `AtlasKey` 分 Glyph/Svg/Image 三形态（[src/platform.rs:L1288-L1302](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform.rs#L1288-L1302) 的 `texture_kind` 按「是否 emoji、是否亚像素」决定进哪张纹理）。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：跟踪一次定时任务从 `cx.background_executor().timer(d)` 到平台原语的路径。
2. **操作步骤**：从 [src/platform.rs:L1033](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform.rs#L1033) 的 `dispatch_after` 出发，用 Grep 在 `src/platform_scheduler.rs` 与任一真实平台（如 `../gpui_linux/src/linux/dispatcher.rs`）里找它的实现与调用方，画出「timer 请求 → scheduler → dispatcher → 主线程派发」的调用链。
3. **需要观察的现象**：`PlatformScheduler`（u2-l5）把 `dispatch_after` 翻译成平台的定时原语；test 平台则把它记进假时钟队列。
4. **预期结果**：一条三跳调用链草图，并标注每跳所在的文件与行号。

#### 4.3.5 小练习与答案

**练习 1**：`PlatformTextSystem` 为什么必须是 `Send + Sync`，而 `TextSystem`（应用级包装）不是？

**答案**：字形光栅化等操作会被后台线程的加载任务并发调用，且要以 `Arc<dyn PlatformTextSystem>` 形式跨线程共享；而 `TextSystem`（u6-l5）叠加了每窗口缓存等主线程状态，归 `App` 所有、只在主线程用。

**练习 2**：`AtlasKey::texture_kind` 为什么把 emoji 与普通字形分开？

**答案**：普通字形适合亚像素抗锯齿的单色（Alpha）纹理，emoji 是彩色位图，需要多色纹理；混在一张纹理里会浪费通道或丢失质量（[src/platform.rs:L1288-L1301](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform.rs#L1288-L1301)）。

### 4.4 test platform：无窗口环境驱动帧

#### 4.4.1 概念说明

`platform/test/` 目录实现了整套平台抽象的「假版本」，供 `#[gpui::test]`（u7-l4）与无头测试使用。核心思路：**不跑事件循环，一切由测试代码手动触发**。

- `TestPlatform`：剪贴板是内存字段、prompt 进队列等回答、`restart` 走 oneshot 通道；`run` 直接 `unimplemented!()`——测试从不需要它。
- `TestWindow`：没有原生窗口（`HasWindowHandle` 返回 `NotSupported`），缩放系数固定 2.0，帧请求靠 `simulate_frame_request` 手动投递。
- `TestDispatcher`：委托 `scheduler` crate 的 `TestScheduler`——假时钟、确定性随机顺序、可 `run_until_parked`。

一个重要的可见性事实：`TestPlatform` 是 `pub(crate)`（[src/platform/test/platform.rs:L20-L21](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform/test/platform.rs#L20-L21)），crate 外**不能直接构造**。外部使用者通过两个封装间接触达它：`TestAppContext`（`#[gpui::test]` 宏自动构造）与 `HeadlessAppContext`（需要真实字体度量时）。

#### 4.4.2 核心流程

测试平台下「手动驱动一帧」的流程：

```text
#[gpui::test] 构造 TestAppContext
  └─ TestAppContext::build → TestPlatform::new(...) + App::new_app   (test_context.rs:127-137)
cx.add_window(|_, _| EmptyView)
  └─ TestPlatform::open_window → TestWindow::new                      (test/platform.rs:399-413)
     窗口初始为脏 → frame_waker 被唤醒 → frame_wake_count() >= 1
cx.notify() → 标脏 → 再次唤醒 waker（计数增加）
test_window.simulate_frame_request(RequestFrameOptions::default())
  └─ 调用 on_request_frame 注册的闭包 → Window::draw → PlatformWindow::draw
     （无 renderer 时 draw 为空操作，但场景照常组装）
```

#### 4.4.3 源码精读

`TestPlatform` 的字段就是它的「假世界」清单：

[src/platform/test/platform.rs:L20-L42](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform/test/platform.rs#L20-L42)
内存剪贴板（`Mutex<Option<ClipboardItem>>`）、prompt 队列、系统通知记录、以及 4.6 节的主角 `expect_restart`——一个 `RefCell<Option<oneshot::Sender<(Option<PathBuf>, Vec<OsString>)>>>`，把重启参数原样发给测试。

三个构造入口决定「假」的程度：

[src/platform/test/platform.rs:L106-L152](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform/test/platform.rs#L106-L152)
`new` 用 `NoopTextSystem`（最假）；`with_text_system` 换真实字体后端（要准确字形度量时）；`with_platform` 还能塞一个 headless renderer 工厂（要真 GPU 渲染/截图时）。注意 `Rc::new_cyclic` 建立 `Weak<Self>`——`TestWindow` 要回指平台查激活窗口。

生命周期方法的「测试语义」：

[src/platform/test/platform.rs:L338-L348](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform/test/platform.rs#L338-L348)
`run` 是 `unimplemented!()`（测试从不启动事件循环）；`quit` 空实现；`restart` 把 `(path, arguments)` 从 `expect_restart` 通道发出去——测试因此能断言「平台到底收到了什么」。

剪贴板的内存实现：

[src/platform/test/platform.rs:L538-L544](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform/test/platform.rs#L538-L544)
读写就是对一个 `Mutex<Option<ClipboardItem>>` 字段的克隆与赋值——测试里 `cx.write_to_clipboard` 后立刻能读回。

`TestWindow` 的手动帧源：

[src/platform/test/window.rs:L143-L158](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform/test/window.rs#L143-L158)
`frame_wake_count` 统计 waker 被唤醒次数；`simulate_frame_request` 取出 `on_request_frame` 注册的闭包手动调用一次——相当于平台的 vsync 到了。注释明说：测试显式投递帧，不耦合帧时序。

[src/platform/test/window.rs:L202-L204](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform/test/window.rs#L202-L204) 与 [L361-L368](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform/test/window.rs#L361-L368)
缩放系数硬编码 2.0（测试逻辑像素到物理像素换算的确定性来源）；`draw` 只有配置了 renderer 才真渲染，否则空操作——场景组装照常，只是不上屏。

`TestDispatcher` 的确定性调度：

[src/platform/test/dispatcher.rs:L52-L78](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform/test/dispatcher.rs#L52-L78)
`advance_clock` 拨假时钟（触发定时器）、`tick` 单步执行、`run_until_parked` 跑到没有可运行任务——u7-l4 的 `run_until_parked` 就是它。

两个「如何正确使用 TestPlatform」的官方范例：

- [src/app/test_context.rs:L127-L149](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/app/test_context.rs#L127-L149)：`TestAppContext::build` 用 `TestPlatform::new` + `App::new_app` 组装，并把 `GpuiMode` 设为 test。
- [src/app/headless_app_context.rs:L1-L9](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/app/headless_app_context.rs#L1-L9) 与 [L65-L101](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/app/headless_app_context.rs#L65-L101)：模块文档自述「用 `TestPlatform` 换掉 macOS 专属的无头上下文」，`with_platform` 接受任意 `PlatformTextSystem`（DirectWrite/CoreText/cosmic）与可选 renderer 工厂——**这就是注入自定义平台配置的标准姿势**，也演示了 `Application` 之外直接 `App::new_app(platform, ...)` 的用法。

#### 4.4.4 代码实践（本讲主实践·可运行）

1. **实践目标**：在无真实窗口的环境跑通「创建视图 → 手动驱动帧 → 验证重启参数转发」全链路。
2. **操作步骤**：
   - 第一步（跑现成测试，验证环境）：
     ```bash
     cargo test -p gpui test_frame_waker_fires_on_frame_demand
     cargo test -p gpui test_restart_preserves_path_and_arguments
     ```
     前者在 [src/window.rs:L6958-L6999](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window.rs#L6958-L6999)：`cx.add_window` 后用 `cx.test_window(...).simulate_frame_request(...)` 驱动帧并断言 `frame_wake_count` 的增减；后者在 [src/app.rs:L3116-L3136](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/app.rs#L3116-L3136)：`expect_restart` + `set_restart_path` + `restart`。
   - 第二步（写自己的变体）：在仓库的 scratch 分支上，往 `src/app.rs` 已有的 `#[cfg(test)]` 测试模块（`test_restart_preserves_path_and_arguments` 所在模块）里追加一个测试（**示例代码**，待本地验证）：
     ```rust
     #[gpui::test]
     async fn test_platform_draw_and_restart(cx: &mut TestAppContext) {
         // 1. 无真实窗口：开一个 TestWindow 支持的视图
         let window = cx.add_window(|_, _| EmptyView);
         let test_window = cx.test_window(window.into());
         // 2. 手动驱动两帧，观察唤醒计数
         let before = test_window.frame_wake_count();
         window.update(cx, |_, _, cx| cx.notify()).unwrap();
         assert!(test_window.frame_wake_count() > before, "notify 必须唤醒帧源");
         test_window.simulate_frame_request(Default::default());
         // 3. 验证重启参数转发（路径经 set_restart_path，参数此时为空 vec）
         let restart = cx.expect_restart();
         cx.update(|cx| {
             cx.set_restart_path("my-zed".into());
             cx.restart();
         });
         let (path, args) = restart.await.expect("restart was not requested");
         assert_eq!(path, Some("my-zed".into()));
         assert!(args.is_empty()); // 未经过 Application::with_restart_arguments
     }
     ```
     注意：`cx.test_window` 是 `pub(crate)`（[src/app/test_context.rs:L531](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/app/test_context.rs#L531)），所以这个测试必须写在 `crates/gpui` crate 内部；crate 外的等价做法是 `#[gpui::test]` + `cx.expect_restart()`（不触碰 `test_window`）。`EmptyView` 等具体类型名以你分支上现有测试的导入为准，可从 `src/window.rs` 测试模块抄。
3. **需要观察的现象**：两个现成测试通过；你的变体里 `notify` 之后唤醒计数增加、`simulate_frame_request` 之后不再自发增长；`restart.await` 拿到的路径与 `set_restart_path` 设的一致。
4. **预期结果**：三条断言全部通过。若第二步示例与本仓库 HEAD 的 API 有出入（如 `EmptyView` 导入路径），以现成测试的写法为准修正——这本身就是一个「对照真实测试学 API」的练习。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `TestPlatform::run` 是 `unimplemented!()` 而不是空实现？

**答案**：测试的生命周期由 `#[gpui::test]` 宏与 `TestAppContext::quit` 管理（构造 → 断言 → 收尾），从不进入平台事件循环。空实现会静默吞掉误调用（比如测试里误写了 `application.run(...)`），`unimplemented!()` 让这种误用立刻 panic 暴露（[src/platform/test/platform.rs:L338-L340](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform/test/platform.rs#L338-L340)）。

**练习 2**：`TestWindow::scale_factor` 固定返回 2.0，这对上层测试意味着什么？

**答案**：所有逻辑像素到物理像素的换算（`to_device_pixels`，u3-l4）在测试中是确定的乘 2，测试断言设备像素数值时不需要读真实显示器的 DPI（[src/platform/test/window.rs:L202-L204](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform/test/window.rs#L202-L204)）。

### 4.5 剪贴板的同步与异步读取

#### 4.5.1 概念说明

`read_from_clipboard` 是同步 API：立刻返回 `Option<ClipboardItem>`。这在桌面平台天经地义，但在浏览器上不成立——Web 的异步剪贴板 API（`navigator.clipboard.read()`）：

1. **要求安全上下文**（https 或 localhost），否则 `navigator.clipboard` 是 `undefined`；
2. **受权限门禁**，可能弹权限框、可能被用户拒绝；
3. 必须在**用户激活窗口内**同步发起。

于是 `Platform` 提供了第二个读取口 `read_from_clipboard_async`，返回 `Task<Result<Option<ClipboardItem>, ClipboardReadError>>`，错误类型 `ClipboardReadError` 区分三种失败，供上层给出不同的用户提示。

#### 4.5.2 核心流程

```text
调用方（能 await 就优先 async）
  │
  ├─ 桌面平台：默认实现 → Task::ready(Ok(read_from_clipboard()))   同步结果包成现成任务
  │
  └─ web 平台：覆写
       ├─ navigator.clipboard 不存在（非安全上下文）→ Err(Unavailable)
       ├─ 同步调用 read() 保住用户激活 → 前台 executor 里 await
       │    ├─ 权限被拒 → Err(Denied(msg))
       │    ├─ 遍历 items：text/plain → 字符串条目；可识别图片 MIME → 图片条目
       │    ├─ 有条目 → Ok(Some(item))
       │    ├─ 只有不认识的类型 → Err(UnsupportedContent)
       │    └─ 空剪贴板 → Ok(None)
```

#### 4.5.3 源码精读

trait 侧的两个读取口与默认实现：

[src/platform.rs:L310-L322](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform.rs#L310-L322)
文档注释写明设计：多数平台同步读取并返回现成任务；剪贴板访问「天然异步且受权限门禁」的平台（浏览器异步剪贴板 API）才覆写——在这些平台上同步版拿不到内容，能 await 的调用方应优先用异步版。

错误类型的三个变体：

[src/platform.rs:L2311-L2343](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform.rs#L2311-L2343)
`Unavailable`（环境不可用，如非安全上下文）、`Denied(String)`（用户拒绝权限/粘贴确认）、`UnsupportedContent`（内容无法转换成 `ClipboardItem`）。注释点明变体划分的动机：「调用方要把失败呈现给用户，不同条件需要不同的用户指引」。

web 平台的覆写（兄弟 crate `gpui_web`）：

[../gpui_web/src/platform.rs:L555-L568](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_web/src/platform.rs#L555-L568)
同步版 `read_from_clipboard` 直接返回 `None`——web 上它**永远**拿不到内容；异步版先用 `js_sys::Reflect::get` 探测 `navigator.clipboard` 是否存在（注释解释：避免对 `undefined` 调方法导致 wasm-bindgen abort），不存在则立刻 `Err(Unavailable)`；随后**同步**调用 `read()`——注释强调必须趁用户激活（如点击菜单项）还在有效期时发起。

[../gpui_web/src/platform.rs:L589-L617](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_web/src/platform.rs#L589-L617)
在前台 executor 里 await 结果后遍历剪贴板条目：只对 `text/plain` 与能识别的图片 MIME 取 blob（注释解释：网页复制常携带 `text/html` 与自定义格式，取了也是浪费或被拒）；无法识别的类型置 `saw_unsupported_type` 标志。收尾三分支：有条目 → `Ok(Some)`；只有不支持的类型 → `Err(UnsupportedContent)`；真空 → `Ok(None)`。

#### 4.5.4 代码实践（源码阅读型）

1. **实践目标**：体会「同一功能在权限模型不同的平台上裂解成两个方法」的演化路径。
2. **操作步骤**：先读 [src/platform.rs:L310-L322](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform.rs#L310-L322) 的默认实现，再用 Grep 在各平台 crate 里搜 `fn read_from_clipboard`：确认桌面平台只实现同步版、只有 `gpui_web` 覆写异步版。
3. **需要观察的现象**：`gpui_web` 里同步版是 `None` 桩、异步版约 60 行；桌面平台相反。
4. **预期结果**：总结出规则——「新平台默认只写同步版即可；仅当读取受权限/用户激活约束时才需要覆写异步版」。然后到 Zed 编辑器侧搜 `read_from_clipboard_async` 的调用处，看应用层如何在 UI 里消费 `ClipboardReadError`（如提示「剪贴板访问被拒绝」）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 web 的 `read()` 必须同步调用、结果才能 await？

**答案**：浏览器的剪贴板读取受用户激活（user activation）保护：`navigator.clipboard.read()` 必须在用户手势（如点击）的激活有效期内被调用。GPUI 的事件回调是同步的，所以先同步发起 `read()` 拿到 Promise，再放进前台 executor 里慢慢 await（[../gpui_web/src/platform.rs:L569-L573](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_web/src/platform.rs#L569-L573) 的注释）。

**练习 2**：`Ok(None)` 与 `Err(UnsupportedContent)` 在 web 实现里如何区分？

**答案**：剪贴板完全为空（没有任何条目）返回 `Ok(None)`——「没有东西可粘贴」是正常状态；有条目但全是 GPUI 不认识的 MIME 类型（置了 `saw_unsupported_type`）返回 `Err(UnsupportedContent)`——「有东西但粘不了」值得提示用户（[../gpui_web/src/platform.rs:L610-L616](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_web/src/platform.rs#L610-L616)）。

### 4.6 restart 参数转发：从 `with_restart_arguments` 到 `Platform::restart`

#### 4.6.1 概念说明

u2-l1 已经从生命周期角度讲过退出/重启；本讲从**平台契约**的角度补全链路。三个关键角色：

- `Application::with_restart_arguments(Vec<OsString>)`：启动前配置要透传的重启参数（如 `--user-data-dir`）。
- `App::set_restart_path(PathBuf)`：运行期更新重启用的二进制路径（典型场景：自更新下载了新版本后，重启到新二进制）。
- `Platform::restart(binary_path: Option<PathBuf>, arguments: Vec<OsString>)`：平台负责杀掉自己并拉起新进程。

两个签名细节值得注意：参数类型是 `OsString` 而非 `String`——命令行参数不保证是合法 UTF-8（Windows 路径、任意字节序列），GPUI 选择原样透传而不是有损转换；路径是 `Option`——`None` 表示重启当前二进制（平台自己定位）。

#### 4.6.2 核心流程

```text
配置期: Application::with_restart_arguments(args)          app.rs:211-214
          └─ 写入 App.restart_arguments（pub(crate) 字段）   app.rs:741
运行期: cx.set_restart_path(path)（可选，自更新后）          app.rs:1612-1614
触发:   cx.restart()                                        app.rs:1601-1609
          ├─ 先通知 restart_observers（on_app_restart 注册的回调） app.rs:2346
          └─ platform.restart(restart_path.take(),           取走并清空，二次 restart 不会重放
                              std::mem::take(&mut restart_arguments))
平台侧: macOS/Linux/Windows → exec 新进程；TestPlatform → expect_restart 通道发出
```

#### 4.6.3 源码精读

配置入口：

[src/app.rs:L210-L214](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/app.rs#L210-L214)
`with_restart_arguments` 只是往 `AppCell` 里的 `App.restart_arguments` 字段写入参数，返回 `self` 继续链式配置。

转发核心：

[src/app.rs:L1600-L1609](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/app.rs#L1600-L1609)
`App::restart` 先让 `restart_observers`（`on_app_restart` 注册的收尾回调，如保存状态）执行，再调用 `platform.restart(self.restart_path.take(), std::mem::take(&mut self.restart_arguments))`——`take` 保证路径与参数都是「一次性消费」，连续两次 restart 第二次会拿到空值。

运行期路径更新与平台契约：

[src/app.rs:L1611-L1614](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/app.rs#L1611-L1614) 与 [src/platform.rs:L133](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform.rs#L133)
`set_restart_path` 是 `App` 上的公开方法；`Platform::restart` 的签名 `fn restart(&self, binary_path: Option<PathBuf>, arguments: Vec<OsString>)` 就是平台收到的全部信息。

端到端验证测试（本讲的权威依据）：

[src/app.rs:L3116-L3136](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/app.rs#L3116-L3136)
`test_restart_preserves_path_and_arguments`：构造包含非 UTF-8 字节的 `--user-data-dir` 参数（Unix 分支用 `b"/tmp/zed data/\xff"`——这正是选 `OsString` 的原因）、设 `restart_path`、调 `cx.restart()`，然后从 `cx.expect_restart()` 拿到 `(path, arguments)` 断言逐项相等。注意 `super::Application(cx.app.clone())` 直接构造 `Application`（[src/app.rs:L146](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/app.rs#L146) 的元组字段对同 crate 测试模块可见），因为 `with_restart_arguments` 只挂在 `Application` 上。

测试平台侧的接收器：

[src/app/test_context.rs:L406-L411](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/app/test_context.rs#L406-L411)
`TestAppContext::expect_restart` 创建 oneshot 通道、把 Sender 塞进 `TestPlatform.expect_restart`，返回 Receiver——测试 `await` 它即可拿到平台「收到」的 `(Option<PathBuf>, Vec<OsString>)`；配合 [src/platform/test/platform.rs:L344-L348](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform/test/platform.rs#L344-L348) 的 `restart` 实现（取出 Sender 发送）完成闭环。

#### 4.6.4 代码实践

见 4.4.4 的主实践（第二步的第 3 部分就是 restart 验证）。这里补充一个独立小实践：

1. **实践目标**：亲眼看到参数类型必须是 `OsString` 的理由。
2. **操作步骤**：细读 [src/app.rs:L3116-L3122](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/app.rs#L3116-L3122)，注意 Unix 分支用 `OsString::from_vec(b"/tmp/zed data/\xff".to_vec())` 构造了一个含非法 UTF-8 字节的路径；思考若签名是 `Vec<String>` 这段代码会怎样。
3. **需要观察的现象**：`\xff` 无法通过 `String::from_utf8` 校验。
4. **预期结果**：结论——用 `String` 会迫使调用方要么报错、要么有损替换（`to_string_lossy` 变 `U+FFFD`），重启参数就不再是用户原始输入；`OsString` 原样透传是唯一无损选择。

#### 4.6.5 小练习与答案

**练习 1**：`App::restart` 里为什么要用 `take()` / `std::mem::take()` 而不是直接引用字段？

**答案**：`platform.restart` 接收所有权（`Option<PathBuf>` 与 `Vec<OsString>`）。用 `take` 把字段搬空：一是满足所有权转移，二是语义上重启参数是「一次性消费」——如果平台实现决定不重启（或 restart 失败后继续运行），再次 restart 不应重放旧参数（[src/app.rs:L1605-L1608](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/app.rs#L1605-L1608)）。

**练习 2**：`restart` 与 `quit` 都会结束进程，`on_app_quit` 与 `on_app_restart` 两个回调的分工是什么？

**答案**：`on_quit`（[src/platform.rs:L203](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform.rs#L203)）挂在退出链路（u2-l1：平台回调 → `App::shutdown`，收尾 future 有 200ms 预算）；`on_app_restart`（[src/app.rs:L2346](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/app.rs#L2346)）挂在重启链路，在 `platform.restart` 被调用**之前**同步执行——重启场景需要「把状态交给新进程」（如标记干净的退出状态）而非「清理」。

## 5. 综合实践

**任务：给你的「假平台」写一份验收报告。**

假设你要为一个新的操作系统（比如某个嵌入式 Linux 发行版）评估移植 GPUI 的工作量。请基于本讲源码完成：

1. **必修清单**：通读 [src/platform.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform.rs)，列出五个 trait（`Platform` / `PlatformWindow` / `PlatformDispatcher` / `PlatformTextSystem` / `PlatformAtlas`）各自**无默认实现**的方法数，估算总工作量。
2. **取舍表**：对带默认实现的「平台特有插槽」（macOS 标签页、Windows 装饰、Wayland layer shell、移动端、a11y），逐项决定你的平台「覆写 / 用默认」，并说明理由（如：无触摸屏 → 移动端全用默认）。
3. **参考实现排序**：确定阅读顺序——建议先读 `TestPlatform`（最小实现）、再读 `gpui_linux` 的 headless 路径（`../gpui_linux/src/linux/headless.rs`，无窗口但真渲染）、最后读完整平台。
4. **验证策略**：说明你会如何用 `HeadlessAppContext::with_platform`（[src/app/headless_app_context.rs:L65-L101](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/app/headless_app_context.rs#L65-L101)）注入你的字体后端与渲染器，用 `expect_restart` / `simulate_frame_request` 验收平台行为。

产出一份不超过两页的 Markdown 报告。这个练习把本讲全部六个模块（trait 职责、窗口契约、子系统 trait、test platform、剪贴板扩展点、restart 链路）串成一次真实的架构评估。

## 6. 本讲小结

- `Platform` 是应用级的操作系统门户（约 14 组职责），由 `App` 持有 `Rc<dyn Platform>`，不带 `Send/Sync`；默认实现分「能力缺失」（返回 None/false）与「等价简化」（委托其他方法）两种语义。
- `PlatformWindow` 是窗口级契约：查询 + 命令 + 回调注册（`on_request_frame`/`on_input`）+ 绘制出口（`draw(&Scene)`）；大量默认空实现构成 macOS/Windows/Wayland/移动端的「平台特有插槽」。
- `PlatformDispatcher` 是唯一 `Send + Sync` 的平台 trait（双执行器共享），`now()` 的可覆写性是测试假时钟的根源；`PlatformTextSystem` 与 `PlatformAtlas` 分别定义字体后端与精灵图集的写入协议。
- test platform 用「不跑事件循环、一切手动触发」换确定性：`TestPlatform::run` 是 `unimplemented!()`，帧靠 `TestWindow::simulate_frame_request` 投递，调度靠 `TestDispatcher` 的假时钟；crate 外通过 `#[gpui::test]` / `HeadlessAppContext` 间接触达（`TestPlatform` 为 `pub(crate)`）。
- 剪贴板有同步/异步两个读取口：默认实现把同步结果包成现成任务，web 平台因权限门禁（安全上下文、用户激活、可拒绝）覆写异步版，失败用 `ClipboardReadError` 的三变体区分用户指引。
- restart 链路：`with_restart_arguments` 配置 → `App::restart` 先通知 `on_app_restart` 回调、再以 `take` 一次性消费路径与参数 → `Platform::restart(Option<PathBuf>, Vec<OsString>)`；`OsString` 保证非 UTF-8 参数无损透传，测试用 `expect_restart` 通道端到端验证。

## 7. 下一步学习建议

- **下一讲 u7-l2（窗口管理）**：直接建立在本讲的 `open_window` / `PlatformWindow` / `WindowParams` 之上，进入 `WindowOptions`、多窗口与实体跨窗口迁移。
- **u7-l3（对话框、菜单与系统通知）**：会用到本讲 `Platform` 的 `prompt_for_paths`、`set_menus`、`show_system_notification` 方法族，并再次遇到 `TestPlatform` 的 prompt 队列与通知记录（`simulate_prompt_answer` 等）。
- **u7-l4（测试 GPUI 应用）**：把本讲的 `TestDispatcher` 假时钟、`TestAppContext` 展开成完整的测试方法论（`run_until_parked`、输入模拟、视觉测试）。
- **继续阅读源码**：对照读一个真实平台 crate（推荐 `gpui_linux/src/linux/headless.rs` 与 `gpui_macos/src/platform.rs` 的 `restart` 实现），比较它们与本讲 `TestPlatform` 在同一批 trait 上的实现复杂度差异——这是检验你是否真正理解平台抽象边界的最好方式。

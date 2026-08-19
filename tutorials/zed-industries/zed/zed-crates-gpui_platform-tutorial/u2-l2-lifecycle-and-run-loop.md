# 应用生命周期与运行循环：run、quit、restart 与回调注册

## 1. 本讲目标

本讲是 u2-l1「Platform trait 全景导览」之后的第一站下钻，聚焦八大方法分组中的**生命周期组**。学完本讲，你应该能够：

1. 描述 `Platform::run(on_finish_launching)` 在 Linux（calloop）、macOS（NSApplication）、Windows（Win32 消息循环）、Web（浏览器异步任务）上分别对应的事件循环入口，以及 `on_finish_launching` 回调在每种平台上被调用的时机差异。
2. 画出一次「优雅退出」的完整路径：`App::quit()` → `Platform::quit()` → 事件循环退出 → 平台触发 `on_quit` 回调 → `App::shutdown()` 收尾，并说明 `QuitMode` 如何决定「关闭最后一个窗口」是否等于「退出应用」。
3. 解释 `on_quit` / `on_reopen` / `on_system_wake` 三个回调注册方法在 Linux 侧如何经由 `PlatformHandlers` 结构体集中存放、在 macOS 侧如何绑定到 Objective-C 委托方法。
4. 说出 `on_app_lifecycle` 与 `on_memory_warning` 为什么是「桌面平台上永远不会被调用」的方法，以及 `AppLifecyclePhase` 四个阶段与 iOS/Android 系统回调的映射关系。

## 2. 前置知识

阅读本讲前，你只需要具备以下概念（不熟悉也没关系，下面用大白话解释）：

- **事件循环（event loop / run loop）**：GUI 程序的心脏。操作系统不会「主动调用」你的代码，而是把按键、鼠标移动、窗口重绘等事件放进队列；程序则反复执行「取事件 → 处理事件」的循环，直到收到退出指令。这个循环在 macOS 上叫 NSApplication run loop，在 Windows 上叫 Win32 消息循环，在 Linux 桌面上 GPUI 自己用 calloop 库搭了一个，在浏览器里则由浏览器本身充当。
- **回调注册（register callback）**：「先告诉平台，将来发生某事时请调用我这个闭包」。Rust 里通常以 `Box<dyn FnMut()>` 的形式把闭包交给平台保存。关键是**注册**和**触发**在时间上是分离的——注册发生在启动期，触发可能永远不发生（比如系统从不休眠）。
- **`Box<dyn FnOnce()>` 与 `Box<dyn FnMut()>` 的区别**：`FnOnce` 只能被调用一次（消耗自身），适合「启动完成」这类一次性事件；`FnMut` 可以被多次可变调用，适合「系统唤醒」这类可重复事件。本讲的 `run` 用前者，三个 `on_*` 回调用后者。
- **calloop**：Linux 桌面平台（Wayland/X11/headless 三后端共用）的事件循环库。它提供 `LoopSignal`（可以从小循环外部发出「停止」信号）和 channel（跨线程投递消息的事件源）。u4-l3 会专门讲它。
- **Subscription（订阅）**：gpui 中 `cx.on_system_wake(...)` 这类 App 层 API 返回一个 `Subscription` 值；它被 drop 时回调会被注销。想让回调活过当前作用域，需要 `.detach()` 或把它存进某个结构体字段。

承接 u2-l1 的结论：`Platform` trait 共 69 个方法、八大分组；本讲覆盖的 `run`/`quit`/`restart`/`activate`/`hide` 家族是**必需方法**（无默认实现），`on_app_lifecycle`/`on_memory_warning` 是**带默认空实现的移动端方法**。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [../gpui/src/platform.rs](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/platform.rs) | 契约层：`Platform` trait 的生命周期方法声明（L131-L137、L203-L222）与 `AppLifecyclePhase` 枚举（L746-L769） |
| [../gpui/src/app.rs](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/app.rs) | 应用层：`Application::run`、`App::quit/restart/shutdown`、`QuitMode`，以及启动期把 App 观察者桥接到平台回调的胶水代码 |
| [../gpui_linux/src/linux/platform.rs](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_linux/src/linux/platform.rs) | Linux 外壳：`LinuxPlatform` 对 run/quit/restart/回调注册的实现，`PlatformHandlers` 与 `LinuxCommon` 两个聚合结构，以及监听系统唤醒的 DBus 代码 |
| [../gpui_linux/src/linux/headless/client.rs](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_linux/src/linux/headless/client.rs) | Linux headless 后端：最简洁的 calloop 事件循环样本，可当作理解 Linux `run` 的最短路径 |
| [../gpui_linux/src/linux/x11/client.rs](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_linux/src/linux/x11/client.rs) | Linux X11 后端：与 headless 同构的 `run` 实现 |
| [../gpui_linux/src/linux/wayland/client.rs](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_linux/src/linux/wayland/client.rs) | Linux Wayland 后端：calloop 事件源的注册现场 |
| [../gpui_macos/src/platform.rs](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_macos/src/platform.rs) | macOS 实现：NSApplication 包装、Objective-C 委托方法（`did_finish_launching`/`will_terminate`/`should_handle_reopen`/`on_system_wake`） |
| [../gpui_windows/src/platform.rs](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_windows/src/platform.rs) | Windows 实现：GetMessageW 消息循环、`PostQuitMessage` 退出、电源通知注册 |
| [../gpui_web/src/platform.rs](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs) | Web 实现：不拥有事件循环，「退出」只是打日志 |
| [../gpui/examples/window.rs](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/examples/window.rs) | 官方示例：`cx.quit()`、`cx.hide()`、`cx.activate()` 的真实用法，本讲实践任务的模板 |

## 4. 核心概念与源码讲解

### 4.1 Platform::run：启动应用并进入平台事件循环

#### 4.1.1 概念说明

`run` 是整个应用真正「活起来」的那一刻。它做两件事：

1. **在合适的时机执行一次 `on_finish_launching` 回调**——应用层所有的初始化（打开第一个窗口、绑定快捷键、注册观察者）都写在这个回调里；
2. **进入平台事件循环并阻塞**，直到平台认为应用该结束了（用户退出、最后一个窗口关闭、收到终止信号）。

关键在于「合适的时机」四个字。每个平台对「启动完成」的定义不同：macOS 必须等 AppKit 发出 `applicationDidFinishLaunching` 通知后才算启动完成（太早创建窗口会被系统拒绝），而 Linux/Windows 的事件循环由 GPUI 自己搭，回调可以先执行再进循环。所以契约只规定签名，不规定时机——时机是各平台的私有语义。

另一个容易忽略的点：`run` 的参数是 `Box<dyn 'static + FnOnce()>`，没有参数、没有返回值。它不给你 `cx`！App 状态是通过闭包捕获的 `Rc<AppCell>` 传进去的（见 4.1.3 第一段源码）。这是一个典型的「桥接层」设计：平台层对 `App` 一无所知，gpui 的应用层把「拿到 App 引用执行用户代码」打包成一个无参闭包递给平台。

#### 4.1.2 核心流程

以 Linux 为例（最清晰），从 `application()` 到事件循环的完整路径：

```text
gpui_platform::application()                      # u1-l2 讲过：构造 Rc<dyn Platform>
  └─ gpui::Application::with_platform(platform)
       └─ 用户调用 application.run(on_finish_launching)   # 注意：run 在 Application 上
            └─ platform.run(Box::new(|| {                  # 契约入口
                 let cx = &mut *app_cell.borrow_mut();     # 闭包内重建 App 引用
                 on_finish_launching(cx);                  # 执行用户初始化（仅一次）
               }))
                 ├─ LinuxPlatform::run:
                 │    1. on_finish_launching()             # 先执行回调
                 │    2. LinuxClient::run(&self.inner)     # 再进入 calloop 循环（阻塞）
                 │    3. 循环退出后触发 quit 回调           # 见 4.2
                 └─ 返回（进程随后结束）
```

四个平台的 `on_finish_launching` 触发时机对照：

| 平台 | 事件循环本体 | 回调触发时机 | `run` 返回时机 |
| --- | --- | --- | --- |
| Linux | calloop `EventLoop::run`（三后端各自持有） | 进循环**之前**，同步执行 | calloop 循环被 `LoopSignal::stop` 停止后 |
| macOS | `NSApplication.run()`（AppKit 拥有） | AppKit 发 `applicationDidFinishLaunching` 时，由委托方法转调（**进循环之后**） | `terminate:` 完成后 |
| Windows | `GetMessageW`/`DispatchMessageW` 循环 | 进循环**之前**，同步执行 | `PostQuitMessage(0)` 使 GetMessageW 返回 0 后 |
| Web | 浏览器事件循环（平台不拥有） | 图形初始化异步完成**之后**（`spawn_local` 的异步任务里） | `run` 立即返回，从不阻塞 |

#### 4.1.3 源码精读

**契约签名**——五个必需方法，没有任何默认实现，每个平台都必须给出自己的版本：

[../gpui/src/platform.rs:L131-L137](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/platform.rs#L131-L137)
这段声明了 `run`（带启动回调）、`quit`、`restart`（带可选二进制路径与参数列表）、`activate`、`hide`、`hide_other_apps`、`unhide_other_apps`。注意 `run` 的回调类型是 `FnOnce`——启动只发生一次。

**应用层如何包装 `run`**——`Application::run` 把带 `&mut App` 参数的用户闭包适配成平台需要的无参闭包：

[../gpui/src/app.rs:L233-L243](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/app.rs#L233-L243)
`this` 是 `Rc<AppCell>` 的克隆，被移动进闭包；平台调用闭包时先 `borrow_mut` 拿到 `&mut App`，再执行用户回调。这就是「平台层不认识 App」的桥接手法。紧随其后的 [run_embedded（L245-L265）](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/app.rs#L245-L265) 面向「事件循环属于别人」的嵌入场景（如 wasm guest）：它调用同样的 `platform.run`，但返回一个 `ApplicationHandle` 让宿主后续可以重新进入应用——它假设平台的 `run` 只执行回调、立即返回，Web 平台正是这么实现的。

**Linux 外壳的 `run`**——三行结构：回调 → 循环 → 退出钩子：

[../gpui_linux/src/linux/platform.rs:L267-L278](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_linux/src/linux/platform.rs#L267-L278)
先同步执行 `on_finish_launching`，然后调用 `LinuxClient::run`（trait 方法，声明于 [platform.rs:L96](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_linux/src/linux/platform.rs#L96)，阻塞在 calloop 上），循环返回后把 `PlatformHandlers` 里的 quit 回调取出来执行。**quit 回调在 `run` 的尾声触发**——这是 Linux 退出的关键设计，4.2 会展开。

**Linux 后端真正的事件循环**——headless 后端是全仓库最短的 `run` 实现：

[../gpui_linux/src/linux/headless/client.rs:L133-L142](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_linux/src/linux/headless/client.rs#L133-L142)
`event_loop.take()` 把 `Option<EventLoop>` 拿走——用 Option 的空值语义保证「run 只能被调用一次」，重复调用直接 panic（`expect("App is already running")`）。X11 后端的实现几乎逐字相同，只是用 `context` + `log_err` 优雅降级而非 panic：[../gpui_linux/src/linux/x11/client.rs:L1792-L1805](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_linux/src/linux/x11/client.rs#L1792-L1805)。

**macOS 的 `run`**——回调时机与 Linux 相反，启动回调被**推迟**到 AppKit 通知：

[../gpui_macos/src/platform.rs:L491-L518](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_macos/src/platform.rs#L491-L518)
非 headless 路径下，`on_finish_launching` 被存进 `state.finish_launching`（`Option<Box<dyn FnOnce()>>`，字段声明见 [L187](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_macos/src/platform.rs#L187)）而不立即执行；接着把平台指针塞进 NSApplication 与委托对象的实例变量，然后调用 `app.run()` 进入 AppKit 循环。headless 路径则跳过 AppKit，直接执行回调后进入裸 `CFRunLoopRun()`。真正执行回调的地方是 Objective-C 委托方法：

[../gpui_macos/src/platform.rs:L1281-L1317](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_macos/src/platform.rs#L1281-L1317)
`did_finish_launching` 响应 AppKit 的「启动完成」通知：设置激活策略、注册键盘布局/热状态/系统唤醒观察者，最后 `state.finish_launching.take()` 取出回调执行。这个委托方法在类构造时被挂到 selector 上：[../gpui_macos/src/platform.rs:L84-L99](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_macos/src/platform.rs#L84-L99) 把 `applicationWillFinishLaunching:`、`applicationDidFinishLaunching:`、`applicationShouldHandleReopen:hasVisibleWindows:`、`applicationWillTerminate:` 四个 selector 绑到 Rust 函数。

**Windows 的 `run`**——教科书式 Win32 消息循环：

[../gpui_windows/src/platform.rs:L447-L465](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_windows/src/platform.rs#L447-L465)
先执行回调、启动垂直同步线程，然后 `GetMessageW` 死循环（返回 0 即退出循环），最后调用 quit 回调——退出钩子的布局与 Linux 完全一致。

**Web 的 `run`**——唯一不阻塞的实现：

[../gpui_web/src/platform.rs:L280-L304](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L280-L304)
回调被推迟到**图形初始化成功之后**：`spawn_local` 启动一个浏览器异步任务，等 WebGPU/WebGL surface 准备好才执行 `on_finish_launching()`；初始化失败则显示「图形不可用」提示。`run` 函数本身立即返回，这正是 `run_embedded` 文档所说的「事件循环属于别人」的形态（u7 会展开）。

#### 4.1.4 代码实践

**实践：用日志验证「回调先于事件循环」的执行顺序**（以 Linux 为例，其他平台可类比）。

1. **实践目标**：亲眼确认 `on_finish_launching` 在 Linux 上于事件循环启动前执行，且只执行一次。
2. **操作步骤**：
   1. 参照 [../gpui/examples/window.rs:L311-L337](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/examples/window.rs#L311-L337) 写一个最小程序（示例代码，非项目原文件）：

      ```rust
      use gpui::{App, WindowOptions};
      use gpui_platform::application;

      fn main() {
          println!("[main] 调用 run 之前");
          application().run(|cx: &mut App| {
              println!("[run 回调] on_finish_launching 执行，线程 {:?}", std::thread::current().id());
              cx.open_window(WindowOptions::default(), |_, cx| {
                  cx.new(|_| gpui::Empty)
              })
              .unwrap();
          });
          println!("[main] run 返回之后");
      }
      ```

   2. 用 `cargo run` 运行（在 zed 仓库内可参照 u1-l2 的独立小 crate 方案，需开启 gpui 的对应 feature）。
   3. 关闭窗口让程序退出，观察三行日志的顺序。
3. **需要观察的现象**：三行日志按 `[main] 调用 run 之前` → `[run 回调]` → （事件循环期间无输出）→ `[main] run 返回之后` 排列；`[run 回调]` 只出现一次。
4. **预期结果**：证明 `run` 是阻塞调用、回调只执行一次且在循环前同步执行。若在 macOS 上重复此实验，`[run 回调]` 的打印会晚于窗口服务初始化（因为要等 `did_finish_launching`）。具体时序「待本地验证」（尤其 macOS 分支）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `run` 的回调类型是 `FnOnce` 而 `on_quit` 的是 `FnMut`？

答案：启动完成是不可重复事件，闭包会被消耗掉执行一次，`FnOnce` 在类型上表达了这个约束（macOS 侧甚至用 `Option<Box<dyn FnOnce()>>` 的 `take()` 来体现「取走即执行」）；而退出前清理、系统唤醒可能（在语义上）发生多次，`FnMut` 允许平台反复调用同一个闭包。Linux 的 `handle_system_wake` 里 `take()` 后再 `get_or_insert(callback)` 放回去的写法，就是在 `FnMut` 语义下避免闭包被 move 走的手法。

**练习 2**：Linux `LinuxPlatform::run` 中，`on_finish_launching()` 执行时窗口还没建立，那回调里 `cx.open_window` 为什么能成功？

答案：开窗走的是 `Platform::open_window`（u3-l1 的主题），它并不依赖事件循环已经在运转——Wayland/X11 连接在 `current_platform` 构造期就已建立，`open_window` 只是把窗口对象注册进客户端状态。事件循环的作用是**分发后续事件**（输入、重绘、任务调度），而不是提供开窗能力。

**练习 3**：`Application::run_embedded` 的文档说嵌入式平台的 `Platform::run` 会「调用回调并立即返回」。对照 4.1.3 的四个实现，哪些平台符合这个描述？

答案：只有 Web。Linux/macOS/Windows 的 `run` 都会阻塞到应用结束；Web 的 `run` 把回调塞进 `spawn_local` 的异步任务后立刻返回，事件循环由浏览器拥有。所以 `run_embedded` 目前实际服务于 wasm 宿主场景。

### 4.2 Platform::quit 与 restart/activate/hide 家族：退出与进程控制

#### 4.2.1 概念说明

`quit` 的语义是「请求平台事件循环结束」，但它**不是**立即杀死进程，而是发出一个「该退出了」的信号。真正优雅的地方在于退出路径的收尾顺序：事件循环退出后、`run` 返回前，平台会触发 `on_quit` 注册的回调，而这个回调在 gpui 内部连接到 `App::shutdown()`——给所有注册了 `on_app_quit` 的模块 200 毫秒完成清理（写盘、断开连接）。

`restart` 则是「先安排好接班人再退场」：spawn 一个独立的 shell 进程，让它轮询等待当前进程退出，然后用同样路径重新拉起应用。三个桌面平台各自用本平台的 shell 方言实现了同一个脚本逻辑。

`activate`/`hide`/`hide_other_apps`/`unhide_other_apps` 是 macOS 有真实语义、其他平台大多降级的一组方法：macOS 的应用有「激活」概念（点 Dock 图标把应用带到前台），而 Linux/Windows 上这些调用大多只打一行日志。

#### 4.2.2 核心流程

一次完整优雅退出的时序（Linux）：

```text
用户按 cmd-q / 关掉最后一个窗口 / 代码调用 cx.quit()
  └─ App::quit()                          # app.rs:1008
       └─ Platform::quit()
            └─ common.signal.stop()        # calloop LoopSignal：请求事件循环停止
                 └─ calloop 循环退出
                      └─ LinuxPlatform::run 继续：取出 callbacks.quit 并调用   # platform.rs:272-277
                           └─ （gpui 在 App::new_app 里注册的那个闭包）        # app.rs:912-919
                                └─ App::shutdown()                           # app.rs:957
                                     ├─ 执行所有 on_app_quit 观察者（限时 200ms）
                                     ├─ 清空 windows
                                     └─ flush_effects
                 └─ run 返回 → main 结束 → 进程退出
```

关窗是否触发退出由 `QuitMode` 决定（[app.rs:L322-L332](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/app.rs#L322-L332)）：

- `Default`：macOS 用 `Explicit`（应用可以无窗口存活，点 Dock 重新开窗），其他平台用 `LastWindowClosed`；
- `LastWindowClosed`：最后一个窗口关闭即退出；
- `Explicit`：只有显式调用 `App::quit` 才退出。

#### 4.2.3 源码精读

**App 层入口**——`App::quit` 只有一行，把决定权完全交给平台：

[../gpui/src/app.rs:L1008-L1010](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/app.rs#L1008-L1010)
转发到 `self.platform.quit()`。「关窗→退出」的判定则在窗口移除逻辑里：

[../gpui/src/app.rs:L1879-L1887](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/app.rs#L1879-L1887)
最后一个窗口被移除后，按 `quit_mode` 判断是否调用 `cx.quit()`；`Default` 模式在编译期用 `cfg!(not(target_os = "macos"))` 分岔——这就是「Linux/Windows 关掉最后一个窗口应用就退出，macOS 不会」的出处。

**Linux 的 quit**——一行代码，靠 calloop 的停止信号：

[../gpui_linux/src/linux/platform.rs:L280-L282](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_linux/src/linux/platform.rs#L280-L282)
`LoopSignal::stop()` 让正在 `event_loop.run()` 里转的循环在处理完当前迭代后返回；随后 4.1.3 读过的 `LinuxPlatform::run` 尾声触发 quit 回调。

**macOS 的 quit**——刻意做成异步，原因写在注释里：

[../gpui_macos/src/platform.rs:L520-L538](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_macos/src/platform.rs#L520-L538)
如果同步调用 `terminate:`，AppKit 会立刻同步触发所有窗口的 `on_close` 回调；若调用方此时还持有 App 状态的借用，就会双重借用 panic。所以把 `terminate:` 派发到主队列**下一轮**执行（`exec_async_f`），保证当前调用栈展开、借用释放后再终止。这是跨语言 FFI 边界上常见的重入陷阱，值得记住。

**Windows 的 quit**——Win32 标准姿势：

[../gpui_windows/src/platform.rs:L467-L471](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_windows/src/platform.rs#L467-L471)
把 `PostQuitMessage(0)` 作为一个任务派发到前台执行器（而不是直接调用），这让退出请求和其他前台任务一样排队——同样是为了避免在任意调用栈里立即终止。`PostQuitMessage` 会使 `GetMessageW` 返回 0，消息循环结束，`run` 尾声触发 quit 回调（[L463-L464](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_windows/src/platform.rs#L463-L464)）。

**Web 的 quit**——直接拒绝：

[../gpui_web/src/platform.rs:L306-L308](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L306-L308)
浏览器里页面无权终止自己所在的标签页，只能打一条 warn 日志。这是「契约统一、能力降级」的典型样本。

**restart 三兄弟**——同一逻辑的三种 shell 方言。Linux 版：

[../gpui_linux/src/linux/platform.rs:L288-L338](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_linux/src/linux/platform.rs#L288-L338)
取当前 PID 与可执行路径，spawn 一个 `bash -c` 子进程，脚本用 `kill -0` 轮询等待旧进程消失，再以相同参数重新启动它（`process_group(0)` 让接班进程与旧进程组解耦）；spawn 成功后立刻 `self.quit()`。macOS 版（[L540-L586](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_macos/src/platform.rs#L540-L586)）用 `open` 命令处理 `.app` bundle；Windows 版（[L473-L528](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_windows/src/platform.rs#L473-L528)）用 PowerShell 脚本加 `ZED_RESTART_*` 环境变量传参，并特意把 spawn 推迟到前台执行器——注释解释了 `CreateProcessW` 会泵消息循环、可能引发 `AppCell` 双重借用。

App 层的 `restart` 入口在 [app.rs:L1595-L1603](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/app.rs#L1595-L1603)：先执行 `on_app_restart` 观察者（文档明确它们**先于** `on_app_quit` 回调，见 [L2330-L2343](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/app.rs#L2330-L2343)），再把 `restart_path`/`restart_arguments` 传给平台。

**activate/hide 家族的降级姿态**——Linux 全部打日志了事：

[../gpui_linux/src/linux/platform.rs:L340-L354](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_linux/src/linux/platform.rs#L340-L354)
四个方法都是 `log::info!("... is not implemented on Linux, ignoring the call")`——不 panic、不返回错误，安静降级。macOS 则有完整实现（[L588-L614](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_macos/src/platform.rs#L588-L614)）：`activateIgnoringOtherApps:`、`hide:`、`hideOtherApplications:`、`unhideAllApplications:` 一一对应 AppKit API。官方示例 [../gpui/examples/window.rs:L251-L265](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/examples/window.rs#L251-L265) 演示了 `cx.hide()` 后 3 秒再 `cx.activate(false)` 恢复的完整用法。

**退出的收尾：App::shutdown**——quit 回调的最终目的地：

[../gpui/src/app.rs:L957-L979](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/app.rs#L957-L979)
取出所有 `on_app_quit` 观察者生成 future，清空窗口，`block_with_timeout(SHUTDOWN_TIMEOUT, ...)` 限时等待全部完成——超时是 200 毫秒（[app.rs:L77](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/app.rs#L77) 定义 `SHUTDOWN_TIMEOUT`），防止某个模块卡死拖住整个退出。观察者的注册入口是 [App::on_app_quit（L2310-L2328）](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/app.rs#L2310-L2328)，签名是 `FnMut(&mut App) -> Fut`——允许异步清理。

#### 4.2.4 代码实践

**实践：给退出路径加日志，跟踪 quit → on_app_quit 的顺序**。

1. **实践目标**：验证优雅退出链路上各钩子的执行顺序与 200ms 超时的存在。
2. **操作步骤**：
   1. 在 4.1.4 的程序里补充退出相关注册（示例代码）：

      ```rust
      application().run(|cx: &mut App| {
          cx.on_app_quit(|_| async {
              println!("[on_app_quit] 开始清理");
              // 在这里 std::thread::sleep(300) 可观察超时日志（未验证）
          })
          .detach();
          cx.open_window(/* ... */).unwrap();
      });
      ```

   2. 用快捷键触发退出：参照 [window.rs:L334-L335](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/examples/window.rs#L334-L335) 定义 `actions!(demo, [Quit]);` 并 `cx.bind_keys([KeyBinding::new("cmd-q", Quit, None)])` + `cx.on_action(|_: &Quit, cx| cx.quit())`（Linux 上建议绑定 `ctrl-q`）。
   3. 运行后按快捷键退出，观察日志；再改用直接关闭窗口退出，对比是否走了同样的链路。
3. **需要观察的现象**：`[on_app_quit]` 在窗口关闭/快捷键两种退出方式下都恰好打印一次，且打印发生在进程结束之前。
4. **预期结果**：两种触发方式最终汇入同一条 `Platform::quit → 事件循环退出 → on_quit 回调 → shutdown` 链路。若把清理改成耗时超过 200ms 的操作，应能看到 `timed out waiting on app_will_quit` 错误日志。「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：Linux 上 `Platform::quit()` 被调用后，`on_quit` 回调大约多久之后执行？它一定执行吗？

答案：不是立即执行。`signal.stop()` 只是请求停止，calloop 要等当前迭代处理完毕、循环返回后才轮到 `LinuxPlatform::run` 尾声取 quit 回调执行，所以延迟取决于当时循环里还有多少未处理事件。它是「尽力执行」：若进程被 `kill -9` 这类外部信号直接杀死，循环根本没有机会正常返回，`on_quit` 不会执行——这正是第 5 节综合实践要观察的差异。

**练习 2**：`QuitMode::Default` 下，同一个 GPUI 应用在 macOS 和 Linux 上「关闭最后一个窗口」的行为有何不同？为什么契约要留这个差异？

答案：macOS 上应用继续运行（`Default` 等价 `Explicit`），Linux/Windows 上应用退出（等价 `LastWindowClosed`，见 app.rs:1879-1887 的 `cfg!(not(target_os = "macos"))`）。原因是桌面习惯差异：macOS 用户预期点 Dock 图标应用能重新开窗（对应 `on_reopen` 回调），而 Linux/Windows 用户预期关掉所有窗口即结束程序。GPUI 把平台习惯编码进默认值，同时允许 `with_quit_mode` 覆盖。

**练习 3**：为什么三个桌面平台的 `restart` 都要 spawn 一个外部 shell 进程来等待和重启，而不是在进程内先清理再 `exec`？

答案：因为要保证「旧进程完全退出后再启动新进程」，两者不能同时在运行（会抢占单实例锁、窗口、端口等资源），而进程无法在自己的代码里等自己退出。外部 shell 进程与旧进程无共享状态，可以安全地用轮询（`kill -0` / `Get-Process`）等待旧 PID 消失再拉起新进程；`process_group(0)` 进一步把接班进程放进独立进程组，避免被旧进程组的信号波及。

### 4.3 回调注册三件套与 Linux 的 PlatformHandlers

#### 4.3.1 概念说明

契约里三个桌面回调注册方法（[platform.rs:L203-L205](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/platform.rs#L203-L205)）：

- `on_quit`：进程即将退出时触发（各平台时机见 4.2）；
- `on_reopen`：用户「重新打开」应用时触发——典型场景是 macOS 上点击 Dock 图标且应用已无窗口；
- `on_system_wake`：系统从睡眠中唤醒时触发，应用可借此重绘界面、重连网络、校准定时器。

这三个方法都是**必需方法**（无默认实现），因为每个平台存放和触发回调的方式完全不同。Linux 把所有回调集中存进一个 `PlatformHandlers` 结构体（本讲的核心模块之一）；macOS 分散存在 `MacPlatformState` 的各个 `Option<Box<dyn FnMut()>>` 字段里；Windows 存在平台的 callbacks 状态里。

`PlatformHandlers` 的价值在于架构分层：`LinuxPlatform` 是泛型外壳（`LinuxPlatform<P>`，P 是 Wayland/X11/headless 后端），三后端共享的回调、菜单、通知状态不能放在任何一个后端里，于是有了 `LinuxCommon` 聚合体——`PlatformHandlers` 是它其中的 `callbacks` 字段。外壳的 `on_quit` 等方法统一写入这里，后端事件循环触发的也是这里，三后端自动获得一致的回调行为。

#### 4.3.2 核心流程

以最复杂的 `on_system_wake` 为例，Linux 上的完整链路（七步）：

```text
1. App::new_app 启动期调用 platform.on_system_wake(closure)     # app.rs:900-910
   （closure 的内容：upgrade Rc<AppCell> → 遍历 system_wake_observers）
2. LinuxPlatform::on_system_wake：
   callbacks.system_wake = Some(callback)                        # platform.rs:562-567
   并首次调用 start_wake_listener()（懒启动，只此一次）
3. start_wake_listener 在 smol 后台线程跑 listen_for_system_wake
4. 该协程连上系统 DBus，订阅 org.freedesktop.login1.Manager
   的 PrepareForSleep 信号                                     # platform.rs:205-227
5. 系统唤醒（参数 sleeping=false）→ wake_sender.send(())         # calloop channel
6. 事件循环里注册的 wake_receiver 事件源收到消息
   → client.with_common(|common| common.handle_system_wake())    # headless: client.rs:40-46
7. handle_system_wake 取出 callbacks.system_wake 执行
   → 第 1 步的 closure → 遍历 App 的 system_wake_observers
   → 用户在 cx.on_system_wake(...) 里注册的回调最终被调用      # app.rs:1349-1362
```

注意第 1 步和第 7 步之间隔着「平台原始事件 → DBus → calloop channel → 平台回调 → App 观察者」五层转发，这是 gpui 把「平台差异」挡在 `App` 之外的标准做法。

#### 4.3.3 源码精读

**契约声明**——三个回调都是必需方法：

[../gpui/src/platform.rs:L203-L205](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/platform.rs#L203-L205)
`on_quit`、`on_reopen`、`on_system_wake` 均接收 `Box<dyn FnMut()>`。与下一节将看到的 `on_app_lifecycle`（默认空实现）形成对照：桌面回调是每个平台必须认真实现的。

**Linux 的回调仓库**——`PlatformHandlers`：

[../gpui_linux/src/linux/platform.rs:L106-L116](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_linux/src/linux/platform.rs#L106-L116)
九个 `Option<Box<dyn FnMut...>>` 字段，覆盖打开 URL、退出、重开、菜单动作、键盘布局变化、系统唤醒。`Option` 表达「回调可能还没注册」；`FnMut` 允许重复触发。它作为 `callbacks` 字段存在于 [LinuxCommon（L118-L136）](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_linux/src/linux/platform.rs#L118-L136)——同一个结构还聚合了执行器、文本系统、`LoopSignal` 等三后端共享的全部状态。

**Linux 外壳的三个注册方法**——写入 `PlatformHandlers`：

[../gpui_linux/src/linux/platform.rs:L550-L567](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_linux/src/linux/platform.rs#L550-L567)
`on_quit`/`on_reopen` 是纯赋值；`on_system_wake` 赋值之外还调用 `start_wake_listener()`——懒启动设计：没人关心系统唤醒就不建立 DBus 连接。

**唤醒监听器与回调触发**——三个函数接力：

[../gpui_linux/src/linux/platform.rs:L180-L202](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_linux/src/linux/platform.rs#L180-L202)
`start_wake_listener` 用 `wake_listener_started` 标志保证只启动一次；`handle_system_wake` 用 `take()` → 调用 → `get_or_insert` 放回的手法取得 `FnMut` 的可变调用权（因为回调存在 `self.callbacks` 里，直接 `&mut` 调用会与 `with_common` 的闭包借用冲突）。

[../gpui_linux/src/linux/platform.rs:L205-L227](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_linux/src/linux/platform.rs#L205-L227)
`listen_for_system_wake` 通过 ashpd 的 zbus 连接系统总线，向 `org.freedesktop.login1` 的 `PrepareForSleep` 信号订阅：参数 `sleeping == false` 表示「从睡眠中醒来」，此时向 calloop channel 发一个空消息。这段代码只在 `wayland`/`x11` feature 开启时编译（`#[cfg(all(target_os = "linux", any(feature = "wayland", feature = "x11")))]`）。

**事件循环侧的接收**——三后端各自把 wake 通道注册为 calloop 事件源。headless 版最短：

[../gpui_linux/src/linux/headless/client.rs:L28-L46](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_linux/src/linux/headless/client.rs#L28-L46)
`LinuxCommon::new` 返回三元组 `(common, main_receiver, wake_receiver)`——主任务通道和唤醒通道并列；两个 channel 都通过 `insert_source` 挂进 calloop，wake 事件到达即调用 `handle_system_wake`。Wayland 后端同样三步（[wayland/client.rs:L743-L772](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_linux/src/linux/wayland/client.rs#L743-L772)），X11 后端见 [x11/client.rs:L312-L341](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_linux/src/linux/x11/client.rs#L312-L341)。

**App 层的桥接**——`App::new_app` 在启动期把平台回调接到 App 观察者集合：

[../gpui/src/app.rs:L900-L910](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/app.rs#L900-L910)
注册 `on_system_wake`：闭包里用 `Rc::downgrade` 弱引用升级拿到 App（避免平台持有强引用导致 App 无法释放），然后遍历 `system_wake_observers`。紧接着的 [L912-L919](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/app.rs#L912-L919) 注册 `on_quit`，内容是调用 `cx.shutdown()`——把 4.2 的两个线程接起来。用户侧入口是 [App::on_system_wake（L1349-L1362）](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/app.rs#L1349-L1362)，返回 `Subscription`，注册即激活（`activate()` 立即跑一次观察者）。

**macOS 的同名链路**——回调存在 `MacPlatformState`（字段见 [L168-L194](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_macos/src/platform.rs#L168-L194)），触发点全部是 Objective-C 委托方法：

[../gpui_macos/src/platform.rs:L971-L988](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_macos/src/platform.rs#L971-L988)
`on_system_wake` 注册回调时，若尚未注册过观察者，则向 AppKit 委托对象挂载 `NSWorkspaceDidWakeNotification` 观察者（[register_system_wake_observer，L1319-L1331](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_macos/src/platform.rs#L1319-L1331)）。注意 `did_finish_launching`（[L1281-L1317](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_macos/src/platform.rs#L1281-L1317)）里也有一段相同逻辑——如果用户在启动回调里注册 `on_system_wake`，那时委托对象已存在，两处配合保证「无论注册早晚都能挂上观察者」。

[../gpui_macos/src/platform.rs:L1396-L1415](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_macos/src/platform.rs#L1396-L1415)
`onSystemWake:` selector 的 Rust 实现：先把回调执行**推迟到主队列下一轮**（`exec_async_f`，与 quit 同样的防重入手法，注释写明是因为 NSNotificationCenter 同步派发通知），再取回调执行。对应地，`on_reopen` 的触发点是 [should_handle_reopen（L1333-L1343）](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_macos/src/platform.rs#L1333-L1343)：仅当 `has_open_windows == false`（应用没有可见窗口）时才调用 reopen 回调——这正是「点 Dock 图标重新开窗」的实现机制；`on_quit` 的触发点是 [will_terminate（L1345-L1353）](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_macos/src/platform.rs#L1345-L1353)，对应 `applicationWillTerminate:`。

**Windows 的注册**——存进平台回调状态并用 Win32 电源通知：

[../gpui_windows/src/platform.rs:L670-L684](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_windows/src/platform.rs#L670-L684)
三个回调分别 `set` 进对应字段；`on_system_wake` 额外调用 `RegisterSuspendResumeNotification` 注册电源事件——唤醒时窗口消息循环收到 `WM_POWERBROADCAST`（注释标明 `self.handle` 是接收消息的平台窗口）。Web 平台则把 `on_system_wake` 实现为空（[gpui_web/src/platform.rs:L461-L469](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L461-L469)）：浏览器不向页面暴露系统睡眠事件。

#### 4.3.4 代码实践

**实践：源码阅读型——把 Linux 唤醒链路的七步落到具体行号**。

1. **实践目标**：不写代码也能动手——亲手把 4.3.2 的七步流程图与源码行号一一对应，检验「链路追踪」能力。
2. **操作步骤**：
   1. 打开 [gpui_linux/src/linux/platform.rs](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_linux/src/linux/platform.rs)，找到 `PlatformHandlers`、`start_wake_listener`、`handle_system_wake`、`listen_for_system_wake` 四处，确认它们的调用关系。
   2. 用 grep 确认三后端各自在哪里注册 `wake_receiver`：

      ```bash
      grep -rn "handle_system_wake" crates/gpui_linux/src
      ```

   3. 在本地 Linux 机器（Wayland/X11 会话）上尝试触发唤醒事件：`systemctl suspend` 后唤醒，观察一个注册了 `on_system_wake` 的程序是否打印日志（可用第 5 节综合实践的程序）。
3. **需要观察的现象**：grep 恰好命中三处 `handle_system_wake` 调用（headless/x11/wayland 各一）加一处定义；真机休眠唤醒后日志出现。
4. **预期结果**：链路七步中每一步都能指出文件与行号。休眠实验在虚拟机里可能不可用，「待本地验证」；若无法休眠，可改为阅读 [login1 文档语义](https://www.freedesktop.org/wiki/Software/systemd/inhibit/)，说明 `PrepareForSleep(true/false)` 分别对应入睡/唤醒。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `PlatformHandlers` 放在 `LinuxCommon` 里而不是直接放在 `LinuxPlatform` 外壳上？

答案：`LinuxPlatform<P>` 是泛型外壳，真正的运行时状态（事件循环句柄、连接、窗口表）都在后端 `P`（Wayland/X11/headless 客户端）里；而后端需要**互不可见地**访问共享回调（比如 headless 的事件循环要触发 system_wake 回调）。`LinuxCommon` 通过 `with_common(f)` 闭包访问器挂进每个后端（headless 的实现见 [headless/client.rs:L58-L60](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_linux/src/linux/headless/client.rs#L58-L60)），外壳与后端都能到达同一份数据。若放在外壳上，后端触发回调就需要持有外壳的引用，形成循环依赖。

**练习 2**：`handle_system_wake` 里为什么用 `take()` + `get_or_insert(callback)` 而不是直接 `if let Some(cb) = &mut self.callbacks.system_wake { cb() }`？

答案：两者语义等价，但 `take()` 写法把「取出所有权 → 调用 → 放回」做得非常显式：调用期间回调不在结构体里，因此回调内部可以安全地再次 `with_common` 访问同一状态（甚至重入注册新的回调），不会与被借用的 `self` 冲突。macOS 侧 `will_terminate`/`should_handle_reopen` 用了完全相同的手法（先 `take`、`drop(lock)`、调用、再 `get_or_insert`），因为它们还要先释放 Mutex 锁再执行用户闭包，防止回调里再锁同一把锁造成死锁。

**练习 3**：macOS 的 `should_handle_reopen` 只在 `has_open_windows == false` 时触发回调。如果 Zed 想实现「点 Dock 图标时若有窗口则全部带到前台」，应该用 `on_reopen` 吗？

答案：不该。`on_reopen` 语义就是「重开」（无窗口时的 Dock 点击）。有窗口时的前台化是 `activate` 家族的职责（`Platform::activate`，macOS 实现见 [L588-L593](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_macos/src/platform.rs#L588-L593)），由系统行为直接完成，不需要应用回调参与。把两个场景分开正是契约设计的清晰之处。

### 4.4 AppLifecyclePhase 与移动端生命周期：桌面上的「永不触发」

#### 4.4.1 概念说明

u2-l1 提过 trait 里有四个带默认实现的「移动端方法」。本讲聚焦其中两个生命周期相关的：

- `on_app_lifecycle(callback: Box<dyn FnMut(AppLifecyclePhase)>)`——生命周期阶段变化时触发；
- `on_memory_warning(callback: Box<dyn FnMut()>)`——系统内存吃紧时触发。

它们与桌面回调的根本差异是**控制权的归属**。注释写得很直白（[platform.rs:L207-L209](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/platform.rs#L207-L209)）：桌面应用自己决定何时退出（`quit` 是应用主动调用）；移动应用的生死由操作系统掌握——系统随时把应用切到后台、暂停、甚至直接杀死，应用只能「响应」而无法「决定」。

`AppLifecyclePhase` 就是把 iOS/Android 的系统回调词汇表抽象成四个平台无关的阶段。契约 doc 注释明确写着「Desktop platforms never invoke this」——四个桌面平台实现里没有任何一个覆盖 `on_app_lifecycle`（它们全部继承默认空实现），所以在桌面上注册这个回调等于把闭包扔进黑洞。这不是缺陷而是设计：契约为未来的 iOS/Android 后端（GPUI 的移动端支持）预留了词汇，桌面平台用默认空实现表达「本平台无此概念」。

#### 4.4.2 核心流程

移动端语义下的生命周期状态机（对照表见契约 doc，[platform.rs:L746-L757](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/platform.rs#L746-L757)）：

```text
        （启动）
          │ onResume / didBecomeActive
          ▼
      ┌────────┐  onPause / willResignActive  ┌──────────┐
      │ Active │ ───────────────────────────▶ │ Inactive │
      └────────┘                              └──────────┘
          ▲ 可见+有输入                          │ 可见+无输入
          │ onStart / willEnterForeground       │ onStop / didEnterBackground
          │                                     ▼
      Foreground ◀────────────────────────  Background
                                                （随时可能被系统杀死，无通知）
```

要点：

- `Active` 与 `Inactive` 的区别是**是否接收输入**（例如 iOS 上弹了个系统对话框，应用可见但收不到触摸）；
- `Background` 之后进程**随时可能被杀**且不会再收到任何回调——所以进入 `Background` 时必须立刻保存状态；
- 桌面平台没有这套压力模型：窗口最小化不是 `Background`（进程不会被随机杀掉），应用退出走的是 4.2 的 `quit` 链路，语义完全正交。

#### 4.4.3 源码精读

**契约中的移动端方法组**——两处默认空实现：

[../gpui/src/platform.rs:L207-L222](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/platform.rs#L207-L222)
注释首先声明「移动平台方法」的语境：OS 拥有应用生命周期，应用只能响应。`on_app_lifecycle` 默认体为空并带 doc「Desktop platforms never invoke this」；`on_memory_warning` 同样默认空实现，doc 指明对应 iOS `didReceiveMemoryWarning` 与 Android `onTrimMemory`。

**`AppLifecyclePhase` 枚举**——四阶段与两大移动平台的映射：

[../gpui/src/platform.rs:L746-L769](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/platform.rs#L746-L769)
doc 注释里直接内嵌了一张映射表（`Active ↔ didBecomeActive/onResume`、`Inactive ↔ willResignActive/onPause`、`Background ↔ didEnterBackground/onStop`、`Foreground ↔ willEnterForeground/onStart`），并解释 `Background` 意味着「GPU surface 可能被销毁、进程可能无预警被杀」。每个变体的字段注释再次强调语义（如 `Inactive` 是「可见但不接收输入」）。

**「桌面永不调用」的证据**——契约层之外，`App` 根本没有暴露转发 API。在 gpui/src/app.rs 里搜索 `on_app_lifecycle` 零命中：`App` 有 `on_system_wake`、`on_app_quit`、`on_thermal_state_change` 等等（它们都在 4.2/4.3 读过），唯独没有 `on_app_lifecycle` 的 App 层版本。桌面调用链在 `App` 这一层就断掉了，平台默认空实现永远接不到回调。你可以自己验证：

```bash
grep -rn "fn on_app_lifecycle\|fn on_memory_warning" crates/gpui*/src
# 只会命中 gpui/src/platform.rs 的 trait 声明与默认实现，四个平台 crate 均无覆盖
```

这与 u2-l1 练习 2 的结论呼应：默认空实现属于「优雅降级 no-op 型」——调用方无需感知平台是否支持，代码可以原样编译运行。

#### 4.4.4 代码实践

**实践：验证移动端方法在桌面上的「静默」**。

1. **实践目标**：用 grep + 编译实验双重确认 `on_app_lifecycle` 在当前代码库没有任何桌面实现，并理解默认实现的「无害性」。
2. **操作步骤**：
   1. 运行上面的 grep 命令，记录命中位置；再运行 `grep -rn "AppLifecyclePhase" crates/gpui/src crates/gpui_linux/src crates/gpui_macos/src crates/gpui_windows/src crates/gpui_web/src`，观察除 trait 定义外还有哪些引用（应只有类型定义与少量无关命中）。
   2. （可选，示例代码）写一段直接调用平台方法的程序，确认它能编译：

      ```rust
      use gpui::App;
      use gpui_platform::{application, Platform};

      fn main() {
          application().run(|_cx: &mut App| {
              let platform: std::rc::Rc<dyn Platform> = unimplemented!();
              platform.on_app_lifecycle(Box::new(|phase| println!("{phase:?}")));
          });
      }
      ```

      （`unimplemented!()` 处需要你自己想办法拿到平台引用——提示：`App` 没有公开 `platform()` 访问器，这正是「App 层不暴露移动端生命周期」的又一证据；也可以在阅读源码层面完成本题。）
3. **需要观察的现象**：grep 只命中 trait 声明与默认实现；没有任何桌面平台文件出现 `fn on_app_lifecycle`。
4. **预期结果**：确认「Desktop platforms never invoke this」不是口头承诺而是代码现状：四个平台 crate 零覆盖 + App 层零转发。若未来出现 iOS/Android 后端，这个格局会被打破——到时用同样命令可以检测到变化。

#### 4.4.5 小练习与答案

**练习 1**：既然桌面上永远不会触发，为什么契约还要定义 `on_app_lifecycle`？删掉它有什么坏处？

答案：`Platform` 是平台无关契约，目标是覆盖 GPUI 支持的所有目标平台（包括移动端规划）。删掉它，移动端实现者就无法用统一接口上报生命周期，要么自己发明接口（破坏契约的统一性），要么在 `Platform` 之外另设平行 trait（增加调用方分支）。默认空实现让这个「预留」几乎零成本：桌面实现者不需要写任何代码，调用方代码也能原样编译。契约宽度与实现成本的平衡点就在默认实现。

**练习 2**：桌面应用窗口最小化到任务栏，对应 `AppLifecyclePhase` 的哪个阶段？

答案：一个都不对应。`AppLifecyclePhase` 描述的是移动 OS 的进程级生命周期，而窗口最小化是桌面窗口管理器的窗口级状态——GPUI 里它体现为窗口事件（resize/焦点变化，u3 的主题），进程始终活跃。把两者混为一谈是读这套契约时最常见的误解；doc 注释里 `Inactive` 的定义（「可见但无输入」）也与「最小化」（不可见）不同。

**练习 3**：`on_memory_warning` 在桌面上同样永不触发。如果桌面应用想响应内存压力（比如缓存变得激进），gpui 有没有替代机制？

答案：契约层没有等价物——`on_memory_warning` 是唯一入口且桌面不触发。现实的替代方案是在应用层自己监控（例如后台任务定期查询进程内存）或依赖操作系统的资源限制信号。这个不对称是「能力探测型默认值」的反面案例：调用方拿到的是「注册成功但永不触发」的静默承诺，所以设计 API 时如果关心桌面，就不该依赖这个回调。

## 5. 综合实践

**任务：注册 `on_quit` 与 `on_system_wake`，对比「优雅退出」与「暴力终止」两种退出方式下回调的触发差异。**

这是本讲规格中指定的实践任务，综合 4.2（退出链路）与 4.3（回调链路）两个模块。

1. **实践目标**：亲手验证两条结论——优雅退出时 `on_app_quit` 观察者会执行；`kill` 信号杀进程时所有退出回调都不会执行。
2. **操作步骤**：
   1. 新建独立小 crate（方法同 u1-l2：依赖 path 形式的 `gpui` 与 `gpui_platform`，按目标平台开 feature），写如下程序（示例代码）：

      ```rust
      use gpui::{App, WindowOptions};
      use gpui_platform::application;

      fn main() {
          application().run(|cx: &mut App| {
              println!("[启动] on_finish_launching, pid={}", std::process::id());

              cx.on_app_quit(|_| async {
                  println!("[on_app_quit] 优雅退出清理执行");
              })
              .detach();

              cx.on_system_wake(|_| {
                  println!("[on_system_wake] 系统刚刚唤醒");
              })
              .detach();

              cx.open_window(WindowOptions::default(), |_, cx| cx.new(|_| gpui::Empty))
                  .unwrap();
          });
          println!("[run 返回] 事件循环已结束");
      }
      ```

   2. **实验 A（优雅退出）**：运行程序，正常关闭窗口（Linux/Windows 上 `QuitMode::Default` 意味着关最后一个窗口即退出），记录日志顺序。
   3. **实验 B（信号终止）**：再次运行程序，记下 pid，从另一终端执行 `kill <pid>`（默认 SIGTERM），观察哪些日志没有出现；再试 `kill -9 <pid>` 对比。
   4. **实验 C（唤醒，可选）**：在有休眠能力的机器上运行程序并 `systemctl suspend`，唤醒后检查是否打印 `[on_system_wake]`；注意 Linux 上该链路依赖 `wayland` 或 `x11` feature（4.3.3 讲过 `listen_for_system_wake` 的 cfg 门控）。
   5. 把三次实验的日志分别存档，对照 4.2.2 的时序图标注每行日志落在链路的哪一步。
3. **需要观察的现象**：
   - 实验 A：`[on_app_quit]` 与 `[run 返回]` 都出现，且前者先于后者；
   - 实验 B：`[on_app_quit]` 与 `[run 返回]` 都**不**出现（SIGTERM 直接终止进程，calloop 没有机会正常退出，`LinuxPlatform::run` 尾声的 quit 回调不会执行）；
   - 实验 C：唤醒后出现 `[on_system_wake]`（若实验环境支持）。
4. **预期结果**：得到一张「触发方式 × 回调是否执行」的对照表——窗口关闭/快捷键：on_app_quit ✅；SIGTERM：❌；SIGKILL：❌；系统唤醒：on_system_wake ✅。由此理解为什么重要状态不能只依赖退出回调落盘（Zed 的做法是持续保存，退出回调只是最后一道保险）。
5. 信号行为与窗口管理器行为在不同环境有差异，实验结果「待本地验证」；若你的 `kill <pid>` 意外触发了清理，请检查是否有代码注册了信号处理器并在其中调用了 `cx.quit()`。

## 6. 本讲小结

- `Platform::run` 是「执行一次启动回调 + 阻塞在平台事件循环」的契约；回调时机各平台不同：Linux/Windows 进循环前同步执行，macOS 等 AppKit `did_finish_launching` 通知（循环内），Web 等异步图形初始化完成后在浏览器任务里执行。
- `Platform::quit` 只发「该退出了」信号：Linux 用 calloop `LoopSignal::stop()`，macOS 把 `terminate:` 推迟到主队列下一轮（防 AppKit 同步关窗造成双重借用），Windows 用 `PostQuitMessage`；三者的 `on_quit` 回调都在事件循环退出后的 `run` 尾声触发。
- 优雅退出的终点是 `App::shutdown()`：给所有 `on_app_quit` 观察者 200 毫秒（`SHUTDOWN_TIMEOUT`）完成异步清理；`QuitMode` 决定「关闭最后一个窗口」是否等于退出，`Default` 模式下这一行为在 macOS 与其他平台编译期分岔。
- `restart` 的通用套路是 spawn 一个外部 shell 进程轮询等待旧 PID 消失再重启（bash / `open` / PowerShell 三种方言）；`activate`/`hide` 家族只在 macOS 有真实实现，Linux 打日志降级。
- Linux 把 `on_quit`/`on_reopen`/`on_system_wake` 等回调集中存进 `LinuxCommon.callbacks`（`PlatformHandlers` 结构体），系统唤醒链路为 `login1 PrepareForSleep 信号 → DBus → calloop channel → handle_system_wake → App 观察者`，三后端共享；macOS 则把回调存进 `MacPlatformState` 并由 Objective-C 委托方法触发。
- `on_app_lifecycle`/`on_memory_warning` 是移动端专属契约：默认空实现 + 四个桌面平台零覆盖 + `App` 层无转发 API，三层事实共同保证「桌面永不触发」；`AppLifecyclePhase` 四阶段映射 iOS/Android 系统回调，与桌面窗口状态（最小化等）是正交概念。

## 7. 下一步学习建议

1. **u2-l3（显示器管理）**：继续沿八大分组下钻 `displays`/`primary_display` 与 `PlatformDisplay` 契约。你可以先复习本讲 4.1 提到的「平台状态在构造期建立」——显示器枚举同样发生在后端客户端初始化期，理解本讲的 `LinuxCommon` 聚合方式后再读 display 实现会非常顺。
2. **u2-l4（系统集成服务）**：本讲看到 `PlatformHandlers` 里还有 `open_urls` 回调字段，u2-l4 会展开 URL 打开、剪贴板、文件对话框、凭据存储这组高频系统集成。
3. **u4 单元（调度与并发）**：本讲多次出现「派发到前台执行器再执行」的防重入手法（macOS quit、Windows restart）以及 calloop channel。u4-l2 的 `PlatformDispatcher` 与 u4-l3 的 `LinuxDispatcher` 会把这些机制讲透：`run` 阻塞期间任务如何被投递进事件循环，正是调度器的核心命题。
4. **延伸阅读**：对照 [gpui/src/app/headless_app_context.rs](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/app/headless_app_context.rs)（u8-l3 主角）思考一个问题：无头场景下 `run` 的循环还在转（4.1.3 的 headless 实现），但没有真实窗口与输入，这个「空转」的价值是什么？答案是它是 u8-l4 确定性测试与离屏渲染的驱动引擎。

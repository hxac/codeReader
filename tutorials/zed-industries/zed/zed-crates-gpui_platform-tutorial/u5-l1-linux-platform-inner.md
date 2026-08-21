# u5-l1 LinuxPlatform 与 LinuxClient：一个外壳、三种后端

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `gpui_platform::current_platform` 与 `gpui_linux::current_platform` 的两层分发关系：第一层在编译期靠 `#[cfg]` 选出 Linux 分支，第二层在运行期靠 headless 参数与环境变量探测选定具体后端。
2. 解释 `LinuxPlatform<P>` 这个泛型外壳如何用一份代码为三种后端实现 gpui 的 `Platform` 契约，以及哪些逻辑留在外壳、哪些下沉到后端。
3. 逐方法列出 `LinuxClient` trait 要求后端实现的方法，并区分必需方法、默认实现方法与 feature 门控方法。
4. 说明 `LinuxCommon` 与 `PlatformHandlers` 各自聚合了什么状态、谁写谁读，理解「公共状态放在公共层、事件能力放在后端」的分层原则。

本讲是第 5 单元（Linux 平台深入）的第一篇：先把 Linux 侧的骨架搭清楚，后续三篇（headless、X11、Wayland）只需往这个骨架上填血肉。

## 2. 前置知识

本讲假设你已读过前置讲义，以下概念会被直接使用：

- **门面 crate 与两层分发的第一层**（u1-l1、u1-l4）：`gpui_platform` 自身无实现，它的 `current_platform(headless)` 用四段互斥的 `#[cfg]` 块按编译目标分发；在 Linux/FreeBSD 目标上它只是转发给 `gpui_linux::current_platform(headless)`。
- **`Platform` 契约与默认实现的三种姿态**（u2-l1）：`Platform` trait 共 69 个方法，其中 18 个带默认实现；默认实现分为能力探测型（返回 `None`/`false`）、优雅降级 no-op 型、通用回退型。
- **`guess_compositor()` 环境探测**（u1-l4）：`ZED_HEADLESS` 存在即返回 `"Headless"`；否则 `WAYLAND_DISPLAY` 非空返回 `"Wayland"`、`DISPLAY` 非空返回 `"X11"`、兜底 `"Headless"`，且读取哪个变量受 feature 门控。
- **单前台线程模型与执行器**（u4-l1）：`BackgroundExecutor` 与 `ForegroundExecutor` 共享同一个 `Arc<dyn PlatformDispatcher>`，前者跑 `Send` 任务、后者绑定主线程。

再补充两个本讲要用到的 Rust 概念：

- **泛型单态化 vs trait 对象动态分发**：`struct LinuxPlatform<P> { inner: P }` 中的 `P` 是编译期已知的具体类型，所以外壳调用 `self.inner.xxx()` 是静态分发（编译器为每个 `P` 生成一份代码）；而 gpui 拿到的是 `Rc<dyn Platform>`，跨过这层 trait 对象才发生一次动态分发。
- **内部可变性**：三个后端都用 `Rc<RefCell<XxxClientState>>` 存状态，`with_common` 就是对 `RefCell::borrow_mut()` 的封装——外壳因此能在不可变 `&self` 上修改公共状态。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/gpui_platform.rs:L57-L81](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/src/gpui_platform.rs#L57-L81) | 分发第一层：门面 crate 的 `current_platform`，Linux 分支直接转发 |
| [../gpui_linux/src/gpui_linux.rs:L1-L5](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/gpui_linux.rs#L1-L5) | gpui_linux crate 根：整 crate 被 `#![cfg(linux/freebsd)]` 门控，只导出 `current_platform` |
| [../gpui_linux/src/linux.rs:L1-L25](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux.rs#L1-L25) | 模块声明与 feature 门控的再导出；`wayland`/`x11` 目录整体受 feature 门控 |
| [../gpui_linux/src/linux.rs:L30-L60](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux.rs#L30-L60) | 分发第二层：`gpui_linux::current_platform`，运行期选定三种后端之一 |
| [../gpui_linux/src/linux/platform.rs:L51-L104](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L51-L104) | 本讲主角之一：`LinuxClient` trait（后端契约） |
| [../gpui_linux/src/linux/platform.rs:L106-L136](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L106-L136) | `PlatformHandlers`（回调登记处）与 `LinuxCommon`（公共状态）的声明 |
| [../gpui_linux/src/linux/platform.rs:L138-L227](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L138-L227) | `LinuxCommon::new` 装配车间与系统唤醒监听 |
| [../gpui_linux/src/linux/platform.rs:L229-L752](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L229-L752) | 本讲主角之二：`LinuxPlatform<P>` 及其完整 `Platform` 实现 |
| [../gpui_linux/src/linux/headless/client.rs:L24-L143](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/headless/client.rs#L24-L143) | 后端之一：`HeadlessClient`（无显示环境，也是本讲最易读的参考实现） |
| [../gpui_linux/src/linux/wayland/client.rs:L924-L1233](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L924-L1233) | 后端之二：`WaylandClient` 的 `LinuxClient` 实现（节选） |
| [../gpui_linux/src/linux/x11/client.rs:L1535-L1808](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L1535-L1808) | 后端之三：`X11Client` 的 `LinuxClient` 实现（节选） |
| [../gpui/src/platform.rs:L93-L123](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui/src/platform.rs#L93-L123) | `guess_compositor()`：第二层分发依赖的环境探测（回顾） |

## 4. 核心概念与源码讲解

### 4.1 LinuxPlatform：泛型外壳与两层分发

#### 4.1.1 概念说明

Linux 上有三种截然不同的运行环境：Wayland 合成器、X11 服务器、以及完全没有显示的环境（CI、远程服务器、`ZED_HEADLESS=1`）。如果让三套代码各自完整实现 69 个方法的 `Platform` 契约，会有大量重复：凭据存储走 Secret Service、文件对话框走 xdg-desktop-portal、系统通知走DBus、重启靠 shell 脚本轮询——这些都与窗口系统无关。

gpui_linux 的解法是**外壳模式**（本质上是模板方法模式的一种变形）：

- `LinuxPlatform<P>` 是外壳，实现 gpui 的 `Platform` 契约，承载所有「Linux 通用」逻辑；
- `P` 是后端，实现 crate 私有的 `LinuxClient` 契约，只负责真正依赖窗口系统的部分；
- 公共状态（执行器、文本系统、回调等）收拢进 `LinuxCommon`，由后端持有、外壳经 `with_common` 访问。

注意 `P` 是泛型参数而不是 `Box<dyn LinuxClient>`：三个后端在编译期就确定了（由 `current_platform` 的分支决定），用泛型可以省掉一次虚表跳转，也让后端方法在各后端单态化。

#### 4.1.2 核心流程

从应用启动到后端选定的完整链路：

```text
gpui_platform::application()
  └─ gpui_platform::current_platform(false)          ← 第一层：编译期 #[cfg] 分发
       └─ (linux/freebsd 分支) gpui_linux::current_platform(headless)
            ├─ headless == true ──────────► LinuxPlatform { inner: HeadlessClient::new() }
            └─ headless == false:
                 match gpui::guess_compositor()       ← 运行期环境变量探测
                   "Wayland"  (cfg wayland)  ────────► LinuxPlatform { inner: WaylandClient::new() }
                   "X11"      (cfg x11)     ────────► LinuxPlatform { inner: X11Client::new().unwrap() }
                   "Headless"                ───────► LinuxPlatform { inner: HeadlessClient::new() }
  └─ Application::with_platform(Rc<dyn Platform>)    ← 外壳被擦成契约指针注入应用
```

两层分工清晰：第一层回答「是不是 Linux」，发生在编译期；第二层回答「Linux 上的哪种窗口系统」，发生在运行期。

#### 4.1.3 源码精读

先看第一层，门面 crate 的 `current_platform` 中 Linux 分支只有一行转发：

[src/gpui_platform.rs:L71-L74](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/src/gpui_platform.rs#L71-L74)——在 `linux`/`freebsd` 目标上直接返回 `gpui_linux::current_platform(headless)`，不做任何额外工作。

第二层是本讲核心入口：

[../gpui_linux/src/linux.rs:L30-L60](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux.rs#L30-L60)——`pub fn current_platform(headless: bool) -> Rc<dyn gpui::Platform>` 做三件事：

1. `headless` 参数为真时直接短路到 `HeadlessClient`（L34-38），连环境变量都不看；
2. 否则 `match gpui::guess_compositor()`，三个分支分别构造 `LinuxPlatform { inner: WaylandClient::new() }`、`X11Client::new().context(...).unwrap()`、`HeadlessClient::new()`；
3. 落到 `_` 分支说明 wayland/x11 feature 都没开且环境不满足，`unreachable!` 给出明确报错信息（L56-58）。

两个值得注意的细节：`X11Client::new()` 返回 `anyhow::Result`（建立 XCB 连接可能失败），这里用 `.context(...).unwrap()` 做启动期 fail-fast；而 `WaylandClient::new()` 和 `HeadlessClient::new()` 不可失败。分支本身还被 `#[cfg(feature = ...)]` 门控——如果只编译了 x11 feature，`"Wayland"` 匹配臂根本不存在，这正是 u1-l3 讲过的「代码 cfg 与 Cargo.toml 依赖严格对齐」。

环境探测函数（回顾 u1-l4）：

[../gpui/src/platform.rs:L93-L123](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui/src/platform.rs#L93-L123)——`guess_compositor()` 依次检查 `ZED_HEADLESS`、`WAYLAND_DISPLAY`、`DISPLAY`，返回字符串供上面的 match 使用。注意它「只猜不连」：注释明确说明不尝试连接合成器，真正的连接发生在各后端的 `new()` 里。

最后看外壳本体，结构简单到只有一行：

[../gpui_linux/src/linux/platform.rs:L229-L231](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L229-L231)——`pub(crate) struct LinuxPlatform<P> { pub(crate) inner: P }`，没有任何其他字段：外壳自己不存状态，一切公共状态都在后端的 `LinuxCommon` 里。

[../gpui_linux/src/linux/platform.rs:L233-L254](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L233-L254)——`impl<P: LinuxClient + 'static> Platform for LinuxPlatform<P>` 的开头几个方法定下了整篇实现的基调：`background_executor`、`foreground_executor`、`text_system` 三个查询全部经 `self.inner.with_common(...)` 从公共状态克隆出来；`keyboard_mapper` 则直接返回 `gpui::DummyKeyboardMapper`（u3-l3 讲过：Linux 键名无需改写，mapper 只是透传）。

#### 4.1.4 代码实践

**实践目标**：用一份不变的程序，验证「同一份 `LinuxPlatform` 代码、不同后端、不同输出」。

1. 复用你在 u1-l2 建的练习 crate（依赖 `gpui` 与 `gpui_platform`，启用 `x11`、`wayland` feature），把 run 回调改成如下逻辑（示例代码）：

   ```rust
   // 示例代码：放在 Application::run 的 on_finish_launching 回调里
   let platform = gpui_platform::current_platform(false);
   println!("compositor = {}", platform.compositor_name());
   for display in platform.displays() {
       println!(
           "display {:?} bounds = {:?}",
           display.id(),
           display.bounds()
       );
   }
   platform.quit();
   ```

2. 在 Linux 图形会话下运行一次，记录 `compositor_name()` 与显示器 bounds；
3. 再分别以 `ZED_HEADLESS=1` 与清空 `WAYLAND_DISPLAY`/`DISPLAY` 的方式重跑两次。

**需要观察的现象**：三份输出的 `compositor_name()` 分别是 `"Wayland"`（或 `"X11"`，取决于会话）与 `"headless"`（注意小写）；headless 下 `displays()` 恰好返回一台固定 1920×1080 的假屏。

**预期结果**：程序代码一字未改，行为随环境切换——这就是「分发在入口、逻辑在外壳、差异在后端」的直接体感。最后一行 `platform.quit()` 让无头模式下的事件循环也能退出、程序正常结束。

**待本地验证**：以上运行期行为需在 Linux 上确认；若你在 macOS/Windows 上阅读本讲，可改为源码阅读型实践：对照 [../gpui_linux/src/linux/headless/client.rs:L66-L68](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/headless/client.rs#L66-L68) 与 [../gpui_linux/src/linux/headless/window.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/headless/window.rs) 中假屏的实现，推断 bounds 输出。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `gpui_linux::current_platform` 的返回类型是 `Rc<dyn Platform>`，而不是 `Rc<LinuxPlatform<WaylandClient>>` 之类的具体类型？

答案：三个 match 分支构造出的外壳类型互不相同（`LinuxPlatform<WaylandClient>`、`LinuxPlatform<X11Client>`、`LinuxPlatform<HeadlessClient>`），函数只能有一个返回类型；擦成 `Rc<dyn Platform>` 后，gpui 主 crate 也无需知道任何 Linux 后端的存在，只面向契约编程。类型擦除只发生在外壳这一层，外壳内部的 `inner: P` 仍是具体类型。

**练习 2**：把 X11 分支的 `.context(...).unwrap()` 去掉会怎样？

答案：无法编译。`X11Client::new()` 返回 `anyhow::Result<Self>`，而 match 分支需要的是 `Rc<dyn Platform>`，类型不匹配。Rust 没有隐式 `Result` 解包，要么 `unwrap`/`expect`（启动期 fail-fast），要么改函数签名返回 `Result`（那将波及整条 `current_platform` 调用链）。

**练习 3**：在 macOS 目标上编译 `gpui_linux` crate 会发生什么？

答案：crate 根第一行 [../gpui_linux/src/gpui_linux.rs:L1](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/gpui_linux.rs#L1) 的 `#![cfg(any(target_os = "linux", target_os = "freebsd"))]` 会把整个 crate 内容（包括 `current_platform`）裁剪为空；同时 gpui_platform 的 Cargo.toml 只在 Linux 目标依赖它，所以它根本不会参与 macOS 构建。

### 4.2 LinuxClient：可替换的后端契约

#### 4.2.1 概念说明

`LinuxClient` 是 gpui_linux **crate 私有**（`pub(crate)`）的第二个契约，专为「外壳之后还要不要再分」而设。对比一下规模就能理解分工：gpui 的 `Platform` 有 69 个方法，`LinuxClient` 只有约 22 个——因为外壳已经消化了大部分 `Platform` 方法（凭据、portal 对话框、通知、菜单、重启……），真正必须由窗口系统决定的才留下来。

它的方法分三类：

- **必需方法（17 个）**：后端必须给出自己的实现；
- **默认实现方法**：可选能力，后端按需覆盖；
- **feature 门控方法**：只在开启特定 feature 时才存在于 trait 中。

#### 4.2.2 核心流程

外壳的每个 `Platform` 方法落到后端的三种方式：

```text
Platform::m() 被调用（Rc<dyn Platform> 动态分发一次）
  │
  ├─ A. 纯委托：self.inner.m()                （静态分发到后端）
  │      例：displays、open_window、剪贴板四件套、run
  │
  ├─ B. 读公共状态：self.inner.with_common(|common| ...)
  │      例：executors、text_system、appearance、menus、on_* 回调注册
  │
  └─ C. 外壳自实现：不碰后端（最多借用 window_identifier / executor）
         例：credentials、prompt_for_paths、show_system_notification、restart
```

A 类是「窗口系统说了算」，B 类是「状态在公共层」，C 类是「Linux 通用服务」。

#### 4.2.3 源码精读

契约全文（建议整段通读一遍）：

[../gpui_linux/src/linux/platform.rs:L51-L104](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L51-L104)——`pub(crate) trait LinuxClient`，逐组说明：

| 分组 | 方法 | 行号 | 说明 |
|---|---|---|---|
| 身份 | `compositor_name` | L52 | 返回 `"Wayland"`/`"X11"`/`"headless"`，是运行期观测后端的官方出口 |
| 公共状态入口 | `with_common` | L53 | 后端把 `&mut LinuxCommon` 借给外壳用的唯一通道 |
| 键盘 | `keyboard_layout` | L54 | 三后端各自报告布局 |
| 显示器 | `displays`、`display`、`primary_display` | L55-58 | `display` 带 `#[allow(unused)]`：外壳目前没有对应的 `Platform::display` 转发，是个「预留」方法 |
| 屏幕捕获 | `is_screen_capture_supported`、`screen_capture_sources` | L60-76 | 仅 `screen-capture` feature 下存在，带默认实现（默认支持、默认返回错误） |
| 窗口 | `open_window` | L78-82 | 三后端分歧最大的方法 |
| 光标 | `set_cursor_style`、`hide_cursor_until_mouse_moves`、`is_cursor_visible` | L83-87 | 后两个有默认体（no-op / 恒可见），headless 因此无需覆盖 |
| 系统集成 | `open_uri`、`reveal_path` | L88-89 | 打开链接与在文件管理器中显示 |
| 剪贴板 | `write_to_primary`、`write_to_clipboard`、`read_from_primary`、`read_from_clipboard` | L90-93 | 普通剪贴板 + Linux 主选区，都是必需 |
| 窗口关系 | `active_window`、`window_stack` | L94-95 | 焦点窗口与 Z 序 |
| 事件循环 | `run` | L96 | 后端阻塞在自己的 calloop 主循环里 |
| portal | `window_identifier` | L98-103 | 仅 wayland/x11 feature 下存在，默认返回 `ready(None)` |

三个实现的位置（后续三讲的入口）：

| 后端 | `impl LinuxClient for ...` | 结构体定义 |
|---|---|---|
| headless | [../gpui_linux/src/linux/headless/client.rs:L57-L143](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/headless/client.rs#L57-L143) | L22：`HeadlessClient(Rc<RefCell<HeadlessClientState>>)` |
| X11 | [../gpui_linux/src/linux/x11/client.rs:L1535-L1808](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L1535-L1808) | L306：`X11Client(Rc<RefCell<X11ClientState>>)` |
| Wayland | [../gpui_linux/src/linux/wayland/client.rs:L924-L1233](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L924-L1233) | L649：`WaylandClient(Rc<RefCell<WaylandClientState>>)` |

三个后端的 `with_common` 实现一模一样，都是把闭包应用到自家 state 的 `common` 字段上：

- [../gpui_linux/src/linux/headless/client.rs:L58-L60](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/headless/client.rs#L58-L60)：`f(&mut self.0.borrow_mut().common)`
- [../gpui_linux/src/linux/x11/client.rs:L1540-L1542](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L1540-L1542)：同型
- [../gpui_linux/src/linux/wayland/client.rs:L1131-L1133](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L1131-L1133)：同型

用两个方法看「同一契约、三种落地」的差异有多真实。

`displays()`——数据来源完全不同：

- [../gpui_linux/src/linux/headless/client.rs:L66-L68](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/headless/client.rs#L66-L68)：返回构造时创建好的那台 `HeadlessDisplay`（假屏）；
- [../gpui_linux/src/linux/wayland/client.rs:L929-L942](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L929-L942)：遍历 state 里**被动累积**的 `outputs`（wl_output 事件逐步上报，呼应 u2-l3），按 scale 换算成逻辑像素；
- [../gpui_linux/src/linux/x11/client.rs:L1549-L1562](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L1549-L1562)：**主动读取** XCB 连接的 `setup().roots`，为每个 screen 构造 `X11Display`。

`read_from_clipboard()`——从「什么都不做」到「带缓存」：

- [../gpui_linux/src/linux/headless/client.rs:L129-L131](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/headless/client.rs#L129-L131)：恒返回 `None`；
- [../gpui_linux/src/linux/wayland/client.rs:L1207-L1209](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L1207-L1209)：直接 `state.clipboard.read()`；
- [../gpui_linux/src/linux/x11/client.rs:L1778-L1793](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L1778-L1793)：先判断「自己是不是剪贴板的 owner」——是则返回带元数据（语法高亮等）的本地缓存 `clipboard_item`，否则才向 X 服务器请求任意格式内容。这是 u2-l4 讲过的「文本哈希 + 元数据私藏」在 X11 的落地。

再看一个「外壳向后端借数据」的例子：外壳自己在 C 类里实现 portal 文件对话框，但对话框需要告诉 portal「我是哪个窗口」，于是回调后端的 `window_identifier`：

[../gpui_linux/src/linux/platform.rs:L411-L414](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L411-L414)——`prompt_for_paths` 一开始就取 `self.inner.window_identifier()`，之后对话框流程全在外壳（详见 u5-l5）。Wayland 侧用 `wl_surface` 生成标识符（[../gpui_linux/src/linux/wayland/client.rs:L1227-L1233](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L1227-L1233)），X11 侧用 X 窗口 id（[../gpui_linux/src/linux/x11/client.rs:L1862-L1868](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L1862-L1868)），headless 压根没有这个方法（feature 门控直接把它从 trait 里裁掉）。

#### 4.2.4 代码实践

**实践目标**：亲手编制一张「`LinuxClient` 方法 × 三后端」覆盖矩阵，验证契约的必需/默认/门控分类。

1. 在 zed 仓库根目录执行（源码阅读型实践，任何操作系统可做）：

   ```bash
   # 定位三个实现块
   rg -n "impl LinuxClient for" crates/gpui_linux/src
   # 列出每个实现块里覆盖了哪些方法
   rg -n "    fn [a-z_]+" crates/gpui_linux/src/linux/headless/client.rs
   rg -n "    fn [a-z_]+" crates/gpui_linux/src/linux/x11/client.rs | sed -n '/1535/,$p'
   rg -n "    fn [a-z_]+" crates/gpui_linux/src/linux/wayland/client.rs | sed -n '/924/,$p'
   ```

2. 画一张三列（headless/X11/Wayland）× 若干行的表，行是 trait 方法；某后端实现了就打勾并填行号，只吃默认实现的填「默认」。
3. 对照 [../gpui_linux/src/linux/platform.rs:L51-L104](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L51-L104) 检查：你表里「默认」的格子是否恰好对应 trait 里带方法体的那几个。

**需要观察的现象**：headless 的实现块最短（大量方法一行完事）；X11 与 Wayland 的实现块里 `hide_cursor_until_mouse_moves`/`is_cursor_visible` 是否出现（两者确实覆盖了它们，因为真实窗口系统有光标可藏）。

**预期结果**：矩阵显示约 17 个必需方法三列全勾；`window_identifier` 只有 X11/Wayland 两列有勾（headless 列连格子都不该有——该方法被 cfg 裁掉了）；`display(id)` 三列情况不一（headless 与 wayland 覆盖，X11 未覆盖，但它是必需方法——想一想这意味着什么，见练习 3）。

**待本地验证**：X11 是否覆盖 `display(id)` 请以你本地 `rg` 输出为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `window_identifier` 的默认实现返回 `ready(None)` 而不是直接不给默认体？

答案：该方法仅在 wayland/x11 feature 下存在于 trait 中，而这两个后端之外还有「feature 开着但当前跑 headless」的场景吗？没有——headless 分支编译时该方法存在（trait cfg 是 `any(wayland, x11)`），但 `HeadlessClient` 也可以选择不覆盖从而拿到 `ready(None)` 的兜底；更重要的是它让 X11/Wayland 之外的未来后端（或测试替身）不必为「没有可用窗口标识」写样板代码。默认返回 `None` 在 portal 协议里是合法值：对话框只是失去模态绑定，仍能打开。

**练习 2**：`hide_cursor_until_mouse_moves` 有默认 no-op、`read_from_clipboard` 没有。判断「该不该给默认体」的标准是什么？

答案：看「无此能力」是否是该能力的合理状态。任何后端都可以合法地「不藏光标」「不支持截屏」，所以 no-op/报错的默认体成立；而「读不到剪贴板内容」与「读到了空」语义混淆（`Option<ClipboardItem>` 的 `None` 同时表示「剪贴板为空」），且每个真实后端必然要处理 X 剪贴板/Wayland 数据设备这两种截然不同的机制，没有可复用的通用回退，因此定为必需。

**练习 3**：`display(id)` 是必需方法却标着 `#[allow(unused)]`，这矛盾吗？

答案：不矛盾，但确实是历史痕迹。`#[allow(unused)]` 压制的是「trait 方法从未被调用」的警告——外壳的 `Platform` 实现里目前没有 `display` 方法的转发（`App::displays` 只走 `displays()`），所以它是「契约要求实现、但暂时无人调用」的预留方法。Rust 的 trait 系统要求必需方法必须有实现，于是三个后端（至少当前编译路径覆盖到的）都得写上。

### 4.3 LinuxCommon：公共状态的装配车间

#### 4.3.1 概念说明

`LinuxCommon` 是「与窗口系统无关、但每个后端都需要」的那部分平台状态。它的所有权安排很有讲究：**每个后端 state 里各持有一份**（`HeadlessClientState.common`、`X11ClientState.common`、`WaylandClientState.common`），外壳不持有，只能通过 `with_common` 闭包式借用。这样公共状态与后端生命周期完全一致，外壳无需额外同步。

它聚合的状态可以分四类：

1. **执行器两件套**：`BackgroundExecutor` + `ForegroundExecutor`（共享一个 `Arc<LinuxDispatcher>`）；
2. **文本系统**：`Arc<dyn PlatformTextSystem>`（feature 决定真实现还是 Noop）；
3. **UI 偏好缓存**：`appearance`、`auto_hide_scrollbars`、`button_layout`、`menus`；
4. **控制与回调**：`callbacks`（`PlatformHandlers`）、`signal`（calloop `LoopSignal`）、`app_name`、`system_notifications`、wake 通道。

#### 4.3.2 核心流程

`LinuxCommon::new(signal)` 的装配时序（signal 由后端传入，取自后端自己的 calloop `EventLoop`）：

```text
后端::new()
  ├─ EventLoop::try_new()                         ← 后端自己的主循环
  ├─ LinuxCommon::new(event_loop.get_signal())    ← 把停止信号交公
  │    ├─ PriorityQueueCalloopReceiver::new()  → (main_sender, main_receiver)
  │    ├─ calloop::channel::channel()          → (wake_sender, wake_receiver)
  │    ├─ 按 feature 选 text_system:
  │    │     wayland|x11 → CosmicTextSystem::new("IBM Plex Sans")
  │    │     否则       → NoopTextSystem::new()
  │    ├─ dispatcher = Arc::new(LinuxDispatcher::new(main_sender))
  │    └─ BackgroundExecutor::new(dispatcher.clone())
  │       + ForegroundExecutor::new(dispatcher)
  │       → 返回 (common, main_receiver, wake_receiver)
  └─ event_loop.handle().insert_source(main_receiver, ...)
     event_loop.handle().insert_source(wake_receiver, ...)
```

关键在于返回的三元组：`LinuxCommon` 留下执行器与 wake_sender，而两个 receiver 由**后端**插进自己的 calloop 事件循环——这正是 u4-l3 讲过的「调度器不拥有主循环，只持 main_sender 唤醒主循环」的装配现场。三处调用点分别在 [../gpui_linux/src/linux/headless/client.rs:L28](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/headless/client.rs#L28)、[../gpui_linux/src/linux/x11/client.rs:L312](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L312)、[../gpui_linux/src/linux/wayland/client.rs:L743](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L743)。

#### 4.3.3 源码精读

[../gpui_linux/src/linux/platform.rs:L118-L136](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L118-L136)——`LinuxCommon` 字段表。注意 `signal: LoopSignal` 在公共层，而它停止的是后端的事件循环：这是外壳能「越权」停掉后端主循环的唯一手柄（见下面 `quit`）。

[../gpui_linux/src/linux/platform.rs:L139-L178](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L139-L178)——`LinuxCommon::new`：与上面流程图一一对应。文本系统的二选一（L149-152）呼应 u1-l3 的结论：零 feature 构建只有 headless 后端，配 `NoopTextSystem` 即可；`WindowButtonLayout::linux_default()`（L166）则是自绘标题栏时右侧按钮排列的默认值。

[../gpui_linux/src/linux/platform.rs:L180-L195](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L180-L195)——`start_wake_listener`：只在首次注册 `on_system_wake` 时调用（见 L562-567），用 `wake_listener_started` 标志保证监听任务只 spawn 一次。

[../gpui_linux/src/linux/platform.rs:L205-L227](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L205-L227)——`listen_for_system_wake`：连接 DBus system bus，订阅 `org.freedesktop.login1` 的 `PrepareForSleep` 信号；只在「唤醒」（`!sleeping`）时向 wake 通道发消息。这是 u2-l2 讲过的 login1 链路的实现现场。

再看外壳怎么用这些公共状态。`quit` 是最短也最妙的一个：

[../gpui_linux/src/linux/platform.rs:L280-L282](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L280-L282)——`Platform::quit` 只做一件事：`common.signal.stop()`。calloop 的 `LoopSignal` 让后端事件循环在下一轮回调后退出，控制权回到：

[../gpui_linux/src/linux/platform.rs:L267-L278](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L267-L278)——`Platform::run` 的三段式：先同步执行 `on_finish_launching`（Linux 特点，u2-l2 讲过），再阻塞在 `LinuxClient::run(&self.inner)`（后端主循环），循环退出后取出 `callbacks.quit` 执行。于是「quit 信号 → 循环退出 → on_quit 回调」串成一条完整的优雅退出链。

`run` 的后端侧对照（同一契约的三种阻塞方式）：

- [../gpui_linux/src/linux/headless/client.rs:L133-L142](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/headless/client.rs#L133-L142)：`event_loop.take().expect("App is already running")` 后 `event_loop.run(None, ...)`——裸 calloop 循环；
- [../gpui_linux/src/linux/wayland/client.rs:L1135-L1150](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L1135-L1150)：同样 take 出 event_loop，但状态参数是 `WaylandClientStatePtr`（弱引用包装，供 wayland-client 回调使用）；
- [../gpui_linux/src/linux/x11/client.rs:L1795-L1808](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L1795-L1808)：用 `context(...)` 而非 `expect` 报「已在运行」。

三者都用 `Option::take` 保证 `run` 只能调用一次——第二次调用拿到 `None` 即报错。

#### 4.3.4 代码实践

**实践目标**：跟踪一次 `quit` 的完整旅程，验证 `LoopSignal` 如何把外壳与后端主循环连起来（源码阅读型实践）。

1. 从 [../gpui/src/app.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui/src/app.rs) 中找到 `App` 上调用 `platform.quit()`（或 `Platform::quit`）的调用点，记下函数名；
2. 依序追下面五个站点并抄下行号：
   - `LinuxPlatform::quit`（[../gpui_linux/src/linux/platform.rs:L280-L282](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L280-L282)）
   - `LoopSignal::stop`（calloop 依赖里，`~/.cargo` 或 `cargo doc --open -p calloop` 可查）
   - 你所在平台后端的 `LinuxClient::run` 中 `event_loop.run(...)` 返回点
   - `LinuxPlatform::run` 的 `callbacks.quit.take()`（L272-277）
   - gpui 侧 `on_quit` 观察者的汇合点（u2-l2 讲过 `App::shutdown` 与 200ms 限时）
3. 把五个站点画成时序图（参与者：调用方 / LinuxPlatform / LinuxCommon / 后端主循环）。

**需要观察的现象**：`signal.stop()` 调用后循环**不是立即**退出——calloop 要等当前这轮事件处理完，所以 `on_quit` 回调总是晚于 `quit()` 返回。

**预期结果**：得到一条「`quit()` 发信号 → 后端 calloop 循环退出 → 外壳 run 的后半段接管 → on_quit 回调执行」的单向链路图；对照 u2-l2 的结论确认无矛盾。

**待本地验证**：calloop `LoopSignal::stop` 的精确退出时机可按你本地的 calloop 版本文档核对。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `LinuxDispatcher` 要包 `Arc` 且 clone 给两个执行器？

答案：`BackgroundExecutor` 与 `ForegroundExecutor` 是两个独立的值（前者可跨线程、后者钉在主线程），但它们必须共享同一份调度状态（三条优先级队列、唤醒逻辑），否则前后台任务会进两个互不知情的队列。`Arc` 让两个执行器持有同一个 `LinuxDispatcher`，这正是 u4-l1「平台只造一个 dispatcher、构造两个执行器」结论在 Linux 侧的代码形态。

**练习 2**：`LinuxCommon` 里的 `text_system` 为什么不放进后端？

答案：文本系统（字体枚举、整形）只依赖 font-stack 而不依赖窗口系统，三后端用的是同一套（Wayland/X11 下都是 `CosmicTextSystem`），放进公共层可避免三份重复；而它按 feature 二选一（L149-152）又恰好对应「后端是否存在」的编译期事实，所以放 `LinuxCommon::new` 里装配最合适。

**练习 3**：`wake_listener_started` 标志防止什么？

答案：防止重复 spawn 监听任务。应用可能多次调用 `on_system_wake` 覆盖注册回调（每次都会走 `start_wake_listener`），若没有该标志，每次注册都会再 spawn 一个 DBus 监听任务，同一信号会唤醒多次。标志保证监听任务全局仅一份，回调槽位则可随意覆盖。

### 4.4 PlatformHandlers：回调的集中登记处

#### 4.4.1 概念说明

`PlatformHandlers` 是九个 `Option<Box<dyn FnMut>>` 槽位的集合，代表 gpui 用户（如 zed 编辑器）通过 `Platform::on_*` 注册的回调。它解决的问题是**登记与触发的分离**：登记发生在外壳（`on_quit`、`on_system_wake`……都是 `Platform` 契约方法），而触发时机各不相同——有的在外壳 `run` 尾声，有的在后端事件处理中，有的来自 DBus。把回调集中存在 `LinuxCommon.callbacks` 里，任何一方都能通过 `with_common` 拿到并拉响。

`Option` 的语义是「最多一个回调、后注册覆盖先注册」；`Box<dyn FnMut>` 允许回调携带并修改捕获状态。

#### 4.4.2 核心流程

九个槽位的「谁存 / 谁触发」全表（本讲用 grep 逐一核实过）：

| 槽位 | 登记处（外壳 `Platform` 方法） | 触发者 | 触发时机 |
|---|---|---|---|
| `quit` | `on_quit`（L550-554） | `LinuxPlatform::run`（L272-277） | 后端事件循环退出后 |
| `system_wake` | `on_system_wake`（L562-567） | `LinuxCommon::handle_system_wake`（L197-202） | login1 `PrepareForSleep`（唤醒沿）经 wake 通道到达 |
| `keyboard_layout_change` | `on_keyboard_layout_change`（L256-259） | Wayland（wayland/client.rs:563-568）、X11（x11/client.rs:1525-1529） | 系统键盘布局变化事件 |
| `open_urls` | `on_open_urls`（L396-399） | ——当前无触发者 | 预留（桌面环境要求打开 URL 时） |
| `reopen` | `on_reopen`（L556-560） | ——当前无触发者 | 预留（macOS Dock 重开语义在 Linux 无对应） |
| `app_menu_action` | `on_app_menu_action`（L597-601） | ——当前无触发者 | 预留（Linux 无全局应用菜单） |
| `will_open_app_menu` | `on_will_open_app_menu`（L603-607） | ——当前无触发者 | 同上 |
| `validate_app_menu_command` | `on_validate_app_menu_command`（L609-613） | ——当前无触发者 | 同上 |

诚实结论：九个槽位里当前只有 3 个会被拉响（quit、system_wake、keyboard_layout_change），其余六个「已登记、暂无人拉响」——这不是缺陷，而是契约完备性与实现进度之间的常态差距，读源码时要能区分「承诺了」与「做到了」。

触发的惯用写法是 take → call → re-store：

```rust
if let Some(mut callback) = self.callbacks.system_wake.take() {
    callback();
    self.callbacks.system_wake = Some(callback);
}
```

先把回调从 `Option` 里拿走（独占持有）、调用、再放回去。之所以不直接 `(&mut callback)()`，是因为回调在 `RefCell` 保护的 `LinuxCommon` 深处，`take` 出来调用可以避免在调用期间继续持有 `common` 的借用——回调内部很可能反过来再访问平台状态。

#### 4.4.3 源码精读

[../gpui_linux/src/linux/platform.rs:L106-L116](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L106-L116)——`PlatformHandlers` 结构体：九个槽位全部 `Option<Box<dyn FnMut...>>`，`derive(Default)` 提供全空初始态。

登记侧，以 `on_system_wake` 为例：

[../gpui_linux/src/linux/platform.rs:L562-L567](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L562-L567)——存回调之外还顺手调用 `common.start_wake_listener()`：首次注册时才把 DBus 监听任务拉起来，体现「按需启动系统服务」。

触发侧两例：

[../gpui_linux/src/linux/platform.rs:L197-L202](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L197-L202)——`handle_system_wake`：take → call → re-store 的标准写法。它由后端在 wake 通道收到消息时调用，例如 [../gpui_linux/src/linux/headless/client.rs:L41-L46](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/headless/client.rs#L41-L46) 里 headless 把 `wake_receiver` 插进 calloop 后，每条消息都调一次 `client.with_common(|common| common.handle_system_wake())`。

[../gpui_linux/src/linux/wayland/client.rs:L563-L568](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L563-L568)——Wayland 后端处理键盘事件时发现布局变化（`changed`），拉响 `keyboard_layout_change`；X11 同型（[../gpui_linux/src/linux/x11/client.rs:L1525-L1529](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L1525-L1529)）。注意这里触发者绕过了外壳、直接摸后端自己 state 里的 `common`——后端本来就拥有 `LinuxCommon`，不需要经外壳中转。

#### 4.4.4 代码实践

**实践目标**：用 grep 复核上表的「谁触发」结论，体会「登记处统一、触发点分散」。

1. 在仓库根目录执行：

   ```bash
   rg -n "callbacks\.(quit|system_wake|keyboard_layout_change|open_urls|reopen|app_menu_action|will_open_app_menu|validate_app_menu_command)" crates/gpui_linux/src
   ```

2. 把输出按「= Some(...)（登记）」与「.take()（触发）」分成两栏，与 4.4.2 的表格对照；
3. 追一条完整链路：`on_system_wake` 注册 → `start_wake_listener` → `listen_for_system_wake`（DBus）→ `wake_sender.send(())` → 后端 calloop 的 `wake_receiver` → `handle_system_wake` → 用户回调。六个环节各记一个文件与行号。

**需要观察的现象**：grep 输出中 `open_urls`/`reopen`/三个菜单回调只有 `= Some(...)` 形式的赋值行，没有任何 `.take()` 行。

**预期结果**：你的两栏清单与 4.4.2 表格一致；`system_wake` 链路六环节行号齐全（其中 DBus 监听与 wake 通道的环节正是 4.3 已读过的 [../gpui_linux/src/linux/platform.rs:L205-L227](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L205-L227)）。

**待本地验证**：无——本实践是纯静态分析，输出可直接核对。

#### 4.4.5 小练习与答案

**练习 1**：为什么槽位类型是 `Option<Box<dyn FnMut>>` 而不是 `Box<dyn FnMut>`？

答案：三点。其一，允许「尚未注册」状态（`Default` 全空），否则构造 `LinuxCommon` 时就得填九个空闭包；其二，支持后注册覆盖先注册（`Some(callback)` 直接赋值）；其三，触发时的 take→call→re-store 惯用 法依赖 `Option::take` 把所有权暂时拿走。

**练习 2**：如果让你给 Linux 补上 `open_urls` 的触发（例如支持 `xdg-open` 反向唤起应用），改动会落在哪一层？

答案：触发点必须在能收到「打开 URL 请求」的层——最合理的是某个后端（或独立的 DBus 监听任务，类似 `listen_for_system_wake` 的做法）在收到请求时 `with_common` 后对 `callbacks.open_urls` 做 take→call→re-store。外壳的登记处代码（L396-399）无需改动，这正是集中登记带来的扩展性。

**练习 3**：`on_keyboard_layout_change` 的回调拉响后，gpui 上层会发生什么？

答案：回调沿着 gpui 的订阅机制通知所有观察键盘布局的实体——u3-l3 讲过「布局切换后 App 重建 keyboard_layout 与 mapper 并通知订阅者」，重建时调用的正是 `Platform::keyboard_layout()`，对 Linux 而言最终落到后端的 `LinuxClient::keyboard_layout()`。所以一次布局变化会先后经过：后端事件 → `callbacks.keyboard_layout_change` → gpui 重建 → `LinuxClient::keyboard_layout()` 读新值。

## 5. 综合实践

**任务**：画出 `LinuxPlatform` 调用 `LinuxClient` 方法的类图与序列图，并任选三个 `Platform` trait 方法（推荐 `read_from_clipboard`、`displays`、`open_window`）标注它们如何穿透到具体后端。完成后你将拥有一张 Linux 平台层的「总电路图」，后续三讲（headless/X11/Wayland）都以它为底图。

### 步骤

1. **先画类图**（参与者与持有关系）。参考答案（可据此核对）：

   ```text
   ┌───────────────────── gpui 主 crate（契约层）──────────────────────┐
   │  Platform（69 方法契约，u2-l1）                                    │
   └────────────▲──────────────────────────────────────────────────────┘
                │ Rc<dyn Platform>（唯一一次动态分发）
   ┌────────────┴─────────────────────────┐    inner        ┌─────────────────────┐
   │ LinuxPlatform<P: LinuxClient + 'static> ├───────────────►│ P ∈ {               │
   │  外壳：portal/凭据/通知/菜单/restart    │   静态分发      │   WaylandClient      │
   └────────────┬─────────────────────────┘                 │   X11Client          │
                │ with_common(&mut LinuxCommon)              │   HeadlessClient     │
   ┌────────────▼─────────────────────────┐   各自持有       └──────────┬──────────┘
   │ LinuxCommon                          │◄───────────────────────────┘
   │  executors / text_system / signal /  │
   │  appearance / menus / notifications  │
   └────────────┬─────────────────────────┘
   ┌────────────▼─────────────────────────┐
   │ PlatformHandlers（九个回调槽位）        │
   └──────────────────────────────────────┘
   ```

2. **再画序列图**。以下给出三个方法在 Wayland 后端下的参考答案（mermaid，可在 GitHub 直接渲染）：

   ```mermaid
   sequenceDiagram
     participant App as 应用代码
     participant GApp as gpui App
     participant LP as LinuxPlatform<br/>(Rc<dyn Platform>)
     participant WC as WaylandClient

     Note over App,WC: ① read_from_clipboard（A 类：纯委托）
     App->>GApp: cx.read_from_clipboard()
     GApp->>LP: Platform::read_from_clipboard() [app.rs:1395]
     LP->>WC: self.inner.read_from_clipboard() [platform.rs:747-749]
     WC-->>App: state.clipboard.read() [wayland/client.rs:1207-1209]

     Note over App,WC: ② displays（A 类：纯委托）
     App->>GApp: cx.displays()
     GApp->>LP: Platform::displays() [app.rs:1304 → platform.rs:360-362]
     LP->>WC: self.inner.displays()
     WC-->>App: outputs 累积值换算逻辑像素 [wayland/client.rs:929-942]

     Note over App,WC: ③ open_window（A 类：纯委托）
     App->>GApp: cx.open_window(options)
     GApp->>LP: Platform::open_window(handle, WindowParams) [platform.rs:384-390]
     LP->>WC: self.inner.open_window(handle, params)
     WC-->>App: Box<dyn PlatformWindow>（WaylandWindow）[wayland/client.rs:982 起]
   ```

3. **换两个后端重画**：把序列图终点分别换成 HeadlessClient（[headless/client.rs:L129-L131](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/headless/client.rs#L129-L131)、[L66-L68](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/headless/client.rs#L66-L68)、[L100-L109](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/headless/client.rs#L100-L109)）与 X11Client（[x11/client.rs:L1778-L1793](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L1778-L1793)、[L1549-L1562](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L1549-L1562)、[L1601 起](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L1601-L1605)），注意中段（外壳部分）完全不变。
4. **补一条 B 类与一条 C 类**：任选 `text_system()`（B 类，经 `with_common`）与 `prompt_for_paths`（C 类，外壳自实现 + 借 `window_identifier`），把它们的路径也画进图里，检验你对 4.2.2 三分类的掌握。

**预期结果**：一张覆盖 A/B/C 三类穿透路径、三种后端、标注了关键行号的总图。检验标准：随机挑一个 `Platform` 方法，你能在图上立刻指出它到具体后端的完整路径。

## 6. 本讲小结

- **两层分发**：`gpui_platform::current_platform` 在编译期用 `#[cfg]` 选出 Linux 分支并转发；`gpui_linux::current_platform` 在运行期按 headless 参数与 `guess_compositor()` 环境探测，从 Wayland/X11/headless 三后端中选定一个，包进 `LinuxPlatform<P>` 后擦成 `Rc<dyn Platform>` 注入应用。
- **外壳模式**：`LinuxPlatform<P>` 用一份代码为三后端实现 `Platform` 契约；泛型 `inner: P` 使后端调用是静态分发，动态分发只在 `Rc<dyn Platform>` 这一层发生一次。
- **LinuxClient 契约**：约 22 个方法（17 个必需 + 少量默认/feature 门控），只保留真正依赖窗口系统的能力；外壳的 `Platform` 方法按「纯委托 / 读公共状态 / 外壳自实现」三分类落位。
- **LinuxCommon**：后端各自持有、经 `with_common` 向外壳开放的公共状态——执行器两件套（共享 `Arc<LinuxDispatcher>`）、feature 决定的文本系统、UI 偏好缓存、`LoopSignal` 与 wake 通道；`quit()` 靠公共层持有的 signal 停掉后端主循环。
- **PlatformHandlers**：九个回调槽位集中登记、分散触发；当前只有 quit、system_wake、keyboard_layout_change 三个会被拉响，其余六个是已登记待实现的预留。
- **take → call → re-store**：触发回调的惯用写法，避免调用期间持有 `RefCell` 借用。

## 7. 下一步学习建议

本讲搭好了 Linux 平台层的骨架，接下来三讲按「从简到繁」深入三个后端：

1. **u5-l2 headless 客户端**：本讲多次把 `HeadlessClient` 当「最易读参考实现」，下一讲系统研究它如何支撑 CI、远程开发与 `ZED_HEADLESS` 场景，哪些接口刻意留空。
2. **u5-l3 X11 客户端**：重点看 XCB 连接建立、事件翻译，以及 `insert_idle` 回调执行完每个 runnable 后排空 x11rb 内部缓冲队列的机制（u4-l3 已铺垫）。
3. **u5-l4 Wayland 客户端**：协议对象、serial 管理、layer_shell 与 popup，理解「无全局坐标」模型对窗口接口的影响。

阅读源码时建议随身携带本讲综合实践画出的总图：每遇到一个后端方法，先在外壳里找到它的登记处与分类，再看后端实现，位置感会好很多。

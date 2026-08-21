# MacPlatform 与 AppKit：Cocoa 事件循环、外观与粘贴板

## 1. 本讲目标

本讲是第 6 单元（macOS 与 Windows 平台实现）的第一讲。前面五个单元里，我们一直在「平台无关契约」的视角下工作：`Platform` trait 定义了什么能力、Linux 三后端如何实现它们。从本讲开始，我们钻进一个具体平台的实现内部——macOS。

学完本讲，你应该能够：

1. 理解 macOS 主线程约束从哪里来、它如何影响 GPUI 的初始化顺序与测试写法（为什么 macOS 测试必须 `--ignored --test-threads=1` 运行）。
2. 描述 AppKit 事件（`NSEvent`）如何被翻译成 GPUI 的 `PlatformInput`，包括坐标系翻转与键盘布局的边角处理。
3. 说明 `NSPasteboard` 与 GPUI `ClipboardItem` 的双向转换，以及「查找粘贴板」这个 macOS 独有的系统级缓冲区。
4. 讲清 `CVDisplayLink` 如何为窗口提供垂直同步的帧步进信号，以及为什么这套实现选择「永生的 display link + 订阅者」结构。
5. 掌握 `NSAppearance` 名称与 `WindowAppearance` 枚举的映射关系。

## 2. 前置知识

本讲假设你已完成 u1、u2 单元，特别是：

- **u2-l1 的八大方法分组**：`Platform` trait 是 gpui 与操作系统之间的契约，本讲反复要问的问题是「这个 macOS 能力在契约上如何暴露」。
- **u2-l2 的生命周期语义**：`run(on_finish_launching)` 在各平台的回调时机不同，macOS 是「等 AppKit 通知」的那一类；本讲会看到它的具体机制。
- **u2-l4 的剪贴板模型**：`ClipboardItem` 是多格式条目（`ClipboardEntry::String / Image / ExternalPaths`），macOS 的 `Pasteboard` 正是这套模型的落地。
- **u4-l4 的 MacDispatcher**：macOS 调度器把任务外包给 GCD（主线程任务进 main queue，提交即唤醒主 run loop）。本讲的 `display_link` 与 `quit` 都会再次用到 `DispatchQueue::main()`。

此外需要一点 Objective-C 常识（不需要会写）：

- **消息发送**：Objective-C 的方法调用是运行期消息分发，Rust 侧用 `msg_send![对象, 选择器: 参数]` 宏表达。选择器（selector）形如 `applicationDidFinishLaunching:`。
- **AppKit 与 `NSApplication`**：macOS 的 GUI 工具kit。一个 GUI 应用只有一个 `NSApplication` 单例，调用它的 `run` 方法会进入事件循环并阻塞，直到应用退出。
- **委托（delegate）模式**：AppKit 不用回调函数注册，而是把「事件发生时该调谁」交给一个委托对象。委托实现一组约约成俗的选择器方法，AppKit 在恰当时机调用它们。这是典型的**控制流倒置**：不是你的代码驱动循环，而是系统循环反过来调用你塞进去的代码。
- **自动释放池（autorelease pool）**：Objective-C 的引用计数惯例，某些构造器返回的对象会被延迟释放，跨池持有需要显式 `retain`。

主线程约束的直觉版本：AppKit/Cocoa 的大多数 API 只能在进程的**第一个线程**（主线程）上调用，违反时进程会直接 `SIGABRT` 崩溃。这不是 Rust 的规则，而是 Apple 的规则；GPUI 的「单前台线程模型」（u4-l1）恰好与它同构，因此在 macOS 上两边严丝合缝。

## 3. 本讲源码地图

本讲的主角是 `gpui_macos` crate。它只会在 macOS 目标上被编译（由 `gpui_platform` 的 Cargo.toml 目标依赖段保证，见 u1-l3），目录结构是扁平的：

| 文件 | 作用 | 本讲涉及 |
| --- | --- | --- |
| `crates/gpui_macos/src/platform.rs` | `MacPlatform`：`Platform` trait 的 macOS 实现，ObjC 类注册、生命周期、菜单、凭据 | ★ 4.1 |
| `crates/gpui_macos/src/events.rs` | `NSEvent` → `PlatformInput` 的纯翻译层，含 `key_to_native` 反向映射 | ★ 4.2 |
| `crates/gpui_macos/src/pasteboard.rs` | `Pasteboard`：`NSPasteboard` 封装，通用/查找两块板 | ★ 4.3 |
| `crates/gpui_macos/src/display_link.rs` | `CVDisplayLink` 垂直同步帧步进（永生注册表 + 订阅者） | ★ 4.4 |
| `crates/gpui_macos/src/window_appearance.rs` | `NSAppearance` 名称 ↔ `WindowAppearance` 映射 | ★ 4.5 |
| `crates/gpui_macos/src/window.rs` | `MacWindow`：`PlatformWindow` 实现，消费上面四个模块 | 引用 |
| `crates/gpui_macos/src/dispatcher.rs` | `MacDispatcher`（u4-l4 已详述） | 引用 |
| `crates/gpui_macos/src/gpui_macos.rs` | crate 根，模块声明与再导出 | 略 |

契约侧参照文件：

- `crates/gpui/src/platform.rs`：`Platform` trait 权威定义，本讲用它核对「macOS 专属能力如何门控」。
- `crates/gpui_platform/src/gpui_platform.rs`：门面 crate，其测试模块里有主线程约束的直接证据。

## 4. 核心概念与源码讲解

### 4.1 MacPlatform：一个 Mutex、两个 ObjC 类与 Cocoa 事件循环

#### 4.1.1 概念说明

`MacPlatform` 是 `Platform` 契约在 macOS 上的实现。它要解决的核心问题是**控制流倒置**：GPUI 希望「启动时执行一次初始化回调，然后阻塞在事件循环里」，而 AppKit 的现实是——事件循环属于 `NSApplication`，事件发生时 AppKit 调用的是**委托对象上的选择器方法**，而不是你注册的 Rust 闭包。

所以 `MacPlatform` 的全部骨架工作就是：在 Rust 世界的闭包槽位与 ObjC 世界的委托方法之间架桥。

#### 4.1.2 核心流程

启动一条链：

1. `gpui_platform::application()`（u1-l4）在编译期选中 macOS 分支，构造 `MacPlatform`。
2. `Application::run(on_finish_launching)` 转发到 `Platform::run`。
3. `run` 把 `on_finish_launching` 存进状态（非 headless 时**不立即执行**），然后 `app.run()` 进入 AppKit 事件循环并阻塞。
4. AppKit 依次回调委托方法：`applicationWillFinishLaunching:` → `applicationDidFinishLaunching:`。
5. `did_finish_launching` 设置激活策略、注册键盘布局/热状态通知观察者，**然后才取出并执行**第 3 步存下的启动回调。

退出一条链（注意它被刻意做成异步的，原因见 4.1.3）：

1. `Platform::quit()` 被调用 → 向 GCD 主队列投递一个 `terminate:` 调用后立即返回。
2. GCD 在主 run loop 的下一轮执行 `terminate:`。
3. AppKit 关闭窗口（同步触发各窗口的 `on_close` 回调）→ `applicationWillTerminate:` → Rust 侧 `quit` 槽位里的回调。

#### 4.1.3 源码精读

**整体形态：一层 Mutex 包住所有状态。**

[crates/gpui_macos/src/platform.rs:166-194](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/platform.rs#L166-L194) 定义了 `MacPlatform(Mutex<MacPlatformState>)`。`MacPlatformState` 聚合了执行器、文本系统、两块粘贴板、以及一排 `Option<Box<dyn FnMut>>` 回调槽位（`reopen`、`quit`、`menu_command`、`open_urls`、`on_system_wake`……）。对照 u5-l1 的 `LinuxCommon` + `PlatformHandlers`：Linux 把公共状态拆成两个结构，macOS 则是单一状态体 + 一把锁——两种风格，同一件事。

**启动前奏：`#[ctor]` 注册 ObjC 类。**

[crates/gpui_macos/src/platform.rs:71-103](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/platform.rs#L71-L103) 在 `main` 之前用 `ctor` crate 的构造器注册两个动态声明的 ObjC 类：

- `GPUIApplication`：继承 `NSApplication`，附加一个名为 `platform` 的 ivar（实例变量），用来反向持有 Rust 侧 `MacPlatform` 的裸指针。
- `GPUIApplicationDelegate`：继承 `NSResponder`，同样带 `platform` ivar，并挂上约 20 个 `extern "C"` 方法——每一个都对应一个 AppKit 委托选择器。

```rust
unsafe fn build_classes() {
    unsafe {
        APP_CLASS = {
            let mut decl = ClassDecl::new("GPUIApplication", class!(NSApplication)).unwrap();
            decl.add_ivar::<*mut c_void>(MAC_PLATFORM_IVAR);
            decl.register()
        }
    };
    ...
```

这是 Rust ↔ ObjC 桥接的标准姿势：ObjC 类在运行期动态生成，ivar 里塞 Rust 指针；委托方法被调用时，用 [get_mac_platform](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/platform.rs#L1256-L1262) 从 ivar 里把指针还原成 `&MacPlatform`，再操作 Rust 状态。

**run：headless 与正常模式分叉。**

[crates/gpui_macos/src/platform.rs:491-518](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/platform.rs#L491-L518)：headless 时**同步执行**启动回调后直接 `CFRunLoopRun()`（CoreFoundation 层的 run loop，不经过 NSApplication——u4-l4 讲过 MacDispatcher 的任务投递最终唤醒的就是这个循环）；正常模式把回调存进 `state.finish_launching`，把 `self` 指针写进两个 ObjC 对象的 ivar，然后 `app.run()` 阻塞。

**did_finish_launching：启动回调真正执行的地方。**

[crates/gpui_macos/src/platform.rs:1281-1317](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/platform.rs#L1281-L1317) 做四件事：设置 `NSApplicationActivationPolicyRegular`（出现在 Dock、可激活）；向 `NSNotificationCenter` 注册键盘布局变更与热状态变更两个观察者；若 `on_system_wake` 已注册则补注册系统唤醒观察者；最后 `state.finish_launching.take()` 取出启动回调执行。这印证了 u2-l2 的结论：macOS 的启动回调时机「等 AppKit 通知」。

**quit：为什么必须异步。**

[crates/gpui_macos/src/platform.rs:520-538](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/platform.rs#L520-L538) 的注释是本模块最好的并发教材：

> 退出应用会关闭窗口，而窗口的 `Window::on_close` 回调会在本方法返回前**同步**执行。如果调用 `Platform::quit` 时还持有平台状态的锁（大多数时候确实如此），就会在 on_close 回调里二次借走同一把锁。解决办法：把 `terminate:` 投递到 GCD 主队列，让真正的终止发生在栈上不再持有任何借用的时刻。

[crates/gpui_macos/src/platform.rs:1371-1394](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/platform.rs#L1371-L1394) 的 `on_thermal_state_change` 用同样的「推迟到下一轮 run loop」手法，原因也相同：通知中心是**同步投递**的，可能在 App 已被借用时触发。

**主线程约束的书面证据。**

[crates/gpui_platform/src/gpui_platform.rs:99-115](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_platform/src/gpui_platform.rs#L99-L115) 的测试模块注释写得非常直白：

> 所有 VisualTestAppContext 测试默认 ignore，因为它们需要 macOS 主线程。标准 Rust 测试跑在工作线程上，与 AppKit/Cocoa API 交互会导致 SIGABRT。运行方式：`cargo test -p gpui visual_test_context -- --ignored --test-threads=1`。

注意第三个测试 [test_window_spawn_uses_test_dispatcher](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_platform/src/gpui_platform.rs#L173-L175) 的 ignore 理由更进一步：「窗口创建在测试线程上会失败」——不只是崩溃风险，`open_window` 这条链路本身就依赖主线程上的 AppKit 状态。u8-l4 会展开讲测试平台设施，这里先记住结论。

#### 4.1.4 代码实践

**实践目标**：把「ObjC 委托选择器 → Rust 状态槽位 → Platform 注册方法」三者的对应关系亲手梳理一遍，体会控制流倒置的全貌。

**操作步骤**（纯源码阅读，任何操作系统可做）：

1. 打开 [platform.rs 的 build_classes](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/platform.rs#L71-L163)，列出所有 `decl.add_method` 注册的选择器。
2. 对每个选择器，跳到对应的 `extern "C" fn`（如 `sel!(applicationShouldHandleReopen:hasVisibleWindows:)` → [should_handle_reopen](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/platform.rs#L1333-L1343)），看它取出状态里的哪个槽位。
3. 再在 `impl Platform for MacPlatform` 里（[L478 起](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/platform.rs#L478)）用编辑器搜索找到写入该槽位的注册方法（如 `on_reopen`，[L947-949](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/platform.rs#L947-L949)）。
4. 画一张三列对照表。

**需要观察的现象**：多数委托方法体都遵循同一模板——`take()` 出闭包、`drop(lock)` 释放锁、调用闭包、再把闭包 `get_or_insert` 塞回去。数一数有几个方法严格遵守了这个模板（答案：`should_handle_reopen`、`will_terminate`、`on_keyboard_layout_change`、`on_thermal_state_change`、`on_system_wake`、`open_urls`、`menu_will_open` 等）。

**预期结果**：得到形如下表的产物（节选）：

| ObjC 选择器 | Rust extern "C" 函数 | 状态槽位 | Platform 注册方法 |
| --- | --- | --- | --- |
| `applicationShouldHandleReopen:hasVisibleWindows:` | `should_handle_reopen` | `reopen` | `on_reopen` |
| `applicationWillTerminate:` | `will_terminate` | `quit` | `on_quit` |
| `onKeyboardLayoutChange:` | `on_keyboard_layout_change` | `on_keyboard_layout_change` | `on_keyboard_layout_change` |
| `applicationDockMenu:` | `handle_dock_menu` | `dock_menu`（读） | `set_dock_menu` |

「take → drop → call → re-store」模板的存在理由结合 4.1.3 的 quit 注释即可自答：回调内部可能再次借用状态，先取出来才能避免死锁/重入。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `MacPlatform` 用一个 `Mutex<MacPlatformState>` 而不是像 Linux 那样拆成多个字段分别加锁？

**参考答案**：macOS 上所有对平台状态的访问都发生在主线程（AppKit 约束 + GPUI 单前台线程模型），锁的竞争压力几乎为零；而单一状态体让「take 回调 → drop 锁 → 调用 → 回填」的重入规避模式可以一次性覆盖全部槽位，简单正确优先（这也符合仓库 CLAUDE.md 的编码取向）。Linux 侧因为 `LinuxCommon` 要被三种后端以不同方式共享借用，才需要更细的结构划分。

**练习 2**：headless 模式下 `run` 为什么可以直接调 `CFRunLoopRun()` 而不用 `app.run()`？

**参考答案**：headless 不需要 AppKit 的窗口/菜单/事件分发，只需要一个能让 MacDispatcher 投递的主线程任务得以执行的消息循环。CoreFoundation 层的 run loop 正是 GCD main queue 的服务者，够了且更轻。

**练习 3**：`did_finish_launching` 里为什么要专门检查 `state.on_system_wake.is_some() && !state.system_wake_observer_registered`？

**参考答案**：注册顺序不确定。若业务代码在 `run` 之前就调了 `on_system_wake`（此时 NSApplication 委托还不存在，[L971-988](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/platform.rs#L971-L988) 会发现委托为 nil 而注册失败），启动完成后就要在这里补注册；`system_wake_observer_registered` 标志防止两处重复注册同一个观察者。

### 4.2 macos::events：AppKit 事件到 PlatformInput 的翻译层

#### 4.2.1 概念说明

`events.rs` 是一个**无状态纯函数模块**：输入一个 `NSEvent`（ObjC 对象指针），输出一个 `Option<PlatformInput>`。它不持有任何状态、不注册任何回调，是整条输入链路里最容易测试的一层。

它解决两个问题：一是**事件类型枚举的翻译**（`NSEventType` 的十几种变体 → gpui 的 `KeyDown / MouseDown / ScrollWheel / Pinch / MousePressure …`）；二是**坐标系的翻译**——macOS 窗口坐标原点在**左下角**、y 轴向上，而 GPUI 统一用左上原点、y 轴向下（u2-l3 讲过这个约定）。

#### 4.2.2 核心流程

```
NSEvent（AppKit 分发）
    │
    ├─ handle_key_event（键盘类）──┐
    │                              ├─→ platform_input_from_native(event, Some(window_height))
    ├─ handle_view_event（鼠标类）─┘          │
    │                                         ├─ 过滤未定义事件类型（返回 None）
    │                                         ├─ 大 match 按 NSEventType 分派
    │                                         │    ├─ y 坐标翻转: y' = window_height − y
    │                                         │    └─ 修饰键翻译: command → platform 位
    │                                         └─ Some(PlatformInput) / None
    └─ window 的 event_callback（GPUI 世界）
```

键盘的特殊之处在于 `parse_keystroke`：`key`（布局无关的键帽语义）与 `key_char`（本次实际字符）的分离（u3-l3 讲过契约），在 macOS 上靠 `charactersIgnoringModifiers` + Carbon 的 `UCKeyTranslate` 双路实现。

#### 4.2.3 源码精读

**入口与过滤器。**

[crates/gpui_macos/src/events.rs:104-118](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/events.rs#L104-L118)：`platform_input_from_native` 先把不在 `NSEventType` 枚举里的裸值（`0 | 21 | 32 | 33 | 35 | 36 | 37`）过滤掉——这是 cocoa-rs 绑定的历史坑，注释链接了 servo 仓库的 issue。`window_height: Option<Pixels>` 参数决定鼠标类事件能否翻译：没有窗口高度就无法做 y 翻转，返回 `None`。

**y 轴翻转。** 以鼠标按下为例，[crates/gpui_macos/src/events.rs:151-163](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/events.rs#L151-L163)：

```rust
position: point(
    px(native_event.locationInWindow().x as f32),
    // MacOS screen coordinates are relative to bottom left
    window_height - px(native_event.locationInWindow().y as f32),
),
```

一行减法，就是两套坐标系的全部换算。

**修饰键映射。** [crates/gpui_macos/src/events.rs:85-102](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/events.rs#L85-L102) 的 `read_modifiers` 把 `NSEventModifierFlags` 翻成 gpui 的 `Modifiers`，关键一行是 `platform: command`——macOS 的「Cmd 键」在 gpui 里统一叫 platform 键（Windows 上是 win 键、Linux 上是 super 键，u3-l3 讲过这个归一化）。

**值得记的三处特色翻译：**

1. **滚轮精度区分**：[events.rs:272-276](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/events.rs#L272-L276) 按 `hasPreciseScrollingDeltas` 把触摸板的像素级增量（`ScrollDelta::Pixels`）与传统鼠标的行级增量（`ScrollDelta::Lines`）分开，`touch_phase` 则由 `NSEventPhase` 映射，供 gpui 侧做滚动动画惯性。
2. **滑动事件当导航键**：[events.rs:211-236](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/events.rs#L211-L236)，罗技 MX Master 这类鼠标把侧键报成 swipe 事件，这里把 `deltaX` 的正负翻译成 `MouseDown(Navigate(Back/Forward))`——设备差异在平台层就被抹平了。
3. **压感与捏合**：[events.rs:190-209](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/events.rs#L190-L209)（Force Touch 的 `MousePressure`）与 [events.rs:237-257](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/events.rs#L237-L257)（触控板捏合的 `Pinch`）。

**键盘：parse_keystroke 的双路策略。**

[crates/gpui_macos/src/events.rs:338-348](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/events.rs#L338-L348) 先取 `charactersIgnoringModifiers`，第一个字符若命中功能键码位表（space/backspace/F1-F35/方向键……，[L359-420](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/events.rs#L359-L420)）就直接得出布局无关的 `key`。普通字符则落入 [L421-482](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/events.rs#L421-L482) 的兜底分支，那里有一张珍贵的测试清单注释：

> 亚美尼亚 / Dvorak+QWERTY / 乌克兰 / 捷克 / 挪威 / 俄语 / 德语 QWERTZ 布局下，`s`、`7`、`;` 这些键在 none/cmd/cmd-shift 组合下的真实输出各不相同。

处理方式是调用 [chars_for_modified_key](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/events.rs#L512-L574)，它经 Carbon 的 `TISCopyCurrentKeyboardLayoutInputSource` 拿到当前布局，再用 `UCKeyTranslate` 反查「这个键码 + 这组修饰键会打出什么字符」。这些 Carbon 符号的 FFI 声明就在 [platform.rs:1515-1543](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/platform.rs#L1515-L1543)，被 events.rs 与 keyboard.rs 共用。Cmd 按下时 macOS 会切到「命令布局」（通常返回 ASCII），代码据此修正 `key` 与 `key_char`，保证任何布局下 `cmd-s` 都能命中绑定。

**反向映射 key_to_native。** [events.rs:29-83](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/events.rs#L29-L83) 把 gpui 键名（`"pageup"`、`"f5"`）翻成 `NSMenuItem` 需要的 key equivalent 字符——它服务于 4.1 的菜单构建，方向与 `parse_keystroke` 恰好相反。

**调用方。** 翻译产物的消费者在 window.rs：[handle_key_event](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/window.rs#L2407-L2416)（键盘）与 [handle_view_event](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/window.rs#L2548-L2566)（鼠标），两者都传入 `Some(window_height)` 后把结果交给窗口的 `event_callback`——即 gpui `Window` 注册的回调，至此事件进入平台无关世界。

#### 4.2.4 代码实践

**实践目标**：制作一张完整的 `NSEventType → PlatformInput` 映射表，并标出每处非平凡转换。

**操作步骤**：

1. 通读 [platform_input_from_native](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/events.rs#L104-L336) 的大 match。
2. 建一张五列表：`NSEventType 变体` / `PlatformInput 变体` / `是否需要 window_height` / `特殊处理` / `行号`。
3. 对照 u3-l3 学过的 Linux 版翻译（`keystroke_from_xkb`）与 Web 版（浏览器已翻译的 `event.key`），在表末尾追加两行总结三者分工差异。

**需要观察的现象**：几乎所有鼠标类分支都有 `window_height.map(|window_height| ...)` 包裹；`NSFlagsChanged` 与键盘分支则不需要。被显式丢弃（返回 `None`）的情况有三种：未定义事件类型、不认识的鼠标键号（`_ => return None`）、swipe 方向为零。

**预期结果**：约 14 行的映射表。特别应包含：`NSEventTypeSwipe → MouseDown(Navigate)` 这一反直觉行，以及 `NSEventTypePressure / NSEventTypeMagnify` 这两个 gpui 有对应事件的冷门项。

**待本地验证**（需要 macOS）：写一个最小 gpui 程序，在窗口上监听 `on_mouse_move / on_scroll_wheel`，从 Finder 用三指拖入一个文件，确认 `MouseDown` 的坐标与视觉位置一致（验证 y 翻转）；再用 `log` 打印 `Keystroke`，切换系统键盘布局（系统设置 → 键盘 → 输入法）后按同一个物理键，观察 `key` 稳定而 `key_char` 变化。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `platform_input_from_native` 是 `unsafe fn`，却敢自称「纯翻译层」？

**参考答案**：`unsafe` 仅因为它接收 `id`（裸 ObjC 指针）并调用其方法——这是与 ObjC 世界交互的必然。但函数本身不读写任何全局或 Rust 侧状态，输出只由输入事件与 `window_height` 决定，逻辑上是纯函数，因此可以放心地在任何持有有效事件的地方调用。

**练习 2**：`Modifiers.platform` 在 macOS 上映射 `command`。如果用户按的是 `ctrl`，gpui 里对应哪个字段？

**参考答案**：`control`。`read_modifiers`（[events.rs:85-102](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/events.rs#L85-L102)）把 `NSControlKeyMask → control`、`NSCommandKeyMask → platform`，一一对应，没有合并。

**练习 3**：`parse_keystroke` 里 `prefer_character_input` 被硬编码为 `false`（[events.rs:131-135](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/events.rs#L131-L135)）。结合 u3-l3，这个字段的语义是什么？

**参考答案**：该标志提示输入处理「优先把本次按键当文本输入而不是键位绑定」。macOS 上普通文本输入走 `NSTextInputClient`（输入法）链路而非 `KeyDown` 直通，所以从 NSEvent 直接翻译时恒为 false；对照 Web 平台的 `keydown` 事件则需要区分，这是平台输入架构差异在数据模型上的投影。

### 4.3 macos::pasteboard：NSPasteboard 封装与查找粘贴板

#### 4.3.1 概念说明

macOS 的剪贴板叫 `NSPasteboard`，是一块**按类型（type）存取**的共享内存：写入方声明若干类型（如 `public.utf8-plain-text`、`public.png`），每种类型挂一段数据；读取方按类型询问。同一块板上可以同时存在多种表示。

`Pasteboard` 结构体把这块板封装成 gpui 的 `ClipboardItem` 世界，并额外做两件事：

1. **私有侧车类型**：`zed-text-hash` 与 `zed-metadata`。写入文本时顺手写入文本哈希与元数据（语法高亮信息，u2-l4 讲过这个动机）；读取时**只有哈希对得上才采信元数据**——防止「别的应用只改了文本、留下我们过期元数据」的错配。
2. **查找粘贴板**：macOS 有一块系统级的 `NSPasteboardNameFind` 板，全体应用共享，存放「最近一次查找的内容」。支持它的应用里 `cmd-E`（以选中内容查找）与 `cmd-G`（跳到下一个）天然跨应用连贯。

#### 4.3.2 核心流程

读取优先级（`read()`）：

```
1. NSFilenamesPboardType 有文件路径？
      → entries = [ExternalPaths(路径列表)]
        若同时有字符串表示 → 追加 String entry（让编辑器能按文本粘贴路径）
2. 否则有 public.utf8-plain-text？
      → String entry；text_hash 校验通过则附带 metadata
3. 否则遍历 ImageFormat::iter() 逐个试图片类型
      → Image entry
4. 都没有 → None
```

写入按 entries 形态分派：空列表 → 清空剪贴板；单条 String → `write_plaintext`（文本 + 可选哈希/元数据侧车）；单条 Image → 按格式写 UTI；只有 ExternalPaths → 不写（源文件路径不外发）；多条 → 只合并字符串部分。

#### 4.3.3 源码精读

**结构体与两块板。**

[crates/gpui_macos/src/pasteboard.rs:22-50](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/pasteboard.rs#L22-L50)：`Pasteboard` 持 `StrongPtr`（显式 retain 的 ObjC 强引用）与两个侧车类型名。构造器注释解释了为何必须 retain：这些构造器返回自动释放对象，而 `Pasteboard` 可能活过它诞生的 autorelease pool。`general()` 与 `find()` 分别绑定 [generalPasteboard](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/pasteboard.rs#L29-L31) 和 [pasteboardWithName(NSPasteboardNameFind)](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/pasteboard.rs#L33-L35)——`NSPasteboardNameFind` 符号直接从 AppKit 框架链接（[L252-256](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/pasteboard.rs#L252-L256)）。

对应地，`MacPlatformState` 里存了两块板（[platform.rs:174-175](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/platform.rs#L174-L175)），`MacPlatform` 的四个 Platform 方法各自转发（[platform.rs:1123-1141](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/platform.rs#L1123-L1141)）：

```rust
fn read_from_find_pasteboard(&self) -> Option<ClipboardItem> {
    let state = self.0.lock();
    state.find_pasteboard.read()
}
```

注意契约侧的门控：这两个方法在 `Platform` trait 里是**编译期 cfg 门控**的——[crates/gpui/src/platform.rs:329-332](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/platform.rs#L329-L332)：

```rust
#[cfg(target_os = "macos")]
fn read_from_find_pasteboard(&self) -> Option<ClipboardItem>;
#[cfg(target_os = "macos")]
fn write_to_find_pasteboard(&self, item: ClipboardItem);
```

非 macOS 目标上这两个方法**根本不存在**，调用点也必须同样 cfg 门控——与 Linux 主选区（`read_from_primary`，L325-327 的 cfg）是同一手法。这是「平台专属能力进契约」的三种方式中最强的一种，另两种见本讲综合实践。

**哈希校验的读取路径。**

[crates/gpui_macos/src/pasteboard.rs:114-142](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/pasteboard.rs#L114-L142)：读字符串时先看板上有没有 `zed-text-hash` 与 `zed-metadata`，再把哈希字节还原成 `u64`，与当前文本重新计算的 `ClipboardString::text_hash(&text)` 比对，一致才把 metadata 附上。

**写入的侧车。**

[crates/gpui_macos/src/pasteboard.rs:204-234](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/pasteboard.rs#L204-L234)：`write_plaintext` 先 `clearContents`，写 `NSPasteboardTypeString`，有元数据时再补写哈希与元数据两个自定义类型。图片路径在 [L236-249](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/pasteboard.rs#L236-L249)，格式名由 [UTType 辅助结构](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/pasteboard.rs#L258-L330) 提供——PNG/TIFF 用 AppKit 内置常量，WebP/SVG 等用 UTI 字符串（`org.webmproject.webp`、`public.svg-image`）。

**现成的测试。**

这个模块自带一组高质量单测（macOS 上运行）：[test_string](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/pasteboard.rs#L373-L405) 验证「写入带元数据的条目后读回等值」以及「模拟其他应用只写文本时读回无元数据」；[test_read_external_path](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/pasteboard.rs#L419-L447) 用 `simulate_external_file_copy` 模拟 Finder 拷贝文件，断言 entries 同时含 `ExternalPaths` 与字符串两份。测试用的是 `Pasteboard::unique()`（私有命名板，不污染真实剪贴板）。

#### 4.3.4 代码实践

**实践目标**：通过阅读现成测试反推行为规格，理解 `ClipboardItem` 往返（round-trip）的边界。

**操作步骤**：

1. 精读 [test_string](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/pasteboard.rs#L373-L405)，列出三个断言块各自的意图。
2. 精读 [test_read_multiple_external_paths](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/pasteboard.rs#L468-L498)，回答：Finder 拷贝两个文件后，`read()` 返回的 entries 里字符串条目的文本是什么格式？
3. 检查 `write()` 的 `[ClipboardEntry::ExternalPaths(_)] => {}` 分支（[L172](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/pasteboard.rs#L172)），思考它为何是空实现。

**需要观察的现象**：测试如何在不依赖其他应用的情况下模拟「外部世界写入」——`declareTypes_owner` + `setData_forType` 直接操作底层板。

**预期结果**：能口述三条规格——①带元数据的条目写读往返无损；②外部应用只写文本时读回的 metadata 为 None（哈希不匹配）；③多文件拷贝的字符串表示是换行连接的路径列表（`"/file.txt\n/image.png"`）。

**待本地验证**（需要 macOS）：`cargo test -p gpui_macos pasteboard` 直接运行这组测试；再用 `pbpaste` 命令行工具对比 Zed 复制代码后系统剪贴板里除文本外是否多了两个自定义类型（可用 `osascript` 或 `NSPasteboard` 探针查看类型列表）。

#### 4.3.5 小练习与答案

**练习 1**：为什么读取时要校验 `zed-text-hash`，而不是直接信任 `zed-metadata`？

**参考答案**：剪贴板是全体应用共享的。另一个应用可能只覆写字符串类型而不清理我们的自定义类型，于是板上会出现「新文本 + 旧元数据」的错配组合。哈希是文本与元数据之间的一致性凭证：哈希不匹配说明文本已被替换，元数据作废。

**练习 2**：查找粘贴板与通用粘贴板在 `Pasteboard` 封装层面有任何代码差异吗？差异体现在哪一层？

**参考答案**：封装层零差异——两个 `Pasteboard` 实例只是绑定了不同名字的板，读写逻辑完全复用。差异全部在语义层：`NSPasteboardNameFind` 是系统约定的查找缓冲区；以及在 Platform 契约层，两个 find 方法带 `#[cfg(target_os = "macos")]` 门控而通用方法没有。

**练习 3**：对照 u2-l4：Linux 的剪贴板实现里，与「侧车类型」最接近的机制是什么？

**参考答案**：Linux X11/Wayland 选区模型同样支持多 target（类型），gpui_linux 的剪贴板也注册了自定义 target 携带文本哈希与元数据，思路同源；差别在协议层——X11 需要拥有者进程持续应答 `SelectionRequest`（u5-l3），而 NSPasteboard 是系统服务代管的板。

### 4.4 macos::display_link：CVDisplayLink 与垂直同步帧步进

#### 4.4.1 概念说明

前三讲（u3-l2、u5-l4）我们见过两种帧驱动：Wayland 用 frame callback 按需唤醒，headless 完全不驱动。macOS 的答案是 `CVDisplayLink`：CoreVideo 提供的、绑定到某块显示器的垂直同步信号源——每当显示器刷新（60Hz 显示器约每 16.67ms，即 \( \Delta t = 1/f \approx 16.67\,\mathrm{ms} \)），它在一个高优先级 io 线程上触发你注册的输出回调。

这个模块的真正课题不是「怎么用 CVDisplayLink」（那只有四个 FFI 调用），而是**怎么安全地停用它**。模块开头的 51 行文档注释是整个 crate 最值得精读的文字之一：`CVDisplayLinkStop` 只标记 io 线程「尽快退出」并立刻返回，没有任何手段知道最后一次回调何时（是否）结束——直接释放对象会让 io 线程读已释放内存，历史上产生过两类真实崩溃（`CVHWTime::reset` 段错误与 Sentry ZED-7XR 的 `dispatch_source_merge_data` 段错误）。

#### 4.4.2 核心流程

设计要点：**每块显示器一条永生（immortal）的 display link，窗口作为订阅者挂上去**。

```
窗口可见 / 移到新屏幕
    → MacWindow::start_display_link
        → WindowFrameSource::start(display_id)
            → subscribe()：注册表加一个订阅者；该屏无订阅者则 start link
显示器刷新（io 线程）
    → display_link_output_callback
        → 对该屏每个订阅者 source 调 merge_data(1)   ← 线程安全的 GCD 原语
GCD 主队列合并触发
    → step(view)（window.rs）
        → request_frame_callback(Default::default())  ← gpui 的「请求下一帧」
窗口遮挡 / 关闭
    → stop_display_link / Drop
        → unsubscribe()：移除订阅者；无人订阅则 stop link（link 本身永不释放）
```

三条纪律支撑这个结构：①link 创建后永不释放，释放竞态无从发生；②回调上下文放的是 display id 整数而非指针，掉队的回调只能摸到静态注册表，发现没有订阅者就空转；③每个窗口自持一个 dispatch source，关闭时先从注册表摘除（回调够不着了）再 cancel 再真正释放。另有一条「link 的启停只在有/无订阅者时发生」的规则：两个窗口共享一块屏时，交错启停不会互相踩。

#### 4.4.3 源码精读

**注册表与订阅者。**

[crates/gpui_macos/src/display_link.rs:65-96](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/display_link.rs#L65-L96)：全局 `REGISTRY: Mutex<Registry>`，按 `CGDirectDisplayID`（u2-l3 讲过这个不透明句柄）索引 `DisplayEntry { link, running, subscribers }`，订阅者是 `(SubscriberId, DispatchRetained<DispatchSource>)`。锁选 `std::sync::Mutex` 而非 parking_lot 也是有意的——注释说明 macOS 上它由 `os_unfair_lock` 实现，其优先级捐赠能缓解高优先级 io 线程与主线程之间的反转（[L47-51](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/display_link.rs#L47-L51)）。

**输出回调。**

[crates/gpui_macos/src/display_link.rs:119-135](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/display_link.rs#L119-L135)：

```rust
unsafe extern "C" fn display_link_output_callback(
    ...,
    display_id: *mut c_void,
) -> i32 {
    let display_id = display_id as usize as CGDirectDisplayID;
    let registry = lock_registry();
    if let Some(entry) = registry.displays.get(&display_id) {
        for (_, frame_requests) in &entry.subscribers {
            frame_requests.merge_data(1);
        }
    }
    0
}
```

io 线程上唯一的动作就是给每个订阅者的 dispatch source `merge_data(1)`——计数式数据源天然**合并**高频信号，真正的执行被搬到主队列。这与 u4-l4 Windows dispatcher 的「唤醒合并」是同一思想在不同 GCD/Win32 原语上的落地。

**订阅/退订。** [subscribe](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/display_link.rs#L137-L202) 在锁外创建新 link（遵守「持锁时不调 CoreVideo」的锁序纪律，见 [L39-45](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/display_link.rs#L39-L45) 的注释），锁内插入订阅者，再在锁外 `link.start()`。[unsubscribe](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/display_link.rs#L204-L226) 移除订阅者，空了才 stop——注释点明：stop 之后仍可能再来一次回调，它只会发现没有订阅者而空转。

**窗口侧的封装。**

[WindowFrameSource](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/display_link.rs#L231-L270)：`new` 在主队列上建 data source，context 指向窗口的原生 view，事件处理器是函数指针 `step`；构造后立即 `resume()`——注释引用 #50875：销毁一个 suspended 的 dispatch source 是未定义行为。[Drop](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/display_link.rs#L272-L282) 的顺序同样讲究：先 unsubscribe（让回调够不着）再 cancel（保证事件处理器不再运行，因为它 context 指向可能已释放的 view）。

**谁在启停它。** window.rs 中 `MacWindowState` 持有 [frame_source 字段](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/window.rs#L598)；[start_display_link](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/window.rs#L759-L779) 先检查窗口遮挡状态（`NSWindowOcclusionStateVisible`）、再取窗口所在屏的 display id、然后 `get_or_insert_with(|| WindowFrameSource::new(data, step)).start(display_id)`。启停的触发点有三处：遮挡状态变化（[L2710-2719](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/window.rs#L2710-L2719)，完全被遮住的窗口停掉信号）、窗口换屏（[L2803-2809](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/window.rs#L2803-L2809)）、窗口销毁（[Drop for MacWindow](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/window.rs#L1300-L1306) 的 `frame_source.take()`）。

**终点：step。** [window.rs:2995-3005](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/window.rs#L2995-L3005)：主队列上的 `step(view)` 取出 `request_frame_callback` 调用之——这个回调正是 gpui `Window` 侧注册的「渲染下一帧」入口。于是整条链闭环：**显示器刷新 → io 线程 merge_data → 主队列 step → GPUI 画一帧**。

#### 4.4.4 代码实践

**实践目标**：把从「显示器刷新」到「GPUI 画帧」的完整链路画成时序图，并标注线程边界。

**操作步骤**：

1. 从 [display_link.rs 的模块注释](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/display_link.rs#L1-L51) 入手，先列出两条历史崩溃的成因。
2. 按顺序通读：`WindowFrameSource::new` → `subscribe` → `display_link_output_callback` → `step`。
3. 画时序图，泳道四条：`CVDisplayLink io 线程`、`REGISTRY 锁`、`GCD 主队列`、`GPUI Window`。
4. 在图上用不同颜色标出「持锁区间」，验证注释中的锁序纪律（持锁时绝不调用 CoreVideo 函数）是否被代码遵守。

**需要观察的现象**：回调在 io 线程只做了「查表 + merge_data」两件事，没有任何渲染相关动作；所有可能耗时的部分都被推到了主队列。

**预期结果**：一张能回答下列问题的图——同一显示器上两个 Zed 窗口时链路如何复用？窗口拖到另一块屏后旧屏的 link 何时 stop？被完全遮挡的窗口为什么不再收信号？

**待本地验证**（需要 macOS）：在 Zed 里打开两个窗口拖到同一屏，用 Instruments 的 Point of Interest 或简单日志观察共享同一条 link 的行为；把窗口最小化/遮挡后确认 CPU 占用下降。

#### 4.4.5 小练习与答案

**练习 1**：为什么不干脆每个窗口自建一条 `CVDisplayLink`、窗口关闭时释放？

**参考答案**：`CVDisplayLinkStop` 无法安全等待 io 线程结束，释放对象存在 use-after-free 竞态（模块注释记录的两类崩溃）。旧修法是泄漏 link，但「每次 resize/激活/遮挡变化都泄漏一个对象」不可持续。共享一条永生 link + 可安全释放的订阅者，把「不可安全销毁」的范围压缩到每屏一个对象。

**练习 2**：`display_link_output_callback` 的 `display_id` 参数为什么传整数而不是指向 Rust 结构的指针？

**参考答案**：掉队（stop 之后仍到达）的回调可能读它持有的任何指针。整数 id 无所有权语义，回调用它只能查静态注册表；查不到订阅者就是无害空转。这是「让迟到的回调无事可做」的防御性设计。

**练习 3**：对照 u5-l4 的 Wayland FrameLoop：两者都在窗口「无活可干」时停止信号，恢复时机有何不同？

**参考答案**：Wayland 靠协议侧 frame callback 与 `schedule_frame` 显式唤醒，窗口停泊于 Parked 后由 GPUI 在有脏区时主动请求；macOS 的信号源启停由**平台事件**驱动——遮挡状态、换屏、窗口关闭——gpui 侧不感知停泊概念，只要窗口可见，每帧刷新都会经 step 调一次 `request_frame_callback`，是否有活可干由回调内部判断。

### 4.5 window_appearance：NSAppearance 名称映射与外观覆盖

#### 4.5.1 概念说明

u3-l4 讲过：`window_appearance()` 四平台皆可查询，但 `set_window_appearance` 只有 macOS 真正生效。原因也在那一讲提过——macOS 的窗口 chrome（边框、标题栏）由 AppKit 渲染，应用若自己切成暗色主题，必须通知 NSApplication 把 chrome 一起切了，否则「暗色内容 + 亮色标题栏」很难看；其他平台的窗口装饰要么由 gpui 自绘（Linux CSD）要么不管（Windows），无需此能力。

本模块是那个结论的落点：`NSAppearance` 对象只有名字可查，映射全靠字符串比对。

#### 4.5.2 核心流程

查询：`NSApplication.effectiveAppearance` → 取 `name` → 四个名字逐一比对 → `WindowAppearance` 枚举。

覆盖：`set_window_appearance(Some(appearance))` → 按枚举反查名字 → `NSAppearance.appearanceNamed:` 造对象 → `setAppearance:`；`None` → 传 nil → 清除覆盖、回归系统设置。

#### 4.5.3 源码精读

**名字到枚举。**

[crates/gpui_macos/src/window_appearance.rs:10-29](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/window_appearance.rs#L10-L29)：

```rust
pub(crate) unsafe fn window_appearance_from_native(appearance: id) -> WindowAppearance {
    let name: id = msg_send![appearance, name];
    unsafe {
        if name == NSAppearanceNameVibrantLight {
            WindowAppearance::VibrantLight
        } else if name == NSAppearanceNameVibrantDark {
            ...
```

四个名字两两配对：`Aqua`/`DarkAqua` → `Light`/`Dark`，`VibrantLight`/`VibrantDark` → 对应 Vibrant 变体（这两个 Vibrant 名字是 u3-l4 提过的「macOS 独有枚举值」的来源）。注意 `NSAppearanceNameAqua/DarkAqua` 两个符号不在 cocoa crate 绑定里，模块底部直接从 AppKit 框架链接（[L31-35](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/window_appearance.rs#L31-L35)）。未知名字时打印日志并兜底 `Light`——系统将来新增外观名也不会崩。

**查询入口。** [platform.rs:680-686](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/platform.rs#L680-L686)：`window_appearance()` 问的是 `effectiveAppearance`——「生效中」的外观，即覆盖值优先、无覆盖时回落系统设置，保证读写语义自洽（trait 文档也写明覆盖会反映在 `window_appearance` 上）。

**覆盖入口。** [platform.rs:688-709](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/platform.rs#L688-L709)：`set_window_appearance` 的注释点出 `None => nil` 的语义——设置 nil appearance 即清除覆盖。契约侧 [crates/gpui/src/platform.rs:171-179](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/platform.rs#L171-L179) 的文档写得很清楚：「目前仅 macOS 实现，其他平台为 no-op」——注意这里用的是**默认空实现**而非 cfg 门控，与 find pasteboard 的编译期门控形成对照（原因：外观查询/覆盖对所有平台都是合法调用，只是不生效；而 find pasteboard 的 API 本身就只对 macOS 有意义）。

#### 4.5.4 代码实践

**实践目标**：厘清「一个外观值从系统到 gpui 再回系统」的完整往返。

**操作步骤**：

1. 阅读 [window_appearance_from_native](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/window_appearance.rs#L10-L29) 与 [set_window_appearance](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/platform.rs#L688-L709)。
2. 画一张状态表：系统浅色 + 无覆盖、系统浅色 + 覆盖 Dark、系统深色 + 覆盖 None，三种情形下 `window_appearance()` 各返回什么。
3. 在 gpui 主 crate 里搜索 `WindowAppearance::VibrantLight` 的使用处（用 Grep 搜 `VibrantDark`），看哪些主题映射到了 Vibrant 变体。

**需要观察的现象**：枚举到名字、名字到枚举两张映射表是否严格互逆（是的——`Light↔Aqua`、`Dark↔DarkAqua`、`VibrantLight↔VibrantLight`、`VibrantDark↔VibrantDark`）。

**预期结果**：能不假思索地回答「覆盖 Dark 后 `window_appearance()` 返回 Dark；清除覆盖后返回系统当前值」。

**待本地验证**（需要 macOS）：在 Zed 设置里切换主题为暗色，观察标题栏是否随之变暗；用 `defaults write -g AppleInterfaceStyle Dark` 切系统外观后确认无覆盖时 gpui 查询值跟随系统。

#### 4.5.5 小练习与答案

**练习 1**：`effectiveAppearance` 与 `appearance` 两个 NSApplication 属性有什么区别？gpui 各用在哪？

**参考答案**：`appearance` 是「覆盖值」（未设置时为 nil），`effectiveAppearance` 是「实际生效值」（覆盖优先，否则系统值）。gpui 查询用 `effectiveAppearance`（[platform.rs:683](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/platform.rs#L683)），设置用 `setAppearance:`（[L707](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/platform.rs#L707)）。

**练习 2**：`set_window_appearance` 在 Platform trait 上为什么不用 `#[cfg(target_os = "macos")]` 门控？

**参考答案**：三方（Linux/Windows/Web）调用它是合法且常见的（应用层不想为此写条件编译），只是不生效。默认空实现的 no-op 姿态（u2-l1 讲过的三种默认实现姿态之二）正好表达「合法但无能力」。find pasteboard 则相反：API 语义本身只属于 macOS，编译期排除更干净。

**练习 3**：为什么未知外观名要兜底 `Light` 而不是 panic？

**参考答案**：Apple 可能随系统更新引入新的外观名；查询接口不值得为未知值崩溃。`Light` 是最保守的默认（对应 Aqua，macOS 有史以来的默认外观），打印日志留排查线索即可。

## 5. 综合实践

**任务：整理一份《macOS 专属能力清单》**——这正是理解「平台能力如何进入平台无关契约」的毕业检查。

对下表每一项，亲自打开源码核对三列信息，并补全最后一列「门控方式」。我们已经在正文里见过全部三种门控方式，这里系统化：

| 门控方式 | 机制 | 契约侧特征 |
| --- | --- | --- |
| A. 编译期 cfg | `#[cfg(target_os = "macos")]` 直接标在 trait 方法上 | 非 macOS 上方法不存在，调用点也须 cfg |
| B. 默认 no-op | trait 提供空/降级默认体，仅 macOS 覆写 | 所有平台可调用，其余平台无效果 |
| C. 必需方法 | 无默认体，各平台都必须实现（非 macOS 常为空体或假值） | 契约强制所有平台表态 |

待核对清单（每项填：trait 声明位置 / macOS 实现位置 / 门控方式 A/B/C）：

1. **外观覆盖** `set_window_appearance` —— 线索：[gpui/src/platform.rs:171-179](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/platform.rs#L171-L179) 与本讲 4.5。
2. **查找粘贴板** `read_from_find_pasteboard` / `write_to_find_pasteboard` —— 线索：[gpui/src/platform.rs:329-332](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/platform.rs#L329-L332) 与本讲 4.3。
3. **Dock 菜单** `set_dock_menu` —— 线索：[gpui/src/platform.rs:236-238](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/platform.rs#L236-L238)；macOS 实现在 [platform.rs:1057-1067](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/platform.rs#L1057-L1067)（构建 NSMenu）与 [handle_dock_menu](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/platform.rs#L1493-L1503)（`applicationDockMenu:` 选择器返回它）。注意它是**必需方法**——想想 Linux 的空实现长什么样（可去 `gpui_linux/src/linux/platform.rs` 验证）。
4. **红绿灯按钮位置** `set_traffic_light_position` —— 线索：[gpui/src/platform.rs:884-885](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/platform.rs#L884-L885)，这是 `PlatformWindow` trait 上的方法（本讲未展开，属 u3-l2 的领域）。
5. **display link 帧步进** —— 特别注意：它**不在任何 trait 上**！线索：本讲 4.4。这是清单里最重要的反例：纯粹的平台实现细节，对外只体现为「窗口按垂直同步节奏收到帧请求」，契约上没有对应方法。
6. **热状态** `thermal_state` / `on_thermal_state_change` —— 线索：[gpui/src/platform.rs:250-251](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/platform.rs#L250-L251) 与 [platform.rs:990-1002](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/platform.rs#L990-L1002)（NSProcessInfo 查询）。
7. **Keychain 凭据** `write_credentials` 等 —— 线索：[platform.rs:1143-1246](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/platform.rs#L1143-L1246)（Security.framework 的 `SecItemAdd/Update/Delete/CopyMatching`，即 u2-l4 讲的「分落 Keychain」）。
8. **重开回调** `on_reopen`（点 Dock 图标但无窗口）—— 线索：[platform.rs:1333-1343](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/platform.rs#L1333-L1343)。
9. **屏幕捕获** `is_screen_capture_supported` —— 线索：[platform.rs:626-637](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/platform.rs#L626-L637)。这里是**第四种门控**：`#[cfg(feature = "screen-capture")]`——按 feature 而非按操作系统。
10. **系统通知** —— 线索：[platform.rs:1004-1023](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/platform.rs#L1004-L1023) 转发给 `system_notifications.rs`（u6-l3 的主角，先只记位置）。

**参考答案**（做完再看）：1→B；2→A；3→C；4→A；5→不在契约上；6→C；7→C；8→C；9→feature 门控（第四种）；10→C（trait 有默认，macOS 覆写——严格说偏 B，去核对 [gpui/src/platform.rs](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/platform.rs) 中 `show_system_notification` 是否有默认体，把它归入 B 或 C 并说明理由）。

完成后你应该得到一个元结论：**门控方式的选择遵循「调用方是否需要在非 macOS 上合法调用」与「能力是否可能泛化到其他平台」两个问题**——这正是设计跨平台 API 时最有迁移价值的判断力。

## 6. 本讲小结

- `MacPlatform` 的骨架是控制流倒置的桥接：`#[ctor]` 在 main 前注册 `GPUIApplication`/`GPUIApplicationDelegate` 两个动态 ObjC 类，用 ivar 反持 Rust 指针，约 20 个 `extern "C"` 委托方法把 AppKit 事件翻译回 Rust 闭包槽位。
- 启动回调的执行时机是 `applicationDidFinishLaunching:`（先设激活策略、注册键盘布局/热状态/唤醒观察者，再取出 `run` 存下的回调）；`quit` 与热状态回调被刻意推迟到 GCD 主队列下一轮，规避「同步 on_close 回调重入锁」的死锁。
- macOS 主线程约束是硬约束：标准 Rust 测试线程上碰 AppKit 会 SIGABRT，所以 gpui_platform 的可视化测试全部 `#[ignore]` 并要求 `--ignored --test-threads=1`。
- `events.rs` 是无状态翻译层：大 match 完成 `NSEventType → PlatformInput`，一行 `window_height - y` 完成坐标系翻转，`command → platform` 完成修饰键归一；键盘经 `charactersIgnoringModifiers` + Carbon `UCKeyTranslate` 双路得出布局无关的 `key` 与布局相关的 `key_char`。
- `pasteboard.rs` 用「zed-text-hash + zed-metadata」侧车类型保护元数据一致性（哈希不匹配即作废），并封装了系统级查找板 `NSPasteboardNameFind`——后者在契约上是 `#[cfg(target_os = "macos")]` 编译期门控。
- `display_link.rs` 用「每屏一条永生 CVDisplayLink + 窗口订阅者」结构规避 `CVDisplayLinkStop` 的不安全拆除：io 线程只做 `merge_data`，主队列上的 `step` 触发 gpui 的 `request_frame_callback`；启停由遮挡、换屏、窗口销毁三类事件驱动。
- `window_appearance.rs` 维护 NSAppearance 名字与 `WindowAppearance` 的四元映射；`set_window_appearance` 以 nil 清除覆盖、`effectiveAppearance` 保证读写自洽，契约上采用「默认 no-op」而非 cfg 门控。

## 7. 下一步学习建议

- **u6-l2（WindowsPlatform 与 DirectX 渲染栈）**：把本讲的「一个 Mutex + ObjC 委托」结构与 Windows 的「消息专用窗口 + PostMessageW 唤醒」对照，你会看到同一套 Platform 契约在两种原生消息机制上的落地差异。
- **u6-l3（菜单、系统通知与跳转列表）**：本讲 4.1 只看了菜单的骨架（tag 索引 `menu_actions` 的机制在 [handle_menu_item](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/platform.rs#L1441-L1456)），`MenuItem` 数据模型、key equivalent 绑定与三方通知实现留给下一讲展开。
- 若想先横向巩固，建议重读 u2-l1 的八大方法分组，用本讲综合实践的「三种（+feature）门控方式」重新分类那 69 个方法，检验你对契约设计的理解是否升级。

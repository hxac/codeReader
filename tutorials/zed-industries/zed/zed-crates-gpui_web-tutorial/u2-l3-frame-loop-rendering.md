# 渲染循环：requestAnimationFrame 帧驱动与 WgpuRenderer

## 1. 本讲目标

学完本讲，你应该能够：

1. 独立追踪一次完整绘制链路：`requestAnimationFrame` 回调 → `request_frame` 回调 → GPUI `draw`/`present` → `WebWindow::draw` → `WgpuRenderer::draw` → `frame.present()`。
2. 解释 `raf_id` 为什么必须在回调体一开始就置空，以及那句「防丢帧」注释背后的设计意图。
3. 说明 `wake_frame_loop` 的防重入守卫如何保证同一时刻最多只有一个挂起的 rAF 请求，以及整个帧循环为什么是「需求驱动」而非空转的。
4. 理解 `frame_waker` 为什么返回弱引用（`Rc::downgrade`）而不是强引用。
5. 理解 `pending_physical_size` 为什么由 ResizeObserver 写入、却延迟到 `draw()` 才应用。
6. 知道 `sprite_atlas()`、`gpu_specs()`、`is_subpixel_rendering_supported()` 是如何把 `WgpuRenderer` 的内部能力暴露给 GPUI 框架层的。

## 2. 前置知识

### 2.1 requestAnimationFrame 与垂直同步

浏览器不允许页面「随时画画」。原生窗口系统里，应用通常自己跑一个消息循环；而在浏览器里，绘制时机由浏览器统一调度：你调用 `window.requestAnimationFrame(callback)` 申请一帧，浏览器会在**下一次垂直同步（vsync）信号到来、页面重绘之前**执行你的回调。回调执行完，浏览器把你在回调里产生的绘制结果合成到屏幕上。

这意味着两件事：

- 帧周期由显示器刷新率决定。60Hz 显示器上帧预算约为 \( T = 1/60\,\text{Hz} \approx 16.67\,\text{ms} \)，120Hz 上约为 \( 8.33\,\text{ms} \)。
- rAF 回调**不会自动重复**。你想画下一帧，必须在回调里（或之后）再次调用 `requestAnimationFrame`。谁负责「再排一帧」，就是本讲要回答的核心问题。

### 2.2 wasm-bindgen 的 Closure（回顾）

在 u2-l2 我们见过：Rust 闭包要通过 `wasm_bindgen::Closure` 包装才能交给 JavaScript 调用。`Closure` 必须一直存活，一旦被 Drop，它对应的 JS 函数再被调用就会抛出 "closure invoked after being dropped"。本讲的 rAF 回调就是一个 `Closure<dyn FnMut()>`，它的存活与清理（`Drop` 时的 `cancel_animation_frame`）是理解本讲代码的一半；另一半（`Rc` 循环与重入安全）会在 u3-l2 专题展开。

### 2.3 GPUI 的失效（invalidation）模型（回顾）

GPUI 不是「每帧重画所有内容」的裸循环，而是失效驱动：视图状态变化时调用 `cx.notify()`，把窗口标脏（dirty）；只有脏窗口才需要真正重绘。框架内部由一个 `WindowInvalidator` 管理脏标记。本讲会看到：**平台层（gpui_web）负责「什么时候可以画」，框架层（gpui）负责「要不要画、画什么」**，两者通过 `on_request_frame` 注册的回调和 `frame_waker` 返回的唤醒器衔接。

### 2.4 Cell / RefCell 内部可变性（回顾）

rAF 回调是浏览器从 JS 世界调进 Rust 的，没有 `&mut self` 可拿。所以 `WebWindowInner` 里与帧循环相关的三个字段全部使用内部可变性：`raf_id: Cell<Option<i32>>`（简单值用 `Cell`）、`raf_function: RefCell<Option<js_sys::Function>>`（`Function` 不是 `Copy`，用 `RefCell` 换出换入）、`pending_physical_size: Cell<Option<(u32, u32)>>`。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| `crates/gpui_web/src/window.rs` | 浏览器窗口实现（主角） | `create_raf_closure`、`wake_frame_loop`、`frame_waker`、`on_request_frame`、`draw`、`completed_frame`、`sprite_atlas`/`gpu_specs` |
| `crates/gpui/src/window.rs` | GPUI 框架的窗口实现（接力方） | `on_request_frame` 注册的回调体、`WindowInvalidator::set_dirty`、`Window::draw`/`present`、`bounds_changed` |
| `crates/gpui/src/platform.rs` | 平台抽象契约（回顾） | `RequestFrameOptions` 结构体、`frame_waker`/`on_request_frame` trait 方法签名 |
| `crates/gpui_wgpu/src/wgpu_renderer.rs` | wgpu 渲染器（终点） | `draw`、`update_drawable_size`、`sprite_atlas`/`gpu_specs` |
| `crates/gpui_web/examples/hello_web/main.rs` | 可运行示例（实践载体） | 加日志、做实验的观察对象 |

> 说明：后文引用 gpui / gpui_wgpu 的源码时，永久链接直接指向各自 crate 的路径。

## 4. 核心概念与源码讲解

本讲的最小模块是「window.rs 的帧循环与绘制」，我们把它拆成四个递进的小节：帧循环的发动机（rAF 闭包）、需求驱动的调度（wake_frame_loop 与 frame_waker）、绘制入口（draw 与延迟应用的尺寸）、真正的像素提交（WgpuRenderer）。

### 4.1 rAF 闭包：帧循环的发动机

#### 4.1.1 概念说明

浏览器上没有人替你「循环」。GPUI 框架期望平台在「可以画一帧」的时候调用它注册的 `request_frame` 回调（通过 `PlatformWindow::on_request_frame` 注册，契约定义在 [platform.rs:L849-L852](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/platform.rs#L849-L852)）。在桌面平台，这个时机来自操作系统的事件循环；在浏览器上，唯一的正统时机就是 rAF 回调。

所以 gpui_web 的做法非常直白：**造一个 Rust 闭包，把它包装成 JS 函数，每次想画一帧就把它交给 `requestAnimationFrame`**。这个闭包在 `WebWindow::new` 里只创建一次，此后被反复复用。

这里有个容易混淆的双份持有：

- `WebWindow::_raf_closure: Closure<dyn FnMut()>` —— Rust 侧的所有权句柄，作用是**保活**（下划线前缀表示「仅为持有而持有」）。
- `WebWindowInner.raf_function: RefCell<Option<js_sys::Function>>` —— JS 侧的函数引用克隆，作用是**反复排程**：`request_animation_frame` 需要一个 JS 函数，每次排帧都从这里取。

#### 4.1.2 核心流程

rAF 闭包的一次完整生命周期：

1. `WebWindow::new` 末尾调用 `create_raf_closure()` 创建闭包，并把它的 JS 函数克隆存进 `raf_function`。
2. 紧接着调用一次 `wake_frame_loop()`，排下整个窗口生命周期的**第一帧**。
3. 浏览器在下次 vsync 前执行闭包：
   - 先把 `raf_id` 置为 `None`（当前这次请求已经兑现，不再挂起）；
   - 再通过 `with_callback` 取出 `callbacks.request_frame` 并调用，把控制权交给 GPUI 框架。
4. GPUI 画完后，如果还有剩余需求，会经由 `frame_waker` 再次调 `wake_frame_loop` 排下一帧（见 4.2）；如果窗口干净了，就不再排——循环自然停摆，直到下一次 `cx.notify()`。
5. 窗口销毁时，`Drop for WebWindow` 先 `cancel_animation_frame` 取消仍挂起的请求，再放掉闭包。

#### 4.1.3 源码精读

创建闭包的函数本体：

[window.rs:L348-L372](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L348-L372) —— `create_raf_closure`：克隆一份 `Rc<WebWindowInner>` 进闭包（保证回调执行时内核还活着），创建 `Closure<dyn FnMut()>`，再把闭包转成的 JS 函数克隆存入 `raf_function`，最后返回闭包给调用方保活。

关键的闭包体只有十几行：

[window.rs:L350-L365](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L350-L365) —— rAF 回调体：第一句就是 `this.raf_id.set(None)`，然后以 `RequestFrameOptions { require_presentation: false, force_render: false }` 调用 GPUI 的 `request_frame` 回调。

注意 `raf_id.set(None)` 之前的那段注释，它是本讲最值得逐字读的注释：

> The request that fired is no longer pending; clear it before running the frame so wakeups issued while the frame executes (e.g. views invalidated during draw) schedule the next request instead of being swallowed.

翻译过来：**先置空再执行帧**。因为 GPUI 在绘制过程中可能再次弄脏视图（比如动画推进时 `cx.notify()`），这些「执行中产生的唤醒」会调用 `wake_frame_loop`；如果此时 `raf_id` 还留着旧值，守卫会误以为「已经排过下一帧了」而直接返回，这次唤醒就被吞掉（swallowed）——下一帧丢失，画面卡一拍。置空放在回调体第一句，保证执行期间任何唤醒都能成功排程新请求。

回调传给 GPUI 的选项字段含义见 [platform.rs:L737-L744](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/platform.rs#L737-L744) —— `RequestFrameOptions` 只有两个字段：`require_presentation`（是否必须呈现）与 `force_render`（是否强制绕过视图缓存重绘）。web 平台两个都传 `false`，把决策权完全交给框架侧。

启动引导与字段定义：

[window.rs:L200-L201](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L200-L201) —— `WebWindow::new` 的末尾：创建闭包后立即 `wake_frame_loop()`，排下第一帧。窗口刚诞生时 GPUI 还没注册 `request_frame` 回调（注册发生在框架侧窗口构造流程中），所以这第一帧回调里 `with_callback` 会发现槽位为空而静默跳过，但 `raf_id` 已被置空，后续唤醒可以正常排程。

[window.rs:L60-L62](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L60-L62) —— 三个帧循环相关字段：`pending_physical_size`、`raf_id`、`raf_function` 的定义。

[window.rs:L70](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L70) —— `_raf_closure` 字段：外壳 `WebWindow` 持有闭包保活，防止 JS 还持有函数时 Rust 侧已被释放。

销毁时的清理顺序：

[window.rs:L505-L519](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L505-L519) —— `Drop for WebWindow` 的前半部分：先 `cancel_animation_frame(raf_id)` 取消挂起的请求，再清空 `raf_function`。注释点明了顺序原因——必须赶在 `_raf_closure`（结构体字段，按声明顺序在这之后释放）被释放之前取消，否则迟到的回调会触发 "closure invoked after being dropped"；而清空 `raf_function` 之后，任何侥幸存活的 `frame_waker` 再调 `wake_frame_loop` 也只能空转（见 4.2.3）。

#### 4.1.4 代码实践：给帧循环装一个「转速表」

实践目标：在 rAF 回调里统计每秒实际执行的回调次数，验证「帧循环是需求驱动」这一论断。

操作步骤（修改的是你本地 checkout 的 crate 源码，不影响仓库；由于 hello_web → gpui_platform → gpui_web 全部是路径依赖，重新 `trunk serve` 即可生效）：

1. 进入 `crates/gpui_web/`，在 `src/window.rs` 顶部（`impl WebWindowInner` 之前）加入（示例代码）：

   ```rust
   // 示例代码：帧计数器（仅本地实验用）
   thread_local! {
       static FRAME_COUNT: std::cell::Cell<u64> = const { std::cell::Cell::new(0) };
       static LAST_REPORT_MS: std::cell::Cell<f64> = const { std::cell::Cell::new(0.0) };
   }
   ```

2. 在 `create_raf_closure` 的闭包体内、`this.raf_id.set(None)` 之后插入（示例代码）：

   ```rust
   FRAME_COUNT.with(|counter| counter.set(counter.get() + 1));
   let now_ms = js_sys::Date::now();
   let elapsed = now_ms - LAST_REPORT_MS.with(|t| t.get());
   if elapsed >= 1000.0 {
       LAST_REPORT_MS.with(|t| t.set(now_ms));
       let frames = FRAME_COUNT.with(|c| c.replace(0));
       log::info!("frame loop: {frames} callbacks in {elapsed:.0} ms");
   }
   ```

3. 在 `examples/hello_web/` 下运行 `trunk serve`，打开浏览器 DevTools 的 Console（`log` 宏的输出由 `web_init` 安装的 ConsoleLogger 转发到控制台，见 u1-l2）。

需要观察的现象与预期结果（待本地验证）：

- 页面静止且无任务运行时，日志会逐渐稀疏乃至停止——空闲窗口不排帧，CPU 占用趋近于零。
- 在画布上移动鼠标、点击按钮，或点一次 Count Primes（后台分块完成时每个 chunk 都会 `cx.notify()`），日志恢复打印，频率接近显示器刷新率（60Hz 屏约 60 次/秒，120Hz 屏约 120 次/秒）。
- 若观察值明显偏离刷新率，回到 4.2 检查唤醒链路哪里被跳过了。

#### 4.1.5 小练习与答案

**练习 1**：为什么需要同时持有 `_raf_closure`（Rust 侧）和 `raf_function`（JS 侧函数引用）两份数据？只留一份行不行？

答案：`Closure` 是 Rust 侧的所有权对象，Drop 之后其 JS 函数调用即抛错，所以必须有人长期持有它（`_raf_closure` 的下划线前缀就是「仅为保活」）。但 `request_animation_frame` 的参数必须是 JS 函数值，且每次排帧都要传一次，所以额外克隆一个 `js_sys::Function` 存在 `raf_function` 里供反复使用。只留 `_raf_closure` 的话没有可供排程的 JS 函数句柄；只留 JS 函数而 Drop 了 `Closure`，则调用会抛 "closure invoked after being dropped"。

**练习 2**：把 `this.raf_id.set(None)` 挪到 `with_callback(...)` 调用之后，会发生什么？

答案：GPUI 在 `request_frame` 回调执行期间产生的唤醒（例如绘制中视图再次失效）调用 `wake_frame_loop` 时，守卫 `if self.raf_id.get().is_some() { return; }` 会看到残留的旧 id 而直接返回，这次唤醒被吞掉。其后果取决于框架侧末尾的再排逻辑是否兜底（见 4.2.3 的 `wake_platform`）：若帧结束时窗口已不脏，本帧内的那次失效就没人响应，画面停在一拍之前的旧内容上，直到下一次外部唤醒。这正是源码注释里 "instead of being swallowed" 防御的场景。

**练习 3**：`WebWindow::new` 里第一次 `wake_frame_loop()` 排下的那一帧，`request_frame` 回调槽位还是空的，这一帧岂不是白白浪费了？

答案：不算浪费，也不出错。`with_callback` 发现槽位为 `None` 时直接返回 `None`（take 之后 `?` 提前返回），闭包体安全结束；而 `raf_id` 已在闭包体开头被置空，为后续唤醒扫清了道路。这次「空帧」的真正作用是完成启动引导：它把闭包和浏览器排程机制连通了。真正的首帧绘制由框架侧注册完回调后的第一次唤醒驱动。

### 4.2 wake_frame_loop、frame_waker 与需求驱动循环

#### 4.2.1 概念说明

有了发动机（rAF 闭包），还需要油门：什么时候排下一帧？答案分三层：

1. **`wake_frame_loop`（平台侧油门）**：任何「需要一帧」的代码路径都调用它。它有个防重入守卫：`raf_id` 已有值说明已经排过、还没兑现，直接返回，保证同一时刻最多只有一个挂起的 rAF 请求——既省 CPU，也避免一帧内重复绘制。
2. **`frame_waker`（跨层油门线）**：GPUI 框架的 `WindowInvalidator` 需要在窗口变脏时唤醒平台排帧，但它不该持有平台的强引用，所以 `PlatformWindow::frame_waker()` 返回一个闭包，gpui_web 的实现里用 `Rc::downgrade` 持有 `WebWindowInner` 的弱引用。
3. **框架侧收尾再排（终点油门）**：GPUI 的 `request_frame` 回调在画完一帧后检查「是否还有剩余需求」，有则调用 `invalidator.wake_platform()` 再排一帧；没有就到此为止。

三层合起来构成**需求驱动**的循环：醒着才画，画完即睡。这与「每帧无条件 rAF 空转」的写法（很多网页动画的做法）形成鲜明对比，也是 GPUI 能在浏览器里保持低空闲功耗的原因。

#### 4.2.2 核心流程

一次点击引发的完整绘制链路：

```text
用户点击 "Count Primes"
  └─ GPUI 元素事件回调中 cx.notify()
       └─ WindowInvalidator::set_dirty(true)                  （gpui/src/window.rs L185）
            └─ 由干净变脏 → 取出 platform_waker 并调用
                 └─ frame_waker 闭包：Weak 升级 → wake_frame_loop()
                      └─ raf_id 为空 → request_animation_frame(raf_function)
浏览器在下次 vsync 前执行 rAF 闭包                              （gpui_web L350）
  ├─ raf_id.set(None)                                          （L355）
  └─ 调用 GPUI 注册的 request_frame 回调                        （L356 → gpui L1517）
       ├─ 防重入：draw_in_progress 则推迟并记下 force_render     （gpui L1541）
       ├─ 节流判断（非激活窗口 ~30fps、热节流 ~60fps 等）          （gpui L1559）
       ├─ invalidator.is_dirty() → Window::draw(cx)（重建元素树、生成 Scene）（gpui L1614, L2836）
       ├─ Window::present() → platform_window.draw(&scene)      （gpui L2998-L2999）
       │    └─ WebWindow::draw：应用 pending_physical_size → renderer.draw(scene)（本讲 4.3/4.4）
       │         └─ WgpuRenderer::draw … frame.present()        （gpui_wgpu L1265, L1401）
       ├─ Window::complete_frame()（web 上是空操作）              （gpui_web L793）
       └─ 仍 dirty 或有 next_frame_callbacks → wake_platform() 再排下一帧（gpui L1646）
```

#### 4.2.3 源码精读

平台侧的油门：

[window.rs:L374-L383](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L374-L383) —— `wake_frame_loop`：守卫 + 从 `raf_function` 取出 JS 函数 + `request_animation_frame`。注意 `.ok()`：请求失败时 `raf_id` 被设为 `None`，下次唤醒可以重试。若 `raf_function` 已在 Drop 中被清空，这里静默空转——这正是销毁路径依赖的行为（见 4.1.3 的 Drop 引用）。

[window.rs:L722-L733](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L722-L733) —— `frame_waker`：先 `Rc::downgrade`，唤醒时 `upgrade` 成功才调 `wake_frame_loop`。内联注释解释了为什么必须弱引用：唤醒器会被存进窗口的 invalidator，而 invalidator 又被 `request_frame` 回调克隆捕获——如果这里放强引用 `Rc<WebWindowInner>`，就形成「inner → callbacks → invalidator → waker → inner」的引用环，窗口关闭后永远无法释放（内存泄漏）。

[window.rs:L735-L737](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L735-L737) —— `on_request_frame`：把框架侧回调存进 `callbacks.request_frame` 槽位。此后每次 rAF 触发就经由 `with_callback`（take/call/restore，见 [window.rs:L337-L346](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L337-L346)，重入安全的细节留给 u3-l2）调用它。

框架侧的接力与收尾（gpui crate）：

[window.rs:L1517-L1525](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L1517-L1525) —— gpui 在构造窗口时把一个巨型闭包注册为 `request_frame` 回调，闭包捕获了 invalidator、活跃状态、next-frame 回调队列等的克隆。

[window.rs:L1541-L1545](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L1541-L1545) —— 防重入守卫：如果本线程已有绘制在栈上（`draw_in_progress()`），直接跳过并把 `force_render` 记到 `deferred_force_render`，等下一帧补上。这是为 Windows 消息泵设计的，web 上正常不会走到，但防御是平台无关的。

[window.rs:L1614-L1633](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L1614-L1633) —— 真正干活的地方：窗口脏（或强制渲染）时执行 `window.draw(cx)`（重建元素树、生成 `Scene`，入口在 [window.rs:L2836](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L2836)），随后 `window.present()`。`present` 的实现只有一行核心语句，见 [window.rs:L2998-L3007](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L2998-L3007) —— 把 `rendered_frame.scene` 传给 `platform_window.draw(...)`，也就是回到 gpui_web 的 `WebWindow::draw`（4.3）。

[window.rs:L1635-L1648](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L1635-L1648) —— 帧末收尾：调用 `complete_frame()`（web 上空操作），然后**条件性再排**——窗口仍脏或有待处理的 next-frame 回调时调 `invalidator.wake_platform()`。这是「需求驱动」的最后一环：干净窗口不再排帧。

[window.rs:L1651](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L1651) —— `invalidator.set_platform_waker(platform_window.frame_waker())`：把平台唤醒器交给失效器，接上「notify → 排帧」的线路。

失效器侧的触发点：

[window.rs:L185-L198](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L185-L198) —— `set_dirty`：注意只在「由干净变脏」（`became_dirty`）时才调用唤醒器——连续多次 `notify()` 只会唤醒一次，与 `wake_frame_loop` 的守卫形成两级去重。

[window.rs:L214-L219](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L214-L219) —— `wake_platform`：帧末收尾再排调用的就是这个方法，无条件调用一次平台唤醒器。

#### 4.2.4 代码实践：破坏性实验——拆掉防重入守卫

实践目标：通过「故意改坏」来验证 `wake_frame_loop` 守卫的必要性。

操作步骤：

1. 保留 4.1.4 的帧计数日志。
2. 把 [window.rs:L374-L377](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L374-L377) 中的守卫删掉或改为 `if false { return; }`（示例代码：仅在本地实验，观察完务必还原）。
3. 重新 `trunk serve`，进行交互（点击按钮、点 Count Primes），观察 Console 中每秒回调数的变化；再关闭窗口（示例里没有关闭入口，可刷新页面观察报错，或临时在 `start_search` 里加一个调用系统关闭的按钮——若不便，跳过关闭观察）。

需要观察的现象与预期结果（待本地验证）：

- 每秒回调数会**超过**显示器刷新率，并且在一帧内被多次唤醒时持续攀升：一帧内两次唤醒会排下两个 rAF 请求，下一 vsync 两个回调都执行，各自又可能引发新的唤醒——请求堆积。
- `raf_id` 只能记录最后一个请求的 id，`Drop` 里的 `cancel_animation_frame` 只能取消其中一个，剩余挂起请求在闭包释放后仍会触发，Console 里可能出现 "closure invoked after being dropped"——这正是 4.1.3 中 Drop 注释所防御的错误场景。
- 顺带体会：即便拆了守卫，**空闲**窗口依然不排帧（没有唤醒源），说明「需求驱动」由唤醒源决定，守卫解决的是「一帧内多次唤醒」的去重问题。

#### 4.2.5 小练习与答案

**练习 1**：`frame_waker` 里的弱引用具体断开了哪一个环？

答案：GPUI 侧的 `WindowInvalidator` 会保存这个唤醒器（`set_platform_waker`），而注册给平台的 `request_frame` 回调又克隆持有 invalidator、invalidator 持有唤醒器。若唤醒器强引用 `Rc<WebWindowInner>`，则链条 `WebWindowInner → callbacks.request_frame → invalidator → platform_waker → WebWindowInner` 闭合成环，`Rc` 引用计数永不归零，窗口及其全部 DOM 监听器泄漏。`Rc::downgrade` 断开最后一跳；窗口已死时 `upgrade` 返回 `None`，唤醒自然失效。

**练习 2**：`set_dirty` 为什么只在「由干净变脏」时唤醒，而 `wake_platform` 是无条件唤醒？

答案：`set_dirty` 面对的是高频失效（一帧内可能 notify 多次、首个脏标记之后的都是冗余），只在这时才需要排程，否则会对着已经挂起的 rAF 反复空转；`wake_platform` 只在帧末收尾这种低频位置被调用，且调用前已确认「有剩余需求」，无条件唤醒是安全的。两者的去重最终都由 `wake_frame_loop` 的守卫兜底。

**练习 3**：启动后的第一帧是怎么排上的？请按顺序列出调用者。

答案：`WebWindow::new`（[window.rs:L200-L201](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L200-L201)）里显式调用 `wake_frame_loop()` 排下引导帧；此后 gpui 侧窗口构造完成、注册 `request_frame` 回调并 `set_platform_waker`，视图初次标脏时经 `set_dirty → platform_waker → wake_frame_loop` 排下真正的首帧绘制。

### 4.3 draw()：pending_physical_size 的延迟应用

#### 4.3.1 概念说明

`WebWindow::draw(scene)` 是 `PlatformWindow` 契约中的呈现入口：GPUI 把渲染好的 `Scene`（图元集合）交给它，由它驱动 GPU 落到屏幕。但 gpui_web 的 `draw` 还兼任一个隐蔽职责：**应用延迟已久的画布尺寸**。

u2-l4 会详细讲 ResizeObserver；这里只需要知道：浏览器里尺寸变化是异步回调通知的，ResizeObserver 闭包在测量到新的物理尺寸后，并不直接改 canvas，而是把它写进 `pending_physical_size` 这个「信箱」，然后通知 GPUI「窗口大小变了」。GPUI 因此标脏、排帧；等到下一帧 `draw` 执行时，才从信箱里取出尺寸，一次性应用。

为什么绕这一圈？三个理由：

1. **合并中间值**：拖拽调整窗口大小时，浏览器会在一瞬间连续触发多次 resize 回调，中间值无人关心。`Cell::set` 后写覆盖先写，信箱里永远只有最新值，`draw` 只应用一次最终尺寸。
2. **避免「清空了却不画」的闪白**：给 `canvas.width/height` 赋值会**清空整个画布位图**。如果在 resize 回调里立刻改尺寸，此刻并没有新内容可画，用户会看到一帧空白；把赋值放进 `draw` 的开头，紧接着就是本帧的渲染，改尺寸与画新内容近乎原子地发生。
3. **保证 surface 重配置先于使用**：物理尺寸变化还牵动 wgpu surface 的重配置（改 `surface_config`、销毁重建中间纹理等，见 4.4.3 的 `update_drawable_size`）。这些操作必须发生在「以新尺寸渲染」之前，而 `draw` 开头正是唯一可靠的位置。

还有一个构造期的呼应：`WebWindow::new` 创建 `WgpuRenderer` 时尺寸填的是 0×0（见 [window.rs:L131-L139](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L131-L139) 中 `WgpuSurfaceConfig` 的 `Size` 全零）——因为此刻还不知道画布多大。真正的初始尺寸同样要走「ResizeObserver 写信箱 → 首帧 draw 应用」这条路。

#### 4.3.2 核心流程

```text
ResizeObserver 回调（异步）
  ├─ 测量新物理尺寸（钳制到 max_texture_dimension）
  ├─ pending_physical_size.set(Some((w, h)))     ← 投信箱
  ├─ 更新 state.bounds / scale_factor
  └─ on_resize 回调 → GPUI bounds_changed → refresh → 窗口标脏
        └─ （走 4.2 的唤醒链路）→ rAF → request_frame → Window::draw → present
              └─ WebWindow::draw(scene)
                   ├─ pending_physical_size.take()         ← 取信箱（取出后信箱清空）
                   ├─ canvas.set_width/set_height          ← 改位图尺寸（会清空画布）
                   ├─ renderer.update_drawable_size(...)   ← 重配置 surface
                   └─ renderer.draw(scene)                 ← 立刻画上新内容
```

#### 4.3.3 源码精读

写入侧（ResizeObserver 闭包的尾部）：

[window.rs:L303-L305](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L303-L305) —— 把钳制后的物理尺寸写进 `pending_physical_size`。注意这里只是「投递」，没有碰 canvas。

消费侧（本讲主角之一）：

[window.rs:L775-L791](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L775-L791) —— `WebWindow::draw` 全文。前半段 `take()` 出待应用尺寸，与 canvas 当前 `width()/height()` 不同才赋值（避免无谓清空），然后借用状态调用 `renderer.update_drawable_size`；显式 `drop(state)` 释放借用后，再调用 `renderer.draw(scene)`。两个细节：`take()` 保证一次应用后信箱即空，同一尺寸不会重复应用；先 `drop(state)` 再画，是因为两处都要 `borrow_mut` 同一个 `RefCell`，顺序写错会在运行时 panic。

GPUI 侧如何因 resize 而标脏：

[window.rs:L2414-L2420](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L2414-L2420) —— `bounds_changed`：`on_resize` 注册的回调最终走到这里，重新读取 scale/内容尺寸并调用 `self.refresh()`（内部会标脏），把「尺寸变了」翻译成「需要重绘」，从而接上 4.2 的排帧链路。

构造期 0×0 呼应（再看一眼）：

[window.rs:L128-L139](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L128-L139) —— `WebWindow::new` 里读取 `device_pixel_ratio`、`max_texture_dimension_2d`，随后以 0×0 的 `WgpuSurfaceConfig` 创建 `WgpuRenderer`。初始尺寸完全依赖首次 resize 信箱投递。

#### 4.3.4 代码实践：观察尺寸应用的合并与时机

实践目标：亲眼看到「多次 resize 回调、一次尺寸应用」的合并效果，并验证应用发生在 draw 开头。

操作步骤（源码阅读 + 加日志型实践）：

1. 在 [window.rs:L303-L305](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L303-L305) 之后加一行 `log::info!("resize pending: {physical_width}x{physical_height}");`（示例代码）。
2. 在 [window.rs:L776](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L776) 的 `if let Some((width, height)) = ...` 分支内加一行 `log::info!("draw applies size: {width}x{height}");`（示例代码）。
3. 重新运行示例，分别做三种操作：缓慢拖拽浏览器窗口边缘、快速连续拖拽、用 Ctrl+加/减 改变页面缩放（会改变 DPR，走 u2-l4 的另一条路径）。

需要观察的现象与预期结果（待本地验证）：

- 快速连续拖拽时，`resize pending` 日志条数明显多于 `draw applies size` 日志条数——多个中间尺寸被合并，只有最新值被应用。
- 每条 `draw applies size` 之后紧跟着本帧的绘制（可结合 4.1.4 的帧日志对时间线），画布没有出现可感知的空白闪烁。
- 若把步骤 2 的日志挪到 `renderer.draw(scene)` 之后，观察日志顺序变化，体会「应用尺寸必须在绘制之前」的顺序约束。

#### 4.3.5 小练习与答案

**练习 1**：`pending_physical_size` 为什么选 `Cell<Option<(u32, u32)>>`，而不是 `RefCell<Vec<(u32, u32)>>` 之类的队列？

答案：因为只有「最新值」有意义，历史值全都是过期信息。`Cell::set` 的覆盖语义天然实现了「保留最新、丢弃其余」的合并策略，且 `Option` 的空态还能表示「没有待应用尺寸」；队列反而需要额外逻辑去丢弃旧值，还要为 `Vec` 引入 `RefCell` 借用管理。

**练习 2**：如果在 ResizeObserver 回调里直接 `canvas.set_width(...)`，最直接的可见后果是什么？

答案：给 canvas 宽高赋值会同步清空位图，而 resize 回调发生的时刻并没有新内容可画——从清空到下一次 draw 之间用户会看到空白（快速拖拽时表现为持续闪白）。此外连续回调会对同一 canvas 反复清空、对 surface 反复重配置，浪费且可能引入撕裂。延迟到 `draw` 开头应用，清空与重绘贴在一起发生。

**练习 3**：`draw` 里为什么必须先 `drop(state)` 再调 `renderer.draw(scene)`？

答案：`state` 是 `RefCell<WebWindowMutableState>` 的 `borrow_mut()` 守卫，而最后的 `self.inner.state.borrow_mut().renderer.draw(scene)` 也要可变借用同一个 `RefCell`。不显式释放前一个守卫就会触发 `BorrowMutError` panic。显式 `drop` 把「尺寸应用」与「绘制」两个临界区隔开，也顺便让每段的持锁时间最短。

### 4.4 WgpuRenderer：真正的像素提交与能力暴露

#### 4.4.1 概念说明

`WebWindow::draw` 的最后一行把 `Scene` 交给了 `WgpuRenderer`——它来自兄弟 crate gpui_wgpu（u2-l1 提过：渲染与文本设施由 gpui_wgpu 提供）。框架层通过三个 `PlatformWindow` 方法窥见渲染器内部：

- `sprite_atlas()`：返回精灵图集（`PlatformAtlas`）。GPUI 把文字光栅化结果、图标等缓存在图集里跨帧复用，框架层需要直接往里塞图块。
- `gpu_specs()`：返回 GPU 规格（`GpuSpecs`），用于诊断/展示。
- `is_subpixel_rendering_supported()`：是否支持亚像素抗锯齿所需的 dual source blending，决定文本渲染路线。

另外两个呈现相关方法也在这段源码里：`completed_frame()` 在 web 上是**空操作**——wgpu 的 `frame.present()` 已经在 `WgpuRenderer::draw` 内部完成了提交，浏览器合成器会自动上屏；这与 Wayland 等需要显式 `surface.commit()` 的平台不同。

#### 4.4.2 核心流程

`WgpuRenderer::draw(scene)` 的主干（省略错误处理细节）：

```text
1. device_lost 检查        → 上下文丢失：记日志、标记 surface 未配置，直接返回（web 专属分支）
2. surface_configured 检查  → 未配置：直接返回 false
3. last_error 检查          → 有上一帧的错误：计数、清图集重试，连续失败超限则 panic
4. atlas.before_frame()     → 图集帧前整理
5. surface.get_current_texture() → 五种结果分别处理：
     Success(frame)         → 继续
     Suboptimal(frame)      → 丢弃本帧、重新 configure，返回
     Lost / Outdated        → 重新 configure，返回
     Timeout / Occluded     → 直接返回
     Validation             → 记错误，返回
6. write_buffer × N         → 写入全局/gamma 等 uniform
7. record_frame(scene)      → 编码并提交全部渲染 pass
8. frame.present()          → 提交到屏幕（web 上由浏览器合成器呈现）
```

#### 4.4.3 源码精读

绘制主入口与 web 专属的上下文丢失处理：

[wgpu_renderer.rs:L1265-L1275](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_wgpu/src/wgpu_renderer.rs#L1265-L1275) —— `WgpuRenderer::draw` 的开头：`#[cfg(target_family = "wasm")]` 的 `device_lost()` 分支。浏览器可能在资源压力下回收 WebGPU/WebGL 上下文；此时打出那条著名日志（"Browser graphics context was lost; rendering has stopped. Reload the page to recover."），把 `surface_configured` 置 false 并返回。此后每帧都会在第 2 步静默返回，所以这条日志只会出现一次，页面定格在最后一帧，直到用户刷新。

[wgpu_renderer.rs:L1281-L1283](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_wgpu/src/wgpu_renderer.rs#L1281-L1283) —— 未配置 surface 的提前返回：从「未配置」状态获取纹理在某些驱动上会无限阻塞，必须先挡掉。

[wgpu_renderer.rs:L1311-L1339](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_wgpu/src/wgpu_renderer.rs#L1311-L1339) —— `get_current_texture` 的五路分发，每种失败都有针对性处理（Suboptimal/Lost/Outdated 都走「重新 configure，下帧再战」）。

[wgpu_renderer.rs:L1395-L1402](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_wgpu/src/wgpu_renderer.rs#L1395-L1402) —— 收尾：`record_frame` 编码提交全部绘制指令，失败则提交空队列清错并返回；成功后 `frame.present()`。这一行就是「像素真正上屏」的时刻点。

resize 时 surface 的重配置（4.3 的下游）：

[wgpu_renderer.rs:L1122-L1160](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_wgpu/src/wgpu_renderer.rs#L1122-L1160) —— `update_drawable_size`：尺寸再次钳制到 `max_texture_size` 并 `max(1)` 兜底；与当前配置不同才更新 `surface_config`，随后 poll 设备等待在途 GPU 任务、销毁旧的中间纹理（path 纹理、MSAA 纹理）再重建，避免显存尖峰。这一串昂贵操作正是 4.3 把应用时机推迟并合并到 draw 开头的理由。

gpui_web 侧的能力暴露与呈现空操作：

[window.rs:L793-L795](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L793-L795) —— `completed_frame`：注释说明 web 上呈现由 wgpu surface present 自动完成，因此是空操作。

[window.rs:L797-L799](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L797-L799) —— `sprite_atlas`：克隆 renderer 内图集的 `Arc` 返回，对应 [wgpu_renderer.rs:L1244-L1246](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_wgpu/src/wgpu_renderer.rs#L1244-L1246)。

[window.rs:L801-L807](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L801-L807) 与 [window.rs:L809-L811](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L809-L811) —— `is_subpixel_rendering_supported` 与 `gpu_specs`：一行转发给 renderer（[wgpu_renderer.rs:L1248-L1253](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_wgpu/src/wgpu_renderer.rs#L1248-L1253)）。平台实现只做「门面」，能力真相在渲染器里。

#### 4.4.4 代码实践：读一条故障链路

实践目标：不运行代码，纯靠阅读把 `draw` 的失败分支映射到用户可见症状，训练「源码 → 行为」的推断能力。

操作步骤：

1. 通读 [wgpu_renderer.rs:L1265-L1283](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_wgpu/src/wgpu_renderer.rs#L1265-L1283)，为 `device_lost` 与 `!surface_configured` 两个提前返回各写一句「用户看到什么」。
2. 对比 [window.rs:L793-L795](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L793-L795)（web 空操作）与 gpui 中该回调的用途，回答：为什么 web 不需要在这里做事？
3. （可选，待本地验证）用 `?backend=webgl` 与默认 Auto（WebGPU 优先）分别启动 hello_web（方法见 u1-l2），对比 Console 中初始化日志，确认两种后端最终都汇入同一条 `WgpuRenderer::draw` 链路。

预期结果：

- `device_lost`：Console 出现一次 "Browser graphics context was lost..."，画面冻结在最后 rendered frame，交互仍可能有日志但不再有任何绘制；刷新页面是唯一恢复途径。
- `!surface_configured`：静默返回 `false`，无任何日志——它通常是 `device_lost` 之后的「余震」状态，所以日志只在丢失那一刻打一次。
- web 的 `completed_frame` 为空，是因为呈现动作（`frame.present()`）已经在 `WgpuRenderer::draw` 内完成，浏览器合成器负责真正上屏；而诸如 Wayland 的平台需要在帧回调里显式 commit，才有事可做。

#### 4.4.5 小练习与答案

**练习 1**：`frame.present()` 为什么放在函数最末、且所有失败分支都提前 `return false` 而不 present？

答案：present 的语义是「本帧内容已完整编码并提交，可以换到前台」。任何失败分支（上下文丢失、纹理获取失败、record_frame 出错）都意味着这一帧内容不完整或不可用，此时 present 一个空/半成品帧轻则闪烁，重则触发验证错误；返回 `false` 让调用方知道本帧未呈现，下一帧重试。

**练习 2**：`Suboptimal(frame)` 分支拿到了可用的 frame 却先 `drop(frame)` 再重新 configure，为什么？

答案：源码注释写明「Textures must be destroyed before the surface can be reconfigured」——wgpu 要求对 surface 重新配置前销毁其现有纹理。Suboptimal 表示能呈现但已非最优（例如 DPR 变化后格式不匹配），正确动作是丢弃本帧、立刻按新配置重建设备端状态，下一帧恢复正常，宁可丢一帧也不用错误参数画。

**练习 3**：GPUI 框架为什么需要从 `PlatformWindow` 拿到 `sprite_atlas()`？

答案：文字与图标的光栅化结果需要跨帧缓存复用，而光栅化发生在框架层（文本系统），纹理上传与图集管理在渲染器里。`sprite_atlas()` 把图集的 `Arc` 句柄交给框架，框架便能在渲染之前把新图块写入图集、在 Scene 里引用图集坐标，渲染器只负责按 Scene 绘制。这是「框架管内容、渲染器管资源」的分工界面。

## 5. 综合实践

**任务：搭建一个「帧循环观测台」，为一次交互绘制完整时间线。**

把本讲四个模块的实验串起来，在本地 checkout 上完成：

1. **装表（4.1）**：加入每秒帧计数日志，格式建议带时间戳，便于对齐。
2. **打点（4.3）**：加入 `resize pending` 与 `draw applies size` 两处日志。
3. **制造持续需求（应用层，不碰 crate）**：在 `examples/hello_web/main.rs` 的 `HelloWeb` 中新增一个会周期性自我更新的字段并 `cx.notify()`（示例代码思路：`cx.spawn` 一个循环任务，每隔约 16ms 用 `cx.background_executor().timer(Duration::from_millis(16)).await` 后更新一个计数器字段并 `cx.notify()`；该定时器在 wasm 上基于 web-time 时钟，具体行为待本地验证）。观察帧率是否稳定贴住刷新率——这是「持续需求 → 持续排帧」的对照组。
4. **记录时间线**：点击一次 Count Primes，从 Console 日志中整理出如下时间线并标注每一步对应的源码位置：

   ```text
   t0  点击 → cx.notify() → set_dirty → wake_frame_loop → request_animation_frame
   t1  rAF 回调：raf_id 置空 → request_frame 回调进入
   t2  Window::draw（元素树重建）→ Window::present → WebWindow::draw
   t3  （若有）draw applies size → update_drawable_size
   t4  WgpuRenderer::draw → frame.present()
   t5  complete_frame（空操作）→ 帧末检查 → 不再排帧（若无剩余需求）
   ```

5. **破坏性对照（4.2）**：拆掉 `wake_frame_loop` 守卫重复第 4 步，记录差异（回调堆积、可能的 dropped-closure 报错），然后**还原全部改动**。
6. 产出一份简短实验报告：三张日志片段 + 一段结论，说明「需求驱动帧循环」与「尺寸延迟应用」各自解决什么问题。

## 6. 本讲小结

- 浏览器上 GPUI 的帧发动机是一个复用的 rAF 闭包（`create_raf_closure`）：`WebWindow` 持 `Closure` 保活，`WebWindowInner.raf_function` 存 JS 函数句柄供反复排程。
- 闭包体第一句就把 `raf_id` 置空，让「帧执行期间产生的唤醒」能排下新请求而不被守卫吞掉——这是防丢帧的关键顺序。
- 帧循环是**需求驱动**的：`cx.notify() → set_dirty → frame_waker（弱引用）→ wake_frame_loop（防重入守卫）→ rAF`；帧末 GPUI 只在有剩余需求时再排，空闲窗口零帧耗电。
- `frame_waker` 用 `Rc::downgrade` 断开「inner → callbacks → invalidator → waker → inner」的引用环；`Drop` 时先取消挂起 rAF 再清空 `raf_function`，双保险防止 use-after-free 式的闭包调用。
- `pending_physical_size` 由 ResizeObserver 投递、在 `draw()` 开头应用：合并中间尺寸、避免清空画布后的闪白、保证 surface 重配置先于以新尺寸渲染。
- 像素提交的终点在 `WgpuRenderer::draw` 的 `frame.present()`；web 的 `completed_frame` 是空操作；`sprite_atlas`/`gpu_specs`/`is_subpixel_rendering_supported` 把渲染器能力以门面方式暴露给框架。

## 7. 下一步学习建议

下一讲（u2-l4）顺着本讲留下的接口往下走：`pending_physical_size` 的数据从哪来——精读 ResizeObserver 闭包中 `devicePixelContentBoxSize`（物理像素）与 Safari 回退 `contentRect`（CSS 像素）的分支、`watch_dpr_changes` 如何用 `resolution` 媒体查询捕捉缩放变化、以及 `max_texture_dimension` 钳制后重算逻辑尺寸的细节。

之后再进入 u2-l5（DOM 指针事件）与 u2-l6（键盘/IME），看「输入」这一侧如何唤醒本讲的帧循环；对 Closure 生命周期与 `Rc` 循环感兴趣的话，u3-l2 会把本讲只点到为止的 `Drop` 顺序与 `with_callback` 重入保护展开专题分析。建议同步重读 [window.rs:L505-L535](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L505-L535)（Drop）与 gpui 侧的 `WindowInvalidator`（[window.rs:L181-L219](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L181-L219)），把唤醒协议的两侧对齐读一遍。

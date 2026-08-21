# u7-l2 WebWindow 与浏览器事件桥接

## 1. 本讲目标

u7-l1 讲清了 wasm 应用的启动序列与 `WebPlatform` 的两段式构造，但刻意留下了一个悬而未决的问题：`run` 回调里 `cx.open_window(...)` 拿到的那个「窗口」，在浏览器里到底是什么？本讲就回答这个问题。学完本讲，你应该能够：

1. 说清 `WebWindow` 如何用一张 `<canvas>` 加一个隐藏 `<input>` 元素拼出「窗口」语义，以及 `WebWindowLifecycle` 如何把一个标签页限制成单窗口。
2. 完成浏览器设备像素比（devicePixelRatio）与 GPUI 逻辑像素 `Pixels` 之间的换算，并解释 `ResizeObserver` 的「物理优先」与「逻辑优先」两条路径为什么要分开写。
3. 描述 `WebEventListeners` 的注册清单与事件分发流程：一个 `pointerdown` 如何变成 `PlatformInput::MouseDown`，一个 `keydown` 如何同时产出修饰键变化、按键事件与文本插入。
4. 对比 `WebWindow` 与桌面 `PlatformWindow` 的能力取舍：哪些方法真实工作、哪些是 no-op、哪些只能返回固定值；特别是——按 eb354c8d50 之后的源码，`WebWindow` **不覆写** `PlatformWindow::schedule_frame`（走默认空实现），帧驱动完全依靠 `frame_waker` + `requestAnimationFrame` 闭环，呈现则内嵌在 `WgpuRenderer::draw` 末尾的 `frame.present()` 里，与 Wayland 的按需唤醒模型形成鲜明对照。

## 2. 前置知识

- **CSS 像素与设备像素**：浏览器里 `width: 800px` 指的是 CSS 像素（逻辑单位），屏幕上一个发光点叫设备像素（物理单位），两者的比值就是 `window.devicePixelRatio`（下文简称 dpr）。Retina 屏 dpr 通常是 2。GPUI 的 `Pixels` 是逻辑像素（回顾 u3-l2：「物理像素 = 逻辑像素 × scale_factor」），所以浏览器侧的换算正好落在同一条公式上。
- **DOM 事件模型**：浏览器把用户输入派发为 DOM 事件（`keydown`、`pointerdown`、`wheel`……），事件沿着 DOM 树冒泡，最终「落地」在**当前聚焦的元素**（键盘类）或**事件命中的元素**（指针类）上。调用 `event.preventDefault()` 可以阻止浏览器默认行为（如空格滚页、右键菜单），但现代浏览器把 `wheel`/`touch` 监听默认标记为 **passive**——passive 监听里调用 `preventDefault()` 会被忽略，必须用 `{passive: false}` 注册才行。
- **requestAnimationFrame（rAF）**：浏览器提供的帧回调接口，回调在下一次重绘之前触发，通常与显示器刷新率同步（60Hz 屏约每 16.7ms 一次）。关键性质：**只有显式排队了才会触发**，排一次只回调一次。
- **wasm-bindgen 的 `Closure`**：Rust 闭包传给 JS 当回调时，`wasm_bindgen::closure::Closure` 持有它的存活。`Closure` 被 drop 而监听器还挂在 DOM 上时，下一次事件会抛出 "closure invoked after being dropped"——所以 Rust 侧必须把 `Closure` 与「反注册」成对管理，这是本讲 `EventListenerHandle` 存在的理由。
- **ResizeObserver 与 MediaQueryList**：`ResizeObserver` 在元素尺寸变化时回调，可选观察 `contentRect`（CSS 像素）或 `devicePixelContentBoxSize`（设备像素，Safari 不支持）；`matchMedia("(resolution: 2dppx)")` 返回的 `MediaQueryList` 可以在查询条件失配时（即 dpr 变了）发 `change` 事件。
- **前置讲义结论**：u7-l1 的 `WebPlatform` 把图形初始化推迟到 `run` 内的 `spawn_local`，成功后把「画布 + surface」存进 `prepared_window` 并触发启动回调；u3-l2 给出了 `PlatformWindow` 契约的方法分组与 `schedule_frame` 语义（「请求平台调度下一帧」，Wayland 是唯一覆写者）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/gpui_web/src/window.rs` | `WebWindow`/`WebWindowInner`：canvas 与隐藏 input 的组装、几何与 dpr、rAF 帧驱动、`PlatformWindow` 的完整实现 |
| `crates/gpui_web/src/events.rs` | `WebEventListeners`/`EventListenerHandle`/`ClickState`：DOM 事件注册、到 `PlatformInput` 的翻译与一系列纯函数映射表 |
| `crates/gpui_web/src/display.rs` | `WebDisplay`：把浏览器屏幕与视口伪装成一台 `PlatformDisplay` |
| `crates/gpui_web/src/platform.rs` | `WebWindowLifecycle` 状态闸门与 `open_window` 的衔接（u7-l1 已讲图形初始化流水线，本讲只取开窗一节） |
| `crates/gpui/src/platform.rs` | `PlatformWindow` 契约：`frame_waker` 默认返回 `None`、`on_request_frame` 必需、`schedule_frame` 默认空实现 |
| `crates/gpui/src/window.rs` | GPUI 侧消费方：`on_request_frame` 回调的注册与帧调度（1533 起）、`set_platform_waker(frame_waker)`、`present()` 的定义 |
| `crates/gpui_wgpu/src/wgpu_renderer.rs` | `WgpuRenderer::draw` 末尾的 `frame.present()`——web 上「呈现」的真正落点 |

## 4. 核心概念与源码讲解

### 4.1 WebWindow：用 canvas 与隐藏 input 拼出一个「窗口」

#### 4.1.1 概念说明

桌面平台上，「窗口」是操作系统提供的对象：有标题栏、有焦点、能最小化、能被窗口管理器移动。浏览器里没有这些东西——一个标签页就是全部。`WebWindow` 的策略是**用 DOM 元素模拟窗口语义**：

- 一张铺满 `<body>` 的 `<canvas>` 充当绘制表面（对应「窗口客户区」）；
- 一个 1×1 像素、透明、固定在左上角的隐藏 `<input>` 元素充当**键盘焦点靶子**——DOM 键盘事件只会派发给当前聚焦的元素，canvas 默认不可聚焦，所以键盘输入、IME 组合输入、粘贴都必须落在 input 上；
- `document.title` 充当窗口标题，`document.fullscreenElement` 充当全屏状态，标签页可见性充当激活状态。

与桌面窗口的本质差异是**单窗口约束**：`WebWindowLifecycle` 枚举把一个标签页的生命周期建模为四个状态，`open_window` 只允许 `Available` 状态通过，窗口关闭后进入 `Closed`，再次开窗会得到 `ReopeningUnsupported` 错误——不存在第二个顶层窗口。

#### 4.1.2 核心流程

`WebWindow` 的一生：

```text
initialize_graphics（u7-l1）
    └─ prepare_canvas：建 canvas、设样式、挂到 body
    └─ 成功后存入 prepared_window，触发 run 回调
cx.open_window（GPUI 应用层）
    └─ WebPlatform::open_window
        ├─ lifecycle 闸门：只有 Available 放行
        ├─ 取出 prepared_window（canvas + wgpu surface）
        └─ WebWindow::new
            ├─ 读 dpr、max_texture_dimension，建 WgpuRenderer
            ├─ 建隐藏 input 元素并立即 focus
            ├─ 建 WebDisplay
            ├─ create_raf_closure + wake_frame_loop（排上第一个 rAF）
            ├─ 建 ResizeObserver（观察 canvas + 监视 dpr 变化）
            └─ register_event_listeners（17 个基础监听器 + 3 个补充）
lifecycle = Open
    …（运行期：rAF 驱动帧、DOM 事件驱动输入、ResizeObserver 驱动几何）
Drop for WebWindow
    └─ 取消 rAF、断开 observer、解开 mql 循环引用
       移除 canvas 与 input、清空 active_window
lifecycle = Closed（此后不可重开）
```

#### 4.1.3 源码精读

先看 DOM 组装。`prepare_canvas` 创建 canvas，设 `tab_index(-1)`（可编程聚焦但不进 Tab 序列），然后是五条样式：宽高 100% 铺满、`display: block` 消除行内间隙、`outline: none` 去聚焦框、`touch-action: none` 把触摸手势全部留给自己处理（否则浏览器会拦截触摸做滚动手势）：

[crates/gpui_web/src/window.rs:77-109](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/window.rs#L77-L109) —— 这段代码创建 canvas、设置 `tabindex=-1` 与五条内联样式（含关键的 `touch-action: none`），最后挂到 `body` 上。

构造函数里，隐藏 input 元素被建出来并**立即 `focus()`**——这就是键盘焦点的「初始落点」，也是后文所有 `listen_input` 注册的事件目标：

[crates/gpui_web/src/window.rs:141-155](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/window.rs#L141-L155) —— 这段代码创建一个 `position:fixed`、1×1 像素、`opacity:0` 的 `<input>`，挂到 body 后立刻调用 `focus()`，让它成为键盘事件的接收者。

再看外壳与内核的两层结构。`WebWindow`（外壳，被 `Box` 成 `dyn PlatformWindow` 交给 GPUI）持有 `Rc<WebWindowInner>`（内核，被各事件闭包共享）以及一批**必须存活到窗口关闭为止**的字段——`_raf_closure`、`_resize_observer`、`_event_listeners` 全部以下划线前缀命名，说明它们的价值在 Drop 而非读取：

[crates/gpui_web/src/window.rs:65-74](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/window.rs#L65-L74) —— 这段代码定义 `WebWindow`：外壳持有内核 `Rc<WebWindowInner>`、显示器、lifecycle 与 active_window 共享单元，以及 rAF 闭包、ResizeObserver、事件监听器三个「保活字段」。

单窗口闸门在平台侧。`WebPlatform::open_window` 先查 `window_lifecycle`，`Open` 报 `AlreadyOpen`、`Closed` 报 `ReopeningUnsupported`、`Unavailable` 报 `GraphicsUnavailable`，只有 `Available` 继续；随后把 `prepared_window`（u7-l1 里图形初始化成功后存下的 canvas+surface）取走并构造 `WebWindow`，成功后置 `Open`：

[crates/gpui_web/src/platform.rs:351-370](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/platform.rs#L351-L370) —— 这段代码是 lifecycle 四态闸门与 `prepared_window` 的取出：`wgpu_context` 或画布未就绪时返回 `GraphicsInitializationPending`，提示「从 `Platform::run` 的回调里重试」。

窗口的销毁同样讲究。`Drop` 里按注释给出的顺序做四件事：取消挂起的 rAF 再释放闭包、拿走 `raf_function` 让遗留的 waker 变成 no-op、断开 ResizeObserver、取出 `mql_handle` 解开「闭包捕获 inner、inner 又存闭包」的引用循环，最后移除两个 DOM 元素并置 `lifecycle = Closed`：

[crates/gpui_web/src/window.rs:505-535](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/window.rs#L505-L535) —— 这段代码是 `WebWindow` 的析构：先 `cancel_animation_frame` 防「closure invoked after being dropped」，再拆掉 dpr 媒体查询的引用循环，最后移除 canvas/input 并把生命周期推进到 `Closed`。

契约侧的「窗口身份」靠 raw-window-handle（回顾 u3-l2：`PlatformWindow` 以 `HasWindowHandle`/`HasDisplayHandle` 为 supertrait 对接 wgpu 生态）。web 的实现把 canvas 的 `JsValue` 指针直接包进 `WebCanvasWindowHandle`，显示器句柄则永远是「web 显示器」：

[crates/gpui_web/src/window.rs:582-599](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/window.rs#L582-L599) —— 这段代码实现两个 raw-window-handle trait：窗口句柄是包着 canvas 指针的 `WebCanvasWindowHandle`，显示句柄是固定的 `DisplayHandle::web()`。

能力取舍在 `impl PlatformWindow for WebWindow`（601-835 行）里一览无余，挑几个代表：

| 方法 | web 上的姿态 | 位置 |
| --- | --- | --- |
| `set_title` | 真实工作：写 `document.title` | [window.rs:686-691](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/window.rs#L686-L691) |
| `minimize` / `zoom` | 日志警告的 no-op：浏览器不暴露这两个动作 | [window.rs:695-701](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/window.rs#L695-L701) |
| `toggle_fullscreen` | 走 Fullscreen API，状态由 `fullscreenchange` 事件回填 | [window.rs:703-716](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/window.rs#L703-L716) |
| `prompt` | 返回 `None`（不支持平台级对话框） | [window.rs:660-668](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/window.rs#L660-L668) |
| `window_decorations` / `window_controls` | 返回固定值 `Decorations::Server`；控制能力只有 fullscreen 为 true | [window.rs:819-832](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/window.rs#L819-L832) |
| `start_window_move` / `start_window_resize` / `update_ime_position` | 空 no-op | [window.rs:809-817](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/window.rs#L809-L817) |

另一个值得注意的取舍：构造函数签名里的 `_handle` 与 `_params` 都是**未使用的**——u3-l1 精读的 `WindowOptions → WindowParams` 翻译产物（窗口边界、标题栏、装饰）在 web 上被整体丢弃，因为「窗口在屏幕上的位置」这件事浏览器根本不交给页面决定。

#### 4.1.4 代码实践

**实践目标**：不运行程序，仅靠源码画出 `WebWindow` 的生命周期状态图，并列出它创建的全部 DOM 资产。

**操作步骤**：

1. 读 [crates/gpui_web/src/platform.rs:60-66](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/platform.rs#L60-L66) 的 `WebWindowLifecycle` 枚举，抄下四个状态。
2. 在 `platform.rs` 中搜索 `window_lifecycle.set`，把每次状态迁移的触发条件（图形初始化成功/失败、开窗成功/失败、Drop）标注到状态之间。
3. 读 [crates/gpui_web/src/window.rs:111-224](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/window.rs#L111-L224) 的构造函数，列出它创建或注册的全部资源：canvas（哪来的？）、input 元素、`WebDisplay`、rAF、ResizeObserver、媒体查询监听、17+3 个事件监听器。

**需要观察的现象**：状态图中 `Closed` 是否有任何出边；资源清单里哪些注册在 canvas 上、哪些在 input 元素上、哪些在 document/媒体查询上。

**预期结果**：`Closed` 没有出边（重开报 `ReopeningUnsupported`）；`Unavailable` 也基本是终态（只有重新初始化图形才可能离开，而这条路径不存在）。资源清单应包含：canvas、隐藏 input、rAF 闭包、ResizeObserver（含 dpr 媒体查询）、visibilitychange/fullscreenchange/prefers-color-scheme 三个 document 级监听，以及 events.rs 里注册的 17 个基础监听器。若要在浏览器里实地核验 DOM 资产，可在运行中的页面上打开 DevTools 的 Elements 面板找 `tabindex="-1"` 的 canvas 与 1×1 透明 input（**待本地验证**，构建流程在 u7-l3 讲）。

#### 4.1.5 小练习与答案

**练习 1**：为什么窗口关闭后再次 `open_window` 会失败，而桌面平台（例如 Linux）可以反复开窗？

**答案**：浏览器的一个标签页只有一份图形资源：`prepare_canvas` 只在图形初始化时执行一次，`prepared_window` 被 `open_window` 取走后不再补充；`WebWindowLifecycle::Closed` 因此是终态，重开报 `ReopeningUnsupported`。桌面平台的窗口是操作系统的可再生资源，`Platform::open_window` 每次都能新建一个。

**练习 2**：`Drop` 里为什么必须先 `cancel_animation_frame` 再让 `_raf_closure` 释放？只删掉 canvas 不行吗？

**答案**：rAF 回调挂在 `browser_window` 上而不是 canvas 上，删 canvas 不影响它触发；若闭包已释放而回调还挂着，下次浏览器触发时就会抛 "closure invoked after being dropped"。此外还拿走了 `raf_function`，让可能比窗口活得久的 `frame_waker`（持 `Weak`）升级失败后安静地 no-op。

**练习 3**：`WebWindow::new` 为什么要立刻对隐藏 input 调用 `focus()`？

**答案**：DOM 键盘事件只派发给当前聚焦元素。若不聚焦，用户第一次按键会落空（或被浏览器当成快捷键）。后续 `pointerdown` 处理器里也会再次 `focus()`，保证「点一下画布后键盘仍然进 input」。

### 4.2 尺寸与设备像素比：ResizeObserver 双轨与纹理上限钳制

#### 4.2.1 概念说明

GPUI 用逻辑像素 `Pixels` 描述布局，GPU 用物理像素的帧缓冲绘制，两者靠 `scale_factor` 换算（u3-l2）。web 上这个 `scale_factor` 就是 `window.devicePixelRatio`，核心公式：

\[ p_{\text{device}} = p_{\text{css}} \cdot r, \qquad r = \text{devicePixelRatio} \]

麻烦在于「谁先谁等」：`ResizeObserver` 的回调能给你**设备像素精确值**（`devicePixelContentBoxSize`，Chrome/Firefox 支持），也能给你 **CSS 像素值**（`contentRect`，所有浏览器都支持但 Safari 只有这个）。前者是「物理优先」——先拿到物理尺寸再除以 dpr 反推逻辑；后者是「逻辑优先」——先拿逻辑再乘 dpr 取整。Safari 不支持前者，所以 `check_device_pixel_support` 在启动时探测一次 `ResizeObserverEntry.prototype.devicePixelContentBoxSize` 是否存在，决定走哪条轨。

第二个约束是 **GPU 纹理上限**：canvas 的绘制缓冲（`canvas.width/height` 属性，单位是物理像素）不能超过 `max_texture_dimension_2d`（比如 8192 或 16384）。4K 屏 + 500% 浏览器缩放完全可能超限，所以物理尺寸要钳制：

\[ c = \min(p_{\text{device}},\ m_{\text{tex}}), \qquad l' = \frac{c}{r} \]

注意钳制之后**逻辑尺寸要从钳制后的物理尺寸重新反推**——若沿用原来的逻辑值，`scale_factor` 就不再把 GPUI 的逻辑 bounds 精确映射到 surface 上，等于暗中扭曲了有效缩放。

#### 4.2.2 核心流程

一次 canvas 尺寸变化（用户拖动窗口、切换全屏、浏览器缩放）的处理：

```text
ResizeObserver 回调（entries[0]）
 ├─ 读当前 dpr
 ├─ 双轨取尺寸：
 │    支持设备像素轨：pw/ph ← devicePixelContentBoxSize；lw/lh = pw/dpr
 │    Safari 轨：      lw/lh ← contentRect；pw/ph = round(lw × dpr)
 ├─ notify_scale 或 物理尺寸变化才继续，否则直接返回（去抖）
 ├─ 物理尺寸为 0（如 display:none）→ bounds 清零、仍触发 resize 回调告知 GPUI
 ├─ 钳制到 max_texture_dimension，必要时从钳制值反推逻辑尺寸
 ├─ 存 pending_physical_size（延迟到下一次 draw 才真正应用）
 ├─ 更新 state.bounds（逻辑）与 state.scale_factor
 └─ 触发 callbacks.resize(logical_size, dpr) → GPUI 的 bounds_changed

下一次 WebWindow::draw
 ├─ 取出 pending_physical_size → 写 canvas.width/height（物理像素绘制缓冲）
 ├─ renderer.update_drawable_size(物理尺寸)
 └─ renderer.draw(scene)
```

dpr 本身的变化走另一条通道：`watch_dpr_changes` 用 `matchMedia("(resolution: {当前dpr}dppx)")` 挂一个 change 监听——**当前值失配**即意味着 dpr 变了；回调里置 `notify_scale` 标志、重新观察 canvas（拿新的物理尺寸）、并重新挂一个新的媒体查询（针对新 dpr）。

#### 4.2.3 源码精读

双轨取尺寸与钳制逻辑都在 `create_resize_observer_closure` 里：

[crates/gpui_web/src/window.rs:238-257](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/window.rs#L238-L257) —— 这段代码是双轨取尺寸：支持时从 `devicePixelContentBoxSize` 拿物理像素再除以 dpr 反推逻辑；Safari 回退到 `contentRect`（CSS 像素）再乘 dpr 取整。

[crates/gpui_web/src/window.rs:285-314](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/window.rs#L285-L314) —— 这段代码先把物理尺寸钳制到 `max_texture_dimension`，再**从钳制后的物理尺寸重新反推逻辑尺寸**（注释明说：否则钳制会暗中扭曲有效缩放），最后写入 state 并存入 `pending_physical_size`。

去抖与零尺寸处理：

[crates/gpui_web/src/window.rs:259-283](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/window.rs#L259-L283) —— 这段代码在「dpr 没变且物理尺寸没变」时直接返回（ResizeObserver 可能因无关原因触发）；物理尺寸为 0 时把 bounds 清零但仍触发 resize 回调，让 GPUI 知道窗口「没了」。

dpr 探测与延迟应用：

[crates/gpui_web/src/window.rs:396-420](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/window.rs#L396-L420) —— 这段代码用「针对当前 dpr 的媒体查询失配」来探测缩放变化：回调里置 `notify_scale`、重新观察 canvas、再挂针对新 dpr 的查询。

[crates/gpui_web/src/window.rs:775-791](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/window.rs#L775-L791) —— 这段代码是 `draw`：先取出 `pending_physical_size` 写入 `canvas.width/height`（物理像素绘制缓冲）并更新渲染器尺寸，再画 scene。几何变化由此与帧节奏对齐——resize 只做记录，真正改帧缓冲要等下一帧。

能力探测本身是一次原型检查：

[crates/gpui_web/src/window.rs:566-580](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/window.rs#L566-L580) —— 这段代码检查 `ResizeObserverEntry.prototype.devicePixelContentBoxSize` 描述符是否存在，Safari 上不存在则整个窗口走 CSS 像素轨。

#### 4.2.4 代码实践

**实践目标**：手算一遍双轨换算与纹理钳制，验证你理解了两条公式。

**操作步骤**：

1. 假设 canvas 的 CSS 尺寸为 800×600，`devicePixelRatio = 2`，`max_texture_dimension_2d = 1024`。
2. 分别按「设备像素轨」与「Safari 轨」写出回调算出的 `(pw, ph, lw, lh)`。
3. 套用钳制公式，写出最终 `pending_physical_size` 与 `state.bounds.size`。
4. 把 dpr 换成 0.5（浏览器缩放 50%）再算一遍，观察钳制是否还触发。

**需要观察的现象**：两条轨在无钳制时结果是否一致（Safari 轨有取整，允许出现 ±1 像素差）；钳制后逻辑尺寸是否由钳制值反推。

**预期结果**：两条轨都得物理 1600×1200；钳制后 `pending_physical_size = (1024, 1024)`，逻辑尺寸 = 1024/2 = 512×512（而不是原来的 800×600）。dpr=0.5 时物理为 400×300，不触发钳制，逻辑尺寸维持 800×600。如需实地验证，可在浏览器里用 Ctrl+加减 调整缩放并观察 `window.devicePixelRatio` 的变化（**待本地验证**）。

#### 4.2.5 小练习与答案

**练习 1**：既然 `contentRect` 所有浏览器都支持，为什么还要优先用 `devicePixelContentBoxSize`？

**答案**：`contentRect × dpr` 是**推算**：dpr 在缩放过渡期可能是小数、取整会累积误差，浏览器对元素的实际物理像素分配也可能与 CSS 尺寸不成严格整数倍。`devicePixelContentBoxSize` 是浏览器**实测**的设备像素盒，精确且免去取整歧义；Safari 没有它，才退而求其次。

**练习 2**：为什么 resize 回调里只记录 `pending_physical_size`，而不是立刻写 `canvas.width/height`？

**答案**：改 canvas 绘制缓冲尺寸会使 surface 失配、触发昂贵的重新配置，且改完必须紧跟一次重绘才有意义。推迟到下一次 `draw` 应用，可以把「尺寸变化」与「帧节奏」合并：一帧内多次 resize 只生效最后一次，且总是与渲染同步发生。

**练习 3**：`watch_dpr_changes` 挂的媒体查询为什么写成 `(resolution: {当前dpr}dppx)` 这种「匹配当前值」的形式？

**答案**：媒体查询的 `change` 事件在**匹配结果翻转**时触发。挂一个恰好匹配当前 dpr 的查询，dpr 一变它就从匹配变失配，于是收到事件——这是「监视任意一个值变化」的标准技巧（`-webkit-device-pixel-ratio` 兼容旧 Safari）。回调里再针对新 dpr 挂新查询，形成滚动监视。

### 4.3 帧驱动：requestAnimationFrame 闭环与 schedule_frame 的默认空实现

#### 4.3.1 概念说明

u3-l2 讲过 eb354c8d50 之后的契约：`PlatformWindow::schedule_frame` 语义是「请求平台调度下一帧」，**默认空实现**代表「平台自有帧驱动、不需要 GPUI 逐帧催促」，目前只有 Wayland（唤醒停泊于 Parked 的渲染循环）与测试 `TestWindow` 覆写。`WebWindow` 属于「不覆写」阵营——通读 [window.rs:601-835](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/window.rs#L601-L835) 的整个 `impl PlatformWindow for WebWindow`，找不到 `schedule_frame` 这个方法。

那 web 的帧从哪来？答案是**旧一点的两个契约方法**组成的 rAF 闭环：

- `frame_waker()`（契约默认返回 `None`）：GPUI 把它存进窗口的 invalidator 作为「平台唤醒器」——任何 `cx.notify()` 式的失效都会调用它。`WebWindow` 覆写它返回一个调用 `wake_frame_loop` 的闭包，即「排一次 rAF」。
- `on_request_frame(callback)`（必需方法）：rAF 回调触发时调用它，GPUI 在里面完成「取 next_frame_callbacks → 该画就画 → 该提交就提交」的一整套帧逻辑。

呈现（present）也没有独立平台机制：GPUI 的 `Window::present()` 只是拿缓存 scene 再调一次 `platform_window.draw`，而 web 的 `draw` 最终落到 `WgpuRenderer::draw`，后者在函数末尾直接 `frame.present()`——把交换链纹理交还浏览器合成器，上屏时机由合成器决定。没有 vsync 回调线程（Windows）、没有 frame callback 协议（Wayland），这就是「呈现交给 wgpu surface」的含义。

与 Wayland 对照：Wayland 窗口空闲时渲染循环**停泊**在 Parked 态、零唤醒，需要 GPUI 在有脏区时调 `schedule_frame` 经 frame_ping 主动唤醒；web 窗口空闲时**没有挂起的 rAF**（排一次只回调一次），失效时经 `frame_waker` 排上新的 rAF。两者都是「按需驱动」，只是「谁排队」一个在平台（Wayland 的 calloop）、一个在浏览器（rAF 队列）。

#### 4.3.2 核心流程

一次 `cx.notify()` 到下一帧上屏的完整旅程：

```text
应用代码 cx.notify()
 └─ invalidator 标脏 → wake_platform()
     └─ WebWindow::frame_waker 闭包（持 inner 的 Weak）
         └─ wake_frame_loop：若无挂起 rAF → requestAnimationFrame(js 函数)
浏览器下一次重绘前触发 rAF
 └─ raf 闭包：
     ├─ 先清 raf_id（本轮回调已消费；帧内再失效可排下一轮，不会被吞）
     └─ callbacks.request_frame(RequestFrameOptions::default)
         └─ GPUI 的 on_request_frame 回调（gpui/src/window.rs:1533 起）：
             ├─ 节流判定（ inactive / 过热降频）
             ├─ 执行 next_frame_callbacks
             ├─ invalidator 脏或 force_render → window.draw + present
             │    └─ present → platform_window.draw(scene) → WgpuRenderer::draw
             │         └─ get_current_texture → 编码 → frame.present()
             ├─ 仍脏或仍有 next_frame 回调 → schedule_frame()（web：默认空实现，无操作）
             └─ 仍脏或仍有 next_frame 回调 → invalidator.wake_platform()
                  └─ 回到开头的 frame_waker → 排下一个 rAF
```

注意闭环的收尾：GPUI 在帧末同时调了 `schedule_frame()`（web 上是 no-op）和 `invalidator.wake_platform()`（走 frame_waker）——后者才是 web 上真正续排下一帧的那条边。

#### 4.3.3 源码精读

rAF 闭包与唤醒器：

[crates/gpui_web/src/window.rs:348-372](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/window.rs#L348-L372) —— 这段代码创建 rAF 闭包：进入回调**第一件事是清掉 `raf_id`**（注释解释：不清的话，帧执行期间发出的失效会被「已有挂起请求」吞掉），然后以默认 `RequestFrameOptions` 调用 GPUI 的 request_frame 回调；同时把 JS 函数存进 `raf_function` 供后续排队复用。

[crates/gpui_web/src/window.rs:374-383](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/window.rs#L374-L383) —— 这段代码是 `wake_frame_loop`：`raf_id` 已有挂起就直接返回（唤醒合并——多次失效只排一个 rAF），否则排一个新请求。

[crates/gpui_web/src/window.rs:722-733](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/window.rs#L722-L733) —— 这段代码是 `frame_waker` 的覆写：闭包持 `Weak` 而非 `Rc`（注释解释：waker 存在窗口的 invalidator 里，而 request_frame 回调又捕获 invalidator 的克隆，强引用会成环泄漏）；升级失败（窗口已关）就安静返回。

契约侧的三個方法签名（注意默认值）：

[crates/gpui/src/platform.rs:849-864](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/platform.rs#L849-L864) —— 这段代码是契约定义：`frame_waker` 默认返回 `None`、`on_request_frame` 必需实现、`schedule_frame` 默认空实现（`fn schedule_frame(&self) {}`）——`WebWindow` 覆写前两者、对后者保持沉默。

GPUI 消费方的闭环收尾：

[crates/gpui/src/window.rs:1631-1669](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/window.rs#L1631-L1669) —— 这段代码是 GPUI 的 request_frame 回调主体：脏了就 `window.draw + present`；帧末若仍脏或仍有 next_frame 回调，先调 `platform_window.schedule_frame()`（web 上 no-op），**再**调 `invalidator.wake_platform()`——后者经 frame_waker 排下一个 rAF，是 web 上真正的续帧边。

[crates/gpui/src/window.rs:1672-1672](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/window.rs#L1672-L1672) —— 这行代码把 `platform_window.frame_waker()` 设为 invalidator 的平台唤醒器，接通「失效 → rAF」这条边。

呈现的落点：

[crates/gpui/src/window.rs:3016-3031](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/window.rs#L3016-L3031) —— 这段代码定义 `Window::present`：它没有独立的平台呈现调用，就是拿缓存 scene 再调一次 `platform_window.draw`。

[crates/gpui_wgpu/src/wgpu_renderer.rs:1311-1312](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_wgpu/src/wgpu_renderer.rs#L1311-L1312) 与 [crates/gpui_wgpu/src/wgpu_renderer.rs:1401-1402](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_wgpu/src/wgpu_renderer.rs#L1401-L1402) —— 这两处是 web 呈现的全部真相：`draw` 内部 `surface.get_current_texture()` 取帧，编码完直接 `frame.present()` 交还浏览器合成器；「present」内嵌在 draw 里，没有单独的呈现通道。

#### 4.3.4 代码实践

**实践目标**：在 GPUI 侧源码里跟踪一遍「一次失效到下一帧」的调用链，写出时序列表，并标注 web 与 Wayland 在每个节点上的对应物。

**操作步骤**：

1. 从 [crates/gpui/src/window.rs:218-231](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/window.rs#L218-L231) 读 `set_platform_waker` 与 `wake_platform` 的实现，弄清「谁调用了 waker」。
2. 顺着 4.3.2 的流程图，把每个箭头写成一行「函数 A → 函数 B（文件:行）」。
3. 对照 u5-l4 讲过的 Wayland `FrameLoop`：为流程图的每个节点写一行「Wayland 上这一步是谁」（提示：停泊唤醒对应 `schedule_frame` → `frame_ping`；帧完成对应 `wl_callback::Done` → `frame_callback_fired`）。

**需要观察的现象**：web 闭环里 `schedule_frame()` 出现的位置，以及为什么它的空实现不会让 web 窗口「卡死」。

**预期结果**：web 闭环中 `schedule_frame()` 是 no-op，但紧随其后的 `invalidator.wake_platform()` → `frame_waker` → `requestAnimationFrame` 保证了续帧，所以不依赖 `schedule_frame`；Wayland 恰好相反——它的帧源（frame callback）需要平台主动安排，所以覆写 `schedule_frame` 唤醒停泊循环。此实践为纯源码阅读，无需运行环境。

#### 4.3.5 小练习与答案

**练习 1**：raf 闭包为什么必须在执行回调体之前先清 `raf_id`？

**答案**：`wake_frame_loop` 用 `raf_id.is_some()` 判断「是否已有挂起请求」来合并唤醒。若回调开始时不清掉，帧执行期间（比如 draw 过程中视图又失效了）发出的唤醒会看到「已有挂起」而跳过排队——而那个「挂起」其实已经被本次回调消费掉了，失效就再也等不到下一帧。

**练习 2**：`WebWindow` 不覆写 `schedule_frame`，为什么不算违反契约？

**答案**：契约的默认空实现语义就是「平台自有帧驱动、无需 GPUI 催促」。web 的帧源（rAF）本身就是按需排队的：有事做时 frame_waker 排一个，没事做时一个都不排——这正是默认实现所描述的姿态。Wayland 的问题在于帧源（frame callback）会随窗口空闲而停泊，必须有人显式唤醒，所以才需要覆写。

**练习 3**：`frame_waker` 返回的闭包为什么持 `Weak<WebWindowInner>` 而不是 `Rc`？

**答案**：waker 被存进窗口自己的 invalidator，而 GPUI 的 `on_request_frame` 回调又捕获了 invalidator 的克隆——`Rc` 会形成「window → invalidator → waker → window」的引用环，窗口关闭后永远无法释放。`Weak` 在窗口已关时升级失败、安静返回，配合 Drop 时拿走 `raf_function`，保证遗留 waker 完全无害。

### 4.4 WebEventListeners：从浏览器事件到 PlatformInput

#### 4.4.1 概念说明

GPUI 的输入统一抽象是 `PlatformInput` 枚举（`MouseDown`/`MouseUp`/`MouseMove`/`MouseExited`/`ScrollWheel`/`KeyDown`/`KeyUp`/`ModifiersChanged`）。桌面上它由 XCB 事件、NSEvent、Windows 消息翻译而来；web 上翻译的原料是 DOM 事件。`WebEventListeners` 就是这层翻译的全部载体，它本体只是一些**必须存活到窗口关闭**的 `EventListenerHandle` 的集合。

`EventListenerHandle` 解决的是 wasm-bindgen 的资源管理问题：`Closure` 传给 `addEventListener` 后，必须既保活又能在窗口关闭时反注册，否则会遭遇 "closure invoked after been dropped"。它的做法是把**目标元素、事件名、闭包**三元组一起持有，`Drop` 时先 `removeEventListener` 再让闭包释放。

分发通道是 `WebWindowCallbacks::input` 槽位：DOM 监听器把翻译好的 `PlatformInput` 交给 `dispatch_input`，后者经 `with_callback` 调用 GPUI 注册的 `on_input` 回调；返回的 `DispatchEventResult` 里 `propagate == false` 表示 GPUI 消化了此键，web 侧随即 `prevent_default()` 抑制浏览器默认行为。`with_callback` 采用 take→call→restore 的借用舞蹈，避免回调重入平台窗口时撞上 `RefCell` 的 `BorrowMutError`。

一个容易误解的点：**GPUI 的 `MouseDown` 对应的浏览器事件是 `pointerdown`，不是 `mousedown`**；而 `blur` 根本不翻译成 `PlatformInput`——它走 `active_status_change` 回调通道。做映射表时必须以「GPUI 产物」为锚点。

#### 4.4.2 核心流程

注册（构造时一次性完成）：

```text
register_event_listeners
 ├─ canvas 上（指针与拖放类，9 个）：
 │    pointerdown / pointerup / pointermove / pointerleave / pointerenter
 │    wheel（non-passive）/ contextmenu / dragover / drop
 ├─ 隐藏 input 上（键盘与文本类，8 个）：
 │    keydown / keyup / paste
 │    compositionstart / compositionupdate / compositionend
 │    focus / blur
 ├─ document 上：visibilitychange（→激活态） / fullscreenchange（→全屏态）
 └─ matchMedia 上：prefers-color-scheme change（→外观变化）
```

分发（以 keydown 为例的三段式）：

```text
keydown（input 元素）
 ├─ ① 读修饰键与 capslock；与缓存不同 → 先发 PlatformInput::ModifiersChanged
 ├─ ② dom_key_to_gpui_key 映射键名；纯修饰键 → 到此为止
 ├─ ③ 组 Keystroke{modifiers, key, key_char} → dispatch_input(KeyDown)
 │      └─ GPUI 返回 DispatchEventResult：
 │           propagate == false → prevent_default()（快捷键被 GPUI 吃掉）
 ├─ ④ 组合输入进行中（IME）→ prevent_default() 返回
 └─ ⑤ keystroke_inserts_text 且有 key_char
        → input_handler.replace_text_in_range(None, text)（文本进编辑器）
        → prevent_default()（防空格滚页、quick-find 等浏览器副作用）
```

#### 4.4.3 源码精读

注册清单与三个注册助手：

[crates/gpui_web/src/events.rs:121-146](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/events.rs#L121-L146) —— 这段代码是全部监听器的注册清单：17 个基础监听器加 3 个补充（可见性、外观、全屏），返回 `WebEventListeners`。

[crates/gpui_web/src/events.rs:29-49](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/events.rs#L29-L49) —— 这段代码是 `EventListenerHandle::add`：闭包 + 目标 + 事件名三元组；doc 注释解释了为什么必须连目标一起持有——只 drop 闭包会让 DOM 上的监听指向已释放的函数。

[crates/gpui_web/src/events.rs:50-72](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/events.rs#L50-L72) —— 这段代码是 `add_non_passive`：以 `{passive: false}` 注册，注释说明现代浏览器默认把 `wheel` 设为 passive、不关掉它 `preventDefault()` 就无效；移除时不需匹配 passive 选项。

指针类事件的翻译（注意三个浏览器特有动作）：

[crates/gpui_web/src/events.rs:187-222](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/events.rs#L187-L222) —— 这段代码处理 `pointerdown`：`prevent_default` 后**聚焦隐藏 input**（保证键盘跟进）、`set_pointer_capture`（注释解释：拖拽拖出画布后仍能收到 move/up，否则松开在画布外时 `pressed_button` 会卡住）、用 `ClickState` 累计多击计数，最后产出 `PlatformInput::MouseDown`。

[crates/gpui_web/src/events.rs:86-118](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/events.rs#L86-L118) —— 这段代码是 `ClickState`：两次点击间隔小于 400ms 且位移小于 5px 才累计 `current_count`——浏览器不提供「这是第几连击」，多击计数完全自己合成（macOS 桌面上 `NSEvent` 原生给 `clickCount`）。

[crates/gpui_web/src/events.rs:305-337](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/events.rs#L305-L337) —— 这段代码处理 `wheel`（non-passive 注册）：按 `delta_mode` 区分 `ScrollDelta::Lines`（模式 1）与 `ScrollDelta::Pixels`（模式 0/2），**两个分量都取反**以对齐 GPUI 在桌面平台上的滚动方向约定。

键盘类事件（最长的一条链）：

[crates/gpui_web/src/events.rs:629-663](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/events.rs#L629-L663) —— 这段代码是 `dom_key_to_gpui_key` 映射表：`Enter→enter`、`" "→space`、`Meta→platform`、方向键/Home/End/F1-F35 全部小写化——回顾 u3-l3 的结论：`key` 取布局无关的键帽语义，浏览器已经替我们做了这层翻译（桌面要靠 xkbcommon/Carbon）。

[crates/gpui_web/src/events.rs:744-778](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/events.rs#L744-L778) —— 这两段是文本插入判定的核心：`keystroke_inserts_text` 区分平台（macOS 上 Option 参与组字所以只排除 Command/Control；其他平台裸 Alt 是快捷键，但 AltGr 被浏览器报成 control+alt 且 `event.key()` 已带组合字符）；`compute_key_char` 据此决定 `key_char` 取什么。

[crates/gpui_web/src/events.rs:366-427](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/events.rs#L366-L427) —— 这段代码是完整的 `keydown` 处理链，即 4.4.2 流程图的源码化身；末尾注释点明 `prevent_default` 的动机：字符已进输入处理器，必须压制空格滚页、quick-find 等**同一按键的浏览器副作用**，而上面没处理的组合（浏览器快捷键）保持默认行为。

粘贴与 IME（浏览器特有的两条通道）：

[crates/gpui_web/src/events.rs:467-511](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/events.rs#L467-L511) —— 这段注释与代码解释了粘贴为什么走 DOM `paste` 事件而非 `Platform::read_from_clipboard`：浏览器的异步剪贴板读塞不进同步签名，而 `ClipboardEvent` 里的 `clipboardData` 在事件内是同步可读的；**File 句柄必须同步收集**，因为事件返回后条目列表就被浏览器清空了。

[crates/gpui_web/src/events.rs:553-584](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/events.rs#L553-L584) —— 这段代码处理 IME 组合输入的三个事件：`compositionupdate` 期间用 `replace_and_mark_text_in_range` 打标记（下划线预编辑文本），`compositionend` 时 `replace_text_in_range` 定稿并 `unmark_text`、清空 input 的值。

焦点与激活（非 `PlatformInput` 通道）：

[crates/gpui_web/src/events.rs:586-612](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/events.rs#L586-L612) —— 这段代码处理 input 元素的 `focus`/`blur`：不走 `PlatformInput`，而是置 `is_active` 并触发 `active_status_change` 回调——浏览器的「窗口激活」等价于「隐藏 input 是否聚焦」。

[crates/gpui_web/src/window.rs:422-453](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/window.rs#L422-L453) —— 这段代码是 `visibilitychange` 监听：标签页切走/切回同样驱动 `is_active` 与 `active_status_change`——桌面上「窗口失焦」与浏览器里「切标签页」在 GPUI 眼里是同一件事。

拖放的「拦截但不翻译」：

[crates/gpui_web/src/events.rs:345-364](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/events.rs#L345-L364) —— 这段注释与代码说明 dragover/drop 只 `prevent_default`（阻止浏览器导航到被拖入的文件），**不合成 `FileDrop` 事件**：浏览器只给 `File` 对象、永远不给文件系统路径，GPUI 的 `ExternalPaths` 消费者会去读不存在的路径。

#### 4.4.4 代码实践

**实践目标**：制作「浏览器事件 → GPUI 产物」映射表（本讲的主实践），并识别至少一处浏览器特有而桌面没有的行为。

**操作步骤**：

1. 打开 [crates/gpui_web/src/events.rs:121-146](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/events.rs#L121-L146)，把 17+3 个监听器逐个抄下，填入下表前三列（事件名、监听目标、GPUI 产物）；第四列填 `register_*` 函数名与行号。
2. 对每个事件问三个问题：产出的是 `PlatformInput` 变体、`WebWindowCallbacks` 槽位回调，还是仅 `prevent_default`？在 canvas 还是 input 元素上监听？为什么？
3. 从下表挑出至少一条「桌面不可能出现」的行为写一段解释。候选：wheel 的 passive 问题、`set_pointer_capture`、多击计数手工合成、拖放拿不到路径、paste 走 DOM 事件、激活态=焦点+可见性。

**表格模板**（已填两行作示例，**示例条目**）：

| 浏览器事件 | 监听目标 | GPUI 侧产物 | events.rs 位置 |
| --- | --- | --- | --- |
| `pointerdown` | canvas | `PlatformInput::MouseDown(MouseDownEvent)` | `register_pointer_down`，L187-222 |
| `keydown` | 隐藏 input | `ModifiersChanged`（有变化时）+ `KeyDown` + 文本插入 | `register_key_down`，L366-427 |
| `pointerup` |  |  |  |
| `pointermove` |  |  |  |
| `pointerleave` / `pointerenter` |  |  |  |
| `wheel` |  |  |  |
| `keyup` |  |  |  |
| `blur` / `focus` |  |  |  |
| `paste` |  |  |  |
| `compositionstart/update/end` |  |  |  |
| `contextmenu` / `dragover` / `drop` |  |  |  |
| `visibilitychange` / `fullscreenchange` | document |  | （在 window.rs） |

**需要观察的现象**：`mousedown` 这个 DOM 事件在表里找不到对应行——GPUI 的 `MouseDown` 由谁供给？`blur` 为什么不在 `PlatformInput` 里？

**预期结果**：`MouseDown` 由 `pointerdown` 供给（web 统一用 Pointer 事件族）；`blur` 走 `active_status_change` 回调而非 `PlatformInput`，因为 GPUI 契约里激活态变化是回调槽位（`on_active_status_change`）不是输入事件。完整参考表见 4.4.5 练习 1 的答案。本实践为源码阅读型，无需运行环境。

#### 4.4.5 小练习与答案

**练习 1**：给出映射表的完整参考答案。

**答案**：

| 浏览器事件 | 监听目标 | GPUI 侧产物 | events.rs 位置 |
| --- | --- | --- | --- |
| `pointerdown` | canvas | `PlatformInput::MouseDown` | L187-222 |
| `pointerup` | canvas | `PlatformInput::MouseUp`（click_count 沿用 MouseDown 时累计值） | L224-250 |
| `pointermove` | canvas | `PlatformInput::MouseMove` | L252-274 |
| `pointerleave` | canvas | `PlatformInput::MouseExited` + `hover_status_change(false)` | L276-303 |
| `pointerenter` | canvas | `hover_status_change(true)`（无 PlatformInput） | L614-626 |
| `wheel`（non-passive） | canvas | `PlatformInput::ScrollWheel`（Lines/Pixels 按 delta_mode） | L305-337 |
| `keydown` | input | `ModifiersChanged`（有变化时）+ `KeyDown` + `replace_text_in_range` | L366-427 |
| `keyup` | input | `ModifiersChanged`（有变化时）+ `KeyUp` | L429-465 |
| `paste` | input | `input_handler.paste`（文本同步、图片异步） | L478-551 |
| `compositionstart/update/end` | input | IME 预编辑标记/定稿（`replace_and_mark_text_in_range` 等） | L553-584 |
| `contextmenu` | canvas | 仅 `prevent_default`（抑制浏览器右键菜单） | L339-344 |
| `dragover` / `drop` | canvas | 仅 `prevent_default`，不合成 FileDrop | L352-364 |
| `focus` / `blur` | input | `active_status_change(true/false)` | L586-612 |
| `visibilitychange` | document | `active_status_change(可见性)` | window.rs L422-453 |
| `fullscreenchange` | document | 更新 `is_fullscreen`（无回调） | window.rs L458-473 |
| `prefers-color-scheme` change | MediaQueryList | `appearance_changed` 回调 | window.rs L485-502 |

**练习 2**：`keydown` 为什么注册在隐藏 input 上，而 `pointerdown` 注册在 canvas 上？

**答案**：DOM 事件的派发目标不同：指针事件派发给命中的元素（用户点的是 canvas），键盘事件派发给当前聚焦的元素。canvas 设了 `tab_index(-1)` 可以编程聚焦，但 GPUI 的文本输入与 IME 依赖真正的可编辑控件，所以用一个隐藏 input 承接键盘、组合输入与粘贴，`pointerdown` 里再顺带把焦点抢回 input。

**练习 3**：`register_wheel` 为什么要用 `listen_non_passive` 而普通的 `listen` 不行？

**答案**：现代浏览器为了滚动性能默认把 `wheel` 监听标记为 passive——passive 监听里 `preventDefault()` 被忽略。滚轮翻译后必须 `prevent_default`，否则每次滚动都会让整个页面跟着滚。`add_non_passive` 以 `{passive: false}` 注册；而移除监听时不需匹配 passive 选项，所以 `Drop` 逻辑与普通注册共用。

### 4.5 WebDisplay：把屏幕与视口伪装成一台显示器

#### 4.5.1 概念说明

u2-l3 讲过 `PlatformDisplay` 契约：`id`/`uuid`/`bounds` 必须实现，`visible_bounds`（工作区）与 `default_bounds` 带默认实现。浏览器里「显示器」是个尴尬的概念：JS 能看到 `window.screen`（所在屏幕的 CSS 像素尺寸）和视口（`innerWidth`/`innerHeight`，扣掉浏览器 chrome 后的可视区域），看不到多显示器枚举。`WebDisplay` 的策略是：一台「假显示器」——`bounds` 报屏幕尺寸，`visible_bounds` 报视口尺寸（恰好呼应桌面上「扣掉任务栏/Dock 的工作区」语义），`default_bounds` 取可见区的 75% 居中。

两个「假」细节值得注意：`id` 是写死的 `DisplayId::new(1)`；`uuid` 每次构造都随机生成（v4）。对照 u2-l3 的结论——`uuid` 本应是跨重启的稳定身份、Zed 靠它恢复窗口位置——web 的 uuid 完全不具各这种能力，每次页面加载都是新身份。

#### 4.5.2 核心流程

```text
WebWindow::new
 └─ WebDisplay::new(browser_window)：id=1、uuid=v4 随机
GPUI 查询显示器
 ├─ bounds()       ← screen.width/height（CSS px，读不到回退 1920×1080）
 ├─ visible_bounds() ← innerWidth/innerHeight（视口，回退同上）
 └─ default_bounds() ← visible 的 75%、居中
```

#### 4.5.3 源码精读

[crates/gpui_web/src/display.rs:4-16](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/display.rs#L4-L16) —— 这段代码定义 `WebDisplay` 并给出 `unsafe impl Send/Sync` 的安全论证注释：`web_sys::Window` 只在主线程访问，显示器只被前台线程读取。

[crates/gpui_web/src/display.rs:18-25](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/display.rs#L18-L25) —— 这段代码是构造函数：`id` 固定为 1，`uuid` 每次随机 v4——对比 u2-l3：桌面平台的 uuid 是持久身份，这里的 uuid 每次加载都变，不能用于恢复窗口位置。

[crates/gpui_web/src/display.rs:27-62](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/display.rs#L27-L62) —— 这两段是尺寸来源：`screen_size` 读 `window.screen`（CSS 像素），`viewport_size` 读 `innerWidth/innerHeight`；两者都有 1920×1080 回退。

[crates/gpui_web/src/display.rs:74-100](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/display.rs#L74-L100) —— 这段代码是契约三个几何方法：`bounds` 原点固定在 (0,0)（屏幕尺寸）、`visible_bounds` 用视口（对应桌面「扣任务栏」的工作区语义）、`default_bounds` 取可见区 75% 居中。

#### 4.5.4 代码实践

**实践目标**：用真实浏览器数据核验 `WebDisplay` 的三个几何方法。

**操作步骤**：

1. 打开任意网页的 DevTools 控制台，执行 `JSON.stringify({screen: [screen.width, screen.height], viewport: [innerWidth, innerHeight], dpr: devicePixelRatio})` 记下数值。
2. 手算 `WebDisplay::bounds()`、`visible_bounds()`、`default_bounds()` 在这台「显示器」上的返回值（default = visible 的 75% 居中）。
3. 改变窗口大小、切换显示器（如果有双屏），重复观察哪些值变了、哪些没变；对比 u2-l3 的 Wayland（无主显示器概念）与 X11（读 XCB screen 列表）。

**需要观察的现象**：`screen.width` 在浏览器缩放变化时是否改变；拖动窗口到另一台显示器时 `screen` 是否跟着变。

**预期结果**：`bounds` 用屏幕尺寸、`visible_bounds` 用视口、`default_bounds` 为视口 75% 居中；浏览器缩放改变的是 dpr 而非 CSS 像素口径下的 screen 尺寸（screen 以 CSS 像素报告）。精确行为**待本地验证**（不同浏览器对 `screen` 的口径有细微差异，这正说明为什么代码里到处是回退值）。

#### 4.5.5 小练习与答案

**练习 1**：`WebDisplay` 的 `uuid` 为什么不能像桌面那样用来恢复窗口位置？

**答案**：它是每次构造随机生成的 v4，页面一刷新就变。桌面平台的 uuid 来自显示器本身的稳定标识（EDID 等），跨重启不变；浏览器根本不向页面暴露「哪台显示器」的稳定身份，`WebDisplay` 只能伪造一个一次性值。这也解释了 u2-l3 里「按 uuid 匹配显示器恢复窗口」的逻辑在 web 上注定落空。

**练习 2**：`bounds` 与 `visible_bounds` 在浏览器里分别对应什么？为什么这样对应是合理的？

**答案**：`bounds` 对应 `window.screen` 的尺寸（整块屏幕），`visible_bounds` 对应视口 `innerWidth/innerHeight`（扣除浏览器地址栏、标签栏等 chrome 后的可视区）。这与桌面语义同构：`bounds` 是显示器全量几何，`visible_bounds` 是「窗口可以合理占据的工作区」。

**练习 3**：`unsafe impl Send for WebDisplay {}` 为什么是安全的？删掉会发生什么？

**答案**：注释给出的论证：`web_sys::Window` 只允许在主线程访问，而 `WebDisplay` 以 `Rc<dyn PlatformDisplay>` 发放、只在 GPUI 前台线程被读取；即便开启 `multithreaded` feature，后台 Worker 线程也从不触碰显示器。删掉它则 `Rc<dyn PlatformDisplay>` 无法满足契约可能要求的跨线程存放（编译失败）——这是「类型系统不认识浏览器的单线程约定，用注释 + unsafe 补上证明」的典型场面。

## 5. 综合实践

把本讲三个最小模块串成一份交付物：**《WebWindow 能力取舍说明书》**，三个表格加一段结论。

**任务一：补全事件映射表**。完成 4.4.4 的完整映射表（参考答案见 4.4.5 练习 1），并把「监听目标」一列单独拎出来数一数：canvas 上几个、input 上几个、document/媒体查询上几个。这个分布本身就是「浏览器窗口语义拼装」的直观体现。

**任务二：给 `impl PlatformWindow for WebWindow`（[window.rs:601-835](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/window.rs#L601-L835)）做姿态分类**。逐个方法归入四类，每类附行号：

- 真实工作（如 `set_title`、`toggle_fullscreen`、`draw`、`frame_waker`）；
- 日志警告的 no-op（`minimize`、`zoom`）；
- 静默 no-op（`update_ime_position`、`start_window_move`、`set_client_inset` 等）；
- 返回固定值/None（`is_maximized` 恒 false、`window_decorations` 恒 Server、`prompt` 恒 None）。

**任务三：帧驱动对照表**。以「帧源、需求表达、呈现落点、空闲时状态」四个维度对比 web 与 Wayland（u5-l4）与 Windows（u6-l2）：

| 维度 | WebWindow | Wayland（u5-l4） | Windows（u6-l2） |
| --- | --- | --- | --- |
| 帧源 | 浏览器 rAF 队列 | frame callback + calloop | VSyncProvider 线程 |
| 需求表达 | `frame_waker` 排 rAF；`schedule_frame` 走默认空实现 | 覆写 `schedule_frame` 经 frame_ping 唤醒 Parked 循环 | `schedule_frame` 走默认空实现 |
| 呈现落点 | `WgpuRenderer::draw` 末尾 `frame.present()` | surface commit + frame callback 确认 | 交换链 Present |
| 空闲时状态 | 无挂起 rAF，零回调 | 停泊 Parked，零唤醒 | vsync 线程持续失效窗口（持续心跳） |

**结论段**：用三到五句话回答——为什么同一个 `PlatformWindow` 契约既能落地 Wayland 的「按需唤醒 + 停泊」模型，也能落地 web 的「rAF 排队」模型，还能落地 Windows 的「持续心跳」模型？提示从「契约只约定需求方与供给方的接口，不约定帧源形态」入手，这正是 u3-l2 所说 `schedule_frame` 默认空实现代表「平台自有帧驱动」的含义。

## 6. 本讲小结

- `WebWindow` 用一张 `touch-action:none` 的 canvas（绘制表面）加一个 1×1 透明 input（键盘焦点靶子）拼出窗口语义；`WebWindowLifecycle` 四态闸门把一个标签页限制成单窗口，`Closed` 是终态、重开报 `ReopeningUnsupported`；`WindowParams` 在构造时被整体忽略。
- 几何是双轨的：支持 `devicePixelContentBoxSize` 的浏览器「物理优先」（再除 dpr 反推逻辑），Safari 走 `contentRect`「逻辑优先」（乘 dpr 取整）；物理尺寸钳制到 `max_texture_dimension_2d` 后必须从钳制值**重新反推**逻辑尺寸；dpr 变化靠「匹配当前 dpr 的媒体查询失配」探测；几何变化记录进 `pending_physical_size`、延迟到下一次 `draw` 才写 canvas 绘制缓冲。
- 帧驱动是 `frame_waker` + rAF 的闭环：失效 → invalidator → frame_waker（持 Weak 防环）→ 排 rAF → rAF 闭包（先清 raf_id 防吞唤醒）→ `on_request_frame` 回调 → draw/present；`WebWindow` **不覆写** `schedule_frame`（契约默认空实现即「平台自有帧驱动」），呈现内嵌在 `WgpuRenderer::draw` 末尾的 `frame.present()`，与 Wayland 的停泊唤醒、Windows 的 vsync 心跳形成三方对照。
- `WebEventListeners` 是 DOM 事件到 `PlatformInput` 的翻译层：`EventListenerHandle` 三元组（目标+事件名+闭包）保证 Drop 时先反注册；指针类事件挂 canvas、键盘/粘贴/IME 挂隐藏 input；keydown 是三段式（修饰键变化 → 按键分发 → 文本插入），`prevent_default` 的时机由「GPUI 是否消化」与「是否产生文本」共同决定。
- 多处行为是浏览器独有的：wheel 必须 non-passive 注册才能 `prevent_default`；`set_pointer_capture` 保证拖出画布仍收 move/up；多击计数靠 `ClickState`（400ms/5px）手工合成；拖放只有 File 没有路径、只拦截不翻译成 `FileDrop`；粘贴走 DOM 事件（`clipboardData` 同步、File 句柄必须当场收集）；激活态 = input 焦点 + 标签页可见性。
- `WebDisplay` 是一台假显示器：`bounds` 报 `screen`、`visible_bounds` 报视口、`default_bounds` 取 75% 居中、读不到就回退 1920×1080；`id` 固定为 1、`uuid` 每次随机，完全不具备桌面 uuid 的持久身份能力。

## 7. 下一步学习建议

- 下一讲 **u7-l3「hello_web 实战」**会把本讲所有「待本地验证」的观察变成可操作的现实：用 trunk 构建 hello_web、在浏览器里实地查看 canvas 与隐藏 input、观察 dpr 缩放与事件行为，并精读素数计数的前后台协作。
- 回顾依赖：**u3-l2** 的 `PlatformWindow` 方法分组与 `schedule_frame` 语义是本讲第 4.3 节的契约基础；**u5-l4** 的 FrameLoop 八态状态机是帧驱动对照表的另一端；**u2-l4** 的异步剪贴板讨论解释了 paste 为什么必须走 DOM 事件。
- 源码层面建议把 [crates/gpui_web/src/events.rs](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/events.rs) 底部的纯函数区（`dom_key_to_gpui_key`、`keystroke_inserts_text`、`compute_key_char`）与 u3-l3 的 macOS/Linux 键盘映射实现并排对照——同一份 `Keystroke` 语义（key 布局无关、key_char 布局相关）在三个平台上的三种原料来源，是检验你对键盘抽象理解深度的最佳练习。

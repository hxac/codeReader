# WebWindow 的诞生：canvas、隐藏输入框与窗口结构

## 1. 本讲目标

上一讲我们看清了 `WebPlatform` 如何初始化图形后端，并把一块「预备好的 canvas + surface」存进 `prepared_window` 槽位。本讲沿着这条链路再走一步，回答一个问题：

> **一个 GPUI 窗口在浏览器里到底是由哪些 DOM 元素组成的？它们是怎么被创建出来的？**

学完本讲，你应该能够：

1. 解释 `open_window` 如何把 canvas 与 surface 的「资产」移交给 `WebWindow::new`，以及失败时的清理路径。
2. 逐条说出 `prepare_canvas` 注入的 5 条内联 CSS（`width`/`height` 100%、`display: block`、`outline: none`、`touch-action: none`）各自解决什么问题。
3. 说明为什么一个 1px 大小、完全透明的 `<input>` 元素是浏览器上接收键盘、IME 输入的**关键设计**，以及 `set_tab_index(-1)` 与 `focus()` 如何配合。
4. 读懂 `WebWindowInner` 用 `RefCell`/`Cell` 拆分可变状态的原因、`Rc` 共享所有权的结构，以及 `raw_window_handle` 的 Web canvas 句柄实现。

## 2. 前置知识

### 2.1 DOM 与 canvas

- **DOM（Document Object Model）**：浏览器把 HTML 页面表示成一棵元素树，JavaScript（以及 wasm）可以通过 `document.create_element` 动态创建新元素、用 `append_child` 挂到树上。元素不必写在 HTML 里——**运行时创建的元素和写死的元素地位完全相同**。gpui_web 的 canvas 和 input 就全是运行时创建的。
- **canvas**：一块「位图画布」，应用可以用 WebGPU/WebGL 往上面画像素。它自己**不是**可编辑元素。
- **内联样式（inline style）**：直接写在元素 `style` 属性上的 CSS，优先级高于样式表。`prepare_canvas` 用 `style.set_property(...)` 逐条设置。

### 2.2 焦点（focus）与 tabindex

- 浏览器同一时刻只有一个元素拥有**键盘焦点**（`document.activeElement`），键盘事件默认派发给它。
- 默认情况下 `<canvas>` **不可获得焦点**；设置 `tabindex` 后变得可聚焦：
  - `tabindex="0"`：可聚焦，且**加入 Tab 键遍历序列**；
  - `tabindex="-1"`：可聚焦（可通过点击或 JS `focus()`），但**不在 Tab 序列里**。
- 浏览器只把**文本输入、输入法组合（composition）、paste** 这类「编辑类事件」派发给可编辑元素（`<input>`、`<textarea>`、`contenteditable`）。canvas 永远收不到 `compositionstart`。这是本讲最重要的浏览器背景知识。

### 2.3 Rust 的内部可变性：Cell 与 RefCell

GPUI 桌面平台上，窗口代码大多在「主线程独占」环境里跑，可以直接拿 `&mut self`。但浏览器回调是从 JS 世界「呼进来」的：一个 `Closure` 闭包捕获的是 `Rc<WebWindowInner>`，在 `FnMut` 闭包里只能拿到共享引用 `&WebWindowInner`。想在共享引用背后修改状态，只能用内部可变性容器：

- **`Cell<T>`**：适合 `Copy` 的简单值（`bool`、`Option<MouseButton>`、`u32` 元组），读写无借用检查，开销最小。
- **`RefCell<T>`**：适合复杂结构体，运行时借用检查——同一时刻允许多个 `borrow()` 或一个 `borrow_mut()`，违反就 panic。

### 2.4 raw window handle

`raw-window-handle` 是 Rust 图形生态的通用约定：窗口系统提供一个跨平台的「原生窗口句柄」抽象，wgpu 等库靠它拿到画布而不关心你用的是 Win32 窗口、X11 窗口还是 HTML canvas。它在 0.6 版本后专门为 Web 增加了 `WebCanvasWindowHandle`——句柄就是指向 canvas 的 JS 值指针。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| `src/platform.rs` | `WebPlatform`，实现 `Platform` trait | `open_window`（调用方）：资产移交与状态机 |
| `src/window.rs` | `WebWindow`，实现 `PlatformWindow` trait | `prepare_canvas`、`WebWindow::new`、`WebWindowInner` 结构、`raw_window_handle` 实现 |
| `src/events.rs` | DOM 事件 → `PlatformInput` | 仅看事件**注册在哪**（canvas 还是 input），字段映射留到 u2-l5/u2-l6 |
| `examples/hello_web/index.html` | 示例页面 | 预置的 canvas 样式，与内联样式互为印证 |
| `../gpui/src/platform.rs` | gpui 定义的契约 | `PlatformWindow` trait 要求实现句柄 trait |

永久链接统一使用当前 HEAD `2936989f1b7a15aaf7131b0a3c17961d706fdbf5`。

## 4. 核心概念与源码讲解

### 4.1 资产移交：open_window 如何把 canvas 与 surface 交给 WebWindow

#### 4.1.1 概念说明

上一讲（u2-l1）已经建立了这个事实：**canvas 不是 `WebWindow` 自己创建的**。`Platform::run` 启动时，`initialize_graphics` 就提前调用了 `WebWindow::prepare_canvas` 造出 canvas、绑定好 wgpu surface，把 `(canvas, surface)` 存进 `WebPlatform.prepared_window` 槽位。为什么这么设计？因为图形初始化是**异步**的（WebGPU 要等 adapter），而 `open_window` 是同步函数——只能把异步部分提前做完，等应用在 `run` 回调里调用 `open_window` 时，资产已经就绪，直接「验收移交」。

所以 `open_window` 本质上是一个**门卫**：它自己不创建任何 DOM 元素，只做三件事——校验窗口类型、校验生命周期状态、把预备资产移交给 `WebWindow::new` 并登记状态。

#### 4.1.2 核心流程

`open_window` 的完整判定流程可以画成四道关卡：

```text
open_window(handle, params)
│
├─ 关卡 1：窗口类型（params.kind）
│    Normal            → 放行
│    AnchoredPopup     → Err(PopupNotSupportedError)
│    PopUp / Floating / Dialog → Err(UnsupportedWindowKind)
│
├─ 关卡 2：生命周期状态机（window_lifecycle）
│    Open      → Err(AlreadyOpen)          # 已有唯一顶层窗口
│    Closed    → Err(ReopeningUnsupported) # 关了不能再开
│    Unavailable → Err(GraphicsUnavailable)
│    Available → 放行
│
├─ 关卡 3：资产验收
│    wgpu_context 为空      → Err(GraphicsInitializationPending)
│    prepared_window.take() → 拿走 (canvas, surface)
│
└─ 关卡 4：WebWindow::new(...)
     成功 → lifecycle = Open；active_window = Some(handle)；返回 Box<dyn PlatformWindow>
     失败 → canvas.remove()（清理 DOM）；lifecycle = Unavailable；返回 Err
```

注意 `take()` 的语义：`PreparedWebWindow` 被**拿走**后槽位为空，第二次调用 `open_window` 即使绕过状态机也拿不到 canvas——上一讲说的「从数据结构上封死第二块顶层窗口」正是在这里落地。

#### 4.1.3 源码精读

先看承载资产的两个类型，位于 platform.rs 顶部：

[crates/gpui_web/src/platform.rs:55-58](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L55-L58)

```rust
struct PreparedWebWindow {
    canvas: web_sys::HtmlCanvasElement,
    surface: wgpu::Surface<'static>,
}
```

canvas 与 surface **成对**存放——它们必须来自同一次图形初始化，拆开就作废。

然后是 `open_window` 主体：

[crates/gpui_web/src/platform.rs:332-370](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L332-L370)

```rust
fn open_window(
    &self,
    handle: AnyWindowHandle,
    params: WindowParams,
) -> anyhow::Result<Box<dyn PlatformWindow>> {
    match &params.kind {
        WindowKind::Normal => {}
        WindowKind::AnchoredPopup(_) => return Err(PopupNotSupportedError.into()),
        WindowKind::PopUp => { /* UnsupportedWindowKind */ }
        WindowKind::Floating => { /* UnsupportedWindowKind */ }
        WindowKind::Dialog => { /* UnsupportedWindowKind */ }
    }
    // ……生命周期校验……
    let prepared_window = self
        .prepared_window
        .borrow_mut()
        .take()
        .ok_or(WebWindowError::GraphicsInitializationPending)?;
```

第一段按 `WindowKind` 拦截：浏览器只有一个页面视口，弹窗、浮动窗口、对话框**都不是独立顶层窗口**，GPUI 应用应把它们画在普通窗口内部。第二段按 `WebWindowLifecycle` 拦截后，`borrow_mut().take()` 把资产一次性取出。

成功与失败的两条收尾路径：

[crates/gpui_web/src/platform.rs:374-396](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L374-L396)

```rust
let window = WebWindow::new(
    handle, params, context, canvas,
    prepared_window.surface,
    self.browser_window.clone(),
    self.window_lifecycle.clone(),
    self.active_window.clone(),
);
match window {
    Ok(window) => {
        self.window_lifecycle.set(WebWindowLifecycle::Open);
        *self.active_window.borrow_mut() = Some(handle);
        Ok(Box::new(window))
    }
    Err(error) => {
        let canvas: &web_sys::Element = canvas_for_cleanup.as_ref();
        canvas.remove();
        self.window_lifecycle.set(WebWindowLifecycle::Unavailable);
        Err(error)
    }
}
```

两个细节值得注意：

1. 构造前先克隆了 `canvas_for_cleanup`，因为 `canvas` 会被 move 进 `WebWindow::new`；一旦构造失败，窗口结构体不存在了，但 DOM 里的 canvas 还挂着，必须手动 `remove()` 兜底。
2. `lifecycle` 和 `active_window` 是 `Rc` 共享的（见 4.4 节），`WebWindow` 的 `Drop` 也会写它们（设 `Closed`、清 `active_window`）——**平台与窗口两侧共同维护状态机**。

#### 4.1.4 代码实践

**实践目标**：把 `open_window` 的判定逻辑整理成可查阅的表格，验证上一讲的状态机描述。

**操作步骤**：

1. 打开 [crates/gpui_web/src/platform.rs:332-397](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L332-L397)，通读 `open_window` 全文。
2. 自行填写下面两张空表（答案见 4.1.5）：

| `params.kind` | 结果 |
| --- | --- |
| `Normal` | ？ |
| `AnchoredPopup` | ？ |
| `PopUp` / `Floating` / `Dialog` | ？ |

| 进入时的 `window_lifecycle` | 结果 |
| --- | --- |
| `Available` | ？ |
| `Open` | ？ |
| `Closed` | ？ |
| `Unavailable` | ？ |

3. 对照 [crates/gpui_web/src/platform.rs:68-79](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L68-L79) 的 `WebWindowError` 定义，确认每个错误变体对应的用户可读文案。

**需要观察的现象**：纯源码阅读型实践，无需运行。

**预期结果**：两张表与 4.1.5 的答案一致；并且能指出五个错误变体中只有 `GraphicsInitializationPending` 的文档注释声明「稍后重试可以成功」。

#### 4.1.5 小练习与答案

**练习 1**：如果应用在 `Platform::run` 回调**之前**就调用了 `open_window`（比如直接在 `main` 里同步调用），会在哪一关卡住、返回什么错误？

**答案**：卡在关卡 3。此时 `wgpu_context` 还是 `None`（或 `prepared_window` 是 `None`），`ok_or(WebWindowError::GraphicsInitializationPending)` 返回 `GraphicsInitializationPending` 错误。它的文档注释明确说：在 `Platform::run` 回调里重试可以成功——这正是 u2-l1 强调「开窗必须写在 run 回调内」的原因。

**练习 2**：`WebWindow::new` 构造失败后，为什么必须调用 `canvas.remove()`？`WebWindow` 的 `Drop` 不是也会移除 canvas 吗？

**答案**：因为构造**失败**意味着 `WebWindow` 结构体从未诞生，它的 `Drop` 永远不会执行——Drop 只对成功构造的对象生效。canvas 已经被 append 到 `body` 上了，不清理就会留下一块黑色空白画布，所以失败分支要自己 `remove()`。

**练习 3**：`PreparedWebWindow` 为什么要把 canvas 和 surface 放在同一个结构体里，而不是分成两个槽位？

**答案**：canvas 和 surface 必须成对——surface 是绑定在特定 canvas 上的图形输出目标（且一个 canvas 只能绑定一种上下文类型，WebGPU 与 WebGL2 互斥）。拆成两个槽位就可能出现「canvas 来自 WebGPU 尝试、surface 来自 WebGL2 重试」的错配。成对存放 + 单槽位 `take()`，在数据结构层面保证了原子移交和「只能开一次」。

---

### 4.2 prepare_canvas：动态创建铺满视口的 canvas 与五条内联样式

#### 4.2.1 概念说明

`prepare_canvas` 是整个 crate 里「把 GPUI 窗口锚定到页面」的第一颗钉子：它创建 canvas、注入样式、挂到 `<body>` 上。示例的 `index.html` 里 `<body>` 是空的——**页面上看到的一切都从这颗钉子开始**。

它被设计成一个独立的 `pub(crate)` 关联函数，而不是 `WebWindow::new` 的一部分，原因是上一讲讲过的降级策略：`initialize_graphics` 的 Auto 模式先造 canvas 试 WebGPU，失败后 `canvas.remove()` 掉，**再调一次 `prepare_canvas`** 造新 canvas 试 WebGL2。把「造 canvas」抽出为无状态的独立步骤，重试才干净。

#### 4.2.2 核心流程

```text
prepare_canvas(browser_window)
│
├─ 1. browser_window.document()          → 拿到 document
├─ 2. document.create_element("canvas")  → 创建元素（此时还在内存里，不在页面上）
│      └─ dyn_into::<HtmlCanvasElement>() → 向下转型为具体类型
├─ 3. canvas.set_tab_index(-1)           → 可聚焦但不进 Tab 序列（见 4.3 节）
├─ 4. 注入 5 条内联样式（见下表）
└─ 5. document.body().append_child(&canvas) → 挂到 DOM 树，开始参与布局
```

五条样式各自解决的问题：

| 样式 | 解决的问题 |
| --- | --- |
| `width: 100%` / `height: 100%` | canvas 铺满父容器（`body`）。窗口尺寸 = canvas 尺寸，这是「整个视口即窗口」的基础 |
| `display: block` | canvas 默认是 inline 元素，行内排列会在底部留一条几像素的「基线空隙」（行高为文字 descender 预留），导致页面出现意外滚动条；块级化后消除 |
| `outline: none` | canvas 被设为可聚焦（tabindex=-1），点击它触发聚焦时浏览器会画默认焦点圈；这条样式把圈抹掉，避免绘制区出现蓝色描边 |
| `touch-action: none` | 禁用浏览器对触摸手势的默认处理（滚动、双指缩放），把完整手势交给应用的 pointer 事件；否则拖动页面会同时滚动视口 |

#### 4.2.3 源码精读

完整实现：

[crates/gpui_web/src/window.rs:77-109](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L77-L109)

```rust
pub(crate) fn prepare_canvas(
    browser_window: &web_sys::Window,
) -> anyhow::Result<web_sys::HtmlCanvasElement> {
    let document = browser_window
        .document()
        .ok_or_else(|| anyhow::anyhow!("No `document` found on window"))?;
    let canvas: web_sys::HtmlCanvasElement = document
        .create_element("canvas")
        .map_err(|error| anyhow::anyhow!("Failed to create canvas element: {error:?}"))?
        .dyn_into()
        .map_err(|error| anyhow::anyhow!("Created element is not a canvas: {error:?}"))?;
    canvas.set_tab_index(-1);

    let style = canvas.style();
    for (property, value) in [
        ("width", "100%"),
        ("height", "100%"),
        ("display", "block"),
        ("outline", "none"),
        ("touch-action", "none"),
    ] {
        style.set_property(property, value).map_err(|error| {
            anyhow::anyhow!("Failed to set canvas {property} style: {error:?}")
        })?;
    }

    let body = document
        .body()
        .ok_or_else(|| anyhow::anyhow!("No `body` found on document"))?;
    body.append_child(&canvas)
        .map_err(|error| anyhow::anyhow!("Failed to append canvas to body: {error:?}"))?;
    Ok(canvas)
}
```

几个 wasm-bindgen 特有的写法值得停下来看：

- `dyn_into()`：`create_element` 返回泛型 `Element`，需要向下转型成 `HtmlCanvasElement` 才能调用 canvas 专属方法（比如后面 ResizeObserver 要观察它）。转型失败被映射为 `anyhow` 错误而不是 panic。
- `set_property` 每次都可能失败（理论上样式表 API 抛异常），所以用 `for` 循环 + `map_err` 统一处理，错误信息里带属性名，方便定位是哪条样式没设上。
- 样式用**数组 + 循环**写而不是五行重复代码——新增样式只改数组。

对照示例页面可以发现有趣的重复——`index.html` 的样式表里**预置了几乎同一组 canvas 规则**：

[crates/gpui_web/examples/hello_web/index.html:19-27](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/examples/hello_web/index.html#L19-L27)

```css
canvas {
    display: block;
    width: 100%;
    height: 100%;
    touch-action: none;
    outline: none;
    -webkit-user-select: none;
    user-select: none;
}
```

两处是互为保障的关系：内联样式保证 crate 在**任何宿主页面**（哪怕样式表写错）都能正常工作；页面样式表则是提前声明意图（并补充了 `user-select: none` 这条 crate 没设的规则）。内联样式优先级更高，最终以它为准。

另外，canvas 的尺寸并非只在这里设定。`PlatformWindow::resize` 会通过改写同一对样式属性来调整窗口：

[crates/gpui_web/src/window.rs:618-626](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L618-L626)

```rust
fn resize(&mut self, size: Size<Pixels>) {
    let style = self.inner.canvas.style();
    style
        .set_property("width", &format!("{}px", f32::from(size.width)))
        .ok();
    // ……height 同理
}
```

#### 4.2.4 代码实践

**实践目标**：验证「canvas 是运行时动态创建的、样式来自源码里的那张表」。

**操作步骤**：

1. 按 u1-l2 的方式启动示例（在 `examples/hello_web` 目录执行 `trunk serve`），打开浏览器。
2. 打开 DevTools → Elements 面板，展开 `<body>`，找到那个 canvas，检查它的 `style` 属性和 `tabindex` 属性。
3. 修改源码：编辑 [crates/gpui_web/src/window.rs:91-97](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L91-L97) 的样式数组，在末尾追加一行 `("border", "8px solid red")`（示例代码，仅用于实验）。
4. 保存后等待 trunk 重新编译、页面自动刷新。

**需要观察的现象**：

- 修改前：DevTools 里 `<body>` 下有一个 canvas，内联样式恰好是源码那五条；`tabindex="-1"`。
- 修改后：页面四周出现一圈 8px 红色边框，DevTools 中 canvas 的内联样式多出 `border: 8px solid red;`。

**预期结果**：边框出现，证明样式表完全由 Rust 代码中的数组驱动。进一步思考：边框画在 canvas 的**外沿**（`box-sizing: border-box` 时会挤压绘制区），而 GPUI 绘制的界面仍铺满 canvas 的内容区——说明「窗口内容」只认 canvas 元素本身，与页面其他视觉元素无关。改完记得把实验用的边框行删掉还原。

#### 4.2.5 小练习与答案

**练习 1**：把 `("touch-action", "none")` 从数组里删掉，在手机或浏览器移动端模拟模式下用单指拖动画布，预期会发生什么？

**答案**：浏览器恢复对触摸的默认处理，单指拖动会**滚动页面**（虽然 canvas 铺满视口时页面本无可滚动，但手势会被浏览器接管、触发橡皮筋回弹），同时 pointer 事件流被默认行为打断。`touch-action: none` 的作用就是声明「该元素上的触摸手势全部由应用自行处理」。（具体表现随浏览器而异，待本地验证。）

**练习 2**：为什么 `prepare_canvas` 是 `pub(crate)` 的关联函数（挂在 `WebWindow` 上），却由 platform.rs 的 `initialize_graphics` 调用，而不是由 `WebWindow::new` 自己调用？

**答案**：canvas 的创建时机必须**早于**窗口对象：图形后端初始化（异步、要试探 WebGPU/WebGL2）需要拿 canvas 去创建 surface，而这一步发生在 `Platform::run` 里、`open_window` 被调用之前。把 `prepare_canvas` 独立出来还让 Auto 降级可以「造 canvas → 试 WebGPU → 失败删掉 → 重造 canvas → 试 WebGL2」地循环使用。若放在 `new` 里，surface 就得在构造函数内异步创建，与同步的构造签名冲突。

**练习 3**：canvas 挂载后尺寸是 `100%`，但 `WebWindow::new` 里创建 renderer 时为什么传的是 `Size { width: DevicePixels(0), height: DevicePixels(0) }`？

**答案**：因为此刻还**不知道** canvas 的实际像素尺寸。布局是异步完成的，真实尺寸要等 `ResizeObserver` 回调送来（u2-l4 精读），再经 `pending_physical_size` 在绘制时应用（u2-l3 精读）。所以初始 renderer 以 0×0 起步，尺寸是「随后补上」的。相关代码见 [crates/gpui_web/src/window.rs:131-139](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L131-L139)。

---

### 4.3 隐藏 input：承接键盘、IME 与焦点的关键设计

#### 4.3.1 概念说明

这是整个 gpui_web 最「非常规」也最精妙的设计。回顾 2.2 节的浏览器铁律：

> 文本输入、composition（IME 组合输入）、paste 只派发给可编辑元素；canvas 收不到。

GPUI 是一个完整的编辑器 UI 框架，必须有键盘输入和输入法。怎么办？**在页面里常驻一个「假」输入框**：1 像素见方、完全不透明度为 0、钉在视口左上角，肉眼不可见，但它是真实的 `<input>`，**持有键盘焦点**，于是所有键盘类事件都以它为目标元素派发——gpui_web 把监听器挂在它身上，把 DOM 事件翻译成 GPUI 输入（翻译细节在 u2-l5/u2-l6）。

这个设计的收益是「零成本拿到系统级能力」：

- **IME 组合输入**（中文拼音、日文假名等）走 input 元素的原生 composition 流程，浏览器负责显示候选窗、上屏，gpui_web 只需监听 `compositionstart/update/end`；
- **粘贴**走原生 `paste` 事件（连图片粘贴都能拿到 blob）；
- **焦点即激活**：input 的 `focus`/`blur` 天然表示「窗口是否拥有键盘焦点」，直接映射 GPUI 的 `active_status_change` 回调。

#### 4.3.2 核心流程

隐藏 input 的生命周期与事件分工：

```text
WebWindow::new
│
├─ create_element("input") + dyn_into
├─ 注入样式：position:fixed; top:0; left:0; width:1px; height:1px; opacity:0
├─ append_child 到 body
└─ input_element.focus()          ← 窗口诞生即获得键盘焦点

事件注册（register_event_listeners）：
├─ 挂在 input 上（listen_input）：
│    keydown / keyup               → 键盘
│    compositionstart/update/end   → IME
│    paste                         → 粘贴
│    focus / blur                  → 窗口激活状态
└─ 挂在 canvas 上（listen）：
     pointerdown/up/move/enter/leave → 指针
     wheel                          → 滚轮
     contextmenu / dragover / drop  → 右键与拖放

焦点保持闭环：
用户点击 canvas → 浏览器把焦点移给 canvas（它有 tabindex=-1，可聚焦）
→ pointerdown 处理器立即调用 input_element.focus() → 焦点回到 input
```

#### 4.3.3 源码精读

创建隐藏 input 的完整代码在 `WebWindow::new` 中段：

[crates/gpui_web/src/window.rs:141-155](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L141-L155)

```rust
let input_element: web_sys::HtmlInputElement = document
    .create_element("input")
    .map_err(|e| anyhow::anyhow!("Failed to create input element: {e:?}"))?
    .dyn_into()
    .map_err(|e| anyhow::anyhow!("Created element is not an input: {e:?}"))?;
let input_style = input_element.style();
input_style.set_property("position", "fixed").ok();
input_style.set_property("top", "0").ok();
input_style.set_property("left", "0").ok();
input_style.set_property("width", "1px").ok();
input_style.set_property("height", "1px").ok();
input_style.set_property("opacity", "0").ok();
body.append_child(&input_element)
    .map_err(|e| anyhow::anyhow!("Failed to append input to body: {e:?}"))?;
input_element.focus().ok();
```

样式逐条拆解：`position: fixed; top: 0; left: 0` 让它脱离文档流钉在视口角落，**不占据、也不影响任何布局**；`width/height: 1px`——注意**不能是 0**，部分浏览器会忽略对零尺寸或不可见输入框的焦点与输入处理，1px 是保守下限；`opacity: 0` 完全透明（比 `display: none` 或 `visibility: hidden` 安全，那两种元素根本不可聚焦）。最后 `focus().ok()` 让窗口一出生就持有键盘焦点，用户立刻可以打字。

注意这里的错误处理风格与 `prepare_canvas` 不同：样式设置用 `.ok()` 忽略（样式失败不致命，input 仍然工作，只是可能被看见），而元素创建与挂载用 `?` 传播（没有 input 就没有键盘，属于致命错误）。这是「失败是否伤害核心功能」的取舍。

事件注册的分派器在 events.rs，两条通道一目了然：

[crates/gpui_web/src/events.rs:148-162](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/events.rs#L148-L162)

```rust
fn listen(/* …… */) -> EventListenerHandle {
    EventListenerHandle::add(self.canvas.as_ref(), event_name, handler)
}

fn listen_input(/* …… */) -> EventListenerHandle {
    EventListenerHandle::add(self.input_element.as_ref(), event_name, handler)
}
```

注册清单与目标元素：

[crates/gpui_web/src/events.rs:121-146](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/events.rs#L121-L146)

```rust
pub fn register_event_listeners(self: &Rc<Self>) -> WebEventListeners {
    let mut handles = vec![
        self.register_pointer_down(),   // canvas
        self.register_pointer_up(),     // canvas
        self.register_pointer_move(),   // canvas
        self.register_pointer_leave(),  // canvas
        self.register_wheel(),          // canvas（non-passive）
        self.register_context_menu(),   // canvas
        self.register_dragover(),       // canvas
        self.register_drop(),           // canvas
        self.register_key_down(),       // input
        self.register_key_up(),         // input
        self.register_paste(),          // input
        self.register_composition_start(),  // input
        self.register_composition_update(), // input
        self.register_composition_end(),    // input
        self.register_focus(),          // input
        self.register_blur(),           // input
        self.register_pointer_enter(),  // canvas
    ];
    // ……document 级：visibilitychange / fullscreenchange、媒体查询级：外观变化
}
```

（每个注册函数挂在哪个元素上，可用 Grep 搜 `listen_input(` 逐一核对：keydown/keyup/paste/composition*/focus/blur 全部走 input 通道。）

「点击画布后把焦点还给 input」的闭环在 pointerdown 处理器里：

[crates/gpui_web/src/events.rs:187-198](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/events.rs#L187-L198)

```rust
fn register_pointer_down(self: &Rc<Self>) -> EventListenerHandle {
    let this = Rc::clone(self);
    self.listen("pointerdown", move |event: JsValue| {
        // ……
        this.input_element.focus().ok();
        // ……
        // Capture the pointer so drags that leave the canvas keep
        // delivering events; without it the pointerup outside the canvas
        // is never seen and `pressed_button` stays stuck.
        this.canvas.set_pointer_capture(event.pointer_id()).ok();
        // ……
```

focus 与 blur 直接映射「窗口激活状态」：

[crates/gpui_web/src/events.rs:586-612](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/events.rs#L586-L612)

```rust
fn register_focus(self: &Rc<Self>) -> EventListenerHandle {
    let this = Rc::clone(self);
    self.listen_input("focus", move |_event: JsValue| {
        {
            let mut state = this.state.borrow_mut();
            state.is_active = true;
        }
        this.with_callback(
            |callbacks| &mut callbacks.active_status_change,
            |callback| callback(true),
        );
    })
}
// register_blur 对称地设 false
```

#### 4.3.4 代码实践

**实践目标**：亲眼确认隐藏 input 的存在、它的焦点地位，以及「点击 canvas 后焦点回归」的闭环。

**操作步骤**：

1. 启动示例，打开 DevTools → Elements，展开 `<body>`：应该能看到 `canvas` 之后跟着一个 `input`。选中它，在右侧 Styles/Computed 面板核对六条内联样式。
2. 在 Console 里执行 `document.activeElement`，记下结果（页面加载后、不点击任何东西时）。
3. 点击 DevTools 外的页面任意位置（canvas 区域），再回到 Console 执行 `document.activeElement`。
4. 点击浏览器地址栏（让页面失焦），再执行 `document.activeElement`。

**需要观察的现象**：

1. `<body>` 子元素顺序为 `canvas`、`input`（`position: fixed; opacity: 0`）。
2. 初始 `activeElement` 是那个 `input`（窗口出生时的 `focus()` 生效）。
3. 点击 canvas 后 `activeElement` **仍是 input**——因为 canvas 的 `tabindex="-1"` 使其可被点击聚焦，但 pointerdown 处理器立刻把焦点抢回 input。
4. 点击地址栏后 `activeElement` 变为 `<body>`——input 失焦，对应 GPUI 的 `blur` → 窗口失活。

**预期结果**：与上述四点一致。若第 3 步在极快的检查时机下短暂观察到 canvas 聚焦，也属正常（焦点先到 canvas 再被处理器交还 input）——这正好印证了 `set_tab_index(-1)` 与 `input_element.focus()` 的配合关系。

#### 4.3.5 小练习与答案

**练习 1**：把 `prepare_canvas` 里的 `canvas.set_tab_index(-1)` 改成 `set_tab_index(0)`，按 Tab 键会发生什么？为什么这会破坏键盘输入？

**答案**：`tabindex=0` 把 canvas 纳入 Tab 遍历序列。按 Tab 时焦点会从 input **移动到 canvas**——而 keydown 监听器只挂在 input 上，canvas 聚焦后按键事件派发给 canvas、无人接收，GPUI 的键盘输入随之失灵（除非再点一次画布触发 pointerdown 抢回焦点）。`-1` 的意义正在于此：canvas 可以被点击聚焦（浏览器需要焦点元素参与指针交互），但**永远不会被 Tab 选中**，焦点序列里只有那个 input。（行为预测，待本地验证。）

**练习 2**：为什么隐藏 input 用 `opacity: 0` + `1px`，而不是 `display: none` 或 `width: 0`？

**答案**：`display: none`（以及 `visibility: hidden`）的元素**不可聚焦、不接收输入事件**，等于自废武功。尺寸设为 0 在部分浏览器上同样会导致元素被布局系统忽略或拒绝聚焦/输入。1px + 透明是「对用户不可见、对浏览器仍然真实存在」的最稳组合，`position: fixed` 再确保它不干扰布局、不产生滚动条。

**练习 3**：既然焦点保持在 input 上，canvas 为什么还需要 `outline: none`？

**答案**：canvas 有 `tabindex=-1`，是**可聚焦**元素。用户点击 canvas 的瞬间，浏览器先把焦点给 canvas（随后才被 pointerdown 处理器交还 input）；可聚焦元素获得焦点时浏览器默认绘制焦点圈。`outline: none` 保证这转瞬即逝（乃至持续）的聚焦不产生蓝色描边，避免污染 GPUI 自己绘制的界面。两条设置是配套的。

---

### 4.4 WebWindowInner 与 WebWindow：内部可变性、Rc 所有权与 raw_window_handle

#### 4.4.1 概念说明

窗口状态被拆成**两层结构、三个角色**：

- **`WebWindow`（外壳）**：实现 `PlatformWindow` trait 的公有类型，被 GPUI 核心持有为 `Box<dyn PlatformWindow>`。它的字段大多以下划线开头（`_raf_closure`、`_resize_observer`、`_event_listeners`……）——这些是**保活句柄**：wasm-bindgen 的 `Closure` 一旦没有任何 Rust 侧引用，就会被释放，而 JS 侧已注册的回调再被触发就会抛 "closure invoked after being dropped"。外壳持有它们 = 声明「窗口活着期间这些回调必须活着」。
- **`WebWindowInner`（内核）**：被 `Rc` 共享的真正状态体。所有 DOM 回调闭包都捕获 `Rc<WebWindowInner>`，从 JS 呼进来时只有共享引用，所以可变状态全靠 `RefCell`/`Cell` 包装。
- **回调与平台共享的 `Rc`**：`lifecycle: Rc<Cell<WebWindowLifecycle>>`、`active_window: Rc<RefCell<Option<AnyWindowHandle>>>` 由平台与窗口两侧共同持有，让 `Drop` 也能更新平台状态。

为什么可变状态要区分 `Cell` 与 `RefCell`？——`Cell` 用于 `Copy` 的简单标量（`bool`、`Option<MouseButton>`、尺寸元组），没有运行时借用检查、最便宜；`RefCell` 用于需要整体借用的复杂结构（`WebWindowMutableState`、`WebWindowCallbacks`、`ClickState`）。

#### 4.4.2 核心流程

`WebWindow::new` 的组装顺序（先后有依赖关系）：

```text
WebWindow::new(handle, params, context, canvas, surface, browser_window, lifecycle, active_window)
│
├─ 1. 读取环境：dpr = device_pixel_ratio；max_texture_dimension = 设备上限
├─ 2. check_device_pixel_support()      → 探测 Safari 是否支持 devicePixelContentBoxSize
├─ 3. WgpuRenderer::new_from_surface(0×0) → 渲染器（尺寸随后补）
├─ 4. 创建隐藏 input 并 focus()          → 4.3 节
├─ 5. WebDisplay::new                    → 显示器抽象
├─ 6. 组装 WebWindowMutableState（bounds/scale_factor/title/input_handler/……）
├─ 7. Rc::new(WebWindowInner { …… })     → 内核诞生
├─ 8. inner.create_raf_closure() + wake_frame_loop()  → 帧循环起搏（u2-l3）
├─ 9. ResizeObserver 创建 + observe_canvas + watch_dpr_changes（u2-l4）
├─ 10. inner.register_event_listeners()  → 17 个 DOM 监听（u2-l5/u2-l6）
└─ 11. 返回 Self { inner, display, lifecycle, active_window, _raf_closure,
                    _resize_observer, _resize_observer_closure, _event_listeners }
```

第 8~10 步全都**只需要 `Rc<WebWindowInner>`**（各闭包克隆一份 `Rc` 捕获），不需要 `&mut WebWindow`——这正是两层结构的动机：外壳还没构造完，内核已经开始向 JS 世界注册回调了。

#### 4.4.3 源码精读

先看三个状态体的定义：

[crates/gpui_web/src/window.rs:31-44](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L31-L44)

```rust
pub(crate) struct WebWindowMutableState {
    pub(crate) renderer: WgpuRenderer,
    pub(crate) bounds: Bounds<Pixels>,
    pub(crate) scale_factor: f32,
    pub(crate) max_texture_dimension: u32,
    pub(crate) title: String,
    pub(crate) input_handler: Option<PlatformInputHandler>,
    pub(crate) is_fullscreen: bool,
    pub(crate) is_active: bool,
    pub(crate) is_hovered: bool,
    pub(crate) mouse_position: Point<Pixels>,
    pub(crate) modifiers: Modifiers,
    pub(crate) capslock: Capslock,
}
```

「一组经常一起读写的状态」合并进一个 `RefCell`，避免每项一个 `RefCell` 的碎片化。

[crates/gpui_web/src/window.rs:46-63](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L46-L63)

```rust
pub(crate) struct WebWindowInner {
    pub(crate) browser_window: web_sys::Window,
    pub(crate) canvas: web_sys::HtmlCanvasElement,
    pub(crate) input_element: web_sys::HtmlInputElement,
    pub(crate) has_device_pixel_support: bool,
    pub(crate) is_mac: bool,
    pub(crate) state: RefCell<WebWindowMutableState>,          // 复合状态
    pub(crate) callbacks: RefCell<WebWindowCallbacks>,          // GPUI 注册的回调
    pub(crate) click_state: RefCell<ClickState>,                // 双击检测
    pub(crate) pressed_button: Cell<Option<MouseButton>>,       // 简单值
    pub(crate) last_physical_size: Cell<(u32, u32)>,
    pub(crate) notify_scale: Cell<bool>,
    pub(crate) is_composing: Cell<bool>,
    mql_handle: RefCell<Option<MqlHandle>>,                     // 打破 Rc 环的把手
    pending_physical_size: Cell<Option<(u32, u32)>>,
    raf_id: Cell<Option<i32>>,
    raf_function: RefCell<Option<js_sys::Function>>,
}
```

前五个字段是**只读环境**（浏览器窗口、两个 DOM 元素、能力探测结果、平台判断），无需包装；其余按「简单值用 `Cell`、复杂体用 `RefCell`」的原则逐个包装。`mql_handle` 私有且特殊——它存放的闭包捕获了 `Rc<WebWindowInner>` 又被存在 inner 内部，构成引用环，`Drop` 时要显式 `take()` 断环（u3-l2 专题精读）。

外壳与「保活句柄」：

[crates/gpui_web/src/window.rs:65-74](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L65-L74)

```rust
pub struct WebWindow {
    inner: Rc<WebWindowInner>,
    display: Rc<dyn PlatformDisplay>,
    lifecycle: Rc<Cell<WebWindowLifecycle>>,
    active_window: Rc<RefCell<Option<AnyWindowHandle>>>,
    _raf_closure: Closure<dyn FnMut()>,
    _resize_observer: Option<web_sys::ResizeObserver>,
    _resize_observer_closure: Closure<dyn FnMut(js_sys::Array)>,
    _event_listeners: WebEventListeners,
}
```

内核初始化与回调注册的衔接：

[crates/gpui_web/src/window.rs:181-224](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L181-L224)

```rust
let inner = Rc::new(WebWindowInner { /* …… */ });

let raf_closure = inner.create_raf_closure();
inner.wake_frame_loop();

let resize_observer_closure = Self::create_resize_observer_closure(Rc::clone(&inner));
let resize_observer =
    web_sys::ResizeObserver::new(resize_observer_closure.as_ref().unchecked_ref()).ok();

if let Some(ref observer) = resize_observer {
    inner.observe_canvas(observer);
    inner.watch_dpr_changes(observer);
}

let event_listeners = inner.register_event_listeners();

Ok(Self {
    inner,
    display,
    lifecycle,
    active_window,
    _raf_closure: raf_closure,
    _resize_observer: resize_observer,
    _resize_observer_closure: resize_observer_closure,
    _event_listeners: event_listeners,
})
```

注意 `web_sys::ResizeObserver::new(...)` 的结果是 `.ok()`——ResizeObserver 构造失败（极老浏览器）时窗口仍可用，只是收不到尺寸变化，属于「优雅降级」而非致命错误。

最后是本讲的收尾知识点：raw window handle。gpui 的契约要求窗口实现两个句柄 trait：

[crates/gpui/src/platform.rs:816](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/platform.rs#L816)

```rust
pub trait PlatformWindow: HasWindowHandle + HasDisplayHandle {
```

Web 实现：

[crates/gpui_web/src/window.rs:582-599](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L582-L599)

```rust
impl raw_window_handle::HasWindowHandle for WebWindow {
    fn window_handle(
        &self,
    ) -> Result<raw_window_handle::WindowHandle<'_>, raw_window_handle::HandleError> {
        let canvas_ref: &JsValue = self.inner.canvas.as_ref();
        let obj = std::ptr::NonNull::from(canvas_ref).cast::<std::ffi::c_void>();
        let handle = raw_window_handle::WebCanvasWindowHandle::new(obj);
        Ok(unsafe { raw_window_handle::WindowHandle::borrow_raw(handle.into()) })
    }
}

impl raw_window_handle::HasDisplayHandle for WebWindow {
    fn display_handle(/* …… */) -> Result<raw_window_handle::DisplayHandle<'_>, raw_window_handle::HandleError> {
        Ok(raw_window_handle::DisplayHandle::web())
    }
}
```

`WebCanvasWindowHandle` 携带的是指向 canvas `JsValue` 的裸指针——Web 平台上「原生窗口句柄」就是 canvas 本身；`DisplayHandle::web()` 则表示 Web 没有独立的「显示器句柄」概念。有了这两个实现，任何基于 raw-window-handle 的图形库（不止 wgpu）都能把 gpui_web 的窗口当作画布目标，这正是该抽象存在的意义。

#### 4.4.4 代码实践

**实践目标**：通过「字段审计」吃透两层结构与所有权关系。

**操作步骤**：

1. 打开 [crates/gpui_web/src/window.rs:46-74](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L46-L74)，给 `WebWindowInner` 的每个字段填一张三列表：**字段 / 包装类型（无、`Cell`、`RefCell`）/ 谁会写它**（提示：用 Grep 搜字段名，例如 `raf_id` 出现在 `create_raf_closure`、`wake_frame_loop`、`Drop` 三处）。
2. 对 `WebWindow` 的四个下划线字段，各自回答：它保活的对象被注册到了哪里？（例如 `_raf_closure` 的 JS 函数存进了 `raf_function` 并交给 `request_animation_frame`。）
3. 运行示例，在 DevTools Console 执行 `document.querySelectorAll('canvas, input').length`，确认页面恰有一个 canvas 和一个 input——即一个 `WebWindow` 的全部 DOM 足迹。

**需要观察的现象**：审计表完成后能发现规律——`Cell` 字段全部是回调路径上的「单值标志/缓存」，`RefCell` 字段全部是需要跨回调整体借用的结构体。

**预期结果**：`document.querySelectorAll('canvas, input').length` 输出 `2`；`WebWindowInner` 约 12 个可变字段中 `Cell` 与 `RefCell` 的分工与 4.4.1 的结论一致。

#### 4.4.5 小练习与答案

**练习 1**：为什么 GPUI 核心持有的是 `Box<dyn PlatformWindow>`，而 DOM 回调闭包捕获的是 `Rc<WebWindowInner>`，而不是直接捕获 `WebWindow`？

**答案**：两个方向的需求不同。GPUI 核心需要**所有权**（窗口生命周期由它管理）且只需要 trait 方法 → `Box<dyn PlatformWindow>`。DOM 回调需要**共享且可能长期存活**的访问途径：同一个 inner 要被 rAF、ResizeObserver、17 个事件监听器等多个闭包同时引用，`Rc` 克隆最自然；而 `WebWindow` 没实现 `Clone`，也无法安全地被多个闭包分别持有。拆出 inner 后，闭包只依赖内核，外壳负责保活与 `Drop` 清理。

**练习 2**：`WebWindowCallbacks`（[crates/gpui_web/src/window.rs:17-29](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L17-L29)）里每个回调都是 `Option<Box<dyn FnMut(...)>>`。它们是谁注册的、什么时候被调用？

**答案**：由 GPUI 框架核心在把 `Box<dyn PlatformWindow>` 包装成高层 `Window` 时通过 `PlatformWindow` 的各种 `on_*` 方法注册（如 `on_request_frame`、`on_input`、`on_active_status_change`、`on_resize`）。gpui_web 在 DOM 事件到来时通过 `with_callback` 取出并调用，把平台事件「上报」给框架。`Option` 为空表示该回调未注册，调用侧直接跳过。

**练习 3**：`HasWindowHandle::window_handle` 里用了 `unsafe { WindowHandle::borrow_raw(...) }`。为什么这里需要 unsafe，而桌面平台的同类实现往往不需要？

**答案**：`borrow_raw` 把一个裸指针包装成带生命周期的 `WindowHandle<'_>`，承诺「句柄只在借用期内有效且底层指针有效」。这里指针来自 `&JsValue`（canvas 的引用），借用检查器无法验证 JS 侧对象存活周期与 wasm 内存的一致性，只能由实现者用 unsafe 承诺。桌面平台的句柄通常是操作系统整数句柄（HWND/X11 window），本身就是裸资源 ID，不存在「指向受管内存的引用」问题。这也是 raw-window-handle 把 `borrow_raw` 设计为 unsafe 的原因：**生命周期正确性由调用双方约定，而非类型系统保证**。

---

## 5. 综合实践

把本讲三个模块串成一个「DOM 全景 + 焦点机制」的验证任务。

**任务**：为 GPUI Web 窗口绘制一份「出生证明」。

1. **画出 DOM 结构图**。启动示例，对照源码（[window.rs:77-109](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L77-L109) 的 `prepare_canvas` 与 [window.rs:141-155](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L141-L155) 的 input 创建），在 DevTools Elements 面板核对后，画出类似下面的结构（补全每个元素的属性与内联样式）：

   ```text
   <html>
   └─ <body>
      ├─ <canvas tabindex="-1" style="width:100%; height:100%; display:block; …">   ← 绘制目标
      └─ <input style="position:fixed; top:0; left:0; width:1px; …">                ← 键盘/IME 焦点
   ```

2. **注释你的图**。在每个内联样式旁用一句话标注它解决的问题（参考 4.2.2 的表格，但用自己的话写）。
3. **做两个可逆实验并记录**：
   - 实验 A：给 `prepare_canvas` 的样式数组加一条 `("border", "8px solid red")`，重新编译，观察并截图；删除该行还原。
   - 实验 B：把 `canvas.set_tab_index(-1)` 注释掉，重新编译；在页面按几下 Tab、再点击画布打字，记录键盘输入是否受影响；恢复代码。
4. **写出机制解释**：用 5~8 句话解释 `set_tab_index(-1)` 与 `input_element.focus()`（创建时一次、pointerdown 时反复）的配合逻辑——要点包括：input 是键盘/IME/paste 的唯一合法目标；canvas 可聚焦但不进 Tab 序列；点击画布会短暂聚焦 canvas，pointerdown 处理器立即把焦点交还 input。

**验收标准**：DOM 图与 DevTools 实际一致；两个实验的现象记录完整（实验 B 的具体表现随浏览器而异，如实记录即可）；机制解释覆盖上述三个要点。

## 6. 本讲小结

- `open_window` 是**门卫**而非工厂：校验 `WindowKind` 与生命周期状态机后，用 `prepared_window.take()` 一次性移交给 `WebWindow::new`；构造失败会 `canvas.remove()` 并把状态机推入 `Unavailable`。
- canvas 由 `prepare_canvas` **运行时动态创建**，五条内联样式（100% 尺寸、block 化、去焦点圈、禁触摸默认行为）让它在任何宿主页面上都能铺满视口、独立成立；独立成函数是为了 WebGPU→WebGL2 降级时可以「删旧造新」。
- 一个 1px、透明、钉在角落的 `<input>` 是浏览器键盘输入的**唯一钥匙**：keydown/keyup、composition（IME）、paste、focus/blur 全部挂在它身上；`pointerdown` 时重新 `focus()` 保证焦点始终回归。
- `set_tab_index(-1)` 让 canvas 可点击聚焦但**不进 Tab 序列**，与 `input_element.focus()` 配合确保焦点序列里只有 input，键盘输入不会被打断。
- 状态分两层：`WebWindow`（外壳，持 `Box<dyn PlatformWindow>` 所有权与下划线保活句柄）+ `WebWindowInner`（`Rc` 共享内核，`Cell` 装简单值、`RefCell` 装复合体），让 DOM 回调在共享引用下也能安全修改状态。
- `raw_window_handle` 的 Web 实现直接把 canvas 的 `JsValue` 指针包装成 `WebCanvasWindowHandle`，使 gpui_web 窗口能被任何遵循 raw-window-handle 的图形库使用。

## 7. 下一步学习建议

窗口诞生时调用了 `create_raf_closure()` 和 `wake_frame_loop()`，但本讲刻意没有展开——下一讲 **u2-l3《渲染循环：requestAnimationFrame 帧驱动与 WgpuRenderer》** 将顺着这两个调用精读浏览器上的帧驱动模型：rAF 闭包如何触发 GPUI 绘制、`raf_id` 的清空时机为何关系到「不丢帧」、`pending_physical_size` 为何要延迟到 draw 才应用。如果你想先热身，可以提前读 [crates/gpui_web/src/window.rs:348-383](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L348-L383)，带着「为什么闭包第一行要先 `raf_id.set(None)`」这个问题进入下一讲。

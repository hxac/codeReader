# u2-l1 WebPlatform 初始化与图形后端选择

## 1. 本讲目标

上一讲（u1-l3）我们弄清了 GPUI 的平台抽象契约：`Platform` trait 是一份「操作系统能力清单」，`gpui_platform` 门面在 wasm 目标下把 `WebPlatform` 装配进 `Application`。本讲我们打开 `WebPlatform` 的引擎盖，精读它的构造与启动流程。学完本讲，你应该能够：

1. 逐字段说出 `WebPlatform` 结构体的组成，并按「构造时就有 / 延迟到 `run()` 才填充 / 与窗口共享」给字段分类。
2. 解释 `WebPlatform::new_with_backend` 的初始化步骤：dispatcher、前后台执行器、内嵌字体、文本系统、显示器、光标恢复监听。
3. 描述 `initialize_graphics` 的 Auto 策略：先 WebGPU、失败后换一块新 canvas 重试 WebGL2、双双失败时在页面上展示错误信息。
4. 画出 `WebWindowLifecycle` 四个状态的转换图，并说出 `WebWindowError` 五种变体各自在什么场景出现。

## 2. 前置知识

### 2.1 浏览器里没有「main 函数跑到底」这回事

桌面应用的主线程归应用自己：`run()` 进入阻塞式事件循环，退出时程序结束。浏览器反过来：**事件循环属于浏览器**，你的 wasm 代码只是被事件循环反复调用的客人。所以浏览器平台上的 `Platform::run` 不能、也不需要接管事件循环——它只负责「登记启动动作」，然后立刻返回。

### 2.2 WebGPU 的一切都是异步的

在浏览器里获取 WebGPU 的 adapter/device 要走 JS 的 `Promise`。Rust 侧用 `async fn` + `wasm_bindgen_futures::spawn_local` 来等待：`spawn_local` 把一个 future 丢到当前（主）线程的 JS 任务队列里轮转，**不需要多线程运行时**。这就是为什么图形初始化只能是异步的，也是「开窗必须写在 `run` 回调里」的根源。

### 2.3 一个 canvas 只能绑定一种上下文类型

按 HTML 规范，同一个 `<canvas>` 元素第一次 `getContext("webgpu")`（或 `"webgl2"`）之后就定型了，再请求另一种类型只会得到 `null`。所以「WebGPU 初始化失败后换 WebGL2」不能复用旧 canvas——必须把旧元素从 DOM 移除、重新造一块。记住这一点，后面读降级代码会非常自然。

### 2.4 Rust 的内部可变性：`Cell` / `RefCell` / `Rc`

wasm 单线程环境下没有 `Send`/`Sync` 压力，代码大量使用 `Rc<RefCell<Option<T>>>` 和 `Rc<Cell<T>>`：

- `Rc`：共享所有权的智能指针（非原子引用计数，单线程专用）。
- `Cell<T>`：对 `Copy` 类型（枚举、bool）的整体读写。
- `RefCell<T>`：运行时借用检查的内部可变性。

它们出现在 `WebPlatform` 的字段里，是因为平台、窗口、闭包三方都要读写同一份状态（比如窗口关闭时要反过来改平台的生命周期标志）。

### 2.5 WebGPU 的 adapter / device / queue

粗略类比：adapter 是「选中的显卡」，device 是「这块卡上的一个逻辑设备句柄」，queue 是「提交绘制命令的队列」。`WgpuContext`（由兄弟 crate `gpui_wgpu` 提供）就是把这三样打包的结构，`Surface` 则是「绘制到哪个 canvas」的抽象。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| `src/gpui_web.rs` | 库入口（25 行） | 模块声明与再导出面：`WebPlatform`、`WebWindowError`、`WebBackendPreference` |
| `src/platform.rs` | 本讲主战场 | `WebPlatform` 结构体与构造、`initialize_graphics`、`Platform::run`、`open_window`、`show_graphics_unavailable_message` |
| `src/window.rs` | 窗口实现（下一讲主角） | 只借两处：`prepare_canvas`（造 canvas）与 `Drop` 中的 `lifecycle.set(Closed)` |
| `../gpui_wgpu/src/wgpu_context.rs` | 渲染与文本设施 | `WebBackendPreference` 枚举定义、`WgpuContext::new_web` 的 adapter 请求与日志 |
| `examples/hello_web/main.rs` | 可运行示例 | `requested_backend()` 如何解析 `?backend=` 查询参数 |

永久链接约定：本讲链接基于当前 HEAD `2936989f1b7a15aaf7131b0a3c17961d706fdbf5`。

## 4. 核心概念与源码讲解

### 4.1 WebPlatform 的构造：把浏览器装配成一台「GPUI 计算机」

#### 4.1.1 概念说明

GPUI 框架核心不关心宿主是什么，它只调用 `Platform` trait 的方法。`WebPlatform` 就是「用浏览器 API 拼出来的一台假电脑」：调度器当 CPU 轮换、文本系统当字体渲染器、`WebDisplay` 当显示器、将来的 `WgpuContext` 当显卡。

这台「假电脑」有一个关键特点：**显卡是后装的**。构造函数 `new_with_backend` 运行时图形初始化还没发生（它是异步的，要等 `run()` 才启动），所以结构体里与图形相关的字段全是 `Option`，先占着位子。理解了「构造时即刻可用」和「延迟填充」这两类字段，就理解了这个结构体的设计。

#### 4.1.2 核心流程

`new_with_backend` 的装配流水线（每步都发生在 wasm 主线程上）：

```text
new_with_backend(allow_multi_threading, backend_preference)
  ├─ 1. 拿到 JS 全局 window（web_sys::window()，拿不到直接 panic）
  ├─ 2. WebDispatcher::new(window, allow_multi_threading)   # 调度核心
  ├─ 3. BackgroundExecutor / ForegroundExecutor::new(dispatcher.clone())
  ├─ 4. CosmicTextSystem::new_without_system_fonts("IBM Plex Sans")
  ├─ 5. 把 BUNDLED_FONTS（编译期内嵌的 8 个 ttf）灌进文本系统
  ├─ 6. WebDisplay::new(window)                              # 显示器
  ├─ 7. 光标状态 + 4 个光标恢复监听器
  └─ 8. 组装 Self：图形相关字段全部初始化为 None / Available
```

#### 4.1.3 源码精读

先看门面导出。[gpui_web.rs:8-24](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/gpui_web.rs#L8-L24) 声明了 8 个私有模块，然后只再导出对应用有用的类型：`WebPlatform` 与 `WebWindowError` 来自 `platform`，`WebBackendPreference` 转手自 `gpui_wgpu`。注意 **`WebWindowLifecycle` 没有被导出**——它是 `pub(crate)` 的内部状态机细节，应用代码只能通过 `WebWindowError` 感知它。而 `WebWindowError` 虽然本讲会拆开讲，它的枚举变体名（如 `GraphicsInitializationPending`）就是应用侧排错的第一手信息，所以必须公开。

结构体本体在 [platform.rs:37-53](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L37-L53)，13 个字段分成三类：

| 分类 | 字段 | 填充时机 |
| --- | --- | --- |
| 即刻可用 | `browser_window`、`dispatcher`、`background_executor`、`foreground_executor`、`text_system`、`active_display`、`callbacks`、`backend_preference`、`cursor_visible`、`last_cursor_css`、`_cursor_restore_listeners` | 构造函数 |
| 延迟填充 | `wgpu_context: Rc<RefCell<Option<WgpuContext>>>`、`prepared_window: Rc<RefCell<Option<PreparedWebWindow>>>` | `run()` 里异步图形初始化成功后 |
| 与窗口共享 | `active_window`、`window_lifecycle`（连同上面两个 `Rc` 字段） | 构造时建空壳，之后由 `WebWindow` 读写 |

`dispatcher` 用 `Arc` 而其余用 `Rc`，是因为它要被送进 wasm 后台线程（u2-l7 详解）；执行器和将来 `FetchHttpClient` 拿到的都是它的克隆。

内嵌字体在 [platform.rs:26-35](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L26-L35)：`include_bytes!` 在**编译期**把两个字体家族共 8 个 ttf 文件直接嵌进 wasm 二进制——IBM Plex Sans（界面无衬线，Regular/Italic/SemiBold/SemiBoldItalic）和 Lilex（等宽，同样四个变体）。浏览器里的 wasm 摸不到宿主机字体目录，这是让文字渲染零网络依赖、零环境依赖的唯一稳妥做法。

构造函数主体在 [platform.rs:123-174](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L123-L174)。几个值得停留的点：

- [platform.rs:127-128](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L127-L128)：`web_sys::window().expect(...)`——整个 crate 唯一「理直气壮 panic」的地方；不在浏览器里跑就没有然后了。
- [platform.rs:135-137](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L135-L137)：`CosmicTextSystem::new_without_system_fonts("IBM Plex Sans")` 明确**不**加载系统字体，并把默认字体族设为 IBM Plex Sans——正好对应上面内嵌的那组。
- [platform.rs:138-144](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L138-L144)：灌字体失败只 `log::error!` 不中断构造——文本系统缺字体时 cosmic-text 仍可工作（渲染会退化），崩溃整个应用不值得。这是 u1-l3 说的「沉默是否伤害用户」判据的又一次应用。
- [platform.rs:167-169](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L167-L169)：`wgpu_context`、`prepared_window` 初始化为 `None`，`window_lifecycle` 初始化为 `Available`——延迟填充的占位，也是 4.3 节状态机的起点。

构造函数的最后一课在 [platform.rs:176-187](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L176-L187)：`fetch_http_client` 返回的 `FetchHttpClient` 内部持有 `dispatcher` 的克隆，这就是 u1-l3 提到 `application_with_web_backend` 用它替换 `NullHttpClient` 的实现基础。

另外注意有两个公开入口（[platform.rs:119-121](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L119-L121)）：`new(allow_multi_threading)` 等价于 `new_with_backend(allow_multi_threading, WebBackendPreference::Auto)`。`gpui_platform::single_threaded_web` 走前者，`application_with_web_backend` 走后者并硬编码 `true`（允许多线程）。

#### 4.1.4 代码实践：字段清单 + 字体零请求验证

1. **实践目标**：把 `WebPlatform` 的 13 个字段亲手归类一遍，并用浏览器验证「字体已内嵌」。
2. **操作步骤**：
   - 打开 [platform.rs:37-53](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L37-L53)，为每个字段补一列「谁会读/写它」：构造函数、`run`、`open_window`，还是 `WebWindow`（提示：跟着 `Rc` 克隆走，[platform.rs:374-383](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L374-L383) 能看到哪些字段被传进了窗口）。
   - 在 `examples/hello_web` 目录运行 `trunk serve`（u1-l2 建立过的环境），打开页面后开 DevTools 的 **Network** 面板，刷新。
3. **需要观察的现象**：Network 面板里除了 wasm 本身和 `index.html`，**没有任何 `.ttf`/`.woff2` 字体请求**，但页面文字正常渲染。
4. **预期结果**：字体确实来自 wasm 内嵌（`include_bytes!`），界面标题「Prime Sieve — GPUI Web」以 IBM Plex Sans 渲染。控制台同时能看到 u1-l2 提到的 `ConsoleLogger` 输出，这为 4.2 的实践做准备。
5. 上述观察依赖本地运行，若环境不可用则「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `dispatcher` 是 `Arc<WebDispatcher>`，而 `window_lifecycle` 是 `Rc<Cell<WebWindowLifecycle>>`？

答案：`dispatcher` 要被送进 wasm 后台线程（`FetchHttpClient` 把 fetch 派发回主线程、`BackgroundExecutor` 驱动后台任务，u2-l7 详解），跨线程共享需要原子引用计数，所以是 `Arc`。`window_lifecycle` 只在主线程上被平台和窗口读写，`Rc` 更轻；状态是 `Copy` 枚举，用 `Cell` 就够，不需要 `RefCell` 的借用语义。

**练习 2**：如果把 `include_bytes!` 换成运行时 `fetch` 字体文件，会引入哪些新问题？

答案：至少三个：启动多了一轮网络往返，文字渲染要等字体下载完成（或先闪替身字体）；离线/弱网环境下应用退化；页面需要额外托管字体资源与 CORS 配置。`include_bytes!` 把这三类问题在编译期全部消掉，代价只是 wasm 体积增大约两个字体家族的大小。

**练习 3**：`new_without_system_fonts` 这个名字里「without system fonts」在浏览器语境下意味着什么？

答案：浏览器沙箱里的 wasm 本来就读不到宿主机字体文件，所谓「系统字体」无从谈起；cosmic-text 在桌面平台会扫描系统字体目录，这里显式跳过该扫描，保证行为确定——字体只有接下来 `add_fonts` 灌进去的那 8 个，默认族是 IBM Plex Sans。

### 4.2 Platform::run 与异步图形初始化：WebGPU 优先、WebGL2 兜底

#### 4.2.1 概念说明

`Platform::run(on_finish_launching)` 是 GPUI 应用生命周期的发令枪：回调执行时，应用应当打开自己的第一个窗口。桌面平台上它跑阻塞事件循环；浏览器上它只做一件事——`spawn_local` 一个异步任务去初始化图形，**成功后才调用 `on_finish_launching`**。这个顺序保证了一件事：写在这个回调里的 `open_window` 一定能拿到已就绪的图形上下文。

图形后端有三种偏好（[wgpu_context.rs:28-34](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_wgpu/src/wgpu_context.rs#L28-L34)）：`Auto`（默认，WebGPU 优先、失败落 WebGL2）、`WebGpu`、`WebGl`。注意 `Auto` 的「先试谁、失败了怎么退」这套策略**并不在 gpui_wgpu 里实现**，而是由 `gpui_web::platform` 的 `initialize_graphics` 函数实现——这样每一步失败都能给出带完整上下文的聚合错误信息。

#### 4.2.2 核心流程

```text
Platform::run(on_finish_launching)
  └─ spawn_local:
       initialize_graphics(window, preference)
         ├─ Auto:
         │    canvas₁ = prepare_canvas()
         │    若 is_browser_webgpu_supported():
         │        WgpuContext::new_web(canvas₁, WebGpu)
         │    否则视为 WebGPU 失败
         │    ├─ 成功 → 返回 (canvas₁, context, surface)
         │    └─ 失败 → 移除 canvas₁，log::warn
         │         canvas₂ = prepare_canvas()          # 换一块干净的 canvas
         │         WgpuContext::new_web(canvas₂, WebGl)
         │         ├─ 成功 → 返回 (canvas₂, context, surface)
         │         └─ 失败 → 移除 canvas₂，聚合两次错误返回 Err
         └─ WebGpu / WebGl（显式指定）:
              canvas = prepare_canvas()
              new_web(canvas, preference)              # 只试这一个
              失败 → 移除 canvas，Err（错误信息注明"只试了 X"）
       ├─ Ok:  填 wgpu_context / prepared_window → on_finish_launching()
       └─ Err: lifecycle ← Unavailable → log::error → 页面上插入 <p> 错误信息
```

`prepare_canvas`（[window.rs:77-107](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L77-L107)）每次调用都新建一个 `<canvas>` 追加到 `<body>`，并设置 `width/height:100%`、`display:block`、`touch-action:none` 等样式——细节留到 u2-l2，这里只需知道它是「造一块干净的画布」。

#### 4.2.3 源码精读

发令枪本体在 [platform.rs:280-304](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L280-L304)。先把四个共享状态克隆进闭包（wasm 闭包 captures 的经典姿势，u3-l2 专题展开），再 `spawn_local`。成功分支（[platform.rs:288-296](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L288-L296)）做了三件事：打日志（把 `context.backend()` 的实际后端名告诉我们）、把 `WgpuContext` 和 `PreparedWebWindow { canvas, surface }` 存进平台字段、最后才调 `on_finish_launching()`——顺序即契约。失败分支（[platform.rs:297-302](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L297-L302)）先把生命周期打成 `Unavailable`（让并发的 `open_window` 拿到确定错误），再打日志并展示用户可见的信息。

`PreparedWebWindow`（[platform.rs:55-58](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L55-L58)）是「canvas + 与它绑定的 wgpu Surface」的成对包装——surface 是针对那块具体 canvas 创建的，两者必须一起搬家，不能错配。

`initialize_graphics` 的 Auto 分支在 [platform.rs:199-243](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L199-L243)：

- [platform.rs:201-207](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L201-L207) 先用 wgpu 自带的 `is_browser_webgpu_supported()` 探测 `navigator.gpu`；注意探测通过与否最终都汇入同一个 `webgpu_result`，之后统一处理。
- [platform.rs:212-217](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L212-L217) WebGPU 失败时：把 canvas₁ 从 DOM **移除**、`log::warn!` 记录原因。
- [platform.rs:219-225](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L219-L225) 重新 `prepare_canvas` 造 canvas₂——为什么换新的？见 2.3：失败的尝试已经碰过 canvas₁ 的上下文，同一元素拿不到第二种上下文类型。这里若连造 canvas 都失败，错误会把「WebGPU 为什么失败」和「造画布为什么失败」一起说清。
- [platform.rs:226-240](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L226-L240) WebGL2 再失败，则移除 canvas₂ 并返回聚合了两条失败原因的 `Err`。

显式指定后端的分支在 [platform.rs:244-264](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L244-L264)：不降级，只试指定的那一个；错误信息里明确写「Only {backend_name} was tried because the application requested it explicitly」——应用主动选择后，用户排错时需要知道没有发生过静默降级。

真正向 adapter 要设备的是 gpui_wgpu 侧的 [wgpu_context.rs:144-150](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_wgpu/src/wgpu_context.rs#L144-L150) 与 [wgpu_context.rs:154-233](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_wgpu/src/wgpu_context.rs#L154-L233)：按偏好设定 `Backends` 位掩码（[wgpu_context.rs:158-162](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_wgpu/src/wgpu_context.rs#L158-L162)）、对 canvas 建 `SurfaceTarget::Canvas` surface、`request_adapter`（`HighPerformance` 优先，[wgpu_context.rs:181-192](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_wgpu/src/wgpu_context.rs#L181-L192)）、再按 adapter 实际回报的 backend 归类（[wgpu_context.rs:194-202](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_wgpu/src/wgpu_context.rs#L194-L202)，出现浏览器之外的 backend 直接报错）。它在 [wgpu_context.rs:216-221](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_wgpu/src/wgpu_context.rs#L216-L221) 打的日志会写出 `requested=...` 与 `selected=...` 的对比，是实践环节的观察点之一。

最后看「双失败」的用户可见降级：[platform.rs:762-776](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L762-L776) 直接 `document.create_element("p")` 造一个段落、写上错误文本、追加到 `<body>`——没有 GPU 就没有 GPUI 界面，那就用最原始的 DOM 告诉用户出了什么问题。每一步都 `.ok()`/`else return` 容错，因为此时连「能安全 panic 吗」都不好说。

#### 4.2.4 代码实践：三种后端配置对照实验

1. **实践目标**：亲手触发 Auto / WebGpu / WebGl 三种路径，用控制台日志确认每种配置实际选中的后端，并观察一次真实的降级。
2. **操作步骤**：
   - 确认示例入口如何读参数：[examples/hello_web/main.rs:408-427](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/examples/hello_web/main.rs#L408-L427) 的 `requested_backend()` 解析 URL 查询串，`?backend=webgpu` → `WebGpu`，`?backend=webgl` → `WebGl`，其余（包括无参数）→ `Auto`；[main.rs:429-431](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/examples/hello_web/main.rs#L429-L431) 把结果交给 `application_with_web_backend`。日志通路由 `web_init()` 保证（它调用 `gpui_web::init_logging()`，见 [gpui_platform.rs:51-54](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_platform/src/gpui_platform.rs#L51-L54)）。
   - `trunk serve` 后依次访问 `http://localhost:8080/`、`http://localhost:8080/?backend=webgpu`、`http://localhost:8080/?backend=webgl`，每次打开 DevTools Console 并刷新。
   - 记录两条日志：`Browser graphics initialized successfully with ...`（来自 [platform.rs:289-292](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L289-L292)，`context.backend()` 的值应为 `BrowserWebGpu` 或 `Gl`）和 `Browser graphics initialized: requested=..., selected=...`（来自 [wgpu_context.rs:216-221](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_wgpu/src/wgpu_context.rs#L216-L221)）。
   - （可选，制造一次降级）在 Chrome 地址栏进入 `chrome://flags` 禁用 WebGPU（搜索 "WebGPU"，Disable 后重启浏览器），再访问 `?backend=webgpu` 和不带参数两种地址各一次，观察日志与页面表现。
3. **需要观察的现象**：
   - 正常环境下三种配置都能启动；`Auto` 与 `?backend=webgpu` 的日志显示 `BrowserWebGpu`，`?backend=webgl` 显示 `Gl`。
   - 禁用 WebGPU 后：`?backend=webgpu` 出现 `Failed to initialize browser graphics` 错误日志，页面渲染出包含「Only WebGPU was tried」字样的 `<p>` 错误段落；不带参数（Auto）则出现 `WebGPU initialization failed; falling back to WebGL2: ...` 的 warn 日志，随后以 `Gl` 后端正常启动。
4. **预期结果**：你将分别看到「显式后端失败即失败」「Auto 静默降级成功」两条路径的用户可见差异——这正是 [platform.rs:244-264](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L244-L264) 与 [platform.rs:199-243](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L199-L243) 两个分支的现场对照。
5. 本实践依赖本地浏览器环境与 `chrome://flags` 的可操作性，若不可用则「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `on_finish_launching()` 必须在 `wgpu_context` 和 `prepared_window` 填充**之后**调用，而不能先调再用 `.set()` 触发重试？

答案：`open_window` 是同步接口，它读 `wgpu_context.borrow()` 时若为 `None` 只能返回 `GraphicsInitializationPending` 错误（见 4.3）。先填充后回调，保证回调里同步执行的 `open_window` 一次成功；GPUI 的应用写法也由此固定为「在 `run` 回调里开窗」。反过来「先回调再填充」会把可避免的错误强加给每个应用。

**练习 2**：WebGPU 失败后为什么必须 `canvas.remove()` 再 `prepare_canvas` 造一块新 canvas，而不是复用 canvas₁ 直接 `new_web(canvas₁, WebGl)`？

答案：HTML 规范限定一个 canvas 元素只能绑定一种上下文类型；初始化 WebGPU 的尝试已经（或可能已经）在 canvas₁ 上建立/协商过 webgpu 上下文，再要 WebGL2 上下文会拿不到。所以代码把被污染的 canvas₁ 移除、造一块全新的 canvas₂ 给 WebGL2。

**练习 3**：Auto 分支里 `is_browser_webgpu_supported()` 探测失败时并没有调用 `WgpuContext::new_web`，而是直接构造了一条 `Err`。这样写的好处是什么？

答案：把「`navigator.gpu` 都不存在/不可用」和「请求 adapter 失败」统一成同一条 `webgpu_result` 处理路径，后续的降级、日志、canvas 清理代码只需写一遍；同时错误信息（"browser WebGPU probe did not return a usable adapter"）如实区分了失败层次，排错时不会把能力缺失误诊为硬件故障。

### 4.3 窗口生命周期状态机与 WebWindowError

#### 4.3.1 概念说明

浏览器平台有一条硬约束：**整个文档只支持一个 GPUI 顶层窗口，且关闭后不能重开**。原因很物理——图形初始化只做一次，`prepared_window` 里那对「canvas + surface」被 `open_window` 一次性取走（`take()`）交给 `WebWindow`，没有第二份；关闭窗口时 canvas 已从 DOM 移除，重建 surface 等于重跑整套初始化，代码干脆不支持。

为了把这条约束表达成机器可查、人可读的形式，crate 用了两件套：

- `WebWindowLifecycle`（内部）：四态状态机，`Rc<Cell<...>>` 存在平台上、由窗口共享。
- `WebWindowError`（公开）：五种错误变体，让应用在 `open_window` 返回 `Err` 时知道「发生了什么、还能不能重试」。

#### 4.3.2 核心流程

状态机的全部合法转换（`→` 左侧是触发者）：

```text
            initialize_graphics 成功(run)      open_window 成功
  Available ────────────────────────► Available ────────────► Open
     │                                    │                     │
     │ initialize_graphics 失败(run)       │ open_window 时      │ WebWindow 被
     ▼                                    │ 图形尚未就绪         │ Drop(窗口关闭)
 Unavailable                               ▼                     ▼
     │任何 open_window                 GraphicsInitialization   Closed
     ▼                                 Pending(可重试,状态不变)    │任何 open_window
 GraphicsUnavailable                                              ▼
                                                          ReopeningUnsupported
  Open 状态下再次 open_window ──► AlreadyOpen
  open_window 内 WebWindow::new 失败 ──► 状态翻成 Unavailable（并移除 canvas）
```

`open_window` 的判定顺序本身就是文档（[platform.rs:332-397](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L332-L397)）：先查窗口种类 → 再查生命周期 → 最后查图形就绪 → 构造窗口 → 回写状态。

#### 4.3.3 源码精读

状态机定义在 [platform.rs:60-66](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L60-L66)，标了 `pub(crate)`——只在本 crate 内部流转。五种错误在 [platform.rs:68-79](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L68-L79)，其中两个变体带文档注释，明确划分了「可重试」（`GraphicsInitializationPending`：等 `Platform::run` 回调里再试）与「不可重试」（`GraphicsUnavailable`：初始化或早前建窗已失败）。它们对应的用户可读文案在 [platform.rs:81-102](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L81-L102) 的 `Display` 实现里，例如 `AlreadyOpen` 的文案直说「GPUI web supports only one top-level window」。

`open_window` 的四道关卡：

1. **窗口种类**（[platform.rs:337-349](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L337-L349)）：只有 `WindowKind::Normal` 放行。注意错误类型的分层：`AnchoredPopup` 返回的是 `PopupNotSupportedError`（GPUI 专门的弹窗错误类型），而 `PopUp`/`Floating`/`Dialog` 返回 `WebWindowError::UnsupportedWindowKind`，文案建议「render it inside the normal window instead」——浏览器里这些应当作为普通窗口内的浮层渲染，而非独立原生窗口。种类检查排在生命周期检查**之前**，所以即使在 `Unavailable` 状态下，弹窗请求得到的也是 `PopupNotSupportedError` 而非 `GraphicsUnavailable`。
2. **生命周期**（[platform.rs:351-360](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L351-L360)）：`Open` → `AlreadyOpen`；`Closed` → `ReopeningUnsupported`；`Unavailable` → `GraphicsUnavailable`；`Available` 放行。
3. **图形就绪**（[platform.rs:362-370](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L362-L370)）：`wgpu_context` 为 `None` 或 `prepared_window` 被 `take()` 出来是 `None`，都返回 `GraphicsInitializationPending`。这里的 `take()` 是点睛之笔——`prepared_window` 是一次性的，取走即空，天然保证了「第二块顶层窗口不存在」在数据结构层面成立。
4. **构造与回写**（[platform.rs:374-396](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L374-L396)）：把 context、canvas、surface 连同共享的 `window_lifecycle`、`active_window` 一起交给 `WebWindow::new`（下一讲主角）。成功则状态置 `Open`、记录活动窗口；失败则把 canvas 从 DOM 移除、状态翻成 `Unavailable`——「构造窗口失败」被视同「图形不可用」，此后任何 `open_window` 都返回 `GraphicsUnavailable`，这与该变体文档注释里「or an earlier window creation failed」的说法一一对应。

那 `Closed` 是谁设置的？不在 platform.rs，而在窗口自己的 `Drop`：[window.rs:529-534](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L529-L534) 里窗口被销毁时移除 canvas 与 input 元素、清掉 `active_window`，最后 `self.lifecycle.set(WebWindowLifecycle::Closed)`。这正是 `window_lifecycle` 必须做成 `Rc<Cell<...>>` 并在构造时传进 `WebWindow` 的原因：平台与窗口共同维护同一份状态，`Drop` 成了状态机的第四个触发者。

#### 4.3.4 代码实践：绘制状态转换触发条件表

1. **实践目标**：不看本节答案，独立整理出 `WebWindowLifecycle` 每个状态转换的触发条件与代码位置。
2. **操作步骤**：
   - 通读 [platform.rs:280-304](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L280-L304)（`run`）、[platform.rs:332-397](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L332-L397)（`open_window`）、[window.rs:529-535](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L529-L535)（`Drop`），找出**所有** `window_lifecycle.set(...)` 调用点。
   - 为每个调用点记录四列：当前状态（隐含或显式）、触发事件、目标状态、对应到应用的可见结果（哪个 `WebWindowError` 变体或成功）。
3. **需要观察的现象**：全 crate 对 `window_lifecycle.set` 的调用共 4 处——`run` 失败分支设 `Unavailable`、`open_window` 成功设 `Open`、`open_window` 建窗失败设 `Unavailable`、`Drop` 设 `Closed`；没有任何代码路径把状态从 `Closed`/`Unavailable` 改回 `Available`。
4. **预期结果**：整理出与 4.3.2 状态图一致的表格（参考版本见本讲第 5 节综合实践第 3 步）。`Available → Open` 之外的一切转换都是单向「退化」，这从代码结构上印证了「单窗口、不可重开」是设计而非疏忽。
5. 纯源码阅读即可完成，无需运行环境。

#### 4.3.5 小练习与答案

**练习 1**：应用把 `cx.open_window(...)` 写在了 `application.run(...)` 回调**之外**（例如某个异步任务里且早于 `run`），会发生什么？写在哪里才能成功？

答案：此时 `window_lifecycle` 还是 `Available`（图形初始化尚未完成或未开始），关卡 3 命中：`wgpu_context` 为 `None`，返回 `GraphicsInitializationPending`，其文档明确说「从 `Platform::run` 的回调里重试可以成功」。正确写法就是放进 `run` 的回调（hello_web 的 [main.rs:431-442](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/examples/hello_web/main.rs#L431-L442) 即是标准姿势）。若迟于 `run` 回调但图形初始化失败，则得到 `GraphicsUnavailable`。

**练习 2**：为什么 `AnchoredPopup` 用 `PopupNotSupportedError`，而 `PopUp`/`Floating`/`Dialog` 用 `WebWindowError::UnsupportedWindowKind`？两者语义差别在哪？

答案：`PopupNotSupportedError` 是 gpui 框架定义的通用「平台不支持弹出窗口」错误，调用方（如弹窗菜单组件）可以据此走通用降级逻辑（改为在普通窗口内渲染浮层）；`UnsupportedWindowKind` 是 gpui_web 自己的错误，携带具体种类名，文案还给出迁移建议。分层让框架级组件只认识框架级错误，平台细节不会渗透进通用代码。

**练习 3**：`prepared_window` 为什么用 `take()` 而不是 `clone()` 或 `borrow()`？

答案：canvas + surface 这一对资源在物理上只有一份：surface 绑定在特定 canvas 上，克隆出两个句柄意味着两个「窗口」画到同一块画布，语义崩坏。`take()` 把值的所有权整体移交给 `WebWindow`，同时把槽位清空——此后再次 `open_window` 命中关卡 3 的 `None` 分支返回 `GraphicsInitializationPending`，配合关卡 2 的 `AlreadyOpen` 检查，双保险封死第二块顶层窗口。

## 5. 综合实践

把本讲三个模块串成一次完整的「启动流程侦查」：

1. **准备**：按 u1-l2 的方式在 `examples/hello_web` 下 `trunk serve`，打开 DevTools（Console + Network 两个面板）。
2. **后端矩阵实验**：依次访问 `/`、`/?backend=webgpu`、`/?backend=webgl`，在 Console 里抓取 `Browser graphics initialized successfully with ...` 与 `Browser graphics initialized: requested=..., selected=...` 两条日志，填一张三行两列的表（配置 × 实际后端）。有条件的话再用 `chrome://flags` 关闭 WebGPU，重复三轮，补记 Auto 的降级 warn 日志与 `?backend=webgpu` 的页面 `<p>` 错误信息（对照 4.2.4）。
3. **状态机审计**：以 4.3.4 的四列表为基础，写出完整参考答案：

| # | 当前状态 | 触发事件（代码位置） | 目标状态 / 应用可见结果 |
| --- | --- | --- | --- |
| 1 | Available | `run()` 中 `initialize_graphics` 成功（[platform.rs:293-295](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L293-L295)） | 状态不变，`wgpu_context`/`prepared_window` 填充，`on_finish_launching()` 被调用 |
| 2 | Available | `run()` 中 `initialize_graphics` 失败（[platform.rs:297-302](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L297-L302)） | `Unavailable`；页面出现错误 `<p>`，后续 `open_window` 得 `GraphicsUnavailable` |
| 3 | Available | `open_window` 时图形尚未就绪（[platform.rs:362-370](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L362-L370)） | 状态不变；返回 `GraphicsInitializationPending`（可重试） |
| 4 | Available | `open_window` 成功（[platform.rs:385-389](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L385-L389)） | `Open`；`active_window` 记录句柄 |
| 5 | Open | 再次 `open_window`（[platform.rs:351-352](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L351-L352)） | 状态不变；返回 `AlreadyOpen` |
| 6 | Open | `open_window` 中 `WebWindow::new` 失败（[platform.rs:390-395](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L390-L395)） | `Unavailable`（canvas 被移除）；返回原始错误，后续得 `GraphicsUnavailable` |
| 7 | Open | 窗口关闭，`WebWindow` 被 `Drop`（[window.rs:534](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L534)） | `Closed` |
| 8 | Closed | 任何 `open_window`（[platform.rs:353-355](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L353-L355)） | 状态不变；返回 `ReopeningUnsupported` |
| 9 | Unavailable | 任何 `open_window`（[platform.rs:356-358](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L356-L358)） | 状态不变；返回 `GraphicsUnavailable` |

4. **收尾追问**：结合第 2、3 步的证据回答——如果 `run` 回调里第一次 `open_window` 返回了 `GraphicsInitializationPending`，最可能的原因是什么？（答案：你拿到的其实不是 `run` 回调的执行时机，或图形初始化 promise 尚未落定；对照转换 #3。）

## 6. 本讲小结

- `WebPlatform` 是「浏览装配成的 GPUI 计算机」：构造函数搭好调度器、双执行器、内嵌 8 个字体的 CosmicTextSystem、显示器与光标状态；图形三字段（`wgpu_context`、`prepared_window`、`window_lifecycle`）留空/置初值，等 `run()` 异步填充。
- 字体用 `include_bytes!` 编译期内嵌，`add_fonts` 失败仅记日志不崩溃；默认字体族为 IBM Plex Sans，不扫系统字体。
- 浏览器的 `Platform::run` 不接管事件循环：它 `spawn_local` 一个异步任务做图形初始化，成功后先填充 `wgpu_context`/`prepared_window` 再调 `on_finish_launching`，失败则置 `Unavailable` 并在页面上插入 `<p>` 错误段落。
- `initialize_graphics` 的 Auto 策略：探测并尝试 WebGPU → 失败则移除旧 canvas、造新 canvas 重试 WebGL2（因为一个 canvas 只能绑一种上下文）→ 双失败返回聚合错误；显式指定后端则只试一个且错误信息注明未做降级。
- 窗口生命周期是四态单向状态机（Available → Open → Closed；任何时刻可跌入 Unavailable），由 `run`、`open_window`、`WebWindow::Drop` 三方通过共享的 `Rc<Cell<WebWindowLifecycle>>` 维护。
- `WebWindowError` 五变体对应五个具体场景，其中只有 `GraphicsInitializationPending` 可重试；`prepared_window` 的 `take()` 从数据结构层面封死了第二块顶层窗口。

## 7. 下一步学习建议

下一讲 **u2-l2「WebWindow 的诞生」**顺着本讲的终点继续：`open_window` 把 canvas、surface、共享状态交给 `WebWindow::new` 之后发生了什么——`prepare_canvas` 注入的每条 CSS 的用意、为什么需要一个 1px 隐藏 input 元素来承接键盘焦点、`WebWindowInner` 如何用 `RefCell`/`Cell` 组织内部可变状态。建议先自行通读 `src/window.rs` 的前 250 行，带着「`window_lifecycle` 在窗口内部还会被谁改」这个问题去读（本讲已剧透一处：`Drop`）。后续 u2-l3 将接管这块 canvas 上的 rAF 帧循环，u3-l3 会把本讲的后端降级与调度器探测汇总成完整的浏览器兼容矩阵。

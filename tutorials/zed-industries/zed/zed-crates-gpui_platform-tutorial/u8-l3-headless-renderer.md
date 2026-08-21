# 无头渲染:PlatformHeadlessRenderer 与离屏截图

## 1. 本讲目标

学完本讲,你应该能够:

1. 说清「无头平台」(headless platform,u5-l2 学过的 `HeadlessClient`)与「无头渲染」(headless rendering,本讲主角)是两个**正交**的概念,并能各举一个组合例子。
2. 逐方法复述 `PlatformHeadlessRenderer` 契约的三个方法,解释 `render_scene`(只提交、不回读)与 `render_scene_to_image`(提交并回读)在性能语义上的差异。
3. 解释 `gpui_platform::current_headless_renderer` 为什么目前只在 macOS 上返回 `Some`(Metal 实现),在其他平台返回 `None`,以及调用方应如何降级。
4. 掌握 `HeadlessAppContext` 的组装方式(测试平台 + 真实文本系统 + 可选渲染器工厂),并能写出「把一个 GPUI 视图离屏渲染成 PNG」的最小示例。

## 2. 前置知识

本讲是第 8 单元(高级主题)的第三篇,默认你已读过 u5-l2(headless 客户端)与 u8-l2(PlatformAtlas 与渲染后端)。下面把几个关键概念用通俗语言再过一遍:

- **无头平台(headless platform)**:一个不连接真实窗口系统的 `Platform` 实现。u5-l2 里 Linux 的 `HeadlessClient` 让 GPUI 在没有显示器的服务器上也能跑完布局、实体更新整条管线——但它**不产生真实像素**,`HeadlessAtlas` 只分配瓦片、不上传纹理。
- **无头渲染(headless / offscreen rendering)**:与本讲正好互补的另一半——**不经过任何窗口,直接把一帧的绘制指令渲染到一块离屏纹理上**。它需要真实的 GPU 后端(Metal、DirectX、wgpu),产出的是真正的像素。
- **Scene(场景)**:GPUI 每帧把元素树编码成 `Scene`——一组图元(quad、shadow、path、sprite)加上对图集纹理的引用。u8-l2 讲过,图集(`PlatformAtlas`)把字形装箱进纹理;本讲的渲染器就是「吃进 Scene、画到离屏纹理」的那一端。
- **回读(readback)**:把 GPU 显存里的纹理像素拷回 CPU 内存。这是 GPU 渲染中最昂贵的操作之一(需要命令缓冲区提交后同步等待),所以契约把「要不要回读」拆成了两个方法。
- **`test-support` feature**:本讲涉及的全部 API(`PlatformHeadlessRenderer`、`current_headless_renderer`、`HeadlessAppContext`、`TestPlatform` 的渲染器注入点)都藏在 `#[cfg(any(test, feature = "test-support"))]` 后面。它们是**测试与基准测量设施**,不属于 GPUI 的正式运行时公共 API。
- **`image::RgbaImage`**:`image` crate 里的 RGBA8 位图类型,宽 \( W \) × 高 \( H \) 的图像在内存中占 \( W \times H \times 4 \) 字节。无头渲染的最终产物就是它。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `../gpui/src/platform.rs` | 契约层:定义 `PlatformHeadlessRenderer` trait 与 `PlatformWindow::render_to_image` 默认实现 |
| `src/gpui_platform.rs` | 门面层:`current_headless_renderer()` 按平台构造渲染器(macOS 返回 Metal 实现,其余 `None`) |
| `../gpui/src/window.rs` | 应用层入口:`Window::render_to_image()` 把最近一帧的 Scene 交给平台窗口回读 |
| `../gpui/src/app/headless_app_context.rs` | 组装层:`HeadlessAppContext` 把测试平台、文本系统、渲染器工厂拼成一个可截图的上下文 |
| `../gpui/src/platform/test/platform.rs` | `TestPlatform::open_window` 调用渲染器工厂,把渲染器塞进每个测试窗口 |
| `../gpui/src/platform/test/window.rs` | `TestWindow` 持有渲染器;`draw` 走 `render_scene`,`render_to_image` 走 `render_scene_to_image` |
| `../gpui_apple/src/metal_renderer.rs` | 唯一实现:`MetalHeadlessRenderer` 与 `MetalRenderer` 的离屏渲染、纹理回读 |
| `../gpui_macos/src/gpui_macos.rs` | 把 `gpui_apple` 的 `MetalHeadlessRenderer` 以 `gpui_macos::metal_renderer` 路径再导出 |
| `../gpui_macos/src/platform.rs` | 对照材料:`MacPlatform` 自己的 `headless` 布尔(无头平台,不是无头渲染) |
| `../gpui/src/app/bench_context.rs` | 真实用户一:`bench_platform()` 接受渲染器工厂,给基准测试提供 GPU 提交路径 |
| `../gpui_macros/src/bench.rs` | 真实用户二:`#[gpui::bench]` 宏展开后调用 `gpui_platform::current_headless_renderer()` |

## 4. 核心概念与源码讲解

本讲的三个最小模块:`PlatformHeadlessRenderer`(契约)、`current_headless_renderer`(门面构造器与 macOS 实现)、`HeadlessAppContext`(组装与截图入口)。

### 4.1 模块一:PlatformHeadlessRenderer——无窗口渲染契约

#### 4.1.1 概念说明

u8-l2 讲渲染后端时你已看到:图集负责「字形/纹理进显存」,渲染器负责「把 Scene 画出来」。正常路径里,渲染目标是一个真实窗口的交换链(Metal 的 `CAMetalLayer` drawable、Windows 的交换链、wgpu 的 surface)。但在两类场景下,你根本没有窗口:

1. **视觉测试(visual test)**:想断言「这一帧画出来的像素长什么样」,又不想(或不能)在 CI 服务器上开真窗口。
2. **基准测试(benchmark)**:想测量「编码 Scene + 提交 GPU」的真实开销,让渲染管线的回归能在数据里现形,但产出的像素根本没人看。

`PlatformHeadlessRenderer` 就是为这两类场景定义的契约:输入一个 `Scene` 和目标尺寸,输出渲染结果。它把「要不要把像素拿回来」拆成两个方法——这正是性能语义的分界线。

注意它与 `PlatformWindow::render_to_image` 的关系:后者是 `PlatformWindow` trait 上的一个方法(带默认报错实现),而前者是一个**独立的 trait 对象**。测试替身 `TestWindow` 通过持有 `Option<Box<dyn PlatformHeadlessRenderer>>` 把两者接通——这会在 4.3 节展开。

#### 4.1.2 核心流程

一次无头渲染的生命周期:

```text
应用代码更新视图
    │  cx.notify() → 窗口重绘
    ▼
Window 把元素树编码成 Scene(含对图集瓦片的引用)
    │
    ├── 普通路径:PlatformWindow::draw(scene)
    │       测试窗口里 → renderer.render_scene(scene, size)
    │       · 创建/复用离屏纹理作为渲染目标
    │       · 编码 GPU 命令并提交
    │       · 不等待 GPU 完成、不拷贝像素          ← 「假呈现」,供基准测量
    │
    └── 截图路径:Window::render_to_image()
            → PlatformWindow::render_to_image(&scene)
            → 测试窗口里 → renderer.render_scene_to_image(scene, size)
            · 新建一块 CPU 可读的 Managed 纹理
            · 编码 GPU 命令、提交、等待完成
            · 回读像素、BGRA→RGBA、装进 RgbaImage   ← 「真取回」,供视觉断言
```

关键点:两条路径复用同一套 Scene 编码与命令提交逻辑,差别只在渲染目标的存储模式(`Private` vs `Managed`)、是否同步等待、是否回读。

#### 4.1.3 源码精读

契约定义在 gpui 主 crate 的 platform.rs,整个 trait 受 test-support 门控:

> [../gpui/src/platform.rs:991-1010](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/platform.rs#L991-L1010) —— `PlatformHeadlessRenderer` trait:三个方法分别是 `render_scene_to_image`(渲染并回读成 RGBA 图像)、`render_scene`(只渲染到离屏目标、不回读,文档注释明确说这是「呈现一帧」的无头对应物,做同样的 CPU 侧编码与 GPU 提交但不阻塞等待)、`sprite_atlas`(交出该渲染器使用的图集)。

```rust
#[cfg(any(test, feature = "test-support"))]
pub trait PlatformHeadlessRenderer {
    fn render_scene_to_image(
        &mut self,
        scene: &Scene,
        size: Size<DevicePixels>,
    ) -> Result<RgbaImage>;

    fn render_scene(&mut self, scene: &Scene, size: Size<DevicePixels>) -> Result<()>;

    fn sprite_atlas(&self) -> Arc<dyn PlatformAtlas>;
}
```

`sprite_atlas` 的存在呼应 u8-l2 的所有权结论「图集每渲染器一份」:持有渲染器的一方(下一节的 `TestWindow`)应当直接复用渲染器自带的图集,而不是另造一个测试图集,否则字形永远进不了真实纹理。

再看 `PlatformWindow` 侧的入口。trait 给了一个**默认报错实现**,任何没接入无头渲染的平台窗口调用即得 `Err`:

> [../gpui/src/platform.rs:982-988](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/platform.rs#L982-L988) —— `PlatformWindow::render_to_image` 的默认实现直接 `anyhow::bail!("render_to_image not implemented for this platform")`;文档注释点明它不把帧呈现到屏幕,用于「想捕获将渲染的内容、又不想显示窗口」的视觉测试。

应用层的门面在 `Window` 上:

> [../gpui/src/window.rs:2454-2461](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/window.rs#L2454-L2461) —— `Window::render_to_image()` 把 `self.rendered_frame.scene`(最近一次绘制编码出的 Scene)原样传给 `platform_window.render_to_image()`。注意它**重新渲染**这份 Scene,而不是读回上次画完的帧——同一份输入,确定性的输出。

测试替身 `TestWindow` 对这两个入口的接线:

> [../gpui/src/platform/test/window.rs:394-403](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/platform/test/window.rs#L394-L403) —— `TestWindow::draw` 在持有渲染器时调用 `renderer.render_scene(scene, device_size)`(并用 `warn_on_err` 记录失败):窗口的每次「呈现」变成一次无 GPU 等待的离屏提交,这正是基准测量需要的形状。

> [../gpui/src/platform/test/window.rs:409-420](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/platform/test/window.rs#L409-L420) —— `TestWindow::render_to_image` 在持有渲染器时调用 `renderer.render_scene_to_image(scene, device_size)` 并返回 `RgbaImage`;没有渲染器时 `bail!("render_to_image not available: no HeadlessRenderer configured")`——这就是非 macOS 平台截图失败的报错文案,综合实践里你会亲眼见到它。

#### 4.1.4 代码实践(源码阅读型)

1. **实践目标**:摸清仓库里 `render_to_image` 与 `render_scene*` 的全部落点,验证「契约一个、入口两个、实现目前一个」的结构。
2. **操作步骤**:在仓库根目录执行:
   ```bash
   rg -n "fn render_to_image|fn render_scene_to_image|fn render_scene\b" crates --type rust
   ```
   再执行 `rg -n "PlatformHeadlessRenderer" crates --type rust`。
3. **需要观察的现象**:第一组搜索应命中:契约(platform.rs)、`Window` 门面(window.rs)、`TestWindow` 接线(test/window.rs)、Metal 实现与内部方法(metal_renderer.rs)、以及 macOS 真实窗口的 layer 版本(gpui_macos/src/window.rs)。第二组搜索还应额外命中 `bench_context.rs` 与 `gpui_macros/src/bench.rs`(4.3 节会讲)。
4. **预期结果**:你能把命中项填进一张三列表格——「契约声明 / 测试侧接线 / 真实实现」,并发现真实实现只有 `gpui_apple` 一处。金属渲染器里还有一个 layer 版本的 `render_to_image`(见 4.2.3),把它单独标注为「窗口路径」,与「无头路径」区分。
5. 本实践为纯源码检索,不依赖运行环境,可直接完成。

#### 4.1.5 小练习与答案

**练习 1**:`render_scene` 与 `render_scene_to_image` 只差一个「回读」,为什么值得拆成两个方法,而不是给前者加一个 `readback: bool` 参数?

**参考答案**:两者不是同一操作的开关,而是两种使用场景的完整语义:基准测量需要「尽可能像真呈现」——提交后立刻返回、不阻塞、复用私有存储的纹理;视觉截图需要「确定性拿到像素」——用 CPU 可读的 Managed 纹理、同步等待 GPU、转换通道顺序。拆成两个方法让实现可以分别优化(纹理存储模式、是否等待、是否复用),也让调用方(测试窗口的 `draw` 与 `render_to_image`)各自绑定到明确的语义上,不存在「忘了传 bool」这类误用。

**练习 2**:`Window::render_to_image` 为什么渲染 `rendered_frame.scene`(上一帧编码好的 Scene),而不是重新走一遍元素树的 layout/paint?

**参考答案**:因为视觉测试想断言的正是「上一次 draw 实际编码了什么」。复用已编码的 Scene 保证截图与呈现内容一致且确定(同一 Scene 输入同一渲染器,输出相同像素),同时省掉了重复的 layout/paint 开销——测试里通常先用 `update_window` 驱动一次绘制,再调 `capture_screenshot`。

### 4.2 模块二:current_headless_renderer 与 MetalHeadlessRenderer——macOS 独苗

#### 4.2.1 概念说明

`gpui_platform` 门面 crate 提供了与 `current_platform` 同风格的便捷构造器 `current_headless_renderer()`。它的返回类型是 `Option<Box<dyn PlatformHeadlessRenderer>>`——**`Option` 本身就是平台能力声明**:目前只有 macOS 返回 `Some`(包装 Metal 实现),Windows、Linux、wasm 一律返回 `None`。

为什么只有 macOS?观察各渲染后端的现状可以归纳出两层原因:

1. **实现层**:只有 Metal 渲染器实现了「无 layer 构造」的路径——`MetalRenderer::new_headless` 接受 `layer: None`,不依赖 `CAMetalLayer`、窗口乃至 AppKit。而 Windows 的 DirectX 栈(u6-l2:设备、渲染器、图集三层)与交换链、窗口句柄深度耦合;Linux/Web 走 wgpu,渲染目标绑定真实 surface。
2. **需求层**:视觉快照测试与 `#[gpui::bench]` 基准最早都是 Zed 在 macOS 上的开发设施,其他平台的测试一直用 Scene 断言(`painted_quads`)与 `TestAtlas` 就够了——契约先抽象出来,实现按需补齐。这正是 u2-l1 总结过的「能力探测型默认值」模式:返回 `None`,调用方负责降级。

还要澄清一个极易混淆的点:`MacPlatform` 内部也有一个 `headless: bool` 字段,但那是 u5-l2 讲过的**无头平台**(不开 AppKit 窗口、事件循环直接跑 `CFRunLoopRun`),与本讲的**无头渲染器**毫无继承关系。两者正交,可以自由组合。

#### 4.2.2 核心流程

`current_headless_renderer` 的编译期链条:

```text
gpui_platform 的 feature "test-support"
    │  Cargo.toml: test-support = ["gpui/test-support", "gpui_macos/test-support"]
    ▼
gpui_macos(仅 cfg(target_os = "macos") 时是依赖)开启 test-support
    │  gpui_macos.rs 里 #[cfg(any(test, feature = "test-support"))]
    ▼
gpui_macos::metal_renderer::MetalHeadlessRenderer 可见(再导出自 gpui_apple)
    │
    ▼
current_headless_renderer() 的 #[cfg(target_os = "macos")] 分支:
    Some(Box::new(MetalHeadlessRenderer::new()))
非 macOS 分支:None
```

运行期的 Metal 离屏渲染(截图路径)流程:

```text
MetalHeadlessRenderer::render_scene_to_image(scene, size)
    │ 委托内部的 MetalRenderer(构造时 layer = None)
    ▼
校验尺寸 > 0,更新路径中间纹理(MSAA 用)
    ▼
创建一次性离屏纹理:BGRA8Unorm、StorageMode::Managed(CPU 可读)
    ▼
render_frame(scene, &target_texture, size) 编码全部图元命令
    ▼
若非统一内存(独显):追加 blit synchronize,否则 CPU 读到旧数据
    ▼
commit() + wait_until_completed()(截图路径才同步等待)
    ▼
read_texture_to_image:get_bytes 回读 → BGRA→RGBA 逐像素交换 → RgbaImage
```

#### 4.2.3 源码精读

先看门面构造器:

> [src/gpui_platform.rs:83-97](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/src/gpui_platform.rs#L83-L97) —— `current_headless_renderer()`:整函数受 `#[cfg(feature = "test-support")]` 门控;函数体内两段 `#[cfg]`——macOS 分支 `Some(Box::new(gpui_macos::metal_renderer::MetalHeadlessRenderer::new()))`,其余平台 `{ None }`。与 `current_platform` 的四分支不同,这里只有「有/无」两态。

```rust
#[cfg(feature = "test-support")]
pub fn current_headless_renderer() -> Option<Box<dyn gpui::PlatformHeadlessRenderer>> {
    #[cfg(target_os = "macos")]
    {
        Some(Box::new(gpui_macos::metal_renderer::MetalHeadlessRenderer::new()))
    }

    #[cfg(not(target_os = "macos"))]
    {
        None
    }
}
```

feature 如何一路透传到实现:

> [Cargo.toml:14-19](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/Cargo.toml#L14-L19) —— `test-support = ["gpui/test-support", "gpui_macos/test-support"]`:门面的 feature 同时点亮契约(gpui)与实现(gpui_macos)两侧的 cfg。这是 u1-l3「feature 透传链」的又一实例。

> [Cargo.toml:26-27](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/Cargo.toml#L26-L27) —— `gpui_macos` 只在 `cfg(target_os = "macos")` 目标依赖段里出现,所以非 macOS 目标上那个 `#[cfg(target_os = "macos")]` 分支引用的 crate 根本不参与编译。

> [../gpui_macos/src/gpui_macos.rs:20-25](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_macos/src/gpui_macos.rs#L20-L25) —— `metal_renderer` 模块把 `gpui_apple::metal_renderer::MetalHeadlessRenderer` 以 `gpui_macos::metal_renderer` 的路径再导出,且同样受 `#[cfg(any(test, feature = "test-support"))]` 门控。也就是说 Metal 代码本体住在跨 Apple 平台的 `gpui_apple` crate 里(与 u6-l1 的分层一致)。

唯一实现:

> [../gpui_apple/src/metal_renderer.rs:1595-1626](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_apple/src/metal_renderer.rs#L1595-L1626) —— `MetalHeadlessRenderer` 只是薄壳:内部持有一个 `MetalRenderer`,`new()` 用共享的 `InstanceBufferPool` 调 `MetalRenderer::new_headless`;三个 trait 方法全部一行委托。可见「无头」不是一套新渲染器,而是同一个渲染器的另一种构造方式。

> [../gpui_apple/src/metal_renderer.rs:179-187](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_apple/src/metal_renderer.rs#L179-L187) —— `MetalRenderer::new_headless`:与窗口版 `new`(153-177 行,先造 `CAMetalLayer`)对照,它传 `layer: None` 走 `new_internal`。文档注释明说:不需要 CAMetalLayer、窗口或 AppKit——这也意味着用它写的测试**不受 AppKit 主线程约束**(对比 u6-l1 里 VisualTestAppContext 测试必须 `--ignored --test-threads=1`)。

两条渲染路径的分野:

> [../gpui_apple/src/metal_renderer.rs:564-607](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_apple/src/metal_renderer.rs#L564-L607) —— `render_scene_to_image`:每次新建 `StorageMode::Managed` 的 BGRA8Unorm 纹理(Managed = GPU 渲染、CPU 可读);独显(非统一内存)上必须追加 `blit.synchronize_resource`,注释解释否则 `get_bytes` 读到的是过期的零;然后 `commit()` + `wait_until_completed()` 同步等待,最后回读。

> [../gpui_apple/src/metal_renderer.rs:609-649](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_apple/src/metal_renderer.rs#L609-L649) —— `render_scene`:渲染目标换成 `StorageMode::Private`(仅 GPU 可见)并缓存在 `headless_render_target` 字段里按尺寸复用;`commit()` 之后**立即返回**,注释点明这是在镜像「呈现到真实窗口」的 CPU 行为(不阻塞等 GPU)。基准测量的就是这条路径。

> [../gpui_apple/src/metal_renderer.rs:1240-1267](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_apple/src/metal_renderer.rs#L1240-L1267) —— `read_texture_to_image`:`get_bytes` 按 `bytes_per_row = width * 4` 整块拷回 CPU,再逐像素 `chunk.swap(0, 2)` 把 Metal 的 BGRA 顺序换成 image crate 期望的 RGBA,最后 `RgbaImage::from_raw` 装箱。回读代价 \( W \times H \times 4 \) 字节的拷贝 + 一次通道交换,这也解释了为什么不截图就不该回读。

对照:窗口版截图(需要 layer)与无头平台的 `headless` 布尔:

> [../gpui_apple/src/metal_renderer.rs:534-562](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_apple/src/metal_renderer.rs#L534-L562) —— `MetalRenderer::render_to_image`(layer 版):从 `CAMetalLayer` 取 `next_drawable` 的纹理当目标,同样提交等待后回读。macOS 真实窗口的截图走这条路([../gpui_macos/src/window.rs:2143-2146](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_macos/src/window.rs#L2143-L2146) 里 `MacWindow::render_to_image` 一行委托);注意 539 行注释:「无头渲染请改用 render_scene_to_image」——layer 为 `None` 时这个方法直接报错,两条路径互斥。

> [../gpui_macos/src/platform.rs:166-194](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_macos/src/platform.rs#L166-L194) —— `MacPlatformState` 的 `headless: bool` 字段(173 行):这是**无头平台**的开关,与无头渲染器无关。

> [../gpui_macos/src/platform.rs:491-518](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_macos/src/platform.rs#L491-L518) —— `MacPlatform::run` 的 headless 分支:立刻同步执行 `on_finish_launching` 后直接 `CFRunLoopRun()`,跳过 NSApplication 启动流程——u5-l2 讲过的「逻辑存在但不上屏」在 macOS 上的形态。

#### 4.2.4 代码实践(平台差异观测)

1. **实践目标**:亲手验证 `current_headless_renderer` 在你的操作系统上返回 `Some` 还是 `None`,并理解其编译期根源。
2. **操作步骤**(示例代码,放在任意依赖 `gpui_platform` 的测试里):
   ```rust
   #[test]
   #[cfg(feature = "test-support")]
   fn probe_headless_renderer() {
       let renderer = gpui_platform::current_headless_renderer();
       println!("headless renderer: {}",
           if renderer.is_some() { "Some" } else { "None" });
   }
   ```
   用 `cargo test -p <你的crate> --features gpui_platform/test-support probe_headless_renderer -- --nocapture` 运行。
3. **需要观察的现象**:macOS 上打印 `Some`;Linux/Windows 上打印 `None`。再读一遍 [Cargo.toml:26-27](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/Cargo.toml#L26-L27),注意非 macOS 目标上 `gpui_macos` 不编译,`None` 分支是唯一幸存代码。
4. **预期结果**:输出与平台严格对应;同一段代码无需任何本地 `#[cfg]` 就能跨平台编译——这正是门面 crate 的意义。本讲义在 Linux CI 环境编写,`Some` 分支的行为为**待本地验证**(需 macOS 机器)。

#### 4.2.5 小练习与答案

**练习 1**:如果不开启 `test-support` feature,直接调用 `gpui_platform::current_headless_renderer()` 会发生什么?

**参考答案**:编译错误。函数整体受 `#[cfg(feature = "test-support")]` 门控(src/gpui_platform.rs:84),feature 关闭时函数根本不存在,而不是返回 `None`。「平台有没有实现」用 `Option` 表达,「这套测试设施要不要编入」用 feature 表达——两层开关各管一件事。

**练习 2**:`MetalHeadlessRenderer` 为什么要持有 `InstanceBufferPool` 的共享 `Arc`,而不是自己新建一个?

**参考答案**:`InstanceBufferPool` 回收复用实例缓冲区(metal_renderer.rs 1529-1532 行注释:Metal 会保留已编码资源直到命令缓冲完成,只有最终最大的缓冲值得留池)。共享 Arc 让无头渲染器与同进程的其他(窗口)渲染器共用一个池,基准测量时的缓冲行为与真实运行保持一致——否则测出来的分配开销不能反映生产路径。

**练习 3**:在 Apple Silicon(统一内存)与 Intel 独显 Mac 上,`render_scene_to_image` 的执行路径差在哪一步?

**参考答案**:差在 blit 同步。统一内存上 CPU 与 GPU 共享同一块物理内存,`get_bytes` 直接可读;独显上渲染目标是 Managed 纹理,GPU 写完后 CPU 侧缓存可能过期,必须先 `blit.synchronize_resource` 做一次显式同步,否则读到过期数据(metal_renderer.rs:593-600 行的注释明确警告「get_bytes returns stale zeros」)。

### 4.3 模块三:HeadlessAppContext——把平台、文本系统与渲染器组装起来

#### 4.3.1 概念说明

有了契约和实现,还缺一个「不用真平台就能跑 GPUI 应用逻辑」的容器。`HeadlessAppContext`(住在 gpui 的 `app/headless_app_context.rs`,同样 test-support 门控)就是这个组装者,它把三样东西拼在一起:

1. **`TestPlatform` + `TestDispatcher`**:u8-l4 会详讲的确定性测试平台——任务不靠真事件循环,靠 `run_until_parked()` 手动泵、`advance_clock()` 推进虚拟时钟。
2. **真实的 `PlatformTextSystem`**:由调用方注入(u8-l1 学过四套字体栈:macOS 用 MacTextSystem、Linux 用 CosmicTextSystem、Windows 用 DirectWrite)。文档注释点明了它的定位:取代旧的 macOS 专属 `HeadlessMetalAppContext`,让「需要真实字形度量的测试」在任何平台都能跑。
3. **可选的无头渲染器工厂**:一个返回 `Option<Box<dyn PlatformHeadlessRenderer>>` 的闭包。给了,`capture_screenshot` 才可用;不给(或平台返回 `None`),窗口照常开、布局与整形照常算,只是拿不到像素。

为什么用「工厂闭包」而不是单个渲染器实例?因为渲染器是**每窗口一份**的(`TestPlatform::open_window` 每次开窗都调一次工厂),与 u8-l2 的所有权结论「渲染器每窗口一份、图集每渲染器一份」对齐。

#### 4.3.2 核心流程

`HeadlessAppContext` 的组装与截图全链路:

```text
HeadlessAppContext::with_platform(text_system, asset_source, renderer_factory)
    │  TestDispatcher::new(SEED 环境变量)
    │  前台/后台执行器共享同一个 dispatcher
    │  TestPlatform::with_platform(执行器×2, text_system, Some(factory))
    │  TextSystem::new(带缓存与回退字体栈的那层,u8-l1)
    │  App::new_app(platform, assets, FakeHttpClient)
    │  mode = GpuiMode::test()
    ▼
cx.open_window(size, build_root)          ← WindowOptions: show=false, focus=false
    │  TestPlatform::open_window → factory() → TestWindow 持有渲染器
    │  sprite_atlas 直接取渲染器的(否则 TestAtlas)
    │  App::open_window 返回前强制绘制第一帧 → renderer.render_scene(...)
    ▼
cx.run_until_parked() / advance_clock(...)  ← 确定性驱动
    ▼
cx.capture_screenshot(window)
    │  app.update_window → Window::render_to_image()
    │  → TestWindow::render_to_image → renderer.render_scene_to_image(...)
    ▼
RgbaImage(可 .save() 成 PNG)
    ▼
Drop:app.shutdown()(先关窗口、释放实体句柄,再跑泄漏检测)
```

#### 4.3.3 源码精读

组装:

> [../gpui/src/app/headless_app_context.rs:65-101](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/app/headless_app_context.rs#L65-L101) —— `with_platform` 构造函数:种子取自 `SEED` 环境变量(控制 TestDispatcher 的伪随机决策,保证可复现);`TestPlatform::with_platform` 收下 `Some(renderer_factory)`;HTTP 客户端用 `FakeHttpClient::with_404_response()`;最后 `app.borrow_mut().mode = GpuiMode::test()`。

```rust
let platform = TestPlatform::with_platform(
    background_executor.clone(),
    foreground_executor.clone(),
    platform_text_system.clone(),
    Some(renderer_factory),
);
let text_system = Arc::new(TextSystem::new(platform_text_system));
let http_client = http_client::FakeHttpClient::with_404_response();
let app = App::new_app(platform, asset_source, http_client);
```

模块可见性(它不是运行时 API):

> [../gpui/src/app.rs:67-68](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/app.rs#L67-L68) 与 [../gpui/src/app.rs:33-34](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/app.rs#L33-L34) —— `mod headless_app_context` 与它的 `pub use` 都受 `#[cfg(any(test, feature = "test-support"))]` 门控:`gpui::HeadlessAppContext` 只在启用 test-support 时存在。

开窗(注意 `show: false`):

> [../gpui/src/app/headless_app_context.rs:104-126](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/app/headless_app_context.rs#L104-L126) —— `open_window(size, build_root)`:把尺寸包装成原点 (0,0) 的 `WindowBounds::Windowed`,`WindowOptions` 显式设 `focus: false`、`show: false`——窗口存在于实体与布局层面,但从不显示,这就是「无头」的第三种含义(逻辑窗口)。

截图入口:

> [../gpui/src/app/headless_app_context.rs:164-171](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/app/headless_app_context.rs#L164-L171) —— `capture_screenshot(window)`:`app.update_window(window, |_, window, _| window.render_to_image())`,一行接通 4.1 节的整条链;文档注释明确「要求构造时提供的工厂返回 `Some`」。

确定性驱动与清理:

> [../gpui/src/app/headless_app_context.rs:128-146](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/app/headless_app_context.rs#L128-L146) —— `run_until_parked`/`advance_clock` 转发 `TestDispatcher` 的泵与虚拟时钟;`allow_parking`/`forbid_parking` 临时允许阻塞在真实 I/O 上(比如异步加载资源)。

> [../gpui/src/app/headless_app_context.rs:189-195](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/app/headless_app_context.rs#L189-L195) —— `Drop` 实现里先 `app.shutdown()`:注释解释是为了在 `LeakDetector` 跑之前关掉窗口、释放实体句柄,否则会误报泄漏。用完即弃也会走这条清理路径。

渲染器如何进入测试窗口:

> [../gpui/src/platform/test/platform.rs:399-413](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/platform/test/platform.rs#L399-L413) —— `TestPlatform::open_window` 每次都调 `headless_renderer_factory`(404 行)再构造 `TestWindow`:工厂是「每窗口一次」,开三个窗口就有三个独立渲染器。

> [../gpui/src/platform/test/window.rs:72-82](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/platform/test/window.rs#L72-L82) —— `TestWindow::new` 里图集的选择:有渲染器就用 `r.sprite_atlas()`(真实装箱、真实纹理),没有就退回 `TestAtlas`(纯记录的假图集)。这直接决定了「字形会不会真的进显存」。

真实用户:基准设施。

> [../gpui/src/app/bench_context.rs:21-59](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/app/bench_context.rs#L21-L59) —— `bench_platform(renderer_factory, text_system)`:文档注释说明渲染器存在时,基准里的每一帧都会经真实图集光栅化并提交 GPU,「quad/sprite 的性能回归会出现在测量里」;为 `None` 时呈现直接丢弃 Scene;并明确写出「目前只有 macOS 提供无头渲染器,其他平台的测量不含 GPU 提交」。

> [../gpui_macros/src/bench.rs:93-99](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_macros/src/bench.rs#L93-L99) —— `#[gpui::bench]` 宏展开出的代码:每个被测函数都拿到 `BenchAppContext`,其平台由 `gpui::bench_platform(Some(Box::new(|| gpui_platform::current_headless_renderer())), gpui_platform::current_platform(true).text_system())` 构造——门面构造器在生产代码里最直接的消费者。文本系统取自 `current_platform(true)`,这个搭配正是下一节综合实践要仿写的模式。

#### 4.3.4 代码实践(最小截图骨架)

1. **实践目标**:不动声色地验证「无渲染器时整条链仍可运行、只是截图报错」,为综合实践做铺垫。
2. **操作步骤**(示例代码):在一个启用 test-support 的测试里,用 `HeadlessAppContext::new`(不传渲染器工厂)开窗、驱动、再截图:
   ```rust
   // 示例代码:需要 gpui 启用 "test-support" feature
   #[test]
   fn headless_context_without_renderer() {
       let text_system = gpui_platform::current_platform(true).text_system();
       let mut cx = gpui::HeadlessAppContext::new(text_system);

       let window = cx
           .open_window(gpui::size(gpui::px(200.), gpui::px(100.)), |_, cx| {
               cx.new(|_| HelloView)
           })
           .unwrap();
       cx.run_until_parked();

       let result = cx.capture_screenshot(window.into());
       println!("capture result: {:?}", result.as_ref().map(|img| img.dimensions()));
   }
   ```
3. **需要观察的现象**:开窗、`run_until_parked` 都成功(布局与实体管线正常);`capture_screenshot` 返回 `Err`,错误信息正是 `render_to_image not available: no HeadlessRenderer configured`(test/window.rs:418)。
4. **预期结果**:macOS 之外的所有平台都应看到这条 Err;macOS 上 `HeadlessAppContext::new` 同样会报错,因为 `new` 写死了 `|| None` 工厂(headless_app_context.rs:52)——想要截图必须用 `with_platform`。**待本地验证**(本环境为 Linux,可验证 None 分支;macOS 行为待有机器时确认)。

#### 4.3.5 小练习与答案

**练习 1**:`HeadlessAppContext` 为什么同时需要 `platform_text_system` 和 `TextSystem::new(platform_text_system)` 两层?直接暴露一层不行吗?

**参考答案**:两层分工不同(u8-l1 讲过):`PlatformTextSystem` 是平台字体引擎(MacTextSystem/CosmicTextSystem/DirectWrite),`TextSystem` 是 gpui 主 crate 包在它外面的缓存层(字体 ID 缓存、回退字体栈)。`TestPlatform` 需要前者是因为 `Platform::text_system` 契约要求;`HeadlessAppContext::text_system()` 把后者暴露给测试,让测试经由带缓存的路径做排版,行为更接近生产。

**练习 2**:为什么渲染器工厂的类型是 `Fn() -> Option<...>` 而不是 `Option<Box<dyn ...>>`?

**参考答案**:`TestPlatform::open_window` 每开一个窗口都要调用一次工厂(test/platform.rs:404),因为渲染器与图集是每窗口一份、且 `&mut self` 独占使用;如果只持有一个实例就无法同时服务多个窗口。`Option` 嵌在返回值里还让工厂自身可以按平台条件化——`current_headless_renderer` 在非 macOS 上就是「永远返回 None 的工厂」。

**练习 3**:对比 `VisualTestAppContext`(u6-l1 提过,macOS 测试必须单线程主线程运行)与 `HeadlessAppContext`:后者的测试为什么没有这个限制?

**参考答案**:`HeadlessAppContext` 底层是 `TestPlatform`(不碰 AppKit 的 NSApplication/NSWindow),截图用 `MetalHeadlessRenderer` 也不需要 layer、窗口或 AppKit(metal_renderer.rs:179-182 的文档注释)。不触碰 AppKit 就不受它的主线程约束,测试可以并行跑——这正是它取代旧 `HeadlessMetalAppContext` 的动机之一。

## 5. 综合实践

**任务**:写一个完整的离屏截图示例——用 `HeadlessAppContext` 打开一个 400×300 的视图,渲染「深色背景 + 一行文字」,把结果保存为 PNG;在非 macOS 平台上以可编译的降级骨架运行并记录平台差异。

**准备(示例代码)**:新建独立小 crate `headless-shot`,目录结构:

```text
headless-shot/
├── Cargo.toml
└── tests/
    └── screenshot.rs
```

`Cargo.toml`:

```toml
[package]
name = "headless-shot"
version = "0.1.0"
edition = "2021"

[dependencies]
gpui = { path = "../zed/crates/gpui", features = ["test-support"] }
gpui_platform = { path = "../zed/crates/gpui_platform", features = ["test-support"] }
image = "0.25"
```

> Linux 上建议再给 `gpui_platform` 加 `wayland` 或 `x11` feature——gpui_linux 的文本系统是 feature 门控的:任一开启时用 `CosmicTextSystem`,否则退化为 `NoopTextSystem`([../gpui_linux/src/linux/platform.rs:149-152](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_linux/src/linux/platform.rs#L149-L152)),那时截图里的文字会是空白。macOS 上同理建议加 `font-kit` feature 以启用 `MacTextSystem`([../gpui_macos/src/platform.rs:200-211](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_macos/src/platform.rs#L200-L211))。

`tests/screenshot.rs`(示例代码,仿照 [../gpui/src/app/headless_app_context.rs:30-37](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/app/headless_app_context.rs#L30-L37) 的文档示例与 [../gpui_macros/src/bench.rs:93-99](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_macros/src/bench.rs#L93-L99) 的宏展开模式):

```rust
use gpui::{App, Context, Window, div, px, rgb, size};
use std::sync::Arc;

struct HelloView;

impl gpui::Render for HelloView {
    fn render(&mut self, _window: &mut Window, _cx: &mut Context<Self>) -> impl gpui::IntoElement {
        div()
            .size_full()
            .bg(rgb(0x1e1e2e))
            .child("Hello, headless!")
    }
}

#[test]
fn render_a_view_offscreen() {
    // 文本系统取自当前平台的无头形态(u8-l1 的四套字体栈之一)
    let text_system = gpui_platform::current_platform(true).text_system();

    // 无头渲染器工厂:macOS 返回 Metal 实现,其他平台返回 None
    let mut cx = gpui::HeadlessAppContext::with_platform(
        text_system,
        Arc::new(()),
        gpui_platform::current_headless_renderer,
    );

    let window = cx
        .open_window(size(px(400.), px(300.)), |_, cx| cx.new(|_| HelloView))
        .unwrap();

    // 泵完所有待办任务,确保首帧编码完成
    cx.run_until_parked();

    match cx.capture_screenshot(window.into()) {
        Ok(image) => {
            let path = "/tmp/gpui-headless.png";
            image.save(path).unwrap();
            println!("saved {}x{} to {path}", image.width(), image.height());
        }
        Err(err) => {
            // 非 macOS 的预期路径:no HeadlessRenderer configured
            eprintln!("capture failed on this platform: {err}");
        }
    }
    // cx 在此 drop → app.shutdown() → 实体句柄释放、LeakDetector 不误报
}
```

**操作步骤**:

1. 按上面的布局创建 crate(路径按你本地 zed 仓库位置调整);`image` crate 用于 `RgbaImage::save`。
2. Linux 上运行:`cargo test -p headless-shot --features gpui_platform/test-support render_a_view_offscreen -- --nocapture`(若你的 Cargo.toml 已把 feature 写进依赖,去掉 `--features`)。
3. 观察两种结果:测试本身应当**通过**(开窗、布局、驱动都没问题);macOS 之外 `capture_screenshot` 走 `Err` 分支,打印的错误串应包含 `no HeadlessRenderer configured`。
4. 在 macOS 上(若可用)重复运行,确认 `/tmp/gpui-headless.png` 生成,用图片查看器打开:深色底、一行浅色文字,尺寸 400×300。
5. 把两次运行的结果填进平台差异表(见下)。

**需要观察的现象与平台差异记录表**:

| 观察点 | macOS | Linux / Windows |
| --- | --- | --- |
| `current_headless_renderer()` | `Some`(Metal) | `None` |
| `open_window` + `run_until_parked` | 成功 | 成功(逻辑窗口照常工作) |
| `capture_screenshot` | `Ok(RgbaImage)` | `Err("... no HeadlessRenderer configured")` |
| 产出的 PNG | 400×300,含真实字形 | 无文件 |
| 文本渲染质量 | 取决于 font-kit feature | 取决于 wayland/x11 feature(否则 Noop,文字空白) |

**预期结果**:你将亲证本讲的核心结论——无头渲染器只是「可选增强」:没有它,`HeadlessAppContext` 依旧是完整的确定性测试容器(布局、整形、实体更新全部可用),只有「拿像素」这一步降级。非 macOS 行为已可在本讲义的 Linux 环境推演,但**完整运行结果待本地验证**(尤其 macOS 的 PNG 产出)。

**延伸挑战**(可选):给 `HelloView` 加一个 `cx.notify()` 驱动的计数器,用 `advance_clock` 推进一个 500ms 的定时器后再截一张图,对比两张 PNG 的差异——这会逼你用上 4.3 节的全部确定性驱动 API。

## 6. 本讲小结

- **两个正交概念**:「无头平台」(u5-l2 的 `HeadlessClient`/`MacPlatform` 的 headless 布尔)解决「没有显示器也要跑管线」;「无头渲染」(`PlatformHeadlessRenderer`)解决「没有窗口也要产出真实像素」。前者不需要 GPU,后者必须有。
- **契约三方法**:`render_scene_to_image`(Managed 纹理 + 同步等待 + 回读,供视觉断言)、`render_scene`(Private 纹理复用 + 提交即返回,供基准测量)、`sprite_atlas`(让测试窗口复用渲染器的真实图集)。
- **macOS 独苗**:`current_headless_renderer` 只在 macOS 返回 `Some`,根因是只有 Metal 渲染器实现了 `new_headless`(layer 为 `None`,不碰 AppKit);`Option` 即能力探测,调用方自行降级,契约不泄漏平台差异。
- **组装者**:`HeadlessAppContext` = `TestPlatform`(确定性调度)+ 调用方注入的真实文本系统 + 可选渲染器工厂(每窗口调用一次);`capture_screenshot` 一行接通 `Window::render_to_image → TestWindow → renderer` 全链。
- **feature 双层开关**:`test-support` 决定这整套 API 是否编译进来;平台 cfg 决定运行期拿到 `Some` 还是 `None`。
- **回读是分水岭**:截图路径独享同步等待与 \( W \times H \times 4 \) 字节的像素回读(外加 BGRA→RGBA 交换),呈现路径一概不做——这就是契约拆成两个方法的全部理由。

## 7. 下一步学习建议

- 下一讲 **u8-l4(test-support 与可视化测试)** 会展开本讲反复借力的 `TestPlatform`/`TestDispatcher`/`run_until_parked`/`advance_clock` 的完整设计,以及 `TestWindow` 的 `simulate_scheduled_frame` 等帧调度模拟设施——把 4.3 节里「黑盒」的确定性调度讲透。
- 若你想给 Linux/Windows 补一个无头渲染器实现(毕业实践方向的进阶挑战),从 [../gpui/src/platform.rs:991-1010](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/platform.rs#L991-L1010) 的契约入手,参考 `gpui_wgpu` 的渲染器如何拿到 wgpu 设备,再对照 `MetalHeadlessRenderer` 的两层方法实现纹理目标与回读;u8-l2 的图集所有权结论(图集每渲染器一份)是你设计的边界条件。
- 阅读 [../gpui/src/app/bench_context.rs](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/app/bench_context.rs) 全文,观察 `BenchAppContext` 如何在基准里同时利用无头渲染器(GPU 提交)与帧预算统计,这是本讲两个真实用户中更复杂的一个。

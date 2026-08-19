# gpui_wgpu 是什么：项目定位与生态位置

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `gpui_wgpu` 这个 crate 在 Zed 仓库依赖链（`gpui` → `gpui_wgpu` ← `gpui_linux` / `gpui_web`）中的位置和职责边界。
2. 区分 crate 内部的两条主线：**渲染主线**（`WgpuContext` / `WgpuRenderer` / `WgpuAtlas` + WGSL 着色器）与**文本主线**（`CosmicTextSystem`）。
3. 只读 `Cargo.toml` 就能说出每个关键第三方依赖（wgpu、cosmic-text、swash、etagere、bytemuck、raw-window-handle 等）的用途。
4. 会用 `grep` / `rg` 在整个 `crates/` 目录里找出谁在消费本 crate，并画出调用关系图。

## 2. 前置知识

本讲是整个手册的第一篇，不要求你了解 Zed 的任何内部实现，但以下几个概念最好先有个直觉：

- **crate（Rust 的编译单元）**：类比其他语言里的「一个包 / 一个库」。Zed 仓库是一个巨大的 Cargo workspace，里面有几百个 crate，`gpui_wgpu` 是其中之一。
- **GPU 渲染管线（pipeline）**：GPU 绘制图形的固定流程——顶点着色器把几何坐标变换到屏幕空间，片元着色器决定每个像素的颜色。管线在创建时就固定了混合方式、绑定的资源布局等。
- **wgpu**：Rust 生态里的跨平台图形抽象层，是 WebGPU 标准的实现。写一份代码，可以在 Vulkan（Linux）、Metal（macOS）、DX12（Windows）、WebGPU / WebGL2（浏览器）上运行。它是本 crate 名字的由来。
- **精灵图集（sprite atlas）**：把很多小图（字形、图标）打包进一张大纹理，绘制时用纹理坐标取样，避免为每个小图单独切换纹理。字形光栅化的结果就放在图集里。
- **文本整形（text shaping）**：把「字符序列 + 字体」转换成「该用哪个字形、放在哪个位置」的过程，要处理连字、双向文本（阿拉伯语、希伯来语）、字体回退（当前字体缺字时换字体）。
- **平台 trait 模式**：`gpui` 定义平台无关的抽象接口（如 `PlatformAtlas`、`PlatformTextSystem`），各平台 crate 提供实现。这样 `gpui` 核心 code 不需要知道自己在哪个操作系统上运行——这是理解本 crate 存在意义的关键。

## 3. 本讲源码地图

本 crate 位于 `crates/gpui_wgpu/`，全部源码加起来约 6581 行（含注释与测试）：

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| `src/gpui_wgpu.rs` | 10 | 库根：声明 4 个私有模块并再导出公共 API |
| `src/wgpu_context.rs` | 606 | GPU 上下文：instance / adapter / device / queue 的创建与适配器选择 |
| `src/wgpu_renderer.rs` | 2243 | 渲染器主体：管线创建、一帧的绘制、错误恢复 |
| `src/wgpu_atlas.rs` | 527 | 精灵图集：实现 `gpui` 的 `PlatformAtlas`，管理纹理与 tile 分配 |
| `src/cosmic_text_system.rs` | 1410 | 文本系统：实现 `gpui` 的 `PlatformTextSystem`，字体加载、排版、光栅化 |
| `src/shaders.wgsl` | 1362 | 共享 WGSL 着色器：公共结构、各图元的顶点/片元函数 |
| `src/shaders_storage.wgsl` | 42 | 实例数据传输变体：storage buffer 路径（原生平台） |
| `src/shaders_webgl.wgsl` | 221 | 实例数据传输变体：纹理路径（WebGL2 无 storage buffer） |
| `src/shaders_subpixel.wgsl` | 56 | 亚像素文本渲染着色器（依赖双源混合特性） |
| `benches/layout_line.rs` | 104 | criterion 基准：测量 `layout_line` 排版性能 |

本讲重点精读其中两个：`Cargo.toml` 和 `src/gpui_wgpu.rs`，其余文件只看「骨架」（struct / trait 定义）。

## 4. 核心概念与源码讲解

### 4.1 Cargo.toml：一份依赖清单就是一张架构图

#### 4.1.1 概念说明

对一个图形库来说，`Cargo.toml` 里的依赖列表几乎就是它的架构图：依赖了 wgpu 说明它做 GPU 渲染，依赖了 cosmic-text 和 swash 说明它做文本，依赖了 etagere 说明它做图集空间分配。

读 `Cargo.toml` 建议按这个顺序：

1. `[package]`：crate 叫什么、版本多少。
2. `[lib] path`：库根文件在哪。
3. `[features]`：有哪些编译期开关。
4. `[dependencies]`：核心依赖。
5. `[target.'cfg(...)'.dependencies]`：平台特定依赖（这里是「非 wasm」与「wasm」两组）。
6. `[dev-dependencies]` 与 `[[bench]]`：测试与基准用什么。

#### 4.1.2 核心流程

```text
阅读 Cargo.toml
  ├─ [package] + [lib]        → 确认 crate 名与库根路径
  ├─ [features]               → font-kit 是可选能力（Linux 字体匹配）
  ├─ [dependencies]           → 通用依赖（原生与 wasm 都要用）
  ├─ [target.'not(wasm)']     → pollster：把异步初始化变成同步阻塞
  ├─ [target.'wasm']          → wasm-bindgen / web-sys / wgpu+webgl
  └─ [dev-dependencies] + [[bench]] → naga 校验着色器、criterion 跑基准
```

#### 4.1.3 源码精读

**库根路径配置。** 按 Zed 仓库规范（见根目录 `CLAUDE.md`：「Never create files with mod.rs paths」），crate 不用默认的 `lib.rs`，而是显式指定与 crate 同名的文件作为库根：

[Cargo.toml:L11-L16](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/Cargo.toml#L11-L16)

这段声明了库根是 `src/gpui_wgpu.rs`，并且只有一个非默认 feature：`font-kit`。

**核心依赖清单。** 下面这段几乎每一行都对应 crate 的一个职责：

[Cargo.toml:L18-L35](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/Cargo.toml#L18-L35)

| 依赖 | 一句话用途 |
| --- | --- |
| `gpui` | Zed 的平台无关 UI 框架，本 crate 实现它的 `PlatformAtlas` / `PlatformTextSystem` 抽象，并消费它的 `Scene` / `PrimitiveBatch` 绘制数据 |
| `wgpu` | 跨平台图形抽象层（WebGPU 的 Rust 实现），所有 GPU 操作的入口 |
| `cosmic-text` | 文本系统库：内含字体数据库 fontdb 与整形引擎，是 `CosmicTextSystem` 的地基 |
| `swash` | 字体解析与字形光栅化（把字形轮廓变成像素位图），`rasterize_glyph` 用它 |
| `etagere` | 2D 矩形装箱分配器，图集用它给每个字形 / 图标分配纹理内的位置 |
| `bytemuck` | 把 Rust 结构体安全地按字节 reinterpret（`Pod` / `Zeroable`），写入 uniform / storage buffer 必需 |
| `raw-window-handle` | 平台无关的窗口句柄抽象，`WgpuRenderer::new` 靠它从任意窗口创建绘制表面 |
| `parking_lot` | 更快更易用的锁，`CosmicTextSystem` 内部用 `RwLock` 保护状态 |
| `pollster` | 同步阻塞执行器（仅原生平台），把 wgpu 异步的 `request_device` 变成同步调用 |
| `unicode-bidi` / `unicode-segmentation` | 双向文本算法与字素簇切分，排版时处理 RTL 文本与组合字符 |
| `anyhow` / `log` / `profiling` / `itertools` / `smallvec` / `collections` / `gpui_util` | 错误处理、日志、性能打点、迭代工具、栈上小数组、Zed 自有容器与工具 |

**可选的 font-kit。** 注意它来自 Zed 自己维护的 fork，且注释里有明确的发布警告：

[Cargo.toml:L37-L39](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/Cargo.toml#L37-L39)

只有存在多个字体来源的平台（典型是 Linux：系统字体 + 用户字体目录）才需要它做精细的字体属性匹配，所以做成可选 feature，由 `gpui_linux` 按需开启（后文 4.3 会给出消费证据）。

**平台条件依赖。** 原生与 wasm 两组成对的依赖段：

[Cargo.toml:L41-L49](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/Cargo.toml#L41-L49)

两个要点：

- 原生平台多一个 `pollster`（阻塞等待异步初始化）；wasm 上无法阻塞，改用 `wasm-bindgen-futures` 异步等待。
- wgpu 在 `[dependencies]` 里已经出现，wasm 段再次出现是为了**叠加 `webgl` feature**——只有浏览器目标才编进 WebGL2 后端回退代码。

**开发依赖与基准。** 这一段能看出 crate 的质量保障手段：

[Cargo.toml:L51-L57](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/Cargo.toml#L51-L57)

`naga`（带 `wgsl-in` feature）用于在 CPU 侧解析校验 WGSL 着色器——不需要 GPU 就能在测试里发现着色器语法错误；`criterion` 驱动 `benches/layout_line.rs` 排版基准。

#### 4.1.4 代码实践

**实践目标**：把 `Cargo.toml` 的依赖清单整理成带用途注释的表格，作为后续阅读的「字典」。

**操作步骤**：

1. 打开 `crates/gpui_wgpu/Cargo.toml`，从上到下通读一遍。
2. 按 4.1.3 的表格样式，为每个依赖写一句自己的话（不要照抄本讲义，用自己的理解重写；写不出来的那个就是你需要补的概念）。
3. 对每个「不认识的依赖」，去 crates.io 或 docs.rs 搜它的 README，补全描述。
4. 特别思考：为什么 `wgpu` 出现了两次（`[dependencies]` 与 wasm 目标段）？

**需要观察的现象**：`[dependencies]` 与两段 `[target...]` 中依赖集合的差异；`font-kit` 前后的注释说明了什么约束。

**预期结果**：得到一张约 15 行的依赖表，且能回答「本 crate 的 GPU 部分、文本部分、图集部分分别主要靠哪些库」。

（本实践为纯源码阅读型，不涉及编译运行，无需本地验证命令输出。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 wgpu 的 `webgl` feature 只写在 wasm 目标依赖里，而不是写在 `[features]` 里让所有人可用？

**答案**：Cargo 的 feature 是整个依赖图全局统一的。若默认开启 `webgl`，原生平台构建也会编入 WebGL 后端代码，白白增加体积和编译时间。写在 `cfg(target_family = "wasm")` 段里，只有浏览器目标才叠加这个 feature，原生构建保持干净。

**练习 2**：`font-kit` 为什么指向 `zed-industries/font-kit` 这个 fork，而不是 crates.io 上的官方 font-kit？注释里的 WARNING 提醒什么？

**答案**：Zed 需要修改 font-kit 的行为来满足自己的字体匹配需求，官方版本发布节奏不受 Zed 控制，所以维护了一个 fork 并以 `zed-font-kit` 名义发布。WARNING 提醒：一旦改动这个依赖（换 rev 或改代码），必须发布新版本的 zed-font-kit 到 crates.io，否则已发布的 gpui_wgpu 版本会引用不到。

**练习 3**：`pollster` 为什么只在非 wasm 目标依赖？

**答案**：wgpu 请求 adapter / device 的 API 是异步的。原生程序（如窗口创建时同步初始化渲染器）可以用 `pollster` 阻塞式地等待 Future 完成；而 wasm 在浏览器主线程上不能阻塞，只能用 `wasm-bindgen-futures` 异步等待，所以两边依赖不同的工具。

### 4.2 gpui_wgpu.rs：10 行的库根与模块划分

#### 4.2.1 概念说明

Rust crate 的库根文件负责声明模块树。本 crate 的库根只有 10 行，却规定了整个 crate 的对外 API 面：所有子模块都是**私有**的 `mod`，再通过 `pub use` 把其中的公共项**汇聚**到库根导出。这叫「re-export 汇聚模式」。

它的好处：

- 外部使用者只看到 `gpui_wgpu::WgpuRenderer` 这样扁平的路径，不暴露内部文件组织。
- 将来在内部拆分、合并、重命名模块，只要 re-export 不变，下游代码零改动。

#### 4.2.2 核心流程

```text
gpui_wgpu.rs（库根）
  ├─ mod cosmic_text_system;   ─pub use→ CosmicTextSystem 等
  ├─ mod wgpu_atlas;           ─pub use→ WgpuAtlas 等
  ├─ mod wgpu_context;         ─pub use→ WgpuContext、WgpuBackend、CompositorGpuHint 等
  ├─ mod wgpu_renderer;        ─pub use→ GpuContext、WgpuRenderer、WgpuSurfaceConfig（按名导出）
  └─ pub use wgpu;             ──再导出依赖的 wgpu 本身
```

注意最后一项 `pub use wgpu;`：下游可以写 `use gpui_wgpu::wgpu;`，保证拿到的是与本 crate 编译时**完全相同版本**的 wgpu 类型——两个不同版本的 wgpu 里 `wgpu::Texture` 是不兼容的类型。后面会看到 `gpui_web`、`gpui_linux` 正是这么用的。

#### 4.2.3 源码精读

完整的库根，一行不多：

[src/gpui_wgpu.rs:L1-L10](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/gpui_wgpu.rs#L1-L10)

这段代码做了三件事：

1. 第 1–4 行声明四个私有模块，模块名即文件名（`cosmic_text_system.rs`、`wgpu_atlas.rs`、`wgpu_context.rs`、`wgpu_renderer.rs`）。
2. 第 6–9 行用 glob 再导出前三个模块的全部公共项；`wgpu_renderer` 则按名字只导出三个类型——因为这个文件里还有大量 `struct GlobalParams` 之类的内部实现细节，不打算对外暴露。
3. 第 7 行 `pub use wgpu;` 把 wgpu 依赖本身也纳入公共 API 面。

一个佐证「按名导出是有意的」的细节：`wgpu_renderer.rs` 里定义了 `GlobalParams`、`WgpuPipelines`、`WgpuResources` 等大量类型，但只有 `GpuContext` / `WgpuRenderer` / `WgpuSurfaceConfig` 三个出现在导出清单里（见 [src/wgpu_renderer.rs:L206-L234](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs#L206-L234) 中 `WgpuRenderer` 的定义，它是 `pub struct`，而同文件的 `WgpuResources` 是私有 `struct`）。

#### 4.2.4 代码实践

**实践目标**：验证「库根导出面」与「内部实现」的边界，并跑通本 crate 的编译检查。

**操作步骤**：

1. 运行 `cargo doc -p gpui_wgpu --no-deps`，然后打开 `target/doc/gpui_wgpu/index.html`（或加 `--open`）。
2. 数一数文档里列出的公共类型和函数，对照 `gpui_wgpu.rs` 的 4 个 `pub use`。
3. 运行 `cargo check -p gpui_wgpu` 确认当前工具链能通过编译（首次会编译依赖，耗时较长）。
4. 再运行 `cargo check -p gpui_wgpu --features font-kit`，对比输出。

**需要观察的现象**：`cargo doc` 生成的公共 API 清单远小于源码里的类型总数；`--features font-kit` 会额外拉取 zed-font-kit 依赖并编译。

**预期结果**：公共 API 大致就是 `CosmicTextSystem`、`WgpuAtlas`、`WgpuContext`、`WgpuBackend`、`CompositorGpuHint`、`WgpuRenderer`、`WgpuSurfaceConfig`、`GpuContext`、`WgpuTextureInfo`、再导出的 `wgpu` 以及少量函数 / wasm 专用类型（`WebBackendPreference`、`PreparedWebGraphics` 仅在 wasm 目标编译）。若编译失败请先确认 Rust 工具链满足 Zed 仓库要求（见仓库根 README），结果待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：四个模块为什么用私有 `mod` + `pub use *`，而不是直接 `pub mod wgpu_renderer;`？

**答案**：直接 `pub mod` 会把内部文件结构变成公共 API 的一部分，下游会写出 `gpui_wgpu::wgpu_renderer::WgpuRenderer` 这样的路径，将来内部重组（比如把 wgpu_renderer 拆成多个文件）就是破坏性变更。私有模块 + 库根汇聚让内部结构可以自由演化。

**练习 2**：`pub use wgpu;` 这一行解决什么问题？给出一个真实的下游用法。

**答案**：解决 wgpu 版本对齐问题——下游通过 `gpui_wgpu::wgpu` 引用的类型一定与本 crate 内部用的 wgpu 是同一份，不会出现「同一个 `wgpu::Surface` 类型来自两个不同版本 crate」的编译错误。真实用法如 [crates/gpui_web/src/window.rs:L14](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_web/src/window.rs#L14) 的 `use gpui_wgpu::{WgpuContext, WgpuRenderer, WgpuSurfaceConfig, wgpu};`。

**练习 3**：`[lib] path = "src/gpui_wgpu.rs"` 符合仓库的哪条编码规范？

**答案**：Zed 根目录 `CLAUDE.md` 规定不使用 `mod.rs`，并且新建 crate 时应通过 `[lib] path` 指向与 crate 同名的描述性文件（如 `gpui_wgpu.rs`），保持命名一致。

### 4.3 渲染主线：WgpuRenderer 与它的两位搭档

#### 4.3.1 概念说明

渲染主线回答的问题是：**「一帧的绘制数据如何变成屏幕上的像素」**。它由三个类型协作：

- `WgpuContext`：持有与 GPU 的「连接」——instance（wgpu 入口）、adapter（选中的显卡）、device（逻辑设备）、queue（命令提交队列）。创建一次，可被多个窗口共享。
- `WgpuRenderer`：渲染器本体。每个窗口一个，负责创建渲染管线、把 `gpui` 交给它的 `Scene`（场景）翻译成 GPU 命令并提交。
- `WgpuAtlas`：精灵图集。字形和图标先光栅化成小位图，由它分配纹理位置并上传，渲染时作为纹理采样。

与 `gpui` 的关系要特别说清楚：**`gpui` 不依赖 `gpui_wgpu`**（查 `crates/gpui/Cargo.toml` 找不到这个依赖）。依赖方向是反过来的——`gpui_wgpu` 依赖 `gpui`，消费它定义的 `Scene` / `PrimitiveBatch`（「画什么」的数据结构），并实现 `PlatformAtlas` 抽象（「怎么管理图集」的接口）。真正把渲染器接到窗口上的是平台 crate（`gpui_linux` / `gpui_web`），它们每帧调用 `renderer.draw(scene)`。

#### 4.3.2 核心流程

```text
平台窗口创建时（以 Wayland 为例）：
  raw window handle ──► WgpuRenderer::new(gpu_context, &window, config, compositor_gpu)
                          ├─ 复用或新建 WgpuContext（instance/adapter/device/queue）
                          ├─ 创建 surface（窗口的绘制表面）
                          └─ 创建管线、图集等资源

每一帧：
  gpui 布局 ──► Scene（PrimitiveBatch 列表：quads/underline/sprite/paths…）
              ──► WgpuRenderer::draw(&mut self, scene: &Scene) -> bool
                    ├─ 获取当前帧纹理
                    ├─ 编码渲染命令（按批次分派到对应管线）
                    └─ 提交并 present；返回 false 表示本帧未成功提交
```

#### 4.3.3 源码精读

**GPU 连接：`WgpuContext` 的字段一览。**

[src/wgpu_context.rs:L9-L18](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L9-L18)

instance / adapter / device / queue 是 wgpu 的标准四件套；`dual_source_blending` 记录设备是否支持亚像素文本渲染所需的特性；`color_texture_format` 决定图集用哪种纹理格式；`device_lost` 是一个原子标志，设备丢失回调触发时置位，供各渲染器轮询。

**面向合成器的显卡提示。**

[src/wgpu_context.rs:L59-L63](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L59-L63)

在双显卡（如 Intel 核显 + NVIDIA 独显）的 Linux 机器上，窗口实际被哪个 GPU 合成会影响该选哪个 adapter。`CompositorGpuHint` 只是一对 PCI ID，由平台层（`gpui_linux`）从显示服务器信息换算后传入，参与适配器排序（第 2 单元详解）。

**渲染器本体的字段骨架。**

[src/wgpu_renderer.rs:L206-L234](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs#L206-L234)

几个值得注意的设计：

- `context: Option<GpuContext>`：多窗口共享的 GPU 上下文引用，用于设备丢失后的恢复协调（wasm 上不用，所以标了 `#[allow(dead_code)]` 的注释说明）。
- `resources: Option<WgpuResources>`：所有「设备丢了就必须一起扔掉」的 GPU 资源被打包成一个结构体，恢复时整体置 `None` 再重建——见 [src/wgpu_renderer.rs:L179-L195](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs#L179-L195) 的文档注释「GPU resources that must be dropped together during device recovery」。
- `atlas: Arc<WgpuAtlas>`：图集以 `Arc` 持有，可与上下文一起跨窗口共享。
- `uses_webgl_instance_data`、`dual_source_blending`、`is_bgr` 等：记录平台能力差异，绘制路径据此分支。

**构造入口与帧入口。**

[src/wgpu_renderer.rs:L249-L266](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs#L249-L266)

`WgpuRenderer::new` 是原生平台入口（注意 `#[cfg(not(target_family = "wasm"))]` 和 `# Safety` 注释——调用方必须保证窗口句柄在渲染器存活期间有效）；它接受 `WgpuSurfaceConfig`（尺寸、是否透明、期望的 present mode，见 [src/wgpu_renderer.rs:L112-L122](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs#L112-L122)）。

[src/wgpu_renderer.rs:L1265](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs#L1265-L1265)

`draw(&mut self, scene: &Scene) -> bool` 是每帧的入口，输入是 `gpui` 的场景，返回值表示本帧是否成功提交。**它不实现 `gpui` 的任何 trait**——平台窗口代码直接调用它。

**真实消费证据（gpui_linux · Wayland）。**

[crates/gpui_linux/src/linux/wayland/window.rs:L574](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_linux/src/linux/wayland/window.rs#L574-L574) 创建渲染器：

```rust
WgpuRenderer::new(gpu_context, &raw_window, config, compositor_gpu)?
```

[crates/gpui_linux/src/linux/wayland/window.rs:L1732](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_linux/src/linux/wayland/window.rs#L1732-L1732) 每帧驱动它：

```rust
state.renderer_presented = state.renderer.draw(scene);
```

X11 路径同构，见 [crates/gpui_linux/src/linux/x11/window.rs:L12](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_linux/src/linux/x11/window.rs#L12-L12) 的导入。`gpui_linux` 依赖本 crate 的方式见 [crates/gpui_linux/Cargo.toml:L63](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_linux/Cargo.toml#L63-L63)——注意它开启了 `font-kit` feature。

**真实消费证据（gpui_web）。**

[crates/gpui_web/src/window.rs:L14](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_web/src/window.rs#L14-L14) 导入渲染三件套与 `wgpu`；[crates/gpui_web/src/gpui_web.rs:L19](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_web/src/gpui_web.rs#L19-L19) 把 `WebBackendPreference`（WebGPU / WebGL2 偏好）再导出给自己的用户。依赖声明见 [crates/gpui_web/Cargo.toml:L23](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_web/Cargo.toml#L23-L23)。

#### 4.3.4 代码实践

**实践目标**：不看本讲正文，独立从源码确认「`WgpuRenderer` 每窗口一个、`WgpuContext` 多窗口共享」这一结论。

**操作步骤**：

1. 打开 [crates/gpui_linux/src/linux/wayland/window.rs](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_linux/src/linux/wayland/window.rs#L111-L111)，找到 `renderer: WgpuRenderer` 字段（约 L111），确认它属于单个窗口的内部状态。
2. 再打开 [crates/gpui_linux/src/linux/wayland/client.rs:L105](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_linux/src/linux/wayland/client.rs#L105-L105) 与 [crates/gpui_linux/src/linux/x11/client.rs:L68](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_linux/src/linux/x11/client.rs#L68-L68)，观察 `GpuContext` / `CompositorGpuHint` 挂在 **client**（连接级，一个连接管所有窗口）而不是 window 上。
3. 对比两者生命周期：窗口关闭时谁被销毁、谁继续存活。

**需要观察的现象**：`WgpuRenderer` 是窗口结构体的字段；`GpuContext` 出现在 client 层并作为参数逐层传入 `WgpuRenderer::new`。

**预期结果**：能画出「client（1）—► GpuContext（1），window（N）—► WgpuRenderer（N）」的 ownership 图，并能用自己的话解释为什么这样设计（提示：设备只有一个，恢复要协调）。

（源码阅读型实践，无需运行。）

#### 4.3.5 小练习与答案

**练习 1**：`WgpuRenderer` 为什么把大量资源放进 `resources: Option<WgpuResources>`，而不是平铺成自己的字段？

**答案**：设备丢失（driver 崩溃、休眠恢复等）后这些资源全部失效，必须整体丢弃重建。打包成单个 struct，恢复逻辑只需 `self.resources = None` 再走一遍内部构造函数即可，既不容易漏掉某项资源，也明确了「生命周期一致」这一隐含约束——struct 的文档注释明确写着它们必须一起释放。

**练习 2**：`gpui` 和 `gpui_wgpu` 谁依赖谁？为什么不能反过来？

**答案**：`gpui_wgpu` 依赖 `gpui`。`gpui` 是平台无关核心，定义 `Scene`、`PlatformAtlas`、`PlatformTextSystem` 等抽象；如果 `gpui` 反过来依赖 `gpui_wgpu`（进而依赖 wgpu、cosmic-text 一大串），headless 测试、其他平台实现都会被拖累，而且 `gpui_wgpu` 还要引用 `gpui` 的类型，会形成循环依赖。实际代码里 `gpui` 对 `gpui_wgpu` 的唯一「引用」是一处文档示例（见 4.4.3）。

**练习 3**：`draw()` 的返回值 `bool` 大概表达什么？调用方拿它做什么？

**答案**：表示本帧是否成功获取帧纹理并提交呈现。返回 false 通常意味着表面失效（窗口尺寸变化、最小化）或设备异常，平台层据此决定是否需要请求重绘；Wayland 窗口代码把它存进 `state.renderer_presented`（[crates/gpui_linux/src/linux/wayland/window.rs:L1732](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_linux/src/linux/wayland/window.rs#L1732-L1732)）。具体分支在第 3 单元逐行分析。

### 4.4 文本主线：CosmicTextSystem

#### 4.4.1 概念说明

文本主线回答的问题是：**「一个字符串如何变成带位置的字形，再变成图集里的像素」**。`CosmicTextSystem` 实现了 `gpui` 的 `PlatformTextSystem` 抽象，内部把工作委托给两个库：

- `cosmic-text`：维护字体数据库（fontdb）、解析字体家族、执行整形（shaping，把字符映射为字形并排版）。
- `swash`：字形光栅化（矢量轮廓 → 像素位图），以及按需缩放。

为什么渲染和文本是两条独立主线？因为它们生命周期不同：文本系统与窗口无关（headless 也能排版），渲染器与窗口一一绑定；Linux 和 Web 平台恰好都能复用同一份基于 cosmic-text 的实现，所以它和 wgpu 渲染器放在同一个 crate 里，但代码上几乎不相交。

#### 4.4.2 核心流程

```text
字体侧：
  add_fonts(字体字节流) ──► fontdb 数据库 + loaded_fonts 列表
  font_id(Font{family, features, fallbacks})
      ├─ 查 font_ids_by_family_cache（命中 → 直接拿候选 FontId 列表）
      └─ 未命中 → load_family 遍历数据库 → 写入缓存
      └─ find_best_match 从候选中按属性挑出最合适的一个

排版与光栅化侧：
  layout_line(文本, 字体, ...) ──► ShapedGlyph 列表（字形 id + 位置）
  rasterize_glyph(字形) ──► swash 渲染 ──► 位图 ──► 存入 WgpuAtlas ──► 渲染时按 sprite 画出
```

#### 4.4.3 源码精读

**新类型包装 + 读写锁。**

[src/cosmic_text_system.rs:L24](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/cosmic_text_system.rs#L24-L24)

`pub struct CosmicTextSystem(RwLock<CosmicTextSystemState>);` —— `gpui` 以 `Arc` 共享文本系统，接口方法都收 `&self`，所以内部用 `RwLock` 提供可变性；排版查询远多于字体加载，读多写少正适合 `RwLock`。

**内部状态。**

[src/cosmic_text_system.rs:L43-L53](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/cosmic_text_system.rs#L43-L53)

`font_system` 是 cosmic-text 的核心；`swash_scale_context` 是光栅化上下文；`loaded_fonts` 按 `FontId` 索引所有已加载字体；`font_ids_by_family_cache` 的注释直接说明了它的动机——避免每次查询都遍历字体数据库。

**两个构造函数。**

[src/cosmic_text_system.rs:L64-L93](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/cosmic_text_system.rs#L64-L93)

`new` 用 `FontSystem::new()` 扫描系统字体（桌面场景）；`new_without_system_fonts` 则以空的 fontdb 启动（浏览器里扫不到系统字体，或测试 / 基准想精确控制字体集，之后用 `add_fonts` 注入）。

**实现 gpui 的抽象接口。**

[src/cosmic_text_system.rs:L95-L133](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/cosmic_text_system.rs#L95-L133)

`impl PlatformTextSystem for CosmicTextSystem` 从 `add_fonts`、`all_font_names`、`font_id` 开始；`font_id` 的实现清晰展示了缓存命中 / 未命中两条路径，随后由 `find_best_match` 挑出最终字面。trait 的其余方法（排版、光栅化）同样委托给内部状态，在第 5 单元逐个精读。

**消费证据（三处，三种姿态）。**

- **gpui_linux**：直接再导出复用——[crates/gpui_linux/src/linux/text_system.rs:L1](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_linux/src/linux/text_system.rs#L1-L1) `pub(crate) use gpui_wgpu::CosmicTextSystem;`
- **gpui_web**：用无系统字体构造——[crates/gpui_web/src/platform.rs:L135](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_web/src/platform.rs#L135-L135) `Arc::new(gpui_wgpu::CosmicTextSystem::new_without_system_fonts(...))`
- **gpui（headless 文档示例）**：[crates/gpui/src/app/headless_app_context.rs:L31](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/app/headless_app_context.rs#L31-L31) 的文档注释演示了无窗口场景下用 `gpui_wgpu::CosmicTextSystem::new("fallback")` 做纯 CPU 排版。此外本 crate 自己的基准 [benches/layout_line.rs:L3](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/benches/layout_line.rs#L3-L3) 也 import 了它。

#### 4.4.4 代码实践

**实践目标**：体会「同一个文本系统实现，三种完全不同的运行环境」。

**操作步骤**：

1. 读上面三处消费代码各自的前后文（各看约 20 行），回答：Linux 桌面、浏览器、无窗口测试分别为什么选择 `new` 或 `new_without_system_fonts`？
2. 打开 [benches/layout_line.rs](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/benches/layout_line.rs#L1-L40)，观察基准如何用 `add_fonts` 注入仓库内置字体，从而在完全可控的字体集上测量 `layout_line`。
3. 思考：如果让你给 `CosmicTextSystem` 加一个「列出所有可用字体家族」的小工具函数，应该加在 trait 里还是 inherent impl 里？为什么？

**需要观察的现象**：三种环境的字体来源差异（系统扫描 / 显式注入 / 基准固定字体）。

**预期结果**：能写出一句话结论，例如「字体来源不可控或不存在系统字体库的环境用 `new_without_system_fonts` + `add_fonts`，桌面 Linux 用 `new` 自动扫描」。

（源码阅读型实践，无需运行。）

#### 4.4.5 小练习与答案

**练习 1**：`CosmicTextSystem` 为什么设计成 `RwLock<State>` 的新类型，而不是把可变方法都收 `&mut self`？

**答案**：`gpui` 通过 `Arc<CosmicTextSystem>` 在整个应用里共享同一个文本系统实例（字体数据库很大，不可能每个窗口一份），而 `PlatformTextSystem` 的接口方法是 `&self`。内部 `RwLock` 让多个排版查询并发读、字体加载独占写，兼顾了共享与可变。

**练习 2**：`font_ids_by_family_cache` 的 key 为什么不只用家族名字符串，而是 `FontKey { family, features, fallbacks }`？

**答案**：同一个家族在「不同的 OpenType 特性集 / 不同的回退链」下，解析出的候选字体列表可能不同（特性过滤、fallback 字体也进入候选）。只用家族名做 key 会把不同配置的结果错误地混在一个缓存条目里。定义见 [src/cosmic_text_system.rs:L27-L31](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/cosmic_text_system.rs#L27-L31)。

**练习 3**：渲染主线和文本主线在代码上几乎不相交，为什么放在同一个 crate？

**答案**：它们是「同一批非 macOS 平台（Linux、Web）需要的同一套 gpui 平台实现」的两个半边：平台 crate（`gpui_linux` / `gpui_web`）反正都要同时引入两者，放在一个 crate 里让依赖关系简单（一个依赖搞定渲染 + 文本），又因为模块完全独立（`wgpu_renderer.rs` vs `cosmic_text_system.rs`）而互不拖累编译与维护。

## 5. 综合实践

**任务**：用一次全局搜索 + 一张手绘关系图，把本讲的所有结论「自己重新发现一遍」。这是本讲规格中要求的代码实践任务，也是后续所有单元的地图。

**操作步骤**：

1. 在仓库根目录运行（任选其一）：

   ```bash
   rg -n "gpui_wgpu::" crates/ -g '*.rs'
   # 或
   grep -rn "gpui_wgpu::" crates/ --include='*.rs'
   ```

2. 把命中结果按 crate 分组，剔除 `crates/gpui_wgpu/` 自身（那是 crate 内部用 `crate::` 之外的自我引用和注释）与本教程目录。
3. 对每个消费点，记下它 import 了什么（`WgpuRenderer`？`GpuContext`？`CosmicTextSystem`？`CompositorGpuHint`？）以及它在做什么（创建渲染器 / 每帧绘制 / 构造文本系统 / 换算 GPU 提示）。
4. 手画（纸笔或任何画图工具）一张关系图，必须包含以下要素：

```text
┌─────────────────────────────────────────────────────────────┐
│ zed 业务 crate（editor、workspace…）                          │
│                    只依赖 gpui，不知道 gpui_wgpu 的存在       │
└───────────────────────────┬─────────────────────────────────┘
                            ▼
┌─────────────────────────────────────────────────────────────┐
│ gpui（平台无关 UI 框架）                                     │
│  · Scene / PrimitiveBatch：描述「画什么」                    │
│  · PlatformAtlas / PlatformTextSystem：平台需实现的抽象      │
│  · 对 gpui_wgpu 仅有一处 headless 文档示例引用，无依赖      │
└───────▲─────────────────────────────────▲───────────────────┘
        │ 实现 PlatformAtlas/TextSystem、  │ 依赖（窗口、事件、
        │ 消费 Scene                       │ GPUI 应用模型）
┌───────┴─────────────────────────────────┴───────────────────┐
│                gpui_linux ／ gpui_web（平台层）              │
│   Wayland/X11 窗口、浏览器 canvas；创建并每帧驱动渲染器      │
└───────▲─────────────────────────────────▲───────────────────┘
        │ WgpuRenderer::new / draw        │ 同左（web 经 canvas）
┌───────┴─────────────────────────────────┴───────────────────┐
│ gpui_wgpu                                                    │
│  渲染主线：WgpuContext + WgpuRenderer + WgpuAtlas + WGSL    │
│  文本主线：CosmicTextSystem（cosmic-text + swash）          │
└─────────────────────────────────────────────────────────────┘
```

5. 在图上额外标注：`CompositorGpuHint` 的产生地（[crates/gpui_linux/src/linux/platform.rs:L1222](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_linux/src/linux/platform.rs#L1222-L1222) 的 `compositor_gpu_hint_from_dev_t`）到消费地（`WgpuRenderer::new` 的参数）的流向。

**需要观察的现象**：搜索命中应集中且数量有限——`gpui_linux`（wayland / x11 的 `window.rs`、`client.rs`、`platform.rs`、`text_system.rs`）与 `gpui_web`（`platform.rs`、`window.rs`、`gpui_web.rs`）两个 crate，外加 `gpui` 的一处文档示例与 `crates/gpui_wgpu/benches/layout_line.rs`。除此之外不应有别的业务 crate 直接 import 本 crate。

**预期结果**：一张与上面 ASCII 图等价的自己的图，并且能向别人解释三个问题：(a) 为什么 zed 业务代码从不直接 import `gpui_wgpu`；(b) 为什么 `gpui` 不依赖 `gpui_wgpu`；(c) Linux 与 Web 两条平台路径各自从哪里进入本 crate。

**待本地验证**：搜索结果的具体行号会随代码演进变化，以你本地的输出为准；若出现本讲未列出的新消费点，说明仓库在你阅读时已经更新，请以实际代码为准。

## 6. 本讲小结

- `gpui_wgpu` 是 Zed 中基于 **wgpu** 的 GPUI 平台实现：渲染器（`WgpuRenderer` / `WgpuContext` / `WgpuAtlas` + 约 1700 行 WGSL）+ 文本系统（`CosmicTextSystem`），服务于 Linux（Wayland / X11）与 Web 等平台。
- 依赖方向是 `gpui_wgpu → gpui`（实现 `PlatformAtlas` / `PlatformTextSystem`、消费 `Scene`），`gpui_linux` / `gpui_web` 同时依赖两者并把渲染器接到真实窗口上；`gpui` 本身不依赖本 crate。
- 渲染主线三件套分工：`WgpuContext` 是可跨窗口共享的 GPU 连接（instance / adapter / device / queue），`WgpuRenderer` 每窗口一个、暴露 `draw(&mut self, scene: &Scene) -> bool`，`WgpuAtlas` 管理纹理空间与上传。
- 文本主线 `CosmicTextSystem` 用 `RwLock` 包住 cosmic-text 的 `FontSystem` 与 swash 光栅化上下文，通过家族缓存与最佳匹配把 `Font` 解析成 `FontId`。
- `Cargo.toml` 就是架构图：wgpu（图形）、cosmic-text（排版）、swash（光栅化）、etagere（图集装箱）、bytemuck（字节转换）、raw-window-handle（窗口句柄）；`font-kit` 是 Linux 专属可选 feature；wasm 目标额外引入 `web-sys` 并给 wgpu 叠加 `webgl` feature。
- 库根 `gpui_wgpu.rs` 只有 10 行：私有模块 + 汇聚式再导出（含 `pub use wgpu;` 保证下游版本对齐）。

## 7. 下一步学习建议

下一讲（`u1-l2-module-structure-and-build.md`）会深入四个源码模块的内部分工与构建方式：跑 `cargo check` / `cargo doc`、比较 `font-kit` feature 的开关差异，并列出本 crate 最核心的公共 API。

在此之前的自查建议：

- 重新扫一眼第 3 节的源码地图，确认你能说出每个文件「属于渲染主线还是文本主线」。
- 挑战预习：打开 [src/wgpu_renderer.rs:L21-L43](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs#L21-L43)，看三种着色器变体（storage / WebGL / subpixel）是如何用 `include_str!` + `concat!` 拼接出来的——这是第 3、4 单元反复要用到的机制。
- 若你对 wgpu 本身不熟，建议先读 wgpu 官方文档中 Instance / Adapter / Device / Queue / Surface 五个概念的定义，再进入第 2 单元（`WgpuContext` 的创建流程精读）。

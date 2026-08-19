# 模块结构与构建方式：从 lib 根读懂代码组织

## 1. 本讲目标

上一讲（u1-l1）我们已经知道 gpui_wgpu 是「基于 wgpu 的 GPUI 渲染器 + 精灵图集 + cosmic-text 文本系统」，它被 gpui_linux 和 gpui_web 消费。本讲我们钻进 crate 内部，回答三个问题：

1. 这个 crate 的代码是如何组织的？——精读只有 10 行的库根 `src/gpui_wgpu.rs`，理解「私有模块 + 汇聚式再导出」的组织策略。
2. 四个 Rust 模块（`wgpu_context` / `wgpu_renderer` / `wgpu_atlas` / `cosmic_text_system`）各自负责什么、各自对外暴露哪些类型？四个 WGSL 着色器文件又扮演什么角色？
3. `Cargo.toml` 是如何构建出这套行为的？——重点理解 `[lib] path` 配置、`font-kit` 可选特性、原生与 wasm 两套条件依赖。

学完本讲，你应该能：

- 脱口说出每个模块的职责边界和公开 API 面；
- 独立完成 `cargo check -p gpui_wgpu`（含与不含 `--features font-kit`）并对差异做出解释；
- 用 `cargo doc` 浏览本 crate 的全部导出项，并判断哪些是核心 API。

## 2. 前置知识

本讲会用到的 Rust 与 Cargo 概念，先用大白话过一遍：

- **库 crate 与库根（lib root）**：Rust 的库 crate 必须有一个「根文件」，默认叫 `src/lib.rs`，但可以在 `Cargo.toml` 里用 `[lib] path = "..."` 改名。根文件是整个模块树的入口。
- **`mod` 声明与私有性**：`mod foo;` 表示「当前目录下有一个子模块 `foo`（文件 `foo.rs` 或 `foo/mod.rs`）」。**不加 `pub` 的模块是私有的**——crate 外部无法通过 `gpui_wgpu::foo::Xxx` 这样的路径访问它，即使 `Xxx` 本身是 `pub` 的。
- **`pub use` 再导出（re-export）**：把某个模块里的公开项「搬」到当前层级对外提供。`pub use foo::*;`（ glob 再导出）把 `foo` 的所有公开项搬到顶层；`pub use foo::{A, B};`（选择性再导出）只搬指定项。这种「内部按模块分文件、对外只暴露一个门面」的写法常被称为**门面模式（facade pattern）**。
- **再导出外部 crate（`pub use wgpu;`）**：除了再导出自己的模块，还能再导出依赖的外部 crate。这样下游可以直接写 `gpui_wgpu::wgpu::Instance`，而不必自己再声明一个 wgpu 依赖——保证大家用的是**同一个 wgpu 版本**，类型完全对齐（这是 u1-l1 已建立的「汇聚式再导出」结论，本讲你会看到它的代码落点）。
- **Cargo feature 与可选依赖**：`[features]` 定义编译期开关；在依赖后面标 `optional = true` 会让该依赖只在某个 feature 开启时才被编译。feature 名和依赖名可以相同——本 crate 的 `font-kit = ["dep:font-kit"]` 就是这个套路，`dep:` 前缀是显式引用可选依赖的现代写法。
- **按目标平台的条件依赖**：`[target.'cfg(...)'.dependencies]` 可以让某些依赖只在特定编译目标下生效。本 crate 用 `cfg(target_family = "wasm")` 区分「原生平台（Linux/macOS/Windows）」与「浏览器 wasm」两套依赖。
- **`include_str!` 与 `concat!`**：`include_str!("x.wgsl")` 在**编译期**把整个文件内容嵌入为字符串字面量；`concat!` 把多个字符串拼成一个。gpui_wgpu 用这两个宏把 WGSL 着色器文件拼成不同变体（见 4.4 节）。
- **WGSL**：WebGPU Shading Language，运行在 GPU 上的着色器语言。它不由 rustc 编译，而是由 wgpu 内置的 naga 翻译器在创建渲染管线时编译。

## 3. 本讲源码地图

gpui_wgpu 的全部文件如下（本 crate 极其精简，没有一层多余的目录）：

| 文件 | 规模 | 作用 | 本讲角色 |
| --- | --- | --- | --- |
| `src/gpui_wgpu.rs` | 10 行 | 库根：声明 4 个私有模块，再导出公开 API | ⭐ 精读 |
| `Cargo.toml` | 57 行 | 包定义、feature、条件依赖、bench 声明 | ⭐ 精读 |
| `src/wgpu_context.rs` | 数百行 | GPU 连接层：`WgpuContext`（instance/adapter/device/queue）、适配器选择、设备丢失标志 | 浏览公开面 |
| `src/wgpu_renderer.rs` | 2000+ 行（本 crate 最大） | 渲染层：`WgpuRenderer` 每窗口渲染器、管线创建、`draw()` 帧循环 | 浏览公开面 |
| `src/wgpu_atlas.rs` | 数百行 | 精灵图集：`WgpuAtlas` 实现 gpui 的 `PlatformAtlas` trait | 浏览公开面 |
| `src/cosmic_text_system.rs` | 1000+ 行 | 文本系统：`CosmicTextSystem` 实现 `PlatformTextSystem`，含 `font-kit` 特性门控代码 | 浏览公开面 |
| `src/shaders.wgsl` | — | 着色器公共部分（结构体、gamma/对比度函数、各图元着色器） | 只看组织方式 |
| `src/shaders_storage.wgsl` | — | 实例数据走 storage buffer 的着色器变体 | 只看组织方式 |
| `src/shaders_webgl.wgsl` | — | 实例数据走纹理（WebGL2 无 storage buffer）的变体 | 只看组织方式 |
| `src/shaders_subpixel.wgsl` | — | 亚像素文本渲染变体（需双源混合特性） | 只看组织方式 |
| `benches/layout_line.rs` | — | criterion 基准（u1-l3 详讲） | 不涉及 |

本讲引用的关键源码：`src/gpui_wgpu.rs`、`Cargo.toml`，以及四个模块文件的公开声明部分。

## 4. 核心概念与源码讲解

### 4.1 库根 gpui_wgpu.rs：10 行读懂门面模式

#### 4.1.1 概念说明

一个 crate 给外部世界的「第一印象」就是它的库根。很多项目的库根动辄几百行（`mod` + `pub use` + 文档），而 gpui_wgpu 的库根只有 10 行，却完整表达了三件事：

1. **内部如何划分**——4 个私有模块，每个模块一个文件；
2. **对外暴露什么**——通过 `pub use` 把公开类型汇聚到顶层，下游永远写 `gpui_wgpu::WgpuRenderer` 而不是 `gpui_wgpu::wgpu_renderer::WgpuRenderer`；
3. **顺带再导出 wgpu 本身**——下游拿到的 `wgpu::Instance`、`wgpu::Surface` 等类型保证与本 crate 用的是同一份。

私有模块的价值在于**信息隐藏**：模块内部的辅助类型（渲染管线结构体、POD 顶点数据、缓存 map 等）即使标了 `pub`（或者根本没标），外部也碰不到，将来重命名、拆分都不构成破坏性变更。

#### 4.1.2 核心流程

库根的加载流程（编译期视角）：

```text
cargo 编译 gpui_wgpu
  └─ 读取 [lib] path 指定的 src/gpui_wgpu.rs 作为模块树根
       ├─ mod cosmic_text_system;   → 私有模块，仅 crate 内可见
       ├─ mod wgpu_atlas;           → 私有模块
       ├─ mod wgpu_context;         → 私有模块
       ├─ mod wgpu_renderer;        → 私有模块
       └─ 对外 API 面 =
            cosmic_text_system::* 的公开项
          + wgpu_atlas::* 的公开项
          + wgpu_context::* 的公开项
          + wgpu_renderer 的三个指定公开项（GpuContext / WgpuRenderer / WgpuSurfaceConfig）
          + 外部 crate wgpu 本身
```

下游消费路径（以 gpui_linux 为例）：`use gpui_wgpu::WgpuRenderer` → 每个窗口构造一个 → 平台事件循环每帧调用 `renderer.draw(&scene)`。

#### 4.1.3 源码精读

库根全文只有 10 行：

[src/gpui_wgpu.rs:1-10](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/gpui_wgpu.rs#L1-L10)——本 crate 的库根：前 4 行声明四个**私有**模块（文本系统、图集、GPU 上下文、渲染器），后 5 行把公开项汇聚到 crate 顶层。

```rust
mod cosmic_text_system;
mod wgpu_atlas;
mod wgpu_context;
mod wgpu_renderer;

pub use cosmic_text_system::*;
pub use wgpu;
pub use wgpu_atlas::*;
pub use wgpu_context::*;
pub use wgpu_renderer::{GpuContext, WgpuRenderer, WgpuSurfaceConfig};
```

逐行解读：

- 第 1–4 行：四个 `mod` 都**没有 `pub`**，模块本身对外不可见。这是刻意为之的门面策略。
- 第 6、8、9 行：三个 glob 再导出。这三个模块的公开项就是各自模块的全部「对外承诺」（见 4.2 节的清单）。
- 第 7 行：`pub use wgpu;` 把整个 wgpu crate 再导出。下游（如 gpui_linux）想创建 `wgpu::Instance` 传给 `WgpuContext::new` 时，用 `gpui_wgpu::wgpu::Instance` 即可，不必（也不应）自己再依赖一份 wgpu——避免「同一个逻辑类型因版本不同而类型不兼容」。workspace 里 wgpu 统一锁在 `wgpu = "29.0.4"`（[仓库根 Cargo.toml:898](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/Cargo.toml#L898) 为 workspace 级声明，本 crate 通过 `wgpu.workspace = true` 继承，见 [Cargo.toml:35](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/Cargo.toml#L35)）。
- 第 10 行：**选择性**再导出。用一个有意思的事实来理解它：`wgpu_renderer` 是本 crate 最大的文件（2000+ 行），但它的**公开项恰好只有这三个**——`WgpuSurfaceConfig`、`GpuContext`、`WgpuRenderer`（文件里的 `WgpuPipelines`、`InstanceBinding`、`WgpuResources` 等统统是私有结构体，见 [src/wgpu_renderer.rs:124-195](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs#L124-L195)）。所以今天写 `pub use wgpu_renderer::*;` 效果完全等价；显式列举的写法把「这个模块只承诺这三样东西」白纸黑字写在库根上，未来即使有人给模块加了新的 `pub` 项，也不会悄无声息地扩大公共 API 面。这是一种防御性的 API 治理风格。

另外注意 `[lib] path = "src/gpui_wgpu.rs"` 这条配置（[Cargo.toml:11-12](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/Cargo.toml#L11-L12)）——库根不叫默认的 `lib.rs`，而是与 crate 同名。这符合 Zed 仓库的统一约定（见仓库根 CLAUDE.md：「创建新 crate 时优先用 `[lib] path` 指定库根，保持命名一致」），gpui_linux 的 `[lib] path = "src/gpui_linux.rs"` 也是同样做法。

#### 4.1.4 代码实践

**实践目标**：用「故意写错路径」的方式，亲眼验证私有模块与再导出的边界。

**操作步骤**：

1. 在 zed 仓库根目录执行下面的搜索，观察下游如何引用本 crate：
   ```bash
   grep -rn "gpui_wgpu::" crates/gpui_linux/src crates/gpui_web/src | head -30
   ```
2. 再搜索 `use gpui_wgpu`，确认下游 `use` 的都是**顶层路径**（如 `gpui_wgpu::WgpuRenderer`、`gpui_wgpu::wgpu::Instance`），没有任何 `gpui_wgpu::wgpu_renderer::...` 形式。
3. （可选，需可写环境）在任意一个下游 crate（或临时新建的小 crate）里写一行注定失败的代码：
   ```rust
   // 示例代码：故意访问私有模块，预期编译失败
   let _ = <gpui_wgpu::wgpu_renderer::WgpuRenderer>::descriptor();
   ```

**需要观察的现象**：

- 步骤 1–2 中，下游的引用路径全部是 `gpui_wgpu::` 后直接跟类型名或 `wgpu::`；
- 步骤 3 编译器报错的关键词是 **private module**（模块 `wgpu_renderer` 是私有的），而不是「找不到类型」——因为模块本身不可见。

**预期结果**：确认「外部世界只能看到门面，看不到内部模块」；同时看到 `pub use wgpu` 的实际受益者——下游在构造 `wgpu::Instance`、`wgpu::Surface` 时确实通过 `gpui_wgpu::wgpu` 拿类型（具体调用点在 u6-l3 平台集成一讲会展开）。

（本实践为源码阅读型，无需 GPU。）

#### 4.1.5 小练习与答案

**练习 1**：如果把库根第 10 行改成 `pub use wgpu_renderer::*;`，下游代码会受影响吗？会暴露更多类型吗？

**答案**：下游代码不受影响（顶层路径不变）；也不会暴露更多类型——因为 `wgpu_renderer` 模块中目前只有 `WgpuSurfaceConfig`、`GpuContext`、`WgpuRenderer` 三个公开项，glob 与显式列举等价。区别只在于「未来防御性」：glob 写法下，模块新增的任何 `pub` 项都会自动进入公共 API。

**练习 2**：为什么 `pub use wgpu;` 对这个 crate 特别重要？去掉它会发生什么？

**答案**：`WgpuContext::new` 的签名要求调用方传入自己创建的 `wgpu::Instance` 和 `wgpu::Surface`。如果去掉再导出，下游 crate 必须自己声明 wgpu 依赖；一旦版本解析出另一个 wgpu 版本（例如 29.0.3 与 29.0.4），两边类型就是不同类型，直接编译失败。再导出让「wgpu 的版本选择权」集中在（workspace 与）本 crate 手里。

**练习 3**：库根第 1–4 行的 `mod` 语句如果加上 `pub`（`pub mod wgpu_renderer;`），是好的改动吗？

**答案**：不推荐。这会把内部实现细节（管线结构、POD 类型等）变成可被下游引用的公共路径，内部重构立刻升级为破坏性变更；同时削弱门面「一处看清全部 API」的可读性。私有模块 + 再导出正是为了保留重构自由度。

### 4.2 四个模块的分工与公开 API 面

#### 4.2.1 概念说明

四个模块沿两条主线分布（承接 u1-l1 的结论，这里落到代码结构上）：

- **渲染主线**（3 个模块，数据自下而上）：
  - `wgpu_context`：**GPU 连接层**。回答「我们连的是哪块 GPU、用什么后端、设备还活着吗」。
  - `wgpu_atlas`：**资源层**。回答「字形/图片怎么装进纹理图集、怎么复用」。
  - `wgpu_renderer`：**执行层**。回答「这一帧的 Scene 怎么变成屏幕上的像素」。
- **文本主线**（1 个模块）：
  - `cosmic_text_system`：**文本层**。回答「字体怎么加载、一行字怎么整形、一个字形怎么光栅化」。

每个模块的公开面都极小（1–5 个类型），这是这个 crate 最鲜明的结构特征：**实现可以很厚，接口必须很薄**。

#### 4.2.2 核心流程

四个模块在一次「打开窗口 → 显示文字」流程中的协作关系：

```text
gpui_linux / gpui_web（平台 crate）
  │
  ├─ WgpuContext::new(instance, surface, hint)        ← wgpu_context 模块
  │    得到 device/queue + 能力标志（双源混合？色彩格式？）
  │
  ├─ WgpuAtlas::from_context(&context)                ← wgpu_atlas 模块
  │    图集持有 device/queue，准备装字形
  │
  ├─ CosmicTextSystem::new(fallback)                  ← cosmic_text_system 模块
  │    字体数据库 + 整形 + 光栅化
  │
  └─ WgpuRenderer::new(window, ...)                   ← wgpu_renderer 模块
       内部创建管线/绑定组，持有 atlas
       平台每帧调用 renderer.draw(&scene)
         └─ 文本主线的产物（字形位图）已经在 atlas 里，
            渲染主线只需按批次实例化绘制
```

#### 4.2.3 源码精读

**wgpu_context 模块**——GPU 连接层，公开 5 个类型：

- [src/wgpu_context.rs:9-18](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L9-L18)：`pub struct WgpuContext`，持有 wgpu 四件套（`instance`/`adapter`/`device`/`queue`，后两者是 `Arc` 便于共享）以及三个派生能力：`backend`、`dual_source_blending`、`color_texture_format`，还有一个 `device_lost: Arc<AtomicBool>` 设备丢失标志。
- [src/wgpu_context.rs:20-25](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L20-L25)：`pub enum WgpuBackend`，把「浏览器 WebGPU / WebGL / 原生某后端」统一成一个枚举。
- [src/wgpu_context.rs:27-40](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L27-L40)：`WebBackendPreference` 与 `PreparedWebGraphics`，注意它们都带 `#[cfg(target_family = "wasm")]`——**只在编译到 wasm 时存在**，原生平台的文档里根本看不到它们（u2-l3 详讲）。
- [src/wgpu_context.rs:59-63](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L59-L63)：`pub struct CompositorGpuHint`，携带 `vendor_id`/`device_id`，用于混合 GPU 系统上提示「合成器在用哪块卡」（u2-l2 详讲）。

**wgpu_renderer 模块**——执行层，公开 3 项：

- [src/wgpu_renderer.rs:112-122](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs#L112-L122)：`pub struct WgpuSurfaceConfig`，创建渲染器时的表面配置（尺寸、是否透明、首选 present mode）。
- [src/wgpu_renderer.rs:164-165](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs#L164-L165)：`pub type GpuContext = Rc<RefCell<Option<WgpuContext>>>;`——一个类型别名：**多个窗口共享同一个（可能正在重建的）GPU 上下文**的槽位，是设备恢复协调的关键（u6-l1 详讲）。
- [src/wgpu_renderer.rs:206-234](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs#L206-L234)：`pub struct WgpuRenderer`，每窗口一个的渲染器。字段里能看到本 crate 的全部「戏份」：`resources: Option<WgpuResources>`（可在设备丢失时整体丢弃）、`atlas: Arc<WgpuAtlas>`、实例数据容量与对齐、`uses_webgl_instance_data`、`failed_frame_count` 等。它的核心方法是 `pub fn draw(&mut self, scene: &Scene) -> bool`（[src/wgpu_renderer.rs:1265](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs#L1265)）。

而同文件里的 [src/wgpu_renderer.rs:124-135](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs#L124-L135)（`WgpuPipelines`，九条渲染管线的私有结构体）与 [src/wgpu_renderer.rs:179-195](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs#L179-L195)（`WgpuResources`，设备恢复时必须一起丢弃的资源集合）都是**私有**的——它们是实现细节，被 4.1 节的门面策略挡在 crate 内部。

**wgpu_atlas 模块**——资源层，公开 2 个类型：

- [src/wgpu_atlas.rs:24](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_atlas.rs#L24)：`pub struct WgpuAtlas(Mutex<WgpuAtlasState>);`——元组结构体，内部状态整体用 `Mutex` 包起来（`WgpuAtlasState` 含 device/queue/etagere 分配器/tile 缓存/pending 上传队列，全部私有）。
- [src/wgpu_atlas.rs:107](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_atlas.rs#L107)：`impl PlatformAtlas for WgpuAtlas`——实现 gpui 定义的平台抽象 trait。**这是本 crate 对 gpui 的两个扩展点之一**：gpui 只规定「图集该会做什么」，本模块决定「用 wgpu + etagere 怎么做」。
- [src/wgpu_atlas.rs:42-44](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_atlas.rs#L42-L44)：`pub struct WgpuTextureInfo`，把图集纹理的 `TextureView` 交给外部（渲染器绑定时用）。

**cosmic_text_system 模块**——文本层，公开 1 个类型：

- [src/cosmic_text_system.rs:24](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/cosmic_text_system.rs#L24)：`pub struct CosmicTextSystem(RwLock<CosmicTextSystemState>);`——同样是一层薄锁包住全部内部状态（字体数据库、已加载字体表、家族缓存、swash 缩放上下文等，见 [src/cosmic_text_system.rs:43-53](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/cosmic_text_system.rs#L43-L53)）。
- [src/cosmic_text_system.rs:95](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/cosmic_text_system.rs#L95)：`impl PlatformTextSystem for CosmicTextSystem`——**第二个平台扩展点**：字体加载、排版 `layout_line`、字形光栅化 `rasterize_glyph` 都从这里进来。

#### 4.2.4 代码实践

**实践目标**：不依赖记忆，用 grep 亲手核实每个模块的公开面，制作一张「模块 → 公开类型 → 实现的平台 trait」对照表。

**操作步骤**：

1. 在 `crates/gpui_wgpu` 目录下执行：
   ```bash
   grep -n "^pub " src/*.rs
   ```
2. 再执行下面两条，找出两个平台 trait 的实现位置：
   ```bash
   grep -n "^impl Platform" src/*.rs
   grep -n "^    pub fn " src/wgpu_renderer.rs | head -20
   ```

**需要观察的现象**：

- `^pub` 的命中数极少——每个模块顶层只有 1–5 个公开类型（native 目标下 `wgpu_context` 是 4 个：`WgpuContext`、`WgpuBackend`、`CompositorGpuHint`，wasm 下再加 2 个）；
- `impl Platform` 恰好两条：`PlatformTextSystem for CosmicTextSystem`、`PlatformAtlas for WgpuAtlas`；
- `WgpuRenderer` 的公开方法清单：`new` / `new_from_canvas` / `new_from_surface` / `update_drawable_size` / `update_transparency` / `draw` / `recover` 等，全部集中在 `impl WgpuRenderer` 块内。

**预期结果**：得到一张与 4.2.3 内容一致的表，并直观感受到「实现很厚、接口很薄」——wgpu_renderer.rs 里 `^pub` 只有 3 处，而私有结构体有十几个。

（纯 grep 实践，无需编译。）

#### 4.2.5 小练习与答案

**练习 1**：`GpuContext` 为什么定义成 `Rc<RefCell<Option<WgpuContext>>>` 而不是直接 `WgpuContext`？

**答案**：`Option` 留出「上下文当前不存在」的空档——设备丢失后、重建完成前，这个槽位是空的，各窗口能据此判断该等待还是该重建；`Rc<RefCell<...>>` 允许多个 `WgpuRenderer`（多个窗口）共享同一槽位、且能在运行时整体替换里面的值。这是多窗口设备恢复协调的基础（u6-l1 展开）。

**练习 2**：`WgpuAtlas` 用 `Mutex` 包状态、`CosmicTextSystem` 用 `RwLock` 包状态，为什么锁的类型不一样？

**答案**：这与各自的访问模式有关。图集的典型操作（分配 tile、入队上传、刷新）都需要独占修改，`Mutex` 足够；文本系统的大量调用是读侧（查字体、查缓存）且状态庞大，`RwLock` 允许多个读者并行、只在加载字体/写缓存时独占。锁的选择反映访问模式，而不是随手一包。（更细的并发语义在 u5 单元结合调用方分析。）

**练习 3**：gpui 本体为什么「看不见」这两个平台 trait 的实现？（提示：回忆 u1-l1 的依赖方向）

**答案**：依赖方向是 gpui_wgpu → gpui（本 crate 依赖 gpui，定义并实现 trait），gpui 不反向依赖 gpui_wgpu。gpui 只在运行时以 trait 对象的方式持有「某个实现了 PlatformAtlas / PlatformTextSystem 的东西」，由平台 crate（gpui_linux / gpui_web）负责装配。这保证了 gpui 核心不被任何具体 GPU/文本后端污染。

### 4.3 Cargo.toml：特性开关、可选依赖与按目标条件依赖

#### 4.3.1 概念说明

如果说库根回答「代码怎么组织」，`Cargo.toml` 就回答「这份代码在什么条件下、以什么形态被编译」。gpui_wgpu 的 `Cargo.toml` 有四个值得精读的设计点：

1. **`[lib] path` 指定非默认库根**（4.1 节已讲）；
2. **`font-kit` 可选特性**：一个只影响**内部行为**、完全不改变公开 API 的 feature——很好的「feature 正确用法」范例；
3. **按目标拆分的两套依赖**：原生目标多一个 `pollster`（阻塞等待异步初始化），wasm 目标多一批 Web 依赖且给 wgpu 打开 `webgl` feature；
4. **dev-dependencies 里的 naga**：让测试可以在 CPU 侧校验 WGSL（u1-l3 详讲）。

#### 4.3.2 核心流程

feature 与条件依赖如何影响一次编译：

```text
cargo check -p gpui_wgpu
  └─ default = [] → 不启用 font-kit
       ├─ 编译 find_best_match（简化版，手写权重评分）
       └─ 不拉取 zed-font-kit 依赖

cargo check -p gpui_wgpu --features font-kit
  └─ 启用 font-kit feature → dep:font-kit 生效
       ├─ 从 GitHub 拉取 zed-industries/font-kit（固定 rev）
       ├─ 编译 find_best_match（font-kit 版，属性精确匹配）
       └─ 公开 API 面不变：两个版本都是私有函数

cargo build --target wasm32-unknown-unknown（经 gpui_web）
  └─ 走 [target.'cfg(target_family = "wasm")'] 段
       ├─ 带上 wasm-bindgen / web-sys(HtmlCanvasElement) / js-sys
       └─ wgpu 额外打开 "webgl" feature → WebGL2 回退可用
       （pollster 不编译；font-kit 也不会被启用）
```

#### 4.3.3 源码精读

**包定义与库根**（[Cargo.toml:1-12](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/Cargo.toml#L1-L12)）：包名 `gpui_wgpu`，`edition`/`publish` 继承 workspace，`[lib] path = "src/gpui_wgpu.rs"` 指定与 crate 同名的库根。这段代码定义了包的身份与入口文件。

```toml
[package]
name = "gpui_wgpu"
version = "0.1.0"
...
[lib]
path = "src/gpui_wgpu.rs"
```

**feature 与可选依赖**（[Cargo.toml:14-16](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/Cargo.toml#L14-L16) 与 [Cargo.toml:37-39](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/Cargo.toml#L37-L39)）：`font-kit = ["dep:font-kit"]` 声明 feature，可选依赖本体是 zed 维护的 font-kit 分叉，锁定在固定 git rev。这两段合起来说明：只有显式开启 feature 时才会拉取并编译这个依赖。

```toml
[features]
default = []
font-kit = ["dep:font-kit"]

# Optional: only needed on platforms with multiple font sources (e.g. Linux)
# WARNING: If you change this, you must also publish a new version of zed-font-kit to crates.io
font-kit = { git = "https://github.com/zed-industries/font-kit", rev = "94b0f28...", package = "zed-font-kit", version = "0.14.1-zed", optional = true }
```

注释写得很清楚：**只在「有多个字体来源的平台（例如 Linux）」才需要**；改动 rev 前必须先发布新版本 zed-font-kit（保证锁定的 rev 永远可解析）。

feature 在代码中的落点是 `cosmic_text_system` 模块里**同名函数的两套实现**：

- [src/cosmic_text_system.rs:755-779](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/cosmic_text_system.rs#L755-L779)：`#[cfg(feature = "font-kit")]` 版的 `find_best_match`，把 gpui 的 `Font` 和候选字体都转换成 font-kit 的 `Properties`（风格/字重/拉伸度，转换函数在 [src/cosmic_text_system.rs:953-989](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/cosmic_text_system.rs#L953-L989)），再调用 `font_kit::matching::find_best_match` 做 CSS 规范级别的精确匹配。
- [src/cosmic_text_system.rs:781-792](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/cosmic_text_system.rs#L781-L792)：`#[cfg(not(feature = "font-kit"))]` 版的 `find_best_match`，空候选报错、单一候选直接返回，多候选时走手写的字重/斜体评分。

注意：这两个函数都是**私有函数**——feature 开关只改变「同一请求内部如何挑字体」，crate 的公开 API 一行不变。

**按目标的条件依赖**（[Cargo.toml:41-49](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/Cargo.toml#L41-L49)）：这段代码把原生与 wasm 两套依赖分开——原生目标带上 `pollster`（把 wgpu 异步的 adapter/device 请求阻塞成同步调用），wasm 目标带上 Web 互操作依赖，并给 wgpu 追加 `webgl` feature。

```toml
[target.'cfg(not(target_family = "wasm"))'.dependencies]
pollster.workspace = true

[target.'cfg(target_family = "wasm")'.dependencies]
wasm-bindgen.workspace = true
wasm-bindgen-futures.workspace = true
web-sys = { version = "0.3", features = ["HtmlCanvasElement"] }
js-sys.workspace = true
wgpu = { workspace = true, features = ["webgl"] }
```

三个细节：

- `web-sys` 只要了 `HtmlCanvasElement` 一个 feature——对应 wasm 下用 HTML canvas 作为 wgpu surface 目标（`WgpuRenderer::new_from_canvas` 的入口，u2-l3/u6-l3 讲）；
- `wgpu` 在 wasm 侧**额外打开 `webgl` feature**：workspace 的 wgpu 依赖是共用的（`wgpu.workspace = true` 两处都写），但 wasm 目标上多开一个 feature，使 WebGL2 后端成为可回退项；
- 这也解释了 u1-l1 提过的「wasm 目标叠加 webgl feature」。

**谁在开启 font-kit**：真实消费者只有两个。gpui_linux 把 gpui_wgpu 声明为可选依赖并**无条件带上 `features = ["font-kit"]`（[gpui_linux/Cargo.toml:63](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_linux/Cargo.toml#L63)）**，由 `wayland`/`x11` 两个 feature 触发启用（[gpui_linux/Cargo.toml:17-19](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_linux/Cargo.toml#L17-L19)、[gpui_linux/Cargo.toml:34-35](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_linux/Cargo.toml#L34-L35)）；gpui_web 则是朴素的 `gpui_wgpu.workspace = true`（[gpui_web/Cargo.toml:23](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_web/Cargo.toml#L23)），不开 font-kit——浏览器里字体由系统另议，且 font-kit 这类原生库也无法服务 wasm。这正是 Cargo.toml 注释所说「Linux 才需要」的现实映射。

**dev-dependencies 与 bench**（[Cargo.toml:51-57](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/Cargo.toml#L51-L57)）：`naga`（带 `wgsl-in` feature）只在测试/基准时编译，用于在 CPU 侧解析校验 WGSL 变体；`[[bench]] name = "layout_line", harness = false` 声明 criterion 基准。这段配置是 u1-l3 的伏笔。

#### 4.3.4 代码实践

**实践目标**：亲手对比 feature 开关对编译产物与依赖图的影响，并回答「font-kit 在什么平台才有意义」。

**操作步骤**：

1. 在 zed 仓库根目录运行（首次编译整个依赖树可能需要较长时间）：
   ```bash
   cargo check -p gpui_wgpu
   ```
2. 再运行（需要网络访问 GitHub 拉取 zed-font-kit）：
   ```bash
   cargo check -p gpui_wgpu --features font-kit
   ```
3. 用依赖树工具观察差异：
   ```bash
   cargo tree -p gpui_wgpu -e features > /tmp/tree-default.txt
   cargo tree -p gpui_wgpu -e features --features font-kit > /tmp/tree-fontkit.txt
   diff /tmp/tree-default.txt /tmp/tree-fontkit.txt | head -50
   ```
4. 阅读上面 4.3.3 引用的两版 `find_best_match`，回答：feature 改变了公开 API 吗？改变了什么？

**需要观察的现象**：

- 步骤 2 会先 `Updating git repository https://github.com/zed-industries/font-kit`，然后多编译 zed-font-kit 及其独有依赖；
- 步骤 3 的 diff 里新增的子树都挂在 `font-kit` 依赖下，同时 feature 列表里多出 `gpui_wgpu feature "font-kit"` 与 `"dep:font-kit"`；
- 两次 check 的**警告/错误都应为 0**。

**预期结果**：确认 (a) font-kit 是纯内部行为开关，公开 API 不变；(b) 它有实际意义的平台是存在多个字体来源、需要精确字体匹配的 Linux 桌面（gpui_linux 无条件开启它），wasm 与其它目标不启用；(c) 代价是拉取一个 git 依赖并增加编译时间。

（编译耗时与 git 拉取结果**待本地验证**——取决于网络与机器；`cargo tree` 的 diff 输出同样待本地验证。另注：若想跑 clippy，Zed 仓库约定用仓库根的 `./script/clippy` 而不是裸 `cargo clippy`。）

#### 4.3.5 小练习与答案

**练习 1**：`default = []` 意味着什么？为什么这个 crate 的默认特性是空的？

**答案**：意味着不带 `--features` 编译时不启用任何非必需功能。font-kit 只对「多字体来源的平台」有用，若设为默认，macOS/Windows/wasm 等用不到它的目标也要付出拉取 git 依赖与编译的代价。默认空 + 由真正需要的消费者（gpui_linux）显式开启，是最省的方案。

**练习 2**：为什么 wasm 段里要给 wgpu **再写一次** `wgpu = { workspace = true, features = ["webgl"] }`？直接在通用依赖里加 `webgl` 不行吗？

**答案**：Cargo 允许在不同 target 段对同一依赖做特性并集，但通用段加 `webgl` 会让**所有目标**（包括原生）都编译 WebGL 相关代码。把它放进 wasm 段，原生构建就完全不带 webgl feature，wasm 构建则自动获得——同一份 workspace 版本声明，按目标裁剪特性。

**练习 3**：注释里的 WARNING（改 rev 前必须先发布新版 zed-font-kit 到 crates.io）在防什么事故？

**答案**：锁定的 `rev` 必须永远可解析。如果指向一个仅存在于某人本地的提交，或分叉仓库历史被改写，任何全新环境都无法复现构建。先发布 crates.io 版本意味着存在一个永久的镜像，git rev 失效时 Cargo 仍可回退到 crates.io 的版本解析。（这是供应链可复现性的防御措施。）

### 4.4 四个 WGSL 文件：不是 Rust 模块的「第五类源码」

#### 4.4.1 概念说明

`src/` 下还有 4 个 `.wgsl` 文件，它们**不出现在库根的 `mod` 声明里**——它们不是 Rust 模块，而是被 `include_str!` 在编译期嵌入的**数据**。之所以拆成 4 个文件而不是 1 个，是因为不同 GPU 后端需要不同的「实例数据传输方式」和「文本渲染方式」，crate 用**文件级拼接**来组合出三种完整着色器变体：

| 文件 | 内容 |
| --- | --- |
| `shaders.wgsl` | 公共部分：全局参数结构、gamma/对比度函数、各图元（quad/shadow/underline/sprite）的顶点与片元着色器 |
| `shaders_storage.wgsl` | 实例数据以 **storage buffer** 传入时的记录加载代码（原生后端） |
| `shaders_webgl.wgsl` | 实例数据以 **Rgba32Uint 纹理**传入时的记录加载代码（WebGL2 没有 storage buffer） |
| `shaders_subpixel.wgsl` | 亚像素文本渲染的额外管线（依赖 dual source blending 特性） |

#### 4.4.2 核心流程

三种变体的拼接发生在 `wgpu_renderer` 模块顶部：

```text
STORAGE_BUFFER_SHADERS = shaders.wgsl + shaders_storage.wgsl
      └─ 原生后端：实例走 storage buffer

WEBGL_SHADERS          = shaders.wgsl + shaders_webgl.wgsl
      └─ WebGL2 后端：实例走 uint 纹理

SUBPIXEL_SHADERS       = "enable dual_source_blending;" + shaders.wgsl
                         + shaders_storage.wgsl + shaders_subpixel.wgsl
      └─ 亚像素文本（仅 storage buffer 传输 + 双源混合能力）
```

运行时 `create_pipelines` 按当前上下文的能力（是否 WebGL、是否支持双源混合）选择用哪个变体创建管线——这是 u3-l2 的内容，本讲只需建立「4 个文件 → 3 种变体」的结构认知。

#### 4.4.3 源码精读

[src/wgpu_renderer.rs:21-43](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs#L21-L43)——三个着色器变体常量的定义：用 `concat!` + `include_str!` 在编译期把 WGSL 文件拼成完整源码字符串。每段常量上方的注释都写明了该变体面向的后端与原因。

```rust
/// Shader variant for backends with storage buffer support: the shared shader
/// logic plus the storage-buffer instance transport.
const STORAGE_BUFFER_SHADERS: &str = concat!(
    include_str!("shaders.wgsl"),
    include_str!("shaders_storage.wgsl"),
);

/// Shader variant for WebGL2, which has no storage buffers: ...
const WEBGL_SHADERS: &str = concat!(
    include_str!("shaders.wgsl"),
    include_str!("shaders_webgl.wgsl"),
);

/// Subpixel text rendering requires dual-source blending, which WebGL2 lacks...
/// The `enable` directive must precede all declarations.
const SUBPIXEL_SHADERS: &str = concat!(
    "enable dual_source_blending;\n",
    include_str!("shaders.wgsl"),
    include_str!("shaders_storage.wgsl"),
    include_str!("shaders_subpixel.wgsl"),
);
```

两个结构细节值得注意：

- `SUBPIXEL_SHADERS` 把 `"enable dual_source_blending;\n"` 放在拼接的**第一位**——WGSL 要求 `enable` 指令位于所有声明之前，拼接顺序因此不能随意调换（注释也专门强调了这一点）；
- 这些字符串随后交给 wgpu（内部由 naga）在**创建管线时**编译校验，因此 WGSL 的语法错误会在首次创建管线时暴露，而不是 cargo 编译期——不过本 crate 用 dev-dependency 里的 naga 写了测试来提前捕获（u1-l3）。

#### 4.4.4 代码实践

**实践目标**：把「4 个 WGSL 文件 → 3 种变体」的映射关系亲手核实一遍，并理解 `enable` 指令的位置约束。

**操作步骤**：

1. 执行：
   ```bash
   grep -n "include_str!" src/wgpu_renderer.rs
   ```
2. 对照三个常量的拼接顺序，画出 4.4.2 的拼接图。
3. 打开 `src/shaders.wgsl` 看前 30 行，确认它以结构体/函数定义开头（没有任何 `enable` 指令）；再打开 `src/shaders_subpixel.wgsl` 确认它自身也不以 `enable` 开头——`enable` 是拼接时插入的。
4. 思考实验（不必真的改）：如果把 `SUBPIXEL_SHADERS` 里 `"enable dual_source_blending;\n"` 挪到最后一段，会发生什么？

**需要观察的现象**：

- `include_str!` 恰好出现若干次、全部集中在三个常量定义里，没有散落在别处；
- `shaders.wgsl` 被三个变体共享；`shaders_storage.wgsl` 被两个变体共享；`shaders_webgl.wgsl` 与 `shaders_subpixel.wgsl` 各只被一个变体使用。

**预期结果**：确认拼接模型；第 4 步的答案是——`enable` 出现在声明之后违反 WGSL 语法，naga 编译着色器时会报错，该变体的管线创建失败（亚像素文本管线是 `Option`，失败时回退普通管线，这个降级链在 u4-l4 展开）。

（纯阅读实践，无需 GPU；「回退」行为待 u4-l4 / 本地验证。）

#### 4.4.5 小练习与答案

**练习 1**：为什么不把 WGSL 写成 Rust 字符串常量，而要放在独立 `.wgsl` 文件里用 `include_str!` 嵌入？

**答案**：独立文件可以获得编辑器的 WGSL 语法高亮/补全、格式化工具支持，且避免在 Rust 字符串里处理转义；`include_str!` 又保证了文件内容与二进制同步（编译期嵌入，不存在运行时读文件失败的问题）。是「编辑体验」与「分发可靠性」的双赢。

**练习 2**：`shaders_storage.wgsl` 和 `shaders_webgl.wgsl` 为什么不能合并成一个文件、用 `#if` 之类的条件编译区分？

**答案**：WGSL 没有类似 C 的预处理器条件编译（能力查询走 `enable`/绑定模型，且实例传输方式涉及不同的资源类型声明：storage buffer 是 `var<storage>`，纹理是 `texture_2d`，绑定布局完全不同）。文件级拼接是 wgpu 生态里常见且简单的变体管理方式。

**练习 3**：如果未来新增第四种后端需要「公共部分 + 新传输层」，按现有模式应该怎么做？

**答案**：新增一个 `shaders_xxx.wgsl`，再在 `wgpu_renderer.rs` 加一个 `concat!` 常量（公共部分 + 新文件），并在 `create_pipelines` 的能力判断里增加一个分支。模式可无痛扩展——这正是 4.4 结构的价值。

## 5. 综合实践

**任务：给 gpui_wgpu 制作一张「API 全景卡片」，并用 cargo doc 检验它。**

经过本讲，你已经掌握了模块划分、公开 API 面和构建配置。现在把它们串起来：

1. **浏览文档**：在仓库根目录运行：
   ```bash
   cargo doc -p gpui_wgpu --no-deps --open
   ```
   （`--no-deps` 只文档化本 crate，速度快；浏览器会自动打开。）
2. **清点导出**：在左侧 crate 列表里逐一点开，记录所有导出的 struct / enum / type alias，以及两个 trait 实现（`impl PlatformAtlas for WgpuAtlas`、`impl PlatformTextSystem for CosmicTextSystem`）在文档中的呈现位置。
3. **对照验证**：把你看到的清单与 4.2.3 的表格对照。特别注意：在**原生目标**下，`WebBackendPreference` 和 `PreparedWebGraphics` 应该**不在**文档里（它们被 `#[cfg(target_family = "wasm")]` 门控）——这是「同一 crate 在不同目标下 API 面不同」的直接证据。
4. **选出 5 个核心 API** 并各写一句「为什么它是核心」。参考答案（你的选择可以不同，关键是理由）：
   - `WgpuRenderer`——本 crate 的执行主体，`draw(&Scene) -> bool` 是每帧唯一入口；
   - `WgpuContext`——一切 GPU 能力的来源，跨窗口共享；
   - `GpuContext`（类型别名）——多窗口设备恢复协调的枢纽，一行定义连接了 u6-l1 的整个恢复机制；
   - `WgpuAtlas`——`PlatformAtlas` 的 wgpu 实现，文本与图片的公共底座；
   - `CosmicTextSystem`——`PlatformTextSystem` 的实现，另一条主线的全部。
5. **完成全景卡片**：一张表概括本 crate——每行一个模块，列出「职责 / 公开类型 / 实现的平台 trait / 被谁消费 / 对应构建配置（feature 或 target 段）」。

**预期产出**：一张可直接放进学习笔记的卡片 + 一份带理由的 5 API 清单。文档内容与 4.2.3 表格的一致性、以及 wasm 类型在原生文档中缺席的现象，**待本地验证**（cargo doc 渲染细节可能随 rustdoc 版本略有差异）。

## 6. 本讲小结

- gpui_wgpu 的库根只有 10 行：4 个**私有** `mod` + 汇聚式 `pub use`，构成典型门面模式；`wgpu_renderer` 用**显式列举**（3 项）而非 glob 再导出，把 API 承诺写在明处。
- `pub use wgpu;` 让下游通过 `gpui_wgpu::wgpu::...` 使用 wgpu 类型，保证版本对齐（workspace 锁定 `wgpu = "29.0.4"`）。
- 四个模块两条主线：渲染主线 `wgpu_context`（GPU 连接）→ `wgpu_atlas`（资源）→ `wgpu_renderer`（执行），文本主线 `cosmic_text_system`；后两者分别实现 gpui 的 `PlatformAtlas` 与 `PlatformTextSystem` 两个平台扩展点。
- `Cargo.toml` 的四个要点：`[lib] path` 指定同名库根；`font-kit` 是**纯内部行为**的可选特性（两版私有 `find_best_match` 按 cfg 切换，公开 API 不变，仅 gpui_linux 开启）；依赖按 `cfg(target_family = "wasm")` 拆两套（原生 `pollster`，wasm 侧 Web 依赖 + wgpu `webgl` feature）；dev-deps 里的 naga 服务 WGSL 校验测试。
- 4 个 WGSL 文件不是 Rust 模块，而是 `include_str!` + `concat!` 拼出的 3 种着色器变体（storage / webgl / subpixel）；`enable dual_source_blending` 必须拼接在最前，因为 WGSL 要求 `enable` 先于一切声明。
- 本 crate 的结构美学：**实现可以很厚（wgpu_renderer 2000+ 行），接口必须很薄（每个模块 1–5 个公开类型）**。

## 7. 下一步学习建议

本讲之后，你已经能独立读懂这个 crate 的任何文件头并说出它的角色。建议的下一步：

1. **u1-l3（跑起来再说：测试与基准初探）**：本讲多次提到的 naga WGSL 校验测试就在 `wgpu_renderer.rs` 末尾的 `tests` 模块里，下一讲亲手运行它们。
2. 提前浏览第二单元的第一讲素材：[src/wgpu_context.rs:9-18](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L9-L18) 的 `WgpuContext` 字段——你现在已经认识它的每个字段属于哪条主线，u2-l1 将展开 `Instance/Adapter/Device/Queue` 四层对象模型。
3. 如果你想立刻看到「门面之外」的世界，可以带着本讲的公开 API 清单去读 `crates/gpui_linux/src/linux/wayland/window.rs` 中创建 `WgpuRenderer` 的调用点（u6-l3 会系统讲解），验证下游确实只用这十来个类型就把整个渲染系统装配了起来。

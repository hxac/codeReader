# PlatformTextSystem：字体加载、整形与四套字体栈

## 1. 本讲目标

学完本讲，你应该能够：

1. 不看源码，列出 `PlatformTextSystem` trait 的 12 个方法，并按「字体管理 / 字形度量查询 / 整形与光栅化」三组归类，说出每个方法的输入输出。
2. 说出 `FontId`、`GlyphId`、`FontRun`、`ShapedRun`、`ShapedGlyph`、`RenderGlyphParams` 这些类型在「一段文字 → 屏幕像素」的渲染管线中各自处在哪一站。
3. 对比四套字体引擎实现的真实分工：macOS 的 CoreText + font-kit + open_type、Linux 与 Web 共用的 cosmic-text + swash（`CosmicTextSystem`）、Windows 的 DirectWrite + D3D11，以及退路 `NoopTextSystem`；理解它们在 `recommended_rendering_mode`、emoji 光栅化、字形回退上的取舍差异。

本讲是第 8 单元（高级主题）的第一讲：它向上承接 u2-l1 建立的「Platform trait 八大分组」地图（文本系统是其中第一组），向下为 u8-l2 的 `PlatformAtlas` 与渲染后端铺路——光栅化出来的字形位图，正是下一讲图集（atlas）的原料。

## 2. 前置知识

本讲默认你已读完 u1 全部四讲和 u2-l1，知道 `gpui_platform` 是门面 crate、`Platform` 是 gpui 与操作系统之间的契约、平台对象以 `Rc<dyn Platform>` 注入应用。在此之外，先补几项字体学基础，用通俗语言过一遍：

- **字形（glyph）与字符（character）**：字符是「语义单位」（比如字母 a），字形是「绘制单位」（字体文件里编号为 68 的那条轮廓）。一个字符在不同字体里对应不同字形；连字（ligature）里两个字符可能合成一个字形。GPUI 用 `GlyphId(u32)` 标识字形，用字体内部的编号。
- **字体族（family）与字重/风格（weight/style）**：我们说「Helvetica」指的是字体族；同族下还有 Regular、Bold、Italic 等多个「面（face）」。gpui 的 `Font` 结构体就是「族名 + 特性 + 回退 + 字重 + 风格」的组合描述。
- **整形（shaping）**：把「字符序列 + 字体」翻译成「字形序列 + 每个字形的位置」的过程。它不是一对一查表：阿拉伯文要连笔、印度文要元音重排、拉丁连字会把 f+i 换成 ﬁ。这是 `layout_line` 方法的核心工作。
- **em square 与字体度量（metrics）**：字体内部用一个抽象方块「em」做单位，`units_per_em` 是每个 em 分成多少份（常见 1000 或 2048）。`ascent`（基线以上高度）、`descent`（基线以下深度，通常为负值）、`line_gap`（建议行间距）等单位都是字体单位，换算成像素的公式是 \( \text{像素} = \frac{\text{字体单位}}{\text{units\_per\_em}} \times \text{font\_size} \)。所谓单倍行高近似为 \( \text{ascent} - \text{descent} + \text{line\_gap} \)。
- **光栅化（rasterization）**：把字形轮廓填充成像素位图的过程。GPUI 的约定：普通字形输出「每像素 1 字节」的灰度掩码；emoji 输出 BGRA 彩色位图；若请求了亚像素渲染（subpixel，即 ClearType 风格的 RGB 分通道抗锯齿），则输出每像素 4 字节。
- **`Arc<dyn Trait>` 与 `Send + Sync`**：文本系统是极少数被 `Arc` 共享、允许后台线程使用的平台设施（例如后台任务里做文本测量），所以 [PlatformTextSystem](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/platform.rs#L1072) 直接要求 `Send + Sync`，而 `Platform` 本身以 `Rc` 锁在主线程（对照 u4-l1）。因此每个实现内部都用 `RwLock`/`Mutex` 包住可变状态。

一个先摆出来的结论（也是对大纲里「Linux 用 font-kit」这一旧印象的纠正）：**font-kit 只出现在 macOS 实现里**。当前源码中 Linux 的 `text_system` 模块只有一行代码——再导出 `gpui_wgpu::CosmicTextSystem`，Linux 与 Web 共用一套纯 Rust 字体栈（cosmic-text 负责字体数据库与整形，swash 负责度量与光栅化）。本讲第 4.3 节会详细展开。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [gpui/src/platform.rs:L1071-L1103](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/platform.rs#L1071-L1103) | `PlatformTextSystem` trait 定义，本讲契约层主战场 |
| [gpui/src/platform.rs:L1105-L1233](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/platform.rs#L1105-L1233) | `NoopTextSystem`：无字体引擎时的假实现 |
| [gpui/src/text_system.rs:L35-L48](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/text_system.rs#L35-L48) | `FontId` 等核心句柄类型 |
| [gpui/src/text_system.rs:L50-L166](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/text_system.rs#L50-L166) | `TextSystem`：gpui 自己的缓存层，包住平台实现 |
| [gpui/src/text_system.rs:L1011-L1133](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/text_system.rs#L1011-L1133) | `GlyphId`、`RenderGlyphParams`、`Font`、`FontMetrics` 数据模型 |
| [gpui/src/text_system/line_layout.rs:L14-L54](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/text_system/line_layout.rs#L14-L54) | `LineLayout` / `ShapedRun` / `ShapedGlyph`：整形的输出模型 |
| [gpui_macos/src/text_system.rs:L56-L230](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_macos/src/text_system.rs#L56-L230) | `MacTextSystem`：CoreText + font-kit 实现（契约实现层） |
| [gpui_macos/src/open_type.rs:L34-L75](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_macos/src/open_type.rs#L34-L75) | macOS 把 OpenType 特性与回退链注入 CTFont 的辅助模块 |
| [gpui_linux/src/linux/text_system.rs:L1](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_linux/src/linux/text_system.rs#L1) | Linux「文本系统」的全部：一行再导出 |
| [gpui_wgpu/src/cosmic_text_system.rs:L24-L204](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_wgpu/src/cosmic_text_system.rs#L24-L204) | `CosmicTextSystem`：Linux/Web 共用的 cosmic-text + swash 实现 |
| [gpui_windows/src/direct_write.rs:L38-L51](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_windows/src/direct_write.rs#L38-L51) | `DirectWriteTextSystem`：Windows 的 DirectWrite 实现（结构与契约入口） |
| [gpui/src/app.rs:L779-L806](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/app.rs#L779-L806) | `App::new_app`：启动时把平台文本系统装进缓存层 |

> 提示：本讲涉及四个兄弟 crate 的源码。在自己机器上阅读时，从 `crates/gpui_platform` 出发，用 `../gpui/src/platform.rs`、`../gpui_wgpu/src/cosmic_text_system.rs` 这样的相对路径即可找到它们；所有永久链接均指向当前 HEAD `f36aec82`。

## 4. 核心概念与源码讲解

### 4.1 PlatformTextSystem：平台无关的字体引擎契约

#### 4.1.1 概念说明

为什么「字体」要进平台层？因为每个操作系统都自带一套成熟且免费的字体引擎：macOS 有 CoreText、Windows 有 DirectWrite，它们直接读系统字体目录、做整形、做 ClearType 级别的光栅化，效果和原生应用完全一致；而 Linux 没有唯一钦定的引擎，GPUI 选择了 Rust 生态的 cosmic-text。`PlatformTextSystem` 就是把「字体怎么加载、怎么整形、怎么光栅化」这些因平台而异的能力，抽象成 12 个方法的契约。

它与其他平台 trait 的最大不同是**线程模型**：u2-l1 讲过 `Platform` 以 `Rc<dyn Platform>` 存在于主线程，而文本系统的使用者常常是后台任务（比如在后台线程测量文本宽度来计算表格列宽），所以它的获取入口是 `Platform::text_system()`，返回的是能跨线程共享的 `Arc<dyn PlatformTextSystem>`，trait 自身标注 `Send + Sync`。这也解释了后面三套实现的共同形态：**结构体里只有一个 `RwLock<状态>` 字段**，读操作拿读锁、整形拿写锁。

契约的 13 个方法按职责分三组：

| 分组 | 方法 | 作用 |
| --- | --- | --- |
| 字体管理 | `add_fonts` | 注入内存中的字体文件字节（Zed 自带的捆绑字体走这条路） |
| | `all_font_names` | 枚举系统所有字体族名（设置面板的字体下拉框数据源） |
| | `font_id` | 把 `Font` 描述（族名+字重+风格+特性+回退）解析成 `FontId` |
| 字形度量查询 | `font_metrics` | 整套字体度量（ascent、descent、cap_height 等） |
| | `typographic_bounds` / `advance` | 单个字形的边界框 / 步进宽度 |
| | `glyph_for_char` | 单字符查字形 id（不含整形，用于等宽字体快速路径） |
| 整形与光栅化 | `layout_line` | **核心**：一行文本 + 若干 `FontRun` → `LineLayout`（字形+位置） |
| | `glyph_raster_bounds` / `rasterize_glyph` | 字形位图的范围与像素数据 |
| 渲染策略 | `recommended_rendering_mode` | 建议灰度还是亚像素渲染 |
| | `glyph_dilation_for_color`（默认 0） | macOS 专属：按文字颜色亮度决定字形加粗程度 |

#### 4.1.2 核心流程

先建立全景：一段文字从字符串到屏幕像素，在 GPUI 里要走六站，`PlatformTextSystem` 占据中间四站。

```text
① 应用层描述字体                ② 解析成句柄           ③ 整形
   Font { family, weight, ... } ──font_id()──▶ FontId ──layout_line()──▶ LineLayout
                                                                      │ ShapedRun { font_id, glyphs }
                                                                      │ ShapedGlyph { id, position, index, is_emoji }
④ 请求光栅化                    ⑤ 光栅化                ⑥ 进入图集、GPU 绘制（u8-l2）
   RenderGlyphParams ──glyph_raster_bounds()──▶ Bounds ──rasterize_glyph()──▶ (尺寸, 位图字节) ──▶ PlatformAtlas
```

关键的数据接力是：

1. 应用层把文本切成若干 `FontRun`（「接下来 `len` 个字节用 `font_id` 这个字体」），连同整行文本交给 `layout_line`。
2. 平台实现调用各自引擎整形，返回 `LineLayout`：一行里所有 `ShapedRun`（同字体的字形段），每个 `ShapedGlyph` 带字形 id、像素位置和**原始文本的 UTF-8 字节下标**（编辑器要用它做光标定位）。
3. 绘制时，每个字形连同尺寸、亚像素变体、缩放因子打包成 `RenderGlyphParams`——它实现 了 `Hash`，直接充当光栅缓存与精灵图集的 key（`AtlasKey::Glyph`）。
4. `rasterize_glyph` 按 `is_emoji` 与 `subpixel_rendering` 输出灰度掩码或彩色位图，交给下一讲的 `PlatformAtlas` 装箱上传。

为什么 `RenderGlyphParams` 里有个 `subpixel_variant: Point<u8>`？因为亚像素定位把一个像素分成 4 个相位（`SUBPIXEL_VARIANTS_X = 4`），同一字形在不同相位下光栅化结果不同，必须分开缓存。这是文本渲染清晰度的关键细节。

#### 4.1.3 源码精读

**契约本体**。trait 定义在 platform.rs 中 `PlatformDispatcher` 之后，全部方法签名如下（节选注释）：

```rust
pub trait PlatformTextSystem: Send + Sync {
    fn add_fonts(&self, fonts: Vec<Cow<'static, [u8]>>) -> Result<()>;
    fn all_font_names(&self) -> Vec<String>;
    fn font_id(&self, descriptor: &Font) -> Result<FontId>;
    fn font_metrics(&self, font_id: FontId) -> FontMetrics;
    fn typographic_bounds(&self, font_id: FontId, glyph_id: GlyphId) -> Result<Bounds<f32>>;
    fn advance(&self, font_id: FontId, glyph_id: GlyphId) -> Result<Size<f32>>;
    fn glyph_for_char(&self, font_id: FontId, ch: char) -> Option<GlyphId>;
    fn glyph_raster_bounds(&self, params: &RenderGlyphParams) -> Result<Bounds<DevicePixels>>;
    fn rasterize_glyph(&self, params: &RenderGlyphParams,
        raster_bounds: Bounds<DevicePixels>) -> Result<(Size<DevicePixels>, Vec<u8>)>;
    fn layout_line(&self, text: &str, font_size: Pixels, runs: &[FontRun]) -> LineLayout;
    fn recommended_rendering_mode(&self, _font_id: FontId,
        _font_size: Pixels) -> TextRenderingMode;
    fn glyph_dilation_for_color(&self, _color: Hsla) -> u8 { 0 }   // 唯一带默认实现的方法
}
```

见 [gpui/src/platform.rs:L1071-L1103](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/platform.rs#L1071-L1103)，这段代码就是契约的全部：11 个必须实现的方法加 1 个带默认实现的 `glyph_dilation_for_color`（[L1100-L1102](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/platform.rs#L1100-L1102)，默认不加粗，只有 macOS 覆盖）。`TextRenderingMode` 三档枚举（PlatformDefault / Subpixel / Grayscale）定义在 [gpui/src/platform.rs:L2125-L2135](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/platform.rs#L2125-L2135)。

**获取入口**。`Platform` trait 上的取用方法是 [gpui/src/platform.rs:L129](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/platform.rs#L129) 的 `fn text_system(&self) -> Arc<dyn PlatformTextSystem>;`——注意返回 `Arc` 而非 `Rc`，与其他平台能力的取用方式形成对照。

**核心句柄与参数类型**（都在 gpui 主 crate 的 text_system.rs）：

- [FontId(pub usize):L35-L38](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/text_system.rs#L35-L38)：**平台实现自己的下标**。macOS 上是 `MacTextSystemState.fonts` 的下标，cosmic 实现里是 `loaded_fonts` 的下标——所以 `FontId` 不能跨平台、甚至不能跨文本系统实例比较，它只是当前进程内的临时句柄。
- [GlyphId(pub u32):L1011-L1014](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/text_system.rs#L1011-L1014)：字体文件内部的字形编号。
- [RenderGlyphParams:L1021-L1032](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/text_system.rs#L1021-L1032)：光栅化请求的全部参数（字体、字形、字号、亚像素相位、缩放、是否 emoji、是否亚像素渲染、加粗等级），手动实现了 `Hash`（[L1036-L1047](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/text_system.rs#L1036-L1047)）以充当缓存 key。
- [Font:L1050-L1068](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/text_system.rs#L1050-L1068)：应用层字体描述，注意族名里的魔法值 `.SystemUIFont` 表示「平台默认 UI 字体」。
- [FontMetrics:L1103-L1133](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/text_system.rs#L1103-L1133)：九个字段的度量包；紧随其后的 [像素换算辅助方法:L1135-L1176](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/text_system.rs#L1135-L1176)（`ascent(font_size)` 等）做的就是第 2 节那条公式。

**整形的输出模型**（line_layout.rs）：

```rust
pub struct LineLayout { pub font_size: Pixels, pub width: Pixels, pub ascent: Pixels,
    pub descent: Pixels, pub runs: Vec<ShapedRun>, pub len: usize }
pub struct ShapedRun { pub font_id: FontId, pub glyphs: Vec<ShapedGlyph> }
pub struct ShapedGlyph { pub id: GlyphId, pub position: Point<Pixels>,
    pub index: usize, pub is_emoji: bool }
```

见 [gpui/src/text_system/line_layout.rs:L14-L54](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/text_system/line_layout.rs#L14-L54)。`ShapedRun` 存在的原因：一行文本可能因为回退被拆成多段（前半用主字体、后半的中文用了回退字体），每段各自持有 `font_id`。输入侧的 `FontRun { len, font_id }` 定义在 [L874-L880](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/text_system/line_layout.rs#L874-L880)。`LineLayout` 上还挂着 `index_for_x` / `closest_index_for_x` 等点击定位方法——编辑器把鼠标 x 坐标换算成字符下标就靠它们。

**缓存层 TextSystem**。平台实现不会裸奔：gpui 主 crate 用 [TextSystem:L50-L59](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/text_system.rs#L50-L59) 把 `Arc<dyn PlatformTextSystem>` 包起来，加三层缓存（`font_ids_by_font`、`font_metrics`、`raster_bounds`）和一个回退字体栈。装配发生在 [App::new_app: gpui/src/app.rs:L794](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/app.rs#L794)：`let text_system = Arc::new(TextSystem::new(platform.text_system()));`。此后应用代码通过 [App::text_system():L1995-L1996](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/app.rs#L1995-L1996) 拿到的是缓存层。两个值得读的点：

- [resolve_font:L148-L166](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/text_system.rs#L148-L166)：先试请求的字体，失败则沿 `fallback_font_stack`（`.ZedMono` → `.ZedSans` → Helvetica → Segoe UI → Ubuntu → …，[L63-L85](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/text_system.rs#L63-L85)）逐个降级，全部失败才 panic。这就是「字体名打错也能显示文字」的兜底机制。
- [read_metrics:L292-L304](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/text_system.rs#L292-L304)：`upgradable_read` 先试读锁缓存，未命中升级写锁并调 `platform_text_system.font_metrics`——这是「读多写少 + 偶尔填充」缓存的标准写法，后面三套平台实现里还会反复看到同款锁用法。

**NoopTextSystem**。[gpui/src/platform.rs:L1105-L1233](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/platform.rs#L1105-L1233) 提供了一个「假但自洽」的实现：`font_id` 永远返回 `FontId(1)`，`glyph_for_char` 返回 `GlyphId(ch.len_utf16())`（ BMP 内字符映射到 1、增补平面字符映射到 2），`layout_line` 按「m 字宽」等宽排布，emoji 占两格。它在哪些场合登场？u5-l2 讲过 Linux 零 feature 构建会拿到它；本讲 4.2/4.4 会看到 macOS 关掉 `font-kit` feature、Windows headless 模式也会换成它。它的价值是让整条渲染管线在没有真实字体引擎时依然可运行（布局、实体、绘制照常，只是文字退化成占位方块）。

#### 4.1.4 代码实践

**实践目标**：亲手调用契约的消费端，观察你机器上真实的字体枚举与解析结果。

1. 在 Zed 仓库根目录运行官方示例，确认环境可用（gpui 的 dev-dependencies 已开启 `font-kit`、`wayland`、`x11` feature，见 [gpui/Cargo.toml:L135](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/Cargo.toml#L135)）：

   ```bash
   cargo run -p gpui --example text
   ```

2. 新建一个独立小 crate（或直接改造 `crates/gpui/examples/text_layout.rs` 的 `run_example`），在启动回调里枚举字体并解析三个族（以下为示例代码）：

   ```rust
   use gpui::{App, font, px};
   use gpui_platform::application;

   fn main() {
       application().run(|cx: &mut App| {
           let ts = cx.text_system();
           let names = ts.all_font_names();
           println!("系统字体族数量: {}", names.len());
           for name in names.iter().take(10) {
               println!("  {name}");
           }
           for family in ["Helvetica", ".SystemUIFont", "不存在的字体Xyz"] {
               let font_id = ts.resolve_font(&font(family));   // 失败会走回退栈
               println!("{family:>18} -> FontId({:?})", font_id);
               println!(
                   "  ascent={}px descent={}px cap_height={}px",
                   ts.ascent(font_id, px(16.)),
                   ts.descent(font_id, px(16.)),
                   ts.cap_height(font_id, px(16.)),
               );
           }
           cx.quit();   // 打印完直接退出事件循环
       });
   }
   ```

3. 需要观察的现象：`all_font_names()` 的返回里除了系统字体还混着 `.ZedMono`、`.ZedSans`、`.SystemUIFont`——读 [TextSystem::all_font_names:L88-L99](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/text_system.rs#L88-L99)，想想为什么缓存层要在平台结果上再追加这些名字（提示：`resolve_font` 的回退栈引用了它们）。
4. 预期结果：第三个「不存在的字体」不会报错，而是解析到某个回退字体的 `FontId`；`ascent + |descent|` 约等于该字体 16px 下的单倍行高。具体数值随平台字体而异，待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `FontId` 用 `usize` 而 `GlyphId` 用 `u32`？
答案：`FontId` 是文本系统内部的字体表下标（进程内、数量小、还要当 `Vec` 索引用），`usize` 最自然；`GlyphId` 是字体文件格式里的字形编号，OpenType 规范里是 16/32 位无符号数，`u32` 足够且 `#[repr(C)]` 便于传给 C API（macOS 的 `CGGlyph` 就是 u16）。

**练习 2**：`layout_line` 为什么需要调用方提供 `FontRun` 列表，而不是只给一个 `Font`？
答案：一行文本可以由多种字体拼成——粗体高亮段、不同语种的回退段。`FontRun` 让上层（`WindowTextSystem` 的 shape 逻辑）先决定「哪段文字用哪个字体」，整形器再在每个 run 内做字形组合与定位；输出侧的 `ShapedRun` 与之一一呼应，且可能因引擎内部的二次回退而比输入 run 更碎。

**练习 3**：`NoopTextSystem::layout_line` 里 `glyph.0 == 2` 的分支在处理什么？
答案：`glyph_for_char` 把 UTF-16 长度为 2 的字符（emoji 等增补平面字符）映射为 `GlyphId(2)`，`layout_line` 据此判定该「字形」是 emoji，占两个 em 宽度（见 [gpui/src/platform.rs:L1195-L1201](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/platform.rs#L1195-L1201)）——用最粗糙的方式保住了「emoji 比普通字符宽」这一布局事实。

### 4.2 macos::text_system：CoreText 整形与 font-kit 加载

#### 4.2.1 概念说明

macOS 实现是三套真实引擎里最「混搭」的一套：**font-kit 负责字体加载与度量，CoreText 负责整形，CoreGraphics 负责光栅化，自家 open_type 模块负责把 OpenType 特性与回退链塞回 CoreText**。为什么不全用 CoreText？因为 CoreText 的 C API 在 Rust 里包装成本高，而 Zed 维护的 font-kit fork 已经把「按族名枚举 face、读度量、查字形边界」这些琐碎查询封装成了纯 Rust 接口；但整形（含连字、双向文本、级联回退）CoreText 无可替代，于是两者各取所长。

它受 feature 门控：`MacPlatform::new` 里只有编译了 `font-kit` feature 才构造 `MacTextSystem`，否则打警告并退化为 `NoopTextSystem`（[gpui_macos/src/platform.rs:L200-L211](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_macos/src/platform.rs#L200-L211)）。门面 crate 的 `font-kit` feature 正是为此透传（[gpui_platform/Cargo.toml:L16](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/Cargo.toml#L16)）——这也是本套手册里 font-kit 唯一出现的位置。

#### 4.2.2 核心流程

`MacTextSystem(RwLock<MacTextSystemState>)` 的状态里维护四张表：内存字体源、系统字体源、已加载字体 `Vec`，以及 `Font → FontId` / `FontKey → 候选列表` / `postscript 名 → FontId` 三张映射。以 `layout_line("Hello 世界", 16px, runs)` 为例：

1. **准备属性字符串**：为每个 `FontRun` 把文本追加进 `CFMutableAttributedString`，并把对应 `CTFont`（已按字号缩放）设为该区间的 `kCTFontAttributeName` 属性。CoreText 的 API 用 UTF-16 计数，所以这里全程做 UTF-8 → UTF-16 的范围换算。
2. **防连字微调**：对第一个 run 把字号加上一个 ULP（`f32::next_up()`）、第二个 run 用原字号，交替进行。字号上极微小的差异会让 CoreText 认为相邻字符「不同源」，从而打断跨 run 边界的连字——否则 run 交界处的 fi 可能被整形器合成连字，导致字形归属混乱。
3. **整形**：`CTLine::new_with_attributed_string` 一口气完成断行判断、连字、双向排列与级联回退，产出若干 `CTRun`。
4. **回收结果**：遍历每个 run 的字形 id、位置、字符串下标，把 UTF-16 下标经 `StringIndexConverter` 转回 UTF-8 下标，按 `font_id` 归并成 `ShapedRun` 列表；行宽取 `CTLine` 的排版边界，ascent/descent 取各 run 字体度量的最大值。

光栅化则分两条路：普通字形建「Alpha-only 灰度」位图上下文（每像素 1 字节），emoji 建「预乘 RGB」上下文（每像素 4 字节），都用 `CTFont.draw_glyphs` 画进去；若请求了 dilation（见下文），还要打开 CoreGraphics 的字体平滑（font smoothing）模拟加粗。

#### 4.2.3 源码精读

**结构体与实现入口**。[gpui_macos/src/text_system.rs:L56-L88](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_macos/src/text_system.rs#L56-L88) 定义 `MacTextSystem(RwLock<MacTextSystemState>)` 与四张表。`impl PlatformTextSystem for MacTextSystem` 从 [L97](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_macos/src/text_system.rs#L97) 开始，全是「拿锁、委托给 state 的方法」的薄壳——真正逻辑都在 `impl MacTextSystemState`（[L255 起](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_macos/src/text_system.rs#L255)）。

**字体枚举的绕坑**。[all_font_names:L102-L134](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_macos/src/text_system.rs#L102-L134) 没有用 core-text crate 封装好的 `get_descriptors()`，而是直接声明并调用 C 函数 `CTFontCollectionCreateMatchingFontDescriptors`——注释写明原因：core-text v21.0.0 对该结果的内存管理规则用错了（Create Rule 下误用 Get Rule 包装，造成泄漏）。这是平台绑定层「绕过上游 bug」的鲜活样本。它还会把 `add_fonts` 注入的内存字体族名追加进结果（[L130-L132](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_macos/src/text_system.rs#L130-L132)）。

**font_id 的两段查表**。[font_id:L136-L174](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_macos/src/text_system.rs#L136-L174)：先查 `font_selections`（完整 `Font` 描述 → id）缓存；未命中则按 `FontKey`（族+特性+回退）取出该族全部候选 face，用 font-kit 的 `find_best_match` 按属性（style/weight/stretch）挑出最匹配的一个。`upgradable_read` → `upgrade` 的锁升级写法与 4.1 读到的 `read_metrics` 同款。

**layout_line 主体**。[L532-L628](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_macos/src/text_system.rs#L532-L628)。几个必看片段：

```rust
let font_size = if break_ligature {
    px(f32::from(font_size).next_up())   // 跨 run 边界打断连字：字号加一个 ULP
} else {
    font_size
};
...
let line = CTLine::new_with_attributed_string(string.as_concrete_TypeRef());  // CoreText 整形
```

（[L559-L575](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_macos/src/text_system.rs#L559-L575)）。字形回收时若发现新的原生字体（CoreText 级联回退引入的），用 `id_for_native_font` 动态登记进字体表再取 `FontId`（[L579-L598](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_macos/src/text_system.rs#L579-L598)）——**输出的 `ShapedRun` 可能比输入的 `FontRun` 多，`FontId` 空间在整形过程中动态生长**，这是三套实现的共同行为。UTF-16→UTF-8 的下标换算由只进不退的游标 [StringIndexConverter:L631-L660](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_macos/src/text_system.rs#L631-L660) 完成，遇到下标回退（双向文本重排后可能发生）就重建游标。

**光栅化**。[raster_bounds:L421-L434](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_macos/src/text_system.rs#L421-L434) 用 font-kit 算边界并四向扩 1 像素给抗锯齿留余量；[rasterize_glyph:L436-L530](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_macos/src/text_system.rs#L436-L530) 按 emoji/普通选色彩空间建 `CGContext`，翻转坐标系对齐 font-kit 的边界约定，启用亚像素定位，最后 emoji 路径还要把「预乘 RGBA」逐像素换成「直通 BGRA」（[L521-L526](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_macos/src/text_system.rs#L521-L526)）——GPUI 的图集统一吃 BGRA。

**macOS 专属：glyph_dilation_for_color**。[L218-L229](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_macos/src/text_system.rs#L218-L229) 复刻了 CoreGraphics 的一条私有行为：开启字体平滑时，macOS 会按文字颜色的亮度把笔画「加粗」0～4 级——白字最粗、黑字不加粗。按亮度 \( L = 0.2126R + 0.7152G + 0.0722B \) 分档，返回 `floor(4L)` 并夹在 0..=4。这个值经 `RenderGlyphParams.dilation` 一路传进光栅化（[L502-L508](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_macos/src/text_system.rs#L502-L508) 用它设置灰色填充色），是为了让 GPUI 自绘的文字观感与系统原生一致。

**open_type 模块**。[apply_features_and_fallbacks:L34-L75](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_macos/src/open_type.rs#L34-L75) 解决的问题是：用户在设置里开了 `calt`/`ss01` 之类的 OpenType 特性、或指定了自定义回退链，而 font-kit 的加载路径不带这些参数。它手工拼一个 `CFDictionary`（键为 `kCTFontFeatureSettingsAttribute`，有回退时再加 `kCTFontCascadeListAttribute`），据此克隆出一个新的 `CTFontDescriptor` 并重建 `CTFont`，**把配置「烙」进字体对象本身**。CoreText 的级联回退机制随后会在整形时自动用上这条链。这个模块手动管理 `CFRelease`，是练习 FFI 内存规则（Create Rule）的好材料。

#### 4.2.4 代码实践

**实践目标**：通过对照实验体会「防连字微调」与「UTF 下标换算」这两个工程细节的存在意义。

1. 运行 `cargo run -p gpui --example text`，窗口里多段不同对齐/装饰的文本就是整条链路的成品。
2. 打开 [gpui_macos/src/text_system.rs:L559-L571](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_macos/src/text_system.rs#L559-L571)，做一个思想实验并记录：若把 `break_ligature` 逻辑整体删除（始终用同一字号），在「f**i**」跨 run 边界（前半常规后半粗体）时 CoreText 可能产出什么？（预期：连字字形被分配给其中一个 run，另一个 run 的字形下标出现空洞，`closest_index_for_x` 的光标定位在边界处偏移。）
3. 用调试器或日志（在示例代码里加 `println!`）打印一段含 emoji 与中文混排文本的 `LineLayout`：数一数输入 `FontRun` 的个数与输出 `ShapedRun` 的个数是否一致，并解释差异来自哪一层（CoreText 级联回退）。该步骤依赖 macOS 真机与 `font-kit` feature，待本地验证；在 Linux 上可做同款实验对照 4.3 节实现。
4. 预期结果：混排文本的输出 run 数 ≥ 输入 run 数；emoji 字形的 `is_emoji == true` 且来自 emoji 字体的 `FontId`。

#### 4.2.5 小练习与答案

**练习 1**：`MacTextSystemState` 为什么同时保留 `memory_source` 和 `system_source` 两个来源？
答案：`system_source`（font-kit 的 `SystemSource`）负责按名字加载系统安装的字体；`add_fonts` 注入的内存字体进 `MemSource`。`all_font_names` 要把两个来源的族名都报出来（[L130-L132](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_macos/src/text_system.rs#L130-L132)），加载时也需要在内存源里优先命中——Zed 捆绑的 `.ZedSans`/`.ZedMono` 就靠它。

**练习 2**：`StringIndexConverter` 为什么设计成「只能前进」？
答案：字形按视觉顺序返回，其字符串下标在单向文本中单调不减；游标只需向前扫描即可，均摊 O(1)。双向文本重排会打破单调性，代码的处理是检测到回退就整体重建游标（[L606-L609](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_macos/src/text_system.rs#L606-L609)），用低频的 O(n) 重建换取高频路径的简洁。

**练习 3**：macOS 实现的 `recommended_rendering_mode` 恒返回 `Grayscale`（[L210-L216](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_macos/src/text_system.rs#L210-L216)），为什么不用亚像素？
答案：macOS 的 Retina 屏物理像素密度高，亚像素渲染的收益趋近于零，反而会带来彩色镶边；且 macOS 的字体平滑（dilation）机制已在灰度路径上做了观感补偿。这是「渲染策略交给平台各自裁量」的典型体现。

### 4.3 linux::text_system 与 CosmicTextSystem：cosmic-text + swash（Linux 与 Web 共用）

#### 4.3.1 概念说明

Linux 侧的「文本系统模块」只有一行代码：

```rust
pub(crate) use gpui_wgpu::CosmicTextSystem;
```

见 [gpui_linux/src/linux/text_system.rs:L1](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_linux/src/linux/text_system.rs#L1)。Linux 没有系统级唯一字体引擎，GPUI 选择了 [cosmic-text](https://github.com/pop-os/cosmic-text)（System76 出品的 Rust 文本库，内含 fontdb 字体数据库与整形器）配合 [swash](https://github.com/dfrg/swash)（字形度量与光栅化库）。这套实现放在 `gpui_wgpu` crate 里，于是被 Linux 与 Web **共用**——这是四套实现里唯一的跨平台共享：Web 浏览器环境同样没有系统字体 API 可调（wasm 沙箱读不到字体目录），只能把字体文件捆进应用。

两者的差别只在初始化：Linux 用 `CosmicTextSystem::new("IBM Plex Sans")`，cosmic-text 的 `FontSystem::new()` 会扫描系统字体目录；Web 用 `new_without_system_fonts`，传入一个空的 fontdb，再把 `BUNDLED_FONTS` 逐个 `add_fonts` 注入（[gpui_web/src/platform.rs:L135-L145](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_web/src/platform.rs#L135-L145)）。

与 u1-l3 的 feature 结论呼应：LinuxCommon 构造时若 `wayland`/`x11` feature 都没开（零 feature 构建），文本系统直接退化为 `NoopTextSystem`（[gpui_linux/src/linux/platform.rs:L149-L152](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_linux/src/linux/platform.rs#L149-L152)）。

#### 4.3.2 核心流程

`CosmicTextSystem(RwLock<CosmicTextSystemState>)` 的状态核心是 cosmic-text 的 `FontSystem`（内含 fontdb 数据库）加上自己的 `loaded_fonts: Vec<LoadedFont>`——**gpui 的 `FontId` 是 `loaded_fonts` 的下标，与 cosmic 内部的 fontdb id 是两套编号**，状态里用 `font_id_for_cosmic_id` 在整形回调时做登记式换算。

`font_id(Font)` 的解析流程：

1. 以「族名+特性+回退」为 key 查 `font_ids_by_family_cache`；
2. 未命中走 `load_family`：先递归解析用户回退链（防回退再套回退，拼写错误的回退族名直接丢弃），再遍历 fontdb 中该族的所有 face，逐一 `get_font` 加载；
3. **坏字体剔除**：用「字符 m 能否映射到非零字形」做体检，映射不到的 face 直接从数据库移除（Segoe Fluent Icons 这类图标字体因历史原因豁免）；
4. 对幸存 face 用 `find_best_match` 按字重/风格打分，选出唯一 `FontId` 并写缓存。

`layout_line` 的流程带一个 Linux 特有的分叉：先检查文本是否含**段落分隔符**（不只是 `\n`，还包括 `\r`、`\u{1c}`..`\u{1e}`、`\u{85}`、`\u{2029}` 等所有 Unicode `Bidi_Class=B` 字符）。有的话按分隔符把行切成小段分别整形再拼接（cosmic 的整形器按 bidi 段落工作，混方向文本一次喂进去会崩）；没有则走无分隔符快速路径。`rasterize_glyph` 则完全交给 swash：普通字形输出 Alpha 掩码，emoji 输出彩色位图（swash 给的是 RGBA，逐像素换通道成 BGRA），请求亚像素时把单通道掩码扩成 RGBA。

#### 4.3.3 源码精读

**结构体与构造器**。[cosmic_text_system.rs:L24-L31](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_wgpu/src/cosmic_text_system.rs#L24-L31) 定义外壳与 `FontKey`；[L43-L62](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_wgpu/src/cosmic_text_system.rs#L43-L62) 是状态：`font_system`（cosmic）、`scratch`（整形缓冲复用）、`swash_scale_context`（swash 缩放器复用——这两个复用字段是性能关键，避免每字形重建上下文）、`loaded_fonts` 与两张缓存表。两个构造器见 [L64-L92](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_wgpu/src/cosmic_text_system.rs#L64-L92)。`impl PlatformTextSystem` 薄壳从 [L95](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_wgpu/src/cosmic_text_system.rs#L95) 开始。

**all_font_names**：直接问 fontdb 要所有 face 的第一个族名，排序去重（[L100-L112](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_wgpu/src/cosmic_text_system.rs#L100-L112)）——对比 macOS 实现的 FFI 绕坑，这里十行搞定，是纯 Rust 栈的优势缩影。

**font_metrics 的符号约定**：swash 的 descent 是正数，gpui 约定是负数，所以第 [L147](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_wgpu/src/cosmic_text_system.rs#L147) 行写 `descent: -metrics.descent`（完整实现 [L135-L158](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_wgpu/src/cosmic_text_system.rs#L135-L158)）。macOS 的 font-kit 恰好同 gpui 约定（负值）无需翻转——**「descent 符号」是每个新实现都必须小心的契约暗礁**。

**load_family 与坏字体剔除**。[L220-L305](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_wgpu/src/cosmic_text_system.rs#L220-L305)。回退链解析的注释值得读（L227-L229：回退族不再允许拉进另一条链；回退族名拼错就静默丢弃，保证主族仍能加载）。剔除逻辑在 [L287-L292](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_wgpu/src/cosmic_text_system.rs#L287-L292)：

```rust
if font.as_swash().charmap().map('m') == 0
    && !allowed_bad_font_names.contains(&postscript_name.as_str())
{
    self.font_system.db_mut().remove_face(font.id());   // 连 'm' 都画不出的字体不要
    continue;
};
```

**layout_line 的分派与拼接**。[L449-L455](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_wgpu/src/cosmic_text_system.rs#L449-L455) 按是否含段落分隔符分派；[shape_segment:L503-L542](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_wgpu/src/cosmic_text_system.rs#L503-L542) 负责把子段的整形结果**平移拼接**回整行：字形的 `index` 加上段起点、`position.x` 加上段宽度，尾部同字体的 run 还会合并（[L527-L537](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_wgpu/src/cosmic_text_system.rs#L527-L537)）。

**光栅化**。[rasterize_glyph:L332-L362](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_wgpu/src/cosmic_text_system.rs#L332-L362) 按 swash 的 `Content` 三分类：`Color`/`SubpixelMask` 换通道成 BGRA 直接过；`Mask` 在请求亚像素时单通道扩四通道。真正调 swash 的是 [render_glyph_image:L364-L408](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_wgpu/src/cosmic_text_system.rs#L364-L408)：按 `subpixel_variant / SUBPIXEL_VARIANTS_X / scale_factor` 算亚像素偏移，emoji 用「彩色轮廓 → 彩色位图 → 普通轮廓」三级来源，普通字形用「位图 → 轮廓」两级，注释还记录了 swash 的 B/R 通道互换 bug 及规避（[L396-L399](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_wgpu/src/cosmic_text_system.rs#L396-L399)）。

**渲染策略**：恒返回 `Subpixel`（[L197-L203](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_wgpu/src/cosmic_text_system.rs#L197-L203)）——Linux 桌面以普通 DPI 显示器为主，ClearType 式亚像素仍是清晰度最优解，与 macOS 的 Grayscale 形成直接的策略对照。

**测试**。文件尾部 `#[cfg(test)] mod tests`（[L996-L998](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_wgpu/src/cosmic_text_system.rs#L996-L998)）用 `include_bytes!` 内嵌 IBM Plex 字体（[L1019-L1020](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_wgpu/src/cosmic_text_system.rs#L1019-L1020)）构造无系统字体的测试实例，覆盖混方向段落、分隔符在行首/行尾等边界。这些测试不依赖窗口系统，任何平台都能跑。

#### 4.3.4 代码实践

**实践目标**：跑通 cosmic 实现自带的单元测试，验证「段落分隔符拆分整形」行为。

1. 在仓库根目录运行：

   ```bash
   cargo test -p gpui_wgpu cosmic_text_system
   ```

2. 打开测试 [shape_text_with_mixed_direction_paragraphs:L1045-L1064](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_wgpu/src/cosmic_text_system.rs#L1045-L1064)：文本 `"first line\n\u{05d0}\u{001c}A"` 里藏了一个 `\u{001c}`（文件分隔符，Unicode 双向段落边界）。测试断言这行文本被切成 2 行、第二行宽度非零——它复现的是一次真实崩溃（注释：混方向文本经 `shape_text` 只按 `\n` 断行直达整形器导致 panic）。
3. 再读 [layout_line_with_mixed_direction_paragraphs:L1066-L1087](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_wgpu/src/cosmic_text_system.rs#L1066-L1087)，它遍历全部 7 个 `Bidi_Class=B` 分隔符（清单在 [L1024-L1026](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_wgpu/src/cosmic_text_system.rs#L1024-L1026)）做排列验证。
4. 需要观察的现象：测试全绿；若故意把 `layout_line` 的分派条件从 `contains_paragraph_separator(text)` 改成 `text.contains('\n')`，哪些测试会红？（预期：使用 `\u{001c}` 等非 `\n` 分隔符的用例失败。）改完记得还原。
5. 预期结果：约十余个测试通过；具体数量以当前 HEAD 为准，待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 Linux 和 Web 能共用 `CosmicTextSystem`，而 macOS/Windows 不能共用它？
答案：共用与否取决于「能否访问系统字体服务」。Linux 的 fontdb 自己扫目录，浏览器里没有目录可扫，两者都退化为「自带字体数据库」的模式，cosmic 恰好两种都支持（`new` / `new_with_locale_and_db`）。macOS/Windows 则有系统级引擎（CoreText/DirectWrite），观感、回退、ClearType 都与 OS 深度绑定，自带的 Rust 栈既拿不到系统级联回退也模拟不了系统渲染策略。

**练习 2**：`CosmicTextSystemState.loaded_fonts` 与 cosmic 的 fontdb id 为什么不统一成一套编号？
答案：gpui 的 `FontId` 必须是紧凑下标（供 `loaded_fonts[font_id.0]` 直接索引，也充当缓存 key）；而 fontdb 的 id 在 `remove_face`（坏字体剔除）后并不回收紧凑性。运行期整形还可能引入「cosmic 自己选的回退字体」，这时才用 [font_id_for_cosmic_id:L418-L443](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_wgpu/src/cosmic_text_system.rs#L418-L443) 惰性登记一个新 `FontId`。两套编号 + 登记换算，兼顾了索引效率与动态回退。

**练习 3**：`scratch: ShapeBuffer` 和 `swash_scale_context: ScaleContext` 为什么放在状态里而不是函数局部？
答案：两者都是可复用的工作缓冲/上下文对象，构造有开销；每字形重建会让光栅化热路径变慢。放进状态跨调用复用是字体系统的常见优化，代价是这些方法必须拿 `&mut self`（这就是 `rasterize_glyph` 在契约实现里拿写锁的原因，[L181-L191](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_wgpu/src/cosmic_text_system.rs#L181-L191)）。

### 4.4 direct_write：Windows 的 DirectWrite 整形与 D3D11 光栅化

#### 4.4.1 概念说明

Windows 实现与前两套的最大区别：**文本系统里嵌着一块 GPU 状态**。`DirectWriteTextSystem` 的状态含 `GPUState`（D3D11 设备上下文、混合状态、一对「emoji 光栅化」着色器），因为 Windows 上彩色 emoji（Segoe UI Emoji 的 COLR/CPAL 表）DirectWrite 不直接给出位图，需要 GPUI 自己跑一遍 D3D11 管线把矢量 emoji 轮廓渲染成纹理（u6-l2 讲过这段渲染栈，`handle_gpu_lost` 还把它挂进了 GPU 设备丢失重建链路）。这体现了第四种集成姿态：**字体栈与渲染栈深度耦合，平台实现的边界不等于库的边界**。

装配同样受 headless 影响：`WindowsPlatform::new` 在非 headless 时创建 `DirectXDevices` 和 `DirectWriteTextSystem`，headless 时直接用 `NoopTextSystem`（[gpui_windows/src/platform.rs:L114-L131](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_windows/src/platform.rs#L114-L131)）。

#### 4.4.2 核心流程

`layout_line` 的 DirectWrite 版本是一次「属性设置 + 回调收集」的往返：

1. 文本编码成 UTF-16（DirectWrite 全程 UTF-16，与 CoreText 相同）；
2. 用首个 run 的字体建 `IDWriteTextFormat`，再 `CreateTextLayout` 得到布局对象；用户回退链经 `format.SetFontFallback` 注入（等价于 macOS 的 cascade list）；
3. 对后续每个 run 调一串 `SetFontCollection/SetFontFamilyName/SetFontSize/SetFontStyle/SetFontWeight/SetTypography`，把每个区间的字体与 OpenType 特性（`SetTypography`）设到位——同样使用 `next_up()` 字号微调打断跨 run 连字；
4. 调 `text_layout.Draw(...)` 让 DirectWrite 回调 GPUI 的 `TextRenderer`，在回调里逐字形收集 id、位置、 advances，经 UTF-16→UTF-8 转换装进 `ShapedRun`；行 ascent/descent 取自 `GetLineMetrics`。

光栅化分单色与彩色两条路：单色用 `IDWriteGlyphRunAnalysis::CreateAlphaTexture` 直接生成 1x1 纹理格式的掩码（亚像素时取 3x1 纹理再逐通道展开）；彩色 emoji 走 D3D11 光栅化，失败则回退单色路径并填黑。渲染策略上，它读取系统 ClearType 设置动态决定 Subpixel/Grayscale。

#### 4.4.3 源码精读

**三层结构**。[DirectWriteTextSystem:L38-L41](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_windows/src/direct_write.rs#L38-L41) = `components`（不可变的工厂/加载器/区域设置等，[L43-L51](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_windows/src/direct_write.rs#L43-L51)，Drop 时反注册字体加载器）+ `state: RwLock<DirectWriteState>`（[L72-L80](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_windows/src/direct_write.rs#L72-L80)，含 `GPUState`、系统/自定义两个字体集合、`fonts: Vec<FontInfo>` 与 `layout_line_scratch` 复用缓冲）。`GPUState::new` 里创建的顶点/像素着色器模块名就叫 `EmojiRasterization`（[L134-L152](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_windows/src/direct_write.rs#L134-L152)）。

**契约实现的容错差异**。[impl PlatformTextSystem:L226-L299](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_windows/src/direct_write.rs#L226-L299) 大部分方法与前两套同构，但 `layout_line` 多了一层兜底：`.log_err().unwrap_or(LineLayout { font_size, ..Default::default() })`（[L279-L288](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_windows/src/direct_write.rs#L279-L288)）——DirectWrite 偶发返回错误（如某些字形触发分析器 bug），这里选择记录日志并返回空布局，不让一行文本崩掉整个窗口。注意契约的 `layout_line` 签名本身不返回 `Result`：macOS 与 cosmic 的状态方法同样直接返回 `LineLayout`，错误只会在更深的内部路径（如 macOS 的 `CTFont` downcast unwrap，[gpui_macos/src/text_system.rs:L581-L585](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_macos/src/text_system.rs#L581-L585)）以 panic 收场——只有 Windows 实现显式把「引擎可能失败」翻译成了降级而非崩溃。

**layout_line 主体**。[L514-L623](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_windows/src/direct_write.rs#L514-L623)。看三个片段：首个 run 建 format 与 `SetFontFallback`（[L534-L552](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_windows/src/direct_write.rs#L534-L552)）；后续 run 的属性设置与 `next_up()` 防连字（[L583-L607](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_windows/src/direct_write.rs#L583-L607)，与 macOS 同款技巧，印证这是跨引擎的通用 workaround）；最后 `text_layout.Draw` 以裸指针把 `RendererContext` 递给 COM 回调（[L610-L623](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_windows/src/direct_write.rs#L610-L623)）——u4-l4 讲过的 trampoline 模式再次出现。

**光栅化**。[rasterize_glyph:L786-L811](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_windows/src/direct_write.rs#L786-L811)：emoji 先试彩色光栅化、失败回退单色并把每像素 1 字节扩成 `[0,0,0,alpha]` 的 BGRA；[rasterize_monochrome:L813-L837](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_windows/src/direct_write.rs#L813-L837) 用 `DWRITE_TEXTURE_ALIASED_1x1` 生成掩码。输出尺寸一律沿用调用方传入的 `glyph_bounds.size`，三套实现在这点上约定一致。

**渲染策略随系统设置**。[recommended_rendering_mode:L290-L299](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_windows/src/direct_write.rs#L290-L299) 读构造时缓存的 `system_subpixel_rendering`（来自系统 ClearType 配置）动态返回 Subpixel 或 Grayscale——介于 macOS 的「恒灰度」与 cosmic 的「恒亚像素」之间：尊重用户系统设置。

**四方能力对照**（综合前三节的结论）：

| 维度 | macOS（CoreText） | Linux/Web（cosmic-text） | Windows（DirectWrite） | Noop |
| --- | --- | --- | --- | --- |
| 字体数据库 | font-kit SystemSource + MemSource | cosmic-text fontdb | IDWriteFontCollection | 无 |
| 整形引擎 | CTLine | cosmic-text ShapeLine | IDWriteTextLayout | 等宽占位 |
| 光栅化 | CGContext | swash | DirectWrite + D3D11（emoji） | 空 |
| `recommended_rendering_mode` | 恒 Grayscale | 恒 Subpixel | 随系统 ClearType 设置 | Grayscale |
| `glyph_dilation_for_color` | 覆盖（0..=4 按亮度） | 默认 0 | 默认 0 | 默认 0 |
| 用户回退链注入 | open_type 模块烙进 CTFont | load_family 解析成链 | SetFontFallback | 不支持 |
| 防跨 run 连字 | 字号 `next_up()` 交替 | 无此技巧（整形器按 run 边界工作） | 字号 `next_up()` 交替 | 不需要 |
| layout_line 出错路径 | 签名无 Result，内部 unwrap | 签名无 Result，状态方法内部处理 | log_err + 返回空布局 | 不会失败 |

#### 4.4.4 代码实践

**实践目标**：把「渲染策略」做成一次跨源码的调研，产出可复用的对照笔记。

1. 在三个文件里分别定位 `recommended_rendering_mode` 的实现：[gpui_macos/src/text_system.rs:L210-L216](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_macos/src/text_system.rs#L210-L216)、[gpui_wgpu/src/cosmic_text_system.rs:L197-L203](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_wgpu/src/cosmic_text_system.rs#L197-L203)、[gpui_windows/src/direct_write.rs:L290-L299](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_windows/src/direct_write.rs#L290-L299)。
2. 回答三个问题并写成笔记：(a) 调用方能通过什么类型拿到这个建议（提示：`TextRenderingMode` 枚举的三档语义，[gpui/src/platform.rs:L2125-L2135](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/platform.rs#L2125-L2135)）？(b) 若 Windows 用户在系统设置里关闭 ClearType，`TextRenderingMode` 与 `RenderGlyphParams.subpixel_rendering` 各自会发生什么？(c) 为什么 cosmic 实现敢于无条件下返回 Subpixel？
3. 进阶（需要 Windows 机器）：运行 `cargo run -p gpui --example text`，在系统设置中切换 ClearType 开关后重跑，观察文字边缘差异；无法切换则记录为待本地验证。
4. 预期结果：形成一张「策略来源 → 实现位置 → 运行期可变性」三行小表；macOS 编译期固定、Windows 构造期缓存一次、cosmic 编译期固定。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `DirectWriteComponents` 要实现 `Drop` 反注册字体加载器？
答案：`in_memory_loader`（IDWriteInMemoryFontFileLoader）是向 DirectWrite 工厂全局注册的 COM 单例式资源；不反注册会泄漏注册项，且进程内重复创建文本系统（如测试里多次初始化）会累积。见 [L53-L61](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_windows/src/direct_write.rs#L53-L61)。

**练习 2**：headless 的 Windows 平台为什么连 DirectWrite 都不建，而 macOS headless 仍可建 MacTextSystem？
答案：`DirectWriteTextSystem::new` 需要传 `&DirectXDevices`（GPU 设备），headless 模式下没有 D3D11 设备，彩色 emoji 光栅化无从谈起，于是整体退化为 Noop（[gpui_windows/src/platform.rs:L114-L131](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_windows/src/platform.rs#L114-L131)）；而 MacTextSystem 只依赖 CPU 侧的 CoreText/font-kit，与窗口渲染无关（[gpui_macos/src/platform.rs:L200-L211](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_macos/src/platform.rs#L200-L211)）。headless 下「布局还能不能算」是两套架构的真实差异。

**练习 3**：`layout_line_scratch: Vec<u16>` 复用缓冲的意义是什么？它带来什么约束？
答案：避免每次整形重新分配 UTF-16 文本缓冲。约束是这块缓冲被 `&mut self` 独占持有，因此 Windows 的 `layout_line` 拿的是写锁——与 cosmic 实现因 `scratch`/`swash_scale_context` 拿写锁同理（[gpui_windows/src/direct_write.rs:L528-L530](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_windows/src/direct_write.rs#L528-L530)）。这是「文本系统虽是 Send+Sync，但整形热路径实际串行化」的根源。

## 5. 综合实践

**任务：制作一个跨操作系统的「字体度量勘测工具」，并对比两个平台的结果。**

这个任务把本讲四个模块串起来：用契约的消费端（4.1 的 `TextSystem` 缓存层）拿数据，理解数据来自哪套平台引擎（4.2/4.3/4.4），并亲手验证「同一字体在不同平台栈上度量不一致」这一事实。

1. **编写工具**（示例代码，可放在独立 crate，依赖 `gpui` 与 `gpui_platform`）：

   ```rust
   use gpui::{App, font, px};
   use gpui_platform::application;
   use std::fmt::Write as _;

   fn main() {
       application().run(|cx: &mut App| {
           let ts = cx.text_system();
           let mut report = String::from("family,id,units_per_em,ascent_px,descent_px,line_height_px\n");
           for family in ["Helvetica", "Arial", "DejaVu Sans", ".SystemUIFont"] {
               let id = ts.resolve_font(&font(family));
               let ascent = ts.ascent(id, px(16.));
               let descent = ts.descent(id, px(16.));
               let line_height = ascent - descent;   // 近似单倍行高（未含 line_gap）
               writeln!(report, "{family},{id:?},{},{:.2},{:.2},{:.2}",
                   ts.units_per_em(id), ascent.0, descent.0, line_height.0).unwrap();
           }
           println!("{report}");
           println!("平台字体族总数: {}", ts.all_font_names().len());
           cx.quit();
       });
   }
   ```

2. **在本机运行**：macOS 上四行数据来自 font-kit/CoreText；Linux 上来自 swash 度量；Windows 上来自 DirectWrite（记得非 headless 才有真实数据）。
3. **在第二个操作系统上运行**（双系统、虚拟机或同事机器均可），把两份 CSV 并排对比。重点观察三件事：
   - `.SystemUIFont` 解析到哪个族、度量是多少（macOS 应是 SF 系，Windows 是 Segoe UI，Linux 常落到回退栈）；
   - 同名族（如 Arial）的 `units_per_em` 是否一致——不同平台可能加载了不同版本的字体文件；
   - `descent` 的符号是否为负（契约约定，见 4.3.3 的符号暗礁）。
4. **回答开放问题**：`line_height` 为什么不含 `line_gap`？读 [TextSystem 的公开方法清单:L254-L290](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/text_system.rs#L254-L290)，你会发现缓存层没有暴露 `line_gap`（完整 `FontMetrics` 只经平台 trait 的 `font_metrics` 方法给出）——这是「应用层 API 面窄于平台契约」的实例。若确实需要精确行距，可以走 `layout_line` 的 `ascent`/`descent` 字段或给 gpui 提 PR。
5. **预期结果**：两份 CSV 中至少 `.SystemUIFont` 一行的三个度量值显著不同；两平台字体族总数相差数百很正常（取决于安装的字体）。具体数值待本地验证。

## 6. 本讲小结

- `PlatformTextSystem` 是 12 个方法的平台无关字体引擎契约（字体管理 / 度量查询 / 整形光栅化 / 渲染策略），以 `Arc<dyn ...>` 共享且 `Send + Sync`，因此四套实现内部都以 `RwLock` 包状态、整形与光栅化走写锁。
- 渲染管线的接力棒是：`Font` → `font_id` → `FontRun` → `layout_line` → `LineLayout{ShapedRun, ShapedGlyph}` → `RenderGlyphParams`（可哈希，直接充当图集 key）→ `rasterize_glyph` → 位图进 `PlatformAtlas`（下一讲）。
- gpui 主 crate 的 `TextSystem` 是包在平台实现外的缓存层（font_id / metrics / raster_bounds 三级缓存 + 回退字体栈），应用代码拿到的都是它；`resolve_font` 的降级链保证坏配置也能渲染出文字。
- 四套实现各取所长：macOS 用 font-kit 查询 + CoreText 整形 + CGContext 光栅化（外加 open_type 模块注入特性与回退、glyph dilation 复刻系统观感）；Linux 与 Web 共用纯 Rust 的 cosmic-text + swash（`CosmicTextSystem`，Web 用空 fontdb + 捆绑字体）；Windows 用 DirectWrite 整形 + D3D11 光栅化彩色 emoji（文本系统内嵌 GPU 状态）；`NoopTextSystem` 是零 feature / headless 下的自洽假实现。
- 两组工程细节在 macOS 与 Windows 复现（字号 `next_up()` 打断跨 run 连字、UTF-16↔UTF-8 下标换算游标——两者的引擎 API 都以 UTF-16 计数）；descent 符号约定（gpui 为负）则是每套实现都必须遵守的契约暗礁；而 `recommended_rendering_mode` 与「layout_line 出错怎么办」体现各平台的策略分歧。
- 大纲中「Linux font-kit」的旧印象已按当前源码纠正：font-kit 只存在于 macOS 路径，Linux 文本系统是对 `gpui_wgpu::CosmicTextSystem` 的一行再导出。

## 7. 下一步学习建议

1. **u8-l2（PlatformAtlas 与渲染后端）**：本讲反复出现的 `rasterize_glyph` 位图与 `AtlasKey::Glyph`（[gpui/src/platform.rs:L1271-L1303](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/platform.rs#L1271-L1303)）正是图集的输入，下一讲看它们如何被装箱、上传、绘制。
2. **回顾 u6-l2（Windows DirectX 渲染栈）**：本讲 4.4 的 `GPUState` 与 `handle_gpu_lost`（[gpui_windows/src/platform.rs:L1415-L1437](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_windows/src/platform.rs#L1415-L1437)）在那一讲有完整的上下游。
3. **延伸阅读缓存与换行**：`gpui/src/text_system.rs` 的 `WindowTextSystem`（[L364 起](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/text_system.rs#L364)）与 `line_layout.rs` 的 `LineWrapper`、`shape_line_by_hash` 等按哈希缓存的布局接口——它们解释了编辑器滚动时为什么不重复整形。
4. **动手方向**：若你想加深理解，可以尝试给 `CosmicTextSystem` 的 `layout_line_no_separators` 快速路径补一个测试用例（现有测试见 [cosmic_text_system.rs:L1164 起](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_wgpu/src/cosmic_text_system.rs#L1164)），练习「读契约写断言」的能力。

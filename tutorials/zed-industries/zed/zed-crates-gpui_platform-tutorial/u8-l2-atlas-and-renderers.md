# u8-l2 PlatformAtlas 与渲染后端：字形与图集管理

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清「sprite atlas（精灵图集）」在 GPU UI 渲染中解决什么问题：为什么要把成千上万个字形/图片小块装进少数几张纹理，从而把每帧的纹理绑定切换次数从与图元数同阶降到与纹理数同阶。
2. 逐字段解释 `AtlasKey`、`AtlasTile`、`AtlasTextureId`、`AtlasTextureKind` 这组数据模型，并描述 `RenderGlyphParams` / `RenderSvgParams` / `RenderImageParams` 三类绘制参数如何经 `get_or_insert_with` 进入图集。
3. 精读 Windows 实现 `DirectXAtlas`：etagere 装箱、三类纹理的像素格式、`UpdateSubresource` 上传、free_list 空槽复用与设备丢失重建。
4. 画出 Windows 渲染栈「设备（全局一份）→ 渲染器（每窗口一份）→ 图集（每渲染器一份）」的所有权链，并说明渲染器如何在批绘制时按 `AtlasTextureId` 从图集取回纹理视图。
5. 对照 macOS 的 `MetalAtlas`，理解同一 `PlatformAtlas` 契约在不同 GPU API 上的落地差异。

## 2. 前置知识

本讲默认你已读过 u3-l2（`PlatformWindow` trait）与 u8-l1（`PlatformTextSystem` 与 `RenderGlyphParams`），并了解以下概念：

- **纹理（texture）与采样**：GPU 上的一块位图。着色器通过「纹理视图 / ShaderResourceView」读取它。每绑定一张纹理再发起绘制，称为一次「纹理切换」。
- **精灵（sprite）**：UI 中一张自带位图的小图块——一个光栅化后的字形、一个图标、一张图片，都是精灵。
- **图集（atlas）**：把许多小位图拼进一张（或几张）大纹理的容器。好处是绘制连续使用同一张纹理的精灵时不必反复切换纹理，批次（batch）可以很大；代价是需要一个「装箱器」管理每个小块在大纹理里的位置。
- **装箱算法**：决定「把多大多小的矩形放进多大的箱子、放哪里」。Zed 使用 `etagere` crate 的 `BucketedAtlasAllocator`（分桶搁架式分配器），这与浏览器渲染引擎常用的方案同源。
- **亚像素渲染（subpixel rendering）**：LCD 屏幕上 RGB 物理亚像素独立发光来提升文字锐度，字形掩码因此是每通道独立的，需要与灰度掩码不同的纹理格式和混合状态（u6-l2 讲过 Windows 的 dual-source blending）。
- **承接点**：u8-l1 讲过 `layout_line` 产出 `LineLayout`（含 `ShapedGlyph`），而 `RenderGlyphParams` 是把某个字形「某一次具体渲染配置」哈希成缓存键的结构。本讲就从这个键出发，看它如何落进图集。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `../gpui/src/platform.rs` | 契约层：`PlatformAtlas` trait、`AtlasKey`/`AtlasTile`/`AtlasTextureId`/`AtlasTextureKind`/`AtlasTextureList` 数据模型（L1271–L1430） |
| `../gpui/src/window.rs` | 消费侧：`paint_glyph`/`paint_emoji`/`paint_svg`/`paint_image` 四条「参数 → 图集瓦片 → 精灵图元」链路 |
| `../gpui/src/text_system.rs`、`../gpui/src/svg_renderer.rs`、`../gpui/src/assets.rs` | 三类键参数 `RenderGlyphParams`/`RenderSvgParams`/`RenderImageParams` 的定义 |
| `../gpui/src/scene.rs` | 精灵图元结构（内嵌 `AtlasTile`）与按 `texture_id` 合批的逻辑 |
| `../gpui_windows/src/directx_atlas.rs` | Windows 图集实现（本讲主角之一） |
| `../gpui_windows/src/directx_renderer.rs` | Windows 渲染器：批分发、从图集取纹理视图绘制 |
| `../gpui_windows/src/directx_devices.rs`、`../gpui_windows/src/platform.rs`、`../gpui_windows/src/window.rs` | 所有权链：全局设备 → 每窗口渲染器 → 每渲染器图集 |
| `../gpui_apple/src/metal_atlas.rs`、`../gpui_apple/src/metal_renderer.rs` | macOS/Metal 对照实现 |

说明：macOS 的 Metal 渲染器位于 `gpui_apple` crate（Apple 平台共享），`gpui_macos` 经由它获得 `MetalAtlas` 与 `MetalRenderer`。

## 4. 核心概念与源码讲解

### 4.1 PlatformAtlas 契约：图集键、瓦片与三类纹理

#### 4.1.1 概念说明

`PlatformAtlas` 是 gpui 对「纹理图集」的平台无关抽象。它本质是一个**以绘制参数为键、以瓦片位置为值的缓存**：

- 键（`AtlasKey`）：完整描述「要渲染什么、以什么配置渲染」。同一个字形的同一渲染配置永远命中同一个瓦片，因此光栅化（CPU 上昂贵的步骤）每个键只做一次。
- 值（`AtlasTile`）：这块位图放在哪张图集纹理（`texture_id`）的哪个矩形区域（`bounds`），以及它在装箱器里的分配句柄（`tile_id`）。

为什么必须分三类纹理（`AtlasTextureKind`）？因为 GPU 纹理的**像素格式**必须与内容匹配：灰度字形掩码每像素 1 字节就够；彩色 emoji 和图片需要 BGRA 每像素 4 字节；LCD 亚像素掩码需要每通道独立的 4 字节格式。混装进一张纹理会浪费显存或无法正确采样，所以图集按「单色 / 多色 / 亚像素」分成三组纹理列表。

#### 4.1.2 核心流程

`PlatformAtlas` 只有两种核心操作：

```text
get_or_insert_with(key, build):
    若 key 已有瓦片        → 直接返回缓存的 AtlasTile（零光栅化成本）
    否则                    → 调 build() 产出 (尺寸, 像素字节)
                             → 在对应类别的纹理列表里装箱分配一个矩形
                             → 把字节上传到该矩形
                             → 记录 key → tile 映射并返回

remove(key):
    删除 key 映射 → 装箱器解除分配 → 纹理引用计数减一
    → 若整张纹理已无任何存活键 → 它的槽位进入 free_list 供复用
```

直觉上，图集把每帧的纹理切换次数从与精灵数 \( n \) 同阶降到与纹理数 \( t \) 同阶，而 \( t \ll n \)（一次编辑器重绘可能有上万个字形精灵，但活跃纹理通常只有几张）。

#### 4.1.3 源码精读

先看契约本身。整个 trait 只有两个必需方法加一个测试期默认方法：

[../gpui/src/platform.rs:L1324-L1336](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui/src/platform.rs#L1324-L1336)：定义 `PlatformAtlas` trait。`get_or_insert_with` 接收键与一个惰性构建闭包（返回 `Option` 是为了「构建方暂时产出不了位图」的场景，例如 SVG 资源尚未加载）；`contains` 只在 `test`/`test-support` 下存在，供测试断言缓存状态。

键是三选一的枚举：

[../gpui/src/platform.rs:L1271-L1277](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui/src/platform.rs#L1271-L1277)：`AtlasKey` 有 `Glyph`/`Svg`/`Image` 三个变体，分别包着 `RenderGlyphParams`、`RenderSvgParams`、`RenderImageParams`。`PartialEq + Eq + Hash + Clone` 使它可以直接充当哈希表键。

[../gpui/src/platform.rs:L1287-L1302](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui/src/platform.rs#L1287-L1302)：`texture_kind()` 决定键落进哪类纹理——emoji 字形与图片进 `Polychrome`（彩色），开启了亚像素渲染的普通字形进 `Subpixel`，其余普通字形与单色 SVG 进 `Monochrome`。这是「键自带分类」的关键一步。

瓦片与纹理身份：

[../gpui/src/platform.rs:L1374-L1386](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui/src/platform.rs#L1374-L1386)：`AtlasTile` 是图集交回给调用方的「取件凭证」：`texture_id`（哪张纹理）、`tile_id`（装箱器内的分配句柄）、`bounds`（纹理内的设备像素矩形）。注意 `#[repr(C)]`——它会按值嵌进 GPU 实例缓冲里的精灵结构。

[../gpui/src/platform.rs:L1388-L1397](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui/src/platform.rs#L1388-L1397)：`AtlasTextureId` 用 `index`（纹理在所属类别列表中的下标）加 `kind` 定位一张纹理。注释点明 `index` 用 `u32` 是为了 Metal 着色器语言兼容。

[../gpui/src/platform.rs:L1399-L1413](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui/src/platform.rs#L1399-L1413)：`AtlasTextureKind` 三值枚举 `Monochrome = 0` / `Polychrome = 1` / `Subpixel = 2`，判别值会进着色器。

[../gpui/src/platform.rs:L1415-L1430](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui/src/platform.rs#L1415-L1430)：`TileId` 是对 `etagere::AllocId` 的直接包装（`serialize`/`deserialize` 互转），即「装箱器分配句柄」的类型化外壳。

契约层还提供了一段通用的「纹理列表」容器，供各平台实现直接复用：

[../gpui/src/platform.rs:L1338-L1372](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui/src/platform.rs#L1338-L1372)：`AtlasTextureList<T>` 是「槽位数组 + free_list」结构：`textures` 里允许有 `None` 空槽，`free_list` 记录可复用的槽位下标。整张纹理的全部键被移除后，其槽位进 free_list，新纹理可顶替旧下标——`AtlasTextureId::index` 因此保持稳定语义。

最后，图集在整体架构中的挂载点：它不挂在 `Platform` 上，而是挂在**窗口**上——每个渲染器（也就每个窗口）一份：

[../gpui/src/platform.rs:L863-L865](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui/src/platform.rs#L863-L865)：`PlatformWindow` 的 `draw`（把一帧 `Scene` 画上去）、`schedule_frame`（u3-l2 讲过的按需帧调度）与 `sprite_atlas()`（返回 `Arc<dyn PlatformAtlas>`）排在一起。`Arc` 说明 gpui 的 `Window` 会长期持有一份共享句柄。

#### 4.1.4 代码实践

**实践目标**：亲手验证「键 → 纹理类别」的全部决策路径，并确认契约的全部实现方。

**操作步骤**：

1. 打开 `../gpui/src/platform.rs` 的 `texture_kind()`（L1287 起），把三个变体 × 各字段的组合写成一张决策表，形如：

   | 键变体 | 字段条件 | 纹理类别 | Windows 像素格式（见 4.3） |
   | --- | --- | --- | --- |
   | Glyph | `is_emoji == true` | Polychrome | BGRA8（4 字节/像素） |
   | Glyph | `subpixel_rendering == true` | Subpixel | RGBA8（4 字节/像素） |
   | Glyph | 其余 | Monochrome | R8（1 字节/像素） |
   | Svg | 任意 | Monochrome | R8 |
   | Image | 任意 | Polychrome | BGRA8 |

2. 在编辑器里对 `trait PlatformAtlas` 使用 rust-analyzer 的「Find Implementations」（或 `grep -rn "impl PlatformAtlas for" crates/`），列出全部实现方。预期至少包括：`DirectXAtlas`（gpui_windows）、`MetalAtlas`（gpui_apple）、Linux/blade 与 Web/wgpu 侧的图集，以及 test-support 的 `TestAtlas`（`../gpui/src/platform/test/window.rs:477` 附近实现 `get_or_insert_with`）。

**需要观察的现象**：决策表覆盖了 `AtlasKey` 的所有分支且无重叠；实现方列表里既有真 GPU 后端也有测试替身。

**预期结果**：得到一张 5 行决策表与一份 4–6 个实现方的清单。若某平台实现 grep 不到，检查该 crate 的 feature 门控（u1-l3 讲过 wayland/x11 feature 会影响哪些模块参与编译）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `get_or_insert_with` 的 `build` 闭包返回 `Option`，而不是直接返回 `(Size, bytes)`？
**答案**：因为有些内容在当下可能根本产不出位图——典型场景是 `paint_svg` 里 SVG 资源还没加载完（见 4.2 的 `render_alpha_mask` 返回 `Option`）。返回 `Ok(None)` 让图集「不缓存任何东西」，等下次 paint 再试；如果强行返回空位图，则会把「暂缺」错误地缓存成永久结果。

**练习 2**：`AtlasTextureId` 里的 `index` 为什么必须在整张纹理被清空后仍然可以被新纹理复用？这依赖什么前提？
**答案**：因为纹理槽位复用能让 `AtlasTextureList` 不无限增长。前提是：旧纹理被移出时，`tiles_by_key` 里已经没有任何指向它的键（`remove` 是先删键、再减引用计数、引用归零才进 free_list），所以旧 `index` 上不存在悬空的 `AtlasTile`——所有还活着的瓦片凭证都指向当前占用该槽位的纹理。

**练习 3**：`contains` 方法为什么用 `#[cfg(any(test, feature = "test-support"))]` 门控？
**答案**：它只服务于测试断言（「这个键此刻是否在缓存里」），生产渲染路径从不查询——图集的正确性靠 `get_or_insert_with` 的语义保证。门控让它不占用发布构建的 trait 表面积，这与 u2-l1 讲过的「平台能力差异由实现层吸收、不泄漏进契约」是同一思路。

### 4.2 从 layout 到瓦片：paint_glyph 的取件路径

#### 4.2.1 概念说明

u8-l1 讲到 `layout_line` 产出 `LineLayout` 为止；本模块补上后半程：**paint 阶段**如何把整形结果里的每个字形变成「图集瓦片 + 精灵图元」。核心有三条链路（外加两条同类）：

- `paint_glyph`：普通字形（灰度或亚像素掩码）；
- `paint_emoji`：emoji 字形（彩色位图）；
- `paint_svg` / `paint_image`：图标与图片。

它们共同的模式是「**先查缓存，未命中才光栅化**」：构造键 → 查 `raster_bounds`（零尺寸则跳过）→ `get_or_insert_with`（内部只在未命中时调 `rasterize_glyph`）→ 拿 `AtlasTile` 组装精灵图元插入 `Scene`。

#### 4.2.2 核心流程

以 `paint_glyph` 为例（逻辑顺序）：

```text
1. 计算 scale_factor 后的设备像素原点
2. 把原点量化到 1/4 设备像素网格 → subpixel_variant ∈ [0,4)×[0,4)
   （同一字形落在不同亚像素相位上会得到不同掩码 → 不同键 → 不同瓦片）
3. 判定 subpixel_rendering（背景不透明 + 平台支持 + 推荐模式）
4. 组装 RenderGlyphParams（font_id/glyph_id/size/相位/scale/emoji/亚像素/膨胀）
5. raster_bounds 为零？→ 跳过（空白字形）
6. sprite_atlas.get_or_insert_with(key, || rasterize_glyph(params))
7. 未命中时：rasterize_glyph 在 CPU 上生成掩码字节 → 图集装箱并上传
8. 用 tile.bounds 组装 MonochromeSprite / SubpixelSprite 插入 scene
```

帧末，`Scene` 把同 `texture_id` 的连续精灵合并成一个批次，渲染器逐批绑定一张图集纹理绘制（见 4.4）。

#### 4.2.3 源码精读

先看键的定义（u8-l1 已见 `RenderGlyphParams`，这里补齐哈希细节与另外两类键）：

[../gpui/src/text_system.rs:L1021-L1032](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui/src/text_system.rs#L1021-L1032)：`RenderGlyphParams` 的八个字段完整刻画一次字形渲染：字体、字形、字号、亚像素相位、缩放、是否 emoji、是否亚像素渲染、字形膨胀量。

[../gpui/src/text_system.rs:L1036-L1047](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui/src/text_system.rs#L1036-L1047)：手写的 `Hash` 实现对浮点字段用 `to_bits()` 哈希——位模式相等即数值相等，避免 `f32` 实现Trait 不可用的问题，也保证键的确定性。

[../gpui/src/svg_renderer.rs:L80-L88](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui/src/svg_renderer.rs#L80-L88)：SVG 键 = 资源路径 + 光栅化尺寸；`SMOOTH_SVG_SCALE_FACTOR = 2.` 表示 SVG 一律按 2 倍尺寸光栅化再缩小绘制，换取平滑边缘。

[../gpui/src/assets.rs:L35-L40](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui/src/assets.rs#L35-L40)：图片键 = `ImageId` + 帧序号（动图多帧各占一个瓦片）。

主链路 `paint_glyph`：

[../gpui/src/window.rs:L4275-L4285](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui/src/window.rs#L4275-L4285)：亚像素量化——把字形原点乘以相位数、取整再除回，得到 `subpixel_variant`（x/y 各 0–3）与整数原点。这是「亚像素相位成为缓存键一部分」的实现处。

[../gpui/src/window.rs:L4286-L4297](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui/src/window.rs#L4286-L4297)：判定亚像素渲染开关（委托 `should_use_subpixel_rendering`，L4339–L4356：要求不透明背景 + 平台支持 + 字号推荐模式）后组装 `RenderGlyphParams`。

[../gpui/src/window.rs:L4299-L4307](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui/src/window.rs#L4299-L4307)：**全讲最核心的一段**——`raster_bounds` 为零即跳过；随后 `sprite_atlas.get_or_insert_with(&params.into(), &mut || rasterize_glyph(&params))`。`params.clone().into()` 即 `RenderGlyphParams → AtlasKey::Glyph` 的 `From` 转换（[platform.rs:L1305-L1321](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui/src/platform.rs#L1305-L1321)）。命中缓存时闭包根本不会执行——这就是「每个字形配置只光栅化一次」的机制。

[../gpui/src/window.rs:L4308-L4334](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui/src/window.rs#L4308-L4334)：用瓦片尺寸与 `raster_bounds.origin` 算出屏幕上的 bounds，按 `subpixel_rendering` 插入 `SubpixelSprite` 或 `MonochromeSprite`，`tile` 凭证原样嵌进图元。

三条平行链路：

[../gpui/src/window.rs:L4378-L4397](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui/src/window.rs#L4378-L4397)：`paint_emoji` 组装 `is_emoji: true`、相位取默认（0,0）的键，同样走 `get_or_insert_with` + `rasterize_glyph`；键的 `is_emoji` 使 `texture_kind()` 判为 `Polychrome`，故在 [L4406-L4415](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui/src/window.rs#L4406-L4415) 插入的是 `PolychromeSprite`。

[../gpui/src/window.rs:L4437-L4452](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui/src/window.rs#L4437-L4452)：`paint_svg` 用「路径 + 2 倍尺寸」作键，闭包调 `svg_renderer.render_alpha_mask`；产出 `None` 时（资源未就绪）整段静默跳过，正对应 4.1 练习 1。

[../gpui/src/window.rs:L4512-L4528](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui/src/window.rs#L4512-L4528)：`paint_image` 的闭包用 `Cow::Borrowed` 借用图片字节（无需拷贝），键为 `(ImageId, frame_index)`；后续还会按可见区域比例裁出子瓦片（sub_tile）。

精灵如何被分批：

[../gpui/src/scene.rs:L711-L719](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui/src/scene.rs#L711-L719)：`MonochromeSprite` 结构——除 bounds/color/content_mask 外直接内嵌 `tile: AtlasTile`，即每实例携带「哪张纹理 + 哪个矩形」。

[../gpui/src/scene.rs:L387-L407](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui/src/scene.rs#L387-L407)：批化逻辑——按绘制顺序排序后，把连续且 `tile.texture_id` 相同的单色精灵合并成一个 `PrimitiveBatch::MonochromeSprites { texture_id, range }`。渲染器（4.4）拿到批次后只需绑定一张图集纹理画整个 range。

#### 4.2.4 代码实践

**实践目标**：确认「取件路径」的全部入口，理解哪条链路产出哪类精灵。

**操作步骤**：

1. 在 `../gpui/src/window.rs` 中搜索 `get_or_insert_with`（应恰好命中 4 处：L4303、L4393、L4446、L4519）。
2. 为每处填写一张「取件路径表」：

   | 行号 | 所在函数 | 键类型 | build 闭包做什么 | 插入的图元 | 纹理类别 |
   | --- | --- | --- | --- | --- | --- |
   | L4303 | paint_glyph | RenderGlyphParams | text_system.rasterize_glyph | Monochrome/SubpixelSprite | Monochrome 或 Subpixel |
   | L4393 | paint_emoji | RenderGlyphParams(is_emoji) | rasterize_glyph | PolychromeSprite | Polychrome |
   | L4446 | paint_svg | RenderSvgParams | svg_renderer.render_alpha_mask | MonochromeSprite | Monochrome |
   | L4519 | paint_image | RenderImageParams | 直接借用图片字节 | PolychromeSprite | Polychrome |

3. （可选，待本地验证）在你当前平台运行一个 gpui 示例（如 `cargo run -p gpui --example window`，工作目录为 `crates/gpui`），在调试器里对 `gpui::Window::paint_glyph` 下断点，观察同一字符串第二次重绘时闭包是否不再执行（缓存命中）。

**需要观察的现象**：表格中「纹理类别」列与 4.1.4 决策表完全对应；调试断点下第二次绘制同一文本时 `rasterize_glyph` 不再被调用。

**预期结果**：4 行取件路径表；若执行了步骤 3，记录「首帧 N 次光栅化、次帧 0 次」。

#### 4.2.5 小练习与答案

**练习 1**：亚像素相位量化把相位空间限制为 4×4 = 16 格，为什么不做满精度？
**答案**：瓦片数随键空间爆炸。满精度意味着每个字形在每个微小平移上都占一个瓦片，图集立刻被同字形的无数个近 duplicate 淹没；4 相位已是视觉收益与缓存规模的工程折中（x、y 方向各 4 档，见 L4275–L4284 的 `SUBPIXEL_VARIANTS_X/Y`）。

**练习 2**：`paint_image` 的 build 闭包为什么能 `Cow::Borrowed` 而 `paint_glyph` 必须生成 `Cow::Owned`？
**答案**：图片字节本来就常驻内存（`RenderImage` 持有 BGRA 帧），借用零拷贝即可；字形掩码则必须当场由 CPU 光栅化生成，是新分配的字节，只能 `Owned`。`Cow` 类型让图集的上传接口统一接受两种来源。

**练习 3**：如果同一帧里 1000 个字形精灵分属 3 张单色纹理，渲染器最多发生几次纹理绑定切换（忽略其他图元）？
**答案**：至多 3 次加批次边界重排——`Scene` 已把同 `texture_id` 的连续精灵合批，每批绑定一次。这正是图集存在的意义：切换次数取决于纹理数而非精灵数。

### 4.3 DirectXAtlas：Windows 的图集实现

#### 4.3.1 概念说明

`DirectXAtlas` 是 `PlatformAtlas` 在 D3D11 上的实现。它的全部状态（设备句柄、三组纹理列表、键表）收在一把 `parking_lot::Mutex` 里——因为图集句柄以 `Arc<dyn PlatformAtlas>` 形式被 gpui 的 `Window` 与渲染器共同持有，可能在多线程上下文被触碰，这一点与 `Platform` 本身的 `Rc` 单线程姿态不同，更接近 `PlatformTextSystem` 的 `Arc + Send + Sync` 风格（u8-l1）。

四个关键词：

- **etagere 装箱**：每张纹理配一个 `BucketedAtlasAllocator`，负责把大小不一的矩形装进固定大小的纹理。
- **三类纹理列表**：`monochrome/polychrome/subpixel` 各一个 `AtlasTextureList`，像素格式分别为 R8_UNORM（1 字节）、B8G8R8A8_UNORM（4 字节）、R8G8B8A8_UNORM（4 字节）。
- **从新到旧找空间**：装箱优先在最新一张纹理里找，放不下再建新纹理（至少 1024×1024，至多 16384×16384）。
- **引用计数 + free_list**：每张纹理数着 `live_atlas_keys`，归零后槽位进 free_list 供新纹理复用。

#### 4.3.2 核心流程

```text
get_or_insert_with(key, build):
    lock 整个图集
    命中 tiles_by_key → 返回缓存瓦片
    未命中 → build() 得 (size, bytes)
    allocate(size, kind):
        在该类别纹理列表里从最后一张往前找能装下的 → 返回瓦片
        都装不下 → push_texture:
            尺寸 = clamp(min_size, 1024..=16384)
            按 kind 选像素格式与每像素字节数
            CreateTexture2D + CreateShaderResourceView
            槽位优先取 free_list，否则追加
    upload: UpdateSubresource 把 bytes 写进纹理的 tile.bounds 矩形
    tiles_by_key.insert(key, tile)

remove(key):
    删键 → 装箱器 deallocate → live_atlas_keys -= 1
    归零 → 槽位进 free_list

handle_device_lost(new_device, new_context):
    替换设备句柄，清空三组纹理与全部键
    （下次 paint 会因缓存全失而重新光栅化上传）
```

#### 4.3.3 源码精读

状态结构：

[../gpui_windows/src/directx_atlas.rs:L17-L35](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui_windows/src/directx_atlas.rs#L17-L35)：`DirectXAtlas(Mutex<DirectXAtlasState>)`；State 持 D3D11 设备与立即上下文、三组 `AtlasTextureList<DirectXAtlasTexture>`、`tiles_by_key` 键表。`DirectXAtlasTexture` 则持有纹理 id、每像素字节数、etagere 分配器、`ID3D11Texture2D`、着色器资源视图与 `live_atlas_keys` 计数。

[../gpui_windows/src/directx_atlas.rs:L38-L47](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui_windows/src/directx_atlas.rs#L38-L47)：构造只需克隆设备与设备上下文的 COM 句柄——图集从诞生起就绑定在「某个 D3D11 设备」上，这也是设备丢失必须重建图集的原因。

契约实现：

[../gpui_windows/src/directx_atlas.rs:L73-L96](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui_windows/src/directx_atlas.rs#L73-L96)：`get_or_insert_with` 的 Windows 版：查表 → 未命中才 `build()` → `allocate` 装箱 → `texture.upload` 上传 → 记表返回。与契约流程图一一对应。

[../gpui_windows/src/directx_atlas.rs:L98-L125](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui_windows/src/directx_atlas.rs#L98-L125)：`remove`：按 `kind` 选对纹理列表、按下标取槽位，`deallocate` + 引用计数减一，归零则槽位进 free_list。注意 `texture_slot.take()` 后有条件地放回——借用舞蹈避免同时持有两个可变引用。

装箱与建纹理：

[../gpui_windows/src/directx_atlas.rs:L128-L152](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui_windows/src/directx_atlas.rs#L128-L152)：`allocate` 用 `iter_mut().rev().find_map(...)` **从最新纹理向旧纹理**找第一个能装下的位置；全失败才 `push_texture` 建新的。优先新纹理可以让旧纹理更快「死透」进 free_list。

[../gpui_windows/src/directx_atlas.rs:L159-L189](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui_windows/src/directx_atlas.rs#L159-L189)：尺寸钳制在 1024（默认）到 16384（D3D11 上限，注释附微软文档链接）之间；三类像素格式在此选定——单色 R8_UNORM、多色 B8G8R8A8_UNORM、亚像素 R8G8B8A8_UNORM，全部带 `D3D11_BIND_SHADER_RESOURCE`（可被着色器采样）。

[../gpui_windows/src/directx_atlas.rs:L190-L245](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui_windows/src/directx_atlas.rs#L190-L245)：`CreateTexture2D` 建空纹理、`CreateShaderResourceView` 建视图；槽位从 free_list 弹出或追加到尾部，`AtlasTextureId { index, kind }` 由此定型。设备丢失时 `CreateTexture2D` 返回 None 是允许的——外层恢复逻辑稍后会整体重建。

上传与回收：

[../gpui_windows/src/directx_atlas.rs:L279-L320](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui_windows/src/directx_atlas.rs#L279-L320)：`upload` 用 `UpdateSubresource` + `D3D11_BOX` 把字节写进纹理的瓦片矩形。开头有一段防御：若调用方给的字节短于 `行字节数 × 高度`，记录错误并放弃上传——注释解释了原因（`UpdateSubresource` 会按 box 尺寸读取，短 slice 会被驱动越界读多达数 MB）。这是一处值得学习的「边界数据先验校验」。

[../gpui_windows/src/directx_atlas.rs:L264-L277](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui_windows/src/directx_atlas.rs#L264-L277) 与 [L322-L328](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui_windows/src/directx_atlas.rs#L322-L328)：单纹理级装箱（`live_atlas_keys += 1`）与引用计数接口。

[../gpui_windows/src/directx_atlas.rs:L58-L70](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui_windows/src/directx_atlas.rs#L58-L70)：`handle_device_lost`——换新设备句柄、清空三组纹理与全部键。清键意味着旧 `AtlasTile` 全部作废，下次 paint 走完整重光栅化。它在渲染器的恢复流程中被调用（见 4.4 的 [directx_renderer.rs:L314](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui_windows/src/directx_renderer.rs#L314)）。

测试（理解行为的最好材料）：

[../gpui_windows/src/directx_atlas.rs:L342-L419](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui_windows/src/directx_atlas.rs#L342-L419)：测试模块用 `D3D_DRIVER_TYPE_WARP`（CPU 模拟的软件 GPU，L355–L373）创建设备，因此**不需要真实显卡**即可测图集。`test_remove_deallocates_tile_space_for_reuse` 插入 64×64 与 700×700 两块，断言它们落进同一张 1024 纹理；移除 700×700 后再插一块 700×700，断言新块仍与最初的小块同纹理——证明 deallocate 后空间确实被复用而非新建纹理。

#### 4.3.4 代码实践

**实践目标**：通过测试断言验证 free_list/装箱复用行为。

**操作步骤**：

1. 通读 [directx_atlas.rs:L392-L419](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui_windows/src/directx_atlas.rs#L392-L419) 的测试，写下三个 `assert_eq!` 各自验证什么。
2. 若你在 Windows 上：在仓库根目录运行 `cargo test -p gpui_windows directx_atlas`，观察测试是否通过（WARP 设备无需独显）。
3. 若你在非 Windows 上：跳过运行，改为回答——「为什么这个测试能在没有 GPU 的 CI 机器上通过？」（答案就在 L359–L362 的 `D3D_DRIVER_TYPE_WARP`。）

**需要观察的现象**：Windows 上测试输出 1 passed；`test_remove_deallocates_tile_space_for_reuse` 名称所述的「移除即回收空间」被两条纹理 id 相等断言证实。

**预期结果**：能口述「700×700 移除后，同尺寸新图复用原空间，因此 texture_id 不变」。步骤 2 在非 Windows 环境标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `allocate` 从最新纹理往旧纹理找，而不是从旧往新？
**答案**：优先填最新纹理可以让更旧的纹理尽快「死透」（全部键被移除）、槽位尽早进 free_list 复用；若从旧往新填，旧纹理会一直挂着零星键，既占显存又占槽位。

**练习 2**：亚像素掩码为什么用 R8G8B8A8（4 字节）而灰度只用 R8（1 字节）？
**答案**：LCD 亚像素渲染里 R、G、B 三个物理亚像素的覆盖率彼此独立，需要每通道一份掩码（外加整体 alpha），天然是 4 字节；灰度掩码所有通道共享一个覆盖率，1 字节足够。这直接决定了 `AtlasTextureKind::Subpixel` 的纹理格式选择（L184–L188）。

**练习 3**：设备丢失后 `handle_device_lost` 只清空图集、不主动重填。谁负责把内容补回来？
**答案**：没有人「主动」补。清空 `tiles_by_key` 后所有键失效，下一帧 paint 时 `get_or_insert_with` 全部未命中，build 闭包重新光栅化并上传——恢复被自然地折叠进正常的渲染路径（配合 4.4 将看到的 `skip_draws` 丢弃恢复后的第一帧）。

### 4.4 渲染器配合与所有权链：设备 → 渲染器 → 图集

#### 4.4.1 概念说明

本模块回答学习目标里的所有权问题。Windows 渲染栈三层各自的份额与生命周期：

| 层 | 类型 | 份数 | 持有 |
| --- | --- | --- | --- |
| 设备 | `DirectXDevices`（adapter/factory/device/context 四元组） | 全应用一份 | `WindowsPlatform`（`RefCell<Option<..>>`） |
| 渲染器 | `DirectXRenderer`（交换链、管线、合成器视觉） | 每窗口一份 | `WindowsWindow.renderer: RefCell<DirectXRenderer>` |
| 图集 | `DirectXAtlas` | 每渲染器一份（即每窗口一份） | `DirectXRenderer.atlas: Arc<DirectXAtlas>` |

对外只暴露一个出口：`PlatformWindow::sprite_atlas()` 把 `Arc<DirectXAtlas>` 擦成 `Arc<dyn PlatformAtlas>` 交给 gpui 的 `Window`（u3-l2 讲过 `Window` 在构造时取走它，[window.rs:L1413](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui/src/window.rs#L1413)）。因此**同一个 gpui 应用的多个窗口各有一个互不共享的图集**——设计上牺牲显存换取「窗口间互不干扰、可独立随设备重建」。

另外要澄清边界：**路径（Path）图元不进图集**。Windows 上路径走「每帧 MSAA 中间纹理两阶段绘制」，图集只收字形、SVG、图片三类内容。

#### 4.4.2 核心流程

一帧 `draw(scene)` 的批分发：

```text
scene 已按 (draw_order, primitive_kind) 排序并合批
for batch in scene.batches():
    Shadows        → draw_shadows
    Quads          → draw_quads
    Paths          → 先画进 MSAA 中间纹理，再从中间纹理搬到后台缓冲
    Underlines     → draw_underlines
    MonochromeSprites { texture_id, range } → atlas.get_texture_view(texture_id)
                                             → 绑定该 SRV，实例化绘制 range
    SubpixelSprites / PolychromeSprites     → 同上，各走专用管线
present()
```

设备丢失恢复（简链）：vsync 线程检测到设备移除（u6-l2）→ 平台侧重建 `DirectXDevices` 并经窗口消息送达 → `DirectXRenderer::handle_device_lost_impl` 重建设备/资源/管线，并调用 `atlas.handle_device_lost` 清空图集 → `skip_draws = true` 丢弃恢复后第一帧 → 下一帧起 paint 重新填充图集。

#### 4.4.3 源码精读

所有权链自底向上：

[../gpui_windows/src/directx_devices.rs:L39-L44](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui_windows/src/directx_devices.rs#L39-L44)：`DirectXDevices` 四元组——DXGI 适配器与工厂、D3D11 设备与立即上下文。u6-l2 讲过它的枚举与创建。

[../gpui_windows/src/platform.rs:L114-L131](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui_windows/src/platform.rs#L114-L131)：`WindowsPlatform::new` 在非 headless 时创建一份设备（连同 DirectWrite 文本系统，它同样拿设备的引用）；headless 时为 `None`——没有设备就没有图集。

[../gpui_windows/src/window.rs:L142-L143](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui_windows/src/window.rs#L142-L143)：`WindowsWindow::new` 里用平台传下来的设备构造**本窗口专属的** `DirectXRenderer`。

[../gpui_windows/src/directx_renderer.rs:L39-L57](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui_windows/src/directx_renderer.rs#L39-L57)：渲染器结构：`atlas: Arc<DirectXAtlas>` 与 `devices: Option<DirectXRendererDevices>`、`resources`（交换链等）、`pipelines`（八条管线）、`font_info`（DirectWrite 的伽马/对比度参数）并列。

[../gpui_windows/src/directx_renderer.rs:L154-L198](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui_windows/src/directx_renderer.rs#L154-L198)：构造顺序——先包 `DirectXRendererDevices`，**然后创建图集（L165：`DirectXAtlas::new(&devices.device, &devices.device_context)`）**，再建交换链资源、全局元素、八条管线与 DirectComposition。图集是渲染器众多资源中唯一以 `Arc` 持有的，因为它要被擦除后交给 gpui 层。

[../gpui_windows/src/directx_renderer.rs:L200-L202](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui_windows/src/directx_renderer.rs#L200-L202) 与 [../gpui_windows/src/window.rs:L999-L1000](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui_windows/src/window.rs#L999-L1000)：`sprite_atlas()` 两级转发——渲染器把 `Arc<DirectXAtlas>` 擦成 `Arc<dyn PlatformAtlas>`，`WindowsWindow` 的 `PlatformWindow::sprite_atlas` 实现再借出。这就是契约出口。

帧绘制与批分发：

[../gpui_windows/src/directx_renderer.rs:L330-L392](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui_windows/src/directx_renderer.rs#L330-L392)：`draw`——`skip_draws` 置位时直接返回（设备丢失恢复后丢弃首帧）；`pre_draw` 上传全局参数并清屏；`upload_scene_buffers` 后按 `scene.batches()` 分发八类图元，其中三类精灵批次携带 `texture_id`；最后 `present`。

[../gpui_windows/src/directx_renderer.rs:L438-L489](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui_windows/src/directx_renderer.rs#L438-L489)：`upload_scene_buffers` 把阴影/矩形/下划线/三类精灵的实例数据整块写进各自的实例缓冲——精灵实例里就嵌着 `AtlasTile`，着色器凭它在图集纹理里采样。

[../gpui_windows/src/directx_renderer.rs:L647-L669](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui_windows/src/directx_renderer.rs#L647-L669)：`draw_monochrome_sprites`——**图集与渲染器在本帧唯一的交点**：`self.atlas.get_texture_view(texture_id)` 取回该批纹理的 SRV，交给 `mono_sprites` 管线的 `draw_range_with_texture` 绑定并绘制实例区间。亚像素与多色版本（[L671-L693](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui_windows/src/directx_renderer.rs#L671-L693)、[L695-L717](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui_windows/src/directx_renderer.rs#L695-L717))结构完全相同，只是管线与混合状态不同（亚像素走 [L1416 起](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui_windows/src/directx_renderer.rs#L1416) 的专用混合状态，即 u6-l2 讲过的 dual-source blending）。

路径不进图集的证据：

[../gpui_windows/src/directx_renderer.rs:L524-L585](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui_windows/src/directx_renderer.rs#L524-L585)：路径先被光栅化进**每窗口的 MSAA 中间纹理**（`path_intermediate_msaa_texture`，属于 `DirectXResources` 而非图集），再 `ResolveSubresource` 到非 MSAA 中间纹理；[L587-L629](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui_windows/src/directx_renderer.rs#L587-L629) 把中间纹理按路径 bounds 搬到后台缓冲。路径形状每帧都可能变，缓存进图集收益低，故走即时光栅化。

设备丢失恢复：

[../gpui_windows/src/directx_renderer.rs:L263-L328](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui_windows/src/directx_renderer.rs#L263-L328)：`handle_device_lost_impl` 重建 devices/resources/globals/pipelines/direct_composition，并在 [L314](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui_windows/src/directx_renderer.rs#L314) 调用 `self.atlas.handle_device_lost(&devices.device, &devices.device_context)` 让图集换绑新设备并清空缓存，最后置 `skip_draws = true`。

macOS 对照（MetalAtlas）：

[../gpui_apple/src/metal_atlas.rs:L13-L37](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui_apple/src/metal_atlas.rs#L13-L37)：`MetalAtlas` 与 DirectX 版结构同型（Mutex 包状态、键表、两组纹理列表），但**没有 `subpixel_textures`**——Apple 平台不做 LCD 亚像素掩码缓存，`Subpixel` 分支直接 `unreachable!()`。

[../gpui_apple/src/metal_atlas.rs:L121-L160](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui_apple/src/metal_atlas.rs#L121-L160)：建纹理用 Metal 的 `TextureDescriptor`：单色 `A8Unorm`、多色 `BGRA8Unorm`（对比 Windows 的 R8/B8G8R8A8，语义等价、格式名不同）；并按 `is_apple_gpu` 选择存储模式——统一内存架构用 `Shared`，独显用 `Managed`（注释附 Apple 文档链接）。这是「同一契约按 GPU 生态微调」的直观例子。

[../gpui_apple/src/metal_atlas.rs:L226-L239](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui_apple/src/metal_atlas.rs#L226-L239)：上传用 `MTLTexture.replace_region`，对应 D3D 的 `UpdateSubresource`——两个 GPU API 的「CPU 写纹理子区域」原语一一对应。

[../gpui_apple/src/metal_renderer.rs:L1623-L1624](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui_platform/../gpui_apple/src/metal_renderer.rs#L1623-L1624)：`MetalRenderer` 的 `PlatformWindow::sprite_atlas` 实现同样只做 `Arc` 擦除转发——所有权姿态与 Windows 完全一致，证明「每窗口一图集、经 PlatformWindow 出口」是跨平台惯例而非 Windows 特例。

#### 4.4.4 代码实践

**实践目标**：把三层所有权与一帧内的图集访问画成可核对的图。

**操作步骤**：

1. 画所有权图（Mermaid 或手绘均可），节点为 `WindowsPlatform`、`DirectXDevices`、`WindowsWindow`、`DirectXRenderer`、`DirectXAtlas`、`gpui::Window`，边为持有关系，边上标注持有类型（`RefCell<Option<..>>` / `RefCell<..>` / `Arc<..>` / 值）与关键行号（L76、L65、L41、L142、L1413）。
2. 在图上用另一种颜色标出**一帧内**的三次图集接触：paint 期 `get_or_insert_with`（window.rs:L4303 等）、批分发期 `get_texture_view`（directx_renderer.rs:L657/681/705）、异常期 `handle_device_lost`（directx_renderer.rs:L314）。
3. 用 `grep -n "get_texture_view" crates/gpui_windows/src/*.rs` 验证接触点数量与你标的一致。

**需要观察的现象**：图中 `Arc<DirectXAtlas>` 是唯一一条从渲染器指向图集的边，且同时被擦除后交给 `gpui::Window`；`get_texture_view` 的调用点恰好 3 处加定义 1 处。

**预期结果**：一张两层颜色的所有权/访问图，附行号。此图与综合实践的数据流图互补（一纵一横）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `DirectXAtlas` 用 `Arc + Mutex` 共享，而 `Platform` 上的大多数状态只用 `Rc`（u2-l1）？
**答案**：图集句柄要以 `Arc<dyn PlatformAtlas>` 的形式穿越 `PlatformWindow::sprite_atlas()` 交给 gpui 层，并被 `Window` 长期持有；它的类型签名（`Send + Sync` 的对象安全 trait + `Arc`）决定了实现必须线程安全，因此内部用 `Mutex` 收拢全部状态。这与 `PlatformTextSystem` 的 `Arc` 姿态一致（u8-l1），是 gpui 中「需要跨句柄共享的平台设施」的通用形态。

**练习 2**：多窗口各建一套图集有什么代价与好处？
**答案**：代价是同一字形在不同窗口里可能被重复光栅化、重复占显存；好处是窗口之间完全解耦——设备丢失恢复、纹理生命周期、销毁窗口都只影响自己的图集，不需要跨窗口的失效协调。对编辑器这类「窗口数少、单窗口内容巨大」的负载，这是合理的取舍。

**练习 3**：`MetalAtlas` 为什么可以没有 `Subpixel` 组，而 `PlatformAtlas` 契约却定义了三种类别？
**答案**：类别是**能力并集**：Windows 有 LCD 亚像素渲染所以三类全用；Apple 平台不做该渲染，`should_use_subpixel_rendering` 在源头就是 false，`Subpixel` 键根本不会产生，实现里 `unreachable!()` 即可。契约按最全能力定义、实现按平台裁剪——与 u2-l1 总结的「默认实现三姿态」同源。

## 5. 综合实践

**任务**：画一张「一次文本绘制请求」的完整数据流图——从 `layout_line` 的结果到字形进入图集、再到 GPU 实例化绘制，标注每一步对应的 trait 方法、函数与源码文件。这是本讲 practice_task 的正式版本，完成后你就把 u8-l1（前半程）与本讲（后半程）串成了一条线。

**要求**：

1. 起点是 `ShapedLine::paint` 产出的每个字形（font_id、glyph_id、原点）；终点是 GPU 上一次 `draw_range_with_texture` 调用。
2. 每一步标注：所在文件与函数名（能附行号更好）、输入输出、命中/未命中两条分支（仅 `get_or_insert_with` 处需要分叉）。
3. 在图侧注明三类键参数结构与三类精灵图元的对应关系。

**参考骨架**（请按自己的阅读补全行号）：

```text
ShapedLine::paint (gpui/text_system.rs)
  │  逐字形调用
  ▼
Window::paint_glyph (gpui/window.rs:4261)
  │  1) 亚像素量化 → subpixel_variant      (window.rs:4275)
  │  2) 组装 RenderGlyphParams            (window.rs:4288)
  ▼
TextSystem::raster_bounds                    (u8-l1)
  │  零尺寸 → 直接结束
  ▼
PlatformAtlas::get_or_insert_with            (gpui/platform.rs:1324)
  ├─ 命中: tiles_by_key 查表 → AtlasTile
  └─ 未命中: TextSystem::rasterize_glyph → (size, bytes)
        │
        ▼  DirectXAtlas::allocate + upload   (gpui_windows/directx_atlas.rs:128/279)
           etagere 装箱 → CreateTexture2D(按需) → UpdateSubresource
  ▼
Scene::insert_primitive(Monochrome/SubpixelSprite)  (gpui/window.rs:4314, scene.rs:711)
  │  Scene 按 texture_id 合批               (scene.rs:387)
  ▼
PlatformWindow::draw(scene)                  (gpui/platform.rs:863)
  ▼
DirectXRenderer::draw                        (gpui_windows/directx_renderer.rs:330)
  │  upload_scene_buffers 写实例(内嵌 AtlasTile) (directx_renderer.rs:438)
  ▼
DirectXAtlas::get_texture_view(texture_id)   (directx_atlas.rs:49)
  ▼
PipelineState::draw_range_with_texture       (directx_renderer.rs:658)
  │  绑定 SRV,绘制实例区间
  ▼
DirectXRenderer::present                     (directx_renderer.rs:245)
```

**验收标准**：图中每个箭头都能说出「谁调用谁、数据是什么」；`get_or_insert_with` 的两条分支都能解释「第二次为什么快」（命中分支不执行闭包，零光栅化、零上传）。

## 6. 本讲小结

- `PlatformAtlas` 是「以绘制参数为键、以纹理内矩形为值」的平台无关缓存：`AtlasKey`（Glyph/Svg/Image）自带 `texture_kind()` 分类，`AtlasTile` 是嵌进 GPU 实例缓冲的取件凭证，图集把每帧纹理切换从精灵数级降到纹理数级。
- 键的三类参数 `RenderGlyphParams`（含亚像素相位与膨胀）、`RenderSvgParams`（2 倍超采样）、`RenderImageParams`（帧序号）在 `paint_glyph`/`paint_svg`/`paint_image` 里经 `get_or_insert_with` 进入图集，未命中才调 CPU 光栅化。
- `DirectXAtlas` 用 etagere 分桶装箱管理三类纹理（R8/B8G8R8A8/R8G8B8A8，1024–16384 像素见方），从新往旧找空间，`live_atlas_keys` 引用计数 + free_list 实现槽位回收，`UpdateSubresource` 上传前做字节长度防御。
- 所有权链是「设备全局一份 → 渲染器每窗口一份 → 图集每渲染器一份」，对外仅经 `PlatformWindow::sprite_atlas()` 擦成 `Arc<dyn PlatformAtlas>` 交给 gpui `Window`；一帧内渲染器只在批分发时用 `get_texture_view` 取回纹理视图；路径图元不走图集，走每帧 MSAA 中间纹理。
- 设备丢失时 `handle_device_lost` 换绑设备并清空全部键，配合 `skip_draws` 丢一帧，恢复折叠进正常 paint 路径；`MetalAtlas` 同型但无 Subpixel 组，并按 Apple GPU 选 Shared 存储模式——同一契约在不同 GPU API 上的两种落地。

## 7. 下一步学习建议

本讲之后，渲染侧只剩两块拼图：

1. **u8-l3 无头渲染**：`PlatformHeadlessRenderer` 的 `sprite_atlas()`（platform.rs:L1008-L1009）说明离屏渲染同样要一个图集——读完本讲再去看它如何在不开窗口的情况下做完整场景编码与 GPU 提交，会非常自然。
2. **u8-l4 test-support**：`TestAtlas`（`../gpui/src/platform/test/window.rs`）是 `PlatformAtlas` 的测试替身，配合 `contains`（本讲 4.1）可以写出「某键是否已缓存」的确定性断言；届时可回头把本讲 4.2 的缓存命中观察做成真正的自动化测试。

此外建议按兴趣选读：`../gpui/src/scene.rs` 的完整批化迭代器（本讲只看了 MonochromeSprite 一支），以及 `etagere` crate 的文档——理解「分桶搁架」为何适合字形这种高度 clustered 的尺寸分布。

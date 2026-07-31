# 图像渲染：光栅 / SVG / PDF

## 1. 本讲目标

本讲讲解 typst-render 如何把排版结果中的 **图像元素（`FrameItem::Image`）** 画进像素画布。学完后你应当能够：

1. 说清 `render_image` 是如何根据**当前坐标变换**推算出“应该把图像光栅化到多大分辨率”的，并理解它避免除零的三角函数策略。
2. 读懂 `build_texture`（被 `comemo::memoize` 缓存）对 **光栅图（Raster）/ SVG / PDF** 三类图像分别构建纹理的分支。
3. 理解重采样滤镜的选择策略：放大用 CatmullRom、缩小用 Lanczos3、`Pixelated` 用 Nearest，以及为何 `(w,h)` 与原图相等时跳过 `resize` 是一项优化。

本讲承接 [u2-l3 纯色 Paint 转换](./u2-l3-paint-solid.md)：你已经知道 `to_sk_paint` 如何把 Typst 的画笔转成 tiny-skia 的 `sk::Paint`；本讲不再涉及画笔颜色，而是聚焦“**把图像当成纹理（Pattern）贴进画布**”这一条独立链路。

## 2. 前置知识

阅读本讲前，你需要先具备以下概念（在前序讲义中已建立）：

- **Frame 场景树与 `render_frame` 派发**（[u1-l3](./u1-l3-frame-tree.md)）：图像是 `FrameItem::Image(image, size, _)` 这种叶子节点，`size` 是图像在版面上的**显示盒尺寸**（单位 pt）。
- **State 与坐标变换**（[u2-l1](./u2-l1-state-and-transforms.md)）：`state.transform` 是从画布原点到当前点的累积仿射变换，把 pt 坐标映射到像素坐标。本讲会反复用到它。
- **`AbsExt::to_f32`**：把 Typst 的 `Abs` 长度统一换算成 pt 的 `f32`，是 Typst 长度与 tiny-skia 之间唯一的单位关卡。

本讲出现的几个外部库（在 [u1-l1](./u1-l1-project-overview.md) 的依赖全景里已提过）：

| 外部库 | 在本讲的作用 |
| --- | --- |
| **tiny-skia** | 提供 `Pixmap`（像素画布）、`Paint`/`Pattern`（纹理画笔）、`Transform`、`fill_rect` |
| **image** | 光栅图解码（`DynamicImage`）与重采样（`resize_exact` + `FilterType`） |
| **resvg** | 把内嵌 SVG 栅格化进 `Pixmap` |
| **hayro** | 把内嵌 PDF 栅格化（带标准字体解析） |
| **comemo** | 给 `build_texture` 做记忆化缓存 |

一个关键直觉：**图像渲染分两步**。第一步是“**提前把图像缩放到目标分辨率**”（在 `build_texture` 里做），第二步是“**把这张已缩放的纹理用 Pattern 贴进画布**”（在 `render_image` 末尾的 `fill_rect` 里做）。提前缩放是为了让最终的像素更清晰——这是本讲反复出现的主题。

## 3. 本讲源码地图

本讲几乎全部内容都在一个文件里：

| 文件 | 作用 |
| --- | --- |
| [`src/image.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/image.rs) | 图像渲染的全部逻辑：`render_image`、`build_texture`、`build_pdf_texture` |
| [`src/lib.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs) | `render_frame` 中派发到 `render_image` 的那一行（调用现场） |

对应的 typst-library 数据类型（仅供查阅，不属于本 crate）：

- `Image` / `ImageKind`（`Raster` / `Svg` / `Pdf`）/ `ImageScaling`（`Smooth` / `Pixelated`）定义在 `typst-library/src/visualize/image/mod.rs`。
- `RasterImage::dynamic()` 返回 image 库的 `DynamicImage`；`width()/height()` 返回原图像素尺寸。
- `PdfImage::page()/width()/height()` 提供待栅格化的 PDF 页及其尺寸。

---

## 4. 核心概念与源码讲解

本讲按渲染的自然顺序拆成三个最小模块：

- **4.1 `render_image`**：从坐标变换推算目标分辨率，再把纹理贴进画布。
- **4.2 `build_texture`**：对光栅 / SVG / PDF 三类图像构建（可缓存的）纹理，重点是光栅图的重采样滤镜选择。
- **4.3 `build_pdf_texture`**：用 hayro 把内嵌 PDF 栅格化（带 14 种标准字体的回退解析）。

### 4.1 `render_image`：从变换推算目标分辨率并贴图

#### 4.1.1 概念说明

排版阶段只决定图像“**在版面上占多大一块（`size`，单位 pt）**”，并不决定它最终是多少像素。最终像素数取决于两层缩放：

1. **全局分辨率** `pixel_per_pt`（来自 `RenderOptions`，默认 2.0）——已并入 `state.transform`。
2. **图像自身的几何变换**（旋转、缩放、倾斜等）——同样并入 `state.transform`。

所以 `render_image` 拿到的 `state.transform`（记作 `ts`）是“从 pt 到画布像素”的**完整累积变换**。问题变成：**给定这个变换，图像在画布上到底占多少像素？该把纹理光栅化到多大？**

typst-render 的做法不是把图像按原始像素直接画上去、再让 tiny-skia 去缩放（那样会糊），而是**先算出目标分辨率，把图像重采样到该分辨率，再用最近邻（Nearest）贴回画布**。源码注释里给出了设计出处：

```
// For better-looking output, resize `image` to its final size before
// painting it to `canvas`. For the math, see:
// https://github.com/typst/typst/issues/1404#issuecomment-1598374652
```

#### 4.1.2 核心流程

`render_image` 可以拆成四步：

1. **算变换的有效放大率 `scale_x`**（像素/pt）：从 `ts` 中“扣除旋转”，取其缩放分量，并刻意避开接近 0 的三角函数值以防除零。
2. **算目标纹理尺寸 `w × h`**：用 `scale_x` 乘以显示盒尺寸，并以图像原生宽高比 `aspect` 约束。
3. **构建纹理**：调用 `build_texture(image, w, h)`（见 4.2），它会被 `comemo` 缓存。
4. **贴图**：把纹理包成 tiny-skia 的 `Pattern` 画笔，用 `fill_rect` 填进显示盒。

伪代码：

```
ts = state.transform                      # pt → 像素 的完整变换
view_width, view_height = size.x, size.y  # 显示盒尺寸（pt）

theta   = atan2(-ts.kx, ts.sx)            # 旋转角
prefer_sin = |sin θ| > 1/√2               # 选离 0 更远的那一个做分母
scale_x = abs( kx/sin θ  if prefer_sin  else  sx/cos θ )

aspect = img.width / img.height           # 原生宽高比
w = ceil( scale_x * max(view_width, aspect*view_height) )
h = ceil( w / aspect )

pixmap = build_texture(image, w, h)?      # 已缩放到目标分辨率的纹理
paint_scale_x = view_width  / pixmap.width
paint_scale_y = view_height / pixmap.height

# 用 Pattern 把纹理贴进画布（最近邻，避免二次模糊）
fill_rect( (0,0,view_width,view_height), Pattern(pixmap, paint_scale), ts, mask )
```

#### 4.1.3 源码精读

函数签名与显示盒尺寸的提取（注意 `size` 的单位是 pt）：

[src/image.rs:18-26](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/image.rs#L18-L26) —— `render_image` 取出 `ts = state.transform` 和显示盒的 `view_width/view_height`。

**有效放大率 `scale_x` 的推导**（本讲最核心的数学）：

[src/image.rs:31-40](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/image.rs#L31-L40) —— `theta` 与 `scale_x`。

tiny-skia 的 `Transform::from_row(sx, ky, kx, sy, tx, ty)` 表示矩阵

\[
\begin{bmatrix} sx & kx & tx \\ ky & sy & ty \\ 0 & 0 & 1 \end{bmatrix}
\]

对一个**纯旋转 θ 再均匀缩放 s** 的变换，其参数为 \(sx=s\cos\theta,\; kx=-s\sin\theta\)。于是

\[
\theta=\mathrm{atan2}(-kx,\;sx)=\mathrm{atan2}(s\sin\theta,\;s\cos\theta)=\theta
\]

正好还原出旋转角。还原出 θ 后，缩放系数可由两式之一得到：

\[
s=\left|\frac{kx}{\sin\theta}\right|=\left|\frac{sx}{\cos\theta}\right|
\]

两个式子理论上相等，但当 θ 接近 0 或 π/2 时，\(\sin\theta\) 或 \(\cos\theta\) 会接近 0，做分母会数值爆炸。代码用 `prefer_sin`（判断 \(|\sin\theta|>1/\sqrt{2}\)，即 sin 比 cos 更“强壮”）来选**离 0 更远**的那个做分母：

```rust
let prefer_sin = libm::sinf(theta).abs() > std::f32::consts::FRAC_1_SQRT_2;
let scale_x = f32::abs(if prefer_sin {
    ts.kx / libm::sinf(theta)
} else {
    ts.sx / libm::cosf(theta)
});
```

其中 `FRAC_1_SQRT_2` 即 \(1/\sqrt{2}\approx0.707\)。这是一个典型的“**避开除零**”数值技巧。

> 说明：这里用 `libm::atan2f/sinf/cosf` 而非 `f32::atan2/sin/cos`，是为了保证在 `no_std`/跨平台下行为一致（typst 注重确定性输出）。

**目标纹理尺寸 `w × h`**：

[src/image.rs:42-44](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/image.rs#L42-L44) —— 用 `aspect` 约束宽高。

```rust
let aspect = (image.width() as f32) / (image.height() as f32);
let w = (scale_x * view_width.max(aspect * view_height)).ceil() as u32;
let h = ((w as f32) / aspect).ceil() as u32;
```

要点：

- `aspect` 是图像**原生**宽高比，纹理必须保持这个比例（`h = w/aspect`），否则图像会变形。
- `max(view_width, aspect·view_height)` 在两个方向候选分辨率里**取大者**。这样保证贴图时纹理在两个方向上都**不会被迫放大**（最多被缩小），从而保持锐利。
- `.ceil() as u32` 得到整数像素。

**构建纹理并贴图**：

[src/image.rs:46-62](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/image.rs#L46-L62) —— 调用 `build_texture` 拿到已缩放的纹理，再包成 `Pattern` 画笔贴进画布。

```rust
let pixmap = build_texture(image, w, h)?;
let paint_scale_x = view_width / pixmap.width() as f32;
let paint_scale_y = view_height / pixmap.height() as f32;

let paint = sk::Paint {
    shader: sk::Pattern::new(
        (*pixmap).as_ref(),
        sk::SpreadMode::Pad,
        sk::FilterQuality::Nearest,        // 关键：最近邻
        1.0,
        sk::Transform::from_scale(paint_scale_x, paint_scale_y),
    ),
    ..Default::default()
};

let rect = sk::Rect::from_xywh(0.0, 0.0, view_width, view_height)?;
canvas.fill_rect(rect, &paint, ts, state.mask);
```

三个关键设计：

1. **`build_texture` 已经把图像重采样到目标分辨率**，所以贴图时用 `FilterQuality::Nearest`——**避免 tiny-skia 再做一次平滑插值**，否则等于“插值两次”，图像会更糊。
2. `paint_scale_x/y = view_* / pixmap.*`：把纹理缩放回显示盒的 pt 尺寸（因为 `fill_rect` 是在 pt 坐标系里画的，`ts` 再把它映到像素）。理想情况下 `paint_scale` 接近 1，纹理几乎 1:1 落到画布。
3. `SpreadMode::Pad`：超出纹理边界时用边缘像素填充（本场景不会真触发越界，因为 rect 正好等于纹理区域）。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `scale_x` 的两条分支在旋转下的等价性，并体会 `prefer_sin` 的避零作用。

**操作步骤（源码阅读 + 手算）**：

1. 假设一个**顺时针旋转 45°、放大 2 倍**的变换。先按 \(s=2,\theta=45°\) 写出 `sx/kx`：\(sx=2\cos45°\approx1.414,\;kx=-2\sin45°\approx-1.414\)。
2. 计算 `theta = atan2(-kx, sx) = atan2(1.414, 1.414) = 45°`。
3. 因为 \(|\sin45°|=0.707 \not> 0.707\)（严格大于才走 sin 分支），所以走 **cos 分支**：`scale_x = |sx/cos45°| = |1.414/0.707| = 2.0`。✓ 还原出 s=2。
4. 再把 θ 设成 **接近 0**（比如 1°）：此时 \(\cos1°\approx1\)（健康），\(\sin1°\approx0.0175\)（很弱）。`prefer_sin` 为假 → 走 cos 分支，除以 ~1，安全。**反过来想**：若误走 sin 分支，`kx/sin1°` 会让一个微小 kx 被极小值放大成噪声——这正是 `prefer_sin` 要规避的。

**需要观察的现象**：当 θ 从 0° 增到 90°，`prefer_sin` 在 45° 附近“切换”，但两条分支算出的 `scale_x` 应当**连续且相等**（都等于真实缩放 s）。

**预期结果**：两种分支给出同一个 `scale_x`；切换点（45°）只是为了让分母始终是较大的那个三角值，纯粹是数值健壮性考量，不影响结果正确性。运行命令「待本地验证」（可在 `render_image` 入口临时加一行 `eprintln!("theta={} scale_x={}", theta, scale_x);` 观察）。

#### 4.1.5 小练习与答案

**练习 1**：为什么贴图时用 `FilterQuality::Nearest` 而不是 `BiLinear`/`BiCubic`？

**参考答案**：因为 `build_texture` 已经把图像**重采样到接近目标分辨率**了。如果贴图时再让 tiny-skia 做一次高质量插值，等于对已经插值过的像素再插值一次，会产生二次模糊。Nearest 让纹理几乎 1:1 落到画布，保持锐利。

**练习 2**：`render_image` 的返回类型是 `Option<()>`，里面有若干 `?`。列出三处会让它提前返回 `None` 的失败点。

**参考答案**：(1) `build_texture(image, w, h)?` 返回 `None`（见 4.2，例如 `Pixmap::new` 因尺寸为 0 失败）；(2) `sk::Rect::from_xywh(...)?` 在宽高非法时返回 `None`；(3) 间接地，`build_texture` 内部的 `Pixmap::new(...)?` 也会导致整体返回 `None`。返回 `None` 意味着“这张图不画了”，保守跳过。

---

### 4.2 `build_texture`：三类图像的纹理构建

#### 4.2.1 概念说明

`build_texture(image, w, h)` 的职责很纯粹：**给定一张图像和目标像素尺寸 `w×h`，产出一张 tiny-skia 的 `Pixmap` 纹理**。它按 `ImageKind` 的三类分支处理：

- **`Raster`**：用 `image` 库把光栅图重采样到 `w×h`，再把像素逐个拷进 `Pixmap` 并**预乘 alpha**。
- **`Svg`**：用 `resvg` 直接把 SVG 栅格化进 `w×h` 的 `Pixmap`。
- **`Pdf`**：委托给 `build_pdf_texture`（见 4.3）。

它被 `#[comemo::memoize]` 标注，意味着**相同的 `(image, w, h)` 只会计算一次**——这在一份文档里同一张图被多次放置、或多次导出时极大节省开销（详见 [u3-l4 记忆化与性能](./u3-l4-memoization-perf.md)）。

#### 4.2.2 核心流程

```rust
#[comemo::memoize]
fn build_texture(image, w, h) -> Option<Arc<sk::Pixmap>> {
    let texture = match image.kind() {
        Raster(raster) => {
            新建 w×h 的 Pixmap;
            若 (w,h)==原图尺寸 → 直接用原图（省一次 resize）;
            否则按 scaling 选 FilterType 并 resize_exact;
            逐像素拷贝并 premultiply;
        }
        Svg(svg) => {
            新建 w×h 的 Pixmap;
            resvg::render(tree, 缩放变换, &mut texture);
        }
        Pdf(pdf) => build_pdf_texture(pdf, w, h)?,
    };
    Some(Arc::new(texture))
}
```

返回 `Arc<sk::Pixmap>`：用引用计数共享，缓存命中时多个调用方共用同一块像素内存，零拷贝。

#### 4.2.3 源码精读

函数头与 `comemo` 缓存标注：

[src/image.rs:68-70](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/image.rs#L68-L70) —— `#[comemo::memoize]` 以 `(image, w, h)` 为缓存键。

**光栅分支——重采样滤镜选择（本讲实践重点）**：

[src/image.rs:71-98](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/image.rs#L71-L98) —— 光栅图：先建空 `Pixmap`，按需 `resize_exact`，再逐像素写入。

```rust
let buf;
let dynamic = raster.dynamic();
let resized = if (w, h) == (dynamic.width(), dynamic.height()) {
    dynamic                                    // 不缩放：直接复用，省一次分配
} else {
    let upscale = w > dynamic.width();
    let filter = match image.scaling() {
        Smart::Custom(ImageScaling::Pixelated) => FilterType::Nearest,
        _ if upscale => FilterType::CatmullRom,
        _ => FilterType::Lanczos3,             // downscale
    };
    buf = dynamic.resize_exact(w, h, filter);
    &buf
};

for ((_, _, src), dest) in resized.pixels().zip(texture.pixels_mut()) {
    let Rgba([r, g, b, a]) = src;
    *dest = sk::ColorU8::from_rgba(r, g, b, a).premultiply();
}
```

三个关键点：

1. **跳过 resize 的优化**：当目标尺寸 `(w,h)` 与原图 `dynamic.width()/height()` 完全相等时，直接用原图 `dynamic`，不调用 `resize_exact`。这**省下一次完整的像素重采样（CPU 密集）和一次中间 `DynamicImage` 的内存分配**。在“图像 1:1 原大显示”的常见场景里，这是一项实打实的优化。
2. **三档滤镜**（来自 `image` 库的 `FilterType`）：
   - `Pixelated` → `Nearest`：最近邻，**保留像素化块状外观**（适合像素艺术、低分辨率 icon）。
   - 放大（`upscale`，目标大于原图）→ `CatmullRom`：双三次插值（bicubic），放大时边缘较锐利、过渡平滑。
   - 缩小（`downscale`，目标小于原图）→ `Lanczos3`：Lanczos 加窗 sinc（窗口半径 3），缩小时抗锯齿好、细节保留多、伪影少。
3. **预乘 alpha**：`ColorU8::from_rgba(...).premultiply()`。tiny-skia 内部按**预乘 alpha**（premultiplied alpha）存储像素——即 RGB 已乘以 alpha/255。这是后续 `src-over` 合成（见 [u3-l3](./u3-l3-glyph-raster-blend.md)）的前提，必须在这里就转好。

> 顺带：`image.scaling()` 返回 `Smart<ImageScaling>`。`Smart::Auto`（默认）走 upscale/downscale 的平滑分支；只有用户显式指定 `image.scaling: "pixelated"` 才命中 `Nearest`。

**SVG 分支**：

[src/image.rs:99-108](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/image.rs#L99-L108) —— 用 resvg 把 SVG 栅格化。

```rust
ImageKind::Svg(svg) => {
    let mut texture = sk::Pixmap::new(w, h)?;
    let tree = svg.tree();
    let ts = tiny_skia::Transform::from_scale(
        w as f32 / tree.size().width(),
        h as f32 / tree.size().height(),
    );
    resvg::render(tree, ts, &mut texture.as_mut());
    texture
}
```

SVG 是矢量格式，**不存在“重采样滤镜”问题**——任意分辨率都清晰。这里只算一个 `from_scale`（目标尺寸 / SVG 原始 viewBox 尺寸），交给 resvg 直接画进 `texture`。

**PDF 分支**：一行委托。

[src/image.rs:109](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/image.rs#L109) —— `ImageKind::Pdf(pdf) => build_pdf_texture(pdf, w, h)?,`，细节见 4.3。

最后包成 `Arc`：

[src/image.rs:112](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/image.rs#L112) —— `Some(Arc::new(texture))`，引用计数共享给缓存与调用方。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：精读 `build_texture` 光栅分支的滤镜选择，说清三种 `FilterType` 的适用场景，并解释“跳过 resize”为何是优化。

**操作步骤（源码阅读型）**：

1. 打开 [src/image.rs:78-90](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/image.rs#L78-L90)，把三种情况填进下表：

   | 条件 | 选中的 `FilterType` | 特点 | 适用场景 |
   | --- | --- | --- | --- |
   | `image.scaling()` == `Pixelated` | `Nearest` | 取最近邻像素，块状 | 像素艺术、保留锐利方块 |
   | `upscale`（目标 > 原图） | `CatmullRom` | 双三次插值 | 放大时边缘锐利、平滑 |
   | 其余（即 `downscale`，目标 < 原图） | `Lanczos3` | Lanczos 加窗 | 缩小时抗锯齿、保细节 |

2. 思考并写下：**为什么放大用 CatmullRom、缩小用 Lanczos3？**
   - **CatmullRom（双三次）** 在**放大**时，用 4×4 邻域拟合一条平滑曲线，放大后边缘比双线性更锐利、伪影少，性价比高。
   - **Lanczos3** 在**缩小**时（需要把多个源像素合并成少数目标像素），其负的旁瓣能更好地**抑制摩尔纹（aliasing/moiré）**、保留高频细节，是高质量下采样的标准选择。放大时用 Lanczos 反而容易产生振铃（ringing），所以放大侧选了更温和的 CatmullRom。
   - 一句话：**放大怕糊、缩小怕锯齿**，故各取所长。

3. 解释 **`(w,h) == (dynamic.width(), dynamic.height())` 时跳过 resize 为何是优化**：因为 `resize_exact` 要做一遍全图重采样（`O(w·h)` 浮点卷积）并分配一张新的 `DynamicImage` 缓冲。当目标与原图尺寸一致时，重采样毫无意义（输出 = 输入），直接返回原图既省 CPU 又省内存分配；这一条在“原图 1:1 放进文档”时几乎总是命中。

**需要观察的现象**：若你在本地编译并临时把那条 `if` 去掉、强制总是 `resize_exact`，对一张大图重复渲染多页时，性能会下降、内存分配会增多（可用 `#[typst_macros::time]` 或 `cargo flamegraph` 观察，待本地验证）。

**预期结果**：能复述三档滤镜的判别条件与各自适用场景，并能解释“跳过 resize”省下的是一次重采样与一次分配。

#### 4.2.5 小练习与答案

**练习 1**：为什么 SVG 和 PDF 分支里**没有** `FilterType` 选择逻辑？

**参考答案**：SVG 与 PDF 是**矢量格式**，在任何分辨率下都能无损栅格化，不存在“像素重采样”这一步。它们由 resvg / hayro 直接按目标尺寸重新绘制，所以不需要（也没有）光栅图那种放大/缩小滤镜选择。

**练习 2**：`build_texture` 返回 `Arc<sk::Pixmap>` 而非 `sk::Pixmap`。结合 `#[comemo::memoize]` 说明这样做的意义。

**参考答案**：`memoize` 让相同 `(image, w, h)` 只算一次，并把结果缓存。返回 `Arc` 后，多个调用方（例如同一张图在多页重复出现）可以**共享同一块像素内存**，而不必每次 `clone` 整张 `Pixmap`（那是一次 `O(w·h)` 的深拷贝）。这是“计算一次 + 零拷贝共享”的组合优化。

---

### 4.3 `build_pdf_texture`：用 hayro 栅格化嵌入 PDF

#### 4.3.1 概念说明

Typst 允许把 PDF 作为图像嵌入（`ImageKind::Pdf`）。栅格化 PDF 比栅格化 SVG 麻烦得多，因为 **PDF 可能引用字体**——尤其是 PDF 的 14 种“标准字体”（Helvetica、Courier、Times 等）。这些字体并不内嵌在 PDF 里，渲染器必须自己提供。

`build_pdf_texture` 用 **hayro** 库完成栅格化，并通过一个**字体解析器闭包**把 14 种标准字体映射到 typst 自带的字体资源（`typst_assets::pdf::*`）。源码里有一句重要注释：

```rust
// Keep this in sync with `typst-svg`!
```

意思是 typst-svg 在栅格化嵌入 PDF 时有**几乎相同**的逻辑——两边要一起维护，避免渲染结果不一致。

#### 4.3.2 核心流程

```rust
fn build_pdf_texture(pdf, w, h) -> Option<sk::Pixmap> {
    // 1) 标准字体解析器：StandardFont -> typst_assets 里的字体字节
    let select_standard_font = |font| match font {
        Helvetica => typst_assets::pdf::SANS,
        ... (共 14 种)
    };

    // 2) hayro 解释器设置：标准字体查询 / 回退字体 / 关闭 cmap / 关闭注解
    let interpreter_settings = InterpreterSettings {
        font_resolver: |query| match query {
            Standard(s) => select_standard_font(s),
            Fallback(f) => select_standard_font(f.pick_standard_font()),
        },
        cmap_resolver: |_| None,     // 不启用 hayro 内嵌 cmap（数据量大）
        warning_sink: |_| {},
        render_annotations: false,   // 像打印一样，不画注解
    };

    // 3) 渲染设置：缩放比 + 目标宽高 + 透明背景
    let render_settings = RenderSettings {
        x_scale: w / pdf.width(), y_scale: h / pdf.height(),
        width: Some(w), height: Some(h), bg_color: TRANSPARENT,
    };

    // 4) hayro 渲染得到像素缓冲，再转成 tiny-skia Pixmap
    let cache = RenderCache::new();
    let hayro_pix = hayro::render(pdf.page(), &cache, &interpreter_settings, &render_settings);
    let bytes: Vec<u8> = bytemuck::cast_vec(hayro_pix.take());
    sk::Pixmap::from_vec(bytes, IntSize::from_wh(w, h)?)
}
```

#### 4.3.3 源码精读

**14 种标准字体映射**：

[src/image.rs:117-135](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/image.rs#L117-L135) —— `select_standard_font` 把 PDF 的 `StandardFont` 枚举逐个映射到 `typst_assets::pdf` 里的字体常量（如 `SANS`/`SANS_BOLD`/`FIXED`/`SERIF`/`DING_BATS`/`SYMBOL` 等），返回 `(Arc<bytes>, 0)`。

> PDF 规范定义了 14 种“标准字体”——任何 PDF 阅读器都必须能渲染它们，即便 PDF 本身没内嵌字模。typst 通过 `typst-assets` crate 自带这些字体的数据，于是 hayro 在栅格化时能拿到字形。

**hayro 解释器设置**：

[src/image.rs:137-148](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/image.rs#L137-L148) —— `InterpreterSettings` 配置三件事：

- `font_resolver`：被 hayro 回调以查询字体。`FontQuery::Standard(s)` 直接查标准字体；`Fontallback(f)` 用 `f.pick_standard_font()` 把回退字体也映射到最接近的标准字体（即一律用 14 种标准字体兜底）。
- `cmap_resolver: |_| None`：刻意**不启用 hayro 的内嵌 cmap**。注释解释：内嵌 cmap 数据量巨大，而该场景较冷门，不值当。
- `render_annotations: false`：注释 `// We want to render like it prints`——**像打印那样渲染**，不画 PDF 的注解（链接、批注等），保证栅格化结果干净。

**渲染设置与执行**：

[src/image.rs:150-163](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/image.rs#L150-L163) —— `RenderSettings` 给出 x/y 缩放比与目标宽高、透明背景；`hayro::render` 产出像素缓冲，`bytemuck::cast_vec` 做零拷贝类型转换，最终 `sk::Pixmap::from_vec` 复用这块内存构造成 tiny-skia 画布。

```rust
let render_settings = RenderSettings {
    x_scale: w as f32 / pdf.width(),
    y_scale: h as f32 / pdf.height(),
    width: Some(w as u16),
    height: Some(h as u16),
    bg_color: TRANSPARENT,
};

let cache = RenderCache::new();
let hayro_pix =
    hayro::render(pdf.page(), &cache, &interpreter_settings, &render_settings);

let bytes: Vec<u8> = bytemuck::cast_vec(hayro_pix.take());
sk::Pixmap::from_vec(bytes, IntSize::from_wh(w, h)?)
```

要点：

- `bg_color: TRANSPARENT`：PDF 页背景透明，这样它叠加到文档上时不带一块白底（与 SVG 行为一致）。
- `bytemuck::cast_vec`：hayro 的像素类型与 tiny-skia 的 `u8` 在内存布局上兼容，`cast_vec` **不拷贝数据**、只重新解释类型，把 `Vec<hayro像素>` 变成 `Vec<u8>`，再由 `Pixmap::from_vec` 接管。
- 注意：`build_pdf_texture` **没有** `#[comemo::memoize]` 标注——它由外层 `build_texture`（已 memoize）包裹，所以同一张 PDF 图的栅格化仍会被外层缓存，无需在此重复标注。

#### 4.3.4 代码实践

**实践目标**：理清“PDF 嵌入图”栅格化时字体从哪里来，以及它与 SVG 嵌入图在背景上的共同点。

**操作步骤（源码阅读型）**：

1. 在 [src/image.rs:117-135](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/image.rs#L117-L135) 数一数 `select_standard_font` 覆盖了哪 14 种标准字体（Helvetica 正/粗/斜/粗斜 4 种、Courier 4 种、Times 4 种、ZapfDingBats、Symbol = 14 种）。
2. 跟踪 `FontQuery::Fallback(f)` 分支：它调 `f.pick_standard_font()` 把任意回退字体归类到 14 种之一，再用 `select_standard_font` 取字体字节。这说明 typst-render 栅格化嵌入 PDF 时，**非标准字体也会被强制映射到最接近的标准字体**（因为这里没有接入 typst 自己的字体系统）。
3. 对比 SVG 分支（4.2.3）和 PDF 分支：两者都**不给纹理铺白底**（SVG 默认透明、PDF 用 `TRANSPARENT`）。说明嵌入矢量图叠加到文档时背景都是透明的。

**需要观察的现象**：若一个嵌入 PDF 使用了非标准字体（比如某种中文字体），栅格化结果里该文本可能**被标准字体（如 Times）回退替代**，与原生 PDF 阅读器渲染不一致。

**预期结果**：能说出 14 种标准字体的来源（`typst_assets::pdf`），并解释为何嵌入 PDF 的文本字体可能被替换。本地实际渲染一张含特殊字体的嵌入 PDF 来观察回退「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`build_pdf_texture` 里为什么有 `// Keep this in sync with typst-svg!` 这句注释？

**参考答案**：因为 typst-svg 在导出 SVG 时，若遇到嵌入的 PDF 图，也要用 hayro 把它栅格化（SVG 本身是矢量，但嵌入的 PDF 必须先转成像素再嵌进 SVG）。两边的标准字体映射、解释器设置必须保持一致，否则“同一份文档导出 PNG 与导出 SVG”时嵌入 PDF 的渲染结果会不同。这句注释提醒维护者两处要同步修改。

**练习 2**：`build_pdf_texture` 自己没有 `#[comemo::memoize]`，那它的结果会被缓存吗？

**参考答案**：会。它只被 `build_texture`（[src/image.rs:109](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/image.rs#L109)）调用，而 `build_texture` 上有 `#[comemo::memoize]`。所以以 `(image, w, h)` 为键，整张 PDF 纹理（含 hayro 栅格化）只算一次，后续命中缓存。无需在 `build_pdf_texture` 上重复标注。

---

## 5. 综合实践

把本讲三个模块串起来：**追踪一张光栅图从 `render_frame` 到画布像素的完整旅程**。

1. **入口**：在 [src/lib.rs:198-199](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L198-L199) 找到 `FrameItem::Image(image, size, _)` 的派发：`render_image(canvas, state.pre_translate(*pos), image, *size)`。注意它和 Shape/Text 一样先 `pre_translate(*pos)` 把图像落位。
2. **算分辨率**：在 `render_image` 里，`state.transform`（已含 `pre_translate`、版面变换、`pixel_per_pt`）→ 算出 `theta`、`scale_x`、目标 `w×h`。
3. **建纹理**：`build_texture(image, w, h)` 命中光栅分支，按 `image.scaling()` 与 `upscale/downscale` 选 `Nearest/CatmullRom/Lanczos3`，`resize_exact` 后逐像素 `premultiply` 写进 `Pixmap`；返回 `Arc<Pixmap>`（缓存）。
4. **贴图**：纹理包成 `Pattern`（`Nearest`、`paint_scale`）→ `fill_rect` 贴进显示盒 → 经 `ts` 映射到画布像素。

**给你的小任务**：

- 假设 `pixel_per_pt=2.0`、图像无旋转无缩放（`ts` 退化为纯缩放）、显示盒 `view_width=100pt, view_height=100pt`，原生图 `200×200` 像素。手算：`theta=0`、`scale_x=?`、`w=h=?`、此时 `(w,h)` 是否等于原图尺寸？会不会命中“跳过 resize”优化？`paint_scale_x` 又是多少？
  - 参考答案：`theta=atan2(0, sx)=0`；`prefer_sin` 为假 → `scale_x=|sx/cos0|=|sx|`。因 `pixel_per_pt=2.0` 且无几何缩放，`sx=2.0`，故 `scale_x=2.0`。`aspect=1`，`w=ceil(2·max(100, 1·100))=ceil(200)=200`，`h=200`。`(w,h)==(200,200)` 等于原图 → **命中跳过 resize 优化**。`paint_scale_x=100/200=0.5`。
- 进阶：若把 `pixel_per_pt` 改成 `1.0`，重算 `w` 与是否命中优化，并解释为何此时 `paint_scale_x` 变成 1.0。
  - 参考答案：`scale_x=1.0`，`w=ceil(1·100)=100`，`h=100`。`(100,100)≠(200,200)` → **不命中**，会走 `resize_exact` 缩小（`downscale`）→ `Lanczos3`。`paint_scale_x=100/100=1.0`，纹理 1:1 贴进画布。

## 6. 本讲小结

- `render_image` 的主线是“**先算目标分辨率、提前重采样、再用 Nearest 贴图**”，这是为了让最终像素更锐利（避免 tiny-skia 二次插值）。
- 目标分辨率由**坐标变换**推算：`theta=atan2(-kx,sx)` 还原旋转角，`scale_x` 用“离 0 更远”的 sin/cos 做分母以**避免除零**。
- `build_texture` 被 `#[comemo::memoize]` 缓存，返回 `Arc<Pixmap>` 实现零拷贝共享；它按 `Raster/Svg/Pdf` 三类分支构建纹理。
- 光栅分支的重采样滤镜三档：`Pixelated→Nearest`、放大→`CatmullRom`、缩小→`Lanczos3`；目标尺寸等于原图时**跳过 resize** 省下一次重采样和一次分配。
- SVG/PDF 是矢量，无需重采样滤镜；PDF 由 hayro 栅格化，**14 种标准字体**由 `typst_assets::pdf` 提供，背景透明，结果与 typst-svg 需保持同步。

## 7. 下一步学习建议

- 想搞清“纹理像素如何与画布像素做 alpha 合成”，请继续阅读 [u3-l3 字形光栅化与像素级混合](./u3-l3-glyph-raster-blend.md)，其中 `blend_src_over` / `alpha_mul` 会展开预乘 alpha 的 `src-over` 公式——本讲里 `premultiply()` 的动机就在那里。
- 想了解 `#[comemo::memoize]` 的缓存键细节（为何 `build_texture` 能跨页命中、`rasterize` 为何用 `f32::to_bits` 做键），请阅读 [u3-l4 记忆化与性能优化](./u3-l4-memoization-perf.md)。
- 建议同步对照 typst-svg 里栅格化嵌入 PDF 的对应代码，体会“Keep this in sync”这句注释背后的双端维护约定。

# 纯色 Paint 转换

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `to_sk_paint` 的整体职责：把 Typst 的 `Paint`（纯色 / 渐变 / 平铺）转换成 tiny-skia 的 `sk::Paint`，并解释它的六个参数（`paint`、`state`、`on_text`、`pixmap`、`shape`、`include_stroke_in_bbox`）各自的作用。
- 解释为什么函数开头那段「根据 `shape` 计算 `item_size` / `fill_transform` / `gradient_map`」的代码对纯色分支完全没用——却仍要提前算出来。
- 复述 `Paint::Solid` 分支只有两行：`set_color` + `anti_alias = true`，并说明它为何如此简单。
- 跟踪 `to_sk_color` 与 `to_sk_color_u8` 两条颜色转换链，讲清它们的精度差异（f32 vs u8）与「谁调用谁」。
- 解释 `ProcessColor` 的角色：它把 Typst 用户可能用到的任意色彩空间（Oklab、CMYK、HSL……）统一归一化到 sRGB，是 `Color` 走向像素之前的「最后一道关卡」。

本讲承接 u2-l1 建立的 `State` 与 `AbsExt`（把 `Abs` 统一转成 pt-f32），以及 u2-l2 的 `render_group`。从本讲起，我们开始进入「可见叶子节点」如何被画出来——而**任何**形状或文本，第一步都是「把它的填充/描边 `Paint` 转成 tiny-skia 能理解的画笔」。本讲只讲其中最简单的 `Paint::Solid`（纯色）；渐变（u3-l1）与平铺（u3-l2）留到专家层。

## 2. 前置知识

在进入本讲前，你需要先掌握（来自前置讲义）：

- **State 背包**：渲染递归中随身携带的不可变状态，含 `transform`、`container_transform`、`mask`、`pixel_per_pt`、`size` 五个字段；本讲主要用到 `state.transform` 与 `state.pixel_per_pt`（见 u2-l1）。
- **AbsExt**：`Abs::to_f32()` 把任意 Typst 长度换算成「以 pt 为单位的 f32」，是 Typst 长度与 tiny-skia 浮点数之间唯一的单位关卡（见 u2-l1）。
- **Frame 场景树**：`Shape` 与 `Text` 是叶子节点，由 `render_frame` 派发给 `shape::render_shape` / `text::render_text`（见 u1-l3）。

本讲会用到的两个新直觉：

- **`Paint` 是「用什么填」，`Shape` 是「填在哪里」**：Typst 把图形拆成「几何（Geometry）」与「颜料（Paint）」两个正交概念。`render_shape` 先把几何转成 tiny-skia 路径（`sk::Path`），再把颜料转成 tiny-skia 画笔（`sk::Paint`），最后让 tiny-skia 用画笔去填路径。`to_sk_paint` 只负责「颜料 → 画笔」这一半。
- **颜色在不同环节有不同的「最佳形态」**：交给 tiny-skia 高层 API（`canvas.fill`、`sk::Paint::set_color`）时用「f32、直通 alpha（straight alpha）、范围 \([0,1]\)」的 `sk::Color`；而在需要逐像素手动混合的地方（字形位图）则用「u8、预乘 alpha、范围 \([0,255]\)」的 `sk::PremultipliedColorU8`。typst-render 提供了 `to_sk_color` 与 `to_sk_color_u8` 两个函数分别产出这两种形态。

## 3. 本讲源码地图

本讲的核心几乎全在一个文件里：

| 文件 | 作用 |
| --- | --- |
| `crates/typst-render/src/paint.rs` | `to_sk_paint`（Paint 转换总入口）、`to_sk_color` / `to_sk_color_u8`（颜色转换）、`PaintSampler` trait |
| `crates/typst-render/src/shape.rs` | `render_shape`：`to_sk_paint` 的两个主要调用点（填充与描边） |
| `crates/typst-render/src/lib.rs` | `render` / `render_merged`：页面背景填充对 `to_sk_color` 的直接调用 |
| `crates/typst-library/src/visualize/paint.rs` | `Paint` 枚举（`Solid` / `Gradient` / `Tiling`）的定义 |
| `crates/typst-library/src/visualize/color.rs` | `Color`、`ProcessColor`、`to_process`、`to_rgb` 的定义 |

只读地看，本讲的主线就是 `paint.rs` 里 `to_sk_paint` 的前 60 行（参数 + 公共计算 + `Paint::Solid` 分支）加上末尾两个 4 行的颜色转换函数。

## 4. 核心概念与源码讲解

### 4.1 to_sk_paint：Paint 到 tiny-skia Paint 的总转换器

#### 4.1.1 概念说明

tiny-skia 不知道 Typst 的 `Paint` 是什么。它只认自己定义的 `sk::Paint`——一个「画笔」对象，描述「用什么颜色/图案去填或描边」。`to_sk_paint` 就是这两者之间的翻译器：

- 输入：Typst 的 `Paint`（可能是纯色、渐变或平铺图案）。
- 输出：tiny-skia 的 `sk::Paint`，可以直接交给 `canvas.fill_path(..., &paint, ...)` 或 `canvas.stroke_path(..., &paint, ...)`。

这个翻译器要同时服务三类调用场景：

1. **形状的填充**（`render_shape` 里 `shape.fill`）。
2. **形状的描边**（`render_shape` 里 `shape.stroke.paint`）。
3. **文本的字形**（`render_text`，渐变/平铺落在文字上时）。

这三种场景对「坐标系」「是否需要描边参与边界框」「是否画在文字上」都有细微差别，所以 `to_sk_paint` 用一组长参数把这些差别一次性表达出来。

#### 4.1.2 核心流程

`to_sk_paint` 的骨架可以概括为「先算公共量，再按 `Paint` 变体三分支」：

```
to_sk_paint(paint, state, on_text, pixmap, shape, include_stroke_in_bbox):
  ┌─ 第 0 步：根据 shape 算三个公共量（仅渐变/平铺会用）─────────┐
  │ if let Some(shape) = shape:                                  │
  │   bbox            = shape.bbox(include_stroke_in_bbox)        │
  │   fill_transform  = from_translate(bbox.min.x, bbox.min.y)    │
  │   gradient_map    = 矩形负尺寸镜像（非矩形则 None）           │
  │   → (item_size, Some(fill_transform), gradient_map)           │
  │ else:                                                         │
  │   → (Size::zero(), None, None)                                │
  └───────────────────────────────────────────────────────────────┘
  sk_paint = sk::Paint::default()
  match paint:
    ┌─ Solid(color) ────────── 仅设 color + anti_alias（本讲主角）
    ├─ Gradient(gradient) ─── 采样成一张 pixmap，包成 Pattern shader（u3-l1）
    └─ Tiling(tilings)  ───── 把图案帧渲染成 pixmap，包成 Pattern shader（u3-l2）
  return sk_paint
```

关键点：第 0 步那三个公共量（`item_size` / `fill_transform` / `gradient_map`）**只有渐变和平铺分支会读取**。对纯色分支而言，它们是「算了也白算」的无效开销——但因为它们依赖 `shape`，而三个分支共用同一个 `match`，写在外面能避免在渐变/平铺分支里重复写一遍 `if let Some(shape)`。这是「以纯色的一点小开销换代码结构清晰」的取舍。

#### 4.1.3 源码精读

函数签名与六个参数：[crates/typst-render/src/paint.rs:L139-L146](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L139-L146) —— `to_sk_paint` 的完整签名。

逐个参数含义：

| 参数 | 类型 | 作用 |
| --- | --- | --- |
| `paint` | `&Paint` | 要转换的颜料（Solid/Gradient/Tiling） |
| `state` | `State` | 当前渲染状态（提供 `transform`、`pixel_per_pt`、`size`、`container_transform`） |
| `on_text` | `bool` | 是否用于文字。影响渐变/平铺的默认 `RelativeTo`；对纯色无影响 |
| `pixmap` | `&mut Option<Arc<sk::Pixmap>>` | **输出参数**：渐变/平铺把采样好的纹理写进这里，让返回的 `sk::Paint` 的 shader 能引用它（保证生命周期） |
| `shape` | `Option<&Shape>` | 若来自 `render_shape`，传入形状以算边界框；来自文本时为 `None` |
| `include_stroke_in_bbox` | `bool` | 算边界框时是否把描边宽度算进去（描边 + 渐变时为 `true`） |

第 0 步「根据 shape 算公共量」：[crates/typst-render/src/paint.rs:L176-L194](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L176-L194) —— 这段是后续两讲（渐变/平铺）的关键，本讲只需记住三件事：

1. `bbox` 来自 [crates/typst-library/src/visualize/shape.rs:L348-L351](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/shape.rs#L348-L351) 的 `Shape::bbox(include_stroke)`——形状的轴对齐包围盒。
2. `fill_transform` 是一个**纯平移**，平移量是包围盒左上角 `bbox.min`（单位 pt，经 `AbsExt::to_f32`）。
3. `gradient_map` 只对 `Geometry::Rect` 非 `None`，用于「矩形负尺寸时把渐变镜像」，正常正向矩形得到的也是恒等映射。`_ => None` 表示线段、曲线等非矩形一律 `None`。

注意 `else` 分支（`shape` 为 `None`，即文本路径）：[crates/typst-render/src/paint.rs:L192-L194](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L192-L194) —— 三个量退化为 `(Size::zero(), None, None)`，文本场景不依赖形状边界框。

`match paint` 的三分支入口：[crates/typst-render/src/paint.rs:L196-L201](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L196-L201) —— 先 `sk::Paint::default()` 建空画笔，再按变体填充。

`to_sk_paint` 的两个主要调用点都在 `render_shape` 里：

- 填充调用：[crates/typst-render/src/shape.rs:L39-L42](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/shape.rs#L39-L42) —— `to_sk_paint(fill, state, false, &mut pixmap, Some(shape), false)`。注意最后一个参数 `false`：**填充时不把描边算进边界框**。
- 描边调用：[crates/typst-render/src/shape.rs:L64-L71](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/shape.rs#L64-L71) —— 这里 `include_stroke_in_bbox` 取 `matches!(paint, Paint::Gradient(_))`，即**仅当描边颜料是渐变时才扩边界框**（纯色描边不需要）。

#### 4.1.4 代码实践

> **实践目标**：跟踪一次纯色填充调用，看清「第 0 步公共量」实际算出了什么，并确认它们在纯色分支里被丢弃。

**操作步骤**：

1. 打开 [crates/typst-render/src/shape.rs:L39-L53](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/shape.rs#L39-L53)，定位填充调用 `to_sk_paint(fill, state, false, &mut pixmap, Some(shape), false)`。
2. 跟着跳进 [crates/typst-render/src/paint.rs:L176-L194](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L176-L194)。因为 `shape = Some(shape)`，进入 `if let Some(shape)` 分支：
   - `bbox = shape.bbox(false)`（`include_stroke_in_bbox = false`，故不含描边）。
   - `fill_transform = Transform::from_translate(bbox.min.x.to_f32(), bbox.min.y.to_f32())`——一个平移到包围盒左上角的变换。
   - `gradient_map`：若几何是 `Rect` 则为 `Some((偏移, 缩放))`，否则 `None`。
   - 返回 `(bbox.size(), Some(fill_transform), gradient_map)`。
3. 接着进入 `match paint` 的 `Paint::Solid` 分支（下一节细讲），确认它**完全没有引用** `item_size`、`fill_transform`、`gradient_map` 这三个变量。

**需要观察的现象**：这三个变量在 `Paint::Solid` 分支里是否出现（提示：不会）。

**预期结果**：对纯色而言，第 0 步算出的 `item_size`/`fill_transform`/`gradient_map` 是「死计算」，编译器甚至可能给出未使用警告以外的优化；但因它们对 `Gradient`/`Tiling` 分支必需，故仍统一在外层计算。`pixmap` 也保持调用方传入的 `None` 不变（纯色不需要纹理）。

#### 4.1.5 小练习与答案

**练习 1**：`pixmap` 参数为什么是 `&mut Option<Arc<sk::Pixmap>>` 而不是直接返回 `sk::Paint`？

**参考答案**：因为 `sk::Paint` 的 `shader` 字段持有的是**引用**（`sk::Pattern` 借用了一张 `Pixmap`）。渐变/平铺分支需要先采样出一张纹理 pixmap，再让返回的画笔引用它。如果 pixmap 是函数局部变量，函数返回后画笔就成了悬垂引用。所以让调用方传入一个 `&mut Option<Arc<Pixmap>>` 作为「输出槽」：函数把纹理 `Arc` 写进去，画笔引用它，二者生命周期绑定到调用方栈帧上。纯色分支不写 `pixmap`，它保持 `None`。

**练习 2**：为什么填充调用传 `include_stroke_in_bbox = false`，而描边调用有时传 `true`？

**参考答案**：边界框用来决定渐变/平铺的采样区域大小。填充只覆盖几何本身，故用纯几何边界框（`false`）；描边会把线条加粗到几何之外，若描边颜料是渐变，希望渐变覆盖到「描边后的完整区域」，故对渐变描边传 `true`。纯色描边不需要采样区域，传 `false` 也无妨（`matches!(paint, Paint::Gradient(_))` 为 `false`）。

---

### 4.2 Paint::Solid：纯色分支的极简实现

#### 4.2.1 概念说明

`Paint::Solid(Color)` 表示「用一种单一颜色平铺整片区域」。对光栅渲染来说，这是最朴素的颜料——没有方向、没有图案、没有渐变，每个像素都取同一个颜色。因此它的转换逻辑短到几乎没有内容：

- 把 `Color` 转成 tiny-skia 的 `sk::Color`；
- 打开抗锯齿。

仅此而已。`sk::Paint::default()` 已经把画笔预设成「纯色填充」模式，所以只要 `set_color` 就够；渐变和平铺那种「采样纹理 → 包成 shader」的复杂流程，纯色一律不需要。

#### 4.2.2 核心流程

```
Paint::Solid(color):
  sk_paint.set_color(to_sk_color(color.to_process()))   // Color → ProcessColor → sk::Color
  sk_paint.anti_alias = true                             // 开启抗锯齿
  // 不动 pixmap、不设 shader、不读 item_size/fill_transform/gradient_map
```

这里出现两次「转换」：

1. `color.to_process()`：`Color` → `ProcessColor`（把「专色 Spot」拍扁成「处理色」）。
2. `to_sk_color(...)`：`ProcessColor` → `sk::Color`（任意色彩空间归一化到 sRGB f32）。

这两步在 4.3、4.4 两节展开。本节只需记住：**纯色分支的全部输出就是一个颜色值加一个布尔标志**。

#### 4.2.3 源码精读

纯色分支全貌：[crates/typst-render/src/paint.rs:L198-L201](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L198-L201) —— 真的就只有这两行：

```rust
Paint::Solid(color) => {
    sk_paint.set_color(to_sk_color(color.to_process()));
    sk_paint.anti_alias = true;
}
```

对比紧随其后的渐变分支：[crates/typst-render/src/paint.rs:L202-L247](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L202-L247) —— 渐变分支要算 `container_size`、`fill_transform`、`gradient_map`、纹理宽高，调用记忆化的 `cached` 采样，再包成 `sk::Pattern` shader，整整 45 行。纯色分支的 2 行 vs 渐变分支的 45 行，直观体现了「纯色是渐变的退化特例」。

一个有趣的细节：矩形填充会在 `render_shape` 里**专门关掉抗锯齿**——[crates/typst-render/src/shape.rs:L44-L46](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/shape.rs#L44-L46)：

```rust
if matches!(shape.geometry, Geometry::Rect(_)) {
    paint.anti_alias = false;
}
```

也就是说，`to_sk_paint` 内部把纯色的 `anti_alias` 设为 `true`，但调用方 `render_shape` 对矩形又改回 `false`。原因：轴对齐矩形的边缘本就是水平/垂直线，开抗锯齿反而会让 crisp 的像素边界变模糊。这是「调用方按几何微调画笔」的一个典型例子。

页面背景填充是纯色的另一条快速路径：[crates/typst-render/src/lib.rs:L34-L41](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L34-L41) —— 渲染整页背景时，若背景是纯色，直接调 `canvas.fill(to_sk_color(...))`（一次整画布填充），**根本不走 `to_sk_paint`**；只有渐变背景才会构造一个 `Geometry::Rect(size).filled(fill)` 形状绕道 `render_shape`。`render_merged` 的 `fill` 同理：[crates/typst-render/src/lib.rs:L67-L69](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L67-L69)。

#### 4.2.4 代码实践

> **实践目标**：验证「纯色分支只设置 `set_color` 与 `anti_alias`，不触碰 `pixmap` 与三个公共量」。

**操作步骤**：

1. 打开 [crates/typst-render/src/paint.rs:L196-L201](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L196-L201)。
2. 逐行核对 `Paint::Solid` 分支体内出现的标识符：`sk_paint`、`to_sk_color`、`color`、`anti_alias`。
3. 反向核对：分支体内是否出现 `item_size`、`fill_transform`、`gradient_map`、`pixmap`、`shader`？（应均未出现。）

**需要观察的现象**：分支体只有两个语句，且都是对 `sk_paint` 的赋值/设置。

**预期结果**：确认纯色分支的输出完全由「一个 `sk::Color` + `anti_alias = true`」决定；`pixmap` 维持调用方的初始值（在 `render_shape` 里是 `None`）。这意味着返回的 `sk::Paint` 没有 shader，tiny-skia 会把它当作「单色填充」处理。

#### 4.2.5 小练习与答案

**练习 1**：既然 `to_sk_paint` 内部已设 `anti_alias = true`，为什么矩形还要在 `render_shape` 里改回 `false`？

**参考答案**：轴对齐矩形的四条边是纯水平/垂直线，能整像素对齐，不需要抗锯齿；开启反而会在线条边缘引入半透明像素，使原本锐利的边界变「糊」。所以对 `Geometry::Rect` 特例关闭。

**练习 2**：页面背景是纯色时为什么可以跳过 `to_sk_paint`？

**参考答案**：`canvas.fill(color)` 是 tiny-skia 提供的「用一种颜色填满整张画布」的原语，直接接收 `sk::Color`，比「构造矩形形状 + 生成画笔 + 填路径」快得多且等价。所以 `render` 对纯色背景走 `canvas.fill(to_sk_color(...))` 快路径，仅对渐变背景才构造形状走 `render_shape`。

---

### 4.3 to_sk_color 与 to_sk_color_u8：颜色通道的精度转换

#### 4.3.1 概念说明

`Paint::Solid` 分支调用的 `to_sk_color`，以及渐变采样器调用的 `to_sk_color_u8`，是两个并列的「颜色转换出口」。它们都接收 `ProcessColor`，都把它归一化到 sRGB，但产出的**数据类型与精度**不同，服务于两条不同的消费链路：

| 函数 | 输出类型 | 通道 | 范围 | alpha 形态 | 主要消费者 |
| --- | --- | --- | --- | --- | --- |
| `to_sk_color` | `sk::Color` | f32 | \([0,1]\) | 直通（straight） | tiny-skia 高层 API（`set_color`、`canvas.fill`） |
| `to_sk_color_u8` | `sk::ColorU8` | u8 | \([0,255]\) | 直通 | 调用方再 `.premultiply()` 得 `PremultipliedColorU8`，供逐像素手动混合 |

**直通 vs 预乘 alpha**：直通 alpha 把颜色与透明度分开存 \((r,g,b,a)\)；预乘 alpha 把颜色预先乘以透明度存 \((ra, ga, ba, a)\)。预乘的好处是合成公式更简单。src-over 合成（预乘形式）为：

\[
C_{\text{out}} = C_{\text{src}} + C_{\text{dst}} \cdot (1 - \alpha_{\text{src}})
\]

其中 \(C\) 表示预乘后的 RGB 三元组。tiny-skia 的逐像素位图混合（`blend_src_over`）就建立在预乘 u8 之上（详见 u3-l3）；而它的高层 `fill`/`stroke` API 接收直通 `sk::Color`，内部自己完成预乘。

#### 4.3.2 核心流程

两条链路的差别只在最后一步：

```
to_sk_color(ProcessColor):                to_sk_color_u8(ProcessColor):
  c = color.to_rgb()          // → sRGB f32 RGBA    c = color.to_rgb()          // → sRGB f32 RGBA
  (r,g,b,a) = c.into_components()  // f32, [0,1]    (r,g,b,a) = c.into_format::<u8,u8>().into_components()  // u8, [0,255]
  sk::Color::from_rgba(r,g,b,a)                    sk::ColorU8::from_rgba(r,g,b,a)
```

`into_components()` 拆出通道；`into_format::<u8, u8>()` 先把 f32 通道量化成 u8 再拆。两者都来自 `palette` crate（`Rgb` 本是 `palette::rgb::Rgba<encoding::Srgb, f32>` 的别名），但 typst-render 把它当不透明类型用，只关心「`to_rgb()` 之后能拿到 \((r,g,b,a)\)」。

#### 4.3.3 源码精读

两个函数并排位于 `paint.rs` 末尾：

- `to_sk_color`：[crates/typst-render/src/paint.rs:L285-L289](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L285-L289)

  ```rust
  pub fn to_sk_color(color: ProcessColor) -> sk::Color {
      let (r, g, b, a) = color.to_rgb().into_components();
      sk::Color::from_rgba(r, g, b, a)
          .expect("components must always be in the range [0..=1]")
  }
  ```

  `to_rgb()` 保证返回 sRGB 空间下 \([0,1]\) 内的 f32，故 `from_rgba` 的 `expect` 不会触发。

- `to_sk_color_u8`：[crates/typst-render/src/paint.rs:L291-L294](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L291-L294)

  ```rust
  pub fn to_sk_color_u8(color: ProcessColor) -> sk::ColorU8 {
      let (r, g, b, a) = color.to_rgb().into_format::<u8, u8>().into_components();
      sk::ColorU8::from_rgba(r, g, b, a)
  }
  ```

`to_sk_color` 的调用者：`Paint::Solid` 分支（本讲）、页面背景快路径 `render`/`render_merged`（[crates/typst-render/src/lib.rs:L36](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L36)、[L68](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L68)），以及渐变缓存的内部采样（`cached` 里 [crates/typst-render/src/paint.rs:L169](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L169)）。

`to_sk_color_u8` 的调用者：`GradientSampler::sample` 与 `TilingSampler::sample`——这两个采样器为「文字字形逐像素采样」服务，需要 u8 预乘值。例如 [crates/typst-render/src/paint.rs:L69-L78](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L69-L78)：

```rust
to_sk_color_u8(
    self.gradient.sample_at((point.x, point.y), ...).to_process(),
)
.premultiply()
```

注意末尾的 `.premultiply()`——`to_sk_color_u8` 产出的是**直通** `ColorU8`，由调用方按需 `.premultiply()` 转成 `PremultipliedColorU8`，实现 `PaintSampler` trait（trait 要求返回 `sk::PremultipliedColorU8`，见 [crates/typst-render/src/paint.rs:L13-L16](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L13-L16)）。

#### 4.3.4 代码实践

> **实践目标**：把两个函数的调用者分类，理解「f32 直通 vs u8 预乘」的分工。

**操作步骤**：

1. 在 `crates/typst-render/src` 下搜索 `to_sk_color(` 与 `to_sk_color_u8(` 的所有调用点。
2. 为每个调用点标注：它把结果交给「tiny-skia 高层 API」还是「逐像素手动混合」。

**需要观察的现象**：`to_sk_color` 全部出现在「交给 tiny-skia 画」的场合（`set_color`、`canvas.fill`、渐变 `cached` 写纹理像素）；`to_sk_color_u8` 全部出现在采样器 `sample()` 里，且后跟 `.premultiply()`。

**预期结果**（待本地验证调用点列表）：`to_sk_color` 调用点——`paint.rs` Solid 分支、`paint.rs` `cached`、`lib.rs` `render` 背景、`lib.rs` `render_merged` 背景；`to_sk_color_u8` 调用点——`GradientSampler::sample`、`TilingSampler::sample`。两者职责泾渭分明：前者「成片填色」，后者「逐像素取色再手动混合」。

#### 4.3.5 小练习与答案

**练习 1**：为什么不直接让 `to_sk_color_u8` 内部就 `.premultiply()` 返回 `PremultipliedColorU8`？

**参考答案**：因为 `cached`（渐变纹理采样）里写 pixmap 像素时用的是 `to_sk_color(...).premultiply().to_color_u8()`（[L169](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L169)），它需要「直通 u8」中间形态；而文字采样器需要 `PremultipliedColorU8`。两个消费者对「是否预乘」需求不同，故 `to_sk_color_u8` 只负责「量化到 u8」，是否预乘留给调用方决定，复用性更好。

**练习 2**：`to_sk_color` 末尾 `.expect("components must always be in the range [0..=1]")` 在什么情况下会 panic？

**参考答案**：当 `from_rgba` 收到超出 \([0,1]\) 的 f32 分量时。但因为输入来自 `ProcessColor::to_rgb()`，它保证输出是合法 sRGB 值，所以实践中不会触发；这行 `.expect` 是一道「不变量断言」，表达「我相信上游契约」。

---

### 4.4 ProcessColor：从 Color 到可渲染的「处理色」

#### 4.4.1 概念说明

Typst 用户可以用很多色彩空间写颜色：Oklab、Oklch、CMYK、HSL、HSV、线性 RGB、Luma……但 tiny-skia 只懂 sRGB。所以在交给 `to_sk_color` 之前，需要一步「归一化」：

- `Color`（用户层，可能是 `Process(ProcessColor)` 或 `Spot(SpotColor)` 专色）→ `Color::to_process()` → `ProcessColor`（拍扁专色）。
- `ProcessColor::to_rgb()` → `Rgb`（任意处理色空间 → sRGB f32）。

`ProcessColor` 因此是「Typst 色彩模型与渲染器之间的统一中间表示」：它枚举了所有「有明确定义、可互相转换」的色彩空间（不含专色）。`to_rgb()` 是它的「出口」——无论原来是哪种空间，都转成 tiny-skia 能直接用的 sRGB。

为什么渲染要调 `to_process()` 而非直接用 `Color`？因为 `Color::Spot`（专色，比如印刷用的潘通色）在光栅图像里无法表达，必须换成它的「回退（fallback）」处理色。`to_process()` 就是这步「专色 → 处理色」的拍扁。

#### 4.4.2 核心流程

```
用户写: Color (Process 或 Spot)
   │  Color::to_process()
   ▼
ProcessColor (Luma | Oklab | Oklch | Rgb | LinearRgb | Cmyk | Hsl | Hsv)
   │  ProcessColor::to_rgb()
   ▼
Rgb  (= palette sRGB f32 RGBA)
   │  into_components() / into_format::<u8,u8>()
   ▼
(r, g, b, a)  →  sk::Color (f32)  或  sk::ColorU8 (u8)
```

#### 4.4.3 源码精读

`Paint` 枚举：[crates/typst-library/src/visualize/paint.rs:L10-L17](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/paint.rs#L10-L17) —— `Solid(Color)`、`Gradient(Gradient)`、`Tiling(Tiling)` 三变体。`Solid` 内层是 `Color`。

`Color` 枚举：[crates/typst-library/src/visualize/color.rs:L282-L286](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/color.rs#L282-L286) —— `Process(ProcessColor)` 或 `Spot(SpotColor)`。

`Color::to_process`：[crates/typst-library/src/visualize/color.rs:L1383-L1388](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/color.rs#L1383-L1388) —— 关键逻辑：`Process(c)` 直接返回 `c`；`Spot(c)` 返回 `c.fallback()`（专色换回退色）。这就是「渲染无法表达专色，必须降级」的实现。

`ProcessColor` 枚举：[crates/typst-library/src/visualize/color.rs:L1434-L1451](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/color.rs#L1434-L1451) —— 八种处理色空间，全部 32 位。

`ProcessColor::to_rgb`：[crates/typst-library/src/visualize/color.rs:L1810-L1821](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/color.rs#L1810-L1821) —— 逐变体转换：`Rgb` 原样返回，`LinearRgb` 经 `from_linear`（伽马校正），`Cmyk` 先 `to_rgba`（用 ICC profile），其余用 `from_color`。无论哪条路径，出口都是 sRGB。

于是 `Paint::Solid` 分支里的 `color.to_process()` 一句，实际串联了「专色拍扁 → 色彩空间归一化」两步语义。这也是为什么 `to_sk_color` / `to_sk_color_u8` 的入参类型是 `ProcessColor` 而非 `Color`：渲染层只关心「已经拍扁、可归一化」的处理色。

#### 4.4.4 代码实践

> **实践目标**：跟踪一个 CMYK 纯色从 `Color` 走到 `sk::Color` 的完整转换链。

**操作步骤**：

1. 假设有一个 CMYK 纯色填充：在 Typst 源码里它先是 `Color::Process(ProcessColor::Cmyk(..))`，被包成 `Paint::Solid(color)`。
2. 进入 [crates/typst-render/src/paint.rs:L199](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L199)：`color.to_process()` —— 因为已是 `Process`，直接得到 `ProcessColor::Cmyk(..)`（不触发专色回退）。
3. 进入 [crates/typst-render/src/paint.rs:L286](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L286)：`color.to_rgb()` 命中 [crates/typst-library/src/visualize/color.rs:L1817](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/color.rs#L1817) 的 `Cmyk(c) => Rgb::from_color(c.to_rgba())` 分支——这里 `to_rgba` 借助静态 ICC profile（[crates/typst-library/src/visualize/color.rs:L36-L38](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/color.rs#L36-L38)）完成 CMYK→RGB。
4. 最终 `into_components()` 拆出 \((r,g,b,a)\)，`from_rgba` 得到 `sk::Color`。

**需要观察的现象**：转换链跨越了 typst-render 与 typst-library 两个 crate；typst-render 只调 `to_process()` / `to_rgb()` 两个高层方法，色彩空间细节全在 typst-library 内部消化。

**预期结果**：渲染层无需关心用户最初用的是哪种色彩空间——`ProcessColor::to_rgb()` 把所有差异抹平到 sRGB。

#### 4.4.5 小练习与答案

**练习 1**：若用户用了一个 `Color::Spot`（专色）做纯色填充，渲染时会发生什么？

**参考答案**：`Paint::Solid` 分支调 `color.to_process()`，命中 [color.rs:L1386-L1387](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/color.rs#L1386-L1387) 的 `Spot(c) => c.fallback()`，得到专色的回退处理色，后续与普通处理色无异。也就是说，PNG 渲染**无法保留专色**，只能画它的回退近似。

**练习 2**：为什么 `to_sk_color` 的入参是 `ProcessColor` 而不是 `Color`？

**参考答案**：渲染层只应处理「已确定可归一化」的颜色。把「拍扁专色」这一步显式留在调用点（`color.to_process()`），让 `to_sk_color` 的职责更纯粹——只做「色彩空间归一化 + 类型适配」。这也让 `to_sk_color` 能被那些天然就是 `ProcessColor` 的场合（如渐变 `sample_at().to_process()`）直接复用。

---

## 5. 综合实践

> **任务**：把本讲四个最小模块串起来，完整复述「一个纯色形状从 `render_shape` 到画布像素」的颜料侧链路，并标注每一步对应的源码位置。

请按下列顺序填写一张「颜料转换流水线」表（先自己写，再对照源码核对）：

| 步骤 | 发生地 | 输入 → 输出 | 关键源码行 |
| --- | --- | --- | --- |
| 1. 发起填充 | `render_shape` | `shape.fill = Paint::Solid(color)` → 调 `to_sk_paint` | shape.rs L39-L42 |
| 2. 算公共量（被丢弃） | `to_sk_paint` 第 0 步 | `Some(shape)` → `(item_size, fill_transform, gradient_map)` | paint.rs L176-L194 |
| 3. 进入纯色分支 | `to_sk_paint` | `Paint::Solid(color)` → `set_color` + `anti_alias` | paint.rs L198-L201 |
| 4. 拍扁专色 | `Color::to_process` | `Color` → `ProcessColor` | color.rs L1383-L1388 |
| 5. 归一化色彩空间 | `ProcessColor::to_rgb` | `ProcessColor` → `Rgb`（sRGB f32） | color.rs L1810-L1821 |
| 6. 适配 tiny-skia 类型 | `to_sk_color` | `Rgb` → `sk::Color` | paint.rs L285-L289 |
| 7. （矩形特例）关抗锯齿 | `render_shape` | `anti_alias = false` | shape.rs L44-L46 |
| 8. 实际填充 | `canvas.fill_path` | `sk::Path` + `sk::Paint` + `ts` + `mask` → 像素 | shape.rs L52 |

**进阶追问**（用文字回答即可）：

1. 如果把这个形状的 `fill` 从 `Paint::Solid` 换成 `Paint::Gradient`，上表中哪几步会变？（提示：第 2 步的公共量不再被丢弃；第 3 步变成 45 行的渐变分支；新增「采样纹理 → Pattern shader」。）
2. 第 2 步「被丢弃的公共量」对纯色是否真的是零成本？（提示：是几乎零成本的小常数计算，但不是编译期消除——`bbox`、`fill_transform` 在运行期确实会被算出来。）

## 6. 本讲小结

- `to_sk_paint` 是 Paint→`sk::Paint` 的总翻译器，用六个参数（`paint`/`state`/`on_text`/`pixmap`/`shape`/`include_stroke_in_bbox`）统一服务形状填充、形状描边、文本三种场景。
- 它先「根据 `shape` 算出 `item_size`/`fill_transform`/`gradient_map`」三个公共量，但**只有渐变和平铺分支会读它们**；纯色分支把它们算出来后直接丢弃。
- `Paint::Solid` 分支极简：只有 `set_color(to_sk_color(color.to_process()))` 与 `anti_alias = true` 两行；矩形会在调用方 `render_shape` 里把 `anti_alias` 改回 `false`。
- `to_sk_color` 产出 f32 直通 `sk::Color`（喂 tiny-skia 高层 API）；`to_sk_color_u8` 产出 u8 直通 `sk::ColorU8`，由调用方 `.premultiply()` 得到逐像素手动混合用的预乘值。
- `ProcessColor` 是渲染前的统一中间表示；`Color::to_process()` 把专色拍扁，`ProcessColor::to_rgb()` 把任意色彩空间归一化到 sRGB——typst-render 只调这两个高层方法，色彩空间细节全在 typst-library 内部消化。

## 7. 下一步学习建议

- **继续向下读形状渲染**：本讲的 `to_sk_paint` 是 `render_shape` 的「颜料半」，下一讲 u2-l4「几何形状与描边」讲「几何半」——`Geometry`（Line/Rect/Curve）如何转 `sk::Path`、描边参数如何映射、虚线为何要偶数化。
- **进入文本渲染**：u2-l6「文本渲染基础」会用到本讲的 `PaintSampler` trait 与 `to_sk_color_u8`——理解字形如何采样渐变/平铺颜色。
- **专家层的「为什么」**：当你想搞清 `to_sk_color_u8` 产出的预乘 u8 到底怎么参与逐像素混合，直接读 u3-l3「字形光栅化与像素级混合」（`blend_src_over`/`alpha_mul`），那里有 src-over 公式的位运算实现。
- **渐变与平铺**：本讲多次提到的 `Paint::Gradient`/`Paint::Tiling` 分支留到 u3-l1、u3-l2，届时你会看到「第 0 步公共量」真正被消费的完整过程。

# 形状渲染与描边 shape.rs

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 `render_shape` 如何把一个 Typst 的 `Shape` 翻译成一段 SVG `<path>`，以及它写出哪些属性（`fill` / `stroke` / `transform` / `d`）。
- 理解 `convert_geometry_to_path` 与 `convert_curve` 如何把三种几何（直线、矩形、曲线）转换成 SVG 路径数据，以及 `comemo::memoize` 在这里的作用。
- 掌握 `write_stroke` 如何把 `FixedStroke` 的厚度、端点、连接、虚线等字段映射到 SVG 的描边属性。
- 理解 `shape_paint_transform` 与 `shape_fill_size` 为渐变 / 平铺（Gradient / Tiling）计算的「绘制变换」与「填充尺寸」的几何含义，尤其是 `RelativeTo::Self_`（相对自身）与 `RelativeTo::Parent`（相对父级）两种参照的区别。
- 能够解释 `Geometry::Rect` 负尺寸为什么需要被特殊处理。

本讲是「矢量原语」单元的第二讲，承接 [u3-l1 路径构建器 path.rs](./u3-l1-path-builder.md)（你已经熟悉 `SvgPathBuilder` 的相对坐标压缩）与 [u2-l3 输出抽象层 write.rs](./u2-l3-write-abstraction.md)（你已经熟悉 `SvgElem`、`attr`、`SvgDisplay`/`SvgWrite`）。

## 2. 前置知识

在进入源码前，先用通俗语言建立几个概念。

### 2.1 什么是「形状」

在 Typst 的排版结果里，一个**形状（Shape）**由四部分组成：几何（geometry）、填充（fill）、填充规则（fill_rule）、描边（stroke）。它的定义在 typst-library 中：

> [`Shape` 结构体](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/shape.rs#L334-L343)：`geometry` + `fill: Option<Paint>` + `fill_rule` + `stroke: Option<FixedStroke>`。

其中**几何（Geometry）**只有三种可能：

> [`Geometry` 枚举](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/shape.rs#L366-L373)：`Line(Point)`（一条到某点的线段）、`Rect(Size)`（原点在左上角的矩形）、`Curve(Curve)`（由移动/直线/贝塞尔曲线组成的任意曲线）。

而**填充或描边的颜料（Paint）**也有三种：

> [`Paint` 枚举](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/paint.rs#L10-L17)：`Solid(Color)`（纯色）、`Gradient(Gradient)`（渐变）、`Tiling(Tiling)`（平铺图案）。

所以「形状渲染」本质上就是：**把 `Geometry` 翻译成 SVG 的 `<path d="...">`，把 `Paint` 翻译成 `fill=` / `stroke=` 属性，再附上必要的变换**。

### 2.2 「相对于谁」：Self_ 与 Parent

渐变和平铺都需要回答一个问题：**这个图案的坐标系锚定在哪里？**

- `RelativeTo::Self_`：锚定在**形状自己的包围盒**上。形状移动，渐变跟着移动，仿佛「画在」形状表面。
- `RelativeTo::Parent`：锚定在**父级 frame**（当前硬帧，详见 [u2-l1](./u2-l1-renderer-and-state.md) 中的 `State.size`）上。形状移动，渐变保持不动，仿佛形状是「透过一扇固定的窗户」看到的背景。

> [`RelativeTo` 枚举](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/gradient.rs#L1228-L1234)：`Self_` 与 `Parent`。

这两种参照决定了渐变需要附加什么样的变换矩阵，这是本讲最需要动脑的部分（见 4.4）。

### 2.3 comemo 记忆化（memoize）

同一个几何（比如「100×50 的矩形」）在一篇文档里可能出现成百上千次。每次都重新生成一遍 SVG 路径字符串是浪费的。`comemo::memoize` 是一个**纯函数缓存**：用参数作为 key，第一次调用时真正计算并存入缓存，之后再遇到相同参数就直接返回缓存结果。它要求函数是「纯」的（相同输入必然相同输出），而「几何 → 路径字符串」恰好是纯函数。

## 3. 本讲源码地图

本讲主要围绕 **`src/shape.rs`**（共约 200 行），并引用若干上下游文件：

| 文件 | 作用 |
|------|------|
| [`src/shape.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/shape.rs#L1-L201) | 本讲主角：`render_shape` 形状渲染入口、`write_stroke` 描边属性、`shape_paint_transform`/`shape_fill_size` 绘制变换、`convert_geometry_to_path`/`convert_curve` 几何转路径。 |
| [`src/path.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/path.rs#L1-L202) | 上一讲的主角 `SvgPathBuilder`，被 `convert_geometry_to_path` 与 `convert_curve` 调用。 |
| [`src/paint.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L31-L57) | 提供 `write_fill` 与 `push_gradient`，`render_shape` 把填充委托给它。 |
| [`src/lib.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L316) | `render_frame` 在分发 `FrameItem::Shape` 时调用 `render_shape`（第 316 行）。 |

阅读顺序建议：先看 4.1 的总入口 `render_shape` 建立全局观，再分别下钻到「几何转路径」（4.2）、「描边」（4.3）、「绘制变换」（4.4）。

## 4. 核心概念与源码讲解

### 4.1 形状渲染总入口 render_shape

#### 4.1.1 概念说明

`render_shape` 是形状渲染的**唯一入口**。它接收一个已经排好版的 `Shape`，把它翻译成一个 SVG `<path>` 元素，并写满所有必要的属性。它本身不做几何计算（那是 4.2 的事），也不直接处理颜色细节（那是 `write_fill`/`write_stroke` 的事），它是一个**编排者（orchestrator）**：把各路帮手的结果拼到一个 `<path>` 上。

#### 4.1.2 核心流程

`render_shape` 对一个 `<path>` 元素依次写入四类信息，顺序很重要：

1. **填充**：若 `shape.fill` 存在，委托 `write_fill`（处理纯色/渐变/平铺，并写出 `fill-rule`）；否则直接写 `fill="none"`，告诉 SVG「这个路径不要填充」。
2. **描边**：若 `shape.stroke` 存在，委托 `write_stroke` 写出 `stroke`、`stroke-width` 等一整套描边属性。
3. **变换**：若当前累积变换 `state.transform` 不是单位矩阵，写 `transform=` 属性，把路径摆到正确位置。
4. **路径数据**：调用 `convert_geometry_to_path` 把几何转成字符串，写到 `d=` 属性。

用伪代码表示：

```
fn render_shape(svg, state, shape):
    path = svg.elem("path")
    if shape.fill 存在:
        write_fill(path, fill, fill_rule, fill_size的aspect_ratio, paint_transform)
    else:
        path.attr("fill", "none")            # 关键：无填充也要显式声明
    if shape.stroke 存在:
        write_stroke(path, stroke, stroke_size的aspect_ratio, stroke_paint_transform)
    if state.transform 不是单位矩阵:
        path.attr("transform", state.transform)
    path.attr("d", convert_geometry_to_path(shape.geometry))
```

#### 4.1.3 源码精读

整个方法定义在这里：

> [`render_shape` 方法](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/shape.rs#L13-L48)：创建 `<path>`，写填充、描边、变换、路径数据。

逐段看关键代码。

先创建元素并对填充做分发：

> [shape.rs:19-31](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/shape.rs#L19-L31)：`svg.elem("path")` 开标签；`Some(paint)` 走 `write_fill`，`None` 走 `fill="none"`。

```rust
let svg = &mut svg.elem("path");

if let Some(paint) = &shape.fill {
    self.write_fill(
        svg, paint, shape.fill_rule,
        self.shape_fill_size(state, paint, shape).aspect_ratio(),
        self.shape_paint_transform(state, paint, shape, false),
    );
} else {
    svg.attr("fill", "none");
}
```

注意 `write_fill` 的后两个参数：`shape_fill_size(...).aspect_ratio()`（宽高比，用于修正渐变角度）和 `shape_paint_transform(..., false)`（绘制变换，`false` 表示「不把描边算进包围盒」，因为这是填充）。这两个辅助函数在 4.4 详解。

接着是描边，结构与填充对称：

> [shape.rs:33-40](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/shape.rs#L33-L40)：`Some(stroke)` 走 `write_stroke`，注意这里 `shape_paint_transform` 的最后一个参数是 `true`（把描边厚度算进包围盒）。

然后是变换与路径数据：

> [shape.rs:42-47](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/shape.rs#L42-L47)：非单位变换写 `transform`；几何经 `convert_geometry_to_path` 转 `d`。

一个小细节：`render_shape` 在哪里被调用？答案在 `render_frame` 的分发里：

> [lib.rs:316](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L316)：`FrameItem::Shape(shape, _) => self.render_shape(svg, &state, shape)`。

也就是说，排版树里每一个形状叶子节点，最终都会走到 `render_shape`。

#### 4.1.4 代码实践

**实践目标**：验证「无填充形状会输出 `fill="none"`」。

**操作步骤**：

1. 准备一个最小 Typst 文件 `shape.typ`，画一个**只有描边、没有填充**的多边形：

   ```typ
   #polygon(
     fill: none,
     stroke: 1pt + black,
     (0%, 0%), (100%, 0%), (50%, 100%),
   )
   ```

2. 用 typst CLI 导出为 SVG（单页，直接指定输出文件名）：

   ```bash
   typst compile shape.typ -f svg -o shape.svg
   ```

   > 说明：typst-cli 对 SVG 格式会为每一页调用 `typst_svg::svg(page, &options)`，见 [compile.rs:583-587](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-cli/src/compile.rs#L583-L587)。单页时 `-o shape.svg` 直接写入该文件。

3. 打开 `shape.svg`，定位到对应的 `<path>` 元素。

**需要观察的现象**：

- 该 `<path>` 上应出现 `fill="none"`（因为 `fill: none`，走的是 `render_shape` 的 `else` 分支）。
- 该 `<path>` 上应出现一整套 `stroke` / `stroke-width` 等属性（走 `write_stroke`）。
- `d=` 里应是相对坐标命令（如 `m`/`l`/`h`/`v`/`c`/`Z`，来自 `SvgPathBuilder`）。

**预期结果**：`<path fill="none" stroke="..." stroke-width="..." d="M ... l ... Z"/>`（具体数值待本地验证）。

> 提示：若把 `fill: none` 改成 `fill: red`，再重新编译，应能看到 `fill="none"` 变成 `fill="#FF0000"`，从而直观对照两个分支。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `render_shape` 在没有填充时要显式写 `fill="none"`，而不是干脆不写 `fill` 属性？

**参考答案**：因为 SVG 的 `<path>` **默认 `fill` 是黑色**（`fill: black`）。如果不显式覆盖，一个只想描边的形状会被意外填上黑色。写 `fill="none"` 是为了明确关闭填充。

**练习 2**：`render_shape` 写属性的顺序是「先 fill 后 stroke 后 transform 后 d」。如果把 `d=` 写到最前面会有问题吗？

**参考答案**：从最终 SVG 的正确性看，XML 属性顺序不影响渲染结果（属性是无序的键值对）。但从生成流程看，`d` 的字符串需要先由 `convert_geometry_to_path` 计算出来，代码把它放在最后只是组织习惯，并非硬性要求。

---

### 4.2 几何转路径 convert_geometry_to_path / convert_curve

#### 4.2.1 概念说明

`render_shape` 只负责拼装属性，真正把 `Geometry` 变成 `d="..."` 字符串的是 `convert_geometry_to_path`（及辅助函数 `convert_curve`）。它们的工作是：遍历几何，调用上一讲学过的 `SvgPathBuilder` 的 `move_to` / `line_to` / `curve_to` / `close` 等命令，产出压缩后的相对坐标路径。

#### 4.2.2 核心流程

`convert_geometry_to_path` 按 `Geometry` 的三种变体分发：

- `Line(t)`：从原点到 `t` 画一条直线 → `builder.line_to(t)`。
- `Rect(size)`：画一个矩形 → `builder.rect(size)`（内部用 4 条边 + close 拼成）。
- `Curve(p)`：任意曲线 → 委托 `convert_curve` 遍历 `CurveItem` 序列。

对 `Curve`，`convert_curve` 遍历曲线的每一个 `CurveItem`：

> [`CurveItem` 枚举](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/curve.rs#L388-L393)：`Move(Point)`（移动画笔）、`Line(Point)`（直线）、`Cubic(p1,p2,p3)`（三次贝塞尔）、`Close`（闭合）。

#### 4.2.3 源码精读

先看带记忆化的总分发函数：

> [`convert_geometry_to_path`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/shape.rs#L177-L188)：`#[comemo::memoize]` 缓存；用 `SvgPathBuilder::with_translate(Point::zero())` 起步。

```rust
#[comemo::memoize]
fn convert_geometry_to_path(geometry: &Geometry) -> EcoString {
    let mut builder = SvgPathBuilder::with_translate(Point::zero());
    match geometry {
        &Geometry::Line(t) => builder.line_to(t),
        &Geometry::Rect(size) => builder.rect(size),
        Geometry::Curve(p) => {
            return convert_curve(Point::zero(), p);
        }
    }
    builder.finsish()
}
```

几个要点：

- **`with_translate(Point::zero())`** 起步会先写一个 `M 0 0`，把画笔移到原点（见 [path.rs:19-30](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/path.rs#L19-L30)）。形状的几何坐标本身是局部的（原点在自身左上角），绝对平移由外层 `transform` 属性承担，所以这里起点固定在原点。
- **`finsish()`**（注意是源码里的拼写）消费 builder 并返回内部的 `EcoString`（见 [path.rs:53-55](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/path.rs#L53-L55)）。
- **`Curve` 分支提前 `return`**：因为 `convert_curve` 自己会创建一个新的 builder，不复用上面的 `builder`。

再看曲线转换：

> [`convert_curve`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/shape.rs#L190-L201)：用 `initial_point` 起步，逐项 match `CurveItem`。

```rust
pub fn convert_curve(initial_point: Point, curve: &Curve) -> EcoString {
    let mut builder = SvgPathBuilder::with_translate(initial_point);
    for item in &curve.0 {
        match *item {
            CurveItem::Move(pos) => builder.move_to(pos),
            CurveItem::Line(pos) => builder.line_to(pos),
            CurveItem::Cubic(p1, p2, p3) => builder.curve_to(p1, p2, p3),
            CurveItem::Close => builder.close(),
        }
    }
    builder.finsish()
}
```

注意 `convert_curve` 是 `pub fn`（公开给 crate 内其他模块），而 `convert_geometry_to_path` 是私有 `fn`。这是因为 `lib.rs` 里的裁剪路径（clip path）需要用 `convert_curve` 把裁剪曲线转成路径（见 [lib.rs:354](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L354) 调用 `shape::convert_curve`），所以它必须跨模块可见。

**关于 `comemo::memoize`**：只有 `convert_geometry_to_path` 被记忆化，`convert_curve` 没有。原因是 `convert_curve` 多了一个 `initial_point` 参数且常用于裁剪（每次 offset 不同），缓存命中率低；而 `convert_geometry_to_path` 只依赖 `Geometry` 本身，在整篇文档里重复率极高，缓存收益大。

矩形是怎么拼出来的？看 `SvgPathBuilder::rect`：

> [path.rs:67-73](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/path.rs#L67-L73)：`move_to(0,0)` → `line_to(0,h)` → `line_to(w,h)` → `line_to(w,0)` → `close`，逆时针描出矩形四角。

#### 4.2.4 代码实践

**实践目标**：手工推演一个矩形几何转换出的 `d` 字符串，验证相对坐标压缩。

**操作步骤**：

1. 假设几何是 `Geometry::Rect(Size{x: 40pt, y: 20pt})`。
2. 跟踪 `convert_geometry_to_path` → `builder.rect((40,20))`：
   - `move_to((0,0))`：起点即原点，相对增量为 0，上一讲学过「`move_to` 增量为零时整条跳过」，所以**不输出**任何 `m` 命令，但 `last_close_point`/`last_point` 仍被设为 `(0,0)`。
   - `line_to((0,20))`：相对增量 `(0,20)`，纯垂直 → 退化为 `v 20`。
   - `line_to((40,20))`：相对增量 `(40,0)`，纯水平 → `h 40`。
   - `line_to((40,0))`：相对增量 `(0,-20)`，纯垂直 → `v -20`。
   - `close`：输出 `Z`。
3. 加上 `with_translate(Point::zero())` 开头的 `M 0 0`。

**需要观察的现象**：最终的 `d` 字符串应近似为 `M 0 0 v 20 h 40 v -20 Z`（而不是四组完整的 `l x y` 绝对坐标）。

**预期结果**：用 4.1 实践里的方法，编译一个 `#rect(width: 40pt, height: 20pt, fill: black)` 并查看其 `<path>` 的 `d=`，应能看到 `h`/`v` 这种简写命令（待本地验证精确字符串）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `convert_geometry_to_path` 用 `with_translate(Point::zero())` 而不是 `with_scale` 或 `empty`？

**参考答案**：形状的几何坐标已经是「以自身左上角为原点」的局部坐标，且单位已是 pt，不需要缩放；`with_translate(Point::zero())` 会写入一个 `M 0 0` 把画笔定位到原点，后续命令基于此生成相对坐标。`with_scale` 是为字形轮廓提取准备的（需要把字体单位换算到 pt），`empty` 是为自定义拼接准备的，这里都不需要。

**练习 2**：`comemo::memoize` 要求被缓存函数是纯函数。`convert_geometry_to_path` 满足吗？

**参考答案**：满足。它的输出完全由输入 `&Geometry` 决定，不读取任何外部状态、不产生副作用（只是构建并返回一个 `EcoString`），相同几何必然得到相同路径字符串，因此可以安全记忆化。

---

### 4.3 描边属性 write_stroke

#### 4.3.1 概念说明

`write_stroke` 负责把一个 `FixedStroke`（完全确定的描边）翻译成 SVG 的一组描边属性。`FixedStroke` 包含六个字段：颜料（paint）、厚度（thickness）、端点（cap）、连接（join）、虚线（dash）、斜接限制（miter_limit）。

> [`FixedStroke` 结构体](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/stroke.rs#L626-L639)：`paint` + `thickness` + `cap` + `join` + `dash` + `miter_limit`。

#### 4.3.2 核心流程

`write_stroke` 依次写出：

1. **`stroke`**：根据 `paint` 分发——纯色直接写颜色；渐变调用 `push_gradient` 拿到 id 后写 `url(#id)`；平铺调用 `push_tiling`。这一段与 `write_fill` 的颜料处理**完全对称**。
2. **`stroke-width`**：描边厚度（pt）。
3. **`stroke-linecap`**：线段端点样式，`butt`/`round`/`square`。
4. **`stroke-linejoin`**：拐角连接样式，`miter`/`round`/`bevel`。
5. **`stroke-miterlimit`**：尖角连接的最大斜接长度比。
6. **虚线**（可选）：`stroke-dashoffset` 与 `stroke-dasharray`。

#### 4.3.3 源码精读

完整方法：

> [`write_stroke`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/shape.rs#L128-L173)：按颜料分发写 `stroke`，再写厚度、端点、连接、斜接限制、虚线。

颜料分发部分：

> [shape.rs:135-147](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/shape.rs#L135-L147)：`Solid` 直接写颜色；`Gradient` 经 `push_gradient` 得到 id，用 `SvgUrl(id)` 包装成 `url(#...)`；`Tiling` 经 `push_tiling`。

```rust
match &stroke.paint {
    Paint::Solid(color) => { svg.attr("stroke", color); }
    Paint::Gradient(gradient) => {
        let id = self.push_gradient(gradient, aspect_ratio, fill_transform);
        svg.attr("stroke", SvgUrl(id));
    }
    Paint::Tiling(tiling) => {
        let id = self.push_tiling(tiling, fill_transform);
        svg.attr("stroke", SvgUrl(id));
    }
}
```

注意：渐变/平铺作为描边时，同样要走「源 + 引用」两层去重（见 [u5-l2](./u5-l2-fill-stroke-dedup.md)，后续讲义）。`SvgUrl(id)` 是一个 newtype 适配器，把 `DedupId` 格式化成 `url(#gXXXX)` 形式（来自 [write.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/write.rs)）。

端点与连接是简单的枚举到字符串映射：

> [shape.rs:150-165](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/shape.rs#L150-L165)：`LineCap::{Butt,Round,Square}` → `"butt"/"round"/"square"`；`LineJoin::{Miter,Round,Bevel}` → `"miter"/"round"/"bevel"`。

虚线部分用 `attr_with` 闭包批量写入 dash 数组：

> [shape.rs:167-172](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/shape.rs#L167-L172)：若有 `dash`，写 `stroke-dashoffset` 与 `stroke-dasharray`（数组用 `push_nums` 逐项写出）。

#### 4.3.4 代码实践

**实践目标**：观察不同 `linecap` / `linejoin` / 虚线设置如何映射到 SVG 属性。

**操作步骤**：

1. 编译下面这个 Typst 文件（一条带圆角端点、虚线的粗线段）：

   ```typ
   #set page(width: 100pt, height: 100pt)
   #line(
     start: (10pt, 20pt),
     end: (90pt, 80pt),
     stroke: (
       paint: black,
       thickness: 4pt,
       cap: "round",
       dash: "dashed",
     ),
   )
   ```

2. 导出 SVG：`typst compile line.typ -f svg -o line.svg`。

3. 在 `line.svg` 中找到该线段对应的 `<path>`。

**需要观察的现象**：

- 出现 `stroke-linecap="round"`。
- 出现 `stroke-width="4"`（单位 pt）。
- 出现 `stroke-dasharray="..."`（一组交替的实线/空白长度）和 `stroke-dashoffset`。

**预期结果**：一个带完整描边属性集的 `<path fill="none" stroke="..." stroke-width="4" stroke-linecap="round" ... stroke-dasharray="..."/>`（精确数值待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`write_stroke` 与 `write_fill`（[paint.rs:31-57](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L31-L57)）在「颜料分发」部分几乎一样。为什么不抽成一个共用函数？

**参考答案**：可以抽，但二者目标属性名不同（`stroke` vs `fill`），且 `write_fill` 额外要写 `fill-rule`、`write_stroke` 额外要写一整套厚度/端点/连接/虚线属性，差异部分远多于相同部分。当前各写各的、共享 `push_gradient`/`push_tiling` 这两个底层去重入口，已经是合理的复用粒度。

**练习 2**：`stroke-miterlimit` 默认值是 4.0（见 [stroke.rs:660](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/stroke.rs#L660)）。它控制什么？

**参考答案**：当两条线段以 `miter`（尖角）方式连接时，若夹角很小，尖角会向外延伸得很长。`miter-limit` 规定尖角长度与厚度的最大比值，超过就把尖角自动退化成 `bevel`（斜角），避免出现极长的尖刺。

---

### 4.4 绘制变换 shape_paint_transform / shape_fill_size

#### 4.4.1 概念说明

这是本讲最核心、也最需要几何直觉的部分。当一个形状用**渐变或平铺**填充（而非纯色）时，SVG 里那个渐变/平铺图案本身定义在一个「单位正方形」\([0,1]\times[0,1]\) 里。要让它正确地铺在形状上，需要计算一个**绘制变换（paint transform）**，把单位正方形映射到目标区域。

这个变换取决于「相对于谁」：

- **`Self_`**：把单位正方形映射到**形状自己的包围盒**。
- **`Parent`**：把单位正方形映射到**父级 frame**，并且要**抵消**形状自身的位移，让图案看起来「固定在背景上」。

此外，渐变还需要一个**宽高比（aspect ratio）**，用来修正非正方形形状里渐变角度的畸变（例如一个扁宽的矩形里，45° 渐变的实际方向需要按宽高比校正）。这正是 `shape_fill_size` 返回尺寸、再取 `.aspect_ratio()` 的用途。

> [`Size::aspect_ratio`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/layout/size.rs#L27-L29)：`Ratio::new(self.x / self.y)`，即宽除以高。

#### 4.4.2 核心流程

`shape_paint_transform` 的计算分三步：

1. **取包围盒**：`shape.bbox(include_stroke_in_bbox)` 得到 `(offset, size)`。对填充用 `bbox(false)`，对描边用 `bbox(true)`（把描边厚度算进去）。
2. **修正负尺寸矩形**：若几何是 `Rect` 且宽或高为负，特殊翻转 offset 与 size（见 4.4.3 与本讲综合实践）。
3. **零值兜底**：若 size 某分量为 0，强制改成 1pt，避免后续除零或退化。
4. **按颜料类型与参照系给变换**：
   - **Gradient + Self_**：`scale(size) · translate(offset)` —— 单位正方形缩放到自身包围盒并平移到位。
   - **Gradient + Parent**：`scale(state.size) · state.transform⁻¹` —— 缩放到父级尺寸，并用累积变换的逆来抵消形状自身位移。
   - **Tiling + Self_**：`identity()`（平铺自身已带尺寸）。
   - **Tiling + Parent**：`state.transform⁻¹`（仅抵消位移）。
   - **Solid**：`identity()`（纯色无需变换）。

用数学语言描述 Self_ 的映射（设包围盒左上角 \(o=(o_x,o_y)\)、尺寸 \(s=(s_x,s_y)\)）：

\[
T_{\text{self}}(p) = \mathrm{diag}(s_x, s_y)\, p + o,
\]

即先把单位坐标 \(p\in[0,1]^2\) 缩放到包围盒大小，再平移到包围盒左上角。

对 Parent，设父级尺寸 \(S=(S_x,S_y)\)、形状累积变换 \(M\)（即 `state.transform`）：

\[
T_{\text{parent}} = M^{-1}\,\mathrm{diag}(S_x, S_y).
\]

这里的 \(M^{-1}\) 是关键：因为同一个 `<path>` 还会把 `transform=M` 写到属性里（见 4.1.3 第 3 步），这会带着填充一起动；为了让渐变「不跟着动」、始终锚定在父级，需要预先乘上 \(M^{-1}\) 把它抵消掉，剩下的就只是「缩放到父级尺寸」。

#### 4.4.3 源码精读

完整方法：

> [`shape_paint_transform`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/shape.rs#L51-L103)：取包围盒、修负尺寸、零值兜底、按颜料与参照系产出变换。

先取包围盒：

> [shape.rs:58-61](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/shape.rs#L58-L61)：`bbox(include_stroke_in_bbox)` 拆成 `offset`（min 角）和 `size`。

负尺寸矩形的特殊处理：

> [shape.rs:63-73](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/shape.rs#L63-L73)：注释明说是「为负尺寸镜像渐变」。

```rust
// Special handling for rectangles (mirrors gradients for negative sizes)
if let Geometry::Rect(rect) = shape.geometry {
    if rect.x.signum() < 1.0 {
        offset.x += size.x;
        size.x *= -1.0;
    }
    if rect.y.signum() < 1.0 {
        offset.y += size.y;
        size.y *= -1.0;
    }
}
```

**为什么需要这段？** 关键在于 `SvgPathBuilder::rect` 是按固定顺序描出四个角的（见 4.2.3）。当 `size.x` 为负时，矩形会向 x 轴负方向延伸，相当于「水平翻转」地画出。包围盒 `bbox` 本身总是规范化的（用 `abs()` 取正尺寸、用 `min` 取真正左上角，见 [shape.rs:421-428](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/shape.rs#L421-L428)），所以若直接用规范化后的正尺寸去算渐变，渐变方向就和「实际翻转着画的矩形」对不上了。这段代码通过**把对应的 size 分量取负、并平移 offset**，让渐变也随之镜像，使填充方向与翻转的矩形一致。`signum() < 1.0` 在值为负（或零）时成立。

零值兜底：

> [shape.rs:75-80](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/shape.rs#L75-L80)：任一维为 0 时强制设为 1pt，避免退化。

按颜料与参照系产出变换：

> [shape.rs:82-102](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/shape.rs#L82-L102)：Gradient 分 Self_/Parent；Tiling 分 Self_/Parent；Solid 走 else。

```rust
if let Paint::Gradient(gradient) = paint {
    match gradient.unwrap_relative(false) {
        RelativeTo::Self_ => Transform::scale(...).post_concat(Transform::translate(offset.x, offset.y)),
        RelativeTo::Parent => Transform::scale(...).post_concat(state.transform.invert().unwrap()),
    }
} else if let Paint::Tiling(tiling) = paint {
    match tiling.unwrap_relative(false) {
        RelativeTo::Self_ => Transform::identity(),
        RelativeTo::Parent => state.transform.invert().unwrap(),
    }
} else {
    Transform::identity()
}
```

> 术语：`post_concat` 是 typst 的变换拼接约定——把参数作为「外层」叠加（先作用自身，再作用参数）；这与 [u2-l1](./u2-l1-renderer-and-state.md) 讲过的 `pre_concat`（参数作「内层」、先作用于点）相对。这里 `scale(...).post_concat(translate(...))` 等价于「先缩放、再平移」。

`unwrap_relative(false)` 处理 `auto`：渐变的 `relative` 字段可能是 `Smart::Auto`，此时默认按 `Self_` 处理（参数 `false` 表示非文本场景，见 [gradient.rs:985-989](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/gradient.rs#L985-L989)）。

再看 `shape_fill_size`：

> [`shape_fill_size`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/shape.rs#L106-L125)：Gradient + Self_ 返回自身包围盒尺寸；Gradient + Parent 返回 `state.size`；其余返回自身尺寸。

它的返回值在 `render_shape` 里立即 `.aspect_ratio()` 取宽高比，传给 `write_fill`/`write_stroke`，最终在 [paint.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L131-L132) 用于 `Gradient::correct_aspect_ratio(angle, aspect_ratio)` 修正渐变角度。

> [`correct_aspect_ratio`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/gradient.rs#L994-L996)：\(\theta'=\mathrm{atan2}(\sin\theta/|r|,\ \cos\theta)\)，其中 \(r\) 为宽高比。

#### 4.4.4 代码实践

**实践目标**：追踪 `RelativeTo::Parent` 渐变的变换矩阵含义，并解释负尺寸矩形的特殊处理。

**操作步骤（A：Parent 渐变）**：

1. 编译一个用「相对父级」渐变填充的小矩形：

   ```typ
   #set page(width: 200pt, height: 100pt)
   #box(width: 60pt, height: 40pt)[
     #rect(width: 60pt, height: 40pt, fill: gradient.linear(..color.map.rainbow, relative: "parent"))
   ]
   ```

2. 导出 SVG 并找到该矩形的 `<path>` 及其引用的渐变定义（在 `<defs>` 里，形如 `<linearGradient ...>` 或带 `gradientTransform` 的引用渐变）。

3. 对比：把 `relative: "parent"` 改成 `relative: "self"`，重新编译，观察渐变定义里 `gradientTransform`（或坐标范围）的变化。

**需要观察的现象（A）**：

- `relative: "self"` 时，渐变被缩放到矩形自身 60×40 的范围。
- `relative: "parent"` 时，渐变被缩放到父级（page 或外层 frame）的更大范围，且 `gradientTransform` 里应能看出一个「抵消形状位移」的平移/逆变换分量，使渐变看起来像固定的背景。

**操作步骤（B：负尺寸矩形）**：

4. 阅读源码 [shape.rs:63-73](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/shape.rs#L63-L73)，假设几何是 `Geometry::Rect(Size{x: -50pt, y: 30pt})`：
   - `bbox` 规范化后：`offset.x = -50`，`size.x = 50`（`abs`）。
   - 因 `rect.x.signum() = -1 < 1.0`：`offset.x += size.x` → `-50 + 50 = 0`；`size.x *= -1` → `-50`。
   - 最终用于渐变的 `(offset.x, size.x) = (0, -50)`，即渐变在 x 方向被**翻转**并平移到 `x∈[-50, 0]`，与 `rect()` 实际向负方向画出的矩形一致。

**需要观察的现象（B）**：负宽矩形的渐变方向应与把它翻成正宽矩形后的渐变方向呈镜像关系，而不是错乱。

**预期结果**：Parent 渐变的 `gradientTransform` 含抵消分量；负尺寸矩形的渐变被正确镜像（具体矩阵数值待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `RelativeTo::Parent` 分支里要乘 `state.transform.invert()`，而 `Self_` 分支不需要？

**参考答案**：因为同一个 `<path>` 会把 `state.transform` 写到 `transform` 属性上，这个变换会连同填充一起被搬动。`Self_` 希望「渐变跟着形状走」，所以正合适，无需额外处理；`Parent` 希望「渐变锚定在父级、不跟形状走」，所以必须预先乘上 `state.transform` 的逆，把 path 的 `transform` 效果抵消掉，剩下的才是「缩放到父级尺寸」。

**练习 2**：`shape_fill_size` 在 Gradient 分支里对 Self_ 返回 `shape_size`（包围盒尺寸），对 Parent 返回 `state.size`（父级尺寸）。这个尺寸最终用在哪里？

**参考答案**：它立即被 `.aspect_ratio()` 取成宽高比，传给 `write_fill`/`write_stroke` → `push_gradient` → 存入渐变的去重表，最终在写出渐变定义时用于 `correct_aspect_ratio` 修正渐变角度，使渐变在非正方形区域里方向正确。

**练习 3**：负尺寸矩形的特殊处理只影响哪种颜料？

**参考答案**：只影响 `Gradient` 且 `RelativeTo::Self_`。因为修改后的 `offset`/`size` 只在 Gradient 的 Self_ 分支被使用；Parent 分支用的是 `state.size`/`state.transform`，Tiling 与 Solid 都不依赖这两个局部变量。

## 5. 综合实践

把本讲的四块知识（`render_shape` 编排、几何转路径、描边属性、绘制变换）串起来，做一个端到端的追踪任务。

**任务**：用一个带渐变填充和虚线描边的多边形，完整追踪它从 Typst 源码到 SVG 输出的全过程。

1. 准备 `demo.typ`：

   ```typ
   #set page(width: 200pt, height: 120pt)
   #polygon(
     fill: gradient.linear(red, blue, relative: "self"),
     stroke: 2pt + (paint: black, dash: "dotted"),
     (0pt, 0pt), (160pt, 0pt), (80pt, 90pt),
   )
   ```

2. 编译：`typst compile demo.typ -f svg -o demo.svg`。

3. 打开 `demo.svg`，做四件事，分别对应本讲四个模块：

   - **对应 4.1（render_shape 编排）**：找到该多边形的 `<path>`，确认它同时有 `fill="url(#...)"`（渐变）、一整套 `stroke` 属性、`d="..."`。为什么这里没有 `fill="none"`？因为它有渐变填充，走的是 `Some(paint)` 分支。
   - **对应 4.2（几何转路径）**：查看 `d=` 字符串，识别其中的 `M`/`l`/`L`/`h`/`v`/`Z` 命令，验证 `convert_curve` 把三个顶点的 `Curve` 转成了相对坐标路径。
   - **对应 4.3（描边）**：确认 `stroke-dasharray` 存在（因为 `dash: "dotted"`），并找到 `stroke-linejoin`（多边形顶点处的连接样式）。
   - **对应 4.4（绘制变换）**：在 `<defs>` 里找到被引用的 `<linearGradient>`（或它的引用渐变），确认其 `gradientUnits="userSpaceOnUse"`，并尝试根据多边形的包围盒推算它的端点坐标（`x1/y1/x2/y2`）是否落在包围盒范围内（`relative: "self"` 的体现）。

4. 进阶对比：把 `relative: "self"` 改成 `relative: "parent"`，重新编译，对比渐变定义的坐标范围变化，直观感受 4.4 里 `state.transform.invert()` 的作用。

**预期结果**：你能指着 `demo.svg` 里的每一段，说清楚它是由 `render_shape` 的哪一步、调用了本讲哪个函数产出的（精确数值待本地验证）。

## 6. 本讲小结

- `render_shape` 是形状渲染的编排者：它创建一个 `<path>`，依次委托写出填充（`write_fill` 或 `fill="none"`）、描边（`write_stroke`）、变换（`transform`）、路径数据（`d`）。
- `convert_geometry_to_path` 用 `comemo::memoize` 缓存，按 `Line`/`Rect`/`Curve` 三种几何分发；`convert_curve` 遍历 `CurveItem` 序列生成相对坐标路径，且作为 `pub fn` 供裁剪路径复用。
- `write_stroke` 把 `FixedStroke` 映射成一整套 SVG 描边属性（`stroke`/`stroke-width`/`stroke-linecap`/`stroke-linejoin`/`stroke-miterlimit`/虚线），其颜料分发与 `write_fill` 对称。
- `shape_paint_transform` 为渐变/平铺计算绘制变换：`Self_` 把单位图案映射到自身包围盒，`Parent` 用 `state.transform` 的逆抵消形状位移以锚定父级。
- `shape_fill_size` 提供宽高比，用于 `correct_aspect_ratio` 修正非正方形区域里的渐变角度。
- `Geometry::Rect` 的负尺寸会被特殊翻转处理，使渐变方向与 `rect()` 固定顺序描出的「翻转矩形」保持一致（仅影响 Gradient + Self_）。

## 7. 下一步学习建议

本讲解完了「形状」这一类渲染对象。建议接下来：

- 阅读 [u4-l1 文本与字形渲染 text.rs](./u4-l1-text-and-glyphs.md)，看另一类渲染对象（文本）如何复用本讲的 `SvgPathBuilder` 与 `convert_curve`，以及它为何要先做 Y 轴翻转。
- 若对渐变细节意犹未尽，直接跳到 [u5-l2 填充/描边入口与去重引用模型](./u5-l2-fill-stroke-dedup.md)，深入 `write_fill`/`push_gradient` 的「源 + 引用」两层去重，理解本讲里 `SvgUrl(id)` 引用的渐变到底是怎样被写入 `<defs>` 的。
- 想理解纯色如何变成字符串（如 `fill="#FF0000"`），可先读 [u5-l1 颜色序列化](./u5-l1-color-serialization.md)。

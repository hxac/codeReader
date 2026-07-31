# 几何形状与描边

## 1. 本讲目标

本讲专讲 typst-render 如何把一个**几何形状**（`Shape`）画成像素。读完本讲你应当能够：

1. 说清 `render_shape` 的总流程：先根据 `Geometry` 构造一条 tiny-skia 路径，再分别做「填充」和「描边」两遍绘制。
2. 掌握三种几何 `Line` / `Rect` / `Curve` 到 `sk::Path` 的转换，特别是**负尺寸矩形**为什么需要一次 `signum` 镜像变换。
3. 读懂 `convert_curve` 如何把 Typst 的曲线元素逐条翻译成 tiny-skia 的路径命令。
4. 理解描边三件套——线帽（`LineCap`）、连接（`LineJoin`）、虚线（`DashPattern`）——到 tiny-skia 对应类型的映射，并解释虚线数组**奇数翻倍**的原理。
5. 把 Typst 的 `FixedStroke` 字段一一对应到 tiny-skia 的 `sk::Stroke` 字段。

## 2. 前置知识

在进入本讲前，请确认你已经理解下面这些在前面讲义中建立的概念：

- **Frame 场景树与派发**（u1-l3）：`render_frame` 遍历 `FrameItem`，其中 `FrameItem::Shape(shape, _)` 会被交给 `shape::render_shape`，并在原地 `state.pre_translate(*pos)` 落位（[src/lib.rs:195-197](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L195-L197)）。
- **State 与坐标变换**（u2-l1）：`state.transform` 是从画布原点到当前点的累积仿射变换；`AbsExt::to_f32` 把 Typst 的 `Abs` 长度统一换算成 pt-f32（[src/lib.rs:277-286](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L277-L286)）。本讲里所有坐标最终都要乘上 `state.transform` 才落到画布像素。
- **纯色 Paint 转换**（u2-l3）：`paint::to_sk_paint(paint, state, on_text, pixmap, shape, include_stroke_in_bbox)` 把 Typst 的 `Paint` 转成 tiny-skia 画笔 `sk::Paint`。本讲只**调用**它，不重复其内部细节。

补充几个本讲会用到的术语：

- **tiny-skia**：typst-render 依赖的 2D 光栅化引擎，提供 `Pixmap`（像素画布）、`Path`（矢量路径）、`Paint`（画笔）、`Stroke`（描边参数）、`Mask`（逐像素遮罩）等原语。
- **路径（Path）**：由「移动到 / 连线到 / 三次贝塞尔到 / 闭合」等命令组成的矢量图形。填充和描边都作用在路径上。
- **填充规则（Fill Rule）**：判断路径某区域是「内」还是「外」的规则，常用 `NonZero`（非零环绕数）与 `EvenOdd`（奇偶）。
- **pt（点）**：Typst 的内部长度单位，1 inch = 72 pt。`Abs::to_f32()` 返回的就是 pt 数。

> 说明：本讲引用的 `Paint`/`to_sk_paint` 内部逻辑属于 u2-l3，渐变与平铺填充属于 u3-l1/u3-l2，本讲只在「填充调用」处点到为止。

## 3. 本讲源码地图

本讲几乎全部集中在**一个文件**里：

| 文件 | 作用 |
| --- | --- |
| [src/shape.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/shape.rs) | 本讲主角。含 5 个函数：`render_shape`（总入口）、`convert_curve`（曲线→路径）、`to_sk_line_cap` / `to_sk_line_join` / `to_sk_dash_pattern`（描边三翻译器）。 |
| [src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs) | 提供 `State`、`AbsExt`，以及 `render_frame` 中对 `Shape` 的派发；另外 `render()` 在画渐变背景时也复用了 `render_shape`。 |
| [src/paint.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs) | 提供 `to_sk_paint`，被 `render_shape` 调用以得到画笔（u2-l3 详讲）。 |

Typst 侧的类型定义（只需了解字段，不必深读）位于 `typst-library`：

- `Shape { geometry, fill, fill_rule, stroke }` — [visualize/shape.rs:334-343](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/shape.rs#L334-L343)
- `Geometry::{ Line(Point), Rect(Size), Curve(Curve) }` — [visualize/shape.rs:366-373](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/shape.rs#L366-L373)
- `FixedStroke { paint, thickness, cap, join, dash, miter_limit }` — [visualize/stroke.rs:626-639](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/stroke.rs#L626-L639)
- `CurveItem::{ Move, Line, Cubic, Close }` — [visualize/curve.rs:388-393](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/curve.rs#L388-L393)

---

## 4. 核心概念与源码讲解

### 4.1 render_shape：形状渲染总入口

#### 4.1.1 概念说明

一个 Typst `Shape` 由四部分组成：**几何**（`geometry`，决定「画什么形状」）、**填充**（`fill`，可选，决定「内部填什么颜色/渐变」）、**填充规则**（`fill_rule`，决定「怎样算内部」）、**描边**（`stroke`，可选，决定「边框怎么画」）。

`render_shape` 是把这样一个 `Shape` 变成像素的总入口。它的设计思路很清晰，可以概括成**「一路径，两遍绘制」**：

1. **一路径**：不管几何是直线、矩形还是自由曲线，先把它统一翻译成一条 tiny-skia 的 `sk::Path`。路径是「形状的纯几何表达」，与颜色无关。
2. **两遍绘制**：如果有 `fill`，用 `canvas.fill_path` 把路径内部填上；如果有 `stroke`，再用 `canvas.stroke_path` 沿路径画一道边框。两者独立，互不影响，且**共用同一条路径**。

这个「先归一化为路径、再分别填充/描边」的拆分，正是 tiny-skia（以及 Skia）这类矢量引擎的通用范式，也是 `render_shape` 如此短小（约 70 行）的根本原因。

#### 4.1.2 核心流程

`render_shape` 的伪代码流程如下：

```
fn render_shape(canvas, state, shape):
    ts = state.transform                      # 当前累积变换（含 pt→像素）
    # —— 第 0 步：把几何归一化成一条 sk::Path ——
    path = match shape.geometry:
        Line(target) => 只有一条连线的路径
        Rect(size)   => 矩形路径（处理负尺寸）
        Curve(curve) => convert_curve(curve)  # 翻译曲线

    # —— 第 1 步：填充（可选）——
    if let Some(fill) = shape.fill:
        paint = to_sk_paint(fill, ...)        # 颜色/渐变 → 画笔
        if 矩形: paint.anti_alias = false      # 轴对齐矩形关掉抗锯齿
        rule  = FillRule 映射                  # NonZero/EvenOdd
        canvas.fill_path(path, paint, rule, ts, state.mask)

    # —— 第 2 步：描边（可选）——
    if let Some(stroke) = shape.stroke:
        width = stroke.thickness.to_f32()
        if width > 0.0:                        # 0 宽度不画
            dash   = to_sk_dash_pattern(...)   # 虚线（可选）
            paint  = to_sk_paint(stroke.paint, ...)
            stroke = sk::Stroke { width, line_cap, line_join, dash, miter_limit }
            canvas.stroke_path(path, paint, stroke, ts, state.mask)
```

两条值得记住的不变量：

- **路径只建一次**，填充和描边复用同一条 `path`。若 `Shape` 同时有 `fill` 和 `stroke`，会先填后描（描边压在填充之上）。
- **矩形走快路径**：tiny-skia 的 `fill_path` 默认开启抗锯齿，但对**轴对齐矩形**这是多余的（边界本身就是整像素级的直线），所以代码在矩形分支里把 `anti_alias` 改回 `false`，省掉一次抗锯齿计算。

#### 4.1.3 源码精读

总入口签名与几何分派，先把 `Geometry` 三选一翻译成 `sk::Path`：

[src/shape.rs:11-37](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/shape.rs#L11-L37) —— `render_shape` 开头：取 `state.transform`，按 `Line` / `Rect` / `Curve` 三种几何构造路径。注意 `Line(target)` 是「从原点连到 `target`」的一条线，所以直接 `line_to`。

**负尺寸矩形**是 Typst 对 tiny-skia 的一处专门适配，值得细看：

[src/shape.rs:19-35](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/shape.rs#L19-L35) —— 矩形分支。代码注释解释了原因：「Skia 通常不允许负尺寸，但 Typst 支持，所以需要时我们施加一次变换」。其手法是：

1. 先用绝对尺寸建一个正矩形 `Rect::from_xywh(0, 0, w.abs(), h.abs())`；
2. 再用 `from_scale(w.signum(), h.signum())` 做一次「按符号翻转」的缩放；
3. 用 `rect.transform(transform)` 把翻转作用到矩形上。

`signum` 的含义是：负数 → \(-1\)，正数 → \(+1\)。所以这是一次纯镜像（不改变大小）。设 \(w=-100,\ h=50\)：

\[
\text{signum}(w),\text{signum}(h)=(-1,+1)
\]

正矩形角点为 \((0,0)\) 与 \((100,50)\)，经缩放 \((-1,+1)\) 后 \(x\) 取反，角点变为 \((0,0)\) 与 \((-100,50)\)——矩形被「镜像到原点左侧」，这正是 Typst 语义里「负宽度的矩形」。注释还特意说明：因为 `transform` 在 tiny-skia 文档里被标注为「较贵」的操作，**只在确有负尺寸时才做**，正尺寸直接走 `else` 分支，省掉这次变换。

接下来是**填充**部分：

[src/shape.rs:39-53](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/shape.rs#L39-L53) —— 填充分支。要点三处：

- 第 41-42 行调用 `to_sk_paint(fill, state, false, &mut pixmap, Some(shape), false)`：`on_text=false`（这是形状不是文字）、`shape=Some(shape)`（让渐变能算包围盒）、`include_stroke_in_bbox=false`（填充不需要把描边宽度算进包围盒）。返回的 `sk::Paint` 已配好颜色或渐变。
- 第 44-46 行：若几何是矩形，把 `paint.anti_alias` 改为 `false`（前述快路径）。
- 第 48-52 行：把 `FillRule::NonZero` 映射成 `sk::FillRule::Winding`，`EvenOdd` 映射成 `sk::FillRule::EvenOdd`，然后 `canvas.fill_path`。注意 `ts`（即 `state.transform`）作为参数传入，tiny-skia 在光栅化时会把路径整体变换到画布像素；`state.mask` 则把逐像素遮罩一并带上（来自 u2-l2 的裁剪链）。

> 字段名小坑：Typst 的 `NonZero` 在 tiny-skia 里叫 `Winding`（环绕数），名字不同但语义一致——都是「按有符号环绕数判定内部」。

最后是**描边**部分：

[src/shape.rs:55-81](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/shape.rs#L55-L81) —— 描边分支。逐段读：

- 第 55-56 行用 `if let Some(FixedStroke { paint, thickness, cap, join, dash, miter_limit })` **一次性解构**出描边的全部六个字段。
- 第 58 行 `width = thickness.to_f32()`：把 `Abs` 厚度转成 pt-f32。
- 第 60-61 行：**零宽度不画**（`if width > 0.0`）。这是有意的保护——画一道 0 宽度的描边既无意义又浪费。
- 第 62 行：`dash.as_ref().and_then(to_sk_dash_pattern)`，把可选的虚线模式翻译成 `Option<sk::StrokeDash>`（详见 4.3）。
- 第 63-71 行：再调一次 `to_sk_paint` 给描边上色，注意最后一个参数 `matches!(paint, Paint::Gradient(_))`——**只有当描边本身是渐变时**，才把 `include_stroke_in_bbox` 置为 `true`，让渐变的采样区域把描边宽度也算进去（纯色描边不需要，保持 `false`）。
- 第 72-78 行：组装 `sk::Stroke`，把 Typst 的 `cap/join/dash/miter_limit` 翻译进来（详见 4.3）。
- 第 79 行：`canvas.stroke_path` 沿同一条 `path` 画边框，同样带上 `ts` 与 `state.mask`。

值得一提的是：`render()` 在画**渐变页面背景**时也复用了 `render_shape`——它把页面尺寸包成一个 `Geometry::Rect(size).filled(fill)` 的形状再渲染（[src/lib.rs:38-40](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L38-L40)）。这说明 `render_shape` 不仅是「用户画的形状」的渲染器，也是 typst-render 内部的通用矩形填充工具。

#### 4.1.4 代码实践

**实践目标**：亲手确认「矩形关抗锯齿、零宽度不画」这两条优化分支的存在与作用。

**操作步骤**（源码阅读型，无需运行）：

1. 打开 [src/shape.rs:44-46](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/shape.rs#L44-L46)，确认 `anti_alias = false` 只在 `Geometry::Rect` 时触发。
2. 思考：一个 `line`（直线）或 `curve`（曲线）形状做纯色填充时，`anti_alias` 是 `true` 还是 `false`？（答案：`true`，因为 `to_sk_paint` 的纯色分支默认 `anti_alias = true`，而这两类几何不会被第 44 行的 `matches!` 命中改回 `false`。）
3. 打开 [src/shape.rs:60-61](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/shape.rs#L60-L61)，确认零宽度描边的 `if width > 0.0` 守卫。
4. 假设把第 61 行的守卫删掉（**仅作思考，不要真改源码**），问：一道 `thickness = 0pt` 的描边交给 tiny-skia 后会发生什么？（预期：tiny-skia 仍会按 0 宽度走一遍光栅化逻辑，产生空像素但白白消耗 CPU。这就是守卫的意义。）

**需要观察的现象 / 预期结果**：你能用一句话说出「为什么矩形的 `anti_alias` 要单独关掉」——因为轴对齐矩形的边缘天然落在整像素边界上，抗锯齿只会模糊它而非平滑它。

> 待本地验证：若你想亲见效果，可在本机用 typst CLI 导出一个纯色填充的矩形与一个带圆角的曲线形状（如 `circle()`）为 PNG，放大边缘对比锯齿感。本环境不运行编译，故标注待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：一个 `Shape` 同时设置了 `fill` 和 `stroke`，`render_shape` 会画几遍路径？顺序如何？

> **答案**：画两遍，**先填充后描边**。填充用 `fill_path`（[L52](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/shape.rs#L52)），描边用 `stroke_path`（[L79](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/shape.rs#L79)），共用同一条 `path`。因此描边会叠在填充之上。

**练习 2**：`to_sk_paint` 在填充调用里 `include_stroke_in_bbox=false`，在描边调用里却是 `matches!(paint, Paint::Gradient(_))`。为什么描边这里要区分渐变？

> **答案**：纯色描边不需要包围盒来采样（颜色处处相同），保持 `false` 即可；而**渐变描边**需要一块采样区域来计算颜色渐变，这块区域必须把描边的半宽也算进去，否则渐变会被截断或错位，故此时置 `true`。

---

### 4.2 convert_curve：把 Typst 曲线翻译成 tiny-skia 路径

#### 4.2.1 概念说明

`Geometry::Curve(Curve)` 是最通用的几何——任意折线与贝塞尔曲线都靠它表达。Typst 的 `Curve` 就是一个 `Vec<CurveItem>`，元素只有四种（[visualize/curve.rs:388-393](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/curve.rs#L388-L393)）：

- `Move(Point)`：把「画笔」移动到某点（不画线，开始一条新子路径）。
- `Line(Point)`：连一条直线到某点。
- `Cubic(p1, p2, p3)`：连一条**三次贝塞尔曲线**到 `p3`，`p1`/`p2` 是两个控制点。
- `Close`：闭合当前子路径（自动连回本子路径起点）。

tiny-skia 的 `sk::PathBuilder` 提供了几乎一一对应的方法：`move_to` / `line_to` / `cubic_to` / `close`。`convert_curve` 的职责就是**逐条元素做这次 1:1 翻译**，把 Typst 曲线变成 tiny-skia 路径。

为什么需要单独一层翻译？因为 Typst 与 tiny-skia 是两个独立 crate，它们的路径类型互不相通；`convert_curve` 是两者之间的**适配层（adapter）**。值得一提的是，它还被 `render_group` 复用来把裁剪曲线转成路径（u2-l2），所以它是 `pub` 的。

#### 4.2.2 核心流程

```
fn convert_curve(curve) -> Option<sk::Path>:
    builder = sk::PathBuilder::new()
    for elem in curve.0:
        match elem:
            Move(p)      => builder.move_to(p.x.to_f32(), p.y.to_f32())
            Line(p)      => builder.line_to(p.x.to_f32(), p.y.to_f32())
            Cubic(p1,p2,p3) => builder.cubic_to(p1.., p2.., p3..)   # 6 个 f32
            Close        => builder.close()
    builder.finish()        # 消耗 builder，产出 Option<sk::Path>
```

关键点：

- 每个 `Point` 的 `x` / `y` 都通过 `.to_f32()`（即 `AbsExt`）从 `Abs` 换算成 pt-f32。
- `cubic_to` 需要**两个控制点 + 终点**共 6 个坐标值，对应三次贝塞尔的标准定义。
- `builder.finish()` 返回 `Option<sk::Path>`：若路径为空（没有任何有效命令），返回 `None`，这正是 `render_shape` 里 `convert_curve(curve)?` 用 `?` 提前返回的原因——空曲线不画。

#### 4.2.3 源码精读

[src/shape.rs:87-113](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/shape.rs#L87-L113) —— `convert_curve` 全文。注意第 89 行 `for elem in &curve.0`：`Curve` 是个元组结构体 `Curve(pub Vec<CurveItem>)`，所以用 `.0` 取出内部 `Vec` 再遍历。四个分支与上面流程完全对应，其中 `Cubic` 分支（第 97-106 行）把三个点展开成 6 个 `to_f32()` 坐标传给 `cubic_to`。末尾 `builder.finish()` 收尾。

> 三次贝塞尔小知识：一条三次贝塞尔段由「起点（当前画笔位置）+ 两个控制点 \(p_1,p_2\) + 终点 \(p_3\)」定义，参数方程为
> \[ B(t) = (1-t)^3 P_0 + 3(1-t)^2 t\, P_1 + 3(1-t) t^2 P_2 + t^3 P_3,\quad t\in[0,1] \]
> tiny-skia 的 `cubic_to(p1, p2, p3)` 隐含 \(P_0\) 为当前画笔位置，与本翻译完全吻合。

#### 4.2.4 代码实践

**实践目标**：用 `Curve::rect` 的构造过程反推「一个矩形曲线」翻译成路径后的命令序列。

**操作步骤**（源码阅读型）：

1. 阅读 [visualize/curve.rs 中 `Curve::rect`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/curve.rs#L402-L412)（已知它依次调用 `move_` → `line` → `line` → `line` → `close`，画一个尺寸为 `size` 的矩形）。
2. 假设 `size = (100, 50)`，写出这条 `Curve` 经过 `convert_curve` 后，`PathBuilder` 收到的 5 条命令：
   - `move_to(0, 0)`
   - `line_to(100, 0)`
   - `line_to(100, 50)`
   - `line_to(0, 50)`
   - `close()`
3. 思考：这条「曲线矩形」与 4.1 里 `Geometry::Rect` 直接建的矩形，最终像素是否一致？（预期：几何上一致，但 `Rect` 走的是快路径、且填充时关掉抗锯齿；曲线矩形则保留抗锯齿。）

**需要观察的现象 / 预期结果**：你能说出 `convert_curve` 对「矩形曲线」产生的命令数 = `move + 3×line + close = 5` 条。

#### 4.2.5 小练习与答案

**练习 1**：`convert_curve` 为什么返回 `Option<sk::Path>` 而不是直接 `sk::Path`？

> **答案**：因为 `PathBuilder::finish()` 在路径为空（没有任何命令或命令无效）时返回 `None`。`render_shape` 用 `convert_curve(curve)?` 把 `None` 传播为「整个形状不画」（[L36](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/shape.rs#L36)），避免对空路径做无意义的光栅化。

**练习 2**：一个 `Curve` 里包含两个 `Move`，翻译后的路径有几条「子路径」？

> **答案**：两条。每个 `Move` 都开始一条新子路径；`Close` 只闭合当前子路径。tiny-skia 的 `sk::Path` 天然支持多条子路径，这正是 `FillRule`（NonZero/EvenOdd）能在自相交多子路径上发挥作用的根基。

---

### 4.3 描边的三个翻译器：线帽、连接、虚线

#### 4.3.1 概念说明

描边（stroke）的「怎么画」由 `FixedStroke` 的六个字段决定（[visualize/stroke.rs:626-639](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/stroke.rs#L626-L639)）：`paint`（颜色）、`thickness`（粗细）、`cap`（线帽）、`join`（连接）、`dash`（虚线）、`miter_limit`（斜接极限）。

其中 `paint` 与 `thickness` 在 `render_shape` 里已被处理（4.1.3），剩下 `cap` / `join` / `dash` / `miter_limit` 四项由三个小翻译器负责：

- `to_sk_line_cap`：线帽——一条**开放路径端点**的形状（平头 / 圆头 / 方头）。
- `to_sk_line_join`：连接——两条线段**交汇处**的拐角形状（斜接 / 圆角 / 斜切）。
- `to_sk_dash_pattern`：虚线——沿路径「画一段、空一段」的周期模式。

三个函数体都极短，本质上是在两套枚举/结构之间做**一一映射**，但其中 `to_sk_dash_pattern` 藏着一个不平凡的「奇数翻倍」处理，是本节重点。

先认清三个枚举的语义（[visualize/stroke.rs:448-478](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/stroke.rs#L448-L478)）：

| Typst `LineCap` | 含义 | Typst `LineJoin` | 含义 |
| --- | --- | --- | --- |
| `Butt` | 平头，端点处齐平（不外延） | `Miter` | 尖角（超过 `miter_limit` 则退化为 `Bevel`） |
| `Round` | 圆头，以端点为圆心的半圆 | `Round` | 圆角 |
| `Square` | 方头，向端点外延伸半个厚度 | `Bevel` | 斜切（直接用直线连两段端头） |

#### 4.3.2 核心流程

线帽与连接的翻译就是逐变体的 `match`，把 Typst 枚举原样映射到同名的 tiny-skia 枚举：

```
to_sk_line_cap(Butt)   => sk::LineCap::Butt
to_sk_line_cap(Round)  => sk::LineCap::Round
to_sk_line_cap(Square) => sk::LineCap::Square

to_sk_line_join(Miter) => sk::LineJoin::Miter
to_sk_line_join(Round) => sk::LineJoin::Round
to_sk_line_join(Bevel) => sk::LineJoin::Bevel
```

虚线翻译则多了一步「长度归一」：

```
to_sk_dash_pattern(dash) -> Option<sk::StrokeDash>:
    n = dash.array.len()
    len = if n 是奇数 { 2*n } else { n }          # 奇数翻倍
    arr = dash.array.iter().map(to_f32).cycle().take(len).collect()
    sk::StrokeDash::new(arr, dash.phase.to_f32())
```

#### 4.3.3 源码精读

线帽翻译器，三种一一对应：

[src/shape.rs:115-121](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/shape.rs#L115-L121) —— `to_sk_line_cap`。`Butt`/`Round`/`Square` 原样映射。

连接翻译器，同样三种：

[src/shape.rs:123-129](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/shape.rs#L123-L129) —— `to_sk_line_join`。`Miter`/`Round`/`Bevel` 原样映射。

虚线翻译器，本节重点：

[src/shape.rs:131-138](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/shape.rs#L131-L138) —— `to_sk_dash_pattern`。代码注释点明约束：「**tiny-skia 只允许偶数长度的虚线数组，而 pdf 允许任意长度**」。处理手法：

1. 第 134 行 `pattern_len = dash.array.len()`：原始数组长度 \(n\)。
2. 第 135 行 `len = if n % 2 == 1 { 2n } else { n }`：奇数则翻倍。
3. 第 136 行 `.iter().map(|l| l.to_f32()).cycle().take(len).collect()`：把原数组**循环**后截取前 `len` 个，得到偶数长度的新数组。
4. 第 137 行 `sk::StrokeDash::new(dash_array, dash.phase.to_f32())`：组装成 tiny-skia 虚线（`phase` 是起始相位偏移）。

**为什么必须偶数？** 虚线数组的语义是「dash, gap, dash, gap, …」交替：偶数下标是「画」的长度，奇数下标是「空」的长度。一个偶数长度数组天然以 gap 结尾，下一轮接 dash，衔接平滑。若数组是奇数（如 `[3]`），一轮结束时停在 dash，下一轮又从 dash 开始，语义上等价于「把数组重复一次再使用」——这正是 SVG/Canvas 规范的标准做法。所以代码在奇数时把 `[3]` 变成 `[3, 3]`（dash=3, gap=3），把 `[3, 2, 4]` 变成 `[3, 2, 4, 3, 2, 4]`，用 `.cycle().take(len)` 优雅地完成这次「自我拼接」。

> 为什么要兼容 pdf 的「任意长度」？因为 Typst 的 `FixedStroke` 是统一中间表示，既要喂给 tiny-skia（光栅，要求偶数），也要喂给 typst-pdf（矢量，允许任意）。本翻译器只在 typst-render 这一侧把奇数归一为偶数，pdf 那侧保持原样，所以需要这步适配。

#### 4.3.4 代码实践

**实践目标**：把 Typst 的 `FixedStroke` 字段一一对应到 tiny-skia 的 `sk::Stroke`，并亲手验证虚线「奇数翻倍」。

**操作步骤**：

1. 对照 [src/shape.rs:55-79](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/shape.rs#L55-L79) 与 [visualize/stroke.rs:626-639](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/stroke.rs#L626-L639)，填写下面这张「字段映射表」。

   **`FixedStroke` → `sk::Stroke` 字段映射表**：

   | `FixedStroke` 字段（Typst） | 类型 | 在 `render_shape` 中的处理 | `sk::Stroke` 对应字段 | 类型 |
   | --- | --- | --- | --- | --- |
   | `paint` | `Paint` | 经 `to_sk_paint(...)` 转成画笔，传给 `stroke_path` 的 `paint` 参数（**不**进 `sk::Stroke`） | ——（属 `sk::Paint`） | `sk::Paint` |
   | `thickness` | `Abs` | `width = thickness.to_f32()`（[L58](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/shape.rs#L58)） | `width` | `f32` |
   | `cap` | `LineCap` | `to_sk_line_cap(*cap)`（[L75](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/shape.rs#L75)） | `line_cap` | `sk::LineCap` |
   | `join` | `LineJoin` | `to_sk_line_join(*join)`（[L76](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/shape.rs#L76)） | `line_join` | `sk::LineJoin` |
   | `dash` | `Option<DashPattern<Abs,Abs>>` | `.as_ref().and_then(to_sk_dash_pattern)`（[L62](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/shape.rs#L62)） | `dash` | `Option<sk::StrokeDash>` |
   | `miter_limit` | `Ratio` | `miter_limit.get() as f32`（[L77](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/shape.rs#L77)） | `miter_limit` | `f32` |

2. 用 `to_sk_dash_pattern` 的逻辑手算三个例子（设 `phase = 0`）：
   - 输入 `array = [3pt, 2pt]`（偶数）→ `len = 2` → 输出 `[3, 2]`
   - 输入 `array = [3pt]`（奇数）→ `len = 2` → `cycle().take(2)` → 输出 `[3, 3]`
   - 输入 `array = [3pt, 2pt, 4pt]`（奇数）→ `len = 6` → `cycle().take(6)` → 输出 `[3, 2, 4, 3, 2, 4]`

**需要观察的现象 / 预期结果**：你能回答——「为什么 `to_sk_dash_pattern` 要把奇数长度数组翻倍？」答：因为 tiny-skia 要求虚线数组长度为偶数（dash/gap 必须成对），而 Typst（兼容 pdf）允许任意长度；奇数数组按 SVG 规范「重复一次」即可变为语义等价的偶数数组，`.cycle().take(len)` 正好实现了这次自我拼接。

> 待本地验证：若想亲见虚线效果，可在本机用 typst 画一个 `rect(stroke: 1pt + black, dash: "dashed")` 并导出 PNG。`"dashed"` 在 Typst 中解析为 `[3pt, 3pt]`（[visualize/stroke.rs:547](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/stroke.rs#L547)），是偶数，不触发翻倍。本环境不运行编译，故标注待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`miter_limit` 为什么用 `.get() as f32`，而 `thickness` 用 `.to_f32()`？

> **答案**：二者类型不同。`miter_limit` 是 `Ratio`（无量纲比值），用 `.get()` 取出内部 `f64` 再 `as f32`；`thickness` 是 `Abs`（带单位的长度），必须经 `AbsExt::to_f32()`（即 `to_pt() as f32`）把任意长度单位换算成 pt-f32。一个是「取数值」，一个是「换单位」。

**练习 2**：若用户给了一个奇数长度虚线 `array = [5pt, 3pt, 2pt]`，最终传给 tiny-skia 的数组是什么？`phase` 如何处理？

> **答案**：`n = 3`（奇数），`len = 6`，`[5,3,2].cycle().take(6) = [5, 3, 2, 5, 3, 2]`。`phase` 不参与翻倍，原样经 `dash.phase.to_f32()` 传给 `sk::StrokeDash::new` 的第二个参数。

**练习 3**：`to_sk_dash_pattern` 返回 `Option<sk::StrokeDash>`，而 `sk::StrokeDash::new` 也返回 `Option`。什么情况下会得到 `None`？

> **答案**：`sk::StrokeDash::new` 在数组为空或含非法值（如负数、NaN）时返回 `None`。`render_shape` 用 `dash.as_ref().and_then(to_sk_dash_pattern)` 把它兜住——`stroke.dash` 为 `None`（实线）时 `as_ref()` 得 `None`，整个表达式为 `None`，`sk::Stroke.dash` 即为 `None`，表示「不使用虚线，画实线」。

---

## 5. 综合实践

**任务**：跟踪一条「带虚线描边、纯色填充的负尺寸矩形」完整走过 `render_shape` 的全过程，把本讲三个模块串起来。

设 Typst 源码画了这样一个形状（仅作思考用，不必编译）：

- 几何：`Geometry::Rect(Size { x: -80pt, y: 40pt })`（负宽度矩形）
- 填充：`Paint::Solid(纯色)`
- 描边：`FixedStroke { paint: 纯色, thickness: 2pt, cap: Round, join: Miter, dash: Some([5pt, 3pt, 2pt]), miter_limit: 4.0 }`

请按顺序回答：

1. **路径构造**（4.1）：负尺寸分支会被命中吗？命中后 `signum` 变换是什么？最终矩形角点落在哪？
   > 参考答案：命中。\(w=-80,h=40\)，`signum = (-1, +1)`；正矩形角点 \((0,0),(80,40)\)，经 \((-1,+1)\) 缩放后变为 \((0,0),(-80,40)\)，矩形镜像到原点左侧。

2. **填充**（4.1）：因为是矩形，`anti_alias` 会被设成什么？`include_stroke_in_bbox` 是 `true` 还是 `false`？
   > 参考答案：`anti_alias = false`（矩形快路径）；填充调用的 `include_stroke_in_bbox = false`。

3. **描边**（4.3）：写出最终组装出的 `sk::Stroke` 各字段值；虚线数组经「奇数翻倍」后是什么？
   > 参考答案：`width = 2.0`、`line_cap = Round`、`line_join = Miter`、`dash = Some(StrokeDash { array: [5,3,2,5,3,2], phase: 0 })`、`miter_limit = 4.0`。原始 `[5,3,2]` 长度 3 为奇数，翻倍成 6，经 `cycle().take(6)` 得 `[5,3,2,5,3,2]`。

4. **复用路径**（4.1+4.2）：填充和描边用的是同一条 `path` 吗？这条路径在像素化时由哪个变换定位？
   > 参考答案：是同一条。`fill_path` 与 `stroke_path` 都传入 `ts = state.transform`，由它（含 pt→像素的缩放与累积平移）把路径整体落到画布像素，`state.mask` 一并带上裁剪遮罩。

**预期结果**：完成本题后，你应该能从「一个 `Shape` 结构」一路讲到「`fill_path` / `stroke_path` 的入参」，把几何翻译、负尺寸适配、填充规则、描边组装、虚线翻倍全部串成一条完整链路。

## 6. 本讲小结

- `render_shape` 的范式是「**一路径，两遍绘制**」：先把 `Geometry`（`Line`/`Rect`/`Curve`）归一化成一条 `sk::Path`，再分别 `fill_path` 与 `stroke_path`，两者复用同一条路径。
- **负尺寸矩形**靠 `from_scale(w.signum(), h.signum())` 做一次纯镜像变换来兼容 tiny-skia，且只在确有负尺寸时才付这次「较贵」的变换代价。
- `convert_curve` 是 Typst `Curve` 与 tiny-skia `PathBuilder` 之间的 1:1 适配层，处理 `Move/Line/Cubic/Close` 四种元素；空路径返回 `None` 被 `?` 传播为「不画」。
- 矩形填充会**关闭抗锯齿**（`anti_alias = false`）走快路径；**零宽度描边**直接跳过（`if width > 0.0`）。
- 描边由三个翻译器完成：`to_sk_line_cap` / `to_sk_line_join` 是同名枚举一一映射；`to_sk_dash_pattern` 多了一步「**奇数长度翻倍**」以满足 tiny-skia「虚线数组必须偶数」的约束（`.cycle().take(len)`）。
- `FixedStroke` 的六个字段中，`paint` 经 `to_sk_paint` 成为画笔参数，`thickness/cap/join/dash/miter_limit` 组装进 `sk::Stroke`；注意 `Abs` 用 `.to_f32()`、`Ratio` 用 `.get() as f32`。

## 7. 下一步学习建议

本讲只处理了**纯色**的填充与描边。接下来建议：

1. **渐变填充**（u3-l1）：阅读 `paint.rs` 里 `to_sk_paint` 的 `Paint::Gradient` 分支与 `GradientSampler`，看渐变如何在 `render_shape` 画出的路径区域内逐像素采样，以及 `include_stroke_in_bbox` 在渐变描边时为何要置 `true`。
2. **平铺图案**（u3-l2）：阅读 `Paint::Tiling` 分支与 `TilingSampler`，理解图案如何像虚线一样做周期性重复。
3. **字形光栅化**（u3-l3）：文本的轮廓字形本质上也是「路径填充」，可以对比 `render_shape` 与 `render_outline_glyph` 的异同，看 typst-render 为何对文字另走一条 pixglyph 光栅化路径。
4. 想加深对曲线的理解，可回到 `typst-library` 的 [visualize/curve.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/curve.rs)，阅读 `Curve` 是如何由 `CurveElem`（高层语义）解析为底层 `CurveItem` 序列的。

# 图形与几何：handle_shape

## 1. 本讲目标

本讲聚焦 `typst-pdf` 中**图形（Shape）的导出**。学完本讲，你应当能够：

- 说清楚 `handle_shape()` 作为图形绘制统一入口的完整执行步骤：发射 tagged PDF 标记、压入变换、转填充、转描边、绘制路径。
- 解释 `convert_geometry()` 如何把 Typst 的三种几何体（`Line` / `Rect` / `Curve`）翻译成 krilla 的 `Path`。
- 理解**负尺寸矩形**为何需要用 `scale(signum)` 变换来「镜像还原」，并能手算一个具体例子。
- 理解 `handle_shape()` 对「填充为空」「描边厚度为 0」的判空逻辑，以及为什么必须这样做（避免 krilla 默认黑色填充）。
- 串联起 `util::convert_path()` 如何把 `Curve` 的四类线段翻译成 PDF 路径算符。

本讲只精读两个核心文件：`src/shape.rs` 与 `src/util.rs`（其中的 `convert_path`）。其余如 `paint.rs`、`tags/mod.rs`、`convert.rs` 仅作为周边上下文引用，深入内容留在后续讲义。

## 2. 前置知识

在进入源码前，先用通俗语言建立三个基础概念。

### 2.1 Shape、Geometry 与 FixedStroke 是什么

Typst 把「一个要画的东西」抽象成 `Shape`，定义在 `typst-library` 中：

```rust
pub struct Shape {
    pub geometry: Geometry,   // 画什么形状
    pub fill: Option<Paint>,  // 内部填充（None 表示不填）
    pub fill_rule: FillRule,  // 填充规则（非零 / 奇偶）
    pub stroke: Option<FixedStroke>, // 边框描边（None 表示不描）
}
```

而「画什么形状」就是 `Geometry` 枚举，只有三种变体：

```rust
pub enum Geometry {
    Line(Point),   // 从原点连到某点的直线
    Rect(Size),    // 以左上角为原点的矩形
    Curve(Curve),  // 由移动/直线/贝塞尔组成的自由曲线
}
```

> 见 [crates/typst-library/src/visualize/shape.rs:366-L373](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/visualize/shape.rs#L366-L373)（`Geometry` 枚举与字段说明，typst-library 侧）。

一个 `Shape` = 一个 `Geometry` + 可选的 `fill` + 可选的 `stroke`。`typst-pdf` 的工作就是把它翻译成 krilla 能理解的「一条路径 + 一个填充 + 一个描边」。

### 2.2 PDF 里的路径、填充、描边

PDF 绘图的基础是**路径（path）**：由若干子路径组成，每条子路径由 `move_to`（抬笔移动）、`line_to`（直线）、`cubic_to`（三次贝塞尔曲线）、`close`（闭合）四种操作拼接而成。

一条路径有两种用法：

- **填充（fill）**：把路径围成的区域涂上颜色；
- **描边（stroke）**：沿着路径画一条有粗细的边线。

`typst-pdf` 不直接拼 PDF 字节，而是调用 krilla 的 `PathBuilder` 构造路径、调用 `Surface::draw_path` 绘制。

### 2.3 坐标系：为什么转换是「一一对应」的

Typst 的坐标系是**原点在左上、y 轴向下**。krilla 的 `Surface` 对外呈现的也是同样的 y 向下坐标系（它在内部处理 PDF 底层 y 向上的翻转）。因此你在本讲看到的所有坐标转换都是「一一对应、不做 y 轴翻转」的——这正是 `u2-l8` 讲过的 `PointExt`/`SizeExt`/`convert_path` 直接搬运坐标的原因。

> 前置依赖：本讲承接 `u2-l7`（Frame 遍历器、`State`/`FrameContext`、变换状态栈、`handle_frame` 的分派、两套栈模型）与 `u2-l8`（`AbsExt::to_f32`、`convert_path`、扩展 trait 模式）。如对「为什么 `handle_shape` 会被调用」或「`fc.state().transform()` 是什么」感到陌生，请先复习这两讲。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用到的地方 |
|------|------|----------------|
| `src/shape.rs` | 图形翻译器（本讲主角） | `handle_shape()`、`convert_geometry()` |
| `src/util.rs` | 横向复用的类型转换工具箱 | `convert_path()`（承接 `u2-l8`） |
| `src/convert.rs` | 总调度 / Frame 遍历器 | `handle_frame()` 里对 `handle_shape` 的两处调用、`State::transform()` |
| `src/paint.rs` | 颜色与绘制状态翻译器（周边） | `convert_fill()`、`convert_stroke()` 签名 |
| `src/tags/mod.rs` | tagged PDF 标记发射（周边） | `tags::shape()`、`TagHandle` |

## 4. 核心概念与源码讲解

### 4.1 handle_shape()：图形绘制的统一入口

#### 4.1.1 概念说明

`handle_shape()` 是所有图形导出的**唯一入口**。无论是用户在 Typst 里写的 `#rect()`、`#line()`、`#path()`，还是 `convert.rs` 在画页面**背景填充**时临时构造的矩形，最终都会汇聚到这一个函数。

它要解决三个问题：

1. **位置**：这个图形在页面的哪里？（由 `FrameContext` 累计的 `transform` 给出）
2. **无障碍标记**：如果开启了 tagged PDF，这个图形要包进一个 Artifact（装饰物）标记里，让屏幕阅读器知道「这不是正文」。
3. **样式**：要填充吗？要描边吗？厚度是多少？（注意过滤掉无意义的样式，避免 krilla 的默认行为捣乱）

#### 4.1.2 核心流程

`handle_shape()` 的执行可以拆成五步：

```
1. 发射 tagged 标记   → tags::shape() 返回 TagHandle（RAII，drop 时自动收尾）
2. 压入图形状态        → set_location（错误定位）+ push_transform（位移到正确位置）
                       用 defer 保证配对 pop
3. 把几何体转成路径     → convert_geometry(&shape.geometry) → Option<Path>
4. 转换填充与描边       → convert_fill / convert_stroke
                       （关键：过滤掉 thickness ≤ 0 的描边）
5. 判空后再绘制         → 仅当 fill 或 stroke 至少有一个时才 draw_path
                       （关键：避免 krilla 默认黑色填充）
```

注意第 2 步里有两套栈的配合（`u2-l7` 讲过）：`fc.state().transform()` 是 Typst 侧的变换簿记，`push_transform`/`pop` 操作的是 krilla `Surface` 的图形状态栈。`handle_shape` 只动后者。

#### 4.1.3 源码精读

先看函数签名与开头的标记/变换处理：

[crates/typst-pdf/src/shape.rs:13-L30](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/shape.rs#L13-L30) — `handle_shape` 开头：先用 `tags::shape()` 发射 tagged PDF 标记（返回 RAII 的 `TagHandle`），再 `set_location` 把 Typst `Span` 传给 krilla 以支持错误定位，接着 `push_transform` 把累计变换压入 krilla 图形状态栈，最后用 `defer` 保证绘制结束后 `pop` 并 `reset_location`。

这里 `tags::shape()` 返回的 `TagHandle` 用到了 RAII 模式：它持有 `&mut Surface`，在 `Drop` 时若 `started` 为真就调用 `surface.end_tagged()` 收尾标记内容序列。`handle` 通过 `.surface()` 方法借出 `&mut Surface` 供后续绘制使用——

[crates/typst-pdf/src/tags/mod.rs:177-L197](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/tags/mod.rs#L177-L197) — `TagHandle` 结构体与 `Drop` 实现：`started` 标记是否真的开启了标记内容序列（tagging 关闭或父级已是 artifact 时为 `false`，此时 drop 什么都不做），`surface()` 借出可变 surface 引用。

接下来是核心的「几何转路径 + 转样式 + 绘制」三段：

[crates/typst-pdf/src/shape.rs:32-L72](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/shape.rs#L32-L72) — `handle_shape` 的主体：先把 `shape.geometry` 转成 `Option<Path>`；填充部分仅在 `shape.fill` 为 `Some` 时调用 `paint::convert_fill`；描边部分先用 `and_then` 过滤掉 `thickness ≤ 0` 的描边，再 `convert_stroke`；最后仅当「fill 或 stroke 至少一个非空」时才 `set_fill` / `set_stroke` / `draw_path`。

这段代码有两个**必须理解的设计点**，它们直接对应本讲的学习目标：

**设计点 A：过滤掉厚度为 0 的描边**

```rust
let stroke = shape.stroke.as_ref().and_then(|stroke| {
    if stroke.thickness.to_f32() > 0.0 { Some(stroke) } else { None }
});
```

为什么要过滤？因为在 PDF 规范里，**线宽为 0 会被当作「设备能画出的最细线条」（通常是 1 个设备像素）**，而不是「不画」。Typst 的语义是「厚度 0 = 没有描边」，所以必须主动剔除，否则会画出一条意想不到的细线。

**设计点 B：判空后再绘制，避免 krilla 默认黑色填充**

```rust
// Otherwise, krilla will by default fill with a black paint.
if fill.is_some() || stroke.is_some() {
    surface.set_fill(fill);
    surface.set_stroke(stroke);
    surface.draw_path(&path);
}
```

这里有两层防护：

- 外层 `if` 守卫：当 fill 与 stroke **都为 None**（比如一个既没填色、描边又被过滤掉的图形）时，干脆**跳过 `draw_path`**，否则 krilla 会用默认的黑色把这个路径填出来。
- 内层 `set_fill(fill)` / `set_stroke(stroke)`：在绘制前**显式地把填充和描边都设置一次**（哪怕其中一个传 `None`）。这样 krilla 的图形状态是明确的：`set_fill(None)` 表示「不要填充」，而不是「用默认黑色填充」。

> 小结：`set_fill(None)` 主动说「别填」，而「不调用 `set_fill` 就直接 `draw_path`」会让 krilla 用默认黑色。所以代码的策略是「**要么显式设置全套样式后绘制，要么完全不绘制**」。

最后看 `handle_shape` 是从哪里被调用的——`convert.rs` 的 `handle_frame()` 里有**两处**调用，分别对应「用户图形」和「页面背景」：

[crates/typst-pdf/src/convert.rs:342-L367](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L342-L367) — `handle_frame` 中对 `handle_shape` 的两处调用：上方 `Geometry::Rect(frame.size() + padding).filled(fill)` 用 `handle_shape` 画页面背景（`artifact_type` 为 `Background`，`span` 为 `detached`）；下方 `FrameItem::Shape` 分派用 `handle_shape` 画用户图形（`artifact_type` 为 `Layout`）。注意背景用的是临时构造的 `Shape`，复用了同一套绘制逻辑。

这就印证了本讲开头的论断：**背景填充和用户图形共用同一个 `handle_shape`**，只是 `artifact_type`（影响 tagged PDF 里 Artifact 的种类标记）不同。

#### 4.1.4 代码实践

**实践目标**：跟踪一个「带描边但厚度为 0」的图形在 `handle_shape` 中的命运。

**操作步骤**（源码阅读型）：

1. 设想一个 Typst 图形：`#rect(width: 40pt, height: 20pt, stroke: 0pt)`，即有 `stroke` 字段但 `thickness = 0pt`，且无 `fill`。
2. 在 [shape.rs:47-L49](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/shape.rs#L47-L49) 处跟踪：`shape.stroke` 是 `Some(...)`，进入 `and_then`，但 `stroke.thickness.to_f32()` 即 `0.0 > 0.0` 为假，于是闭包返回 `None`，`stroke` 变量最终为 `None`。
3. 由于 `shape.fill` 也是 `None`，`fill` 为 `None`。
4. 在 [shape.rs:67-L71](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/shape.rs#L67-L71) 处：`fill.is_some() || stroke.is_some()` 即 `false || false`，整个 `if` 不执行，**`draw_path` 不会被调用**。

**需要观察的现象 / 预期结果**：这个图形在最终 PDF 里**什么都不画**（既不填色也不描边）。这正是 Typst 的语义。

**对比实验**（待本地验证）：若把 `stroke` 改成 `0.5pt`（去掉上面第 2 步的过滤），预期会看到一条 0.5pt 的边框。可以用 Typst 分别导出这两个版本，用 PDF 查看器或 `mutool trace` 对比绘制算符差异。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `handle_shape` 在 `draw_path` 之前要同时调用 `set_fill(fill)` 和 `set_stroke(stroke)`，而不是只设置存在的那一项？

**参考答案**：为了把 krilla 的图形状态置为**完全明确**。若只设置了 stroke 却不调用 `set_fill(None)`，krilla 可能沿用默认状态（默认黑色填充），导致描边图形被意外填上黑色。显式 `set_fill(None)` 等于告诉 krilla「这个路径只描边、不填充」。

**练习 2**：`handle_shape` 里 `surface.push_transform(&fc.state().transform().to_krilla())` 的 `transform` 反映了什么？为什么需要 `defer` 来 `pop`？

**参考答案**：`fc.state().transform()` 是 `FrameContext` 累计到当前图形的**全部平移/变换簿记**（见 `u2-l7` 的 `State`），它决定了图形画在页面的哪个位置。`push_transform` 把它压入 krilla 的图形状态栈；`defer` 利用 RAII 保证函数返回时（无论正常返回还是 `?` 提前返回出错）都执行 `pop`，让 krilla 栈与 Typst 侧状态保持对称、不污染后续绘制。

---

### 4.2 convert_geometry()：把几何体变成 PDF 路径

#### 4.2.1 概念说明

`convert_geometry()` 是一个私有函数，负责把 `Geometry` 翻译成 krilla 的 `Path`。它返回 `Option<Path>`——当几何体退化（例如矩形尺寸为 0 导致 `PathBuilder::finish` 返回 `None`）时返回 `None`，此时 `handle_shape` 外层的 `if let Some(path)` 会跳过整个绘制。

三种几何体的处理难度不同：

- `Line`：两段操作（`move_to` + `line_to`），最简单。
- `Rect`：用 `PathBuilder::push_rect` 一步推入，但要处理 Typst 特有的**负尺寸**。
- `Curve`：委托给 `util::convert_path`，逐段翻译（见 4.3 节）。

#### 4.2.2 核心流程

```
convert_geometry(geometry) -> Option<Path>:
  新建 PathBuilder
  match geometry:
    Line(p)  → move_to(0,0); line_to(p.x, p.y)
    Rect(s)  → 处理可能的负尺寸 → push_rect(修正后的 Rect)
    Curve(c) → convert_path(c, &mut builder)   # 逐段
  builder.finish()   # 返回 Option<Path>
```

#### 4.2.3 源码精读

完整函数：

[crates/typst-pdf/src/shape.rs:77-L109](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/shape.rs#L77-L109) — `convert_geometry`：按 `Geometry` 三变体分派，最后 `path_builder.finish()` 收尾。

**`Line` 分支**最直白：从原点 `(0,0)` 连到目标点。注意线段的起点是固定的原点——线段在 Frame 里的实际位置由 `handle_shape` 压入的 `transform` 负责，`convert_geometry` 只管「形状本身」。

**`Curve` 分支**只是一行委托：`convert_path(c, &mut path_builder)`，把逐段翻译的工作交给 `util.rs`（见 4.3 节）。

**`Rect` 分支**是本节重点，它要处理 Typst 支持但 krilla 不直接支持的**负尺寸**：

[crates/typst-pdf/src/shape.rs:85-L102](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/shape.rs#L85-L102) — `Geometry::Rect` 分支：当 `w` 或 `h` 为负时，先建一个取绝对值的矩形，再用 `Transform::from_scale(w.signum(), h.signum())` 做镜像变换来还原负尺寸的视觉效果。

代码里的注释点明了动机：「krilla doesn't normally allow for negative dimensions, but Typst supports them, so we apply a transform if needed.」（krilla 通常不允许负尺寸，但 Typst 支持，所以需要时套一个变换。）

**为什么负尺寸需要镜像？** 在 Typst 里，一个 `Rect(Size::new(-80pt, 60pt))` 表示「以原点为右上角、向左延伸 80pt、向下延伸 60pt」的矩形——即宽度的负号代表**沿 x 轴翻转**。而 krilla 的 `Rect::from_xywh` 只接受非负宽高（负值会返回 `None`）。于是策略是：

1. 先用绝对值建一个「正常」矩形：`Rect::from_xywh(0, 0, |w|, |h|)`；
2. 再用一个**缩放变换**把负的那根轴翻转回来。

缩放矩阵为：

\[
S = \begin{pmatrix} \operatorname{sign}(w) & 0 \\ 0 & \operatorname{sign}(h) \end{pmatrix}
\]

它把任意点 \((x, y)\) 映射为 \((\operatorname{sign}(w)\cdot x,\ \operatorname{sign}(h)\cdot y)\)。当某根轴的尺寸为负时，`signum` 返回 \(-1\)，该轴被镜像；为正时返回 \(1\)，不变。

**手算一个具体例子**：取 `w = -80pt`，`h = 60pt`。

- 进入负尺寸分支：`transform = from_scale(signum(-80), signum(60)) = from_scale(-1, 1)`。
- `Rect::from_xywh(0, 0, 80, 60)` 得到角点在 \(x\in[0,80],\ y\in[0,60]\) 的矩形。
- 施加变换 \(S\)：\(x\) 乘 \(-1\)，\(y\) 不变，角点变为 \(x\in[-80,0],\ y\in[0,60]\)。
- 结果：一个「从原点向左延伸 80pt、向下延伸 60pt」的矩形——正是负宽度在 Typst 中的含义。

最后 `push_rect` 把这个修正后的矩形推进 `PathBuilder`。注意整段用 `if let Some(rect) = rect` 兜底：若 `from_xywh` 或 `transform` 因退化尺寸返回 `None`，就**什么都不推**，`finish()` 随后返回 `None`，`handle_shape` 跳过绘制。

#### 4.2.4 代码实践

**实践目标**：亲手验证负尺寸矩形的变换修正，画出修正前后的矩形角点。

**操作步骤**：

1. 取三个例子，分别写出 `transform` 与修正后矩形的角点范围（坐标单位 pt，原点左上、y 向下）：

   | 输入 `(w, h)` | `from_scale(signum(w), signum(h))` | 绝对值矩形角点 | 修正后矩形角点 |
   |---------------|-----------------------------------|----------------|----------------|
   | `(-80, 60)` | `(-1, 1)` | `x∈[0,80], y∈[0,60]` | `x∈[-80,0], y∈[0,60]` |
   | `(80, -60)` | `(1, -1)` | `x∈[0,80], y∈[0,60]` | `x∈[0,80], y∈[-60,0]` |
   | `(-80, -60)` | `(-1, -1)` | `x∈[0,80], y∈[0,60]` | `x∈[-80,0], y∈[-60,0]` |

2. 对照源码 [shape.rs:88-L94](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/shape.rs#L88-L94)，确认你的推导与 `Rect::from_xywh(0.0, 0.0, w.abs(), h.abs()).and_then(|rect| rect.transform(transform))` 一致。

**预期结果**：三例中负的那根轴都被镜像翻转，正的那根轴不变，矩形视觉上「朝着负尺寸的方向延伸」。这个练习能让你确信 `signum` 变换精确复刻了 Typst 的负尺寸语义。

#### 4.2.5 小练习与答案

**练习 1**：如果 `w = 0.0`（正好为 0，不是负），会走哪个分支？结果如何？

**参考答案**：`w < 0.0 || h < 0.0` 为假（0 不小于 0），走 `else` 分支 `Rect::from_xywh(0, 0, 0, h)`。注意 `from_xywh` 的返回类型是 `Option<Rect>`（这一点从源码里的 `.and_then(...)` 与 `if let Some(rect) = rect` 守卫可以确认）。对于这种零宽的退化矩形，它**预期**会返回 `None`，于是 `if let Some(rect)` 不命中、`push_rect` 不执行，`finish()` 随后返回 `None`，`handle_shape` 跳过绘制。即便 krilla 在退化尺寸上返回的是 `Some`（极不可能），代码外层「fill/stroke 判空」的守卫也会兜住——一个零宽且无有效样式的图形无论如何都不会产生可见输出。

> 待本地验证：krilla 的 `Rect::from_xywh` 对零/负宽高的精确返回值需查阅 krilla 源码确认；本练习结论基于「它返回 `Option` 且代码用 `if let Some` 兜底」这一可观察事实。

**练习 2**：为什么 `Line` 分支里起点写死 `(0, 0)`，而不是线段的真实位置？

**参考答案**：`convert_geometry` 只负责「形状的几何描述」，不负责「形状放在页面哪里」。线段的真实位置由 `handle_shape` 通过 `push_transform(&fc.state().transform())` 提供给 krilla。这种「几何归几何、位置归变换」的解耦，让同一个 `convert_geometry` 能在任何位置复用。

---

### 4.3 convert_path()：曲线段到 PDF 算符（util.rs）

#### 4.3.1 概念说明

`util::convert_path()` 是 `u2-l8` 已经介绍过的类型转换工具箱的一员，但因为它直接服务于 `Geometry::Curve`，本讲把它作为第三个最小模块完整讲一遍。

它的任务：把 Typst 的 `Curve`（一条自由曲线，由若干 `CurveItem` 段组成）翻译成 krilla `PathBuilder` 上的操作序列。`Curve` 内部就是一个 `CurveItem` 的列表（`path.0`），每一段是下列四种之一：

- `Move(p)`：抬笔移动到点 `p`（开始一条新的子路径）；
- `Line(p)`：从当前点画直线到 `p`；
- `Cubic(p1, p2, p3)`：从当前点画三次贝塞尔曲线，`p1`/`p2` 为两个控制点、`p3` 为终点；
- `Close`：闭合当前子路径。

#### 4.3.2 核心流程

`convert_path` 采用 `u2-l8` 讲过的「**追加式**」设计：接收一个**已存在的** `&mut PathBuilder`，把段一段段追加进去，而不是自己新建 builder 再返回。这样 `convert_geometry` 可以先复用同一个 `PathBuilder` 做别的操作（虽然当前 `Curve` 分支只调一次，但这个签名留出了组合的余地）。

```
convert_path(curve, builder):
  for item in curve.0:
    Move(p)      → builder.move_to(p.x, p.y)
    Line(p)      → builder.line_to(p.x, p.y)
    Cubic(p1,p2,p3) → builder.cubic_to(p1, p2, p3)
    Close        → builder.close()
```

四种 `CurveItem` 与 PDF 路径算符一一对应：`m`（move）、`l`（line）、`c`（cubic bezier）、`h`（close）。注意 PDF 只支持**三次**贝塞尔曲线，因此没有二次贝塞尔的分支——Typst 侧若需要二次曲线，会在上游转换成三次。

#### 4.3.3 源码精读

[crates/typst-pdf/src/util.rs:208-L225](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/util.rs#L208-L225) — `convert_path`：遍历 `curve.0` 的每个 `CurveItem`，用穷尽 `match` 分派到 `PathBuilder` 的四种方法，坐标统一用 `AbsExt::to_f32`（即 `to_pt() as f32`）转成 PDF 点。

几个要点：

- **穷尽 match**：四种 `CurveItem` 全部覆盖，编译器保证未来新增变体时编译失败，是类型安全的保证（与 `u2-l8` 里 `LineCapExt` 等同思路）。
- **`Cubic` 的三点语义**：`cubic_to(p1, p2, p3)` 接收「两控制点 + 终点」，**起点是隐式的当前点**（由上一段 `Move`/`Line`/`Cubic` 决定），所以这里不传起点。
- **统一换算**：所有坐标经 `to_f32()`（`Abs → pt → f32`），与 `convert_geometry` 里 `Rect` 用 `to_f32()`、`Line` 用 `l.x.to_f32()` 完全一致，本 crate 的绝对长度一律走这条换算路径。

#### 4.3.4 代码实践

**实践目标**：用一个具体的 `Curve` 跟踪 `convert_path` 产生的操作序列。

**操作步骤**：

1. 设想一条曲线由四段组成：`Move((0,0))`、`Line((10,0))`、`Cubic((15,5),(15,15),(10,20))`、`Close`。
2. 跟踪 [util.rs:211-L223](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/util.rs#L211-L223) 的 `match`，写出 `PathBuilder` 收到的调用序列：

   ```
   move_to(0, 0)
   line_to(10, 0)
   cubic_to(15, 5, 15, 15, 10, 20)   # 当前点(10,0)为隐式起点
   close()
   ```

3. 注意 `Cubic` 段只传了三个点（两控制点 + 终点），起点 `(10, 0)` 来自上一段的终点，没有显式传递。

**预期结果**：你得到一条「从原点向右到 (10,0)，再用一段三次贝塞尔弯到 (10,20)，最后闭合回原点」的子路径。这能帮你建立「`CurveItem` 序列 ↔ `PathBuilder` 调用序列」的一一对应直觉。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `convert_path` 接收 `&mut PathBuilder` 而不是自己 `PathBuilder::new()` 再返回 `Path`？

**参考答案**：这是「追加式」设计：调用方（`convert_geometry`）已经持有一个 `PathBuilder` 并会在最后统一 `finish()`。把 builder 作为参数传入，让 `convert_path` 可以与其他路径操作（如 `push_rect`）在同一个 builder 上组合，职责更清晰，也避免了「多次 finish / 多个 Path 再拼接」的麻烦。

**练习 2**：`Cubic(p1, p2, p3)` 里的三个点分别是什么？为什么没有第四个「起点」参数？

**参考答案**：`p1`、`p2` 是两个控制点，`p3` 是终点。起点是**隐式的当前点**——即上一段操作（`Move`/`Line`/`Cubic`）留下的落笔位置。路径构造天然是「连续描线」的过程，每一段都接续上一段的终点，所以不需要重复传起点。

---

## 5. 综合实践

把本讲三个模块串起来，跟踪一个「刁钻」的图形从 Frame 到 PDF 的全过程。

**任务**：设想一个 Typst 图形 `Shape`，满足：

- `geometry = Geometry::Rect(Size::new(-40pt, 30pt))`（负宽度矩形）；
- `fill = None`（不填色）；
- `stroke = Some(FixedStroke { thickness: 0pt, paint: black, ... })`（有描边字段，但厚度为 0）。

请按顺序回答：

1. `handle_shape` 调用 `convert_geometry` 后得到什么 `Path`？写出 `Rect` 分支里 `transform`、绝对值矩形角点、修正后矩形角点。
2. `fill` 变量的值是什么？`stroke` 变量经过 [shape.rs:47-L49](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/shape.rs#L47-L49) 过滤后的值是什么？
3. 最终 `draw_path` 会被调用吗？为什么？这个图形在 PDF 里是否可见？

**参考解答**：

1. `w = -40pt`、`h = 30pt`，进入负尺寸分支：`transform = from_scale(-1, 1)`；绝对值矩形 `Rect::from_xywh(0, 0, 40, 30)` 角点为 `x∈[0,40], y∈[0,30]`；修正后角点为 `x∈[-40,0], y∈[0,30]`。`push_rect` 推入该矩形，`finish()` 返回 `Some(path)`。
2. `fill = None`（因为 `shape.fill` 是 `None`）。`stroke`：`shape.stroke` 虽是 `Some`，但 `thickness.to_f32() = 0.0`，`0.0 > 0.0` 为假，闭包返回 `None`，故 `stroke = None`。
3. `draw_path` **不会被调用**：`fill.is_some() || stroke.is_some()` 即 `false || false`。于是即便路径已成功构造，也因「无有效样式」被整体跳过——这个图形在 PDF 里**完全不可见**。这正是设计点 A（过滤 0 厚度描边）与设计点 B（判空后再绘制）协同作用的典型场景：没有它们，krilla 会用默认黑色把这块矩形填出来，产生一个本不该出现的黑色块。

**延伸**（待本地验证）：把 `stroke` 改成 `thickness: 1pt` 重跑第 2、3 步，预期 `stroke` 变为 `Some`，`draw_path` 被调用，PDF 里出现一个向左延伸的矩形描边轮廓。可用 Typst 实际导出并对比。

## 6. 本讲小结

- `handle_shape()` 是所有图形导出的**唯一入口**，用户图形与页面背景填充共用它，差别仅在 `artifact_type`（`Layout` vs `Background`）。
- 它的执行链是「发射 tagged 标记（RAII `TagHandle`）→ 压入变换（`push_transform` + `defer` 配对 `pop`）→ `convert_geometry` 转路径 → 转填充/描边 → 判空后 `draw_path`」。
- **判空是关键**：过滤掉 `thickness ≤ 0` 的描边，且仅当 fill 或 stroke 至少一个非空才绘制，目的都是避免 krilla 用默认黑色填充出本不该出现的形状。
- `convert_geometry()` 把 `Line`/`Rect`/`Curve` 三种几何体翻译成 `Path`：`Line` 两段操作、`Rect` 用 `push_rect`、`Curve` 委托 `convert_path`。
- **负尺寸矩形**用 `Transform::from_scale(w.signum(), h.signum())` 做镜像变换来还原，因为 krilla 不允许负尺寸而 Typst 支持。
- `convert_path()` 以「追加式」接收 `&mut PathBuilder`，把 `Move`/`Line`/`Cubic`/`Close` 一一对应到 PDF 的 `m`/`l`/`c`/`h`，`Cubic` 只传「两控制点 + 终点」（起点为隐式当前点）。

## 7. 下一步学习建议

本讲只讲了「形状本身」如何变成路径，但**填充和描边的颜色**是如何翻译的（RGB/CMYK/luma/专色、不透明度分离）还没有展开——那是 `paint.rs` 的职责。建议下一讲学习 **u3-l11 纯色与色彩空间转换**，它会补上 `convert_fill` / `convert_stroke` / `convert_solid` 的完整内部逻辑。

如果你想更早看到「绘制状态如何影响图形」，可以先回头重读 `u2-l8` 的 `LineCapExt` / `LineJoinExt` / `FillRuleExt`（描边的线帽、连接、填充规则），它们在 `paint::convert_stroke` 里被调用，本讲的 `handle_shape` 已经为它们搭好了舞台。

继续阅读建议：`src/paint.rs`（颜色翻译）、`src/convert.rs` 中 `handle_frame` 对 `FrameItem::Shape` 的分派上下文，以及 `typst-library` 的 `visualize/shape.rs`（`Shape`/`Geometry`/`FixedStroke` 的完整定义与 `bbox` 计算）。

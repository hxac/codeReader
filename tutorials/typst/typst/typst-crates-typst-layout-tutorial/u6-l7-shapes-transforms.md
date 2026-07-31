# 图形、变换与修饰符

## 1. 本讲目标

前面的 u6-l1～u6-l6 都在讲「内容如何被装进容器」——表格、公式、栈、列表。本讲换一个视角，讲三类**对已经排好的 Frame 做二次加工**的排版器：

1. **shapes（图形）**：从零画出一条线、一个矩形、一个圆、一段任意贝塞尔曲线，再把它们变成 `FrameItem::Shape`。
2. **transforms（变换）**：对一个已经排好的帧施加移动、旋转、缩放、倾斜——核心难点是「变换会改变内容占地，那给内容排版时该用多大的画布」。
3. **modifiers（修饰符）**：把「隐藏」和「超链接」这两种与几何无关的帧级修饰，统一地、不遗漏地施加到 flow/inline/math 的产物上。

外加三个小而独立的容器型排版器：**image（图片）**、**pad（内边距）**、**repeat（重复填充）**。

学完本讲，你应当能够：

- 说清一个图形（如 `#rect()`、`#curve()`）是如何从用户参数走到 `Geometry` + `Stroke` + `Fill`、最后变成帧里的一个 `Shape` 的；
- 理解变换的通用套路：**先把 region 用 `compute_bounding_box` 放大 → 排版 → 施加 `Transform`**，并解释 `reflow` 开关如何决定最终帧尺寸是「原样」还是「变换后的包围盒」；
- 写出「绕锚点 \((x,y)\) 变换」的复合矩阵 `translate(x,y) ∘ T ∘ translate(-x,-y)`，并解释为何是这个顺序；
- 解释 `FrameModifiers` 为什么**必须**由 flow/inline/math 三个手动管理子样式的排版器各自施加，而 grid/stack/shapes 等不用管；
- 看懂 `image` 的 fit（contain/cover/stretch）、`pad` 的「先缩后排完再涨回来」、`repeat` 的次数公式。

本讲是 u6「网格、数学与容器布局」单元的收尾篇，依赖 u6-l5（栈）与 u2-l3（Frame/Fragment）。

## 2. 前置知识

本讲默认你已掌握前置讲义建立的认知，这里只做最简回顾并补三个本讲要用到的概念。

- **Frame / FrameItem**（见 u2-l3）：排版的最终产物是一棵 Frame 树，叶子是 `FrameItem` 的六种变体之一。本讲反复出现的两种是 `FrameItem::Shape(Shape, Span)`（一个矢量图形）与 `FrameItem::Image(Image, Size, Span)`（一张位图）。一个 `Shape` 由 `geometry`（几何）、`fill`（填充）、`stroke`（描边）、`fill_rule`（填充规则）组成。
- **Region / Regions**（见 u2-l2）：排版器的可用画布。`region.size` 是当前尺寸，`region.expand` 是「是否要填满」的契约。本讲的图形/变换/图片大多只吃单个 `Region`（注定单帧），只有 `pad` 吃可断裂的 `Regions`。
- **Transform（仿射变换）**：本讲新增。Typst 用一个 6 元组表示二维仿射变换，它既能描述旋转/缩放/倾斜，也能复合。这是 transforms 一节的核心数据结构，下面 4.2 会展开。

三个本讲要用的小概念：

1. **`Geometry`（几何）**：一个图形的「形状描述」，有 `Line`（线段）、`Rect`（矩形）、`Curve`（贝塞尔曲线）等变体。`Geometry::Line(delta).stroked(stroke)` 表示「把这条线用给定描边画出来」。
2. **`Stroke` / `FixedStroke`（描边）**：描述「线条本身」——粗细 `thickness`、颜色 `paint`、线帽 `cap`、虚线 `dash` 等。图形的「边框」就是 stroke。
3. **`Abs`（绝对长度）/ `Rel`（相对长度）**：`Abs` 是 pt 这样的绝对值；`Rel<Abs>` 可以带一个相对比例（如 `5%`），需要相对某个基准尺寸解析（`rel.relative_to(base)`）。

## 3. 本讲源码地图

本讲涉及四个主文件与三个辅助文件：

| 文件 | 作用 |
| --- | --- |
| `src/shapes.rs` | 全部图形：`line`/`curve`/`polygon`/`rect`/`square`/`ellipse`/`circle`，以及矩形的描边/圆角/分段系统（`styled_rect`、`ControlPoints`）。 |
| `src/transforms.rs` | 全部变换：`move`/`rotate`/`scale`/`skew`，核心通用函数 `measure_and_layout` 与 `compute_bounding_box`。 |
| `src/modifiers.rs` | 帧级修饰符 `FrameModifiers`（link/hide）、`FrameModify` trait、`layout_and_modify` 与 `modify_text`。 |
| `src/image.rs` | 图片排版 `layout_image`，含 fit（contain/cover/stretch）与 DPI 处理。 |
| `src/pad.rs` | 内边距 `layout_pad`，核心是「先 `shrink` 缩小 region → 排版 → 再 `grow` 涨回帧」。 |
| `src/repeat.rs` | 重复填充 `layout_repeat`，推导「填满给定宽度需要几份」的次数公式。 |

辅助理解的外部类型（定义在 `typst-library`，本 crate 只消费）：

| 文件 | 作用 |
| --- | --- |
| `crates/typst-library/src/layout/transform.rs` | `Transform` 类型与 `pre_concat`/`rotate`/`skew` 等构造。 |
| `crates/typst-library/src/layout/point.rs` | `Point::transform` / `transform_inf`（点被变换）。 |
| `crates/typst-library/src/layout/frame.rs` | `Frame::transform`/`translate_visual`/`resize`/`hide`/`clip` 等方法。 |
| `crates/typst-library/src/visualize/curve.rs` | `Curve`（贝塞尔路径）、`Geometry`、`Shape`。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：图形构建 shapes、变换 transforms、媒体与容器辅助（image/pad/repeat）、帧级修饰符 modifiers。

### 4.1 图形构建 shapes

#### 4.1.1 概念说明

图形排版器（`#line()`、`#rect()`、`#circle()`、`#curve()`、`#polygon()`）的任务很纯粹：**根据用户参数算出一个几何形状，把它包成一个 `Shape`，推进一个尺寸刚好包住它的 `Frame::soft`**。它们大多不需要 `Engine`（不排版子内容）、不需要 `Locator`（无内省），所以你会在源码里看到 `_: &mut Engine, _: Locator` 这样的占位参数。

图形分两类，处理难度天差地别：

- **简单图形**（line/polygon/curve）：几何就是路径本身，描边只是「沿路径画一条线」。代码短小。
- **矩形容器图形**（rect/square/ellipse/circle）：它们既能当纯图形画，也能**包住一段子内容**（如 `#rect[Hello]`）；而且矩形有圆角、四边可不同的描边、描边相交处的连接处理。这一类的描边系统（`styled_rect` → `segmented_rect` → `ControlPoints`）是整个 `shapes.rs` 里最复杂的部分。

#### 4.1.2 核心流程

简单图形（以 `line` 为例）的流程：

1. 解析起点 `start` 与位移 `delta`（用户给 `end` 就算 `end - start`；否则用 `length` + `angle` 算）；
2. 帧尺寸 = `start` 与 `start + delta` 的逐分量最大值；
3. 构造 `Geometry::Line(delta).stroked(stroke)`，推进帧。

矩形容器图形（rect/square/ellipse/circle）统一走 `layout_shape`：

1. 若有子内容 `body`：先把 region 按 `inset` 缩小（圆形还要额外缩 \(0.5 - \sqrt2/4\) 以贴合内切），在缩小后的 pod 里用 `layout_frame` 排子内容，排完再 `grow` 涨回；
2. 若是「正方形/圆」(`is_quadratic`)：先测量确定边长，再用 `Size::splat(length)` + `expand=true` 强制等比例；
3. 若无子内容：用默认尺寸 `45pt × 30pt`（受 region.expand 选择）；
4. 根据 `is_round` 走椭圆曲线或矩形的 `fill_and_stroke`，把描边/填充 `prepend` 到帧最底层。

#### 4.1.3 源码精读

**画一条线**——`layout_line`，最简单的图形排版器，连 `Engine` 都不用：

[start/transforms.rs 之外，shapes.rs 的 layout_line:L21-L53](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/shapes.rs#L21-L53)

关键三行：尺寸取 `start.max(start + delta)`（保证正向），用 `Geometry::Line(delta).stroked(stroke)` 造图形，推进帧。`Frame::soft` 表示「软帧」，尺寸可被父级改写（见 u2-l3 的 FrameKind）。

**任意曲线**——`layout_curve` + `CurveBuilder`。`#curve()` 由若干 `CurveComponent`（Move/Line/Quad/Cubic/Close）组成，`CurveBuilder` 把它们翻译成一条 `Curve`。值得注意的是 Typst 内部统一用**三次贝塞尔**（cubic）：二次（quad）会被 `control_q2c` 转成三次，自动控制点靠 `mirror_c`（关于端点镜像上一个控制点）。

[shapes.rs 的 cubic 段与包围盒:L223-L240](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/shapes.rs#L223-L240)

这段用 `kurbo::CubicBez::bounding_box()` 精确算出三次贝塞尔段的极值，累加进 `self.size`——所以曲线的帧尺寸是精确包围盒，不是端点包围盒。

描边/填充的解析逻辑（curve/polygon 共用）：当 `stroke` 是 `auto` 时，「有 fill 就不描边、无 fill 就给默认描边」：

[shapes.rs 中 stroke 为 auto 的解析:L126-L130](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/shapes.rs#L126-L130)

**矩形容器的统一入口**——`layout_shape`。它先处理「正方形要先测量」的逻辑：

[shapes.rs 的 quadratic 测量:L519-L532](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/shapes.rs#L519-L532)

`quadratic_size` 判断用户是否显式指定了宽或高（`region.expand` 为 true 表示该轴被强制），据此决定边长；都没指定就先排一次子内容、取 `max_by_side` 作为边长。随后用 `Size::splat(length)` + `expand=true` 二次排版强制等比例。

为什么圆/椭圆要额外缩 `0.5 - √2/4`？因为一个内切于正方形的圆，其外接圆角矩形描边会比正方形边长大 \( \sqrt2 \) 倍，这个偏移让带描边的圆刚好贴合内容：

[shapes.rs 圆形的额外 inset:L504-L507](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/shapes.rs#L504-L507)

**矩形描边系统**——`styled_rect` 是分流的入口：若四边描边一致且无圆角，走最便宜的 `simple_rect`（一个 `Geometry::Rect`）；否则走 `segmented_rect`，把矩形拆成多段、分别用「填充带」或「描边线」绘制：

[shapes.rs 的 styled_rect 分流:L686-L697](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/shapes.rs#L686-L697)

`segmented_rect` 的精妙之处：当相邻两边的描边不同（颜色/粗细/线帽不同），它不在角上硬碰硬，而是把每条边画成一个**有宽度的填充区域**（`fill_segment`），让描边在拐角处自然交汇；`ControlPoints` 则负责算出每条边的外侧/中线/内侧三套控制点，以支持任意圆角半径与描边宽度。这部分是 `shapes.rs` 最长也最几何的部分，初学时只需记住「四边一致用矩形原图元，否则分段绘制」即可。

#### 4.1.4 代码实践

**实践目标**：直观感受 `stroke: auto` 的「有填充不描边、无填充给默认描边」规则。

**操作步骤**（源码阅读 + 本地验证）：

1. 在 Typst 文档里分别写 `#rect(width: 60pt, height: 40pt)` 与 `#rect(width: 60pt, height: 40pt, fill: red)`，编译观察：前者有黑色边框、无填充；后者有红色填充、**无**边框。
2. 对照源码 [shapes.rs:L562-L568](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/shapes.rs#L562-L568) 确认：`Smart::Auto if fill.is_none() => Sides::splat(Some(FixedStroke::default()))`（无填充→默认描边），`Smart::Auto => Sides::splat(None)`（有填充→不描边）。

**需要观察的现象**：第二个矩形没有黑色描边，因为它有 fill。

**预期结果**：与源码逻辑一致。若想两者都有，需显式 `stroke: 1pt + black`。

#### 4.1.5 小练习与答案

**练习 1**：`#square()` 在没有指定宽高、也没有子内容时，边长是多少？

**答案**：从 [shapes.rs:L544-L556](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/shapes.rs#L544-L556) 可知，默认尺寸是 `Size::new(45pt, 30pt)`，`is_quadratic` 时取 `default.min_by_side()` = `30pt`，所以边长 30pt（受 region 上限约束）。

**练习 2**：为什么 `#curve()` 的包围盒要用 `kurbo::CubicBez::bounding_box()` 而不是简单取首尾控制点的极值？

**答案**：三次贝塞尔曲线可能「凸出」其控制点四边形之外（曲线的极值点不一定在端点）。直接取端点极值会得到偏小的帧，导致曲线被裁切；`bounding_box()` 精确求导算极值（见 [shapes.rs:L234-L236](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/shapes.rs#L234-L236)）。

### 4.2 变换 transforms

#### 4.2.1 概念说明

变换排版器（`#move()`、`#rotate()`、`#scale()`、`#skew()`）对**一个已经排好的帧**施加二维仿射变换。它们的难点不在「怎么变换」，而在一个先有鸡还是先有蛋的问题：

> 内容的排版尺寸取决于可用 region，而 region 应该多大，又取决于内容变换后会占多大地方。

比如旋转 45°：一个 100×100 的方块旋转后占地约 141×141。如果你给内容排版的 region 还是 100×100，内容会被挤变形；但如果给 141×141，内容又不知道该按多大排。

Typst 的解法是「**先用反向变换把 region 放大，再正向变换内容**」。这个套路被提炼成一个通用函数 `measure_and_layout`，四个变换里除了最简单的 `move` 都复用它。

另一个关键开关是 **`reflow`（回流）**：

- `reflow: false`（多数变换的默认）：只对内容做**视觉**变换，**帧尺寸不变**。内容可能溢出帧或留白——它「飘」在原占地上。
- `reflow: true`：变换后**重新计算帧尺寸**为变换后的包围盒，让周围排版真正为变换后的内容让出空间。

#### 4.2.2 核心流程

`Transform` 是一个 6 元组仿射变换 \((s_x, k_y, k_x, s_y, t_x, t_y)\)，作用在点 \((x,y)\) 上为：

\[
\begin{pmatrix} x' \\ y' \end{pmatrix}
= \begin{pmatrix} s_x & k_x \\ k_y & s_y \end{pmatrix}
\begin{pmatrix} x \\ y \end{pmatrix}
+ \begin{pmatrix} t_x \\ t_y \end{pmatrix}
\]

其中 \(s_x,s_y\) 是缩放、\(k_x,k_y\) 是倾斜、\(t_x,t_y\) 是平移。`pre_concat(prev)` 表示「先做 `prev` 再做 `self`」，即矩阵乘法 \(M_{self} \cdot M_{prev}\)。

变换排版器的通用流程（`measure_and_layout`）：

- **reflow 分支**：
  1. 用预先算好的放大尺寸 `size` 作为 pod（`expand=false`）**测量**内容，得到自然尺寸 `frame.size()`；
  2. 用自然尺寸作为 pod（`expand=true`）**正式排版**；
  3. 计算复合变换 `ts = translate(锚点) ∘ transform ∘ translate(-锚点)`（绕锚点变换）；
  4. `frame.transform(ts)`，再用 `compute_bounding_box` 算变换后的包围盒，`translate_visual` 平移、`set_size` 改尺寸；
  5. 基线单独处理（变换后基线会偏移）。
- **非 reflow 分支**：在原 region 排版 → 施加 `ts` → 直接返回（帧尺寸不变）。

「绕锚点 \((x,y)\) 变换」的复合矩阵：

\[
T_{\text{pivot}} = \mathrm{translate}(x,y)\;\circ\;T\;\circ\;\mathrm{translate}(-x,-y)
\]

先把锚点搬到原点、变换、再搬回去。源码里这正是 `Transform::translate(x,y).pre_concat(transform).pre_concat(Transform::translate(-x,-y))`，因为 `pre_concat` 是右结合（先应用 `prev`）。

#### 4.2.3 源码精读

**最简单的 move——不进 `measure_and_layout`**：

[transforms.rs 的 layout_move:L15-L27](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/transforms.rs#L15-L27)

`move` 只是排版后 `translate_visual(delta)`。「visual」意味着**只移动内容、不移动基线**（对比 `Frame::translate` 会同步移动基线，见 frame.rs L304 vs L316）。`delta` 用 `Rel::relative_to(region.size)` 解析，所以 `#move(dx: 50%)` 是相对区域宽度的 50%。

**rotate 的反向放大**——先看它如何算「放大后的 region」：

[transforms.rs 的 layout_rotate:L38-L48](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/transforms.rs#L38-L48)

`compute_bounding_box(region.size, Transform::rotate(-angle))` 把**原 region** 用**反向**旋转 \(-\theta\) 算包围盒，得到 `size`。直觉是：我们想要一个「旋转 \(\theta\) 后刚好能塞回原 region」的画布；反向旋转原 region 取包围盒就是这个画布的近似（包围盒运算不完美可逆，所以是近似，但够用）。注意这里 `is_finite` 为假（无限大区域）时直接给 `inf`，因为无限大旋转后还是无限大。

随后把 `size` 连同 `Transform::rotate(angle)`、`origin`（锚点对齐）、`reflow` 一起交给 `measure_and_layout`：

[transforms.rs 调用 measure_and_layout:L49-L61](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/transforms.rs#L49-L61)

注意最后一个参数 `keep_baseline` 被硬编码为 `false`——旋转后基线在哪尚无定论（180° 旋转会把基线甩到文字上方），故旋转一律 `clear_baseline`（见源码注释 L227-L230）。

**通用核心 `measure_and_layout`**——reflow 分支：

[transforms.rs 的 measure_and_layout reflow 分支:L201-L247](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/transforms.rs#L201-L247)

逐段解读：
- L203-204：用放大后的 `size` 作 pod 测量，得 `frame`（这一步用了 `locator.relayout()`，复用身份做测量，见 u2-l4）；
- L207-208：用**测量出的自然尺寸** `frame.size()` 作 pod、`expand=true` 重新排版，得到正式帧；
- L209：`align.zip_map(frame.size(), FixedAlignment::position)` 算出锚点 \((x,y)\)（Start→0、Center→中、End→满）；
- L212-214：组装绕锚点的复合变换 `ts`；
- L231-232：基线处理——若 `keep_baseline`，把基线中点经 `ts` 变换后的 y 坐标作新基线；
- L235-238：`compute_bounding_box(frame.size(), ts)` 得到 `(offset, size)`，然后 `frame.transform(ts)`（把变换挂成 group 的 transform，见 frame.rs L345）、`translate_visual(offset)`（把负坐标拉回正象限）、`set_size(size)`（帧尺寸 = 变换后包围盒）。

非 reflow 分支则短得多：

[transforms.rs 的 measure_and_layout 非 reflow 分支:L248-L261](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/transforms.rs#L248-L261)

只在原 `region` 排版、施加 `ts`、返回——**帧尺寸保持排版结果不变**，内容只是视觉上被变换。这就是「reflow=false 时内容会溢出/留白」的来源。

**compute_bounding_box**——把变换后矩形的四角取轴对齐包围盒：

[transforms.rs 的 compute_bounding_box:L265-L282](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/transforms.rs#L265-L282)

它变换四个角点（用 `transform_inf`，因为尺寸可能含无穷大，`transform` 会把 inf 归零而 `transform_inf` 保留原始运算），取 x/y 的 min/max，返回 `offset = (-min_x, -min_y)`（用于把负坐标拉回原点）与 `size = (max-min)`。`width.abs()` 是为了应对负缩放（翻转）导致 max<min 的情况。

**scale 与 skew** 的套路与 rotate 完全一致，只是放大尺寸的算法不同：scale 用 `Ratio::new(1.0 / s).of(r)`（除以缩放比例），skew 用 `compute_bounding_box(region.size, Transform::skew(ax,ay))`。scale 还有个 `resolve_scale` 处理 `auto`（保持长宽比，需测量内容）：

[transforms.rs 的 resolve_scale auto 处理:L142-L150](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/transforms.rs#L142-L150)

#### 4.2.4 代码实践

**实践目标**：搞清「旋转一个有限尺寸块」时 region 如何被放大、施加变换，以及 `reflow` 对最终帧尺寸的影响。这正是本讲指定的实践任务。

**操作步骤**（源码阅读型）：

1. 打开 [transforms.rs 的 layout_rotate:L31-L62](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/transforms.rs#L31-L62)。假设原 region 为 100pt×100pt、`angle = 45°`。
   - `compute_bounding_box((100,100), rotate(-45°))`：把 100×100 的四角旋转 −45°，得到一个约 141×141 的包围盒，故 `size ≈ (141, 141)`。这就是「放大的 region」。
2. 跟进 `measure_and_layout` 的 **reflow 分支**（假设 `reflow=true`）：
   - 测量：用 141×141 的 pod 排内容，得自然尺寸（假设内容自然就是 100×100）；
   - 重排：用 100×100、`expand=true` 排出正式帧；
   - `ts = rotate(45°)`（绕 origin 锚点）；
   - `compute_bounding_box((100,100), rotate(45°))` → 约 141×141；
   - `frame.set_size(141×141)`。
3. 对比 **reflow=false**：内容在原 100×100 region 排版，施加 `rotate(45°)`，但帧尺寸**仍是 100×100**——旋转后的 141×141 内容会「溢出」帧边界。

**需要观察的现象 / 预期结果**：

- `reflow=true`：最终帧尺寸 ≈ 141×141（变换后包围盒），周围文字会为旋转后的方块让出完整空间；
- `reflow=false`：最终帧尺寸仍是 100×100，旋转后的方块会与相邻内容重叠（因为占地没变）。

**本地验证**（可选）：写 `A #rotate(45deg, reflow: true, rect(width: 100pt, height: 100pt, fill: red)) B` 与把 `reflow: true` 去掉的版本各编译一次，对比 `A`、方块、`B` 三者的间距差异。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `move` 不像 rotate/scale/skew 那样调用 `measure_and_layout`？

**答案**：平移不改变内容的占地尺寸（只改变位置），所以不存在「该用多大 region」的先有鸡先有蛋问题，直接排版后 `translate_visual` 即可（见 [transforms.rs:L22-L26](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/transforms.rs#L22-L26)）。

**练习 2**：`scale` 在 `auto` 缩放时为什么要测量内容？

**答案**：`#scale(x: 200%)` 是相对比例，但 `#scale(x: 100pt)` 是「缩放到 100pt 宽」，后者需要先知道内容自然宽度才能算出比例 `100pt / 自然宽`。`resolve_scale` 中的 `LazyCell` 让这个测量只在确有 `Length` 型参数时才发生（见 [transforms.rs:L124-L128](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/transforms.rs#L124-L128)）。

**练习 3**：`compute_bounding_box` 为什么用 `transform_inf` 而不是 `transform`？

**答案**：`Point::transform` 在遇到无穷大坐标时会把结果归零（`.of()` 的语义），而这里我们要对有限尺寸的四角做精确几何运算；`transform_inf` 用原始乘法、不做无穷处理（见 point.rs L81 vs L90）。当 region 无穷大时，外层已提前走 `Size::splat(Abs::inf())` 分支，不会进到 `compute_bounding_box`。

### 4.3 媒体与容器辅助 image / pad / repeat

这三个排版器都很短，但各自代表一种重要的「容器」范式。

#### 4.3.1 image：图片与 fit 策略

`layout_image` 先解码图片得到像素宽高 `pxw × pxh` 与像素长宽比 `px_ratio`，再与 region 长宽比比较，决定「图片比区域更宽还是更高」（`wide`），最后按 `fit` 策略缩放：

[image.rs 的 fit 策略:L56-L66](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/image.rs#L56-L66)

三种 fit：
- **Contain**（默认）：整张图塞进 target，**保持长宽比**，可能留白（图片小于 target）；
- **Cover**：填满 target，保持长宽比，**可能裁切**（图片大于 target，于是 `frame.clip` 裁掉超出部分，见 L76-78）；
- **Stretch**：拉伸到 target，**不保持长宽比**。

当 region 两个轴都没 `expand`（即用户没指定尺寸）时，走「自然尺寸」分支：用图片 DPI 把像素换算成物理尺寸（默认 72 DPI），再被 region 上限约束（见 [image.rs:L43-L53](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/image.rs#L43-L53)）。最终图片被先放进一个「恰好其尺寸」的帧，再 `resize` 到 target、居中对齐。

#### 4.3.2 pad：先缩后排完再涨回

`layout_pad` 是少有的吃**多区域 `Regions`**（可断裂）的容器。套路是「缩 region → 排子内容 → 涨回帧」：

[pad.rs 的 layout_pad:L18-L35](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pad.rs#L18-L35)

`shrink` 把每个 region 尺寸减去 padding（padding 可带相对比例，相对 region 自身解析），子内容在缩小后的 region 排版，每个产出帧再 `grow` 涨回。`grow` 不是 `shrink` 的简单逆运算——因为 padding 的相对部分要相对**涨回后**的尺寸解析，源码用代数推导解出涨回尺寸 \( w = (s + p_{abs}) / (1 - p_{rel}) \)（见 [pad.rs:L64-L80](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pad.rs#L64-L80) 的注释）。这个 `grow` 也被 `shapes.rs` 的圆/椭圆内容复用（4.1.3 提到）。

#### 4.3.3 repeat：填满宽度需要几份

`layout_repeat` 把一段内容在给定宽度内重复 N 次。核心是一个次数公式（源码注释写得很清楚）：

[repeat.rs 的次数推导:L37-L48](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/repeat.rs#L37-L48)

设 \(N\) 为份数、\(w\) 为单份宽、\(g\) 为间隙、\(F\) 为待填宽度，则 \(N\cdot w + (N-1)\cdot g \le F\)，解得：

\[
N = \left\lfloor \frac{F + g}{w + g} \right\rfloor
\]

注意 `repeat` 强制 region 宽度有限（否则 `bail!` 报错），且 `piece` 用 `expand=(false,false)` 排版（不让单份被撑满）。若开启 `justify`，则把剩余空间 \( (F+g) \bmod (w+g) \) 均摊到 \(N-1\) 个间隙里，让首尾严格贴边。最多重复 1000 份（`min(1000)`）作防护。

#### 4.3.4 代码实践

**实践目标**：验证 repeat 的次数公式与 justify 行为。

**操作步骤**：

1. 在 Typst 中写 `#box(width: 200pt)[#repeat(". .", gap: 10pt)]`，先估算单份 `". ."` 的宽度 \(w\)（取决于字体，约 15pt）。
2. 用公式 \(N = \lfloor(200 + 10)/(w + 10)\rfloor\) 算出预期份数。
3. 加上 `justify: true`，观察首尾是否严格贴着 200pt 的两端（间隙被撑大）。

**预期结果**：未 justify 时末尾可能有留白；justify 后留白被均摊到间隙，首尾贴边。若不确定字体下 `". ."` 的确切宽度，标注「待本地验证确切份数」。

#### 4.3.5 小练习与答案

**练习 1**：`#image("a.png")` 不指定宽高时，最终显示尺寸由什么决定？

**答案**：由图片的像素尺寸与 DPI 决定。`layout_image` 把像素除以 DPI（默认 72）换算成物理英寸，再被 region 上限约束（见 [image.rs:L47-L52](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/image.rs#L47-L52)）。

**练习 2**：为什么 `pad` 的 `grow` 不能直接用 `frame.size() + padding.sum()`？

**答案**：因为 padding 可含相对比例（如 `5%`），而相对比例要相对**涨回后**的总尺寸解析，不是相对原帧尺寸。直接相加会让相对 padding 比预期小。源码用 \( w = (s + p_{abs})/(1 - p_{rel}) \) 反解（见 [pad.rs:L73-L85](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pad.rs#L73-L85)）。

### 4.4 帧级修饰符 modifiers

#### 4.4.1 概念说明

有些样式**不改变任何几何**，只对最终帧做「附加处理」：`hide`（隐藏内容但保留结构，用于 query）和 `link`（给一块内容加超链接）。它们叫**帧级修饰符（FrameModifiers）**。

这些修饰符有一个微妙的归属问题：**它们应该在哪一层被施加？**

答案是源码注释里的这句话——「**always applied at the highest level of style uniformity（总是在样式最一致的最高层施加）**」。理解它需要先看一个事实：**flow、inline、math 三个排版器会「手动管理子内容的样式」**——它们能产出样式各异的多个子帧（比如一个段落里每段文字样式不同）。而 grid、stack、shapes、pad 这些不会，它们的子内容样式由外层 realize 统一决定。

于是产生分工：

- **flow/inline/math**：因为自己手动切分样式，必须由**自己**在每个子帧边界施加 `FrameModifiers`，否则一个含不同样式的块会被漏掉或错位施加；
- **其他排版器**：不用管，它们的父级（来自 realize）会把整块内容当一个整体施加修饰符。

这就是本讲第三个学习目标——「为何 `FrameModifiers` 必须在手动管理样式的 layouter 中统一应用」——的答案。

#### 4.4.2 核心流程

`FrameModifiers` 只有 `get_in(styles)` 一个构造方法，从样式链读出 `LinkElem::current`（当前链接目标）与 `HideElem::hidden`（是否隐藏）。施加通过 `FrameModify` trait 的 `modify`/`modified`：

[modifiers.rs 的 modify_frame:L92-L111](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/modifiers.rs#L92-L111)

施加顺序：先推 `FrameItem::Link`（一个覆盖整个帧的不可见链接矩形），再（若 hidden）`frame.hide()`。`hide` 只保留 `Tag` 与非空 `Group`（递归隐藏），所以隐藏的内容**仍保留内省结构**（query 仍能查到），只是去掉可视项。

为方便「排版 + 施加」一步完成，提供了两个辅助：

- `layout_and_modify(styles, layout_fn)`：先取出修饰符，**临时关掉 link**（避免生成冗余的嵌套链接），用清洗过的样式排版，再施加修饰符。flow/inline 用它。
- `modify_text(styles)`：文本帧专用，给链接矩形加一个纵向 outset（`0.5 * leading`），让可点击区域比字形的紧凑包围盒略大，更好点。

#### 4.4.3 源码精读

**FrameModifiers 与 trait 定义**：

[modifiers.rs 的 FrameModifiers 与 FrameModify trait:L21-L51](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/modifiers.rs#L21-L51)

`FrameModify` 不仅实现了 `Frame`，还实现了 `Fragment`（逐帧施加）和 `Result<T,E>`（对 `layout_frame(...)?` 的结果直接 `.modify()`，很顺手）。

**layout_and_modify 的去重链接**：

[modifiers.rs 的 layout_and_modify:L119-L138](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/modifiers.rs#L119-L138)

注释解释了为何要 `LinkElem::current.set(None)`：如果不在内部关掉 link，外层施加的链接会与每个子帧自己施加的链接叠加，生成大量冗余嵌套链接，使输出膨胀。所以这里「这一层已经施加了链接，子层就别再施加了」。

**谁在调用？**——grep 结果印证了「flow/inline/math 三家」的结论：

| 调用点 | 用法 |
| --- | --- |
| `src/flow/collect.rs` L438/L538/L644 | `layout_and_modify(styles, \|styles\| {...})`——块/单帧排版后施加 |
| `src/inline/collect.rs` L215 | `frame.modify(&FrameModifiers::get_in(styles))`——行内盒（box） |
| `src/inline/collect.rs` L228 / `src/inline/line.rs` L579 | `layout_and_modify(...)`——行内内容 |
| `src/inline/shaping.rs` L462 | `frame.modify_text(self.styles)`——文本帧（带链接 outset） |
| `src/math/fragment/mod.rs` L246 | `frame: frame.modified(&modifiers)`——公式片段 |

可见 grid/stack/shapes/transforms/pad/image/repeat **都不**直接调用——它们依赖父级（realize 产出的块）经 flow/inline 路径代为施加。例如 `#link("https://x")[#rect[Hi]]`，`rect` 自身不管 link，是外层把 `[Hi]` 当块交给 flow 时由 flow 的 `layout_and_modify` 施加的。

#### 4.4.4 代码实践

**实践目标**：验证「隐藏的内容仍可被 query 查到」与「修饰符由 flow 施加而非 shapes」。

**操作步骤**：

1. 在 Typst 中写：
   ```typst
   #let h = heading[隐藏的标题]
   #metadata(h)
   #hide(h)
   #context query(heading).len()
   ```
2. 编译，观察：标题不可见，但 `query(heading)` 的结果长度包含它。
3. 对照源码 [frame.rs 的 hide:L325-L334](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/frame.rs#L325-L334)：`hide` 用 `retain` 保留 `Tag`、丢弃可视项——Tag 正是内省的载体（见 u2-l4）。

**需要观察的现象**：页面上看不到「隐藏的标题」，但 query 计数把它算进去了。

**预期结果**：与源码一致。这印证了「hide 是帧级修饰、保留内省结构」的设计。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `FrameModifiers` 不在 `layout_frame`/`layout_fragment` 这类公共入口里统一施加，而要由 flow/inline/math 各自施加？

**答案**：因为修饰符要在「样式最一致的最高层」施加。flow/inline/math 手动管理子样式、会产出样式不同的多个子帧，公共入口无法知道每个子帧的正确样式层；而其他排版器的子内容样式由 realize 统一决定，父级块（经 flow）会代为施加。若强行在公共入口施加，要么漏掉（子帧样式不同），要么重复（见 [modifiers.rs:L8-L16](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/modifiers.rs#L8-L16) 注释）。

**练习 2**：`layout_and_modify` 为什么要临时把 `LinkElem::current` 设为 `None`？

**答案**：这一层已经为整块内容施加了一个覆盖性链接，若不关掉，子层排版时还会再为每个子帧施加嵌套链接，导致输出里出现大量冗余的嵌套链接矩形，严重膨胀（见 [modifiers.rs:L126-L135](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/modifiers.rs#L126-L135)）。

## 5. 综合实践

把本讲四条主线串起来：**画一个带圆角的矩形卡片，里面放一段旋转的文字，整张卡片加超链接，并验证隐藏后仍可查询**。

**任务**：用 Typst 写如下文档，并跟踪它在本讲四个模块里的处理路径：

```typst
#link("https://typst.app")[
  #rect(
    radius: 8pt, inset: 12pt, fill: rgb("eee"),
  )[
    #rotate(90deg, reflow: true)[
      #text("竖排文字")
    ]
  ]
]
```

**请按本讲源码解释以下四点**：

1. **shapes**：`rect` 如何走 `layout_shape`？它有子内容，所以先按 `inset: 12pt` `shrink` region（复用 `pad::shrink`）排子内容，再 `grow` 涨回；描边/填充经 `fill_and_stroke` → 因有 `radius: 8pt`（非零圆角）走 `segmented_rect` 分段绘制圆角矩形（见 [shapes.rs:L571-L592](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/shapes.rs#L571-L592)）。
2. **transforms**：`rotate(90deg, reflow: true)` 走 `measure_and_layout` 的 reflow 分支——先用 `compute_bounding_box(region, rotate(-90°))` 放大 region 测量，重排，施加 `rotate(90°)`，最终帧尺寸 = 旋转后包围盒（宽高互换）（见 [transforms.rs:L201-L238](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/transforms.rs#L201-L238)）。
3. **modifiers**：`#link(...)` 不会由 `rect` 自身处理，而是当整个块交给 flow 时，由 flow 的 `layout_and_modify` 在帧上推一个 `FrameItem::Link`（见 [flow/collect.rs 的 layout_and_modify 调用](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs#L644)）。
4. **验证 hide**：把 `#link(...)` 包一层 `#hide(...)`，卡片消失但 `query` 仍能查到其中的内容——因为 `frame.hide()` 保留 Tag（见 [frame.rs:L325-L334](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/frame.rs#L325-L334)）。

**预期结果**：编译出一张带圆角灰底的卡片，内含竖排文字，整卡可点跳转；包 `hide` 后不可见但可查询。若某些尺寸（如旋转后确切占地）无法手算确定，标注「待本地验证」。

## 6. 本讲小结

- **图形（shapes）**：line/curve/polygon 是「路径 + 描边」的简单图形；rect/square/ellipse/circle 统一走 `layout_shape`，能包子内容（先 `shrink` 后 `grow`）、支持圆角与四边不同描边（四边一致用矩形原图元，否则 `segmented_rect` 分段绘制）。
- **变换（transforms）**：核心套路是「反向变换放大 region（`compute_bounding_box`）→ 排版 → 绕锚点施加复合变换」；`reflow` 决定最终帧尺寸——`true` 取变换后包围盒、`false` 保持原尺寸（内容视觉变换、可能溢出）。`move` 是例外，只 `translate_visual`、不进 `measure_and_layout`。
- **复合变换**：绕锚点 \((x,y)\) 变换的矩阵是 `translate(x,y) ∘ T ∘ translate(-x,-y)`，对应源码 `Transform::translate(x,y).pre_concat(T).pre_concat(translate(-x,-y))`，因 `pre_concat` 右结合。
- **image/pad/repeat**：image 三种 fit（contain 留白 / cover 裁切 / stretch 拉伸）、无尺寸时按 DPI 换算；pad「先缩 region 排版再涨回帧」，`grow` 用代数式反解相对 padding；repeat 用 \(N=\lfloor(F+g)/(w+g)\rfloor\) 算份数，`justify` 把余量均摊到间隙。
- **修饰符（modifiers）**：`FrameModifiers`（link/hide）是与几何无关的帧级修饰，**必须**由 flow/inline/math 三个手动管理子样式的排版器各自施加（用 `layout_and_modify` / `modify` / `modify_text`），其他排版器依赖父级代为施加；`hide` 保留 Tag 故隐藏内容仍可被 query。
- **贯穿主线**：shapes 产出 `FrameItem::Shape`、image 产出 `FrameItem::Image`、transforms 对帧挂 `group.transform`、modifiers 追加 `FrameItem::Link` 或 `hide`——四者都是对「Frame 这棵树」的不同加工，是 u2-l3「排版结果就是一棵 Frame 树」的延展。

## 7. 下一步学习建议

本讲是 u6「网格、数学与容器布局」单元的最后一篇，也是整个 typst-layout crate 七个单元里最后一片「子系统精读」。接下来建议：

1. **进入 u7（规则注册与集成）**：本讲反复提到「图形/变换的 `layout_*` 函数如何被挂成元素排版器」——答案在 `src/rules.rs` 的 `register`。u7-l1 会讲 `Target::Paged` 与 `*_RULE` 的注册机制，u7-l2 会讲 `LAYOUT_RULE` 等复杂规则。学完 u7，你就能把 u1～u6 所有散落的 `layout_*` 函数在脑中挂回它们的元素。
2. **横向打通 Frame 加工链**：回头对比 u2-l3（Frame/Fragment）、u3-l4（页面 finalize 的 `push_frame` 拼装顺序）、本讲（transform/hide/link/clip），你会发现它们都在操作同一棵 Frame 树。建议重读 u3-l4 的 `background→header→inner→footer→foreground` 拼装顺序，体会 FrameItem 顺序对内省与计数器的影响。
3. **源码延伸阅读**：若对图形的描边系统（`ControlPoints` 的外/中/内三套控制点）感兴趣，可深读 [shapes.rs:L1069-L1338](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/shapes.rs#L1069-L1338) 配合其 ASCII 示意图（L1052-L1068）；若对变换后基线处理感兴趣，可追踪 `keep_baseline` 在 scale（竖直翻转时关闭）与 rotate（一律关闭）中的差异。

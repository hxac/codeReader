# 圆锥渐变 Conic

## 1. 本讲目标

本讲是「绘制系统」单元里最难的一篇，专门拆解 typst-svg 如何把 Typst 的**圆锥渐变（conic gradient）**翻译成 SVG。

读完本讲，你应当能够：

- 说清**为什么圆锥渐变在 typst-svg 里要特殊处理**——SVG 标准根本没有原生的 conic 渐变元素。
- 看懂 `write_gradients` 的 `Conic` 分支如何用一个 `<pattern>` + **360 段扇形**把一个圆锥渐变「拼」出来。
- 推导每一段扇形的 `move_to / line_to / arc / close` 路径，以及 `correct_tiling_pos` 如何把单位正方形里的坐标映射到 pattern 内容坐标系。
- 解释 `SVGSubGradient` 与 `write_subgradients` 为什么为每一段扇形单独生成一个**双停靠点 linearGradient**，而不是给扇形填一个单一颜色。
- 理解 `inverse_ratio = aspect_ratio.recip()` 在角度修正中起到的「反向补偿」作用。

本讲承接 u5-l3（线性/径向渐变的源定义与「源 + 引用」两层去重模型），如果你对 `write_gradients` / `write_gradient_refs` / `correct_aspect_ratio` 还不熟，建议先读那一篇。

## 2. 前置知识

### 2.1 什么是圆锥渐变

线性渐变沿一条直线变化颜色，径向渐变沿半径变化颜色，而**圆锥渐变**是绕一个**中心点**沿**角度**变化颜色——想象一个色轮（color wheel）：从 12 点钟方向开始，顺时针扫一圈，颜色随角度连续变化。Typst 里用 `gradient.conic(..)` 创建，典型用法见测试用例：

```typst
// 来自 tests/suite/visualize/gradient.typ:314-318
#square(
  size: 50pt,
  fill: gradient.conic(..color.map.rainbow, space: color.hsv),
)
```

圆锥渐变的关键参数（见 `ConicGradient` 结构体 [gradient.rs:1165-1178](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/gradient.rs#L1165-L1178)）：颜色停靠点 `stops`、起始角度 `angle`、圆心 `center`（用 `Ratio` 表示，即相对形状包围盒的 0~1 比例）、色彩空间 `space`。

### 2.2 SVG 为什么没有圆锥渐变

SVG 1.1 / SVG 2 里只有 `linearGradient` 和 `radialGradient` 两种渐变元素，**没有 conic**。CSS里有 `conic-gradient()`，但那是 CSS 背景属性，不能用作 SVG 的 `fill`。因此任何要把圆锥渐变写进 SVG 的工具，都得自己**模拟**。typst-svg 的策略是：把整圈颜色离散成 360 个角度切片（扇形），每个切片用一个近似的小线性渐变填充。

### 2.3 你需要记住的几个上游概念

- **`<pattern>`**：SVG 的图案平铺元素，本来用于重复填充（壁纸式），这里被「借用」来装载 360 个扇形 path。本讲的 conic 源定义就是一个 pattern，而不是 gradient 元素。
- **「源 + 引用」两层去重**（u5-l2/u5-l3 已讲）：源不带变换、只存一份；引用只记「源 ID + 变换矩阵」，是近乎空壳的 `<pattern>`。圆锥渐变在这套模型里额外多出**第三层**——子渐变 `conic_subgradients`。
- **`correct_aspect_ratio(angle, ratio)`**：把一个角度按宽高比预修正，定义在 typst-library [gradient.rs:994-996](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/gradient.rs#L994-L996)：
  ```rust
  pub fn correct_aspect_ratio(angle: Angle, aspect_ratio: Ratio) -> Angle {
      Angle::atan2(angle.sin() / aspect_ratio.get().abs(), angle.cos()).normalized()
  }
  ```
  直觉：非等比缩放会把角度「拽歪」，这个函数在缩放发生**之前**先把角度反向掰一下，让缩放之后的视觉角度回到目标值。线性渐变里用的是 `aspect_ratio`，圆锥里用的是它的倒数 `inverse_ratio`——这是本讲的一个重点。

## 3. 本讲源码地图

本讲几乎全部内容集中在 `src/paint.rs`，只引用少量上游与同 crate 文件：

| 文件 | 作用 |
| --- | --- |
| [src/paint.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs) | 圆锥渐变的全部实现：`write_gradients` 的 Conic 分支、`write_subgradients`、`SVGSubGradient`、`correct_tiling_pos`、`NUM_CONIC_SEGMENTS` |
| [src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs) | `SVGRenderer` 的 `conic_subgradients` 字段、`finalize` 的写出顺序 |
| [src/path.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/path.rs) | `SvgPathBuilder` 的 `move_to / line_to / arc / close`，扇形路径就是用它拼的 |
| typst-library `gradient.rs` | `ConicGradient` 结构、`correct_aspect_ratio`、`sample` |

## 4. 核心概念与源码讲解

### 4.1 SVG 无原生圆锥渐变：为何用 pattern + 360 段扇形

#### 4.1.1 概念说明

SVG 不能直接表达「绕中心按角度变色」，所以 typst-svg 采取**离散化近似**：把整圈 360° 切成 `NUM_CONIC_SEGMENTS` 段（每段 1°），每一段是一个从圆心射出的**扇形（wedge / pie slice）**。把 360 个扇形拼在一起，就重新覆盖了整个色轮。

每个扇形本身仍需要「从一种颜色过渡到下一种颜色」——因为即使在一个 1° 的切片内，圆锥渐变的颜色也在变化。SVG 能表达「一段直线上的颜色过渡」的就是 `linearGradient`，于是 typst-svg 给**每个扇形配一个独立的双停靠点 linearGradient**（子渐变）。

为什么不直接用一个大 `linearGradient`？因为圆锥渐变的颜色变化方向是**沿圆周（切向）**的，每一段切向方向都不同，一个线性渐变无法描述。切成 360 段、每段一个方向近似，是最朴素的可行解。

> 为什么装进 `<pattern>` 而不是 `<g>`？因为 pattern 自带「平铺 + 自带坐标系」的语义，方便后续用 `patternTransform`（见 4.4）把这个色轮整体搬到形状的包围盒里；同时 pattern 的 `viewBox` 给 360 个扇形提供了一个干净的 [0,1] 绘图坐标系。

#### 4.1.2 核心流程

离散数量由一个常量决定：

```rust
// src/paint.rs:16-19
/// The number of segments in a conic gradient.
/// This is a heuristic value that seems to work well.
/// Smaller values could be interesting for optimization.
const NUM_CONIC_SEGMENTS: usize = 360;
```

注释说得很直白：**360 是个「经验上效果好」的启发式值**，并非从数学推导来；更小的值可以做体积优化（代价是色轮出现可见的「辐条」锯齿）。

整体流程：

```text
对一个 Conic 渐变 G（带圆心 center、起始角 angle、宽高比 aspect_ratio）：
  1. 创建一个 <pattern>（不是 gradient 元素！）
  2. dtheta = 2π / 360            // 每段 1°
  3. for i in 0..360:
       a. theta1 = 起始角 + dtheta * i      （经 inverse_ratio 修正 + 取负）
       b. theta2 = 起始角 + dtheta * (i+1)
       c. 用 move_to(center) → line_to(弧端@theta1) → arc(弧端@theta2) → close 拼出一个扇形 path
       d. t1 = i/360, t2 = (i+1)/360
       e. c0 = G.sample(t1), c1 = G.sample(t2)   // 取该段两端的颜色
       f. 组装一个 SVGSubGradient { center, t0: theta1, t1: theta2, c0, c1 }，去重后得到 id
       g. 输出 <path d=... fill="url(#id)" stroke="none" .../>
  4. continue —— 跳过函数末尾给 linear/radial 生成 <stop> 的通用代码
```

#### 4.1.3 源码精读

Conic 分支的整体入口在 `write_gradients` 里，与 Linear / Radial 并列（[src/paint.rs:167-243](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L167-L243)）。注意它创建的是 `pattern` 元素：

```rust
// src/paint.rs:167-177
Gradient::Conic(conic) => {
    let mut pattern = defs.elem("pattern");
    pattern.attr("id", id);
    pattern.attr("viewBox", "0 0 1 1");
    pattern.attr("preserveAspectRatio", "none");
    pattern.attr("patternUnits", "userSpaceOnUse");
    pattern.attr("width", "2");
    pattern.attr("height", "2");
    // TODO: Refactor this.
    pattern.attr("x", "-0.5");
    pattern.attr("y", "-0.5");
```

这段属性串的含义：

- `patternUnits="userSpaceOnUse"` + `width="2" height="2"`：图案瓦片是 2×2 用户单位。
- `viewBox="0 0 1 1"` + `preserveAspectRatio="none"`：瓦片**内部**用 [0,1]×[0,1] 的坐标系绘图（扇形坐标都落在这个坐标系里），再拉伸到 2×2 的瓦片。
- `x="-0.5" y="-0.5"`：把瓦片整体平移，使圆心（在 [0,1] 坐标系的中心附近）对齐到形状的几何中心。注释 `// TODO: Refactor this.` 表明作者也认为这块定位逻辑可以再清理。

#### 4.1.4 代码实践

**实践目标**：直观感受「段数 = 360」对体积的影响。

**操作步骤**：

1. 读常量定义 [src/paint.rs:19](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L19)。
2. 心算：一个圆锥渐变会产生 360 个 `<path>` + 最多 360 个 `<linearGradient>` 子渐变定义。如果一份文档里有 N 个不同形状的圆锥渐变，理论上会产生 **360·N** 个 path。
3. （可选，待本地验证）用 typst-cli 编译一个含 `gradient.conic(..)` 的 `.typ` 文件输出 SVG：
   ```typst
   #square(size: 100pt, fill: gradient.conic(..color.map.rainbow))
   ```
   用 `typst compile --format svg` 生成 `.svg`，再 `grep -c '<path' 文件.svg` 数一数 path 数量，验证是否接近 360。

**需要观察的现象**：单个圆锥渐变会让 SVG 文件显著变大（多出几百行 path/linearGradient）。

**预期结果**：path 数量约为 360（若多个圆锥渐变颜色完全相同，因去重，子渐变会被复用，path 数仍由「源 pattern」数量决定，见 4.4）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 typst-svg 不直接用 CSS 的 `conic-gradient()` 来实现？

**参考答案**：因为输出目标是独立的 `.svg` 文件（或内嵌 SVG 片段），`fill` 属性只能引用 SVG 元素（如 `url(#id)` 指向 `linearGradient`/`radialGradient`/`pattern`）或纯色，不能填一个 CSS 背景函数。CSS `conic-gradient()` 只能用在 CSS 的 `background` 等属性上，依赖宿主渲染器，不符合 typst-svg「产出可移植 SVG」的目标。

**练习 2**：如果把 `NUM_CONIC_SEGMENTS` 改成 `36`，视觉和体积分别会怎样变化？

**参考答案**：体积大约降到 1/10（36 个 path + 子渐变），但每段跨度变成 10°，相邻扇形的颜色跳变更明显，色轮上会出现可见的「辐条」状色阶。这正是注释所说「smaller values could be interesting for optimization」的取舍。

---

### 4.2 Conic 分支源码精读：扇形 pattern 的生成

#### 4.2.1 概念说明

上节讲了「为什么切 360 段」，本节讲「每一段具体怎么画」。一段扇形（pie slice）的几何由四个动作拼成：

```text
move_to(圆心)
line_to(圆周上的起点 P1，对应角度 theta1)
arc(  从 P1 沿单位圆弧画到 P2，对应角度 theta2 )
close( 回到圆心 )
```

这正好对应 `SvgPathBuilder` 的四个命令（u3-l1 已详细介绍过相对坐标压缩机制）。两个关键问题：

1. **「圆周上的点」怎么算**？用三角函数把角度转成坐标。
2. **角度要不要修正**？要，而且圆锥用的是宽高比的**倒数** `inverse_ratio`，与线性渐变相反。

#### 4.2.2 核心流程

角度与坐标的采样循环（[src/paint.rs:187-238](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L187-L238)），伪代码：

```text
dtheta = TAU / 360                      // TAU = 2π
inverse_ratio = aspect_ratio.recip()    // 关键：用倒数

for i in 0..360:
    // 1) 计算两端角度（取负是为了顺时针旋转）
    theta1 = -correct_aspect_ratio(conic.angle + dtheta*i,     inverse_ratio)
    theta2 = -correct_aspect_ratio(conic.angle + dtheta*(i+1), inverse_ratio)

    // 2) 用三角函数把角度映射成单位圆上的坐标（相对圆心的偏移）
    //    x = -2·cos(θ), y = +2·sin(θ)
    P1 = center + (-2·cos(theta1), +2·sin(theta1))
    P2 = center + (-2·cos(theta2), +2·sin(theta2))

    // 3) 拼扇形路径（坐标都先经 correct_tiling_pos 换算）
    path = move_to(center) → line_to(P1) → arc(radius=1, P2) → close

    // 4) 取该段两端颜色，造子渐变（见 4.3）
    ...
```

#### 4.2.3 源码精读

**角度计算与取负**（[src/paint.rs:187-197](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L187-L197)）：

```rust
// We build an arg segment for each segment of a circle.
let dtheta = Angle::rad(TAU / NUM_CONIC_SEGMENTS as f64);
for i in 0..NUM_CONIC_SEGMENTS {
    // Negate the angle for clockwise gradient rotation.
    let theta1 = -Gradient::correct_aspect_ratio(
        conic.angle + (dtheta * i as f64),
        inverse_ratio,
    );
    let theta2 = -Gradient::correct_aspect_ratio(
        conic.angle + (dtheta * (i + 1) as f64),
        inverse_ratio,
    );
```

注意三层叠加：`conic.angle`（用户给定的起始角）`+ dtheta*i`（扫到第 i 段）→ `correct_aspect_ratio(..., inverse_ratio)`（按倒数做宽高比修正）→ 最外层取负（`-`，转成顺时针，因为 SVG 的 y 轴朝下，角度方向与数学惯例相反）。

**扇形路径构建**（[src/paint.rs:200-217](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L200-L217)）：

```rust
// Create the path for the segment.
let mut builder = SvgPathBuilder::empty();
builder.move_to(correct_tiling_pos(center.x, center.y));

builder.line_to(correct_tiling_pos(
    Ratio::new(-2.0 * theta1.cos()) + center.x,
    Ratio::new(2.0 * theta1.sin()) + center.y,
));
builder.arc(
    Size::splat(Abs::pt(1.0)),
    Angle::zero(),
    0,
    1,
    correct_tiling_pos(
        Ratio::new(-2.0 * theta2.cos()) + center.x,
        Ratio::new(2.0 * theta2.sin()) + center.y,
    ),
);
builder.close();
```

要点逐条对应：

- `move_to(center)`：笔尖抬到圆心。
- `line_to(...)`：画一条到「角度 theta1 对应圆周点」的直线（扇形的一条直边）。点的 x 偏移是 `-2·cos(theta1)`、y 偏移是 `+2·sin(theta1)`——这是把角度参数化成单位圆坐标的特定写法（系数 2 与后续 `correct_tiling_pos` 的 `×0.5` 配合，最终画出一个半径为 1 的单位圆）。
- `arc(Size::splat(Abs::pt(1.0)), Angle::zero(), 0, 1, ...)`：沿单位圆从 theta1 点画弧到 theta2 点。参数依次是 `rx=ry=1pt`（圆形半径）、`x_axis_rot=0`、`large_arc_flag=0`（每段只有 1°，永远走小弧）、`sweep_flag=1`（正向）。SVG 的 `a` 命令语义详见 [src/path.rs:76-100](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/path.rs#L76-L100)。
- `close()`：闭合回到圆心，完成一个封闭扇形。

> 顺带一提：`SvgPathBuilder` 的方法名是 [`finsish`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/path.rs#L53-L55)（`finish` 的拼写错误），下文 4.3 会看到调用处也用的这个名字——这是源码里的真实拼写，不是本讲义打错。

**`correct_tiling_pos` 把 Ratio 坐标换算到 pattern 绘图坐标系**（[src/paint.rs:519-522](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L519-L522)）：

```rust
/// Maps a coordinate in a unit size square to a coordinate in the tiling.
pub fn correct_tiling_pos(x: Ratio, y: Ratio) -> Point {
    0.5 * Point::new(Abs::pt(x.get() + 0.5), Abs::pt(y.get() + 0.5))
}
```

它的作用是把一个 [0,1] 区间的 `Ratio` 坐标变成 pattern 内容里的一个 `Point`：先把 `r ∈ [0,1]` 平移成 `r+0.5 ∈ [0.5,1.5]`，再整体乘 `0.5`，得到 `[0.25, 0.75]` 范围的坐标。以圆心 `(0.5, 0.5)` 为例：

\[
\text{correct\_tiling\_pos}(0.5, 0.5) = 0.5 \times (0.5+0.5,\; 0.5+0.5) = (0.5,\; 0.5)
\]

即圆心正好落在 [0,1] 坐标系的正中央。而扇形端点的 `-2·cos / +2·sin`（幅度为 2）经 `×0.5` 后幅度变为 1，于是画出的圆半径恰为 1，刚好充满 pattern 的单位绘图区。

#### 4.2.4 代码实践

**实践目标**：手工推演一个具体角度的扇形端点坐标，确认你理解了 `correct_tiling_pos` 与三角参数化的配合。

**操作步骤**：

1. 设圆心 `center = (0.5, 0.5)`，取某段角度 `theta1 = 0`（弧度）。
2. 代入端点公式：x_ratio = `-2·cos(0) + 0.5 = -2 + 0.5 = -1.5`，y_ratio = `+2·sin(0) + 0.5 = 0.5`。
3. 代入 `correct_tiling_pos(-1.5, 0.5)`：`0.5 × ((-1.5+0.5), (0.5+0.5)) = 0.5 × (-1, 1) = (-0.5, 1.0)`。
4. 同样算圆心端 `correct_tiling_pos(0.5,0.5) = (0.5, 0.5)`。

**需要观察的现象**：圆心 (0.5,0.5) 到该端点 (-0.5, 1.0) 的欧氏距离：

\[
\sqrt{(0.5-(-0.5))^2 + (0.5-1.0)^2} = \sqrt{1+0.25} = \sqrt{1.25} \approx 1.118
\]

咦，不是恰好 1？这是因为该角度下端点并不在「水平/正交」方向上，y 分量也非零。**待本地验证**：取一个使 `sin(theta)=0` 的角度（例如真正落在坐标轴上的段），端点距离才会恰好为 1。这个练习的关键不是算出漂亮数字，而是确认你掌握了「Ratio → +0.5 → ×0.5」的三步换算。

**预期结果**：能复述 `correct_tiling_pos` 把单位正方形坐标映射到 pattern 内容坐标的完整步骤，并解释系数 2 与 `×0.5` 抵消后得到单位圆半径的过程。

#### 4.2.5 小练习与答案

**练习 1**：`arc` 命令里为什么 `large_arc_flag` 恒为 `0`？

**参考答案**：因为每段只跨 `dtheta = 2π/360 = 1°`，永远小于 180°，两点之间总是走「小弧」即可，不会出现需要走大弧（>180°）的情况，所以固定写 `0`。

**练习 2**：路径用 `SvgPathBuilder::empty()` 构造（而非 `with_translate`/`with_scale`），意味着什么？

**参考答案**：`empty()` 表示 scale 为 1、无预偏移（见 [src/path.rs:43-50](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/path.rs#L43-L50)），即扇形坐标直接按传入值写、不做额外缩放或平移。所有几何变换（缩放、平移、宽高比修正）都已在角度与 `correct_tiling_pos` 里算好，builder 只负责把绝对坐标转成相对坐标压缩输出。

---

### 4.3 子渐变 SVGSubGradient 与 write_subgradients

#### 4.3.1 概念说明

每个扇形需要一个 `fill`。最朴素的想法是：给扇形填**单一颜色**（取该段中间角度的颜色）。但这样相邻扇形之间会出现明显的颜色跳变——360 个色块拼出的色轮会有「辐条」锯齿。

typst-svg 的做法更精细：给**每个扇形配一个双停靠点（2-stop）的 `linearGradient`**，从「该段起始角的颜色 c0」过渡到「该段结束角的颜色 c1」。由于第 i 段的结束角 = 第 i+1 段的起始角，于是第 i 段的 c1 == 第 i+1 段的 c0，**相邻扇形的颜色在边界上连续**，整圈拼出来就平滑了。

这个「每段一个小线性渐变」就叫**子渐变（subgradient）**，用结构体 `SVGSubGradient` 描述。

#### 4.3.2 核心流程

子渐变的**注册**发生在 `write_gradients` 的 Conic 循环里（边画扇形边登记），**写出**发生在单独的 `write_subgradients` 里（在 `finalize` 阶段集中写）。这又是 typst-svg 一贯的「渲染期只记引用、finalize 集中写定义」模式（与字形 `<symbol>`、裁剪路径同构，见 u4-l2）。

```text
渲染期（write_gradients, Conic 分支）：
  对每段扇形：
    c0 = gradient.sample(t1),  c1 = gradient.sample(t2)
    构造 SVGSubGradient { center, t0: theta1, t1: theta2, c0, c1 }
    插入 conic_subgradients 去重表 → 得到 DedupId
    扇形 <path fill="url(#该id)">

finalize 期（write_subgradients）：
  遍历 conic_subgradients，每个写成一个 <linearGradient>：
    用 t0/t1 与 center 算出 x1,y1,x2,y2（决定渐变方向）
    两个 <stop>：offset 0% → c0，offset 100% → c1
```

#### 4.3.3 源码精读

**构造并去重插入子渐变**（紧接扇形路径之后，[src/paint.rs:219-238](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L219-L238)）：

```rust
let t1 = (i as f64) / NUM_CONIC_SEGMENTS as f64;
let t2 = (i + 1) as f64 / NUM_CONIC_SEGMENTS as f64;
let subgradient = SVGSubGradient {
    center: conic.center,
    t0: theta1,
    t1: theta2,
    c0: gradient.sample(RatioOrAngle::Ratio(Ratio::new(t1))),
    c1: gradient.sample(RatioOrAngle::Ratio(Ratio::new(t2))),
};
let id = self
    .conic_subgradients
    .insert_with(subgradient.clone(), || subgradient);

// Add the path to the pattern.
pattern
    .elem("path")
    .attr("d", builder.finsish())
    .attr("fill", SvgUrl(id))
    .attr("stroke", "none")
    .attr("shape-rendering", "optimizeSpeed");
```

要点：

- `t1 = i/360`、`t2 = (i+1)/360`：把「段序号」换算成渐变参数 `t ∈ [0,1]`（0 是第一个停靠点，1 是最后一个）。`gradient.sample(t)` 在该参数处采样颜色（`sample` 定义见 [gradient.rs:784-800](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/gradient.rs#L784-L800)）。
- `SVGSubGradient` 同时存了**几何**（`center`、角度 `t0/t1`）和**颜色**（`c0/c1`），因为它既要决定 linearGradient 的方向、又要决定两个 stop 的颜色。
- `insert_with` 把子渐变塞进 `conic_subgradients` 去重表（命名空间字符 `'s'`，见 [src/lib.rs:279](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L279)），相同子渐变共用一个 `id`。
- 扇形 path 的 `fill` 用 `SvgUrl(id)` 包成 `url(#id)` 引用这个子渐变；`stroke="none"` 不要描边；`shape-rendering="optimizeSpeed"` 告诉渲染器优先速度而非抗锯齿精度（360 段已经够细，无需再为单段做高开销抗锯齿）。

**`SVGSubGradient` 结构体**（[src/paint.rs:415-428](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L415-L428)）：

```rust
/// A subgradient for conic gradients.
#[derive(Clone, Hash)]
pub struct SVGSubGradient {
    /// The center point of the gradient.
    center: Axes<Ratio>,
    /// The start point of the subgradient.
    t0: Angle,
    /// The end point of the subgradient.
    t1: Angle,
    /// The color at the start point of the subgradient.
    c0: Color,
    /// The color at the end point of the subgradient.
    c1: Color,
}
```

派生了 `Hash`，所以可以直接作为 `Deduplicator` 的 key（u6-l3 会讲 `Deduplicator` 如何用 `hash128` 去重）。

**集中写出子渐变**（[src/paint.rs:287-316](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L287-L316)）：

```rust
pub(super) fn write_subgradients(&mut self, svg: &mut SvgElem) {
    if self.conic_subgradients.is_empty() {
        return;
    }

    let mut defs = svg.elem("defs");
    for (id, gradient) in self.conic_subgradients.iter() {
        let x1 = 2.0 - gradient.t0.cos() + gradient.center.x.get();
        let y1 = gradient.t0.sin() + gradient.center.y.get();
        let x2 = 2.0 - gradient.t1.cos() + gradient.center.x.get();
        let y2 = gradient.t1.sin() + gradient.center.y.get();

        defs.elem("linearGradient")
            .attr("id", id)
            .attr("gradientUnits", "objectBoundingBox")
            .attr("x1", x1)
            .attr("y1", y1)
            .attr("x2", x2)
            .attr("y2", y2)
            .with(|svg| {
                svg.elem("stop")
                    .attr("offset", "0%")
                    .attr("stop-color", gradient.c0.to_hex());

                svg.elem("stop")
                    .attr("offset", "100%")
                    .attr("stop-color", gradient.c1.to_hex());
            });
    }
}
```

注意几个设计：

- `gradientUnits="objectBoundingBox"`：子渐变的坐标系绑定到**引用它的扇形 path 的包围盒**（而不是整个 SVG 的用户空间）。每个扇形都很小，所以这个渐变是「为这一小片量身定做」的。
- `x1/y1/x2/y2` 由 `t0/t1` 角度与圆心算出，决定渐变线的**方向**：方向向量 `(x2-x1, y2-y1)` 沿该扇形切向，正是颜色应当变化的方向。前缀 `2.0 -` 是一个共同偏移（同时加到 x1、x2 上），只平移不影响方向，把渐变线推到包围盒外使整段扇形落在渐变的有效过渡区内。这块精确的像素映射较绕，**待本地验证**其细粒度效果。
- 颜色用 `to_hex()` 写成 sRGB 十六进制（u5-l1 讲过的纯色序列化快速路径），子渐变不保留 Oklab 等原色彩空间——因为每段跨度只有 1°，颜色差异极小，烘焙成 sRGB 已无可见损失。

#### 4.3.4 代码实践

**实践目标**：验证「相邻扇形颜色连续」这一关键性质。

**操作步骤**：

1. 读循环体里 `t1 = i/360`、`t2 = (i+1)/360`（[src/paint.rs:219-220](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L219-L220)）。
2. 对段 `i`：`c1 = sample((i+1)/360)`；对段 `i+1`：`c0' = sample((i+1)/360)`。
3. 因为两个 `sample` 的入参完全相同，且 `sample` 是确定性的纯函数，所以 `c1(段i) == c0'(段i+1)`。

**需要观察的现象**：相邻两段的公共边界上，前一段 100% 停靠点的颜色与后一段 0% 停靠点的颜色严格相等。

**预期结果**：从而整圈 360 段在角度方向上颜色连续，只在径向（圆心方向）才可能有跳变——但人眼对切向连续敏感、对径向不敏感，所以视觉上是一张平滑色轮。这就是「每段单独子渐变」相对「每段填单一颜色」的全部收益。

#### 4.3.5 小练习与答案

**练习 1**：如果改成「每段填单一颜色（取段中点颜色）」，会比现在省多少？

**参考答案**：会省掉全部 360 个 `<linearGradient>` 子渐变定义（每个含 2 个 stop），`<path>` 改成 `fill="#rrggbb"` 直接填色。体积省一截，但相邻段边界出现颜色跳变（c_中点_i ≠ c_中点_i+1 的过渡不连续），色轮出现可见辐条。这是「体积 vs 平滑度」的经典取舍。

**练习 2**：为什么 `SVGSubGradient` 要派生 `Hash`？

**参考答案**：因为它要作为 `Deduplicator` 的 key。当文档里多个圆锥渐变产生了几何与颜色完全相同的子段（例如两个一样的彩虹圆锥），去重表会让它们共用同一个子渐变 ID，避免重复写出相同的 `<linearGradient>`。`Hash` 是 `Deduplicator::insert_with` 计算 `hash128` 的前提（详见 u6-l3）。

---

### 4.4 三层去重与 finalize 写出顺序

#### 4.4.1 概念说明

圆锥渐变在 typst-svg 的去重体系里占了**三个**命名空间，比线性/径向多一层。回顾 u5-l2 讲过的「源 + 引用」两层模型，圆锥在此基础上多了「子渐变」层：

| 层 | Deduplicator 字段 | kind 字符 | 写出函数 | SVG 形态 |
| --- | --- | --- | --- | --- |
| 源（不带变换） | `gradients` | `'f'` | `write_gradients` | `<pattern>` 含 360 个 `<path>` |
| 引用（带变换） | `gradient_refs` | `'r'` | `write_gradient_refs` | 空壳 `<pattern patternTransform=... href=#源>` |
| 子渐变 | `conic_subgradients` | `'s'` | `write_subgradients` | 360 个双停靠 `<linearGradient>` |

#### 4.4.2 核心流程

`finalize` 集中写出全部定义，顺序固定（[src/lib.rs:411-419](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L411-L419)）：

```rust
fn finalize(mut self, mut svg: SvgElem) {
    self.write_glyph_defs(&mut svg);
    self.write_clip_path_defs(&mut svg);
    self.write_gradients(&mut svg);        // 源 pattern（含 conic 的 360 path）
    self.write_gradient_refs(&mut svg);    // 引用 pattern（含 conic 的 patternTransform）
    self.write_subgradients(&mut svg);     // conic 专属：360 个 linearGradient
    self.write_tilings(&mut svg);
    self.write_tiling_refs(&mut svg);
}
```

注意 `write_subgradients` 排在 `write_gradients` 与 `write_gradient_refs` **之后**——先写引用了它们的源 pattern 和 ref，再写被 pattern 内部 `fill` 引用的子渐变。SVG 对 `<defs>` 内部的引用顺序没有强制要求（只要在同一文档内、渲染前已定义即可），但保持稳定的写出顺序有利于可复现输出与 diff 友好。

#### 4.4.3 源码精读

**圆锥引用写成带 `patternTransform` 的空壳 pattern**（`write_gradient_refs`，[src/paint.rs:325-336](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L325-L336)）：

```rust
let (elem_name, transform_name) = match gradient_ref.kind {
    GradientKind::Linear => ("linearGradient", "gradientTransform"),
    GradientKind::Radial => ("radialGradient", "gradientTransform"),
    GradientKind::Conic => ("pattern", "patternTransform"),
};
defs.elem(elem_name)
    .attr(transform_name, SvgTransform(gradient_ref.transform))
    .attr("id", id)
    .attr("href", SvgIdRef(gradient_ref.id))
    .attr("xlink:href", SvgIdRef(gradient_ref.id));
```

关键点：`GradientKind::Conic` 时，元素名是 `pattern`、变换属性名是 `patternTransform`（而线性/径向是 `gradientTransform`）。这是因为圆锥的源本就是 pattern。这个空壳 pattern 只带「变换 + id + href」，通过 `href`（及兼容用的 `xlink:href`）指回那个含 360 个 path 的源 pattern——这就是 u5-l2 讲的「源 + 引用」体积优化在圆锥上的体现：同一圆锥渐变无论被多少个形状使用，**真正的 360 个 path 只存一份**，每个不同使用变换各存一个近乎空的引用 pattern。

`GradientKind` 由 `&Gradient` 转换而来（[src/paint.rs:441-449](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L441-L449)），让 `GradientRef` 只需记一个轻量的「种类」标记，而不必克隆整个渐变。

#### 4.4.4 代码实践

**实践目标**：把一个圆锥渐变「从 fill 到最终 SVG 定义」的完整数据流走一遍。

**操作步骤**：按以下顺序阅读源码，画一张调用链图。

1. 某形状 `fill: gradient.conic(...)` → `render_shape` → `write_fill`（[src/paint.rs:44-47](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L44-L47)）→ `push_gradient`。
2. `push_gradient`（[src/paint.rs:66-85](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L66-L85)）：先把 `(gradient, aspect_ratio)` 插入 `gradients`（源，`'f'`）；若使用变换 `ts` 非单位矩阵，再插入 `gradient_refs`（引用，`'r'`）。返回的 id 经 `SvgUrl` 包成 `fill="url(#id)"` 写到形状上。
3. `finalize` → `write_gradients` 的 Conic 分支：为这个源 id 写出含 360 个 path 的 `<pattern>`，每个 path 又触发 `conic_subgradients`（`'s'`）的登记。
4. `finalize` → `write_gradient_refs`：为引用 id 写出空壳 `<pattern patternTransform=... href=#源>`。
5. `finalize` → `write_subgradients`：为每个子渐变 id 写出双停靠 `<linearGradient>`。

**需要观察的现象**：三个命名空间（`'f'`/`'r'`/`'s'`）各司其职，互不干扰；同一个 id（如 `f3A2B...`）绝不会既指源 pattern 又指子渐变。

**预期结果**：能画出「形状 fill → url(#r...) → 空 pattern 带 patternTransform → href(#f...) → 源 pattern 内 360 个 path → 每个 path fill=url(#s...) → 双停靠 linearGradient」的完整引用链。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `write_subgradients` 必须在 `write_gradients` 之后调用，而不能之前？

**参考答案**：因为子渐变是在 `write_gradients` 的 Conic 循环里**边画扇形边登记**到 `conic_subgradients` 表的（[src/paint.rs:228-230](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L228-L230)）。若先调 `write_subgradients`，此时表还是空的，什么也写不出。必须先让 `write_gradients` 把所有源 pattern（及其内含的子渐变登记）跑完，`conic_subgradients` 表才被填满，`write_subgradients` 才有内容可写。

**练习 2**：一个文档里有 3 个**完全相同**的圆锥渐变、各用在 1 个形状上，最终会写出几个源 pattern、几个引用 pattern、几组（×360）子渐变？

**参考答案**：源 pattern **1 个**（`(gradient, aspect_ratio)` 相同，去重）；引用 pattern **3 个**（每个形状的使用变换 `ts` 可能不同，各存一份，若变换也全同则合并为 1 个）；子渐变 **1 组共 360 个**（子渐变 key 含 center/角度/颜色，三者渐变相同故完全一致，去重）。这就是三层去重的体积收益。

---

## 5. 综合实践

**任务**：解释 `inverse_ratio`（`aspect_ratio.recip()`）在圆锥角度修正中的作用，并说明每个扇形为什么要单独引用一个子渐变 ID，而不是直接填单一颜色。

请按以下步骤完成（阅读 + 推导型实践，无需运行）：

### 步骤一：推导 `inverse_ratio` 的代数效果

1. 读 Conic 分支里这两行（[src/paint.rs:184](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L184) 与 [src/paint.rs:190-197](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L190-L197)）：

   ```rust
   let inverse_ratio = aspect_ratio.recip();
   ...
   let theta1 = -Gradient::correct_aspect_ratio(
       conic.angle + (dtheta * i as f64),
       inverse_ratio,
   );
   ```

2. 回顾 `correct_aspect_ratio` 的定义（[gradient.rs:994-996](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/gradient.rs#L994-L996)）：

   \[
   \text{correct\_aspect\_ratio}(\alpha, r) = \operatorname{atan2}\!\left(\frac{\sin \alpha}{|r|},\; \cos \alpha\right)
   \]

3. 因为 `inverse_ratio = 1/\text{aspect\_ratio}`，代入后圆锥实际执行的是：

   \[
   \theta = -\operatorname{atan2}\!\left(\sin \alpha \cdot |\text{aspect\_ratio}|,\; \cos \alpha\right)
   \]

   而线性渐变（u5-l3）执行的是 `atan2(sin α / |aspect_ratio|, cos α)`。**一个乘、一个除**——方向相反。

### 步骤二：解释「为什么相反」

结合以下事实组织你的解释（可标注「待本地验证」于不确定处）：

- 线性渐变的角度是**一个参数**，交给 SVG 渲染器在 `gradientTransform`（含非等比缩放）**之后**解释；
- 圆锥渐变的几何已经被**烘焙成 pattern 内容的坐标**（360 个扇形的绝对位置），这些坐标在 pattern 的局部坐标系里固定，再被 `patternTransform` 整体变换；
- 由于「参数 vs 烘焙坐标」的差异，同一个非等比缩放在两者上引起的角度畸变方向相反，因此预补偿的方向也相反——圆锥用倒数。
- 此外最外层还有一个取负 `-`，把数学惯例的逆时针翻成 SVG（y 轴朝下）坐标系下的顺时针，匹配 Typst 圆锥渐变的旋转方向约定。

### 步骤三：解释「为什么每段单独子渐变而非单一颜色」

用 4.3 的结论作答，要点：

1. 每段扇形两端颜色 `c0 = sample(t1)`、`c1 = sample(t2)`，子渐变在段内从 c0 过渡到 c1。
2. 第 i 段的 `t2 == 第 i+1 段的 t1`，故 `c1_i == c0_{i+1}`，相邻段边界颜色严格连续。
3. 若改填单一颜色，相邻段中点颜色不连续，色轮出现可见辐条；子渐变以「每段多一个 linearGradient 定义」为代价，换取整圈视觉平滑。
4. 子渐变还经 `conic_subgradients`（`'s'`）去重，多个相同圆锥共用，控制了体积膨胀。

### 预期产出

一段 200~300 字的中文说明，覆盖：(a) `inverse_ratio` 使修正从「除以宽高比」变成「乘以宽高比」，方向与线性渐变相反；(b) 取负的顺时针作用；(c) 子渐变保证相邻段颜色连续、避免辐条锯齿。

> 若想验证你的解释，可用 typst-cli 编译一个**非正方形**形状的圆锥渐变（如 `#rect(width: 200pt, height: 100pt, fill: gradient.conic(..color.map.rainbow))`）输出 SVG，检查 360 个扇形是否仍拼成一个方向正确的椭圆色轮——这能间接验证 `inverse_ratio` 的修正是否正确（**待本地验证**）。

## 6. 本讲小结

- SVG 没有原生圆锥渐变，typst-svg 用 **`NUM_CONIC_SEGMENTS = 360`** 段扇形 `<pattern>` 近似，段数是经验值，可调以做体积优化。
- 每段扇形由 `move_to(圆心) → line_to(弧端) → arc → close` 拼成；端点用 `-2·cos / +2·sin` 参数化，`correct_tiling_pos` 把 Ratio 坐标换算到 pattern 的 [0,1] 绘图坐标系（`r → (r+0.5)·0.5`）。
- 角度修正用 `inverse_ratio = aspect_ratio.recip()`，与线性渐变的「除以宽高比」相反（圆锥是「乘」），并在最外层取负转为 SVG 顺时针方向。
- 每段配一个**双停靠点子渐变** `SVGSubGradient`（含 center/角度/双色），由 `write_subgradients` 集中写成 `<linearGradient gradientUnits="objectBoundingBox">`；相邻段 `c1_i == c0_{i+1}` 保证颜色连续，避免辐条锯齿。
- 圆锥在去重体系里占三层：源 pattern（`'f'`）、引用 pattern（`'r'`，带 `patternTransform` + `href`）、子渐变（`'s'`）；`write_subgradients` 必须在 `write_gradients` 之后，因为子渐变是在源 pattern 的循环里登记的。

## 7. 下一步学习建议

- **u5-l5 平铺 Tiling**：本讲的源 pattern 是「借用 `<pattern>` 装扇形」，而 tiling 才是 pattern 的「本职」——重复填充图案。两讲对照阅读，能看清 typst-svg 如何把同一个 SVG 元素用于两种截然不同的目的，以及 `patternTransform` 在引用层的拼接细节。
- **u6-l3 去重机制 Deduplicator**：本讲反复提到 `'f'/'r'/'s'` 三个命名空间与 `insert_with`，其底层 `hash128` + `IndexMap` + `DedupId` 编码将在 u6-l3 系统讲解，读完能彻底理解「按 key 哈希、惰性构造值」的去重语义。
- **拓展阅读**：若对角度修正的几何推导感兴趣，可结合 typst-library 的 `correct_aspect_ratio` 与 SVG `patternTransform` 的非等比缩放语义，自行推导「参数式 vs 烘焙式」两种渐变下角度畸变方向为何相反。

# 线性与径向渐变实现

## 1. 本讲目标

上一篇（u5-l2）讲清了「源（source）+ 引用（ref）」两层去重模型：渲染期 `push_gradient` 只登记、`finalize` 阶段才真正把定义写进 `<defs>`。但那个模型把「源定义」当成了黑盒——它到底长什么样、坐标怎么算出来，本讲就来拆开。

具体地，本讲只聚焦 `write_gradients` 与 `write_gradient_refs` 里**线性渐变（Linear）与径向渐变（Radial）**两个分支（圆锥渐变 Conic 因实现特殊，单独留给 u5-l4）。学完后你应当能够：

1. 说清一个线性渐变的「角度」如何经过 `correct_aspect_ratio` 宽高比修正、再经 `quadrant()` 选起点角、最后算出 SVG 的 `x1/y1/x2/y2` 四个端点坐标，并能解释 `factor = |sin| + |cos|` 为什么恰好让 100% 颜色落在对角顶点。
2. 看懂径向渐变的「双圆模型」（外圆 `cx/cy/r` + 焦点圆 `fx/fy/fr`）如何一一对应 `RadialGradient` 结构字段，并解释为什么径向分支不需要 `correct_aspect_ratio`。
3. 解释 `spreadMethod="pad"` 与 `gradientUnits="userSpaceOnUse"` 这两个固定属性的作用。
4. 理解 `generate_intermediate_stops_for_rgb_interpolation` 这个「兼容读取器」workaround：为什么要在两个停靠点之间插出一串中间 `<stop>`，以及它的二分判定准则。
5. 读懂 `write_gradient_refs` 如何把带变换的引用写成近乎空壳的 `linearGradient`/`radialGradient`，仅靠 `gradientTransform` + `href` 指回源。

## 2. 前置知识

本讲假定你已经读过 u5-l2（两层去重模型）和 u3-l1（`SvgPathBuilder`）。下面把几个 SVG 与 Typst 的基础概念再对齐一次。

- **SVG `linearGradient`**：用一条「渐变线」的方向决定颜色过渡。两个端点 `(x1,y1) → (x2,y2)` 定义这条线，0% 颜色在起点、100% 颜色在终点，垂直于渐变线的方向颜色相同。
- **SVG `radialGradient`**：用一个圆（圆心 `cx,cy`、半径 `r`）定义 100% 颜色所在的「外边界」，再用一个焦点圆（`fx,fy,fr`）定义 0% 颜色起始的「内边界」。最简单的情况下焦点就是外圆圆心、内半径为 0。
- **`gradientUnits`**：决定 `x1/y1/...` 这些坐标在哪个坐标系里度量。`userSpaceOnUse` = 用当前用户坐标系（绝对坐标）；`objectBoundingBox` = 用元素自身包围盒的比例（0~1）。typst-svg 的线性/径向源渐变**统一用 `userSpaceOnUse`**，坐标手工写在单位方形 `[0,1]` 内，再用 `gradientTransform` 定位（见 4.1）。
- **`spreadMethod`**：渐变参数超出 `[0,1]` 范围时怎么办。`pad` = 钳位到端点颜色（超出 100% 的地方一律显示 100% 颜色）；另外还有 `reflect`（镜像）、`repeat`（循环）。typst-svg 固定用 `pad`。
- **`aspect_ratio`**：本讲里特指形状填充区域的「宽 / 高」，由 `Size::aspect_ratio()` 给出（即 `size.x / size.y`）。它在上游 `shape_fill_size()` 里算好，随 `write_fill` 一路传进 `push_gradient` → `write_gradients`。
- **`correct_aspect_ratio(angle, aspect_ratio)`**：宽高比角度修正函数（定义在 typst-library 的 `Gradient` 上）。本讲会反复用到它，直觉是「在单位方形里把角度预先扭一下，抵消后续 `gradientTransform` 非等比缩放带来的角度畸变」。
- **`ts`（Transform）**：把「单位方形里的渐变」映射到「某个具体使用位置」的变换，由 `shape_paint_transform` 算出（u3-l2 / u5-l2 已讲）。`ts.is_identity()` 时直接复用源定义，否则生成引用。

> 一句话回顾两层去重：源渐变**不带** `gradientTransform`、按 `(gradient, aspect_ratio)` 去重，全文档只存一份；引用只记「源 ID + `ts`」，每个不同使用变换各存一份。本讲 4.1~4.4 讲「源」怎么写，4.5 讲「引用」怎么写。

## 3. 本讲源码地图

本讲以 `src/paint.rs` 为主战场，并向上游追溯到 typst-library 的渐变定义：

| 文件 | 本讲涉及的关键符号 | 作用 |
| --- | --- | --- |
| `src/paint.rs` | `write_gradients`（Linear/Radial 分支）、`write_gradient_refs`、`GradientKind` | 源渐变的几何计算与写出、引用空壳的写出 |
| `src/shape.rs` | `shape_fill_size`、`shape_paint_transform` | 上游：算出传入的 `aspect_ratio` 与 `ts` |
| `src/lib.rs` | `finalize` | 调用顺序：先 `write_gradients` 写源，再 `write_gradient_refs` 写引用 |
| `crates/typst-library/.../gradient.rs` | `correct_aspect_ratio`、`anti_alias`、`generate_intermediate_stops_for_rgb_interpolation`、`LinearGradient`、`RadialGradient` | 角度修正、抗锯齿标志、中间停靠点算法、渐变数据结构 |
| `crates/typst-library/.../angle.rs` | `Angle::quadrant`、`Quadrant` | 把角度归到四个象限，用于选线性渐变起点角 |

数据流（从上游到写出）：

```
shape_fill_size  ──► aspect_ratio  ┐
shape_paint_transform ─► ts ───────┤
                                   ▼
render_shape ──► write_fill ──► push_gradient(gradient, aspect_ratio, ts)
                                  │  登记进 self.gradients（源）/ self.gradient_refs（引用）
                                  ▼
finalize ──► write_gradients        ◄── 本讲 4.1~4.4：写「源」（无 gradientTransform）
          ──► write_gradient_refs   ◄── 本讲 4.5：写「引用」（带 gradientTransform + href）
```

`finalize` 的调用顺序见 [src/lib.rs:411-419](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L411-L419)：先写字形、裁剪路径，再 `write_gradients`（源）→ `write_gradient_refs`（引用）→ `write_subgradients`（圆锥专用）→ 平铺相关。本讲只关心前两个渐变调用。

## 4. 核心概念与源码讲解

### 4.1 write_gradients 的三分支结构与「源不带变换」

#### 4.1.1 概念说明

`write_gradients` 的职责很纯粹：把 `self.gradients` 去重表里登记过的所有**源渐变**写进一个 `<defs>` 块。注意「源」这个词——这些渐变**不带任何 `gradientTransform`**，它们都定义在单位方形 `[0,1] × [0,1]` 的坐标系里。真正把它们搬到形状上的变换，由 4.5 的「引用」单独承载。这正是两层去重的核心：源只存几何，变换另算。

函数体是一个对 `self.gradients` 的遍历，内部按 `Gradient::Linear / Radial / Conic` 三路 `match`。其中线性、径向走「生成元素 → 写停靠点」的公共尾巴；圆锥走自己的 `<pattern>` 逻辑后用 `continue` 跳过停靠点生成（留给 u5-l4）。

#### 4.1.2 核心流程

```
对每个 (id, (gradient, aspect_ratio)) in self.gradients:
    match gradient:
        Linear  → 建 <linearGradient>，算 x1/y1/x2/y2（见 4.2）
        Radial  → 建 <radialGradient>，直接抄 cx/cy/r/fx/fy/fr（见 4.3）
        Conic   → 建 <pattern>，360 段扇形……然后 continue（u5-l4）
    （Linear/Radial 公共尾巴）写出所有 <stop> 停靠点（见 4.4）
```

每个渐变元素开头都会写三个固定属性：`id`（来自去重表）、`spreadMethod="pad"`、`gradientUnits="userSpaceOnUse"`。

#### 4.1.3 源码精读

函数入口与空表快速返回见 [src/paint.rs:117-123](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L117-L123)（开 `<defs>`、遍历去重表）。三分支 `match` 的结构见 [src/paint.rs:124-244](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L124-L244)，其中 Conic 分支末尾的 `continue` 见 [src/paint.rs:241-243](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L241-L243)——它跳过了后面整段停靠点生成代码。

#### 4.1.4 代码实践

**目标**：确认「源不带变换」这一不变量。

1. 在 [src/paint.rs:125-166](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L125-L166)（Linear + Radial 分支）里搜索 `gradientTransform`。
2. 你会发现：源定义里**只字未提** `gradientTransform`；它只出现在 4.5 的 `write_gradient_refs` 里。
3. 再对照 `push_gradient`（u5-l2）确认：源的去重键是 `(gradient, aspect_ratio)`，**不含 `ts`**；只有 `ts` 非单位矩阵时才另外插入一个 `GradientRef`。

**预期结果**：你能向别人说清「为什么同一个渐变被十处使用、变换各异时，源几何只写一遍」——因为源与使用变换解耦。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Conic 分支要用 `continue` 跳过公共的停靠点生成代码？
**答案**：圆锥渐变不是用一个渐变元素 + 一串 `<stop>` 实现的，而是用 360 段扇形 `<pattern>`，每段扇形各自引用一个独立的 sub-gradient（双停靠 `linearGradient`）。它的「停靠点」逻辑完全不同，所以必须跳过 Linear/Radial 的公共尾巴。

**练习 2**：`gradientUnits="userSpaceOnUse"` 换成 `objectBoundingBox` 会出什么问题？
**答案**：`objectBoundingBox` 会把坐标解释为「相对被填充元素包围盒的比例」，而 typst-svg 的坐标是手工写在单位方形里、再靠 `gradientTransform`（`ts`）定位的。两者叠加会把渐变放错位置。`userSpaceOnUse` 让坐标落在受 `gradientTransform` 控制的用户坐标系里，typst-svg 才能完全掌控定位。（对照：圆锥 sub-gradient 反而用 `objectBoundingBox`，见 [src/paint.rs:301](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L301)。）

---

### 4.2 线性渐变：从角度到端点坐标

#### 4.2.1 概念说明

线性渐变的核心问题：用户给的是一个**角度**（比如 `45deg`），但 SVG `<linearGradient>` 要的是**两个端点** `(x1,y1) → (x2,y2)`。本模块就是把角度翻译成端点。

直觉分三步：

1. **宽高比修正**：先用 `correct_aspect_ratio` 把角度「预先扭一下」。因为源渐变画在单位正方形里，而真实形状往往不是正方形，后续 `gradientTransform` 的非等比缩放会改变视觉角度；预先修正能让最终视觉效果与 Typst 自带的参考采样器 `sample_at` 完全一致。
2. **选起点角**：用 `angle.quadrant()` 判断角度落在第几象限，据此在单位方形的四个角里挑一个作为渐变起点 `(x1,y1)`（0% 颜色所在的角）。
3. **算终点**：沿方向向量 `(cos, sin)` 走 `factor` 步得到终点 `(x2,y2)`，其中 `factor` 是一个精心选的缩放系数。

#### 4.2.2 核心流程

修正角度（`r = aspect_ratio = 宽/高`）：

\[ \theta' = \operatorname{atan2}\!\left(\frac{\sin\theta}{r},\ \cos\theta\right) \]

源码里的 `correct_aspect_ratio` 就是这一行，见 [gradient.rs:994-996](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/gradient.rs#L994-L996)。

然后取三角分量并算缩放系数：

```text
let (sin, cos) = (θ'.sin(), θ'.cos());
let factor = sin.abs() + cos.abs();
```

选起点角（注意 SVG 的 y 轴朝下，所以 (0,0) 是左上角）：

| `angle.quadrant()` | 角度范围 | 起点 `(x1,y1)` | 物理位置 |
| --- | --- | --- | --- |
| `First` | 0°–90° | `(0.0, 0.0)` | 左上 |
| `Second` | 90°–180° | `(1.0, 0.0)` | 右上 |
| `Third` | 180°–270° | `(1.0, 1.0)` | 右下 |
| `Fourth` | 270°–360° | `(0.0, 1.0)` | 左下 |

终点：

\[ x_2 = x_1 + \cos\theta' \cdot \text{factor}, \qquad y_2 = y_1 + \sin\theta' \cdot \text{factor} \]

**为什么 `factor = |sin| + |cos|`？** 这是一个关键的设计。定义渐变参数（沿渐变线的归一化位置）为点到起点向量在方向上的投影：

\[ t(x,y) = \frac{(x - x_1)\,|\cos\theta'| + (y - y_1)\,|\sin\theta'|}{\text{factor}} \]

代入「对角顶点」（第一象限时即 `(1,1)`）：

\[ t(1,1) = \frac{|\cos\theta'| + |\sin\theta'|}{|\sin\theta'| + |\cos\theta'|} = 1 \]

也就是说，**`factor` 这个取值恰好让对角顶点落在 `t = 1`（100% 颜色）**，起点角落在 `t = 0`（0% 颜色）。这正是 CSS 渐变的约定：渐变线从「最近的角」延伸到「最远的对角」。同时，这个 `t(x,y)` 与 Typst 参考采样器 `sample_at` 里的线性分支公式完全相同（见 [gradient.rs:912-929](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/gradient.rs#L912-L929)），从而保证 SVG 输出与 Typst 位图渲染逐参数一致。

> 一个副产物：`factor` 在 θ'=45° 时取到最大值 \(|\sin 45°|+|\cos 45°| = \sqrt{2}\)，对应单位方形里最长的渐变线（对角线）；在 θ'=0°/90° 时退化为 1。

#### 4.2.3 源码精读

线性分支整体见 [src/paint.rs:125-153](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L125-L153)，关键几行：

- `correct_aspect_ratio` 调用与 `(sin, cos)`、`factor` 计算：[src/paint.rs:131-136](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L131-L136) —— 先修正角度，再取三角分量，factor = |sin|+|cos|。
- `quadrant()` 选起点角：[src/paint.rs:138-143](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L138-L143)。
- 终点与四个属性写出：[src/paint.rs:144-150](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L144-L150)。

上游 `quadrant()` 的实现见 [angle.rs:126-137](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/layout/angle.rs#L126-L137)：先把角度对 360° 取模，再按 90°/180°/270° 划分到 `First/Second/Third/Fourth`（`Quadrant` 枚举见 [angle.rs:258-268](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/layout/angle.rs#L258-L268)）。

`aspect_ratio` 的来源是 `shape_fill_size(state, paint, shape).aspect_ratio()`，即填充区尺寸的宽高比，见 [src/shape.rs:26](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/shape.rs#L26)；`Size::aspect_ratio()` 的定义是 `x / y`，见 [size.rs:27-29](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/layout/size.rs#L27-L29)。

#### 4.2.4 代码实践

**目标**：手工跟踪一个画在正方形上的 45° 线性渐变，推出最终 `x1,y1,x2,y2`。

设形状是正方形，故 `aspect_ratio = 1.0`，用户角度 `θ = 45°`。

1. **宽高比修正**：`correct_aspect_ratio(45°, 1.0) = atan2(sin45/1, cos45) = atan2(0.707, 0.707) = 45°`。正方形下角度不变。
2. **三角分量**：`sin = 0.707`，`cos = 0.707`。
3. **factor**：`0.707 + 0.707 = 1.414`（即 √2）。
4. **象限**：`quadrant(45°)` → 45 ≤ 90 → `First` → `(x1,y1) = (0, 0)`。
5. **终点**：`x2 = 0 + 0.707 × 1.414 = 1.0`，`y2 = 0 + 0.707 × 1.414 = 1.0`。

最终端点 `(0,0) → (1,1)`，正方形对角线，符合 45° 直觉。

**再解释两个固定属性**：

- `spreadMethod="pad"`：渐变参数超出 `[0,1]` 的区域钳位到端点颜色。本例里单位方形的四个角 `t` 值都在 `[0,1]` 内（`(0,0)→0`、`(1,1)→1`），所以 pad 对正方形内部无影响；但当渐变用于 `RelativeTo::Parent`、形状只覆盖渐变盒的一部分时，形状外延会被钳到端点纯色，这正是期望行为。
- `gradientUnits="userSpaceOnUse"`：`x1/y1/x2/y2` 在用户坐标系（受 `gradientTransform` 控制）里度量，typst-svg 才能用单位方形坐标 + `ts` 精确定位。

**预期结果**：你能口算出任一角度在正方形上的端点；并能解释为什么 0° 渐变会得到 `(0,0)→(1,0)`（factor=1，第一象限，水平）。

#### 4.2.5 小练习与答案

**练习 1**：宽矩形（`aspect_ratio = 2`）上的 45° 渐变，修正后的角度是多少？
**答案**：`correct_aspect_ratio(45°, 2) = atan2(sin45/2, cos45) = atan2(0.354, 0.707)`，即 `atan(0.5) ≈ 26.57°`。源渐变在单位方形里按 ≈26.57° 画，再经 `gradientTransform` 的 (2,1) 缩放映射到宽矩形上——Typst 的 `sample_at` 用同样的修正，所以两者输出一致。

**练习 2**：为什么 `factor` 在 45° 时最大、在 0°/90° 时为 1？
**答案**：`factor = |sinθ|+|cosθ|`。对 θ∈[0°,90°]，求导得极值在 θ=45°，此时 `factor = √2`（单位方形对角线长度，最长的渐变线）；θ=0° 或 90° 时一个分量为 0，`factor = 1`（水平/垂直渐变线）。

---

### 4.3 径向渐变：双圆模型

#### 4.3.1 概念说明

径向渐变不涉及角度，它的形状由**两个圆**决定：

- **外圆**（`cx, cy, r`）：100% 颜色到达的边界圆。
- **焦点圆**（`fx, fy, fr`）：0% 颜色起始的内圆。

最常见情形是焦点圆退化为外圆圆心（`fx=cx, fy=cy, fr=0`），即一个标准的从圆心向外辐散的径向渐变。但 SVG 允许焦点偏移，从而产生「光从一侧打来」的效果。

这些参数在 Typst 里直接存在 `RadialGradient` 结构的 `center / radius / focal_center / focal_radius` 字段中，全部是 `Ratio`（相对填充区尺寸的比例，0~1）。

#### 4.3.2 核心流程

径向分支**不做任何几何计算**，直接把结构字段 `.get()` 取出比值后抄进属性：

```text
cx ← radial.center.x    cy ← radial.center.y    r  ← radial.radius
fx ← radial.focal_center.x  fy ← radial.focal_center.y  fr ← radial.focal_radius
```

为什么径向不需要 `correct_aspect_ratio`？因为径向的「方向」由圆心/半径决定，没有角度概念；宽高比的影响已经由 `gradientTransform`（`ts`）的非等比缩放承担——同一个圆形径向在宽矩形上被拉成椭圆，这正是 `RelativeTo::Self_` 的预期。

#### 4.3.3 源码精读

径向分支见 [src/paint.rs:154-166](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L154-L166)：建 `radialGradient`、写固定三属性，再依次写 `cx/cy/r/fx/fy/fr`。

对应的 `RadialGradient` 结构定义见 [gradient.rs:1083-1100](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/gradient.rs#L1083-L1100)，字段语义与 SVG 属性一一对应。Typst 自己的参考采样 `sample_at` 径向分支见 [gradient.rs:931-952](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/gradient.rs#L931-L952)（用双圆几何求 `t`），SVG 输出与之匹配。

#### 4.3.4 代码实践

**目标**：理解焦点偏移的效果。

1. 阅读结构字段 [gradient.rs:1083-1100](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/gradient.rs#L1083-L1100)，确认默认焦点（`focal_center == center`、`focal_radius == 0`）。
2. 假设一个 `RadialGradient`：`center=(0.5,0.5)`、`radius=0.5`、`focal_center=(0.3,0.3)`、`focal_radius=0.0`。手推它会被写成：`cx=0.5 cy=0.5 r=0.5 fx=0.3 fy=0.3 fr=0`。
3. 解释现象：焦点偏到左上 `(0.3,0.3)`，渐变的 0% 起点从左上偏向中心，视觉上像光源在左上方。

**预期结果**：你能把任意 `RadialGradient` 字段翻译成 SVG 属性；并理解 `fr > 0` 时 0% 颜色不再是一个点而是一个小圆（内边界）。

#### 4.3.5 小练习与答案

**练习 1**：`focal_center = center` 且 `focal_radius = 0` 时，焦点圆退化成什么？
**答案**：退化为外圆圆心这一个点。此时就是最朴素的「圆心 0% → 外圆 100%」径向渐变。

**练习 2**：为什么径向分支不需要 `correct_aspect_ratio`，而线性分支需要？
**答案**：径向用圆心/半径定位，没有「角度」需要修正；非等比宽高比的影响直接由 `gradientTransform` 把圆拉成椭圆来体现。线性渐变有方向角度，非等比缩放会改变视觉角度，所以要预先用 `correct_aspect_ratio` 修正。

---

### 4.4 颜色停靠点与「兼容读取器」中间停靠点 workaround

#### 4.4.1 概念说明

线性/径向分支走完几何后，进入**公共尾巴**：写出 `<stop offset="..." stop-color="..."/>` 列表。这部分看似简单（把 `gradient.stops` 抄出来），但藏着一个重要的 workaround。

问题背景：Typst 的渐变默认在 **Oklab** 等感知均匀的色彩空间里插值（`gradient.space`）。但 SVG 的 `<stop>` 只能写颜色值，**很多 SVG 渲染器会忽略渐变本应使用的色彩空间，直接在 sRGB 里对相邻 `stop-color` 做线性插值**。这会导致 Typst 期望的 Oklab 平滑过渡在这些渲染器里出现可见色带。

解决办法：与其指望渲染器尊重色彩空间，不如**主动在两个停靠点之间预插出一串「中间停靠点」**，把 Oklab 插值的结果「烘焙」进 `stop-color` 序列。这样即使渲染器在 sRGB 里线性插值，因为相邻 stop 之间足够密、颜色差足够小，结果也接近正确。这就是 `generate_intermediate_stops_for_rgb_interpolation` 的作用——一个「兼容偷懒渲染器」的兜底。

#### 4.4.2 核心流程

遍历相邻停靠点对（`windows(2)`）：

```text
对每对相邻 (start_c, start_t) → (end_c, end_t):
    写出起始 stop：offset=start_t, stop-color=start_c.to_hex()
    若 end_t > start_t 且 gradient.anti_alias() 为真:
        对 generate_intermediate_stops_for_rgb_interpolation(start, end) 的每个 (c, t):
            写出中间 stop：offset=t, stop-color=c.to_space(space).to_hex()
（循环结束后）单独补写最后一个 stop（末端的 100% 颜色）
```

中间停靠点算法的直觉（[gradient.rs:843-904](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/gradient.rs#L843-L904)）：用**二分**判定是否需要继续细分——

1. 取当前区间的中点，分别算「精确颜色」（按渐变色彩空间采样）与「sRGB 线性插值近似颜色」（两端各 50% 混合）。
2. 算两者在 sRGB 空间的欧氏距离（误差）。
3. 若误差 > 阈值 `0.001` 且未达细分上限（`MAX_SUBDIVISIONS = 64`），继续二分；否则把当前末端作为一个中间 stop 产出。

误差度量见 [gradient.rs:863-876](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/gradient.rs#L863-L876)，阈值与上限见 [gradient.rs:848-849](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/gradient.rs#L848-L849)。

注意一个细节：写出的 `stop-color` 一律走 `to_hex()`（即落到 sRGB `#rrggbb`），见 [src/paint.rs:255-258](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L255-L258) 与 [src/paint.rs:268-273](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L268-L273)。这正契合 workaround 的目的——既然要骗过 sRGB 插值的渲染器，就给它们 sRGB 值。`anti_alias()` 标志由 `gradient.anti_alias()` 读出（[gradient.rs:975-981](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/gradient.rs#L975-L981)），它本质是「这个渐变颜色反差大、容易出现色带，需要烘焙中间停靠点」的开关。

#### 4.4.3 源码精读

公共尾巴整体见 [src/paint.rs:249-282](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L249-L282)：

- `windows(2)` 遍历相邻 stop、写起始 stop：[src/paint.rs:251-258](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L251-L258)。
- 中间停靠点 workaround 的触发与写出：[src/paint.rs:260-274](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L260-L274)（`anti_alias()` 判定在第 263 行）。
- 末端最后一个 stop 补写：[src/paint.rs:277-282](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L277-L282)。

> 代码注释 `// Unwraps of to_space ... are safe ...`（[src/paint.rs:246-248](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L246-L248)）说明：停靠点色彩空间兼容性在上游已校验，这里 `to_space().unwrap()` 安全。

#### 4.4.4 代码实践

**目标**：观察 anti-alias 对输出 `stop` 数量的影响。

1. 在 [src/paint.rs:263](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L263) 处的 `gradient.anti_alias()` 条件上加一行心智断点：若关闭，则只写原始 stop（每对只写起始 + 末端），不产生中间 stop。
2. 构造两个反差大的颜色（如纯黑 → 纯白）在 Oklab 空间的渐变，设想 `anti_alias=true` 时会在中间插出多个 stop；`anti_alias=false` 时只有首尾两个 stop。
3. 解释现象：渲染器若尊重色彩空间，两者都正确；若只在 sRGB 插值，`anti_alias=true` 的密集 stop 会让过渡更平滑。

**预期结果**：你能说清「为什么 Typst 宁可多写一堆 `<stop>` 也要开 anti-alias」——为了在不完美的 SVG 渲染器里保住渐变质量。实际渲染对比「待本地验证」（可用浏览器分别打开两种 SVG 观察）。

#### 4.4.5 小练习与答案

**练习 1**：中间 stop 的 `stop-color` 为什么用 `to_hex()` 而不保留原色彩空间（如 `oklab(...)`）？
**答案**：因为这个 workaround 的目标就是骗过「忽略色彩空间、在 sRGB 线性插值」的渲染器。把 Oklab 采样结果预先转成 sRGB hex 烘焙进 stop，相邻 stop 颜色差极小，渲染器即使 sRGB 插值也接近正确。

**练习 2**：`end_t > start_t` 这个条件排除了什么情况？
**答案**：排除了两个停靠点 offset 相同（`end_t == start_t`）的退化情况——offset 相同就没有区间可插值，二分会除零或无意义。只有存在正向区间时才生成中间 stop。

---

### 4.5 引用定义：write_gradient_refs

#### 4.5.1 概念说明

4.1~4.4 写的都是「源」。当一个渐变的使用变换 `ts` 不是单位矩阵时（现实中绝大多数情况，因为形状包围盒极少恰好 1pt×1pt），`push_gradient` 会额外插入一个 `GradientRef`。`write_gradient_refs` 就把这些引用写出来。

每个引用是一个**近乎空壳**的渐变元素：它自己不定义任何几何或停靠点，只带三样东西——

1. `gradientTransform`（或圆锥的 `patternTransform`）：使用变换 `ts`。
2. `id`：引用自己的 ID（命名空间 `r`）。
3. `href` + `xlink:href`：指向源渐变的 ID。

这样形状上的 `fill="url(#引用ID)"` 实际渲染时，会先把 `gradientTransform` 套上去，再沿 `href` 找到源定义的几何与 stop。源几何只存一份、复用 N 次，体积收益巨大。

`GradientKind`（`Linear`/`Radial`/`Conic`）决定两件事：元素名（`linearGradient`/`radialGradient`/`pattern`）和变换属性名（`gradientTransform`/`gradientTransform`/`patternTransform`）。它存在 `GradientRef.kind` 里，是为了**不必 clone 整个渐变**就能知道该用哪种元素。

#### 4.5.2 核心流程

```text
对每个 (id, gradient_ref) in self.gradient_refs:
    按 gradient_ref.kind 选 (elem_name, transform_name):
        Linear  → ("linearGradient",  "gradientTransform")
        Radial  → ("radialGradient",  "gradientTransform")
        Conic   → ("pattern",         "patternTransform")
    建 <elem_name>:
        attr transform_name = gradient_ref.transform   # 即 ts
        attr id            = id
        attr href          = "#<源ID>"
        attr xlink:href    = "#<源ID>"     # 兼容旧标准
```

`href` 与 `xlink:href` 同时写是出于兼容：现代浏览器认 `href`，部分老工具只认 `xlink:href`。

#### 4.5.3 源码精读

`write_gradient_refs` 见 [src/paint.rs:318-338](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L318-L338)：

- `GradientKind` → 元素名/变换名映射：[src/paint.rs:325-329](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L325-L329)。
- 写变换、id、双 href：[src/paint.rs:330-336](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L330-L336)。

`GradientRef` 与 `GradientKind` 结构见 [src/paint.rs:400-449](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L400-L449)；`From<&Gradient> for GradientKind` 的分发见 [src/paint.rs:441-449](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L441-L449)。

#### 4.5.4 代码实践

**目标**：跟踪一个 `RelativeTo::Parent` 渐变的引用链。

1. 阅读 `shape_paint_transform` 的 `RelativeTo::Parent` 分支（[src/shape.rs:89-94](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/shape.rs#L89-L94)）：`ts = scale(state.size).post_concat(state.transform.invert())`。这把渐变锚定到父级 frame 尺寸，并用 `state.transform` 的逆抵消形状自身位移。
2. 由于这个 `ts` 几乎一定不是单位矩阵，`push_gradient` 会走引用路径（u5-l2 的 [src/paint.rs:80-84](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L80-L84)），生成一个 `GradientRef`。
3. 在 `write_gradient_refs` 里，这个引用被写成 `<linearGradient gradientTransform="matrix(...)" id="r..." href="#f..." xlink:href="#f..."/>`。

**预期结果**：你能解释「为什么父级相对的渐变会多出一个 `r` 命名空间的引用元素，且它的 `gradientTransform` 把渐变钉在父 frame 上」。

#### 4.5.5 小练习与答案

**练习 1**：同一个线性渐变被两个形状使用，两者 `aspect_ratio` 相同但位置不同（`ts` 不同）。会生成几个源、几个引用？
**答案**：1 个源（去重键 `(gradient, aspect_ratio)` 相同）+ 2 个引用（每个不同的 `ts` 各一个）。这正是两层去重的收益。

**练习 2**：为什么圆锥渐变的引用变换属性名是 `patternTransform` 而不是 `gradientTransform`？
**答案**：因为圆锥渐变用 `<pattern>` 元素实现（而非 `linearGradient`/`radialGradient`），而 SVG 里 `<pattern>` 的变换属性就叫 `patternTransform`。`GradientKind::Conic` 映射到 `("pattern", "patternTransform")`（[src/paint.rs:328](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L328)）。

---

## 5. 综合实践

把本讲的知识串起来，做一个端到端的「源 + 引用」追踪。

写一个 Typst 文档，在一个**非正方形**矩形上放一个 45°、双色（黑→白）、Oklab 空间、开启 anti-alias 的线性渐变：

```typst
#rect(
  width: 200pt,
  height: 100pt,
  fill: gradient.linear(
    black, white,
    angle: 45deg,
    space: oklab,
    anti: true,
  ),
)
```

用 `typst compile --format svg doc.typ out.svg` 导出 SVG（`--format svg` 的具体名称以本地 CLI 为准；若不确定，标注「待本地验证」）。然后在 `out.svg` 里完成：

1. 找到 `<defs>` 里的源 `<linearGradient id="f...">`，核对它的 `x1/y1/x2/y2`：因为 `aspect_ratio = 200/100 = 2`，修正后角度应 ≈26.57°，据此推算端点是否与文件吻合。
2. 数一数它的 `<stop>` 数量：anti-alias 开启时应在黑→白之间插出多个中间 stop。
3. 找到引用元素 `<linearGradient id="r..." gradientTransform="..." href="#f...">`，确认它把源 `f...` 钉在这个 200×100 矩形上。
4. 把 `anti: true` 改成 `false` 重新编译，对比 `<stop>` 数量变化，验证 4.4 的 workaround。

**预期结果**：你能从一份真实 SVG 里指出「源（`f` 命名空间，无 `gradientTransform`，带几何与 stop）」与「引用（`r` 命名空间，带 `gradientTransform` + `href`，空壳）」两半，并解释它们的分工。

## 6. 本讲小结

- `write_gradients` 只写**源**渐变（不带 `gradientTransform`），坐标统一在单位方形 `[0,1]`，固定带 `spreadMethod="pad"` 与 `gradientUnits="userSpaceOnUse"`。
- **线性渐变**把角度翻译成端点：`correct_aspect_ratio` 预修正宽高比 → `quadrant()` 选起点角 → `factor = |sin|+|cos|` 让对角顶点恰好落在 `t=1` → 算出 `x1/y1/x2/y2`；这套参数化与 Typst 参考 `sample_at` 逐参数一致。
- **径向渐变**是双圆模型（外圆 `cx/cy/r` + 焦点圆 `fx/fy/fr`），字段直接来自 `RadialGradient` 结构，不做几何计算，也不需要宽高比修正。
- 颜色停靠点有「兼容读取器」workaround：`anti_alias()` 开启时用 `generate_intermediate_stops_for_rgb_interpolation` 二分插出中间 stop，把 Oklab 插值烘焙成密集的 sRGB `to_hex()`，骗过忽略色彩空间的渲染器。
- `write_gradient_refs` 把带变换的引用写成空壳：`GradientKind` 决定元素名与变换属性名，每个引用只带 `gradientTransform`/`patternTransform` + `id` + `href`（兼容 `xlink:href`），指回唯一源定义。

## 7. 下一步学习建议

- **u5-l4 圆锥渐变 Conic**：SVG 原生不支持 conic，typst-svg 用 360 段扇形 `<pattern>` 近似，每段引用一个独立的双停靠 sub-gradient。本讲的 `write_gradients` Conic 分支与 `continue` 就是它的入口。
- **u5-l5 平铺 Tiling**：与渐变对称的 `<pattern>` 渲染，理解 `push_tiling` 的「渲染两次」与 `patternTransform` 拼接后，可回来对比 `write_gradient_refs` 与 `write_tiling_refs` 的同构性。
- **u6-l3 去重机制 Deduplicator**：若想搞清源/引用 ID 的 `hash128` 与十六进制编码底层，继续深入 `Deduplicator` 与 `DedupId`。

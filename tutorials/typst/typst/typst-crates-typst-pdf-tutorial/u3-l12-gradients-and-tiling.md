# 渐变与平铺图案

## 1. 本讲目标

上一篇（u3-l11）我们讲完了「纯色」如何从 Typst 翻译到 krilla。本讲接着把 `Paint` 的另外两种复杂形态——**渐变（Gradient）**和**平铺图案（Tiling / Pattern）**——讲透。

学完后你应当能够：

- 说清 `convert_gradient()` 如何把 Typst 的线性 / 径向 / 锥形渐变翻译成 krilla 的几何参数与变换矩阵，以及三者为何采用不同的坐标约定。
- 解释 `RelativeTo::Self_` 与 `RelativeTo::Parent` 的本质区别，以及 `correct_transform()` 为何在 `Parent` 模式下要「先求逆、再叠加容器变换」。
- 理解 `convert_gradient_stops()` 为什么对色相空间（HSL/HSV/OkLch）和 Oklab 要额外插入大量中间色站。
- 描述 `convert_pattern()` 如何借助 `stream_builder` + 一个全新的 `FrameContext` + `handle_frame()`，把一个 Tiling 的内部 Frame 递归渲染成 krilla 的 `Pattern` 内容流。

本讲承接 u3-l11（纯色与色彩空间）与 u2-l7（`handle_frame` / `handle_group` 的 Frame 遍历器与 `State` / `FrameContext` 状态栈）。

## 2. 前置知识

在进入源码前，先建立两个直觉。

**直觉一：PDF 的「复杂绘制」其实是函数 + 坐标变换。**
在 PDF 里，纯色填充只要给一个颜色值；但渐变和平铺图案不是「一个颜色」，而是一段**绘制程序**——PDF 规范称之为 Type 2/3 Shading（渐变）或 Tiling Pattern（平铺）。这段程序在「自己的归一化坐标系」里定义，再通过一个**变换矩阵**被映射到目标区域。所以「翻译一个渐变」= 给出几何参数 + 给出把归一化坐标映射到页面坐标的变换矩阵。这正是 krilla 的 `LinearGradient` / `RadialGradient` / `SweepGradient` / `Pattern` 都带一个 `transform` 字段的原因。

**直觉二：相对到「自己」还是相对到「父容器」。**
一段渐变可以相对自己的包围盒铺开（`RelativeTo::Self_`），也可以相对它所在父容器的包围盒铺开（`RelativeTo::Parent`）。后者最典型的例子是文字渐变：你希望一整段文字共享同一条从左到右的渐变，而不是每个字各自从浅到深。这两种模式决定了变换矩阵怎么算，是本讲的核心难点。

**两个坐标系的事实（来自 u2-l7，这里复述要点）：**
`State` 记录三样东西——`transform`（当前累计变换）、`container_transform`（最近一个 hard frame 的变换快照）、`container_size`（最近一个 hard frame 的尺寸）。`handle_frame` 在进入 hard frame 时调用 `register_container()` 把当前 `transform` 与 frame 尺寸存档。这套簿记正是为本讲的渐变/图案服务的。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `src/paint.rs` | 渐变与图案的全部翻译逻辑：`convert_paint`（总分派）、`convert_gradient`、`convert_gradient_stops`、`convert_pattern`、`correct_transform`。 |
| `src/convert.rs` | 提供 `State`（变换与容器尺寸簿记）、`FrameContext`（状态栈）、`handle_frame`（递归遍历器，被 `convert_pattern` 复用）。 |
| `src/tags/mod.rs` | `tiling()` 与 `disabled()`：在渲染 Tiling 内容流时关闭标签生成、并把整个平铺标记为 Background artifact。 |

> 提示：`Gradient` / `Tiling` / `RelativeTo` 这些「原料」类型定义在上游 `typst-library`，本讲只在需要时点出其语义，翻译逻辑才是 typst-pdf 的工作。

## 4. 核心概念与源码讲解

### 4.1 复杂绘制总入口：convert_paint 与坐标基准

#### 4.1.1 概念说明

u3-l11 讲过，`convert_fill` / `convert_stroke` 是 `handle_shape`、`handle_text` 调用上色的入口。它们都先委托给 `convert_paint()`——这是 `Paint` 的总分派器，按 `Paint::Solid` / `Gradient` / `Tiling` 三选一。本讲关注后两条分支。

但 `convert_paint()` 还干了一件对所有复杂绘制都很关键的事：**算出「自己的包围盒」**。因为渐变要相对自己铺开时，需要一个尺寸作为基准。

#### 4.1.2 核心流程

`convert_paint` 在分派前，先用 `shape.bbox(...)` 算出形状的包围盒 `(offset, size)`，并对负尺寸矩形做镜像修正（与 u3-l10 的 `convert_geometry` 对负尺寸矩形的处理呼应）；若 `size` 任一维为 0，则兜底成 1pt，避免退化渐变。然后才 `match paint` 分派。

#### 4.1.3 源码精读

[convert_paint（paint.rs:L69-L112）](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/paint.rs#L69-L112) —— 计算包围盒并分派。关键片段：

```rust
let (offset, mut size) = if let Some(s) = shape {
    let bbox = s.bbox(include_stroke_in_bbox);
    // ... 负尺寸矩形镜像修正 ...
} else {
    (Point::zero(), Size::zero())   // 纯文字没有形状 → 退化为 0
};
if size.x.is_zero() { size.x = Abs::pt(1.0); }   // 兜底 1pt
if size.y.is_zero() { size.y = Abs::pt(1.0); }

match paint {
    Paint::Solid(c) => { /* 见 u3-l11 */ }
    Paint::Gradient(g) => Ok(convert_gradient(g, on_text, state, size, offset)),
    Paint::Tiling(p) => convert_pattern(gc, p, on_text, surface, state),
}
```

两点解读：

- `(offset, size)` 就是「Self_ 模式下的坐标基准」。注意当 `shape` 为 `None`（作用于纯文字时）时退化为 `(0, 0)`，随后又被兜底成 `1pt`——所以文字上的渐变若用 `Self_` 会退化成几乎一个点，这也是为什么文字渐变默认走 `Parent`（见 4.2）。
- `on_text` 被原样传给 `convert_gradient` / `convert_pattern`，用于决定 `auto` 相对方式时的回退值。

#### 4.1.4 代码实践

**实践目标**：确认「无形状 → 1pt 兜底」的行为。

1. 打开 [paint.rs:L97-L102](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/paint.rs#L97-L102)。
2. 设想一个 `shape = None` 的调用（纯文字渐变）：`offset`、`size` 分别是什么？经过兜底后 `size` 又变成什么？
3. **预期结果**：`offset = (0,0)`、`size = (0,0)` → 兜底后 `size = (1pt, 1pt)`。这说明 Self_ 模式下文字渐变基准几乎为零，故文字场景必须依赖 Parent 模式。**待本地验证**：可在 `convert_gradient` 入口临时加一行 `eprintln!("size={size:?} offset={offset:?}")` 观察实际取值。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 `convert_paint` 要在分派前就把 `size` 的零值兜底成 1pt，而不是交给 krilla 处理？
  - **答案**：因为 `convert_gradient` 内部会做 `size.aspect_ratio()`、以及用 `size` 做 `Transform::scale`。若 `size` 为 0，会出现除零/0 缩放，得到退化或 NaN 的渐变；1pt 兜底保证了数学上良定义。
- **练习 2**：`include_stroke_in_bbox` 参数（第 6 形参）对渐变包围盒有何影响？
  - **答案**：它传给 `s.bbox(...)`。描边会让形状的外接框变大；对填充渐变通常不把描边计入（`false`），对描边渐变才计入（`true`），让渐变覆盖到描边边缘。

---

### 4.2 正确变换：correct_transform 与 RelativeTo

#### 4.2.1 概念说明

这是全讲最关键、也最绕的一点。先说清 krilla 的一个行为：

> 在 krilla 中，如果一个形状自带变换，那么它的「复杂绘制」（渐变/图案）会**继承该形状的变换**。

也就是说，krilla 会自动把形状自己的 `transform` 叠加到 paint 上。这带来一个微妙问题：

- 若渐变是 `RelativeTo::Self_`（相对自己），这正合适——形状被搬到哪里，渐变跟着走。
- 若渐变是 `RelativeTo::Parent`（相对父容器），这就**错位**了：我们希望渐变钉在父容器的坐标系里，但 krilla 会额外叠加形状自己的变换，把渐变也跟着搬走了。

`correct_transform()` 就是用来「修正」这个错位的。

#### 4.2.2 核心流程

`correct_transform(state, relative)` 返回一个要叠加到 paint 上的额外变换：

- `Self_` → 返回**单位变换**。因为 krilla 已经会叠加形状变换，无需再加。
- `Parent` → 先把「当前累计变换」**求逆**（抵消 krilla 即将叠加的形状变换），再 `pre_concat`「容器变换」（回到父容器坐标系）。

用伪代码表示 `Parent` 分支：

```
correct = invert(state.transform)    // 1. 抵消形状自身变换
        .pre_concat(state.container_transform)  // 2. 重新挂到容器坐标系
```

#### 4.2.3 源码精读

[correct_transform（paint.rs:L411-L426）](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/paint.rs#L411-L426) —— 两个分支，注释本身就是最好的讲解：

```rust
fn correct_transform(state: &State, relative: RelativeTo) -> Transform {
    match relative {
        // krilla 会把形状变换叠加到 paint 上，所以这里不必再补。
        RelativeTo::Self_ => Transform::identity(),
        // 先抵消形状变换，再重新挂到「下一个父容器」用的变换上。
        RelativeTo::Parent => state
            .transform()
            .invert()
            .unwrap()
            .pre_concat(state.container_transform()),
    }
}
```

要读懂这段，必须回到 `State`：[State 定义（convert.rs:L177-L185）](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L177-L185) 的 `transform` / `container_transform` / `container_size` 三字段，以及它们在 [register_container（convert.rs:L197-L200）](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L197-L200) 里的赋值：

```rust
pub(crate) fn register_container(&mut self, size: Size) {
    self.container_transform = self.transform;   // 把「此刻的累计变换」存档
    self.container_size = size;
}
```

而 `register_container` 只在进入 hard frame 时被调用：[handle_frame 中的调用点（convert.rs:L338-L340）](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L338-L340)。

```rust
if frame.kind().is_hard() {
    fc.state_mut().register_container(frame.size());
}
```

于是整条因果链是：

1. `handle_frame` 进入一个 hard frame，把当前累计变换存为 `container_transform`。
2. 之后无论嵌套多少 soft frame / group，`transform` 越叠越多，但 `container_transform` 不变（直到下一个 hard frame）。
3. 某个形状上有一个 `Parent` 渐变。`correct_transform` 用 `invert(transform)` 抹掉「自 hard frame 以来累计的全部位移」，再用 `container_transform` 重新挂回容器原点——渐变就被钉在了父容器坐标系。

> 备注：`RelativeTo` 枚举定义见 [typst-library: gradient.rs:L1229-L1234](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/visualize/gradient.rs#L1229-L1234)；`auto` 相对方式在文字上回退为 `Parent`、在形状上回退为 `Self_`，见 [Gradient::unwrap_relative（gradient.rs:L985-L989）](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/visualize/gradient.rs#L985-L989)。

#### 4.2.4 代码实践

**实践目标**：验证「先求逆再叠加」的几何含义。

1. 假设某形状位于父容器内，自 hard frame 起累计位移 `T = translate(50, 30)`，且 `container_transform = identity`（hard frame 就在页面原点）。
2. 写出 `Parent` 模式下 `correct_transform` 的返回值。
3. **预期结果**：`invert(T) = translate(-50, -30)`，再 `pre_concat(identity)` 仍是 `translate(-50, -30)`。krilla 会先把形状变换 `T` 叠到 paint，再叠这个 `translate(-50,-30)`，两者抵消 → 渐变坐标原点回到页面原点（父容器），正是 `Parent` 的语义。**待本地验证**：可临时在 `correct_transform` 的 `Parent` 分支打印 `state.transform()` 与返回值对照。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 `Self_` 分支直接返回单位变换，而不是返回 `state.transform()`？
  - **答案**：因为 krilla 已经会把形状自身的 `transform` 自动叠加到复杂 paint 上；如果再返回 `state.transform()`，就会叠加两次，渐变会「跑过头」。单位变换 = 不额外补充。
- **练习 2**：如果 `state.transform()` 不可逆（比如含 0 缩放的退化矩阵），代码会怎样？
  - **答案**：`.invert().unwrap()` 会 panic。实践中 Typst 排版产生的变换矩阵都是良定义的（平移/旋转/正缩放），不会出现奇异矩阵，因此这里用 `unwrap` 是工程上可接受的断言。

---

### 4.3 渐变几何：convert_gradient（线性 / 径向 / 锥形）

#### 4.3.1 概念说明

`convert_gradient()` 把 Typst 的 `Gradient`（`Linear` / `Radial` / `Conic` 三变体）翻译成 krilla 的 `LinearGradient` / `RadialGradient` / `SweepGradient`。三者都需要给出：色站 `stops`、终点处理 `spread_method`、以及一个 `transform`。区别在于**几何参数的坐标约定不同**——这是最容易踩坑的地方。

#### 4.3.2 核心流程

函数开头先确定「坐标基准与变换基底」：

1. 按相对方式决定 `(size, offset)`：`Self_` 用传入的形状包围盒；`Parent` 改用 `state.container_size()` 且 `offset` 归零。
2. 算 `base_transform = correct_transform(state, relative)`（见 4.2）。
3. 算色站 `stops = convert_gradient_stops(gradient)`（见 4.4）。

然后按变体分派：

- **线性**：端点用归一化 \([0,1]\) 坐标，变换里再 `scale(size)`。
- **径向**：圆心/焦点的位置与半径都用归一化比例，变换里同样 `scale(size)`。
- **锥形**：圆心直接用「实际点数 = size × 比例」，变换里**不做 scale**，而是 `rotate_at`。

#### 4.3.3 源码精读

先看统一的开头：[convert_gradient 开头（paint.rs:L207-L214）](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/paint.rs#L207-L214)

```rust
let (size, offset) = match gradient.unwrap_relative(on_text) {
    RelativeTo::Self_ => (size, offset),
    RelativeTo::Parent => (state.container_size(), Point::zero()),
};
let angle = gradient.angle().unwrap_or_else(Angle::zero);
let base_transform = correct_transform(state, gradient.unwrap_relative(on_text));
let stops = convert_gradient_stops(gradient);
```

**线性渐变** [（paint.rs:L216-L251）](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/paint.rs#L216-L251)：核心是「把方向角换算成单位正方形内的一对端点」。

```rust
let angle = Gradient::correct_aspect_ratio(angle, size.aspect_ratio());
let (sin, cos) = (angle.sin(), angle.cos());
let factor = cos.abs() + sin.abs();                 // ① 缩放因子
let (x1, y1) = match angle.quadrant() {             // ② 起点按象限选角
    Quadrant::First => (0.0, 0.0),
    Quadrant::Second => (1.0, 0.0),
    Quadrant::Third => (1.0, 1.0),
    Quadrant::Fourth => (0.0, 1.0),
};
let x2 = x1 + (cos * factor) as f32;
let y2 = y1 + (sin * factor) as f32;
```

数学上，`factor` 是把单位方向向量 \((\cos\theta,\sin\theta)\) 放大到正好触及包围盒边缘的系数：

\[\text{factor} = |\cos\theta| + |\sin\theta|\]

这样端点 \((x_2,y_2)\) 落在单位正方形的边界上，与 CSS `linear-gradient` 的「渐变线触边」语义一致。注意 `correct_aspect_ratio` 先按宽高比修正了角度（见 [gradient.rs:L994-L996](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/visualize/gradient.rs#L994-L996)），避免非正方形盒子里角度失真。端点都在 \([0,1]\) 归一化空间，所以 `transform` 里再叠 `translate(offset)` 与 `scale(size)` 把它映射到真实尺寸：

```rust
transform: base_transform
    .pre_concat(Transform::translate(offset.x, offset.y))
    .pre_concat(Transform::scale(
        Ratio::new(size.x.to_f32() as f64),
        Ratio::new(size.y.to_f32() as f64),
    ))
    .to_krilla(),
```

**径向渐变** [（paint.rs:L252-L273）](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/paint.rs#L252-L273)：几何参数（外圆圆心 `(cx,cy)` 与半径 `cr`、焦点 `(fx,fy)` 与半径 `fr`）全部是 Typst 里的 `Ratio`，直接 `as f32` 取归一化值；变换与线性一致（`translate(offset)` + `scale(size)`）。

**锥形渐变** [（paint.rs:L274-L300）](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/paint.rs#L274-L300)：注意它与线性/径向的**关键差异**——圆心用实际点数，变换里没有 `scale`，而是 `rotate_at`：

```rust
let cx = size.x.to_f32() * conic.center.x.get() as f32;   // 实际点数，非归一化
let cy = size.y.to_f32() * conic.center.y.get() as f32;
let actual_transform = base_transform
    .pre_concat(Transform::rotate_at(                      // 旋转对齐角度约定
        angle + Angle::rad(PI),
        Abs::pt(cx as f64),
        Abs::pt(cy as f64),
    ))
    .pre_concat(Transform::translate(offset.x, offset.y));
let sweep = SweepGradient {
    cx, cy,
    start_angle: 0.0,
    end_angle: 360.0,
    transform: actual_transform.to_krilla(),
    // ...
};
```

为何锥形不同？因为 krilla 的 `SweepGradient` 期望 `cx/cy` 是**绝对坐标**（它内部不再用变换做缩放），所以这里提前把比例乘上 `size`；而角度对齐靠 `rotate_at(angle + π, 绕圆心)`——Typst 的锥形角度约定（0rad 朝向、整体被旋转）与 krilla 扫描约定（`start_angle=0` 为正 x 轴方向，逆时针扫到 360°）不同，多出的 π 与这次旋转共同把两者对齐。三个分支都用 `SpreadMethod::Pad`（端点颜色向外延伸）。

#### 4.3.4 代码实践

**实践目标**：体会三种渐变的坐标约定差异。

1. 在 [convert_gradient（paint.rs:L200-L301）](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/paint.rs#L200-L301) 的三个分支里，分别找出「尺寸 `size` 出现在哪里」。
2. **预期结果**：线性/径向——`size` 只出现在 `transform` 的 `scale(...)` 里，几何参数本身是归一化的；锥形——`size` 直接乘进了 `cx/cy`，`transform` 里没有 `scale`，只有 `rotate_at`。**待本地验证**：可对 `size=(100pt,100pt)` 与 `size=(200pt,100pt)` 手算线性渐变的端点 `x2/y2`（归一化部分不变，只是缩放不同），与锥形的 `cx/cy`（会随 size 变化）对比。

#### 4.3.5 小练习与答案

- **练习 1**：一个 0° 的线性渐变（水平向右），它的 `(x1,y1)` 与 `(x2,y2)` 分别是多少？
  - **答案**：0° 在第一象限（cos=1, sin=0），`factor = 1`，起点 `(0,0)`，终点 `(0+1·1, 0+0·1) = (1, 0)`，即从左到右贯穿。
- **练习 2**：为什么锥形渐变的 `transform` 里不需要 `scale(size)`？
  - **答案**：因为锥形的 `cx/cy` 已经在构造时乘上了 `size`，是绝对坐标；若再 `scale(size)` 就等于缩放了两次。

---

### 4.4 色站插值：convert_gradient_stops

#### 4.4.1 概念说明

PDF 的渐变着色（Shading）在**设备色彩空间**（通常是 sRGB）里做线性插值。但 Typst 允许用户在 HSL、HSV、OkLch、Oklab 等感知/色相空间里定义色站。若直接把首尾两色丢给 PDF，由 PDF 在 sRGB 里线性插值，会出现两个问题：

- 色相空间里「红 → 绿」会经过浑浊的棕色，而不是鲜艳的黄；
- Oklab 等感知空间下的过渡也会偏离用户预期。

解决思路：**预烘焙**。在 Typst 自己的色彩空间里把这段过渡采样成很多个中间色站，再交给 PDF。PDF 只需在相邻（已经很接近的）色站间做 sRGB 线性插值，视觉上就近似正确了。

#### 4.4.2 核心流程

`convert_gradient_stops` 对线性/径向与锥形采取两套策略：

- **线性/径向**：用 `.windows(2)` 遍历相邻色站对。仅当「两站 offset 不重合 **且** 色彩空间是色相型（`hue_index().is_some()`）或 Oklab」时，才调 `gradient.generate_intermediate_stops_for_rgb_interpolation(first, second)` 插入中间色站。普通 sRGB 等空间不插值（PDF 原生插值已足够）。
- **锥形**：因为绕一整圈 360°，必须更密地采样。按色彩空间决定最大步长 `max_dt`：同色 90°（`0.25`）、色相空间约 1.8°（`0.005`，一圈至少 ~200 站）、其它 18°（`0.05`，一圈至少 ~20 站），然后在每段里用 `Color::mix_iter` 在锥形自身的色彩空间里逐步混色、逐站输出；锐变（`t0 == t1`）特殊处理为硬边界。

每个色站都经 `convert_solid`（u3-l11）拆出颜色与不透明度，封装成 krilla `Stop { offset, color, opacity }`。

#### 4.4.3 源码精读

线性/径向分支 [（paint.rs:L316-L347）](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/paint.rs#L316-L347) 的核心判断：

```rust
// 仅当 gap 非零、且是色相空间或 Oklab 时，才插入中间色站
if second.offset.unwrap() > first.offset.unwrap()
    && (gradient.space().hue_index().is_some()
        || gradient.space() == ColorSpace::Process(ProcessColorSpace::Oklab)
        || gradient.space() == ColorSpace::Process(ProcessColorSpace::Oklab)) // 注：源码此处重复
{
    gradient
        .generate_intermediate_stops_for_rgb_interpolation(first, second)
        .for_each(|(color, at)| add_single(&color, at));
}
```

> 阅读提示：上面源码里 `== Oklab` 的条件出现了两次（第 334、336 行），是源码里一处冗余书写（逻辑上等价于只写一次），阅读时知道其意图是「Oklab 也走插值」即可。

锥形分支 [（paint.rs:L348-L398）](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/paint.rs#L348-L398) 的步长选择：

```rust
let max_dt = if c0 == c1 {
    0.25            // 同色：每 90° 一站即可
} else if conic.space.hue_index().is_some() {
    0.005           // 色相空间：一圈至少 ~200 站，保证色相过渡平滑
} else {
    0.05            // 其它：一圈至少 ~20 站
};
```

色相型空间的判定见 [ProcessColorSpace::hue_index（color.rs:L2579-L2585）](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/visualize/color.rs#L2579-L2585)：`Hsl`/`Hsv` 返回 `Some(0)`，`Oklch` 返回 `Some(2)`，其余 `None`。

#### 4.4.4 代码实践

**实践目标**：观察「插值与否」对色站数量的影响。

1. 构想两个线性渐变，都是从红到绿：一个 `space: srgb`，一个 `space: oklab`，各 2 个用户色站。
2. 追踪 `convert_gradient_stops`，估算最终输出多少个 `Stop`。
3. **预期结果**：srgb 空间不插值 → 仅首/尾各加一次，约 2 个色站；oklab 空间会调 `generate_intermediate_stops_for_rgb_interpolation` 插入若干中间色站 → 明显更多。**待本地验证**：在 `add_single` 闭包里加 `stops.len()` 计数打印，对比两种空间的输出长度。

#### 4.4.5 小练习与答案

- **练习 1**：为什么线性渐变在 sRGB 空间下不需要插入中间色站？
  - **答案**：因为 PDF 设备色彩空间本就是 sRGB，原生线性插值与 Typst 的插值一致，预烘焙没有收益、反而增大文件。
- **练习 2**：锥形渐变里「同色 `c0 == c1`」为什么要单独给 `max_dt = 0.25`？
  - **答案**：两端同色时没有过渡可言，只需稀疏站位（每 90°）让 PDF 知道「这一段就是纯色」即可，无需密采样。

---

### 4.5 平铺图案：convert_pattern 与 stream_builder 递归复用

#### 4.5.1 概念说明

Tiling（平铺图案）= 一块可重复的「瓷砖」+ 平铺规则。在 Typst 里，这块瓷砖本身是一棵 **Frame**（`pattern.frame()`），里面可以是任意内容：图形、文字、甚至另一个渐变。要导出成 PDF 的 Tiling Pattern，需要把这块瓷砖**录制成一个可重放的内容流（content stream）**。

巧妙之处在于：typst-pdf **不另写一套瓷砖渲染器**，而是复用已有的 Frame 遍历器 `handle_frame()`——在一条全新的 krilla `Surface`（由 `stream_builder` 产生）上重新跑一遍遍历。

#### 4.5.2 核心流程

`convert_pattern()` 的步骤：

1. 算平铺变换：`correct_transform(state, relative)` 再 `pre_concat(pattern.transform())`（后者是 Tiling 的 offset+angle，见 [tiling.rs:L374-L377](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/visualize/tiling.rs#L374-L377)）。
2. 用 `surface.stream_builder()` 创建一条**子流**，取出它的 `Surface`。
3. 在 `tags::tiling(...)` 包裹下，建一个全新 `FrameContext::new(None, 瓷砖尺寸)`，调用 `handle_frame` 把瓷砖的内部 Frame 绘制进这条子流。
4. `surface.finish()` + `stream_builder.finish()` 收尾得到 `stream`。
5. 组装 krilla `Pattern { stream, transform, width, height }`，瓷砖大小 = `size + spacing`。

#### 4.5.3 源码精读

[convert_pattern（paint.rs:L165-L198）](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/paint.rs#L165-L198)：

```rust
let transform = correct_transform(state, pattern.unwrap_relative(on_text))
    .pre_concat(pattern.transform());

let mut stream_builder = surface.stream_builder();        // ① 开一条子流
let mut surface = stream_builder.surface();
tags::tiling(gc, &mut surface, pattern.size(), |gc, surface| {
    let mut fc = FrameContext::new(None, pattern.frame().size());  // ② 全新 FrameContext
    handle_frame(                                       // ③ 复用遍历器绘制瓷砖
        &mut fc,
        pattern.frame(),
        Sides::splat(Abs::zero()),
        None,
        surface,
        gc,
    )
})?;
surface.finish();
let stream = stream_builder.finish();                    // ④ 收尾成内容流
let pattern = Pattern {
    stream,
    transform: transform.to_krilla(),
    width: (pattern.size().x + pattern.spacing().x).to_pt() as f32,   // 瓷砖含间距
    height: (pattern.size().y + pattern.spacing().y).to_pt() as f32,
};
Ok((pattern.into(), 255))
```

四个要点：

- **全新 FrameContext**：`FrameContext::new(None, ...)` 见 [convert.rs:L230-L236](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L230-L236)。第一个参数 `page_idx = None`——瓷砖不属于任何具体页（它是可复用的资源）；第二个参数是瓷砖 Frame 的尺寸，作为根 `State` 的 `container_size`，让瓷砖内部若再有 `Parent` 渐变也有正确的容器基准。
- **递归复用**：`handle_frame` 就是 u2-l7 讲的那个分派器，它会照常分派 Text/Shape/Image/Link/Tag。于是「瓷砖里画一段文字」「瓷砖里再嵌一个渐变」都能正确工作——后者会回调本文件的 `convert_gradient`，形成 `convert_pattern → handle_frame → convert_gradient` 的间接递归。
- **tags::tiling 的作用**：见 [tags::tiling（tags/mod.rs:L100-L142）](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/tags/mod.rs#L100-L142)。它做两件事：把 `gc.tags.in_tiling` 置为 `true`（于是 [disabled()（tags/mod.rs:L147-L149）](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/tags/mod.rs#L147-L149) 在瓷砖内部返回 `true`，**瓷砖内部不再发射任何结构标签**——重复铺满的装饰内容不该进入无障碍结构树）；并在合适时把整块平铺标记为 `Background` artifact（PDF 1.7 下若 bbox 为空则降级为 `Other`）。
- **瓷砖尺寸含 spacing**：`width = size.x + spacing.x`。`size` 是瓷砖图案本身的大小，`spacing` 是相邻瓷砖间的留白，两者相加才是平铺单元的步长。

#### 4.5.4 代码实践

**实践目标**：追踪「Tiling 内部 Frame → krilla Pattern stream」的录制过程。

1. 阅读 [convert_pattern（paint.rs:L165-L198）](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/paint.rs#L165-L198)，回答：瓷砖的内容流是在哪个 `Surface` 上录制的？它与「页面主 Surface」是什么关系？
2. 设想一个 Tiling，其 `frame()` 内部含一个 `Parent` 相对的渐变。追踪这个渐变会以哪个 `State.container_size` 为基准。
3. **预期结果**：内容流录制在 `stream_builder.surface()` 返回的**子 Surface** 上，与页面主 Surface 相互独立；子流录制完毕后才作为 `Pattern.stream` 挂回主绘制。内部渐变以「新建 FrameContext 时传入的 `pattern.frame().size()`」为容器基准（因为 `handle_frame` 进入瓷砖的 hard frame 时会 `register_container` 把它存档）。**待本地验证**。

#### 4.5.5 小练习与答案

- **练习 1**：为什么 `convert_pattern` 给 `FrameContext::new` 传 `page_idx = None`？
  - **答案**：瓷砖是一份可被多页反复引用的资源，不归属任何具体页码；`None` 也让 `FrameContext::page_size()` 返回 `None`，避免把瓷砖误当成真实页面。
- **练习 2**：为什么要在瓷砖内部用 `tags::tiling` 把 `in_tiling` 置真？
  - **答案**：瓷砖会被平铺重复无数次，若对其内部内容逐一发射结构标签，既毫无无障碍意义，又会产生海量重复标签污染结构树；`in_tiling` 让 `disabled()` 在瓷砖内部返回 `true`，统一跳过标签生成。

---

## 5. 综合实践

把本讲的两条主线串起来：**`correct_transform` 的「先逆再加」** 与 **`convert_pattern` 的递归复用**。

### 任务一：解释 `correct_transform` 的 Parent 分支

阅读 [correct_transform（paint.rs:L411-L426）](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/paint.rs#L411-L426)，回答：

1. krilla 的什么行为使得「复杂 paint 会自动继承形状变换」？（提示：看函数顶端的注释。）
2. 在 `RelativeTo::Parent` 时，为什么要先 `invert(state.transform())`？
3. 紧接着为什么又要 `pre_concat(state.container_transform())`？

**参考答案**：krilla 会把形状自带的 `transform` 自动叠加到复杂 paint 上。`Parent` 语义要求渐变钉在父容器坐标系，但 krilla 即将叠加的形状变换会把它带偏；所以先 `invert(transform)` 抵消这部分叠加，再用 `container_transform` 把坐标原点重新挂回「最近一个 hard frame 的入口」（即父容器基准）。两者合起来 = 「撤销自容器以来的全部位移，回到容器原点」。

### 任务二：解释 `convert_pattern` 的递归渲染

阅读 [convert_pattern（paint.rs:L165-L198）](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/paint.rs#L165-L198) 与 [handle_frame（convert.rs:L327-L391）](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L327-L391)，回答：

1. `convert_pattern` 如何借助 `FrameContext` + `handle_frame` 把 Tiling 的内部 frame 渲染成 krilla Pattern stream？
2. 为什么这一步不需要新写一套瓷砖绘制代码？

**参考答案**：它先从主 Surface 用 `stream_builder()` 派生出一条子 Surface（一段可重放的内容流），然后 `tags::tiling` 包裹下新建一个 `FrameContext`（`page_idx=None`，根尺寸 = 瓷砖 frame 尺寸），直接调 `handle_frame` 把 `pattern.frame()` 当作普通 Frame 走一遍完整遍历——文字、图形、图像、嵌套渐变都会被原样录制进子流。最后 `finish()` 得到 `stream`，连同 `transform`、`width`、`height` 组装成 krilla `Pattern`。因为 `handle_frame` 本就是通用的「Frame → Surface 绘制」翻译器，瓷砖不过是一棵普通 Frame，自然可以复用，无需专码。

### 任务三（可选，源码阅读型）：画一张调用链

用伪代码画出「页面上一段带渐变填充的文字 + 一个平铺背景」的导出调用链，标出 `handle_frame`、`convert_paint`、`convert_gradient`、`convert_pattern`、`correct_transform`、`tags::tiling` 出现的位置与嵌套关系。

## 6. 本讲小结

- 复杂绘制（渐变/图案）= **几何参数 + 把归一化坐标映射到页面坐标的变换矩阵**；`convert_paint` 先算出形状包围盒作为 `Self_` 基准，零值兜底成 1pt。
- `correct_transform` 利用 krilla「paint 继承形状变换」的特性：`Self_` 返回单位变换；`Parent` 先 `invert(transform)` 抵消形状变换，再 `pre_concat(container_transform)` 回到父容器坐标系。
- 三种渐变坐标约定不同：线性/径向用归一化几何 + `scale(size)` 变换；锥形把圆心直接乘成绝对点数、变换里用 `rotate_at(angle+π)` 对齐角度约定、不做 scale。
- `convert_gradient_stops` 对色相空间与 Oklab **预烘焙**中间色站，绕过 PDF 在 sRGB 里直接插值的失真；锥形按空间密采样（同色 90°、色相 ~1.8°、其它 18°）。
- `convert_pattern` 用 `stream_builder` 派生子 Surface，新建 `FrameContext` 复用 `handle_frame` 把瓷砖内部 Frame 录制成 Pattern 内容流；`tags::tiling` 在瓷砖内部关闭标签生成并标记为 Background artifact。

## 7. 下一步学习建议

- 本讲之后，`Paint` 的三种形态（纯色/渐变/平铺）已全部讲完。建议回头对比 `convert_fill` / `convert_stroke` 如何统一处理这三者的「不透明度分离」（u3-l11），形成完整上色心智模型。
- 接下来进入 **u3-l13 图像**：栅格图、SVG、嵌入 PDF 的处理，其中 SVG/嵌入 PDF 同样涉及「在子 Surface 上录制内容」的思路，可与本讲 `convert_pattern` 的 `stream_builder` 互相对照。
- 若对 tagged PDF 感兴趣，可留意本讲提到的 `tags::tiling` / `disabled()`——它们是 u5 单元「tagged PDF 子系统」的边缘入口，届时会看到 `in_tiling` 这个标志在整个标签状态机里的位置。

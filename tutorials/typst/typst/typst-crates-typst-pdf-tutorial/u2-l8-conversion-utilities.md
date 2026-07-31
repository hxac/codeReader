# 类型转换工具集与路径转换

## 1. 本讲目标

本讲精读 `src/util.rs`——它是 `typst-pdf` 的「类型翻译官」。读完本讲你应该能够：

1. 说清楚为什么 `typst-pdf` 需要这样一层工具集，以及它统一采用的「扩展 trait」设计模式。
2. 掌握把 Typst 几何类型（`Abs` / `Point` / `Size` / `Transform` / `Sides`）转换成 krilla 几何类型的几组 trait。
3. 掌握把 Typst 绘制枚举（`LineCap` / `LineJoin` / `FillRule`）映射到 krilla 枚举的对应关系。
4. 理解 `convert_path()` 如何遍历 `Curve`（`CurveItem` 的 `Move` / `Line` / `Cubic` / `Close` 四种段）填充 krilla 的 `PathBuilder`，以及为什么必须分别处理这四种段。
5. 认识 `ValidatorsExt`（错误信息中的列表格式化）、`display_font()`（错误信息中的字体名）、专色名转换等辅助工具。

本讲是承接 [u2-l5](u2-l5-convert-orchestrator.md) 的横向工具层：编排核心 `convert()` 与各内容翻译器（`text` / `shape` / `paint` / `image` 等）都不直接处理「Typst 类型 ↔ krilla 类型」的差异，而是统一委托给 `util.rs`。

## 2. 前置知识

- **Typst 的绝对长度 `Abs`**：一个绝对物理长度（pt / mm / cm / inch 等），内部按原始单位存储，通过 `to_pt()` 换算成「点（point）」。1 point = 1/72 英寸，这正是 PDF 用户空间坐标的默认单位。
- **PDF 用户空间坐标**：PDF 把每个页面看作一个浮点坐标系，单位是 point，原点（0,0）在**左下角**，y 轴**向上**。krilla 这个库在内部用 `f32` 来表示坐标。
- **仿射变换矩阵 `Transform`**：二维仿射变换可以用 6 个数描述：缩放/旋转/错切分量 + 平移分量。Typst 把它存成 `(sx, ky, kx, sy, tx, ty)` 六个字段。
- **路径（path）与子路径**：一条矢量路径由若干「子路径」组成，每个子路径由一系列指令构成——抬笔移动到某点（不画线）、画直线、画三次贝塞尔曲线、闭合（连回子路径起点）。PDF 内容流里对应四个算符 `m` / `l` / `c` / `h`。
- **三次贝塞尔曲线**：由 3 个点（2 个控制点 + 1 个终点）定义的曲线，是 PDF / PostScript 里唯一支持的曲线类型。
- **扩展 trait（extension trait）模式**：当你无法修改某个外部类型的定义、又想给它加方法时，Rust 的惯例是定义一个自己的 trait，再为外部类型 `impl` 它。这是 `util.rs` 通篇使用的手法。

> 如果你对 krilla 还不熟悉，只需记住一句话：krilla 是真正「拼装 PDF 字节」的底层库，`util.rs` 的职责就是把 Typst 的值「喂」成 krilla 认识的形状。krilla 的几何类型集中在 `krilla::geom`（别名 `kg`），绘制类型集中在 `krilla::paint`（别名 `kp`），标签类型集中在 `krilla::tagging`（别名 `kt`）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/util.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/util.rs) | **本讲主角**。定义全部转换 trait 与 `convert_path`、`display_font` 两个函数。 |
| [src/convert.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs) | 导入并使用这些工具：`convert_path`（裁剪路径）、`AbsExt`（页面尺寸/出血框）、`ValidatorsExt`/`display_font`（错误诊断）。 |
| [src/shape.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/shape.rs) | 用 `convert_path` 把 `Geometry::Curve` 转成路径，用 `TransformExt`、`AbsExt`。 |
| [src/paint.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/paint.rs) | 用 `LineCapExt` / `LineJoinExt` / `FillRuleExt` / `SpotColorantToNameExt` / `TransformExt` 转换描边与填充。 |
| [src/text.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/text.rs) | 用 `TransformExt`、`AbsExt`、`display_font`。 |

本讲只精读 `src/util.rs`（共 225 行），其余文件仅作为「调用方」佐证。

## 4. 核心概念与源码讲解

### 4.1 设计动机：为什么需要这层工具集

#### 4.1.1 概念说明

`typst-pdf` 是一个适配器层（见 [u1-l1](u1-l1-project-overview.md)）：它的输入是 Typst 的类型（`Abs`、`Point`、`Transform`、`Curve`、`Color`……），它的输出是对 krilla 的调用，而 krilla 又有自己的一套同名但不兼容的类型（`krilla::geom::Size`、`Point`、`Transform`……）。

这两套类型**不能直接互换**，原因至少有三：

1. **数值类型不同**：Typst 的 `Abs` 是带物理单位的长度，krilla 的几何量是裸 `f32`。
2. **枚举成员与命名不同**：Typst 的 `LineJoin::Miter` 对应 krilla 的 `kp::LineJoin::Miter`，名字虽像但分属不同类型，编译器会拒绝混用。
3. **语义细节不同**：例如 `Sides<T>` 在 Typst 里是「上右下左」，而 krilla 标签侧的 `kt::Sides` 是「before/after/start/end」（与书写方向有关），需要显式映射。

如果每个内容翻译器（`text` / `shape` / `paint` / `image`）都各自写一遍这些转换，就会到处重复且容易写错。因此 `util.rs` 把它们集中成一组**扩展 trait**，约定一个统一的方法名 `to_krilla()`，让调用方一行代码完成转换。

#### 4.1.2 核心流程

整个文件的组织遵循一个固定模板，可以套用到几乎所有转换 trait：

```
对每一对「Typst 类型 → krilla 类型」：
  1. 定义一个 pub(crate) trait XxxExt { fn to_krilla(...) -> KgType; }
  2. 为 Typst 类型 impl XxxExt
  3. 在 to_krilla() 内部做必要的换算 / match 分派
  4. 调用方：use crate::util::XxxExt;  value.to_krilla()
```

这样设计的好处：

- `pub(crate)` 让 trait 只在 crate 内可见，不污染公共 API。
- 统一叫 `to_krilla()`，调用点读起来像「把这个 Typst 值变成 krilla 值」，意图清晰。
- 把「换算逻辑」与「使用逻辑」解耦：翻译器只管业务，换算细节集中在 `util.rs`。

#### 4.1.3 源码精读

文件顶部的 `use` 语句直接揭示了这套别名体系，是理解全篇的钥匙：

[src/util.rs:1-17](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/util.rs#L1-L17) —— 文件头：注释点明「把 Typst 类型转成 krilla 类型的基础工具」，并用 `as` 给 krilla 的几个子模块起短别名（`ks` = color::separation、`kg` = geom、`kp` = paint、`kt` = tagging），下文一律用这些短名引用 krilla 类型。

而最基础的一个转换，是 `AbsExt`：

[src/util.rs:113-121](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/util.rs#L113-L121) —— `AbsExt::to_f32()` 把 Typst 绝对长度转成 PDF 点数（`f32`）：内部调用 `self.to_pt()`（返回 `f64`），再 `as f32` 收窄为单精度。这一行是几乎所有几何转换的「地基」——后面 `Point`、`Size`、`Transform`、`convert_path` 里的每一个坐标，最终都要经过 `to_f32()`。

> 注意 `to_f32()` 接收 `self`（按值），而其它 trait 多是 `&self`。这是因为 `Abs` 是轻量 `Copy` 类型，按值传递更直接。

#### 4.1.4 代码实践

实践类型：源码阅读。

1. 打开 [src/util.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/util.rs)。
2. 数一数：文件里一共定义了多少个 `pub(crate) trait`？其中有几个方法名恰好叫 `to_krilla`？
3. 观察：这些 trait 的方法签名返回的 krilla 类型分别落在 `kg::` / `kp::` / `ks::` / `kt::` 哪个模块下。

**预期结果**：你会看到几乎每个 trait 的方法都叫 `to_krilla`，返回类型分别属于几何（`kg`）、绘制（`kp`）、专色（`ks`）、标签（`kt`）模块——这正是「按 krilla 子模块归类」的体现。`AbsExt::to_f32` 是少数不叫 `to_krilla` 的例外，因为它返回的是裸 `f32`，不属于任何 krilla 类型。

#### 4.1.5 小练习与答案

**练习**：为什么不把这些转换直接做成 krilla 或 typst-library 里的 `From`/`Into` 实现，而要单独写扩展 trait？

**参考答案**：① krilla 和 typst-library 是两个独立 crate，`typst-pdf` 不能在它们里加 `impl From`（孤儿规则禁止为外部类型实现外部 trait）；② 即便能写，把 PDF 专用的换算（如 pt→f32、`Sides` 的书写方向重映射）塞进通用库里，会让通用库承担不该有的 PDF 知识，破坏关注点分离；③ `pub(crate)` 的扩展 trait 把这些细节完全隐藏在 `typst-pdf` 内部，不泄露到公共 API。

---

### 4.2 几何类型转换：Point、Size、Transform、Sides

#### 4.2.1 概念说明

这一组 trait 处理「位置」「尺寸」「变换」三类几何量。它们的共同点是：**数值成分都要先经过 `AbsExt::to_f32()` 变成点数**，再喂给 krilla 的对应构造函数。

- `Point`：二维点，有 `x`、`y` 两个 `Abs` 分量。
- `Size`：尺寸，有 `x`（宽）、`y`（高）两个 `Abs` 分量；可能为「负或零」，因此 krilla 的 `from_wh` 会校验合法性并返回 `Option`。
- `Transform`：仿射变换，前四个分量（`sx/ky/kx/sy`）是无量纲的 `Ratio`，后两个（`tx/ty`）是 `Abs` 平移。
- `Sides<T>`：四向边距（上右下左），需重映射成 krilla 标签侧的「逻辑方向」。

#### 4.2.2 核心流程

```
PointExt:   Point{x, y}        --x.to_f32(), y.to_f32()-->  kg::Point::from_xy(x, y)
SizeExt:    Size{x, y}         --x.to_f32(), y.to_f32()-->  kg::Size::from_wh(x, y) -> Option
TransformExt:
  Transform{sx,ky,kx,sy,tx,ty}
    --sx.get(), ky.get(), kx.get(), sy.get()   (Ratio -> f64 -> as f32)
    --tx.to_f32(), ty.to_f32()                 (Abs -> pt)
    --> kg::Transform::from_row(sx, ky, kx, sy, tx, ty)
SidesExt:   Sides{top,right,bottom,left}
    --> kt::Sides{before:top, after:bottom, start:left, end:right}
```

关键细节：

- `Transform` 的四个 `Ratio` 用 `.get()` 取出底层 `f64`，再隐式 `as f32`；而 `tx/ty` 是 `Abs`，必须用 `to_f32()` 换算成点。
- `from_row` 的实参顺序是 `(sx, ky, kx, sy, tx, ty)`，与 Typst `Transform` 结构体字段定义顺序一致（参见下方源码），因此这是一一对应，不是转置。
- `SizeExt` 返回 `Option`：当宽或高非法时 `from_wh` 返回 `None`，由调用方决定如何处理。

#### 4.2.3 源码精读

先看 Typst 侧的 `Transform` 定义，确认字段顺序：

[crates/typst-library/src/layout/transform.rs:244-251](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/transform.rs#L244-L251) —— Typst `Transform` 的六个字段正是 `sx, ky, kx, sy`（`Ratio`）加 `tx, ty`（`Abs`）。

再看转换实现：

[src/util.rs:83-98](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/util.rs#L83-L98) —— `TransformExt`：把四个 `Ratio` 通过 `.get()` 取出、两个 `Abs` 通过 `.to_f32()` 换算，按 krilla `from_row` 期望的顺序（`sx, ky, kx, sy, tx, ty`）逐一传入。注意前四个是缩放/错切（无量纲），后两个是平移（带 pt 单位），所以取值方式不同。

[src/util.rs:35-53](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/util.rs#L35-L53) —— `SizeExt` 与 `PointExt`：分别用 `from_wh` / `from_xy` 构造，每个分量都走 `to_f32()`。`SizeExt` 返回 `Option<kg::Size>`，因为 krilla 的 `from_wh` 会拒绝非正尺寸。

[src/util.rs:19-33](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/util.rs#L19-L33) —— `SidesExt`：把 Typst 的「物理方向」重映射成 krilla 标签侧的「逻辑方向」。注释明确说明这是**假定书写模式为 `LrTb`**（从左到右、从上到下，即常见的拉丁/中文横排）时成立——此时 `top→before`、`bottom→after`、`left→start`、`right→end`。这个 trait 只在标签（tagging）相关场景使用，所以落在 `kt::Sides`。

实际调用方示例（`text.rs` / `shape.rs` 把当前变换矩阵压栈时）：

[src/text.rs:46](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/text.rs#L46) —— `surface.push_transform(&fc.state().transform().to_krilla());`，一行就把 Typst 的累计变换交给 krilla 的图形状态栈。这与 [u2-l7](u2-l7-frame-walker.md) 讲的「变换状态栈」直接对接。

#### 4.2.4 代码实践

实践类型：源码阅读 + 推理。

1. 在 [src/shape.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/shape.rs) 中搜索 `to_f32`，观察矩形宽高 `w = size.x.to_f32()`、`h = size.y.to_f32()` 是如何被使用的（构造 `Rect::from_xywh`）。
2. 思考：假如某个 `Frame` 的尺寸宽或高为 0，`SizeExt::to_krilla()` 会返回什么？搜索 `SizeExt` 的调用点，看调用方是否对 `None` 做了处理。

**预期结果**：`from_wh(0.0, h)` 之类会返回 `None`，调用方需要据此跳过该元素的绘制。`待本地验证`：你可以在 `convert.rs` 里搜索 `SizeExt` / `to_krilla` 的调用，确认是否都配合了 `if let Some(...)` 或 `?`。

#### 4.2.5 小练习与答案

**练习 1**：`TransformExt::to_krilla()` 里，为什么 `sx` 用 `.get()` 而 `tx` 用 `.to_f32()`？

**参考答案**：`sx` 是 `Ratio`（无量纲比例），`.get()` 取出其底层 `f64`；`tx` 是 `Abs`（带物理单位的长度），必须先 `to_pt()` 换算成 PDF 点再收窄为 `f32`。两者物理含义不同，取值方式也就不同。

**练习 2**：`SidesExt::to_lrtb_krilla` 的注释为什么强调「assuming `LrTb`」？

**参考答案**：`kt::Sides` 用的是**逻辑方向**（before/after/start/end），它的物理含义依赖书写模式。只有在 `LrTb`（横排、从左到右从上到下）时，「start = 左、end = 右、before = 上、after = 下」才成立；若是竖排（如 `TbRl`），start/end 的物理方向会不同。注释提醒维护者：这层映射不是普适的。

---

### 4.3 绘制枚举转换：LineCap、LineJoin、FillRule

#### 4.3.1 概念说明

这三组枚举控制**描边与填充的渲染细节**，语义在 PDF 规范里是固定的，Typst 和 krilla 各自定义了同名的枚举，但属于不同类型，必须显式翻译：

- **`LineCap`（线帽）**：线段两端的形状——`Butt`（平齐）、`Round`（圆头）、`Square`（方形延伸）。
- **`LineJoin`（线连接）**：线段拐角处的形状——`Miter`（尖角）、`Round`（圆角）、`Bevel`（斜切）。
- **`FillRule`（填充规则）**：判断一个点是否落在填充区域内的规则——`NonZero`（非零环绕数，默认）、`EvenOdd`（奇偶）。

这三者都是「一一对应」的纯映射，没有任何计算。

#### 4.3.2 核心流程

```
LineCapExt:    LineCap::{Butt,Round,Square}     --> kp::LineCap::{Butt,Round,Square}
LineJoinExt:   LineJoin::{Miter,Round,Bevel}    --> kp::LineJoin::{Miter,Round,Bevel}
FillRuleExt:   FillRule::{NonZero,EvenOdd}      --> kp::FillRule::{NonZero,EvenOdd}
```

每条 `match` 分支都是「同构」的：左边枚举成员名字 == 右边枚举成员名字。这看起来「枯燥」，但它的价值在于**让编译器在两侧枚举发生演化时强迫你逐分支检查**。

#### 4.3.3 源码精读

[src/util.rs:55-67](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/util.rs#L55-L67) —— `LineCapExt`：`Butt/Round/Square` 三分支一一对应。

[src/util.rs:69-81](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/util.rs#L69-L81) —— `LineJoinExt`：`Miter/Round/Bevel` 三分支一一对应。

[src/util.rs:100-111](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/util.rs#L100-L111) —— `FillRuleExt`：`NonZero/EvenOdd` 两分支一一对应。

调用方示例（`paint.rs` 构造描边时）：

[src/paint.rs:60-63](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/paint.rs#L60-L63) —— 描边厚度 `thickness.to_f32()`、连接 `join.to_krilla()`、线帽 `cap.to_krilla()`，分别走 `AbsExt`、`LineJoinExt`、`LineCapExt`。

#### 4.3.4 代码实践

实践类型：源码阅读。

1. 打开 [src/paint.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/paint.rs)，找到 `convert_stroke` 附近的代码，观察 `line_join`、`line_cap`、`width` 三个字段是如何各自调用一个 `util` trait 的。
2. 思考：如果 krilla 未来给 `LineJoin` 增加一个新成员（比如 `Arc`），这套写法会在什么时候报错？

**预期结果**：因为这里用的是穷尽 `match`（没有 `_ =>` 通配），一旦 Typst 或 krilla 任一侧枚举新增成员，编译器会立刻在对应的 `to_krilla()` 实现里报「non-exhaustive match」错误，迫使维护者补上新分支——这正是「一一映射」带来的类型安全收益。

#### 4.3.5 小练习与答案

**练习**：`FillRule::NonZero` 与 `EvenOdd` 在渲染上的实际区别是什么？给一个能看出差异的简单图形。

**参考答案**：两者都用于判断「自相交或嵌套路径」的区域是否被填充。`NonZero` 用环绕数：若一条射线穿过路径的「顺时针」与「逆时针」边界数量不等（环绕数 ≠ 0）则填充；`EvenOdd` 用奇偶：穿过的边界数为奇数则填充。经典的五角星轮廓（一笔画成、内部形成一个五边形孔）：`NonZero` 下整个星（含中心五边形）都被填充；`EvenOdd` 下中心五边形是镂空的。Typst 的 `path()`、`polygon()` 等可用 `fill-rule` 设置。

---

### 4.4 路径转换：convert_path 与 CurveItem

#### 4.4.1 概念说明

`convert_path()` 是本文件里**唯一带实质逻辑**（而非纯一一映射）的转换。它把 Typst 的矢量曲线 `Curve` 转成 krilla 的 `PathBuilder` 指令序列。

Typst 的 `Curve` 定义非常简洁——它就是一个 `CurveItem` 列表：

[crates/typst-library/src/visualize/curve.rs:382-393](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/visualize/curve.rs#L382-L393) —— `Curve(pub Vec<CurveItem>)`，且 `CurveItem` 只有四个变体：`Move(Point)`、`Line(Point)`、`Cubic(Point, Point, Point)`、`Close`。

这恰好与 PDF 路径算符一一对应，因此转换过程就是一次顺序遍历 + 分派。

#### 4.4.2 核心流程

`convert_path` 采用「**追加式填充**」设计：它不创建 `PathBuilder`，而是接收一个现成的 `&mut PathBuilder`，把 `Curve` 的指令逐条追加进去。这样调用方可以往同一个 builder 里拼入多段子路径（例如先画一个矩形，再画一条曲线），最后统一 `finish()`。

```
fn convert_path(path: &Curve, builder: &mut PathBuilder):
    for item in path.0:                  # path.0 就是 Vec<CurveItem>
        match item:
            Move(p)    -> builder.move_to(p.x.to_f32(), p.y.to_f32())   # 抬笔，开始新子路径
            Line(p)    -> builder.line_to(p.x.to_f32(), p.y.to_f32())   # 画直线到 p
            Cubic(c1,c2,c3) -> builder.cubic_to(c1..., c2..., c3...)    # 三次贝塞尔：2 控制点 + 终点
            Close      -> builder.close()                               # 闭合当前子路径
```

为什么必须**分别处理**这四种段？因为它们对应 PDF 路径里语义完全不同的四类算符：

| CurveItem | krilla 调用 | PDF 算符 | 语义 | 需要点数 |
| --- | --- | --- | --- | --- |
| `Move(p)` | `move_to(x,y)` | `m` | 开始一条新子路径，**不画线**（抬笔） | 1 |
| `Line(p)` | `line_to(x,y)` | `l` | 从当前点画直线到 p | 1 |
| `Cubic(c1,c2,c3)` | `cubic_to(...)` | `c` | 三次贝塞尔曲线（c1、c2 为控制点，c3 为终点） | 3 |
| `Close` | `close()` | `h` | 把当前子路径连回其起点并闭合 | 0 |

四个变体携带的点数不同（0/1/1/3），且 `Close` 根本不带点，因此**不可能**用一个统一的「点列表」去表达，只能用 `match` 逐变体分派。此外：

- **`Move` 与 `Line` 都只带一个点**，但绝不能合并：`Move` 是「抬笔换起点」，不产生可见线段；`Line` 才是「画线」。混淆会导致多出多余的线。
- **PDF 只支持三次贝塞尔**：Typst 的 `Curve` 已经把所有二次贝塞尔/圆弧等在排版阶段扁平化为 `Cubic`，所以 `convert_path` 不需要处理 `Quad`，这让转换非常干净。

每个点的坐标都走 `AbsExt::to_f32()`，把 Typst 长度换算成 PDF 点。

#### 4.4.3 源码精读

[src/util.rs:208-225](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/util.rs#L208-L225) —— `convert_path`：遍历 `path.0`，按 `Move`/`Line`/`Cubic`/`Close` 四分支分别调用 `PathBuilder` 的 `move_to`/`line_to`/`cubic_to`/`close`。每个 `Point` 的 `x`、`y` 都通过 `.to_f32()` 转成点数。注意它**借用了** `path` 和 `builder`，本身不分配新对象。

`Cubic` 分支里 `cubic_to` 接收 6 个数（c1.x, c1.y, c2.x, c2.y, c3.x, c3.y），对应三次贝塞尔的「两个控制点 + 终点」（起点是当前的「画笔位置」，由前一条 `Move`/`Line`/`Cubic` 隐式确定）。

两个典型调用方，体现了「追加式」设计的价值：

[src/shape.rs:104](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/shape.rs#L104) —— `Geometry::Curve(c) => convert_path(c, &mut path_builder);`，随后第 108 行 `path_builder.finish()` 收尾。同一函数里 `Geometry::Rect` 会先 `path_builder.push_rect(...)`，可见 builder 可以混用矩形快捷指令与通用曲线。

[src/convert.rs:407-411](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L407-L411) —— 裁剪路径的构造：`PathBuilder::new()` → `convert_path(p, &mut builder)` → `builder.finish()` → `.transform(fc.state().transform.to_krilla())`。这正是 [u2-l7](u2-l7-frame-walker.md) 里 `handle_group` 处理 `clip` 的落点：先在「局部坐标」建好路径，再用当前累计变换整体变换到页面坐标。

#### 4.4.4 代码实践

实践类型：源码阅读 + 推理。

1. 在 [src/util.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/util.rs) 找到 `convert_path`，确认 `Cubic` 分支里 `cubic_to` 收到的点顺序是「c1, c2, c3」即「控制点 1、控制点 2、终点」。
2. 思考验证：若一段 `Curve` 的第一个段是 `Line` 而非 `Move`（即没有先 `move_to` 就 `line_to`），PDF 阅读器通常会如何处理？

**预期结果**：PDF 规范里，若子路径没有显式 `m` 起始，当前点视为 `(0,0)`，因此 `Line` 会从原点画起——这通常不是期望行为。所以正常生成的 `Curve` 都会以 `Move` 开头。`待本地验证`：可阅读 `typst-library` 里 `Curve` 的构造逻辑（如 `curve.rs` 中各 `Curve*` 的 `push` 方法），确认 Typst 是否总是先发 `Move`。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `convert_path` 接收 `&mut PathBuilder` 而不是自己 `new` 一个再返回？

**参考答案**：为了「可拼接」。调用方可能要先 `push_rect` 再接 `convert_path`，或多段曲线拼成一条复合路径，最后统一 `finish()`。若 `convert_path` 自己创建并返回 builder，调用方就无法把多段内容拼到一起。

**练习 2**：`Cubic(Point, Point, Point)` 三个点分别是什么？为什么不是「起点 + 控制点 + 终点」四个点？

**参考答案**：三个点依次是「控制点 1、控制点 2、终点」。起点不需要显式给出——它是画笔的当前位置，由上一条 `Move`/`Line`/`Cubic` 的终点隐式确定。这是 PDF / PostScript 路径模型的统一约定：所有「画到某点」的算符都以「当前点」为基准。

---

### 4.5 诊断辅助：ValidatorsExt、display_font 与专色名转换

#### 4.5.1 概念说明

本节收集三类「服务于错误诊断与少数特殊场景」的工具，它们不像前几节那样遍布各翻译器，但都在关键链路里出现：

- **`ValidatorsExt`**：把 krilla 的 `Validators`（一组校验器，如 PDF/A、PDF/UA）格式化成人类可读的字符串，用于错误信息（如「X error: PDF/A-2b, PDF/UA-1」）。
- **`display_font`**：把一个 `FontInstance` 格式化成 `` font `家族名` `` 的字符串，用于错误信息里指明是哪个字体出问题。
- **`SpotColorantToNameExt` / `SpotColorantFromNameExt`**：在 Typst 专色名（`SpotColorantName`）与 krilla 专色色料（`SeparationColorant`）之间双向转换，包括 `All`（所有色料）、自定义名、以及 `Option` 下的「无色料（`NoColorant`）」。

#### 4.5.2 核心流程

`ValidatorsExt` 有两个格式化方法，差别只在最后两项之间的连接词：

```
to_comma_list:  a, b, c, d            # 纯逗号
to_and_list:    a, b, c, and d        # 末项前用 "and"（牛津逗号）
                a and b               # 两项
                a                     # 一项
                (空)                  # 零项
```

`to_and_list` 用 `SmallVec<[_; 2]>`（栈上最多 2 个元素的小向量）暂存名字，再按「0 / 1 / 2 / ≥3」四种长度分别处理。这反映了一个先验：通常只有 1–2 个校验器报错，多数情况不触发堆分配。

`display_font(font)`：

```
Some(font) -> eco_format!("font `{}`", font.info().family.repr())   # font `Noto Sans`
None       -> "a font"                                              # 找不到具体字体时兜底
```

专色名转换（`SpotColorantName` ↔ krilla `SeparationColorant`）：

```
Typst All       <-> krilla AllColorants
Typst Custom(n) <-> krilla Custom(n)
Option<Some>    <-> 上面
Option<None>    <-> krilla NoColorant     # 仅 krilla 侧有「无色料」概念
```

#### 4.5.3 源码精读

[src/util.rs:123-162](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/util.rs#L123-L162) —— `ValidatorsExt`：`to_comma_list` 逐项写出、用 `", "` 分隔；`to_and_list` 先把名字收进 `SmallVec<[_; 2]>`，再用模式匹配处理 0/1/2/≥3 四种长度——两项时 `` {a} and {b} ``，三项及以上时 `` {rest..}, and {last} ``（带牛津逗号）。两者都用 `typst_utils::display` 包装成 `impl Display`，可以像字符串一样写入任何 `fmt::Write`。

[src/util.rs:200-206](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/util.rs#L200-L206) —— `display_font`：接收 `Option<&FontInstance>`，`Some` 时取 `font.info().family.repr()` 拼成 `` font `…` ``，`None` 时返回 `"a font"`。注意它不是 trait 方法，而是一个独立的 `pub(crate) fn`，因为它需要「`Option` + 格式化」这种不适合做成扩展方法的逻辑。

[src/util.rs:164-198](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/util.rs#L164-L198) —— 专色名转换。`SpotColorantToNameExt`（第 172 行）把 `SpotColorantName::All/Custom` 映射到 krilla 的 `AllColorants/Custom`；第 181 行还为 `Option<T>` 多写了一个实现，把 `None` 映射到 krilla 独有的 `NoColorant`。`SpotColorantFromNameExt`（第 190 行）是反方向转换，用于从 krilla 校验错误里还原 Typst 的色料名。

实际使用 `ValidatorsExt` 与 `display_font` 的诊断链路（`convert.rs` 的错误映射，详见 [u5-l18](u5-l18-error-and-validation-mapping.md)）：

[src/convert.rs:542](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L542) —— `` eco_format!("{} error:", failing_validators.to_comma_list()) ``，用 `to_comma_list` 拼出「哪些校验器报错」。

[src/convert.rs:446](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L446) —— `display_font(gc.fonts_backward.get(&f))`，用 `fonts_backward` 反向映射把 krilla 字体还原成 Typst 字体，再拼出可读的字体名（与 [u2-l5](u2-l5-convert-orchestrator.md) 讲的「字体反向缓存支持错误反查」呼应）。

[src/paint.rs:159](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/paint.rs#L159) —— `color.colorant.name.to_krilla()`，专色名转换在 `convert_spot` 里把 Typst 专色交给 krilla 的 `SeparationSpace`。

#### 4.5.4 代码实践

实践类型：源码阅读。

1. 在 [src/convert.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs) 搜索 `to_and_list` 与 `to_comma_list` 的所有出现，观察它们分别出现在哪种错误信息里（提示：一个偏「列举校验器」，一个偏「自然语言结尾」）。
2. 在 [src/convert.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs) 搜索 `display_font`，确认它在「字体相关错误」里被调用，并把 `gc.fonts_backward` 的反查结果作为参数。

**预期结果**：你会看到 `to_and_list` 多用于「自然语言句子」式的提示（如「…requires X and Y」），而 `to_comma_list` 多用于「标签前缀」（如「X, Y error:」）。`display_font` 配合反向映射，让面向用户的错误信息能用 Typst 侧的字体家族名（而非 krilla 内部 id）来表述。

#### 4.5.5 小练习与答案

**练习 1**：`to_and_list` 用 `SmallVec<[_; 2]>` 而不是 `Vec`，有什么好处？

**参考答案**：校验器报错时通常只有 1–2 个，`SmallVec<[_; 2]>` 在元素不超过 2 时完全在栈上存储、不触发堆分配，省去了一次 `malloc`；只有罕见的多校验器情况才会退化到堆。这是零成本应对常见情况、为罕见情况保留正确性的典型优化。

**练习 2**：为什么 `SpotColorantToNameExt` 既要为 `SpotColorantName` 实现、又要为 `Option<T>` 实现？

**参考答案**：因为 krilla 的 `SeparationColorant` 有三种状态（`AllColorants` / `Custom` / `NoColorant`），而 Typst 的 `SpotColorantName` 只有两种（`All` / `Custom`）。要表达 krilla 的 `NoColorant`，Typst 侧必须用 `Option<SpotColorantName>` 的 `None`。因此需要为 `Option<T>` 额外实现一次转换，把 `None` 映射到 `NoColorant`，覆盖全部三种状态。

---

## 5. 综合实践

本任务把本讲三块内容（枚举映射、路径转换、诊断辅助）串起来，全部为**源码阅读型实践**（无需运行）。

### 任务一：三组枚举的「值对应表」

挑选以下三个转换 trait，分别列出 Typst 侧 → krilla 侧的成员对应关系，并各举一个用户侧的触发场景：

1. **`TransformExt`**（`src/util.rs:83-98`）：不是枚举，而是结构体字段映射。请说明六个字段 `(sx, ky, kx, sy, tx, ty)` 各自的 Typst 类型、取值方式（`.get()` 还是 `.to_f32()`）以及对应 krilla `from_row` 的哪个参数。触发场景：任何带旋转/缩放的元素（如 `rotate()`、`scale()`）。
2. **`FillRuleExt`**（`src/util.rs:100-111`）：列出 `NonZero → NonZero`、`EvenOdd → EvenOdd` 的对应，并说明在 `path(fill-rule: ...)` 里设 `evenodd` 时会如何影响一个自相交星形。
3. **`SpotColorantToNameExt`**（`src/util.rs:164-188`）：列出 `All → AllColorants`、`Custom(n) → Custom(n)`、`Option<None> → NoColorant` 三种对应，并说明 krilla 为何比 Typst 多一个 `NoColorant` 状态。

### 任务二：解释 `convert_path` 为何要分别处理四种段

阅读 [src/util.rs:208-225](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/util.rs#L208-L225)，写一段 150 字左右的说明，要点包括：

- 四种 `CurveItem` 分别对应 PDF 的哪个算符（`m` / `l` / `c` / `h`）。
- 为什么 `Move` 和 `Line` 都只带一个点却不能合并（抬笔 vs 画线）。
- 为什么 `Cubic` 带 3 个点而非 4 个（起点是隐式当前点）。
- 为什么没有 `Quad`（PDF 只支持三次贝塞尔，Typst 已在排版阶段扁平化）。

### 任务三：跟踪一条真实调用链

从 [src/convert.rs:407-411](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L407-L411)（裁剪路径的构造）出发，画出「一个带 `clip` 的 group frame」是如何经过 `PathBuilder::new()` → `convert_path` → `finish()` → `transform.to_krilla()`（来自 `TransformExt`）最终变成 krilla 裁剪路径的。用伪代码或箭头图表示，并标注每一步用到了本讲的哪个工具。

**完成标志**：你能不看源码，复述出 `util.rs` 提供了哪几类转换工具、各自的方法名（多为 `to_krilla`）、以及 `convert_path` 的「追加式」设计与四段分派的原因。

## 6. 本讲小结

- `util.rs` 是 `typst-pdf` 的类型翻译官，统一采用「`pub(crate) trait XxxExt { fn to_krilla(...) }`」扩展 trait 模式，把 Typst 类型翻译成 krilla 类型，调用方一行 `value.to_krilla()` 即可。
- 一切几何量的地基是 `AbsExt::to_f32()`：`self.to_pt() as f32`，把 Typst 绝对长度换算成 PDF 点（`f32`）。
- 几何类型 `PointExt` / `SizeExt` / `TransformExt` / `SidesExt` 各自调用 krilla 构造函数；其中 `Transform` 的四个 `Ratio` 用 `.get()`、两个 `Abs` 用 `.to_f32()`，按 `from_row(sx,ky,kx,sy,tx,ty)` 顺序传入；`SizeExt` 因 `from_wh` 会校验合法性而返回 `Option`。
- 绘制枚举 `LineCapExt` / `LineJoinExt` / `FillRuleExt` 是穷尽 `match` 的一一映射，看似枯燥却换来编译期安全：任一侧枚举新增成员都会触发编译错误。
- `convert_path()` 是本文件唯一带实质逻辑的转换：它以「追加式」接收 `&mut PathBuilder`，把 `Curve` 的 `Move`/`Line`/`Cubic`/`Close` 四种段分别映射到 PDF 的 `m`/`l`/`c`/`h` 算符。
- `ValidatorsExt`（`to_comma_list` / `to_and_list`，后者带牛津逗号、用 `SmallVec<[_;2]>`）、`display_font`、专色名双向转换共同服务于错误诊断与专色场景。

## 7. 下一步学习建议

本讲把「翻译官」讲清楚了，接下来可以带着这套工具去读各个内容翻译器，看它们如何**消费**这些 trait：

- **[u3-l9 文字与字体](u3-l9-text-and-fonts.md)**：看 `handle_text` 如何用 `TransformExt` 压栈、用 `AbsExt` 处理字号。
- **[u3-l10 图形与几何](u3-l10-shapes-and-geometry.md)**：看 `handle_shape` / `convert_geometry` 如何用 `convert_path` 把 `Geometry::Curve` 落成 krilla 路径，以及负尺寸矩形的修正。
- **[u3-l11 纯色与色彩空间](u3-l11-solid-paint-and-color-spaces.md)**：看 `convert_fill` / `convert_stroke` 如何组合 `LineCapExt` / `LineJoinExt` / `FillRuleExt` / `SpotColorantToNameExt`。
- 若想看这些诊断工具如何拼成最终错误信息，可跳到 **[u5-l18 错误处理与校验结果映射](u5-l18-error-and-validation-mapping.md)**。

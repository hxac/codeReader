# 度量与几何类型

## 1. 本讲目标

Typst 是一个排版引擎，而排版的本质就是「在二维平面上放置和测量内容」。于是 `typst-library` 的 `src/layout/` 目录里有一整套「度量与几何原语」类型，它们是后面所有布局元素（页面、网格、堆叠、定位……）的公共词汇。

本讲聚焦其中最基础的一层：长度、角度、比率、份额、方向与对齐。读完本讲你应当能够：

1. 说出 `Abs`、`Em`、`Length`、`Rel<Length>` 各自表示什么，以及它们为何层层嵌套。
2. 解释一段 `20% + 5cm` 在源码里是如何被表示、又如何被「相对某个容器宽度」解析成绝对值的。
3. 理解为什么 `Em::resolve` 必须吃一条 `StyleChain`——也就是「em 换算成绝对长度为什么依赖字体大小」。
4. 看懂 `Dir`（方向轴）与 `Alignment`（对齐轴）的轴语义，以及 `start`/`end` 如何随文字方向翻转。

> 本讲只讲「度量原语本身」的类型与算术，**不讲**它们如何驱动区域切分与帧生成——那是 u6-l2（Region/Frame/Fragment）的主题。样式查询链 `StyleChain` 的细节已在 u4-l1 讲过，本讲只会作为「消费者」使用它。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**直觉一：度量值在 Typst 里不是裸 `f64`。**
为了不把「5 厘米」和「5 度」加在一起，源码为每种度量都定义了一个「新类型（newtype）」：`Abs(Scalar)`、`Em(Scalar)`、`Angle(Scalar)`、`Ratio(Scalar)`、`Fr(Scalar)`。它们内部都只是一个 `f64`，但编译器把它们当成不同类型，互不相加。这样新类型的体积恒为 8 字节，复制、哈希都极廉价。

**直觉二：有些度量是「绝对的」，有些是「相对的」，相对量必须事后解析。**
`5cm` 是绝对的，编译期就知道多少原始单位；但 `2em`（2 倍字体大小）和 `20%`（占容器宽度 20%）只有等「字体大小」或「容器宽度」确定后才能算出真实距离。源码用两套机制处理这种「延迟」：一是把 `em` 与 `abs` 并列存在一个 `Length` 里，二是用 `Resolve` trait（u4-l1 已讲）在拿到样式链后才把相对量解析成绝对量。

**直觉三：相对量之间用「复合类型」组合，而不是多个变量。**
一个常见的输入是 `25% + 1cm`——既有相对部分又有绝对部分。源码为此定义了泛型 `Rel<T> { rel: Ratio, abs: T }`，专门表达「按某整体的比例 + 一段绝对偏移」。这种「把同类信息打包成一个值」的做法贯穿整个 layout 模块。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `src/layout/` 下（部分轴/边类型在 `axes.rs`/`sides.rs`，样式查询链在 `foundations/styles.rs`，字号折叠在 `text/mod.rs`）：

| 文件 | 作用 |
| --- | --- |
| [`src/layout/abs.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/abs.rs) | 绝对长度 `Abs`，及 pt/mm/cm/in 四种单位的原始换算 |
| [`src/layout/em.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/em.rs) | 字体相对长度 `Em`，其 `resolve` 依赖字体大小 |
| [`src/layout/length.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/length.rs) | 复合长度 `Length = { abs, em }`，用户写的 `5pt`/`2em` 都先落到这里 |
| [`src/layout/rel.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/rel.rs) | 相对量 `Rel<T> = { rel: Ratio, abs: T }`，承载 `20% + 5cm` 这类混合表达 |
| [`src/layout/ratio.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/ratio.rs) | 比率 `Ratio`，如 `25%` |
| [`src/layout/angle.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/angle.rs) | 角度 `Angle`，rad/deg 及三角函数 |
| [`src/layout/fr.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/fr.rs) | 份额 `Fr`，用于 `1fr` 这类「瓜分剩余空间」语义 |
| [`src/layout/dir.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/dir.rs) | 方向 `Dir`（ltr/rtl/ttb/btt），决定主轴走向 |
| [`src/layout/align.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/align.rs) | 对齐 `Alignment`（含水平/垂直分量与 `start`/`end`） |

## 4. 核心概念与源码讲解

### 4.1 绝对长度 `Abs` 与原始单位系统

#### 4.1.1 概念说明

`Abs` 是「绝对长度」——一个不依赖任何上下文、编译期就完全确定的距离。用户写的 `2.54cm`、`72pt`、`1in`、`254mm` 都属于这一类。它是一切长度类型的最底层积木：`Length`、`Rel<Length>`、`Size`、`Point`、`Sides` 里的「绝对分量」最终都落到 `Abs`。

`Abs` 的设计要点是：**内部不存 pt 也不存 cm，而是存一个统一的「原始单位（raw）」**，单位换算只在「构造时乘比例」「读取时除比例」两处发生。这样做的好处是同一份长度可以自由地以任意单位读写，而算术运算始终在统一尺度上进行。

#### 4.1.2 核心流程

`Abs` 的生命周期可以概括为：

1. **构造**：`Abs::with_unit(val, unit)` 把「数值 × 该单位的换算系数」存成一个 `Scalar`。
2. **运算**：`Abs` 与 `Abs` 之间的 `+`、`-`、比较、求和等，全部直接对内部的 `Scalar`（原始单位）操作，无需换算。
3. **读取**：`to_pt()`/`to_mm()` 等把原始单位除以对应系数，换算成所需单位输出。

关键在于「换算系数」从哪来。源码为四种绝对单位各选了一个整数原始尺度，使得「所有单位下的整数都能被精确表示」：

| 单位 | `raw_scale()` | 含义：1 个该单位 = 多少 raw |
| --- | --- | --- |
| pt | 127.0 | 1 pt = 127 raw |
| mm | 360.0 | 1 mm = 360 raw |
| cm | 3600.0 | 1 cm = 3600 raw |
| in | 9144.0 | 1 in = 9144 raw |

这些数值并非随意：因为 \(72\text{pt}=1\text{in}\)，而 \(127 \times 72 = 9144\)；同理 \(360 \times 10 = 3600\)、\(2.54\text{cm}=1\text{in}\) 也都对得上。选取整数倍尺度后，整数 pt/mm/cm/in 在原始单位下仍是整数，避免了浮点累计误差。

#### 4.1.3 源码精读

`Abs` 是一个新类型，内部仅一个 `Scalar`：

```rust
// src/layout/abs.rs
/// An absolute length.
#[derive(Default, Copy, Clone, Eq, PartialEq, Ord, PartialOrd, Hash)]
pub struct Abs(Scalar);
```
> [src/layout/abs.rs:10-12](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/abs.rs#L10-L12) —— `Abs` 的全部内部表示就是一个 `f64`。

构造函数把数值乘上单位系数：

```rust
// src/layout/abs.rs
pub fn with_unit(val: f64, unit: AbsUnit) -> Self {
    Self(Scalar::new(val * unit.raw_scale()))
}
pub fn pt(pt: f64) -> Self { Self::with_unit(pt, AbsUnit::Pt) }
pub fn mm(mm: f64) -> Self { Self::with_unit(mm, AbsUnit::Mm) }
pub fn cm(cm: f64) -> Self { Self::with_unit(cm, AbsUnit::Cm) }
pub fn inches(inches: f64) -> Self { Self::with_unit(inches, AbsUnit::In) }
```
> [src/layout/abs.rs:31-53](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/abs.rs#L31-L53) —— `pt`/`mm`/`cm`/`inches` 都是对 `with_unit` 的薄封装。

而单位系数定义在 `AbsUnit::raw_scale`，注释解释了为何选这些数：

```rust
// src/layout/abs.rs
const fn raw_scale(self) -> f64 {
    // We choose a raw scale which has an integer conversion value to all
    // four units of interest, so that whole numbers in all units can be
    // represented accurately.
    match self {
        AbsUnit::Pt => 127.0,
        AbsUnit::Mm => 360.0,
        AbsUnit::Cm => 3600.0,
        AbsUnit::In => 9144.0,
    }
}
```
> [src/layout/abs.rs:265-275](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/abs.rs#L265-L275) —— 四种单位的原始尺度。

读取只需除回系数，例如 `to_unit` 与 `to_pt`：

```rust
// src/layout/abs.rs
pub fn to_unit(self, unit: AbsUnit) -> f64 { self.to_raw() / unit.raw_scale() }
pub fn to_pt(self) -> f64 { self.to_unit(AbsUnit::Pt) }
```
> [src/layout/abs.rs:66-73](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/abs.rs#L66-L73) —— 换算只在读取时发生。

算术运算直接对原始单位操作，例如 `Abs + Abs`：

```rust
// src/layout/abs.rs
impl Add for Abs {
    type Output = Self;
    fn add(self, other: Self) -> Self { Self(self.0 + other.0) }
}
```
> [src/layout/abs.rs:169-175](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/abs.rs#L169-L175) —— 两个 `Abs` 相加就是两个 `Scalar` 相加，无需关心单位。

最后，源码里有一条单测直观展示了换算关系（`150mm` 应等于 `15cm`）：

```rust
// src/layout/abs.rs
#[test]
fn test_length_unit_conversion() {
    assert!((Abs::mm(150.0).to_cm() - 15.0) < 1e-4);
}
```
> [src/layout/abs.rs:282-285](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/abs.rs#L282-L285) —— 通过单位换算一致性来验证 `raw_scale` 的正确性。

#### 4.1.4 代码实践

**实践目标**：亲手验证「原始单位」机制，体会「换算只在边界发生」。

**操作步骤**：

1. 打开 [`src/layout/abs.rs:260-276`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/abs.rs#L260-L276)，确认四种单位的 `raw_scale`。
2. 用铅笔/心算验证三条等式：
   - \(72\text{pt} = 72 \times 127 = 9144 = 1\text{in}\)
   - \(10\text{mm} = 10 \times 360 = 3600 = 1\text{cm}\)
   - \(2.54\text{cm} = 2.54 \times 3600 = 9144 = 1\text{in}\)
3. 在仓库根运行 `cargo test -p typst-library test_length_unit_conversion`。

**需要观察的现象**：第 2 步三条等式都应成立；第 3 步测试应通过。

**预期结果**：因为 `raw_scale` 是整数倍关系，单位换算不会引入浮点累计误差，`150mm` 与 `15cm` 在原始单位下完全相等（误差 `< 1e-4`）。若你修改任意一个 `raw_scale` 值，测试应立刻失败。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Abs` 不直接存 `f64` 的 pt 值，而要引入「原始单位」？

> **参考答案**：若内部存 pt，那么 `1in` 一进来就得换算成 `72pt`，而 `2.54cm` 换成 pt 会得到无限循环小数，造成精度损失。改用整数倍尺度的原始单位后，每个单位下的整数都能被精确表示，运算全程在同一尺度下进行，换算误差被推到「读取时一次性除法」这一处，可忽略不计。

**练习 2**：`Abs::fits` 和 `Abs::approx_eq` 为什么都需要一个 `EPS = 1e-4`（见 [`abs.rs:262`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/abs.rs#L262)）？

> **参考答案**：浮点运算不可避免地产生微小误差。排版中判断「某元素能否放进容器」时，若严格用 `<=`，一个「理论上恰好填满」的元素可能因 `1e-10` 级误差被误判放不下。引入一个微小松弛量 `EPS` 既容许这种误差，又不会显著改变排版结果。

---

### 4.2 字体相对长度 `Em` 与样式依赖

#### 4.2.1 概念说明

`Em` 是「字体相对长度」：`1em` 等于当前字体大小。这是排版里极其常用的单位——行距、缩进、字间距、上下标偏移都习惯以 em 表达，这样字号一变，所有相关距离等比例缩放。

`Em` 与 `Abs` 在表示上几乎一样（都是 `Scalar` 新类型），但有一个本质区别：**`Em` 无法单独换算成绝对长度**。要知道 `2em` 是多少 pt，必须先知道「当前字体大小是多少 pt」。这正是本讲的关键难点——为什么 `Em` 的解析依赖样式链 `StyleChain`。

#### 4.2.2 核心流程

`Em` 的换算公式很简单：

\[
\text{abs} = \text{em} \times \text{font\_size}
\]

但「`font_size` 从哪来」才是重点：

1. 字体大小在 Typst 里是一个**可被 set 规则层层覆盖、且会折叠**的样式字段 `TextElem::size`（类型 `TextSize(Length)`）。
2. 想拿到最终的字体大小（`Abs`），必须用 `styles.resolve(TextElem::size)` 沿样式链查询并折叠。
3. 因此 `Em::resolve` 的签名是 `fn resolve(self, styles: StyleChain) -> Abs`——它要的就是这条链。

折叠本身还有一层微妙之处：因为字号字段也用 `Length`（含 em 分量）表示，`TextSize::fold` 是「把两个线性函数相乘」（见下方源码），使得嵌套的 `set text(size: …)` 能正确地把相对字号层层放大。这正是 u4-l1 提到的 `Fold` 语义在字号上的具体应用。

#### 4.2.3 源码精读

`Em` 的定义与 `Abs` 形式相同：

```rust
// src/layout/em.rs
/// A length that is relative to the font size.
///
/// `1em` is the same as the font size.
#[derive(Default, Copy, Clone, Eq, PartialEq, Ord, PartialOrd, Hash)]
pub struct Em(Scalar);
```
> [src/layout/em.rs:12-16](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/em.rs#L12-L16) —— `Em` 内部同样只是一个 `f64`（em 倍数）。

把 em 换算成绝对长度的核心方法 `at`：

```rust
// src/layout/em.rs
/// Converts to an absolute length at the given font size.
pub fn at(self, font_size: Abs) -> Abs {
    let resolved = font_size * self.get();
    if resolved.is_finite() { resolved } else { Abs::zero() }
}
```
> [src/layout/em.rs:61-64](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/em.rs#L61-L64) —— 公式 \(\text{abs} = \text{font\_size} \times \text{em}\)；非有限值（如除零）兜底为 0。

注意 `at` 只接受一个已知的 `Abs` 字号。而真正「从样式里取出字号」的逻辑在 `Resolve` 实现里：

```rust
// src/layout/em.rs
impl Resolve for Em {
    type Output = Abs;

    fn resolve(self, styles: StyleChain) -> Self::Output {
        if self.is_zero() { Abs::zero() } else { self.at(styles.resolve(TextElem::size)) }
    }
}
```
> [src/layout/em.rs:157-163](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/em.rs#L157-L163) —— 这就是「`Em::resolve` 为何需要 `StyleChain`」的答案：字号要从 `TextElem::size` 这个样式字段里查出来。

那么 `TextElem::size` 本身如何被查询/折叠？它是 `Length` 类型的字段，折叠时把两个 `Length`（本质两个线性函数）相乘：

```rust
// src/text/mod.rs
pub struct TextSize(pub Length);

impl Fold for TextSize {
    fn fold(self, outer: Self) -> Self {
        // Multiply the two linear functions.
        Self(Length {
            em: Em::new(self.0.em.get() * outer.0.em.get()),
            abs: self.0.em.get() * outer.0.abs + self.0.abs,
        })
    }
}
```
> [src/text/mod.rs:1127-1136](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L1127-L1136) —— 字号折叠 = 两个线性函数相乘。这就解释了为什么 em 解析必须在样式折叠完成之后。

把一个 `Abs` 反向换算成 `Em` 也有用（例如把绝对字距转成 em），见 `from_abs`：

```rust
// src/layout/em.rs
pub fn from_abs(length: Abs, font_size: Abs) -> Self {
    let result = length / font_size;
    if result.is_finite() { Self(Scalar::new(result)) } else { Self::zero() }
}
```
> [src/layout/em.rs:40-43](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/em.rs#L40-L43) —— `at` 的逆运算：abs 除以字号得到 em。

#### 4.2.4 代码实践

**实践目标**：体会「同一个 `Em` 值在不同字号下解析出不同 `Abs`」。

**操作步骤**：

1. 阅读 [`em.rs:157-163`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/em.rs#L157-L163) 的 `Em::resolve`。
2. 写一段 Typst 源码（这属于用户侧验证，不是源码修改）观察 em 随字号变化：

```typst
#set text(size: 12pt)
#context [(2em).to-absolute()]  // 期望约 24pt

#set text(size: 6pt)
#context [(2em).to-absolute()]  // 期望约 12pt
```

**需要观察的现象**：同一段 `(2em)` 在 12pt 字号下解析为 24pt，在 6pt 字号下解析为 12pt。

**预期结果**：验证了「em 是字体相对量」——其绝对值随 `TextElem::size` 而变。`to-absolute` 内部（见 [`length.rs:157-160`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/length.rs#L157-L160)）正是调用了 `Resolve`，因此必须包在 `context` 里——没有 context 就拿不到样式链。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Em` 能派生 `Eq`/`Ord`/`Hash`，而 `Abs` 也能，但「em 的绝对长度」却不能直接比较？

> **参考答案**：`Em` 比较的是「em 倍数」本身（如 `2em == 2em`），这与上下文无关，所以可以派生这些 trait。但「2em 在 12pt 字号下是多少 pt」是一个 `Abs`，它依赖字号，只有在 resolve 之后才能比较。源码把「可比较的、与上下文无关的部分」放进 `Em` 自身，把「依赖上下文的绝对值」留给 resolve 结果。

**练习 2**：在 [`em.rs:161`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/em.rs#L161) 里，为什么 `self.is_zero()` 时直接返回 `Abs::zero()`，而不是走 `at`？

> **参考答案**：当 em 为 0 时，无论字号多少结果都是 0，直接短路返回可以避免一次无意义的 `styles.resolve(TextElem::size)` 查询（那次查询会沿样式链遍历，有成本）。这是把「显然为 0」的常见情形提前裁掉的微小优化。

---

### 4.3 复合长度 `Length` 与相对长度 `Rel<Length>`

> 这是本讲的核心模块，承载了练习任务里「`20% + 5cm` 如何被表示与解析」的全部内容。

#### 4.3.1 概念说明

现在我们有了两种长度积木：绝对的 `Abs` 和字体相对的 `Em`。一个真实的尺寸往往同时含有两者，比如 `#set text(size: 12pt)` 下的行距 `1.2em + 2pt`。源码用 `Length` 把它们并列存起来：

```rust
pub struct Length { pub abs: Abs, pub em: Em }
```

而 `20% + 5cm` 这类「相对某整体的比例 + 绝对偏移」则由更外层的 `Rel<T>` 承载：

```rust
pub struct Rel<T: NumericLength = Length> { pub rel: Ratio, pub abs: T }
```

注意 `Rel<T>` 是泛型的：`Rel<Length>`、`Rel<Abs>` 都合法。布局里最常见的就是 `Rel<Length>`——它同时支持百分比、em、pt 等所有写法。

#### 4.3.2 核心流程

理解 `Length` 与 `Rel<Length>` 的关键是「线性函数」视角。一个 `Length` 可以看作关于字号 \(s\) 的一次函数：

\[
L(s) = \text{abs} + \text{em} \cdot s
\]

`Length::resolve(styles)` 就是把当前字号 \(s\) 代入，得到一个 `Abs`。

而 `Rel<Length>` 可以看作关于「容器整体宽度 \(w\)」的一次函数，其系数本身又是一个关于 \(s\) 的函数：

\[
R(w, s) = \text{rel} \cdot w + \text{abs}(s)
\]

`Rel::relative_to(whole)` 只代入 \(w\)，保留 `abs` 仍为 `Length`；而 `Rel::resolve(styles)` 则把内部的 `Length` 也解析掉，得到 `Rel<Abs>`。

现在来看 `20% + 5cm` 是怎么被构造出来的：

1. 解析器把 `20%` 解析为 `Ratio(0.2)`，把 `5cm` 解析为 `Length { abs: Abs::cm(5.0), em: Em::zero() }`。
2. `+` 号触发 `impl Add<Length> for Ratio`（见下方源码），两边各自升级成 `Rel<Length>` 再相加。
3. 结果是 `Rel { rel: Ratio(0.2), abs: Length { abs: 5cm, em: 0 } }`——百分比部分和绝对部分被分别保存。
4. 之后布局代码用 `relative_to(container_width)` 代入 \(w\)，得到 `0.2 * container_width + 5cm`（仍是一个 `Length`）。

#### 4.3.3 源码精读

`Length` 的定义，注意它直接把 `abs` 与 `em` 并列：

```rust
// src/layout/length.rs
#[ty(scope, cast, since = "forever")]
#[derive(Default, Copy, Clone, Eq, PartialEq, Hash)]
pub struct Length {
    /// The absolute part.
    pub abs: Abs,
    /// The font-relative part.
    pub em: Em,
}
```
> [src/layout/length.rs:42-49](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/length.rs#L42-L49) —— `Length` 就是 `Abs` + `Em` 两个字段。

给定字号，把 `Length` 解析成 `Abs`：

```rust
// src/layout/length.rs
/// Convert to an absolute length at the given font size.
pub fn at(self, font_size: Abs) -> Abs {
    self.abs + self.em.at(font_size)
}
```
> [src/layout/length.rs:75-77](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/length.rs#L75-L77) —— 直接套用线性函数 \(L(s) = \text{abs} + \text{em} \cdot s\)。

`Length::resolve` 把字号从样式链取来，再调 `at`：

```rust
// src/layout/length.rs
impl Resolve for Length {
    type Output = Abs;

    fn resolve(self, styles: StyleChain) -> Self::Output {
        self.abs + self.em.resolve(styles)
    }
}
```
> [src/layout/length.rs:266-272](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/length.rs#L266-L272) —— em 部分交给 `Em::resolve`，abs 部分原样保留。

`Rel<T>` 的定义：

```rust
// src/layout/rel.rs
#[ty(cast, name = "relative", title = "Relative Length", since = "forever")]
#[derive(Default, Copy, Clone, Eq, PartialEq, Hash)]
pub struct Rel<T: NumericLength = Length> {
    /// The relative part.
    pub rel: Ratio,
    /// The absolute part.
    pub abs: T,
}
```
> [src/layout/rel.rs:76-83](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/rel.rs#L76-L83) —— 默认泛型参数是 `Length`，所以 `Rel` 默认就是 `Rel<Length>`。

把 `Rel<T>` 代入一个「整体」值，得到 `T`——这正是布局里把 `20% + 5cm` 应用到容器宽度的地方：

```rust
// src/layout/rel.rs
/// Evaluate this relative to the given `whole`.
pub fn relative_to(self, whole: T) -> T {
    self.rel.of(whole) + self.abs
}
```
> [src/layout/rel.rs:111-114](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/rel.rs#L111-L114) —— `rel.of(whole)` 是百分比部分，`+ self.abs` 是偏移部分。

其中 `Ratio::of` 的实现：

```rust
// src/layout/ratio.rs
/// Return the ratio of the given `whole`.
pub fn of<T: Numeric>(self, whole: T) -> T {
    let resolved = whole * self.get();
    if resolved.is_finite() { resolved } else { T::zero() }
}
```
> [src/layout/ratio.rs:107-111](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/ratio.rs#L107-L111) —— `whole * ratio`，非有限值兜底 0。

最关键的一步：`Ratio + Length` 如何产生 `Rel<Length>`。源码为 `impl Add<T> for Ratio` 做了升级：

```rust
// src/layout/rel.rs
impl<T: NumericLength> Add<T> for Ratio {
    type Output = Rel<T>;

    fn add(self, other: T) -> Self::Output {
        Rel::from(self) + Rel::from(other)
    }
}
```
> [src/layout/rel.rs:284-290](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/rel.rs#L284-L290) —— 这就是 `20% + 5cm` 的构造入口：`Ratio(0.2) + Length{5cm}` → `Rel<Length>`。

而两个 `Rel<T>` 相加时，百分比和绝对部分各自相加：

```rust
// src/layout/rel.rs
impl<T: NumericLength> Add for Rel<T> {
    type Output = Self;

    fn add(self, other: Self) -> Self::Output {
        Self { rel: self.rel + other.rel, abs: self.abs + other.abs }
    }
}
```
> [src/layout/rel.rs:213-222](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/rel.rs#L213-L222) —— 按字段拆分相加，保持结构。

最后，`Rel<Length>::resolve` 会把内部的 `Length` 解析掉，得到 `Rel<Abs>`（百分比仍在，em 已消除）：

```rust
// src/layout/rel.rs
impl<T> Resolve for Rel<T>
where
    T: Resolve + NumericLength,
    <T as Resolve>::Output: NumericLength,
{
    type Output = Rel<<T as Resolve>::Output>;

    fn resolve(self, styles: StyleChain) -> Self::Output {
        self.map(|abs| abs.resolve(styles))
    }
}
```
> [src/layout/rel.rs:316-326](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/rel.rs#L316-L326) —— 只解析绝对分量，保留百分比分量待 `relative_to` 处理。

#### 4.3.4 代码实践（本讲核心实践任务）

**实践目标**：用源码追踪 `20% + 5cm` 从构造到解析的全过程，并解释 `Em::resolve` 为何需要 `StyleChain`。

**操作步骤**：

1. **构造**：在 [`rel.rs:284-290`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/rel.rs#L284-L290) 确认 `Ratio + Length` 升级为 `Rel<Length>`。请把 `20% + 5cm` 手工展开：
   - `rel = Ratio(0.2)`
   - `abs = Length { abs: Abs::cm(5.0), em: Em::zero() }`
   - 组装结果：`Rel::<Length> { rel: 0.2, abs: Length{abs: 5cm, em: 0} }`
2. **代入容器宽度**：在 [`rel.rs:111-114`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/rel.rs#L111-L114) 阅读 `relative_to`。假设容器宽度为 `whole = 10cm`，则 `rel.of(whole) + abs = 0.2 * 10cm + 5cm = 7cm`（一个 `Length`，因为 `abs` 仍是 `Length`）。
3. **解析 em（若有）**：本例 `em` 为 0，故 [`length.rs:266-272`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/length.rs#L266-L272) 中 `self.em.resolve(styles)` 走 [`em.rs:161`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/em.rs#L161) 的短路返回 0，字号查询被跳过。
4. **思考变体**：把输入改成 `20% + 5cm + 2em`。现在 `em` 不为零，`Em::resolve` 必须调用 `styles.resolve(TextElem::size)` 取字号——这就是「Em 解析依赖 StyleChain」的全部原因。
5. **用户侧验证**（非源码修改）：

```typst
#block(width: 10cm)[
  #context [
    容器宽 10cm 时，(20% + 5cm) 解析为：
    #measure((20% + 5cm))  // 观察其宽度
  ]
]
```

**需要观察的现象**：第 2 步手算得到 7cm；第 5 步测得宽度应接近 7cm（受容器/字体影响可能有微小差异）。

**预期结果**：`Rel<Length>` 把百分比与绝对部分分开存储，`relative_to` 代入整体宽度、`resolve` 代入字号，两个维度独立。当 `em != 0` 时，resolve 必须查询字号样式链——这解释了为何相对/字体单位的最终值总是「延迟到布局时才确定」。

> 若你无法在本地编译运行，相关手算结论已可直接从源码得出，第 5 步可标注为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Length` 的 `PartialOrd`（[`length.rs:195-205`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/length.rs#L195-L205)）在「abs 和 em 都非零」时返回 `None`？

> **参考答案**：当 `Length` 同时有 abs 和 em 分量时，它是一个关于字号 \(s\) 的线性函数 \(a + e \cdot s\)，在字号未确定前无法与另一个 `Length` 比大小（两条直线可能相交）。只有当某一方 em 为 0（退化为常数）或双方都纯 em（可比较斜率）时，比较才有确定结果，故返回 `None` 表示「不可比较」。`Rel` 的 `partial_cmp` 出于同样理由（多了一个百分比维度）也返回 `None`。

**练习 2**：`Rel::one()` 与 `Rel::zero()` 分别表示什么（[`rel.rs:87-94`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/rel.rs#L87-L94)）？`relative_to(whole)` 对 `Rel::one()` 会得到什么？

> **参考答案**：`one()` 是「100% + 0」，即恰好等于整体；`zero()` 是「0% + 0」，即零。`Rel::one().relative_to(whole) = one().rel.of(whole) + 0 = 1.0 * whole + 0 = whole`，即返回整体本身。

---

### 4.4 角度 `Angle`、比率 `Ratio` 与份额 `Fr`

#### 4.4.1 概念说明

长度之外，布局与变换还需要另外三种标量度量：

- **`Angle`（角度）**：用于旋转、斜切。支持 `rad` 与 `deg`，并提供三角函数（`sin`/`cos`/`atan2` 等）。
- **`Ratio`（比率）**：就是 `25%` 这种「占整体的比例」。它不仅出现在 `Rel` 里，也独立用于缩放（如 `scale(x: 150%)`）。
- **`Fr`（份额/fraction）**：用于「瓜分剩余空间」，如 `grid` 的 `1fr` 轨道、`h(1fr)` 的弹性间距。`1fr` 不是「占 100%」，而是「占剩余空间中我的份数 ÷ 总份数」。

三者的内部表示都是 `Scalar`（一个 `f64`），且都实现了 `Numeric` trait，但语义和算术各不相同。

#### 4.4.2 核心流程

**`Angle`** 采用与 `Abs` 完全一致的「原始单位」模式：内部存 rad（弧度），`deg` 在构造时乘 \( \pi/180 \)。它额外提供三角函数——直接在原始单位（rad）上调用 `libm`。

**`Ratio`** 内部存「百分比对应的小数」（`0.2` 表示 `20%`）。它的核心方法是 `of(whole)`——返回整体的某个比例，这正是 `Rel::relative_to` 使用的接口。

**`Fr`** 没有解析时点（它本身不依赖样式），它的核心方法 `share` 计算「我在剩余空间里应分到多少」：

\[
\text{share} = \frac{\text{self}}{\text{total}} \cdot \text{remaining}
\]

其中 `total` 是所有 fr 的和、`remaining` 是容器扣掉固定尺寸后剩下的空间。注意结果被 `max(0)` 钳制——剩余空间为负（内容超出）时不产生负宽度。

#### 4.4.3 源码精读

`Angle` 与 `Abs` 几乎同构，只是单位尺度不同（rad 为 1.0，deg 为 \(\pi/180\)）：

```rust
// src/layout/angle.rs
#[ty(scope, cast, since = "forever")]
#[derive(Default, Copy, Clone, Eq, PartialEq, Ord, PartialOrd, Hash)]
pub struct Angle(Scalar);
```
> [src/layout/angle.rs:23-25](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/angle.rs#L23-L25) —— `Angle` 也是一个 `Scalar` 新类型。

角度单位尺度，注意 deg 的换算系数是 \( \pi/180 \)：

```rust
// src/layout/angle.rs
fn raw_scale(self) -> f64 {
    match self {
        Self::Rad => 1.0,
        Self::Deg => PI / 180.0,
    }
}
```
> [src/layout/angle.rs:248-254](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/angle.rs#L248-L254) —— `Angle` 内部统一存弧度。

三角函数直接在 rad 上算：

```rust
// src/layout/angle.rs
pub fn sin(self) -> f64 { libm::sin(self.to_rad()) }
pub fn cos(self) -> f64 { libm::cos(self.to_rad()) }
```
> [src/layout/angle.rs:81-88](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/angle.rs#L81-L88) —— 用 `libm`（no_std 友好的数学库）计算。

`Ratio` 的定义与 `of`：

```rust
// src/layout/ratio.rs
#[ty(cast, since = "forever")]
#[derive(Default, Copy, Clone, Eq, PartialEq, Ord, PartialOrd, Hash)]
pub struct Ratio(Scalar);
```
> [src/layout/ratio.rs:62-64](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/ratio.rs#L62-L64) —— `0.2` 即 `20%`。

```rust
// src/layout/ratio.rs
/// Return the ratio of the given `whole`.
pub fn of<T: Numeric>(self, whole: T) -> T {
    let resolved = whole * self.get();
    if resolved.is_finite() { resolved } else { T::zero() }
}
```
> [src/layout/ratio.rs:107-111](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/ratio.rs#L107-L111) —— 比率作用于任意 `Numeric`。

`Fr` 的定义与 `share`：

```rust
// src/layout/fr.rs
#[ty(cast, name = "fraction", since = "forever")]
#[derive(Default, Copy, Clone, Eq, PartialEq, Ord, PartialOrd, Hash)]
pub struct Fr(Scalar);
```
> [src/layout/fr.rs:23-25](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/fr.rs#L23-L25) —— `Fr` 内部也是一个 `f64`（份数）。

```rust
// src/layout/fr.rs
/// Determine this fraction's share in the remaining space.
pub fn share(self, total: Self, remaining: Abs) -> Abs {
    let ratio = self / total;
    if ratio.is_finite() && remaining.is_finite() {
        (ratio * remaining).max(Abs::zero())
    } else {
        Abs::zero()
    }
}
```
> [src/layout/fr.rs:53-61](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/fr.rs#L53-L61) —— 套用 \(\text{self}/\text{total} \times \text{remaining}\)，并用 `max(0)` 钳制。

`Fr` 之间的除法返回 `f64`（一个无量纲比值），这是 `share` 里 `self / total` 能得到 ratio 的前提：

```rust
// src/layout/fr.rs
impl Div for Fr {
    type Output = f64;
    fn div(self, other: Self) -> f64 { self.get() / other.get() }
}
```
> [src/layout/fr.rs:120-126](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/fr.rs#L120-L126) —— 同类相除得到标量比值，和 `Abs / Abs -> f64` 同理。

#### 4.4.4 代码实践

**实践目标**：理解 `Fr` 与 `Ratio` 在「分配空间」语义上的差别。

**操作步骤**：

1. 阅读 [`fr.rs:53-61`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/fr.rs#L53-L61) 的 `share`。
2. 手算：容器剩余空间 `remaining = 30cm`，两段弹性间距分别为 `1fr` 与 `2fr`（`total = 3fr`）。求各自分到多少。
   - `1fr.share(3fr, 30cm) = (1/3) * 30cm = 10cm`
   - `2fr.share(3fr, 30cm) = (2/3) * 30cm = 20cm`
3. 对比：若用 `Ratio`，`Ratio::of` 是「占整体的固定比例」，与「其他元素有多少 fr」无关；而 `Fr::share` 依赖 `total`（所有 fr 之和），是动态的。
4. 用户侧验证（非源码修改）：

```typst
Left #h(1fr) Mid #h(2fr) Right
```

**需要观察的现象**：第 2 步得到 10cm 与 20cm；第 4 步中两段空白按 1:2 分配页面剩余宽度。

**预期结果**：`Fr` 是「相对剩余空间的动态分配」，`Ratio` 是「相对整体的固定比例」——两者都叫「相对」，但机制不同。

#### 4.4.5 小练习与答案

**练习 1**：`Angle` 的 `normalized`（[`angle.rs:71-73`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/angle.rs#L71-L73)）为什么用 `rem_euclid(TAU)` 而不是普通 `% TAU`？

> **参考答案**：普通 `%` 在操作数为负时会返回负值（如 `-10deg % 360 = -10`），而旋转角度通常希望落在 `[0, 360deg)` 的非负区间。`rem_euclid` 是「欧几里得取余」，无论正负都返回非负余数，正好满足「角度归一化到 `0..=360deg`」的语义。

**练习 2**：为什么 `Fr / Fr`、`Abs / Abs`、`Ratio / Ratio` 都返回 `f64` 而不是自身类型？

> **参考答案**：两个同类度量相除，物理意义是一个「无量纲比值」（如「A 是 B 的几倍」），它不再是长度/份额/比率，而是一个纯标量。让结果类型为 `f64` 既符合量纲分析，也方便直接拿去当系数用（如 `share` 里 `self/total` 得到的比值再去乘 `Abs`）。

---

### 4.5 方向 `Dir` 与对齐 `Alignment` 的轴语义

#### 4.5.1 概念说明

布局是二维的，于是需要描述「内容沿哪个方向流动」和「内容贴在哪一边」。源码用两个枚举表达：

- **`Dir`（方向）**：四个走向 `ltr`/`rtl`/`ttb`/`btt`，决定主轴的方向与正负。
- **`Alignment`（对齐）**：沿某轴的对齐位置，如 `left`/`center`/`right`、`top`/`horizon`/`bottom`，以及方向相关的 `start`/`end`。

这两者的核心关联是「轴」：`Dir` 分水平（ltr/rtl）和垂直（ttb/btt）两类轴；`Alignment` 也分水平/垂直两类。对齐里的 `start`/`end` 不是固定方向，而是「随文字方向翻转」——这正是多语言排版（如阿拉伯文 rtl）能正确对齐的关键。

`Alignment` 还能「二维组合」：`left + bottom` 表示「水平靠左、垂直靠底」，用 `+` 运算符把一个水平分量和一个垂直分量合成 `Alignment::Both(h, v)`。

#### 4.5.2 核心流程

方向决定「正方向」：源码约定 **ltr 与 ttb 为正方向**（`is_positive() == true`），rtl 与 btt 为负。这个布尔量会影响对齐的最终落点。

对齐的「解析」分两步：

1. **折叠（Fold）**：当多层 set 规则分别给了水平分量和垂直分量时，`Alignment::fold` 把它们合并成一个 `Both`。例如外层 `set align(center)`（水平）、内层 `set align(bottom)`（垂直），折叠后变成 `center + bottom`。
2. **解析（Resolve）**：`Alignment::resolve(styles)` 取出文字方向 `TextElem::dir`，调用 `fix(dir)` 把 `start`/`end` 翻译成固定的 `FixedAlignment::{Start, Center, End}`（在全局坐标里 Start=左/上、End=右/下）。

这套「折叠攒齐两个轴、解析时按方向定型」的设计，让对齐既能分别独立设置两轴，又能正确响应文字方向。

#### 4.5.3 源码精读

`Dir` 枚举与「正方向」判定：

```rust
// src/layout/dir.rs
#[ty(scope, name = "direction", since = "forever")]
#[derive(Debug, Copy, Clone, Eq, PartialEq, Hash)]
pub enum Dir {
    LTR, RTL, TTB, BTT,
}

impl Dir {
    /// Whether this direction points into the positive coordinate direction.
    pub const fn is_positive(self) -> bool {
        match self {
            Self::LTR | Self::TTB => true,
            Self::RTL | Self::BTT => false,
        }
    }
}
```
> [src/layout/dir.rs:21-43](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/dir.rs#L21-L43) —— 四个方向，ltr/ttb 为正。

`Dir` 还能回答「属于哪条轴」：

```rust
// src/layout/dir.rs
#[func(since = "0.7.0")]
pub const fn axis(self) -> Axis {
    match self {
        Self::LTR | Self::RTL => Axis::X,
        Self::TTB | Self::BTT => Axis::Y,
    }
}
```
> [src/layout/dir.rs:96-102](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/dir.rs#L96-L102) —— 方向到轴的映射。`Axis` 只有 `X`/`Y` 两个值（见 [`axes.rs:170-175`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/axes.rs#L170-L175)）。

`Alignment` 枚举把单轴和双轴对齐统一在一起：

```rust
// src/layout/align.rs
#[ty(scope, since = "forever")]
#[derive(Debug, Copy, Clone, Eq, PartialEq, Hash)]
pub enum Alignment {
    H(HAlignment),
    V(VAlignment),
    Both(HAlignment, VAlignment),
}
```
> [src/layout/align.rs:142-148](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/align.rs#L142-L148) —— 三种形态：纯水平、纯垂直、二者兼备。

`+` 运算符负责把一个水平和一个垂直分量合成 `Both`，并拒绝同类相加：

```rust
// src/layout/align.rs
impl Add for Alignment {
    type Output = StrResult<Self>;

    fn add(self, rhs: Self) -> Self::Output {
        match (self, rhs) {
            (Self::H(h), Self::V(v)) | (Self::V(v), Self::H(h)) => Ok(h + v),
            (Self::H(_), Self::H(_)) => bail!("cannot add two horizontal alignments"),
            // ...其余错误分支
        }
    }
}
```
> [src/layout/align.rs:229-248](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/align.rs#L229-L248) —— `left + bottom` 在此合成；两个水平分量相加会报错。

`Alignment::fix` 把 `start`/`end` 按文字方向定型为全局的 `FixedAlignment`：

```rust
// src/layout/align.rs
/// Normalize the alignment to a LTR-TTB space.
pub fn fix(self, text_dir: Dir) -> Axes<FixedAlignment> {
    Axes::new(
        self.x().unwrap_or_default().fix(text_dir),
        self.y().unwrap_or_default().fix(text_dir),
    )
}
```
> [src/layout/align.rs:168-174](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/align.rs#L168-L174) —— 把对齐规范化到「左到右、上到下」的全局坐标系，输出二维 `Axes<FixedAlignment>`。

而 `HAlignment::fix` 才是 `start`/`end` 翻转的真正落点——它依据 `dir.is_positive()` 决定 start 落在左还是右：

```rust
// src/layout/align.rs
impl FixAlignment for HAlignment {
    fn fix(self, dir: Dir) -> FixedAlignment {
        match (self, dir.is_positive()) {
            (Self::Start, true) | (Self::End, false) => FixedAlignment::Start,
            (Self::Left, _) => FixedAlignment::Start,
            (Self::Center, _) => FixedAlignment::Center,
            (Self::Right, _) => FixedAlignment::End,
            (Self::End, true) | (Self::Start, false) => FixedAlignment::End,
        }
    }
}
```
> [src/layout/align.rs:319-329](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/align.rs#L319-L329) —— `start` 在 ltr 下变成全局「左(Start)」，在 rtl 下变成全局「右(End)」；`left`/`right` 则是固定的。

`Alignment::resolve` 取出文字方向再 `fix`：

```rust
// src/layout/align.rs
impl Resolve for Alignment {
    type Output = Axes<FixedAlignment>;

    fn resolve(self, styles: StyleChain) -> Self::Output {
        self.fix(styles.resolve(TextElem::dir))
    }
}
```
> [src/layout/align.rs:270-276](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/align.rs#L270-L276) —— 与 `Em::resolve` 同理，对齐的最终落点也依赖样式链（文字方向）。

#### 4.5.4 代码实践

**实践目标**：观察 `start`/`end` 如何随文字方向翻转。

**操作步骤**：

1. 阅读 [`align.rs:319-329`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/align.rs#L319-L329) 的 `HAlignment::fix`。
2. 用一张二维表手推 `start` 在不同方向下的全局落点：
   - ltr（`is_positive = true`）：`start` → `FixedAlignment::Start`（全局左）
   - rtl（`is_positive = false`）：`start` → `FixedAlignment::End`（全局右）
3. 思考：为什么 `left`/`right` 不依赖 `dir`（永远是 `Start`/`End`），而 `start`/`end` 依赖？

**需要观察的现象**：第 2 步表格说明 `start` 是「语义上的起点」，rtl 下起点在右边。

**预期结果**：`start`/`end` 提供方向无关的语义对齐（适合多语言），`left`/`right` 提供固定的物理对齐。这正是 Typst 同时提供两套水平对齐的原因。

#### 4.5.5 小练习与答案

**练习 1**：`Alignment::fold`（[`align.rs:260-268`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/align.rs#L260-L268)）为什么要把 `(H, V-or-Both)` 合成 `Both`？这与 u4-l1 的 `Fold`（同类型合并）有何不同？

> **参考答案**：水平分量与垂直分量是「互补」的两个轴，合在一起才构成完整二维对齐。普通 `Fold`（如字号相乘）是把两个同维度的值合并成一个值；而 `Alignment::fold` 是把「分别落在两个轴上的设置」拼装成一个 `Both`，属于「按轴合并」。当内层只设了一个轴时，外层另一个轴的值应被保留，故 `fold` 把另一轴从 `outer` 取来填上。

**练习 2**：为什么垂直对齐 `VAlignment::fix`（[`align.rs:476-485`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/align.rs#L476-L485)）直接忽略传入的 `dir`？

> **参考答案**：文字方向（ltr/rtl）只影响水平阅读顺序，垂直方向上所有语言都是「从上到下」排版，不存在「从下到上」的文字方向。因此垂直对齐 `top`/`horizon`/`bottom` 是固定的，不需要随 `dir` 翻转，`fix` 里直接用常量映射。

---

## 5. 综合实践

把本讲各模块串起来，完成一个「度量追踪」小任务。

**任务**：给定用户输入 `#rect(width: 30% + 2cm, height: 4em, radius: 8pt)`，请按下面的清单，把每个数值从「用户写法」一路追踪到「布局时实际使用的原始单位」，并标注每一步发生在哪个文件。

**操作步骤**：

1. **`30% + 2cm`（width）**
   - 类型：`Rel<Length>`。
   - 构造：`Ratio(0.3) + Length{2cm}` → 经 [`rel.rs:284-290`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/rel.rs#L284-L290) 升级。
   - 解析：先 `relative_to(container_width)`（[`rel.rs:111-114`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/rel.rs#L111-L114)）代入容器宽，再 `resolve(styles)`（[`length.rs:266-272`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/length.rs#L266-L272)）解析可能的 em（本例 em=0）。
   - 最终得到一个 `Abs`（原始单位）。
2. **`4em`（height）**
   - 类型：`Length { abs: 0, em: Em(4.0) }`（裸 em 经 `From<Em>` 升级，见 [`length.rs:213-217`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/length.rs#L213-L217)）。
   - 解析：`Length::resolve` → `Em::resolve`（[`em.rs:157-163`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/em.rs#L157-L163)）→ 取 `TextElem::size` 字号，调 `Em::at(font_size)`（[`em.rs:61-64`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/em.rs#L61-L64)）。
   - 若字号 12pt，则 `4em = 48pt`。
3. **`8pt`（radius）**
   - 类型：`Length { abs: Abs::pt(8.0) = 8*127=1016 raw, em: 0 }`。
   - 解析：abs 部分原样保留，em=0 短路，结果就是 `1016 raw`。

**需要观察的现象**：三个数值走了三条不同的解析路径——百分比依赖容器宽度、em 依赖字号、纯 abs 无依赖。

**预期结果**：你能为任意一个 Typst 长度输入画出「它依赖哪些上下文（容器/字号/无）」的判断流程。这是阅读 u6-l2（Region/Frame）布局流程前必须具备的直觉。

## 6. 本讲小结

- `Abs` 是绝对长度，内部用整数倍尺度的「原始单位」存储，单位换算只在构造/读取边界发生，避免浮点误差。
- `Em` 是字体相对长度，无法单独解析成 `Abs`——它的 `resolve` 必须吃 `StyleChain`，从中查出 `TextElem::size` 字号；而字号本身又会被 `TextSize::fold` 当作线性函数相乘折叠。
- `Length = { abs, em }` 把绝对与字体相对两部分并列，可看作关于字号的一次函数；`Rel<T> = { rel: Ratio, abs: T }` 再叠加一个百分比部分。
- `20% + 5cm` 经 `Ratio + Length → Rel<Length>`（[`rel.rs:284-290`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/rel.rs#L284-L290)）构造，`relative_to(whole)` 代入容器宽，`resolve(styles)` 代入字号，两个维度独立。
- `Angle`/`Ratio`/`Fr` 是另外三类标量度量：`Angle` 用原始单位（rad）+ 三角函数；`Ratio::of` 算占整体比例；`Fr::share` 按份数瓜分剩余空间（与 `Ratio` 的「固定比例」语义不同）。
- `Dir` 给出方向轴与正负（ltr/ttb 为正），`Alignment` 用 `+` 合成二维对齐；`start`/`end` 经 `HAlignment::fix(dir)` 随文字方向翻转，而 `left`/`right` 是固定物理方向。
- 这些度量原语大多派生 `Copy`/`Hash`/`Resolve`/`Fold`，是整个 layout 模块复用的公共词汇；它们的「解析」普遍依赖 `StyleChain`，体现了 Typst「相对量延迟解析」的设计。

## 7. 下一步学习建议

本讲只讲了「度量原语」本身，接下来应该看它们如何被布局引擎消费：

1. **u6-l2（Region、Regions、Frame 与 Fragment）**：看 `Rel<Length>`/`Abs` 如何被喂进 `Regions`（尺寸、expand、backlog 分页模型），以及布局结果如何组成 `Frame` 帧树——这是度量原语的主要消费者。
2. **u6-l3（stack、grid、columns）**：看 `Fr` 如何在 grid 轨道解析中瓜分剩余空间、`Length`/`Rel` 如何决定轨道尺寸。
3. **回看 u4-l1（Styles、StyleChain 与 fold/resolve）**：本讲反复用到 `Resolve`/`Fold`/`StyleChain`，若对这些机制仍有疑问，建议对照 `TextSize::fold` 与 `Em::resolve` 再读一遍，把「样式折叠」与「度量解析」两条线在脑中对接。

> 建议继续精读的源码：[`src/layout/regions.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/regions.rs)、[`src/layout/size.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/size.rs)、[`src/layout/point.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/point.rs)，了解 `Abs` 如何被组装成 `Size`/`Point` 等二维复合几何类型。

# 可视化：颜色、描边、形状、曲线与图像

## 1. 本讲目标

`visualize` 模块是 Typst「把内容画出来」的入口。它定义了所有与绘图相关的用户可见类型与元素：颜色、渐变、平铺、描边、各种形状、自由曲线和图像。

本讲结束后，你应该能够：

- 说清 `Color` 是如何被注册成 Typst 一等类型的，以及它的构造器（`luma`/`oklab`/`oklch`/`rgb`/`cmyk`/`hsl`/`hsv`）从哪里来。
- 理解 `Paint` 如何把「纯色 / 渐变 / 平铺」三种填充统一成一种描述，以及 `Stroke` 如何用一层 `Smart<T>` 实现「字段可继承」。
- 看懂 `rect`/`square`/`ellipse`/`circle`/`polygon`/`curve` 这些形状元素是如何把用户配置归一化成内部 `Shape` + `Geometry` 的。
- 追踪 `ImageElem::decode` 如何根据「显式格式 → 文件扩展名 → 字节嗅探」三级策略，把同一份字节分发到 raster / svg / pdf 三条解码路径。
- 说明 `image` 文档里的裁剪（clipping）示例是借助 `block(clip: true)` 实现的**视觉裁剪**，而不是 `ImageElem` 自身的字段或解码行为。

贯穿全讲的一条主线（和前面所有讲义一致）：本 crate 只「定义类型/元素 + 归一化配置数据」，真正把这些数据栅格化成像素的算法住在行为 crate（`typst-layout` 等），运行期经 `Routines` 回调。

## 2. 前置知识

本讲依赖第 2 单元（值与类型）和第 3 单元（元素系统）。复习三个关键点即可：

- **`Value` 枚举与 `primitive!`**：Typst 运行时的万能值类型 `Value` 有若干「原始变体」（如 `Int`/`Float`/`Str`/`Color`）。`primitive!` 宏批量为这些原始类型生成 `Reflect`/`IntoValue`/`FromValue`，使其能与 `Value` 互转（见 u2-l1）。
- **`#[ty]` 与 `define_type`**：`#[ty(scope)]` 把一个 Rust 类型变成 Typst 的 `NativeType`（带名字、文档、构造器、子作用域），`define_type::<T>()` 把它注册进全局作用域（见 u2-l3）。
- **`#[elem]`、`Smart<T>`、`Fold`/`Resolve`**：元素用 `#[elem]` 宏生成；`Smart<T>` 表达「`auto`（继承/智能默认）或 `Custom(具体值)`」；`#[fold]` 字段沿样式链折叠，`Resolve` 把相对值解析成绝对值（见 u3-l3、u4-l1）。

另外补充两个绘图术语：

- **paint（涂料）**：决定「一块区域用什么填」的描述，可以是纯色、渐变或平铺图案。
- **stroke（描边）**：决定「一条线/一个轮廓怎么画」的描述，包含涂料、粗细、端点、拐角、虚线、斜接限制。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `src/visualize/` 下：

| 文件 | 作用 |
| --- | --- |
| [mod.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/mod.rs) | 声明子模块，`define()` 把 visualize 的全部类型与元素注册到全局作用域 |
| [color.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/color.rs) | `Color` 类型、各色彩空间（Luma/Oklab/Oklch/Rgb/LinearRgb/Cmyk/Hsl/Hsv）、构造器与变换方法 |
| [gradient.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/gradient.rs) | `Gradient`（线性/径向/圆锥渐变） |
| [tiling.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/tiling.rs) | `Tiling`（重复平铺图案） |
| [paint.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/paint.rs) | `Paint` 枚举，统一纯色/渐变/平铺 |
| [stroke.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/stroke.rs) | `Stroke`、`FixedStroke`、`LineCap`/`LineJoin`/`DashPattern` |
| [shape.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/shape.rs) | `RectElem`/`SquareElem`/`EllipseElem`/`CircleElem` 与内部 `Shape`/`Geometry`/`FillRule` |
| [polygon.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/polygon.rs) | `PolygonElem` |
| [curve.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/curve.rs) | `CurveElem`（自由曲线元素）与内部 `Curve`/`CurveItem` |
| [image/mod.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/image/mod.rs) | `ImageElem`、`Image`/`ImageKind`/`ImageFormat`、格式嗅探与解码分发 |

`define()` 一眼看清本模块注册了哪些定义（共 4 个类型 + 8 个元素）：

[visualize/mod.rs:28-43](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/mod.rs#L28-L43) 把 `Color`/`Gradient`/`Tiling`/`Stroke` 注册为类型，把 `ImageElem`/`LineElem`/`RectElem`/`SquareElem`/`EllipseElem`/`CircleElem`/`PolygonElem`/`CurveElem` 注册为元素。注意这里没有把「形状内部的 `Shape`/`Geometry`」注册给用户——它们是只供编译器内部使用的归一化数据结构。

## 4. 核心概念与源码讲解

### 4.1 Color、Gradient、Tiling：三种「涂料源」的类型注册

#### 4.1.1 概念说明

`Color`、`Gradient`、`Tiling` 三者有一个共同点：它们既是**用户可直接书写的值**（`red`、`gradient.linear(..)`、`tiling(..)`），又是**带构造器和方法的类型**（`color.rgb`、`gradient.linear`、`tiling`）。

这其实是两件事，Typst 用两套机制分别实现：

1. **作为 `Value` 的原始变体**：`Value` 枚举里直接有 `Color(Color)`、`Gradient(Gradient)`、`Tiling(Tiling)` 三个变体。由 `primitive!` 宏生成它们与 `Value` 的互转。
2. **作为 `NativeType`**：用 `#[ty(scope, cast)]` 标注，`define_type::<T>()` 注册，从而拥有名字、文档、构造器与子作用域（`color.rgb`、`gradient.linear` 等都挂在类型的 scope 上）。

`Color` 内部并不直接存某个色彩空间的四个数，而是包了一层枚举：`Process`（普通「工艺色」，可属于任意色彩空间）或 `Spot`（印刷「专色」）。日常用到的 `rgb`/`oklch`/`cmyk` 等都属于 `Process`。

`Gradient` 同理是三变体枚举：`Linear`/`Radial`/`Conic`，分别用 `Arc` 引用计数持有具体参数。`Tiling` 则是单个 `Arc<TilingInner>` 的新类型。

#### 4.1.2 核心流程

以 `#rect(fill: oklch(60%, 0.2, 30))` 为例，颜色的「诞生」流程：

1. 求值器遇到 `oklch(...)`，查到它指向 `Color::oklch` 这个原生函数（`Color` 类型 scope 里的 `#[func]`）。
2. `oklch` 从 `Args` 取出 lightness/chroma/hue/alpha，构造一个 `Oklch` 色彩空间对象，包成 `Color::Process(ProcessColor::Oklch(..))`。
3. 该 `Color` 被装进 `Value::Color(..)`，赋给 `rect` 的 `fill` 字段。
4. 排版时，行为 crate 拿到这个 `Color`，按目标色彩空间做转换与绘制。

关键设计：**`Color` 不绑定固定色彩空间**。同一个颜色可以 `.to_rgb()`、`.to_oklab()`、`.to_cmyk()` 来回换算，换算由各色彩空间结构体上的转换函数完成，`Color` 只是一个带标签的容器。

颜色混合 `mix` 是把多个 `WeightedColor`（颜色 + 权重）在指定色彩空间里做加权平均，权重归一化后逐分量相加。

#### 4.1.3 源码精读

**Color 的双重身份（1）：`Value` 原始变体。** `Value` 枚举里直接有颜色相关变体：

[value.rs:49-51](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/value.rs#L49-L51) 定义了 `Color(Color)` 与 `Gradient(Gradient)` 变体（`Tiling` 同理）。它们与 `Int`/`Float`/`Str` 并列，说明颜色在 Typst 里是「一等原始值」。

[value.rs:631-633](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/value.rs#L631-L633) 用 `primitive!` 为三者生成 `Reflect`/`IntoValue`/`FromValue`：

```rust
primitive! { Color: "color", Color }
primitive! { Gradient: "gradient", Gradient }
primitive! { Tiling: "tiling", Tiling }
```

这一行决定了 `fill: red` 里那个 `red` 值的 `Type::of::<Color>()` 会得到类型名 `"color"`，也决定了它参与其它类型的 `cast!`（例如后面 `Paint` 的 `color: Color => ...` 分支）。

**Color 的双重身份（2）：`NativeType`。** 在 color.rs 里，`Color` 同时被标注为 `#[ty]`：

[color.rs:280-287](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/color.rs#L280-L287)

```rust
#[ty(scope, cast, since = "forever")]
#[derive(Clone, PartialEq, Eq, Hash)]
pub enum Color {
    /// A process color.
    Process(ProcessColor),
    /// A spot color.
    Spot(SpotColor),
}
```

注意这里的 `cast` 标志。回顾 u2-l3：`#[ty]` 宏默认会为一个类型生成一个平凡的 `cast! { type T }`；但当带上 `cast` 标志时，宏**不再生成**它——因为 `primitive!` 已经提供了 `Reflect`/`FromValue`/`IntoValue`，再生成就会重复。所以 `cast` 标志的含义是「转换实现已在别处（`primitive!`）提供，请勿再生成」。这正是 `Color`/`Gradient`/`Tiling` 三者一致的写法。

`#[ty(scope)]` 让 `Color` 拥有一个子作用域，`define_type::<Color>()` 把它注册为 `color` 类型（见 [scope.rs:152-155](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/scope.rs#L152-L155) 的 `define_type`，它用 `short_name()` 作键）。于是 `color.rgb`、`color.components`、`color.lighten` 都可访问。

**构造器都是 `#[func]`。** 以 `luma` 为例：

[color.rs:340-366](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/color.rs#L340-L366) 定义了 `pub fn luma(..)`。它有两种调用形态：传一个现成 `Color`（转灰度），或传 lightness（可选 alpha）直接构造 `Luma`。参数都用 `#[external]` 标注——回顾 u3-l4，`#[external]` 表示「这个参数名只出现在文档里，函数体内改用手动 `args.find`/`args.expect` 取值」，因为颜色构造器要支持「单参 hex 串 / 单参颜色 / 多个分量」等多种重载，必须手工分派。`rgb` 是最典型的多形态构造器：

[color.rs:606-623](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/color.rs#L606-L623) 依次尝试：先找字符串（`rgb("#239dad")` → `Self::from_str`），再找颜色（转 RGB），最后才按 r/g/b/a 分量构造。`cmyk`、`oklab`、`oklch`、`hsl`、`hsv`、`linear_rgb` 结构完全相同。

hex 字符串解析靠 `FromStr`：

[color.rs:1409-1415](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/color.rs#L1409-L1415) 把字符串解析委托给 `ProcessColor::from_str`，再 `.into()` 包成 `Color`。所以 `rgb("#ff0000")` 与 `rgb(0xff, 0x00, 0x00)` 走的是两条不同代码路径，但最终都得到 `Color::Process(ProcessColor::Rgb(..))`。

**命名颜色与构造器的「提升为全局」。** `Color` 的 scope 里定义了一组常量（`BLACK`/`WHITE`/`RED`/`BLUE`/…）：

[color.rs:289-322](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/color.rs#L289-L322) 把这些颜色写成 `pub const RED: Self = Self::Process(ProcessColor::Rgb(Rgb::new(..)))`。但它们能被写成裸的 `red`/`blue`，是因为装配期在 `global()` 里手动 `define` 了：

[lib.rs:364-381](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L364-L381)（节选）里既有 `global.define("blue", Color::BLUE)`、`global.define("red", Color::RED)`（提升命名颜色），也有 `global.define("luma", Color::luma_data())`、`global.define("oklab", Color::oklab_data())`、`global.define("oklch", Color::oklch_data())`、`global.define("rgb", Color::rgb_data())`、`global.define("cmyk", Color::cmyk_data())`（提升常用构造器）。`rgb_data()` 是 `#[func]` 宏为每个函数生成的静态 `NativeFuncData` 访问器（回顾 u3-l4）。所以你可以写 `rgb(..)` 也可以写 `color.rgb(..)`，两者指向同一份函数数据。

> 注意：只有 `luma`/`oklab`/`oklch`/`rgb`/`cmyk` 五个被提升为裸全局；`linear_rgb`/`hsl`/`hsv` 等只能通过 `color.hsl(..)` 访问。

**Gradient / Tiling 同构。**

[gradient.rs:189-195](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/gradient.rs#L189-L195) 定义 `Gradient` 为 `Linear`/`Radial`/`Conic` 三变体（各持 `Arc<..>`），同样标 `#[ty(scope, cast)]`，同样在 [value.rs:632](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/value.rs#L632) 用 `primitive!` 注册为原始变体。三个构造器 `gradient.linear`/`gradient.radial`/`gradient.conic` 是 `#[func]`。

[tiling.rs:57-59](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/tiling.rs#L57-L59) 定义 `Tiling(Arc<TilingInner>)`，写法完全平行。

#### 4.1.4 代码实践

**实践目标**：验证「Color 既是原始值又是 NativeType」这一双重身份，并追踪一个 hex 串的解析路径。

**操作步骤**（源码阅读型）：

1. 打开 [value.rs:631](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/value.rs#L631)，确认 `Color`/`Gradient`/`Tiling` 都在 `primitive!` 列表里。
2. 打开 [color.rs:280](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/color.rs#L280)，确认 `Color` 标了 `#[ty(scope, cast)]`；再打开 [mod.rs:30](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/mod.rs#L30) 确认它被 `define_type::<Color>()` 注册。
3. 追踪 `rgb("#239dad")`：从 [color.rs:607-608](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/color.rs#L607-L608) 的 `args.find::<Spanned<Str>>()` 分支，进入 [color.rs:1409](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/color.rs#L1409) 的 `FromStr::from_str`，最终落到 `ProcessColor::from_str`。
4. 在 [lib.rs:377-381](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L377-L381) 找到 `luma`/`oklab`/`oklch`/`rgb`/`cmyk` 五个构造器被提升为裸全局的代码。

**需要观察的现象**：`rgb` 的 `#[func]` 同时生成了「挂在 `color` scope 上」和「可被 `Color::rgb_data()` 取出再 define 到全局」两份访问入口，但背后是同一份 `NativeFuncData`。

**预期结果**：你能向别人解释「为什么 `fill: red` 与 `fill: color.rgb(100%, 0%, 0%)` 都合法，且前者来自一个全局常量、后者来自一个挂载在类型 scope 上又被提升的函数」。

> 待本地验证：若你本地能编译 Typst，可在 REPL 里试 `#repr(red)`、`#repr(rgb("#ff0000"))`、`#type(red)`，观察输出分别是 `rgb(100%, 21.2%, 21.2%)`、`rgb(100%, 0%, 0%)`、`color`，印证上述路径。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Color` 的 `#[ty]` 标注要带 `cast` 标志，而去掉它会编译失败或行为变化？

**答案**：`cast` 标志告诉 `#[ty]` 宏「不要再生成平凡的 `cast! { type Color }`」，因为 `primitive!`（value.rs:631）已经为 `Color` 生成了 `Reflect`/`IntoValue`/`FromValue`。若宏再生成一份，会与 `primitive!` 的实现重复冲突。

**练习 2**：`hsl(30deg, 50%, 60%)` 和 `color.hsl(30deg, 50%, 60%)` 有区别吗？为什么 `hsl` 不像 `rgb` 那样能裸写？

**答案**：无区别，指向同一个 `Color::hsl` 函数。区别在「作用域可见性」：`luma`/`oklab`/`oklch`/`rgb`/`cmyk` 五个在 [lib.rs:377-381](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L377-L381) 被额外 `define` 为裸全局，而 `hsl`/`hsv`/`linear_rgb` 没有被提升，只能经 `color.` 前缀访问。

**练习 3**：`Color` 为什么不直接存 `Rgb` 四个分量，而要做成 `Process`/`Spot` 的枚举？

**答案**：为了「色彩空间无关」。同一个颜色可在 8 种色彩空间间换算（`to_rgb`/`to_oklab`/`to_cmyk`/…），若硬绑死 Rgb 会丢失原始空间信息并引入不必要的精度损失；`Spot` 则是为了支持印刷专色（一种独立于工艺色的颜色种类）。

---

### 4.2 Paint 与 Stroke：填充与描边的统一描述

#### 4.2.1 概念说明

有了 `Color`/`Gradient`/`Tiling` 三种「涂料源」，还需要一个统一类型来描述「填充或描边用什么」。这就是 `Paint`：一个三变体枚举，分别包三者。`fill` 字段（如 `rect` 的 `fill`）的类型是 `Option<Paint>`，因此 `rect(fill: blue)`、`rect(fill: gradient.linear(..))`、`rect(fill: tiling(..))` 都合法。

`Stroke` 更复杂。它要描述六个维度：涂料、粗细、端点（cap）、拐角（join）、虚线（dash）、斜接限制（miter limit）。难点在于这些维度都「可继承」——你可以只写 `2pt + red`，其余维度沿用默认。Typst 用 `Smart<T>` 包装每个字段：`Smart::Auto` 表示「未指定、继承默认」，`Smart::Custom(v)` 表示「显式给定」。

还有两个相关类型：

- `Stroke<T>`：用户-facing、带泛型的描边（长度类型参数 `T` 通常是 `Length`，resolve 后是 `Abs`）。
- `FixedStroke`：六维全部填满、不再有 `Auto` 的描边，是真正交给绘制层的类型。

#### 4.2.2 核心流程

以 `#rect(stroke: 2pt + red)` 为例：

1. `2pt + red` 被 `Stroke` 的 `cast!` 解析：`+` 运算在 `Stroke` 上定义（把两边的 paint/thickness 合并），等价于一个 `Stroke { paint: red, thickness: 2pt, ..Auto }`。
2. 该 `Stroke` 赋给 `rect` 的 `stroke` 字段（一个 `#[fold]` 字段，沿样式链折叠）。
3. 排版前，调用 `Stroke::resolve(styles)` 把相对长度解析成 `Abs`，得到 `Stroke<Abs>`。
4. 行为 crate 调用 `stroke.unwrap_or_default()`，用默认值（黑、1pt、butt、miter、无虚线、miter-limit 4.0）填满所有 `Auto`，得到 `FixedStroke`，交给栅格化器。

`Stroke` 的 `Fold` 语义是「逐字段 `.or()` 覆盖」：内层样式只要某字段是 `Custom`，就覆盖外层；`Auto` 则沿用外层。

#### 4.2.3 源码精读

**Paint 统一三种涂料。**

[paint.rs:9-17](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/paint.rs#L9-L17)

```rust
pub enum Paint {
    /// A solid color.
    Solid(Color),
    /// A gradient.
    Gradient(Gradient),
    /// A tiling.
    Tiling(Tiling),
}
```

它的 `cast!` 让任何能转成 `Color`/`Gradient`/`Tiling` 的值自动成为 `Paint`：

[paint.rs:92-102](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/paint.rs#L92-L102) 三个输入分支分别对应三种涂料源，输出分支则把 `Paint` 还原为它内部的那个具体值（而不是一个新对象）。此外 [paint.rs:80-84](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/paint.rs#L80-L84) 提供了 `From<T: Into<Color>> for Paint`，使得任意「能变成颜色」的东西（包括命名常量 `red`）都能 `.into()` 成 `Paint`。

> 一个有意思的方法是 [paint.rs:41-51](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/paint.rs#L41-L51) 的 `as_decoration()`：当 paint 被用作文字装饰（下划线等）的涂料时，渐变/平铺会被强制改成 `RelativeTo::Parent`——因为装饰是按字形绘制的，必须把坐标系锚到父容器而非整个文本块。

**Stroke 的六维 + 全 `Smart`。**

[stroke.rs:52-67](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/stroke.rs#L52-L67)

```rust
#[ty(scope, cast, since = "forever")]
pub struct Stroke<T: Numeric = Length> {
    pub paint: Smart<Paint>,
    pub thickness: Smart<T>,
    pub cap: Smart<LineCap>,
    pub join: Smart<LineJoin>,
    pub dash: Smart<Option<DashPattern<T>>>,
    pub miter_limit: Smart<Ratio>,
}
```

六个字段清一色 `Smart<..>`，这就是「字段可继承」的来源。注意 `Stroke` 自己也标了 `#[ty(scope, cast)]`，因此它既是 `stroke(...)` 构造器函数，又是一个可被 `cast!` 接受的类型（`rect(stroke: ...)` 接受的就是它）。

**`cast!` 的多形态输入。** `Stroke` 的 `cast!` 是理解「`2pt + red` 为何能用」的关键：

[stroke.rs:397-439](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/stroke.rs#L397-L439)（节选）

```rust
cast! {
    type Stroke,
    thickness: Length => Self { thickness: Smart::Custom(thickness), ..Default::default() },
    color: Color => Self { paint: Smart::Custom(color.into()), ..Default::default() },
    gradient: Gradient => Self { paint: Smart::Custom(gradient.into()), ..Default::default() },
    tiling: Tiling => Self { paint: Smart::Custom(tiling.into()), ..Default::default() },
    mut dict: Dict => { /* 逐字段 take，缺失即 Auto */ },
}
```

四个单值分支解释了文档里那句「你可以在 stroke 位置传 length、color、gradient 或 tiling」：传 `2pt` 只设 thickness、传 `red` 只设 paint，其余 `..Default::default()` 全是 `Auto`。`2pt + red` 里的 `+` 则是 `Stroke` 上的 `Add` 实现（把两个部分 Stroke 合并）。字典分支允许 `(paint: blue, thickness: 4pt, cap: "round")` 这种全控写法。

**Fold / Resolve / unwrap。**

- [stroke.rs:369-380](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/stroke.rs#L369-L380) `Fold`：逐字段 `self.X.or(outer.X)`，内层 `Custom` 覆盖外层。
- [stroke.rs:382-395](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/stroke.rs#L382-L395) `Resolve`：只把 thickness/dash 里的相对长度解析成 `Abs`，paint/cap/join 不依赖样式原样保留，得到 `Stroke<Abs>`。
- [stroke.rs:276-304](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/stroke.rs#L276-L304) `unwrap_or(default)`：用一份 `FixedStroke` 作兜底，把所有 `Auto` 替换成具体默认值，产出真正可绘制的 `FixedStroke`。

**FixedStroke 的默认值。**

[stroke.rs:652-663](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/stroke.rs#L652-L663) 给出全部默认：paint=黑、thickness=1pt、cap=Butt、join=Miter、dash=None、miter_limit=4.0。注释提到 4.0 是「与 tiny-skia 一致」（tiny-skia 是 Typst 用的 2D 栅格化库）。`LineCap`（butt/round/square）与 `LineJoin`（miter/round/bevel）定义在 [stroke.rs:446-478](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/stroke.rs#L446-L478)，虚线 `DashPattern` 在 [stroke.rs:490-497](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/stroke.rs#L490-L497)，预定义虚线（`"dashed"`/`"dotted"`/…）作为字符串字面量分支写在 [stroke.rs:534-570](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/stroke.rs#L534-L570)。

#### 4.2.4 代码实践

**实践目标**：通过 `Stroke::repr` 的输出反推「字段继承」机制。

**操作步骤**（源码阅读型 + 本地可选）：

1. 阅读 [stroke.rs:306-367](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/stroke.rs#L306-L367) 的 `Repr` 实现。注意它的两条分支：当 cap/join/dash/miter_limit 全是 `Auto` 时，走「简写形式」（`2pt + red` 或单值）；只要任一非 Auto，就走「字典形式」`(paint: .., thickness: ..)`。
2. 据此预测下列四式的 `repr` 输出：
   - `(2pt + red)`
   - `(red)`
   - `stroke(cap: "round")`
   - `stroke(paint: blue, thickness: 4pt, dash: "dashed")`
3. 若本地可运行 Typst，用 `#repr(..)` 验证你的预测。

**预期结果**：前两个走简写（输出 `2pt + red`、`red`）；后两个因含非 Auto 的 cap/dash，走字典形式（输出 `(cap: "round")`、`(paint: rgb(..), thickness: 4pt, dash: (array: (3pt, 3pt), phase: 0pt))`）。

> 待本地验证：repr 的精确字符串以本地 Typst 运行结果为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Stroke` 的字段全用 `Smart<T>` 而不是直接 `T` + `Option`？

**答案**：`Smart<T>` 区分了三种状态语义——`Auto`（未指定、继承默认）、`Custom(Some)`、`Custom(None)`（对 dash 这种本身就是 `Option` 的字段尤其重要，要能区分「未指定」与「显式设为无虚线」）。`Fold` 用 `.or()` 在 `Auto` 时沿用外层，从而支持「只改一个维度、其余继承」的用法。

**练习 2**：`Stroke` 和 `FixedStroke` 的区别是什么？谁出现在用户层，谁出现在绘制层？

**答案**：`Stroke<T>` 是用户层类型，六个字段都可能是 `Auto`，泛型 `T` 在 resolve 前是 `Length`。`FixedStroke` 是绘制层类型，字段全部填满（无 `Auto`）、thickness 为 `Abs`，由 `Stroke::unwrap_or_default` 产出。行为 crate 只接收 `FixedStroke`。

---

### 4.3 形状元素与内部 Shape / Geometry

#### 4.3.1 概念说明

形状元素是用户直接书写的 `#rect(..)`、`#square(..)`、`#ellipse(..)`、`#circle(..)`、`#polygon(..)`、`#line(..)`、`#curve(..)`。它们都是 `#[elem]` 元素，字段高度相似：`width`/`height`/`fill`/`stroke`/`inset`/`outset`/`body`。

但元素本身只是「用户配置的载体」。真正描述「一个可填充/可描边的几何图形」的是内部类型 `Shape`，它由三部分组成：

- `geometry: Geometry` —— 图形几何（一条直线、一个矩形、或一条 `Curve`）。
- `fill: Option<Paint>` —— 填充涂料。
- `stroke: Option<FixedStroke>` —— 描边（必须是已 `unwrap` 的 `FixedStroke`）。

`Geometry` 三变体：`Line(Point)`、`Rect(Size)`、`Curve(Curve)`。注意椭圆/圆/多边形并不在 `Geometry` 变体里——它们在元素层会被转换成 `Curve`（贝塞尔曲线），再以 `Geometry::Curve` 形式参与绘制。

#### 4.3.2 核心流程

以 `#rect(width: 35%, fill: blue, stroke: red)` 为例：

1. `RectElem` 的字段保存用户配置（`width`/`height`/`fill`/`stroke`/`radius`/`inset`/`outset`/`body`）。
2. 在 realize/layout 阶段，行为 crate 读取这些字段，构造一个 `Geometry::Rect(size)`（矩形是少数直接走 `Rect` 变体的形状）。
3. 调用 `Geometry::filled_and_stroked(fill, stroke)` 把它打包成 `Shape { geometry, fill: Some(blue.into()), stroke: Some(red_stroke), .. }`。
4. 该 `Shape` 被画进 `Frame`（作为 `FrameItem::Shape`，见 u6-l2）。

`Shape` 的设计是「几何与涂料解耦」：同一份 `Geometry` 可 `.filled()` 只填、`.stroked()` 只描边、`.filled_and_stroked()` 两者都做。这让形状元素与曲线元素（`CurveElem`）共享同一条绘制通路。

#### 4.3.3 源码精读

**形状元素的字段同构。** 以 `RectElem` 为代表：

[shape.rs:19-128](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/shape.rs#L19-L128)（节选关键字段）

```rust
#[elem(title = "Rectangle", since = "forever")]
pub struct RectElem {
    pub width: Smart<Rel<Length>>,
    pub height: Sizing,
    pub fill: Option<Paint>,
    #[fold] pub stroke: Smart<Sides<Option<Option<Stroke>>>>,
    #[fold] pub radius: Corners<Option<Rel<Length>>>,
    #[fold] #[default(Sides::splat(Some(Abs::pt(5.0).into())))]
    pub inset: Sides<Option<Rel<Length>>>,
    #[fold] pub outset: Sides<Option<Rel<Length>>>,
    #[positional] pub body: Option<Content>,
}
```

几个要点：

- `fill: Option<Paint>`——直接接受 `Paint`，所以 `fill: blue`/`fill: gradient.linear(..)` 都行。
- `stroke` 的类型 `Smart<Sides<Option<Option<Stroke>>>>` 看起来很吓人，其实层层拆开就是「可选地、按四边（`Sides`）、每边可选地给一个描边」。`Sides`/`Corners` 用 `rest`/`x`/`y`/`left`/`top`/... 键解析（回顾 u6-l4 的 `Margin` 同构），支持字典写法。`#[fold]` 让多次 `set rect(stroke: ..)` 沿样式链折叠。
- `inset`（内边距）默认 5pt，`outset`（外延）默认零。
- `body` 是 `#[positional]`，可放内容；省略时矩形取默认尺寸（至多 45pt × 30pt）。

**square / circle 的 `#[parse]` 互斥参数。** `square` 的 `size` 与 `width`/`height` 互斥，`circle` 的 `radius` 与 `width`/`height` 互斥。这种「多个互斥参数归一为一个字段」的需求用 `#[parse]` 实现：

[shape.rs:154-171](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/shape.rs#L154-L171)（`SquareElem` 的 width/height parse）

```rust
#[parse(
    let size = args.named::<Smart<Length>>("size")?.map(|s| s.map(Rel::from));
    match size {
        None => args.named("width")?,
        size => size,
    }
)]
pub width: Smart<Rel<Length>>,
```

回顾 u3-l3：`#[parse]` 覆盖字段的默认参数解析。这里先尝试取 `size`，若给了就转成 `width`，否则才取 `width`——把 `size` 和 `width` 两个用户参数归一到 `width` 一个字段上。`circle` 的 `radius` 同理（[shape.rs:283-291](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/shape.rs#L283-L291)，把 `radius` 乘 2 转成 `width`）。

**内部 Shape / Geometry / FillRule。**

[shape.rs:333-343](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/shape.rs#L333-L343)

```rust
pub struct Shape {
    pub geometry: Geometry,
    pub fill: Option<Paint>,
    pub fill_rule: FillRule,
    pub stroke: Option<FixedStroke>,
}
```

`Geometry` 三变体见 [shape.rs:364-373](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/shape.rs#L364-L373)：`Line`/`Rect`/`Curve`。注意 `stroke` 是 `FixedStroke`（已 resolve、已 unwrap），不是用户层的 `Stroke`。`Geometry` 上提供 `.filled()`/`.stroked()`/`.filled_and_stroked()` 三个构造 `Shape` 的便捷方法（[shape.rs:376-408](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/shape.rs#L376-L408)）。

`FillRule`（[shape.rs:355-362](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/shape.rs#L355-L362)）决定「曲线自交时哪部分算内部」：`NonZero`（默认，按有向边交叉数之和）或 `EvenOdd`（按交叉数奇偶）。它对 `curve` 和 `polygon` 这种可能自交的图形有意义。

**曲线元素 CurveElem 与内部 Curve。** `CurveElem`（[curve.rs:42-93](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/curve.rs#L42-L93)）用 `#[variadic] components: Vec<CurveComponent>` 接收一串 `curve.move`/`curve.line`/`curve.cubic`/`curve.close` 子元素，内部归一成 [curve.rs:384](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/curve.rs#L384) 的 `Curve(pub Vec<CurveItem>)`——一组路径段（move/line/quad/cubic/close）。这条 `Curve` 既是 `Geometry::Curve` 的载荷，也是椭圆/圆/多边形被转换后的共同归宿。

#### 4.3.4 代码实践

**实践目标**：理解「形状元素的字段配置 → 内部 `Shape`/`Geometry`」的归一化，以及 `#[parse]` 互斥参数。

**操作步骤**（源码阅读型）：

1. 对比 `RectElem`（[shape.rs:19](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/shape.rs#L19)）与 `EllipseElem`（[shape.rs:221](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/shape.rs#L221)）的字段，找出它们的差异（提示：椭圆没有 `radius` 圆角，且 `stroke` 类型更简单——`Smart<Option<Stroke>>` 而非按四边）。
2. 阅读 `SquareElem` 与 `CircleElem` 的 `#[parse]` 块，画一张「用户参数 → 字段」的归一表：
   - `square(size: 40pt)` → `width` 来自 `size`
   - `square(width: 40pt)` → `width` 来自 `width`
   - `circle(radius: 25pt)` → `width = 2 * radius`
3. 在 [shape.rs:376-408](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/shape.rs#L376-L408) 确认 `Geometry::filled_and_stroked` 总是把 `fill_rule` 设为 `FillRule::default()`（即 `NonZero`）——所以 `fill-rule` 只有在元素显式提供时才生效。

**需要观察的现象**：`RectElem.stroke` 的嵌套 `Option`（`Sides<Option<Option<Stroke>>>`）能在「禁用某边描边」与「某边用默认描边」之间区分；而 `EllipseElem.stroke` 只是 `Smart<Option<Stroke>>`，因为椭圆没有「按边」概念。

**预期结果**：你能说清为何矩形可以有圆角和四边不同描边，而椭圆不能。

> 待本地验证：可尝试 `#rect(stroke: (left: red, top: 2pt))` 与 `#ellipse(stroke: (left: red))`，后者应不被支持。

#### 4.3.5 小练习与答案

**练习 1**：椭圆和圆为什么没有出现在 `Geometry` 枚举的变体里？

**答案**：`Geometry` 只有 `Line`/`Rect`/`Curve`。椭圆和圆在绘制时被近似成贝塞尔曲线（`Curve`），再以 `Geometry::Curve` 参与。矩形是少数能直接用 `Rect` 变体的形状。

**练习 2**：`Shape.fill_rule` 默认是 `NonZero`。这个字段对 `rect` 有意义吗？

**答案**：对 `rect`（凸的、不自交）几乎无意义，因为 `NonZero` 与 `EvenOdd` 对简单凸多边形给出相同结果。它主要影响自交图形（`polygon` 的星形、自交 `curve`），在那里两种规则会画出不同的「内部」。

---

### 4.4 ImageElem：按格式分发解码（含 clipping 说明）

#### 4.4.1 概念说明

`ImageElem` 是图像元素。它要处理两类输入（文件路径、原始字节）、多种格式（png/jpg/gif/webp/svg/pdf/原始像素），以及尺寸/适配/无障碍/ICC 等配置。

核心机制是**格式嗅探 + 解码分发**：先确定「这份字节是什么格式」，再据此走 raster / svg / pdf 三条解码路径之一。格式的确定有三道防线：

1. 用户显式指定的 `format`；
2. 文件扩展名；
3. 字节内容嗅探（magic bytes）。

解码产物统一为 `Image`（一个 `Arc<LazyHash<ImageInner>>`，廉价克隆与哈希），内部 `kind` 是 `ImageKind` 三变体（`Raster`/`Svg`/`Pdf`）。

**关于 clipping（裁剪）**：`image` 文档里有一个用 `block(clip: true)` 裁剪图像的示例，常被误以为是 `ImageElem` 的能力。实际上裁剪是 `block`/`box` 元素的 `clip` 字段实现的**视觉裁剪**，与 `ImageElem` 的字段或解码机制无关——`ImageElem` 没有 `clip` 字段。

#### 4.4.2 核心流程

以 `#image("photo.jpg", width: 80%)` 为例：

1. `ImageElem` 的 `source` 字段在 `#[parse]` 里调用 `source.load(engine.world)` 读取字节（回顾 u11-l1 的 `DataSource`/`Load`），得到 `Derived<DataSource, Loaded>`。
2. realize 阶段调用 [`Packed<ImageElem>::decode`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/image/mod.rs#L240)。
3. `decode` 先 `determine_format` 确定格式：
   - 若用户给了 `format`，直接用；
   - 否则若是路径源，按扩展名猜（`.jpg`→Jpg、`.svg`→Svg、`.pdf`→Pdf …）；
   - 否则用字节嗅探（`ImageFormat::detect`）。
4. 按 `ImageFormat` 分支走三条解码路径之一，产出 `ImageKind`：
   - `Raster(format)` → `RasterImage::new(bytes, format, icc)`；
   - `Vector(Svg)` → `SvgImage::with_fonts_images(..)`（并检查 foreignObject 警告）；
   - `Vector(Pdf)` → `PdfDocument::new(bytes)` → `PdfImage::new(doc, page_idx)`。
5. `Image::new(kind, alt, scaling)` 把它包成 `Arc<LazyHash<ImageInner>>`（`new_impl` 标了 `#[comemo::memoize]`，相同输入复用同一个 `Arc`）。

#### 4.4.3 源码精读

**ImageElem 的字段与能力。**

[image/mod.rs:77-79](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/image/mod.rs#L77-L79)

```rust
#[elem(since = "forever", Locatable, Tagged, Synthesize, LocalName, Figurable)]
pub struct ImageElem {
```

它带了一串能力：`Locatable`/`Tagged`（可被内省，回顾 u9-l1）、`Synthesize`（合成步骤，用于写入 locale）、`LocalName`/`Figurable`（可放进 `figure` 作为图，回顾 u8-l3）。注意**没有**任何与裁剪相关的能力或字段。

**source：在 `#[parse]` 里就把字节读好。**

[image/mod.rs:95-101](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/image/mod.rs#L95-L101)

```rust
#[required]
#[parse(
    let source = args.expect::<Spanned<DataSource>>("source")?;
    let loaded = source.load(engine.world)?;
    Derived::new(source.v, loaded)
)]
pub source: Derived<DataSource, Loaded>,
```

`Derived<A, B>` 同时保存「原始输入（路径或字节）」与「派生结果（已加载字节）」，这样 `determine_format` 之后还能回头取文件扩展名。ICC profile 字段 `icc` 用了同样的 `#[parse]` 模式（[image/mod.rs:215-223](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/image/mod.rs#L215-L223)）。

**decode：格式分发的主干。**

[image/mod.rs:240-352](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/image/mod.rs#L240-L352) 的核心是一个 `match format`：

```rust
let kind = match format {
    ImageFormat::Raster(format) => ImageKind::Raster(
        RasterImage::new(loaded.data.clone(), format, icc_opt).at(span)?,
    ),
    ImageFormat::Vector(VectorFormat::Svg) => {
        // 检查 foreignObject 并警告
        ImageKind::Svg(SvgImage::with_fonts_images(..).within(loaded)?)
    }
    ImageFormat::Vector(VectorFormat::Pdf) => {
        let document = PdfDocument::new(loaded.data.clone())?; // 处理加密/损坏
        // ... 页号检查 ...
        ImageKind::Pdf(PdfImage::new(document, page_idx))
    }
};
```

三条路径细节：

- **Raster**：交给 `raster.rs` 的 `RasterImage::new`（用 `image` crate 解码像素），可选挂 ICC profile。
- **SVG**：交给 `svg.rs` 的 `SvgImage::with_fonts_images`（用 usvg 解析，并注入当前字体族以便 SVG 内的 `<text>` 能用 Typst 字体）。解码前先用 `memchr` 扫描 `<foreignObject`，若命中则发出警告（[image/mod.rs:259-268](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/image/mod.rs#L259-L268)），因为含 foreignObject 的 SVG 在 Typst 里可能渲染不正确。
- **PDF**：交给 `pdf.rs` 的 `PdfDocument::new`（hayro 库），处理加密/损坏错误（[image/mod.rs:288-314](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/image/mod.rs#L288-L314)），并按 `page` 字段取指定页。

**determine_format：三级策略。**

[image/mod.rs:356-370](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/image/mod.rs#L356-L370) 实现三级回退：

```rust
fn determine_format(&self, styles: StyleChain) -> StrResult<ImageFormat> {
    if let Smart::Custom(v) = self.format.get(styles) { return Ok(v); }      // 1. 显式
    if let DataSource::Path(path) = source
        && let Some(format) = determine_format_from_path(id.vpath()) { ... } // 2. 扩展名
    Ok(ImageFormat::detect(&loaded.data).ok_or("unknown image format")?)    // 3. 嗅探
}
```

扩展名映射在 [image/mod.rs:374-386](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/image/mod.rs#L374-L386)，字节嗅探 `ImageFormat::detect` 在 [image/mod.rs:559-573](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/image/mod.rs#L559-L573)：先试交换格式（png/jpg/gif/webp 的 magic bytes，由 `ExchangeFormat::detect`），再试 SVG（前 2048 字节找 SVG 命名空间，或 gzip magic `1f 8b` 对应 svgz，见 [image/mod.rs:583-595](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/image/mod.rs#L583-L595)），再试 PDF（前 2048 字节找 `%PDF-`，见 [image/mod.rs:577-580](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/image/mod.rs#L577-L580)）。

**Image：memoize 的值类型。**

[image/mod.rs:414-415](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/image/mod.rs#L414-L415) `Image(Arc<LazyHash<ImageInner>>)`，与 u6-l2 的 `Frame` 一样用 `Arc<LazyHash<..>>` 兼顾廉价克隆与可哈希。`new_impl` 标了 `#[comemo::memoize]`（[image/mod.rs:452-459](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/image/mod.rs#L452-L459)），所以同一份 `(kind, alt, scaling)` 反复构造得到同一个 `Arc`，解码结果在整个编译里复用。`ImageFit`（cover/contain/stretch，[image/mod.rs:395-409](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/image/mod.rs#L395-L409)）决定图像如何适配 `width`/`height` 给出的区域。

**Clipping 示例的真实归属。** 文档里的裁剪示例位于：

[image/mod.rs:52-76](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/image/mod.rs#L52-L76)（`= Clipping <clipping>` 段）。它的写法是：

```typst
#let lynx = image("lynx.jpg", height: 150pt, fit: "cover")
#grid(
  columns: 2,
  lynx,
  stack(
    spacing: 5pt,
    block(lynx, clip: true, inset: (bottom: -80pt)),
    block(lynx, clip: true, inset: (top: -75pt)),
  )
)
```

这里的 `clip: true` 和 `inset` 都是 **`block` 元素的参数**，不是 `image` 的。机制是：用负的 `inset`（如 `bottom: -80pt`）把 block 的内容区向外延伸（即让图像溢出 block 边界），再用 `block` 的 `clip: true` 把溢出部分视觉裁掉。文档明确指出（[image/mod.rs:56-59](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/image/mod.rs#L56-L59)）：

> Note that the clipped parts are only visually hidden. The full image data is still embedded in the output (except when exporting to PNG). This approach is thus not suitable for redacting parts of an image.

也就是说，裁剪只是**视觉层面**的隐藏，完整图像数据仍嵌入输出（导出 PNG 时除外），因此**不能**用它来给图像打码脱敏。这条特性由 `block` 的实现（在 layout 层）决定，`ImageElem` 既无 `clip` 字段，其解码路径（上面的 `decode`）也完全不涉及裁剪逻辑。

#### 4.4.4 代码实践

**实践目标**：追踪「同一份字节，三种确定格式的方式」，并确认 clipping 不属于 `ImageElem`。

**操作步骤**（源码阅读型 + 本地可选）：

1. 在 [image/mod.rs:240-352](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/image/mod.rs#L240-L352) 的 `decode` 里，找到 raster / svg / pdf 三条分支各自的入口函数（`RasterImage::new` / `SvgImage::with_fonts_images` / `PdfDocument::new`）。
2. 在 [image/mod.rs:356-370](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/image/mod.rs#L356-L370) 的 `determine_format` 里，列出三级回退的顺序，并解释「为什么传 `bytes(..)` 时通常要显式给 `format:`」——因为字节源没有扩展名可猜，只能靠嗅探，而原始像素数据根本嗅探不出来。
3. 在 [image/mod.rs:52-76](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/image/mod.rs#L52-L76) 阅读 Clipping 示例，再到 `ImageElem` 的字段列表（[image/mod.rs:78-229](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/image/mod.rs#L78-L229)）确认其中没有任何 `clip` 字段——`clip` 是 `block` 的字段。
4. 本地可选：写一个最小文档验证三种格式确定方式：
   ```typst
   #image("a.svg")                       // 靠扩展名
   #image(read("a.svg", encoding: none), format: "svg")  // 靠显式 format
   #image(bytes(..), format: (encoding: "luma8", width: 4, height: 4))  // 原始像素必须显式
   ```

**需要观察的现象**：原始像素数据（`encoding: "luma8"`）必须用字典形式的 `format:` 指明编码与宽高，否则 `determine_format` 三道防线全部失败（无扩展名、嗅探不到），报 `unknown image format`。

**预期结果**：你能解释「为什么 `image(bytes(..))` 不给 `format:` 几乎一定失败，而 `image("a.png")` 几乎一定成功」。

> 待本地验证：第 4 步的运行结果以本地 Typst 与本地图片文件为准；若无现成图片，可仅完成源码阅读部分（步骤 1–3）。

#### 4.4.5 小练习与答案

**练习 1**：`#image(bytes(range(16).map(x => x*16)), format: (encoding: "luma8", width: 4, height: 4))` 为什么必须给 `format:` 字典？三层嗅探分别会发生什么？

**答案**：原始像素数据没有文件扩展名（路径源才走 `determine_format_from_path`），也没有任何 magic bytes（`ExchangeFormat::detect`/`is_svg`/`is_pdf` 都不匹配），所以三层全失败。必须用字典形式 `format:` 显式告诉 Typst 这是 `luma8` 编码的 4×4 像素。`width × height × 通道数` 必须等于字节长度。

**练习 2**：文档说 clipping「不适合用于给图像脱敏」，为什么？

**答案**：因为 `block(clip: true)` 只在**视觉上**隐藏溢出部分，完整图像数据仍嵌入输出（PDF 里图像未被裁切，只是被 clip 遮挡；只有导出 PNG 时才真正不绘制被裁部分）。读者从 PDF 里仍可能提取到完整原图。`ImageElem` 的解码与输出始终保留完整数据。

**练习 3**：`Image::new_impl` 上的 `#[comemo::memoize]` 起什么作用？

**答案**：让「相同的 `(kind, alt, scaling)` 输入」复用同一个 `Arc<LazyHash<ImageInner>>`，避免同一张图在文档里反复出现时重复解码、重复分配。这是 Typst 增量编译/记忆化思想（comemo）在图像上的一个具体落点（回顾 u12-l2）。

---

## 5. 综合实践

把本讲四个模块串起来，做一个「用代码画出一张彩色徽章」的小任务。目标产物是一张矩形徽章：背景是渐变、边框是描边、中间用曲线画一个图标、右下角嵌一张小图。

**任务要求**（纯 Typst 源码编写型，可本地编译）：

1. **颜色与渐变**：用 `gradient.linear(..color.map.rainbow)` 或自定义 `oklch` 颜色构造一个线性渐变，作为 `rect` 的 `fill`。
2. **描边**：用 `stroke: 2pt + blue` 或字典形式 `(paint: blue, thickness: 2pt, cap: "round")` 给徽章加边框；尝试用 `repr` 观察两种写法的差别（对应 4.2 的简写/字典两条分支）。
3. **曲线图标**：用 `curve(..)` 画一个简单图形（如三角或波浪），给它 `fill` 和 `stroke`，体会「几何与涂料解耦」（4.3）。
4. **图像**：嵌一张本地小图，分别尝试 `fit: "cover"`/`"contain"`/`"stretch"`，观察 `ImageFit` 的差异（4.4）。
5. **反思题**：尝试用 `block(image(..), clip: true, inset: (bottom: -20pt))` 裁剪这张小图，并解释「为什么这不是 `image` 的功能、为什么不能用来脱敏」。

**进阶（源码阅读型）**：完成上述产物后，回到源码画一张「从用户输入到 `Frame`」的数据流图，标注每一处类型转换：

- `gradient.linear(..)` → `Gradient` → `Paint::Gradient` → `rect.fill: Option<Paint>`；
- `2pt + blue` → `Stroke` cast → `Smart<Sides<..>>` 字段 → `Fold` → `Resolve` → `FixedStroke` → `Shape.stroke`；
- `curve(..)` → `CurveElem` → `Curve` → `Geometry::Curve` → `Shape.geometry`；
- `image(..)` → `DataSource::load` → `determine_format` → `decode` → `ImageKind` → `Image`。

预期：画完这张图后，你会看到 `Paint`/`Shape`/`Geometry`/`ImageKind` 这四个内部类型正是本模块把「五花八门的用户输入」收束成「绘制层能处理的少数几种数据」的汇聚点。

## 6. 本讲小结

- `Color`/`Gradient`/`Tiling` 都有**双重身份**：既是 `Value` 的原始变体（由 `primitive!` 生成转换实现），又是 `NativeType`（由 `#[ty(scope, cast)]` + `define_type` 注册）；`#[ty]` 的 `cast` 标志正是为避免与 `primitive!` 重复生成而设。
- `Color` 的构造器（`rgb`/`luma`/`cmyk`/`oklch`/…）是挂在类型 scope 上的 `#[func]`；其中 `luma`/`oklab`/`oklch`/`rgb`/`cmyk` 五个在装配期额外 `define` 为裸全局，命名颜色（`red`/`blue`/…）则是 `pub const` 常量。
- `Paint` 用一个三变体枚举统一纯色/渐变/平铺；`Stroke` 用六个 `Smart<T>` 字段实现「逐维可继承」，经 `Fold` → `Resolve` → `unwrap_or_default` 最终产出绘制层使用的 `FixedStroke`。
- 形状元素（`rect`/`square`/`ellipse`/`circle`/`polygon`/`curve`）只是配置载体；真正描述可绘制图形的是内部 `Shape{geometry, fill, fill_rule, stroke}`，其中 `Geometry` 只有 `Line`/`Rect`/`Curve` 三种——椭圆/圆/多边形都被转成 `Curve`。互斥参数（`size`/`radius`）用 `#[parse]` 归一。
- `ImageElem::decode` 用「显式 `format` → 扩展名 → 字节嗅探」三级策略确定格式，再分发到 raster/svg/pdf 三条解码路径，产物统一为 memoize 的 `Image`。
- clipping 是 `block(clip: true)` 实现的**视觉裁剪**，与 `ImageElem` 无关；它只视觉隐藏、仍嵌入完整数据，不能用于脱敏。

## 7. 下一步学习建议

- **内省与图像**：`ImageElem` 带有 `Locatable`/`Tagged`/`Figurable` 能力，可结合第 9 单元（u9）实现「查询文档里所有图片」「按图生成图表目录」。
- **绘制层去哪了**：本讲反复提到「真正栅格化的算法在行为 crate」。建议在学完第 5 单元 `Routines`（u5-l4）后，去 `typst-layout` 里搜索 `Shape`/`FixedStroke`/`Gradient` 的消费点，看 `FrameItem::Shape` 是如何被栅格化的。
- **字体与 SVG 文本**：`SvgImage::with_fonts_images` 把当前字体族注入 SVG，这是「SVG 内 `<text>` 用 Typst 字体」的实现。可结合 u7（文本系统）理解为何需要注入字体。
- **数据加载链**：`ImageElem` 的 `source` 字段是 u11-l1（数据加载）的直接应用。若跳过了 u11-l1，建议回头读 `DataSource`/`Load`/`Loaded`，再回看本讲的 `#[parse]` 块会更顺。
- **下一讲（u12-l1）**：特性开关与 PDF/HTML 输出模块。`ImageElem` 在 PDF 导出与（未来的）HTML 导出中的差异、ICC profile 与无障碍（`alt`）如何与输出格式交互，都将在那里展开。

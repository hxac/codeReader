# 类型转换系统：cast、Type、Module 与 Scope

## 1. 本讲目标

本讲是「值与类型基础」单元的收束篇。在上一讲里，你已经认识了 `Value` 枚举和 `Array`/`Dict`/`Bytes`/`Label` 这些容器。但这些类型只是「数据本身」——Typst 还需要回答三个更上层的问题：

1. 一个 Rust 类型（比如 `f64`、`Color`）如何变成 Typst 用户能看到的 `Value`，反过来一个 `Value` 又如何被还原成具体的 Rust 类型？
2. 一个类型（比如 `color`、`str`）如何拥有名字、文档、构造函数，从而能被用户写进代码里？
3. 这些「定义」（变量、函数、类型）到底存放在什么结构里，用户写 `#rect` 或 `#str` 时，编译器又是按什么顺序把它们找出来的？

学完本讲，你将能够：

- 说清 `Reflect` / `IntoValue` / `FromValue` 三段式转换模型的分工，以及 `CastInfo` 如何同时服务于「转换」与「报错」。
- 读懂 `cast!` 宏的多分支声明，区分 `self =>`、`v: T =>`、`"字面量" =>` 三类分支。
- 理解 `Type` / `Module` 如何把一个类型及其附属定义组织成带名字的命名空间。
- 掌握 `Scope` / `Binding` / `Scopes` 的存储结构，并解释变量查找如何从局部作用域一路回退到标准库 `base` 与 `std`。

## 2. 前置知识

本讲假设你已经掌握：

- **`Value` 枚举**：Typst 运行时的万能值类型，约 30 个变体，体积被约束在 32 字节以内（见 u2-l1）。本讲会反复在 `Value::Int`、`Value::Float`、`Value::Str`、`Value::Type`、`Value::Module` 这些变体之间穿梭。
- **弱类型提升**：Typst 允许「整数当浮点数用」这类隐式转换（见 u2-l1 中 `Int` 自动提升为 `Float`）。本讲会从源码层面解释这个行为是怎么来的。
- **容器类型**：`Array` 用 `EcoVec`、`Dict` 用 `IndexMap`、`Label` 用字符串驻留实现 O(1) 操作（见 u2-l2）。本讲的 `Scope` 同样基于 `IndexMap`，理解写时复制与有序映射会很有帮助。
- **标准库装配**：`Library` 经 `global()` 把各模块「定义」进一个全局 `Scope`（见 u1-l3）。本讲解释这些「定义」长什么样、怎么被查到。

两个贯穿全讲的关键词，先在这里统一约定：

- **转换（cast）**：Rust 类型与 Typst `Value` 之间的双向翻译。这不是「强制类型转换」，而是带校验、带错误信息的「适配」。
- **命名空间（scope / module）**：一个「名字 → 定义」的有序映射。`Scope` 是底层容器，`Module` 是对外可见的、带名字和文件来源的 `Scope` 包装，`Type` 则是「类型的命名空间」，可挂载该类型专属的子函数（比如 `color.mix`）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/foundations/cast.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/cast.rs) | 转换系统的核心：`Reflect` / `IntoValue` / `FromValue` 三 trait、`CastInfo` 描述类型、`cast!` 宏的若干内置实例。 |
| [src/foundations/value.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/value.rs) | `Value::cast` 入口与 `primitive!` 宏——后者批量为标量生成三 trait 实现。 |
| [src/foundations/ty.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/ty.rs) | `Type` 类型句柄、`NativeType` / `NativeTypeData`——把一个类型注册为用户可见的一等类型。 |
| [src/foundations/module.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/module.rs) | `Module`——带名字、带内容、带文件来源的命名空间包装。 |
| [src/foundations/scope.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs) | `Scope` / `Binding` / `Scopes`——定义的存储、种类、查找与回退。 |
| [src/foundations/int.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/int.rs) | `cast!` 多分支实例（`ToInt`）与整数族宏，演示 `v: T =>` 分支的写法。 |
| [src/visualize/color.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/visualize/color.rs) | `#[ty(cast)]` 派生形式（`Color`）与显式 `cast!` 块（`ColorSpace` 等）的真实案例。 |

## 4. 核心概念与源码讲解

### 4.1 类型转换三段式模型：Reflect / IntoValue / FromValue 与 CastInfo

#### 4.1.1 概念说明

Rust 是强类型语言，`f64` 和 `Color` 之间不能随便互换；但 Typst 用户写的是弱类型的脚本，`#let x = 10` 之后 `x` 既能当整数，也能（在某些函数里）当浮点数用。于是在 Rust 一侧的「具体类型」和用户一侧的「万能 `Value`」之间，必须有一座翻译桥梁。

Typst 把这座桥拆成 **三个 trait**，文件开头的注释写得非常清楚：

- `Reflect for T`：描述「`T` 能接受哪些 Typst 值」，用于**文档和自动补全**，也提供一个快速的「能不能转」判定。
- `IntoValue for T`：`T -> Value`，**不会失败**（infallible）。
- `FromValue for T`：`Value -> T`，**可能失败**（fallible）。

> 为什么不直接用标准库的 `TryFrom<Value>`？源码注释给出两个原因：`TryFrom` 会与其他实现冲突；而 `From<T> for Value` 会把实现方向反过来，导致代码里到处是难懂的 `.into()`。所以 Typst 自定义了这三个 trait。

`CastInfo` 则是这套模型的「类型描述语言」：它用一个枚举来表达「我接受任意值 / 我接受某个特定值 / 我接受某种类型 / 我接受以下若干种之一」。它既喂给 `Reflect`（描述输入输出），又喂给错误信息生成器（告诉用户「我期望什么、却看到了什么」）。

#### 4.1.2 核心流程

三段式模型的数据流向可以画成一条双向通道：

```text
         IntoValue (Rust -> Value)              FromValue (Value -> Rust)
T (Rust) ───────────────────────────▶ Value ───────────────────────────▶ T (Rust)
        IntoValue::into_value              T::from_value / Value::cast
        （不会失败）                          （可能失败，返回 HintedStrResult）

                 ▲                                          │
                 │  Reflect::input()/output()               │  失败时
                 │  返回 CastInfo                            ▼
                 └──── 描述「期望什么」 ◀── CastInfo::error(found)
                                                拼出 "expected X, found Y" + 提示
```

要点：

1. **`IntoValue` 与 `FromValue` 不必对称**。例如 `f64` 的 `FromValue` 愿意接受一个 `Value::Int`（整数提升为浮点），但 `i64` 的 `FromValue` 不接受 `Value::Float`（浮点不能无损变整数）。这就解释了 u2-l1 提到的「弱类型体验」的来源。
2. **`Reflect` 是为工具与性能服务的**。`castable(value)` 是一个 `bool` 判定，注释明确说「这是为性能存在」——本可以通过 `CastInfo` 动态判断，但那样要堆分配 + 动态检查；为每个类型生成专用的优化机器代码快得多。
3. **`CastInfo` 一物两用**：既描述类型，也驱动「智能错误信息」。比如用户传了一个 `Value::Int` 而你期望的是长度，错误信息会附带「长度需要单位——你是不是想写 `12pt`？」这类提示——这正是 `CastInfo::error` 干的事。

#### 4.1.3 源码精读

**三个 trait 的定义**位于 cast.rs 顶部。首先是 `Reflect`，它的文档注释把整个模型讲了一遍：

[cast.rs:20-58](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/cast.rs#L20-L58) — `Reflect` trait 声明 `input` / `output` / `castable` / `error` 四个方法，并解释了三段式模型。`error` 默认委托给 `Self::input().error(found)`。

`IntoValue` 只有一个方法，极其简单：

[cast.rs:172-178](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/cast.rs#L172-L178) — `IntoValue::into_value(self) -> Value`，`Value` 自身的实现就是 `self`（恒等）。

`FromValue` 是会失败的逆向转换：

[cast.rs:249-255](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/cast.rs#L249-L255) — `FromValue::from_value(value) -> HintedStrResult<Self>`。注意它带一个上界 `Sized + Reflect`：**任何能从 `Value` 还原的类型，都必须先能「描述自己」**（实现 `Reflect`）。

实际触发 `FromValue` 的统一入口是 `Value::cast`：

[value.rs:152-154](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/value.rs#L152-L154) — `value.cast::<T>()` 只是 `T::from_value(value)` 的一层语法糖。整个 crate 里到处出现的 `args.expect::<T>("name")?`，最终都会走到这里。

**`CastInfo` 枚举**是这套模型的「语法」：

[cast.rs:294-304](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/cast.rs#L294-L304) — 四个变体：`Any`（任意值皆可）、`Value(Value, &str)`（特定值，带短文档）、`Type(Type)`（某类型的任意值）、`Union(Vec<Self>)`（多选一）。

它的 `error` 方法把「期望 / 实际」拼成人类可读的错误，并附带智能提示：

[cast.rs:309-365](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/cast.rs#L309-L365) — `CastInfo::error`。注意末尾三段特判：传了整数却期望长度 → 提示「加单位」；传了字符串却期望标签 → 提示「用 `<...>` 或 `label(...)`」；传了 `decimal` 却期望 `float` → 提示「显式用 `float(value)` 转换」。这些都是真实可复现的 Typst 报错提示的源头。

为了最直观地看到三 trait 如何「成套」实现，最好的例子是 `primitive!` 宏——它一次性为某个标量生成 `Reflect + IntoValue + FromValue`：

[value.rs:577-617](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/value.rs#L577-L617) — `primitive!` 宏。`Reflect::input/output` 都返回 `CastInfo::Type(Type::of::<Self>())`；`IntoValue` 把自己包成对应 `Value` 变体；`FromValue` 先匹配自己的变体，再匹配若干「其它可接受变体」（即类型提升分支），都不中就调用 `<Self as Reflect>::error(&v)`。

具体调用处，浮点数之所以能接受整数，就来自这一行：

[value.rs:619-621](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/value.rs#L619-L621) — `primitive! { f64: "float", Float, Int(v) => v as f64 }`。最后那个 `Int(v) => v as f64` 正是「整数自动提升为浮点」的源头。对比 `primitive! { i64: "integer", Int }` 没有额外分支，所以浮点不能反向变整数。

#### 4.1.4 代码实践

**实践目标**：亲手验证「整数提升为浮点」的报错行为，并对照 `CastInfo::error` 理解提示从何而来。

**操作步骤**（源码阅读型实践，无需编译）：

1. 打开 [value.rs:577-617](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/value.rs#L577-L617)，确认 `primitive!` 同时生成了三 trait。
2. 对比 `i64`（L620）和 `f64`（L621）两条调用：`f64` 多了 `Int(v) => v as f64` 分支，`i64` 没有。
3. 在脑海中模拟：某个函数参数声明为 `f64`，用户传入 `Value::Int(3)` → `FromValue for f64` 命中 `Int(v) => v as f64` → 返回 `3.0`，成功。
4. 反向模拟：参数声明为 `i64`，用户传入 `Value::Float(3.0)` → `FromValue for i64` 的 `match` 不命中任何分支 → 走 `v => Err(<Self as Reflect>::error(&v))` → 最终由 [cast.rs:309-365](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/cast.rs#L309-L365) 拼出 `expected integer, found float`。

**需要观察的现象**：`IntoValue`（`i64 -> Value::Int`）与 `FromValue`（`Value -> i64`，只认 `Int`）的「方向不对称」。

**预期结果**：你能用一句话解释「为什么 `1 + 1.0` 在 Typst 里成立，而把 `1.0` 赋给一个只收整数的参数会报错」——前者算术层做了提升（见 u2-l1 的 `ops.rs`），后者类型层 `FromValue` 拒绝。

> 本地验证（可选）：若有 typst CLI，写一个最小 `.typ` 文件，调用一个只收 `int` 的函数（如 `calc.even(2.0)`），观察报错文本是否与上面 `CastInfo::error` 的拼装一致。若不便运行，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`Reflect::castable` 既然和 `CastInfo` 描述的信息重叠，为什么 Typst 还要单独提供它？

**参考答案**：性能。注释 [cast.rs:40-45](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/cast.rs#L40-L45) 说明，用 `CastInfo` 判定需要堆分配加动态检查；而 `castable` 为每个类型生成专用机器代码，是热路径上的快速预筛。

**练习 2**：`FromValue` 的签名带约束 `Sized + Reflect`。如果某个类型只实现了 `FromValue` 却不实现 `Reflect`，会发生什么？

**参考答案**：编译失败。`FromValue<V = Value>: Sized + Reflect`（[cast.rs:252](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/cast.rs#L252)）把 `Reflect` 列为父 trait，强制「能被还原的类型必须先能描述自己」，保证 `error()` 总能用。

### 4.2 cast! 宏：多分支转换声明

#### 4.2.1 概念说明

对内置标量，`primitive!` 已经够用。但对 `Color`、`ColorSpace`、`MathClass` 这类复合类型，转换规则更复杂：它们既要支持「字符串字面量 → 变体」（如 `"binary" => MathClass::Binary`），又要支持「另一种类型 → 本类型」（如 `v: Color => ...`），还要支持反向输出（`self => ...`）。

为了不让人手写三 trait 的样板代码，`typst-macros` 提供了两个工具（本 crate 只是 reexport）：

[cast.rs:1-3](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/cast.rs#L1-L3) — `pub use typst_macros::{cast, Cast};`。`cast!` 是声明式宏（生成三 trait 实现），`Cast` 是把 `IntoValue + FromValue` 合起来的便捷 trait。

Typst 里有两种写法：

- **派生形式**：在类型上加属性 `#[ty(cast)]`，让 `#[ty]` 宏自动生成「接受本类型对应 `Value` 变体」的实现。
- **宏块形式**：显式写 `cast! { 目标类型, 分支... }`，列出所有输入/输出分支。

#### 4.2.2 核心流程

一个 `cast!` 宏块最多有三类分支，顺序通常为：

```text
cast! {
    目标 Rust 类型,

    // ① 输出分支（生成 IntoValue）：self 指向被转换的值
    self => <返回 Value 的表达式>,

    // ② 类型分支（生成 FromValue 的某条路径）：v 是解构出的内部值
    v: 另一种类型 => <返回 Self 的表达式>,

    // ③ 字面量分支（也属于 FromValue）：匹配特定的字符串值
    "字面量" => <返回 Self 的表达式>,
}
```

- 分支 ① 对应 `IntoValue`，必须存在才能把该类型放进 `Value`。
- 分支 ② / ③ 对应 `FromValue` 的多条路径，按声明顺序匹配；都不中则走 `Reflect::error`。
- 字面量分支的文档注释（`/// ...`）会被收集进 `CastInfo::Value(value, "短文档")`，最终出现在文档与自动补全里。

派生形式 `#[ty(cast)]` 则等价于「只接受本类型自己的 `Value` 变体」，是一种简化版的 `cast!`。

#### 4.2.3 源码精读

**最清晰的字面量分支例子**是 `MathClass`——数学符号的分类，用字符串字面量对应各变体：

[cast.rs:482-527](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/cast.rs#L482-L527) — `cast! { MathClass, ... }`。`self =>` 用 `match` 把变体翻成字符串（输出）；随后每个 `"normal" => MathClass::Normal` 之类都是字面量输入分支，每个分支上方的 `///` 注释会成为该字面量的文档。`SyntaxMode`（[cast.rs:467-480](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/cast.rs#L467-L480)）是同结构的更小例子。

**类型分支（`v: T =>`）的典型例子**是 `ToInt`——它把多种数值/字符串统一收集成一个供 `int()` 构造器使用的中间类型：

[int.rs:454-461](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/int.rs#L454-L461) — `cast! { ToInt, v: i64 => ..., v: bool => ..., v: f64 => ..., v: Decimal => ..., v: Str => ... }`。注意它没有 `self =>` 输出分支（`ToInt` 只是输入侧的聚合体，不需要放进 `Value`），却有 5 条 `v: T =>` 分支，演示了「一个 Rust 类型可接受多种 Typst 类型」。

> 整数族还有一个用宏生成的批量 cast：[int.rs:495-513](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/int.rs#L495-L513) 的 `signed_int!` 把 `i8/i16/i32/i128/isize` 统一处理，太大会回退成 `Value::Float`——这解释了为什么超大整数在 Typst 里会「悄悄」变浮点。

**最丰富的三分支混合例子**在颜色模块。`ColorSpace`（色彩空间）同时有输出分支、类型分支、和一个兜底分支：

[color.rs:2534-2553](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/visualize/color.rs#L2534-L2553) — `cast! { ColorSpace, ... }`。三类分支：
- `self => match self { ... }`：把变体翻成对应的构造函数值（输出）。
- `spot: SpotColorant => Self::Spot(spot)`：类型分支，从一个专色色料构造 `Spot` 变体。
- `v: Value => { ... }`：兜底分支，期望收到一个函数（如 `rgb`、`oklch`），并尝试把它识别成某个处理色空间；不中就 `bail!` 报错。

而 `Color` 本身用的是**派生形式**：

[color.rs:280-282](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/visualize/color.rs#L280-L282) — `#[ty(scope, cast, since = "forever")] pub enum Color { ... }`。`cast` 标志让宏自动生成「接受 `Value::Color`」的转换；`scope` 标志则额外生成一个附属作用域（承载 `color.mix`、`color.components` 等子函数，见 4.3）。

#### 4.2.4 代码实践（本讲指定实践任务之一）

**实践目标**：选 `visualize/color.rs` 中的一个 `cast!` 实例，标注它的「输入分支」与「输出」，并区分派生形式与宏块形式。

**操作步骤**：

1. 打开 [color.rs:280-282](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/visualize/color.rs#L280-L282)，确认 `Color` 用的是 `#[ty(scope, cast)]` **派生形式**——它没有手写 `cast!` 块，转换实现由宏自动生成。
2. 打开 [color.rs:2534-2553](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/visualize/color.rs#L2534-L2553) 的 `ColorSpace`，画一张表：

   | 分支 | 种类 | 对应生成 |
   | --- | --- | --- |
   | `self => match self { ... }` | 输出 | `IntoValue` |
   | `spot: SpotColorant => Self::Spot(spot)` | 类型输入 | `FromValue` 路径 1 |
   | `v: Value => { ... }` | 兜底输入 | `FromValue` 路径 2 |

3. 再看一个更小的类型分支例子 [color.rs:2158-2165](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/visualize/color.rs#L2158-L2165) 的 `ProcessColor`：输出 `self => Color::from(self).into_value()`，输入 `v: Color => match v { ... }`——注意它把 `Color` 解构，遇到 `Spot` 变体就 `bail!`。

**需要观察的现象**：同一个文件里，`Color` 用派生、`ColorSpace`/`ProcessColor` 用宏块——选择哪种，取决于转换规则是「只认自己」还是「有多条输入路径」。

**预期结果**：你能说清「为什么 `Color` 不需要手写 `cast!`（派生够了），而 `ColorSpace` 必须手写（要接受函数值并智能识别）」。

#### 4.2.5 小练习与答案

**练习 1**：在 `cast! { ColorSpace, ... }` 里，为什么 `v: Value => { ... }` 这条兜底分支放在最后？

**参考答案**：`FromValue` 的多条路径按声明顺序匹配。`spot: SpotColorant` 是更具体的类型分支，应优先命中；`v: Value` 匹配任意值，必须放最后做兜底，否则会「吃掉」前面所有分支。

**练习 2**：`ToInt`（[int.rs:454-461](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/int.rs#L454-L461)）没有 `self =>` 输出分支。这意味着什么？

**参考答案**：`ToInt` 只服务于 `int()` 构造器的**输入侧**（把多种类型聚合成一个可被还原成整数的中间体），它本身不会作为 `Value` 出现在用户代码里，因此不需要 `IntoValue`。这也说明 `cast!` 的输出分支是可选的。

### 4.3 Type 与 Module：类型与模块的命名空间

#### 4.3.1 概念说明

光有转换还不够。用户能写 `#type(x)`、能用 `str` 这个名字、能调用 `color.mix(...)`，是因为这些「类型」和「模块」被注册成了**一等公民**，拥有名字、文档、构造函数、乃至附属作用域。

- **`Type`**：描述「一种类型」的句柄。它本身也是一个 Typst 类型（你可以 `#type(type)`，结果还是 `type`）。`Type` 指向一块静态数据 `NativeTypeData`，里面存放名字、长短名、文档、关键词、构造函数和**一个附属 `Scope`**。
- **`Module`**：一组相关定义的集合，带名字、带可排版内容（`content`）、带文件来源（`file_id`）。它对应「内置模块（如 `math`）」「文件/包 import 的产物」「`plugin` 函数的返回值」。
- **`NativeType` / `NativeScope`**：Rust 侧的 trait，分别是「由原生 Rust 类型定义的 Typst 类型」和「类型的附属作用域」。

一句话区分：`Type` 是「类型的元信息 + 附属子定义」；`Module` 是「一组任意定义 + 文档正文 + 文件来源」。两者内部都用 `Scope` 存定义，但 `Type` 的定义是该类型专属的子函数（`color.mix`），`Module` 的定义是模块顶层导出的任意东西。

#### 4.3.2 核心流程

```text
   用户写 #type(12)                 用户写 str                       用户写 color.mix
        │                                │                                  │
        ▼                                ▼                                  ▼
  Type::construct (constructor)    全局 Scope 里查 "str"              先查 "color" 得到 Type
  返回 value.ty()                  得到 Binding(Value::Type(str))    再用 Type::field 查 "mix"
                                                                         （从 Type 的附属 Scope）
        │                                │                                  │
        └────────────── 数据来源 ─────────┴──────────────────────────────────┘
                          NativeTypeData（静态）  ←  Type 持有
                            ├ name / long_name / title
                            ├ docs / keywords / since
                            ├ constructor (LazyLock)
                            └ scope: LazyLock<Scope>   ← 子函数挂在这里

   文件 import "a.typ" → 求值后得到 Module { name, scope, content, file_id }
                          └ scope: Scope              ← 模块顶层定义挂在这里
```

关键点：

1. `Type` 的数据是**静态的**（`Static<NativeTypeData>`），全局唯一、`Copy` 且廉价；其中 `constructor` 和 `scope` 都是 `LazyLock`（惰性初始化）。
2. 查类型字段（`color.mix`）走 `Type::field` → `Type::scope().get(field)`；查模块字段（`std.rect`）走 `Module::field` → `Module::scope().get(field)`——最终都落到 `Scope::get`。
3. `Module` 用 `Arc<ModuleInner>` 做引用计数，克隆廉价；相等性按「指针相等 + 同名」判定（见源码）。

#### 4.3.3 源码精读

**`Type` 本身**是一个被注册的类型——注意它自己头上也有 `#[ty(scope, cast, since = "0.8.0")]`：

[ty.rs:63-65](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/ty.rs#L63-L65) — `pub struct Type(Static<NativeTypeData>);`。`Static` 保证它 `Copy` 且指向进程生命周期的静态数据。

`Type` 的方法大多是只读访问器。其中两个最关键：取附属作用域，和按名取字段：

[ty.rs:117-132](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/ty.rs#L117-L132) — `Type::scope()` 返回 `&'static Scope`；`Type::field()` 先在附属 scope 里查，查不到就 `bail!`，查到则用 `binding.read_checked(sink)` 读取（顺便触发弃用警告）。

`Type` 自己也是一个带构造函数的类型——用户写的 `#type(x)` 就走到它的构造函数：

[ty.rs:148-154](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/ty.rs#L148-L154) — `#[func(constructor, since = "0.8.0")] pub fn construct(value: Value) -> Type { value.ty() }`。`constructor` 标志声明这是该类型的构造函数，`value.ty()` 把任意值映射回它的 `Type`。

「一个原生类型要提供什么」由 `NativeType` trait 规定，实际数据放进 `NativeTypeData`：

[ty.rs:195-208](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/ty.rs#L195-L208) — `NativeType` trait，要求 `const NAME` 和 `fn data() -> &'static NativeTypeData`，并提供默认的 `fn ty()`。

[ty.rs:211-231](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/ty.rs#L211-L231) — `NativeTypeData` 结构：`name`（短名，如 `str`）、`long_name`（长名，用于错误信息，如 `string`）、`title`（文档标题，如 `String`）、`since`、`docs`、`def_site`、`keywords`、`constructor`（`LazyLock`）、`scope`（`LazyLock<Scope>`，挂子函数）。

甚至连「静态数据引用」本身也能被 cast 成 `Type` 值：

[ty.rs:239-242](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/ty.rs#L239-L242) — `cast! { &'static NativeTypeData, self => Type::from(self).into_value() }`。这是一个只有输出分支的最小 `cast!`，用于把静态数据引用直接当 `Type` 用。

**`Module`** 的结构与 `Type` 平行，但多了「内容」与「文件来源」：

[module.rs:47-65](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/module.rs#L47-L65) — `Module` 用 `Arc<ModuleInner>`，内部 `ModuleInner` 含 `scope`（顶层定义）、`content`（可排版正文）、`file_id`（文件来源）。注意 `Module` 也注册为类型 `#[ty(cast, since = "forever")]`。

模块的创建与字段访问：

[module.rs:67-78](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/module.rs#L67-L78) — `Module::new(name, scope)` 构造一个具名模块（content 空、无文件来源）。

[module.rs:138-147](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/module.rs#L138-L147) — `Module::field()` 在 scope 里查字段，查不到时按「有无名字」给出不同错误信息，这与 `Type::field` 的错误信息风格一致。

#### 4.3.4 代码实践

**实践目标**：对照 `Type::field` 与 `Module::field`，理解「类型字段」和「模块字段」最终都走 `Scope::get`。

**操作步骤**：

1. 阅读 [ty.rs:117-132](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/ty.rs#L117-L132)（`Type::scope` / `Type::field`）和 [module.rs:138-147](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/module.rs#L138-L147)（`Module::field`）。
2. 注意两者都调用 `something.scope().get(field)`，差别只在「scope 从哪来」：`Type` 从静态 `NativeTypeData.scope`，`Module` 从 `Arc<ModuleInner>.scope`。
3. 回顾 [value.rs:156-169](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/value.rs#L156-L169) 的 `Value::field`：当对一个 `Value::Type(ty)` 或 `Value::Module(module)` 取字段时，正是分流到这两个 `field` 方法。

**需要观察的现象**：`Type` 和 `Module` 在「字段访问」上几乎同构，只是数据来源（静态 vs `Arc`）和附带信息（content/file_id）不同。

**预期结果**：你能解释「为什么 `color.mix` 和 `std.rect` 在底层走的是同一套查找逻辑」——它们最终都调用 `Scope::get`。

> 待本地验证：若有 typst CLI，可分别试 `#color.mix(..)`（类型字段）和 `#std.rect(..)`（模块字段），观察两者都能正常解析；这正是同一套机制的两种入口。

#### 4.3.5 小练习与答案

**练习 1**：`Type` 内部用 `Static<NativeTypeData>`，而 `Module` 用 `Arc<ModuleInner>`。为什么设计不同？

**参考答案**：`Type` 描述的是编译期就完全确定的内置类型，数据是进程级静态的，用 `Static` 即可，`Copy` 且零分配；`Module` 的内容（尤其来自文件/包 import 的模块）是运行期产生的，需要引用计数共享与写时复制，所以用 `Arc`（配合 [module.rs:99-114](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/module.rs#L99-L114) 的 `Arc::make_mut` 修改）。

**练习 2**：`NativeTypeData` 里 `constructor` 和 `scope` 为什么用 `LazyLock` 而不是普通字段？

**参考答案**：构造函数与附属作用域可能引用其他尚未完成静态初始化的类型/函数，存在循环引用风险；`LazyLock` 把初始化推迟到首次访问，打破静态初始化顺序依赖。同时并非每个类型都会被用到，惰性也省去无用开销。

### 4.4 Scope / Binding / Scopes：定义的存储与查找

#### 4.4.1 概念说明

`Type` 和 `Module` 解决了「类型/模块长什么样」，但真正存放「名字 → 定义」的底层容器是 `Scope`。一个 `Scope` 就是一个有序映射（`IndexMap`），键是名字（`EcoString`），值是 `Binding`。

`Binding` 不只是「一个值」，它还携带元信息：值的种类（普通绑定 vs 闭包捕获）、定义处的 `Span`、分类（`Category`）、弃用信息。这些元信息决定了「能不能改写」「报错指向哪里」「文档归到哪一类」。

在求值时，变量查找不止看「当前作用域」。Typst 维护一个**作用域栈** `Scopes`：最上层是当前函数/块的局部变量，往下逐层是外层作用域，栈底回退到标准库的全局 `Scope`（`base.global`），最后还有一个特殊的 `std` 名字。数学模式则回退到 `base.math` 而非 `base.global`。

#### 4.4.2 核心流程

变量查找（代码模式）的回退顺序：

```text
Scopes::get("x")
  │
  ├─ 1. self.top            （当前局部作用域）
  ├─ 2. self.scopes (rev)   （外层作用域，从内到外）
  ├─ 3. base.global.scope() （标准库全局：rect / str / page ...）
  ├─ 4. 若 var == "std"     （特殊名字 std，指向 base.std）
  └─ 都没有 → unknown_variable(var)  （附带「是不是把减号写成了连字符」等提示）
```

数学模式（`get_in_math`）把第 3 步换成 `base.math.scope()`，且错误信息会区分「是不是该加 `#`」「是不是该用 `std.x`」。

`Scope` 的写入侧有四个便捷方法，分别对应四种「定义」：

| 方法 | 注册的对象 | 典型调用 |
| --- | --- | --- |
| `define_func::<F>()` | 原生函数 | `global.define_func::<panic_func>()` |
| `define_type::<T>()` | 原生类型 | `global.define_type::<Str>()` |
| `define_elem::<E>()` | 原生元素 | `global.define_elem::<RectElem>()` |
| `define(name, value)` | 任意具名值 | `global.define("left", Align::LEFT)` |

#### 4.4.3 源码精读

**`Scopes`** 是求值期的变量查找栈：

[scope.rs:16-25](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L16-L25) — `Scopes { top: Scope, scopes: Vec<Scope>, base: Option<&Library> }`。`top` 是当前活动作用域，`scopes` 是外层栈，`base` 指向标准库（提供全局/math/std 回退）。

核心查找逻辑 `Scopes::get`：

[scope.rs:46-59](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L46-L59) — 先用 `iter::once(&self.top).chain(self.scopes.iter().rev())` 从内到外找局部作用域；找不到再 `or_else` 回退到 `base`：查 `base.global.scope()`，再特判 `var == "std"` 返回 `base.std`；都失败则 `unknown_variable(var)`。这正是本讲指定实践要阅读的回退链。

数学模式的变体 `get_in_math`：

[scope.rs:76-94](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L76-L94) — `Scopes::get_in_math`。回退目标从 `base.global` 换成 `base.math`，错误信息 `unknown_variable_math` 会针对数学模式给出更贴切的提示（如「试试加 `#`」「用 `std.x` 访问」）。

**`Scope`** 的存储结构：

[scope.rs:105-111](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L105-L111) — `Scope { map: IndexMap<EcoString, Binding, FxBuildHasher>, deduplicate: bool, category: Option<Category> }`。用 `IndexMap` 保留插入顺序（文档顺序），`FxBuildHasher` 是快速非加密哈希，`deduplicate` 在 debug 模式下防重复定义，`category` 给随后注册的绑定盖上分类标签。

四种 `define_*` 写入方法集中在 `Scope` 的构造 impl 里：

[scope.rs:135-186](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L135-L186) — `define_func`（L137-140）从 `T::data()` 取函数数据；`define_type`（L152-156）用 `T::ty().short_name()` 作名；`define_elem`（L159-163）用 `T::ELEM` 的名字；`define`（L176-185）是通用入口，在 debug 下检查 `deduplicate`，并把当前 `self.category` 盖到新建 `Binding` 上。它们都最终落到 `bind`。

**`Binding`** 的元信息：

[scope.rs:248-261](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L248-L261) — `Binding { value, kind: BindingKind, span, category, deprecation }`。`BindingKind`（[scope.rs:264-270](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L264-L270)）区分 `Normal`（可改写）与 `Captured(Capturer)`（函数/上下文捕获，只读）。`write()`（[scope.rs:313-327](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L313-L327)）对捕获绑定会报「variables from outside the function/context are read-only」——这正是 Typst 里闭包捕获变量不可修改的来源。

最后，类型的附属作用域由 `NativeScope` trait 提供：

[scope.rs:239-246](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L239-L246) — `NativeScope` 规定类型可提供 `constructor()` 和 `scope()`，后者就是挂在 `NativeTypeData.scope` 上的那个 `Scope`（即 `color.mix` 这类子函数的家）。

#### 4.4.4 代码实践（本讲指定实践任务之一）

**实践目标**：阅读 `Scopes::get`，说清变量查找如何回退到标准库 `base.global` 与 `std`。

**操作步骤**：

1. 打开 [scope.rs:46-59](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L46-L59)，按 4.4.2 的回退顺序，逐行标注每一步对应哪段代码。
2. 特别留意 `or_else(|| { let base = self.base?; match base.global.scope().get(var) { ... None if var == "std" => Some(&base.std) ... } })`：这是「最后回退到全局 + std 特例」的关键。
3. 思考一个具体场景：用户在某个函数内部写 `#let len = calc.abs(-3)`。求值 `calc` 时，`top` 与所有外层 `scopes` 里都没有 `calc`，于是回退到 `base.global.scope().get("calc")` 命中（因为 `calc` 在 [foundations/mod.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/mod.rs) 被注入全局）。
4. 对比 [scope.rs:62-73](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L62-L73) 的 `get_mut`：对在全局 `base` 中找到的变量，它返回 `cannot_mutate_constant` 错误——所以你不能给 `std` 或全局常量赋值。

**需要观察的现象**：局部作用域优先，标准库兜底；标准库定义是只读常量。

**预期结果**：你能解释「为什么用户写的 `#let calc = 1` 会临时遮蔽（shadow）全局的 `calc` 模块，但一旦离开该作用域，`calc` 又变回标准库模块」——因为查找总是从 `top` 开始，局部绑定优先命中。配合 [scope.rs:96-102](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L96-L102) 的 `check_std_shadowed`，Typst 还能对「遮蔽了标准库名字」给出提示。

#### 4.4.5 小练习与答案

**练习 1**：在 `Scopes::get` 中，为什么对 `std` 要做 `None if var == "std" => Some(&base.std)` 的特判，而不是把 `std` 直接定义在全局 scope 里？

**参考答案**：`std` 是一个指向「整个标准库」的特殊别名（`base.std` 是一个预先构建好的 `Binding`/`Module`），它需要跨「全局模式」和「数学模式」都可用且语义一致（两种模式的 `get` / `get_in_math` 都特判 `std`）。把它作为 `Scopes` 层的特例，而不是塞进某个具体 `Scope`，能让它始终独立于作用域栈与模式切换存在。

**练习 2**：`Binding::write()` 对 `Captured` 绑定会报错（[scope.rs:313-327](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L313-L327)）。请举一个会触发该错误的 Typst 代码片段。

**参考答案**：在闭包里修改捕获的外层变量，例如：

```typst
#let x = 1
#let f = () => { x = 2 }   // x 是从外层捕获的，只读
#f()
```

求值 `x = 2` 时，`x` 在闭包的作用域里是 `BindingKind::Captured(Function)`，`write()` 会返回「variables from outside the function are read-only and cannot be modified」。

## 5. 综合实践

**任务**：追踪一个完整的「定义 → 查找 → 转换」链路，把本讲四个模块串起来。

设想用户写了这样一段 Typst 代码（伪代码，仅用于追踪）：

```typst
#let my-color = color.mix((red, 50%), (blue, 50%))
#set text(fill: my-color)
```

请按下列步骤，用本讲学到的源码知识解释每一步：

1. **类型如何被注册**：`color` 这个名字为何能在全局被找到？它是一个 `Type`（由 `#[ty(scope, cast)]` 标注，见 [color.rs:280-282](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/visualize/color.rs#L280-L282)），经 `global()` 装配时由 `define_type::<Color>()`（[scope.rs:152-156](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L152-L156)）写入全局 `Scope`。
2. **字段如何被查找**：`color.mix` 走 `Type::field("mix")` → `Type::scope().get("mix")`（[ty.rs:117-132](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/ty.rs#L117-L132)）；而 `my-color` 这个局部变量经 `Scopes::get("my-color")` 在 `top` 命中（[scope.rs:46-59](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L46-L59)）。
3. **结果如何被转换**：`color.mix` 返回一个 `Color`。当 `set text(fill: ...)` 需要把它存进 `text` 元素的 `fill` 字段时，参数侧用 `FromValue for Color`（由 `#[ty(cast)]` 派生，对应 `primitive!`/`cast!` 生成的实现）把 `Value` 还原成 Rust 的 `Color`；反过来，运行期把 `Color` 放进 `Value` 时走 `IntoValue`。
4. **错误如何产生**：假设用户误写成 `#set text(fill: "not a color")`，`FromValue for Color` 匹配失败，调用 `<Color as Reflect>::error(&v)`，最终由 [cast.rs:309-365](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/cast.rs#L309-L365) 拼出 `expected color, found string`。

**交付物**：画一张包含「`Scope` 存储 → `Scopes::get` 查找 → `Type::field` 取子函数 → `FromValue`/`IntoValue` 转换 → `CastInfo::error` 报错」五个环节的流程图，并在每个环节标注对应的源码文件与行号。这是一次纯源码阅读型实践，无需编译运行。

## 6. 本讲小结

- Typst 用 **`Reflect` / `IntoValue` / `FromValue` 三段式模型**在 Rust 类型与万能 `Value` 之间搭桥：`Reflect` 描述类型（用于文档/补全/快速判定），`IntoValue` 是不会失败的「Rust → Value」，`FromValue` 是可能失败的「Value → Rust」。
- **`CastInfo`** 用 `Any` / `Value` / `Type` / `Union` 四变体描述「期望什么值」，既喂给 `Reflect`，又驱动 [cast.rs:309-365](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/cast.rs#L309-L365) 的智能错误信息（如「长度需要单位」「用 `label(...)`」）。
- **`cast!` 宏**用 `self =>`（输出）、`v: T =>`（类型输入）、`"字面量" =>`（字面量输入）三类分支批量生成三 trait；`primitive!` 是标量的批量版；`#[ty(cast)]` 是「只认自身变体」的派生简化版。`f64` 接受 `Int` 的隐式提升就来自 [value.rs:621](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/value.rs#L619-L621)。
- **`Type`** 是「类型的元信息 + 附属 `Scope`」，数据静态（`Static<NativeTypeData>`）；**`Module`** 是「一组定义 + 文档正文 + 文件来源」，数据用 `Arc`。两者的 `field()` 最终都落到 `Scope::get`。
- **`Scope`**（`IndexMap<名字, Binding>`）是底层存储；**`Binding`** 携带种类/`Span`/分类/弃用等元信息，`Captured` 绑定只读。**`Scopes::get`** 按「当前 → 外层 → `base.global` → `std`」回退查找（[scope.rs:46-59](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L46-L59)），数学模式回退到 `base.math`。

## 7. 下一步学习建议

本讲把「值、类型、定义」的静态结构讲完了。接下来应进入「内容与元素」单元（u3）：

- **u3-l1 Content 与 RawContent**：本讲的 `Value::Content` 变体只是个入口，下一讲深入 `Content` 这个「所有标记与函数调用的产物」是如何用 `Packed` 做类型擦除、如何携带 `span/label/meta`。
- **u3-l2 Element、NativeElement 与能力 vtable**：本讲提到 `define_elem::<E>()` 注册元素；下一讲解释元素如何作为「类型擦除句柄」、如何用 `can<C>` 查询内省能力。
- **u3-l3 elem 宏、字段系统与 Packed**：本讲的 `cast!` 处理「类型 ↔ Value」，下一讲的 `#[elem]` 宏则处理「元素字段 ↔ 样式」，两者结构高度相似（都用宏生成样板），对照学习会事半功倍。

建议同时复习 u2-l1 的 `Value::cast`（[value.rs:152-154](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/value.rs#L152-L154)）和 u1-l3 的 `global()` 装配，因为本讲所有查找与转换最终都服务于那条装配链。

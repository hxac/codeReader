# 类型转换系统：cast、Type、Module 与 Scope

## 1. 本讲目标

本讲是「值与类型基础」单元的收口篇。前面两讲（u2-l1、u2-l2）认识了 `Value` 枚举和各种数据类型，但一直绕开了三个更上层的问题：Rust 类型怎么变成 Typst 的 `Value`、一个类型怎么获得名字与文档、这些定义到底存在哪里又怎么被查到。本讲就回答这三个问题。学完之后，你应该能够：

- 说清 `Reflect` / `IntoValue` / `FromValue` 三个 trait 各自的职责，以及为什么 Typst 要把「类型转换」拆成三段而不是用标准库的 `From`/`TryFrom`。
- 看懂项目里随处可见的 `cast! { ... }` 宏，能区分它的几种分支写法（字面量分支、类型输入分支、`self =>` 输出分支），并标注一段 `cast!` 的输入与输出。
- 理解 `CastInfo` 这个「描述性类型」如何同时服务于文档、自动补全和「expected X, found Y」错误信息。
- 解释 `Type`、`Module` 如何把一个 Rust 类型、一个命名空间表示成 Typst 里的一等值。
- 画出 `Scopes::get` 的变量查找链：从当前作用域一路回退到标准库 `base.global`，并解释 `std` 这个特殊名字为什么能直接拿到整个全局库。

## 2. 前置知识

本讲承接 u2-l1（`Value` 枚举与标量）和 u2-l2（容器类型）。在继续之前，请确认你已了解：

- `Value` 是 Typst 运行时的万能值类型，约 30 个变体，每个变体对应一个 Rust 类型（如 `Value::Int(i64)`、`Value::Str(Str)`），体积被约束在 32 字节以内。
- `primitive!` 宏（见 u2-l1）批量为标量生成转换实现，并把它们注册为「一等类型」。
- 容器类型 `Array`/`Dict` 用写时复制实现廉价克隆（见 u2-l2）；`Dict` 内部基于 `IndexMap`，本讲的 `Scope` 同样基于它。

本讲还会用到三个来自 u1-l3 的基础印象：

- 标准库被装配成一个 `Library` 配置对象，其中的 `global` 和 `math` 字段都是 `Module`，每个 `Module` 内部包着一个 `Scope`。
- `define` / `define_type` / `define_elem` / `define_func` 是把定义注入 `Scope` 的几条装配指令。
- `Library.std` 是一个特殊的 `Binding`，用于提供 `std` 这个名字。

下面凡是用到的术语（`Scope`、`Binding`、`Module`、`Library`）都会在本讲里逐个展开。

> 术语约定：本讲的 **转换（cast）** 指 Rust 类型与 Typst `Value` 之间的双向翻译——这不是「强制类型转换」，而是带校验、带错误信息的「适配」。

## 3. 本讲源码地图

本讲涉及四个核心源码文件，都在 `src/foundations/` 下：

| 文件 | 作用 | 本讲用它讲什么 |
|------|------|----------------|
| [src/foundations/cast.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/cast.rs) | 类型转换的三个核心 trait 与 `cast!` 宏的 reexport | `Reflect`/`IntoValue`/`FromValue`/`CastInfo` 三段式模型，以及几段真实的 `cast!` 示例 |
| [src/foundations/ty.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/ty.rs) | `Type` 类型对象 | 一个 Rust 类型如何被表示成 Typst 里的一等「类型值」 |
| [src/foundations/module.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/module.rs) | `Module` 模块对象 | 模块如何把一组定义（一个 `Scope`）打包成可导入、可点取的值 |
| [src/foundations/scope.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs) | `Scope` / `Binding` / `Scopes` | 名字→绑定的有序映射，以及运行时变量查找的回退链 |

此外会引用三个「佐证文件」：

- [src/foundations/value.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/value.rs)：`primitive!` 宏与 `Value::cast` 方法。
- [src/foundations/mod.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/mod.rs)：标准库 foundations 分类的 `define` 装配。
- [src/visualize/color.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/visualize/color.rs) 与 [src/foundations/int.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/int.rs)：两段真实的 `cast!` 实例。

> 提示：`cast!`、`#[ty]`、`#[scope]`、`#[func]` 这些宏本身定义在 **`typst-macros`** crate 里（本 crate 在 [src/foundations/cast.rs:3](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/cast.rs#L3) 只是 `pub use typst_macros::{cast, Cast};` 引入它们）。本讲只讲它们在 `typst-library` 里的**用法与效果**（即生成出来的 trait 实现），不深入宏的展开细节。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 cast 宏与 Reflect**：三段式转换模型（`Reflect`/`IntoValue`/`FromValue`）与 `CastInfo`，以及 `cast!` 的几种分支写法。
- **4.2 Type / Module**：把「类型」和「模块」表示成 Typst 一等值的两个对象。
- **4.3 Scope / Binding / Scopes**：定义的存储与运行时变量查找（含标准库回退）。

### 4.1 cast 宏与 Reflect

#### 4.1.1 概念说明

Typst 的运行时只认识一种值类型——`Value`；但 Rust 这边有几十种具体类型（`i64`、`Str`、`Color`、`Module`……）。两者之间需要一个**双向、带类型描述**的桥梁。Typst 没有用标准库的 `From<T> for Value` 或 `TryFrom<Value>`，而是设计了三个 trait，分工如下（这恰恰是 [src/foundations/cast.rs:20-32](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/cast.rs#L20-L32) 顶部注释里写明的设计意图）：

| Trait | 方向 | 是否可能失败 | 主要用途 |
|-------|------|--------------|----------|
| `Reflect` | （描述） | — | 描述 `T` 能接受/能产生哪些 Typst 值，供文档、自动补全、错误信息使用 |
| `IntoValue` | `T → Value` | 不会失败 | 把一个 Rust 值变成 `Value`（构造方向） |
| `FromValue` | `Value → T` | 可能失败 | 把一个 `Value` 还原成具体 Rust 类型（解析方向） |

为什么不用标准库方案？源码注释给了两点理由：

1. 不能用 `TryFrom<Value>`：会和别的 `impl` 冲突（`Value` 上已经有许多 blanket impl）。
2. 可以用 `From<T> for Value`，但那会把 `impl` 写在 `Value` 一侧，导致代码里到处 `.into()`，难以辨认。

所以 Typst 选择用**专属 trait** 把三个关注点彻底分开：描述（Reflect）、构造（IntoValue）、解析（FromValue）。

第三个概念是 `CastInfo`——它是一个**描述性枚举**，表达「这个位置能接受什么样的值」：

- `CastInfo::Any`：任意值都行。
- `CastInfo::Value(Value, &str)`：某个具体的值（比如字面量 `"markup"`），带一句文档。
- `CastInfo::Type(Type)`：某一类类型的任意值（比如「任意整数」）。
- `CastInfo::Union(Vec<Self>)`：以上几种的「或」组合。

`Reflect::input()` / `Reflect::output()` 各返回一个 `CastInfo`，分别描述「能转换进 `T` 的值」和「`T` 能转换出去的值」。这套描述被三处复用：生成文档、IDE 自动补全、以及拼出错误信息。

#### 4.1.2 核心流程

当你写 `value.cast::<T>()`（或函数参数解析时），数据流是这样的：

```
            ┌─────────────────────────────────────────────┐
            │  Value  ──────cast::<T>()──────►  T          │
            └─────────────────────────────────────────────┘
                          委托给 FromValue::from_value
                          （FromValue: Sized + Reflect，必带描述能力）

   from_value 内部:
     1. 先看 Value 是不是 T 直接对应的变体 ──► Ok(还原后的 T)
     2. 否则看是否匹配某个「转换分支」(int→float 这类) ──► Ok(T)
     3. 都不匹配 ──► Err( Reflect::error(&value) )
                          └── 由 CastInfo::error 拼出
                              "expected <input()描述>, found <实际类型>"
                              并按需附加 hint（如「length 需要单位」）

   反向（构造方向）:
     T  ──into_value()──►  Value      （IntoValue，绝不失败）

   辅助快速判断（用于 try 光标、show 选择等）:
     Reflect::castable(&value) ──► bool   （比走 CastInfo 快得多）
```

三个要点：

- **`FromValue` 强制要求 `Reflect`**（见下方源码），所以「能被从 Value 解析出来的类型」一定也「能描述自己」——这正是失败时能给出漂亮错误信息的前提。
- **`castable` 是性能优化**：完全可以用 `CastInfo` 判断一个值是否可转换，但那需要堆分配 + 动态遍历；`castable` 是为每种类型编译出的专用机器码，快得多（注释在 [src/foundations/cast.rs:41-45](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/cast.rs#L41-L45) 里有说明）。
- **`CastInfo` 可相加**：`impl Add for CastInfo` 会把两个描述合并成去重后的 `Union`（[src/foundations/cast.rs:389-417](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/cast.rs#L389-L417)）。这正是「一个参数接受多种类型」的底层机制。

#### 4.1.3 源码精读

**① 三个核心 trait 的定义。** 先看 `Reflect`：

[src/foundations/cast.rs:33-58](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/cast.rs#L33-L58) —— `Reflect` trait，提供 `input()`/`output()`/`castable()`/`error()` 四个方法。`error()` 默认委托给 `Self::input().error(found)`，即用「输入描述」来生成「期望…却得到…」的错误。

[src/foundations/cast.rs:175-178](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/cast.rs#L175-L178) —— `IntoValue`，只有一个 `into_value(self) -> Value`，**没有** `Reflect` 约束（构造方向不需要自我描述）。

[src/foundations/cast.rs:252-255](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/cast.rs#L252-L255) —— `FromValue<V = Value>: Sized + Reflect`，**显式要求 `Reflect`**。这就是「能解析就必能描述」的类型层保证。

**② `CastInfo` 枚举与错误生成。**

[src/foundations/cast.rs:294-304](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/cast.rs#L294-L304) —— 四个变体 `Any`/`Value`/`Type`/`Union`。

[src/foundations/cast.rs:306-365](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/cast.rs#L306-L365) —— `CastInfo::error(found)`：用 `walk` 把嵌套的 `Union` 摊平，收集所有可接受的「描述项」，拼成 `expected A, B or C, found <实际类型>`；并按实际值附加 hint（整数但期望 length → 提示「did you mean Npt?」；字符串但期望 label → 提示 `<...>`；decimal 但期望 float → 提示显式转换）。**你在 Typst 里看到的所有「expected X, found Y」错误都来自这里。**

**③ 统一入口 `Value::cast`。**

[src/foundations/value.rs:151-154](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/value.rs#L151-L154) —— `Value::cast::<T>(self)` 直接委托 `T::from_value(self)`。这是整个转换系统的统一入口。

**④ `primitive!` 宏：批量给标量生成三件套。** `Value` 的每个变体对应的 Rust 类型，几乎都靠这个宏一次性生成 `Reflect`+`IntoValue`+`FromValue`：

[src/foundations/value.rs:583-613](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/value.rs#L583-L613) —— 宏的一个分支：为 `$ty` 生成三件套，其中 `FromValue` 先匹配主变体，再匹配「转换分支」（如 `Int(v) => v as f64`，这就是 u2-l1 讲过的「弱类型自动提升」的落点），都不匹配则 `Err(Self::error(&v))`。

[src/foundations/value.rs:619-663](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/value.rs#L619-L663) —— 真实的调用清单。注意 `f64`、`Str`、`Content`、`Func` 等都带「转换分支」：例如 `Func` 可以从 `Type(ty) => ty.constructor()?.clone()` 得到（把类型当函数用，就是调用其构造器）。

**⑤ `cast!` 宏的几种写法。** `cast!` 用于「`primitive!` 没覆盖到」的自定义类型（枚举、newtype 等）。它有几种典型分支：

- **字面量分支**：`"markup" => SyntaxMode::Markup`。见 [src/foundations/cast.rs:467-480](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/cast.rs#L467-L480)（`SyntaxMode`）和 [src/foundations/cast.rs:482-527](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/cast.rs#L482-L527)（`MathClass`，14 个字面量分支）。每一行既是「可接受的输入值」，也是「输出时的字符串」。
- **类型输入分支**：`v: Color => ...`，接受某个已有的 Typst 类型并转换。见 [src/visualize/color.rs:2158-2165](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/visualize/color.rs#L2158-L2165) 的 `ProcessColor`：输入接受 `Color`、输出则包成 `Color::from(self)`；以及 [src/foundations/int.rs:454-461](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/int.rs#L454-L461) 的 `ToInt`，用 5 个 `v: <类型> =>` 分支表达「i64/bool/f64/Decimal/Str 都能转成它」。
- **`self =>` 输出分支**：上面两例都带 `self => <expr>`，描述如何把 `T` 转回 `Value`（即生成 `IntoValue`）。

> 一个边界例子：[src/foundations/cast.rs:437-465](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/cast.rs#L437-L465) 的 `Never`（不可居住类型）。它的 `input()` 返回空 `Union`、`castable` 恒为 `false`——即「什么都转换不进来」，常用于表示「这个参数永不出现」。

#### 4.1.4 代码实践

**实践一（源码阅读型，本讲指定的核心实践）：标注一段 `cast!` 的输入与输出。**

1. **实践目标**：能说清 `cast!` 里每行的方向（输入还是输出）和作用。
2. **操作步骤**：
   - 打开 [src/visualize/color.rs:2158-2165](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/visualize/color.rs#L2158-L2165) 的 `ProcessColor` 这段 `cast!`。
   - 把它抄到一张纸上，逐行标注。
3. **参考标注**（答案示例）：
   - 第 1 行 `ProcessColor,`：声明被转换的目标类型。
   - 第 2 行 `self => Color::from(self).into_value(),`：**输出分支**（生成 `IntoValue`）。把一个 `ProcessColor` 包成 `Color::Process(...)` 再变成 `Value::Color`。
   - 第 3–4 行 `v: Color => match v { Color::Process(c) => c, Color::Spot(_) => bail!(...) }`：**输入分支**（生成 `FromValue`）。接受 `Value::Color`，若内部是 `Process` 就取出，若是 `Spot` 则报错。
4. **需要观察的现象**：注意这段 `cast!` 没有任何 `"字面量" =>` 分支，也没有多个 `v: 类型 =>` 分支——它只有「一个输入类型 + 一个输出」。对比 [src/foundations/int.rs:454-461](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/int.rs#L454-L461) 的 `ToInt`（5 个输入分支）和 [src/foundations/cast.rs:482-527](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/cast.rs#L482-L527) 的 `MathClass`（14 个字面量分支），体会「分支越多 = `CastInfo::Union` 越大 = 错误信息里 expected 列表越长」。
5. **预期结果**：你能画出 `ProcessColor` 的 `CastInfo` 大致是 `Union([ Type(Color) ])`（输入）和 `Type(Color)`（输出）。

**实践二（运行观察型，待本地验证）：用错误信息反向观察 `CastInfo`。**

1. **实践目标**：亲眼看到 `CastInfo::error` 生成的提示。
2. **操作步骤**：准备一个最小 `.typ` 文件，故意把错误类型传给函数：
   ```typ
   #set text(size: "big")
   ```
3. **需要观察的现象**：编译报错信息应类似 `expected length, found string`。
4. **预期结果**：错误里的 `expected length` 正是 `TextSize` 的 `Reflect::input()`（一个 `CastInfo::Type(length)`）经 `error()` 拼出来的；`found string` 来自 `value.ty()`。这说明 `CastInfo` 不是抽象摆设，它直接决定用户看到的报错。
5. 若本地没有 Typst CLI，可标记「待本地验证」，仅做源码层面的推理。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `FromValue` 要带 `Reflect` 约束，而 `IntoValue` 不带？
> **答案**：`FromValue` 解析失败时要生成「expected …, found …」错误，这需要 `Reflect::input()` 来描述「期望什么」；所以能被解析的类型必须能自我描述。`IntoValue` 是构造方向，永不失败，不需要描述能力，故不要求 `Reflect`。

**练习 2**：`Reflect::castable` 和直接用 `CastInfo` 判断「某值能否转换」有什么区别？
> **答案**：功能上等价，但 `castable` 是为每个类型单独编译出的专用判断（直接模式匹配 `Value` 变体），而走 `CastInfo` 需要堆分配并动态遍历 `Union`。`castable` 存在纯粹是为了性能（见 cast.rs 注释）。

**练习 3**：看 [src/foundations/int.rs:454-461](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/int.rs#L454-L461) 的 `ToInt`，它没有 `self =>` 输出分支。这意味着它**没有**生成 `IntoValue`。那它为什么还要写 `cast!`？
> **答案**：`ToInt` 是一个内部聚合类型（把多种输入统一成一种），只用于「从 `Value` 解析」，不需要反向变回 `Value`，所以只声明输入分支、省略输出分支即可。`cast!` 允许只写需要的方向。

---

### 4.2 Type / Module

#### 4.2.1 概念说明

`Value` 里的每个变体都对应一个「类型」。Typst 把「类型」本身也做成了一等值——你可以写 `type(12)` 得到 `int`，可以把 `int` 当函数调用来构造整数。表示「类型」的 Rust 结构就是 `Type`。

`Type` 解决的问题是：**一个 Rust 类型如何携带足够的元数据（名字、文档、构造器、子作用域），从而能在 Typst 里被命名、被文档化、被点取（如 `str.len`）？**

`Module` 解决的是另一个问题：**一组相关的定义（函数、常量、子类型）如何被打包成一个可整体导入、可点取的值？** 你写 `#import "utils.typ"` 拿到的就是一个 `Module`；内置的 `math`、`sys` 也是 `Module`。

两者的共同点：都把「一组定义」组织起来。区别在于 `Type` 是「类型的附属作用域」（放该类型的构造器和方法），`Module` 是「独立命名空间」（可来自文件、包、内置）。

#### 4.2.2 核心流程

**一个 Rust 类型变成 Typst 一等类型的路径：**

```
Rust 类型 T（带 #[ty(...)] 注解）
   │
   │  typst-macros 生成
   ▼
impl NativeType for T          ← 提供 data(): &'static NativeTypeData
   │
   ▼
Type::of::<T>() / T::ty()      ← 一个 Type(Static<NativeTypeData>) 句柄
   │
   │  在装配期由
   │  scope.define_type::<T>()  注入全局 Scope
   ▼
用户可见的名字（如 "str"）→ Value::Type(...)
```

`#[ty(scope, cast, since = ...)]` 里的几个开关：

- `scope`：这个类型有一个附属作用域（由 `#[scope] impl` 块里的 `#[func]` 方法构成），`Type::scope()` 能取到它。
- `cast`：这个类型本身可以被 `cast`（即 `Value::Type` 与 `Type` 之间能互转）。
- `since = "x.y"`：文档里标注引入版本。

**模块的构成与访问：**

```
Module { name, inner: Arc<ModuleInner{ scope, content, file_id }> }
                                   │
                                   └─ 访问 module.foo  ──►  scope.get("foo")
                                                              （本质是一次 Scope 查找）
```

`Module` 内部用 `Arc` 引用计数，克隆廉价；两个 `Module` 相等当且仅当指向同一个内部对象（指针相等）。

#### 4.2.3 源码精读

**① `Type` 句柄与元数据。**

[src/foundations/ty.rs:63-65](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/ty.rs#L63-L65) —— `#[ty(scope, cast, since = "0.8.0")]` 标注的 `pub struct Type(Static<NativeTypeData>);`。`Static` 是指向 `'static` 数据的轻量引用，所以 `Type` 是 `Copy`/廉价的。

[src/foundations/ty.rs:67-133](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/ty.rs#L67-L133) —— `Type` 的一组访问器：`short_name()`（代码里的名字，如 `str`）、`long_name()`（诊断里用的名字，如 `string`）、`title()`（文档标题，如 `String`）、`docs()`、`constructor()`（返回该类型的构造器函数）、`scope()`（附属作用域）、`field()`（从作用域取一个字段）。注意 [src/foundations/ty.rs:123-132](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/ty.rs#L123-L132)：**`Type::field` 的实现就是去自己的 `scope()` 里 `get(field)`**——`str.len` 这种访问，本质是查类型的作用域。

[src/foundations/ty.rs:194-231](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/ty.rs#L194-L231) —— `NativeType` trait 与 `NativeTypeData` 结构。后者是「类型的全部静态元数据」：`name`/`long_name`/`title`/`since`/`docs`/`def_site`/`keywords`/`constructor`(LazyLock)/`scope`(LazyLock)。构造器和作用域都用 `LazyLock`，即**第一次用到才初始化**。

**② `type()` 构造器。**

[src/foundations/ty.rs:135-155](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/ty.rs#L135-L155) —— `#[scope] impl Type` 里的 `#[func(constructor, since = "0.8.0")] pub fn construct(value: Value) -> Type { value.ty() }`。这就是用户写的 `type(x)`：接收任意 `Value`，返回它的类型。`#[func(constructor)]` 表示它既是 `type` 类型的作用域成员，也是 `type` 的构造器（于是 `type(12)` = 调用构造器）。

**③ `define_type`：把类型注入作用域。**

[src/foundations/scope.rs:151-156](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L151-L156) —— `define_type::<T>`：取 `T::ty()` 得到 `Type`，再用 `short_name()` 作为名字、`Type` 作为值调 `define`。于是装配后，全局作用域里就有 `"str" → Value::Type(...)` 这一条绑定。

[src/foundations/mod.rs:91-113](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/mod.rs#L91-L113) —— foundations 分类的装配：连续 `define_type::<bool>()`、`::<i64>()`、`::<Str>()` ……把所有基础类型注册进全局。这就是「为什么 `int`、`str` 这些名字在 Typst 里可用」的源头。

**④ `Module` 结构与访问。**

[src/foundations/module.rs:47-65](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/module.rs#L47-L65) —— `#[ty(cast, since = "forever")] pub struct Module { name, inner: Arc<ModuleInner> }`；`ModuleInner { scope, content, file_id }`。一个模块 = 名字 + 作用域 + 可排版内容 + 可选的来源文件 id。

[src/foundations/module.rs:67-90](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/module.rs#L67-L90) —— 两种构造方式：`Module::new(name, scope)`（有名字，内置模块用）和 `Module::anonymous(scope)`（匿名）。还有一系列 `with_*` builder 方法。

[src/foundations/module.rs:139-147](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/module.rs#L139-L147) —— `Module::field`：访问 `module.foo` 时，**直接委托 `self.scope().get(field)`**，找不到就报 `module X does not contain Y`。这印证了「模块访问 = 作用域查找」。

[src/foundations/module.rs:177-181](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/module.rs#L177-L181) —— `PartialEq`：两个模块相等当且仅当名字相同**且 `Arc::ptr_eq`**（指向同一份内部数据）。所以模块比较是 O(1) 指针比较，不逐字段比较内容。

[src/foundations/value.rs:663](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/value.rs#L663) —— `primitive! { Module: "module", Module }`：`Module` 也是一个一等类型（`type(sys)` 返回 `module`），并能在 `Value::Module` 与 `Module` 间互转。

#### 4.2.4 代码实践

**实践（源码追踪型）：追踪一个类型从 Rust 到用户可见名字的完整路径。**

1. **实践目标**：把「Rust 类型 → Typst 名字」的链条走通。
2. **操作步骤**：
   - 以 `Str` 为例。它由 [src/foundations/value.rs:636-640](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/value.rs#L636-L640) 的 `primitive! { Str: "string", Str, ... }` 注册为类型（类型名 `string`，变体 `Value::Str`）。
   - 在 [src/foundations/mod.rs:96](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/mod.rs#L96) 看到 `global.define_type::<Str>();`。
   - 追到 [src/foundations/scope.rs:151-156](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L151-L156)：`define_type::<Str>` 取 `Str::ty()`（一个 `Type`），用 `short_name()` 作为键写入 `Scope`。
3. **需要观察的现象**：理清「`primitive!` 定义类型的变体与提升规则」和「`define_type` 把类型名挂进作用域」是**两个独立步骤**——前者管 `Value::Str ↔ Str`，后者管「`str` 这个名字指向谁」。
4. **预期结果**：你能解释用户写 `str.len("abc")` 时发生什么——`str` 经 `Scopes::get` 找到 `Value::Type(Str 的 Type)`，`.len` 经 [src/foundations/ty.rs:123-132](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/ty.rs#L123-L132) 的 `Type::field` 在 `Str` 的附属作用域里查到 `len` 函数。
5. **关于类型名**：`primitive!` 里写的 `string` 是文档展示用的名字，而 `define_type` 用的是 `Type::short_name()`（取 `NativeTypeData::name`，对 `Str` 实际是 `str`）。两者的精确对应需对照 `src/foundations/str.rs` 里 `#[ty(...)]` 的标注确认；若不确定，标注「待确认：以 str.rs 中 `#[ty]` 的 name 为准」。

#### 4.2.5 小练习与答案

**练习 1**：`Type::field` 和 `Module::field` 的实现有什么共同点？
> **答案**：两者最终都委托给一个 `Scope::get(field)` 查找。类型有「附属作用域」，模块本身包着一个作用域，所以 `a.b` 这种点取，底层统一是一次 `Scope` 查找。

**练习 2**：为什么 `Module` 的 `PartialEq` 用 `Arc::ptr_eq` 而不是逐字段比较？
> **答案**：`Module` 内部是 `Arc<ModuleInner>`，引用计数让克隆廉价；用指针相等判断「是否同一个模块」是 O(1) 且语义清晰（两个文件各自 import 同一个包，得到的 Module 实例可视为同一份）。逐字段比较既慢又会递归比较 `scope`/`content`，没必要。

**练习 3**：`NativeTypeData` 的 `constructor` 和 `scope` 为什么是 `LazyLock`？
> **答案**：很多类型在编译里根本用不到构造器或方法（比如你只读不构造）。`LazyLock` 让这些元数据**首次访问才初始化**，避免在装配期为所有类型都构建一遍，省启动时间和内存。

---

### 4.3 Scope / Binding / Scopes

#### 4.3.1 概念说明

如果说 `Type`/`Module` 是「把定义打包」，那么 `Scope` 就是定义实际存放的地方：一张**有序**的「名字 → 绑定」映射。`Binding` 是这张表里的一条记录，除了值本身，还带 span、分类、是否只读、是否已弃用等元信息。

需要区分两个名字相近的类型：

- **`Scope`**（单数）：一张名字表。装配期的 `define_type`/`define_func` 都往这里写；它也作为 `Module`/`Type` 的内部存储。
- **`Scopes`**（复数，带生命周期 `'a`）：**求值期的变量查找栈**。它持有当前活动作用域 `top`、外层作用域栈 `scopes`，以及一个指向标准库 `Library` 的 `base` 引用。

为什么要分两层？因为「定义在哪里登记」（装配期，单张 `Scope`）和「运行时按名字找到它」（求值期，多层 `Scopes` 含回退）是两个不同的问题。`Scopes` 的关键能力是**回退**：局部找不到就去外层找，外层找不到就去标准库找。

#### 4.3.2 核心流程

**装配期：往 `Scope` 里写。**

```
scope.define("name", value)
   └─► Binding::detached(value)   记下当前 category
        └─► bind(name, binding)   写入 IndexMap（保留插入序）

更专业的入口：
  define_func::<T>()   ─► define(T::data().name, Func)
  define_type::<T>()   ─► define(T::ty().short_name(), Type)
  define_elem::<T>()   ─► define(T::ELEM.name(), elem)
```

**求值期：`Scopes::get` 的查找链（本讲重点）。**

```
查找变量 var:
  1. 当前活动作用域 self.top                ┐
  2. 由内向外遍历 self.scopes（.rev()）      ├─ 用户/局部作用域
                                             ┘
     └─ 都没找到？回退到 base（标准库 Library）:
        3a. base.global.scope().get(var)     ── 全局标准库（code 模式）
        3b. 特例：var == "std"  ──► 直接返回 &base.std
     └─ 还是没有 ──► Err(unknown_variable(var))
```

数学模式下走 `get_in_math`，回退目标换成 `base.math.scope()`（数学有自己的命名空间）。

**只读约束**：`get_mut`（可变访问）**不允许**改动标准库里的常量——若变量存在于 `base.global` 或就是 `std`，会返回 `cannot mutate a constant`。这就是为什么你不能重新赋值内置的 `int`（普通 `let` 同名遮蔽则另有 `check_std_shadowed` 检查）。

#### 4.3.3 源码精读

**① `Scopes` 求值栈。**

[src/foundations/scope.rs:16-25](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L16-L25) —— `struct Scopes<'a> { top: Scope, scopes: Vec<Scope>, base: Option<&'a Library> }`。`base` 就是指向标准库的「最终回退点」。

[src/foundations/scope.rs:33-43](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L33-L43) —— `enter`/`exit`：进入/退出一层作用域。`enter` 把当前 `top` 存进 `scopes` 并换一张空表；`exit` 弹出恢复。这就是 `#{ ... }`、函数体、`let` 块的作用域嵌套机制。

**② `Scopes::get` 的回退链（本讲指定实践的重点）。**

[src/foundations/scope.rs:45-59](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L45-L59) —— 逐行看 `get`：

1. `std::iter::once(&self.top).chain(self.scopes.iter().rev()).find_map(|scope| scope.get(var))`：先在 `top`，再从内到外遍历 `scopes`，找到就返回。
2. `.or_else(|| { let base = self.base?; match base.global.scope().get(var) { Some(b) => Some(b), None if var == "std" => Some(&base.std), None => None } })`：局部全 miss 时回退到标准库全局作用域；**特判 `std`**——任何情况下 `std` 这个名字都返回 `&base.std`。
3. `.ok_or_else(|| unknown_variable(var))`：仍找不到才报「unknown variable」。

[src/foundations/scope.rs:61-73](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L61-L73) —— `get_mut`：同样的局部查找，但**不回退取值**，只在判断错误类型时检查 `base`——若发现在标准库里，就报 `cannot_mutate_constant`。即标准库常量只读。

[src/foundations/scope.rs:75-94](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L75-L94) —— `get_in_math`：与 `get` 同构，但回退到 `base.math.scope()`，且找不到时的错误信息（`unknown_variable_math`）会贴心地提示「在数学里要多加空格 / 用引号 / 用 `#` 切到 code 模式」。

**③ `std` 这个特殊绑定从哪来。**

[src/lib.rs:179-180](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L179-L180) —— `Library` 有一个 `pub std: Binding` 字段。

[src/lib.rs:221-234](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L221-L234) —— `build()` 里由 `std: Binding::detached(global)` 赋值——即 **`std` 这个绑定的值，正是全局库 `global` 模块本身**。所以 `std.table` 等价于直接在全局作用域里查 `table`：`std` 是全局库的自引用别名。

**④ `Scope` 单张表与 `define`。**

[src/foundations/scope.rs:105-111](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L105-L111) —— `struct Scope { map: IndexMap<EcoString, Binding, FxBuildHasher>, deduplicate, category }`。用 `IndexMap` 是为了**保留插入顺序**（文档生成、稳定迭代都依赖它）；`FxBuildHasher` 是 rustc 的高速哈希。

[src/foundations/scope.rs:175-185](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L175-L185) —— `define(name, value)`：构造 `Binding::detached(value)`，打上当前 `category`，再 `bind`。debug 模式下若开了 `deduplicate` 且重名会直接 panic（防止装配期重复注册）。返回 `&mut Binding` 方便链式调用（如 `.deprecated(...)`）。

[src/foundations/scope.rs:135-163](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L135-L163) —— 三个语义化入口：`define_func::<T>`、`define_type::<T>`、`define_elem::<T>`，分别用各自的「标准名字」调 `define`。

[src/foundations/scope.rs:189-217](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L189-L217) —— `bind`/`get`/`get_mut`/`iter`：底层就是 `IndexMap` 的 entry 操作。

**⑤ `Binding` 与只读捕获。**

[src/foundations/scope.rs:248-261](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L248-L261) —— `struct Binding { value, kind, span, category, deprecation }`。`kind` 区分 `Normal`（可写）与 `Captured(Capturer)`（闭包/上下文捕获的只读副本）。

[src/foundations/scope.rs:295-327](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L295-L327) —— `read()` 直接返回值；`read_checked(sink)` 会在读取时按需发弃用警告；`write()` 对 `Captured` 绑定报错（这就是「闭包外变量只读」的来源，错误信息在 [src/foundations/scope.rs:315-327](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L315-L327)）。

#### 4.3.4 代码实践

**实践（本讲指定的第二个核心实践，源码阅读型）：解释 `Scopes::get` 如何回退到标准库与 `std`。**

1. **实践目标**：能复述变量查找的三级回退，并解释 `std` 的特殊性。
2. **操作步骤**：
   - 读 [src/foundations/scope.rs:45-59](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L45-L59) 的 `get`。
   - 跟踪用户在空文档里写 `#table()` 时，名字 `table` 是怎么被找到的：当前 `top`/`scopes` 都是空的（用户没定义），于是回退到 `base.global.scope().get("table")`，命中（因为 `model/table.rs` 的装配调用了 `define_elem::<TableElem>()`，把 `"table"` 写进了全局 `Scope`）。
   - 再跟踪 `#std.table()`：`std` 在第 2 步特判里直接返回 `&base.std`（其值是全局模块），随后 `.table` 经 [src/foundations/module.rs:139-147](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/module.rs#L139-L147) 的 `Module::field` 又查一次全局作用域。
3. **需要观察的现象**：`table` 与 `std.table` 走的是两条不同路径，但最终落到**同一个** `Binding`（都在全局 `Scope` 里）。这验证了「`std` 是全局库的自引用」。
4. **预期结果**：你能解释——即使没有任何 `import`，`table` 也能用，是因为 `Scopes::get` 的标准库回退；而 `std` 总能用，是因为它被硬编码在回退逻辑里。
5. 若想进一步验证只读约束：读 [src/foundations/scope.rs:61-73](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L61-L73)，说明为什么 `#(int = 5)` 这类对标准库名字的赋值会失败（`cannot_mutate_constant`）。具体报错信息「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`Scopes::get` 查找变量时的顺序是什么？为什么用 `scopes.iter().rev()`？
> **答案**：顺序是 `top` → `scopes` 由内向外 → `base.global`（标准库）→ 特判 `std`。用 `.rev()` 是因为 `scopes` 是一个栈，**后 push 的是更内层**的作用域（见 `enter` 把当前 `top` push 进去），要从最内层往外找，就要逆序遍历，这样才能正确实现「内层遮蔽外层」。

**练习 2**：为什么 `std` 不需要写在全局 `Scope` 里，而是单独特判返回 `&base.std`？
> **答案**：因为 `std` 的值就是「全局库模块本身」（见 [src/lib.rs:231](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L231) `Binding::detached(global)`）。把它作为 `Library` 的一个独立字段 `std: Binding`，并在 `get` 里特判，既避免了循环构造（`global` 模块里再塞一个指向自己的条目），也让 `std` 永远可用、不受用户遮蔽影响。

**练习 3**：`Binding::write` 在什么情况下会失败？
> **答案**：当 `Binding` 的 `kind` 是 `Captured(_)` 时。闭包或上下文表达式从外层捕获的变量是只读副本，对它赋值会报「variables from outside the function/context are read-only」。`Normal` 绑定则可写（前提是它不在只读的标准库作用域里——后者由 `Scopes::get_mut` 拦截）。

---

## 5. 综合实践

把三个最小模块串起来，做一个**「定义的一生」追踪任务**：跟踪一个标准库函数从「Rust 源码」到「用户在文档里调用」的完整生命。

以 `panic` 函数（foundations 分类里的一个原生函数）为例：

1. **定义与标注**：在 [src/foundations/mod.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/mod.rs) 里找到它的 `#[func]` 定义（搜索 `pub fn panic`），看清它的参数签名——这些参数类型都实现了 `FromValue`（4.1）。
2. **注册进作用域**：在 [src/foundations/mod.rs:115](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/mod.rs#L115) 看到 `global.define_func::<panic>();`，它经 [src/foundations/scope.rs:136-140](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L136-L140) 把名字 `panic` 和对应的 `Func` 写进全局 `Scope`（4.3）。
3. **打包成库**：这个全局 `Scope` 在 [src/lib.rs:224](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L224) 被 `global(...)` 包成 `Module`，存进 `Library.global`；同时 `Library.std = Binding::detached(global)`（4.3 的 `std` 特判）。
4. **运行时被找到**：用户写 `#panic("oops")` 时，求值器的 `Scopes::get("panic")` 局部 miss，回退到 `base.global.scope().get("panic")` 命中（4.3）。
5. **参数被转换**：调用时，`"oops"` 这个 `Value::Str` 经 `Args` 解析，用 `FromValue::from_value` 还原成 `panic` 函数签名要求的 Rust 类型（4.1）；若类型不符，`CastInfo::error` 负责生成报错。

**交付物**：画一张时序图（或写一份编号清单），标注上面 5 个阶段各自涉及的源码文件与行号。这张图能把本讲的「转换—类型/模块—作用域」三块知识连成一条线。

> 若你想做扩展：把 `panic` 换成某个**元素**（如 `heading`，见 u3-l3），重复上述追踪。元素走的是 `define_elem` 而非 `define_func`，但「注册进 Scope → 被回退查找」的后半段完全一样。这能帮你体会函数与元素在装配层面的统一性。

## 6. 本讲小结

- Typst 的类型转换是**三段式**：`Reflect`（描述）、`IntoValue`（构造，不失败）、`FromValue`（解析，可能失败且要求 `Reflect`）。不用 `From`/`TryFrom` 是为了避免 impl 冲突和满天 `.into()`。
- `CastInfo` 是「描述性类型」（`Any`/`Value`/`Type`/`Union`），同时服务于文档、自动补全和「expected X, found Y」错误信息；`Reflect::castable` 是同义的快速判断。
- `cast!` 宏为非标量类型生成三件套，支持字面量分支（`"x" => V`）、类型输入分支（`v: T => ...`）和输出分支（`self => ...`），可只写需要的方向。
- `Type` 是「类型的句柄」，指向静态元数据 `NativeTypeData`（名字/文档/构造器/作用域），由 `#[ty]` 生成、由 `define_type` 注入作用域；`type(x)` 构造器就是返回 `value.ty()`。
- `Module` 是「带名字的作用域 + 可选内容 + 文件 id」，用 `Arc` 引用计数、指针判等；访问 `module.foo` 本质是一次 `Scope::get`。
- `Scope` 是有序（`IndexMap`）的名字→`Binding` 映射；`Scopes` 是求值期的查找栈，`get` 按「top → 外层 → 标准库全局 → 特判 `std`」回退；标准库常量只读，`std` 是全局库的自引用别名。

## 7. 下一步学习建议

本讲完成了「值与类型基础」单元，把 `Value`（u2-l1）、容器（u2-l2）和转换/命名空间（本讲）三块拼齐。接下来进入 **u3「Content 与元素系统」**：

- **u3-l1（Content 与 RawContent）** 会讲 `Value::Content` 背后的核心类型——它正是本讲多次提到的「内容」的具体表示，且 `Content` 也是一个带 `#[ty]` 的一等类型。
- **u3-l3（elem 宏、字段系统与 Packed）** 会深入 `#[elem]` 宏。元素的字段（required/default/ghost/fold/parse）大量用到本讲的 `cast!` 和 `FromValue`——每个字段都是一个会被 `cast` 的类型。读完 u3-l3 你会发现本讲的转换系统是元素字段解析的直接基石。
- **u4-l1（Styles、StyleChain 与 fold/resolve）** 会用到本讲提到的 `Fold` trait（见 [src/foundations/cast.rs:575-582](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/cast.rs#L575-L582) 里 `Derived` 的 `Fold` 实现），把样式字段如何「折叠」讲透。

建议在进入 u3 之前，回头用本讲「综合实践」的方法，自己追踪一遍 `#panic("...")` 的完整路径——如果每一步都能对上行号，说明本讲已经吃透。

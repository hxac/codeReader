# 用 syn 解析宏输入：DynamicSpacingInput

## 1. 本讲目标

前两单元我们搭建了背景：`derive_dynamic_spacing!` 是一个函数式过程宏，全仓库只有 [crates/ui/src/styles/spacing.rs](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/styles/spacing.rs#L29-L44) 这一个调用点，它在编译期把 14 行间距清单「替换」成一个 `DynamicSpacing` 枚举。本讲我们第一次真正走进 [crates/ui_macros/src/dynamic_spacing.rs](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L1-L167) 的内部，只看**前半段**——解析。

学完本讲你应该能够：

1. 读懂 `DynamicSpacingInput` 与 `DynamicSpacingValue` 两个类型的 `Parse` 实现，理解「先建模、再实现 Parse」的过程宏开发套路。
2. 掌握 `parse_macro_input!`、`Punctuated`、`Token![,]`、`ParseStream` 各自的分工。
3. 理解 `peek`（前瞻不消费）与 `parenthesized!`（括号子流）这对组合如何区分 `24` 和 `(1, 2, 4)` 两种输入形态。
4. 动手在 scratch crate 里写一个同构的解析宏，并用故意写错的输入观察 syn 的报错行为。

生成代码的后半段（`quote!` 模板）属于下一讲 u2-l2，本讲不展开。

## 2. 前置知识

### 2.1 承接前两讲

- u1-l2 已讲过：过程宏本质是「TokenStream 进、TokenStream 出」的编译期函数，`proc-macro = true` 让 crate 成为编译器插件；存在 `proc_macro` 与 `proc_macro2` 两套 TokenStream。本讲直接使用这些结论。
- u1-l3 已讲过：`derive_dynamic_spacing!` 的调用点、以及「Cargo 依赖箭头 ≠ 宏展开箭头」——`ui_macros` 只依赖 syn 和 quote，本讲讲的解析代码全部运行在**下游 crate（ui）的编译期**。

### 2.2 syn 是什么

syn（zed workspace 使用 `syn 2.0.101`）是把 token 流解析成 Rust 语法树的库。它的定位可以和 quote 对照理解：

| 库 | 方向 | 输入 → 输出 |
| --- | --- | --- |
| syn | 解析 | `TokenStream` → 语法树（`LitInt`、`DeriveInput`、`Punctuated` 等） |
| quote | 生成 | 数据 → `TokenStream`（拼代码） |

本讲只涉及 syn。更准确地说，本讲只用 syn 的**自定义解析**能力：不是解析任意 Rust 代码，而是定义「我的宏输入长什么样」，然后教 syn 怎么读。

### 2.3 ParseStream：一个只能向前走的游标

`ParseStream`（实际类型是 `&syn::parse::ParseBuffer`）是 syn 最核心的抽象。把它想象成一条「游标 + 便签」：

- 游标指向 token 流中尚未消费的位置，只能向前走（消费）。
- 每次尝试解析都有**事务性**：如果一次 `parse::<T>()` 失败，游标会自动退回尝试前的位置，就像什么都没发生过。
- 「便签」记录错误。解析失败时，syn 能给出「在哪个位置、期待什么」的错误，而不是直接 panic。

这个「失败自动回滚」的特性是后面 `peek` 与乐观解析的基础，请先记住它。

### 2.4 一个容易困惑的点：调用定界符不进入宏

`spacing.rs` 里用方括号调用宏：`derive_dynamic_spacing![...]`。对于函数式过程宏（`#[proc_macro]`），**调用时写的是 `!()`、`![]` 还是 `!{}` 都不影响输入**：进入宏函数的 TokenStream 只包含定界符**内部**的内容，定界符本身被编译器剥掉。

所以宏真正看到的输入是：

```
( 0 , 0 , 0 ) , ( 1 , 1 , 2 ) , … , ( 18 , 20 , 22 ) , 24 , 32 , 40 , 48
```

一串「裸」的 token，没有外层包裹。这就是为什么解析代码第一件事就是处理「逗号分隔的列表」。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| [crates/ui_macros/src/dynamic_spacing.rs](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L1-L50) | 本讲主角 | 第 1–50 行：数据建模 + 两个 `Parse` 实现 + 入口两行 |
| [crates/ui_macros/src/ui_macros.rs](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/ui_macros.rs#L6-L10) | 宏声明入口 | 4 行转发：`#[proc_macro]` 声明 + 调用 `derive_spacing` |
| [crates/ui/src/styles/spacing.rs](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/styles/spacing.rs#L29-L44) | 唯一调用点 | 提供 14 个真实输入值，是理解「输入长什么样」的样本 |

## 4. 核心概念与源码讲解

### 4.1 先建模再解析：DynamicSpacingInput 与 DynamicSpacingValue

#### 4.1.1 概念说明

宏输入是一串无结构的 token，直接对 token 做判断非常痛苦。syn 的做法是**先定义你想要的数据形状（普通 Rust 类型），再实现 `Parse` trait 告诉 syn 怎么把 token 流填进这个形状**。这是编写非平凡过程宏的标准套路，记住这个顺序：**建模在先，解析在后，生成最后**。

本讲的「形状」由两个类型构成：

- `DynamicSpacingInput`：整个宏输入，即「一个逗号分隔的列表」。
- `DynamicSpacingValue`：列表中的**一个**元素，只能是两种形态之一，所以用枚举：
  - `Single(LitInt)`：单个整数，如 `24`。三档密度值（Compact/Default/Comfortable）不由用户给定，而是后续按公式 \(n-4 \mid n \mid n+4\) 推导（公式细节属于 u3 单元）。
  - `Tuple(LitInt, LitInt, LitInt)`：三元整数组，如 `(1, 2, 4)`。三个值直接就是三档密度下的像素值。

`LitInt` 是 syn 对「整数字面量」的语法树类型——注意它保存的是**字面量本身**（含 span 信息），不是一个数值，把 `"24"` 变成 `24u32` 是稍后 `base10_parse` 的事（u2-l2 与 u5-l1 会讨论）。

两种形态的语义差异用真实输入对照：

| 输入形态 | 变体 | spacing.rs 中的真实例子 | 语义 |
| --- | --- | --- | --- |
| `24` | `Single(24)` | `24`、`32`、`40`、`48`（第 40–43 行） | 只给 Default 档像素值，另外两档由公式推导 |
| `(1, 2, 4)` | `Tuple(1, 2, 4)` | `(0, 0, 0)` 到 `(18, 20, 22)` 共 10 组（第 30–39 行） | 三档像素值全部显式给出 |

#### 4.1.2 核心流程

从宏调用到结构化数据的完整链路：

```
derive_dynamic_spacing![ 24 , (1, 2, 4) ]        ← 源码（crates/ui/src/styles/spacing.rs）
        │  编译器剥掉方括号
        ▼
TokenStream: 24 , (1, 2, 4)                       ← 裸 token 流
        │  ui_macros.rs 的 #[proc_macro] 入口转发
        ▼
dynamic_spacing::derive_spacing(input)
        │  parse_macro_input!(input as DynamicSpacingInput)
        ▼
DynamicSpacingInput {
    values: Punctuated[                           ← 逗号分隔的列表
        Single(LitInt "24"),
        Tuple(LitInt "1", LitInt "2", LitInt "4"),
    ]
}
        │  （后续 derive_spacing 函数遍历它生成代码 —— u2-l2 的内容）
        ▼
TokenStream（生成的 DynamicSpacing 枚举）
```

本讲关注的是中间那一段：token 流如何变成 `DynamicSpacingInput`。

#### 4.1.3 源码精读

先看两个类型的定义（本讲范围只有第 1–50 行，第 52 行起是代码生成，留给 u2-l2）：

[crates/ui_macros/src/dynamic_spacing.rs:7-9](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L7-L9) —— 整个宏输入被建模成「一个列表」：

```rust
struct DynamicSpacingInput {
    values: Punctuated<DynamicSpacingValue, Token![,]>,
}
```

`Punctuated<DynamicSpacingValue, Token![,]>` 读作「用逗号分隔的若干个 `DynamicSpacingValue`」。`Token![,]` 是 syn 提供的 token 类型宏，展开为标点类型 `syn::token::Comma`——用**类型**表示「逗号」这个标点，这样分隔符信息就进入类型系统，`Punctuated` 的解析与迭代都能围绕它展开。

[crates/ui_macros/src/dynamic_spacing.rs:11-21](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L11-L21) —— 列表元素的定义与说明注释：

```rust
// When a single value is provided, the standard spacing formula is
// used to derive the of spacing values.
//
// When a tuple of three values is provided, the values are used as
// the spacing values directly.
enum DynamicSpacingValue {
    Single(LitInt),
    Tuple(LitInt, LitInt, LitInt),
}
```

这段注释（含一处原文笔误 "derive the of spacing values"）解释了两种形态的分工，与调用方 [crates/ui/src/styles/spacing.rs:5-15](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/styles/spacing.rs#L5-L15) 的注释互为镜像——**宏的输入语法约定写在两个地方**：实现侧（这里）和调用侧（spacing.rs），阅读时两边对照最有效。

再对照真实输入样本 [crates/ui/src/styles/spacing.rs:29-44](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/styles/spacing.rs#L29-L44)：

```rust
derive_dynamic_spacing![
    (0, 0, 0),
    (1, 1, 2),
    …
    (18, 20, 22),
    24,
    32,
    40,
    48
];
```

14 个元素：前 10 个是 `Tuple`（小间距需要非对称的三档值），后 4 个是 `Single`（大间距用公式推导即可）。注意最后一项 `48` 后面**没有**尾随逗号——这会在 4.3 讨论列表解析时再回来对照。

#### 4.1.4 代码实践

**实践目标**：建立 scratch 工作区，并完成「建模」这一步——只写类型，不写解析，体会模型先于代码。

**操作步骤**（以下均为**示例代码**，在 Zed 仓库之外操作，不要改动仓库源码）：

1. 在任意目录创建练习工作区：

```bash
cargo new scratch-spacing --workspace
cd scratch-spacing
cargo new spacing_macros      # 过程宏 crate
cargo new demo                # 消费宏的普通 crate
```

2. 编辑 `spacing_macros/Cargo.toml`：

```toml
[package]
name = "spacing_macros"
version = "0.1.0"
edition = "2021"

[lib]
proc-macro = true

[dependencies]
syn = { version = "2", features = [] }
quote = "1"
```

3. 在 `spacing_macros/src/lib.rs` 中只写数据模型（暂不实现 `Parse`）：

```rust
use proc_macro::TokenStream;
use syn::{LitInt, Token, punctuated::Punctuated};

// 示例代码：模仿 ui_macros 的建模
struct SpacingInput {
    values: Punctuated<SpacingValue, Token![,]>,
}

enum SpacingValue {
    Single(LitInt),
    Tuple(LitInt, LitInt, LitInt),
}
```

此时 `cargo check -p spacing_macros` 会警告未使用的类型——这是预期的，下一步才用到它们。

**需要观察的现象**：`Punctuated<SpacingValue, Token![,]>` 中 `Token![,]` 出现在**类型位置**；编译通过说明「标点也是类型」这一 syn 设计是真实可用的。

**预期结果**：编译通过（仅有 dead_code 类警告）。完整解析在下两节逐步补全。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `DynamicSpacingValue` 是 enum 而不是两个独立的 struct？

**答案**：列表中每个位置**要么**是单值**要么**是三元组，二者互斥。enum 天然表达「多选一」，且让后续代码必须 `match` 处理所有分支——漏掉一种形态编译器会直接报错，这是用类型系统保证处理完备性的典型手法。

**练习 2**：`Single(LitInt)` 里存的是数值 `24` 吗？

**答案**：不是。`LitInt` 是**字面量语法树节点**，保存字面量的文本表示和它在源码中的位置信息（span）。把字面量转成 `f32`/`u32` 数值是 `base10_parse` 在生成阶段的工作（见 [crates/ui_macros/src/dynamic_spacing.rs:58](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L58)）。保留 `LitInt` 的好处是 span 不丢失，报错能定位到源码位置。

**练习 3**：如果宏调用写成 `derive_dynamic_spacing![24, (1, 2, 4)]`，宏函数收到的 TokenStream 里有没有方括号？

**答案**：没有。函数式过程宏的调用定界符会被编译器剥掉，输入只是定界符**内部**的 token：`24 , (1, 2, 4)`。用 `!()`、`![]`、`!{}` 调用对宏来说完全等价。

### 4.2 parse_macro_input! 与 ParseStream：解析的入口与游标

#### 4.2.1 概念说明

有了模型，还需要一条「驱动路径」。syn 的自定义解析围绕一个 trait 展开：

```rust
// 示例代码：syn::parse::Parse 的签名（见 syn 官方文档）
pub trait Parse: Sized {
    fn parse(input: ParseStream<'_>) -> Result<Self>;
}
```

含义是：「给我一个游标（`ParseStream`），我尝试从当前位置读出一个 `Self`，读不动就返回错误」。`DynamicSpacingInput` 和 `DynamicSpacingValue` 都实现了这个 trait，于是「解析一个列表」可以复用「解析一个元素」——**小的解析器组合成大的解析器**，这正是 syn 的设计思想。

入口处则是 syn 的 `parse_macro_input!` 宏，它一行做了三件事：

1. 调用 `syn::parse::<DynamicSpacingInput>(整个 TokenStream)`，把 `proc_macro::TokenStream` 解析成我们的模型；
2. 解析成功 → 得到结构化的 `DynamicSpacingInput` 值；
3. 解析失败 → **提前 return**，把 syn 错误转成一个 `compile_error!` 调用（错误消息附着在出错 token 的源码位置上）。

它大致等价于（示例代码，展示语义）：

```rust
let input = match syn::parse::<DynamicSpacingInput>(input) {
    Ok(data) => data,
    Err(err) => return err.to_compile_error().into(),
};
```

#### 4.2.2 核心流程

```
derive_spacing(input: TokenStream)
        │
        ├─ parse_macro_input!(input as DynamicSpacingInput)
        │       │
        │       ├─ DynamicSpacingInput::parse(ParseStream)   ← 4.3 展开
        │       │       └─ 逐个调用 DynamicSpacingValue::parse ← 4.4 展开
        │       │
        │       ├─ Ok → input: DynamicSpacingInput（继续往下走）
        │       └─ Err → return 编译错误（带源码位置的波浪线）
        ▼
  input.values.iter() … 生成代码（u2-l2 的内容）
```

注意失败路径：**解析错误不是 panic**，而是一个正常的提前返回，用户在 IDE 里看到的是画在宏调用上的红色波浪线和错误消息。这与 u5-l1 要讨论的 `unwrap()` 问题形成对照——`parse_macro_input!` 是「好错误」，后面的 `base10_parse().unwrap()` 是「坏崩溃」。

#### 4.2.3 源码精读

文件开头的 use 汇总了本讲用到的全部 syn 工具：

[crates/ui_macros/src/dynamic_spacing.rs:1-5](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L1-L5) —— 从 syn 导入解析所需的三类东西：类型（`LitInt`）、标点类型宏（`Token`）、解析机制（`Parse`/`ParseStream`/`parse_macro_input`/`Punctuated`）：

```rust
use proc_macro::TokenStream;
use quote::{format_ident, quote};
use syn::{
    LitInt, Token, parse::Parse, parse::ParseStream, parse_macro_input, punctuated::Punctuated,
};
```

（`quote` 和 `format_ident` 属于生成阶段，u2-l2 再用。）

`DynamicSpacingInput` 的 `Parse` 实现：

[crates/ui_macros/src/dynamic_spacing.rs:23-29](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L23-L29) —— 整个输入的解析只有一个调用：读一个「逗号结尾可选的列表」：

```rust
impl Parse for DynamicSpacingInput {
    fn parse(input: ParseStream) -> syn::Result<Self> {
        Ok(DynamicSpacingInput {
            values: input.parse_terminated(DynamicSpacingValue::parse, Token![,])?,
        })
    }
}
```

注意 `parse_terminated` 的第一个参数是**函数指针** `DynamicSpacingValue::parse`——把「元素的解析器」作为参数传给「列表的解析器」，解析器组合的直接体现。第二个参数 `Token![,]` 此处是**值**（`token::Comma` 类型的单元值），同一个宏在类型位置和值位置各出现一次。

入口两行：

[crates/ui_macros/src/dynamic_spacing.rs:48-50](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L48-L50) —— `derive_spacing` 是内部实现函数，第一行就把 TokenStream 换成了模型：

```rust
pub fn derive_spacing(input: TokenStream) -> TokenStream {
    let input = parse_macro_input!(input as DynamicSpacingInput);
```

而它由 [crates/ui_macros/src/ui_macros.rs:6-10](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/ui_macros.rs#L6-L10) 的 `#[proc_macro]` 入口原样转发（u1-l2 讲过的「薄入口」模式）：

```rust
#[proc_macro]
pub fn derive_dynamic_spacing(input: TokenStream) -> TokenStream {
    dynamic_spacing::derive_spacing(input)
}
```

#### 4.2.4 代码实践

**实践目标**：给 scratch 宏接上 `parse_macro_input!`，先只支持「单个整数」一种形态，验证解析错误能以编译错误（而非 panic）的形式出现。

**操作步骤**（示例代码，续 4.1.4 的工程）：

1. 修改 `spacing_macros/src/lib.rs`，实现一个最小宏：

```rust
use proc_macro::TokenStream;
use syn::{LitInt, Token, parse::Parse, parse::ParseStream, parse_macro_input, punctuated::Punctuated};

// 示例代码：最小解析宏，只接受单个整数
struct SpacingInput {
    values: Punctuated<LitInt, Token![,]>,
}

impl Parse for SpacingInput {
    fn parse(input: ParseStream) -> syn::Result<Self> {
        Ok(SpacingInput {
            values: input.parse_terminated(LitInt::parse, Token![,])?,
        })
    }
}

#[proc_macro]
pub fn parse_demo(input: TokenStream) -> TokenStream {
    let input = parse_macro_input!(input as SpacingInput);
    // 暂时生成一个空实现，下一节再回显解析结果
    quote::quote! {}
}
```

2. 在 `demo/src/main.rs` 里使用它，并在 `demo/Cargo.toml` 加上 `spacing_macros = { path = "../spacing_macros" }`：

```rust
parse_demo![24, 32];
fn main() {}
```

3. `cargo check -p demo` 通过后，把调用改成 `parse_demo![24, x];` 再 check 一次。

**需要观察的现象**：第二次 check 失败，错误消息类似 `expected integer literal`，且 IDE 中出错位置有红色波浪线标在 `x` 上（具体措辞与位置以本地编译输出为准，待本地验证）。

**预期结果**：合法输入通过；非法输入得到一条**带位置的编译错误**——这正是 `parse_macro_input!` 提前返回路径的效果。对比一下：如果这里 panic!，用户看到的会是晦涩的 "proc macro panicked" 而不是指向 `x` 的错误。

#### 4.2.5 小练习与答案

**练习 1**：`parse_macro_input!(input as SpacingInput)` 失败时函数发生了什么？

**答案**：它提前 `return`，返回值是错误转成的 `compile_error!{"..."}` token 流。因为过程宏函数的返回类型就是 `TokenStream`，这个提前返回在类型上是合法的——syn 用宏语法把「返回错误」伪装成了一行表达式。

**练习 2**：`Parse` trait 的 `parse` 关联函数签名里没有 `&self`，为什么？

**答案**：因为 `parse` 的输入是 `ParseStream`（待读的 token 流），输出才是 `Self`。它是「工厂方法」而非「方法」：类型本身还没有实例，当然不能接收 `&self`。这也解释了为什么可以把 `DynamicSpacingValue::parse` 当函数指针直接传给 `parse_terminated`。

**练习 3**：`Token![,]` 在 4.2.3 的两处出现分别处于什么位置？

**答案**：在 `Punctuated<DynamicSpacingValue, Token![,]>`（类型位置，作为泛型参数声明分隔符类型）和 `input.parse_terminated(..., Token![,])`（值位置，作为标记值传入）。syn 的 `token!` 宏生成的类型既能当泛型参数，又有单例值。

### 4.3 Punctuated 与 parse_terminated：逗号分隔的列表

#### 4.3.1 概念说明

`Punctuated<T, P>` 表示「用 P 分隔的 T 序列」，是 syn 里最常用的容器之一，因为宏输入几乎总是列表（本讲的间距值、宏的属性参数、函数参数列表……）。它解决了三个问题：

1. **分隔符处理**：自动处理「元素之间必须有逗号、每个元素之后可能有逗号」的繁琐逻辑；
2. **与生成衔接**：`Punctuated` 可以直接被 `quote!` 迭代展开（u2-l2 会用到）；
3. **语义明确**：`parse_terminated` 与 `parse_separated_nonempty` 等不同方法对应不同的列表语法约定。

`parse_terminated` 的具体语义是：

- 元素之间**必须**有分隔符；
- 列表**末尾允许**有一个分隔符（即「terminated」——分隔符可以终止列表）；
- **空列表合法**（零个元素也返回 Ok）；
- 元素后面若既没有分隔符也没有结束，解析停止，交由上层判断。

为什么过程宏输入偏爱 `parse_terminated`？因为用户写宏调用时常有尾随逗号的习惯（多行列表加一行值方便增删），允许尾随逗号对使用者最友好；`spacing.rs` 的输入末项 `48` 没有尾随逗号，但即使用户写了，也能正常解析。

#### 4.3.2 核心流程

`parse_terminated(DynamicSpacingValue::parse, Token![,])` 的行为可以写成这样的伪代码：

```
values = []
loop {
    尝试解析一个元素（DynamicSpacingValue::parse）
        ├─ 立即失败 → 停止循环（若 values 为空，返回空列表，合法）
        └─ 成功 → push 进 values
    前瞻下一个 token 是不是逗号？
        ├─ 是 → 消费逗号，继续循环
        └─ 否 → 停止循环
}
返回 values
```

对照真实输入（剥掉定界符后）走一遍：

```
(0, 0, 0) , (1, 1, 2) , … , 24 , 32 , 40 , 48
    │           │              │
 Tuple      Tuple  ……       Single ×4      ← 每个元素由 4.4 的逻辑判定形态
```

停止循环后，若 token 流还有剩余（例如输入 `24 32` 缺逗号），外层 `syn::parse` 会发现流未耗尽而报错——`parse_terminated` 自己不负责「必须读完」这件事。

一个值得记录的边界情况：输入**完全为空**（`parse_demo![]`）时 `parse_terminated` 返回空列表、解析成功。对 `derive_dynamic_spacing!` 而言，空输入会生成一个没有任何变体的空枚举——宏本身不报错，错误会推迟到下游使用不存在的变体时才出现（待本地验证）。

#### 4.3.3 源码精读

[crates/ui_macros/src/dynamic_spacing.rs:23-29](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L23-L29) —— 本讲的「列表解析器」全貌（4.2.3 已贴出代码，这里换一个视角）：`DynamicSpacingInput::parse` 的函数体只有一句 `parse_terminated` 调用，说明**整个宏的顶层语法就是「逗号分隔的列表」**，再无其他结构。任何复杂度都被下推到元素解析器（4.4）里。

对照调用方数据 [crates/ui/src/styles/spacing.rs:29-44](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/styles/spacing.rs#L29-L44)：14 个元素、13 个分隔符、无尾随逗号。把这 14 行喂给 `parse_terminated`，得到的 `Punctuated` 长度为 14，其中前 10 个元素是 `Tuple`、后 4 个是 `Single`——这就是解析阶段的全部产出，生成阶段（u2-l2）将逐个遍历它们。

#### 4.3.4 代码实践

**实践目标**：验证 `parse_terminated` 的三条语义（尾随逗号、空列表、缺逗号）。

**操作步骤**（示例代码，续上一节工程，先给宏加上「回显」能力——用编译错误把解析结果打印出来，这是过程宏开发里最实用的调试技巧之一）：

1. 把 `parse_demo` 的生成部分改成「把元素个数回显成编译错误」：

```rust
#[proc_macro]
pub fn parse_demo(input: TokenStream) -> TokenStream {
    let input = parse_macro_input!(input as SpacingInput);
    let count = input.values.len();
    // 示例代码：故意报错，用编译错误回显宏在编译期看到的数据
    syn::Error::new(
        proc_macro2::Span::call_site(),
        format!("parsed {} values", count),
    )
    .to_compile_error()
    .into()
}
```

（需在 `Cargo.toml` 的依赖里加上 `proc-macro2 = "1"`，并在 lib.rs 顶部补 `use`。）

2. 依次修改 `demo/src/main.rs` 的调用并 `cargo check -p demo`，记录错误消息：
   - `parse_demo![24, 32, 40,];`（尾随逗号）
   - `parse_demo![];`（空输入）
   - `parse_demo![24 32];`（缺逗号）
   - `parse_demo![24, 32];`（普通情况，作对照）

**需要观察的现象**：四种情况应分别观察到 `parsed 4`（尾随逗号被允许）、`parsed 0`（空列表合法）、第三种情况**看不到** `parsed N` 而是类似 `unexpected token` 的 syn 报错（缺逗号导致流未耗尽）、第四种 `parsed 2`。

**预期结果**：

| 输入 | 预期错误消息 | 说明 |
| --- | --- | --- |
| `24, 32, 40,` | `parsed 4` | 尾随逗号允许 |
| `[]`（空） | `parsed 0` | 空列表合法 |
| `24 32` | 类似 `unexpected token`（措辞待本地验证） | 流未耗尽，`parse_terminated` 停止后由外层报错 |
| `24, 32` | `parsed 2` | 正常 |

**注意**：这个「故意报错回显」的技巧只用于调试——它让你的宏永远编译失败，却能打印出宏在编译期掌握的一切中间数据，相当于在编译期打日志。调试完记得还原。

#### 4.3.5 小练习与答案

**练习 1**：`parse_terminated` 与「要求至少一个元素」的语义有何不同？

**答案**：`parse_terminated` 允许空列表（返回空 `Punctuated`）；syn 另有 `Punctuated::parse_separated_nonempty` 要求至少一个元素。`derive_dynamic_spacing!` 选了前者，所以空输入在解析层面是合法的。

**练习 2**：输入 `24 32`（缺逗号）时报错来自哪一层？

**答案**：不是 `parse_terminated` 本身——它在解析完 `24` 后发现下一个 token 不是逗号，就正常停止并返回一个元素的列表。报错来自更外层的 `syn::parse`（由 `parse_macro_input!` 调用）：它要求整个 TokenStream 被完全消费，发现还剩 `32` 未消费，于是报错。

**练习 3**：为什么 `spacing.rs` 的调用末尾 `48` 后没写逗号，但说「用户写了也没关系」？

**答案**：因为解析用的是 `parse_terminated`，它允许列表以分隔符结尾。分隔符处理被容器统一抽象，调用方无论写不写尾随逗号，解析结果都一样。

### 4.4 peek 与 parenthesized!：前瞻分流与括号子流

#### 4.4.1 概念说明

现在处理最难的一步：列表元素有两种形态，`24` 和 `(1, 2, 4)`，如何区分？

不能「先按 Single 解析，失败了再按 Tuple 解析」吗？其实可以——回想 2.3 节 `ParseStream` 的事务性（失败自动回滚）。但「尝试-回滚」只有两个分支都从**同一起点**开始才有效，而本例的两个分支语法上没有公共前缀：一个以整数开头，一个以 `(` 开头。更清晰的做法是**前瞻（peek）分流**：

- `input.peek(syn::token::Paren)`：偷看下一个 token 是不是一个「圆括号组」，**不消费它**。类似下棋前先看一眼对方棋子，手不落子。
- `syn::parenthesized!(content in input)`：确认是括号组之后，把括号**内部**的 token 提取成一个**子 ParseStream**（名为 `content`），并消费掉整个组。注意它同样剥掉定界符——和 2.4 节宏调用剥定界符是同一个道理：括号只是包装，内容才是语法。

子流的价值在于**错误的局部化**：`(1, 2, 4)` 内部的解析错误（比如只有两个数）发生、并定位在 `content` 这条子游标上，报错会精确指向括号内的位置；同时子流自带「必须读完」的检查——如果组内还有 token 没被消费，syn 会在子流被丢弃时记下 `unexpected token` 类错误。这两个行为都会在实践里验证。

`parenthesized!` 的写法是 syn 约定俗成的两行模式：

```rust
let content;
syn::parenthesized!(content in input);
```

先声明、再由宏在 `input` 上执行「打开括号组」并把子游标**赋值**给 `content`。为什么不写成一行返回值？因为该宏需要借住 `input` 的生命周期，Rust 宏语法下「先声明后赋值」是 syn 选择的实现方式，照这个模式写即可。

#### 4.4.2 核心流程

`DynamicSpacingValue::parse` 的决策树：

```
输入下一个 token 是圆括号组吗？（peek，不消费）
    ├─ 是 → 打开括号子流 content：
    │        解析整数 a → 解析逗号 → 解析整数 b → 解析逗号 → 解析整数 c
    │        任一步失败 → 报错（位置在括号内）
    │        全部成功 → Single 的兄弟：Tuple(a, b, c)
    │        （若 content 内还有剩余 token → 稍后报 unexpected token）
    └─ 否 → 乐观解析：直接解析一个整数
             成功 → Single(n)
             失败 → 把 syn 的错误原样上抛（交由上层处理）
```

值得注意 else 分支的「乐观」风格：不再 peek 检查「下一个是不是整数字面量」，而是直接 `input.parse()?`，失败就把错误交给 `parse_macro_input!` 的提前返回路径。对于二选一的末梢分支，这是比 peek 更简洁的惯用写法——反正已经没有别的分支可试了。

#### 4.4.3 源码精读

[crates/ui_macros/src/dynamic_spacing.rs:31-46](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L31-L46) —— 本讲最核心的 16 行，逐段拆解：

```rust
impl Parse for DynamicSpacingValue {
    fn parse(input: ParseStream) -> syn::Result<Self> {
        if input.peek(syn::token::Paren) {
            let content;
            syn::parenthesized!(content in input);
```

`peek(syn::token::Paren)` 判断「下一个 token 是否为圆括号组」。是则用 `parenthesized!` 打开子流。从这里开始，`input` 游标已越过整个 `(…)` 组，组内世界由 `content` 负责。

```rust
            let a: LitInt = content.parse()?;
            content.parse::<Token![,]>()?;
            let b: LitInt = content.parse()?;
            content.parse::<Token![,]>()?;
            let c: LitInt = content.parse()?;
            Ok(DynamicSpacingValue::Tuple(a, b, c))
```

Tuple 分支按「整数、逗号、整数、逗号、整数」的固定次序在子流里依次读取。注意两种取值写法在此并存：

- `let a: LitInt = content.parse()?;` —— 依赖类型标注让 `parse` 的泛型推断出 `LitInt`；
- `content.parse::<Token![,]>()?;` —— 用turbofish 显式指定解析目标是逗号，读到后直接丢弃（`Token![,]` 是零大小的标记类型，它的唯一作用是「确认这里有个逗号」）。

任一步失败（如输入 `(1, 2)` 在第二个逗号处失败、`(1, x, 4)` 在第二个整数处失败），`?` 立即把错误上抛，错误位置来自失败 token 的 span——这就是「括号子流让错误局部化」的具体含义。

```rust
        } else {
            Ok(DynamicSpacingValue::Single(input.parse()?))
        }
    }
}
```

Single 分支只有一行：不 peek、直接解析一个 `LitInt`。若下一个 token 不是整数字面量（比如 `x`），错误上抛给 `parse_terminated`，此时列表解析失败、再上抛给 `parse_macro_input!`，最终变成用户看到的编译错误。

把这 16 行连起来读：**peek 决定「走哪条路」，parenthesized! 决定「在哪里读」，一连串 `parse()?` 决定「按什么次序读」**。解析逻辑的全部复杂度就这三种原语的组合。

#### 4.4.4 代码实践

**实践目标**：完成 Single/Tuple 双形态支持，然后执行本讲规格要求的「非法输入实验」——用故意写错的输入观察并记录 syn 的报错行为。

**操作步骤**（示例代码，续前两节工程）：

1. 在 `spacing_macros/src/lib.rs` 中补全 `SpacingValue` 及其 `Parse`（逐行模仿源码即可）：

```rust
// 示例代码：与 dynamic_spacing.rs 的 DynamicSpacingValue 同构
enum SpacingValue {
    Single(LitInt),
    Tuple(LitInt, LitInt, LitInt),
}

impl Parse for SpacingValue {
    fn parse(input: ParseStream) -> syn::Result<Self> {
        if input.peek(syn::token::Paren) {
            let content;
            syn::parenthesized!(content in input);
            let a: LitInt = content.parse()?;
            content.parse::<Token![,]>()?;
            let b: LitInt = content.parse()?;
            content.parse::<Token![,]>()?;
            let c: LitInt = content.parse()?;
            Ok(SpacingValue::Tuple(a, b, c))
        } else {
            Ok(SpacingValue::Single(input.parse()?))
        }
    }
}
```

同时把 `SpacingInput` 的泛型参数从 `LitInt` 换回 `SpacingValue`，`parse_terminated(SpacingValue::parse, Token![,])`；把回显错误改成同时打印形态，例如（示例代码）：

```rust
let shapes: Vec<String> = input
    .values
    .iter()
    .map(|v| match v {
        SpacingValue::Single(n) => format!("Single({})", n),
        SpacingValue::Tuple(a, b, c) => format!("Tuple({}, {}, {})", a, b, c),
    })
    .collect();
syn::Error::new(
    proc_macro2::Span::call_site(),
    format!("parsed: {}", shapes.join(", ")),
)
.to_compile_error()
.into()
```

2. 先用合法输入确认回显正确：`parse_demo![24, (1, 2, 4)];` → 预期错误消息 `parsed: Single(24), Tuple(1, 2, 4)`。

3. **非法输入实验**：逐个替换调用并 `cargo check -p demo`，把结果填进一张记录表（这就是本讲的综合素材）：

| 实验输入 | 预期行为 | 预期错误消息（措辞待本地验证） | 实际观察 |
| --- | --- | --- | --- |
| `(1, 2)` | 第二个逗号处解析失败 | 类似 `expected ','` | 待填写 |
| `x` | Single 分支整数解析失败 | 类似 `expected integer literal` | 待填写 |
| `(1, 2, 3, 4)` | Tuple 解析「成功」但子流未耗尽 | 类似 `unexpected token` | 待填写 |
| `24 32` | 列表提前停止、流未耗尽 | 类似 `unexpected token` | 待填写 |
| `(1, 2, 4)` 单独 | 合法，回显 `Tuple(1, 2, 4)` | `parsed: Tuple(1, 2, 4)` | 待填写 |

**需要观察的现象**：重点对比 `(1, 2)`（错误指向括号内部）与 `x`（错误指向标识符本身）的错误**位置**；以及 `(1, 2, 3, 4)` 这种「多给了值」的情况并不报 `expected` 类错误，而是子流剩余 token 触发的 `unexpected token` 类错误。

**预期结果**：五组输入各得一条带源码位置的编译错误（或合法回显），错误消息措辞以本地 rustc/syn 输出为准。若某组与预期不符，正是最有价值的学习材料——回头重读 4.4.1 的机制说明找原因。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `input.peek(syn::token::Paren)` 的 `peek` 换成实际解析（先尝试 `Tuple` 失败再尝试 `Single`），会有什么问题？

**答案**：功能上也可行（`ParseStream` 失败自动回滚），但两条分支语法没有公共前缀，「尝试-回滚」会比前瞻多一次注定失败的解析；更重要的是分流意图不清晰——`peek` 版本一眼可读「看到括号走 Tuple 分支」。当前缀有重叠时（例如两种形态都以标识符开头）才必须用尝试-回滚或更细的 `peek2`。

**练习 2**：`content.parse::<Token![,]>()?;` 这一行没有把结果绑定到变量，它的作用是什么？

**答案**：这是**语法断言**：确认子流的当前位置是一个逗号并消费它。`Token![,]`（`token::Comma`）是零大小类型，值本身没有信息，解析它的唯一目的就是「要求这里必须是逗号，否则报错」——这与正则表达式中的字面量匹配是一个道理。

**练习 3**：为什么 `(1, 2, 3, 4)` 不会在解析 `Tuple` 时立刻报错，而是报 `unexpected token`？

**答案**：`Tuple` 分支只解析「整数、逗号、整数、逗号、整数」共 5 个 token，解析完这 5 个就返回 `Ok`，它并不检查 `content` 是否还有剩余。剩余 token 的检查发生在子流 `content` 被**丢弃**时——syn 的 `ParseBuffer` 在 drop 时若发现还有未消费的 token，会记下 `unexpected token` 错误并通过共享错误槽传到外层。所以错误稍晚于解析本身出现。

## 5. 综合实践

**任务**：把 4.1–4.4 的分步成果合并，完成一个「间距输入检查器」宏 `spacing_audit!`——它不做任何代码生成，专门把解析结果以编译错误的形式完整回显，作为你阅读 `dynamic_spacing.rs` 解析部分的「可执行笔记」。

**要求**：

1. 在 scratch 工作区实现 `spacing_audit!`，输入语法与 `derive_dynamic_spacing!` 完全一致：逗号分隔的「单个整数或三元整数组」列表。
2. 解析必须复用本讲学到的四个原语：`parse_macro_input!` + `parse_terminated` + `peek` + `parenthesized!`。
3. 回显内容包括：元素总数、每个元素的形态与值，并标注它对应 `DynamicSpacing` 的哪个变体名（提示：变体名规则取自 u1-l3——`Single` 取 \(n\)、`Tuple` 取中间值 \(b\)，用 `format_ident!("Base{:02}", …)` 的思路在错误消息里拼出 `Base24` 这类名字，`{:02}` 表示不足两位补零）。
4. 把 [crates/ui/src/styles/spacing.rs:29-44](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/styles/spacing.rs#L29-L44) 的 14 个真实值原样拷进 `demo/src/main.rs` 调用 `spacing_audit!`，核对回显：应得到 14 个元素，前 10 个 `Tuple`、后 4 个 `Single`，变体名依次为 `Base00, Base01, Base02, Base03, Base04, Base06, Base08, Base12, Base16, Base20, Base24, Base32, Base40, Base48`。
5. 最后重做 4.4.4 的非法输入实验表，填完「实际观察」列。

**预期结果**：一次 `cargo check -p demo` 即可在错误消息中看到完整解析结果；所有非法输入的报错位置都能指到具体 token。变体名对照中 `(3, 6, 8)` 对应 `Base06`、`(10, 12, 14)` 对应 `Base12`——如果你推出的名字与这里一致，说明你已同时掌握了解析与命名规则两件事（命名规则将在 u3-l1 深入）。

## 6. 本讲小结

- 过程宏开发的标准顺序是**建模 → 解析 → 生成**：`dynamic_spacing.rs` 先用 `DynamicSpacingInput`（列表）和 `DynamicSpacingValue`（`Single`/`Tuple` 二选一）两个普通类型定义输入形状，再实现 `Parse`。
- `parse_macro_input!(input as T)` 是入口：成功得到结构化模型，失败**提前返回**一条带源码位置的编译错误——这是过程宏「好错误」的样板。
- `ParseStream` 是只能向前走的事务性游标：解析失败自动回滚，`peek` 前瞻不消费，这两个特性支撑了「前瞻分流」与「乐观解析」两种风格。
- `Punctuated<T, Token![,]>` + `parse_terminated` 用一个调用处理逗号列表的全部语法约定（元素间必须有逗号、允许尾随逗号、允许空列表）；缺逗号等「流未耗尽」错误由外层 `syn::parse` 兜底。
- `parenthesized!` 把 `(…)` 内部提取为子流，实现错误局部化；子流未耗尽会触发 `unexpected token`——`(1, 2, 3, 4)` 报错晚于 `Tuple` 解析成功，就是这个机制。
- 「用编译错误回显宏的中间数据」是过程宏最实用的调试技巧，本讲实践全程使用它。

## 7. 下一步学习建议

本讲止步于 [crates/ui_macros/src/dynamic_spacing.rs:50](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L50)：`input` 已经是结构化的 `DynamicSpacingInput`，但还没有变成任何输出。下一讲 **u2-l2（用 quote 生成代码：DynamicSpacing 枚举的诞生）** 从第 52 行继续：`format_ident!("Base{:02}", …)` 如何生成 `Base04` 这样的标识符、`quote!` 模板中 `#variant` 与 `#(...)*` 的插值与重复规则，以及为什么生成代码里写 `::theme::` 绝对路径。如果你想在下一讲前热身，可以先把综合实践里回显的 `BaseXX` 名单留着——u2-l2 会告诉你这些名字如何变成真实的枚举变体。

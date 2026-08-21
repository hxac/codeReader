# 用 quote 生成代码：DynamicSpacing 枚举的诞生

## 1. 本讲目标

上一讲（u2-l1）我们读完了 `dynamic_spacing.rs` 的前半段：用 syn 把 `derive_dynamic_spacing![24, (1, 2, 4)]` 这样的输入解析成了 `DynamicSpacingInput`。但解析只是"读进来"，过程宏真正的产出是"写出去"的代码。

本讲精读后半段——`derive_spacing` 函数如何用 `quote!` 模板把解析结果重新拼装成完整的 `DynamicSpacing` 枚举。学完后你应该能：

1. 读懂 `quote!` 模板中 `#variant`、`#n` 这样的插值点，说出它们各自把什么数据放进了生成代码。
2. 掌握 `#(...)* ` 重复语法：知道它如何让两个 `Vec` "齐步走"地批量展开，以及逗号写在重复体内部和写在分隔符位置的区别。
3. 理解 `format_ident!("Base{:02}", n)` 如何生成 `Base04`、`Base24` 这类标识符，以及 `#[doc = #doc_string]` 如何给每个变体注入文档注释。
4. 明白生成代码里 `::theme::` 这种绝对路径由谁负责解析（提示：不是 ui_macros）。

本讲的验证手段是"手工展开"：给你一个输入，你能写出宏应该生成的 Rust 代码，再和源码模板逐行核对。

## 2. 前置知识

### 2.1 quote 是什么

`quote` 是与 `syn` 配套的代码生成库。`syn` 负责"token → 结构化数据"，`quote` 负责反方向："数据 + 模板 → token"。它的核心是一个同名的 `quote!` 宏：

- 模板里写的所有内容**原样**成为输出 token 的一部分（类型、关键字、标点都会逐字保留）。
- `#变量` 表示插值：把这个变量的值转成 token 塞进模板，变量可以是任何实现了 `ToTokens` 的类型——数字、字符串、`Ident`、`LitInt`，甚至嵌套的 `TokenStream`。
- `##` 表示转义：如果输出里真的需要一个字面量 `#`，写 `##`（比如生成属性宏时用到，本 crate 没用到）。

一个直觉类比：`quote!` 有点像 `format!`，但操作对象不是字符串而是 token。正因为是 token 而不是字符串，生成出来的代码永远不需要"重新解析"，格式化、span 信息都由 token 结构天然保证。

### 2.2 两种 TokenStream 的边界

`quote!` 的返回值是 `proc_macro2::TokenStream`（quote 自己的世界里的 token 流），而过程宏入口函数必须返回 `proc_macro::TokenStream`（编译器世界里的 token 流）。所以函数末尾要做一次转换，对应源码的最后一行 `TokenStream::from(expanded)`。这个边界在 u1-l2 已经提过，本讲在源码里落到实地。

### 2.3 本讲会用到的输入数据回顾

来自 u2-l1 的结论：解析完成后我们手里有 `Punctuated<DynamicSpacingValue, Token![,]>`，每个元素是：

- `Single(LitInt)`：如输入 `24`；
- `Tuple(LitInt, LitInt, LitInt)`：如输入 `(1, 2, 4)`，语义是 Compact/Default/Comfortable 三档密度直接用这三个像素值。

本讲要做的就是把这些数据"翻译"回一大段 Rust 代码。

## 3. 本讲源码地图

| 文件 | 本讲关注点 |
| --- | --- |
| `src/ui_macros.rs`（本 crate） | L6-L10：`#[proc_macro]` 入口，一行转发到实现模块 |
| `src/dynamic_spacing.rs`（本 crate） | **本讲主战场**。L52-L89 构造 match 分支；L91-L125 构造变体名与文档；L127-L164 最终 `quote!` 模板 |
| `../ui/src/styles/spacing.rs`（ui crate） | L29-L44：全仓库唯一的宏调用点，14 个输入值；L1-L2 的 `use` 语句解释了生成代码里的名字从哪来 |

## 4. 核心概念与源码讲解

### 4.1 quote! 模板：从数据到 TokenStream

#### 4.1.1 概念说明

`derive_spacing` 的生成阶段可以拆成"备料"和"组装"两步：

- **备料**：遍历解析结果，为每个输入值预先构造好两类小件——match 分支的 `TokenStream`（一段）、变体名 `Ident` + 文档字符串（一对）。
- **组装**：把这些小件填进一个完整的 `quote!` 模板，得到枚举定义 + `impl` 块，一次性返回。

这个"先造零件、再拼整机"的结构是过程宏里非常常见的组织方式：模板负责代码的静态骨架，插值负责动态内容。

#### 4.1.2 核心流程

```text
DynamicSpacingInput
        │  遍历每个 DynamicSpacingValue
        ├─► 第一轮 map：每个值生成一段 match 分支 TokenStream
        │       （里面插值了 #variant 变体名 和 #n/#a/#b/#c 浮点数值）
        │       收集为 spacing_ratios: Vec<TokenStream>
        │
        ├─► 第二轮 map：每个值生成 (变体名 Ident, 文档字符串)
        │       unzip 拆成 variant_names: Vec<Ident> 和 doc_strings: Vec<String>
        │
        └─► quote! 总模板：枚举定义（重复插入变体+文档）
                        + impl 块（重复插入 match 分支、rems、px 方法）
                │
                └─► TokenStream::from(expanded) 返回给编译器
```

对于 Single 输入，密度换算遵循公式：Compact 取 \(\max(n-4,\ 0)\)，Default 取 \(n\)，Comfortable 取 \(n+4\)，再统一除以 16 得到 rem 比例：

\[ \text{spacing\_ratio} = \frac{\text{px}_{\text{density}}}{16} \]

#### 4.1.3 源码精读

入口只有两行：先 `parse_macro_input!` 解析（u2-l1 讲过），失败会自动提前返回编译错误；成功后进入生成阶段。

[ui_macros.rs:L6-L10](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/ui_macros.rs#L6-L10)——`#[proc_macro]` 声明的函数式宏入口，转发到 `dynamic_spacing::derive_spacing`。

[dynamic_spacing.rs:L52-L89](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L52-L89)——第一轮备料：`input.values.iter().map(...).collect()` 把每个输入值变成一段 match 分支。看 Single 分支的模板：

```rust
DynamicSpacingValue::Single(n) => {
    let n = n.base10_parse::<f32>().unwrap();
    quote! {
        DynamicSpacing::#variant => match ::theme::theme_settings(cx).ui_density(cx) {
            ::theme::UiDensity::Compact => (#n - 4.0).max(0.0) / BASE_REM_SIZE_IN_PX,
            ::theme::UiDensity::Default => #n / BASE_REM_SIZE_IN_PX,
            ::theme::UiDensity::Comfortable => (#n + 4.0) / BASE_REM_SIZE_IN_PX,
        }
    }
}
```

完整源码见 [dynamic_spacing.rs:L64-L74](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L64-L74)。注意三个插值细节：

1. `#variant` 插入的是上一段（[L57-L59](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L57-L59)）用 `format_ident!` 造出来的标识符，如 `Base24`。
2. `#n` 插入的是 `f32` 数值。`base10_parse::<f32>()` 先把 `LitInt` 变成 `24.0`，quote 再把它渲染回浮点字面量 token `24.0`。**注意这不再是原来的 token**——原文的 span 信息在这里丢掉了，数字经历了"token → 数值 → token"的往返。
3. 生成的表达式刻意保持 `(#n - 4.0).max(0.0)` 这种"展开后仍可读"的形状，而不是预先算好 `20.0`。代价是每次调用 `spacing_ratio` 都在运行时做减法（编译器通常会常量折叠），收益是 `cargo expand` 的输出和模板逐字对应，便于排查。

Tuple 分支（[L75-L86](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L75-L86)）结构完全相同，只是三个密度档直接用 `#a`、`#b`、`#c` 三个值，套用 \((a,\ b,\ c)\) 而不是公式推导。

最后的总模板骨架（[L127-L164](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L127-L164)）里，除了插值点之外的所有内容——`#[derive(...)]`、`pub enum DynamicSpacing`、`impl` 块、三个方法的文档注释——都是**原样输出**的字面 token。特别地，模板里的 `///` 注释行能直接出现在生成代码里，因为 Rust 词法器在宏展开之前就把 `///` 处理成了 `#[doc = "..."]` 属性 token，它对 quote 来说只是普通 token。

函数最后一行 [L166](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L166) 的 `TokenStream::from(expanded)` 完成 `proc_macro2` 到 `proc_macro` 的跨界转换，把结果交还给编译器，替换掉调用点的 `derive_dynamic_spacing![...]`。

#### 4.1.4 代码实践

**实践目标**：在不构建整个 Zed workspace 的情况下，亲手验证 quote 的插值行为。

**操作步骤**（以下均为示例代码，在 Zed 仓库之外新建一个 scratch crate）：

1. `cargo new quote_lab && cd quote_lab`。
2. 在 `Cargo.toml` 中加 `quote = "1"`（不需要 syn）。
3. 把 `main.rs` 写成：

```rust
use quote::{format_ident, quote};

fn main() {
    // 模拟 derive_spacing 对 Single 输入 24 的处理
    let n: f32 = 24.0;
    let variant = format_ident!("Base{:02}", 24u32);
    let arm = quote! {
        DynamicSpacing::#variant => match ::theme::theme_settings(cx).ui_density(cx) {
            ::theme::UiDensity::Compact => (#n - 4.0).max(0.0) / BASE_REM_SIZE_IN_PX,
            ::theme::UiDensity::Default => #n / BASE_REM_SIZE_IN_PX,
            ::theme::UiDensity::Comfortable => (#n + 4.0) / BASE_REM_SIZE_IN_PX,
        }
    };
    println!("{}", arm);
}
```

4. `cargo run`。

**需要观察的现象**：程序能编译通过——虽然 token 里引用了根本不存在的 `::theme::theme_settings`。这证明了 token 不做名称解析和类型检查（u1-l2 的结论落地）。打印出来的就是一段合法的 Rust 源码文本。

**预期结果**：输出形如 `DynamicSpacing :: Base24 => match :: theme :: theme_settings (cx) . ui_density (cx) { ... }` 的 token 文本（token 的字符串形式会在标点两侧带空格，这是正常现象）。

### 4.2 #(...)* 重复语法：批量展开的两种写法

#### 4.2.1 概念说明

总模板需要把 14 个变体、14 段 match 分支填进固定形状的骨架里。`quote!` 的重复语法就是为此设计的：

- `#(...)* `：把重复体里的每个插值变量按元素逐个展开，重复体整体重复 N 次。
- **齐步走规则**：同一个重复体里出现的所有 `Vec`（或迭代器）长度必须一致，quote 按下标配对取元素。长度不一致时宏在展开期直接崩溃，编译器报告过程宏 panic。
- 逗号的两种放法：
  - **写在重复体内部**：`#(#items,)* `——每个元素后面都跟一个逗号，包括最后一个（尾随逗号）。Rust 的枚举变体列表和 match 分支列表都合法接受尾随逗号，所以这种写法很省心。
  - **写在分隔符位置**：`#(#items),*`——逗号出现在 `)` 和 `*` 之间，作为元素之间的分隔符，最后一个元素后没有逗号。

#### 4.2.2 核心流程

以真实调用点的 14 个输入为例：

```text
doc_strings   = [ "`0px`|`0px`|`0px (@16px/rem)` - ...",  "`2px`|`4px`|`6px (@16px/rem)` - ...", ... 共 14 个 ]
variant_names = [ Base00, Base04, ... 共 14 个 ]        ← 两Vec按下标配对

#(
    #[doc = #doc_strings]
    #variant_names,
)*
        │ 展开
        ▼
#[doc = "`0px`|`0px`|`0px (@16px/rem)` - ..."] Base00,
#[doc = "`2px`|`4px`|`6px (@16px/rem)` - ..."] Base04,
...（共 14 行）
```

#### 4.2.3 源码精读

[dynamic_spacing.rs:L138-L141](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L138-L141)——枚举变体的批量生成。`#doc_strings` 与 `#variant_names` 在同一个 `#(...)* ` 里齐步走；逗号写在重复体内部（`#variant_names,`），所以每个变体行都以逗号结尾。

[dynamic_spacing.rs:L149](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L149)——`#(#spacing_ratios,)*`。乍看像"分隔符写法"，但仔细数括号：逗号在 `)` **之前**，处于重复体内部，所以同样是"每个分支后都带逗号"的尾随逗号写法。展开后填进 L148 的 `match self { ... }` 里。

再看备料侧如何保证两个 Vec 等长：[L91-L125](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L91-L125)。这一轮 `map` 的闭包返回二元组 `(quote!(#variant), quote!(#doc_string))`，最后用 [L125](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L125) 的 `.unzip()` 把 `Vec<(A, B)>` 拆成 `(Vec<A>, Vec<B>)`。因为两个 Vec 来自**同一次遍历**，等长是构造上保证的，quote 的齐步走规则天然满足。

顺带一个阅读观察：`variant` 的构造逻辑（[L56-L63](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L56-L63) 与 [L95-L102](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L95-L102)）在两轮遍历里各写了一遍，是重复代码。读源码时留意这种"宁可重复也要分两轮备料"的结构——它把"生成分支"和"生成变体"两条关注点切开了。

#### 4.2.4 代码实践

**实践目标**：用最小例子直观对比"逗号在体内"与"逗号在分隔符位置"的输出差异。

**操作步骤**（接着 4.1 的 `quote_lab`，示例代码）：

```rust
let names = vec![format_ident!("Base00"), format_ident!("Base04")];

let in_body = quote! { #(#names,)* };   // 逗号在重复体内 → 尾随逗号
let separated = quote! { #(#names),* }; // 逗号是分隔符 → 无尾随逗号

println!("体内:   {}", in_body);
println!("分隔符: {}", separated);
```

**需要观察的现象**：两行输出的逗号数量与位置。

**预期结果**：`体内` 输出 `Base00 , Base04 ,`（末尾有逗号）；`分隔符` 输出 `Base00 , Base04`（末尾无逗号）。

**小练习与答案**：

1. 练习：如果把 L149 改成 `#(#spacing_ratios)*`（去掉逗号），生成的 match 会怎样？
   答案：14 个分支会首尾相连没有任何逗号分隔，语法非法，ui crate 编译时报"expected `,`"一类的错误。这正是逗号必须进重复体的原因。
2. 练习：`spacing_ratios`、`variant_names`、`doc_strings` 三个集合中，哪两个处于同一个重复体、靠齐步走配对？
   答案：`variant_names` 与 `doc_strings`（L138-L141 同一个 `#(...)* `）。`spacing_ratios` 独占 L149 的重复体。
3. 练习：为什么两个 Vec 一定等长，不用担心 quote 齐步走失败？
   答案：它们由同一次 `input.values.iter().map(...)` 经 `unzip()` 拆出，元素一一对应，等长是构造保证的。

### 4.3 format_ident! 与 #[doc] 注入

#### 4.3.1 概念说明

**format_ident!** 是 quote 提供的"标识符版 format!"：按 `format!` 的格式字符串语法拼接，但产物是一个 `Ident`（标识符 token）而不是字符串，可以直接用 `#variant` 插进模板。`"Base{:02}"` 里的 `{:02}` 表示十进制、不足两位左侧补零：

- `6` → `Base06`（补了一个 0）
- `24` → `Base24`（正好两位）
- `120` → `Base120`（超过两位不截断）

命名取值规则：Single 取 `n` 本身，Tuple 取**中间值 b**（Default 档的像素值）。也就是说，`BaseXX` 的 XX 永远是"默认密度下的像素值"——这与调用点注释（spacing.rs L23-L28）的约定一致。

**#[doc] 注入**：Rust 里 `/// 文档` 只是 `#[doc = "文档"]` 的语法糖。因此过程宏可以像构造普通属性一样，用 `#[doc = #doc_string]` 给每个变体动态生成文档。对 IDE 悬浮提示和 rustdoc 来说，这和手写的文档注释毫无区别——`DynamicSpacing::Base24` 在编辑器里悬浮时显示的那行 `20px|24px|28px` 说明，就是宏在编译期算出来塞进去的。

#### 4.3.2 核心流程

对每个输入值：

```text
输入                    变体名          文档字符串
24            ──►  Base24   ──►  "`20px`|`24px`|`28px (@16px/rem)` - Scales with the user's rem size."
(2, 4, 6)     ──►  Base04   ──►  "`2px`|`4px`|`6px (@16px/rem)` - Scales with the user's rem size."
(1, 1, 2)     ──►  Base01   ──►  "`1px`|`1px`|`2px (@16px/rem)` - Scales with the user's rem size."
(0, 0, 0)     ──►  Base00   ──►  "`0px`|`0px`|`0px (@16px/rem)` - Scales with the user's rem size."
```

Single 的文档值由公式 \(\max(n-4,0) \mid n \mid n+4\) 推出；Tuple 直接用 \((a, b, c)\)。`f32` 用 `{}` 格式化时 `20.0` 显示为 `20`，所以文档里是整数样子的 `20px`。

#### 4.3.3 源码精读

[dynamic_spacing.rs:L57-L62](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L57-L62)——变体名的生成。注意 Single 用 `n.base10_parse::<u32>()`，Tuple 用 `b.base10_parse::<u32>()`：都取 **Default 档像素值**作为 `BaseXX` 的 XX。

[dynamic_spacing.rs:L103-L112](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L103-L112)——Single 输入的文档字符串构造：先算 `compact = (n - 4.0).max(0.0)`、`comfortable = n + 4.0`，再套固定格式 ``"`{}px`|`{}px`|`{}px (@16px/rem)` - Scales with the user's rem size."``。Tuple 版本在 [L113-L121](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L113-L121)，只是三个值直接来自输入。

[dynamic_spacing.rs:L123](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L123)——`(quote!(#variant), quote!(#doc_string))`：把 `Ident` 和 `String` 都各自包成一个单 token 的 `TokenStream`（`String` 实现了 `ToTokens`，会变成字符串字面量 token），供下一阶段 `#[doc = #doc_string]` 使用。

[dynamic_spacing.rs:L128-L135](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L128-L135)——枚举整体的文档是**直接写死在模板里**的 `///` 注释，和变体级的动态 `#[doc = ...]` 形成对照：静态文档手写，动态文档生成。

还有一个值得知道的细节：`format_ident!` 生成的 `Ident` 默认携带 `Span::call_site()`（指向宏调用点的虚拟 span），而不是原始数字字面量的 span。这意味着生成代码报错时，错误定位会指向 `spacing.rs` 的宏调用处，而不是 `dynamic_spacing.rs` 内部——对使用者反而更友好。span 与错误处理的深水区留给 u5-l1。

#### 4.3.4 代码实践

**实践目标**：验证 `{:02}` 补零行为和文档字符串格式。

**操作步骤**（仍在 `quote_lab`，示例代码）：

```rust
for n in [0u32, 4, 6, 12, 24, 120] {
    println!("{:>8} -> {}", n, format_ident!("Base{:02}", n));
}
let (n, compact, comfortable) = (24.0f32, 20.0f32, 28.0f32);
println!(
    "`{}px`|`{}px`|`{}px (@16px/rem)` - Scales with the user's rem size.",
    compact, n, comfortable
);
```

**需要观察的现象**：补零何时发生；`f32` 以 `{}` 格式化后的样子。

**预期结果**：`0 -> Base00`、`4 -> Base04`、`6 -> Base06`、`12 -> Base12`、`24 -> Base24`、`120 -> Base120`；文档行为 `` `20px`|`24px`|`28px (@16px/rem)` - Scales with the user's rem size. ``（`20.0` 显示为 `20`）。

**小练习与答案**：

1. 练习：真实调用点（spacing.rs L30-L44）里 `(3, 6, 8)` 会生成什么变体名和文档？
   答案：Tuple 取中间值 → `Base06`；文档 `` `3px`|`6px`|`8px (@16px/rem)` - Scales with the user's rem size. ``（Tuple 不套公式，直接用三个输入值）。
2. 练习：如果新增一个 Single 输入 `4`，变体名和文档是什么？它会和 `(2, 4, 6)` 生成的变体冲突吗？
   答案：Single(4) → 公式得 \(\max(0,0)=0 \mid 4 \mid 8\)，变体名 `Base04`，文档 `` `0px`|`4px`|`8px (@16px/rem)` ...``；而 `(2, 4, 6)` 也生成 `Base04`（文档不同）——两个同名变体会导致 ui crate 编译错误（重复定义）。这是宏不做去重检查的一个体现。
3. 练习：为什么 `#[doc = #doc_string]` 能让 rustdoc 和 IDE 显示文档，而不用写成 `///`？
   答案：`///` 只是 `#[doc = "..."]` 的语法糖，词法阶段就被转成属性 token；两者对下游工具完全等价，而属性形式才能接受动态生成的字符串。

### 4.4 生成代码的名字解析：::theme:: 绝对路径与调用方依赖

#### 4.4.1 概念说明

模板里引用了一批 ui_macros 自己**并没有**依赖的名字：`::theme::theme_settings`、`::theme::UiDensity`、`App`、`Rems`、`Pixels`、`rems`、`px`。这在 u1-l3 已经从"依赖箭头 vs 展开箭头"的角度讲过结论，本讲在源码层面落实：

过程宏生成的 token 会被拼接到**调用点所在的 crate**（这里是 ui），随后按调用方上下文做普通的名称解析与类型检查。所以：

- 生成代码里的路径，由**调用方 crate 的依赖**负责解析；
- 写 `::theme::`（开头的 `::`）意味着从"绝对路径"解析——锚定在外部 prelude / crate 根上，不受调用点局部作用域里恰好有个叫 `theme` 的变量或模块影响。这是宏生成代码的防御性习惯：宏无法预知自己被展开到什么作用域里。
- 不带 `::` 前缀的 `App`、`rems`、`px` 等则是普通路径，依赖调用点文件顶部有对应的 `use`。

#### 4.4.2 核心流程

```text
ui_macros 生成的 token（引用 ::theme::...、App、rems、px）
        │  拼回 ui crate 的 spacing.rs 调用点
        ▼
按 ui crate 的环境解析：
  spacing.rs L1:  use gpui::{App, Pixels, Rems, px, rems};   ← App/Rems/Pixels/rems/px 从这来
  spacing.rs L2:  use theme::UiDensity;                       ← theme 是 ui 的依赖
  ui 的 Cargo.toml: theme.workspace = true                    ← 依赖箭头在调用方
        ▼
类型检查通过 → DynamicSpacing 枚举在 ui crate 中“诞生”
```

#### 4.4.3 源码精读

[dynamic_spacing.rs:L68](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L68)——`::theme::theme_settings(cx).ui_density(cx)`：运行时查询用户当前密度设置。开头的 `::` 使路径从绝对位置解析。

[dynamic_spacing.rs:L146-L151](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L146-L151)——`spacing_ratio` 方法：`const BASE_REM_SIZE_IN_PX: f32 = 16.0;` 定义在生成代码内部，把像素换算成 rem 比例（除以 16）。`match self` 的分支体就是 4.2 节批量生成的 `#(#spacing_ratios,)*`。

[dynamic_spacing.rs:L153-L162](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L153-L162)——对外的两个方法：`rems` 把比例包成 `Rems` 类型（`rems(...)` 函数），`px` 则先取 `::theme::theme_settings(cx).ui_font_size(cx)` 换算成 `f32`，乘以比例后用 `px(...)` 包成 `Pixels`。这里的 `App`、`Rems`、`Pixels`、`rems`、`px` 都是裸名字。

[spacing.rs:L1-L2](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/styles/spacing.rs#L1-L2)（ui crate）——`use gpui::{App, Pixels, Rems, px, rems};` 和 `use theme::UiDensity;`：生成代码里所有裸名字的解析来源。宏作者与调用点作者之间的"隐式契约"就在这两行 `use` 里——调用点删掉任何一个导入，编译错误会出现在**生成的代码**里。

对照 ui 的 [Cargo.toml:L30](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/Cargo.toml#L30)（`theme.workspace = true`）与 ui_macros 自己的依赖列表（只有 syn、quote，见 u1-l1）：生成代码里的 `::theme::` 由 ui 的依赖箭头解析，ui_macros 的 Cargo.toml 里完全没有 theme。这个设计让宏 crate 保持极薄，代价是"宏能用哪些名字"受限于调用方必须提供的依赖与导入。

最后看消费侧的真实用法（运行时效果）：[modal.rs:L174](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/components/modal.rs#L174) 的 `.px(DynamicSpacing::Base12.rems(cx))`——组件代码使用的正是宏生成的变体和方法。密度换算的运行时细节（`UiDensity` 三档、`ui_font_size`）是下一讲 u3-l2 的主题。

#### 4.4.4 代码实践

**实践目标**：通过"破坏契约"直观感受生成代码对调用点导入的依赖。

**操作步骤**：

1. 打开 `crates/ui/src/styles/spacing.rs`，阅读 L1-L2 的两条 `use`。
2. 本地临时注释掉 L1 中的 `rems`（例如改成 `use gpui::{App, Pixels, px};`），运行 `cargo check -p ui`。
3. 阅读报错信息，注意它指向的代码位置与报错内容。
4. 还原改动（务必还原，不要提交）。

**需要观察的现象**：报错指向的行号落在宏调用点附近，报错文本却是 `rems` 未找到——因为 `rems(...)` 出现在生成代码的 `rems` 方法里（模板 L155）。

**预期结果**：`cargo check -p ui` 报 `cannot find function rems in this scope` 一类的错误，定位到 spacing.rs 的宏调用处。这是"宏生成代码在调用方语境里做名称解析"最直接的证据。**此实验需修改源码，请在本地完成并还原；若不便修改，标注待本地验证，改为纯阅读：在报错想象中追踪 `rems` 这个名字从模板 L155 到 spacing.rs L1 的解析路径。**

**小练习与答案**：

1. 练习：为什么模板里写 `::theme::...` 而不是 `theme::...`？
   答案：开头的 `::` 强制从绝对路径（crate 根/外部 prelude）开始解析，避免调用点某个局部也叫 `theme` 的绑定把路径劫走。宏无法控制自己被展开到什么作用域，所以生成代码中的路径要尽量写死锚点。
2. 练习：ui_macros 的 Cargo.toml 里没有 theme 依赖，生成代码却引用了 `::theme::`，为什么不会在 ui_macros 编译时报错？
   答案：过程宏只输出 token，不做名称解析；生成的 token 拼接到调用方 ui crate 后才解析与类型检查，此时用的是 ui 的依赖（`theme.workspace = true`）。
3. 练习：生成代码里 `BASE_REM_SIZE_IN_PX` 为什么不会污染调用方的命名空间？
   答案：它声明在生成代码的 `spacing_ratio` 方法体内部（模板 L147），是函数内的局部 `const`，作用域仅限该方法。

## 5. 综合实践

**任务**：以输入值 `24`（Single 形态）为例，手工推导 `derive_dynamic_spacing![24];` 应生成的完整代码，写成 Rust，再对照源码模板逐行核对。

**步骤**：

1. **先推导，不看源码**。拿一张纸，按下面三步推出所有动态内容：
   - 变体名：`format_ident!("Base{:02}", 24u32)` → ?
   - 文档字符串：`compact = (24.0 - 4.0).max(0.0)`，`comfortable = 24.0 + 4.0`，套格式串 → ?
   - match 分支：Single 公式 \(\max(n-4,0) \mid n \mid n+4\) 除以 16 → ?
2. **写出完整代码**。把你推导的动态内容填进模板骨架（[dynamic_spacing.rs:L127-L164](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L127-L164) 的字面部分 + 你的动态部分），应当得到（参考答案，可作为核对基准）：

```rust
/// A dynamic spacing system that adjusts spacing based on
/// [UiDensity].
///
/// The number following "Base" refers to the base pixel size
/// at the default rem size and spacing settings.
///
/// When possible, [DynamicSpacing] should be used over manual
/// or built-in spacing values in places dynamic spacing is needed.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum DynamicSpacing {
    #[doc = "`20px`|`24px`|`28px (@16px/rem)` - Scales with the user's rem size."]
    Base24,
}

impl DynamicSpacing {
    /// Returns the spacing ratio, should only be used internally.
    fn spacing_ratio(&self, cx: &App) -> f32 {
        const BASE_REM_SIZE_IN_PX: f32 = 16.0;
        match self {
            DynamicSpacing::Base24 => match ::theme::theme_settings(cx).ui_density(cx) {
                ::theme::UiDensity::Compact => (24.0 - 4.0).max(0.0) / BASE_REM_SIZE_IN_PX,
                ::theme::UiDensity::Default => 24.0 / BASE_REM_SIZE_IN_PX,
                ::theme::UiDensity::Comfortable => (24.0 + 4.0) / BASE_REM_SIZE_IN_PX,
            },
        }
    }

    /// Returns the spacing value in rems.
    pub fn rems(&self, cx: &App) -> Rems {
        rems(self.spacing_ratio(cx))
    }

    /// Returns the spacing value in pixels.
    pub fn px(&self, cx: &App) -> Pixels {
        let ui_font_size_f32: f32 = ::theme::theme_settings(cx).ui_font_size(cx).into();
        px(ui_font_size_f32 * self.spacing_ratio(cx))
    }
}
```

3. **逐行核对**。对照源码核对这五个关键点，每点都回到具体行号：
   - 枚举文档与 `#[derive(...)]`：来自 [L128-L136](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L128-L136) 的字面模板；
   - `#[doc = ...] Base24,`：来自 [L138-L141](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L138-L141) 的重复体，执行一次；
   - match 分支：来自 [L64-L74](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L64-L74) 的 Single 模板，`#n` 展开为 `24.0`；
   - `rems` / `px`：来自 [L153-L162](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L153-L162) 的字面模板，无动态内容。
4. **机器验证（可选）**：若安装了 cargo-expand，可运行 `cargo expand -p ui styles` 查看真实展开（关注 `DynamicSpacing` 一节，真实调用点有 14 个变体，找到 `Base24` 那行与你手推的比对）。若未安装 cargo-expand，以手推结果为准，**标注待本地验证**。
5. **轻量替代验证**：把第 2 步手推的代码直接粘进 4.1 的 `quote_lab` scratch crate（补上 `use` 和两个空 `struct App;` 之类的桩即可，示例代码），`cargo run` 编译通过即说明你推导的代码语法自洽。

**预期结果**：手推代码与模板逐行吻合；唯一的常见错点是文档里 `20.0` 应显示为 `20`（`f32` 的 `{}` 格式化），以及 match 分支末尾那个来自重复体的尾随逗号。

## 6. 本讲小结

- `derive_spacing` 的生成阶段是"两轮备料 + 一次组装"：先造出 `spacing_ratios`（match 分支）和 `variant_names`/`doc_strings`（变体名与文档），再填进总 `quote!` 模板。
- `#变量` 把任何 `ToTokens` 数据插进模板；`#n` 插入的浮点值经历了"token → 数值 → token"的往返，原文 span 在此丢失。
- `#(...)* ` 让多个等长 `Vec` 齐步走批量展开；本 crate 把逗号写在重复体内部，产生合法的尾随逗号（`#(#spacing_ratios,)*` 的逗号在 `)` 之前，属于体内写法）。
- `format_ident!("Base{:02}", n)` 生成变体名，XX 取 Default 档像素值（Single 取 `n`，Tuple 取中间值 `b`），`{:02}` 补零保证两位；`#[doc = #doc_string]` 是动态文档注入，与 `///` 等价。
- 生成代码引用的 `::theme::`、`App`、`rems`、`px` 全部由调用方 ui crate 解析（spacing.rs L1-L2 的 `use` 与 ui 对 theme 的依赖），ui_macros 自身只依赖 syn 和 quote。

## 7. 下一步学习建议

- 下一讲 **u3-l1（变体命名与文档：BaseXX 规则与自动生成的 doc 注释）** 会从使用侧深入命名规则：在 spacing.rs 里新增间距值并观察新变体的诞生，本讲的推导能力正是那里的验证工具。
- 之后 **u3-l2（密度感知间距）** 讲 `spacing_ratio`/`rems`/`px` 的运行时行为与 `UiDensity` 三档设置，把本讲生成的"死代码"讲"活"。
- 想巩固 quote 手感的读者，建议通读 [quote 的官方文档](https://docs.rs/quote/latest/quote/) 中 Repetition 一节，再回头看本讲 4.2 的两种逗号写法。
- 延伸阅读：`src/derive_register_component.rs` 用同样的 syn/quote 套件实现派生宏（u4-l1 精读），可以对比"函数式宏的列表输入"与"派生宏的 `DeriveInput` 输入"在代码生成上的异同。

# 过程宏工作流与错误处理

## 1. 本讲目标

本讲承接 [u1-l1 项目概览](./u1-l1-overview.md)，带你从「知道 typst-macros 有七个宏」深入到「理解一个宏从用户源码到生成代码的完整数据流」。

学完本讲，你应当能够：

1. 区分 **attribute（属性）**、**function-like（函数式）**、**derive（派生）** 三类过程宏，并说出它们在函数签名上的差异（参数个数、哪个是 `stream`、哪个是 `item`）。
2. 看懂 typst-macros 里 `BoundaryStream` 与 `proc_macro2::TokenStream` 的边界转换，并解释为什么宏内部普遍使用后者。
3. 理解 `parse_macro_input!` 如何把一串 token 解析成某个 `syn` 项。
4. 掌握 `bail!` 宏 + `.unwrap_or_else(|err| err.to_compile_error())` 这套「绝不 panic、把错误变成编译错误」的统一回传模式。

## 2. 前置知识

在阅读源码前，先用三段大白话建立直觉。

### 2.1 什么是「过程宏」

普通宏（`macro_rules!`）靠模式匹配做文本替换，能力有限。**过程宏（procedural macro）** 则是一段真正的 Rust 函数：编译器在编译期调用它，把一段 Rust 源码（以 token 流的形式）喂给它，它返回另一段 token 流，编译器再把返回的 token 流当成真正的代码继续编译。

所以过程宏的本质是一台 **编译期运行的代码翻译器**：输入是「用户写的代码」，输出是「编译器最终看到的代码」。

### 2.2 token 流（TokenStream）

源码被编译器切分后，会变成一串 token（标识符、关键字、标点、字面量……）。这串 token 的容器类型就叫 `TokenStream`。

本讲会遇到 **两个名字几乎一样、但来源不同** 的类型：

| 类型 | 来自 | 特点 |
|---|---|---|
| `proc_macro::TokenStream` | 编译器内置 | 只能在过程宏入口出现；与编译器线程状态绑定 |
| `proc_macro2::TokenStream` | 第三方 crate `proc-macro2` | 可以在任何地方用，被 `syn` / `quote` 全面支持 |

typst-macros 用 `BoundaryStream` 这个别名专门指代「编译器边界」上的前者，内部逻辑则统一用后者。

### 2.3 编译错误也是一种「输出」

过程宏不能随便 `panic!`——一旦 panic，整个编译就崩了，用户体验极差。正确的做法是：遇到非法输入时，**生成一段 `compile_error!("...")` 代码** 作为输出。编译器看到这段代码，就会在用户源码的对应位置报一个正常的编译错误。

typst-macros 把这套机制封装成了 `to_compile_error()` 模式，后面会详细讲。

## 3. 本讲源码地图

本讲只聚焦「所有宏共享的工作流骨架」，涉及两个文件：

| 文件 | 作用 |
|---|---|
| [src/lib.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs) | 七个 `#[proc_macro*]` 入口的集合地。负责接收编译器传入的 token 流、解析、调用子模块、回传结果或错误。 |
| [src/util.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs) | 所有宏共享的工具层，本讲只用到其中的 `bail!` 宏与 `Result`（`syn::Result`）类型别名。 |

子模块（`func.rs` / `cast.rs` / `elem.rs` 等）内部如何生成代码，是后续讲义的内容；本讲只看它们的**入口函数签名**。

## 4. 核心概念与源码讲解

### 4.1 三类过程宏与七个入口的签名

#### 4.1.1 概念说明

Rust 的过程宏分成三类，编译器用三个不同的属性来标记，并要求**对应的函数签名也不同**：

1. **属性宏（attribute macro）** —— 标记 `#[proc_macro_attribute]`。
   写在某个项的前面（如 `#[func] fn double(...)`）。编译器会把「属性括号里的参数」和「被装饰的项」**两部分** token 都传进来，所以签名是 **两个参数**。
2. **函数式宏（function-like macro）** —— 标记 `#[proc_macro]`。
   像 `cast! { ... }` 这样像函数一样调用。编译器只把 `!` 后面花括号里的 token 传进来，签名是 **一个参数**。
3. **派生宏（derive macro）** —— 标记 `#[proc_macro_derive(...)]`。
   跟在 `#[derive(Cast)]` 后面。编译器把被派生的那个**类型定义**整体传进来，签名也是 **一个参数**。

typst-macros 的七个入口正好覆盖这三类。

#### 4.1.2 核心流程

我们先用一张表把七个入口归类：

| 入口 | 宏类型 | 编译器标记 | 参数个数 | 第一个参数含义 | 第二个参数含义 |
|---|---|---|---|---|---|
| `func` | 属性 | `#[proc_macro_attribute]` | 2 | `stream`：属性参数 | `item`：被装饰的函数 |
| `ty` | 属性 | `#[proc_macro_attribute]` | 2 | `stream`：属性参数 | `item`：被装饰的类型项 |
| `elem` | 属性 | `#[proc_macro_attribute]` | 2 | `stream`：属性参数 | `item`：被装饰的结构体 |
| `scope` | 属性 | `#[proc_macro_attribute]` | 2 | `stream`：属性参数 | `item`：被装饰的 impl 块 |
| `time` | 属性 | `#[proc_macro_attribute]` | 2 | `stream`：属性参数 | `item`：被装饰的函数 |
| `cast` | 函数式 | `#[proc_macro]` | 1 | `stream`：`cast! { ... }` 的内容 | —— |
| `derive_cast` | 派生 | `#[proc_macro_derive(Cast, attributes(string))]` | 1 | `item`：被派生的枚举 | —— |

一个**关键命名细节**：在 typst-macros 里，参数叫 `stream` 还是 `item` 不是随意的——
- `stream` 表示「属性的配置参数 / 宏调用体」（会被 `syn::parse2` 进一步解析成元数据）。
- `item` 表示「被装饰或被派生的那一段真实代码」。

派生宏只有一个参数，所以它干脆就叫 `item`；属性宏则 `stream` + `item` 两个都有。

#### 4.1.3 源码精读

先看文件顶部的「边界声明」——它交代了 `BoundaryStream` 这个别名的来历：

[src/lib.rs:L3-L15](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L3-L15) —— `extern crate proc_macro` 链接编译器内置 crate；`#[macro_use] mod util` 让 `util.rs` 里的宏（如 `bail!`）对后续所有模块可见；第 14 行把编译器的 `proc_macro::TokenStream` 起了个别名 `BoundaryStream`，意为「跨边界用的 token 流」。

接着看三类签名各取一个代表。

**属性宏 `func`（两个参数）**：

[src/lib.rs:L103-L109](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L103-L109) —— 标记是 `#[proc_macro_attribute]`，签名 `fn func(stream: BoundaryStream, item: BoundaryStream)`。`stream` 是 `#[func(...)]` 里的参数（如 `title = "Minimum"`），`item` 是整个 `fn double(...) { ... }`。

**函数式宏 `cast`（一个参数）**：

[src/lib.rs:L296-L301](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L296-L301) —— 标记是 `#[proc_macro]`，签名只有一个 `stream`。注意它**没有** `parse_macro_input!` 这一步（为什么？见 4.3）。

**派生宏 `derive_cast`（一个参数）**：

[src/lib.rs:L322-L328](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L322-L328) —— 标记是 `#[proc_macro_derive(Cast, attributes(string))]`，签名只有一个参数，但名字叫 `item`（因为派生宏处理的是「整个类型定义」）。`attributes(string)` 声明了 `#[string]` 这个辅助属性。

其余四个属性宏 `ty` / `elem` / `scope` / `time` 与 `func` 形态一致，只是第二个参数解析成不同的 `syn` 类型：[src/lib.rs:L137-L142](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L137-L142)、[src/lib.rs:L212-L218](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L212-L218)、[src/lib.rs:L260-L266](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L260-L266)、[src/lib.rs:L362-L368](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L362-L368)。

#### 4.1.4 代码实践

**实践目标**：建立「宏类型 → 签名」的肌肉记忆。

**操作步骤**：

1. 打开 [src/lib.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs)，定位到七个 `pub fn`。
2. 对每个入口，记录：编译器属性标记、参数个数、参数名（`stream` 还是 `item`）。
3. 完成上面 4.1.2 的那张表。

**需要观察的现象**：属性宏的标记是 `proc_macro_attribute`、函数式是 `proc_macro`、派生是 `proc_macro_derive`，三者前缀不同；属性宏恒为两参，另两类恒为一参。

**预期结果**：你能不查表说出「`cast` 是函数式宏所以只有一个参数」「`func` 是属性宏所以有 stream 和 item 两个参数」。

#### 4.1.5 小练习与答案

**练习 1**：假如你想新增一个「只能用在函数上、且需要额外配置」的宏（类似 `#[time]`），应该选哪类过程宏？签名长什么样？

> **答案**：选属性宏 `#[proc_macro_attribute]`，因为既要装饰一个项、又要在属性里写配置。签名为 `fn xxx(stream: BoundaryStream, item: BoundaryStream) -> BoundaryStream`。

**练习 2**：为什么 `derive_cast` 的唯一参数叫 `item` 而不叫 `stream`？

> **答案**：派生宏接收的是被派生的「整个类型定义」（这里是枚举），属于真实的代码项，而不是「宏调用体 / 属性参数」，因此按 typst-macros 的命名约定叫 `item`。

---

### 4.2 TokenStream 的边界转换

#### 4.2.1 概念说明

前面提到有两套 `TokenStream`。typst-macros 的策略很清晰：

- **入口处**用编译器的 `proc_macro::TokenStream`（别名 `BoundaryStream`），因为这是编译器唯一愿意交接的类型。
- **内部逻辑**全部转成 `proc_macro2::TokenStream`，因为 `syn`（解析）和 `quote`（生成）都只认它。

这个「转」的动作发生在入口函数里，靠 `.into()` 完成。这就是 `BoundaryStream`（边界流）这个名字的由来——它标记了「跨过这条边界就要换类型」。

#### 4.2.2 核心流程

以 `func` 为例，一个 token 流的往返是这样的：

```
编译器                              typst-macros 内部
─────────────────────────────────────────────────────────
proc_macro::TokenStream (BoundaryStream)
   │  stream.into() / item 先被 parse_macro_input! 解析
   ▼
proc_macro2::TokenStream  ──► syn::ItemFn（结构化的函数项）
   │                          func::func(...) 内部用 quote! 生成
   ▼
proc_macro2::TokenStream（生成的新代码）
   │  .into()
   ▼
proc_macro::TokenStream (BoundaryStream)  ──► 返回给编译器
```

关键点：**进去时 `.into()` 把编译器类型降级成 proc-macro2 类型；出来时再 `.into()` 升回编译器类型**。

#### 4.2.3 源码精读

再看一次 `func` 入口，这次重点放在三个 `.into()` 与一次 `parse_macro_input!`：

[src/lib.rs:L103-L109](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L103-L109)：
- `func::func(stream.into(), &item)` —— 第一个 `.into()` 把 `BoundaryStream` 转成 `proc_macro2::TokenStream`，交给子模块。
- 末尾的 `.into()` —— 把子模块返回的 `proc_macro2::TokenStream` 转回 `BoundaryStream`，返回给编译器。

子模块那一侧，参数类型确认了内部用的是 proc-macro2：

[src/func.rs:L14-L17](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L14-L17) —— `pub fn func(stream: TokenStream, item: &syn::ItemFn) -> Result<TokenStream>`，这里的 `TokenStream` 是文件顶部 `use proc_macro2::TokenStream;` 引入的（见 [src/func.rs:L2](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L2)），返回类型 `Result` 则是 `syn::Result`。

> **为什么内部普遍用 proc_macro2 而不是直接用 proc_macro？**
> 主要有三点：（1）`syn` 和 `quote` 这两个核心库就是围绕 `proc_macro2::TokenStream` 设计的，直接用编译器类型会和它们对不上；（2）`proc_macro::TokenStream` 只能在真正的过程宏 crate 里使用，无法在普通单元测试、build script 里构造，而 proc-macro2 解除了这个限制，让宏逻辑**可以被单元测试**；（3）proc-macro2 的 span 处理更灵活，便于在错误回传时精确定位。

#### 4.2.4 代码实践

**实践目标**：亲手追踪一次类型转换。

**操作步骤**：

1. 在 [src/lib.rs:L104-L108](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L104-L108) 的 `func` 入口里，圈出两处 `.into()`，分别标注「入：BoundaryStream → proc_macro2」和「出：proc_macro2 → BoundaryStream」。
2. 跟着 [src/func.rs:L14-L17](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L14-L17) 确认子模块收到的就是 `proc_macro2::TokenStream`。

**需要观察的现象**：两次 `.into()` 方向相反，恰好一进一出。

**预期结果**：你能解释「编译器只认 BoundaryStream，所以入口必须做两次类型转换」。

#### 4.2.5 小练习与答案

**练习 1**：如果把入口里第一个 `stream.into()` 去掉、直接把 `stream` 传给 `func::func`，会发生什么？

> **答案**：类型不匹配。`func::func` 的第一个参数是 `proc_macro2::TokenStream`，而 `stream` 是 `proc_macro::TokenStream`（BoundaryStream），编译期就会报类型错误。

**练习 2**：为什么把内部 token 流类型选成 proc-macro2，对「测试」特别友好？

> **答案**：因为 `proc_macro::TokenStream` 只在过程宏执行环境里可用，单元测试里构造不出来；proc-macro2 没有这个限制，测试代码可以直接 `quote! { ... }` 出一段 token 流喂给被测函数。

---

### 4.3 parse_macro_input!：把 TokenStream 解析为 syn 项

#### 4.3.1 概念说明

token 流是一串「扁平」的 token，操作起来很痛苦。`syn` 库的作用，是把这串 token **解析成一棵带类型的语法树节点**（比如 `syn::ItemFn` 表示一个完整的函数项、`syn::ItemStruct` 表示一个结构体）。

`parse_macro_input!` 是 `syn` 提供的一个便利宏：它接收 `proc_macro::TokenStream`，按你指定的目标类型解析，成功就返回结构化的项；**失败时会自动生成编译错误并提前从过程宏函数返回**（注意：它不会 panic，也不会走 `?`，而是自己处理错误并 early-return）。

所以你会看到：除了 `cast` 之外，几乎所有入口都用 `parse_macro_input!` 把 `item` 解析成具体的 `syn` 类型。

#### 4.3.2 核心流程

```
item: BoundaryStream
   │
   │  parse_macro_input!(item as syn::ItemFn)
   │  ├─ 解析成功 ──► 返回 syn::ItemFn（继续往下走）
   │  └─ 解析失败 ──► 生成 compile_error! 并直接 return（函数到此结束）
   ▼
结构化的 syn::ItemFn
```

为什么要指定不同的目标类型？因为不同宏装饰的东西不同：

- `func` / `time` 装饰函数 → 解析为 `syn::ItemFn`
- `elem` 装饰结构体 → 解析为 `syn::ItemStruct`
- `ty` / `scope` 装饰「任意项」（可能是 struct、enum、type、impl）→ 解析为更宽泛的 `syn::Item`
- `derive_cast` 派生在枚举上 → 解析为 `syn::DeriveInput`

而 `cast` 是函数式宏，它的「调用体」不是某个现成的 Rust 项，而是一套**自定义语法**（`self => ...`、`v: i64 => ...`），所以它不走 `parse_macro_input!`，而是在子模块里用 `syn::parse2` 解析成自定义的 `CastInput`（见 [src/cast.rs:L71-L73](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/cast.rs#L71-L73)）。这就是 4.1.3 里 `cast` 入口没有 `parse_macro_input!` 的原因。

#### 4.3.3 源码精读

**典型用法（`func`）**：

[src/lib.rs:L104-L106](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L104-L106) —— `let item = syn::parse_macro_input!(item as syn::ItemFn);` 把被装饰的 token 流解析成一个 `syn::ItemFn`，之后传给 `func::func` 的就是结构化的函数项了。

**宽泛目标（`ty` / `scope`）**：

[src/lib.rs:L138](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L138) 与 [src/lib.rs:L262](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L262) —— 都解析为 `syn::Item`，因为它们要兼容多种项类型。

**派生宏（`derive_cast`）**：

[src/lib.rs:L323-L324](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L323-L324) —— 解析为 `DeriveInput`（[src/lib.rs:L15](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L15) 单独 `use syn::DeriveInput;` 就是为了这里）。

**反例（`cast` 不用它）**：

[src/lib.rs:L297-L298](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L297-L298) —— 直接 `cast::cast(stream.into())`，把解析延迟到子模块里用 `syn::parse2` 处理自定义语法。

#### 4.3.4 代码实践

**实践目标**：理解「目标类型」的选择逻辑。

**操作步骤**：

1. 通读 [src/lib.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs) 里七个入口的 `parse_macro_input!` 调用。
2. 列出每个入口解析的目标类型，并思考「为什么是这个类型」。
3. 对比 [src/cast.rs:L72](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/cast.rs#L72) 的 `syn::parse2::<CastInput>`，理解函数式宏为何改用 `parse2`。

**需要观察的现象**：属性宏的解析目标 = 它装饰的项的类型；函数式宏的解析目标 = 自定义语法的类型。

**预期结果**：你能解释「`elem` 必须解析为 `ItemStruct`，因为 `#[elem]` 只能贴在结构体上」。

#### 4.3.5 小练习与答案

**练习 1**：`ty` 和 `scope` 为什么解析为 `syn::Item` 而不是某个具体类型？

> **答案**：`#[ty]` 可能贴在 struct、enum、type alias 等多种项上，`#[scope]` 贴在 impl 块上，因此需要一个能涵盖多种项的宽泛类型，`syn::Item` 正是「任意 Rust 项」的枚举。

**练习 2**：如果用户把 `#[func]` 贴在一个 `struct` 上（而不是函数），会在哪一步报错？

> **答案**：在 `parse_macro_input!(item as syn::ItemFn)` 这一步——它期望解析成函数项，遇到 struct 就解析失败，`parse_macro_input!` 会自动生成编译错误并提前返回。

---

### 4.4 错误的创建与回传：bail! 与 to_compile_error 模式

#### 4.4.1 概念说明

过程宏里有两层「可能出错」的地方，typst-macros 用两套不同的机制处理：

1. **解析用户写的那段代码时出错**（比如 `#[func]` 贴在了 struct 上）——由 `parse_macro_input!` 自动处理（见 4.3）。
2. **宏的内部逻辑判断输入不合法时出错**（比如某个属性写错了值）——由本节讲的 `bail!` + `to_compile_error` 模式处理。

这套模式的核心思想是：**子模块函数返回 `syn::Result<TokenStream>`，出错就返回 `Err(syn::Error)`；入口函数用 `.unwrap_or_else(|err| err.to_compile_error())` 把错误转成一段 `compile_error!` 代码返回给编译器。全程绝不 panic。**

`syn::Error` 的妙处在于它**携带 span（源码位置）信息**：报错时能精确定位到用户源码的哪一行哪一段，而不是笼统地说「宏出错了」。

#### 4.4.2 核心流程

```
                  子模块（如 func::func）
                  ──────────────────────
   输入非法 ──►  bail!(某item, "typst: 错误说明")
                      │  展开为：return Err(syn::Error::new_spanned(&item, ...))
                      ▼
                 返回 Err(syn::Error)   ◄── 带有精确 span
                      │
   正常    ──►  返回 Ok(TokenStream)
                      │
                      ▼
            ┌─────────────────────────────────────────┐
            │  入口函数（lib.rs）                       │
            │  .unwrap_or_else(|err| err.to_compile_error())  │
            │   ├─ Ok(ts)  ──► ts                       │
            │   └─ Err(e)  ──► e.to_compile_error()     │
            │                  生成 compile_error!("typst: …") │
            └─────────────────────────────────────────┘
                      │  .into()
                      ▼
              BoundaryStream ──► 编译器（在用户源码处报编译错误）
```

#### 4.4.3 源码精读

**第一步：`bail!` 宏——在子模块里创建带 span 的错误。**

[src/util.rs:L9-L23](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L9-L23) —— 这是个 `macro_rules!` 宏，定义在 `util.rs` 里，靠 lib.rs 顶部的 `#[macro_use] mod util;`（[src/lib.rs:L5-L6](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L5-L6)）对所有子模块可见。它有两个分支：

- `bail!(callsite, ...)` —— 用当前调用点（call site）的 span，见 [src/util.rs:L11-L16](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L11-L16)。
- `bail!($item, ...)` —— 把错误「钉」在某个具体的语法项上（`syn::Error::new_spanned`），这样报错位置更精准，见 [src/util.rs:L17-L22](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L17-L22)。

两种都把消息前缀统一加上 `"typst: "`，让用户一眼看出是 typst 的宏在报错。展开后都是 `return Err(...)`——注意是直接 `return`，所以 `bail!` 之后就立刻退出当前函数。

`bail!` 在 `util.rs` 自身也被用到，例如校验非法属性时：[src/util.rs:L94-L102](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L94-L102) 的 `validate_attrs` 遇到不认识的属性就 `bail!(ident, "unrecognized attribute: {ident}")`。

**第二步：`Result` 类型别名——子模块统一返回 `syn::Result`。**

[src/util.rs:L7](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L7) —— `use syn::{Attribute, Ident, Result, Token};`，这里的 `Result` 就是 `syn::Result<T>`（即 `std::result::Result<T, syn::Error>`）。所以 [src/func.rs:L14](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L14) 的 `-> Result<TokenStream>` 实际返回 `Result<proc_macro2::TokenStream, syn::Error>`。

**第三步：入口函数的统一收尾——`.unwrap_or_else(|err| err.to_compile_error())`。**

回到 [src/lib.rs:L106-L108](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L106-L108)（`func`）：
- `func::func(...)` 返回 `Result<TokenStream, syn::Error>`。
- `.unwrap_or_else(|err| err.to_compile_error())`：成功就直接用那份 `TokenStream`；失败就把 `syn::Error` 调用 `to_compile_error()`，生成一段形如 `compile_error!("typst: …")`、且带正确 span 的 token 流。
- `.into()` 再把它转回 `BoundaryStream` 返回。

**这套模板在七个入口里几乎一字不差地重复**，例如 `cast`：[src/lib.rs:L298-L300](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L298-L300)、`derive_cast`：[src/lib.rs:L325-L327](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L325-L327)。这是本讲最重要的「模式」——务必记住它。

#### 4.4.4 代码实践

**实践目标**：亲手走一遍「非法输入 → 编译错误」的路径。

**操作步骤**（源码阅读型，无需运行）：

1. 在 [src/util.rs:L94-L102](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L94-L102) 找到 `validate_attrs`，想象一个 `#[func]` 函数上残留了一个 typst 不认识的属性 `#[foobar]`。
2. 追踪它如何走到 `bail!(ident, "unrecognized attribute: {ident}")`（[src/util.rs:L98](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L98)），产生一个 `Err(syn::Error)`。
3. 这个 `Err` 一路 `?` 冒泡回 `func::func`，再到入口 [src/lib.rs:L107](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L107) 的 `.unwrap_or_else(|err| err.to_compile_error())`，被转成 `compile_error!`。
4. **需要观察的现象**：错误信息以 `typst: ` 开头，且报错位置指向 `foobar` 这个标识符所在的行。

**预期结果**：你能画出从 `bail!` 到最终编译错误的完整调用链。

**待本地验证**：如果想亲眼看到报错，可在 typst 仓库里临时给某个 `#[func]` 函数加一个非法属性后 `cargo build`（本讲不修改源码，仅作说明）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 typst-macros 坚持用 `to_compile_error()` 而不是直接 `unwrap()` 或 `panic!`？

> **答案**：`unwrap`/`panic!` 会让编译器收到过程宏崩溃的信号，报一个难看的内部错误，且无法指向用户源码的具体位置；`to_compile_error()` 生成带 span 的 `compile_error!`，能在用户写错的那一行给出清晰、定位准确的编译错误，体验好得多。

**练习 2**：`bail!` 宏展开后是 `return Err(...)`，这对使用它的函数签名有什么要求？

> **答案**：调用 `bail!` 的函数返回类型必须是 `Result<_, syn::Error>`（即 `syn::Result<_>`），这样 `return Err(syn::Error::...)` 才能通过类型检查。typst-macros 的所有子模块入口都满足这一点。

**练习 3**：`bail!(ident, ...)` 和 `bail!(callsite, ...)` 有什么区别？什么时候用哪个？

> **答案**：前者把错误定位到某个具体的语法项（`new_spanned`），报错位置精准；后者定位到宏展开的调用点（`call_site`），位置较泛。当能指到具体的出错标识符/字段时用前者；当错误与整体输入有关、没有单一明确的位置时用后者。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个**数据流图 + 说明**任务（对应本讲规格里的实践任务）。

### 任务一：画出 `#[func]` 的端到端数据流图

任选 `func` 入口，画出「用户源码 → 最终生成的 TokenStream」的完整数据流，**每一步都要标注类型**。参考答案如下：

```
用户源码:
  #[func(title = "Minimum")]
  fn min(...) -> i64 { ... }
        │
        │  编译器把属性参数和函数项分别打包
        ▼
  stream: proc_macro::TokenStream (BoundaryStream)   ← title="Minimum"
  item:   proc_macro::TokenStream (BoundaryStream)   ← fn min ...
        │
        │  [4.1] 属性宏签名 func(stream, item)
        │  [4.3] parse_macro_input!(item as syn::ItemFn)
        ▼
  item: syn::ItemFn                                   ← 结构化函数项
  stream: BoundaryStream
        │
        │  [4.2] stream.into()
        ▼
  stream: proc_macro2::TokenStream
        │
        │  func::func(stream: proc_macro2::TokenStream, &syn::ItemFn)
        │    -> syn::Result<proc_macro2::TokenStream>
        │     ├─ Ok(生成的 quote! 代码): proc_macro2::TokenStream
        │     └─ Err(syn::Error)（带 span）
        ▼
  syn::Result<proc_macro2::TokenStream>
        │
        │  [4.4] .unwrap_or_else(|err| err.to_compile_error())
        │     ├─ Ok  ──► proc_macro2::TokenStream（生成的代码）
        │     └─ Err ──► proc_macro2::TokenStream（compile_error!）
        ▼
  proc_macro2::TokenStream
        │
        │  [4.2] .into()
        ▼
  proc_macro::TokenStream (BoundaryStream)  ──► 返回编译器
```

### 任务二：解释「为什么内部普遍用 proc_macro2::TokenStream」

请用一段话说明（不少于三点）。参考要点：

1. `syn` 与 `quote` 围绕 `proc_macro2::TokenStream` 设计，直接用编译器类型无法与它们配合。
2. `proc_macro::TokenStream` 只能在过程宏执行环境里出现，无法在单元测试中构造；proc-macro2 解除该限制，使宏逻辑**可被测试**。
3. proc-macro2 的 span 机制更灵活，便于在 `to_compile_error()` 时精确定位用户源码。

### 任务三（可选，进阶）：对比一个错误场景

阅读 [src/util.rs:L94-L102](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L94-L102) 的 `validate_attrs`，描述：当某个 `#[func]` 函数上残留了一个既不是 `doc` 也不是 `derive` 的陌生属性时，错误是如何从 `bail!` 一路传到用户终端的编译错误的。把你画的链路写下来。

## 6. 本讲小结

- typst-macros 的七个入口分属三类过程宏：属性宏（`func`/`ty`/`elem`/`scope`/`time`，两参 `stream`+`item`）、函数式宏（`cast`，一参 `stream`）、派生宏（`derive_cast`，一参 `item`）。
- `BoundaryStream` 是编译器 `proc_macro::TokenStream` 的别名，标记「类型边界」；入口用 `.into()` 把它转成 `proc_macro2::TokenStream` 交给内部，返回前再 `.into()` 转回去。
- `parse_macro_input!(item as 某syn类型)` 把扁平 token 流解析成结构化的语法树节点；失败时它**自动生成编译错误并 early-return**，不会 panic。
- 子模块统一返回 `syn::Result<TokenStream>`，内部用 `bail!` 宏（`util.rs` 中定义，经 `#[macro_use] mod util` 暴露）创建带 span 的 `syn::Error`。
- 入口函数用 `.unwrap_or_else(|err| err.to_compile_error())` 把 `Err` 转成 `compile_error!` 代码，**全程绝不 panic**，且错误能精确定位到用户源码——这套模板在七个入口里几乎一字不差地重复。

## 7. 下一步学习建议

本讲建立的是「骨架」：签名、边界、解析、错误。接下来建议：

1. **学 [u1-l3 共享工具层 util.rs](./u1-l3-shared-util.md)**：深入 `util.rs` 里 `documentation`、`has_attr`/`take_attr`/`parse_attr`/`validate_attrs`、`determine_name_and_title` 等工具，理解子模块 `parse` 阶段都依赖哪些共用函数。
2. **之后进入第二单元**：从最简单的 [u2-l1 `#[time]` 计时宏](./u2-l1-time-macro.md) 开始，第一次完整走「解析 `Meta` → 修改 `ItemFn` → 注入语句」的 `parse → create` 流水线，把本讲的骨架填上血肉。

阅读时可以常备本讲的数据流图，后续每个宏都遵循同样的「入口 → parse_macro_input! → 子模块 parse/create → unwrap_or_else(to_compile_error) → .into()」结构，只是 `parse` 和 `create` 的细节不同。

# 项目概览：typst-macros 是什么

## 1. 本讲目标

本讲是整个 `typst-macros` 学习手册的第一篇。读完本讲后，你应该能够：

- 说清楚 `typst-macros` 在 Typst 项目里扮演什么角色，它如何把「原生 Rust 代码」桥接成「Typst 运行时能识别的函数 / 类型 / 元素」。
- 理解一个 Rust **过程宏 crate**（proc-macro crate）和普通库 crate 的区别，特别是 `Cargo.toml` 里 `proc-macro = true` 这一行意味着什么。
- 说出本 crate 提供的**七个公开宏**（`func`、`ty`、`elem`、`scope`、`cast`、`derive_cast`、`time`）各自的作用、属于哪一类过程宏、以及它们实现的是 Typst 运行时的哪个 trait。
- 理解 `src/lib.rs` 作为入口文件，是如何通过 `extern crate proc_macro;`、模块声明和 `#[proc_macro]` 入口函数，把请求分发到各个子模块的。

本讲只做「认路」，不深入任何一个宏的内部实现。后续每一讲会分别深入一个宏。

## 2. 前置知识

在开始之前，你需要对以下几个概念有最基本的了解。不熟悉也没关系，我们用通俗的方式解释。

### 2.1 什么是 Typst

Typst 是一个用 Rust 编写的现代排版系统（类似 LaTeX 的替代品）。它的核心是一个**运行时（runtime）**，能够执行用户用 Typst 语言写的脚本、调用内置函数（如 `min`、`str`）、渲染各种元素（如标题 `heading`、矩形 `rect`）。

这些「内置函数、内置类型、内置元素」如果都用 Typst 语言自己实现，会很慢且不灵活。因此 Typst 的做法是：**用 Rust 原生代码实现它们，再通过某种机制注册到运行时里**。`typst-macros` 就是这个「某种机制」的核心——它用过程宏自动生成注册代码。

### 2.2 什么是过程宏（procedural macro）

Rust 有两种宏：

- **声明宏（macro_rules!）**：用模式匹配的方式做文本替换，功能有限。
- **过程宏（procedural macro）**：是一段真正的 Rust 函数，它接收**一段 Rust 源码**，经过任意计算后，输出**另一段 Rust 源码**。可以理解为「在编译期运行的、用来生成代码的代码」。

过程宏又分三类，本讲会反复用到这个分类，请记住：

| 类别 | 标记 | 签名（简化） | 典型用法 |
|------|------|--------------|----------|
| 属性宏（attribute） | `#[proc_macro_attribute]` | `(属性流, 项流) -> 项流` | `#[func] fn double(x: i64) -> i64 { ... }` |
| 函数式宏（function-like） | `#[proc_macro]` | `(输入流) -> 输出流` | `cast! { CoolInt, ... }` |
| 派生宏（derive） | `#[proc_macro_derive(...)]` | `(项流) -> 输出流` | `#[derive(Cast)] enum Niceness { ... }` |

过程宏操作的基本单位是 **TokenStream（记号流）**——你可以把它粗略地理解为「一段源码被切分成的一个个记号（token）的序列」。

### 2.3 涉及的几个关键 crate

`typst-macros` 依赖四个外部 crate，后面会反复出现：

- **`proc-macro2`**：标准库 `proc_macro` 的一个「增强分身」。标准库的 `proc_macro::TokenStream` 只能在过程宏内部使用；`proc-macro2` 让 `TokenStream` 可以在任何地方使用（这是后面 `quote!` 宏能工作的前提）。
- **`syn`**：把 `TokenStream` **解析（parse）**成一棵抽象语法树（AST），这样宏就能理解「这是一个函数」「这是一个结构体」。
- **`quote`**：提供 `quote!` 宏，用类似模板的语法**生成** `TokenStream`。
- **`heck`**：一个字符串大小写转换工具，比如把 `HeadingElem` 转成 `heading`（kebab-case）或 `Heading`（title case）。

记住这条主线：**`syn` 负责「读懂」，`quote` 负责「生成」，`proc-macro2` 是它们之间的桥梁，`heck` 负责起名字**。

## 3. 本讲源码地图

本讲只涉及两个文件，它们是整个 crate 的「地基」：

| 文件 | 行数级别 | 作用 |
|------|----------|------|
| `Cargo.toml` | 约 26 行 | 声明这是一个过程宏 crate，并列出四个依赖。 |
| `src/lib.rs` | 约 369 行 | crate 的唯一入口，定义七个公开的 `#[proc_macro]` 函数，并把具体工作分发到 `cast/elem/func/scope/time/ty/util` 七个子模块。 |

为方便对照，下面是 `src/` 目录下的全部文件（本讲不深入除 `lib.rs` 外的其他文件，但知道它们的存在有助于建立全局观）：

```
src/
├── lib.rs     # 入口：七个 #[proc_macro] 公开入口
├── util.rs    # 所有宏共用的工具层（文档提取、属性解析、命名推导等）
├── cast.rs    # cast! 函数式宏 + #[derive(Cast)] 派生宏
├── ty.rs      # #[ty] 类型宏
├── elem.rs    # #[elem] 元素宏（本 crate 最复杂的宏）
├── func.rs    # #[func] 函数宏
├── scope.rs   # #[scope] 作用域组装宏
└── time.rs    # #[time] 计时宏（最简单的一个）
```

可以看到：**`lib.rs` 里有七个入口，正好对应 `cast/elem/func/scope/time/ty` 六个功能模块，再加一个 `util` 共用工具模块**。这种「入口薄、分发到模块」的组织方式非常清晰。

## 4. 核心概念与源码讲解

本讲按规格拆成三个最小模块，每个模块都对应本讲的一个学习目标。

### 4.1 Cargo.toml 的 `[lib] proc-macro = true`

#### 4.1.1 概念说明

一个普通的 Rust 库 crate（`[lib]` 里什么都不写）编译出来的是一个「普通库」，可以被别的 crate 直接 `use`。但**过程宏 crate 不一样**：它编译出来的不是普通的库，而是一个**编译器插件**——它要在「编译别的 crate 时」由编译器加载并执行。

要告诉 Rust 编译器「这个 crate 是过程宏 crate，请用特殊方式编译它」，就必须在 `Cargo.toml` 里写一行：

```toml
[lib]
proc-macro = true
```

少了这一行，整个 crate 的所有 `#[proc_macro]` 函数都会编译失败。这一行是过程宏 crate 的「身份证」。

#### 4.1.2 核心流程

过程宏 crate 的「编译 → 加载 → 执行」流程大致如下：

1. **编译期**：Cargo 看到 `proc-macro = true`，把本 crate 编译成一个**动态库**（而不是普通静态库）。
2. **被使用时**：当用户 crate（如 `typst-library`）写了 `#[func] fn double(...)`，Rust 编译器加载这个动态库。
3. **执行**：编译器把 `fn double(...)` 那段源码转成 `TokenStream`，调用本 crate 的 `func` 函数。
4. **回传**：本 crate 处理后返回新的 `TokenStream`，编译器把它当作最终代码继续编译。

整个过程对最终用户透明：用户只看到一句 `#[func]`，背后的代码生成全由本 crate 完成。

#### 4.1.3 源码精读

打开 [Cargo.toml:15-22](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/Cargo.toml#L15-L22)，这是本 crate 最关键的配置：

```toml
[lib]
proc-macro = true

[dependencies]
heck = { workspace = true }
proc-macro2 = { workspace = true }
quote = { workspace = true }
syn = { workspace = true }
```

- `[lib]` 段下的 `proc-macro = true`（第 16 行）就是上面说的「身份证」，**没有它就没有本 crate 的一切**。
- `[dependencies]` 列出了四个依赖，正是第 2.3 节介绍的四个 crate。注意它们都用了 `{ workspace = true }`，表示版本号由**工作区根目录的 `Cargo.toml`** 统一管理——这是 Typst 这种多 crate 大项目的常见做法，保证所有 crate 用同一版本的 `syn`、`quote` 等，避免版本冲突。

> 小知识：过程宏 crate 有一个限制——**它不能导出除过程宏以外的任何公共项**（不能导出普通函数、结构体等）。这也是为什么本 crate 只暴露七个宏，所有工具函数都藏在内部模块里。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `proc-macro = true` 的作用，理解它不可省略。

**操作步骤**：

1. 打开本 crate 的 `Cargo.toml`，找到第 16 行 `proc-macro = true`。
2. 想象一个对照实验（**不要真的改源码**，只在脑中推演）：如果把这一行删掉，再 `cargo build`，会发生什么？
3. 对照 [Cargo.toml:18-22](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/Cargo.toml#L18-L22) 里的四个依赖，逐个回忆它们在第 2.3 节里的职责。

**需要观察的现象 / 预期结果**：

- 删掉 `proc-macro = true` 后，`src/lib.rs` 里的 `#[proc_macro_attribute]`、`#[proc_macro]`、`#[proc_macro_derive]` 这些属性会全部报错，因为它们只能出现在过程宏 crate 里。错误信息大致会提示「`proc_macro` 相关的属性只能在 proc-macro crate 中使用」。
- 这反向证明了：**这一行是整个 crate 得以存在的根基**。

> 本实践属于「源码阅读型实践」，不需要真正运行命令，重点是建立「配置 → 能力」的因果直觉。

#### 4.1.5 小练习与答案

**练习 1**：为什么过程宏 crate 必须把 `proc-macro = true` 写在 `[lib]` 段，而不是写在 `[package]` 段？

**参考答案**：因为 `proc-macro = true` 修饰的是**库目标（lib target）本身的编译方式**，告诉 Cargo「把这个 lib 编译成编译器可加载的动态库」，属于 lib 的属性，所以放在 `[lib]` 段。`[package]` 段描述的是包的元信息（名字、版本、作者等），与编译产物形态无关。

**练习 2**：本 crate 依赖了 `heck`，但 `heck` 不像 `syn`/`quote`/`proc-macro2` 那样直接处理 TokenStream，那它用来做什么？

**参考答案**：`heck` 是字符串大小写转换库。本 crate 需要把 Rust 的类型名（如 `HeadingElem`）自动转成 Typst 里暴露给用户的名字（kebab-case 的 `heading`）和标题（title case 的 `Heading`），这些转换由 `heck` 完成。

---

### 4.2 lib.rs 的七个 `#[proc_macro]` 入口

#### 4.2.1 概念说明

`src/lib.rs` 是整个 crate**唯一对外暴露的入口**。它定义了七个公开函数，每个函数头上都有一个 `#[proc_macro...]` 属性，告诉编译器「这是一个过程宏入口」。这七个入口就是本 crate 对外提供的全部能力。

七个宏按「实现 Typst 运行时的哪个 trait」可以分成几组：

- **`#[func]`**：把一个 Rust 函数变成 Typst 函数 → 实现 `NativeFunction`。
- **`#[ty]`**：把一个 Rust 类型变成 Typst 类型 → 实现 `NativeType`。
- **`#[elem]`**：把一个 Rust 结构体变成 Typst 元素（排版节点）→ 实现 `NativeElement`。
- **`#[scope]`**：为一个函数/类型/元素挂载一个「作用域」（里面装常量、子函数、子类型、子元素）→ 实现 `NativeScope`。
- **`cast!`**：定义一个 Rust 类型与 Typst 值（`Value`）之间如何互转 → 实现 `Reflect`/`FromValue`/`IntoValue`。
- **`#[derive(Cast)]`**：专门给枚举用的简化版 `cast!`，把枚举变体映射成 kebab-case 字符串。
- **`#[time]`**：给一个函数加上计时埋点，用于性能追踪（最特殊，不实现运行时 trait，而是注入计时代码）。

#### 4.2.2 核心流程

虽然七个宏功能各异，但它们的入口函数长得几乎一模一样，遵循同一个「三步走」模板：

```
1. parse_macro_input!(item as 某个 syn 类型)   // 用 syn 把输入 TokenStream 解析成 AST 节点
2. 调用对应模块的处理函数，例如 func::func(stream, &item)
3. .unwrap_or_else(|err| err.to_compile_error()) // 出错时把错误变成编译错误
   .into()                                         // 把 proc_macro2::TokenStream 转回 proc_macro::TokenStream
```

其中第 3 步特别值得记住：过程宏**没有 `panic!` 的权利**（panic 会让编译直接崩溃，给用户极差的体验）。正确做法是把所有错误封装成 `syn::Error`，再用 `to_compile_error()` 转成一段「合法但会报错的 TokenStream」回传，这样错误就能以正常的编译错误形式展示给用户。

#### 4.2.3 源码精读

我们逐个看七个入口。先看一个属性宏的典型代表 `func`：

[lib.rs:103-109](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L103-L109)：

```rust
#[proc_macro_attribute]
pub fn func(stream: BoundaryStream, item: BoundaryStream) -> BoundaryStream {
    let item = syn::parse_macro_input!(item as syn::ItemFn);
    func::func(stream.into(), &item)
        .unwrap_or_else(|err| err.to_compile_error())
        .into()
}
```

读这段代码：

- `#[proc_macro_attribute]` 标明这是**属性宏**，所以签名是 `(stream, item)` 两个参数：`stream` 是 `#[func(...)]` 括号里的属性参数，`item` 是被修饰的函数本身。
- `BoundaryStream` 就是 `proc_macro::TokenStream` 的别名（见第 4.3 节）。
- `parse_macro_input!(item as syn::ItemFn)` 把输入解析成 syn 的「函数项」节点；如果用户的代码不是合法函数，这里会自动报错。
- `func::func(stream.into(), &item)` 把工作交给 `func` 子模块；`.into()` 把编译器的 `TokenStream` 转成 `proc_macro2::TokenStream`（第 4.3 节会解释为什么）。

其余几个属性宏（`ty`、`elem`、`scope`、`time`）结构完全相同，只是解析的 syn 类型不同：

| 入口 | 标记 | 解析的 syn 类型 | 实现的 trait | 链接 |
|------|------|-----------------|--------------|------|
| `func` | `#[proc_macro_attribute]` | `syn::ItemFn` | `NativeFunction` | [lib.rs:104](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L104) |
| `ty` | `#[proc_macro_attribute]` | `syn::Item` | `NativeType` | [lib.rs:137-141](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L137-L141) |
| `elem` | `#[proc_macro_attribute]` | `syn::ItemStruct` | `NativeElement` | [lib.rs:213-217](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L213-L217) |
| `scope` | `#[proc_macro_attribute]` | `syn::Item` | `NativeScope` | [lib.rs:261-265](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L261-L265) |
| `time` | `#[proc_macro_attribute]` | `syn::ItemFn` | （注入计时代码，非 trait） | [lib.rs:363-367](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L363-L367) |

注意 `ty` 和 `scope` 解析的是通用的 `syn::Item`（因为它们能接受结构体、枚举、类型别名等多种项），而 `func`/`time` 只接受函数 `ItemFn`，`elem` 只接受结构体 `ItemStruct`。

再看**函数式宏** `cast!`，它的签名只有**一个参数**：

[lib.rs:296-301](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L296-L301)：

```rust
#[proc_macro]
pub fn cast(stream: BoundaryStream) -> BoundaryStream {
    cast::cast(stream.into())
        .unwrap_or_else(|err| err.to_compile_error())
        .into()
}
```

它用的是 `#[proc_macro]`（不是 `_attribute`），所以像普通函数调用 `cast! { ... }` 一样使用，括号里的全部内容就是那唯一的 `stream`。它也没有 `parse_macro_input!`，而是把整个流直接交给 `cast::cast` 去自定义解析。

最后看**派生宏** `derive_cast`：

[lib.rs:322-328](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L322-L328)：

```rust
#[proc_macro_derive(Cast, attributes(string))]
pub fn derive_cast(item: BoundaryStream) -> BoundaryStream {
    let item = syn::parse_macro_input!(item as DeriveInput);
    cast::derive_cast(item)
        .unwrap_or_else(|err| err.to_compile_error())
        .into()
}
```

- `#[proc_macro_derive(Cast, attributes(string))]` 表示这是名为 `Cast` 的派生宏（用户写 `#[derive(Cast)]`），并且允许在被修饰的类型上使用 `#[string]` 辅助属性（例如 `#[string("❌")]` 来指定变体对应的字符串）。
- 派生宏的签名也只有一个 `item` 参数（被派生的那个枚举），解析成 `syn::DeriveInput`。

> 小结：五个属性宏 `(stream, item)` 两参，一个函数式宏 `cast` 一参，一个派生宏 `derive_cast` 一参。记住「**属性宏两参，其余一参**」这个规律，就能快速判断一个入口属于哪一类。

#### 4.2.4 代码实践

**实践目标**：把七个公开宏整理成「输入项类型 → 实现的 trait → 生成内容」的卡片，建立全局认知。

**操作步骤**：

1. 打开 [src/lib.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs)，每个宏上方都有一段详细的 `///` 文档注释（例如 `func` 的注释在第 17-102 行），阅读每段注释开头的一句话定位。
2. 仿照下面的格式，自己填写一张卡片表（下面已给出 `func` 和 `cast` 作为范例，请补全其余五个）：

   | 宏 | 类别 | 输入项类型 | 实现的 trait | 生成内容概要 |
   |----|------|-----------|--------------|--------------|
   | `#[func]` | 属性宏 | `fn double(x: i64) -> i64` | `NativeFunction` | 一个与函数同名的空 enum + 该 enum 的 `NativeFunction` 实现 |
   | `cast!` | 函数式宏 | `CoolInt` 类型 + 转换规则 | `Reflect`/`FromValue`/`IntoValue` | 上述三个 trait 的 impl |
   | `#[ty]` | ？ | ？ | ？ | ？ |
   | ... | ... | ... | ... | ... |

3. 填完后，与第 4.2.3 节的表格对照查漏补缺。

**需要观察的现象 / 预期结果**：

- 你会发现 `func`、`ty`、`elem`、`scope` 四个宏最终都实现了一个 `NativeXxx` trait，这正是「把 Rust 项注册成 Typst 运行时对象」的统一机制。
- `cast!` 和 `derive_cast` 实现的是同一组 trait（`Reflect`/`FromValue`/`IntoValue`），区别只在于 `derive_cast` 仅支持枚举、是 `cast!` 的便捷封装。
- `time` 是唯一一个「不实现运行时 trait、只做代码注入」的宏，它是为性能追踪服务的。

**关于「同名 enum」的思考题（必做）**：

`#[func]` 是属性宏，修饰的是一个**函数**，但它生成的却是一个与函数**同名**的空 `enum`。例如对 `fn double`，宏会生成 `enum double {}`。函数和类型同名，为什么不会冲突？

**预期结果 / 答案**：因为 Rust 中**函数和类型位于不同的命名空间（namespace）**。`fn double()` 里的 `double` 属于「值命名空间」（函数本身就是个值），而 `enum double {}` 里的 `double` 属于「类型命名空间」。两者互不干扰，可以共存。这一点在源码注释里也有明确说明，见 [lib.rs:17-22](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L17-L22)：

> "This implements `NativeFunction` for a freshly generated type with the same name as a function. (In Rust, functions and types live in separate namespace, so both can coexist.)"

这种「借用类型命名空间生成影子类型」的技巧是 `#[func]` 的核心，后续 `func` 两讲会深入展开。

#### 4.2.5 小练习与答案

**练习 1**：`#[elem]` 宏为什么把输入解析成 `syn::ItemStruct` 而不是 `syn::Item`？

**参考答案**：因为 Typst 的「元素」在 Rust 侧必然用一个**结构体**来表达（结构体的字段就是元素的属性，如 `level`、`body`）。如果用户把 `#[elem]` 加在枚举或函数上，那就是用法错误，`parse_macro_input!(item as syn::ItemStruct)` 会直接报错，从而在编译期挡住错误用法。

**练习 2**：`cast!` 入口里为什么没有 `parse_macro_input!`，而 `func`/`elem` 等都有？

**参考答案**：`cast!` 是函数式宏，它的输入不是某个固定的 Rust 项（不是函数、不是结构体），而是 `cast` 自定义的一套 DSL 语法（类型名 + `self => ...` + 若干 `v: T => ...` 转换臂）。这种自定义语法没有现成的 syn 类型可解析，所以入口把整个 `TokenStream` 原样传给 `cast::cast`，由它自己用 syn 的低层 API 逐段解析。

**练习 3**：派生宏 `derive_cast` 上写的是 `#[proc_macro_derive(Cast, attributes(string))]`，其中 `attributes(string)` 的作用是什么？

**参考答案**：它声明 `#[string]` 是本派生宏的「辅助属性（helper attribute）」。这样当用户在枚举变体上写 `#[string("❌")]` 时，编译器不会报「未知属性」错误，而是允许它存在，以便宏内部读取它来覆盖变体默认的字符串映射。

---

### 4.3 lib.rs 开头的模块声明与 `extern crate proc_macro`

#### 4.3.1 概念说明

读完七个入口后，我们回头看 `lib.rs` 最开头的几行——它们虽短，却决定了整个 crate 的「骨架」。这部分解决三个问题：

1. 为什么开头要写 `extern crate proc_macro;`？
2. 七个功能模块是如何声明和组织的？`#[macro_use]` 又是什么？
3. `use proc_macro::TokenStream as BoundaryStream;` 为什么要起 `BoundaryStream` 这个怪名字？

#### 4.3.2 核心流程

`lib.rs` 顶部的组织逻辑可以画成：

```
extern crate proc_macro;        ← 显式链接编译器提供的 proc_macro crate
        ↓
#[macro_use] mod util;          ← util 模块，且其中的宏对后续所有模块可见
mod cast; mod elem; mod func;   ← 六个功能模块，各自实现一个/两个宏
mod scope; mod time; mod ty;
        ↓
use proc_macro::TokenStream as BoundaryStream;  ← 给 TokenStream 起别名
use syn::DeriveInput;
        ↓
七个 pub fn 入口（见 4.2 节）
```

#### 4.3.3 源码精读

**① `extern crate proc_macro;`**

见 [lib.rs:3](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L3)。

`proc_macro` 是 Rust 编译器**内置**提供的一个 crate，专门用于编写过程宏。在 Rust 2018 之后的 edition 中，大多数外部 crate 都可以省略 `extern crate` 直接用，但 `proc_macro` 是一个例外——**在过程宏 crate 里，通常仍需显式写 `extern crate proc_macro;`**，才能在文件里直接使用 `proc_macro::TokenStream`、`proc_macro::TokenStream::from` 等符号。这一行是过程宏 crate 的标准起手式。

**② 模块声明**

见 [lib.rs:5-12](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L5-L12)：

```rust
#[macro_use]
mod util;
mod cast;
mod elem;
mod func;
mod scope;
mod time;
mod ty;
```

这里有两个要点：

- **`#[macro_use] mod util;`**：`util` 是所有宏共用的工具层（文档提取、属性解析、命名推导等），里面定义了一些 `macro_rules!` 宏（如 `bail!`）。`#[macro_use]` 表示「`util` 里定义的宏，对它**之后**声明的所有模块都可见」。所以 `util` 必须排在最前面，`cast/elem/func/...` 才能用上 `bail!` 等宏。这也是为什么 `util` 排在模块列表的第一位。
- 七个模块对应 `src/` 下的七个 `.rs` 文件（见第 3 节的源码地图）。每个模块（除 `util` 外）实现一到两个公开宏的核心逻辑，入口函数只负责「解析 + 调用对应模块」。

**③ `BoundaryStream` 别名**

见 [lib.rs:14-15](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L14-L15)：

```rust
use proc_macro::TokenStream as BoundaryStream;
use syn::DeriveInput;
```

- `proc_macro::TokenStream` 是**编译器**提供给过程宏的 `TokenStream`，它只能在过程宏入口处出现（因为它只在编译期、由编译器实例化）。
- 作者给它起了别名 **`BoundaryStream`**（boundary = 边界）。这个名字很有画面感：它代表「过程宏与编译器之间的**边界**」。一跨过这个边界（进入 `func::func` 等模块内部），代码就会用 `.into()` 把它转成 `proc_macro2::TokenStream`——后者可以在任何地方使用、可以被 `quote!` 操作。

回看第 4.2.3 节的入口模板：`func::func(stream.into(), &item)` 里的 `.into()`，正是「把 `BoundaryStream`（边界流）转换成内部可自由使用的 `proc_macro2::TokenStream`」这一步。这就是本 crate 区分两种 `TokenStream` 的原因：**边界处用编译器的，内部全用 `proc-macro2` 的**。

#### 4.3.4 代码实践

**实践目标**：通过跟踪一次 `#[func]` 调用，体会「入口 → 模块 → 转换」的分流结构。

**操作步骤**：

1. 从 [lib.rs:104](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L104) 的 `pub fn func` 出发，看清 `stream` 和 `item` 的类型都是 `BoundaryStream`。
2. 跟踪 `func::func(stream.into(), &item)` 这一行：注意 `stream.into()` 把 `BoundaryStream`（即 `proc_macro::TokenStream`）转成了 `proc_macro2::TokenStream`。
3. （可选）用编辑器打开 `src/func.rs`，找到 `pub fn func` 的定义，看它接收的 `stream` 参数类型是不是 `proc_macro2::TokenStream`，从而验证「一进模块就换成了 proc-macro2」。
4. 再对照 [lib.rs:14](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L14)，理解 `BoundaryStream` 这个别名的「边界」含义。

**需要观察的现象 / 预期结果**：

- 入口函数签名里的类型是 `BoundaryStream`（编译器版 TokenStream）。
- 调用内部模块时，参数已经 `.into()` 成了 `proc_macro2::TokenStream`。
- 由此可以总结出本 crate 的一条重要约定：**`lib.rs` 是编译器 TokenStream 与 proc-macro2 TokenStream 的转换边界**。

> 本实践是「源码阅读型实践」，重在理解边界转换，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：如果交换 `#[macro_use] mod util;` 和 `mod cast;` 的顺序（把 `cast` 放到 `util` 前面），会发生什么？

**参考答案**：`#[macro_use]` 只让其后的模块可见其中的宏。如果把 `cast` 放在 `util` 之前，`cast.rs` 里就无法使用 `util` 定义的 `bail!` 等宏，导致编译失败。因此 `util` 必须排在所有功能模块之前。

**练习 2**：为什么入口要把 `BoundaryStream` `.into()` 成 `proc_macro2::TokenStream` 再传给子模块，而不是直接传 `BoundaryStream`？

**参考答案**：因为 `proc_macro::TokenStream`（即 `BoundaryStream`）只能在过程宏 crate 的入口上下文使用，它不能被 `quote!` 直接操作，也无法在辅助函数、测试里使用。转换成 `proc_macro2::TokenStream` 后，`syn`、`quote` 以及各种辅助函数才能自由地处理它。所以「越早转换越省事」，入口处统一 `.into()`。

## 5. 综合实践

把本讲的三个最小模块串起来，完成下面这个综合小任务：

**任务**：假设你要给一位没接触过 `typst-macros` 的同事用 5 分钟讲清楚「这个 crate 是干嘛的、怎么组织的」。请基于本讲内容，产出一份**一页纸的讲解大纲**，必须包含：

1. **一句话定位**：用「编译期 + 过程宏 + 桥接」这三个关键词，说清本 crate 的作用。（提示：见第 1、2 节）
2. **一张配置图**：画出从 `Cargo.toml` 的 `proc-macro = true`，到 `lib.rs` 的七个入口，再到六个功能模块 + 一个 `util` 的分发关系。（提示：见第 4.2.2、4.3.2 节的流程）
3. **一张分类表**：把七个宏按「属性宏 / 函数式宏 / 派生宏」三类归类，并各举一个用户写法示例。（提示：见第 2.2 节和第 4.2.3 节）

完成后，你应该能用这一页纸回答出：「`typst-macros` 为什么必须是过程宏 crate？它对外暴露了什么？内部如何分工？」这三个问题。

> 如果想进一步验证自己的理解，可以尝试向同事解释「为什么 `#[func] fn double` 能和一个生成的 `enum double {}` 共存」——能讲清这一点，说明你真正吃透了本讲。

## 6. 本讲小结

- `typst-macros` 是 Typst 的**过程宏 crate**，作用是在编译期把原生 Rust 代码「翻译并注册」成 Typst 运行时能识别的函数、类型、元素——核心是「代码生成 + 运行时 trait 契约」。
- `Cargo.toml` 里的 `[lib] proc-macro = true`（[Cargo.toml:15-16](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/Cargo.toml#L15-L16)）是本 crate 的「身份证」，四个依赖 `heck/proc-macro2/quote/syn` 分别负责起名、桥接、生成、解析。
- `src/lib.rs` 暴露**七个公开宏**：`#[func]`、`#[ty]`、`#[elem]`、`#[scope]`、`cast!`、`#[derive(Cast)]`、`#[time]`，分属属性宏（5 个）、函数式宏（1 个）、派生宏（1 个）三类。
- 七个入口共享同一套模板：`parse_macro_input!` 解析 → 调用子模块 → `.unwrap_or_else(|err| err.to_compile_error())` 把错误变编译错误，绝不 panic。
- `lib.rs` 顶部 `extern crate proc_macro;` 链接编译器内置 crate；`#[macro_use] mod util;` 让工具宏对所有后续模块可见；`BoundaryStream` 别名点明了「编译器 TokenStream 与 proc-macro2 TokenStream 的转换边界」。
- `#[func]` 能生成与函数同名的空 `enum`，是因为 Rust 中函数（值命名空间）与类型（类型命名空间）互不冲突。

## 7. 下一步学习建议

本讲只是「认路」。要真正理解这些宏，建议按手册的学习顺序继续：

1. **下一讲 `u1-l2`（过程宏工作流与错误处理）**：深入「`BoundaryStream` → `proc_macro2::TokenStream` → `syn` 解析 → `quote!` 生成」的端到端数据流，以及 `to_compile_error` 错误回传模式的细节，建议先读它，把第 4.3 节的「边界转换」吃透。
2. **再下一讲 `u1-l3`（共享工具层 util.rs）**：`util.rs` 是所有宏的地基，理解了它（文档提取、属性三件套、命名推导），后续读任何单个宏都会轻松很多。
3. **入门之后**：从最简单的 `#[time]`（`u2-l1`）入手，逐步挑战 `#[func]`、`#[elem]`。`#[elem]` 是本 crate 最复杂的宏，建议放到最后（第四单元）再啃。

推荐继续阅读的源码：先重读 [src/lib.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs) 每个宏的 `///` 文档注释（它们是最权威的用法说明），再打开 `src/util.rs` 准备进入下一讲。

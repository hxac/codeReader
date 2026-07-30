# 项目定位、依赖与整体架构

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 `typst-ide` 是什么、它在 Typst monorepo（多包仓库）中的定位，以及它与 `typst`、`typst-eval`、`typst-html`、`typst-layout` 等兄弟 crate 的依赖关系。
- 说出 `typst-ide` 对外提供的 **五大 IDE 能力**（补全、悬停、跳转定义、双向跳转、表达式分析），以及它们各自对应的公共入口函数。
- 读懂 `src/lib.rs`：理解 `mod` 模块声明如何把 8 个源文件组织成一棵模块树，理解 `pub use` 如何把内部函数「再导出」成公共 API，并能把「公共函数 → 所属文件 → 对外类型」三者对应起来。

本讲是整本学习手册的**第一篇**，不要求你已经熟悉 Typst 源码。我们会从最表层的 `Cargo.toml` 和 `lib.rs` 两个文件入手，先把「地图」画清楚，后续每一篇再深入各自的源码。

## 2. 前置知识

在进入源码之前，先用通俗的语言澄清几个名词。如果你已经熟悉，可以跳过本节。

- **Typst**：一个现代化的排版系统（类似 LaTeX 的替代品），用标记语言写文档，编译成 PDF / HTML 等输出。Typst 本身用 Rust 编写，整个仓库（`typst/typst`）是一个包含多个 crate 的 monorepo。
- **crate（包）**：Rust 的编译/发布单元。一个 monorepo 里可以有多个 crate，每个 crate 有自己的 `Cargo.toml` 和根源文件。本讲的 `typst-ide` 就是其中一个 crate。
- **IDE 能力**：编辑器里那些「智能」功能的总称，例如代码补全（打字时弹出建议）、悬停提示（鼠标停在某处显示说明）、跳转定义（跳到变量/函数的声明处）。这些功能在语言服务器协议（LSP）里有标准化定义。
- **`lib.rs`（库 crate 的根文件）**：Rust 规定一个库 crate 的根模块就是 `src/lib.rs`。它是整个 crate 对外的「门面」，所有公共 API 通常都在这里汇聚。
- **`mod` 声明**：Rust 用 `mod foo;` 把另一个文件/目录挂载为当前模块的子模块。
- **`pub use`（再导出）**：把某个内部路径的项「重新暴露」到一个更浅的路径下。例如内部函数定义在 `complete::autocomplete`，但通过 `pub use self::complete::autocomplete;` 之后，使用者只要写 `typst_ide::autocomplete` 就能调用。

如果你对 `mod` 和 `pub use` 仍不熟，记住一句话即可：**`mod` 是把文件「接进来」，`pub use` 是把接进来的东西「摆上货架」。**

## 3. 本讲源码地图

本讲只涉及两个文件，它们是理解整个 crate 的入口：

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| `Cargo.toml` | 34 | 声明 crate 名称、版本、依赖（运行时依赖 + 开发依赖）。回答「typst-ide 依赖谁、被谁依赖」。 |
| `src/lib.rs` | 54 | 库 crate 的根模块。用 `mod` 声明 8 个子模块，用 `pub use` 导出公共 API，并定义了核心 trait `IdeWorld`。回答「typst-ide 对外提供什么」。 |

其余源文件（`complete.rs`、`tooltip.rs`、`definition.rs`、`jump.rs`、`analyze.rs`、`matchers.rs`、`docs.rs`、`utils.rs`）在本讲里只按名字认识即可，后续每篇会逐一精读。

> 说明：本讲引用的行号基于当前 HEAD `146a58329a30f6cd38978c22c6bf0e430d8962a1`，随版本变化行号可能漂移，但文件结构稳定。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**模块声明**、**再导出**、**依赖关系**。三者正好对应「源码如何组织 / 如何对外暴露 / 依赖了什么」三个问题。

### 4.1 lib.rs 模块声明

#### 4.1.1 概念说明

一个 Rust 库 crate 的代码不一定全写在一个文件里。`lib.rs` 用一行 `mod foo;` 就可以把 `src/foo.rs` 这个文件挂载为名为 `foo` 的子模块。这样既能把代码按职责拆分，又能保持对外接口简洁。

`typst-ide` 把 IDE 能力按功能拆成了 8 个源文件，每个文件负责一类职责。`lib.rs` 顶部的连续 `mod` 声明，就是这棵「模块树」的总目录。

#### 4.1.2 核心流程

模块挂载的流程非常直接：

1. 编译器读到 `mod analyze;`。
2. 它去 `src/analyze.rs`（或 `src/analyze/mod.rs`）寻找文件内容。
3. 把该文件作为 `analyze` 子模块挂到 crate 根下。
4. 对每个子模块重复此过程，最终形成完整的模块树。

由于这些 `mod` 声明**没有 `pub`** 前缀，所以子模块本身对外部使用者是私有的——外部不能写 `typst_ide::complete::autocomplete`。真正对外暴露的是下面 4.2 节讲的 `pub use`。这是一种常见的封装手法：**实现藏在私有模块里，只把挑好的公共 API 摆到货架上。**

#### 4.1.3 源码精读

`lib.rs` 第 1 行是整个 crate 的文档注释（`//!` 表示对当前模块的说明）：

```rust
//! Capabilities for Typst IDE support.
```

这句「为 Typst 提供 IDE 支持」就是整个 crate 的一句话定位。

接着是 8 行模块声明：

[文件路径:L3-L10](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/lib.rs#L3-L10)

```rust
mod analyze;
mod complete;
mod definition;
mod docs;
mod jump;
mod matchers;
mod tooltip;
mod utils;
```

按职责把 8 个子模块归类如下：

| 子模块 | 文件 | 主要职责 |
| --- | --- | --- |
| `analyze` | `analyze.rs` (142 行) | 推断表达式可能的值（字面量求值 / trace 追踪）、解析 import、收集标签 |
| `complete` | `complete.rs` (2075 行) | 自动补全引擎，是整个 crate 最大的文件 |
| `definition` | `definition.rs` (202 行) | 跳转定义 |
| `docs` | `docs.rs` (171 行) | 从原生文档或源码注释中提取文档（内部使用，不对外导出函数） |
| `jump` | `jump.rs` (809 行) | 源码 ↔ 渲染结果的双向跳转 |
| `matchers` | `matchers.rs` (386 行) | 共享的语法树匹配：`deref_target`（表达式归类）、`named_items`（作用域收集） |
| `tooltip` | `tooltip.rs` (548 行) | 悬停提示 |
| `utils` | `utils.rs` (287 行) | 共享工具：临时 Engine、标准库作用域、字体摘要等（内部使用） |

最后，第 53-54 行还声明了一个**仅测试时**才编译的模块：

[文件路径:L53-L54](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/lib.rs#L53-L54)

```rust
#[cfg(test)]
mod tests;
```

`#[cfg(test)]` 表示「只有跑测试时才编译它」，正式构建会被剔除。这个 `src/tests.rs` 提供了 `TestWorld` 等测试基础设施，是第 u1-l3 讲的主题。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是让你亲手核对上面的模块归类表。

1. **实践目标**：确认 `lib.rs` 里声明的每个 `mod` 都真实对应一个 `src/*.rs` 文件，并核对各文件行数。
2. **操作步骤**：
   - 在仓库根目录执行 `ls crates/typst-ide/src/*.rs`，列出所有源文件。
   - 执行 `wc -l crates/typst-ide/src/*.rs`，查看每个文件的行数。
3. **需要观察的现象**：列出的 9 个文件（8 个子模块 + `lib.rs` 自身）应当与 `mod` 声明一一对应；`complete.rs` 应当是最大的（约 2000+ 行），`jump.rs` 次之。
4. **预期结果**：文件名集合为 `{analyze, complete, definition, docs, jump, matchers, tooltip, utils, lib, tests}`，与 4.1.3 的表格吻合。`tests.rs` 对应 `#[cfg(test)] mod tests;`。
5. **若无法本地运行**：可改为直接在 GitHub 上浏览 `crates/typst-ide/src/` 目录核对。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `mod complete;` 不加 `pub`？如果不小心写成 `pub mod complete;`，对外使用者的体验会有什么变化？

> **参考答案**：不加 `pub` 意味着 `complete` 子模块对外不可见，使用者只能通过 `lib.rs` 里 `pub use` 挑选出的公共 API（如 `typst_ide::autocomplete`）访问。这样可以把实现细节（如 `complete.rs` 里大量的内部辅助函数）藏起来，保持公共接口简洁稳定。若改成 `pub mod`，使用者就能直接访问 `typst_ide::complete::` 下的所有 `pub` 项，封装就被破坏了。

**练习 2**：`#[cfg(test)] mod tests;` 这一行在 `cargo build`（非测试构建）时会带来什么影响？

> **参考答案**：`#[cfg(test)]` 使该模块仅在 `cargo test` 时编译。在普通的 `cargo build` / `cargo build --release` 中，编译器完全忽略它，`tests.rs` 不会被编译进产物，因此不会增大发布体积，也不影响正式功能。

### 4.2 pub use 导出

#### 4.2.1 概念说明

`pub use` 叫做**再导出（re-export）**。一个项原本定义在很深的内部路径里（例如 `crate::complete::autocomplete`），通过 `pub use self::complete::autocomplete;` 之后，它就被「摆」到了 crate 根这一层，使用者只需写 `typst_ide::autocomplete` 即可。

`typst-ide` 对外暴露的就是一组「IDE 能力函数」和它们返回的「结果类型」。把这些函数按功能归组，正好就是本 crate 的**五大 IDE 能力**：

1. **自动补全（completion）**：`autocomplete`
2. **悬停提示（hover/tooltip）**：`tooltip`
3. **跳转定义（go-to-definition）**：`definition`
4. **双向跳转（jump）**：源码→渲染、渲染→源码，由 `jump_from_click` / `jump_from_cursor` 等承担
5. **表达式分析（analyze）**：`analyze_expr` / `analyze_import` / `analyze_labels`

此外还有一组**共享的匹配工具**（`deref_target`、`named_items`），它们虽不属于「五大能力」之一，但被补全、悬停、定义三者共同依赖，因此也对外导出，供高级使用者复用。

#### 4.2.2 核心流程

公共 API 的暴露流程：

1. 函数在某个私有子模块（如 `complete.rs`）里被定义为 `pub fn`。
2. `lib.rs` 用一行 `pub use self::complete::{Completion, CompletionKind, autocomplete};` 把它「提到」crate 根。
3. 外部 crate 在 `Cargo.toml` 里加上 `typst-ide = "…"` 依赖后，就能直接用 `typst_ide::autocomplete(...)` 调用。

值得注意的是：并非所有 `pub fn` 都被再导出。例如 `analyze.rs` 里有 `pub fn analyze_expr_with_fallback`，但它**没有**出现在 `lib.rs` 的 `pub use` 列表里——又因为 `analyze` 是私有模块，外部根本访问不到它。这说明 `typst-ide` 有意识地只暴露经过挑选的稳定 API，其余的留作内部实现。

#### 4.2.3 源码精读

导出集中在 `lib.rs` 的 6 行：

[文件路径:L12-L17](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/lib.rs#L12-L17)

```rust
pub use self::analyze::{analyze_expr, analyze_import, analyze_labels};
pub use self::complete::{Completion, CompletionKind, autocomplete};
pub use self::definition::{Definition, definition};
pub use self::jump::{Jump, jump_from_click, jump_from_click_in_frame, jump_from_cursor};
pub use self::matchers::{DerefTarget, NamedItem, deref_target, named_items};
pub use self::tooltip::{Tooltip, tooltip};
```

每一行都是「从某个子模块里挑出若干项，摆到 crate 根」。把每行的导出项与其签名对应起来（签名来自各源文件，行号即定义处）：

| 公共项 | 类型 | 所属文件 | 签名要点（输入 → 返回） |
| --- | --- | --- | --- |
| `autocomplete` | 函数 | [complete.rs:L37](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L37) | `(world, output: Option<_>, source, cursor, explicit: bool)` → `Option<(usize, Vec<Completion>)>` |
| `Completion` | 结构体 | [complete.rs:L74](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L74) | 含 `kind` / `label` / `apply` / `detail` 四个字段 |
| `CompletionKind` | 枚举 | [complete.rs:L91](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L91) | `Syntax` / `Func` / `Type` / `Param` / `Constant` … |
| `tooltip` | 函数 | [tooltip.rs:L24](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L24) | `(world, output: Option<_>, source, cursor, side: Side)` → `Option<Tooltip>` |
| `Tooltip` | 枚举 | [tooltip.rs:L46](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L46) | `Text(EcoString)` / `Code(EcoString)` |
| `definition` | 函数 | [definition.rs:L27](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs#L27) | `(world, output: Option<_>, source, cursor, side: Side)` → `Option<Definition>` |
| `Definition` | 枚举 | [definition.rs:L13](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs#L13) | `Span(Span)` / `File(FileId)` / `Std(Value)` |
| `jump_from_click` | 泛型函数 | [jump.rs:L34](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L34) | `(world, document: &D, position: &D::Position)` → `Option<Jump>` |
| `jump_from_click_in_frame` | 函数 | [jump.rs:L209](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L209) | `(world, output, frame: &Frame, click: Point)` → `Option<Jump>` |
| `jump_from_cursor` | 泛型函数 | [jump.rs:L343](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L343) | `(document: &D, source, cursor)` → `Vec<D::Position>` |
| `Jump` | 枚举 | [jump.rs:L16](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L16) | `File(FileId, usize)` / `Url(Url)` / `Position(PagedPosition)` |
| `analyze_expr` | 函数 | [analyze.rs:L12](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L12) | `(world, node: &LinkedNode)` → `EcoVec<(Value, Option<Styles>)>` |
| `analyze_import` | 函数 | [analyze.rs:L80](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L80) | `(world, source: &LinkedNode)` → `Option<Value>` |
| `analyze_labels` | 函数 | [analyze.rs:L104](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L104) | `(output: impl AsOutput)` → `(Vec<(Label, Option<EcoString>)>, usize)` |
| `deref_target` | 函数 | [matchers.rs:L219](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/matchers.rs#L219) | `(node: LinkedNode)` → `Option<DerefTarget>` |
| `named_items` | 泛型函数 | [matchers.rs:L9](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/matchers.rs#L9) | `(world, position, recv: impl FnMut(NamedItem)->Option<T>)` → `Option<T>` |
| `DerefTarget` | 枚举 | [matchers.rs:L264](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/matchers.rs#L264) | `VarAccess` / `Callee` / `ImportPath` / … |
| `NamedItem` | 枚举 | [matchers.rs:L179](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/matchers.rs#L179) | `Var` / `Fn` / `Module` / `Import` |

从上表可以总结出公共 API 的两个共同特征：

- **几乎都以 `world: &dyn IdeWorld` 作为第一个参数**：它是所有 IDE 功能的数据来源（详见 u1-l2 讲）。
- **很多函数带一个 `output: Option<impl AsOutput>` 参数**：`output` 是上一次编译的产物（文档）。它是**可选的**——不传也能工作，只是功能会降级（例如「跳转引用定义」需要它才能定位标签）。`AsOutput` 是 typst-library 里定义的 trait（`fn as_output(&self) -> &dyn Output;`），让函数能同时接受 `PagedDocument`（PDF 后端）和 `HtmlDocument`（HTML 后端）。

#### 4.2.4 代码实践

这正是本讲的主实践任务：**亲手绘制「公共 API → 所属文件 → 对外类型」对照表**。

1. **实践目标**：在上表的基础上，自己重新核对一遍，加深对「函数 / 它返回的类型 / 它住在哪个文件」三者映射的记忆。
2. **操作步骤**：
   - 打开 [src/lib.rs:L12-L17](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/lib.rs#L12-L17)，列出 6 行 `pub use` 里被导出的全部项（函数与类型）。
   - 对每个**函数**，跳转到它的定义文件，抄下完整的 `pub fn` 签名（参数列表 + 返回类型）。
   - 在一张表里填入三列：函数名 / 所属文件 / 对外类型（返回类型及其关键变体）。
3. **需要观察的现象**：注意 `tooltip`、`definition`、`autocomplete` 三个函数的参数列表高度相似（都含 `world`、`output`、`source`、`cursor`），而 `jump_*` 系列的参数差异较大。
4. **预期结果**：得到一张与本讲 4.2.3 表格相近的对照表，并能口述「补全返回 `(from, Vec<Completion>)`、悬停返回 `Tooltip`、定义返回 `Definition`、跳转返回 `Jump`」。
5. **待本地验证**：若你想进一步验证签名，可在仓库根目录运行 `cargo doc --no-deps -p typst --no-deps -p typst-ide`（命令较长，建议待本地验证），用浏览器打开生成的文档核对每个公共函数的签名。

#### 4.2.5 小练习与答案

**练习 1**：`autocomplete` 的返回类型是 `Option<(usize, Vec<Completion>)>`。请推测：这里的 `usize` 和 `Vec<Completion>` 分别可能表示什么？为什么整体要包一层 `Option`？

> **参考答案**：`Vec<Completion>` 是补全候选列表；`usize` 通常是「补全起始偏移」（即从源码的哪个位置开始替换，编辑器据此决定补全窗口）。外层 `Option` 表示「这个位置是否需要补全」——当光标处无可补全内容（例如在注释中间）时返回 `None`，函数本身不做任何处理。

**练习 2**：`analyze_expr_with_fallback` 在 `analyze.rs` 里是 `pub fn`，但不在 `lib.rs` 的 `pub use` 列表中。外部使用者能调用它吗？为什么？

> **参考答案**：不能。因为 `mod analyze;` 没有 `pub`，`analyze` 模块对外私有。即使函数本身是 `pub`，它也只对 crate 内部可见。没有 `pub use` 把它「提」到 crate 根，外部就无法访问。这体现了 `typst-ide` 只暴露稳定子集的设计。

**练习 3**：`Definition` 枚举有 `Span` / `File` / `Std` 三个变体，请结合日常 IDE 使用经验，分别举一个会触发它们的场景。

> **参考答案**：
> - `Span(Span)`：跳转到**当前文件或项目内**某变量/函数的声明（例如跳到 `#let x = 1` 的位置）。
> - `File(FileId)`：跳转到一个**被 import/include 的整个文件**（例如在 `#import "other.typ"` 的路径上跳转）。
> - `Std(Value)`：跳转到**标准库**里的定义（例如在 `#table` 上跳转，它来自 Typst 内置函数）。

### 4.3 Cargo.toml 依赖

#### 4.3.1 概念说明

`Cargo.toml` 是 Rust crate 的「身份证 + 物料清单」。本节我们关心两件事：

1. 这个 crate **依赖哪些别的 crate**（`[dependencies]`）——这决定了它能调用什么。
2. 哪些依赖**只在测试时需要**（`[dev-dependencies]`）——这些不会进入正式产物。

理解依赖，就理解了 `typst-ide` 与 Typst 其他组件的关系。

#### 4.3.2 核心流程

Cargo 的依赖解析流程：

1. 读取 `Cargo.toml` 的 `[dependencies]`，把列出的 crate 作为运行时依赖。
2. `version = { workspace = true }` 表示「版本号跟随工作区根 `Cargo.toml` 的统一设定」，这样 monorepo 内多个 crate 能保持版本一致。
3. `[dev-dependencies]` 里的依赖只在 `cargo test` / `cargo bench` 时生效，正式 `cargo build` 不包含。
4. 编译时，Cargo 会把这些依赖的 crate 一起拉进来，供 `typst-ide` 的代码 `use`。

需要特别说明的是：经核对，typst 工作区里**没有任何内部 crate 依赖 `typst-ide`**。也就是说 `typst-ide` 是一棵「叶子」库——它依赖别的 typst crate，但没有 typst crate 依赖它。它的真正消费者是**外部的语言服务器**（如 `tinymist`、`typst-lsp` 等编辑器后端）。

#### 4.3.3 源码精读

运行时依赖（正式产物需要）：

[文件路径:L15-L26](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/Cargo.toml#L15-L26)

```toml
[dependencies]
typst = { workspace = true }
typst-eval = { workspace = true }
typst-html = { workspace = true }
typst-layout = { workspace = true }
typst-utils = { workspace = true }
comemo = { workspace = true }
ecow = { workspace = true }
indexmap = { workspace = true }
rustc-hash = { workspace = true }
serde = { workspace = true }
unscanny = { workspace = true }
```

可以把这些依赖分成两组来理解：

**（a）Typst 内部 crate**——IDE 能力的「燃料」：

| 依赖 | 在 typst-ide 里的用途 |
| --- | --- |
| `typst` | 核心：提供 `World` trait、语法树（`Source` / `LinkedNode` / `Span`）、`Value`、`Library` 等基础类型 |
| `typst-eval` | 执行 import、求值表达式（`analyze_import` / `analyze_expr` 真正跑求值时需要） |
| `typst-html` | 支持跳转到 HTML 后端的渲染结果（`HtmlDocument`） |
| `typst-layout` | 支持跳转到分页后端的渲染结果（`Frame` / `PagedPosition`） |
| `typst-utils` | 通用小工具 |

可以看到，**补全/悬停/定义** 主要靠 `typst` + `typst-eval`；而**双向跳转 jump** 因为要落到渲染产物上，所以额外需要 `typst-html` 和 `typst-layout`。

**（b）第三方通用 crate**——基础设施：

| 依赖 | 用途 |
| --- | --- |
| `comemo` | Typst 自研的记忆化（memoization）库，用于缓存求值结果；`with_engine` 里 `.track()` 就用到它 |
| `ecow` | 经济的写时复制字符串（`EcoString`），几乎每个返回类型里都有它 |
| `indexmap` | 保序的哈希表（插入顺序保持） |
| `rustc-hash` | 高性能哈希（`FxHashSet` 等） |
| `serde` | 序列化（公共类型可被序列化，方便跨进程 LSP） |
| `unscanny` | 轻量字符串扫描，用于解析光标、提取文本 |

开发依赖（仅测试时）：

[文件路径:L28-L31](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/Cargo.toml#L28-L31)

```toml
[dev-dependencies]
typst-assets = { workspace = true, features = ["fonts"] }
typst-dev-assets = { workspace = true }
once_cell = { workspace = true }
```

- `typst-assets`（开启 `fonts` 特性）：提供内置字体，测试补全/悬停时需要真实字体。
- `typst-dev-assets`：提供测试用的开发资源。
- `once_cell`：惰性初始化，`tests.rs` 里共享的 `TestBase` 用它来一次性加载 library / book / fonts。

#### 4.3.4 代码实践

1. **实践目标**：把每个运行时依赖与「它支撑的 IDE 能力」对应起来。
2. **操作步骤**：
   - 列出 `[dependencies]` 的 11 个 crate。
   - 对 `typst`、`typst-eval`、`typst-html`、`typst-layout` 四个内部依赖，分别说出「如果没有它，哪个 IDE 能力会失效」。
3. **需要观察的现象**：你会发现 `typst-html` 与 `typst-layout` 几乎只为 `jump.rs` 服务；如果删掉双向跳转功能，这两个依赖也就不再需要。
4. **预期结果**：能口述「`typst` 是地基（语法树+值），`typst-eval` 让 analyze 能真正求值，`typst-html`/`typst-layout` 让 jump 能落到渲染产物上」。
5. **待本地验证**：想进一步确认某个第三方 crate 的用法，可在 `src/` 下搜索对应的 `use` 语句（例如 `use ecow::` 看哪里用了 `EcoString`）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `typst-assets` 放在 `[dev-dependencies]` 而不是 `[dependencies]`？

> **参考答案**：`typst-assets` 提供的是测试用的内置字体资源。正式使用 `typst-ide` 的语言服务器，其字体会由使用者的 `World` 实现自行提供，不需要 typst-ide 自带字体。把它放在开发依赖里，可以避免把字体资源打进正式产物、增大体积，只在跑测试时才拉取。

**练习 2**：`version = { workspace = true }` 是什么意思？这样做有什么好处？

> **参考答案**：表示该依赖的版本号不在这里写死，而是「继承」工作区根 `Cargo.toml`（`[workspace.dependencies]`）里的统一设定。好处是：整个 monorepo 里所有 crate 引用同一个依赖时版本完全一致，避免版本冲突；升级时只需改根 `Cargo.toml` 一处，所有 crate 同步。

## 5. 综合实践

把本讲的三个模块串起来，完成下面这个**总览任务**，它也是后续每一篇讲义的「导航地图」。

**任务**：为 `typst-ide` 画一张「能力架构图」。

1. 在纸或文本编辑器里画三层结构：
   - **最上层（公共 API）**：列出 `lib.rs` `pub use` 导出的 5 个能力入口函数 `autocomplete` / `tooltip` / `definition` / `jump_from_click`（+ `jump_from_cursor`）/ `analyze_expr`。
   - **中间层（模块）**：把它们连线到各自的源文件（`complete.rs` / `tooltip.rs` / `definition.rs` / `jump.rs` / `analyze.rs`）。
   - **最下层（依赖）**：从这些文件再连线到关键依赖（`typst`、`typst-eval`、`typst-html`、`typst-layout`）。
2. 在图中标出两个共享模块的位置：`matchers.rs`（被补全/悬停/定义共用）、`docs.rs` 与 `utils.rs`（内部辅助，未导出）。
3. 在每个公共函数旁注上它的**返回类型**（`Completion` / `Tooltip` / `Definition` / `Jump` / `Value…`）。

**预期结果**：得到一张能让你「一眼看清 typst-ide 全貌」的图。后续每读一篇讲义，你都可以回到这张图，定位当前讲义在整体中的位置。完成这张图后，建议保存它，作为整本学习手册的导航。

> 提示：如果你暂时无法画图，用 4.2.3 的对照表加上 4.3.3 的依赖表，文字描述出这三层关系同样达标。

## 6. 本讲小结

- `typst-ide` 是 Typst monorepo 中提供 **IDE 能力**的库 crate，定位是为外部语言服务器（而非 typst 自身）服务——它是工作区里的「叶子」crate，没有内部 crate 依赖它。
- `src/lib.rs` 是门面：用 8 行 `mod` 把实现拆进私有子模块，用 6 行 `pub use` 把挑选好的公共 API 摆上货架，实现「实现私有、接口精简」。
- 五大 IDE 能力与入口函数一一对应：补全 `autocomplete`、悬停 `tooltip`、定义 `definition`、跳转 `jump_from_click` / `jump_from_cursor`、分析 `analyze_expr` 等。
- 公共 API 有两个共同特征：首个参数几乎都是 `world: &dyn IdeWorld`（数据来源）；很多带一个可选的 `output: Option<impl AsOutput>`（上一次编译产物，缺失则降级）。
- 依赖关系揭示分工：`typst` + `typst-eval` 支撑补全/悬停/定义/分析；`typst-html` + `typst-layout` 专门支撑双向跳转 jump。
- `docs.rs`、`utils.rs` 虽无顶层导出，却是被各能力模块共用的内部辅助，不可忽视。

## 7. 下一步学习建议

本讲只看了「门面」，还没有进入任何一个能力的内部。建议按以下顺序继续：

1. **下一讲 u1-l2《IdeWorld —— IDE 功能的数据契约》**：弄清所有公共函数第一个参数 `world: &dyn IdeWorld` 到底是什么、为什么需要 `upcast`、`packages()` / `files()` 为何是可选增强。这是理解所有 IDE 功能的前提。
2. **随后 u1-l3《运行、构建与测试基础设施》**：学会用 `TestWorld` 构造测试场景，这样后续每篇讲义的实践都能真正跑起来。
3. 之后再按大纲进入第 2 单元「语法树定位与表达式分析基石」，从底层匹配工具（`deref_target` / `named_items`）开始逐层向上。

如果你急于看到「真实功能」，也可以先跳到第 3 单元看 `tooltip` 的悬停提示实现，但建议至少先读完 u1-l2，否则公共参数的含义会卡住你。

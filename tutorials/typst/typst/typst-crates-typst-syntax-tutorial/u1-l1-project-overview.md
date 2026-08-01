# 项目定位与核心概念（u1-l1）

> 适用对象：第一次接触 `typst-syntax` 的读者。本讲不要求你已经读过任何 Typst 源码。

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 `typst-syntax` 在整个 Typst 编译流水线里扮演什么角色、解决了什么问题。
- 区分 **具体语法树（CST）** 与 **抽象语法树（AST）**，并知道它们各自用在什么场合。
- 认识 `SyntaxMode` 枚举的三种模式：`Markup`、`Math`、`Code`，并能给一段 Typst 文本判断它属于哪种模式。
- 读懂 `lib.rs` 的模块声明（`mod` / `pub mod`）与公共导出（`pub use`），知道哪些类型被暴露给外部使用者。

本讲是整本学习手册的「第 0 公里」。我们刻意只读两个文件（`README.md` 和 `src/lib.rs`），目的是先建立全局认知，再在后续讲义里逐层下钻。

## 2. 前置知识

在开始之前，最好对以下概念有一点直觉（没有也没关系，我们会顺带解释）：

- **Typst 是什么**：Typst 是一个现代化的排版系统，你写一段类似 Markdown 的文本（Typst 源码），它帮你排版成 PDF 等输出。你可以把它粗略理解成「更好的 LaTeX」。
- **编译器前端 / 后端**：一个编译器通常分成「前端」和「后端」。前端负责把人类写的文本翻译成机器能理解的结构（语法树），后端负责把结构转化成最终产物（排版结果）。`typst-syntax` 属于**前端**。
- **Rust 基础语法**：本讲会看到 `enum`、`pub mod`、`pub use` 这样的 Rust 写法。你只要知道 `mod` 用来声明一个模块，`pub` 表示「公开可见」即可。

关键术语先打个预防针：

| 术语 | 一句话解释 |
|------|-----------|
| Token（词法单元） | 词法分析把文本切成的一个个小单元，比如一个关键字、一个数字、一段空白。 |
| 语法树 | 用树状结构表达「文本是怎么由小单元组合起来的」。 |
| CST | Concrete Syntax Tree，具体语法树，**保留所有细节**。 |
| AST | Abstract Syntax Tree，抽象语法树，**只保留语义相关的信息**。 |
| Span（源码区间） | 给语法树上每个节点贴的一个编号，用来反查「这段结果来自源码的哪个位置」。 |

## 3. 本讲源码地图

本讲只涉及两个文件：

| 文件 | 作用 | 本讲怎么看 |
|------|------|-----------|
| `README.md` | 用一段话讲清本 crate 的定位、列出各源码文件的职责、点明设计参考来源。 | 用来建立「这个 crate 到底干什么」的全局印象。 |
| `src/lib.rs` | crate 的根模块文件：声明所有子模块、用 `pub use` 把对外的类型/函数集中暴露、并定义 `SyntaxMode` 枚举。 | 用来理解「模块如何组织、外部能用到哪些东西」。 |

## 4. 核心概念与源码讲解

### 4.1 typst-syntax 的定位：编译前端的语法核心

#### 4.1.1 概念说明

当你在 Typst 里写下这样一段文字时：

```typst
= 我的标题
这是一段正文，里面有 #bold[加粗] 和 $a^2 + b^2 = c^2$。
```

计算机并不能直接理解它。Typst 必须先把这段纯文本「拆解」成一个结构化的、机器友好的表示，后续的求值（evaluation）与排版（layout）才能在此基础上进行。

`typst-syntax` 就是负责「拆解」的这一层。它**不**负责把文档排成 PDF，那是后续 crate 的工作；它只负责把文本变成一棵语法树，并附带上「定位信息」和「语法高亮信息」等基础设施。

#### 4.1.2 核心流程

从 Typst 源码文本到可用的语法结构，`typst-syntax` 内部大致经历这样一条链路：

```text
源码文本（字符串）
      │  词法分析（lexer）：切成 token
      ▼
   token 流
      │  语法分析（parser）：拼装成树
      ▼
具体语法树 CST（SyntaxNode 嵌套）
      │  按需转换（ast.rs）
      ▼
抽象语法树 AST（类型化视图，供求值使用）
```

在这条主链路之外，还有几个「辅助系统」平行存在：

- **Span 系统**：给每个节点编号，便于反查源码位置（用于报错、跳转）。
- **Source / Lines**：把「文本 + 语法树 + 行列索引」打包成一个不可变对象。
- **文件身份（path）**：把项目里的文件路径压缩成 16 位的唯一 ID。
- **增量重解析（reparser）**：当你只改了一个字时，尽量只重新解析受影响的那一小段。
- **语法高亮（highlight）**：把语法树翻译成编辑器/HTML 能用的着色信息。

#### 4.1.3 源码精读

README 的开头一句话点明了本 crate 的定位：

[README.md:3-7](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/README.md#L3-L7) —— 这段话列出了 `typst-syntax` 管理的若干核心抽象：分配源码文件 id、解析 Typst 语法、创建 AST、初始化 span、语法高亮。**这正是本 crate 的「职责清单」。**

`lib.rs` 第一行的文档注释则用一句话概括了整个 crate：

[src/lib.rs:1](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lib.rs#L1) —— `//! Parser and syntax tree for Typst.`，即「Typst 的解析器与语法树」。

> 注意：本讲只讨论 `typst-syntax` 内部。它产出的 AST 会被下游 crate（如求值、排版相关）消费，但那些不在本讲范围内，我们点到即止。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**（本讲不要求运行代码）：

1. **目标**：用自己的话提炼出 `typst-syntax` 的核心职责。
2. **步骤**：
   - 打开 [README.md:3-7](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/README.md#L3-L7)。
   - 把这一段里提到的每一个动词/动作（例如 "assigning source file ids"、"parsing Typst syntax"）单独列出来。
3. **观察现象**：你会得到一份英文清单。
4. **预期结果**：你应该能列出 **5 项**核心职责（分配文件 id、解析语法、构建 AST、初始化 span、语法高亮）。这正是本讲综合实践（第 5 节）要你做的事。

#### 4.1.5 小练习与答案

**练习 1**：`typst-syntax` 会负责把文档排版成 PDF 吗？

> **答案**：不会。排版是下游 crate 的职责。`typst-syntax` 只负责把文本解析成语法树及相关基础设施。

**练习 2**：下面哪一项**不是** `typst-syntax` 的职责？（A）词法分析（B）构建 AST（C）分配文件 id（D）渲染像素到屏幕。

> **答案**：D。渲染像素与 `typst-syntax` 无关。

---

### 4.2 CST 与 AST 的区别

#### 4.2.1 概念说明

`typst-syntax` 同时维护两棵「树」，这是它最容易让初学者困惑的点。先把结论摆出来：

- **CST（Concrete Syntax Tree，具体语法树）**：像一张**照片**，保留源码里的所有细节——包括空白、注释、标点，甚至错误的 token。它是「无损」的。
- **AST（Abstract Syntax Tree，抽象语法树）**：像一张**素描**，只保留与语义有关的结构，丢弃空白/注释等无关紧要的细节。它是 CST 之上的「类型化视图」。

为什么要分两层？

- CST 的「无损」特性对**工具**非常重要：语法高亮、增量重解析、代码补全、报错定位都需要知道每一个字符的位置，连空格和注释都不能丢。
- AST 的「类型化」特性对**求值**非常方便：下游代码只想知道「这是一个 `if` 表达式，条件是 X，then 分支是 Y」，不想关心 `if` 两边的空格长什么样。

#### 4.2.2 核心流程

两棵树的协作关系：

```text
              parser.rs 产出                  ast.rs 转换
源码文本 ───────────────────▶ CST ──────────────────▶ AST
                              │
                              ├── highlight.rs 读它 → 高亮
                              ├── reparser.rs 读它 → 增量重解析
                              └── 工具/IDE 读它 → 补全、诊断
```

也就是说，**CST 是「唯一真相来源」**，AST 只是按需在它之上架起的一层「便于求值的视图」。

#### 4.2.3 源码精读

README 对这两个文件是这样描述的：

[README.md:14-19](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/README.md#L14-L19) —— `parser.rs` 产出一个由 `SyntaxNode` 嵌套向量组成的 **Concrete Syntax Tree**；`ast.rs` 则是 CST 与用于求值的 **Abstract Syntax Tree** 之间的转换层。

关键点：
- 「Concrete Syntax Tree made of nested vectors of `SyntaxNode`s」告诉我们 CST 的载体是 `SyntaxNode`（这个类型会在 U5 详讲）。
- 「conversion layer」告诉我们 AST **不是**另一份独立解析的产物，而是从 CST **转换**来的。

此外，README 还说明了设计的参考来源：

[README.md:31-36](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/README.md#L31-L36) —— 解析器结构大量借鉴自 Rust Analyzer；增量重解析算法来自某篇论文（第 4 节）。这两点说明了为什么本 crate 里能看到很多「rust-analyzer 风格」的设计（marker、事件式解析等，后续讲义会展开）。

#### 4.2.4 代码实践

1. **目标**：把 CST 与 AST 的职责对号入座。
2. **步骤**：
   - 阅读 [README.md:12-29](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/README.md#L12-L29) 的文件清单。
   - 找出哪些文件主要「消费/产出 CST」，哪些「主要服务 AST」。
3. **观察现象**：你会注意到 `parser.rs`、`node.rs`、`highlight.rs`、`reparser.rs` 都围绕 CST；`ast.rs` 则站在它们之上。
4. **预期结果**：能用一句话回答「CST 由谁产出？AST 由谁从什么转换而来？」。

#### 4.2.5 小练习与答案

**练习 1**：如果你要实现一个「保留每一行注释」的代码格式化工具，你会优先基于 CST 还是 AST？为什么？

> **答案**：CST。因为 CST 是无损的，保留了注释与空白；AST 会丢弃这些。

**练习 2**：为什么 Typst 不直接只用 AST，省去 CST？

> **答案**：因为高亮、增量重解析、IDE 诊断等都需要精确到每个字符的位置信息（含空白/注释/错误 token），这些只有「无损」的 CST 才能提供。

---

### 4.3 SyntaxMode 枚举：三种语法模式

> 这是本讲要求掌握的最小模块之一。

#### 4.3.1 概念说明

同样是字符 `-`，在 Typst 里却有完全不同的含义：

- 在普通正文里，行首的 `-` 是**列表标记**（list item）。
- 在数学公式里，`-` 是**减号**。
- 在代码（`#` 之后）里，`-` 是**减法运算符**。

同一个字符，含义随「上下文」而变。`typst-syntax` 用 `SyntaxMode` 枚举来显式表达这种上下文。它有三个取值：

| 变体 | 含义 | 典型场景 |
|------|------|---------|
| `Markup` | 文本与标记 | 文档正文，最顶层默认模式 |
| `Math` | 数学原子与运算符 | 公式 `$...$` 内部 |
| `Code` | 关键字、字面量与运算符 | `#` 之后、代码块 `{...}` 内部 |

#### 4.3.2 核心流程

一段 Typst 文档会在三种模式之间**切换**：

```text
#let x = 1        ← '#' 之后是 Code 模式
= 标题            ← 顶层正文是 Markup 模式
$a^2 + b^2$       ← '$' 之间是 Math 模式
```

模式决定了解析规则。词法分析器（lexer，U3 会详讲）拿到一个字符后，会先看「当前在哪种模式」，再决定产出什么 token。模式切换的边界由特定字符触发：`#` 进入 Code、`$` 进入/退出 Math、`[` `]` 包裹 content 等。

> 提示：`SyntaxMode` 是一个很小的「枚举」，但它贯穿整个 crate。后续讲义（U3 词法、U4 语法）会反复用到它。本讲只要建立直觉即可。

#### 4.3.3 源码精读

`SyntaxMode` 就定义在 `lib.rs` 里：

[src/lib.rs:41-50](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lib.rs#L41-L50) —— `pub enum SyntaxMode`，三个变体 `Markup` / `Math` / `Code`，每个变体上都有文档注释说明它代表什么。

读这段代码时注意几个 Rust 细节：

- `#[derive(Debug, Copy, Clone, Eq, PartialEq, Hash)]`：表示这个枚举可以拷贝（`Copy`）、可以比较相等（`PartialEq`/`Eq`）、可以做哈希表键（`Hash`）。因为它只有三个取值、没有附加数据，所以用值拷贝非常廉价。
- `pub enum`：`pub` 表示对外可见。结合 `lib.rs` 没有用 `pub use` 单独重新导出它，它是通过所在的 `lib.rs` 根模块直接以 `typst_syntax::SyntaxMode` 暴露的。
- 三个变体的文档注释分别给出了「何时使用」的线索：Markup 对应顶层正文，Math 对应公式，Code 对应 `#` 之后。

#### 4.3.4 代码实践

1. **目标**：能对真实 Typst 片段判断其所属模式。
2. **步骤**：对下面每段文本，标注「哪一部分」分别处于 `Markup` / `Math` / `Code`：
   - `Hello, world`
   - `#let name = "Typst"`
   - `面积是 $pi * r^2$。`
3. **观察现象**：同一段文本里可能同时包含多种模式。
4. **预期结果**（**待本地验证**，因为这是你基于规则的判断）：
   - `Hello, world`：整段 `Markup`。
   - `#let name = "Typst"`：`let name = "Typst"` 部分（`#` 之后）是 `Code`。
   - `面积是 $pi * r^2$。`：文字「面积是」「。」是 `Markup`，`pi * r^2` 是 `Math`。

#### 4.3.5 小练习与答案

**练习 1**：`SyntaxMode` 有哪三个变体？分别对应什么场景？

> **答案**：`Markup`（正文/标记）、`Math`（公式）、`Code`（`#` 之后的代码）。

**练习 2**：为什么 `SyntaxMode` 需要派生 `Copy`？

> **答案**：它是一个只有三个取值、无附加数据的小枚举，按值复制开销极小；派生 `Copy` 后可以在函数间随意传递而不必借用或克隆。

**练习 3**：字符 `-` 在 Markup 与 Code 模式下分别可能是什么？

> **答案**：Markup 模式下行首的 `-` 通常是列表标记；Code 模式下 `-` 是减法/负号运算符。

---

### 4.4 lib.rs 的模块声明与公共导出

> 这是本讲要求掌握的最小模块之二，也是理解整个 crate「骨架」的关键。

#### 4.4.1 概念说明

一个 Rust crate 的 `lib.rs` 是它的「门面」。`typst-syntax` 的 `lib.rs` 做三件事：

1. **声明子模块**：用 `mod xxx;` 告诉编译器「这个 crate 由哪些文件组成」。
2. **选择性公开**：`pub mod` 表示整个模块对外可见；不带 `pub` 的 `mod` 表示模块私有，外部只能看到我们「主动放出」的东西。
3. **集中导出（re-export）**：用 `pub use xxx::YYY;` 把分散在各私有模块里的重要类型/函数，统一「摆到」crate 根，方便外部以 `typst_syntax::YYY` 这样的短路径使用。

理解这套机制后，你拿到任何一个新 crate，读它的 `lib.rs` 就能迅速知道「它对外提供什么」。

#### 4.4.2 核心流程

模块可见性与导出的协作：

```text
私有模块（mod xxx;）          公共导出（pub use）           外部使用者
─────────────────────         ────────────────────         ──────────
mod node;      ─┐
mod parser;     │  只把「挑出来」的类型        ──▶  typst_syntax::Source
mod source;    ─┘  通过 pub use 放到 crate 根       typst_syntax::parse
                                                                 typst_syntax::SyntaxNode
pub mod ast;   ───▶ 整个模块对外可见 ──────────▶  typst_syntax::ast::...
pub mod package;──▶ 整个模块对外可见 ──────────▶  typst_syntax::package::...
```

一句话总结：**`pub mod` 整扇门打开；`mod` + `pub use` 只开一扇窗。**

#### 4.4.3 源码精读

**模块声明部分**：

[src/lib.rs:3-16](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lib.rs#L3-L16) —— 这里声明了全部子模块。

可以清楚地分成两组：

- **公开模块**（`pub mod`）：只有 `ast` 和 `package` 两个。意味着 `typst_syntax::ast::...` 和 `typst_syntax::package::...` 的完整内容对外可见。
- **私有模块**（不带 `pub` 的 `mod`）：`highlight`、`kind`、`lexer`、`lines`、`node`、`parser`、`path`、`reparser`、`set`、`source`、`span`。它们**不**直接对外，外部只能用到下面 `pub use` 挑出来的那些类型。

**公共导出部分**：

[src/lib.rs:18-36](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lib.rs#L18-L36) —— 把私有模块里的关键类型/函数集中导出到 crate 根。

按下表归类（这正是综合实践要你整理的内容）：

| 来源模块 | 导出的类型 / 函数 | 用途速览 |
|----------|------------------|---------|
| `highlight` | `Tag`, `highlight`, `highlight_html` | 语法高亮 |
| `kind` | `SyntaxKind` | 所有 token/节点类型的枚举 |
| `lexer` | `is_id_start`, `is_id_continue`, `is_ident`, `is_newline`, `is_valid_label_literal_id`, `link_prefix`, `split_newlines` | 字符判定工具函数 |
| `lines` | `Lines` | 行列索引与编码转换 |
| `node` | `Diagnosis`, `LinkedChildren`, `LinkedNode`, `Side`, `SyntaxDiagnostic`, `SyntaxNode` | CST 数据结构 |
| `parser` | `parse`, `parse_code`, `parse_math` | 三个解析入口 |
| `path` | `FileId`, `PathError`, `RealizeError`, `RootedPath`, `VirtualPath`, `VirtualRoot`, `VirtualizeError` | 文件身份与虚拟路径 |
| `source` | `Source` | 「文本+树+行列」的文件抽象 |
| `span` | `DiagSpan`, `DiagSpanKind`, `RangeMapper`, `Span`, `SpanKind`, `SpanNumber`, `Spanned`, `SubRange` | 源码定位（span）系统 |

> 注意：这些表里的名字不需要现在全部记住。你只要记住「`lib.rs` 用 `pub use` 把这些类型摆到了 crate 根」这件事，后续讲义会逐一深入。

**内部私有引用（不导出）**：

[src/lib.rs:38-39](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lib.rs#L38-L39) —— 注意这里是普通的 `use`（不是 `pub use`），引用了 `Lexer` 和 `reparse_block` / `reparse_markup`。它们是 crate 内部使用的辅助项，**不**对外暴露。这正好和上面的 `pub use` 形成对照：一个对外，一个对内。

#### 4.4.4 代码实践

1. **目标**：练习「从 `lib.rs` 读出对外 API」的能力。
2. **步骤**：
   - 打开 [src/lib.rs:18-36](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lib.rs#L18-L36)。
   - 按来源模块分组，把每一条 `pub use` 导出的名字列成一张表（就像上文那样，但这次由你自己整理）。
   - 再检查 [src/lib.rs:38-39](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lib.rs#L38-L39)，确认 `Lexer` 等是私有引用、**不会**出现在外部 API 里。
3. **观察现象**：你会发现 `node` 模块虽然私有，却贡献了最多的导出类型；而 `ast` / `package` 是整模块公开。
4. **预期结果**：你能回答「`typst_syntax::Source` 这个类型最初来自哪个私有模块？」（答案：`source`）。

#### 4.4.5 小练习与答案

**练习 1**：`pub mod ast;` 与 `mod node;` 有什么区别？

> **答案**：`pub mod ast;` 把整个 `ast` 模块对外公开，外部可直接访问 `typst_syntax::ast::...` 的全部内容；`mod node;` 是私有模块，外部无法整体访问，只能用到经 `pub use` 挑出的类型（如 `SyntaxNode`）。

**练习 2**：下面哪些会出现在 `typst_syntax` 的对外 API 中？（A）`Source`（B）`Lexer`（C）`parse`（D）`SyntaxMode`。

> **答案**：A、C、D。`Lexer` 是 [src/lib.rs:38](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lib.rs#L38) 的私有 `use`，不对外；其余三者分别通过 `pub use`（Source、parse）或 `pub enum`（SyntaxMode）暴露。

**练习 3**：为什么 Typst 选择把 `node` 模块设为私有，却用 `pub use` 导出 `SyntaxNode` 等类型，而不是直接 `pub mod node;`？

> **答案**：这样可以把「实现细节」（模块内部的辅助类型、内部函数）隐藏起来，只把稳定的对外类型摆在 crate 根，既缩短了使用者的导入路径，也降低了内部重构对外部的破坏面。

---

## 5. 综合实践

本讲的综合实践把 4.1 和 4.4 串起来，是本讲唯一需要「动笔」的任务。

**任务背景**：假设你要向一位没接触过 `typst-syntax` 的同事介绍这个 crate，需要一份一页纸的速览。

**操作步骤**：

1. 阅读 [README.md:3-7](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/README.md#L3-L7) 与 [src/lib.rs:1-50](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lib.rs#L1-L50)。
2. 用**自己的话**（不要照抄英文原文）写出 `typst-syntax` 的 **5 项核心职责**。参考维度：它管什么输入、产出什么结构、附带哪些基础设施。
3. 整理一张「对外 API 速查表」：把 [src/lib.rs:18-36](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lib.rs#L18-L36) 的每一条 `pub use`，按「来源模块 → 导出的类型/函数 → 一句话用途」列出来，并指出哪些是 `pub mod` 整模块公开（`ast`、`package`）。
4. 最后用一句话回答：CST 与 AST 各自服务于谁？

**预期结果**：

- 5 项职责应当大致覆盖：① 词法/语法解析（文本→树）；② 构建 CST（`SyntaxNode`）；③ 转换出 AST（供求值）；④ span/文件 id 等定位基础设施；⑤ 语法高亮（及增量重解析等附加能力）。
- 速查表应与第 4.4.3 节的表格一致。
- CST 服务于工具/高亮/重解析，AST 服务于求值。

> 如果你暂时无法确认某一项是否准确，请标注「待本地验证」，不要编造。

## 6. 本讲小结

- `typst-syntax` 是 Typst 的**编译前端**：它把源码文本变成语法树，并提供 span 定位、文件身份、增量重解析、语法高亮等基础设施，**不**负责最终排版。
- 它同时维护两棵树：**CST**（具体语法树，无损，载体是 `SyntaxNode`）是真相来源；**AST**（抽象语法树，类型化、按需转换）服务于求值。
- `SyntaxMode` 枚举有三种模式 `Markup` / `Math` / `Code`，用来区分同一段文本在不同上下文里的解析规则。
- `lib.rs` 是 crate 的门面：用 `pub mod` 整体公开 `ast`、`package`，用 `mod` + `pub use` 选择性暴露 `Source`、`parse`、`SyntaxNode`、`Span`、`SyntaxKind` 等核心类型。
- 设计借鉴 rust-analyzer（解析器结构）与一篇增量编译论文（重解析算法），这解释了后续会看到的 marker、事件式解析等「rust-analyzer 风格」设计。

## 7. 下一步学习建议

本讲只是「看清了门牌」。接下来建议：

1. **先跑通端到端**：阅读下一讲 **u1-l2（构建、依赖与运行方式）**，学会用 `cargo` 单独构建/测试本 crate，并在一个最小程序里调用 `parse` 或 `Source::detached`，亲眼看一次「文本→语法树」的过程。
2. **建立模块地图**：再读 **u1-l3（源码目录与模块地图）**，把 README 列出的 14 个源码文件的职责与依赖关系画成一张图。
3. **再看词汇表**：U2 会系统讲解贯穿全 crate 的 `SyntaxKind` 枚举与 `SyntaxSet` 位集——那是理解后续 lexer/parser 的前提。

> 建议按 u1-l2 → u1-l3 → u1-l4 → U2 的顺序推进，每篇都动手做一次实践。

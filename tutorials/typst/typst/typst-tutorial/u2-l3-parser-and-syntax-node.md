# 语法树：Parser 与 SyntaxNode

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `parse` / `parse_code` / `parse_math` 三个解析入口各自的使用场景，以及 markup（标记）、code（代码）、math（数学）三种语法模式如何在**同一次**解析中互相切换；
- 读懂 `SyntaxNode` 的内部形态（叶子、内部节点、错误节点、警告包装）与它的「无损具体语法树（CST）」设计：父节点缓存的长度、后代数、诊断摘要，以及 span 编号区间；
- 描述本轮重构后的编号接口：`InnerNode::numberize` 的签名改为接受 `Option<&mut Span>` 与子节点区间参数，`InnerNode` 提升为 `pub(super)`，`replace_children` / `renumber_children` / `update_parent` 等重解析方法已从 node.rs 迁移到 reparser.rs 的 `impl InnerNode` 块；
- 解释 Typst「解析永不失败、错误进树」的容错策略：错误如何变成树上的 `Error` 节点、解析器如何避免错误级联、如何在嵌套深度超限时自保；
- 亲手打印一段含语法错误的源码的完整语法树，并验证错误不会阻断后续内容的解析。

## 2. 前置知识

在进入源码之前，先用通俗语言补齐几个概念。上一讲（u2-l2）讲过词法器 `Lexer`，本讲直接消费它的产物。

- **token 与 `SyntaxKind`**：词法器把源码字符串切成一个个带类别的片段（标识符、数字、星号……），类别就是 `SyntaxKind` 枚举。本讲只需要知道两件事：`SyntaxKind::Error` 表示「这段字符序列本身就不合法」；每个 `SyntaxKind` 都能通过 `name()`、`is_trivia()`、`is_keyword()`、`is_grouping()` 等方法快速分类。
- **三种语法模式 `SyntaxMode`**：Typst 源码里同一串字符在不同位置含义不同。文件顶层是 markup（正文文本），`#` 之后是 code（表达式），`$ ... $` 之间是 math（公式）。模式决定了词法器怎么分词、解析器期待什么结构。
- **递归下降（recursive descent）**：手写解析器最常见的写法——「看到什么 token，就调用对应的解析函数」。函数体里再调用更深的解析函数，调用栈的形状恰好对应语法树的嵌套形状。
- **优先级爬升（precedence climbing）**：处理 `1 + 2 * 3` 这类二元运算的技巧：给每个运算符定优先级，解析函数带一个「当前最低优先级」参数，遇到优先级更低的运算符就返回，交给上层处理。
- **具体语法树 CST vs 抽象语法树 AST**：AST 只保留语义上有意义的结构；CST（本讲的 `SyntaxNode`）**无损**地保留源码中的一切——包括空白、注释这类「琐碎内容」（trivia），因此可以从树完整还原出源文本。typst-syntax 的 README 明确说 parser.rs 的职责是「preparing a Concrete Syntax Tree made of nested vectors of `SyntaxNode`s」（见 [crates/typst-syntax/README.md:L14-L15](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/README.md#L14-L15)），并且整个 parser 的结构借鉴自 rust-analyzer（[crates/typst-syntax/README.md:L31-L33](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/README.md#L31-L33)）。
- **容错解析（error-tolerant parsing）**：编辑器需要在用户写了一半、甚至写错的情况下继续工作，所以解析器**不能一遇到错误就中断**，而要把错误记录下来继续解析。Typst 把这条原则推到极致：`parse` 永远返回一棵树，错误只是树上的特殊节点。
- **Span**：上一讲（u2-l1）介绍过的源码定位手段。本讲会看到解析结束后如何给整棵树的节点分配编号区间。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [crates/typst-syntax/src/parser.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/parser.rs) | 手写递归下降解析器：三个入口、三套主循环、错误恢复都在这里 |
| [crates/typst-syntax/src/node.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/node.rs) | `SyntaxNode`：CST 的数据结构、错误/警告节点、诊断收集、span 编号入口 |
| [crates/typst-syntax/src/reparser.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/reparser.rs) | 增量重解析算法；本轮重构后，`impl InnerNode` 的 `replace_children` / `renumber_children` / `update_parent` 也住在这里（详见 4.2.3 ⑧） |
| [crates/typst-syntax/src/kind.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/kind.rs) | `SyntaxKind` 枚举：所有 token 与节点类别，以及 `is_error()` 等分类判断 |
| [crates/typst-syntax/src/set.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/set.rs) | `SyntaxSet`：用 `u128` 位图表示的一组 `SyntaxKind`，解析器靠它声明「我可以接受哪些 token」 |
| [crates/typst-syntax/src/lib.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/lib.rs) | crate 出口；`SyntaxMode` 三模式枚举定义于此 |
| [crates/typst-syntax/README.md](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/README.md) | crate 自述：各文件职责与设计出处（rust-analyzer、Martin 的增量编译论文） |

本讲的重点是前两个文件（外加 reparser.rs 中迁入的 `impl InnerNode` 块）；其余只取用到的片段。

## 4. 核心概念与源码讲解

### 4.1 递归下降主循环

#### 4.1.1 概念说明

parser.rs 要解决的问题：把 token 流组织成一棵结构化的 `SyntaxNode` 树。难点在于 Typst 的「三模式」——markup 里可以嵌 code（`#` 引出表达式）和 math（`$` 引出公式），code 里又可以嵌 markup（`[ ... ]` 内容块）和 math，如此往复。

Typst 的做法不是为每种模式写三个独立解析器，而是**一个 `Parser` 结构，模式作为词法器的一个状态**，在递归下降过程中通过 `enter_modes` 压栈/出栈式地切换。README 对 parser.rs 的定位是「The main parser definition」（[crates/typst-syntax/README.md:L14-L15](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/README.md#L14-L15)）。

三种模式由 [crates/typst-syntax/src/lib.rs:L42-L50](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/lib.rs#L42-L50) 中的 `SyntaxMode` 枚举定义：

- `Markup`：文件顶层、内容块 `[ ... ]` 内部——正文文本；
- `Math`：公式 `$ ... $` 内部——数学原子与运算符；
- `Code`：`#` 之后、代码块 `{ ... }` 内部——关键字、字面量、运算符。

#### 4.1.2 核心流程

一次 `parse(text)` 的整体流程：

```text
parse(text)
 ├─ Parser::new(text, 0, SyntaxMode::Markup)   ← 创建解析器，模式为 Markup
 ├─ markup_exprs(p, stop 集合含 End)           ← 主循环：逐个吃 markup 表达式
 │    while !p.at_set(stop_set):
 │        markup_expr(p, at_start, &mut nesting)
 │            ├─ 看到 Text/Space/Link/Label/... → p.eat()          （直接吃掉）
 │            ├─ 看到 *  → strong()               （递归：粗体）
 │            ├─ 看到行首的标题标记 → heading()     （递归：标题）
 │            ├─ 看到 $  → equation()             ← 切到 Math 模式
 │            ├─ 看到 #  → embedded_code_expr()   ← 切到 Code 模式
 │            └─ 其他      → p.unexpected()        （错误进树，见 4.3）
 └─ p.finish_into(SyntaxKind::Markup)           ← 把所有孩子打包成根节点
```

三个入口完全对称，只是初始模式和主循环不同：

- `parse`（[parser.rs:L15-L21](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/parser.rs#L15-L21)）：Markup 模式 + `markup_exprs`，根节点 kind 为 `Markup`；
- `parse_code`（[parser.rs:L24-L30](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/parser.rs#L24-L30)）：Code 模式 + `code_exprs`，根节点 kind 为 `Code`；
- `parse_math`（[parser.rs:L33-L39](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/parser.rs#L33-L39)）：Math 模式 + `math_exprs`，根节点 kind 为 `Math`。

模式切换的四个「铰链」：

| 源码形态 | 触发函数 | 进入的模式 |
| --- | --- | --- |
| `#expr`（markup/math 中嵌代码） | `embedded_code_expr` | Code |
| `$ ... $`（markup 中嵌公式） | `equation` | Math |
| `{ ... }`（代码块） | `code_block` | Code |
| `[ ... ]`（内容块） | `content_block` | Markup |

code 模式的主循环里，表达式之间要求分号或换行分隔，否则报 `expected semicolon or line break`；math 模式的主循环最简单：不是数学表达式就 `unexpected()`。三者都由 `check_depth_until` / `increase_depth` 兜底，嵌套深度上限是 `MAX_DEPTH`，值为 256（[parser.rs:L12](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/parser.rs#L12)），防止病态输入（如几百个左括号）把递归调用栈撑爆。

#### 4.1.3 源码精读

**① 三个入口**（[crates/typst-syntax/src/parser.rs:L15-L39](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/parser.rs#L15-L39)）：每个入口只做三件事——建 `Parser`、跑主循环、`finish_into` 打包根节点。函数体开头的 `typst_timing::TimingScope` 只在启用 `timing` feature 时生效，用于统计解析耗时。

**② markup 主循环**（[crates/typst-syntax/src/parser.rs:L52-L63](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/parser.rs#L52-L63)）：`while !p.at_set(stop_set)` 不断调用 `markup_expr`；`at_start` 记录「是否位于行首」，它决定标题、列表这类**行首标记**（`HeadingMarker`、`ListMarker` 等）是否生效。`nesting` 计数器处理 markup 里裸 `[` `]` 的嵌套——行内括号会被当作普通文本吃掉，而不是结构符号。

**③ markup 分发**（[crates/typst-syntax/src/parser.rs:L89-L136](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/parser.rs#L89-L136)）：一个巨大的 `match p.current()`。这是理解「看到什么 token 走什么路」的最好速查表：`Star` 走 `strong()`，`Dollar` 走 `equation()`，`Hash` 走 `embedded_code_expr()`，行首的 `HeadingMarker`/`ListMarker`/`EnumMarker`/`TermMarker` 走对应结构函数；落空则 `p.unexpected()`。

**④ 嵌入式代码表达式**（[crates/typst-syntax/src/parser.rs:L560-L579](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/parser.rs#L560-L579)）：`#` 之后切到 Code 模式解析一个表达式。注意 `if p.had_trivia() || p.end()` 分支会调用 `p.expected("expression")` 然后返回——`#` 后面直接结束（源码只有一个 `#`）就会在这里产生 `expected expression` 错误节点，这正是 4.3.4 实践会看到的树形。

**⑤ 公式与两种块**（[crates/typst-syntax/src/parser.rs:L227-L244](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/parser.rs#L227-L244)）：`equation` 用 `p.enter_modes(SyntaxMode::Math, ...)` 进入数学模式，吃掉 `$`、解析 `Math` 节点、用 `expect_closing_delimiter` 等待收尾 `$`，最后 `wrap` 成 `Equation` 节点。[parser.rs:L748-L768](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/parser.rs#L748-L768) 的 `code_block` / `content_block` 同理，分别用 `enter_modes` 切到 Code / Markup——「调用栈就是模式栈」。

**⑥ 模式切换的实现**（[crates/typst-syntax/src/parser.rs:L1791-L1830](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/parser.rs#L1791-L1830)）：`enter_modes` 保存旧模式，设置词法器新模式，执行闭包，然后恢复旧模式并**重新词法分析当前 token**（因为同一段字符在 markup 和 code 下会分成不同的 token）。`with_nl_mode` 的文档注释说得很直白：「This function effectively repurposes the call stack as a stack of modes」。

**⑦ Parser 的状态**（[crates/typst-syntax/src/parser.rs:L1480-L1506](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/parser.rs#L1480-L1506)）：`Parser` 持有共享文本 `text`、多模式 `lexer`、`nodes`（已产出的语法树节点，按序排列）、单 token 前瞻 `token`（`Token` 结构缓存了当前 token 的 kind、节点、前置 trivia 数量、换行信息）、`balanced`（括号是否配平）、记忆化用的 `memo` 与嵌套深度 `depth`。**已经解析好的节点以扁平 `Vec` 形式堆在 `nodes` 里，需要成组时用 `wrap` 收拢**——这就是「nested vectors of SyntaxNodes」的真正含义。

**⑧ 吃 token 与打包**（[crates/typst-syntax/src/parser.rs:L1750-L1789](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/parser.rs#L1750-L1789)）：`eat` 把当前 token 的节点 push 进 `nodes` 再前移词法器；`wrap(from, kind)` 把 `from` 标记到当前 token 之前的所有节点 drain 出来，包成一个 `SyntaxNode::inner` 放回原位。配合 `marker()`（[parser.rs:L1702-L1712](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/parser.rs#L1702-L1712)）记录的位置，「先解析孩子、再包装父亲」就是 Typst 建树的全部秘密。

另外值得一提：`code_expr_prec`（[parser.rs:L587](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/parser.rs#L587) 起）实现优先级爬升；`Parser` 的 `memo` 字段配合 `expr_with_paren`（[parser.rs:L987](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/parser.rs#L987) 起）做了类似 packrat 的解析回溯记忆化，本讲不展开。

#### 4.1.4 代码实践

**实践目标**：亲眼看到递归下降「一个 token 一个 token 地吃」的过程。

**操作步骤**：

1. 打开 `crates/typst-syntax/src/parser.rs`，定位到 `fn eat`（约 L1750）；
2. 在 `self.nodes.push(...)` 之后临时加一行日志（示例代码，验证后请还原）：

```rust
fn eat(&mut self) {
    self.nodes.push(std::mem::take(&mut self.token.node));
    eprintln!("eat: {:?}", self.nodes.last().unwrap()); // 临时添加
    self.token = Self::lex(&mut self.nodes, &mut self.lexer, self.nl_mode);
}
```

3. 运行仓库自带的树形输出测试：

```bash
cargo test -p typst-syntax test_debug -- --nocapture
```

**需要观察的现象**：stderr 里按时间顺序打印出每个进入 `nodes` 的节点（含空白这类 trivia 节点），形如 `eat: HeadingMarker: "="`、`eat: SpaceNoNewline: " "`、`eat: Text: "Head"`……（具体顺序与措辞待本地验证）。

**预期结果**：日志序列恰好是测试断言的那棵树（见 4.3.4）的**前序遍历**；测试本身仍然通过，说明加日志没有破坏解析行为。观察完后删除日志行。

#### 4.1.5 小练习与答案

**练习 1**：`parse`、`parse_code`、`parse_math` 分别在什么场景被调用？

**答案**：`parse` 解析整个源文件（markup 是文档顶层）；`parse_code` 用于解析「纯代码」的片段（需要以代码模式为起点的输入场景）；`parse_math` 用于把一整段文本按公式解析。三者共享同一套 `Parser` 机制，只是初始 `SyntaxMode` 与主循环不同。

**练习 2**：标题标记（`HeadingMarker`，即行首的等号加空格）为什么只有出现在行首才生效？

**答案**：`markup_exprs` 主循环用 `at_start`（由 `p.had_newline()` 维护）追踪是否位于行首；`markup_expr` 的 match 分支写成 `SyntaxKind::HeadingMarker if at_start` 才调用 `heading(p)`，非行首的等号会走别的分支（被当作文本处理）。行首约束是 markup 模式区别于 code 模式的关键上下文敏感性。

**练习 3**：`Parser` 的 `token` 字段为什么说是「单 token 前瞻」？

**答案**：解析器任何时刻只持有当前 token（还没进 `nodes`），`current()`/`at()` 都只看它；`eat()` 之后才通过 `lex()` 取下一个。匹配分支时只能「看一眼前」，需要更多上下文的场合靠 `wrap`（事后成组）和 `memo`（回溯记忆化）弥补。

### 4.2 SyntaxNode 节点模型

#### 4.2.1 概念说明

`SyntaxNode` 是整个 typst-syntax 的中枢数据结构：parser 造它、AST 在它之上提供类型化视图（下一讲）、高亮器和 IDE 遍历它。它的设计目标是**无损 + 可增量维护**，也就是 rust-analyzer 一系的「绿树」思想（README 明确说 parser 结构 adapted from Rust Analyzer，[crates/typst-syntax/README.md:L31-L33](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/README.md#L31-L33)）。

「无损」体现在：树上保留一切 token（包括空白、注释这些 trivia），任意子树的文本拼接起来就是原文。「可增量维护」体现在三点：

1. 每个内部节点缓存了整棵子树的**摘要信息**（总字节长、总节点数、有无错误/警告），改一个孩子只需沿路径更新祖先的摘要；
2. 每个节点持有**编号区间**（span number），新节点插进来时尽量不动兄弟的编号；
3. 提供一整套**局部替换 + 重编号**的操作（`replace_children` / `renumber_children` / `update_parent`），供增量重解析使用。

> **重构提示**：第 3 点提到的三个方法**不再位于 node.rs**——它们已整体迁移到 reparser.rs 的 `impl InnerNode` 块中（见 4.2.3 ⑧）。node.rs 只保留树的「构造与查询」职责，把「修改」职责让给了重解析模块。

#### 4.2.2 核心流程

一个节点的四种内部形态（`Node` 枚举，[node.rs:L52-L58](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/node.rs#L52-L58)）：

| 形态 | 存储内容 | 对应 `SyntaxKind` |
| --- | --- | --- |
| `Leaf(text, kind)` | token 原文 + 类别 | 除 Error 外的任意类别 |
| `Inner(Arc<InnerNode>, kind)` | 孩子列表 + 摘要 | 结构类别（Markup、Heading、Equation…） |
| `Error(Arc<ErrorNode>, kind)` | 错误消息 + 触发文本 + 提示 | 恒为 `SyntaxKind::Error` |
| `Warning(Arc<WarningWrapper>, kind)` | 包裹一个孩子 + 警告消息 | 同被包裹孩子的类别 |

文档注释（[node.rs:L27-L51](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/node.rs#L27-L51)）写明了不变式：**错误节点的 kind 恒为 `Error`，叶子和内部节点永远不是 `Error`**；警告只是「包装」，穿透后仍是原节点。由于警告把孩子的 kind 复制了一份，改 kind 时必须穿过警告逐层赋值（这正是 `Node::node_ref` / `Node::inner_mut` 两个辅助方法存在的理由，[node.rs:L67-L101](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/node.rs#L67-L101)）。

建树与聚合流程：

```text
SyntaxNode::leaf(kind, text)    → 叶子
SyntaxNode::inner(kind, kids)   → InnerNode::new(kids)：
      len         = Σ child.len()
      descendants = 1 + Σ child.descendants()
      diagnosis   = OR(所有 child.diagnosis)      ← 错误/警告向上冒泡缓存
SyntaxNode::error(msg, text)    → 错误节点
node.warn(msg)                  → 把现有节点包进 Warning
```

span 编号（`numberize`）把一个 `u64` 区间 `within` 分配给子树：先算步长

\[ \text{stride} = \left\lfloor \frac{\text{within.end} - \text{within.start}}{2 \times \text{descendants}} \right\rfloor \]

每个孩子分到「后代数 × stride」宽的子区间，节点自己的编号取其中点 \((start + end) / 2\)。刻意只用**左半边**空间，是为了「留出空位给未来的插入」——增量编辑插入新节点时，兄弟的编号可以保持不变。区间挤不下时返回 `Unnumberable`，调用方（reparser 的 `renumber_children` 会先向两侧指数扩容重试，仍失败才上报，最终由 `Source` 层退回更大范围的处理）。

**新签名的含义**：本轮重构后，`InnerNode::numberize` 接受四个参数——`span: Option<&mut Span>`、`id`、`range: Option<Range<usize>>`、`within`。两者互斥（源码里有 `debug_assert!(range.is_none(), "node_span and range are exclusive")`）：

- `span = Some(&mut 自身span)`、`range = None`：传统全量编号——给自己和所有孩子分配编号；
- `span = None`、`range = Some(子区间)`：**只给一段孩子重新编号，自己的编号不动**——这是为增量重解析新增的能力，替换了几个孩子之后无需触碰父节点已有的编号。

#### 4.2.3 源码精读

**① 结构定义**（[crates/typst-syntax/src/node.rs:L16-L25](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/node.rs#L16-L25)）：`SyntaxNode` 只有 `data: Node` 和 `span: Span` 两个字段——span 提到顶层是为了高频访问的效率（注释里甚至为省 8 个字节把 kind 留在 `Node` 里）。

**② 构造器家族**（[crates/typst-syntax/src/node.rs:L104-L168](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/node.rs#L104-L168)）：`leaf` / `inner` 带 `debug_assert!(!kind.is_error())`，把「错误必须用 `error()` 造」这一不变式写进调试断言；`error(message, text)` 注意参数顺序是「消息在前、文本在后」；`warn` 是原地包裹。此外还有带定位能力的 `warn_at`（警告只指向节点内部的一段子区间 `SubRange`）与 `hint` / `hint_at` / `with_hints`（给错误或警告追加提示，同样可带子区间）——提示不再只是一个字符串，而是 `(EcoString, Option<SubRange>)` 二元组。

**③ InnerNode 与聚合**（[crates/typst-syntax/src/node.rs:L647-L677](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/node.rs#L647-L677)）：`InnerNode` 的字段就是 4.2.2 表里列的摘要：`len`、`descendants`、`diagnosis`、`upper`（编号区间上界）、`children`。`new` 在建节点时一次算清全部摘要。注意结构体声明现在是 `pub(super)`——即对整个 crate 可见，这是为了让 reparser.rs 能直接操作它。

**④ 编号算法**（[crates/typst-syntax/src/node.rs:L681-L729](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/node.rs#L681-L729)）：即上面的 stride 公式，源码注释原话是「we try to fit all numbers into the left half of `within` so that there is space for future insertions」。对外入口是 `SyntaxNode::numberize`（[node.rs:L529-L543](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/node.rs#L529-L543)）：叶子/错误节点直接取区间中点，内部节点则委托 `inner.numberize(Some(&mut self.span), id, None, within)`。`Unnumberable` 与 `NumberingResult` 定义在 [node.rs:L1366-L1379](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/node.rs#L1366-L1379)。

**⑤ 诊断摘要与收集**（[crates/typst-syntax/src/node.rs:L288-L330](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/node.rs#L288-L330)）：`diagnosis()` 直接返回缓存的 `Diagnosis`（`errors`、`warnings` 两个公开布尔位，结构体与实现见 [node.rs:L755-L787](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/node.rs#L755-L787)），所以「这棵子树有没有错」是 O(1) 查询；`errors_and_warnings()` 才真正遍历树，且用 `diagnosis` 剪枝——没问题的子树直接跳过。收集产物 `SyntaxDiagnostic`（含 `is_error`、`span`、`message`、`hints`）定义于 [node.rs:L789-L802](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/node.rs#L789-L802)；注意其中 `span` 的类型是 `DiagSpan`（上一讲 u2-l1 介绍过的诊断专用 span 形态），提示也是带 `DiagSpan` 的 `Spanned` 列表。

**⑥ Debug 输出**（[crates/typst-syntax/src/node.rs:L623-L639](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/node.rs#L623-L639) 与 [node.rs:L832-L858](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/node.rs#L832-L858)）：`{:?}` 格式为——叶子 `Kind: "text"`、内部节点 `Kind: len [ ...孩子... ]`、错误节点在无文本无提示时简写为 `Error: "message"`，否则输出带 text/message/hint 字段的结构体。用 `{:#?}` 就能得到缩进的整棵树，这是本讲实践的读树工具。

**⑦ 定位与遍历辅助**：`children()`（[node.rs:L280-L286](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/node.rs#L280-L286)）对叶子/错误返回空迭代器；带父指针、可反查字节区间的 `LinkedNode`（[node.rs:L961 起](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/node.rs#L961)）是 IDE 场景的主力，本讲不展开。另外 `is_leaf` / `is_inner` / `descendants` / `children_mut` / `upper` 等一批查询方法（[node.rs:L566-L600](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/node.rs#L566-L600)）现在都标为 `pub(super)`——它们是给 crate 内部（主要是 reparser.rs）用的。

**⑧ 迁往 reparser.rs 的修改操作**：`replace_children`、`renumber_children`、`update_parent` 三个方法现在位于 [crates/typst-syntax/src/reparser.rs:L338-L474](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/reparser.rs#L338-L474) 的 `impl InnerNode` 块中：

- `update_parent`（[reparser.rs:L344-L368](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/reparser.rs#L344-L368)）用代数更新摘要：`len` 与 `descendants` 减旧加新；`diagnosis` 则在能短路时直接采用新值，否则对孩子的 `Diagnosis` 重新做 OR；
- `replace_children`（[reparser.rs:L376-L420](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/reparser.rs#L376-L420)）在真正 splice 之前先**裁掉公共前缀与公共后缀**（用 `spanless_eq` 比较，spanless 相同的孩子原样保留），只为中间真正变化的部分做替换——这是编号稳定的第二重保障；
- `renumber_children`（[reparser.rs:L424-L474](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/reparser.rs#L424-L474)）给替换后的孩子区间重新编号：编号下界取「前一孩子的 `upper()` 或父节点编号 +1」，上界取「后一孩子的编号或父节点的 `upper`」；装不下时**向两侧指数扩容**重试，全装不下才返回 `Unnumberable`。L461 的 `self.numberize(None, id, Some(renumber), within)` 正是新签名 `Option<&mut Span>` 的用武之地——父节点编号不参与，只给孩子分区。

与之配套，node.rs 新增了 `reparse_inner_node_mut`（[node.rs:L601-L620](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/node.rs#L601-L620)）：它返回 `Option<(&mut InnerNode, SyntaxKind, Span)>`，**刻意不穿透 Warning 包装**——注释解释说在语法警告内部做重解析无法保证正确（警告的子区间定位可能恰好落在被替换的文本上）。这与 4.2.3 ② 的 `Node::inner_mut`（[node.rs:L81-L91](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/node.rs#L81-L91)，会穿透警告）形成对照：一个是「一般性内部访问」，一个是「重解析专用的安全访问」。

#### 4.2.4 代码实践

**实践目标**：用 Rust 亲手造几个节点，验证「摘要聚合」与 Debug 格式。

**操作步骤**：

1. 打开 `crates/typst-syntax/src/node.rs`，滚到文件末尾的 `#[cfg(test)] mod tests`（L1381 起，`test_debug` 就在这里）；
2. 在 `mod tests` 内临时添加一个测试（示例代码，验证后可删除）：

```rust
#[test]
fn test_manual_tree() {
    // 手工构造：Markup 包着一个文本叶子和一个错误节点
    let text = SyntaxNode::leaf(SyntaxKind::Text, "Hi");
    let err = SyntaxNode::error("boom", "!!");
    let root = SyntaxNode::inner(SyntaxKind::Markup, vec![text, err]);

    // 摘要聚合：2 字节文本 + 2 字节错误文本
    assert_eq!(root.len(), 4);
    assert!(root.diagnosis().errors);   // 错误向上冒泡到了根
    println!("{root:#?}");              // 观察树形 Debug 输出
}
```

3. 运行：

```bash
cargo test -p typst-syntax test_manual_tree -- --nocapture
```

**需要观察的现象**：打印出的树根是 `Markup: 4 [...]`，孩子包含 `Text: "Hi"` 与带 text、message 字段的 `Error` 节点（具体排版细节待本地验证）。

**预期结果**：`len()` 等于各孩子字节长之和；`diagnosis().errors` 为 true；`children()` 能迭代出两个孩子。这说明摘要字段在建节点时就被正确聚合。

**延伸（选做）**：测试模块里新增的 `test_numberize_basic` / `test_numberize_replace` / `test_numberize_insertion`（[node.rs:L1591 起](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/node.rs#L1591)）系统性地断言了「插入多少个节点才会迫使兄弟编号改变」；其中辅助函数 `replace_and_renumber`（[node.rs:L1690-L1702](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/node.rs#L1690-L1702)）串联调用迁移后的 `InnerNode::replace_children` 与 `renumber_children`，是理解 4.2.3 ⑧ 的最佳读料。

#### 4.2.5 小练习与答案

**练习 1**：为什么错误不做成独立的顶层类型，而是 `Node` 枚举的一个变体？

**答案**：统一在 `SyntaxNode` 里，parser 的 `nodes: Vec<SyntaxNode>` 与所有 `wrap`/`children` 逻辑无需为错误特判类型；同时不变式「kind 为 Error 的节点只出现在 Error 变体里」由构造器断言和 `convert_to_kind` 的 panic 保证，诊断收集又能靠 `diagnosis` 摘要 O(1) 剪枝。

**练习 2**：`InnerNode` 为什么要缓存 `len` 和 `descendants`？

**答案**：CST 的文本与结构查询极高频（诊断定位、增量重解析、IDE 跳转都要「这个节点覆盖哪个字节区间」）。缓存后，任意节点的区间可由「祖先偏移累加 + 自身 len」直接得出；`numberize` 分配编号区间也直接依赖 `descendants`。没有缓存就要每次遍历子树。

**练习 3**：`numberize` 的步长为什么要除以 `2 * descendants` 而不是 `descendants`？

**答案**：只用区间左半边给现有节点编号，右半边留白。将来在中间插入新节点时，可以从留白中取号，**兄弟节点的编号保持稳定**，下游基于 span 的缓存（comemo 记忆化、增量编译）就不会大面积失效。这就是「绿树」为增量编译准备的编号方案。

**练习 4**：`InnerNode::numberize` 的 `span: Option<&mut Span>` 参数为什么设计成可空的？

**答案**：全量解析后编号时传 `Some(&mut self.span)`，要求同时给节点自身和孩子编号；增量重解析替换了一段孩子后，`renumber_children` 传 `None` 加 `Some(range)`，只重排那段孩子的编号、父节点编号保持不变。一个签名同时服务两种场景，避免了重复实现同一套 stride 分配逻辑。

### 4.3 容错与错误恢复策略

#### 4.3.1 概念说明

Typst 的解析阶段**永不返回 Err**：`parse(&str) -> SyntaxNode` 总是给出完整的树。所有语法错误都表现为树上的 `Error` 节点，后续阶段（求值、IDE）通过 `errors_and_warnings()` 把它们收集成 `SyntaxDiagnostic`。这带来三个直接好处：

1. 编辑器在文档半成品状态下仍能继续提供高亮、补全、预览；
2. 一次编译可以**同时**报告多个错误，而不是第一个错误就停；
3. 错误节点本身携带原文文本，树仍然无损，错误修复后增量重解析可以精确替换。

错误的来源分两类：词法错误（Lexer 发现非法字符序列，直接产出 kind 为 `Error` 的 token）与语法错误（Parser 发现结构不符合预期）。Parser 对两类错误的处理统一收敛到几个小函数。

#### 4.3.2 核心流程

Parser 挂错误的四种方式：

```text
① expected_at(m, thing)   在标记 m 处「插入」一个零长 Error 节点
                           （不消耗任何 token，表示"这里缺东西"）
② node.unexpected()       把当前 token 吃掉并整体转成
                           Error("unexpected " + kind 名)
③ node.expected(name)     把已吃掉的 token 转成
                           Error("expected X, found Y")
④ expect_closing_delimiter(open, kind)
                           到结束都没等到右括号时，把开头 m 处的
                           左括号节点转成 Error("unclosed delimiter")
```

防级联与保底机制：

- `after_error()`：上一个非 trivia 节点已是错误时，`expected` 不再重复插入零长错误（避免一行错报十次）；
- `trim_errors()`：回滚掉末尾连续的零长错误节点，常在产生「更有信息量」的错误前先清理现场；
- `balanced` 标志：遇到未配平的分组括号就置 false，增量重解析（u2-l5）据此放弃局部重解析、退回更大范围；
- 深度保护：`check_depth_until` / `increase_depth` 超过 `MAX_DEPTH`（256）时调用 `depth_check_error`，它会**保证向前吃掉至少一个 token**（必要时顺带配平括号）再返回，防止死循环。

#### 4.3.3 源码精读

**① expected 家族**（[crates/typst-syntax/src/parser.rs:L1964-L2022](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/parser.rs#L1964-L2022)）：`expect(kind)` 吃到就吃、吃不到就报 `expected` 加 kind 名；`expected_at(m, thing)` 构造一个文本为空串的 `SyntaxNode::error` 插入到标记处——注意文本为空串，所以是**零长错误**。`expected` 的注释（[parser.rs:L1991-L2009](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/parser.rs#L1991-L2009)）特别有意思：当前 token 本身是错误 token 时要**把它吃掉再继续**，否则退出当前词法模式后，增量重解析可能在错误模式下重新分词，导致「全量解析不等于增量解析」——`#import "str` 未闭合字符串就是注释里给出的例子。这是容错设计与增量编译的深度耦合点。

**② unexpected 与节点级转换**（[crates/typst-syntax/src/parser.rs:L2034-L2038](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/parser.rs#L2034-L2038) 与 [node.rs:L477-L526](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/node.rs#L477-L526)）：`unexpected()` 先 `trim_errors()` 再吃掉 token 并调节点级 `convert_to_error`（已是错误节点则保持原消息）。node.rs 侧的 `expected`/`unexpected` 还会补提示，例如把关键字误用为标识符时提示「keyword `x` is not allowed as an identifier; try `x_` instead」。

**③ 防级联**（[crates/typst-syntax/src/parser.rs:L2012-L2015](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/parser.rs#L2012-L2015) 与 [parser.rs:L2041-L2051](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/parser.rs#L2041-L2051)）：`after_error` 只看标记前一个节点的 kind 是否 `is_error()`（`SyntaxKind::is_error` 的判定见 [kind.rs:L418](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/kind.rs#L418)）；`trim_errors` 从尾部回收零长错误。

**④ 未闭合括号**（[crates/typst-syntax/src/parser.rs:L1981-L1987](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/parser.rs#L1981-L1987)）：`expect_closing_delimiter` 找不到右括号时，把**开头**的左括号节点转成 `unclosed delimiter` 错误——错误精确落在「括号打开」的位置，比报在文件末尾有用得多。

**⑤ 兜底分支**：code 模式的 `code_primary` 尾部（[crates/typst-syntax/src/parser.rs:L666-L737](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/parser.rs#L666-L737)）注释写明它会「Consume erroneous tokens for things like `#12p`, `#]`」——`#` 后跟不合法 token 时，要么 `unexpected()` 吃掉（atomic 情形），要么 `expected("expression")` 插零长错误。math 主循环（[parser.rs:L246-L262](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/parser.rs#L246-L262)）同样：不属于 `set::MATH_EXPR` 的 token 一律 `unexpected()` 吃掉继续。markup 分发（[parser.rs:L89-L136](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/parser.rs#L89-L136)）里多余的 `]` 也是 `unexpected()` 加一条提示「try using a backslash escape: \]」。

**⑥ 深度保护**（[crates/typst-syntax/src/parser.rs:L2061-L2112](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/parser.rs#L2061-L2112)）：`increase_depth` 返回一个 RAII 守卫（`defer`），离开作用域自动减一；超限时 `depth_check_error` 的注释强调「must guarantee some sort of forward progress」——每个分支至少吃一个 token，这是容错解析不死的最后防线。

**⑦ 警告也是一种树节点**：`SyntaxNode::warn`（[node.rs:L137-L168](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/node.rs#L137-L168)）把既有节点包进 `WarningWrapper`（[node.rs:L861-L941](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/node.rs#L861-L941)），例如 `**`（空粗体）会得到「no text within stars」警告但**不阻断**解析——Strong 结构照常建出来。

#### 4.3.4 代码实践（本讲核心实践）

**实践目标**：打印一段含语法错误的源码的完整 `SyntaxNode` 树，亲眼确认「错误进树、解析不中断」。

**操作步骤**：

1. 仓库已经帮我们准备好读树工具：`node.rs` 测试模块里的 `test_debug`（[crates/typst-syntax/src/node.rs:L1387-L1444](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/node.rs#L1387-L1444)）用 `format!("{:#?}", crate::parse(...))` 断言了四棵真实树。先运行它建立直觉：

```bash
cargo test -p typst-syntax test_debug
```

2. 在同一个 `mod tests` 里添加自己的测试（示例代码）：

```rust
#[test]
fn test_error_does_not_stop_parsing() {
    // `#]` 是 code_primary 注释里点名的非法组合：
    // `#` 开启代码表达式，`]` 不是合法的代码开头
    let tree = crate::parse("Hi #] there *bold*");

    println!("{tree:#?}");   // 完整树，含 Error 节点
    let (errors, warnings) = tree.errors_and_warnings();
    println!("{} errors, {} warnings", errors.len(), warnings.len());
    for e in &errors {
        println!("  - {}", e.message);
    }
}
```

3. 运行：

```bash
cargo test -p typst-syntax test_error_does_not_stop_parsing -- --nocapture
```

**需要观察的现象**：树中 `#]` 附近出现一个 `Error` 节点（来自 `code_primary` 对 atomic 情形的 `p.unexpected()` 处理，消息形如 `unexpected right bracket`，具体措辞待本地验证）；**关键在于** `there *bold*` 之后的文本与 `Strong` 结构仍然完整地出现在树里。

**预期结果**：`errors` 至少 1 条；后续 `Text` 与 `Strong` 节点照常存在；整体根节点仍是完整的 `Markup: n [...]`。作为对照，`test_debug` 里 `parse("#")` 的断言树（真实源码中的断言，可直接引用）是：

```text
Markup: 1 [
    Hash: "#",
    Error: "expected expression",
]
```

而 `parse("**")` 得到的是包着完整 `Strong` 结构的 `Warning` 节点——错误与警告都只是树上的普通居民。

#### 4.3.5 小练习与答案

**练习 1**：`expected_at` 插入的错误节点为什么文本是空串？空串错误和吃掉 token 的错误有什么分工？

**答案**：空串（零长）错误表示「这里**缺**一个东西」，不占据任何源码位置，纯粹是占位标记，用于缺分号、缺表达式等场景；吃掉 token 的错误（`unexpected`）表示「这里**多**了个不合法的东西」，错误节点携带该 token 的原文。`trim_errors` 专门回收零长错误，避免它们堆积。

**练习 2**：`expected()` 在当前 token 本身是错误 token 时为什么要吃掉它而不是只插入零长错误？

**答案**：源码注释（parser.rs:L1991-L2009）解释：若不吃掉就退出当前词法模式，之后增量重解析时这段文本可能在**markup 模式**下重新分词；例如 `#import "str` 的未闭合字符串若不被吃掉，将来补上右引号时增量解析可能不再把它当作字符串，导致全量解析与增量解析结果不一致。容错策略必须与增量编译保持一致。

**练习 3**：`depth_check_error` 为什么必须「至少吃掉一个 token」？

**答案**：深度超限的输入（如几百层括号）如果恢复逻辑不吃 token 就返回，外层循环的条件不变，会无限循环。保证前进（forward progress）是容错解析器不死循环的必要条件，函数注释明确写了这一点。

## 5. 综合实践

把本讲三个模块串成一个「预测—验证」闭环：

1. **准备输入**（可以直接把源码内联进测试，不必单独存文件）：

```typst
#heading[Title] $x^2$
#let f(x) = x * 2
#f(3) and ## oops
- item one
```

2. **手工预测**：只看源码，按 `markup_expr` 的 match 分发表写出根节点 `Markup` 的顶层孩子序列，并标注：哪个位置切到 Math 模式（`Equation` 节点）、哪个位置切到 Code 模式（`LetBinding`、函数调用节点）、`#heading[...]` 的方括号里为什么会出现 `ContentBlock`、`##` 处会出现什么样的 `Error` 节点（参考 4.3.4 中 `test_debug` 对两个连续井号的断言）。
3. **机器验证**：仿照 4.3.4，在 `node.rs` 的 `mod tests` 里写一个临时测试，把上面四行源码作为字符串传给 `crate::parse`，用 `{:#?}` 打印整棵树，与你的预测逐项对照。
4. **统计与解释**：调用 `errors_and_warnings()` 数出错误数；再用 4.1 的模式切换知识解释树里各结构节点的来历。
5. **修复对比**：把 `##` 改成转义形式（反斜杠加井号），重新打印，确认 Error 节点消失、其余子树不变——这正是下一讲增量重解析只替换局部子树的基础。

预期：预测与实际树在孩子种类上完全一致；连续两个井号会产生一个带两条 hint 的 `Error` 节点（`test_debug` 已断言其形态），转义后消失。（整棵树的具体输出待本地验证。）

## 6. 本讲小结

- Typst 用**一套** `Parser` + 模式切换实现三语法：`#`、`$`、`{}`、`[]` 四个铰链通过 `enter_modes` 在 Markup/Code/Math 间切换，「调用栈即模式栈」。
- 解析产物是**无损 CST**：`SyntaxNode` 有 Leaf/Inner/Error/Warning 四种形态，trivia 也进树；内部节点缓存 `len`/`descendants`/`diagnosis` 摘要，支撑 O(1) 查询与增量维护。
- span 编号用「区间取半留白」的 stride 方案，为增量编辑预留插入空间；新签名 `InnerNode::numberize(Option<&mut Span>, .., Option<Range<usize>>, ..)` 让「只给孩子区间重编号」成为一等操作。
- 本轮重构把**修改类**方法（`replace_children` / `renumber_children` / `update_parent`）从 node.rs 迁入 reparser.rs 的 `impl InnerNode`，`InnerNode` 提升为 `pub(super)`，node.rs 专注于构造与查询；新增的 `reparse_inner_node_mut` 不穿透警告，保证重解析的健全性。
- 解析**永不失败**：错误以零长插入、token 转换、括号定位三种方式变成树上的 `Error` 节点；`after_error`/`trim_errors` 防级联，`balanced` 标志与 `MAX_DEPTH` 深度保护守住底线，且恢复逻辑保证向前推进。
- 容错设计与增量编译深度耦合：错误 token 必须被吃掉，以保证全量解析与增量重解析结果一致。

## 7. 下一步学习建议

- 下一讲（u2-l4）**AST 视图与 Span 定位系统**：`ast.rs` 如何在这棵「无类型」CST 之上提供零成本的类型化视图（`SyntaxNode` 与 AST 节点之间的 cast 关系），以及如何用 Span 从树反查源码字节区间。建议先重读本讲 4.2 的 `LinkedNode` 一节。
- 之后（u2-l5）**增量解析与语法高亮**：`reparser.rs` 如何用本讲 4.2.3 ⑧ 的 `replace_children`/`renumber_children`（它们如今就定义在 reparser.rs 里）只重解析受影响的子树，以及「不在警告内重解析」约束的完整动机；`highlight.rs` 如何消费 `SyntaxKind`。
- 延伸阅读：crates/typst-syntax/README.md 链接的 rust-analyzer 语法设计文档，以及 Martin Haug 的增量排版论文（Section 4 讲重解析算法）。

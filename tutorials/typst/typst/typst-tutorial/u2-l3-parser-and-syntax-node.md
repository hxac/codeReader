# 语法树：Parser 与 SyntaxNode

> 承接 [u2-l2 词法与类别：Lexer 与 SyntaxKind]：上一讲我们看到 Lexer 把字符流切成一个个带 `SyntaxKind` 的 token。本讲跟随这些 token 进入 Parser，看它们如何被组装成一棵完整的语法树，以及 Typst 为什么「解析永不失败」。

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `parse` / `parse_code` / `parse_math` 三个解析入口的分工，以及 Parser 在 markup、code、math 三种模式之间切换的机制（`enter_modes` / `with_nl_mode`）。
2. 读懂 `SyntaxNode` 的节点模型：叶节点存原文文本、内部节点存孩子列表与缓存信息、错误与警告作为树的组成部分存在，以及 span 编号（numberize）如何为每个节点分配位置身份。
3. 解释 Typst 的容错设计：语法错误不抛异常、不中断，而是变成树中的 `Error` 节点，解析照常继续——这是编辑器「边打字边标红」体验的基础。
4. 亲手打印一棵含错误节点的语法树，并观察错误之后的内容仍被正常解析。

## 2. 前置知识

- **token 与词法**：Lexer 已经把源码切成了 token 序列，每个 token 有一个类别（`SyntaxKind`）。本讲只关心 Parser 如何消费它们。
- **CST 与 AST 的区别**：
  - **CST（Concrete Syntax Tree，具体语法树）**：完整保留源码中的一切——包括空格、注释、逗号。Typst 的 `SyntaxNode` 树就是 CST，所以「树 + 无损」可以直接还原出全部原文。
  - **AST（Abstract Syntax Tree，抽象语法树）**：只保留语义上有意义的部分，跳过空白与标点。Typst 没有为 AST 单独建树，而是在 CST 之上提供「懒视图」（`ast.rs`，下一讲 u2-l4 详解）。
- **递归下降解析（recursive descent）**：为每类语法结构写一个函数，函数里看当前 token 是什么就调用哪个子函数，天然递归。Typst 的 `parser.rs` 全部由这类手写函数组成，没有用解析器生成器。
- **优先级爬升（precedence climbing）**：处理 `1 + 2 * 3` 这类优先级问题时，给每个运算符一个优先级数字，解析函数带一个「最低优先级」参数，决定是否继续吞掉后面的运算符。`code_expr_prec` 与 `math_expr_prec` 都是这个套路。
- **前看（lookahead）**：解析器在决定动作前先偷看当前 token 的能力。Typst 的 Parser 恰好持有一个「当前 token」，相当于 1 个前看。
- **绿树（green tree）**：来自 rust-analyzer 的术语——节点不可变、孩子用引用计数（Rust 中是 `Arc`）共享、克隆子树代价极低。父节点不存指向孩子的反向信息之外的任何「位置」，需要父链和偏移时用 `LinkedNode` 包装（红节点）临时补上。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [crates/typst-syntax/src/parser.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/parser.rs) | 递归下降解析器本体：入口函数、各语法结构的解析函数、`Parser` 状态机、错误生成 |
| [crates/typst-syntax/src/node.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/node.rs) | `SyntaxNode` / `InnerNode` / `ErrorNode` / `LinkedNode`：树的数据结构与 span 编号 |
| [crates/typst-syntax/src/kind.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/kind.rs) | `SyntaxKind` 枚举与各类判定（`is_trivia` / `is_terminator` / `is_error`），上一讲已精读，本讲引用其判定函数 |
| [crates/typst-syntax/src/set.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/set.rs) | `SyntaxSet`：一个 `u128` 位图，Parser 用它表达「停在哪里」的停止条件 |
| [crates/typst-syntax/src/lib.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lib.rs) | `SyntaxMode` 枚举定义（Markup/Math/Code 三模式）与 crate 公开导出清单 |

## 4. 核心概念与源码讲解

### 4.1 递归下降主循环：三种模式如何切换

#### 4.1.1 概念说明

Typst 源码里有三种「方言」：markup（正文标记）、code（`#` 后的脚本）、math（`$...$` 里的公式）。同一个字符在三 种模式里含义完全不同——比如 `_` 在 markup 里是强调记号，在 code 里是匿名参数，在 math 里是下标 attachment。所以解析器必须随时知道自己「在哪种模式里」，并且能在遇到 `[`、`{`、`$`、`#` 时切换模式、递归进入子结构，结束时再切回来。

Parser 的核心工作方式可以概括为三种动作（这段总结直接来自源码文档注释）：

1. **eat（吃掉）**：把当前 token 作为一个叶节点压进 `nodes` 数组，然后让 Lexer 前进，准备下一个 token。
2. **wrap（打包）**：从之前记录的标记（marker）到 `nodes` 末尾（不含当前 token 及其后随 trivia）的所有节点打包成一个指定 `SyntaxKind` 的内部节点。
3. **出错（error）**：期望的东西没出现、或出现了不该出现的东西时，生成或转换出一个 `Error` 节点。

这套「先吃后打包」的写法让解析函数可以只管往前吃，最后一步 `p.wrap(m, Kind)` 决定这堆节点整体是什么结构——不需要预知子结构的形状。

#### 4.1.2 核心流程

一次 `typst compile` 中的解析主链路（结合 u1-l4 的 CLI 调用链）：

```text
Source 文本
   │
   ▼
parse(text)                      ← 入口：markup 模式
   │  创建 Parser(text, 0, SyntaxMode::Markup)
   ▼
markup_exprs                     ← 主循环：逐个解析 markup 表达式
   │  循环体 markup_expr(p) 按当前 token 分发：
   │    Text/Link/Label → 直接 eat
   │    Star  → strong(p)          *强调*
   │    HeadingMarker(且在行首) → heading(p)
   │    Dollar → equation(p)       进入 math 模式
   │    Hash  → embedded_code_expr(p)  进入 code 模式
   │    其它  → p.unexpected()     生成错误节点后继续
   ▼
p.finish_into(SyntaxKind::Markup)  → 根为 Markup 的 SyntaxNode
```

模式切换与停止条件的两个正交维度：

- **`SyntaxMode`（语法模式，存在 Lexer 里）**：决定 Lexer 会产出哪些 `SyntaxKind`。`enter_modes` 在进入子结构前切换、退出后还原。
- **`AtNewline`（换行模式，存在 Parser 里）**：决定「遇到换行要不要假装到了结尾」。比如标题内容遇到换行就该停（`Stop`），而列表项要求下一行缩进更深才继续（`RequireColumn`）。需要停时，Parser 把当前 token 的 kind 临时改成 `End` 制造「假结尾」，退出时再还原真实 kind。

停止条件用 `SyntaxSet`（一个 `u128` 位图）表达，比如 `syntax_set!(Star, RightBracket, End)` 表示「遇到 `*`、`]` 或结尾就停」。

#### 4.1.3 源码精读

**三个解析入口**，分别以 Markup、Code、Math 模式起跳，最终分别产出 `Markup`、`Code`、`Math` 根节点。`parse` 是编译文档时用的入口：

[crates/typst-syntax/src/parser.rs:L15-L39](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/parser.rs#L15-L39) — 定义 `parse`、`parse_code`、`parse_math`：各自创建一个对应初始模式的 `Parser`，跑一遍 `*_exprs` 主循环，再 `finish_into` 成单一根节点。

三模式枚举本体在 [crates/typst-syntax/src/lib.rs:L42-L51](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lib.rs#L42-L51)，导出于 [crates/typst-syntax/src/lib.rs:L28](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lib.rs#L28)。

**markup 主循环**：

[crates/typst-syntax/src/parser.rs:L52-L63](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/parser.rs#L52-L63) — `markup_exprs`：`while !p.at_set(stop_set)` 反复调用 `markup_expr`；`at_start` 记录「当前是否在行首」，供标题/列表记号判定；`nesting` 处理内容块里嵌套 `[` `]` 的特殊情况。

**分发中枢**：

[crates/typst-syntax/src/parser.rs:L89-L136](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/parser.rs#L89-L136) — `markup_expr`：一个 `match p.current()` 完成全部分发。注意两个细节：(1) `HeadingMarker if at_start`——同样的 `=` 不在行首就只会落到兜底分支被当成 `Text`；(2) 兜底分支 `_ => p.unexpected()`——任何认不出的 token 都不会让解析崩溃，而是生成错误节点后由外层循环继续。

**从 markup 进入 code 的跳板**：

[crates/typst-syntax/src/parser.rs:L559-L579](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/parser.rs#L559-L579) — `embedded_code_expr`：吃掉 `#` 后立刻用 `enter_modes(SyntaxMode::Code, AtNewline::Stop, ...)` 切进 code 模式；`atomic = true` 表示内嵌表达式不允许后随运算符继续组合（`#1 + 2` 里的 `+` 属于 markup 文本而非代码）；末尾检查语句是否需要分号收尾。

**模式切换的实现——「把调用栈当模式栈」**：

[crates/typst-syntax/src/parser.rs:L1791-L1806](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/parser.rs#L1791-L1806) — `enter_modes`：进入前保存 Lexer 旧模式，退出后恢复并**从上一 token 末尾重新词法分析**（同一个 token 在不同模式下可能切法不同，所以必须重切）。

[crates/typst-syntax/src/parser.rs:L1812-L1828](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/parser.rs#L1812-L1828) — `with_nl_mode`：换行模式的进/出与恢复；退出时决定当前 token 保持真实 kind 还是被标记为临时 `End`。

换行模式的五种取值与判定在 [crates/typst-syntax/src/parser.rs:L1539-L1574](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/parser.rs#L1539-L1574)：`Continue` / `Stop` / `ContextualContinue`（code 里 `else`、`.` 可跨行续接）/ `StopParBreak`（markup 里普通换行不断句）/ `RequireColumn(min_col)`（列表项靠缩进列决定续行）。

**Parser 的全部状态**：

[crates/typst-syntax/src/parser.rs:L1480-L1506](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/parser.rs#L1480-L1506) — `struct Parser`：共享的源文本、多模式 Lexer、换行模式、当前 token（唯一的前看）、`balanced` 标记、已产出的 `nodes` 向量、回溯记忆表 `memo`、递归深度 `depth`。文档注释（L1446-L1479）详细说明了 eat/wrap/error 三动作与 CST/AST 关系，非常值得整段阅读。

**吃与打包**：

[crates/typst-syntax/src/parser.rs:L1750-L1753](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/parser.rs#L1750-L1753) — `eat`：把 token 自带的现成 `SyntaxNode` 压进 `nodes`，再调 `lex` 取下一个 token。

[crates/typst-syntax/src/parser.rs:L1702-L1704](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/parser.rs#L1702-L1704) 与 [crates/typst-syntax/src/parser.rs:L1766-L1771](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/parser.rs#L1766-L1771) — `marker()` 只是记下 `nodes.len()` 这个下标；`wrap` 把 `from..to` 区间的节点 drain 出来，塞进一个新的 `SyntaxNode::inner` 再插回去——「后进先包」，树是自底向上长出来的。

**停止条件的位图实现**：

[crates/typst-syntax/src/set.rs:L7-L42](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/set.rs#L7-L42) — `SyntaxSet(u128)`：每个 `SyntaxKind` 占一位，`contains` 是一次位与；[crates/typst-syntax/src/set.rs:L51-L57](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/set.rs#L51-L57) 的 `syntax_set!` 宏让它能写成编译期常量，零运行时开销。

**优先级爬升与一处回溯**（进阶阅读，先知道有这回事即可）：

[crates/typst-syntax/src/parser.rs:L587-L661](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/parser.rs#L587-L661) — `code_expr_prec`：先解析一个主表达式（`code_primary`），再在循环里不断尝试「后随 `(` `[` → 函数调用」「`.` → 字段访问」「二元运算符 → 递归右侧操作数」，运算符优先级不够就 break。math 侧对应 [crates/typst-syntax/src/parser.rs:L270-L400](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/parser.rs#L270-L400) 的 `math_expr_prec`。

[crates/typst-syntax/src/parser.rs:L987-L1049](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/parser.rs#L987-L1049) — `expr_with_paren`：遇到 `(` 时无法确定它是括号表达式、数组、字典、参数列表还是解构，先按最常见的猜测解析，看到 `=>` 或 `=` 再回溯重解。L1010-L1024 的注释解释了为什么朴素回溯最坏要 \( O(2^n) \)（嵌套 `(x: (x: (x) => y) => y) => y` 反复重解），以及用 memo 表把复杂度压到 \( O(2n) \) 的技巧——每个括号结构至多被解析两次。

#### 4.1.4 代码实践：对照模式切换读一段源码

**实践目标**：验证 `=` 不在行首时不会被解析为标题，体会「分发 + at_start 判定」。

**操作步骤**：

1. 打开 [crates/typst-syntax/src/parser.rs:L121-L132](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/parser.rs#L121-L132)，注意 `HeadingMarker if at_start` 分支与兜底的 `Colon => p.convert_and_eat(SyntaxKind::Text)`。
2. 写两个对比文件（临时文件即可， anywhere 均可，不放进仓库）：
   - `a.typ` 内容：`= Intro`（`=` 在行首）
   - `b.typ` 内容：`some = sign`（`=` 在单词后）
3. 用已构建好的 CLI 分别编译：`cargo run -p typst-cli -- compile a.typ a.pdf` 与 `cargo run -p typst-cli -- compile b.typ b.pdf`。

**需要观察的现象**：`a.typ` 产出一级标题；`b.typ` 不报错，`= sign` 只是普通正文文本。

**预期结果**：`b.typ` 中 `=` 走不到 `heading` 分支，因为 `at_start` 为假，最终落入兜底被 `convert_and_eat(SyntaxKind::Text)` 吃成文本。具体 PDF 渲染效果「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`parse`、`parse_code`、`parse_math` 三个入口分别被什么场景使用？如果只想检查一段 `#let x = 1` 脚本的语法，该用哪个？

**答案**：`parse` 编译整个文档（markup 为根）；`parse_math` 由 `parse` 内部进入 `$` 时间接使用，也用于独立解析公式片段（如 math 模式的补全场景）；`parse_code` 用于纯脚本上下文。检查 `#let x = 1` 这种以 `#` 开头的内嵌脚本时，`parse` 即可（`#` 会经 `embedded_code_expr` 进入 code 模式）；若文本本身不含 `#`（如 `"let x = 1"`），应选 `parse_code`，因为 `let` 在 markup 模式下只是普通文本。

**练习 2**：为什么 `enter_modes` 退出时要 `lexer.jump(self.token.prev_end)` 并重新 lex 当前 token，而不是直接继续？

**答案**：因为模式决定切词。同一个字符序列在不同模式下切出的 token 不同（例如退出 code 模式后，剩下的 `+` 在 markup 里应属于 `Text`）。只有从上一个 token 的末尾重新开始、用恢复后的模式重新切词，才能保证树与「从头到尾一次性解析」的结果一致——这对增量重解析（u2-l5）的正确性至关重要。

**练习 3**：`syntax_set!(Star, RightBracket, End)` 在内存里是什么？`add`/`contains` 的开销是多少？

**答案**：一个 `SyntaxSet(u128)`，即 128 位整数里按 `SyntaxKind` 的判别值把对应位置 1。`add` 与 `contains` 都是一次位或/位与，编译期常量折叠后零运行时成本（见 [crates/typst-syntax/src/set.rs:L7-L42](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/set.rs#L7-L42)）。

### 4.2 SyntaxNode 节点模型：一棵「绿树」

#### 4.2.1 概念说明

`SyntaxNode` 是整个编译器的地基数据结构：求值（u3 单元）、高亮、IDE、增量重解析全都围着它转。它的设计要点：

1. **四种形态合一**：叶节点（存原文文本）、内部节点（存孩子）、错误节点（存出错文本 + 消息 + 提示）、警告包装节点（透明地包住另一个节点，附加警告消息）。注意：较老版本的 Typst 只有前三种，本仓库当前版本新增了 `Warning`——网上旧文章若说「只有三种」，已过时。
2. **无损**：所有叶节点文本依次拼接 = 原文；内部节点不存自己的文本，只存长度等缓存。
3. **绿树**：孩子放在 `Arc<InnerNode>` 里，克隆节点只是引用计数 +1；树不可变，共享结构免费。这对增量重解析（u2-l5）是决定性的：改动后新树可以和旧树共享未变动的绝大部分子树。
4. **span 编号（numberize）**：解析完成后给每个节点分配一个数字，父节点编号小于所有孩子、兄弟从左到右递增。这样凭编号就能在树里二分定位（`LinkedNode::find_number`），也是 u2-l1 讲过的 `Span` 的编号来源。

#### 4.2.2 核心流程

树的生长与编号：

```text
Parser 阶段（自底向上）
  eat  → SyntaxNode::leaf(kind, text)          叶：文本 + 类别
  wrap → SyntaxNode::inner(kind, children)     内部：孩子们 + 缓存
  出错 → SyntaxNode::error(message, text)      错误：文本 + 消息

InnerNode::new 时同步汇总缓存：
  len         = Σ child.len()          （字节长度）
  descendants = 1 + Σ child.descendants()
  diagnosis   = OR(child.diagnosis)    （子树是否含错/警告）

解析完成后 numberize（自顶向下分配编号区间）
  给根一个总区间，按 descendants 切分给各孩子
  目标：尽量只占区间左半，把右半留给未来的插入（增量场景）
```

编号区间分配的关键直觉：如果每次插入新节点都要给后面所有节点重新编号，增量编译就废了。所以 Typst 采用「稀疏编号」——子树编号挤在可用区间的左半边，中段留白；将来插入时直接用留白里的数字，无需重排（留白耗尽时才局部重排，见 `replace_children` 里的指数扩圈重试）。

#### 4.2.3 源码精读

**外壳与内核**：

[crates/typst-syntax/src/node.rs:L18-L25](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/node.rs#L18-L25) — `struct SyntaxNode` 只有两个字段：`data: Node` 与 `span: Span`（span 放顶层是注释里明说的性能取舍）。

[crates/typst-syntax/src/node.rs:L53-L58](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/node.rs#L53-L58) — `enum Node` 四形态：`Leaf(EcoString, SyntaxKind)` / `Inner(Arc<InnerNode>, SyntaxKind)` / `Error(Arc<ErrorNode>, SyntaxKind)` / `Warning(Arc<WarningWrapper>, SyntaxKind)`。注释（L27-L51）解释了为什么 `SyntaxKind` 要冗余存在 `Node` 里（省 8 字节 + 警告包装会复制 kind 带来的维护约束）。

**三个构造器**：

[crates/typst-syntax/src/node.rs:L109-L140](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/node.rs#L109-L140) — `leaf` / `inner` / `error`。注意 `debug_assert!(!kind.is_error())`：普通节点绝不允许携带 `Error` kind，错误必须走 `error` 构造器，这是树的不变量。

**内部节点的缓存**：

[crates/typst-syntax/src/node.rs:L640-L652](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/node.rs#L640-L652) — `InnerNode` 的五个字段：`len`（字节长）、`descendants`（子树节点总数）、`diagnosis`（是否含错/警告）、`upper`（编号区间上界）、`children`。[crates/typst-syntax/src/node.rs:L654-L668](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/node.rs#L654-L668) 的 `new` 在构造时一次性汇总前三项——之后查询零遍历。

**常用查询接口**：

[crates/typst-syntax/src/node.rs:L216-L237](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/node.rs#L216-L237) — `kind()` 与 `len()`：`len` 对叶节点取文本长度、对内部节点取缓存值，O(1)。

[crates/typst-syntax/src/node.rs:L278-L283](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/node.rs#L278-L283) — `children()`：只有内部节点有孩子；叶与错误节点返回空切片（错误节点是「文本终端」，出错原文就存在它自己身上，[crates/typst-syntax/src/node.rs:L921-L930](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/node.rs#L921-L930)）。

[crates/typst-syntax/src/node.rs:L291-L327](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/node.rs#L291-L327) — `diagnosis()` 与 `errors_and_warnings()`：前者 O(1) 判定子树有没有问题（靠缓存），后者真正遍历收集 `SyntaxDiagnostic`；且遍历时会跳过 `diagnosis` 为空的内部子树（L311 的剪枝）。

**span 编号**：

[crates/typst-syntax/src/node.rs:L672-L719](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/node.rs#L672-L719) — `InnerNode::numberize`：先算步长 `stride = space / (2 * descendants)`（L692-L694，只占左半），给本节点取区间中点作编号，再按孩子的 `descendants` 加权切分区间递归。区间塞不下时返回 `Unnumberable`，由上层换更大区间重试。

**LinkedNode：给绿树临时装上「父链与偏移」**：

[crates/typst-syntax/src/node.rs:L1068-L1077](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/node.rs#L1068-L1077) — `LinkedNode` 持有节点引用 + 父链 + 在原文中的字节偏移；[crates/typst-syntax/src/node.rs:L1101-L1103](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/node.rs#L1101-L1103) 的 `range()` 直接给出节点在源文件中的字节区间。绿树本体不存这些信息（省内存、保共享），需要时包一层就有了。

#### 4.2.4 代码实践：用 LinkedNode 遍历并核对偏移

**实践目标**：确认「叶节点文本长度之和 == 根节点 `len()`」，并学会用 `LinkedNode` 取每个节点的字节区间。

**操作步骤**（不修改任何已有源码，只新增一个示例文件）：

1. 新建 `crates/typst-syntax/examples/offsets.rs`（cargo 会自动发现 `examples/` 目录）：

   ```rust
   // 示例代码：打印每个叶节点的字节区间与文本
   use typst_syntax::{LinkedNode, Side, SyntaxNode};

   fn main() {
       let src = "= Hello *world* #counter(page)";
       let tree = SyntaxNode::leaf(typst_syntax::SyntaxKind::End, ""); // 占位防未用告警
       drop(tree);
       let root = typst_syntax::parse(src);
       println!("root len = {}", root.len(),);
       let mut leaf_sum = 0usize;
       fn walk(node: &LinkedNode, leaf_sum: &mut usize) {
           if node.children().count() == 0 {
               *leaf_sum += node.len();
               println!("{:?} @ {:?} = {:?}", node.kind(), node.range(), node.leaf_text());
           }
           for child in node.children() {
               walk(&child, leaf_sum);
           }
       }
       walk(&LinkedNode::new(&root), &mut leaf_sum);
       println!("leaf length sum = {leaf_sum}");
   }
   ```

2. 运行：`cargo run -p typst-syntax --example offsets`。

**需要观察的现象**：每个叶子一行的 `kind @ 区间 = 文本` 列表；最后两行 `root len` 与 `leaf length sum` 相等；区间从 `0..1`（`=`）开始严格首尾相接直到覆盖整个源串。

**预期结果**：`root len` == `leaf length sum` == 源文字节长度。这正是「CST 无损」的直接体现。完整输出「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `SyntaxNode` 的 `Clone` 很便宜？这给增量重解析带来什么好处？

**答案**：克隆只是复制 `Node` 枚举（内部是 `Arc` 指针）加一个 `Span`，引用计数加一而已（[crates/typst-syntax/src/node.rs:L17](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/node.rs#L17) 的 `#[derive(Clone, ...)]`）。增量重解析时，未被编辑影响的新旧两棵树可以共享绝大部分 `InnerNode`，比较与替换都只发生在局部。

**练习 2**：`InnerNode` 为什么要缓存 `diagnosis`？谁靠它加速？

**答案**：`errors_and_warnings` 收集诊断时可以整棵跳过「无错无警告」的子树（[crates/typst-syntax/src/node.rs:L311-L314](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/node.rs#L311-L314) 的剪枝）；对一份几乎全对的文档，收集错误无需真正遍历全树。

**练习 3**：`numberize` 里 `stride = space / (2 * descendants)` 的「除以 2」是为了什么？

**答案**：让本子树的编号只占可用区间的左半，右半留白。未来在子树中间插入新节点时，插入位置附近有富余编号可用，不必给后续所有节点重排编号（见 [crates/typst-syntax/src/node.rs:L689-L699](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/node.rs#L689-L699) 的注释与 u2-l5 将讲的 `replace_children` 重编号策略）。

### 4.3 容错与错误恢复：解析永不失败

#### 4.3.1 概念说明

传统编译器教学里，「语法错误」往往意味着解析函数返回 `Err` 或直接 panic。Typst 反其道而行：**`parse` 的签名返回 `SyntaxNode`，不是 `Result`**——解析在类型层面就不可能失败。所有问题都变成树里的 `Error` 节点，树始终完整、始终覆盖全部原文。

这个设计换来三个实际好处：

1. **报错不打断**：文档里第 3 行有语法错误，第 4 行以后的标题、列表照常解析，编辑器可以一次标出所有错误，而不是停在第一个。
2. **降级渲染**：即使有错，树仍可继续走求值/排版管线，错误片段显示为红波浪线而不是让整个文档编译失败（配合 u1-l5 讲过的诊断型测试）。
3. **增量友好**：错误是树的一部分，编辑后局部重解析时错误节点与其他节点一视同仁，缓存键（`spanless_eq`）不会因为「有没有错」而特殊化。

容错的核心难题是**错误恢复**：出错后从哪里继续？连续报几个错？Typst 的策略可以概括为：

- **该吃就吃**：意外 token 生成错误节点后照常吃掉，主循环前进，保证解析总是向前推进。
- **防止连锁报错**：刚报过错的位置不再立刻报第二个错（`after_error`），并清理零长度错误节点（`trim_errors`），避免「一个错引发一串错」。
- **括号感知**：`balanced` 标志记录括号是否配平；未闭合的定界符把「开括号」本身转成错误节点，防止错误扩散到结构外。

#### 4.3.2 核心流程

以 `#foo(\nMore *bold* text`（函数调用未闭合括号）为例的错误恢复路径：

```text
markup_expr 看到 Hash
  └─ embedded_code_expr：切 code 模式
       └─ code_primary: Ident("foo") → 循环中 directly_at(LeftParen)
            └─ args(p)
                 ├─ assert(LeftParen)          ← 此时记住 m2 标记
                 ├─ 换行处 AtNewline::Stop 制造假 End → 循环退出
                 └─ expect_closing_delimiter(RightParen) 失败
                      └─ nodes[m2]（那个 "("）convert_to_error("unclosed delimiter")
  ← 回到 markup：换行照旧、More/Strong/Text 全部正常解析
最终树：Markup[ Hash, FuncCall[ Ident, Error("unclosed delimiter"), Args[...] ],
           SpaceWithNewline, Text("More "), Strong[...], Text(" text") ]
```

三个关键不变量：

1. 错误节点也是 `SyntaxNode`，有 `kind() == Error`、有文本（可为空）、有消息与 hints。
2. 任何时刻叶节点文本拼接仍等于原文（错误节点保存它「吃掉」的原文）。
3. `diagnosis` 缓存让「树里有没有错」永远 O(1) 可查。

#### 4.3.3 源码精读

**错误节点本体**：

[crates/typst-syntax/src/node.rs:L132-L140](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/node.rs#L132-L140) — `SyntaxNode::error(message, text)`：消息在前、原文在后（注释特意提醒参数顺序）。错误节点数据结构在 [crates/typst-syntax/src/node.rs:L921-L930](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/node.rs#L921-L930)：`text` + `message` + `hints`（可选的用户提示，例如「试试 `\]` 转义」）。

**错误的两条生成路径**：

[crates/typst-syntax/src/parser.rs:L1991-L2022](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/parser.rs#L1991-L2022) — `expected`：想要某物而不得。若当前 token 本身就是 Lexer 产出的错误 token，则直接吃掉它（保证前进，且注释 L1996-L2003 解释了为什么必须吃：否则增量重解析可能在错误模式下切词）；否则在当前位置**插入**一个零长度 `Error` 节点（`expected_at`）。`after_error`（L2012-L2015）确保不会紧跟在错误后面再插一个零长错误。

[crates/typst-syntax/src/parser.rs:L2034-L2038](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/parser.rs#L2034-L2038) — `unexpected`：出现了不该出现的东西。先 `trim_errors` 清掉尾部零长错误（把「expected X」+「unexpected Y」两条合并成一条更清晰的信息），再把吃掉的 token 转换为错误节点。

**token 节点就地转错误**：

[crates/typst-syntax/src/node.rs:L491-L509](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/node.rs#L491-L509) — `convert_to_error` 取出原节点全文转成错误节点；`expected` 变体还能拼出「expected X, found Y」的消息，并在关键字误用作标识符时附加提示（试试 `keyword_`）。

**未闭合定界符**：

[crates/typst-syntax/src/parser.rs:L1981-L1987](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/parser.rs#L1981-L1987) — `expect_closing_delimiter`：闭括号没等到，就把标记 `open` 处的**开括号**转成 `Error("unclosed delimiter")`。错误挂在开括号上而不是闭括号位置（根本没有闭括号），用户点错误信息正好跳到需要补括号的地方。

**零长错误的清理与 `expect` 的整合**：

[crates/typst-syntax/src/parser.rs:L2041-L2051](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/parser.rs#L2041-L2051) — `trim_errors`：从尾部回溯，删掉所有零长错误节点，为合并报错做准备。

[crates/typst-syntax/src/parser.rs:L1964-L1976](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/parser.rs#L1964-L1976) — `expect`：吃掉期望的 kind，吃不到就报「expected …」；特判「期望标识符却遇到关键字」时给出带提示的更友好错误（`try `if_` instead` 一类）。

**括号配平**：

[crates/typst-syntax/src/parser.rs:L1494-L1496](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/parser.rs#L1494-L1496) — `balanced` 字段：初值 `true`，只在遇到不配对的分组定界符时变 `false`，永不回真。谁消费它？增量重解析：`reparse_markup` 只在 `p.balanced` 时才接受重解析结果（[crates/typst-syntax/src/parser.rs:L66-L84](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/parser.rs#L66-L84)），防止把残缺块当成完整块替换进树里。

**递归深度保护**：

[crates/typst-syntax/src/parser.rs:L11-L12](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/parser.rs#L11-L12) 与 [crates/typst-syntax/src/parser.rs:L2073-L2111](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/parser.rs#L2073-L2111) — `MAX_DEPTH = 256`：嵌套表达式过深时不再无限递归（防栈溢出），而是生成一个合并的 `maximum parsing depth exceeded` 错误，并保证至少吃掉一个 token 向前推进（`depth_check_error` 的注释明确要求前进性，否则解析器可能死循环）。

**真实错误样例（来自仓库自带测试，可直接当文档读）**：

[crates/typst-syntax/src/node.rs:L1505-L1544](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/node.rs#L1505-L1544) — `test_debug` 断言了三组真实输出，其中 `parse("#")` 的树是：

```text
Markup: 1 [
    Hash: "#",
    Error: "expected expression",
]
```

`parse("##")` 展示了带消息与 hints 的错误节点；`parse("**")` 展示了 `Warning` 包装节点（`no text within stars` 警告透明地包住 `Strong` 节点，不改变其结构）。这些是理解错误节点外观最可靠的参照。

#### 4.3.4 代码实践：打印含错误节点的完整语法树（本讲核心实践）

**实践目标**：亲眼验证「错误不阻断解析」——含错误源码的树里，`Error` 节点之后的内容依然被完整解析成正常节点。

**操作步骤**：

1. 新建 `crates/typst-syntax/examples/tree-dump.rs`：

   ```rust
   // 示例代码：dump 语法树 + 收集诊断
   use typst_syntax::SyntaxNode;

   fn main() {
       // 第一处错误：括号未闭合；其后是正常 markup。
       let src = "#foo(\nMore *bold* text";
       let tree: SyntaxNode = typst_syntax::parse(src);
       println!("{tree:#?}");

       let (errors, warnings) = tree.errors_and_warnings();
       println!("\nerrors = {}, warnings = {}", errors.len(), warnings.len());
       for e in &errors {
           println!("  error: {}", e.message);
       }
   }
   ```

2. 运行：`cargo run -p typst-syntax --example tree-dump`。
3. 对照运行已有单测确认 Debug 格式理解无误：`cargo test -p typst-syntax --lib test_debug`。
4. 把 `src` 换成 `"#let x = "`（表达式缺失）再跑一次，对比错误形态的差异：这次错误是**插入的零长度节点**（消息 `expected expression`），而上一次是**开括号就地转换**（消息 `unclosed delimiter`）。

**需要观察的现象**：

- 树的根仍是完整的 `Markup`，`FuncCall`（或 `LetBinding`）内部嵌着一个 `Error` 节点；
- `Error` 之后的 `SpaceWithNewline`、`Text("More ")`、`Strong` 等节点结构完好；
- `errors = 1`，且树上其余部分（`diagnosis` 为假的子树）未被真正遍历。

**预期结果**：与 4.3.2 的流程图一致——错误被「就地封装」，后续解析完全不受影响。`#let x = ` 的情形应看到 `Error: "expected expression"` 出现在 `Eq` 与空格之后。两份完整输出格式「待本地验证」，若与上述结构性描述有出入，以本地实际输出为准并回头核对对应源码分支。

#### 4.3.5 小练习与答案

**练习 1**：`expected` 在什么情况下会「吃掉」token 而不是「插入」零长错误节点？

**答案**：当前 token 本身已是错误 token（Lexer 产出的 `SyntaxKind::Error`）时（[crates/typst-syntax/src/parser.rs:L1992-L2005](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/parser.rs#L1992-L2005)）。注释给出的理由：如 `#import "str` 的未闭合字符串必须保证以 code 模式消费，否则将来补一个引号时增量重解析会把它当 markup 文本切词，导致增量结果与全量解析不一致。

**练习 2**：`parse("(1, 2"` 中那个 `(` 最终会变成什么？报错为什么挂在那里？

**答案**：`parenthesized_or_array_or_dict` 末尾的 `expect_closing_delimiter` 失败，`(` 被就地转换成 `Error("unclosed delimiter")`（见 [crates/typst-syntax/src/parser.rs:L1099](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/parser.rs#L1099) 与 L1981-L1987）。挂到开括号是因为根本没有闭括号可指，而用户修补的位置正是开括号处。

**练习 3**：如果没有 `trim_errors`，`#1 +`（假设后续 token 也不合法）这类输入会怎样？

**答案**：会先插入零长的「expected expression」错误，紧跟着又对同一个意外 token 报「unexpected …」，产生成串的重复诊断。`trim_errors`（[crates/typst-syntax/src/parser.rs:L2034-L2051](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/parser.rs#L2034-L2051)）在报「unexpected」前删除尾部零长错误，把两条合并为一条指向真实问题的信息。

## 5. 综合实践

**任务：写一个 30 行的「语法诊断报告器」示例，把本讲三个模块串起来。**

在 `crates/typst-syntax/examples/report.rs` 中实现（示例代码，全部使用 `typst-syntax` 的公开 API）：

1. **输入**：一段故意包含三类问题的源码——未闭合括号（`#image(` 后直接换行）、行中误写的标题记号（`some = sign`，预期被当文本，不报错）、以及一个真正缺失表达式的 `#let y = `。
2. **第一步（模块 4.1）**：`parse` 之后用 `LinkedNode::new(&root)` 遍历，打印每个 `SyntaxKind` 出现的次数，验证三模式协作产出的节点种类（`Markup`、`FuncCall`、`Text`、`Strong`…）。
3. **第二步（模块 4.2）**：断言 `root.len() == src.len()`；再用 `LinkedNode::range()` 把每个 `Error` 节点映射回源码字节区间，打印 `src[range]` 与错误消息。
4. **第三步（模块 4.3）**：调用 `errors_and_warnings()`，把诊断数量与自己遍历找到的 `Error` 节点数量对比——两者应相等，这就是对「诊断缓存 + 遍历剪枝」正确性的一次交叉验证。
5. **收尾**：`cargo run -p typst-syntax --example report`，再跑一遍 `cargo test -p typst-syntax --lib`，确认没有破坏 crate 自身测试。

预期现象：报告列出全部错误且每条都带可定位的原文片段；`=` 在行中不产生任何错误（它是文本）；删除 `#let y = ` 行后重跑，错误数恰好减一。完整输出「待本地验证」。

## 6. 本讲小结

- Parser 是手写递归下降解析器，`parse` / `parse_code` / `parse_math` 三个入口分别以 Markup、Code、Math 模式起跳；`markup_expr` 的 `match p.current()` 是全部分发逻辑，「行首」等状态决定同一记号的两种命运。
- 模式切换由 `enter_modes`（语法模式，存于 Lexer）与 `with_nl_mode`（换行模式，靠临时假 `End` 实现）正交管理，函数调用栈本身就是模式栈。
- `SyntaxNode` 是无损 CST「绿树」：叶存文本、内存孩子、错误与警告皆入树；`InnerNode` 缓存 `len` / `descendants` / `diagnosis` 让常用查询 O(1)；`numberize` 用稀疏编号为增量编译预留插入空间。
- 容错三件套：`expected` 插零长错误、`unexpected` 就地转错误并 `trim_errors` 合并、`expect_closing_delimiter` 把错误挂到开括号；`balanced` 与 `MAX_DEPTH` 分别守护括号配平与递归深度。
- `parse` 返回 `SyntaxNode` 而非 `Result`——错误是数据不是控制流，这是「一次标出所有错误 + 降级渲染 + 增量友好」三个特性的共同根源。
- `LinkedNode` 在不可变绿树之上按需补上父链与字节偏移，是 IDE 与诊断定位的桥。

## 7. 下一步学习建议

下一讲 **u2-l4（AST 视图与 Span 定位系统）** 将回答一个自然的问题：CST 里到处是空格、逗号、括号，求值器怎么用？答案是 `ast.rs` 在同一棵树上提供类型化的懒 AST 视图（`SyntaxNode::cast` 到 `ast::Expr` 等），配合 `Span` 反查字节区间。建议提前浏览 [crates/typst-syntax/src/ast.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/ast.rs) 的开头注释，并留意本讲 4.2.4 实践里已经用过的 `LinkedNode`——它就是下一讲「跳转定位」的底层工具。之后再进入 u2-l5 的增量重解析，看 `replace_children` 如何利用本讲的绿树共享与稀疏编号做到「只重排局部」。

# Parser 架构与入口

## 1. 本讲目标

本讲是「语法分析 Parser」单元（U4）的第一篇，目标是建立对 `typst-syntax` 解析器的**宏观架构认知**，为后续四篇（解析原语、Markup 解析、Code/Math 解析、错误恢复）铺垫骨架。读完本讲你应该能够：

- 说清 `parse` / `parse_code` / `parse_math` 三个公共入口各自选择的 `SyntaxMode` 与最终包出的根 `SyntaxKind` 的对应关系。
- 复述 `Parser` 结构体持有的 8 个状态字段，并解释每个字段为什么必须存在。
- 理解「递归下降 + marker 事件式」这一混合解析范式的直觉：先把 token 推进 `nodes`，再用 `wrap` 事后圈成子树。
- 说明 `finish` 与 `finish_into` 如何把一串节点收尾为一棵可用的语法树。
- 了解 `MAX_DEPTH` 深度上限与 `memo` 记忆化这两个保护解析器不爆炸的机制。

本讲**只看架构与入口**，不展开具体的 Markup/Code/Math 解析规则——那些是 u4-l3、u4-l4 的主题。

## 2. 前置知识

在学习本讲前，请确认你已掌握前置讲义中的以下概念（本讲会直接使用，不再重复定义）：

- **CST 与 SyntaxNode**（u1-l1、u5 系列）：解析器产出的「具体语法树」是无损的，载体是 `SyntaxNode`；它分叶子节点（leaf）和内部节点（inner）。
- **SyntaxMode 三模式**（u1-l1、u3-l2）：`Markup`（正文）、`Math`（公式）、`Code`（`#` 后的代码）。同一字符在不同模式下被 Lexer 切成不同 token。
- **SyntaxKind 词汇表**（u2-l1）：Lexer 产的 token 与 Parser 建的节点共用同一套 `SyntaxKind` 枚举。
- **Lexer 的工作方式**（u3-l1）：Lexer 是有状态迭代器，基于 `unscanny::Scanner` 游标，按 `mode` 分派，逐个产出 `(SyntaxKind, SyntaxNode)`。

两个本讲会用到的常识：

- **递归下降（recursive descent）**：为每种语法构造写一个函数，函数体内根据当前 token 决定调用哪个子函数，从而把「调用的层级」天然对应到「语法的嵌套层级」。
- **事件式解析（event-based parsing）**：不直接 `return` 一棵子树，而是先发出一串「事件」（开始节点、吃进 token、结束节点），最后由一个统一的后端把事件装配成树。这是 rust-analyzer 的 `rowan` 解析器采用的思路，typst 借鉴了它。

typst 的 Parser 是这两者的**混合体**：函数组织是递归下降的，但「装配子树」用的是 marker 事件式——下面 4.1 会详细讲。

## 3. 本讲源码地图

本讲几乎只读一个文件：

| 文件 | 作用 |
| --- | --- |
| [src/parser.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs) | 解析器全部实现：三个公共入口、`Parser` 结构体、解析原语（marker/eat/wrap）、深度限制与记忆化。 |

另外会顺手引用两个文件做交叉印证：

| 文件 | 作用 |
| --- | --- |
| [src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lib.rs) | 通过 `pub use` 把 `parse`/`parse_code`/`parse_math` 挂牌到 crate 根，是这三个入口对外可见的「闸门」。 |
| [src/kind.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs) | 定义三个根节点 kind：`Markup` / `Math` / `Code`。 |

`parser.rs` 是 typst-syntax 中**最大的单个源文件**（两千多行）。本讲只读它的「骨架」部分（文件头部的入口、`Parser` 结构体定义、以及文件末尾的原语与收尾函数），跳过中间大量具体的语法规则函数。

---

## 4. 核心概念与源码讲解

### 4.1 整体架构：递归下降 + marker 事件式解析

#### 4.1.1 概念说明

typst 的 Parser 用的是「**递归下降的函数组织** + **marker 事件式的树装配**」混合范式。这两点分开理解：

**递归下降**体现在函数命名上：`markup` 调 `markup_exprs`，`markup_exprs` 循环调 `markup_expr`，`markup_expr` 根据当前 token 分派到 `heading` / `strong` / `equation` / `embedded_code_expr` 等子函数。函数调用栈的深度，就是语法嵌套的深度。

**marker 事件式**体现在「如何把一组子节点圈成一棵子树」。传统递归下降里，解析 `*bold*` 的函数会 `return` 一个 `Strong` 节点；而 typst 的 Parser 是**先把所有子节点按顺序推进一个扁平的 `nodes: Vec<SyntaxNode>`**，等子节点都到位后，再用 `wrap(marker, kind)` 把这一段「事后」打包成父节点。`marker` 只是一个记录「从 `nodes` 的哪个下标开始」的位置戳。

为什么要这样设计？因为这样做的好处是：**解析函数不需要在入口就决定子树的边界**，遇到错误时也能把「已经吃进来的若干 token」合理地圈进一个节点里，便于错误恢复和增量重解析（reparser，U9）。这正是 CST「无损」特性得以实现的关键。

#### 4.1.2 核心流程

一次完整解析的骨架可以这样描述（伪代码）：

```
parse(text):
    p = Parser::new(text, offset=0, mode=Markup)   # 建 Parser，预取第一个 token
    markup_exprs(p, at_start=true, stop_set={End}) # 递归下降，把节点推进 p.nodes
    return p.finish_into(SyntaxKind::Markup)       # 收尾：把所有节点包成根节点
```

而 `Parser::new` 内部做的第一件事，就是向 Lexer 要第一个 token，存进 `p.token`（单 token 前瞻）。之后每次 `eat()`：把当前 token 推进 `nodes`，再向 Lexer 要下一个 token。`wrap()` 则负责事后打包。

#### 4.1.3 源码精读

解析器的整体设计意图写在 `Parser` 结构体上方的文档注释里，值得先读：

[src/parser.rs:1482-1499](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1482-L1499) —— 这段注释点明 Parser 维护两类「模式栈」：语法模式（`SyntaxMode`，存在 Lexer 里）和换行模式（`AtNewline`，决定换行是否终止当前表达式）。

marker 的定义极其简单，就是一个包着 `usize` 的 newtype：

[src/parser.rs:1596-1600](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1596-L1600) —— `Marker(usize)` 记录「`nodes` 向量的某个下标」，是后续 `wrap` 的起点。

`wrap` 把 `nodes[from..to]` 这一段抽出来，重组成一个内部节点插回原位，这就是「事后打包」的核心：

[src/parser.rs:1785-1790](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1785-L1790) —— 注意 `to = self.before_trivia().0`，即「打包到当前 token 之前的 trivia 之前」，保证 trivia 不会被错误地圈进结构节点。

`eat` 则是「吃掉当前 token 并预取下一个」：

[src/parser.rs:1769-1772](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1769-L1772) —— 用 `mem::take` 把 `token.node` 移走推进 `nodes`，再调用 `Self::lex` 取下一个 token。

#### 4.1.4 代码实践

**实践目标**：用眼睛走一遍 `strong()`（解析 `*Strong*`）的源码，体会「marker → 吃子节点 → wrap」三步曲。

**操作步骤**：

1. 打开 [src/parser.rs:137-151](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L137-L151) 的 `strong` 函数。
2. 逐行标注它做了什么：先 `marker()` 记起点 `m`，再 `assert(Star)` 吃掉开头的 `*`，接着递归 `markup(...)` 吃中间内容，`expect_closing_delimiter` 尝试吃结尾的 `*`，最后 `wrap(m, SyntaxKind::Strong)` 把这一整段圈成 `Strong` 节点。
3. 注意 `p[m].len() == 2` 这条判断：当开头 `*` 和结尾 `*` 之间没有任何内容时（总长恰好 2），会发出「no text within stars」的警告。

**需要观察的现象**：`m` 是在吃任何子节点**之前**记录的；等 `wrap` 执行时，`nodes[m..]` 已经堆满了 `Star`、中间文本、`Star`，`wrap` 把它们一次性收拢。

**预期结果**：你能用一句话复述 marker 的作用——「在开始吃子节点前埋一个位置戳，事后用 `wrap` 把戳之后的节点打包」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 typst 不让每个解析函数直接 `return SyntaxNode`，而要采用 marker + `wrap` 的事后打包？

**参考答案**：事后打包让解析函数不必在入口就承诺子树边界，遇到错误或可选结构时，可以把「已经吃进来的若干 token」灵活地圈进合适的节点；同时扁平的 `nodes` 向量便于增量重解析（只替换其中一段），也便于 `LinkedNode`（u5-l3）做带偏移的随机访问。

**练习 2**：`Marker` 内部只是一个 `usize`，它指向的是什么？

**参考答案**：指向 `Parser::nodes` 向量的一个下标，即「从第几个已解析节点开始」作为待打包子树的起点。

---

### 4.2 三个入口：parse / parse_code / parse_math

#### 4.2.1 概念说明

`typst-syntax` 对外暴露三个解析入口，分别对应三种语法模式的「顶层」：

- `parse(text)`：把整段文本当作**正文 Markup** 解析（最常用，对应一个 `.typ` 文件的顶层）。
- `parse_code(text)`：把整段文本当作**代码** 解析（对应 `{ }` 代码块的内部）。
- `parse_math(text)`：把整段文本当作**数学公式** 解析（对应 `$ $` 方程的内部）。

这三个函数都定义在 `parser.rs` 文件最顶部，并通过 [src/lib.rs:28](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lib.rs#L28) 的 `pub use self::parser::{parse, parse_code, parse_math};` 对外公开。`Parser` 结构体本身是私有的，外部只能通过这三个函数进入解析器。

#### 4.2.2 核心流程

三个入口的结构**完全对称**，都是固定的四步：

1. 开一个 `typst_timing::TimingScope` 做性能计时（便于在编译器里观测解析耗时）。
2. `Parser::new(text, 0, mode)`：从文本偏移 0、指定模式创建解析器。
3. 调用与模式对应的「顶层表达式序列」函数，传入停止集合 `syntax_set!(End)`（即只在遇到输入末尾 `End` 时停）。
4. `p.finish_into(root_kind)`：把全部节点包成一个根节点返回。

对应关系表（本讲最重要的一张表）：

| 入口函数 | `SyntaxMode` | 顶层解析函数 | 根 `SyntaxKind` | 典型输入 |
| --- | --- | --- | --- | --- |
| `parse` | `Markup` | `markup_exprs` | `Markup` | `= Hello` |
| `parse_code` | `Code` | `code_exprs` | `Code` | `let x = 1` |
| `parse_math` | `Math` | `math_exprs` | `Math` | `x^2 + 1` |

注意「模式」「顶层函数」「根 kind」三者是一一绑定的，且根 kind 的定义就在 [src/kind.rs:22](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L22)（`Markup`「A file or content block 的内容」）、[src/kind.rs:218](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L218)（`Code`「A code block 的内容」）、[src/kind.rs:79](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L79)（`Math`「方程的内容」）。

#### 4.2.3 源码精读

三个入口的实现，逐字看一遍：

[src/parser.rs:15-21](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L15-L21) —— `parse`：模式 `Markup`，调 `markup_exprs(&mut p, true, syntax_set!(End))`，注意第二个参数 `true` 是 `at_start`（标记是否处于行首，Markup 用它判断是否把 `=` 当标题），最后 `finish_into(SyntaxKind::Markup)`。

[src/parser.rs:23-29](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L23-L29) —— `parse_code`：模式 `Code`，调 `code_exprs(&mut p, syntax_set!(End))`，`finish_into(SyntaxKind::Code)`。

[src/parser.rs:31-37](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L31-L37) —— `parse_math`：模式 `Math`，调 `math_exprs(&mut p, syntax_set!(End))`，`finish_into(SyntaxKind::Math)`。

三者的「停止集合」都是 `syntax_set!(End)`——只认输入结尾为终点，这正是「顶层」的语义：顶层解析不会因为遇到某个括号就提前停。

#### 4.2.4 代码实践

**实践目标**：亲手验证上面对应关系表，确认三个入口产出的根 kind 与模式匹配。

**操作步骤**（示例代码，可在仓库外新建一个依赖 `typst-syntax` 的小 crate，或写成一个临时测试）：

```rust
// 示例代码：非项目原有，仅用于演示公共入口
use typst_syntax::{parse, parse_code, parse_math, SyntaxKind};

fn main() {
    assert_eq!(parse("= Hello").kind(),         SyntaxKind::Markup);
    assert_eq!(parse_code("let x = 1").kind(),  SyntaxKind::Code);
    assert_eq!(parse_math("x^2 + 1").kind(),    SyntaxKind::Math);
    println!("三种入口的根 kind 均符合预期");
}
```

如果你不想新建项目，也可以直接用 `Source::detached`（它内部就是 `parse`）来观察（参见 u1-l2）：

```rust
// 示例代码
use typst_syntax::{Source, SyntaxKind};
let src = Source::detached("= Hello");
assert_eq!(src.root().kind(), SyntaxKind::Markup);
```

**需要观察的现象**：三个函数返回的 `SyntaxNode` 的 `kind()` 分别是 `Markup` / `Code` / `Math`，且与传入文本是否「合法」无关——即便文本有语法错误，根 kind 仍是这三者之一（错误会以子树里的 `Error` 节点形式存在）。

**预期结果**：三行断言全部通过，印证「入口 → 模式 → 根 kind」的一一绑定。

> 说明：本实践若不便在本地搭建依赖，可改为「源码阅读型实践」——直接对照上面三段源码，口头复述每个入口的模式与根 kind，效果等同。

#### 4.2.5 小练习与答案

**练习 1**：如果用 `parse_code("= Hello")` 解析一段以 `=` 开头的文本，会发生什么？根 kind 是什么？

**参考答案**：根 kind 仍是 `SyntaxKind::Code`。但 Code 模式下 `=` 不是合法表达式的开头，Lexer/Parser 会产出 `Error` 节点（具体是 `unexpected`）。这说明根 kind 只由「入口函数」决定，与输入合法性无关。

**练习 2**：三个入口的停止集合为什么都用 `syntax_set!(End)`？

**参考答案**：因为它们解析的是「顶层」文本，唯一的合法终止条件就是读到输入末尾（`SyntaxKind::End`）。非顶层的解析（如 `markup`、`strong` 内部）会用更大的停止集合，比如遇到 `*`、`RightBracket` 就停——那是 u4-l3 的内容。

---

### 4.3 Parser 结构与状态字段

#### 4.3.1 概念说明

`Parser` 是整个解析过程的中枢，但它**对 crate 外是不可见的私有类型**（`struct Parser<'s>`，没有 `pub`）。外部只能通过三个入口函数间接创建它。理解它的字段，就理解了解析器运行时「记得什么」。

`Parser` 持有的状态可以分为三类：

- **输入与游标**：`text`（源文本）+ `lexer`（词法游标）。
- **当前视域**：`token`（正在看的那个 token，单 token 前瞻）+ `nl_mode`（换行模式）。
- **输出与防护**：`nodes`（正在累积的 CST）+ `balanced`（括号是否平衡）+ `memo`（记忆化回溯）+ `depth`（嵌套深度）。

#### 4.3.2 核心流程

`Parser` 的生命周期：

```
new(text, offset, mode)         # 建 lexer、跳到 offset、预取第一个 token 进 self.token
   ↓
[一系列 markup_exprs / code_expr ... 递归调用]   # 期间不断 eat() 推进 nodes、wrap() 打包子树
   ↓
finish() / finish_into(kind)    # 消费 self，取出 nodes 或包成根节点
```

构造时 `new` 会立刻调用 `Self::lex(...)` 把第一个 token 预取出来，保证 `self.token` 在任何解析函数入口处都已经是有效的「当前 token」。

#### 4.3.3 源码精读

`Parser` 结构体定义及其字段注释：

[src/parser.rs:1499-1525](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1499-L1525) —— 8 个字段逐条理解：

| 字段 | 类型 | 作用 |
| --- | --- | --- |
| `text` | `&'s str` | 源文本，与 Lexer 共享同一份引用。 |
| `lexer` | `Lexer<'s>` | 词法游标，内含 `SyntaxMode`。 |
| `nl_mode` | `AtNewline` | 换行模式：换行是否终止当前表达式。 |
| `token` | `Token` | 当前 token，**单 token 前瞻**；`wrap` 时不包含它。 |
| `balanced` | `bool` | 定界符是否平衡，只能从 `true` 变 `false`（单向）。 |
| `nodes` | `Vec<SyntaxNode>` | 正在累积的 CST；Code/Math 模式下含 trivia，但不含 `token`。 |
| `memo` | `MemoArena` | 记忆化回溯用的检查点（仅括号场景，见 4.5）。 |
| `depth` | `u32` | 当前表达式嵌套深度，配合 `MAX_DEPTH` 防爆。 |

`token` 字段的细化结构（`Token` 结构体）也很关键，它不仅是 kind，还缓存了 trivia 信息：

[src/parser.rs:1530-1545](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1530-L1545) —— 注意 `n_trivia`（前置 trivia 个数）、`newline`（前置 trivia 是否含换行）、`start`/`prev_end`（文本偏移），这些让 Parser 不必反复回问 Lexer 就能做换行判定与 trivia 处理。

`new` 的实现：

[src/parser.rs:1620-1636](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1620-L1636) —— 建一个 `Lexer::new(text, mode)`，`lexer.jump(offset)` 跳到起始偏移（增量重解析时会用到非 0 偏移），`nl_mode` 默认 `Continue`，然后 `Self::lex` 预取首个 token，`balanced` 初始为 `true`，`depth` 初始为 0。

> 关于 `nl_mode`：`AtNewline` 是一个枚举，决定「遇到换行要不要假装遇到了 `End`」。这是 u4-l5（新行处理与错误恢复）的主题，本讲只需知道它存在并被存为 Parser 状态即可。其定义见 [src/parser.rs:1558-1571](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1558-L1571)。

#### 4.3.4 代码实践

**实践目标**：通过阅读源码，把 `Parser` 的 8 个字段归到「输入/视域/输出防护」三类。

**操作步骤**：

1. 打开 [src/parser.rs:1499-1525](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1499-L1525)。
2. 画一张三栏表，把 `text`/`lexer`/`nl_mode`/`token`/`nodes`/`balanced`/`memo`/`depth` 填进去。
3. 对每个字段写一句话：「如果没有这个字段，Parser 会丢失什么信息？」

**需要观察的现象**：你会发现每个字段都不可省略——例如去掉 `token` 就没有前瞻能力，去掉 `depth` 就无法防无限递归，去掉 `memo` 在嵌套括号场景会退化到指数时间。

**预期结果**：你能脱表复述 8 个字段的职责。无需运行命令。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `text` 字段是 `&'s str` 而不是 `String`？

**参考答案**：解析过程不修改源文本，借用（共享引用）即可，避免一次无谓的拷贝；生命周期 `'s` 保证文本在 Parser 整个生命周期内有效。Lexer 也持有同一份引用。

**练习 2**：`balanced` 字段为什么注释说「只能从 `true` 变 `false`」？

**参考答案**：一旦发现定界符不配对（比如多了一个 `)`），`balanced` 就被置为 `false`，之后不会因任何理由再变回 `true`。这个单向标志主要供增量重解析（reparser）判断「这次重解析是否仍平衡」，从而决定是否回退到全量解析（见 u9-l2）。

---

### 4.4 收尾：finish 与 finish_into

#### 4.4.1 概念说明

解析完所有文本后，需要把 Parser 内部那串扁平的 `nodes` 「收尾」成对外可用的形态。这里有两个收尾函数：

- `finish(self) -> Vec<SyntaxNode>`：直接把累积的节点向量交出去（裸的、尚未有根节点包裹）。
- `finish_into(self, kind) -> SyntaxNode`：断言已经读到末尾，然后把所有节点包进**一个根内部节点**，返回完整的树。

三个公共入口都用 `finish_into`，因为外部调用者期望拿到「一棵完整的树」（一个根节点）。`finish` 则主要被 `reparser` 等内部场景使用（增量重解析时只要那一小段节点序列，不需要根包裹）。

#### 4.4.2 核心流程

```
finish_into(kind):
    assert!(self.at(SyntaxKind::End))        # 必须已读到末尾
    return SyntaxNode::inner(kind, self.finish())  # 把 finish() 的 Vec 包成根节点
```

`SyntaxNode::inner(kind, children)` 是构造「内部节点」的标准方式（见 u5-l2），它把一组子节点收进一个父节点。所以「整棵 CST」其实就是一个 `inner` 节点，其 children 是顶层的一系列表达式。

#### 4.4.3 源码精读

[src/parser.rs:1639-1641](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1639-L1641) —— `finish`：消费 `self`，直接返回 `self.nodes`。

[src/parser.rs:1644-1647](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1644-L1647) —— `finish_into`：先 `assert!(self.at(SyntaxKind::End))`（确保 token 流已耗尽），再用 `SyntaxNode::inner(kind, self.finish())` 把全部节点包成根节点。注意它内部就调了 `finish()`。

这个 `assert` 是一道安全网：如果某条解析路径在没读完文本时就提前收尾，这里会 panic，帮助开发者发现解析逻辑 bug，而不是默默吐出一棵残缺的树。

作为对比，`reparse_markup`（增量重解析的 markup 钩子）用的是 `finish()` 而非 `finish_into`：

[src/parser.rs:64-82](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L64-L82) —— 末行 `(p.balanced && p.current_start() == range.end).then(|| p.finish())`，只在「括号平衡且刚好解析到编辑范围末尾」时，才用 `finish()` 取出节点序列（不要根包裹）。这是 `finish` 的典型用场，细节留到 U9。

#### 4.4.4 代码实践

**实践目标**：理解 `finish_into` 产出的「根节点」与它的 children 的关系。

**操作步骤**：

1. 阅读上面两段源码，确认 `finish_into` = `inner(kind, finish())`。
2. 写一段示例（沿用 4.2 的用法），解析 `"= Hello\n- item"`，打印根节点的 kind 与 children 数量：

```rust
// 示例代码
use typst_syntax::parse;
let root = parse("= Hello\n- item");
println!("根 kind = {:?}, 子节点数 = {}", root.kind(), root.children().count());
```

**需要观察的现象**：根 kind 是 `Markup`，children 包含 `Heading`、`ListItem` 等顶层表达式（以及可能的 trivia）。根节点本身只是个「容器」。

**预期结果**：你看到根节点的 children 不是单个 token，而是已经 wrap 好的子树——印证了「整棵树 = 一个根 inner 节点 + 一串子树」。

> 若本地无法运行，可改读 [src/parser.rs:15-21](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L15-L21) 的 `parse`，确认它最后一步 `finish_into(SyntaxKind::Markup)` 就是把 `markup_exprs` 推进来的所有节点包成根。

#### 4.4.5 小练习与答案

**练习 1**：`finish_into` 里的 `assert!(self.at(SyntaxKind::End))` 如果被去掉，会有什么风险？

**参考答案**：如果某条解析路径在文本尚未耗尽时就调 `finish_into`，会默默返回一棵**只覆盖了部分文本**的树，错误被隐藏到下游（求值/渲染）才暴露，难以定位。`assert` 让这种解析逻辑 bug 在解析阶段就 panic 显形。

**练习 2**：为什么 `reparse_markup` 用 `finish()` 而不是 `finish_into()`？

**参考答案**：增量重解析只需要「被编辑影响的那一小段」的节点序列，用来替换原树中对应位置，不需要也不应该给它套一个根节点；它还要自行判断 `balanced` 与范围是否对齐。而顶层入口需要一棵完整的树，所以用 `finish_into`。

---

### 4.5 MAX_DEPTH 深度限制与 memo 记忆化

#### 4.5.1 概念说明

解析器有两类「爆炸」风险，typst 各自设了一道防线：

**风险一：无限/过深递归导致栈溢出。** 恶意或手误的输入（比如几千层嵌套括号 `((((...))))`）会让递归下降的调用栈无限加深，最终栈溢出崩溃。防线是 `MAX_DEPTH`：每个表达式嵌套一层，`depth` 计数加 1，超过上限就不再下钻，而是产出一个「maximum parsing depth exceeded」错误节点。

**风险二：猜测-回溯导致指数级时间。** 在带括号的代码表达式（如 `(x: (x: (x) => y) => y) => y`）里，Parser 无法一次性判断括号是数组、字典、参数列表还是解构——它得先猜一种，猜错就回滚重试。朴素回溯在最坏情况下会让同一个位置被反复解析，时间复杂度退化为：

\[
\text{最坏时间} = O(2^n) \quad \text{（n 为嵌套深度）}
\]

防线是 `memo`（记忆化）：把「在某位置已正确解析出的结果」缓存起来，下次再走到同一位置就直接复用。加上记忆化后，最坏时间降到：

\[
\text{最坏时间} = O(2n)
\]

即每个括号表达式最多被解析两次。这是 packrat parsing 的思路，但 typst 只在括号场景局部使用。

#### 4.5.2 核心流程

**深度限制**的检查发生在两个层次：

```
表达式序列函数（markup_exprs / code_exprs / math_exprs）入口：
    check_depth_until(stop_set)   # 超 MAX_DEPTH 则产合并错误并跳过整段

单个表达式函数（markup_expr / code_expr_prec / math_expr_prec）入口：
    increase_depth()              # depth+=1，返回一个 drop 时 depth-=1 的守卫
```

`increase_depth` 返回的守卫用 `typst_utils::defer` 实现：进入函数时 `+1`，函数返回（守卫被 drop）时自动 `-1`，保证深度计数与调用栈严格同步。

**记忆化**的流程（在 `expr_with_paren` 里）：

```
1. restore_memo_or_checkpoint()：
   - 若该位置已被 memo 命中 → 直接复用 arena 里缓存的节点，return。
   - 否则 → 建一个 checkpoint（保存 lexer 游标、模式、token、nodes 长度）。
2. 先按「最可能」的猜测解析（括号表达式 / 数组 / 字典）。
3. 若后面跟 '=>' 或 '=' → 说明猜错了，restore(checkpoint) 回滚，按参数列表/解构重解析。
4. memoize_parsed_nodes()：把「这次正确解析的结果」连同状态存进 memo，供以后复用。
```

#### 4.5.3 源码精读

`MAX_DEPTH` 的定义带着一句诚实的注释：

[src/parser.rs:12-13](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L12-L13) —— `const MAX_DEPTH: u32 = 256;`，注释 `// Picked by gut feeling.`（凭直觉选的）。这是一个工程上的经验阈值。

两个检查函数：

[src/parser.rs:2080-2087](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L2080-L2087) —— `check_depth_until`：未超限返回 `Some(&mut self)`，超限则调 `depth_check_error` 并返回 `None`（让调用方提前 return）。

[src/parser.rs:2092-2100](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L2092-L2100) —— `increase_depth`：未超限则 `self.depth += 1` 并返回 `Some(defer(self, |p| p.depth -= 1))`（守卫 drop 时自减）；超限则报错返回 `None`。`defer` 让「配对增减」不需要手写 `drop`。

超限时的错误恢复（关键在于「必须保证向前推进」以免死循环）：

[src/parser.rs:2103-2128](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L2103-L2128) —— `depth_check_error`：用 `balance` 计数器跟踪开/闭括号，**至少吃掉一个 token** 保证前进，吃到「括号平衡且命中 stop_set」或末尾为止，最后 `wrap_error(m, "maximum parsing depth exceeded")` 把这段圈成一个错误节点。

记忆化的存储结构：

[src/parser.rs:1889-1902](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1889-L1902) —— `MemoArena`：一个统一的 `arena: Vec<SyntaxNode>`（减少反复分配）+ 一个 `memo_map: FxHashMap<MemoKey, (Range<usize>, PartialState)>`。键 `MemoKey` 就是当前 token 的文本起始偏移（[src/parser.rs:1907](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1907)）。

记忆化的使用现场（带详细注释，强烈推荐阅读）：

[src/parser.rs:1006-1068](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1006-L1068) —— `expr_with_paren`：先 `restore_memo_or_checkpoint()`，再猜括号表达式，猜错（后面跟 `Arrow`/`Eq`）则 `restore(checkpoint)` 重解析为参数列表或解构赋值，最后 `memoize_parsed_nodes(memo_key, prev_len)`。注释 [src/parser.rs:1031-1043](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1031-L1043) 把「为什么不会指数爆炸」讲得很清楚。

#### 4.5.4 代码实践

**实践目标**：亲手触发 `MAX_DEPTH` 错误，观察错误恢复行为。

**操作步骤**（示例代码）：

```rust
// 示例代码：构造 300 层嵌套括号，超过 MAX_DEPTH=256
use typst_syntax::parse_code;

fn main() {
    let deep = "(".repeat(300) + &")".repeat(300) + &"1".to_string();
    // 注意：以上字符串拼接仅作示意，实际需要用 String::from + push_str 拼装
    let root = parse_code(&format!("({}1{})", "(".repeat(300), ")".repeat(300)));
    // 遍历找出错误节点
    for desc in root.descendants() {
        if desc.kind().is_error() {
            println!("遇到错误节点: {:?}", desc.kind());
        }
    }
}
```

**需要观察的现象**：即便嵌套远超 256 层，解析也不会栈溢出崩溃，而是在某处产出 `Error` 节点（消息含 「maximum parsing depth exceeded」），并且后续的闭括号仍被尽量平衡地吃掉。

**预期结果**：解析正常返回一棵树（不 panic），树里能找到错误节点。**待本地验证**：具体在第几层触发、错误节点的精确文本范围，建议你在本地断点或加日志确认，因为我未实际运行上述命令。

> 源码阅读型替代实践：直接读 [src/parser.rs:2103-2128](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L2103-L2128) 的 `depth_check_error`，画出它「至少吃一个 token + 跟踪括号平衡 + 命中 stop_set 即停」的流程，并解释为什么必须保证向前推进。

#### 4.5.5 小练习与答案

**练习 1**：`increase_depth` 返回的「守卫」为什么用 `defer` 而不是在函数末尾手动 `self.depth -= 1`？

**参考答案**：因为解析函数可能有多个提前返回路径（遇到 `?`/`None` 等），手写减一很容易漏掉某条路径导致计数失准。`defer` 把减一绑定到守卫的 `Drop`，无论函数从哪条路径返回（包括 panic 解栈），计数都会正确回退。

**练习 2**：如果没有 `memo`，输入 `(x: (x: (x) => y) => y) => y` 会发生什么？

**参考答案**：每一层括号都会被「先猜成数组/字典、发现 `=>` 后回滚重解析成参数列表」，回滚导致外层重新解析内层，嵌套 n 层就会产生 \(O(2^n)\) 次重复解析。`memo` 保证同一位置最多解析两次，降到 \(O(2n)\)。

**练习 3**：`MemoArena` 为什么只用在括号场景（`expr_with_paren`），而不是全局启用 packrat？

**参考答案**：全局 packrat 的空间开销大且会改变解析的时间特性。typst 只有括号这一处存在「必须猜测才能解析」的歧义，其余语法都能用单 token 前瞻确定性地决策，因此只在局部启用记忆化，以最小代价消除指数回溯。

---

## 5. 综合实践

把本讲的所有知识串起来，完成下面这个「入口追踪」任务：

**任务**：以 `parse("#let f(x) = x + 1\n= Title\n$x^2$")` 为对象，回答以下问题，并尽量对照源码佐证。

1. **入口与模式**：这个调用走的是哪个入口函数？它选择的 `SyntaxMode` 是什么？为什么不能用 `parse_code`？（提示：文本里有 `= Title` 这种 Markup 语法。）
2. **根 kind**：返回的 `SyntaxNode` 的 `kind()` 是什么？这个 kind 是在哪一行代码里被确定的？（提示：看 [src/parser.rs:15-21](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L15-L21) 的 `finish_into`。）
3. **状态字段追踪**：解析过程中，`Parser` 的 `depth` 字段在进入 `embedded_code_expr`（解析 `#let ...`）时会怎样变化？为什么会自动回退？（提示：`increase_depth` + `defer`。）
4. **模式切换**：`#` 触发了从 Markup 到 Code 的切换；`$` 又触发了到 Math 的切换。这些切换存在 Parser 的哪个字段里？（提示：不在 Parser，而在 `lexer` 内部的 `SyntaxMode`，通过 `enter_modes` 切换。）
5. **收尾**：解析结束后，`finish_into` 内部的 `assert` 检查的是什么？如果不通过会怎样？

**完成方式**：

- 阅读型：对照本讲引用的源码片段逐题作答。
- 运行型（可选）：用 `Source::detached` 解析这段文本，遍历 `root().descendants()` 打印每个节点的 kind，验证你对「根是 Markup、内含 LetBinding/Heading/Equation」的判断。

这个任务把你学到的「入口 → 模式 → Parser 字段 → 深度守卫 → 收尾」整条链路都过了一遍，为下一讲（u4-l2 解析原语）打好基础。

## 6. 本讲小结

- typst 的 Parser 是**递归下降 + marker 事件式**的混合体：函数按语法嵌套递归调用，但子树用「先推进 `nodes`、再用 `wrap(marker, kind)` 事后打包」的方式装配，利于错误恢复与增量重解析。
- 三个公共入口 `parse` / `parse_code` / `parse_math` 结构完全对称，各自绑定一个 `SyntaxMode`（Markup/Code/Math）、一个顶层解析函数、一个根 `SyntaxKind`，通过 `lib.rs` 的 `pub use` 对外公开。
- `Parser` 持有 8 个状态字段：`text`/`lexer`（输入）、`token`/`nl_mode`（视域）、`nodes`/`balanced`/`memo`/`depth`（输出与防护），其中 `token` 提供「单 token 前瞻 + 缓存 trivia」。
- `finish` 取出裸节点向量，`finish_into(kind)` 断言已到末尾并包成根 `inner` 节点；三个入口用后者，`reparser` 用前者。
- `MAX_DEPTH = 256` 配合 `increase_depth`/`check_depth_until` 防止栈溢出，超限时 `depth_check_error` 保证向前推进并产出合并错误。
- `memo`（`MemoArena`）只在括号场景局部启用 packrat 式记忆化，把猜测-回溯的最坏时间从 \(O(2^n)\) 降到 \(O(2n)\)。

## 7. 下一步学习建议

本讲只看了 Parser 的「骨架」，没有展开任何具体的解析规则。接下来建议：

1. **u4-l2 Marker、Token 与解析原语**：深入 `marker`/`before_trivia`/`eat`/`at`/`current`/`wrap`/`convert_and_eat` 等原语，理解「单 token 前瞻 + trivia 分离」的精妙设计。本讲的 4.1 已经埋了伏笔。
2. **u4-l3 Markup 解析**：看 `markup_exprs` / `markup_expr` 如何解析标题、列表、加粗等正文结构，以及 `at_start` 标志的作用。
3. **u4-l4 Code 与 Math 解析**：看 `code_expr_prec` 的优先级爬升（Pratt）与 `math_expr_prec` 的算符优先级，理解本讲提到的 `expr_with_paren` 全貌。
4. **u4-l5 新行处理与错误恢复**：本讲多次提到的 `AtNewline` 模式与 `unexpected`/`hint`/`trim_errors` 都在那里展开。

读完整个 U4，你就能完整解释「一段 Typst 文本是如何变成一棵 CST 的」。

# Marker、Token 与解析原语

## 1. 本讲目标

本讲是「语法分析 Parser」单元（U4）的第二篇，承接 u4-l1 建立的「递归下降 + marker 事件式」宏观架构，**下钻到解析器最底层的几块积木**：位置戳 `Marker`、单 token 前瞻 `Token`、换行模式 `AtNewline`，以及 `eat` / `at` / `current` / `marker` / `wrap` / `convert_and_eat` 等解析原语。

读完本讲你应该能够：

- 说清 `Marker` 是什么、为什么它只是一个 `usize`、以及它如何让解析函数「先解析子节点、再事后圈成子树」。
- 复述 `Token` 结构体的 6 个字段，并解释「单 token 前瞻 + 前置 trivia 已落入 `nodes`」这一略显反直觉的设计。
- 区分 `marker()` 与 `before_trivia()` 两个位置戳的差别，理解 trivia（空白/注释）归属哪一层子树。
- 看懂 `eat` / `at` / `current` / `convert_and_eat` / `wrap` / `flush_trivia` 各自做的一件事，并能用它们组合描述任意一段解析步骤。
- 理解 `AtNewline` 如何在换行处「伪造一个 `End`」来终止当前表达式，从而让换行参与语法判定。

本讲**只讲原语本身**，不展开 Markup / Code / Math 的具体语法规则（那是 u4-l3、u4-l4 的主题），也不展开错误恢复细节（u4-l5）。但本讲会以 `strong()`（解析 `*bold*`）作为贯穿示例，因为它把 `marker → eat → wrap` 三步用得最干净。

## 2. 前置知识

学习本讲前，请确认你已掌握前置讲义中的以下概念（本讲直接使用，不再重新定义）：

- **CST 与 SyntaxNode**（u1-l1、u5 系列）：解析器产出的具体语法树是无损的，载体是 `SyntaxNode`，分叶子节点（leaf）与内部节点（inner）。内部节点 `SyntaxNode::inner(kind, children)` 把一串子节点圈成一棵子树。
- **SyntaxKind 词汇表**（u2-l1）：Lexer 产的 token 与 Parser 建的节点共用同一套 `SyntaxKind` 枚举；`is_trivia()` 判定一个 kind 是否是可跳过的碎片（空白、行/块注释等）。
- **Parser 宏观架构**（u4-l1）：Parser 是私有结构体，持有一个扁平的 `nodes: Vec<SyntaxNode>`；解析函数先把 token 推进 `nodes`，再用 `wrap` 事后打包成内部节点。Parser 持有 8 个状态字段，其中本讲重点关注 `token`、`nodes`、`nl_mode`。
- **Lexer 的工作方式**（u3-l1）：Lexer 是有状态迭代器，`lexer.next()` 返回 `(SyntaxKind, SyntaxNode)`，并带一个 `newline()` 标记表示上个 token 是否含换行。

两个本讲会用到的常识：

- **前瞻（lookahead）**：解析器在决定「接下来调用哪个子函数」之前，需要先「偷看」下一个 token 但不消费它。typst 的 Parser 只做**单 token 前瞻**——只看当前这一个 token，不做任意长度的回溯（唯一例外是括号场景的 packrat 记忆化，见 u4-l1）。
- **trivia**：指空白、注释等对语法结构无意义、可以跳过的 token 碎片。它们仍要进入 CST（因为 CST 无损），但不参与语法判定。

> 提醒：本讲涉及的所有类型（`Marker`、`Token`、`AtNewline`、`Parser`）都是 **crate 内部私有**的（`struct` 不带 `pub`，或仅 `pub(super)`）。外部代码无法直接构造它们，只能通过 `lib.rs` 公开的 `parse` / `parse_code` / `parse_math` 三个入口间接使用。因此本讲的「代码实践」以**源码阅读 + 通过公开 `parse` 观察产物**为主。

## 3. 本讲源码地图

本讲几乎只读一个文件：

| 文件 | 作用 |
| --- | --- |
| [src/parser.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs) | 解析器全部实现。本讲聚焦其中三类定义：`Marker` / `Token` / `AtNewline` 三个数据结构，`current` / `at` / `marker` / `before_trivia` / `eat` / `convert_and_eat` / `wrap` / `flush_trivia` 等原语方法，以及产出 `Token` 的 `lex` 函数和贯穿示例 `strong()`。 |

另外引用两个文件做交叉印证：

| 文件 | 作用 |
| --- | --- |
| [src/node.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs) | 提供 `SyntaxNode::inner` / `leaf` 构造器、`convert_to_kind` 方法（`convert_and_eat` 的底层），以及一个打印 `**` 解析结果的测试 `test_debug`，用于验证我们对 `strong()` 的分析。 |
| [src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lib.rs) | `pub use` 把 `parse` 挂牌到 crate 根，是实践环节唯一可用的解析入口。 |

阅读建议：本讲的代码点集中在 `parser.rs` 的 **1499–1887 行**（`Parser` 结构体字段、`Token`/`AtNewline`/`Marker` 定义、原语方法、`lex` 函数）和文件头部的 **137–151 行**（`strong()` 示例）。建议先把这两段对照着读一遍。

---

## 4. 核心概念与源码讲解

### 4.1 Marker：圈子树的「位置戳」

#### 4.1.1 概念说明

`Marker` 解决的问题是：**如何在「还没确定子树边界」的情况下，先记下一个起点，等子节点全部解析完之后再回来把这一段圈成一棵子树？**

传统递归下降解析器里，解析 `*bold*` 的函数会这样写（概念伪代码）：

```text
fn strong() -> Node {
    eat(star)                    // 吃掉开头的 *
    let children = parse_markup() // 解析中间内容，返回一棵子树
    eat(star)                    // 吃掉结尾的 *
    return Node::Strong(children) // 把结果打包返回
}
```

这种写法要求函数**入口就知道自己要产出一个 `Strong` 节点**。但 typst 的 Parser 不这么做——它先把所有子节点（包括两个 `*` 和中间的文本）**按顺序推进一个扁平的 `nodes: Vec<SyntaxNode>`**，等全部到位后再用 `wrap` 把 `nodes` 里某一段「事后」打包成 `Strong`。

要做这件事，就需要一个「位置戳」记录「从 `nodes` 的哪个下标开始打包」。这就是 `Marker`。

#### 4.1.2 核心流程

`Marker` 的定义极其朴素——它就是一个包装了 `usize` 的新类型（newtype）：

```rust
// 一个标记，代表节点在解析器中的位置。主要用于 wrap，也可作为下标访问节点，如 p[m]。
#[derive(Debug, Copy, Clone, Eq, PartialEq)]
struct Marker(usize);
```

来源：[src/parser.rs:1596-1600](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1596-L1600)——`Marker` 就是一个 `usize`，派生了 `Copy`，所以可以随意按值复制，无需生命周期。

它有两个用途，对应两段代码：

1. **作为 `wrap` 的起点**：`wrap(from: Marker, kind)` 把 `nodes` 中从 `from` 到当前位置的一段抽出来，包成一个内部节点。这是它的主要用途（见 4.4.3）。
2. **作为下标访问 `nodes`**：Parser 实现了 `Index<Marker>` / `IndexMut<Marker>`，于是可以写 `p[m]` 直接读写那个位置上的节点：

```rust
// 用 marker 作为下标访问 parser。
impl Index<Marker> for Parser<'_> {
    type Output = SyntaxNode;
    fn index(&self, m: Marker) -> &Self::Output {
        &self.nodes[m.0]
    }
}
```

来源：[src/parser.rs:1603-1615](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1603-L1615)——`p[m]` 等价于 `p.nodes[m.0]`。`strong()` 在打包后用 `p[m].len()` 和 `p[m].warn(...)` 就是靠这个下标访问。

于是典型的「marker → 解析子节点 → wrap」三步，可以画成下面这张时序图（以 `*hi*` 为例，假设没有前后空白）：

```text
步骤                    nodes 状态              说明
─────────────────────────────────────────────────────────────────
let m = p.marker();     [ ]                     记下起点 m = 0
p.assert(Star);         [ Star ]                吃掉开头 *
parse 中间内容           [ Star, Text ]          吃掉 "hi"
p.eat closing Star;     [ Star, Text, Star ]    吃掉结尾 *
p.wrap(m, Strong);      [ Strong[Star,Text,Star] ]  把 [0..3) 圈成 Strong
```

注意第 1 步 `marker()` 取的是**当时** `nodes.len()` 的值（一个快照），所以即使后续 `nodes` 不断增长，`m` 仍指向当初那个位置。这正是「位置戳」的含义。

#### 4.1.3 源码精读

`marker()` 方法本身只有一行，返回「当前 `nodes` 的长度」作为新节点的落点：

```rust
/// 一个 marker，在该 token 被 eat 之后会指向解析器中的当前 token。
fn marker(&self) -> Marker {
    Marker(self.nodes.len())
}
```

来源：[src/parser.rs:1719-1723](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1719-L1723)——为什么是 `nodes.len()`？因为当前 token 还没被 eat（还在 `token` 字段里），它一旦被 eat 就会 `push` 到 `nodes` 末尾，落点正好是现在的 `nodes.len()`。所以 `marker()` 指向的是「当前 token 即将落入的位置」。

`strong()` 函数把这三步用得最清楚，建议逐行对照：

```rust
fn strong(p: &mut Parser) {
    p.with_nl_mode(AtNewline::StopParBreak, |p| {
        let m = p.marker();                              // ① 记下起点
        p.assert(SyntaxKind::Star);                      // ② 吃掉开头的 *
        markup(p, false, true, syntax_set!(Star, RightBracket, End)); // ③ 解析中间内容
        let had_closing = p.expect_closing_delimiter(m, SyntaxKind::Star); // ④ 吃掉结尾的 *
        p.wrap(m, SyntaxKind::Strong);                   // ⑤ 把 [m..现在) 圈成 Strong
        if had_closing && p[m].len() == 2 {
            p[m].warn("no text within stars");           // ⑥ 用 p[m] 访问刚建的节点
            ...
        }
    });
}
```

来源：[src/parser.rs:137-151](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L137-L151)——注意第 ① 步 `marker()` 在第 ② 步 `assert(Star)` **之前**调用。因为 `assert` 会吃掉 `*`，必须在吃之前记下它的落点，否则 `m` 就会指向 `*` 之后。第 ⑥ 步的 `p[m]` 靠的就是上面看到的 `Index<Marker>` 实现——`wrap` 之后，原本 `nodes[m.0]` 位置上的节点已经变成了那个新的 `Strong` 内部节点，所以 `p[m]` 现在就是 `Strong`。

> **为什么 typst 要用这种「事后圈」的方式？** 因为它让解析函数**不必在入口承诺子树边界**。考虑错误恢复：遇到 `*unclosed`（没有结尾 `*`）时，传统写法很难优雅地「反悔」已经返回的子树；而 marker 方式下，子节点本就平铺在 `nodes` 里，`expect_closing_delimiter` 发现缺失时只需把 `nodes[m.0]` 处的节点原地转成错误节点（见 4.4.3 的 `convert_to_error`），整个流程仍然向前推进。这一点 u4-l5 会展开。

#### 4.1.4 代码实践

**实践目标**：亲手追踪一次 `strong()` 的执行，验证 `marker → eat → wrap` 三步如何把 `*hi*` 变成一棵 `Strong` 子树。

**操作步骤**：

1. 打开 [src/parser.rs:137-151](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L137-L151) 的 `strong()`，照着 4.1.2 的时序图，在纸上画出对 `*hi*` 每一步执行后 `nodes` 的状态。
2. 打开 [src/node.rs:1488-1543](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1488-L1543) 的 `test_debug` 测试，阅读它对 `**`（两个连续星号）的断言。
3. 模仿该测试，在仓库内新建一个临时测试（或直接在 `node.rs` 的 `mod tests` 里临时加一个 `#[test]`），用 `crate::parse("*hi*")` 解析，并 `println!("{{tree:?}}")` 或断言它的 `{:#?}` 输出。

**需要观察的现象**：

- `*hi*` 解析后根 `Markup` 下应有一个 `Strong` 节点，其子节点依次是 `Star "*"`、`Markup[Text "hi"]`、`Star "*"`——与 4.1.2 时序图的最终状态一致。
- `**` 解析后应是 `Warning` 包裹一个 `Strong: 2 [Star, Markup: 0, Star]`，正好印证 4.1.3 第 ⑥ 步：`p[m].len() == 2`（两个星号、中间空）触发了 `warn("no text within stars")`。

**预期结果**（与 `test_debug` 中 `**` 的断言一致）：

```text
Markup: 8 [
    Strong: 8 [
        Star: "*",
        Markup: 4 [
            Text: "hi",
        ],
        Star: "*",
    ],
]
```

> 说明：`Marker`/`Token`/`Parser` 都是私有类型，无法在 crate 外构造。这里只能通过公开的 `parse` 观察产物来**间接**验证我们的追踪。若你不方便运行测试，可只完成第 1、2 步的纸面追踪，结果标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：在 `strong()` 中，如果把 `let m = p.marker();` 移到 `p.assert(SyntaxKind::Star);` **之后**，会产生什么后果？

**参考答案**：`marker()` 返回当时的 `nodes.len()`。若移到 `assert(Star)` 之后，开头的 `*` 已经被 eat 进 `nodes`，`m` 就会指向 `*` **之后**的位置。于是第 ⑤ 步 `wrap(m, Strong)` 圈住的范围会**漏掉开头的 `*`**，产出的 `Strong` 节点缺少左星号，CST 不再无损。这正是为什么 marker 必须在 eat 之前取。

**练习 2**：`Marker` 派生了 `Copy`。为什么这一点对它的使用方式很重要？

**参考答案**：因为 `marker()` 返回的 `Marker` 随后要被**两次**使用——一次传给 `wrap(m, ...)` 作为打包起点，另一次（在 `strong()` 里）传给 `expect_closing_delimiter(m, ...)` 和用作下标 `p[m]`。若 `Marker` 不是 `Copy`，第二次使用就会触发所有权错误，需要显式 `.clone()`。派生 `Copy` 让位置戳可以像整数一样随意复制，使用起来更轻量。

---

### 4.2 Token：单 token 前瞻与前置 trivia

#### 4.2.1 概念说明

`Token` 是 Parser 眼中的「当前 token」。它解决两个问题合在一起的需求：

1. **前瞻**：Parser 在决定调哪个子函数之前要先看一眼下一个 token，但不能消费它。于是 Parser 把「下一个待吃的 token」缓存在 `token` 字段里，`eat()` 时才真正把它推进 `nodes`。
2. **trivia 归属**：Lexer 产出的 token 前面可能跟着若干 trivia（空白、注释）。这些 trivia 已经被推进了 `nodes`，但它们在概念上「属于」当前这个 token 的前导。Parser 需要记住「当前 token 前面有几个 trivia」，才能决定这些 trivia 该归进哪一层子树。

`Token` 把这两份信息打包在一起，是 Parser 与 Lexer 之间的一次「交接」。

#### 4.2.2 核心流程

`Token` 结构体有 6 个字段：

```rust
/// Lexer 返回的单个 token，缓存了它的 SyntaxKind 与前置 trivia 记录。
#[derive(Debug, Clone)]
struct Token {
    kind: SyntaxKind,     // 当前 token 的类别
    node: SyntaxNode,     // 当前 token 的节点，待 eat 后 push 到 nodes 末尾
    n_trivia: usize,      // 本 token 之前有几个 trivia
    newline: Option<Newline>, // 本 token 的前置 trivia 中是否含换行
    start: usize,         // 本 token 在 text 中的起始下标
    prev_end: usize,      // 上一个 token 在 text 中的结束下标
}
```

来源：[src/parser.rs:1527-1545](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1527-L1545)。

这里有一个**反直觉但关键**的设计：`node` 字段只持有「真正的 token」这一个节点，而它前面的 trivia 节点**并不在 `Token` 里**——它们在 `lex()` 产出 `Token` 时就已经被 `push` 进了 `nodes`（见 4.4.1 的 `lex`）。`Token` 只是用 `n_trivia` 这个计数器记住「我前面有 N 个 trivia 已经落在 `nodes` 末尾」。

`newline` 字段的类型 `Newline` 是另一个小结构，记录换行的两份信息：

```rust
/// 一组 trivia 中关于换行的信息。
#[derive(Debug, Copy, Clone)]
struct Newline {
    column: Option<usize>, // 下一个 token 在其所在行的列号
    parbreak: bool,        // 这些换行里是否有段落分隔（连续空行）
}
```

来源：[src/parser.rs:1547-1554](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1547-L1554)——`column` 用于 Markup 模式下列对齐判定（如列表项缩进），`parbreak` 用于判断是否触发了段落分隔。这两份信息会被 `AtNewline`（4.3 节）用来决定是否在换行处停止解析。

`token` 字段在 `Parser` 中的注释也点明了它「单 token 前瞻」的身份：

```rust
/// 当前正在审视、尚未进入 nodes 的 token。它相当于解析器的单 token 前瞻。
///
/// wrap 时，它不会被包含在被 wrap 的节点中。
token: Token,
```

来源：[src/parser.rs:1508-1512](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1508-L1512)——注意最后一句：`wrap` 的上界是 `before_trivia()`（4.4.2），它**不包括**当前 token 本身。这保证了「正在偷看但还没决定吃掉的 token」不会被误打包进任何子树。

#### 4.2.3 源码精读

`Token` 是由关联函数 `lex` 产出的（之所以是关联函数而非 `&self` 方法，是因为构造 Parser 时还没有 `self`，需要先有一个 `Token` 才能初始化）。`lex` 的核心是一段循环，把 trivia 和真正的 token 分开处理：

```rust
fn lex(nodes: &mut Vec<SyntaxNode>, lexer: &mut Lexer, nl_mode: AtNewline) -> Token {
    let prev_end = lexer.cursor();
    let mut start = prev_end;
    let (mut kind, mut node) = lexer.next();
    let mut n_trivia = 0;
    let mut had_newline = false;
    let mut parbreak = false;

    while kind.is_trivia() {           // 只要还是 trivia 就一直吃
        had_newline |= lexer.newline();// 换行一定属于 trivia
        parbreak |= kind == SyntaxKind::Parbreak;
        n_trivia += 1;
        nodes.push(node);              // trivia 立即落入 nodes
        start = lexer.cursor();
        (kind, node) = lexer.next();
    }
    // ...（换行处理见 4.3.3）
    Token { kind, node, n_trivia, newline, start, prev_end }
}
```

来源：[src/parser.rs:1854-1886](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1854-L1886)——关键在第 `nodes.push(node)` 这一行：**trivia 在 `lex` 阶段就已经被推进 `nodes`**，而真正的 token 留在局部变量 `node` 里，最终装进返回的 `Token`。这就是为什么 `Token` 里只存一个 `node`，却要额外用 `n_trivia` 记数。

这段循环还顺带累计了两份换行信息：`had_newline`（是否出现过任何换行）和 `parbreak`（是否出现段落分隔），它们在循环结束后被组装成 `Newline`（见 4.3.3）。

#### 4.2.4 代码实践

**实践目标**：理解 trivia 已落入 `nodes` 这一设计，看清 `n_trivia` 的作用。

**操作步骤**：

1. 阅读 [src/parser.rs:1854-1886](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1854-L1886) 的 `lex`，确认 trivia 是在 `while kind.is_trivia()` 循环里被 `push` 进 `nodes` 的。
2. 思考：对文本 `* hi *`（星号、空格、hi、空格、星号），`strong()` 解析中间内容时，`Text "hi"` 这个 token 的 `n_trivia` 是多少？那两个空格 trivia 落在哪里？
3. 用 `crate::parse("* hi *")` 观察 CST：那两个空格是否被包进了内层的 `Markup`（即 `Strong` 的中间子树）？

**需要观察的现象**：`* hi *` 的 `Strong` 子树中，内层 `Markup` 应包含 `Space " "`、`Text "hi"`、`Space " "`——即两个空格 trivia 都进了内层 `Markup`，而不是留在外层。这正是 4.4.2 将看到的 `before_trivia()` + `flush_trivia()` 配合的结果。

**预期结果**：待本地验证（你可以把 `* hi *` 的 `{:#?}` 打印出来与 `*hi*` 对比，观察空格的归属层）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Token.node` 只持有一个节点，而 trivia 不在 `Token` 里？

**参考答案**：因为 trivia 在 `lex()` 产出 `Token` 之前就已经被 `push` 进了 `nodes` 向量。`Token` 只缓存「尚未决定是否吃掉的真正 token」一个节点，用 `n_trivia` 这个计数器指回 `nodes` 末尾的那几个 trivia。这样做的好处是 `eat()` 实现极简——只需 `push(token.node)` 一次（见 4.4.1），trivia 早已就位。

**练习 2**：`Token` 派生了 `Clone`（而非 `Copy`），为什么？

**参考答案**：因为 `Token` 含有 `SyntaxNode` 字段（`node`），而 `SyntaxNode` 内部是 `Arc` 指针，并非 `Copy` 类型（它管理堆上的 CST 数据）。Parser 在做记忆化 checkpoint 时需要克隆整个 `Token`（见 u4-l1 的 `PartialState`），所以派生 `Clone`。`Marker` 则只含 `usize`，可以 `Copy`。

---

### 4.3 AtNewline：换行如何「伪造 End」终止解析

#### 4.3.1 概念说明

在 Typst 里，换行不只是空白——它有**语法效力**。例如 Code 模式下，一行 `#let x = 1` 后面遇到换行，这条语句就结束了；Markup 模式下，连续空行（parbreak）会结束当前段落/标题。也就是说，**换行可以像一个「假分号」或「假结束符」一样终止当前表达式**。

但 Lexer 并不会在换行处产出 `End` token（那样就丢失了换行信息）。于是 Parser 用一个「换行模式」字段 `nl_mode: AtNewline` 来表达「**当前这段解析，遇到换行要不要假装读到了 End**」。

#### 4.3.2 核心流程

`AtNewline` 是一个五变体枚举，每个变体回答「遇到换行时是否停止」：

```rust
/// 在换行处如何继续解析。
#[derive(Debug, Copy, Clone, Eq, PartialEq)]
enum AtNewline {
    Continue,            // 换行处继续
    Stop,                // 遇到任何换行都停
    ContextualContinue,  // 仅当后续是 else 或 . 时才继续（仅 Code）
    StopParBreak,        // 只在段落分隔（连续空行）处停（仅 Markup）
    RequireColumn(usize),// 要求下一个 token 的列号 >= 某值（仅 Markup）
}
```

来源：[src/parser.rs:1556-1571](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1556-L1571)。

每个变体的「停止条件」由 `stop_at` 方法封装，它接收换行信息 `Newline` 和当前 token 的 `kind`，返回一个布尔：

```rust
fn stop_at(self, Newline { column, parbreak }: Newline, kind: SyntaxKind) -> bool {
    match self {
        AtNewline::Continue => false,
        AtNewline::Stop => true,
        AtNewline::ContextualContinue => match kind {
            SyntaxKind::Else | SyntaxKind::Dot => false, // 续行
            _ => true,
        },
        AtNewline::StopParBreak => parbreak,
        AtNewline::RequireColumn(min_col) => {
            column.is_some_and(|column| column <= min_col)
        }
    }
}
```

来源：[src/parser.rs:1573-1594](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1573-L1594)——这个函数是「换行是否终止解析」的**唯一仲裁者**。几个要点：

- `ContextualContinue` 只在续行场景（`else`、链式调用的 `.`）放行，其余换行都停。这就是为什么 `#if x { 1 } \n else { 2 }` 里的 `else` 能跨行续上。
- `RequireColumn(min_col)` 用于 Markup 里判定「下一个结构是否缩进足够」（如子列表项）；当 `min_col` 为 `0` 时退化为 `Continue`，为 `usize::MAX` 时退化为 `Stop`，注释里有说明。

`stop_at` 返回 `true` 时，`lex` 会在 4.3.3 看到：它把 `kind` **临时改成 `End`**，于是上层 `while !p.at_set(stop_set)` 之类的循环就会以为「读到了结束」而停下。

`AtNewline` 的切换由 `with_nl_mode` 完成——它是一个「临时改模式、执行闭包、再恢复」的包装器：

```rust
fn with_nl_mode(&mut self, mode: AtNewline, func: impl FnOnce(&mut Parser<'s>)) {
    let previous = self.nl_mode;
    self.nl_mode = mode;
    func(self);
    self.nl_mode = previous;       // 闭包返回后恢复原模式
    if let Some(newline) = self.token.newline && mode != previous {
        // 恢复当前 token 的真实 kind，或插入一个假 End
        let actual_kind = self.token.node.kind();
        if self.nl_mode.stop_at(newline, actual_kind) {
            self.token.kind = SyntaxKind::End;
        } else {
            self.token.kind = actual_kind;
        }
    }
}
```

来源：[src/parser.rs:1831-1847](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1831-L1847)——它把调用栈当作「模式栈」来用：进入子解析时压入新模式，返回时弹出。退出时还要重新判定一次当前 token 是否该被「伪造为 End」，因为闭包执行期间 `nl_mode` 可能已经影响过 `token.kind`，现在模式变了，需要按**外层**模式重新决定。

#### 4.3.3 源码精读

「伪造 End」的真正发生地点在 `lex` 函数的尾部，紧接 4.2.3 看到的 trivia 循环：

```rust
let newline = if had_newline {
    let column =
        (lexer.mode() == SyntaxMode::Markup).then(|| lexer.column(start));
    let newline = Newline { column, parbreak };
    if nl_mode.stop_at(newline, kind) {
        // 插入一个临时的 End 来叫停解析器。真实 kind 稍后从 node 恢复。
        kind = SyntaxKind::End;
    }
    Some(newline)
} else {
    None
};

Token { kind, node, n_trivia, newline, start, prev_end }
```

来源：[src/parser.rs:1871-1886](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1871-L1886)——三个细节：

1. **`column` 只在 Markup 模式计算**（`.then(...)`），因为列对齐判定只在 Markup 有意义。其他模式下 `column` 为 `None`，`RequireColumn` 也就不会触发停止。
2. **真实 kind 没丢**：被改成 `End` 的只是局部变量 `kind`，而真正的 token 节点还在 `node` 里（`node.kind()` 仍是原值）。所以 `with_nl_mode` 退出时能用 `self.token.node.kind()` 把它恢复回来。这是一次「只改标签、不丢数据」的戏法。
3. `newline` 信息始终被装进 `Token`，无论是否伪造了 `End`。这样 `had_newline()`、`current_column()` 等查询即使在被伪造的 `End` 上也仍能拿到换行信息。

把 4.3.2 和 4.3.3 串起来，`AtNewline` 的完整闭环是：

```text
with_nl_mode(Stop, |p| { 解析子结构 })
   │
   ├─ 进入：nl_mode = Stop
   ├─ 解析中每次 eat → lex → stop_at(newline, kind)?
   │        是 → token.kind 伪造成 End → 上层循环以为结束而停
   │        否 → token.kind 保持原样 → 继续解析
   └─ 退出：nl_mode 恢复 + 按 outer 模式重新判定 token.kind
```

#### 4.3.4 代码实践

**实践目标**：感受 `AtNewline::Stop` 如何让标题在换行处终止。

**操作步骤**：

1. 阅读 [src/parser.rs:170-176](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L170-L176) 的 `heading()`，注意它用 `with_nl_mode(AtNewline::Stop, ...)` 包裹，所以标题内容**遇到任何换行就停**。
2. 对比 [src/parser.rs:137-151](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L137-L151) 的 `strong()`，它用的是 `AtNewline::StopParBreak`——即普通换行不停（`*a\nb*` 仍是一个 Strong），只有连续空行才停。
3. 用 `crate::parse("= Title\nbody")` 观察：标题 `Heading` 只包含 `Title`，`body` 在下一行成为 `Markup` 的另一个子节点；这正是 `Stop` 在换行处伪造 `End` 的效果。

**需要观察的现象**：`Heading` 节点不应跨越 `\n` 把 `body` 也吃进去；而 `*a\nb*`（若有）的 `Strong` 应跨越单换行包含两行文本。两者的差别完全来自 `Stop` 与 `StopParBreak`。

**预期结果**：`"= Title\nbody"` 的 `Heading` 子树只到 `Title` 为止；待本地验证 `"*a\nb*"` 的 `Strong` 是否跨行。

#### 4.3.5 小练习与答案

**练习 1**：`lex` 把 `kind` 改成 `End` 后，真正的 token 去哪了？会不会丢失？

**参考答案**：没丢。被改的只是局部变量 `kind`，真正的 token 节点仍完整保存在 `node` 里（`node.kind()` 仍是原值），并随 `Token` 返回。等解析退出当前 `with_nl_mode` 作用域时，`with_nl_mode` 会用 `self.token.node.kind()` 重新恢复 `token.kind`。所以「伪造 End」只是一个临时标签，CST 数据无损。

**练习 2**：`AtNewline::ContextualContinue` 为什么只在 `Else` 和 `Dot` 时放行续行？

**参考答案**：因为这两种是 Typst Code 里**合法跨行续接**的场景：`if..else` 的 `else` 可以在下一行，链式方法调用的 `.` 也可以在下一行。除此之外的换行在 Code 里都意味着「当前表达式结束」，应当停止。这个判定让 Typst 的代码不需要显式分号也能正确切分语句，同时允许少数续行模式。

---

### 4.4 解析原语：eat / at / current / wrap / convert_and_eat

#### 4.4.1 概念说明

有了 `Marker`（位置戳）、`Token`（前瞻 + trivia）、`AtNewline`（换行模式）这三块积木，Parser 的具体语法函数（如 `strong`、`heading`）只需要用一组**原语（primitives）**把它们串起来。这一组原语是所有语法函数共用的最小操作集，掌握它们就能读懂任意一段解析代码。

按职责分三类：

| 类别 | 原语 | 一句话作用 |
| --- | --- | --- |
| 前瞻查询 | `current` / `at` / `at_set` / `had_trivia` / `had_newline` | 只看当前 token，不消费 |
| 消费推进 | `eat` / `eat_if` / `assert` / `convert_and_eat` | 把当前 token 推进 `nodes`，并让 Lexer 前进 |
| 圈子树 | `marker` / `before_trivia` / `flush_trivia` / `wrap` | 取位置戳、调整 trivia 归属、打包内部节点 |

#### 4.4.2 核心流程

**前瞻查询**都极薄，几乎都是直接读 `self.token` 的字段：

```rust
fn current(&self) -> SyntaxKind { self.token.kind }            // 类似 peek()
fn at(&self, kind: SyntaxKind) -> bool { self.token.kind == kind } // 是否是某 kind
fn at_set(&self, set: SyntaxSet) -> bool { set.contains(self.token.kind) } // 是否在某集合
fn had_trivia(&self) -> bool { self.token.n_trivia > 0 }       // 前面是否有 trivia
fn had_newline(&self) -> bool { self.token.newline.is_some() } // 前置 trivia 是否含换行
```

来源：[src/parser.rs:1649-1685](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1649-L1685)——它们都只读 `token`，不改变任何状态。其中 `at` / `at_set` 是语法函数里出现频率最高的判定：`while !p.at_set(stop_set)`、`if p.at(Star)` 几乎随处可见。

**消费推进**的核心是 `eat`，它做两件事——把当前 token 推进 `nodes`，然后让 Lexer 前进一步产出新 token：

```rust
fn eat(&mut self) {
    self.nodes.push(std::mem::take(&mut self.token.node));   // ① 推进当前 token
    self.token = Self::lex(&mut self.nodes, &mut self.lexer, self.nl_mode); // ② 取下一个
}
```

来源：[src/parser.rs:1767-1772](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1767-L1772)——注意 `std::mem::take`：它把 `token.node`「搬走」塞进 `nodes`，留下一个默认值，避免克隆。第 ② 步调用的正是 4.2.3 看过的 `lex`，它会把下一个 token 前的 trivia 先推进 `nodes`，再返回新 `Token`。

围绕 `eat` 有三个变体：

- `eat_if(kind)`：仅当 `at(kind)` 时才 eat，返回是否吃了。会跳过 trivia（[src/parser.rs:1739-1749](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1739-L1749)）。
- `assert(kind)`：断言当前一定是 `kind` 再 eat，用于「函数交接时约定以某 token 开头」的场景（[src/parser.rs:1751-1758](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1751-L1758)）。`strong()` 里的 `p.assert(SyntaxKind::Star)` 就是。
- `convert_and_eat(kind)`：**先改 token 的 kind 再 eat**——下一小节专门讲。

**圈子树**这一组是 marker 事件式的核心。除了 4.1 看过的 `marker()`，还有三个：

`before_trivia()` 返回一个指向「当前 token 的第一个前置 trivia」的位置戳：

```rust
fn before_trivia(&self) -> Marker {
    Marker(self.nodes.len() - self.token.n_trivia)
}
```

来源：[src/parser.rs:1725-1729](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1725-L1729)——回想 4.2：当前 token 的前置 trivia 已经躺在 `nodes` 的**末尾**（最后 `n_trivia` 个）。所以 `nodes.len() - n_trivia` 正好指向这串 trivia 的第一个。对比 `marker()` 返回的 `nodes.len()`（指向 trivia **之后**、真正 token 的落点），两者差了一个 `n_trivia`：

```text
nodes:  [ ... , trivia_1, trivia_2, (token 待落入此处) ]
                      ▲                    ▲
                      │                    │
              before_trivia()          marker()
              （含 trivia）          （不含 trivia）
```

`wrap(from, kind)` 用 `before_trivia()` 作为上界，把一段节点打包成内部节点：

```rust
fn wrap(&mut self, from: Marker, kind: SyntaxKind) {
    let to = self.before_trivia().0;          // 上界 = 当前 token 的首个 trivia
    let from = from.0.min(to);                // 防御：from 不能超过 to
    let children = self.nodes.drain(from..to).collect(); // 抽出 [from, to)
    self.nodes.insert(from, SyntaxNode::inner(kind, children)); // 在原位插回
}
```

来源：[src/parser.rs:1781-1790](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1781-L1790)——三个动作：`drain` 抽出 `[from, to)` 这一段作为子节点，再用 `SyntaxNode::inner(kind, children)` 包成内部节点，`insert` 回到 `from` 位置。注意上界用的是 `before_trivia()` 而非 `marker()`，意味着**当前 token 前面的 trivia 会被包进正在构造的子树**——但当前 token 本身不会（它还在 `token` 字段里没被 eat）。

`flush_trivia()` 把当前 token 的 trivia 计数清零，相当于「声明这些 trivia 已被认领」：

```rust
fn flush_trivia(&mut self) {
    self.token.n_trivia = 0;
    self.token.prev_end = self.token.start;
}
```

来源：[src/parser.rs:1774-1779](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1774-L1779)——它配合 `before_trivia` 使用。典型场景见 `markup()`：当 `wrap_trivia=true` 时，内层 `Markup` 的 marker 取 `before_trivia()`（把尾随 trivia 包进内层），随后调 `flush_trivia()` 清零计数，这样**外层**再 `wrap` 时，`before_trivia()` 就不会再把这些 trivia 算作「当前 token 的前导」——它们已经安顿在内层子树里了。

> 一句话区分三个位置戳：`marker()` = 当前 token 落点（**不含** trivia）；`before_trivia()` = 当前 token 首个 trivia（**含** trivia）；`flush_trivia()` 不是位置戳，而是「把 trivia 所有权移交给已建子树」的记账操作。

#### 4.4.3 源码精读

`convert_and_eat` 是个精巧的原语，它解决「Lexer 产的 kind 和 Parser 想要的 kind 不一致」的问题：

```rust
fn convert_and_eat(&mut self, kind: SyntaxKind) {
    // 只需替换这里的 node。
    self.token.node.convert_to_kind(kind);
    self.eat();
}
```

来源：[src/parser.rs:1760-1765](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1760-L1765)——它先把当前 token 节点的 kind **原地改写**为 `kind`，再调 `eat()` 消费。典型用法在 `markup_expr` 里：Markup 模式下，嵌套的 `[` 和 `]` 被 Lexer 切成 `LeftBracket` / `RightBracket`，但 Parser 在某些上下文要把它们当作普通文本 `Text` 处理，于是 `p.convert_and_eat(SyntaxKind::Text)`（见 [src/parser.rs:91-98](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L91-L98)）。

它底层调用的 `convert_to_kind` 定义在 `node.rs`，逻辑是遍历（可能穿过 Warning 包装层）把叶子或内部节点的 kind 字段直接改写，同时保留原文不变：

```rust
pub(super) fn convert_to_kind(&mut self, new_kind: SyntaxKind) {
    if new_kind.is_error() { panic!(...); }
    else if self.kind().is_error() { panic!(...); }
    let mut data = &mut self.data;
    loop {
        match data {
            Node::Leaf(_, kind) | Node::Inner(_, kind) => { *kind = new_kind; break; }
            Node::Error(_, _) => unreachable!(),
            Node::Warning(warn, kind) => { *kind = new_kind; data = &mut Arc::make_mut(warn).child; }
        }
    }
}
```

来源：[src/node.rs:466-488](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L466-L488)——注意两点：① 它**拒绝改写成错误 kind**（要改错误请用 `convert_to_error`）；② 它能穿透 `Warning` 包装层，把内层被包裹节点的 kind 也一并改掉（因为 Warning 会复制一份 kind）。改写只动 kind 字段，节点承载的文本一字不动，所以 CST 依然无损。

把 4.4.2 和 4.4.3 的原语组装起来，`strong()`（[src/parser.rs:137-151](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L137-L151)）的每一步都能对号入座：

| 步骤 | 代码 | 用到的原语 |
| --- | --- | --- |
| 记起点 | `let m = p.marker();` | `marker()` |
| 吃开头 `*` | `p.assert(SyntaxKind::Star);` | `assert` → `eat` |
| 解析中间内容 | `markup(p, false, true, ...)` | 内部用 `before_trivia` / `flush_trivia` / `wrap` |
| 吃结尾 `*` | `p.expect_closing_delimiter(m, Star)` | `eat_if` → `eat`（缺失时 `convert_to_error`） |
| 圈成 Strong | `p.wrap(m, SyntaxKind::Strong);` | `wrap`（上界用 `before_trivia()`） |
| 访问结果 | `p[m].len()` / `p[m].warn(...)` | `Index<Marker>` |

#### 4.4.4 代码实践

**实践目标**：仿照 `strong()` 的结构，手写一份解析步骤伪代码，并对照真实 CST 验证。

**操作步骤**：

1. 再读一遍 [src/parser.rs:137-151](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L137-L151) 的 `strong()`，以及 [src/parser.rs:40-47](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L40-L47) 的 `markup()`（它演示了 `before_trivia` + `flush_trivia` + `wrap` 的组合）。
2. 假设要新增一个语法构造 `~波浪号强调~`（类似 `_emph_` 但用 `~` 定界），**仿照 `strong()`/`emph()` 写一份伪代码**，要求：用 `marker()` 记起点、`assert(Tilde)` 吃定界符、递归解析中间内容、`wrap(m, Wavy)` 打包。
3. 把伪代码里每一步标注上用到的原语名（`marker`/`assert`/`eat`/`wrap`/`before_trivia`/`flush_trivia`）。
4. 用 `crate::parse("*hi*")` 和 `crate::parse("_hi_")` 各解析一次，打印 `{:#?}`，对照确认两者的 CST 结构对称（`Strong` 对 `Emph`，`Star` 对 `Underscore`）。

**需要观察的现象**：你写的伪代码应当与 `strong()` / `emph()`（[src/parser.rs:154-168](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L154-L168)）结构一致，只是定界符 kind 与根 kind 不同。`*hi*` 与 `_hi_` 的 CST 应高度对称。

**预期结果**：`*hi*` 产出 `Strong[Star, Markup[Text "hi"], Star]`；`_hi_` 产出 `Emph[Underscore, Markup[Text "hi"], Underscore]`。若你方便运行，可用如下临时测试验证（**示例代码**，非项目原有代码）：

```rust
// 示例代码：可临时放入 node.rs 的 #[cfg(test)] mod tests 中运行
#[test]
fn practice_strong_vs_emph() {
    let strong = format!("{:#?}", crate::parse("*hi*"));
    let emph = format!("{:#?}", crate::parse("_hi_"));
    println!("{strong}\n\n{emph}");
    assert!(strong.contains("Strong"));
    assert!(emph.contains("Emph"));
}
```

运行方式：在本仓库执行 `cargo test -p typst-syntax practice_strong_vs_emph -- --nocapture`（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：`wrap(from, kind)` 的上界为什么用 `before_trivia()` 而不是 `marker()`？用 `* hi *`（中间带空格）举例说明。

**参考答案**：若上界用 `marker()`，那么当前 token 前面的 trivia（如结尾 `*` 前的那个空格）就会被**排除**在正在打包的子树之外，可能错误地留在外层。用 `before_trivia()` 则把尾随 trivia 包进当前子树。对 `* hi *`，内层 `Markup` 用 `before_trivia()` 取起点、再 `flush_trivia()`，使得 `Space " "`、`Text "hi"`、`Space " "` 都进内层 `Markup`，最终 `Strong[Star, Markup[Space, Text, Space], Star]`，空格归属正确。

**练习 2**：`convert_and_eat(Text)` 与直接 `eat()` 有什么区别？为什么不直接让 Lexer 产出 `Text`？

**参考答案**：`convert_and_eat(Text)` 在 eat 之前把节点 kind **原地改写**为 `Text`，而 `eat()` 保持 Lexer 给的原 kind。不直接让 Lexer 产 `Text`，是因为同一个 `[` 字符在不同上下文有不同含义：在 Markup 顶层它是进入内容块的 `LeftBracket`，但在已被 `[` 打开的内容块**内部**（嵌套），它要被当作字面文本 `Text`。Lexer 没有「当前嵌套深度」的上下文（它只看字符和模式），所以这个「按上下文重解释」的判定交给 Parser 用 `convert_and_eat` 完成。

**练习 3**：`expect_closing_delimiter` 在找不到结尾定界符时做了什么？它如何做到「流程仍向前推进」？

**参考答案**：见 [src/parser.rs:2000-2006](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L2000-L2006)。若 `eat_if(kind)` 失败（找不到结尾 `*`），它把 `nodes[open.0]`（即开头那个 `*` 所在位置）的节点**原地转成错误节点** `convert_to_error("unclosed delimiter")`，但**不消费任何额外 token**，于是解析继续从当前 token 向前推进。这正是 marker 事件式的好处：子节点本就平铺在 `nodes` 里，错误恢复只需改一个位置上的节点，不必「撤销」已返回的子树。

---

## 5. 综合实践

把本讲四块积木串起来，完成下面这个**源码阅读 + 行为验证**的综合任务：

**任务背景**：Typst 的列表项 `- item` 与强调 `*bold*` 都是「位置敏感的标记」。本讲已经分析过 `strong()`；现在请用同样的方法分析 `list_item()`，并对比两者对 `AtNewline` 模式的不同选择。

**要求**：

1. **定位与阅读**：在 [src/parser.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs) 中找到 `list_item` 函数（提示：它在 `strong`/`emph`/`heading` 附近，约 170 行之后），阅读它的实现。
2. **画对照表**：为 `strong()` 和 `list_item()` 各画一张表，列出：① 用了哪个 `AtNewline` 模式；② 用了哪些原语（`marker`/`assert`/`eat`/`wrap`/`before_trivia`/`flush_trivia`/`convert_and_eat`）；③ 是否调用 `markup` 并传 `wrap_trivia=true`。
3. **解释差异**：用一段话解释为什么 `strong()` 选 `StopParBreak`（允许跨单行、空行才停），而列表项选了它那个模式——这跟「列表项内容能否跨行」有什么关系？
4. **行为验证**：用 `crate::parse("- a\n- b")` 和 `crate::parse("- a\n  b")`（第二行有缩进）观察 CST，验证你对列表项换行模式的推断。
5. **产出**：一份不超过半页的「`strong` vs `list_item` 原语用法对照」小结，附两个解析结果的 CST 片段。

**评判标准**：能正确指出两者在 `AtNewline` 模式与原语组合上的差异，并用 CST 输出印证「列表项内容在何种换行下会延续/终止」，就算完成。运行结果若无法本地验证，请明确标注「待本地验证」并给出你**预期**的 CST 结构。

## 6. 本讲小结

- **`Marker`** 是一个 `Copy` 的 `usize` 新类型，是「圈子树」的位置戳：在 eat 子节点**之前**用 `marker()` 记下起点，子节点全部 eat 完之后再 `wrap(m, kind)` 事后打包成内部节点。这让解析函数不必在入口承诺子树边界，利于错误恢复。
- **`Token`** 是 Parser 的单 token 前瞻，缓存当前 token 的 `kind`/`node` 以及「前置 trivia 计数 `n_trivia`」与「换行信息 `newline`」。反直觉但关键的一点：trivia 在 `lex()` 阶段就已落入 `nodes`，`Token` 只用 `n_trivia` 指回它们。
- **`marker()` 与 `before_trivia()`** 是两个位置戳：前者指向当前 token 落点（**不含** trivia），后者指向首个前置 trivia（**含** trivia）；`flush_trivia()` 则把 trivia 所有权移交给已建子树。三者的配合决定 trivia 归属哪一层。
- **`eat` / `at` / `current`** 是最常用的「消费 + 前瞻」原语：`eat` 把当前 token 推进 `nodes` 并让 Lexer 前进，`at`/`at_set`/`current` 只读 `token` 不改状态。`assert`、`eat_if`、`convert_and_eat` 是 `eat` 的带条件/带改写变体。
- **`convert_and_eat`** 先用 `node.convert_to_kind` 原地改写 kind 再 eat，用于「同一字符在不同上下文重解释」（如嵌套 `[`/`]` 当作 `Text`），底层只动 kind、不改文本，CST 无损。
- **`AtNewline`** 是换行模式枚举，由 `stop_at` 仲裁「换行是否终止解析」；一旦判定停止，`lex` 会把当前 token 的 `kind` **临时伪造成 `End`** 叫停上层循环，但真实 kind 仍保存在 `node` 里、退出 `with_nl_mode` 时恢复——这就是换行参与语法判定的机制。

## 7. 下一步学习建议

本讲掌握了 Parser 的底层原语，接下来可以：

- **u4-l3 Markup 解析**：看 `markup_exprs` / `markup_expr` 如何用本讲的原语循环解析文本、标题、列表、强调、方程、`#` 嵌套代码，重点关注 `at_start` 状态与 `at`/`at_set` 的判定。
- **u4-l4 Code 与 Math 解析**：看 `code_expr_prec` 的优先级爬升如何用 `marker`/`wrap` 构造嵌套表达式，以及 `AtNewline::ContextualContinue` 如何让 `else`/`.` 跨行续接。
- **u4-l5 新行处理与错误恢复**：深入 `with_nl_mode`、`unexpected`/`expected`/`trim_errors`，看 `convert_to_error`、`wrap_error` 如何与 marker 配合做错误恢复（本讲 4.4.5 练习 3 已触及）。
- **U5 CST 数据结构**：从消费侧回看 `SyntaxNode::inner`、`convert_to_kind`、`convert_to_error` 的完整实现，理解 marker 打包出的内部节点在内存里长什么样。

如果想在读 u4-l3 之前热身，建议重读 [src/parser.rs:137-151](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L137-L151) 的 `strong()`，并尝试不看本讲义、独立说出它的每一行用到了哪个原语。

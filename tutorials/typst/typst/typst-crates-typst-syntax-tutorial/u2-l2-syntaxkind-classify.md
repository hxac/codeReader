# SyntaxKind 的分类方法

## 1. 本讲目标

上一讲（u2-l1）我们把 `SyntaxKind` 当作一个有 137 个变体的大枚举来通览。但解析器、诊断系统、增量重解析在写代码时，几乎不会去逐个列举这 137 个变体——它们反复问的是同一类「分类问题」：

- 这个 token 是不是可以跳过的空白/注释？
- 这个节点是不是一条需要分号或换行来收尾的「语句」？
- 当前字符是不是一个会终止列表解析的「终止符」？

这些问题之所以重要，是因为它们直接决定了解析器在每个位置的**决策分支**。typst-syntax 把这些反复出现的分类逻辑集中写成 `SyntaxKind` 上的一组 `is_*` 判定方法，让全 crate 复用同一份「分类口径」。

学完本讲，你应当能够：

1. 说出 `is_grouping` / `is_terminator` / `is_block` / `is_stmt` / `is_keyword` / `is_trivia` / `is_error` 各自返回 `true` 的 `SyntaxKind` 范围。
2. 理解 trivia、block、stmt、terminator 这些分类在解析器里分别承担什么决策意义。
3. 区分「关键字 token」与「语句节点」——例如 `Let` 是关键字，而 `LetBinding` 是语句。
4. 学会用 `name()` 拿到面向用户的可读名称，并理解它如何被拼接进错误消息。
5. 看懂这组方法在 `parser.rs` / `node.rs` / `ast.rs` / `reparser.rs` 里的复用方式。

## 2. 前置知识

本讲承接 u2-l1，默认你已经知道：

- `SyntaxKind` 是 lexer 产出的 token 与 parser 构建的 CST 节点**共用**的统一标签类型（见 [src/kind.rs:6-8](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L6-L8)），以 `#[repr(u8)]` 紧凑表示。
- 同一个语法概念常以「token」和「节点」两种形态出现：例如关键字 `Let`（token，由 lexer 产生）对应语句节点 `LetBinding`（由 parser 产生）。这是本讲多个分类方法的核心区别点。
- trivia 指空白与注释等「对语法结构无意义、可被跳过」的片段。
- CST 节点可以用 `kind()` 取到它的 `SyntaxKind`（u5 会详讲 `SyntaxNode`，这里只需知道「拿到 kind 后就能调用 `is_*`」）。

## 3. 本讲源码地图

本讲几乎只围绕一个文件展开，但会跳到几个「消费方」文件验证复用关系：

| 文件 | 作用 |
| --- | --- |
| [src/kind.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs) | 定义 `SyntaxKind` 枚举，以及本讲的主角：`impl SyntaxKind` 里的 `is_*` 判定方法与 `name()`。 |
| [src/parser.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs) | 解析器，是 `is_terminator` / `is_trivia` / `is_grouping` / `is_keyword` 最密集的消费方。 |
| [src/node.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs) | CST 节点；诊断生成（`convert_to_error` / `expected` / `unexpected`）复用了 `is_error` / `is_keyword` / `name()`。 |
| [src/ast.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs) | AST 视图；`Markup::exprs` 用 `is_stmt()` 过滤语句后的换行。 |
| [src/reparser.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs) | 增量重解析；用 `is_block` 选重解析目标、用 `is_trivia`/`is_error` 决定扩展范围。 |

> 关于「高亮」的一个澄清：本系列大纲提到这组方法「被 parser 与 highlight 复用」，但**实际源码里 `highlight.rs` 并不调用任何 `is_*` 方法**。它走的是另一条路——对 `SyntaxKind` 做一次完整的 `match`，映射到自己定义的 `Tag` 枚举（见 [src/highlight.rs:150-239](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/highlight.rs#L150-L239)）。也就是说，typst-syntax 里其实存在**两套分类口径**：`is_*` 是面向解析与诊断的「谓词口径」，而 `highlight.rs` 的 `match` 是面向着色的「标签口径」。本讲聚焦前者。

## 4. 核心概念与源码讲解

### 4.1 分类判定方法族（is_*）

#### 4.1.1 概念说明

`SyntaxKind` 有 137 个变体，如果每个调用方都自己写一长串 `matches!(kind, A | B | C | ...)`，不但冗长，而且**口径容易不一致**——今天解析器认为「空白」是这 5 种，明天诊断系统又漏掉一种，bug 就产生了。

typst-syntax 的做法是把「哪些 kind 属于同一类」这个**分类口径**集中收口在 `impl SyntaxKind` 里，写成一组返回 `bool` 的判定方法：

- `is_grouping`：是不是成对的括号/花括号/方括号？
- `is_terminator`：是不是会终止前一个表达式的 token？
- `is_block`：是不是代码块或内容块？
- `is_stmt`：是不是一条需要分号/换行收尾的语句？
- `is_keyword`：是不是一个关键字 token？
- `is_trivia`：是不是在 code/math 模式下可被自动跳过的空白/注释？
- `is_error`：是不是错误节点？

每个方法都是一行 `matches!`，读起来像一句中文陈述，调用方只需 `kind.is_xxx()` 即可复用**同一份**判断。

#### 4.1.2 核心流程

这组方法本身没有复杂流程——它们都是纯函数式的集合判定。关键在于**每个集合到底装了哪些 kind**，以及**这些集合之间是否有交集**。下表给出了每个方法返回 `true` 的 kind 全集：

| 方法 | 返回 `true` 的 `SyntaxKind` | 个数 |
| --- | --- | --- |
| `is_trivia` | `Shebang`、`LineComment`、`BlockComment`、`Space`、`Parbreak` | 5 |
| `is_block` | `CodeBlock`、`ContentBlock` | 2 |
| `is_stmt` | `LetBinding`、`SetRule`、`ShowRule`、`ModuleImport`、`ModuleInclude` | 5 |
| `is_keyword` | `Not`、`And`、`Or`、`None`、`Auto`、`Let`、`Set`、`Show`、`Context`、`If`、`Else`、`For`、`In`、`While`、`Break`、`Continue`、`Return`、`Import`、`Include`、`As` | 20 |
| `is_grouping` | `LeftBracket`、`LeftBrace`、`LeftParen`、`RightBracket`、`RightBrace`、`RightParen` | 6 |
| `is_terminator` | `End`、`Semicolon`、`RightBrace`、`RightParen`、`RightBracket` | 5 |
| `is_error` | `Error` | 1 |

观察这张表，有两个对解析决策至关重要的结论：

1. **「关键字 token」与「语句节点」是两套互不重叠的集合。** `is_keyword` 里装的是 `Let`、`If`、`For` 等 lexer 产出的 token；而 `is_stmt` 里装的是 `LetBinding`、`Conditional`…… 不，注意：`Conditional` 并不在 `is_stmt` 里！`is_stmt` 只含 5 种「顶层声明性语句」。这说明 typst 把 `if`/`for`/`while` 视为**表达式**而非语句。而 `Let`（token，∈ keyword）与 `LetBinding`（节点，∈ stmt）分别落在两个集合里——同一个概念的两种形态。

2. **集合之间存在有意的交集。** 例如右花括号 `RightBrace` 同时是 `is_grouping`（成对括号之一）和 `is_terminator`（会终止表达式）。这种重叠正是解析器在不同场景下需要的：在收集数组元素时要靠「终止符」停下来，而在配平括号时又要靠「grouping」判断是否破坏了平衡。

把分类用「优先级顺序」串起来，就得到一个常用的分类判别流程（也是本讲代码实践的思路）：

```text
给定一个 SyntaxKind k：
  1. 若 k.is_trivia()        → 归入 trivia
  2. 否则若 k.is_block()     → 归入 block
  3. 否则若 k.is_stmt()      → 归入 stmt
  4. 否则若 k.is_keyword()   → 归入 keyword
  5. 否则                    → 归入 other（含 operator / grouping / literal / error 等）
```

注意第 5 类「other」是个兜底筐：运算符（`Plus`）、字面量（`Int`）、左括号（`LeftParen`）、错误（`Error`）都会落进来。这是因为本讲实践只分 5 类，而 `is_*` 方法本就不是为「覆盖全部 kind」设计的——它们各自只回答一个具体的分类问题。

#### 4.1.3 源码精读

**七个判定方法的定义**集中在一处，可以一次看完：

这些方法位于 [src/kind.rs:297-383](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L297-L383)。逐个来看：

- `is_grouping` — [src/kind.rs:299-309](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L299-L309)：判定六种成对分隔符。注释里写「Is this a bracket, brace, or parenthesis?」。
- `is_terminator` — [src/kind.rs:312-321](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L312-L321)：判定「会终结前一个表达式」的 token，含流末尾 `End`、分号、以及三种右分隔符。
- `is_block` — [src/kind.rs:324-326](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L324-L326)：只认 `CodeBlock` 与 `ContentBlock` 两种。
- `is_stmt` — [src/kind.rs:329-338](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L329-L338)：五种「需要分号或换行终止」的语句。
- `is_keyword` — [src/kind.rs:341-365](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L341-L365)：20 个关键字 token。注意它是关键字 **token**，不含语句节点。
- `is_trivia` — [src/kind.rs:369-378](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L369-L378)：注释里特别说明这是「在 code 和 math 模式下被 parser 自动跳过」的节点。
- `is_error` — [src/kind.rs:381-383](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L381-L383)：唯一标注了 `pub const fn` 的方法，因为它有时需要在常量上下文里使用。

**复用点 1：解析器靠 `is_terminator` 停下列表解析。** 解析 `import` 项列表 `a, b, c` 时，循环条件就是「当前 token 不是终止符」：

```rust
while !p.current().is_terminator() {
    // 解析一个 import 项，再期望一个逗号
    ...
    if !p.current().is_terminator() {
        p.expect(SyntaxKind::Comma);
    }
}
```

见 [src/parser.rs:944-968](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L944-L968)。同样的 `while !p.current().is_terminator()` 模式在数组、字典、参数列表、参数列表解析里反复出现（如 [src/parser.rs:1104](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1104)、[src/parser.rs:1211](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1211)、[src/parser.rs:1276](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1276)、[src/parser.rs:1356](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1356)）。一个判定方法统一了所有「读到右括号/分号/结尾就停」的决策。

**复用点 2：词法预读靠 `is_trivia` 跳过空白与注释。** 解析器在取下一个「有效 token」前，会把连续的 trivia 全部预读并挂到节点流里：

```rust
while kind.is_trivia() {
    had_newline |= lexer.newline();
    parbreak |= kind == SyntaxKind::Parbreak;
    n_trivia += 1;
    nodes.push(node);
    ...
    (kind, node) = lexer.next();
}
```

见 [src/parser.rs:1862-1869](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1862-L1869)。这里只要 `kind.rs` 调整了 trivia 的口径，整个解析器的「跳过」行为就会自动同步。

**复用点 3：错误恢复靠 `is_grouping` 判断是否破坏括号平衡。** 当解析器遇到意外 token 时，要决定「这会不会导致后续的右括号对不上」。`unexpected` 与 `expect` 都用 `self.balanced &= !kind.is_grouping()` 来更新一个「括号是否仍平衡」的标志位：

```rust
fn unexpected(&mut self) {
    self.trim_errors();
    self.balanced &= !self.token.kind.is_grouping();
    self.eat_and_get().unexpected();
}
```

见 [src/parser.rs:2053-2057](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L2053-L2057)（`expect` 里的同款写法见 [src/parser.rs:1991](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1991)）。意思是：丢失的是一个 grouping 分隔符时，括号平衡就被破坏了。

**复用点 4：AST 用 `is_stmt` 过滤「语句后的换行」。** `Markup::exprs()` 在遍历直接子节点时，用 `is_stmt()` 记住「上一个是不是语句」，从而丢掉语句之后那个无意义的 `Space`：

```rust
let mut was_stmt = false;
self.0.children().filter(move |node| {
    let kind = node.kind();
    let keep = !was_stmt || node.kind() != SyntaxKind::Space;
    was_stmt = kind.is_stmt();
    keep
})
```

见 [src/ast.rs:233-245](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L233-L245)。这是 AST 层复用 `is_*` 的典型例子。

**复用点 5：增量重解析靠 `is_block` 选目标、靠 `is_trivia`/`is_error` 扩范围。** reparser 找到最内层包住编辑区的节点后，若是块就用块级重解析；同时用一个 `expand` 谓词决定是否把相邻的 trivia/error 也并进来重算：

```rust
if child.kind().is_block()
    && let Some(reparsed) = reparse_block(text, new_range.clone())
{ ... }
```

见 [src/reparser.rs:99-108](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L99-L108)；而 `expand` 函数见 [src/reparser.rs:262-270](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L262-L270)，它把 `is_trivia()` 和 `is_error()` 作为「应越过节点的边界继续扩展」的判据。

#### 4.1.4 代码实践

**实践目标**：写一个函数，用 `is_*` 方法把任意 `SyntaxKind` 归到 trivia / block / stmt / keyword / other 五类之一，并用断言验证若干代表性 kind 的归类。

**操作步骤**：在仓库里临时给 `src/kind.rs` 末尾的 `#[cfg(test)] mod test`（见 [src/kind.rs:728](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L728)）新增一个测试。**示例代码**如下：

```rust
// 示例代码：加到 src/kind.rs 的 #[cfg(test)] mod test { ... } 内部
#[test]
fn classify_into_five_buckets() {
    fn bucket(k: SyntaxKind) -> &'static str {
        if k.is_trivia() {
            "trivia"
        } else if k.is_block() {
            "block"
        } else if k.is_stmt() {
            "stmt"
        } else if k.is_keyword() {
            "keyword"
        } else {
            "other"
        }
    }

    // trivia：空白、注释
    assert_eq!(bucket(SyntaxKind::Space), "trivia");
    assert_eq!(bucket(SyntaxKind::LineComment), "trivia");
    assert_eq!(bucket(SyntaxKind::Parbreak), "trivia");

    // block：代码块、内容块
    assert_eq!(bucket(SyntaxKind::CodeBlock), "block");
    assert_eq!(bucket(SyntaxKind::ContentBlock), "block");

    // stmt：五种声明性语句（注意是「节点」）
    assert_eq!(bucket(SyntaxKind::LetBinding), "stmt");
    assert_eq!(bucket(SyntaxKind::ModuleImport), "stmt");

    // keyword：关键字 token（注意是「token」，与上面的语句节点不同）
    assert_eq!(bucket(SyntaxKind::Let), "keyword");
    assert_eq!(bucket(SyntaxKind::If), "keyword");

    // other：运算符、左括号、错误
    assert_eq!(bucket(SyntaxKind::Plus), "other");
    assert_eq!(bucket(SyntaxKind::LeftParen), "other");
    assert_eq!(bucket(SyntaxKind::Error), "other");
}
```

运行：

```bash
cargo test -p typst-syntax classify_into_five_buckets
```

**需要观察的现象**：

- `Let`（关键字 token）归入 `keyword`，而 `LetBinding`（语句节点）归入 `stmt`——同一个「let」概念跨了两个桶。
- `RightBrace` 既不是 `block` 也不是 `stmt`，会落入 `other`；这印证了 `is_grouping`/`is_terminator` 并未出现在这 5 个桶里，本练习的分类口径并不覆盖全部 kind。
- 测试应当全部通过。

**预期结果**：测试编译并运行通过（`test result: ok. 1 passed`）。如需把桶打印出来观察，可把 `assert_eq!` 换成 `println!("{:?} -> {}", k, bucket(k));` 并用 `cargo test ... -- --nocapture`。若你不在本仓库环境，单独建一个依赖 `typst-syntax` 的小工程写 `main` 也可，输出相同；具体控制台文本「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`Conditional`（`if` 表达式节点）在五桶分类里会落在哪个桶？为什么？

> **答案**：落在 `other`。`Conditional` 不属于 `is_block`（它不是 `CodeBlock`/`ContentBlock`），也不在 `is_stmt` 的五种里（typst 把 `if`/`for`/`while` 当作表达式而非语句），更不是 trivia/keyword（它是节点不是 token）。这说明「语句」在 typst 里是个很窄的概念。

**练习 2**：`RightBrace` 同时满足 `is_grouping` 和 `is_terminator`。请解释这两种判定分别在解析器的哪个决策里被用到。

> **答案**：作为 `is_terminator`，它出现在收集数组/字典/参数列表的 `while !p.current().is_terminator()` 循环条件里，让循环在读到 `}` 时停下；作为 `is_grouping`，它在 `unexpected`/`expect` 里通过 `self.balanced &= !kind.is_grouping()` 判断「丢失了一个分隔符，括号平衡被破坏」，从而影响后续的错误恢复策略。

**练习 3**：如果想在 `is_trivia` 里新增一种 kind（假设叫 `MagicSpace`），需要改哪些地方才能让解析器、AST、reparser 都自动跟上？

> **答案**：只需在 [src/kind.rs:369-378](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L369-L378) 的 `matches!` 列表里加上 `Self::MagicSpace`。因为 parser 的预读循环、reparser 的 `expand`/`next_at_start`、以及多处「跳过 trivia 找有效节点」的逻辑都调用 `is_trivia()`，分类口径集中收口后，所有消费方会自动把新 kind 当 trivia 处理——这正是集中式分类的价值。

### 4.2 name() 面向用户的可读名称

#### 4.2.1 概念说明

`is_*` 方法回答的是「机器视角」的分类问题（是/否）。但解析器还要给用户**看人话**——当用户写了 `#let = 1` 这种错误时，报错不能写「found `Let`」，而要写「found keyword `let`」。

`name()` 就是把每个 `SyntaxKind` 翻译成一句稳定的、面向人类的英文字符串。它和 `is_*` 是互补的一对：

- `is_*`：供解析器内部做控制流决策。
- `name()`：供诊断系统拼出可读的错误消息。

注意 `name()` 返回的是 `&'static str`（编译期固定的字符串字面量），所以拼接错误消息几乎零成本。

#### 4.2.2 核心流程

`name()` 的实现是一次**穷举式 `match`**——137 个变体每个对应一条字符串。它没有也不能用 `is_*` 来简化，因为每个 kind 的名字都不同。流程上它是一个纯查表函数：

```text
name(k):
  在 match 表里找到 k 对应的那一条 → 返回 &'static str
```

值得对照阅读的是命名规律，它体现了「token vs 节点」的二分：

- 关键字 **token** 的名字带反引号，如 `Let` → `"keyword \`let\`"`。
- 对应的**节点**名字带「expression」后缀，如 `LetBinding` → `"\`let\` expression"`。
- 字面量/标识符用普通名词，如 `Int` → `"integer"`、`Ident` → `"identifier"`。
- 标点用描述性短语，如 `LeftBrace` → `"opening brace"`。

这种规律让你光看 `name()` 的输出，就能猜出它是 token 还是节点。

#### 4.2.3 源码精读

`name()` 的定义在 [src/kind.rs:386-526](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L386-L526)。它是 `match self { ... }` 的穷举，每个变体一行。关键片段（节选）：

```rust
Self::Let => "keyword `let`",
...
Self::LetBinding => "`let` expression",
...
Self::Ident => "identifier",
Self::Int => "integer",
```

**复用点：`name()` 几乎只被诊断系统用来拼错误消息。** 在 CST 节点的 `expected` 与 `unexpected` 方法里，它把「期望 X，却遇到 Y」的 Y 替换成可读名：

```rust
pub(super) fn expected(&mut self, expected: &str) {
    let kind = self.kind();
    self.convert_to_error(eco_format!("expected {expected}, found {}", kind.name()));
    ...
}

pub(super) fn unexpected(&mut self) {
    self.convert_to_error(eco_format!("unexpected {}", self.kind().name()));
}
```

见 [src/node.rs:500-514](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L500-L514)。于是用户看到的不是 `unexpected Plus`，而是 `unexpected plus`。

解析器侧也复用了同一个 `name()`：当期望一个标识符却读到关键字时，`expect` 会把期望的名字（由 `kind.name()` 给出）写进错误：

```rust
} else if kind == SyntaxKind::Ident && self.token.kind.is_keyword() {
    self.trim_errors();
    self.eat_and_get().expected(kind.name());
}
```

见 [src/parser.rs:1987-1992](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1987-L1992)（同一 `expect` 里第 1992 行 `self.expected(kind.name())` 也是同款用法）。注意这里把 `is_keyword()` 与 `name()` 组合起来用：先用 `is_keyword()` 判定「读到的其实是个关键字」，再用 `name()` 把它写成可读的错误——两个方法在一次错误恢复里分工合作。

> 旁注：lexer 里还有一处 `non_decimal.name()`（[src/lexer.rs:1021](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L1021)），但那是 Rust 标准库 `IntErrorKind` 的方法，**同名但不同源**，不要和 `SyntaxKind::name()` 混淆。

#### 4.2.4 代码实践

**实践目标**：观察 `name()` 对 token 与节点的不同输出，体会命名规律。

**操作步骤**：在 `src/kind.rs` 的 test 模块里加一段**示例代码**：

```rust
#[test]
fn observe_name_output() {
    for k in [
        SyntaxKind::Let,        // 关键字 token
        SyntaxKind::LetBinding, // 语句节点
        SyntaxKind::Plus,       // 运算符
        SyntaxKind::LeftBrace,  // 标点
        SyntaxKind::Error,      // 错误
    ] {
        eprintln!("{:?} -> \"{}\"", k, k.name());
    }
}
```

运行 `cargo test -p typst-syntax observe_name_output -- --nocapture`。

**预期结果**：

| `SyntaxKind` | `name()` 输出 |
| --- | --- |
| `Let` | `keyword \`let\`` |
| `LetBinding` | `\`let\` expression` |
| `Plus` | `plus` |
| `LeftBrace` | `opening brace` |
| `Error` | `syntax error` |

可以观察到：`Let` 与 `LetBinding` 的名字都含 `` `let` ``，但前者带 `keyword`、后者带 `expression`，正好对应 token/节点二分。若你无法在本机运行，上面表格已据源码给出确定结果；控制台确切排版「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `name()` 用穷举 `match`，而不像 `is_keyword()` 那样用 `matches!` 归并？

> **答案**：因为每个 kind 的可读名都**不同**，没有可归并的集合；`matches!` 适合「多个变体共享同一个布尔结论」的场景（如「这 20 个都是关键字」），而 `name()` 要为每个变体返回各自的字符串，只能逐条列出。

**练习 2**：用户输入 `#let = 1`（`=` 前缺标识符）。请追着 `name()` 与 `is_keyword()` 的复用点，描述错误消息大致是如何产生的。

> **答案**：解析 `let` 绑定时，`expect(Ident)` 发现当前不是标识符。若当前 token 恰好是个关键字，走 [src/parser.rs:1987-1989](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1987-L1989) 的分支（用 `is_keyword()` 判定），否则走第 1992 行 `self.expected(kind.name())`，其中 `kind.name()` 返回 `"identifier"`。最终在 [src/node.rs:500-502](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L500-L502) 拼成类似 `expected identifier, found equals sign` 的消息——「equals sign」正是 `Eq.name()` 的返回值。

**练习 3**：`is_error` 是唯一标了 `pub const fn` 的方法。结合它的用途，猜猜为什么它特别需要 `const`。

> **答案**：`is_error` 常被用在 `debug_assert!`、数组初始化、或在编译期就要判定的地方（例如 CST 节点构造时用 `debug_assert!(!kind.is_error())`，见 [src/node.rs:112](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L112) 与 [src/node.rs:122](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L122)）。标成 `const fn` 后它可在常量与 `const` 上下文里调用，比其它只在运行时调用的 `is_*` 更通用。

## 5. 综合实践

把本讲的两条线索（`is_*` 分类 + `name()` 命名）串起来，做一次「真实解析 → 节点分类」的小追踪。

**任务**：解析下面这段故意带错误的 Typst 代码，遍历 CST 的所有节点，对每个节点打印三列：`Debug 形式的 kind`、`name()` 输出、以及用本讲 4.1.4 的 `bucket()` 得到的五桶归类。

```text
= 标题
#let = 1
- 列表项
```

**操作步骤**（**示例代码**，可放进一个临时 `examples/` 或 test）：

```rust
use typst_syntax::{LinkedNode, Source};

fn bucket(k: typst_syntax::SyntaxKind) -> &'static str {
    if k.is_trivia() { "trivia" }
    else if k.is_block() { "block" }
    else if k.is_stmt() { "stmt" }
    else if k.is_keyword() { "keyword" }
    else { "other" }
}

fn main() {
    let src = Source::detached("= 标题\n#let = 1\n- 列表项\n");
    let root = LinkedNode::new(src.root());
    for node in root.descendants() {
        let k = node.kind();
        println!("{:?}\t{}\t{}", k, k.name(), bucket(k));
    }
}
```

**需要观察的现象**：

1. 树根是 `Markup`（`other`）。
2. 标题节点 `Heading`（`other`），其引导符 `=` 是 `HeadingMarker`（`other`，`name()` 为 `"heading marker"`）。
3. `LetBinding` 节点会被归到 **`stmt`**，而它内部的 `Let` 关键字 token 会被归到 **`keyword`**——你在同一棵子树里同时看到这两个桶，直观印证「token/节点二分」。
4. 由于 `=` 前缺少标识符，`LetBinding` 内会出现一个 `Error` 节点（`other`，`name()` 为 `"syntax error"`），其消息由 `name()` 拼出。
5. 列表项的 `-` 是 `ListMarker`，项本身是 `ListItem`，二者都不是 trivia（trivia 只含空白/注释/`Parbreak`），所以都落入 `other`。

**预期结果**：输出一张三列表，能看到 `Let`(keyword) 与 `LetBinding`(stmt) 并存、`Error`(other) 出现在错误位置。这同时复习了 u1-l4 的 CST 遍历、u2-l1 的变体识别，以及本讲的分类方法。若无法运行，上述结构化结论可据源码直接得出；逐行文本「待本地验证」。

## 6. 本讲小结

- typst-syntax 把「哪些 `SyntaxKind` 属于同一类」的**分类口径**集中写在 `impl SyntaxKind` 的 7 个 `is_*` 方法里，让 parser / node / ast / reparser 复用同一份判断，避免口径漂移。
- 七个方法各自回答一个具体问题：`is_trivia`（5 种可跳过片段）、`is_block`（2 种块）、`is_stmt`（5 种声明性语句）、`is_keyword`（20 个关键字 token）、`is_grouping`（6 种成对分隔符）、`is_terminator`（5 种终止符）、`is_error`（仅 `Error`）。
- 「关键字 token」与「语句节点」是**两套互不重叠的集合**：`Let` ∈ keyword，`LetBinding` ∈ stmt；且 typst 把 `if`/`for`/`while` 视为表达式而非语句（`Conditional` 不在 `is_stmt` 里）。
- `name()` 是 `is_*` 的互补方法，用穷举 `match` 把每个 kind 翻译成可读英文字符串，主要被诊断系统拼进错误消息（如 `expected identifier, found equals sign`）。
- `highlight.rs` **并不**使用 `is_*`，而是用自己的 `match` + `Tag` 枚举做着色分类——typst-syntax 里存在「谓词口径」与「标签口径」两套并行的分类体系。

## 7. 下一步学习建议

- 下一讲 **u2-l3（SyntaxSet 位集与 syntax_set! 宏）** 会展示另一套「分类口径」的工程化用法：parser 用 `u128` 位集把「< 128 的若干 kind」打包成集合（如 `STMT`、`CODE_EXPR`），做 O(1) 的集合判定。它和本讲的 `is_*` 是互补的两种集合表达——`is_*` 是写死的单个谓词，`SyntaxSet` 是可组合的位集。
- 进入 **U3（Lexer）** 后，你会看到 lexer 产出的每个 token 都带一个 `SyntaxKind`；届时可以回头验证：lexer 产出的 trivia token（`Space`、`LineComment`…）确实都满足 `is_trivia()`，与 parser 预读循环的假设一致。
- 想提前体会 `name()` 的用户价值，可在本仓库里跑 `cargo test -p typst-syntax`，观察解析错误用例里 `kind.name()` 是如何出现在诊断字符串中的。

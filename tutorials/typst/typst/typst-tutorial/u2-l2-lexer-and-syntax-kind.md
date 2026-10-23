# 词法与类别：Lexer 与 SyntaxKind

## 1. 本讲目标

编译的第一步是把一长串字符切成一个个有意义的「词」。本讲带你精读 Typst 语法层的两个基石文件：

1. `SyntaxKind` 枚举——Typst 给所有 token 和语法节点定义的「身份证」体系；
2. `Lexer`——一个手写的词法状态机，它根据当前处于 **markup（标记）、code（代码）、math（数学）** 三种模式中的哪一种，把同一个字符切成完全不同的 token。

学完本讲，你应该能够：

- 列举 `SyntaxKind` 的主要分组，并解释「一个枚举同时服务词法和语法两层」的设计；
- 跟踪 Lexer 在遇到 `#`、引号、数字、标识符等字符时的分词路径；
- 解释为什么模式切换由 parser 下达、Lexer 只负责执行；
- 解释词法错误如何以 Error 节点的形式保留在树里，而不是让解析中断。

## 2. 前置知识

- **token（词法单元）**：词法分析（lexing）把源码字符串切分为最小有意义片段。比如 `#for i in range(3)` 会被切成 `#`、`for`、空格、`i`、空格、`in`、… 等若干 token，每个 token 带一个类别标签。
- **手写状态机**：很多语言用正则工具（如 lex）生成词法器，Typst 选择全部手写在 [lexer.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs) 中（约 1300 行）。核心循环就是：读一个字符，按当前模式查表决定进入哪个处理分支，贪婪吞掉后续字符，产出一个 token。
- **Scanner（扫描游标）**：Lexer 借助 `unscanny` 库的 `Scanner`，它封装了「在字符串上移动的游标」，提供 `eat`（吃一个字符）、`eat_if`（条件地吃）、`eat_while`（贪婪地吃）、`scout(-1)`（偷看前一个字符）、`peek`（偷看下一个字符）等原语。读 Lexer 代码时把这些当「光标操作」理解即可。
- **三种语法模式**：上一讲（u2-l1）我们见过 `Source` 持有全文并维护行/列。本讲关注文本的「身份」之外的问题：同一段文本在不同上下文有不同含义。文件顶层是 markup 模式；`#` 之后是 code 模式；`$...$` 内部是 math 模式。定义在 [lib.rs:L42-L51](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lib.rs#L42-L51)。
- **Unicode 基础**：Typst 全面支持 Unicode。标识符规则基于 Unicode 的 XID_Start/XID_Continue 属性（`unicode-ident` 库）；数学模式还涉及「字素簇」（grapheme cluster，用户感知的单个字符，可能由多个码点组成，例如 `ℱ︀`）。
- **trivia（碎屑）**：注释和空白这类对语义无影响的 token，parser 在 code/math 模式会自动跳过它们。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [crates/typst-syntax/src/kind.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/kind.rs) | `SyntaxKind` 枚举：全部 token/节点类别、`is_*` 谓词、`name()` 人读名称、`mode_after()` 模式推断 |
| [crates/typst-syntax/src/lexer.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs) | 手写词法器 `Lexer`：markup/math/code 三套分词逻辑 + 错误产生 |
| [crates/typst-syntax/src/lib.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lib.rs) | crate 出口：`SyntaxMode` 定义、导出 `parse`/`SyntaxKind`/`is_ident` 等（注意：**`Lexer` 本身不对外导出**） |
| [crates/typst-syntax/src/node.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/node.rs) | `SyntaxNode`：叶子/内部/错误三类节点的构造与遍历接口 |
| [crates/typst-syntax/src/parser.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/parser.rs) | 下一讲的主角，本讲只看它如何「驱动」Lexer 切换模式 |

## 4. 核心概念与源码讲解

### 4.1 SyntaxKind 分类体系

#### 4.1.1 概念说明

`SyntaxKind` 是一个拥有上百个变体的 Rust 枚举，它是 Typst 语法世界中所有「东西」的分类学。它有一个非常重要的设计决策，写在文件开头：

> Can be created by the lexer or by the parser.（既可以由词法器产生，也可以由解析器产生）

也就是说，**同一个枚举同时承担两种角色**：

- **叶子 token 的类别**：Lexer 产出，如 `Text`、`Int`、`Star`、`For`；
- **内部节点的类别**：Parser 把若干 token 包装成结构时赋予，如 `Strong`、`ForLoop`、`Heading`。

一个直观对照：`*hi*` 中两个 `*` 各是一个 `Star` 叶子，而整体会被 parser 包装成一个 `Strong` 内部节点。类似地还有 `HeadingMarker`（叶子，`=`）与 `Heading`（内部节点）、`ListMarker` 与 `ListItem`。带 `Marker` 后缀的一律是 Lexer 产出的「前导记号」。

#### 4.1.2 核心流程

枚举体按注释分成七个语义段落（从上到下）：

```text
1. 边界与错误    End / Error
2. 碎屑          Shebang / LineComment / BlockComment
3. markup 组     Markup / Text / Space* / Parbreak / Escape /
                 Shorthand / SmartQuote / Strong / Emph / Raw* /
                 Link / Label / Ref / Heading / ListItem / EnumItem / TermItem ...
4. math 组       Equation / Math / MathLetter / MathSymbol /
                 MathNumber / MathIdent / MathFieldAccess / MathShorthand ...
5. 标点与运算符  Hash / 括号 / Comma / Colon / Star / Underscore /
                 Eq / EqEq / Arrow / Dots ...
6. 关键字        Not / And / Or / None / Auto / Let / Set / Show /
                 If / Else / For / In / While / Import ...
7. code 表达式   Code / Ident / Bool / Int / Float / Numeric / Str /
                 FuncCall / Closure / LetBinding / ForLoop ...
```

除枚举本身外，`impl SyntaxKind` 还提供三组配套能力：

1. **谓词方法**（`is_grouping`、`is_terminator`、`is_keyword`、`is_trivia` 等）——parser 用它们做分支决策；
2. **`name()`**——每个类别的英文人读名称，用于生成报错信息；
3. **`mode_after()`**——回答「这个节点之后处于哪种语法模式」，是 IDE 高亮与补全判断模式的依据（本讲只认识它，细节在 u2-l4/u5-l6 用到）。

#### 4.1.3 源码精读

**（1）枚举声明与文档注释即分组。** 每个 doc 注释都给出该类别的语法示例，本身就是一份速查表：

- [kind.rs:L8-L12](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/kind.rs#L8-L12)——枚举开头是 `End`（token 流结束哨兵）和 `Error`（非法字符序列）。注意 `Error` 不携带信息，详细信息存在 `SyntaxNode::error` 节点里（见 4.3）。
- [kind.rs:L21-L77](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/kind.rs#L21-L77)——markup 组。注意空白被拆成三档：`SpaceNoNewline`、`SpaceWithNewline`（markup 中恰好一个换行；code/math 中至少一个换行）、`Parbreak`（仅 markup 产出，连续空白含至少两个换行）。
- [kind.rs:L79-L131](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/kind.rs#L79-L131)——math 组。`MathLetter` 与 `MathSymbol` 的 doc 注释强调「恰好一个字素簇」，这是数学排版按单符号处理的基础。
- [kind.rs:L133-L202](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/kind.rs#L133-L202)——标点与运算符。注意 `Star` 的注释：一个符号三种身份——加粗开关、乘法、通配导入。
- [kind.rs:L204-L243](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/kind.rs#L204-L243)——关键字组，`not`/`and`/`or` 也在这里，说明它们是词法级的。
- [kind.rs:L245-L322](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/kind.rs#L245-L322)——code 表达式组。这一段几乎全部由 parser 产生（`FuncCall`、`ForLoop`、`Closure`…），再次印证「一个枚举两层用途」。

**（2）谓词方法：parser 的决策依据。**

- [kind.rs:L368-L393](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/kind.rs#L368-L393)——`is_keyword()` 列出全部 20 个关键字。
- [kind.rs:L403-L415](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/kind.rs#L403-L415)——`is_trivia()`：shebang、两种注释、两种空白、段落断行。这些在 code/math 模式会被 parser 的取词循环直接收进树里跳过（见 4.2）。
- [kind.rs:L395-L401](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/kind.rs#L395-L401)——`is_text_like()`：能产生与自身匹配文本的类别，排版层据此把相邻文本合并。

**（3）`name()`：报错文案的来源。** [kind.rs:L423-L568](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/kind.rs#L423-L568) 给每个变体一个英文名，例如 `Error => "syntax error"`、`RightParen => "closing paren"`。当你看到 Typst 报 "expected closing brace, found closing paren" 时，文案就是从这张表拼出来的。

**（4）`ModeAfter`：从类别反推模式。** [kind.rs:L571-L605](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/kind.rs#L571-L605) 定义了模式推断的返回类型：`Known(某模式)`（如 `Hash => Known(Code)`）、`Parent`（取决于父节点，如 `Star` 在 code 是乘法、在 markup 是加粗）、`None`（注释内部不处于任何模式）等。[kind.rs:L607-L772](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/kind.rs#L607-L772) 是全量映射表，文件尾部 [kind.rs:L775-L906](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/kind.rs#L775-L906) 的单元测试用几十个真实片段验证它。

#### 4.1.4 代码实践

**实践目标**：用公开 API 观察「叶子类别 + 内部类别」如何共存于一棵树。

`Lexer` 没有被 crate 导出（它是 `pub(super)`，见 [lexer.rs:L16](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs#L16)；导出清单在 [lib.rs:L18-L23](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lib.rs#L18-L23)，只有 `is_ident`、`link_prefix` 等辅助函数）。所以外部观察 token 的正确姿势是：调用公开的 `parse()`（[parser.rs:L15-L21](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/parser.rs#L15-L21)），再深度优先遍历树。本讲后面的实践都复用这个骨架，先在 typst 仓库**同级**目录建一个独立小项目（不修改仓库）：

1. 操作步骤：

   ```bash
   cargo new lex-playground   # 建在 typst 仓库外面
   cd lex-playground
   ```

   在 `Cargo.toml` 的 `[dependencies]` 加（path 按实际相对位置调整）：

   ```toml
   typst-syntax = { path = "../typst/crates/typst-syntax" }
   ```

   把 `src/main.rs` 替换为下面的**示例代码**：

   ```rust
   use typst_syntax::{parse, SyntaxNode};

   fn main() {
       let root = parse("*hi* 1 + 2");
       walk(&root, 0);
   }

   // 深度优先打印每个节点的类别；叶子额外打印原文
   fn walk(node: &SyntaxNode, depth: usize) {
       if node.children().len() == 0 {
           println!("{}{:?}\t{:?}", "  ".repeat(depth), node.kind(), node.leaf_text());
       } else {
           println!("{}{:?}", "  ".repeat(depth), node.kind());
           for child in node.children() {
               walk(child, depth + 1);
           }
       }
   }
   ```

   然后运行 `cargo run`。

2. 需要观察的现象：树的顶层是 `Markup`；`*hi*` 那一层是内部节点 `Strong`，它的三个孩子是 `Star`、`Text("hi")`、`Star`；数字和 `+` 则是叶子 `Int`、`SpaceNoNewline`、`Plus`。
3. 预期结果：`Strong`（parser 包装）与 `Star`（lexer 叶子）出现在同一棵树的不同层级，直观验证「一个枚举两层用途」。（具体缩进输出待本地验证。）

接口依据：`kind()` 在 [node.rs:L215-L223](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/node.rs#L215-L223)，`children()` 在 [node.rs:L277-L283](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/node.rs#L277-L283)，`leaf_text()` 在 [node.rs:L244-L254](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/node.rs#L244-L254)（内部节点返回空串，错误节点返回出错原文）。

#### 4.1.5 小练习与答案

1. **练习**：`= Intro` 中，`=` 和整体分别是什么类别？谁是 Lexer 产的、谁是 Parser 产的？
   **答案**：`=`（含重复的 `==`）是叶子 `HeadingMarker`（Lexer 产出）；整体标题是内部节点 `Heading`（Parser 产出，见 [parser.rs:L174-L179](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/parser.rs#L174-L179) 中 `p.wrap(m, SyntaxKind::Heading)`）。
2. **练习**：`Star` 一个符号承担哪三种语法身份？
   **答案**：markup 里的加粗开关 `*strong*`、code 里的乘法运算符、import 中的通配符（doc 注释见 [kind.rs:L156-L158](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/kind.rs#L156-L158)）。
3. **练习**：哪些类别属于 trivia？这暗示了什么？
   **答案**：`Shebang`、`LineComment`、`BlockComment`、`SpaceNoNewline`、`SpaceWithNewline`、`Parbreak`（[kind.rs:L403-L415](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/kind.rs#L403-L415)）。暗示注释与空白并不丢弃，而是作为节点留在树中（保证树覆盖全部原文， Span 与增量重解析才能工作），只是 parser 在 code/math 模式会跳过它们。

### 4.2 Lexer 状态机与模式切换

#### 4.2.1 概念说明

Typst 是「标记 + 脚本 + 数学」三合一语言，这意味着**词法是上下文相关的**：同一个字符在不同模式下含义完全不同。例如：

- `*` 在 markup 中是加粗开关（`Star`→由 parser 包装成 `Strong`），在 code 中只是乘号；
- `~` 在 markup 中是不换行空格（`Shorthand`），在 code 中是非法字符；
- `1.2pt` 在 markup 中是纯文本（`Text`），在 code 中是一个带单位的数值（`Numeric`），在 math 中是 `MathNumber` 加 `MathIdent`。

`Lexer` 就是一个为此设计的手写状态机。它的全部状态只有三个字段：

```rust
pub(super) struct Lexer<'s> {
    s: Scanner<'s>,        // 游标：底层字符串 + 当前位置
    mode: SyntaxMode,      // 当前模式：Markup / Math / Code
    error: Option<(EcoString, EcoVec<EcoString>)>, // 当前 token 的错误信息槽
}
```

见 [lexer.rs:L14-L25](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs#L14-L25)。

一个容易误解的点：**Lexer 自己几乎不做模式切换决策**。它像一个「按当前模式工作的分词工人」，模式由 parser 在遇到模式边界（`#`、`$`、`[`、`]` 等）时通过 `set_mode` 下达。切换的主从关系在 4.2.3 第（5）点展开。

#### 4.2.2 核心流程

`Lexer::next()` 的分派逻辑（简化伪代码）：

```text
fn next():
    start = cursor
    c = eat 一个字符
    match c:
        None              -> End
        空白（按模式判定） -> whitespace()   # 三档：Parbreak / SpaceWithNewline / SpaceNoNewline
        '#' 且位于文件首且后跟 '!' -> shebang()
        "//"              -> line_comment()
        "/*"              -> block_comment()
        "*/"              -> 报错 token（块注释意外结束）
        '`' 且不在 math    -> raw()（整个原始文本块在词法层一次吃完）
        其他              -> 按当前模式分派:
            Markup -> markup(start, c)
            Math   -> math(start, c)   # 可能直接返回复合节点
            Code   -> code(start, c)

    text = 从 start 到游标的切片
    若 error 槽非空 -> 构造 Error 节点（消息 + 提示 + 原文）
    否则            -> 构造叶子节点(kind, text)
```

空白档位的判定规则（仅 markup 才可能产出 `Parbreak`）：

\[
\text{kind} =
\begin{cases}
\text{Parbreak} & \text{markup 且 } n_{\text{newlines}} \ge 2 \\
\text{SpaceWithNewline} & n_{\text{newlines}} \ge 1 \\
\text{SpaceNoNewline} & \text{其他}
\end{cases}
\]

#### 4.2.3 源码精读

**（1）主入口 `next()`。** [lexer.rs:L82-L118](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs#L82-L118)：先处理**所有模式共享**的前缀（空白、shebang、两种注释、`*/` 错误、反引号 raw），再在 [lexer.rs:L100-L107](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs#L100-L107) 按模式分派到 `markup`/`math`/`code` 三个函数。注意 math 分支可能返回复合节点（如 `[|` 简写），其余情况最后统一在 [lexer.rs:L112-L117](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs#L112-L117) 组装节点。

**（2）markup 分词：[lexer.rs:L474-L510](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs#L474-L510)。** 关键分支：

- `#` → 直接返回 `Hash`（[L491](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs#L491)）。**Lexer 不理解「进入代码模式」，它只交出一个 Hash token，由 parser 决定切换**；
- `=` → 先 `eat_while('=')` 吞掉全部等号，再要求后面是空白或结尾（`space_or_end()`，[L646-L651](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs#L646-L651)）才认定为 `HeadingMarker`，否则降级为普通文本（[L499-L502](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs#L499-L502)）。`-`、`+`、`/` 的列表/词条标记同理——这就是为什么 `a - b` 中的 `-` 不会变成列表符号；
- `*`、`_` → 仅当**不在单词内部**时才返回 `Star`/`Underscore`（[L488-L489](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs#L488-L489)）。`in_word()`（[L628-L644](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs#L628-L644)）判定两侧是否都是「字母数字且**不属于**汉字/假名/谚文」——CJK 字符不算「单词字符」，所以 `中*文*` 中的 `*` 正常生效为加粗，而英文 `snake_case_word` 里的 `_` 不会误触发斜体。这是 Typst 对中日韩文本友好的一个细节；
- `h` 开头且后跟 `ttp://`/`ttps://` → 进入 `link()` 识别超链接（[L478-L479](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs#L478-L479)）；
- 其余一切 → `text()`。

`text()`（[lexer.rs:L587-L626](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs#L587-L626)）是「反向思维」的典型：它不枚举文本字符，而是维护一张**停止表**（[L598-L602](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs#L598-L602)）——遇到可能开启特殊语法的字符（`\`、`[`、`~`、`-`、`.`、`'`、`"`、`*`、`_`、`:`、`h`、`` ` ``、`$`、`<`、`>`、`@`、`#`、空白）就停下。随后还有一个「继续合并」循环（[L604-L623](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs#L604-L623)）：如果停下的字符回头看其实还是文本（比如 `h` 后面不是 `ttp://`，`-` 后面不是 `--`/`-?`），就把它并入当前 Text 继续。这样 `http` 里最后那个 `p` 不会孤零零成词，`a-b` 保持为一个 Text。

**（3）code 分词：[lexer.rs:L864-L906](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs#L864-L906)。**

- **引号**：`"` → `string()`（[L1080-L1093](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs#L1080-L1093)），用 `escaped` 标志处理 `\"` 转义，吃到未转义的 `"` 为止，没吃到就报 "unclosed string"；
- **数字**：`0-9` 或 `.数字` 开头 → `number()`（[L975-L1078](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs#L975-L1078)），支持 `0x`/`0o`/`0b` 进制、小数点与 `e`/`E` 指数、以及单位后缀（合法集合为 `pt`/`mm`/`cm`/`in`/`deg`/`rad`/`em`/`fr`/`%`，[L1017-L1021](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs#L1017-L1021)），最终产出 `Int`/`Float`/`Numeric` 三档之一。注意 [L998-L1001](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs#L998-L1001) 特意排除 `..`（spread）和方法调用（`1.round()`）与小数点的混淆；
- **标识符**：`is_id_start` 开头 → `ident()`（[L953-L965](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs#L953-L965)），贪婪吞 `is_id_continue`（允许 `_` 和 `-`），然后查关键字表 `keyword()`（[L1097-L1123](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs#L1097-L1123)）。**关键字识别发生在词法层**——`for` 产出的直接是 `For` 而非 `Ident`。但有个精细例外（[L957-L961](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs#L957-L961)）：如果标识符紧跟在 `.` 或 `@` 后面（字段访问/引用目标），不查关键字表，所以 `a.for` 中的 `for` 保持 `Ident`；
- **运算符**：先匹配双字符组合（`==`、`!=`、`<=`、`+=`、`..`、`=>`…，[L872-L881](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs#L872-L881)），再匹配单字符。经典的「最长匹配优先」。

**（4）math 分词：[lexer.rs:L655-L791](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs#L655-L791)。** math 模式最独特：

- 一大段 `MathShorthand` 匹配表（[L661-L694](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs#L661-L694)），把 `->`、`=>`、`>=`、`<<`、`|-` 等几十种符号序列直接映射为单个 token；
- 数字（[L742-L750](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs#L742-L750)）只允许一个小数点，没有单位后缀；
- 最精彩的是「单字母 / 符号 / 标识符」三分（[L752-L788](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs#L752-L788)）：先测出「完整标识符长度」和「首个字素簇长度」，再比较——标识符比一个字素长 → `MathIdent`（如 `pi`，还可能由 `math_ident_or_field()`（[L793-L807](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs#L793-L807)）组装成 `arrow.r` 这类 `MathFieldAccess`）；恰好一个字素 → `MathLetter`（如 `x`）；不是合法标识符 → `MathSymbol`（按字素簇边界切一刀）。这就是数学模式「每个变量天然是斜体单符号」的词法根基。

**（5）谁在切换模式？parser。** Lexer 提供 `mode()`/`set_mode()`（[lexer.rs:L33-L41](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs#L33-L41)），但调用方是 parser：

- 创建 parser 时传入初始模式（[parser.rs:L1600-L1617](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/parser.rs#L1600-L1617)），顶层是 markup；
- parser 遇到模式边界（markup 中的 `Hash`、`Dollar`、`LeftBracket` 等，见 [parser.rs:L116-L126](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/parser.rs#L116-L126)）就调用 `enter_modes()`（[parser.rs:L1787-L1806](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/parser.rs#L1787-L1806)）：先保存旧模式 → `set_mode(新模式)` → 递归解析子结构 → 退出时恢复旧模式，并且**从当前 token 的起点重新词法分析**（[L1800-L1805](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/parser.rs#L1800-L1805)），因为跨界后的第一个 token 在旧模式下可能要重新分类。函数注释说得很形象："This function effectively repurposes the call stack as a stack of modes"——模式栈就借住在函数调用栈上；
- parser 的取词循环 `lex()`（[parser.rs:L1830-L1867](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/parser.rs#L1830-L1867)）在 code/math 下会把 trivia 收进节点表并跳过（[L1843-L1850](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/parser.rs#L1843-L1850)），同时记录换行信息用于 code 模式的「行末即语句结束」判定（临时 `End` token，[L1852-L1864](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/parser.rs#L1852-L1864)）。

**（6）空白判定的模式差异。** `is_space()`（[lexer.rs:L1144-L1152](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs#L1144-L1152)）在 markup 下只认空格、制表符和换行符；在 code/math 下认**一切 Unicode 空白**。于是不换行空格 U+00A0 在正文中是文本（正确！），在代码里则是可忽略空白。

**（7）raw 是词法层的小型解析器。** 整个 `` `...` `` 块（含三反引号、语言标签、去缩进）在 `raw()`（[lexer.rs:L175-L238](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs#L175-L238)）里一次产成一棵带 `RawDelim`/`RawLang`/`RawTrimmed`/`Text` 孩子的子树，parser 直接整个吃掉（[parser.rs:L116](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/parser.rs#L116) 注释："Raw is handled entirely in the Lexer"）。词法与语法的边界是工程选择，不是教条。

#### 4.2.4 代码实践

**实践目标**：亲眼验证「同一段文本在三种模式下 token 完全不同」。

1. 操作步骤：在 4.1.4 的 playground 里把 `main` 改为（**示例代码**）：

   ```rust
   use typst_syntax::{parse, parse_code, parse_math, SyntaxNode};

   fn leaves(node: &SyntaxNode) {
       if node.children().len() == 0 {
           println!("{:?}\t{:?}", node.kind(), node.leaf_text());
       } else {
           for child in node.children() {
               leaves(child);
           }
       }
   }

   fn main() {
       println!("--- markup: 1.2pt ---");
       leaves(&parse("1.2pt"));
       println!("--- code: 1.2pt ---");
       leaves(&parse_code("1.2pt"));
       println!("--- math: $1.2pt$ ---");
       leaves(&parse_math("1.2pt"));
   }
   ```

   `parse_code`/`parse_math` 的定义见 [parser.rs:L24-L39](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/parser.rs#L24-L39)，分别以 code/math 为初始模式解析整段文本。

2. 需要观察的现象：三列输出中 `1.2pt` 的分类。
3. 预期结果（依据源码推导，待本地验证）：
   - markup：一个 `Text("1.2pt")`——`numbering()` 要求 `数字.` 后是空白才构成 `EnumMarker`（[L552-L561](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs#L552-L561)），不满足就整体落入 `text()`；
   - code：一个 `Numeric("1.2pt")`——`number()` 吃下小数与合法单位后缀；
   - math：`MathNumber("1.2")` + `MathIdent("pt")`——数学数字无单位后缀，`pt` 是多字素标识符。

#### 4.2.5 小练习与答案

1. **练习**：`#let x = 1` 中的 `let` 是什么类别？写成 `a.let` 又如何？为什么？
   **答案**：前者是 `Let`（关键字表 [lexer.rs:L1106](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs#L1106)）；后者保持 `Ident`，因为标识符紧跟 `.`/`@` 时不查关键字表（[lexer.rs:L957-L961](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs#L957-L961)），否则字段访问会被误伤。
2. **练习**：`中*文*` 与 `a_b_c` 中，`*` 和 `_` 的处理结果有何不同？
   **答案**：`中*文*` 的 `*` 两侧是 CJK 字符，`in_word()` 判定不在单词内，产出 `Star`（加粗生效）；`a_b_c` 的 `_` 两侧是英文字母，在单词内部，直接按文本处理（[lexer.rs:L488-L489](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs#L488-L489) 与 [L628-L644](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs#L628-L644)）。
3. **练习**：`- item` 与 `3 - 1` 中的 `-` 分别是什么？判定条件是什么？
   **答案**：前者是 `ListMarker`，后者是文本/运算符的一部分。markup 中 `-` 仅在后面紧跟空白或结尾（`space_or_end()`）时才成为列表标记（[lexer.rs:L503](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs#L503)）；且 `-` 后跟数字时（如 `-1`）会被 `Shorthand` 分支拦截（[L487](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs#L487)）。

### 4.3 错误 token 化策略

#### 4.3.1 概念说明

Typst 的解析器有一条铁律：**解析永不失败**。无论输入多离谱，`parse()` 总是返回一棵覆盖全部原文的树。这对编辑器场景至关重要——用户敲到一半的代码必须还能高亮、补全、跳转。

实现这一点的关键是**错误 token 化**：词法错误不抛异常、不返回 `Result`，而是产出一个类别为 `Error` 的节点，它同时携带：

- 人可读的错误消息；
- 零到多条提示（hints，告诉用户怎么改）；
- 出错位置的原文文本。

树照常继续生长，诊断信息延后统一收集。

#### 4.3.2 核心流程

```text
Lexer 内部:
    error(msg)  -> 把 (msg, 空hints) 写入 self.error 槽, 返回 SyntaxKind::Error
    hint(msg)   -> 给 error 槽追加提示（仅当当前确实是错误）
next() 末尾:
    取出 error 槽:
        Some((msg, hints)) -> SyntaxNode::error(msg, 原文).with_hints(hints)
        None               -> 正常叶子 SyntaxNode::leaf(kind, 原文)
之后:
    parser 把 Error 节点当作普通 token 消费, 解析继续
诊断收集（按需）:
    root.errors_and_warnings() 遍历树, 收集全部错误与警告
```

#### 4.3.3 源码精读

**（1）错误槽机制。** [lexer.rs:L62-L76](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs#L62-L76)：`error()` 带断言保证一个 token 只登记一次错误；`hint()` 只在确有错误时追加。`next()` 的开头和节点组装处（[L83](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs#L83)、[L113-L115](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs#L113-L115)）用 `debug_assert` 和 `Option::take` 维护「token 之间槽必须为空」的不变量。

**（2）Error 节点是不变式的第三类。** `SyntaxNode` 有三类数据（叶子、内部、错误），[node.rs:L129-L140](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/node.rs#L129-L140) 的 `error()` 构造器把消息和原文一起存进去；而 `leaf()`/`inner()` 上的 `debug_assert!(!kind.is_error())`（[node.rs:L109-L127](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/node.rs#L109-L127)）保证「Error 类别只出现在 Error 节点里」（设计说明见 [node.rs:L35-L37](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/node.rs#L35-L37) 的注释）。

**（3）三类典型词法错误。**

- **顶层 `*/`**（[lexer.rs:L91-L98](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs#L91-L98)）：错误 "unexpected end of block comment"，附带两条 hint 建议转义 `*` 或补开 `/*`。注意这是所有模式共享的前缀分支——块注释的意外结束无论在哪都要报；
- **代码模式非法字符**（[lexer.rs:L908-L951](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs#L908-L951)）：这是「友好报错」的教科书。`&&` 报 "`&&` is not valid in code" 并提示 "in Typst, `and` is used for logical AND"；`||` 对应 `or`；`!` 提示用 `not` 或写成 `!=`；`~=` 提示用 `!=`；多余 `#` 会说 "you are already in code mode" 并建议删掉 `#`。每个分支都是「错误消息 + 猜测用户意图的 hint」组合；
- **未闭合字符串**（[lexer.rs:L1080-L1093](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs#L1080-L1093)）：吃到结尾仍没有未转义的引号，返回 "unclosed string"，错误节点的文本覆盖剩余全部输入。

此外数字后缀校验也在词法层完成：`12px` 会得到 "invalid number suffix: `px`"（[lexer.rs:L1014-L1077](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs#L1014-L1077)，注意该函数结尾按「数字错误 / 后缀错误」组合出消息与 hint）。

**（4）诊断的延迟收集。** [node.rs:L303-L325](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/node.rs#L303-L325) 的 `errors_and_warnings()` 遍历整棵树，把 Error 节点转成 `SyntaxDiagnostic`（字段含 `is_error`、`span`、`message`、`hints`，定义在 [node.rs:L908-L918](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/node.rs#L908-L918)）。CLI 与 IDE 最终展示的红色波浪线就从这里来。

#### 4.3.4 代码实践

**实践目标**：注入一个错误，观察它成为树的一部分而非让解析崩溃。

1. 操作步骤：在 playground 中（**示例代码**）：

   ```rust
   use typst_syntax::parse;

   fn main() {
       let root = parse("#a && b");
       let (errors, warnings) = root.errors_and_warnings();
       for e in errors {
           println!("error: {}", e.message);
           for h in &e.hints {
               println!("  hint: {}", h.v);
           }
       }
       println!("warnings: {}", warnings.len());
   }
   ```

2. 需要观察的现象：程序正常返回（没有 panic），打印出错误消息和 hint；同时 `parse` 仍返回了一棵完整的树（可用 4.1.4 的 `walk` 打印验证，Error 节点也会以 `Error` 类别出现在孩子列表中）。
3. 预期结果（待本地验证）：至少一条错误，消息为 "`&&` is not valid in code"，hint 为 "in Typst, `and` is used for logical AND"；可能还伴随 parser 层补充的错误（如对无法成形的表达式的报错）。

#### 4.3.5 小练习与答案

1. **练习**：为什么词法错误不用 `Result` 或 panic？
   **答案**：编辑器需要对「带病」的半成品代码继续提供高亮、补全、增量重解析（u2-l5）。错误 token 化保证任何输入都得到一棵覆盖全部原文的完整树，诊断延后由 `errors_and_warnings()` 统一收集。
2. **练习**：`hint()` 在什么情况下是空操作？
   **答案**：`self.error` 槽为 `None` 时（[lexer.rs:L70-L75](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs#L70-L75)），即当前 token 不是错误时，hint 被静默丢弃——提示必须依附于错误。
3. **练习**：`"abc`（未闭合字符串）的 Error 节点文本覆盖到哪里？
   **答案**：从开头引号到输入结束的全部剩余文本（`eat_until` 停在结尾后 `from(start)` 取整段），见 [lexer.rs:L1082-L1090](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs#L1082-L1090)。

## 5. 综合实践

把本讲三个模块串起来：**先预测、再验证、再破坏**。使用 4.1.4 搭好的 `lex-playground` 项目。

**任务输入**：`= Hi #for i in range(3) [*#i*]`

**第一步：手工预测 token 序列。** 逐字符过一遍 4.2 学到的规则，写出你预测的（类别, 文本）序列。参考答案（依据源码推导，待本地验证）：

| # | 类别 | 文本 | 依据 |
| --- | --- | --- | --- |
| 1 | `HeadingMarker` | `=` | `=` 后是空白，且位于行首 |
| 2 | `SpaceNoNewline` | ` ` | 单个空格，无换行 |
| 3 | `Text` | `Hi` | 停止表在空格处截断 |
| 4 | `SpaceNoNewline` | ` ` | |
| 5 | `Hash` | `#` | markup 中的 `#` → Hash，parser 随后切到 code |
| 6 | `For` | `for` | code 模式关键字 |
| 7 | `SpaceNoNewline` | ` ` | |
| 8 | `Ident` | `i` | 非关键字标识符 |
| 9 | `SpaceNoNewline` | ` ` | |
| 10 | `In` | `in` | 关键字 |
| 11 | `SpaceNoNewline` | ` ` | |
| 12 | `Ident` | `range` | |
| 13 | `LeftParen` | `(` | |
| 14 | `Int` | `3` | 无小数点无后缀 |
| 15 | `RightParen` | `)` | |
| 16 | `SpaceNoNewline` | ` ` | |
| 17 | `LeftBracket` | `[` | code 模式 `[` → 内容块开始，parser 切回 markup |
| 18 | `Star` | `*` | markup 中且不在单词内 |
| 19 | `Hash` | `#` | 又一次切到 code |
| 20 | `Ident` | `i` | |
| 21 | `Star` | `*` | 退出 code 后重新词法分析仍为 Star |
| 22 | `RightBracket` | `]` | 内容块结束 |

**第二步：程序验证。** 用 4.1.4 的 `leaves` 遍历 `parse("= Hi #for i in range(3) [*#i*]")`（注意 main 里字符串转义），逐行对照上表。关注两点：叶子序列是否与预测一致；树中还有 `Heading`、`ForLoop`、`FuncCall`、`Args`、`ContentBlock`、`Strong` 等**内部节点**（parser 层产物，正是下一讲主题）。

**第三步：破坏实验。** 做两处修改并各跑一次：

1. 把 `[*#i*]` 改成 `[_#i_]`——预期 `_` 的处理与 `*` 对称（`Underscore`→`Emph`）；
2. 在文档末尾追加 ` #x && y`——预期出现 4.3 的 `&&` 错误 token，且树中其余部分不受影响。

**验收标准**：能解释表中每一行的「依据」列；能指出哪些类别来自 Lexer、哪些来自 Parser；能描述错误如何留在树中而不中断解析。

## 6. 本讲小结

- `SyntaxKind` 是「一枚枚举、两层用途」：Lexer 产出叶子 token 类别（`Star`、`Int`、`For`），Parser 产出内部节点类别（`Strong`、`FuncCall`、`ForLoop`）；`Marker` 后缀是前导记号的标志。
- `Lexer` 是无正则、全手写的状态机，核心状态只有「游标 + 模式 + 错误槽」；`next()` 先处理跨模式共享的前缀（空白、注释、raw、`*/`），再按 markup/math/code 分派。
- 同一字符在三种模式下分类不同（`1.2pt` → Text / Numeric / MathNumber+MathIdent）；空白语义也是模式相关的（`Parbreak` 仅 markup 产出，且需要至少两个换行）。
- 模式切换由 **parser 通过 `set_mode`/`enter_modes` 下达**，Lexer 只是执行者；退出子模式时会从 token 边界重新词法分析。
- 关键字识别发生在词法层，但 `.`/`@` 之后的标识符豁免；`*`/`_` 是否切换格式取决于 `in_word()`，其中 CJK 字符不算单词字符。
- 词法错误不中断解析：错误消息与 hint 写入错误槽，`next()` 末尾组装成 `SyntaxNode::error` 节点，诊断延后由 `errors_and_warnings()` 收集——「解析永不失败」是编辑器友好性的根基。

## 7. 下一步学习建议

本讲的 Lexer 只负责「切词」，而 `Hash`、`LeftBracket`、`Star` 这些 token 如何被组装成 `ForLoop`、`Strong`、`ContentBlock`，是下一讲 **u2-l3《语法树：Parser 与 SyntaxNode》** 的主题：精读 [parser.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/parser.rs) 的递归下降主循环与 [node.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/node.rs) 的树结构（孩子分节点/文本两类、`number` 压缩表示）。在阅读之前，建议先完成本讲综合实践——你已经在这棵树里见过 parser 包装出来的节点了。之后再进入 u2-l4 的 AST 类型化视图与 Span 定位，以及 u2-l5 的增量重解析（它会复用本讲的 `SyntaxKind` 与模式知识）。

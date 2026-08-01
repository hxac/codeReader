# SyntaxKind 枚举全貌

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 `SyntaxKind` 在 typst-syntax 里扮演什么角色——为什么词法和语法要共用同一套「标签」。
- 看懂 `kind.rs` 里这个枚举的定义形式，理解 `#[repr(u8)]` 带来的紧凑表示，以及当前 137 个变体与「数量上限」的关系。
- 把 137 个变体按 **markup / math / code / 通用** 四类归位，并能解释每一类的含义。
- 判断一个 `SyntaxKind` 是由 **lexer 产生**（token）、**parser 产生**（节点），还是两者都可能产生，并掌握用命名规律快速分辨的方法。

本讲只读 `src/kind.rs` 一个核心文件，是后续词法（U3）、语法（U4）、CST 数据结构（U5）等讲义的共同词汇表。

## 2. 前置知识

在学习本讲前，你需要先建立以下几个概念（它们在 u1-l1 与 u1-l3 已经讲过，这里简要复习）：

- **Token（词法单元）**：词法分析器（lexer）从源码文本里切出的最小片段，例如一个 `+`、一个标识符 `it`、一段注释。
- **CST（具体语法树）**：typst-syntax 维护的「无损」语法树，树上的每一个节点都对应源码里的一段文本，连空白、注释都保留。CST 是 typst-syntax 的「唯一真相来源」。
- **AST（抽象语法树）**：建立在 CST 之上的、按需转换的类型化视图，服务于下游求值。
- **SyntaxMode**：Typst 的三种语法模式——`Markup`（正文）、`Math`（公式）、`Code`（`#` 之后的代码）。同一个字符在不同模式下可能产生完全不同的 token。

CST 里的每个节点都需要一个「标签」来说明「我是什么」。这个标签的类型就是 `SyntaxKind`。本讲要回答的核心问题是：**这套标签到底有哪些？它们是怎么组织起来的？**

## 3. 本讲源码地图

| 文件 | 作用 | 本讲如何使用 |
| --- | --- | --- |
| [`src/kind.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs) | 定义 `SyntaxKind` 枚举及其判定方法 | **本讲唯一精读文件**，全部分类依据都来自这里 |
| `src/parser.rs` | 语法分析器 | 仅引用一处注释（`Raw` 完全由 lexer 处理）来佐证「谁产生 kind」 |
| `src/set.rs` | `SyntaxSet` 位集 | 仅引用其 `u128` 实现来解释「数量上限」的工程后果，详细留给 u2-l3 |
| `src/lexer.rs` | 词法分析器 | 本讲不深入，相关细节在 U3 讲解 |

## 4. 核心概念与源码讲解

本讲围绕唯一一个最小模块展开：**SyntaxKind 枚举定义**。我们把它拆成「是什么 → 怎么定义 → 分几类 → 谁产生」四步，最后给出代码实践与练习。

### 4.1 概念说明：SyntaxKind 是贯穿词法与语法的统一词汇表

先看 `kind.rs` 顶部的文档注释，它一句话点明了这个类型的双重身份：

> A syntactical building block of a Typst file.
>
> Can be created by the lexer or by the parser.
>
> —— [src/kind.rs:3-L5](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L3-L5)

这句话有两层含义，理解它就理解了 `SyntaxKind` 的设计精髓：

1. **它既是「token 的种类」，也是「CST 节点的种类」**。在很多编译器里，词法 token 和语法树节点是两套独立的类型；typst-syntax 借鉴 rust-analyzer 的做法，**让两者共用同一个枚举**。于是 lexer 产出的 token 流和 parser 构建的 CST 树，用的是同一套词汇表，无缝衔接。
2. **某些具体的变体确实可能来自两边**。例如 `Error`：lexer 遇到非法字符序列会产出一个 `Error` token，parser 遇到意料之外的 token 也会包出一个 `Error` 节点（详见 4.4）。

直觉上，你可以把 `SyntaxKind` 想象成一本「零件目录」：lexer 和 parser 都从这本目录里挑零件，只是 lexer 多挑「最小的原子零件」，parser 多挑「组装好的总成零件」。

### 4.2 核心流程：枚举定义与 `#[repr(u8)]` 紧凑表示

枚举的定义头如下：

```rust
#[derive(Debug, Copy, Clone, Eq, PartialEq, Hash)]
#[repr(u8)]
pub enum SyntaxKind {
    /// The end of token stream.
    End,
    /// An invalid sequence of characters.
    Error,
    // ... 共 137 个变体 ...
}
```

对应 [src/kind.rs:6-L8](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L6-L8)。

两个关键点：

- **`#[repr(u8)]`**：强制让每个变体在内存里用一个 `u8`（1 字节）表示。Rust 会按声明顺序从 0 开始给每个变体分配一个判别值（discriminant）：`End = 0`、`Error = 1`、`Shebang = 2`、…… 最后一个 `DestructAssignment = 136`。
- **派生了 `Copy + Clone + Eq + PartialEq + Hash`**：意味着 `SyntaxKind` 是一个轻量、可比较、可哈希的值类型，可以放心地到处复制、放进集合、用作 match 的 key。

#### 为什么必须紧凑？

因为 **CST 上的每一个节点都要存一个 `SyntaxKind`**。一篇文档动辄几千、上万个节点，如果每个节点的 kind 占 8 字节，树的整体内存就会明显膨胀。用 `u8` 把它压到 1 字节，是 typst-syntax 控制 CST 内存占用的基础手段之一。

#### 变体数量与「上限」

精确数一下：当前 `SyntaxKind` 一共有 **137 个变体**（判别值 `0..=136`）。

- 相对 `u8` 的上限 256：还留有充足余量，新增语法构造时不必担心溢出。
- 但要小心另一个隐藏的上限：本 crate 里还有一个用 `u128` 实现的位集 `SyntaxSet`（用来表示「一组 kind」），它只能容纳判别值 `< 128` 的 kind。其定义与约束在 [`src/set.rs:9`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/set.rs#L9) 与 [`src/set.rs:20-L21`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/set.rs#L20-L21)：

```rust
pub struct SyntaxSet(u128);
// ...
/// You can only add kinds with discriminator < 128.
pub const fn add(self, kind: SyntaxKind) -> Self {
    assert!((kind as u8) < BITS);
    // ...
}
```

因为 137 > 128，所以**排在最后面的 9 个变体**（`ImportItems`、`ImportItemPath`、`RenamedImportItem`、`ModuleInclude`、`LoopBreak`、`LoopContinue`、`FuncReturn`、`Destructuring`、`DestructAssignment`）目前**无法被放进任何 `SyntaxSet`**。这是一个真实的、可验证的工程后果——它提醒我们：**新增 kind 并非零成本，越靠后的变体会受到 `SyntaxSet` 容量的约束**。位集的细节留到 u2-l3 展开，这里只需记住这个数量关系。

### 4.3 全变体分类总览：四张表

下面把 137 个变体按 **通用 / Markup / Math / Code** 四类列成四张表。分类的主要依据是每个变体上方 [src/kind.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs) 里的文档注释（它给出了每个 kind 对应的 Typst 语法片段），以及命名规律。

「产生方」一列的含义见 4.4：**lexer** = 词法器产出的 token；**parser** = 语法器组装的节点；**both** = 两边都可能产生。

#### 表 A：通用类（跨模式，不属于任一特定模式）

| 变体 | 含义 | 产生方 |
| --- | --- | --- |
| `End` | token 流的结束哨兵 | lexer（永不进入最终 CST） |
| `Error` | 非法字符序列或意外 token | **both** |
| `Shebang` | `#! ...` | lexer |
| `LineComment` | `// ...` 行注释 | lexer |
| `BlockComment` | `/* ... */` 块注释 | lexer |
| `Space` | 空白（markup 中至多含一个换行） | lexer |
| `Parbreak` | 段落分隔（一个或多个空行） | lexer |

> 说明：这些大多是「trivia」——注释、空白、段落分隔——在 code/math 模式下会被 parser 自动跳过。`Error` 是唯一的例外，lexer 和 parser 都可能制造它。详见 [src/kind.rs:9-L19](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L9-L19) 与 `is_trivia` 判定 [src/kind.rs:369-L378](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L369-L378)（`is_trivia` 等分类方法本身是 u2-l2 的主题）。

#### 表 B：Markup 专属（正文模式）

对应 [src/kind.rs:21-L74](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L21-L74)。

| 变体 | 含义 | 产生方 |
| --- | --- | --- |
| `Markup` | 文件或 content block 的正文容器 | parser |
| `Text` | 纯文本 | **both**（lexer 产文本；parser 也会把落单的 marker/冒号 convert 成 Text） |
| `Linebreak` | 强制换行 `\` | lexer |
| `Escape` | 转义 `\#`、`\u{1F5FA}` | lexer |
| `Shorthand` | 简写，如 `~`（不换行空格）、`-?`（软连字符） | lexer |
| `SmartQuote` | 智能引号 `'` / `"` | lexer |
| `Strong` | `*Strong*` 加粗 | parser |
| `Emph` | `_Emph_` 强调 | parser |
| `Raw` / `RawLang` / `RawDelim` / `RawTrimmed` | 原始文本及其语言标签、反引号定界符、待忽略空白 | **lexer**（详见下方说明） |
| `Link` | 超链接 `https://typst.org` | lexer |
| `Label` | `<intro>` 标签 | lexer |
| `Ref` / `RefMarker` | 引用 `@target[..]` 及其引导标记 | parser / lexer |
| `Heading` / `HeadingMarker` | `= Introduction` 标题及引导符 `=` | parser / lexer |
| `ListItem` / `ListMarker` | `- ...` 列表项及引导符 | parser / lexer |
| `EnumItem` / `EnumMarker` | `+ ...` 或 `1. ...` 枚举项及引导符 | parser / lexer |
| `TermItem` / `TermMarker` | `/ Term: Details` 术语项及引导符 | parser / lexer |

> 规律：凡是以 **`*Marker` 结尾**的（`HeadingMarker`、`ListMarker`、`EnumMarker`、`TermMarker`、`RefMarker`）都是 lexer 产出的「引导 token」，parser 看到它后才把后续内容包成对应的 `Heading`/`ListItem`/... 节点。

> 关于 `Raw`：它看起来像个「总成节点」，但实际**几乎完全由 lexer 在 markup 模式下一次性产出**（包含定界符、语言标签、正文、裁剪空白等子结构）。parser 里对应它的分支只有一行——`p.eat()`，并附有明确注释：`// Raw is handled entirely in the Lexer.`（见 [src/parser.rs:114](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L114)）。这是「同一个 kind 由 lexer 产出、parser 直接吃掉」的典型例子，原始文本的词法细节在 u3-l3 展开。

#### 表 C：Math 专属（公式模式）

对应 [src/kind.rs:76-L103](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L76-L103)。

| 变体 | 含义 | 产生方 |
| --- | --- | --- |
| `Equation` | `$x$`、`$ x^2 $` 整个公式块 | parser |
| `Math` | 公式块内部的内容容器 | parser |
| `MathText` | 公式里的孤立文本片段，如 `x`、`25`、`=`、`|` | lexer |
| `MathIdent` | 公式里的标识符，如 `pi` | lexer |
| `MathFieldAccess` | 字段访问，如 `arrow.r.long.double.bar` | lexer（lexer 做前瞻识别） |
| `MathShorthand` | 公式里的简写，如 `<=` | lexer |
| `MathAlignPoint` | 对齐点 `&` | lexer |
| `MathCall` / `MathArgs` | 公式里的函数调用及其参数 | parser |
| `MathDelimited` | 匹配定界符 `[x + y]` | parser |
| `MathAttach` | 带上下标附着，如 `a_1^2` | parser |
| `MathPrimes` | 成组撇号，如 `a'''` | lexer |
| `MathFrac` | 分式 `x/2` | parser |
| `MathRoot` | 根号 `√x`、`∛x`、`∜x` 对应的**节点** | parser |

> 注意区分两对容易混淆的 kind：
> - **`Root`（[src/kind.rs:171-L172](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L171-L172)）** vs **`MathRoot`**：前者是 lexer 产出的 `√` 字符 token；后者是 parser 用 `p.wrap(..., SyntaxKind::MathRoot)` 包出来的「根号表达式」节点（见 [src/parser.rs:317-L322](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L317-L322)）。
> - **`MathPrimes`（lexer 的撇号 token）** 会被 parser 当作附着算符消费，最终包进 `MathAttach` 节点（见 `math_op` 的判别 [src/parser.rs:404-L417](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L404-L417)）。

#### 表 D：Code 专属（`#` 之后的代码模式）

这一类变体最多，可再细分。对应 [src/kind.rs:105-L294](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L105-L294)。

**D-1 标点与分隔符（lexer）** — [src/kind.rs:105-L174](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L105-L174)

| 变体 | 含义 | 产生方 |
| --- | --- | --- |
| `Hash` `LeftBrace` `RightBrace` `LeftBracket` `RightBracket` `LeftParen` `RightParen` `Comma` `Semicolon` `Colon` | 各种括号、逗号、分号、冒号、`#` | lexer |
| `Star` `Underscore` `Dollar` | 多义符号（`*`/`_`/`$`，具体语义由 parser 结合上下文决定） | lexer |
| `Plus` `Minus` `Slash` `Hat` `Dot` `Eq` `EqEq` `ExclEq` `Lt` `LtEq` `Gt` `GtEq` `PlusEq` `HyphEq` `StarEq` `SlashEq` `Dots` `Arrow` `Root` `Bang` | 各种运算符 | lexer |

**D-2 关键字 token（lexer）** — [src/kind.rs:176-L215](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L176-L215)

| 变体 | 含义 | 产生方 |
| --- | --- | --- |
| `Not` `And` `Or` `None` `Auto` | 运算符与字面量关键字 | lexer |
| `Let` `Set` `Show` `Context` `If` `Else` `For` `In` `While` `Break` `Continue` `Return` `Import` `Include` `As` | 各种关键字 | lexer |

> 规律：这里每一个都是 **lexer 识别出的关键字 token**。parser 看到 `Let` token 后，才会去组装一个 `LetBinding` **节点**；看到 `If` token 才组装 `Conditional` 节点；看到 `Return` token 才组装 `FuncReturn` 节点。也就是说，「关键字 token」和「语句节点」是两套不同的 kind，分别由 lexer 与 parser 产出。

**D-3 字面量与叶子（lexer）** — [src/kind.rs:217-L230](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L217-L230)

| 变体 | 含义 | 产生方 |
| --- | --- | --- |
| `Ident` | 标识符 `it` | lexer |
| `Bool` | 布尔 `true`/`false` | lexer |
| `Int` `Float` `Numeric` | 整数、浮点、带单位数值 `12pt` | lexer |
| `Str` | 字符串 `"..."` | lexer |

**D-4 结构节点（parser）** — [src/kind.rs:231-L294](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L231-L294)

| 变体 | 含义 | 产生方 |
| --- | --- | --- |
| `Code` | 代码块内容容器 | parser |
| `CodeBlock` `ContentBlock` `Parenthesized` | 代码块 `{}、`内容块 `[]`、分组 `()` | parser |
| `Array` `Dict` `Named` `Keyed` | 数组、字典、命名对、键值对 | parser |
| `Unary` `Binary` `FieldAccess` `FuncCall` `Args` `Spread` | 一元/二元运算、字段访问、函数调用及其参数、展开 | parser |
| `Closure` `Params` | 闭包及其参数列表 | parser |
| `LetBinding` `SetRule` `ShowRule` `Contextual` `Conditional` `WhileLoop` `ForLoop` | 各类语句/控制流节点 | parser |
| `ModuleImport` `ImportItems` `ImportItemPath` `RenamedImportItem` `ModuleInclude` | 导入/包含相关节点 | parser |
| `LoopBreak` `LoopContinue` `FuncReturn` | `break`/`continue`/`return` 对应的语句节点（注意与 D-2 的关键字 token 区分） | parser |
| `Destructuring` `DestructAssignment` | 解构模式与解构赋值 | parser |

### 4.4 源码精读：谁产生这些 kind

把 4.3 四张表里的「产生方」汇总，可以得到三条清晰的经验法则。它们正是本讲学习目标 3 想要你建立的判断力。

#### 法则一：lexer 负责「原子 token」

凡是**单个字符或固定字符串就能确定的 token**，都由 lexer 产出，包括：

- 所有 trivia（注释、空白、`Shebang`、`Parbreak`）；
- 所有标点、括号、运算符；
- 所有关键字 token（`Let`、`If`、`Return`、……）；
- 所有字面量（`Int`、`Str`、`Bool`、……）；
- 所有 markup/math 的「叶子原子」（`Text`、`Escape`、`MathText`、`MathIdent`、`MathShorthand`、……）；
- 所有「`*Marker`」引导符（`HeadingMarker`、`ListMarker`、……）。

#### 法则二：parser 负责「结构节点」

凡是**需要看多个 token、按语法规则组装出来**的节点，都由 parser 产出，包括：

- 三大容器 `Markup` / `Code` / `Math`；
- markup 的包裹节点 `Strong`、`Emph`、`Heading`、`ListItem`、`Ref`、`Equation`；
- math 的算符节点 `MathRoot`、`MathAttach`、`MathFrac`、`MathDelimited`、`MathCall`；
- code 的所有复合表达式与语句节点 `FuncCall`、`Closure`、`LetBinding`、`Conditional`、……。

parser 组装节点的方式典型地是「先打 marker，吃掉若干 token，再用 `p.wrap(...)` 把这段包成目标 kind」。例如根号节点：

```rust
SyntaxKind::Root => {
    p.eat();
    let m2 = p.marker();
    math_expr_prec(p, MATH_ROOT_PREC, syntax_set!());
    math_unparen(p, m2);
    p.wrap(m, SyntaxKind::MathRoot);   // ← parser 在这里「造」出 MathRoot 节点
}
```

见 [src/parser.rs:317-L322](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L317-L322)。而哪个算符该包成哪种 kind，由 `math_op` 集中判别（[src/parser.rs:404-L417](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L404-L417)）：`Slash → MathFrac`、`Hat/Underscore → MathAttach`、`Root → MathRoot`。

#### 法则三：少数 kind 两边都可能产生

- **`Error`**：lexer 遇到非法字符序列返回 `Error` token；parser 的错误恢复（`unexpected`/`expected`）也会产出 `Error` 节点。
- **`Text`**：lexer 直接产出正文文本；parser 还会把「落单的 marker 或冒号」用 `convert_and_eat(SyntaxKind::Text)` 转成普通文本（见 [src/parser.rs:126-L130](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L126-L130)）。
- **`Raw`**：虽然几乎完全由 lexer 产出（[src/parser.rs:114](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L114) 注释明确说 "handled entirely in the Lexer"），但它本身是个带子结构的节点，是「lexer 也能产出结构化 kind」的代表案例。

> 这三条法则印证了 [src/kind.rs:3-L5](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L3-L5) 那句文档：**`SyntaxKind` 既可由 lexer 产生，也可由 parser 产生**。

#### 拓展视角：用 `mode_after` 看每个 kind 属于哪个模式

`kind.rs` 里还有一个（crate 内部私有的）`mode_after` 实现（[src/kind.rs:565-L725](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L565-L725)），它对**每一个** kind 都标明了「这个 kind 之后会进入哪种 `SyntaxMode`」。换句话说，它把 137 个变体按 `Markup`/`Math`/`Code`/`Parent`（随父节点）/`None` 又做了一次分组。这是一个很好的「交叉验证」视角：你可以拿它对照 4.3 的四张表，加深对每个 kind 归属的理解。该机制的完整意义在后续讲义（CST 的 `mode_after`）展开，这里只需知道它存在。

### 4.5 代码实践

**实践目标**：亲手把 137 个变体归到 markup / math / code / 通用 四类，并标出产生方，从而检验你对本讲的掌握。

**操作步骤**：

1. 打开 [src/kind.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs)，逐段阅读每个变体上方的文档注释。
2. 按本讲 4.3 的四张表，自己重画一份空白表，把每个变体填进去，并标注产生方（lexer / parser / both）。
3. 用下面的命名规律快速自检：
   - `*Marker` 结尾 → 引导 token → lexer；
   - 关键字 token（`Let`、`If`、`Return`、……）→ lexer，对应语句节点（`LetBinding`、`Conditional`、`FuncReturn`、……）→ parser；
   - 括号 / 运算符 / 字面量 / 叶子原子 → lexer；
   - 容器与复合结构（`Markup`、`FuncCall`、`MathAttach`、……）→ parser。
4. 运行下面这段**示例代码**（非项目原有代码），抽查各类代表，确认你的分类与 `name()`、`is_*` 的实际行为一致。

```rust
// 示例代码（非项目原有）：在仓库外新建一个小 Rust 项目运行
// Cargo.toml 里加上对 typst-syntax 的依赖（path 或 git 均可）
use typst_syntax::SyntaxKind::*;

fn main() {
    // 每行：(分类标签, 该类下的代表 kind 列表)
    let samples: &[(&str, &[typst_syntax::SyntaxKind])] = &[
        ("通用/trivia",  &[Space, LineComment, Parbreak]),
        ("markup-leaf", &[Text, Escape, Shorthand, Link, HeadingMarker]),
        ("markup-node", &[Strong, Heading, ListItem, Ref]),
        ("math-leaf",   &[MathText, MathIdent, MathShorthand, MathAlignPoint]),
        ("math-node",   &[MathRoot, MathAttach, MathFrac, MathCall]),
        ("code-token",  &[Let, If, Return, Plus, EqEq, Int, Str]),
        ("code-node",   &[LetBinding, Conditional, FuncReturn, FuncCall]),
    ];

    for (label, kinds) in samples {
        for k in *kinds {
            println!(
                "{:<13} | {:<22} | name={:<26} | trivia={} kw={} stmt={}",
                label,
                format!("{k:?}"),
                k.name(),
                k.is_trivia(),
                k.is_keyword(),
                k.is_stmt(),
            );
        }
    }
}
```

> 提示：`name()` 的定义在 [src/kind.rs:386-L526](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L386-L526)，`is_trivia`/`is_keyword`/`is_stmt` 等判定方法在 [src/kind.rs:297-L383](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L297-L383)（这些方法本身是 u2-l2 的主题）。

5.（可选）跑一个本 crate 已有的测试，感受这些 kind 在真实解析里如何出现：

```bash
cargo test -p typst-syntax --lib kind::test
```

**需要观察的现象**：

- 每个代表 kind 的 `name()` 输出是否与你在 4.3 表格里写的人类可读名一致；
- `Let` / `If` / `Return` 的 `is_keyword()` 是否为 `true`，而 `LetBinding` / `Conditional` / `FuncReturn` 的 `is_keyword()` 是否为 `false`、`is_stmt()` 中 `LetBinding` 是否为 `true`；
- `Space` / `LineComment` 的 `is_trivia()` 是否为 `true`。

**预期结果**：程序会按分类打印一张对照表，`name()` 列与 4.3 表格吻合；关键字 token 与语句节点的 `is_*` 差异正好印证「token 由 lexer、节点由 parser」的分工。具体的逐行输出**待本地验证**（取决于你是否配置好依赖并运行）。

### 4.6 小练习与答案

**练习 1**：`Root` 和 `MathRoot` 是同一个东西吗？分别由谁产生？

> **参考答案**：不是。`Root`（[src/kind.rs:171-L172](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L171-L172)）是 lexer 产出的 `√` / `∛` / `∜` 字符 token；`MathRoot` 是 parser 在解析完根号及其被开方表达式后，用 `p.wrap(m, SyntaxKind::MathRoot)` 组装出来的「根号表达式」节点（[src/parser.rs:317-L322](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L317-L322)）。前者是叶子 token，后者是结构节点。

**练习 2**：判断下列 kind 各由谁产生，并说明依据：`HeadingMarker`、`Heading`、`Let`、`LetBinding`、`FuncReturn`、`Raw`。

> **参考答案**：
> - `HeadingMarker`：lexer（`*Marker` 引导符）。
> - `Heading`：parser（看到 `HeadingMarker` 后组装的标题节点）。
> - `Let`：lexer（关键字 token）。
> - `LetBinding`：parser（看到 `Let` 后组装的绑定语句节点）。
> - `FuncReturn`：parser（注意它和关键字 token `Return` 不同，是 `return` 对应的语句节点）。
> - `Raw`：lexer（parser 里只有 `p.eat()`，注释说明 "handled entirely in the Lexer"，[src/parser.rs:114](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L114)）。

**练习 3**：为什么说新增一个排在枚举末尾的 `SyntaxKind` 变体可能带来隐藏成本？

> **参考答案**：因为 `SyntaxKind` 用 `u8` 表示，理论上限是 256 个变体；当前已有 137 个，距 256 尚有余量。但 crate 里的 `SyntaxSet` 位集基于 `u128`，只能容纳判别值 `< 128` 的 kind（[src/set.rs:9](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/set.rs#L9)、[src/set.rs:20-L21](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/set.rs#L20-L21)）。目前已有 9 个变体（判别值 128–136）无法进入任何 `SyntaxSet`；如果继续在末尾追加新 kind，受影响的范围会更大。所以新增 kind 时要考虑它在 `SyntaxSet` 中的可表达性。

## 5. 综合实践

把本讲的知识串起来，完成下面这个「标注任务」。

给定一段 Typst 源码：

```typst
= 标题
- 列表项，带 *加粗* 和 `code`
#let f(x) = x + 1
$ a^2 + b = c $
```

请按下列步骤操作：

1. 用 `Source::detached(text)` 解析它（回顾 u1-l2/u1-l4 的做法），遍历 `root().descendants()`，打印每个节点的 `kind()`。
2. 对打印出的每一个 kind，在一张表里写出：它属于 markup / math / code / 通用 哪一类；产生方是 lexer 还是 parser。
3. 重点验证这些对应关系是否在树里真实出现：
   - `HeadingMarker`（`=`）被包进 `Heading`；
   - `ListMarker`（`-`）被包进 `ListItem`；
   - `Star` token 被包进 `Strong`；
   - `Let` token 出现在 `LetBinding` 内；
   - `Hat`（`^`）参与构成 `MathAttach`。
4. 若某处分类与你的预期不符，回到 [src/kind.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs) 查阅该变体的文档注释，修正理解。

这个任务同时用到了「枚举全貌」「分类」「谁产生 kind」三部分知识，是本讲最好的收尾练习。遍历与解析结果**待本地验证**。

## 6. 本讲小结

- `SyntaxKind` 是 typst-syntax 里**贯穿词法与语法的统一标签类型**：lexer 产出的 token 和 parser 构建的 CST 节点共用同一个枚举（[src/kind.rs:3-L8](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L3-L8)）。
- 枚举用 `#[repr(u8)]` 紧凑表示，当前共 **137 个变体**（判别值 0–136），既省内存，又满足「装进每个 CST 节点」的需求。
- 137 个变体可归为 **通用 / Markup / Math / Code** 四大类；通用类多为 trivia，三类专属变体各自描述对应模式下的语法构造。
- 产生方有三条经验法则：**lexer 产原子 token**（trivia、标点、运算符、关键字、字面量、叶子、`*Marker`）；**parser 产结构节点**（容器、包裹、复合表达式、语句）；**少数 kind 两边都可能产生**（`Error`、`Text`、`Raw`）。
- 命名规律是快速分辨的钥匙：`*Marker` 是 lexer 的引导 token；关键字 token（`Let`/`If`/`Return`）对应 parser 的语句节点（`LetBinding`/`Conditional`/`FuncReturn`）；`Root` token 对应 `MathRoot` 节点。
- 数量上限有真实工程后果：`SyntaxSet` 基于 `u128`，只能容纳判别值 `< 128` 的 kind，目前末尾 9 个变体无法进入任何 `SyntaxSet`。

## 7. 下一步学习建议

- **下一步学 u2-l2（SyntaxKind 的分类方法）**：本讲多次提到的 `is_trivia`/`is_keyword`/`is_stmt`/`is_block`/`is_grouping`/`name()` 等方法，将在那里系统讲解，它们是 parser 与 highlight 复用这套词汇表的桥梁。
- **随后学 u2-l3（SyntaxSet 位集）**：深入理解本讲提到的 `u128` 位集、`syntax_set!` 宏与预定义集合，弄清「为什么 137 个变体会与 `< 128` 产生张力」。
- **之后再进入 U3（Lexer）**：届时你会亲眼看到这些 lexer 产出的 token 是如何被逐字符切出来的，本讲的分类表将成为最好的索引。
- **延伸阅读**：若想提前理解 parser 的「marker + wrap」组装方式，可先扫一眼 [src/parser.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs) 中 `strong()`、`heading()` 等函数，作为 U4 的预习。

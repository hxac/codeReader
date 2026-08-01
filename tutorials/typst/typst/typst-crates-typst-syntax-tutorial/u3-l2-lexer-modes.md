# Markup / Code / Math 三模式词法

## 1. 本讲目标

上一讲（u3-l1）我们认识了 `Lexer` 的骨架：它是一个有状态迭代器，靠 `mode` 字段决定如何分派字符，靠 `newline`/`error` 两块状态辅助解析。本讲把镜头对准那个 `mode` 字段，回答一个核心问题：

> 同一个字符，为什么在 Typst 的不同位置会被切成完全不同的 token？

例如 `-` 在正文里是普通文字、在 `#` 后的代码里是减号、在公式 `$...$` 里是数学减号；`"` 在正文里是「智能引号」，在代码里却是字符串的起止符。学完本讲你应当能够：

- 说清 `SyntaxMode`（Markup / Code / Math）如何决定 `Lexer` 的分派路径；
- 区分 `markup()`、`code()`、`math()` 三个分派函数各自的产物；
- 理解空白与注释等「与模式无关」的逻辑为何被抽到共享方法里；
- 知道 `math()` 有时会附带返回一棵 `SyntaxNode` 子树，这是它与另外两个模式的本质区别。

## 2. 前置知识

- **SyntaxMode 枚举**：在 [lib.rs:43-50](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lib.rs#L43-L50) 定义，三个变体 `Markup`（正文/顶层）、`Math`（公式）、`Code`（`#` 后的代码）。它决定了同一字符的词法规则。
- **Lexer 状态**（u3-l1）：`mode` 决定分派、`newline` 记录上个 token 是否含换行、`error` 暂存当前 token 的错误与提示。
- **SyntaxKind 词汇表**（U2）：词法和语法共用同一套 `SyntaxKind` 标签，例如 `Text`、`Minus`、`MathShorthand`、`Star`、`SmartQuote` 都是其中变体。
- **一个重要事实**：`Lexer` 被声明为 `pub(super) struct`，且 `lexer` 模块在 [lib.rs:8](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lib.rs#L8) 是私有 `mod`。也就是说，**crate 外部无法直接构造 `Lexer`**。要「按指定模式词法化」一段文本，只能借助三个公开入口 `parse` / `parse_code` / `parse_math`（分别固定为 Markup / Code / Math 模式），它们内部才会 `new` 出 `Lexer`。本讲的代码实践正是基于这一点设计的。

## 3. 本讲源码地图

本讲只读一个文件，但它体量较大（1277 行），按职责分成几段：

| 源码区域 | 作用 |
|---|---|
| [lexer.rs:95-132](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L95-L132) `next()` | 模式分派总枢纽：先处理空白/注释/raw 等公共前缀，再按 `mode` 调用三个分派函数。 |
| [lexer.rs:488-665](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L488-L665) `markup()` 一族 | Markup 模式：文本、智能引号、转义、标题/列表标记、链接/标签等。 |
| [lexer.rs:669-850](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L669-L850) `math()` 一族 | Math 模式：数学简写、定界符、数学标识符，**可能返回附带 `SyntaxNode` 的 token**。 |
| [lexer.rs:854-1082](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L854-L1082) `code()` 一族 | Code 模式：字面量、标识符/关键字、运算符。 |
| [lexer.rs:135-184](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L135-L184) `whitespace` / `line_comment` / `block_comment` | 三模式共享的空白与注释处理。 |

## 4. 核心概念与源码讲解

### 4.1 模式分派的总枢纽 `next()`

#### 4.1.1 概念说明

`next()` 是 `Lexer` 实现迭代器的核心（虽然 `Lexer` 没有显式 `impl Iterator`，但 parser 一直调它）。它的职责只有一句话：**吃掉一个 token，并决定它是什么 `SyntaxKind`**。关键在于，它并不是一开始就按模式分派，而是先过滤掉「与模式无关」的公共前缀（空白、shebang、注释、`*/` 错误、raw 反引号），把「真正依赖模式」的字符才交给三个分派函数。

这种「先公共、后分派」的结构，正是空白/注释逻辑能被三模式共享的根本原因。

#### 4.1.2 核心流程

`next()` 的大致流程（伪代码）：

```
吃一个首字符 c：
  若 c 是空白（按当前模式判定）  → whitespace()
  若开头是 "#!"                 → shebang()
  若 "//"                       → line_comment()
  若 "/*"                       → block_comment()
  若 "*/"                       → 报错（意外的块注释结束）
  若 "`" 且不在 Math 模式        → raw()（提前 return，自带节点）
  否则按 mode 分派：
      Markup → markup(c)
      Math   → math(c)        // 可能附带 SyntaxNode，提前 return
      Code   → code(c)
  若文本已结束                   → SyntaxKind::End
最后截取本 token 文本，结合 error 暂存区，造出 leaf 或 Error 节点。
```

#### 4.1.3 源码精读

模式分派发生在这段 `match`（注意最后一个分支才真正用到 `self.mode`）：

[lexer.rs:100-124](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L100-L124) —— `next()` 先用一连串「公共前缀」分支吃掉空白/注释/raw，最后才 `match self.mode` 分派到 `markup` / `math` / `code`。

```rust
let kind = match self.s.eat() {
    Some(c) if is_space(c, self.mode) => self.whitespace(start, c),
    Some('#') if start == 0 && self.s.eat_if('!') => self.shebang(),
    Some('/') if self.s.eat_if('/') => self.line_comment(),
    Some('/') if self.s.eat_if('*') => self.block_comment(),
    // ... "*/" 报错、"`" raw ...
    Some(c) => match self.mode {
        SyntaxMode::Markup => self.markup(start, c),
        SyntaxMode::Math => match self.math(start, c) {
            (kind, None) => kind,
            (kind, Some(node)) => return (kind, node), // 附带子树，提前返回
        },
        SyntaxMode::Code => self.code(start, c),
    },
    None => SyntaxKind::End,
};
```

注意 `math()` 的返回类型是 `(SyntaxKind, Option<SyntaxNode>)`，而另外两个只返回 `SyntaxKind`。当 `math()` 返回 `Some(node)` 时，`next()` 会**提前 return**，绕过结尾那段「造 leaf 节点」的统一收尾逻辑——这是 math 模式独有的「带子树 token」机制，4.4 节会展开。

另外，「空白」本身也依赖模式：`is_space` 在 Markup 下只认空格/制表符/换行，而在 Code/Math 下放宽到一切 `char::is_whitespace()`：

[lexer.rs:1133-1140](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L1133-L1140) —— `is_space` 是模式相关的空白判定。

```rust
fn is_space(character: char, mode: SyntaxMode) -> bool {
    match mode {
        SyntaxMode::Markup => matches!(character, ' ' | '\t') || is_newline(character),
        _ => character.is_whitespace(),
    }
}
```

#### 4.1.4 代码实践（源码阅读型）

**目标**：确认「公共前缀优先于模式分派」这一设计。

**步骤**：在 [lexer.rs:100-124](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L100-L124) 中数一下，在 `match self.mode` 出现之前，共有多少个分支已经「抢先」处理了字符？分别对应什么 `SyntaxKind`？

**预期结果**：你会看到 `whitespace`、`shebang`、`line_comment`、`block_comment`、`*/` 报错、`` ` `` raw 共 6 类公共前缀，它们都不看 `self.mode`（除 raw 排除 Math）。这正是空白/注释能被三模式共享的结构保证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 raw（反引号）的处理被放在公共前缀里，却又要加 `if self.mode != SyntaxMode::Math` 的条件？

> **参考答案**：因为 raw 文本 ` `...` ` 在 Markup 和 Code 模式都可能出现，逻辑相同，所以上移到公共前缀复用；但 Math 模式下反引号不是 raw 定界符，故需排除 Math。

**练习 2**：`next()` 结尾有一段「统一收尾造节点」的逻辑（截文本 + 看 `error` 暂存区）。为什么 `math()` 的某些返回值要 `return` 提前跳出这段逻辑？

> **参考答案**：因为那些返回值自带一棵已经构造好的 `SyntaxNode` 子树（如 `MathFieldAccess`），不能再走「造单个 leaf」的收尾路径。

---

### 4.2 Markup 模式词法：`markup()`

#### 4.2.1 概念说明

Markup 模式对应 Typst 的「正文」——标题、段落、强调、列表、链接、转义等。它的词法特点是**以文本为默认产物**：凡是不被特殊规则捕获的字符，都会被 `text()` 收集成一个 `Text` token。只有在特定位置出现的字符才被识别成标记，比如行首的 `=`、`-`、`+`、`/` 才是标题/列表标记，`*`/`_` 不在词内时才是强调标记。

#### 4.2.2 核心流程

`markup()` 的决策可以分为几组：

1. **结构性标记**：`\\`（转义/换行）、`http://` / `https://`（链接）、`<label>`、`@ref`。
2. **排版简写（Shorthand）**：`...`、`--`、`---`、`-?`、`-数字`、`~` 等，会被渲染成省略号、各种破折号、不换行空格等。
3. **强调标记**：`*`（`!in_word` 时为 `Star`）、`_`（`!in_word` 时为 `Underscore`）。
4. **模式切换/定界符**：`#`（`Hash`，进入代码）、`[` `]`（内容块）、`'` `"`（智能引号）、`$`（进入公式）、`:`（标签终止）。
5. **行首列表标记**：`= `（`HeadingMarker`）、`- `（`ListMarker`）、`+ `（`EnumMarker`）、`/ `（`TermMarker`）——都要求后面是空格或结尾。
6. **数字**：`0..=9` 走 `numbering()`，可能是有序列表标记 `1.` 也可能是普通文本。
7. **兜底**：其余全部走 `text()`。

#### 4.2.3 源码精读

[lexer.rs:488-523](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L488-L523) —— `markup()` 的分派表。关键看几个「位置敏感」的分支：

```rust
'=' => {
    self.s.eat_while('=');
    if self.space_or_end() { SyntaxKind::HeadingMarker } else { self.text() }
}
'-' if self.space_or_end() => SyntaxKind::ListMarker,
'+' if self.space_or_end() => SyntaxKind::EnumMarker,
'/' if self.space_or_end() => SyntaxKind::TermMarker,
'0'..='9' => self.numbering(start),
_ => self.text(),
```

注意 `=` 后面要 `space_or_end()`（空格/换行/注释/结尾）才是标题标记，否则（如 `a=b` 中的 `=`）就退回 `text()`。`-` 则更微妙：

[lexer.rs:496-502](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L496-L502) —— `-` 有多个带 guard 的分支：`--`/`---`/`-?`/`-数字` 都是 `Shorthand`，行首 `- ` 才是 `ListMarker`，词内的 `-` 则落到 `text()`。

`text()` 是 Markup 的灵魂——它用一个查表 + 「续接规则」贪婪合并文本：

[lexer.rs:600-639](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L600-L639) —— `text()` 先 `eat_until` 停在「可能是特殊字符」处，再判断这个字符「是否其实仍是文本」并续接。

```rust
// 续接规则：这些字符若后面不是更特殊的形式，就并入当前 Text
match s.eat() {
    Some(' ') if s.at(char::is_alphanumeric) => {} // "a 1" 中的多空格
    Some('/') if !s.at(['/', '*']) => {}            // 不是注释的 "/"
    Some('-') if !s.at(['-', '?']) => {}            // 不是 --/-?/-数字 的 "-"
    Some('.') if !s.at("..") => {}                  // 不是 ... 的 "."
    Some('h') if !s.at("ttp://") && !s.at("ttps://") => {}
    _ => break,
}
```

这条 `Some('-') if !s.at(['-', '?']) => {}` 续接规则，正是为什么 `"1-2"` 在 Markup 下会被合并成**单个** `Text "1-2"`（详见第 5 节综合实践）。

#### 4.2.4 代码实践（源码阅读型）

**目标**：理解 `numbering()` 如何区分「有序列表标记」与「普通文本数字」。

**步骤**：阅读 [lexer.rs:565-574](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L565-L574) 的 `numbering()`。回答：要让一段数字成为 `EnumMarker`，需要同时满足哪三个条件？

**预期结果**：三个条件是——(1) `eat_if('.')` 吃掉一个小数点；(2) `self.space_or_end()` 后面是空格/结尾；(3) `read.parse::<u64>().is_ok()` 数字本身能解析成合法整数。三者全满足才返回 `EnumMarker`，否则退回 `self.text()`。所以 `"1. 引言"` 的 `1.` 是枚举标记，而 `"1.5"` 或 `"1.a"` 则是文本。

#### 4.2.5 小练习与答案

**练习 1**：在 Markup 下，`"a*b"` 和 `"a *b*"` 里的 `*` 分别会切成什么 token？

> **参考答案**：`"a*b"` 中 `*` 处于词内（前后都是字母，`in_word()` 为真），`'*' if !self.in_word()` 不成立，落到 `text()`，故整段是一个 `Text "a*b"`；`"a *b*"` 中 `*` 不在词内，得到 `Star` 标记，用于强调。

**练习 2**：为什么 `'"'` 在 Markup 下是 `SmartQuote`，而在 Code/Math 下却是字符串起止？

> **参考答案**：Markup 模式把引号当成「智能引号」做排版（自动区分左右引号），见 [lexer.rs:507-508](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L507-L508)；而 Code/Math 需要字符串字面量，故 `'"' => self.string()`。

---

### 4.3 Code 模式词法：`code()`

#### 4.3.1 概念说明

Code 模式对应 `#` 之后的代码，以及代码块 `{...}` 内部。这里的词法规则最「传统」：数字字面量、字符串字面量、标识符/关键字、各种运算符与分隔符。和 Markup 最大的区别是——**这里没有「默认文本」**：任何不被识别的字符都会产生 `Error`（见 `invalid_char_in_code`），而不是被静默吞成文本。

#### 4.3.2 核心流程

`code()` 的分派可分为：

1. **字面量**：`0..9` / `.数字` → `number()`（产出 `Int`/`Float`/`Numeric`）；`"` → `string()`（产出 `Str`）。
2. **多字符运算符**：`==`、`!=`、`<=`、`>=`、`+=`、`-=`、`...`、`=>` 等先于单字符版本匹配。
3. **单字符分隔符/运算符**：`{ } [ ] ( ) $ , ; : . + - * / = < >`。
4. **标识符/关键字**：`is_id_start(c)` → `ident()`，后者再把 `let`/`if`/`none` 等翻成关键字 token。
5. **兜底**：其余字符 → `invalid_char_in_code()` 产出 `Error` 并附提示。

#### 4.3.3 源码精读

[lexer.rs:854-895](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L854-L895) —— `code()` 分派表。注意多字符运算符必须排在单字符之前（`match` 自上而下匹配）：

```rust
'=' if self.s.eat_if('=') => SyntaxKind::EqEq,   // == 优先
'-' | '\u{2212}' if self.s.eat_if('=') => SyntaxKind::HyphEq,
// ... 单字符版本在后面
'-' | '\u{2212}' => SyntaxKind::Minus,
```

数字的处理全部委托给 `number()`，它支持二/八/十六进制前缀、浮点、指数、以及 `pt`/`mm`/`%` 等数值后缀：

[lexer.rs:964-1067](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L964-L1067) —— `number()` 根据 `is_float` 与后缀情况返回 `Float` / `Int` / `Numeric`，遇到非法后缀或非十进制带后缀则报错。

标识符与关键字的分离在 `ident()`：

[lexer.rs:942-954](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L942-L954) —— `ident()` 吃完整标识符后调用 `keyword()` 判定是否是关键字；单独的 `_` 返回 `Underscore`（通配符），其余返回 `Ident`。

[lexer.rs:1086-1112](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L1086-L1112) —— `keyword()` 把 `none`/`let`/`if`/`for` 等字符串映射到对应的 `SyntaxKind` 关键字 token。

「没有默认文本」的兜底逻辑：

[lexer.rs:899-940](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L899-L940) —— `invalid_char_in_code()` 对常见误用（`#`、`&&`、`||`、`!`、`~=`）给出友好提示，例如 `&&` 会提示「Typst 用 `and`」。

#### 4.3.4 代码实践（源码阅读型）

**目标**：理解 `number()` 如何区分 `Int`、`Float`、`Numeric` 三种结果。

**步骤**：阅读 [lexer.rs:964-1067](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L964-L1067)，跟踪 `is_float` 标志在哪些情况下被置为 `true`，以及结尾 [lexer.rs:1054-1066](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L1054-L1066) 的返回匹配。

**预期结果**：`is_float` 在「以 `.` 开头」「含小数点」「含指数 `e/E`」时置真。返回规则是——`(Ok, Ok(None)) && is_float` → `Float`；`(Ok, Ok(None))`（非浮点）→ `Int`；`(Ok, Ok(Some))`（有合法后缀）→ `Numeric`。所以 `10` 是 `Int`、`1.5` 是 `Float`、`12pt` 是 `Numeric`。

#### 4.3.5 小练习与答案

**练习 1**：Code 模式下输入 `#`（井号）会发生什么？

> **参考答案**：`#` 在 `code()` 里没有对应分支，落到 `invalid_char_in_code('#')`，产出 `Error` token，并附提示「you are already in code mode / try removing the `#`」（见 [lexer.rs:913-917](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L913-L917)）。

**练习 2**：为什么 `==` 必须写在 `=` 之前？

> **参考答案**：`match` 自上而下匹配，先尝试 `eat_if('=')` 吃掉第二个 `=`；若不写在前，`=` 分支会先把单个 `=` 吃掉，导致 `==` 永远无法被识别成 `EqEq`。

---

### 4.4 Math 模式词法：`math()`（可附带 SyntaxNode）

#### 4.4.1 概念说明

Math 模式对应公式 `$...$` 内部。它的词法有两点与其余模式显著不同：

1. **大量「数学简写」**：像 `->`、`=>`、`==>`、`<<`、`~>` 等多字符序列，统一产出 `MathShorthand`（在渲染时变成箭头、等价等数学符号）。
2. **可能附带返回一棵 `SyntaxNode` 子树**：当遇到多字符素（多字形簇）的数学标识符时，`math()` 会在词法阶段直接构造一棵 `MathFieldAccess`（如 `vec.x`）子树返回，而不是只产出一个扁平 token。这是 `math()` 返回类型为 `(SyntaxKind, Option<SyntaxNode>)` 的根本原因。

#### 4.4.2 核心流程

`math()` 的分派可分为：

1. **转义与字符串**：`\\`（转义）、`"`（字符串）。
2. **数学简写（MathShorthand）**：一大片以 `-` `<` `>` `=` `|` `~` `:` `!` `.` 开头的多字符序列，以及单字符的 `*` `-` `~`。
3. **标点与定界符**：`.` `,` `;` `#` `_`（下标）`$` `/` `^` `&`（对齐点）`!`，以及 `√∛∜`（`Root`）。
4. **定界符识别**：`(` `)` 直接为 `LeftParen`/`RightParen`；`[|` `|]` 映射为 `LeftBrace`/`RightBrace`；其余字符按 Unicode 的 `MathClass::Opening/Closing` 判定为左右大括号。
5. **数学标识符**：满足 `is_math_id_start` 且后续 `is_math_id_continue` 时，吃完整标识符；若只有一个字形簇 → `MathText`；若是多字形簇 → 调 `math_ident_or_field()` 构造子树并**附带返回**。
6. **兜底**：其余字符走 `math_text()`，产出 `MathText`。

#### 4.4.3 源码精读

[lexer.rs:669-760](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L669-L760) —— `math()` 主体。注意它把单字符 `*` `-` `~` 也归入 `MathShorthand`（与 Code 模式的 `Minus`/`Star` 完全不同）：

```rust
'*' | '-' | '~' => SyntaxKind::MathShorthand,
// ...
c if is_math_id_start(c) && self.s.at(is_math_id_continue) => {
    self.s.eat_while(is_math_id_continue);
    let (last_index, _) =
        self.s.from(start).grapheme_indices(true).next_back().unwrap();
    if last_index == 0 {
        SyntaxKind::MathText          // 单字形簇：扁平 token
    } else {
        let (kind, node) = self.math_ident_or_field(start);
        return (kind, Some(node));    // 多字形簇：附带子树提前返回
    }
}
```

「附带子树」的构造逻辑在 `math_ident_or_field()`：

[lexer.rs:763-776](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L763-L776) —— 它先造一个 `MathIdent` 叶子，若后面还跟着 `.ident`，就用 `inner` 把「标识符 + `.` + 标识符」包成 `MathFieldAccess` 子树，循环支持 `a.b.c`。

```rust
let mut node = SyntaxNode::leaf(kind, self.s.from(start));
while let Some(ident) = self.maybe_dot_ident() {
    kind = SyntaxKind::MathFieldAccess;
    let field_children = vec![
        node,
        SyntaxNode::leaf(SyntaxKind::Dot, '.'),
        SyntaxNode::leaf(SyntaxKind::MathIdent, ident),
    ];
    node = SyntaxNode::inner(kind, field_children);
}
```

这说明**词法器在 Math 模式下会越权做一点「迷你语法」工作**——把 `vec.x` 直接构造成带三个子节点的 `MathFieldAccess` 子树交给 parser。这么做是为了避免 parser 频繁来回处理数学字段访问（与 raw 在 lexer 内整体解析的设计动机类似，见 [lexer.rs:189-191](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L189-L191) 的注释）。

#### 4.4.4 代码实践（源码阅读型）

**目标**：理解 `math_ident_or_field()` 与 `maybe_dot_ident()` 如何协作构造字段访问子树。

**步骤**：阅读 [lexer.rs:763-788](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L763-L788)。回答：对于输入 `vec.x.y`，词法器最终返回的 `SyntaxKind` 是什么？子树结构大致如何？

**预期结果**：返回 `MathFieldAccess`。子树是一个三层嵌套：外层 `MathFieldAccess` 的子节点为「内层 `MathFieldAccess`（`vec` `.``x`）+ `.` + `y`」。也就是说 lexer 在词法阶段就把整条字段访问链包好了。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `math()` 的返回签名是 `(SyntaxKind, Option<SyntaxNode>)`，而 `markup()`/`code()` 只返回 `SyntaxKind`？

> **参考答案**：因为 `math()` 在遇到多字形簇标识符（字段访问）时，会在词法阶段直接构造一棵 `SyntaxFieldAccess` 子树，需要把这棵子树连同 kind 一起返回；其余两个模式不在词法阶段构造子树，故只需返回 kind，由 `next()` 统一造 leaf。

**练习 2**：在 Math 模式下，`*` 和 `-` 分别被切成什么 token？这与 Code 模式有何不同？

> **参考答案**：Math 下 `*`、`-` 都是 `MathShorthand`（会被渲染成数学乘号/减号）；Code 下 `*` 是 `Star`、`-` 是 `Minus`（程序运算符）。

---

### 4.5 共享逻辑：`whitespace` / `line_comment` / `block_comment`

#### 4.5.1 概念说明

三模式的空白与注释规则高度一致，因此被抽到「共享方法」impl 块（[lexer.rs:91-185](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L91-L185) 标注为 `Shared methods with all SyntaxMode`）。它们都在 `next()` 的公共前缀里被调用，**早于模式分派**，所以对三模式行为一致。唯一一处模式相关的细节是：Markup 模式下连续 ≥2 个换行会产生 `Parbreak`（段落分隔），其余情况一律是 `Space`。

#### 4.5.2 核心流程

- `whitespace`：贪吃所有空白字符，统计换行数；Markup 且换行 ≥2 → `Parbreak`，否则 → `Space`；同时把 `self.newline` 置位。
- `line_comment`：吃到换行符为止（不含换行），产出 `LineComment`。
- `block_comment`：用状态机处理**嵌套**的 `/* ... */`，深度归零才结束，产出 `BlockComment`。

#### 4.5.3 源码精读

[lexer.rs:135-149](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L135-L149) —— `whitespace()` 的模式相关分支只有一处：

```rust
self.newline = newlines > 0;
if self.mode == SyntaxMode::Markup && newlines >= 2 {
    SyntaxKind::Parbreak
} else {
    SyntaxKind::Space
}
```

[lexer.rs:156-159](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L156-L159) —— `line_comment()` 用 `eat_until(is_newline)` 吃到行尾。

[lexer.rs:161-184](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L161-L184) —— `block_comment()` 用 `(state, c)` 状态机维护嵌套深度 `depth`：遇到 `/*` 加一、`*/` 减一，归零时结束。

```rust
match (state, c) {
    ('*', '/') => { depth -= 1; if depth == 0 { break; } '_' }
    ('/', '*') => { depth += 1; '_' }
    _ => c,
}
```

注意：`whitespace` 与注释产出的 `Space`/`Parbreak`/`LineComment`/`BlockComment` 都属于 **trivia**（u2-l2 讲过的 `is_trivia` 为真的片段），parser 会在「前置 trivia」与「真正 token」之间谨慎安排它们，但这些判定与模式无关。

#### 4.5.4 代码实践（观察型）

**目标**：体会「连续两行空行 → 段落分隔」只在 Markup 生效。

**步骤**：阅读 [lexer.rs:144-148](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L144-L148) 的条件分支，思考：在 Code/Math 模式下，即使有 5 个连续换行，产出的 token 是什么？

**预期结果**：因为条件要求 `self.mode == SyntaxMode::Markup`，Code/Math 模式下无论多少换行都只产出 `Space`（当然 `self.newline` 仍会被置位，供 parser 判断语句终止）。

#### 4.5.5 小练习与答案

**练习 1**：`/* a /* b */ c */` 能否被正确识别为单个块注释？为什么？

> **参考答案**：能。`block_comment()` 支持**嵌套**：遇到内层 `/*` 深度加到 2，遇到第一个 `*/` 减到 1，遇到第二个 `*/` 减到 0 才结束，整段是一个 `BlockComment`。

**练习 2**：为什么 `whitespace` 里要专门优化 `' ' if more.is_empty() => 0`（单个空格）这条分支？

> **参考答案**：单个空格是最常见的 token，跳过 `count_newlines` 这趟字符串扫描能减少热路径开销——这是「先优化最常见情形」的典型微优化。

## 5. 综合实践

**目标**：用一个最小程序，亲眼看到「同一段字符串在三种模式下被切成不同的 token」。

由于 `Lexer` 是 crate 内部类型（`pub(super)`，见 [lexer.rs:16](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L16)），我们无法在外部直接 `Lexer::new(...)`。但三个公开入口 `parse` / `parse_code` / `parse_math` 分别固定了 Markup / Code / Math 模式（见 [parser.rs:16-37](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L16-L37)），它们返回的 CST 根节点（`Markup`/`Code`/`Math`）的叶子就是词法器产出的 token。我们就用它来观察。

**操作步骤**：

1. 新建一个 Rust 项目，在 `Cargo.toml` 中添加依赖（版本号请以 crates.io 上 `typst-syntax` 的最新版为准，或在 typst 仓库内用 workspace/path 引用）：

   ```toml
   [dependencies]
   typst-syntax = "<待本地确认的版本>"
   ```

2. 把下面这段示例代码（标注为「示例代码」）写入 `src/main.rs`。它递归遍历 CST，只把**叶子 token** 的 `kind` 与文本打印出来：

   ```rust
   // 示例代码
   use typst_syntax::{parse, parse_code, parse_math, SyntaxNode};

   fn dump(node: &SyntaxNode, depth: usize) {
       let indent = "  ".repeat(depth);
       // 叶子 = 没有子节点的节点（leaf 或 error）
       let is_leaf = node.children().next().is_none();
       if is_leaf {
           println!("{indent}• {:?} {:?}", node.kind(), node.leaf_text());
       } else {
           println!("{indent}{:?}", node.kind());
       }
       for child in node.children() {
           dump(child, depth + 1);
       }
   }

   fn main() {
       println!("=== Markup ===");
       dump(&parse("1-2"), 0);
       println!("=== Code ===");
       dump(&parse_code("1-2"), 0);
       println!("=== Math ===");
       dump(&parse_math("1-2"), 0);
   }
   ```

3. 运行 `cargo run`。

**需要观察的现象 / 预期结果**：

聚焦于「叶子节点（`•`）」这一行，三种模式的差异如下表：

| 模式 | 入口 | 根 kind | 叶子 token 序列 | `-` 的角色 |
|---|---|---|---|---|
| Markup | `parse("1-2")` | `Markup` | `Text "1-2"` | 正文的一部分（单 token） |
| Code | `parse_code("1-2")` | `Code` | `Int "1"`、`Minus "-"`、`Int "2"` | 减法运算符 |
| Math | `parse_math("1-2")` | `Math` | `MathText "1"`、`MathShorthand "-"`、`MathText "2"` | 数学减号 |

> Code 与 Math 的根节点下还会有若干由 parser 添加的中间节点（如表达式包裹），那是第 U4 单元的内容；本实践只关心「叶子 token」即词法器的直接产物。

**解释（为什么）**：

- **Markup**：`1` 走 `numbering()` → 退回 `text()`，`text()` 的续接规则 `Some('-') if !s.at(['-', '?'])`（[lexer.rs:628](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L628)）把后面不是 `--`/`-?` 的 `-` 并入当前文本，于是整段 `"1-2"` 被合并成单个 `Text`。
- **Code**：`1` 走 `number()` → `Int`；`-` 走 `'-' => Minus`（[lexer.rs:884](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L884)）；`2` → `Int`。
- **Math**：`1` 走 `math_text()` → `MathText`；`-` 走 `'*' | '-' | '~' => MathShorthand`（[lexer.rs:707](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L707)）；`2` → `MathText`。

若你的运行结果与上表不符（例如 typst-syntax 版本差异），请以本地实际输出为准。

## 6. 本讲小结

- `next()` 是模式分派的总枢纽：先处理空白/shebang/注释/raw 等**公共前缀**，最后才按 `self.mode` 调用 `markup()` / `math()` / `code()`。
- `markup()` 以 `text()` 为默认产物，靠「位置敏感」的标记规则（行首 `=`/-/+//、`!in_word` 的 `*`/`_`）识别结构，`-` 在词内会被文本续接规则吞掉。
- `code()` 最「严格」：数字/字符串/标识符/关键字/运算符，任何无法识别的字符都走 `invalid_char_in_code()` 产出 `Error`，没有默认文本。
- `math()` 有两大独特点：大量 `MathShorthand` 简写；遇到多字形簇标识符时会在**词法阶段**构造 `MathFieldAccess` 子树，返回类型因此是 `(SyntaxKind, Option<SyntaxNode>)`。
- `whitespace` / `line_comment` / `block_comment` 是三模式共享逻辑，唯一的模式相关细节是 Markup 下连续 ≥2 换行产出 `Parbreak`。
- 同一字符在不同模式下产出不同 token 是常态：`"` 在 Markup 是智能引号、在 Code/Math 是字符串；`*` 在 Markup 是强调标记、Code 是乘法、Math 是简写；`-` 在 Markup 是文本、Code 是减号、Math 是简写。

## 7. 下一步学习建议

- 下一讲 **u3-l3 原始文本 Raw 的词法处理** 将深入 `raw()`（[lexer.rs:191](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L191)），它是 `next()` 公共前缀里另一个「在词法阶段整体解析」的构造，与本讲 `math_ident_or_field()` 的设计动机一脉相承。
- 若想了解这些 token 如何被组装成树，可进入 **U4 语法分析 Parser**，特别是 `markup_exprs` / `code_exprs` / `math_exprs` 如何消费本讲产出的 token。
- 若对字符级工具函数（`is_id_start`、`is_newline`、`link_prefix` 等）感兴趣，可预习 **u3-l4 字符判定工具函数**。

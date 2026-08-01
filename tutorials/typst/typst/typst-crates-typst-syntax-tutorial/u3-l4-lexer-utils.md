# 字符判定工具函数

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 Typst 如何判定「一个字符能否出现在标识符里」，并知道它建立在哪份 Unicode 标准之上。
- 区分 `is_id_start`、`is_id_continue`、`is_ident` 三者的职责边界，以及它们在词法与清单解析中的复用方式。
- 理解 `is_newline` 判定的 6 个换行字符，以及 `split_newlines` 如何把 `\r\n` 当作一次换行处理。
- 读懂 `link_prefix` 如何从一段文本里「贪婪吃掉链接 + 剥掉尾部标点 + 校验括号平衡」。
- 解释 `is_valid_label_literal_id` 与标签 `<label>`、引用 `@ref` 的关系。

本讲只聚焦 `src/lexer.rs` 末尾的一组**公共字符工具函数**，它们被 `lib.rs` 通过 `pub use` 挂牌到 crate 根。它们本身不产出 token，却是 Lexer 内部分派、包清单校验、AST 校验等处共享的「字符口径」。

## 2. 前置知识

### 2.1 什么是「标识符」

在编程语言里，**标识符（identifier）** 是程序员给变量、函数、字段起的名字，比如 `x`、`my_func`、`温度`。判定「哪些字符能组成标识符」看似简单（字母数字下划线？），但一旦要支持多语言（中文变量名、emoji 之外的 Unicode 字母），就必须依赖 Unicode 标准。

Typst 选用 [Unicode UAX #31](http://www.unicode.org/reports/tr31/) 定义的 **XID_Start / XID_Continue** 两个字符集合作为基础，并在此基础上做了少量扩展。Rust 生态里 `unicode-ident` crate 提供了这两个集合的查询函数。

### 2.2 换行不止 `\n`

文本里的「换行」在不同操作系统、不同 Unicode 码点下有多种表示：`\n`（Unix）、`\r\n`（Windows）、`\r`（老 Mac），以及 Unicode 专用的行/段分隔符 `\u{0085}`、`\u{2028}`、`\u{2029}`。Typst 需要统一识别它们，因为换行决定了 markup 段落切分与 code 语句终止。

### 2.3 unscanny::Scanner

这些工具大量使用 [`unscanny`](https://docs.rs/unscanny) 提供的 `Scanner` 游标。你可以把它理解为「一根在字符串上移动的指针」，常用操作：

- `eat()`：读一个字符并前进。
- `eat_while(pred)`：只要字符满足 `pred` 就一直吃。
- `eat_if(ch)`：如果当前是 `ch` 就吃掉，返回是否吃到。
- `before()` / `after()`：当前游标之前 / 之后的切片。
- `scout(n)` / `uneat()`：相对当前游标前后偷看 / 回退一格。

本讲涉及的函数几乎都是「用 Scanner 跑一遍字符、按规则停在某处、返回切片」的模式。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`src/lexer.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs) | 本讲主角。文件末尾（1144–1277 行）集中定义了 7 个公共字符工具函数，以及 `count_newlines`、`is_valid_in_label_literal` 等私有帮手。文件顶部 `use` 了 `unicode-ident` 等 Unicode crate。 |
| [`src/lib.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lib.rs) | crate 门面。用一条 `pub use` 把 7 个工具函数从私有的 `lexer` 模块挂牌到 crate 根，对外暴露。 |

此外，本讲会**引用性提及**这些消费方（不展开）：

- `src/package.rs`：解析 `@namespace/name:version` 时用 `is_ident` 校验命名空间与名称。
- `src/ast.rs`：用 `is_ident` 校验用户写的名字是否合法。
- `src/highlight.rs`：内部有一个**同名但不同**的私有 `is_ident(node)`，不要和本讲的公共 `is_ident(string)` 混淆。

## 4. 核心概念与源码讲解

### 4.1 标识符的 Unicode 基础：is_id_start / is_id_continue / is_ident

#### 4.1.1 概念说明

Typst 的标识符判定建立在 Unicode UAX #31 之上。UAX #31 定义了两个核心字符集：

- **XID_Start**：可以作为标识符**首字符**的字符，主要是各语言的「字母」（拉丁字母、汉字、希腊字母等）。
- **XID_Continue**：可以出现在标识符**后续位置**的字符，在 XID_Start 基础上加上数字、组合符号等。

这两个集合由 `unicode-ident` crate 提供，Typst 在 [src/lexer.rs:5](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L5) 直接引入：

```rust
use unicode_ident::{is_xid_continue, is_xid_start};
```

Typst 在此基础上做了两处**有意扩展**：

1. 允许 `_`（下划线）作为首字符，也允许它出现在后续位置——这样 `_tmp`、`foo_bar` 是合法标识符。
2. 允许 `-`（连字符）出现在后续位置——这样 `my-func` 也是合法标识符（很多 Lisp 风格语言的惯例）。

于是 Typst 自定义了两个判定函数 `is_id_start` / `is_id_continue`，再用它们组合出对整个字符串的判定 `is_ident`。

#### 4.1.2 核心流程

判定一个字符串 `s` 是否是合法标识符，遵循经典的「首字符 + 后续字符」两段式：

```
空串                      → 不是
s[0] 满足 is_id_start     → 继续
s[1..] 全部满足 is_id_continue → 是标识符
任意一个后续字符不满足      → 不是
```

用一条公式表达「合法标识符」集合：

\[
\text{Ident} = \text{IdStart} \;\circ\; {\text{IdContinue}}^*
\]

即「一个起始字符，后跟零或多个续字符」。注意首字符用更严格的 `is_id_start`（不含 `-`），续字符用更宽松的 `is_id_continue`（含 `-`）。

#### 4.1.3 源码精读

**`is_id_start`** —— 首字符判定（[src/lexer.rs:1244-1248](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L1244-L1248)）：

```rust
/// Whether a character can start an identifier.
#[inline]
pub fn is_id_start(c: char) -> bool {
    is_xid_start(c) || c == '_'
}
```

在 UAX #31 的 XID_Start 之上，额外允许 `_`。注意**不含 `-`**：`-myvar` 不是合法标识符开头。

**`is_id_continue`** —— 续字符判定（[src/lexer.rs:1250-1254](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L1250-L1254)）：

```rust
/// Whether a character can continue an identifier.
#[inline]
pub fn is_id_continue(c: char) -> bool {
    is_xid_continue(c) || c == '_' || c == '-'
}
```

在 XID_Continue 之上，额外允许 `_` 和 `-`。两者都标了 `#[inline]`，因为它们会在 Lexer 的热路径上被逐字符调用，内联后能消除函数调用开销。

**`is_ident`** —— 整串判定（[src/lexer.rs:1229-1242](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L1229-L1242)）：

```rust
/// Whether a string is a valid Typst identifier.
///
/// In addition to what is specified in the [Unicode Standard][uax31], we allow:
/// - `_` as a starting character,
/// - `_` and `-` as continuing characters.
///
/// [uax31]: http://www.unicode.org/reports/tr31/
#[inline]
pub fn is_ident(string: &str) -> bool {
    let mut chars = string.chars();
    chars
        .next()
        .is_some_and(|c| is_id_start(c) && chars.all(is_id_continue))
}
```

这段代码精炼地实现了上面的两段式判定：`chars.next()` 取首字符，`is_some_and` 同时处理了「空串返回 `None` → false」和「首字符是否合法」两件事；`chars.all(is_id_continue)` 用迭代器的短路求值检查剩余字符，遇到第一个非法字符立即返回 `false`。

**Lexer 内部如何复用**：在 Code 模式里，`code()` 分派函数用 `is_id_start` 判定是否进入标识符解析分支（[src/lexer.rs:891](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L891)），随后 `ident()` 方法用 `eat_while(is_id_continue)` 把后续字符吃进来（[src/lexer.rs:942-943](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L942-L943)）。这正是「首字符用 start、续字符用 continue」规则的落地。

**对外复用**：`is_ident` 不只是 Lexer 自用。解析包清单 `typst.toml` 里的 `@namespace/name` 时，`package.rs` 也用它校验命名空间与名称是否合法（[src/package.rs:322-323](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/package.rs#L322-L323) 与 [src/package.rs:335-336](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/package.rs#L335-L336)）。把字符口径集中在一处公共函数，保证了「词法阶段」和「清单解析阶段」对「什么是合法名字」的认定一致。

> 顺带一提：Math 模式有**自己的**更严格版本 `is_math_id_start` / `is_math_id_continue`（[src/lexer.rs:1256-1266](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L1256-L1266)）：数学里 `_` 表示下标、`-` 表示减号，所以它们都被排除在数学标识符之外。这两个是私有函数，不在本讲导出范围内，但能帮你理解为何要单独定义 `is_id_*` 而不是直接用 `unicode-ident`。

#### 4.1.4 代码实践

**目标**：用 `is_ident` / `is_id_start` / `is_id_continue` 验证一组字符串的合法性，体会首字符与续字符口径的差异。

**操作步骤**（在仓库根目录运行一个临时测试）：

1. 在 `crates/typst-syntax/src/lexer.rs` 的文件末尾追加一个临时 `#[cfg(test)]` 模块（这是「示例代码」，验证完请删除，勿提交）：

```rust
// 示例代码：临时测试，验证后删除
#[cfg(test)]
mod util_probe {
    use super::*;
    #[test]
    fn probe_idents() {
        let cases = ["x", "_tmp", "my-func", "温度", "-bad", "", "1st", "a b"];
        for c in cases {
            println!("{:?} -> is_ident={}", c, is_ident(c));
        }
        // 单字符口径对比
        for c in ['-', '_', 'a', '1'] {
            println!(
                "{:?} start={} continue={}",
                c,
                is_id_start(c),
                is_id_continue(c)
            );
        }
    }
}
```

2. 运行：

```bash
cargo test -p typst-syntax util_probe -- --nocapture
```

**需要观察的现象**：

- `"x"`、`"_tmp"`、`"my-func"`、`"温度"` 都应为 `true`（`-` 不能开头但能续接；汉字属 XID_Start）。
- `"-bad"`、`""`、`"1st"`、`"a b"` 应为 `false`（`-`/数字/空格不能开头，空串非法，空格断开）。
- 单字符里：`-` 的 `start=false` 但 `continue=true`；`1` 两者皆 `false`（数字既非 start 也非 Typst 的 continue——注意 Typst 的 `is_id_continue` 不含数字，这与很多语言不同）。

> 关于数字：Typst 的 `is_id_continue` 直接复用 `is_xid_continue`，后者**包含**十进制数字。所以严格来说 `"a1"` 是合法标识符，`"1st"` 非法仅因为首字符 `1` 不满足 `is_id_start`。**待本地验证**：`println!("{}", is_ident("a1"))` 应输出 `true`，请运行确认。

**预期结果**：理解了「start 更严、continue 更宽」以及 Typst 对 `_`、`-` 的两项扩展。

#### 4.1.5 小练习与答案

**练习 1**：`is_ident("a-1")` 返回什么？为什么？

> **答案**：返回 `true`。首字符 `a` 满足 `is_id_start`，后续 `-` 满足 `is_id_continue`（Typst 扩展），`1` 满足 `is_xid_continue`（数字可续接）。整串合法。

**练习 2**：为什么 Typst 要自定义 `is_id_start`，而不直接用 `unicode-ident` 的 `is_xid_start`？

> **答案**：因为 Typst 想允许 `_` 开头（如 `_tmp`、纯 `_` 占位符），这是 UAX #31 默认集合并未保证的。自定义一层薄封装既保留 Unicode 标准，又能加上 Typst 的语言设计决策。

**练习 3**：`is_id_continue('1')` 是 `true` 还是 `false`？

> **答案**：是 `true`。因为 `is_xid_continue('1')` 为真（数字属于 XID_Continue），Typst 没有在 `is_id_continue` 里排除数字。数字能续接但不能开头。

---

### 4.2 换行判定 is_newline

#### 4.2.1 概念说明

「换行」是一个被多个模块反复需要的字符口径：Lexer 要知道上个 token 是否含换行（用于终止 code 语句），`Lines` 要按换行切分文本建立行索引，markup 要按连续换行切分段落。如果每个模块各写一份换行判定，口径就可能漂移。Typst 把它收口为一个公共函数 `is_newline`。

#### 4.2.2 核心流程

`is_newline(c)` 判定 `c` 是否是 6 个换行字符之一，覆盖三大类来源：

```
Unix/控制字符：\n  \x0B(垂直制表)  \x0C(换页)  \r(回车)
Unicode「下一行」：\u{0085}（Next Line, NEL）
Unicode 行/段分隔：\u{2028}(Line Separator)  \u{2029}(Paragraph Separator)
```

注意：`\r` 单独算一次换行，`\n` 也单独算一次；把 `\r\n` 当成**一次**换行是 `split_newlines`/`count_newlines` 的职责，而不是 `is_newline` 的职责——后者只看单个字符。

#### 4.2.3 源码精读

[src/lexer.rs:1144-1152](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L1144-L1152)：

```rust
pub fn is_newline(character: char) -> bool {
    matches!(
        character,
        // Line Feed, Vertical Tab, Form Feed, Carriage Return.
        '\n' | '\x0B' | '\x0C' | '\r' |
        // Next Line, Line Separator, Paragraph Separator.
        '\u{0085}' | '\u{2028}' | '\u{2029}'
    )
}
```

实现就是一个 `matches!` 宏穷举 6 个字符，没有任何外部依赖——这是「自包含、可预测」的典范。注释把 6 个字符分成「传统控制字符」与「Unicode 专用分隔符」两组，便于读者理解。

它在 crate 内部被广泛复用，例如 Lexer 的空白判定 `whitespace`（[src/lexer.rs:1137](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L1137)）和行注释终止（[src/lexer.rs:152](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L152)）都调用它。

#### 4.2.4 代码实践

**目标**：验证 6 个换行字符的判定，并体会 `\r` 与 `\n` 各自单独为真。

**操作步骤**：在上一节的临时测试模块里再加：

```rust
// 示例代码
#[test]
fn probe_newlines() {
    for c in ['\n', '\r', '\u{0B}', '\u{0C}', '\u{0085}', '\u{2028}', '\u{2029}', ' ', 'a'] {
        println!("{:?} (U+{:04X}) -> {}", c, c as u32, is_newline(c));
    }
}
```

**需要观察的现象**：前 6 个为 `true`，空格和 `a` 为 `false`。

**预期结果**：确认换行口径与平台无关——即便源码来自 Windows（`\r\n`）或含 Unicode 行分隔符的文档，Typst 也能统一识别。

#### 4.2.5 小练习与答案

**练习 1**：字符串 `"\r\n"` 调用 `is_newline` 会怎样？

> **答案**：`is_newline` 接收的是单个 `char`，不能一次接收两个字符。分别看：`is_newline('\r')` 与 `is_newline('\n')` 都是 `true`。把它们合并成「一次换行」是 `split_newlines` 的工作。

**练习 2**：为什么把换行判定做成 `pub` 函数而不是宏或常量表？

> **答案**：函数可被 `Scanner::eat_while(is_newline)`、`s.at(is_newline)` 等高阶用法直接当作谓词传递，调用点简洁；同时集中维护一份字符清单，避免散落各处的魔数。

---

### 4.3 链接前缀 link_prefix

#### 4.3.1 概念说明

在 Markup 模式下，Typst 能自动识别「裸链接」——直接写 `https://typst.org` 就会被词法成一个 `Link` token，无需引号包裹。但现实文本里链接常常和正文紧挨着，比如句末的 `见 https://typst.org。`，那个句号 `。` 不属于链接。而且链接里可能带括号，比如维基百科的 URL `https://en.wikipedia.org/wiki/Rust_(programming_language)`，末尾的 `)` 究竟属于链接还是正文，需要靠**括号平衡**来判断。

`link_prefix` 就是负责「从一段文本里尽可能长地吃出一个链接，并报告括号是否平衡」的函数。

#### 4.3.2 核心流程

```
1. 用 Scanner 从头扫描，吃掉所有「链接允许字符」：
   - 字母数字
   - 一组安全标点：! # $ % & * + , - . / : ; = ? @ _ ~ '
   - '[' '('：压栈，继续吃
   - ']' ')'：仅当栈顶匹配时弹出并继续；否则停止
   - 其它字符（空格、中文、换行等）：停止
2. 吃完后，剥掉末尾「更可能是正文标点」的字符：! , . : ; ? '
3. 返回 (已吃前缀, 括号是否平衡)
```

关键设计：

- **括号配对栈**：遇到 `[`/`(` 压栈，遇到 `]`/`)` 尝试弹栈匹配。若 `]` 能弹出对应的 `[`，说明这对括号在链接内部，继续吃；否则该 `]`/`)` 视为正文，停止扫描。最终若栈空，说明所有括号都配对，链接合法。
- **尾部剥除**：句末的 `.`、`?` 等通常是标点而非 URL 的一部分，用 `scout(-1)` 检查最后一个字符并 `uneat()` 回退。

#### 4.3.3 源码精读

[src/lexer.rs:1154-1189](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L1154-L1189)：

```rust
/// Extracts a prefix of the text that is a link and also returns whether the
/// parentheses and brackets in the link were balanced.
pub fn link_prefix(text: &str) -> (&str, bool) {
    let mut s = unscanny::Scanner::new(text);
    let mut brackets = Vec::new();

    #[rustfmt::skip]
    s.eat_while(|c: char| {
        match c {
            | '0' ..= '9'
            | 'a' ..= 'z'
            | 'A' ..= 'Z'
            | '!' | '#' | '$' | '%' | '&' | '*' | '+'
            | ',' | '-' | '.' | '/' | ':' | ';' | '='
            | '?' | '@' | '_' | '~' | '\'' => true,
            '[' => { brackets.push(b'['); true }
            '(' => { brackets.push(b'('); true }
            ']' => brackets.pop() == Some(b'['),
            ')' => brackets.pop() == Some(b'('),
            _ => false,
        }
    });

    // Don't include the trailing characters likely to be part of text.
    while matches!(s.scout(-1), Some('!' | ',' | '.' | ':' | ';' | '?' | '\'')) {
        s.uneat();
    }

    (s.before(), brackets.is_empty())
}
```

逐段说明：

- `brackets: Vec<u8>` 是配对栈，存 `b'['` / `b'('`。
- `eat_while` 的闭包返回 `bool`：返回 `true` 继续吃，返回 `false` 立即停。注意 `']' => brackets.pop() == Some(b'[')` 这一行很巧妙——若栈顶是 `[`，弹出返回 `true`（这对括号在链接内，继续）；若栈空或不匹配，`pop()` 返回 `None`，比较为 `false`，扫描停止。
- 第二个 `while` 循环剥尾部标点：`scout(-1)` 偷看游标前一字符，若是 `! , . : ; ? '` 就 `uneat()` 回退一格。循环用 `while` 是因为可能要剥多个（如 `...`）。
- 返回 `s.before()`（已扫描的切片）和 `brackets.is_empty()`（栈空即平衡）。

**Lexer 如何使用**：`link()` 方法（[src/lexer.rs:551-563](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L551-L563)）调用 `link_prefix`，把返回的链接长度喂给 `advance`，并在括号不平衡时产出 `Error` token：

```rust
fn link(&mut self) -> SyntaxKind {
    let (link, balanced) = link_prefix(self.s.after());
    self.s.advance(link.len());
    if !balanced {
        return self.error(
            "automatic links cannot contain unbalanced brackets, \
             use the `link` function instead",
        );
    }
    SyntaxKind::Link
}
```

这解释了返回 `bool` 的用途：平衡时产出正常的 `Link`，否则给用户一个带提示的 `Error`，引导改用 `#link()` 函数。

#### 4.3.4 代码实践

**目标**：用 `link_prefix` 从混有正文的文本里提取链接前缀，并观察尾部剥除行为。

**操作步骤**：在临时测试模块里加：

```rust
// 示例代码
#[test]
fn probe_link() {
    let cases = [
        "https://typst.org 网址",
        "见 https://typst.org。",
        "https://en.wikipedia.org/wiki/Rust_(programming_language))",
        "https://typst.org]",
    ];
    for text in cases {
        let (link, balanced) = link_prefix(text);
        println!("text={:?}\n  -> link={:?}, len={}, balanced={}", text, link, link.len(), balanced);
    }
}
```

运行 `cargo test -p typst-syntax probe_link -- --nocapture`。

**需要观察的现象**：

- `"https://typst.org 网址"` → 遇到空格停止，`link = "https://typst.org"`，长度 16，`balanced = true`。
- `"见 https://typst.org。"` → 注意此例从「见」开始，`见` 是中文不在允许集，**立刻停止**，`link = ""`。这提醒你：`link_prefix` 不负责「找到链接起点」，它假设起点已由 Lexer 的模式判定好（Lexer 只在识别到 URL scheme 开头时才调用 `link()`）。
- `"https://en.wikipedia.org/wiki/Rust_(programming_language))"` → 第一个 `)` 配对 `( ` 后继续，第二个 `)` 栈空不匹配 → 停止。`link` 含一对平衡括号，`balanced = true`，末尾多出的 `)` 留给正文。
- `"https://typst.org]"` → `]` 无匹配 `[`，停止；末尾 `g` 不是标点不剥除。`link = "https://typst.org"`，`balanced = true`。

**预期结果**：理解「贪婪吃 + 括号平衡判停 + 尾部剥标点」三段逻辑，以及为何函数假设调用方已定位到链接起点。

#### 4.3.5 小练习与答案

**练习 1**：`link_prefix("https://typst.org, see")` 返回什么？

> **答案**：扫描会吃到 `"https://typst.org,"`（逗号在允许集），随后尾部剥除把末尾 `,` 剥掉，最终 `link = "https://typst.org"`，`balanced = true`。这正是「尾部剥除」的价值——剥掉更像正文的标点。

**练习 2**：为何 `link_prefix` 要返回 `bool` 而不是直接在括号不平衡时截断链接？

> **答案**：因为括号不平衡（如 `https://x(a]b`）是一种**词法错误**，Lexer 需要用它产出一个 `Error` token 并提示用户改用 `#link()` 函数。返回 `false` 让调用方（`link()`）决定如何报错，职责分离更清晰。

---

### 4.4 多行切分 split_newlines

#### 4.4.1 概念说明

很多时候我们需要把一段多行文本按行切成 `Vec<&str>`，而且**不保留换行符本身**。典型场景是处理块级 raw 文本时，要逐行分析以计算公共缩进（dedent）。`split_newlines` 就是这个切分工具，它和 `is_newline` 配合，并额外处理 Windows 风格的 `\r\n`。

#### 4.4.2 核心流程

```
start = 0, end = 0
逐字符 eat：
  若 is_newline(c):
      若 c == '\r' 且紧跟 '\n'：把 '\n' 也吃掉（CRLF 视为一次换行）
      把 text[start..end] 压入结果（end 是换行符之前的位置）
      start = 当前游标（换行之后）
  更新 end = 当前游标
循环结束后，把最后一段 text[start..] 压入结果
```

关键点：

- `end` 比「当前字符」落后一格——它记录的是上一个字符读完后的游标位置，恰好是当前换行符**之前**的位置，所以 `text[start..end]` 正好切到换行符前。
- `\r\n` 被合并为一次换行：遇到 `\r` 后用 `eat_if('\n')` 顺手吃掉可能跟随的 `\n`，避免把 `\r\n` 当成两次换行（否则会产生一个空行）。

#### 4.4.3 源码精读

[src/lexer.rs:1191-1212](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L1191-L1212)：

```rust
/// Split text at newlines. These newline characters are not kept.
pub fn split_newlines(text: &str) -> Vec<&str> {
    let mut s = Scanner::new(text);
    let mut lines = Vec::new();
    let mut start = 0;
    let mut end = 0;

    while let Some(c) = s.eat() {
        if is_newline(c) {
            if c == '\r' {
                s.eat_if('\n');
            }
            lines.push(&text[start..end]);
            start = s.cursor();
        }
        end = s.cursor();
    }

    lines.push(&text[start..]);
    lines
}
```

逐行说明：

- `lines.push(&text[start..end])`：在遇到换行时，把「上一行内容」压入。注意 `end` 是循环末尾才更新的，所以此刻它还停在换行符之前。
- `s.eat_if('\n')`：仅在当前是 `\r` 时尝试吃掉紧跟的 `\n`，返回值被丢弃——这里只关心副作用（前进游标）。
- 末尾 `lines.push(&text[start..])`：循环结束后把最后一段（末尾无换行）压入，保证最后一行不丢。

**谁在用它**：块级 raw 的处理函数 `blocky_raw` 在 [src/lexer.rs:306](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L306) 调用 `split_newlines(s.after())` 把 raw 内容切成行，随后逐行计算 dedent（见 u3-l3 关于 raw 的讲解）。

> 关联的私有帮手 `count_newlines`（[src/lexer.rs:1215-1227](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L1215-L1227)）用同样的 `\r\n` 合并逻辑统计换行数，当你只关心「有几行」而不需要内容时用它，避免分配 `Vec`。

#### 4.4.4 代码实践

**目标**：验证 `split_newlines` 对 `\n`、`\r\n`、连续换行、Unicode 换行的处理。

**操作步骤**：在临时测试模块加：

```rust
// 示例代码
#[test]
fn probe_split() {
    let cases = [
        ("a\nb", 2),
        ("a\r\nb", 2),              // CRLF = 1 次换行
        ("a\n\nb", 3),              // 中间空行
        ("a\u{2028}b", 2),          // Unicode 行分隔符
        ("no newline", 1),
        ("trailing\n", 2),          // 末尾换行 → 多出一个空串
    ];
    for (text, n) in cases {
        let lines = split_newlines(text);
        println!("{:?} -> {} 行: {:?}", text, lines.len(), lines);
        assert_eq!(lines.len(), n);
    }
}
```

**需要观察的现象**：

- `"a\r\nb"` 切成 `["a", "b"]`（2 段），证明 CRLF 被当作一次换行，没有产生中间空行。
- `"a\n\nb"` 切成 `["a", "", "b"]`（3 段），中间空行是空串。
- `"trailing\n"` 切成 `["trailing", ""]`（2 段），末尾换行后多出一个空串——因为最后一行 `text[start..]` 是空。

**预期结果**：理解 CRLF 合并、空行用空串表示、末尾换行产生末尾空串这三个边界行为。

#### 4.4.5 小练习与答案

**练习 1**：如果删掉 `if c == '\r' { s.eat_if('\n'); }` 这两行，`"a\r\nb"` 会切成什么？

> **答案**：会切成 `["a", "", "b"]`（3 段）。因为 `\r` 和 `\n` 各自触发一次换行，中间产生一个空串。这正是要专门合并 CRLF 的原因。

**练习 2**：`split_newlines("")`（空串）返回什么？

> **答案**：返回 `[""]`（长度为 1，含一个空串）。循环不执行，直接走到末尾 `lines.push(&text[0..])` 压入一个空切片。

---

### 4.5 标签与引用字面量 is_valid_label_literal_id

#### 4.5.1 概念说明

Typst 支持给元素打**标签**（如 `<intro>`）和**引用**标签（如 `@intro`）。标签名和引用名里允许的字符集与普通标识符略有不同：除了标识符续字符，还允许 `:` 和 `.`（这样可以写分层或带版本的标签名）。

本模块有两个相关函数：

- 私有 `is_valid_in_label_literal(c)`：单字符判定，供 Lexer 在解析 `<...>` / `@...` 时逐字符吃。
- 公共 `is_valid_label_literal_id(id)`：整串判定，供外部（或校验场景）一次性确认一个字符串是否是合法的标签/引用名。

#### 4.5.2 核心流程

```
合法标签字符 = is_id_continue(c) ∪ {':', '.'}

is_valid_label_literal_id(id):
  id 非空 且 每个字符都属于「合法标签字符」
```

注意它**不**像 `is_ident` 那样区分首字符与续字符——标签字面量没有「首字符更严」的要求，每个位置都用同一套口径。

#### 4.5.3 源码精读

私有字符判定 [src/lexer.rs:1268-1272](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L1268-L1272)：

```rust
/// Whether a character can be part of a label literal's name.
#[inline]
fn is_valid_in_label_literal(c: char) -> bool {
    is_id_continue(c) || matches!(c, ':' | '.')
}
```

在 `is_id_continue` 之上，额外允许 `:` 和 `.`。

公共整串判定 [src/lexer.rs:1274-1277](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L1274-L1277)：

```rust
/// Returns true if this string is valid in a label literal.
pub fn is_valid_label_literal_id(id: &str) -> bool {
    !id.is_empty() && id.chars().all(is_valid_in_label_literal)
}
```

`!id.is_empty()` 保证空串非法（与标签 `<>` 不能为空一致），`chars().all(...)` 检查每个字符。

**Lexer 内部的使用**：解析标签 `<intro>` 的 `label()` 与解析引用 `@intro` 的 `ref_marker()` 都用 `eat_while(is_valid_in_label_literal)` 吃掉名字部分。以 `label()` 为例（[src/lexer.rs:587-598](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L587-L598)）：

```rust
fn label(&mut self) -> SyntaxKind {
    let label = self.s.eat_while(is_valid_in_label_literal);
    if label.is_empty() {
        return self.error("label cannot be empty");
    }
    if !self.s.eat_if('>') {
        return self.error("unclosed label");
    }
    SyntaxKind::Label
}
```

可见「空标签」直接报错，恰好对应 `is_valid_label_literal_id` 的 `!id.is_empty()`。`ref_marker()`（[src/lexer.rs:576-585](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L576-L585)）类似，但额外会把末尾的 `.`/`:` 剥掉（因为它们可能属于后续正文），这与 `link_prefix` 剥尾部标点的思路一致。

#### 4.5.4 代码实践

**目标**：验证哪些字符串是合法的标签/引用名，并对比 `is_ident` 的差异。

**操作步骤**：在临时测试模块加：

```rust
// 示例代码
#[test]
fn probe_label() {
    let cases = ["intro", "sec:1.2", "a.b:c", "", ".a", "a-b"];
    for id in cases {
        println!(
            "{:?} -> label_id={}, is_ident={}",
            id,
            is_valid_label_literal_id(id),
            is_ident(id)
        );
    }
}
```

**需要观察的现象**：

- `"intro"`、`"sec:1.2"`、`"a.b:c"` 的 `label_id` 都是 `true`（`:` `.` 合法）。
- `""` 为 `false`（空串非法）。
- `".a"` 的 `label_id` 为 `true`（`.` 合法，且非空），但 `is_ident` 为 `false`（`.` 不是标识符字符）。这是两者最明显的差异。
- `"a-b"` 的 `label_id` 为 `true`（`-` 在 `is_id_continue` 里），`is_ident` 也为 `true`。

**预期结果**：理解标签字面量字符集 = 标识符续字符集 ∪ {`:` `.`}，且不区分首字符。

#### 4.5.5 小练习与答案

**练习 1**：`is_valid_label_literal_id(".a")` 与 `is_ident(".a")` 为何不同？

> **答案**：前者 `true`、后者 `false`。标签字面量允许 `.` 出现在任意位置且不区分首字符；而 `is_ident` 的首字符用更严的 `is_id_start`，`.` 不满足它，所以 `.a` 不是合法标识符。

**练习 2**：为什么 `ref_marker()` 在吃完名字后还要剥掉末尾的 `.`/`:`？

> **答案**：因为正文里 `@intro.` 的句号或 `@intro:` 的冒号很可能是后续句子的标点，而非引用名的一部分。剥掉它们避免把正文标点误并入引用名，这与 `link_prefix` 剥尾部标点是同一种「宁可少 吃、留给正文」的保守策略。

---

## 5. 综合实践

**任务**：写一个小命令行小程序（或 `#[test]`），输入一段 Typst 风格的文本，用本讲的工具函数对它做一次「字符级体检」，输出诊断报告。

要求实现以下逻辑（示例代码，可在仓库外的独立小项目或临时测试里写）：

```rust
// 示例代码：综合体检。需要把 typst-syntax 作为依赖，
// 并 use typst_syntax::{is_ident, is_newline, link_prefix, split_newlines, is_valid_label_literal_id};
fn diagnose(text: &str) {
    // 1. 统计换行数：用 split_newlines 切分，打印行数与每行内容
    let lines = split_newlines(text);
    println!("共 {} 行：{:?}", lines.len(), lines);

    // 2. 找出所有「疑似链接」：遍历每个空格分隔的 token，对每个 token 调 link_prefix，
    //    若返回的前缀非空且 balanced，则报告它
    for tok in text.split_whitespace() {
        let (link, balanced) = link_prefix(tok);
        if !link.is_empty() {
            println!("疑似链接: {} (balanced={})", link, balanced);
        }
    }

    // 3. 对每个 token 判断它是「合法标识符」还是「合法标签名」
    for tok in text.split_whitespace() {
        println!(
            "{:?}: is_ident={} label_id={}",
            tok,
            is_ident(tok),
            is_valid_label_literal_id(tok)
        );
    }

    // 4. 用 is_newline 统计原始换行字符数（应与 split_newlines 行数的关系受 CRLF 影响）
    let nl_count = text.chars().filter(|c| is_newline(*c)).count();
    println!("换行字符总数: {}", nl_count);
}
```

用以下输入测试（注意含 CRLF 与中文）：

```
let x = 1  https://typst.org  sec:1.2  <intro>
```

（若想测 CRLF，可在构造字符串时插入 `\r\n`。）

**需要观察并解释的现象**：

1. `split_newlines` 的行数与 `is_newline` 统计的字符数在纯 `\n` 时相差 1（行数 = 换行数 + 1），但在含 `\r\n` 时，字符数会是行数相关的 2 倍关系——请用本讲学到的「CRLF 合并」解释差异。
2. `link_prefix("https://typst.org")` 应提取出完整链接、`balanced=true`。
3. `sec:1.2` 的 `is_ident=false` 但 `label_id=true`，体现两套口径的差异。
4. `<intro>` 因为含 `<` `>`，既非合法标识符也非合法标签名（`<` 不在标签字符集）——这符合预期，因为 `<...>` 是 Lexer 用 `label()` 整体识别的，`intro` 才是名字部分。

**预期结果**：能够把 5 个工具函数组合起来，对真实文本做出一致的字符级判断，并解释口径差异。

## 6. 本讲小结

- Typst 的字符判定工具集中在 [`src/lexer.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs) 末尾，由 [`lib.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lib.rs) 的 `pub use` 统一挂牌为公共 API（[src/lib.rs:20-23](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lib.rs#L20-L23)）。
- 标识符判定基于 Unicode UAX #31（`unicode-ident` 的 `is_xid_start/continue`），Typst 额外允许 `_` 开头、`_` 与 `-` 续接，由 `is_id_start` / `is_id_continue` / `is_ident` 三件套实现，并被 Lexer 与包清单解析复用。
- `is_newline` 用一个 `matches!` 穷举 6 个换行字符（`\n \x0B \x0C \r \u{0085} \u{2028} \u{2029}`），是全 crate 统一的换行口径。
- `link_prefix` 用 Scanner 贪婪吃链接、用括号栈判平衡、并剥掉尾部疑似正文的标点，供 Lexer 识别裸链接。
- `split_newlines` 按换行切分文本（丢弃换行符），并把 `\r\n` 合并为一次换行，是块级 raw dedent 计算的基础。
- `is_valid_label_literal_id` 定义标签/引用名的字符集（标识符续字符 ∪ {`:` `.`），与 `is_ident` 口径不同、且不区分首字符。

## 7. 下一步学习建议

- **回看 Lexer 三模式**：本讲讲的是「字符口径」，下一步建议回到 u3-l2（Markup/Code/Math 三模式词法），观察这些工具函数在 `markup()` / `code()` / `math()` 分派里被调用的真实位置，把「工具」与「调用点」对应起来。
- **进入 Parser**：字符工具产出的 token 如何被组装成 CST？建议进入 U4（语法分析 Parser），从 u4-l1（Parser 架构与入口）开始，看 Lexer 与 Parser 如何衔接。
- **延伸阅读**：阅读 [Unicode UAX #31](http://www.unicode.org/reports/tr31/) 了解标识符标准的全貌；阅读 `unicode-ident` crate 文档了解 XID_Start/Continue 的实现与性能特点。

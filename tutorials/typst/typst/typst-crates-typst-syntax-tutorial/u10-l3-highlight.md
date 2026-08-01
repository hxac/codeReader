# 语法高亮

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `typst-syntax` 把「一段 Typst 源码」变成「带颜色的编辑器视图」的整体思路：它**不重新做词法/语法分析**，而是直接遍历已有的 CST，按节点给一个 `Tag`。
- 读懂 `src/highlight.rs` 里三块核心内容：`Tag` 枚举（高亮的「调色板」）、`highlight` 函数（CST 节点 → `Tag` 的映射）、`highlight_html` 函数（把树拍平成带 `<span class>` 的 HTML）。
- 理解高亮为什么必须是**上下文相关**的——同一个 `*`、同一个标识符，颜色取决于它在树里的位置；以及 `highlight_ident` / `highlight_hash` 两个助手如何借助 `LinkedNode` 的父链做判定。
- 弄懂 `Tag::tm_scope` 如何把 Typst 的高亮标签对接到编辑器（VS Code 等）通用的 **TextMate 语法**，以及 `Tag::css_class` 如何对接网页高亮。
- 解释错误节点（`Tag::Error`）在 `highlight_html` 里被**刻意跳过**的细节。

## 2. 前置知识

本讲假设你已经掌握以下内容（均在更早的讲义中建立）：

- **CST 与 `SyntaxNode`**（U5）：typst-syntax 解析出的「具体语法树」是无损的，每个节点有一个 `SyntaxKind` 标签；叶子节点带文本，内部节点带子节点。本讲的高亮就是把 `SyntaxKind` 翻译成颜色。
- **`LinkedNode` 带父指针遍历**（u5-l3）：裸 `SyntaxNode` 只知道自己，而高亮常常要问「我爹是谁」「我后面紧跟着谁」。`LinkedNode` 在 `SyntaxNode` 之上补了**父链**和**绝对字节偏移**，并提供 `parent_kind()`、`next_sibling_kind()`、`next_leaf()`、`prev_leaf()` 等方法。本讲的 `highlight` 接收的就是 `&LinkedNode`，而不是 `&SyntaxNode`。
- **`SyntaxKind` 词汇表**（U2）：你需要大致知道 `Let`、`Ident`、`Star`、`LeftParen`、`Error` 等 kind 各代表什么。不需要背全，遇到不熟的本讲会顺带说明。
- 一个直觉：**高亮是「语法分析之后」的一道只读工序**。它不报错、不改树，只读 CST 输出标签。所以它和词法（U3）、语法（U4）是解耦的——你可以把 `highlight` 想象成「给已经建好的 CST 重新刷一层漆」。

> 名词速查
> - **TextMate 语法（TextMate grammar）**：编辑器界（TextMate → Sublime → VS Code）通用的语法高亮规则格式，用「作用域（scope）」字符串（如 `keyword.typst`、`string.quoted.double.typst`）描述每段文本的类别。`Tag::tm_scope` 就是 Typst 高亮标签到这套作用域字符串的桥。
> - **CSS class**：网页里给一段文本上色用的样式类名，本讲用 `typ-key`、`typ-str` 这种短名。

## 3. 本讲源码地图

本讲只涉及**一个**源码文件，外加一点点 `lib.rs` 的导出声明：

| 文件 | 作用 | 本讲用到什么 |
| --- | --- | --- |
| `src/highlight.rs` | 全部高亮逻辑：`Tag` 枚举、`highlight`、`highlight_html` 及两个私有助手 | 全文精读 |
| `src/lib.rs` | crate 门面 | 第 6 行 `mod highlight;`（私有模块），第 18 行 `pub use self::highlight::{Tag, highlight, highlight_html};`（把三个名字挂到 crate 根对外公开） |

需要记住的一处可见性细节：`highlight` 模块本身是私有的（`mod highlight;`），但其中的 `Tag`、`highlight`、`highlight_html` 三个名字被 `pub use` 公开了；而 `highlight_ident`、`highlight_hash`、`highlight_html_impl`、`is_ident` 四个函数是模块内私有，外部看不到。

`highlight` 还会调用 `LinkedNode`（来自 `src/node.rs`）和 `ast::Expr`（来自 `src/ast.rs`）上的若干方法，这些在前置讲义里讲过，本讲只引用不复述。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. **4.1 `Tag` 枚举**：高亮的「调色板」，外加三套对外输出（`LIST` / `tm_scope` / `css_class`）。
2. **4.2 `highlight` 主分派**：把 `SyntaxKind` 映射成 `Tag` 的大 `match`，以及「何时返回 `None`」。
3. **4.3 上下文相关高亮**：`highlight_ident` 与 `highlight_hash` 如何用父链判定函数名、插值变量。
4. **4.4 `highlight_html` 与 TextMate 衔接**：树如何拍平成 HTML、错误节点为何被跳过、`tm_scope` 如何对接编辑器。

### 4.1 `Tag` 枚举：高亮的调色板

#### 4.1.1 概念说明

高亮的本质是**给每一段文本贴一个类别标签**，至于「这个标签具体显示成什么颜色」则交给下游（编辑器主题、网页 CSS）决定。`typst-syntax` 只负责产出标签，不负责配色——这样同一套高亮结果既能喂给 VS Code、也能喂给网页、也能喂给终端。

`Tag` 枚举就是这套标签的取值集合。它故意**与 `SyntaxKind` 不同**：`SyntaxKind` 是给词法/语法用的（有 137 个变体，粒度细，区分 `LeftParen` / `RightParen` / `Star` 等具体符号），而 `Tag` 是给显示用的（22 个变体，粒度粗，把 `LeftParen` / `RightParen` / `Comma` 都归为 `Punctuation`）。这是一种典型的「**显示口径 vs 解析口径**」分离——回忆 u2-l2 提到的「谓词口径」与「标签口径」并行存在，`Tag` 正是后者。

#### 4.1.2 核心流程

`Tag` 本身只是个枚举，它的「流程」体现在三套配套输出：

```text
Tag（枚举值，如 Tag::Keyword）
  │
  ├── tag as usize  ──► 索引 Tag::LIST  得到枚举本身（用于位图/数组下标）
  ├── tag.tm_scope() ──► "keyword.typst"           （喂给编辑器 TextMate）
  └── tag.css_class()──► "typ-key"                  （喂给网页 CSS）
```

三个方法都是穷举 `match`，**一一对应、不漏不重**。设计上它们是「同一份标签的三种翻译」，所以新增一个 `Tag` 变体时，三处 `match` 必须同步加分支，否则编译器会报 `match` 不 exhaustive 的错。

#### 4.1.3 源码精读

枚举定义在 [src/highlight.rs:5-50](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/highlight.rs#L5-L50)，共 22 个变体。可粗略归成几组：

| 分组 | 变体 | 含义 |
| --- | --- | --- |
| Markup 正文 | `Strong`、`Emph`、`Link`、`Raw`、`Label`、`Ref`、`Heading`、`ListMarker`、`ListTerm`、`Escape` | 加粗、强调、链接、原始文本、标签、引用、标题、列表标记、术语、转义 |
| Math 公式 | `MathDelimiter`、`MathOperator`、`MathGroupingParens` | `$..$` 定界符、数学算符、公式里的圆括号 |
| Code 代码 | `Keyword`、`Operator`、`Number`、`String`、`Function`、`Interpolated`、`Punctuation` | 关键字、算符、数字、字符串、函数名、插值变量、标点 |
| 通用 | `Comment`、`Error` | 注释、语法错误 |

[Tag::LIST（src/highlight.rs:56-79）](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/highlight.rs#L56-L79) 是一个 `const` 数组，**按定义顺序**列出所有变体。它的文档点明用途：作为 `tag as usize` 的「反向表」。`Tag` 派生了 `#[derive(... Eq, PartialEq, Hash)]` 但**没有**显式 `#[repr(u8)]`，所以不能保证 `as usize` 的判别值连续；遇到「把一堆 tag 存进位图/数组」的场景，应当先经 `LIST` 拿到稳定的 0..22 下标，而不是直接用 `tag as usize`。

[Tag::tm_scope（src/highlight.rs:83-108）](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/highlight.rs#L83-L108) 返回 TextMate 作用域字符串，命名遵循 TextMate 惯例（点分层级，如 `keyword.typst`、`string.quoted.double.typst`、`markup.bold.typst`、`constant.numeric.typst`、`entity.name.function.typst`）。注意每个都带 `.typst` 后缀，避免和别的语言作用域混淆。

[Tag::css_class（src/highlight.rs:111-136）](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/highlight.rs#L111-L136) 返回短横线风格的 CSS 类名（`typ-key`、`typ-str`、`typ-func`…），供 `highlight_html` 使用。

> 顺带一提：u2-l2 曾经强调 `highlight.rs` **不调用** `SyntaxKind` 的 `is_*` 谓词，而是用自己的 `match` + `Tag`。本讲正式打开了这套「标签口径」体系。

#### 4.1.4 代码实践

**实践目标**：建立「一个 `Tag` 对应三种字符串」的直觉。

**操作步骤**（源码阅读型）：

1. 打开 [src/highlight.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/highlight.rs#L5-L50)，对照 4.1.3 的表格。
2. 任意挑三个变体，比如 `Function`、`Number`、`Comment`。
3. 分别到 `tm_scope` 和 `css_class` 两个 `match` 里找到它们对应的字符串。

**需要观察的现象**：三个方法的分支顺序与 `Tag` 定义顺序一致；每个 `Tag` 在三个方法里都恰好出现一次。

**预期结果**（根据源码读出，待本地验证）：

| `Tag` | `tm_scope()` | `css_class()` |
| --- | --- | --- |
| `Function` | `entity.name.function.typst` | `typ-func` |
| `Number` | `constant.numeric.typst` | `typ-num` |
| `Comment` | `comment.typst` | `typ-comment` |

#### 4.1.5 小练习与答案

**练习 1**：`Tag` 为什么不和 `SyntaxKind` 合并成一个枚举？

> **参考答案**：两者职责不同、粒度不同。`SyntaxKind` 服务于解析（要区分 `LeftParen` / `RightParen` / `Comma`），`Tag` 服务于显示（这些都归为 `Punctuation` 就够）。合并会让解析器背着显示包袱、显示器背着解析细节，两边都变臃肿。

**练习 2**：假设要新增一个 `Tag::Annotation` 变体，需要改 `highlight.rs` 里哪几处？

> **参考答案**：至少四处——枚举定义、`LIST` 数组、`tm_scope` 的 `match`、`css_class` 的 `match`。漏掉后两者会触发编译错误（`match` 不 exhaustive），所以是编译器强制的「不漏」。

---

### 4.2 `highlight`：CST 节点 → Tag 的主分派

#### 4.2.1 概念说明

`highlight` 是整个模块的核心。它的签名很轻：

```rust
pub fn highlight(node: &LinkedNode) -> Option<Tag>
```

输入一个**带父链的 CST 节点**，输出「它该刷成什么颜色」，`None` 表示「不上色」（保留默认色）。这是一个**纯函数、单点查询**：它只看当前这一个节点（必要时借助父链看上下文），不遍历整棵树——遍历是调用方（如 `highlight_html`）的事。

之所以返回 `Option<Tag>` 而非 `Tag`，是因为 CST 里大量节点根本不需要上色：纯文本 `Text`、空白 `Space`、各种结构容器（`Code`、`FuncCall`、`LetBinding`…）。这些一律返回 `None`，调用方就跳过它们、只对叶子或被点名的节点刷漆。

#### 4.2.2 核心流程

```text
highlight(node)
  │
  ├─ 先看 node.kind()（SyntaxKind）做主分派
  │     │
  │     ├─ 大量 kind → 固定 Tag（如 Let→Keyword, Int→Number, Str→String）
  │     ├─ 大量结构容器/纯文本 → None
  │     └─ 少数 kind → 上下文相关，转交助手：
  │           · Ident / MathIdent → highlight_ident(node)
  │           · Hash              → highlight_hash(node)
  │           · LeftParen / Star / Slash / Eq 等 → 看 parent_kind() 决定
  │
  └─ 返回 Option<Tag>
```

主分派有两类「上下文相关」情况：一类转交助手（4.3 详讲），一类就地用 `parent_kind()` / `next_sibling_kind()` 判断（下面精读）。后者是高亮里最值得琢磨的设计——**同一个字符在不同句法位置颜色不同**。

#### 4.2.3 源码精读

主分派是 [src/highlight.rs:142-311](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/highlight.rs#L142-L311) 的大 `match`。先看几条「固定映射」的硬规则：

- 关键字一律 `Tag::Keyword`：[第 244-263 行](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/highlight.rs#L244-L263)把 `Not`/`And`/`Or`/`None`/`Auto`/`Let`/`Set`/`Show`/`Context`/`If`/`Else`/`For`/`In`/`While`/`Break`/`Continue`/`Return`/`Import`/`Include`/`As` 全部映到 `Keyword`。
- 字面量：`Bool → Keyword`（注意布尔值 `true`/`false` 当关键字着色）、`Int/Float/Numeric → Number`、`Str → String`，见 [第 267-271 行](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/highlight.rs#L267-L271)。
- 注释：`Shebang/LineComment/BlockComment → Comment`，[第 305-307 行](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/highlight.rs#L305-L307)。
- 错误：`Error → Tag::Error`，[第 308 行](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/highlight.rs#L308)。

再看几条**就地上下文相关**的精妙分支，它们都用 `node.parent_kind()`（[node.rs:1277](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1277)）问父节点：

1. **圆括号** [第 197-204 行](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/highlight.rs#L197-L204)：`LeftParen`/`RightParen` 在普通代码里是 `Punctuation`，但如果父亲是 `Math`（公式），就是 `MathGroupingParens`——同样的 `(`，在 `$a(b)$` 里和 `f(b)` 里颜色不同。

2. **星号 `*`** [第 208-211 行](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/highlight.rs#L208-L211)：
   ```rust
   SyntaxKind::Star => match node.parent_kind() {
       Some(SyntaxKind::Strong) => None,   // *hi* 里的 * 是加粗定界符，颜色交给 Strong 父节点
       _ => Some(Tag::Operator),            // 否则（如 2 * 3）当乘法算符
   },
   ```

3. **斜杠 `/`** [第 219-222 行](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/highlight.rs#L219-L222)：父亲是 `MathFrac`（公式分式）→ `MathOperator`，否则 → `Operator`。

4. **等号 `=`** [第 225-228 行](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/highlight.rs#L225-L228)：父亲是 `Heading`（`= 标题`）→ `None`（标题记号不单独上色），否则 → `Operator`。

还有一条用了 `next_sibling_kind()`（[node.rs:1287](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1287)）的**术语列表特例** [第 144-149 行](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/highlight.rs#L144-L149)：
```rust
SyntaxKind::Markup
    if node.parent_kind() == Some(SyntaxKind::TermItem)
        && node.next_sibling_kind() == Some(SyntaxKind::Colon) =>
{
    Some(Tag::ListTerm)   // / 术语: 描述  ——「术语」那部分正文刷成 ListTerm
}
```
普通正文 `Markup` 是 `None`，但若是「术语项」里、且后面紧跟冒号的那段正文，就刷成 `ListTerm`。这是用「父 + 右兄弟」两个条件精准锁定位置。

最后注意**结构容器几乎全是 `None`**：[第 265-303 行](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/highlight.rs#L265-L303)把 `Code`、`CodeBlock`、`FuncCall`、`LetBinding`、`Conditional`、`ForLoop` 等几十个结构节点一律返回 `None`。原因：高亮只对**有文本的叶子或被点名的标记**上色，结构容器本身不直接对应可见文本，颜色由它内部的子节点各自决定。

#### 4.2.4 代码实践

**实践目标**：验证「同一字符，颜色随上下文变化」。

**操作步骤**（源码阅读 + 本地运行，二选一）：

1. 阅读 [src/highlight.rs:432-486](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/highlight.rs#L432-L486) 的 `#[cfg(test)] mod tests`，看 `test_highlighting` 如何用 `highlight` 把一段文本拍成 `(Range<usize>, Tag)` 列表。
2. 在仓库里运行测试，观察断言：
   ```bash
   cargo test -p typst-syntax highlight::tests
   ```

**需要观察的现象**：测试 `#f(x + 1)` 的期望里，`(` 和 `)` 都是 `Punctuation`（[第 467、470 行](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/highlight.rs#L467-L470)），因为这里的圆括号父亲是 `Args` 而非 `Math`。

**预期结果**：测试通过。若你在脑中把 `#f(x + 1)` 改成公式 `$f(x + 1)$`，根据 [第 197-200 行](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/highlight.rs#L197-L200)，那对圆括号的 `Tag` 应当从 `Punctuation` 变成 `MathGroupingParens`——这点待本地验证（可在测试里加一条用例确认）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `highlight` 不接收 `&SyntaxNode` 而要 `&LinkedNode`？

> **参考答案**：因为它要频繁地问「父节点是什么 kind」「前一个/后一个叶子是什么」——这些信息裸 `SyntaxNode` 没有，只有带父链和绝对偏移的 `LinkedNode` 才能提供（见 u5-l3）。所以高亮是建立在「可导航的树视图」之上，而非裸节点之上。

**练习 2**：`FuncCall`、`LetBinding` 这类结构节点为什么返回 `None`？

> **参考答案**：它们本身不直接对应可见文本，没有「自己的一段字符」要上色。真正可见的是它们内部的子节点（关键字 `let`、函数名、算符等），这些子节点各自被分派到具体 `Tag`。容器返回 `None`，调用方就跳过它、继续往子节点走。

---

### 4.3 上下文相关高亮：`highlight_ident` 与 `highlight_hash`

#### 4.3.1 概念说明

有一类节点单看 `kind` 无法决定颜色，必须结合**兄弟叶子**和**祖先链**。最典型的是两类：

- **标识符（`Ident` / `MathIdent`）**：一个名字到底是「函数名」（`f` in `f(x)`）还是「插值变量」（`#x`）还是「普通变量」？取决于它周围是谁。
- **井号 `Hash`**：`#let`、`#f(...)`、`#[...]` 里的 `#` 是「插值引导符」，它本身的颜色应当跟随它引导的那个表达式。

为此 `highlight` 把这两种 kind 转交给两个私有助手：[highlight_ident（src/highlight.rs:314-369）](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/highlight.rs#L314-L369) 和 [highlight_hash（src/highlight.rs:372-379）](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/highlight.rs#L372-L379)。

#### 4.3.2 核心流程

`highlight_ident` 按**优先级从高到低**依次判断，命中即返回：

```text
highlight_ident(node)
  1. 紧贴后一个叶子是 ( 且父为 Args/MathArgs/Params    → Function（函数调用名）
     或紧贴后一个叶子是 [ 且父为 ContentBlock          → Function（内容块调用名）
  2. 自己在数学公式里（MathIdent/MathFieldAccess）       → Interpolated
  3. （沿 FieldAccess 链向上找祖先）
     祖先父为 ShowRule 且邻接冒号                       → Function（show 规则的选择器函数）
  4. 祖先的前一个叶子是 #                              → Interpolated（# 插值变量）
  5. 前一个叶子是 . 且再前一个是标识符                  → 递归复用那个标识符的 Tag
                                                          （让 a.b.c 的 b、c 沿用 a 的颜色）
  6. 都不满足                                            → None
```

`highlight_hash` 更短：拿到 `#` 的右兄弟，把它转成 `ast::Expr`，若该表达式「可用 `#` 嵌入」（`expr.hash()` 为真），就**返回它最左叶子的 `Tag`**——也就是说 `#` 偷用紧跟它的那个词的颜色。

```text
highlight_hash(node)            // node 是 Hash 节点
  next = node.next_sibling()?    // # 后面那个表达式兄弟
  expr = next.cast::<ast::Expr>()?
  if !expr.hash() { return None }   // 比如 #1 不是合法插值，# 不上色
  highlight(&next.leftmost_leaf()?)  // 用「最左叶子」的颜色给 # 自己
```

#### 4.3.3 源码精读

先看 `highlight_ident` 的「函数名」判定，[第 316-333 行](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/highlight.rs#L316-L333)。它用 `node.next_leaf()`（[node.rs:1391](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1391)）取**下一个叶子**，并要求两者**偏移紧贴**（`node.range().end == next.offset()`）——中间不能有空格。满足「紧贴 `(` 且父为参数列表」或「紧贴 `[` 且父为内容块」才认定是函数名：

```rust
let next_leaf = node.next_leaf();
if let Some(next) = &next_leaf
    && node.range().end == next.offset()
    && ((
        next.kind() == SyntaxKind::LeftParen
            && matches!(
                next.parent_kind(),
                Some(SyntaxKind::Args | SyntaxKind::MathArgs | SyntaxKind::Params)
            )
    ) || (
        next.kind() == SyntaxKind::LeftBracket
            && next.parent_kind() == Some(SyntaxKind::ContentBlock)
    ))
{
    return Some(Tag::Function);
}
```

「紧贴」要求很关键：`f (x)`（中间有空格）里的 `f` 不会被判为函数名，因为 `range().end != next.offset()`。

「字段链向上」逻辑在 [第 340-345 行](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/highlight.rs#L340-L345)，用一个 `while` 循环沿 `FieldAccess` 父链上行，找到第一个非字段访问的祖先，以便判断 `show rect: ...` 这种选择器场景（[第 347-352 行](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/highlight.rs#L347-L352)）和 `#` 插值（[第 354-357 行](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/highlight.rs#L354-L357)）。

最妙的是「点号传播」，[第 359-366 行](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/highlight.rs#L359-L366)：
```rust
let prev = node.prev_leaf()?;
if prev.kind() == SyntaxKind::Dot {
    let prev_prev = prev.prev_leaf()?;
    if is_ident(&prev_prev) {
        return highlight_ident(&prev_prev);   // 复用前一个标识符的判定
    }
}
```
这让 `a.b.c` 中的 `b`、`c` 沿用 `a` 的颜色：如果 `a` 是函数名，整条链都是函数色；如果 `a` 是 `#` 插值变量，整条链都是插值色。一个递归把链上颜色「拉齐」。

再看 `highlight_hash`，[第 372-379 行](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/highlight.rs#L372-L379)。关键是 `expr.hash()`——这是 `ast::Expr` 上的方法，定义在 [ast.rs:533](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L533)，判断「这个表达式能否用 `#` 嵌入 markup」。比如 `#let`、`#f(x)`、`#x` 都可以，而裸数字 `#1` 不行——所以 `#` 在后者情况下返回 `None`，不上色。

`highlight_hash` 最后调用 `next.leftmost_leaf()`（[node.rs:1314](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1314)）拿到表达式最左那个叶子，再递归 `highlight` 它。这样 `#let x = 1` 里的 `#` 就和 `let` 一样刷成 `Keyword`，`#f(x)` 里的 `#` 和 `f` 一样刷成 `Function`。测试 [第 474-484 行](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/highlight.rs#L474-L484) 正好印证：`#let f(x) = x` 中 `#`(0..1) 与 `let`(1..4) 都是 `Keyword`，`f`(5..6) 是 `Function`。

#### 4.3.4 代码实践

**实践目标**：亲手验证 `highlight_ident` 的「紧贴」与「点号传播」两条规则。

**操作步骤**（源码阅读 + 本地运行）：

1. 在测试模块里读 [test("#f(x + 1)", ...)](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/highlight.rs#L462-L472)，确认 `f` 的 `Tag` 是 `Function`。
2. 在仓库内写一个临时测试（或用 `cargo test` 既有用例），构造两段文本：
   - `#f(x)`：`f` 紧贴 `(`。
   - `#f (x)`：`f` 与 `(` 之间有空格。
3. 分别对 `f` 这个叶子调用 `highlight`。

**需要观察的现象**：前者 `f → Some(Function)`；后者因为不满足「紧贴」，`f` 落到 `highlight_ident` 末尾返回 `None`（不上色），同时由于 `f` 前面有 `#`，会被第 4 条「祖先前叶子是 `#`」判为 `Interpolated`——注意这两条规则的交互。

**预期结果**：`#f(x)` 的 `f` 是 `Function`；`#f (x)` 的 `f` 是 `Interpolated`（被 `#` 拉成插值色），而那个孤立的 `(x)` 不构成函数调用。具体输出待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`a.b.c` 中 `c` 的颜色如何决定？

> **参考答案**：对 `c` 调 `highlight_ident`，前一个叶子是 `.`，再前一个是 `b`（标识符），于是递归 `highlight_ident(&b)`；`b` 同理递归到 `a`。最终 `c` 沿用 `a` 的判定结果：若 `a` 是函数名则 `c` 也是 `Function`，若 `a` 是 `#` 插值则 `c` 是 `Interpolated`。

**练习 2**：`highlight_hash` 为什么不直接返回固定 `Tag`，而要去看右兄弟的最左叶子？

> **参考答案**：因为 `#` 的语义完全取决于它引导的表达式——`#let` 是关键字、`#f(..)` 是函数、`#x` 是变量。让 `#`「借用」被引导词的颜色，视觉上 `#` 和它引导的词同色，更符合阅读直觉。

---

### 4.4 `highlight_html` 与 TextMate 衔接，以及错误节点

#### 4.4.1 概念说明

有了 `highlight(node) -> Option<Tag>` 这个单点查询，剩下的问题就是「怎么遍历整棵树、把结果拍平成某种输出格式」。`highlight_html` 是模块里给出的一个**参考实现**：它把 CST 拍平成一段 HTML，每个上色片段包在 `<span class="typ-xxx">...</span>` 里。

`highlight_html` 解决三件事：

1. **遍历**：用 `LinkedNode` 递归走整棵树。
2. **上色**：对每个节点查 `highlight`，命中（且非 `Error`）就包 `<span>`。
3. **转义**：把 `<`、`>`、`&` 等字符转成 HTML 实体，保证生成的 HTML 安全。

同时，`Tag::tm_scope` 把同一份 `Tag` 翻译成 TextMate 作用域字符串——这是 Typst 对接 **VS Code 等编辑器**高亮的桥梁。编辑器不用 `highlight_html`，它用自己的 TextMate 引擎，但作用域命名与 `tm_scope` 对齐，保证颜色一致。

#### 4.4.2 核心流程

```text
highlight_html(root) -> String
  buf = "<code>"
  从 root 造一个 LinkedNode
  highlight_html_impl(buf, node)        # 递归
  buf += "</code>"
  return buf

highlight_html_impl(buf, node):
  span = false
  tag = highlight(node)
  if tag 是 Some 且 tag != Error:        # ← 关键：Error 被刻意跳过
      buf += "<span class=\"{tag.css_class()}\">"
      span = true

  text = node.leaf_text()                # 叶子节点的文本（内部节点返回空）
  if text 非空:
      逐字符 HTML 转义后追加              # < > & ' " 五个字符特判
  else:
      for child in node.children():
          highlight_html_impl(buf, child)   # 内部节点：递归子节点

  if span: buf += "</span>"
```

关键设计有三：

- **`leaf_text` 决定停止递归**：叶子节点有文本，直接转义输出后不再下钻；内部节点 `leaf_text` 返回空，于是去递归子节点（见 u5-l2 对 `leaf_text` 的说明）。
- **`Error` 不包 span**：错误节点虽然 `highlight` 返回 `Some(Tag::Error)`，但 `highlight_html_impl` 在 [第 400-407 行](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/highlight.rs#L400-L407) 用 `&& tag != Tag::Error` 显式排除了它——错误文本照常输出（转义后），但不套 `typ-error` 的 span。这意味着网页高亮里**错误不会单独标红**，留给下游诊断系统去画波浪线。
- **HTML 转义**：`'` → `&#39;`、`"` → `&quot;` 等五个字符手工替换（[第 411-419 行](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/highlight.rs#L411-L419)），保证生成的 HTML 嵌进网页不会破坏标签结构。

#### 4.4.3 源码精读

入口 [highlight_html（src/highlight.rs:389-395）](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/highlight.rs#L389-L395) 只做外壳：套 `<code>...</code>`，把根节点包成 `LinkedNode::new(root)`（[node.rs:1081](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1081)），转交 `highlight_html_impl`。

递归体 [highlight_html_impl（src/highlight.rs:398-430）](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/highlight.rs#L398-L430)。开 span 的判断：

```rust
let mut span = false;
if let Some(tag) = highlight(node)
    && tag != Tag::Error
{
    span = true;
    html.push_str("<span class=\"");
    html.push_str(tag.css_class());
    html.push_str("\">");
}
```

注意 `let ... && ...` 是 Rust 的 `let`-chains（链式 `let`），两个条件都满足才开 span。

文本获取用 `node.leaf_text()`（定义在 [node.rs:247](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L247) 的 `SyntaxNode::leaf_text`，`LinkedNode` 经 `Deref` 透传）：叶子返回自身文本，内部节点返回空串。于是 `if !text.is_empty()` 分支处理叶子（转义输出），`else` 分支处理内部节点（递归子节点）。这是「自然停在叶子」的遍历——既不会把同一文本输出两次，也不会漏掉任何字符。

至于 TextMate 衔接：[Tag::tm_scope（src/highlight.rs:83-108）](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/highlight.rs#L83-L108) 提供作用域字符串，但 `typst-syntax` 这个 crate **本身不包含** TextMate 语法文件（`.tmLanguage.json`）。`tm_scope` 是「契约」：Typst 官方的 VS Code 扩展会提供一份 TextMate 语法，其作用域命名与这里的返回值对齐，从而让编辑器高亮与 `highlight_html` 输出在语义上同源。

#### 4.4.4 代码实践

**实践目标**：用 `highlight_html` 真正生成一段带 `<span class>` 的 HTML，观察关键字、字符串、注释对应的类名。

**操作步骤**（本地运行型）：

1. 在仓库**外**新建一个小 Rust 项目（不要改本仓库源码）：
   ```bash
   cargo new hl-demo && cd hl-demo
   ```
2. 在 `Cargo.toml` 加入 git 依赖（或在本仓库 workspace 内加一个临时 example）：
   ```toml
   [dependencies]
   typst-syntax = { git = "https://github.com/typst/typst", rev = "32fd4cc3" }
   ```
3. 在 `src/main.rs` 写：
   ```rust
   use typst_syntax::{highlight_html, parse};

   fn main() {
       let src = "#let x = \"hi\" // 注释\nx + 1";
       let root = parse(src);
       let html = highlight_html(&root);
       println!("{html}");
   }
   ```
4. `cargo run` 查看输出。

**需要观察的现象**：

- `#let` 的 `#` 和 `let` 都应在 `<span class="typ-key">` 里。
- `"hi"` 应在 `<span class="typ-str">` 里。
- `// 注释` 应在 `<span class="typ-comment">` 里。
- `1` 应在 `<span class="typ-num">` 里。
- 注释里的中文 `<` `>` 若有，应被转义成 `&lt;` `&gt;`。

**预期结果**（根据源码推演，**待本地验证**）：输出形如
```html
<code><span class="typ-key">#let</span> x <span class="typ-op">=</span>
 <span class="typ-str">&quot;hi&quot;</span> <span class="typ-comment">// 注释</span>
x <span class="typ-op">+</span> <span class="typ-num">1</span></code>
```
注意 `#` 与 `let` 是否各自独立成 span、空白 `x ` 是否带 span，取决于 `highlight` 逐节点的判定，以本地实际输出为准。

> 若无法联网拉取 git 依赖，可改在本仓库内 `cargo test -p typst-syntax highlight::tests` 运行既有测试，或临时在 `src/highlight.rs` 的 `#[cfg(test)] mod tests` 里加一条用 `highlight_html` 的断言（这是你自己的本地实验，不影响提交）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `highlight_html_impl` 对 `Tag::Error` 刻意不包 `<span>`？

> **参考答案**：高亮与诊断是两套系统。错误标记（红色波浪线、错误列表）由诊断系统（U5 的 `SyntaxDiagnostic`）负责，`highlight_html` 只管「语法类别上色」。若在这里也把错误文本包成 `typ-error`，会和诊断重叠、造成双重标红，所以刻意跳过，错误文本只做普通转义输出。

**练习 2**：`highlight_html` 如何保证不会把同一段文本输出两次，也不会漏掉任何字符？

> **参考答案**：靠 `leaf_text()` 的「叶子非空、内部为空」二分：叶子节点直接转义输出自身文本后**不再下钻**，内部节点因 `leaf_text` 返回空而**转入递归子节点**。两者职责互补，每个字符恰好在它所属的叶子节点被输出一次。

---

## 5. 综合实践

把本讲四个模块串起来，做一个「**迷你高亮器**」实验，加深对「`Tag` 调色板 + `highlight` 分派 + 上下文 + HTML 拍平」整体链路的理解。

**任务**：写一个小程序，对一段 Typst 源码生成一份「**字节区间 → Tag**」的清单（而不是 HTML），并按颜色分组统计每种 `Tag` 覆盖了多少个节点。

**建议步骤**：

1. 用 `typst_syntax::parse(src)` 得到根 `SyntaxNode`。
2. 用 `LinkedNode::new(&root)` 包成可导航节点（参考 [测试里的 highlight_tree 写法，src/highlight.rs:450-458](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/highlight.rs#L450-L458)）。
3. 递归遍历：对每个节点调 `highlight`，命中就记录 `(node.range(), tag)`，并递归 `node.children()`。
4. 把结果按 `tag` 分组计数，打印一张表，形如：
   ```text
   Keyword     : 3 个节点
   Function    : 1 个节点
   String      : 1 个节点
   Number      : 2 个节点
   Comment     : 1 个节点
   Operator    : 2 个节点
   Punctuation : ...
   ```
5. 选用一段尽量「全」的样例，覆盖关键字、字符串、注释、函数名、数字、公式定界符等，例如：
   ```typst
   #let f(x) = x + 1 // 加一
   = 标题
   $a/b$ 与 *加粗* 和 `raw`
   ```

**观察重点**：

- `#let` 的 `#` 与 `let` 是否都算 `Keyword`？（验证 4.3 的 `highlight_hash`）
- `$` 是否算 `MathDelimiter`？`/` 在 `$a/b$` 里是否算 `MathOperator`、在 `x + 1` 之外却不是？（验证 4.2 的上下文分支）
- `f` 是否算 `Function`？`raw` 是否算 `Raw`？（验证 `Tag` 调色板）

**预期结果**：你的清单应当能区分出公式内的 `/`（`MathOperator`）与普通算符 `+`（`Operator`），并把 `#let` 整体归到 `Keyword`。具体计数待本地验证。

> 进阶：把这份「区间 → Tag」清单再用 `Tag::tm_scope` 翻译成作用域字符串，体会「同一份高亮结果既能喂 HTML（`css_class`）也能喂编辑器（`tm_scope`）」的分离设计。

## 6. 本讲小结

- `Tag` 是高亮的「调色板」，22 个变体，粒度比 `SyntaxKind` 粗，专门服务于显示；它有三套一一对应的输出：`LIST`（稳定下标）、`tm_scope`（编辑器 TextMate 作用域）、`css_class`（网页 CSS 类）。
- `highlight(node: &LinkedNode) -> Option<Tag>` 是核心：一个**纯函数、单点查询**，按 `SyntaxKind` 主分派；结构容器与纯文本返回 `None`，由调用方跳过。
- 高亮是**上下文相关**的：`*`、`/`、`(`、`=` 等符号的颜色取决于 `parent_kind()`；标识符颜色由 `highlight_ident` 用「紧贴 `(`/`[`、点号传播、`#` 插值、show 冒号」等规则判定；`#` 的颜色由 `highlight_hash`「借用」它引导表达式的最左叶子。
- `highlight_html` 是把树拍平成 HTML 的参考实现：叶子节点转义输出文本、内部节点递归子节点，靠 `leaf_text()` 的二分保证不重不漏；并**刻意不给 `Tag::Error` 包 span**，把错误标记留给诊断系统。
- `tm_scope` 是 `typst-syntax` 与编辑器高亮的**契约**：作用域命名与 Typst 官方 VS Code 扩展的 TextMate 语法对齐，使网页与编辑器高亮同源。

## 7. 下一步学习建议

- **U10 收尾**：下一讲 u10-l4「二次开发指南与测试」会综合本讲，给出「新增一个语法构造时，kind → lexer → parser → ast → **highlight** → set」的完整改动链路，并讲解各文件内联测试的组织方式。届时你会看到：新增 `SyntaxKind` 必须同步在 `highlight` 的大 `match` 里补一个分支，否则该构造在编辑器里没有颜色。
- **回看 U5**：若你对 `highlight_html_impl` 里 `leaf_text()` 的二分遍历、`LinkedNode` 的父链导航还不够熟，建议重读 u5-l2（构造与访问）与 u5-l3（LinkedNode 遍历）。
- **延伸阅读**：`Tag::tm_scope` 的返回值是 TextMate 作用域命名规范的实例，可查阅 [TextMate 语言 grammars 文档](https://macromates.com/manual/en/language_grammars) 了解 `keyword`、`string.quoted.double`、`entity.name.function` 等层级化作用域的约定，理解 Typst 为什么这样命名。
- **动手方向**：试着为某个 Typst 尚未单独上色的构造（如果你发现的话）在本地 fork 里给 `highlight` 加一条分支，并在 `#[cfg(test)] mod tests` 的 `test_highlighting` 里加一条断言，跑通 `cargo test -p typst-syntax highlight`，体会「显示口径」与「解析口径」协同更新的过程。

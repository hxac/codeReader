# Markup 解析

## 1. 本讲目标

本讲聚焦 `typst-syntax` 解析器中 **Markup（正文）模式** 的解析逻辑。学完后你应当能够：

1. 说清 `markup_exprs` 的主循环如何用 `at_start`（行首状态）驱动一串 markup 表达式的解析；
2. 读懂 `markup_expr` 这个「分发枢纽」如何根据当前 token 选择不同的解析分支；
3. 解释标题（`=`）、列表（`-`）、枚举（`+`/`1.`）、术语（`/`）这四类**行首标记**为何同时需要 lexer 与 parser 两道关卡；
4. 理解 `*...*`（strong）、`_..._`（emph）如何用 marker + wrap + 换行模式构造子树；
5. 看懂 `@ref`、`$...$` 如何在 markup 中触发，以及 `#`、`$`、`[ ]` 如何通过 `enter_modes` 切换到 Code / Math / Content 三种模式。

本讲只读一个文件 `src/parser.rs`，但会少量联系 `src/lexer.rs`（标记 token 的产生）与 `src/ast.rs`（节点视图）来把因果链补全。

## 2. 前置知识

在进入本讲前，请确认你已经掌握以下概念（它们在前置讲义中讲过）：

- **SyntaxMode 三模式**：Markup（正文）、Math（公式 `$...$`）、Code（`#` 后的代码）。同一字符在不同模式下会被 lexer 切成不同 token（见 u3-l2）。
- **Marker + wrap 事件式解析**：解析函数先把 token 推进扁平的 `nodes` 向量，用 `marker()` 记下位置戳，子树解析完再用 `wrap(m, kind)` 事后打包成内部节点（见 u4-l2）。
- **单 token 前瞻 `Token`**：parser 始终持有一个尚未落入 `nodes` 的「当前 token」，并通过 `current()`/`at()`/`at_set()` 查询它，`eat()` 消费它（见 u4-l2）。
- **`AtNewline` 换行模式**：parser 用 `with_nl_mode` 临时把「换行」伪造成 `SyntaxKind::End` 来叫停上层循环，从而让换行参与语法判定（见 u4-l2）。
- **trivia**：空白、注释等可跳过片段。它们已在 `lex()` 阶段被推进 `nodes`，当前 token 只用 `n_trivia` 计数指回（见 u4-l2）。

本讲会反复用到一句话：**Markup 模式的解析本质是「循环吃表达式」，而行首位置 `at_start` 决定了某些字符是「结构标记」还是「普通文本」。**

## 3. 本讲源码地图

本讲只涉及一个核心源文件，但会交叉引用另两个文件来补全因果：

| 文件 | 本讲涉及内容 |
| --- | --- |
| `src/parser.rs` | 全部 markup 解析函数：`parse`、`markup`、`markup_exprs`、`markup_expr`、`strong`/`emph`/`heading`/`list_item`/`enum_item`/`term_item`/`reference`/`equation`，以及模式切换原语 `enter_modes`/`with_nl_mode`/`lex` |
| `src/lexer.rs` | `=`/`-`/`+`/`/`/`1.`/`@` 这些字符如何被切成 `*Marker` / `RefMarker` token，以及 `space_or_end` 判定 |
| `src/ast.rs` | `Markup`、`Strong`、`Equation` 等节点的类型化视图，帮助理解 CST 子节点结构 |

> 提醒：以下所有永久链接的 HEAD 均为 `32fd4cc3861e0ab99f4c42ca6bea281482ba9f51`。

## 4. 核心概念与源码讲解

### 4.1 markup 表达式序列：`markup_exprs` 与 `at_start`

#### 4.1.1 概念说明

一段 Typst 正文（Markup）并不是一棵「先有整体结构再有内容」的树，而是一串**平铺的表达式序列**：标题、列表项、加粗片段、普通文本、引用、公式……一个接一个排列。解析 Markup 的核心，就是一个循环：**「只要还没遇到停止条件，就解析下一个 markup 表达式」**。

这个循环里有一个关键状态 `at_start`：它表示「当前 token 是否位于一行的开头」。为什么行首这么重要？因为在 Typst 里，`=`、`-`、`+`、`/` 只有出现在行首才是标题、列表、枚举、术语；出现在行中就是普通文本（`a = b` 里的 `=` 不是标题）。所以 parser 必须知道自己「是不是在行首」才能正确分发。

#### 4.1.2 核心流程

`markup_exprs` 的执行流程可以概括为：

1. 入口先做一次深度检查（防栈溢出，见 u4-l1）。
2. 用 `at_start |= p.had_newline()` 把「调用者传入的行首提示」与「当前 token 前置 trivia 是否含换行」合并，得到真正的行首状态。
3. 进入主循环：只要当前 token 不属于停止集合 `stop_set`，就调用 `markup_expr(p, at_start, &mut nesting)` 解析一个表达式。
4. 每解析完一个表达式，用 `at_start = p.had_newline()` **刷新**行首状态——即「下一个 token 是否在新行开头」。
5. 循环里还有一个 `nesting` 计数器，用来在嵌套 `[ ]` 时即使遇到停止 token 也继续吃掉匹配的右括号（详见 4.2）。

用伪代码表示：

```
fn markup_exprs(p, at_start, stop_set):
    at_start = at_start 或 当前token前有换行
    nesting = 0
    while 不是停止token 或 (嵌套中且当前是 RightBracket):
        markup_expr(p, at_start, nesting)   # 解析一个表达式
        at_start = 当前token前有换行          # 刷新行首状态
```

#### 4.1.3 源码精读

先看 `markup_exprs` 的完整定义：

[`src/parser.rs:50-61`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L50-L61) —— markup 表达式序列的主循环。注意第 54 行的 `at_start |= p.had_newline()`（合并行首提示）和第 59 行的 `at_start = p.had_newline()`（每轮刷新），这两行是 `at_start` 状态机的核心。

其中 `had_newline()` 的实现只有一行：

[`src/parser.rs:1683-1685`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1683-L1685) —— 判断当前 token 的前置 trivia 中是否含有换行。`at_start` 完全由它派生。

而 `markup_exprs` 的调用者之一，就是顶层的 `parse` 入口：

[`src/parser.rs:16-21`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L16-L21) —— 顶层 `parse` 以 `SyntaxMode::Markup` 启动解析器，调用 `markup_exprs(&mut p, true, ...)`，第二个参数 `true` 就是初始 `at_start`（文档开头天然算行首），停止集合只有 `End`（解析到文本末尾才停）。

另一个常用包装是 `markup`，它在 `markup_exprs` 外面套了一层 marker + wrap，便于把「一段正文」整体打包成一个 `Markup` 内部节点（供标题体、列表体等复用）：

[`src/parser.rs:40-47`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L40-L47) —— `markup` = `marker()`（或 `before_trivia()`）+ `markup_exprs` + `wrap(Markup)`。`wrap_trivia` 参数决定这段正文的边界要不要把前置 trivia 也圈进来（strong/emph 需要，标题体不需要）。

#### 4.1.4 代码实践

**实践目标**：用一个小程序验证「顶层 Markup 的直接子节点就是一串表达式，且换行会以 Space（trivia）节点形式插在中间」。

**操作步骤**（示例代码，需自行创建小项目或在本仓库 `cargo test -p typst-syntax` 环境中验证）：

```rust
// 示例代码：非项目原有代码
use typst_syntax::parse;

fn main() {
    let root = parse("第一行文本\n第二行文本");
    println!("根节点 kind: {:?}", root.kind()); // 期望: Markup
    for child in root.children() {
        println!("  - {:?}: {:?}", child.kind(), child.text());
    }
}
```

**需要观察的现象**：根节点是 `Markup`；它的直接子节点里会同时出现 `Text("第一行文本")`、`Space`（那个换行）、`Text("第二行文本")`。

**预期结果**：换行被 lexer 切成 trivia（`Space`），并直接作为顶层 `Markup` 的子节点出现——这正是「Markup 是一串平铺表达式」的体现。trivia 的精确归属「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `markup_exprs` 第 59 行的 `at_start = p.had_newline()` 改成 `at_start = false`（每轮都不再判定行首），文档里哪些构造会解析错误？

**参考答案**：所有依赖行首的标记都会失效。例如 `- 列表项` 若出现在换行后，因 `at_start` 恒为 false，`ListMarker` 不会触发 `list_item()`，而会落到 4.2 里那条「转换成普通文本」的兜底分支，于是列表项退化成字面文本 `- 列表项`。标题、枚举、术语同理。

**练习 2**：`markup_exprs` 的 `stop_set` 为什么必须包含 `SyntaxKind::End`？

**参考答案**：`End` 表示「文本末尾（或被换行模式伪造的临时结尾）」。主循环条件是 `!p.at_set(stop_set)`，若 `stop_set` 不含 `End`，解析器读完整篇文本仍不知道该停止，会陷入死循环。源码第 51 行的 `debug_assert!(stop_set.contains(SyntaxKind::End))` 正是这条不变量。

---

### 4.2 单个 markup 表达式的分发：`markup_expr`

#### 4.2.1 概念说明

`markup_exprs` 的循环每轮都调用 `markup_expr`，它是一个**分发枢纽**：根据当前 token 的 `SyntaxKind`，选择对应的解析分支。它覆盖了正文里几乎所有元素：纯文本片段、转义、智能引号、链接、标签、raw、加粗、着重、标题、列表、枚举、术语、引用、公式，以及嵌入式代码（`#`）。

`markup_expr` 最值得关注的设计有两点：

1. **行首标记的「两道关卡」**：`HeadingMarker`/`ListMarker`/`EnumMarker`/`TermMarker` 这四个分支都带了 `if at_start` 守卫。也就是说，光有 lexer 产出这些 marker token 还不够，parser 还要确认「确实在行首」才会把它们当结构标记；否则落到兜底分支转成普通文本。
2. **`[ ]` 的嵌套计数 `nesting`**：在纯正文里，`[` 和 `]` 默认是字面文本（内容块 `[...]` 只在代码上下文如函数调用、或 `@ref` 之后才有意义）。parser 用 `nesting` 计数跟踪未配对的 `[`，只有当 `nesting == 0` 时遇到 `]` 才报错。

#### 4.2.2 核心流程

`markup_expr` 是一个大的 `match p.current()`，按职责可分为几组：

```
match 当前 token:
  # —— 第 1 组：[ ] 嵌套（纯正文里当文本处理）
  LeftBracket            => nesting += 1; 当作 Text 吃掉
  RightBracket 且嵌套中  => nesting -= 1; 当作 Text 吃掉
  RightBracket（未配对）  => 报错 + 提示用 \] 转义

  # —— 第 2 组：直接吃掉的「叶子」表达式
  Shebang / Text / Linebreak / Escape / Shorthand / SmartQuote / Link / Label / Raw
                         => eat()

  # —— 第 3 组：模式切换入口
  Hash                   => embedded_code_expr  （切到 Code）
  Dollar                 => equation            （切到 Math）

  # —— 第 4 组：成对包裹
  Star                   => strong   (*...*)
  Underscore             => emph     (_..._)

  # —— 第 5 组：行首标记（需要 at_start）
  HeadingMarker if 行首  => heading
  ListMarker    if 行首  => list_item
  EnumMarker    if 行首  => enum_item
  TermMarker    if 行首  => term_item

  # —— 第 6 组：引用
  RefMarker              => reference

  # —— 第 7 组：兜底（行中的标记字符 / 冒号 → 当文本；其它 → 报错）
  HeadingMarker | ListMarker | EnumMarker | TermMarker | Colon => convert_and_eat(Text)
  其它                   => unexpected()
```

注意第 5 组与第 7 组是**同一个 token 的两种命运**：行首的 `=` 是标题标记，行中的 `=` 走第 7 组变成文本。这就是 Typst 用两道关卡（lexer 看后续是否空白、parser 看是否行首）共同判定「结构 vs 文本」的实现。

#### 4.2.3 源码精读

[`src/parser.rs:87-134`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L87-L134) —— `markup_expr` 的完整 `match`。第 88 行的 `increase_depth()` 是深度自检（防栈溢出）。

第 119–122 行是行首标记分支，注意每个都带 `if at_start` 守卫：

[`src/parser.rs:119-124`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L119-L124) —— 标题/列表/枚举/术语/引用/方程的分发。前四者要求 `at_start`。

第 126–130 行是「行中标记字符与冒号」的兜底——转换成 `Text` 吃掉：

[`src/parser.rs:126-130`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L126-L130) —— 行中的 `=`/`-`/`+`/`/`/`:` 不满足行首条件时，用 `convert_and_eat(Text)` 改写成文本 token 再吃掉。`convert_and_eat` 会先把 token 的 kind 原地改成 `Text` 再 eat（见 u4-l2）。

为了说明「行中标记为何会变成文本」，对照 lexer 侧：`=` 只有在后面紧跟空白或文本末尾时才被切成 `HeadingMarker`：

[`src/lexer.rs:512-518`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L512-L518) —— lexer 产生四个行首 marker。`space_or_end()` 判定后跟空白/注释/末尾。注意 lexer **不看是否行首**，只看后续字符；是否行首由 parser 的 `at_start` 把关。

[`src/lexer.rs:659-664`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L659-L664) —— `space_or_end` 的定义：遇到文本末尾、空白、`//`、`/*` 即返回 true。

因此 `a = b` 里的 `=`（后跟空格）会被 lexer 切成 `HeadingMarker`，但因 `at_start` 为 false，parser 走兜底分支转成文本——两道关卡缺一不可。

#### 4.2.4 代码实践

**实践目标**：验证同一字符 `=` 在行首与行中产出不同结构。

**操作步骤**：

```rust
// 示例代码
use typst_syntax::parse;

fn dump_kinds(title: &str, text: &str) {
    let root = parse(text);
    println!("=== {title} ===");
    for c in root.children() {
        print!("{:?} ", c.kind());
    }
    println!();
}

fn main() {
    dump_kinds("行首等号", "= 标题");
    dump_kinds("行中等号", "a = b");
}
```

**需要观察的现象**：「行首等号」里会出现 `Heading` 节点（其第一个子节点是 `HeadingMarker`）；「行中等号」里不会出现 `Heading`，`=` 会以 `Text` 形式出现。

**预期结果**：行中 `=` 走兜底分支变成 `Text`，证明 `at_start` 守卫起作用。精确的 trivia 节点「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Raw` 在 `markup_expr` 里只是一行 `p.eat()`，没有任何子树构造？

**参考答案**：因为 raw（反引号包裹的原始文本）在**词法阶段**就由 lexer 一次性组装成了完整的 CST 子树（包含 `RawDelim`/`RawLang`/`RawTrimmed` 等子 token，见 u3-l3）。parser 拿到的 `Raw` token 本身已是一棵完整的子树节点，所以直接 `eat()` 即可，无需再做结构解析。源码第 114 行的注释 `// Raw is handled entirely in the Lexer.` 明确说明了这一点。

**练习 2**：`markup_expr` 末尾的 `_ => p.unexpected()` 兜底会在什么情况下触发？给出一个例子。

**参考答案**：当当前 token 既不属于任何已知 markup 表达式、也不是可转文本的标记字符时触发。例如在 Markup 模式下出现一个本不该出现的 token（如某些仅在 Code 模式产生的 token 混入），会走 `unexpected()`：它先 `trim_errors()` 清理零长错误，再把 `balanced` 按需置位，最后吃掉该 token 并标记为错误节点，从而实现错误恢复并继续向前解析。

---

### 4.3 行首触发的结构：`heading` / `list_item` / `enum_item` / `term_item`

#### 4.3.1 概念说明

标题、列表、枚举、术语这四类构造有高度相似的结构：都以一个**行首标记 token** 开头，后面跟一段作为「正文」的 Markup。它们之间的主要差异在于：

- **换行模式不同**：标题在一行内结束（`AtNewline::Stop`）；列表/枚举/术语项的正文可以跨行，但要求后续行**缩进不少于标记所在列**（`AtNewline::RequireColumn(column)`），否则该项结束。
- **停止集合不同**：标题体遇到 `Label`（`<label>`）就停；术语项需要在标记与冒号之间解析 term，再在冒号后解析 description。

它们都用 `markup(p, ...)` 来解析「标记之后的正文」，最后 `wrap` 成对应节点。

#### 4.3.2 核心流程

四个函数的共同骨架：

```
fn 某构造(p):
    p.with_nl_mode(换行模式, |p| {
        let m = p.marker();          # 记位置戳
        p.assert(标记token);          # 吃掉标记（= / - / + / /）
        markup(p, ..., 停止集合);     # 解析标记后的正文
        p.wrap(m, 对应节点kind);
    })
```

各函数的具体配置：

| 函数 | 标记 token | 换行模式 | 正文 at_start | 停止集合（额外） | 节点 kind |
| --- | --- | --- | --- | --- | --- |
| `heading` | `HeadingMarker` | `Stop`（行内结束） | false | `Label` | `Heading` |
| `list_item` | `ListMarker` | `RequireColumn(列)` | true | — | `ListItem` |
| `enum_item` | `EnumMarker` | `RequireColumn(列)` | true | — | `EnumItem` |
| `term_item` | `TermMarker` | `RequireColumn(列)` | （分两段） | `Colon` | `TermItem` |

`RequireColumn(col)` 的含义：换行后若新行起始列号 `<= col`（即缩进更少或相同），就停止；只有缩进更深（列号更大）的续行才会被并入当前项。这正好对应 Typst 里「列表项的续行必须比标记缩进更深」的书写规则。

#### 4.3.3 源码精读

[`src/parser.rs:171-178`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L171-L178) —— `heading`：用 `AtNewline::Stop` 让标题体在第一个换行处结束；正文 `markup(p, false, false, ...)` 的停止集合含 `Label`（标题末尾可贴标签）。

[`src/parser.rs:181-188`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L181-L188) —— `list_item`：换行模式是 `AtNewline::RequireColumn(p.current_column())`，`current_column()` 取的是标记所在列。注意正文用 `markup(p, true, false, ...)`，`at_start=true` 意味着列表项正文本身也按行首处理，所以项内还能再嵌套标记。`enum_item` 结构与之完全对称：

[`src/parser.rs:191-198`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L191-L198) —— `enum_item`：除标记 token 与最终 kind 外，与 `list_item` 同构。注意枚举项可由 `+` 或 `1.` 触发——后者由 lexer 的 `numbering` 产生 `EnumMarker`：

[`src/lexer.rs:565-570`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L565-L570) —— `1.` 形式的枚举标记：吃掉数字后若紧跟 `.` 且 `space_or_end()`，返回 `EnumMarker`。

[`src/parser.rs:201-212`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L201-L212) —— `term_item` 最复杂：它分两段。先用 `AtNewline::Stop` 解析 term 部分（`/ Term`，遇到 `Colon` 停），再 `p.expect(Colon)` 吃掉冒号，最后用 `RequireColumn` 解析 description 正文。最终整体 `wrap(TermItem)`。这种「外层 `RequireColumn` + 内层 `Stop`」的嵌套换行模式，靠 `with_nl_mode` 把调用栈当成模式栈来实现。

为了理解 `current_column` 的来源，看 parser 如何从换行信息里取列号：

[`src/parser.rs:1689-1694`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1689-L1694) —— `current_column`：优先用换行模式缓存的列号，否则回退到从 lexer 实时计算。这个列号就是 `RequireColumn` 比较的基准。

#### 4.3.4 代码实践

**实践目标**：验证列表项的「续行缩进」规则如何体现为 `RequireColumn` 的停止行为。

**操作步骤**：

```rust
// 示例代码
use typst_syntax::parse;

fn main() {
    // 第二行缩进比 "-" 更深，应被并入列表项正文
    let root = parse("- 第一行\n  续行");
    for c in root.children() {
        println!("{:?}", c.kind());
        if let Some(item) = c.children().next() {
            println!("  第一个子节点: {:?}", item.kind()); // 期望 ListMarker
        }
    }
}
```

**需要观察的现象**：根的直接子节点里只有一个 `ListItem`（而不是 `ListItem` + 独立的 `Text("续行")`），因为续行缩进更深被并入项内。试着把续行改成顶格（无缩进），观察 `续行` 是否被拆到顶层。

**预期结果**：顶格续行时 `RequireColumn` 会叫停，续行成为顶层 Markup 的独立表达式。具体 trivia 布局「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `heading` 用 `markup(p, false, false, ...)`，而 `list_item` 用 `markup(p, true, false, ...)`？第二个参数 `true/false` 的含义是什么？

**参考答案**：第二个参数是传给 `markup_exprs` 的初始 `at_start`。标题体是单行的，其正文一般不会再嵌套标题/列表等行首标记，且用 `AtNewline::Stop` 在换行处立即结束，所以初始 `at_start=false`。列表项正文允许跨行且本身就像一个新的正文区，项内可能继续出现行首标记（如嵌套列表），所以传 `at_start=true`，让项内首行也能触发标记。

**练习 2**：`term_item` 为什么要嵌套两层 `with_nl_mode`？

**参考答案**：term 部分（`/ Term`）必须在遇到冒号或换行时停止，所以内层用 `AtNewline::Stop`；而整个术语项（含 description）允许跨行续行，所以外层用 `AtNewline::RequireColumn`。两层模式分别控制 term 段与整个 term_item 的换行边界，靠 `with_nl_mode` 的栈式切换共存于同一次解析。

---

### 4.4 强调与着重：`strong` / `emph`

#### 4.4.1 概念说明

`*Strong*`（加粗）与 `_Emphasized_`（着重）是 markup 里典型的**成对包裹**构造：以一个定界符（`*` 或 `_`）开头，中间是一段正文，再以同种定界符结尾。它们的解析逻辑几乎完全对称，是练习 marker + wrap + 换行模式的最佳样本。

关键点：

- 换行模式用 `AtNewline::StopParBreak`：单个换行不会结束 strong/emph（允许跨行加粗），只有「段落间断」（空行，即连续 ≥2 换行产生的 `Parbreak`）才会停止。
- `wrap_trivia=true`：定界符与正文之间的 trivia 要被圈进 strong/emph 子树，保证无损还原。
- 有一个友好的警告：若两个星号之间没有内容（`**`），产生「no text within stars」警告并给出提示。

#### 4.4.2 核心流程

```
fn strong(p):
    p.with_nl_mode(StopParBreak, |p| {
        let m = p.marker();
        p.assert(Star);                                   # 吃掉开头 *
        markup(p, at_start=false, wrap_trivia=true,        # 解析中间正文
               停止集合={Star, RightBracket, End});
        had_closing = p.expect_closing_delimiter(m, Star); # 吃掉结尾 *（缺失则把开头标记为错误）
        p.wrap(m, Strong);
        if 有结尾星号 且 子树长度==2:                        # 只有首尾两个 *、中间无内容
            警告 "no text within stars" + 提示
    })
```

`expect_closing_delimiter` 的返回值告诉我们结尾定界符是否存在；若缺失，它会把开头的 `*` 节点转换成错误节点（`unclosed delimiter`），从而在 CST 里留下可定位的诊断信息。

#### 4.4.3 源码精读

[`src/parser.rs:137-151`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L137-L151) —— `strong` 的完整实现。注意 `wrap_trivia=true`（第 141 行经由 `markup` 的 `before_trivia()` 起效）、`StopParBreak` 换行模式，以及第 144–149 行对 `**` 空内容的警告。

[`src/parser.rs:154-168`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L154-L168) —— `emph` 与 `strong` 逐行同构，只是把 `Star`/`Strong` 换成 `Underscore`/`Emph`。

`expect_closing_delimiter` 的实现，它处理「定界符未闭合」的错误恢复：

[`src/parser.rs:2000-2006`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L2000-L2006) —— 若吃不到结尾定界符，就把位于 `open` 标记处的开头的 `*`/`_` 节点 `convert_to_error("unclosed delimiter")`，并返回 `false`。

对应的 AST 视图：`Strong` 节点把其正文暴露为一个 `Markup`：

[`src/ast.rs:670-680`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L670-L680) —— `node! { struct Strong }` 声明节点，`body()` 用 `cast_first()` 取出第一个能 cast 成 `Markup` 的子节点（即跳过开头的 `*` 定界符）。

#### 4.4.4 代码实践

**实践目标**：观察 strong 节点的三段式结构（开头 `*`、正文 Markup、结尾 `*`），以及未闭合时的错误恢复。

**操作步骤**：

```rust
// 示例代码
use typst_syntax::parse;

fn main() {
    let root = parse("*hi*");
    let strong = root.children().next().unwrap();
    println!("kind: {:?}", strong.kind()); // 期望 Strong
    for c in strong.children() {
        println!("  {:?}", c.kind());      // 期望: Star, Markup, Star
    }

    // 未闭合：观察开头 * 被转成错误节点
    let root2 = parse("*未闭合");
    for c in root2.descendants() {
        if c.kind().is_error() {
            println!("error: {:?}", c.text());
        }
    }
}
```

**需要观察的现象**：闭合的 `*hi*` 产出 `Strong`，其子节点为 `Star` + `Markup`（含 `Text "hi"`）+ `Star`；未闭合的 `*未闭合` 中，开头的 `*` 被转换成错误节点（消息含 `unclosed delimiter`）。

**预期结果**：成对包裹的三段式结构清晰可见，未闭合时靠 `expect_closing_delimiter` 做错误恢复。trivia 细节「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 strong/emph 用 `AtNewline::StopParBreak` 而不是 `AtNewline::Stop`？

**参考答案**：`Stop` 会在任意单个换行处停止，那样 `*第一行\n第二行*` 里的换行就会提前结束 strong，导致结尾 `*` 找不到。`StopParBreak` 只在「段落间断」（空行 / `Parbreak`）处停止，单个换行被允许穿过，所以加粗可以跨行；只有遇到空行才认为加粗确实该结束了。

**练习 2**：第 144 行的判断 `p[m].len() == 2` 中，为什么是 2 而不是 0？

**参考答案**：`len()` 返回节点覆盖的字节长度。`**`（两个星号、中间无内容）的 `Strong` 节点覆盖长度正好是两个 `*` 字符 = 2 字节。若长度为 2 且有闭合星号，说明中间没有任何正文，于是触发「no text within stars」警告。长度为 0 的情况在这里不会出现，因为至少含开头 `*`。

---

### 4.5 引用、方程与模式切换：`reference` / `equation` / `content_block` / 嵌入式代码

#### 4.5.1 概念说明

Markup 并不是封闭的——它有四个「出口」可以切换到其它语法模式：

- `#` → 切到 **Code** 模式（嵌入式代码表达式）；
- `$` → 切到 **Math** 模式（方程）；
- `[ ]` → 切到 **Markup** 模式（内容块，但注意它通常从 **Code** 上下文进入，如函数调用 `#rect[...]` 或 `@ref[...]`）；
- `@` → 产生**引用** `RefMarker`，可选地跟一个内容块作为补充内容 `@target[...]`。

模式切换的底层机制是 `enter_modes`：它临时改写 lexer 的 `SyntaxMode`（让后续 token 按新模式的规则切分），用 `with_nl_mode` 套上新的换行模式，解析完子结构后再恢复原模式——这相当于把「调用栈」当成「模式栈」。

#### 4.5.2 核心流程

**引用 `reference`**：

```
fn reference(p):
    let m = p.marker();
    p.assert(RefMarker);                      # 吃掉 @target
    if 紧跟着 LeftBracket（无 trivia）:
        content_block(p);                     # 解析 @target[...] 的补充内容
    p.wrap(m, Ref);
```

**方程 `equation`**（切到 Math）：

```
fn equation(p):
    let m = p.marker();
    p.enter_modes(Math, AtNewline::Continue, |p| {
        p.assert(Dollar);                     # 吃掉开头 $
        math(p, 停止集合={Dollar, End});       # 解析公式内容
        p.expect_closing_delimiter(m, Dollar); # 吃掉结尾 $（缺失则报错）
    });
    p.wrap(m, Equation);
```

**嵌入式代码 `embedded_code_expr`**（`#` 切到 Code）：吃掉 `#`，要求其后不能有 trivia（`#` 必须紧贴表达式），然后按 Code 规则解析一个表达式。

**内容块 `content_block`**（`[` 切到 Markup）：吃掉 `[`，用 `markup` 解析其中的正文，再期望结尾 `]`。

#### 4.5.3 源码精读

[`src/parser.rs:215-222`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L215-L222) —— `reference`：吃掉 `RefMarker` 后，用 `directly_at(LeftBracket)` 判断是否**紧贴**一个 `[`（不允许中间有空白），若是则解析一个内容块作为引用的补充内容，最后 `wrap(Ref)`。`RefMarker` 由 lexer 的 `ref_marker` 产生：

[`src/lexer.rs:576-585`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L576-L585) —— lexer 的 `ref_marker`：吃掉合法的标签字符，并剥掉尾部疑似正文的 `.`/`:`。

[`src/parser.rs:225-233`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L225-L233) —— `equation`：用 `enter_modes(SyntaxMode::Math, AtNewline::Continue, ...)` 切到 Math 模式解析公式。换行模式 `Continue` 表示公式内换行不叫停（数学表达式有自己的停止集合）。

模式切换的核心原语 `enter_modes`：

[`src/parser.rs:1810-1825`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1810-L1825) —— `enter_modes`：保存旧 mode → 设新 mode → 用 `with_nl_mode` 执行子解析 → 退出时若 mode 变了，回退 lexer 游标并重新 lex 当前 token（因为同一段文本在不同模式下会被切成不同 token，切换回来必须重 lex）。注释称其「把调用栈当模式栈」。

`#` 切到 Code 的入口：

[`src/parser.rs:579-598`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L579-L598) —— `embedded_code_expr`：`enter_modes(Code, Stop, ...)`，吃掉 `#`，第 582 行检查 `had_trivia() || end()`——若 `#` 后紧跟空白或文本末尾，则报 `expected expression`（因为 `#` 必须紧贴一个代码表达式）。随后用 `code_expr_prec` 解析（Code 表达式解析详见 u4-l4）。

`[` 切到 Markup 的内容块（注意它从 Code 侧或 reference 进入，不是 markup 自身）：

[`src/parser.rs:778-786`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L778-L786) —— `content_block`：`enter_modes(Markup, Continue, ...)`，吃掉 `[`，用 `markup(p, true, true, ...)` 解析正文（`wrap_trivia=true` 无损圈入），再期望 `]`。

> 小结模式切换：在 **Markup** 里，`#` 出 Code、`$` 出 Math、`@` 出引用（可带内容块）；而 `[ ]` 内容块本身一般是从 **Code**（函数调用）或 reference 进入的——这解释了为什么 `markup_expr` 里遇到裸 `[` 是当文本处理（4.2 第 1 组），而不是开内容块。

#### 4.5.4 代码实践

**实践目标**：观察 `#`、`$`、`@` 如何在 markup 中产生跨模式节点。

**操作步骤**：

```rust
// 示例代码
use typst_syntax::parse;

fn main() {
    let root = parse("#let x = 1\n平方是 $x^2$，见 @intro");
    for node in root.descendants() {
        match node.kind() {
            k @ (typst_syntax::SyntaxKind::LetBinding
               | typst_syntax::SyntaxKind::Equation
               | typst_syntax::SyntaxKind::Ref) => {
                println!("找到 {:?}，文本片段: {:?}", k, node.text());
            }
            _ => {}
        }
    }
}
```

**需要观察的现象**：能分别找到 `LetBinding`（由 `#` 切入 Code 解析）、`Equation`（由 `$` 切入 Math 解析）、`Ref`（由 `@` 产生）三类节点，且它们都嵌在顶层 `Markup` 之中。

**预期结果**：三种模式切换入口各产出对应节点，证明 markup 通过 `#`/`$`/`@` 与其它模式互通。`Equation` 内部的 Math 子结构（如 `MathAttach`）「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：`enter_modes` 在退出时为什么要 `self.lexer.jump(self.token.prev_end)` 并重新 `lex` 当前 token？

**参考答案**：同一段文本在不同 `SyntaxMode` 下会被切成不同 token。例如 `]` 在 Markup 模式是 `RightBracket`，但在 Code 模式可能是别的含义。子模式解析完退回父模式时，当前持有的 token 是用子模式的规则 lex 出来的，可能不再适用于父模式。因此 `enter_modes` 把 lexer 游标回退到当前 token 的起点，用父模式重新 lex，得到符合父模式规则的 token。源码第 1819–1824 行处理了这一「重 lex」。

**练习 2**：`reference` 用 `directly_at(LeftBracket)` 而不是 `at(LeftBracket)`，二者区别是什么？为什么引用要用前者？

**参考答案**：`at(k)` 只判断当前 token 的 kind 是否为 `k`；`directly_at(k)` 还要求当前 token **没有前置 trivia**（`!had_trivia()`），即 `@target` 与 `[` 之间不能有空格。引用要求 `@intro[内容]` 紧贴书写；若写成 `@intro [...]`（中间有空格），`[` 就不属于引用的补充内容，而会被当作后续独立结构。所以必须用 `directly_at` 保证紧邻关系。

---

## 5. 综合实践

把本讲的所有知识点串起来：自己用 `parse` 解析一段包含多种 markup 元素的文本，画出它的 CST 形状，并解释每个结构是如何被 `markup_exprs` 循环 + `markup_expr` 分发出来的。

**任务文本**：

```
= Typst 入门
本节介绍 *加粗* 与 _着重_。

- 列表项一
  列表项一的续行
+ 枚举项

公式 $a^2$ 见 @sec-intro[label]。
```

**要求**：

1. 用 `parse(text)` 得到根 `Markup`，用 `descendants()` 打印所有节点的 `kind()` 与缩进层级（可按深度缩进打印），手绘出 CST 树形图。
2. 在图中标注：哪些节点由行首标记触发（`Heading`/`ListItem`/`EnumItem`），哪些由成对包裹触发（`Strong`/`Emph`），哪些由模式切换触发（`Equation`/`Ref`，以及 `#` 相关的 Code 节点）。
3. 解释 `- 列表项一\n  列表项一的续行` 为何续行被并入同一个 `ListItem`（联系 `RequireColumn`），并预测：如果把续行改成顶格（无缩进），CST 会怎样变化。
4. 找到 `@sec-intro[label]` 中的 `Ref` 节点，确认它含一个 `RefMarker` 子节点和一个 `ContentBlock` 子节点（因为 `[` 紧贴 `@sec-intro`）。

**预期收获**：你能完整复述「文本 → lexer 切 token（含行首 marker）→ `markup_exprs` 循环 → `markup_expr` 按 `at_start` 分发 → 各构造函数用 marker+wrap+换行模式建子树 → 必要时 `enter_modes` 切换模式」这条链路。trivia（Space/换行）节点的精确位置「待本地验证」。

## 6. 本讲小结

- Markup 解析的骨架是 `markup_exprs` 的主循环：不断调用 `markup_expr` 解析下一个表达式，并用 `at_start = p.had_newline()` 维护「当前是否在行首」这一关键状态。
- `markup_expr` 是分发枢纽：按当前 token 的 `SyntaxKind` 选择分支；其中标题/列表/枚举/术语四类标记带 `if at_start` 守卫，行中则兜底转成普通 `Text`。
- 行首标记是「两道关卡」：lexer 看 `=`/`-`/`+`/`/` 后续是否空白来产生 `*Marker` token，parser 看 `at_start` 是否为真来决定当结构还是当文本，两者共同判定。
- `heading`/`list_item`/`enum_item`/`term_item` 结构相似，差异在换行模式（`Stop` vs `RequireColumn(列)`）与停止集合；`term_item` 用嵌套 `with_nl_mode` 分别控制 term 段与整个项。
- `strong`/`emph` 是成对包裹的典型：用 `StopParBreak` 允许跨行、`wrap_trivia=true` 无损圈入 trivia、`expect_closing_delimiter` 处理未闭合错误。
- 模式切换由 `enter_modes` 完成：`#` 切 Code（`embedded_code_expr`）、`$` 切 Math（`equation`）、`[` 切 Markup（`content_block`，多从 Code/reference 进入）、`@` 产生引用（`reference`，可带紧贴的内容块）。

## 7. 下一步学习建议

- **下一讲 u4-l4「Code 与 Math 解析」**：本讲只打开了 `#`/`$` 两个模式切换入口，但 Code 表达式的优先级爬升（`code_expr_prec`）、let/set/show/if 等语句、以及 Math 的算符优先级与定界处理都还没展开，这正是下一讲的主题。
- **延伸阅读**：对照 `src/parser.rs` 中 `code_expr_prec`（L606 起）与 `math_expr_prec`（L268 起），体会它们与 `markup_expr` 在「分发 + 优先级」上的同与异。
- **回顾 CST 数据结构**：学完 u4 全部解析后，建议进入 U5（`src/node.rs`）看 `SyntaxNode` 如何承载本讲产出的这些节点，以及 `LinkedNode` 如何带父指针遍历它们。

# 新行处理与错误恢复

## 1. 本讲目标

本讲承接 u4-l4（Code 与 Math 解析），把 Parser 里两块「最容易看走眼、却最见功底」的内容讲透：**换行（newline）如何参与语法判定**，以及**当输入不符合任何语法规则时，解析器如何优雅地恢复并把错误塞回 CST**。

在前几讲里，`AtNewline`、`with_nl_mode`、`lex` 伪造 `End` 这些名词已经反复出现过：u4-l2 介绍了它们的「是什么」，u4-l3 用它们解释了 markup 的标题/列表/强调，u4-l4 用 `ContextualContinue` 解释了 code 语句的切分。本讲不再重复「是什么」，而是回答两个更深入的问题：

1. **机制层**：换行模式是「栈」式管理的——内层解析可能改变叫停规则，退出时还要把刚才伪造的 `End` 重新评估一次。这套「入栈 / 出栈 / 出栈重算」到底是怎么协同的？
2. **错误恢复层**：Typst 的解析器**永远不会 panic**，遇到任何垃圾输入都会产出 `Error` 节点并继续往下吃。`unexpected` / `expected` / `expected_at` / `hint` / `trim_errors` 这五个函数分别扮演什么角色？为什么还要专门「清理零长错误噪音」？

学完后你应当能够：

1. 画出 `AtNewline` 五个变体（`Continue` / `Stop` / `ContextualContinue` / `StopParBreak` / `RequireColumn`）在 `stop_at` 里的判定表，并说清 `RequireColumn` 的「续行缩进」语义与 `column` 为何只在 Markup 模式下才有值；
2. 用「入栈改 `nl_mode` → `lex` 伪造 `End` → 出栈恢复 `nl_mode` → 用真实 kind 重算是否该伪造 `End`」四步，解释为何内层 `heading`（`Stop`）结束后，外层 markup 不会被它「误伤」而提前终止；
3. 区分 `unexpected`（把这个 token 当成意外、吃掉并标错）、`expected`（本该出现某物、可能插一个零长 Error 或吃掉错误 token）、`expected_at`（在某位置插零长 Error）、`hint`（给「紧邻的尾部错误」追加一条用户可见建议）四种动作；
4. 解释 `trim_errors` 为什么只删「尾部、零长」的 Error 节点，以及它和「保持 token 在正确词法模式」、`balanced` 标志之间的连带关系。

本讲只精读一个文件 `src/parser.rs`，但会触到 `src/node.rs` 里几个 `pub(super)` 的节点级辅助方法（`convert_to_error` / `unexpected` / `hint`）以及 `src/lexer.rs` 的 `newline()` / `column()`，把因果链补全。

## 2. 前置知识

进入本讲前，请确认你已掌握（均在前置讲义中讲过）：

- **单 token 前瞻 + trivia**：`token` 是「当前未入栈的 token」，附带 `n_trivia`（它前面落入 `nodes` 的 trivia 数量）与 `newline`（trivia 里是否含换行及其列号）。`marker()` 指向 `nodes.len()`（不含 trivia），`before_trivia()` 指向 `nodes.len() - n_trivia`（含 trivia 起点）。`eat()` 把 `token` 推入 `nodes` 再 lex 出下一个（u4-l2）。
- **`AtNewline` 换行模式基础**：parser 用一个 `nl_mode` 字段规定「换行是否叫停」，`lex` 在 trivia 含换行时按 `stop_at` 决定是否把当前 token 的 kind 伪造成 `End` 来停止上层循环，但**真实 kind 仍存在 `token.node` 里**（u4-l2、u4-l3、u4-l4）。
- **三模式与 `enter_modes`**：`#` 切 Code、`$` 切 Math、`[` 切 Markup；`enter_modes(mode, stop, f)` 同时改「词法模式」与「换行模式」，退出时会回退 lexer 游标重 lex 最后一个 token（u4-l3、u4-l4）。
- **`SyntaxKind::Error` 与 CST 无损**：词法错误与语法错误都以 `Error` 节点的形式进 CST，CST 仍是「无损、可还原原文」的真相来源（u3-l1、u5 预告）。

一句话铺垫：**换行在 Typst 里既是「可跳过的空白」（trivia），又是「语句分隔符」与「结构边界」（标题单行、列表续行缩进、段落断行）——同一个 `\n`，在不同上下文里意义完全不同，而 `AtNewline` 就是这层「上下文」的开关。**

## 3. 本讲源码地图

| 文件 | 本讲涉及内容 |
| --- | --- |
| `src/parser.rs` | `Newline` 结构、`AtNewline` 枚举与 `stop_at`；`lex`（trivia 扫描、换行检测、伪造 `End`）；`with_nl_mode`（入栈/出栈/出栈重算）；真实用法：`markup_exprs` / `heading` / `list_item` / `term_item` / `strong` / `emph` / `equation` / `code_exprs` / `embedded_code_expr`；错误恢复：`expect` / `expected` / `expected_at` / `hint` / `after_error` / `unexpected` / `trim_errors` / `eat_and_get` / `balanced` 字段；深度兜底 `depth_check_error` |
| `src/node.rs` | 节点级辅助：`SyntaxNode::error`、`convert_to_error`、`expected`、`unexpected`、`hint`、`is_empty` |
| `src/lexer.rs` | `Lexer::newline()`（上个 token 是否含换行）、`Lexer::column(idx)`（某字节偏移距最近换行的字符数） |

> 提醒：以下所有永久链接的 HEAD 均为 `32fd4cc3861e0ab99f4c42ca6bea281482ba9f51`。

## 4. 核心概念与源码讲解

### 4.1 换行为什么能控制解析：`AtNewline` 五变体与 `stop_at` 判定

#### 4.1.1 概念说明

先建立直觉。考虑下面三段 Typst，注意 `\n`（换行）在其中扮演的截然不同的角色：

```typst
// (A) Code：换行 = 语句分隔符
#let x = 1
#let y = 2

// (B) Markup 标题：换行 = 标题这一行到此结束
= Introduction
body text continues here

// (C) Markup 列表：换行后「缩进更深」= 续行，「不缩进」= 列表项结束
- first line
  second line
- another item
```

- 在 (A) 里，`#let x = 1` 后的换行把两条 `let` 切成两条独立语句——**换行就是分号**。
- 在 (B) 里，`=` 后的换行让标题 `Introduction` 在行末收口，下一行 `body text ...` 回到外层正文——换行是「**单行结构的终止符**」。
- 在 (C) 里，列表项 `- first line` 的换行**不一定**终止该项：如果下一行缩进更深（`  second line`），它属于同一个列表项；如果缩进相同或更浅（`- another item`），则该项结束——换行是「**带条件的结构边界**」，条件就是「列号」。

同一个 `\n`，三种语义。Typst 的做法不是给 lexer 加一堆状态，而是让 **parser 在进入某个子解析任务时，临时声明一个「换行模式」**，告诉底层 lex：「接下来遇到换行，按这个规则决定要不要假装到了结尾」。这套规则就是 `AtNewline` 枚举，仲裁函数就是 `stop_at`。

关键认知：**`AtNewline` 回答的唯一问题是「当前这个（含换行 trivia 的）token，要不要被当作『到此为止』的 `End`？」** 它不直接决定解析走哪条分支，而是通过「伪造 `End`」间接叫停上层循环。

#### 4.1.2 核心流程

`stop_at` 接收两份信息：一份是换行 trivia 的摘要 `Newline { column, parbreak }`，一份是「换行之后那个 token 的 `kind`」（因为 `ContextualContinue` 要看它是不是 `else`/`.`）。它返回一个布尔：`true` 表示「叫停（伪造 `End`）」，`false` 表示「放行」。

判定表如下：

| 变体 | `stop_at` 返回 `true`（叫停）的条件 | 典型用途 |
| --- | --- | --- |
| `Continue` | 永不（直接 `false`） | 顶层 markup、math 内部、深度兜底 |
| `Stop` | 只要 trivia 含换行 | 标题（单行）、term 的 term 部分、`#` 嵌入代码 |
| `ContextualContinue` | 含换行**且**下一个 token **不是** `Else` 也不是 `Dot` | code 语句切分（让 `else`/`.` 跨行续接） |
| `StopParBreak` | 含换行**且**其中含**段落断行**（`Parbreak`） | `*strong*` / `_emph_`（可跨普通换行，遇空行止） |
| `RequireColumn(min_col)` | 含换行**且**下一个 token 的列号 `column <= min_col` | 列表/枚举/术语项（续行需更深缩进） |

两个要点需要单独拎出来：

1. **`RequireColumn` 的方向容易记反**。它「叫停」的条件是 `column <= min_col`，也就是说**续行只有在列号严格大于 `min_col`（缩进更深）时才被放行**；列号相等或更浅就叫停。这符合 (C) 的直觉：列表项 `- ` 在列号 `c`，那么列号 `> c` 的后续行属于该项，列号 `<= c` 的行结束该项。
2. **`column` 只在 Markup 模式下才有值**（见 4.2.3 的 `lex`），在 Code/Math 里 `column` 是 `None`。`RequireColumn` 的 `stop_at` 对 `None` 的处理是「放行」（不叫停），因为「不带列号的换行」通常出现在模式切换边界，没法用列号判定，索性放行交给外层。

此外还有两个「边界退化」性质，注释里写得很直白：`RequireColumn(0)` 等价于 `Continue`（任何列号都 `> 0` 不成立？不——列号 `0` 时 `0 <= 0` 叫停，列号 `> 0` 放行，所以实际上 `RequireColumn(0)` 让「顶格的换行」叫停、缩进的换行放行，近似但不完全等于 `Continue`；注释说的是它「行为上接近 Continue」），`RequireColumn(usize::MAX)` 等价于 `Stop`（任何列号都 `<= MAX`，恒叫停）。

#### 4.1.3 源码精读

先看携带换行信息的载体 `Newline`（[parser.rs:L1547-L1554](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1547-L1554)）：`column` 是「换行之后那个 token 起始处距最近换行的字符数」，`parbreak` 标记 trivia 里是否含段落断行（连续空行）。

然后是五个变体本身（[parser.rs:L1556-L1571](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1556-L1571)），每个变体的文档注释一句话点明用途，注意 `RequireColumn(usize)` 是唯一带数据的变体。

仲裁逻辑 `stop_at`（[parser.rs:L1575-L1593](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1575-L1593)）就是把上表翻译成代码。值得逐行读：

- `Continue => false`：无条件放行。
- `Stop => true`：无条件叫停。
- `ContextualContinue` 分支用了 `#[expect(clippy::match_like_matches_macro)]`，因为它就是个「只对 `Else | Dot` 返回 `false`、其余 `true`」的特判——这正是「`else`/`.` 可以跨行续接」的源头。
- `StopParBreak => parbreak`：只有段落断行才叫停，普通单换行放行（让 `*strong*` 能跨行）。
- `RequireColumn(min_col)`：`column.is_some_and(|column| column <= min_col)`，把上面「方向」那点落实；注释解释了 `None` 时放行的边界场景。

`stop_at` 的两个调用点都在 parser 内部：一个在 `lex` 里（决定要不要伪造 `End`，见 4.2.3），一个在 `with_nl_mode` 出栈时（重新评估，见 4.2.3）。换句话说，**`stop_at` 是换行机制的「唯一裁判」，两处调用对应「进 token 时初判」和「退栈时复审」**。

#### 4.1.4 代码实践

**实践目标**：用「源码阅读 + 手工推演」吃透 `stop_at` 的判定方向，尤其是 `RequireColumn`。

**操作步骤**：

1. 打开 [parser.rs:L1575-L1593](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1575-L1593)，对照上面的判定表逐变体核对。
2. 对下面三段输入，**手工**推断「换行之后那个 token 会被 `stop_at` 判成叫停还是放行」（假设当时 `nl_mode` 如括号所注）：

   ```text
   输入1: "= T\nbody"        nl_mode = Stop            （标题内部）
   输入2: "- a\n  b\n- c"    nl_mode = RequireColumn(0)（列表项，标记在列0）
   输入3: "#if x [a]\nelse [b]"  nl_mode = ContextualContinue
   ```

3. 对输入2，分别算出第一个 `\n`（在 `a` 之后）与第二个 `\n`（在 `  b` 之后）处，下一行 token 的列号，再代入 `column <= 0` 判断。

**需要观察的现象**：

- 输入1：`body` 前的换行在 `Stop` 下恒叫停 → 标题 `T` 收口。
- 输入2：`  b` 列号 2，`2 <= 0` 为假 → 放行，`b` 续入列表项；`- c` 列号 0，`0 <= 0` 为真 → 叫停，列表项 `- a\n  b` 收口。
- 输入3：换行后是 `else`，`ContextualContinue` 对 `Else` 返回 `false` → 放行，`else` 被外层 `conditional` 抓住续接。

**预期结果**：你应当确信「`RequireColumn` 续行需更深缩进」「`ContextualContinue` 只放行 `else`/`.`」两条结论。

> 待本地验证：若你想用程序确认，可参考 4.2.4 的实践，用 `Source::detached` 解析后遍历 CST，看 `ListItem` 是否确实吞掉了 `  b`。

#### 4.1.5 小练习与答案

**练习 1**：`RequireColumn(usize::MAX)` 为什么等价于 `Stop`？
**答**：任何真实列号 `column` 都满足 `column <= usize::MAX`，所以 `is_some_and(...)` 只要 `column` 非 `None` 就恒为真（叫停），与 `Stop` 行为一致；而 `column` 为 `None` 的边界场景二者也都不影响顶层结构。

**练习 2**：为什么 `StopParBreak` 用来包 `*strong*`，而不是直接用 `Stop`？
**答**：因为 Typst 允许粗体跨**普通单换行**（如 `*line one\nline two*` 仍是同一个粗体），只有遇到**空行（段落断行）**才认定粗体没闭合。`StopParBreak` 恰好「只对 `parbreak` 叫停」，正是所需；用 `Stop` 会在第一个单换行就切断粗体。

---

### 4.2 换行模式的栈式管理：`lex` 伪造 `End` 与 `with_nl_mode` 出栈重算

#### 4.2.1 概念说明

光有 `stop_at` 这个裁判还不够，还得有「执行判决」的机制。Typst 用了一个非常巧妙、但初读容易困惑的设计：**它不是在 parser 主循环里到处写 `if 遇到换行 { break }`，而是让词法层 `lex` 在产出 token 时，直接把它的 `kind` 伪造成 `End`**。这样上层那些 `while !p.at_set(stop_set)` 循环天然就会停——因为它们本就是「见到 `End`（或 stop_set 里的 token）就退出」。

这个设计带来两个直接好处：

1. **上层循环零特殊判断**：`markup_exprs`、`code_exprs`、`math_exprs` 这些主循环只认 stop_set 与 `End`，根本不用关心「换行」这回事，换行被词法层吸收掉了。
2. **换行规则可「临时切换」**：因为规则只存在 `nl_mode` 一个字段里，进入子任务时换一下、退出时换回来即可——这就是「栈式管理」。注释里把 `with_nl_mode` 描述为「effectively repurposes the call stack as a stack of modes」（把调用栈当成模式栈来用）。

但伪造 `End` 有一个副作用必须处理：**伪造的 `End` 是「假的」**。真实 kind 还存在 `token.node` 里没动。当内层任务结束、控制权回到外层时，外层可能用的是**另一套**换行规则——刚才内层判定的「该不该叫停」对外层不一定成立。于是 `with_nl_mode` 在退出时还要做一次「复审」：用外层的 `nl_mode` 重新跑一遍 `stop_at`，决定是保留伪造的 `End`、还是把真实 kind 还原回去。

这就是本节的两个主角：`lex` 负责「进 token 时按当前 `nl_mode` 初判并伪造」，`with_nl_mode` 负责「出栈时按恢复后的 `nl_mode` 复审」。两者都用同一个 `stop_at`，但时机不同。

#### 4.2.2 核心流程

一次带换行的 token 产出（`lex` 内部）：

```
prev_end = 游标
循环：
  从 lexer 取一个 (kind, node)
  若 kind.is_trivia():
      累计是否含换行(had_newline)、是否含 parbreak
      n_trivia += 1
      把 node 推入 nodes          ← trivia 立即入栈
      继续循环
  否则：跳出循环                  ← 得到真正的 token
若 had_newline:
  column = 仅当 Markup 模式才算 lexer.column(start)，否则 None
  newline = Newline { column, parbreak }
  若 nl_mode.stop_at(newline, kind) == true:
      kind = End                  ← 伪造！但 token.node 不变
  记录 newline 到 token
返回 Token { kind(可能被伪造), node(真实), n_trivia, newline, ... }
```

`with_nl_mode` 的入栈/出栈：

```
with_nl_mode(mode, func):
  previous = self.nl_mode
  self.nl_mode = mode             ← 入栈：切到内层规则
  func(self)                      ← 跑子解析（期间 lex 用 mode 规则伪造 End）
  self.nl_mode = previous         ← 出栈：恢复外层规则
  若 当前 token 还带着换行(newline is Some) 且 mode != previous:
      actual_kind = token.node.kind()   ← 取回真实 kind
      若 (恢复后的) nl_mode.stop_at(newline, actual_kind):
          token.kind = End        ← 外层规则认为该叫停：保留伪造
      否则:
          token.kind = actual_kind ← 外层规则认为不该叫停：还原真实 kind
```

一个关键点：**复审只在 `mode != previous` 时发生**。如果内层规则与外层相同，伪造与否的结论不会变，没必要重算。另外，复审能成立的前提是「伪造 `End` 时只改 `token.kind`、不改 `token.node`」——所以 `token.node.kind()` 永远是真实 kind，这是整个机制的安全锚。

#### 4.2.3 源码精读

先看 `lex`（[parser.rs:L1854-L1886](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1854-L1886)）。重点三处：

- trivia 扫描循环（L1862-L1869）：边扫边把 trivia 推入 `nodes`、累计 `had_newline` 与 `parbreak`。注意换行本身就是 trivia（`Space`/`Parbreak` 都是 trivia），所以「换行信息」是在扫 trivia 时顺带收集的。
- column 的条件计算（L1872-L1874）：`(lexer.mode() == SyntaxMode::Markup).then(|| lexer.column(start))`——这就是 4.1.2 说的「column 只在 Markup 有值」的来源。
- 伪造判定（L1875-L1879）：`if nl_mode.stop_at(newline, kind) { kind = SyntaxKind::End; }`，注释明确说「Insert a temporary `SyntaxKind::End` to halt the parser. The actual kind will be restored from `node` later.」——「later」就是指 `with_nl_mode` 的复审。

`lexer.column(start)`（[lexer.rs:L68-L72](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L68-L72)）的实现是：从 `start` 往前看，数到最近的换行字符为止有多少个字符——也就是「这一行已经走了多少列」。`lexer.newline()`（[lexer.rs:L63-L65](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L63-L65)）返回 lexer 自己记录的「上一个 token 是否含换行」标志（u3-l1 讲过这块状态）。

再看 `with_nl_mode`（[parser.rs:L1831-L1847](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1831-L1847)）。对照上面的伪代码逐行读：L1832-L1833 保存并切换 `nl_mode`，L1834 执行子解析，L1835 恢复，L1836-L1846 是复审。复审块的条件 `self.token.newline.is_some() && mode != previous`（L1836-L1837）保证只在「当前 token 跨越了换行」且「内外规则确实不同」时才介入；`self.nl_mode.stop_at(...)`（L1841）用的是**恢复后**的 `nl_mode`（即外层 `previous`），这正是「让外层规则重新发声」。

`enter_modes`（[parser.rs:L1810-L1825](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1810-L1825)）是 `with_nl_mode` 的「同时改词法模式」版：它在 `with_nl_mode(stop, func)` 之外，还保存/恢复 `lexer.mode()`，并在退出时若模式确实变了，回退游标重 lex 最后一个 token（因为同一个文本片段在不同词法模式下会切成不同 token，u3-l2 讲过）。所以 `enter_modes` = 换词法模式 + 换行模式 + 退出重 lex。

#### 4.2.4 代码实践

**实践目标**：用一个具体例子看清「内层伪造的 `End` 不会误伤外层」。

**操作步骤**：阅读下面这条调用链，并回答问题。

```text
顶层 parse("= Heading\nbody")  →  markup_exprs(nl_mode = Continue)
   markup_expr 遇到 HeadingMarker(at_start)  →  heading(p)
      heading: with_nl_mode(Stop, |p| {
         assert(HeadingMarker);
         markup(false, false, {Label, RightBracket, End});   // 内层，nl_mode=Stop
         wrap(m, Heading);
      })
   回到 markup_exprs 继续循环
```

**需要观察的现象与预期结果**：

1. 进入 `heading` 时，`previous = Continue`，`nl_mode` 被切成 `Stop`。
2. 内层 `markup` 吃完 ` Heading` 后，lex 处理 `\n` 后的 `body` token：trivia 含换行，`stop_at(Stop, Text) == true` → `body` 的 kind 被伪造为 `End` → 内层 `markup` 循环见到 `End` 退出 → 标题只含 ` Heading`。
3. `heading` 的 `with_nl_mode` 退出：恢复 `nl_mode = Continue`。此时 `token.newline` 仍为 `Some`，且 `Stop != Continue`，触发复审。`actual_kind = Text`（`token.node` 里存的是真实 kind），`stop_at(Continue, Text) == false` → **把 `token.kind` 还原为 `Text`**。
4. 回到外层 `markup_exprs`，当前 token 是 `Text("body")`（不是 `End`），循环继续，正确地把 `body` 解析为正文。

**结论**：如果**没有**复审这一步，外层 `markup_exprs` 会看到内层留下的伪造 `End` 而提前结束，导致 `body` 被丢弃。复审机制保证了「内层的叫停只在内层有效，出栈后由外层重新定夺」。

> 待本地验证：用 `Source::detached("= Heading\nbody")` 解析，遍历 `root().descendants()`，确认 `Heading` 节点只覆盖 `= Heading`，而 `body` 作为 `Text` 出现在 `Heading` 的**兄弟**位置（同属顶层 `Markup` 的子节点），而非被丢掉或误吞。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `lex` 伪造 `End` 时只改 `token.kind`、不改 `token.node`？
**答**：因为出栈复审（`with_nl_mode`）需要用「真实 kind」重新判定。`token.node.kind()` 是取回真实 kind 的唯一途径；若连 `node` 一起改了，真实信息就丢失了，无法复审。这也是 `expected` 注释里强调「actual kind will be restored from `node` later」的根据。

**练习 2**：`code_exprs` 用 `with_nl_mode(ContextualContinue, ...)` 包住「解析一条语句」的整个过程。请解释为什么 `#if x [a]\nelse [b]` 里的 `else` 能被 `if` 抓住。
**答**：解析 `if` 这条语句时，`conditional` 在 `code_expr_prec` 内部被调用，整个调用链都在 `ContextualContinue` 闭包内。当 lex 处理 `\nelse` 时，trivia 含换行，但 `stop_at(ContextualContinue, Else) == false`（`ContextualContinue` 对 `Else` 放行）→ 不伪造 `End` → `conditional` 能看到并吃掉 `Else`，把 `else [b]` 续接到 `if` 上。

---

### 4.3 五变体在真实解析中的用法（速查与串讲）

本节没有新的源码机制，只是把五个变体在 parser 里的**真实调用点**集中陈列，作为速查表，也印证 4.1 的判定表。这一节的价值在于：你会看到「同一个变体被反复复用」，体会 `AtNewline` 作为「可组合开关」的设计。

| 调用点 | 使用的变体 | 为什么 |
| --- | --- | --- |
| `markup_exprs`（[parser.rs:L50-L61](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L50-L61)） | 继承顶层 `Continue` | 顶层正文换行不切分结构，只靠 `at_start` 决定行首标记 |
| `heading`（[parser.rs:L171-L178](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L171-L178)） | `Stop` | 标题单行，行末即收 |
| `list_item` / `enum_item`（[parser.rs:L181-L198](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L181-L198)） | `RequireColumn(p.current_column())` | 续行需比标记列号更深 |
| `term_item`（[parser.rs:L201-L212](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L201-L212)） | 嵌套：外层 `RequireColumn`，内层 term 部分用 `Stop` | term（`/ Term`）单行，description（`: ...`）可续行 |
| `strong` / `emph`（[parser.rs:L137-L168](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L137-L168)） | `StopParBreak` | 可跨单换行，遇空行止 |
| `equation`（[parser.rs:L225-L233](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L225-L233)） | 经 `enter_modes(Math, Continue, ...)` | `$...$` 内部换行不切分 |
| `code_exprs`（[parser.rs:L556-L576](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L556-L576)） | `ContextualContinue` | 语句切分，但放行 `else`/`.` 续行 |
| `embedded_code_expr`（[parser.rs:L579-L598](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L579-L598)） | 经 `enter_modes(Code, Stop, ...)` | `#expr` 只抓一条，行末即止 |
| `depth_check_error`（[parser.rs:L2103-L2128](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L2103-L2128)） | `Continue` | 深度超限兜底，一路吃到平衡点，不因换行提前停 |

两个值得注意的细节：

- `list_item` 里 `RequireColumn(p.current_column())` 的参数是在**进入 `with_nl_mode` 之前**求值的（[parser.rs:L182](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L182)）。此刻当前 token 正是 `ListMarker`，`current_column()`（[parser.rs:L1689-L1694](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1689-L1694)）返回的就是「标记符所在列号」，于是 `min_col` 锁定为「列表标记的缩进」，续行只有更深才被吞入。
- `markup_exprs` 主循环用 `at_start = p.had_newline()`（[parser.rs:L59](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L59)）来驱动「行首标记」判定（u4-l3 讲过）。注意 `had_newline()` 只问「trivia 里有没有换行」，**与 `nl_mode` 无关**——也就是说，即便 `Continue` 模式不伪造 `End`，parser 仍然知道「刚才跨过了换行」，从而把下一个 `=`/`-`/`+`/`/` 当作行首结构。这是「换行的两重身份」在代码上的体现：一重（`had_newline`）驱动行首判定，一重（`nl_mode`/`stop_at`）驱动结构终止。

> 本节是「串讲/速查」，不单独设实践任务，相关动手任务并入第 5 节综合实践。

---

### 4.4 错误恢复工具箱：`unexpected` / `expected` / `expected_at` / `hint`

#### 4.4.1 概念说明

换行机制解决的是「合法输入怎么切分」，错误恢复解决的是「不合法输入怎么办」。Typst parser 有一条铁律：**任何输入都要解析出一棵完整的 CST，绝不 panic、绝不卡死**。这是因为 CST 是「无损真相来源」，下游（求值、IDE、增量重解析）都依赖「无论输入多烂，总能拿到一棵树」。

错误恢复的核心动作只有两个：

1. **造 Error 节点**：把「出问题的片段」或「本该出现却没出现的东西」标记成 `SyntaxKind::Error` 节点，塞进 CST。
2. **继续往前吃**：适当消费 token 让游标前进，保证解析不会原地打转。

围绕这两个动作，parser 提供四个语义不同的函数，外加一个 `trim_errors` 去噪函数（4.5 专门讲）。先区分前四个：

- **`expected(thing)`**——「本该出现 `thing`（如 `expected ";"`、`expected "expression"`）」。它有两条分支：如果当前 token 本身是个错误 token，那就 `trim_errors` 后**吃掉它**（保证它在正确词法模式里被消费，见 4.5）；否则，如果「上一个非 trivia 节点还不是 Error」，就在当前位置**插入一个零长度的 Error 节点**，消息为 `expected {thing}`。零长度意味着它不「占用」任何原文文本，只是个标记。
- **`expected_at(m, thing)`**——`expected` 的底层，在指定 marker `m` 处插入零长 Error 节点。`expected` 调它时传的是 `before_trivia()`（当前 token 的 trivia 起点）。
- **`unexpected()`**——「当前这个 token 是个意外」。它 `trim_errors` 后，把当前 token 吃掉，并把吃掉的节点**原地转成 Error**，消息为 `unexpected {kind}`。与 `expected` 的关键区别：`unexpected` **会消费掉这个 token**（因为它不该出现在这），而 `expected`（非错误 token 分支）**只插零长标记、不消费**（因为当前 token 可能对更外层还有用）。
- **`hint(text)`**——给「紧邻的尾部 Error 节点」追加一条用户可见的建议。它不造新节点，只是往已有 Error 的 hints 列表里 push 一条字符串。

还有一个面向上层的高层封装 `expect(kind)`——「期望并尝试吃掉 `kind`，吃不到就报错」，它内部根据情况调用 `expected` 或走「关键字当标识符」的特殊路径。

#### 4.4.2 核心流程

各函数的控制流（简化伪代码）：

```
expected(thing):
  if 当前 token.kind.is_error():
      trim_errors(); eat()                ← 吃掉错误 token（保词法模式正确）
  else if !after_error():                 ← 上一个非 trivia 节点还不是 Error
      expected_at(before_trivia(), thing) ← 插零长 Error：expected {thing}

expected_at(m, thing):
  nodes.insert(m.0, SyntaxNode::error("expected {thing}", ""))   ← 文本为空→零长

hint(text):
  m = before_trivia()
  if let Some(error) = nodes.get_mut(m.0 - 1):   ← 紧邻的前一个节点
      error.hint(text)                           ← 往它的 hints 加一条

unexpected():
  trim_errors()
  balanced &= !当前 token.kind.is_grouping()     ← 误吃定界符会破坏平衡
  eat_and_get().unexpected()              ← 吃掉并原地转 Error：unexpected {kind}

expect(kind):
  if at(kind): eat()
  else if kind==Ident 且 当前是关键字:
      trim_errors(); eat_and_get().expected("identifier")  ← 关键字转错并附 hint
  else:
      balanced &= !kind.is_grouping(); expected(kind.name())
```

几个要点：

- `after_error()`（[parser.rs:L2031-L2034](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L2031-L2034)）的作用是**防堆叠**：如果上一个节点已经是 Error 了，就不再插新的 `expected` Error，避免一坨「expected X」「expected Y」叠在一起刷屏。
- `eat_and_get()`（[parser.rs:L1733-L1737](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1733-L1737)）吃掉当前 token 并返回**对刚入栈节点的可变引用**，所以 `unexpected()` 能紧接着把它原地转成 Error。这是「先 eat 拿到引用、再 mutate」的常用模式。
- 节点级的 `convert_to_error`（[node.rs:L491-L496](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L491-L496)）会先取走原文文本再换成 Error 节点，保证「转错」也不丢原文（CST 无损）；`unexpected`（[node.rs:L512-L514](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L512-L514)）和 `expected`（[node.rs:L500-L509](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L500-L509)）都基于它。

#### 4.4.3 源码精读

`expected`（[parser.rs:L2010-L2028](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L2010-L2028）：它的注释和那条超长的「`#import "str`」注释（L2013-L2022）非常重要——这是 4.5 要展开的「为何必须吃掉错误 token」的动机，先记住结论：**遇到错误 token 时若不消费，退出当前词法模式后，未来的增量重解析可能把它切成另一种 token，导致全量解析与增量解析结果不一致**。

`expected_at`（[parser.rs:L2038-L2041](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L2038-L2041)）：一行 `SyntaxNode::error(eco_format!("expected {thing}"), "")`——注意第二个参数（文本）是空串，所以这是个**零长** Error。它 `insert(m.0, ...)` 而非 push，插在 marker 指定的位置。

`hint`（[parser.rs:L2044-L2049](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L2044-L2049)）：`m.0 - 1` 取「紧邻当前 token 的前一个节点」，若它是可变引用就调用节点级 `hint`（[node.rs:L170-L173](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L170-L173)），往它的 hints 列表 push。若前一个不是 Error/Warning，`hint` 会 panic，所以调用方要确保「刚报过错」才 hint。

`unexpected`（[parser.rs:L2053-L2057](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L2053-L2057)）：先 `trim_errors()`，再更新 `balanced &= !self.token.kind.is_grouping()`——意思是「如果意外吃掉的是定界符（括号等），就标记解析器已不平衡」，这一标志会被增量重解析（u9）用来拒绝局部重解析。最后 `eat_and_get().unexpected()` 吃掉并转错。

`expect`（[parser.rs:L1983-L1995](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1983-L1995)）：注意 L1987-L1989 的特殊路径——「期望标识符却遇到关键字」时，走 `eat_and_get().expected("identifier")`，而节点级 `expected`（[node.rs:L500-L509](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L500-L509)）会自动追加 hint「keyword `x` is not allowed as an identifier; try `x_` instead」。这是一个「错误 + 修正建议」联动的好例子。

最后看 `code_exprs` 里的实战（[parser.rs:L567-L573](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L567-L573)）：解析完一条语句后，若既不在 stop_set、也没吃到分号，就 `expected("semicolon or line break")`；如果当前是 `Label`，再追加两条 hint（「labels can only be applied in markup mode」「try wrapping your code in a markup block」）。这示范了「`expected` 报错 + `hint` 给出路」的标准组合。

#### 4.4.4 代码实践

**实践目标**：用故意写错的输入，亲眼看到 `Error` 节点与 `hints` 如何产生。

**操作步骤**（源码阅读型 + 可选运行）：

1. 解析 `#let x = 1\n#let y = 2`：这是**合法**输入。阅读 `code_exprs`（[parser.rs:L556-L576](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L556-L576)），推演：第一条 `let` 解析完后，`ContextualContinue` 下 `\n#` 中的 `#`（`Hash`）不是 `Else`/`Dot` → `stop_at` 叫停 → 当前语句结束；外层循环见到换行/分号判定通过（无分号但到了新语句边界），继续解析第二条 `let`。预期：CST 里有两条 `LetBinding`，**没有 Error**。
2. 解析 `#let = `（故意写错，缺标识符）：阅读 `let_binding`（你已在 u4-l4 读过）会调用 `expect(Ident)` 或类似。当前 token 是 `=`（`Eq`），既非 `Ident` 也非关键字 → 走 `expected("identifier")`，在 `=` 之前插入零长 Error `expected identifier`；随后 `=` 被当作 `let` 的部分继续处理。预期：CST 里出现一个零长度 `Error` 节点，消息为 `expected identifier`（或类似），并可能带 hint。
3.（可选运行）若你想本地确认，可在仓库根新建一个临时 bin 或在 `cargo test -p typst-syntax` 里加一个测试，用 `typst_syntax::parse("#let = ")` 拿到 root，遍历 `root().descendants()`，过滤 `kind() == SyntaxKind::Error`，打印其 `leaf_text`（应为空，证明零长）与通过诊断收集得到的消息/hints。

**需要观察的现象与预期结果**：

- 合法输入：两条 `LetBinding`，无 Error。
- 错误输入：至少一个 `Error` 节点，文本长度为 0（零长），消息形如 `expected identifier`；若触发了关键字路径，还会有 hint。
- 注意：本讲不展开如何从 `SyntaxNode` 收集 `SyntaxDiagnostic`（那是 u5-l4 的主题），这里只需确认「Error 节点确实被插入了」。

> 待本地验证：精确的 Error 数量与消息措辞取决于 `let_binding` 的具体容错路径，建议以本地 `parse` 输出为准。

#### 4.4.5 小练习与答案

**练习 1**：`unexpected()` 和 `expected(thing)`（非错误 token 分支）在「是否消费当前 token」上有何区别？为什么？
**答**：`unexpected()` **会**消费当前 token（`eat_and_get()`），因为「意外出现的 token」对任何外层都没用，吃掉它才能让游标前进、避免死循环。`expected()` 的非错误分支**不**消费，只插零长 Error——因为当前 token（比如该出现 `;` 却看到了下一条语句的起始）对更外层可能仍然合法，不能误吃。两者的共同点是都尽量保证「向前推进」。

**练习 2**：为什么 `hint()` 必须紧跟着一次 `expected`/`unexpected` 调用？
**答**：`hint()` 往 `nodes[m.0 - 1]`（紧邻的前一个节点）加建议，而节点级 `hint`（[node.rs:L170-L173](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L170-L173)）要求该节点是 Error/Warning，否则 panic。所以调用顺序必然是「先造 Error，再 hint」，例如 `code_exprs` 里 `expected("semicolon or line break")` 之后才 `hint(...)`。

---

### 4.5 `trim_errors` 与错误恢复的总策略

#### 4.5.1 概念说明

错误恢复还有一个低调但关键的帮手：`trim_errors`。它的职责很窄——**删除 `nodes` 尾部那些「零长度」的 Error 节点**。

为什么会有「零长 Error」需要清理？因为 `expected_at` 插的就是零长 Error（不占原文文本）。设想一种场景：解析器在某个位置反复尝试、多次 `expected(...)`，但 `after_error()` 只防「紧邻一个」，挡不住「零长 Error 之后又因为别的原因走到 `trim_errors`」的累积；更常见的是 `unexpected`/`expected` 在吃掉错误 token **之前**先调用了 `trim_errors`——目的是把之前累计的、还没消费任何文本的「占位 Error」清掉，避免它们和即将产生的真实 Error 重复堆叠。

一句话总结 `trim_errors` 的策略：**从 `nodes` 尾部往前扫，凡是「Error 且长度为 0」的连续段统统删掉；一旦遇到非 Error 节点或非零长 Error 就停手**。它只动尾部、只删零长，因此绝不会误删「已经裹着真实文本的 Error」（那种 Error 保留了原文，是有意义的）。

这条策略与一个更大的设计目标挂钩，也是 `expected` 那条超长注释（[parser.rs:L2013-L2022](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L2013-L2022)）想表达的东西：**全量解析与增量重解析必须产出一致的 CST**。注释举的例子是 `#import "str`（未闭合字符串）：

- 在 Code 模式下，`"str` 被词法器切成一个未闭合的字符串 Error token。
- 若 `expected` 不消费它就退出 Code 模式回到 Markup，这个 token 可能被重 lex 成 Markup 文本。
- 之后用户补上闭合引号 `"`，增量重解析只在局部进行，可能仍把它当 Markup 文本，从而**与全量解析（会把它当字符串）不一致**。
- 所以 `expected` 遇到错误 token 时，必须 `trim_errors()` 后 `eat()`，**确保错误 token 在当前（正确的）词法模式里被消费掉**。

`trim_errors` 在这里的角色是「消费前先清理战场」——把零长的占位 Error 删干净，再吃掉真实错误 token，保证 CST 干净且模式正确。

#### 4.5.2 核心流程

```
trim_errors():
  end = before_trivia().0            ← 不含当前 token 的 trivia
  start = end
  while start > 0
        且 nodes[start-1].kind().is_error()
        且 nodes[start-1].is_empty():   ← 零长
      start -= 1
  nodes.drain(start..end)            ← 删除 [start, end) 这段零长 Error
```

它被三处调用，构成错误恢复的「标准前奏」：

1. `expected`（吃错误 token 之前）：[parser.rs:L2023](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L2023)。
2. `unexpected`（吃意外 token 之前）：[parser.rs:L2054](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L2054)。
3. `expect`（关键字当标识符路径，吃之前）：[parser.rs:L1988](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1988)。

模式很统一：**「要吃掉一个出问题的 token 之前，先 `trim_errors` 把零长噪音清掉」**。

#### 4.5.3 源码精读

`trim_errors`（[parser.rs:L2060-L2070](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L2060-L2070)）：`end` 取 `before_trivia()`（排除当前 token 自带的 trivia，只看已落袋的节点）；`while` 条件同时检查 `is_error()` 与 `is_empty()`（[node.rs:L226-L228](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L226-L228) 的 `is_empty` 即 `len == 0`）；最后 `drain(start..end)` 一次性删掉这段。

回到 `expected` 的完整逻辑（[parser.rs:L2010-L2028](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L2010-L2028)），现在可以完整读懂它的三分支：

- 当前 token 是 Error（L2011）：`trim_errors()` + `eat()`——吃掉它，保词法模式正确（即上面 `#import "str` 的动机）。
- 当前 token 非 Error，且前面没有紧邻 Error（`!after_error()`，L2025）：`expected_at` 插零长 Error。
- 否则（前面已有 Error）：什么都不做——防堆叠。

再看 `balanced` 字段（[parser.rs:L1513-L1515](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1513-L1515)）：注释说它「只从 true 翻到 false，不回弹」。`unexpected`（[parser.rs:L2055](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L2055)）和 `expect`（[parser.rs:L1991](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1991)）在涉及定界符时会把它清零。这个标志是给 u9 增量重解析用的：一旦本轮解析「吃错了定界符」，局部重解析就可能不安全，reparser 会据此回退到全量解析。本讲只需记住它是错误恢复的一个「副作用记录器」。

最后，`depth_check_error`（[parser.rs:L2103-L2128](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L2103-L2128)）是错误恢复的「终极兜底」：当嵌套深度超过 `MAX_DEPTH`(256) 时，它用 `with_nl_mode(Continue, ...)` 一路吃 token、用 `balance` 计数器配平括号，直到遇到 stop_set 或 `End`，最后用 `wrap_error` 把这段包成一个 `maximum parsing depth exceeded` 错误。注释（L2107-L2111）强调它必须保证「向前推进」（至少吃一个 token），否则解析器会死循环。它选择 `Continue` 模式正是因为兜底要「无视换行、一口气吃到平衡点」。

#### 4.5.4 代码实践

**实践目标**：观察 `trim_errors` 如何让错误输入产出「干净」的 Error 节点。

**操作步骤**（源码阅读型）：

1. 解析 `#let x = 1\n#let y = 2`（合法）。阅读 `code_exprs`（[parser.rs:L560-L575](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L560-L575)）：第一条 `let` 后，`ContextualContinue` 对 `\n#`（`#` 非 `Else`/`Dot`）叫停 → 进入 L567 的判定：`!at_set(stop_set) && !eat_if(Semicolon)`——此时换行已被 `ContextualContinue` 当成有效分隔，语句正常结束，**不报错**。预期：两条干净 `LetBinding`，零 Error。
2. 解析 `#let = `（错误）。推演 `let_binding` 缺标识符：走到 `expect(Ident)` 类逻辑，当前是 `=` → 走 `expected("identifier")` 分支（非错误 token、且 `!after_error()`）→ 在 `=` 前**插一个零长 Error**。预期：CST 中有一个零长 `expected identifier` Error。
3. 现在设想「如果没有 `trim_errors` 会怎样」：假如在这之前 parser 因别的尝试已经插了若干零长 `expected` Error，没有清理就会和新的 Error 叠成一串。`unexpected`/`expected` 在吃 token 前调 `trim_errors` 正是为了避免这种堆叠。
4.（可选运行）本地用 `typst_syntax::parse("#let = ")` 与 `parse("#let x = 1\n#let y = 2")`，遍历打印所有 `Error` 节点的 `len()` 与诊断消息，确认前者出现零长 Error、后者没有 Error。

**需要观察的现象与预期结果**：

- 合法输入：Error 数量为 0。
- 错误输入：存在 `len() == 0` 的 Error 节点，消息含 `expected identifier`（精确措辞待本地验证）。
- 不应出现「一长串连续零长 Error 堆叠」——这正是 `trim_errors` 的功劳。

> 待本地验证：精确的 Error 消息与数量以本地 `parse` + 诊断收集（u5-l4）结果为准；本讲聚焦于「Error 节点会被插入、且会被去噪」这一机制。

#### 4.5.5 小练习与答案

**练习 1**：`trim_errors` 为什么只删「零长」Error，而不删「裹着真实文本」的 Error？
**答**：零长 Error 是 `expected_at` 插的「占位标记」，不承载原文，删掉无损于 CST 的「无损还原」性质。而裹着真实文本的 Error（如 `unexpected` 把一个真实 token 转错、或词法器的 Error token）保留了出错处的原文，是 CST 的一部分，删了就破坏了「CST 可还原原文」的不变量。

**练习 2**：`expected` 在「当前 token 是 Error」时为什么要 `trim_errors()` + `eat()`，而不是直接 `expected_at` 插标记？
**答**：两个原因。其一，若不吃掉错误 token，退出当前词法模式后它可能被重 lex 成别的 token，导致未来增量重解析与全量解析结果不一致（`#import "str` 例子）。其二，`trim_errors` 先清掉之前的零长噪音，再吃掉真实错误 token，使 CST 干净、且 token 在正确模式下被消费。

---

## 5. 综合实践

把本讲两条主线（换行模式 + 错误恢复）串起来，完成下面这个「对比解析」任务。

**任务**：准备三段输入，分别用 `typst_syntax::parse`（或 `Source::detached`）解析，遍历 CST 并报告每段的：(a) 顶层 `Markup` 下直接子节点的 kind 序列；(b) 所有 `Error` 节点的数量、是否零长、消息；(c) 是否有 hint。

```typst
// 输入 A：换行切语句（合法）
#let x = 1
#let y = 2

// 输入 B：标题单行 + 续行（换行模式）
= Title
still body

// 输入 C：错误恢复
#let = 
```

**操作步骤**：

1. 对输入 A，验证「两条 `LetBinding`、无 Error」。重点解释：为何 `\n#` 处的换行能切开两条语句（`ContextualContinue` 对非 `Else`/`Dot` 叫停），而又不产生 `expected("semicolon or line break")`（因为换行本身就是合法的语句分隔）。
2. 对输入 B，验证「`Heading` 只覆盖 `= Title`，`still body` 是顶层 `Markup` 的兄弟 `Text`」。重点用 4.2 的「内层 `Stop` 伪造 End → 出栈复审还原真实 kind」解释：为何标题的 `Stop` 没有把外层 markup 也一起叫停。
3. 对输入 C，验证「出现零长 `Error` 节点（消息含 `expected identifier` 之类）」。重点解释：`expect(Ident)` 失败 → `expected("identifier")` → `expected_at` 插零长 Error；若你本地看到 hint，说明走了关键字路径或 `let_binding` 主动追加了建议。

**预期结果（自检清单）**：

- [ ] A：2 个 `LetBinding`，0 个 Error。
- [ ] B：1 个 `Heading`（不含 `still body`）+ 至少 1 个 `Text`（`still body`）作为兄弟。
- [ ] C：≥1 个零长 Error，消息与「缺标识符」相关。
- [ ] 你能用「`stop_at` 判定 + `with_nl_mode` 出栈复审」解释 B 的结构。
- [ ] 你能用「`expected` → `expected_at` 插零长 Error + `trim_errors` 去噪」解释 C 的 Error。

> 待本地验证：精确节点序列与消息措辞以本地解析输出为准；若不便运行，可改为「阅读源码画出每段的解析步骤与最终 CST 形状」的纸面练习。

## 6. 本讲小结

- **换行的两重身份**：在 Typst 里 `\n` 既是可跳过的 trivia，又是语句分隔符与结构边界。`had_newline()` 驱动「行首标记」判定，`nl_mode`/`stop_at` 驱动「结构终止」，二者独立。
- **`AtNewline` 五变体 + `stop_at`** 是换行判定的唯一裁判：`Continue`（永不叫停）、`Stop`（恒叫停）、`ContextualContinue`（只放行 `else`/`.`）、`StopParBreak`（只对段落断行叫停）、`RequireColumn(c)`（续行需列号 `> c`）。`column` 只在 Markup 有值。
- **伪造 `End` 机制**：`lex` 在 trivia 含换行时，按当前 `nl_mode.stop_at(...)` 决定是否把当前 token 的 `kind` 改成 `End`（只改 kind、不改 `node`），从而让上层 `while !at_set(...)` 循环天然停止。
- **栈式管理 + 出栈复审**：`with_nl_mode` 把调用栈当模式栈，入栈切 `nl_mode`、出栈恢复；若当前 token 跨越换行且内外模式不同，用恢复后的 `nl_mode` 重跑 `stop_at`，决定保留伪造 `End` 还是还原真实 kind——这保证了「内层的叫停不误伤外层」。
- **错误恢复四件套**：`expected`（本该出现某物，插零长 Error 或吃掉错误 token）、`expected_at`（在指定位插零长 Error）、`unexpected`（吃掉意外 token 并原地转错）、`hint`（给紧邻尾部 Error 追加建议）。Typst parser 永不 panic，所有问题都变成 CST 里的 Error 节点。
- **`trim_errors` 去噪**：吃掉出问题 token 之前，先删除 `nodes` 尾部所有「零长 Error」，避免占位 Error 堆叠；同时保证错误 token 在正确词法模式里被消费，维护「全量解析与增量重解析一致」。`balanced` 标志则把「误吃定界符」记录下来供 u9 的 reparser 决策。

## 7. 下一步学习建议

- **进入 U5（CST 数据结构）**：本讲频繁提到「Error 节点」「零长 Error」「节点级 `hint`/`convert_to_error`」，这些都是 `SyntaxNode` 的内部形态。u5-l1 / u5-l2 会讲清 `Node` 枚举的 Leaf/Inner/Error/Warning 四种形态，u5-l4 会讲如何从 CST 收集 `SyntaxDiagnostic`（即本讲里「待本地验证」的诊断消息来源）。建议接着读 u5-l4，把本讲的实践任务真正跑通。
- **阅读 `let_binding` 全文**：本讲的错误恢复实践以 `#let = ` 为例，但没有展开 `let_binding` 的完整容错路径。回到 [parser.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs) 读 `let_binding`，观察它如何在 `expect(Ident)` 失败后继续解析 `=`、模式、初始化表达式，是一次很好的「错误恢复现场教学」。
- **预告 u9（增量重解析）**：本讲两次埋下伏笔——`expected` 吃错误 token 是为了「全量与增量解析一致」（[parser.rs:L2013-L2022](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L2013-L2022)），`balanced` 标志供 reparser 判断是否安全。学到 u9 时你会看到这两个设计如何被 `reparser.rs` 消费。在那之前，先掌握 U5（CST）与 U6（Span）会让 u9 顺畅很多。

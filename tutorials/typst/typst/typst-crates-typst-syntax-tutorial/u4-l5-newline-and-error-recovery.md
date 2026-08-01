# 新行处理与错误恢复

## 1. 本讲目标

本讲是 Parser 单元（U4）的收尾篇，聚焦两件最影响「解析器是否好用」的事：

1. **换行如何参与语法判定**：Typst 是「对空白与换行敏感」的语言——在 Markup 里换行分割段落，在 Code 里换行结束一条语句，在列表里缩进决定续行。本讲要讲清 `AtNewline` 枚举与 `stop_at` 如何把这套规则集中表达。
2. **遇到错误如何恢复**：解析器不能一遇错就崩溃，而要尽量继续往下解析、产出可定位、带修正建议的诊断。本讲要讲清 `expected` / `unexpected` / `expected_at` / `hint` / `trim_errors` 这一组错误恢复原语。

学完后你应当能够：

- 说清 `AtNewline` 五个变体（`Continue` / `Stop` / `ContextualContinue` / `StopParBreak` / `RequireColumn`）各自的判定语义；
- 解释 `with_nl_mode` 如何用「调用栈」管理换行模式，以及 `lex` 如何把一个换行「伪装」成 `SyntaxKind::End` 叫停解析；
- 看懂 `expected` / `unexpected` / `expected_at` 如何在不破坏 CST 完整性的前提下做错误恢复，以及 `after_error` 去重、`balanced` 失衡标记的作用；
- 学会用 `hint()` 与 `trim_errors()` 给出面向用户的修正建议、并清理零长度错误噪音。

## 2. 前置知识

本讲建立在已学讲义之上，下列概念不再从头解释：

- **CST 与 `SyntaxNode`**（u4-l1/u4-l2，U5 基础）：解析器产出的是无损 CST，错误也是一种节点（`Node::Error`），警告是包装层（`Node::Warning`）。
- **`Token` 单 token 前瞻与 trivia**（u4-l2）：当前 token 之前可能附带若干 trivia（空白、注释），`Token` 用 `n_trivia` 与 `newline` 记录这些信息。本讲的「换行」就藏在 trivia 里。
- **Marker / eat / wrap 解析原语**（u4-l2）：`eat()` 推进一个 token，`wrap(m, kind)` 把一段已解析节点事后打包成内部节点。
- **三种 `SyntaxMode`**（u1-l1）：Markup / Code / Math。换行模式（`AtNewline`）与语法模式（`SyntaxMode`）是两套**正交**的模式栈，本讲专门讲前者。

一个贯穿全讲的关键直觉：

> Typst 的换行**不是语法树里的显式节点**，而是 lexer 产出的 trivia 的一部分。解析器通过「在换行处临时把当前 token 的 kind 改写成 `End`」来让上层循环自然停下。所以本质上没有「换行 token」，只有「因换行而触发的伪 End」。

## 3. 本讲源码地图

本讲几乎全部落在 **`src/parser.rs`** 一个文件里，错误节点的构造细节落在 **`src/node.rs`**，少量判定谓词在 **`src/kind.rs`**，一个「词法阶段就附带 hint」的例子在 **`src/lexer.rs`**。

| 文件 | 本讲关注的内容 |
| --- | --- |
| `src/parser.rs` | `AtNewline` 枚举与 `stop_at`；`with_nl_mode` / `enter_modes`；`lex` 中的伪 End；`expected` / `unexpected` / `expected_at` / `after_error` / `hint` / `trim_errors` / `expect` / `expect_closing_delimiter`；各解析函数如何选择换行模式 |
| `src/node.rs` | `SyntaxNode::convert_to_error` / `expected` / `unexpected` / `hint`；`errors_and_warnings` 诊断收集；`test_debug` 测试（权威的 Debug 输出样例） |
| `src/kind.rs` | `is_terminator` / `is_grouping` / `is_error` 三个判定谓词 |
| `src/lexer.rs` | `invalid_char_in_code`：词法阶段为 `##` 这类错误附带 hint 的例子 |

> 所有永久链接的 HEAD 均为 `32fd4cc3861e0ab99f4c42ca6bea281482ba9f51`。

## 4. 核心概念与源码讲解

### 4.1 AtNewline 换行模式与 stop_at 判定

#### 4.1.1 概念说明

同样是「换行」，在不同语法结构里含义不同：

- 在顶层 Markup 里，单个换行只是普通空白，段落要靠**两个以上连续换行**（`Parbreak`）才断开；
- 在 `#` 引导的代码表达式里，换行应当**结束这一条语句**；
- 在标题 `= ...` 里，换行应当**结束标题**；
- 在列表项 `- ...` 里，换行后**若缩进更深则续行、若缩进回退则结束**。

如果把这些规则散落在每个解析函数里，口径会四处漂移。typst 把「换行后到底停不停」抽象成一个枚举 `AtNewline`，并由一个集中函数 `stop_at` 给出布尔判定。这样所有解析函数只需声明「我用哪种换行模式」，决策逻辑只有一份。

关键认知：**`AtNewline` 回答的唯一问题是「当前这个（含换行 trivia 的）token，要不要被当作『到此为止』的 `End`？」** 它不直接决定解析走哪条分支，而是通过「伪造 `End`」间接叫停上层循环（伪造机制见 4.2）。

#### 4.1.2 核心流程

`stop_at(self, Newline{column, parbreak}, kind) -> bool` 接收两份信息：换行 trivia 的摘要（列号 `column`、是否段落断 `parbreak`）以及换行之后那个 token 的 `kind`（因为 `ContextualContinue` 要看它是不是 `Else` / `Dot`）。判定表如下：

| 变体 | `stop_at` 返回 `true`（叫停）的条件 | 典型场景 |
| --- | --- | --- |
| `Continue` | 永不（直接 `false`） | 顶层 markup、代码块 `{ }`、内容块 `[ ]`、公式内部 |
| `Stop` | 任何换行 | `#` 嵌入代码、标题（单行） |
| `ContextualContinue` | 含换行**且**当前 token 不是 `Else` 也不是 `Dot` | code 语句序列（让 `else`、链式 `.field` 跨行续接） |
| `StopParBreak` | 仅当换行是**段落断**（`parbreak`） | `*strong*` / `_emph_`（允许跨单行，遇空行才停） |
| `RequireColumn(min_col)` | 当前 token 的列号 `column <= min_col` | 列表 / 枚举项（缩进回退即停，更深即续行） |

两个要点单独拎出来：

1. **`RequireColumn` 的方向容易记反**。叫停条件是 `column <= min_col`，即**续行只有在列号严格大于 `min_col`（缩进更深）时才放行**，列号相等或更浅就叫停。列表标记在列号 `c`，那么列号 `> c` 的后续行属于该项，列号 `<= c` 的行结束该项。
2. **`column` 只在 Markup 模式下才有值**（见 4.2.3 的 `lex`），在 Code/Math 里为 `None`。`RequireColumn` 的 `stop_at` 对 `None` 的处理是「放行」（不叫停），因为跨语法模式边界的换行没法用列号判定，索性交给外层。

两个边界退化（源码注释明说）：`RequireColumn(0)` 行为接近 `Continue`（仅顶格换行叫停），`RequireColumn(usize::MAX)` 等价于 `Stop`（任何列号都 `<= MAX`，恒叫停）。

#### 4.1.3 源码精读

先看携带换行信息的载体 [`Newline`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1547-L1554)：`column` 是「下一 token 起点距最近换行的字符数」（仅 Markup 有），`parbreak` 标记 trivia 里是否含段落断。

再看枚举本身与 `stop_at`：[`enum AtNewline` 与 `impl AtNewline { fn stop_at }`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1556-L1594)。逐条对应：

- `Continue => false`：无条件放行；
- `Stop => true`：无条件叫停；
- `ContextualContinue`：用了 `#[expect(clippy::match_like_matches_macro)]`，因为它就是「只对 `Else | Dot` 返回 `false`、其余 `true`」的特判——这正是「`else`/`.` 可跨行续接」的源头；
- `StopParBreak => parbreak`：只对段落断叫停，普通单换行放行；
- `RequireColumn(min_col)`：`column.is_some_and(|column| column <= min_col)`，注释解释了 `None` 时放行的边界场景。

`stop_at` 全 crate 只有两处调用：一处在 `lex`（进 token 时初判，见 4.2.3），一处在 `with_nl_mode` 出栈时（复审，见 4.2.3）。**它是换行机制的「唯一裁判」，两处调用对应「初判」与「复审」**。

#### 4.1.4 代码实践

**目标**：用「源码阅读 + 手工推演」吃透 `stop_at` 的判定方向，尤其是 `RequireColumn`。

**操作步骤**：

1. 打开 [`stop_at`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1573-L1594)，对照判定表逐变体核对。
2. 对下面五组 `(模式, Newline{column,parbreak}, 当前 kind)`，**先自己**判断返回值，再去源码核对：
   - `(Stop, {None,false}, Ident)` → ？
   - `(ContextualContinue, {None,false}, Else)` → ？
   - `(ContextualContinue, {None,false}, Ident)` → ？
   - `(StopParBreak, {None,false}, Ident)`（单换行）→ ？
   - `(StopParBreak, {None,true}, Ident)`（段落断）→ ？
   - `(RequireColumn(4), {Some(2),false}, Text)`（缩进回退）→ ？
   - `(RequireColumn(4), {Some(6),false}, Text)`（缩进更深）→ ？

**需要观察的现象与预期结果**：依次为 `true / false / true / false / true / true / false`。

> 这一步无需运行，纯源码阅读；若想验证，可在 crate 的 `#[cfg(test)] mod tests` 里临时加 `assert!(AtNewline::Stop.stop_at(Newline{column:None,parbreak:false}, SyntaxKind::Ident))` 风格的断言（`AtNewline`/`Newline` 为 crate 私有，测试需写在 crate 内）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ContextualContinue` 需要把当前 token 的 `kind` 作为 `stop_at` 的参数，而其它四个变体其实用不到？

**答案**：`ContextualContinue` 的语义是「换行通常结束语句，但 `else` 子句和链式字段访问 `.` 是合法续接词」。判断「是不是续接词」必须看下一个真实 token 是什么，所以要把 `kind` 传进来。其余四个变体只依赖换行本身（有无换行 / 是否段落断 / 列号），与下一个 token 的种类无关——你看 `Continue/Stop/StopParBreak/RequireColumn` 分支确实没读 `kind`。

**练习 2**：若想让某个列表项「只要缩进不回到第 0 列就续行」，应选哪个变体、传什么值？

**答案**：用 `RequireColumn(0)`。`stop_at` 在 `column <= 0`（即 `column == 0`）时才返回 `true`，列号 ≥1 都会续行。源码注释也明确：`RequireColumn(0)` 行为接近 `Continue`——只在顶格换行时停。

---

### 4.2 with_nl_mode 与「伪 End」：换行如何终止解析

#### 4.2.1 概念说明

`AtNewline` 给出了「该不该停」的判定，但解析器的各个 `while !p.at_set(stop_set)` 循环只认 `SyntaxKind::End` 这一种停止信号。换行不是 token，怎么让它「停」？

typst 的做法很巧妙：**不改循环，改 token**。当 `lex` 发现当前 token 前的 trivia 含换行、且当前换行模式 `stop_at` 返回 `true` 时，就把这个 token 的 `kind` 临时改写成 `SyntaxKind::End`（真实 kind 仍保留在 `node` 里）。于是上层循环看到 `End`，自然停下。等退出这一层换行模式时，再用真实 kind 重新裁决一次。

这就是 [`with_nl_mode`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1827-L1847) 的职责：进入一个语法结构时临时切换换行模式，退出时还原——它把 Rust 的**调用栈当作换行模式的栈**来用（源码注释原话：「effectively repurposes the call stack as a stack of modes」）。

#### 4.2.2 核心流程

一次带换行的 token 产出（`lex` 内部）：

```
prev_end = 游标
循环：从 lexer 取 (kind, node)
  若 kind.is_trivia():
      累计 had_newline、parbreak；n_trivia += 1；把 node 推入 nodes（trivia 立即入栈）
      继续
  否则：跳出                      ← 得到真正的 token
若 had_newline:
  column = 仅当 Markup 模式才算 lexer.column(start)，否则 None
  newline = Newline { column, parbreak }
  若 nl_mode.stop_at(newline, kind) == true:
      kind = End                   ← 伪造！但 token.node 不变
  记录 newline 到 token
返回 Token { kind(可能被伪造), node(真实), n_trivia, newline, ... }
```

`with_nl_mode` 的入栈/出栈：

```
with_nl_mode(mode, func):
  previous = self.nl_mode
  self.nl_mode = mode              ← 入栈：切到内层规则
  func(self)                       ← 跑子解析（其间 lex 用 mode 规则伪造 End）
  self.nl_mode = previous          ← 出栈：恢复外层规则
  若 当前 token 仍带换行(newline is Some) 且 mode != previous:
      actual_kind = token.node.kind()    ← 取回真实 kind
      若 (恢复后的) nl_mode.stop_at(newline, actual_kind):
          token.kind = End         ← 外层规则认为该叫停：保留伪造
      否则:
          token.kind = actual_kind ← 外层规则认为不该叫停：还原真实 kind
```

关键点：**复审只在 `mode != previous` 时发生**（内外规则相同则结论不变，没必要重算）。复审能成立的前提是「伪造 `End` 时只改 `token.kind`、不改 `token.node`」——所以 `token.node.kind()` 永远是真实 kind，这是整个机制的安全锚。

各解析函数如何选择换行模式（本节核心，建议对照源码逐行确认）：

| 调用点 | 换行模式 | 含义 |
| --- | --- | --- |
| `markup_exprs`（顶层正文） | `Continue` | 换行不结束正文循环，只是 trivia |
| [`code_exprs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L556-L576) | `ContextualContinue` | 换行结束语句，但允许 `else` / `.` 续行 |
| [`embedded_code_expr`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L579-L598)（`#…`） | `Stop` | 一条 `#` 表达式遇换行立即结束 |
| [`strong` / `emph`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L137-L168) | `StopParBreak` | 允许跨单换行，遇段落断才停 |
| [`heading`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L170-L178) | `Stop` | 标题只占一行 |
| [`list_item` / `enum_item`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L180-L198) | `RequireColumn(标记列号)` | 缩进更深则续行、回退则结束 |
| [`code_block` / `content_block`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L766-L786)（`{ }` / `[ ]`） | `Continue` | 块内换行不结束，靠右分隔符结束 |
| [`equation`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L225-L233)（`$…$`） | `Continue` | 公式内换行不结束，靠 `$` 结束 |

#### 4.2.3 源码精读

- [`fn lex`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1854-L1886)：这是「换行 → 伪 End」的真正发生地。注释写着「this might insert a temporary `SyntaxKind::End` based on our newline mode」。注意三处：trivia 扫描循环（1862–1869）边扫边把 trivia 推入 `nodes`、累计换行；column 的条件计算（1872–1874）`(lexer.mode() == Markup).then(|| lexer.column(start))`，这就是「column 只在 Markup 有值」的来源；伪造判定（1875–1879）`if nl_mode.stop_at(newline, kind) { kind = End }`。
- [`fn with_nl_mode`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1827-L1847)：退出时的「重新裁决」（1836–1846）保证内层伪造的 End 不会错误地泄漏到外层——它依据**还原后**的 `self.nl_mode` 与 token 真实 kind 再判一次。
- [`fn current_column`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1687-L1694) 与 [`fn had_newline`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1682-L1685)：`current_column` 优先用换行缓存里的列号，没有才现算；它是 `RequireColumn(…)` 取「标记列号」的来源。
- [`fn enter_modes`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1806-L1825)：`enter_modes` = 切换 `SyntaxMode`（词法模式）+ `with_nl_mode`（换行模式），退出时若模式变化则回退 lexer 游标、丢弃最后一批 trivia 并重新 `lex`，因为「同一段文本在不同 SyntaxMode 下切成不同 token」（u3-l2 讲过）。

#### 4.2.4 代码实践

**目标**：跟踪 `= Heading\nbody`，看清「内层伪造的 End 不会误伤外层」——这是复审机制的最佳示例。

**操作步骤**：阅读下面这条调用链并回答。

```text
顶层 parse("= Heading\nbody")  →  markup_exprs(nl_mode = Continue)
   markup_expr 遇到 HeadingMarker(at_start)  →  heading(p)
      heading: with_nl_mode(Stop, |p| {
         assert(HeadingMarker);
         markup(false, false, {Label, RightBracket, End});   // 内层 nl_mode=Stop
         wrap(m, Heading);
      })
   回到 markup_exprs 继续循环
```

**需要观察的现象与预期结果**：

1. 进入 `heading` 时，`previous = Continue`，`nl_mode` 切成 `Stop`。
2. 内层 `markup` 吃完 ` Heading` 后，lex 处理 `\n` 后的 `body` token：trivia 含换行，`stop_at(Stop, Text) == true` → `body` 的 kind 被伪造为 `End` → 内层循环见到 `End` 退出 → 标题只含 ` Heading`。
3. `heading` 的 `with_nl_mode` 退出：恢复 `nl_mode = Continue`。此时 `token.newline` 仍为 `Some` 且 `Stop != Continue`，触发复审。`actual_kind = Text`，`stop_at(Continue, Text) == false` → **把 `token.kind` 还原为 `Text`**。
4. 回到外层 `markup_exprs`，当前 token 是 `Text("body")`（不是 `End`），循环继续，`body` 被正确解析为正文，而非被丢弃。

**结论**：若没有复审，外层会看到内层留下的伪造 `End` 而提前结束，导致 `body` 被丢弃。复审机制保证了「内层的叫停只在内层有效，出栈后由外层重新定夺」。

> 待本地验证：用 `Source::detached("= Heading\nbody")` 解析，遍历 `root().descendants()`，确认 `Heading` 节点只覆盖 `= Heading`，而 `body` 作为 `Text` 出现在 `Heading` 的**兄弟**位置（同属顶层 `Markup` 的子节点）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `lex` 伪造 `End` 时只改 `token.kind`、不改 `token.node`？

**答案**：因为出栈复审需要用「真实 kind」重新判定，`token.node.kind()` 是取回真实 kind 的唯一途径。若连 `node` 一起改了，真实信息丢失就无法复审。源码注释「actual kind will be restored from `node` later」正是此意。

**练习 2**：`code_block`（`{ }`）内部为什么用 `Continue` 而不是 `Stop`？

**答案**：代码块里通常有多条语句，靠 `}` 与 `;` 分隔结束，换行只是普通空白。若用 `Stop`，`lex` 会在块内第一个换行处插入伪 End，导致 `code_exprs` 循环提前结束、`}` 之前的语句被丢弃。用 `Continue` 才能让循环一直跑到真正的 `}`（它在外层传入的 `stop_set` 里）。

---

### 4.3 错误恢复原语：expected / unexpected / expected_at

#### 4.3.1 概念说明

解析器遇到「期望 A 却看到 B」时，不能直接 `return` 把后续文本全丢掉——那会让一个错别字导致整篇文档无法解析。typst 的策略是**就地生成一个 `Error` 节点塞进 CST，然后继续推进**，保证 CST 始终覆盖全部源码文本。这组原语分工明确：

- **`unexpected`**：当前 token **不该出现在这里** → 消费它并标注为错误；
- **`expected(thing)`**：**期望某物却没看到** → 若当前是个错误 token 就直接吃掉（避免在模式切换后影响增量重解析），否则在当前位置插入一个零长度 Error；并用 `after_error` 去重，避免连续报一串；
- **`expected_at(marker, thing)`**：在**指定位置**插入零长度 Error（常用于「该有却缺」的补位）；
- **`expect(kind)` / `expect_closing_delimiter`**：`expect` 期望某个具体 token，缺失则报错并把 `balanced` 置为失衡（当 `kind` 是分组符时）。

三条值得记住的设计：

1. **错误也是节点**：`unexpected` 用节点级 [`unexpected`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L511-L514) 把叶子就地 `convert_to_error`，文本不丢；`expected_at` 插入的是零长度 Error。两者都保证 CST 无损。
2. **`after_error` 去重**：连续缺失时只报一个，避免噪音瀑布。
3. **`balanced` 单向转假**：一旦吃到未配对的分组符（`([{` 之类），`balanced` 永久置假，供增量重解析判断「这次解析是否结构可信」。

#### 4.3.2 核心流程

```
unexpected():
  1. trim_errors()                       ← 先清掉之前残留的零长错误
  2. balanced &= !kind.is_grouping()     ← 吃掉一个未配对分组符 → 标记失衡
  3. eat_and_get().unexpected()          ← 吃掉该 token，并把它原地转成 Error 节点

expected(thing):
  若 token.kind.is_error():
      trim_errors(); eat()               ← 直接消费已有的错误 token（关键：见下方注释）
  否则若 after_error():
      （什么都不做）                      ← 前一个非 trivia 节点已是错误，去重
  否则:
      expected_at(before_trivia(), thing) ← 插入零长度 Error

expected_at(m, thing):
  在 nodes[m.0] 处插入空文本 Error 节点 "expected {thing}"
```

`unexpected` 与 `expected`（非错误 token 分支）有一个关键区别：`unexpected` **会消费**当前 token（它不该出现在这，吃掉才能让游标前进、避免死循环）；`expected` 的非错误分支**不消费**（当前 token 可能对更外层还有用，只在原位插零长标记）。

#### 4.3.3 源码精读

- [`fn expected`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L2008-L2028)：注意它对「当前已是错误 token」的特殊处理——**必须真正吃掉它**。源码注释用 `#import "str`（未闭合字符串）举例：若不在代码模式里消费掉这个错误 token，将来用户补上闭合引号时，增量重解析可能把它当成 Markup 文本词法化，导致「全量解析」与「增量解析」结果不一致。这是一条为增量重解析（U9）服务的隐藏不变量。
- [`fn expected_at`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L2036-L2041)：`SyntaxNode::error(eco_format!("expected {thing}"), "")` 造一个**空文本**错误节点并 `insert`，不消耗任何 token。
- [`fn after_error`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L2030-L2034)：检查「最近非 trivia 节点是否已是错误」，用于去重。
- [`fn unexpected`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L2051-L2057)：先 `trim_errors`、再维护 `balanced`、最后 `eat_and_get().unexpected()`。
- [`fn expect`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1982-L1995)：期望具体 `kind`。两个有意思的分支：若期望 `Ident` 却遇到关键字，会清错后用 `eat_and_get().expected(...)`（节点级 `expected` 还会自动补「关键字不能当标识符」的 hint）；否则 `balanced &= !kind.is_grouping()` 后调 `expected`。
- 节点侧：[`SyntaxNode::convert_to_error`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L491-L496) 只在「还不是错误」时转换（避免重复覆盖）并保留原文；[`SyntaxNode::expected`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L498-L510) 还会为「关键字当标识符/模式」自动补一条 hint。

#### 4.3.4 代码实践

**目标**：用故意写错的 `#let = ` 观察 `expected` 与 `after_error` 如何协作产出**恰好一个** Error 节点。

**操作步骤**（源码阅读型，可选用第 5 节程序验证）：

1. 跟踪 `#let = `：`#` 经 `embedded_code_expr` 进入 Code + `Stop`；`let_binding` 吃掉 `let`，期望绑定目标。
2. `let_binding` 先 `eat_if(Ident)`——当前是 `=`（`Eq`），不是 Ident；也不是 `(` 开头，于是走 `pattern` → [`pattern_leaf`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1428-L1463)。`Eq` 既非关键字也不在 `PATTERN_LEAF`（它等于 `ATOMIC_CODE_EXPR`，不含 `Eq`）里，于是调 [`expected("pattern")`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L2036-L2041) → 在 `=` 前插入零长 Error **`expected pattern`**。
3. 随后 `=` 被 `expect(Eq)` 正常吃掉，再 `code_expr` 期望右值却读到 `End`，本应再报 `expected expression`，但 [`after_error()`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L2030-L2034) 为真 → 去重，不再插第二个错误。

**需要观察的现象与预期结果**：`LetBinding` 子树内**只有一个** Error 节点，消息为 `expected pattern`（注意不是 `expected identifier`——因为 `let` 后接的不是标识符就是模式，`=` 走的是模式分支），且**没有 hints**（这条路径没有调 `hint()`）。错误数恰为 1，正是 `after_error` 去重的直接体现。

> 待本地验证：精确的 trivia 文本布局以本地解析输出为准；但「恰好一个零长 `expected pattern` Error、无 hint」是源码逻辑确定的结论。

#### 4.3.5 小练习与答案

**练习 1**：`unexpected` 里为什么要 `self.balanced &= !self.token.kind.is_grouping()`？

**答案**：`unexpected` 意味着当前 token 不该出现在此、要被当错误吃掉。若它恰好是分组分隔符（`([{` 或 `)]}`），说明定界符配对被打乱——这种结构不可信。把 `balanced` 置为 `false`（且只单向转假、永不恢复）后，增量重解析（U9）就能据此判断「定界符不均衡，不能信任局部重解析结果」。`is_grouping` 的定义见 [`kind.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L298-L309)。

**练习 2**：`expected` 在「当前 token 已是 Error」时为什么必须 `eat()` 而不是跳过？

**答案**：源码注释举 `#import "str`（未闭合字符串）为例：这个错误字符串 token 是在 Code 模式下词法化的。如果解析器不吃掉它就退出当前模式，等用户补上闭合引号做增量重解析时，那段文本可能落入 Markup 模式被当成普通文本，导致全量解析与增量解析结果不一致。吃掉它能保证 token 始终在「正确的模式」下被消费，维护增量重解析的正确性。

---

### 4.4 hint 修正建议与 trim_errors 噪音清理

#### 4.4.1 概念说明

光报「expected pattern」对用户不够友好——好的编译器会补一句「你可能想…」。typst 用两类机制产出**修正建议（hint）**，并配套一个**噪音清理**机制：

- **hint**：附加到「最近的那个错误/警告节点」上的字符串。它不是独立节点，而是 `Error`/`Warning` 节点里的一个 hints 列表。hint 可以来自两处：**词法阶段**（lexer 在造 Error token 时直接挂上，如 `##`）或**解析阶段**（parser 在出错后用 `p.hint(…)` 或 `node.hint(…)` 补上）。
- **trim_errors**：删掉**末尾连续的零长度 Error 节点**。因为 `expected_at` 会插入零长错误，若紧接着又 `unexpected`，旧的零长错误就成了多余噪音，需要先清掉再插新的。

注意 `hint` 是**找最近的错误节点贴上去**，所以调用顺序很重要：必须**先产生错误、再贴 hint**。这也是为什么 `unexpected` / `expected` 末尾常紧跟一两条 `p.hint(…)`。

#### 4.4.2 核心流程

```
hint(hint):
  m = before_trivia()
  取 nodes[m.0 - 1]（最近的非 trivia 节点）
  若它是 Error/Warning → 调 node.hint(hint) 追加到它的 hints 列表

trim_errors():
  end = before_trivia()
  从 end 往前回溯，凡是「is_error() 且 is_empty()」的连续节点都纳入删除区间
  drain 掉这段零长错误
```

`trim_errors` 的「零长度」判定依赖 [`SyntaxNode::is_empty`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L226-L228)（`len == 0`）与 [`SyntaxKind::is_error`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L381-L383)。它只清「空」错误，**保留有文本的错误**（如 `unexpected` 造的、带原文的错误节点）。它被三处调用，构成错误恢复的「标准前奏」：`expected`（吃错误 token 前）、`unexpected`（吃意外 token 前）、`expect`（关键字当标识符路径，吃之前）——模式很统一：**「要吃掉一个出问题的 token 之前，先 `trim_errors` 把零长噪音清掉」**。

#### 4.4.3 源码精读

- [`fn hint`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L2043-L2049)：定位到 `nodes[m.0 - 1]` 并对其 `error.hint(hint)`。注意它取「最近非 trivia 节点」，所以即便中间夹了空白 trivia 也能找对；若该节点不是 Error/Warning 会 panic，所以必须「刚报过错」才 hint。
- [`fn trim_errors`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L2059-L2070)：从 `before_trivia()` 起向前回溯，连续满足 `is_error() && is_empty()` 才删。
- 节点侧：[`SyntaxNode::hint`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L170-L173) 把字符串 push 进 `hints` 列表（仅 Error/Warning 节点有该列表）。
- **跨组件好例子——词法阶段就挂 hint**：[`Lexer::invalid_char_in_code`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L897-L912)。当 `#` 后紧跟另一个 `#`（`scout(-2)` 是 `#`）时，lexer 先 `error("the character `#` is not valid in code")`，再连挂两条 hint：「the preceding hash is causing this to parse in code mode」「try escaping the preceding hash: `\#`」。这些 hint 在词法阶段就随 Error token 产出，解析器 `expected("expression")` 看到 `is_error()` 后 `eat()` 消费它时，hint 原样保留。
- **解析阶段挂 hint 的例子**：[`code_exprs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L567-L573) 里，解析完一条语句后若既不在 stop_set、也没吃到分号，就 `expected("semicolon or line break")`；若当前是 `Label`，再追加两条 hint（「labels can only be applied in markup mode」「try wrapping your code in a markup block」）。这示范了「`expected` 报错 + `hint` 给出路」的标准组合。

最终这些 hint 如何被取出？[`SyntaxNode::errors_and_warnings`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L303-L327) 遍历整棵树，把每个 `Error`/`Warning` 节点连同其 hints 汇总成 `SyntaxDiagnostic` 列表（结构见 [`SyntaxDiagnostic`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L908-L918)，含 `is_error` / `message` / `hints` 等字段）。

#### 4.4.4 代码实践

**目标**：用 `##` 观察一个「带两条 hint」的错误节点，并对照项目自带测试确认输出。

**操作步骤**：

1. 打开 [`src/node.rs` 的 `test_debug` 测试](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1486-L1541)，其中 `parse("##")` 的预期 Debug 输出是权威样例：

   ```
   Markup: 2 [
       Hash: "#",
       Error: {
           text: "#",
           message: "the character `#` is not valid in code",
           hint: "the preceding hash is causing this to parse in code mode",
           hint: "try escaping the preceding hash: `\#`",
       },
   ]
   ```
2. 运行 `cargo test -p typst-syntax test_debug` 应当通过——这就是「带 hint 的错误节点」最权威的样例。
3. 同一测试里 `parse("**")` 给出的是「带 hint 的**警告**节点」（`Warning`：no text within stars）。

**需要观察的现象与预期结果**：Error 节点的 Debug 输出里直接内联了 `message` 与两条 `hint:` 行；用 `errors_and_warnings()` 收集时，该错误的 `hints` 字段长度为 2。此输出有项目自带断言背书，可信赖。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `hint()` 要在「最近的错误节点」上贴，而不是单独造一个 hint 节点？

**答案**：其一，hint 是错误/警告的「附属说明」，本身不对应源码区间，单独成节点会破坏「CST 覆盖全部源码文本」的无损不变量（每个 CST 节点都对应一段文本）。其二，诊断系统按错误节点聚合 hints，把 hint 挂在错误内部能让「一个错误 + 它的多条建议」天然成组。其三，`Node::Leaf/Inner` 没有 hints 列表（见 `hints_mut`），只有 `Error/Warning` 才有，所以 hint 必须依附于这两类节点。

**练习 2**：`trim_errors` 为什么只删「零长度」错误，而不删带文本的错误？

**答案**：`expected_at` 插入的是**零长度**补位错误（文本为空），它只起「在此位置标记缺失」的作用；一旦后续推进到下一个真实结构，这些补位错误就 redundant 了，删掉不影响 CST 对源码的覆盖。但 `unexpected` 造的错误节点**保留了原文文本**（`len > 0`），它真实对应一段写错的源码，删掉就会丢失源码信息、破坏无损性。所以 `trim_errors` 用 `is_empty()` 严格区分二者。

---

## 5. 综合实践

把本讲四块知识串起来：解析三段输入，分别观察「换行终止」「错误恢复去重」「带 hint 的错误」，并打印诊断。

**目标**：用一个最小程序同时验证 `AtNewline`（换行终止语句）、`expected` + `after_error`（错误去重）、`hint`（修正建议）三件事。

**示例代码**（依赖 `typst-syntax`，运行方式见下方说明）：

```rust
// 示例代码：综合观察换行终止、错误恢复与 hint
use typst_syntax::parse;

fn main() {
    for src in [
        "#let x = 1\n#let y = 2", // 换行正常终止：应得两条语句、无错误
        "#let = ",                 // 缺绑定目标：错误恢复 + after_error 去重
        "##",                      // 词法阶段就附带 2 条 hint 的错误
    ] {
        println!("\n========== source: {src:?} ==========");
        let root = parse(src);
        // Debug 格式会内联打印 Error / Warning 节点的 message 与 hint
        // （与 node.rs 中 test_debug 测试的输出格式一致）
        println!("{root:#?}");

        // errors_and_warnings 把整棵树的诊断分别收集
        let (errors, warnings) = root.errors_and_warnings();
        println!("--> errors: {}, warnings: {}", errors.len(), warnings.len());
        for e in &errors {
            println!("    ERROR: {} (hints: {})", e.message, e.hints.len());
        }
    }
}
```

**操作步骤与运行方式**：

1. **方式 A（推荐，最省事）**：在 typst 仓库根目录运行 `cargo test -p typst-syntax test_debug`，先把项目自带的权威 Debug 样例（含 `##`、`**`）跑通，确认你对 Debug 输出格式的认知（u1-l2 已建立这一运行方式）。
2. **方式 B**：在仓库外新建一个小 Rust 项目，把 `typst-syntax` 作为 path 依赖加入（参考 u1-l2：`typst-syntax = { path = "…/typst/crates/typst-syntax" }`），贴入上面的 `main`，`cargo run`。

**需要观察的现象与预期**：

- `#let x = 1\n#let y = 2`：根下两个 `LetBinding`，**0 错误 0 警告**——换行（`embedded_code_expr` 内的 `AtNewline::Stop`）干净地切分了语句。
- `#let = `：出现 `Error: "expected pattern"`，**错误数恰为 1**（`after_error` 去重生效），**hints 数为 0**（这条路径没挂 hint）。
- `##`：出现 `Error`，`message` 为 `the character `#` is not valid in code`，**hints 数为 2**——与 [`test_debug`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1514-L1527) 断言一致。

> 说明：前两段输入的**精确 trivia 文本布局**（如换行被渲染成哪种 Space 节点）建议本地实跑确认；`##` 的输出有项目自带测试背书，可信赖。

## 6. 本讲小结

- **换行的两重身份**：`\n` 既是可跳过的 trivia，又是语句分隔符与结构边界。`had_newline()` 驱动「行首标记」判定（u4-l3），`nl_mode`/`stop_at` 驱动「结构终止」，二者独立。
- **`AtNewline` 五变体 + `stop_at`** 是换行判定的唯一裁判：`Continue`（永不叫停）、`Stop`（恒叫停）、`ContextualContinue`（只放行 `else`/`.`）、`StopParBreak`（只对段落断叫停）、`RequireColumn(c)`（续行需列号 `> c`）。`column` 只在 Markup 有值。
- **伪造 `End` 机制**：`lex` 在 trivia 含换行时按当前 `nl_mode.stop_at(...)` 决定是否把当前 token 的 `kind` 改成 `End`（只改 kind、不改 `node`），让上层 `while !at_set(...)` 循环天然停止。
- **栈式管理 + 出栈复审**：`with_nl_mode` 把调用栈当模式栈，入栈切 `nl_mode`、出栈恢复；若当前 token 跨越换行且内外模式不同，用恢复后的 `nl_mode` 重跑 `stop_at`，决定保留伪造 `End` 还是还原真实 kind——保证「内层的叫停不误伤外层」。
- **错误恢复四件套**：`expected`（本该出现某物，插零长 Error 或吃掉错误 token）、`expected_at`（在指定位插零长 Error）、`unexpected`（吃掉意外 token 并原地转错）、`hint`（给紧邻尾部 Error 追加建议）。`after_error` 防错误堆叠，`balanced` 记录误吃定界符供 reparser 决策。解析器永不 panic。
- **`trim_errors` 去噪**：吃掉出问题 token 之前先删除 `nodes` 尾部所有「零长 Error」，避免占位错误堆叠；同时保证错误 token 在正确词法模式里被消费，维护「全量解析与增量重解析一致」。

## 7. 下一步学习建议

本讲结束后 Parser 单元（U4）完结，CST 的「构造侧」已讲透。建议按以下顺序继续：

1. **U5（CST 数据结构）**：本讲反复出现的 `Error` / `Warning` 节点在 `src/node.rs` 里的四种 `Node` 形态、`LinkedNode` 带父指针遍历，以及 `errors_and_warnings` 如何下钻 `WarningWrapper`——去 U5 把这些数据结构彻底读透；尤其 u5-l4 会讲清如何从 CST 收集 `SyntaxDiagnostic`，正好把本讲「待本地验证」的诊断来源补上。
2. **U6（Span 系统）**：`expected` 注释里提到的「增量重解析正确性」依赖 Span 的稳定性，U6 讲清 `numberize` 如何给每个节点（包括错误节点）盖编号。
3. **U9（增量重解析）**：本讲多次埋的伏笔——`balanced` 为何重要、`expected` 为何必须吃掉错误 token、`with_nl_mode` 退出复审——都是为增量重解析服务。学完 U9 再回看本讲会有更深的理解。
4. **动手延伸**：选一个现有 `unexpected()` 调用点，尝试给它补一条 `p.hint(…)`，运行第 5 节的程序观察 hint 是否正确贴到对应错误节点上，体会「先出错、再贴 hint」的调用顺序约束。

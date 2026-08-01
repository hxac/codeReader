# markup / block 重解析钩子

## 1. 本讲目标

上一讲（u9-l2）我们看清了 `try_reparse` 如何「自顶向下找到最内层包围编辑范围的节点」。但找到节点之后，**真正把那段文本重新解析成语法树**的工作，交给的是两个函数：`reparse_block` 与 `reparse_markup`。

本讲学完后，你应该能够：

1. 说清 `reparse_block` 与 `reparse_markup` 各自的输入、输出与成功条件。
2. 理解「Parser 复用」——增量重解析为何不另写小解析器，而是直接驱动完整 `Parser` 解析一段子串。
3. 掌握 `at_start` 与 `nesting` 两个状态在「跨重解析边界」时如何被合成、传递与校验。
4. 解释为什么这两个钩子都要求「定界符平衡」，以及为什么 Typst 当前不对列表/标题/math 做局部重解析。

## 2. 前置知识

本讲假设你已经掌握：

- **CST 与 `SyntaxNode`**（U5）：增量重解析直接替换 CST 的子节点。
- **Parser 的基本架构**（u4-l1、u4-l2）：`Parser::new`、`eat`、`marker`/`wrap`、单 token 前瞻。
- **markup 解析中的 `at_start`**（u4-l3）：行首状态决定 `=`/`-`/`+`/`/` 是结构标记还是普通文本。
- **`try_reparse` 的窗口策略**（u9-l2）：自顶向下找最内层包围节点、失败向外扩展、最终汇入全量兜底。

两个关键概念先点透：

- **钩子（hook）**：指 `parser.rs` 用 `pub(super)` 暴露给 `reparser.rs` 调用的两个函数。`reparser` 负责决定「重解析哪一段」，钩子负责「把这一段真正解析出来」。
- **平衡（balanced）**：`Parser` 内部一个只从 `true` 单向翻转为 `false` 的布尔字段，标记本次解析是否出现了「意外的或缺失的成对分隔符」。它是局部重解析能否成立的命根子。

## 3. 本讲源码地图

本讲只涉及两个文件：

| 文件 | 作用 |
| --- | --- |
| [parser.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs) | 完整解析器。定义两个钩子 `reparse_markup` / `reparse_block`，以及被它们复用的 `Parser::new`、`block`、`markup_expr` 等内部函数。 |
| [reparser.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs) | 增量重解析调度。`try_reparse` 在两条分支上调用这两个钩子，并负责合成 / 校验 `at_start` 与 `nesting`。 |

## 4. 核心概念与源码讲解

### 4.1 两条钩子的定位与 Parser 复用

#### 4.1.1 概念说明

增量重解析最朴素的实现是「重新解析全文」。`try_reparse` 的全部意义在于：**只重新解析一小段文本，然后把结果塞回原树**。但「解析一小段」并不需要写一套缩水版解析器——typst 直接复用完整的 `Parser`，秘诀在于 `Parser::new` 接受一个**起始偏移量** `offset`，让解析器从文本中间开始吃 token。

`reparser.rs` 在文件顶部就把两个钩子引进来，明确它们是「回调进完整解析器」的入口：

[reparser.rs:3-5](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L3-L5) —— 从 crate 根引入 `reparse_block` 与 `reparse_markup`（它们经 `parser.rs` 的 `pub(super)` 暴露）。

两条钩子的分工对应 `try_reparse` 的两条路径：

- **`reparse_block`**：处理「一个完整的块」——`[...]`（content block）或 `{...}`（code block）。输入是块的完整字节范围，输出是单个 `SyntaxNode`。
- **`reparse_markup`**：处理「一串连续的 markup 表达式」——顶层文档或某个 content block 内的若干兄弟表达式。输入是这段范围加上 `at_start` / `nesting` 上下文，输出是 `Vec<SyntaxNode>`（可能多个节点）。

#### 4.1.2 核心流程

```
try_reparse（u9-l2）找到最内层包围节点后分流：

  路径 A：单个 inner 子节点，且该子节点 is_block()
        └─ reparse_block(text, new_range) -> Option<SyntaxNode>
           成功 → replace_children 用新块替换旧块

  路径 B：当前节点是 Markup，且父节点是 根 或 ContentBlock
        └─ expand_and_reparse_markup(...)
             └─ reparse_markup(text, range, &mut at_start, &mut nesting, top_level)
                  -> Option<Vec<SyntaxNode>>
                成功 → replace_children 用新节点序列替换旧区间
```

两条钩子都返回 `Option`：`Some` 表示「这一段解析成功且定界符平衡」，`None` 表示失败，由上层向外扩展或最终回退到全量 `parse`。

#### 4.1.3 源码精读

`Parser::new` 的第三个参数 `offset` 是「Parser 复用」的关键——它让 Lexer 从 `offset` 处开始扫描，而不是从 0：

[parser.rs:1620-1636](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1620-L1636) —— `new(text, offset, mode)` 内部 `lexer.jump(offset)` 跳到子串起点，并立刻 `lex` 出第一个 token。这样钩子就能让一个全新 `Parser` 直接从文本中间开始工作，复用全部正常解析逻辑。

`try_reparse` 里两条分支的调用点：

- 块分支：[reparser.rs:99-108](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L99-L108) —— 仅当 `child.kind().is_block()` 时调用 `reparse_block`，成功后 `replace_children` 替换。
- markup 分支：[reparser.rs:111-125](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L111-L125) —— 仅当 `node_kind == Markup` 且父为 `None | ContentBlock` 时进入 `expand_and_reparse_markup`。

`is_block()` 只认两种节点，见 [kind.rs:324-326](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L324-L326)：`CodeBlock | ContentBlock`。这决定了 `reparse_block` 只可能被这两种块触发。

#### 4.1.4 代码实践

**实践目标**：跟踪一条编辑从 `try_reparse` 走到钩子的完整分支。

**操作步骤**：

1. 打开 [reparser.rs:55-126](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L55-L126) 的 `try_reparse`。
2. 找到第 68 行的 `if let [child]` 分支（路径 A）和第 111 行的 `if node_kind == SyntaxKind::Markup` 分支（路径 B）。
3. 对照上文的「核心流程」伪代码，标注哪一行调 `reparse_block`、哪一行（间接）调 `reparse_markup`。

**需要观察的现象**：两条路径互斥——一个 `Markup` 节点即使只有一个子节点，也走路径 B（markup），因为它的子节点不是 `is_block()`；一个 `ContentBlock` 才会触发路径 A。

#### 4.1.5 小练习与答案

**练习 1**：为什么钩子要做成 `pub(super)` 而不是 `pub`？

> **答案**：它们是 `parser` 与 `reparser` 两个模块之间的内部契约，外部用户只该用 `Source::edit`（u9-l1），不该直接调钩子。`pub(super)` 把可见性限制在父模块 `syntax` 内，避免泄漏实现细节。

**练习 2**：`reparse_block` 返回 `Option<SyntaxNode>`，`reparse_markup` 返回 `Option<Vec<SyntaxNode>>`，为什么类型不同？

> **答案**：块本身是一个完整节点（`ContentBlock`/`CodeBlock`），重解析后仍是单个节点；而 markup 重解析的是「一段兄弟表达式序列」，可能产生零到多个顶层表达式节点，所以是 `Vec`。

---

### 4.2 reparse_block：整块重解析钩子

#### 4.2.1 概念说明

当编辑发生在某个 `ContentBlock` `[...]` 或 `CodeBlock` `{...}` **内部**，且这个块本身能完整包住编辑范围时，`try_reparse` 选择整块重解析：把这块对应的原文区间重新喂给 `Parser`，得到一棵新的子树，替换旧块。

`reparse_block` 的契约很硬：

- **调用方保证**：`range.start` 处必须是 `[` 或 `{`（块的左定界符）。
- **成功条件**：解析后定界符平衡，且解析恰好消费到 `range.end`（既不多不少）。

#### 4.2.2 核心流程

```
reparse_block(text, range):
  1. p = Parser::new(text, range.start, Code)   // 从块起点开始，起步用 Code 模式
  2. assert 当前 token 是 LeftBracket 或 LeftBrace   // 调用方承诺
  3. block(&mut p)                               // 复用普通块解析（内部会切模式）
  4. 若 p.balanced && p.prev_end() == range.end:
        返回 Some(p.finish().into_iter().next().unwrap())   // 唯一根节点
     否则返回 None
```

注意第 3 步：`block()` 是 parser 平时解析块时用的**同一个函数**，会根据左定界符分派到 `content_block`（切到 Markup 模式）或 `code_block`（Code 模式）。这就是「Parser 复用」最直接的体现——增量路径与全量路径走完全相同的解析代码。

#### 4.2.3 源码精读

钩子本体只有 7 行：

[parser.rs:747-755](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L747-L755) —— `reparse_block`。`assert!` 校验起点是 `[` 或 `{`；解析后用 `p.balanced && p.prev_end() == range.end` 判定成功。

`block` 及其两个分派函数：

[parser.rs:757-786](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L757-L786) —— `block` 按 `LeftBracket`/`LeftBrace` 分派到 `content_block` / `code_block`，各自 `enter_modes` 切换语法模式并用 `expect_closing_delimiter` 吃右定界符。

`balanced` 字段的定义与「只降不升」语义：

[parser.rs:1513-1515](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1513-L1515) —— 注释明确「only ever transitions from `true` to `false`」。它只在两处被翻转：期望某个分隔符却没吃到（[parser.rs:1991](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1991) `self.balanced &= !kind.is_grouping();`），或意外吃到一个分隔符（[parser.rs:2055](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L2055)）。

`prev_end` 的含义：

[parser.rs:1711-1714](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1711-L1714) —— 返回「上一个被吃掉的 token 的结束位置」。`block()` 解析含右定界符，所以解析完块后 `prev_end()` 应正好等于块的字节末尾 `range.end`。

#### 4.2.4 代码实践

**实践目标**：理解 `reparse_block` 的成功条件，并预测一次失败回退。

**操作步骤**：

1. 阅读 [reparser.rs:433-455](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L433-L455) 的 `test_reparse_block`。
2. 重点看这一条：`test("#{}}", Edit::After("{"), "{", All);`。原文 `#{}}`（嵌入式 code block `#{}` 后跟一个多余的 `}`），在第一个 `{` 之后插入 `{`，得到 `#{{}}`。
3. 推理：插入的 `{` 在 code block 内部开启了一个新的嵌套块，原来的那个 `}` 被内层吃掉，外层块失去闭合 → `balanced` 变 `false` → `reparse_block` 返回 `None` → 回退全量，因此预期是 `All`。

**需要观察的现象**：凡是把 `All` 的用例挑出来，它们几乎都是「编辑改变了定界符配对」的场景；而 `Incr` 用例（如 `test("Hello #{ x + 1 }!", Edit::Match("x"), "abc", Incr("{ abc + 1 }"))`）都是在块内部做不触及定界符配对的改动。

**预期结果**：运行 `cargo test -p typst-syntax test_reparse_block` 全部通过；你的推理与断言一致。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `reparse_block` 用 `prev_end()` 而不是 `current_start()` 来比对 `range.end`？

> **答案**：`block()` 解析完右定界符后，Parser 已经向前 `lex` 出了块之后的下一个 token（但没吃）。`prev_end()` 指向「最后吃掉的 token」（即右定界符）的末尾，正好是块末尾；而 `current_start()` 指向块之后那个未吃 token 的起点，会比 `range.end` 大。所以这里必须用 `prev_end()`。

**练习 2**：若 `range` 起点不是 `[` 或 `{`，`reparse_block` 会怎样？

> **答案**：第 751 行的 `assert!` 会触发 panic。这是「调用方契约」——`try_reparse` 在第 99 行用 `child.kind().is_block()` 保证了只有真正的块才进来，所以起点必然是左定界符。assert 是防御性校验，正常路径不会触发。

---

### 4.3 reparse_markup：markup 表达式序列重解析钩子

#### 4.3.1 概念说明

当编辑发生在**顶层文档**或某个 **content block 内部的 markup**，且没有更内层的块能包住它时，`try_reparse` 走 markup 路径：把「受影响的一段兄弟 markup 表达式」重新解析。

与 `reparse_block` 不同，这里重解析的不是「一个节点」而是「一串表达式」。因此返回 `Vec<SyntaxNode>`，并且因为 markup 解析对「行首状态」和「内容块嵌套」敏感，钩子需要额外的 `at_start` 与 `nesting` 上下文（4.4 节详解）。

#### 4.3.2 核心流程

```
reparse_markup(text, range, &mut at_start, &mut nesting, top_level):
  1. p = Parser::new(text, range.start, Markup)   // 从段起点开始，Markup 模式
  2. *at_start |= p.had_newline()                 // 把段首换行并入 at_start
  3. 循环，只要 没到 end 且 当前 token 起点 < range.end:
       a. 若 非顶层 && nesting==0 && 当前是 RightBracket: break  // 这个 ] 属于外层，别吃
       b. markup_expr(p, at_start, nesting)       // 复用单个表达式解析
       c. at_start = p.had_newline()
  4. 若 p.balanced && p.current_start() == range.end:
        返回 Some(p.finish())                     // Vec<SyntaxNode>
     否则返回 None
```

注意循环条件用 `current_start() < range.end`：只要当前 token 的**起点**还没越过段尾，就继续解析。退出循环后，要求**下一个未吃 token 的起点正好等于 `range.end`**，即段尾必须落在 token 边界上。

#### 4.3.3 源码精读

钩子本体：

[parser.rs:64-82](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L64-L82) —— `reparse_markup`。它直接复用 `markup_expr`（[parser.rs:87-134](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L87-L134)），而 `markup_expr` 正是平时解析 markup 表达式用的同一个函数。

把它和正常路径的 `markup_exprs` 对比，能看出它「省略了什么、保留了什么」：

[parser.rs:50-61](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L50-L61) —— 正常 `markup_exprs` 用 `at_set(stop_set)` 判断停止；而 `reparse_markup` 改用「字节范围 `range.end`」作为停止线。这是因为增量重解析的边界是**字节位置**，不是某个 token kind。

调用点在 `expand_and_reparse_markup`：

[reparser.rs:191-214](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L191-L214) —— 拿到 `reparse_markup` 返回的节点序列后，还要再校验段尾的 `at_start` / `nesting` 是否与「假定这段没被改动」时一致（4.4 节）。校验通过才 `replace_children(start..end, newborns)` 替换。

> **边界规则的差异**：`reparse_block` 用 `prev_end()`，`reparse_markup` 用 `current_start()`。前者解析「一个完整节点」，要用最后吃掉的 token 末尾对齐；后者解析「一串表达式序列」，要用下一个未吃 token 的起点对齐。这正是实践任务要对比的核心。

#### 4.3.4 代码实践

**实践目标**：对比两条钩子的成功条件表达式。

**操作步骤**：

1. 打开 `reparse_block`（[parser.rs:753-754](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L753-L754)）和 `reparse_markup`（[parser.rs:81](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L81)）。
2. 列出两者的成功条件：
   - block：`p.balanced && p.prev_end() == range.end`
   - markup：`p.balanced && p.current_start() == range.end`
3. 共同点是 `p.balanced`——两者都要求定界符平衡。

**需要观察的现象**：两条钩子的成功条件都把 `balanced` 放在第一位。这就是「为何要求定界符平衡」的直接证据：局部重解析假设**窗口之外的定界符配对不受影响**；若窗口内出现改变全局配对的分隔符（多了一个 `}`、少了一个 `]`……），`balanced` 翻成 `false`，钩子返回 `None`，交给上层向外扩展或全量回退。

**预期结果**：你能用自己的话讲清「`balanced` 失败 → 钩子返回 None → try_reparse 向外扩展 → 最终全量」这条链路。

#### 4.3.5 小练习与答案

**练习 1**：`reparse_markup` 第 75 行 `if !top_level && *nesting == 0 && p.at(RightBracket) { break; }` 的作用是什么？

> **答案**：当重解析发生在某个 content block 内部（非顶层），解析到属于**外层块**的右括号 `]` 时必须停下——这个 `]` 不是本段要吃的内容。`nesting == 0` 表示当前没有未闭合的内层 `[`，所以这个 `]` 必然属于外层。停在这里让 `current_start()` 正好落在 `range.end` 上，保证成功条件成立。

**练习 2**：为什么 markup 路径需要 `expand_and_reparse_markup` 做「指数扩展」，而 block 路径不需要？

> **答案**：块是一个完整节点，边界明确（左定界符到右定界符），一次重解析要么成功要么失败。而 markup 表达式序列的「正确边界」事先不知道——段首段尾可能正好把一个表达式切成两半。所以需要向外指数扩展（`expansion *= 2`，见 [reparser.rs:222](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L222)），直到找到一组能让解析恰好对齐的兄弟节点。

---

### 4.4 at_start 与 nesting：跨边界的状态一致性

#### 4.4.1 概念说明

markup 解析有两个「上下文敏感」的状态：

- **`at_start`**：是否处于行/块的起始位置。它决定 `=`/`-`/`+`/`/` 被解释成标题/列表/枚举/术语标记，还是普通文本（见 u4-l3）。
- **`nesting`**：内容块 `[ ]` 的嵌套深度。它决定 `]` 是闭合当前 content block（应停止），还是被当成文本（markup 模式下成对的 `[` `]` 文本，见 [parser.rs:91-102](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L91-L102)）。

问题来了：`reparse_markup` 只解析**一段子串**，但这段在原文中的「开头状态」取决于它**之前**的所有兄弟节点。如果重解析时给出的 `at_start` / `nesting` 与真实情况不符，解析结果就会和全量解析不一致——这违背了「增量结果必须等于全量结果」的铁律（见 `test` 辅助函数的断言 [reparser.rs:360](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L360)）。

因此 `reparser` 必须：在调用钩子**前**合成段首状态，在调用钩子**后**校验段尾状态。

#### 4.4.2 核心流程

```
expand_and_reparse_markup 对每一次尝试：

  合成（synthesizing）段首状态：
    遍历重解析范围 start 之前的所有兄弟节点：
      prefix_len += child.len()
      next_at_start(child, &mut at_start)    // 推算段首 at_start
      next_nesting(child, &mut nesting)      // 推算段首 nesting

  计算「假设这段没被改动」时段尾应有的状态：
    遍历 children[start..end]，用同样的 next_at_start / next_nesting
    得到 prev_at_start_after 与 prev_nesting_after

  调用 reparse_markup(text, new_range, &mut at_start, &mut nesting, top_level)
  （钩子内部会随解析推进更新 at_start / nesting）

  校验段尾一致性：
    (at_end || at_start == prev_at_start_after)
    && ((at_end && top_level) || nesting == prev_nesting_after)
    → 通过才 replace_children
```

#### 4.4.3 源码精读

合成与校验的完整段落：

[reparser.rs:164-214](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L164-L214) —— 先用 `[..start]` 段合成段首的 `prefix_len` / `nesting` / `at_start`（164-173 行），再算出段尾的期望值 `prev_at_start_after` / `prev_nesting_after`（176-183 行），调用钩子后做一致性校验（204-206 行）。

两个状态推算函数：

[reparser.rs:274-282](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L274-L282) —— `next_at_start`：trivia 中的 `Parbreak` 或含换行的 `Space` 会把 `at_start` 置真；任何非 trivia 节点都把它置假。

[reparser.rs:285-293](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L285-L293) —— `next_nesting`：文本节点 `[` 让 `nesting +1`，`]`（当 `nesting > 0`）让 `nesting -1`。注意它只看 `Text` 节点里的字面 `[` `]`，因为这些在 markup 模式下被切成文本。

校验条件的两个分支（[reparser.rs:204-206](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L204-L206)）：

- `at_end || at_start == prev_at_start_after`：如果重解析一直吃到父节点末尾（`at_end`），后面没有兄弟节点，段尾 `at_start` 不影响任何人，免校验；否则必须与期望一致。
- `(at_end && top_level) || nesting == prev_nesting_after`：顶层且吃到末尾时，`nesting` 也不影响后续；否则必须一致。这就是为什么 `top_level` 标志存在——它放宽了顶层的校验。

#### 4.4.4 代码实践

**实践目标**：用一条测试用例体会「`at_start` 不一致导致扩展」。

**操作步骤**：

1. 看 `test_reparse_markup` 中的 `test("\n= A heading", Edit::After("A"), "n evocative", All);`（[reparser.rs:412](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L412)）。
2. 原文 `\n= A heading`，在 `A` 后插入 `n evocative` 得到 `\n= An evocative heading`。这次编辑在标题内部（`Heading` 节点），而当前实现**不对标题内部做局部重解析**（见 4.5），且改动跨越了影响 `at_start` 的边界，最终回退全量，预期 `All`。

**需要观察的现象**：把用例改成在顶层文本上做类似编辑，比如 `test("abc~def~gh~", ...)`（[reparser.rs:395](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L395)）得到 `Incr`。对比可见：顶层纯文本编辑能增量成功，而触及标题内部或 `at_start` 语义的编辑会回退。

**预期结果**：理解「段首/段尾的 `at_start` 与 `nesting` 必须与全量解析时一致，否则这一次尝试失败、向外扩展」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `next_nesting` 只对 `Text` 节点里的 `[` `]` 计数，而不看 `LeftBracket`/`RightBracket` token？

> **答案**：在 markup 模式下，字面的 `[` `]` 若不构成 content block，会被 lexer 切成 `Text` 节点（见 `markup_expr` 第 91-98 行对 `LeftBracket`/`RightBracket` 的处理：嵌套时 `convert_and_eat(Text)`）。真正的 content block 用 `ContentBlock` 节点表示，其边界由解析器在更外层配对，不会出现在待合成的兄弟序列里。所以兄弟序列里能影响 `nesting` 的只有被当文本吃的 `[` `]`。

**练习 2**：校验条件里为什么对 `top_level` 有特殊豁免？

> **答案**：顶层文档（父为根 `Markup`）不存在「外层 content block 的 `]`」会来吃的问题，且若重解析吃到文档末尾（`at_end`），后续没有任何兄弟节点会依赖 `nesting`。所以在 `at_end && top_level` 时免去 `nesting` 校验，避免误杀合法的顶层增量。

---

### 4.5 当前覆盖范围的取舍：为什么不重解析列表/标题/math

本节不是独立的最小模块，而是回答本讲 topic 的最后一个问题：当前的增量重解析**只覆盖哪些场景**，又**刻意不覆盖**哪些。

#### 4.5.1 覆盖范围

综合 4.1–4.4，当前两条钩子覆盖的场景是：

1. 编辑完全落在某个 `CodeBlock` / `ContentBlock` **内部** → `reparse_block`。
2. 编辑落在**顶层文档**或某个 **content block 内部的 markup 表达式序列** → `reparse_markup`。

#### 4.5.2 刻意不覆盖的场景

`try_reparse` 的文档注释坦白说明了取舍：

[reparser.rs:43-54](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L43-L54) —— 两段关键说明：

- **不对列表项/标题等内部的 markup 做局部重解析**。注释解释：过去曾经支持，但因「缩进与换行的边界情况」导致实现非常容易出 bug，最终被移除，且实测性能影响很小。代码层面这正是 4.4 里 `at_start` / `RequireColumn`（列对齐）语义复杂性的体现——列表项靠列号判定续行，子串重解析很难正确合成这些状态。
- **完全不对 math 做局部重解析**。注释指出这「并不太难」，可以作为另一种块来处理，但目前没做。

这层取舍在 `try_reparse` 的分派条件里被强制执行：

[reparser.rs:111-113](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L111-L113) —— 只有 `node_kind == Markup && parent_kind ∈ {None, ContentBlock}` 才进 markup 路径。一个 `Heading` 或 `ListItem` 内部的 `Markup` 子节点，其父既不是根也不是 `ContentBlock`，于是落到第 123-125 行直接返回 `None`，向上回退。

#### 4.5.3 代码实践

**实践目标**：验证「标题内部的编辑不会触发局部重解析」。

**操作步骤**：

1. 阅读 [reparser.rs:412](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L412) 的 `test("\n= A heading", Edit::After("A"), "n evocative", All);`，确认它预期 `All`（全量回退）。
2. 推理调用链：编辑点在 `Heading` 节点内的 `Markup` 子节点里；`try_reparse` 下钻到该 `Markup` 时，`parent_kind` 是 `Some(Heading)`，不满足第 112 行的 `None | ContentBlock`，于是返回 `None`；逐层向上回退，直到根走全量。

**需要观察的现象**：把这条用例的预期从 `All` 改成 `Incr(...)` 运行 `cargo test`，测试会失败——因为当前实现确实会全量回退。这反向印证了覆盖范围的取舍。

**预期结果**：保持原 `All` 断言时测试通过；理解「父节点类型不匹配 → 不进 markup 路径 → 回退」是取舍的代码落点。

## 5. 综合实践

设计一个贯穿本讲的小任务：**自己写一个增量重解析探测函数，预测一次编辑是增量成功还是全量回退，并解释原因。**

下面这段代码复刻了 `reparser.rs` 测试模块里 `test` 辅助函数的核心思想（[reparser.rs:351-374](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L351-L374)），你可以把它放进一个依赖 `typst-syntax` 的小程序，或直接抄进 `reparser.rs` 的 `#[cfg(test)] mod tests` 里调用。

```rust
// 示例代码：模仿 reparser::tests::test 的核心断言
use typst_syntax::{Source, Span, parse};

/// 对 text 做「把 replace 范围替换成 with」的编辑，
/// 返回 true 表示增量重解析成功（重解析范围 < 全文），false 表示全量回退。
fn is_incremental(text: &str, replace: std::ops::Range<usize>, with: &str) -> bool {
    let mut src = Source::detached(text);
    let replaced = src.edit(replace, with);

    // 铁律：增量重解析的结果必须与全量解析完全一致
    let mut reparsed = src.root().clone();
    let mut full = parse(src.text());
    reparsed.synthesize(Span::detached());
    full.synthesize(Span::detached());
    assert_eq!(reparsed, full, "增量结果与全量结果不一致！");

    // 重解析范围等于全文 → 全量回退；否则 → 增量成功
    replaced != (0..src.text().len())
}

fn main() {
    // 用例 A：content block 内部、不触及定界符配对的改动 → 增量
    println!("A: {}", is_incremental("Hello #{ x + 1 }!", 9..10, "abc")); // 替换 "x"

    // 用例 B：在 code block 内插入 { 破坏定界符配对 → 全量回退
    println!("B: {}", is_incremental("#{}", 2..2, "{"));
}
```

**任务步骤**：

1. 运行这段程序（或在 `reparser.rs` 的 `tests` 模块里加两条 `assert!` 断言并 `cargo test -p typst-syntax`）。
2. 对每个用例，先用本讲学到的规则预测结果（true/false），再与实际输出对照。
3. 为用例 B 写出失败链路：`reparse_block` 内 `balanced` 变 `false` → 返回 `None` → `try_reparse` 向外扩展 → 全量回退。

**需要观察的现象**：

- 用例 A 中，`replaced` 是 `"{ abc + 1 }"` 这一小段（远小于全文），`is_incremental` 返回 `true`。
- 用例 B 中，`replaced` 等于 `0..src.text().len()`（全文），`is_incremental` 返回 `false`。

**预期结果**：你的预测与实际一致；能完整复述「定界符平衡 → 钩子成功；失衡 → 回退全量」这条主线。

> 若你无法本地运行，可改为阅读 `test_reparse_block`（[reparser.rs:433-455](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L433-L455)）与 `test_reparse_markup`（[reparser.rs:391-429](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L391-L429)），把每条用例按 `Incr` / `All` 分类，并标注触发哪条钩子或为何回退——效果等价。

## 6. 本讲小结

- `reparse_block` 与 `reparse_markup` 是 `parser.rs` 暴露给 `reparser.rs` 的两个 `pub(super)` 钩子，分别处理「整块重解析」与「markup 表达式序列重解析」。
- 两条钩子的本质都是 **Parser 复用**：通过 `Parser::new(text, offset, mode)` 让完整解析器从子串中点开始工作，复用 `block` / `markup_expr` 等正常解析函数。
- 成功条件都以 `p.balanced` 为前提：block 用 `prev_end() == range.end`，markup 用 `current_start() == range.end`——前者对齐「最后吃掉的 token 末尾」，后者对齐「下一个未吃 token 起点」。
- **定界符必须平衡**：`balanced` 是只降不升的布尔位，任何意外/缺失的成对分隔符都会让它翻 `false`，钩子返回 `None`，从而触发向外扩展或全量回退——这是局部重解析正确性的命根子。
- `at_start` 与 `nesting` 是 markup 解析的上下文状态，`expand_and_reparse_markup` 必须在调用钩子前**合成**段首状态、调用后**校验**段尾状态，确保增量结果与全量一致。
- 当前覆盖范围刻意不含列表项/标题内部的 markup（缩进换行边界太易错）与 math（未实现）；`try_reparse` 用 `parent_kind ∈ {None, ContentBlock}` 这一分派条件强制执行该取舍。

## 7. 下一步学习建议

至此 U9「增量重解析」单元结束。建议下一步：

1. **回头贯通整条增量链路**：按 `Source::edit`（u9-l1）→ `reparse` 调度 → `try_reparse` 找节点（u9-l2）→ `reparse_block` / `reparse_markup` 钩子（本讲）→ `replace_children` 重编号（u6-l2 的 `upper` 字段）的顺序，自顶向下走一遍，确认每一步的输入输出都能接上。
2. **进入 U10**：本讲多次提到 `FileId` 与 `Span`，下一单元 u10-l1 会讲 `FileId` 的全局驻留（interning），讲清 `Span` 高 16 位的来源。
3. **动手扩展（可选）**：如果你对 4.5 提到的「math 局部重解析」感兴趣，可尝试仿照 `reparse_block` 写一个 `reparse_math`，关注其成功条件与 `balanced` 校验——这是检验你是否真正理解本讲的最好练习。

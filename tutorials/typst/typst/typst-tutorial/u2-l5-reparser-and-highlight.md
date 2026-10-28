# 增量解析与语法高亮（u2-l5）

## 1. 本讲目标

本讲是「语法层」单元的收官篇。学完后你应该能够：

1. 说出 `Source::edit` 触发的增量重解析的整体策略：**增量优先，增量替换成功但编号失败时只在根节点重新 `numberize`，只有增量替换本身失败才整树重解析**。
2. 跟踪 `try_reparse_and_renumber` 的下钻过程：如何用 `overlapping_children` 定位重叠子节点、何时递归下钻、何时用 `reparse_block` 重解析整个块、何时走 `expand_and_reparse_markup` 的指数扩展，以及 `MarkupReparse` 结构如何统一封装「替换哪些孩子」与「重解析了哪段文本」。
3. 解释 `replace_children` / `renumber_children` / `update_parent` 这三个方法为什么从 `node.rs` 迁入 `reparser.rs`（它们只服务于增量重解析），以及 `replace_children` 如何通过修剪公共前后缀来保住兄弟节点的 span 编号。
4. 理解「不在语法警告（Warning 节点）内部重解析」这条安全性约束：警告的消息、提示与子区间可能引用被替换文本的任意子集，局部重解析后无法可靠地更新它们。
5. 用 `highlight.rs` 的 `Tag` 体系为一棵语法树着色，并说出它与增量重解析如何配合支撑编辑器场景。

## 2. 前置知识

本讲假设你已读过 u2-l3（Parser 与 SyntaxNode）与 u2-l4（AST 与 Span）。在此基础上补充三个概念：

- **增量计算与缓存友好**。Typst 的编译性能高度依赖 comemo 记忆化（见 u3-l1、u5-l8）：如果一次编辑导致整棵语法树重建，树上所有节点的 `Span` 都会变化，下游所有以 `Span` 为 key 的缓存会全部失效。增量重解析的目标不只是「少解析几个字符」，更是「让没被编辑波及的节点保持原对象、原 span」。
- **写时复制（copy-on-write）**。语法树内部用 `Arc<InnerNode>` 共享。`Arc::make_mut` 在引用计数为 1 时原地修改，否则克隆一份再改。增量重解析只触碰被替换路径上的节点，其余子树继续共享，所以「只改局部」同时意味着「克隆成本也局部」。
- **trivia（琐碎节点）与 at_start / nesting**。空格、换行、注释等 token 是 trivia；`at_start` 表示「是否处于行首」（决定 `= 标题`、`- 列表` 是否生效）；`nesting` 表示内容块 `[` `]` 的嵌套深度。这三个状态在增量重解析时必须在重解析起点被**人工复原**，终点被**校验一致**，这是本讲最难也最精彩的部分。

回顾一个关键不变量（u2-l4 已讲）：span 编号满足「父小于所有子孙、兄弟从左到右递增」。`LinkedNode::find_number` 正是靠它做二分式剪枝查找。增量重解析的难点在于：替换一段孩子后，新孩子必须**塞进左右兄弟编号之间的空隙里**，塞不下时就要重新编号。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/typst-syntax/src/source.rs` | `Source::edit` / `Source::replace`：公开 API，先改文本和行表，再调 `reparse` |
| `crates/typst-syntax/src/reparser.rs` | 增量重解析的全部核心逻辑（本讲主角），以及从 `node.rs` 迁入的 `InnerNode` 三个修改方法 |
| `crates/typst-syntax/src/lines.rs` | `Lines`：行起点表的增量维护（UTF-8 / UTF-16 / 行列换算） |
| `crates/typst-syntax/src/node.rs` | `SyntaxNode` / `Node` 四形态、`InnerNode`、`numberize` 两级签名、`reparse_inner_node_mut`（不下钻警告） |
| `crates/typst-syntax/src/parser.rs` | 只借用两个入口：`reparse_markup`（重解析一段标记表达式）与 `reparse_block`（重解析一个完整的代码/内容块） |
| `crates/typst-syntax/src/highlight.rs` | `Tag` 高亮标签体系、`highlight` / `highlight_html` |

## 4. 核心概念与源码讲解

### 4.1 reparse 入口：增量优先、编号失败只重编号

#### 4.1.1 概念说明

用户在编辑器里每敲一个字符，都会产生一次「把旧文本某区间替换成新串」的编辑。最朴素的响应是重新 `parse` 全文——对一个几百 KB 的文档来说，每个按键都全量重解析太浪费。增量重解析（incremental reparsing）的思路是：**语法树本身就是无损的（CST），树上的每个子节点都对应文本的一段连续区间；如果编辑只落在某个子节点内部，就只需要重新解析那个子节点对应的区间，其余子树原样保留。**

但「替换成功」和「编号成功」是两件事。替换成功指新解析出的孩子能合法放回树里；编号成功指能给新孩子分配到满足有序性的 span 编号。旧实现里，深层编号失败会放弃整次增量重解析、退回全量重解析；重构后的策略是**把这两步解耦**：只要替换成功，树就保留，编号失败时先在局部扩展重试，仍不行再从根重新编号——重新编号只是遍历赋号，远比重新解析便宜。这正是「修复嵌套重解析」的那次改动（git 提交 `d8f8d4df1`）。

#### 4.1.2 核心流程

`Source::edit(replace, with)` 的完整路径：

```text
Source::edit(replace, with)
 ├─ Arc::make_mut 写时复制出新的 SourceInner
 ├─ lines.edit(replace, with)        # 更新文本 + 增量维护行起点表
 └─ reparse(&mut root, 新文本, replace, with.len())
      ├─ root.reparse_inner_node_mut()          # 根必须是 Inner（且不能被警告包裹）
      ├─ try_reparse_and_renumber(...)          # 深度优先下钻，见 4.2
      │    └─ 成功 → 返回 (重解析区间, NumberingResult)
      ├─ 增量成功: reparsed_range = 返回区间; renumber = 编号是否失败
      ├─ 增量失败: *root = parse(text) 全量重建; renumber = true
      └─ renumber && 根有 id → root.numberize(id, Span::FULL)  # 全树重新编号
```

#### 4.1.3 源码精读

先看公开入口。`Source::edit` 先改行表，再把剩余工作全部交给 `reparse`：[crates/typst-syntax/src/source.rs:L90-L104](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/source.rs#L90-L104)——这里 `Arc::make_mut(&mut self.0)` 保证克隆 `Source` 近乎零成本、只有真正编辑时才复制内部状态（呼应 u2-l1 讲过的 `Arc<LazyHash<SourceInner>>`）。`Source::new` 则是全量路径：`parse` + 一次性 `numberize(id, Span::FULL)`，见 [crates/typst-syntax/src/source.rs:L31-L41](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/source.rs#L31-L41)。

再看本讲主角 `reparse`：[crates/typst-syntax/src/reparser.rs:L16-L41](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/reparser.rs#L16-L41)。这段代码只有 20 多行，但浓缩了整个新策略：

```rust
let mut renumber = true;
let mut reparsed_range = 0..text.len();
if let Some(inner) = root.reparse_inner_node_mut()
    && let Some((range, num_result)) =
        try_reparse_and_renumber(text, replaced, replacement_len, None, inner, 0)
{
    reparsed_range = range;
    renumber = num_result.is_err();   // 关键：替换成功但编号失败，只置 renumber
} else {
    *root = parse(text);              // 增量彻底失败才全量重建
}

if renumber && let Some(id) = root_id {
    root.numberize(id, Span::FULL).unwrap();  // 从根重新编号，而不是重新解析
}
```

三个细节值得咀嚼：

- `renumber = num_result.is_err()`：`NumberingResult` 是 `Result<(), Unnumberable>`（定义在 [crates/typst-syntax/src/node.rs:L1366-L1379](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/node.rs#L1366-L1379)）。编号失败时树已经被增量替换好了，此时只补一次全树编号——代价是这棵树上所有 span 都会变（缓存失效），但省掉了重新解析。
- `Span::FULL` 是编号的总量程 \(2..2^{47}\)（48 位编号空间的左半部分），见 [crates/typst-syntax/src/span.rs:L87](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/span.rs#L87)。
- `root_id` 取自编辑前的根 span。若根是 detached（没有编号），则跳过编号步骤。

行表的增量维护在 `Lines::edit`：[crates/typst-syntax/src/lines.rs:L199-L229](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/lines.rs#L199-L229)。它的做法是「截断 + 重算尾部」：先把编辑起点所在行之后的行起点全部丢弃（`truncate(line + 1)`），再从编辑起点用 `lines_from` 重新扫描。特别注意 `\r` 与 `\n` 拼接的边界情形——编辑可能把原本分离的 `\r` 和 `\n` 粘成一个 `\r\n`（此时要 `pop` 掉多出的一行起点）。行扫描器 `lines_from` 本身在 [crates/typst-syntax/src/lines.rs:L251-L276](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/lines.rs#L251-L276)，它同时维护字节偏移和 UTF-16 偏移（LSP 协议用 UTF-16，这是 `Line` 结构存两个索引的原因）。

#### 4.1.4 代码实践

运行真实测试来观察「增量 vs 全量」的分类标准。测试助手 `test` 把返回的重解析区间分为两类：等于整个新文本记为 `All`，否则记为 `Incr(实际重解析的文本)`，见 [crates/typst-syntax/src/reparser.rs:L547-L572](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/reparser.rs#L547-L572)。它同时断言「增量树 == 全量树（忽略 span）」，即 `spanless_eq(&normal_parse)`，这是增量重解析的正确性契约：**无论走哪条路，得到的树必须与全量解析完全一致（除编号外）**。

```bash
cargo test -p typst-syntax --lib reparse
```

预期结果：`test_reparse_basic`、`test_reparse_markup`、`test_reparse_block`、`test_reparse_unclosed_embedded`、`test_reparse_syntax_warnings`、`test_reparse_nested_code`、`test_reparse_nested_markup`、`test_reparse_within_warnings` 全部通过。注意看 `test_reparse_nested_code`（[crates/typst-syntax/src/reparser.rs:L693-L721](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/reparser.rs#L693-L721)）：在 127 层嵌套块里做小编辑，期望仍是 `Incr`——这正是「编号失败不再退回全量重解析」修复的直接受益者。

#### 4.1.5 小练习与答案

**练习 1**：如果 `try_reparse_and_renumber` 返回 `Some((range, Err(Unnumberable)))`，`reparse` 会做哪两件事？树会被全量重建吗？

**答案**：不会重建。树保留增量替换的结果，`reparsed_range = range`、`renumber = true`；随后因为 `renumber` 为真且根有 id，执行 `root.numberize(id, Span::FULL)` 从根重新给全树编号。只有 `try_reparse_and_renumber` 返回 `None`（增量替换本身失败）时才会 `*root = parse(text)`。

**练习 2**：为什么 `Lines::edit` 不能简单地「编辑后重算整个行表」？虽然那样结果也对。

**答案**：结果确实相同，但 `Lines` 被 `Arc` 包裹、与 `Source` 一起参与哈希与克隆；截断 + 尾部重算让未受影响的行起点结构保持不变（写时复制下甚至可能不触发克隆），并保证与 `Source` 的「编辑只影响局部」的缓存策略一致。同时 `\r`/`\n` 拼接的特判需要在编辑点局部处理，全量重算反而要额外保证语义一致。

---

### 4.2 下钻与替换：try_reparse_and_renumber 与 MarkupReparse

#### 4.2.1 概念说明

这是算法核心。直觉是：**从根开始，深度优先地寻找「完全包住编辑区间」的最内层节点**，然后只对它动手。可以下手的目标有两类：

1. **单个块节点**（`CodeBlock` `{...}` 或 `ContentBlock` `[...]`）：只要编辑完全落在块内，就把块对应的整段文本重新解析成一个新块，旧块整个换掉。
2. **一串标记表达式**：编辑落在 markup 层的若干个孩子之间（比如改了一个词），此时把这一段孩子（连同两侧扩展的余量）重新解析成一组新孩子。

重解析绝不能「改变邻居」。标记语法是上下文敏感的：`= 标题` 只在行首是标题，`[` `]` 的配对会改变外层内容块的分组。所以第二步必须**在重解析起点复原解析器状态（at_start、nesting），在终点校验这些状态与旧树一致**，否则就会破坏邻居节点。

`MarkupReparse` 是这次重构新增的结构：把「新解析出的孩子们」（`newborns`）、「被替换的孩子区间」（`prev_children`）、「新文本中被重解析的区间」（`new_range`）打包在一起，消除了旧代码里两个 `Range<usize>` 含义混淆的隐患。

#### 4.2.2 核心流程

`try_reparse_and_renumber` 的决策树：

```text
try_reparse_and_renumber(text, replaced, replacement_len, parent_kind, (node, kind, span), offset)
 ├─ overlapping_children(node, replaced, offset)
 │    → 找出与编辑重叠的孩子区间 overlap 与其起始字节偏移；覆盖不住则返回 None
 ├─ 情形 A：overlap 恰好是一个孩子，且它完全包住编辑区间
 │    ├─ child.reparse_inner_node_mut() 有值（是 Inner 且未被警告包裹）
 │    │    └─ 递归下钻；成功 → update_parent 维护摘要，必要时在本层重编号，返回
 │    ├─ 下钻失败/不可下钻，但 child 是块（is_block）
 │    │    └─ reparse_block(text, 块的新区间) → 整块替换 + renumber_children
 │    └─ 否则落到情形 B
 ├─ 情形 B：node 是 Markup，且父亲是顶层（None）或 ContentBlock
 │    └─ expand_and_reparse_markup(...) → MarkupReparse { newborns, prev_children, new_range }
 │         ├─ node.replace_children(prev_children, newborns)
 │         └─ node.renumber_children(span, 替换后的孩子区间)
 └─ 都不行 → None（上层继续向更外层求助，最外层失败则全量解析）
```

`expand_and_reparse_markup` 的扩展循环：

```text
expansion = 1
loop:
  start = overlap.start - max(expansion, 2)   # 两侧留余量
  end   = overlap.end + expansion
  向两侧越过 trivia / 错误 / 分号 / 单独的 "/" 或 ":"（expand 函数）
  若左边是 Hash(#) 则再吃进一个（表达式以 # 开头）
  复原 at_start / nesting / prefix_len（扫描 start 之前的所有孩子）
  计算新区间 new_range = offset+prefix_len .. +（旧长度差 + 替换长度）
  reparse_markup(text, new_range, &mut at_start, &mut nesting, top_level)
  成功且 (at_end ∨ at_start 保持) ∧ ((at_end ∧ top_level) ∨ nesting 保持) → 返回
  start==0 ∧ 已到末尾仍失败 → None
  expansion *= 2                               # 指数扩展
```

#### 4.2.3 源码精读

函数签名与文档注释值得逐句读：[crates/typst-syntax/src/reparser.rs:L43-L79](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/reparser.rs#L43-L79)。注意参数形态的变化：节点以三元组 `(node, node_kind, node_span): (&mut InnerNode, SyntaxKind, Span)` 传入——因为 `Node::Warning` 会「外携」一份 kind，直接从 `&mut SyntaxNode` 上取 kind 和 span 在警告场景下容易踩坑，签名里显式携带这两个值让数据流更清晰。`parent_kind` 只用来判断「父亲是不是 `ContentBlock`」。

情形 A 的递归下钻：[crates/typst-syntax/src/reparser.rs:L82-L133](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/reparser.rs#L82-L133)。先缓存孩子旧的 `len/descendants/diagnosis` 摘要，递归成功后 `assert_eq!(child.len, new_len)` 并调用 `node.update_parent(...)` 代数式地修正自身摘要；若下级编号失败（`num_result.is_err()`），先尝试**在本层**扩展重编号（`node.renumber_children(node_span, overlap)`），把失败逐层上报。块重解析分支调用 [crates/typst-syntax/src/parser.rs:L728-L736](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/parser.rs#L728-L736) 的 `reparse_block`，它要求新块定界符平衡且恰好耗尽区间。

情形 B 的入口判断与 `MarkupReparse` 的消费：[crates/typst-syntax/src/reparser.rs:L135-L155](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/reparser.rs#L135-L155)。注意条件 `matches!(parent_kind, None | Some(SyntaxKind::ContentBlock))`：**只在顶层 markup 或内容块的直接孩子层做标记表达式重解析**。文档注释（L51-L71）坦率承认了取舍：列表项、标题内部目前不做（旧实现因缩进与换行的边界情况多 bug 而被移除，性能损失不大）；数学表达式也整体不支持增量重解析。

`MarkupReparse` 结构本体：[crates/typst-syntax/src/reparser.rs:L157-L166](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/reparser.rs#L157-L166)。三个字段各司其职，注释明确写着「用结构体避免两个 `Range<usize>` 含义混淆」。

指数扩展与 at_start/nesting 校验：[crates/typst-syntax/src/reparser.rs:L168-L270](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/reparser.rs#L168-L270)。L238-L259 的注释是理解这段的钥匙，翻译成大白话：`at_start` 和 `nesting` 一般必须与旧值一致，否则重解析会波及邻居——`= Head` 只有在行首才是标题；删掉 `#[a[b]c` 的第一个 `[` 会让外层内容块重新分组。允许不一致的仅有的豁免是：到达本层 markup 末尾时 `at_start` 可变（后面没有邻居了）；既是末尾又是顶层时 `nesting` 可变（只有顶层 markup 没有外围块）。配套的两个状态推进函数在 [crates/typst-syntax/src/reparser.rs:L316-L336](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/reparser.rs#L316-L336)：`Parbreak` 与带换行的空格会把 `at_start` 置回真；`Text` 类型的 `[` / `]` 增减 `nesting`。`overlapping_children` 与 `expand` 在 [crates/typst-syntax/src/reparser.rs:L272-L314](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/reparser.rs#L272-L314)。

接着看从 `node.rs` 迁来的三个方法（现在住在 `impl InnerNode` 块里，[crates/typst-syntax/src/reparser.rs:L338-L475](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/reparser.rs#L338-L475)）：

- **`update_parent`**（[L338-L368](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/reparser.rs#L338-L368)）：`len` 和 `descendants` 是纯数值，用「先减旧再加新」的代数更新避免溢出；`diagnosis` 是孩子们的「或」，三种分支避免不必要的全孩子扫描。
- **`replace_children`**（[L370-L420](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/reparser.rs#L370-L420)）：splice 之前先用 `spanless_eq` **修剪公共前缀和公共后缀**——重解析虽然粗粒度地覆盖了一段文本，但前后没变的孩子根本不会被替换，它们的对象和 span 原封不动。这是「重解析范围粗、缓存失效细」的关键一招。
- **`renumber_children`**（[L422-L474](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/reparser.rs#L422-L474)）：给一段孩子重新分配编号。可用区间是 `(前一个孩子的编号上界, 后一个孩子的编号)`，塞不下时向两侧**指数扩展**重编号范围，全塞满仍失败返回 `Err(Unnumberable)` 交给上层（最终兜底是 4.1 的全树 `numberize`）。

编号的数学在 `InnerNode::numberize`：[crates/typst-syntax/src/node.rs:L679-L729](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/node.rs#L679-L729)。给 \(n\) 个节点分配区间 \([s, e)\)（长度 \(L = e - s\)）时，步长优先取

\[
\mathit{stride} = \left\lfloor \frac{L}{2n} \right\rfloor,\quad\text{塞不进左半区时退化为}\quad \left\lfloor \frac{L}{n} \right\rfloor
\]

也就是说**默认只占用区间的前半段**，把后半段留白给未来的插入——这就是「兄弟编号之间有空隙」的来源，也是 `replace_children` 修剪前后缀能让未变兄弟保住编号的原因。本次重构把它的签名改成了 `(&mut self, span: Option<&mut Span>, id, range: Option<Range<usize>>, within)`：两个 `Option` 分别支持「是否顺带写本节点自己的 span」与「是否只编号一段孩子」，精确支撑局部重编号；外层的 `SyntaxNode::numberize`（[crates/typst-syntax/src/node.rs:L528-L543](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/node.rs#L528-L543)）则是「编号整棵子树」的便捷包装。

#### 4.2.4 代码实践

阅读测试用例并预测结果（源码阅读型实践）：

1. 实践目标：不运行代码，先手工预测 `test` 的分类，再对照真实断言。
2. 操作步骤：打开 [crates/typst-syntax/src/reparser.rs:L588-L627](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/reparser.rs#L588-L627) 的 `test_reparse_markup`，挑这三条：`test("* #{1+2} *", Edit::Match("2"), "3", Incr("{1+3}"))`、`test("hello  world", Edit::Match("world"), "walkers", All)`、`test("#var. hello", Edit::Match(" "), " ", All)`。
3. 需要观察的现象：第一条为何能停在 `{1+3}` 这么小的范围（编辑落在 `#{...}` 块内，情形 A 递归下钻 + `reparse_block`）；后两条为何必须全量（单词替换改变了文本 token 的合并方式 / `#var.` 后插空格会把后续 markup 卷入代码模式的字段访问）。
4. 预期结果：你的推断应与断言中的 `Incr` / `All` 一致；若推断不出，回到 4.2.2 的决策树逐步走一遍。
5. 待本地验证：用 `cargo test -p typst-syntax --lib test_reparse_markup -- --nocapture` 运行确认。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `expand_and_reparse_markup` 每次都要多带 `expansion.max(2)` 个孩子的余量，而不是精确只取重叠的孩子？

**答案**：编辑可能改变 token 的合并边界。比如在两个单词间插入字符，可能与前后的文本 token 拼成不同的 `Text` 节点（测试注释特意说明用 `~` 是因为它总是独立成 token，而空格可能与相邻文本合并）。带上余量后重解析出的孩子序列更容易与旧孩子在边界上对齐，`replace_children` 再靠 `spanless_eq` 修剪掉实际相同的部分；一次不够就指数扩倍重试。

**练习 2**：`replace_children` 已经把孩子们换好了，为什么还要立刻调 `renumber_children`？新解析出来的孩子不是没有编号吗？

**答案**：`reparse_markup` / `reparse_block` 产出的新节点 span 全是 detached（编号只在 `numberize` 时分配）。`renumber_children` 负责把这段新孩子塞进「左邻上界 ~ 右邻编号」的空隙中；`replace_children` 的文档注释也明确要求调用后必须紧接着 `renumber_children`。塞不下时按指数扩展扩大重编号范围，最终仍失败则把 `Err(Unnumberable)` 层层上报，由 4.1 的入口决定是否全树重编号。

**练习 3**：`update_parent` 里为什么要写 `self.len = (self.len - prev_len) + new_len`，而不是 `self.len += new_len - prev_len`？

**答案**：注释写明是避免溢出。若 `new_len < prev_len`，先加后减的写法 `self.len + new_len - prev_len` 在极端长度下可能出现无符号整数下溢；先减旧值再加新值的两步写法在 `self.len >= prev_len` 恒成立（旧长度本来包含被换孩子）的前提下安全。

---

### 4.3 安全边界：不在语法警告内部重解析

#### 4.3.1 概念说明

u2-l3 讲过 `SyntaxNode` 有四种形态：`Leaf` / `Inner` / `Error` / `Warning`。其中 `Warning` 很特殊：它是一个**包裹器**（`WarningWrapper`），透明地包住另一个真实节点，外携 `message`、`hints` 和可选的 `sub_range`（相对子区间）。`Error` 节点是「叶子化」的（文本和消息都是自己的，没有孩子），而 `Warning` 里面裹着**一整棵活子树**。

这带来一个不对称的难题：增量重解析要在「完全包住编辑的子树」里做替换，如果我们钻进一个警告内部做局部替换，**被替换的只是警告的孩子的孩子，而警告本身（连同它的消息和子区间）会被保留**——但那条消息可能逐字引用了刚刚被改掉的文本。没有任何通用方法能在局部重解析后正确地「修补」消息文本和子区间，所以唯一健全的选择是：**不下钻警告**。要么整体换掉这个警告节点（markup 层重解析会把它整个替换），要么向外扩展甚至全量解析。

这也是 `reparse_inner_node_mut`（不下钻警告）与 `Node::inner_mut`（透明穿过警告）分成两个方法的原因：普通树操作可以无视警告包装，而重解析专用的可变访问必须尊重这条安全边界。

#### 4.3.2 核心流程

```text
SyntaxNode::reparse_inner_node_mut()
 ├─ Node::Inner(inner, kind) → Some((Arc::make_mut(inner), kind, self.span))
 ├─ Node::Warning(..)       → None   # 不穿过！
 └─ Node::Leaf / Node::Error → None
```

于是在 `try_reparse_and_renumber` 里，编辑若落在被警告包裹的节点内部：情形 A 的「单孩子完全包住编辑」分支因为 `child.reparse_inner_node_mut()` 返回 `None` 而无法下钻，只能落到情形 B 在更外层做 markup 重解析——重解析范围显著扩大，被警告的节点作为 `newborns` 的一部分被整体重建，警告自然消失（因为新文本不再触发它，或者根本不再产生该警告）。

#### 4.3.3 源码精读

安全边界的实现只有 8 行：[crates/typst-syntax/src/node.rs:L601-L620](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/node.rs#L601-L620)。文档注释把理由讲得很透：警告的 message / hints / sub-range 可能「target or include subsets of the replaced text」，试图在一般情形下正确匹配并更新这些子集不可行；并补充说实际文档中这条限制几乎没有性能影响。

`WarningWrapper` 的字段定义在 [crates/typst-syntax/src/node.rs:L861-L881](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/node.rs#L861-L881)，`Node` 四形态枚举在 [crates/typst-syntax/src/node.rs:L52-L58](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/node.rs#L52-L58)。对比着看「穿过警告」的常规版本 `Node::inner_mut`（[crates/typst-syntax/src/node.rs:L81-L91](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/node.rs#L81-L91)）：它会顺着 `Warning(warn, _) => data = &mut Arc::make_mut(warn).child` 一路下钻。两个方法一字之差，划出了「一般可变访问」与「重解析可变访问」的界线。

直接验证这条约束的测试：[crates/typst-syntax/src/reparser.rs:L753-L797](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/reparser.rs#L753-L797)。它手工用 `warn_at` 给第二个内容块挂上自定义警告（注释解释：目前没有任何解析器产出的警告能自然触发这个 bug，所以要造树），然后做了两次编辑：

- 第一次编辑 `r~m` → `rm` 落在**无警告**的第一个块内：重解析范围停留在块内（`"normal"`），警告不受影响；
- 第二次编辑 `n~i` → `ni` 落在**被警告**的第二个块内：重解析范围**扩大到整篇文本**（`"#[normal] #[warning]"`），且 `source.root().diagnosis().warnings` 变为假——增量替换没有钻进警告，而是把警告节点连同所在区域整体重建了。

自然警告的真实例子是 `**`（空的强调）：解析器对 `Strong` 节点调用 `p[m].warn("no text within stars")`，见 [crates/typst-syntax/src/parser.rs:L139-L153](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/parser.rs#L139-L153)。对应测试 [crates/typst-syntax/src/reparser.rs:L675-L685](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/reparser.rs#L675-L685)：在 `**` 的两个星号之间插入任何内容，重解析范围都会扩大（`All`）。

#### 4.3.4 代码实践

源码阅读型实践：

1. 实践目标：亲眼确认「`**` 是警告」以及「警告包裹的是 `Strong` 子树而非取代它」。
2. 操作步骤：在仓库里新建临时 Rust 文件（或用综合实践里的 example 程序）打印 `format!("{:#?}", typst_syntax::parse("**"))`。
3. 需要观察的现象：输出形如 `Markup: 2 [ Warning: { message: "no text within stars", hint: "...", Strong: 2 [Star, Markup: 0, Star] } ]`（`Strong` 子树完整地待在警告内部）。
4. 预期结果：与 [crates/typst-syntax/src/node.rs:L1416-L1443](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/node.rs#L1416-L1443) 中 `test_debug` 对 `parse("**")` 的断言逐字段一致。
5. 待本地验证：具体缩进格式以本地运行为准。

#### 4.3.5 小练习与答案

**练习 1**：`Node::inner_mut` 和 `SyntaxNode::reparse_inner_node_mut` 都能拿到 `&mut InnerNode`，它们的本质区别是什么？为什么需要两个？

**答案**：`inner_mut` 遇到 `Warning` 会**继续下钻**到被包裹的子节点（警告被视为透明的包装）；`reparse_inner_node_mut` 遇到 `Warning` **直接返回 None**（把警告视为重解析的边界）。需要两个是因为警告的双重身份：对一般树操作（改 kind、取孩子）它是透明包装；对增量重解析它是不健全的雷区——它的消息与子区间引用了内部文本，局部替换后无法更新。

**练习 2**：既然不能在警告内重解析，那被警告包裹的节点是不是永远不会被增量更新？

**答案**：不是。禁止的只是「钻进警告内部做局部替换」。markup 层的重解析（情形 B）可以把警告节点**连同它的孩子一起整体替换**——旧警告节点整个从树里换出去，新解析出的区域如果没有问题就不会再挂警告。这从根上绕开了「修补消息」的不可能任务，代价只是重解析范围扩大。

---

### 4.4 高亮 Tag 体系：把语法树变成编辑器配色

#### 4.4.1 概念说明

增量重解析解决了「编辑后快速拿到新树」；高亮解决「拿到树之后怎么变成编辑器里的颜色」。Typst 没有为每个 token 单独定义颜色，而是定义了一组**语义标签** `Tag`（22 种：注释、标点、转义、加粗、斜体、链接、raw、标签、引用、标题、列表记号、术语、数学定界符/算符/分组括号、关键字、运算符、数字、字符串、函数、插值、错误）。标签到颜色的映射交给消费方：编辑器插件用 TextMate scope（`tm_scope`），网页用 CSS class（`css_class`）。这种「语义标签 + 双向映射」的设计让同一棵树能同时服务 VS Code、网页文档站等不同前端。

高亮是**基于 `LinkedNode` 的上下文判断**，而不是单纯查 `SyntaxKind` 表：同一个 `Ident`，后面跟着 `(` 时是「函数」（`#f(...)`），跟在 `#` 后是「插值」，跟在 `.` 后继承前一个标识符的判定。所以 `highlight` 的输入是带父链和兄弟链的 `LinkedNode`。

#### 4.4.2 核心流程

```text
编辑器场景的完整闭环：
按键 → Source::edit → 增量 reparse（4.1~4.3） → 新 root
     → LinkedNode::new(root) 自顶向下遍历
     → highlight(node) → Option<Tag>
     → (offset, Tag) 序列 → 前端按 tm_scope / css_class 上色
```

#### 4.4.3 源码精读

`Tag` 枚举与两种映射：[crates/typst-syntax/src/highlight.rs:L3-L50](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/highlight.rs#L3-L50)（枚举本体）、[crates/typst-syntax/src/highlight.rs:L81-L108](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/highlight.rs#L81-L108)（`tm_scope`）、[crates/typst-syntax/src/highlight.rs:L110-L137](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/highlight.rs#L110-L137)（`css_class`）。`Tag::LIST` 常量按定义顺序列出全部标签，可作为 `tag as usize` 的对照表。

主派发函数 `highlight`：[crates/typst-syntax/src/highlight.rs:L139-L155](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/highlight.rs#L139-L155) 是开头几条，整个函数覆盖所有 `SyntaxKind`（到 L316）。看几个体现「上下文敏感」的分支：`Star` 在 `Strong` 父节点下不高亮、否则是运算符（L213-L216）；`Slash` 在 `MathFrac` 父节点下是数学算符（L224-L227）；`LeftParen` 的父节点是 `Math` 时按「数学分组括号」处理（L202-L209）；第一条 `Markup` 分支结合「父是 `TermItem` 且下一个兄弟是 `Colon`」识别术语表词条（L143-L149）。

标识符的上下文推断 `highlight_ident`：[crates/typst-syntax/src/highlight.rs:L318-L374](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/highlight.rs#L318-L374)。判定顺序：紧邻参数列表/内容块括号 → 函数；在数学模式 → 插值；沿 `FieldAccess` 祖先上溯后紧邻 show 规则冒号 → 函数；前面是 `#` → 插值；前面是 `.` 且 `.` 前又是标识符 → **递归套用前一个标识符的判定**（`a.b(...)` 里 `b` 继承 `a` 的函数身份）。

`#` 的透明转发 `highlight_hash`：[crates/typst-syntax/src/highlight.rs:L376-L384](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/highlight.rs#L376-L384)——`#` 本身取**其后表达式最左叶子**的标签，所以 `#f` 里 `#` 与 `f` 同为函数色；`ast::Expr::hash()` 判断该表达式是否真的可被 `#` 引导，不行则 `#` 不高亮。

现成的 HTML 输出 `highlight_html`：[crates/typst-syntax/src/highlight.rs:L391-L435](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/highlight.rs#L391-L435)，先序遍历、按 `css_class` 包 `<span>`、对五种 HTML 特殊字符做转义，`Tag::Error` 不着色。这些 API 通过 `pub use` 从 crate 根导出（[crates/typst-syntax/src/lib.rs:L18](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/lib.rs#L18)），是 typst-ide 与文档站的公共入口。

#### 4.4.4 代码实践

1. 实践目标：为一个字符串生成带 CSS class 的 HTML 高亮。
2. 操作步骤：新建 `crates/typst-syntax/examples/highlight_probe.rs`（示例代码，非项目原有文件），内容一行 `fn main() { println!("{}", typst_syntax::highlight_html(&typst_syntax::parse("#let f(x) = x * 2"))); }`，然后运行 `cargo run -p typst-syntax --example highlight_probe`。
3. 需要观察的现象：`let` 外包 `typ-key`，`f` 外包 `typ-func`，`*` 外包 `typ-op`，数字 `2` 外包 `typ-num`。
4. 预期结果：与 [crates/typst-syntax/src/highlight.rs:L479-L489](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/highlight.rs#L479-L489) 中 `test("#let f(x) = x", ...)` 断言的 `(range, Tag)` 序列一一对应。
5. 待本地验证：具体 HTML 串以本地输出为准。

#### 4.4.5 小练习与答案

**练习 1**：`highlight` 为什么接收 `&LinkedNode` 而不是 `&SyntaxNode`？

**答案**：因为判定需要上下文：父节点类型（`Star` 是否在 `Strong` 内、`Paren` 是否在 `Math` 内）、相邻兄弟（词条的 `Markup` 后面是否跟 `Colon`）、前后叶子（标识符后是否是 `(`、前面是否是 `#` 或 `.`）。`SyntaxNode` 只是纯树节点，没有父链、偏移和兄弟信息；`LinkedNode` 才携带这些。

**练习 2**：`highlight_hash` 为什么不直接返回固定的 `Tag::Interpolated`？

**答案**：`#` 的颜色应与它引导的表达式一致（`#f` 整体看起来是一个函数引用），所以它转发「下一个表达式的最左叶子」的标签；并且先用 `ast::Expr::hash()` 校验该表达式确实可被 `#` 引导，非法时返回 `None` 不上色。

**练习 3**：增量重解析对高亮性能意味着什么？

**答案**：编辑后 `Source::edit` 只重建局部子树、未变节点的对象与 span 不变；高亮遍历虽然仍走整棵 `LinkedNode` 树，但树本身是共享的、遍历是廉价的，且前端可以依据「哪些字节区间的 (range, Tag) 序列没变」跳过重绘。稳定的 span 也让编辑器里基于位置的其他查询（跳转、补全）不必整体失效。

## 5. 综合实践

把 4 个模块串起来：写一个探针程序，对同一个 `Source` 连续做小编辑，记录每次编辑返回的重解析范围与 span 稳定性；再对比「无警告场景的局部重解析」与「警告场景的扩大重解析」。

**第一步**：新建 `crates/typst-syntax/examples/reparse_probe.rs`（示例代码，本讲新写）：

```rust
use typst_syntax::{FileId, Source};

/// 打印根节点每个孩子的文本与 span（Debug 会展开为 Span(Number { .. })）
fn dump(source: &Source, label: &str) {
    print!("{label:>10}:");
    for child in source.root().children() {
        print!(" {:?}{:?}", child.leaf_text(), child.span());
    }
    println!();
}

fn main() {
    // 场景一：普通文本的连续小编辑
    let mut source = Source::new(FileId::new_for_test("probe"), "abc~def~gh~");
    dump(&source, "初始");

    let r1 = source.edit(5..6, "+"); // 把 'e' 改成 '+'
    println!("编辑1 重解析范围 {r1:?} = {:?}", &source.text()[r1.clone()]);
    dump(&source, "编辑1后");

    let r2 = source.edit(9..10, "!"); // 把 'h' 改成 '!'
    println!("编辑2 重解析范围 {r2:?} = {:?}", &source.text()[r2.clone()]);
    dump(&source, "编辑2后");

    // 与全量解析对照：除 span 外必须完全一致
    let fresh = typst_syntax::parse(source.text());
    assert!(source.root().spanless_eq(&fresh));

    // 场景二：语法警告内部的编辑（** 会产生 "no text within stars" 警告）
    let mut warned = Source::new(FileId::new_for_test("probe_warn"), "**");
    let r3 = warned.edit(1..1, "strong"); // 在两个星号之间插入
    println!("警告场景 重解析范围 {r3:?} = {:?}", &warned.text()[r3.clone()]);
    println!("仍有警告? {}", warned.root().diagnosis().warnings);
}
```

**第二步**：运行 `cargo run -p typst-syntax --example reparse_probe`。

**第三步**，对照观察清单：

1. **编辑 1**：期望 `r1 == 0..8`、切片为 `"abc~d+f~"`（与 [crates/typst-syntax/src/reparser.rs:L593](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/reparser.rs#L593) 的真实断言一致）。注意重解析范围虽然从 0 开始，但 `dump` 里第一个孩子 `Text("abc")` 的 span **与初始完全相同**——`expand_and_reparse_markup` 粗粒度覆盖，`replace_children` 修剪公共前缀保住了它。
2. **编辑 2**：期望重解析范围只覆盖中后段（类似 `"d+f~g!~"`，具体边界待本地验证），且更前面的孩子 span 继续保持不变——验证「连续编辑只波及局部子树，绝非整树重建」。
3. **断言**：`spanless_eq(&parse(text))` 通过，验证增量树 ≡ 全量树（4.1 讲的正确性契约）。
4. **警告场景**：期望 `r3` 覆盖整个新文本 `"*strong*"`（与 [crates/typst-syntax/src/reparser.rs:L681](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/reparser.rs#L681) 的断言 `test("**", Edit::At(1), "strong", All)` 一致），且 `仍有警告? false`——`reparse_inner_node_mut` 拒绝下钻警告，重解析扩大到外层并把警告节点整体重建掉（4.3 的安全边界）。

**第四步**（选做）：把场景二的初始文本换成 `"a *b* c"`（正常强调、无警告），在 `b` 处做同样插入，对比重解析范围明显更小、且编辑不消灭任何节点。

> 注：`examples/` 下的探针文件只是学习用临时产物，验证完可删除；不要把它提交进仓库。

## 6. 本讲小结

- `Source::edit` = 写时复制 + `Lines::edit`（行表截断重算尾部）+ `reparse`；`reparse` 的策略是**增量优先**：增量替换成功但编号失败时只在根上重新 `numberize`，增量替换失败才 `parse(text)` 整树重建。
- `try_reparse_and_renumber` 以 `(InnerNode, SyntaxKind, Span)` 三元组下钻：单个孩子完全包住编辑时递归或 `reparse_block` 整块替换；markup 层（顶层或 ContentBlock 直接孩子）用 `expand_and_reparse_markup` 指数扩展，靠 `MarkupReparse { newborns, prev_children, new_range }` 统一封装替换与重编号。
- `replace_children` / `renumber_children` / `update_parent` 已从 `node.rs` 迁入 `reparser.rs`：它们只服务增量重解析；修剪公共前后缀 + 「编号只用区间前半段留空隙」共同保证了未变兄弟的 span 稳定，进而保住下游 comemo 缓存。
- 重解析绝不能穿越 `Warning` 节点：`reparse_inner_node_mut` 与透明的 `inner_mut` 一字之差划出安全边界，因为警告的消息/提示/子区间可能引用被替换文本的任意子集，无法在局部替换后修补。
- `highlight.rs` 用 22 种语义 `Tag` + `tm_scope` / `css_class` 双映射服务编辑器与网页；判定基于 `LinkedNode` 的上下文（父节点、前后叶子），`#` 透明转发后续表达式的颜色。

## 7. 下一步学习建议

本讲结束后，语法层（u2）就完整了：你已经掌握「文本 → token → CST → span 定位 → 增量更新 → 高亮」的全链路。接下来两条路：

1. **主线（推荐）**：进入第三单元 u3-l1「World 抽象」，看 `Source` 如何作为 `World::source` 的返回值进入编译器，以及 `Span` 如何贯穿求值与排版阶段成为缓存 key。
2. **支线（工具向）**：带着本讲的 `Tag` 与 `LinkedNode` 知识直接跳到 u5-l6「typst-ide」，看补全、跳转、悬停如何复用同一套树与 span 机制；增量重解析与 `typst watch` 的联动则在 u5-l8「comemo 与增量编译」中展开。

建议顺手再读一遍 `reparser.rs` 顶部的函数文档注释（L43-L71）——它坦率记录了「哪些场景不做增量、为什么、性能代价如何」的工程取舍，是学习「如何为一棵树设计增量算法」的极好范文。

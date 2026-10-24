# 增量解析与语法高亮

## 1. 本讲目标

上一讲（u2-l3）我们理解了「解析永不失败、错误进树」的递归下降解析器，u2-l4 又建立了 Span 与 LinkedNode 的定位体系。本讲回答两个紧随其后的问题：

1. **编辑之后怎么办？** 用户在编辑器里每敲一个键，源文件就变了一次。如果每次都从头解析整棵树，长文档的补全、诊断、预览都会卡顿。本讲精读 `reparser.rs`，看 Typst 如何只重新解析受影响的一小段文本。
2. **树建好之后，编辑器怎么用？** 本讲讲 `lines.rs` 的行信息维护（行列/UTF-16 换算，LSP 协议的硬需求）和 `highlight.rs` 的 Tag 体系（语法高亮的标准输出格式）。

学完本讲，你应该能够：

- 解释增量重解析的「向下找最小重解析单元、失败则指数外扩、再失败回退全量」三级策略，以及每一步的安全约束。
- 描述 `Source::edit` 如何先更新行信息、再增量重解析，两者如何配合。
- 用 `highlight()` 与 `Tag` 为一段源码生成着色标注，并说出它为什么必须基于 `LinkedNode`（上下文敏感）。

## 2. 前置知识

- **增量计算（incremental computation）**：只重算受影响部分、复用其余结果的思路。Typst 里它贯穿三层：本讲的增量解析、后续单元的 comemo 增量求值/排版。核心直觉：编辑通常是**局部的**，离编辑点远的语法结构几乎不变。
- **为什么编辑器场景特殊**：命令行编译一次就退出，而语言服务器（如生态中的 tinymist）长驻内存，每次按键都要重新解析、重新高亮、重新诊断。这要求解析延迟与「文档大小」脱钩、与「编辑大小」挂钩。
- **UTF-8 与 UTF-16**：Rust 字符串按 UTF-8 存字节；而 LSP（Language Server Protocol）等编辑器协议用 UTF-16 码元定位光标。 emoji（如 💛）在 UTF-8 里占 4 字节、UTF-16 里占 2 码元，所以两种坐标系之间需要精确换算——这就是 `lines.rs` 存在的理由。
- **承接前讲的概念**：`SyntaxNode` 的绿树结构与内部缓存（len/descendants）、`numberize` 按区间中点分配 Span 编号（给增量插入留空隙）、`LinkedNode` 携带父链可 O(1) 反查区间。本讲会直接使用这些结论。
- **语法高亮的两种行业标准**：TextMate scope（VS Code 主题体系使用的 `keyword.operator.typst` 这类点分层级名）与 CSS class（网页展示用）。`highlight.rs` 对同一个 Tag 同时提供两种映射。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [crates/typst-syntax/src/source.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/source.rs) | `Source`：文件身份 + 文本 + 行信息 + 语法树的组合体。`edit`/`replace` 是增量更新的公开入口。 |
| [crates/typst-syntax/src/reparser.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/reparser.rs) | 增量重解析算法本体：定位最小重解析单元、指数外扩、安全校验、回退策略。 |
| [crates/typst-syntax/src/lines.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lines.rs) | 行元数据：每行起始的字节偏移与 UTF-16 偏移，以及字节/行/列/UTF-16 之间的互查和编辑后的增量维护。 |
| [crates/typst-syntax/src/highlight.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/highlight.rs) | 语法高亮：`Tag` 枚举、`SyntaxKind → Tag` 的上下文敏感映射、HTML 输出。 |
| [crates/typst-syntax/src/parser.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/parser.rs) | 解析器。本讲只用到它为增量重解析准备的两个受限入口 `reparse_block` 与 `reparse_markup`。 |
| [crates/typst-syntax/src/node.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/node.rs) | `SyntaxNode` 与 `LinkedNode`。本讲用到 `replace_children`（局部换子树并重新编号）、`update_parent`（同步祖先缓存）。 |

## 4. 核心概念与源码讲解

### 4.1 编辑与增量重解析

#### 4.1.1 概念说明

想象你在编辑一份 5000 行的 `.typ` 文档，在第一行的代码块里把 `x` 改成 `xy`。这次编辑影响的只是那一个代码块；后面 4999 行的标题、列表、公式结构完全没变。增量重解析（incremental reparsing）要做的就是把「没变」变成「不重算」：

- 旧树中不受影响的子树**原样保留**（连 Span 编号都不动，这对后续 comemo 缓存至关重要）。
- 只有编辑点附近的一个**最小文本区间**被切出来重新解析，新子树替换旧子树。
- 替换后必须证明「新子树与前后文拼起来，和全量解析的结果一样」，证明不了就扩大区间重试，最终兜底全量解析。

为什么最后要有兜底？因为增量算法的正确性依赖边界条件（括号配平、行首状态、嵌套深度等），任何不确定都必须退回「全量解析」这个永远正确的基准。**正确性优先于性能**是整个 reparser 的设计基调，文件里的测试甚至直接断言「增量结果 == 全量结果」。

#### 4.1.2 核心流程

`Source::edit(replace, with)` 的主干：

```text
Source::edit(replace: 旧文本中被替换的字节区间, with: 替换文本)
  ├─ 1. lines.edit(replace, with)        # 更新文本与行元数据（见 4.2）
  └─ 2. reparse(root, 新文本, replace, with.len())
        ├─ try_reparse(...)              # 尝试增量
        │    ├─ 2a. 向下：找「完全包住编辑区间」的最内层节点
        │    │      ├─ 单个内部子节点包住 → 递归下钻
        │    │      └─ 下钻失败且该子节点是块（{} 或 [] 块）
        │    │             → reparse_block 只重解析这个块
        │    └─ 2b. 向上：当前节点是 Markup 且父节点是根或内容块
        │             → expand_and_reparse_markup 在兄弟表达式层面外扩重试
        │                  ├─ 每轮把窗口向两侧指数扩倍（1, 2, 4, ...）
        │                  ├─ 通过 at_start/nesting 边界一致性校验
        │                  └─ start==0 且 end==末尾 仍失败 → 放弃
        └─ 3. 全部失败 → parse(整个新文本) + 重新 numberize（全量回退）
```

指数外扩的开销有个漂亮的上界：设第 \( i \) 轮窗口为 \( 2^i \) 个兄弟节点，每轮解析成本与窗口成正比，则总成本

\[ \sum_{i=0}^{k} c \cdot 2^i = c\,(2^{k+1} - 1) < 2c \cdot 2^k \]

即**所有失败尝试的总开销不超过最后一次成功尝试的 2 倍**——最坏情况也只是常数因子地劣于「直接解析最终窗口一次」。

#### 4.1.3 源码精读

**入口：`Source::edit` 先改行、再重解析。**

[crates/typst-syntax/src/source.rs:106-114](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/source.rs#L106-L114)

```rust
pub fn edit(&mut self, replace: Range<usize>, with: &str) -> Range<usize> {
    let inner = &mut **Arc::make_mut(&mut self.0);

    // Update the text and lines.
    inner.lines.edit(replace.clone(), with);

    // Incrementally reparse the replaced range.
    reparse(&mut inner.root, inner.lines.text(), replace, with.len())
}
```

这段代码把文本更新（含行元数据）与树更新串起来，返回值是**新文本中实际被重解析的区间**——这个返回值是后面实践中我们观察「局部性」的主要窗口。注意 `Source` 是 `Arc<LazyHash<...>>` 包裹的（[source.rs:23-32](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/source.rs#L23-L32)），`Arc::make_mut` 保证写时复制：没被编辑的 `Source` 克隆仍然共享同一份树。

**reparser 的顶层 `reparse`：增量失败就全量回退。**

[crates/typst-syntax/src/reparser.rs:13-27](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/reparser.rs#L13-L27)

```rust
pub fn reparse(
    root: &mut SyntaxNode,
    text: &str,
    replaced: Range<usize>,
    replacement_len: usize,
) -> Range<usize> {
    try_reparse(text, replaced, replacement_len, None, root, 0).unwrap_or_else(|| {
        let id = root.span().id();
        *root = parse(text);
        if let Some(id) = id {
            root.numberize(id, Span::FULL).unwrap();
        }
        0..text.len()
    })
}
```

`try_reparse` 返回 `None` 时，直接 `parse(text)` 重建整棵树并重新编号，返回 `0..text.len()` 表示全量重解析。回退路径复用了 u2-l3 讲过的 `parse` 与 u2-l4 讲过的 `numberize`（在 `Span::FULL` 的编号空间内重新分配）。

**算法说明文档**值得先读一遍，它坦率地写明了策略与已知局限：

[crates/typst-syntax/src/reparser.rs:29-52](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/reparser.rs#L29-L52)

- 深度优先找「完全包住替换区间的最内层节点」：要么是一个代码/内容块，要么是若干个直接位于 Markup 块（或顶层）里的 markup 表达式。
- 重解析成功的判据是「新解析的文本括号配平，且配平层级与之前一致」。
- **已知局限**：不重解析列表、标题等内部的 markup 表达式（旧实现因缩进与换行的边界情况太多且性能收益甚微而被移除）；math 完全不做增量重解析。

**下钻与块重解析：`try_reparse`。**

[crates/typst-syntax/src/reparser.rs:53-124](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/reparser.rs#L53-L124)

```rust
fn try_reparse(
    text: &str, replaced: Range<usize>, replacement_len: usize,
    parent_kind: Option<SyntaxKind>, node: &mut SyntaxNode, offset: usize,
) -> Option<Range<usize>> {
    let (overlap, start_offset) = overlapping_children(node, replaced.clone(), offset)?;
    // ...（省略）...

    if let [child] = &mut children[overlap.clone()]
        && start_offset < replaced.start
        && replaced.end < start_offset + child.len()
        && child.is_inner()
    {
        // 编辑严格落在这个子节点内部：先递归下钻
        if let Some(range) = try_reparse(/* ... 传 child ... */) {
            node.update_parent(prev_len, new_len, prev_desc, new_desc);
            return Some(range);
        }
        // 下钻失败，但子节点是块：整块重解析
        if child.kind().is_block()
            && let Some(reparsed) = reparse_block(text, new_range.clone())
        {
            return node
                .replace_children(overlap, vec![reparsed])
                .is_ok()
                .then_some(new_range);
        }
    }

    if node_kind == SyntaxKind::Markup
        && matches!(parent_kind, None | Some(SyntaxKind::ContentBlock))
    {
        expand_and_reparse_markup(/* ... */)
    } else {
        None
    }
}
```

三个要点：

1. `overlapping_children` 先算出与编辑区间重叠的子节点下标范围；返回 `None` 意味着「该节点的孩子们盖不住编辑区间」，直接放弃这一层。
2. 「单个子节点**严格**包住编辑点且是内部节点」时先递归下钻（编辑越深、重解析范围越小）；递归失败再考虑把这个块整体重解析。`is_block` 对应 `[`/`{` 开头的块节点（判定见 [kind.rs:352](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/kind.rs#L352)）。
3. 最后一道分支：当前节点本身是 Markup、且父节点是「根（`None`）或 ContentBlock」时，交给 4.1 中的 markup 外扩逻辑。递归返回 `None` 会自然地沿调用栈上浮，在每一层尝试这个分支——这就是「向下找、失败向上退」的实现方式。

**两个受限解析入口：`reparse_block` 与 `reparse_markup`。**

[crates/typst-syntax/src/parser.rs:730-736](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/parser.rs#L730-L736)

```rust
pub(super) fn reparse_block(text: &str, range: Range<usize>) -> Option<SyntaxNode> {
    let mut p = Parser::new(text, range.start, SyntaxMode::Code);
    assert!(p.at(SyntaxKind::LeftBracket) || p.at(SyntaxKind::LeftBrace));
    block(&mut p);
    (p.balanced && p.prev_end() == range.end)
        .then(|| p.finish().into_iter().next().unwrap())
}
```

块重解析从区间起点（必须是 `[` 或 `{`）以 Code 模式启动解析器，成功条件有两条：`p.balanced`（括号配平，没有把界外的开括号吃进来）且**恰好消费到区间末尾**（`p.prev_end() == range.end`）。第二条防止解析器「越界吞字」。

[crates/typst-syntax/src/parser.rs:66-84](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/parser.rs#L66-L84)

```rust
pub(super) fn reparse_markup(
    text: &str, range: Range<usize>,
    at_start: &mut bool, nesting: &mut usize, top_level: bool,
) -> Option<Vec<SyntaxNode>> {
    let mut p = Parser::new(text, range.start, SyntaxMode::Markup);
    *at_start |= p.had_newline();
    while !p.end() && p.current_start() < range.end {
        // If not top-level and at a new RightBracket, stop the reparse.
        if !top_level && *nesting == 0 && p.at(SyntaxKind::RightBracket) {
            break;
        }
        markup_expr(&mut p, *at_start, nesting);
        *at_start = p.had_newline();
    }
    (p.balanced && p.current_start() == range.end).then(|| p.finish())
}
```

markup 重解析循环解析一个个 markup 表达式，`at_start`（是否处于「行首可出现标题/列表标记」的状态）与 `nesting`（裸 `[`/`]` 的嵌套深度）作为**进出两侧必须一致的边界状态**由调用方传入并带回。非顶层时遇到新的 `RightBracket` 要停手——那是外层内容块的闭括号，不能吃掉。

**markup 外扩：`expand_and_reparse_markup`。**

[crates/typst-syntax/src/reparser.rs:128-224](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/reparser.rs#L128-L224)

```rust
let mut expansion = 1;
loop {
    // Add slack in both directions.
    let mut start = overlap.start.saturating_sub(expansion.max(2));
    let mut end = (overlap.end + expansion).min(children.len());

    // Expand to the left / right：把 trivia、错误、分号、"/"、":" 吸进窗口
    while start > 0 && expand(&children[start]) { start -= 1; }
    while end < children.len() && expand(&children[end]) { end += 1; }

    // Also take hash：# 表达式的 # 号必须跟着表达式走
    if start > 0 && children[start - 1].kind() == SyntaxKind::Hash { start -= 1; }

    // 合成窗口起点的 at_start / nesting，以及窗口终点的期望值
    // ...（省略两段 for 循环）...

    let reparsed = reparse_markup(text, new_range.clone(), &mut at_start, &mut nesting, top_level);
    if let Some(newborns) = reparsed {
        if (at_end || at_start == prev_at_start_after)
            && ((at_end && top_level) || nesting == prev_nesting_after)
        {
            return node.replace_children(start..end, newborns)
                .is_ok().then_some(new_range);
        }
    }

    // If it didn't even work with all children, we give up.
    if start == 0 && at_end { break; }
    expansion *= 2;   // 指数外扩
}
```

安全约束在这一段里全部登场：

- **窗口缝合检查**：若重解析窗口后面还有兄弟节点（`!at_end`），则重解析后的 `at_start` 必须等于原窗口消化后的值（`prev_at_start_after`）；若不是「顶层且到末尾」，`nesting` 也必须一致（[reparser.rs:202-204](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/reparser.rs#L202-L204)）。这两条保证新子树与左右邻居拼起来语义连续。
- **expand 谓词**（[reparser.rs:261-268](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/reparser.rs#L261-L268)）：trivia（空白注释）、错误节点、分号、`/`、`:` 这类「跟谁走都行」的节点要一并吸入窗口，避免它们卡在边界上导致前后状态对不齐。
- **Span 编号耗尽兜底**：`replace_children` 在旧编号空隙里给新子树编号，空隙不够时返回 `Err`，此处 `.is_ok()` 失败即放弃增量（[reparser.rs:207-210](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/reparser.rs#L207-L210)）。这正对应 u2-l4 讲过的「numberize 在区间中点分配、给增量留空隙」的设计。

**树上手术刀：`replace_children` 与 `update_parent`。**

[crates/typst-syntax/src/node.rs:581-604](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/node.rs#L581-L604)

```rust
pub(super) fn replace_children(
    &mut self, range: Range<usize>, replacement: Vec<SyntaxNode>,
) -> NumberingResult { /* ... */ }

pub(super) fn update_parent(
    &mut self, prev_len: usize, new_len: usize,
    prev_descendants: usize, new_descendants: usize,
) { /* ... */ }
```

`replace_children` 把 `children[start..end]` 换成新解析出的节点列表，并在被替换子树原有的编号区间内重新 `numberize`——窗口外的兄弟节点**原封不动**，这正是「只有局部子树变化」的代码学保证。`update_parent` 则同步祖先缓存的 `len`/`descendants` 计数（下钻成功路径在 [reparser.rs:91](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/reparser.rs#L91) 调用它），让 O(1) 的长度/诊断查询在增量更新后依然成立。

**正确性如何回归测试**：`test()` 辅助函数对每次编辑都断言「增量重解析的树 == 全量解析的树」。

[crates/typst-syntax/src/reparser.rs:347-371](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/reparser.rs#L347-L371)

```rust
fn test(text: &str, edit: Edit, with: &str, expected: Reparse) {
    let mut source = Source::detached(text);
    let replaced_range = source.edit(edit.into_range(text), with);
    let mut reparsed_tree = source.root().clone();
    let mut normal_parse = parse(source.text());
    reparsed_tree.synthesize(Span::detached());
    normal_parse.synthesize(Span::detached());
    if reparsed_tree != normal_parse {
        /* 打印两棵树并 panic */
    }
    // 再断言实际重解析范围符合预期（All 或 Incr(片段)）
}
```

先把两棵树都 `synthesize` 成相同的 detached Span 再比较，剔除了编号差异，只比结构。四个测试函数（basic/markup/block/unclosed_embedded，[reparser.rs:374-472](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/reparser.rs#L374-L472)）用几十个用例钉死了边界行为，例如 `test("Hello #{ x + 1 }!", Edit::Match("x"), "abc", Incr("{ abc + 1 }"))` 断言改代码块内的字符只重解析 `{ abc + 1 }` 这 10 个字节。

#### 4.1.4 代码实践

**实践目标**：对同一个 `Source` 连续执行多次小编辑，用 `edit()` 的返回区间与远处节点的 Span 稳定性，验证「只有局部子树被重建，其余子树原样保留」。

**操作步骤**：

1. 先跑现成测试确认环境正常：

   ```bash
   cargo test -p typst-syntax reparse
   ```

   应看到 `test_reparse_basic`、`test_reparse_markup`、`test_reparse_block`、`test_reparse_unclosed_embedded` 四个测试通过。

2. 在 [reparser.rs:292](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/reparser.rs#L292) 的 `mod tests` 里**临时**加入下面的测试（示例代码，非项目原有代码，观察完记得还原）：

   ```rust
   #[test]
   fn observe_incremental_edits() {
       use crate::LinkedNode;

       // 在整棵树里找文本为 text 的叶子，返回它的 Span（远处哨兵）
       fn span_of_text(node: &LinkedNode, text: &str) -> Option<Span> {
           if node.leaf_text().as_str() == text {
               return Some(node.span());
           }
           node.children().find_map(|c| span_of_text(&c, text))
       }

       let mut src = Source::detached(
           "Hello #{ x + 1 }!\n= A heading\nTail.".to_string(),
       );

       // 哨兵：离编辑点很远的叶子节点
       let far = span_of_text(&LinkedNode::new(src.root()), "Tail.").unwrap();

       // 编辑 1：把代码块里的 x 改成 yy（局部修改）
       let i = src.text().find('x').unwrap();
       let r1 = src.edit(i..i + 1, "yy");
       println!("编辑 1 重解析区间 {:?}: {:?}", r1, &src.text()[r1.clone()]);

       // 编辑 2：在标题末尾追加 !（触碰 markup 边界状态）
       let j = src.text().find("heading").unwrap() + "heading".len();
       let r2 = src.edit(j..j, "!");
       println!("编辑 2 重解析区间 {:?}: {:?}", r2, &src.text()[r2.clone()]);

       // 远处的哨兵节点应该原样健在：Span 仍可反查，文本不变
       let node = src.find(far).unwrap();
       assert_eq!(node.leaf_text().as_str(), "Tail.");
       println!("哨兵节点仍在，区间 {:?}", node.range());

       // 至少有一次编辑没有触发全量重解析
       assert!(r1.len() < src.text().len() || r2.len() < src.text().len());
   }
   ```

3. 运行并观察输出：

   ```bash
   cargo test -p typst-syntax observe_incremental_edits -- --nocapture
   ```

**需要观察的现象**：

- 编辑 1 的重解析区间应只覆盖代码块内部（参考既有用例 `Incr("{ abc + 1 }")` 的形态），远小于全文长度。
- 编辑 2 修改的是标题文字，可能得到稍大的区间甚至回退全量（外扩没找到稳定边界时）。
- 无论哪种情况，`"Tail."` 哨兵的 Span 始终能通过 `src.find` 反查成功且文本不变——窗口外的子树真的没被动过。

**预期结果**：测试通过；打印出的重解析区间长度明显小于 `src.text().len()`（除非两次编辑都触发了全量回退）。具体区间数值**待本地验证**——它取决于外扩窗口吸收了哪些兄弟节点，这正是值得你在输出里对照 `expand` 谓词手工推演的地方。

4. 观察完毕后用 `git checkout -- crates/typst-syntax/src/reparser.rs` 还原源码。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `try_reparse` 要求编辑区间被「严格」包住（`start_offset < replaced.start && replaced.end < start_offset + child.len()`），而不是恰好贴着子节点边界也算？

**答案**：如果编辑贴边或跨越多个兄弟节点，就不存在「单个子节点完全包含编辑」的前提，继续下钻没有意义。贴边情形直接落入 Markup 外扩路径：以重叠的兄弟节点集合为初始窗口向外扩，由 `reparse_markup` 在表达式层面重解析并做边界状态校验，这样能正确处理「编辑改变了一个表达式和相邻表达式之间的结构」的情况。

**练习 2**：`reparse_block` 的成功条件里 `p.prev_end() == range.end` 起什么作用？去掉它会怎样？

**答案**：它要求解析**恰好消费到预设区间的末尾**，既不早停（区间里有解析不动的残余）也不越界（把区间外的文本吞进新子树）。去掉它之后，`p.balanced` 只能保证括号配平；解析器若多吃了区间外的字符，替换进树里的子树会和右侧邻居的文本重叠，绿树与源文本不再一一对应，后续所有基于 `Span → 字节区间` 的定位（诊断、跳转、高亮）都会错位。

**练习 3**：增量重解析替换了某个代码块子树后，文档其余部分的 Span 编号为什么能保持不变？

**答案**：u2-l4 讲过 `numberize` 把每个节点的编号分配在其编号区间的中点，父节点区间为子树留有空隙。`replace_children` 只在被替换子树**原有的编号空隙内**为新子树编号；空隙不足时返回 `Unnumberable`，调用方（[reparser.rs:100-105](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/reparser.rs#L100-L105)、[reparser.rs:207-210](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/reparser.rs#L207-L210)）随即放弃增量、回退全量。编号稳定让上层（求值阶段）以 Span 为 key 的 comemo 缓存在小编辑后大面积命中——这是增量解析为增量编译铺的第一块砖。

### 4.2 行信息维护

#### 4.2.1 概念说明

编辑器问编译器的第一类问题往往不是「这段代码什么结构」，而是「**第 37 行第 4 列对应哪个字节**」「**UTF-16 偏移 1024 处是什么**」。诊断信息的展示（`第 3 行: 错误消息`）、光标定位、跳转定义全都依赖这套换算。

朴素做法是每次询问都从头扫描文本，复杂度 \( O(n) \)；`lines.rs` 的做法是预计算**每行起始处的两个数字**——字节偏移与 UTF-16 偏移——存进有序数组，之后所有换算都能先二分定位行、再在行内小范围处理：

\[ \text{byte\_to\_line}: O(\log L), \quad \text{行内换算}: O(\text{行长}) \]

其中 \( L \) 是行数。更妙的是**编辑后的维护也是增量的**：编辑点之前的行完全不变，只需截断之后的行起点、从编辑点重扫后缀。

另一个易被忽视的角色：`Source::replace`（外部工具把**整份新文本**塞过来时的入口，比如测试或某些同步场景）也靠 `Lines::replacement_range` 先做一次前后缀 diff，把「整文件替换」降级为「最小单次编辑」，再走同一条增量管线。

#### 4.2.2 核心流程

`Lines` 的数据模型极简：

```text
Lines<String> = Arc<LinesInner { lines: Vec<Line>, text: String }>
Line = { byte_idx: 行首字节偏移, utf16_idx: 行首 UTF-16 偏移 }
```

各类换算的流程：

```text
byte ↔ (line, column)：
  byte_to_line(byte)     二分查找 ≤ byte 的最后一个行首 → 行号
  byte_to_column(byte)   行首到 byte 之间字符数（注意是字符数，不是字节数）
  line_column_to_byte    反向：跳过 column 个字符

byte ↔ utf16：
  byte_to_utf16(byte)    行首 utf16_idx + 行内前缀的 UTF-16 长度
  utf16_to_byte(u16)     二分行首 utf16_idx → 行内逐字符累加 len_utf16

编辑维护 Lines::edit(replace, with)：
  1. 记录编辑起点的 (byte, utf16, line)
  2. 字符串替换文本本体
  3. lines.truncate(line + 1)      ← 编辑点之前的行起点全部保留
  4. 处理 \r 与 \n 拼接的特殊情况（删掉被合并的行起点）
  5. lines_from(编辑点, ...) 重扫后缀，追加新的行起点
```

注意第 3-5 步的成本是 \( O(\text{编辑点之后的文本长度}) \)——不是全文，也不是常数，而是「编辑越靠文件尾部越便宜」。

#### 4.2.3 源码精读

**数据结构与构造**：

[crates/typst-syntax/src/lines.rs:11-28](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lines.rs#L11-L28)

```rust
pub struct Lines<S>(Arc<LinesInner<S>>);

struct LinesInner<T> {
    lines: Vec<Line>,
    text: T,
}

pub struct Line {
    byte_idx: usize,   // The UTF-8 byte offset where the line starts.
    utf16_idx: usize,  // The UTF-16 codepoint offset where the line starts.
}
```

每行的元数据只有两个 `usize`。`Arc` 包裹让 `Lines` 克隆和整体 `Hash`（[lines.rs:232-236](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lines.rs#L232-L236)）都很廉价——后者是 `Source` 参与下游 comemo 缓存 key 的前提。

**二分定位行**：

[crates/typst-syntax/src/lines.rs:67-74](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lines.rs#L67-L74)

```rust
pub fn byte_to_line(&self, byte_idx: usize) -> Option<usize> {
    (byte_idx <= self.text().len()).then(|| {
        match self.0.lines.binary_search_by_key(&byte_idx, |line| line.byte_idx) {
            Ok(i) => i,
            Err(i) => i - 1,
        }
    })
}
```

行首偏移天然有序，二分查找：命中即是行首；未命中时插入点前一位就是所在行。`byte_to_utf16` 与 `utf16_to_byte`（[lines.rs:59-64](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lines.rs#L59-L64)、[lines.rs:97-115](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lines.rs#L97-L115)）用同样的手法分别在两种坐标系上二分。

**编辑后的增量维护**：

[crates/typst-syntax/src/lines.rs:204-229](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lines.rs#L204-L229)

```rust
pub fn edit(&mut self, replace: Range<usize>, with: &str) {
    let start_byte = replace.start;
    let start_utf16 = self.byte_to_utf16(start_byte).unwrap();
    let line = self.byte_to_line(start_byte).unwrap();

    let inner = Arc::make_mut(&mut self.0);
    inner.text.replace_range(replace.clone(), with);

    // Remove invalidated line starts.
    inner.lines.truncate(line + 1);

    // Handle adjoining of \r and \n.
    if inner.text[..start_byte].ends_with('\r') && with.starts_with('\n') {
        inner.lines.pop();
    }

    // Recalculate the line starts after the edit.
    inner.lines.extend(lines_from(
        start_byte, start_utf16, &inner.text[start_byte..],
    ));
}
```

四步：定位编辑起点 → 改文本 → `truncate(line + 1)` 砍掉编辑点之后的所有行起点（保留编辑所在行，因为它的起点在编辑点之前不受影响）→ 从编辑点重扫后缀生成新行起点。中间那个 `if` 处理「原文以 `\r` 结尾、插入以 `\n` 开头」的拼接：两者会合并成一个 `\r\n` 断行，所以刚保留的那行起点其实已失效，要再 `pop` 掉。

**换行判定与 `\r\n` 合并**：

[crates/typst-syntax/src/lines.rs:252-276](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lines.rs#L252-L276)

```rust
fn lines_from(byte_offset: usize, utf16_offset: usize, text: &str)
-> impl Iterator<Item = Line> + '_ {
    let mut s = unscanny::Scanner::new(text);
    let mut utf16_idx = utf16_offset;

    std::iter::from_fn(move || {
        s.eat_until(|c: char| { utf16_idx += c.len_utf16(); is_newline(c) });
        if s.done() { return None; }
        if s.eat() == Some('\r') && s.eat_if('\n') {
            utf16_idx += 1;   // \r\n 是一个断行，但占两个 UTF-16 码元
        }
        Some(Line { byte_idx: byte_offset + s.cursor(), utf16_idx })
    })
}
```

`is_newline` 来自词法器（[lexer.rs:1156-1161](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lexer.rs#L1156-L1161)），除 `\n`、`\r` 外还承认 Vertical Tab、Form Feed、Next Line、行/段分隔符等 Unicode 断行——**行结构与词法器共用同一套换行定义**，避免「lexer 认为换行、行号却不加」的分裂。`\r\n` 被当作一个断行处理，但补记那 1 个额外码元。

**整文件替换的最小 diff**：

[crates/typst-syntax/src/lines.rs:172-197](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lines.rs#L172-L197)

```rust
pub fn replacement_range(&self, new: &str) -> Option<(usize, usize)> {
    let old = self.text();
    let mut prefix =
        zip(old.bytes(), new.bytes()).take_while(|(x, y)| x == y).count();
    if prefix == old.len() && prefix == new.len() { return None; }
    while !old.is_char_boundary(prefix) || !new.is_char_boundary(prefix) {
        prefix -= 1;
    }
    let mut suffix = /* 从后向前同理 */;
    while !old.is_char_boundary(old.len() - suffix) /* ... */ { suffix += 1; }
    Some((prefix, suffix))
}
```

前后缀逐字节比对找出公共前后缀，中间差集就是最小编辑。两处 `while` 把边界回退到 UTF-8 字符边界，防止把多字节字符劈成两半。`Source::replace`（[source.rs:86-98](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/source.rs#L86-L98)）拿这个结果调用 `edit`，于是「全量换文本」也自动享受增量解析。

#### 4.2.4 代码实践

**实践目标**：亲手验证字节/行/列/UTF-16 换算的往返一致性，以及多字节字符对三套坐标的影响。

**操作步骤**：

1. 运行 lines.rs 自带的单元测试：

   ```bash
   cargo test -p typst-syntax test_source_file
   ```

2. 对照测试常量手工推演（[lines.rs:288](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lines.rs#L288)）：

   ```rust
   const TEST: &str = "ä\tcde\nf💛g\r\nhi\rjkl";
   ```

   先自己算：共几行？每行起始的 `byte_idx` 和 `utf16_idx` 是多少？（提示：`ä` 占 2 字节 1 码元，`💛` 占 4 字节 2 码元。）

3. 把答案与 [lines.rs:291-302](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lines.rs#L291-L302) 的断言对照：

   ```rust
   assert_eq!(lines.0.lines, [
       Line { byte_idx: 0,  utf16_idx: 0 },
       Line { byte_idx: 7,  utf16_idx: 6 },
       Line { byte_idx: 15, utf16_idx: 12 },
       Line { byte_idx: 18, utf16_idx: 15 },
   ]);
   ```

**需要观察的现象**：`💛`（第 1 行）使 `byte_idx` 与 `utf16_idx` 的差值从 1 拉大到 2；`\r\n`（第 1 行末）被算作一个断行，因此总共只有 4 行而不是 5 行。

**预期结果**：测试全部通过；你手算的行起点与断言一致。若不一致，重点检查 `\r\n` 的合并与 `ä` 的 2 字节。

#### 4.2.5 小练习与答案

**练习 1**：`Lines::edit` 里 `truncate(line + 1)` 之后为什么还要一个条件 `pop`？什么时候触发？

**答案**：处理 `\r` 与 `\n` 的拼接。当编辑点之前的文本以 `\r` 结尾、插入文本以 `\n` 开头时，两者合并成一个 `\r\n` 断行，编辑所在行的「起点之后紧跟断行」关系改变——该行起点本身虽然还在编辑点之前，但后续重扫会从编辑点生成行起点，而原行起点对应的断行位置已变，于是刚保留的这条行记录实际失效，需要 `pop` 掉。测试用例 `test("abc\ndef\r", 8..8, "\nghi", "abc\ndef\r\nghi")`（[lines.rs:401](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/lines.rs#L401)）正是覆盖这一分支。

**练习 2**：为什么 `Line` 要同时存 `byte_idx` 和 `utf16_idx` 两个偏移，只存一个不行吗？

**答案**：只存 `byte_idx` 的话，`utf16_to_byte` 就必须先二分到行、再把行首到目标之间的文本逐字符累加 `len_utf16`——但二分的前提是数组按 `utf16_idx` 有序，只存字节偏移时无法对 UTF-16 查询二分，只能整段扫描。两个偏移各自构成有序序列，让 byte 与 UTF-16 两个方向都能「二分定位行 + 行内线性」，\( O(\log L + \text{行长}) \) 封顶。存储开销只是每行多 8 字节。

**练习 3**：`Source::replace(new)` 接到一份与旧文本 99% 相同的新文本时，内部会发生什么？

**答案**：它调用 `Lines::replacement_range` 做前后缀 diff，找出最小差异区间（对齐到字符边界），然后把这个差异当作一次普通 `edit` 处理：更新行信息 + 增量重解析那 1% 的差异区域（[source.rs:86-98](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/source.rs#L86-L98)）。若新旧完全相同则直接返回 `0..0`。

### 4.3 高亮 Tag 体系

#### 4.3.1 概念说明

语法高亮看似简单（给关键字上个色），实则有坑：

1. **一词多义**：同一个 `*`，在 `*bold*` 里是标记、在 `1 * 2` 里是乘号；同一个 `(`，在代码里是普通括号、在数学模式里是「分组括号」。着色不能只看 token 本身，还得看**上下文**（父节点、甚至前后兄弟）。
2. **标准对接**：不同编辑器/主题各说各话。Typst 选择同时输出 TextMate scope（VS Code 主题生态的通用语）和 CSS class（网页场景），一个语义、两种编码。

`highlight.rs` 的方案：定义 22 种语义 `Tag`，提供一个**上下文敏感**的函数 `highlight(&LinkedNode) -> Option<Tag>`，输入必须是带父链的 `LinkedNode`（u2-l4 讲过它），返回 `None` 表示「不该着色」（普通文本、空白、纯结构节点）。

一个容易忽略的事实：这套高亮不只服务编辑器——**Typst 文档里嵌的代码块**（```` ```typst ```` 的 raw block）在编译产物中的着色，就是 typst-library 里复用 `typst_syntax::highlight` 实现的，后文会看这段消费代码。

#### 4.3.2 核心流程

```text
highlight(node: &LinkedNode) -> Option<Tag>
  ├─ 大 match 按 SyntaxKind 分派
  │    ├─ 大部分种类 → 固定 Tag（Let/If/For → Keyword，Int/Float → Number，...）
  │    ├─ 纯结构节点（CodeBlock、FuncCall、Args...）→ None（不着色）
  │    └─ 上下文敏感种类 → 查 parent_kind / 兄弟：
  │         Star   父是 Strong → None；否则 → Operator
  │         LeftParen/RightParen  父是 Math → MathGroupingParens；否则 Punctuation
  │         Slash  父是 MathFrac → MathOperator；否则 Operator
  │         Eq     父是 Heading（标题的 = 号）→ None；否则 Operator
  │         Ident/MathIdent → highlight_ident() 进一步推断
  ├─ highlight_ident：标识符的身份推断
  │    ├─ 紧邻参数列表的 ( 或内容块 [ → Function
  │    ├─ 数学标识符 → Interpolated
  │    ├─ show 规则冒号前后 → Function
  │    ├─ 前面是 # → Interpolated
  │    └─ 在 a.b 的 b 位置 → 递归沿用 a 的 Tag
  └─ highlight_hash：# 号透传其后表达式的 Tag

输出编码：
  Tag::tm_scope()  → "keyword.operator.typst" 这类 TextMate scope
  Tag::css_class() → "typ-op" 这类 CSS class
  highlight_html() → 直接产出 <code><span class="...">…</span></code>
```

#### 4.3.3 源码精读

**Tag 枚举与双重编码**：

[crates/typst-syntax/src/highlight.rs:5-50](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/highlight.rs#L5-L50) 定义了 22 个语义变体（Comment、Strong、Link、Heading、Keyword、Number、String、Function、Interpolated、Error 等）；每种都能映射为 TextMate scope 或 CSS class：

[crates/typst-syntax/src/highlight.rs:83-108](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/highlight.rs#L83-L108)

```rust
pub fn tm_scope(&self) -> &'static str {
    match self {
        Self::Strong => "markup.bold.typst",
        Self::Link => "markup.underline.link.typst",
        Self::MathDelimiter => "punctuation.definition.math.typst",
        Self::Keyword => "keyword.typst",
        Self::Number => "constant.numeric.typst",
        Self::Function => "entity.name.function.typst",
        Self::Interpolated => "meta.interpolation.typst",
        Self::Error => "invalid.typst",
        // ...
    }
}
```

[crates/typst-syntax/src/highlight.rs:111-136](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/highlight.rs#L111-L136) 的 `css_class` 则给出 `typ-strong`、`typ-num`、`typ-func` 这类短类名。

**主体：一个巨大的上下文敏感 match**：

[crates/typst-syntax/src/highlight.rs:142-316](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/highlight.rs#L142-L316)，摘几组代表性分支：

```rust
pub fn highlight(node: &LinkedNode) -> Option<Tag> {
    match node.kind() {
        SyntaxKind::Markup
            if node.parent_kind() == Some(SyntaxKind::TermItem)
                && node.next_sibling_kind() == Some(SyntaxKind::Colon) =>
        {
            Some(Tag::ListTerm)      // 术语列表里冒号前的词条
        }
        // ...
        SyntaxKind::LeftParen => match node.parent_kind() {
            Some(SyntaxKind::Math) => Some(Tag::MathGroupingParens),
            _ => Some(Tag::Punctuation),
        },
        SyntaxKind::Star => match node.parent_kind() {
            Some(SyntaxKind::Strong) => None,   // *bold* 的星号本身不着色
            _ => Some(Tag::Operator),
        },
        SyntaxKind::Slash => Some(match node.parent_kind() {
            Some(SyntaxKind::MathFrac) => Tag::MathOperator,  // 分数线
            _ => Tag::Operator,
        }),
        SyntaxKind::Eq => match node.parent_kind() {
            Some(SyntaxKind::Heading) => None,  // 标题的 = 不是运算符
            _ => Some(Tag::Operator),
        },
        // ...
        SyntaxKind::Let | SyntaxKind::If | SyntaxKind::For /* ... */ => Some(Tag::Keyword),
        SyntaxKind::Int | SyntaxKind::Float | SyntaxKind::Numeric => Some(Tag::Number),
        SyntaxKind::Str => Some(Tag::String),
        SyntaxKind::Ident => highlight_ident(node),
        // ...
        SyntaxKind::Error => Some(Tag::Error),
        // 大量纯结构节点 => None
    }
}
```

第一分支展示了最深的上下文依赖：一个 Markup 节点通常不着色，但若它是 `TermItem` 的孩子且后面跟着冒号（`/ term: description`），它就是应当着色的**词条**。这也解释了为什么签名是 `&LinkedNode` 而非 `&SyntaxNode`——裸绿树节点没有 `parent_kind()`/`next_sibling_kind()`。

**标识符的身份推断**：

[crates/typst-syntax/src/highlight.rs:319-374](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/highlight.rs#L319-L374)

```rust
fn highlight_ident(node: &LinkedNode) -> Option<Tag> {
    // Are we directly before an argument list?
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
    // ...
    // Are we behind a dot, that is behind another identifier?
    let prev = node.prev_leaf()?;
    if prev.kind() == SyntaxKind::Dot {
        let prev_prev = prev.prev_leaf()?;
        if is_ident(&prev_prev) {
            return highlight_ident(&prev_prev);   // a.b 沿用 a 的 Tag
        }
    }
    None
}
```

判断「是不是函数名」不看名字，而看**位置**：紧贴着 `(`（且该括号属于 Args/Params）或 `[`（内容块）的标识符就是函数调用名；`calc.max` 里的 `max` 通过「点号前面是标识符」递归沿用 `calc` 的结论。中间还有数学标识符 → Interpolated、show 规则冒号前后 → Function、`#` 之后 → Interpolated 等分支。`#` 号本身则透传其后表达式的颜色（[highlight.rs:377-384](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/highlight.rs#L377-L384)）。

**现成的 HTML 输出**：

[crates/typst-syntax/src/highlight.rs:394-435](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/highlight.rs#L394-L435)

```rust
pub fn highlight_html(root: &SyntaxNode) -> String {
    let mut buf = String::from("<code>");
    let node = LinkedNode::new(root);
    highlight_html_impl(&mut buf, &node);
    buf.push_str("</code>");
    buf
}
```

`highlight_html_impl` 深度优先遍历：有 Tag 的节点包一层 `<span class="typ-...">`（Error 除外），叶子文本做 HTML 转义。这就是网页端展示 Typst 代码的最短路径。

**仓库内的真实消费者**：文档里 ```` ```typst ```` 代码块的着色。

[crates/typst-library/src/text/raw.rs:962-972](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/text/raw.rs#L962-L972)

```rust
for child in self.node.children() {
    let mut scopes = self.scopes.clone();
    if let Some(tag) = typst_syntax::highlight(&child) {
        scopes.push(syntect::parsing::Scope::new(tag.tm_scope()).unwrap());
    }
    // ... 递归处理子节点
}
```

typst-library 的 raw 元素高亮器遍历语法树，把每个节点的 `tm_scope()` 压进 syntect 的 scope 栈，最终交给主题配色。也就是说：**你在 Typst 文档里贴的 Typst 代码，其高亮正是本节这几十行 match 的产出**。

#### 4.3.4 代码实践

**实践目标**：对一段典型源码跑通「解析 → 遍历 → Tag 标注」，并与官方测试断言互证。

**操作步骤**：

1. 先跑现成的高亮测试：

   ```bash
   cargo test -p typst-syntax test_highlighting
   ```

   该测试（[highlight.rs:444-490](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/highlight.rs#L444-L490)）内置期望值，如 `#f(x + 1)` 期望 `(0..1, Function), (1..2, Function), (2..3, Punctuation), (5..6, Operator), (7..8, Number), (8..9, Punctuation)`——注意 `#` 和 `f` 都是 Function（`#` 透传），`x` 没有出现在期望表里（普通标识符不着色）。

2. 在 [highlight.rs:437](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/highlight.rs#L437) 的 `mod tests` 里**临时**加入（示例代码，观察后还原）：

   ```rust
   #[test]
   fn observe_my_highlight() {
       let text = r#"#let greet(name) = "Hi " + name"#;
       let root = crate::parse(text);
       let mut tags = vec![];
       fn walk(tags: &mut Vec<(std::ops::Range<usize>, Tag)>, node: &LinkedNode) {
           if let Some(tag) = highlight(node) {
               tags.push((node.range(), tag));
           }
           for child in node.children() {
               walk(tags, &child);
           }
       }
       walk(&mut tags, &LinkedNode::new(&root));
       for (range, tag) in &tags {
           println!("{:?} {:>12}  {}", range, format!("{:?}", tag), &text[range.clone()]);
       }
       println!("{}", highlight_html(&root));
   }
   ```

3. 运行：

   ```bash
   cargo test -p typst-syntax observe_my_highlight -- --nocapture
   ```

**需要观察的现象**：

- `#let` 两个 token 都是 Keyword，`greet` 是 Function（紧贴 `(`），字符串 `"Hi "` 是 String，`+` 是 Operator。
- `name`（参数处与 `+` 右侧）预期不着色（不在 tags 里）。
- 末尾打印的 HTML 里可以看到嵌套的 `<span class="typ-key">`、`<span class="typ-func">`、`<span class="typ-str">`。

**预期结果**：测试通过，输出与上述推断一致；若某项不符，回到 `highlight` 对应分支找原因。具体逐 token 输出**待本地验证**。

4. 观察后 `git checkout -- crates/typst-syntax/src/highlight.rs` 还原。

#### 4.3.5 小练习与答案

**练习 1**：`highlight` 的参数为什么是 `&LinkedNode` 而不是 `&SyntaxNode`？举一个离开父链就无法判定的例子。

**答案**：因为高亮是上下文敏感的，需要问「父节点是什么/兄弟是什么」，而绿树的 `SyntaxNode` 只存孩子不存父。例子：`*` 在父节点为 `Strong` 时不着色（星号是加粗标记本身），否则是乘法 `Operator`；`(` 在父节点为 `Math` 时是 `MathGroupingParens`，否则是普通 `Punctuation`；Markup 节点要「父是 TermItem 且右兄弟是冒号」才判为 `ListTerm`。这些都依赖 `LinkedNode` 的 `parent_kind()` / `next_sibling_kind()`。

**练习 2**：`#calc.max(1, 2)` 中 `max` 会被标成什么？推断过程经过哪些分支？

**答案**：`Function`。`max` 是 `Ident`，走 `highlight_ident`：它紧贴的下一个叶子是 `(`（属于 `Args`）本可直接判为 Function；即使位置条件因字段访问结构而变化，最后的「点号分支」也会发现 `max` 前是 `.`、`.` 前是标识符 `calc`，于是递归 `highlight_ident(calc)`，由 `calc` 紧贴 `.` 前的结构最终落在 `Function`（`#` 之后的推导链同理）。`#` 号自身通过 `highlight_hash` 透传为同样的 Function 色。

**练习 3**：`highlight_html_impl` 为什么对 `Tag::Error` 特殊处理（不包 span）？

**答案**：见 [highlight.rs:404-407](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/highlight.rs#L404-L407) 的 `tag != Tag::Error` 条件。HTML 展示场景（如文档站代码示例）里内容都来自**已通过编译的**代码，Error 高亮没有意义；而编辑器场景下错误标红是有用的，所以 `highlight` 仍会对 Error 节点返回 `Tag::Error`，由调用方（编辑器）自行决定是否使用。同一个 Tag 体系、两种消费策略。

## 5. 综合实践

**任务：写一个 20 行以内的「迷你编辑器回放」临时测试，把本讲三个模块串成一条流水线。**

背景：语言服务器的核心循环就是「收到编辑 → 更新 Source → 对受影响区间重算诊断/高亮」。我们用仓库现有 API 模拟这个循环：

1. **操作步骤**（临时测试可放在 `crates/typst-syntax/src/reparser.rs` 的 `mod tests` 中，示例代码，完成后还原）：

   ```rust
   #[test]
   fn mini_editor_replay() {
       // 模拟逐字输入 "Hi *world*"（最后补上闭合的 *）
       let mut src = Source::detached(String::new());
       let keys = ["H", "i", " ", "*", "w", "o", "r", "l", "d", "*"];
       for k in keys {
           let end = src.text().len();
           let range = src.edit(end..end, k);        // 模块 1：增量重解析
           let (line, col) = src.lines()             // 模块 2：行信息
               .byte_to_line_column(range.end)
               .unwrap();
           println!(
               "输入 {:?} 后：重解析 {} 字节，光标落点 {},{}",
               k, range.len(), line + 1, col + 1,
           );
       }

       // 模块 3：对最终文档做高亮，并验证增量树 == 全量树
       let mut fast = src.root().clone();
       let mut slow = parse(src.text());
       fast.synthesize(Span::detached());
       slow.synthesize(Span::detached());
       assert_eq!(fast, slow);
       println!("{}", crate::highlight_html(src.root()));
   }
   ```

2. **运行**：`cargo test -p typst-syntax mini_editor_replay -- --nocapture`。

3. **需要观察的现象**：每次按键后的重解析字节数（应远小于累计文本长度，除非边界外扩）；行/列落点递增；最后的等价断言通过；HTML 里 `*world*` 整体被包进 `typ-strong`。

4. **预期结果**：断言全部通过；单次重解析字节数保持个位到十位数量级。具体数值**待本地验证**——特别注意输入第一个 `*`（开了一个未闭合的 Strong）时边界如何变化，这正是 `expand_and_reparse_markup` 外扩逻辑活跃的时刻。

5. 完成后还原源码：`git checkout -- crates/typst-syntax/src/reparser.rs`。

## 6. 本讲小结

- `Source::edit` 是增量更新的公开入口：先用 `Lines::edit` 更新文本与行起点，再调 `reparse` 增量重解析，返回**实际被重解析的新文本区间**。
- reparser 三级策略：向下找完全包住编辑的最小单元（块则 `reparse_block`）、在 Markup 层面指数外扩（`expand_and_reparse_markup`，总开销不超过最终窗口的 2 倍）、全部失败回退全量 `parse`——正确性永远有兜底。
- 增量替换的安全约束：括号配平且恰好消费到区间末尾、`at_start`/`nesting` 边界状态与原值一致、Span 编号空隙充足；窗口外的子树连同 Span 原样保留，为上层增量编译铺路。
- `Lines` 只存每行起始的字节与 UTF-16 双偏移，全部坐标换算「二分定位行 + 行内线性」；编辑后 `truncate + 重扫后缀` 增量维护，`replacement_range` 的前后缀 diff 让整文件替换也走增量管线。
- `highlight(&LinkedNode) -> Option<Tag>` 是上下文敏感的着色判定：一词多义靠 `parent_kind`/兄弟信息消歧，标识符身份（函数/插值）靠位置推断；`tm_scope`/`css_class` 双编码对接编辑器与网页两个生态。
- 这套 API 的主要客户是编辑器场景（长驻、低延迟），仓库内也有真实消费者——文档中 raw 代码块的高亮（typst-library/src/text/raw.rs）。

## 7. 下一步学习建议

本讲结束了第二单元（语法层）。接下来按学习路线进入**第三单元 typst-library 与 typst-eval**，建议从 [u3-l1 World 抽象](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs) 开始：Source 及其增量能力正是 World 向编译器暴露的核心输入之一。

在此之前，如果你想把语法层吃得更透，可以：

- 阅读 [reparser.rs:374-472](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/reparser.rs#L374-L472) 的全部测试用例，对每个 `Incr`/`All` 结果先做预测再看答案——这是检验你是否真正理解边界约束的最好习题。
- 关注本讲反复出现的「增量」主线：本讲的增量解析 → u5-l8 将讲的 comemo 增量求值/排版，Span 稳定性是贯穿两者的钥匙。
- 若对编辑器集成感兴趣，可以留意在 [highlight.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/highlight.rs) 之上构建的官方 crate [crates/typst-ide/src/lib.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-ide/src/lib.rs)（学习路线中的 u5-l6），看补全与跳转如何复用 LinkedNode 定位。

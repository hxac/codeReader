# try_reparse 核心算法

## 1. 本讲目标

本讲是增量重解析单元（U9）的第二篇，承接 u9-l1。上一篇我们看清了「入口与兜底」：`Source::edit` 把「改文本+行表」与「改语法树」解耦，`reparse` 先尝试局部增量（`try_reparse`），失败则全量 `parse + numberize` 兜底。本讲下钻 `try_reparse` 本身——也就是「局部增量到底怎么做」的核心算法。

学完后你应该能够：

- 说清 `try_reparse` 如何**深度优先**地自顶向下找到「完全包住编辑范围的最内层节点」，失败时如何**向外扩展 / 向上回退**。
- 用 `overlapping_children` 手算「哪些子节点与编辑范围重叠」。
- 区分两条重解析路径：**单个块子节点**走 `reparse_block`，**顶层 / markup 块内的表达式序列**走 `expand_and_reparse_markup`。
- 复述「成功的判据」：定界符平衡、重解析范围精确对齐、边界上下文（`at_start` / `nesting`）一致、重编号不超限。
- 理解 `update_parent` 与 `replace_children` 在成功路径上各自扮演的收尾角色。

## 2. 前置知识

本讲假设你已掌握（来自前置讲义）：

- **CST 与 SyntaxNode**（U5）：节点有 `Leaf / Inner / Error / Warning` 四种形态，`Inner` 节点带 `children`，并缓存了 `len`（字节长度）、`descendants`（子树节点数）、`diagnosis`（是否含错/警告）、`upper`（编号上界）四个字段。
- **Span 编号系统**（U6）：`numberize` 给每个节点盖唯一编号，守两条不变量——父节点编号小于任意子节点、兄弟从左到右递增。编号区间用「取中点、刻意挤左半区」策略留余量，`Span::FULL` 是整个编号空间。
- **reparse 入口与兜底**（u9-l1）：`reparse` 返回「新文本中实际被重解析的字节范围」；返回 `0..text.len()` 即代表做了全量。`try_reparse` 返回 `None` 时，`reparse` 用 `unwrap_or_else` 兜底全量重建。

两个本讲要用到的关键事实：

- `is_block()` 只对 `CodeBlock`（`{ ... }`）与 `ContentBlock`（`[ ... ]`）返回真（见 [src/kind.rs:324-326](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L324-L326)）。
- `Source::edit` 调用 `reparse`，把旧文本里被替换的范围 `replace` 与替换串长度 `with.len()` 传进去（见 [src/source.rs:104-112](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L104-L112)）。

## 3. 本讲源码地图

本讲几乎全部围绕单一文件 `src/reparser.rs`，并少量引用它的「帮手」：

| 文件 | 作用 |
| --- | --- |
| `src/reparser.rs` | 增量重解析的全部策略：`try_reparse`、`overlapping_children`、`expand_and_reparse_markup`、`expand`/`next_at_start`/`next_nesting` 三个上下文推演函数，以及一份完整的测试套件。 |
| `src/node.rs` | `SyntaxNode` 的 `is_inner` / `descendants` / `children_mut` / `replace_children` / `update_parent`，以及 `InnerNode` 内部的替换与重编号实现。 |
| `src/kind.rs` | `is_block()` 判据。 |
| `src/parser.rs` | `reparse_block` 与 `reparse_markup` 两个「局部重解析钩子」（实现细节是 u9-l3 的主题，本讲只把它们当成 `try_reparse` 调用的黑盒，关注其成功条件）。 |

数据流回顾（来自 u9-l1）：`Source::edit` → `Lines::edit`（改文本+行表）→ `reparse`（改树）→ `try_reparse`（本讲主角）→ 成功则局部返回小范围，失败则全量兜底。

## 4. 核心概念与源码讲解

### 4.1 try_reparse：深度优先找最内层包围节点

#### 4.1.1 概念说明

增量重解析的核心矛盾是：**编辑发生在某一处，但它对语法树的影响可能蔓延到周围**。比如在 `#x + 1` 里把 `x` 删掉，可能让原本的标识符 token 消失；在 `*ab` 里加一个 `*`，可能让原本未闭合的 `Strong` 闭合。所以「只重解析被编辑的那一个叶子」是远远不够的。

Typst 的策略可以概括为一句话（来自源码顶部注释，见 [src/reparser.rs:31-54](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L31-L54)）：

> 自顶向下深度优先地找到「完全包住编辑范围的、最内层的」节点或节点组。它要么是**单个代码/内容块**，要么是**直接位于顶层或某个 markup 块内的一串 markup 表达式**。然后调用解析器重解析这段文本，**仅当定界符平衡、且嵌套层级与原来一致时才算成功**；否则把范围向外扩展，或向上一层回退，直到成功或退到全文。

注意三个关键词：**最内层**（尽量小，保住更多节点的 Span 不变）、**完全包住**（不能切在节点中间）、**平衡 + 同层级**（保证局部重解析的结果与全量解析完全一致）。

#### 4.1.2 核心流程

`try_reparse` 是一个递归函数，签名见 [src/reparser.rs:55-62](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L55-L62)。它在「某个节点 `node`、从字节偏移 `offset` 开始」的局部视图里工作，返回 `Option<Range<usize>>`：`Some(r)` 表示「新文本的 `r` 范围已被重解析并写回」，`None` 表示「本层搞不定，请上层另想办法」。

每一层做三件事：

```text
try_reparse(text, replaced, replacement_len, parent_kind, node, offset):
  1. overlapping_children(node, replaced, offset)
       —— 找出 node 中与编辑范围重叠的子节点组 (overlap, start_offset)
       —— 若 node 的子节点无法完全覆盖编辑范围，直接返回 None

  2. 【路径 A：单块子节点】若重叠的恰好是 1 个 inner 子节点、
        且编辑严格落在它内部 → 先递归下钻 try_reparse(child)；
        递归失败再尝试：若该子节点是 block → reparse_block 整块重解析。

  3. 【路径 B：markup 表达式序列】若 node 本身是 Markup、
        且其父是「根」或 ContentBlock → expand_and_reparse_markup
        在兄弟表达式间指数扩展地重解析。

  否则返回 None（向上回退）。
```

「向上回退」是理解整体行为的关键：当某一层 `try_reparse` 返回 `None`，调用它的那一层（在路径 A 的递归里）会转而尝试自己这一层的块重解析；若仍失败，再返回 `None` 给更上一层……直到最外层的 `reparse` 收到 `None`，触发全量兜底。因此「失败时向外扩展」既发生在**单层内部**（路径 B 的指数扩展），也发生在**调用栈层级之间**（路径 A 的递归回退）。

#### 4.1.3 源码精读

函数主体的「分流」逻辑：

```rust
// src/reparser.rs:63-66  —— 先定位重叠子节点，拿不到就直接放弃本层
let (overlap, start_offset) = overlapping_children(node, replaced.clone(), offset)?;
let node_kind = node.kind();
let children = node.children_mut();
```

随后是路径 A 的判定与路径 B 的兜底（[src/reparser.rs:68-125](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L68-L125)）。路径 A 的开头是一个组合条件：

```rust
// src/reparser.rs:68-72
if let [child] = &mut children[overlap.clone()]
    && start_offset < replaced.start
    && replaced.end < start_offset + child.len()
    && child.is_inner()
```

四个条件缺一不可：重叠子节点**恰好一个**、编辑起点严格大于该子节点起点、编辑终点严格小于该子节点终点（两道严格不等式保证编辑「严格落在内部」，不贴边）、且该子节点是 `Inner`（叶子无处下钻）。

路径 B 的触发条件则锁定了「合法的 markup 重解析位置」：

```rust
// src/reparser.rs:111-113
if node_kind == SyntaxKind::Markup
    && matches!(parent_kind, None | Some(SyntaxKind::ContentBlock))
```

即：当前节点必须是 `Markup`，且它的父节点要么是整棵树的根（`parent_kind == None`），要么是 `ContentBlock`（`[ ... ]`）。这正是注释里「只在顶层或 markup 块内重解析 markup 表达式」的代码体现——**列表项、标题内部**的 markup 不在此列，原因注释也说了：曾经支持过，但缩进与换行的边界情况太容易出 bug，移除后性能影响很小（见 [src/reparser.rs:43-50](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L43-L50)）。

#### 4.1.4 代码实践

**实践目标**：用真实测试用例体会「同一段文本里，有的编辑能局部重解析（`Incr`），有的被迫全量（`All`）」。

**操作步骤**：阅读 [src/reparser.rs:432-455](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L432-L455) 的 `test_reparse_block` 测试。测试框架 `test()`（[src/reparser.rs:351-374](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L351-L374)）会把 `Source::edit` 返回的范围与全文比较：等于全文记为 `Reparse::All`，否则记为 `Reparse::Incr(实际重解析的文本片段)`。

**需要观察的现象**：

- `test("Hello #{ x + 1 }!", Edit::Match("x"), "abc", Incr("{ abc + 1 }"))` —— 把 `x` 换成 `abc`，重解析范围恰好是那个代码块 `{ abc + 1 }`，远小于全文。
- `test("A#{}!", Edit::After("{"), "\"", All)` —— 在 `{` 后插入 `"`（一个未闭合的字符串引号），被迫 `All`。
- `test("#{}}", Edit::After("{"), "{", All)` —— 插入 `{` 改变了定界符配对，被迫 `All`。

**预期结果**：`Incr` 的用例都满足「块定界符仍平衡、重解析范围精确落在块内」；`All` 的用例都破坏了平衡或边界对齐。**待本地验证**：可在仓库内执行 `cargo test -p typst-syntax --lib reparser::tests::test_reparse_block` 观察这些断言是否通过。

#### 4.1.5 小练习与答案

**练习 1**：`try_reparse` 为什么坚持找「最内层」的包围节点，而不是直接重解析最外层的根？

**参考答案**：重解析的范围越小，被重新编号的节点越少，未被触及的节点 Span 保持不变，下游（求值、增量编译）的缓存命中率就越高。找最内层就是为了让「受影响面」最小化。

**练习 2**：路径 B 的条件为什么要求 `parent_kind` 是 `None` 或 `ContentBlock`，而不是任意父节点？

**参考答案**：因为只有在「顶层」或「内容块 `[ ... ]`」内部的 markup 序列，其解析行为只依赖 `at_start` 和 `[`/`]` 嵌套这两个本地上下文（见 4.4）。列表项、标题内部的 markup 还依赖缩进列号等更脆弱的上下文，局部重解析难以保证与全量解析一致，所以被有意排除。

---

### 4.2 overlapping_children：定位与编辑范围重叠的子节点

#### 4.2.1 概念说明

`try_reparse` 每到一层，首先要回答一个问题：「在当前节点的孩子们里，哪几个跟编辑范围 `[replaced.start, replaced.end)` 有重叠？」这就是 `overlapping_children` 的职责。它是整个算法的「地理定位器」——只有先定位到受影响的子节点，才能决定下钻还是扩展。

它返回一个二元组 `(Range<usize>, usize)`：第一个是「子节点在父中的下标区间」，第二个是「这组子节点在源文本中的起始字节偏移」。注意这俩量的单位不同（注释明确提醒，见 [src/reparser.rs:228-233](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L228-L233)）：一个是孩子下标，一个是字节偏移。

#### 4.2.2 核心流程

```text
overlapping_children(node, range, offset):
  前置校验（任一不满足返回 None）：
    - node 必须是 inner（有孩子）
    - offset <= range.start   （编辑起点不早于 node 起点）
    - range.end <= offset + node.len()  （编辑终点不晚于 node 终点）

  线性扫描 children，维护：
    start        = 包含 range.start 的那个孩子的下标
    start_offset = 该孩子的起始字节偏移
    index        = 第一个「越过 range.end」的孩子下标（即末尾 exclusive）
  返回 (start..index, start_offset)
```

它保证两条不变量（以 `debug_assert!` 标注，见源码）：`start_offset <= range.start`，且 `range.end <= 最终 offset`。也就是说，返回的这组孩子**总是完全覆盖**编辑范围。

#### 4.2.3 源码精读

前置校验一行写完（[src/reparser.rs:239-242](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L239-L242)）：

```rust
if !node.is_inner() || offset > range.start || range.end > offset + node.len() {
    return None;
}
```

扫描循环（[src/reparser.rs:243-256](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L243-L256)）：

```rust
for child in node.children() {
    if offset < range.start {
        start = index;          // 这个孩子可能包含 range.start，记下
        start_offset = offset;
    }
    offset += child.len();
    index += 1;
    if range.end < offset { break; }  // 已经越过终点，停止
}
```

要点：在消费每个孩子**之前**判断「当前偏移是否还在 `range.start` 之前」；只要还在，就把 `start` 更新为当前孩子。因此扫描结束后，`start` 恰好落在「起点位于 `range.start` 之前（含）的最后一个孩子」上——也就是包含 `range.start` 的那个孩子。`index` 则在「消费完某个孩子后偏移首次严格超过 `range.end`」时停住。

#### 4.2.4 代码实践

**实践目标**：用纸笔验证 `overlapping_children` 的行为，建立直觉。

**操作步骤**：假设某个 `Markup` 节点有三个孩子，字节跨度分别是：

| 孩子下标 | 文本 | 字节跨度 |
| --- | --- | --- |
| 0 | `abc` | [0, 3) |
| 1 | `def` | [3, 6) |
| 2 | `ghi` | [6, 9) |

对编辑范围 `range = [4, 6)`（落在孩子 1 内部，且终点恰为孩子 1 的右边界），手动模拟循环。

**需要观察的现象**：追踪每一轮的 `offset / start / start_offset / index`。

**预期结果**：

- 第 0 轮：`offset=0 < 4` → `start=0, start_offset=0`；`offset→3`，`index→1`；`6 < 3`? 否。
- 第 1 轮：`offset=3 < 4` → `start=1, start_offset=3`；`offset→6`，`index→2`；`6 < 6`? 否（严格小于）。
- 第 2 轮：`offset=6 < 4`? 否；`offset→9`，`index→3`；`6 < 9`? 是 → `break`。

返回 `(1..3, 3)`。注意：尽管编辑范围 `[4,6)` 完全在孩子 1 内部，但因为 `range.end` 恰好落在边界上（用的是严格 `<`），孩子 2 也被拉进了重叠组。这对路径 B（markup 扩展）是安全的——多带一个兄弟一起重解析即可；对路径 A 则意味着 `[child]` 模式不成立（因为是 2 个孩子），会自然落到路径 B。**待本地验证**：可在 `reparser.rs` 的 `#[cfg(test)]` 模块里临时加一个断言来确认。

#### 4.2.5 小练习与答案

**练习 1**：把上面的编辑范围改成 `[4, 5)`（完全在孩子 1 内部、不贴任何边界），返回值是什么？

**参考答案**：第 1 轮消费完孩子 1 后 `offset=6`，`5 < 6` 成立 → `break`，此时 `index=2`。返回 `(1..2, 3)`——恰好一个孩子，满足路径 A 的 `[child]` 模式。

**练习 2**：为什么前置校验里 `offset > range.start` 要返回 `None`？

**参考答案**：`offset` 是 `node` 的起始字节。若 `offset > range.start`，说明编辑范围的起点在 `node` 之前——`node` 的孩子们不可能覆盖这个范围，再往下找无意义，应当返回 `None` 让上层处理。

---

### 4.3 单块子节点路径：递归下钻与 reparse_block

#### 4.3.1 概念说明

当 `overlapping_children` 报告「恰好一个 inner 子节点、且编辑严格落在其内部」时，进入路径 A。这条路径体现了「能往下钻就往下钻」的思想：

1. **先递归**：对这个子节点再调一次 `try_reparse`，看能不能在更深层完成更小范围的重解析。
2. **递归失败再整块重解析**：如果这个子节点本身是一个「块」（`{ ... }` 或 `[ ... ]`），就调 `reparse_block` 把整块重新解析。

为什么块要整块重解析？因为块有自己的定界符（`{ }` 或 `[ ]`），只要编辑没碰到定界符（严格落在内部），整块文本就可以独立地重新解析，结果与在全量解析中完全一致——这就是「定界符平衡」判据的来源。

#### 4.3.2 核心流程

```text
路径 A（overlap 恰好 1 个 inner 子节点 child，编辑严格内部）：
  计算 new_len = prev_len + replacement_len − replaced.len()
       new_range = start_offset .. start_offset + new_len   （覆盖整个 child）

  第一步：递归 try_reparse(child, start_offset)
    成功 ⇒ 断言 child.len() == new_len
            update_parent(prev_len, new_len, prev_desc, new_desc)  ← 更新本节点缓存
            返回该层 range ✓

  第二步（递归失败时）：
    child.kind().is_block()?  （CodeBlock 或 ContentBlock）
      是 ⇒ reparse_block(text, new_range)
             Some(rep) 且 replace_children(overlap, vec![rep]) 重编号成功
               ⇒ 返回 new_range ✓
             否 ⇒ 继续（落到路径 B 判定，通常不满足 → 返回 None）
```

注意 `new_range` 覆盖的是**整个 child**（含定界符），不是被编辑的小片段——因为 `reparse_block` 需要从块的左定界符重新解析。

#### 4.3.3 源码精读

递归下钻与成功后的收尾（[src/reparser.rs:80-95](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L80-L95)）：

```rust
let prev_len = child.len();
let prev_desc = child.descendants();
let new_len = prev_len + replacement_len - replaced.len();
let new_range = start_offset..start_offset + new_len;

if let Some(range) = try_reparse(/* ..., child, start_offset */) {
    assert_eq!(child.len(), new_len);
    let new_desc = child.descendants();
    node.update_parent(prev_len, new_len, prev_desc, new_desc);
    return Some(range);
}
```

`new_len` 的计算就是一行算术：\( \text{new\_len} = \text{prev\_len} + \text{replacement\_len} - \text{replaced.len()} \)。`assert_eq!` 用来保证「递归成功时孩子的新长度确实等于预测值」——这是一个内部一致性检查。

递归失败后的块重解析（[src/reparser.rs:99-108](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L99-L108)）：

```rust
if child.kind().is_block()
    && let Some(reparsed) = reparse_block(text, new_range.clone())
{
    return node
        .replace_children(overlap, vec![reparsed])
        .is_ok()
        .then_some(new_range);
}
```

`reparse_block` 的成功条件（[src/parser.rs:749-755](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L749-L755)）是两个判断的与：

```rust
(p.balanced && p.prev_end() == range.end).then(|| p.finish().into_iter().next().unwrap())
```

即「所有定界符平衡（`p.balanced`）」且「解析器恰好消费到 `range.end`、不多不少（`p.prev_end() == range.end`）」。前者保证块是合法闭合的，后者保证重解析结果正好填满 `new_range`、没有溢出也没有提前结束。哪怕这两个都满足，`replace_children` 仍可能因「编号空间不足」返回 `Err`（罕见），于是用 `.is_ok().then_some(...)` 把它折回 `None`，向上回退。

#### 4.3.4 代码实践

**实践目标**：用公开 API `Source::edit` 观察路径 A 的块重解析范围。

**操作步骤**：在仓库外新建小项目（或写成 `examples` 风格的代码），调用：

```rust
// 示例代码：观察块内编辑的重解析范围
use typst_syntax::Source;

let mut src = Source::detached("Hello #{ x + 1 }!");
// "x" 位于字节 9..10
let range = src.edit(9..10, "abc");
println!("重解析范围: {:?}", range);           // 期望恰好覆盖代码块
println!("范围内文本: {:?}", &src.text()[range]); // 期望为 "{ abc + 1 }"
println!("新全文: {:?}", src.text());
```

**需要观察的现象**：返回的 `range` 应该是整个 `CodeBlock` 的范围（从 `{` 到 `}`），而不是只覆盖 `abc`。`&src.text()[range]` 应打印出 `"{ abc + 1 }"`。

**预期结果**：与 `test_reparse_block` 第一条用例 `Incr("{ abc + 1 }")` 一致——块定界符未被触碰、严格在内部编辑，故走路径 A 的块重解析，范围精确为该块。**待本地验证**：精确的字节范围请以本地运行结果为准（依赖 `typst-syntax` 的具体版本）。

#### 4.3.5 小练习与答案

**练习 1**：路径 A 的条件里 `replaced.end < start_offset + child.len()` 用的是严格小于。若改成 `<=`（允许编辑终点贴着孩子右边界），会带来什么风险？

**参考答案**：编辑若贴到孩子的右边界，可能触及孩子与右兄弟的「接缝」（例如块的闭合 `}` 后紧跟的内容）。严格小于保证编辑完全在孩子内部、不碰接缝，这样把孩子单独重解析才是安全的；放宽到 `<=` 会让本应由上层（路径 B 或更外层）处理的接缝情况错误地进入路径 A，可能产生与全量解析不一致的结果。

**练习 2**：递归下钻成功后，为什么要调 `update_parent` 而不是 `replace_children`？

**参考答案**：递归成功时，孩子 `child` 本身还在原位（只是它内部被改写了、长度变了），并没有被「替换成另一个节点」。所以只需把父节点缓存的 `len`、`descendants`、`diagnosis` 按「旧值→新值」做差量修正（`update_parent` 正是干这个，见 4.5），不需要走 `replace_children` 的 splice + 重编号流程。

---

### 4.4 markup 表达式序列路径：指数扩展与边界上下文校验

#### 4.4.1 概念说明

当 `node` 是 `Markup` 且父是根或 `ContentBlock` 时，进入路径 B（`expand_and_reparse_markup`）。这条路径处理的是「一串平铺的 markup 表达式」——比如正文里的若干段文本、强调、列表项。它和路径 A 的根本区别在于：markup 表达式之间**没有成对的定界符**把每一段框死，编辑的影响可能跨越多个相邻表达式，因此无法像块那样「一次定界」，而需要**逐步扩大范围**直到重解析结果稳定。

 Typst 用「指数扩展」来控制扩大节奏：每失败一次，就把纳入重解析的兄弟数量翻倍（\( 1, 2, 4, 8, \dots, 2^k \)），兼顾「尽量小」与「快速收敛」。

但「重解析成功」还不够——还必须保证重解析**没有改变边界处的上下文**，否则局部结果会与全量解析不一致。这里说的上下文有两个：

- `at_start`：下一段 markup 是否处在「行/块起始」（决定 `=`/`-`/`+`/`/` 是否被识别为标题/列表/枚举/术语标记）。
- `nesting`：内容块 `[ ]` 的嵌套深度（决定非顶层时重解析该在哪里被 `]` 截断）。

#### 4.4.2 核心流程

```text
expand_and_reparse_markup(node, replaced, ..., overlap, offset, top_level):
  expansion = 1
  loop:
    1. 在 overlap 两侧各取 expansion 个兄弟（左侧至少留 2）；
       再用 expand() 贪婪吞下边缘的 trivia/error/分号/「/」「:」叶子；
       若左邻是 Hash('#')，一并吞入（# 必须与其后的代码表达式一起重解析）。
       → 得到重解析窗口 [start, end)（孩子下标）

    2. 扫描 children[..start]，累推出 prefix_len、at_start、nesting 的「入口态」；
       扫描 children[start..end]，累推出 prev_len、prev_at_start_after、prev_nesting_after
       （即「重解析前，窗口末尾之后的 at_start/nesting 应当是什么」）。

    3. new_range = offset+prefix_len .. offset+prefix_len+new_len
       调 reparse_markup(text, new_range, &mut at_start, &mut nesting, top_level)

    4. 若返回 Some(newborns)，再校验边界一致性：
         (at_end || at_start == prev_at_start_after)
         && ((at_end && top_level) || nesting == prev_nesting_after)
       全部满足、且 replace_children 重编号成功 ⇒ 返回 new_range ✓

    5. 若 start==0 且 at_end（已覆盖全部孩子）仍失败 ⇒ break，返回 None
       否则 expansion *= 2，回到第 1 步
```

`expand()` 决定哪些节点「粘」在边缘必须一起重解析（[src/reparser.rs:263-270](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L263-L270)）：trivia、error、分号、以及文本是 `/` 或 `:` 的叶子。原因是这些 token 单独留在边界会让重解析结果依赖于邻接内容（例如 `/` 可能与后续字符组成 `/*` 注释、`:` 可能开启术语项、分号终结语句）。

#### 4.4.3 源码精读

扩展窗口的代码（[src/reparser.rs:143-162](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L143-L162)）：

```rust
let mut expansion = 1;
loop {
    let mut start = overlap.start.saturating_sub(expansion.max(2));
    let mut end = (overlap.end + expansion).min(children.len());

    while start > 0 && expand(&children[start]) { start -= 1; } // 向左吞粘性节点
    while end < children.len() && expand(&children[end]) { end += 1; } // 向右吞

    if start > 0 && children[start - 1].kind() == SyntaxKind::Hash { start -= 1; } // 吞 #
    // ...
```

注意 `expansion.max(2)`：即便 `expansion=1`，左侧也至少回退 2 个，给重解析留出足够的前文上下文。

边界一致性校验是整条路径最精细的部分（[src/reparser.rs:200-213](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L200-L213)）：

```rust
if let Some(newborns) = reparsed {
    if (at_end || at_start == prev_at_start_after)
        && ((at_end && top_level) || nesting == prev_nesting_after)
    {
        return node.replace_children(start..end, newborns).is_ok().then_some(new_range);
    }
}
```

- `at_end || at_start == prev_at_start_after`：若窗口已到末尾（`at_end`），后面没有孩子，`at_start` 自然无所谓；否则必须保证重解析后出口的 `at_start` 与原本「窗口之后那个孩子」所要求的 `at_start` 一致——否则那个孩子会不会被识别成标题/列表就可能翻转。
- `(at_end && top_level) || nesting == prev_nesting_after`：顶层且到末尾时 `nesting` 无所谓；否则 `nesting` 必须一致——否则 `[ ]` 的配对关系会被打乱。

`reparse_markup` 自身的成功条件（[src/parser.rs:64-82](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L64-L82)）同样是「平衡 + 精确对齐」：`(p.balanced && p.current_start() == range.end)`。

#### 4.4.4 代码实践

**实践目标**：理解为何某些「看似局部」的编辑会因破坏边界上下文而被迫全量。

**操作步骤**：阅读 [src/reparser.rs:403-408](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L403-L408) 的几条 `All` 用例：

- `test("#var. hello", Edit::Match(" "), " ", All)` —— 把 `#var` 后的单空格换成「空格」看似没变，但触发了全量。
- `test("hello  world", Edit::Match("world"), "walkers", All)` —— 把 `world` 换成 `walkers`，竟也是全量。
- `test("~*~*~", Edit::At(2), "*", All)` —— 在 `*` 前再插一个 `*`，全量。

**需要观察的现象**：这些用例的共同点是「编辑改变了 token 的拼接或 `at_start` 上下文」，例如多空格合并、`*` 配对关系改变。

**预期结果**：它们都无法满足「重解析后边界 `at_start`/`nesting` 与原来一致」，于是 `expand_and_reparse_markup` 一路扩展到全部孩子仍失败，返回 `None`，触发全量 `All`。**待本地验证**：可用 `cargo test -p typst-syntax --lib reparser::tests::test_reparse_markup` 跑通。

#### 4.4.5 小练习与答案

**练习 1**：`expand()` 为什么把 `leaf_text() == "/"` 的叶子也判为「需要扩展」？

**参考答案**：单独一个 `/` 在边界上是歧义的——它可能与后面的 `*` 组成块注释 `/*`，也可能是枚举标记 `/`（当处于行首时），还可能是除号。把它留在边界外、不参与重解析，会让重解析结果依赖边界另一侧的内容，无法保证与全量解析一致，所以必须吞进来一起重解析。

**练习 2**：指数扩展（`expansion *= 2`）相比线性扩展（`expansion += 1`）有什么取舍？

**参考答案**：线性扩展在最坏情况下要扩展 O(n) 次，每次都调一次解析器，代价高；指数扩展只需 O(log n) 次就能覆盖全部孩子，收敛快。代价是单次扩展可能「多带」一些本不需要的兄弟，重解析范围略大——但因为只重解析、不影响未触及节点的 Span，这点额外开销远小于多次重试，是合理的工程取舍。

---

### 4.5 update_parent 与 replace_children：成功的收尾与增量重编号

#### 4.5.1 概念说明

两条路径一旦「重解析成功」，都要把结果写回树。这里有两个层次的工作：

1. **修正祖先缓存**：CST 的 `InnerNode` 缓存了 `len`、`descendants`、`diagnosis`，子树一改，沿路径的所有祖先缓存都得跟着修。`update_parent` 做轻量的差量修正；`replace_children` 在替换孩子后内部也会修。
2. **局部重编号**：被替换的孩子需要重新分配 Span 编号。`replace_children` 内部带一个「指数扩展重编号」循环，把需要重编号的兄弟范围逐步扩大，直到编号塞得下或放弃。

注意「重解析的指数扩展」（4.4 的 `expand_and_reparse_markup`）与「重编号的指数扩展」（本节 `InnerNode::replace_children`）是同一思想的两处应用：前者扩大「重解析文本」的范围，后者扩大「重新分配编号」的兄弟范围。

#### 4.5.2 核心流程

`SyntaxNode::update_parent`（[src/node.rs:594-604](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L594-L604)）只是转发到 `InnerNode::update_parent`（[src/node.rs:848-858](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L848-L858)）：

```text
update_parent(prev_len, new_len, prev_desc, new_desc):
  len         += new_len − prev_len
  descendants += new_desc − prev_desc
  diagnosis   = Diagnosis::any(&children)   // 从孩子重算
```

它只更新「一个孩子的尺寸/诊断变了」这种最简单的情况——对应路径 A 递归下钻成功后的收尾。

`replace_children` 则处理「换掉一整段孩子」的复杂情况（块路径换 1 个、markup 路径换一段），内部步骤见下。

#### 4.5.3 源码精读

`InnerNode::replace_children`（[src/node.rs:737-845](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L737-L845)）分四步：

第一步，**裁掉公共前后缀**（[src/node.rs:746-764](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L746-L764)）：用 `spanless_eq` 比较新旧孩子的结构（不看 span），前后相同的不参与替换，从而把真正要改的范围压到最小，也让重编号范围尽量小。

第二步，**差量更新 `len`/`descendants`/`diagnosis`**（[src/node.rs:770-793](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L770-L793)）：长度与节点数都用「新和 − 旧和」修正；诊断按「原本无错就直接用新诊断 / 新诊断错且警告齐全也直接用 / 否则扫描范围外的孩子重新聚合」三种情况处理，避免漏掉被换掉的错误孩子。

第三步，**splice 写入**新孩子（[src/node.rs:796-798](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L796-L798)）。

第四步，**指数扩展重编号**（[src/node.rs:800-844](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L800-L844)）：

```rust
let mut left = 0;
let mut right = 0;
loop {
    let renumber = range.start - left..range.end + right;
    // 用「前一个孩子的 upper」与「后一个孩子的 number」框定可用编号区间
    let within = start_number..end_number;
    if self.numberize(span, id, Some(renumber), within).is_ok() {
        return Ok(());
    }
    if left == max_left && right == max_right { return Err(Unnumberable); }
    left = (left + 1).next_power_of_two().min(max_left);  // 指数扩展
    right = (right + 1).next_power_of_two().min(max_right);
}
```

编号区间由「重编号范围左侧那个孩子的 `upper`（编号上界）」到「右侧那个孩子的 `span.number()`」框定——这正是 u6-l2 讲过的「父编号小于子、兄弟递增」两条不变量给出的可用空间。塞不下就把重编号范围向两侧指数扩大、重新分配更宽的区间；到极限仍塞不下，返回 `Err(Unnumberable)`。调用方（`try_reparse`）用 `.is_ok().then_some(...)` 把 `Err` 折回 `None`，于是整条增量路径失败、回退到全量。

#### 4.5.4 代码实践

**实践目标**：把 `replace_children` 的「指数扩展重编号」与 4.4 的「指数扩展重解析」对照，确认它们是同一思想的两次应用。

**操作步骤**：对照阅读两段循环：

- 重解析扩展：[src/reparser.rs:143-223](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L143-L223)（`expansion *= 2`，扩大参与重解析的兄弟范围）。
- 重编号扩展：[src/node.rs:806-844](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L806-L844)（`left/right` 指数增长，扩大参与重编号的兄弟范围）。

**需要观察的现象**：两者结构高度相似——都是「先试小范围，失败就把范围向两侧翻倍，直到成功或覆盖全部」。

**预期结果**：能复述两者的差异——重解析扩展扩大的是「重新跑解析器的文本窗口」（为了找到与全量解析一致的最小可重解析段），重编号扩展扩大的是「重新分配 Span 编号的孩子范围」（为了在编号空间里塞下变化后的子树）。两者都失败时，最终都把控制权交回上层（重解析扩展失败 → `None` → 全量；重编号失败 → `Err(Unnumberable)` → `.is_ok()` 折回 `None` → 全量）。

#### 4.5.5 小练习与答案

**练习 1**：`update_parent` 重算 `diagnosis` 时用的是 `Diagnosis::any(&children)`（扫描全部孩子），而 `replace_children` 用了更复杂的三分支逻辑。为什么 `update_parent` 可以「偷懒」？

**参考答案**：`update_parent` 只在路径 A「递归下钻成功」后调用，此时只有那一个孩子内部改了、其他孩子未动，且改动的就是「包含错误/警告」的那棵子树，所以直接从全部孩子重新聚合诊断既正确又简单。`replace_children` 面对的是「换掉一段孩子」，被换掉的可能是唯一含错的孩子，必须区分「原本是否有错」「新诊断是否齐全」等情况，避免误判，因此需要更精细的三分支逻辑。

**练习 2**：为什么重编号区间用「左邻孩子的 `upper`」作为下界、用「右邻孩子的 `number`」作为上界？

**参考答案**：由编号不变量（u6-l2）——任意孩子的编号都大于左兄弟子树里所有节点的编号、小于右兄弟的编号。「左邻的 `upper`」正是左邻子树的最大编号加一（即重编号区间的最小可用编号），「右邻的 `number`」正是重编号区间不能逾越的上界。用它们框定 `within`，既不与左右邻居冲突，又能尽量局部化重编号。

## 5. 综合实践

**实践目标**：把本讲四条线索（深度优先下钻、单块路径、markup 扩展路径、失败回退）串成一张完整的决策流程图——这正是本讲规格指定的核心实践任务。

**操作步骤**：

1. 重读 `try_reparse` 的注释与函数体（[src/reparser.rs:31-126](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L31-L126)）。
2. 自行在纸上画出从「收到一次编辑」到「返回重解析范围或回退全量」的完整决策流程。
3. 在图上标注每个决策点的判据（恰好一个 inner 孩子 / 严格内部 / `is_block` / `Markup` 且父为根或 `ContentBlock` / `balanced` / `prev_end == range.end` / 边界 `at_start`/`nesting` 一致 / 重编号成功）。
4. 用 4.1.4 里 `Incr` 与 `All` 的真实用例各选一两个，沿着你画的图走一遍，验证图能正确预测结果。

**需要观察的现象**：图里应当清晰体现两条「回退通道」——路径 A 的递归回退（沿调用栈向上）与路径 B 的指数扩展（在兄弟间向外）——以及它们最终都汇入 `reparse` 的全量兜底。

**预期结果**：下面给出一份参考流程图（ASCII 版），可对照检查自己的图是否覆盖了全部判据：

```text
Source::edit(replace, with)
   │  Lines::edit 改文本+行表
   ▼
reparse(root, text, replace, with.len())
   │
   ▼
try_reparse(node=root, replaced=replace, offset=0)   ← 递归入口
   │
   ├─ overlapping_children(node, replaced, offset)
   │     └─ None（子节点无法覆盖） ⇒ 返回 None ──────────┐
   ▼                                                      │
 重叠组是「恰好 1 个 inner 子节点」且编辑严格在其内部？    │
   │                                                      │
   ├─ 是 ──▶ 递归 try_reparse(child) 下钻更深层            │
   │           ├─ Some(range) ⇒ update_parent ⇒ 返回 ✓    │
   │           └─ None ⇒ child.is_block()?                │
   │              ├─ 是 ⇒ reparse_block(new_range)        │
   │              │     (判据: balanced && prev_end==end) │
   │              │     ├─ Some 且 replace_children 重编号 │
   │              │     │   成功 ⇒ 返回 new_range ✓        │
   │              │     └─ 否 ⇒ 落到下方 markup 判定       │
   │              └─ 否 ⇒ 落到下方 markup 判定             │
   ▼                                                      │
 node 是 Markup 且 parent ∈ {根(None), ContentBlock}?     │
   │                                                      │
   ├─ 是 ──▶ expand_and_reparse_markup                    │
   │           expansion=1,2,4… 向两侧指数扩展兄弟        │
   │           reparse_markup (判据: balanced && 对齐)    │
   │           + 边界校验 (at_start / nesting 一致)       │
   │           + replace_children 重编号成功              │
   │           ├─ 全部满足 ⇒ 返回 new_range ✓             │
   │           └─ 覆盖全部孩子仍失败 ⇒ 返回 None ─────────┤
   ▼                                                      │
 否 ⇒ 返回 None ─────────────────────────────────────────┤
                                                          │
   任一层返回 None ⇒ 上层在更外层重试；                    │
   最外层 reparse 收到 None ──▶ unwrap_or_else 兜底：      │
        parse(text) 全量重建 + numberize(id, Span::FULL)  │
        返回 0..text.len()（= 全量）                       │
```

**待本地验证**：选定的真实用例可用 `cargo test -p typst-syntax --lib reparser::tests` 验证你的预测是否与断言一致。

## 6. 本讲小结

- `try_reparse` 是一个**递归**函数，每层先用 `overlapping_children` 定位与编辑范围重叠的子节点，再分流到两条路径，失败则返回 `None` 让上层回退。
- **路径 A（单块子节点）**：重叠组恰好一个 inner 子节点、编辑严格在其内部时，先递归下钻，递归失败再对该 `CodeBlock`/`ContentBlock` 调 `reparse_block` 整块重解析。
- **路径 B（markup 表达式序列）**：当 `node` 是 `Markup` 且父为根或 `ContentBlock` 时，用 `expand_and_reparse_markup` 在兄弟间**指数扩展**地重解析。
- **成功的判据**始终是：定界符平衡（`p.balanced`）、重解析范围精确对齐（`prev_end`/`current_start == range.end`）、边界上下文一致（`at_start`/`nesting`）、以及重编号不超限（`replace_children` 返回 `Ok`）。
- `update_parent` 做「单个孩子尺寸变化」的轻量差量修正（用于路径 A 递归成功）；`replace_children` 做「换掉一段孩子」的完整替换，内部含**指数扩展重编号**循环，失败返回 `Err(Unnumberable)`，被折回 `None` 触发全量兜底。
- 「失败向外扩展」发生在两个地方：路径 B 在兄弟间指数扩展、路径 A 沿调用栈向上回退；两者最终都汇入 `reparse` 的全量 `parse + numberize` 兜底，保证正确性永不妥协。

## 7. 下一步学习建议

下一篇 **u9-l3（markup / block 重解析钩子）** 将打开本讲里两个「黑盒」`reparse_markup` 与 `reparse_block`，讲清它们如何复用 `Parser` 重新解析局部文本、如何传递 `at_start` 与 `nesting`、以及为何要求定界符平衡。建议：

- 继续阅读 [src/parser.rs:64-82](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L64-L82)（`reparse_markup`）与 [src/parser.rs:749-755](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L749-L755)（`reparse_block`）的实现。
- 回顾 u4-l2 的「单 token 前瞻 + marker 事件式」解析原语，理解钩子为何能从一个任意字节偏移「接着解析」。
- 若想验证整体正确性，可通读 [src/reparser.rs:295-476](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L295-L476) 的测试套件——它对每条用例都断言「增量重解析的树 == 全量解析的树」，这正是增量算法正确性的终极保证。

# 编号 Span 与 numberize

## 1. 本讲目标

u6-l1 讲清楚了「编号怎么编码进 8 字节」，本讲接着回答下一个问题：**这些编号到底是按什么规则分配给 CST 里每个节点的？**

精读 `src/node.rs` 的 `numberize`，学完本讲你应当能够：

- 读懂 `Source::new` 里那行 `root.numberize(id, Span::FULL)` 做了什么，并理解 `SpanNumber` 与 `Span::FULL` 这两个约束。
- 手算 `InnerNode::numberize` 的「区间划分 + 取中点 + 故意挤左半区留余量」策略，解释它如何保证两条编号不变量：父节点编号 < 任意子节点编号；兄弟节点编号从左到右递增。
- 说明这两条不变量如何被 `find_number` 利用，做到「给定编号，快速在树里二分式剪枝定位节点」。
- 解释 `Unnumberable` 在什么时候发生（尤其是增量重编号时），以及 `InnerNode` 缓存的 `upper` 字段在其中扮演的角色。

## 2. 前置知识

本讲建立在以下已建立的认知之上：

- **`Span` 的位布局**（u6-l1）：`Span` 内部是 `NonZeroU64`，高 16 位是 `FileId`，低 48 位是 `number`；编号 span 的合法取值范围是 `Span::FULL = 2 .. 2^47`。本讲讲的正是这些 `number` 怎么被填进去。
- **`SyntaxNode` 的构造与形态**（u5-l1、u5-l2）：CST 节点有 `Leaf`（叶子）/ `Inner`（带 children 的结构节点）/ `Error` / `Warning` 四种形态；从零构造的节点 `span` 初始都是 `Span::detached()`，**等待 `numberize` 统一盖上真实编号**。`Inner` 形态的载荷是私有结构体 `InnerNode`，它缓存了 `len` / `descendants` / `diagnosis` / `upper` 等字段——本讲会用到其中的 `descendants` 和 `upper`。
- **`LinkedNode::find_number`**（u5-l3 已用过）：按编号反查节点的方法，依赖本讲要证明的两条不变量。
- **端到端数据流**（u1-l4 已预告）：`Source::new` 走「parse → numberize → 建行索引」三步，`numberize` 是其中给裸 CST「上户口」的一步。

如果你还不熟悉「为什么用编号而非字节范围」，请先回看 u6-l1 的 4.1 节——本讲默认你已认同「编号在编辑下稳定」这一动机。

## 3. 本讲源码地图

本讲主要读两个文件：

| 文件 | 本讲关注的内容 |
| --- | --- |
| [src/node.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs) | `SyntaxNode::numberize`、`InnerNode::numberize`、`InnerNode` 的 `descendants`/`upper` 字段、`replace_children` 的重编号循环、`find_number`、`NumberingResult`/`Unnumberable` |
| [src/span.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs) | `SpanNumber`、`Span::FULL`、`Span::from_number`、以及顶部关于「编号有序」的文档注释 |

辅助参考：

| 文件 | 关注点 |
| --- | --- |
| [src/source.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs) | `Source::new` 第 39 行那行 `root.numberize(id, Span::FULL).unwrap()`——`numberize` 的真实调用点 |

> 可见性提醒：`numberize` 是 `pub(super)`、`Span::number()` 与 `Span::FULL` 是 `pub(crate)`、`SpanNumber` 的字段也是 `pub(crate)`。它们都是 crate 内部机制，外部只能通过 `Source` 间接感受。本讲的代码实践因此定位为「在 crate 的测试模块里」观察，与 u6-l1 的做法一致。

## 4. 核心概念与源码讲解

### 4.1 全局入口：从 Source::new 到 SyntaxNode::numberize

#### 4.1.1 概念说明

`parse` 产出的裸 CST 只有问题：每个节点的 `span` 都是 `Span::detached()`（u5-l2 讲过「所有从零构造的节点 `span` 初始为 detached」）。detached span 不指向任何文件，下游（求值、诊断、IDE 跳转）没法用它定位源码。**`numberize` 的职责，就是给整棵树的每个节点盖上一个真实、唯一、有序的编号**，把 detached 换成带 `FileId` 的编号 span。

这件事发生在 `Source::new` 里，紧跟在 `parse` 之后：

[src/source.rs:36-41](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L36-L41)：`new` 先 `parse`，再 `numberize`，最后把「id + 文本 + 行索引 + 已编号的树」打包成 `Source`。

```rust
pub fn new(id: FileId, text: String) -> Self {
    let _scope = typst_timing::TimingScope::new("create source");
    let mut root = parse(&text);
    root.numberize(id, Span::FULL).unwrap();
    Self(Arc::new(LazyHash::new(SourceInner { id, lines: Lines::new(text), root })))
}
```

注意第 39 行的 `.unwrap()`：它假设 `numberize` 一定成功。这个假设是否成立，取决于 `Span::FULL` 给的编号空间够不够大——这正是本节要讲清的约束。

#### 4.1.2 核心流程

`numberize` 的两个入参定义了「上户口」的全部自由度：

- `id: FileId`——这些编号属于哪个文件（会被 `Span::from_number` 压进高 16 位）。
- `within: Range<u64>`——**允许使用的编号区间**。对一棵刚解析完的完整文件，这个区间就是 `Span::FULL`。

`Span::FULL` 与 `SpanNumber` 是两个互相呼应的约束：

[src/span.rs:86-87](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L86-L87)：`FULL` 是「源文件 span 编号的全部可用范围」。

```rust
/// The full range of numbers available for source file span numbering.
pub(crate) const FULL: Range<u64> = 2..(1 << 47);
```

注意起点是 **2**，不是 0 也不是 1——因为编号 1 留给了 detached 哨兵（u6-l1 的 4.3 节讲过）。终点是 \( 2^{47} \)，所以编号 span 一共有 \( 2^{47}-2 \approx 1.4\times 10^{14} \) 个可用槽位。

[src/span.rs:65-72](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L65-L72)：`SpanNumber` 是「一个 span 在其 `Source` 内的唯一编号」，文档保证它落在 `Span::FULL` 内。

```rust
/// The unique number of a span within its [`Source`](crate::Source). Known to
/// be within the range of `Span::FULL`.
#[derive(Debug, Copy, Clone, Eq, PartialEq)]
pub struct SpanNumber(pub(crate) u64);
```

现在能回答「`Source::new` 里 `.unwrap()` 安全吗」：一棵真实源文件的 CST 节点数顶多几万、几十万，远小于 \( 2^{47} \)，所以用整个 `Span::FULL` 区间给一棵新树编号**几乎不可能失败**。失败只可能发生在增量重解析时把编号挤进一个很窄的子区间（见 4.4 节），那时才需要处理 `Unnumberable`。

#### 4.1.3 源码精读

`numberize` 的对外入口定义在 `SyntaxNode` 上，逻辑很短：

[src/node.rs:516-531](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L516-L531)：`SyntaxNode::numberize`——区间为空就报错，内部节点委托给 `InnerNode::numberize`，叶子/错误节点直接取区间中点。

```rust
pub(super) fn numberize(
    &mut self,
    id: FileId,
    within: Range<u64>,
) -> NumberingResult {
    if within.start >= within.end {
        Err(Unnumberable)
    } else if let Some((inner, span)) = self.inner_and_span_mut() {
        inner.numberize(span, id, None, within)
    } else {
        self.span =
            Span::from_number(id, SpanNumber((within.start + within.end) / 2));
        Ok(())
    }
}
```

三个分支一句一句看：

1. **`within.start >= within.end`**：允许区间是空的（或退化），没法再分号，返回 `Err(Unnumberable)`。这是失败的最直接来源。
2. **内部节点**（`inner_and_span_mut` 返回 `Some`）：交给 `InnerNode::numberize` 处理，第三个参数 `None` 表示「编号整棵子树」（4.2 节会看到它还能只编号一段 children）。
3. **叶子/错误节点**（没有 children）：直接把区间中点 \(\lfloor(\text{start}+\text{end})/2\rfloor\) 作为自己的编号。「取中点」这个动作在每一层都会出现，是整个算法的基调。

> 「取中点」而非「取起点」是有意为之：取中点能给本节点两侧都留出余量，配合 4.2 节的「挤左半区」策略，最终让每个节点都待在自己区间的「中央」，编辑后更有可能保持稳定。

#### 4.1.4 代码实践

**实践目标**：确认「`Source::new` 之后，整棵树不再有 detached span」这件事确实发生了。

**操作步骤**：

1. 打开 [src/source.rs:36-41](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L36-L41)，确认 `numberize` 在 `parse` 之后、构造 `Source` 之前被调用。
2. 在 `src/node.rs` 末尾的 `#[cfg(test)] mod tests`（[src/node.rs:1481](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1481)）里临时加一个小测试，遍历 `Source::detached` 的 root，断言没有任何节点的 span 是 detached：

   ```rust
   #[test]
   fn practice_no_detached_after_new() {
       let root = crate::Source::detached("= Hi").root().clone();
       let mut all = vec![root];
       while let Some(node) = all.pop() {
           assert!(!node.span().is_detached(), "{:?} 仍是 detached", node.kind());
           all.extend(node.children().cloned());
       }
   }
   ```

3. 运行（**待本地验证**）：

   ```bash
   cargo test -p typst-syntax practice_no_detached_after_new
   ```

**需要观察的现象**：每个节点（包括 root）的 `span().is_detached()` 都返回 `false`，说明 `numberize` 给整棵树都盖上了真实编号。

**预期结果**：测试通过，断言无一触发。

#### 4.1.5 小练习与答案

**练习 1**：`Span::FULL` 为什么是 `2 .. 2^47`，而不是 `0 .. 2^48`？

> **答案**：编号 1 被预留为 detached 哨兵（`Span::DETACHED`，文件号为 0），所以可用编号从 2 开始；上界 \( 2^{47} \) 之上（\([2^{47}, 2^{48})\)）要腾给 external 文件范围与 range span 两个区段（u6-l1 的 4.3 节）。因此 numbered span 只能占用 \([2, 2^{47})\)。

**练习 2**：如果把 `Source::new` 第 39 行的 `.unwrap()` 换成 `if let Err(_) = ... { return ... }` 静默忽略，会出现什么后果？

> **答案**：失败时整棵树会保留一堆 detached span，下游用 `span.id()` 会拿到 `None`，导致诊断无法定位、IDE 跳转失效、记忆化缓存键退化。实际上对完整文件解析这几乎不会失败，所以用 `unwrap` 既安全又能尽早暴露逻辑错误。

---

### 4.2 核心算法：InnerNode::numberize 的区间划分与取中点

#### 4.2.1 概念说明

`SyntaxNode::numberize` 对叶子节点直接取中点就完事了；真正有技术含量的是**内部节点怎么把自己的编号区间 `within` 公平地分给「自己 + 所有后代」**。这就是 `InnerNode::numberize` 的工作。

它的核心难点是一个「既要又要」的约束：

- 编号必须满足两条单调不变量（父<子、兄弟递增）——这要求子节点的编号严格落在父节点之后、且按子树切成不重叠的连续段。
- 编号又要在编辑后尽量稳定——这要求**不要把区间塞满**，而是留出余量，好让后续局部重编号（U9）有空间插入新节点。

Typst 的解法是：先把本节点放在区间最前面（取中点），再把剩余空间按「每个子树的节点数」按比例切给各子树；并且**刻意只占用整个区间的一半**，把右半区空出来留给未来的插入。

#### 4.2.2 核心流程

`InnerNode::numberize` 的执行分四步。先约定记号：设本节点要编号的总节点数为 \( D \)（含自己），区间宽度 \( S = \text{within.end} - \text{within.start} \)。

**第 1 步：数清楚要编号多少个节点。**

当 `range` 是 `None`（编号整棵子树）时，\( D = \) `self.descendants`（本节点缓存的「整棵子树的节点数，含自己」）。`descendants` 字段在 `InnerNode::new` 构造时就算好了（[src/node.rs:656-668](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L656-L668)），是 \( 1 + \sum \) 子节点 `descendants`。

**第 2 步：算步长 `stride`——每个节点平均占多少编号宽度。**

\[
\text{stride} = \left\lfloor \frac{S}{2D} \right\rfloor
\]

除以 \( 2D \) 而不是 \( D \)，就是「挤左半区」：让 \( D \) 个节点总共只占 \( \text{stride}\cdot D = S/2 \) 的宽度，正好填满区间的**左半**，右半留空。若这个 `stride` 算出来是 0（区间太窄），退而求其次只用全宽、不留余量：

\[
\text{stride} = \left\lfloor \frac{S}{D} \right\rfloor \quad(\text{若仍为 0，则返回 Unnumberable})
\]

**第 3 步：给本节点编号。** 取 \(\text{start}\)（区间起点）往后一个 `stride` 的子区间 \([\text{start},\, \text{start}+\text{stride}]\) 的中点，并把 `upper` 字段记为 `within.end`（本子树编号的上界，4.4 节用）。然后 `start` 前进到 `start + stride`。

**第 4 步：给各子树编号。** 对每个子节点 \( c_i \)，按它的节点数 \( d_i \) 分配宽度 \( d_i \cdot \text{stride} \)，得到子区间，递归调用 `c_i.numberize(id, start..end)`。

用一张图概括区间是怎么切的（以 `within = [0, 64)`、\( D=8 \)、`stride=4` 为例，仅示意本节点与第一个子树）：

```
within = [0, 64)，D = 8，stride = 64 / (2*8) = 4

 bit: 0                                                        64
      | self |  child A 子树 (d=5 → 20)  | child B | child C | ← 右半区空闲 →
      |  4   |          20               |    4    |    4    |       32
      ↑      ↑                           ↑
    start  start+4                     ...

本节点编号 = [0,4] 的中点 = 2
子树 A 分到 [4, 24)，再递归内部按 stride=2 切给它的 3 个孩子……
注意：实际只用了 [0, 32)，[32, 64) 这「右半区」是留给未来插入的余量。
```

> 关键不变量由此自动成立：
> - 本节点编号 = \(\text{start} + \lfloor\text{stride}/2\rfloor < \text{start}+\text{stride} \le\) 第一个子区间起点 < 任意子节点编号 ⇒ **父 < 子**。
> - 各子区间 \([\text{start}_i, \text{end}_i)\) 首尾相接、互不重叠 ⇒ **兄弟从左到右递增**，且任一子树的全部编号都落在自己的区间内、小于下一个兄弟的编号。

#### 4.2.3 源码精读

`InnerNode::numberize` 完整实现（[src/node.rs:672-719](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L672-L719)）。四个步骤逐一对应：

**第 1 步——数节点数**（[src/node.rs:679-687](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L679-L687)）：`range=None` 时用 `self.descendants`；`range=Some(..)` 时只数被选中的那段 children（用于增量重编号，4.4 节）。

```rust
let descendants = match &range {
    Some(range) if range.is_empty() => return Ok(()),
    Some(range) => self.children[range.clone()]
        .iter()
        .map(SyntaxNode::descendants)
        .sum::<usize>(),
    None => self.descendants,
};
```

`SyntaxNode::descendants`（[src/node.rs:561-567](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L561-L567)）：叶子/错误返回 1，内部节点返回缓存的 `inner.descendants`。

**第 2 步——算步长**（[src/node.rs:689-699](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L689-L699)）：先试 \( S/(2D) \)，不行退到 \( S/D \)，再不行就 `Unnumberable`。注释把「挤左半区」的动机写得很清楚。

```rust
// Determine the distance between two neighbouring assigned numbers. If
// possible, we try to fit all numbers into the left half of `within`
// so that there is space for future insertions.
let space = within.end - within.start;
let mut stride = space / (2 * descendants as u64);
if stride == 0 {
    stride = space / self.descendants as u64;
    if stride == 0 {
        return Err(Unnumberable);
    }
}
```

> 注意一个细节：回退分支用的是 `self.descendants`（**整棵**子树的节点数）而非 `descendants`（可能只是被选中那段的节点数）。这是因为增量重编号时，`range=Some(..)` 只重做一段，但可用空间仍要按「整棵子树原本占多宽」来估算步长，避免新编号与未参与重编号的兄弟挤撞。`range=None` 时两者相等，无差别。

**第 3 步——给本节点编号**（[src/node.rs:701-708](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L701-L708)）：只在 `range.is_none()`（编号整棵子树的根）时执行；记下 `self.upper = within.end`，并把 `start` 往前推一个 `stride`。

```rust
let mut start = within.start;
if range.is_none() {
    let end = start + stride;
    *span = Span::from_number(id, SpanNumber((start + end) / 2));
    self.upper = within.end;
    start = end;
}
```

**第 4 步——递归给各子树编号**（[src/node.rs:710-716](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L710-L716)）：每个子节点按其 `descendants()` 比例分宽度，递归。

```rust
let len = self.children.len();
for child in &mut self.children[range.unwrap_or(0..len)] {
    let end = start + child.descendants() as u64 * stride;
    child.numberize(id, start..end)?;
    start = end;
}
Ok(())
```

子节点的 `numberize` 又会回到 4.1.3 的 `SyntaxNode::numberize`：叶子取中点落地，内部节点再次进入本函数——如此自顶向下递归，直到所有叶子都拿到编号。

`Span::from_number`（[src/span.rs:118-122](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L118-L122)）只是把 `FileId` 与编号打包进 8 字节（u6-l1 已详讲），`debug_assert` 编号落在 `FULL` 内：

```rust
pub(crate) const fn from_number(id: FileId, SpanNumber(number): SpanNumber) -> Self {
    debug_assert!(Self::FULL.start <= number);
    debug_assert!(number < Self::FULL.end);
    Self::pack(id, number)
}
```

#### 4.2.4 代码实践

**实践目标**：用一段已知结构的文本，**手算**一遍编号，再对照程序输出验证。

**操作步骤**：

1. 先看 `test_debug`（[src/node.rs:1487-1504](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1487-L1504)）确认 `"= Head <label>"` 的 CST 结构与各节点 `descendants`：

   ```
   Markup(root, D=8)
   ├─ Heading(D=5)
   │  ├─ HeadingMarker "="(1)
   │  ├─ Space " "(1)
   │  └─ Markup(D=2)
   │     └─ Text "Head"(1)
   ├─ Space " "(1)
   └─ Label "<label>"(1)
   ```

2. 在 `node.rs` 的 `#[cfg(test)] mod tests` 里临时加一个测试，**故意用小区间 `0..64`** 编号，让数字便于阅读（生产代码用的是 `Span::FULL`，数字会大到无法目视）：

   ```rust
   #[test]
   fn practice_numberize_invariants() {
       let id = crate::Source::detached("= Head <label>").id();
       let mut root = crate::parse("= Head <label>");
       root.numberize(id, 0..64u64).unwrap();

       fn walk(node: &crate::SyntaxNode, depth: usize) {
           println!(
               "{}{:?}: number={}",
               "  ".repeat(depth),
               node.kind(),
               node.span().number()
           );
           let nums: Vec<u64> = node.children().map(|c| c.span().number()).collect();
           // 不变量 1：父节点编号严格小于任意子节点
           for &cn in &nums {
               assert!(node.span().number() < cn, "父不小于子");
           }
           // 不变量 2：兄弟节点编号从左到右严格递增
           for w in nums.windows(2) {
               assert!(w[0] < w[1], "兄弟未递增");
           }
           for child in node.children() {
               walk(child, depth + 1);
           }
       }
       walk(&root, 0);
   }
   ```

3. 运行并打印（**待本地验证**）：

   ```bash
   cargo test -p typst-syntax practice_numberize_invariants -- --nocapture
   ```

**需要观察的现象**：按 4.2.2 的策略手算，`within=0..64`、`D=8`、`stride=4`，应得到如下编号（root 取 \([0,4]\) 中点 2；Heading 子树分到 \([4,24)\)、内部 `stride=2`；以此类推）：

```
Markup: number=2
  Heading: number=5
    HeadingMarker: number=7
    Space: number=9
    Markup: number=10
      Text: number=11
  Space: number=26
  Label: number=30
```

**预期结果**：程序输出与上面一致；两条不变量的 `assert` 全部通过。注意 Heading 整棵子树的最大编号 11 仍小于下一个兄弟 Space 的 26——这正是「子树整体小于下一个兄弟」的体现。

> 如果把 `0..64u64` 改回真实的 `crate::Span::FULL`，输出数字会变成 \( 10^{13} \) 量级，但两条不变量依旧成立——变的只是绝对大小，不变的是相对顺序。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `stride` 的首选公式是 \( S/(2D) \) 而不是 \( S/D \)？少除的那个 2 有什么用？

> **答案**：除以 \( 2D \) 让 \( D \) 个节点总共只占 \( S/2 \) 的宽度，即只填满区间的**左半**，右半留空。这片预留空间是给增量重解析用的：当你在某处插入新节点、需要在该父节点的编号区间里重编号时，右半区的余量让新编号往往不必挤撞兄弟就能放下，从而**让更多旧节点的编号保持不变**，保住 memoization 缓存。这正是 u6-l1 反复强调的「编号稳定」动机在分配层的落实。

**练习 2**：在上面的 `0..64` 例子里，为什么 Text 的编号是 11 而不是某个更大的数？请从「区间不断被切分」的角度解释。

> **答案**：root 把 \([0,64)\) 的左半 \([0,32)\) 用掉，Heading 分到 \([4,24)\)；Heading 内部 `stride=2`，把它内部 Markup 子树分到 \([10,14)\)；该 Markup 内部 `stride=1`，把自己编在 \([10,11]\) 中点 10，把 Text 分到 \([11,12)\)，Text 取中点 11。每一层都是「取自己那段的最前面一小段的中间」，所以深层叶子的编号是被层层切剩下的「碎区间」的中点。

**练习 3**：若把测试里的区间从 `0..64` 改成 `0..4`（仍对同一棵 \( D=8 \) 的树），会发生什么？为什么？

> **答案**：`stride = 4/(2*8) = 0`；回退 `stride = 4/8 = 0`；于是返回 `Err(Unnumberable)`，`.unwrap()` 会 panic。原因是要给 8 个节点编号，至少需要宽度 8（每个节点至少占 1），而 `0..4` 宽度只有 4，装不下。这正是 4.4 节要讨论的失败情形。

---

### 4.3 两条编号不变量与 find_number 的快速定位

#### 4.3.1 概念说明

花这么大力气设计「区间划分 + 取中点」，最终目的是让编号满足两条**单调不变量**，使得「给定一个编号，快速在 CST 里找到对应节点」成为可能。这两条不变量写在 `span.rs` 顶部的文档里：

[src/span.rs:52-61](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L52-L61)：编号在树里有序——父<子、兄弟递增；合起来就是「兄弟序列 \([A,B,C]\) 中，A 及其全部后代的编号都小于 B，B 及其全部后代都小于 C」。

```
- The span number of a parent node is always smaller than the number of any of its children
- The span numbers of sibling nodes always increase from left to right
```

`find_number` 就是直接吃这两条不变量的方法：你给它一个 `SpanNumber`，它能在树里高效定位到那个节点。它被 `LinkedNode::find` → `Source::find` 调用，是 IDE「点一下跳到定义」、诊断「把 span 映射回字节范围」等功能的反查基础（u5-l3、u8-l1 已铺垫）。

#### 4.3.2 核心流程

`find_number(self, target)` 的剪枝逻辑分三层：

1. **命中**：本节点编号 == target，直接返回自己。
2. **早退**：target < 本节点编号。因为「父<子」，本节点的整个子树编号都 > 本节点 > target，target 绝不可能在本子树里 ⇒ 返回 `None`。
3. **递归剪枝**：本节点是内部节点且 target > 本节点编号时，遍历各子节点。对子节点 \( c_i \)，**只有当「下一个兄弟 \( c_{i+1} \) 的编号 > target」时才递归** \( c_i \)。理由：\( c_i \) 整棵子树的编号都 < \( c_{i+1} \) 的编号；若 \( c_{i+1} \) 的编号已经 ≤ target，说明 target 落在 \( c_{i+1} \) 或更右边，\( c_i \) 子树里不可能有，直接跳过。这就是「用下一个兄弟的编号当本子树的上界」的二分式剪枝。

直观地说，它把「兄弟递增」这条不变量当成了**隐式的二分索引**：每个兄弟的编号是它左边那整块子树的「上界 fence」，比较一次就能砍掉一整块子树。

#### 4.3.3 源码精读

`find_number`（[src/node.rs:1124-1155](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1124-L1155)），三层逻辑与上面一一对应：

```rust
pub(crate) fn find_number(&self, target: SpanNumber) -> Option<Self> {
    let number = self.span().number();
    if number == target.0 {
        return Some(self.clone());          // ① 命中
    }

    // The parent of a subtree has a smaller span number than all of its
    // descendants. Therefore, we can bail out early if the target span's
    // number is smaller than our number.
    if self.node.is_inner() && number < target.0 {
        let mut children = self.children().peekable();
        while let Some(child) = children.next() {
            // Every node in this child's subtree has a smaller span number than
            // the next sibling. Therefore we only need to recurse if the next
            // sibling's span number is larger than the target span's number.
            if children.peek().is_none_or(|next| next.span().number() > target.0)
                && let Some(found) = child.find_number(target)
            {
                return Some(found);         // ③ 用下一个兄弟当上界剪枝后递归
            }
        }
    }

    None                                     // ② 早退 / 没找到
}
```

几个要点：

- **早退条件**藏在 `if self.node.is_inner() && number < target.0` 里：只有「是内部节点」**且**「target 比本节点大」才进入子节点遍历。若 target < number，条件不成立，直接落到末尾 `None`——这就是早退。
- **`children.peek()` 拿到的是「下一个兄弟」**。`is_none_or` 处理最后一个孩子（`peek()` 为 `None` 时返回 `true`，即「无上界约束，必须递归」）。
- 这个 `children()` 来自 `LinkedNode`（[src/node.rs:1138-1152](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1138-L1152) 注释里强调用 `self.children()` 而非 `inner.children()`，是为了保持在 `LinkedNode` 上下文里、保留父链），所以 `find_number` 返回的也是带父指针的 `LinkedNode`，下游能继续 `range()` / `parent()`。

`find_number` 的入口是 `find`（[src/node.rs:1115-1122](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1115-L1122)），它按 `SpanKind` 分派：编号 span 走 `find_number`，range span 走 `find_range`，detached 直接 `None`。

#### 4.3.4 代码实践

**实践目标**：用 4.2.4 那棵手算出来的树，验证 `find_number` 能按编号精确命中每个节点，并体会「下一个兄弟当上界」的剪枝。

**操作步骤**：

1. 在 `node.rs` 的测试模块里，复用 4.2.4 用 `0..64` 编号的 root，取一个已知编号（例如 Text 的 11），用 `LinkedNode::new(&root).find(span)` 反查：

   ```rust
   #[test]
   fn practice_find_number() {
       let id = crate::Source::detached("= Head <label>").id();
       let mut root = crate::parse("= Head <label>");
       root.numberize(id, 0..64u64).unwrap();

       // 用公开的 children() 手动遍历 CST，找到 Text 节点的 span
       // （它的编号应为 11，见 4.2.4）
       let mut text_span = None;
       let mut stack = vec![&root];
       while let Some(node) = stack.pop() {
           if node.kind() == crate::SyntaxKind::Text {
               text_span = Some(node.span());
           }
           stack.extend(node.children());
       }
       let text_span = text_span.unwrap();

       let linked = crate::LinkedNode::new(&root).find(text_span);
       assert!(linked.is_some(), "应能按编号找到 Text");
       assert_eq!(linked.unwrap().kind(), crate::SyntaxKind::Text);
   }
   ```

   > 说明：`LinkedNode::new` / `find`、`SyntaxNode::children` / `kind` / `span` 都是公开 API（分别在 [src/node.rs:1081](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1081)、[src/node.rs:1116](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1116)、[src/node.rs:278](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L278)、[src/node.rs:216](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L216)、[src/node.rs:240](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L240)）。注意 `SyntaxNode::descendants()` 返回的是 `usize`（节点计数）而非迭代器，所以遍历树要用 `children()` 递归。

2. 运行（**待本地验证**）：

   ```bash
   cargo test -p typst-syntax practice_find_number
   ```

**需要观察的现象**：给定 Text 的 span，`find` 经 `find_number` 的剪枝后，**只在 Heading 子树里递归**（因为 target=11 < 下一个顶层兄弟 Space 的 26，故 Heading 之后 Space、Label 整棵被跳过），最终命中 Text。

**预期结果**：测试通过；`linked.kind()` 是 `Text`。可以额外在 `find_number` 里临时加一行 `println!` 打印每次比较的节点 kind，观察 Space、Label 根本没被递归进入——这就是剪枝的收益。

#### 4.3.5 小练习与答案

**练习 1**：在 `find_number` 里，如果删掉「`children.peek().is_none_or(|next| next.span().number() > target.0)`」这个判断、对每个子节点都无条件递归，结果还对吗？会有什么损失？

> **答案**：结果仍正确（最终总能找到或返回 `None`），但会**失去剪枝**：明明 target 已知小于某个兄弟、不可能在该兄弟及其右边的子树里，却还要把它们全遍历一遍，最坏退化为整棵树的线性扫描。加上判断后，每次比较都能砍掉一整块子树，接近二分查找的效率。

**练习 2**：为什么 `find_number` 在 `number > target` 时可以直接返回 `None`，而不用担心 target 藏在「某个编号更小的祖先」里？

> **答案**：因为调用方是从 root 开始自顶向下找的。`number > target` 说明 target 比当前节点小，而「父<子」保证当前节点的所有后代编号都 > 当前节点 > target，所以 target 不可能在当前子树里。至于「编号更小的祖先」，它们在自顶向下的过程中已经被逐层经过并比较过了，不会回到更上面去。

---

### 4.4 Unnumberable：何时编号失败，以及 upper 字段如何服务增量重编号

#### 4.4.1 概念说明

前面一直说 `Source::new` 里 `numberize` 「几乎不会失败」。那 `Unnumberable` 到底什么时候才真正出现？答案在**增量重解析**（U9）：当用户编辑文本后，Typst 不会重 parse 整个文件，而是只替换 CST 里受影响的一段 children，然后**只对这一小段重新编号**。这时允许区间 `within` 是从父节点原有编号空间里抠出来的一个很窄的子区间，`stride` 可能算成 0，编号就会失败。

所以 `Unnumberable` 不是「程序出错」，而是「这个窄区间装不下需要重新编号的节点」的信号。收到这个信号后，`replace_children` 会**向两侧指数级扩大**重编号范围再试；若扩到整棵父子树仍失败，才把 `Unnumberable` 一路上抛，触发 U9 的 `try_reparse` 兜底——回退到全量 `parse + numberize`。

这个机制能成立，靠的是 `InnerNode` 缓存的 `upper` 字段：它记录了「本子树编号区间的上界」，让重编号时能精确算出可用区间。

#### 4.4.2 核心流程

先看三个相关定义：

- **`NumberingResult`**（[src/node.rs:1466-1467](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1466-L1467)）：就是 `Result<(), Unnumberable>`——编号要么成功（无返回值），要么返回 `Unnumberable`。
- **`Unnumberable`**（[src/node.rs:1469-1479](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1469-L1479)）：一个空的标记类型，`Display` 实现固定输出 `"cannot number within this interval"`，并实现了 `std::error::Error`。

```rust
pub(super) type NumberingResult = Result<(), Unnumberable>;

/// Indicates that a node cannot be numbered within a given interval.
#[derive(Debug, Copy, Clone, Eq, PartialEq)]
pub(super) struct Unnumberable;
```

- **`upper` 字段**（[src/node.rs:648-649](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L648-L649)）：`InnerNode` 的字段，含义是「本节点编号区间的上界」。它在 4.2.3 的第 3 步被写入 `self.upper = within.end`。

读取它的是 `SyntaxNode::upper`（[src/node.rs:606-612](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L606-L612)：

```rust
pub(super) fn upper(&self) -> u64 {
    match self.node_ref() {
        NodeRef::Leaf(_) | NodeRef::Error(_) => self.span.number() + 1,
        NodeRef::Inner(inner) => inner.upper,
    }
}
```

即：叶子的「上界」是它的编号 +1（叶子没有子树，上界就是自己编号的下一个）；内部节点的上界是当初 `numberize` 记下的 `within.end`。

**增量重编号的区间计算**（`replace_children` 内，[src/node.rs:800-844](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L800-L844)）：

1. 算**下界** `start_number`：重编号段左边那个兄弟的 `upper()`；若从第一个孩子开始重编号，则用本节点编号 +1（[src/node.rs:809-818](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L809-L818)）。
2. 算**上界** `end_number`：重编号段右边第一个兄弟的编号；若到最后一个孩子，用本节点的 `upper`（[src/node.rs:820-828](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L820-L828)）。
3. 用 `within = start_number..end_number` 调 `self.numberize(span, id, Some(renumber), within)`（[src/node.rs:831-832](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L831-L832)）。
4. 若返回 `Ok`，成功；若 `Err` 且已扩到极限，返回 `Err(Unnumberable)` 上抛；否则**指数级扩大** `left`/`right` 再试（[src/node.rs:836-843](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L836-L843)）。

这里正是 4.2 节「挤左半区」红利的兑现处：因为当初每棵子树都只占了自己区间的一半，`upper` 与下一个兄弟编号之间通常留有大片空隙，所以即便插入新节点，多数时候不扩大范围就能重编号成功——旧节点的编号尽量不动，缓存得以复用。

#### 4.4.3 源码精读

`replace_children` 的重编号循环把上面的流程写得很紧凑（[src/node.rs:800-844](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L800-L844)）。关键是下界与上界的两段计算，它们完全依赖 `upper()` 与兄弟的 `span().number()`：

```rust
// 下界：左边兄弟的 upper，或本节点编号+1
let start_number = renumber
    .start
    .checked_sub(1)
    .and_then(|i| self.children.get(i))
    .map_or(span.number() + 1, |child| child.upper());

// 上界：右边第一个兄弟的编号，或本节点的 upper
let end_number = self
    .children
    .get(renumber.end)
    .map_or(self.upper, |next| next.span().number());

let within = start_number..end_number;
if self.numberize(span, id, Some(renumber), within).is_ok() {
    return Ok(());
}

if left == max_left && right == max_right {
    return Err(Unnumberable);
}

// 指数级扩大到两侧
left = (left + 1).next_power_of_two().min(max_left);
right = (right + 1).next_power_of_two().min(max_right);
```

注意第 3 步调用的是 `InnerNode::numberize` 的 **`range=Some(renumber)`** 分支（4.2.3 第 1 步见过）：它只对 `renumber` 这段 children 重新编号，不动其余兄弟——这正是「局部重编号」得以实现的关键。`numberize` 内部用 `?` 传播 `Unnumberable`：任一子节点编号失败，整段失败，由外层循环扩大范围重试。

> 本节只是为 `Unnumberable` 与 `upper` 建立认知、点明它们为增量重解析服务；完整的重解析调用链（`Source::edit → reparse → try_reparse`，失败回退全量解析）是 U9 三篇讲义的主题，本讲不展开。

#### 4.4.4 代码实践

**实践目标**：亲手触发一次 `Unnumberable`，理解它「不是 bug，而是区间装不下」的语义。

**操作步骤**：

1. 在 `node.rs` 测试模块里，对 \( D=8 \) 的同一棵树，故意给一个**宽度不足**的区间，观察 `numberize` 返回 `Err`：

   ```rust
   #[test]
   fn practice_unnumberable() {
       let id = crate::Source::detached("= Head <label>").id();
       let mut root = crate::parse("= Head <label>");
       // 这棵树有 8 个节点，至少需要宽度 8；0..4 装不下
       let res = root.numberize(id, 0..4u64);
       assert!(res.is_err(), "窄区间应返回 Unnumberable");
       println!("numberize 返回: {:?}", res);
   }
   ```

2. 运行（**待本地验证**）：

   ```bash
   cargo test -p typst-syntax practice_unnumberable -- --nocapture
   ```

**需要观察的现象**：`numberize` 返回 `Err(Unnumberable)`，打印显示 `cannot number within this interval`。树没有被破坏（`numberize` 失败时不写入任何编号，root 仍是 detached）。

**预期结果**：`res.is_err()` 为真。这说明 `Unnumberable` 是一个**可恢复的、被预期的**结果——U9 的重解析逻辑正是靠捕获它来决定「扩大范围重试 / 回退全量解析」。

#### 4.4.5 小练习与答案

**练习 1**：`upper` 字段为什么对叶子定义成 `span.number() + 1`，而对内部节点是当初记下的 `within.end`？

> **答案**：叶子没有子树，它的「编号区间」就是它自己那一个编号，上界自然是「编号 +1」。内部节点在 `numberize` 时拿到了一整段 `within`，它的子树编号会铺满该区间的左半，但**区间本身（含未用的右半）**都归这个内部节点管辖，所以上界记为 `within.end`。重编号时，`upper` 让我们能算出「这个子树之后还剩多少编号空间可用」。

**练习 2**：为什么 `replace_children` 失败后选择「指数级扩大」重编号范围，而不是直接放弃、立刻回退全量解析？

> **答案**：扩大范围能让 `numberize` 拿到更宽的 `within`，从而让 `stride` 不再为 0，往往就能成功——这样仍只重编号局部，大部分旧节点的编号不变，缓存命中率最高。只有扩到整棵父子树都装不下时才认输回退全量解析。指数级扩大（`next_power_of_two`）保证重试次数是对数级，摊销代价很低。这是「尽量局部、实在不行才全量」的工程取舍。

---

## 5. 综合实践

**任务**：把本讲四块知识——「入口与 `Span::FULL`」「区间划分取中点」「两条不变量与 `find_number`」「`Unnumberable` 与 `upper`」——串成一份「编号生命周期」说明书。

请完成以下 deliverable：

1. **画一张编号分配流程图**：以 `"= Head <label>"`、`within=0..64` 为例，画出 root 如何把 \([0,64)\) 的左半 \([0,32)\) 分配给「自己（取中点 2）+ Heading 子树 + Space + Label」，再画出 Heading 子树内部如何递归切分。在每个节点旁标出它的编号与它分到的子区间。
2. **写一份不变量证明**：用 4.2.2 给出的「本节点取 \(\text{start}+\lfloor\text{stride}/2\rfloor\)，子节点从 \(\text{start}+\text{stride}\) 开始」这一事实，严格推出「父<子」；再用「子区间首尾相接」推出「兄弟递增」与「任一子树整体小于下一兄弟」。
3. **回答一个综合问题**：假设用户在 Heading 里插入了一个新节点，触发 `replace_children` 对 Heading 的 children 局部重编号。请说明 `start_number` 和 `end_number` 分别由什么算出（用到 `upper` 还是兄弟编号？），为什么 4.2 节「挤左半区」让这次重编号很可能**不必扩大范围**就能成功，以及万一失败的兜底路径。

**参考答案要点**：

- 流程图：root \([0,64)\) 取中点 2、`upper=64`；children 区间为 Heading \([4,24)\)（编号 5）、Space \([24,28)\)（编号 26）、Label \([28,32)\)（编号 30）；\([32,64)\) 留空。Heading 内部 `stride=2`，自编号 5、`upper=24`，三个孩子依次 \(7,9\) 与 Markup 子树 \([10,14)\)。
- 证明：本节点编号 \(=\text{start}+\lfloor\text{stride}/2\rfloor < \text{start}+\text{stride}\le\) 第一个子区间起点 \(\le\) 任一子节点编号 ⇒ 父<子。子区间 \([\text{start}_i,\text{end}_i)\) 满足 \(\text{end}_i=\text{start}_{i+1}\) 且子树编号全在 \([\text{start}_i,\text{end}_i)\) 内 ⇒ 兄弟递增、且子树 \(i\) 整体 \(<\) 子树 \(i+1\)。
- 综合问题：`start_number` 取 Heading 第一个重编号孩子左边兄弟的 `upper()`（或 Heading 编号+1），`end_number` 取重编号段右边第一个兄弟的编号（或 Heading 的 `upper`）。因为当初 Heading 子树只占了自己 \([4,24)\) 的左半，右半留空，新节点通常能塞进这片余量，`stride` 不为 0，故不必扩大。若余量仍不够（`stride=0`），`numberize` 返回 `Unnumberable`，`replace_children` 指数级扩大范围重试；扩到极限仍失败则上抛，由 U9 的 `try_reparse` 回退全量 `parse + numberize`。

> 想跑代码佐证，可把 4.2.4、4.3.4、4.4.4 三个临时测试一起放进 `node.rs` 的 `#[cfg(test)] mod tests`，用 `cargo test -p typst-syntax practice_ -- --nocapture` 观察「成功编号 / find 命中 / Unnumberable」三类输出（**待本地验证**）。

## 6. 本讲小结

- `numberize` 在 `Source::new` 里紧跟 `parse` 之后被调用（[src/source.rs:39](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L39)），用 `Span::FULL = 2..2^47` 这整个区间给新树的每个节点盖上唯一编号，把 detached 换成真实 span；区间足够大，故 `.unwrap()` 安全。
- 核心算法在 `InnerNode::numberize`：先数节点数 \(D\)，算步长 \(\text{stride}=\lfloor S/(2D)\rfloor\)（刻意只填左半、留右半给未来插入）；本节点取区间最前一小段的中点；再把剩余宽度按各子树节点数 \(d_i\cdot\text{stride}\) 按比例切给子节点递归。
- 该策略自动保证两条编号不变量——**父<子**、**兄弟从左到右递增**（进而「任一子树整体小于下一兄弟」），由 [src/span.rs:52-61](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L52-L61) 的文档固定为契约。
- `find_number` 吃这两条不变量做二分式剪枝：「target 小于本节点」早退、「下一个兄弟编号当本子树上界」跳过整块子树，使按编号反查节点接近对数代价。
- 失败用 `NumberingResult = Result<(), Unnumberable>` 表达：区间太窄、`stride` 算成 0 时返回 `Unnumberable`。它主要发生在增量重编号（`replace_children`）里；`InnerNode.upper` 字段记录子树编号上界，使重编号能算出可用区间并在失败时指数级扩大，实在不行才触发 U9 的全量回退。

## 7. 下一步学习建议

本讲讲完了「编号怎么分配」与「为什么这样分配能支撑快速反查与增量稳定」。建议按顺序继续：

- **u6-l3 DiagSpan、SubRange 与外部范围**：把 span 系统的最后一块拼图补齐——`DiagSpan`/`DiagSpanKind` 如何表达外部文件范围、`SubRange` 如何在节点内指向子区间、`Source::range` 如何结合本讲的 `SpanNumber` 与 `find_number` 把编号反查成字节范围。
- **U9 增量重解析（u9-l1/u9-l2/u9-l3）**：本讲 4.4 节只是为 `Unnumberable` 与 `upper` 埋下伏笔；完整的 `Source::edit → reparse → try_reparse` 调用链、`overlapping_children` 如何定位受影响子节点、失败回退全量解析，都在 U9 详讲。
- **想立刻看反查的实际用法**：跳到 **u8-l1** 的 `Source::find(span)`，它内部就是调用本讲的 `find_number`，把一个 span 映射回带父指针的 `LinkedNode`。

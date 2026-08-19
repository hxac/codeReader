# SumTree 集成：前缀和树如何支撑 O(log n) 查询

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `sum_tree::Item` / `Summary`（rope 用它的无上下文变体 `ContextLessSummary`）/ `Dimension` 三个 trait 各自的契约与分工，并定位 rope 为 `Chunk` / `ChunkSummary` 实现它们的具体代码。
2. 逐部件读懂 `self.chunks.find::<usize, _>((), &offset, Bias::Left)` 这类调用：泛型 `D` 是什么、`()` 从哪来、`Bias` 决定什么、返回的三元组 `(start, end, item)` 各是什么。
3. 解释 `Dimensions<usize, Point>` 这样的**元组维度**如何让一次树下降同时累计两种坐标，从而使 `offset_to_point` 这类换算只需一次 O(log n) 查找加一次块内位图运算。
4. 理解 rope 自己的 `TextDimension` trait 为什么存在（它比 `Dimension` 多出的两个构造器解决什么问题），进而解释 `Cursor::summary::<D>` 为什么能返回任意维度；同时读懂 `DimensionPair` 的「key 比较、value 搭车」设计。

本讲是 u2-l2 的直接续篇：u2-l2 证明了 `(TextSummary, +=)` 是一个 monoid，本讲回答「这个 monoid 挂在什么数据结构上、怎么被查询」。

## 2. 前置知识

### 2.1 从 u2-l2 带过来的三个结论

- **`TextSummary` 是可拼的**：两段文本各自的摘要能 O(1) 合并成拼接文本的摘要（`AddAssign` 合并代数），空摘要是单位元——完整的 monoid。
- **`Rope` 只有一个字段**：`chunks: SumTree<Chunk>`（[rope.rs:L25-L28](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L25-L28)）。u2-l2 已经看过 `Rope::summary()` 读的是树根缓存，但「树」本身长什么样一直没有展开——这就是本讲的主角。
- **块内统计是位图 popcount**：`ChunkSlice` 的四张位图让单块的 `TextSummary` 不用逐字符扫描（细节在 u2-l4 展开，本讲只在挂点处引用）。

### 2.2 前缀和：从数组到树

先建立「前缀和树」的直觉。给定数组 \( a_0, a_1, \dots, a_{n-1} \)，前缀和数组定义为

\[ \text{prefix}[i] = a_0 + a_1 + \dots + a_{i-1} \]

有了它，「前 i 项的和」是 O(1) 查表。但编辑器文本是**频繁修改**的：在中间插一项，后面所有前缀和都要重算，O(n)。SumTree 的思路是把前缀和搬到一棵多叉树上：**每个内部节点缓存「以它为根的子树里所有元素的和」**。于是：

- 改一个元素：只有它到根的路径上的缓存需要重算，路径长度 \( O(\log n) \)；
- 查「某个位置落在哪个元素里」：从根往下走，每层用子节点的缓存判断该往哪个孩子走，同样是 \( O(\log n) \)。

设每个节点最多容纳 \( B \) 个孩子（zed 里 \( B = 2 \times \text{TREE\_BASE} = 12 \)，测试配置另行缩小），\( n \) 个元素的树高约为 \( \lceil \log_B n \rceil \)，一次查找的比较次数量级为

\[ T_{\text{find}} = O(B \cdot \log_B n) \]

\( B \) 是常数，所以就是 \( O(\log n) \)。直观感受一下规模：1 GiB 的文本按每块约 125 字节算约 860 万个块，\( \log_{12}(8.6 \times 10^6) \approx 7 \)——定位任意字节只走约 7 层、每层至多 12 次累加比较。

### 2.3 sum_tree 是「泛型容器 + 三个 trait」

SumTree 本身不知道什么是文本。它是 `crates/sum_tree` 提供的通用 B+ 树，容器 `SumTree<T>` 只要求 `T: Item`，而「元素如何报摘要、摘要如何合并、按什么坐标导航」全部由使用方通过 trait 声明。本讲要啃的正是 rope 写下的这份「接入声明」。读代码时会遇到三个角色，先给一句话预告（4.1、4.2 展开）：

| trait | 一句话职责 | rope 侧的实现者 |
|---|---|---|
| `Item` | 「我是元素，这是我的摘要」 | `Chunk` |
| `Summary` / `ContextLessSummary` | 「摘要如何从零开始、如何累加」 | `ChunkSummary`（包着 `TextSummary`） |
| `Dimension` | 「把摘要投影成我要的坐标系，供导航与汇总」 | `usize` / `Point` / `OffsetUtf16` / `PointUtf16` / `TextSummary` 等 |

### 2.4 需要的 Rust 背景

关联类型（`type Summary`）、trait bound（`where D: Dimension<'a, S>`）、以及「为外部类型实现外部 trait」——`impl sum_tree::Item for Chunk` 与 `impl<'a> sum_tree::Dimension<'a, ChunkSummary> for usize` 都是这种桥接实现，两边的类型都不属于 rope。泛型参数由调用处的 `::<...>` 指定或由编译器推断，这是读懂 `find::<usize, _>` 这类调用的前提。

## 3. 本讲源码地图

| 文件 | 行号 | 内容 | 作用 |
|---|---|---|---|
| `crates/rope/src/rope.rs` | L14 | `use sum_tree::{Bias, Dimension, Dimensions, SumTree}` | rope 消费的 sum_tree API 面 |
| `crates/rope/src/rope.rs` | L25-L28 | `struct Rope { chunks: SumTree<Chunk> }` | 整个 rope 就是一棵 Chunk 树 |
| `crates/rope/src/rope.rs` | L1255-L1263 | `impl sum_tree::Item for Chunk` | 元素挂点：块 → 块摘要 |
| `crates/rope/src/rope.rs` | L1265-L1278 | `ChunkSummary` + `ContextLessSummary` | 摘要类型及其累加规则 |
| `crates/rope/src/rope.rs` | L1441-L1466 | `TextDimension` trait 及 `Dimensions` 的实现 | rope 自己的维度抽象 |
| `crates/rope/src/rope.rs` | L1468-L1589 | 五个 `Dimension` / `TextDimension` 实现 | `TextSummary`/`usize`/`OffsetUtf16`/`Point`/`PointUtf16` |
| `crates/rope/src/rope.rs` | L1591-L1725 | `DimensionPair<K, V>` | 「key 导航、value 搭车」的命名维度 |
| `crates/rope/src/rope.rs` | L369-L534 | 坐标换算全家 | `find` / `cursor` 的最大消费现场 |
| `crates/rope/src/rope.rs` | L678-L784 | rope 的 `Cursor` | 对 `sum_tree::Cursor` 的包装 |
| `crates/rope/src/rope.rs` | L312-L334 | `summary` / `len` / `max_point` / `cursor` | O(1) 查询入口 |
| `crates/rope/src/chunk.rs` | L16-L33 | `Chunk` 结构 | 树上的元素本体 |
| `crates/rope/src/chunk.rs` | L316-L351 | `text_summary` / `len` / `len_utf16` / `lines` | 块摘要的位图来源 |
| `crates/sum_tree/src/sum_tree.rs` | L15-L18 | `TREE_BASE` | 节点扇出基数（测试/正式不同值） |
| `crates/sum_tree/src/sum_tree.rs` | L34-L38 | `trait Item` | 元素契约 |
| `crates/sum_tree/src/sum_tree.rs` | L51-L72 | `Summary` / `ContextLessSummary` 及 blanket impl | 摘要契约 |
| `crates/sum_tree/src/sum_tree.rs` | L95-L130 | `Dimension` / `SeekTarget` | 投影与比较契约 |
| `crates/sum_tree/src/sum_tree.rs` | L138-L165 | `Dimensions<D1, D2, D3>` | 元组维度及其 SeekTarget |
| `crates/sum_tree/src/sum_tree.rs` | L167-L204 | `Bias` | 边界贴左 / 贴右 |
| `crates/sum_tree/src/sum_tree.rs` | L206-L213 | `struct SumTree<T>` | B+ 树本体与文档 |
| `crates/sum_tree/src/sum_tree.rs` | L424-L507 | `find` / `find_iterate` | 一次性查找的下降算法 |
| `crates/sum_tree/src/sum_tree.rs` | L597-L605, L723-L741 | `cursor` / `extent` / `summary` | 树的其余入口 |
| `crates/sum_tree/src/cursor.rs` | L30-L37 | `struct Cursor` | 可复用游标 |
| `crates/sum_tree/src/cursor.rs` | L82-L115 | `start` / `end` / `item` | 游标读取 API |
| `crates/sum_tree/src/cursor.rs` | L408-L460 | `seek` / `seek_forward` / `slice` / `suffix` / `summary` | 游标前进与汇总 |
| `crates/sum_tree/src/cursor.rs` | L819-L841 | `SeekAggregate for SummarySeekAggregate` | `Cursor::summary` 折叠任意维度的机制 |

## 4. 核心概念与源码讲解

### 4.1 Item 与 Summary：Chunk 挂上树的两个挂点

#### 4.1.1 概念说明

`SumTree<T>` 对元素只有一个要求：实现 `Item` trait。这个 trait 小得惊人——只有一个关联类型和一个方法：

```rust
pub trait Item: Clone {
    type Summary: Summary;
    fn summary(&self, cx: <Self::Summary as Summary>::Context<'_>) -> Self::Summary;
}
```

翻译成人话：「每个元素必须能报出自己的摘要，摘要类型必须是可累加的」。树上每个叶子节点除了存元素本身，还存每个元素的 `item_summary`；每个内部节点存「子树摘要」。插入、追加、更新元素时，SumTree 沿路径调用 `Item::summary` 重算受影响节点的缓存——这就是 u2-l2 说的「编辑后统计仍然 O(log n)」的实现现场。

`Summary` trait 则是摘要类型自己的契约：能给出零元（`zero`）、能把另一份摘要累加进来（`add_summary`）——恰好是 monoid 的两条公理。rope 没有直接实现带上下文的 `Summary`，而是实现了更简单的 `ContextLessSummary`（不需要 `Context` 参数的版本），sum_tree 用一个 blanket impl 自动把它升级成完整的 `Summary`。于是 rope 里所有传给 sum_tree 的「上下文」都是 `()`——你在 `find::<D, _>((), ...)` 里看到的那个空元组就是它。

rope 侧的摘要类型是 `ChunkSummary`——一个只包了 `text: TextSummary` 的 newtype。为什么包一层？因为树上挂的是 `Chunk`，它的摘要类型必须在 `impl Item for Chunk` 里一次性指定；包一层让「树的摘要」与「文本统计」在类型上分家，`Rope::summary()` 里那句 `self.chunks.summary().text` 也因此读起来很直白。

#### 4.1.2 核心流程

把「构建 → 累加 → 查询」串起来（对照 u1-l3 的写入路径）：

```text
1. Rope::push(text) 把文本切成 ≤ MAX_BASE 的块，逐块 chunks.extend(Chunk::new(...))；
2. SumTree 插入每个 Chunk 时调用 Item::summary()
   → ChunkSummary { text: chunk.as_slice().text_summary() }   （位图 O(1) 统计）
3. 插入路径上每个内部节点的摘要 = 对各子树摘要依次 add_summary
   → 委托给 self.text += &summary.text，即 u2-l2 的合并代数
4. 之后：
   - rope.summary()  = 树根缓存里的 ChunkSummary.text     O(1)
   - rope.len()      = 树根摘要向 usize 维度的投影(extent)  O(1)
   - rope.cursor(o)  = 包一个 sum_tree::Cursor 从 o 开始    O(log n) 定位
```

关键认知：**SumTree 不理解文本，它只会在插入时反复调用 `add_summary`。树的全部「智能」都来自 u2-l2 证明过的结合律**——无论树怎么分裂、平衡，折出来的和都一样。

#### 4.1.3 源码精读

元素挂点：

[crates/rope/src/rope.rs:L1255-L1263](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1255-L1263)——`impl sum_tree::Item for Chunk`：`type Summary = ChunkSummary`；`summary()` 把 `self.as_slice().text_summary()` 包进 `ChunkSummary`。注意 `cx` 参数被忽略（`_cx`），因为 rope 的摘要不需要上下文。

摘要类型与累加规则：

[crates/rope/src/rope.rs:L1265-L1278](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1265-L1278)——`ChunkSummary` 只有 `text: TextSummary` 一个字段；`ContextLessSummary::add_summary` 一行委托：`self.text += &summary.text`。**树上所有节点的摘要合并，最终都汇聚到 u2-l2 精读过的那个 `AddAssign`。**

sum_tree 侧的两个契约与升级机制：

[crates/sum_tree/src/sum_tree.rs:L34-L38](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/sum_tree/src/sum_tree.rs#L34-L38)——`Item` trait 定义，只有 `summary` 一个方法。

[crates/sum_tree/src/sum_tree.rs:L51-L72](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/sum_tree/src/sum_tree.rs#L51-L72)——`Summary`（带 `Context` 关联类型）与 `ContextLessSummary`（无上下文版），以及把后者自动升级为前者的 blanket impl（`type Context<'a> = ()`）。这就是 rope 里满眼 `()` 的原因：`ChunkSummary` 走的是无上下文通道。（带上下文的版本供其他 crate 使用，比如需要字体信息的场景——具体使用者不在本讲范围。）

树本体与扇出：

[crates/sum_tree/src/sum_tree.rs:L206-L213](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/sum_tree/src/sum_tree.rs#L206-L213)——`SumTree<T>` 的文档注释：这是一棵 B+ 树，叶子存 `Item` 和每个 item 的摘要，内部节点存子树摘要；**每个节点最多 `TREE_BASE * 2` 个 item**；任意 `Dimension` 都可以用来在树中定位。

[crates/sum_tree/src/sum_tree.rs:L15-L18](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/sum_tree/src/sum_tree.rs#L15-L18)——`TREE_BASE`：sum_tree 自己测试时是 2，否则是 6。**注意这只在编译 sum_tree crate 自身的测试时生效**；rope 的测试引用的是正式配置（扇出 12），rope 测试里缩小的是 Bitmap（u2-l4 的主题），两者别混淆。

块摘要的位图来源（只引用，不展开）：

[crates/rope/src/chunk.rs:L316-L331](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/chunk.rs#L316-L331)——`ChunkSlice::text_summary()` 组装九字段，全部来自 popcount；`Item::summary` 消费的正是它。

#### 4.1.4 代码实践

**实践目标**：亲手遍历树，验证「逐块摘要折叠 == 树根缓存」，并拿到块边界清单（后续 4.3 的 Bias 实验要用）。

**操作步骤**：

1. 在你的 Zed 克隆里打开 `crates/rope/src/rope.rs`，滚动到文件尾部 `mod tests`，加入下面的测试（读者练习；本讲义本身不修改源码）。测试模块里能访问私有字段 `rope.chunks`，这是把树「打开看」的唯一入口。
2. 在仓库根目录运行 `cargo test -p rope sum_tree_item_lab -- --nocapture`。

```rust
// 示例代码：读者练习，加在 crates/rope/src/rope.rs 的 mod tests 内
#[test]
fn sum_tree_item_lab() {
    // 测试配置下 Bitmap = u16、MAX_BASE = 16，这个文本必然跨很多块
    let text: String = (0..40).map(|i| format!("第{i}行 line\n")).collect();
    let rope = Rope::from(&text);

    // 用 usize 维度的游标走完整棵树，逐块折叠摘要
    let mut cursor = rope.chunks.cursor::<usize>(());
    cursor.seek(&0usize, Bias::Right);
    let mut folded = TextSummary::default(); // 单位元起步
    let mut starts = Vec::new();
    while let Some(chunk) = cursor.item() {
        starts.push(*cursor.start());            // 该块的全局起始字节偏移
        folded += &chunk.as_slice().text_summary(); // 与 Item::summary 等价
        cursor.next();
    }

    println!("chunk 数: {}, 块边界: {:?}", starts.len(), starts);
    assert!(starts.len() > 1, "文本应当跨多个块");
    // 逐块折叠 == 树根缓存：结合律的工程兑现
    assert_eq!(folded, rope.summary());
    // 块边界严格递增，最后一块的末端 == 全文长度
    assert!(starts.windows(2).all(|w| w[0] < w[1]));
    assert_eq!(starts[0], 0);
}
```

**需要观察的现象**：打印出的块边界是一串递增数字（测试配置下相邻边界大多相差 16 左右）；`folded == rope.summary()`——你的**线性折叠顺序**与 SumTree **按树形折叠**给出完全相同的九字段结果。

**预期结果**：断言全部通过。若把折叠顺序倒过来（从后往前 `+=`），结果**仍然**相同——`TextSummary` 的合并同时满足结合律与交换律（对拼接语义而言两段拼起来同一文本），这就是树可以任意整形的前提。

**待本地验证**：请实际运行确认（本讲义作者未替你执行）。

#### 4.1.5 小练习与答案

**练习 1**：`Item::summary` 的 `cx` 参数在 rope 里为什么永远是 `()`？

**答案**：`ChunkSummary` 实现的是 `ContextLessSummary`，经 sum_tree 的 blanket impl 升级为 `Summary` 时关联类型固定为 `type Context<'a> = ()`。所有需要传上下文的 sum_tree API（`find`/`cursor`/`extend`/`push`）因此都收一个 `()`。带上下文的 `Summary` 变体是给「摘要计算依赖外部状态」的 item 准备的，rope 的文本统计不需要。

**练习 2**：为什么选 B+ 树（内部节点只存摘要、数据全在叶子）而不是 AVL/红黑树那样的二叉搜索树？

**答案**：两个工程理由。其一，扇出大（12）树就矮，1 GiB 文本也只有约 7 层，每次查找的比较次数少且集中；其二，叶子连续存 item、内部节点只缓存摘要，配合 `Arc` 包裹的节点（`SumTree(Arc<Node<T>>)`），克隆一棵树是 O(1) 的结构共享——这对「每次编辑产生新版本」的编辑器工作负载至关重要（u2-l7 会看到 `Cursor::slice` 大量复制子树）。

**练习 3**：`Rope::summary()` 与 `Rope::len()` 分别读树的什么？

**答案**：`summary()` 调 `self.chunks.summary()` 返回**树根缓存的整份 `ChunkSummary`**再取 `.text`；`len()` 调 `self.chunks.extent(())` 把**同一份根摘要投影到 `usize` 维度**。两者都不触发遍历，都是 O(1)；区别只是「要整份报告」还是「只要一个数字」。（见 [rope.rs:L312-L318](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L312-L318)。）

### 4.2 Dimension 与 SeekTarget：摘要的任意投影

#### 4.2.1 概念说明

树已经能维护摘要了，还缺什么？**导航**。「第 1000 个字节落在哪个块里」「第 40 行的第 3 个字节在哪个块里」——这类问题需要把「累计到当前位置的摘要」与目标比较。`Dimension` trait 就是「累计器」的抽象：

```rust
pub trait Dimension<'a, S: Summary>: Clone {
    fn zero(cx: S::Context<'_>) -> Self;
    fn add_summary(&mut self, summary: &'a S, cx: S::Context<'_>);
}
```

任何「能从零开始、能把摘要累加进来」的类型都是一个维度。用 u2-l2 的语言说，维度是从摘要 monoid 到目标类型 monoid 的**同态**：

\[ \phi: (\text{ChunkSummary}, +) \to (D, +), \qquad \phi(S_1 + S_2) = \phi(S_1) + \phi(S_2), \quad \phi(0) = 0 \]

例如 `usize` 的投影就是 \[ \phi(S) = S.\text{text}.\text{len} \]，`Point` 的投影是 \[ \phi(S) = S.\text{text}.\text{lines} \]。

**关键认知：树节点里存的始终是完整的 `ChunkSummary`，维度不占树的存储**。`Dimension` 的累计发生在**查询下降的过程中**——从根往下走时，每跳过一个子树，就把该子树的摘要 `add_summary` 进手里的累计器。所以同一棵树可以同时支持按字节、按行列、按 UTF-16 定位：换的只是累计器类型，不动树本身。

光能累计还不够，还要能**比较**：「目标位置」与「当前累计位置」谁大谁小，才知道该往左还是往右走。这是 `SeekTarget` 的职责：

```rust
pub trait SeekTarget<'a, S: Summary, D: Dimension<'a, S>> {
    fn cmp(&self, cursor_location: &D, cx: S::Context<'_>) -> Ordering;
}
```

sum_tree 给了一个 blanket impl：任何 `D: Dimension + Ord` 自动成为自己的 `SeekTarget`（直接用 `Ord::cmp`）。`usize`、`Point`、`OffsetUtf16`、`PointUtf16` 都实现了 `Ord`（`Point` 按 row 优先、column 其后的字典序，恰好就是文本位置的先后序），所以它们既能当累计器又能当目标——`find::<usize, _>((), &offset, ...)` 里传的 `&offset` 就是目标。

#### 4.2.2 核心流程

查找的下降算法（`find_iterate`，4.3 精读源码）：

```text
pos = D::zero()                        # 累计器从零开始
node = 根
loop:
    for (child, child_summary) in node 的孩子们:
        child_end = pos + child_summary      # 累计到该孩子末尾
        if target < child_end 或 (== 且 Bias::Left):
            命中本孩子 → 下钻，continue 外层
        pos = child_end                       # 整个孩子都在目标之前，跳过
    # 叶子层：对每个 item 同样比较，返回命中的 item 与其末尾位置
```

三个要点：

1. `pos` 的类型就是调用方通过 `::<D>` 指定的维度——**下降一趟，同时在 D 坐标系里量出了位置**；
2. 每层的比较对象是「累计位置 + 子树摘要」，永远不需要真正进入子树数一遍——这就是前缀和的树上版本；
3. `Point` 这类「跨行会丢列」的坐标能当维度，靠的是 u2-l2 证明的合并代数：`S.text.lines` 的累加语义（位置 + 位移）与 `AddAssign` 完全一致。

#### 4.2.3 源码精读

Dimension 与 SeekTarget 的定义：

[crates/sum_tree/src/sum_tree.rs:L95-L110](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/sum_tree/src/sum_tree.rs#L95-L110)——`Dimension` trait：`zero` + `add_summary`，附带默认方法 `with_added_summary` / `from_summary`（取值版投影）。

[crates/sum_tree/src/sum_tree.rs:L112-L120](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/sum_tree/src/sum_tree.rs#L112-L120)——blanket impl：任何 `Summary` 自己也是自己的 `Dimension`（投影为恒等）。这解释了为什么 `TextSummary` 可以直接当维度用。

[crates/sum_tree/src/sum_tree.rs:L122-L130](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/sum_tree/src/sum_tree.rs#L122-L130)——`SeekTarget` 及其 blanket impl（`Dimension + Ord` 自动成为自己的目标）。

rope 写下的五个投影实现（模式完全一致，只有取的字段不同）：

[crates/rope/src/rope.rs:L1492-L1500](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1492-L1500)——`usize`：`*self += summary.text.len`（字节数）。

[crates/rope/src/rope.rs:L1516-L1524](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1516-L1524)——`OffsetUtf16`：累加 `summary.text.len_utf16`（UTF-16 码元数）。

[crates/rope/src/rope.rs:L1540-L1548](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1540-L1548)——`Point`：累加 `summary.text.lines`（EOF 行列位置；`Point` 的 `+=` 即「位置 + 位移」）。

[crates/rope/src/rope.rs:L1564-L1572](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1564-L1572)——`PointUtf16`：累加 `summary.text.lines_utf16()`。

[crates/rope/src/rope.rs:L1468-L1476](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1468-L1476)——`TextSummary`：整份摘要累加（`*self += &summary.text`），要全部字段时用它。

extent：根摘要的投影：

[crates/sum_tree/src/sum_tree.rs:L723-L734](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/sum_tree/src/sum_tree.rs#L723-L734)——`extent::<D>()`：把**根节点**的摘要投影到维度 `D`。`Rope::len()` / `max_point()` / `max_point_utf16()` 三个 O(1) 方法就是三行 extent 调用（[rope.rs:L316-L330](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L316-L330)）。

#### 4.2.4 代码实践

**实践目标**：验证「不同维度导航到同一位置」与「extent 是根摘要的投影」。

**操作步骤**：

1. 仍在 `mod tests` 内加入（示例代码）：

```rust
#[test]
fn sum_tree_dimension_lab() {
    let text: String = (0..40).map(|i| format!("第{i}行 line😀\n")).collect();
    let rope = Rope::from(&text);
    let offset = 77usize; // 任取一个字节偏移（下面统一换算成其他坐标）

    // 同一逻辑位置，用三种单一维度分别 find
    let (s_len, e_len, i_len) = rope.chunks.find::<usize, _>((), &offset, Bias::Left);
    let point = rope.offset_to_point(offset);
    let (s_pt, e_pt, i_pt) = rope.chunks.find::<Point, _>((), &point, Bias::Left);
    let (s_u16, _e, i_u16) = rope
        .chunks
        .find::<OffsetUtf16, _>((), &rope.offset_to_offset_utf16(offset), Bias::Left);

    // 三次查找落到同一个块上
    assert!(std::ptr::eq(i_len.unwrap(), i_pt.unwrap()));
    assert!(std::ptr::eq(i_len.unwrap(), i_u16.unwrap()));

    // 各维度返回的块起点是同一位置的三种表示
    assert_eq!(s_pt, rope.offset_to_point(s_len));
    assert_eq!(rope.offset_utf16_to_offset(s_u16), s_len);
    // (start, end) 恰好夹住目标：start <= 目标 <= end
    assert!(s_len <= offset && offset <= e_len);
    assert_eq!(e_len - s_len, i_len.unwrap().text.len());
}

#[test]
fn sum_tree_extent_lab() {
    let rope = Rope::from("ab\ncdef\n😀\nxy");
    // extent 三连：len / max_point / max_point_utf16 都是根摘要的投影
    assert_eq!(rope.len(), rope.summary().len);
    assert_eq!(rope.max_point(), rope.summary().lines);
    assert_eq!(rope.max_point_utf16(), rope.summary().lines_utf16());
}
```

2. 运行 `cargo test -p rope sum_tree_dimension_lab sum_tree_extent_lab`（cargo 会把它们当两个过滤器，分别跑即可，或去掉一个名字直接跑 `sum_tree_` 前缀：`cargo test -p rope sum_tree`）。

**需要观察的现象**：三种维度的 `find` 返回的 `item` 是**同一个 `&Chunk`**（用 `std::ptr::eq` 验证指针相同）；`Point` 维度返回的块起点 `s_pt` 恰好等于 `offset_to_point(s_len)`——同一位置的两种坐标。

**预期结果**：全部通过。这组断言是「维度只是投影、树只有一棵」的最直接证据：换维度不换树，也不换命中结果。

**待本地验证**：请实际运行确认。

#### 4.2.5 小练习与答案

**练习 1**：如果想新增一个「tab 字符总数」维度，要动哪些代码？

**答案**：分两步。首先 `ChunkSummary`（其实是里面的 `TextSummary`）得带上这个统计量——目前九个字段里没有 tab 计数，`Chunk` 的 tabs 位图信息在 `Item::summary` 这一步被丢掉了；需要在 `TextSummary` 增加字段并维护 `From<&str>` / `AddAssign` 两条路径。然后定义 `struct TabCount(usize)` 并实现 `Dimension<ChunkSummary>`（`add_summary` 里 `self.0 += summary.text.tabs`）。这也印证了 u2-l2 练习 3 的原则：新维度必须能由摘要 O(1) 投影，否则就得先扩摘要。（`TextDimension` 侧的配套实现见 4.4；具体接口以源码为准，**待确认**。）

**练习 2**：`Point` 的 `Ord` 是什么顺序？为什么它恰好能当 `usize` 的替身做导航？

**答案**：按 `(row, column)` 字典序（row 优先）。文本位置的本质先后就是「先比行、同行比列」，且 `Point` 与字节偏移在合法文本上互相单调对应——字节偏移增大，`offset_to_point` 的结果在 `Point` 字典序上不减。所以用哪个维度导航，命中的块集合一致（4.2.4 实践验证了这一点）。

**练习 3**：`Dimension::add_summary` 拿到的是 `&ChunkSummary`。为什么 `Point` 的实现累加 `summary.text.lines` 而不是自己扫描块的文本数换行符？

**答案**：因为摘要里**已经**缓存了这块的 EOF 位置（`lines` 字段），投影的意义就是「不重算，只取数」。若维度需要的信息不在摘要里（如练习 1 的 tab 数），就必须先扩展摘要——维度是摘要的函数，不是块的函数。块内换算（如 `chunk.as_slice().offset_to_point`）只在「目标落在块内部」的最后一段才发生（见 4.3）。

### 4.3 解剖一次 find：泛型、上下文、Bias 与返回值

#### 4.3.1 概念说明

本模块把 rope 里出现频率最高的一行调用拆到每个字符：

```rust
let (start, _, item) = self.chunks.find::<usize, _>((), &offset, Bias::Left);
```

出自 `Rope::is_char_boundary`（[rope.rs:L42-L50](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L42-L50)）。逐部件解释：

| 部件 | 含义 |
|---|---|
| `self.chunks` | `SumTree<Chunk>`，被查找的树 |
| `find::<usize, _>` | 查找维度 `D = usize`：返回的位置以字节偏移表示；`_` 是目标类型，由实参推断为 `usize` |
| `()` | `Summary::Context`——rope 无上下文，恒为 `()` |
| `&offset` | `SeekTarget`：要定位的字节偏移 |
| `Bias::Left` | 目标恰好落在块边界时的归属：贴左块 |
| 返回 `(start, end, item)` | 命中块在 `D` 维度下的**起点**、**终点**（起点 + 该块摘要的投影）、块本身的引用；越界时 `item` 为 `None` 且 `start == end ==` 树末端 |

**Bias 的语义**值得单独记。文档注释给的例子：缓冲区 `AˇBCD`、光标在偏移 1——`Bias::Left` 把这个位置附着到字符 `A`，`Bias::Right` 附着到 `B`。落到树上：目标等于某块末尾时，`Left` 命中**前一块**（`end == 目标`），`Right` 命中**后一块**（`start == 目标`）。这不是无关紧要的细节：`Chunks` 正向迭代用 `seek(range.start, Bias::Right)`（从起始块开始吐文本），反向迭代用 `seek(range.end, Bias::Left)`（拿到包含 `range.end` 前最后一个字节的块）——两个方向各取所需（[rope.rs:L806-L814](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L806-L814)）。

**find 与 cursor 的分工**：`find` 是一次性定位（不保存下降路径，适合「只查一次」）；`sum_tree::Cursor` 保存下降栈、可以 `next`/`prev`/`seek_forward` 连续前进，还提供 `slice`（把走过的一段剪成新树）与 `summary`（把走过的一段折叠成任意维度）。rope 的 `Rope::cursor(offset)` 就是包了一个 `sum_tree::Cursor<Chunk, usize>`（[rope.rs:L678-L693](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L678-L693)），u1-l3 见过的 `replace`/`slice` 全靠它。

#### 4.3.2 核心流程

`find` 的完整流程（对照源码 L424–L507）：

```text
find(D, target, bias):
    tree_end = D::zero() + 根摘要            # 整棵树在 D 维度的末端
    if target > tree_end 或 (== 且 bias == Right):
        return (tree_end, tree_end, None)    # 越界：统一返回树末端
    pos = D::zero()
    从根开始每层:
        for 每个 (child, child_summary):
            child_end = pos + child_summary  # add_summary 累计
            if target < child_end 或 (== 且 Left):
                下钻该 child，继续
            pos = child_end                  # 跳过整个 child
        # 到叶子层：对每个 item 的摘要做同样比较
        命中 → return (pos_before, item_end, Some(item))
    没找到 → return (pos, pos, None)
```

以 `offset_to_point(offset)` 为例看 rope 怎么消费它（这是 4.4「一次查找两种坐标」的铺垫，先用单一维度理解）：

```text
offset_to_point(offset):
    if offset >= len: return summary.lines       # O(1) 越界短路
    (start, _, Some(chunk)) = find::<usize>((), offset, Left)
    overshoot = offset - start                    # 块内字节偏移
    return chunk.as_slice().offset_to_point(overshoot)  # 块内位图换算
```

树负责把范围缩小到**一个块**（\( O(\log n) \)），块内的小坐标换算交给位图（\( O(1) \)，见 [chunk.rs:L407-L414](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/chunk.rs#L407-L414)：`row` 是 offset 之前换行符位图的 popcount，`column` 是 offset 减去最后一个换行符的位置）。两级接力，全程没有逐字符扫描。

#### 4.3.3 源码精读

`find` 的签名与越界预处理：

[crates/sum_tree/src/sum_tree.rs:L424-L448](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/sum_tree/src/sum_tree.rs#L424-L448)——返回类型 `(D, D, Option<&'slf T>)`；文档注明它是 `Cursor::new + seek + item` 的高效等价物。先算 `tree_end` 并拦截越界（`Greater` 或 `Equal + Right` 直接返回 `(tree_end, tree_end, None)`）——**这就是 rope 各换算函数开头那个 `if offset >= self.summary().len` 短路的树侧搭档**，两层防线一起保证后面的解包安全。

下降主循环：

[crates/sum_tree/src/sum_tree.rs:L450-L507](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/sum_tree/src/sum_tree.rs#L450-L507)——`find_iterate`：内部节点层对每个孩子算 `child_end = position + child_summary` 再比较（L468–L479）；叶子层对每个 item 做同样的事（L486–L502）。注意 `position` 在跳过孩子/条目时被逐步推进——它就是「前缀和」在下降中的动态维护。

Bias 定义：

[crates/sum_tree/src/sum_tree.rs:L167-L204](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/sum_tree/src/sum_tree.rs#L167-L204)——`enum Bias { Left, Right }`，文档用光标/选区/折叠区三个例子解释「贴左/贴右」；`invert()` 取反。

rope 消费 `find` 的最简例子：

[crates/rope/src/rope.rs:L42-L50](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L42-L50)——`is_char_boundary`：`find::<usize, _>` 定位块，`offset - start` 折算成块内下标，再查块内位图。空树特判在前（`chunks.is_empty()` 时只有 `offset == 0` 合法）。

`offset_to_point` 的树级 + 块级接力：

[crates/rope/src/rope.rs:L397-L409](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L397-L409)——先 `if offset >= self.summary().len` 短路返回 `summary().lines`（EOF 即「无穷远」的 Point）；然后一次 `find`，`overshoot` 交给块内换算，`map_or(Point::zero(), ...)` 兜住理论上的 `None`。

[crates/rope/src/chunk.rs:L407-L414](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/chunk.rs#L407-L414)——块内 `offset_to_point`：`mask` 是「offset 之前的所有位」，`row` = `newlines & mask` 的 popcount，`column` = offset −（最后一个换行符位 + 1）。位图细节 u2-l4 展开。

`find` 的替代写法（cursor 版）：

[crates/rope/src/rope.rs:L439-L452](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L439-L452)——`point_utf16_to_point` 没用 `find`，而是 `cursor::<Dimensions<PointUtf16, Point>>()` 加 `seek`。功能等价，风格不同；`find` 适合一次性，`cursor` 适合还要继续前进的场景。同一文件里两种写法并存，说明这是口味而非硬约束。

游标的读取面：

[crates/sum_tree/src/cursor.rs:L82-L115](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/sum_tree/src/cursor.rs#L82-L115)——`start()`（当前位置，`&D`）、`end()`（start + 当前 item 摘要的投影）、`item()`（当前块；列表空或到末尾时 `None`）。

[crates/sum_tree/src/cursor.rs:L408-L460](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/sum_tree/src/cursor.rs#L408-L460)——`seek`（可前后任意跳）/ `seek_forward`（只许前进，更快）/ `slice`（把走过的部分剪成新 `SumTree`）/ `suffix`（取剩余全部）/ `summary::<Target, Output>`（把走过的部分折叠成任意维度——4.4 的主角）。rope 的 `Cursor::seek_forward` 在偏移回退时 `assert!`（[rope.rs:L695-L709](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L695-L709)），与 sum_tree 的 `seek_internal` 的「cannot seek backward」断言（cursor.rs L471–L474）双层设防。

#### 4.3.4 代码实践

**实践目标**：用实验确认 Bias 在块边界上的归属行为。

**操作步骤**：

1. 在 `mod tests` 内加入（示例代码）：

```rust
#[test]
fn sum_tree_bias_lab() {
    // 纯 ASCII：块的切分完全可预测（测试配置 MAX_BASE = 16）
    let text = "x".repeat(100);
    let rope = Rope::from(&text);

    // 先收集所有块边界（复用 4.1.4 的走法）
    let mut cursor = rope.chunks.cursor::<usize>(());
    cursor.seek(&0usize, Bias::Right);
    let mut starts = Vec::new();
    while let Some(_chunk) = cursor.item() {
        starts.push(*cursor.start());
        cursor.next();
    }

    // 取一个不在首尾的边界做实验
    let boundary = starts[1];
    assert!(boundary > 0 && boundary < text.len());

    // Left：贴前一块——命中块的 end 恰好等于 boundary
    let (s_l, e_l, i_l) = rope.chunks.find::<usize, _>((), &boundary, Bias::Left);
    assert_eq!(e_l, boundary);
    assert!(s_l < boundary);
    assert!(i_l.is_some());

    // Right：贴后一块——命中块的 start 恰好等于 boundary
    let (s_r, e_r, i_r) = rope.chunks.find::<usize, _>((), &boundary, Bias::Right);
    assert_eq!(s_r, boundary);
    assert!(e_r > boundary);
    assert!(i_r.is_some());
    // 两次命中的不是同一个块
    assert!(!std::ptr::eq(i_l.unwrap(), i_r.unwrap()));
}
```

2. 运行 `cargo test -p rope sum_tree_bias_lab`。
3. 把 `boundary` 换成 `starts[2]`、`text.len()`（末尾）再观察：末尾 + `Left` 命中最后一块（`end == len`），末尾 + `Right` 则 `item` 为 `None`（越界分支）。

**需要观察的现象**：同一个 `boundary`，`Left` 与 `Right` 返回不同的块——前者 `end == boundary`，后者 `start == boundary`。

**预期结果**：断言通过。这解释了 `Chunks` 为什么正反两个方向用了相反的 Bias：正向要从「起始块」开始吐字，反向要拿到「结束位置前一字节所在块」。

**待本地验证**：请实际运行确认。

#### 4.3.5 小练习与答案

**练习 1**：`Rope::cursor(offset)` 内部 `seek` 用的是 `Bias::Right`（[rope.rs:L685-L693](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L685-L693)），为什么选 Right 不选 Left？

**答案**：游标语义是「从 offset 开始向后消费」。`Right` 让 item() 停在**起始**于 offset 的块上，`start() == offset`，随后 `slice`/`summary` 从这里起算；若用 `Left`，item() 会停在 offset 前面那块，`Cursor::slice` 里 `start_ix = self.offset - self.chunks.start()` 会得到越界下标。反例即 `Chunks` 的反向迭代：它要的是「结束位置前的内容」，所以用 `Left`。

**练习 2**：为什么 rope 每个坐标换算函数开头都有一句 `if offset >= self.summary().len { return ...; }`？

**答案**：两层作用。语义上，把越界输入统一映射到 EOF（`summary().lines` 等），给调用方确定的返回值；工程上，提前拦截后，`find` 的结果一定 `Some`，后面的 `item.map_or(...)` 只是防御性兜底，块内换算也不会拿到越界的 `overshoot`。若没有这句，`find` 在越界时返回 `None`，`start` 是树末端，`offset - start` 会下溢 panic。

**练习 3**：`find` 的复杂度是 \( O(B \log_B n) \)，那 `Rope::is_char_boundary` 总复杂度是多少？

**答案**：\( O(B \log_B n) \) + 块内 O(1) 位图查询 = \( O(\log n) \)（B 是常数 12）。同理 `offset_to_point`、`point_to_offset` 等所有换算都是对数级。这就是本讲标题「前缀和树支撑 O(log n) 查询」的逐条兑现。

### 4.4 Dimensions 元组、TextDimension 与 DimensionPair：一次查找，两种坐标

#### 4.4.1 概念说明

**问题**：`offset_to_point` 需要同时知道两件事——目标块的全局**字节起点**（算 overshoot）和全局 **Point 起点**（加回结果）。按 4.3 的做法要查两次树吗？

**不需要。** sum_tree 内建了元组维度 `Dimensions<D1, D2, D3>`：它自己实现 `Dimension`（`add_summary` 时把摘要同时累加给两个成员），于是**一次下降同时维护两套坐标**。配套的 `SeekTarget` 实现规定：目标只与 `.0`（第一个成员）比较。于是：

```rust
let (start, _, item) = self.chunks.find::<Dimensions<usize, Point>, _>((), &offset, Bias::Left);
// start.0 = 目标块的字节起点；start.1 = 同一块的 Point 起点
let overshoot = offset - start.0;
start.1 + chunk.as_slice().offset_to_point(overshoot)   // 完事
```

一次树下降，键维度（`usize`）负责导航，值维度（`Point`）搭车计数。rope 的全部换算函数都是这个模板，只是键值互换：

| 函数 | 键（导航用） | 值（搭车累计） |
|---|---|---|
| `offset_to_point` | `usize` | `Point` |
| `offset_to_offset_utf16` | `usize` | `OffsetUtf16` |
| `point_to_offset` | `Point` | `usize` |
| `point_utf16_to_point` | `PointUtf16` | `Point` |
| `point_utf16_to_offset` | `PointUtf16` | `usize` |

**`TextDimension`：rope 对维度的再包装。** 注意到上面所有函数都只处理「整块」的边界；但 `Cursor::summary::<D>(end)` 要算的是**任意区间** `[self.offset, end)` 的维度值，区间的两端可能落在块的中间——树上没有「半块」的摘要。于是 rope 定义：

```rust
pub trait TextDimension:
    'static + Clone + Copy + Default + for<'a> Dimension<'a, ChunkSummary> + Debug
{
    fn from_text_summary(summary: &TextSummary) -> Self;  // 从整份摘要投影
    fn from_chunk(chunk: ChunkSlice) -> Self;             // 从任意切片现算
    fn add_assign(&mut self, other: &Self);
}
```

比 `Dimension` 多出的 `from_chunk` 正是补「半块」的：区间两端各切一小段，`D::from_chunk(chunk.slice(a..b))` 现场算出切片的维度值。`usize`/`Point`/`OffsetUtf16`/`PointUtf16`/`TextSummary` 五个类型连同 `Dimensions` 元组与 `DimensionPair` 都实现了它——**这就是 `Cursor::summary::<D>` 能返回任意维度的全部秘密**：`D` 只要满足 `TextDimension`（因而满足 `Dimension`），sum_tree 的游标在前进途中每跳过一个节点/条目，就调用一次 `D::add_summary` 把该段的贡献累进结果（[cursor.rs:L819-L841](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/sum_tree/src/cursor.rs#L819-L841) 的 `SummarySeekAggregate`）；两端不足整块的部分用 `from_chunk` 补齐。

**`DimensionPair<K, V>`：同一思想的命名结构体版。** 它是 rope 自己定义的维度：`key` 参与比较（`Ord` 只比 key），`value` 是 `Option<V>`，加减时跟着走。与 `Dimensions` 元组相比有两个差异：其一，结构体字段有名字、`value` 可缺席（`Sub` 时两侧 value 都在才相减，否则结果 value 为 `None`）；其二，它专门提供了 `impl AddAssign<DimensionPair<Point, D>> for Point`，允许把 Pair 的 key 直接加到 `Point` 上。由于「`Dimension` + `Ord` 自动成为自己的 `SeekTarget`」，可以用一个 `value: None` 的 Pair 当目标去 seek——比较只看 key。目前整个工作区没有它的调用者（与 u2-l2 发现的 `newline()` 类似，属于预留公共 API，外部是否有使用**待确认**），但它是理解「维度可组合」这件事最直观的教材。

#### 4.4.2 核心流程

`Cursor::summary::<D>(end_offset)` 的三段式（u2-l2 从摘要视角看过，这里从**维度机制**视角重看）：

```text
输入：游标当前在 self.offset，要算 [self.offset, end_offset) 的 D 维度值
1. 起点块：若当前块的起点早于 self.offset（游标从块中间开始）：
      D::from_chunk(start_chunk.slice(start_ix..min(end, 块末)))
   ——树上没有半块摘要，用 from_chunk 现算
2. 中间整块：chunks.summary(&end_offset, Bias::Right)
   ——sum_tree 游标每跳过一个节点/条目，Output::add_summary 累计一次
   ——这一步的 Output 就是调用方指定的 D，所以「任意维度」
3. 终点块：若 end_offset 落在块中间：
      D::from_chunk(end_chunk.slice(0..end_ix))
返回累加结果，游标前进到 end_offset
```

`Dimensions` 元组维度的下降（与 4.3 的单一维度完全同构，只是累计器变成了二元组）：

```text
pos = (usize::zero(), Point::zero())
每跳过一个子树/条目：
    pos.0 += summary.text.len        # 键：字节
    pos.1 += summary.text.lines      # 值：Point（同一份摘要、两个投影）
比较：target 只与 pos.0 比
```

#### 4.4.3 源码精读

`Dimensions` 的定义与两个 trait 实现：

[crates/sum_tree/src/sum_tree.rs:L138-L153](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/sum_tree/src/sum_tree.rs#L138-L153)——`pub struct Dimensions<D1, D2, D3 = ()>(pub D1, pub D2, pub D3)`；它的 `Dimension` 实现把 `add_summary` 同时派发给三个成员。

[crates/sum_tree/src/sum_tree.rs:L155-L165](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/sum_tree/src/sum_tree.rs#L155-L165)——关键的一小段：`SeekTarget for D1`——目标类型是**第一个成员的类型**，比较时只看 `cursor_location.0`。这就是「键导航、值搭车」的契约出处。

rope 换算函数的消费现场（注意泛型实参里键值如何随语义互换）：

[crates/rope/src/rope.rs:L397-L409](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L397-L409)——`offset_to_point`：`find::<Dimensions<usize, Point>, _>`，`overshoot = offset - start.0`，结果是 `start.1 + 块内换算`。

[crates/rope/src/rope.rs:L369-L381](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L369-L381)——`offset_to_offset_utf16`：`Dimensions<usize, OffsetUtf16>`，同一模板的 UTF-16 版。

[crates/rope/src/rope.rs:L454-L464](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L454-L464)——`point_to_offset`：键值互换为 `Dimensions<Point, usize>`（目标换成了 `&point`）。注意函数上方的 `#[instrument(skip_all)]`：这个热点换算挂了 ztracing 追踪点，性能排查时可观测。

`TextDimension` 与五个实现：

[crates/rope/src/rope.rs:L1441-L1447](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1441-L1447)——trait 定义：`Dimension` 是超 trait，外加 `from_text_summary` / `from_chunk` / `add_assign` 三个方法。

[crates/rope/src/rope.rs:L1449-L1466](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1449-L1466)——`Dimensions<D1, D2, ()>` 的 `TextDimension`：两个成员各自 `from_chunk` / `from_text_summary`，`add_assign` 派发——元组维度因此也能当 `Cursor::summary` 的 `D`。

[crates/rope/src/rope.rs:L1502-L1514](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1502-L1514)——`usize` 的 `TextDimension`：`from_chunk` 返回 `chunk.len()`（切片的字节数）——半块的字节数就是切片长度，O(1)。

[crates/rope/src/rope.rs:L1550-L1562](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1550-L1562)——`Point` 的 `TextDimension`：`from_chunk` 返回 `chunk.lines()`（切片的行数 + 末行长度，位图 popcount）。`OffsetUtf16`（L1526–L1538）、`PointUtf16`（L1574–L1589）、`TextSummary`（L1478–L1490）同理。

`Cursor::summary::<D>` 与它依赖的折叠机制：

[crates/rope/src/rope.rs:L745-L775](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L745-L775)——三段式的完整实现：L757–L762 起点半块（`D::from_chunk`）、L766 中间整块（`self.chunks.summary(&end_offset, Bias::Right)`）、L767–L770 终点半块。`D: TextDimension` 的 trait bound 就写在这一行：`pub fn summary<D: TextDimension>(&mut self, end_offset: usize) -> D`。

[crates/sum_tree/src/cursor.rs:L452-L460](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/sum_tree/src/cursor.rs#L452-L460)——sum_tree 侧的 `Cursor::summary::<Target, Output>`：`Output: Dimension`——**任意维度**都能当输出。

[crates/sum_tree/src/cursor.rs:L819-L841](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/sum_tree/src/cursor.rs#L819-L841)——`SummarySeekAggregate`：`push_item` / `push_tree` 都只是调用 `self.0.add_summary(...)`。跳过整棵子树时一次加子树摘要，逐条目走时一次加条目摘要——「任意维度可折叠」的机制本体。

`DimensionPair`：

[crates/rope/src/rope.rs:L1591-L1597](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1591-L1597)——文档注释直说设计意图：「只有第一个维度参与比较，但两个维度在加减法中都会更新」。

[crates/rope/src/rope.rs:L1608-L1615](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1608-L1615)——`Ord` 只比较 `key`。配合 sum_tree 的 SeekTarget blanket impl，`DimensionPair` 可以直接当 seek 的目标与游标维度。

[crates/rope/src/rope.rs:L1635-L1665](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1635-L1665)——`Sub`（两侧 value 都在才逐项相减，否则结果 value 为 `None`）与 `AddAssign`（遇到 value 为 `None` 的加数，自身 value 也变 `None`——「未知传染」）。

[crates/rope/src/rope.rs:L1667-L1671](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1667-L1671)——`Point += DimensionPair<Point, D>`：只加 key——把 Pair 的导航结果直接落到 `Point` 上。

[crates/rope/src/rope.rs:L1675-L1694](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1675-L1694)——`Dimension for DimensionPair`：`add_summary` 更新 key 与 value 两路——与 `Dimensions` 元组完全同构的行为。

#### 4.4.4 代码实践

**实践目标**：完成本讲规格指定的两项实验——(a) 长绳子上 `cursor` + 坐标往返验证；(b) 用 `DimensionPair` 亲手做一次「key 导航、value 搭车」的查找，并让 `Cursor::summary::<D>` 同时返回三种维度。

**操作步骤**：

1. 在 `mod tests` 内加入（示例代码）：

```rust
// 朴素参照：直接对 String 计算行列（列按字节计，与 rope 口径一致）
fn naive_point(text: &str, offset: usize) -> Point {
    let before = &text[..offset];
    let row = before.matches('\n').count() as u32;
    let column = match before.rfind('\n') {
        Some(ix) => (offset - ix - 1) as u32,
        None => offset as u32,
    };
    Point::new(row, column)
}

#[test]
fn sum_tree_round_trip_lab() {
    let text: String = (0..60).map(|i| format!("第{i}行😀 line\n")).collect();
    let rope = Rope::from(&text);

    // (a) 每个字符边界偏移上：offset <-> point 往返 + 与朴素扫描对拍
    let mut offsets = vec![0usize];
    offsets.extend(text.char_indices().map(|(i, _)| i));
    offsets.push(text.len());
    for &offset in &offsets {
        let point = rope.offset_to_point(offset);
        assert_eq!(point, naive_point(&text, offset), "offset {offset}");
        assert_eq!(rope.point_to_offset(point), offset, "round trip at {offset}");
    }

    // (b) DimensionPair：key = Point 导航，value = usize 搭车
    let probe = naive_point(&text, text.len() / 2);
    let mut pair_cursor = rope.chunks.cursor::<DimensionPair<Point, usize>>();
    pair_cursor.seek(
        &DimensionPair { key: probe, value: None }, // value 缺席：比较只看 key
        Bias::Left,
    );
    let s = pair_cursor.start();
    // 一次查找同时拿到：目标块起点的 Point（key）与字节偏移（value）
    let chunk_start_point = s.key;
    let chunk_start_offset = s.value.unwrap();
    assert_eq!(rope.point_to_offset(chunk_start_point), chunk_start_offset);

    // (c) 同一段区间，三种维度各要一次
    let (start, end) = (11usize, text.len() - 7);
    assert_eq!(rope.cursor(start).summary::<usize>(end), end - start);
    assert_eq!(
        rope.cursor(start).summary::<Point>(end),
        TextSummary::from(&text[start..end]).lines,
    );
    assert_eq!(
        rope.cursor(start).summary::<TextSummary>(end),
        TextSummary::from(&text[start..end]),
    );
}
```

2. 运行 `cargo test -p rope sum_tree_round_trip_lab`。
3. 把 `(start, end)` 换成落在多字节字符**内部**的值再跑（如 `(9, text.len() - 6)`——字节 9 在 😀 的 4 个字节中间）：注意 `summary::<usize>` 仍然成立（字节长度与边界无关），而 `&text[start..end]` 会在切片处 panic——这就是 rope 的区间 API 要求字符边界、而字节维度本身不要求的原因（`Cursor::summary` 的 `slice` 内部有边界回退，行为以 u3-l1 的边界防御为准，**待确认**具体回退方向）。

**需要观察的现象**：

- (a) 全部往返成立：`point_to_offset(offset_to_point(o)) == o` 对每个字符边界 `o` 都成立，且与朴素扫描的行列完全一致——树级定位 + 块内位图换算的两级接力没有引入任何偏差；
- (b) `s.value` 不是 `None`：下降途中每跳过一个子树，`DimensionPair::add_summary` 同时更新了 key 和 value，所以**一次** `seek` 后两种坐标都在手上；`value: None` 只出现在目标里（参与比较的只有 key）；
- (c) 三次 `summary::<D>` 返回三种类型，都等于朴素口径——`D` 是调用方在现场挑的，树与游标不为所动。

**预期结果**：断言全部通过（`naive_point` 的列按字节计，与 `Point` 的口径一致，参见 u2-l1）。

**待本地验证**：请实际运行确认。

#### 4.4.5 小练习与答案

**练习 1**：`point_to_offset` 为什么是 `Dimensions<Point, usize>` 而不是 `Dimensions<usize, Point>`？

**答案**：`Dimensions` 的 `SeekTarget` 实现规定**第一个成员是比较键**。`point_to_offset` 的输入目标是 `Point`，必须把它放在 `.0` 才能参与导航；`usize` 是想顺带拿到的「目标块的字节起点」，放 `.1` 搭车。反过来写，`&point` 就无法与 `cursor_location.0`（usize）比较，类型直接不匹配。

**练习 2**：用一句话向同事解释 `Cursor::summary::<D>` 为什么能返回任意维度。

**答案**：因为游标前进的每一步都只是对跳过内容的摘要调用 `D::add_summary`（`SummarySeekAggregate`），而 `D` 的唯一要求是「能从摘要累加」（`Dimension`），rope 再用 `TextDimension::from_chunk` 补上两端不足整块的切片——所以 `usize`、`Point`、`TextSummary`、乃至 `Dimensions` 元组和 `DimensionPair` 都能当 `D`。`DimensionPair` 就是这个机制的struct化演示：key 负责被比较，value 负责搭车，两者都在同一次下降里累计。

**练习 3**：`DimensionPair` 的 `value` 为什么是 `Option`，而 `Dimensions` 元组的成员不是？

**答案**：`DimensionPair` 实现了 `Sub`（位移运算），两个 Pair 相减时若**减数**的 value 缺席，结果的 value 就无法确定，只能用 `None` 表示「未知」，且 `AddAssign` 会让 `None` 传染；`Dimensions` 元组不提供减法语义，成员永远在 `zero()` 时就初始化好、只增不减，因此不需要 `Option`。（这是从 trait 实现反推的设计动机，源码未显式注释，**待确认**。）

**练习 4**：`Cursor::summary::<TextSummary>(end)` 与 `Rope::slice(start..end).summary()` 都能得到区间摘要，差别是什么？

**答案**：前者只做一次对数级遍历 + 常数次半块计算，**不分配**新树；后者要真正构造一棵新 `SumTree`（克隆中间子树、复制两端半块），成本高一个量级，但得到可以继续编辑的独立 `Rope`。要统计就用 `summary::<D>`，要文本就用 `slice`——这正是 u1-l3「读文本用迭代器、不改就别拷」的原则在摘要上的对应物。

## 5. 综合实践

**任务：写一个「树导航观察站」测试，把本讲的机制串成一条链——块折叠、多维度 find、Bias、DimensionPair 导航、区间多维度 summary、往返对拍。**

参考实现（示例代码，加在 `crates/rope/src/rope.rs` 的 `mod tests` 内）：

```rust
#[test]
fn sum_tree_navigation_lab() {
    // 1. 构造一段足够长、含 CJK 与 emoji 的确定性文本
    let text: String = (0..80).map(|i| format!("第{i}行😀 line\n")).collect();
    let rope = Rope::from(&text);

    // 2. 块折叠：逐块摘要相加 == 树根缓存（4.1）
    let mut cursor = rope.chunks.cursor::<usize>(());
    cursor.seek(&0usize, Bias::Right);
    let mut folded = TextSummary::default();
    let mut boundaries = vec![0usize];
    while let Some(chunk) = cursor.item() {
        boundaries.push(*cursor.start() + chunk.text.len());
        folded += &chunk.as_slice().text_summary();
        cursor.next();
    }
    boundaries.pop(); // 最后一块的末端 == len，不作为内部边界
    assert_eq!(folded, rope.summary());

    // 3. 多维度 find：字节键 + Point/UTF-16 值，三次查找同一块（4.2/4.4）
    // 注意从 boundaries[1..] 开始：offset == 0 时 Left/Right 都命中首块，不属内部边界
    for &offset in &boundaries[1..] {
        // 内部边界上：Left 贴前块（end == offset）、Right 贴后块（start == offset）（4.3）
        let (_, e_l, i_l) = rope.chunks.find::<usize, _>((), &offset, Bias::Left);
        let (s_r, _, i_r) = rope.chunks.find::<usize, _>((), &offset, Bias::Right);
        assert_eq!(e_l, offset);
        assert_eq!(s_r, offset);
        assert!(!std::ptr::eq(i_l.unwrap(), i_r.unwrap()));

        // 换算函数 = find<Dimensions<键, 值>> + 块内位图换算（4.4）
        let point = rope.offset_to_point(offset);
        assert_eq!(rope.point_to_offset(point), offset);
    }

    // 4. DimensionPair 导航：按 Point 找块，顺带拿到字节起点（4.4）
    let mid = naive_point(&text, text.len() / 2);
    let mut pair_cursor = rope.chunks.cursor::<DimensionPair<Point, usize>>();
    pair_cursor.seek(&DimensionPair { key: mid, value: None }, Bias::Left);
    assert_eq!(
        pair_cursor.start().value.unwrap(),
        rope.point_to_offset(pair_cursor.start().key)
    );

    // 5. 区间多维度 summary：三种 D 都与朴素口径一致（4.4）
    let (start, end) = (boundaries[1], boundaries[5]);
    assert_eq!(rope.cursor(start).summary::<usize>(end), end - start);
    assert_eq!(
        rope.cursor(start).summary::<TextSummary>(end),
        TextSummary::from(&text[start..end])
    );

    // 6. 往返对拍：所有字符边界上 offset <-> point（规格指定的验证）
    for (i, _) in text.char_indices() {
        assert_eq!(rope.point_to_offset(rope.offset_to_point(i)), i);
    }
}
```

运行：`cargo test -p rope sum_tree_navigation_lab`。

**检查点**（每个编号对应代码里的同编号段落）：

- 2 验证「树只是 monoid 的折叠器」：你的线性折叠与树的分层折叠相等；
- 3 验证 Bias 的贴边规则与「一次查找两种坐标」在**每个**块边界上都成立，而不是碰巧一次；
- 4 验证 `DimensionPair` 的 key/value 语义：`value: None` 的目标只按 key 比较，游标的 value 却被下降途中的 `add_summary` 填满；
- 5/6 验证 `Cursor::summary::<D>` 的「任意维度」与坐标换算的可逆性——这正是 crate 自己在随机测试 [rope.rs:L2161-L2167](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L2161-L2167) 里反复对拍的那类不变量。

预期全部通过（**待本地验证**）。想再加码：仿照 `test_random_rope`（[rope.rs:L1887-L1911](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1887-L1911)）把第 6 步搬进 `#[gpui::test(iterations = 100)]` 的随机文本里——那就是 u3-l2 的正式内容。

## 6. 本讲小结

- rope 接入 SumTree 只需要两个挂点：`impl Item for Chunk`（块报摘要）与 `impl ContextLessSummary for ChunkSummary`（摘要累加委托给 u2-l2 的 `TextSummary` 合并代数）；树节点缓存的全部「智能」来自摘要 monoid 的结合律。
- `Dimension` 是摘要的投影（同态 \[ \phi(S_1 + S_2) = \phi(S_1) + \phi(S_2) \]），累计发生在查询下降途中、不占树的存储；`Dimension + Ord` 自动成为 `SeekTarget`，于是 `usize`/`Point`/`OffsetUtf16`/`PointUtf16` 都能导航同一棵树。
- `find::<D, _>((), &target, bias)` 返回 `(start, end, item)`：调用里 `()` 是无上下文的 Summary Context，`Bias` 决定目标落在块边界时贴左块还是右块（`Left` 命中 `end == 目标` 的前块，`Right` 命中 `start == 目标` 的后块）；复杂度 \( O(B \log_B n) \)，B = 12。
- `Dimensions<D1, D2>` 元组维度让一次下降同时维护两套坐标：键 `.0` 导航、值 `.1` 搭车，rope 全部坐标换算函数都是「`find<Dimensions<键, 值>>` + 块内位图换算」这一个模板。
- `TextDimension` 在 `Dimension` 之上补了 `from_chunk`/`from_text_summary`，解决区间两端不足整块时的现场计算；`Cursor::summary::<D>` 因此能返回任意维度（折叠机制在 `SummarySeekAggregate`：每跳过一段就 `D::add_summary` 一次）。
- `DimensionPair<K, V>` 是「key 比较、value 搭车」的命名维度，value 用 `Option` 支撑减法的「未知传染」；当前工作区内没有调用者，属于预留 API（**待确认**外部用途）。

## 7. 下一步学习建议

本讲把「树 + trait 接入 + 导航」讲完了，后续三条线各自展开：

1. **u2-l4（Chunk 内部：四张位图与定长缓冲）**：本讲反复出现的「块内位图换算」（`offset_to_point`、`text_summary`、`len_utf16`）终于要拆开看底牌——`chars`/`chars_utf16`/`newlines`/`tabs` 四张位图如何把块内统计变成 popcount 与掩码运算。
2. **u2-l7（遍历与读取）**：本讲只用了 `Cursor` 的 `seek`/`item`/`next`/`summary`/`slice`，`Chunks` 的正反迭代、`next_line`/`prev_line`（内部用 `search_forward(|summary| summary.text.lines.row > 0)` 这种**按摘要剪枝**的跳块技巧——本讲 Dimension 机制的直接进阶应用）、`Bytes` 的 `io::Read` 都在那里。
3. **u3-l2（测试策略）**：综合实践结尾埋的伏笔——把对拍不变量搬进 `#[gpui::test(iterations = N)]` 的随机文本，用固定 seed 保证可复现。

另外一个方向：如果想看 SumTree 的其他「用户」怎么写接入声明，可以对比 `crates/sum_tree` 文档注释里提到的用法与 `crates/text` 中按 `TextSummary` 导航的代码——同一套 Item/Summary/Dimension 模式在不同 crate 里的变奏，是检验你是否真正理解本讲内容的最好试金石。

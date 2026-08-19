# 从迭代器到树：构建 API 全景

## 1. 本讲目标

上一讲（u2-l2）我们学会了用 `Dimension` 与 `SeekTarget` 定义导航轴，但一直没有回答一个问题：**树一开始是怎么被造出来的？**

本讲系统讲解 `SumTree` 的全部构建入口。学完后你应该能够：

1. 说出 `new` / `from_summary` / `from_item` / `from_iter` / `from_par_iter` / `push` / `extend` / `par_extend` 各自的用途与适用场景。
2. 手动跟踪 `from_iter` 的执行过程：每次取 `2 * TREE_BASE` 个元素装满一个叶子，再逐层向上把节点组装成父节点，直到只剩一个根。
3. 解释 `from_par_iter` 为什么要求 `T: Send + Sync`、`IndexedParallelIterator` 等约束。
4. 理解 `push` / `extend` / `par_extend` 最终都汇聚到 `append` 这一个修改原语，以及「批量构建 + 一次 append」优于「逐个 push」的原因。
5. 用三种方式构建同一批元素并对拍验证，粗测它们的性能差异。

## 2. 前置知识

本讲假设你已读过前置讲义，以下概念会被直接使用：

- **Item 与 Summary（u2-l1）**：元素通过 `Item::summary(&self, cx)` 产出汇总；`Summary::add_summary` 按序列顺序做幺半群叠加。`IntegersSummary`（count/sum/contains_even/max）是本讲反复用到的测试汇总类型，它实现了 `ContextLessSummary`，因此 `Context<'a>` 固定为 `()`——这就是为什么本讲所有 API 调用都传一个 `()` 作为 `cx`。
- **Node 与 TREE_BASE（u1-l2）**：`Node` 是 `Internal`（height/summary/child_summaries/child_trees）与 `Leaf`（summary/items/item_summaries）两个变体的枚举；单节点容量上限为 \( 2 \times TREE\_BASE \)（本 crate 测试构建下 `TREE_BASE = 2`，即容量 4；正式构建下为 6，即容量 12）。`SumTree` 本身只是 `Arc<Node<T>>` 的包装。
- **Arc 写时复制（u1-l2）**：修改走 `Arc::make_mut`，只在引用计数大于 1 时克隆。

此外需要一点 Rust 标准库与 rayon 的常识：

- `Iterator::fuse()`：把一个「返回过 `None` 之后还可能再返回 `Some`」的迭代器变成「一旦返回 `None` 就永久耗尽」的迭代器。
- rayon 的 `ParallelIterator` 是并行版的 `Iterator`；`IndexedParallelIterator` 是额外支持精确长度与随机分片的并行迭代器（`chunks` 这类方法需要它）。

## 3. 本讲源码地图

本讲内容几乎全部集中在 crate 的根模块里：

| 文件 | 本讲涉及的内容 |
| --- | --- |
| [crates/sum_tree/src/sum_tree.rs:225-378](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L225-L378) | 一次性构建入口：`new`、`from_summary`、`from_item`、`from_iter`、`from_par_iter` |
| [crates/sum_tree/src/sum_tree.rs:750-794](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L750-L794) | 增量构建入口：`extend`、`par_extend`、`push`，以及它们汇聚到的 `append` |
| [crates/sum_tree/src/sum_tree.rs:1268-1281](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1268-L1281) | `Node` 枚举定义：构建过程装配的就是这个结构 |
| [crates/sum_tree/src/sum_tree.rs:1404-1414](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1404-L1414) | `test_extend_and_push_tree`：`extend` + `append` 的最小对拍测试 |
| [crates/sum_tree/src/sum_tree.rs:1803-1818](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1803-L1818) | `test_from_iter`：覆盖 `from_iter` 的空迭代器与「复活迭代器」边界 |
| [crates/sum_tree/Cargo.toml:16-23](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/Cargo.toml#L16-L23) | `heapless`（定容节点存储）与 `rayon`（并行构建）依赖 |

先给出全貌图。本讲的所有构建 API 最终只有两个「落点」：**直接手搓一个 `Node`**（`new` / `from_summary` / `from_iter` / `from_par_iter`），或者**走 `append`**（`from_item` / `push` / `extend` / `par_extend`）：

```
new(cx) ─────────────┐
from_summary(s) ─────┤                     ┌─ Self::new(cx)          （空输入）
from_item(item, cx) ┤                     │
  └─ new + push      │                     │  装叶子：每 2*TREE_BASE 个元素一个 Leaf
from_iter(iter, cx) ┼──► 手工装配 Node ────┤  装父亲：每 2*TREE_BASE 个节点一个 Internal
from_par_iter(...) ──┘                     └─ 逐层收敛到唯一根
                                            （from_par_iter 的两层装配是并行的）

push(item, cx)     ──► append(单元素叶子树, cx)
extend(iter, cx)   ──► append(from_iter(iter, cx), cx)
par_extend(iter,cx)──► append(from_par_iter(iter, cx), cx)
                       append 内部再分派到 push_tree_recursive / append_large / from_child_trees
```

`append` 的内部机制（分裂、欠溢合并）属于第 4 单元（u4-l1、u4-l2）的内容，本讲只把它当作一个可靠的黑盒：**把两棵树按顺序接成一棵**。

## 4. 核心概念与源码讲解

### 4.1 一次性入口：new、from_summary 与 from_item

#### 4.1.1 概念说明

这三个是最简单的构建入口，解决的问题是「给我一个初始状态的树」：

- `new(cx)`：空树。分配一个 `items` 与 `item_summaries` 都为空的叶子，`summary` 取 `Summary::zero(cx)`。
- `from_summary(summary)`：同样是空叶子，但允许你**预置汇总值**。文档注释特别说明它的使用场景：元素类型的 Context 很复杂、但汇总的零值不依赖 Context 时，可以绕过 `new(cx)` 直接构造。
- `from_item(item, cx)`：只有一个元素的树，实现就是 `new` + `push`。

另外还有一个便利入口：`Default`。当汇总类型满足 `Context<'a> = ()`（即实现了 `ContextLessSummary` 的包络）时，可以直接写 `SumTree::<T>::default()`，它等价于 `Self::new(())`。

#### 4.1.2 核心流程

```
new(cx):
  分配空 Leaf { summary: Summary::zero(cx), items: [], item_summaries: [] }
  包上 Arc，返回 SumTree

from_summary(summary):
  同上，但 summary 字段直接用传入值

from_item(item, cx):
  tree = new(cx)
  tree.push(item, cx)   // push 见 4.4
  tree

Default（要求 Context = (）):
  Self::new(())
```

注意「空树」并不是 `Option` 或空指针，而是一个真实存在的空叶子节点。这让所有读取路径（比如 `tree.summary()`）无需判空。

#### 4.1.3 源码精读

[crates/sum_tree/src/sum_tree.rs:226-232](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L226-L232) 中 `new` 用 `Summary::zero(cx)` 初始化叶子汇总，`items` 与 `item_summaries` 用 `ArrayVec::new()` 置空——这里的 `ArrayVec` 是 `heapless::Vec` 的别名（见 [crates/sum_tree/src/sum_tree.rs:7](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L7)），容量在类型里写死为 `2 * TREE_BASE`。

[crates/sum_tree/src/sum_tree.rs:234-247](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L234-L247) 依次是 `from_summary`（预置汇总的空叶子）与 `from_item`（`new` 后立刻 `push` 一个元素）：

```rust
pub fn from_summary(summary: T::Summary) -> Self {
    SumTree(Arc::new(Node::Leaf {
        summary,
        items: ArrayVec::new(),
        item_summaries: ArrayVec::new(),
    }))
}

pub fn from_item(item: T, cx: <T::Summary as Summary>::Context<'_>) -> Self {
    let mut tree = Self::new(cx);
    tree.push(item, cx);
    tree
}
```

`from_item` 不手工装叶子，而是复用 `push`——这是「能走 append 就走 append」这一设计取向的第一次体现。

[crates/sum_tree/src/sum_tree.rs:1258-1266](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1258-L1266) 是 `Default` 实现，约束 `S: for<'a> Summary<Context<'a> = ()>` 正是 u2-l1 讲过的「无环境汇总」包络。测试里到处可见的 `SumTree::<u8>::default()`（例如 [crates/sum_tree/src/sum_tree.rs:1406](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1406)）能写出来，就是因为 `u8` 的汇总 `IntegersSummary` 的 Context 是 `()`。

#### 4.1.4 代码实践

实践目标：验证空树的形态，以及 `from_summary` 的语义。

操作步骤（示例代码，可在 `mod tests` 内临时添加一个测试）：

```rust
#[test]
fn practice_empty_and_from_summary() {
    let empty = SumTree::<u8>::default();
    assert!(empty.is_empty());
    assert_eq!(empty.items(()), Vec::<u8>::new());

    // from_summary：空叶子 + 预置汇总
    let preset = SumTree::from_summary(IntegersSummary {
        count: 7,
        ..Default::default()
    });
    assert!(preset.is_empty());                          // 没有元素……
    assert_eq!(preset.extent::<Count>(()), Count(7));    // ……但汇总说有 7 个
}
```

需要观察的现象：`is_empty()` 为真（叶子无元素），但 `extent::<Count>()` 返回 7。预期结果：两个断言都通过——`from_summary` 构造的树「内容为空、账本非零」，这正是它存在的意义（由调用方自行保证账实相符）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `from_summary` 不需要 `cx` 参数，而 `new` 需要？

**答案**：`new` 必须调用 `Summary::zero(cx)` 来构造汇总的零值，零值可能依赖 Context（u2-l1 讲过带生命周期的 GAT `Context<'a>` 可以承载环境）；而 `from_summary` 的汇总值由调用方直接给出，完全绕开了 `zero`，所以签名里没有 `cx`。

**练习 2**：`SumTree::<T>::default()` 对哪些 `T` 可用？

**答案**：只对汇总类型满足 `for<'a> Summary<Context<'a> = ()>` 的 `T` 可用，即 `ContextLessSummary` 的包络实现（如测试中的 `IntegersSummary`）。Context 非 `()` 的汇总类型必须显式调用 `new(cx)`。

---

### 4.2 from_iter：自底向上逐层组装

#### 4.2.1 概念说明

`from_iter` 是最重要的批量构建入口：把一个普通迭代器一次性变成一棵形状规整的树。它解决的问题是——

- 如果用 `push` 逐个插入 \( n \) 个元素，每次都要走一遍 `append` 的合并逻辑，代价是 \( O(n \log n)\) 级别；
- 而 `from_iter` 先把元素**按满容量打包成叶子**，再**逐层把节点打包成父节点**，整棵树一次成型，总代价是 \( O(n) \)。

一句话记忆：**`from_iter` 是「装箱」**——叶子装元素、内部节点装子树，每一层每箱最多 `2 * TREE_BASE` 件，装满就封箱，最后剩一个根。

#### 4.2.2 核心流程

记单节点容量 \( C = 2 \times TREE\_BASE \)（本 crate 测试构建下 \( C = 4 \)，正式构建下 \( C = 12 \)）。

```
阶段一：装叶子
  iter = iter.into_iter().fuse().peekable()
  只要 iter.peek() 是 Some：
      取出最多 C 个元素 → items
      逐个调用 item.summary(cx) → item_summaries
      从 item_summaries[0] 出发，向右折叠 add_summary → 叶子 summary
      把这个 Leaf 包成 SumTree 压入 nodes

阶段二：逐层装父亲
  height = 0
  只要 nodes.len() > 1：
      height += 1
      顺序遍历 nodes，累积到一个新的 Internal（height 为当前值）
      每挂一个孩子：add_summary 到父汇总、child_summaries/child_trees 各 push 一份
      一旦 child_trees.len() == C → 封箱，开一个新的父节点
      本层结束后，nodes ← 本层全部父节点（parent_nodes）

收尾：
  nodes 为空      → Self::new(cx)（输入迭代器为空）
  nodes 只剩一个  → 它就是根
```

两个值得量化的性质：

1. **高度与元素数的关系**。高度为 \( h \) 的树最多容纳 \( C^{h+1} \) 个元素（叶子层 \( C^1 \)，第一层内部节点 \( C^2 \)，以此类推），即：

\[
n \le C^{h+1} \quad\Longleftrightarrow\quad h \ge \lceil \log_C n \rceil - 1
\]

   以 \( n = 10{,}000 \)、\( C = 12 \)（正式构建）为例：\( \log_{12} 10{,}000 \approx 3.7 \)，所以 4 层就够了（\( h = 3 \)）；本 crate 测试构建下 \( C = 4 \)，\( \log_4 10{,}000 \approx 6.6 \)，需要 7 层（\( h = 6 \)）。

2. **总工作量是线性的**。叶子层做了约 \( n \) 次 `add_summary`；第 \( k \) 层只有约 \( n / C^k \) 个节点、每个做至多 \( C - 1 \) 次叠加，全部加起来是一个等比级数：

\[
n \cdot \left(1 + \frac{1}{C} + \frac{1}{C^2} + \cdots\right) = n \cdot \frac{C}{C - 1} = O(n)
\]

还有两个容易被忽略的细节：

- **叶子是「从左到右装满」的**：除最后一个叶子外，每个叶子恰好 \( C \) 个元素；最后一个叶子可能不满，甚至可能少于 `TREE_BASE`（欠溢）。`from_iter` **不做任何再平衡**——欠溢只在后续 `append` 路径中才会被处理（`is_underflowing` 的检查在 `push_tree_recursive` / `append_large` 里，见 u4）。
- **`fuse()` 不是装饰**。`while iter.peek().is_some()` 会反复窥探迭代器；如果某个迭代器在返回过 `None` 之后又「复活」吐出新元素，没有 `fuse` 的话这个循环会无限装箱。项目专门有一个测试覆盖这一点（见下文源码精读）。

#### 4.2.3 源码精读

完整实现位于 [crates/sum_tree/src/sum_tree.rs:249-316](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L249-L316)。先看阶段一（装叶子）：

```rust
let mut iter = iter.into_iter().fuse().peekable();
while iter.peek().is_some() {
    let items: ArrayVec<T, { 2 * TREE_BASE }, u8> =
        iter.by_ref().take(2 * TREE_BASE).collect();
    let item_summaries: ArrayVec<T::Summary, { 2 * TREE_BASE }, u8> =
        items.iter().map(|item| item.summary(cx)).collect();

    let mut summary = item_summaries[0].clone();
    for item_summary in &item_summaries[1..] {
        <T::Summary as Summary>::add_summary(&mut summary, item_summary, cx);
    }
    ...
}
```

[crates/sum_tree/src/sum_tree.rs:255-271](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L255-L271)：`take(2 * TREE_BASE)` 保证单个叶子最多装 \( C \) 个元素；汇总的折叠从 `item_summaries[0]` 出发**从左向右**进行——顺序折叠保证了不可交换的汇总类型（u2-l1 强调过的语义）依然正确。`ArrayVec` 的第三个类型参数 `u8` 是 heapless 的长度索引类型，容量 \( \le 12 \) 远在 `u8` 范围内。

再看阶段二（逐层装父亲），位于 [crates/sum_tree/src/sum_tree.rs:274-308](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L274-L308)：

```rust
while nodes.len() > 1 {
    height += 1;
    let mut current_parent_node = None;
    for child_node in nodes.drain(..) {
        let parent_node = current_parent_node.get_or_insert_with(|| {
            SumTree(Arc::new(Node::Internal {
                summary: <T::Summary as Summary>::zero(cx),
                height,
                ...
            }))
        });
        ...
        child_summaries.push(child_summary.clone()).unwrap_oob();
        child_trees.push(child_node.clone()).unwrap_oob();

        if child_trees.len() == 2 * TREE_BASE {
            parent_nodes.extend(current_parent_node.take());
        }
    }
    parent_nodes.extend(current_parent_node.take());
    mem::swap(&mut nodes, &mut parent_nodes);
}
```

几个关键点：

- `current_parent_node` 是一个「正在装箱中的父节点」，装满 \( C \) 个孩子就 `take()` 出去封箱，下一个孩子自动开新箱；
- `push(...).unwrap_oob()` 里的 `unwrap_oob` 是 [crates/sum_tree/src/sum_tree.rs:20-29](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L20-L29) 定义的小工具：因为元素类型不要求实现 `Debug`，不能用 `Result::unwrap`，于是用十行代码把「必然装得下」这一不变量变成显式断言。装得下的理由就是上一行 `if`：到达容量即刻封箱；
- `Arc::get_mut(&mut parent_node.0).unwrap()`（[crates/sum_tree/src/sum_tree.rs:293](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L293)）：刚 `Arc::new` 出来的父节点必然独占所有权，`get_mut` 一定成功，失败即 `unreachable!()`。

收尾在 [crates/sum_tree/src/sum_tree.rs:310-315](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L310-L315)：输入为空时退回 `Self::new(cx)`；否则 `debug_assert_eq!(nodes.len(), 1)` 后弹出唯一的根。

最后看两个项目自带的测试。[crates/sum_tree/src/sum_tree.rs:1803-1818](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1803-L1818) 的 `test_from_iter` 除了一般的 100 元素对拍，还构造了一个「会复活」的迭代器——交替返回 `Some(1)` 与 `None`，断言 `from_iter` 只取到 `vec![1]`，这正是对 `fuse()` 的回归测试。[crates/sum_tree/src/sum_tree.rs:1834-1845](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1834-L1845) 的 `impl Item for u8` 则提醒我们：`u8 → IntegersSummary` 这套测试装配只存在于 `mod tests` 内，本讲所有实践代码都应写在同一个测试模块里。

#### 4.2.4 代码实践

实践目标：亲眼看到「装箱」的形状。

操作步骤（示例代码，`TREE_BASE = 2` 的测试构建下叶子容量为 4）：

```rust
#[test]
fn practice_from_iter_shape() {
    let tree = SumTree::from_iter(0..9u8, ());
    println!("{tree:#?}");
}
```

运行：

```bash
cargo test -p sum_tree practice_from_iter_shape -- --nocapture
```

需要观察的现象：9 个元素（0..9）会装成三个叶子 `[0,1,2,3]`、`[4,5,6,7]`、`[8]`，两个满叶子加一个只有 1 个元素的尾叶子；`nodes.len() = 3 > 1`，于是产生一个 `height: 1` 的根，带 3 个 `child_trees`。注意尾叶子只有 1 个元素，小于 `TREE_BASE = 2`——这就是「`from_iter` 不做欠溢处理」的直观证据（`is_underflowing` 的定义见 [crates/sum_tree/src/sum_tree.rs:1358-1363](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1358-L1363)）。

预期结果：根为 `Internal { height: 1, child_trees: [Leaf(4 项), Leaf(4 项), Leaf(1 项)] }`。若把元素数改成 4 或更少，应观察到根直接就是 `Leaf`（`nodes.len() == 1`，不进入装父亲的循环）。待本地验证（以实际 Debug 输出为准）。

#### 4.2.5 小练习与答案

**练习 1**：用 `from_iter` 构建 \( n = 13 \) 个元素的树（测试构建 \( C = 4 \)），叶子怎么分布？根的高度是多少？

**答案**：叶子为 `[0..4]`、`[4..8]`、`[8..12]`、`[12]` 共 4 个；`nodes.len() = 4 > 1`，装成一个 `height: 1` 的内部节点，它恰好装满 4 个孩子。根高度 1。

**练习 2**：如果去掉 `fuse()`，`test_from_iter` 中那个复活迭代器会导致什么行为？

**答案**：`peek()` 在每次取空一批后会再次窥探；复活迭代器会再次吐出 `Some(1)`，于是 `while iter.peek().is_some()` 永远为真，循环不断装出新的单元素叶子，测试无法终止（或耗尽内存）。`fuse()` 保证一旦见到 `None`，迭代器视为永久耗尽。

**练习 3**：`from_iter` 构建的树中，除最后一层的最后一个节点外，每个节点孩子数都是多少？这带来什么复杂度收益？

**答案**：除每层最后一个节点外都是满的 \( C = 2 \times TREE\_BASE \) 个孩子/元素。这使得整棵树的构建工作量是等比级数收敛的 \( O(n) \)，而逐个 `push` 是每次 \( O(\log n) \) 的追加，总计 \( O(n \log n) \)。

---

### 4.3 from_par_iter：rayon 并行构建

#### 4.3.1 概念说明

`from_par_iter` 是 `from_iter` 的并行孪生版：当元素本身的 `summary` 计算很贵（比如 rope 的 `ChunkSummary` 要统计行数、字节数、最大字符等），单线程装箱会成为瓶颈，于是把「装叶子」和「装父亲」都交给 rayon 线程池并行完成。整体算法结构与 `from_iter` 完全一致：**装箱两层循环不变，只是每一层的装箱改成了并行 `chunks`**。

#### 4.3.2 核心流程

```
阶段一（并行）：装叶子
  iter.into_par_iter().chunks(2 * TREE_BASE)
      每个分片并行地：collect 成 items → 求 item_summaries → 折叠出 summary → Leaf
      收集为 Vec<SumTree>

阶段二（逐层、层内并行）：装父亲
  height = 0
  只要 nodes.len() > 1：
      height += 1
      nodes = nodes.into_par_iter().chunks(2 * TREE_BASE)
          每个分片并行地：collect 成 child_trees/child_summaries → 折叠出 summary → Internal
          .collect::<Vec<_>>()

收尾：与 from_iter 相同（空 → new；否则唯一的根）
```

#### 4.3.3 源码精读

签名与约束位于 [crates/sum_tree/src/sum_tree.rs:318-325](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L318-L325)：

```rust
pub fn from_par_iter<I, Iter>(iter: I, cx: ...) -> Self
where
    I: IntoParallelIterator<Iter = Iter>,
    Iter: IndexedParallelIterator<Item = T>,
    T: Send + Sync,
    T::Summary: Send + Sync,
    for<'a> <T::Summary as Summary>::Context<'a>: Sync,
```

这四个约束每一条都对应一个真实的并行需求：

| 约束 | 为什么需要 |
| --- | --- |
| `IntoParallelIterator` | 入口本身要能变成并行迭代器 |
| `IndexedParallelIterator` | `chunks(2 * TREE_BASE)` 需要精确长度才能均匀分片；普通 `ParallelIterator` 提供不了 |
| `T: Send + Sync` | 元素会被移动到其他线程装箱 |
| `T::Summary: Send + Sync` 与 `Context<'a>: Sync` | 汇总与 `cx` 会被多个线程同时持有、克隆、调用 `add_summary` |

装叶子阶段在 [crates/sum_tree/src/sum_tree.rs:326-343](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L326-L343)，闭包体内的代码与串行版逐行对应（collect → 逐个 `item.summary(cx)` → 从左向右折叠），唯一的区别是它运行在 rayon 的工作线程上。装父亲阶段在 [crates/sum_tree/src/sum_tree.rs:345-370](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L345-L370)：每轮循环用 `into_par_iter().chunks(2 * TREE_BASE)` 把当前层的节点并行分组，各组独立组装出 `Internal` 节点后收集回 `Vec`，层与层之间仍是串行的 `while` 循环——这是算法本质决定的：**层 k+1 的输入是层 k 的输出，层内可并行、层间有依赖**。

收尾 [crates/sum_tree/src/sum_tree.rs:372-377](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L372-L377) 与串行版相同。`rayon` 是 crate 的正式依赖（[crates/sum_tree/Cargo.toml:18](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/Cargo.toml#L18)），所以 `par_extend` 对下游用户开箱即用。`test_random` 里两条构建路径都会被随机选中（[crates/sum_tree/src/sum_tree.rs:1436-1444](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1436-L1444)），保证并行构建与串行构建产出一致的树。

#### 4.3.4 代码实践

实践目标：体验「并行入口需要索引并行迭代器」这一约束。

操作步骤（示例代码）：

```rust
#[test]
fn practice_par_build() {
    use rayon::prelude::*;

    // Vec 的 IntoParallelIterator 是 IndexedParallelIterator，可以直接用
    let items: Vec<u8> = (0..1_000).map(|i| (i % 256) as u8).collect();
    let tree = SumTree::from_par_iter(items, ());
    assert_eq!(tree.extent::<Count>(()), Count(1_000));
}
```

然后把 `items` 换成 `items.into_par_iter().filter(|_| true)` 再编译一次（`filter` 之后的类型只是普通 `ParallelIterator`，不再是 `IndexedParallelIterator`）。

需要观察的现象：第二次编译报错，错误信息会指向 `chunks` 需要的 trait 约束。

预期结果：第一段编译通过、断言通过；第二段编译失败，因为 `filter` 会丢失精确长度信息。这正是签名上 `IndexedParallelIterator` 存在的原因。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么「装父亲」的 `while` 循环不能整体并行化，只能层内并行？

**答案**：第 k+1 层的每个父节点的汇总是其孩子汇总的折叠，而孩子是第 k 层的产物；层与层之间存在数据依赖。同一层内的各个父节点互不依赖，所以只能层内并行。

**练习 2**：并行版和串行版产出的树，内容与形状是否一致？

**答案**：内容（items 序列、各级汇总）完全一致——`test_random` 依赖这一点做对拍。形状上两者都保证「每节点至多 \( C \) 个孩子」，具体每层最后一个节点的孩子数可能因分箱顺序细节不同而不同，但这不影响任何 API 语义。

**练习 3**：对于 \( n = 10{,}000 \) 个 `u8`（汇总计算极廉价），并行构建一定比串行快吗？

**答案**：不一定。并行带来的分片、任务调度与线程同步有固定开销，当单个元素的 `summary` 计算极廉价时，开销可能超过收益，`par_extend` 甚至可能更慢。并行构建的收益场景是「汇总计算昂贵 + 元素量大」（如 rope 的文本块）。

---

### 4.4 push：单元素增量与 append

#### 4.4.1 概念说明

`push` 是最小粒度的增量构建：往树尾追加一个元素。它的实现策略非常「不直接」——**不是把元素塞进最右叶子，而是手工造一个只含该元素的单叶子树，然后调用 `append`**。这样做把「如何合并进已有树」的全部细节（最右叶子是否已满、高度是否匹配、是否需要分裂）统一交给 `append` 一处处理，避免了两套并行的修改逻辑。

#### 4.4.2 核心流程

```
push(item, cx):
  summary = item.summary(cx)
  single = Leaf { summary, items: [item], item_summaries: [summary] }  // 单元素树
  self.append(single, cx)
```

每次 `push` 的代价是 `append` 的代价：从根走到最右叶子的路径长度 \( O(\log n) \)（内部机制在 u4-l1 精读）。因此：

- 逐个 `push` \( n \) 个元素：\( O(n \log n) \)，且每次都可能触发 `Arc::make_mut` 的路径克隆；
- `from_iter` 一次装 \( n \) 个元素：\( O(n) \)。

这就是「已知全部数据时优先批量构建」的原因。

#### 4.4.3 源码精读

[crates/sum_tree/src/sum_tree.rs:768-778](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L768-L778)：

```rust
pub fn push(&mut self, item: T, cx: <T::Summary as Summary>::Context<'_>) {
    let summary = item.summary(cx);
    self.append(
        SumTree(Arc::new(Node::Leaf {
            summary: summary.clone(),
            items: ArrayVec::from_iter(Some(item)),
            item_summaries: ArrayVec::from_iter(Some(summary)),
        })),
        cx,
    );
}
```

注意单叶子树的 `summary` 字段与 `item_summaries[0]` 是同一个值的两份拷贝——这正是 u1-l2 讲过的「汇总存两份」冗余的最小样例。

再看它汇聚到的 `append` 顶层分派，位于 [crates/sum_tree/src/sum_tree.rs:780-794](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L780-L794)：

```rust
pub fn append(&mut self, mut other: Self, cx: ...) {
    if self.is_empty() {
        *self = other;
    } else if !other.0.is_leaf() || !other.0.items().is_empty() {
        if self.0.height() < other.0.height() {
            // 小树 self 追加到大树 other：append_large 路径
            ...
        } else if let Some(split_tree) = self.push_tree_recursive(other, cx) {
            *self = Self::from_child_trees(self.clone(), split_tree, cx);
        }
    }
}
```

本讲只需要读懂三个分派条件：(1) 自己是空树就直接接管 `other`（一次指针移动）；(2) `other` 是空叶子则什么都不做；(3) 其余情况按两树高度关系走 `push_tree_recursive`（等高或自己更高，把孩子下推合并）或 `append_large`（自己更矮，钻进大树的左下角），必要时用 `from_child_trees` 在顶端合成一个更高的新根。这三条内部路径分别在 u4-l1 与 u4-l2 精读。

#### 4.4.4 代码实践

实践目标：用 `Arc::ptr_eq` 观察写时复制——`push` 是否克隆根节点，取决于根是否被共享。

操作步骤（示例代码；`mod tests` 是根模块的子模块，可以访问私有字段 `tree.0`）：

```rust
#[test]
fn practice_push_cow() {
    use std::sync::Arc;

    // 场景 A：根被快照共享 → push 触发写时复制，根指针改变
    let mut tree = SumTree::from_iter(0..8u8, ());
    let snapshot = tree.clone();
    let root_before = Arc::as_ptr(&tree.0);
    tree.push(8, ());
    assert!(!Arc::ptr_eq(&tree.0, &snapshot.0));      // 旧根属于快照
    assert_eq!(snapshot.items(()), (0..8).collect::<Vec<u8>>());
    assert_ne!(Arc::as_ptr(&tree.0), root_before);    // 新根是克隆

    // 场景 B：根未被共享 → 原地修改，根指针不变
    let mut tree2 = SumTree::from_iter(0..8u8, ());
    let root2_before = Arc::as_ptr(&tree2.0);
    tree2.push(8, ());
    assert_eq!(Arc::as_ptr(&tree2.0), root2_before);  // 独占所有权，无需克隆
}
```

需要观察的现象：场景 A 中快照 `snapshot` 的内容保持 `0..8` 不变（结构共享 + 写时复制保证了持久化语义），而 `tree` 的根指针发生了变化；场景 B 中根指针保持不变。

预期结果：四个断言全部通过。这正是 u1-l2「克隆只是引用计数加一、修改只复制被共享的路径」在 `push` 上的可观测验证。待本地验证（场景 B 依赖「无其他持有者」这一编译器可见的事实，断言以实际运行结果为准）。

#### 4.4.5 小练习与答案

**练习 1**：`push` 为什么不直接修改最右叶子的 `items`，而要构造单元素树再 `append`？

**答案**：直接修改需要单独处理「最右叶子已满需要分裂」「高度不匹配」「汇总回写」等所有边界；走 `append` 则把这些逻辑集中在一处，`push`、`extend`、`par_extend` 以及用户直接调用的 `append` 共享同一套合并与平衡机制，避免多份易漂移的实现。

**练习 2**：向一棵有 \( 10^6 \) 个元素的树末尾 `push` 一个元素，需要复制大约多少个节点？

**答案**：从根到最右叶子一条路径，长度即树高 \( h + 1 \approx \log_C n + 1 \)（\( C = 12 \) 时约 7 个节点）；若根未被共享则连这些复制都不需要，直接原地改。

**练习 3**：`from_item` 与 `push` 的关系是什么？

**答案**：`from_item(item, cx)` 就是 `new(cx)` 之后立刻 `push(item, cx)`（见 4.1.3 引用的源码），它是「构造即含一个元素」的便捷入口。

---

### 4.5 extend 与 par_extend：批量增量的统一入口

#### 4.5.1 概念说明

`extend` 与 `par_extend` 解决「向已存在的树尾部追加一批元素」的问题。它们的实现是同一个模式的两行代码：**先用 4.2 / 4.3 的批量构建入口把新元素装成一棵规整的小树，再用 `append` 接上去**。

这个「先装满一箱再上车」的策略把成本从「每个元素一次合并」降到「整批一次合并」：

- 逐个 `push` \( m \) 个元素：\( m \) 次 `append`，每次 \( O(\log n) \)；
- `extend` \( m \) 个元素：\( O(m) \) 装箱 + 1 次 `append`。

#### 4.5.2 核心流程

```
extend(iter, cx):
  subtree = from_iter(iter, cx)        // 4.2：并行版换成 from_par_iter
  self.append(subtree, cx)
```

选择依据只有一条：数据是否已经全部在手。

| 场景 | 推荐入口 |
| --- | --- |
| 从零构建、数据全在手 | `from_iter` / `from_par_iter` |
| 向已有树尾部追加一批 | `extend` / `par_extend` |
| 流式、逐个到达 | `push` |
| 两棵现成的树合并 | 直接 `append` |

#### 4.5.3 源码精读

[crates/sum_tree/src/sum_tree.rs:750-766](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L750-L766) 两个函数体各只有一行：

```rust
pub fn extend<I>(&mut self, iter: I, cx: ...) 
where I: IntoIterator<Item = T>,
{
    self.append(Self::from_iter(iter, cx), cx);
}

pub fn par_extend<I, Iter>(&mut self, iter: I, cx: ...)
where /* 与 from_par_iter 相同的四条约束 */,
{
    self.append(Self::from_par_iter(iter, cx), cx);
}
```

`par_extend` 的约束集与 `from_par_iter` 完全一致（对比 [crates/sum_tree/src/sum_tree.rs:757-764](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L757-L764) 与 4.3.3 引用的签名）——因为它只是把活儿转包给后者。

项目自带的用法范本是 [crates/sum_tree/src/sum_tree.rs:1404-1414](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1404-L1414) 的 `test_extend_and_push_tree`：两棵树分别用 `extend` 构建（`0..20` 与 `50..100`），`append` 之后断言 `items(())` 等于两个区间串联。而 `test_random` 的 splice 循环（[crates/sum_tree/src/sum_tree.rs:1459-1470](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1459-L1470)）展示了生产代码的真实模式：用游标 `slice` 切出前缀，对新段随机选择 `extend` 或 `par_extend`，再 `append` 剩余后缀——**切片 + 批量构建 + 拼接**正是 rope 等下游做文本编辑的骨架。

验证工具 `items(())` 定义在 [crates/sum_tree/src/sum_tree.rs:380-390](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L380-L390)：它借一个游标从头遍历并克隆所有元素，本讲的实践对拍都以它为准（代价 \( O(n) \) 次克隆，仅测试用）。

#### 4.5.4 代码实践

实践目标：验证「append 一棵树不会碰坏被共享的旧树」，即 `extend` 的持久化安全性。

操作步骤（示例代码）：

```rust
#[test]
fn practice_extend_isolated() {
    let mut tree = SumTree::from_iter(0..20u8, ());
    let snapshot = tree.clone();

    tree.extend(50..100u8, ());

    assert_eq!(tree.items(()), (0..20).chain(50..100).collect::<Vec<u8>>());
    assert_eq!(snapshot.items(()), (0..20).collect::<Vec<u8>>()); // 旧树完好
    assert_eq!(tree.extent::<Count>(()), Count(70));
}
```

需要观察的现象：`extend` 之后 `tree` 与 `snapshot` 内容互不干扰，各自的总数分别为 70 与 20。

预期结果：三个断言全部通过（与 `test_extend_and_push_tree` 的断言形态一致）。待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：`extend` 的完整代价由哪两部分构成？

**答案**：`Self::from_iter(iter, cx)` 的 \( O(m) \) 装箱（\( m \) 为新元素数），加上一次 `append` 的对数级合并代价。

**练习 2**：如果把 `extend` 实现成 `for item in iter { self.push(item, cx) }`，功能还正确吗？性能会差多少？

**答案**：功能完全正确（`push` 与 `extend` 语义同为尾部追加），但代价从「\( O(m) \) 装箱 + 1 次合并」退化为 \( m \) 次 `append`（每次 \( O(\log n) \) 且伴随路径克隆），整体从近似线性退化为 \( O(m \log n) \)。

**练习 3**：`par_extend` 与 `extend` 的约束差异体现在哪里？为什么 `par_extend` 不能接受任意 `Iterator`？

**答案**：`par_extend` 要求 `IntoParallelIterator` 且其迭代器是 `IndexedParallelIterator`，外加 `T`/`T::Summary` 的 `Send + Sync` 与 `Context: Sync`；普通 `Iterator` 无法被 rayon 均匀分片（没有长度信息），也就无法 `chunks(2 * TREE_BASE)` 装箱。

---

## 5. 综合实践

把本讲全部知识串起来的任务：**用三种方式构建同一批 10,000 个元素，对拍一致性并粗测耗时**。

实践目标：

1. 验证 `from_iter`、循环 `push`、`par_extend` 三条构建路径产出内容完全一致的树。
2. 用 `std::time::Instant` 量化「批量构建」与「逐个追加」的实际差距，把 4.2 的复杂度分析落到实处。

操作步骤（示例代码，添加到 `mod tests` 内）：

```rust
#[test]
fn practice_build_three_ways() {
    use std::time::Instant;

    const N: usize = 10_000;
    let source: Vec<u8> = (0..N).map(|i| (i % 256) as u8).collect();

    // 方式一：from_iter 一次装箱
    let t0 = Instant::now();
    let tree_a = SumTree::from_iter(source.iter().copied(), ());
    let t1 = Instant::now();

    // 方式二：逐个 push
    let mut tree_b = SumTree::<u8>::default();
    for &item in &source {
        tree_b.push(item, ());
    }
    let t2 = Instant::now();

    // 方式三：先空树再 par_extend（批量并行）
    let mut tree_c = SumTree::<u8>::default();
    tree_c.par_extend(source.clone(), ());
    let t3 = Instant::now();

    // 对拍：三者内容一致，且都等于参考序列
    let items_a = tree_a.items(());
    assert_eq!(items_a, tree_b.items(()));
    assert_eq!(items_a, tree_c.items(()));
    assert_eq!(items_a, source);
    assert_eq!(tree_a.extent::<Count>(()), Count(N));

    println!(
        "from_iter: {:?}; push x {N}: {:?}; par_extend: {:?}",
        t1 - t0,
        t2 - t1,
        t3 - t2
    );
}
```

运行（`--nocapture` 用于看到 `println!` 输出）：

```bash
cargo test -p sum_tree practice_build_three_ways -- --nocapture
```

需要观察的现象：

1. 所有断言通过——三条路径产出相同的元素序列与相同的 `Count` 总量。
2. 计时输出中三种方式的耗时排序。按 4.2/4.4 的分析，`from_iter` 应明显快于「循环 push 一万次」；`par_extend` 因为 `u8` 的汇总计算极廉价（4.3.5 练习 3），未必快于串行 `from_iter`，且测试构建 `TREE_BASE = 2` 下节点数很多，差距会进一步放大。

预期结果：断言全部通过；典型排序为 `from_iter` ≤ `par_extend` ≪ `push × 10000`。具体数字依赖机器与 rayon 线程池状态，**待本地验证**——请把你的实测数字记录下来，与复杂度结论对照。

延伸思考（选做）：把 `N` 改成 `100` 再跑一次，观察「批量构建」的优势是否缩小；想想为什么 `push` 在小 `N` 时几乎不吃亏（提示：树高很小，`append` 路径很短）。

## 6. 本讲小结

- `SumTree` 的构建入口分两族：**手工装配 `Node`**（`new` / `from_summary` / `from_iter` / `from_par_iter`）与**走 `append`**（`from_item` / `push` / `extend` / `par_extend`）。
- `from_iter` 是「装箱」算法：每 \( 2 \times TREE\_BASE \) 个元素装一个叶子，每 \( 2 \times TREE\_BASE \) 个节点装一个父节点，逐层收敛到唯一根，总工作量 \( O(n) \)；`fuse()` 防御「返回 `None` 后复活」的迭代器。
- `from_iter` 只装箱、不再平衡：每层最后一个节点可能欠溢，欠溢的修复推迟到后续 `append` 路径。
- `from_par_iter` 与串行版算法同构，层内并行（`chunks`）、层间串行；`IndexedParallelIterator` 与 `Send + Sync` 约束分别来自分片与跨线程共享的需要。
- `push` = 构造单元素叶子树 + `append`；`extend` = `append(from_iter(...))`；`par_extend` = `append(from_par_iter(...))`——所有增量修改汇聚到 `append` 一个原语。
- 批量构建（\( O(m) \) 装箱 + 1 次合并）在数据成批到达时严格优于逐个 `push`（\( m \) 次 \( O(\log n) \) 合并）。

## 7. 下一步学习建议

构建 API 讲完，读取路径是下一个自然主题：第 3 单元（u3）将进入 `Cursor`——栈式导航、`seek` 与 `Bias` 边界语义、`slice`/`summary` 聚合。建议带着两个问题去读：

1. `items(())`（本讲的验证工具）内部用的 `cursor::<()>(cx)` + `next()` 是怎么走树的？（入口在 [crates/sum_tree/src/cursor.rs](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs)）
2. `test_random` 的 splice 循环里 `cursor.slice(&Count(splice_start), Bias::Right)` 是如何「顺带」重建一棵树的？

如果你更关心写路径，可以直达第 4 单元：u4-l1 精读本讲反复出现的 `append` 内部（`push_tree_recursive` 的等高展开、高度差递归与 `from_child_trees` 升根），u4-l2 补上 `append_large` 与 `merge_into_right` 的欠溢处理。

# append 与节点分裂：push_tree_recursive 精读

## 1. 本讲目标

在 u2-l3 中我们把 `append` 当作一个黑盒：`push`、`extend`、`par_extend` 全都「最终汇聚到 append」。本讲打开这个黑盒，读完你应当能够：

1. 逐行跟踪 `SumTree::append` 的顶层分派逻辑，说清四种情况各自走到哪里。
2. 跟踪两棵任意高度的树 append 时，**每一层**节点发生的变化：等高时孩子被「摊开上提」、高度差为 1 时整树挂载、高度差更大时递归下推。
3. 理解溢出条件 `child_count > 2 * TREE_BASE` 与 `midpoint = (child_count + child_count % 2) / 2` 的对半分裂策略，并能证明分裂后两半都不越界、不欠溢。
4. 解释为什么一次 append 之后树高最多增加 1。
5. 理解 `Arc::make_mut` 在递归修改路径上的写时复制行为：为什么修改一棵被共享的树只付出「一条根到叶路径」的复制成本。

本讲是整个 crate 中算法密度最高的部分，建议边读边在纸上画树。

## 2. 前置知识

### 2.1 B+ 树的容量上下界与「欠溢」

u1-l2 已经介绍过：叶子存元素，内部节点只存子树；单个节点的孩子数（叶子则是元素数）**上限**为 \( 2 \times \text{TREE\_BASE} \)（测试构建 4，正式构建 12）。本讲补上故事的另一半——**下限**：一个「健康」的非根节点至少要有 TREE_BASE 个孩子/元素。节点太瘦（低于下限）称为**欠溢（underflow）**，判定函数就是本讲要精读的 `Node::is_underflowing`。上限靠「分裂」维护，下限靠「合并/摊开」维护，append 的两条内部路径分别对应这两件事：

- `push_tree_recursive`：大树在后、小树在前（本章主角）；
- `append_large` / `merge_into_right`：小树在前、大树在后（下一讲 u4-l2 的主角）。

### 2.2 高度约定

回顾 u1-l2 的约定：叶子高度为 0，内部节点的 `height` 字段比其孩子高 1。因此「高度为 h 的节点的孩子必须高度为 h-1」是一条硬性结构约束——本讲会看到 `push_tree_recursive` 的三种分支正是围绕这条约束展开的。

### 2.3 from_iter 的「自底向上装箱」作为对照

u2-l3 精读过 `from_iter`：每 2×TREE_BASE 个元素装一个叶子，再逐层向上组装父节点。它是**自底向上**的批量构建。而 `append` 是**自顶向下**的增量修改：从根开始逐层决定把新内容放在哪，必要时分裂并把分裂结果向上冒泡。两种视角对照着看，B+ 树的维护逻辑会清晰很多。

### 2.4 为什么 append 值得精读

u3-l3 的综合实践中已经出现过编辑范式：`cursor.slice(...)` 切出前缀 + `push` 换入新元素 + `append` 接回后缀。也就是说，sum_tree 上一切「修改」最终都是若干次 append。append 的成本（复制多少节点、分享多少子树）直接决定了 Zed 文本编辑的性能特征。

### 2.5 Arc::make_mut 回顾

u1-l2 讲过 `SumTree` 就是 `Arc<Node>` 的包装：克隆只是引用计数加一。修改时的关键调用是 `Arc::make_mut(&mut self.0)`：

- 引用计数为 1（没有别人共享这个节点）→ 原地返回可变引用，零成本；
- 引用计数大于 1（存在快照/克隆）→ 先克隆**这一层**的 `Node`（注意 `Node` 的克隆是浅克隆：`child_trees` 里的 `Arc` 依然共享），再对副本返回可变引用。

于是一次自顶向下的递归修改，复制量恰好是「根到被改叶」一条路径上每层一个节点壳，路径之外的所有子树原封不动地结构共享——这就是**路径复制（path copying）**。

## 3. 本讲源码地图

本讲只涉及一个源码文件，但按行号分区阅读：

| 代码区域 | 行号 | 作用 |
| --- | --- | --- |
| `TREE_BASE` 定义 | L15-L18 | 测试构建为 2、正式构建为 6 的编译期切换 |
| `SumTree::extend` / `push` | L750-L778 | append 的两个上游调用者 |
| `SumTree::append` | L780-L794 | 顶层入口：按高度与空否分派 |
| `SumTree::push_tree_recursive` | L796-L918 | 本章主角：递归下推、三种高度关系、分裂 |
| `append_large` / `merge_into_right` | L921-L1100 | 反向情形（self 更矮），下一讲精读 |
| `SumTree::from_child_trees` | L1102-L1120 | 用两棵同高子树造一个新根 |
| `Node` 枚举定义 | L1269-L1281 | Internal/Leaf 的字段布局 |
| `Node::height` / `is_underflowing` 等 | L1316-L1364 | 节点辅助方法 |
| `mod tests` 中 `impl Item for u8` | L1820-L1866 | 实践代码依赖的测试类型 |

文件：[src/sum_tree.rs](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs)

## 4. 核心概念与源码讲解

### 4.1 SumTree::append：按高度分派的拼接入口

#### 4.1.1 概念说明

`append(other)` 把 `other` 的全部内容拼到 `self` 末尾，**元素顺序必须保持 self 在前、other 在后**。这一顺序约束是理解分派逻辑的钥匙：两棵树高度不一时，不能简单交换操作数，因为「把大树拼到小树后面」和「把小树拼到大树前面」需要完全不同的下钻方向。

append 是全 crate 唯一的增量修改原语：

- `push(item)` = 构造一个单元素叶子树 + `append`（见 [src/sum_tree.rs:L768-L778](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L768-L778)，把单个 item 连同它的 summary 装进一个 `Node::Leaf` 再调用 append）；
- `extend(iter)` = `from_iter(iter)` 批量建树 + `append`（见 [src/sum_tree.rs:L750-L755](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L750-L755)）；
- u3-l3 见过的 `insert_or_replace` / `remove` 也是 slice + push + append 的组合。

#### 4.1.2 核心流程

append 顶层只有四条路，可以画成下面的判定流：

```text
append(self, other):
  ├─ self 为空                → *self = other        （直接接管，零复制）
  ├─ other 为空叶子           → 什么都不做            （原样返回）
  ├─ self.height < other.height → append_large(self.clone(), &mut other)
  │                              ├─ 返回 Some(tree) → from_child_trees(tree, other) 作为新 self
  │                              └─ 返回 None       → *self = other
  └─ self.height ≥ other.height → push_tree_recursive(self, other)
                                 ├─ 返回 None        → 完成，self 已原地吸收
                                 └─ 返回 Some(split) → from_child_trees(self.clone(), split) 作为新 self
```

两个细节值得注意：

1. 「other 为空」的判断写作 `!other.0.is_leaf() || !other.0.items().is_empty()`——other 是内部节点时必然非空（内部节点至少有一个孩子），是叶子时看 `items` 是否为空。`is_empty` 的定义见 [src/sum_tree.rs:L743-L748](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L743-L748)（Internal 恒为非空，Leaf 看 items）。提前返回空树避免了后续路径产生无意义的分裂或空节点。
2. 只有「self 不矮于 other」才走 `push_tree_recursive`；反向情形交给 `append_large`（下一讲），它把小树沿着大树的最左脊柱下钻。本讲聚焦前者。

#### 4.1.3 源码精读

append 本体只有 14 行，全部逻辑在分派上——[src/sum_tree.rs:L780-L794](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L780-L794)：这段代码先处理两个空树捷径，再按 `self.0.height() < other.0.height()` 分成 append_large 与 push_tree_recursive 两条路径，两条路径若产生分裂树（`Some`），统一用 `from_child_trees` 造一个高一层的新根。

```rust
pub fn append(&mut self, mut other: Self, cx: <T::Summary as Summary>::Context<'_>) {
    if self.is_empty() {
        *self = other;
    } else if !other.0.is_leaf() || !other.0.items().is_empty() {
        if self.0.height() < other.0.height() {
            if let Some(tree) = Self::append_large(self.clone(), &mut other, cx) {
                *self = Self::from_child_trees(tree, other, cx);
            } else {
                *self = other;
            }
        } else if let Some(split_tree) = self.push_tree_recursive(other, cx) {
            *self = Self::from_child_trees(self.clone(), split_tree, cx);
        }
    }
}
```

三个观察点：

- `*self = other` 是移动语义，代价为零——「空树接管」不会触碰任何节点。
- `push_tree_recursive` 拿走 `other` 的所有权，因为它可能把 `other` 的孩子搬空、把 `other` 的节点壳直接丢弃（见 4.2 的等高分支）。
- 若 `push_tree_recursive` 返回 `Some(split_tree)`，注意传入 `from_child_trees` 的是 `self.clone()`——因为 `push_tree_recursive(&mut self, ...)` 结束后 self 已变成「左半」，这里克隆一次 Arc 以满足 `from_child_trees(left, right)` 的按值参数。这次克隆只是引用计数加一（u1-l2 讲过的廉价快照）。

#### 4.1.4 代码实践

1. **实践目标**：用 `Arc::ptr_eq` 直接「看见」两条空树捷径，验证它们确实零成本。
2. **操作步骤**：在 `src/sum_tree.rs` 的 `mod tests` 中（必须是 crate 内部，才能访问私有字段 `tree.0`，并享受 `TREE_BASE = 2`）加入下面的测试（示例代码）：

   ```rust
   #[test]
   fn test_append_empty_shortcuts() {
       // 情形一：self 为空 → 直接接管，两个句柄指向同一个节点
       let mut empty: SumTree<u8> = SumTree::default();
       let tree = SumTree::from_iter(0..3, ());
       empty.append(tree.clone(), ());
       assert!(Arc::ptr_eq(&empty.0, &tree.0));

       // 情形二：other 为空叶子 → 完全不动，连 Arc 都没碰
       let mut tree = SumTree::from_iter(0..3, ());
       let snapshot = tree.clone();
       tree.append(SumTree::default(), ());
       assert!(Arc::ptr_eq(&tree.0, &snapshot.0));
       assert_eq!(tree.items(()), vec![0, 1, 2]);
   }
   ```

3. **需要观察的现象**：两个 `ptr_eq` 断言均通过；第二个断言说明追加空树之后 `tree.0` 与快照共享同一分配。
4. **预期结果**：测试通过。如果第二条断言失败，说明空树路径做了多余的事（复制或重建）。
5. `mod tests` 需要 `use std::sync::Arc;`——文件顶部（L11）已有该导入，测试模块通过 `use super::*;`（L1395）继承。

#### 4.1.5 小练习与答案

**练习 1**：为什么 append 不能在 `self.height < other.height` 时简单地交换两棵树再调用 `push_tree_recursive`？

**答案**：`push_tree_recursive(other)` 语义上是「把 other 的内容放到 self **之后**」，元素顺序是硬约束。交换操作数会得到「self 在后、other 在前」的序列，与 append 的契约相反。所以反向情形需要 `append_large`：保持 other（大树）为主体，把 self（小树）沿着大树的最左脊柱下钻塞到大树最前面。

**练习 2**：`push` 一个元素也会走完整的 append 分派（构造单元素叶子 → append）。结合 `extend` 的实现说明：为什么成批数据应该用 `extend` 而不是循环 `push`？

**答案**：循环 push n 次会做 n 次 append，每次都可能触发从叶到根的分裂与摘要回写，总代价 \( O(n \log n) \) 量级；`extend` 先用 `from_iter` 以 \( O(n) \) 自底向上装箱成一棵平衡树，再一次 append 拼接。这正是 u2-l3 的结论，现在可以从 append 的实现侧再确认一遍。

### 4.2 SumTree::push_tree_recursive：写时复制与三种高度关系

#### 4.2.1 概念说明

`push_tree_recursive(&mut self, other)` 的任务是：把 `other`（一棵不高于 self 的树）塞进 self 的「右下角」。它的返回值是理解全函数的钥匙：

- 返回 `None`：self 原地吸收了 other 的全部内容，没有溢出；
- 返回 `Some(右半)`：self 装不下，**self 保留左半，返回值是与 self 同高的一棵新树装着右半**。注意「同高」——分裂不改变这一层的高度，升层是 `from_child_trees` 在 append 顶层做的事。

函数入口第一行的 `Arc::make_mut(&mut self.0)` 是 sum_tree「并发友好」的机关所在（见 2.5）：如果这棵树有快照存在，这一层节点被复制成私有副本；递归下钻时每一层都各自 `make_mut`，于是整条修改路径被复制，而路径之外的子树与快照保持共享。

#### 4.2.2 核心流程

对每个节点先按 `height_delta = self.height - other.height` 分三种情况，最后统一处理可能的溢出：

```text
push_tree_recursive(self, other) -> Option<同高的右半树>:
  node = Arc::make_mut(&mut self.0)          # 写时复制这一层
  若 node 是 Internal:
      node.summary += other.summary           # 入口先记账（无论走哪条分支）
      delta = node.height - other.height
      ├─ delta == 0:  把 other 的全部孩子上提一层，追加到 self 的孩子列表尾部
      ├─ delta == 1 且 other 不欠溢: 把 other 整棵（一个 Arc 克隆）挂为最右孩子
      └─ 其他（delta ≥ 2，或 delta == 1 但 other 欠溢）:
             递归 push 进 self 当前最右子树；
             回写最右孩子的 summary；若递归返回分裂树，也加入待追加列表
      若 孩子数 > 2*TREE_BASE: 对半分裂，左半留 self，右半打包返回 Some(...)
      否则: 追加待追加列表，返回 None
  若 node 是 Leaf:
      合并两侧 items；若总数 > 2*TREE_BASE 同样对半分裂，右半返回 Some(...)
```

三种高度关系的设计动机：

- **delta == 0（等高，摊开上提）**：self 的孩子必须比 self 矮一层，而 other 与 self 同高，直接挂会违反结构约束。所以把 other 的节点壳丢弃、它的孩子们整体上提一层，成为 self 新的最右侧孩子们。代价是要克隆 other 全部孩子的 summary 与 Arc（最多 2×TREE_BASE 份），换来的是树的致密。
- **delta == 1（整树挂载）**：other 恰好比 self 矮一层，正符合孩子的高度要求。挂载只需一次 `Arc` 克隆（`trees_to_append.push(other)`），other 的整棵子树原样结构共享——这是三种情况里最便宜的。附加条件 `!other_node.is_underflowing()`：如果 other 是个「瘦子」，直接挂上去会在树中添一个新的欠溢节点，不如走下一条路把它摊进已有的最右子树。
- **delta ≥ 2 或 delta == 1 且欠溢（递归下推）**：把 other 继续往 self 的最右子树里塞。注意递归回来后必须做一件事——**回写最右孩子的 summary**（`*child_summaries.last_mut() = ...`），因为孩子吸收内容后它的 summary 变了，而父节点路由表里的副本不会自动更新（u1-l2 讲过「汇总存两份」的同步义务，这里就是回写点之一）。

#### 4.2.3 源码精读

**入口与写时复制**——[src/sum_tree.rs:L801-L810](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L801-L810)：这段代码用 `Arc::make_mut` 拿到本层节点的可变引用（必要时复制节点壳），并**先于任何分支判定**把 other 的 summary 并入 self 的 summary——因为无论内容最终放在哪个孩子里，self 的汇总都要包含 other 的全部。

```rust
match Arc::make_mut(&mut self.0) {
    Node::Internal {
        height,
        summary,
        child_summaries,
        child_trees,
        ..
    } => {
        let other_node = other.0.clone();
        <T::Summary as Summary>::add_summary(summary, other_node.summary(), cx);

        let height_delta = *height - other_node.height();
```

`other.0.clone()` 只克隆根节点的 Arc，后面三个分支通过 `Node` 的统一访问器（`child_summaries()`、`child_trees()`、`items()`，定义在 [src/sum_tree.rs:L1335-L1356](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1335-L1356)）读取 other 的内容，而不匹配它的具体变体。

**三种高度关系**——[src/sum_tree.rs:L813-L837](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L813-L837)：这段代码按 `height_delta` 把 other 的内容放入「待追加」列表——等高时摊开上提 other 的全部孩子，高度差 1 且不欠溢时整树挂载，否则递归下推到最右子树并回写其 summary。

```rust
let mut summaries_to_append = ArrayVec::<T::Summary, { 2 * TREE_BASE }, u8>::new();
let mut trees_to_append = ArrayVec::<SumTree<T>, { 2 * TREE_BASE }, u8>::new();
if height_delta == 0 {
    summaries_to_append.extend(other_node.child_summaries().iter().cloned());
    trees_to_append.extend(other_node.child_trees().iter().cloned());
} else if height_delta == 1 && !other_node.is_underflowing() {
    summaries_to_append
        .push(other_node.summary().clone())
        .unwrap_oob();
    trees_to_append.push(other).unwrap_oob();
} else {
    let tree_to_append = child_trees
        .last_mut()
        .unwrap()
        .push_tree_recursive(other, cx);
    *child_summaries.last_mut().unwrap() =
        child_trees.last().unwrap().0.summary().clone();

    if let Some(split_tree) = tree_to_append {
        summaries_to_append
            .push(split_tree.0.summary().clone())
            .unwrap_oob();
        trees_to_append.push(split_tree).unwrap_oob();
    }
}
```

四个细节：

1. 两个 `ArrayVec` 的容量都是 `2 * TREE_BASE`：等高分支最多搬 other 的 2×TREE_BASE 个孩子，恰好装满；挂载/分裂分支最多 push 1 项。因此后面的 `unwrap_oob`（u1-l2 介绍过的十行断言助手，见 [src/sum_tree.rs:L20-L29](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L20-L29)）必然成立。
2. 等高分支里被丢弃的只是 other 的**节点壳**，她的孩子们通过 Arc 克隆全部存活——结构依然共享。
3. 递归分支中 `child_trees.last_mut().unwrap()` 的两次 `unwrap` 依赖「Internal 节点至少有一个孩子」这一内部不变量。
4. 递归返回的分裂树作为「一个新的待追加孩子」进入本层判定——这就是分裂向上冒泡的传导方式，每层最多冒泡一棵。

**叶子分支**——[src/sum_tree.rs:L875-L915](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L875-L915)：这段代码处理 other 也是叶子（delta 必为 0）的情形：合并两侧元素，装不下就对半分裂，右半作为新叶返回。

```rust
let child_count = items.len() + other_node.items().len();
if child_count > 2 * TREE_BASE {
    // ……对半分裂：midpoint 之前留 self，其余打包成新 Leaf 返回……
} else {
    <T::Summary as Summary>::add_summary(summary, other_node.summary(), cx);
    items.extend(other_node.items().iter().cloned());
    item_summaries.extend(other_node.child_summaries().iter().cloned());
    None
}
```

注意叶子分支没有「挂载」选项——两个叶子之间不存在「高度差 1」的挂载关系，合并是唯一选择。另外 `other_node.child_summaries()` 对 Leaf 返回的正是 `item_summaries`（见访问器 L1335-L1342 的 match），统一接口让这段代码无需区分。

#### 4.2.4 代码实践

1. **实践目标**：亲手跟踪一次「delta = 2 + 欠溢下推」的递归（本讲最绕的路径），验证逐层推演与程序实际行为一致。
2. **操作步骤**：
   - 在纸上画出 `SumTree::from_iter(0..17, ())` 的形状（TREE_BASE = 2，容量 4）：叶子为 `[0,1,2,3] [4,5,6,7] [8,9,10,11] [12,13,14,15] [16]`，共 5 个；第二层组装成 `父1 = [叶1..叶4]`（4 个孩子，满）、`父2 = [叶5]`（1 个孩子）；根高 2，孩子为 `[父1, 父2]`。
   - 推演 `tree.append(SumTree::from_iter(17..18, ()), ())`：
     - 根（h2）对单元素叶（h0）：delta = 2 → 走递归分支，下推给 `父2`；
     - `父2`（h1）对叶（h0）：delta = 1，但叶只有 1 个元素 `< TREE_BASE = 2`，**欠溢** → 仍走递归分支，下推给 `叶5`；
     - `叶5`（h0）对叶（h0）：delta = 0 → 摊开合并，`[16] + [17] = [16,17]`，共 2 项 ≤ 4，不分裂，返回 `None`；
     - `父2` 回写 `叶5` 的新 summary，孩子数仍为 1，返回 `None`；根同理。
   - 在 `mod tests` 中加入下面的测试验证（示例代码）：

   ```rust
   #[test]
   fn test_append_descend_and_flatten() {
       let mut tree = SumTree::from_iter(0..17, ());
       assert_eq!(tree.0.height(), 2);
       tree.append(SumTree::from_iter(17..18, ()), ());
       assert_eq!(tree.0.height(), 2);
       assert_eq!(tree.items(()), (0..18).collect::<Vec<u8>>());
       println!("{:#?}", tree);
   }
   ```

3. **需要观察的现象**：高度保持 2；`println!` 的输出中 `父2` 仍然只有 1 个孩子（树中允许存在这样的瘦节点），而最右侧叶子变成了 `[16, 17]`。
4. **预期结果**：三个断言全部通过；`{:#?}` 的多行输出与你在纸上的画法逐层对应（Debug 格式由 [src/sum_tree.rs:L1283-L1313](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1283-L1313) 的手工实现决定：Internal 打印 height/summary/child_summaries/child_trees，Leaf 打印 summary/items/item_summaries）。
5. 若想确认 `父2` 的孩子数，可在测试里用 `tree.0.child_trees()[1].child_trees().len()`（`child_trees()` 定义于 [src/sum_tree.rs:L1344-L1349](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1344-L1349)，crate 内可见），预期为 1。

#### 4.2.5 小练习与答案

**练习 1**：`height_delta == 1` 且 other 欠溢时走的是递归分支。此时递归调用 `child_trees.last_mut().push_tree_recursive(other, cx)` 内部的 `height_delta` 是多少？会发生什么？

**答案**：self 的最右孩子高度为 `self.height - 1`，与 other 相同，所以递归内 `height_delta == 0`，走等高摊开分支——other 的孩子们被上提合并进 self 原有的最右孩子。也就是说，欠溢的 other 不是被「挂」上去，而是被「摊」进去，避免新增一个瘦节点。

**练习 2**：入口处 `add_summary(summary, other_node.summary(), cx)` 发生在分支判定**之前**。如果把它挪到各分支内部（只在真正吸收内容时记账），会有问题吗？

**答案**：不会有正确性问题，但会显著冗余——三条分支最终都会完整吸收 other 的内容（摊开吸收其全部孩子、挂载吸收整棵、递归则由下层节点吸收后再回写孩子的 summary），本层 self 的汇总总是要加上 other 的全量汇总。放在入口统一做一次，分支内就只需处理孩子级的回写。这也和 u2-l1 讲过的「add_summary 按序列顺序叠加」一致：self 在前、other 在后，入口处一次性前缀并入。

**练习 3**：一次 `push_tree_recursive` 调用中，`Arc::make_mut` 最多会在多少个节点上触发复制？复制的总成本是即什么量级？

**答案**：递归沿最右脊柱下钻，每层调用一次 `make_mut`，树高为 h 时最多 h+1 个节点各复制一次节点壳（每个壳是固定大小的 ArrayVec 结构，子树 Arc 依旧共享）。所以单次 append 的复制成本是 \( O(\log n) \) 个节点壳，与树高同阶——这正是「路径复制」的代价模型。

### 4.3 节点分裂与 from_child_trees：midpoint 对半与树高上界

#### 4.3.1 概念说明

分裂（split）解决的问题是：吸收新内容后节点孩子数超过容量上限 \( 2B \)（\( B = \text{TREE\_BASE} \)）。策略是**对半分**：左半留在原节点，右半打包成一个**同高**新节点向上返回。`from_child_trees` 则是唯一会「长高」的函数：它把两棵同高的树包成一个高一层的新根。

分裂时左右两半的规模可以用 midpoint 公式验证。设溢出后孩子数 \( c \in (2B, 4B] \)（下界来自 `child_count > 2B` 的触发条件；上界来自「原有孩子 ≤ 2B，等高摊开最多再并入 2B」），midpoint 取

\[ m = \left\lceil \frac{c}{2} \right\rceil = \frac{c + (c \bmod 2)}{2} \]

则左半 \( m \) 个、右半 \( c - m = \lfloor c/2 \rfloor \) 个，于是：

\[ \left\lfloor \frac{c}{2} \right\rfloor \geq B, \qquad \left\lceil \frac{c}{2} \right\rceil \leq 2B \]

即两半都**不超容量、不欠溢**（奇数时左半多拿一个）。这就是为什么一次分裂就足以让该节点重新合法，不需要级联重排。

#### 4.3.2 核心流程

```text
分裂（Internal 版，Leaf 版同构）:
  c = child_trees.len() + trees_to_append.len()
  若 c > 2B:
      m = ceil(c / 2)
      拼接序列 = child_trees ++ trees_to_append （summary 同序拼接）
      self 保留前 m 个，重算 self.summary
      返回 Some(新 Internal { height: self.height, 后 c-m 个 })
  否则:
      child_trees.extend(trees_to_append)
      返回 None

from_child_trees(left, right):
  要求 left.height == right.height
  返回新 Internal { height: left.height + 1, 孩子 = [left, right] }
```

树高上界的证明（本讲学习目标之三）：

1. **每层最多分裂一次**：节点吸收的内容只有三个来源——other 摊开的孩子、整棵 other、下层冒泡上来的一棵分裂树。前两者让 `child_count` 最多到 \( 4B \)，后者最多加 1；无论哪种，一次对半分裂后两半各 ≤ \( 2B \)，该节点立即回到合法状态，不会再次溢出。
2. **分裂严格逐层向上冒泡**：每层分裂返回的是**一棵**与该层同高的树，进入上一层的待追加列表；不存在一次返回多棵、或跨层冒泡的通道。
3. **只有根分裂会升层，且只升一层**：冒泡到根时，append 顶层用 `from_child_trees(根的左半, 根的右半)` 造一个 `高度 = 旧根 + 1` 的新根，然后流程终结——没有任何代码路径会再对新根做第二次包裹。

综上，设 append 前两树高度为 \( h_1, h_2 \)，append 后高度 \( H \) 满足：

\[ \max(h_1, h_2) \le H \le \max(h_1, h_2) + 1 \]

（append 只增不删，下界显然；上界即上述三条。）`append_large` 路径同理：它返回的树与大树同高，`from_child_trees` 高度为 `大树高度 + 1`。

#### 4.3.3 源码精读

**Internal 的溢出分裂**——[src/sum_tree.rs:L839-L873](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L839-L873)：这段代码计算合并后的孩子数，超过 `2 * TREE_BASE` 时以 midpoint 为界对半分裂——左半原地留在 self 并重算 summary，右半构造一个**同 height** 的 Internal 节点作为返回值；未溢出则直接 extend 待追加列表并返回 None。

```rust
let child_count = child_trees.len() + trees_to_append.len();
if child_count > 2 * TREE_BASE {
    // ……
    let midpoint = (child_count + child_count % 2) / 2;
    {
        let mut all_summaries = child_summaries
            .iter()
            .chain(summaries_to_append.iter())
            .cloned();
        left_summaries = all_summaries.by_ref().take(midpoint).collect();
        right_summaries = all_summaries.collect();
        let mut all_trees =
            child_trees.iter().chain(trees_to_append.iter()).cloned();
        left_trees = all_trees.by_ref().take(midpoint).collect();
        right_trees = all_trees.collect();
    }
    *summary = sum(left_summaries.iter(), cx);
    *child_summaries = left_summaries;
    *child_trees = left_trees;

    Some(SumTree(Arc::new(Node::Internal {
        height: *height,
        summary: sum(right_summaries.iter(), cx),
        child_summaries: right_summaries,
        child_trees: right_trees,
    })))
} else {
    child_summaries.extend(summaries_to_append);
    child_trees.extend(trees_to_append);
    None
}
```

值得注意的点：

- `(child_count + child_count % 2) / 2` 是 `div_ceil(2)` 的手写形式；`merge_into_right` 里同样的语义就直接写了 `div_ceil(2)`（[src/sum_tree.rs:L1028](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1028)），两处行为一致。
- 分裂后 `self.summary` 用 `sum(...)` **重算**而非增量维护，`sum` 是模块级折叠助手（[src/sum_tree.rs:L1381-L1391](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1381-L1391)），从 `zero` 出发按序 `add_summary`——再次体现 u2-l1 讲过的幺半群折叠方向。
- 孩子树通过 `cloned()` 移入两侧，每个都是一次 Arc 引用计数加一，没有子树深拷贝。

**Leaf 的溢出分裂**——[src/sum_tree.rs:L883-L909](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L883-L909)：与 Internal 完全同构，只是操作对象换成 items 与 item_summaries，分裂产物是一个新 Leaf。这段代码把左右两侧的元素与摘要按 midpoint 对半，左侧留在原叶、右侧打包成新叶返回。

**from_child_trees**——[src/sum_tree.rs:L1102-L1120](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1102-L1120)：这段代码用两棵同高的树造一个高一层、恰有两个孩子的 Internal 新根，并在构造时以 `sum` 折叠出根汇总。

```rust
fn from_child_trees(
    left: SumTree<T>,
    right: SumTree<T>,
    cx: <T::Summary as Summary>::Context<'_>,
) -> Self {
    let height = left.0.height() + 1;
    let mut child_summaries = ArrayVec::new();
    child_summaries.push(left.0.summary().clone()).unwrap_oob();
    child_summaries.push(right.0.summary().clone()).unwrap_oob();
    let mut child_trees = ArrayVec::new();
    child_trees.push(left).unwrap_oob();
    child_trees.push(right).unwrap_oob();
    SumTree(Arc::new(Node::Internal {
        height,
        summary: sum(child_summaries.iter(), cx),
        child_summaries,
        child_trees,
    }))
}
```

它是全 crate 唯一 `height` 取 `旧高度 + 1` 的地方，也只在 `append` 的两个调用点出现（[L786](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L786) 与 [L791](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L791)）。新根只有 2 个孩子（≥ 2 但 < TREE_BASE 时是欠溢的——**根允许欠溢**，这是 B+ 树的标准豁免）。`unwrap_oob` 在这里保护的是「往容量 2B ≥ 2 的 ArrayVec 里 push 2 个元素」，显然安全。

#### 4.3.4 代码实践

1. **实践目标**：亲眼看到一次「根分裂 → 树高 +1」的完整过程，验证 4.3.2 的证明。
2. **操作步骤**：
   - 构造 `a = SumTree::from_iter(0..16, ())`：4 个满叶子 → 1 个高 1 的根，4 个孩子（满）。
   - 推演 `a.append(from_iter(16..17, ()), ())`：根（h1）对单元素叶：delta = 1 但叶欠溢（1 < 2）→ 递归下推最右叶 `[12,13,14,15]`；叶对叶 delta = 0，`child_count = 5 > 4` → 分裂，midpoint = (5+1)/2 = 3：左 `[12,13,14]`、右 `[15,16]`；根收到冒泡的右叶，`child_count = 4 + 1 = 5 > 4` → 根分裂，midpoint = 3：左 = `[叶1,叶2,叶3]`，右 = `[叶4', 右叶]`；append 顶层 `from_child_trees` 造高 2 新根。
   - 用下面的测试验证（示例代码）：

   ```rust
   #[test]
   fn test_append_root_split_raises_height() {
       let mut tree = SumTree::from_iter(0..16, ());
       assert_eq!(tree.0.height(), 1);
       tree.append(SumTree::from_iter(16..17, ()), ());
       assert_eq!(tree.0.height(), 2);
       assert_eq!(tree.items(()), (0..17).collect::<Vec<u8>>());
       println!("{:#?}", tree);
   }
   ```

3. **需要观察的现象**：高度从 1 变为 2（恰好 +1）；新根恰好有 2 个孩子；`items(())` 保持 0..17 有序。
4. **预期结果**：断言全部通过；Debug 输出的新根 `child_trees` 长度为 2，第一个孩子是装着 3 个叶子的旧根左半，第二个孩子是装着 `[12,13,14]` 与 `[15,16]` 两个叶子的右半。
5. 可以再把 `0..16` 换成 `0..100`、追加 `100..101`，观察高度仍只 +1（大树场景下分裂可能发生在更深的层，但冒泡到根仍只升一层）。

#### 4.3.5 小练习与答案

**练习 1**：为什么分裂要把「右半」返回、把「左半」留在原节点，而不是反过来？

**答案**：待追加的内容总是逻辑上排在已有内容**之后**（append 语义），所以拼接序列 `child_trees ++ trees_to_append` 的左半必然以旧孩子为主、右半含新内容。让原节点保留左半（前缀）、新节点承载右半（后缀），新节点在父层中也应排在原节点之后——与它作为「待追加孩子」进入父层的位置一致，元素的有序性在每一层都得到保持。

**练习 2**：证明：等高摊开分支中 `child_count` 的上界是 \( 4B \)，因此 `right_summaries` 的 ArrayVec（容量 \( 2B \)）永远不会溢出。

**答案**：摊开分支发生时 self 与 other 同高。self 的孩子数 ≤ \( 2B \)（容量上限），other 的孩子数 ≤ \( 2B \)，所以 \( c \le 4B \)；分裂后右半为 \( \lfloor c/2 \rfloor \le 2B \)，恰好不超过容量。同理挂载/冒泡分支 \( c \le 2B + 1 \)，右半 ≤ \( B + ... \) 也 ≤ \( 2B \)。这就是两个 ArrayVec 声明容量为 `2 * TREE_BASE` 的依据（[src/sum_tree.rs:L813-L814](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L813-L814)）。

**练习 3**：`from_iter`（u2-l3）也可能产生高度 h 的树，但它从不调用 `from_child_trees`。它是怎么长高的？两条路径的代价差异是什么？

**答案**：`from_iter` 自底向上：先把元素装成叶子，再在 `while nodes.len() > 1` 循环里每轮把最多 \( 2B \) 个同高节点装进一个高一层的新父节点（[src/sum_tree.rs:L274-L308](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L274-L308)）。它是批量、整层的组装，\( O(n) \) 完成且不需要写时复制；而 append 的长高是增量、单点的（一次根分裂 + 一个两孩子新根），并伴随路径复制。两者正好是「批量构建」与「增量修改」的镜像。

### 4.4 Node::is_underflowing：欠溢判定与挂载/摊开的选择

#### 4.4.1 概念说明

`is_underflowing` 只有两行，却是 `push_tree_recursive` 中 `height_delta == 1` 分支的守门人：

\[ \text{underflowing}(v) \iff \text{children}(v) < \text{TREE\_BASE} \]

它在 append 中的角色不是「修复」欠溢，而是**避免制造新的欠溢**：当 other 恰好矮一层、可以直接挂载时，先检查它是否太瘦——太瘦就放弃廉价的整树挂载，改走递归摊开，把它的内容并入已有的最右子树。这是一个「多复制一点 vs 树更致密」的取舍。

需要澄清一个容易误解的点：sum_tree 并**不**严格维护「所有非根节点 ≥ TREE_BASE」的不变量。`from_iter` 的尾部节点（如 4.2.4 实践中只有 1 个孩子的 `父2`）、以及欠溢节点出现在中间层都是被允许的。欠溢的上限影响的是**最坏**复杂度（欠溢节点越多树越瘦高，u1-l2 的容量-高度关系 \( n \le C^{h+1} \) 中 C 变小），所以代码在「顺手能避免」时避免它，`append_large`/`merge_into_right`（下一讲）则会在小树贴大树时主动修复它。

#### 4.4.2 核心流程

```text
is_underflowing(node):
  Internal → child_trees.len() < TREE_BASE
  Leaf    → items.len()        < TREE_BASE

在 push_tree_recursive 的 delta == 1 分支中：
  other 不欠溢 → 挂载（1 次 Arc 克隆，other 子树整体共享）
  other 欠溢   → 递归下推，在子层走 delta == 0 的摊开合并
```

#### 4.4.3 源码精读

**判定函数**——[src/sum_tree.rs:L1358-L1363](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1358-L1363)：这段代码按节点类型分别用孩子数/元素数与 TREE_BASE 比较，低于下限即为欠溢。

```rust
fn is_underflowing(&self) -> bool {
    match self {
        Node::Internal { child_trees, .. } => child_trees.len() < TREE_BASE,
        Node::Leaf { items, .. } => items.len() < TREE_BASE,
    }
}
```

**唯一的消费点**——[src/sum_tree.rs:L818-L822](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L818-L822)：这段代码在高度差恰为 1 时用 `is_underflowing` 决定「整树挂载」还是落入后面的递归摊开分支，挂载时把 other 的 summary 与整棵树各 push 一份。

```rust
} else if height_delta == 1 && !other_node.is_underflowing() {
    summaries_to_append
        .push(other_node.summary().clone())
        .unwrap_oob();
    trees_to_append.push(other).unwrap_oob();
}
```

（`append_large` 的入口 [src/sum_tree.rs:L926-L931](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L926-L931) 也用同一个判定决定「直接返回小树」还是 `merge_into_right`，那是下一讲的内容，此处仅指出两个消费点共享同一语义。）

对照两个具体例子（TREE_BASE = 2）：

| 场景 | other | `is_underflowing` | 走向 |
| --- | --- | --- | --- |
| 4.2.4 的递归下推 | 单元素叶 `[17]`（1 < 2） | true | 递归摊进最右子树 |
| 挂载 | 双元素叶 `[9,10]`（2 ≮ 2） | false | 整树挂为最右孩子 |

#### 4.4.4 代码实践

1. **实践目标**：对比「挂载」与「摊开」两种路径产出的树形差异，直观感受欠溢判定的作用。
2. **操作步骤**：在 `mod tests` 中加入（示例代码）：

   ```rust
   #[test]
   fn test_append_mount_vs_flatten() {
       // 场景一：other 不欠溢（2 个元素）→ 整树挂载，根孩子数 3+1=4，不分裂
       let mut mounted = SumTree::from_iter(0..9, ());   // 3 叶 → 根 h1
       mounted.append(SumTree::from_iter(9..11, ()));    // 双元素叶，不欠溢
       assert_eq!(mounted.0.height(), 1);
       assert_eq!(mounted.0.child_trees().len(), 4);
       assert_eq!(mounted.items(()), (0..11).collect::<Vec<u8>>());

       // 场景二：other 欠溢（1 个元素）→ 摊进最右叶，根孩子数不变
       let mut flattened = SumTree::from_iter(0..9, ());
       flattened.append(SumTree::from_iter(9..10, ())); // 单元素叶，欠溢
       assert_eq!(flattened.0.height(), 1);
       assert_eq!(flattened.0.child_trees().len(), 3);   // 没有新增孩子
       assert_eq!(flattened.items(()), (0..10).collect::<Vec<u8>>());
   }
   ```

3. **需要观察的现象**：场景一根的孩子从 3 变 4（新挂了一个完整叶子 `[9,10]`，可用 `mounted.0.child_trees()[3].items()` 打印确认内容为 `[9, 10]`）；场景二根的孩子数保持 3，最右叶从 `[8]` 变成 `[8, 9]`（`flattened.0.child_trees()[2].items()`）。
4. **预期结果**：全部断言通过；两个场景的树形差异印证欠溢判定对路径的选择作用。
5. 本实践依赖「crate 内部测试才有 `tree.0` 访问权与 TREE_BASE = 2」这一前提（u1-l3）；若放到 `tests/` 集成测试目录则 TREE_BASE = 6，上述元素个数推演全部失效。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `from_child_trees` 造出的新根只有 2 个孩子，代码却不担心它欠溢？

**答案**：B+ 树对根节点豁免下限——根可以只有 1 到 2 个孩子（甚至 `push` 单元素时根就是一个 1 元素叶子）。`is_underflowing` 的判定本身对根同样返回 true，但它只在「挂载 vs 摊开」的选择点被消费，从未对根做强制再平衡。后续的 append 会随着内容增多逐渐把根填满。

**练习 2**：一个空树 `SumTree::default()` 的 `is_underflowing()` 是 true 还是 false？这会影响 append 的行为吗？

**答案**：`default()` 产生一个 0 元素的空叶子（[src/sum_tree.rs:L1262-L1266](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1262-L1266) 委托给 `new`），`0 < TREE_BASE`，判定为 true。但它不会走到 `height_delta == 1` 的判定——append 顶层早已用 `other 为空叶子` 的捷径把空树挡在门外（4.1），所以这个 true 从不参与决策。

**练习 3**：如果把 `height_delta == 1` 分支的欠溢检查去掉（一律整树挂载），哪些性质会被破坏？哪些仍然保持？

**答案**：仍然保持的是：元素有序性、每节点 ≤ 2×TREE_BASE 的上限（分裂逻辑不受影响）、查询正确性（summary 依旧准确）。被破坏的是树的致密性：单元素叶子这类瘦节点会不断被挂载进树，最坏情况下树退化出大量 1 孩子内部节点，高度增长远快于 \( \log_{2B} n \)，最坏查询/寻路成本从对数退化为线性量级。这正是该检查「多一次下钻、换树更胖」的价值。

## 5. 综合实践

把本讲的四个模块串成一个完整实验：按规格分别 append 高度组合为 (0, 0)、(1, 0)、(2, 0) 的树，用 Debug 打印验证高度与有序性，并解释树高上界。

在 `src/sum_tree.rs` 的 `mod tests` 中加入（示例代码，所有元素个数均按 TREE_BASE = 2 推演）：

```rust
#[test]
fn test_append_height_matrix() {
    // (0, 0)：两个不满的叶子合并，仍是一棵 height 0 的单叶树
    let mut h0_h0 = SumTree::from_iter(0..2, ());
    h0_h0.append(SumTree::from_iter(2..4, ()), ());
    assert_eq!(h0_h0.0.height(), 0);
    assert_eq!(h0_h0.items(()), vec![0, 1, 2, 3]);

    // (0, 0) 但总量超容：叶子对半分裂 → from_child_trees 升一层
    let mut h0_h0_split = SumTree::from_iter(0..3, ());
    h0_h0_split.append(SumTree::from_iter(3..7, ()), ()); // 3+4=7 > 4
    assert_eq!(h0_h0_split.0.height(), 1);
    assert_eq!(h0_h0_split.items(()), (0..7).collect::<Vec<u8>>());

    // (1, 0)：delta = 1 且不欠溢 → 整树挂载（见 4.4.4 场景一）
    let mut h1_h0 = SumTree::from_iter(0..9, ());
    h1_h0.append(SumTree::from_iter(9..11, ()), ());
    assert_eq!(h1_h0.0.height(), 1);
    assert_eq!(h1_h0.items(()), (0..11).collect::<Vec<u8>>());

    // (2, 0)：delta = 2 → 递归下推 + 最深处摊开合并（见 4.2.4）
    let mut h2_h0 = SumTree::from_iter(0..17, ());
    h2_h0.append(SumTree::from_iter(17..18, ()), ());
    assert_eq!(h2_h0.0.height(), 2);
    assert_eq!(h2_h0.items(()), (0..18).collect::<Vec<u8>>());

    // 根分裂场景：高度恰好 +1（见 4.3.4）
    let mut root_split = SumTree::from_iter(0..16, ());
    root_split.append(SumTree::from_iter(16..17, ()), ());
    assert_eq!(root_split.0.height(), 2); // 1 → 2
    assert_eq!(root_split.items(()), (0..17).collect::<Vec<u8>>());

    println!("h0_h0:\n{:#?}", h0_h0);
    println!("h0_h0_split:\n{:#?}", h0_h0_split);
    println!("h1_h0:\n{:#?}", h1_h0);
    println!("h2_h0:\n{:#?}", h2_h0);
    println!("root_split:\n{:#?}", root_split);
}
```

运行方式（在 zed 仓库根目录）：

```bash
cargo test -p sum_tree test_append_height_matrix -- --nocapture
```

**观察与解释要点**：

1. 五个场景覆盖了 `push_tree_recursive` 的全部路径：叶子合并、叶子分裂升层、整树挂载、递归下推摊开、根分裂升层。
2. 「为什么一次 append 后树高最多增加 1」——对应 4.3.2 的三步证明：每层最多分裂一次且分裂产物与该层同高、分裂只逐层冒泡一棵、唯一升层点是 `from_child_trees` 且只执行一次。`root_split` 场景演示了最坏情形：叶分裂冒泡成根分裂，高度也只从 1 涨到 2。
3. Debug 输出中每一层 `summary` 字段的 `count` 都等于该子树元素数（`IntegersSummary` 的字段见 [src/sum_tree.rs:L1820-L1826](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1820-L1826)），可以随手核对入口处「先记账」与分裂后「重算」两处逻辑。
4. 有序性断言 `items(())` 是最终防线：无论内部走了哪条路径，元素序列必须与直接拼接的结果一致——这与 `test_extend_and_push_tree`（[src/sum_tree.rs:L1404-L1414](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1404-L1414)）的断言风格一致。

预期结果：全部断言通过；各 Debug 输出的树形与本讲 4.2.4 / 4.3.4 / 4.4.4 中的推演逐一对应（具体打印排版以本地运行为准）。

## 6. 本讲小结

- `append` 是 sum_tree 唯一的增量修改原语：顶层只做四件事——空树接管、空 other 直接返回、self 更矮交 `append_large`、否则 `push_tree_recursive`，分裂时统一用 `from_child_trees` 升层。
- `push_tree_recursive` 按高度差三路分派：delta 0 摊开上提（丢弃 other 节点壳、孩子整体升一层）、delta 1 且不欠溢整树挂载（一次 Arc 克隆换整棵子树共享）、其余递归下推并回写最右孩子的 summary。
- 溢出条件是 `child_count > 2 * TREE_BASE`，midpoint 取 \( \lceil c/2 \rceil \) 对半分裂：左半留原节点、右半打包成**同高**树向上返回；由于 \( c \le 4B \)，两半各落在 \( [B, 2B] \) 内，一次分裂即恢复合法。
- 一次 append 后树高满足 \( \max(h_1,h_2) \le H \le \max(h_1,h_2)+1 \)：分裂逐层冒泡且每层最多一棵，唯一升层点 `from_child_trees` 只执行一次。
- `is_underflowing`（孩子/元素数 < TREE_BASE）是「挂载 vs 摊开」的守门人，目标是避免制造新的瘦节点而非强制再平衡——sum_tree 允许欠溢节点存在，欠溢只影响最坏复杂度。
- `Arc::make_mut` 让修改只复制「根到叶一条路径」上的节点壳（\( O(\log n) \) 个，子树 Arc 依旧共享），这是克隆快照后仍能低成本增量修改的根基。

## 7. 下一步学习建议

本讲只覆盖了「self 不矮于 other」的半边天下。下一讲 **u4-l2《append_large 与 merge_into_right：欠溢处理》** 精读 [src/sum_tree.rs:L921-L1100](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L921-L1100)：小树贴到大树前面时如何沿最左脊柱下钻（`append_large`，含「前插分裂节点」的左倾分裂）、同高节点如何真正**合并**（`merge_into_right` 返回 Some 承载左半的约定），以及它们如何顺手修复欠溢节点。学完后建议重读 `test_random`（u1-l3）中的随机 splice 对拍，把其中的 append 调用在脑中映射到本讲与下一讲的分支上；之后再进入 u4-l3 的 `KeyedItem`/`edit`，看批量编辑如何用 slice + append 组合出单趟线性合并。

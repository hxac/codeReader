# 树的骨架:Node、TREE_BASE 与 Arc 结构共享

## 1. 本讲目标

学完本讲,你应该能够:

1. 逐字段说出 `Node` 枚举两个变体的含义:`Internal` 的 `height`/`summary`/`child_summaries`/`child_trees`,以及 `Leaf` 的 `summary`/`items`/`item_summaries`,并解释"子树汇总为什么要存两份"这种看似冗余的设计。
2. 解释 `TREE_BASE` 如何同时决定节点的容量上限(`2 * TREE_BASE`)与下限(`TREE_BASE`,欠溢线),并能用公式 \( (2 \cdot \text{TREE\_BASE})^{h+1} \) 推算"n 个元素的树根高度是多少"。
3. 理解测试构建 `TREE_BASE = 2`、正式构建 `TREE_BASE = 6` 的编译期切换,以及一个容易被忽视的细节:**下游 crate 跑测试时,sum_tree 是按 6 编译的**。
4. 读懂 `CapacityResultExt::unwrap_oob` 这个 10 行小工具存在的理由(定容数组的越界断言 + `T` 不实现 `Debug` 的约束),并知道它在哪些位置被调用。
5. 理解 `SumTree(Arc<Node<T>>)`:克隆只是引用计数加一,修改走 `Arc::make_mut` 的写时复制,并能用手写测试亲眼验证这两件事。

## 2. 前置知识

上一讲(u1-l1)已经建立了"sum_tree = 带汇总的并发友好 B+ 树"的整体图景,并让你第一次用 Debug 打印看到了一棵真实的小树。本讲在那之上深入骨架层,需要用到以下概念:

- **枚举即带标签的联合体**:`enum Node { Internal { .. }, Leaf { .. }` 在内存里是"一个标签 + 对应变体的字段",两个变体共用同一块空间。Rust 的 `match` 会强制你穷尽处理所有变体,这是比 trait 对象(虚表分发)更廉价、更利于编译器优化的静态分发。
- **const 泛型**:`ArrayVec<T, { 2 * TREE_BASE }>` 里那个花括号常量是**类型参数**——容量在编译期就写进了类型,数组内联在结构体里,不再有独立的堆分配。
- **`Arc` 与引用计数**:`Arc<T>` 是线程安全的引用计数智能指针。`clone()` 只做一次原子计数加一,所有克隆共享同一份 `T`;当计数归零,`T` 才被释放。
- **写时复制(Copy-on-Write)**:数据结构对外表现为"不可变",修改时先检查引用计数——只有自己持有(计数为 1)就原地改;有别人共享就先复制一份再改。`Arc::make_mut` 就是这个语义的现成实现。
- **B+ 树的容量不变量**:每个节点的子节点数有上限(防节点过大)与下限(防树退化成链表)。sum_tree 里上限是 `2 * TREE_BASE`,下限是 `TREE_BASE`,低于下限称为"欠溢"(underflow)。
- **`#[cfg(...)]` 条件编译**:由 Cargo 在**编译每个 crate 时**根据当前正在构建的目标决定,是编译期开关,不是运行时开关。

## 3. 本讲源码地图

本讲几乎全部内容集中在一个文件里:

| 文件 | 相关行 | 作用 |
| --- | --- | --- |
| [crates/sum_tree/src/sum_tree.rs](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1-L1903) | L7、L15-L29、L206-L316、L642-L676、L768-L860、L1102-L1138、L1258-L1363 | 本讲主战场:`ArrayVec` 别名、`TREE_BASE`、`CapacityResultExt`、`SumTree`/`Node` 定义、Debug 实现、`Arc::make_mut`/`Arc::get_mut` 的全部用例 |
| [crates/sum_tree/src/cursor.rs](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L1-L861) | L7-L11、L29-L37、L322-L331 | 仅作证据引用:`StackEntry` 与 `Cursor` 的栈结构,说明 `child_summaries` 为什么值得单独存一份 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块:

1. **Node**——`Internal`/`Leaf` 的字段级布局与"汇总存两份"的用意。
2. **TREE_BASE**——一个常量如何决定树的形状,以及高度的数学。
3. **CapacityResultExt**——为定容数组断言"这里绝不会越界"的小工具。
4. **SumTree 的 `Arc<Node>` 包装**——克隆即快照,修改走写时复制。

### 4.1 Node:Internal 与 Leaf 的字段级布局

#### 4.1.1 概念说明

`Node` 是整棵树唯一的"积木":一棵树要么是一个 `Leaf`(直接装元素),要么是一个 `Internal`(装若干棵子树)。上一讲我们已经在 Debug 输出里见过它们,这一讲把每个字段讲透。

先看定义:

[crates/sum_tree/src/sum_tree.rs:L1268-L1281](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1268-L1281)

```rust
#[derive(Clone)]
pub enum Node<T: Item> {
    Internal {
        height: u8,
        summary: T::Summary,
        child_summaries: ArrayVec<T::Summary, { 2 * TREE_BASE }, u8>,
        child_trees: ArrayVec<SumTree<T>, { 2 * TREE_BASE }, u8>,
    },
    Leaf {
        summary: T::Summary,
        items: ArrayVec<T, { 2 * TREE_BASE }, u8>,
        item_summaries: ArrayVec<T::Summary, { 2 * TREE_BASE }, u8>,
    },
}
```

逐字段拆解:

**Internal(内部节点,只做路由,不存元素):**

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `height` | `u8` | 该节点到叶子的距离。叶子高度记为 0,叶子的父节点是 1,依此类推。**只有 Internal 有这个字段**,叶子的 0 是约定(见下面的 `height()` 方法)。它让 `append` 能先比较两棵树的高低再决定怎么拼(第 4 单元的主角),也让游标知道还剩几层可下 |
| `summary` | `T::Summary` | 整棵子树的汇总缓存。根节点的这个字段就是 `extent()` 的 \( O(1) \) 来源 |
| `child_summaries` | `ArrayVec<T::Summary, 2*TREE_BASE>` | **每个子树的汇总,平铺成一个连续数组**——这是 B+ 树的"路由表" |
| `child_trees` | `ArrayVec<SumTree<T>, 2*TREE_BASE>` | 子树本体。每个 `SumTree` 就是一个 `Arc<Node>`,所以这个数组实际是一排指针 |

**Leaf(叶子节点,元素唯一居所):**

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `summary` | `T::Summary` | 整叶的汇总缓存 |
| `items` | `ArrayVec<T, 2*TREE_BASE>` | 元素本体。B+ 树的特征:**只有叶子存元素**,内部节点一个元素都不碰 |
| `item_summaries` | `ArrayVec<T::Summary, 2*TREE_BASE>` | 与 `items` 平行的逐元素汇总数组 |

两个"看似冗余"的设计值得专门想清楚,它们是这个 crate 最有味道的取舍:

**冗余之一:`child_summaries[i]` 与 `child_trees[i].0.summary()` 内容相同。**
为什么不省掉前者、要用时再去子节点里读?看一眼游标下钻的代码就明白了:

[crates/sum_tree/src/cursor.rs:L322-L331](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L322-L331)

```rust
child_summaries,
...
while entry.index() < child_summaries.len() {
    let next_summary = &child_summaries[entry.index()];
    ...
```

游标在某一层挑选"该往哪个孩子走"时,只需要**连续扫描 `child_summaries` 这一个数组**——不必顺着 `child_trees` 里的指针一个个解引用。汇总数组是纯值类型(比如测试里的 `IntegersSummary` 只有十几个字节),平铺后对 CPU 缓存极其友好;而子树本体挂着整棵子树的内存,跳跃访问代价高。代价则是:**每次修改子树,都必须同步维护这份汇总数组**,例如 [sum_tree.rs:L828-L829](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L828-L829) 在递归修改最后一个孩子后,立刻把新的汇总写回 `child_summaries`。

**冗余之二:叶子里 `item_summaries[i]` 与重新调用 `items[i].summary(cx)` 结果相同。**
存下来的好处有二:一是游标的 `start()`/`end()` 定位在"元素与元素之间",需要逐元素的度量做累加;二是修改单个元素后重算整叶汇总时,不必再对每个元素调用一次可能昂贵的 `summary(cx)`——[sum_tree.rs:L669](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L669) 里 `update_last_recursive` 就是直接 `sum(item_summaries.iter(), cx)` 汇总,只重算被改的那一个。

还要注意 `Node` 上挂着一组便于统一访问的私有方法,让调用方不必关心自己拿到的是哪种节点:

[crates/sum_tree/src/sum_tree.rs:L1316-L1333](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1316-L1333)

```rust
impl<T: Item> Node<T> {
    fn is_leaf(&self) -> bool { matches!(self, Node::Leaf { .. }) }

    fn height(&self) -> u8 {
        match self {
            Node::Internal { height, .. } => *height,
            Node::Leaf { .. } => 0,
        }
    }

    fn summary(&self) -> &T::Summary { ... }
```

这里能直接看到"叶子高度为 0"是**方法的约定**而非字段:叶子的 `height()` 恒返回 0,Internal 的 `height` 字段从 1 起步。另外 `child_summaries()` 方法([L1335-L1342](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1335-L1342))对 Internal 返回子树汇总、对 Leaf 返回逐元素汇总——游标因此能用同一段代码在两种节点上做"扫描汇总数组"这件事,这是 B+ 树"叶子层与内部层同构"带来的便利。

#### 4.1.2 核心流程

把一棵树画出来,字段与位置的对应关系是:

```text
Internal { height: 2, summary: S(全部25个元素),          ← 根
    child_summaries: [ S₁₆, S₉ ],                        ← 路由表(平铺,纯值)
    child_trees:     [ ↗子树A,     ↗子树B ] }            ← 指针数组
        │                    │
Internal { height: 1, summary: S₁₆ }          Internal { height: 1, summary: S₉ }
    child_summaries: [S₄,S₄,S₄,S₄]               child_summaries: [S₄,S₄,S₄,S₁]
    child_trees:     [↗,↗,↗,↗]                   child_trees:     [↗,↗,↗,↗]
        ││││  (4 片叶子,各 4 个元素)                  ││││  (3 片满叶 + 1 片 1 元素)
Leaf { summary: S₄, items: [e₁,e₂,e₃,e₄], item_summaries: [s₁,s₂,s₃,s₄] }
```

对一个节点做任何事,都遵循同一个心智模型:

1. **问路由**:要在这一层定位目标,只扫 `child_summaries`(或叶子的 `item_summaries`)这个连续数组;
2. **下钻或落地**:内部层选中下标后从 `child_trees` 取指针往下走;叶子层选中下标后直接访问 `items`;
3. **改动回写**:任何修改完成后,沿原路向上把每个节点的 `summary` 与 `child_summaries` 里对应的一项重新算好。

#### 4.1.3 源码精读

**① Debug 实现:本讲实践的观察工具**

[crates/sum_tree/src/sum_tree.rs:L215-L223](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L215-L223)

```rust
impl<T> fmt::Debug for SumTree<T>
where
    T: fmt::Debug + Item,
    T::Summary: fmt::Debug,
{
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        f.debug_tuple("SumTree").field(&self.0).finish()
    }
}
```

`SumTree` 的 Debug 直接委托给内部的 `Node`;而 `Node` 的 Debug([L1283-L1314](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1283-L1314))会把 `child_trees` 整个递归打印。所以 `println!("{:#?}", tree)` 能一次性吐出从根到每片叶子的完整结构——这就是我们实践里的"透视镜"。

**② 空树也是一片叶子**

[crates/sum_tree/src/sum_tree.rs:L226-L232](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L226-L232)

```rust
pub fn new(cx: <T::Summary as Summary>::Context<'_>) -> Self {
    SumTree(Arc::new(Node::Leaf {
        summary: <T::Summary as Summary>::zero(cx),
        items: ArrayVec::new(),
        item_summaries: ArrayVec::new(),
    }))
}
```

注意空树不是 `Option<Node>`,而是一片"零元素、零汇总"的叶子。整个 crate 因此从不需要处理"没有根"的情况——所有代码路径都保证拿到的是合法 `Node`。`is_empty()`([L743-L748](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L743-L748))的判定也与此呼应:Internal 一定非空(否则它就不该存在),Leaf 看元素是否为空。

**③ 叶子的分组规则:每 4 个一组,最后一组允许不满**

[crates/sum_tree/src/sum_tree.rs:L255-L272](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L255-L272)

```rust
let mut iter = iter.into_iter().fuse().peekable();
while iter.peek().is_some() {
    let items: ArrayVec<T, { 2 * TREE_BASE }, u8> =
        iter.by_ref().take(2 * TREE_BASE).collect();
    let item_summaries: ArrayVec<T::Summary, { 2 * TREE_BASE }, u8> =
        items.iter().map(|item| item.summary(cx)).collect();
    ...
    nodes.push(SumTree(Arc::new(Node::Leaf { summary, items, item_summaries })));
}
```

`take(2 * TREE_BASE)` 保证了 `collect` 进定容数组**最多恰好装满**——这是 `collect` 不会因容量不足而失败的原因,也是"每个叶子最多 `2 * TREE_BASE` 个元素"这一不变量在构建期的第一次兑现。

**④ 欠溢线:`TREE_BASE` 作为下限**

[crates/sum_tree/src/sum_tree.rs:L1358-L1363](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1358-L1363)

```rust
fn is_underflowing(&self) -> bool {
    match self {
        Node::Internal { child_trees, .. } => child_trees.len() < TREE_BASE,
        Node::Leaf { items, .. } => items.len() < TREE_BASE,
    }
}
```

容量问题不只是上限:子节点数低于 `TREE_BASE` 的节点"欠溢",意味着树在往链表方向退化。`from_iter` 建出的"最后一组"(比如 25 个元素时的 1 元素叶子)可以欠溢,而 `append` 系列操作会借助它决定"是直接挂上去,还是先和邻居合并"——完整机制留给 u4-l2,这里先记住判定函数本身。

#### 4.1.4 代码实践

**实践:构建 25 个元素的树,数叶子、验证容量、解释根高度(本讲主实践)**

1. **实践目标**:亲手验证三条骨架不变量——叶子数与分组、单叶容量上限 `2 * TREE_BASE`、根节点高度的来历。

2. **操作步骤**:

   1. 打开 `crates/sum_tree/src/sum_tree.rs`,滚到文件末尾的 `mod tests`,在 `test_extend_and_push_tree` 旁边添加以下测试(这是**示例代码**,学习用,验证后请删除,不要提交):

      ```rust
      // 示例代码:学习用,验证后请删除
      fn leaf_sizes<T: Item>(tree: &SumTree<T>) -> Vec<usize> {
          match tree.0.as_ref() {
              Node::Leaf { items, .. } => vec![items.len()],
              Node::Internal { child_trees, .. } => {
                  child_trees.iter().flat_map(leaf_sizes).collect()
              }
          }
      }

      #[test]
      fn test_node_layout_25_items() {
          let mut tree = SumTree::<u8>::default();
          tree.extend(0..25, ());

          let sizes = leaf_sizes(&tree);
          println!("leaf sizes: {:?}", sizes);
          println!("root height: {}", tree.0.height());
          println!("extent::<Count>: {:?}", tree.extent::<Count>(()).0);
          println!("{:#?}", tree);

          assert_eq!(sizes.iter().sum::<usize>(), 25);      // 元素一个不少
          assert!(sizes.iter().all(|&n| n <= 2 * TREE_BASE)); // 容量不变量
      }
      ```

      说明:`tests` 是根模块的子模块,所以能访问 `SumTree` 的私有字段 `.0`、`Node` 的私有方法 `height()`——这也是为什么这个实践必须写在 crate 自己的测试模块里。

   2. 在仓库根目录运行:

      ```bash
      cargo test -p sum_tree test_node_layout_25_items -- --nocapture
      ```

3. **需要观察的现象**:

   - `leaf sizes` 应为 `[4, 4, 4, 4, 4, 4, 1]`:25 = 6×4 + 1,即 6 片满叶加 1 片只有 1 个元素的尾叶,共 **7 片叶子**;
   - 每个数字都 ≤ `2 * TREE_BASE = 4`(测试构建),断言通过;
   - `root height` 应为 **2**;`extent::<Count>` 应为 25,且与 Debug 输出中根节点 `summary` 里的 `count: 25` 一致;
   - `{:#?}` 的完整输出里能看到:根 `Internal { height: 2, .. }` 有 2 个 `Internal { height: 1 }` 孩子,前者带 4 片叶子、后者带 3 片(合计 7)。

4. **预期结果**:测试通过,以上数值全部吻合。**根高度为 2 的解释**:7 片叶子先组装成 2 个高度为 1 的内部节点(4 + 3),2 > 1 于是再向上组装出高度为 2 的根。用 4.2 节的公式验证:高度 1 的树最多容纳 \( 4^2 = 16 < 25 \le 64 = 4^3 \),所以 25 个元素必然落在高度 2。具体打印排版以本地输出为准,格式细节**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**:`child_summaries` 和 `child_trees` 里对应元素的信息重复,为什么不把它删掉、用的时候从子树读?

**参考答案**:它是 B+ 树的路由表。游标下钻时只需连续扫描这个纯值数组([cursor.rs:L330-L331](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L330-L331)),不必解引用任何子树指针,缓存友好;代价是修改子树后必须同步回写(如 [sum_tree.rs:L828-L829](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L828-L829))。这是典型的"用维护成本换读取性能"。

**练习 2**:为什么 `height` 只存在 `Internal` 变体里,`Leaf` 没有?

**参考答案**:高度本来就是"到叶子的距离",叶子自身恒为 0,无需存储;`Node::height()`([L1321-L1326](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1321-L1326))把这个约定封装成统一方法。省下叶子里的一个字段,对"数量最多"的叶子层是最优的。

**练习 3**:25 个元素的树里,那片只有 1 个元素的尾叶满足 `is_underflowing()` 吗?这算不算 bug?

**参考答案**:满足(`1 < TREE_BASE = 2`)。不算 bug:`from_iter` 允许最后一组不满,欠溢节点在 B+ 树里是合法的临时状态;`is_underflowing` 的用途是让 `append` 在拼接时决定是否合并/下钻(u4-l2 详述),而不是构造期的硬约束。

### 4.2 TREE_BASE:一个常量如何决定树的形状

#### 4.2.1 概念说明

[crates/sum_tree/src/sum_tree.rs:L15-L18](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L15-L18)

```rust
#[cfg(test)]
pub const TREE_BASE: usize = 2;
#[cfg(not(test))]
pub const TREE_BASE: usize = 6;
```

三行代码,两副面孔:

| 构建 | `TREE_BASE` | 节点容量上限 `2 * TREE_BASE` | 欠溢线 `TREE_BASE` |
| --- | --- | --- | --- |
| 测试构建(本 crate 自己跑 `cargo test`) | 2 | 4 | 2 |
| 正式构建(及下游 crate 的一切构建) | 6 | 12 | 6 |

设计动机是**可测试性**:树的所有有趣行为——分裂、多层、欠溢合并——都只在"元素数超过节点容量"后出现。容量为 12 时要 17 个以上元素才见高度 2,要 145 个以上才见高度 3;容量为 4 时 5 个元素就见高度 2。测试里动辄操作几十上百个元素,小容量让随机测试用极小的数据量就能覆盖全部结构路径,也让 Debug 输出短到能人工检查。

一个容易踩的认知坑:`#[cfg(test)]` 是**按 crate 生效**的编译期开关。当 rope 或 multi_buffer 在自己的测试里使用 `SumTree` 时,sum_tree 作为依赖是以 `cfg(not(test))` 编译的,`TREE_BASE = 6`。换句话说,"测试下容量为 4"只发生在你 `cargo test -p sum_tree` 时。本讲义第 3、4 单元的所有行数推演都默认你在这个模式下运行。

#### 4.2.2 核心流程(树的形状数学)

设容量 \( C = 2 \cdot \text{TREE\_BASE} \)。因为每一层每个节点至多 \( C \) 个孩子、只有叶子装元素,所以:

\[ \text{根高度为 } h \text{ 的树,元素数至多 } C^{h+1} \]

反过来说,`from_iter` 构建时 n 个元素的树,根高度是满足 \( n \le C^{h+1} \) 的最小 h。测试构建 \( C = 4 \) 的换算表:

| 元素数 n | 根高度 | 边界来源 |
| --- | --- | --- |
| 0 ~ 4 | 0 | \( n \le 4^1 \),单叶装得下 |
| 5 ~ 16 | 1 | \( 4^1 < n \le 4^2 \) |
| 17 ~ 64 | 2 | \( 4^2 < n \le 4^3 \) |
| 65 ~ 256 | 3 | \( 4^3 < n \le 4^4 \) |

正式构建 \( C = 12 \):高度 1 容纳至多 144,高度 2 至多 1728,高度 3 至多 20736。这就是 B+ 树"矮胖"的量化表达——百万级元素也只要高度 5(\( 12^6 \approx 3 \times 10^6 \))。这也解释了 [cursor.rs:L32](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L32) 里游标路径栈容量取 16 就绰绰有余:它能表达的高度上限是 15,对应天文数字的容量上限。

构建期的分组算法(承接 4.1 的流程)可以精确写出:

```text
n 个元素
  叶子数   L = ⌈n / C⌉          (每叶装满 C 个,最后一叶装剩余)
  高度 1 节点数 = ⌈L / C⌉
  高度 2 节点数 = ⌈⌈L / C⌉ / C⌉
  ...直到某一层只剩 1 个节点,它就是根
```

以 n = 25、C = 4 验证:L = 7 → 高度 1 节点 2 个 → 高度 2 节点 1 个 → 根高度 2,与 4.1 实践观察一致。

#### 4.2.3 源码精读

**① 上限在构建期被 `take` 保证**

[crates/sum_tree/src/sum_tree.rs:L257-L260](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L257-L260)(叶子侧,已在 4.1.3 ③ 引用)与内部节点侧:

[crates/sum_tree/src/sum_tree.rs:L299-L304](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L299-L304)

```rust
child_summaries.push(child_summary.clone()).unwrap_oob();
child_trees.push(child_node.clone()).unwrap_oob();

if child_trees.len() == 2 * TREE_BASE {
    parent_nodes.extend(current_parent_node.take());
}
```

`from_iter` 每装满 `2 * TREE_BASE` 个孩子就把当前父节点封箱,开一个新的——父节点永远不会超容。

**② 上限在修改期由分裂保证(预览)**

[crates/sum_tree/src/sum_tree.rs:L839-L846](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L839-L846)

```rust
let child_count = child_trees.len() + trees_to_append.len();
if child_count > 2 * TREE_BASE {
    ...
    let midpoint = (child_count + child_count % 2) / 2;
```

`append` 路径上,孩子数一旦**超过** `2 * TREE_BASE` 就从中点对半分裂、让树长高一层。本讲只需要认识到"`TREE_BASE` 定义了分裂的触发线";完整走读在 u4-l1。

**③ `TREE_BASE` 是 `pub const`**

注意它是公开常量。下游(以及我们讲义里的测试)可以直接写 `2 * TREE_BASE` 来表达"容量上限"这类断言,而不必硬编码 4 或 12——4.1.4 的实践正是这么做的,这让断言在两种构建下都自动正确。

#### 4.2.4 代码实践

**实践:用公式预测根高度,再用测试验证**

1. **实践目标**:把 4.2.2 的换算表从"纸面公式"变成"亲眼所见"。

2. **操作步骤**:

   1. 先笔算填表(留个空列待验证):

      | n | 预测根高度 | 预测叶子分组 |
      | --- | --- | --- |
      | 5 | ? | ? |
      | 16 | ? | ? |
      | 17 | ? | ? |
      | 64 | ? | ? |
      | 65 | ? | ? |

   2. 在 `mod tests` 里加一个参数化的小测试(**示例代码**,验证后删除):

      ```rust
      // 示例代码:学习用,验证后请删除
      #[test]
      fn test_root_height_boundaries() {
          for (n, expected_height) in [(5, 1), (16, 1), (17, 2), (64, 2), (65, 3)] {
              let mut tree = SumTree::<u8>::default();
              tree.extend(0..n, ());
              assert_eq!(
                  tree.0.height(),
                  expected_height,
                  "n = {} 的根高度应为 {}",
                  n,
                  expected_height
              );
          }
      }
      ```

   3. 运行 `cargo test -p sum_tree test_root_height_boundaries`。

3. **需要观察的现象**:五个边界值全部通过;尤其体会 n = 16 与 n = 17、n = 64 与 n = 65 这两处"恰好越界,高度 +1"的跳变。

4. **预期结果**:表中答案依次为高度 1、1、2、2、3;叶子分组为 `[4,1]`、`[4,4,4,4]`、`[4,4,4,4,1]`、16 片满叶、`[4×16, 1]`(17 片叶子)。若把某个预期值故意写错,失败信息会直接打印实际的 n 与高度,便于对照。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**:为什么不在运行时用配置(环境变量或构造参数)控制节点容量,而要用编译期常量?

**参考答案**:容量作为 const 泛型参数写进了 `ArrayVec<T, { 2 * TREE_BASE }>` 的**类型**里,节点内容因此内联在节点分配中,没有二次堆分配、没有容量分支;换成运行时配置就得改用 `Vec`,牺牲定容存储的全部收益。作为交换,测试与正式环境只能通过 `cfg` 各编译一份——对手写数据结构库这是划算的取舍。

**练习 2**:正式构建下,高度为 2 的树最多多少个元素?10000 个元素的树根高度是多少?

**参考答案**:\( C = 12 \),高度 2 至多 \( 12^3 = 1728 \) 个;高度 3 至多 \( 12^4 = 20736 \),所以 10000 个元素根高度为 3。

**练习 3**:你在 rope crate 的测试里见到一棵 `SumTree`,想数它的叶子容量——直接按"每叶最多 4 个"去断言会发生什么?

**参考答案**:会失败。rope 的测试编译时,sum_tree 以依赖身份按 `cfg(not(test))` 构建,`TREE_BASE = 6`,每叶最多 12 个。`#[cfg(test)]` 只对"正在被测试的那个 crate"生效,这是条件编译按 crate 生效的语义决定的。

### 4.3 CapacityResultExt:为定容数组断言不变量

#### 4.3.1 概念说明

定容数组 `ArrayVec` 的 `push` 不可能静默扩容,它的返回类型是 `Result<(), T>`——装不下时把元素原样还给你。面对这个 `Result`,最常见的写法是 `.unwrap()`:在"我确信一定装得下"的位置,把不可能发生的失败变成 panic。

但 `.unwrap()` 有个隐藏门槛:`Result<T, E>::unwrap` 要求错误类型 `E: Debug`(panic 时要打印它)。这里 `E = T` 是树的元素类型,而 `Item` trait 只要求 `Clone`,不要求 `Debug`。于是作者写了这个 10 行的私有工具:

[crates/sum_tree/src/sum_tree.rs:L20-L29](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L20-L29)

```rust
// Helper for when we cannot use ArrayVec::<T>::push().unwrap() as T doesn't impl Debug
trait CapacityResultExt {
    fn unwrap_oob(self);
}

impl<T> CapacityResultExt for Result<(), T> {
    fn unwrap_oob(self) {
        self.unwrap_or_else(|_| panic!("item should fit into fixed size ArrayVec"))
    }
}
```

要点逐条:

- `unwrap_or_else(|_| panic!("..."))` 的闭包**丢弃了被退回的元素**,所以不需要 `T: Debug`;panic 消息是静态字符串 "item should fit into fixed size ArrayVec";
- trait 没有 `pub`——它是模块内部的人体工学工具,不进入公共 API;
- 名字里的 oob 是 out of bounds(越界)。这个方法表达的语义是:**"调用点上方的逻辑已保证不越界,若仍越界即是程序 bug"**,和 `unreachable!()` 同类,但保留了 `Result` 的显式性。

#### 4.3.2 核心流程

```text
调用 push(...).unwrap_oob() 的前提,永远是"上方已保证容量充足":

from_iter 叶子阶段:take(2 * TREE_BASE) 限制了元素数        → collect 必然装下
from_iter 父节点阶段:装满即封箱(L302)                    → push 必然装下
push_tree_recursive:先算 child_count,超限先分裂(L840)    → push 时必然有位
from_child_trees:固定只 push 两个孩子                     → 必然装下
```

换言之,`unwrap_oob` 不是在"处理错误",而是在**声明不变量**。一旦某个分支算错了容量,这里的 panic 会立刻把 bug 暴露在最近的测试里,而不是让树悄悄丢数据。

#### 4.3.3 源码精读

**① 典型调用点之一:构建新根**

[crates/sum_tree/src/sum_tree.rs:L1102-L1113](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1102-L1113)

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
```

固定 push 两个成员,容量至少为 4,断言显然成立。顺带注意 `height = left.0.height() + 1`:树在长高时,新根的高度由孩子高度加一得到——`height` 字段的一致性在所有构造点被维护。

**② 典型调用点之二:等高拼接**

[crates/sum_tree/src/sum_tree.rs:L818-L822](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L818-L822)

```rust
} else if height_delta == 1 && !other_node.is_underflowing() {
    summaries_to_append
        .push(other_node.summary().clone())
        .unwrap_oob();
    trees_to_append.push(other).unwrap_oob();
```

这里 push 进的是**本函数栈上的临时数组**(最多收集一个待挂子树),随后的 `child_count > 2 * TREE_BASE` 检查(L840)才是真正的容量闸门。可以看到不变量的守护是分层的:临时数组先无条件收,汇总到父节点前再统一判断是否分裂。

#### 4.3.4 代码实践

**实践(源码阅读型):清点 `unwrap_oob` 的调用点并分类**

1. **实践目标**:体会在哪些位置、以什么理由"确信不越界"。

2. **操作步骤**:

   1. 在仓库根目录执行检索:

      ```bash
      grep -n "unwrap_oob" crates/sum_tree/src/sum_tree.rs
      ```

   2. 对每个调用点,在源码里向上找它的"容量保证"(是 `take` 限制、装满即封箱、还是先分裂后 push),做一张三列笔记:位置 / 周围代码在做什么 / 容量保证来自哪里。
   3. (选做,做完请还原)把 `from_child_trees` 里任意一个 `.unwrap_oob()` 改成 `.unwrap()`,运行 `cargo build -p sum_tree`,读一读编译错误——你会发现报错落在 `T` 没有实现 `Debug` 上,这正是这个 trait 存在的原因。**注意:对 `SumTree<u8>`(测试里 `u8: Debug`)编译能通过,但 crate 还要为任意 `T: Item` 编译,所以错误一定出现**;验证后用 `git restore crates/sum_tree/src/sum_tree.rs` 还原。

3. **需要观察的现象**:调用点全部集中在"先有容量论证、后有 push"的位置;改用 `.unwrap()` 后,泛型代码因缺少 `Debug` 约束而拒绝编译。

4. **预期结果**:检索到约 10 处调用(具体数目以当前源码为准);编译错误信息形如 "`T` cannot be formatted using `{:?}`" 或提示 `T: Debug` 未满足。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**:为什么不干脆给 `Item` trait 加上 `Debug` 约束,然后处处用 `.unwrap()`?

**参考答案**:那会把"实现一个可存入树的最小义务"从 `Clone + summary` 抬高到还要 `Debug`,所有下游元素类型(rope 的 `Chunk`、multi_buffer 的各种编辑项)都得为此实现 Debug;而 crate 真正需要的只是"在不变量被破坏时 panic",10 行的 `unwrap_oob` 用静态消息达到了同样效果,没有给用户加任何负担。

**练习 2**:`unwrap_oob` 与 CLAUDE.md 里"Avoid functions that panic like unwrap()"的规范矛盾吗?

**参考答案**:不矛盾。规范反对的是**把可恢复的运行时错误**用 unwrap 吞掉;`unwrap_oob` 面对的是**内部逻辑不变量**——它失败意味着代码有 bug 而非外部输入异常,这正是不变量断言(与 `assert!`/`unreachable!` 同类)的适用场景。事实上源码也刻意避开了其它可失败路径上的 unwrap(如 `Arc::get_mut` 处用 `else { unreachable!() }` 显式声明)。

### 4.4 SumTree 的 `Arc<Node>` 包装:克隆即快照,修改走写时复制

#### 4.4.1 概念说明

[crates/sum_tree/src/sum_tree.rs:L206-L213](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L206-L213)

```rust
/// A B+ tree in which each leaf node contains `Item`s of type `T` and a `Summary`s for each `Item`.
/// Each internal node contains a `Summary` of the items in its subtree.
///
/// The maximum number of items per node is `TREE_BASE * 2`.
#[derive(Clone)]
pub struct SumTree<T: Item>(Arc<Node<T>>);
```

整个"并发友好"的奥义都在这一行类型定义里:

- `SumTree` 是个**单字段新类型**,字段是 `Arc<Node<T>>`。`#[derive(Clone)]` 派生的 clone 就是 `Arc` 的 clone——一次原子引用计数加一,与树的大小无关。
- 树中每个 `Internal` 的孩子又是 `Arc<Node>`,`push`、`slice`、`append` 产出的也是 `Arc<Node>`……于是"克隆一棵树"得到的是**共享全部节点的新根句柄**,新旧两个版本可以并行地被读。
- "修改"一棵树时走 `Arc::make_mut(&mut self.0)`:若该节点只有自己引用(计数为 1),直接原地改;若被共享,先**克隆这一个节点**(孩子仍是 `Arc`,依旧共享)再改。修改影响的只是根到叶的一条路径,路径之外的子树原封不动——这就是上一讲说的"结构共享"落到字节层面的机制。

还有一个容易误判的细节:树的可观察相等与内存共享是两回事。

[crates/sum_tree/src/sum_tree.rs:L1141-L1147](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1141-L1147)

```rust
impl<T: Item + PartialEq> PartialEq for SumTree<T> {
    fn eq(&self, other: &Self) -> bool {
        self.iter().eq(other.iter())
    }
}

impl<T: Item + Eq> Eq for SumTree<T> {}
```

`==` 逐元素比较,**不看指针**。两棵布局完全不同(比如 `from_iter` 直建 vs 逐个 `push` 出来)的树,只要元素序列相同就相等。反过来,`clone` 出来的两棵树指针相同当然也相等——共享与否要用 `Arc::ptr_eq` 才能探测。

#### 4.4.2 核心流程

```text
克隆(快照):
  let snapshot = tree.clone();
    → Arc 计数 +1,两个句柄指向同一批节点          O(1)

修改(以 push 为例):
  tree.push(item):
    push 把单个元素包成一片 1 元素叶子,走 append
      → 每一层节点 Arc::make_mut(&mut self.0)
          计数为 1(没人共享):原地修改该节点
          计数 > 1(快照在场):先克隆该节点再修改
      → 孩子数组里的其余 Arc 原样搬运,继续共享
    结果:旧快照完好,新树只新增了"一条路径"的节点
```

写时复制的成本公式:一次编辑新建的节点数 = 被触碰路径的长度 ≈ 树高 + 常数,即 \( O(\log n) \) 次节点克隆,而不是 \( O(n) \) 次元素复制。

#### 4.4.3 源码精读

**① 修改路径上的 `Arc::make_mut`**

以"更新最后一个元素"为例,这是全 crate 最短的一条完整修改路径:

[crates/sum_tree/src/sum_tree.rs:L642-L676](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L642-L676)

```rust
fn update_last_recursive(
    &mut self,
    f: impl FnOnce(&mut T),
    cx: <T::Summary as Summary>::Context<'_>,
) -> Option<T::Summary> {
    match Arc::make_mut(&mut self.0) {
        Node::Internal {
            summary,
            child_summaries,
            child_trees,
            ..
        } => {
            let last_summary = child_summaries.last_mut().unwrap();
            let last_child = child_trees.last_mut().unwrap();
            *last_summary = last_child.update_last_recursive(f, cx).unwrap();
            *summary = sum(child_summaries.iter(), cx);
            Some(summary.clone())
        }
        Node::Leaf { summary, items, item_summaries } => {
            if let Some((item, item_summary)) = items.last_mut().zip(item_summaries.last_mut()) {
                (f)(item);
                *item_summary = item.summary(cx);
                *summary = sum(item_summaries.iter(), cx);
                Some(summary.clone())
            } else {
                None
            }
        }
    }
}
```

这段代码把 4.1 讲的两件事串了起来:递归沿**最右路径**下钻(内部节点只碰 `last_mut()` 的孩子),每一层 `Arc::make_mut` 决定"原地改还是先克隆";抵达叶子后改元素、重算它的 `item_summary`、再由 `item_summaries` 重算整叶 `summary`;返回途中每一层用 `child_summaries` 重算本节点 `summary` 并回写路由表——正是 4.1.1 说的"改动回写"。

写路径的主力 `push_tree_recursive` 用的是同一个模式,入口在 [sum_tree.rs:L801](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L801)(`match Arc::make_mut(&mut self.0)`),u4-l1 会专门走读。

**② 构建期的 `Arc::get_mut`:免克隆的快路径**

[crates/sum_tree/src/sum_tree.rs:L288-L296](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L288-L296)

```rust
let Node::Internal {
    summary,
    child_summaries,
    child_trees,
    ..
} = Arc::get_mut(&mut parent_node.0).unwrap()
else {
    unreachable!()
};
```

`from_iter` 逐层组装父节点时,新节点是**独占**的(刚 `Arc::new` 出来,没人共享),所以用 `Arc::get_mut` 拿可变借用,完全跳过克隆。`else { unreachable!() }` 声明的正是这条构造期不变量:"刚造出来的节点不可能被共享"。与 `make_mut` 对照着看,能准确理解两者分工:`get_mut` 是"我保证独占,借我改";`make_mut` 是"不独占就先复制"。

**③ 消费孩子时只 clone 指针**

[crates/sum_tree/src/sum_tree.rs:L1102-L1119](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1102-L1119)(`from_child_trees`,已在 4.3.3 引用全文)把 `left`/`right` 两棵树装进新根后返回。注意 `left` 是按值传入的 `SumTree`——把它 push 进 `child_trees` 只是把那个 `Arc` 移动/克隆进去;调用方若事先留了克隆(比如 `append` 里的 `self.clone()`,见 [L785](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L785) 与 [L790-L791](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L790-L791)),旧版本就作为快照存活下来。**append 从不销毁旧树,只是造一棵新树**——撤销重做、协作编辑的历史版本就是这么留下的。

**④ 最左最右:穿越指针的递归**

[crates/sum_tree/src/sum_tree.rs:L1122-L1138](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1122-L1138)

```rust
fn leftmost_leaf(&self) -> &Self {
    match *self.0 {
        Node::Leaf { .. } => self,
        Node::Internal { ref child_trees, .. } => child_trees.first().unwrap().leftmost_leaf(),
    }
}

fn rightmost_leaf(&self) -> &Self {
    match *self.0 {
        Node::Leaf { .. } => self,
        Node::Internal { ref child_trees, .. } => child_trees.last().unwrap().rightmost_leaf(),
    }
}
```

它们顺着 `child_trees` 的首/尾指针一路下到叶子,是"首元素/末元素在哪"这类查询的基础(`first`/`last`/`last_summary` 都建立在上面)。注意这里返回的是**借用**——不可变结构的一大红利:只要树还活着,指向它内部节点的引用永远合法。

#### 4.4.4 代码实践

**实践:亲手验证"克隆即共享、修改即复制"**

1. **实践目标**:用 `Arc::ptr_eq` 把两个口头结论变成可断言的事实。

2. **操作步骤**:

   1. 在 `mod tests` 里添加(**示例代码**,验证后删除):

      ```rust
      // 示例代码:学习用,验证后请删除
      #[test]
      fn test_structural_sharing() {
          let mut tree = SumTree::<u8>::default();
          tree.extend(0..100, ());

          // 克隆 = 快照:两个句柄指向同一个根节点
          let snapshot = tree.clone();
          assert!(Arc::ptr_eq(&tree.0, &snapshot.0));

          // 修改触发写时复制:根节点被换成了新分配
          tree.push(100, ());
          assert!(!Arc::ptr_eq(&tree.0, &snapshot.0));

          // 旧版本完好无损;新版本多了末尾元素
          assert_eq!(snapshot.items(()), (0..100).collect::<Vec<u8>>());
          assert_eq!(tree.items(()), (0..101).collect::<Vec<u8>>());

          // 内容相等与指针共享是两回事:
          // 重新从相同迭代器建树,内容相等但不共享任何节点
          let mut rebuilt = SumTree::<u8>::default();
          rebuilt.extend(0..101, ());
          assert_eq!(rebuilt, tree);
          assert!(!Arc::ptr_eq(&rebuilt.0, &tree.0));
      }
      ```

      (`Arc` 已由根模块的 `use std::sync::Arc` 引入,`use super::*` 后可直接使用。)

   2. 运行 `cargo test -p sum_tree test_structural_sharing`。

3. **需要观察的现象**:

   - 克隆后 `ptr_eq` 为真——克隆确实只是共享根;
   - `push` 后 `ptr_eq` 为假——`make_mut` 复制了根(以及路径上被触碰的节点),但 `snapshot` 的元素序列分毫未动;
   - 内容相等的两棵树可以不共享任何节点。

4. **预期结果**:三条断言全部通过。若想再进一步,可在 `push` 前后分别打印 `{:#?}` 对比:新树里**未被触碰的左半部分子树**与旧树逐字段相同(它们的 `Arc` 指向同一批节点,Debug 输出自然一致),被触碰的右缘路径则是新分配的。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**:`Arc::make_mut` 在"独占"与"共享"两种情况下各做什么?为什么 `from_iter` 里可以用更便宜的 `Arc::get_mut`?

**参考答案**:独占(计数 1)时原地返回可变借用;共享时先深拷贝该节点的**本体**(孩子指针依旧共享)再返回借用。`from_iter` 组装父节点时节点刚创建、必然独占,`get_mut` 不会失败,还能省掉一次冗余的克隆检查;`else { unreachable!() }` 把这条构造期不变量写在了明面上(见 [sum_tree.rs:L288-L296](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L288-L296))。

**练习 2**:对一棵 n 个元素的树做一次 `push`,大约会新分配多少个节点?为什么这与 n 无关?

**参考答案**:约等于树高加常数个——`push` 把新元素包成叶子后沿右缘路径逐层 `make_mut`,每层至多克隆一个节点(容量检查触发分裂时多出新节点,仍是每层常数个),即 \( O(\log_{2 \cdot \text{TREE\_BASE}} n) \)。路径之外的所有子树通过 `Arc` 继续共享,不产生任何分配。

**练习 3**:`assert_eq!(rebuilt, tree)` 里 `SumTree` 的 `PartialEq` 是按指针比较还是按内容比较?这个选择对测试意味着什么?

**参考答案**:按内容——`impl PartialEq` 逐元素迭代比较([L1141-L1145](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1141-L1145))。对测试是好事:`test_random` 可以放心断言"随机操作后的树 == Vec 参考模型重建的树",不必关心内部布局差异;要探测共享必须另用 `Arc::ptr_eq`。

## 5. 综合实践

把四个模块串成一个"给树做全身体检"的任务。所有改动都加在 `mod tests` 里,做完统一 `git restore crates/sum_tree/src/sum_tree.rs` 还原:

1. **建模**:先在纸上用 4.2.2 的公式推演 25 个元素(`extend(0..25, ())`)的树:叶子分组、每层节点数、根高度、根 `summary` 里 `count` 的值。
2. **透视**:用 4.1.4 的 `test_node_layout_25_items` 打印 `{:#?}`,把 Debug 输出**逐节点**整理成一张表,列为:节点类型 / height / 孩子数(或元素数)/ `summary.count`。检查:
   - 每个节点的孩子数或元素数都 ≤ `2 * TREE_BASE`(模块 4.1、4.2 的不变量);
   - 每个内部节点的 `summary.count` 等于其所有 `child_summaries` 的 `count` 之和(模块 4.1 的"汇总存两份"一致性);
   - 根 `summary.count = 25` = `extent::<Count>()`(模块 4.1 的汇总缓存红利)。
3. **快照实验**:接上 4.4.4 的 `test_structural_sharing`,在 `push(100, ())` 之后**再打印一次** `{:#?}`,对照第 2 步的表,亲手圈出"被复制的右缘路径"与"原样共享的左半子树"。
4. **收尾笔记**:用三句话回答——
   - `TREE_BASE` 同时决定了哪两条结构边界线?
   - 为什么 `child_summaries` 明明冗余却必须存在?
   - 为什么一次 `push` 之后旧快照依然完好,而内存里只多了 \( O(\log n) \) 个节点?

完成后你就把"骨架层"全部打通了:布局、容量、不变量断言、共享与写时复制,后续讲义的每个 API 都建立在这些概念之上。

## 6. 本讲小结

- `Node` 只有两个变体:**`Leaf` 独占元素**(`items` + 平行的 `item_summaries` + 整叶 `summary`),**`Internal` 只做路由**(`height` + `child_summaries` 路由表 + `child_trees` 指针数组 + 整子树 `summary`);叶子高度恒为 0 是 `Node::height()` 的约定。
- "汇总存两份"(`child_summaries` 与子节点自身的 `summary`、`item_summaries` 与重算 `item.summary()`)是**用维护成本换读取性能**:游标下钻只扫连续的纯值数组,不必解引用任何孩子指针。
- `TREE_BASE` 一常量两用:上限 `2 * TREE_BASE`(测试 4 / 正式 12)触发分裂,下限 `TREE_BASE` 判定欠溢;根高度满足 \( n \le (2 \cdot \text{TREE\_BASE})^{h+1} \),25 个元素在测试构建下必得高度 2。
- `#[cfg(test)]` 按 crate 生效:**只有 `cargo test -p sum_tree` 时容量才是 4**,下游 crate 的测试用的是 12。
- `CapacityResultExt::unwrap_oob` 用 10 行绕开 `T: Debug` 约束,把"此处必不越界"的内部不变量变成显式断言;它的每个调用点上方都有明确的容量论证。
- `SumTree(Arc<Node>)` 是"并发友好"的全部机制:`clone()` 是 O(1) 的引用计数加一(快照),修改走 `Arc::make_mut` 的写时复制,只克隆根到叶的一条路径;`==` 按内容比较,共享与否要靠 `Arc::ptr_eq` 探测。

## 7. 下一步学习建议

- **下一讲(u1-l3)《构建、运行与随机化测试》**:学会用 `SEED` / `ITERATIONS` / `OPERATIONS` 环境变量精确复现 `test_random` 的某次随机运行——本讲我们一直在"手工构造确定性输入",下一讲给你"解剖任意随机场景"的能力,也是之后排查树结构问题最重要的工具。
- 本讲的 `is_underflowing`、`child_count > 2 * TREE_BASE` 的分裂、`Arc::make_mut` 的递归修改,都是第 4 单元(u4-l1、u4-l2)append 内部机制的前置词汇,建议在笔记里给它们各留一页。
- 如果你想立刻看到"多维度汇总"在真实代码里的样子,可以提前浏览 [crates/rope/src/rope.rs](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/rope/src/rope.rs) 中 `Chunk` 与 `ChunkSummary` 的定义——把本讲的 `IntegersSummary` 想象成"字节、字符、行数多维度版"即可,系统讲解在 u2-l1 与 u5-l2。

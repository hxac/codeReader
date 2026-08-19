# slice、suffix、summary 与 SeekAggregate：让 seek 顺带产出结果

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `SeekAggregate` 这个私有 trait 的四个回调分别在 `seek_internal` 的哪个位置被触发、各自携带什么信息。
2. 解释 `Cursor::slice` 为什么能「一边定位、一边建树」，以及它如何通过 `push_tree` 里的 `Arc` 克隆实现中间子树的结构共享，使切片代价与区间长度无关。
3. 用 `Cursor::summary::<_, D>()` 做区间聚合查询，并说明它与 `SumTree::summary()`（整树汇总）的区别。
4. 解释 `suffix()` 背后的 `End` 哨兵目标为什么恒返回 `Ordering::Greater`，从而把游标一路推到树尾。
5. 亲手实现一个 `splice` 函数——这正是 `test_random` 里反复验证的核心模式，也是下一讲写路径操作的前置直觉。

## 2. 前置知识

本讲建立在 u3-l1（Cursor 栈式导航）与 u3-l2（Bias 边界归属）之上，开始前请确认你理解以下内容：

- **Cursor 是借住在树上的只读导航器**：内部用一个容量 16 的数组维护「根到叶路径栈」，`position` 是当前停靠点在某个维度 `D` 上的前缀和。
- **seek 的消费规则**（u3-l2 的核心结论，本讲反复用到）：在 `seek_internal` 中，每当比较结果满足

  \[ \text{comparison} = \text{Greater} \;\;\lor\;\; (\text{comparison} = \text{Equal} \land \text{bias} = \text{Right}) \]

  游标就「消费」一个子树或元素——把它的汇总加进 `position`、停靠槽位前移；否则下钻或停下。所以 `Bias::Right` 时结束于目标的元素被消费（右闭合），`Bias::Left` 时不消费（留给右侧）。

- **Dimension 与 SeekTarget**（u2-l2）：维度是可叠加的记账轴；`SeekTarget::cmp` 用目标与游标当前位置比较，返回 `Ordering`。

两个本讲新出现的通用概念，先用一句话建立直觉：

- **访问者 / 策略模式（visitor / strategy）**：同一段遍历代码，遍历过程中「顺手做什么」被抽象成一组成员函数，由调用方注入不同实现。`SeekAggregate` 就是这样一个访问者接口。
- **结构共享（structural sharing）**：不可变树被修改或切割时，未受影响的整棵子树用 `Arc` 克隆（只加引用计数）直接复用，只有边界路径真正重建。这是 u1-l2 讲过的「持久化数据结构」在切片场景的具体体现。

## 3. 本讲源码地图

| 文件 | 本讲涉及内容 |
| --- | --- |
| [crates/sum_tree/src/cursor.rs](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs) | 本讲主战场：`Cursor::{slice, suffix, summary}`、私有 trait `SeekAggregate` 及其三个实现（`()`, `SliceSeekAggregate`, `SummarySeekAggregate`）、哨兵目标 `End` |
| [crates/sum_tree/src/sum_tree.rs](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs) | `Bias`、`SeekTarget`、`Dimension` 的定义；`tests` 模块中的 `test_random`（splice 与 summary 对拍）、`test_cursor`、`IntegersSummary`/`Count`/`Sum` 测试脚手架 |

运行测试的方式（承接 u1-l3）：在仓库根目录执行 `cargo test -p sum_tree`。

## 4. 核心概念与源码讲解

### 4.1 SeekAggregate：seek 引擎的记账钩子

#### 4.1.1 概念说明

回顾 u3-l1：`Cursor::seek` 与 `seek_forward` 都会调用同一个私有引擎 `seek_internal`，从根到叶一路决定「消费还是下钻」。现在补上被 u3-l1 刻意略过的那个参数：`seek_internal` 还接收一个 `&mut dyn SeekAggregate<'a, T>`。

`SeekAggregate` 是一个模块私有的 trait（cursor.rs 中没有 `pub`），它把「seek 路上经过的东西」以回调的形式报告给一个聚合器。crate 内置三个实现，对应三种使用意图：

| 实现 | 用途 | 被谁使用 |
| --- | --- | --- |
| `()` | 什么都不记录，纯定位 | `seek` / `seek_forward` |
| `SliceSeekAggregate<T>` | 把经过的子树与元素攒成一棵新的 `SumTree` | `Cursor::slice` |
| `SummarySeekAggregate<D>` | 只把经过的汇总叠加到一个维度 `D` 上 | `Cursor::summary` |

也就是说：**seek、slice、summary 共用同一套下钻与消费逻辑**，区别只在「路上捡到东西怎么办」。这是典型的访问者模式——`seek_internal` 只写一份，三种行为以动态分发（`&mut dyn`）注入，而不会为每种聚合器复制一份几乎相同的树遍历代码。

顺带一提，「单元类型实现 trait 当空操作」这个惯用法你在 u2-l2 见过一次（`()` 作为零维度），这里是同一招在另一个 trait 上的复用。

#### 4.1.2 核心流程

`SeekAggregate` 有四个回调，触发时机全部在 `seek_internal` 的循环里：

```
seek_internal(target, bias, aggregate):
    断言 target 不在当前 position 之前（不许后退）
    若从未 seek 过：把根节点压栈
    循环处理栈顶，直到在某片叶子停下或栈被弹空：
        栈顶是内部节点：
            对当前槽位起的每个子树：
                若消费条件成立（Greater 或 Equal+Right）：
                    position += 子树汇总
                    aggregate.push_tree(子树)          ← 回调 1
                    停靠槽位 +1
                否则：把该子树压栈、下钻
        栈顶是叶子：
            aggregate.begin_leaf()                     ← 回调 2
            对当前槽位起的每个元素：
                若消费条件成立：
                    position += 元素汇总
                    aggregate.push_item(元素)          ← 回调 3
                    停靠槽位 +1
                否则：aggregate.end_leaf()；停止      ← 回调 4
            叶子耗尽：aggregate.end_leaf()             ← 回调 4（又一次）
    返回是否精确命中目标端点
```

四个回调与三个实现的对照表（本讲后续两节逐个精读后实现部分）：

| 回调 | 触发时机 | `()` | `SliceSeekAggregate` | `SummarySeekAggregate` |
| --- | --- | --- | --- | --- |
| `begin_leaf` | 每进入一个叶子 | 空 | 空 | 空 |
| `push_item` | 叶子内消费一个元素 | 空 | 元素与汇总克隆进缓冲 | `add_summary` |
| `push_tree` | 内部层整体消费一棵子树 | 空 | `append(子树的 Arc 克隆)` | `add_summary` |
| `end_leaf` | 叶子访问结束（中途停下或自然耗尽） | 空 | 把缓冲打包成新叶子 `append` 进结果树 | 空 |

注意一个细节：**`push_tree` 只会针对「整体位于目标之前」的完整子树触发**。只要下钻进了某棵子树，它的内容就改由叶子层的 `push_item` 逐个上报。这条「内外分层上报」的规则正是 slice 能做到结构共享的关键，4.2 详述。

#### 4.1.3 源码精读

先是 trait 本体，四个方法都是 `&mut self`，参数全部带 `'a` 生命周期（允许实现方保留引用；当前两个具体实现实际都是立即克隆或立即叠加）：

[crates/sum_tree/src/cursor.rs:L748-L763](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L748-L763) —— 定义 `SeekAggregate` trait：`begin_leaf` / `end_leaf` / `push_item` / `push_tree` 四个回调，后两者接收被消费的元素或子树及其汇总。

单元类型的空实现，四个方法体全是空的：

[crates/sum_tree/src/cursor.rs:L774-L785](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L774-L785) —— 为 `()` 实现空的 `SeekAggregate`，让纯定位的 seek 不付任何记账成本。

再看引擎侧。`seek` 与 `seek_forward` 各传入一个 `&mut ()`：

[crates/sum_tree/src/cursor.rs:L408-L415](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L408-L415) —— `Cursor::seek` 先 `reset()` 再调用 `seek_internal(pos, bias, &mut ())`，聚合器是空实现。

[crates/sum_tree/src/cursor.rs:L465-L470](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L465-L470) —— `seek_internal` 的签名：第三个参数是 `&mut dyn SeekAggregate<'a, T>`，即动态分发的记账钩子。

内部层的消费分支里，`push_tree` 的调用点：

[crates/sum_tree/src/cursor.rs:L500-L525](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L500-L525) —— 内部节点中逐子树判断：若目标在子树结束点之后（或相等且 `Bias::Right`），则 `position` 前进、`aggregate.push_tree(child_tree, child_summary, cx)`、停靠槽位加一；否则把子树压栈下钻（`continue 'outer`）。

叶子层的三个回调调用点：

[crates/sum_tree/src/cursor.rs:L528-L556](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L528-L556) —— 进入叶子先 `begin_leaf()`；逐元素判断消费条件，消费则 `push_item`；在某元素前停下时先 `end_leaf` 再 `break 'outer`；若叶子被耗尽，循环结束后同样调用 `end_leaf`。

#### 4.1.4 代码实践

**实践目标**：不运行任何代码，仅靠阅读，画出 `seek_internal` 中四个回调的全部调用点，并验证一张「回调顺序」预测表。

**操作步骤**：

1. 打开 [crates/sum_tree/src/cursor.rs:L465-L575](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L465-L575)，数一数 `aggregate.` 出现的位置（共 5 处调用、4 种方法，`end_leaf` 出现两次）。
2. 对一棵高 1、叶子为 `[0,1,2,3] [4,5,6,7]`（测试构建 TREE_BASE=2 时 `SumTree::from_iter(0..8, ())` 的典型形态）、游标 `cursor::<Count>(())` 执行 `slice(&Count(6), Bias::Right)`，手工写出回调序列。
3. 把你的答案与下面给出的参考序列对照。

**需要观察的现象**：回调序列中 `push_tree` 与 `push_item` 的交替模式——先 `push_item`（左边界叶子的前半段）、`end_leaf`，再 `push_tree`（被整棵跨过的子树，若有），最后 `push_item`（右边界叶子）、`end_leaf`。

**预期结果**（按代码推导；具体序列待本地验证）：高 1 的树下，`Count(6)`/`Right` 大致产生

```text
begin_leaf, push_item(4), push_item(5), end_leaf   ← 第 2 个叶子只消费前两个元素
```

且在此之前第 1 个叶子 `[0,1,2,3]` 是被内部层 `push_tree` 整棵上报的（四个元素全部位于目标之前）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `seek_internal` 用 `&mut dyn SeekAggregate`（动态分发）而不是泛型参数？

**参考答案**：`SeekAggregate` 的四个方法都是 `&mut self` 且不含泛型方法，天然对象安全；用 trait 对象后 `seek_internal` 只编译出一份代码，`seek`/`slice`/`summary` 三个公开入口共享同一个引擎。相比树导航本身的开销，每次回调的虚表分发代价可以忽略，却换来了「一份遍历逻辑、多种聚合行为」的结构。（这是从代码形态读出的设计事实；是否有性能考量属于合理推断。）

**练习 2**：`begin_leaf` 在两个具体实现里都是空操作，它为什么还存在于 trait 里？

**参考答案**：它是「进入叶子」这一事件的生命周期钩子，为聚合器提供了对称的接口（begin/end 成对出现）。当前的实现不需要在进入时做初始化（缓冲在 `end_leaf` 里被 `mem::take` 清空，天然从空开始），所以是空操作；但接口保留了表达这类需求的能力。

**练习 3**：`push_tree` 的参数里有子树的 `summary`，但 `SliceSeekAggregate::push_tree` 的实现把它忽略了（形参名为 `_`）。为什么可以忽略？

**参考答案**：`SumTree<T>` 自带根汇总（`SumTree::summary()` O(1) 可取），`append` 拼接时直接使用子树自带的汇总即可，无需外部再传一份。参数保留 `summary` 是为了 `SummarySeekAggregate` 这类只关心汇总、不关心树的实现能拿到数据。

---

### 4.2 Cursor::slice 与 SliceSeekAggregate：边定位边建树

#### 4.2.1 概念说明

`Cursor::slice(end, bias)` 返回从**游标当前位置**到 `end` 之间的所有元素组成的新 `SumTree`。它不额外遍历——`seek_internal` 在定位的过程中，把路过、消费的东西顺手交给 `SliceSeekAggregate`，聚合器把它们拼成结果树。

关键在于 `SliceSeekAggregate` 对两类回调采用了完全不同的两种策略：

- **`push_tree`（整棵子树被跨过）**：`self.tree.append(tree.clone(), cx)`。`SumTree<T>` 就是 `Arc<Node<T>>`（u1-l2），`clone` 只增加引用计数，子树里的所有元素与汇总**原封不动地与原树共享**。
- **`push_item`（边界叶子里的零散元素）**：把元素与汇总克隆进一个容量 `2 * TREE_BASE` 的定容缓冲，等 `end_leaf` 触发时打包成一个新叶子节点，再 `append` 进结果树。

也就是说：**区间的「腹部」整树共享，「两端」逐元素重建**。于是切片的代价只与树高和边界有关，与区间里有多少元素无关——切出 10 个元素和切出 100 万个元素，边界工作量相同。作为对比，`Vec` 的切片是 O(k) 的 memcpy（这未必更慢，但当你随后还要在两个端点处继续增删、并且想让新旧版本共存时，结构共享的优势就显出来了）。

一个保守的复杂度上界：

\[ T_{\text{slice}} = O(\underbrace{\log n}_{\text{定位}}) + O\!\left(\underbrace{\log n}_{\text{被消费子树数}} \times \underbrace{\log n}_{\text{单次 append}}\right) = O(\log^2 n) \]

实际通常更低（append 在两树高度匹配时近乎直接挂接），但要点是：**式中没有区间长度 k**。

还有一个正确的心理模型：`slice` 之后游标就停在 `end` 处（它本来就是一次 seek）。所以「切三段」就是连续三次 `slice`，每次的起点是上一次的终点。`test_random` 的 splice 正是这个模式。

#### 4.2.2 核心流程

`SliceSeekAggregate` 维护三样东西：

```text
tree: SumTree<T>                  # 结果树，从 SumTree::new(cx) 开始
leaf_items: ArrayVec<T, 2*TREE_BASE>        # 边界元素的缓冲
leaf_item_summaries: ArrayVec<Summary, 2*TREE_BASE>
leaf_summary: T::Summary          # 缓冲内元素的汇总和
```

一次 `slice` 的完整流程：

1. `Cursor::slice` 构造一个空聚合器（结果树为 `SumTree::new(self.cx)`）。
2. 调用 `seek_internal(end, bias, &mut 聚合器)`，正常下钻定位。
3. 途中：
   - 整棵子树被跨过 → `push_tree` → `tree.append(子树.clone())`；
   - 叶子内元素被消费 → `push_item` → 塞进缓冲、`leaf_summary` 累加；
   - 叶子访问结束 → `end_leaf` → 用缓冲构造 `Node::Leaf`，`tree.append(新叶子)`，缓冲被 `mem::take` 清空。
4. `seek_internal` 返回（bool 是否精确命中被 `slice` 丢弃），返回 `slice.tree`。

一个值得注意的容量不变量：**`end_leaf` 在每次叶子访问结束时都会执行**（中途停下或自然耗尽各有一个调用点，见 4.1.3 的两个 `end_leaf`），而 `begin_leaf` 是空操作、缓冲在被 `take` 后为空——所以缓冲里同时至多只装「一个叶子里被消费的那部分元素」，上限 `2 * TREE_BASE`，恰好等于 `leaf_items` 的定容。`push` 后的 `unwrap_oob()`（u1-l2 讲过的那个十行断言）把这条内部不变量显式化了。

Bias 的语义在这里再次生效（承接 u3-l2）：`slice(end, Bias::Right)` 会把「结束于 end」的最后一个元素收进结果（右闭合）；`Bias::Left` 则不收。因此对同一目标做两次切割时，**切点配 Right 则边界元素归左段、配 Left 归右段**。

#### 4.2.3 源码精读

入口只有 12 行，结果树从空树起步：

[crates/sum_tree/src/cursor.rs:L432-L444](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L432-L444) —— `Cursor::slice`：构造 `SliceSeekAggregate`（内含 `SumTree::new(self.cx)` 与空的叶子缓冲），交给 `seek_internal`，最后返回 `slice.tree`。

聚合器的字段，两个定容数组容量正是 `2 * TREE_BASE`：

[crates/sum_tree/src/cursor.rs:L765-L770](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L765-L770) —— `SliceSeekAggregate` 结构：结果树 `tree`、叶子元素缓冲 `leaf_items`、叶子汇总缓冲 `leaf_item_summaries`、缓冲累计汇总 `leaf_summary`。

三个方法的实现，每个都很短：

[crates/sum_tree/src/cursor.rs:L787-L817](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L787-L817) —— `SliceSeekAggregate` 的三个回调实现。`end_leaf` 用 `mem::replace`/`mem::take` 取走缓冲，打包成 `Node::Leaf` 后 `append` 进结果树；`push_item` 克隆元素与汇总入缓冲并累加 `leaf_summary`；`push_tree` 忽略 summary 参数，直接 `self.tree.append(tree.clone(), cx)`——这一行就是结构共享的全部秘密：`tree.clone()` 是 `Arc` 引用计数加一。

结合 `seek_internal` 的调用点看「腹部共享、两端重建」：内部层的 `push_tree`（[crates/sum_tree/src/cursor.rs:L500-L525](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L500-L525)）只在「子树整体在目标之前」时触发；一旦下钻，就由叶子层的 `push_item`（[crates/sum_tree/src/cursor.rs:L528-L556](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L528-L556)）逐元素上报。

`test_random` 中的实战用法（第 5 节综合实践会完整复刻它）：

[crates/sum_tree/src/sum_tree.rs:L1459-L1470](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1459-L1470) —— 随机测试中的 splice：先 `cursor.slice(&Count(splice_start), Bias::Right)` 保住前缀，插入新元素，再 seek 到删除终点后 `cursor.slice(&tree_end, Bias::Right)` 接回后缀，两次 slice 之间的元素即被删除。

顺带两个可以在测试里直接看到断言的例子：

[crates/sum_tree/src/sum_tree.rs:L1598-L1601](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1598-L1601) —— `test_cursor` 对空树断言 `slice(&Count(0), Bias::Right).items(())` 为空。

[crates/sum_tree/src/sum_tree.rs:L1642-L1643](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1642-L1643) —— 单元素树切 `Count(1)` 配 `Bias::Right` 得到 `[1]`，切完后 `item()` 为 `None`（游标停在切点）。

#### 4.2.4 代码实践

**实践目标**：直观感受「切一半」的结果正确性，以及切片后游标停在切点、可直接续切的行为。

**操作步骤**（以下均为示例代码，写入 `crates/sum_tree/src/sum_tree.rs` 的 `mod tests` 内，然后 `cargo test -p sum_tree`）：

```rust
#[test]
fn test_slice_in_half() {
    let tree = SumTree::from_iter(0..100u8, ());
    let mut cursor = tree.cursor::<Count>(());

    let head = cursor.slice(&Count(40), Bias::Right);
    let tail = cursor.suffix(); // 下一节讲的 End 哨兵，先借用一下

    assert_eq!(head.items(()), (0..40).collect::<Vec<u8>>());
    assert_eq!(tail.items(()), (40..100).collect::<Vec<u8>>());

    // slice 本身就是一次 seek：切完 head 后游标在 Count(40)
    assert_eq!(cursor.start().0, 40);

    // 拼回去，元素序列恢复
    let mut restored = head;
    restored.append(tail, ());
    assert_eq!(restored.items(()), tree.items(()));
}
```

**需要观察的现象**：

1. 三条 `assert_eq!` 全部通过；`cursor.start().0 == 40` 证明 slice 后游标确实停在切点。
2. 在测试里临时加一行 `eprintln!("{:?}", head);`（`SumTree` 实现了 `Debug`），观察结果树的节点形态。
3. 试着把 `Bias::Right` 改成 `Bias::Left` 再运行，看 `head`/`tail` 各自少了/多了哪个元素。

**预期结果**：

1. 断言通过（与 `test_cursor`/`test_random` 中同型断言一致，行为有既有测试背书）。
2. `Bias::Left` 时 `head` 变成 `(0..39)`、`tail` 变成 `(39..100)`——索引 39 的元素结束于 `Count(40)`，Left 不消费它，归入右段。（预期结果，待本地验证。）
3. Debug 输出中结果树的中间大块与原树对应区间内容一致——这是结构共享的间接体现；`slice(&Count(0), Bias::Right)`（空切片）会包含一个汇总为零的空叶子，这是 `end_leaf` 在零缓冲时也会打包的推论，可在 Debug 输出里核对（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `end_leaf` 里用 `mem::replace` / `mem::take` 而不是先 `clone` 再 `clear`？

**参考答案**：`mem::take` 把缓冲的所有权直接移交给新造的 `Node::Leaf`，同时让字段回到空状态，一次移动、零复制；`clone` 再 `clear` 则要完整复制一遍定容数组。另外这个写法也保证了「缓冲在两次叶子访问之间必然为空」这一容量不变量不被破坏。

**练习 2**：`slice` 的文档注释说 "Advances the cursor and returns traversed items as a tree"。请解释 "advances" 的含义，以及它对「连续多次 slice」意味着什么。

**参考答案**：`slice` 内部就是一次 `seek_internal`，执行完游标停在 `end` 目标处、`position` 前进到切点。因此多次 `slice` 天然首尾相接：第 n 段的起点就是第 n-1 段的终点，无需重新定位（不过注意入口断言禁止目标在当前位置之前——切点必须单调不减）。

**练习 3**：如果两棵树存放相同元素但节点布局不同（例如一棵来自 `from_iter`、一棵来自多次 `append`），`tree1 == tree2` 成立吗？

**参考答案**：成立。`SumTree` 的 `PartialEq` 是逐元素比较迭代器的（见 [crates/sum_tree/src/sum_tree.rs:L1141-L1145](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1141-L1145) 的 `self.iter().eq(other.iter())`），与布局无关。不过 `test_random` 的对拍惯用 `assert_eq!(tree.items(()), reference_items)`，比较 `items()` 是更直白的写法。

---

### 4.3 Cursor::summary 与 SummarySeekAggregate：只记账、不建树

#### 4.3.1 概念说明

很多查询并不需要新树，只需要一个数：某区间内有多少个元素？字节偏移 1000 到 2000 之间有多少行？`Cursor::summary(end, bias)` 就是这个「区间聚合查询」接口——它跑同样的 `seek_internal`，但聚合器只做一件事：把路过元素的汇总 `add_summary` 到一个维度值上，**不克隆任何元素、不建任何节点**。

它和两个「近亲」的区别要分清：

| 接口 | 语义 | 代价 |
| --- | --- | --- |
| `SumTree::summary()`（[crates/sum_tree/src/sum_tree.rs:L736](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L736)） | 整棵树的根汇总 | O(1) |
| `Cursor::end()` | 当前停靠元素**自身**的结束位置 | O(1) |
| `Cursor::summary(end, bias)` | **从游标当前位置到 end** 的区间汇总 | 同 slice：只与树高和边界相关 |

一个容易踩的坑：`Cursor::summary` 度量的起点是**游标当前停靠处**，不是树头。所以「求区间 [a, b) 的聚合」要先 `seek(&Count(a), ...)` 再 `summary(&Count(b), ...)`——`test_random` 里正是这么写的。

另一个精妙之处：方法签名为 `summary<Target, Output>`，`Output: Dimension` 与游标自身的记账维度 `D` **互相独立**。你可以让游标用 `Count` 寻路，却让输出落在 `Sum` 维度上——寻路轴和产出轴解耦，这正是 u2-l2「多维度」设计的落地点。

#### 4.3.2 核心流程

```text
summary(end, bias):
    out = Output::zero(cx)
    聚合器 = SummarySeekAggregate(out)
    seek_internal(end, bias, 聚合器):
        push_item(_, summary, _)  →  out.add_summary(summary)
        push_tree(_, summary, _)  →  out.add_summary(summary)   # 子树汇总整块叠加
    返回 out
```

注意 `push_tree` 在这里同样生效：腹部子树不必逐元素展开，直接把**子树的整块汇总**叠加一次即可——这就是「只记账」版本的结构共享：共享的不是节点，而是已算好的前缀和。

#### 4.3.3 源码精读

入口，注意泛型参数 `Output` 与游标维度 `D` 相互独立：

[crates/sum_tree/src/cursor.rs:L452-L460](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L452-L460) —— `Cursor::summary`：从 `Output::zero` 起步，用 `SummarySeekAggregate` 包住它跑 `seek_internal`，返回维度值。

聚合器本体，一个单元结构体装着维度值：

[crates/sum_tree/src/cursor.rs:L819-L841](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L819-L841) —— `SummarySeekAggregate` 的实现：`push_item` 与 `push_tree` 都只调用 `self.0.add_summary(summary, cx)`，树与元素参数被忽略；`begin_leaf`/`end_leaf` 为空。

`test_random` 对 slice 与 summary 的交叉验证——同一区间，两种聚合器必须给出一致答案：

[crates/sum_tree/src/sum_tree.rs:L1581-L1588](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1581-L1588) —— 测试先 seek 到区间起点，分别用 `cursor.slice` 与 `cursor.summary::<_, Sum>` 求区间，再断言 `summary.0 == slice.summary().sum`：建树版与记账版结果相等。

输出维度 `Sum` 与寻路维度 `Count` 的定义位置（u2-l1/u2-l2 已精读，此处仅作锚点）：

[crates/sum_tree/src/sum_tree.rs:L1878-L1902](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1878-L1902) —— `Count` 与 `Sum` 对 `IntegersSummary` 的 `Dimension` 实现：前者累加 `count` 字段，后者累加 `sum` 字段。

#### 4.3.4 代码实践

**实践目标**：用 `Cursor::summary` 求区间和，并与朴素迭代求和对拍。

**操作步骤**（示例代码，放入 `mod tests`）：

```rust
#[test]
fn test_range_sum() {
    let items: Vec<u8> = (0..=50).collect(); // 0,1,2,...,50
    let mut tree = SumTree::default();
    tree.extend(items.iter().copied(), ());

    let (start, end) = (10usize, 30usize);
    let mut cursor = tree.cursor::<Count>(());
    cursor.seek(&Count(start), Bias::Right);
    let range_sum = cursor.summary::<_, Sum>(&Count(end), Bias::Right);

    let naive = items[start..end].iter().map(|&b| b as usize).sum::<usize>();
    assert_eq!(range_sum.0, naive);
}
```

**需要观察的现象**：断言通过；把 `(start, end)` 换成 `(0, items.len())`、`(5, 5)`、`(49, 60)`（end 越过树尾，此时参考答案应截断到 `items[49..]`）等情况重跑。

**预期结果**：

- `(0, items.len())` 时结果等于整树 `tree.summary().sum`（区间覆满全树时区间聚合 = 根汇总）。
- `(5, 5)` 为空区间，结果为 0。
- `(49, 60)`：end 越过树尾时 `seek_internal` 停在树尾，结果等于从 49 到结尾的求和（游标越尾行为承接 u3-l1）。以上均待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Cursor::summary` 比「先 `slice` 再读 `slice.summary()`」更省？

**参考答案**：`slice` 要为边界元素克隆数据、打包新叶子、维护结果树的不变量；`SummarySeekAggregate` 只在每个被消费的子树/元素上做一次 `add_summary`，腹部子树的汇总整块叠加，不分配任何节点。当查询只需要一个聚合值时，省掉了全部建树开销。

**练习 2**：`cursor.summary::<_, Sum>(&Count(end), bias)` 中 `bias` 会影响结果吗？

**参考答案**：会。`bias` 决定「结束于目标的元素」是否被消费（u3-l2 规则）：`Bias::Right` 时它计入聚合，`Bias::Left` 时不计。对离散求和这类场景，二者恰好差一个边界元素的贡献。

**练习 3**：能否让游标以 `Sum` 维度寻路、输出 `Count`？

**参考答案**：可以。`Cursor::summary` 的约束是 `Target: SeekTarget<'a, T::Summary, D>`（与游标维度 `D` 匹配）加 `Output: Dimension`（任意维度）。用 `tree.cursor::<Sum>(())` 寻路、`cursor.summary::<_, Count>(...)` 输出即可——例如「从字节偏移 500 到 1000 之间有多少个元素」这类跨轴查询。

---

### 4.4 suffix 与 End：吃到结尾的哨兵目标

#### 4.4.1 概念说明

「从当前位置取到树尾」是一个高频需求（切掉前缀、取后半段），但它有个小麻烦：取到结尾需要一个「比一切位置都大」的目标。为此 cursor.rs 内部定义了一个哨兵（sentinel）类型 `End<D>`：它的 `SeekTarget::cmp` **对任何输入都返回 `Ordering::Greater`**。

回看消费规则（Greater 或 Equal+Right 即消费），恒 Greater 意味着 `seek_internal` 里**每个**子树、每个元素都满足消费条件：一路吃、永不减速、直到栈被弹空——游标停在树尾，`at_end` 置真（u3-l1 讲过的越尾状态）。配上 `SliceSeekAggregate`，被吃掉的正是「当前位置之后的全部内容」，这就是 `suffix()`：

```rust
pub fn suffix(&mut self) -> SumTree<T> {
    self.slice(&End::new(), Bias::Right)
}
```

两个设计细节值得品：

- `End<D>(PhantomData<D>)` 用幻影类型绑定了维度 `D`，使 `End<D>` 能为任意游标维度满足 `SeekTarget<'a, S, D>`；由于 `cmp` 根本不看参数，这个绑定纯粹是为了类型检查通过。
- `End` 和 `End::new` 都是**模块私有**的，外部用户拿不到。 「取到结尾」这个意图被封成了命名方法 `suffix()`——调用方读到的是语义，而不是「构造一个恒 Greater 的怪目标」这种实现细节。

另外，`seek_internal` 入口有一道断言「目标不得在当前位置之前」（[crates/sum_tree/src/cursor.rs:L471-L474](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L471-L474)），`End` 恒 Greater 所以天然通过——`suffix` 在游标任何停靠处都能调用。

#### 4.4.2 核心流程

```text
suffix():
    等价于 slice(&End::new(), Bias::Right)
    End.cmp(...) = Greater（恒真）
        → 内部层：每棵子树都满足消费条件 → push_tree → 整树 Arc 共享
        → 叶子层：每个元素都满足消费条件 → push_item → 边界重建
    栈被弹空，at_end = true，item() 返回 None
    返回「当前位置到树尾」构成的新树
```

于是「split at k」的完整写法是：

```text
head = cursor.slice(&Count(k), Bias::Right)   # [0, k)，边界元素按 Right 归左段
tail = cursor.suffix()                        # [k, n)
```

两次调用共享同一个游标，第二次从第一次的切点续切。

#### 4.4.3 源码精读

[crates/sum_tree/src/cursor.rs:L447-L449](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L447-L449) —— `Cursor::suffix`：一行，转发到 `slice(&End::new(), Bias::Right)`。

[crates/sum_tree/src/cursor.rs:L843-L861](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L843-L861) —— `End<D>(PhantomData<D>)` 的定义、私有的 `new`、以及 `SeekTarget` 实现：`cmp` 无视参数恒返回 `Ordering::Greater`，另附 `Debug` 实现。

对照 seek 的普通目标 `Count`（Ord 型目标，按数值比较）：

[crates/sum_tree/src/sum_tree.rs:L122-L130](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L122-L130) —— `SeekTarget` trait 与「任意 `Dimension + Ord` 类型自动成为目标」的包络实现；`End` 是这条 trait 之外的定制目标：不比较、恒 Greater。

一个有意思的对照点：`test_random` 的 splice 里第二段用的是 `cursor.slice(&tree_end, Bias::Right)`（[crates/sum_tree/src/sum_tree.rs:L1467-L1468](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1467-L1468)，`tree_end` 是事先记下的整树 extent）——由于目标就是树尾，效果与 `cursor.suffix()` 等价（两者都会把剩余内容全部消费）。读代码时能识别这类「可替换写法」，说明你已经把 `End` 的语义内化了。

#### 4.4.4 代码实践

**实践目标**：实现并验证一个 `split` 函数，体会「同一游标上连续切割」与 `End` 哨兵的配合。

**操作步骤**（示例代码，放入 `mod tests`）：

```rust
fn split(tree: &SumTree<u8>, at: usize) -> (SumTree<u8>, SumTree<u8>) {
    let mut cursor = tree.cursor::<Count>(());
    let head = cursor.slice(&Count(at), Bias::Right);
    let tail = cursor.suffix();
    (head, tail)
}

#[test]
fn test_split() {
    let mut tree = SumTree::default();
    tree.extend(vec![10, 20, 30, 40, 50], ());

    let (head, tail) = split(&tree, 2);
    assert_eq!(head.items(()), vec![10, 20]);
    assert_eq!(tail.items(()), vec![30, 40, 50]);

    // 后缀取尽后游标在树尾
    // （split 借用了 tree，这里重新造一个游标验证）
    let mut cursor = tree.cursor::<Count>(());
    cursor.seek(&Count(2), Bias::Right);
    let _tail = cursor.suffix();
    assert_eq!(cursor.item(), None);
    assert_eq!(cursor.start().0, 5);
}
```

**需要观察的现象**：断言通过；把 `at` 改为 0 和 5（两个端点）重跑；再给 `split` 的第一个切点换成 `Bias::Left`，观察边界元素 `20` 归属哪一段。

**预期结果**：

- `at = 0`：head 为空、tail 为整树；`at = 5`：head 为整树、tail 为空。
- 第一个切点换 `Bias::Left` 后：结束于 `Count(2)` 的元素 `20` 不再被左段消费，head 变为 `[10]`、tail 变为 `[20, 30, 40, 50]`——与 u3-l2 的规则「结束于目标的元素，Right 归左段、Left 归右段」一致。（预期结果，待本地验证。）

#### 4.4.5 小练习与答案

**练习 1**：`End<D>` 里的 `PhantomData<D>` 起什么作用？去掉行不行？

**参考答案**：`cmp` 不含 `D` 类型的值，编译器会报「未使用的类型参数」；`PhantomData<D>` 让 `End<D>` 在类型层面携带维度信息，从而使 `impl SeekTarget<'a, S, D> for End<D>` 对任意 `D: Dimension<'a, S>` 成立，且调用 `suffix()` 时类型推断能从游标确定 `D`。去掉它类型参数无处安放，无法通过编译。

**练习 2**：`suffix()` 为什么固定用 `Bias::Right`？换成 `Left` 有区别吗？

**参考答案**：没有实际区别。`End` 恒返回 `Greater`，消费条件里的 `Equal && bias == Right` 分支永远不会走到，bias 在这里是个不参与决策的参数；写 `Right` 只是语义上「把一切都收进来」的自然选择。

**练习 3**：`suffix` 之后继续调用 `cursor.next()` 会发生什么？

**参考答案**：栈已空且 `at_end == true`，`search_forward` 里的 `if !self.at_end` 不成立，不会重新压栈，`item()` 保持 `None`——游标停在树尾之后，直到你再次 `seek`（会 `reset()` 重置）才会复活。这与 u3-l1 讲的越尾状态一致。

---

## 5. 综合实践

把本讲三个聚合器相关的知识串起来，实现 `test_random` 中被百万次验证的核心模式：**基于游标切割的 splice**。

**任务**：实现下面的函数，并证明它与 `Vec::splice` 语义一致（示例代码）：

```rust
fn splice(tree: SumTree<u8>, range: std::ops::Range<usize>, new_items: Vec<u8>) -> SumTree<u8> {
    let tree_end = tree.extent::<Count>(()).0;
    let mut cursor = tree.cursor::<Count>(());

    // 第 1 刀：保住 [0, range.start)，切点配 Right，边界元素归左段
    let mut new_tree = cursor.slice(&Count(range.start), Bias::Right);

    // 中段：换成新元素（append 的知识来自 u2-l3）
    new_tree.extend(new_items, ());

    // 跳过 [range.start, range.end)：seek 本身不产出，聚合器是 ()
    cursor.seek(&Count(range.end), Bias::Right);

    // 第 2 刀：接回 [range.end, 树尾)
    new_tree.append(cursor.slice(&Count(tree_end), Bias::Right), ());
    new_tree
}
```

对照原版：这与 [crates/sum_tree/src/sum_tree.rs:L1459-L1470](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1459-L1470) 逐行同构，只是把随机量换成了参数、把 `extend`/`par_extend` 二选一固定为 `extend`。

**验证测试**（示例代码，与上面的 `splice` 一起放进 `mod tests`）：

```rust
#[test]
fn test_my_splice() {
    let mut tree = SumTree::<u8>::default();
    tree.extend(vec![1, 2, 3, 4, 5], ());

    let spliced = splice(tree, 1..3, vec![9, 9, 9]);

    let mut reference = vec![1, 2, 3, 4, 5];
    reference.splice(1..3, vec![9, 9, 9]);

    assert_eq!(spliced.items(()), reference); // [1, 9, 9, 9, 4, 5]
}

#[test]
fn test_my_splice_edges() {
    let build = || {
        let mut t = SumTree::<u8>::default();
        t.extend(vec![1, 2, 3, 4, 5], ());
        t
    };

    // 空替换、空区间
    let t = splice(build(), 2..2, vec![]);
    assert_eq!(t.items(()), vec![1, 2, 3, 4, 5]);

    // 删到树尾
    let t = splice(build(), 3..5, vec![7]);
    assert_eq!(t.items(()), vec![1, 2, 3, 7]);

    // 从树头开始删
    let t = splice(build(), 0..5, vec![]);
    assert_eq!(t.items(()), Vec::<u8>::new());
}
```

**操作步骤**：

1. 打开 `crates/sum_tree/src/sum_tree.rs`，把两个函数添加到文件底部的 `mod tests` 内（`Count`、`Sum` 等类型就在同一模块中定义，[crates/sum_tree/src/sum_tree.rs:L1828-L1832](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1828-L1832)，无需任何 import）。
2. 运行 `cargo test -p sum_tree test_my_splice`。
3. 选做：仿照 `test_random`（[crates/sum_tree/src/sum_tree.rs:L1417-L1472](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1417-L1472)）写一个随机版——用 `StdRng::seed_from_u64(seed)` 随机生成 `range` 与 `new_items`，循环断言 `splice` 结果与 `Vec::splice` 参考模型一致。
4. 选做：把第二刀的 `cursor.slice(&Count(tree_end), Bias::Right)` 换成 `cursor.suffix()`，重跑测试，验证 4.4 节所说的等价性。

**需要观察的现象**：所有断言通过；随机版在不同 seed 下稳定对拍。

**预期结果**：

- `test_my_splice` 通过，结果为 `[1, 9, 9, 9, 4, 5]`。
- 边界用例通过：`2..2` 空区间不改变内容、`3..5` 与 `0..5` 正确处理树尾与树头。
- 换成 `suffix()` 后测试依旧通过（两者语义等价）。
- 本节代码未在撰写环境中实际运行，均为依据源码与既有同型测试推导的预期，待本地验证。

**为什么这个模式重要**：它把「删除 + 插入」变成「两次切割 + 一次 extend + 一次 append」，全程没有逐元素挪动中段数据——这正是 Zed 的 rope 能在亿字节文本上做高效编辑的机制原型（u5-l2 会看到它的生产版本）。

## 6. 本讲小结

- `SeekAggregate` 是 `seek_internal` 的记账钩子：`begin_leaf`/`push_item`/`push_tree`/`end_leaf` 四个回调把「seek 路上消费了什么」报告给聚合器；`seek`/`seek_forward` 传入空实现 `()`。
- `Cursor::slice` 用 `SliceSeekAggregate` 边定位边建树：腹部子树经 `push_tree` 以 `Arc` 克隆整棵共享，只有两端边界叶子经 `push_item` + `end_leaf` 逐元素重建；切片代价与区间长度无关（保守上界 \( O(\log^2 n) \)）。
- `Cursor::summary` 用 `SummarySeekAggregate` 只做维度叠加、不建树，适合「区间内有多少 / 区间和是多少」这类聚合查询；输出维度与游标寻路维度互相独立。
- 这三个入口共用同一个 seek 引擎，是访问者模式在性能敏感代码里的干净落地。
- `End` 是恒返回 `Ordering::Greater` 的哨兵目标，模块私有、经 `suffix()` 暴露；`suffix` 把当前位置到树尾的全部内容收进结果树。
- 切点的 Bias 决定边界元素归属：配 `Right` 归左段、配 `Left` 归右段；`test_random` 的 splice 用两次 Right 切出半开区间。

## 7. 下一步学习建议

- **下一讲（u3-l4）**：`FilterCursor` 与 `search_forward`/`search_backward`——另一种「边走边做」的遍历：按节点 summary 整棵剪枝的过滤游标，与本讲的聚合器思路互补。
- **向后看（u4 单元）**：本讲的 `slice` 产出的树全部靠 `append` 拼装，u4-l1/u4-l2 将精读 `append`/`push_tree_recursive`/`append_large` 如何在拼接时维护 B+ 树的平衡不变量——那是「slice 为什么能这么便宜」的另一半答案。
- **源码延伸阅读**：带着「区间聚合」的视角去翻 `crates/rope/src/rope.rs` 中对 `Cursor::summary` 与 `slice` 的调用点，看看编辑器是如何用一个游标同时完成定位、取摘要与切片的。

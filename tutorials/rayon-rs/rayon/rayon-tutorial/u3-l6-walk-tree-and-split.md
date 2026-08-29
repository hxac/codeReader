# 树形遍历与通用切分（u3-l6）

## 1. 本讲目标

学完本讲，你应该能够：

1. 使用 `walk_tree` / `walk_tree_prefix` / `walk_tree_postfix` 把任意树形结构（甚至只是"逻辑上的树"）表达成并行迭代器。
2. 理解 `rayon::iter::split` 如何**按值**（而不是按下标）切分数据，让任何"知道怎么把自己对半分"的类型获得并行能力。
3. 读懂 `bridge_unindexed` 背后的分治递归（divide and conquer）：切分 → `join_context` 双分支 → `reduce` 归并，以及"切分预算 + 被窃取重置"的自适应策略。
4. 对同一棵树分别用 `join` 手写递归、`iter::split`、`walk_tree` 三种方式并行统计节点总数，验证结果一致并对比代码量。

本讲是单元三的收尾：前几讲的适配器（map、fold、zip、flatten）都建立在"已经有一个并行源"之上，本讲回答的是**最后一类问题——当数据本身不是数组/范围，而是一棵树、一个二维区域、任意可分裂的结构时，并行源从哪里来**。

## 2. 前置知识

阅读本讲前，请确认你理解以下概念（来自前面的讲义）：

- **有索引 vs 无索引**（u2-l1、u3-l3）：`IndexedParallelIterator` 知道长度、能按下标 `split_at(mid)` 精确二分；`ParallelIterator` 只能"在某个地方"分裂。树没有 `usize` 长度，天然属于后者。
- **plumbing 三角色**（u3-l1）：Producer 生产元素、Consumer 消费元素、Folder 逐个吞元素。本讲只需要其中的 `UnindexedProducer`。
- **工作窃取与切分预算**（u1-l1、u3-l3）：rayon 不会无限切分，切分次数约等于线程数，任务被偷走时预算会重置。
- **`join`**（u1-l1）：两个闭包，第二个先入队、第一个先本地执行，空闲线程可窃取入队的那个。本讲的分治递归最终就落到 `join_context` 上。

一个形象的比喻：`Producer::split_at(index)` 像切蛋糕——先量好尺寸再下刀；`UnindexedProducer::split()` 像分家产——把东西直接掰成两堆，每堆自己继续掰。切片走前一条路，树和任意结构只能走后一条路。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/iter/walk_tree.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/walk_tree.rs) | 本讲主角一：`walk_tree_prefix` / `walk_tree_postfix` / `walk_tree` 三个树遍历入口及其 Producer 实现 |
| [src/iter/splitter.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/splitter.rs) | 本讲主角二：`rayon::iter::split()` 函数与 `Split` 迭代器、`SplitProducer`（按值切分） |
| [src/iter/plumbing/mod.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs) | `UnindexedProducer` trait、`Splitter` 切分预算、`bridge_unindexed` 分治递归 |
| [src/iter/mod.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs) | 模块声明与导出：`mod splitter;`（L149）、`mod walk_tree;`（L160）、导出 `Split, split`（L199）与 walk_tree 家族（L206-208） |
| [src/iter/test.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/test.rs) | `check_split` 测试（split + flat_map 还原范围）、scope 版 `divide_and_conquer` 辅助函数 |
| [src/split_producer.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/split_producer.rs) | **命名陷阱澄清**：这是 `&str` / `&[T]` 按"分隔符"切分的共享生产器（`Fissile` trait），供 `src/str.rs` 与 `src/slice/mod.rs` 使用，与本讲的按值切分无关 |
| [src/math.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/math.rs) | **澄清**：只含 `simplify_range`（把 `RangeBounds` 归一化为 `Range`，供 vec/string/vec_deque 的 drain 类 API 使用）。切分的"对半数学"并不在此文件，而在各 Producer 自己的 `split` 里 |

两个容易踩的坑，先说清楚：

1. **`src/iter/splitter.rs` ≠ `src/split_producer.rs`**。前者是本讲的 `iter::split`（按值对半分），后者是 `par_split` 那类"按分隔符切字符串/切片"的私有工具（见 [src/split_producer.rs:1-16](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/split_producer.rs#L1-L16) 的模块注释 "Common splitter for strings and slices"）。文件名几乎互为镜像，搜索时务必看清路径。
2. **`Split` 是结构体不是 trait**。学习大纲里提到的"Split trait"在当前源码中的实际形态是：`iter::split()` **函数** + `Split<D, S>` **迭代器结构体**（[src/iter/splitter.rs:105-119](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/splitter.rs#L105-L119)）。你不需要 impl 任何 trait，只要提供一个"切分闭包"即可。

## 4. 核心概念与源码讲解

### 4.1 walk_tree 模式

#### 4.1.1 概念说明

树是最经典的"天然可分"结构：一棵树 = 根 + 若干子树，每个子树又是一棵树。串行世界用递归（或显式栈）遍历它；并行世界的问题是——**递归的展开速度取决于树的形状，切分点在编译期不可知**，所以它没法实现 `Producer::split_at(index)`，只能实现"把我掰成两半"的 `UnindexedProducer::split()`。

`walk_tree` 家族把这件事做成了开箱即用的 API。它的抽象非常巧妙：你不需要真的拥有一棵 `struct Tree`，只需要提供：

- 一个**初始状态** `root: S`（`S` 可以是树节点、下标、游戏棋盘……任何东西）；
- 一个**孩子函数** `children_of: Fn(&S) -> I`，告诉 rayon"这个状态的子状态有哪些"。

rayon 就把"由 `children_of` 递归定义的虚拟树"当成并行数据源遍历。三个变体的区别只在**顺序保证**：

| 入口 | 顺序保证 | 典型场景 |
| --- | --- | --- |
| `walk_tree_prefix` | 前序：父先于子 | 依赖父节点先处理的计算 |
| `walk_tree_postfix` | 后序：子先于父 | 合并子结果后再算父（如下而上求和） |
| `walk_tree` | 无保证，选当前认为最快的策略 | 只关心结果总量、不关心顺序 |

`S: Send`（状态要能在线程间搬运），`children_of` 要 `Send + Sync`（会被多个工作线程共享调用）。注意一个细节：`walk_tree` 与 `walk_tree_prefix` 额外要求孩子的迭代器是 `DoubleEndedIterator`（原因见 4.1.3 第 3 点）。

#### 4.1.2 核心流程

以 `walk_tree_prefix` 的 Producer 为例，它内部维护两个栈：

```text
to_explore: Vec<S>   # 待探索的子树根（栈，从尾部弹出）
seen:       Vec<S>   # 已经探索过（已产出）的节点

split() 流程：
1. 只要 to_explore 里只剩 1 个元素，就弹出它、把它的孩子逆序压栈、
   把它自己记入 seen —— 沿着单链一路下探，直到出现分叉（≥2 个前沿节点）
2. split_vec(&mut to_explore)：把前沿对半分，右生产者拿走一半、
   自己留下另一半 —— 两个 Producer 从此各管一摊，互不重叠
3. 若前沿分不动（退化为纯链、全在 seen 里），退而对 seen 对半分

fold_with() 流程（前序）：
1. 先把 seen 里的节点灌给下游（它们先于所有待探索节点）
2. 循环：弹出 to_explore 栈顶 e → 孩子逆序压栈 → 把 e 交给 folder
3. folder.full() 时提前收工（配合 find_any 等短路消费者）
```

"对半分前沿"本质上是**把 DFS 的栈切一半给别人**：切走的那半栈对应的子树集合与留下的那半互不相交，所以两个生产者产出的节点集合恰好 partition 整棵树，这正是 `UnindexedProducer::split()` 的契约——分完之后两边各自完整可用。

为什么前序要用"逆序压栈"？因为栈从**尾部**弹出。孩子按正序产生、若正序压栈则最后生成的孩子会被先处理；`.rev()` 反转后压栈，`pop()` 取出的恰好是第一个孩子，兄弟顺序得以保持。

#### 4.1.3 源码精读

**1. Producer 的两个栈与 trait 约束**

[src/iter/walk_tree.rs:4-16](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/walk_tree.rs#L4-L16) 定义了 `WalkTreePrefixProducer`：`to_explore` 是待处理的前沿，`seen` 是已访问节点，`children_of` 是借用来的孩子函数。注意约束 `I: IntoIterator<Item = S, IntoIter: DoubleEndedIterator>`——前序实现需要能反转孩子迭代器。

**2. split：先下探单链，再对半分前沿**

[src/iter/walk_tree.rs:19-48](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/walk_tree.rs#L19-L48) 是核心切分逻辑：

- L21-26 的 `while self.to_explore.len() == 1` 循环：前沿只有一个节点时无从分裂，弹出它、孩子 `.rev()` 压栈、节点进 `seen`，相当于沿链下探到分叉处；
- L28 调用 `split_vec` 拿走一半前沿；
- L29-37 构造右生产者：`std::mem::swap` 交换后，右侧拿到 `to_explore` 的一半、`seen` 清空（`seen` 里的节点仍归左侧产出，保证前序中它们先出现）；
- L38-46 的 `or_else` 分支：前沿分不动时，连 `seen` 也可以对半分（应对链状退化树）。

辅助函数 [src/iter/walk_tree.rs:326-336](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/walk_tree.rs#L326-L336)：长度 ≤ 1 返回 `None`（不可分），否则 `split_off(len/2)` 对半。

**3. fold_with：前序的栈式探索**

[src/iter/walk_tree.rs:50-69](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/walk_tree.rs#L50-L69)：先 `consume_iter(self.seen)` 把已访问节点交给下游，再循环 `pop()` 前沿、孩子 `.rev()` 压栈（L60-62）、`folder.consume(e)` 产出节点。每次产出后检查 `folder.full()`（L64），这让 `walk_tree_prefix(...).find_any(...)` 这类短路操作能提前停。也解释了 4.1.1 提到的 `DoubleEndedIterator` 约束——`.rev()` 只在双端迭代器上可用。

**4. 迭代器本体与入口函数**

`WalkTreePrefix` 结构体只存 `initial_state` 和 `children_of`（[src/iter/walk_tree.rs:74-78](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/walk_tree.rs#L74-L78)）；它的 `drive_unindexed`（[src/iter/walk_tree.rs:88-98](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/walk_tree.rs#L88-L98)）把根节点装进 `to_explore`、交给 `bridge_unindexed` 启动分治（见 4.3）。公开入口 [src/iter/walk_tree.rs:203-213](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/walk_tree.rs#L203-L213) 只是个构造器。

文档给出了顺序保证的直观例子（[src/iter/walk_tree.rs:106-124](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/walk_tree.rs#L106-L124)）：7 节点完美二叉树前序消元为 `a,b,d,e,c,f,g`；后序（[src/iter/walk_tree.rs:343-363](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/walk_tree.rs#L343-L363)）为 `d,e,b,f,g,c,a`。

**5. 后序变体的两点不同**

- `fold_with`（[src/iter/walk_tree.rs:265-279](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/walk_tree.rs#L265-L279)）不维护栈，而是对前沿每个元素调用 `consume_rec_postfix` **递归**展开（[src/iter/walk_tree.rs:281-295](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/walk_tree.rs#L281-L295)：先递归所有孩子、最后 `folder.consume(s)` 消费自己），结束时才把 `seen` **逆序**灌给下游——祖先最后产出；
- `split`（[src/iter/walk_tree.rs:232-263](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/walk_tree.rs#L232-L263)）里 `std::mem::take(&mut self.seen)` 把已访问节点**整体交给右生产者**，源码注释 `// postfix -> upper nodes are processed last`（L243）点明动机：`join` 先跑左侧，右侧持有祖先且最后消费，全局后序才成立。

**6. `walk_tree`：无序变体是后序的新类型包装**

[src/iter/walk_tree.rs:454-457](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/walk_tree.rs#L454-L457) 定义 `pub struct WalkTree<S, B>(WalkTreePostfix<S, B>)`；入口 [src/iter/walk_tree.rs:495-506](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/walk_tree.rs#L495-L506) 构造后序实现再包一层，`ParallelIterator` 实现（[src/iter/walk_tree.rs:508-522](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/walk_tree.rs#L508-L522)）直接转发 `drive_unindexed`（impl 上多了一个 `I: Send` 约束）。今天"最快"的选择就是后序；这个新类型留出了将来换策略的余地。

三个入口都从 `rayon::iter` 导出（[src/iter/mod.rs:206-208](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L206-L208)），用法是自由函数：`use rayon::iter::walk_tree;`。

#### 4.1.4 代码实践

**实践目标**：直观感受三个变体的顺序差异，确认"虚拟树"用法（`S` 不必是真树节点）。

**操作步骤**（以下为示例代码，新建独立 Cargo 工程，`Cargo.toml` 加 `rayon = "1"`）：

```rust
use rayon::iter::{walk_tree, walk_tree_postfix, walk_tree_prefix};

fn main() {
    // 例 1：S 是 u32，树由函数隐式定义（来自官方文档的例子）
    //        4
    //       / \
    //      2   3
    //         / \
    //        1   2
    let children = |&e: &u32| {
        if e <= 2 { Vec::new() } else { vec![e / 2, e / 2 + 1] }
    };
    let pre: Vec<u32> = walk_tree_prefix(4u32, children).collect();
    let post: Vec<u32> = walk_tree_postfix(4, children).collect();
    let any: Vec<u32> = walk_tree(4, children).collect();
    println!("prefix  : {:?}", pre);
    println!("postfix : {:?}", post);
    println!("walk    : {:?}", any);
    assert_eq!(walk_tree_prefix(4, children).sum::<u32>(), 12);

    // 例 2：真树节点也能走 & 引用（官方文档第二个例子的结构）
    // struct Node { content: u32, left: Option<Box<Node>>, right: Option<Box<Node>> }
    // walk_tree_prefix(&root, |r| r.left.as_ref().into_iter()
    //     .chain(r.right.as_ref()).map(|n| &**n))
    //     .map(|node| node.content)
    let _ = any;
}
```

**需要观察的现象**：`prefix` 与 `postfix` 的输出顺序稳定；`walk` 的顺序可能与 postfix 相同，但文档不保证。

**预期结果**：`prefix` 输出 `[4, 2, 3, 1, 2]`（父先于子），`postfix` 输出 `[2, 1, 2, 3, 4]`（子先于父），两者之和均为 12。`walk` 的具体顺序待本地验证（当前实现转发后序，通常与 postfix 一致）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `walk_tree_prefix` 要求 `IntoIter: DoubleEndedIterator`，而 `walk_tree_postfix` 不要求？

**答案**：前序用显式栈实现——孩子必须 `.rev()` 后压栈，`pop()` 从尾部弹出时才能先得到第一个孩子（[src/iter/walk_tree.rs:24](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/walk_tree.rs#L24) 与 [L62](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/walk_tree.rs#L62) 两处 `.rev()`），反转迭代器需要 `DoubleEndedIterator`；后序的 `fold_with` 按正序 `for` 循环递归（[L270](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/walk_tree.rs#L270)），不需要反转。

**练习 2**：`WalkTreePrefixProducer::split` 里，`to_explore` 被对半分后，右侧新生产者的 `seen` 为什么是空的 `Vec::new()`？

**答案**：`seen` 里的节点在前序中必须**先于**所有待探索节点产出。切分后左侧继续持有这些节点并在 `fold_with` 开头先消费它们（[src/iter/walk_tree.rs:55](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/walk_tree.rs#L55)）；而 `join` 先执行左侧，于是全局前序成立。若把 `seen` 分给右侧，这些节点会排到左侧待探索节点之后，顺序就错了。对比后序：`seen` 恰恰要给右侧（祖先最后产出）。

**练习 3**：如果树的形状是一条单链（每个节点只有一个孩子），`split` 还能分出两个生产者吗？

**答案**：前沿永远只有 1 个元素，`while` 循环会一路下探把所有节点搬进 `seen`、`to_explore` 变空，`split_vec` 对空前沿返回 `None`；随后走 `or_else` 分支对 `seen` 对半分（[src/iter/walk_tree.rs:38-46](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/walk_tree.rs#L38-L46)）。所以仍能切分，但切分的是"已经算完的部分"，并行收益有限——树越平衡，walk_tree 并行度越高，这也是文档强调"balanced tree 最佳"的原因。

### 4.2 Split：iter::split 按值切分

#### 4.2.1 概念说明

`walk_tree` 专门服务树；`rayon::iter::split` 则是**最通用的按值切分入口**：你交给它任意数据 `D: Send` 和一个切分函数，它返回一个 `ParallelIterator<Item = D>`——注意 **Item 就是数据片本身**，迭代产出的是"切分结束后的一堆碎片"。

切分函数的签名是关键：

```rust
S: Fn(D) -> (D, Option<D>) + Sync
```

含义：吃进一份数据，返回"留下的左半"和"可选的右半"。返回 `None` 表示"这块分不动了，它就是最终碎片"。rayon 负责**何时调用**它（调用多少次由切分预算决定，见 4.3），你只负责**怎么分**。

把它与已有知识对照：

| | `Producer::split_at(index)`（u3-l3） | `UnindexedProducer::split()`（本讲） |
| --- | --- | --- |
| 切分依据 | 下标（数据有长度） | 值（数据自己知道怎么掰） |
| 适用 | 切片、范围、zip 后的复合生产者 | 树、二维区域、任意递归结构 |
| 驱动函数 | `bridge`（配合 `LengthSplitter`） | `bridge_unindexed`（配合 `Splitter`） |

再次强调：**`Split` 是结构体、`split` 是函数**（[src/iter/splitter.rs:105-119](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/splitter.rs#L105-L119)），没有 `Split` trait。别与 `src/split_producer.rs` 里按分隔符切字符串的实现混淆（见第 3 节的命名陷阱）。

#### 4.2.2 核心流程

```text
iter::split(data, splitter) 的执行模型：

1. 消费者（sum/for_each/collect…）触发 drive_unindexed
2. 构造 SplitProducer { data, splitter }，进入 bridge_unindexed 递归
3. 递归中每次先问 Splitter 预算：还有得分就调 splitter(data)
   - 返回 (left, Some(right))：右半成为新 SplitProducer，
     join_context 同时递归两半，结果用 Reducer 归并
   - 返回 (left, None)：left 整体作为一个 Item 交给下游
4. 预算耗尽：不再切分，fold_with 直接把 data 作为一个 Item 消费

所以最终 Item 数 ≈ 2^ceil(log2(线程数)) 量级（不是元素数！）
```

典型用法是两段式：`split(...)` 负责把大问题分成若干块，`.map(串行处理一块)` 在块内用普通串行代码收尾。这正是分治算法的并行化模板——官方文档用"递归切分一维/二维索引范围"演示了这一点（[src/iter/splitter.rs:12-103](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/splitter.rs#L12-L103)）：二维例子按"较长的一维"切分，复现了图形学与数值模拟里经典的块状并行。

#### 4.2.3 源码精读

**1. 入口函数与迭代器结构体**

[src/iter/splitter.rs:105-119](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/splitter.rs#L105-L119)：`pub fn split<D, S>(data: D, splitter: S) -> Split<D, S>`，约束 `D: Send`、`S: Fn(D) -> (D, Option<D>) + Sync`。`Split` 结构体只有 `data` 和 `splitter` 两个字段，可 `Clone`。

**2. ParallelIterator 实现**

[src/iter/splitter.rs:127-144](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/splitter.rs#L127-L144)：`type Item = D`——产出的是数据片；`drive_unindexed` 把 `data` 连同 `&splitter` 装进 `SplitProducer`，交给 `bridge_unindexed`。

**3. SplitProducer：最简 UnindexedProducer**

[src/iter/splitter.rs:146-171](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/splitter.rs#L146-L171) 是本讲最值得背诵的 20 行：

- `split`（L158-163）：调用用户的切分函数，`self.data = left`，右半包成新的 `SplitProducer` 返回。整个"按值切分"的语义就浓缩在这里——不碰下标，只交换所有权；
- `fold_with`（L165-170）：只有一行 `folder.consume(self.data)`——一片碎片就是一个 Item。

对比 `WalkTreePrefixProducer`（两个栈 + 递归下探）就能看出 `UnindexedProducer` 契约的弹性：只要"能掰两半、能自己产出"，实现可以极简也可以很讲究。

**4. 官方测试：split + flat_map 还原范围**

[src/iter/test.rs:1670-1687](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/test.rs#L1670-L1687) 的 `check_split`：把 `0..1024` 按 `split` 递归对半，再 `flat_map(|range| range)` 展平成元素，断言与 `(0..1024).into_par_iter().collect()` 完全一致。这个测试同时验证了两件事：碎片恰好 partition 原数据（不重不漏）、`.rev()` 无关的顺序拼接连线正确。

#### 4.2.4 代码实践

**实践目标**：观察 `iter::split` 的"碎片数量与形状"如何随线程数变化，验证切分预算的行为。

**操作步骤**（示例代码）：

```rust
use rayon::iter::split;
use rayon::prelude::*;

fn main() {
    let pieces: Vec<std::ops::Range<usize>> =
        split(0..4096usize, |r| {
            if r.end - r.start <= 1 { return (r, None); }
            let mid = r.start + (r.end - r.start) / 2;
            (r.start..mid, Some(mid..r.end))
        })
        .collect();

    println!("pieces = {}", pieces.len());
    println!("sizes  = {:?}", pieces.iter().map(|r| r.end - r.start).collect::<Vec<_>>());

    // 完整性验证（思路同 check_split 测试）
    let sum: usize = split(0..4096, |r| {
        if r.end - r.start <= 1 { return (r, None); }
        let mid = r.start + (r.end - r.start) / 2;
        (r.start..mid, Some(mid..r.end))
    })
    .flat_map(|r| r)
    .sum();
    assert_eq!(sum, (0..4096usize).sum());
    println!("sum ok");
}
```

分别在 `RAYON_NUM_THREADS=1`、`=4`、`=16` 环境下运行（例如 `RAYON_NUM_THREADS=4 cargo run --release`）。

**需要观察的现象**：碎片数量与每片大小随线程数变化；碎片数是 2 的幂附近的值。

**预期结果**：碎片数大致为不小于线程数的最小 2 的幂（如 4 线程约 4~8 片，每片 512~1024 个元素），总和恒等于 4096、`sum ok` 始终打印。精确片数受窃取重置影响，待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：用 `iter::split` 遍历一棵二叉树并 `.count()`，得到的是节点总数吗？

**答案**：不是。`Split` 的 Item 是**碎片**而非节点：只要切分函数还能分，内部节点就被切分函数"消费"掉了，只有最终不可再分的碎片才会成为 Item。`.count()` 数出的是碎片数（约等于切分叶数）。要统计节点总数，应对每个碎片串行计数再求和：`split(...).map(|piece| serial_count(piece)).sum()`（见第 5 节综合实践）。

**练习 2**：切分闭包的约束是 `Fn(D) -> (D, Option<D>) + Sync`，为什么是 `Fn`（不能是 `FnMut`）？

**答案**：切分函数会被多个工作线程**通过共享引用**并发调用（`SplitProducer` 持有 `&self.splitter`，见 [src/iter/splitter.rs:146-149](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/splitter.rs#L146-L149)），`Fn`（不改变捕获状态）+ `Sync`（可跨线程共享）是并发的最小要求；`FnMut` 允许修改捕获状态，无法安全地共享。

**练习 3**：`splitter` 返回的二元组里，"留在 self 里的是哪一半"影响语义吗？

**答案**：对**结果集合**无影响（两半终会被遍历），但可能影响**产出顺序**：`bridge_unindexed` 先递归左侧（见 4.3），左侧的碎片会先进入下游。`check_split` 能按序拼出 `0..1024`，正是因为其切分函数把"前半"留在左侧（`start..mid` 为左、`mid..end` 为右）。

### 4.3 divide_and_conquer：分治递归的统一驱动

#### 4.3.1 概念说明

walk_tree 和 iter::split 是两个"前端"，它们共同的"后端"是 `bridge_unindexed` 里的分治递归。理解了这段递归，你就理解了 rayon 中**一切无索引并行**的骨架：

```text
divide_and_conquer(problem):
    if 预算还够 且 problem 可分:
        (left, right) = problem.split()
        (r1, r2) = join_context(divide_and_conquer(left),
                                divide_and_conquer(right))
        return reducer.reduce(r1, r2)
    else:
        return problem.fold_with(consumer)
```

与 u3-l3 学过的 `bridge`（indexed 版）相比，区别只有两处：切分依据从"下标中点 + LengthSplitter"换成了"数据自决 + Splitter"；`LengthSplitter` 额外看 `min/max_len`，`Splitter` 只看线程数与窃取信号。

"预算 + 窃取重置"是这段代码的灵魂（u3-l3 已见过 indexed 版）：初始预算 = 当前线程数；每分一刀预算减半；一旦发现本任务是从别的线程**偷来的**（`context.migrated()`），预算重置回线程数——因为窃取说明还有空闲工人，多切几刀才能喂饱他们。这让递归深度自动适应机器负载，理论的并行期望时间 \( T_P \lesssim W/P + O(S) \)（W 为总工作量、P 为线程数、S 为生成跨度，即 u1-l1 提过的 Cilk 式结论）在实践中近似成立。

值得说明：**仓库中没有独立的 `divide_and_conquer` 源码模块**——它就是 `bridge_unindexed_producer_consumer` 这个函数；同名辅助函数只出现在测试里（[src/iter/test.rs:1660-1668](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/test.rs#L1660-L1668)，用 `scope::spawn` 手写分治数叶子数，用于混合场景测试）。

#### 4.3.2 核心流程

```text
bridge_unindexed(producer, consumer):
    splitter = Splitter::new()          # 预算 = current_num_threads()
    递归(producer, consumer, splitter):

递归体 bridge_unindexed_producer_consumer(migrated, splitter, producer, consumer):
    1. consumer.full()？ → 提前完成（短路消费者已满足）
    2. splitter.try_split(migrated) 为假？ → 预算耗尽：
       producer.fold_with(consumer.into_folder()).complete()
    3. producer.split()：
       - (left, Some(right))：
           reducer   = consumer.to_reducer()
           left_c    = consumer.split_off_left()   # 消费者也一分为二
           (r1, r2)  = join_context(
               |ctx| 递归(left,  left_c,  splitter, ctx.migrated()),
               |ctx| 递归(right, consumer, splitter, ctx.migrated()))
           return reducer.reduce(r1, r2)
       - (producer, None) → 不可分：直接 fold_with 完成
```

注意消费者侧的配合：每切一刀，消费者也调 `split_off_left()` 克隆出一份给自己包到左分支（这就是 u3-l1 "包装消费者"模式在消费端的原生形态），两个分支各自完整，最后由 `Reducer` 把两个部分结果合并。

#### 4.3.3 源码精读

**1. UnindexedProducer 契约**

[src/iter/plumbing/mod.rs:223-243](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L223-L243)：文档注释写得很清楚——这类生产者"不知道精确长度、也不能在指定点切分，你只能请它'在某个地方'分一下"。两个必需方法：`split`（L236，分不出时右半为 `None`）和 `fold_with`（L240，把元素灌给 folder，folder 满了可提前停）。

**2. Splitter：预算与窃取重置**

[src/iter/plumbing/mod.rs:245-284](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L245-L284)：`Splitter::new()` 把 `splits` 初始化为 `crate::current_num_threads()`（L258-264）；`try_split`（L267-283）的三分支——被窃取则重置为 `max(线程数, splits/2)` 并放行（L270-274），有余额则减半放行（L275-278），否则拒绝（L279-282）。字段注释（L252-254）说明由于每次都是减半，实际碎片数是线程数的**下一个 2 的幂**。

**3. bridge_unindexed 与递归体**

[src/iter/plumbing/mod.rs:437-445](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L437-L445)：入口构造 `Splitter` 后进入递归。递归体 [src/iter/plumbing/mod.rs:447-476](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L447-L476)：

- L457-458：消费者已满则立刻收工（短路支持）；
- L459：预算判断，配合 L473-474 的兜底 `fold_with`；
- L460-471：核心分治——`producer.split()` 拿到右半后，`consumer.to_reducer()` + `consumer.split_off_left()` 拆分消费者，`join_context` 的两个闭包里用 `context.migrated()` 把"本分支是否被窃取"继续向下传（L465-468），最后 `reducer.reduce(left_result, right_result)`（L469）合并；
- L471：生产者表示"分不动"（返回 `None`）时，直接整块消费。

**4. 测试里的手写分治**

[src/iter/test.rs:1644-1668](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/test.rs#L1644-L1668)：`scope_mix` 测试中定义的 `divide_and_conquer` 辅助函数用 `scope.spawn` 递归二分 1024、在叶子处给 `AtomicUsize` 计数——这是**任务级**的分治（不经过迭代器），与 `bridge_unindexed` 的**数据级**分治互为镜像，读对照能加深理解。

#### 4.3.4 代码实践

**实践目标**：把 `bridge_unindexed_producer_consumer` 的递归画成图，并用可观察现象验证"预算 ≈ 线程数"。

**操作步骤**：

1. 对照 [src/iter/plumbing/mod.rs:447-476](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L447-L476)，手绘递归树：一个节点代表一次函数调用，标出它走"继续分"还是"fold_with 收尾"分支的条件；
2. 在 4.2.4 实践程序的基础上，分别在 `RAYON_NUM_THREADS=1`、`=4`、`=16` 下记录碎片数；
3. 阅读并运行测试：`cargo test -p rayon --test -- skip 2>/dev/null || cargo test -p rayon check_split`（更稳妥的命令是直接 `cargo test -p rayon iter::test::check_split --verbose`，具体过滤写法待本地验证）。

**需要观察的现象**：线程数翻倍时，碎片数是否按 2 的幂阶梯上升（1→1、4→4~8、16→16~32），而总工作量不变。

**预期结果**：碎片数阶梯与 `Splitter` 注释的 `next_power_of_two` 规律吻合；`check_split` 测试通过。实际数值因窃取重置存在抖动，待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`try_split` 在被窃取时为什么重置为 `max(current_num_threads(), splits / 2)` 而不是简单恢复为线程数？

**答案**：`splits / 2` 保留了"本分支此前已经切过的深度信息"——如果先前因为长树切出了很深的预算，取 max 能保住这部分已获得的更细粒度不丢失；而直接重置为线程数可能在深度递归场景下反而变粗。核心动机是：窃取发生 = 还有空闲线程 = 多切几刀值得。

**练习 2**：递归体里 `join_context`（而不是 `join`）被使用，`context.migrated()` 传下去的是什么信息？

**答案**：`migrated` 表示"当前这个分支的闭包是否是被别的线程窃取后执行的"。它一路透传给下层递归的 `Splitter::try_split`（[src/iter/plumbing/mod.rs:465-468](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L465-L468)），用于触发预算重置。`join` 做不到这一点，因为它不向闭包暴露调度上下文（u5-l1 会详细讲 `join_context`）。

**练习 3**：为什么 `bridge_unindexed` 每一刀都要 `consumer.split_off_left()`，而 `fold_with` 收尾时不用？

**答案**：切分后左右两分支会在不同线程并发消费，必须各持一份（状态独立的）消费者，否则共享可变状态就是数据竞争；`fold_with` 收尾意味着不再切分，这个分支独占当前消费者，直接 `consumer.into_folder()` 逐元素消费即可。

## 5. 综合实践

**任务**：定义一个二叉树类型，用**三种方式**并行统计节点总数，验证结果一致并对比代码量——这是对本讲三个模块（walk_tree、iter::split、分治驱动）的总串联。

**准备**：新建独立 Cargo 工程，`Cargo.toml` 添加 `rayon = "1"`，以下为完整示例代码（`src/main.rs`）：

```rust
use rayon::iter::{split, walk_tree_prefix};
use rayon::prelude::*;
use rayon::join;

#[derive(Debug)]
struct Tree {
    #[allow(dead_code)]
    value: u32,
    left: Option<Box<Tree>>,
    right: Option<Box<Tree>>,
}

fn build(depth: u32) -> Option<Box<Tree>> {
    if depth == 0 {
        None
    } else {
        Some(Box::new(Tree {
            value: depth,
            left: build(depth - 1),
            right: build(depth - 1),
        }))
    }
}

// ── 方式一：join 手写分治递归（分治的最原始形态）──────────────
fn count_join(t: &Tree) -> u32 {
    let (l, r) = join(
        || t.left.as_deref().map_or(0, count_join),
        || t.right.as_deref().map_or(0, count_join),
    );
    1 + l + r
}

// ── 方式二：iter::split 按值切分 + 片内串行计数 ────────────────
fn split_tree(t: &Tree) -> (&Tree, Option<&Tree>) {
    match (t.left.as_deref(), t.right.as_deref()) {
        (Some(l), Some(r)) => (l, Some(r)),
        (Some(only), None) | (None, Some(only)) => (only, None),
        (None, None) => (t, None), // 叶子：自身就是最终碎片
    }
}

fn count_serial(t: &Tree) -> u32 {
    1 + t.left.as_deref().map_or(0, count_serial)
        + t.right.as_deref().map_or(0, count_serial)
}

// ── 方式三：walk_tree_prefix，孩子函数驱动 ────────────────────
fn children(t: &&Tree) -> Vec<&Tree> {
    t.left
        .as_deref()
        .into_iter()
        .chain(t.right.as_deref())
        .collect()
}

fn main() {
    let root = Tree { value: 0, left: build(14), right: build(14) };
    // 完美二叉树：深度 14 的两棵子树 + 根 = 2 * (2^15 - 1) + 1 = 65535
    let expected = 2 * (2u32.pow(14 + 1) - 1) + 1;

    let c1 = count_join(&root);
    let c2 = split(&root, split_tree).map(count_serial).sum::<u32>();
    let c3 = walk_tree_prefix(&root, children).map(|_| 1u32).sum::<u32>();

    println!("join      : {c1}");
    println!("iter split: {c2}");
    println!("walk_tree : {c3}");
    assert_eq!((c1, c2, c3), (expected, expected, expected));
    println!("all equal: {expected}");
}
```

**操作步骤**：

1. 用 `cargo run --release` 运行，确认三种方式结果一致且等于公式值；
2. 对比三种实现的行数：`count_join` 约 7 行、方式二约 15 行（切分函数 + 串行计数 + 一行管道）、方式三约 8 行（孩子函数 + 一行管道）——方式二最长，因为它必须**同时**回答"怎么分"和"分完的片怎么算"两个问题；
3. 把 `build(14)` 改成 `build(18)`，分别给三种方式用 `std::time::Instant` 计时（计时前先跑一次小规模预热线程池，结论参考 u1-l3）；
4. 选做：把方式三的 `walk_tree_prefix` 换成 `walk_tree`，观察计时差异（顺序不保证，但计数不变）。

**需要观察的现象**：三种计数完全相等；计时上三者同量级（都按树的分形结构切分），`walk_tree` 版本通常与 prefix 接近。

**预期结果**：打印 `all equal: 65535`；三种方式代码量排序为 join < walk_tree < iter::split。性能对比的具体数字待本地验证。

**思考题**（不必写码）：如果把方式二的 `.map(count_serial).sum()` 换成 `.count()`，结果是多少？（答案见 4.2.5 练习 1：数的是碎片数而非节点数。）

## 6. 本讲小结

- 树与任意递归结构没有 `usize` 长度，无法走 `split_at(index)`，只能走 **按值切分** 的 `UnindexedProducer::split()`——"把我掰成两半，两边各自完整"。
- `walk_tree` 家族用"初始状态 + 孩子函数"定义**虚拟树**：prefix 用双栈（`to_explore`/`seen`）实现前序、postfix 用递归实现后序并把祖先交给右侧最后产出、无序变体当前只是 postfix 的新类型包装。
- `rayon::iter::split` 是最通用的按值切分入口：`Fn(D) -> (D, Option<D>)` 描述"怎么分"，**Item 是碎片本身**，块内处理通常交给串行代码，这是分治算法并行化的标准模板（注意：`Split` 是结构体不是 trait）。
- 两个前端共用一个后端：`bridge_unindexed` 的分治递归（split → `join_context` → `reduce`），`Splitter` 预算从线程数起步、逐刀减半、被窃取时重置，使切分粒度自适应负载。
- 命名陷阱：`src/iter/splitter.rs` 是本讲的 `iter::split`；`src/split_producer.rs` 是字符串/切片按**分隔符**切分的另一套私有工具；`src/math.rs` 只是范围归一化辅助，切分逻辑不在其中。
- 仓库没有独立的 divide_and_conquer 模块——它就是 `bridge_unindexed_producer_consumer`；测试里的同名函数展示了任务级（scope::spawn）的镜像写法。

## 7. 下一步学习建议

本讲是单元三（适配器机制）的终点，也是通往内核的桥：`bridge_unindexed` 的递归最终落在 `join_context` 上，而它正是**单元五 rayon-core 调度内核**的入口。建议下一讲按依赖顺序学习：

- **u4-l1（plumbing 总览）**：把 Producer/Consumer/Folder/Reducer 的完整契约系统化，本讲只用了其中无索引的一半；
- **u5-l1（join 原语）**：本讲综合实践中手写的 `count_join` 将在内核层被解剖——闭包如何装箱成 Job、`join_context` 的 `migrated` 从哪里来；
- 想立即动手的读者，可以尝试给 4.1 的树加上节点权重，用 `walk_tree_postfix` 自底向上求"子树权重和"（后序正好匹配合并方向），再对照 [src/iter/walk_tree.rs:265-295](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/walk_tree.rs#L265-L295) 体会递归消费的实现。

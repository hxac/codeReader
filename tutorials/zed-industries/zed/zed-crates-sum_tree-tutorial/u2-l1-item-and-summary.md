# u2-l1 Item 与 Summary：为元素定义汇总

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `Item` trait 在 sum_tree 中的角色：它是「一个元素如何产出自己的汇总」的契约，是任何类型进入 `SumTree` 的入场券。
2. 说出 `Summary` trait 的两个方法 `zero` 与 `add_summary` 的语义，特别是 `add_summary` 的「按序列顺序单调叠加」约定。
3. 区分 `ContextLessSummary`（无环境汇总）与带 `Context<'a>` 的 `Summary`（有环境汇总），并理解 `Context<'a>` 存在的真实理由。
4. 知道 `NoSummary` 占位类型的用途，以及为什么它不直接复用 `()`。
5. 能照着测试模块中的 `IntegersSummary`（count / sum / contains_even / max 四个字段），为自己的类型完整实现 `Item` 与 `ContextLessSummary`。

本讲是整个 u2 单元的地基：下一讲的 `Dimension` 与 `SeekTarget`，以及 u3 的 `Cursor`，全部建立在本讲的两个 trait 之上。

## 2. 前置知识

阅读本讲前，你应当已经了解（u1 系列讲义的内容）：

- **B+ 树的基本形态**：元素只存在叶子节点，内部节点只存子树的路由信息。sum_tree 中每个节点容量上限是 `2 * TREE_BASE`（本 crate 测试构建下为 4，正式构建下为 12）。
- **Node 的结构**：叶子有 `items` 和平行的 `item_summaries` 两个数组；内部节点有 `child_summaries`（路由表）和 `child_trees`；两类节点都带一个整节点 `summary` 字段。
- **Arc 结构共享**：`SumTree<T>` 就是 `Arc<Node<T>>` 的包装，克隆廉价、修改走写时复制。

本讲新引入的两个名词，先用一句话建立直觉：

- **汇总（Summary）**：一个「能描述一串元素的可叠加的小对象」。比如「这 6 个数里有 37 个元素、总和是 200、最大值是 99」就是一个汇总。它必须能把两段拼接起来：左边一段的汇总 ⊕ 右边一段的汇总 = 拼接后的汇总。
- **可叠加（monoid 语义）**：数学上，这叫幺半群——有一个单位元 `zero`（空串的汇总），有一个满足结合律的二元运算 `add_summary`。sum_tree 的所有查询能力都来自这个性质。

为什么 B+ 树要配汇总？因为「从开头到某位置的总和」这类前缀查询，可以沿着根到叶的路径把沿途子树的汇总直接加起来，复杂度是 \( O(\log n) \)，而不是 \( O(n) \) 地数一遍元素。这就是 crate 名字里 "Sum" 的含义。

## 3. 本讲源码地图

本讲几乎全部内容集中在一个文件里：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `crates/sum_tree/src/sum_tree.rs` | crate 根模块：全部核心 trait 与 `SumTree` API | `Item`、`Summary`、`ContextLessSummary`、`NoSummary` 的定义（文件开头约 130 行），以及测试模块里的 `IntegersSummary` 全家桶（文件末尾） |
| `crates/rope/src/rope.rs` | Zed 的文本 rope，sum_tree 最重要的下游 | `ChunkSummary`——真实生产代码中 `ContextLessSummary` 的范本 |
| `crates/editor/src/display_map/crease_map.rs` | 编辑器折叠行的显示映射 | `ItemSummary`——真实生产代码中**带 `Context<'a>`** 的 `Summary` 范本，用来说明 `Context` 为什么必须存在 |

阅读建议：先精读 `sum_tree.rs` 开头 L31–L130 这一百行（四个 trait 定义挨在一起），再跳到文件末尾 L1820–L1902 看测试如何落地，最后扫一眼两个下游文件里的真实实现。

## 4. 核心概念与源码讲解

本讲的五个最小模块：**Item**、**Summary 与 ContextLessSummary**、**Context\<'a\> 的存在理由**、**NoSummary**、**IntegersSummary 范本**。

### 4.1 Item：元素进入树的入场契约

#### 4.1.1 概念说明

`Item` 回答的问题是：「一个类型 `T` 要存进 `SumTree<T>`，树应该怎么知道它『长什么样』？」

答案出奇地简单：类型自己声明一个关联的汇总类型，并且提供一个方法，把单个元素变成它的汇总。树拿到这个方法后，就能在构建时为每个元素、每个叶子、每个内部节点逐层算出汇总，之后的一切查询都只看汇总、不必再碰元素本身。

注意 `Item` 只要求 `Clone`，**不要求 `Ord`、不要求 `Debug`**。sum_tree 本身是「有序序列」而不是「有序集合」——它按插入顺序存放元素；只有当你要用 u4 会讲的 `KeyedItem` / `TreeMap` 时才需要键上的序。

#### 4.1.2 核心流程

一个类型获得 `Item` 实现后，进入树的路径是：

```text
item.summary(cx)          ← Item trait 提供的唯一方法，每个元素调用一次
        │
        ▼
叶子节点: item_summaries[i] = 第 i 个元素的汇总
          叶子整节点 summary = item_summaries 逐个 add_summary 折叠
        │
        ▼
内部节点: child_summaries[i] = 第 i 个子树的整树汇总
          节点整树 summary = child_summaries 逐个 add_summary 折叠
        │
        ▼
根: SumTree::summary() 直接返回根节点的 summary —— O(1) 拿到全树聚合值
```

写成数学形式，对任意节点 \( n \) 与它的孩子 \( c_1, \dots, c_k \)（叶子时孩子是各元素）：

\[ S(n) = S(c_1) \oplus S(c_2) \oplus \cdots \oplus S(c_k) \]

其中 \( \oplus \) 就是 `add_summary`。这个不变量由树的构建与修改代码负责维护，本讲 4.1.3 会指出维护它的具体代码行。

#### 4.1.3 源码精读

trait 定义本身只有四行：

> [crates/sum_tree/src/sum_tree.rs:L31-L38](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L31-L38)
> `Item` trait：关联类型 `type Summary: Summary` 声明「我这个元素用哪种汇总类型描述」，唯一的方法 `summary(&self, cx)` 把 `&self` 变成一个 `Self::Summary`。注意 `cx` 的类型是 `<Self::Summary as Summary>::Context<'_>`——汇总类型自己决定需要什么环境，4.3 节展开。

树的构建代码是消费这个方法的地方。看 `from_iter` 装叶子的一段：

> [crates/sum_tree/src/sum_tree.rs:L256-L272](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L256-L272)
> 每次从迭代器取 `2 * TREE_BASE` 个元素装进一个叶子：`items.iter().map(|item| item.summary(cx)).collect()` 为每个元素调用一次 `Item::summary`（第 260 行），然后从第一个元素汇总出发，把其余的逐个 `add_summary` 进去（第 262–265 行），得到叶子的整节点汇总。

再看向上组装父节点的一段：

> [crates/sum_tree/src/sum_tree.rs:L297-L300](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L297-L300)
> 对每个子树调用 `child_node.summary()` 拿到子树整树汇总，`add_summary` 折叠进父节点汇总，同时把一份克隆 push 进 `child_summaries` 路由表——这就是 u1-l2 讲过的「汇总存两份」冗余的写入点。

单个元素的快捷路径 `push` 也一样：

> [crates/sum_tree/src/sum_tree.rs:L768-L778](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L768-L778)
> `push` 就是「造一个只含一个元素的叶子再 `append`」：第 769 行调用 `item.summary(cx)`，同一份汇总既当叶子整节点 summary 又当 `item_summaries[0]`。

最后是读取端：

> [crates/sum_tree/src/sum_tree.rs:L736-L741](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L736-L741)
> `SumTree::summary()` 对 Internal / Leaf 两个变体做 match，都直接返回节点的 `summary` 字段——全树聚合值是 O(1) 可取的，因为构建时已经算好了。

#### 4.1.4 代码实践

**实践目标**：亲眼确认「每个元素只调用一次 `Item::summary`，且汇总的折叠发生在叶子与父节点两层」。

**操作步骤**（源码阅读型实践）：

1. 打开 [crates/sum_tree/src/sum_tree.rs:L249-L316](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L249-L316) 的 `from_iter`，数一数 `item.summary(cx)` 与 `add_summary` 各出现几次。
2. 对照 [crates/sum_tree/src/sum_tree.rs:L1269-L1281](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1269-L1281) 的 `Node` 枚举，确认 `item_summaries` 只出现在 Leaf 变体、`child_summaries` 只出现在 Internal 变体。
3. 用 `cargo doc -p sum_tree --no-deps --open`（在仓库根目录执行）查看 `Item` trait 的文档页，确认它只有一个方法。

**需要观察的现象**：`from_iter` 里元素级汇总只在叶子层产生一次；父节点层复用子树的整树汇总，绝不重新触碰元素。

**预期结果**：`Item::summary` 在整个文件的生产代码中只被 `from_iter`（L260）、`from_par_iter`（L332）、`push`（L769）三处调用——其余所有代码只操作已算好的汇总。待本地验证：你可以用编辑器搜索 `\.summary(cx)` 确认这三处。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Item` 不要求 `T: Ord`，而 u4 将讲的 `KeyedItem` 要求键有序？

**答案**：`SumTree<T>` 本质是持久化**有序序列**（ordered sequence，按插入顺序），不是有序集合；定位与切片靠的是 `Dimension`（下一讲）而非元素比较。只有 `TreeMap`/`edit` 这类需要按键查找的功能才要求键有序，所以这条约束被推迟到 `KeyedItem` 的关联类型 `Key` 上，让纯序列场景的元素类型零负担。

**练习 2**：`from_iter` 折叠叶子汇总时为什么从 `item_summaries[0].clone()` 出发（L262），而不是从 `Summary::zero(cx)` 出发？

**答案**：两者语义等价——`zero` 是折叠的单位元，从第一个元素开始折是省掉一次「单位元 ⊕ 第一个汇总」的等值变换的小优化。这也提示了一个隐含契约：`zero` 加任何汇总都必须得到该汇总自身，即 `zero` 必须是真正的单位元（`NoSummary`、`IntegersSummary` 的 `Default` 都满足）。

### 4.2 Summary 与 ContextLessSummary：可叠加的汇总

#### 4.2.1 概念说明

`Summary` trait 描述「一串元素的聚合描述」必须会两件事：

1. **产生单位元**：`zero(cx)` 返回「空串的汇总」。空树的节点上放的就是它。
2. **叠加**：`add_summary(&mut self, summary: &Self)` 把「紧随 `self` 之后的那段元素」的汇总并入 `self`。注意参数是**只读引用**——右操作数不会被消耗。

`add_summary` 的关键语义是**按序列顺序的单调叠加**：sum_tree 的所有代码路径都保证 `self` 始终代表序列开头到某处的前缀汇总，`other` 始终代表**紧跟其后的下一段**。也就是说折叠永远是从左到右、按段拼接进行的，绝不会乱序混合两段。这个约定非常重要，因为**汇总的叠加不必可交换**——比如文本汇总里「最后一行的长度」在拼接左右两段时取右段的值、而「第一行的长度」取左段的值，只有按顺序折叠才正确。

于是实现者的契约可以写成：对任意把序列切成连续几段 \( A, B, C \) 的划分，

\[ S(A \concat B \concat C) = S(A) \oplus S(B) \oplus S(C) \quad (\text{按此顺序折叠}) \]

且 \( S(\varepsilon) = \text{zero} \)。

**`ContextLessSummary` 是什么？** 它是 `Summary` 的「简化版」：当计算汇总不需要任何外部环境时，你不必写带泛型生命周期的 `Context` 关联类型，只需实现无参的 `zero()` 与 `add_summary(&mut self, &Self)`。然后靠一个包络实现（blanket impl）自动获得完整的 `Summary` 能力。实践中绝大多数汇总都是无环境的——rope 的 `TextSummary`、测试里的 `IntegersSummary` 都走这条路，少写很多样板代码。

#### 4.2.2 核心流程

```text
实现方选择路径：

  需要环境（如需要 buffer 快照才能比较 Anchor）
      │
      ▼
  直接 impl Summary，自己声明 type Context<'a> = ...      （4.3 节）
      │
  不需要环境
      ▼
  impl ContextLessSummary（zero() + add_summary(&mut self, &Self)）
      │
      ▼
  blanket impl 自动补全 Summary（Context = ()）
      │
      ▼
  树内所有 <T::Summary as Summary>::add_summary(...) 调用点无需感知差异
```

#### 4.2.3 源码精读

> [crates/sum_tree/src/sum_tree.rs:L47-L55](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L47-L55)
> `Summary` trait 全文。`type Context<'a>: Copy` 是一个 GAT（泛型关联类型）：汇总类型自己声明「算我需要什么环境」。`zero` 和 `add_summary` 都把这个环境接进来。文档注释明确点出：一个 Summary 类型可以派生多个 `Dimension` 用于树内导航。

> [crates/sum_tree/src/sum_tree.rs:L57-L60](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L57-L60)
> `ContextLessSummary` trait 全文：没有 `Context`，两个方法都不带 `cx` 参数。

> [crates/sum_tree/src/sum_tree.rs:L62-L72](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L62-L72)
> 包络实现 `impl<T: ContextLessSummary> Summary for T`：把 `Context<'a>` 固定为 `()`，再把带 `()` 的调用转调到无环境的版本。这就是「实现 `ContextLessSummary` 就等于实现了 `Summary`」的机制来源。注意 `()` 必须满足 `Copy`，空元组天然满足。

叠加顺序「永远左前缀并右后继」的一个证据在 append 路径：

> [crates/sum_tree/src/sum_tree.rs:L801-L812](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L801-L812)
> `push_tree_recursive` 把右树 `other` 挂进左树 `self` 时，第 810 行执行 `add_summary(summary, other_node.summary())`——左树汇总在前（`&mut self`）、右树汇总在后（`&self`），与两棵树元素的先后关系一致。内部机制细节留给 u4，这里只需记住方向。

一个真实生产范本（rope）：

> [crates/rope/src/rope.rs:L1265-L1278](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/rope/src/rope.rs#L1265-L1278)
> rope 的 `ChunkSummary`（文本块的汇总）实现的就是 `ContextLessSummary`：`zero` 用 `Default`，`add_summary` 里 `self.text += &summary.text`——`TextSummary` 的 `+=` 是有顺序感的拼接（首行信息取左、末行信息取右），完全依赖上一段说的折叠方向约定。

#### 4.2.4 代码实践

**实践目标**：验证 `zero` 是单位元、`add_summary` 按序叠加，用测试模块现成的 `IntegersSummary` 动手。

**操作步骤**：

1. 打开 [crates/sum_tree/src/sum_tree.rs:L1855-L1866](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1855-L1866)，阅读 `ContextLessSummary for IntegersSummary` 的四个字段操作。
2. 在 `mod tests` 内**临时添加**如下测试（示例代码，验证后删除）：

```rust
#[test]
fn test_integers_summary_monoid() {
    use std::cmp;

    let a = 3u8.summary(()); // 元素 3 的汇总
    let b = 4u8.summary(()); // 元素 4 的汇总

    // 单位元性质：zero ⊕ a == a
    let mut from_zero = IntegersSummary::zero();
    from_zero.add_summary(&a);
    assert_eq!(from_zero.count, a.count);

    // 按序叠加：a ⊕ b 描述序列 [3, 4]
    let mut folded = a.clone();
    folded.add_summary(&b);
    assert_eq!(folded.count, 2);
    assert_eq!(folded.sum, 7);
    assert!(folded.contains_even); // 4 是偶数
    assert_eq!(folded.max, 4);
}
```

3. 在仓库根目录运行 `cargo test -p sum_tree test_integers_summary_monoid`。

**需要观察的现象**：测试通过；如果把 `add_summary` 中的 `self.contains_even |= other.contains_even` 临时改成 `=`（覆盖而非或），偶数出现在左段时会丢失。

**预期结果**：原样通过；改坏后 `test_integers_summary_monoid` 或现有测试会失败（`IntegersSummary` 无 `PartialEq` 派生，若想断言整个结构体相等需临时加 `PartialEq, Eq` 派生——示例代码因此只逐字段断言）。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：给 `Summary` 的 `add_summary` 换个视角——为什么右操作数是 `&Self` 而不是 `Self`？

**答案**：树里同一份汇总经常要「既被算进父节点、又留一份在路由表里」（如 L297–L300 先借用再 clone）。只读借用让调用方保留原件，避免不必要的克隆；需要所有权时调用方自己 clone。

**练习 2**：假设你要设计一个汇总字段「序列中最后一个元素本身」（`last_item: Option<T>`），`add_summary` 该怎么写？它可交换吗？

**答案**：`self.last_item = other.last_item.clone();`——右段非空时直接覆盖，右段为空（`None`）时保留左值。不可交换：交换左右会得到完全不同的结果。它之所以能工作，恰恰因为 sum_tree 保证按序折叠；`TextSummary.last_line_chars` 就是同款技巧。

### 4.3 Context<'a> 存在的理由：汇总有时需要「环境」

#### 4.3.1 概念说明

`Context<'a>` 解决的问题是：**有些汇总没法凭元素自己算出来，还需要一份外部世界的信息。**

最典型的场景是 Zed 编辑器里的 `Anchor`（锚点）：锚点是「文本中某个位置」的持久化表示，但比较两个锚点谁在前谁在后，必须对照某个具体的多缓冲区快照（`MultiBufferSnapshot`）才能完成——脱离快照，锚点之间没有全序。于是「以锚点区间为汇总」的树，在算汇总、算路由比较时都需要把快照借进来。

把「环境」做成关联类型 `type Context<'a>` 而不是普通的泛型参数 `Summary<Ctx>`，有两个好处：

1. **同一份汇总类型可以在不同快照下复用**，快照只是借用的参数，不是类型的一部分；
2. **带生命周期** `'a`，环境可以是对某个实体的引用（`&'a MultiBufferSnapshot`），树不必拥有它，算完即还——这也解释了为什么 `SumTree` 的 API（`push`、`extend`、`from_iter`……）人手一个 `cx` 参数：它们要把环境一路透传到每次 `Item::summary` / `add_summary` 调用。

对于不需要环境的类型，包络实现把 `Context` 固定成 `()`，于是你在测试里到处看到的 `tree.extend(0..10, ())` 里那个空元组，就是「无环境」占位。

#### 4.3.2 核心流程

```text
调用链中 cx 的流动（以 push 为例）：

SumTree::push(item, cx)                    ← 调用者提供环境
    └─ item.summary(cx)                    ← 透传给 Item
           └─ 实现内部用 cx 完成需要环境的计算
SumTree::append(other, cx)
    └─ add_summary(summary, other_summary, cx)   ← 透传给 Summary
```

环境的类型由汇总类型单方面声明，元素类型通过 `<Self::Summary as Summary>::Context<'_>` 引用它——也就是说「需要什么环境」是**汇总类型的决定**，`Item` 只是跟随。

#### 4.3.3 源码精读

> [crates/sum_tree/src/sum_tree.rs:L51-L54](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L51-L54)
> `type Context<'a>: Copy` 的声明处。`Copy` 约束保证环境可以随意按值传递（引用天然 Copy）。

真实的有环境实现来自编辑器的折叠行映射：

> [crates/editor/src/display_map/crease_map.rs:L371-L381](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/editor/src/display_map/crease_map.rs#L371-L381)
> editor crate 的 `ItemSummary` 直接 `impl sum_tree::Summary`（不走 `ContextLessSummary`），声明 `type Context<'a> = &'a MultiBufferSnapshot`。它的 `add_summary` 语义是「取右段的区间作为拼接后的区间」——折叠后汇总总是描述整段的最末一个区间，这正是 4.2 练习 2 的 `last_item` 技巧的生产版。

> [crates/editor/src/display_map/crease_map.rs:L383-L392](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/editor/src/display_map/crease_map.rs#L383-L392)
> 配套的 `Item for CreaseItem`：`summary(&self, _cx: &MultiBufferSnapshot)` 的 `cx` 类型正是上面声明的环境。此处虽未用到快照，但签名被 trait 统一约束。

还有一个为「有环境类型」准备的便捷构造器：

> [crates/sum_tree/src/sum_tree.rs:L234-L241](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L234-L241)
> `from_summary` 的文档注释写明用途：当元素类型的 `Context` 是非平凡类型、但其 `zero` 值并不依赖环境时，可以用已备好的汇总直接造空树，绕过 `new(cx)`。

#### 4.3.4 代码实践

**实践目标**：体会「环境的类型由汇总类型决定」这一流向。

**操作步骤**（源码阅读型实践）：

1. 在 `sum_tree.rs` 中搜索所有 `cx: <T::Summary as Summary>::Context<'_>` 形式的参数签名，统计有多少个公开 API 带它（`new` / `from_iter` / `from_par_iter` / `push` / `extend` / `append` / `edit` 等）。
2. 打开 crease_map.rs 的调用侧（可在 `crates/editor/src/display_map/` 目录内 grep `SumTree<CreaseItem>` 相关代码），观察真实调用如何传入 `&snapshot`。

**需要观察的现象**：sum_tree 的公开 API 签名里没有任何具体的环境类型——它完全不知道 `MultiBufferSnapshot` 的存在；环境只出现在实现侧。

**预期结果**：你会确认 sum_tree 对环境类型零耦合，这是「泛型关联类型 + 透传」设计带来的解耦。待本地验证（以实际 grep 结果为准）。

#### 4.3.5 小练习与答案

**练习 1**：`Context<'a>` 为什么要求 `Copy`？

**答案**：环境会在每一次 `summary` / `add_summary` 调用中按值使用，且经常在递归、迭代（rayon 并行建树要求 `Context<'_>: Sync`，见 `from_par_iter` 的约束 L318–L325）中复制多份。`Copy` 让这些传递都是廉价的按值复制，无需显式 `.clone()`，也让借用型环境（`&T`）天然达标。

**练习 2**：如果把环境做成 `Summary<Ctx>` 的泛型参数而不是关联类型，会有什么麻烦？

**答案**：同一份汇总数据会随环境类型分裂成多个不相容的具体类型（`Summary<A>` 与 `Summary<B>` 互不相同），树的类型 `SumTree<T>` 也得跟着多一个泛型参数并渗透到所有签名；而关联类型让「汇总类型 → 环境」是唯一的单射映射，`SumTree<T>` 保持单参数。此外 GAT 还支持带生命周期的环境，普通泛型参数表达不了「借用某个快照」这种关系。

### 4.4 NoSummary：当树不需要汇总时

#### 4.4.1 概念说明

有些用法只想把 `SumTree` 当「持久化数组」用：按位置切片、追加、克隆，完全不需要任何聚合信息。但 `Item` 强制要求关联一个 `Summary` 类型——总得有个东西填在那里。

`NoSummary` 就是官方提供的占位汇总：一个不可实例化出任何信息的单元结构体，`zero` 返回自己，`add_summary` 什么都不做。用了它的树，节点上挂的汇总全是同一个无意义的值，聚合查询自然无从谈起，但序列语义（`items`、`slice`、`append`）完好无损。

#### 4.4.2 核心流程

```text
需要聚合（绝大多数用法）        自定义 XxxSummary：count/max/lines...
                                     │
不需要聚合（纯序列）            NoSummary
                                     │
                                     ▼
                       Item::Summary = NoSummary
                       summary(&self, ()) 永远返回 NoSummary
```

#### 4.4.3 源码精读

> [crates/sum_tree/src/sum_tree.rs:L74-L86](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L74-L86)
> `NoSummary` 的定义与实现。L77–L79 的注释解释了一个精妙之处：**为什么不直接用 `()` 当占位汇总**——因为文件后面有 `impl<'a, T: Summary> Dimension<'a, T> for ()`（即 `()` 已经被用作「零维度」的 fill-in，见 [L132-L136](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L132-L136)），如果再给 `()` 实现 `Summary`，会与包络实现 `impl<T: Summary> Dimension for T`（[L112-L120](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L112-L120)）发生重叠冲突。单独造一个新类型就绕开了冲突。

这个细节值得品味：Rust 的孤儿规则与包络实现经常「撞车」，`NoSummary` 是用最小的新类型化解冲突的典范手法。

#### 4.4.4 代码实践

**实践目标**：确认 `NoSummary` 的树仍然是功能完整的序列。

**操作步骤**（源码阅读型实践）：

1. 在仓库内搜索 `NoSummary` 的使用处（`Grep "NoSummary" crates/ --glob '*.rs'`），看看哪些下游类型选择了「无汇总」。
2. 阅读其中一处 `Item` 实现，确认其 `summary` 方法体就是返回 `NoSummary`。

**需要观察的现象**：使用 `NoSummary` 的树照常调用 `items` / `extend` / `append`；只有 `extent::<D>()` 这类维度查询没有可用的信息。

**预期结果**：能找到至少一处真实使用（如 editor/language crate 中的某些仅作序列使用的树）。待本地验证（以 grep 实际结果为准）。

#### 4.4.5 小练习与答案

**练习 1**：给一棵 `Item::Summary = NoSummary` 的树调用 `tree.summary()`，返回什么？这次调用有意义吗？

**答案**：返回 `&NoSummary`——调用合法但无信息量。`summary()` 是 O(1) 的字段读取，返回的值永远等于 `NoSummary` 本身。

**练习 2**：`NoSummary` 派生了 `Debug, Clone, Copy, PartialEq, Eq, Hash`，但 `()` 也能派生这些。决定「新造类型」的关键约束是什么？

**答案**：不是派生，而是 trait 实现的重叠：`()` 已被占用为任意 `Summary` 的零维度实现，同时包络实现让一切 `Summary` 自动成为 `Dimension`，两者在 `()` 上重叠，编译器会拒绝。`NoSummary` 与 `()` 类型不同，各占一个实现槽，互不冲突。

### 4.5 IntegersSummary：测试模块里的完整范本

#### 4.5.1 概念说明

`sum_tree.rs` 末尾的 `#[cfg(test)] mod tests` 里，作者为 `u8` 实现了一整套体系：`Item for u8` + `IntegersSummary`（含 count / sum / contains_even / max 四个字段的汇总）+ 三个 `Dimension` + 一个 `SeekTarget`。这是全 crate 唯一的「从零实现」示范，也是学习自定义元素类型的最佳模板。

四个字段各有代表性：

| 字段 | 类型 | add_summary 操作 | 代表的汇总套路 |
| --- | --- | --- | --- |
| `count` | `usize` | `+=` | 可交换的数值累加 |
| `sum` | `usize` | `+=` | 同上 |
| `contains_even` | `bool` | `\|=` | 存在性标记（任何一段命中即为真） |
| `max` | `u8` | `cmp::max` | 保序的极值归约 |

一个汇总同时携带多路信息，正是「多维度查询」的物质基础——下一讲的 `Dimension` 就是从这四个字段里各自投影出一条导航轴。

#### 4.5.2 核心流程

```text
tests 模块的实现清单（依依赖顺序）：

IntegersSummary 结构体（Default 派生 ⇒ zero 直接用 Default::default）
    ↓
impl Item for u8        —— 每个元素产出 count=1、sum=自身、奇偶、max=自身
    ↓
impl ContextLessSummary for IntegersSummary —— 四个字段各自的折叠规则
    ↓（自动获得 Summary，Context = ()）
impl Dimension for Count / Sum / u8(max)     —— 从汇总投影维度（下一讲主角）
    ↓
impl SeekTarget for Count                    —— 定义目标与位置的比较
```

#### 4.5.3 源码精读

> [crates/sum_tree/src/sum_tree.rs:L1820-L1826](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1820-L1826)
> `IntegersSummary` 结构体：`#[derive(Clone, Default, Debug)]`——`Default` 让 `zero()` 一行搞定；字段全私有，测试内的断言靠同模块可见性直接访问。

> [crates/sum_tree/src/sum_tree.rs:L1834-L1845](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1834-L1845)
> `impl Item for u8`：单元素的汇总是最朴素的形式——计数贡献 1，总和贡献自身值，奇偶判断 `(*self & 1) == 0`，最大值就是自身。注意为**外部类型** `u8` 实现本 crate 的 trait 完全合法（trait 与类型有一个在本 crate 即可，这里是 trait 在本 crate）。

> [crates/sum_tree/src/sum_tree.rs:L1855-L1866](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1855-L1866)
> `impl ContextLessSummary for IntegersSummary`：本讲的核心范本。四行折叠对应上表的四种套路；`cmp::max` 来自测试模块顶部的 `use std::cmp;`（[L1394-L1397](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1394-L1397)）。

> [crates/sum_tree/src/sum_tree.rs:L1868-L1876](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1868-L1876)
> `impl Dimension<'_, IntegersSummary> for u8`：把「最大值」这个维度直接安在 `u8` 类型上，`add_summary` 里 `*self = summary.max`——极值维度的叠加是覆盖而非累加。维度的完整机制留待下一讲，这里先混个脸熟。

> [crates/sum_tree/src/sum_tree.rs:L1878-L1892](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1878-L1892)
> `Count` 维度（累加 `summary.count`）及其 `SeekTarget` 实现：`self.0.cmp(&cursor_location.count)`——拿目标位置与前缀计数比较，返回 `Ordering` 驱动游标二分。u3-l1 会精读这套比较如何被消费。

#### 4.5.4 代码实践

**实践目标**：用现有 `SumTree<u8>` 验证「汇总随构建自动维护、与手算一致」。

**操作步骤**：

1. 在 `mod tests` 内添加如下测试（示例代码）：

```rust
#[test]
fn test_integers_summary_aggregation() {
    let tree = SumTree::from_iter(0u8..=9, ());

    // 手算期望值
    let expected_count = 10;
    let expected_sum: usize = (0u8..=9).map(|x| x as usize).sum();
    let expected_max = 9u8;

    let summary = tree.summary();
    assert_eq!(summary.count, expected_count);
    assert_eq!(summary.sum, expected_sum);
    assert!(summary.contains_even);
    assert_eq!(summary.max, expected_max);

    // 空树的汇总必须是单位元
    let empty = SumTree::<u8>::new(());
    assert_eq!(empty.summary().count, 0);

    // 逐个 push 的树，汇总同样正确
    let mut pushed = SumTree::<u8>::new(());
    for x in 0u8..=9 {
        pushed.push(x, ());
    }
    assert_eq!(pushed.summary().sum, expected_sum);
}
```

2. 在仓库根目录运行 `cargo test -p sum_tree test_integers_summary_aggregation`。

**需要观察的现象**：测试通过；测试构建下 `TREE_BASE = 2`，10 个元素必然跨多个叶子并长出内部节点，因此 `tree.summary()` 的值确实来自「叶子折叠 + 父节点再折叠」的多层 `add_summary`，而不是单层直算。

**预期结果**：全部断言通过（`0..=9` 求和为 45，含偶数，最大值 9）。待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：`IntegersSummary` 为什么不派生 `PartialEq`？

**答案**：测试目前只需要逐字段断言（现有测试也是这么写的），没必要为断言整相等而派生；`#[derive(Default)]` 才是必需的（`zero` 依赖它）。派生 `PartialEq` 无害但不增益——体现「按需派生」的克制。

**练习 2**：如果给 `IntegersSummary` 增加一个 `min: u8` 字段，`Item for u8` 与 `add_summary` 分别要改什么？`zero` 还对吗？

**答案**：`Item::summary` 里 `min: *self`；`add_summary` 里 `self.min = cmp::min(self.min, other.min)`。但注意 `zero()` 来自 `Default`，`u8` 的 `Default` 是 0——对 `min` 而言 0 **不是**单位元（最小值折叠的单位元应是 `u8::MAX`），空段折叠会得到错误的 0。修复办法：手写 `zero()` 让 `min: u8::MAX`（或用 `Option<u8>` 表达「尚无元素」）。这个练习说明：**依赖 `Default` 的 `zero` 只在每个字段默认值恰为单位元时才安全**。

## 5. 综合实践

**任务**：为自定义类型 `Word` 完整实现 `Item` 与 `ContextLessSummary`，并验证汇总正确性——把本讲的全部概念串成一条动手链路。

在 `crates/sum_tree/src/sum_tree.rs` 的 `mod tests` 内（建议加在 `test_integers_summary_monoid` 附近）添加以下代码（示例代码）：

```rust
#[derive(Clone, Debug)]
struct Word {
    text: String,
    is_keyword: bool,
}

#[derive(Clone, Default, Debug)]
struct WordSummary {
    count: usize,          // 元素个数
    max_len: usize,        // 最长单词长度（按字节）
    contains_keyword: bool,// 是否含关键字
}

impl Item for Word {
    type Summary = WordSummary;

    fn summary(&self, _cx: ()) -> Self::Summary {
        WordSummary {
            count: 1,
            max_len: self.text.len(),
            contains_keyword: self.is_keyword,
        }
    }
}

impl ContextLessSummary for WordSummary {
    fn zero() -> Self {
        Default::default()
    }

    fn add_summary(&mut self, other: &Self) {
        self.count += other.count;
        self.max_len = cmp::max(self.max_len, other.max_len);
        self.contains_keyword |= other.contains_keyword;
    }
}

#[test]
fn test_word_tree_summary() {
    let words = [
        ("let", true),
        ("sum_tree", false),
        ("is", true),
        ("a", false),
        ("b", false),
        ("tree", false),
        ("crate", false),
    ];
    let tree = SumTree::from_iter(
        words.iter().map(|(text, is_keyword)| Word {
            text: text.to_string(),
            is_keyword: *is_keyword,
        }),
        (),
    );

    // 手算：7 个单词，最长的是 "sum_tree"（8 字节），含关键字（let / is）
    assert_eq!(tree.summary().count, 7);
    assert_eq!(tree.summary().max_len, "sum_tree".len());
    assert!(tree.summary().contains_keyword);

    // 序列语义不受汇总影响
    assert_eq!(tree.items(()).len(), 7);
    assert_eq!(tree.items(())[0].text, "let");

    // 空树单位元
    assert_eq!(SumTree::<Word>::new(()).summary().count, 0);

    // 分半构建再 append：汇总等于整体构建（折叠方向正确性的抽查）
    let mut left = SumTree::from_iter(
        words[..3].iter().map(|(t, k)| Word { text: t.to_string(), is_keyword: *k }),
        (),
    );
    let right = SumTree::from_iter(
        words[3..].iter().map(|(t, k)| Word { text: t.to_string(), is_keyword: *k }),
        (),
    );
    left.append(right, ());
    assert_eq!(left.summary().count, 7);
    assert_eq!(left.summary().max_len, 8);
    assert_eq!(left.summary().count, tree.summary().count);
}
```

**操作步骤**：

1. 上述代码依赖测试模块顶部已有的 `use super::*;` 与 `use std::cmp;`（[L1394-L1397](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1394-L1397)），无需新增 import。
2. 在仓库根目录运行 `cargo test -p sum_tree test_word_tree_summary`。
3. 观察通过后，做两个破坏性实验并还原：
   - 把 `add_summary` 中 `self.contains_keyword |= other.contains_keyword` 改为 `=`，重跑——哪个断言失败？
   - 把 `max_len: self.text.len()` 改为 `self.text.len() + 1`，重跑——观察「元素级汇总错一个常数，会污染整棵树的聚合」。

**需要观察的现象**：原版通过；实验一破坏「存在性」折叠；实验二让 `max_len` 系统性偏大 1，证明树内没有任何纠错机制——汇总的正确性完全由实现者的 `Item` / `add_summary` 契约保证。

**预期结果**：原版全部断言通过（7 个词、最长 8 字节、含关键字；分半 append 后汇总一致）。待本地验证。

**思考延伸**（为下一讲铺垫）：`max_len` 只能聚合，不能定位——如果你想「seek 到第一个长度 ≥ 6 的单词」，就需要把 `max_len` 变成一个 `Dimension` 并配上 `SeekTarget`。这正是 u2-l2 的主题。

## 6. 本讲小结

- **`Item`** 是元素的入场契约：关联一个 `Summary` 类型，并提供 `summary(&self, cx)` 把单个元素变成汇总；树在 `from_iter` / `from_par_iter` / `push` 三处消费它，之后只操作汇总。
- **`Summary`** 的语义是幺半群：`zero(cx)` 是单位元，`add_summary(&mut self, &Self)` 是按序列顺序的单调叠加（左前缀并入右后继），叠加不必可交换，但折叠方向由树保证。
- **`ContextLessSummary`** 是无环境汇总的简化入口，经 [L62-L72](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L62-L72) 的包络实现自动升级为 `Summary`（`Context = ()`）；绝大多数汇总（包括 rope 的 `ChunkSummary`）走这条路。
- **`Context<'a>` 存在的理由**：有些汇总需要外部环境才能计算（editor 的 `ItemSummary` 需要 `&MultiBufferSnapshot` 才能比较锚点区间）；GAT 让环境是汇总类型的单射声明且可带生命周期，`SumTree` 对具体环境零耦合。
- **`NoSummary`** 是「纯序列、零聚合」的占位汇总；新造类型而非复用 `()`，是为了避免与「`()` 作为零维度」的实现重叠冲突。
- **`IntegersSummary`** 是全 crate 唯一的从零实现范本：count / sum（数值累加）、contains_even（存在性或）、max（极值归约）四种折叠套路一应俱全，照抄它就能为自己的类型落地。

## 7. 下一步学习建议

下一讲 **u2-l2《Dimension 与 SeekTarget：多维度定位》** 将从本讲的汇总里「投影」出导航轴：同一个 `IntegersSummary` 如何同时支持按 `Count`、按 `Sum`、按最大值定位，`Dimensions<D1, D2, D3>` 如何组合多轴，以及 `SeekTarget::cmp` 返回的 `Ordering` 如何驱动游标。建议带着综合实践的思考延伸去读。

继续阅读源码的顺序建议：

1. [crates/sum_tree/src/sum_tree.rs:L95-L110](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L95-L110) —— `Dimension` trait 定义（下一讲主材料，先预读）。
2. [crates/rope/src/rope.rs:L1281-L1310](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/rope/src/rope.rs#L1281-L1310) —— `TextSummary` 的字段清单，感受一个「每行信息都靠折叠方向」的真实多字段汇总。
3. [crates/editor/src/display_map/crease_map.rs:L394-L399](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/editor/src/display_map/crease_map.rs#L394-L399) —— `SeekTarget for Range<Anchor>`：有环境版的目标比较，衔接 u2-l2 与 u2-l3 的内容。

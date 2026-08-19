# u2-l2 Dimension 与 SeekTarget：多维度定位

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `Dimension` trait 的角色：它是从 `Summary` 里「投影」出一条可叠加导航轴的方式——同一个 `IntegersSummary` 可以同时派生出按元素个数（`Count`）、按数值总和（`Sum`）、按最大值（`u8`）定位的三条轴，且每条轴都能独立用于 `seek` 与 `extent`。
2. 为已有的 `Summary` 实现自定义 `Dimension`，并识别三种典型投影形态：计数累加、数值求和、极值覆盖。
3. 解释 `SeekTarget::cmp` 返回的 `Ordering` 三值语义（Greater / Equal / Less 分别意味着什么），以及「目标类型」与「游标位置维度类型」是被这个 trait 解耦的两件事。
4. 知道 `()` 可以作任意树的「零维度」，`Dimensions<D1, D2, D3>` 如何让一次导航同时记录多条轴上的坐标。
5. 牢记一条铁律：**维度只能投影 Summary 里已经有的信息**——这条铁律就是本讲综合实践的主角。

本讲承接 u2-l1 的 `Item` / `Summary`：上一讲我们让树「记住」了聚合信息，本讲让这些信息变成可导航的坐标系。u3 整个单元的 `Cursor`，都是在本讲两个 trait 的约束下运转的。

## 2. 前置知识

阅读本讲前，你应当已经掌握（u2-l1 的内容）：

- **`Summary` 是幺半群**：`zero(cx)` 是单位元，`add_summary(&mut self, &Self)` 是按序列顺序的单调叠加（左前缀并入右后继），叠加不必可交换。
- **`IntegersSummary` 的四个字段**：`count`（`+=` 累加）、`sum`（`+=` 累加）、`contains_even`（`|=` 存在性或）、`max`（`cmp::max` 极值归约）。
- **`Context<'a>`**：汇总类型自己声明的环境，无环境时经 `ContextLessSummary` 包络实现固定为 `()`。

本讲新引入的直觉，先用两段话建立：

- **维度（Dimension）是汇总的同态投影**。把「一串元素的汇总」想成一张多列报表（列 = count、sum、max……），维度就是只看其中一列（或几列）时戴上的滤镜。滤镜必须「保叠加」：先拼两段再看滤镜，与先各自看滤镜再拼，结果要一样——否则树里预聚合的汇总就没法用来导航了。
- **目标（SeekTarget）是「能与当前位置比大小」的任意东西**。seek 要找的位置不必本身是一个坐标，它只需要回答一个问题：「比起游标当前所在的坐标，目标在左边还是右边？」这个「比大小」被抽象成 `cmp` 方法，返回三值的 `Ordering`。

为什么 B+ 树的汇总天然适合导航？因为从根到叶的每一步，都只需要比较「目标」与「当前孩子结束处的前缀投影」，就能决定往哪个孩子走——一次比较砍掉一半候选，总代价 \( O(\log n) \)。维度就是那把用来量「前缀」的尺子。

## 3. 本讲源码地图

本讲的 trait 定义全部集中在一个文件的头部：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `crates/sum_tree/src/sum_tree.rs` | crate 根模块 | `Dimension` / `SeekTarget` / `Dimensions` 的定义（L88–L165，不到 80 行）；`extent`（L723–L734）与 `cursor::<D>`（L597–L605）两个消费点；测试模块的 `Count` / `Sum` / `u8` 三个维度实现（L1868–L1902） |
| `crates/sum_tree/src/cursor.rs` | 游标实现 | `seek` / `seek_forward` / `slice` / `summary` 的泛型约束（L400–L460）——它们是 `SeekTarget` 的全部消费现场；`seek_internal` 中 `target.cmp` 的三处用法（L471–L474、L507–L509、L573）；私有的 `End` 目标（L843–L855） |
| `crates/rope/src/rope.rs` | 文本 rope，sum_tree 最重要的下游 | `point_utf16_to_point`（L439–L452）——`Dimensions` 双轴导航的生产范本 |
| `crates/editor/src/display_map/crease_map.rs` | 编辑器折叠行映射 | `SeekTarget for Range<Anchor>` / `for Anchor`（L394–L407）——「有环境」的目标比较生产范本 |

阅读建议：先精读 `sum_tree.rs` 的 L88–L165（四个定义挨在一起，一次读完），再跳到文件末尾看三个维度实现如何落地，最后带着问题去 cursor.rs 看 `cmp` 被怎么消费——本讲只看约束和调用点，`seek_internal` 的完整机制留给 u3-l1、u3-l2。

## 4. 核心概念与源码讲解

本讲的五个最小模块：**Dimension**、**三种典型维度形态（Count / Sum / max）**、**() 零维度与「Summary 即自身维度」**、**Dimensions 组合维度**、**SeekTarget**。

### 4.1 Dimension：从 Summary 投影出的导航轴

#### 4.1.1 概念说明

`Dimension` 回答的问题是：「树的汇总里存了那么多统计量，我能不能只挑一个，当作从序列开头数起的坐标用？」

答案是可以，但要满足一个条件：**投影必须保叠加**。写成数学形式，设汇总的叠加运算为 \( \oplus \)，维度的叠加运算为 \( \otimes \)（由 `add_summary` 实现），投影 \( \pi \) 必须是两个幺半群之间的同态：

\[ \pi(a \oplus b) = \pi(a) \otimes \pi(b), \qquad \pi(0_S) = 0_D \]

只要这个条件成立，「前 \( i \) 个元素的坐标」就可以定义为：

\[ \mathrm{pos}_D(i) = \pi\big(S(x_1) \oplus S(x_2) \oplus \cdots \oplus S(x_i)\big) \]

而这个值在树里是现成的：任意节点到根的路径上，把沿途汇总折叠一次再投影即可。这就是 seek 能做对数级路由的全部数学基础。

trait 的文档注释举了 Zed 自己的例子：rope 的 `TextSummary` 同时汇总了行数、字符数、字节数——每一项都是一个可以独立 seek 的维度，「跳到第 3 行」和「跳到第 100 个字符」用的是同一棵树、不同的 Dimension。

还有一个签名细节值得注意：`add_summary(&mut self, summary: &'a S, ...)` 的参数带生命周期 `'a`，且 `'a` 出现在 trait 名字上（`Dimension<'a, S>`）。这允许一种特殊维度：**不复制投影值，而是把汇总的引用存下来**（u5-l1 会看到 TreeMap 的 `MapKeyRef` 用这招避免克隆键）。本讲的 `Count` / `Sum` 都是「复制值」型维度，但要知道签名为此留了口子。

#### 4.1.2 核心流程

```text
Dimension 在两条路径上被消费：

路径一：extent —— 全树坐标，O(1)
    tree.extent::<D>(cx)
        └─ D::zero(cx) 得到零点
        └─ 把「根节点的整树 summary」投影一次（add_summary 一次调用）
        └─ 返回 D —— 不遍历任何元素！

路径二：cursor —— 游标的位置类型
    tree.cursor::<D>(cx)
        └─ 创建 Cursor<'a, 'b, T, D>
        └─ 游标内部的 position: D，语义是
           「从序列开头到游标处的汇总，投影到维度 D」
        └─ seek 期间每跳过一个孩子/元素，position 就 add_summary 一次
```

也就是说：`D` 决定了「游标身上的刻度是什么单位」——是数元素的（`Count`）、数字节总和的（`Sum`）、还是看前缀最大值的（`u8`）。换一个 `D`，同一棵树就是一套不同的坐标系。

#### 4.1.3 源码精读

trait 定义与两个默认方法：

> [crates/sum_tree/src/sum_tree.rs:L88-L110](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L88-L110)
> `Dimension` trait 全文。L88–L94 的文档注释点了 rope 的 `TextSummary` 例子；必选方法只有 `zero(cx)`（零点）和 `add_summary(&mut self, summary: &'a S, cx)`（把一段汇总投影并叠加到自己身上——注意吃的是 `&S` 不是 `&Self`，每次都从汇总重新投影）；L99–L109 提供了两个默认方法：`with_added_summary`（链式写法）和 `from_summary`（一次投影，`zero` 后立刻 `add_summary`）。

第一个消费点，`extent`：

> [crates/sum_tree/src/sum_tree.rs:L723-L734](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L723-L734)
> `extent` 的实现体：先 `D::zero(cx)`，然后对根节点（无论 Internal 还是 Leaf）只做**一次** `extent.add_summary(summary, cx)` 就返回。整棵树在维度 D 上的「总刻度」是 O(1) 可取的——因为根的 summary 早就折叠好了。调用时用 turbofish 指定维度：`tree.extent::<Count>(())`。

第二个消费点，`cursor::<D>`：

> [crates/sum_tree/src/sum_tree.rs:L597-L605](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L597-L605)
> `SumTree::cursor` 的签名：`D` 只出现在返回类型 `Cursor<'a, 'b, T, D>` 里，约束是 `D: Dimension<'a, T::Summary>`。调用 `tree.cursor::<Count>(())` 就得到一把「以元素个数为刻度」的游标——泛型参数 `D` 完全决定游标的度量衡，树本身不动分毫。

游标身上的 `position: D` 长什么样，可以看 `start` 与 `end`：

> [crates/sum_tree/src/cursor.rs:L82-L95](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L82-L95)
> `start()` 直接返回 `&self.position`——游标当前位置在维度 D 上的投影；`end()` 则把当前 item 的汇总也投影叠加进去，得到「越过当前 item 之后」的坐标。`start` 与 `end` 的差恰好覆盖当前 item，这个关系在 u3 的 Bias 语义里是主角。

#### 4.1.4 代码实践

**实践目标**：亲手验证「同一棵树，换一个 `D` 就是换一套坐标系，且 `extent` 不遍历元素」。

**操作步骤**：

1. 在 `crates/sum_tree/src/sum_tree.rs` 的 `mod tests` 内添加如下测试（示例代码，验证后可保留或删除）：

```rust
#[test]
fn test_extent_dimensions() {
    let tree = SumTree::from_iter(0u8..=9, ());

    // 同一棵树，三条不同的坐标轴
    let count_extent = tree.extent::<Count>(());
    let sum_extent = tree.extent::<Sum>(());
    let max_extent = tree.extent::<u8>(());

    assert_eq!(count_extent, Count(10)); // 元素个数
    assert_eq!(sum_extent, Sum(45));     // 0+1+...+9
    assert_eq!(max_extent, 9u8);         // 前缀最大值 = 全局最大值

    // 空树的任何维度都是零点
    let empty = SumTree::<u8>::default();
    assert_eq!(empty.extent::<Count>(()), Count(0));
    assert_eq!(empty.extent::<Sum>(()), Sum(0));
    assert_eq!(empty.extent::<u8>(()), 0u8);
}
```

2. 在仓库根目录运行 `cargo test -p sum_tree test_extent_dimensions`。

**需要观察的现象**：三个 `extent` 调用返回完全不同的类型（`Count` / `Sum` / `u8`），但都来自同一棵树；空树在各维度上都是零点（`u8` 维度的零点是 `0`，见 4.2.3 的 `Default::default()`）。

**预期结果**：全部断言通过（10 个元素、总和 45、最大值 9）。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`Dimension::add_summary` 的参数为什么是 `&S`（汇总的引用）而不是 `&Self`（另一个维度值）？

**答案**：维度的职责就是「从汇总投影」：树里流动的是汇总（节点上存的、路由表里的都是 `S`），维度只有在拿到汇总时才需要知道怎么投影。如果参数是 `&Self`，调用方就得先把汇总投影成维度再叠加，投影逻辑反而要在树的生产代码里到处复写——现在的设计让投影规则完全收在维度实现内部。

**练习 2**：假设某维度 `D` 的 `add_summary` 写成 `*self = D::from_summary_goes_wrong(summary)` 之类的「直接覆盖」，什么条件下它仍是合法维度？

**答案**：只要满足同态条件 \( \pi(a \oplus b) = \pi(a) \otimes \pi(b) \) 且保零点即可。本讲 4.2 的 `u8`（max）维度就是「覆盖」型：`*self = summary.max`，因为前缀最大值的叠加律是 \( \max(\pi(a), \pi(b)) \)——先拼再投影等于先投影再取最大。合法性与「覆盖还是累加」无关，只与代数性质有关。

### 4.2 测试中的三个维度：Count、Sum 与 u8 的 max

#### 4.2.1 概念说明

测试模块为 `IntegersSummary` 实现了三个维度，恰好覆盖三种典型投影形态：

| 维度类型 | 叠加写法 | 投影形态 | 导航语义（「坐标 c」指什么位置） |
| --- | --- | --- | --- |
| `Count(usize)` | `self.0 += summary.count` | 计数累加 | 序列里第 c 个元素（0 起） |
| `Sum(usize)` | `self.0 += summary.sum` | 数值求和 | 前 u 值之和恰好达到 c 的地方 |
| `u8`（max） | `*self = summary.max` | 极值覆盖 | 前缀最大值首次达到 c 的地方 |

前两个是「正常」的可加维度；第三个最值得玩味：

1. **它是「覆盖」而非「累加」**：从左往右移动时，位置上的值是「到目前为止见过的最大值」，单调不减——这正是 seek 路由需要的性质（游标位置必须随前进单调不降，见 4.5.3 开头的断言）。
2. **它是 `KeyedItem::Key` 的原型**：`KeyedItem` 要求键类型 `for<'a> Dimension<'a, Self::Summary> + Ord`——即「键必须是一个有序的维度」。`u8` 同时实现了 `Dimension`（本节）和 `KeyedItem for u8` 的 `Key`（`type Key = u8`），TreeMap 靠「子树汇总里的最大键」来路由查找（u5-l1 的主题）。
3. **它演示了「维度类型可以不是新造的包装结构体」**：直接在外部类型 `u8` 上实现本 crate 的 `Dimension` 完全合法（trait 在本 crate，孤儿规则允许）。

一个必须现在就说破的限制：**三个维度能投影出来的，只有 `IntegersSummary` 四个字段的组合**。`contains_even` 是 `bool`（存在性），从它**永远数不出「偶数有几个」**——一段里有一个偶数和一个亿个偶数，投影出来都是 `true`。综合实践会正面撞上这堵墙。

#### 4.2.2 核心流程

```text
IntegersSummary 的字段  ──投影──▶  Dimension

  count: usize          ─────────▶  Count   （第 i 个元素在哪）
  sum: usize            ─────────▶  Sum     （累加和到 c 的位置在哪）
  max: u8               ─────────▶  u8      （最大值达到 c 的位置在哪）
  contains_even: bool   ─────────✖  想数偶数个数？数不出来：
                                    bool 折叠是 |=，信息在聚合时已被压缩

要新的轴？两条路：
  A. 从已有字段组合投影（仍受限于已有信息）
  B. 给 Summary 加字段，重新定义信息源（综合实践的做法）
```

#### 4.2.3 源码精读

三个维度实现挨在一起：

> [crates/sum_tree/src/sum_tree.rs:L1868-L1876](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1868-L1876)
> `impl Dimension<'_, IntegersSummary> for u8`：max 维度。`zero` 用 `Default::default()`（即 `0u8`——对非负 `u8` 的 max 折叠，0 恰是单位元）；`add_summary` 是 `*self = summary.max`——直接覆盖。看似「丢掉了左边的积累」，实则因为右操作数永远是「紧随其后的那段」的汇总，其 `max` 已经覆盖了叠加语义。

> [crates/sum_tree/src/sum_tree.rs:L1878-L1886](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1878-L1886)
> `impl Dimension<'_, IntegersSummary> for Count`：`self.0 += summary.count`——最朴素的计数投影。`Count` 结构体本身派生了 `Ord, PartialOrd, Default, Eq, PartialEq, Clone, Debug`（[L1828-L1829](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1828-L1829)），`Ord` 是它能走 4.5 的毯式 `SeekTarget` 的前提，`Default` 让 `zero` 一行搞定。

> [crates/sum_tree/src/sum_tree.rs:L1894-L1902](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1894-L1902)
> `impl Dimension<'_, IntegersSummary> for Sum`：`self.0 += summary.sum`——与 `Count` 同构，只是换了字段。三个维度加起来不到 30 行，可见实现一个维度的成本之低。

`KeyedItem` 对维度的依赖（为 u4-l3 / u5-l1 预留的伏笔）：

> [crates/sum_tree/src/sum_tree.rs:L40-L45](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L40-L45)
> `KeyedItem` 的关联类型约束：`type Key: for<'a> Dimension<'a, Self::Summary> + Ord`——「键 = 一个有序的维度」。配套的 `impl KeyedItem for u8`（[L1847-L1853](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1847-L1853)）正是把上面那个 max 维度登记为键。

#### 4.2.4 代码实践

**实践目标**：正面撞上「维度只能投影已有信息」这堵墙——先写一个**错误**的 `EvenCount` 维度，看它如何出错，为综合实践的正确版做铺垫。

**操作步骤**：

1. 在 `mod tests` 内添加（示例代码，这是**反面教材**）：

```rust
#[derive(Ord, PartialOrd, Default, Eq, PartialEq, Clone, Debug)]
struct EvenCount(usize);

// 错误实现：contains_even 是 bool，数不出「偶数个数」
impl Dimension<'_, IntegersSummary> for EvenCount {
    fn zero(_cx: ()) -> Self {
        Default::default()
    }

    fn add_summary(&mut self, summary: &IntegersSummary, _: ()) {
        self.0 += summary.contains_even as usize;
    }
}

#[test]
fn test_even_count_wrong() {
    let tree = SumTree::from_iter(0u8..=9, ());
    // 0..=9 里的偶数：0, 2, 4, 6, 8 —— 手算应该是 5
    let expected = tree.iter().filter(|x| *x % 2 == 0).count();

    // 粒度一：逐元素折叠——每个偶数元素各贡献一次，竟然「数对了」
    let mut per_item = EvenCount::zero(());
    for x in 0u8..=9 {
        let summary = <u8 as Item>::summary(&x, ());
        per_item.add_summary(&summary, ());
    }
    assert_eq!(per_item.0, expected); // 通过：5

    // 粒度二：extent 只对根的整树汇总做一次投影——bool 只剩「有没有」
    let got = tree.extent::<EvenCount>(());
    assert_eq!(got.0, expected); // 期望失败！got.0 是 1
}
```

2. 运行 `cargo test -p sum_tree test_even_count_wrong`，观察两个断言各自的结果。
3. 解释失败值：`extent` 的实现（4.1.3）只对根汇总调用**一次** `add_summary`，而根的 `contains_even` 是一个 `bool`——`true as usize` 永远只能是 1。信息早在 `ContextLessSummary::add_summary` 的 `|=` 折叠（[L1860-L1865](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1860-L1865)）里就被压缩掉了。

**需要观察的现象**：逐元素断言通过（5），`extent` 断言失败（1）。**同一个维度、同一批数据，答案随折叠粒度变化**——这正是违反同态条件 \( \pi(a \oplus b) = \pi(a) \otimes \pi(b) \) 的直接后果：`bool` 的 `|=` 把「有几个」压缩成「有没有」，压缩是有损的、不可逆的。一个「时对时错」的维度比一个「永远错」的更危险。

**预期结果**：`left: 1, right: 5` 的失败与逐元素断言的通过同时出现。待本地验证。修好它的方法见本讲综合实践。

#### 4.2.5 小练习与答案

**练习 1**：`u8` 维度的 `zero` 是 `0`。如果汇总里加一个 `min: u8` 字段并仿照 max 实现 `MinDim(u8)`，`zero` 还能是 `0` 吗？

**答案**：不能。min 折叠的单位元是 `u8::MAX`（`min(MAX, x) = x`），而 `Default for u8` 是 0——`min(0, x) = 0` 会把一切吞掉。这与 u2-l1 练习 2 的 `min` 字段问题是同一个坑：**依赖 `Default` 的 `zero` 只在每个字段的默认值恰为单位元时才安全**。

**练习 2**：`Sum` 维度导航「数值和恰好达到 c 的位置」，对一个全零序列 `[0, 0, 0]`，`seek(&Sum(0), ...)` 会定位到哪里？

**答案**：任何位置的前缀和都是 0，目标与所有 `child_end` 都 `Equal`——具体落点完全由 `Bias` 决定（`Left` 停在开头、`Right` 一路吃平所有零元素直到结尾）。这是「多元素共享同一坐标」的退化情形，Bias 就是为此存在的（u3-l2 的主题）。

### 4.3 () 零维度与「Summary 即自身的维度」

#### 4.3.1 概念说明

两个「不写也能用」的实现，填掉了维度体系的两个角落：

1. **`()` 是任意 `Summary` 的零维度**：`zero` 返回 `()`，`add_summary` 什么都不做。选它当 `D`，等于告诉游标「我不需要任何刻度，只是顺序走走」。生产代码里 `SumTree::items` 就用 `cursor::<()>()` 做纯遍历——省掉一路累加 `position` 的开销。它同时也是 `Dimensions<D1, D2, D3 = ()>` 的默认第三槽：不需要第三条轴时不用显式填占位类型。

2. **任何 `Summary` 都是自身的「全维度」**：包络实现 `impl<'a, T: Summary> Dimension<'a, T> for T` 把 `zero` / `add_summary` 直接转调给 `Summary` 的同名方法。于是 `tree.cursor::<IntegersSummary>(())` 合法——游标的位置就是完整汇总，`cursor.start().sum`、`cursor.start().count` 都能直接读（test_cursor 里到处是这种断言）。

这两个实现还有一个共同的「结构代价」：它们和 u2-l1 讲过的 `NoSummary` 注释是同一件事的三面——`()` 被占用为零维度、`T: Summary` 被占用为全维度，所以「占位汇总」必须另造 `NoSummary`，否则包络实现之间会在 `()` 上重叠冲突。

#### 4.3.2 核心流程

```text
想给游标配刻度时，选项一览：

  需要哪条轴？
      │
      ├─ 元素个数            Count / 其他计数维度
      ├─ 某字段的累加/极值    自定义 Dimension（4.2）
      ├─ 好几条轴同时要       Dimensions<D1, D2, D3>（4.4）
      ├─ 全部字段都要         直接用 T::Summary 当 D（本节包络实现）
      └─ 完全不要刻度         ()（本节零维度）
```

#### 4.3.3 源码精读

> [crates/sum_tree/src/sum_tree.rs:L132-L136](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L132-L136)
> `impl<'a, T: Summary> Dimension<'a, T> for ()`：两个方法体都是空的——零维度既无零点可言也无叠加可做。它对**任意** `T: Summary` 成立，是全维度体系里最「便宜」的选择。

> [crates/sum_tree/src/sum_tree.rs:L112-L120](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L112-L120)
> 包络实现 `impl<'a, T: Summary> Dimension<'a, T> for T`：`zero` 转调 `Summary::zero`，`add_summary` 转调 `Summary::add_summary`。「汇总本身就是自己的全维度」——投影为恒等映射。

> [crates/sum_tree/src/sum_tree.rs:L380-L390](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L380-L390)
> 零维度的生产消费现场：`items` 方法用 `self.cursor::<()>(cx)` 建游标，`next()` 前进、`item()` 取元素。这里用 `()` 而不是 `Count`，就是因为收集全部元素不需要任何位置信息。

> [crates/sum_tree/src/sum_tree.rs:L77-L79](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L77-L79)
> `NoSummary` 的注释原话：不复用 `()` 是为了避免与 `impl<T: Summary> Dimension for T` 的包络实现冲突（`()` 还要当零维度用）。u2-l1 从 Summary 一侧读过它，现在从 Dimension 一侧再看，两边的因果就闭环了。

#### 4.3.4 代码实践

**实践目标**：确认「`Summary` 当维度」与「`()` 零维度」两条免费路径都能编译、都能工作。

**操作步骤**：

1. 在 `mod tests` 内添加（示例代码）：

```rust
#[test]
fn test_full_and_zero_dimensions() {
    let mut tree = SumTree::from_iter(0u8..=4, ());

    // 路线一：Summary 本身当维度（包络实现），start() 能读出所有字段
    let mut full = tree.cursor::<IntegersSummary>(());
    full.seek(&Count(3), Bias::Right);
    assert_eq!(full.start().count, 3); // 前缀汇总：count = 3
    assert_eq!(full.start().sum, 0 + 1 + 2);
    assert_eq!(full.item(), Some(&3));

    // 路线二：() 零维度，只管顺序遍历（Iter 产出 &u8，收集需 copied）
    let items: Vec<u8> = tree.iter().copied().collect();
    assert_eq!(items, vec![0, 1, 2, 3, 4]);
}
```

2. 运行 `cargo test -p sum_tree test_full_and_zero_dimensions`。
3. 阅读上面的代码时注意：`full.seek(&Count(3), ...)` 传的目标是 `Count`，但游标维度是 `IntegersSummary`——为什么能编译？答案在 4.5 的手写 `SeekTarget`。

**需要观察的现象**：`cursor::<IntegersSummary>` 的 `start()` 是完整汇总（可读 `count` / `sum` / `contains_even` / `max` 四个字段）；`tree.iter()` 内部走的正是零维度游标路径。

**预期结果**：全部断言通过。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：既然 `items()` 用 `cursor::<()>`，为什么 `iter()`（返回 `Iter`）不走 `Dimension` 泛型？

**答案**：`Iter` 是专门为「从头到尾顺序遍历」设计的不带泛型参数的独立结构（cursor.rs 的 `Iter<'a, T>`），实现更轻。`Dimension` 泛型是为「可定位的游标」准备的；纯遍历没有定位需求，`Iter` 内部维护自己的轻量栈即可。（`items` 与 `iter` 是两条并存的遍历路径，前者返回 `Vec`，后者惰性。）

**练习 2**：`impl Dimension for ()` 对任意 `T: Summary` 成立，那 `cursor::<()>()` 的游标上调用 `start()` 返回什么？

**答案**：返回 `&()`——一个什么都不是的坐标。合法但无信息，编译器也不会拦你；这正是「零刻度」的含义。顺带一提，对零维度游标调用 `seek(&某目标)` 需要目标实现 `SeekTarget<S, ()>`，而 `()` 上的比较基本只有 `Equal`，实际中零维度游标只用 `next()` / `item()` 驱动。

### 4.4 Dimensions<D1, D2, D3>：一次导航，多轴记账

#### 4.4.1 概念说明

一个频繁出现的真实需求：**按 A 轴定位，同时想知道 B 轴上的坐标**。rope 的典型场景——「把 UTF-16 坐标 `PointUtf16` 的位置换算成 `Point`（行/列）坐标」：你得沿着树找到目标 chunk，而找到之后，还需要知道这个 chunk 边界在另一套坐标系里是多少。

笨办法是 seek 两次（一次按 A 轴、一次按 B 轴）。聪明办法是 `Dimensions<D1, D2, D3>`：一个三元组维度，`add_summary` 时把汇总的投影**同时累加到三条轴上**。游标按 D1 寻路（4.4.3 会讲为什么「按 D1」），到达后 `start().1` / `start().2` 直接给出另外两条轴上的坐标——一次导航，多轴记账。

为什么最多三条？这只是这个组合维度的设计容量：第三个槽默认 `()`，不用白不用；需要更多轴时可以嵌套（`Dimensions` 的每个槽本身也可以是 `Dimensions`）或者自定义更多字段的结构体。

#### 4.4.2 核心流程

```text
Dimensions(PointUtf16, Point) 的 seek 过程（rope 真实场景）：

cursor = chunks.cursor::<Dimensions<PointUtf16, Point>>(())
cursor.seek(&point_utf16, Bias::Left)
    │
    │  每跳过一个 chunk：
    │    position.0 += chunk_summary 的 PointUtf16 投影   ← 导航轴
    │    position.1 += chunk_summary 的 Point 投影        ← 记账轴（搭便车）
    ▼
到达目标 chunk 边界：
    cursor.start().0  →  该边界的 PointUtf16 坐标（用来算 overshoot）
    cursor.start().1  →  同一边界的 Point 坐标（换算的答案）
```

导航之所以「按 D1」，是因为配套实现了一个 `SeekTarget`：`D1` 可以直接当 `Dimensions<D1, D2, D3>` 游标的目标（比较时只看 `.0` 槽）。

#### 4.4.3 源码精读

> [crates/sum_tree/src/sum_tree.rs:L138-L153](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L138-L153)
> `Dimensions` 的定义：普通三元组 `pub struct Dimensions<D1, D2, D3 = ()>(pub D1, pub D2, pub D3)`，派生了一整套比较 trait；`Dimension` 实现里 `zero` 分别对三条槽取零点，`add_summary` 把同一段汇总依次喂给三条槽——每条槽各自投影，互不干扰。

> [crates/sum_tree/src/sum_tree.rs:L155-L165](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L155-L165)
> 配套的 `SeekTarget` 实现：`D1` 可以直接作为 `Dimensions<D1, D2, D3>` 游标的目标，`cmp` 里 `self.cmp(&cursor_location.0, cx)`——**只比较第一槽**。第二、三槽纯粹搭便车记账，不参与寻路。

生产范本（rope 的坐标换算）：

> [crates/rope/src/rope.rs:L439-L452](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/rope/src/rope.rs#L439-L452)
> `point_utf16_to_point`：`cursor::<Dimensions<PointUtf16, Point>>(())` 后 `seek(&point, Bias::Left)`（目标 `point` 是 `PointUtf16`，走的正是上面那个「只看第一槽」的实现）；随后 `cursor.start().0` 是 UTF-16 侧的边界坐标（算 overshoot），`cursor.start().1` 是同一边界在 Point 侧的坐标——**换算在 seek 完成的那一刻就免费拿到了**。

> [crates/rope/src/rope.rs:L466-L476](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/rope/src/rope.rs#L466-L476)
> 同款套路的第二处：`point_to_offset_utf16` 用 `Dimensions<Point, OffsetUtf16>`——这次按 Point 导航、记 UTF-16 偏移的账。同一棵 chunk 树，随取随换坐标系。

生态里的更多用例（说明这是高频惯用法，不是测试玩具）：gpui 的虚拟列表用 `cursor::<Dimensions<Count, Height>>()`（[crates/gpui/src/elements/list.rs:L720](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/gpui/src/elements/list.rs#L720)，按条数导航、记像素高度账，注意 `Count` / `Height` 是 gpui 自己定义的维度）；multi_buffer 里大量 `cursor::<Dimensions<ExcerptOffset, MultiBufferOffset>>()` 之类的双轴游标。

#### 4.4.4 代码实践

**实践目标**：在测试树上复刻 rope 的「按一轴导航、读另一轴坐标」惯用法。

**操作步骤**：

1. 在 `mod tests` 内添加（示例代码）：

```rust
#[test]
fn test_dimensions_composite() {
    let tree = SumTree::from_iter(0u8..=9, ());

    // 按 Count 导航，同时记 Sum 的账
    let mut cursor = tree.cursor::<Dimensions<Count, Sum>>(());
    cursor.seek(&Count(5), Bias::Right);

    // 第一槽：前 5 个元素的位置
    assert_eq!(cursor.start().0, Count(5));
    // 第二槽：同一边界的 Sum 坐标 = 0+1+2+3+4
    assert_eq!(cursor.start().1, Sum(10));
    // 游标停在第 5 个元素上（下标 5，值为 5）
    assert_eq!(cursor.item(), Some(&5));
}
```

2. 运行 `cargo test -p sum_tree test_dimensions_composite`。
3. 把 `cursor::<Dimensions<Count, Sum>>` 换成 `cursor::<Dimensions<Sum, Count>>`，把目标换成 `&Sum(10)`，再断言 `start().1 == Count(5)`——验证两条轴可以互换主从。

**需要观察的现象**：`seek(&Count(5), ...)` 的目标类型是 `Count`，却能用于 `Dimensions<Count, Sum>` 维度的游标（因为 4.4.3 的 `SeekTarget` 实现）；`start().1` 无需第二次 seek 就给出了 Sum 轴坐标。

**预期结果**：两个方向的断言都通过。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么不把 `Dimensions` 设计成「任意长度的轴列表」（比如 `Vec dyn Dimension`）？

**答案**：维度的叠加发生在 seek 循环的最内层（每个孩子 / 每个元素一次），必须是静态分发、零分配的代码才不拖慢导航。三元组是编译期定型的，`add_summary` 展开成三次具体类型的调用；动态列表则每次都要虚表分发和堆分配。需要更多轴时，嵌套 `Dimensions` 或自定义结构体维度都能在保持静态分发的前提下扩展。

**练习 2**：`cursor::<Dimensions<Count, Sum>>()` 的游标上直接 `seek(&Sum(10), ...)`（用第二轴当目标）能编译吗？

**答案**：不能。`SeekTarget` 只为 `D1`（第一槽）实现，`seek` 的约束是 `Target: SeekTarget<'a, T::Summary, Dimensions<D1, D2, D3>>`——`Sum` 没有实现「针对 `Dimensions<Count, Sum>`」的 `SeekTarget`，编译期即被拒绝。想按 Sum 导航就得换 `cursor::<Dimensions<Sum, Count>>()`。这个不对称是刻意设计：导航轴必须唯一明确。

### 4.5 SeekTarget：目标与位置的比较规则

#### 4.5.1 概念说明

有了维度（刻度），还差最后一块拼图：**「目标」如何与「当前位置」比大小**。`SeekTarget` 把这件事抽象成一个方法：

```rust
fn cmp(&self, cursor_location: &D, cx: S::Context<'_>) -> Ordering;
```

返回值的三值语义（`self` 是目标，`cursor_location` 是游标位置在维度 D 上的投影）：

| 返回值 | 含义 | seek 循环的反应 |
| --- | --- | --- |
| `Greater` | 目标在此位置右侧 | 继续前进（可整棵跳过当前孩子） |
| `Equal` | 目标恰好压在此位置 | 配合 `Bias` 决定归属（u3-l2 详述） |
| `Less` | 目标在此位置左侧 | 已越过目标——`seek_internal` 入口断言直接 panic |

三个设计要点：

1. **目标不必是坐标**。它只需要「能和坐标比较」。最常见的目标是维度类型本身（`&Count(5)`），但也可以是任何实现了 `SeekTarget` 的类型——editor 的 `Range<Anchor>` 就是一例。
2. **目标类型与位置维度类型解耦**。`SeekTarget<'a, S, D>` 的两个类型参数是分开的：位置维度是 `D`，目标可以是另一个类型。测试里 `cursor::<IntegersSummary>` 的游标拿 `&Count(3)` 当目标，靠的就是手写实现。
3. **`cx` 参与比较**。比较本身可能需要环境（比较两个 `Anchor` 需要 buffer 快照），所以 `cmp` 也接 `S::Context<'_>`。

`seek` 的返回值 `bool` 也由这套比较定义：落定后再比一次，`Equal` 才算「精确定位到目标」，否则（如目标越界）返回 `false`。

#### 4.5.2 核心流程

```text
seek 内部的比较协议（简化版，完整机制在 u3）：

入口：assert!(target.cmp(&self.position).is_ge())   ← Less = 想后退，禁止

对每个孩子（内部节点）：
    child_end = position ⊕ 该孩子的汇总投影
    比较 target.cmp(&child_end)：
        Greater               → 整个孩子在目标左边，跳过（position = child_end）
        Equal 且 Bias::Right  → 目标压右边界且右偏，也跳过
        其他                  → 下钻进这个孩子
叶子内逐元素同理，只是「孩子」换成「元素」。

出口：return target.cmp(&end) == Ordering::Equal    ← 是否精确命中
```

#### 4.5.3 源码精读

trait 与毯式实现：

> [crates/sum_tree/src/sum_tree.rs:L122-L124](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L122-L124)
> `SeekTarget` trait 全文：一个方法、三个类型参数（汇总、位置维度、环境）。整个 trait 只表达「目标 vs 当前位置」这一件事。

> [crates/sum_tree/src/sum_tree.rs:L126-L130](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L126-L130)
> 毯式实现：当 `D: Dimension<'a, S> + Ord` 时，`D` 自己就是合法目标，`cmp` 直接转调 `Ord::cmp`。**这就是 `Count` / `Sum` / `u8` 都派生 `Ord` 的回报**——不用写任何 `SeekTarget` 代码就能 `seek(&Count(5), ...)`。

手写实现（为什么需要它）：

> [crates/sum_tree/src/sum_tree.rs:L1888-L1892](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1888-L1892)
> `impl SeekTarget<'_, IntegersSummary, IntegersSummary> for Count`：`self.0.cmp(&cursor_location.count)`——只拿 `count` 字段参与比较。它存在的原因：`IntegersSummary` **没有派生 `Ord`**（多字段汇总通常没有全序），走不了毯式实现；而测试又想让「位置是全汇总」的游标（`cursor::<IntegersSummary>`，见 4.3）能按计数定位，于是手写一个「目标看单字段」的比较。这正是「目标类型 ≠ 位置维度类型」的活例子。

消费现场（seek 家族的泛型约束）：

> [crates/sum_tree/src/cursor.rs:L408-L414](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L408-L414)
> `Cursor::seek` 的签名：`Target: SeekTarget<'a, T::Summary, D>`——目标类型 `Target` 与游标维度 `D` 分离。`seek` 先 `reset()` 再定位（所以可以从任意状态调用）；`seek_forward`（[L423-L428](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L423-L428)）不 reset，只能向前推进。`slice` / `summary`（[L432-L460](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L432-L460)）接受同样的目标类型。

比较协议的三个关键行：

> [crates/sum_tree/src/cursor.rs:L471-L474](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L471-L474)
> 入口断言 `target.cmp(&self.position).is_ge()`：目标不得在当前位置左侧——游标只能前进，想后退必须用会 `reset()` 的 `seek` 重新定位。这就是 `Less` 分支「不存在」的原因。

> [crates/sum_tree/src/cursor.rs:L504-L509](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L504-L509)
> 路由核心：先把 `child_summary` 叠进前缀得到 `child_end`，再 `target.cmp(&child_end)`；`Greater`（或 `Equal` 且 `Bias::Right`）就整孩子跳过——一次比较砍掉一整棵子树，这就是 \( O(\log n) \) 的来源。叶子层的逐元素版本在 [L539-L545](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L539-L545)。

> [crates/sum_tree/src/cursor.rs:L566-L574](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L566-L574)
> 出口：落定后的 `end`（`Bias::Left` 时含当前 item 的汇总）再与目标比一次，`Equal` 才返回 `true`——「是否精确命中」。目标越界（如 `Count(100)` 对 10 个元素）时游标停在结尾、返回 `false`。

两个特殊目标：

> [crates/sum_tree/src/cursor.rs:L843-L855](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L843-L855)
> 私有的 `End<D>` 目标：`cmp` 恒返回 `Greater`——「永远没到」，把游标一路推到结尾。`suffix()`（[L446-L449](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L446-L449)）用它切片出「从当前位置到末尾」。这是「目标不是坐标」的极端例证：一个哨兵。

> [crates/editor/src/display_map/crease_map.rs:L394-L407](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/editor/src/display_map/crease_map.rs#L394-L407)
> 生产范本：`SeekTarget for Range<Anchor>` 与 `for Anchor`——目标是锚点（区间），比较通过 `AnchorRangeExt::cmp` 完成，且 `cx` 是 `&MultiBufferSnapshot`（**比较本身需要环境**）。没有快照，锚点之间无全序，这正是 u2-l1「有环境汇总」在目标侧的镜像。

交叉维度的一个综合现场（随机测试里）：

> [crates/sum_tree/src/sum_tree.rs:L1581-L1588](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1581-L1588)
> `cursor.summary::<_, Sum>(&Count(end), end_bias)`：按 `Count` 给出的目标导航，聚合输出却是 `Sum` 维度——位置维度（`Count`）、目标类型（`Count`，走毯式）、输出维度（`Sum`，`summary` 的第二个泛型参数）三者各司其职。「用一把尺子寻路，用另一把尺子报数」在这一个调用里全部到齐。

#### 4.5.4 代码实践

**实践目标**：体验目标与位置维度的解耦、以及 `seek` 返回值的「精确命中」语义。

**操作步骤**：

1. 在 `mod tests` 内添加（示例代码）：

```rust
#[test]
fn test_seek_target_semantics() {
    let tree = SumTree::from_iter(0u8..=9, ());

    // 用法一：位置维度 = 目标类型（毯式 SeekTarget）
    let mut cursor = tree.cursor::<Count>(());
    assert!(cursor.seek(&Count(3), Bias::Right));   // 精确命中
    assert_eq!(cursor.item(), Some(&3));
    assert!(!cursor.seek(&Count(100), Bias::Right)); // 越界：停在结尾，未命中
    assert_eq!(cursor.item(), None);

    // 用法二：位置维度 = 全汇总，目标 = Count（手写 SeekTarget）
    let mut full = tree.cursor::<IntegersSummary>(());
    assert!(full.seek(&Count(5), Bias::Right));
    assert_eq!(full.item(), Some(&5));
    assert_eq!(full.start().count, 5);

    // 用法三：哨兵目标——suffix 拿到从当前位置到结尾的切片
    let mut tail = tree.cursor::<Count>(());
    tail.seek(&Count(7), Bias::Right);
    assert_eq!(tail.suffix().items(()), vec![7, 8, 9]);
}
```

2. 运行 `cargo test -p sum_tree test_seek_target_semantics`。
3. 故意把用法二的目标换成 `&Sum(6)` 再编译一次，观察错误信息里出现的是哪个 trait（`Count` 实现了针对 `IntegersSummary` 的 `SeekTarget`，`Sum` 没有）。

**需要观察的现象**：越界 seek 返回 `false` 且 `item()` 变为 `None`（游标停在结尾）；用法二能编译全靠 [L1888-L1892](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1888-L1892) 的手写实现；`suffix()` 切出了 `[7, 8, 9]`。

**预期结果**：原版全部通过；步骤 3 产生编译错误，指出 `Sum` 未实现 `SeekTarget<_, IntegersSummary>`。待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `seek_internal` 的入口断言用 `is_ge()`（大于等于）而不是严格 `is_gt()`？

**答案**：`Equal` 是合法起点——游标当前位置恰好就是目标位置时，seek 应当完成「定位到此处」（配合 Bias 决定停在哪个元素上），而不是报错。只有 `Less`（目标已被越过、需要后退）才是非法状态。

**练习 2**：给 `u8`（max 维度）的游标 `seek(&5u8, Bias::Left)`，对升序树 `0..=9` 会停在哪里？为什么？

**答案**：停在值为 5 的元素上。路由时 `child_end` 是前缀最大值：扫过 0–4 时 `5.cmp(child_end)` 一直是 `Greater`（跳过），到元素 5 结束时 `child_end == 5`、比较为 `Equal`，`Bias::Left` 不满足跳过条件，于是下钻停在此处——「前缀最大值首次达到 5 的位置」。对升序序列这恰好就是值 5；对无序序列则是「第一个使前缀最大值达到 5 的元素」，语义是前者而非后者。

**练习 3**：`End` 目标的 `cmp` 恒返回 `Greater`，结合 4.5.2 的协议，解释它为什么能把游标推到结尾而不是死循环。

**答案**：`Greater` 的含义是「目标还在右边」，循环的反应是跳过当前孩子 / 元素并前进；因为永远「没到」，游标便吃掉所有孩子直到 `stack` 为空、`at_end = true` 才退出。循环的推进由「跳过」驱动，而退出条件是「没有更多孩子可跳」，不是「比较返回了 Equal」——所以哨兵式目标天然收敛。

## 5. 综合实践

**任务**：把 4.2.4 里那个失败的 `EvenCount` 维度修好——正确路径不是在维度上耍花招，而是**回到信息源头，给 `IntegersSummary` 增加 `even_count` 字段**。这个任务串起本讲全部概念：维度的同态条件、Summary 是信息源、`extent` 的 O(1) 投影、以及维度只能投影已有信息的铁律。

在 `crates/sum_tree/src/sum_tree.rs` 的 `mod tests` 内做以下修改（练习性修改，不要提交；改动全部位于 `#[cfg(test)]` 模块内，不影响生产代码）：

**第一步**：给 `IntegersSummary` 增加字段（示例代码）：

```rust
#[derive(Clone, Default, Debug)]
pub struct IntegersSummary {
    count: usize,
    sum: usize,
    contains_even: bool,
    even_count: usize, // 新增：偶数元素的个数
    max: u8,
}
```

**第二步**：让 `Item for u8` 产出新字段（[L1834-L1845](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1834-L1845) 处）：

```rust
fn summary(&self, _cx: ()) -> Self::Summary {
    IntegersSummary {
        count: 1,
        sum: *self as usize,
        contains_even: (*self & 1) == 0,
        even_count: (*self & 1 == 0) as usize, // 新增
        max: *self,
    }
}
```

**第三步**：让 `ContextLessSummary` 正确折叠新字段（[L1855-L1866](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1855-L1866) 处）：

```rust
fn add_summary(&mut self, other: &Self) {
    self.count += other.count;
    self.sum += other.sum;
    self.contains_even |= other.contains_even;
    self.even_count += other.even_count; // 新增：计数累加，与 count 同款套路
    self.max = cmp::max(self.max, other.max);
}
```

**第四步**：实现维度与测试（示例代码）：

```rust
#[derive(Ord, PartialOrd, Default, Eq, PartialEq, Clone, Debug)]
struct EvenCount(usize);

impl Dimension<'_, IntegersSummary> for EvenCount {
    fn zero(_cx: ()) -> Self {
        Default::default()
    }

    fn add_summary(&mut self, summary: &IntegersSummary, _: ()) {
        self.0 += summary.even_count;
    }
}

#[test]
fn test_even_count_dimension() {
    // 覆盖奇偶混合、全奇、全偶、空四种情形
    for items in [
        vec![0u8, 1, 2, 3, 4, 5, 6, 7, 8, 9],
        vec![1u8, 3, 5, 7],
        vec![2u8, 4, 6],
        vec![],
    ] {
        let tree = SumTree::from_iter(items.iter().copied(), ());

        // 对拍：与「数一遍元素」的参考模型比较
        let expected = items.iter().filter(|x| *x % 2 == 0).count();
        assert_eq!(tree.extent::<EvenCount>(()), EvenCount(expected));

        // 全树汇总里的字段值与维度投影一致（投影的保真性）
        assert_eq!(tree.summary().even_count, expected);
    }
}
```

**操作步骤**：

1. 依次完成上述四步修改（都在 `mod tests` 内；`EvenCount` 与测试加在 4.2.4 实验附近即可，若保留了 4.2.4 的错误实现请先删除，避免两个同名类型冲突）。
2. 在仓库根目录运行 `cargo test -p sum_tree test_even_count_dimension`。
3. 再跑一遍全量 `cargo test -p sum_tree`，确认给 `IntegersSummary` 加字段没有破坏现有测试（现有测试逐字段断言，不比较整结构体，理论上是安全的）。
4. 进阶验证导航能力：再加一段测试，用 `EvenCount` 当游标维度（`EvenCount` 派生了 `Ord`，走毯式 `SeekTarget`，一行都不用多写）。对 `0..=9` seek 到 `EvenCount(3)`（第 3 个偶数之后），两个偏置各断言一次：

```rust
let mut cursor = tree.cursor::<EvenCount>(());
// Bias::Left：停在「第 3 个偶数」这个元素上（0、2、4 中的 4）
cursor.seek(&EvenCount(3), Bias::Left);
assert_eq!(cursor.item(), Some(&4));
// Bias::Right：越过边界继续跳过「零宽度」的奇数元素，停在下一次汇总变大处
cursor.seek(&EvenCount(3), Bias::Right);
assert_eq!(cursor.item(), Some(&6));
```

**需要观察的现象**：

- 修正后 `extent::<EvenCount>()` 与手数结果在所有四种情形下一致——信息源（字段）对了，任何树形下投影都对。
- 进阶验证里「按偶数个数定位」与「按总个数定位」（`Count`）落在不同元素上，直观体现「换维度 = 换坐标系」。
- 两个偏置的落点不同，且 `Bias::Right` 落在 6 而非 4：奇数元素在 `EvenCount` 轴上是**零宽度**的（汇总贡献为 0），`Equal + Bias::Right` 的跳过条件会把它们连带跳过，直到下一次汇总变大（元素 6 结束时 `EvenCount` 到 4）才停下——这是「多元素共享同一坐标」的又一实例，也是 u3-l2 Bias 语义的预告。

**预期结果**：`test_even_count_dimension` 通过；全量测试通过；进阶断言 `Bias::Left` 得 `Some(&4)`、`Bias::Right` 得 `Some(&6)`。待本地验证。

**为什么这个任务重要**：它演示了维度体系的信息流向——`Item`（元素 → 汇总）→ `Summary::add_summary`（段 → 段折叠）→ `Dimension`（汇总 → 坐标轴）→ `SeekTarget`（目标 ↔ 坐标比较）。每一层只能消费下一层已经保留的信息；想数偶数，就必须从 `Item` 层就把「是不是偶数」以可加的形式（`usize` 而非 `bool`）存进汇总。`contains_even` 与 `even_count` 并存也说明：同一个事实（奇偶）可以按查询需求以不同粒度进入汇总——`bool` 够回答「有没有」，`usize` 才够回答「有几个」并支撑导航。

## 6. 本讲小结

- **`Dimension`** 是从 `Summary` 到「可叠加坐标轴」的同态投影：`zero` 给零点，`add_summary(&mut self, &S)` 每次从汇总重新投影并叠加；同态条件 \( \pi(a \oplus b) = \pi(a) \otimes \pi(b) \) 是树内预聚合能用于导航的全部前提。
- **同一个 Summary 可以派生任意多条维度**：测试的 `IntegersSummary` 同时有 `Count`（计数累加）、`Sum`（求和累加）、`u8`（极值覆盖）三条轴，各自独立用于 `extent` 与 `seek`；`extent::<D>()` 只投影根汇总一次，O(1)。
- **三种典型投影形态**：计数、求和、极值（`*self = summary.max` 的覆盖式叠加合法，因为 max 满足投影律且保持位置单调不减）；`u8` 维度同时是 `KeyedItem::Key` 的原型。
- **`()` 是任意树的零维度**（`items()` 用它做纯遍历），**任何 `Summary` 经包络实现成为自身的全维度**（`cursor::<IntegersSummary>` 能读出所有字段）；这两个实现与 `NoSummary` 的「不复用 `()`」决定互为因果。
- **`Dimensions<D1, D2, D3>`** 让一次导航同时记多条轴的账：`add_summary` 扇出到三个槽，配套 `SeekTarget` 只比较第一槽——rope 的 `point_utf16_to_point` 是生产范本。
- **`SeekTarget::cmp` 的 `Ordering` 语义**：`Greater` 继续前进（可整子树跳过）、`Equal` 配合 `Bias` 定界、`Less` 被入口断言禁止（游标只进不退）；目标类型与位置维度类型解耦，`Count` 当 `IntegersSummary` 游标的目标、editor 的 `Range<Anchor>` 目标、恒 `Greater` 的 `End` 哨兵都是这一解耦的实例。
- **铁律**：维度只能投影 Summary 里已有的信息——`bool` 的存在性折叠数不出「有几个」，要新坐标轴就得回到 `Item` / `Summary` 层扩充信息源。

## 7. 下一步学习建议

下一讲 **u2-l3《从迭代器到树：构建 API 全景》** 暂时离开 trait 体系，看树的几种诞生方式：`from_iter` 自底向上逐层组装、`from_par_iter` 的 rayon 并行构建、`push` / `extend` / `par_extend` 如何最终汇聚到 `append`。建议先带着本讲的一个问题去读：构建过程在每一层调用 `add_summary` 折叠汇总时，折叠方向是否严格遵守了 u2-l1 的「左前缀并入右后继」——这是维度投影正确性的隐含前提。

继续阅读源码的顺序建议：

1. [crates/sum_tree/src/cursor.rs:L465-L574](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L465-L574) —— `seek_internal` 完整精读（u3-l1 的主材料；本讲只看了三处 `cmp`，中间的栈升降逻辑值得先扫一遍留下印象）。
2. [crates/rope/src/rope.rs:L439-L476](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/rope/src/rope.rs#L439-L476) —— 两处 `Dimensions` 双轴导航连读，体会「坐标换算」这一最高频用法。
3. [crates/editor/src/display_map/crease_map.rs:L394-L407](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/editor/src/display_map/crease_map.rs#L394-L407) —— 有环境的 `SeekTarget`；u3 讲 `Cursor` 时会再次回到这个文件看它的调用侧。

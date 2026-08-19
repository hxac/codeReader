# FilterCursor：按 summary 剪枝遍历

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 `filter_node` 谓词作用的对象是**节点/子树的 summary**，而不是单个 item，以及这为什么能成立（可靠性契约）。
2. 逐行解释 `Cursor::search_forward` 在跳过一棵不匹配子树时，如何用**一次 `add_summary`** 完成 position 记账，从而保证 `start()` 返回的仍然是元素在**原树**中的前缀位置。
3. 理解 `search_backward` 的镜像逻辑及其与前向版本在记账方式上的差别。
4. 定量对比 `FilterCursor` 与 `iter().filter()` 的复杂度差异，知道什么时候该用哪一个。
5. 看到 rope、buffer_diff 中 `search_forward` / `FilterCursor` 的生产用法时能立刻读懂。

## 2. 前置知识

本讲建立在前三讲之上，先快速回顾需要的结论：

- **u3-l1（Cursor 栈式导航）**：`Cursor` 内部用一个容量 16 的数组模拟「根到叶路径栈」，每个 `StackEntry` 记录三个字段——`tree`（停靠的节点）、`index`（停靠的槽位）、`position`（进入该槽位之前的维度前缀和）。`did_seek` 守护「先 seek 再读取」的契约。
- **u3-l2（Bias）**：seek 系列靠 `SeekTarget::cmp` 加 `Bias` 决定边界归属。本讲的 `search_forward` / `search_backward` **没有** target 和 bias——它们的停靠条件只有一个：谓词通过。
- **u3-l3（SeekAggregate）**：`seek_internal` 通过四个记账回调定制「沿途收集什么」。本讲的 `filter_node` 定制的是另一个正交的轴——「在哪里停」。两者是同一个遍历引擎的两种定制方式。
- **u2-l1 / u2-l2（Summary 与 Dimension）**：`Summary::add_summary` 按序列顺序单调叠加；`Dimension` 从 Summary 投影出可加的导航轴。本讲的 `contains_even` 是「存在性折叠」，`Count` 是「计数投影」。

再引入两个本讲的新术语：

- **谓词（predicate）**：一个返回 `bool` 的函数。这里特指 `FnMut(&T::Summary) -> bool` 形状的闭包。
- **剪枝（pruning）**：在树的遍历中，如果在某个内部节点就能断定「整棵子树都不含目标」，就不再进入它，直接跳过。判断依据正是该子树的 summary。

## 3. 本讲源码地图

| 文件 | 相关区段 | 作用 |
| --- | --- | --- |
| `crates/sum_tree/src/cursor.rs` | L679-L746 | `FilterCursor` 的定义、构造、访问器与 `Iterator` 实现 |
| `crates/sum_tree/src/cursor.rs` | L296-L385 | `Cursor::search_forward`：前向谓词搜索（剪枝核心） |
| `crates/sum_tree/src/cursor.rs` | L221-L288 | `Cursor::search_backward`：反向谓词搜索 |
| `crates/sum_tree/src/sum_tree.rs` | L597-L619 | `SumTree::cursor` 与 `SumTree::filter` 两个入口 |
| `crates/sum_tree/src/sum_tree.rs` | L1480-L1519 | `test_random` 中对 FilterCursor 的完整随机对拍 |
| `crates/sum_tree/src/sum_tree.rs` | L1820-L1902 | 测试模块的 `IntegersSummary`、`Count` 等配套定义 |
| `crates/rope/src/rope.rs` | L885-L887、L941 | 生产代码：用 `search_forward` 跳到「包含换行」的 chunk |
| `crates/buffer_diff/src/buffer_diff.rs` | L1133-L1143 | 生产代码：用 `FilterCursor` 反向遍历 diff hunk |

## 4. 核心概念与源码讲解

### 4.1 谓词为什么能作用在 summary 上：contains_even 的可靠性契约

#### 4.1.1 概念说明

普通的过滤遍历（`iter().filter(|item| ...)`）必须把每个元素都看一遍才能判断去留。而 sum_tree 的每个内部节点都随身携带 `child_summaries`——每个孩子子树里**所有元素的汇总**（u1-l2 讲过这份冗余的用途：让游标免解引用孩子指针）。

这意味着一类问题的答案可以在子树级别直接读出来：「这棵子树里**有没有**偶数？」「这棵子树里**存不存在**换行符？」这类**存在性问题**恰好可以折叠进 summary。于是过滤条件不必逐元素判定，而是先在子树级判定：

- 谓词对子树 summary 返回 `false` → 整棵子树一起跳过，一次维度叠加了账；
- 返回 `true` → 进入这棵子树继续细分，直到叶子级别对单个元素的 summary 判定。

但这个机制对谓词有一个**单向的可靠性契约**：

> 谓词对某子树 summary 返回 `false`，必须能保证该子树里**没有任何**元素匹配；返回 `true` 只表示「可能有」，允许误报。

误报的代价只是多下钻、多扫一片叶子（变慢），不会漏项；漏项（不可靠的 `false`）才是正确性 bug。

#### 4.1.2 核心流程

以测试模块的 `IntegersSummary.contains_even` 为例，它是一个 bool 字段，折叠规则是逻辑或：

- 单元素：\(\text{contains\_even}(x) = (x \bmod 2 = 0)\)；
- 叠加：`add_summary` 用 `|=` 合并，即叶子（或子树）的值

\[ \text{contains\_even}(\text{subtree}) = \bigvee_{x \in \text{subtree}} \text{contains\_even}(x) \]

对子树 summary 判定 `contains_even == false`，等价于子树内所有元素的 `contains_even` 都是 `false`——可靠性契约成立，可以整棵剪掉。

执行流程：

```text
给定谓词 filter_node: Fn(&Summary) -> bool
在内部节点：对每个孩子的 child_summary 调用 filter_node
    false → 跳过整棵孩子子树（不进入）
    true  → 下降进这棵子树继续找
在叶子：    对每个元素的 item_summary 调用 filter_node
    true  → 游标停在这个元素上
    false → 消费掉这个元素，看下一个
```

#### 4.1.3 源码精读

summary 的定义，`contains_even` 是其中一个 bool 字段：

[crates/sum_tree/src/sum_tree.rs:1820-1826](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1820-L1826)

这是测试模块里的 `IntegersSummary`，携带 `count`、`sum`、`contains_even`、`max` 四个字段，分别示范计数、求和、存在性、极值四类折叠。

单元素的 summary 计算：

[crates/sum_tree/src/sum_tree.rs:1834-1845](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1834-L1845)

`Item for u8` 的实现：每个元素的 `contains_even` 由 `(*self & 1) == 0` 算出（L1841）。

存在性的折叠规则：

[crates/sum_tree/src/sum_tree.rs:1860-1865](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1860-L1865)

`add_summary` 的方法体：`contains_even |= other.contains_even`（L1863）就是逻辑或折叠，`max` 用 `cmp::max` 折叠。父节点的 summary 是孩子 summary 的逐字段折叠，可靠性契约由此归纳成立。

`test_random` 里实际使用的谓词就是一个闭包：

[crates/sum_tree/src/sum_tree.rs:1480-1487](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1480-L1487)

`tree.filter::<_, Count>((), |summary| summary.contains_even)` 创建过滤游标；紧跟的 `expected_filtered_items` 用「逐元素 `% 2 == 0`」的朴素方式构造参考答案——这正是对拍：两种方式必须给出完全相同的元素序列，否则就是谓词违反了可靠性契约。

#### 4.1.4 代码实践

1. **实践目标**：换一个同样满足可靠性契约的谓词，验证剪枝过滤与朴素逐项过滤结果一致。
2. **操作步骤**：在 `crates/sum_tree/src/sum_tree.rs` 的 `mod tests` 中新加一个测试（示例代码，仿照 `test_random` 的写法）：

   ```rust
   #[test]
   fn test_filter_cursor_by_max() {
       // 谓词：子树 max >= 200 才可能含有 >= 200 的元素
       let tree = SumTree::from_iter(0..=255u8, ());

       let matched: Vec<u8> = tree
           .filter::<_, Count>((), |summary| summary.max >= 200)
           .copied()
           .collect();

       let reference: Vec<u8> =
           tree.iter().copied().filter(|item| *item >= 200).collect();
       assert_eq!(matched, reference);
       assert_eq!(matched.len(), 56);
   }
   ```

3. **需要观察的现象**：前 200 个元素所在的子树，其 summary 的 `max` 小于 200，会被整棵跳过；游标只在含大元素的区域下钻。
4. **预期结果**：两个断言都通过（区间 \([200,255]\) 共 56 个元素）。`max >= 200` 这个谓词甚至不会误报——`max` 是由 `cmp::max` 折叠的，必然被子树中某个真实元素取得。待本地验证。
5. 运行方式：仓库根目录执行 `cargo test -p sum_tree test_filter_cursor_by_max`。

#### 4.1.5 小练习与答案

**练习 1**：谓词 `|s| s.count > 0` 满足可靠性契约吗？它有剪枝效果吗？

答案：满足——非空子树的 `count` 必然 ≥ 1，返回 `false` 的情况只在空子树出现，不会漏项。但它几乎无剪枝效果：所有非空子树都通过谓词，遍历会下钻到每个叶子，退化为全遍历。它语义上恒真，是个「合法但无用」的谓词。

**练习 2**：我想统计树中偶数元素的**个数**，能把谓词写成「数 contains_even」吗？

答案：不能。`contains_even` 是存在性 bool，OR 折叠不保留个数信息——这是 u2-l2 的铁律：维度（以及谓词）只能利用 Summary 已有的信息。要数个数，需要在 `IntegersSummary` 上增加 `even_count: usize` 字段并在 `Item::summary` 与 `add_summary` 里维护。当然，用 `Count` 维度配 `contains_even` 谓词遍历、对命中项计数也可以，但那是「遍历 + 计数」，不是 summary 直接给出的答案。

**练习 3**：`|s| s.max >= 200` 与 `|s| s.contains_even` 哪个可能误报？

答案：`contains_even` 可能误报（子树里有偶数，但游标停在的元素由叶子级判定决定，且任一具体子树通过只表示「有」）。`max >= 200` 不会误报，因为折叠函数 `cmp::max` 保持「max 必被某个元素取得」这一性质。两者都满足可靠性契约——契约只约束 `false` 必须可靠。

### 4.2 SumTree::filter 与 FilterCursor：谓词与游标的组合

#### 4.2.1 概念说明

`FilterCursor` 不是一个独立的遍历器，而是「一个普通 `Cursor` + 一个谓词闭包」的薄包装。它把「按谓词移动」这个语义封装成三个动作：

- `next()`：前进到下一个谓词通过的位置（内部调 `search_forward`）；
- `prev()`：后退到上一个谓词通过的位置（内部调 `search_backward`）；
- `item()` / `item_summary()` / `start()` / `end()`：只读转发给内部的 `Cursor`。

注意一个与 `Cursor` 的关键 API 差异：**`FilterCursor` 没有 `seek`**。它的起点只有两个——从头 `next()` 或从尾 `prev()`。要从中间某处开始，只能逐步前进（生产代码里通常不需要，因为过滤遍历本来就是要「扫出所有匹配项」）。

#### 4.2.2 核心流程

```text
SumTree::filter(cx, filter_node)
  └─ FilterCursor::new
       └─ tree.cursor::<D>(cx)          # 新建一个普通 Cursor（fresh、未 seek）
FilterCursor::next()
  └─ cursor.search_forward(&mut filter_node)   # 谓词作为参数传入
FilterCursor::prev()
  └─ cursor.search_backward(&mut filter_node)
作为 Iterator 使用时：
  next() → 若未 seek 过先推进一次 → 取 item() → 再推进 → 返回 item
```

`FilterCursor` 同时实现了 `Iterator`（`Item = &'a T`），所以对 `()` 上下文的 summary 可以直接 `for` 循环或 `collect`。

#### 4.2.3 源码精读

入口方法（带一条重要文档注释）：

[crates/sum_tree/src/sum_tree.rs:607-619](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L607-L619)

`SumTree::filter` 的泛型参数 `U` 是**记账维度**——与 `SumTree::cursor::<D>` 一样，决定 `start()` / `end()` 用哪个 Dimension 记前缀账。文档注释警示：如果 summary 的 `Context` 不是 `()`，返回的过滤游标**不能**与 Rust 的迭代器语法一起用。稳妥做法是像 `test_random` 和 buffer_diff 那样手动循环调用 `next()` / `prev()`。`FilterCursor` 通过根模块的 `pub use` 导出（[crates/sum_tree/src/sum_tree.rs:6](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L6)），是公开 API 的一部分。

结构体定义——就两个字段：

[crates/sum_tree/src/cursor.rs:679-682](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L679-L682)

`cursor` 是借住在此树上的普通游标，`filter_node` 是谓词闭包。

构造与访问器：

[crates/sum_tree/src/cursor.rs:684-716](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L684-L716)

`new` 内部就是 `tree.cursor::<D>(cx)` 再装上谓词；`start` / `end` / `item` / `item_summary` 全部一行转发。

移动方法：

[crates/sum_tree/src/cursor.rs:718-724](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L718-L724)

`next` / `prev` 把 `&mut self.filter_node` 传给 `search_forward` / `search_backward`——注意谓词是 `FnMut`，按可变引用传递，所以闭包可以携带可变状态（例如计数器）。

`Iterator` 实现：

[crates/sum_tree/src/cursor.rs:727-746](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L727-L746)

标准的「返回当前、再前进」模式：若游标还没 seek 过，先推进一次（L735-L737）；若 `item()` 有值，先推进再返回当前项（L739-L741）。与 `Cursor` 自身的 `Iterator` 实现（L659-L677）完全同构。

#### 4.2.4 代码实践

1. **实践目标**：体验 `FilterCursor` 的迭代器用法，并与 `iter().filter()` 的写法对照。
2. **操作步骤**：在 `mod tests` 中加入（示例代码）：

   ```rust
   #[test]
   fn test_filter_cursor_as_iterator() {
       let tree = SumTree::from_iter(0..10u8, ());

       // 写法一：FilterCursor 作为 Iterator（Context 为 ()，可以用迭代器语法）
       let evens: Vec<u8> = tree
           .filter::<_, Count>((), |summary| summary.contains_even)
           .copied()
           .collect();

       // 写法二：朴素迭代器过滤
       let reference: Vec<u8> =
           tree.iter().copied().filter(|item| item % 2 == 0).collect();

       assert_eq!(evens, reference);
       assert_eq!(evens, vec![0, 2, 4, 6, 8]);
   }
   ```

3. **需要观察的现象**：两种写法给出相同结果；写法一的闭包接收的是 `&IntegersSummary`（子树/元素级），写法二的闭包接收的是 `&u8`（纯元素级）。
4. **预期结果**：断言通过，`evens == [0, 2, 4, 6, 8]`。待本地验证。
5. 思考：写法二的 `filter` 只能逐元素判定；写法一在 4.3 节讲的机制下可以整棵跳过子树。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `FilterCursor` 不提供 `seek`？如果确实想从树的中间开始过滤遍历，怎么办？

答案：过滤遍历的语义是「枚举所有匹配项」，起点天然是两端之一，这是 API 设计的取舍。当前实现里没有公开途径把一个已 seek 的 `Cursor` 装进 `FilterCursor`（`new` 总是新建 fresh 游标），所以只能从头 `next()` 逐步前进跳过前缀，或者先在 `Cursor` 上 seek 后用 `search_forward(谓词)`（`search_forward` 本身就是 `Cursor` 的公开方法）。

**练习 2**：`FilterCursor` 既有一个固有方法 `next(&mut self)`，又实现了 `Iterator::next`，两者冲突吗？

答案：不冲突。Rust 方法解析时固有方法优先于 trait 方法，所以 `filter_cursor.next()` 调用的是固有方法；而在 `for` 循环 / `.collect()` 等消费 `Iterator` 的场合走 trait 方法。`Iterator` 实现内部（L736、L740）也是显式调用固有 `next` / `search_forward`。

**练习 3**：把 `filter` 的泛型维度 `U` 从 `Count` 换成 `Sum`（sum_tree.rs:1894-1902 定义的维度），`start()` 的含义变成什么？

答案：变成「从树开头到当前元素之前，所有元素的和」（按 `Sum` 维度投影的前缀账）。维度选择不影响停靠位置，只影响 `start()` / `end()` 的记账单位。

### 4.3 search_forward：前向剪枝与 position 记账

#### 4.3.1 概念说明

`search_forward` 是前向谓词搜索的引擎，也是 `Cursor::next` 的泛化——`next` 就是 `search_forward(|_| true)`（谓词恒真，因此不做任何剪枝，退化为普通步进）。本讲的学习目标二「跳过子树时同步累加 position」就发生在这里。

关键认识：**跳过一棵子树 = 对它的 summary 做一次 `add_summary`**。被跳过的子树里可能有成百上千个元素，但记账成本是 O(1) 次维度叠加，而不是 O(m) 次逐元素累加。这就是 `start()` 在过滤遍历中依然返回「原树前缀位置」的原因——`Count` 维度下它就是当前元素在原树中的下标。

复杂度对比（设分支因子上限 \( B = 2 \cdot \text{TREE\_BASE} \)，树高 \( h = O(\log_B n) \)，\( m \) 为被下降进入的叶子数）：

\[ \text{iter().filter()}: \ \Theta(n) \ \text{次逐元素判定} \]

\[ \text{FilterCursor}: \ O\big(B \cdot (h + m)\big) \ \text{次 summary 判定} \]

每层至多扫描 B 个孩子 summary；只下降进「summary 通过」的子树，而被进入的叶子其 summary 都通过了谓词。当匹配稀疏（\( m \ll n \)）且大块区域整体不含匹配时，FilterCursor 远快于逐项过滤；当几乎所有元素都匹配时，\( m \) 趋近全部叶子，退化为与 `iter().filter()` 同阶——但依然顺带拿到了 `start()` 的前缀账。

#### 4.3.2 核心流程

```text
search_forward(filter_node):
  若栈空：
      若非 at_end：压入根节点（index=0, position=零），descend = true
      did_seek = true
  while 栈非空：
      取栈顶 entry
      若 entry 是内部节点：
          若非刚下降（!descend）：index += 1（越过上次停靠的槽），entry.position ← self.position
          从 index 起逐个检查 child_summaries：
              filter_node(孩子 summary) 通过 → 停在这个孩子上
              不通过 → index += 1；entry.position 与 self.position 各叠加该 summary一次
          child_trees[index] 存在 → 压栈下降（descend = true）；不存在 → 弹栈上升（descend = false）
      若 entry 是叶子：
          若非刚下降：消费当前元素（index += 1，双 position 叠加其 item_summary）
          逐个检查 item_summaries：
              filter_node(元素 summary) 通过 → 直接 return（游标停在此元素）
              不通过 → 消费该元素（index += 1，双 position 叠加）
          扫到叶子末尾 → 弹栈上升
  循环结束：at_end = 栈已空
```

注意叶子级命中时是**提前 return**，不走循环收尾——因为找到元素时栈必然非空，`at_end` 必为 `false`，无需更新。

#### 4.3.3 源码精读

先看 `next` 与 `search_forward` 的关系——`next` 是谓词恒真的退化形式：

[crates/sum_tree/src/cursor.rs:291-299](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L291-L299)

初始化：栈空且未越过末尾时从头开始：

[crates/sum_tree/src/cursor.rs:302-314](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L302-L314)

这段处理两种进入状态：fresh 游标（压根从头开始）和已经越过末尾的游标（`at_end == true`，不再压栈，循环体不执行，直接维持结束状态）。

内部节点——本讲的核心，跳过子树时的双重记账：

[crates/sum_tree/src/cursor.rs:325-341](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L325-L341)

逐行看 L330-L339 的扫描循环：对当前槽位的 `next_summary` 调用 `filter_node`；通过则 `break`（停在index 不动）；不通过则同时做三件事——`entry.index += 1`（移到下一槽）、`entry.position.add_summary(next_summary)`（更新父栈帧记录的槽位起点）、`self.position.add_summary(next_summary)`（更新游标全局位置）。**每个被跳过的子树只花一次 `add_summary`**，这就是剪枝的记账本质。L341 `child_trees.get(entry.index())` 判断停在的槽位是否还有孩子可下降。

叶子——元素级判定与提前返回：

[crates/sum_tree/src/cursor.rs:343-364](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L343-L364)

`!descend` 分支（L344-L349）先消费掉上次停靠的元素——注意即使停靠后又调用一次 `next()`，那个元素也要计入 position。内层 `loop`（L351-L363）对元素的 `item_summary` 逐个判定，通过即 `return`（L354），不通过则消费。到叶子末尾 `break None`（L361）触发上层弹栈。

压栈与弹栈、收尾：

[crates/sum_tree/src/cursor.rs:368-384](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L368-L384)

有孩子可下降就压栈并置 `descend = true`（下一轮跳过「消费当前槽」的步骤）；否则弹栈、`descend = false`。循环走完意味着整棵树扫尽，`at_end = 栈空`（L383）。

`test_random` 中对这个机制的最终验证——`start()` 等于**原树**中的下标：

[crates/sum_tree/src/sum_tree.rs:1496-1503](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1496-L1503)

`expected_filtered_items` 保存的是 `(原树下标, 元素)` 对（见 L1482-L1487 的 `enumerate`），L1501 断言 `filter_cursor.start().0 == reference_index`：过滤游标每停一处，`Count` 维度的 `start()` 必须精确等于该元素在原树中的下标——被跳过的奇数元素全都通过子树级/元素级的 `add_summary` 计入了账本。

#### 4.3.4 代码实践

1. **实践目标**：亲眼验证「跳过的元素依然被计入 position」——`start()` 给出的是原树下标而非过滤序列下标。
2. **操作步骤**：在 `mod tests` 中加入（示例代码）：

   ```rust
   #[test]
   fn test_filter_cursor_start_is_original_index() {
       // 元素值恰好等于下标：0,1,2,...,9
       let tree = SumTree::from_iter(0..10u8, ());

       let mut filter_cursor =
           tree.filter::<_, Count>((), |summary| summary.contains_even);

       let mut starts = Vec::new();
       filter_cursor.next();
       while let Some(item) = filter_cursor.item() {
           assert_eq!((*item & 1), 0);
           starts.push(filter_cursor.start().0);
           filter_cursor.next();
       }

       // 偶数元素 0,2,4,6,8 的原树下标就是 0,2,4,6,8
       // 而不是过滤序列里的 0,1,2,3,4
       assert_eq!(starts, vec![0, 2, 4, 6, 8]);
   }
   ```

3. **需要观察的现象**：如果剪枝时忘记累加 position（可以把 L336-L337 两行 `add_summary` 临时注释掉再跑），`starts` 会变成什么。
4. **预期结果**：原测试通过、`starts == [0, 2, 4, 6, 8]`；注释掉记账后断言失败（`starts` 会系统性偏小）。改动源码后记得还原。待本地验证。
5. 进阶观察：在 `filter_cursor.next()` 前后各打印一次 `filter_cursor.start().0`，体会「停靠时 start = 前缀账」的不变量。

#### 4.3.5 小练习与答案

**练习 1**：叶子级命中走提前 `return`，为什么不用更新 `at_end`？

答案：命中时游标栈至少包含根到该叶子的完整路径，栈非空，`at_end` 按定义必须是 `false`。而进入函数前若从 fresh 状态开始，L313 已把 `did_seek` 置真但 `at_end` 只在栈空且越尾时为 `true`；扫尽全树（弹空栈）才会走 L383 置 `at_end = true`。提前返回的路径栈必非空，所以无需触碰 `at_end`。

**练习 2**：`entry.position` 与 `self.position` 各自维护什么？为什么两处都要 `add_summary`？

答案：`self.position` 是游标的全局位置（`start()` 直接返回它）；`entry.position` 是**父栈帧**记录的「进入当前槽位之前」的位置，属于 u3-l1 讲过的 `StackEntry` 不变量。跳过子树时它同时是「下一个槽位的起点」。只更新其中一个，另一个会在后续被读取时（如 `search_backward` 的 L246-L250 会用父栈帧的 `position` 重置 `self.position`）给出错误的前缀账。

**练习 3**：什么输入下 `FilterCursor` 相比 `iter().filter()` 完全没有优势？

答案：当几乎所有子树的 summary 都通过谓词时（例如元素几乎全是偶数），游标要下降进每片叶子并逐元素判定，判定次数与 \( n \) 同阶；此时它与 `iter().filter()` 的复杂度相同，只是还附带了 `start()` 的前缀账。剪枝的优势来自「大块区域整体不匹配」。

### 4.4 search_backward：反向剪枝与两端边界

#### 4.4.1 概念说明

`search_backward` 是 `search_forward` 的镜像：从右往左找上一个谓词通过的位置，`Cursor::prev` 同样是它谓词恒真的退化形式（[crates/sum_tree/src/cursor.rs:216-218](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L216-L218)）。它有两个与前向版本不同的细节：

1. **前缀账是每步重算的**。前向版本跳子树时增量地累加；反向版本每次步进都从父栈帧的 `position` 出发，重新对 `[..index]` 的所有孩子 summary 求和。单步代价是 O(index) 次叠加，但 index 受节点容量 \( 2 \cdot \text{TREE\_BASE} \) 封顶（测试构建 4、正式构建 12），整体仍是常数级。
2. **两端边界由特殊状态表达**。从尾部出发靠一个「虚拟末端槽位」（index = 孩子数，position = 全树 extent）实现；越过头部后栈被弹空，`item()` 返回 `None`、`start()` 归零，此后再调 `next()` 能从头部恢复——这正是 `test_random` 里专门覆盖的路径。

#### 4.4.2 核心流程

```text
search_backward(filter_node):
  若未 did_seek：did_seek = true；at_end = true（fresh 游标的 prev 从树尾出发）
  若 at_end：
      position ← 零；at_end ← 树是否为空
      非空则压入根，index = 孩子数（一Past-the-end 的虚拟槽），position = 全树 extent
  while 栈非空：
      self.position ← 父栈帧的 position（无父则零）
      entry = 栈顶
      若非刚下降（!descending）：
          index == 0 → 弹栈，继续下一轮
          否则 index -= 1（向左移一格）
      对 child_summaries[..index] 逐个 add_summary → self.position 成为当前孩子的起点
      entry.position ← self.position
      descending ← filter_node(child_summaries[index])
      若是内部节点且 descending：压入该孩子，index = 其孩子数 - 1（最右槽）
      若是叶子且 descending：break（游标停在此元素）
      （不通过 → 循环继续，同一层再左移一格）
```

#### 4.4.3 源码精读

初始化——fresh 游标从树尾出发：

[crates/sum_tree/src/cursor.rs:225-242](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L225-L242)

`!did_seek` 时直接置 `at_end = true`（L225-L228），于是走入 L230-L242 的分支：压入根节点、`index` 设为 `child_summaries` 的长度（**一past-the-end 的虚拟槽位**）、`position` 设为全树 extent。这个虚拟槽代表的正是「树尾」这个不对应任何元素的位置——u3-l1 讲过「新鲜游标调 `prev()` 会从树尾出发落在最后一个元素」，机制就在这里。

每步重算前缀与谓词判定：

[crates/sum_tree/src/cursor.rs:244-267](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L244-L267)

L246-L250 把 `self.position` 重置为父栈帧记录的位置（栈中第二个条目），没有父帧则为零。L253-L259：非下降状态下，`index == 0` 就弹栈（本层扫完了），否则左移一格。L262-L265 对 `[..entry.index()]` 的所有孩子 summary 重新累加——与前向的增量记账不同，这是**每步全量重算**，代价 O(index)，受节点容量封顶。L267 对当前落处的孩子 summary 调用 `filter_node`，结果决定是否下降（内部节点）或停靠（叶子）。

下降与停靠：

[crates/sum_tree/src/cursor.rs:268-286](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L268-L286)

内部节点且谓词通过：压入该孩子，新栈帧的 `index` 直接设为**其孩子数减一**（最右槽位，L276）——反向下降当然要从孩子的最右边开始。叶子且谓词通过：`break`，游标停在此元素。谓词不通过时什么都不做，循环继续在**同一层**左移——这就是反向剪枝：不匹配的孩子被逐个略过，每略过一个只重算一次前缀。

越过头部后的状态与恢复，`test_random` 有专门覆盖：

[crates/sum_tree/src/sum_tree.rs:1506-1516](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1506-L1516)

随机地向前向回走（L1506-L1509 是测试里 20% 概率的回退分支）；当已经停在第一个匹配项时再 `prev()` 一次（L1511-L1512），断言 `item()` 为 `None` 且 `start().0 == 0`（L1513-L1514）——栈已弹空、前缀账归零；紧接着 `next()`（L1515）又能从头部恢复到第一个匹配项。这条「越界—恢复」路径是反向遍历可靠性的试金石。

生产代码中的两个真实用例。其一是 rope 的 `Chunks` 迭代器——`next_line` 要跳到下一个含换行符的 chunk：

[crates/rope/src/rope.rs:885-887](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/rope/src/rope.rs#L885-L887)

当前 chunk 里没有 `'\n'` 时，对 chunk 游标调用 `search_forward(|summary| summary.text.lines.row > 0)`——谓词「这片子树的行数 > 0」就是 `contains_even` 的生产版：整棵不含换行的子树被 O(1) 跳过，`self.offset` 直接从 `self.chunks.start()` 读回跳过的字符数。同一谓词的反向版本在 [crates/rope/src/rope.rs:941](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/rope/src/rope.rs#L941) 的 `search_backward` 中使用。

其二是 buffer_diff 用 `FilterCursor` 反向枚举 diff hunk：

[crates/buffer_diff/src/buffer_diff.rs:1133-1143](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1133-L1143)

`hunks_intersecting_range_rev_impl` 把自定义 `filter` 闭包（按 hunk summary 判断是否与目标区间相交）交给 `self.hunks.filter::<_, DiffHunkSummary>(buffer, filter)`，然后在 `iter::from_fn` 里反复 `cursor.prev()` 取 `cursor.item()`——「手动循环而非迭代器语法」正是 `SumTree::filter` 那条文档注释建议的稳妥写法（这里的 summary 上下文是 `&BufferSnapshot`，不是 `()`）。

#### 4.4.4 代码实践

1. **实践目标**：完整走一遍「从尾部反向遍历 → 越过头部 → 恢复」的路径。
2. **操作步骤**：在 `mod tests` 中加入（示例代码）：

   ```rust
   #[test]
   fn test_filter_cursor_backward() {
       let tree = SumTree::from_iter(0..10u8, ());

       let mut filter_cursor =
           tree.filter::<_, Count>((), |summary| summary.contains_even);

       // 从树尾出发反向收集
       let mut reversed = Vec::new();
       filter_cursor.prev();
       while let Some(item) = filter_cursor.item() {
           reversed.push((*item, filter_cursor.start().0));
           filter_cursor.prev();
       }
       assert_eq!(reversed, vec![(8, 8), (6, 6), (4, 4), (2, 2), (0, 0)]);

       // 已在最前，再 prev 一次：越过头部
       filter_cursor.prev();
       assert_eq!(filter_cursor.item(), None);
       assert_eq!(filter_cursor.start().0, 0);

       // 从头部恢复
       filter_cursor.next();
       assert_eq!(filter_cursor.item(), Some(&0));
   }
   ```
3. **需要观察的现象**：反向收集到的序列是逆序的，且每个 `start()` 仍是原树下标；越过头部后 `item()` 为 `None`、`start()` 归零；`next()` 立即回到第一个偶数。
4. **预期结果**：全部断言通过。待本地验证。
5. 对照：把 `filter_cursor.prev()` 全部换成 `filter_cursor.next()`，收集到的应是正序 `[0, 2, 4, 6, 8]`——两个方向共享同一套 position 不变量。

#### 4.4.5 小练习与答案

**练习 1**：反向下降进孩子时，新栈帧的 `index` 为什么是「孩子数 - 1」而不是 0？

答案：反向遍历从右往左。下降进一棵子树后应从它的**最右**槽位开始检查，所以初始化为最后一个槽（L276）；配合 `descending = true` 跳过「左移一格」的步骤，正好落在最右孩子上。前向版本对偶地初始化为 0（L373）。

**练习 2**：反向每步都全量重算 `[..index]` 的前缀和，会不会造成平方级开销？

答案：不会超出常数。单步重算的次数是 `index`，而 `index < 2 * TREE_BASE`（节点容量上限，测试构建 4、正式构建 12）。一层之内扫 k 个槽的总代价是 \( O(k^2) \) 次叠加但 \( k \le B \)，故每层代价封顶在 \( B^2 \) 量级的常数；整体仍是 \( O(B \cdot (h + m)) \)。前向的增量记账是把这份常数进一步压小，而非复杂度层面的差别。

**练习 3**：反向遍历把栈弹空之后，`at_end` 是什么值？为什么随后的 `next()` 还能正常工作？

答案：`at_end` 保持 `false`（反向路径上只有初始化时可能把它设为 `true`，且随后在非空树上又改回 `false`；循环收尾不改它，见 L288 之前没有对 `at_end` 的赋值）。于是随后的 `search_forward` 看到栈空且 `!at_end`，会重新压入根、从头开始扫描——这正是「越界后可恢复」的实现基础。

## 5. 综合实践

把本讲的所有内容串起来：用 `FilterCursor` 统计一棵较大树中所有偶数元素的个数与总和，并与 `iter().filter()` 的结果对拍。

在 `crates/sum_tree/src/sum_tree.rs` 的 `mod tests` 中加入（示例代码）：

```rust
#[test]
fn test_filter_cursor_even_statistics() {
    // 一万个元素（值在 0..256 循环取值），让树有足够的高度
    let items = (0..10_000).map(|ix| (ix % 256) as u8).collect::<Vec<_>>();
    let tree = SumTree::from_iter(items.iter().copied(), ());

    // 方式一：FilterCursor + Count 维度，next()/start() 驱动
    let mut filter_cursor =
        tree.filter::<_, Count>((), |summary| summary.contains_even);
    let mut even_count = 0;
    let mut even_sum = 0;
    let mut starts = Vec::new();
    filter_cursor.next();
    while let Some(item) = filter_cursor.item() {
        even_count += 1;
        even_sum += *item as usize;
        starts.push(filter_cursor.start().0);
        filter_cursor.next();
    }

    // 方式二：朴素迭代器过滤（参考模型）
    let reference: Vec<(usize, u8)> = items
        .iter()
        .enumerate()
        .filter(|(_, item)| *item % 2 == 0)
        .map(|(ix, item)| (ix, *item))
        .collect();

    // 对拍：元素个数、总和、每次停靠的原树下标完全一致
    assert_eq!(even_count, reference.len());
    assert_eq!(
        even_sum,
        reference.iter().map(|(_, item)| *item as usize).sum::<usize>()
    );
    assert_eq!(starts, reference.iter().map(|(ix, _)| *ix).collect::<Vec<_>>());

    // 谓词不匹配的整棵子树被跳过：值全为奇数的子树其 contains_even 为 false。
    // 用 summary 直接验证参考值：一半元素是偶数。
    assert_eq!(even_count, 5_000);
}
```

要求与观察点：

1. **对拍三件套**：个数、总和、`start()` 序列都要与参考模型一致——第三项是本讲的核心不变量（跳过的元素依然记账）。
2. **手工核算**：因为 256 是偶数，`ix % 256` 与 `ix` 同奇偶——值为偶数当且仅当下标为偶数。所以偶数元素恰 5_000 个，`starts` 应等于全部偶数下标 `[0, 2, 4, ..., 9998]`。这是一个可以先手算、再用运行结果验证的预测，待本地验证。
3. **性能观察（可选）**：用 `std::time::Instant` 分别计时两种方式，再把谓词换成匹配率极低的 `|s| s.max >= 200`，观察 FilterCursor 的优势如何随匹配稀疏度扩大。
4. **运行**：`cargo test -p sum_tree test_filter_cursor_even_statistics`；配合 u1-l3 讲过的方式可用 `-- --nocapture` 加打印观察。

## 6. 本讲小结

- `FilterCursor` = `Cursor` + `FnMut(&T::Summary) -> bool` 谓词的薄包装；`next()`/`prev()` 分别转发给 `search_forward`/`search_backward`，没有 `seek`，起点只有两端。
- 谓词作用在**子树 summary** 上：返回 `false` 必须可靠地意味着子树内无匹配（可靠性契约），返回 `true` 允许误报。`contains_even` 用 `|=` 折叠、`max` 用 `cmp::max` 折叠，都满足契约。
- 前向剪枝的记账本质：跳过一棵子树 = 一次 `add_summary`，同时更新 `entry.position`（父栈帧槽位起点）与 `self.position`（全局位置），因此 `start()` 始终是原树中的前缀位置。
- 反向版本每步从父栈帧重算 `[..index]` 前缀（受 \( 2 \cdot \text{TREE\_BASE} \) 封顶的常数开销）；fresh 游标的 `prev()` 从「虚拟末端槽位」出发；越过头部后栈空、`start()` 归零，`next()` 可恢复。
- 复杂度：`iter().filter()` 是 \( \Theta(n) \) 次逐项判定；FilterCursor 是 \( O(B(h + m)) \) 次 summary 判定（\( B = 2 \cdot \text{TREE\_BASE} \)、\( h = O(\log_B n) \)、\( m \) 为进入的叶子数）——匹配越稀疏优势越大，全匹配时退化同阶。
- 生产范本：rope 的 `next_line` 用 `search_forward(|summary| summary.text.lines.row > 0)` 跳过不含换行的子树；buffer_diff 用 `FilterCursor` + `prev()` 反向枚举相交的 hunk。

## 7. 下一步学习建议

本讲读完，u3 单元（Cursor 游标导航）就完整了：seek 定位、Bias 边界、SeekAggregate 聚合、谓词剪枝四种定制方式你都已在源码层面见过。接下来：

1. **进入 u4 单元（写路径）**：`append` 与节点分裂。你会看到读路径攒下的所有 summary 知识如何在修改时被反向维护（回写 child_summaries / item_summaries），建议先读 `SumTree::append` 与 `push_tree_recursive`。
2. **横向对照 u3-l3 的 SeekAggregate**：`filter_node`（决定在哪停）与 `SeekAggregate`（决定沿途收集什么）是同一引擎的两个正交扩展，试着在纸上设计一个「带谓词的 slice」会加深理解。
3. **生态预告（u5-l2）**：rope 的 `Chunks` 迭代器就是本讲 4.4 生产范本的完整展开，`ChunkSummary` 的 `text.lines.row` 即其 summary 维度；学 u5 时回头重读 [crates/rope/src/rope.rs:885-887](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/rope/src/rope.rs#L885-L887) 会有完全不同的深度。

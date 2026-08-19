# Bias：边界归属的语义

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `Bias::Left` 与 `Bias::Right` 各自的归属规则：目标位置恰好落在元素边界上时，游标停在「结束于目标的元素」还是「开始于目标的元素」。
2. 读懂 `seek_internal` 与 `find_iterate` 中 `comparison == Ordering::Equal` 时的 bias 分支，并理解这两处分支其实是同一条规则的两种写法。
3. 对任意 `seek(&Count(n), bias)` 与 `slice(&Count(n), bias)`，在运行之前就能预测 `item()` 与切片内容的差异（切片端点的开闭）。
4. 知道 `Bias::invert()` 的用途，以及 `test_random` 的 splice、`TreeMap::remove_range` 等真实代码为什么选择那样的 bias 组合。

本讲承接 u3-l1 的游标知识：那里我们学会了「栈式导航 + 先 seek 再读取」，本讲专攻 seek 过程中唯一一个需要人为决策的语义开关——边界归属。

## 2. 前置知识

- **前缀和坐标**（u2-l2）：在 `Count` 维度下，树中第 \(i\) 个元素（0 基）「占据」区间 \([i,\ i+1)\)。游标每消费一个元素，就把它的 summary 累加进 `position`。因此任何一个目标位置 `n` 都是一条「缝」：它既是第 \(n-1\) 个元素的终点，也是第 \(n\) 个元素的起点。
- **SeekTarget::cmp 的返回值**（u2-l2）：`Greater` 表示目标在当前累计位置之前（继续前进）；`Less` 表示目标在累计位置之后（应停在当前元素）；`Equal` 表示恰好压在边界上——**此时 cmp 无法再提供信息，必须由 Bias 仲裁**。这就是 Bias 存在的全部理由。
- **游标状态**（u3-l1）：`stack` 是根到叶的路径栈，`position` 是当前维度前缀和，`item()` 读取栈顶叶子中停靠槽位上的元素。
- 一个容易混淆的点先行澄清：`Bias` 影响的是「停在哪」，**不影响**「能不能向后 seek」——`seek` 的方向约束（`cannot seek backward` 断言）与 bias 无关，本讲不再展开。

## 3. 本讲源码地图

| 文件 | 与本讲相关的部分 | 作用 |
|---|---|---|
| [src/sum_tree.rs](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs) | `Bias` 定义与文档（L167-L204）、`find`/`find_exact` 家族（L396-L595）、`test_cursor`（L1593-L1780）、`test_random` 中的 splice（L1446-L1470） | Bias 的定义、免游标查找中的 bias 分支、既有断言 |
| [src/cursor.rs](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs) | `seek`/`seek_forward`/`slice`/`suffix`（L405-L460）、`seek_internal`（L465-L574）、`End` 目标（L843-L855） | 游标侧的 bias 分支与切片聚合 |
| [src/tree_map.rs](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/tree_map.rs) | `get`/`remove`/`remove_range`/`closest`/`iter_from`（L56-L154） | bias 选择范本：真实业务如何搭配两种 bias |

## 4. 核心概念与源码讲解

### 4.1 Bias：边界归属的仲裁者

#### 4.1.1 概念说明

树只存「整数坐标」——每个元素占一段维度区间。但用户给出的目标位置（比如「偏移量 1」）常常正好落在两个元素的交界处。此时「位置 1 属于谁」在数学上没有答案，必须人为约定：

- `Bias::Left`：目标贴**左边**的元素，即「结束于目标位置」的那个元素；
- `Bias::Right`：目标贴**右边**的元素，即「开始于目标位置」的那个元素。

这个约定在文本编辑器里不是学术问题，而是产品语义。源码文档用三个例子说得很清楚（下节精读）：光标落在 `A` 和 `B` 之间时贴 `A` 还是贴 `B`；选区的左右两个锚点为什么一个用 Right、一个用 Left；折叠区域边缘的偏移如何随 bias 摆动。

#### 4.1.2 核心流程

以 `Count` 维度、树 \([1,2,3,4,5,6]\) 为例（每个元素计数为 1），目标 `Count(n)` 的归属规则：

| 目标 n | 边界缝的位置 | `Bias::Left` 停在 | `Bias::Right` 停在 |
|---|---|---|---|
| 0 | 树头（无左元素） | 元素 1 | 元素 1 |
| 1 | 元素 1 与 2 之间 | 元素 1（结束于 1） | 元素 2（开始于 1） |
| 3 | 元素 3 与 4 之间 | 元素 3 | 元素 4 |
| 6 | 树尾（无右元素） | 元素 6 | 树尾之外（`item()` 为 `None`） |

形式化地，seek 内部对每个候选元素（或子树）计算 `child_end`（消费它之后的累计坐标），然后：

\[
\text{继续前进（消费该元素）} \iff \text{target} > \text{child\_end}\ \lor\ \bigl(\text{target} = \text{child\_end}\ \land\ \text{bias} = \text{Right}\bigr)
\]

\[
\text{停在这里} \iff \text{target} < \text{child\_end}\ \lor\ \bigl(\text{target} = \text{child\_end}\ \land\ \text{bias} = \text{Left}\bigr)
\]

两式互补。`Less`/`Greater` 分支与 bias 无关；**bias 只在 `Equal` 时发声**。

#### 4.1.3 源码精读

`Bias` 本体极其简单——一个双变体枚举加一个 `invert` 方法：

- [sum_tree.rs:167-187](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L167-L187)：`Bias` 的文档注释。这是全 crate 最值得逐字读的注释之一。三个例子：
  - 缓冲区 `AˇBCD`（ˇ 是光标，偏移为 1）：`Bias::Left` 把光标贴到字符 `A`，`Bias::Right` 贴到 `B`；
  - 缓冲区 `A«BCˇ»D`（选区从 1 到 3）：**选区左锚点用 `Bias::Right`**（贴 `B`，选区内的第一个字符），**右锚点用 `Bias::Left`**（贴 `C`，选区内的最后一个字符）——两个锚点都「向内贴」，这样在选区边缘做编辑时锚点行为可预期；
  - 缓冲区 `{ˇ<...>`（`<...>` 是折叠区域）：显示偏移同为 1，`Bias::Left` 对应缓冲区偏移 1（贴 `{`），`Bias::Right` 则贴到折叠区域内部的首字符——同一显示位置映射出不同缓冲区偏移。
- [sum_tree.rs:188-195](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L188-L195)：枚举定义。注意 `#[default]` 标在 `Left` 上——不写 bias 的场景默认贴左。
- [sum_tree.rs:197-204](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L197-L204)：`invert()` 把 `Left`/`Right` 互换，详见 4.4。

一句话总结：**`Bias` 是 `SeekTarget::cmp` 返回 `Ordering::Equal` 时的补充仲裁输入**。`cmp` 说「目标恰好在这条缝上」，`Bias` 决定缝两边的元素谁接纳目标。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：把文档中的文本例子翻译成 `Count` 维度的预测，建立「例子 ↔ 规则」的直觉。
2. **操作步骤**：
   - 打开 [sum_tree.rs:167-187](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L167-L187)，逐个阅读三个例子；
   - 把 `AˇBCD` 想象成 `SumTree<u8>` 里的 `[65, 66, 67, 68]`（每个字符计数 1），光标偏移 1 就是 `Count(1)`；
   - 按公式写出 `seek(&Count(1), Bias::Left)` 与 `seek(&Count(1), Bias::Right)` 各自应停在哪个元素。
3. **需要观察的现象**：文档说 Left 贴 `A`、Right 贴 `B`，对应到 `Count` 就是 Left 停在 0 基下标 0、Right 停在下标 1。
4. **预期结果**：与 4.2.4 实践中实测的表格 n=1 行一致（也与既有断言 [sum_tree.rs:1769-1773](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1769-L1773) 一致）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Bias` 需要实现 `Default` 且默认值是 `Left`？
**答案**：贴左是「位置属于已越过的内容」这一更保守的直觉（类似区间左闭），大量调用点只需要一个倾向性默认值；`#[default]`（[sum_tree.rs:190-192](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L190-L192)）让 `Bias::default()` 可用，方便在结构体/参数里做缺省。

**练习 2**：目标位置落在某个元素的**中间**（比如 `Sum` 维度下目标是 4，而某元素汇总后使 `child_end = 7`）时，bias 起作用吗？
**答案**：不起作用。此时 `cmp` 返回 `Less`（或前进路径上的 `Greater`），两式中和 bias 无关的分支已经决定行为；bias 只在 `Equal`（恰好压线）时参与判定。

### 4.2 seek_internal 中的 bias 分支

#### 4.2.1 概念说明

`Cursor::seek`、`seek_forward`、`slice`、`suffix`、`summary` 五个公开方法全部汇聚到同一个私有引擎 `seek_internal`。因此 bias 在游标侧的全部语义都集中在这一处：**内层节点循环里决定「整棵子树是否划入已消费区」，叶子循环里决定「当前元素是否被消费」**。被消费的元素/子树会同步喂给 `aggregate`（这就是 `slice` 能顺手切出子树的原因，细节留待 u3-l3）。

对 `slice` 而言，「被消费」直接翻译成切片内容，于是 bias 决定了切片端点的**开闭**：

- `slice(&Count(n), Bias::Right)`：结束于 `n` 的元素**进入**切片（右端闭合）；
- `slice(&Count(n), Bias::Left)`：结束于 `n` 的元素**留在游标右侧**（右端开放）。

#### 4.2.2 核心流程

`seek_internal(target, bias, aggregate)` 的骨架（承接 u3-l1 的栈式下钻）：

```text
断言 target >= position（禁止向后）
若首次 seek：把根压栈
循环（栈顶为当前节点）：
  内层节点：
    对每个子树：累计 child_end = position + 子树 summary
      若 target > child_end 或 (== 且 bias=Right)：
          消费整棵子树：position = child_end；
          aggregate.push_tree(子树)   ← slice 时整棵 Arc 子树直接进结果
          槽位前移
      否则：把子树压栈，下钻
  叶子：
    对每个元素：累计 child_end
      若 target > child_end 或 (== 且 bias=Right)：
          消费该元素：position = child_end；aggregate.push_item(元素)
      否则：break（游标停在此元素上）
收尾：
  at_end = 栈空（Right 推到树尾时会发生）
  若 bias=Left 且当前有停靠元素：end = position + 该元素 summary
  返回 (target == end)   ← 是否精确命中
```

关键观察：**`Equal + Right` 与 `Greater` 走同一条「消费」分支，`Equal + Left` 与 `Less` 走同一条「停留」分支**。记忆口诀：Right 把边界当作「已经过去」，Left 把边界当作「尚未到达」。

#### 4.2.3 源码精读

- [cursor.rs:405-414](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L405-L414)：`Cursor::seek` 只是 `reset()` + `seek_internal(pos, bias, &mut ())`——空聚合 `()` 表示这次导航不收集任何东西。bias 原样透传。
- [cursor.rs:500-526](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L500-L526)：内层节点循环。bias 分支在 [cursor.rs:507-514](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L507-L514)：`comparison == Ordering::Greater || (comparison == Ordering::Equal && bias == Bias::Right)` 为真时整棵子树被划入已消费区（`self.position = child_end`、`aggregate.push_tree`、槽位前移）；否则下钻。** Equal + Left 时游标钻进「结束位置恰好等于目标」的那棵子树**，目标是该子树的最后一个元素。
- [cursor.rs:528-556](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L528-L556)：叶子循环。同样的条件出现在 [cursor.rs:542-551](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L542-L551)：为真则消费当前元素（`push_item` 进聚合），为假则 `break 'outer`，游标栈顶就停在未消费的元素上——之后 `item()` 返回的就是它。
- [cursor.rs:430-444](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L430-L444)：`slice` 用 `SliceSeekAggregate` 作为聚合调用 `seek_internal`——**切片的开闭完全由这里消费了哪些元素决定**，没有独立的切割逻辑。
- [cursor.rs:563-573](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L563-L573)：收尾三行最容易被忽略。`at_end = 栈空`（Right 推到树尾时所有元素被消费、栈被弹空）。随后若 `bias == Bias::Left` 且游标停在某个元素上，把该元素的 summary 加回得到 `end` 再与 target 比较——因为 Left 停靠时 `position` 是目标元素的**起点**，加回其 summary 才等于目标位置。这个布尔返回值的语义是「目标是否精确命中某个元素端点」。

既有测试对以上行为的断言（[sum_tree.rs:1769-1779](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1769-L1779)）：同一目标 `Count(1)`，Left 停在元素 1、Right 停在元素 2；以及游标停泊后连续切片时 `Count(6)` 配 Left 得 `[4, 5]`（元素 6 未被消费）、配 Right 得 `[6]`（被消费）——这正是「切片右端开 / 闭」的直接证据。

#### 4.2.4 代码实践

1. **实践目标**：用系统化表格实测 `seek(&Count(n), Bias::Left/Right)` 的停靠差异，验证 4.1.2 的预测。
2. **操作步骤**：在 `sum_tree.rs` 测试模块（`test_cursor` 附近）添加如下测试（本地实验，验完可还原；`Count` 已在 [sum_tree.rs:1828-1832](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1828-L1832) 定义，`Count` 对自身维度的 `SeekTarget` 能力来自 [sum_tree.rs:126-130](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L126-L130) 的 blanket 实现），然后在仓库根目录运行 `cargo test -p sum_tree test_bias_boundary_table`：

```rust
#[test]
fn test_bias_boundary_table() {
    let mut tree = SumTree::<u8>::default();
    tree.extend(vec![1, 2, 3, 4, 5, 6], ());
    let all = vec![1, 2, 3, 4, 5, 6];

    for n in 0..=6usize {
        let mut cursor = tree.cursor::<Count>(());
        let found_left = cursor.seek(&Count(n), Bias::Left);
        let item_left = cursor.item().copied();
        let start_left = cursor.start().0;

        let mut cursor = tree.cursor::<Count>(());
        let found_right = cursor.seek(&Count(n), Bias::Right);
        let item_right = cursor.item().copied();
        let start_right = cursor.start().0;

        // Left：目标贴「结束于 n 的元素」，即 0 基下标 n-1（n=0 时无左元素，停在首元素）
        let expect_left = if n == 0 { Some(all[0]) } else { Some(all[n - 1]) };
        // Right：目标贴「开始于 n 的元素」，即 0 基下标 n（n=6 时越界）
        let expect_right = all.get(n).copied();

        assert_eq!(item_left, expect_left, "n={n}, Bias::Left");
        assert_eq!(item_right, expect_right, "n={n}, Bias::Right");
        assert_eq!(start_left, n.saturating_sub(1), "n={n}, Left start");
        assert_eq!(start_right, n, "n={n}, Right start");

        // Right 恒精确命中；Left 仅在 n>=1（目标确为某元素终点）时命中
        assert!(found_right, "n={n}, Right found");
        assert_eq!(found_left, n >= 1, "n={n}, Left found");
    }
}
```

3. **需要观察的现象**：整理成实测表（应为）：

| n | `seek(.., Left)` 的 `item()` | `start()` | `seek(.., Right)` 的 `item()` | `start()` |
|---|---|---|---|---|
| 0 | `Some(&1)` | 0 | `Some(&1)` | 0 |
| 1 | `Some(&1)` | 0 | `Some(&2)` | 1 |
| 2 | `Some(&2)` | 1 | `Some(&3)` | 2 |
| 3 | `Some(&3)` | 2 | `Some(&4)` | 3 |
| 4 | `Some(&4)` | 3 | `Some(&5)` | 4 |
| 5 | `Some(&5)` | 4 | `Some(&6)` | 5 |
| 6 | `Some(&6)` | 5 | `None`（树尾） | 6 |

4. **预期结果**：测试通过；n=1 行与 `test_cursor` 既有断言（[sum_tree.rs:1770-1773](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1770-L1773)）完全吻合；`found_left` 在 n=0 为 `false`（目标 0 是首元素的**起点**而非终点，见 4.2.3 对收尾三行的解释）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `seek(&Count(6), Bias::Right)` 之后 `item()` 返回 `None`，而 `start()` 仍返回 6？
**答案**：Equal + Right 使最后一个元素也被消费，栈被弹空、`at_end = true`（[cursor.rs:563](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L563)）；`item()` 在栈空时返回 `None`（u3-l1），而 `position` 已累加到树的全长 6。

**练习 2**：新鲜游标上 `slice(&Count(3), Bias::Left)` 与 `slice(&Count(3), Bias::Right)` 各返回什么？
**答案**：Right 消费结束于 3 的元素 → `[1, 2, 3]`（右端闭合）；Left 停在元素 3 之前 → `[1, 2]`（右端开放）。可对照 [sum_tree.rs:1666](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1666)（`Count(2)` + Right → `[1,2]`）自行验证。

**练习 3**：`suffix()`（[cursor.rs:446-449](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L446-L449)）内部是 `slice(&End::new(), Bias::Right)`，这里传 Right 是必须的吗？
**答案**：不是。`End` 的 `cmp` 恒返回 `Greater`（[cursor.rs:851-855](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L851-L855)），永远走 `Greater` 分支消费一切，bias 分支根本不会命中；写 `Right` 只是语义上明示「一路消费到尾」。

### 4.3 find_iterate 中的 bias 分支

#### 4.3.1 概念说明

`SumTree::find` / `find_exact` / `find_with_prev` 是「免游标」的一次性查找（u4-l3 会讲它们的业务用途）。它们不维护栈，而是用递归循环 `find_iterate` 直接在树上走一遍。这里 bias 的判定条件**在字面上与 `seek_internal` 相反**：

- `seek_internal`：`Greater || (Equal && Right)` → **前进**；
- `find_iterate`：`Less || (Equal && Left)` → **命中/下钻**。

两者互为补集（对 `Ordering` 三种取值加上 bias 二分恰好吃满），所以语义完全一致——只是 `seek_internal` 从「消费」角度写，`find_iterate` 从「命中」角度写。此外 `find` 家族在入口处还有一个**树尾守卫**：目标与全树终点 `Equal` 且 bias 为 `Right` 时直接返回 `None`——因为树尾之后没有任何「开始于目标」的元素。

#### 4.3.2 核心流程

```text
find(target, bias):
  tree_end = 根 summary 投影到维度 D
  若 target > tree_end 或 (== 且 bias=Right)：返回 None（目标在树外或树尾右侧）
  find_iterate 从根出发：
    内层节点：child_end = 累计 + 子树 summary
      target_in_child = (target < child_end) 或 (== 且 bias=Left)
      命中 → 下钻该子树；否则累计前移
    叶子：child_end = 累计 + 元素 summary
      find（非精确）：命中 = (target < child_end) 或 (== 且 bias=Left)
        → Left 返回「结束于目标的元素」（左邻），Right 返回「开始于目标的元素」（右邻）
      find_exact（EXACT）：命中 = (target == child_end)，与 bias 无关
```

#### 4.3.3 源码精读

- [sum_tree.rs:410-415](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L410-L415)：`find_exact` 的树尾守卫——`Greater || (Equal && bias == Bias::Right)` 时返回 `(tree_end, tree_end, None)`。`find` 的同款守卫在 [sum_tree.rs:436-441](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L436-L441)，`find_with_prev` 在 [sum_tree.rs:521-525](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L521-L525)。例：`find(&Count(6), Bias::Right)` → `None`，`find(&Count(6), Bias::Left)` → `Some(&6)`（返回最后一个元素）。
- [sum_tree.rs:468-479](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L468-L479)：`find_iterate` 内层节点分支。`target_in_child = Less || (Equal && Left)`：Equal + Left 时目标属于「结束位置等于目标」的子树，下钻；Equal + Right 时该子树被跳过、累计前移，进入右侧子树找「开始于目标」的元素。
- [sum_tree.rs:486-502](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L486-L502)：叶子分支。命中条件在 [sum_tree.rs:490-496](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L490-L496)：非精确模式用 `Less || (Equal && Left)`；**精确模式（`find_exact`，`EXACT = true`）只看 `Equal`，bias 不参与叶子级判定**。
- 一个值得注意的坑：`find_exact` 搭配 `Bias::Right` 并不可靠——当目标恰好等于某棵**内部子树**的汇总边界时，Right 会把搜索推入右侧子树（[sum_tree.rs:472-473](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L472-L473)），而右侧子树内不会再出现 `Equal`，最终返回 `None`（是否踩中取决于树形）。本代码库中 `find_exact` 的调用点 `SumTree::get` 用的是 `Bias::Left`（[sum_tree.rs:1250](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1250)）。

#### 4.3.4 代码实践

1. **实践目标**：验证 `find` 与 `find_exact` 在边界目标下的 bias 差异，体会树尾守卫与 EXACT 分支。
2. **操作步骤**：在测试模块添加（树按测试构建 `TREE_BASE = 2` 装箱，`[1..6]` 会拆成叶 `[1,2,3,4]` 与 `[5,6]`，所以 `Count(4)` 恰是子树边界）：

```rust
#[test]
fn test_bias_in_find() {
    let mut tree = SumTree::<u8>::default();
    tree.extend(vec![1, 2, 3, 4, 5, 6], ());

    let (_, _, found) = tree.find::<IntegersSummary, _>((), &Count(4), Bias::Left);
    assert_eq!(found, Some(&4)); // 左邻：结束于 4 的元素
    let (_, _, found) = tree.find::<IntegersSummary, _>((), &Count(4), Bias::Right);
    assert_eq!(found, Some(&5)); // 右邻：开始于 4 的元素

    let (_, _, exact) = tree.find_exact::<IntegersSummary, _>((), &Count(4), Bias::Left);
    assert_eq!(exact, Some(&4));
    // Right 时目标与子树边界重合，搜索被推入右侧子树后不再有 Equal：
    let (_, _, exact) = tree.find_exact::<IntegersSummary, _>((), &Count(4), Bias::Right);
    assert_eq!(exact, None);

    // 树尾守卫：Equal + Right 直接返回 None
    let (_, _, found) = tree.find::<IntegersSummary, _>((), &Count(6), Bias::Right);
    assert_eq!(found, None);
    let (_, _, found) = tree.find::<IntegersSummary, _>((), &Count(6), Bias::Left);
    assert_eq!(found, Some(&6));
}
```

3. **需要观察的现象**：`find` 的 Left/Right 分别给出左邻/右邻；`find_exact` 的 `Count(4)` + Right 意外返回 `None`。
4. **预期结果**：测试通过。`find_exact` + Right 的行为依赖树形（若 `[1..6]` 装进单个叶子则不会触发），本测试在 `TREE_BASE = 2` 的测试构建下确定成立；正式构建（`TREE_BASE = 6`，单叶子）下 `find_exact(Count(4), Right)` 会返回 `Some(&4)`——**待本地验证**（需临时改 `TREE_BASE` 或换更长的树重跑）。

#### 4.3.5 小练习与答案

**练习 1**：`find_iterate` 与 `seek_internal` 的 bias 条件一正一反，为什么说是同一条规则？
**答案**：`seek_internal` 写的是「何时前进」（`Greater || (Equal && Right)`），`find_iterate` 写的是「何时命中/下钻」（`Less || (Equal && Left)`）。`Ordering` 只有三个值，两式对每种 `(comparison, bias)` 组合的划分互为补集，行为一致。

**练习 2**：`find(&Count(0), Bias::Left)` 在非空树上返回什么？
**答案**：树尾守卫只挡 `Greater`/`Equal+Right`，`Count(0) < tree_end` 通过；下钻后首个元素 `child_end > 0` 得 `Less` → 命中，返回第一个元素（相当于「树头的左邻」约定为首元素，与 4.2 表中 n=0 行一致）。

### 4.4 Bias::invert 与两种切割范式

#### 4.4.1 概念说明

`invert()` 表达的是一种**镜像关系**：同一个目标位置，如果把归属方向反过来，就用 `bias.invert()`。文档中选区的两个锚点（左锚点 Right、右锚点 Left）正是互为镜像的一对——从选区左端「向内看」与从右端「向内看」，观察方向相反。

掌握了 4.2 的开闭语义后，bias 的工程价值集中体现在「切割」场景：**用两次定位把树切成三段（前缀 | 操作区 | 后缀），bias 决定与切点重合的元素划归哪一段**。统一的心智模型是：

> 切点 p 配 `Right`：结束于 p 的元素划归**左侧**；切点 p 配 `Left`：与 p 重合的元素划归**右侧**。

于是「删除区间 \([a, b)\)」有两种等价写法，取决于边界元素应当归属哪侧：

- **元素端点坐标（如 `Count`）**：边界元素是「结束于 a 的元素」，它应保留在前缀里 → 两个切点都用 `Right`；
- **元素自身键坐标（如 `TreeMap` 的 `MapKey`，见 u5-l1）**：键等于 a 的元素本身就是要删的第一个元素 → 两个切点都用 `Left`。

#### 4.4.2 核心流程

`test_random` 里 splice 的切割流程（对拍 `Vec::splice`）：

```text
cursor = tree.cursor::<Count>()
前缀 = cursor.slice(&Count(start), Bias::Right)   # 保留 [0, start)：结束于 start 的元素进前缀
前缀.extend(新元素)
cursor.seek(&Count(end), Bias::Right)             # 停在"开始于 end"的元素上
后缀 = cursor.slice(&tree_end, Bias::Right)       # [end, len)
结果 = 前缀.append(后缀)                           # 被删除的恰是 [start, end)
```

`TreeMap::remove_range` 的同构流程（键坐标，两个切点都换 `Left`）：

```text
前缀 = cursor.slice(&start, Bias::Left)   # 保留键 < start（键 == start 的元素留在删除区）
cursor.seek(&end, Bias::Left)             # 停在键 == end 的元素上
结果 = 前缀.append(cursor.suffix())        # suffix 包含键 == end → 删除 [start, end)
```

#### 4.4.3 源码精读

- [sum_tree.rs:197-204](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L197-L204)：`invert()` 的全部实现——`Left`/`Right` 互换。用 `grep` 检索本仓库 `crates/` 目录未发现调用点（检索到的 `.invert()` 均属其他类型），它是为下游「成对出现的镜像定位」保留的对称工具。
- [sum_tree.rs:1459-1470](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1459-L1470)：`test_random` 中的 splice 切割，两个切点均为 `Bias::Right`；随后 L1472 与参考模型 `reference_items.splice(...)` 对拍，证明 `Right × 2` 切出的正是半开区间 \([start, end)\)。
- [tree_map.rs:112-121](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/tree_map.rs#L112-L121)：`TreeMap::remove_range`，两个切点均为 `Bias::Left`，同样得到半开的 \([start, end)\)——因为键等于 `start` 的元素应删（Left 使它不进前缀），键等于 `end` 的元素应留（Left 使游标恰好停在它上面，`suffix()` 包含它）。
- [tree_map.rs:123-130](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/tree_map.rs#L123-L130)：`closest` 是 bias 与 `prev()` 的精巧配合——注释说明它返回「小于等于给定键的最大条目」。`seek(&key, Bias::Right)` 让游标**越过**键等于目标的条目、停在下一个条目（或树尾）；再 `prev()` 退一步，恰好落回键等于目标的条目（若不存在则落在小于它的最大条目上）。若把 Right 换成 Left，`seek` 会停在目标条目上，`prev()` 退一格就变成严格小于了。
- [tree_map.rs:60-73](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/tree_map.rs#L60-L73) 与 [tree_map.rs:132-138](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/tree_map.rs#L132-L138)：`get`/`iter_from` 都用 `find`/`seek` + `Bias::Left`——键相等即「结束于目标」，Left 保证命中该条目本身（`iter_from` 从它开始迭代，包含它）。

#### 4.4.4 代码实践（源码阅读型）

1. **实践目标**：通过改动 `remove_range` 的 bias 预测行为变化，检验对切割范式的理解。
2. **操作步骤**：
   - 精读 [tree_map.rs:112-121](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/tree_map.rs#L112-L121)，记下两个 `Bias::Left`；
   - 思考两个改法：① 只把 `seek(&end, ...)` 的 Left 改成 Right；② 只把 `slice(&start, ...)` 的 Left 改成 Right；
   - 在纸上写出每种改法下「被删除的键区间」。
3. **需要观察的现象**：改法 ① 中游标会越过键等于 `end` 的条目，`suffix()` 不再包含它；改法 ② 中键等于 `start` 的条目进入前缀被保留。
4. **预期结果**：原版删 \([start, end)\)；改法 ① 变成双闭 \([start, end]\)；改法 ② 变成开区间 \((start, end)\)。如需实测，可在本地临时修改这两个 bias 并用现成的 `test_remove_between_and_path_successor` 系测试观察失败（验完还原）。此推演结论**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `closest` 用「`seek(Right)` + `prev()`」而不是直接 `seek(Left)`？
**答案**：`seek(Left)` 停在键等于目标的条目上，无法区分「命中」与「落在缝隙」；`Right` 先无条件越过相等条目，`prev()` 再退回，无论目标键是否存在都落在「≤ 目标」的最大条目上，一个组合覆盖两种情况。

**练习 2**：`Bias::invert()` 适合用在什么场景？
**答案**：成对出现且方向镜像的定位，比如文档中的选区双锚点（左锚点 Right、右锚点 Left）：拿到一端 bias 后另一端就是 `bias.invert()`，避免手写两个方向分支。

**练习 3**：`test_random` 的 splice 若把第一个切点改成 `Bias::Left`，对拍还会通过吗？
**答案**：不会。前缀会少保留「结束于 start」的那一个元素（即 0 基下标 `start-1` 的元素被误删），与 `Vec::splice(start..end, ..)` 的参考结果相差一个元素，断言失败。

## 5. 综合实践

**任务**：实现一个等价于 `Vec` 切片 `items[start..end]` 的函数 `slice_range`，再用 bias 的镜像改出「左移一格」的变体，体会两个切点的 bias 如何各自影响结果。

在测试模块中添加（示例代码，仿照 [sum_tree.rs:1459-1470](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1459-L1470) 的 splice 模式）：

```rust
fn slice_range(tree: &SumTree<u8>, start: usize, end: usize, left_bias: Bias) -> SumTree<u8> {
    let tree_end = tree.extent::<Count>(());
    let mut cursor = tree.cursor::<Count>(());
    // 切点 1：结束于 start 的元素配 Right 进前缀、配 Left 留在右侧
    let mut result = cursor.slice(&Count(start), left_bias);
    // 切点 2：同样受 left_bias 控制
    cursor.seek(&Count(end), left_bias);
    result.append(cursor.slice(&tree_end, Bias::Right), ());
    result
}

#[test]
fn test_slice_range_bias() {
    let mut tree = SumTree::<u8>::default();
    tree.extend((0..=9).collect::<Vec<_>>(), ());
    let reference: Vec<u8> = (0..=9).collect();

    // Right × 2：边界元素（结束于切点者）归左侧 → 标准 [start, end)
    for start in 0..10 {
        for end in start..=10 {
            let sliced = slice_range(&tree, start, end, Bias::Right);
            assert_eq!(sliced.items(()), reference[start..end]);
        }
    }

    // Left × 2：边界元素归右侧 → 保留的是 [0, start-1) 与 [end-1, len)
    let sliced = slice_range(&tree, 3, 7, Bias::Left);
    let mut expected = reference[..2].to_vec();
    expected.extend_from_slice(&reference[6..]);
    assert_eq!(sliced.items(()), expected);
}
```

验收标准：

1. `cargo test -p sum_tree test_slice_range_bias` 通过；
2. 能口头解释为什么 `Left × 2` 的结果等价于删除了 `items[start-1..end-1]`（提示：两个切点处的边界元素都从「保留侧」挪到了「删除侧」）；
3. （可选）把 `slice_range` 中第二个切点换成 `left_bias.invert()`，预测并验证新的保留区间。

## 6. 本讲小结

- `Bias` 只在 `SeekTarget::cmp` 返回 `Ordering::Equal`（目标恰好压在元素边界）时参与判定：`Left` 贴「结束于目标」的元素，`Right` 贴「开始于目标」的元素；`Left` 是默认值。
- 游标侧的全部 bias 语义集中在 `seek_internal`：`Greater || (Equal && Right)` 消费、`Less || (Equal && Left)` 停靠；`slice`/`suffix`/`summary` 与 `seek` 共用这台引擎，切片端点的开闭由此决定（Right 右端闭合、Left 右端开放）。
- `seek` 的布尔返回值表示「目标是否精确命中元素端点」；`Left` 停靠时需把当前元素的 summary 加回（[cursor.rs:566-571](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L566-L571)）才能得到目标坐标。
- 免游标的 `find` 家族用补集形式 `Less || (Equal && Left)` 表达同一规则；入口的树尾守卫使 `Equal + Right` 落空返回 `None`；`find_exact` 的叶子级判定只看 `Equal`，但 Right 会把与子树边界重合的目标推离命中路径，精确查找应配 `Left`。
- 切割范式：切点配 `Right` 则边界元素归左段，配 `Left` 则归右段；`test_random` 的 splice 用 `Right × 2`、`TreeMap::remove_range` 用 `Left × 2`，都切出半开区间，差别在边界元素应当保留还是删除。
- `Bias::invert()` 表达镜像定位（如选区双锚点）；`TreeMap::closest` 的 `seek(Right) + prev()` 是 bias 与步进组合出「≤ 目标」语义的范本。

## 7. 下一步学习建议

- 下一讲 u3-l3《slice、suffix、summary 与 SeekAggregate》将拆开本讲反复出现的 `aggregate` 参数：`slice` 之所以能在 seek 的同时顺手建树，靠的是 `SliceSeekAggregate` 的 `push_tree`/`push_item` 回调；学完它你就明白「整棵 Arc 子树直接进切片」的结构共享细节。
- 想看 bias 在真实产品里的分量，可跳读 `crates/rope/src/rope.rs` 中对 `sum_tree::Bias` 的使用（配合 u5-l2 的 `SumTree<Chunk>`），体会偏移/锚点如何依赖边界归属。
- u4-l3 会讲解 `find`/`find_exact`/`find_with_prev` 的业务封装（`get`/`edit`），可以把本讲 4.3 的结论直接带过去验证。

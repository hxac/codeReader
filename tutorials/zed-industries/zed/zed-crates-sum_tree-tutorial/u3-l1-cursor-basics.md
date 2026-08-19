# Cursor 基础：栈式导航与 seek

## 1. 本讲目标

学完本讲，你应该能够：

- 说出 `Cursor` 的内部表示：一个容量 16 的 `StackEntry` 数组模拟的「根到叶子路径栈」，并准确解释每个 `StackEntry` 中 `tree`、`index`、`position` 三个字段的含义。
- 会用 `tree.cursor::<D>()` 创建按维度 `D` 记账的游标，用 `seek` / `seek_forward` 完成定位，用 `next` / `prev` 在树上漫游。
- 会用 `item` / `item_summary` / `start` / `end` 读出游标当前位置的状态。
- 理解 `did_seek` 与 `at_end` 两个状态位各自守护什么，以及 `assert_did_seek` 断言背后的「先 seek、再读取」契约。

本讲是整个 u3 单元的地基：下一讲的 Bias 边界语义、再下一讲的 slice/summary 聚合、以及 FilterCursor 剪枝遍历，全部跑在本讲讲的这套栈式导航机器上。

## 2. 前置知识

本讲默认你已从前面几讲了解了以下内容，这里只做一句話回顾：

- **B+ 树骨架**（u1-l2）：`SumTree` 的叶子节点存 `items` 与逐元素的 `item_summaries`；内部节点只存 `child_summaries`（路由表）与 `child_trees`，不存元素。
- **Summary 与 Dimension**（u2-l1、u2-l2）：`Summary` 是可单调叠加的汇总；`Dimension` 是从 `Summary` 投影出的可加导航轴（如测试中的 `Count`、`Sum`）。游标全程用某个维度 `D`「记账」。
- **SeekTarget**（u2-l2）：目标位置与游标当前维度比较，`cmp` 返回 `Ordering`——`Greater` 表示目标还在前方（前进）、`Equal` 表示恰好落在边界上（交给 `Bias` 定夺）、`Less` 会被入口断言直接禁止。

在此基础上，本讲需要两个新直觉：

1. **树没有 O(1) 随机访问**。`SumTree` 不是数组，不能 `tree[i]`。要读第 i 个元素，必须从根走到叶子。如果每次读取都从根重走一遍，连续扫描的代价就是每次 \( O(\log n) \)。**游标（Cursor）就是把「从根到当前位置的路径」缓存下来的对象**——一次定位花 \( O(\log n) \)，之后相邻移动（`next`/`prev`）通常只动一两个栈条目，接近 \( O(1) \)。

2. **路径栈就像文件管理器的面包屑**。你打开 `C:\a\b\c\file.txt` 时，窗口里留着 `C: → a → b → c` 的路径；想换到兄弟目录 `d`，只需回退一层再下钻，而不必从 `C:` 重新找起。`Cursor` 的 `stack` 字段就是这条面包屑，每层记录「我在哪个节点、停在第几个孩子、进入它之前维度累计到了多少」。

另一个本讲反复用到的事实：在本 crate 自己的测试构建里 `TREE_BASE = 2`（u1-l3 讲过 `cfg(test)` 是 crate 局部的），所以每个节点最多 4 个元素/孩子，六七个元素就能长出两层结构，非常适合观察栈的行为。

## 3. 本讲源码地图

| 文件 | 在本讲中的角色 |
| --- | --- |
| `crates/sum_tree/src/cursor.rs` | 本讲主战场：`StackEntry`、`Cursor` 全部方法（`new`/`seek`/`next`/`item`/…）、`Iter`、`FilterCursor` 都定义在这里 |
| `crates/sum_tree/src/sum_tree.rs` | 提供 `Dimension`/`SeekTarget`/`Bias` 的定义、`SumTree::cursor` 入口方法，以及测试模块里的 `IntegersSummary`/`Count`/`Sum` 与现成的 `test_cursor` 测试 |

模块组织见 [crates/sum_tree/src/sum_tree.rs:L1-L13](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1-L13)：根模块以私有 `mod cursor;` 引入游标模块，再用 `pub use cursor::{Cursor, FilterCursor, Iter};` 精选导出，所以使用者只写 `sum_tree::Cursor`。

测试模块中的三个辅助类型会贯穿本讲所有实践，先温习一遍：

- [crates/sum_tree/src/sum_tree.rs:L1820-L1826](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1820-L1826) 定义 `IntegersSummary`（count/sum/contains_even/max 四个字段），是 `u8` 元素的汇总类型。
- [crates/sum_tree/src/sum_tree.rs:L1828-L1832](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1828-L1832) 定义 `Count(usize)` 与 `Sum(usize)` 两个维度类型，都派生了 `Ord`。
- [crates/sum_tree/src/sum_tree.rs:L1878-L1886](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1878-L1886) 为 `Count` 实现 `Dimension`：每遇到一段汇总就把其中的元素个数累加进来——这就是游标的「计数轴」。

## 4. 核心概念与源码讲解

先给一张本讲 API 速览表，后文逐个展开：

| API | 一句话语义 | 前置条件 |
| --- | --- | --- |
| `tree.cursor::<D>(cx)` | 创建按维度 `D` 记账的游标 | 无 |
| `cursor.seek(&target, bias)` | 重置后从根定位到 target | 无（可反复调用，可向后跳） |
| `cursor.seek_forward(&target, bias)` | 不重置，只能向前定位 | 已 seek 过 |
| `cursor.next()` / `cursor.prev()` | 向后/向前移动一个元素 | 无（首次调用分别等价于从头/从尾开始） |
| `cursor.item()` / `cursor.item_summary()` | 读当前元素/它的汇总，末尾返回 `None` | 已 seek 过 |
| `cursor.start()` / `cursor.end()` | 当前元素的起点/终点维度值 | 已 seek 过 |

### 4.1 Cursor 与 StackEntry：数组模拟的根到叶路径栈

#### 4.1.1 概念说明

`Cursor` 是 `SumTree` 的只读导航器。它不复制树、不修改树，只是借住在一棵树（`&SumTree<T>`）上，维护一条从根到某个叶子的路径。

路径的每一层是一个 `StackEntry`，三个字段：

- **`tree`**：这一层停着的节点（`&SumTree<T>`，即对 `Node` 的引用）。栈底是根节点，栈顶一定是叶子节点。
- **`index`**：停在该节点的第几个孩子（内部节点）或第几个元素（叶子）上。注意它是「下一个待处理的槽位」——`item()` 读的就是栈顶叶子的第 `index` 个元素。
- **`position`**：进入第 `index` 个孩子/元素**之前**的维度累计值（前缀和）。有了它，从路径上任何一层恢复「当前位置」都不必重扫整棵树。

为什么用定容数组而不是堆上的 `Vec`？因为树高有上界。非根节点至少有 `TREE_BASE`（正式构建为 6）个孩子，所以 h 层的树至少容纳 \( 6^{h} \) 量级的元素；\( 6^{16} \approx 2.8 \times 10^{12} \)，16 层已经远超任何现实规模。栈的容量 16 由此而来——不需要动态分配，`push` 处的 `unwrap_oob()` 把「绝不会溢出」变成显式断言。

#### 4.1.2 核心流程

以测试构建（`TREE_BASE = 2`）下 `extend(vec![1,2,3,4,5,6])` 建出的树为例：叶子最多 4 个元素，得到叶子 `[1,2,3,4]` 与 `[5,6]`，根是一个有两个孩子的内部节点。执行 `cursor.seek(&Count(4), Bias::Right)` 后，栈的内容是：

```text
栈（底 → 顶）             含义
┌─────────────────────────────────────────────┐
│ 根(Internal)  index=1  position=Count(4)     │  已吞掉第 0 个孩子(叶子[1,2,3,4])
├─────────────────────────────────────────────┤
│ 叶(Leaf[5,6]) index=0  position=Count(4)     │  停在第 0 个元素(5)上
└─────────────────────────────────────────────┘
cursor.position == Count(4)   ← 公共字段，等于栈顶所停元素的起点
```

游标需要维护的不变量：

1. `stack` 的长度等于当前叶子深度 + 1；栈底是根，栈顶是叶子。
2. 任何一次完成的 `seek` / `next` / `prev` 之后，栈顶必为叶子——源码里两处 `debug_assert!` 守着这条不变量（见 4.2.3 与 4.3.3 的链接）。
3. 每个条目的 `index` 是「当前停靠」的槽位；对非栈顶条目，`position` 严格等于进入第 `index` 个孩子之前的累计值（后退移动时要用它恢复游标位置）；对栈顶叶子，权威的起点是公共字段 `position`（即 `start()` 的返回值）。

#### 4.1.3 源码精读

[crates/sum_tree/src/cursor.rs:L6-L11](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L6-L11) 定义路径栈条目 `StackEntry`，就是上文说的 `tree` / `index` / `position` 三元组；[crates/sum_tree/src/cursor.rs:L13-L18](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L13-L18) 提供把 `u32` 下标转成 `usize` 的小助手（定容存储优先用小整数宽度）。

[crates/sum_tree/src/cursor.rs:L29-L37](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L29-L37) 是 `Cursor` 结构体本身，逐字段看：

- `tree: &'a SumTree<T>`——游标借住的树；生命周期 `'a` 保证了 `item()` 可以返回 `&'a T`（元素引用活得不比树短，也不依赖游标自身存活）。
- `stack: ArrayVec<StackEntry<'a, T, D>, 16, u8>`——容量 16 的定容路径栈（`heapless::Vec` 别名为 `ArrayVec`，u1-l2 讲过）。
- `pub position: D`——当前停靠位置的维度值。注意它是**公共字段**，但推荐用 `start()` 访问器读取。
- `did_seek` / `at_end`——两个状态位，见 4.4。
- `cx`——汇总的环境上下文（u2-l1 讲过 GAT `Context<'a>`；对 `ContextLessSummary` 恒为 `()`），生命周期 `'b` 与树无关。

[crates/sum_tree/src/cursor.rs:L64-L73](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L64-L73) 是构造函数 `Cursor::new`：新建的游标栈为空、`position` 为维度零元、`did_seek = false`，而 `at_end` 直接取 `tree.is_empty()`——空树的游标天生「已在末尾」。

真正暴露给用户的入口在树这一侧：[crates/sum_tree/src/sum_tree.rs:L597-L605](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L597-L605) 的 `SumTree::cursor` 通过泛型参数 `D`（常以 turbofish 写法 `tree.cursor::<Count>(())` 指定）选择记账维度。同一棵树配不同维度就是不同的游标：`cursor::<Count>` 用元素个数导航，`cursor::<Sum>` 用元素值之和导航，`cursor::<()>` 则完全不记账、只能顺序遍历。

顺带一提，[crates/sum_tree/src/cursor.rs:L39-L52](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L39-L52) 为 `Cursor` 实现了 `Debug`（打印 `stack`、`position`、`did_seek`、`at_end`，其中 `StackEntry` 的 `Debug` 特意不打 `tree` 字段，避免递归展开整棵子树），这让下一节的「打印游标内部」实践成为可能。

#### 4.1.4 代码实践

**实践目标**：亲眼看到路径栈在 seek 前后的变化。

**操作步骤**（示例代码，本地实验用，勿提交）：

1. 打开 `crates/sum_tree/src/sum_tree.rs`，在 `mod tests` 内（建议紧跟 `test_cursor` 之后）临时加入：

```rust
#[test]
fn test_inspect_cursor_stack() {
    let mut tree = SumTree::default();
    tree.extend(vec![1, 2, 3, 4, 5, 6], ());
    let mut cursor = tree.cursor::<Count>(());
    println!("seek 之前: {:?}", cursor);
    cursor.seek(&Count(4), Bias::Right);
    println!("seek 之后: {:#?}", cursor);
}
```

2. 在仓库根目录运行：`cargo test -p sum_tree test_inspect_cursor_stack -- --nocapture`。
3. 实验结束后用 `git checkout crates/sum_tree/src/sum_tree.rs` 还原。

**需要观察的现象**：seek 之前 `stack` 为空、`did_seek: false`；seek 之后 `stack` 有两个条目（测试构建下这棵树高为 1），`did_seek: true`。

**预期结果**（由源码语义推导，具体格式待本地验证）：seek 后应看到根条目 `index: 1`（第 0 个孩子整叶被吞掉）与叶子条目 `index: 0`，两者 `position` 与公共 `position` 均为 `Count(4)`；`at_end: false`。

#### 4.1.5 小练习与答案

**练习 1**：`StackEntry` 的 `Debug` 实现为什么只打印 `index` 和 `position`，不打印 `tree`？

答案：`tree` 是指向子树的引用，打印它会递归展开整棵子树，输出既冗长又无用；定位一层只需知道「停在第几个槽位、累计到多少」，这两个信息足以重建位置。

**练习 2**：路径栈容量为什么敢写死为 16？

答案：非根节点至少有 `TREE_BASE`（正式构建为 6）个孩子，树高 h 至少对应 \( 6^h \) 量级的元素数，16 层对应 \( 6^{16} \approx 2.8 \times 10^{12} \) 个元素，现实中不可能超出；定容数组还免去了每次进栈的堆分配。

**练习 3**：`Cursor` 有两个生命周期参数 `'a` 和 `'b`，分别属于谁？

答案：`'a` 属于被借住的树（以及从树里借出的元素引用，所以 `item()` 能返回 `&'a T`）；`'b` 属于汇总上下文 `cx`。二者独立，传一个短命的 `cx` 不会束缚树的借用。

### 4.2 seek 与 seek_forward：从根到叶的定位

#### 4.2.1 概念说明

`seek` 回答的问题是：「按维度 `D` 走到位置 target，停在那里」。它是游标的定位原语，也是 `slice`、`summary`（下一讲的主角）共用的底层引擎。

两个变体的区别只在于**是否重置**：

- `seek`：先 `reset()`（清栈、位置归零），再从根下钻。因此可以反复调用、可以跳到比当前更靠前的位置。
- `seek_forward`：不重置，从当前栈继续向前。用在「已经定位过一次、接下来只向后推进」的场景（例如先 seek 到区间起点，再 seek_forward 到区间终点），省去重新从根下钻的开销。目标若在当前位置之前，会触发 `"cannot seek backward"` 断言。

二者的返回值是 `bool`：**是否精确命中**——目标是否恰好落在某个元素的边界上。目标越过树尾（比如对 6 个元素 seek `Count(100)`）时返回 `false`，游标停在末尾。

另外注意：seek 过程中每「吞掉」一个孩子或元素，都会调用一个 `aggregate` 钩子的 `push_tree` / `push_item`。普通 `seek` 传入的是空实现 `()`，什么都不记；`slice` 和 `summary` 传入会记账的实现，于是定位与聚合共用同一趟遍历。这是下一讲的主题，本讲只需认出这个钩子的存在。

#### 4.2.2 核心流程

`seek_internal` 的骨架可以概括为（伪代码）：

```text
断言 target >= 当前 position        # 不允许向后
若从未 seek 过: push 根节点条目 (index=0)

循环（栈顶条目 entry）:
    若 entry 是内部节点:
        从 entry.index 起逐个看孩子:
            child_end = position + child_summary
            若 target > child_end 或 (target == child_end 且 bias == Right):
                吞掉这个孩子: position = child_end, index += 1
                aggregate.push_tree(...)          # 记账钩子
            否则:
                push 这个孩子, 下钻一层            # 目标在这个孩子内部
                继续外层循环
        孩子扫完仍没停 -> 栈顶出栈, 上升一层继续    # 目标在本节点末尾之外
    若 entry 是叶子:
        同样按 item_summaries 逐元素吞, 吞不动的就是停靠元素
        停下时 break

at_end = (栈空)
返回 (target == 游标终点维度值)       # 是否精确命中
```

「吞掉」的判定条件值得多看一眼：`target > child_end` 意味着目标在这个孩子结束之后，整个孩子都在目标左侧，可以整块跳过——这正是汇总带来的加速：内部节点一层最多比较 \( 2 \times \text{TREE\_BASE} \) 次，总代价 \( O(C \cdot \log_C n) \)，\( C = 2 \times \text{TREE\_BASE} \)。`Equal` 的情况（目标恰好压在孩子边界上）交给 `Bias`：`Bias::Right` 把边界让给左侧（吞掉），`Bias::Left` 留给右侧（下钻）。完整的边界语义表是下一讲（u3-l2）的内容，本讲先记住这个分支的存在即可。

#### 4.2.3 源码精读

[crates/sum_tree/src/cursor.rs:L408-L414](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L408-L414) 是 `seek`：先 `reset()` 再委托 `seek_internal`，聚合计传入空实现 `&mut ()`。

[crates/sum_tree/src/cursor.rs:L423-L428](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L423-L428) 是 `seek_forward`：不 reset，直接 `seek_internal`；文档注释明确标注了「未 seek 过时应改用 seek」以及可能 panic 的情形。

[crates/sum_tree/src/cursor.rs:L471-L474](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L471-L474) 是入口断言 `target.cmp(&self.position).is_ge()`——`SeekTarget` 不允许 `Less`（u2-l2 讲过的约定在这里变成运行时守卫）。

[crates/sum_tree/src/cursor.rs:L476-L485](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L476-L485) 处理首次 seek：把根节点压栈（`index = 0`、`position` 为维度零元），并置 `did_seek = true`。

[crates/sum_tree/src/cursor.rs:L487-L527](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L487-L527) 是内部节点分支：从 `entry.index` 起用 `child_summaries` 逐孩子比较，`L500-L514` 是「吞掉整个孩子」的分支（推进 `position`、记账、`index += 1`、回写 `entry.position`），`L515-L525` 是「目标在这个孩子内部」的分支（压栈下钻、`continue 'outer`）。`ascending` 标志在 `L495-L498` 控制「刚从下层回到本层时先把 `index` 前移一格再继续扫」。

[crates/sum_tree/src/cursor.rs:L528-L556](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L528-L556) 是叶子分支：同样的吞元素逻辑，但粒度是单个 `item_summary`；吞不动的那个元素就是停靠点，`break 'outer` 跳出整个循环。注意这里只推进 `entry.index` 而不回写 `entry.position`——栈顶叶子的权威起点由公共字段 `position` 承担（见 4.1.2 的不变量说明）。

[crates/sum_tree/src/cursor.rs:L559-L564](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L559-L564) 是收尾：本层扫完仍没停就出栈上升；循环结束后 `at_end = stack.is_empty()`，并用 `debug_assert!` 守住「栈非空则栈顶必为叶子」的不变量。

[crates/sum_tree/src/cursor.rs:L566-L573](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L566-L573) 计算返回值：先取当前 `position`，若 `Bias::Left` 且停在某元素上，则把该元素的汇总加上得到「游标终点」，最后返回 `target == end`。这就是「是否精确命中」的实现。

最后看目标类型如何与游标维度解耦（u2-l2 的伏笔在此落地）：

- [crates/sum_tree/src/sum_tree.rs:L126-L130](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L126-L130) 是万能实现：任何「自身就是维度且可 `Ord`」的类型都能直接当目标用——`cursor::<Count>` 配 `&Count(n)` 走的就是它。
- [crates/sum_tree/src/sum_tree.rs:L1888-L1892](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1888-L1892) 则是测试里手写的实现：让 `Count` 能当「维度为完整 `IntegersSummary` 的游标」的目标（只比较 `count` 字段）。`test_cursor` 里 `cursor.slice(&Count(2), Bias::Right)`（[crates/sum_tree/src/sum_tree.rs:L1666-L1670](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1666-L1670)）正是这种组合：游标按完整汇总记账，目标却只是一个计数。

#### 4.2.4 代码实践

**实践目标**：系统观察 `seek` 的返回值、停靠位置与越界行为，并验证 `seek_forward` 不能后退。

**操作步骤**（示例代码，本地实验用，勿提交；加入 `mod tests`）：

```rust
#[test]
fn test_seek_observations() {
    let mut tree = SumTree::default();
    tree.extend(vec![1, 2, 3, 4, 5, 6], ());

    for n in 0..=7 {
        let mut cursor = tree.cursor::<Count>(());
        let found = cursor.seek(&Count(n), Bias::Right);
        println!(
            "n={n} found={found} start={:?} item={:?}",
            cursor.start(),
            cursor.item()
        );
    }

    let mut cursor = tree.cursor::<Count>(());
    cursor.seek(&Count(5), Bias::Right);
    cursor.seek_forward(&Count(6), Bias::Right); // 向前，正常
    cursor.seek_forward(&Count(1), Bias::Right); // 向后，预期 panic
}
```

运行：`cargo test -p sum_tree test_seek_observations -- --nocapture`（最后一行会 panic，属预期现象；可分两次注释运行）。

**需要观察的现象**：`n` 从 0 到 6 时 `found` 均为 `true` 且 `start` 恰为 `Count(n)`；`n = 7` 时 `found` 变为 `false`、`item()` 为 `None`；最后的 `seek_forward` 向后跳触发 `"cannot seek backward"` panic。

**预期结果**（由源码语义推导，待本地验证）：`n=0..6` 依次停在元素 1..6 与末尾（`n=6` 时 `item` 为 `None`，因为 6 个元素全部被吞掉）；`n=7` 时游标停在 `Count(6)`（树尾），返回 `false`。

#### 4.2.5 小练习与答案

**练习 1**：`seek` 与 `seek_forward` 的本质区别是什么？什么时候必须用 `seek`？

答案：`seek` 先 `reset()` 清栈归零再从根下钻，因此可以反复定位、也可以跳到当前位置之前；`seek_forward` 保留现有栈从当前位置继续，只允许向后（维度增大方向）推进，目标靠前会 panic。需要「向后跳」或「重新定位」时必须用 `seek`；已知单调向前的连续定位（如先到区间起点再到终点）用 `seek_forward` 更省。

**练习 2**：`seek` 返回 `false` 说明什么？游标此时停在哪里？

答案：说明目标没有精确落在任何元素边界上（典型情况是目标越过了树尾）。游标停在它能到达的最远位置——通常是树尾，`position` 等于全树的维度值（如 `Count(6)`），`item()` 返回 `None`。

**练习 3**：为什么 `seek_internal` 宁可断言失败也不支持向后 seek？

答案：整套机制——`position` 的前缀和记账、`aggregate` 钩子的「吞掉即聚合」——都建立在单调前进的假设上；支持后退意味着要么从根重新下钻（那正是 `seek` 做的），要么维护反向账本，复杂度换不来收益。用断言把误用变成显式失败，符合「尽早暴露不变量破坏」的设计取向（与 u1-l2 讲过的 `unwrap_oob` 同一思路）。

### 4.3 next 与 prev：游标漫游

#### 4.3.1 概念说明

`next()` 和 `prev()` 把游标向前/向后移动一个元素。它们其实是两个更通用方法的退化形式：

- `next()` ≡ `search_forward(|_| true)`——谓词恒真，即「不跳过任何东西，走一格」。
- `prev()` ≡ `search_backward(|_| true)`——同理。

而 `search_forward` / `search_backward` 带一个 `FnMut(&T::Summary) -> bool` 谓词，允许在节点级别整块跳过不满足条件的子树——那就是 u3-l4 要讲的 `FilterCursor`。本讲把谓词恒真的路径读透，带谓词的版本只是同一台机器换个刹车。

除了移动游标本尊，还有一对「偷看但不移动」的方法 `next_item()` / `prev_item()`：分别返回下一个/上一个元素的引用，游标原地不动。`test_cursor` 用它们密集地验证每次移动后的邻域关系。

另外，`Cursor` 还实现了标准库的 `Iterator` trait，可以直接 `collect` 或进 `for` 循环——`test_random` 里就有 `tree.iter()` 与 `tree.cursor::<()>(()).collect()` 的对拍（见下文链接）。

#### 4.3.2 核心流程

`next()` 的一般情形只是「栈顶叶子的 `index` 加一」。有意思的是跨叶子的时刻：

```text
next() 发现栈顶叶子的 index 已到叶子末尾:
    pop 掉叶子条目, 上升一层
    父条目 index += 1
    若父节点还有孩子: push 该孩子 (index=0), 一路下钻到新叶子的第 0 个元素
    若父节点也到末尾: 继续上升……
栈空 -> at_end = true
```

所以相邻移动多数情况 \( O(1) \)，跨叶子边界时付出 \( O(\text{树高}) \) 的调整，均摊后整趟顺序扫描仍是线性级别。

`prev()` 的主循环与上面对称（`index` 减一、出栈时把父条目 `index` 回退），但它多一个**特殊分支**：当游标「从未 seek 过」或「已停在末尾」（`at_end == true`）时调用 `prev()`，语义是「从树尾出发向前走」。此时栈是空的，无法「回退」，于是这个分支反方向构造路径：压入根条目、`index` 置为最后一个孩子的下一位、`position` 置为全树维度值，然后一路向右下钻到最右叶子的最后一个元素。这就是为什么对新鲜游标直接调 `prev()` 会落在**最后一个**元素上，而不是第一个。

#### 4.3.3 源码精读

[crates/sum_tree/src/cursor.rs:L290-L293](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L290-L293) 定义 `next()` 为谓词恒真的 `search_forward`。

[crates/sum_tree/src/cursor.rs:L296-L314](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L296-L314) 是 `search_forward` 的起点：栈空且未到末尾时压入根条目并置 `descend = true`；栈空且 `at_end` 时什么都不做（空树或已到尾，`next` 成为空操作）。

[crates/sum_tree/src/cursor.rs:L316-L381](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L316-L381) 是主循环：`L319-L342` 内部节点分支——先把 `index` 前移一格（`L325-L328`），再用谓词逐孩子扫描跳过不匹配的子树（`L330-L339`，跳过时同步推进 `entry.position` 与公共 `position`），返回要下钻的孩子；`L343-L364` 叶子分支——同样的逐元素扫描，谓词命中即 `return` 停靠；`L368-L380` 决定「有孩子则压栈下钻，无孩子则出栈上升」。谓词恒真时，内部节点扫第一个孩子就停、叶子扫第一个元素就停，退化为「走一格」。

[crates/sum_tree/src/cursor.rs:L383-L385](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L383-L385) 收尾：`at_end = stack.is_empty()`，并 `debug_assert!` 栈顶必为叶子。

[crates/sum_tree/src/cursor.rs:L214-L218](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L214-L218) 定义 `prev()`；[crates/sum_tree/src/cursor.rs:L225-L242](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L225-L242) 就是上文的 `at_end` 特殊分支——从未 seek 过时先把 `did_seek` 置真并当作「在末尾」，然后把 `position` 归零、压入根条目（`index` 为孩子数，即最右孩子之后），准备向左下钻。[crates/sum_tree/src/cursor.rs:L244-L287](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L244-L287) 是向后主循环：每层先用上一层的 `position`（`L246-L250` 从次栈顶条目恢复）加上本层孩子汇总重建前缀（`L262-L265`），`index` 回退一格，谓词命中则下钻、否则继续回退，一路走到目标叶子。

[crates/sum_tree/src/cursor.rs:L138-L159](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L138-L159) 是 `next_item()`：若当前元素不是叶子末位，直接读 `items[index + 1]`；否则借 [crates/sum_tree/src/cursor.rs:L161-L174](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L161-L174) 的 `next_leaf()` 沿栈上找有右兄弟的层，取右兄弟子树的最左叶子首元素。[crates/sum_tree/src/cursor.rs:L176-L197](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L176-L197) 的 `prev_item()` 与 [crates/sum_tree/src/cursor.rs:L199-L212](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L199-L212) 的 `prev_leaf()` 完全对称。

[crates/sum_tree/src/cursor.rs:L659-L677](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L659-L677) 是 `Iterator for Cursor` 的实现：`next` 先确保 seek 过（没有就先挪一格），再取 `item()`、推进游标、返回元素。项目里的现成对拍在 [crates/sum_tree/src/sum_tree.rs:L1473-L1476](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1473-L1476)：`tree.iter().collect()` 与 `tree.cursor::<()>(()).collect()` 逐元素相等。

#### 4.3.4 代码实践

**实践目标**：用 `next`/`prev` 完成整树的正序与逆序遍历，并与参考实现对拍。

**操作步骤**（示例代码，本地实验用，勿提交；加入 `mod tests`）：

```rust
#[test]
fn test_walk_with_cursor() {
    let mut tree = SumTree::default();
    tree.extend(vec![1, 2, 3, 4, 5, 6], ());

    // 正序：游标作为 Iterator
    let forward: Vec<u8> = tree.cursor::<()>(()).collect();
    assert_eq!(forward, tree.items(()));

    // 逆序：新鲜游标 + 反复 prev()
    let mut cursor = tree.cursor::<Count>(());
    let mut backward = Vec::new();
    loop {
        cursor.prev();
        match cursor.item() {
            Some(item) => backward.push(*item),
            None => break,
        }
    }
    let mut expected = tree.items(());
    expected.reverse();
    assert_eq!(backward, expected);
}
```

运行：`cargo test -p sum_tree test_walk_with_cursor`。实验后还原文件。

**需要观察的现象**：两条断言均通过；第一次 `prev()` 后 `item()` 拿到的是 6（最后一个元素），印证 4.3.2 说的 `at_end` 分支从树尾出发。

**预期结果**：正序 `[1,2,3,4,5,6]`，逆序 `[6,5,4,3,2,1]`。可再打印每次 `prev()` 后的 `start()` 观察 `Count` 从 15 一路降到 0（与 [crates/sum_tree/src/sum_tree.rs:L1697-L1737](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1697-L1737) 中 `test_cursor` 的既有断言一致）。

#### 4.3.5 小练习与答案

**练习 1**：`next()` 从一个叶子的最后一个元素跨到下一个叶子的第一个元素，栈发生了什么？

答案：叶子条目 `index` 到达末尾后出栈；父条目 `index += 1`；若有右兄弟则把右兄弟压栈并一路压到其最左叶子（`index = 0`）；若某层也没有右兄弟则继续出栈上升。栈空时置 `at_end = true`。

**练习 2**：对从未 seek 的新鲜游标调用 `prev()`，为什么落在最后一个元素而不是第一个？

答案：新鲜游标 `did_seek = false`，`search_backward` 把它当作「在末尾」处理（置 `at_end = true`），压入根条目并把 `index` 放到最右孩子之后、`position` 设为全树维度，然后向左下钻到最右叶子的最后一个元素。「向后走一格」的语义在「从末尾出发」的语境下自然得到最后一个元素。

**练习 3**：`Cursor` 实现了 `Iterator` 后，`for x in &mut cursor { .. }` 调用的是哪个 `next`？

答案：调用 `Iterator::next`（trait 方法），其内部再调用同名的固有方法 `next`（Rust 中固有方法优先于 trait 方法），也就是 4.3 讲的「移动一格」逻辑；实现里还处理了「从未 seek 过则先挪一格」的引导步骤（[crates/sum_tree/src/cursor.rs:L665-L676](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L665-L676)）。

### 4.4 item、item_summary、start、end 与「先 seek」契约

#### 4.4.1 概念说明

定位与移动之外，剩下的是「读出游标状态」的四个方法，它们全都只读栈顶条目：

- `start()`：当前元素的**起点**维度值（就是公共字段 `position` 的借用）。
- `end()`：当前元素的**终点**维度值 = `start` + 当前元素汇总；停在末尾时等于 `start`。
- `item()`：当前元素引用；**树为空或游标在末尾时返回 `None`**（不 panic）。
- `item_summary()`：当前元素的汇总引用，`None` 条件同上。

它们共同遵守一条契约：**调用前必须先 `seek` / `next` / `prev`**。契约由 `did_seek` 状态位记录、由 `assert_did_seek` 在每个读取方法入口强制执行——忘了 seek 就读取，会得到一句明确的 panic 信息，而不是被静默吞掉的 `None`。

`at_end` 位则记录「游标是否已越过最后一个元素」（栈空）。它与 `item() == None` 高度相关但不等同：`at_end` 是内部导航状态，`item()` 是对外查询结果；二者的同步点在 `seek_internal` 与 `search_forward` 的收尾（`at_end = stack.is_empty()`）。

#### 4.4.2 核心流程

| 调用 | 前置条件 | 返回 | 末尾行为 |
| --- | --- | --- | --- |
| `cursor.start()` | 已 seek | `&D`，当前元素起点 | 等于全树维度值 |
| `cursor.end()` | 已 seek | `D`，起点+元素汇总 | 等于 `start()` |
| `cursor.item()` | 已 seek | `Option<&'a T>` | `None` |
| `cursor.item_summary()` | 已 seek | `Option<&'a T::Summary>` | `None` |
| `cursor.did_seek()` | 无 | `bool`（公开探针，不 panic） | — |

读取路径本身极短：`item()` 取栈顶条目，按 `entry.index` 直接索引叶子里的 `items` 数组；`index == items.len()`（吞完了所有元素）或栈空时返回 `None`。一次读取 \( O(1) \)。

#### 4.4.3 源码精读

[crates/sum_tree/src/cursor.rs:L82-L84](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L82-L84) 的 `start()` 就是返回公共字段 `position` 的引用。

[crates/sum_tree/src/cursor.rs:L86-L95](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L86-L95) 的 `end()`：拿得到元素汇总就把 `start` 克隆一份加上它，拿不到（末尾）就原样返回 `start`。

[crates/sum_tree/src/cursor.rs:L97-L115](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L97-L115) 的 `item()`：先 `assert_did_seek()`，再看栈顶——文档注释（`L97`）明说「列表为空或游标在末尾时返回 `None`」；`match` 的 `_ => unreachable!()` 分支表达的是「栈顶必为叶子」这条 4.1.2 的不变量。

[crates/sum_tree/src/cursor.rs:L117-L136](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L117-L136) 的 `item_summary()` 与 `item()` 逐行同构，只是索引 `item_summaries` 数组——u1-l2 讲过的「汇总与元素平行存放」在这里兑现：读汇总不必经过元素。

[crates/sum_tree/src/cursor.rs:L387-L393](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L387-L393) 是 `assert_did_seek`：`did_seek` 为假即 panic，信息为 "Must call `seek`, `next` or `prev` before calling this method"。[crates/sum_tree/src/cursor.rs:L395-L397](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L395-L397) 另提供不 panic 的 `did_seek()` 探针，供调用方先探测再读取。

行为基准可对照现成测试 [crates/sum_tree/src/sum_tree.rs:L1666-L1676](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1666-L1676)：对 `[1..6]` 的树切片到 `Count(2)` 后，`item()` 是 `Some(&3)`、`start().sum == 3`（1+2 的前缀和）、`next_item()` 是 `Some(&4)`——起点、当前元素、邻域三者互相印证。

#### 4.4.4 代码实践

**实践目标**：实现本讲的主任务 `nth_item`——按索引取第 n 个元素，覆盖首、中、尾、越界四种情况。

**操作步骤**（示例代码，本地实验用，勿提交；加入 `mod tests`）：

```rust
fn nth_item(tree: &SumTree<u8>, n: usize) -> Option<u8> {
    let mut cursor = tree.cursor::<Count>(());
    cursor.seek(&Count(n), Bias::Right);
    cursor.item().copied()
}

#[test]
fn test_nth_item() {
    let mut tree = SumTree::default();
    tree.extend(0..10, ());

    assert_eq!(nth_item(&tree, 0), Some(0));  // 首
    assert_eq!(nth_item(&tree, 4), Some(4));  // 中
    assert_eq!(nth_item(&tree, 9), Some(9));  // 尾（最后一个元素）
    assert_eq!(nth_item(&tree, 10), None);    // 恰好越界（== 元素个数）
    assert_eq!(nth_item(&tree, 255), None);   // 远越界：停在树尾，不 panic
    assert_eq!(nth_item(&SumTree::<u8>::default(), 0), None); // 空树
}
```

运行：`cargo test -p sum_tree test_nth_item`。实验后还原文件。

**需要观察的现象**：全部断言通过；越界时 `seek` 内部只是吞掉所有元素后停在树尾，`item()` 平静地返回 `None`，全程无 panic。

**预期结果**：如断言所列。关键机制：`Bias::Right` 使「结束边界恰为 n」的元素被吞掉，于是游标起点恰好是第 n 个元素（0 基）；若换成 `Bias::Left`，`nth_item(tree, n)` 将返回第 n-1 个元素——可自行改动验证（这也是 u3-l2 的预告）。

#### 4.4.5 小练习与答案

**练习 1**：`item()` 在哪些情况下返回 `None` 而不是 panic？

答案：两种——树为空（栈始终为空）；或游标停在末尾（栈空或栈顶叶子的 `index == items.len()`，即最后一个元素刚被吞掉）。见 [crates/sum_tree/src/cursor.rs:L97-L115](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L97-L115) 的两个 `None` 出口。

**练习 2**：`end()` 的值由什么组成？为什么它不依赖 seek 时用的 `Bias`？

答案：`end = start + 当前元素汇总`（拿不到元素汇总时退化为 `start`）。它只是「起点加当前元素长度」的算术，与定位时边界归属给左还是给右无关；`Bias` 影响的是停在哪个元素（`start` 是多少），而不是「起点+长度」这个加法本身。

**练习 3**：假如去掉 `assert_did_seek`，未 seek 就调 `item()` 会发生什么？为什么仍要保留这个断言？

答案：栈为空时 `stack.last()` 返回 `None`，方法静默返回 `None`——一个逻辑 bug 会被伪装成「位置在末尾」的假象，很难排查。断言把误用提前到第一现场变成显式 panic，符合「尽早暴露不变量破坏」的设计取向。

## 5. 综合实践

把本讲全部内容串起来：实现一个按索引区间取元素的函数 `range_items`，用 `seek` 定位起点、`next()` 逐个推进、`start()` 判断终点，最后与 `cursor.slice`（下一讲的主角，这里只当对拍基准）以及 `Vec` 切片互相印证——这正是 `test_random` 中 splice 操作的骨架（先切前段、再跳后段，见 [crates/sum_tree/src/sum_tree.rs:L1459-L1470](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1459-L1470)）。

（示例代码，本地实验用，勿提交；加入 `mod tests`）

```rust
fn range_items(tree: &SumTree<u8>, range: std::ops::Range<usize>) -> Vec<u8> {
    let mut cursor = tree.cursor::<Count>(());
    cursor.seek(&Count(range.start), Bias::Right);
    let mut result = Vec::new();
    while cursor.start().0 < range.end {
        match cursor.item() {
            Some(item) => result.push(item.clone()),
            None => break, // 越过树尾
        }
        cursor.next();
    }
    result
}

#[test]
fn test_range_items() {
    let mut tree = SumTree::default();
    tree.extend(0..10, ());

    assert_eq!(range_items(&tree, 2..5), vec![2, 3, 4]);
    assert_eq!(range_items(&tree, 0..10), tree.items(()));
    assert_eq!(range_items(&tree, 7..10), vec![7, 8, 9]);
    assert_eq!(range_items(&tree, 5..15), vec![5, 6, 7, 8, 9]); // 终点越界
    assert_eq!(range_items(&tree, 3..3), Vec::<u8>::new());     // 空区间

    // 与 slice 对拍：同为「取 [2,5) 的元素」
    let mut cursor = tree.cursor::<Count>(());
    cursor.seek(&Count(2), Bias::Right);
    assert_eq!(cursor.slice(&Count(5), Bias::Right).items(()), vec![2, 3, 4]);
}
```

运行：`cargo test -p sum_tree test_range_items`，完成后还原源文件。

要点自查：

1. 循环条件为什么用 `start() < range.end` 而不是计数器？（提示：`start()` 就是游标走过多少元素的「免费」账本，这正是维度记账的意义。）
2. 把 `cursor.next()` 换成 `Iterator` 的用法（`for` 循环）可行吗？需要改哪些地方？（提示：`Iterator` 会先「挪一格」再取元素，与「先读后挪」的顺序不同。）
3. `range.start > range.end` 时函数行为如何？（不 panic，返回空——想想为什么循环条件天然容忍。）

## 6. 本讲小结

- `Cursor` 的核心是容量 16 的 `StackEntry` 路径栈：每层记录 `tree`（停在哪层节点）、`index`（停在第几个孩子/元素）、`position`（进入它之前的维度前缀和）；栈底是根，栈顶必为叶子。
- `seek` = 重置 + 从根下钻；`seek_forward` 不重置、只许前进（`"cannot seek backward"` 断言）。定位沿「吞掉整个在目标左侧的孩子」推进，每层最多比较 \( 2 \times \text{TREE\_BASE} \) 次。
- `next`/`prev` 是谓词恒真的 `search_forward`/`search_backward`，跨叶子时靠栈的上升/下钻调整；新鲜游标的 `prev()` 从树尾出发，落在最后一个元素上；`Cursor` 还实现了 `Iterator` 可直接 collect。
- `item`/`item_summary`/`start`/`end` 全部只读栈顶，\( O(1) \)；末尾统一返回 `None` 而不 panic。
- `did_seek` 与 `at_end` 两个状态位分别守护「先 seek 再读取」契约（`assert_did_seek`）与「已越过树尾」的导航状态；`did_seek()` 提供不 panic 的探测。
- 目标类型与游标维度解耦：`cursor::<Count>` 可配 `&Count(n)`（万能 `Ord` 实现），`cursor::<IntegersSummary>` 也可配 `&Count(n)`（测试手写的 `SeekTarget` 实现）。

## 7. 下一步学习建议

- 下一讲（u3-l2）深入 `Bias`：本讲多次出现的「`Equal` 且 `Bias::Right` 则吞掉」分支，在 `seek`/`slice`/`find` 中如何系统性地决定边界归属，推荐对照 [crates/sum_tree/src/sum_tree.rs:L167-L204](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L167-L204) 的文档注释（`AˇBCD` 光标示例）预习。
- 再下一讲（u3-l3）拆解本讲埋下的 `aggregate` 钩子：`slice`/`suffix`/`summary` 如何在同一趟 `seek_internal` 里顺带建树或聚合维度。
- 想看生产级用法，可去 `crates/rope/src/rope.rs` 中搜索 `cursor::<` 与 `seek`，观察 `TextSummary` 多维度游标如何按字节偏移、行列、UTF-16 三种坐标定位——那是本讲机器的全功率形态。

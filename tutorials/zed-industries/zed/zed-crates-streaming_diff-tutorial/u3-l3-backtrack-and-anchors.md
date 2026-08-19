# 回溯与锚点：从最优终点还原编辑脚本

## 1. 本讲目标

上一讲（u3-l2）我们读完了 `push_new` 的前两段：矩阵滚动与增量填表。当时把 `backtrack` 当作黑盒，只说了一句「先回溯、后推进锚点」。本讲打开这个黑盒，学完后你应当能够：

1. 解释 `push_new` 末段的**终点搜索**：为什么要在最后一列里找分数最高的那一行作为新的旧侧锚点 `old_text_ix`，而新侧锚点 `new_text_ix` 永远直接推到当前新文本末尾。
2. 逐步跟踪 `backtrack` 的反向决策循环：三种候选前驱（插入 / 删除 / 相等）如何构造、如何用 `max_by_key` 选出胜者。
3. 讲清楚两个「紧凑化」技巧：`pending_insert` 如何把连续多个插入步合并成一个携带完整文本的 `Insert`，以及 Delete / Keep 如何与最后一个 hunk 就地合并。
4. 说明 `OrderedFloat` 为什么必须存在——`f64` 只有 `PartialOrd`，而 `max_by_key` 的键要求 `Ord`。

本讲全程使用一个贯穿示例：`old = "aaaa\nbbbb"`、`new = "aaaa\nBBBB"`（第二行整体被替换，`\n` 保留）。我们会手算出整条回溯路径，并在综合实践中用测试验证。

## 2. 前置知识

本讲默认你已读过前三讲的结论，这里只做最小回顾：

- **DP 状态**（u3-l1）：分数矩阵的第 \(i\) 行第 \(j\) 列记作 \(S(i,j)\)，表示「旧文本前 \(i\) 个字符与新文本前 \(j\) 个字符的最优对齐分数」。矩阵按列填表、只保留当前一轮的列（u3-l2 的滚动技巧）。
- **打分常量**（u2-l2）：插入 \(I=-1\)，删除 \(D=-20\)，相等奖励 \(B(r)=1.8^{\min(\lfloor r/4\rfloor,\,16)}\)，其中 \(r\) 是连续相等游程长度。分数全为「负代价 + 正奖励」。
- **锚点**：`(old_text_ix, new_text_ix)` 是两个 `usize` 字段（[src/streaming_diff.rs:L118-L119](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L118-L119)），表示「旧 / 新文本各自已被前面各轮 `push_new` 结算掉的前缀长度」。当前矩阵的第 0 列存的就是锚点列的分数。
- **CharOperation**（u1-l2）：`Insert { text }` 携带新文本，`Delete { bytes }` / `Keep { bytes }` 只带旧文本的字节数。本讲会出现「hunk」这个词，指操作序列里的一个元素（一段连续的编辑）。
- **Rust 背景**：`Option<T>` 实现了 `Ord` 且 `None < Some(_)`；`Iterator::max_by_key` 要求键 `K: Ord`，并且**并列时返回最后一个**元素（这与 `min_by_key` 取第一个相对）。这两点是读懂回溯循环 tie-break 的钥匙。

一个值得先建立的直觉：**回溯的正确性来自「路径合法」，而不是「路径最优」**。任何一条从锚点走到终点、每步只消费一个新字符（插入步）、或一个旧字符（删除步）、或一对相等字符（相等步）的路径，翻译成 `CharOperation` 后应用到旧文本都必然重建出新文本——因为新文本的每个字符恰好被发射一次，旧文本的每个字符要么保留要么跳过。分数比较只决定 diff「长得好不好看」（hunk 是否贴合人的直觉），不影响 round-trip 正确性。记住这一点，后面读到一些「不严格重现前向递推」的细节时就不会困惑。

## 3. 本讲源码地图

本讲几乎全部内容都在同一个文件里，但它同时是库根和测试容器：

| 文件 / 代码段 | 行号 | 作用 |
|---|---|---|
| [Cargo.toml:L14-L16](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/Cargo.toml#L14-L16) | 14-16 | 运行期依赖只有 `ordered-float` 与 `rope`，本讲的 `OrderedFloat` 来自前者 |
| [src/streaming_diff.rs:L1](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L1) | 1 | `use ordered_float::OrderedFloat;`——全 crate 仅两处使用（导入 + L230） |
| [src/streaming_diff.rs:L93-L104](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L93-L104) | 93-104 | `Matrix` 的 `Debug` 实现，按行列印整个矩阵，实践时用它观察分数表 |
| [src/streaming_diff.rs:L184-L193](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L184-L193) | 184-193 | **终点搜索**：在最后一列找最大分行 |
| [src/streaming_diff.rs:L195-L198](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L195-L198) | 195-198 | 调用 `backtrack`，然后才推进两个锚点并返回 hunks |
| [src/streaming_diff.rs:L201-L274](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L201-L274) | 201-274 | **backtrack 主体**：反向决策 + hunk 合并 |
| [src/streaming_diff.rs:L276-L279](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L276-L279) | 276-279 | `finish`：把终点强制钉在 `(old.len(), new.len())` 再回溯（下一讲主角，本讲做对照） |
| [src/streaming_diff.rs:L1104-L1124](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L1104-L1124) | 1104-1124 | 测试模块里的 `apply_char_operations` 参考解释器，综合实践要用它做 round-trip 验证 |

## 4. 核心概念与源码讲解

### 4.1 终点搜索：在最后一列里找分数最高的旧行

#### 4.1.1 概念说明

经典 Needleman-Wunsch 的全局对齐要求路径从 \((0,0)\) 走到 \((m,n)\)——旧文本必须被「用完」。而 `push_new` 是流式的：本轮到达的新文本只是半成品，**旧文本的尾部很可能要留给未来的新块去匹配**。所以这里采用「半全局对齐」（free end-gap）：

- **新侧不自由**：`push_new(text)` 收到的文本已经真实到达，必须全部被本轮 diff 覆盖，所以终点固定在最后一列，`next_new_text_ix = self.new.len()`。
- **旧侧自由**：终点可以在 `old_text_ix..=old.len()` 的任意一行。消费一个匹配不上的旧字符要付 \(-20\) 的删除分，而「先不消费」是零成本——账单推迟到 `finish()` 强制钉死终点时再结。

「在最后一列挑分数最高的行」就是在「多保留一些匹配的旧字符（加分）」和「被迫删除垃圾旧字符（重罚）」之间自动找平衡。这也解释了 u2-l2 讲过的打分不对称（删除贵 20 倍）是如何直接塑造锚点位置的：锚点倾向于停在「旧文本与已到达新文本的最长匹配」之后一点，而把后面暂时对不上的旧文本原样挂起。

#### 4.1.2 核心流程

```text
输入：已填好的矩阵（行 0..=old.len()，最后一列 j_last = new.len() - new_text_ix）
输出：新锚点 (next_old_text_ix, next_new_text_ix)

max_score        = -∞
next_old_text_ix = old_text_ix        # 保底值：本轮一个旧字符也不消费
next_new_text_ix = new.len()          # 新侧无条件推到末尾
for i in old_text_ix ..= old.len():
    if S(i, j_last) > max_score:      # 注意是严格大于
        max_score        = S(i, j_last)
        next_old_text_ix = i

hunks = backtrack(next_old_text_ix, next_new_text_ix)   # 先回溯
old_text_ix = next_old_text_ix                          # 后推进锚点
new_text_ix = next_new_text_ix
return hunks
```

两个 tie-break 细节：

- 扫描用严格 `>`，因此**并列时保留最先遇到（最小的 \(i\)）**——即并列时倾向少消费旧文本。
- 循环下界从 `old_text_ix` 开始，锚点单调不减（u3-l2 已建立的不变量），已结算过的旧前缀永远不会被重新结算。

#### 4.1.3 源码精读

终点搜索本体：

- [src/streaming_diff.rs:L184-L193](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L184-L193) — `max_score` 初始化为 `f64::NEG_INFINITY`；`next_old_text_ix` 的保底值是当前旧锚点；`next_new_text_ix` 直接取 `self.new.len()`；随后在 `self.old_text_ix..=self.old.len()` 的行区间里读取最后一列（注意 `get` 的列参数是**相对列号** `next_new_text_ix - self.new_text_ix`，因为矩阵每轮都从锚点列重新起算，见 u3-l2），用严格 `>` 刷新最大值与行号。

回溯与锚点推进的**先后顺序**：

- [src/streaming_diff.rs:L195-L198](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L195-L198) — 先 `backtrack(next_old_text_ix, next_new_text_ix)`，再执行 `self.old_text_ix = next_old_text_ix; self.new_text_ix = next_new_text_ix;`。顺序不能颠倒：`backtrack` 的循环条件是「走到 `(self.old_text_ix, self.new_text_ix)` 为止」，锚点字段此刻还必须保持旧值，充当回溯的起点哨兵。

对照 `finish`：

- [src/streaming_diff.rs:L276-L279](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L276-L279) — `finish` 消耗 `self` 并直接 `backtrack(self.old.len(), self.new.len())`：终点被强制钉在旧文本末尾，挂起已久的旧尾部此刻必须以 `Keep` 或 `Delete` 结算。这是「自由终点」与「强制终点」仅有的差别。

**贯穿示例的末列手算表**。`old = "aaaa\nbbbb"`、`new = "aaaa\nBBBB"`，一次性 `push_new` 整段新文本（矩阵为 10×10，锚点从 \((0,0)\) 出发）。按 u3-l2 的递推手算最后一列（\(j=9\)，即新文本全部到达后）：

| \(i\) | 已消费的旧前缀 | \(S(i,9)\) | 说明 |
|---|---|---|---|
| 0 | `""` | \(-9\) | 纯插入 9 个字符，\(9 \times (-1)\) |
| 1 | `"a"` | \(-7\) | |
| 2 | `"aa"` | \(-5\) | |
| 3 | `"aaa"` | \(-3\) | |
| 4 | `"aaaa"` | \(-0.2\) | 路径 = 对角线匹配 4 个 `a`（\(S(4,4)=4.8\)）再插入 5 个字符（`\nBBBB`，\(-5\)） |
| 5 | `"aaaa\n"` | **\(2.6\)** | 路径 = 匹配 `aaaa\n`（\(S(5,5)=6.6\)）再插入 4 个 `B`（\(-4\)）✦ 全列最大 |
| 6 | `"aaaa\nb"` | \(-17.4\) | 比 \(i=5\) 恰好低 20：多消费一个匹配不上的 `b` 只能删除 |
| 7 | `"aaaa\nbb"` | \(-37.4\) | |
| 8 | `"aaaa\nbbb"` | \(-57.4\) | |
| 9 | `"aaaa\nbbbb"` | \(-77.4\) | |

最大值 2.6 唯一地落在 \(i=5\)，于是本轮锚点推进到 \((5, 9)\)：旧文本的 `"bbbb"`（下标 5..8）保持挂起，等待 `finish` 结算。

#### 4.1.4 代码实践

1. **实践目标**：不运行代码，靠递推手算验证上表的两个关键值，从而确认锚点为什么是 5。
2. **操作步骤**：
   - 复算 \(S(4,4) = 4.8\)：对角线四步相等，游程分别为 \(1,2,3,4\)，奖励 \(1+1+1+1.8\)（游程 4 时 \(\lfloor 4/4\rfloor=1\)，\(1.8^1=1.8\)）。
   - 复算 \(S(5,5) = 6.6\)：第五步匹配 `'\n'`，游程 5，指数 \(\lfloor 5/4\rfloor = 1\)，\(6.6 = 4.8 + 1.8\)。
   - 推出 \(S(4,9) = 4.8 - 5 = -0.2\)（再插 5 个字符）与 \(S(5,9) = 6.6 - 4 = 2.6\)（再插 4 个字符）。
   - （可选，本地验证）在 [src/streaming_diff.rs:L182](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L182) 与 L184 之间临时插入一行 `eprintln!("{:?}", self.scores);`，利用 [src/streaming_diff.rs:L93-L104](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L93-L104) 的 `Debug` 实现直接打印整张分数表。**这是练习性临时改动，看完请用 `git checkout -- crates/streaming_diff/src/streaming_diff.rs` 还原。**
3. **需要观察的现象**：打印出的最后一列与手算表一致（浮点格式可能是 `2.6` 的近似显示）；第 0 列是上一轮锚点列的分数。
4. **预期结果**：\(S(5,9)=2.6\) 为末列最大，锚点 \((5,9)\)。手算推导如上；打印结果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `DELETION_SCORE` 从 \(-20\) 改成 \(-2\)（其他不变），末列的最大值位置最可能怎么变？为什么？

**答案**：更可能下移（消费更多旧字符）。删除变便宜后，「删掉尾部 `bbbb`」的代价从 \(-80\) 变成 \(-8\)，\(S(9,9)\) 这类「全部用完」的终点分数大幅上升；当删除便宜到某个程度，把旧文本全部结算掉反而超过「挂起 + 插入」的路径。这正是 u2-l2 说的：删除/插入的相对代价直接控制锚点的竞争裕度。（具体数值待本地验证，可用练习 4.1.4 的打印法观察。）

**练习 2**：`push_new("")`（空块）时终点搜索返回什么？整个 `push_new` 返回什么？

**答案**：空块时 `self.new.len() == self.new_text_ix`，填表循环不执行，「最后一列」就是第 0 列（锚点列本身）。扫描在锚点列上找最大分行——通常仍是原 `old_text_ix` 附近；即便 `next_old_text_ix` 变了，`backtrack` 从 `(i, new.len())` 出发时 \(j\) 一开始就等于 `new_text_ix`，循环条件立刻满足或只走删除步。对空块而言 `next_new_text_ix - self.new_text_ix = 0`，回溯只会产出锚点列内部的路径；常见结果是返回空 `Vec`（u3-l2 已把「空块为无操作」列为不变量）。

---

### 4.2 backtrack：从终点反向走 DP 决策

#### 4.2.1 概念说明

`backtrack(old_text_ix, new_text_ix)`（注意参数名与字段重名，传的是**终点**）要解决的问题是：矩阵只存了每个格子的**分数**，没存每个格子是「从哪个方向来的」——回溯需要自己重新推断。方法是站在终点 \((i,j)\)，枚举三个可能的**前驱格**：

| 前驱 | 移动语义 | 消费的内容 | 对应操作 |
|---|---|---|---|
| \((i, j-1)\) | 插入步（向右走一格） | 新字符 `new[j-1]` | `Insert` |
| \((i-1, j)\) | 删除步（向上走一格） | 旧字符 `old[i-1]` | `Delete` |
| \((i-1, j-1)\) | 相等步（沿对角线） | 一对相等的字符 | `Keep` |

选择规则：三个前驱格中**存储分数最大**者胜。注意这里比较的是前驱格的**原始分数**，并没有加上各自的移动代价再比（见 4.2.3 末尾的讨论）。由于路径是反向走的，生成的 hunk 也是**从后往前**追加，最后统一 `reverse`。

#### 4.2.2 核心流程

```text
i, j = 终点;  hunks = [];  pending_insert = None
while (i, j) != (锚点 old_text_ix, new_text_ix):
    候选 = [
        j > new_text_ix                ? Some((i,   j-1)) : None,   # 插入
        i > old_text_ix                ? Some((i-1, j  )) : None,   # 删除
        i > old_text_ix 且 j > new_text_ix
          且 old[i-1] == new[j-1]      ? Some((i-1, j-1)) : None,   # 相等
    ]
    (prev_i, prev_j) = 按 OrderedFloat(S(前驱)) 取最大;   # 并列取数组中最后者
    if prev 是插入步:
        把 prev_j..j 并入 pending_insert（区间向左增长）
    else:
        先把 pending_insert 冲刷成一个 Insert
        if prev 是删除步:  发射/合并 Delete{old[i-1].len_utf8()}
        else (相等步):     发射/合并 Keep {old[i-1].len_utf8()}
    i, j = prev_i, prev_j
循环结束后再冲刷一次残留的 pending_insert;  hunks.reverse()
```

为什么「不可走的方向」用 `None` 表示就能自动出局：候选数组的元素类型是 `Option<(usize, usize)>`，键类型是 `Option<OrderedFloat<f64>>`，而 `None < Some(_)`，所以任何合法前驱都严格大于 `None`。循环条件保证 \((i,j)\) 未回到锚点，则 \(i > \text{锚点}i\) 或 \(j > \text{锚点}j\) 至少成立（路径坐标只会向下递减、且不会低于锚点），删除 / 插入候选至少有一个是 `Some`——这就是两处 `.unwrap()`（L232、L233）不会 panic 的原因。

#### 4.2.3 源码精读

- [src/streaming_diff.rs:L202-L205](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L202-L205) — 初始化：`pending_insert: Option<Range<usize>>`（新文本的**字符下标**区间，不是字节区间）、空的 `hunks`、游标 \((i,j)\) 置为终点。
- [src/streaming_diff.rs:L206-L225](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L206-L225) — 循环条件与三个候选的构造。相等候选除了边界检查，还要**现场重新比较字符** `self.old[i-1] == self.new[j-1]`（L218）——回溯完全不使用 `equal_runs` 数组，游程信息已经烙进矩阵分数里，合法性只需要字符相等这一条。
- [src/streaming_diff.rs:L227-L233](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L227-L233) — 决策核心：`[insertion_score, deletion_score, equality_score].iter().max_by_key(...)`，键是 `cell.map(|(i, j)| OrderedFloat(self.scores.get(i, j - self.new_text_ix)))`。两点值得注意：① 读矩阵时把**绝对列号 \(j\)** 换算成相对列号（矩阵第 0 列是锚点列）；② `max_by_key` 并列时返回**最后**一个元素，而相等候选排在数组末位，因此并列优先级是 **相等 > 删除 > 插入**——与 4.1 终点搜索「并列取最先行」正好是两个不同方向的保守选择。
- [src/streaming_diff.rs:L262-L273](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L262-L273) — 游标推进、循环结束后冲刷残留的 `pending_insert`，最后 `hunks.reverse()` 把逆序构建的序列转正。

**贯穿示例的回溯轨迹**（锚点 \((0,0)\)，终点 \((5,9)\)）：

| 步 | 当前 \((i,j)\) | 插入候选 \((i,j-1)\) | 删除候选 \((i-1,j)\) | 相等候选 \((i-1,j-1)\) | 胜者 | 动作 |
|---|---|---|---|---|---|---|
| 1 | \((5,9)\) | \(S(5,8)=3.6\) | \(S(4,9)=-0.2\) | `'\n'`≠`'B'` → None | 插入 | `pending_insert = 8..9` |
| 2 | \((5,8)\) | \(4.6\) | \(0.8\) | None | 插入 | 区间扩为 `7..9` |
| 3 | \((5,7)\) | \(5.6\) | \(1.8\) | None | 插入 | `6..9` |
| 4 | \((5,6)\) | \(6.6\) | \(2.8\) | None | 插入 | `5..9` |
| 5 | \((5,5)\) | \(-15.2\) | \(3.8\) | `'\n'`==`'\n'`，\(S(4,4)=4.8\) | **相等** | 冲刷 `Insert{"BBBB"}`，发射 `Keep{1}` |
| 6 | \((4,4)\) | \(-17\) | \(2\) | \(3\) | 相等 | `Keep` 合并 → `{2}` |
| 7 | \((3,3)\) | \(-18\) | \(1\) | \(2\) | 相等 | `Keep{3}` |
| 8 | \((2,2)\) | \(-19\) | \(0\) | \(1\) | 相等 | `Keep{4}` |
| 9 | \((1,1)\) | \(-20\) | \(-1\) | \(0\) | 相等 | `Keep{5}`，抵达锚点，循环结束 |

`hunks` 构建顺序为 `[Insert{"BBBB"}, Keep{5}]`，`reverse` 后即 `push_new` 的返回值：`[Keep{bytes:5}, Insert{text:"BBBB"}]`——正是「保留 `aaaa\n`、插入 `BBBB`、旧尾部 `bbbb` 挂起」。

再对照 `finish` 的回溯（锚点已推进到 \((5,9)\)，终点强制 \((9,9)\)）：四步全是删除候选获胜（\(-57.4 > -76.4\)、\(-37.4 > -56.4\)、\(-17.4 > -36.4\)、\(2.6 > -16.4\)），四个 `b` 依次合并成一个 `Delete{bytes:4}`。两段拼接：`[Keep{5}, Insert{"BBBB"}, Delete{4}]`，应用到 `old` 恰好得到 `new`。

**一个值得注意的实现细节**（源码阅读结论，不是 bug 报告）：回溯比较的是前驱格的**原始分数**，而前向递推比较的是「前驱分数 + 移动代价」。例如插入代价 \(-1\) 与相等奖励 \(\ge 1.8\) 并没有被补回比较式里，因此回溯选出的移动**不总是**前向递推的严格 argmax。这对正确性无害——如 4.2.1 所说，只要路径每步合法（相等步只发生在字符相等处，由 L217-L222 保证），round-trip 就成立；分数只影响 hunk 的主观质量。这也解释了为什么本 crate 的随机测试（[src/streaming_diff.rs:L926-L951](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L926-L951)）断言的是「应用操作后等于新文本」，而不是「操作序列最优」。

#### 4.2.4 代码实践

1. **实践目标**：亲手走一遍反向决策，把 4.2.3 的轨迹表自己填出来，检验对三个候选与胜者规则的理解。
2. **操作步骤**：遮住表中「胜者」「动作」两列，只看 \((i,j)\) 与三个候选分数，逐行推断胜者与 `pending_insert` / `hunks` 的变化；再对照 `finish` 段的四步删除，写出最终 `Delete{bytes:4}` 的合并过程。
3. **需要观察的现象**：步 1-4 都是插入候选胜出（分数沿 \((5,5)\to(5,9)\) 递减，但始终高于同行删除候选）；步 5 相等候选以 4.8 反超。
4. **预期结果**：与 4.2.3 两张表一致（手算推导；可用综合实践的测试在本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `backtrack` 里读矩阵用 `j - self.new_text_ix` 换算列号，而终点搜索里也这样做？如果直接用绝对列号 `j` 会怎样？

**答案**：矩阵每轮 `push_new` 都从锚点列重新起算（u3-l2 的滚动设计），第 0 列存锚点列、第 \(k\) 列对应绝对新文本位置 `new_text_ix + k`。因此绝对列号必须减去锚点 `new_text_ix` 才落到矩阵下标范围内。直接用 `j` 会越界（触发 `Matrix::get` 的列越界 panic），或者在新文本很长时读到错误的列。`backtrack` 在锚点推进**之前**调用，此刻 `self.new_text_ix` 还是本轮起点，换算恰好正确。

**练习 2**：回溯过程中如果三个候选都是 `None`，两处 `.unwrap()` 会 panic。构造得出这种情况吗？

**答案**：构造不出。循环条件是 \((i,j) \ne (\text{old\_text\_ix}, \text{new\_text\_ix})\)，而路径上的 \(i,j\) 单调递减且以锚点为下界（每步前驱至少把一个坐标减一、循环一到锚点即停）。于是 \((i,j)\) 不等于锚点意味着 \(i > \text{old\_text\_ix}\) 或 \(j > \text{new\_text\_ix}\)，对应删除候选或插入候选至少一个为 `Some`。`Option` 的 `Ord`（`None < Some(_)`）进一步保证 `Some` 候选在比较中必然压过 `None`，被选中的前驱一定是合法移动。

---

### 4.3 pending_insert 与 Delete/Keep 合并：生成紧凑的 hunk

#### 4.3.1 概念说明

如果按步翻译，贯穿示例的路径会产出 5 个 `Keep{1}` + 1 个 `Insert{"BBBB"}`——下游（比如 u1-l2 讲过的 `LineDiff`）就要处理一长串碎片。`backtrack` 用两个技巧把输出压紧：

1. **`pending_insert` 延迟发射**：连续的插入步不立即生成 `Insert`，而是把新文本的字符区间向左增长（`Range<usize>` 只记下标，零拷贝）；一旦走到任何非插入步，区间一次性收集成 `String` 发射成一个 `Insert`。
2. **同类 hunk 就地合并**：删除步 / 相等步发射前先看 `hunks` 的最后一个元素，若是同类 `Delete` / `Keep` 就把 `len_utf8()` 加上去，否则才 push 新元素。

两者合起来给出一个可测试的不变量：**输出序列中不存在相邻的同变体操作**（`Keep,Keep`、`Delete,Delete`、`Insert,Insert` 都不会出现）。

#### 4.3.2 核心流程

```text
插入步 (prev = (i, j-1)):
    if pending_insert 已存在: pending_insert.start = prev_j   # 区间向左扩张
    else:                   pending_insert = prev_j..j

非插入步 (删除步或相等步):
    if pending_insert 存在:
        hunks.push(Insert { text: new[range].iter().collect() })   # 一次性成串
    char_len = old[i-1].len_utf8()
    删除步:  末尾是 Delete ? *len += char_len : push Delete{char_len}
    相等步:  末尾是 Keep  ? *len += char_len : push Keep {char_len}
```

#### 4.3.3 源码精读

- [src/streaming_diff.rs:L235-L240](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L235-L240) — 插入步分支：判定条件是 `prev_i == i && prev_j == j - 1`。已挂起的区间只改 `start`（向左增长），否则新建 `prev_j..j`。区间存的是 `self.new`（`Vec<char>`）的**字符下标**。
- [src/streaming_diff.rs:L241-L246](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L241-L246) — 非插入步先 `pending_insert.take()` 冲刷：`self.new[range].iter().collect()` 把区间内的 `char` 收集成 `String`（`take` 同时把槽位清空）。
- [src/streaming_diff.rs:L248-L259](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L248-L259) — `char_len = self.old[i-1].len_utf8()`：旧文本以 `Vec<char>` 存放（u3-l1），而 `CharOperation` 以**字节**计量 `Delete`/`Keep`（u1-l2），这里完成换算。随后删除步（`prev_i == i-1 && prev_j == j`）与相等步（落到 `else if`）分别尝试与 `hunks.last_mut()` 模式匹配合并。
- [src/streaming_diff.rs:L266-L270](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L266-L270) — 循环结束后的最后一次冲刷：如果路径**以插入步开头**（回溯视角的第一步，即正序的最后一段插入），区间还挂在 `pending_insert` 里，必须在这里补发射。

用贯穿示例核对：步 1-4 连续插入把区间涨到 `5..9`；步 5（相等步）先冲刷出 `Insert{"BBBB"}`，再发射 `Keep{1}`；步 6-9 连续相等步把 `Keep` 合并到 `{5}`。`finish` 段的四步删除同理合并成 `Delete{4}`。注意一个细节：合并的分支各自只检查「末尾是否**同类**」，`Delete` 可以紧跟着 `Insert` 或 `Keep` 出现（不同类不合并），例如最终序列 `[Keep{5}, Insert{"BBBB"}, Delete{4}]` 就是三种变体交错。

#### 4.3.4 代码实践

1. **实践目标**：验证「相邻同变体必被合并」这一不变量在另一组输入上仍然成立。
2. **操作步骤**：取 `old = "aaaa\nbbbb"`、`new = "aaaa\nbbbb"`（新旧完全相同），先在纸上预测 `push_new` 与 `finish` 各返回什么，再按下面综合实践的方式加测试跑一遍。
3. **需要观察的现象**：回溯全程走对角线（9 个相等步），合并成一个 `Keep`；没有任何 `Insert` / `Delete`。
4. **预期结果**：`push_new` 返回 `[Keep{bytes:9}]`，`finish` 返回 `[]`（锚点已在 \((9,9)\)，无剩余区间）。推导依据：相同文本下对角线分数一路最高（游程奖励单调累积），终点搜索选中 \(i=9\)；手算推导，待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `pending_insert` 用 `Range<usize>` 而不是直接 `String`，每次插入步 `push_str`？

**答案**：回溯是逆序走的，连续插入步对应的新文本字符在 `self.new` 中恰好是**连续区间**（下标从高向低扩展）。用区间记录只需每步改一个 `start`（O(1)、零分配），发射时一次 `iter().collect()` 成 `String`。若用 `String` 累加，要么每步拼接（逆序还要 `insert_str(0, ..)` 之类的高开销操作），要么发射时再反转，都更繁琐。

**练习 2**：`Insert` 之后紧跟 `Insert` 可能出现吗？`Delete` 之后紧跟 `Keep`、`Keep` 之后紧跟 `Delete` 呢？

**答案**：`Insert,Insert` 不可能——连续插入步只会扩张 `pending_insert`，只有非插入步或循环结束才发射 `Insert`，两次发射之间必然隔着至少一个 `Delete`/`Keep`。同理 `Delete,Delete` 与 `Keep,Keep` 不可能（就地合并）。而 `Delete,Keep`、`Keep,Delete`、`Insert,Keep` 等异类相邻完全正常——合并分支只匹配同类。所以稳定的不变量是「不存在**相邻同变体**」，而不是「每种变体只出现一次」。

---

### 4.4 OrderedFloat：让浮点分数可比较

#### 4.4.1 概念说明

Rust 的 `f64` 只实现 `PartialOrd`：因为 `NaN` 与任何值比较都返回 `false`，全序关系（`<`、`==`、`>` 三者必居其一）不成立。而 `Iterator::max_by_key` 的签名要求键类型满足 `K: Ord`。于是 [src/streaming_diff.rs:L227-L233](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L227-L233) 的键由两层包装构成：

```text
Option<(usize, usize)>                 ← 候选前驱（None = 该方向不可走）
    .map(|(i, j)|                      ← 对 Some 的内容打分，None 保持 None
        OrderedFloat(f64)              ← 把 PartialOrd 的 f64 变成 Ord
    )
→ Option<OrderedFloat<f64>>            ← max_by_key 的键
```

`OrderedFloat`（来自 [Cargo.toml:L14-L16](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/Cargo.toml#L14-L16) 依赖的 `ordered-float` crate，[src/streaming_diff.rs:L1](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L1) 导入）是 `f64` 的新类型包装，补上了全序实现。在本 crate 的比较路径中分数永远是有限值（常量加减与 \(1.8^k\)，\(k \le 16\)；`NEG_INFINITY` 只在前向填表里当「禁走」哨兵，不会被存进格子），所以 `OrderedFloat` 在这里的作用纯粹是**满足类型系统的 `Ord` 约束**，而非处理 `NaN` 语义。

顺带留意一个类型层面的巧思：外层 `None < Some(_)` 的语义恰好表达了「不可走的方向自动出局」（4.2 已分析），不需要任何显式优先级代码。

#### 4.4.2 核心流程

```text
三个候选: [Option<插入前驱>, Option<删除前驱>, Option<相等前驱>]
    ↓ 每个候选映射为「其格子的分数，包上 OrderedFloat」
键列表:  [Option<OrderedFloat(S)>; 3]      （None = 不可走）
    ↓ max_by_key（Ord 比较；并列取最后者）
胜者候选 = Some((prev_i, prev_j))           （循环条件保证必然存在）
```

#### 4.4.3 源码精读

- [src/streaming_diff.rs:L230](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L230) — 全 crate 唯一的使用点：`cell.map(|(i, j)| OrderedFloat(self.scores.get(i, j - self.new_text_ix)))`。`cell` 是 `&Option<(usize, usize)>`，`map` 解引用复制二元组后读分换列号。
- 对照前向填表的比较方式：[src/streaming_diff.rs:L178](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L178) 用的是 `f64::max`（`PartialOrd` 就够，因为不需要排序语义）；回溯要「三选一按键取最大」就必须 `Ord`。同一个 crate 里两种比较方式的分工，正好示范了 `PartialOrd` 与 `Ord` 的边界。

#### 4.4.4 代码实践

1. **实践目标**：亲眼看到「`f64` 不能当 `max_by_key` 的键」这一编译期约束。
2. **操作步骤**：在仓库外任选一个练习目录，新建一个小 crate（`cargo new ord_demo`，并在 `Cargo.toml` 加 `ordered-float = "4"`），`main.rs` 写：

   ```rust
   use ordered_float::OrderedFloat;

   fn main() {
       let scores = vec![1.0f64, 3.0, 2.0];
       // 先试着解除下一行注释再编译：
       // let best = scores.iter().max_by_key(|s| **s);
       let best = scores.iter().max_by_key(|s| OrderedFloat(**s));
       println!("{:?}", best);
   }
   ```

3. **需要观察的现象**：注释掉 `f64` 版本时正常编译；解开注释后编译失败，错误信息指出 `f64` 未实现 `Ord`（`max_by_key` 要求 `K: Ord`）。
4. **预期结果**：打印 `Some(3.0)`（`OrderedFloat` 包装后比较仍按数值大小）；`f64` 版本的编译错误文本因编译器版本而异，待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：如果把回溯里的 `max_by_key` 换成 `max_by(|a, b| a.cmp(b))`，功能会变吗？

**答案**：比较语义不变（仍是 `Option<OrderedFloat>` 的 `Ord`），但**并列行为相反**：`max_by_key` 并列返回最后元素，`max_by` 同样返回最后元素——两者在这一点上其实一致（`min_by_key`/`min_by` 才取第一）。真正会翻转并列行为的是手写 `if a > b` 之类的首次比较。这个练习的关键是意识到：并列时「相等候选优先」依赖候选数组 `[插入, 删除, 相等]` 的**排列顺序**，改动数组顺序会悄悄改变 diff 的形状。

**练习 2**：终点搜索（L189）直接用 `if score > max_score` 比较 `f64`，没有包 `OrderedFloat`，为什么可行？

**答案**：那里只需要「两两比较、刷新最大值」，`PartialOrd` 的 `<`/`>` 就足够，Rust 并不要求全序。只有把值当作**排序键 / 哈希键 / `BTreeMap` 键 / `max_by_key` 键**这类需要 `Ord` 的位置，才必须用 `OrderedFloat`。这也是读源码时分辨「哪里的比较是语义比较、哪里是被类型系统强制的」的一个小技巧。

---

## 5. 综合实践

**任务**：为 `backtrack` 写一个端到端测试——用贯穿示例 `old = "aaaa\nbbbb"`、`new = "aaaa\nBBBB"`，断言回溯产物恰好把 `old` 补丁成 `new`，并断言相邻同类操作已被合并。

**放置位置**：`backtrack` 是私有方法、`apply_char_operations` 是测试模块私有的辅助函数，因此测试必须写进 [src/streaming_diff.rs:L525](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L525) 之后的 `mod tests` 内部（这属于本地练习性改动，验证后请还原）。在 `test_apply_char_operations`（[src/streaming_diff.rs:L1037-L1050](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L1037-L1050)）附近加入：

```rust
#[test]
fn test_backtrack_replace_second_line() {
    let old_text = "aaaa\nbbbb";
    let new_text = "aaaa\nBBBB";

    let mut diff = StreamingDiff::new(old_text.to_string());

    // 第一段：push_new 内部完成终点搜索（锚点定为 (5, 9)）并调用 backtrack。
    // 其返回值就是 backtrack(最优终点) 的输出。
    let mut char_ops = diff.push_new(new_text);
    assert_eq!(char_ops.len(), 2);
    assert!(matches!(char_ops[0], CharOperation::Keep { bytes: 5 }));
    assert!(matches!(char_ops[1], CharOperation::Insert { ref text } if text == "BBBB"));

    // 第二段：finish 等价于 backtrack(old.len(), new.len())，
    // 把挂起的旧尾部 "bbbb" 以 Delete 结算。
    char_ops.extend(diff.finish());
    assert_eq!(char_ops.len(), 3);
    assert!(matches!(char_ops[2], CharOperation::Delete { bytes: 4 }));

    // round-trip：操作序列按序应用到 old，必须恰好重建 new。
    let patched = apply_char_operations(old_text, &char_ops);
    assert_eq!(patched, new_text);

    // 合并不变量：backtrack 的输出中不存在相邻的同变体操作。
    for pair in char_ops.windows(2) {
        let adjacent_same = matches!(
            (&pair[0], &pair[1]),
            (CharOperation::Keep { .. }, CharOperation::Keep { .. })
                | (CharOperation::Delete { .. }, CharOperation::Delete { .. })
                | (CharOperation::Insert { .. }, CharOperation::Insert { .. })
        );
        assert!(!adjacent_same, "存在未合并的相邻同类操作: {:?}", pair);
    }
}
```

**操作步骤**：

1. 在 `mod tests` 内粘贴上述测试（放 `test_apply_char_operations` 之后即可）。
2. 在仓库根目录运行 `cargo test -p streaming_diff test_backtrack_replace_second_line -- --nocapture`。
3. 观察两条断言组：结构断言（长度 + 三个变体的形状）与 round-trip / 合并断言。
4. 想直接单测私有函数的话，可在 `push_new` 之后补一行 `let ops = diff.backtrack(diff.old.len(), diff.new.len());`——在 `mod tests` 内可见私有字段与方法，效果与 `finish()` 相同但不消耗 `diff`。
5. 验证完毕后还原：`git checkout -- crates/streaming_diff/src/streaming_diff.rs`（或 `git restore crates/streaming_diff/src/streaming_diff.rs`）。不要把这个练习性改动提交进仓库。

**需要观察的现象与预期结果**：

- 测试通过；`push_new` 段得到 `[Keep{bytes:5}, Insert{text:"BBBB"}]`，`finish` 段补上 `[Delete{bytes:4}]`，拼接后 round-trip 等于 `"aaaa\nBBBB"`。
- 结构断言用 `matches!` 而非 `assert_eq!`，因为 `CharOperation` 只派生了 `Debug, Clone`（[src/streaming_diff.rs:L106-L111](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L106-L111)），没有 `PartialEq`——对比 `LineOperation`（L281）是派生了 `PartialEq` 的。
- 上述精确序列来自本讲 4.1/4.2 的手算推导，**待本地验证**；若与你本地运行结果不一致，以运行结果为准，并回查 4.1.3 的末列分数表定位分歧。

**延伸**（可选）：把 `push_new` 的调用拆成两次（先 `"aaaa\n"` 再 `"BBBB"`），观察第一轮锚点停在 \((5,5)\)、返回 `[Keep{5}]`，第二轮返回 `[Insert{"BBBB"}]`——分块只改变结算时机，不改最终拼接结果（u3-l4 的主题）。

## 6. 本讲小结

- **终点搜索**（L184-L193）在最后一列自旧锚点起找最大分行：新侧锚点无条件推到 `new.len()`，旧侧自由——删除太贵（\(-20\)），把暂时对不上的旧尾部「挂起」零成本，账单留给 `finish` 强制钉死终点时结算。
- **锚点推进的顺序**是「先 `backtrack`、后赋值」（L195-L197）：回溯的循环条件依赖锚点字段保持旧值充当起点哨兵。
- **回溯循环**（L206-L264）从终点反推前驱：三个 `Option<(usize, usize)>` 候选按 `Option<OrderedFloat>` 取最大，`None < Some(_)` 让不可走方向自动出局；并列时 `max_by_key` 取最后元素，优先级为相等 > 删除 > 插入。
- **紧凑化**靠 `pending_insert`（新文本字符区间向左增长、遇非插入步一次性成串）与 Delete/Keep 的 `last_mut` 就地合并，稳定不变量是「输出中不存在相邻同变体操作」。
- 回溯比较的是前驱格**原始分数**而非「分数 + 移动代价」，因此不严格重现前向 argmax；正确性由「路径每步合法」保证，分数只影响 hunk 质量——这正是随机测试只断言 round-trip 的原因。
- `OrderedFloat` 的唯一职责是给 `f64` 补上 `Ord`，满足 `max_by_key` 的键约束；本 crate 的分数不含 `NaN`。

## 7. 下一步学习建议

下一讲 **u3-l4 finish 与流式语义** 将把本讲的锚点机制推广成完整的不变量陈述：任意分块方式下，把所有 `push_new` 返回值与 `finish` 返回值按序拼接、依次应用到旧文本，必然重建新文本。建议先自己动手做 5. 综合实践的「延伸」步骤（拆两次 `push_new`），再阅读 [src/streaming_diff.rs:L276-L279](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L276-L279) 与随机化测试 [src/streaming_diff.rs:L963-L981](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L963-L981)（`random_streaming_diff` 正是把「随机分块 + 拼接 + round-trip」自动化）。完成 u3-l4 后即可进入单元四的 `LineDiff`。

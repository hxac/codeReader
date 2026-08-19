# 构造函数与 DP 初始化：从全局对齐算法说起

## 1. 本讲目标

前两单元我们已经认识了差异的"语言"（`CharOperation` / `LineOperation`）、打分矩阵的"容器"（`Matrix`）和打分常量的"价值观"（删一个字符 −20、插一个字符 −1、连续相等指数奖励）。本讲把这三者第一次接在一起，看**动态规划的起点是怎么搭起来的**。学完后你应该能：

1. 说出打分矩阵中状态 \( S(i, j) \) 的准确含义，以及"插入 / 删除 / 相等"三种子问题分别对应哪些前驱格子。
2. 逐行解释 `StreamingDiff::new` 的每一步：为什么把 `old` 转成 `Vec<char>`、为什么矩阵是 `(old_len + 1) × 1`、为什么第 0 列要填 `i * DELETION_SCORE`。
3. 说清 `Matrix::resize` 与 `Matrix::set` 在初始化中各自承担的角色（先扩容、后填值，顺序不能颠倒）。
4. 对比这个实现与教科书版 Needleman-Wunsch 算法的异同：浮点分数、不对称代价、以及"终点不固定、取分数最大处锚定"的流式改造。

## 2. 前置知识

### 2.1 动态规划与"状态"

动态规划（Dynamic Programming，DP）把一个大问题拆成互相重叠的小问题，把每个小问题的答案**存进表格**，避免重复计算。表格里的每个格子叫一个**状态**。本讲中状态是：

> \( S(i, j) \) = 把旧文本的前 \( i \) 个字符与（已到达的）新文本的前 \( j \) 个字符对齐时，能拿到的**最高分数**。

分数越高表示这种对齐方式越好。负分数代表"这一路走来付出了净代价"。

### 2.2 编辑脚本与三种子问题

第一单元讲过：diff 的输出是一串操作（Keep / Delete / Insert），按序应用到旧文本就得到新文本。DP 填表时，每个格子 \( S(i,j) \) 只可能从三个方向之一走过来，每个方向对应一种"最后一步操作"：

| 方向 | 前驱状态 | 最后一步做的事 | 直觉 |
|---|---|---|---|
| 向右（消耗一个新字符） | \( S(i, j-1) \) | Insert 一个新字符 | 新文本多了一个字符，旧文本原地不动 |
| 向下（消耗一个旧字符） | \( S(i-1, j) \) | Delete 一个旧字符 | 旧文本被跳过一个字符 |
| 对角（各消耗一个） | \( S(i-1, j-1) \) | 若两字符相等则 Keep | 新旧各前进一格，字符相同就是"白捡的匹配" |

### 2.3 边界条件

递推不能无限往回追，必须有一些"初始格子"直接给值：

- 第 0 列 \( S(i, 0) \)：新文本一个字符都还没来，旧文本的前 \( i \ 个字符只能全部删掉。
- 第 0 行 \( S(0, j) \)：旧文本已经用完（或还没对上任何旧字符），新文本的前 \( j \) 个字符只能全部插入。

本讲的男主角 `StreamingDiff::new` 负责的就是**第 0 列**；第 0 行则由下一讲的 `push_new` 在每一列动态补上。

### 2.4 Needleman-Wunsch：本实现的"教科书原型"

Needleman-Wunsch（NW）是 1970 年提出的序列全局对齐算法，生物信息学里用来对齐 DNA/蛋白质序列。它的骨架与这里完全一致：二维打分矩阵、三个方向的递推、填满后从右下角回溯得到对齐路径。读完本讲你再去看任何一篇 NW 教程，会发现 streaming_diff 就是"NW 换了一套面向 LLM 流式输出的打分函数，并且只保留一条可滚动的列带"。具体差异在第 4.4 节展开。

### 2.5 复习：Vec::resize 的行为

标准库的 `Vec::resize(new_len, value)`：若新长度更大，在**尾部**追加 `value` 的拷贝；若更小，从尾部截断。这个"只在尾部动"的性质是 `Matrix::resize` 能安全扩容的关键（u2-l1 已详述）。

## 3. 本讲源码地图

本 crate 只有一个库文件，本讲涉及其中三段：

| 代码区域 | 位置 | 作用 |
|---|---|---|
| `Matrix` 结构与 `resize` / `set` | `src/streaming_diff.rs` 第 10–30、66–76 行 | 打分矩阵的容器：一维存储、扩容、定点写入 |
| `Matrix` 的 `Debug` 实现 | 第 93–104 行 | 按行列排版打印矩阵，是本讲实践的观察工具 |
| `StreamingDiff` 结构体与常量 | 第 113–128 行 | 七个状态字段 + 四个打分常量 |
| `StreamingDiff::new` | 第 130–147 行 | **本讲主角**：构造函数与 DP 初始化 |
| `push_new` 的第 0 行初始化 | 第 164 行 | `current_scores[0] = j * INSERTION_SCORE`，与第 0 列呼应 |
| 测试辅助 `random_streaming_diff` | 第 963–981 行 | 构造函数在真实代码中的调用现场 |

## 4. 核心概念与源码讲解

### 4.1 状态 (i, j) 与三种子问题

#### 4.1.1 概念说明

把旧文本记为 \( o_1 o_2 \dots o_m \)（按 `char` 计），新文本已到达部分记为 \( n_1 n_2 \dots n_n \)。定义：

\[ S(i, j) = \text{对齐 } o_{1..i} \text{ 与 } n_{1..j} \text{ 可获得的最高分数} \]

矩阵有 \( (m+1) \times (n+1) \) 个格子——多出来的那一行一列就是"空前缀"，用来放边界条件。这就是为什么构造函数里矩阵的行数是 `old_len + 1` 而不是 `old_len`。

三个常量沿用 u2-l2 的记号：插入 \( I = -1 \)，删除 \( D = -20 \)，相等奖励 \( B(r) = 1.8^{\min(\lfloor r/4 \rfloor,\ 16)} \)，其中 \( r \) 是以该格子结尾的连续相等字符个数。

#### 4.1.2 核心流程

递推关系（下一讲的 `push_new` 实现"填表"部分，本讲先建立公式）：

\[ S(i, j) = \max \begin{cases} S(i, j-1) + I & \text{（插入 } n_j \text{）} \\[2pt] S(i-1, j) + D & \text{（删除 } o_i \text{）} \\[2pt] S(i-1, j-1) + B(r_{i,j}) & \text{仅当 } o_i = n_j \text{（否则为 } -\infty \text{）} \end{cases} \]

边界条件：

\[ S(i, 0) = i \cdot D = -20\,i, \qquad S(0, j) = j \cdot I = -j \]

用文字读一遍第 0 列：**"新文本还空着，旧文本前 \( i \) 个字符全删，代价 \( 20i \)"**。注意它是"删除代价 × 个数"的线性累加——每多留一个没对上的旧字符，分数就再跌 20。这也再次印证 u2-l2 的结论：删除极贵，所以算法会拼命寻找旧文本中能匹配上的部分，而不是推倒重来。

字符错配时对角分支取 \( -\infty \)（Rust 写法 `f64::NEG_INFINITY`），含义是"这一步禁止走"，让 `max` 自然把它淘汰掉，不需要额外的 `if` 分支。

#### 4.1.3 源码精读

三候选取最大值的核心在 `push_new` 主循环里（本讲只看一眼，下一讲精读）：

[streaming_diff.rs:165-179](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L165-L179)

```rust
for i in 1..=old.len() {
    let insertion_score = previous_scores[i] + Self::INSERTION_SCORE;
    let deletion_score = current_scores[i - 1] + Self::DELETION_SCORE;
    let equality_score = if old[i - 1] == new_char {
        // ……相等奖励……
        previous_scores[i - 1] + Self::EQUALITY_BASE.powi(exponent)
    } else {
        f64::NEG_INFINITY
    };

    current_scores[i] = insertion_score.max(deletion_score).max(equality_score);
}
```

这段代码与上面的数学公式一一对应：`previous_scores[i]` 就是左边的格子 \( S(i, j-1) \)（上一列同行），`current_scores[i - 1]` 就是上面的格子 \( S(i-1, j) \)（本列上一行），`previous_scores[i - 1]` 是左上对角格子 \( S(i-1, j-1) \)。**列主序布局 + "逐列填表"在这里兑现**：一列内从上往下算（用本列刚写出的 `current_scores[i-1]`），列与列之间从左往右推进（用整列的 `previous_scores`）。

而第 0 行的边界在第 164 行，每个新列开始时先写入：

[streaming_diff.rs:164-164](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L164-L164)

```rust
current_scores[0] = j as f64 * Self::INSERTION_SCORE;
```

即 \( S(0, j) = -j \)。它与本讲的第 0 列共同构成 DP 的"两面墙"。

#### 4.1.4 代码实践

**实践目标**：不写代码，只用纸笔验证你对三种子问题的理解——手算一个小矩阵的**第 0 列和第 0 行**。

**操作步骤**：

1. 取旧文本 `old = "ab"`，新文本第一个分块为 `"b"`。
2. 画出 3 行（\( i = 0, 1, 2 \)）× 2 列（\( j = 0, 1 \)）的空表。
3. 用 \( S(i, 0) = -20i \) 填第 0 列；用 \( S(0, j) = -j \) 填第 0 行。

**需要观察的现象**：第 0 列的数值跌得极快（0、−20、−40），第 0 行跌得慢（0、−1）。

**预期结果**（手算）：

| | j=0 | j=1 |
|---|---|---|
| **i=0** | 0 | −1 |
| **i=1** | −20 | ？ |
| **i=2** | −40 | ？ |

两个"？"留到 4.2.4 的实践中由完整的 DP 填出。实际数值请以本地运行输出为准（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么错配时对角分支用 `f64::NEG_INFINITY` 而不是 0 或某个负数？

**参考答案**：`NEG_INFINITY` 表示"这条路根本不存在"，任何有限分数在 `max` 中都会胜过它。若用 0，则错配格子的对角候选可能反而超过插入/删除候选，导致回溯时"免费"走过一个不相等的字符对，破坏"Keep 的字符必须真的相同"这一语义。

**练习 2**：`S(2, 0) = -40` 的直觉解释是什么？

**参考答案**：新文本尚未到达任何字符时，旧文本 `ab` 的两个字符都无处安放，只能全部删除；每删一个字符付出 20 分，共 −40。它衡量的是"如果一点都不复用旧文本，起点有多惨"。

**练习 3**：矩阵为什么是 `(old_len + 1) × (new_len + 1)` 而不是 `old_len × new_len`？

**参考答案**：\( i \) 和 \( j \) 表示的是**前缀长度**而非下标，"空前缀"（什么都不对齐）是合法状态，也是边界条件的载体。少了这一行一列，递推在 \( i=0 \) 或 \( j=0 \) 处就没有落脚点。

### 4.2 StreamingDiff::new：构造函数与第 0 列初始化

#### 4.2.1 概念说明

`new` 是整个流式计算的"奠基仪式"：旧文本在构造时刻就**完整固定**（这是本 crate 的核心假设——只有新文本是流的），所以与旧文本长度相关的所有初始化在此一次做完：

1. 把旧文本转成 `Vec<char>`（之后按字符比较、按下标随机访问）。
2. 分配打分矩阵，先建成只有 1 列的"火柴棍"形状。
3. 把这一列填成删除代价，作为 DP 的第 0 列。
4. 初始化两个游标锚点（`old_text_ix` / `new_text_ix`）和相等游程双缓冲。

#### 4.2.2 核心流程

```text
StreamingDiff::new(old: String)
  ├─ old.chars().collect()          # String → Vec<char>，统一按字符处理
  ├─ scores = Matrix::new()          # 空矩阵（0×0）
  ├─ scores.resize(old_len + 1, 1)   # 扩成 (old_len+1) × 1，全 0
  ├─ for i in 0..=old_len:
  │     scores.set(i, 0, i * DELETION_SCORE)   # 第 0 列 = 删除初始化
  └─ 返回 Self { old, new: [], scores,
                 old_text_ix: 0, new_text_ix: 0,
                 previous_equal_runs: [0; old_len+1],
                 current_equal_runs: [0; old_len+1] }
```

注意循环是 `0..=old_len`（闭区间），所以第 0 列共 `old_len + 1` 个格子全部写入，其中 `S(0,0) = 0`（空对空，不付任何代价）。

#### 4.2.3 源码精读

先看结构体定义，明确 `new` 要初始化的七个字段：

[streaming_diff.rs:113-122](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L113-L122)

```rust
pub struct StreamingDiff {
    old: Vec<char>,
    new: Vec<char>,
    scores: Matrix,
    old_text_ix: usize,
    new_text_ix: usize,
    previous_equal_runs: Vec<u32>,
    current_equal_runs: Vec<u32>,
}
```

`old` / `new` 是两段文本的字符数组；`scores` 是打分矩阵；`old_text_ix` / `new_text_ix` 是**锚点**（已"结算"完毕、回溯不必再越过的坐标，本讲中它们是 0）；两个 `equal_runs` 数组是 u2-l2 讲过的相等游程双缓冲，长度同样是 `old_len + 1`（每行一个游程值）。

构造函数本体：

[streaming_diff.rs:130-147](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L130-L147)

```rust
pub fn new(old: String) -> Self {
    let old = old.chars().collect::<Vec<_>>();
    let old_len = old.len();
    let mut scores = Matrix::new();
    scores.resize(old_len + 1, 1);
    for i in 0..=old_len {
        scores.set(i, 0, i as f64 * Self::DELETION_SCORE);
    }
    Self {
        old,
        new: Vec::new(),
        scores,
        old_text_ix: 0,
        new_text_ix: 0,
        previous_equal_runs: vec![0; old_len + 1],
        current_equal_runs: vec![0; old_len + 1],
    }
}
```

逐行解读：

- **第 131 行**：`old.chars().collect::<Vec<_>>()`。`String` 不支持 O(1) 随机按下标取字符（UTF-8 变长编码），而 DP 需要高频随机访问 `old[i-1]`，所以一次性转成 `Vec<char>`。代价是内存放大（每个 `char` 占 4 字节），换来的是整个算法期间 O(1) 的比较访问。
- **第 134 行**：`scores.resize(old_len + 1, 1)`。矩阵初始只有 1 列——此时新文本长度为 0，正好对应 \( j = 0 \) 这一列。**列数会随后续 `push_new` 增长，行数永远固定为 `old_len + 1`**（旧文本不变）。
- **第 135–137 行**：把第 0 列填成 \( i \times (-20) \)。注意 `i as f64` 的显式转换：整数乘浮点常量需要统一类型。
- **第 138–146 行**：组装七个字段。`new` 为空、两个锚点归零、游程缓冲全 0。

打分常量就定义在 `impl` 块的开头，供这里和 `push_new` 共用：

[streaming_diff.rs:125-128](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L125-L128)

```rust
const INSERTION_SCORE: f64 = -1.;
const DELETION_SCORE: f64 = -20.;
const EQUALITY_BASE: f64 = 1.8;
const MAX_EQUALITY_EXPONENT: i32 = 16;
```

顺带看一个真实调用现场——随机测试辅助函数第一行就是 `new`，随后在一个 `while` 循环里反复 `push_new`，最后 `finish` 收尾。这就是本 crate 的标准使用姿势：

[streaming_diff.rs:963-981](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L963-L981)

```rust
fn random_streaming_diff(rng: &mut impl Rng, old: &str, new: &str) -> Vec<CharOperation> {
    let mut diff = StreamingDiff::new(old.to_string());
    // ……按随机大小分块循环 push_new，收集 CharOperation……
    char_operations.extend(diff.finish());
    char_operations
}
```

#### 4.2.4 代码实践

**实践目标**：完成 4.1.4 那张表里剩下的两个"？"——手算 `old = "ab"`、首个新块为 `"b"` 时的完整 3×2 分数矩阵，再用源码自带的 `Debug` 实现打印核对。

**操作步骤**：

1. 在仓库外新建一个练习目录（例如 `~/scratch/sd-init/`），运行 `cargo init --name sd_init`。
2. 把 `crates/streaming_diff/src/streaming_diff.rs` 中 `Matrix`（第 10–104 行，含 `Debug` 实现）和 `StreamingDiff`（第 113–199 行，`new` 与 `push_new`，连带常量）复制进 `src/main.rs`；`main` 函数里 `use` 所需的 `ordered_float::OrderedFloat` 等依赖（若不想引入依赖，可先把 `backtrack` 中用到 `OrderedFloat` 的部分删掉，只保留 `new` 和 `push_new` 的填表逻辑）。在 `Cargo.toml` 加 `ordered-float = "4"`。
3. 在 `main` 中写下（**示例代码**，非项目原有代码）：

   ```rust
   fn main() {
       let mut diff = StreamingDiff::new("ab".to_string());
       println!("初始化后（仅第 0 列）:{:?}", diff.scores);
       let ops = diff.push_new("b");
       println!("push_new(\"b\") 之后:{:?}", diff.scores);
       println!("返回的操作: {:?}", ops);
   }
   ```

   （`scores` 是私有字段，练习副本里可把它改成 `pub` 或给 `StreamingDiff` 加一个打印方法。）

4. `cargo run` 观察两次打印。

**需要观察的现象**：第一次打印是单列 `[0, -20, -40]`；第二次打印变成 3 行 × 2 列的完整矩阵，其中第 0 列仍是 `[0, -20, -40]`（被保留），第 1 列顶部是 −1。

**预期结果**（手算推导，请以本地输出为准，待本地验证）：

| | j=0 | j=1（新字符 'b'） |
|---|---|---|
| **i=0** | 0 | −1（第 0 行：\( 1 \times (-1) \)） |
| **i=1**（'a'） | −20 | max(−20−1, −1−20, −∞) = **−21**（'a' ≠ 'b'，无对角候选） |
| **i=2**（'b'） | −40 | 对角命中：\( S(1,0) + 1.8^{\min(\lfloor 1/4 \rfloor, 16)} = -20 + 1.8^0 = \) **−19**，胜过插入（−41）与删除（−41） |

最后一列的最大分数是 −19（在 \( i = 2 \) 处），回溯会沿对角（Keep 'b'）再向上（Delete 'a'）走出 `Delete{1}, Keep{1}` 之类的操作序列——把 `"ab"` 补丁成 `"b"`。回溯细节是下一讲（u3-l3）的内容，这里只需核对矩阵数值。

> 小提示：`Debug` 实现用 `{:5}` 逐格排版（见第 4.3.3 节），负数与小数会占满宽度，读起来是"表格"而不是一行数组。

#### 4.2.5 小练习与答案

**练习 1**：如果 `old` 是空字符串 `""`，`new` 会发生什么？矩阵长什么样？

**参考答案**：`old_len = 0`，矩阵 resize 成 `1 × 1`，唯一格子 `S(0,0) = 0`；两个游程缓冲长度为 1。之后每次 `push_new` 只填第 0 行（\( S(0,j) = -j \)），所有新字符都表现为 Insert——没有旧文本可复用，纯插入正是正确答案。

**练习 2**：为什么 `new` 接收的是 `String`（拿走所有权）而不是 `&str`？

**参考答案**：因为要把旧文本转成 `Vec<char>` 并在整个流式生命周期内持有。接收所有权让调用方明确"旧文本从此交给 diff 管理、不可再变"；若借用 `&str` 则需要处理生命周期与"调用方中途修改旧文本"的一致性问题，与"旧文本固定"的核心假设冲突。

**练习 3**：把第 0 列的初始化从 `i * DELETION_SCORE` 改成全 0，算法还能得到正确（可重建新文本）的结果吗？会改变什么？

**参考答案**：仍能重建新文本——正确性依赖的是回溯路径的连贯性而非分数绝对值。但它会改变**路径选择**：删除旧字符的前置代价被清零后，"删掉旧文本再插入新文本"与"保留匹配"之间的分差被大幅压缩，diff 会更容易放弃复用旧文本，输出更多 Delete+Insert 而不是 Keep，行级差异也会变得更碎。（推理性结论，可用 4.2.4 的练习副本改一行验证，待本地验证。）

### 4.3 Matrix::resize 与 Matrix::set：初始化的底层支撑

#### 4.3.1 概念说明

`new` 的第 134–137 行是"先 `resize` 后 `set`"的两步舞。这两个方法上一单元（u2-l1）已从容器角度剖析过，本讲从**初始化流程**的角度再串一遍：

- `resize` 负责**开辟战场**：把一维 `Vec<f64>` 拉伸到需要的格子数，新格子补 0。
- `set` 负责**落子**：带行列双重越界检查地写入某个格子。

顺序不可颠倒：`set` 在 `resize` 之前调用会因越界 panic。

#### 4.3.2 核心流程

```text
Matrix::new()          # cells=[], rows=0, cols=0
    │
    ▼
resize(R, C)           # cells.resize(R*C, 0.); rows=R; cols=C
    │                  # 尾部补零 → 行不变时等价于"右侧新增全 0 列"
    ▼
set(i, 0, v) × (R 次)  # 逐格写入第 0 列，越界即 panic("row/column out of bounds")
```

在 `new` 的场景里 \( R = old\_len + 1 \)、\( C = 1 \)，所以 `resize` 从空向量直接建出 \( R \) 个 0，随后循环把它们覆写成 \( 0, -20, -40, \dots \)。

#### 4.3.3 源码精读

[streaming_diff.rs:26-30](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L26-L30)

```rust
fn resize(&mut self, rows: usize, cols: usize) {
    self.cells.resize(rows * cols, 0.);
    self.rows = rows;
    self.cols = cols;
}
```

三行代码：先按总格子数扩缩一维缓冲（补 0），再更新尺寸记录。**它不检查"rows 是否变化"**——在 `new` 中从 `(0, 0)` 到 `(old_len+1, 1)` 当然安全；在后续 `push_new` 中行数恒为 `old_len + 1`、只增列数，所以"尾部补零 = 右侧新增全 0 列"的不变量始终成立（u2-l1 的结论在此被 `new` 首次使用）。

[streaming_diff.rs:66-76](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L66-L76)

```rust
fn set(&mut self, row: usize, col: usize, value: f64) {
    if row >= self.rows {
        panic!("row out of bounds")
    }
    if col >= self.cols {
        panic!("column out of bounds")
    }
    self.cells[col * self.rows + row] = value;
}
```

两道防线之后才做列主序下标换算 `col * rows + row`（u2-l1 讲过的布局）。对照读取端 `get`（第 55–64 行，同样的双重检查）。初始化循环里 `set(i, 0, …)` 的 `col` 恒为 0，所以实际写入位置就是 `cells[i]`——第 0 列物理上恰是一维缓冲的最前面一段。

再看本讲实践的观察工具——`Matrix` 的 `Debug` 实现：

[streaming_diff.rs:93-104](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L93-L104)

```rust
impl Debug for Matrix {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        writeln!(f)?;
        for i in 0..self.rows {
            for j in 0..self.cols {
                write!(f, "{:5}", self.get(i, j))?;
            }
            writeln!(f)?;
        }
        Ok(())
    }
}
```

手写 `Debug`（而不是派生）正是为了按"行 × 列"的二维面貌打印——派生只会吐出一维 `cells` 数组。它通过公开的 `get` 读值，本身不碰私有布局，这也是它能安全存在于 `impl Matrix` 之外的原因。

#### 4.3.4 代码实践

**实践目标**：亲眼验证"先 `resize` 后 `set`"的顺序约束与扩容保留语义。

**操作步骤**（在 4.2.4 的练习副本里继续，**示例代码**）：

1. 在 `main` 中先构造 `Matrix::new()`，直接调用 `set(0, 0, 1.0)`，`cargo run` 观察 panic 信息。
2. 注释掉上一行，改为 `resize(3, 1)` 后 `set` 三个值并 `println!("{:?}", matrix)`。
3. 再调用 `resize(3, 2)`（行数不变、加一列），打印，观察第 0 列原值与第 1 列的取值。

**需要观察的现象**：第 1 步得到 `row out of bounds`（或 `column out of bounds`，取决于检查顺序——源码先查行）的 panic；第 3 步第 0 列原值保留、第 1 列全为 0。

**预期结果**：与 [streaming_diff.rs:67-75](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L67-L75) 的检查逻辑和 `Vec::resize` 尾部补零语义一致（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`resize` 里如果先更新 `self.rows` / `self.cols` 再 `cells.resize`，会出什么问题？

**参考答案**：本题场景下恰好无事，但顺序一旦对调，`resize` 与 `set`/`get` 之间存在被观察到"尺寸已变、数据未变"的中间状态的风险（尤其在多线程共享时可导致按新尺寸索引旧缓冲而越界）。先改数据后改元数据是让"尺寸与内容同步生效"的稳妥写法。

**练习 2**：为什么初始化循环不直接 `scores.cells[i] = …` 而要走 `set`？

**参考答案**：`set` 提供带语义的越界检查与统一的列主序换算。直接摸 `cells` 虽然在第 0 列这个特例下碰巧等价（`col=0` 时下标就是 `row`），但把布局知识泄漏到调用点，一旦布局调整这里就会悄悄写错。`set` 的 panic 消息（`row out of bounds`）也比裸索引 panic 更可诊断。

### 4.4 与教科书 Needleman-Wunsch 的异同

#### 4.4.1 概念说明

把本实现放回算法谱系，能看清哪些是"继承"、哪些是"魔改"：

| 维度 | 教科书 Needleman-Wunsch | streaming_diff |
|---|---|---|
| 状态定义 | \( S(i,j) \)：两段序列前缀的最优对齐分 | 相同 |
| 三方向递推 | 插入 / 删除 / 匹配（或错配） | 相同骨架 |
| 分数类型 | 整数（如 match +1、mismatch −1、gap −1） | `f64` 浮点（因为相等奖励是 \( 1.8^k \)） |
| 两种 gap 代价 | 通常对称（同一个 gap 罚分） | **不对称**：插 −1、删 −20 |
| 匹配奖励 | 每个匹配固定 +1 | 随**连续相等游程**指数增长 \( 1.8^{\min(\lfloor r/4 \rfloor, 16)} \) |
| 起点 | \( S(0,0)=0 \)，边界为累积 gap 罚分 | 相同（第 0 列 \( -20i \)、第 0 行 \( -j \)） |
| 终点 | 固定在右下角 \( (m, n) \) 回溯 | **不固定**：最后一列中取分数最大的行作为锚点再回溯 |
| 矩阵形态 | 完整 \( (m+1) \times (n+1) \) 表 | 只保留可滚动的列带（下一讲的 `swap_columns` 技巧） |

#### 4.4.2 核心流程

后三行差异正是"流式"二字的来源，逻辑链是：

```text
新文本分块到达、且每块都要立刻产出 diff
    │
    ├─ 终点不能等 (m, n) 凑齐 → 每轮在当前最后一列里挑分数最大的行锚定
    │    （旧文本尾部尚未对上的部分留给后续轮次，即 old_text_ix 可小于 m）
    │
    ├─ 新文本每次只多几列 → 完整大矩阵浪费内存 → 只留边界列 + 若干新列
    │
    └─ 面向 LLM 输出打分 → 保留旧文本优先（删贵插贱）、长匹配指数奖励
```

数学上，每一轮 `push_new` 求解的其实是一个**以锚点为原点的半全局（semi-global）对齐**：起点固定在 \( (old\_text\_ix, new\_text\_ix) \)，终点在旧文本方向自由、在新文本方向固定为当前末列。这与 NW 的"两端都固定"形成关键区别。

#### 4.4.3 源码精读

终点自由的证据在 `push_new` 末段（本讲只指路，u3-l3 精读）：

[streaming_diff.rs:184-193](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L184-L193)

```rust
let mut max_score = f64::NEG_INFINITY;
let mut next_old_text_ix = self.old_text_ix;
let next_new_text_ix = self.new.len();
for i in self.old_text_ix..=self.old.len() {
    let score = self.scores.get(i, next_new_text_ix - self.new_text_ix);
    if score > max_score {
        max_score = score;
        next_old_text_ix = i;
    }
}
```

教科书在 \( (m, n) \) 处开始回溯；这里则遍历**最后一列的所有行**（\( i \) 从当前锚点到 `old.len()`），挑分数最高者作为新锚点 `next_old_text_ix`。`finish`（u3-l4 的主题）则会把终点钉到旧文本末尾，做一次"收官对齐"。

浮点分数与不对称代价的源头就是 [streaming_diff.rs:125-128](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L125-L128) 那四个常量——u2-l2 已从"价值观"角度解读过，此处只强调它们在本讲的落点：**第 0 列的斜率就是 `DELETION_SCORE`**，改这个常量，构造函数写下的边界线就整体变陡或变缓。

#### 4.4.4 代码实践

**实践目标**：用 4.2.4 的练习副本，体感"浮点分数"如何进入矩阵。

**操作步骤**（**示例代码**）：

1. 把练习副本中的 `MAX_EQUALITY_EXPONENT` 保持 16 不动，改 `main` 为构造 `StreamingDiff::new("abcd".to_string())` 并 `push_new("abcd")`。
2. 打印矩阵，聚焦对角线格子 \( S(1,1) \dots S(4,4) \)。
3. 再改为 `new("ab".to_string())` + `push_new("ab")`，对比对角线数值。

**需要观察的现象**：`"abcd"` 案例中前三个对角格每次恰好 +1（游程 1、2、3 的指数都是 \( \lfloor r/4 \rfloor = 0 \)，即 \( 1.8^0 = 1 \)），第四个对角格增加 **1.8**（游程 4，指数 1）。

**预期结果**（手算推导）：`S(1,1)=1, S(2,2)=2, S(3,3)=3, S(4,4)=4.8`。整数分数的教科书 NW 永远不可能出现 4.8 这样的格子——这是"浮点分数 + 游程指数奖励"打分模型的直接印记（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：NW 固定终点 \( (m,n) \)，streaming_diff 每轮取"最后一列分数最大的行"。如果强行把它改回固定终点 \( (m, n) \)，流式场景会遇到什么麻烦？

**参考答案**：流式过程中新文本远未到齐，\( n \) 每轮都在变；更麻烦的是要求 \( i = m \)（旧文本必须全部消费完）。若某轮新文本恰好只覆盖旧文本的开头（比如旧文本后半段还没被新输出触及），强制对齐到 \( (m,n) \) 会把旧文本尾部大段"删除再等未来重插"，产生剧烈抖动的中间 diff。自由终点允许"旧文本尾部先挂着，等下一块新文本来了再说"，这正是 `old_text_ix` 锚点存在的意义。

**练习 2**：为什么这个实现必须用 `f64` 而教科书 NW 常用整数？

**参考答案**：相等奖励是 \( 1.8^k \) 形式的指数函数，结果天然是非整数；且指数上限 16（\( 1.8^{16} \approx 7625 \)）意味着分数范围较宽。用整数需要预先放大取整、损失精度并改变路径选择的 tie-break 行为。浮点带来的比较问题则由 `OrderedFloat` 解决（u3-l3 会讲到）。

**练习 3**：构造函数里第 0 列填 `i * DELETION_SCORE`，第 0 行却在 `push_new` 里按 `j * INSERTION_SCORE` 现算。两者为什么不放在同一个地方？

**参考答案**：第 0 列只依赖旧文本——构造时信息齐备，可以一次性算死。第 0 行的 \( j \) 会随新文本增长而增长、且每轮矩阵被重新锚定（列号是相对锚点的），不可能在构造时预知，只能在 `push_new` 逐列现写。这个"能定的先定、不能定的留到现场"的分野，正是流式初始化与一次性初始化的分界线。

## 5. 综合实践

**任务：给 `StreamingDiff::new` 写一份"初始化快照"验证器。**

在 4.2.4 / 4.3.4 的练习副本上继续（全部为**示例代码**，不要改动仓库源码）：

1. 写一个函数 `snapshot(old: &str) -> Vec<f64>`：构造 `StreamingDiff::new(old.to_string())` 后，通过 `scores.get(i, 0)`（练习副本中可将 `scores` 设为 `pub` 或加 getter）把第 0 列读出返回。
2. 对 `"ab"`、`""`、`"你好"`（多字节字符，验证按 `char` 而非字节计数）三个输入分别断言：
   - `"ab"` → `[0.0, -20.0, -40.0]`；
   - `""` → `[0.0]`；
   - `"你好"` → `[0.0, -20.0, -40.0]`（两个 `char`，与 `"ab"` 相同——初始化只数字符数，不看字节宽度）。
3. 接着对 `"你好"` 调用 `push_new("你")`，手算并断言完整矩阵第 1 列为 `[-1.0, -19.0, -39.0]`（'你' 与 `old[0]` 相等走对角 +1，与 `old[1]` '好' 无关的格子只能插入/删除取最大）。推导过程与 4.2.4 的 `"ab"` / `"b"` 完全同构。
4. 用 `println!("{:?}", diff.scores)` 打印每一张矩阵，与你的手算表并排贴在笔记里。

这个任务把本讲三个最小模块全部串起：`new` 的第 0 列（模块一）、`resize` 开辟的矩阵形态（模块二）、`set` 写入并由 `get`/`Debug` 读回（模块三），并顺带验证了"按字符计数"这一 `Vec<char>` 转换的动机。全部数值断言以本地 `cargo run` / `cargo test` 输出为准（待本地验证）。

## 6. 本讲小结

- DP 状态 \( S(i,j) \) 表示"旧前 \( i \) 字符对新前 \( j \) 字符的最优对齐分"，矩阵多出一行一列放空前缀边界。
- 三种子问题对应三个前驱方向：插入（左格 −1）、删除（上格 −20）、相等（左上格 + 游程奖励，错配为 \( -\infty \)）。
- `StreamingDiff::new` 做四件事：旧文本转 `Vec<char>`、矩阵 resize 成 `(old_len+1) × 1`、第 0 列填 \( -20i \)、初始化锚点与游程双缓冲。
- 第 0 列由 `new` 一次算死（只依赖旧文本），第 0 行由 `push_new` 每列现算（依赖不断增长的新文本）——"能定的先定"是流式初始化的分界线。
- 与教科书 NW 相比：骨架相同，差异在浮点分数、不对称 gap 代价、游程指数奖励、自由终点锚定与滚动列带。
- `Matrix` 手写的 `Debug` 实现按二维排版打印，是核对手算矩阵的现成工具。

## 7. 下一步学习建议

第 0 列已经立好，下一讲 **u3-l2「push_new 主循环：矩阵滚动与增量填表」**将进入本 crate 最精彩的部分：`swap_columns(0, cols-1)` 如何把上一轮的边界列滚动为新一轮的第 0 列、`resize` 如何在行数不变的前提下扩出新列、`adjacent_columns_mut` 如何用 `split_at_mut` 同时借出"上一列只读 + 当前列可写"。建议先读 [streaming_diff.rs:149-199](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L149-L199) 也就是 `push_new` 全函数，带着本讲的手算矩阵去对照每一列的诞生过程；之后再进入 u3-l3 的回溯与锚点。

# 打分模型:为什么删除比插入贵 20 倍

## 1. 本讲目标

学完本讲,你应该能够:

- 说出 `INSERTION_SCORE`、`DELETION_SCORE`、`EQUALITY_BASE`、`MAX_EQUALITY_EXPONENT` 四个常量的数值,以及它们各自出现在源码的哪些位置。
- 写出打分动态规划的递推式,解释"插入 / 删除 / 相等"三种选择分别给路径总分贡献多少。
- 解释连续相等游程 `equal_run` 如何通过 \( 1.8^{\min(\lfloor r/4 \rfloor,\, 16)} \) 这条阶梯式指数曲线获得奖励,以及为什么要设上限。
- 分析"删除比插入贵 20 倍"这一不对称设计如何让 diff 倾向于"保留旧文本 + 把新内容表达为插入",正好匹配 LLM 流式输出的场景;并且能动手改常量、观察行为变化。

## 2. 前置知识

本讲只讲一件事:**分数是怎么算出来的**。你不需要完整理解 `push_new` 的矩阵滚动机制(那是下一单元 u3-l2 的内容),但需要以下直觉。

**打分式动态规划(序列对齐)。** 求 diff 可以看成:在一个二维表格里从左上角走到某个终点,每一步三选一——向右(插入一个新字符)、向下(删除一个旧字符)、沿对角线(新旧字符配对保留)。每走一步会获得一个分数,路径总分是所有步骤分数之和,算法的目标是**总分最大**。在本 crate 里:

- 行坐标 `i` 对应旧文本前 `i` 个字符,列坐标 `j` 对应新文本前 `j` 个字符(下一单元 u3-l1 会详细讲初始化);
- 插入和删除是"付出代价",分数为**负**;
- 相等匹配是"获得奖励",分数为**正**。

于是"最大化总分"天然等价于"少改动、多保留"。这正是我们在 u1-l2 里学过的操作序列(编辑脚本)的另一种视角:一条路径 ↔ 一份编辑脚本。

**游程(run)。** 连续若干次沿对角线、且每一步新旧字符都相等的长度,记作 \( r \)。本讲的指数奖励不是"每匹配一个字符加固定分",而是"匹配得越连续,每个字符的奖励越高"。

**几个 Rust / 浮点细节。**

- `f64::powi(e)` 是以整数 `i32` 为指数的快速幂,源码里用它计算 \( 1.8^e \)。
- `f64::NEG_INFINITY`(负无穷)在 `max` 中永远不可能胜出,源码用它表示"这一步不合法"。
- 分数是浮点数,而 `f64` 没实现 `Ord`,所以回溯阶段用 `ordered_float::OrderedFloat` 包一层才能放进 `max_by_key` 比较——这是 `Cargo.toml` 中 `ordered-float` 依赖存在的主要原因之一([crates/streaming_diff/Cargo.toml:L14-L16](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/Cargo.toml#L14-L16))。

**复习两讲的内容。** u1-l2:三种 `CharOperation` 中 `Keep`/`Delete` 只带字节数、`Insert` 携带文本;u2-l1:`Matrix` 是列主序的一维 `Vec<f64>`,`adjacent_columns_mut` 把"上一列(只读)+ 当前列(可写)"两个窗口同时借出来。本讲只把这两个窗口当作黑盒使用。

## 3. 本讲源码地图

本讲全部内容集中在一个文件里,按下表定位:

| 代码位置 | 作用 |
| --- | --- |
| [crates/streaming_diff/src/streaming_diff.rs:L124-L128](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L124-L128) | 四个打分常量的定义,本讲的主角 |
| [crates/streaming_diff/src/streaming_diff.rs:L113-L122](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L113-L122) | `StreamingDiff` 的字段,含游程双缓冲 `previous_equal_runs` / `current_equal_runs` |
| [crates/streaming_diff/src/streaming_diff.rs:L130-L147](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L130-L147) | `new()`:第 0 列的删除初始化 |
| [crates/streaming_diff/src/streaming_diff.rs:L155-L182](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L155-L182) | `push_new()` 的列循环:三种候选分数与递推 |
| [crates/streaming_diff/src/streaming_diff.rs:L184-L193](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L184-L193) | 锚点选择:分数的第一个下游消费者 |
| [crates/streaming_diff/src/streaming_diff.rs:L227-L233](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L227-L233) | 回溯时用 `OrderedFloat` 比较单元格分数 |
| [crates/streaming_diff/src/streaming_diff.rs:L93-L104](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L93-L104) | `Matrix` 的 `Debug` 实现,实践时用来打印分数表 |
| [crates/streaming_diff/src/streaming_diff.rs:L1104-L1124](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L1104-L1124) | `apply_char_operations`:实践用的校验器 |

## 4. 核心概念与源码讲解

### 4.1 一格三种选择:打分递推全景

#### 4.1.1 概念说明

打分模型的全部信息浓缩在四个关联常量里:

```rust
const INSERTION_SCORE: f64 = -1.;
const DELETION_SCORE: f64 = -20.;
const EQUALITY_BASE: f64 = 1.8;
const MAX_EQUALITY_EXPONENT: i32 = 16;
```

这是 [crates/streaming_diff/src/streaming_diff.rs:L124-L128](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L124-L128) 的原文。注意:**源码没有任何注释解释这些数值**,它们是经验调参值(为什么恰好是 -20 和 1.8,待确认)。本讲要做的是"从数值的效果反推设计意图",而不是转述注释。

递推的含义:`S(i, j)` 表示"消费了旧文本前 `i` 个字符、新文本前 `j` 个字符"这条对齐路径能取得的最高总分。表格中每一格的分数,只依赖左边一格(插入)、上边一格(删除)和左上一格(相等)三者,取最大。

#### 4.1.2 核心流程

用数学语言写出递推式,记 \(\sigma_{\mathrm{ins}} = -1\)、\(\sigma_{\mathrm{del}} = -20\):

\[ S(i,j)=\max\begin{cases} S(i,\,j-1)+\sigma_{\mathrm{ins}} & \text{(插入新字符 } j\text{)}\\[2pt] S(i-1,\,j)+\sigma_{\mathrm{del}} & \text{(删除旧字符 } i\text{)}\\[2pt] S(i-1,\,j-1)+B(r) & \text{(仅当 } \texttt{old}[i-1]=\texttt{new}[j-1]\text{,否则为 } -\infty\text{)} \end{cases} \]

其中相等奖励函数(\(r\) 是结束于前驱单元格的连续相等游程长度):

\[ B(r) = 1.8^{\min(\lfloor r/4 \rfloor,\, 16)} \]

两条边界(常量最先露脸的地方):

\[ S(i, 0) = -20 \cdot i \qquad S(0, j) = -1 \cdot j \]

即"把旧文本前 `i` 个字符全删掉"的累计代价,和"把新文本前 `j` 个字符全插进来"的累计代价。

伪代码(只看打分,忽略矩阵滚动):

```text
对每个新到达的字符 j:
    S(0, j) := j × (-1)                     # 行 0 边界:只能靠插入
    对每个旧行 i = 1 ..= old.len():
        插入候选 := S(i, j-1) + (-1)          # 新字符 j 作为插入
        删除候选 := S(i-1, j) + (-20)         # 旧字符 i 被删除
        相等候选 := old[i-1] == new[j-1] ?
                    S(i-1, j-1) + B(r) : -∞  # 对角匹配
        S(i, j) := max(三个候选)
```

#### 4.1.3 源码精读

递推主体在 `push_new` 的行循环里:

- [crates/streaming_diff/src/streaming_diff.rs:L164-L167](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L164-L167):行 0 的边界 `current_scores[0] = j * INSERTION_SCORE`(`j` 是**绝对**新字符计数,这样跨多次 `push_new` 调用分数仍然可比);随后取到上一列(`previous_scores`)与当前列(`current_scores`)这两个窗口——这正是 u2-l1 讲过的 `adjacent_columns_mut`。
- [crates/streaming_diff/src/streaming_diff.rs:L166](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L166):`insertion_score = previous_scores[i] + INSERTION_SCORE`——同一行、上一列,即"先走到 (i, j-1),再插入新字符 j"。
- [crates/streaming_diff/src/streaming_diff.rs:L167](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L167):`deletion_score = current_scores[i - 1] + DELETION_SCORE`——同一列、上一行,即"先删除旧字符 i 再继续"。
- [crates/streaming_diff/src/streaming_diff.rs:L168-L176](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L168-L176):相等分支。字符相等时从左上角前驱取分并加上奖励 \( B(r) \);**不相等时直接取 `f64::NEG_INFINITY`**(L174-L175),这样 L178 的 `max` 永远不可能选中"错配的对角步"——这是用负无穷表达"禁止走法"的标准技巧,比加一个很大的负数更干净(不会与真实分数比较时出歧义)。
- [crates/streaming_diff/src/streaming_diff.rs:L178](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L178):`current_scores[i] = insertion_score.max(deletion_score).max(equality_score)`——三个候选取最大,一行就是整个打分模型。

第 0 列的边界在构造函数里:

- [crates/streaming_diff/src/streaming_diff.rs:L134-L137](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L134-L137):`scores.resize(old_len + 1, 1)` 后把第 0 列填成 `i as f64 * DELETION_SCORE`,即 \( S(i,0) = -20i \)。矩阵构造的完整流程留给 u3-l1。

比较字符用的是 `old[i - 1] == new_char`(L168)——按 `char`(Unicode 标量值)比较;而输出的操作用 `len_utf8` 字节数计量。这就是 u1-l2 总结的"按字符比较、按字节计量"在打分层的具体体现。

#### 4.1.4 代码实践

**(1)实践目标:** 不依赖任何推断,亲眼看到两条边界公式的数值。

**(2)操作步骤(示例代码,非项目原有代码):**

1. 新建一个临时练习 crate:`cargo new scoring-lab && cd scoring-lab`,添加依赖 `ordered-float`。
2. 从 [crates/streaming_diff/src/streaming_diff.rs:L10-L104](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L10-L104) 复制 `Matrix`、从 L106-L111 复制 `CharOperation`、从 L113-L279 复制 `StreamingDiff`(**不要复制 `LineDiff` 部分**,那样就不用引入 `rope` 依赖;同时删掉文件顶部的 `use rope::...`)。
3. 在副本里临时把 `scores` 字段设为 `pub`(本地副本允许改,原工程不动),然后在 `main` 里:

```rust
let diff = StreamingDiff::new("abc".to_string());
println!("{:?}", diff.scores);   // Matrix 已有 Debug 实现(L93-L104)
```

**(3)需要观察的现象:** 打印出来的是一个 4 行 1 列的矩阵,只有第 0 列。

**(4)预期结果:** 第 0 列自上而下为 `0、-20、-40、-60`,即 \( S(i,0) = -20i \)。这是由 L134-L137 直接决定的确定结果;再调用 `push_new("x")` 后,新列第 0 行应为 `-1`(\( S(0,1) = -1 \))。

#### 4.1.5 小练习与答案

**练习 1:** `old = "abcd"` 时,构造完成后第 0 列有哪几个值?

**答案:** 5 个值(行 0 到行 4):`0、-20、-40、-60、-80`。第 0 列有 `old_len + 1` 行,"删除前 `i` 个旧字符"累计付出 \( 20i \) 分。

**练习 2:** 为什么不相等时的相等候选是 `NEG_INFINITY`,而不是 0 或某个大负数?

**答案:** L178 要对三个候选做 `max`。取 `NEG_INFINITY` 保证"错配对角步"在任何情况下都不可能胜出;若取 0,会给未匹配的对角步凭空加 0 分,当插入/删除候选都是负数时(流式前期很常见),`max` 会错误地选中错配,把不相同的字符标成 `Keep`,diff 直接错误。取"很大的负数"虽然通常也能工作,但与真实路径分数的差值有限,不如负无穷语义干净。

**练习 3:** 行 0 的边界为什么用绝对计数 `j`(而不是本次 `push_new` 内的相对列号)?

**答案:** 行 0 只能靠"插入"到达:上一列行 0 的值是 \( (j-1)\times(-1) \),递推一步就是 \( j \times (-1) \)。由于锚点推进后列号是相对的、而 `j` 在循环里是绝对索引(L155),用绝对 `j` 恰好与"从上一列行 0 加一次插入代价"严格一致,保证分数在多次 `push_new` 调用之间连续可比。

### 4.2 插入 -1、删除 -20:不对称的编辑代价

#### 4.2.1 概念说明

把两个代价放在一起看:

| 对齐方式 | 分数贡献 |
| --- | --- |
| `Keep` 一个相同字符 | \( B(r) \ge +1.0 \)(游程最短时也有 +1) |
| 删除一个旧字符 | \(-20\) |
| 插入一个新字符 | \(-1\) |
| "删了再重插"同一个字符 | \(-20 + (-1) = -21\) |
| 真正修改一个字符(旧 `b` → 新 `B`) | 同样必须 \(-21\),别无选择 |

先看**符号层面**能推出的结论:只要插入、删除代价都是负数,"保留一个相同字符"(至少 +1)就永远碾压"删掉它再插入"(至多 −1)。差距至少是 2 分,与比例无关。所以"相同内容绝不删了重写"这件事,任何负代价都能保证。

再看**比例层面**(20:1)决定的东西。结合 u1-l1 建立的背景——这个 crate 服务于 agent 的流式编辑(`edit_session`)与代码生成渲染(`buffer_codegen`),新文本是 LLM 逐块产出的"重写后的区域",旧文本是缓冲区里已有的内容——20:1 的倾斜有三个可分析的效果:

1. **改动很贵,于是算法拼命找公共子串。** 修改一个字符要付 −21,而每保住一个字符至少 +1。分数差驱使 DP 把任何一段"旧文本中仍然成立的内容"锚定为长 `Keep`,把真正的变化压缩成少数几个删/插点。行级折叠(u4 单元)后,这意味着更少的"整行删除 + 整行插入",流式渲染时行级 diff 更稳定、闪烁更少。
2. **插入便宜,匹配流式产出的形态。** 新文本的长度由模型输出决定,不是算法能控制的;如果重罚插入,DP 会倾向于把新字符"硬匹配"到旧文本里位置不对的相同字符上,产生错位的 `Keep`。`-1` 只是一个轻微的、主要用于平局裁决的成本。
3. **影响锚点选择。** 每次 `push_new` 结束时,算法在当前新列上扫描所有旧行,取**分数最大**的行作为新锚点([crates/streaming_diff/src/streaming_diff.rs:L184-L193](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L184-L193))。删除越贵,"靠删除硬推进到旧文本末尾"的路径分数越惨,锚点越倾向于停在"旧文本主要被 `Keep` 消费掉"的位置。举个可手算的例子:`old = "abcdef"`、目前收到的新文本是 `"abc"`,则 \( S(3,3) = 3 \)(全部 Keep),而 \( S(6,3) = 3 - 120 = -117 \)(把 `def` 全删掉),锚点毫无悬念落在行 3。把删除代价改成 −2 后 \( S(6,3) = 3 - 6 = -3 \),锚点不变,但**裕度**从 120 分缩到 6 分——在重复、歧义文本里,这种裕度差异就会真正改变选出的路径(详见 4.2.4 实践的推演)。

需要诚实说明:以上是"从数值效果反推的设计意图"。20 这个数为什么不是 50 或 5,源码没有注释,属于经验调参(待确认);能严格论证的是方向性结论,而不是具体倍数。

#### 4.2.2 核心流程

不对称代价参与决策的完整链路:

```text
常量 σ_ins = -1、σ_del = -20
      │
      ├─ 构造期:new() 把第 0 列填成 i × (-20)      ← 删除边界的"起跑线"
      │
      ├─ 填表期:每个单元格三选一取 max              ← 路径分数在此定型
      │
      ├─ 锚点期:push_new 末段按分数挑最大行          ← 分数决定锚点 old_text_ix
      │
      └─ 回溯期:backtrack 用 OrderedFloat 比较分数   ← 分数决定具体走哪条路
```

也就是:常量 → 单元格分数 → (锚点位置 + 回溯路径) → 最终的 `Keep`/`Insert`/`Delete` 序列。

#### 4.2.3 源码精读

- [crates/streaming_diff/src/streaming_diff.rs:L125-L126](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L125-L126):`INSERTION_SCORE: f64 = -1.` 与 `DELETION_SCORE: f64 = -20.`,两者都在 L166、L167、L136、L164 四处被消费。
- [crates/streaming_diff/src/streaming_diff.rs:L184-L193](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L184-L193):锚点搜索。`for i in self.old_text_ix..=self.old.len()` 里逐行取分数,严格大于当前最大值才更新(`>` 而非 `>=`,同分取**最小**的行)。这里读的每个分数都已被两个代价常量浸透。
- [crates/streaming_diff/src/streaming_diff.rs:L227-L233](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L227-L233):回溯阶段比较三个前驱单元格的分数,`OrderedFloat(self.scores.get(i, j - self.new_text_ix))` 让浮点数可以进 `max_by_key`。注意候选数组的顺序是 `[insertion, deletion, equality]`,而 `max_by_key` 在并列时返回**最后一个**最大元素——平局时对角(Keep)优先于删除、删除优先于插入。这是分数之外的第二个决策来源:并列时的固定偏好。

#### 4.2.4 代码实践

**(1)实践目标:** 把 `DELETION_SCORE` 从 -20 改成 -2,观察并记录 `Keep`/`Insert`/`Delete` 序列与分数的变化(规格任务的前半部分,综合实践会把它固化成断言测试)。

**(2)操作步骤:**

1. 在 4.1.4 搭好的 `scoring-lab` 副本里,先记录基线:对 `old = "aaaa\nbbbb"`、`new = "aaaa\nBBBB"` 跑一次完整 diff 并打印操作序列(驱动代码见第 5 节综合实践的 `diff_all`)。
2. 把副本中的 `DELETION_SCORE` 改为 `-2.`,重复第 1 步。
3. 在 `push_new` 结束前临时打印 `self.scores`(或锚点搜索循环里的每个 `score`),对比两种代价下最后一列的分数向量。

**(3)需要观察的现象:** 操作序列是否变化;最后一列(以及锚点所在行)的分数数值变化;`finish()` 阶段删除路径的分数变化。

**(4)预期结果(手推,待本地验证):** 对这个特定例子,**操作序列大概率不变**。理由:第二行 `bbbb` 与 `BBBB` 逐字符不等,这些字符的对齐是"被迫的"(只能删旧插新),没有竞争方案;我的手推结果是基线与改动后都得到 `[Keep{5}, Insert{"BBBB"}, Delete{4}]`。变化体现在分数地形上:删除 4 个 `b` 的总代价从 -80 变为 -8,`S(9,9)` 从 -77.4 变为 -5.4,锚点搜索中"越过未匹配旧文本"的裕度大幅缩小。想看到序列真正翻转,需要重复/歧义文本(见第 5 节的扩展步骤)。

#### 4.2.5 小练习与答案

**练习 1:** 保留一个相同字符与"删了再重插"同一字符,分数贡献分别是多少?差距多大?

**答案:** 保留至少 \( B(1) = 1.8^0 = +1 \)(游程越长越高,封顶约 +12144);"删了重插"是 \( -20 + (-1) = -21 \)。差距至少 22 分。更准确地说:只要两个代价都非正,这个结论就与具体比例无关;20:1 影响的是多种可行对齐之间的选择强度。

**练习 2:** 如果把 `DELETION_SCORE` 改成 `+2`(删除变成奖励),会发生什么?

**答案:** 第 0 列变成 \( S(i, 0) = 2i \),越删除分越高。路径会倾向于把旧文本整段删掉再重插(删 1 个 +2,配合插入 -1,净 +1,与 Keep 的 +1 并列甚至更优,"删了重插"不再吃亏);锚点搜索也会被推向旧行末端。diff 退化为大面积 `Delete` + `Insert`,`Keep` 几乎消失——这从反面说明了负代价的必要性。(行为细节待本地验证,方向性可以由边界公式直接推出。)

**练习 3:** 为什么插入代价只设 -1,而不是也设成 -20 那样的"重罚"?

**答案:** 插入是流式场景的常态:新文本由模型持续产出,长度不受算法控制。重罚插入会让 DP 试图"逃避插入",把新字符硬匹配到旧文本中错误的相同字符上,产生位置错位的 `Keep`,重建结果虽然仍正确(不变量不破),但差异的呈现位置混乱、行级折叠后闪烁更多。-1 的定位是"轻微成本 + 平局裁决",真正的强偏好由相等奖励(4.3 节)来提供。

### 4.3 相等奖励:EQUALITY_BASE = 1.8 与指数上限 16

#### 4.3.1 概念说明

相等分支的奖励不是一个常数,而是一条**阶梯式指数曲线**:

\[ B(r) = 1.8^{\min(\lfloor r/4 \rfloor,\, 16)}, \qquad r = \text{equal\_run} \]

两个参数各司其职:

- **`EQUALITY_BASE = 1.8`(底数):** 奖励随游程长度指数增长。游程每续 4 个字符,指数加 1,单字符奖励乘 1.8。
- **`MAX_EQUALITY_EXPONENT = 16`(指数上限):** 单字符奖励封顶在 \( 1.8^{16} \approx 12143.95 \)。

查表(数值为手算,保留 2 位小数):

| 游程长度 \( r \) | 指数 \( \lfloor r/4 \rfloor \) | 单字符奖励 \( B(r) \) |
| --- | --- | --- |
| 1–3 | 0 | 1.0 |
| 4–7 | 1 | 1.8 |
| 8–11 | 2 | 3.24 |
| 12–15 | 3 | 5.832 |
| 16–19 | 4 | 10.50 |
| 24–27 | 6 | 34.01 |
| 48–51 | 12 | 1156.83 |
| ≥ 64 | 16(封顶) | ≈ 12143.95 |

为什么用 `/4` 分段而不是逐字符线性加分?为了奖励**连续性**:同样数量的相等字符,聚成一条长游程比分散成多条短游程得分高得多。手算一个对比:连续 Keep 16 个字符,各步奖励依次是 \( 1,1,1,1.8\times4, 3.24\times4, 10.4976 \),合计约 **56.99**;而同样 16 个字符分散成若干段不超过 3 的短游程,每步只有 +1,合计 **16**。指数阶梯把"成块保留"和"零散保留"拉开了 3.5 倍以上的差距,diff 因此倾向于产出**少而长的 `Keep` 段**——这正是"锚定"所需要的形态。

为什么设指数上限?源码同样没有注释,但可以做数值分析(推演,待确认):`f64` 的最大值约 \( 1.8 \times 10^{308} \),而

\[ 1.8^{x} \ge 10^{308} \iff x \ge \frac{308}{\log_{10} 1.8} \approx \frac{308}{0.2553} \approx 1206 \]

也就是说,只要有一条约 1200+ 字符的连续相等游程(大段未改动文本,重写场景里很常见),无上限的 \( 1.8^r \) 就会溢出为 `inf`,后续 `inf - inf` 产生 `NaN`,浮点 `max` 与 `OrderedFloat` 比较全部失效。封顶之后,单步奖励 ≤ 约 \( 1.21 \times 10^4 \),一条 \( 10^6 \) 字符的游程总分约 \( 1.21 \times 10^{10} \),远离 `f64` 的量级极限;同时超过 64 字符后奖励回到线性增长,超长匹配之间也不会互相淹没。

还有一个隐含设计:奖励是**加在前驱总分之上**的(`previous_scores[i - 1] + B(r)`),所以一条游程的累计奖励是各阶梯的累加,呈"分段线性、整体超线性"的增长——在到达封顶之前,游程越长,继续续上它的吸引力越大。

#### 4.3.2 核心流程

相等奖励的计算只有三行,但挂在游程记账之上:

```text
若 old[i-1] == new[j-1]:
    r := previous_equal_runs[i-1] + 1        # 对角前驱的游程 + 1
    current_equal_runs[i] := r               # 记下本格的游程
    指数 := min(r / 4, 16)                   # 整数除法 + 封顶
    相等候选 := S(i-1, j-1) + 1.8^指数
否则:
    相等候选 := -∞                           # 本格游程保持为 0
```

\( r \) 的维护机制(双缓冲)是 4.4 节的主题。

#### 4.3.3 源码精读

- [crates/streaming_diff/src/streaming_diff.rs:L127-L128](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L127-L128):`EQUALITY_BASE: f64 = 1.8` 与 `MAX_EQUALITY_EXPONENT: i32 = 16`。注意上限的类型是 `i32`,正好匹配 `f64::powi` 的参数类型,省去一次转换。
- [crates/streaming_diff/src/streaming_diff.rs:L172-L173](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L172-L173):`let exponent = cmp::min(equal_run as i32 / 4, Self::MAX_EQUALITY_EXPONENT);` 然后 `previous_scores[i - 1] + Self::EQUALITY_BASE.powi(exponent)`。`equal_run` 是 `u32`,`as i32 / 4` 是整数除法——这就是"每 4 个字符一个阶梯"的出处。整条公式与 \( B(r) \) 一一对应。

#### 4.3.4 代码实践

**(1)实践目标:** 把 `MAX_EQUALITY_EXPONENT` 从 16 改为 2,验证"上限何时才参与决策"。

**(2)操作步骤:**

1. 在 `scoring-lab` 副本里,先不改任何东西,对 `old = "aaaa\nbbbb"`、`new = "aaaa\nBBBB"` 记录基线序列与 `Matrix` 打印。
2. 把 `MAX_EQUALITY_EXPONENT` 改为 `2`,重复。
3. 把例子换成一长段公共前缀再试:例如 `old = <80 个 'a' + "\nbbbb">`、`new = <80 个 'a' + "\nBBBB">`(让最长游程达到 81,远超 64 的封顶线),再对比改动前后的分数与序列。

**(3)需要观察的现象:** 三种配置下操作序列是否相同;`Matrix` 中对角线上的分数增长速度;最长游程超过 64 后,封顶与否的单步奖励差异。

**(4)预期结果(手推,待本地验证):** 对第 1、2 步的小例子,**预期序列不变**:该例最长游程是 5(`"aaaa\n"`),指数最高为 \( \lfloor 5/4 \rfloor = 1 \),上限从 16 降到 2 根本不触发——这个小例子本身"看不见"这个常量。第 3 步的长前缀例子中,基线的对角线奖励在游程超过 64 后稳定在约 12144/字符,而改动后被压在 \( 1.8^2 = 3.24 \),分数曲线明显变平;但"Keep 长前缀"依然远胜"删了重插"(\(+3.24\) 对 \(-21\)),所以操作序列预期仍然不变,变化的只是分数量级与竞争裕度。结论:上限主要是一个**数值安全阀**,在正常输入下很少改变决策。

#### 4.3.5 小练习与答案

**练习 1:** 计算 \( B(10) \) 和 \( B(70) \)。

**答案:** \( B(10) = 1.8^{\lfloor 10/4 \rfloor} = 1.8^2 = 3.24 \);\( B(70) = 1.8^{\min(\lfloor 70/4 \rfloor, 16)} = 1.8^{16} \approx 12143.95 \)(\( \lfloor 70/4 \rfloor = 17 \) 已被上限截断为 16)。

**练习 2:** 如果去掉 `MAX_EQUALITY_EXPONENT`(指数直接用 \( \lfloor r/4 \rfloor \)),一条 2000 字符的连续相等游程会发生什么?

**答案:** 指数为 500,\( 1.8^{500} \approx 10^{127.6} \) 仍在 `f64` 范围内但已极大;真正出问题要从游程约 1206 起(\( 1.8^{301.5} \) 附近越过 \( 10^{308} \)),`powi` 返回 `inf`,此后 `inf` 参与加法、`max`、以及与其他 `inf` 的比较,`inf - inf` 型路径比较会产生 `NaN`,`OrderedFloat` 的排序失效,回溯结果不可预测。所以上限是把奖励增长限制在安全量级的安全阀。

**练习 3:** 为什么说 `/4` 分段奖励的是"连续性"而不是"数量"?

**答案:** 因为奖励按游程当前长度查阶梯:16 个字符聚成一条连续游程合计约 56.99 分,而同样 16 个字符分散成 ≤3 的短游程只有 16 分。数量相同、连续性不同,得分差 3.5 倍以上——DP 因此宁可放弃零散的小匹配,也要保住长段的 `Keep`,产出的编辑脚本更紧凑、hunk 更少。

### 4.4 equal_run 的计算:双缓冲游程数组

#### 4.4.1 概念说明

指数奖励依赖一个关键状态:当前对角匹配已经**连续**了多少个字符。这就是 `equal_run`。它由两个字段承担:

```rust
previous_equal_runs: Vec<u32>,
current_equal_runs: Vec<u32>,
```

见 [crates/streaming_diff/src/streaming_diff.rs:L120-L121](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L120-L121)。两个数组长度都是 `old_len + 1`(每个旧行一个槽位),与打分矩阵的列一一对应:`previous_equal_runs[i]` 记录"上一列第 `i` 行"的游程,`current_equal_runs[i]` 记录"当前列第 `i` 行"的游程。

之所以要两个数组,和 u2-l1 讲过的矩阵滚动是同一个原因:填当前列时既要读上一列的游程、又要写当前列的游程,读完一列后两个身份互换,周而复始。**双缓冲**让游程状态可以像分数一样只保留"最近两列",内存不随新文本增长。

有一个值得注意的语义细节(源码事实):游程的写入只看"这一格的字符是否相等",**不看这一格的最高分是否真来自对角步**。也就是说,`current_equal_runs[i]` 记录的是"沿着对角线方向、字符相容性意义上的连续长度",而不是"最优路径实际走过的连续 Keep 长度"。这使 \( B(r) \) 成为一个启发式奖励:极端情况下,一条路径经由插入步到达前驱单元格后仍能续上对角游程的分数。这不妨碍正确性(不变量只要求操作序列可重建新文本,见 u3-l4),只是说明打分模型本身就是近似启发,而非严格的最优对齐目标。

#### 4.4.2 核心流程

游程的维护嵌在列循环里,与填表同步:

```text
初始化:new() 里两个数组都填 0(vec![0; old_len + 1])
每个新字符 j(即每一列):
    current_equal_runs 全部清零            ← 不相等格的游程必须是 0
    对每个旧行 i:
        若 old[i-1] == new[j-1]:
            r := previous_equal_runs[i-1] + 1     # 读对角前驱
            current_equal_runs[i] := r            # 写当前格
            用 B(r) 计算相等候选分数
        否则:
            current_equal_runs[i] 保持 0,相等候选 = -∞
    列填完后:swap(previous_equal_runs, current_equal_runs)
             # 刚写完的列变成下一列的"上一列"
```

游程的重置条件有两个:某格字符不相等(该格保持 `fill(0)` 留下的 0),或回退到锚点边界(锚点之后的列重新从零开始累计,因为 `new_text_ix` 推进后旧列数据不再参与)。

#### 4.4.3 源码精读

- [crates/streaming_diff/src/streaming_diff.rs:L144-L145](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L144-L145):构造函数里 `previous_equal_runs: vec![0; old_len + 1]`、`current_equal_runs: vec![0; old_len + 1]`——初始游程全零。
- [crates/streaming_diff/src/streaming_diff.rs:L156](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L156):每个新字符(每列)开始前 `self.current_equal_runs.fill(0)`。这一步不可省略:清零保证不相等格的游程是 0,否则上一列的旧游程会顺着数组槽位"漏"到当前列,奖励被错误地延续。
- [crates/streaming_diff/src/streaming_diff.rs:L169-L170](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L169-L170):`let equal_run = previous_equal_runs[i - 1] + 1;` 再 `current_equal_runs[i] = equal_run;`——读对角前驱(上一列、上一行)的游程加一,写入当前格。游程沿对角线传递。
- [crates/streaming_diff/src/streaming_diff.rs:L181](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L181):`std::mem::swap(&mut self.previous_equal_runs, &mut self.current_equal_runs);`——一列填完,双缓冲换位,零拷贝地完成"当前变上一"。

#### 4.4.4 代码实践

**(1)实践目标:** 亲眼追踪游程如何一列列累计、在哪里归零。

**(2)操作步骤:**

1. 在 `scoring-lab` 副本的相等分支里(L168-L176 位置)临时加一行:

```rust
eprintln!("j={} i={} old={:?} new={:?} run={} exp={} bonus={}",
    j, i, old[i - 1], new_char, equal_run, exponent,
    Self::EQUALITY_BASE.powi(exponent));
```

2. 用 `StreamingDiff::new("aaaa\nbbbb".to_string())` 构造,一次性 `push_new("aaaa\nBBBB")`,观察 stderr 输出。
3. 再改成两次推送 `push_new("aaaa")`、`push_new("\nBBBB")`,对比游程在块边界处的行为。

**(3)需要观察的现象:** 每列中被相等的字符触发的日志;游程数值的递增序列;遇到 `b` 对 `B` 后是否再无相等日志;分块推送时游程是否跨块延续。

**(4)预期结果(手推,待本地验证):** 一次性推送时,前 5 列(新字符 `a,a,a,a,\n`)与旧行 1..=5 依次相等,游程依次为 1、2、3、4、5,指数依次为 0、0、0、1、1,奖励依次为 1.0、1.0、1.0、1.8、1.8;第 6 列起(`B` 对 `b`)不再有任何相等日志——游程在 5 处停止累计。这些 `Keep` 的奖励合计 \( 1+1+1+1.8+1.8 = 6.6 \)。分块推送时,`"\n"` 属于第二块,但它的游程应仍是 5(游程按对角连续性累计,与推送边界无关)——这一点值得重点确认,它是"分块方式不影响打分语义"的具体体现。

#### 4.4.5 小练习与答案

**练习 1:** `old = "abab"`、`new = "abab"`(全同,沿对角线一路匹配),写出每一步的 `equal_run`、指数与奖励。

**答案:** 四步的游程依次 1、2、3、4;指数依次 \( \lfloor 1/4 \rfloor = 0 \)、0、0、\( \lfloor 4/4 \rfloor = 1 \);奖励依次 1.0、1.0、1.0、1.8,合计 4.8。

**练习 2:** 为什么每列开始前必须 `current_equal_runs.fill(0)`?去掉会发生什么?

**答案:** 游程必须表达"连续相等"。若不清零,上一列遗留的游程值会留在数组槽位里,当前列中那些字符不相等、或虽相等但前驱不相等的格子,可能读到被污染的前驱游程而算出偏大的奖励;更糟的是经由"插入/删除步"到达的格子也会携带旧游程,奖励与路径脱钩。`fill(0)` 配合 L181 的 `swap` 构成标准的双缓冲:写前清零,写完换位。

**练习 3:** `previous_equal_runs[i - 1]` 什么时候是 0?

**答案:** 三种情况:(1)对角前驱格 `(i-1, j-1)` 处字符不相等——该格在它所在列被 `fill(0)` 后没有写入;(2)位于锚点边界(`j-1` 等于当前 `new_text_ix`,锚点之后的列从零开始累计);(3)构造之初(L144-L145 全零初始化)。此时 `equal_run` 计算结果为 1,奖励回落到 \( 1.8^0 = 1 \)。

## 5. 综合实践

把规格里的实验任务完整落地:**复制 `StreamingDiff` 到本地练习文件,分别修改 `DELETION_SCORE`(从 -20 到 -2)与 `MAX_EQUALITY_EXPONENT`(从 16 到 2),对固定的一对文本运行 diff,把观察到的 `Keep`/`Insert`/`Delete` 序列变化写成两个断言测试。**

### 5.1 搭建练习环境

1. `cargo new scoring-lab && cd scoring-lab`,在 `Cargo.toml` 的 `[dependencies]` 里加 `ordered-float = "4"`(版本以本地编译通过为准,待确认;zed 工作区内由 workspace 统一管理,练习 crate 需要自行指定)。
2. 从 [crates/streaming_diff/src/streaming_diff.rs:L10-L104](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L10-L104) 复制 `Matrix`,从 [L106-L111](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L106-L111) 复制 `CharOperation`,从 [L113-L279](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L113-L279) 复制 `StreamingDiff`。不要复制 `LineDiff`/`LineOperation` 及 `is_line_start` 等函数,并删除顶部的 `use rope::...` 与 `use std::collections::BTreeSet`,这样练习 crate 只依赖 `ordered-float`。
3. 在副本的 `CharOperation` 上补一个 `#[derive(PartialEq)]`。原 crate 故意没有派生它——它的测试从不直接比较字符操作序列,只比较"应用之后"的文本([crates/streaming_diff/src/streaming_diff.rs:L1104-L1124](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L1104-L1124) 的 `apply_char_operations` 就是那个校验器);练习里我们要断言序列本身,所以在自己的副本里补上是合理的。
4. 一并复制 `apply_char_operations`(L1104-L1124),再写两个辅助函数(示例代码,非项目原有代码):

```rust
fn diff_all(old: &str, new: &str) -> Vec<CharOperation> {
    // 仿照 tests::random_streaming_diff(L963-L981)的"推送 + finish"模式
    let mut diff = StreamingDiff::new(old.to_string());
    let mut ops = diff.push_new(new);
    ops.extend(diff.finish());
    ops
}

fn show(old: &str, new: &str) {
    let ops = diff_all(old, new);
    println!("{:?}", ops);
    assert_eq!(apply_char_operations(old, &ops), new);
}
```

### 5.2 两个断言测试

基线测试(常量未改动时):

```rust
#[test]
fn baseline_replace_second_line() {
    let ops = diff_all("aaaa\nbbbb", "aaaa\nBBBB");
    assert_eq!(apply_char_operations("aaaa\nbbbb", &ops), "aaaa\nBBBB");
    assert_eq!(ops, vec![
        CharOperation::Keep { bytes: 5 },
        CharOperation::Insert { text: "BBBB".into() },
        CharOperation::Delete { bytes: 4 },
    ]);
}
```

实验 A 测试(`DELETION_SCORE = -2.`)与实验 B 测试(`MAX_EQUALITY_EXPONENT = 2`):各自只改一个常量,重复同样的断言结构,把 `assert_eq!` 的期望值换成你**实际观察到**的序列。

### 5.3 预期现象与结果

1. **基线(手推,待本地验证):** 序列为 `[Keep{5}, Insert{"BBBB"}, Delete{4}]`。推导要点:前 5 个字符沿对角线匹配(游程 1..5,奖励合计 6.6);`b` 与 `B` 不等,第二行只能删旧插新;锚点取 `S(5,9) = 6.6 - 4 = 2.6`(该列最大值),所以 `push_new` 先产出 `Keep` 与 `Insert`,`finish()` 再补上 `Delete`。注意 `Insert` 排在 `Delete` 之前,这是回溯方向(从终点往回走)决定的。
2. **实验 A(待本地验证):** 我的手推结论是**序列不变**——这个例子的对齐没有竞争方案,而"删了重插"在任何负代价下都输给 `Keep`(见 4.2)。变化的是分数:`S(9,9)` 从 -77.4 升到 -5.4,锚点选择"越过未匹配旧文本"的裕度从 120 分级别缩到几分。若你观察到序列确实不变,请把它写成断言,并在注释里记下"该常量在此例中只影响分数裕度";这与"常量塑造偏好强度,而非改写唯一解"的理解一致。
3. **实验 B(待本地验证):** 同样预期**序列不变**——最长游程 5,指数最高 1,上限 16 改 2 不触发(见 4.3.4)。
4. **不变量断言(三种配置都必须成立):** `apply_char_operations(old, &ops) == new`。原因:回溯只会沿合法的 DP 移动构造路径,`Keep`/`Delete` 按序消费旧文本、`Insert` 的文本逐字取自新文本,所以**改变常量只是改变选哪条路径,不会破坏重建**;除非把常量改到产生 `inf`/`NaN`(例如去掉指数上限再喂超长相同游程),浮点比较才会失效。这个不变量的完整论证是 u3-l4 的主题。
5. **扩展(选做,待本地验证):** 想看到常量真正改变序列,需要"歧义文本"。用 `old = "aaaa"`、`new = "aaaaa"` 试试插入位置落在游程前还是后;再用 `old = "aaaa\naaaa"`、`new = "aaaa\nAAAA"` 对比两种 `DELETION_SCORE` 下的输出;记录每组的分数打印(`{:?}` 打 `Matrix` 即可,L93-L104 已实现 `Debug`)。

## 6. 本讲小结

- 打分模型的全部输入是四个常量:`INSERTION_SCORE = -1`、`DELETION_SCORE = -20`、`EQUALITY_BASE = 1.8`、`MAX_EQUALITY_EXPONENT = 16`([L124-L128](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L124-L128)),数值本身无注释说明,是经验值,方向性作用可以严格分析。
- 递推式 \( S(i,j)=\max(S(i,j-1)-1,\ S(i-1,j)-20,\ S(i-1,j-1)+B(r)) \),边界 \( S(i,0)=-20i \)、\( S(0,j)=-j \);错配的对角候选取 `NEG_INFINITY` 表示禁止走法。
- 符号层面:负代价保证"保留相同字符(≥ +1)"永远胜过"删了重插(= −21)";20:1 的比例决定的是竞争对齐间的偏好强度与锚点([L184-L193](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L184-L193))的落点,使 diff 呈"保留旧文本 + 插入新内容"的形态,契合 LLM 流式编辑。
- 相等奖励按 \( B(r)=1.8^{\min(\lfloor r/4\rfloor,16)} \) 阶梯式指数增长:每续 4 个字符单字符奖励乘 1.8,封顶约 12144;奖励连续性(16 连续 ≈ 57 分对 16 分散 = 16 分),上限是防 `f64` 溢出的安全阀。
- `equal_run` 由 `previous_equal_runs`/`current_equal_runs` 双缓冲维护:每列 `fill(0)` 清零、对角前驱加一、列尾 `swap` 换位;它记录的是对角相容性意义上的连续长度,与最优路径的实际走法无关,因此 \( B(r) \) 是启发式奖励。
- 改常量的实验结论(待本地验证):在无歧义的小例子上序列稳定、只有分数地形变化;重建不变量 `apply(old, ops) == new` 对常量改动鲁棒。

## 7. 下一步学习建议

下一讲是 **u3-l1《构造函数与 DP 初始化:从全局对齐算法说起》**,把本讲的递推式放回教科书式的 Needleman-Wunsch 框架里,精读 `StreamingDiff::new` 如何转 `Vec<char>`、建 `(old_len+1) × 1` 矩阵、初始化第 0 列。建议:

1. 带着本讲的两个问题去读 u3-l1:为什么用浮点分数而不是整数?为什么初始化只建 1 列?
2. 之后 **u3-l2** 会拆开本讲刻意绕开的矩阵滚动机制(`swap_columns` + `resize` + `adjacent_columns_mut` 如何配合 `fill(0)`/`swap` 的游程双缓冲)。
3. 在进入 u3 之前,可以先跑一次 `cargo test -p streaming_diff`(在仓库根目录),再试试 `ITERATIONS=1000 SEED=7 cargo test -p streaming_diff test_random_diffs`,直观感受"任意分块下操作序列都能重建新文本"这条不变量——它正是本讲所有打分偏好的最终裁判。

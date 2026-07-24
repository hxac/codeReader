# 结果收集与 mul_outcome 输出索引

## 1. 本讲目标

上一讲（u2-l2）我们弄懂了 `systolic.v` 里每个 cell 如何在 `cycle_num` 的调度下做乘加（MAC），并把累加结果存进二维寄存器 `matrix_mul_2D[i][j]`。本讲要解决的是「算完之后怎么把结果取出来」这个问题。

学完本讲，你应该能够：

1. 说清楚 `mul_outcome` 这条 168bit 输出总线是如何被切成 8 段、每段 21bit 的。
2. 给定一个 `matrix_index`，手算出它对应的 `upper_bound` 与 `lower_bound`，并解释为什么 `matrix_index=0` 和 `matrix_index=8` 会产生完全相同的边界。
3. 看懂最后那个 `always@(*)` 块里的两段 `for` 循环如何按「反对角线」从 8×8 结果矩阵里挑出正好 8 个有效结果，拼成一次 `mul_outcome` 输出。
4. 理解「互补反对角线配对」这一设计巧思：为什么任意一对反对角线 `s` 与 `s+8` 加起来恰好等于 `ARRAY_SIZE`（8）个 cell。

本讲只讲 `systolic.v` 内部「结果收集」这一段纯组合逻辑；至于 `matrix_index` 是由谁、在什么时机推进的，属于控制器（u3-l1）的范畴，本讲只在需要时点出二者如何对齐。

## 2. 前置知识

在进入源码前，先用一张图建立直觉。8×8 脉动阵列里每个 cell `(i, j)` 最终累加出一个完整的输出元素，存在 `matrix_mul_2D[i][j]` 里。如果我们把「行号 + 列号」相等的那些 cell 连起来，就得到一条条**反对角线（anti-diagonal）**：所有满足 `i + j == s` 的 cell 属于同一条反对角线 `s`。

对于一个 8×8 矩阵，反对角线编号 `s` 的取值范围是 `0 ~ 14`，每条反对角线上的 cell 个数并不相等：

```
反对角线 s:        0  1  2  3  4  5  6  7  8  9 10 11 12 13 14
cell 个数 c(s):    1  2  3  4  5  6  7  8  7  6  5  4  3  2  1
```

可以看到：靠两端的反对角线 cell 很少（只有 1 个），中间那条 `s=7` 最满（8 个）。把 64 个 cell 全加起来：`1+2+...+8+...+2+1 = 64`，正好是 8×8。

这就带来了一个输出难题：`mul_outcome` 一次只能吐出 **8 个**结果（8 段 × 21bit），可单条反对角线 cell 数从 1 到 8 不等。如果每拍只读一条反对角线，输出总线的利用率会忽高忽低，大多数拍都浪费。

本讲的 gather 逻辑给出的精妙解法是：**把互补的两条反对角线 `s` 与 `s+8` 配成一对一起读**。观察上表，`c(s) + c(s+8)` 恒等于 8（例如 `c(0)+c(8)=1+7=8`，`c(3)+c(11)=4+4=8`，`c(7)+c(15)=8+0=8`）。于是每个 `matrix_index` 总能正好凑齐 8 个 cell，把 `mul_outcome` 填得满满当当。理解了这一点，后面的源码就只是「如何机械地实现这个配对」了。

> 术语提示：
> - **反对角线（anti-diagonal）**：矩阵中所有满足 `行号 + 列号 == 常数` 的元素集合。
> - **互补反对角线对**：`s` 与 `s+ARRAY_SIZE`，二者 cell 数之和恒为 `ARRAY_SIZE`。
> - **`+:` 变基部分选择**：Verilog 写法 `bus[base +: width]`，表示从第 `base` 位开始、向上取 `width` 位，等价于 `bus[base+width-1 : base]`，常用于基地址是变量的切片。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `rtl/systolic.v` | 8×8 脉动阵列本体 | 第 120–151 行的第三个 `always@(*)` 块：结果收集 |
| `rtl/tpu_top.v` | 顶层结构化连线 | 第 113–117 行：`mul_outcome` 如何接到 `ori_data` 上送给 `quantize` |
| `rtl/quantize.v` | 饱和量化 | 第 24 行：把 `ori_data` 按 21bit 切回来，反向印证 `mul_outcome` 的打包方式 |
| `rtl/systolic_controll.v` | 主控状态机 | 第 157–176 行：`matrix_index` 何时开始递增、与 `cycle_num` 的对齐关系（仅作背景） |

## 4. 核心概念与源码讲解

### 4.1 mul_outcome 的打包结构与清零初始化

#### 4.1.1 概念说明

`systolic` 模块对外只输出一个信号 `mul_outcome`，它是把「这一拍要送出去的 8 个 21bit 结果」首尾拼接成的一条宽总线。可以这样理解它的结构：

```
mul_outcome[167:0]
├── 段0 [20:0]      ← matrix_mul_2D 中某个 cell 的 21bit 结果
├── 段1 [41:21]
├── 段2 [62:42]
├── ...
└── 段7 [167:147]
```

每段宽度由 `OUTCOME_WIDTH` 决定，段数由 `ARRAY_SIZE` 决定。这条总线随后原样送给 `quantize` 模块（在顶层里它叫 `ori_data`），由 `quantize` 再逐段切回来做饱和量化。所以 gather 的职责非常单一：**从 8×8 的 `matrix_mul_2D` 里挑出 8 个正确的 cell，按固定顺序拼进 `mul_outcome`。**

#### 4.1.2 核心流程

1. 进入组合 `always@(*)` 块后，**先把整条 168bit 总线清零**。
2. 清零是必须的：这是一个纯组合块驱动一个 `reg`（`mul_outcome` 被声明为 `output reg`），如果某些段在某个 `matrix_index` 下不被赋值，又不先给默认值，综合工具会推断出**锁存器（latch）**，这是 RTL 设计的大忌。
3. 清零之后再由两段 `for` 循环去覆盖需要的那 8 段。
4. 整条 `mul_outcome` 共 `ARRAY_SIZE * OUTCOME_WIDTH = 8 * 21 = 168` bit。

位宽推导：

\[
\text{mul\_outcome 宽度} = \text{ARRAY\_SIZE} \times \text{OUTCOME\_WIDTH} = 8 \times 21 = 168
\]

其中 `OUTCOME_WIDTH = DATA_WIDTH + DATA_WIDTH + 5 = 8 + 8 + 5 = 21`（两个 8bit 有符号数相乘得 16bit，再加 5bit 累加保护位），与 u1-l4 讲过的 `ORI_WIDTH` 完全一致。

#### 4.1.3 源码精读

`mul_outcome` 的端口声明与关键 `localparam`：

[rtl/systolic.v:23](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L23) —— `output reg signed [(ARRAY_SIZE*(DATA_WIDTH+DATA_WIDTH+5))-1:0] mul_outcome`，可见总宽就是 `ARRAY_SIZE*(DATA_WIDTH+DATA_WIDTH+5)`，且带 `signed`。

[rtl/systolic.v:28](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L28) —— `localparam OUTCOME_WIDTH = DATA_WIDTH+DATA_WIDTH+5;`，定义每段宽度。

清零初始化循环：

[rtl/systolic.v:133-134](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L133-L134) —— `for(i=0; i<ARRAY_SIZE*OUTCOME_WIDTH; i=i+1) mul_outcome[i] = 0;`，逐位清零，既给出默认值又避免锁存器。

顶层把这 168bit 原封不动接到量化器：

[rtl/tpu_top.v:113-117](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L113-L117) —— `.matrix_index(matrix_index)` 与 `.mul_outcome(ori_data)`，即 `systolic` 的 `mul_outcome` 就是顶层的 `ori_data`（`wire signed [ARRAY_SIZE*ORI_WIDTH-1:0]`，168bit）。

而 `quantize` 又用同样的切法把它切回来，反向印证了打包结构：

[rtl/quantize.v:24](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/quantize.v#L24) —— `ori_shifted_data = ori_data[i*ORI_WIDTH +: ORI_WIDTH];`，第 `i` 段正是 `mul_outcome` 的第 `i` 段。发送端和接收端用同一套 `i*WIDTH +: WIDTH` 切片约定，保证语义对齐。

> 一个关于符号的细节：`mul_outcome[...]` 这种变基部分选择在 Verilog 里技术上返回的是无符号量，但赋值时 21bit 的二进制补码位模式被原样拷贝，`quantize` 端把 `ori_shifted_data` 声明为 `signed` 再重新解释，因此负数的符号位是靠「位置」保留下来的，没有丢失。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `mul_outcome` 的总宽与分段位域。

**操作步骤**：

1. 打开 `rtl/systolic.v` 第 4–8 行与第 23、28 行，记下 `ARRAY_SIZE=8`、`DATA_WIDTH=8`、`OUTCOME_WIDTH=21`。
2. 计算 `mul_outcome` 总宽，并写出段 0 ~ 段 7 各自的位域范围。
3. 打开 `rtl/quantize.v` 第 10 行（`ori_data` 的位宽声明）与第 24 行（切片），核对两端位宽与切法是否一致。

**需要观察的现象 / 预期结果**：

- 总宽应为 `168` bit。
- 段 `k` 占据位域 `[k*21 + 21 - 1 : k*21]`，例如段 0 = `[20:0]`，段 7 = `[167:147]`。
- `quantize` 端 `ARRAY_SIZE*(DATA_WIDTH+DATA_WIDTH+5)-1:0` 同样是 `167:0`，切片步长同样是 21，二者匹配。

**运行结果**：本实践为源码阅读型，无需运行仿真；上述位域可由参数直接推得。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `ARRAY_SIZE` 改成 4（假设其余硬编码也跟着改），`mul_outcome` 总宽变成多少？段数和每段宽度各是多少？

**答案**：段数 = `ARRAY_SIZE = 4`，每段仍是 `OUTCOME_WIDTH = 8+8+5 = 21` bit（`OUTCOME_WIDTH` 只依赖 `DATA_WIDTH`，与 `ARRAY_SIZE` 无关），所以总宽 = `4 * 21 = 84` bit。

**练习 2**：为什么 gather 块开头那个把 `mul_outcome` 逐位清零的循环不能省？

**答案**：因为 `mul_outcome` 是 `output reg`，且 gather 是纯组合 `always@(*)` 块。如果某个 `matrix_index` 下某些段没有被两段 `for` 循环赋值，又没有默认值，综合器会为这些位推断出锁存器，导致时序与功能都出错。清零循环既提供了默认值，又消除了锁存器。

---

### 4.2 matrix_index 到 upper_bound / lower_bound 的折叠映射

#### 4.2.1 概念说明

`matrix_index` 是一个 6bit 输入（取值 `0 ~ 63`，实际由控制器驱动，正常工作范围为 `0 ~ 15`），它告诉 gather「现在要取哪一批结果」。gather 并不直接用 `matrix_index` 去索引 cell，而是先把它折算成两个边界：

- `upper_bound`：用于在矩阵**左上三角**（含主对角线）里挑反对角线。
- `lower_bound`：用于在矩阵**右下三角**里挑反对角线。

折算规则非常简单，但藏着一个关键性质：**`matrix_index` 对 `ARRAY_SIZE` 取模后才决定边界**，因此 `matrix_index = k` 与 `matrix_index = k + ARRAY_SIZE` 会产生完全相同的 `(upper_bound, lower_bound)`。

#### 4.2.2 核心流程

```
若 matrix_index < ARRAY_SIZE (=8):
    upper_bound = matrix_index
    lower_bound = matrix_index + ARRAY_SIZE
否则:
    upper_bound = matrix_index - ARRAY_SIZE
    lower_bound = matrix_index
```

把 `matrix_index` 记为 `m`，`ARRAY_SIZE` 记为 `N`。无论走哪个分支，结果都等价于：

\[
\text{upper\_bound} = m \bmod N, \qquad \text{lower\_bound} = (m \bmod N) + N
\]

也就是说 `upper_bound ∈ {0..N-1}`，`lower_bound = upper_bound + N ∈ {N..2N-1}`。对本项目 `N=8`：

| matrix_index | 分支 | upper_bound | lower_bound |
| --- | --- | --- | --- |
| 0 | < 8 | 0 | 8 |
| 1 | < 8 | 1 | 9 |
| ... | ... | ... | ... |
| 7 | < 8 | 7 | 15 |
| 8 | ≥ 8 | 0 | 8 |
| 9 | ≥ 8 | 1 | 9 |
| ... | ... | ... | ... |
| 15 | ≥ 8 | 7 | 15 |

可以清楚看到：**`matrix_index` 0~7 与 8~15 是一一对应、完全相同的边界对**。这正是控制器能区分两批（`data_set`）输出却复用同一套 gather 逻辑的基础——同一组边界在流水线稳态下会被读两次，分别对应先后流入阵列的两批数据。具体的时序交错由控制器（u3-l1）负责，gather 本身只认 `matrix_index`，不关心 `data_set`。

#### 4.2.3 源码精读

边界寄存器声明：

[rtl/systolic.v:37-38](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L37-L38) —— `reg [5:0] upper_bound; reg [5:0] lower_bound;`，都是 6bit，足以容纳 `0 ~ 15`。

折叠映射本体：

[rtl/systolic.v:122-129](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L122-L129) —— 即上面的 `if / else` 折算逻辑。

控制器侧（背景，仅说明 `matrix_index` 何时开始走）：

[rtl/systolic_controll.v:157-176](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic_controll.v#L157-L176) —— 在 `ROLLING` 状态里，只有当 `cycle_num >= ARRAY_SIZE+1`（即 `>= 9 = FIRST_OUT`，首批结果已算完）之后，`matrix_index` 才开始每拍 `+1`，同时拉高 `sram_write_enable`。这保证了「gather 读到的 cell 一定已经累加完成」，且「读出与写回 SRAM 是同拍协调的」。

#### 4.2.4 代码实践

**实践目标**：亲手验证折叠映射，体会「模 8 复用」。

**操作步骤**：

1. 对照 [rtl/systolic.v:122-129](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L122-L129) 的 `if/else`，对 `matrix_index = 0, 3, 7, 8, 11, 15` 六个取值，分别套用规则算出 `(upper_bound, lower_bound)`。
2. 把结果填进一张表，观察哪些 `matrix_index` 共享同一对边界。

**需要观察的现象 / 预期结果**：

| matrix_index | upper_bound | lower_bound |
| --- | --- | --- |
| 0 | 0 | 8 |
| 3 | 3 | 11 |
| 7 | 7 | 15 |
| 8 | 0 | 8 |
| 11 | 3 | 11 |
| 15 | 7 | 15 |

可见 `0↔8`、`3↔11`、`7↔15` 边界完全相同，印证 `matrix_index` 以 `ARRAY_SIZE=8` 为周期复用边界。

**运行结果**：源码阅读型实践，结果可由规则直接推得，无需仿真。

#### 4.2.5 小练习与答案

**练习 1**：`upper_bound` 与 `lower_bound` 为什么总是相差恰好 `ARRAY_SIZE`？

**答案**：由折算公式，`lower_bound = upper_bound + ARRAY_SIZE` 恒成立（两个分支都满足）。这个差值正是「互补反对角线对」中两条反对角线的编号差，保证它们一上一下、cell 数互补。

**练习 2**：若 `matrix_index` 由于某种原因被驱动到 16（超出正常范围 0~15），按本块逻辑会发生什么？

**答案**：走 `else` 分支：`upper_bound = 16 - 8 = 8`，`lower_bound = 16`。`upper_bound=8` 在左上三角里找不到 `i+j==8` 且 `j<=7-i` 的 cell（左上三角 `i+j` 最大为 7），`lower_bound=16` 更是超出矩阵最大反对角线 14。于是两段循环都挑不到 cell，`mul_outcome` 保持全 0。正常工作时控制器不会让 `matrix_index` 超过 15，所以这是「安全落空」。

---

### 4.3 两段 for 循环反对角线收集与互补配对

#### 4.3.1 概念说明

有了 `upper_bound` 和 `lower_bound`，接下来就是把 8×8 的 `matrix_mul_2D` 里、位于这两条反对角线上的 cell 挑出来拼进 `mul_outcome`。代码用两段 `for` 循环分工：

- **第一段循环（左上三角）**：扫描 `j <= ARRAY_SIZE-1-i` 的区域，挑出 `i+j == upper_bound` 的 cell，写入段 `i`。
- **第二段循环（右下三角）**：扫描 `j >= ARRAY_SIZE-i` 的区域，挑出 `i+j == lower_bound` 的 cell，也写入段 `i`。

两段循环都把结果写到 `mul_outcome[i*OUTCOME_WIDTH +: OUTCOME_WIDTH]`，即「**段号 = 行号 i**」。由于 `upper_bound ≤ 7` 对应的行较小、`lower_bound ≥ 8` 对应的行较大，两段循环天然写入不同的段，互不冲突，合起来正好填满 8 段。

#### 4.3.2 核心流程

先看每条反对角线上有多少 cell。对一个 `N×N` 矩阵（`N=ARRAY_SIZE`），反对角线 `s` 的 cell 数：

\[
c(s) =
\begin{cases}
s+1, & 0 \le s \le N-1 \\
2N-1-s, & N-1 \le s \le 2N-2
\end{cases}
\]

对本项目 `N=8`：`s=0..7` 时 `c(s)=s+1`（递增 1→8）；`s=8..14` 时 `c(s)=15-s`（递减 7→1）；`s=15` 超出范围，`c(15)=0`。

**互补配对的核心等式**：对任意 `s ∈ {0..N-1}`，

\[
c(s) + c(s+N) = N
\]

验证（`N=8`）：
- `s=0`：`c(0)+c(8) = 1+7 = 8`
- `s=3`：`c(3)+c(11) = 4+4 = 8`
- `s=7`：`c(7)+c(15) = 8+0 = 8`

这就是 gather 设计的数学根基：**把反对角线 `s`（进 `upper_bound`）与 `s+8`（进 `lower_bound`）配成一对，二者 cell 数之和恒为 8**，因此每个 `matrix_index` 总能挑出正好 `ARRAY_SIZE` 个 cell，把 `mul_outcome` 的 8 段填满，输出带宽利用率 100%。

更进一步，让 `matrix_index`（等价地 `upper_bound`）从 0 走到 7，覆盖的反对角线是 `0..7`（上）与 `8..14`（下）—— 也就是全部 `0..14` 共 64 个 cell，**每个输出元素恰好被读出一次**：

| matrix_index | upper (s) | 上段 cell 数 | lower (s+8) | 下段 cell 数 | 合计 |
| --- | --- | --- | --- | --- | --- |
| 0 | 0 | 1 | 8 | 7 | 8 |
| 1 | 1 | 2 | 9 | 6 | 8 |
| 2 | 2 | 3 | 10 | 5 | 8 |
| 3 | 3 | 4 | 11 | 4 | 8 |
| 4 | 4 | 5 | 12 | 3 | 8 |
| 5 | 5 | 6 | 13 | 2 | 8 |
| 6 | 6 | 7 | 14 | 1 | 8 |
| 7 | 7 | 8 | 15 | 0 | 8 |

8 行合计 `8×8 = 64` 个 cell，整张 8×8 结果矩阵被完整读出。

两段循环的扫描区域用文字示意（8×8，行 `i` 向下、列 `j` 向右）：

```
        j=0 1 2 3 4 5 6 7
i=0    [ A  A  A  A  A  A  A  A ]   ← 第一段循环覆盖左上(含对角线): j <= 7-i
i=1    [ A  A  A  A  A  A  A  B ]
i=2    [ A  A  A  A  A  A  B  B ]
 ...
i=7    [ A  B  B  B  B  B  B  B ]   ← 第二段循环覆盖右下: j >= 8-i
```

`A` 区由第一段循环处理（挑 `i+j==upper_bound`），`B` 区由第二段循环处理（挑 `i+j==lower_bound`）。两区合起来就是整个矩阵。

#### 4.3.3 源码精读

第一段循环（左上三角，挑 `upper_bound` 反对角线）：

[rtl/systolic.v:137-142](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L137-L142) —— `for(i=0; i<ARRAY_SIZE; i=i+1) for(j=0; j<ARRAY_SIZE-i; j=j+1) if(i+j == upper_bound) mul_outcome[i*OUTCOME_WIDTH+:OUTCOME_WIDTH] = matrix_mul_2D[i][j];`。注意 `j` 的上界是 `ARRAY_SIZE-i`，把扫描限制在左上三角；命中条件是 `i+j == upper_bound`。

第二段循环（右下三角，挑 `lower_bound` 反对角线）：

[rtl/systolic.v:144-149](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L144-L149) —— `for(i=1; i<ARRAY_SIZE; i=i+1) for(j=ARRAY_SIZE-i; j<ARRAY_SIZE; j=j+1) if(i+j == lower_bound) mul_outcome[i*OUTCOME_WIDTH+:OUTCOME_WIDTH] = matrix_mul_2D[i][j];`。这里 `i` 从 1 开始（因为 `i=0` 时 `lower_bound ≥ 8` 不可能命中），`j` 从 `ARRAY_SIZE-i` 起，扫描右下三角。

整个 gather 块一气呵成：

[rtl/systolic.v:121-151](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L121-L151) —— 先折算边界、再清零、再两段循环挑 cell 拼装，构成 `mul_outcome` 的完整产生过程。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：对 `matrix_index = 0` 与 `matrix_index = ARRAY_SIZE (=8)` 两种情况，写出 `upper_bound / lower_bound`，并标出哪几个 `matrix_mul_2D[i][j]` 被挑进 `mul_outcome` 的哪一段。再补一个对照例 `matrix_index = 7`。

**操作步骤**：

1. 由 4.2 的折算规则，先算出两种 `matrix_index` 的边界。
2. 对每段 `i = 0..7`，分别套第一段循环（找 `j` 使 `i+j == upper_bound` 且 `j <= 7-i`）和第二段循环（找 `j` 使 `i+j == lower_bound` 且 `j >= 8-i`），确定该段来自哪个 cell。
3. 把结果填入下表。

**需要观察的现象 / 预期结果**：

**情形 A：matrix_index = 0**（走 `< 8` 分支：`upper=0`, `lower=8`）

- 第一段循环（`upper=0`）：只有 `i=0, j=0` 满足 `i+j==0` → 段0 = `matrix_mul_2D[0][0]`。
- 第二段循环（`lower=8`）：`i=1..7`，`j=8-i` →
  - 段1 = `matrix_mul_2D[1][7]`
  - 段2 = `matrix_mul_2D[2][6]`
  - 段3 = `matrix_mul_2D[3][5]`
  - 段4 = `matrix_mul_2D[4][4]`
  - 段5 = `matrix_mul_2D[5][3]`
  - 段6 = `matrix_mul_2D[6][2]`
  - 段7 = `matrix_mul_2D[7][1]`

汇总表：

| 段 | 位域 | 来源 cell `matrix_mul_2D[i][j]` | 所在反对角线 `i+j` |
| --- | --- | --- | --- |
| 0 | [20:0] | `[0][0]` | 0（upper） |
| 1 | [41:21] | `[1][7]` | 8（lower） |
| 2 | [62:42] | `[2][6]` | 8（lower） |
| 3 | [83:63] | `[3][5]` | 8（lower） |
| 4 | [104:84] | `[4][4]` | 8（lower） |
| 5 | [125:105] | `[5][3]` | 8（lower） |
| 6 | [146:126] | `[6][2]` | 8（lower） |
| 7 | [167:147] | `[7][1]` | 8（lower） |

即 1 个 cell 来自反对角线 0，7 个 cell 来自反对角线 8，互补凑齐 8 段。

**情形 B：matrix_index = 8**（走 `≥ 8` 分支：`upper = 8-8 = 0`, `lower = 8`）

边界与情形 A **完全相同**（`upper=0, lower=8`），因此挑选结果与上表**逐段一致**。这正是 4.2 所说的「`matrix_index` 以 8 为周期复用边界」的直接体现：`0` 与 `8` 读同一批 cell 位置，只是它们在流水线稳态下分别对应先后流入的两批数据（由控制器与 `data_set` 协调，详见 u3-l1）。

**对照情形 C：matrix_index = 7**（`upper=7`, `lower=15`）

- 第一段循环（`upper=7`）：`i=0..7, j=7-i`，命中 8 个 cell → 段0=`[0][7]`, 段1=`[1][6]`, 段2=`[2][5]`, 段3=`[3][4]`, 段4=`[4][3]`, 段5=`[5][2]`, 段6=`[6][1]`, 段7=`[7][0]`，全部来自主反对角线 `i+j=7`。
- 第二段循环（`lower=15`）：矩阵最大反对角线是 14，`15` 命中 0 个 cell。

所以 `matrix_index=7` 是「一条最满的反对角线独占全部 8 段」的特例，与 `matrix_index=0`「两条互补反对角线合占 8 段」形成鲜明对比，正好说明配对设计的普适性。

**运行结果**：本实践为源码阅读 + 纸上推演型，结论可由源码规则严格推出。若想在仿真里眼见为实，可在 `Pre-Synthesis_Simulation/test_tpu.v` 中于 `tpu_start` 拉高后、用波形观察 `systolic` 实例的 `matrix_index` 与 `mul_outcome`，对照本表核对（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么第一段循环里 `j` 的上界写成 `ARRAY_SIZE-i`，而不是简单的 `ARRAY_SIZE`？

**答案**：这是为了把扫描限制在左上三角（含主对角线），即 `j <= N-1-i` 等价于 `i+j <= N-1`。左上三角里 `i+j` 最大为 `N-1`，恰好与 `upper_bound ∈ {0..N-1}` 的范围匹配，避免把本该由第二段循环处理的右下三角 cell 重复扫到。

**练习 2**：两段循环都向 `mul_outcome[i*OUTCOME_WIDTH +: OUTCOME_WIDTH]` 写入，会不会发生「同一段被写两次、后者覆盖前者」的冲突？

**答案**：不会。第一段循环命中的 cell 满足 `i+j = upper_bound ≤ N-1`，故 `i ≤ upper_bound ≤ N-1`，但更重要的是它要求 `j ≤ N-1-i`，配合 `upper_bound` 的取值，命中的行 `i` 范围是 `0..upper_bound`；第二段循环命中的 cell 满足 `i+j = lower_bound ≥ N` 且 `j ≥ N-i`，命中的行 `i` 范围是 `lower_bound-(N-1) .. N-1`，即从 `upper_bound+1` 起。两段命中的行号集合不交（一个占 `0..upper_bound`，一个占 `upper_bound+1..N-1`），因此写入的段号不重叠，各段恰好被写一次。

**练习 3**：用一句话概括 gather 解决了什么问题。

**答案**：它把「单条反对角线 cell 数从 1 到 8 不等」这一不规则输出，通过「互补反对角线 `s` 与 `s+8` 配对」规整成「每拍恒定输出 `ARRAY_SIZE` 个结果」，从而 100% 利用 `mul_outcome` 的 8 段带宽，并在 `matrix_index` 走过 `0..7` 时恰好把 64 个输出元素各读一次。

## 5. 综合实践

**任务**：用一张总表把 gather 的全局行为画出来，并把它和下游模块串起来。

1. **重建覆盖总表**：仿照 4.3.2 的表格，为 `matrix_index = 0..7` 每一行写出 `(upper, lower)`、上段 cell 数、下段 cell 数、合计，并验证 8 行合计为 64。
2. **画出数据通路**：在一张图上标出
   `matrix_mul_2D[8][8]` →（gather，按 `matrix_index` 选 8 个 cell）→ `mul_outcome[167:0]` →（顶层 `ori_data`）→ `quantize`（切成 8×21bit，饱和量化为 8×16bit）→ `quantized_data[127:0]` → `write_out`。
   在 `mul_outcome` 这条线上标出「段号 = 行号 i」的对应关系。
3. **回答两个问题**：
   - 如果设计者不做「互补配对」，而是每拍只读一条反对角线，`mul_outcome` 的 8 段里平均会有多少段闲置？（提示：64 个 cell / 15 条反对角线 ≈ 每条约 4.3 个，远不到 8。）
   - `matrix_index = 0` 和 `matrix_index = 8` 产生相同的 `mul_outcome` 段布局，那仿真里（u4）如何区分这两次输出属于不同的逻辑矩阵？

**预期成果**：一张完整的 `matrix_index → cell 选择` 覆盖表、一张贯穿 `systolic → quantize → write_out` 的数据通路图，以及对「配对必要性」和「同布局不同数据集」的清晰文字解释。

## 6. 本讲小结

- `mul_outcome` 是一条 `ARRAY_SIZE * OUTCOME_WIDTH = 168` bit 的打包总线，切成 8 段、每段 21bit，段号即行号 `i`；gather 块开头先逐位清零以防推断出锁存器。
- `matrix_index` 经 `if/else` 折算成 `(upper_bound, lower_bound)`，且 `matrix_index` 以 `ARRAY_SIZE=8` 为周期复用边界：`0` 与 `8`、`1` 与 `9`、……、`7` 与 `15` 边界完全相同。
- 两段 `for` 循环分别扫描左上三角（`j <= 7-i`，挑 `upper_bound`）与右下三角（`j >= 8-i`，挑 `lower_bound`），都把命中 cell 写入段 `i`，两段命中的行号不交，各段恰好写一次。
- 数学根基是「互补反对角线对」：`c(s) + c(s+8) = 8` 恒成立，所以每个 `matrix_index` 总能挑出正好 8 个 cell，输出带宽利用率 100%。
- `matrix_index` 走过 `0..7` 即可覆盖全部 64 个输出元素各一次；`8..15` 在流水线稳态下复用同一套边界，对应第二批数据，具体时序由控制器与 `data_set` 协调。
- gather 是纯组合逻辑，读出时机由控制器在 `cycle_num >= FIRST_OUT (=9)` 后才推进 `matrix_index` 并拉高 `sram_write_enable`，保证「读到的 cell 已算完」且「读出与写回同拍」。

## 7. 下一步学习建议

本讲讲完了「结果怎么从阵列里取出来」。接下来建议：

1. **u3-l1（主控状态机 systolic_controll）**：搞清楚 `matrix_index`、`cycle_num`、`data_set`、`sram_write_enable` 的精确推进时序，理解为什么 `matrix_index=0` 和 `=8` 读相同位置却对应不同数据，以及 `tpu_done` 何时拉高。
2. **u3-l3（输出写回 write_out）**：看 `quantized_data`（本讲 `mul_outcome` 量化后的产物）如何按 `matrix_index` 再次重排，写入 a/b/c 三组输出 SRAM，完成「计算 → 收集 → 量化 → 写回」全链路。
3. **u3-l4（后处理量化 quantize）**：精读 `quantize.v`，看它如何把本讲的 21bit 段饱和截断成 16bit，理解 `ori_shifted_data` 与 `mul_outcome` 段的一一对应。
4. **u4（端到端仿真）**：在 `test_tpu.v` 的波形里实际观察 `matrix_index` 从 0 扫到 15、`mul_outcome` 随之变化的过程，把本讲的纸面推演变成可见的波形验证。

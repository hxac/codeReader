# 权重 FIFO 与歪斜喂入（weightFifo / dff8）

## 1. 本讲目标

本讲精读扩展架构中专门负责「权重缓存与时间歪斜」的两个小模块：`dff8` 与 `weightFifo`。学完后你应当能够：

- 说清 `dff8` 这个带同步复位、带使能的 D 触发器在三种输入条件下（复位 / 使能 / 保持）各自做什么，以及 `signed` 与 `q <= q` 的综合含义。
- 画出 `weightFifo` 如何用两个 `generate` 把若干个 `dff8` 串成一张「列 × 深度」的二维移位阵列，并解释数据如何从 `weightIn` 一拍一拍移到 `weightOut`。
- 解释为什么使能信号 `en` 是「按列共享」的——同一列里所有深度上的 DFF 共用一个 `en` 位，从而让控制器能独立地暂停或推进某一列的权重，制造脉动阵列所需的时间歪斜（skew）。
- 推算 `FIFO_DEPTH` 与延迟拍数的关系，并理解 `top.v` 里把 `FIFO_DEPTH = WIDTH_HEIGHT = 16` 的用意。

## 2. 前置知识

在进入源码前，先用三句话建立直觉。

**第一，什么是移位 FIFO。** 普通的 SRAM 型 FIFO 用一块存储器 + 读写指针实现「先进先出」；而这里的 `weightFifo` 是**用一串 D 触发器首尾相连**做成的「移位型 FIFO」——没有存储器、没有指针，数据靠每个时钟沿整体下移一格来流动。它的好处是：读出零延迟、时序干净，代价是面积随深度线性增长，所以只适合做**浅而宽**的缓冲（本讲里深度只有 4 或 16）。

**第二，为什么权重需要「歪斜」。** 这承接 [u2-l1](u2-l1-shift-queues.md) 与 [u3-l2](u3-l2-addr-sel-skew.md) 已经建立的认知：脉动阵列要算对矩阵乘，相邻 cell 上的数据必须**错开一拍**到达。核心 `rtl/` 是靠 `addr_sel` 给 SRAM 读地址加一个 −4 的偏移来制造数据侧的歪斜（见 u3-l2）；而扩展架构在**权重侧**用一串触发器做延迟，等价地实现歪斜——只是把「地址偏移」换成了「触发器延迟」。这就是本讲的物理直觉。

**第三，权重固定（weight-stationary）的思路。** 核心 `rtl/` 是输出固定（output-stationary，见 [u1-l1](u1-l1-project-overview.md)）：每个 cell 就地累加。扩展架构里，权重先被一次性灌进 FIFO「固定」住，之后输入数据流过阵列时反复复用同一组权重——这正是权重固定的特征。FIFO 在这里同时扮演「缓存（让权重可复用）」与「延迟（让权重按时序歪斜到达）」两个角色。

> 说明：本讲如 [u6-l1](u6-l1-rtl-modified-overview.md) 所述，仓库中只收录了 `top.v` 及少数模块源码，`sysArr` 等被例化模块的内部源码不在库内，故涉及阵列内部行为的结论均依据 `weightFifo`/`dff8` 的端口、代码与 `software/instr_set.h` 的 API 描述推断。

## 3. 本讲源码地图

本讲只涉及两个文件，外加 `top.v` 中的一处例化点：

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| `rtl/RTL_modified/weightFifo/dff8.v` | 56 行 | 一个 `DATA_WIDTH` 位宽、带同步复位与使能的 D 触发器，是 FIFO 的「原子单元」。 |
| `rtl/RTL_modified/weightFifo/weightFifo.v` | 72 行 | 用 `generate` 把 `FIFO_INPUTS × FIFO_DEPTH` 个 `dff8` 串成移位 FIFO，按列共享使能。 |
| `rtl/RTL_modified/top.v`（L256–L265） | — | `weightFifo` 的唯一例化点：把 `weightMem` 的读出经 FIFO 歪斜后送给 `sysArr`。 |

调用链一览（数据方向 →）：

```
weightMem ──rd_data──▶ weightFifo.weightIn ──(移位 FIFO_DEPTH 级)──▶ weightFifo.weightOut ──▶ sysArr.win
                              ▲
              en = mem_to_fifo_en | fifo_to_arr_en   （按列共享，来自两个 fifo_control）
```

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：先讲原子单元 `dff8`，再讲 `weightFifo` 如何用 `generate` 把原子串成移位链，最后讲 `FIFO_DEPTH` 与按列共享使能如何共同决定歪斜拍数。

### 4.1 dff8：带使能与同步复位的 D 触发器

#### 4.1.1 概念说明

`dff8` 是一个最普通的 D 触发器（D Flip-Flop），但做了三处工程化包装：

1. **参数化位宽**：用 `parameter DATA_WIDTH = 8` 让一个触发器同时搬运 8 位（一个权重的位宽），而不是一位一位地搬。
2. **带使能（enable）**：多了一个 `en` 输入。`en=1` 时下一拍采新数据；`en=0` 时**保持原值不动**。这正是后面「按列暂停某一列」的基础。
3. **同步复位**：`reset` 在时钟上升沿才生效，且优先级最高。

为什么用 `signed`？因为权重是有符号定点数（与核心 `rtl/` 的 Q4.4 一致，也对应 `instr_set.h` 里的 `int8_t`），声明成 `signed` 后，综合工具与仿真器都会按补码解释，避免后续比较/扩展出错。

#### 4.1.2 核心流程

每个时钟上升沿，`dff8` 按下面的优先级决定下一拍输出 \( q_{\text{next}} \)：

\[
q_{\text{next}} =
\begin{cases}
0, & \text{if } \texttt{reset}=1 \quad\text{(复位，优先级最高)}\\
d, & \text{else if } \texttt{en}=1 \quad\text{(使能，采新值)}\\
q, & \text{otherwise} \quad\text{(保持)}
\end{cases}
\]

用伪代码描述就是：

```
on posedge clk:
    if (reset)   q <= 0          // 同步清零
    else if (en) q <= d          // 采样新输入
    else         q <= q          // 保持（综合成带使能的 FF）
```

注意第三条 `q <= q`：它写在**时钟沿触发的 `always` 块内**，所以综合出来是一个「带使能端的触发器」（en=0 时输入被忽略），而**不会**推断出锁存器（latch）。源码注释「expecting this to get synthesized away」指的就是这条自回环会被综合器优化成 enable 端口，而不是真的去写回自己。

#### 4.1.3 源码精读

模块声明与端口，注意 `d`/`q` 都标了 `signed`：

[dff8.v:30-38](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/weightFifo/dff8.v#L30-L38) —— 声明 `dff8(clk, reset, en, d, q)`，`DATA_WIDTH=8`，`d` 为 `signed` 输入，`q` 为 `signed reg` 输出。

核心时序逻辑，即上面三分支判定：

[dff8.v:40-54](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/weightFifo/dff8.v#L40-L54) —— `always @(posedge clk)` 内依次判断 `reset`、`en`，否则 `q <= q` 保持，并在注释里标注该自回环会被综合优化掉。

三个要点：
- `reset` 与 `en` 都是**电平敏感、在时钟沿采样**（同步），不是异步复位。
- 优先级固定为 复位 > 使能 > 保持，不可被覆盖。
- 因为是 `signed [7:0]`，`q <= d` 是 8 位补码整体搬运，不会拆位。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `dff8` 的三分支行为与「无锁存器」结论。

**操作步骤**（源码阅读 + 手算，因为仓库内没有 `dff8` 的独立 testbench）：

1. 打开 [dff8.v:40-54](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/weightFifo/dff8.v#L40-L54)。
2. 假设当前 `q = 8'sb00000111`（十进制 +7），`d = 8'sb11111001`（补码，十进制 −7）。在下表每一行填出**下一个上升沿后**的 `q`：

| reset | en | 下一拍 q（填十进制） |
| --- | --- | --- |
| 1 | 0 | ? |
| 1 | 1 | ? |
| 0 | 1 | ? |
| 0 | 0 | ? |

**需要观察的现象 / 预期结果**：

- 第 1、2 行：只要 `reset=1`，无论 `en` 如何，`q` 都被清成 0（复位优先级最高）。
- 第 3 行：`reset=0, en=1`，`q` 变成 `d` 的值 −7。
- 第 4 行：`reset=0, en=0`，`q` 保持原值 +7（这正是「按列暂停」时发生的事）。

3. 再回答一个综合问题：如果把整段 `always` 从 `always @(posedge clk)` 改成纯组合 `always @(*)`，并把 `q` 改成 `reg`，会发生什么？（提示：此时 `else q <= q` 会**推断出锁存器**，时序闭环被破坏——这正是源码坚持用时钟沿触发的原因。）

> 仓库内无独立仿真脚本，上表为手算推演；如需仿真验证，可自行写一个最小 testbench（见 4.3.4 的示例代码）。

#### 4.1.5 小练习与答案

**练习 1**：`dff8` 的复位是同步还是异步？如果想要「上电立刻清零、不等时钟」该怎么改？

**答案**：是**同步复位**（`reset` 在 `always @(posedge clk)` 内判断）。若要异步复位，需把敏感列表改成 `always @(posedge clk or posedge reset)`，并把 `if (reset)` 放在块首——本项目没有这样做，说明设计假设上电后由 `master_control` 的 `reset_global` 配合若干时钟周期完成初始化。

**练习 2**：`d` 和 `q` 为什么要写成 `signed`？去掉 `signed` 会怎样？

**答案**：权重是有符号 8 位补码（Q4.4 或 `int8_t`）。写成 `signed` 后，仿真与综合都按补码解释，后续若有比较、符号扩展才正确。去掉 `signed`，它们会被当成无符号数，本模块内 `q <= d` 的搬运表面无差异，但一旦在更上层做算术/比较就会出错。

---

### 4.2 weightFifo：用 generate 串成的移位 FIFO

#### 4.2.1 概念说明

`weightFifo` 把很多个 `dff8` 排成一张二维网格：

- **列（column）**：`FIFO_INPUTS` 列，对应同时并行喂入的 `FIFO_INPUTS` 个权重。默认 `FIFO_INPUTS = 4`，在 `top.v` 里被设成 `WIDTH_HEIGHT = 16`（阵列宽度）。
- **深度（depth / stage）**：`FIFO_DEPTH` 级，对应一个权重从入口走到出口要经过几个触发器。默认 `FIFO_DEPTH = 4`，在 `top.v` 里也是 16。

每个「格子」是一个 `dff8`。同一列里的 `FIFO_DEPTH` 个 `dff8` 首尾相连，构成一条**纵向移位寄存器**：权重从最上面一级（stage 0）进入，每个时钟沿整体下移一格，经过 `FIFO_DEPTH` 级后从最下面一级（stage `FIFO_DEPTH-1`）输出。所以整个 FIFO 就是 **`FIFO_INPUTS` 条并行的、彼此独立的移位寄存器**。

源码用「数组化例化（array of instances）」+ 两个 `generate` 循环来描述这张网格，而不是手写 16 或 256 行连接——这正是参数化设计的威力：换一个 `FIFO_DEPTH`，连线自动重生成。

#### 4.2.2 核心流程

设 `W[c][s]` 表示第 `c` 列、第 `s` 级触发器里持有的权重（`s=0` 为入口级，`s=FIFO_DEPTH-1` 为出口级）。每个时钟沿（且该列 `en[c]=1` 时）：

\[
W[c][s] \leftarrow W[c][s-1], \quad s = 1,2,\dots,\text{FIFO\_DEPTH}-1
\]
\[
W[c][0] \leftarrow \text{weightIn 的第 } c \text{ 个权重}
\]

\[ \text{weightOut 的第 } c \text{ 个权重} = W[c][\text{FIFO\_DEPTH}-1] \]

展开成数据流图（以 `FIFO_INPUTS=4`、`FIFO_DEPTH=4` 为例，只画第 `c` 列，其余列结构完全相同）：

```
weightIn[c] ──▶ [dff8 stage0] ──▶ [dff8 stage1] ──▶ [dff8 stage2] ──▶ [dff8 stage3] ──▶ weightOut[c]
                      │                 │                 │                 │
                      └──────── 共用一个 en[c] ────────────┘  （整列同步推进/保持）
```

关键性质：一个在周期 \( t \) 进入 `weightIn` 的权重，要经过 `FIFO_DEPTH` 个时钟沿才出现在 `weightOut`。所以 FIFO 引入的延迟为：

\[
\text{延迟拍数} = \text{FIFO\_DEPTH}
\]

这正是「歪斜」的来源——深度越大，权重被延迟得越久。

#### 4.2.3 源码精读

**参数与位宽派生**：

[weightFifo.v:29-34](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/weightFifo/weightFifo.v#L29-L34) —— 定义 `DATA_WIDTH=8`、`FIFO_INPUTS=4`、`FIFO_DEPTH=4`，并派生 `FIFO_WIDTH = DATA_WIDTH*FIFO_INPUTS = 32`（一行/一级里同时持有的比特数）。

**端口**：注意 `en` 与 `weightIn`/`weightOut` 的位宽与列方向的注释：

[weightFifo.v:36-40](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/weightFifo/weightFifo.v#L36-L40) —— `en[FIFO_INPUTS-1:0]` 每位管一列；`weightIn`/`weightOut` 各为 `FIFO_WIDTH` 位。注释里 `weightIn` 标「MSB is leftmost column」，而 `weightOut` 标「LSB is leftmost column」——见下方「需注意」。

**三条内部总线**：`colEn`（每个触发器一个使能位）、`dffIn`/`dffOut`（每个触发器一组 `DATA_WIDTH` 位数据）：

[weightFifo.v:42-44](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/weightFifo/weightFifo.v#L42-L44) —— `colEn` 宽 `FIFO_INPUTS*FIFO_DEPTH` 位，`dffIn`/`dffOut` 各宽 `FIFO_WIDTH*FIFO_DEPTH` 位。

**数组化例化**：一行就把 `FIFO_INPUTS*FIFO_DEPTH` 个 `dff8` 全部例化，并把三条总线分别接到每个实例的端口：

[weightFifo.v:46-52](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/weightFifo/weightFifo.v#L46-L52) —— `dff8 dffArray[FIFO_INPUTS*FIFO_DEPTH-1:0](...)`，按行优先（row-major）排布，每个实例从 `colEn`/`dffIn`/`dffOut` 中取属于自己的那段。

> 接线约定（数组化例化）：对于声明为 `[N-1:0]`（降序）的实例数组，把一条宽总线连到一个窄端口时，**下标最小的实例对齐总线的最低位**。因此实例 `k` 占用 `dffIn[8k+7 : 8k]`、`dffOut[8k+7 : 8k]`、`colEn[k]`。本讲后续的列号/级号推算都基于此约定。

**入口与出口接线**：第 0 级输入接 `weightIn`，最后一级输出接 `weightOut`：

[weightFifo.v:54-55](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/weightFifo/weightFifo.v#L54-L55) —— `dffIn[FIFO_WIDTH-1:0] = weightIn`（入口）；`weightOut = dffOut[FIFO_WIDTH*FIFO_DEPTH-1 : FIFO_WIDTH*(FIFO_DEPTH-1)]`（取最末一级）。

**generate 之一：级间数据连线（assignConn）**。这一段把每一级的输入接到上一级的输出，从而把散落的触发器串成移位链：

[weightFifo.v:57-62](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/weightFifo/weightFifo.v#L57-L62) —— `for (i=1; i<FIFO_DEPTH; i++)` 让第 `i` 级的输入 = 第 `i-1` 级的输出，等价于 `W[c][s] ← W[c][s-1]`。因为级数是参数，所以必须用 `generate` 在 elaboration 时生成对应条数的 `assign`。

**generate 之二：按列共享使能（colEn）**。这一段把同一列里所有深度的触发器绑到同一个 `en` 位：

[weightFifo.v:64-71](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/weightFifo/weightFifo.v#L64-L71) —— 双重循环遍历 `(列 i, 深 j)`，令 `colEn[j*FIFO_DEPTH+i] = en[i]`，使第 `i` 列的 `FIFO_DEPTH` 个触发器共享 `en[i]`。

> **需注意（源码观察，待确认）**：`weightIn` 与 `weightOut` 的注释对「哪一端是 leftmost column」给出了相反的描述（一个 MSB、一个 LSB）。但模块内部从 `weightIn → dffIn → dffOut → weightOut` 全程保持比特顺序不变，`weightOut` 只是 `weightIn` 延迟 `FIFO_DEPTH` 拍后的副本，**模块自身不做位序翻转**。因此这两条相反的注释要么反映外部接线的约定差异，要么是文档不一致；实际列方向映射以 `top.v` 中与 `sysArr.win` 的连接为准。另外，`colEn` 的线性下标 `j*FIFO_DEPTH+i` 与数据布局的 `j*FIFO_INPUTS+i` 只有在 `FIFO_INPUTS == FIFO_DEPTH` 时完全一致——而默认配置（4×4）与 `top.v` 实际配置（16×16）都满足这一条件，故当前用法正确；若要改成非方阵配置需重新核对此下标。

#### 4.2.4 代码实践

**实践目标**：用一张表追踪 `FIFO_INPUTS=4`、`FIFO_DEPTH=4` 配置下，一组 `weightIn` 在连续 4 个时钟周期内逐级移到 `weightOut` 的过程。

**操作步骤**：

1. 设 `en = 4'b1111`（四列全部使能），复位已在周期 0 之前完成，所有级初值为 0。
2. 记第 `t` 个周期入口处送入的第 `c` 列权重为 \( W_t^{(c)} \)。为简化，令每周期送入「该周期的编号」作为权重，即周期 `t` 的 `weightIn` 四列分别为 \( W_t^{(0)}{=}t,\ W_t^{(1)}{=}t,\ W_t^{(2)}{=}t,\ W_t^{(3)}{=}t \)（实际只需关注任意一列，四列行为相同）。
3. 仿照 4.2.2 的递推式 \( W[c][s]\leftarrow W[c][s-1] \)，填写下表「第 0 列」各级在每个**上升沿之后**持有的值（其余列数值相同）：

| 上升沿 # | stage0 \(W[0]\) | stage1 \(W[1]\) | stage2 \(W[2]\) | stage3=weightOut |
| --- | --- | --- | --- | --- |
| 1（周期0后） | 0 | 0 | 0 | 0 |
| 2（周期1后） | 1 | 0 | 0 | 0 |
| 3（周期2后） | 2 | 1 | 0 | 0 |
| 4（周期3后） | ? | ? | ? | ? |

**需要观察的现象 / 预期结果**：

- 第 4 行应为 `3, 2, 1, 0`——即周期 0 送入的 `0` 这时才出现在 `weightOut`。
- 这说明：周期 `t` 送入的权重，要到周期 `t + FIFO_DEPTH − 1` 结束（第 `t+FIFO_DEPTH` 个上升沿）才出现在 `weightOut`，**总延迟 = `FIFO_DEPTH` 拍**。
- 把 `FIFO_DEPTH` 改成 16（`top.v` 的配置），同一组数据就要 16 拍后才到达出口。

> 待本地验证：仓库未提供 `weightFifo` 的 testbench，上表为依据源码递推手算；建议自行写一个最小自校验 testbench 复现（4.3.4 给出示例代码）。

#### 4.2.5 小练习与答案

**练习 1**：`weightFifo.v` 里为什么用 `generate` 而不是直接手写每条 `assign`？

**答案**：因为级间连线数量与列使能数量都依赖参数 `FIFO_DEPTH`/`FIFO_INPUTS`。`generate for` 在 elaboration 阶段按参数展开成对应条数的 `assign`，既避免手写出错，又让换参数（如 4→16）时连线自动重生成。

**练习 2**：如果只把 `FIFO_DEPTH` 从 4 改成 6，`FIFO_WIDTH` 会变吗？`weightOut` 的延迟会变成几拍？

**答案**：`FIFO_WIDTH = DATA_WIDTH*FIFO_INPUTS` 只与 `DATA_WIDTH`、`FIFO_INPUTS` 有关，故 `FIFO_WIDTH` **不变**；但延迟变成 `FIFO_DEPTH = 6` 拍。也就是说「宽度」与「深度」是两个独立的旋钮，分别控制并行度与延迟。

---

### 4.3 FIFO_DEPTH 与按列共享使能：制造时间歪斜

#### 4.3.1 概念说明

前两节把 FIFO 当成「一条单纯的延迟线」。但 `weightFifo` 真正的价值在于两点工程巧思，它们共同制造了脉动阵列所需的时间歪斜：

1. **`FIFO_DEPTH` 决定延迟拍数（歪斜的「大小」）**。把 `FIFO_DEPTH` 设成 16（= `WIDTH_HEIGHT`，阵列宽度），意味着权重从入口到出口恰好被延迟一个「阵列宽度」量级的拍数，这与输入侧 `inputMem` 的读出节拍相配合，使权重与数据在 `sysArr` 里正确相遇。这与核心 `rtl/` 中 `addr_sel` 用「−4 偏移」制造歪斜（u3-l2）是同一思想在权重侧的体现。

2. **`en` 按列共享（歪斜的「形状」）**。使能不是一根线管全部，而是 `FIFO_INPUTS` 根线、每根管一列。控制器可以让第 0 列先开始移位、第 1 列晚一拍、第 2 列再晚一拍……于是不同列的权重**错拍**到达阵列——这正是脉动阵列要求相邻列错开一拍到达的物理实现。反之，如果只想暂停某一列（比如还在装载），把那一列的 `en` 拉低即可，其余列继续推进。

#### 4.3.2 核心流程

**按列共享使能的展开**（`FIFO_INPUTS=4`、`FIFO_DEPTH=4`）。根据 [weightFifo.v:64-71](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/weightFifo/weightFifo.v#L64-L71) 的 `colEn[j*FIFO_DEPTH+i] = en[i]`，每个 `en[i]` 扇出到该列全部 4 个深度上的触发器：

| 使能位 | 驱动的 colEn 触发器（下标） | 含义 |
| --- | --- | --- |
| `en[0]` | `colEn[0], colEn[4], colEn[8], colEn[12]` | 第 0 列的 stage0/1/2/3 同步推进 |
| `en[1]` | `colEn[1], colEn[5], colEn[9], colEn[13]` | 第 1 列同步推进 |
| `en[2]` | `colEn[2], colEn[6], colEn[10], colEn[14]` | 第 2 列同步推进 |
| `en[3]` | `colEn[3], colEn[7], colEn[11], colEn[15]` | 第 3 列同步推进 |

也就是说：`en[2]=0` 时，第 2 列的整条 4 级移位寄存器被**冻结**，而其它三列照常下移——这就是「按列暂停」。

**与控制器的配合**：在 `top.v` 中，`en` 来自两个 `fifo_control` 模块输出的按位或：

[top.v:256-265](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L256-L265) —— 例化 `weightFifo`，`.en(mem_to_fifo_en | fifo_to_arr_en)`，并把 `FIFO_INPUTS`、`FIFO_DEPTH` 都设成 `WIDTH_HEIGHT=16`。

- `mem_to_fifo_en`（来自 `mem_fifo`）：**装载阶段**，把 `weightMem` 读出的权重一列列灌进 FIFO。
- `fifo_to_arr_en`（来自 `fifo_arr`）：**排出阶段**，把 FIFO 里的权重歪斜地喂给 `sysArr`。

对应到软件 API（`instr_set.h`）：`tpu_fill_fifo()` 触发装载（写权重进 FIFO），`tpu_mat_mult()` 触发排出与计算。两者分别拉高 `mem_to_fifo_en` 与 `fifo_to_arr_en`，最终都汇集成 `weightFifo` 的 `en`。

#### 4.3.3 源码精读

按列共享使能的 `generate` 双循环已在 [weightFifo.v:64-71](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/weightFifo/weightFifo.v#L64-L71) 给出。要点：

- 外层 `widthIndex` 遍历**列** `i`，内层 `depthIndex` 遍历**深度** `j`，对每列把 `en[i]` 赋给该列所有深度的 `colEn`。
- 由此，一根 `en[i]` 同时驱动 `FIFO_DEPTH` 个触发器的使能端；综合后这些触发器共享同一个 enable，利于时钟门控（clock gating）优化，降低功耗。

装载/排出两条控制路径的汇合点在 `top.v`：

[top.v:259](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L259) —— `.en(mem_to_fifo_en | fifo_to_arr_en)`，两个 `fifo_control` 的使能按位或，任一为 1 即推进对应列。

[top.v:263-265](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L263-L265) —— `defparam` 把 `DATA_WIDTH=8`、`FIFO_INPUTS=16`、`FIFO_DEPTH=16` 传给 FIFO，对应 16×16 阵列：权重延迟 16 拍、16 列各自独立使能。

#### 4.3.4 代码实践

**实践目标**：验证「按列共享使能」能冻结单列而不影响其它列；并为整个 `weightFifo` 写一个最小自校验 testbench，复现 4.2.4 的移位表。

**操作步骤**：

1. **单列冻结手算**。设 `FIFO_INPUTS=4`、`FIFO_DEPTH=4`，某周期起 `en = 4'b1011`（即第 2 列 `en[2]=0`，其余为 1）。各列送入新值。回答：一个周期后，第 2 列的 stage0 持有什么值？其它列呢？
   - **预期**：第 2 列 stage0 **保持**上一拍的旧值（被冻结）；第 0、1、3 列 stage0 **采入**新 `weightIn`。
2. **（可选，示例代码）最小 testbench**。下面是一段**示例代码（仓库中不存在，需自行创建）**，用来验证 `FIFO_INPUTS=4`、`FIFO_DEPTH=4` 的延迟与冻结行为：

```verilog
// 示例代码：仓库内不存在，仅供本讲练习使用
module tb_weightFifo;
    reg clk=0, reset=1;
    reg [3:0] en;
    reg [31:0] weightIn;
    wire [31:0] weightOut;
    integer t;

    weightFifo dut(.clk(clk), .reset(reset), .en(en), .weightIn(weightIn), .weightOut(weightOut));
    defparam dut.DATA_WIDTH=8; dut.FIFO_INPUTS=4; dut.FIFO_DEPTH=4;

    always #5 clk = ~clk;          // 10ns 周期
    initial begin
        en=4'b1111; weightIn=32'h0; reset=1;
        #12 reset=0;               // 周期0后释放复位
        for (t=1; t<=8; t=t+1) begin
            weightIn = t;          // 每周期送入 t
            #10;
            $display("edge %0d: weightOut=%h", t, weightOut);
        end
        // 让第2列冻结，观察其余列仍推进
        en = 4'b1011; weightIn = 32'hAA; #10;
        $display("frozen col2, edge: weightOut=%h", weightOut);
        $finish;
    end
endmodule
```

**需要观察的现象 / 预期结果**：

- 复位释放后，`weightIn=t` 要到第 `t+3` 次打印附近才出现在 `weightOut`（延迟 4 拍）。
- `en=4'b1011` 时，`weightOut` 的第 2 个字节（对应第 2 列）应**保持不变**，而第 0/1/3 个字节继续推进——直观证明「按列共享使能」。

> 待本地验证：上述 testbench 为示例代码，需自行放入仿真目录并补上 `dff8.v`/`weightFifo.v` 的文件列表后运行；具体打印节拍取决于复位释放时机，请以你的仿真波形为准。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `en` 要按列设计成多位，而不是用一根线统管整个 FIFO？

**答案**：因为脉动阵列要求相邻列的权重**错拍到达**——第 `c+1` 列要比第 `c` 列晚一拍。多位 `en` 让控制器能逐列控制开始移位的时刻，从而「画出」歪斜的形状；单根 `en` 只能让全阵列同时推进，无法产生列间错拍。

**练习 2**：`top.v` 里 `en = mem_to_fifo_en | fifo_to_arr_en`，为什么用「或」而不是「与」？

**答案**：装载（`mem_to_fifo_en`）和排出（`fifo_to_arr_en`）是两个不同阶段，但都表现为「让 FIFO 某列推进」。用「或」表示「只要任一阶段需要该列移位，就推进」，符合两个控制源互不冲突、各自驱动自己关心的列的语义；若用「与」，则要求两阶段同时同意才移位，会让装载或排出无法独立工作。

**练习 3**：把 `FIFO_DEPTH` 设成 `WIDTH_HEIGHT=16`，相比设成 1，对歪斜有什么影响？

**答案**：`FIFO_DEPTH=1` 时 FIFO 退化成单级，权重 1 拍直通，几乎没有延迟缓冲能力；`FIFO_DEPTH=16` 时权重被延迟 16 拍，恰好与阵列宽度量级匹配，使权重能与流过的多行输入数据正确对齐相遇，这也是 `top.v` 选择 16 的原因。

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个综合小任务。

**任务**：假设要把 `weightFifo` 从「4 列 × 4 深」改造成「4 列 × 6 深」，并让第 3 列的权重比其它列**晚 2 拍**进入 `sysArr`。请：

1. **改参数**：指出需要修改哪些 `parameter`/`defparam`（提示：只改 `FIFO_DEPTH`，不改 `FIFO_INPUTS`），并说明 `FIFO_WIDTH`、`dffIn`/`dffOut`/`colEn` 总位宽各变成多少。
2. **推延迟**：改造后一个权重从 `weightIn` 到 `weightOut` 需要几拍？
3. **设计使能波形**：结合「按列共享使能」，给出前若干拍 `en[3:0]` 的取值序列，使第 3 列的首次推进比第 0/1/2 列晚 2 拍（提示：在前 2 拍令 `en[3]=0`，其余列照常 `en=1`；之后 `en=4'b1111`）。说明为什么这种「逐列延迟使能」等价于在权重流里注入歪斜。
4. **核对非方阵 caveat**：改造后 `FIFO_INPUTS(4) ≠ FIFO_DEPTH(6)`。请回到 [weightFifo.v:64-71](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/weightFifo/weightFifo.v#L64-L71) 检查 `colEn[j*FIFO_DEPTH+i]` 的下标是否仍能与数据布局 `j*FIFO_INPUTS+i` 对齐；若不对齐，给出修正方案（例如把 `colEn` 下标改成 `j*FIFO_INPUTS+i`）。

**预期结果（自检）**：

1. 只改 `FIFO_DEPTH = 6`。`FIFO_WIDTH=32`（不变）；`dffIn`/`dffOut` 宽 `FIFO_WIDTH*FIFO_DEPTH = 32*6 = 192` 位；`colEn` 宽 `FIFO_INPUTS*FIFO_DEPTH = 4*6 = 24` 位。
2. 延迟 = `FIFO_DEPTH = 6` 拍。
3. 前 2 拍 `en = 4'b0111`，之后 `en = 4'b1111`，即可让第 3 列晚 2 拍启动。
4. 非方阵时 `j*FIFO_DEPTH+i = j*6+i` 与数据布局 `j*4+i` **不对齐**（例如 `j=1,i=0`：前者得 6，后者得 4），需把 `colEn` 下标改为 `j*FIFO_INPUTS+i` 才能正确「按列共享」。这印证了 4.2.3 的待确认观察。

## 6. 本讲小结

- `dff8` 是一个 `DATA_WIDTH` 位宽、**同步复位、带使能**的 D 触发器：`reset` 优先清零，`en=1` 采新值，`en=0` 时 `q<=q` 保持；因写在时钟沿块内，综合成带使能的 FF 而非锁存器；`d`/`q` 用 `signed` 体现权重的补码语义。
- `weightFifo` 用「数组化例化 + 两个 `generate`」把 `FIFO_INPUTS × FIFO_DEPTH` 个 `dff8` 排成网格：`assignConn` 串联级间数据形成移位链，`colEn` 把使能按列共享。整模块是 `FIFO_INPUTS` 条并行的纵向移位寄存器。
- **延迟 = `FIFO_DEPTH` 拍**：`weightOut` 是 `weightIn` 延迟 `FIFO_DEPTH` 个时钟沿的副本，模块内不做位序翻转；`FIFO_DEPTH` 越大，权重被延迟越久，歪斜越大。
- **按列共享使能** `en[i]` 同时驱动第 `i` 列的全部 `FIFO_DEPTH` 个触发器，使控制器能逐列暂停或错拍推进，从而「画出」脉动阵列所需的列间歪斜形状——这是核心 `rtl/` 中 `addr_sel` 地址歪斜（u3-l2）在权重侧的对应物。
- 在 `top.v` 中 `FIFO_INPUTS = FIFO_DEPTH = WIDTH_HEIGHT = 16`，`en = mem_to_fifo_en | fifo_to_arr_en`，分别承载「装载（`tpu_fill_fifo`）」与「排出计算（`tpu_mat_mult`）」两个阶段。
- 源码有一处文档不一致（`weightIn`/`weightOut` 对 leftmost column 的 MSB/LSB 注释相反）与一处下标依赖 `FIFO_INPUTS==FIFO_DEPTH` 的隐含假设，改非方阵配置时需复核。

## 7. 下一步学习建议

本讲把权重如何被缓存、延迟、歪斜喂入阵列讲清了。建议接下来：

- 阅读 [u6-l3](u6-l3-accumtable-relu.md)：看 `sysArr` 产出的结果如何被 `accumTable` 按「子矩阵」分块累加、再经 `reluArr` 激活写入 `outputMem`，理解大于阵列尺寸的大矩阵是如何靠分块算出来的。
- 回看 [u6-l1](u6-l1-rtl-modified-overview.md) 的数据通路图，把本讲的 `weightFifo` 与 `inputMem → sysArr → accumTable → reluArr → outputMem` 主链对齐，体会「权重固定 + 数据流动」的整体节奏。
- 若关心歪斜的另一种实现，对照 [u3-l2](u3-l2-addr-sel-skew.md) 的 `addr_sel`：核心 `rtl/` 用「地址偏移」制造歪斜，扩展架构用「触发器延迟」制造歪斜，两者是同一问题的两种硬件解法。
- 进阶练习：尝试把 4.3.4 的示例 testbench 跑通，并用波形观察 `en[2]=0` 时第 2 列各级值的「冻结」现象，把本讲的所有结论用仿真一次性验证。

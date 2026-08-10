# 行缓冲与卷积计算流水线

## 1. 本讲目标

u5-l1 讲清了卷积「读 → 算 → 写回」三段状态机的总体分工，但刻意把一个最棘手的细节留到了本讲：**模块一次只能从存储里读出 2 个像素（一个 16 位字），可 3×3 卷积窗却需要 3 行 × 3 列共 9 个像素的邻域**。这两件事怎么对得上？

学完本讲，你应该能够：

1. 说清楚为什么卷积要维护一个「双行缓冲」`convolution_buffer`，以及它为什么被刻意拆成两拍写回（为了被综合器推断成 SPRAM）。
2. 画出 `convolution_buffer_local` 这个 **4 列 × 3 行** 局部矩阵，并解释「4 列」而不是「3 列」的根本原因——一次处理左右两个像素，窗各错开一列。
3. 解释 `convolution_previous_read` 这个移位寄存器为什么必须存在（每拍读 2 个像素，但邻域按奇数对齐），以及 `WRITEBACK_2` 里 `[1] <= [0]` 的移位动作在推进哪条流水。
4. 看懂 `calc_left_buf` / `calc_right_buf` 两个累加器如何在 10 拍内并行算完左右两个像素的 9 次乘加。
5. 说出读计数器（`counter_convolution_x/y`）与写计数器（`..._write`）之间的「读超前、写滞后」偏移，以及为什么边界像素直接置 0。

---

## 2. 前置知识

本讲假设你已经读过 u5-l1，知道：

- 3×3 卷积 = 用 9 个定点系数对邻域加权求和；卷积核与结果都走 1.3.4 定点格式，结果最后经 `apply_clamp_fixed16` 取 `[11:4]` 还原尺度。
- 卷积运算 FSM 有四个状态循环：`STATE_PROC_CONVOLUTION`（读输入 + 预取邻域）→ `STATE_PROC_CONVOLUTION_CALCULATION`（9 拍乘加）→ `STATE_PROC_CONVOLUTION_WRITEBACK_1` → `STATE_PROC_CONVOLUTION_WRITEBACK_2`（写回）。
- 存储是 **单端口 RAM**：一个时钟沿只能读 *或* 写一个 16 位字，读数据要延迟一拍才在 `data_read` 上有效（`data_read_valid` 握手）。
- 每个 16 位字打包 **2 个 8 位像素**：`data_read[7:0]` 是「左像素」，`data_read[15:8]` 是「右像素」。

还有一个朴素但关键的算术事实，本讲会反复用到：

\[ \text{每个字} = 2 \text{ 个像素}, \quad \text{而 } 3\times3 \text{ 窗需要每行 } 3 \text{ 个像素} \]

2 和 3 一个偶一个奇，正是所有麻烦的来源。本讲其实就是讲项目作者如何用最少的片上资源把这个「2 与 3 的错位」摆平。

---

## 3. 本讲源码地图

本讲只涉及一个文件，但会反复在它的几个段落间来回跳：

| 关键源码段落 | 行号 | 作用 |
|---|---|---|
| 行缓冲与局部矩阵声明 | `image_processing.v:102-122` | `convolution_buffer`、`convolution_buffer_local`、`convolution_previous_read`、各计数器 |
| 卷积启动初始化 | `image_processing.v:444-459` | 在 `STATE_CONVOLUTION_READ_PARAM` 末尾把所有卷积寄存器清零、设读/写基地址 |
| `STATE_PROC_CONVOLUTION` | `image_processing.v:630-673` | 读输入字、填局部矩阵、进入计算 |
| `STATE_PROC_CONVOLUTION_CALCULATION` | `image_processing.v:674-781` | 10 拍并行算左右两像素、边界置 0 |
| `STATE_PROC_CONVOLUTION_WRITEBACK_1/2` | `image_processing.v:782-839` | 把当前读移位进 `previous_read`、写入行缓冲、推进计数器 |

源码永久链接 base：
`https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/`

---

## 4. 核心概念与源码讲解

### 4.1 双行缓冲 convolution_buffer 与 SPRAM 推断

#### 4.1.1 概念说明

3×3 卷积要求对「中心像素的上一行、当前行、下一行」各取 3 个像素。如果每算一个像素都回到主存里去取它上下两行的邻居，单端口 RAM 的带宽根本撑不住（一次卷积要 9 次随机读取）。

标准解法是 **行缓冲（line buffer）**：把图像按行流式读入，在片上保留「刚刚过去的那几行」，这样当中心像素到达时，它的上下行邻居已经在片上现成可取。

本项目只保留 **两行** 历史就够，因为：

- 当某一行作为「当前行」被流式读入时，它的上一行已经完整在片上；
- 再上一行（即中心像素的「上一行的上一行」）也只需一行历史。

于是用一个二维数组存「最近两行」，每行宽度 = 图像宽度（最多 256，由 `CONVOLUTION_LINE_MAX_SIZE` 限定）：

```verilog
parameter CONVOLUTION_LINE_MAX_SIZE = 256;          // 第 85 行
reg [7:0] convolution_buffer [0:CONVOLUTION_LINE_MAX_SIZE-1][0:1];  // 第 103 行
```

第二维 `[0:1]` 只有 **2 个槽位**，对应两行历史。哪个槽存「较新行」、哪个存「较旧行」，靠 **行号的奇偶** 轮换（ping-pong）：写到 `convolution_buffer[x][ 行号奇偶 ]`，读时用 `(行号奇偶+1)%2` 取另一行（下一节细讲）。这种「两槽轮流」的好处是写新一行的同时自动覆盖两行前的旧行，**不用搬数据**，与 u3-l2 的双缓冲思想同源。

#### 4.1.2 核心流程

行缓冲的写发生在写回阶段，**一次只写 1 个字节**：

1. `WRITEBACK_1`：把 `convolution_previous_read[1]` 的低字节写到 `convolution_buffer[x][行奇偶]`。
2. `WRITEBACK_2`：把同一组数据的高字节写到 `convolution_buffer[x+1][行奇偶]`，同时推进读计数器、判断是否写结果回主存。

为什么把「写 2 个字节」拆成两个状态、两个时钟沿？因为目标平台是 **单端口 SPRAM**（`SB_SPRAM256KA`），**一个时钟沿只能写一次**。若在同一拍里对同一数组发两个写地址，综合器无法把它映射成一块 SPRAM，只能退化成散落的触发器，面积爆炸。代码注释点明了这一点。

#### 4.1.3 源码精读

行缓冲与配套寄存器的声明：

[声明：convolution_buffer（两行历史）与 convolution_buffer_local、previous_read 等寄存器](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L102-L122)

```verilog
//will keep the previous lines for convolution
reg [7:0] convolution_buffer [0:CONVOLUTION_LINE_MAX_SIZE-1][0:1];
...
//buffers to keep last reads, as we need 3x3 matrices but only have two lines of buffers
reg [15:0] convolution_previous_read[1:0];
```

写回阶段的两拍（注意每个状态只写一个字节）：

[WRITEBACK_1：把上一组读取的低字节写入行缓冲](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L782-L787)

```verilog
STATE_PROC_CONVOLUTION_WRITEBACK_1: begin
   //this is done in two states to infer a spram for the convolution buffer
   convolution_buffer[convolution_previous_read_counter_x[1][7:0]][convolution_previous_read_counter_y[1][0]] <= convolution_previous_read[1][7:0];
   state_processing <= STATE_PROC_CONVOLUTION_WRITEBACK_2;
end
```

[WRITEBACK_2：把同一组读取的高字节写入行缓冲（第二拍，满足单端口一拍一写）](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L798-L799)

```verilog
//second write to conv. buffer (spram behaviour)
convolution_buffer[convolution_previous_read_counter_x[1][7:0]+1][convolution_previous_read_counter_y[1][0]] <= convolution_previous_read[1][15:8];
```

这里被写入的是 `convolution_previous_read[1]`（两拍前读到的那个字），而 *不是* 当前正在处理的 `data_read`——这是下一节「移位流水」要讲的核心。

#### 4.1.4 代码实践

**实践目标**：理解「两拍写回 = SPRAM 推断」这一硬件约束。

**操作步骤**：

1. 打开 [WRITEBACK_1 与 WRITEBACK_2](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L782-L799)，确认两个状态各自只对 `convolution_buffer` 发起 **一次** 非阻塞写。
2. 假想把第 799 行的高字节写也合并进 `WRITEBACK_1`，即同一拍写 `convolution_buffer[x]` 和 `convolution_buffer[x+1]`。
3. 对照 u6-l2 里 `SB_SPRAM256KA` 原语「一拍一个写口」的特性，说明这种合并会发生什么。

**需要观察的现象 / 预期结果**：单端口 SPRAM 一个时钟沿只接受一个写地址；同拍双写会让 yosys 无法把 `convolution_buffer` 推断成一块 SPRAM，只能展开成大量独立寄存器（512 字节 → 4096 个触发器），在 iCE40 上很可能资源不够或时序失败。这正是作者写注释 `// this is done in two states to infer a spram` 的原因。（本结论为源码阅读推导，待本地用 yosys 综合验证资源报告。）

#### 4.1.5 小练习与答案

**练习 1**：`convolution_buffer` 第二维为什么是 `[0:1]`（2 个槽）而不是 `[0:2]`（3 个槽）？3×3 卷积不是要 3 行吗？

> **答**：因为「第 3 行」——中心像素的下一行——是 **当前正从主存流式读入的那一行**（在 `data_read` / `convolution_previous_read` 里），并不需要预先存在行缓冲里。行缓冲只缓存「已经过去的两行」（上一行、上上行），加上当前流，正好凑齐 3 行。

**练习 2**：行缓冲用「行号奇偶」在两槽间 ping-pong，相比「维护三个独立行数组 + 指针」有什么好处？

> **答**：写新行时直接覆盖两行前的旧槽，无需搬移数据、无需显式指针维护，存储量恒为 2 行。和 u3-l2 的双缓冲「只换地址标签不搬数据」是同一种零拷贝思想。

---

### 4.2 4×3 局部矩阵 convolution_buffer_local：拼出 3×3 邻域（含 previous_read）

#### 4.2.1 概念说明

本模块是全讲义的「题眼」。它回答两个纠缠在一起的问题：

1. **每拍只读 2 个像素，怎么凑出每行 3 个像素的邻域？**
2. **为什么局部矩阵是 4 列而不是 3 列？**

先回答第 2 个，因为它解释了第 1 个。回想 u3：一个 16 位字打包 **左右相邻的两个像素**，卷积每算完一个字就把左右两像素的结果一起写回。对「左像素」做 3×3 卷积需要列 \(c-1, c, c+1\)；对紧挨它的「右像素」（列 \(c+1\)）需要列 \(c, c+1, c+2\)。两个窗共享中间两列，合起来需要 **连续 4 列**：\(c-1, c, c+1, c+2\)。所以局部矩阵是 **4 列 × 3 行**，左像素用左 3 列，右像素用右 3 列，各错开一列。

```
列:      c-1    c     c+1   c+2
行 y-1:  L00   L10   L20   L30     ← convolution_buffer_local[i][0]
行 y  :  L01   L11   L21   L31     ← convolution_buffer_local[i][1]
行 y+1:  L02   L12   L22   L32     ← convolution_buffer_local[i][2]

左像素窗 = 列{c-1,c,c+1} × 3行   →  local[0..2][0..2]
右像素窗 = 列{c,c+1,c+2} × 3行   →  local[1..3][0..2]
```

再回答第 1 个。最上面两行（y-1、y）的像素已经在 `convolution_buffer` 里，按列直接取即可。麻烦在最下面那行（y+1，当前正在流式读入的行）：它还 **没进缓冲**，只能从「刚刚读到的字」里拼。可是每拍只读出 2 个像素，而 4 列窗要 4 个连续像素，怎么办？

答案：用一个 2 级移位寄存器 `convolution_previous_read[1:0]` 记住「上两次读到的字」，与当前读 `data_read` 拼起来。三次读 = 6 个连续像素，从中挑出所需的 4 个连续列。**因为每拍读 2 个像素、而邻域按奇数 3 对齐，单次读不够，必须借历史读凑齐**——这就是 `previous_read` 存在的根本原因。

#### 4.2.2 核心流程

设当前这一拍从主存读出字 \(W_n\)，它含两个像素：低字节 = 列 \(c\) 的像素，高字节 = 列 \(c+1\) 的像素。两次移位后：

| 寄存器 | 含义 | 低字节 `[7:0]` | 高字节 `[15:8]` |
|---|---|---|---|
| `data_read`（当前） | \(W_n\) | 列 \(c\) | 列 \(c+1\) |
| `previous_read[0]` | \(W_{n-1}\) | 列 \(c-2\) | 列 \(c-1\) |
| `previous_read[1]` | \(W_{n-2}\) | 列 \(c-4\) | 列 \(c-3\) |

当前行（y+1）需要的 4 个连续列 \(c-3, c-2, c-1, c\) 正好散落在这三次读里，按「`previous_read[1]` 高字节 → `previous_read[0]` 低字节 → `previous_read[0]` 高字节 → `data_read` 低字节」的顺序取出，填进 `local[i][2]`。于是「左像素」中心是列 \(c-2\)、「右像素」中心是列 \(c-1\)——**实际计算结果比当前读落后 2 列**，这正是 4.4 节要讲的「读超前、写滞后」。

填好 `convolution_buffer_local` 后，真正的 9 次乘加就只读这个寄存器矩阵，**不再碰行缓冲**——把对 `convolution_buffer` 的读取集中在填表那一拍，既减少读端口压力，又配合综合器推断 RAM（注释 `//do the lookup before the calculation (will infer the sprams!)`）。

#### 4.2.3 源码精读

局部矩阵与累加器声明：

[声明：4×3 局部矩阵、9 拍计数器、左右累加器](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L105-L109)

```verilog
//a 4x3 matrix for current calculation
reg [7:0] convolution_buffer_local [0:3][0:2];
reg [7:0] matrix_convolution_counter;
reg [15:0] calc_left_buf;
reg [15:0] calc_right_buf;
```

填表：上两行取自 `convolution_buffer`（按 `行奇偶` 区分新旧两行），最下一行取自 `previous_read` + 当前 `data_read`：

[在 STATE_PROC_CONVOLUTION 里一次性填满 4×3 局部矩阵](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L658-L671)

```verilog
// 第 0 行 = 旧行 (y-1)：索引 (行奇偶+1)%2
convolution_buffer_local[0][0] <= convolution_buffer[counter_convolution_x_write[7:0]-1][ (counter_convolution_y_write[0]+1)%2];
convolution_buffer_local[1][0] <= convolution_buffer[counter_convolution_x_write[7:0]  ][ (counter_convolution_y_write[0]+1)%2];
convolution_buffer_local[2][0] <= convolution_buffer[counter_convolution_x_write[7:0]+1][ (counter_convolution_y_write[0]+1)%2];
convolution_buffer_local[3][0] <= convolution_buffer[counter_convolution_x_write[7:0]+2][ (counter_convolution_y_write[0]+1)%2];
// 第 1 行 = 新行 (y)：索引 行奇偶
...
// 第 2 行 = 当前行 (y+1)：从 previous_read 与当前读拼出连续 4 列
convolution_buffer_local[0][2] <= convolution_previous_read[1][15:8];   // 列 c-3
convolution_buffer_local[1][2] <= convolution_previous_read[0][7:0];    // 列 c-2
convolution_buffer_local[2][2] <= convolution_previous_read[0][15:8];   // 列 c-1
convolution_buffer_local[3][2] <= data_read[7:0];                       // 列 c
```

`WRITEBACK_2` 里的移位动作——把「当前读」推进 `previous_read`，老数据顺次后移：

[WRITEBACK_2：previous_read 移位流水（[0]<=当前读，[1]<=[0]）](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L789-L796)

```verilog
//keep the current read in a buffer before putting it in the convolution buffer
//because we read data 2 by 2 and we need 3x3 matrices
convolution_previous_read[0] <= data_read_store;
...
convolution_previous_read[1] <= convolution_previous_read[0];
```

每次写回都执行这条移位，于是 `previous_read` 始终保持着「上一次」与「上上次」的读，供下一次填表使用。数据从这里再经过一拍才被 `WRITEBACK_1/2` 写进 `convolution_buffer`（写成两行历史），形成完整的「主存 → previous_read → convolution_buffer」三级流水。

#### 4.2.4 代码实践

**实践目标**：亲手验证「每拍读 2 像素、邻域按 3 对齐 → 必须借 previous_read」这条因果链。

**操作步骤**：

1. 假设没有 `convolution_previous_read`，每拍只能看到当前的 `data_read`（2 个像素：列 \(c\)、\(c+1\)）。
2. 问自己：要凑出当前行的 4 个连续列 \(c-3..c\)，光靠这 2 个像素够吗？
3. 现在把 `previous_read[0]`（再前 2 像素：\(c-2,c-1\)）、`previous_read[1]`（再前 2 像素：\(c-4,c-3\)）加上，数一下可用像素数与所需列数。
4. 对照第 668–671 行的取字节顺序，确认 4 个列 \(c-3,c-2,c-1,c\) 分别来自哪个寄存器的哪个字节。

**需要观察的现象 / 预期结果**：

- 仅当前读：只有 2 像素，无法覆盖 4 列窗 → **不够**。
- 加 `previous_read[0]`：共 4 像素（\(c-2,c-1,c,c+1\)），仍缺最左的列 \(c-3\) → **仍不够**。
- 再加 `previous_read[1]`：共 6 像素（\(c-4..c+1\)），足以挑出 \(c-3..c\) 这 4 列 → **刚好够，且 exactly 用到 [1] 的高字节、[0] 的低/高字节、当前读的低字节**。

这就从「需要 4 列、每读 2 列」推出了「必须记 2 级历史」的必然性。注：列 \(c-4\) 与 \(c+1\) 这两个像素本次填表用不到，它们是移位带来的「附赠」，分别会参与更早或更晚的窗。

#### 4.2.5 小练习与答案

**练习 1**：为什么局部矩阵是 **4 列** 而不是 3 列？

> **答**：因为一个字含左右两像素，左像素窗用列 \(c-1,c,c+1\)，右像素窗用列 \(c,c+1,c+2\)，两窗合并需要连续 4 列。若只存 3 列，右像素就缺了 \(c+2\) 列，无法在同一拍里和左像素一起算。

**练习 2**：填表那一拍（658–671 行）一次性读了 `convolution_buffer` 8 个单元，这对综合成 RAM 友好吗？

> **答**：这是一种刻意的取舍。把所有对行缓冲的读集中到「填表」一拍、之后 9 拍计算只读 `convolution_buffer_local` 这个普通寄存器矩阵，就避免在乘加循环里反复读行缓冲。代码注释 `(will infer the sprams! (yosys 0.9))` 表明作者在 yosys 0.9 上验证过这种写法能被推断成 SPRAM 块。具体推断结果与所用 yosys 版本有关，待本地综合确认。

---

### 4.3 左右双像素并行累加 calc_left_buf / calc_right_buf

#### 4.3.1 概念说明

填好 4×3 局部矩阵后，`STATE_PROC_CONVOLUTION_CALCULATION` 用 10 拍（`matrix_convolution_counter` 从 0 数到 9）把 9 个核系数各自乘一个邻域像素并累加。关键技巧：**左右两个像素的 9 次乘加在这 10 拍里交错并行**，而不是先算完左再算右。

- `calc_left_buf`：左像素累加器。用核 `convolution_matrix[0..8]` 乘局部矩阵的 **左 3 列**（`local[0..2][0..2]`）。
- `calc_right_buf`：右像素累加器。用同一组核乘 **右 3 列**（`local[1..3][0..2]`），整体比左像素错开一列。

为了共享同一个 `matrix_convolution_counter` 节拍，右像素的乘加比左像素 **延迟一拍启动**：counter=1 时右像素才做它的第 1 次乘加（用 `matrix[0]`），而左像素在 counter=0 就做了第 1 次。到 counter=9 时两者都正好算完 9 次。这就是「错开一列」在时序上的对应——左右窗共享中间两列，于是共享大部分乘加节拍，只各补一个端点。

#### 4.3.2 核心流程

左像素累加（counter 0→9）：

| counter | 乘加 | 写回 |
|---|---|---|
| 0 | `matrix[0]*local[0][0]` | `calc_left_buf` |
| 1 | `+matrix[1]*local[1][0]` | `calc_left_buf` |
| 2 | `+matrix[2]*local[2][0]` | `calc_left_buf` |
| 3 | `+matrix[3]*local[0][1]` | `calc_left_buf` |
| 4 | `+matrix[4]*local[1][1]` | `calc_left_buf` |
| 5 | `+matrix[5]*local[2][1]` | `calc_left_buf` |
| 6 | `+matrix[6]*local[0][2]` | `calc_left_buf` |
| 7 | `+matrix[7]*local[1][2]` | `calc_left_buf` |
| 8 | `+matrix[8]*local[2][2]` | `calc_left_buf` |
| 9 | （空转，取最终值） | 送钳位 |

右像素累加（counter 1→9，少一个起点、补一个终点）：

| counter | 乘加 | 写回 |
|---|---|---|
| 1 | `matrix[0]*local[1][0]` | `calc_right_buf` |
| 2 | `+matrix[1]*local[2][0]` | `calc_right_buf` |
| 3 | `+matrix[2]*local[3][0]` | `calc_right_buf` |
| 4 | `+matrix[3]*local[1][1]` | `calc_right_buf` |
| 5 | `+matrix[4]*local[2][1]` | `calc_right_buf` |
| 6 | `+matrix[5]*local[3][1]` | `calc_right_buf` |
| 7 | `+matrix[6]*local[1][2]` | `calc_right_buf` |
| 8 | `+matrix[7]*local[2][2]` | `calc_right_buf` |
| 9 | `+matrix[8]*local[3][2]` | （终值） |

两表对照可见：同一拍里左、右各做一次乘加，用的核系数相同，只是右像素的列索引整体 +1。10 拍完成后，两像素结果分别钳位（`apply_clamp_fixed16` 取 `[11:4]` 还原定点）、再叠加 `convolution_data_to_add`（即 `add_to_result` 参数控制的原值或 0），写入 `data_write[7:0]` 与 `data_write[15:8]`。

#### 4.3.3 源码精读

左像素累加循环：

[CALCULATION：左像素 9 拍乘加（calc_left_buf，用局部矩阵左 3 列）](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L684-L725)

```verilog
if(matrix_convolution_counter == 0) begin
   temp_calc = temp_calc + convolution_matrix[0]*{8'b0, convolution_buffer_local[0][0]};
   calc_left_buf <= temp_calc;
end else if (matrix_convolution_counter == 1) begin
   temp_calc = calc_left_buf;
   temp_calc = temp_calc + convolution_matrix[1]*{8'b0, convolution_buffer_local[1][0]};
   calc_left_buf <= temp_calc;
end
... // 直到 counter==8 累加完 matrix[8]*local[2][2]
else if (matrix_convolution_counter == 9) begin
   temp_calc = calc_left_buf;          // 取最终累加值
end
temp_calc[7:0] = apply_clamp_fixed16(temp_calc, clamp);   // 定点还原 + 钳位
data_write[7:0] <= apply_clamp({8'b0, convolution_data_to_add[7:0]}+{8'b0, temp_calc[7:0]}, 1);
```

右像素累加循环（注意从 counter==1 起步）：

[CALCULATION：右像素 9 拍乘加（calc_right_buf，用局部矩阵右 3 列，错开一列）](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L736-L773)

```verilog
//starts at 1 because want to couple similar operations with the first byte calculation
if(matrix_convolution_counter == 1) begin
   temp_calc = temp_calc + convolution_matrix[0]*{8'b0, convolution_buffer_local[1][0]};
   calc_right_buf <= temp_calc;
end else if (matrix_convolution_counter == 2) begin
   temp_calc = calc_right_buf;
   temp_calc = temp_calc + convolution_matrix[1]*{8'b0, convolution_buffer_local[2][0]};
   ...
```

节拍推进与结束判断：到 counter==9 进入写回，否则 counter+1。

[节拍推进：counter==9 进 WRITEBACK_1，否则继续累加](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L776-L780)

```verilog
if(matrix_convolution_counter == 9) begin
   state_processing <= STATE_PROC_CONVOLUTION_WRITEBACK_1;
end else begin
   matrix_convolution_counter <= matrix_convolution_counter + 1;
end
```

#### 4.3.4 代码实践

**实践目标**：确认左右两像素「同一拍、同系数、列错一」的并行关系。

**操作步骤**：

1. 打开 [左像素累加](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L684-L721) 与 [右像素累加](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L736-L770)。
2. 对每个 `matrix_convolution_counter` 值（1..8），把左、右两表里的「核系数」和「local 列下标」并列抄出。
3. 检查：同一 counter 下，左右用的核系数是否相同？右像素的列下标是否恰好比左像素 +1？

**需要观察的现象 / 预期结果**：以 counter=3 为例——左像素 `matrix[3]*local[0][1]`，右像素 `matrix[2]*local[3][0]`。看起来「同 counter 不同系数」会让人疑惑，但把两表按各自第几次乘加对齐（左第 4 次、右第 3 次）后可看出：**两者的第 k 次乘加都用 `matrix[k-1]`，且右像素的列下标 = 左像素列下标 +1**。例如第 1 次：左 `matrix[0]*local[0][0]` vs 右 `matrix[0]*local[1][0]`（列 0→1）；第 4 次：左 `matrix[3]*local[0][1]` vs 右 `matrix[3]*local[1][1]`（列 0→1）。错开一列一目了然。

#### 4.3.5 小练习与答案

**练习 1**：右像素累加为什么从 `counter==1` 开始而不是 `counter==0`？

> **答**：为了让左右两路共享同一个 `matrix_convolution_counter` 节拍器。右像素的窗比左像素整体右移一列，相当于「晚一个列单元到位」，于是它的第 1 次乘加安排在 counter==1 拍，与左像素在 counter==0 的第 1 次乘加错开一拍；最终右像素在 counter==9 补上它的第 9 次乘加（左像素在 counter==9 空转），两人同时算完。

**练习 2**：`calc_left_buf` 与 `calc_right_buf` 是 16 位宽，但像素只有 8 位、核是 8 位符号定点。为什么用 16 位累加器？

> **答**：8 位无符号像素 × 8 位有符号核 = 最多 16 位的乘积，9 次累加再加 `convolution_data_to_add` 还会增长。16 位够容纳 1.3.4 定点下 9 项加权和中度溢出，最后由 `apply_clamp_fixed16` 取 `[11:4]`（右移 4 位还原定点）并饱和到 0..255。这与 u4-l2 讲的定点还原一致。

---

### 4.4 读/写计数器偏移与边界像素置 0

#### 4.4.1 概念说明

卷积有两套坐标：**读坐标**（`counter_convolution_x / _y`，跟踪「主存读到了哪」）和 **写坐标**（`counter_convolution_x_write / _y_write`，跟踪「正在算的是哪个像素、结果写回哪」）。它们不重合，因为：

- 中心像素的卷积窗需要它的 **右邻列** 和 **下一行**，这些数据必须在算之前先读到；
- 加上行缓冲要「先填两行才能出第一个有效结果」。

所以 **读指针永远跑在写指针前面**——代码注释 `since we have to read in advance, there is a slight offset between the read and the write` 说的就是这件事。读坐标每个写回周期都推进；写坐标只在「真正写出结果」时才推进（第一行不写结果，所以写坐标先原地等）。

边界处理则简单粗暴：3×3 窗在图像边缘会越界（缺邻居），项目不补 0 也不镜像，而是 **把边界像素的卷积结果直接置 0**。具体由两条 `if` 分别判断左右像素的越界条件。

#### 4.4.2 核心流程

每个完整卷积周期（CONV → CALC → WB1 → WB2）里，`WRITEBACK_2` 推进坐标：

1. **读坐标**总是推进：`x += 2`，到行宽则 `x=0; y+=1`（每周期读 2 像素）。
2. **写坐标**仅在「非首行」推进：第一行（`y==0`，或 `y==1 && x==0` 的起始）只填行缓冲、不写结果（`wr_en=0`），所以写坐标原地不动，造就读超前的偏移。
3. **结束条件**：读坐标读到 `y >= img_height+1`（多读一行冲刷流水线），运算 FSM 回 `STATE_IDLE`。
4. **边界置 0**：
   - 左像素（写回 `data_write[7:0]`）：`y_write==0`（顶行）或 `y_write>=img_height-1`（底行）或 `x_write==0`（最左列）→ 置 0。
   - 右像素（写回 `data_write[15:8]`）：同样顶/底行条件，或 `x_write>=img_width-2`（右像素在最右列，因为右像素列 = `x_write+1`，当 `x_write+1==img_width-1` 即 `x_write==img_width-2`）→ 置 0。

注意 `x_write` 步长为 2，取值 0, 2, 4, …, `img_width-2`。所以 `x_write==0` 命中最左列，`x_write==img_width-2` 命中最右列，正好把左右两条边界都覆盖。

#### 4.4.3 源码精读

读/写两套坐标的声明与「读超前」注释：

[声明：读坐标 counter_convolution_x/y 与写坐标 _write（注释点明读超前偏移）](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L111-L116)

```verilog
reg [15:0] counter_convolution_x;
reg [15:0] counter_convolution_y;
//since we have to read in advance, there is a slight offset between the read and write
reg [15:0] counter_convolution_x_write;
reg [15:0] counter_convolution_y_write;
```

读坐标推进（每个写回周期都走）：

[WRITEBACK_2：读坐标 x+=2、到宽换行](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L801-L807)

```verilog
if(counter_convolution_x+2 >= img_width) begin
   counter_convolution_x <= 0;
   counter_convolution_y <= counter_convolution_y + 1;
end else begin
   counter_convolution_x <= counter_convolution_x + 2;
end
```

首行不写结果（写坐标原地等待，造就读超前）：

[WRITEBACK_2：第一行只填缓冲不写结果（wr_en=0）](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L809-L825)

```verilog
if(counter_convolution_y == 0 || (counter_convolution_y == 1 && counter_convolution_x == 0)) begin
   //first line not written back, need delay to fill the convolution buffer
   wr_en <= 0;
end else begin
   wr_en <= 1;
   proc_conv_memory_addr_write <= proc_conv_memory_addr_write + 2;
   //only update write counter when there is an actual write
   ...counter_convolution_x_write / _y_write 推进...
end
```

边界置 0 的两条判断（注意右像素的列条件是 `img_width-2`）：

[CALCULATION：左像素边界置 0（顶/底行或最左列）](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L678-L680)

```verilog
if(counter_convolution_y_write == 0 || counter_convolution_y_write >= img_height-1 || counter_convolution_x_write == 0) begin
   data_write[7:0] <= 0;
end
```

[CALCULATION：右像素边界置 0（顶/底行或最右列）](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L729-L731)

```verilog
if(counter_convolution_y_write == 0 || counter_convolution_y_write >= img_height-1 || counter_convolution_x_write >= img_width-2) begin
   data_write[15:8] <= 0;
end
```

结束条件（多读一行冲刷）：

[WRITEBACK_2：读坐标越过 img_height+1 才结束](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L834-L838)

```verilog
if(counter_convolution_y >= img_height+1 && counter_convolution_x+2 >= img_width)begin
   state_processing <= STATE_IDLE;
end else begin
   state_processing <= STATE_PROC_CONVOLUTION;
end
```

#### 4.4.4 代码实践

**实践目标**：验证读/写坐标的偏移与边界置 0 的覆盖性。

**操作步骤**：

1. 取一幅 8×4 的小图（`img_width=8, img_height=4`），列出 `x_write` 的所有取值（步长 2）。
2. 对照第 678、729 两行边界条件，标出每个 `x_write` 下，左像素（列 `x_write`）与右像素（列 `x_write+1`）是否被置 0。
3. 对照第 809 行首行条件，说明 `y_write==0` 时为何 `wr_en=0`、写坐标不推进，从而读坐标如何超前。

**需要观察的现象 / 预期结果**：

- `x_write` 取值：0, 2, 4, 6。
- 最左列：`x_write==0` → 左像素置 0（右像素列 1，不置 0）。
- 最右列：`x_write==6==img_width-2` → 右像素置 0（右像素列 7 = `img_width-1`，最右列）。
- 顶行 `y_write==0`、底行 `y_write>=3==img_height-1` → 左右都置 0。
- 于是图像的最外一圈像素全为 0，内部像素才有卷积结果——这正是用 `run_gnuplot.sh` 看卷积输出时常见的「黑边」来源。
- 首行 `y_write==0` 不写结果，读坐标却继续走，等到能写出第一行有效结果时，读坐标已领先若干行；这种「先填管子再出水」的延迟正是流水线的代价。（数值结论为源码阅读推导，待本地仿真确认。）

#### 4.4.5 小练习与答案

**练习 1**：为什么读坐标要读到 `img_height+1`，比图像高度多一行？

> **答**：因为算最后一行（`y = img_height-1`）的卷积窗需要它的下一行（`y = img_height`）作为「当前流进行」。这一行数据得先读进来才能完成最后一行结果的计算。多读的那一行让流水线把尾部结果冲刷出来。注意底行结果随后会被边界条件置 0，但冲刷过程不能省。

**练习 2**：若不区分读、写两套坐标，统一只用一个坐标，会出什么问题？

> **答**：写结果需要的数据（右邻列、下一行）还没读到，就会算出错误结果或读到无效数据。读超前的本质是「卷积窗的因果性」——中心像素的邻居必须先于计算到位。把读、写坐标解耦，正是为了让读指针自由地跑到前面去取数，写指针则按结果就绪的节奏慢慢跟进。

---

## 5. 综合实践

把本讲四个模块串起来，做一次「纸上演练」：以一个 6×4 的小图、核为均值（9 个系数都是 `(1)<<4`，即定点 1.0，等价于 3×3 平均）为例，手动跟踪一次卷积周期。

1. **画出存储布局**：每行 6 像素 → 每行 3 个 16 位字。给每行标上像素值（自拟几个 0..255 的数）。
2. **跟踪一个写回周期**：从 `STATE_PROC_CONVOLUTION` 读入某个字 \(W_n\)（含列 \(c,c+1\)）开始，写出：
   - `data_read`、`previous_read[0]`、`previous_read[1]` 各自对应哪些列；
   - 填进 `convolution_buffer_local` 的 4×3 矩阵（上两行来自 `convolution_buffer`，下一行来自三次读）；
   - `calc_left_buf` / `calc_right_buf` 在 counter 0..9 各加哪一项；
   - 最终 `data_write[7:0]` / `[15:8]` 写回哪两列、是否被边界条件置 0。
3. **跟踪 `previous_read` 的移位**：在 `WRITEBACK_2` 后，写出 `previous_read[0]`、`[1]` 的新内容，确认它们为下一个周期备好了「上一次」「上上次」的读。
4. **回答**：若把均值核换成 u5-l3 的锐化核（中心 5、四邻 -1），上述哪几步会变、哪几步不变？

**预期**：只有第 2 步里「核系数」与累加值会变（系数来自 `convolution_matrix`），填表、移位、坐标推进、边界置 0 的机制全部不变——这说明行缓冲与计算流水线是 **与核无关** 的通用框架，换核只是换 9 个常数。这正是后续 u5-l3 能「只换核就做高斯模糊 / 边缘检测」的底层原因。（本任务为源码阅读 + 手算，待本地用 `test_gaussian_blur` 仿真输出对照。）

---

## 6. 本讲小结

- `convolution_buffer` 是 **两行历史** 的行缓冲，第二维 `[0:1]` 用行号奇偶 ping-pong；它被刻意拆成 `WRITEBACK_1` + `WRITEBACK_2` 两拍写，是为了让综合器把它推断成 **单端口 SPRAM**（一拍一写）。
- 「第 3 行」不需要存缓冲——它就是当前正从主存流式读入的那一行。
- 局部矩阵 `convolution_buffer_local` 是 **4 列 × 3 行**，因为一个字含左右两像素，左像素窗（列 c-1,c,c+1）与右像素窗（列 c,c+1,c+2）合并需连续 4 列。
- 每拍只读 2 像素、邻域按奇数 3 对齐 → 必须用 `convolution_previous_read` 2 级移位寄存器记住「上两次读」，与当前读拼出当前行的连续 4 列；`WRITEBACK_2` 的 `[1] <= [0]` 推进这条流水。
- `calc_left_buf` / `calc_right_buf` 在 10 拍内交错并行：同一拍、同核系数，右像素列下标比左像素 +1；右像素从 counter==1 起步、在 counter==9 收尾，与左像素同时算完。
- 读坐标（`counter_convolution_x/_y`）永远超前写坐标（`..._write`），因为卷积窗的右邻列与下一行必须先读到；首行不写结果，造就偏移。
- 边界像素（顶/底行、最左/最右列）的卷积结果直接置 0，表现为输出图像的黑边。

---

## 7. 下一步学习建议

本讲搞定了卷积的 **数据通路**（怎么凑邻域、怎么并行算），但还没有把这些机制和「具体核」挂钩。建议接着学：

- **u5-l3 卷积核实践：高斯模糊与边缘检测**：结合 `software/main.cpp` 的 `test_gaussian_blur` 与 `test_simple_edge_detection`，看主机如何用 `(1)<<4` 这类位移构造定点核、如何用 `add_to_result` 把多个方向梯度核累加成一张边缘图。读完它你会回头发现：本讲的行缓冲 / 双累加器对任何核都通用，换核只是换 `convolution_matrix` 的 9 个常数。
- 想从端到端验证本讲结论，可在仿真模式（u1-l3 的 `build_simulation.sh`）下跑一次高斯模糊，用 `run_gnuplot.sh` 观察输出图的「黑边」与内部平滑效果，与本讲 4.4 的边界置 0 预测对照。
- 若关心 SPRAM 推断与硬件资源，可读 u6-l2（iCE40 硬件顶层与 SPRAM 接口），看 `convolution_buffer` 在真实芯片上映射到哪类资源。

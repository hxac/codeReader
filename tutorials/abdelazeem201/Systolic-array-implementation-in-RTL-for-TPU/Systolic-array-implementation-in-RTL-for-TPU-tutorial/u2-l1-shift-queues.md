# 数据与权重的移位队列（weight_queue / data_queue）

> 本讲属于「脉动阵列数据通路」单元（u2）的第一讲。它把上一单元建立的顶层框图（u1-l3）与定点/参数知识（u1-l4）落到了 `systolic.v` 内部最基础的一层：**数据是怎么进阵列、又是怎么一格一格流动的**。

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 `systolic.v` 里 `weight_queue` 与 `data_queue` 这两个二维寄存器阵列的维度、位宽和存储开销。
- 解释 SRAM 读出的 32bit 数据是如何被切成 8 个 8bit 字节，分别喂进阵列的**第 0 行**（weight）和**第 0 列**（data）。
- 看懂「weight 自上而下、data 自左而右」这段循环移位代码，并能用非阻塞赋值的时序规则推出某个 cell 在某周期接收到的数据来自哪里。
- 理解「移位」为何能制造出脉动阵列必需的**时间错位（skew）**，为下一讲（u2-l2 的 MAC 乘加）做好准备。

## 2. 前置知识

在进入源码前，先用最直白的方式建立两个直觉。

**直觉一：脉动阵列靠「数据流动」而不是「数据广播」工作。**
普通 CPU 算矩阵乘时，每个元素都要从寄存器堆/存储里反复取出来。脉动阵列的做法不同：把 weight 和 data 装进一排排**移位寄存器**，让它们像传送带上的零件一样，每个时钟节拍**只移动一格**，每到一个处理单元（cell）就顺便做一次乘加。这样数据一旦进入阵列，就能被沿途的多个 cell 反复复用，访存带宽需求被压到极低。本项目里，weight 像瀑布一样**从上往下流**，data 像流水线一样**从左往右流**，两者在每个 cell 的交叉点上相遇并做乘加（MAC）——这正是 u1-l1 里确立的「宪法级」数据流方向。

**直觉二：Verilog 的非阻塞赋值（`<=`）是理解移位的关键。**
在一个 `always@(posedge clk)` 块里，所有 `<=` 的右边（RHS）**统一读取本拍开始前的旧值**，左边（LHS）**统一在本拍结束时更新为新值**。所以下面这两行可以「同时」执行而互不干扰：

```verilog
weight_queue[0][j] <= sram_rdata_w0[...];   // 第 0 行吃 SRAM 的新数据
weight_queue[1][j] <= weight_queue[0][j];   // 第 1 行吃第 0 行的【旧】数据
```

第 1 行拿到的是第 0 行**更新前**的值，而不是刚吃进去的新值。这正是「数据下移一格」能正确实现的根本原因。如果你对这一点还不熟，建议先在纸上把这两行的「旧值→新值」表画一遍再继续。

**位宽小抄**（来自 u1-l4）：输入 `DATA_WIDTH=8`（8 位有符号定点 Q4.4）；SRAM 一次读出 `SRAM_DATA_WIDTH=32`bit，恰好装 4 个 8bit 字节；阵列规模 `ARRAY_SIZE=8`。

## 3. 本讲源码地图

本讲只聚焦一个文件，但会从顶层顺带确认数据从哪里来。

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `rtl/systolic.v` | 8×8 脉动阵列本体：移位队列 + MAC + 结果收集 | **全部重点**：队列声明、第 0 行/列填入、移位循环 |
| `rtl/tpu_top.v` | 纯结构化顶层，把外部 SRAM 读数据接到 `systolic` | 确认 `sram_rdata_w0/w1/d0/d1` 的来源 |

> 提醒（来自 u1-l2）：`rtl/` 下的核心模块与 `Pre-Synthesis_Simulation/` 下的仿真副本逐字节相同；综合脚本 `syn.tcl` 也以 `../rtl/systolic.v` 为输入。所以本讲读 `rtl/systolic.v` 等同于读整个项目的权威实现。

## 4. 核心概念与源码讲解

本讲把 `systolic.v` 的「数据搬运」部分拆成三个最小模块：

1. **移位队列的声明与存储结构**——这两个二维数组到底是什么。
2. **第 0 行 / 第 0 列的填入逻辑**——32bit SRAM 数据怎么变成 8 个字节进入阵列边界。
3. **移位 for 循环**——进入边界之后，数据怎么一格一格地往阵列内部传。

### 4.1 移位队列的声明与存储结构

#### 4.1.1 概念说明

`weight_queue` 和 `data_queue` 是两个**二维寄存器阵列**，可以想象成两张铺在 8×8 阵列上的「透明薄膜」：

- `weight_queue[i][j]` 存放**当前周期**停留在 cell(i,j) 的那个 weight 字节。
- `data_queue[i][j]` 存放**当前周期**停留在 cell(i,j) 的那个 data 字节。

每个 cell 在做 MAC 时（下一讲 u2-l2），用的就是这两个数组在同一个 `(i,j)` 位置上的值：`weight_queue[i][j] * data_queue[i][j]`。所以这两个数组就是 cell 的「本地存储」——对应 u1-l1 里提到的每个 cell 的 **weight 寄存器**和 **data 寄存器**。（ALU 累加寄存器则是另一组数组 `matrix_mul_2D`，不在本讲范围。）

它们都是 `signed`（有符号），因为输入是 Q4.4 的有符号定点数。

#### 4.1.2 核心流程

从存储视角看，这两个队列的生命周期是：

1. **复位**：`srstn=0` 时，全部 64 个 cell 的两个字节都清零。
2. **工作**：`alu_start=1` 的每个时钟沿，边界 cell 从 SRAM 吃新字节，内部 cell 从邻居吃旧字节。
3. **静默**：`alu_start=0` 时，队列保持不动（不移位、也不吃新数据）。

存储开销：每个队列是 `8 × 8 × 8bit = 512bit`，两个队列合计 **1024bit** 的触发器。这在综合报告里会体现为一部分 flip-flop 面积。

#### 4.1.3 源码精读

先看端口：`systolic` 模块把 4 路 SRAM 读数据作为输入接进来（这两路 weight、两路 data 的命名会在 4.2 节用到）：

[rtl/systolic.v:L16-L20](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L16-L20) —— 声明 `sram_rdata_w0/w1`（weight）和 `sram_rdata_d0/d1`（data）四路 32bit 输入。

再看两个队列本身的声明：

[rtl/systolic.v:L32-L33](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L32-L33) —— `data_queue` 与 `weight_queue`，都是 `[0:7][0:7]` 的二维数组，元素为 `signed [7:0]`。

```verilog
reg signed [DATA_WIDTH-1:0] data_queue   [0:ARRAY_SIZE-1] [0:ARRAY_SIZE-1];
reg signed [DATA_WIDTH-1:0] weight_queue [0:ARRAY_SIZE-1] [0:ARRAY_SIZE-1];
```

两个要点：

- 第一个下标 `i` 是**行号**（自上而下 0→7），第二个下标 `j` 是**列号**（自左而右 0→7）。这个约定贯穿整份代码，记住它后面看移位循环会轻松很多。
- 数组维度用了参数 `ARRAY_SIZE`，元素位宽用了参数 `DATA_WIDTH`。位宽会随参数变化，但**填入逻辑里写死了 `i<4` 和 `i+4`**（见 4.2 节），所以只有 `ARRAY_SIZE=8` 时才能正确工作——这是 u1-l4 里讲过的「半参数化」特征，这里不展开。

#### 4.1.4 代码实践

**目标**：建立对队列存储规模的直觉，并学会在仿真里把它们「打印」出来。

**操作步骤**：

1. 在 `rtl/systolic.v` 第 33 行附近，确认两个队列都是 8×8、每个元素 8bit。
2. 心算（或用计算器）算出：单个队列 = 8×8×8 = 512 bit，两个队列 = 1024 bit。
3. （可选，源码阅读型）在仿真环境的 `test_tpu.v` 里，于 `tpu_start` 拉高若干拍后，给 `systolic` 实例加一段：
   ```verilog
   // 示例代码：调试用，非项目原有代码
   for(i=0;i<8;i=i+1) $display("wq[%0d][3]=%h dq[%0d][3]=%h",i, dut.weight_queue[i][3], i, dut.data_queue[i][3]);
   ```
   注意：因为 `weight_queue` 是模块内部 `reg`，部分仿真器需要层次化路径（如 `dut.systolic_inst.weight_queue[...]`）才能访问，具体实例名待本地确认。

**需要观察的现象**：复位期间打印值应全为 0；`alu_start` 期间，同一列 `weight_queue[i][3]` 在连续两拍之间应呈现「上邻格的旧值」下移的效果。

**预期结果**：你能说出一句话——「每个队列就是 64 个 8bit 触发器，weight 与 data 各占一份」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `ARRAY_SIZE` 改成 16（且假设填入逻辑也相应改对），单个 weight_queue 需要多少 bit？
**答**：16×16×8 = 2048 bit。

**练习 2**：`weight_queue` 和 `data_queue` 为什么必须声明成 `signed`？
**答**：因为输入是 Q4.4 有符号定点数，cell 做乘加时要按有符号数处理（符号扩展 `{5{mul_result[15]}}` 也依赖乘积的最高位为符号位）；若声明成无符号，负数会被当成大正数，结果全错。

---

### 4.2 第 0 行 / 第 0 列的填入逻辑（32bit → 8bit 切片）

#### 4.2.1 概念说明

阵列的**边界**是数据进入的「闸口」：

- **weight 从顶部进入**：每个时钟沿，把 8 个 weight 字节灌进**第 0 行**的 8 个 cell（`weight_queue[0][0..7]`）。
- **data 从左侧进入**：每个时钟沿，把 8 个 data 字节灌进**第 0 列**的 8 个 cell（`data_queue[0..7][0]`）。

问题在于：外部 SRAM 一次只吐 32bit（4 个字节），而边界一排需要 8 个字节。所以本项目用了**两路 SRAM**拼出 8 个字节：

- weight：`w0` 提供第 0 行的左半边（列 0–3），`w1` 提供右半边（列 4–7）。
- data：`d0` 提供第 0 列的上半边（行 0–3），`d1` 提供下半边（行 4–7）。

这就是 u1-l3 里那个关键澄清的落点：所谓「eight SRAM」其实是 **2 路 × 4 字节通道 = 8 个字节通道**，物理读端口只有 4 个（w0/w1/d0/d1）。

#### 4.2.2 核心流程

把一个 32bit SRAM 字拆成 4 个 8bit 字节，用的是 Verilog 的**变基部分选择（indexed part-select）**语法 `[BASE -: N]`：表示从 `BASE` 开始向低位取 `N` 位，即 `[BASE : BASE-N+1]`。

代码里 `BASE = 31-8*i`，对 `i=0,1,2,3` 得到 31、23、15、7，于是 4 个字节分别是：

| `i` | 切片 `[31-8*i -: 8]` | 实际位段 | 对应字节 |
| --- | --- | --- | --- |
| 0 | `[31 -: 8]` | `[31:24]` | 最高字节 |
| 1 | `[23 -: 8]` | `[23:16]` | 次高字节 |
| 2 | `[15 -: 8]` | `[15:8]` | 次低字节 |
| 3 | `[7 -: 8]` | `[7:0]` | 最低字节 |

于是填入映射为（**建议你在脑子里把这张表记住**，后面跟踪数据流时直接用）：

- `weight_queue[0][0] ← w0[31:24]`，`[0][1] ← w0[23:16]`，`[0][2] ← w0[15:8]`，`[0][3] ← w0[7:0]`
- `weight_queue[0][4] ← w1[31:24]`，`[0][5] ← w1[23:16]`，`[0][6] ← w1[15:8]`，`[0][7] ← w1[7:0]`
- `data_queue[0][0] ← d0[31:24]`，`[1][0] ← d0[23:16]`，`[2][0] ← d0[15:8]`，`[3][0] ← d0[7:0]`
- `data_queue[4][0] ← d1[31:24]`，`[5][0] ← d1[23:16]`，`[6][0] ← d1[15:8]`，`[7][0] ← d1[7:0]`

#### 4.2.3 源码精读

填入逻辑全部在同一个 `always@(posedge clk)` 块里，且被 `alu_start` 门控：

[rtl/systolic.v:L44-L54](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L44-L54) —— 复位清零，以及 `alu_start` 才允许移位/填入的总开关。

weight 第 0 行的填入：

[rtl/systolic.v:L56-L59](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L56-L59) —— `i` 从 0 到 3，`w0` 填列 0–3、`w1` 填列 4–7。

```verilog
for(i=0; i<4; i=i+1) begin
    weight_queue[0][i]   <= sram_rdata_w0[31-8*i-:8];
    weight_queue[0][i+4] <= sram_rdata_w1[31-8*i-:8];
end
```

data 第 0 列的填入：

[rtl/systolic.v:L66-L69](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L66-L69) —— `d0` 填行 0–3、`d1` 填行 4–7。

```verilog
for(i=0; i<4; i=i+1) begin
    data_queue[i][0]   <= sram_rdata_d0[31-8*i-:8];
    data_queue[i+4][0] <= sram_rdata_d1[31-8*i-:8];
end
```

两段对照看，会发现一个**对称美**：weight 按「行固定=0、列变化」填，data 按「列固定=0、行变化」填，正好一个走顶部边、一个走左侧边。注意这里的 `i<4` 和 `i+4` 是**写死的**——它假设 SRAM 字宽是 32bit、字节宽是 8bit、阵列是 8×8。这也再次印证了「半参数化」：改 `ARRAY_SIZE` 而不改这里，边界只有一半 cell 会被填入。

#### 4.2.4 代码实践

**目标**：把 SRAM 字节到边界 cell 的映射彻底内化。

**操作步骤**：

1. 假设某拍 SRAM 读出 `sram_rdata_w0 = 32'hAA_BB_CC_DD`、`sram_rdata_w1 = 32'h11_22_33_44`（十六进制，每两位是一个字节）。
2. 依据 4.2.2 的映射表，写出本拍结束后 `weight_queue[0][0..7]` 的值。
3. 同理对 `sram_rdata_d0 = 32'h01_02_03_04`、`sram_rdata_d1 = 32'h05_06_07_08`，写出 `data_queue[0..7][0]` 的值。

**预期结果**：

- `weight_queue[0]` 依次为 `AA BB CC DD 11 22 33 44`（列 0→7）。
- `data_queue[*][0]` 依次为 `01 02 03 04 05 06 07 08`（行 0→7）。

**需要观察的现象**：最高字节（如 `AA`、`01`）总是落在下标最小的位置（列 0 / 行 0）。如果你算出来 `DD` 落在列 0，说明把位段方向搞反了，回去重看 `[31-:8]` 的定义。

#### 4.2.5 小练习与答案

**练习 1**：为什么 weight 用两路（w0/w1），而不是用一路 64bit 的 SRAM？
**答**：这是工程取舍。SRAM 数据宽度通常做成 32bit（与总线/存储器宽度匹配），用两路 32bit 拼出 8 个字节，既满足一排 8 个 cell 的需求，又复用了标准 32bit SRAM 宏单元。

**练习 2**：若把 `sram_rdata_w0[31-8*i-:8]` 改成 `sram_rdata_w0[8*i+:8]`，映射会怎样变化？
**答**：会变成 `i=0→[7:0]`、`i=1→[15:8]`、…，即最低字节进列 0。字节顺序整体反过来，weight 列与原设计的地址排布不再对应，会导致 weight 与 data 在 cell 里「对不上」，结果错误。这也说明字节顺序是和 testbench 里 `char2sram` 的打包方式严格约定的。

---

### 4.3 移位 for 循环（weight 向下、data 向右）

#### 4.3.1 概念说明

边界 cell 吃进数据之后，下一个时钟沿，这些数据要**往阵列内部传一格**：

- **weight 下移**：cell(i,j) 的 weight 来自它**正上方**的 cell(i−1,j)，即整列同步下移一格。
- **data 右移**：cell(i,j) 的 data 来自它**正左方**的 cell(i,j−1)，即整行同步右移一格。

这种「每个时钟只走一格」的移动，正是脉动阵列（systolic = 像心脏收缩一样节拍式流动）得名的原因。它的核心好处是：**通信只发生在相邻 cell 之间**，连线极短、可高频、可扩展。

#### 4.3.2 核心流程

用伪代码描述两个移位循环（均被 `alu_start` 门控，复位时清零）：

```
每个 posedge clk（且 alu_start=1）：
  // weight：第 0 行由 SRAM 填入（见 4.2），其余各行下移
  for i = 1 .. 7:
      for j = 0 .. 7:
          weight_queue[i][j] <= weight_queue[i-1][j]   // 吃上方邻居的旧值

  // data：第 0 列由 SRAM 填入（见 4.2），其余各列右移
  for i = 0 .. 7:
      for j = 1 .. 7:
          data_queue[i][j] <= data_queue[i][j-1]       // 吃左方邻居的旧值
```

注意两个循环的**边界处理**正好和填入逻辑互补：

- weight 循环从 `i=1` 开始——因为 `i=0`（第 0 行）由 4.2 节的 SRAM 填入负责，不能被覆盖。
- data 循环从 `j=1` 开始——因为 `j=0`（第 0 列）由 4.2 节的 SRAM 填入负责。

两者写在**同一个 `always` 块**里，靠非阻塞赋值保证「下移」和「右移」用的是旧值，互不干扰。

**时间错位（skew）的含义**：因为 weight 要走 `i` 拍才到第 i 行、data 要走 `j` 拍才到第 j 列，所以同一拍从边界灌进去的 8 个 weight 与 8 个 data，**不会**在同一拍于任意 cell 同时相遇——它们到达 cell(i,j) 的时间天然错开了。这种错位正是 MAC 单元能算对矩阵乘的关键，具体由 `cycle_num` 来调度，详见下一讲 u2-l2。本讲只需记住：**移位循环制造了 skew，skew 是脉动阵列的命门**。

#### 4.3.3 源码精读

weight 的下移循环：

[rtl/systolic.v:L61-L63](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L61-L63) —— 行从 1 开始，每行整体 `weight_queue[i][j] <= weight_queue[i-1][j]`。

```verilog
for(i=1; i<ARRAY_SIZE; i=i+1)
    for(j=0; j<ARRAY_SIZE; j=j+1)
        weight_queue[i][j] <= weight_queue[i-1][j];
```

data 的右移循环：

[rtl/systolic.v:L71-L73](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L71-L73) —— 列从 1 开始，每列整体 `data_queue[i][j] <= data_queue[i][j-1]`。

```verilog
for(i=0; i<ARRAY_SIZE; i=i+1)
    for(j=1; j<ARRAY_SIZE; j=j+1)
        data_queue[i][j] <= data_queue[i][j-1];
```

把整段放一起看会更清楚——填入（4.2）与移位（4.3）共享同一个时序块：

[rtl/systolic.v:L44-L76](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L44-L76) —— 完整的「复位 + 填入 + 移位」`always@(posedge clk)` 块。

一个容易踩的坑：`i`、`j` 是模块级 `integer`（见 [L40](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L40)），被填入、移位、MAC、输出收集**多个 always 块共用**。因为不同 always 块的 `i/j` 循环在仿真里是顺序展开、且各自在块内完整跑完一轮，所以共用不会冲突；但这是一种「不优雅」的写法，阅读时要意识到 `i/j` 在不同块里指的是各自那一份循环变量。

#### 4.3.4 代码实践

**目标**：验证「weight 逐拍下移一格」的直观感受。

**操作步骤**：

1. 在纸上画一个 8 行 × 1 列的竖条，代表 `weight_queue[0..7][3]`（第 3 列）。
2. 设第 3 列的 SRAM 注入序列（每拍进入 `weight_queue[0][3]` 的值）为 `W0, W1, W2, W3, …`（即每拍 `w0[7:0]` 不同）。
3. 逐拍填写这一列每个 cell 的值。

**预期结果**（每拍整列下移一格，顶部吃新值）：

| 拍数 | cell(0,3) | cell(1,3) | cell(2,3) | cell(3,3) | … |
| --- | --- | --- | --- | --- | --- |
| T0 | W0 | – | – | – | … |
| T1 | W1 | W0 | – | – | … |
| T2 | W2 | W1 | W0 | – | … |
| T3 | W3 | W2 | W1 | W0 | … |

**需要观察的现象**：`W0` 这个字节每过一拍就往下挪一格（T0 在 cell(0,3)，T1 在 cell(1,3)，T2 在 cell(2,3)…）。这就是「weight 自上而下」的可视化。

#### 4.3.5 小练习与答案

**练习 1**：data 的右移循环为什么 `i` 从 0 开始，而 weight 的下移循环 `i` 从 1 开始？
**答**：weight 的边界是第 0 **行**（`i=0`），由 SRAM 填入，所以下移循环要跳过 `i=0`、从 `i=1` 起；data 的边界是第 0 **列**（`j=0`），由 SRAM 填入，所以右移循环跳过的是 `j=0`，而 `i` 必须从 0 起遍历所有行。两者跳过的维度不同（一个跳行、一个跳列）。

**练习 2**：如果移位也用阻塞赋值 `=` 而不是非阻塞 `<=`，会发生什么？
**答**：在同一拍内，`weight_queue[1][j]` 会立刻拿到刚写入的 `weight_queue[0][j]`（SRAM 新值），接着 `weight_queue[2][j]` 又会拿到刚更新的 `weight_queue[1][j]`……结果是 SRAM 的新值在一拍之内**贯穿整列**直达底部，完全破坏了「一格一格流动」的脉动语义。所以非阻塞赋值在这里是**必须**的。

---

## 5. 综合实践

本任务把三个最小模块串起来：跟踪一个**内部 cell** 在连续 3 个时钟周期里，它的 `weight_queue[i][j]` 与 `data_queue[i][j]` 分别来自哪个上游。

**选定 cell**：`(i=2, j=3)`，即第 2 行第 3 列——它既不在边界（不是第 0 行/列），又足够浅，便于手工追溯。

**记号约定**：

- 记 `W(t)` = 第 `t` 拍从 SRAM 读出、被填入 `weight_queue[0][3]` 的那个字节，即 `sram_rdata_w0[7:0]` 在第 `t` 拍的值（见 4.2，列 3 来自 `w0[7:0]`）。
- 记 `D(t)` = 第 `t` 拍从 SRAM 读出、被填入 `data_queue[2][0]` 的那个字节，即 `sram_rdata_d0[15:8]` 在第 `t` 拍的值（见 4.2，行 2 来自 `d0[15:8]`）。

**推理（利用非阻塞赋值「取旧值」规则）**：

- weight 要从第 0 行下移到第 2 行，需要 2 拍：`weight_queue[2][3]` 在第 `T` 拍更新后的值 = 第 `T−1` 拍的 `weight_queue[1][3]` = 第 `T−2` 拍的 `weight_queue[0][3]` = `W(T−2)`。
- data 要从第 0 列右移到第 3 列，需要 3 拍：`data_queue[2][3]` 在第 `T` 拍更新后的值 = `D(T−3)`。

**逐拍表（cell(2,3) 在三拍内的取值与上游来源）**：

| 拍 | weight_queue[2][3] 取值 | weight 直接上游 | data_queue[2][3] 取值 | data 直接上游 |
| --- | --- | --- | --- | --- |
| T   | `W(T−2)` | 上一拍的 cell(1,3) | `D(T−3)` | 上一拍的 cell(2,2) |
| T+1 | `W(T−1)` | 上一拍的 cell(1,3) | `D(T−2)` | 上一拍的 cell(2,2) |
| T+2 | `W(T)`   | 上一拍的 cell(1,3) | `D(T−1)` | 上一拍的 cell(2,2) |

**如何画出完整来源链**：

- weight：cell(2,3) ← cell(1,3) ← cell(0,3) ← `sram_rdata_w0[7:0]`。即某个 weight 字节进阵列后，第 1 拍在 cell(0,3)，第 2 拍在 cell(1,3)，第 3 拍在 cell(2,3)。
- data：cell(2,3) ← cell(2,2) ← cell(2,1) ← cell(2,0) ← `sram_rdata_d0[15:8]`。即某个 data 字节进阵列后，依次经过 cell(2,0)→(2,1)→(2,2)→(2,3)，每拍右移一格。

**你需要回答的检验问题**（待本地验证）：

1. 在 cell(2,3)，同一个 weight 字节 `W(t)` 与同一个 data 字节 `D(t')` 要在**同一拍**相遇，`t` 与 `t'` 应满足什么关系？（提示：令 `W(T−2)=D(T−3)` 那一拍相遇 → 注入时刻 `t = T−2`，`t' = T−3`，即 **data 要比 weight 早一拍注入**。这正是控制器 `addr_sel` 制造地址歪斜、以及 testbench 里 `i%4` 错位的根本原因，详见 u3-l2 与 u4-l1。）
2. 若把 cell 换成 `(3,3)`，weight 与 data 的注入时间差又是多少？（答：weight 走 3 拍、data 走 3 拍，差为 0，即同时注入即可在 (3,3) 相遇。）

完成本实践后，你应当能凭直觉说出：**对角线 `i==j` 上的 cell（如 (3,3)）weight 与 data 同时到达；偏离对角线的 cell 则需要由地址歪斜来补偿到达时间差**——这就是下一讲 MAC 乘加时序的物理基础。

## 6. 本讲小结

- `weight_queue` 与 `data_queue` 是两个 `8×8`、元素为 `signed [7:0]` 的二维寄存器阵列，合计 1024bit，分别充当每个 cell 的本地 weight/data 寄存器。
- 边界是唯一的数据入口：weight 从第 0 行进入、data 从第 0 列进入；每路 32bit SRAM 被切成 4 个字节，两路拼出 8 个字节，喂满一排边界 cell（w0/w1→第 0 行，d0/d1→第 0 列）。
- 切片用变基部分选择 `[31-8*i -: 8]`，`i=0..3` 对应位段 `[31:24]…[7:0]`，**最高字节进下标 0**。
- 移位 for 循环让 weight 每拍整体下移一格（`weight_queue[i][j] <= weight_queue[i-1][j]`）、data 每拍整体右移一格（`data_queue[i][j] <= data_queue[i][j-1]`），通信只在相邻 cell 间发生。
- 非阻塞赋值 `<=` 是「逐格流动」能正确实现的根本保证；填入与移位互补地处理边界（weight 跳 `i=0`、data 跳 `j=0`）。
- 移位天然制造时间错位（skew）：到达 cell(i,j) 时，weight 滞后注入 `i` 拍、data 滞后 `j` 拍，这正是脉动阵列算对矩阵乘的命门。

## 7. 下一步学习建议

本讲只讲了「数据怎么流」，**还没讲 cell 拿到 weight 和 data 之后怎么算**。建议紧接着学：

- **u2-l2 MAC 乘加计算与累加时序**：看 `systolic.v` 第 92–118 行的组合 `always@(*)` 块，理解 `cycle_num`、`FIRST_OUT`、`PARALLEL_START` 如何决定每个 cell 在哪一拍开始累加、何时做新累加。本讲的 skew 推理（cell 到达时间差）将在那里被 `cycle_num` 的门控逻辑正式「对账」。
- **u3-l2 地址选择与输入歪斜 addr_sel**：看控制器如何通过地址偏移在 SRAM 端就预先制造好 weight/data 的注入时间差，与本讲综合实践第 1 题的结论呼应。
- 想加深对非阻塞赋值与时序的理解，可以再用 4.3.5 的练习 2 在仿真器里做个对比实验（改成 `=` 看波形如何崩坏）。

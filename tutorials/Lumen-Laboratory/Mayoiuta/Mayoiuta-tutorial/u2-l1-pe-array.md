# 处理单元与脉动阵列 PE Array

## 1. 本讲目标

上一讲（u1-l3）我们把顶层 `NPU_SOC` 拆成了一张「装配图」，并指出它的三个子模块（`npu_controller` / `npu_core` / `performance_monitor`）源码都不在仓库，属于待确认的灰盒。其中 `npu_core`（计算核）是整颗 NPU 真正「干活」的地方——但那扇门目前锁着。

本讲我们换一扇能打开的门：[hardware/rtl/core/pe_array.v](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/core/pe_array.v)。它是仓库里**真实存在、可以逐行读懂**的计算单元，定义了 NPU 里最核心的两种东西——**处理单元（Processing Element, PE）**和把许多 PE 拼起来的**脉动阵列（PE Array）**。可以把它理解成 `npu_core` 内部「最可能复用」的计算原语：看懂了它，你就握住了 NPU 加速神经网络运算的钥匙。

读完本讲你应当能够：

- 用自己的话说清**脉动阵列（systolic array）**为什么能把成百上千次乘加（MAC）摊到一片 PE 上并行执行，以及它的数据为什么是「从北/西进、向南/东出」地流动。
- 看懂 `PE_Array` 里 `i == 0` 和 `j == 0` 这两个**边界输入选择**——为什么阵列的顶边和左边要从外部喂数、而内部 PE 则吃「上一个 PE 的输出」。
- 解释三条 `opcode`（`0x1` 加载权重、`0x2` 乘加、`0x3` 直通）各自的含义，以及 `accumulator`（累加器）与 `weight_reg`（权重寄存器）在其中扮演的角色。
- 识别本文件里几处真实的「待确认」疑点：第 40 行的数组越界、`done` 信号被 64 个 PE 同时驱动、`start` 输入无人使用、乘加运算在 16 位宽度下被截断——并对它们给出诚实判断，不臆造。

> 本讲是第 2 单元（核心计算通路）的第一讲，后续 u2-l2（卷积引擎）和 u2-l3（多精度 PE）都建立在本讲的「PE + 数据流」直觉之上。

## 2. 前置知识

先用三小节建立直觉。已经熟悉脉动阵列和定点运算的读者可以跳到第 3 节。

### 2.1 为什么 NPU 需要「脉动阵列」

神经网络里最常见的运算是**乘加（Multiply-Accake，MAC）**：\(a \times w + b\)。一次矩阵—向量乘、一次卷积，本质上都是成千上万次 MAC 的堆叠。CPU 用一两个乘法器串行地算，GPU 用大量线程并行地算，而 NPU 的思路是——**直接摆一片乘法器阵列出来，让数据像血流一样流过它们，每个乘法器在每个时钟节拍都做一次 MAC**。

这种「数据有节律地（systolic，原意是心脏收缩）流过一片处理单元」的结构，就叫**脉动阵列（systolic array）**。它的好处是：每个 PE 只和相邻的 PE 说话（连线短、省功耗），数据进来后被多个 PE 复用（带宽需求低），整片阵列每个节拍都在出结果（吞吐高）。Google TPU 的核心就是一片脉动阵列。

打个比方：普通计算像「一个厨师从头到尾做一桌菜」；脉动阵列像「流水线厨房」——食材从一头进，每经过一位厨师就加一道工序，菜从另一头源源不断地出。

### 2.2 两种「驻留」策略：权重固定 与 输出固定

脉动阵列有两种经典排法，本讲的 `pe_array.v` 用的是**权重固定（weight-stationary）**：

- **权重固定**：权重预先装进每个 PE 的 `weight_reg` 里「住下」，激活值（输入特征）像流水一样从阵列的一侧流过，每经过一个 PE 就和那里的固定权重做一次乘加，结果累加在本 PE 的 `accumulator` 里。本讲正是这种结构——opcode `0x1` 装权重、`0x2` 流激活做乘加。
- **输出固定**：结果（部分和）「住」在 PE 里，两个输入矩阵的数据分别从两个方向流进来，每拍贡献一个乘积项。u2-l2 的卷积引擎会更接近这种思路。

记住这条主线：**权重住着不动，激活流动并在每个 PE 累加**。后面所有源码细节都是为这条主线服务的。

### 2.3 定点数与位宽截断：先埋一个伏笔

硬件里的小数通常用**定点数（fixed-point）**表示，即约定好小数点位置后的整数。本阵列的 `DATA_WIDTH = 16`，意味着每个数据是 16 位整数（默认无符号，本文件未指定符号位处理）。两个 16 位数相乘，数学上结果可达 32 位；但本讲会在 4.2 看到，代码把乘积又塞回了 16 位的累加器里——**高位被截断**。这是真实存在的设计取舍，先记住，到源码精读时再细说。

> 名词速查：RTL、Verilog module、`generate-for`、`parameter`、低有效复位 `rst_n`、顶层模块这些都在 u1-l1/u1-l2/u1-l3 已建立，本讲直接使用。

## 3. 本讲源码地图

本讲只精读一个文件，但会把它从头到尾拆透。

| 文件 | 作用 | 本讲怎么用它 |
|---|---|---|
| [hardware/rtl/core/pe_array.v](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/core/pe_array.v) | 声明 `PE_Array`（阵列外壳）与 `Processing_Element`（单个 PE）两个模块 | 全文精读：阵列如何用 `generate-for` 拼成、PE 的三指令状态机、数据流向、输出收集 |

> 说明：本文件**完全自包含**——`PE_Array` 和 `Processing_Element` 两个模块都在这一个文件里，且不引用任何外部子模块。这与 u1-l3 的 `NPU_SOC`（引用了三个缺失子模块）不同：本讲每一行都能在文件里找到对应实现，没有「源码缺失」的黑盒。但文件中仍有若干语法/逻辑疑点（越界、多驱动、未用输入、位宽截断），本讲会逐一如实标注为「待确认」，而非假装它们是正确实现。

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：先看阵列外壳 `PE_Array` 如何用 `generate-for` 把一片 PE 拼出来、如何注入边界数据；再看单个 `Processing_Element` 的三指令状态机与累加器机制；最后把两者合起来，追踪一次数据在整片阵列里的流动，并点出输出收集处的疑点。

### 4.1 阵列外壳：PE_Array 的参数化拼装与边界注入

#### 4.1.1 概念说明

`PE_Array` 是一片 \(N \times N\) 的 PE 方阵（\(N\) 即参数 `ARRAY_SIZE`，默认 8）。它自己不做任何运算，只做两件事：

1. **拼阵列**：用 `generate-for` 双重循环，把 \(N \times N\) 个 `Processing_Element` 例化出来，摆成方阵。
2. **接线**：为每个 PE 准备好「北面进、西面进」的输入和「南面出、东面出」的输出。处于阵列**顶边**（`i==0`）和**左边**（`j==0`）的 PE 直接吃外部输入；处于内部的 PE 则吃「上一个邻居」的输出——这就是脉动阵列「数据逐拍传递」的实现方式。

#### 4.1.2 核心流程

设阵列规模为 \(N\)（`ARRAY_SIZE`），PE 的行列下标为 \((i, j)\)，\(i, j \in \{0,1,\dots,N-1\}\)。每个 PE 有四个数据口：北入、西入、南出、东出。它们的连接规则是：

\[
\text{north\_in}(i,j) = \begin{cases} \texttt{north\_in}[j] & i = 0 \\ \text{south\_out}(i-1, j) & i > 0 \end{cases}
\]

\[
\text{west\_in}(i,j) = \begin{cases} \texttt{west\_in}[i] & j = 0 \\ \text{east\_out}(i, j-1) & j > 0 \end{cases}
\]

也就是说：**北面来的数据一路向南流**（顶边注入，逐行下传），**西面来的数据一路向东流**（左边注入，逐列右传）。用一个 \(4 \times 4\) 的小阵列示意：

```text
       north_in[0]  north_in[1]  north_in[2]  north_in[3]
            │            │            │            │
            ▼            ▼            ▼            ▼
west_in[0]─▶PE(0,0)───▶PE(0,1)───▶PE(0,2)───▶PE(0,3)───▶ east_out 行0
            │            │            │            │
            ▼            ▼            ▼            ▼
west_in[1]─▶PE(1,0)───▶PE(1,1)───▶PE(1,2)───▶PE(1,3)───▶ east_out 行1
            │            │            │            │
            ▼            ▼            ▼            ▼
west_in[2]─▶PE(2,0)───▶PE(2,1)───▶PE(2,2)───▶PE(2,3)───▶ east_out 行2
            │            │            │            │
            ▼            ▼            ▼            ▼
west_in[3]─▶PE(3,0)───▶PE(3,1)───▶PE(3,2)───▶PE(3,3)───▶ east_out 行3
            │            │            │            │
            ▼            ▼            ▼            ▼
         south_out[0] south_out[1] south_out[2] south_out[3]
```

纵向（南北）传递的是「激活流」，横向（东西）在权重加载阶段传递的是「权重流」。每个 PE 只和「上、左」两个邻居的输出相连——这正是脉动阵列「局部互连、连线最短」的精髓。

#### 4.1.3 源码精读

**阵列外壳的端口**定义在 [hardware/rtl/core/pe_array.v:1-14](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/core/pe_array.v#L1-L14)：

```verilog
module PE_Array #(
    parameter DATA_WIDTH = 16,            // 每个数据的位宽
    parameter ARRAY_SIZE = 8              // 阵列边长 N，默认 8x8
)(
    input wire clk,
    input wire rst_n,
    input wire [DATA_WIDTH-1:0]  north_in [ARRAY_SIZE-1:0],  // 顶边注入：N 个数据
    input wire [DATA_WIDTH-1:0]  west_in  [ARRAY_SIZE-1:0],  // 左边注入：N 个数据
    output wire [DATA_WIDTH-1:0] south_out[ARRAY_SIZE-1:0],  // 底边输出：N 个数据
    output wire [DATA_WIDTH-1:0] east_out [ARRAY_SIZE-1:0],  // 右边输出：N 个数据
    input wire [3:0]             opcode,   // 4 位操作码，广播给所有 PE
    input wire                   start,
    output wire                  done
);
```

逐行说明：

- `parameter DATA_WIDTH = 16` / `ARRAY_SIZE = 8`：和 u1-l3 的 `CORES` 一样，这是**参数化**旋钮。把 `ARRAY_SIZE` 改成 16，下面 `generate` 就会「长」出 16×16=256 个 PE；改成 4 就是 4×4=16 个。这是 NPU「可扩展」愿景在计算单元层的落地。
- `north_in [ARRAY_SIZE-1:0]`：注意这是**数组型端口**（unpacked array）——`ARRAY_SIZE` 根线，每根 `DATA_WIDTH` 位宽。它表示「顶边的 N 个输入孔」，分别喂给第 0 行的 N 个 PE。`west_in` 同理表示左边的 N 个输入孔。
- `south_out` / `east_out`：底边和右边的 N 个输出孔，用于把阵列边缘的 PE 输出收集出去。
- `opcode [3:0]`：4 位操作码。关键点：**它被广播（broadcast）给所有 PE**——同一时刻全片阵列执行同一条指令。这是脉动阵列的典型控制方式：用「全局指令 + 数据流动」代替「每 PE 单独控制」。
- `start` / `done`：意图是「启动 / 完成」握手。但后面会看到，`start` 在本文件里**从未被使用**，`done` 则有**多驱动**问题——两者都属待确认（见 4.3）。

**内部互连网线**在 [hardware/rtl/core/pe_array.v:16-17](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/core/pe_array.v#L16-L17)：

```verilog
wire [DATA_WIDTH-1:0] pe_data_h [ARRAY_SIZE][ARRAY_SIZE];  // 水平流：东出 → 右邻西入
wire [DATA_WIDTH-1:0] pe_data_v [ARRAY_SIZE][ARRAY_SIZE];  // 垂直流：南出 → 下邻北入
```

这是两个二维 wire 数组，构成了 PE 之间的「内部管道」。约定：

- `pe_data_h[i][j]` 接住 PE\((i,j)\) 的**东出**，并喂给右邻 PE\((i,j+1)\) 的西入。
- `pe_data_v[i][j]` 接住 PE\((i,j)\) 的**南出**，并喂给下邻 PE\((i+1,j)\) 的北入。

> 阅读窍门：Verilog 里 `wire [W-1:0] name [N][N];` 声明的是「N×N 条位宽为 W 的线」组成的二维数组，用 `name[i][j]` 取第 \((i,j)\) 条。这里的下标范围是 `0..N-1`——请记住这一点，4.3 会有一处越界正是踩在 `N` 上。

**参数化例化与边界注入**是本模块的灵魂，在 [hardware/rtl/core/pe_array.v:19-37](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/core/pe_array.v#L19-L37)：

```verilog
generate
for (genvar i = 0; i < ARRAY_SIZE; i = i + 1) begin : row_gen
    for (genvar j = 0; j < ARRAY_SIZE; j = j + 1) begin : col_gen
        Processing_Element #(
            .DATA_WIDTH(DATA_WIDTH)
        ) u_pe (
            .clk(clk),
            .rst_n(rst_n),
            .north_in(i == 0 ? north_in[j] : pe_data_v[i-1][j]),  // 顶边吃外部，否则吃上邻南出
            .west_in (j == 0 ? west_in[i]  : pe_data_h[i][j-1]),  // 左边吃外部，否则吃左邻东出
            .south_out(pe_data_v[i][j]),                          // 本 PE 南出 → 喂下邻
            .east_out (pe_data_h[i][j]),                          // 本 PE 东出 → 喂右邻
            .opcode(opcode),
            .start(start),
            .done(done)                                           // 见 4.3：多驱动疑点
        );
    end
end
endgenerate
```

逐行说明：

- `generate ... endgenerate` + 两层 `for (genvar ...)`：Verilog 的**参数化例化**惯用法（u1-l3 已见）。综合时循环被静态展开——`ARRAY_SIZE=8` 就在硬件里「长」出 8×8=64 个 `Processing_Element` 实例，每个都有独立的寄存器。`row_gen` / `col_gen` 是给每一层循环起的名字（generate block label）。
- **`.north_in(i == 0 ? north_in[j] : pe_data_v[i-1][j])`**：边界注入的核心。第 0 行（`i==0`）的 PE 从顶边外部输入 `north_in[j]` 取数；其余行的 PE 从「上一行同列 PE 的南出」`pe_data_v[i-1][j]` 取数。这就是「激活一路向南流」的接线。
- **`.west_in(j == 0 ? west_in[i] : pe_data_h[i][j-1])`**：同理，第 0 列从左边外部输入取数，其余列从左邻东出取数。这是「权重一路向东流」的接线（在加载阶段）。
- `.south_out(pe_data_v[i][j])` / `.east_out(pe_data_h[i][j])`：本 PE 的两个输出分别写进互连网线，供下邻 / 右邻读取。至此，「上/左进、下/右出」的脉动数据通路闭合。
- `.opcode(opcode)`：把顶层 `opcode` 广播给每一个 PE，确保全片同步执行同一指令。

> 一致性核对：这段 `generate` 写得非常干净——每个 PE 的四个数据口都恰好接到了正确的网线上，没有任何越界（`i-1` 只在 `i>0` 时取，`j-1` 只在 `j>0` 时取，由三目运算符保证）。它和 u1-l3 的环形 NoC 一样，是 Mayoiuta「参数化 + 局部互连」设计哲学的典范。

#### 4.1.4 代码实践

**实践目标**：在 `ARRAY_SIZE=4` 的小阵列里，亲手追踪「北面输入」如何逐行下传，从而把 4.1.2 的抽象公式落到具体的网线连接上。

**操作步骤**：

1. 假设 `ARRAY_SIZE=4`，在纸上画一张 4×4 的 PE 方阵（可参考 4.1.2 的示意图）。
2. 对顶边注入：写下 `north_in[0..3]` 分别进入哪些 PE（应是第 0 行的 PE(0,0)..PE(0,3)）。
3. 对内部纵向传递：对 PE(1,2)、PE(2,2)、PE(3,2)，分别写出它们的 `north_in` 来自哪条网线。
4. 类似地，对横向传递：写出 PE(2,1)、PE(2,2)、PE(2,3) 的 `west_in` 来源。

**需要观察的现象**：

- PE(1,2) 的 `north_in` = `pe_data_v[0][2]`（即 PE(0,2) 的南出）；PE(2,2) 的 `north_in` = `pe_data_v[1][2]`；PE(3,2) 的 `north_in` = `pe_data_v[2][2]`。同列 PE 沿 `pe_data_v[*][2]` 这条「竖管」首尾相接。
- 横向同理：同行 PE 沿 `pe_data_h[2][*]` 这条「横管」首尾相接。

**预期结果**：你会清楚看到——**每一列是一条竖直的数据流水线，每一行是一条横向的数据流水线**，PE 正是这两组流水线的交汇点。这就是脉动阵列「数据在网格上流动」的物理图像。

**待本地验证**：以上为静态连线分析，不涉及运行；若要观察数据逐拍传播的波形，需要仿真环境，仓库暂未提供，标注待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：把 `ARRAY_SIZE` 从 8 改成 16，`PE_Array` 对外的端口数量会变多吗？内部会多出多少个 PE？

**参考答案**：对外端口数量**不变**——`north_in` / `west_in` / `south_out` / `east_out` 始终是 4 组数组型端口，`opcode` / `start` / `done` / `clk` / `rst_n` 也固定。但每组数组端口的**长度**会从 8 变成 16（因为 `[ARRAY_SIZE-1:0]`），内部例化的 PE 数量从 \(8\times8=64\) 变成 \(16\times16=256\)，多出 192 个。这正是参数化设计的好处：对外接口形状稳定，内部规模可伸缩。

**练习 2**：为什么边界注入要用三目运算符 `i == 0 ? north_in[j] : pe_data_v[i-1][j]`，而不是直接写 `north_in = pe_data_v[i-1][j]`？

**参考答案**：因为第 0 行（`i==0`）的 PE **上面没有邻居**，`pe_data_v[i-1][j]` 会变成 `pe_data_v[-1][j]`，下标越界、无意义。所以必须为边界 PE 单独接外部输入 `north_in[j]`。三目运算符的作用正是「区分边界与内部」。这是所有脉动阵列都要处理的「边界条件」问题。

---

### 4.2 单个处理单元：Processing_Element 的三指令状态机

#### 4.2.1 概念说明

整片阵列的运算能力来自每一个 `Processing_Element`。一个 PE 内部有两件最重要的家当：

- **`weight_reg`（权重寄存器）**：存放「住」在这个 PE 里的权重。一旦装好，在权重固定模式下就不再变动，等待激活来和它相乘。
- **`accumulator`（累加器）**：存放部分和。每次乘加指令都把 `north_in * weight_reg` 累加上去。

PE 是一个**时序状态机**：每个时钟上升沿（或复位下降沿），它根据广播来的 4 位 `opcode` 决定「这一拍干什么」。本文件定义了三条有效指令，外加一个 `default` 兜底：

| opcode | 助记名 | 行为 | 作用 |
|---|---|---|---|
| `0x1` | 加载权重 | `weight_reg ← west_in` | 把西入数据装进权重寄存器 |
| `0x2` | 乘加 MAC | `accumulator ← accumulator + north_in × weight_reg` | 激活乘权重并累加 |
| `0x3` | 直通 PASS | `south_out ← north_in`；`east_out ← west_in` | 数据原样穿过，不做运算 |
| 其他 | `default` | `done ← 0` | 未定义指令，仅拉低 done |

#### 4.2.2 核心流程

一个 PE 的典型工作周期是「先装权重，再反复乘加，最后（必要时）直通」：

```text
   复位 ──▶ opcode=0x1 (装权重, 多拍可装满阵列)
                │
                ▼
          opcode=0x2 (乘加, 激活逐拍流入, accumulator 不断累加)
                │
                ▼
          opcode=0x3 (直通, 把数据原样传给下游)   或   读出 accumulator
```

需要强调两点：

1. **权重加载与乘加是分相（phase）进行的**。先在 `0x1` 相把权重铺进各 PE 的 `weight_reg`，再切到 `0x2` 相让激活流入做乘加。两个相不能混——因为 `0x2` 用的是 `weight_reg` 里的旧权重，必须先装好。这种「分相控制」由外部逻辑（不在本文件）通过切换 `opcode` 完成。
2. **`0x3` 直通**让数据不经计算直接穿过 PE，相当于把 PE 当成「导线」用。这在某些数据排布场景下有用（让数据快速到位），但它**不动累加器**。

注意一个设计缺口：本文件**没有「清零累加器」的指令**。要让 `accumulator` 归零，目前唯一的办法是全局 `rst_n` 复位——但 `rst_n` 会同时把 `weight_reg` 也清零（见 4.2.3）。这意味着「保留权重、只清累加器」在当前指令集里做不到。综合实践的代码实践任务正是补上这个缺口。

#### 4.2.3 源码精读

**PE 的端口与内部寄存器**在 [hardware/rtl/core/pe_array.v:44-59](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/core/pe_array.v#L44-L59)：

```verilog
module Processing_Element #(
    parameter DATA_WIDTH = 16
)(
    input wire clk,
    input wire rst_n,
    input wire [DATA_WIDTH-1:0] north_in,
    input wire [DATA_WIDTH-1:0] west_in,
    output reg [DATA_WIDTH-1:0] south_out,
    output reg [DATA_WIDTH-1:0] east_out,
    input wire [3:0] opcode,
    input wire start,                 // 待确认：本文件中从未使用
    output reg done
);

reg [DATA_WIDTH-1:0] accumulator;     // 累加器：存放部分和
reg [DATA_WIDTH-1:0] weight_reg;      // 权重寄存器：存放固定权重
```

逐行说明：

- `output reg south_out` / `east_out`：注意是 `reg`，意味着它们是**寄存器输出**（在时钟沿更新），不是组合逻辑。只在 `0x3` 直通相被赋值，其余相保持上一拍的值。
- `start`：声明为输入，但接下来整个 `always` 块里都**找不到它**。它是一个「死输入」——待确认（见 4.3.4 的实践）。
- `accumulator` / `weight_reg`：PE 的两个核心状态。注意它们都只有 `DATA_WIDTH=16` 位宽——这埋下了乘加截断的伏笔。

**状态机主体**在 [hardware/rtl/core/pe_array.v:61-84](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/core/pe_array.v#L61-L84)：

```verilog
always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
        accumulator <= 0;
        weight_reg  <= 0;
        done        <= 0;
    end else begin
        case(opcode)
            4'h1: begin                              // 加载权重
                weight_reg <= west_in;
                done       <= 1;
            end
            4'h2: begin                              // 乘加 MAC
                accumulator <= accumulator + (north_in * weight_reg);
                done        <= 1;
            end
            4'h3: begin                              // 直通 PASS
                south_out <= north_in;
                east_out  <= west_in;
                done      <= 1;
            end
            default: done <= 0;
        endcase
    end
end
```

逐行说明：

- `always @(posedge clk or negedge rst_n)`：这是**异步低有效复位**的时序块写法。`posedge clk` 表示时钟上升沿触发；`negedge rst_n` 表示 `rst_n` 从 1 跳到 0（下降沿）也触发——且因为写在敏感列表里，复位是「异步」的（不需要等时钟）。`if (!rst_n)` 分支就是复位动作：把 `accumulator`、`weight_reg`、`done` 全清零。这就是「全局复位会同时清权重和累加器」的来源。
- **`4'h1`（加载权重）**：`weight_reg <= west_in` 把西入数据锁存进权重寄存器。注意此相**不更新 `south_out` / `east_out`**，所以权重不会在这一拍向右传播——关于「整行权重如何铺满」的时序问题，见 4.3 的讨论（待确认）。
- **`4'h2`（乘加）**：本阵列的灵魂指令。`accumulator <= accumulator + (north_in * weight_reg)` 实现了 \(a \leftarrow a + x \cdot w\)。

  ⚠ **位宽截断（待确认）**：`north_in` 和 `weight_reg` 都是 16 位，数学上 \(x \cdot w\) 可达 32 位。但本行把整个右值表达式最终赋给 16 位的 `accumulator`，Verilog 会按上下文宽度（这里是 16）计算——**乘积的高 16 位被截断**，累加也在 16 位空间内进行（溢出回绕）。标准 MAC 单元通常保留 32 位累加器以避免溢出；本设计用 16 位累加是粗粒度近似，是否为有意简化待确认，但读者必须知道：**这里算出来的累加值在数值较大时会失真**。
- **`4'h3`（直通）**：`south_out <= north_in` 和 `east_out <= west_in` 把两口输入原样转到两口输出，相当于 PE 变成「十字导线」。此相**不动 `accumulator` 也不动 `weight_reg`**。
- `default: done <= 0`：任何未定义的 opcode（含 `0x0`）都落入此分支，只把 `done` 拉低，其他寄存器保持不变。这意味着 **`0x0` 实际上是一个「空操作 / NOP」**——它不改变任何状态。

> 设计观察：`done` 在三条有效指令里都被置 1，且由于 `done` 是寄存器、`opcode` 又是全阵列广播，**所有 PE 会在同一拍把 `done` 写成同一个值**。这在功能上「看起来」一致，但在 4.3 会看到，这种写法导致了多驱动语法问题。

#### 4.2.4 代码实践

**实践目标**：动手为 PE 增加一条新指令——**「清零累加器」（opcode `0x4`）**，补上 4.2.2 指出的设计缺口，并思考它对阵列数据流的影响。

**操作步骤**（在本地副本上修改，勿改动仓库源码）：

1. 复制 [pe_array.v](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/core/pe_array.v) 到一个练习目录。
2. 在 `case(opcode)` 的 `4'h3` 分支之后、`default` 之前，插入新分支：

   ```verilog
   // 示例代码：新增「清零累加器」指令（非项目原有代码）
   4'h4: begin
       accumulator <= 0;     // 只清累加器
       done        <= 1;
   end
   ```

3. 思考：为什么分支里**不写** `weight_reg <= 0;`？

**需要观察的现象**：

- 新指令 `0x4` 只复位 `accumulator`，**保留** `weight_reg`。这正是它与全局 `rst_n` 的关键区别——`rst_n` 会把权重也清掉，而 `0x4` 只清累加器。
- 由于 `opcode` 是全阵列广播，发一次 `0x4` 会**同时清零所有 64 个 PE 的累加器**，而权重纹丝不动。

**预期结果**：得到一个可在「保留权重」前提下「重开一轮累加」的指令。典型用法是——先用 `0x1` 装满权重，然后每个输出通道的计算开始前发一拍 `0x4` 清零累加器，再切 `0x2` 流激活做乘加。

**思考延伸（不必实现）**：

- 如果想加的是**「加偏置 bias」**指令（如 `0x5`），可写成 `accumulator <= accumulator + north_in;`——把北入当作偏置直接加到累加器。请对比它与 `0x2` 的区别：`0x5` 不乘权重，只加偏置。
- 新指令对**数据流**的影响：`0x4` / `0x5` 都不更新 `south_out` / `east_out`，所以它们**不推进阵列的数据流动**，只改变 PE 内部状态。这与 `0x3`（推进数据流、不动内部状态）正好相反。

**待本地验证**：以上为新指令的设计与手写示例，未在仿真器中运行；若要验证波形（例如确认 `0x4` 后 `accumulator` 归零而 `weight_reg` 不变），需引入仿真环境，仓库暂未提供，标注待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：在 opcode `0x2`（乘加）相，PE 的 `south_out` 和 `east_out` 会被更新吗？如果不更新，它们的值是什么？

**参考答案**：不会更新。`0x2` 分支只赋值 `accumulator` 和 `done`，没有对 `south_out` / `east_out` 赋值。因为它们是 `reg`，未赋值时**保持上一拍的值**（寄存器锁存）。所以乘加相期间，PE 的两个输出端口「冻结」在最近一次 `0x3` 直通写入的值上——这意味着乘加相的数据流靠的是 `pe_data_h/v` 网线上「上一拍」的快照，理解这点对分析时序很关键。

**练习 2**：`north_in * weight_reg` 在数学上可能是 32 位数，但代码把它赋给 16 位的 `accumulator`。如果 `north_in = 300`、`weight_reg = 300`（均未超过 16 位无符号上限 65535），累加结果会是多少？

**参考答案**：\(300 \times 300 = 90000\)，超过 16 位无符号上限 \(2^{16}-1 = 65535\)。在 16 位空间里，\(90000 - 65536 = 24464\)（回绕），所以 `accumulator` 会得到 24464 而非 90000——这是一个**溢出失真**。这印证了 4.2.3 的位宽截断警告：本阵列的累加器在数值稍大时就会溢出，实际使用需要配合定标（scaling）或更宽的累加器，属待确认的设计取舍。

---

### 4.3 数据流贯通与输出收集：把阵列连成整体

#### 4.3.1 概念说明

4.1 讲了「PE 之间怎么连线」，4.2 讲了「单个 PE 怎么算」。本节把两者合起来，回答两个收尾问题：

1. **输出怎么收集**：阵列底边和右边的 PE 输出，如何汇总成 `PE_Array` 对外的 `south_out` / `east_out`？
2. **整片阵列如何协同完成一次计算**：权重加载相、乘加相、直通相之间，数据在网格上实际是怎么动的？

同时，本节会集中点出本文件里**真实的语法/逻辑疑点**——它们都集中在「输出收集」和「握手信号」两处，需要诚实标注为待确认。

#### 4.3.2 核心流程

把三个相串起来，一次完整的「权重固定式矩阵计算」理想流程如下：

```text
[相1：加载权重 0x1]  西入权重 ──逐拍向东──▶ 铺满每行 PE 的 weight_reg
        │
        ▼  （切 opcode = 0x2）
[相2：乘加 0x2]     北入激活 ──逐拍向南──▶ 每经过一个 PE 就乘上当地 weight_reg 并累加
        │
        ▼
[相3：直通/读出 0x3]  累加结果或中间数据 ──▶ 从 south_out / east_out 收集出阵列
```

理想情况下，相 1 把权重逐拍从左边注入、经过若干拍后铺满整行；相 2 把激活逐拍从顶边注入、经过若干拍后让每个 PE 的累加器收敛到目标值；相 3 把结果收集出去。

但要注意一个**时序前提**：相 1（`0x1`）分支**不更新 `east_out`**（见 4.2.3），所以「权重在加载相逐拍向东传播」这一点，**在本文件的指令语义里并不能直接成立**——`0x1` 只锁存本 PE 的权重，不向右转发。要让权重真正流过整行，需要外部控制额外穿插 `0x3` 直通相来搬运，或者依赖某种本文件未体现的机制。因此「权重如何铺满整片阵列」的精确时序，在仅看本文件时无法确定——属待确认。这一点和 u1-l3 对 `npu_controller` 的态度一致：**只讲能从源码确证的连线与指令，对依赖外部控制的行为标注待确认**。

#### 4.3.3 源码精读

**输出收集**是本文件疑点最密集之处，在 [hardware/rtl/core/pe_array.v:39-40](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/core/pe_array.v#L39-L40)：

```verilog
assign south_out = pe_data_v[ARRAY_SIZE-1];          // 第 39 行：取最后一行（底边）
assign east_out  = pe_data_h[ARRAY_SIZE][ARRAY_SIZE-1]; // 第 40 行：⚠ 越界！
```

逐行说明：

- **第 39 行 `south_out = pe_data_v[ARRAY_SIZE-1]`**：`pe_data_v[ARRAY_SIZE-1]` 是二维数组的「最后一行」（下标 `ARRAY_SIZE-1`，合法），它是底边 N 个 PE 的南输出组成的长度为 N 的一维数组，整体赋给同为「N 个元素」的 `south_out` 端口。**语义上正确**——它收集的是阵列底边的输出。
- ⚠ **第 40 行 `east_out = pe_data_h[ARRAY_SIZE][ARRAY_SIZE-1]`——数组越界（待确认/疑似 bug）**：`pe_data_h` 在第 16 行声明为 `[ARRAY_SIZE][ARRAY_SIZE]`，即 `[8][8]`，合法下标是 `0..7`。而此处第一维下标写成了 `[ARRAY_SIZE]` 即 `[8]`——**越界**。对比第 39 行正确使用了 `[ARRAY_SIZE-1]`，第 40 行漏了 `-1`。合理推测原意是取最右一列（即所有 `pe_data_h[i][ARRAY_SIZE-1]`）或右下角（`pe_data_h[ARRAY_SIZE-1][ARRAY_SIZE-1]`），但 Verilog 无法用单一表达式从二维 unpacked 数组里「切一列」，而写成的单元素又越界。因此 `east_out` 的实际收集逻辑**无法从当前源码确证**，属待确认。

> 诚实判断：第 40 行是一处**几乎可以确定的笔误**（漏写 `-1`），但因为它牵涉「到底想收集右列还是右下角」的语义，且 unpacked 数组列切片本身工具支持不一，本讲不替读者拍板「正确写法应该是什么」，而是标注为待确认——这与全手册「不臆造」的原则一致。

**`done` 的多驱动问题**回到 [hardware/rtl/core/pe_array.v:33](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/core/pe_array.v#L33) 的 `.done(done)`：

`PE_Array` 的 `done` 是单根 `output wire`，而 `generate` 循环把**每一个** PE 的 `done`（在 PE 内是 `output reg`）都连到了这同一根线上。当 `ARRAY_SIZE=8` 时，就有 64 个 `always` 块在驱动同一根 `done`。虽然在有效指令下它们都赋 `1`、看似一致，但**多个过程驱动源（multiple drivers）连到一根 wire 在 Verilog 里是非法/多驱动的**——仿真器会在驱动冲突时报 `x` 或告警，综合工具也会拒绝。正确做法通常是用一个归约表达式，例如「所有 PE 的内部 done 相与」后再赋给顶层 `done`。因此本文件的 `done` 连法**属待确认**，不能当作可工作的握手信号。

**`start` 未使用**：在 [第 11–12 行](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/core/pe_array.v#L11-L12) 声明、[第 32 行](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/core/pe_array.v#L32) 透传给 PE，但在 `Processing_Element` 的整个 `always` 块（第 61–84 行）里从未出现。它是一根**贯穿两层模块却无人消费的死信号**，属待确认（很可能是为未来「按拍启动」控制预留的接口，当前未实现）。

> 汇总本节三处待确认疑点：① 第 40 行 `east_out` 越界；② `done` 被 64 个 PE 多驱动；③ `start` 死输入。它们都不影响你理解脉动阵列的**计算原理与数据流向**，但提醒你：本文件更像一份「教学/草案」级 RTL，距离可直接综合的工程实现还有距离。

#### 4.3.4 代码实践

**实践目标**：用「证据搜集法」亲自确认上述三处疑点，而不是凭讲述记住它们。

**操作步骤**：

1. 打开 [pe_array.v](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/core/pe_array.v)。
2. **查越界**：定位第 16 行 `pe_data_h` 的声明维度，再定位第 40 行的下标。数一数 `[ARRAY_SIZE]` 是否落在合法范围 `0..ARRAY_SIZE-1` 内。对比第 39 行。
3. **查多驱动**：在文件里搜索 `done`，统计 `generate` 循环里有多少个实例的 `.done(...)` 连到了顶层 `done`；再确认顶层 `done` 的声明是 `wire` 还是 `reg`，思考「多个 `output reg` 驱一根 `wire`」是否合法。
4. **查死输入**：搜索 `start`，确认它出现在声明（`PE_Array` 与 `Processing_Element` 两处）和例化连接 `.start(start)` 中，但**不出现**在任何 `always` / `assign` 的右值或条件里。

**需要观察的现象**：

- 第 40 行第一维下标为 `ARRAY_SIZE`，越界；第 39 行为 `ARRAY_SIZE-1`，合法——两者写法不一致。
- `done` 在 `generate` 中被 `ARRAY_SIZE×ARRAY_SIZE` 个实例驱动，全部连到同一根顶层 `wire done`。
- `start` 只在端口表与例化连接处出现，无任何逻辑消费。

**预期结果**：得到一张「疑点—证据—判断」三栏表，每条疑点都能指到具体行号。你会确认：这三处都是源码阅读能直接发现的真实问题，而非传闻。

**待本地验证**：以上为静态阅读结论。若要确认综合器对「越界」与「多驱动」的实际报错行为，需引入 Verilog 综合工具，仓库暂未提供，标注待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：第 39 行 `assign south_out = pe_data_v[ARRAY_SIZE-1];` 把二维数组的一整行赋给了一维数组端口。这种「整行赋值」在 Verilog 里是否一定可综合？

**参考答案**：不一定。这属于 **unpacked 数组之间的整体赋值**，其可综合性依赖工具与语言版本（Verilog-2001 / SystemVerilog 支持程度不同）。语义上它是清楚的——把最后一行的 N 个元素整体搬给 `south_out`——但严谨的工程实现通常会展开成 N 条独立的 `assign south_out[k] = pe_data_v[ARRAY_SIZE-1][k];` 以确保可综合。所以「语义正确」≠「工程上一定可综合」，属待确认。

**练习 2**：如果把 `done` 的多驱动改成「所有 PE 内部 done 相与」的归约，语义上应该是什么？为什么相与（AND）而不是相或（OR）？

**参考答案**：若要表达「整片阵列这一拍全部完成」，应当把所有 PE 的内部 done 信号**相与（AND）**——只有当每一个 PE 都报 `done=1` 时，顶层 `done` 才为 1；只要有一个 PE 还没完成（`done=0`），整体就为 0。若用 OR，则只要任意一个 PE 完成就报完成，会漏掉尚未完成的 PE，语义错误。所以「全部完成」天然对应 AND 归约。这也反衬出原代码「所有 PE 直接驱动同一根线」既非法、又没有表达出这种归约意图。

---

## 5. 综合实践

本讲的综合任务是把第 4 节的「阵列拼装 + PE 状态机 + 数据流」三部分串成一张完整的脉动阵列工作图，并在其上标注一次真实计算的数据流动轨迹与待确认疑点。这是源码阅读型实践，目的是让你把零散的知识固化为一张可随时回想的整体图。

**实践目标**：绘制 `ARRAY_SIZE=4` 时 `PE_Array` 的一次完整工作轨迹——依次经历「加载权重 → 乘加 → 直通」三相，标出每一相的数据流向，并标出三处待确认疑点的位置。

**操作步骤**：

1. 在画布中央画一张 4×4 的 PE 方阵，每个 PE 内部画两个小方框（`weight_reg`、`accumulator`）。
2. 标出**外部输入**：顶边的 `north_in[0..3]`、左边的 `west_in[0..3]`；标出**外部输出**：底边的 `south_out[0..3]`、右边的 `east_out[0..3]`。
3. 用**蓝色箭头**画出纵向 `pe_data_v` 管道（北→南），用**红色箭头**画出横向 `pe_data_h` 管道（西→东），并在每条管道旁注上对应的网线名 `pe_data_v[i][j]` / `pe_data_h[i][j]`。
4. 在图旁列出三个相的控制时序：
   - 相 1（`opcode=0x1`）：西入权重 → 各 PE `weight_reg`；标注「此相 `east_out` 不更新，权重如何铺满整行待确认」。
   - 相 2（`opcode=0x2`）：北入激活 → 经 `pe_data_v` 逐行下传 → 每个 PE 执行 `accumulator += north_in × weight_reg`；标注「乘加在 16 位宽度截断」。
   - 相 3（`opcode=0x3`）：`north_in→south_out`、`west_in→east_out` 原样穿透。
5. 在图上用**警告标记**标出三处待确认疑点的精确位置：
   - 第 40 行 `east_out` 收集处的「越界」标记。
   - 每一个 PE 的 `done` 输出汇聚到同一根线的「多驱动」标记。
   - `start` 输入旁的「未使用」标记。

**需要观察的现象**：你会得到一张「计算原理清晰、工程疑点明确」的阵列工作图——数据流向与三相时序一目了然，同时三处待确认问题也被钉在图上，不会被「原理讲通了」的成就感掩盖。

**预期结果**：一张标注完整的 4×4 脉动阵列工作轨迹图，至少包含：边界注入与内部互连网线、三个 opcode 相的时序与数据流、`weight_reg` 与 `accumulator` 在各相的变化，以及三处待确认疑点的位置标记。

**待本地验证**：本实践为静态源码阅读产出，不涉及运行；若要观察「权重铺满 / 激活乘加」的真实逐拍波形，需仿真环境，仓库暂未提供，标注待本地验证。

## 6. 本讲小结

- `PE_Array` 是一片 \(N \times N\)（默认 8×8）的**脉动阵列**，用 `generate-for` 双重循环参数化地例化出 \(N^2\) 个 `Processing_Element`，对外形状稳定、内部规模可伸缩——这是 NPU「可扩展」愿景在计算单元层的落地。
- 数据流向遵循「**北/西进、南/东出**」：顶边 `north_in` 和左边 `west_in` 是外部注入点（`i==0` / `j==0`），内部 PE 则通过二维网线 `pe_data_v`（纵向）和 `pe_data_h`（横向）吃「上邻南出 / 左邻东出」，实现数据逐拍传递。
- 单个 `Processing_Element` 是一个**三指令状态机**：`0x1` 把西入装进 `weight_reg`（权重固定）、`0x2` 执行 `accumulator += north_in × weight_reg`（乘加，MAC）、`0x3` 把两口输入原样转到两口输出（直通）；复位 `rst_n` 会同时清权重和累加器。
- 本阵列是**权重固定（weight-stationary）**结构：权重住进 `weight_reg` 不动，激活流动并在每个 PE 累加；一次完整计算需要外部控制按「装权重 → 乘加 → 直通」三相切换 `opcode`。
- 本文件存在四处真实的「待确认」疑点：① 第 40 行 `east_out` 收集**数组越界**（漏写 `-1`）；② `done` 被 \(N^2\) 个 PE **多驱动**；③ `start` 是**贯穿两层却无人使用的死输入**；④ 乘加在 16 位宽度下**高位截断**。它们不影响理解原理，但说明本文件距工程级可综合实现尚有距离。

## 7. 下一步学习建议

本讲建立了「PE + 脉动数据流」的直觉，这是整颗 NPU 计算通路的基石。建议按以下顺序继续：

1. **进入卷积引擎**：u2-l2 的 [hardware/rtl/core/conv_engine.v](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/core/conv_engine.v) 讲解 3×3 卷积如何用滑动窗口 + 九点乘累加实现。你会看到它和本讲的 MAC 是同源关系，但数据排布从「矩阵—矩阵」变成了「滑动窗口—卷积核」。
2. **再看多精度 PE**：u2-l3 的 [hardware/rtl/core/adaptive_pe.v](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/core/adaptive_pe.v) 把本讲「只支持 16 位定点」的 PE 升级成 INT8/FP16/BFLOAT16/INT16 四精度可切换，并实现真正的 IEEE754 半精度乘法——正好对照本讲 4.2.3 的「位宽截断」话题。
3. **回看本讲的时序缺口**：等读完卷积引擎后，回头思考本讲标注的「权重加载相不更新 `east_out`，权重如何铺满整行」这一待确认问题——届时你会对「外部控制如何驱动阵列分相工作」有更具体的判断。
4. **向上回到顶层**：带着本讲对 PE 的理解，重读 u1-l3 的 `npu_core`（待确认）。虽然 `npu_core` 源码缺失，但你可以合理推测：它的内部很可能就是以某种 `PE_Array` 为核心，再加上取数、重排、控制逻辑——本讲正是那块最关键的「芯」。
5. **若关心数据如何喂给阵列**：可跳到 u3-l2 的 [data_reorder.v](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/memory/data_reorder.v)，看 NCHW/NHWC 数据排布如何被重排成 PE 阵列所需的格式——那是本讲 `north_in` / `west_in` 之上游。

> 持续提醒：本讲凡涉及「权重铺满整行的精确时序」「`east_out` 越界后的真实行为」「`done` 多驱动后综合结果」等描述，在引入仿真/综合环境前都应保持「待确认」标注，切勿臆造其工程行为。

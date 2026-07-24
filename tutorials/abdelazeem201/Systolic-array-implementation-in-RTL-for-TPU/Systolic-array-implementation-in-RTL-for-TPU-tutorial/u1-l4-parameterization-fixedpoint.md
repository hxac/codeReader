# Verilog 参数化设计与定点数表示

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `parameter` 与 `localparam` 的区别，以及 `tpu_top` 如何把四个顶层参数层层传递给子模块。
- 推导出 `ORI_WIDTH = DATA_WIDTH+DATA_WIDTH+5 = 21` 这个 localparam 的来龙去脉，理解「乘积 16 位 + 5 位保护位」的含义。
- 看懂从 **8 位有符号输入 → 16 位乘积 → 21 位累加 → 16 位量化输出** 这一整条定点数值旅程，并理解每一段位宽为什么是这样。
- 动手把 `ARRAY_SIZE` 改成新值，准确判断「哪些位宽会自动跟随、哪些写死的常量不会跟随」，从而体会参数化设计的边界。

## 2. 前置知识

本讲承接 [u1-l3](u1-l3-tpu-top-datapath.md)。你已经知道 `tpu_top` 是一个纯结构化顶层：它不含任何 `always`/`assign` 逻辑，只做参数声明、端口声明、wire 声明与五处子模块例化。本讲就专门「放大」那张结构图里的数字——为什么端口是这些位宽、为什么中间结果 `ori_data` 是 168 位。

在进入源码前，先建立两个直觉。

**直觉一：定点数 = 把小数点「钉」在固定位上。**
硬件里没有真正的小数点，定点数（fixed-point）就是约定某一位是符号位、之后几位是小数位。常用记法 `Qm.n` 表示「m 位整数（含符号）、n 位小数」。例如 `Q4.4` 的 8 位数，小数点固定在第 4 位之后：

\[ \text{真实值} = \frac{\text{8位有符号整数}}{2^{4}} \]

**直觉二：乘法位数相加，累加位数要加保护位。**
两个 `Q4.4`（8 位）相乘，整数位相加、小数位相加，结果是 `Q8.8`（16 位）。而把 8 个这样的乘积加起来，可能溢出 16 位，所以累加器要更宽——这就是 `ORI_WIDTH` 多出的那几位「保护位」的来源。

理解了这两点，下面的源码就是把这些直觉「写死」成 Verilog。

## 3. 本讲源码地图

| 文件 | 在本讲的作用 |
| --- | --- |
| [rtl/tpu_top.v](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v) | 唯一的参数源头：声明四个 `parameter`、派生出 `ORI_WIDTH`，并把参数传递给子模块；同时用参数定义了所有端口的位宽。 |
| [rtl/systolic.v](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v) | 脉动阵列本体。用 `OUTCOME_WIDTH` 定义每个 cell 的累加器宽度，用 `DATA_WIDTH` 定义输入队列宽度，是「8→16→21」数值旅程的中间发生地。 |
| [rtl/quantize.v](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/quantize.v) | 把 21 位累加结果饱和量化回 16 位输出，完成「21→16」的最后一段旅程。 |

> 提醒：仓库里还有 `rtl/systolic array/` 这个带空格的教学副本目录，内容与本讲分析的 `rtl/` 核心源码逐字节相同。综合脚本只读 `rtl/`，本讲一律以 `rtl/` 为准（详见 [u1-l2](u1-l2-repo-structure.md)）。

## 4. 核心概念与源码讲解

### 4.1 参数声明：parameter 的声明与层层传递

#### 4.1.1 概念说明

`parameter` 是 Verilog 的「编译期常量」。它最大的价值在于**参数化**：把位宽、规模这些会变的量抽成一个名字，实例化时可以覆盖默认值，从而一份代码生成不同规模的硬件。

与 `localparam` 的区别：

- `parameter`：可以在**上层例化时被覆盖**（`.ARRAY_SIZE(4)`），是「对外的旋钮」。
- `localparam`：只能在模块**内部由表达式派生**，外部不能改，是「内部派生量」。

本项目的四个对外旋钮全部集中在 `tpu_top`：

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `ARRAY_SIZE` | 8 | 阵列边长，8 表示 8×8；文档提到可放大到 32×32 |
| `SRAM_DATA_WIDTH` | 32 | 外部 SRAM 一次读/写的字宽（32 位 = 4 字节） |
| `DATA_WIDTH` | 8 | 单个 weight/data 元素的位宽（8 位有符号定点） |
| `OUTPUT_DATA_WIDTH` | 16 | 量化后输出元素的位宽（16 位有符号定点） |

#### 4.1.2 核心流程

参数的流动是一条「自上而下」的单向链：

```text
tpu_top 声明 4 个 parameter（带默认值）
   │
   ├─→ 自己用它们计算 localparam ORI_WIDTH
   ├─→ 用它们定义所有端口位宽（sram_wdata 等）
   └─→ 例化子模块时，把需要的参数 .名(名) 显式传下去
         ├─→ systolic   收到 ARRAY_SIZE / SRAM_DATA_WIDTH / DATA_WIDTH
         ├─→ quantize   收到 ARRAY_SIZE / SRAM_DATA_WIDTH / DATA_WIDTH / OUTPUT_DATA_WIDTH
         ├─→ write_out  收到 ARRAY_SIZE / OUTPUT_DATA_WIDTH
         └─→ systolic_controll 收到 ARRAY_SIZE
```

关键约定：**每经过一层例化，参数名必须显式写出来**（`.ARRAY_SIZE(ARRAY_SIZE)`）。不写则子模块用它自己的默认值——本项目默认值碰巧一致，所以「不写也能跑」，但那是巧合，不是设计。

#### 4.1.3 源码精读

**参数声明的源头**——四个 `parameter` 带默认值，就在 `tpu_top` 模块名之后：[rtl/tpu_top.v:L1-L6](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L1-L6) 声明了 `ARRAY_SIZE/SRAM_DATA_WIDTH/DATA_WIDTH/OUTPUT_DATA_WIDTH`。

**参数直接决定端口位宽**。最典型的例子是三组写回端口的数据线：

```verilog
output [ARRAY_SIZE*OUTPUT_DATA_WIDTH-1:0] sram_wdata_a,
```

完整声明见 [rtl/tpu_top.v:L28-L37](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L28-L37)。代入默认值就是 `8*16-1 = 127`，即 128 位。注意 `sram_waddr_a/b/c` 仍是写死的 `[5:0]`（6 位），**并不**由参数派生——这是后面实践题要抓的要点。

**参数向子模块传递**。以 `quantize` 的例化为例，四个参数被显式点名传下去：[rtl/tpu_top.v:L80-L85](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L80-L85)。`systolic` 的例化则只传三个参数（它不需要 `OUTPUT_DATA_WIDTH`）：[rtl/tpu_top.v:L95-L99](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L95-L99)。

**子模块侧接收参数**。`systolic` 在模块头声明同名参数（带相同默认值）：[rtl/systolic.v:L4-L8](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L4-L8)；`quantize` 同理：[rtl/quantize.v:L3-L8](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/quantize.v#L3-L8)。这种「上层 `.X(X)`、下层同名默认值」是参数化模块的标准写法。

#### 4.1.4 代码实践

**目标**：体会「参数是旋钮」，并亲手拧它一次。

1. 打开 [rtl/tpu_top.v:L1-L6](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L1-L6)，**只读不改**地确认四个参数默认值。
2. 想象在更上一层（例如 testbench）例化 `tpu_top` 时写成 `tpu_top #(.ARRAY_SIZE(4)) u_tpu (...)`。
3. 用纸笔（不跑仿真）推算：`sram_wdata_a` 的位宽表达式 `ARRAY_SIZE*OUTPUT_DATA_WIDTH` 在 `ARRAY_SIZE=4` 时变成多少？
4. 对照 [rtl/tpu_top.v:L80-L85](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L80-L85)，确认 `quantize` 是否也会跟着收到 `ARRAY_SIZE=4`（答案：会，因为参数是显式点名传递的）。

**需要观察的现象**：只要上层覆盖 `ARRAY_SIZE`，所有用 `ARRAY_SIZE*...` 表达的位宽都会一齐改变，无需逐个修改。

**预期结果**：`sram_wdata_a` 从 128 位变为 64 位（`4*16`），`quantize`/`write_out`/`systolic` 都自动收到 `ARRAY_SIZE=4`。

> 注意：本实践是「纸面推算」，不要求你真的改源码并跑通——因为改了也跑不通（见 4.3.4 的原因）。本讲全程**不修改源码**。

#### 4.1.5 小练习与答案

**练习 1**：`systolic_controll` 的例化只传了 `ARRAY_SIZE` 一个参数（[rtl/tpu_top.v:L120-L122](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L120-L122)），它内部能正确知道 `DATA_WIDTH` 吗？

**答案**：能，但用的是它**自己**的默认值而非顶层传下来的。因为 `tpu_top` 没有显式 `.DATA_WIDTH(DATA_WIDTH)`，`systolic_controll` 就退回自身模块头里写的默认值。本项目默认值一致所以没暴露问题，但这属于「隐式依赖」，不够健壮。

**练习 2**：`parameter` 和 `localparam` 能否互换使用？

**答案**：不能随便换。需要让上层覆盖的量（如 `ARRAY_SIZE`）必须用 `parameter`；纯内部派生、不希望被外部改的量（如 `ORI_WIDTH`）用 `localparam` 更安全，能防止误覆盖。

---

### 4.2 ORI_WIDTH 与 OUTCOME_WIDTH：localparam 派生位宽

#### 4.2.1 概念说明

`ORI_WIDTH` 是本项目最关键的「派生位宽」。它出现在两个地方、写法略有不同，但含义完全一致：

- `tpu_top` 里：`localparam ORI_WIDTH = DATA_WIDTH+DATA_WIDTH+5;`
- `systolic` / `quantize` 里：等价的 `OUTCOME_WIDTH = DATA_WIDTH+DATA_WIDTH+5`，以及内联写法 `(DATA_WIDTH+DATA_WIDTH+5)`。

这三个名字（`ORI_WIDTH`、`OUTCOME_WIDTH`、以及直接写出的表达式）指向**同一个数：21**。它们都表示「单个 cell 一次完整乘加后的中间结果位宽」。

为什么是 21？拆成两部分理解：

\[ \text{ORI\_WIDTH} = \underbrace{\text{DATA\_WIDTH} + \text{DATA\_WIDTH}}_{\text{乘积位宽}=16} + \underbrace{5}_{\text{保护位}} = 21 \]

- 前两项 `8+8=16`：两个 8 位有符号数相乘，结果最多 16 位（这是定点乘法「位数相加」的性质）。
- `+5`：累加保护位。一个 cell 要把 `ARRAY_SIZE=8` 个乘积累加起来，理论上每翻倍加 1 位，8 个值需要 \(\lceil\log_2 8\rceil = 3\) 位；设计取 5 位，多出 2 位作安全余量。

#### 4.2.2 核心流程

派生位宽的「数学骨架」如下（默认参数下）：

\[ \text{DATA\_WIDTH} = 8 \;\Rightarrow\; \text{乘积} = 8+8 = 16\text{ 位} \;\Rightarrow\; \text{ORI\_WIDTH} = 16+5 = 21\text{ 位} \]

于是整条数据通路的位宽都可由这两个量算出：

| 信号 | 位宽表达式 | 默认值 | 出处 |
| --- | --- | --- | --- |
| 单个乘积 `mul_result` | `DATA_WIDTH+DATA_WIDTH` | 16 | `systolic.v` |
| 单个累加器 `matrix_mul_2D[i][j]` | `OUTCOME_WIDTH` | 21 | `systolic.v` |
| 整批输出 `mul_outcome` / `ori_data` | `ARRAY_SIZE*OUTCOME_WIDTH` | `8*21=168` | `systolic.v` / `tpu_top.v` |
| 量化输出 `quantized_data` | `ARRAY_SIZE*OUTPUT_DATA_WIDTH` | `8*16=128` | `quantize.v` / `tpu_top.v` |

注意一个关键区别：`OUTCOME_WIDTH` 只依赖 `DATA_WIDTH`，**与 `ARRAY_SIZE` 无关**。所以无论阵列是 8×8 还是 32×32，单个 cell 的累加器都是 21 位；变的只是「有多少个 cell」和「整批拼起来有多宽」。

#### 4.2.3 源码精读

**`tpu_top` 里的派生**：[rtl/tpu_top.v:L41](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L41) —— `localparam ORI_WIDTH = DATA_WIDTH+DATA_WIDTH+5;`。这是顶层唯一的一个 localparam，下面两根 wire 直接用它：

```verilog
wire signed [ARRAY_SIZE*ORI_WIDTH-1:0] ori_data;                  // 8*21 = 168 位
wire signed [ARRAY_SIZE*OUTPUT_DATA_WIDTH-1:0] quantized_data;    // 8*16 = 128 位
```

见 [rtl/tpu_top.v:L47-L48](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L47-L48)。`ori_data` 就是 `systolic` 输出、`quantize` 输入的那根 168 位总线（在 `tpu_top` 里它叫 `ori_data`，在 `systolic` 里叫 `mul_outcome`，是同一根线）。

**`systolic` 里的同名派生**：[rtl/systolic.v:L26-L28](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L26-L28) 定义了三个 localparam，其中 `OUTCOME_WIDTH = DATA_WIDTH+DATA_WIDTH+5` 与顶层 `ORI_WIDTH` 完全等价。`FIRST_OUT`、`PARALLEL_START` 则是控制器时序用的阈值（详见 [u2-l2](u2-l2-mac-accumulate.md)）。

`systolic` 的输出端口直接把表达式写进位宽：[rtl/systolic.v:L23](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L23) `output reg signed [(ARRAY_SIZE*(DATA_WIDTH+DATA_WIDTH+5))-1:0] mul_outcome` —— 即 168 位，与 `tpu_top` 的 `ori_data` 严丝合缝。

**`quantize` 里的同名派生**：[rtl/quantize.v:L15](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/quantize.v#L15) 又写了一遍 `localparam ORI_WIDTH = DATA_WIDTH+DATA_WIDTH+5;`。它的输入端口 `ori_data` 也用内联表达式：[rtl/quantize.v:L10](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/quantize.v#L10)。

> 小结：同一个 21 被写了三遍（`ORI_WIDTH`、`OUTCOME_WIDTH`、内联表达式）。这是本项目的一个可改进点——理想做法是在一处定义、处处引用，避免「改了一处忘了另一处」。

#### 4.2.4 代码实践

**目标**：验证「`OUTCOME_WIDTH` 只随 `DATA_WIDTH` 变，不随 `ARRAY_SIZE` 变」。

1. 读 [rtl/systolic.v:L28](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L28)，确认 `OUTCOME_WIDTH` 表达式里**没有** `ARRAY_SIZE`。
2. 读 [rtl/systolic.v:L30](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L30)，确认每个 `matrix_mul_2D[i][j]` 的元素宽度是 `OUTCOME_WIDTH`（21 位），与阵列规模无关。
3. 假想 `DATA_WIDTH` 从 8 改成 16（其余不变），纸面推算 `ORI_WIDTH`、`mul_result`、单个累加器的新位宽。

**需要观察的现象**：改 `DATA_WIDTH` 会同时搬动「乘积→累加器→整批」三级的位宽；改 `ARRAY_SIZE` 只搬动「整批」这一级，单个 cell 的位宽纹丝不动。

**预期结果**：`DATA_WIDTH=16` 时，`mul_result=32` 位，`ORI_WIDTH=16+16+5=37` 位，单个累加器变 37 位；而无论 `ARRAY_SIZE` 多大，单个累加器始终 21 位（在 `DATA_WIDTH=8` 下）。

**待本地验证**：以上为纸面推算，若你有仿真环境可进一步把 `DATA_WIDTH` 调大并观察综合工具报出的端口位宽是否吻合（注意：与 `sram_rdata` 切片逻辑耦合，直接改可能跑不通，仅作位宽推算练习）。

#### 4.2.5 小练习与答案

**练习 1**：为什么保护位取 5 而不是理论最小的 3？

**答案**：累加 8 个 16 位乘积，理论需要 `16+⌈log₂8⌉=16+3=19` 位即可不溢出。取 5 位（共 21 位）多出 2 位余量，给极端输入或后续可能的累加轮次留安全空间。这是一种偏保守的工程选择。

**练习 2**：`tpu_top` 的 `ORI_WIDTH` 和 `systolic` 的 `OUTCOME_WIDTH` 是不是必须相等？如果不等会怎样？

**答案**：必须相等，否则 `systolic` 输出的 `mul_outcome` 与 `tpu_top` 声明的 `ori_data` 位宽不匹配，连不上。正因为两者表达式都是 `DATA_WIDTH+DATA_WIDTH+5`，只要 `DATA_WIDTH` 一致就天然相等——这也是为什么参数要层层传同一个 `DATA_WIDTH`。

---

### 4.3 有符号定点数与位宽推导：8 → 16 → 21 → 16 的数值旅程

#### 4.3.1 概念说明

本节把三个模块串起来，看一个具体数值从输入到输出经历了什么。先确定三段的定点格式：

| 阶段 | 位宽 | 定点格式 | 取值范围（真实值） |
| --- | --- | --- | --- |
| 输入 weight / data | 8 | Q4.4（4 整 4 小，有符号） | \([-8.0,\; +7.9375]\)，分辨率 \(1/16\) |
| 单次乘积 `mul_result` | 16 | Q8.8（8 整 8 小） | 约 \(\pm 63\) |
| 累加器 `matrix_mul_2D[i][j]` | 21 | Q13.8（13 整 8 小，含余量） | 远大于实际需要 |
| 量化输出 | 16 | Q8.8（8 整 8 小） | \([-128,\; +127.99]\)，饱和到 \(\pm 32767\) 原始码 |

**关键性质：有符号乘法的位数相加。** 两个 `signed [7:0]`（Q4.4）相乘，结果是 Q8.8 的 16 位有符号数。整数位 \(4+4=8\)，小数位 \(4+4=8\)，共 16 位（其中最高位含一个冗余符号位，这是有符号乘法的正常现象）。

**关键性质：符号扩展。** 把 16 位乘积加进 21 位累加器前，必须先把 16 位「拉长」到 21 位且保持数值不变——这靠复制符号位完成。

#### 4.3.2 核心流程

一个 cell 完成一次乘加的数值流程（伪代码）：

```text
w = weight_queue[i][j]        // signed 8 位, Q4.4
d = data_queue[i][j]          // signed 8 位, Q4.4
mul_result = w * d            // signed 16 位, Q8.8
ext = 符号扩展(mul_result, 21位)   // 复制 bit[15] 共 5 次, 拼到高位
matrix_mul_2D[i][j] = matrix_mul_2D[i][j] + ext   // 21 位累加, Q13.8
```

最终量化时：

```text
if (累加值 >= +32767)  输出 = +32767        // 正向饱和
else if (累加值 <= -32768) 输出 = -32768   // 负向饱和
else 输出 = 累加值的低 16 位                 // 数值恰好落在 16 位范围内, 截取即可
```

注意最后一步：累加器是 Q13.8（8 位小数），输出也是 Q8.8（8 位小数），**小数位相同**，所以截取低 16 位不会改变小数点的位置——只是去掉了多余的高位整数位。前提是饱和判断已经保证数值落在 16 位能表示的范围内。

符号扩展的数学表达：对 16 位有符号数 \(x\)，其符号位是 \(x_{15}\)，扩展到 21 位为

\[ x_{\text{ext}} = \{\,\underbrace{x_{15},x_{15},x_{15},x_{15},x_{15}}_{5\text{ 位}},\; x_{15..0}\,\} \]

正数（\(x_{15}=0\)）高位补 0，负数（\(x_{15}=1\)）高位补 1，数值不变。

#### 4.3.3 源码精读

**输入队列是有符号 8 位**：[rtl/systolic.v:L32-L33](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L32-L33) 声明 `data_queue`、`weight_queue` 为 `reg signed [DATA_WIDTH-1:0]`（即 signed 8 位）。`signed` 关键字让综合工具把它们当有符号数处理，乘法才会得到正确的有符号结果。

**乘积寄存器是 16 位有符号**：[rtl/systolic.v:L35](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L35) `reg signed [DATA_WIDTH+DATA_WIDTH-1:0] mul_result;`（16 位）。

**乘法 + 符号扩展 + 累加**，这是本节的核心代码：

```verilog
mul_result = weight_queue[i][j] * data_queue[i][j];
matrix_mul_2D_nx[i][j] = matrix_mul_2D[i][j] + { {5{mul_result[15]}} , mul_result };
```

见 [rtl/systolic.v:L97-L103](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L97-L103)。逐字解读 `{ {5{mul_result[15]}} , mul_result }`：

- `mul_result[15]` 是 16 位乘积的符号位。
- `{5{...}}` 把这个符号位复制 5 份。
- 外层 `{...,...}` 把 5 位符号扩展拼到 16 位乘积前面，得到 21 位有符号数，与 `matrix_mul_2D[i][j]`（21 位）相加。

同一段里还有「首入」分支（不累加、直接装入符号扩展后的乘积）：[rtl/systolic.v:L98-L99](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L98-L99)，同样用 `{ {5{mul_result[15]}} , mul_result }` 把 16 位扩成 21 位。两条分支的位宽处理一致。

**量化饱和**：[rtl/quantize.v:L14](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/quantize.v#L14) 定义 `max_val = 32767, min_val = -32768`（即 16 位有符号的上下界）。饱和三分支见 [rtl/quantize.v:L22-L29](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/quantize.v#L22-L29)：

```verilog
ori_shifted_data = ori_data[i*ORI_WIDTH +: ORI_WIDTH];          // 取出 21 位
if(ori_shifted_data >= max_val) ... = max_val;                  // 正饱和
else if(ori_shifted_data <= min_val) ... = min_val;             // 负饱和
else ... = ori_shifted_data[OUTPUT_DATA_WIDTH-1:0];             // 截低 16 位
```

这里 `ori_data[i*ORI_WIDTH +: ORI_WIDTH]` 用 Verilog 的「基地址 +: 位宽」切片，从 168 位总线里精确切出第 `i` 个 21 位 cell 结果。

> **文档与代码的不一致（重要）**：[rtl/quantize.v:L21](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/quantize.v#L21) 的注释写着「from 32 bit(16: integer, 8: precision)」，但实际 `ORI_WIDTH=21`、`OUTPUT_DATA_WIDTH=16`。这条注释是过时的，**以代码为准**。变量名 `ori_shifted_data` 也带误导——代码里并没有移位操作，只是切片。学会「信代码、慎信注释」是读源码的重要习惯。

#### 4.3.4 代码实践（本讲核心实践）

**目标**：把 `ARRAY_SIZE` 从 8 改成 4，准确判断「哪些位宽自动跟随、哪些不会」，从而真正理解参数化的边界。

**操作步骤（纸面推算，不改源码不跑仿真）**：

1. **会自动跟随的部分**（凡是参数表达式里含 `ARRAY_SIZE` 的，都会变）：
   - 阵列规模：`matrix_mul_2D`、`matrix_mul_2D_nx`、`data_queue`、`weight_queue` 从 `[0:7][0:7]` 变成 `[0:3][0:3]`，cell 数从 64 变 16。依据 [rtl/systolic.v:L30-L33](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L30-L33)。
   - 整批输出宽度：`mul_outcome` / `ori_data` 从 `8*21=168` 位变 `4*21=84` 位。依据 [rtl/systolic.v:L23](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L23) 与 [rtl/tpu_top.v:L47](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L47)。
   - 量化输出宽度：`quantized_data`、`sram_wdata_a/b/c` 从 `8*16=128` 位变 `4*16=64` 位。依据 [rtl/quantize.v:L11](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/quantize.v#L11) 与 [rtl/tpu_top.v:L28-L37](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L28-L37)。
   - **不变**的部分：`mul_result`（16 位）、`OUTCOME_WIDTH`/`ORI_WIDTH`（21 位）、单个累加器（21 位）——它们只依赖 `DATA_WIDTH`，与 `ARRAY_SIZE` 无关。

2. **不会自动跟随的部分**（写死的常量，改 `ARRAY_SIZE` 后会出错或失配）：
   - **SRAM 喂入循环写死成 4**：[rtl/systolic.v:L56](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L56) 和 [rtl/systolic.v:L66](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L66) 的 `for(i=0; i<4; ...)`。它假设「32 位 SRAM 字 = 4 个字节」，每拍从 `w0`/`w1` 各喂 4 个、共 8 个进第 0 行。`ARRAY_SIZE=4` 时第 0 行只有 4 格，而代码仍会写 `weight_queue[0][i+4]`、`data_queue[i+4][0]`（[L58](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L58)、[L68](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L68)），下标 `4..7` 越界。这个 `4` 实际是 `SRAM_DATA_WIDTH/DATA_WIDTH`，却写成了字面量。
   - **取模写死成 16**：[rtl/systolic.v:L97](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L97) 的 `%16`，其实代表 `2*ARRAY_SIZE`。`ARRAY_SIZE=4` 时应改成 `%8`，否则反对角线调度逻辑全错。
   - **SRAM 字切片写死成 31/8**：`sram_rdata_w0[31-8*i-:8]`（[L57](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L57) 等）把 31、8 写死，隐含 `SRAM_DATA_WIDTH=32`、`DATA_WIDTH=8`。它与 `ARRAY_SIZE` 无关，所以本次改 `ARRAY_SIZE` 不受影响——但说明它也不是真正的参数化。
   - **地址/索引位宽写死**：`sram_waddr_a/b/c` 是 `[5:0]`（[tpu_top.v:L29](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L29) 等），`addr_serial_num` 是 `[6:0]`（[tpu_top.v:L44](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L44)），`cycle_num [8:0]`、`matrix_index [5:0]`（[tpu_top.v:L52-L53](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L52-L53)）。这些都按 8×8 规模固定，不会随 `ARRAY_SIZE` 缩放（对小规模通常仍够用，但大规模会不够）。

**需要观察的现象**：改一个 `ARRAY_SIZE`，「位宽」层面自动正确，「喂入与调度逻辑」层面却埋了三个坑（`i<4`、`i+4`、`%16`）。

**预期结论**：本项目的参数化是**半参数化**——位宽骨架随参数走，但若干关键常量仍按 `ARRAY_SIZE=8` 写死。所以「文档说可扩展到 32×32」要打折扣：直接改参数无法即插即用，还需同步修正上述写死量。

**待本地验证**：若你有仿真环境，可在副本里把 `ARRAY_SIZE` 设为 4 并修正上述三处后跑 testbench，观察结果是否正确；本讲不要求实际修改源码。

#### 4.3.5 小练习与答案

**练习 1**：把 `w = 0111_1111`（Q4.4）、`d = 0111_1111`（Q4.4）相乘，`mul_result` 的 16 位原始码是多少？真实值是多少？

**答案**：`w = 127/16 = 7.9375`，`d = 7.9375`。真实积 \(= 7.9375^2 \approx 63.0039\)。在 Q8.8 下原始码 \(= 63.0039 \times 256 \approx 16129 = \text{0x3F01}\)。16 位码为 `0011_1111_0000_0001`。最高位为 0，符号扩展时高位补 0。

**练习 2**：如果去掉饱和逻辑，直接对 `ori_shifted_data` 取低 16 位，什么输入下会出错？

**答案**：当累加结果超出 16 位有符号范围（\(> +32767\) 或 \(< -32768\)）时会出错——此时高位丢失，低 16 位的符号和数值都被破坏，表现为「突然变号」或「跳变」。例如正向溢出时本应是大正数，截断后最高位可能变成 1 而被解读成大负数。饱和逻辑正是为了在这种边界情况下把结果「钳住」在可表示范围内。

**练习 3**：`{ {5{mul_result[15]}} , mul_result }` 一共多少位？为什么是 5 而不是别的数？

**答案**：\(5 + 16 = 21\) 位，正好等于 `ORI_WIDTH`。之所以是 5，是因为累加器宽度 `ORI_WIDTH = DATA_WIDTH+DATA_WIDTH+5`，比 16 位乘积正好多 5 位，符号扩展就必须补 5 位才能与累加器对齐相加。这个 5 与 `ORI_WIDTH` 里的 5 是同一个数，但在源码里被写成了字面量，没有用 `ORI_WIDTH-16` 之类的表达式——又是 4.3.4 里「半参数化」的一个佐证。

## 5. 综合实践

把本讲三个最小模块串成一个任务：**为 TPU 画一张「位宽推导表」并标注参数化健康度**。

1. 画一张表，四列分别是：**信号名 / 位宽表达式 / 代入默认值的数值 / 依赖哪个参数**。把以下信号填进去：`weight_queue` 元素、`mul_result`、`matrix_mul_2D[i][j]`、`mul_outcome`、`ori_data`、`quantized_data`、`sram_wdata_a`、`sram_waddr_a`、`addr_serial_num`。
2. 在「依赖哪个参数」列里，凡是用字面量写死的（如 `[5:0]`、`%16`、`i<4`），标注「**写死**」并写出它真正应该等于哪个参数表达式（例如 `i<4` 应为 `i < SRAM_DATA_WIDTH/DATA_WIDTH`）。
3. 基于这张表写一段结论：如果要把阵列从 8×8 改成 4×4，需要修改哪几处源码？改成 32×32 又需要修改哪几处？两者的修改清单有何不同？

这个任务逼你把「参数声明 → localparam 派生 → 有符号位宽推导」三件事全部走一遍，并且亲手发现参数化的边界——这正是本讲的核心收获。

参考答案要点（你可以先自己填再对照）：

- 真正随 `ARRAY_SIZE` 走的：所有 `ARRAY_SIZE*...` 表达的整批位宽与二维队列规模。
- 写死的：`i<4`、`i+4`（应为 `SRAM_DATA_WIDTH/DATA_WIDTH` 相关）、`%16`（应为 `2*ARRAY_SIZE`）、各 `[5:0]/[6:0]/[8:0]` 地址位宽、`[31-8*i-:8]` 切片。
- 4×4 与 32×32 的差异：4×4 主要是「下标越界 + 调度取模」两类问题；32×32 除上述外，还会遇到地址位宽不够（如 32 行需要更多地址位）、单批数据量翻倍等规模性问题，修改面更大。

## 6. 本讲小结

- `tpu_top` 用四个 `parameter`（`ARRAY_SIZE/SRAM_DATA_WIDTH/DATA_WIDTH/OUTPUT_DATA_WIDTH`）作为对外旋钮，例化时以 `.名(名)` 显式传给子模块。
- `localparam ORI_WIDTH = DATA_WIDTH+DATA_WIDTH+5 = 21` 是内部派生量，决定单个 cell 的累加器宽度；`systolic` 的 `OUTCOME_WIDTH` 与它等价（同一公式写了三遍）。
- 定点数值旅程为 **8 位 Q4.4 输入 → 16 位 Q8.8 乘积 → 21 位 Q13.8 累加 → 16 位 Q8.8 饱和输出**；其中「+5」是累加保护位（理论 3 位即够，多 2 位余量）。
- 符号扩展 `{ {5{mul_result[15]}} , mul_result }` 把 16 位乘积无损拉长到 21 位以参与累加；量化则用饱和到 ±32767/−32768 保证截取低 16 位不破坏数值。
- `OUTCOME_WIDTH` 只依赖 `DATA_WIDTH`，与 `ARRAY_SIZE` 无关——改阵列规模不动单个 cell 的位宽。
- 本项目是**半参数化**：位宽骨架随参数走，但 `i<4`、`i+4`、`%16`、地址位宽等仍按 8×8 写死，「文档称可扩到 32×32」不能即插即用。同时 `quantize.v` 的注释（"32 bit"）与代码（21 位）不符，需以代码为准。

## 7. 下一步学习建议

- 下一讲 [u1-l5](u1-l5-sim-env-tb-sram.md) 进入仿真环境，你会看到这些参数化的位宽在 testbench 里如何与 SRAM 行为模型对接（例如 `sram_16x128b` 正好对应 128 位的 `sram_wdata`）。
- 进入第二单元后，[u2-l1](u2-l1-shift-queues.md) 会精读 `weight_queue`/`data_queue` 的移位逻辑——本讲只看了它们的位宽声明，那里会看 8 位数据如何流动。
- 想深入「+5 保护位是否够」的读者，可在 [u2-l2](u2-l2-mac-accumulate.md) 结合 `cycle_num` 调度，分析一个 cell 到底累加多少次、极端输入下 21 位是否会溢出。
- 对参数化健壮性感兴趣的读者，可以思考一个改进练习：把 `i<4`、`%16` 改写成由 `SRAM_DATA_WIDTH/DATA_WIDTH/ARRAY_SIZE` 派生的表达式，让阵列真正可缩放（注意：这是思考题，请勿修改仓库源码）。

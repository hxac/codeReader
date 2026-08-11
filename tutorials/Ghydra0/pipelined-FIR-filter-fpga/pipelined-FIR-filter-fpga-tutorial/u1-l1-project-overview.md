# 项目概览与 FIR 滤波器入门

## 1. 本讲目标

学完本讲后，你应该能够：

- 用一句话说清楚这个项目在做什么——用 Verilog 在 FPGA 上实现一个**流水线 FIR 滤波器**。
- 看懂 README 里给出的两种「运行方式」（TCL 重建工程 / 手动添加源文件），并知道哪种方式在刚 clone 的仓库里更可靠。
- 用一句中文讲清 FIR 滤波器的本质：对当前与过去的若干输入样本做**加权求和**。
- 在源码中找到顶层模块名、默认滤波器类型（moving average）和默认系数（`16'h2000`）。

本讲是整本学习手册的第一篇，**不要求你懂 Verilog 或 FPGA**。我们只建立「项目是什么、怎么跑、核心算法在干什么」的直觉，细节模块留给后续讲义。

## 2. 前置知识

本讲用通俗语言解释一切，但下面几个名词先混个眼熟即可，不懂也没关系，正文会展开：

- **FPGA（现场可编程门阵列）**：一块可以通过代码「重新连线」的芯片。你写硬件描述代码，工具把它「编译」成芯片里的真实电路。
- **HDL（硬件描述语言）**：描述硬件行为的语言，本项目用的是 **Verilog**（文件后缀 `.v`）。
- **Vivado**：AMD/Xilinx 公司的官方开发工具，用来把 Verilog 代码综合（翻译）成 FPGA 能烧录的比特流，也用来做仿真。本项目的工程文件 `Fir_filter.tcl` 就是 Vivado 生成的。
- **FIR 滤波器**：一种数字滤波器，本讲第 4.3 节会详细讲。现在只要记住它是「对输入信号做加权求和」即可。
- **流水线（pipeline）**：把一个大计算拆成多级，每级之间加寄存器，让数据像流水一样一级一级往前走，从而提高时钟频率。本项目把每个滤波器「抽头（tap）」做成一级流水线。

如果你对「加权求和」这个说法陌生，可以这样理解：期末总评成绩 = 平时 ×30% + 期中 ×30% + 期末 ×40%。这里的 30%、30%、40% 就是「权重（系数）」，把几项成绩按权重加起来就是「加权求和」。FIR 滤波器干的就是这件事，只不过加的是「一段时间内的输入样本」。

## 3. 本讲源码地图

本讲只看两份「入口性」文件，它们决定了项目的定位和运行方式：

| 文件 | 作用 | 本讲用来看什么 |
| --- | --- | --- |
| `README.md` | 项目的使用说明。告诉怎么运行、默认滤波器类型、可调参数。 | 顶层模块、运行步骤、默认系数。 |
| `Fir_filter.tcl` | Vivado 自动生成的工程重建脚本。记录了目标器件、顶层模块、源文件清单、仿真顶层等。 | 目标 part、综合顶层、仿真顶层。 |

为了把「FIR 滤波器概念」讲扎实，本讲还会**引用但不下钻**两份 Verilog 源文件（它们是后续讲义的主角，这里只用最顶层的一两眼）：

| 文件 | 作用 |
| --- | --- |
| `Fir_filter.srcs/sources_1/new/fir_filter.v` | 滤波器**顶层模块**，定义端口和系数，并用 `generate` 把多个 tap 串成流水线。 |
| `Fir_filter.srcs/sources_1/new/fir_filter_tb.v` | **仿真测试台（testbench）**，给滤波器喂激励、打印输出。 |

完整的源文件清单（后续讲义会逐个深入）：

```
Fir_filter.srcs/sources_1/new/adder.v          # 加法器（叶子模块）
Fir_filter.srcs/sources_1/new/delay.v          # 寄存器/延迟（叶子模块）
Fir_filter.srcs/sources_1/new/multiplier.v     # Q15 定点乘法器
Fir_filter.srcs/sources_1/new/fir_tap.v        # 单级流水线抽头
Fir_filter.srcs/sources_1/new/fir_filter.v     # 顶层模块
Fir_filter.srcs/sources_1/new/fir_filter_tb.v  # 仿真测试台
Fir_filter.srcs/constrs_1/imports/Vivado_projects/Nexys-A7-100T-Master.xdc  # 引脚/时序约束
Fir_filter.tcl                                 # Vivado 工程重建脚本
README.md                                      # 使用说明
```

## 4. 核心概念与源码讲解

本讲的三个最小模块：**项目定位**、**运行方式**、**FIR 滤波器概念**。

### 4.1 项目定位

#### 4.1.1 概念说明

先看 README 的第一行，它一句话给出了项目定位：

> A pipelined implementation of an FIR filter for FPGA's

把它拆成三个关键词：

1. **FIR filter（FIR 滤波器）**：这是项目要实现的**算法**。本质上是对输入信号做加权求和（详见 4.3）。
2. **for FPGA（面向 FPGA）**：这个算法不是跑在 CPU 上的软件，而是被做成**真实硬件电路**烧进 FPGA 芯片。所以代码是 Verilog（硬件描述语言），而不是 Python/C。
3. **pipelined（流水线式）**：实现方式上，把滤波器拆成多级「抽头」，每级之间插入寄存器，让数据逐拍（每个时钟周期）往前流。这样可以把时钟频率拉高（详见后续「单级 tap」讲义）。

一句话定位：**本项目用 Verilog 写了一个流水线结构的 FIR 滤波器，目标是 Artix-7 系列 FPGA。**

> 目标器件的准确型号写在 TCL 里：`xc7a100tcsg324-1`，这是 Digilent **Nexys A7-100T** 开发板上那颗 Artix-7 芯片。即使你没有这块板子，也完全可以用 Vivado 仿真来学习。

#### 4.1.2 核心流程

从「外部视角（黑盒）」看，这个滤波器只有三个接口：

```text
         ┌─────────────────────────────┐
  clk ──▶│                             │
         │      pipelined FIR filter   │──▶ yn  (输出样本)
  xn  ──▶│                             │
         └─────────────────────────────┘
```

- `clk`：时钟，每个上升沿数据往前走一级。
- `xn`：当前输入样本（一个 16 位数）。
- `yn`：当前输出样本（一个 16 位数）。

数据流（高层）：`xn` 每个时钟进入流水线，依次经过多个 tap（每个 tap 负责「延迟一次 × 乘以系数 → 加进累加和」），最后从第 0 级流出成为 `yn`。**每个 tap 是一级流水线**这件事是「pipelined」的全部含义，细节留给后续讲义。

#### 4.1.3 源码精读

顶层模块的端口定义在 `fir_filter.v`，正好对应上面的黑盒：

[README.md:L1-L3](https://github.com/Ghydra0/pipelined-FIR-filter-fpga/blob/4c6aedf850b021e36fcb0a3d86e0902704594eeb/README.md#L1-L3) — README 一句话定位项目：FPGA 上的流水线 FIR 实现。

[fir_filter.v:L23-L27](https://github.com/Ghydra0/pipelined-FIR-filter-fpga/blob/4c6aedf850b021e36fcb0a3d86e0902704594eeb/Fir_filter.srcs/sources_1/new/fir_filter.v#L23-L27) — 顶层模块 `fir_filter` 的端口：`clk`、输入 `xn`、输出 `yn`，位宽由参数 `WIDTH` 控制（默认 16）。

```verilog
module fir_filter #(parameter WIDTH = 16, parameter TAPS = 4) (
    input clk,
    input [WIDTH-1:0] xn, // input sample
    output [WIDTH-1:0] yn // output sample
    );
```

注意两个参数：

- `WIDTH = 16`：每个样本和系数都是 16 位。
- `TAPS = 4`：滤波器有 4 个抽头（4 个系数）。

> 这一小段代码已经能回答「顶层模块叫什么」——叫 `fir_filter`。

#### 4.1.4 代码实践

**实践目标**：亲手从源码里确认顶层模块名、端口方向和位宽参数。

**操作步骤**：

1. 打开 `Fir_filter.srcs/sources_1/new/fir_filter.v`，找到 `module fir_filter ...` 这一行（第 23 行）。
2. 找出：模块名、有几个端口、哪些是 `input` 哪些是 `output`、`WIDTH` 和 `TAPS` 的默认值。

**需要观察的现象**：

- 模块名是 `fir_filter`。
- 端口共 3 个：`clk`（输入）、`xn`（输入）、`yn`（输出）。
- `WIDTH` 默认 16，`TAPS` 默认 4。

**预期结果**：与你从 README 推断出的「16 位、4 抽头」一致。

#### 4.1.5 小练习与答案

**练习 1**：项目的顶层模块名是什么？综合（synthesis）用的顶层和仿真用的顶层是同一个吗？

> **答案**：综合顶层是 `fir_filter`（见 `fir_filter.v`）。仿真顶层不是它，而是测试台 `fir_filter_tb`（由 TCL 指定，见 4.2.3）。

**练习 2**：为什么说这个项目是「硬件项目」而不是「软件项目」？

> **答案**：因为它用 Verilog（硬件描述语言）描述电路，最终通过 Vivado 综合成 FPGA 芯片里的真实逻辑门/寄存器，而不是编译成 CPU 上顺序执行的机器码。

---

### 4.2 运行方式

#### 4.2.1 概念说明

「运行」硬件代码有两种含义，初学者要先分清：

- **仿真（simulation）**：在电脑上用 Vivado 模拟电路行为，看波形、打印输出。**不需要 FPGA 板子**，是学习阶段最常用的方式。本项目提供了测试台 `fir_filter_tb.v`。
- **综合实现上板（synthesis → implementation → bitstream）**：把代码编译成比特流，烧进真实 FPGA。需要 Nexys A7-100T 板子。

README 给了三条使用步骤，归纳起来是两条路径：

1. **TCL 重建工程**：用 `Fir_filter.tcl` 一键重建整个 Vivado 工程。
2. **手动添加源文件**：自己新建一个 Vivado 工程，把 `.v` 文件加进去。

#### 4.2.2 核心流程

**路径 A：TCL 重建（README 推荐，但在刚 clone 的仓库里有个坑）**

README 给的命令：

[README.md:L4-L9](https://github.com/Ghydra0/pipelined-FIR-filter-fpga/blob/4c6aedf850b021e36fcb0a3d86e0902704594eeb/README.md#L4-L9) — README 的 How to use 三步：clone 后用 Vivado 打开；用 TCL 重建；或直接用源文件。

伪代码：

```text
1. git clone <仓库>
2. cd 进仓库
3. vivado -mode tcl -source Fir_filter.tcl   # 注意：README 里写作 my_project.tcl，实际文件名是 Fir_filter.tcl
```

> ⚠️ **重要提示（待本地验证）**：直接在 clone 的仓库里 `source Fir_filter.tcl` **大概率会报找不到源文件**。因为 TCL 里写死的源文件路径是 `${origin_dir}/../Vivado_projects/Fir_filter/...`（见 4.2.3），也就是「仓库上一层目录里的 `Vivado_projects/Fir_filter/`」，这是作者本机的目录结构，clone 下来的仓库并没有这个布局——真实文件其实就在仓库内的 `Fir_filter.srcs/sources_1/new/` 下。所以如果你要走 TCL 路径，通常需要用 `-tclargs --origin_dir <某个让 ../Vivado_projects/Fir_filter 能对上的路径>` 来修正，或先把文件按它期望的布局摆放。

**路径 B：手动添加源文件（对刚 clone 的仓库最可靠）**

```text
1. Vivado 里 File → New Project，选 part = xc7a100tcsg324-1（没有板子可选同系列任意 part 做仿真）
2. Add Sources → Design → 加入 Fir_filter.srcs/sources_1/new/ 下的 6 个 .v
   （adder.v, delay.v, multiplier.v, fir_tap.v, fir_filter.v, fir_filter_tb.v）
3. 把 fir_filter 设为综合顶层，fir_filter_tb 设为仿真顶层
4. Run Simulation 即可看波形
```

> 这条路径不依赖 TCL 里写死的路径，所以在 fresh clone 上几乎一定能跑通仿真。

#### 4.2.3 源码精读

[Fir_filter.tcl:L140](https://github.com/Ghydra0/pipelined-FIR-filter-fpga/blob/4c6aedf850b021e36fcb0a3d86e0902704594eeb/Fir_filter.tcl#L140) — 创建工程时指定的目标器件就是 `xc7a100tcsg324-1`（Nexys A7-100T 上的 Artix-7）。

```tcl
create_project ${_xil_proj_name_} ./${_xil_proj_name_} -part xc7a100tcsg324-1
```

[Fir_filter.tcl:L174-L185](https://github.com/Ghydra0/pipelined-FIR-filter-fpga/blob/4c6aedf850b021e36fcb0a3d86e0902704594eeb/Fir_filter.tcl#L174-L185) — 这里能看到 4.2.2 提到的「路径坑」：源文件被引用为 `${origin_dir}/../Vivado_projects/Fir_filter/...`，即仓库上一层目录里的作者本地路径。

```tcl
set files [list \
 [file normalize "${origin_dir}/../Vivado_projects/Fir_filter/Fir_filter.srcs/sources_1/new/adder.v" ]\
 ... ]
```

[Fir_filter.tcl:L196](https://github.com/Ghydra0/pipelined-FIR-filter-fpga/blob/4c6aedf850b021e36fcb0a3d86e0902704594eeb/Fir_filter.tcl#L196) — 指定**综合/实现**的顶层模块为 `fir_filter`。

```tcl
set_property -name "top" -value "fir_filter" -objects $obj
```

[Fir_filter.tcl:L228-L231](https://github.com/Ghydra0/pipelined-FIR-filter-fpga/blob/4c6aedf850b021e36fcb0a3d86e0902704594eeb/Fir_filter.tcl#L228-L231) — 指定**仿真**顶层为 `fir_filter_tb`（测试台），仿真库名为 `xil_defaultlib`。

```tcl
set_property -name "top" -value "fir_filter_tb" -objects $obj
```

#### 4.2.4 代码实践

**实践目标**：只读 TCL，回答关于工程的三个关键问题。

**操作步骤**：

1. 打开 `Fir_filter.tcl`。
2. 搜索 `create_project`，读出 `-part` 后面的器件型号。
3. 搜索 `"top"`，分别找出 `sources_1` fileset 的 top 和 `sim_1` fileset 的 top。

**需要观察的现象**：

- 目标 part：`xc7a100tcsg324-1`。
- 综合顶层：`fir_filter`。
- 仿真顶层：`fir_filter_tb`。

**预期结果**：与上文一致。**待本地验证**：在你自己装的 Vivado（建议 2023.2，与 TCL 头部声明一致）里实际走一遍「手动添加源文件」路径并 `Run Simulation`，确认能弹出波形窗口。

#### 4.2.5 小练习与答案

**练习 1**：README 写的命令是 `vivado -mode tcl -source my_project.tcl`，但仓库里没有 `my_project.tcl`，为什么？

> **答案**：README 是模板化写法，实际的 TCL 文件名是 `Fir_filter.tcl`。运行时应改成 `vivado -mode tcl -source Fir_filter.tcl`（并注意 4.2.2 的路径坑）。

**练习 2**：仿真顶层为什么是 `fir_filter_tb` 而不是 `fir_filter`？

> **答案**：`fir_filter` 是被测器件（DUT/UUT），它没有自带时钟和激励；`fir_filter_tb` 是测试台，内部例化了 `fir_filter`、产生了时钟和输入 stimulus，所以仿真要以 testbench 为顶层。

---

### 4.3 FIR 滤波器概念

#### 4.3.1 概念说明

**FIR** = Finite Impulse Response（有限长单位冲激响应）。名字吓人，本质极简：

> **FIR 滤波器就是把「当前输入」和「过去若干个输入」各自乘上一个权重（系数），再全部加起来，作为当前输出。**

回到开头的「期末成绩」类比：把「平时/期中/期末」换成「当前样本/上一个样本/上上个样本」，把百分比换成滤波器系数，就是 FIR。

- **F（Finite，有限）**：只用「有限个」过去的样本（本项目是 4 个），不无限回溯。与之相对的是 IIR（无限冲激响应），会用输出反馈，本项目不是这种。
- **系数（coefficient / coeff）**：每个样本对应的权重。系数决定了滤波器的「性格」（低通、高通、移动平均等）。
- **抽头（tap）**：一个「系数 + 一段延迟 + 乘加」的组合叫一个抽头。抽头数 = 系数数 = `TAPS`。

本项目默认是一个**移动平均滤波器（moving average）**：所有系数都相等，等于 \(1/\text{TAPS}\)。它做的事情就是把最近 `TAPS` 个样本求平均，起到「平滑/低通」的效果。

#### 4.3.2 核心流程

FIR 的数学定义（差分方程）：

\[
y[n] = \sum_{k=0}^{N-1} h[k]\, x[n-k] = h[0]x[n] + h[1]x[n-1] + \cdots + h[N-1]x[n-(N-1)]
\]

其中：

- \(x[n]\) 是当前输入样本，\(x[n-1]\) 是上一个样本，\(x[n-k]\) 是往前第 \(k\) 个样本。
- \(h[k]\) 是第 \(k\) 个系数（权重）。
- \(N\) 是抽头数（本项目 \(N=4\)）。
- \(y[n]\) 是当前输出样本。

**移动平均**是 FIR 的特例：所有系数相等，\(h[k] = 1/N\)：

\[
y[n] = \frac{1}{N}\bigl(x[n] + x[n-1] + x[n-3] + \cdots + x[n-(N-1)]\bigr)
\]

以本项目默认 \(N=4\) 为例，列个表就很直观：

| 时刻 | 当前输入 | 参与求和的 4 个样本（从新到旧） | 移动平均输出 \(y[n]\) |
| --- | --- | --- | --- |
| n=3 | x[3] | x[3], x[2], x[1], x[0] | (x[3]+x[2]+x[1]+x[0]) / 4 |
| n=4 | x[4] | x[4], x[3], x[2], x[1] | (x[4]+x[3]+x[2]+x[1]) / 4 |

可以看到：每来一个新样本，求和窗口就**整体右移一格**——这正是「移动平均」名字的由来。

**关于系数的定点表示（Q15）**：FPGA 里不方便用小数，所以系数用「定点整数」表示。本项目用 **Q15** 格式：把 1.0 对应到 \(2^{15} = 32768\)，于是一个系数的实际数值 = 整数值 / 32768。默认系数 `16'h2000`：

\[
\text{0x2000} = 8192,\qquad 8192 / 32768 = 0.25 = 1/4 = 1/\text{TAPS}
\]

正好等于 \(1/N\)，与「移动平均 = 1/抽头数」完全吻合。✅

> 提示：硬件里实际是把 \(x\) 乘以这个定点系数、再做定点评缩放来近似上面的加权求和；乘法器和缩放的细节是后续「Q15 定点乘法器」讲义的内容。本讲只要理解「系数 ≈ 0.25，即每个样本占四分之一权重」即可。

#### 4.3.3 源码精读

[fir_filter.v:L29-L35](https://github.com/Ghydra0/pipelined-FIR-filter-fpga/blob/4c6aedf850b021e36fcb0a3d86e0902704594eeb/Fir_filter.srcs/sources_1/new/fir_filter.v#L29-L35) — 系数定义。注释直接写明「MOVING AVERAGE FILTER: coeff = 1/taps」，4 个系数全部是 `16'h2000`（即 1/4）。

```verilog
// Coefficients: READ ME
// MOVING AVERAGE FILTER: coeff = 1/taps
wire [WIDTH-1:0] coeffs [0:TAPS-1];
assign coeffs[0] = 16'h2000;
assign coeffs[1] = 16'h2000;
assign coeffs[2] = 16'h2000;
assign coeffs[3] = 16'h2000;
```

> 想做别的滤波器（低通、高通、汉明窗等）？把这里的 4 个系数换成你设计好的系数即可，这正是 README 说的「Parameters can be changed to create the filter type desired」。

[README.md:L14-L16](https://github.com/Ghydra0/pipelined-FIR-filter-fpga/blob/4c6aedf850b021e36fcb0a3d86e0902704594eeb/README.md#L14-L16) — README 对参数的说明，点明默认系数 `16'h2000` 等于 1/4，且采用 Q15 定点。

[fir_filter.v:L40-L68](https://github.com/Ghydra0/pipelined-FIR-filter-fpga/blob/4c6aedf850b021e36fcb0a3d86e0902704594eeb/Fir_filter.srcs/sources_1/new/fir_filter.v#L40-L68) — `generate` 循环把 4 个 `fir_tap` 串成流水线，最后 `assign yn = stage_out[0];` 把第 0 级的输出作为滤波器输出。这段是「流水线」的精髓，后续讲义会逐行拆。

[fir_filter_tb.v:L50-L73](https://github.com/Ghydra0/pipelined-FIR-filter-fpga/blob/4c6aedf850b021e36fcb0a3d86e0902704594eeb/Fir_filter.srcs/sources_1/new/fir_filter_tb.v#L50-L73) — 测试台喂的激励是一串递增的样本：1000, 2000, 3000, …, 10000，每 20ns 换一个。用移动平均的直觉就能预判输出趋势（见下方实践）。

```verilog
initial begin
    xn = 1000;
    #20; xn = 2000;
    #20; xn = 3000;
    ...
end
```

#### 4.3.4 代码实践

**实践目标**：用「加权求和」的直觉手算一个移动平均结果，体会 FIR 在做什么。

**操作步骤**：

1. 假设滤波器是默认的 4 抽头移动平均（每个系数 = 1/4）。
2. 假设最近 4 个输入样本依次为（从新到旧）：`4000, 3000, 2000, 1000`。
3. 套用公式 \(y[n] = (x[n]+x[n-1]+x[n-2]+x[n-3])/4\) 手算。

**需要观察的现象 / 预期结果**：

\[
y = (4000 + 3000 + 2000 + 1000) / 4 = 10000 / 4 = 2500
\]

也就是说，输出是最近 4 个样本的算术平均。如果输入在缓慢上升，输出就是「平滑后的、滞后一点的」上升序列——这就是移动平均的「低通平滑」效果。

> 进阶（待本地验证）：用 Vivado 跑一遍 `fir_filter_tb`，看 `$monitor` 打印的 `yn` 是否在若干拍延迟后趋近于「最近 4 个样本的平均」。因为有流水线延迟，开始几拍的 `yn` 还没填满窗口，不必和手算逐拍对齐；重点观察**稳态**趋势。

#### 4.3.5 小练习与答案

**练习 1**：把默认系数 `16'h2000` 换算成十进制整数，再换算成 Q15 小数，说说它为什么代表「1/4」。

> **答案**：`0x2000` = 8192。Q15 下实际数值 = 8192 / 32768 = 0.25 = 1/4。又因 `TAPS=4`，1/4 = 1/TAPS，正好是移动平均要求的「每个样本权重相等且和为 1」。

**练习 2**：如果把 `TAPS` 改成 8 仍想做移动平均，系数应该改成多少（Q15 十六进制）？

> **答案**：移动平均要求每个系数 = 1/8 = 0.125 = 4096/32768 = `0x1000`。所以 8 个系数都设成 `16'h1000`（并要相应在 `coeffs` 数组里补齐到 8 个、调整 `TAPS=8`）。

**练习 3**：用一句中文写出 FIR 滤波器对输入序列做了什么运算。

> **答案**：FIR 滤波器把当前输入与过去若干个输入样本分别乘以各自的系数（权重），再把所有乘积加起来，作为当前输出。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个小任务（这是本讲的「结业练习」）：

> **任务**：你已经 clone 了仓库。请写一份一页的「项目速览」笔记，包含以下四项，且每一项都要**引用一处真实源码位置**作为依据：
>
> 1. 用一句话定义本项目（含「FPGA / Verilog / 流水线 FIR」三个要点）。
> 2. 列出顶层模块名（综合顶层 + 仿真顶层），并指出各自由哪个文件 / 哪条 TCL 语句决定。
> 3. 说明默认滤波器类型、抽头数、位宽、默认系数及其 Q15 含义。
> 4. 给出一个刚 clone 仓库的人「最可靠的运行/仿真步骤」，并解释为什么不推荐直接 `source Fir_filter.tcl`。

**参考要点（自己先写，再对照）**：

1. 本项目用 Verilog 在 Artix-7 FPGA（Nexys A7-100T）上实现一个**流水线结构**的 FIR 滤波器（依据 `README.md` 第 1–3 行）。
2. 综合顶层 `fir_filter`（`fir_filter.v` 第 23 行；TCL 第 196 行指定）；仿真顶层 `fir_filter_tb`（`fir_filter_tb.v`；TCL 第 229 行指定）。
3. 默认是**移动平均滤波器**，`TAPS=4`、`WIDTH=16`，4 个系数均为 `16'h2000` = 8192 = Q15 的 0.25 = 1/4 = 1/TAPS（`fir_filter.v` 第 29–35 行）。
4. 最可靠的是「手动新建工程 + 添加 6 个 `.v` 文件 + 设好两个顶层 + Run Simulation」。不推荐直接 `source Fir_filter.tcl`，是因为它把源文件路径写死成 `../Vivado_projects/Fir_filter/...`（TCL 第 174–181 行），fresh clone 里不存在该布局，会报找不到文件（待本地验证）。

## 6. 本讲小结

- 本项目 = **Verilog + Vivado + Artix-7 (Nexys A7-100T)** 实现的**流水线 FIR 滤波器**。
- 顶层模块是 `fir_filter`（端口只有 `clk / xn / yn`），仿真顶层是测试台 `fir_filter_tb`。
- 运行方式有两条路径：TCL 重建（`Fir_filter.tcl`，但路径写死、fresh clone 上易失败）和手动添加源文件（最可靠，适合学习仿真）。
- FIR 滤波器的本质就是**加权求和**：\(y[n]=\sum_k h[k]x[n-k]\)。
- 默认配置是 4 抽头移动平均，系数 `16'h2000` = Q15 的 1/4 = 1/TAPS。
- 目标器件 `xc7a100tcsg324-1` 由 TCL 的 `create_project -part` 指定。

## 7. 下一步学习建议

本讲只建立了「项目是什么、怎么跑、算法是什么」的直觉，**还没碰 Verilog 细节**。建议接下来按这个顺序深入：

1. **下一讲（建议）**：《目录结构与 Vivado 工程构建》——吃透 `Fir_filter.tcl` 的每一段（fileset、runs、约束），搞清楚 XDC 约束文件里为什么全是注释。
2. **再之后**：进入「核心模块」系列，按「叶子模块 → 单级 tap → tap 链」的顺序读 `adder.v / delay.v / multiplier.v / fir_tap.v / fir_filter.v`，看清「流水线」到底是怎么用代码搭出来的。
3. **想立刻动手**：先把本讲的「手动添加源文件」跑通仿真，看到 `fir_filter_tb` 打印的 `yn` 波形，再带着波形里的疑问去读后续讲义，效果最好。

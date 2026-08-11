# 项目总览：IKA32010 是什么

## 1. 本讲目标

本讲是整本学习手册的第一讲，目标不在于讲清任何一条指令的实现细节，而是帮你在大脑里建立一个「坐标系」：**这个项目到底是什么、它复刻的是哪一颗芯片、源码是怎么组织的、我该怎么把它用起来**。

读完本讲，你应该能够：

- 说清楚 IKA32010 是用 SystemVerilog 写的 **TI TMS32010 DSP 软核**，以及 TMS32010 在历史上是什么、用来做什么。
- 理解 README 里两个关键修饰词的含义：**「半周期精确 (semi-cycle-accurate)」** 和 **「FPGA proven（已在 FPGA 上验证）」**。
- 知道项目的验证方式：它通过在街机游戏 **Twin Cobra、Sky Shark、Wardner** 上跑通来证明实现正确。
- 看懂顶层模块 `IKA32010` 的端口列表，并掌握本仓库「端口名即文档」的命名约定。
- 认识 `src/IKA32010.sv` 里内嵌的四个子模块：**ALU、RAM、Stack、Multiplier**，知道它们各自的职责。

## 2. 前置知识

本讲面向零基础读者，但有几个概念先解释清楚会让你更轻松。

**DSP（Digital Signal Processor，数字信号处理器）**
一类专门为「数字信号处理」优化的处理器，典型特点是：硬件乘法器、乘加（MAC）单周期完成、哈佛结构（程序存储器和数据存储器分开、各自有总线）、支持零开销循环。IKA32010 复刻的 TMS32010 就是 TI（德州仪器）在 1983 年推出的一款早期经典定点 DSP，被大量用于调制解调器、语音处理、以及 80 年代街机的音效合成。`docs/` 目录里附带的 `TMS32010_Users_Guide_1985.pdf` 正是这颗芯片 1985 年版的官方用户手册，是我们阅读源码时最权威的对照资料。

**软核（soft core）**
用硬件描述语言（这里是 SystemVerilog）写成的处理器设计，以源码形式存在。你可以把它「综合（synthesize）」到 FPGA（现场可编程门阵列）上变成真实可运行的电路，也可以在仿真器里跑。与之相对的是「硬核」——直接固化在硅片里的电路。

**SystemVerilog / Verilog**
硬件描述语言，用来描述数字电路。本讲你只需要知道：`module ... endmodule` 定义一个电路模块；`input` / `output` 声明引脚方向；`always @(posedge clk)` 描述「时钟上升沿触发的寄存器逻辑」；`always @(*)` 描述「纯组合逻辑」。看不懂具体语法没关系，本讲只看「轮廓」。

**机器周期与四分频**
原始 TMS32010 对外有一个时钟输入，内部把每个指令周期分成 4 个相位。IKA32010 用一个 2 位计数器把外部的 `EMUCLK`「四分频」为一个 DSP 机器周期——这一点我们会在 4.1 节和后续 u1-l4 讲里反复用到。

## 3. 本讲源码地图

本讲只看「骨架」，涉及的文件很少：

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| [README.md](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/README.md) | 60 | 项目说明：特性、验证状态、实例化示例、编译选项、FPGA 资源占用。 |
| [LICENSE](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/LICENSE) | 25 | BSD 2-Clause 许可证。 |
| [src/IKA32010.sv](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv) | 2017 | 主源文件，含顶层模块 `IKA32010` 和四个内嵌子模块。 |

其中 `src/` 目录还有三个配套文件（本讲只做提示，不在深入之列）：

| 文件 | 行数 | 作用（后续讲义展开） |
| --- | --- | --- |
| `src/IKA32010_mnemonics.sv` | 62 | 助记符与各类控制信号常量定义。 |
| `src/IKA32010_disasm.sv` | 210 | 仿真时把执行的操作码反汇编成可读文本。 |
| `src/IKA32010_tb.v` | 92 | testbench，仿真激励。 |

`docs/` 目录里则是三份参考资料：`TMS32010_Users_Guide_1985.pdf`（官方用户手册）、`TMS320C1X.PDF`（同系列手册）、`opcode table.xlsx`（指令编码表）。

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：**4.1 顶层模块 IKA32010**（项目定位 + 端口 + 时钟分频），**4.2 子模块概览**（ALU/RAM/Stack/Multiplier）。

### 4.1 顶层模块 IKA32010

#### 4.1.1 概念说明

`IKA32010` 是整个项目的顶层模块，也是你实例化时唯一需要直接打交道的模块。它把「复刻一颗 TMS32010」所需要的全部外部引脚都暴露出来，并在内部把时钟分频、程序计数器、总线控制器、指令译码（微码）、数据通路等串成一个完整的处理器。

它有三个身份标签，都写在 README 里：

> A **semi-cycle-accurate, BSD2 licensed** core. **FPGA proven.**
> —— [README.md:4-6](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/README.md#L4-L6)

- **BSD2 licensed**：采用 BSD 2-Clause 许可证，允许你自由使用、修改、再发布（包括商用），只要保留版权声明与免责声明。这是相当宽松的开源许可。
- **semi-cycle-accurate（半周期精确）**：所谓「周期精确（cycle-accurate）」是指电路每一个时钟周期的对外行为都和原始芯片完全一致；「半（semi-）」这个修饰词表明本核心在 DSP 机器周期粒度上忠实地重现了对外可见的时序（总线相位、指令周期数、引脚波形），从而能跑通真实的 TMS32010 程序，但不追求逐门、逐半周期的内部硅片级还原。**最权威的「是否够准」的证据不是这个词本身，而是下方的街机验证。** 该词在内部时序上的精确边界，建议对照 `docs/` 用户手册与 testbench 波形进一步确认。
- **FPGA proven**：不止是「能综合通过」，而是「在真实 FPGA 上跑通过真实程序」。README 的 Current status 一节给出了具体证据。

#### 4.1.2 核心流程

从「上电」到「跑程序」，顶层模块大致经历以下流程（本讲只看骨架，细节留给后续讲义）：

1. **复位**：`i_RS_n` 拉低期间，内部状态（程序计数器 PC、堆栈、累加器等）被清零。
2. **时钟分频**：`cyclecntr` 把外部 `i_EMUCLK` 四分频为一个 DSP 机器周期，并产生 `o_CLKOUT` 等分相时钟。
3. **取指**：每个机器周期，PC 通过外部总线从程序 ROM 读入一条 16 位指令，锁存进指令寄存器。
4. **译码（微码）**：一个大型组合逻辑块根据指令产生一整套控制信号（选哪条数据源、ALU 做什么运算、是否读写 RAM、是否压栈……）。
5. **执行 / 数据通路**：ALU、乘法器、RAM、堆栈在控制信号驱动下完成运算与数据搬移。
6. **总线事务**：总线控制器对外驱动 `o_MEN_n / o_DEN_n / o_WE_n` 等信号，完成指令读、IN、OUT、表读、表写等外部访问。

时序上的核心关系只有一条，记牢即可：

\[ f_{CLKOUT} = \frac{1}{4}\, f_{EMUCLK} \]

也就是 4 个 `EMUCLK` 周期 = 1 个 DSP 机器周期。

#### 4.1.3 源码精读

**① 项目自述与验证证据（README）**

README 开头一句话点明了项目定位与作者：

```
# IKA32010
A BSD-licensed core for TI's TMS32010 DSP © 2024 Sehyeon Kim(Raki)
```
—— [README.md:1-2](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/README.md#L1-L2)

而「FPGA proven」最直接的证据是这三款街机游戏在真实硬件（MiSTer FPGA 平台）上跑通：

```
v1.0 – ✅Verified using the arcade games Twin Cobra, Sky Shark, and Wardner by atrac17.
```
—— [README.md:8-9](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/README.md#L8-L9)

这三款都是 Toaplan 出品的经典街机游戏，它们的音效板上使用了真实的 TMS32010。能在这些游戏上替换原始芯片而不出错，是非常强的正确性背书。

**② 顶层模块端口列表**

下面是顶层模块 `IKA32010` 的完整端口声明。**这是本讲最重要的代码段**，你的实践任务就是吃透它：

```verilog
module IKA32010 (
    //chip clock
    input   wire            i_EMUCLK, //emulator master clock
    input   wire            i_CLKIN_PCEN, //CLKIN positive edge enable

    //clock out
    output  wire            o_CLKOUT,
    output  wire            o_CLKOUT_PCEN,
    output  wire            o_CLKOUT_NCEN,

    //chip reset
    input   wire            i_RS_n,

    //bus control
    output  reg             o_MEN_n, //external instruction read
    output  reg             o_DEN_n, //IN instruction
    output  reg             o_WE_n, //OUT instruction

    output  wire    [11:0]  o_AOUT,
    input   wire    [15:0]  i_DIN,
    output  reg     [15:0]  o_DOUT,
    output  reg             o_DOUT_OE,

    //flag
    input   wire            i_BIO_n,

    //interrupt
    input   wire            i_INT_n
);
```
—— [src/IKA32010.sv:1-29](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1-L29)

这里有一个贯穿全仓库的命名约定，README 明确告诉你：「方向和有效极性都写在端口名里」：

> The direction and the polarity of the signals are described in the port names.
> —— [README.md:42](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/README.md#L42)

拆解规则如下：

| 命名片段 | 含义 |
| --- | --- |
| `i_` 前缀 | input，输入引脚（外部 → 核心） |
| `o_` 前缀 | output，输出引脚（核心 → 外部） |
| `_n` 后缀 | active low，**低电平有效**（0 表示「成立/激活」） |
| 无 `_n` 后缀 | active high，高电平有效 |

据此，`i_RS_n` 是「低有效的同步复位输入」，`o_MEN_n` 是「低有效的存储器使能输出」，而 `o_DOUT_OE` 是「高有效的输出使能」。看懂这套约定，端口表就不再需要逐条注释。

**③ 时钟分频（半周期精确的物理来源）**

顶层模块一上来就是时钟分频逻辑，它正是「机器周期 = 4 × EMUCLK」的实现：

```verilog
//master cycle counter
reg     [1:0]   cyclecntr;
always @(posedge i_EMUCLK) if(i_CLKIN_PCEN) begin
    if(!i_RS_n) cyclecntr <= 2'd0;
    else cyclecntr <= (cyclecntr == 2'd3) ? 2'd0 : cyclecntr + 2'd1;
end

//divided clock
assign  o_CLKOUT = cyclecntr[1];
assign  o_CLKOUT_NCEN = (cyclecntr == 2'd3) & i_CLKIN_PCEN;
assign  o_CLKOUT_PCEN = (cyclecntr == 2'd1) & i_CLKIN_PCEN;
```
—— [src/IKA32010.sv:48-58](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L48-L58)

`cyclecntr` 是 2 位计数器，在 `0→1→2→3→0` 之间循环，于是每 4 个 `EMUCLK` 周期构成一个完整的 DSP 机器周期。`o_CLKOUT = cyclecntr[1]` 让输出时钟每两个 `EMUCLK` 翻转一次，正好是四分频；`o_CLKOUT_PCEN` 和 `o_CLKOUT_NCEN` 则在相位 1 和相位 3 各产生一个使能脉冲，分别对应原芯片 CLKIN 的正沿/负沿节拍。（精确相位含义会在 u1-l4 详讲。）

**④ FPGA 资源占用（FPGA proven 的数据支撑）**

README 末尾给出了两颗 FPGA 上的实测资源与频率，能帮你直观感受这个核心的体量：

| FPGA | 逻辑单元 | 寄存器 | BRAM | 乘法器 | fmax（慢角） |
| --- | --- | --- | --- | --- | --- |
| Altera EP4CE6E22C8 | 1243 LE | 275 | 4096 bits | 两个 9-bit | 44.98 MHz @85°C |
| Altera 5CSEBA6U23I7 (MiSTer) | 601 ALM | 275 | 4096 bits | 1 DSP 块 | 60.28 MHz @100°C |

—— [README.md:57-59](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/README.md#L57-L59)

注意两个有趣的对应关系：**BRAM 4096 bits** 正好等于 `256 × 16`（数据 RAM 子模块的容量，见 4.2）；**1 DSP 块 / 两个 9-bit 乘法器**对应的是 16×16 有符号乘法器子模块。这说明综合工具把源码里的乘法器和 RAM 自动映射到了 FPGA 的硬核资源上——这正是「软核」跑在「FPGA」上的典型形态。

#### 4.1.4 代码实践

**实践目标**：把顶层模块的端口彻底吃透，建立一张你自己的「引脚清单」，为后续在 testbench 或 FPGA 工程里实例化做准备。

**操作步骤**：

1. 打开 [src/IKA32010.sv:1-29](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1-L29) 与 [README.md:17-41](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/README.md#L17-L41)（实例化示例）。
2. 对照端口声明，为每一个端口填写下表（建议用 Markdown 或电子表格）：

   | 端口名 | 方向（input/output） | 位宽 | 有效极性（高/低） | 功能（一句话） |
   | --- | --- | --- | --- | --- |

3. 极性判断方法：只看名字后缀——带 `_n` 是低有效，不带是高有效。功能注释优先参考源码里 `//` 旁注和 README「信号含义」一节（[README.md:45-50](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/README.md#L45-L50)）。
4. 把端口按功能分组：**时钟组**（EMUCLK/CLKIN_PCEN/CLKOUT…）、**复位组**（RS_n）、**总线控制组**（MEN_n/DEN_n/WE_n）、**地址数据组**（AOUT/DIN/DOUT/DOUT_OE）、**控制/中断组**（BIO_n/INT_n）。

**需要观察的现象**：完成清单后，你应该发现全部 15 个端口都能被无歧义地归类，且方向与极性完全由名字决定，无需查手册。

**预期结果**：参考下表（节选），你可以拿它核对自己的答案：

| 端口名 | 方向 | 位宽 | 极性 | 功能 |
| --- | --- | --- | --- | --- |
| `i_EMUCLK` | input | 1 | 高 | 仿真主时钟 |
| `i_CLKIN_PCEN` | input | 1 | 高 | CLKIN 正沿使能 |
| `o_CLKOUT` | output | 1 | 高 | 四分频时钟输出 |
| `o_CLKOUT_PCEN` | output | 1 | 高 | 正沿使能脉冲 |
| `o_CLKOUT_NCEN` | output | 1 | 高 | 负沿使能脉冲 |
| `i_RS_n` | input | 1 | 低 | 同步复位 |
| `o_MEN_n` | output | 1 | 低 | 外部指令读使能 |
| `o_DEN_n` | output | 1 | 低 | IN 指令读使能 |
| `o_WE_n` | output | 1 | 低 | OUT 指令写使能 |
| `o_AOUT` | output | 12 | 高 | 地址输出 |
| `i_DIN` | input | 16 | 高 | 数据输入 |
| `o_DOUT` | output | 16 | 高 | 数据输出 |
| `o_DOUT_OE` | output | 1 | 高 | 三态输出使能 |
| `i_BIO_n` | input | 1 | 低 | BIO 转移控制输入 |
| `i_INT_n` | input | 1 | 低 | 中断请求输入 |

> 说明：上表的方向与位宽来自源码声明，极性来自命名约定，功能综合自源码注释与 README。其中 `o_MEN_n / o_DEN_n / o_WE_n` 在原芯片上对应程序/数据存储器访问使能，源码注释分别标注为「external instruction read / IN instruction / OUT instruction」，精确时序待在 u2-l3 详讲并对照手册确认。

#### 4.1.5 小练习与答案

**练习 1**：端口名 `i_BIO_n` 中，`i`、`BIO`、`_n` 分别传达什么信息？

**参考答案**：`i` 表示它是输入（input）端口；`BIO` 是信号名（对应原芯片的「Branch on I/O status」转移控制引脚）；`_n` 表示低电平有效——也就是当这个引脚为 0 时「条件成立」。

**练习 2**：`o_CLKOUT_PCEN` 和 `o_CLKOUT_NCEN` 都没有 `_n` 后缀，这说明它们的极性是什么？结合 4.1.3 的分频代码，二者分别在 `cyclecntr` 等于几时拉高？

**参考答案**：都没有 `_n`，所以都是**高电平有效**。由代码 `o_CLKOUT_PCEN = (cyclecntr == 2'd1) & i_CLKIN_PCEN`、`o_CLKOUT_NCEN = (cyclecntr == 2'd3) & i_CLKIN_PCEN` 可知，前者在 `cyclecntr==1`、后者在 `cyclecntr==3` 时产生一个脉冲（且都要求 `i_CLKIN_PCEN` 为高）。

**练习 3**：README 用「FPGA proven」而不是「FPGA synthesizable（可综合）」，这两个说法的差别在哪？为什么前者更有说服力？

**参考答案**：「可综合」只保证代码能被综合工具转成电路、不报错，但不保证功能正确；「FPGA proven」表示它已经在真实 FPGA 上运行并通过了真实程序（这里是三款街机游戏）的验证，是功能正确性的强证据。

### 4.2 子模块概览（ALU/RAM/Stack/Multiplier）

#### 4.2.1 概念说明

`src/IKA32010.sv` 这一个文件里其实写了 **5 个模块**：1 个顶层 `IKA32010`（第 1～1758 行），以及紧跟其后的 4 个子模块。这 4 个子模块在顶层里被实例化，分别承担处理器的四块「脏活累活」：

- **`IKA32010_alu`**：算术逻辑单元。负责 AND/OR/XOR/ABS/ADD/SUB/SUBC 七种运算，里面藏着累加器（accumulator）和 Z/N/V 三个标志位。
- **`IKA32010_ram`**：片上数据 RAM。256 个 16 位单元，支持一种特殊的 `DMOV`（把当前单元内容搬到高一地址）数据搬移。
- **`IKA32010_stack`**：硬件堆栈。4 级深度，用于 CALL/RET 和中断时保存返回地址。
- **`IKA32010_multiplier`**：16×16 有符号乘法器，产出 32 位乘积，并会被综合工具映射到 FPGA 的 DSP 硬核。

之所以把它们单独写成模块，既有「逻辑清晰」的考虑，也有「让综合工具更容易识别硬核资源（BRAM、DSP 块）」的现实好处——你在 4.1.3 的资源表里已经看到 BRAM 和 DSP 块被精确命中。

#### 4.2.2 核心流程

四个子模块之间没有直接互连，而是都挂在顶层 `IKA32010` 上，由顶层的「微码（microcode）」统一调度。数据流向大致是：

```
                 ┌──────────── 顶层微码控制信号 ────────────┐
                 │                                           │
   reg_wrbus ◀──┼──(RAM/AR/Stack/乘法器P/标志位/立即数 选源)  │
     (写总线)    │                                           │
        │        │     ┌──────── ALU ────────┐               │
        └───────►│     │ portA=累加器反馈     │               │
                 │     │ portB=移位后数据 or P│──► 累加器     │
                 │     └──────────────────────┘               │
                 │     ┌── Multiplier ──┐                     │
                 │     │ T × op1 ──► P │──► 喂给 ALU portB    │
                 │     └────────────────┘                     │
                 │     ┌── Stack ──┐  ┌── RAM ──┐             │
                 │     │ CALL/RET  │  │ 读/写/DMOV│            │
                 │     └───────────┘  └─────────┘             │
                 └───────────────────────────────────────────┘
```

- **取到的数据**通过顶层写总线 `reg_wrbus` 汇聚。
- **ALU** 的 port A 一般是累加器的反馈，port B 可以是移位后的数据或乘法器的 P 寄存器。
- **Multiplier** 算出的乘积 P 可以被 ALU 累加（这正是 DSP「乘加」的由来）。
- **Stack** 保存/恢复 PC（或 ACC），配合子程序调用与中断。
- **RAM** 提供数据存储，支持直接/间接寻址与 DMOV。

（这些数据通路会在 u2 整个单元里逐条展开。本讲只要记住「四个帮手 + 一个调度者」的格局即可。）

#### 4.2.3 源码精读

下面把四个子模块的模块头与在顶层的实例化各看一眼，建立「名字 ↔ 职责」的对应关系即可，**不必现在读懂内部逻辑**。

**① IKA32010_multiplier —— 乘法器**

模块头声明它接收两个 16 位操作数、输出 32 位乘积：

```verilog
module IKA32010_multiplier (
    input   wire            i_EMUCLK,
    input   wire            i_RST_n,
    input   wire            i_MUL_EN,
    input   wire    [15:0]  i_OP0,
    input   wire    [15:0]  i_OP1,
    output  wire    [31:0]  o_P
);
```
—— [src/IKA32010.sv:1985-1994](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1985-L1994)

源码里有一句关键注释，解释了它为什么省心：

```verilog
//Quartus(DE10-nano) and Vivado(Zybo-Z20) will synthesis this well using a DSP block
```
—— [src/IKA32010.sv:1996](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1996)

也就是说，乘法用 `signed` 修饰的有符号乘 `*` 写出来，综合器（Quartus / Vivado）会自动把它映射到 FPGA 的 DSP 硬块——这正是资源表里「1 DSP 块」的由来。顶层实例化见 [src/IKA32010.sv:398-401](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L398-L401)。

**② IKA32010_stack —— 硬件堆栈**

```verilog
module IKA32010_stack (
    input   wire            i_EMUCLK, i_CEN, i_RST_n,
    input   wire            i_PUSH, i_POP,
    input   wire    [11:0]  i_DIN,
    output  wire    [11:0]  o_DOUT
);
```
—— [src/IKA32010.sv:1943-1953](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1943-L1953)

它是一个 4 级（`stack[0:3]`）移位式堆栈，`PUSH` 入栈、`POP` 出栈、否则保持。注意数据是 12 位——正好等于程序计数器 PC 的位宽，这就是它能用来保存返回地址的原因。顶层实例化见 [src/IKA32010.sv:412-416](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L412-L416)。

**③ IKA32010_alu —— 算术逻辑单元**

模块头里通过一组 `localparam` 暴露了它支持的运算种类：

```verilog
localparam  ALU_AND  = 3'd0;
localparam  ALU_OR   = 3'd1;
localparam  ALU_XOR  = 3'd2;
localparam  ALU_ABS  = 3'd3;
localparam  ALU_ADD  = 3'd4;
localparam  ALU_SUB  = 3'd5;
localparam  ALU_SUBC = 3'd6;
```
—— [src/IKA32010.sv:1780-1786](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1780-L1786)

也就是说，ALU 能做与/或/异或/取绝对值/加/减/条件减（SUBC，用于实现除法）。它内部还包含累加器和 Z（零）、N（负）、V（溢出）标志。顶层实例化见 [src/IKA32010.sv:448-456](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L448-L456)。

**④ IKA32010_ram —— 片上数据 RAM**

```verilog
module IKA32010_ram (
    input   wire            i_EMUCLK,
    input   wire            i_DMOV, //one-cycle special command
    input   wire            i_WE,
    input   wire    [7:0]   i_ADDR,
    input   wire    [15:0]  i_DIN,
    output  wire    [15:0]  o_DOUT
);
//...
reg     [15:0]  RAM[0:255];
```
—— [src/IKA32010.sv:1909-1928](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1909-L1928)

`RAM[0:255]` 即 256 个 16 位单元 = 4096 bit，与资源表里的 BRAM 4096 bits 完全吻合。`i_DMOV` 是一特殊的「把当前单元搬到下一高地址」的单周期操作（注释里的 one-cycle special command）。顶层实例化见 [src/IKA32010.sv:491-494](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L491-L494)。

#### 4.2.4 代码实践

**实践目标**：用源码阅读的方式，把「四个子模块 ↔ 顶层实例化 ↔ FPGA 硬核资源」三者对应起来，验证 4.2.1 的论断。

**操作步骤**：

1. 在 [src/IKA32010.sv](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv) 中分别定位四处实例化：`u_multiplier`（L398）、`u_stack`（L412）、`u_alu`（L448）、`u_ram`（L491）。
2. 为每个子模块填一张「职责对照卡」：

   | 子模块 | 实例化行号 | 模块定义行号 | 数据位宽 | 对应 FPGA 资源 | 一句话职责 |
   | --- | --- | --- | --- | --- | --- |

3. 对应「FPGA 资源」一列，回到 [README.md:57-59](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/README.md#L57-L59)：`u_ram` → BRAM 4096 bits；`u_multiplier` → DSP 块 / 两个 9-bit 乘法器；`u_alu` 与 `u_stack` → 普通逻辑单元（LE/ALM）与寄存器。
4. 留意每个实例里都有 `.i_EMUCLK(i_EMUCLK)` 或 `.i_CEN(cyc_ncen)` 这样的连接——确认四个子模块都共享顶层那个四分频出来的节拍。

**需要观察的现象**：四个子模块的实例化端口名与各自模块头声明一一对应；`u_ram` 的位宽与 BRAM 容量对得上，`u_multiplier` 与 DSP 块对得上。

**预期结果**：你得到一张对照卡，证明「源码里的四个子模块」恰好解释了「综合报告里的四类资源」。无需运行任何工具，这是纯阅读型实践。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `IKA32010_stack` 的数据端口是 12 位，而 `IKA32010_ram` 是 16 位？

**参考答案**：堆栈主要用来保存返回地址，而程序计数器 PC 是 12 位（见 [src/IKA32010.sv:98](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L98) 的 `reg [11:0] if_pc`），所以堆栈数据宽度取 12 位正好够用。RAM 存的是 16 位数据字（TMS32010 是 16 位定点 DSP），所以是 16 位。

**练习 2**：资源表里写「BRAM 4096 bits」，请用源码里的数字验证它。

**参考答案**：`IKA32010_ram` 里 `reg [15:0] RAM[0:255]`，即 256 个 16 位单元，容量为 \( 256 \times 16 = 4096 \) bit，与资源表完全一致。

**练习 3**：如果某天你想把数据 RAM 从 256 字扩到 512 字，除了改 `RAM[0:255]`，还要注意哪些端口/信号会受影响？

**参考答案**：RAM 的地址端口 `i_ADDR` 当前是 8 位（[src/IKA32010.sv:1914](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1914)），仅够寻址 256 字；扩到 512 字需要把地址位宽提到 9 位，进而影响顶层的 `ram_addr` 生成逻辑、间接寻址用的辅助寄存器位宽以及数据页指针 DP 的拼接方式。这会偏离原芯片定义，属于「二次开发」，不在本核心的目标范围内——本练习只是用来体会「位宽不是随便定的」。

## 5. 综合实践

把 4.1 和 4.2 串起来，完成下面这个小任务，作为本讲的收尾。

**任务：给 IKA32010 画一张「最小外围连线」草图。**

1. 把 4.1.4 整理出的端口清单按功能分组。
2. 假设你要在 FPGA 上用 IKA32010 跑一段最简单的程序，请画出它和外部世界最少需要连哪些信号：
   - 时钟与复位：`i_EMUCLK`、`i_CLKIN_PCEN`、`i_RS_n` 怎么接？
   - 程序 ROM：指令从哪进？（提示：`i_DIN` 与 `o_AOUT`、`o_MEN_n`）
   - 数据 I/O：`o_DOUT_OE` 在三态双向总线里扮演什么角色？
   - 暂时悬空/接常量的引脚：例如暂时不用中断时 `i_INT_n` 接成什么电平？（提示：回想 `_n` 的极性含义）
3. 在草图旁边标注每个信号的**有效极性**，并写明「为什么要这样接」。

**验收标准**：能解释清楚「为什么 `i_INT_n` 不用时通常接高电平」「为什么 `o_MEN_n` 是低有效却要接给 ROM 的片选」，就说明你已经真正掌握了端口命名约定。这一步不需要跑仿真，是纯设计型练习，为 u1-l5（仿真与 testbench）和 u1-l3（端口详解）打基础。

## 6. 本讲小结

- IKA32010 是一个用 **SystemVerilog** 写的 **TI TMS32010 DSP 软核**，BSD 2-Clause 许可，作者是 Sehyeon Kim (Raki)。
- 两个关键修饰词：**semi-cycle-accurate**（在机器周期粒度忠实重现对外时序）与 **FPGA proven**（已在真实 FPGA 上跑通真实程序）。
- 最强的正确性证据来自 README：在街机游戏 **Twin Cobra、Sky Shark、Wardner**（by atrac17）上验证通过。
- 顶层模块 `IKA32010` 的端口遵循「名字即文档」约定：`i_`/`o_` 表方向，`_n` 表低有效。
- 时钟分频逻辑（`cyclecntr` 0→3 循环）把 `EMUCLK` 四分频为一个 DSP 机器周期，\( f_{CLKOUT} = f_{EMUCLK}/4 \)。
- `src/IKA32010.sv` 内含四个子模块：**ALU**（运算+累加器+标志）、**RAM**（256×16 + DMOV）、**Stack**（4 级，12 位）、**Multiplier**（16×16 有符号→DSP 块）。

## 7. 下一步学习建议

本讲只看了「轮廓」。接下来建议按以下顺序推进：

1. **u1-l2 目录结构、源码与文档导航**：把 `src/` 四个文件与 `docs/` 三份资料的分工讲清楚，建立「从官方手册到源码」的对照阅读习惯。
2. **u1-l3 顶层模块端口与引脚定义**：把本讲 4.1.3 的端口表逐个讲透，理解每组总线控制信号的精确用途。
3. **u1-l4 时钟分频与周期计数器**：深入 4.1.3 ③ 的四分频逻辑，画出 `EMUCLK / o_CLKOUT / PCEN / NCEN` 的完整波形。
4. 若你想提前感受「跑起来」的样子，可以跳到 **u1-l5 仿真与 testbench 入门**，用 `src/IKA32010_tb.v` 喂几条指令看看波形——但建议先把端口和时钟这两讲过一遍，体验会更顺。

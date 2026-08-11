# FPGA 综合、外设接口与系统集成

## 1. 本讲目标

本讲是专家层最后一讲，也是整套手册的收尾。前面八讲我们都是在「IKA32010 这个软核内部」打转——讲它的数据通路、微码、指令译码。本讲换个视角：**站在内核外面**，把它当成一个「黑盒 IP 核」，学习如何把它集成进一个更大的 FPGA/SoC 系统。

学完后你应该能够：

1. 依据端口名独立完成 IKA32010 的实例化，并正确外接程序 ROM、数据 I/O 与时钟源。
2. 说清 `IKA32010_multiplier` 子模块为什么会被综合工具映射为 FPGA 的硬核 DSP 块，以及这种映射带来什么好处（资源、fmax）。
3. 掌握 IN/OUT 指令的 PA 端口寻址机制，能自行编写一个外设端口来捕获 DSP 输出的数据。
4. 了解多片 DSP 共存时如何用 `IKA32010_DEVICE_ID` 区分各自的反汇编日志。
5. 对「资源占用、fmax」这类 FPGA 后端指标有第一手的数值印象。

---

## 2. 前置知识

本讲默认你已经学过下面几讲（否则部分时序结论会显得突兀）：

- **u1-l3 顶层端口**：知道 `i_`/`o_` 表方向、`_n` 表低有效，知道 `o_MEN_n/o_DEN_n/o_WE_n` 三种选通与 `o_AOUT/i_DIN/o_DOUT` 的总线角色。
- **u1-l4 时钟分频**：知道 `cyclecntr` 把 `i_EMUCLK` 四分频为一个机器周期，`cyc_ncen`（相位 3）是主工作拍。
- **u2-l3 总线控制器**：知道 `busctrl_req` 编码六种事务（取指/表读/表写/IN/OUT/空闲），`busctrl_mode[3]` 是地址多路选择位。
- **u2-l8 乘法器**：知道 `IKA32010_multiplier` 做有符号 16×16→32 乘法，`result` 比操作数锁存晚一拍。

几个 FPGA 综合的基础术语（不熟悉的读者可先看这里的解释）：

- **软核（soft core）**：用 HDL（这里是 SystemVerilog）描述的处理器，综合后落到位级逻辑（LE/ALM/查找表）上，不是固定硅片。
- **LE / ALM**：Altera/Intel FPGA 的基本逻辑单元，LE（Logic Element）是老结构、ALM（Adaptive Logic Module）是新结构。
- **DSP 块 / 乘法器单元**：FPGA 内嵌的硬核乘加单元，专门用来做高速乘法，比用查找表拼出来的乘法器快得多、省得多。
- **BRAM**：FPGA 内嵌的块状静态 RAM，常被用来映射处理器核的片内 RAM。
- **fmax**：设计能稳定运行的最高时钟频率，通常给「慢角（slow，高温低压）」和「快角（fast，低温）」两个数。
- **三态 I/O（tri-state）**：FPGA 芯片引脚上才有真正的硬件三态；模块内部用「数据线 + 输出使能（OE）」一对信号来模拟。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/README.md) | 给出实例化模板、编译选项、两块 FPGA 的实测资源占用与 fmax。 |
| [src/IKA32010.sv](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv) | 顶层端口声明、乘法器子模块及其 DSP 映射注释、总线控制器（含 IN/OUT 事务时序）、IN/OUT 指令译码。 |
| [src/IKA32010_tb.v](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_tb.v) | 唯一 testbench：时钟/复位激励、程序 ROM 模型、总线复用，以及一个 PA0 输出锁存外设的范例。 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**顶层实例化与端口**、**IKA32010_multiplier 的 DSP 块映射**、**IN/OUT 外设寻址与系统集成**。最后的「综合实践」会把三者串成一个能跑的小系统。

### 4.1 顶层实例化与端口

#### 4.1.1 概念说明

IKA32010 对外暴露 **15 个端口**（6 入 9 出），没有任何参数化的配置接口——它就是一片「引脚定义固定」的虚拟芯片。要在 FPGA 里用它，标准做法和用任何 IP 核一样：

1. 把本仓库加进工程（或作为 submodule）。
2. 在你的顶层里照端口表实例化一个 `IKA32010`。
3. 给它接上「时钟源 + 程序 ROM + 可选的外设」。

它**没有片内程序 ROM**：所有指令都要经外部总线 `i_DIN` 读进来。所以最小可工作系统至少要有「时钟 + 复位 + 一段程序 ROM」三样。这与原始 TMS32010 的「程序挂在片外」完全一致，是历史 DSP 的典型结构。

端口命名继续遵守前几讲反复出现的「端口名即文档」三条规则：

- `i_` / `o_` 前缀 → 输入 / 输出方向。
- `_n` 后缀 → 低电平有效。
- `[x:0]` → 位宽。

#### 4.1.2 核心流程

实例化时的端口分组连线思路如下（伪代码式描述）：

```
时钟源(PLL/晶振) ──► i_EMUCLK
                 ──► i_CLKIN_PCEN   (恒为 1，或外部 1/4 占空比脉冲)
复位按钮/逻辑   ──► i_RS_n          (低有效)
程序 ROM        ◄──► o_AOUT(地址) / i_DIN(指令数据)，由 o_MEN_n 选通
外设输入(可选)  ──► i_DIN，由 o_DEN_n 选通   (配合 IN 指令)
外设输出(可选)  ◄──  o_DOUT / o_DOUT_OE，由 o_WE_n 选通 (配合 OUT 指令)
外部事件(可选)  ──► i_BIO_n (配合 BIOZ)、i_INT_n (下降沿中断)
对外时钟        ◄──  o_CLKOUT / o_CLKOUT_PCEN / o_CLKOUT_NCEN
```

关键点：

- `i_EMUCLK` 是主时钟；`i_CLKIN_PCEN` 是高有效的「正边沿时钟使能」，决定哪些 `i_EMUCLK` 边沿能让核内 `cyclecntr` 前进（详见 u1-l4）。**最简单**的接法是把它恒接 `1'b1`，此时 4 个 `i_EMUCLK` = 1 个机器周期；testbench 里为了拉长仿真时间，用了一个 1/4 占空比的窄脉冲接它。
- `o_AOUT` 有**双重身份**：取指/表读写时输出程序计数器 `if_pc`，IN/OUT 时输出 PA 端口地址（低 3 位有效），由 `busctrl_mode[3]` 切换（详见 4.3）。
- `i_DIN` 与 `o_DOUT` 是两条**独立单向**线，靠 `o_DOUT_OE` 在 FPGA 引脚层模拟双向三态。

#### 4.1.3 源码精读

顶层端口声明见 [src/IKA32010.sv:1-29](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1-L29)：这里逐个声明了 15 个端口，注释直接说明了每个信号的用途（例如 `o_MEN_n` 旁注 `external instruction read`、`o_DEN_n` 旁注 `IN instruction`、`o_WE_n` 旁注 `OUT instruction`）——**端口的注释本身就是引脚说明书**。

README 给出的官方实例化模板在 [README.md:17-41](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/README.md#L17-L41)：它把全部 15 个端口列在一个例化块里，留空让你填信号名。README 紧接着在 [README.md:45-50](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/README.md#L45-L50) 用人话解释了几个最容易接错的端口——尤其点明 `o_DOUT_OE` 是「FPGA 三态 I/O 驱动器的输出使能」。

一个真实可参考的实例化样本是 testbench 本身，见 [src/IKA32010_tb.v:30-51](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_tb.v#L30-L51)：

```verilog
IKA32010 main (
    .i_EMUCLK     (EMUCLK),
    .i_CLKIN_PCEN (~cen_n),   // 由 divider 产生的 1/4 占空比脉冲
    ...
    .o_AOUT       (ADDR),
    .i_DIN        (RDBUS),    // 指令/数据复用输入
    .o_DOUT       (WRBUS),
    .o_DOUT_OE    (           ),  // testbench 没用到输出，留空
    .i_BIO_n      (1'b1),     // 常拉高，不触发 BIOZ
    .i_INT_n      (INT_n)
);
```

注意它把 `o_MEN_n/o_DEN_n/o_WE_n` 接到三根 `wire` 上用来在外部做总线仲裁，把 `o_AOUT` 接到 `ADDR`、`i_DIN`/`o_DOUT` 接到 `RDBUS`/`WRBUS`——这就是「程序 ROM + 数据 I/O 挂在同一条总线」的最小形态。

#### 4.1.4 代码实践

**实践目标**：把端口表彻底吃透，建立「端口 ↔ 外接对象」的映射直觉。

**操作步骤**：

1. 打开 [src/IKA32010.sv:1-29](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1-L29)。
2. 为 15 个端口各填一行表格，包含：端口名、方向（in/out）、位宽、有效极性（高/低/边沿）、应外接的对象。

**需要观察的现象**：你应该能发现「输入只有 6 个、输出有 9 个」「低有效的信号全部带 `_n`」等规律，并能指出哪些端口在「最小取指系统」里是必需的（时钟/复位/地址/`i_DIN`/`o_MEN_n`），哪些只在用到特定功能时才需要（`i_INT_n`、`i_BIO_n`、`o_DOUT`/`o_WE_n` 等）。

**预期结果**：得到一张 15 行的端口表。参考答案见 4.1.5。

#### 4.1.5 小练习与答案

**练习 1**：如果只想让 DSP「跑程序、不碰任何外设」，最少需要接哪几个端口？

**参考答案**：`i_EMUCLK`、`i_CLKIN_PCEN`、`i_RS_n`、`o_AOUT`、`i_DIN`、`o_MEN_n` 共 6 个即可（构成「时钟 + 复位 + 程序 ROM」最小取指系统）。`o_CLKOUT*` 只是给外部观测用、不接也能跑。

**练习 2**：`i_DIN` 和 `o_DOUT` 为什么是两条独立的单向线，而不是一条 `inout`？

**参考答案**：在模块内部（芯片 fabric 里）没有真正的三态硬件，三态只在 FPGA 引脚上才有。所以内核用「独立的 `o_DOUT` 输出 + `o_DOUT_OE` 输出使能」这对信号，让**最顶层**（紧贴引脚的那一层）把它拼成一条真正的 `inout` 三态引脚。这样内核代码与具体的引脚工艺解耦。

---

### 4.2 IKA32010_multiplier 的 DSP 块映射

#### 4.2.1 概念说明

16×16 有符号乘法是整个 DSP 里最「贵」的运算：如果用查找表纯拼出来，要消耗大量 LE/ALM 且频率上不去。好在现代 FPGA 都内嵌了专用的硬核乘法器（Altera 叫 DSP block，Xilinx 叫 DSP slice）。我们要做的不是手动例化这些硬核，而是**把乘法器写成综合工具能识别的标准形式**，让工具自动把它「推断（infer）」成 DSP 块。

IKA32010 的乘法器子模块恰好就是这么写的：纯 `signed` 寄存器 + 一个 `*` 运算符。所有主流综合工具（Quartus、Vivado）都能把这种写法直接映射到硬核 DSP——源码作者甚至在注释里直接点名了「Quartus（DE10-nano）和 Vivado（Zybo-Z20）都会用 DSP 块综合得很好」。

#### 4.2.2 核心流程

乘法器的数据通路是一个三级流水（寄存器在两个操作数与结果之间）：

```
i_MUL_EN=1 时：
  T 拍：op0_latch <= i_OP0   (signed)
        op1_latch <= i_OP1   (signed)
  T+1 拍：result <= op0_latch * op1_latch   ← 乘法在这一拍完成
  对外：o_P = unsigned'(result)，比操作数锁存晚一个机器周期
```

这个「操作数先锁存、结果下一拍才出」的结构，恰好匹配 FPGA DSP 块内部「输入寄存器 → 乘法器 → 输出寄存器」的天然流水，所以：

- 综合器愿意把整段映射成**一个 DSP 块**（含其内置寄存器），而不是拆散落到逻辑单元上。
- 因为 `mul_en` 是整机器周期有效、操作数稳定，4 个 `EMUCLK` 边沿之后乘积必然正确，所以子模块**不需要 `cyc_ncen` 选通**（它直接认 `i_EMUCLK`），这也简化了时序约束。

> 数学上，有符号 16×16 乘积范围为 \(-2^{15}\timesimes 2^{15}\) 到 \(2^{15}\timesimes 2^{15}\)，即 \([-2^{30},\ 2^{30}]\)，放进 32 位有符号数（范围 \([-2^{31},\ 2^{31}-1]\)）绰绰有余，故 `result` 用 `signed [31:0]` 不会溢出。

#### 4.2.3 源码精读

子模块定义见 [src/IKA32010.sv:1985-2018](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1985-L2018)。关键几行：

- [src/IKA32010.sv:1996](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1996)：作者注释明确说 Quartus 与 Vivado 都会用 DSP 块综合——这就是「写法即约束」的信心来源。
- [src/IKA32010.sv:1999-2000](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1999-L2000)：`op0_latch, op1_latch` 为 `signed [15:0]`，`result` 为 `signed [31:0]`——全 `signed` 是让工具按有符号乘法器映射的前提。
- [src/IKA32010.sv:2011](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L2011)：`result <= op0_latch * op1_latch;`——一个 `*` 即可，工具自会识别。
- [src/IKA32010.sv:2016](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L2016)：`assign o_P = unsigned'(result);`——内核内部按无符号 wire 传 P 寄存器，避免顶层拼接时的符号扩展歧义。

顶层例化见 [src/IKA32010.sv:398-401](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L398-L401)：`i_OP0` 接 T 寄存器 `reg_t`，`i_OP1` 接第二操作数 `mul_op1`，`o_P` 接回 `reg_p`。

实测资源印证见 [README.md:57-59](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/README.md#L57-L59)：

| FPGA | 逻辑资源 | 片内 RAM | 乘法器 | fmax（慢角） | fmax（快角） |
| --- | --- | --- | --- | --- | --- |
| Altera EP4CE6E22C8（Cyclone IV） | 1243 LE / 275 reg | BRAM 4096 bit | 2 个 9-bit 乘法器单元 | 44.98 MHz（85°C） | 103.95 MHz（0°C） |
| Altera 5CSEBA6U23I7（MiSTer，Cyclone V） | 601 ALM / 275 reg | BRAM 4096 bit | **1 个 DSP 块** | 60.28 MHz（100°C） | 132.33 MHz（-40°C） |

注意两个细节：① 256×16 的数据 RAM 被映射成「BRAM 4096 bit」（正好 256×16=4096）；② Cyclone IV 上用了「2 个 9-bit 乘法器单元」（因为它的 DSP 较小，需多个拼成 16×16），而 Cyclone V 上干脆就是「1 个 DSP 块」。这正好说明同一份代码在不同 FPGA 上会落到不同形态的硬核。

#### 4.2.4 代码实践

**实践目标**：用真实资源数据验证「乘法器 → DSP 块」的映射确实发生。

**操作步骤**：

1. 把本工程加入 Quartus（或 Vivado），综合。
2. 打开编译报告，定位到「Resource Usage」/「DSP」一栏。
3. 对照 README 给出的两张表（[README.md:57-59](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/README.md#L57-L59)）。

**需要观察的现象**：综合报告里应出现「1 个 DSP block」（Cyclone V 类）或「2 个 9-bit multiplier」（Cyclone IV 类），且数据 RAM 落到 BRAM。

**预期结果**：与你所用芯片对应的 README 行基本吻合。若你用 Xilinx，预期会看到 1 个 DSP48 slice。**待本地验证**（取决于你手头的板子）。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `result <= op0_latch * op1_latch` 改成 `assign result = i_OP0 * i_OP1`（纯组合、无锁存），综合结果会有什么不同？

**参考答案**：工具仍会推断一个乘法器，但失去输入/输出寄存器，DSP 块内部的流水寄存器用不上，fmax 会下降，布线压力上升。作者特意保留 `op0_latch/op1_latch/result` 三级寄存器，正是为了让 DSP 块的内置寄存器被充分复用。

**练习 2**：为什么子模块的复位用 `i_RST_n`（接 `i_RS_n`）而工作节拍却**不**用 `cyc_ncen`？

**参考答案**：`mul_en` 在整机器周期内有效且操作数稳定，4 个 `EMUCLK` 边沿足够让流水走完，结果必然正确，所以无需用 `cyc_ncen` 选通来节流；而复位必须能清掉流水寄存器里的垃圾初值，所以接了 `i_RST_n`。这样既正确又便于时序约束。

---

### 4.3 IN/OUT 外设寻址与系统集成

#### 4.3.1 概念说明

DSP 与外设通信全靠 IN（外设→DSP）和 OUT（DSP→外设）两条指令。它们不像普通存储器指令那样去读写程序 ROM，而是走一条**独立的「外设事务」通道**：地址线 `o_AOUT` 的低 3 位当「外设端口编号（PA）」，配合 `o_DEN_n`（IN 选通）/`o_WE_n`（OUT 选通）来选中某个外设。

这就把系统设计成两类存储空间：

- **程序空间**：由 `o_MEN_n` 选通，地址 = `if_pc`，给指令 ROM 与表读写用。
- **I/O 空间**：由 `o_DEN_n`/`o_WE_n` 选通，地址 = PA 端口号，最多 8 个口（PA0~PA7），给外设用。

一个外设只要「监听 `o_AOUT[2:0]` 是否等于自己的编号 + 对应选通是否有效」就能与 DSP 通信。testbench 里的 `addrlatch` 就是一个标准范例。

另外，当系统里挂了**多片** IKA32010（或一片 DSP + 另一个 CPU）时，反汇编日志会混在一起。作者用 `IKA32010_DEVICE_ID` 给每片核打一个字符串标签，便于在日志里区分。

#### 4.3.2 核心流程

**地址 MUX**（决定 `o_AOUT` 输出什么）由微码的 `busctrl_addr_muxsel`（即 `busctrl_mode[3]`）控制：

```
busctrl_mode[3] == 0  →  o_AOUT = if_pc                  (程序空间)
busctrl_mode[3] == 1  →  o_AOUT = {9'b0, if_opcodereg[10:8]}   (PA 端口)
```

即 IN/OUT 时，PA 端口号直接取自**指令字的 [10:8] 位**，3 位最多编 8 个口。

**OUT 事务的总线时序**（一个机器周期内 4 个相位的电平）：

| 相位 (cyclecntr) | o_MEN_n | o_DEN_n | o_WE_n | o_DOUT_OE | 数据 |
| --- | --- | --- | --- | --- | --- |
| 0 | 1 | 1 | 1 | 0 | — |
| 1 | 1 | 1 | 1 | 1 | o_DOUT <= reg_wrbus（送上输出数据） |
| 2 | 1 | 1 | **0** | 1 | WE_n 拉低，外设在此采样 |
| 3 | 1 | 1 | 1 | 0 | 事务结束 |

所以**外设的标准捕获写法**是：在 `o_WE_n` 为低的那个 `EMUCLK` 边沿，且 `o_AOUT[2:0]` 等于自己端口号时，把 `o_DOUT` 锁存下来。

**IN 事务**对称：相位 0~2 拉低 `o_DEN_n`，外设在 `DEN_n` 为低时把数据驱动到 `i_DIN`，DSP 在相位 3 把 `i_DIN` 锁进 `busctrl_inlatch`，下一机器周期再写入数据 RAM。

三种选通 `o_MEN_n/o_DEN_n/o_WE_n` 在任何相位都互斥（同一时刻只有一个为低），保证总线不会被两个驱动源同时抢占。

#### 4.3.3 源码精读

地址多路选择见 [src/IKA32010.sv:159-164](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L159-L164)：`case(busctrl_mode[3])` 在 `if_pc` 与 `{9'b0, if_opcodereg[10:8]}`（注释写 `//PA0 1 2`）间二选一。对应的常量定义在 [src/IKA32010_mnemonics.sv:21-30](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_mnemonics.sv#L21-L30)：`BUSCTRL_ADDR_PC=1'd0`、`BUSCTRL_ADDR_PERIPHERAL=1'd1`、`COMMAND_IN=3'd4`、`COMMAND_OUT=3'd5`。

IN/OUT 的总线时序生成见 [src/IKA32010.sv:233-252](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L233-L252)：这正是上面两张时序表的源头——IN 分支在相位 3 把 `i_DIN` 锁进 `busctrl_inlatch`（第 239 行），OUT 分支在相位 1 把 `reg_wrbus` 送上 `o_DOUT`、相位 2 拉低 `o_WE_n`（第 247-249 行）。

IN/OUT 指令的微码译码见 [src/IKA32010.sv:1604-1661](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1604-L1661)。两条都是**两周期指令**，遵循 u3-l2 讲过的 `ex_inst_cycle` 分相位模板：

- OUT（[L1634-L1661](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1634-L1661)）：cycle0 设 `COMMAND_OUT + BUSCTRL_ADDR_PERIPHERAL`，PC 保持，输出数据取自 RAM（`WRBUS_SOURCE_RAM`）；cycle1 恢复 `OPCODE_READ`。
- IN（[L1604-L1632](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1604-L1632)）：cycle0 设 `COMMAND_IN + BUSCTRL_ADDR_PERIPHERAL` 发起外设读；cycle1 把 `busctrl_inlatch` 经 `WRBUS_SOURCE_INLATCH` 写入 RAM。

**外设捕获的标准范例**就在 testbench 里，见 [src/IKA32010_tb.v:56](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_tb.v#L56)：

```verilog
reg [15:0] addrlatch;
always @(posedge EMUCLK) if(!WE_n && ADDR[2:0] == 3'd0) addrlatch <= WRBUS;
```

这一行就是一个「PA0 输出端口」外设：每当 OUT 选通 `WE_n` 为低、且地址低 3 位为 0（PA0）时，把 `o_DOUT`（即 `WRBUS`）锁存。它就是我们 4.3.2 所述「外设标准捕获写法」的直接实现。

**多 DSP 调试标签**：三个编译宏总开关在 [src/IKA32010.sv:33-35](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L33-L35)：

```verilog
`define IKA32010_DISASSEMBLY
`define IKA32010_DISASSEMBLY_SHOWID
`define IKA32010_DEVICE_ID "ikakawa"
```

反汇编函数把 `IKA32010_DEVICE_ID` 拼到每行日志前缀（见 [src/IKA32010_disasm.sv:12-13](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_disasm.sv#L12-L13)），于是多片核同时跑时，你能从行首的 `IKA32010_xxx:` 分辨每条指令来自哪片。README 对这三个宏的说明见 [README.md:52-55](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/README.md#L52-L55)。

#### 4.3.4 代码实践

**实践目标**：亲手写一个 PA0 输出外设，并跟踪 OUT 指令把数据从 RAM 送到外设的全链路。

**操作步骤**：

1. 在 testbench 里找到 [src/IKA32010_tb.v:56](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_tb.v#L56) 这一行。
2. 仿照它再写一个 PA1 输出锁存：`always @(posedge EMUCLK) if(!WE_n && ADDR[2:0]==3'd1) pa1_latch <= WRBUS;`。
3. 在源码里跟踪 OUT 指令（[L1634-L1661](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1634-L1661)）：确认 PA 端口号取自 `if_opcodereg[10:8]`，数据取自 RAM。

**需要观察的现象**：当 DSP 执行 `OUT DMA, PA1` 时，`pa1_latch` 应更新为该 RAM 单元的值，而 `addrlatch`（PA0）不变；反之 `OUT ..., PA0` 只动 `addrlatch`。两个外设互不干扰，证明 PA 地址译码正确。

**预期结果**：两个锁存器各自只响应自己端口号的 OUT。**待本地验证**（需要一段会分别向 PA0、PA1 输出的测试程序，见第 5 节综合实践）。

#### 4.3.5 小练习与答案

**练习 1**：OUT 指令最多能寻址几个外设端口？由指令字的哪些位决定？

**参考答案**：最多 8 个（PA0~PA7），由指令字的 `[10:8]` 三位决定（见 [src/IKA32010.sv:162](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L162)）。

**练习 2**：为什么外设锁存用 `posedge EMUCLK` 而不是 `cyc_ncen`？

**参考答案**：`o_WE_n` 只在相位 2 的一个 `EMUCLK` 周期内为低（见 [src/IKA32010.sv:249](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L249)）。用 `posedge EMUCLK` 能在那一拍稳定捕获 `o_DOUT`；若用 `cyc_ncen`（只在相位 3 一拍），那时 `o_WE_n` 已经抬起来、`o_DOUT_OE` 也已关闭，就采不到正确数据了。

**练习 3**：系统里同时跑两片 IKA32010，怎么让反汇编日志不混？

**参考答案**：给两片分别定义不同的 `IKA32010_DEVICE_ID` 字符串（例如 `"left"`/`"right"`），并都打开 `IKA32010_DISASSEMBLY_SHOWID`。反汇编函数会把该 ID 拼到每行日志行首（[src/IKA32010_disasm.sv:12-13](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_disasm.sv#L12-L13)），即可从行首区分。

---

## 5. 综合实践

把本讲三个模块串起来：**在一个新顶层里实例化 IKA32010，外接一段程序 ROM 和一个 PA0 输出端口，写一段最短的 DSP 程序用 OUT 指令输出数据，并在仿真中观察端口波形与锁存结果。**

### 5.1 数据流设计

```
LACK 0x55      →  ACC = 0x00000055
SACL DMA0      →  RAM[0x00] = ACC低16位 = 0x0055
OUT DMA0, PA0  →  o_DOUT = RAM[0x00] = 0x0055，o_AOUT[2:0]=0(=PA0)，WE_n 发脉冲
                  →  外设 PA0 锁存 pa0_latch = 0x0055
```

OUT 指令的编码格式为 `0100_1???_????_????`：`[10:8]` 是 PA 号，`[7:0]` 是数据存储器操作数。所用到的几条指令编码均已与源码 `casez` 分支核对：

| 地址 | 机器码 | 助记符 | 对应源码 casez |
| --- | --- | --- | --- |
| 0x000 | `0x7E55` | `LACK 0x55` | [src/IKA32010.sv:880](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L880)（`0111_1110_????_????`） |
| 0x001 | `0x5000` | `SACL DMA0, shift 0` | [src/IKA32010.sv:928](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L928)（`0101_0???_????_????`） |
| 0x002 | `0x4800` | `OUT DMA0, PA0` | [src/IKA32010.sv:1635](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1635)（`0100_1???_????_????`） |
| 0x003 | `0x7F80` | `NOP` | [src/IKA32010.sv:639](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L639)（`0111_1111_1000_0000`） |

### 5.2 一个自包含的最小 testbench（示例代码）

下面是一个**不依赖任何外部 ROM 文件**的最小 testbench，把程序直接写在 `initial` 块里。它综合了本仓库 testbench 的时钟/复位/ROM 模型（[src/IKA32010_tb.v:1-84](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_tb.v#L1-L84)）与 PA0 锁存外设（[src/IKA32010_tb.v:56](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_tb.v#L56)）的写法。

```verilog
// 示例代码：最小 OUT 演示 testbench
`timescale 10ps/10ps
module tb_out_demo;
    reg  EMUCLK = 1'b1;
    reg  RS_n  = 1'b1;
    always #1 EMUCLK = ~EMUCLK;

    // 1/4 占空比的 PCEN（沿用原 testbench 做法）
    reg [1:0] divider = 2'd0;
    always @(posedge EMUCLK) divider <= divider + 2'd1;
    wire cen  = (divider == 2'd3);

    // 复位时序
    initial begin
        #20 RS_n = 1'b0;
        #40 RS_n = 1'b1;
        #4000 $display("PA0_LATCH = %h", pa0_latch);
        #10 $finish;
    end

    wire        MEN_n, DEN_n, WE_n;
    wire [11:0] AOUT;
    wire [15:0] DOUT;
    wire        DOUT_OE;

    // ─── 程序 ROM：只响应 o_MEN_n（指令读）───
    reg [15:0] progrom [0:4095];
    integer i;
    initial begin
        for(i=0;i<4096;i=i+1) progrom[i] = 16'h7F80; // 默认填 NOP
        progrom[0] = 16'h7E55; // LACK 0x55
        progrom[1] = 16'h5000; // SACL DMA0
        progrom[2] = 16'h4800; // OUT DMA0, PA0
        progrom[3] = 16'h7F80; // NOP
    end
    // 取指时把指令送上 i_DIN；非取指则让出总线（高阻）
    wire [15:0] rom_data = (MEN_n) ? 16'hZZZZ : progrom[AOUT];

    // ─── DUT ───
    IKA32010 u_dut (
        .i_EMUCLK(EMUCLK), .i_CLKIN_PCEN(cen),
        .o_CLKOUT(), .o_CLKOUT_PCEN(), .o_CLKOUT_NCEN(),
        .i_RS_n(RS_n),
        .o_MEN_n(MEN_n), .o_DEN_n(DEN_n), .o_WE_n(WE_n),
        .o_AOUT(AOUT), .i_DIN(rom_data), .o_DOUT(DOUT), .o_DOUT_OE(DOUT_OE),
        .i_BIO_n(1'b1), .i_INT_n(1'b1)
    );

    // ─── PA0 输出外设：照搬 tb 第 56 行写法 ───
    reg [15:0] pa0_latch;
    always @(posedge EMUCLK) if(!WE_n && AOUT[2:0]==3'd0) pa0_latch <= DOUT;
endmodule
```

### 5.3 操作步骤

1. 新建工程，加入 `src/IKA32010.sv`、`src/IKA32010_mnemonics.sv`、`src/IKA32010_disasm.sv`（保持 `IKA32010_DISASSEMBLY` 宏打开，便于看执行轨迹）。
2. 把上面的 testbench 存为 `tb_out_demo.v` 加入工程。
3. 仿真运行到 `$finish`。

### 5.4 需要观察的现象与预期结果

- 反汇编日志应依次出现 `LACK`、`SACL`、`OUT`、`NOP`（行首带 `IKA32010_ikakawa:` 前缀，验证 4.3 的 DEVICE_ID 机制）。
- 波形中，OUT 指令所在机器周期的相位 2 出现 `WE_n` 单拍低脉冲、`AOUT[2:0]=3'b000`、`DOUT=0x0055`。
- 仿真结束前 `$display` 打印 `PA0_LATCH = 0055`。

若结果不符，先核对：复位是否正常释放、`i_CLKIN_PCEN` 是否正确（接 `1'b1` 或 1/4 占空比脉冲均可，但必须有时钟使能）、程序 ROM 的 `i_DIN` 高阻让出是否正确。**完整时序待本地验证。**

---

## 6. 本讲小结

- IKA32010 是一个端口固定的「黑盒 IP 核」：15 个端口（6 入 9 出），最小可工作系统只需「时钟 + 复位 + 程序 ROM + `i_DIN/i_DOUT`/`o_MEN_n` 等总线」，无片内程序 ROM。
- 实例化模板见 [README.md:17-41](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/README.md#L17-L41)；端口逐条含义见 [src/IKA32010.sv:1-29](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1-L29)；真实范例见 testbench [src/IKA32010_tb.v:30-51](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_tb.v#L30-L51)。
- `IKA32010_multiplier` 用纯 `signed` + `*` 写法（[src/IKA32010.sv:1985-2018](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1985-L2018)），被综合工具自动映射为 FPGA 硬核 DSP 块，实测 Cyclone V 上 1 个 DSP 块、fmax 可达 132 MHz（[README.md:57-59](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/README.md#L57-L59)）。
- IN/OUT 用 PA 端口寻址：`o_AOUT[2:0]` 取自指令字 `[10:8]`（[src/IKA32010.sv:159-164](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L159-L164)），配合 `o_DEN_n`（IN）/`o_WE_n`（OUT）选通（[src/IKA32010.sv:233-252](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L233-L252)）。
- 外设标准写法是「`!WE_n && AOUT[2:0]==端口号` 时锁存 `o_DOUT`」，直接范例见 [src/IKA32010_tb.v:56](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_tb.v#L56)。
- 多片 DSP 系统用 `IKA32010_DEVICE_ID` 字符串标签区分反汇编日志（[src/IKA32010.sv:33-35](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L33-L35)）。

---

## 7. 下一步学习建议

到这里，IKA32010 学习手册的三层（入门 / 进阶 / 专家）已全部走完，你已经具备了从「读端口」到「改微码」再到「上板集成」的完整能力。建议的后续方向：

1. **真刀真枪上板**：在本讲综合实践的基础上，把 `tb_out_demo` 改造成可综合顶层，把 `pa0_latch` 接到板子上的 LED 或串口，亲眼看到 DSP 输出的数据。
2. **跑真实程序**：对照 docs 里的 TMS32010 手册，把一段真实街机 ROM（README 提到的 Twin Cobra 等）喂给核，用 `IKA32010_DISASSEMBLY` 日志验证自己的理解。
3. **系统级扩展**：尝试在程序空间之外挂一个「数据 ROM」配合 TBLR，或实现一个 PA 端口上的简单外设（如 PWM、UART），加深对 4.3「I/O 空间 vs 程序空间」的理解。
4. **回看微码**：若想深入二次开发，回到 u3-l1～u3-l7，挑一条指令，试着在 `casez` 里新增一条自定义指令（记得同步更新 mnemonics 常量与 disasm 函数），完成一次端到端的「改 ISA」练习。

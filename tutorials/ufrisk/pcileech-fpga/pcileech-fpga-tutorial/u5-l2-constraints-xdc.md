# 约束文件：引脚映射与时序约束

## 1. 本讲目标

学完本讲后，你应当能够：

- 读懂 Xilinx 约束文件 `.xdc` 的三大类指令：**物理实现约束**（`PACKAGE_PIN`/`IOSTANDARD`/`LOC`）、**时序约束**（`create_clock`/`set_input_delay`/`set_output_delay`）、**时序例外**（`set_false_path`/`set_multicycle_path`）。
- 看懂 `pcileech_squirrel.xdc` 如何把顶层端口一一绑定到 Squirrel 板卡（XC7A35T-484）的物理引脚，并设置正确的电气标准与压摆率。
- 理解 FT601 这一高速并行接口为什么需要 `set_input_delay`/`set_output_delay`、`IOB TRUE` 寄存器打包和一条 `set_multicycle_path`。
- 认识 `set_false_path` 对 `tickcount64`、`_pcie_core_config`、`user_lnk_up_int`、`user_reset_out` 等「慢速/异步」路径的放过处理。
- 在 xdc 中找到 PCIe 收发器（`pcie_rx_p/n`、`pcie_tx_p/n`、`pcie_clk_p/n`）以及 `GTPE2_CHANNEL` 的 `LOC` 约束，并说明它们与板卡硬件走线的对应关系。

## 2. 前置知识

在进入源码前，先用三段话补齐必要背景。

**约束文件是什么。** 综合（synthesis）把 SystemVerilog 翻译成逻辑网表，实现（implementation）再把网表里的逻辑「摆」到 FPGA 真实的物理资源上。但综合工具并不知道：某个端口应该走芯片的哪根引脚？这根引脚该用多高的电压标准？某条时钟走多快？哪些路径不必卡死时序？这些「人知道、工具不知道」的信息，就由约束文件（Xilinx 中即 `.xdc`，本质是一系列 Tcl 命令）告诉工具。可以说：**HDL 描述逻辑功能，XDC 描述物理与时间现实。** 没有 XDC，工程能综合却无法完成实现。

**XDC 的三类指令。**

| 类别 | 代表命令 | 作用 |
| --- | --- | --- |
| 物理实现约束 | `PACKAGE_PIN`、`IOSTANDARD`、`LOC`、`SLEW`、`IOB` | 把端口/单元钉到具体物理位置，设置电气属性 |
| 时序约束 | `create_clock`、`set_input_delay`、`set_output_delay` | 声明时钟与外部引脚的相对时序关系，驱动建立/保持时间检查 |
| 时序例外 | `set_false_path`、`set_multicycle_path` | 告诉分析器「这条路径不需要单周期检查」，放过它 |

**为什么高速并行总线特别需要约束。** FT601 是一颗 USB3 桥接芯片，它与 FPGA 之间是一组 32 位数据线加若干控制线，跑在 100MHz。这种并行接口对时序极其敏感：数据在引脚上必须在外部器件采样窗口内稳定。为此需要三招——用 `set_input_delay`/`set_output_delay` 描述外部器件的采样关系，用 `IOB TRUE` 把驱动寄存器塞进紧挨引脚的 IOB（I/O Block）以缩短走线延迟，用 `set_multicycle_path` 放松那些本就慢一拍的控制路径。这三招在本讲的 FT601 段落里都会原样出现。

本讲承接 u5-l1：u5-l1 讲了工程有三大异步时钟域（`clk`/`clk_com`/`clk_pcie`），本讲正是这些时钟在 XDC 里的「声明形式」与「跨域处理」的落地。

## 3. 本讲源码地图

本讲只读一个文件，但它是工程能否在真实板卡上跑起来的关键。

| 文件 | 作用 |
| --- | --- |
| `PCIeSquirrel/src/pcileech_squirrel.xdc` | Squirrel 板卡的完整约束：FT601/LED/按键/系统时钟/PCIe 引脚分配、时钟创建、FT601 输入输出延迟、IOB 打包、时序例外、GTP 通道定位、比特流属性 |

需要交叉确认的源码（不在 source_files 列表，但用于验证层次路径）：

- `PCIeSquirrel/src/pcileech_squirrel_top.sv`：顶层端口定义，XDC 里每一个 `[get_ports xxx]` 都对应这里的端口名。
- `PCIeSquirrel/src/pcileech_ft601.sv`：`FT601_OE_N`/`FT601_RD_N`/`FT601_WR_N`/`FT601_DATA_OUT`/`OE` 寄存器，验证 IOB 与 multicycle 约束指向的真实单元。
- `PCIeSquirrel/src/pcileech_com.sv`：确认 `i_pcileech_ft601` 实例名，组成约束里的层次路径。

## 4. 核心概念与源码讲解

### 4.1 引脚约束：PACKAGE_PIN / IOSTANDARD / SLEW

#### 4.1.1 概念说明

FPGA 芯片有数百个引脚（Squirrel 用的 XC7A35T-484 是 484 脚 BGA 封装）。顶层的每一个端口（如 `ft601_data[0]`、`clk`、`pcie_rx_p[0]`）最终都要落到某一根物理引脚上。这件事由两个核心属性决定：

- **`PACKAGE_PIN`**：指定端口绑定到芯片封装的哪个焊球（如 `Y18`、`H4`）。这由**板卡原理图**决定——PCB 上这根 FPGA 焊球连到了 FT601 的哪根脚，就写哪个封装位号。
- **`IOSTANDARD`**：指定这根引脚的电气标准。`LVCMOS33` 表示 3.3V 单端 LVCMOS，必须和板卡上该引脚的供电电压（VCCO）一致，否则综合实现会报错或烧件。

对于输出引脚，常常再加 **`SLEW`**：`FAST` 表示压摆率（电平翻转速度）快，适合高速并行总线；`SLOW` 省功耗、减 EMI。

#### 4.1.2 核心流程

给一个端口加约束的固定套路是「先定位、再设标准、再设属性」：

```tcl
# 1) 把端口钉到某根引脚
set_property PACKAGE_PIN <位号> [get_ports <端口名>]
# 2) 设置电气标准
set_property IOSTANDARD LVCMOS33 [get_ports <端口名>]
# 3) （输出可选）设置压摆率
set_property SLEW FAST [get_ports <端口名>]
```

XDC 支持用通配符 `{a[*] b[*]}` 一次给一组端口批量设置，避免逐根重复。

#### 4.1.3 源码精读

FT601 的字节使能与数据线逐一绑定到引脚（节选）：

```tcl
set_property PACKAGE_PIN Y18  [get_ports ft601_be[0]]
set_property PACKAGE_PIN N13  [get_ports ft601_data[0]]
...
set_property PACKAGE_PIN AB20 [get_ports ft601_data[31]]
```

这几行把 `ft601_be[0..3]` 与 `ft601_data[0..31]` 逐根钉到具体焊球。完整逐行清单见 [pcileech_squirrel.xdc:L1-L36](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel.xdc#L1-L36)（中文说明：为 FT601 的 4 根字节使能与 32 根数据线逐一指定封装引脚位号，位号由 Squirrel 板卡 PCB 走线决定）。

FT601 的控制信号（读写、使能、复位）同样绑定，再用通配符批量设标准与压摆率：

```tcl
set_property IOSTANDARD LVCMOS33 [get_ports {ft601_txe_n ft601_rxf_n}]
set_property IOSTANDARD LVCMOS33 [get_ports {{ft601_be[*]} {ft601_data[*]}}]
set_property IOSTANDARD LVCMOS33 [get_ports {ft601_wr_n ft601_rd_n ft601_oe_n ft601_siwu_n ft601_rst_n}]
set_property SLEW FAST [get_ports {{ft601_be[*]} {ft601_data[*]}}]
set_property SLEW FAST [get_ports {ft601_wr_n ft601_rd_n ft601_oe_n ft601_siwu_n ft601_rst_n}]
```

见 [pcileech_squirrel.xdc:L44-L48](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel.xdc#L44-L48)（中文说明：所有 FT601 信号统一为 LVCMOS33 标准；输出类信号全部设为 FAST 压摆率，匹配 100MHz 并行总线的快速翻转需求）。

LED、按键、FT2232 复位脚同理，单根引脚加标准即可，见 [pcileech_squirrel.xdc:L50-L57](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel.xdc#L50-L57)（中文说明：`user_ld1/ld2`、`user_sw1_n/sw2_n`、`ft2232_rst_n` 绑定到板卡 LED/按键/FT2232 复位引脚，均为 LVCMOS33）。

#### 4.1.4 代码实践

**实践目标**：建立「端口 ↔ 引脚 ↔ 板卡功能」的对应直觉。

**操作步骤**：

1. 打开 `PCIeSquirrel/src/pcileech_squirrel_top.sv` 的端口列表（[L19-L52](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L19-L52)）。
2. 在 xdc 里找到 `clk` 与 `ft601_clk` 两条时钟端口。
3. 对照两条 `create_clock` 命令的周期值。

**需要观察的现象**：`clk` 绑在 `H4`、`ft601_clk` 绑在 `W19`，两者都被声明为 10ns（100MHz）周期。

**预期结果**：你会确认「板载系统晶振」与「FT601 输出的通信时钟」是两个不同物理来源的 100MHz 时钟，各自占一根引脚——这正是 u5-l1 讲的「两大异步时钟域」在物理层的体现。如果手头有 Squirrel 原理图，可进一步核对 `H4` 是否连到板载晶振、`W19` 是否来自 FT601 的 CLKOUT 引脚。

#### 4.1.5 小练习与答案

**练习 1**：为何 `ft601_data` 设为 `SLEW FAST`，而 `user_ld1`（LED）不设？
**答案**：`ft601_data` 是 100MHz 并行高速总线，需要快翻转保证采样窗口；LED 只是人眼可见的指示，翻转频率低，`SLOW`/默认即可，还能降低电源噪声与 EMI。

**练习 2**：如果某根 FT601 数据线在 xdc 里漏写了 `PACKAGE_PIN`，综合会过、实现会怎样？
**答案**：实现阶段会因「端口未分配引脚」报错（`DRC NSTD-1` 之类），无法生成 bitstream。XDC 的引脚分配是实现阶段才强制检查的。

---

### 4.2 时序约束：create_clock 与 input/output delay

#### 4.2.1 概念说明

时序分析器需要知道两件事才能检查建立/保持时间：**时钟长什么样**，以及**数据相对时钟在引脚上是什么时候到的**。

- **`create_clock`**：声明一个时钟——周期、波形、源头。源头可以是某个端口（物理输入引脚），也可以是某条内部网线。
- **`set_input_delay`**：描述「外部器件驱动数据到达 FPGA 输入引脚时，相对于参考时钟的最大/最小延迟」。分析器据此判断 FPGA 内部第一级寄存器能否正确采样。
- **`set_output_delay`**：描述「FPGA 输出数据到达外部器件、并被其采样所需的相对时间余量」。分析器据此判断 FPGA 输出寄存器是否要更早把数据推出去。

`-min`/`-max` 分别给出最坏情况的两端，工具据此做最严苛的检查。

#### 4.2.2 核心流程

FT601 这一并行接口的时序建模三步：

```tcl
# 1) 声明 FT601 时钟（来源是 ft601_clk 端口）
create_clock -period 10.000 -name net_ft601_clk -waveform {0.000 5.000} [get_ports ft601_clk]
# 2) 输入：FT601 驱动的数据/状态线相对该时钟的到达延迟
set_input_delay -clock net_ft601_clk -min 6.5 [...]
set_input_delay -clock net_ft601_clk -max 7.0 [...]
# 3) 输出：FPGA 驱动的控制/数据线相对该时钟的余量要求
set_output_delay -clock net_ft601_clk -max 1.0  [...]
set_output_delay -clock net_ft601_clk -min 4.8  [...]
```

`-waveform {0.000 5.000}` 表示时钟上升沿在 0ns、下降沿在 5ns，即占空比 50%。

#### 4.2.3 源码精读

系统时钟与 FT601 时钟各创建一次：

```tcl
# SYSCLK
set_property PACKAGE_PIN H4 [get_ports clk]
set_property IOSTANDARD LVCMOS33 [get_ports clk]
create_clock -period 10.000 -name net_clk -waveform {0.000 5.000} [get_ports clk]

# FT601 CLK
create_clock -period 10.000 -name net_ft601_clk -waveform {0.000 5.000} [get_ports ft601_clk]
set_property IOSTANDARD LVCMOS33 [get_ports ft601_clk]
set_property PACKAGE_PIN W19 [get_ports ft601_clk]
```

见 [pcileech_squirrel.xdc:L59-L67](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel.xdc#L59-L67)（中文说明：声明系统时钟 `net_clk` 与通信时钟 `net_ft601_clk`，均为 100MHz 周期、50% 占空比，分别源自 `clk` 与 `ft601_clk` 端口）。

FT601 输入延迟（数据与状态线）：

```tcl
set_input_delay -clock [get_clocks net_ft601_clk] -min 6.5 [get_ports {ft601_data[*]}]
set_input_delay -clock [get_clocks net_ft601_clk] -max 7.0 [get_ports {ft601_data[*]}]
set_input_delay -clock [get_clocks net_ft601_clk] -min 6.5 [get_ports ft601_rxf_n]
set_input_delay -clock [get_clocks net_ft601_clk] -max 7.0 [get_ports ft601_rxf_n]
set_input_delay -clock [get_clocks net_ft601_clk] -min 6.5 [get_ports ft601_txe_n]
set_input_delay -clock [get_clocks net_ft601_clk] -max 7.0 [get_ports ft601_txe_n]
```

见 [pcileech_squirrel.xdc:L69-L74](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel.xdc#L69-L74)（中文说明：FT601 驱动的 32 位数据线及 `rxf_n`/`txe_n` 状态线，相对 `net_ft601_clk` 的到达延迟为 6.5~7.0ns，告诉分析器数据在引脚上稳定的时间窗）。

FT601 输出延迟（控制与数据线）：

```tcl
set_output_delay -clock [get_clocks net_ft601_clk] -max 1.0 [get_ports {ft601_wr_n ft601_rd_n ft601_oe_n}]
set_output_delay -clock [get_clocks net_ft601_clk] -min 4.8 [get_ports {ft601_wr_n ft601_rd_n ft601_oe_n}]
set_output_delay -clock [get_clocks net_ft601_clk] -max 1.0 [get_ports {{ft601_be[*]} {ft601_data[*]}}]
set_output_delay -clock [get_clocks net_ft601_clk] -min 4.8 [get_ports {{ft601_be[*]} {ft601_data[*]}}]
```

见 [pcileech_squirrel.xdc:L76-L79](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel.xdc#L76-L79)（中文说明：FPGA 输出给 FT601 的读写/使能控制线与数据线，输出余量要求 max=1.0ns、min=4.8ns，约束 FPGA 必须在 FT601 采样窗内把数据稳定推出）。

#### 4.2.4 代码实践

**实践目标**：理解 `-min`/`-max` 与建立/保持时间检查的关系。

**操作步骤**：

1. 假设把 `set_input_delay -max` 从 7.0 改大到 9.5（接近时钟周期 10ns）。
2. 推断：FPGA 内部第一级采样寄存器的建立时间余量会发生什么变化。

**需要观察的现象**：`-max` 越大，表示「外部数据来得越晚」，留给 FPGA 内部走线的建立时间余量越小。

**预期结果**：当 `-max` 接近周期（10ns）时，建立时间检查会失败（setup violation），实现阶段会报 WNS（Worst Negative Slack）为负。这是「待本地验证」的推断——若你有 Vivado 工程，可在改完后重新综合实现查看时序报告。

#### 4.2.5 小练习与答案

**练习 1**：`set_input_delay` 和 `set_output_delay` 各自站在谁的视角？
**答案**：`set_input_delay` 站在 FPGA 视角，描述外部器件把数据送到 FPGA 输入引脚的延迟；`set_output_delay` 描述 FPGA 输出数据要在何时被外部器件正确采样所需的余量。

**练习 2**：为什么系统时钟 `net_clk` 没有 `set_input_delay`/`set_output_delay`？
**答案**：`net_clk` 是 FPGA 的本地工作时钟，源就在 FPGA 内部驱动逻辑，不存在「外部器件采样 FPGA 引脚」的接口；只有像 FT601 这样与外部器件并行对接的总线才需要 input/output delay。

---

### 4.3 IOB 打包与输出多周期路径

#### 4.3.1 概念说明

光给 delay 还不够，要让输出寄存器尽量靠近引脚。FPGA 的每个 I/O Block（IOB）内部可以放一个寄存器，紧贴焊球。把驱动某引脚的寄存器「打包」进 IOB 的好处是：寄存器到引脚的走线极短、延迟极小且稳定，时序可预测。这由 `set_property IOB TRUE` 作用在**单元**（`get_cells`，而非 `get_ports`）上实现。

注意层次：约束单元时要写完整的实例层次路径。本工程里 FT601 的驱动寄存器位于顶层例化的 `i_pcileech_com` 内部的 `i_pcileech_ft601`，故路径前缀是 `i_pcileech_com/i_pcileech_ft601/`。这些寄存器名（`FT601_OE_N_reg`、`FT601_RD_N_reg`、`FT601_WR_N_reg`、`FT601_DATA_OUT_reg`）在 `pcileech_ft601.sv` 中确有定义。

此外，FT601 的输出使能 `OE` 是一个状态机驱动的慢速控制信号——它只在 RX/TX 切换时翻转，而非每拍都变。数据驱动器在 `OE` 置起后才真正对外有效。这种「控制慢、数据配合」的路径，可以安全地放宽为 2 周期路径，用 `set_multicycle_path 2` 告诉分析器别按单周期卡它。

#### 4.3.2 核心流程

```tcl
# 1) 把关键输出寄存器打包进 IOB（针对单元 get_cells）
set_property IOB TRUE [get_cells i_pcileech_com/i_pcileech_ft601/FT601_OE_N_reg]
set_property IOB TRUE [get_cells i_pcileech_com/i_pcileech_ft601/FT601_RD_N_reg]
set_property IOB TRUE [get_cells i_pcileech_com/i_pcileech_ft601/FT601_WR_N_reg]
set_property IOB TRUE [get_cells i_pcileech_com/i_pcileech_ft601/FT601_DATA_OUT_reg[0][*]]

# 2) OE 控制路径放宽为 2 周期
set_multicycle_path 2 -from [get_pins i_pcileech_com/i_pcileech_ft601/OE_reg/C] -to [get_ports {{ft601_be[*]} {ft601_data[*]}}]
```

#### 4.3.3 源码精读

IOB 打包四行：

```tcl
set_property IOB TRUE [get_cells i_pcileech_com/i_pcileech_ft601/FT601_OE_N_reg]
set_property IOB TRUE [get_cells i_pcileech_com/i_pcileech_ft601/FT601_RD_N_reg]
set_property IOB TRUE [get_cells i_pcileech_com/i_pcileech_ft601/FT601_WR_N_reg]
set_property IOB TRUE [get_cells i_pcileech_com/i_pcileech_ft601/FT601_DATA_OUT_reg[0][*]]
```

见 [pcileech_squirrel.xdc:L81-L84](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel.xdc#L81-L84)（中文说明：把 FT601 的输出使能/读/写控制寄存器与数据输出寄存器 `FT601_DATA_OUT[0]` 全部打包进 IOB，使驱动点紧贴引脚、缩短输出延迟）。这些寄存器真实存在于 `pcileech_ft601.sv`，见 [pcileech_ft601.sv:L20-L22 与 L54](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_ft601.sv#L20-L22)（中文说明：`FT601_OE_N/RD_N/WR_N` 是模块输出位，`FT601_DATA_OUT[5]` 是 5 深度的数据队列数组，综合后即对应上面的 `_reg`）。实例名 `i_pcileech_ft601` 在 `pcileech_com.sv` 例化处，见 [pcileech_com.sv:L190](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_com.sv#L190)（中文说明：`pcileech_com` 内部例化 FT601 模块时取名 `i_pcileech_ft601`，构成约束层次路径）。

输出多周期路径：

```tcl
set_multicycle_path 2 -from [get_pins i_pcileech_com/i_pcileech_ft601/OE_reg/C] -to [get_ports {{ft601_be[*]} {ft601_data[*]}}]
```

见 [pcileech_squirrel.xdc:L86](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel.xdc#L86)（中文说明：从 FT601 输出使能寄存器 `OE_reg` 到 be/data 输出引脚的路径放宽为 2 周期；`OE` 只在收发切换时翻转，是慢速控制路径）。`OE` 寄存器定义见 [pcileech_ft601.sv:L56](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_ft601.sv#L56)（中文说明：`OE` 是带 `KEEP` 属性的输出使能位，初值 1，控制 `FT601_BE`/`FT601_DATA` 的三态驱动）。

#### 4.3.4 代码实践

**实践目标**：体会 IOB 打包对时序的实际作用。

**操作步骤**：

1. 在 `pcileech_ft601.sv` 中找到 `OE`、`FT601_OE_N`、`FT601_DATA_OUT` 的定义，确认它们就是约束里 `_reg` 后缀对应的单元。
2. （若本地有 Vivado）先注释掉四行 `IOB TRUE`，跑一次实现，记录 FT601 输出路径的 WNS；再恢复，对比 WNS 变化。

**需要观察的现象**：去掉 IOB 打包后，输出寄存器会被放在 FPGA 内部的普通slice 里，到引脚的走线更长、更不确定，WNS 通常变差。

**预期结果**：恢复 IOB 打包后输出路径时序明显改善。若无法本地构建，则标注「待本地验证」——这是源码阅读型实践的重点：理解「IOB TRUE 把寄存器塞进引脚旁的 IOB」这一动作如何改善输出时序。

#### 4.3.5 小练习与答案

**练习 1**：`IOB TRUE` 作用在 `get_cells`，而 `PACKAGE_PIN` 作用在 `get_ports`，为什么不同？
**答案**：引脚位置是端口的物理属性，故用 `get_ports`；IOB 打包是「把某个寄存器单元放进 IOB」的逻辑布局属性，作用对象是单元，故用 `get_cells`。两者作用于设计层次的不同层级。

**练习 2**：为什么 `OE` 到数据引脚的路径能放心设为 multicycle=2？
**答案**：`OE` 只在状态机 RX↔TX 切换时翻转，频率远低于数据时钟；数据真正被外部采样发生在 `OE` 已稳定置起之后，给了额外一个周期的余量，因此放宽到 2 周期不会丢数据。

---

### 4.4 时序例外：false_path 对慢速/异步路径的放过

#### 4.4.1 概念说明

时序分析器默认对**所有**同步路径做「单周期」建立/保持检查。但工程里有些路径根本不需要卡这个标准：

- **纯异步/统计路径**：如自由计数器 `tickcount64`，它只是用来产生上电延时和 LED 慢闪，输出端到下游的时序完全不重要。
- **主机命令驱动的慢速控制路径**：如 `_pcie_core_config` 寄存器，由主机经 USB 写入，变化极慢，下游任何被它控制的逻辑都有无穷余量。
- **跨时钟域的状态信号**：如 PCIe 硬核的 `user_lnk_up_int`、`user_reset_out`，它们从 `clk_pcie` 域出来被 `clk` 域采样，是异步关系（u5-l1 讲过），单周期检查毫无意义。

对这类路径，正确做法是 `set_false_path` 明确放过，避免分析器报一堆假违例、淹没真正的时序问题。

> 说明：现代更规范的做法是对跨时钟域用 `set_clock_groups -asynchronous`。pcileech-fpga 在这些慢速状态点上采用了更直接的 `set_false_path`，效果是告诉工具「不要检查这些路径」。

#### 4.4.2 核心流程

```tcl
# 自由计数器输出放过
set_false_path -from [get_pins {tickcount64_reg[*]/C}]
# PCIe 核心配置寄存器（主机慢速写）放过
set_false_path -from [get_pins {i_pcileech_fifo/_pcie_core_config_reg[*]/C}]
# 跨域链路状态：lnk_up -> 命令发送寄存器
set_false_path -from [get_pins .../user_lnk_up_int_reg/C] -to [get_pins {i_pcileech_fifo/_cmd_tx_din_reg[16]/D}]
# 跨域复位：user_reset_out 放过
set_false_path -from [get_pins .../user_reset_out_reg/C]
```

#### 4.4.3 源码精读

四条 false_path 集中在一起：

```tcl
set_false_path -from [get_pins {tickcount64_reg[*]/C}]
set_false_path -from [get_pins {i_pcileech_fifo/_pcie_core_config_reg[*]/C}]
set_false_path -from [get_pins i_pcileech_pcie_a7/i_pcie_7x_0/inst/inst/user_lnk_up_int_reg/C] -to [get_pins {i_pcileech_fifo/_cmd_tx_din_reg[16]/D}]
set_false_path -from [get_pins i_pcileech_pcie_a7/i_pcie_7x_0/inst/inst/user_reset_out_reg/C]
```

见 [pcileech_squirrel.xdc:L87-L90](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel.xdc#L87-L90)（中文说明：放过四类不必单周期检查的路径——顶层自由计数器、fifo 的 PCIe 核心配置寄存器、PCIe 硬核链路就绪状态到 fifo 命令发送寄存器、PCIe 硬核用户复位输出）。

逐条对应：

- `tickcount64_reg` 对应顶层 [pcileech_squirrel_top.sv:L79](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L79) 的 `time tickcount64 = 0;`——一个 64 位自由计数器，只用于上电复位延时（`tickcount64 < 64`）和 LED 慢闪，其扇出巨大且对时序无要求。
- `_pcie_core_config_reg` 对应 u2-l5 讲过的 `rw[207:128]` 整段搬运结果（主机经 USB 慢速写入，变化以毫秒计）。
- `user_lnk_up_int_reg`/`user_reset_out_reg` 是 PCIe 硬核 `pcie_7x_0` 内部的状态/复位寄存器（路径里的 `inst/inst/...` 是 Xilinx IP 的标准内部层次），它们从 `clk_pcie` 域出来，属于跨域慢速状态。

#### 4.4.4 代码实践

**实践目标**：理解 false_path 是「放过假违例」，而非「修复真问题」。

**操作步骤**：

1. 在顶层找到 `tickcount64` 的所有使用点（复位判断 `tickcount64 < 64`、LED 慢闪 `tickcount64[24]`、长按检测 `tickcount64_reload > 500000000`）。
2. 思考：如果把 `tickcount64` 的 false_path 删掉，时序报告会怎样？

**需要观察的现象**：`tickcount64` 是 64 位计数器，高位翻转极少但扇出到多处慢速逻辑，单周期检查会产生大量「假」违例。

**预期结果**：删掉该 false_path 后，实现阶段时序报告里会冒出许多与 `tickcount64` 相关的 WNS 违例，但这些违例并不影响功能（因为下游本就不关心这些位的精确到达时间）。这正说明 false_path 的作用是「告诉工具别浪费时间在无意义路径上」。

#### 4.4.5 小练习与答案

**练习 1**：`set_false_path` 和 `set_multicycle_path` 有何区别？
**答案**：`set_false_path` 完全放弃对某路径的时序检查（用于异步或无关路径）；`set_multicycle_path N` 仍检查，但把允许的周期数从 1 放宽到 N（用于确实多周期才采样的同步路径）。前者是「不检查」，后者是「按更宽标准检查」。

**练习 2**：第三条 false_path 同时给了 `-from` 和 `-to`，而前两条只有 `-from`，为什么？
**答案**：前两条放过的是「从某个源寄存器出发的所有路径」（源端全放过）；第三条精确放过「链路就绪状态 → fifo 命令发送寄存器第 16 位」这一条特定跨域路径，只切断这一条，避免误放过其他不该放过的路径。

---

### 4.5 GTP 通道定位与 PCIe 物理约束

#### 4.5.1 概念说明

PCIe 是高速串行差分协议，FPGA 端靠专用的**千兆位收发器**（Artix-7 上叫 GTP，即 `GTPE2_CHANNEL`）收发。这些收发器不是通用逻辑，而是芯片上固定位置的特殊硬单元——每个 GTP 通道有固定的 `X/Y` 坐标。因此 PCIe 的差分对引脚（`pcie_tx_p/n`、`pcie_rx_p/n`、`pcie_clk_p/n`）**不是随便哪根引脚都能用**，它们必须连到某个 GTP 通道的专用收发脚上，这由板卡 PCB 走线决定。

约束上要做两件事：

- 用 **`LOC`** 把 PCIe IP 内部的 `GTPE2_CHANNEL` 实例钉到正确的 `X0Yn` 坐标（即告诉工具用哪个 GTP 通道）。
- 用 `PACKAGE_PIN` 把 `pcie_rx_p/n`、`pcie_tx_p/n`、`pcie_clk_p/n` 绑到板卡原理图指定的差分对焊球。注意差分对的 P/N 两根是成对出现的，且**正端引脚位号**通常即代表这对差分对。

另外，PCIe 参考时钟 `pcie_clk_p/n` 也需要 `create_clock` 声明（在网线 `pcie_clk_p` 上）。

#### 4.5.2 核心流程

```tcl
# 1) 把 PCIe IP 内部的 GTP 通道钉到固定坐标
set_property LOC GTPE2_CHANNEL_X0Y2 [get_cells {.../gtpe2_channel_i}]
# 2) PCIe 辅助信号（在位/复位/唤醒）走普通 LVCMOS33 引脚
set_property PACKAGE_PIN A13 [get_ports pcie_present]
set_property PACKAGE_PIN B13 [get_ports pcie_perst_n]
# 3) 差分收发对绑定到 GTP 专用脚
set_property PACKAGE_PIN A10 [get_ports {pcie_rx_n[0]}]
set_property PACKAGE_PIN B10 [get_ports {pcie_rx_p[0]}]
set_property PACKAGE_PIN A6  [get_ports {pcie_tx_n[0]}]
set_property PACKAGE_PIN B6  [get_ports {pcie_tx_p[0]}]
# 4) 参考时钟差分对
set_property PACKAGE_PIN E6 [get_ports pcie_clk_n]
set_property PACKAGE_PIN F6 [get_ports pcie_clk_p]
create_clock -name pcie_sys_clk_p -period 10.0 [get_nets pcie_clk_p]
```

#### 4.5.3 源码精读

GTP 通道定位（这是本讲最「长」的一行约束）：

```tcl
set_property LOC GTPE2_CHANNEL_X0Y2 [get_cells {i_pcileech_pcie_a7/i_pcie_7x_0/inst/inst/gt_top_i/pipe_wrapper_i/pipe_lane[0].gt_wrapper_i/gtp_channel.gtpe2_channel_i}]
```

见 [pcileech_squirrel.xdc:L93](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel.xdc#L93)（中文说明：把 PCIe IP 内部的 GTP 收发器通道实例钉到 `GTPE2_CHANNEL_X0Y2` 坐标——即 Squirrel 板卡上那对连着金手指 PCIe TX/RX 差分对的专用收发器）。这条层次路径 `i_pcileech_pcie_a7/i_pcie_7x_0/inst/inst/gt_top_i/pipe_wrapper_i/pipe_lane[0].gt_wrapper_i/gtp_channel.gtpe2_channel_i` 是 Xilinx 7 系列 PCIe IP 内部固定的收发器实例层次，`pipe_lane[0]` 表示第 0 条 lane（x1 工程只有一条）。

PCIe 辅助信号与差分对绑定：

```tcl
#PCIe signals
set_property PACKAGE_PIN A13 [get_ports pcie_present]
set_property PACKAGE_PIN B13 [get_ports pcie_perst_n]
set_property PACKAGE_PIN A14 [get_ports pcie_wake_n]
set_property IOSTANDARD LVCMOS33 [get_ports pcie_present]
set_property IOSTANDARD LVCMOS33 [get_ports pcie_perst_n]
set_property IOSTANDARD LVCMOS33 [get_ports pcie_wake_n]

set_property PACKAGE_PIN A10 [get_ports {pcie_rx_n[0]}]
set_property PACKAGE_PIN B10 [get_ports {pcie_rx_p[0]}]
set_property PACKAGE_PIN A6  [get_ports {pcie_tx_n[0]}]
set_property PACKAGE_PIN B6  [get_ports {pcie_tx_p[0]}]

set_property PACKAGE_PIN E6  [get_ports pcie_clk_n]
set_property PACKAGE_PIN F6  [get_ports pcie_clk_p]

create_clock -name pcie_sys_clk_p -period 10.0 [get_nets pcie_clk_p]
```

见 [pcileech_squirrel.xdc:L92-L109](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel.xdc#L92-L109)（中文说明：`pcie_present`/`pcie_perst_n`/`pcie_wake_n` 是金手指的在位/复位/唤醒普通单端信号，绑 LVCMOS33；收发差分对 `pcie_rx_p/n[0]`、`pcie_tx_p/n[0]` 与参考时钟 `pcie_clk_p/n` 绑到 GTP 专用焊球；并在 `pcie_clk_p` 网线上声明 100MHz 参考时钟 `pcie_sys_clk_p`）。

**硬件对应关系**：在标准 PCIe 金手指上，这些差分对和辅助信号都由插槽引脚定义固定。`pcie_perst_n`（PERST#）是主机给设备的复位信号；`pcie_present`（PRSNT#）是「在位检测」，主机靠它知道卡已插好；`pcie_wake_n`（WAKE#）用于节能唤醒。`pcie_clk_p/n` 是主机下发的 100MHz 参考时钟，经 u3-l1 讲的 `IBUFDS_GTE2` 进入 FPGA 喂给 GTP。`LOC GTPE2_CHANNEL_X0Y2` 之所以固定，是因为只有那个 GTP 通道的收发脚在 PCB 上连到了金手指的差分对——换个 `LOC` 值（如 `X0Y1`）就和板卡走线对不上，链路起不来。

#### 4.5.4 代码实践（本讲主线实践）

**实践目标**：把 PCIe 物理约束与板卡硬件走线一一对应起来。

**操作步骤**：

1. 在 `pcileech_squirrel.xdc` 中找到三类 PCIe 约束：
   - 收发差分对：`pcie_rx_p/n[0]`、`pcie_tx_p/n[0]`（[L101-L104](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel.xdc#L101-L104)）。
   - 参考时钟：`pcie_clk_p/n`（[L106-L107](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel.xdc#L106-L107)）。
   - GTP 定位：`LOC GTPE2_CHANNEL_X0Y2`（[L93](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel.xdc#L93)）。
2. 画一张对应表，把每个约束映射到 PCIe 金手指上的物理意义。
3. 思考：为什么 `pcie_clk_p/n` 要单独 `create_clock`，而 GTP 收发数据线却不需要为每根写 delay？

**需要观察的现象**：

| 约束 | 引脚/坐标 | 金手指上的含义 |
| --- | --- | --- |
| `pcie_tx_p[0]`/`pcie_tx_n[0]` | B6 / A6 | 设备→主机 发送差分对（PETp0/PETn0） |
| `pcie_rx_p[0]`/`pcie_rx_n[0]` | B10 / A10 | 主机→设备 接收差分对（PERp0/PERn0） |
| `pcie_clk_p`/`pcie_clk_n` | F6 / E6 | 主机下发的 100MHz 参考时钟差分对（REFCLK+/−） |
| `LOC GTPE2_CHANNEL_X0Y2` | X0Y2 | 处理上述差分对的专用 GTP 收发器通道 |
| `pcie_perst_n` | B13 | 主机 PERST# 复位 |
| `pcie_present` | A13 | 在位检测 PRSNT# |
| `pcie_wake_n` | A14 | WAKE# 唤醒 |

**预期结果**：你会得出结论——`pcie_tx/rx/clk_p/n` 四对差分线的引脚位号必须落在 `GTPE2_CHANNEL_X0Y2` 这个 GTP 通道的专用收发脚上（A6/B6/A10/B10/E6/F6 这组焊球在 XC7A35T-484 的 Bank 上正是该 GTP 通道的引脚）。参考时钟必须 `create_clock`，是因为它要作为时序分析的源时钟；而 GTP 内部高速串行数据（5Gbps/2.5Gbps）由硬核自己处理时钟恢复，不参与普通静态时序分析，故无需逐根写 delay。

**待本地验证**：若你手头有 Squirrel 板卡原理图或 XC7A35T-484 的封装引脚表，可核对 `GTPE2_CHANNEL_X0Y2` 通道对应的收发脚是否确为 A6/B6/A10/B10。

#### 4.5.5 小练习与答案

**练习 1**：为什么 PCIe 差分对用 `LOC` 锁 GTP 通道，而 FT601 用 `PACKAGE_PIN` 锁普通引脚？
**答案**：FT601 是普通并行 LVCMOS 信号，走 FPGA 通用 IOB，用 `PACKAGE_PIN` 即可；PCIe 是高速串行差分，必须用专用 GTP 收发器硬核，GTP 在芯片上位置固定，用 `LOC GTPE2_CHANNEL_X0Yn` 指定用哪个通道。

**练习 2**：如果把 `LOC` 改成 `GTPE2_CHANNEL_X0Y1`（假设存在），会怎样？
**答案**：工具会把 PCIe 收发器放到 X0Y1 通道，但板卡 PCB 上连到金手指差分对的是 X0Y2 通道的引脚——收发器与物理引脚错位，PCIe 链路物理上无法连通，设备不会被主机枚举到。`LOC` 值必须与板卡硬件走线严格一致。

---

### 4.6 比特流属性与配置约束（CFGBVS / BITSTREAM.*）

#### 4.6.1 概念说明

XDC 末尾还有一组作用于 `current_design`（整个设计）的属性，它们不针对某个引脚或路径，而是控制**生成的比特流**怎么被烧录、FPGA 上电后怎么配置自身：

- **`CFGBVS` / `CONFIG_VOLTAGE`**：告诉 FPGA 配置逻辑「我的 VCCBVS 供电是多少」，决定配置时电平检测的参考。`CFGBVS Vcco` + `CONFIG_VOLTAGE 3.3` 表示配置供电接 3.3V。
- **`BITSTREAM.CONFIG.SPI_BUSWIDTH`**：SPI 烧录总线宽度（4 表示 4 线 QSPI）。
- **`BITSTREAM.GENERAL.COMPRESS`**：比特流压缩，省 Flash 空间、加快加载。
- **`BITSTREAM.CONFIG.SPI_FALL_EDGE`**：SPI 采样沿选择。
- **`BITSTREAM.CONFIG.CONFIGRATE`**：配置加载速率（66 MHz）。

这些大多在移植/换板时不需要改，除非新板卡的配置 Flash 或供电不同。

#### 4.6.2 源码精读

```tcl
set_property CFGBVS Vcco [current_design]
set_property CONFIG_VOLTAGE 3.3 [current_design]
set_property BITSTREAM.CONFIG.SPI_BUSWIDTH 4 [current_design]
set_property BITSTREAM.GENERAL.COMPRESS TRUE [current_design]
set_property BITSTREAM.CONFIG.SPI_FALL_EDGE YES [current_design]
set_property BITSTREAM.CONFIG.CONFIGRATE 66 [current_design]
```

见 [pcileech_squirrel.xdc:L111-L116](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel.xdc#L111-L116)（中文说明：整设计的配置属性——配置供电 3.3V、4 线 SPI 烧录、比特流压缩、SPI 下降沿采样、66MHz 加载速率）。它们与 u1-l3 讲的烧录流程（OpenOCD/CH347）相关：`SPI_BUSWIDTH=4` 与 `CONFIGRATE=66` 决定了 Squirrel 从板载 SPI Flash 自启动加载 bitstream 的速度。

## 5. 综合实践

**任务**：为「给 Squirrel 工程新增一个调试用 LED 输出端口」起草约束修改清单，把所学串起来。

背景：假设你想在顶层 `pcileech_squirrel_top.sv` 新增一个输出端口 `output debug_led`，用来观察某个内部信号。请回答：

1. **引脚**：从板卡上挑一个当前未使用的、连到 LED 或排针的 LVCMOS33 引脚（例如某根空闲焊球），写出它的 `PACKAGE_PIN` 与 `IOSTANDARD` 约束。
2. **时序**：这个 LED 由某慢速状态寄存器（类似 `tickcount64[24]`）驱动，是否需要写 `set_input_delay`/`set_output_delay`？是否建议加 `set_false_path`？为什么？
3. **IOB**：这个 LED 是否需要 `IOB TRUE`？为什么？
4. **不影响 PCIe**：确认你的改动不会触碰 `LOC GTPE2_CHANNEL_X0Y2` 与四对 PCIe 差分引脚。

**参考思路**：

1. 写两行即可：`set_property PACKAGE_PIN <空闲焊球> [get_ports debug_led]` 与 `set_property IOSTANDARD LVCMOS33 [get_ports debug_led]`。具体焊球号须查板卡原理图上「未使用」的引脚，不能与 FT601/PCIe/时钟冲突。
2. LED 是慢速人眼可见信号，无外部采样关系，**不需要** `set_input_delay`/`set_output_delay`（那是对接外部器件并行总线才用的）。若驱动它的是 `tickcount64` 这类慢速源，建议加 `set_false_path -from <源寄存器>/C -to [get_ports debug_led]`，避免无意义的假违例。
3. **不需要** `IOB TRUE`。IOB 打包是为缩短高速输出路径延迟、改善时序余量；LED 慢速翻转，放普通 slice 即可，引脚到 LED 的走线延迟毫无影响。
4. PCIe 部分是独立的物理资源块（GTP 通道与其专用差分脚），新增一个普通 LVCMOS33 引脚不会占用它们；只要新挑的焊球不在 Bank 冲突即可。

完成这个任务后，你应当能独立地为任何新增端口写出「最小且正确」的约束。

## 6. 本讲小结

- **XDC 三类指令**：物理实现约束（`PACKAGE_PIN`/`IOSTANDARD`/`LOC`/`SLEW`/`IOB`）、时序约束（`create_clock`/`set_input_delay`/`set_output_delay`）、时序例外（`set_false_path`/`set_multicycle_path`）。
- **引脚分配**：FT601 的 32 位数据 + 控制线逐根 `PACKAGE_PIN` 绑定到 Squirrel 板卡焊球，统一 `LVCMOS33`、输出 `SLEW FAST`，位号由 PCB 走线决定。
- **FT601 时序三招**：`set_input_delay`/`set_output_delay` 描述与 FT601 的采样关系；`IOB TRUE` 把 `FT601_OE_N/RD_N/WR_N/DATA_OUT` 寄存器打包进引脚旁 IOB；`set_multicycle_path 2` 放过慢速的 `OE` 输出使能路径。
- **false_path 放过四类路径**：`tickcount64` 自由计数器、`_pcie_core_config` 主机慢速配置、PCIe 硬核 `user_lnk_up_int` 与 `user_reset_out` 跨域状态，避免假违例淹没真问题。
- **PCIe 物理约束**：`LOC GTPE2_CHANNEL_X0Y2` 锁定专用 GTP 通道，四对差分引脚（tx/rx/clk 的 P/N）绑到该通道专用焊球，`pcie_clk_p` 声明 100MHz 参考时钟——这些值与板卡硬件走线严格一一对应，移植时不可乱改。
- **比特流属性**：`CFGBVS`/`CONFIG_VOLTAGE`/`SPI_BUSWIDTH`/`CONFIGRATE` 等控制上电自加载方式，与烧录流程关联。

## 7. 下一步学习建议

- **继续 u5 单元**：下一篇 u5-l3「Xilinx IP 核与工程生成脚本」会讲解 `ip/` 目录下的 `.xci`/`.coe` 如何被 `vivado_generate_project.tcl` 组织成可综合工程，与本讲引脚约束在同一个脚本里被一并导入。
- **回看 u5-l1**：若对 `set_false_path` 背后的「跨时钟域」直觉还不够扎实，可重读 u5-l1 关于三大时钟域与双时钟 FIFO 的部分，把「物理时钟来源」（本讲的 `create_clock`）与「跨域处理」（双时钟 FIFO + false_path）对上。
- **横向对比移植**：阅读 `CaptainDMA/35T325_x1/src/*.xdc` 或 `ZDMA/100T/src/*.xdc` 等其他设备的约束文件，对比 GTP 通道 `LOC` 值与引脚位号的差异——这是 u6-l2「移植到新板卡」的关键素材。**待确认**：具体设备目录下的 xdc 文件名与是否存在，可在学习时用 `Glob` 查 `**/src/*.xdc` 核实。

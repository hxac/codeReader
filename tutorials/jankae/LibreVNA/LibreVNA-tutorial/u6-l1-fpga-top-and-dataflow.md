# FPGA 顶层设计与数据流总览

## 1. 本讲目标

本讲是 FPGA 单元（单元 6）的第一讲，也是整个 FPGA 子树的「导览图」。学完后你应该能够：

1. 说出 `top.vhd` 实例化了哪些主要功能块（PLL 时钟、Synchronizer、Sweep、MCP33131 ADC 接口、Windowing、Sampling、DFT、MAX2871、SPICommands、SweepConfigMem），以及它们之间的互连关系。
2. 对照 `FPGA_protocol.tex` 协议文档解释 MCU 与 FPGA 之间的命令集：16 位命令字、寄存器写、扫描配置写、测量结果读，并能在 `SPIConfig.vhd` 中找到每条命令的解码代码。
3. 描述 800 kHz 级中频采样在 FPGA 内的处理流水线概貌：三路 ADC → 加窗 → 单 bin 正交解调（VNA 路径）/ 96 bin DFT（频谱仪路径）→ 结果寄存器 → SPI 上报 MCU。
4. 读懂一份 ISE 工程文件（`VNA.xise`）和引脚约束文件（`top.ucf`），知道 bitstream 是从哪些源码综合出来的（承接 u1-l4）。

本讲只做「顶层走读」，不深入任何单个模块的内部实现——Sweep 状态机、ADC 时序、窗函数与 DFT 核分别留给本单元后续讲义。

## 2. 前置知识

### 2.1 VHDL 的最小语法包

LibreVNA 的 FPGA 逻辑用 VHDL 编写。读懂本讲只需要以下几个概念：

- **entity（实体）**：一个模块的「对外接口」，类似 C++ 的函数签名。例如 `entity top is Port(...)` 列出顶层芯片的所有引脚。
- **architecture（结构体）**：模块的内部实现，类似函数体。
- **component + port map（元件与例化）**：在结构体里声明要使用的子模块（component），然后用 `port map` 把子模块端口接到内部信号上——这就是「原理图连线」的文本形式。`top.vhd` 的结构体几乎就是一张用文字写的原理图。
- **signal（信号）**：模块内部的连线。VHDL 的 `<=` 是信号赋值，可以理解为「把这根线接到那个输出上」，右侧的与/非运算就是路上的门电路。
- **generic（类属参数）**：例化时传入的编译期常量，类似模板参数。例如 `GENERIC MAP(CLK_DIV => 2)`。
- **process（进程）**：时序逻辑的载体，`if rising_edge(CLK) then` 表示「每个时钟上升沿做一次」，是寄存器的写法。

### 2.2 时钟域与同步器

FPGA 内部所有模块都跑在同一个 102.4 MHz 主时钟 `clk_pll` 上（后面会看到来源）。但外部输入信号（MCU 的 AUX 线、PLL 的锁定指示 LD、触发线）是异步的——它们随时可能变化，如果直接送进时序逻辑，会违反建立/保持时间，产生**亚稳态**（metastability，即一个触发器输出长时间停在非法电平）。标准解法是让信号先过 2 级触发器（称为**同步器**，Synchronizer），本讲会在 `top.vhd` 里看到 8 个这样的实例。详细原理在 u6-l2 展开。

### 2.3 回顾：这块 FPGA 在系统中的位置

复习 u1-l1 与 u5-l4 的结论：

- 射频链路把信号两次下变频到 250 kHz 中频，三路（端口 1、端口 2、参考）各由一颗 16 位 ADC 以约 914 kHz 采样（采样率由 FPGA 分频产生，本讲会算）。
- VNA 扫描采用「MCU 预编程 + FPGA 自主扫描」：MCU 在开始前把每个扫描点的 PLL 配置写入 FPGA 内的配置存储器，之后 FPGA 自己逐点推进频率、等待稳定、启动采样，MCU 只在收到「新数据」中断后经 SPI 把结果读走。
- FPGA bitstream 存在板载 Flash，上电由 MCU 灌入（u1-l4 的 `AssembleFirmware.py` 与 `FPGA::Configure`）。

所以本讲要回答的三个问题正好是：**FPGA 内部有哪些块**（4.1）、**MCU 怎么指挥它**（4.2）、**数据怎么流**（4.3）。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [FPGA/VNA/top.vhd](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd) | 顶层设计：芯片引脚定义 + 全部功能块的例化与互连，本讲主教材 |
| [FPGA/VNA/SPIConfig.vhd](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/SPIConfig.vhd) | `SPICommands` 实体：SPI 从机协议解码、寄存器堆、命令分发，MCU 命令入口 |
| [FPGA/VNA/spi_slave.vhd](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/spi_slave.vhd) | 通用 SPI 从机底层（位收发、半字预解码），被 SPIConfig 例化，细节在 u6-l5 |
| [Documentation/DeveloperInfo/FPGA_protocol.tex](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/FPGA_protocol.tex) | MCU↔FPGA 协议的成文文档（引脚表、命令、寄存器表、SweepConfig 与结果格式） |
| [FPGA/VNA/VNA.xise](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/VNA.xise) | Xilinx ISE 工程文件：源码清单、器件型号、顶层设定 |
| [FPGA/VNA/top.ucf](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.ucf) | 引脚位置与时序约束（本讲用来看时钟周期） |
| [FPGA/VNA/ipcore_dir/PLL.xco](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/ipcore_dir/PLL.xco) | 时钟管理 IP 的配置（102.4 MHz 输出） |
| [Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp) | 固件侧 FPGA 驱动：命令的另一端，用来交叉验证协议 |

另外目录里还有 `Hann.dat` / `Kaiser.dat` / `Flattop.dat` 三个窗系数文件（u6-l4 的素材），以及 `Test_*.vhd` 一族 testbench（u6-l6 的素材）。本讲只引用、不展开。

## 4. 核心概念与源码讲解

### 4.1 top 实例化结构

#### 4.1.1 概念说明

FPGA 与 MCU 程序最大的思维差异是：**FPGA 的「程序」是一张电路图**。`top.vhd` 就是这张图的总图——它定义 Spartan-6 芯片对外的全部引脚，然后在内部例化 12 类功能块并用信号把它们连起来。ISE 综合工具把 `top.vhd` 及其引用的所有模块编译成 bitstream（u1-l4 讲过如何烧写）。

看懂顶层就等于拿到了整颗 FPGA 的「零件清单 + 接线表」。这也解释了为什么 `VNA.xise` 里把 `top.vhd` 设为 Implementation Top File（[VNA.xise:282](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/VNA.xise#L282)）：综合是从顶层出发、递归引用 component 的过程。

器件与工程信息：

- 器件为 **Spartan-6 xc6slx9、封装 tqg144、速度等级 -2**（[VNA.xise:215-218](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/VNA.xise#L215-L218)），约 9000 个逻辑单元的中等规模器件。
- 工程文件里登记了全部 `.vhd` 源文件与 testbench（[VNA.xise:18-143](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/VNA.xise#L18-L143)），这与 GUI 侧 `.pro` 文件「活的文件索引」角色一致（u1-l3 的结论在这里同样适用：没登记的文件不参与综合）。
- 输入时钟约束为周期 62.5 ns，即板载 **16 MHz** 晶振（[top.ucf:3](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.ucf#L3)）。

#### 4.1.2 核心流程

`top.vhd` 的结构分三段，阅读顺序建议如下：

```text
① entity top 的端口列表        → 芯片引脚分组（MCU 侧 / 三路 ADC / 两颗 PLL / 射频开关 / LED）
② architecture 的 component 声明 → 零件清单（12 类模块的接口签名）
③ architecture begin 后的例化    → 接线：每个模块一份 port map + 少量输出多路选择器
```

顶层引脚按「对话对象」分成六组：

| 引脚组 | 方向 | 用途 |
|---|---|---|
| `MCU_MOSI/MISO/MCU_SCK/MCU_NSS/MCU_INTR` + `MCU_AUX1..3` | 与 MCU | SPI 命令通道 + 总线切换选择线 + 中断 |
| `PORT1/PORT2/REF_*`（CONVSTART/SDO/SCLK） | 与三颗 ADC | 每路一根转换启动、一根数据、一根时钟 |
| `SOURCE_*` / `LO1_*`（CLK/MOSI/LE/CE/RF_EN/LD/MUX） | 与两颗 MAX2871 | FPGA 直驱 PLL 的寄存器写总线 |
| `PORT1/2_SELECT`、`PORT_SELECT1/2`、`BAND_SELECT_*`、`ATTENUATION`、`FILT_*`、`*MIX*_EN`、`AMP_PWDN` | 到射频开关/衰减器/混频器 | 激励路由与电平控制 |
| `TRIGGER_IN/OUT` | 与外部 | 多机同步（u5-l4 讲过用途） |
| `CLK`、`RESET`、`LEDS` | 其他 | 16 MHz 输入时钟、复位、8 个 LED |

#### 4.1.3 源码精读

**(a) 顶层引脚定义。** [top.vhd:32-87](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L32-L87) 是 entity `top` 的完整端口表。注意几个位宽：`ATTENUATION` 是 7 位（0.25 dB 步进的衰减器，u5-l2 讲过），`LEDS` 8 位，三路 ADC 的 `SDO` 是串行输入、`SCLK` 是 `inout`（时钟由 FPGA 驱动、但按双向引脚约束）。

**(b) 零件清单。** [top.vhd:90-330](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L90-L330) 集中声明了所有 component，从接口就能猜到各自职责：

| Component | 声明位置（行号） | 职责（据端口推断） |
|---|---|---|
| `PLL` | 90-100 | 时钟管理 IP：16 MHz → 102.4 MHz |
| `ResetDelay` | 102-109 | 复位延迟，等时钟锁定后再放行复位 |
| `Sweep` | 111-159 | 扫描状态机：逐点读配置、设 PLL、触发采样 |
| `Windowing` | 161-176 | 对三路 ADC 原始样本乘窗系数，16 位 → 18 位 |
| `Sampling` | 178-202 | 正交解调与累加，输出每通道 48 位 I/Q |
| `MCP33131` | 203-219 | ADC 接口时序 + 样本最小/最大值统计 |
| `MAX2871` | 220-235 | FPGA 侧的 PLL 寄存器写入时序发生器 |
| `SPICommands` | 236-291 | MCU 命令入口：寄存器堆 + 结果读出 |
| `DFT` | 293-308 | 96 bin DFT（频谱仪加速用） |
| `SweepConfigMem` | 310-321 | 双端口 RAM：每个扫描点 96 位配置 |
| `Synchronizer` | 323-330 | 2 级触发器同步器 |

**(c) 时钟与复位树。** [top.vhd:485-494](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L485-L494) 例化时钟 IP：外部 `CLK` 进、`clk_pll` 出、`LOCKED` 指示锁定。IP 配置文件写明输出频率请求值为 102.4 MHz（[PLL.xco:73](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/ipcore_dir/PLL.xco#L73)）。16 MHz × 6.4 = 102.4 MHz，选这个「难看」的频率是为了让 ADC 采样率、中频 250 kHz 与相位增量都是整数关系（4.3 节计算）。随后 [top.vhd:498-504](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L498-L504) 用 `ResetDelay` 把「时钟未锁定」再延展 100 个 `clk_pll` 周期，生成内部复位 `int_reset`——避免在时钟稳定前释放寄存器。

**(d) 异步输入全部过同步器。** [top.vhd:506-561](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L506-L561) 连续例化 8 个 `Synchronizer`（`stages => 2`）：AUX1/2/3、两颗 PLL 的锁定检测 LD、片选 NSS、触发输入、触发输出。这是 2.2 节亚稳态问题的标准答案。注意 `NSS` 单独同步后得到 `nss_sync`，供后面 SPI 总线复用判断使用。

**(e) FPGA 直驱两颗 PLL。** [top.vhd:564-595](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L564-L595) 例化两份 `MAX2871` 组件（`Source` 与 `LO1`，均 `CLK_DIV => 6`）。它们不产生频率，只负责把 Sweep 给出的 4 个 32 位寄存器值按 datasheet 时序打到芯片的 CLK/MOSI/LE 引脚上；两颗都写完（`plls_reloaded`，L594）且两颗都报锁定（`plls_locked`，L595）才通知 Sweep 继续。**这就是「FPGA 自主扫描」的物质基础：扫频时 MCU 完全不在环里。**

**(f) 三路 ADC 接口。** [top.vhd:597-644](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L597-L644) 例化三份 `MCP33131`（`CLK_DIV => 2, CONVCYCLES => 77`）。三个关键细节：

- 三路共用同一个 `adc_trigger_sample` 启动信号——**同步采样**是 S 参数比值测量的前提（u3-l2 讲过 S 参数 = 接收/参考）。
- 只有端口 1 的 `READY` 被使用（L620、L636 注释写明 "synchronous ADCs, ready indicated by port 1 ADC"），三颗芯片同型号同时钟，以第一颗的就绪代表全部。
- 每颗 ADC 的样本最小/最大值被拼进 96 位 `adc_minmax` 信号（各占 16+16 位），供 MCU 查询饱和情况（对应协议文档的 ADC limits 命令，见 4.2）。

**(g) 输出多路选择器：正常扫描 vs 硬件覆盖。** [top.vhd:459-483](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L459-L483) 与 [top.vhd:738-744](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L738-L744) 是顶层少有的「逻辑」：端口开关、波段选择、源滤波器、衰减器这些射频控制线，平时由 Sweep 状态机驱动，但当 `HW_overwrite_enabled = '1'` 时改为由 SPI 写入的 15 位覆盖寄存器直接驱动。这正是 u5-l2/u5-l4 提到的「信号源模式靠 OverwriteHardware 长期钉住射频控制线」的电路实现。

**(h) 共享 SPI 总线的复用。** [top.vhd:746-761](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L746-L761) 实现了 u5-l2 讲过的「一条 SPI 在 FPGA 与两颗 PLL 间分时路由」：`AUX1=AUX2=0` 时 MCU 的 SPI 直达 FPGA 内部的 `SPICommands`（`fpga_select`，L748）；`AUX1=1` 时 SCK/MOSI/NSS 直通源 PLL（L750-752）；`AUX2=1` 时直通 LO1（L754-756）；MISO 则按同一规则选择回送源（L758-761）。这段代码与协议文档的引脚表（4.2.3 引用）逐行对应。

**(i) 其余例化**（数据通路主角，4.3 节精读）：Windowing（[top.vhd:647-660](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L647-L660)）、Sampling（[top.vhd:662-685](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L662-L685)）、Sweep（[top.vhd:689-735](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L689-L735)）、SPICommands（[top.vhd:768-821](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L768-L821)）、DFT（[top.vhd:825-838](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L825-L838)）、SweepConfigMem（[top.vhd:840-850](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L840-L850)）。

#### 4.1.4 代码实践（本讲主实践：手绘 top.vhd 模块级框图）

1. **实践目标**：不看任何现成图，仅凭 `top.vhd` 画出 FPGA 内部模块级框图，标注 ADC 输入、SPI 从机、各功能块、MCU 接口，以及每根连线的方向与位宽。
2. **操作步骤**：
   - 打开 [top.vhd:32-87](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L32-L87)，先在纸上画出芯片边界，把引脚按 4.1.2 的六组标在边界上（注意方向 in/out/inout）。
   - 从 [top.vhd:456](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L456)（`begin`）起逐个例化画框，画一个框就把它端口表里出现的信号名抄到连线上。数据通路建议按「左入右出」排：ADC 引脚 → MCP33131 → Windowing → Sampling/DFT → SPICommands → MCU_MISO。
   - 位宽直接抄信号声明，例如 `adc_port1_data` 是 16 位、`port1_windowed` 是 18 位、`sampling_result` 是 304 位（[top.vhd:356-379](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L356-L379)）。
   - 单独用虚线画出「控制流」：`Sweep` 输出的 `START_SAMPLING`/`RELOAD_PLL_REGS` 与回流的 `PLL_LOCKED`/`SAMPLING_DONE`。
3. **需要观察的现象**：画完后自检三个问题——① 三路 ADC 数据在哪一点汇成一条 304 位结果？② Sweep 的配置从哪来、写到哪去？③ MCU_MISO 的数据源有几种可能？
4. **预期结果**：得到一张与 4.3.1 文字流程一致的框图；三个自检问题的答案分别在 `Sampling` 例化的 `sampling_result` 拼接（L678-683）、`SweepConfigMem` 双端口（L840-850）、SPI 复用多路器（L758-761）。本实践纯源码阅读，无需硬件与 ISE，无「待本地验证」项。

#### 4.1.5 小练习与答案

**练习 1**：顶层有 8 个 `Synchronizer` 实例，但 `MCU_SCK` 和 `MCU_MOSI` 却没有过同步器，为什么？

**答案**：`MCU_SCK/MOSI` 是 SPI 时钟与数据，它们由 `spi_slave`（在 SPIConfig 内部例化）按 SPI 自己的时钟沿采样，属于独立的 SPI 时钟域处理；而 AUX、LD、NSS、Trigger 这类「电平型」异步信号才需要先同步到 `clk_pll` 域。`NSS` 例外地既被同步（供总线复用判断）又进 SPI 从机。

**练习 2**：`PORT1_MIX1_EN <= not port1mix_en;`（[top.vhd:472](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L472)）为什么一级/二级混频使能总是相反？

**答案**：两根使能线互锁，保证同一时刻只有一级混频器偏置开启，避免两级同时导通引入泄漏与功耗；一个寄存器位（`PORT1_EN`）经反相器生成互补的两路控制，是「一个配置位 + 硬件互斥」的常见做法。

**练习 3**：如果想确认综合用的完整文件清单，除了读 `VNA.xise` 还能怎么办？

**答案**：从顶层出发递归追 component：`top.vhd` 声明的 12 类 component 各对应一个同名 `.vhd` 文件，其中 `PLL`、`SweepConfigMem` 等在 `ipcore_dir/` 下还有配套 `.xco` IP 配置；`SPICommands` 实体在 `SPIConfig.vhd` 里（文件名与实体名不一致，靠这条递归法才不会漏）。

### 4.2 MCU 命令入口

#### 4.2.1 概念说明

MCU 指挥 FPGA 的方式是「**SPI + 寄存器堆**」：所有可配置项（扫描点数、每点样本数、窗类型、ADC 分频、相位增量、中断屏蔽、PLL 默认寄存器……）都被编址为 16 位寄存器；MCU 写寄存器改变行为，读寄存器/结果口取回数据。这层协议由三个文件共同定义：

- 成文规范：`FPGA_protocol.tex`（寄存器表、命令编码、数据格式）；
- 解码实现：`SPIConfig.vhd`（`SPICommands` 实体，含底层 `spi_slave` 例化）；
- 发起方：固件 `FPGA.cpp`（`FPGA::WriteRegister` 等）。

三者必须严格一致——这个项目里没有 Protocol.hpp 那样的两端同源编译机制（那是 USB 协议的做法，u4-l1），FPGA 协议靠「文档 + 人工对齐」，所以本讲的实践重点就是交叉核对。

另外，命令/数据总线是**共享**的：同一组 SCK/MOSI/MISO/NSS 按 AUX1/AUX2 状态在「FPGA 自己 / 源 PLL / LO PLL」三者间切换（4.1.3 (h)），协议文档的引脚表对此有明确描述。

#### 4.2.2 核心流程

一次 SPI 事务 = NSS 拉低 → 若干 16 位字（MSB 在前）→ NSS 拉高。**第一个字永远是命令字**，且 MCU 在发命令字的同时会收到 FPGA 回送的**中断状态字**。命令字最高 3 位（bit15..13）决定事务类型：

| bit15..13 | 命令 | 后续字 | 协议文档出处 |
|---|---|---|---|
| `000` | 写扫描配置 | 13 位点号 + 6 个字（拼成 96 位 SweepConfig） | L132-143 |
| `001` | 恢复被暂停的扫描 | 无 | L160-170 |
| `011` | 复位 ADC 最小/最大统计 | 无 | L214-224 |
| `100` | 写寄存器 | 5 位寄存器地址在命令字 bit4..0，随后 1 个字为寄存器值 | L116-131 |
| `010` | （读同步字，回 `0xF0A5`） | 无实质后续 | 代码 |
| `101` | 读 DFT 结果 | 12 个字（一个 bin：端口 1/2 的 I/Q，各 48 位） | L226-288 |
| `110` | 读采样结果 | 最多 19 个字（304 位结果，最低字在前） | L145-158 |
| `111` | 读 ADC 最小/最大 | 6 个字 | L172-212 |

> 提示：文档里的位框图是 MSB 在左绘制的（左上角标着 15），对照代码时建议统一按「bit15..bit0」口头描述，不易出错。

中断状态字（命令字期间回送）的位分配：bit5=DFT 新结果、bit4=扫描暂停、bit3=数据覆盖、bit2=新数据、bit1=源失锁、bit0=LO 失锁。屏蔽寄存器（0x00）与之间按位与，非零则 `MCU_INTR` 拉高——固件侧据此进入中断服务（u5-l1 的 `EXTI` 链路）。

寄存器一览（写入端代码在 4.2.3 精读）：

| 地址 | 名称 | 作用 |
|---|---|---|
| 0x00 | Interrupt Mask | 中断屏蔽（bit5 DFTIE 同时使能 DFT 引擎） |
| 0x01 | Sweep Points | 每扫描点数 − 1 |
| 0x02 | Samples Per Point | 每点样本数，以 16 个样本为步进 |
| 0x03 | System Control | 各混频器/放大器/PLL 使能、窗类型、同步主机位等 |
| 0x04 | ADC Prescaler | ADC 采样率分频 |
| 0x05 | Phase Increment | 单 bin 解调的每样本相位步进 |
| 0x06 | Sweep Setup | 每 point 的 stage 编排与同步使能 |
| 0x07 | Hardware Overwrite | 硬件覆盖（信号源模式用） |
| 0x08-0x0F | MAX2871 Defaults | 两颗 PLL 的默认寄存器 0/1/3/4 |
| 0x12/0x13 | DFT First Bin / Spacing | 96 bin DFT 的起始频率与间距 |
| 0x14/0x15 | Settling Time Low/High | 加激励到开始采样的延时（20 位） |

#### 4.2.3 源码精读

**(a) 底层从机与状态机骨架。** [SPIConfig.vhd:135-149](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/SPIConfig.vhd#L135-L149) 例化通用 `spi_slave`（`W => 16` 每字 16 位，`PREWIDTH => 3` 预解码 3 位）。状态机只有 4 个状态：`FirstWord / WriteSweepConfig / ReadResult / WriteRegister`（[SPIConfig.vhd:117-118](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/SPIConfig.vhd#L117-L118)）。

**(b) 中断状态的组装与_INTR 生成。** [SPIConfig.vhd:191-196](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/SPIConfig.vhd#L191-L196) 每个时钟沿拼接状态位：`DEBUG_STATUS(10 downto 1)`（低 6 位另有来源）拼上 `DFT_RESULT_READY & SWEEP_HALTED & data_overrun & unread_sampling_data & SOURCE_UNLOCKED & LO_UNLOCKED`——与 4.2.2 的位分配逐位对应；只要 `interrupt_status and interrupt_mask` 非零就置 `INTERRUPT_ASSERTED`。数据覆盖检测在 [SPIConfig.vhd:205-210](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/SPIConfig.vhd#L205-L210)：新数据到来时若上一份还没被读走，置 `data_overrun`（协议文档注明该位只能靠复位 FPGA 清除）。

**(c) NSS 下降沿：装载状态字作为第一个回送字。** [SPIConfig.vhd:211-215](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/SPIConfig.vhd#L211-L215) 检测到 NSS 由高变低时回到 `FirstWord` 并把 `interrupt_status` 放进发送缓冲——这就是「发命令字的同时读到中断状态」的实现。

**(d) 读类命令：3 位预解码抢时间。** [SPIConfig.vhd:218-244](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/SPIConfig.vhd#L218-L244) 利用 `spi_slave` 的 3 位预完成信号，在命令字还没移完时就把要回送的数据装上：`"101"` 锁存 DFT 输出、`"110"` 锁存 304 位采样结果并清 `unread_sampling_data`、`"111"` 锁存 ADC 最小/最大、`"010"` 回送固定同步字 `1111000010100101`（0xF0A5，供 MCU 验证链路）。`ReadResult` 状态（L239-242）随后每字左移 16 位依次送出，**最低字在前**，与文档一致。

**(e) 写类命令：整字解码。** [SPIConfig.vhd:247-268](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/SPIConfig.vhd#L247-L268) 在 `spi_complete` 时按 `spi_buf_out(15 downto 13)` 分发：`"000"` 进 `WriteSweepConfig` 并把 bit12..0 存为扫描点地址；`"001"` 发出一次 `SWEEP_RESUME` 脉冲；`"011"` 发出 `RESET_MINMAX`；`"100"` 进 `WriteRegister` 并取 bit4..0 作寄存器地址。

**(f) 寄存器堆本体。** [SPIConfig.vhd:269-309](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/SPIConfig.vhd#L269-L309) 就是 4.2.2 寄存器表的代码形态，例如 `when 3 =>` 一个 case 同时拆出 `PORT1_EN <= spi_buf_out(15)`、`WINDOW_SETTING <= spi_buf_out(6 downto 5)`、`SYNC_MASTER <= spi_buf_out(1)` 等十几个位域（对照文档 System Control Register 0x03 的位图）。注意 L309 `selected_register <= selected_register + 1`——地址自动递增，连续写多个寄存器时每个字都算一次写。

**(g) 扫描配置写入：移位 6 次拼 96 位。** [SPIConfig.vhd:310-318](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/SPIConfig.vhd#L310-L318)：前 5 个字移入 80 位缓冲，第 6 个字到位后拼接成 96 位 `SWEEP_DATA` 并发出一拍 `SWEEP_WRITE` 写入 `SweepConfigMem`（4.1.3 (i) 的双端口 RAM 端口 A）。96 位的内容（PLL 分频/FRAC/VCO、衰减、滤波、每点样本档位）由 [FPGA_protocol.tex:558-656](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/FPGA_protocol.tex#L558-L656) 逐位定义，u6-l2 会用到。

**(h) 另一端的实证：固件怎么发。** [FPGA.cpp:28-36](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp#L28-L36) 的 `FPGA::WriteRegister` 只发 4 个字节：`{0x80, reg, value>>8, value&0xFF}`。展开成 16 位字即 `0x8000 | reg`——bit15..13 恰是 `100`（写寄存器命令），bit4..0 恰是寄存器地址，第二个字是数值。代码与文档与解码三方闭环。再看两个带换算的封装：

- [FPGA.cpp:119-123](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp#L119-L123)：`SetNumberOfPoints` 先自减再写——对应文档「寄存器存点数 − 1」。
- [FPGA.cpp:125-133](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp#L125-L133)：`SetSamplesPerPoint` 除以 16 再写——对应文档「SPP 以 16 样本为步进」，并夹到 8191。
- [FPGA.cpp:146-156](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp#L146-L156)：`SetupSweep` 用移位拼出 SweepSetup 寄存器的 stage/sync/端口 stage 位域，与 [SPIConfig.vhd:289-292](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/SPIConfig.vhd#L289-L292) 的拆解互为镜像。

**(i) 引脚与总线复用的文档约定。** [FPGA_protocol.tex:59-91](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/FPGA_protocol.tex#L59-L91) 给出数字接口引脚表和 AUX1/AUX2 四种组合的真值表，并特别提醒：直通 PLL 模式下 NSS 会转发到 LE 引脚，而 LE 必须在有效寄存器移完前保持低，所以**要先拉低 NSS 再切换到 PLL 直通模式**——这是顶层 (h) 那段多路器背后隐藏的时序约定。

#### 4.2.4 代码实践（协议三方一致性核对）

1. **实践目标**：任选三个寄存器，在「协议文档、FPGA 解码代码、固件写入代码」三处各找到定义，写一份字段级一致性核对记录，体会这种「文档 + 人工对齐」协议的维护方式。
2. **操作步骤**（以 0x02 Samples Per Point 为例做一遍，再自选两个）：
   - 文档侧：读 [FPGA_protocol.tex:322-333](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/FPGA_protocol.tex#L322-L333)，记下「以 16 样本为步进、仅当 SweepConfig 样本档位为 000 时生效」。
   - FPGA 侧：在 [SPIConfig.vhd:274](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/SPIConfig.vhd#L274) 找到 `when 2 => NSAMPLES <= spi_buf_out(12 downto 0);`，记下位域是 bit12..0、13 位。
   - 固件侧：读 [FPGA.cpp:125-133](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp#L125-L133)，记下写入前除以 16、上限 8191。
   - 把三行并排填进表格，勾选「一致/不一致」。
3. **需要观察的现象**：核对中你会遇到至少一处需要动脑的位域（例如 0x03 System Control 的 LED 位在固件里取反、FPGA 侧也取反，[SPIConfig.vhd:282](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/SPIConfig.vhd#L282) 的 `LEDS <= not spi_buf_out(9 downto 7)`），注意哪些极性约定只写在代码注释里。
4. **预期结果**：得到一张三列核对表，三处定义全部一致则说明你已能独立读懂这套协议；若发现任何字段不一致，那正是文档过期或注释缺失的信号（如实记录）。本实践同样无需硬件，无「待本地验证」项。

#### 4.2.5 小练习与答案

**练习 1**：MCU 想知道「有没有新采样数据、LO 是否失锁」，不读任何结果口，只开一次 SPI 事务怎么做到？

**答案**：拉低 NSS 发送任意命令字的**第一个字期间**，FPGA 回送的正是中断状态字（[SPIConfig.vhd:211-215](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/SPIConfig.vhd#L211-L215)），bit2 即新数据、bit0 即 LO 失锁；平时也可通过屏蔽寄存器让这些位直接拉高 `MCU_INTR` 硬件中断线。

**练习 2**：为什么读采样结果（`"110"`）要求 MCU「在下一批数据到来之前」读完？没读完会发生什么？

**答案**：结果只保存在一组寄存器里，新数据会直接覆盖；若覆盖发生时 `unread_sampling_data` 仍为 1，[SPIConfig.vhd:205-210](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/SPIConfig.vhd#L205-L210) 会置 `data_overrun`（状态字 bit3），且按文档说明该位只有复位 FPGA 才能清除。这也是 Sweep 设计 halt/resume 机制的原因之一（低 IF 带宽时采样很慢，MCU 来不及读就先暂停，u6-l2 展开）。

**练习 3**：寄存器地址在命令字里只占 5 位（32 个），但寄存器表用到了 0x15（21），地址自动递增（L309）有什么用？

**答案**：写 0x08-0x0F 这 8 个连续的 MAX2871 默认寄存器时，MCU 只需发一次 `"100"+0x08` 命令字，随后连发 8 个数据字即可（[FPGA.cpp:201-210](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp#L201-L210) 的 `WriteMAX2871Default` 就是逐个写的封装），地址递增让连续写少发命令字、缩短配置时间。

### 4.3 数据流水线总览

#### 4.3.1 概念说明

FPGA 存在的全部理由是**实时性**：三路 ADC 以约 914 kHz 连续吐出 16 位样本，MCU 跑 FreeRTOS 无法保证逐样本不丢，USB 也搬不动原始流。所以 LibreVNA 把「每个采样点该做的数学」全部下沉到片上：**加窗 → 正交解调/DFT → 累加成一对 48 位 I/Q**，每个扫描点只往 MCU 送一次约 38 字节的结果。这就是 u1-l1 框图里「FPGA 紧贴 ADC 做实时处理」的具体含义。

流水线有一个精妙的设计：**加窗之后的样本同时喂给两条并行支路**——

- **VNA 支路**（`Sampling`）：在 250 kHz 中频处做单 bin 正交解调，得到该中频信号的复振幅（I/Q），用于 S 参数；
- **SA 支路**（`DFT`）：96 bin 多点 DFT，一次采样期得到 96 个频点的功率，是频谱仪模式的加速器（u5-l4 讲过 SA 点数多、MCU 逐点编排慢的问题）。

两条支路互不干扰，由各自的中断开关独立启停（DFT 由屏蔽寄存器 bit5 即 DFTIE 使能，[SPIConfig.vhd:153](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/SPIConfig.vhd#L153)；顶层 `dft_reset <= not dft_enable`，[top.vhd:823](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L823)——不用时整块复位省电）。

#### 4.3.2 核心流程

一个扫描点内的数据流（伪代码，对照 4.3.3 的实例）：

```text
Sweep 状态机（每个扫描点循环）:
    从 SweepConfigMem 读出本点 96 位配置
    ├── 组合出 Source/LO1 各 4 个 32 位寄存器 → MAX2871 组件写入两颗 PLL
    ├── 等待 plls_locked 且经过 SETTLING_TIME
    ├── 按 stage 编排路由激励到端口 1 或 2（PORT1/2_SELECT 等输出）
    └── 发出 START_SAMPLING 脉冲
                              │
采样链（每个样本重复一次）:     ▼
    MCP33131 ×3  ──16 位样本──▶  Windowing（乘窗系数 → 18 位）
                                     │
                    ┌────────────────┴────────────────┐
                    ▼ (NEW_SAMPLE)                    ▼ (NEW_SAMPLE)
        Sampling：按 PHASEINC 旋转样本          DFT：96 个 bin 并行
        累加 I/Q（每通道 48 位）              累加（每 bin 48 位）
                    │                                 │
                    ▼                                 ▼
        拼进 304 位 sampling_result        拼进 192 位 dft_output
        （含 16 位点号/stage 头）           （"101"命令逐 bin 读出）
                    │
                    ▼
        SPICommands：置 ND 中断 → MCU 经 SPI 读走 19 个字
```

**采样率与解调频率的数学**（102.4 MHz 主时钟的由来）：

ADC 由主时钟分频驱动，文档给出的关系（[FPGA_protocol.tex:386-399](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/FPGA_protocol.tex#L386-L399)）：

\[ SR_{ADC} = \frac{102.4\,\text{MHz}}{Presc}, \qquad Presc \ge 112 \]

单 bin 解调的相位步进寄存器（[FPGA_protocol.tex:401-415](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/FPGA_protocol.tex#L401-L415)）：

\[ PhaseInc = \frac{4096 \cdot f_{IF2}}{SR_{ADC}} \quad (\text{单位 } 2\pi/4096\,\text{rad}) \]

代入默认值验证：取 \( Presc = 112 \)，则 \( SR_{ADC} = 102.4\,\text{MHz}/112 \approx 914.286\,\text{kHz} \)；对 \( f_{IF2} = 250\,\text{kHz} \)：

\[ PhaseInc = \frac{4096 \times 250000}{914286} = 1120 = 10 \times Presc \]

这正是文档说的「250 kHz 中频时 PhaseInc = 10 × Presc」，也正是 [SPIConfig.vhd:177-178](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/SPIConfig.vhd#L177-L178) 复位默认值 `ADC_PRESCALER=112`、`ADC_PHASEINC=1120` 的来历——三个数字互相咬合，说明 102.4 MHz 不是随手选的。

**结果格式的数学**：采样结果是 6 个 48 位 I/Q（[FPGA_protocol.tex:658-744](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/FPGA_protocol.tex#L658-L744)）。之所以 48 位，是因为最多 91392 个样本（SweepConfig 样本档位 111）的 18 位加窗样本求和需要 \( 18 + \log_2 91392 \approx 34.5 \) 位，留余量取 48。DFT 的 bin 频率由两个寄存器决定：\( f_{firstBin} = SR_{ADC} \cdot \text{FIRST\_BIN}/2^{16} \)，\( \Delta f = SR_{ADC} \cdot \text{SPACING}/2^{24} \)（[FPGA_protocol.tex:508-536](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/FPGA_protocol.tex#L508-L536)）。

#### 4.3.3 源码精读

**(a) 加窗级。** [top.vhd:647-660](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L647-L660)：三路 16 位原始样本进、三路 18 位加窗样本出（16 位样本 × 窗系数会变宽，多出 2 位防溢出）。两个接线细节：`RESET => sampling_start`——每次新采样开始时复位窗计数器，窗与采样期严格对齐；`ADC_READY => adc_port1_ready` 复用端口 1 ADC 的就绪信号作为「三个样本都到齐」的节拍（4.1.3 (f) 的同步采样设计）。

**(b) VNA 支路。** [top.vhd:662-685](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L662-L685)：`Sampling` 的输入是三路 18 位加窗样本与 `PHASEINC`，输出是 6 个 48 位 I/Q。从顶层看它完成的就是「以相位步进旋转样本并按通道累加」的单 bin 复振幅提取；`ipcore_dir` 里的 SinCos 查表 IP 同时被 `Sampling.vhd` 与 `DFT.vhd` 引用（可用 Grep 验证），是数字振荡器的sin/cos 来源，内部结构留待 u6-l3/u6-l4。注意 6 个 48 位结果在 L678-683 直接按位段拼进 304 位 `sampling_result`——**位段的排列顺序就是协议文档 Sampling Result 一节的图形**（最高 16 位是 `RESULT_INDEX`，见 (d)）。

**(c) SA 支路。** [top.vhd:825-838](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L825-L838)：`DFT` 例化为 `BINS => 96`，只吃端口 1/2 两路（参考通道对频谱测量无用），与 `Sampling` 共用同一个 `NEW_SAMPLE` 节拍，`NEXT_OUTPUT` 由 SPICommands 在每次 `"101"` 读事务时推进一个 bin。输出 192 位 = 2 通道 × (I+Q) × 48 位。

**(d) 结果头与点号。** [top.vhd:734](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L734)：`RESULT_INDEX => sampling_result(303 downto 288)`——Sweep 把当前 stage 号与扫描点号（协议格式：3 位 stage + 13 位点号）直接写进结果的最高 16 位。MCU 读回 19 个字后据此知道「这是第几个点、哪个激励阶段」，这正是 u4-l2 讲过的 VNADatapoint「点号 + stage 掩码」在 FPGA 侧的源头。

**(e) 扫描编排与配置存储。** [top.vhd:689-735](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L689-L735) 的 SweepModule 是流水线的「指挥」：输出 `CONFIG_ADDRESS` 去 `SweepConfigMem` 取本点配置（L693-694），输出 PLL 寄存器组与 `RELOAD_PLL_REGS` 给两颗 MAX2871 组件，回收 `PLL_RELOAD_DONE/PLL_LOCKED`，输出 `START_SAMPLING` 给采样链，回收 `SAMPLING_DONE`。`SETTLING_TIME`（20 位，寄存器 0x14/0x15）控制「加激励到开始采样」的等待。而配置存储本身 [top.vhd:840-850](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L840-L850) 是一块 13 位地址 × 96 位的双端口 BRAM：端口 A 由 SPICommands 写（MCU 预编程），端口 B 由 Sweep 读（自主扫描）——**双端口 RAM 是「MCU 写、FPGA 读」两个主设备共享存储的标准结构**，也是整个「预编程 + 自主扫描」架构的支点。

**(f) 停止与暂停。** [top.vhd:687](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L687)：`sweep_reset <= not aux3_sync`——AUX3 是低有效的扫描使能（协议文档引脚表：改设置时必须拉高），MCU 改配置前拉低 AUX3 即冻结扫描状态机。更细粒度的暂停则走 SweepConfig 的 halt 位 + `"001"` 恢复命令（4.2），u6-l2 精读。

#### 4.3.4 代码实践（跟踪一个样本的完整旅程）

1. **实践目标**：以「端口 1 的某一个 ADC 样本」为主角，写出它从芯片引脚到 MCU 内存经过的每一级模块、每次位宽变化与每次时钟节拍，形成一张数据护照。
2. **操作步骤**：
   - 从 [top.vhd:609](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L609)（`SDO => PORT1_SDO`）出发，记录串行位如何变成 16 位 `adc_port1_data`（模块内部细节可先当黑盒，标「u6-l3」）。
   - 跟 `adc_port1_data` 进入 Windower（L651），记位宽 16→18；再跟 `port1_windowed` 同时进入 Sampler（L669）与 SA_DFT（L829），画出分叉。
   - 在 Sampler 侧跟到 `PORT1_I => sampling_result(287 downto 240)`（L678），记最终位宽 48；在 SPICommands 侧找到 `"110"` 命令如何把它搬上 `MCU_MISO`（[SPIConfig.vhd:229-232](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/SPIConfig.vhd#L229-L232) 与 ReadResult 状态）。
   - 最后在固件侧找到读回后的去向：[FPGA.cpp:286-305](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp#L286-L305) 的 `InitiateSampleRead` 以命令字 `0xC000`（bit15..13 = `"110"`，读采样结果）发起 40 字节 DMA 收发（命令字 2 字节 + 19 个字）；DMA 完成回调 [FPGA.cpp:316-334](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp#L316-L334) 用 `assembleSampleResultValue` 把字节拼回 48 位有符号数，并从 `raw[38]/raw[39]` 拆出 `pointNum`（13 位）与 `stageNum`（3 位）——正好对应结果最高 16 位的头字段。
3. **需要观察的现象**：位宽在 16 → 18 → 48 的三次变化分别对应「ADC 原始精度」「乘窗增益」「长累积动态范围」，想想每一步为什么必须加宽。
4. **预期结果**：一张 6~8 步的旅程表（引脚 → MCP33131 → Windowing → Sampling/DFT → 结果寄存器 → SPICommands → MISO → 固件 DMA 拼装），每步有模块名、行号、位宽。若想进一步用仿真验证（在 `Test_SPICommands.vhd` 里注入一个已知幅度的正弦并检查读回的 I/Q），需要 Xilinx ISE 14.7 环境，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 VNA 支路只算「一个 bin」，频谱仪支路却要 96 个 bin？

**答案**：VNA 每次测量只关心中频 \( f_{IF2} \) 处一个频率点的复振幅（激励与本振都是单音，信息集中在 250 kHz 中频），单 bin 解调即足够且最省资源；频谱仪要在一段带宽内找信号，需要同时观测多个频率，96 bin DFT 让一次采样期覆盖 96 个频点，是 u5-l4 所说 SA 模式提速的关键。

**练习 2**：`Sampling` 与 `DFT` 都以 `windowing_ready` 为 `NEW_SAMPLE`，如果 MCU 把窗类型设为「矩形」（System Control 的 Window 位 = 00），两条支路会发生什么？

**答案**：窗系数全为满幅（等效不加权，[FPGA_protocol.tex:373](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/FPGA_protocol.tex#L373) 的 Rectangular），流水线结构不变，只是频谱泄露特性变差——矩形窗旁瓣高，对相邻信号分辨不利；这正是 GUI 里可选 Hann/Kaiser/Flat Top 的原因（u6-l4 展开）。

**练习 3**：估算：若 `Presc = 112`、SweepConfig 样本档位选 011（912 样本，对应约 1 kHz 等效中频带宽），单个扫描点的纯采样时长约是多少？

**答案**：采样率 \( \approx 914.286\,\text{kHz} \)，采 912 个样本约需 \( 912 / 914286 \approx 0.997\,\text{ms} \)，即约 1 ms；加上 PLL 重配与稳定时间（SETTLING_TIME）才是每点的完整耗时。这与文档「914 kHz / SPP」的等效带宽口径一致（档位表里 912 样本 ≈ 1 kHz，见 [FPGA_protocol.tex:619-631](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/FPGA_protocol.tex#L619-L631)）。

## 5. 综合实践

把三个模块串成一个任务——**「给一次完整扫描写一份解剖报告」**：

1. 先完成 4.1.4 的框图（静态结构）。
2. 假设一次 VNA 测量：501 个点、IF 带宽 1 kHz、Kaiser 窗、双端口（每点 2 个 stage）。基于本讲源码写出时序清单：
   - MCU 侧准备动作各对应哪个寄存器/命令（提示：`SetNumberOfPoints`、`SetSamplesPerPoint`、`SetWindow`、`SetupSweep`、`WriteSweepConfig`，全部在 [FPGA.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp) 中，行号在 4.2.3/4.3.3 已给出一部分，其余自行定位）；
   - FPGA 侧每个点、每个 stage 的动作序列（读配置 → 写 PLL → 等锁定与稳定 → 路由激励 → 采样 → 置 ND）；
   - MCU 收到 501×2 次 ND 中断各做什么。
3. 用 4.2.4 的核对表方法，把其中至少两个寄存器在三处（文档/SPIConfig/FPGA.cpp）对齐。
4. 交付物：框图 1 张 + 时序清单 1 份 + 核对表 1 份。全程无需硬件；若想用 testbench 验证时序（例如在 `Test_SPICommands.vhd` 基础上模拟 501 点配置写入），需要 ISE 14.7，**待本地验证**。

## 6. 本讲小结

- `top.vhd` 是一张文字原理图：12 类功能块（时钟/复位/同步器基础设施、Sweep 编排、三路 ADC 接口、Windowing、Sampling、DFT、两颗 MAX2871 写入器、SPICommands、双端口 SweepConfigMem）加少量输出多路器构成，器件为 Spartan-6 xc6slx9，主时钟 16 MHz 倍频到 102.4 MHz。
- MCU 命令入口是「SPI + 寄存器堆」：第一个 16 位命令字的 bit15..13 区分写扫描配置/写寄存器/读结果/恢复扫描等 8 类事务，命令字期间回送中断状态字；协议由 `FPGA_protocol.tex`、`SPIConfig.vhd`、`FPGA.cpp` 三方人工对齐（无 USB 协议那种同源编译保障）。
- 数据流水线：三路 ADC 同步采样 → 加窗（16→18 位）→ 分两支并行——`Sampling` 按 PHASEINC 做单 bin 正交解调得 48 位 I/Q（VNA），`DFT` 算 96 bin（频谱仪）→ 结果带 16 位「点号+stage」头，经 ND 中断由 MCU 用 SPI 读走。
- 「MCU 预编程 + FPGA 自主扫描」的支点是双端口 `SweepConfigMem`（每点 96 位配置，最多 4501 点）：端口 A 归 MCU 写、端口 B 归 Sweep 读；扫频环路上的 PLL 也由 FPGA 直驱（MAX2871 组件），MCU 彻底出环。
- 102.4 MHz 时钟让采样率（914.286 kHz @ Presc=112）、250 kHz 中频与相位增量（1120）恰好整数咬合，复位默认值即这套关系的一次自洽。
- 射频控制线有两套驱动来源：Sweep（正常扫描）与硬件覆盖寄存器 0x07（信号源模式钉住电平），顶层用多路器二选一。

## 7. 下一步学习建议

本讲建立了 FPGA 的全局图，下一讲起逐块下钻，建议顺序：

1. **u6-l2（扫描引擎）**：精读 [FPGA/VNA/Sweep.vhd](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sweep.vhd) 的状态机——本讲只看到了它的接口（START_SAMPLING、RELOAD_PLL_REGS、SWEEP_HALTED 等），它如何逐点推进、何时 halt、如何配合触发同步是下一讲主角；同时补齐 Synchronizer 的亚稳态原理。
2. **u6-l3（采样链路）**：`MCP33131.vhd` 的 ADC 时序与 `Sampling.vhd` 的单 bin 解调内部（含 SinCos IP 的使用）。
3. **u6-l4（片上信号处理）**：`Windowing.vhd`/`window.vhd` 与三个 `.dat` 窗系数文件、`DFT.vhd` 的递推结构。
4. **u6-l5（MCU-FPGA 接口下钻）**：`spi_slave.vhd` 的位级时序与 `MAX2871.vhd` 的寄存器写入时序，把本讲 4.2 的协议往下再剥一层。
5. 想动手验证的读者可以随时插入 **u6-l6（testbench 全家福）**：`Test_SPICommands.vhd`、`Test_Sampling.vhd` 等是验证本讲所有结论的仿真手段。

阅读本讲时若对固件侧如何使用这些接口有疑问，回头翻 u5-l2（Hardware 门面与 SPI 分时路由）与 u5-l4（设备端三大模式的扫描时序）。

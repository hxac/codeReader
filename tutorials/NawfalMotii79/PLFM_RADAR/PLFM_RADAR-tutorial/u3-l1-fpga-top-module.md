# FPGA 顶层模块 radar_system_top 全景

## 1. 本讲目标

学完本讲后，你应该能够：

- 理解 `radar_system_top.v` 在整片 FPGA 里扮演「总接线员」的角色，把发射机、接收机、CFAR、自测试、USB 接口这五大子模块连成一条完整流水线。
- 看懂三个主要时钟域（100MHz 系统 / 120MHz DAC / USB 接口时钟）在顶层的入口，以及跨域信号为什么不能直接连。
- 掌握 `USB_MODE` 参数如何用 `generate` 在编译期于 FT601（32 位 USB 3.0）与 FT2232H（8 位 USB 2.0）两个 USB 模块之间二选一，并理解未用引脚的 tie-off（悬空处理）方式。
- 读懂主机命令（opcode）到 `host_*` 配置寄存器的 `case` 译码表，能说出某个 opcode 会改写哪个寄存器。
- 理解 `radar_system_top_50t.v` 这个「物理包装层」如何把同一份核心逻辑适配到只有 69 个 IO 的量产芯片。

本讲是进入 FPGA 内部细节的**第一站**，只读顶层「接线图」，不深入任何子模块内部算法（DDC、匹配滤波、CFAR 等在 U4 系列讲义展开）。

## 2. 前置知识

在开始之前，你需要先建立以下直觉（这些概念在 U1、U2 已建立，这里只做一句话复习）：

- **顶层模块（top module）**：FPGA 工程里「最外面」那个模块。它声明与芯片物理引脚（pin）一一对应的端口，并在内部例化（instantiate，即「摆放并连线」）各个子模块。你可以把它想象成一块 PCB：子模块是芯片，顶层是走线与接插件。
- **例化（instantiation）**：在 Verilog 里写 `模块名 实例名 ( .端口(连线), ... );`，相当于把一个芯片焊到板上并连好线。本讲你会看到大量 `*_inst` 结尾的实例名。
- **时钟域（clock domain）**：由同一棵时钟驱动的所有触发器（flip-flop）构成一个域。不同时钟域之间的信号必须做「跨时钟域（CDC）」处理，否则会出现亚稳态（metastability）。CDC 的细节是下一讲 u3-l2 的主题，本讲你只需知道「跨域的地方有特殊电路」。
- **generate 块**：Verilog 的编译期条件分支，类似 C 的 `#if`。综合后只有命中条件的分支会变成真实电路，另一分支的代码不占资源。
- **tie-off（悬空处理）**：把一个未使用的输出端口接到一个固定常量（如 `1'b1`、`1'b0`），或把未使用的输入端口接到常量电平，避免综合时出现「悬空线」警告。
- **opcode（操作码）**：主机（PC 上的 GUI）通过 USB 发给 FPGA 的命令的第一个字节，用来告诉 FPGA「这条命令要干什么」。本讲关注的重点是「opcode 如何变成 FPGA 内部的寄存器写操作」。

> 名词小贴士：Verilog 里 `wire` 是连续驱动的连线，`reg` 是在 `always` 块里被赋值的寄存器（或组合逻辑）。`localparam` 是编译期常量。

## 3. 本讲源码地图

本讲涉及两个文件，它们是「核心 + 包装」的关系：

| 文件 | 作用 | 行数 |
|------|------|------|
| [`radar_system_top.v`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v) | FPGA 逻辑核心：声明全部端口、例化五大子模块、做 USB_MODE 二选一、解码主机命令 | 1078 |
| [`radar_system_top_50t.v`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top_50t.v) | 量产包装层：把核心模块适配到 XC7A50T（69 IO），固定 `USB_MODE=1` | 223 |

此外，顶层例化的子模块（本讲只点名、不深入）都在同目录下，方便你按图索骥：

- 发射机：`radar_transmitter.v`（实例 `tx_inst`）
- 接收机：`radar_receiver_final.v`（实例 `rx_inst`，内部串接 DDC→匹配滤波→MTI→Doppler，详见 U4）
- CFAR 检测器：`cfar_ca.v`（实例 `cfar_inst`）
- 板级自测试：`fpga_self_test.v`（实例 `self_test_inst`）
- USB 接口：`usb_data_interface.v`（FT601）与 `usb_data_interface_ft2232h.v`（FT2232H），二选一

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 顶层例化**、**4.2 USB_MODE generate**、**4.3 命令解码**。

### 4.1 顶层例化：把五大子模块连成一条流水线

#### 4.1.1 概念说明

`radar_system_top` 的核心职责是**接线，不是计算**。它自己几乎不做信号处理，而是：

1. 声明与 FPGA 物理引脚一一对应的端口（时钟、DAC、ADC、USB、SPI、GPIO……）。
2. 把这些端口像「接插件」一样，分配给内部各个子模块。
3. 用内部 `wire`（连线）把子模块的输出接到下一个子模块的输入，串成一条数据流水线。

可以把顶层想象成一张机箱背板：发射机、接收机、CFAR、USB 是插在上面的板卡，顶层负责用排线把它们按正确顺序连起来。

#### 4.1.2 核心流程

数据在顶层内部的流动可以这样概括（接收方向）：

```text
发射机 tx_inst ──chirp 计数/帧脉冲(CDC)──► 接收机 rx_inst ──Doppler/距离像──► DC notch ──► CFAR cfar_inst
                                                                                  │
   自测试 self_test_inst ◄──ADC debug tap                                        │
                                                                                  ▼
                                                                       USB usb_inst ──► 主机
```

要点：

- **发射机**工作在 120MHz DAC 时钟域，产生 chirp 波形、控制混频器与 ADAR1000 的 load 信号；它还输出「当前是第几个 chirp / 第几帧」的计数。
- **接收机**工作在 100MHz 系统域，吃进 ADC 的 LVDS 数据，内部完成 DDC、匹配滤波、距离抽取、MTI、Doppler FFT，吐出距离-多普勒数据与距离像。
- 两个模块跨时钟域，所以 chirp 计数与帧脉冲要经过 CDC 才能从 120MHz 域进到 100MHz 域（见 4.1.3）。
- **CFAR** 接收经过 DC notch 过滤的 Doppler 数据，做目标检测。
- **自测试**借用接收机的 ADC debug tap 来抓真实 ADC 数据，做上电自检。
- **USB** 把检测/数据打包发给主机，同时把主机命令接进来。

#### 4.1.3 源码精读

顶层端口声明里，三类时钟是入口。注意注释写明了 USB 接口时钟在两种 USB 模式下频率不同：[radar_system_top.v:22-27](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L22-L27)。文件头注释也对三个时钟域做了明确说明：[radar_system_top.v:12-15](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L12-L15)。

```verilog
input wire clk_100m,        // 100MHz 系统时钟（主处理域）
input wire clk_120m_dac,    // 120MHz DAC 时钟（发射/chirp 域）
input wire ft601_clk_in,    // USB 接口时钟（FT601=100MHz，FT2232H=60MHz）
input wire reset_n,         // 全局低有效复位
```

时钟进来后先用 `BUFG` 缓冲一遍，降低时钟偏斜（skew）。仿真模式下没有 `BUFG`，直接穿通：[radar_system_top.v:298-318](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L298-L318)。复位则分别同步到三个域（`ASYNC_REG` 打 2~3 级）：[radar_system_top.v:320-355](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L320-L355)。

发射机例化是典型的「端口对应」写法，把时钟、DAC、混频器、ADAR1000、SPI 电平转换器全部接到 `tx_inst`：[radar_system_top.v:435-496](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L435-L496)。注意它同时接了两套复位：120MHz 域用 `sys_reset_120m_n`，100MHz 域用 `sys_reset_n`。

接收机 `rx_inst` 是最「重」的例化，端口最多——它要把所有 `host_*` 配置寄存器（后面 4.3 讲）都接进去：[radar_system_top.v:502-566](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L502-L566)。其中chirp 计数与帧脉冲用的是 **CDC 之后的版本**（`tx_current_chirp_sync`、`tx_new_chirp_frame_sync`），而不是发射机原始输出——这就是跨域安全的体现。

关键的跨域代码：chirp 计数用 6 位 Gray 码同步器，帧脉冲用 toggle-CDC（脉冲→电平翻转→同步→边沿检测还原脉冲）：[radar_system_top.v:385-429](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L385-L429)。

> 为什么帧脉冲要用 toggle-CDC 而不能直接同步？因为它是 120MHz 域里的「1 个时钟周期宽」的脉冲，100MHz 域的电平同步器可能完全采样不到它。本讲只点到为止，原理在 u3-l2 详解。

CFAR 例化接的是经过 DC notch 过滤的数据（`notched_doppler_*`）：[radar_system_top.v:620-651](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L620-L651)。自测试例化借用了接收机的 ADC debug tap（`rx_dbg_adc_i`/`rx_dbg_adc_valid`）：[radar_system_top.v:671-684](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L671-L684)。

#### 4.1.4 代码实践

**实践目标**：用肉眼在顶层画出「五大实例 + 主要数据连线」的接线图，建立空间感。

**操作步骤**：

1. 打开 [`radar_system_top.v`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v)，搜索关键字 `tx_inst`、`rx_inst`、`cfar_inst`、`self_test_inst`、`usb_inst`，分别定位五个例化。
2. 在每个例化的端口列表里，找出它**接收哪些内部 wire**、**驱动哪些内部 wire**。例如 `rx_inst` 的输出 `.doppler_output(rx_doppler_output)` 驱动了 `rx_doppler_output` 这根线，随后被 DC notch 逻辑消费。
3. 用纸画出五个方框，用箭头标出 `tx_current_chirp_sync`、`rx_doppler_output`、`notched_doppler_data`、`cfar_detect_flag`、`usb_detect_flag` 这几根关键连线的走向。

**需要观察的现象**：你会发现顶层里几乎没有算术运算（`+`、`*`），几乎全是 `assign` 连线和例化端口对应——这正是「顶层只接线」的特征。少数例外是 DC notch 的比较逻辑与命令译码。

**预期结果**：你能用一句话回答「CFAR 的输入数据来自谁、输出送给谁」——输入来自 DC notch（它消费 `rx_inst`），输出经 `rx_detect_flag` 送给 `usb_inst`。

> 本实践是「源码阅读型实践」，不需要编译，但如果你想验证自己的接线图正确，可在第 5 节综合实践中用回归脚本做端到端确认。

#### 4.1.5 小练习与答案

**练习 1**：顶层为什么把 `current_chirp` 输出引脚接到 `tx_current_chirp_sync`（CDC 后版本）而不是发射机原始的 `tx_current_chirp`？

**参考答案**：`tx_current_chirp` 在 120MHz DAC 域产生，若直接接到由 100MHz 域驱动的输出寄存器/片外采样，会跨时钟域产生亚稳态。CDC 后的 `tx_current_chirp_sync` 已同步到 100MHz 域，可安全使用。源码见 [radar_system_top.v:1008-1011](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L1008-L1011)。

**练习 2**：`rx_inst` 的复位为什么单独有一个 100MHz 域的 `sys_reset_n`，而 `tx_inst` 同时有 `sys_reset_120m_n` 和 `sys_reset_n` 两个？

**参考答案**：`rx_inst` 全部工作在 100MHz 域，只需一个 100MHz 同步复位；`tx_inst` 跨了 120MHz DAC 域（DAC/chirp 逻辑）与 100MHz 域（边沿检测/CDC），所以两个域各需要一个同步复位，分别用 `sys_reset_120m_n` 和 `sys_reset_n`。

---

### 4.2 USB_MODE generate：FT601 与 FT2232H 的编译期二选一

#### 4.2.1 概念说明

AERIS-10 雷达有两种 USB 接口方案，对应两种开发板：

- **FT601**：32 位 USB 3.0，用于 200T 高端板（带宽大）。
- **FT2232H**：8 位 USB 2.0（245 同步 FIFO 模式），用于 50T 量产板（成本低）。

这两者的物理引脚、数据位宽、握手信号都不同。如果为每种板子写一套顶层，代码会大量重复。本项目的做法是：**用一份顶层，靠 `parameter USB_MODE` 在编译期 `generate` 二选一**，只例化命中条件的那一个 USB 模块。这样同一份 RTL 既能综合到 200T 板，也能综合到 50T 板，只需改一个参数。

#### 4.2.2 核心流程

参数定义：[radar_system_top.v:145](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L145)。

```verilog
parameter USB_MODE = 1;   // 0=FT601(32-bit,200T), 1=FT2232H(8-bit,50T 量产默认)
```

generate 块的逻辑：

```text
if (USB_MODE == 0)  → gen_ft601 分支：例化 usb_data_interface（FT601）
                      并把 FT2232H 的物理端口 tie-off 到安全电平
else                → gen_ft2232h 分支：例化 usb_data_interface_ft2232h（FT2232H）
                      并把 FT601 的物理端口 tie-off 到安全电平
```

两个分支内部的「应用层接口」是一样的——同样接收 `usb_range_profile`、`usb_doppler_*`、`usb_detect_*`，同样输出 `usb_cmd_*`。所以**从雷达数据通路看，两个 USB 模块是可互换的**，差异只在物理层那一侧。

#### 4.2.3 源码精读

整个 generate 块：[radar_system_top.v:718-863](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L718-L863)。

**FT601 分支**（`gen_ft601`）例化的是 `usb_data_interface`：[radar_system_top.v:719-784](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L719-L784)。因为选了 FT601，FT2232H 的物理引脚（`ft_*`）就成了未用输出，必须 tie-off，否则综合器会报警告或把它们优化掉导致端口不匹配：

```verilog
assign ft_rd_n = 1'b1;   // 读使能拉高（无效）
assign ft_wr_n = 1'b1;   // 写使能拉高（无效）
assign ft_oe_n = 1'b1;   // 输出使能拉高（高阻）
assign ft_siwu = 1'b0;   // 唤醒信号拉低
```

源码见 [radar_system_top.v:787-790](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L787-L790)。

**FT2232H 分支**（`gen_ft2232h`）例化的是 `usb_data_interface_ft2232h`：[radar_system_top.v:792-851](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L792-L851)。此时未用的是 FT601 的物理引脚（`ft601_*`），tie-off 列表更长——包括 4 位的字节使能、多个控制信号、可选时钟输出：

```verilog
assign ft601_be      = 4'b0000;
assign ft601_txe_n   = 1'b1;
assign ft601_rxf_n   = 1'b1;
assign ft601_wr_n    = 1'b1;
assign ft601_rd_n    = 1'b1;
assign ft601_oe_n    = 1'b1;
assign ft601_siwu_n  = 1'b1;
assign ft601_clk_out = 1'b0;
```

源码见 [radar_system_top.v:854-861](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L854-L861)。

注意两个分支里 USB 模块的实例名都叫 `usb_inst`，但因为它们被 `generate` 的命名块（`gen_ft601` / `gen_ft2232h`）包住，层次路径不同（`gen_ft601.usb_inst` vs `gen_ft2232h.usb_inst`），所以不会冲突。

**物理包装层 `radar_system_top_50t.v`** 就是「以 `USB_MODE=1` 例化核心模块」的典型用法：[radar_system_top_50t.v:117-121](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top_50t.v#L117-L121)。它把 FT2232H 的 60MHz `ft_clkout` 接到核心模块共享的 `ft601_clk_in` 时钟端口（[radar_system_top_50t.v:124](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top_50t.v#L124)），并用一组 `_nc`（no-connect）悬空线处理 50T 板上没有物理引脚的 FT601 与 debug 输出，保证核心逻辑的完整性不被综合器裁剪：[radar_system_top_50t.v:97-115](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top_50t.v#L97-L115)。未用的 FT601 输入被接到常量（如 `ft601_txe_tied = 1'b0`），inout 总线被置为高阻 `32'hZZZZZZZZ`：[radar_system_top_50t.v:87-95](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top_50t.v#L87-L95)。

#### 4.2.4 代码实践

**实践目标**：亲手验证两个 generate 分支各自例化了什么、tie-off 了什么，确认「二选一」机制。

**操作步骤**：

1. 在 [`radar_system_top.v`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v) 中定位 `generate`（第 718 行）到 `endgenerate`（第 863 行）。
2. 找到 `if (USB_MODE == 0) begin : gen_ft601` 分支，记录：例化的模块名、实例名，以及其后 4 行 `assign ft_*` 的 tie-off 电平。
3. 找到 `else begin : gen_ft2232h` 分支，记录：例化的模块名、实例名，以及其后 8 行 `assign ft601_*` 的 tie-off 电平。
4. 打开 [`radar_system_top_50t.v`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top_50t.v)，确认它例化核心时写的 `.USB_MODE(1)`。

**需要观察的现象**：两个分支里接到 USB 模块的「应用层」端口（`range_profile`、`doppler_real`、`cmd_opcode` 等）几乎完全相同——这正是「可互换」的体现；差异全在物理层 `ft601_*` / `ft_*` 那一侧。

**预期结果（应填入笔记）**：

| 分支 | 命中条件 | 例化模块 | 实例名 | 未用引脚 tie-off |
|------|----------|----------|--------|-------------------|
| `gen_ft601` | `USB_MODE==0` | `usb_data_interface` | `usb_inst` | `ft_rd_n=1`、`ft_wr_n=1`、`ft_oe_n=1`、`ft_siwu=0` |
| `gen_ft2232h` | `USB_MODE==1`（默认/50T） | `usb_data_interface_ft2232h` | `usb_inst` | `ft601_be=0000`、`ft601_txe_n=1`、`ft601_rxf_n=1`、`ft601_wr_n=1`、`ft601_rd_n=1`、`ft601_oe_n=1`、`ft601_siwu_n=1`、`ft601_clk_out=0` |

> 本实践为静态阅读型，无需运行；结论可直接对照源码逐行核对。

#### 4.2.5 小练习与答案

**练习 1**：为什么两个 generate 分支的实例都叫 `usb_inst` 却不冲突？

**参考答案**：因为它们各自被命名 generate 块 `gen_ft601` / `gen_ft2232h` 包裹，综合后的完整层次路径分别是 `gen_ft601.usb_inst` 与 `gen_ft2232h.usb_inst`，名字唯一。而且 `generate` 是编译期条件，任何一次综合只会命中其中一个分支，物理上不存在两个同名实例。

**练习 2**：`radar_system_top_50t.v` 里 `ft601_data_internal = 32'hZZZZZZZZ` 是什么意思？为什么这样做？

**参考答案**：`Z` 表示高阻态（high-impedance）。这是 inout（双向）总线在「不驱动」时的正确取值——让总线悬空、不主动拉电平，避免与片外冲突。50T 板上 FT601 的 32 位数据总线根本没有物理引脚，但核心模块的 `ft601_data` 端口必须接到「某个东西」，于是接到一个永远高阻的内部线，等于「断开但不报错」。

---

### 4.3 命令解码：从 opcode 到 host_* 寄存器

#### 4.3.1 概念说明

主机（GUI）通过 USB 下发的每条命令是一个 **4 字节结构** `{opcode, addr, value}`，被 USB 模块拆成 `usb_cmd_opcode`（1 字节）、`usb_cmd_addr`（1 字节）、`usb_cmd_value`（2 字节）。FPGA 顶层的任务是把 `usb_cmd_opcode` 当作一个「功能编号」，用一个 `case` 表决定它要改写哪个 `host_*` 配置寄存器。

这些 `host_*` 寄存器随后被接到接收机、CFAR、自测试等子模块（见 4.1.3 的 `rx_inst` 端口），从而主机一条命令就能改变雷达的工作参数（检测门限、CFAR 配置、chirp 时序、AGC、自测试触发……）。

这是 GUI 与 FPGA 之间的**硬契约**：Python 端 `radar_protocol.py` 的 `Opcode` 枚举值必须与 Verilog 端这个 `case` 表一一对应，任何一侧改了 opcode 编号而另一侧不改，命令就会失效或写错寄存器（跨层契约测试 u11-l3 就是用来防这个的）。

#### 4.3.2 核心流程

命令从 USB 到寄存器的完整路径：

```text
主机 ──USB──► usb_inst(USB域) ──► usb_cmd_valid(1周期脉冲, USB域)
                                      │ 拆出 usb_cmd_opcode/addr/value
                                      ▼
                          toggle-CDC：USB域脉冲 ──► 100MHz域脉冲(cmd_valid_100m)
                                      │
                                      ▼
              always @(posedge clk_100m)：case(usb_cmd_opcode) ──► 写对应 host_* 寄存器
```

两个关键设计：

1. **命令跨域**：`usb_cmd_valid` 是 USB 时钟域的 1 周期脉冲，必须用 toggle-CDC 搬到 100MHz 域（与帧脉冲同样手法）。`cmd_data/opcode/addr/value` 在脉冲后会保持稳定，所以直接在 100MHz 域采样即可。
2. **自清零脉冲**：`host_trigger_pulse`、`host_status_request`、`host_self_test_trigger` 这类「触发型」寄存器，在 `case` 命中时置 1，但在每个时钟周期的开头（`else` 分支）无条件清 0，于是它们形成「收到命令那一拍为 1、之后立刻回 0」的单次脉冲。

#### 4.3.3 源码精读

命令跨域的 toggle-CDC 三步：翻转→3 级同步→边沿检测还原脉冲，得到 `cmd_valid_100m`：[radar_system_top.v:865-902](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L865-L902)。

命令译码的 `always` 块在 100MHz 域，复位时给所有 `host_*` 赋默认值，非复位时先清三个触发脉冲、再在 `cmd_valid_100m` 时按 `case` 分发：[radar_system_top.v:911-1002](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L911-L1002)。

核心 `case` 表（节选关键分支）：[radar_system_top.v:950-999](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L950-L999)。

```verilog
case (usb_cmd_opcode)
    8'h01: host_radar_mode      <= usb_cmd_value[1:0];   // 雷达模式
    8'h02: host_trigger_pulse   <= 1'b1;                  // 触发脉冲（自清零）
    8'h03: host_detect_threshold<= usb_cmd_value;         // 检测门限
    8'h04: host_stream_control  <= usb_cmd_value[2:0];    // 数据流开关
    // 0x10-0x15：chirp 时序（运行时可覆盖编译期默认值）
    8'h15: /* 见下方说明：钳制到 DOPPLER_FRAME_CHIRPS */
    8'h20: host_range_mode      <= usb_cmd_value[1:0];    // 距离模式
    8'h21: host_cfar_guard      <= usb_cmd_value[3:0];    // CFAR 保护单元
    8'h22: host_cfar_train      <= usb_cmd_value[4:0];    // CFAR 训练单元
    8'h23: host_cfar_alpha      <= usb_cmd_value[7:0];    // CFAR 门限系数(Q4.4)
    8'h24: host_cfar_mode       <= usb_cmd_value[1:0];    // CA/GO/SO
    8'h25: host_cfar_enable     <= usb_cmd_value[0];      // CFAR 使能
    8'h26: host_mti_enable      <= usb_cmd_value[0];      // MTI 使能
    8'h27: host_dc_notch_width  <= usb_cmd_value[2:0];    // DC 陷波宽度
    8'h28: host_agc_enable      <= usb_cmd_value[0];      // AGC 使能
    // ... 0x29-0x2C：AGC target/attack/decay/holdoff
    8'h30: host_self_test_trigger <= 1'b1;                 // 自测试触发（自清零）
    8'h31: host_status_request    <= 1'b1;                 // 状态回读
    8'hFF: host_status_request    <= 1'b1;                 // 状态回读（别名）
    default: ;
endcase
```

本讲实践任务要求的三个 opcode：

- **`8'h03`** → 写 `host_detect_threshold`（检测门限，16 位），见 [radar_system_top.v:953](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L953)。
- **`8'h21`** → 写 `host_cfar_guard`（CFAR 每侧保护单元数，4 位，取 `value[3:0]`），见 [radar_system_top.v:979](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L979)。
- **`8'h30`** → 置 `host_self_test_trigger`（自测试触发脉冲，自清零），见 [radar_system_top.v:994](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L994)。

一个值得注意的「安全钳制」：opcode `0x15` 设置每仰角 chirp 数时，并不是无条件写入，而是与 `DOPPLER_FRAME_CHIRPS`（固定值 32，因为 Doppler 路径是 16 长 + 16 短的双 16 点 FFT）比较，超范围就钳制到 32 并置 `chirps_mismatch_error` 标志：[radar_system_top.v:250-251](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L250-L251) 与 [radar_system_top.v:961-975](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L961-L975)。这防止主机写入不兼容的 chirp 数导致 Doppler 累加错乱。

> Python 端的 Opcode 枚举与本表一一对应：`DETECT_THRESHOLD=0x03`、`CFAR_GUARD=0x21`、`SELF_TEST_TRIGGER=0x30`（见 `radar_protocol.py` 第 69、85、101 行），可自行对照确认契约一致。

#### 4.3.4 代码实践

**实践目标**：把 opcode 编号、Verilog `case` 分支、Python 枚举三者对齐，验证硬契约。

**操作步骤**：

1. 打开 [`radar_system_top.v` 的 case 表](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L950-L999)。
2. 打开 [`9_Firmware/9_3_GUI/radar_protocol.py`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py) 的 `Opcode` 枚举段（约第 66-101 行）。
3. 选三个 opcode（如 `0x01`、`0x23`、`0x30`），分别在两边找到它们：Verilog 侧写出目标 `host_*` 寄存器与所取位段，Python 侧写出枚举名与注释。
4. 用下表记录结果。

**需要观察的现象**：两边的编号必须**逐位相等**。如果发现某编号只在一边存在，说明契约破了（实际上本项目由跨层契约测试 u11-l3 自动守护）。

**预期结果（示例）**：

| opcode | Verilog 写入的寄存器 | 取值位段 | Python 枚举名 |
|--------|----------------------|----------|----------------|
| `0x01` | `host_radar_mode` | `value[1:0]` | `RADAR_MODE` |
| `0x23` | `host_cfar_alpha` | `value[7:0]` | `CFAR_ALPHA` |
| `0x30` | `host_self_test_trigger`（自清零脉冲） | 置 1 | `SELF_TEST_TRIGGER` |

> 本实践为静态对照型，无需编译运行。

#### 4.3.5 小练习与答案

**练习 1**：opcode `0x30` 命中后，`host_self_test_trigger` 会被置成 1，但下一拍它又变回 0，这是为什么？子模块如何捕捉到这次触发？

**参考答案**：因为 `always` 块的 `else` 分支里有一句 `host_self_test_trigger <= 1'b0;`（[radar_system_top.v:948](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L948)），每个时钟周期开头都把它清 0；只有 `case` 命中那一拍才被置 1，于是形成一个 1 周期宽的脉冲。`fpga_self_test` 子模块内部用边沿检测（或直接把该脉冲当作触发）捕捉到这个脉冲并启动自测试 FSM。

**练习 2**：如果主机把 `chirps_per_elev` 设成 16（而不是默认的 32），会发生什么？

**参考答案**：`case` 的 `0x15` 分支会判断：16 不为 0、也不大于 32，所以 `host_chirps_per_elev` 被正常写成 16；但因为 16 ≠ `DOPPLER_FRAME_CHIRPS`(32)，`chirps_mismatch_error` 会被置 1（[radar_system_top.v:973](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L973)），提醒主机：虽然写进去了，但 Doppler 累加（固定按 16 长 + 16 短组织）与每仰角 16 个 chirp 不匹配，结果可能错乱。这是一个「写入但告警」的防御性设计。

---

## 5. 综合实践

把本讲三个模块串起来，做一个「在 USB_MODE=1 下，追踪一条主机命令从进 USB 到改寄存器，再到被子模块使用」的端到端阅读任务。

**任务背景**：假设你是新加入的固件工程师，需求是「让主机能把 CFAR 的保护单元数从默认 2 改成 4」。你要确认整条链路是通的。

**操作步骤**：

1. **定位 USB 模块**：确认默认 `USB_MODE=1`（[radar_system_top.v:145](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L145)），所以生效的是 `gen_ft2232h` 分支里的 `usb_data_interface_ft2232h`（[radar_system_top.v:792-851](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L792-L851)）。它输出 `usb_cmd_opcode`/`usb_cmd_value`。
2. **追踪跨域**：跟着 `cmd_valid_toggle_ft601` → `cdc_single_bit` → `cmd_valid_100m` 的 toggle-CDC 路径（[radar_system_top.v:865-902](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L865-L902)），确认命令安全进入 100MHz 域。
3. **定位译码**：在 `case` 表里找到 `8'h21: host_cfar_guard <= usb_cmd_value[3:0];`（[radar_system_top.v:979](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L979)）。确认主机发 `value=0x0004` 时，`host_cfar_guard` 会被写成 `4'd4`。
4. **追踪到子模块**：`host_cfar_guard` 在 `cfar_inst` 例化里接到 `.cfg_guard_cells(host_cfar_guard)`（[radar_system_top.v:632](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L632)），真正消费它的是 `cfar_ca.v`（CFAR 算法在 u4-l5 详解）。
5. **（可选）跑回归确认无回归**：执行 `./9_Firmware/9_2_FPGA/run_regression.sh --quick`（若本地装了 iverilog），确认改阅读理解后 RTL 仍能通过 lint 与集成仿真。**待本地验证**：若环境无 iverilog，跳过此步，仅做静态阅读。

**预期交付**：一张包含 5 个节点（USB 模块 → CDC → case → host_cfar_guard → cfar_inst.cfg_guard_cells）的链路图，每个节点标注源码行号。这条链路就是「主机命令如何改变 FPGA 行为」的完整答案，也是后续做任何 opcode 扩展（见 u14-l2）的模板。

## 6. 本讲小结

- `radar_system_top.v` 是「接线员」而非「计算员」：它声明物理端口、用 `BUFG` 缓冲时钟、用 `ASYNC_REG` 同步复位，并例化发射机、接收机、CFAR、自测试、USB 五大子模块串成流水线。
- 顶层有三个主要时钟域入口：100MHz 系统域、120MHz DAC 域、USB 接口时钟域（FT601=100MHz / FT2232H=60MHz）；跨域信号（chirp 计数、帧脉冲、命令有效）都经过专门 CDC 处理，细节留待 u3-l2。
- `parameter USB_MODE` 用 `generate` 在编译期于 `usb_data_interface`（FT601）与 `usb_data_interface_ft2232h`（FT2232H）之间二选一，未命中分支的物理引脚被 tie-off 到安全电平。
- 主机 4 字节命令 `{opcode, addr, value}` 经 USB 模块拆解、再经 toggle-CDC 进 100MHz 域，最后由 `case(usb_cmd_opcode)` 译码写入对应 `host_*` 寄存器；opcode 编号与 Python `Opcode` 枚举构成硬契约。
- 关键 opcode：`0x03→host_detect_threshold`、`0x21→host_cfar_guard`、`0x30→host_self_test_trigger`；`0x15` 设 chirps_per_elev 时会被钳制到 `DOPPLER_FRAME_CHIRPS=32` 并可能置 `chirps_mismatch_error`。
- `radar_system_top_50t.v` 是 50T 量产板的物理包装层：以 `USB_MODE=1` 例化核心，用 `_nc` 悬空线与 `DONT_TOUCH` 属性保留无引脚的逻辑完整性。

## 7. 下一步学习建议

- **下一讲 u3-l2（时钟域、复位同步与 CDC 基础）**：本讲多次提到「toggle-CDC」「Gray 码多比特同步」「ASYNC_REG 复位同步」却没展开，u3-l2 会从原理到 `cdc_modules.v` 的实现一次讲透。建议紧接着读。
- **横向 U4 系列（FPGA 接收信号处理链）**：想看 `rx_inst` 内部 DDC→匹配滤波→MTI→Doppler→CFAR 的算法实现，按 u4-l1 到 u4-l5 顺序读。
- **横向 u6-l2（主机命令协议与 Opcode 映射）**：想看 Python 端 `build_command` 如何拼出本讲消费的 4 字节命令、状态包如何回读 `host_*`，读这篇。
- **进阶 u14-l2（二次开发扩展点）**：当你需要新增一个 opcode（FPGA `case` + Python 枚举 + GUI 控件 + 测试同步改动）时，这篇给出完整清单。

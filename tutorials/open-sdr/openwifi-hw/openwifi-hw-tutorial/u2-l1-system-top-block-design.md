# 顶层 system_top 与 block design

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清一个 Vivado（Xilinx）FPGA 工程里「顶层 wrapper」和「block design（BD）」分别是什么、各承担什么职责。
- 看懂 openwifi-hw 里 `system_top.v` → `system_wrapper.v` → `system`（block design）这三层「洋葱」式结构是如何把 FPGA 物理引脚、Zynq 处理系统（PS）和 openwifi 自定义 IP 串起来的。
- 读懂 `boards/openwifi.tcl` 如何根据板卡名（`BOARD_NAME`）选定目标器件（part）、把源码与约束加入工程、并把顶层模块设为 `system_top`，最终生成 `system_top.xsa` 硬件镜像。
- 知道约束文件 `system.xdc` 在整个工程里的作用（引脚约束 + 跨时钟域时序放宽）。

本讲是第 2 单元（顶层设计与系统集成）的第一篇，承接 u1-l4 的「构建脚本链路」：上一讲讲的是「脚本如何跑起来」，本讲讲的是「脚本最终组装出来的那个 SoC 顶层长什么样」。后续 u2-l2 会进入 `openwifi_ip` 内部，u2-l3 讲 PS-PL 互连。

## 2. 前置知识

在进入源码前，先用通俗语言建立几个概念。

### 2.1 PS 与 PL

Xilinx Zynq 这类 SoC 芯片内部有两部分：

- **PS（Processing System）**：一颗 ARM Cortex 处理器核，运行 Linux。它负责高层协议、驱动、用户态程序。在 openwifi 里，PS 跑的是 `openwifi` 软件仓库的驱动与 mac80211 协议栈。
- **PL（Programmable Logic）**：就是 FPGA 可编程逻辑，承载所有用 Verilog 写的硬件电路。openwifi-hw 的全部产物（xpu、tx_intf、rx_intf、openofdm_tx/rx、side_ch）都在 PL 侧。

两者之间通过总线（AXI）、DMA 和中断交换数据与控制信号。

### 2.2 什么是 block design（BD）

写 Verilog 可以用「例化模块 + 连线」的方式搭电路。Vivado 还提供了一种图形化方式：**IP Integrator（IPI）**，把一个个现成的 IP 核（如 Zynq PS、AXI DMA、AD9361 数据通路）拖到画布上像画电路图一样连起来，得到的就是一个 **block design**。它本质上和手写 Verilog 等价，但更适合搭建大型 SoC 顶层。

openwifi-hw 里 `system.bd` 就是这样一张「画布」，它把 Zynq PS、ADI 的 AD9361 射频数据通路、AXI DMA、中断拼接器，以及 openwifi 自己的 `openwifi_ip` 层级全部连成一个完整系统。

### 2.3 什么是顶层（top）模块

综合（synthesis）工具需要一个「入口模块」作为整个设计的根，它对外的端口就是 FPGA 芯片真实的物理引脚。这个根模块叫 **顶层模块（top module）**。在 openwifi-hw 里，顶层模块叫 `system_top`。

Vivado 里常用一种「洋葱式」分层：真正的物理引脚在最外层，一层层往里包，最里面才是 block design。本讲要讲的正是这种分层。

### 2.4 XDC 约束文件

FPGA 工具需要知道两件事，光有 Verilog 不够：

1. **某个逻辑信号对应芯片的哪个物理引脚**（引脚约束）。
2. **某些路径的时序要求**（时序约束），尤其是跨时钟域（CDC）的路径，常常要显式「放宽」告诉工具别去卡时序。

这些都写在 `.xdc`（Xilinx Design Constraints）文件里。`system.xdc` 就是 openwifi-hw 的约束文件之一。

## 3. 本讲源码地图

本讲以 `zc706_fmcs2`（Xilinx ZC706 板 + FMCOMMS2/3/4 射频子卡 + AD9361）这一板卡工程为例。涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `boards/zc706_fmcs2/src/system_top.v` | **综合顶层模块**。声明 FPGA 物理引脚，例化 I/O 缓冲与 `system_wrapper`。openwifi 在此处对引脚做重新映射。 |
| `boards/zc706_fmcs2/src/system_wrapper.v` | **block design 的 Verilog wrapper**，由 Vivado 自动生成。它例化 block design 主体 `system`。 |
| `boards/zc706_fmcs2/src/system.bd` | **block design 本体**（JSON 式文本）。里面是 PS、AD9361 通路、DMA、`openwifi_ip` 层级等所有 IP 的连接关系。 |
| `boards/zc706_fmcs2/src/system.xdc` | **约束文件**。openwifi 自定义的跨时钟域时序放宽。 |
| `boards/openwifi.tcl` | **顶层工程脚本**。所有板卡共用，负责创建 Vivado 工程、选器件、加源码与约束、综合实现、导出 `.xsa`。 |
| `boards/zc706_fmcs2/set_files.tcl` | 该板卡的源文件/约束/IP 仓路径清单（被 `openwifi.tcl` 读取）。 |
| `ip/parse_board_name.tcl` | 根据 `BOARD_NAME` 给出目标器件 `part_string`、板卡 `board_part_string`、规模标志 `fpga_size_flag` 等。 |

> 提示：`zc706_fmcs2` 只是众多板卡之一。其他板卡（`zcu102_fmcs2`、`adrv9361z7035` 等）的 `system_top.v`/`system.bd` 结构类似，只是物理引脚、器件型号不同。本讲读完后，你应当能举一反三去看任意一块板卡。

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：

- **4.1 顶层 wrapper**：讲 `system_top.v` 与 `system_wrapper.v` 的分层关系与引脚映射。
- **4.2 block design**：讲 `system.bd` 里面有哪些 IP、`openwifi.tcl` 如何把它建成一个可导出镜像的工程。

### 4.1 顶层 wrapper（system_top / system_wrapper）

#### 4.1.1 概念说明

openwifi-hw 的综合顶层是一个「三层洋葱」：

```
FPGA 物理引脚
   │
   ▼
system_top.v      ← 声明物理引脚；做引脚重映射（openwifi 在这里加料）；例化 I/O 缓冲
   │
   ▼
system_wrapper.v  ← Vivado 自动生成的 BD wrapper；例化 block design 主体
   │
   ▼
system (system.bd)← block design：PS + AD9361 通路 + DMA + openwifi_ip
```

为什么要分这么多层？

- **`system_top.v`**：是「人写的」最外层，把 FPGA 真实引脚（DDR、PS 的 MIO、AD9361 的 LVDS 差分数据线、SPI、GPIO）暴露出来。因为 ADI 参考设计里 GPIO/SPI 的排布和 openwifi 实际板卡连线不完全一致，openwifi 在这里做了一层**引脚重映射**（把 64 位内部 GPIO 总线的某些位映射到 `gpio_muxout_tx`、`spi_csn` 等具体引脚）。
- **`system_wrapper.v`**：是 Vivado 「自动生成」的，它只是把 block design 的 `system` 模块包了一层 Verilog 外壳，并补上 IIC 的 `IOBUF`。这层一般不改。
- **`system`（block design）**：真正的系统级连接图，承载了所有 IP。

> 区分：`system_top`（人写、含物理引脚） vs `system_wrapper`（自动生成、包 BD） vs `system`（BD 本体）。三者名字相近但层级不同，是初学者最容易混淆的点。

#### 4.1.2 核心流程

`system_top.v` 内部做三件事：

1. **声明物理引脚端口**：DDR、fixed_io（PS）、HDMI、IIC、AD9361 的 RX/TX LVDS 差分对、SPI、GPIO、TDD 同步等。
2. **例化 `ad_iobuf`（I/O 缓冲）**：把内部 64 位 GPIO 总线的输入/输出/三态信号，经过 IOBUF 原语接到真实引脚。同时把 AD9361 的 SPI、控制 GPIO（`enable`、`txnrx`、`tdd_sync` 等）做缓冲。
3. **例化 `system_wrapper`**：把上面整理好的信号连同 DDR/fixed_io/AD9361 数据线，原样接给 `system_wrapper`，再由它进入 block design。

数据/控制流向可以概括为：

```
AD9361 射频芯片 ──LVDS──> system_top 物理引脚 ──> system_wrapper ──> system.bd(util_ad9361_*) ──> openwifi_ip
PS(ARM) ──DDR/fixed_io──> system_top ──> system_wrapper ──> system.bd(sys_ps7) ──AXI/DMA──> openwifi_ip
```

#### 4.1.3 源码精读

**① 顶层端口列表（物理引脚）**

[boards/zc706_fmcs2/src/system_top.v:40-112](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/zc706_fmcs2/src/system_top.v#L40-L112) 声明了 `module system_top` 的全部物理引脚。可按功能分组阅读：

- DDR 相关（`ddr_*`）：连到 Zynq PS 的 DDR 控制器。
- `fixed_io_*`：PS 的固定引脚（MIO、PS 时钟/复位）。
- AD9361 接收 LVDS：`rx_clk_in_p/n`、`rx_frame_in_p/n`、`rx_data_in_p/n[5:0]`。
- AD9361 发射 LVDS：`tx_clk_out_p/n`、`tx_frame_out_p/n`、`tx_data_out_p/n[5:0]`。
- AD9361 控制：`enable`、`txnrx`、`tdd_sync`、`gpio_muxout_tx/rx`、`gpio_resetb`、`gpio_sync`、`gpio_en_agc`、`gpio_ctl[3:0]`、`gpio_status[7:0]`。
- SPI0（到 AD9361）：`spi_csn/clk/mosi/miso`。
- SPI1/UDC：`spi_udc_csn_tx`、`spi_udc_csn_rx`、`spi_udc_sclk`、`spi_udc_data`。

这一大段就是「FPGA 芯片对外的腿」。

**② 内部信号与 I/O 缓冲例化**

[boards/zc706_fmcs2/src/system_top.v:116-143](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/zc706_fmcs2/src/system_top.v#L116-L143) 定义了内部线网（`gpio_i/gpio_o/gpio_t` 各 64 位、`adc_*`/`dac_*` 数据线、`tdd_sync_*` 等）。随后：

[boards/zc706_fmcs2/src/system_top.v:147-173](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/zc706_fmcs2/src/system_top.v#L147-L173) 用 `ad_iobuf`（ADI 提供的 IOBUF 封装）把内部 GPIO 位与物理引脚一一对应。注意这处把 `gpio_muxout_tx`、`gpio_resetb`、`spi_*` 等具体引脚「钉」到了 GPIO 总线的特定位上——这正是 openwifi 相对 ADI 参考设计「加料」的地方。

**③ 例化 system_wrapper**

[boards/zc706_fmcs2/src/system_top.v:175-245](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/zc706_fmcs2/src/system_top.v#L175-L245) 例化 `system_wrapper i_system_wrapper`，把 DDR、fixed_io、`gpio_i/o/t`、AD9361 LVDS、SPI0/SPI1 全部接进去。其中两句尤其能体现「引脚重映射」的意图：

```verilog
.up_enable (gpio_o[47]),   // PS 通过 GPIO 第 47 位控制 AD9361 使能
.up_txnrx  (gpio_o[48]));  // PS 通过 GPIO 第 48 位控制收/发切换
```

也就是说，PS 侧软件写 GPIO 寄存器的第 47/48 位，就能开关 AD9361、切换收发——这层映射发生在 `system_top` 里。

**④ system_wrapper 例化 block design 主体**

[boards/zc706_fmcs2/src/system_wrapper.v:242-316](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/zc706_fmcs2/src/system_wrapper.v#L242-L316) 例化了 `system system_i`，这个 `system` 就是 `system.bd` 综合出来的模块。`system_wrapper.v` 本身的模块端口定义在 [system_wrapper.v:14-84](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/zc706_fmcs2/src/system_wrapper.v#L14-L84)，可以看到它和 `system_top` 的端口几乎一一对应（只是把 IIC 拆成 `_i/_o/_t` 三态），中间夹了一个 IIC 的 IOBUF（[system_wrapper.v:232-241](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/zc706_fmcs2/src/system_wrapper.v#L232-L241)）。这层是 Vivado 生成的，**通常不需要手动改**。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：建立「物理引脚 → 内部 GPIO 位 → system_wrapper」的对应直觉。
2. **操作步骤**：
   - 打开 [system_top.v:147-173](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/zc706_fmcs2/src/system_top.v#L147-L173)。
   - 对照第一个 `ad_iobuf` 的 `.dio_p(...)` 列表，把每个物理引脚对应的 GPIO 位号填出来（注释里已经写了，如 `// 50:50`、`// 46:46`）。
   - 再到 [system_top.v:175-245](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/zc706_fmcs2/src/system_top.v#L175-L245) 找到 `.spi0_csn_0_o (spi_csn)`、`.up_enable (gpio_o[47])` 等，确认 SPI0 片选、AD9361 使能分别走的是哪条线。
3. **需要观察的现象**：你会发现 AD9361 的「控制类」信号（使能、收发切换、片选）并不是直连，而是经过 GPIO 总线的某些位间接控制；而「数据类」信号（RX/TX LVDS）则是直连进 `system_wrapper`。
4. **预期结果**：能画出一张「物理引脚 ↔ 内部信号」对照表，至少覆盖 `enable`、`txnrx`、`spi_csn`、`rx_data_in_p` 四个引脚。
5. 运行环境：本任务为源码阅读，无需 Vivado。

#### 4.1.5 小练习与答案

**练习 1**：`system_top`、`system_wrapper`、`system` 三者谁是「人写的顶层」、谁是「自动生成的 BD wrapper」、谁是「block design 本体」？

> **答案**：`system_top` 是人写的综合顶层（含物理引脚与重映射）；`system_wrapper` 是 Vivado 自动生成的 BD Verilog wrapper；`system` 是 `system.bd` 综合出的本体模块，由 `system_wrapper` 例化。

**练习 2**：PS 软件想让 AD9361 切换到「发送」状态，从 `system_top.v` 看，它该操作 GPIO 总线的哪一位？

> **答案**：`gpio_o[48]`（`up_txnrx`）。PS 写 GPIO 输出寄存器的第 48 位，经 `system_top` 传到 `system_wrapper` 的 `up_txnrx`，再进入 BD 控制 AD9361。

**练习 3**：为什么 `system_wrapper.v` 一般不让人手改？

> **答案**：它是 Vivado 根据 `system.bd` 自动重新生成的，每次重新「生成目标（generate target）」都会覆盖；要改连接应该在 block design（`system.bd`）里改，而不是改这个 wrapper。

---

### 4.2 block design（system.bd + openwifi.tcl 工程）

#### 4.2.1 概念说明

`system.bd` 是一张「系统级电路图」。它把四类东西连在一起：

1. **Zynq PS**（`sys_ps7`）：ARM 核 + DDR 控制器 + 各类 PS 外设。它是整个系统的「大脑」，也是 openwifi 驱动运行的地方。
2. **AD9361 射频数据通路**（`util_ad9361_*` 一族，来自 adi-hdl）：负责把 AD9361 的 LVDS 数据拆包/打包、做时钟分频、TDD 同步。这是「射频 ↔ 基带」的桥梁。
3. **AXI 互连与 DMA**（`axi_interconnect_*`、`axi_dma_0`）：让 PS 能通过 AXI 总线配置寄存器、用 DMA 高速搬送收发数据包。
4. **openwifi_ip 层级**：把 openwifi 的六个自定义 IP（`xpu`、`tx_intf`、`rx_intf`、`openofdm_tx`、`openofdm_rx`、`side_ch`）打包成一个 block design 子层级，作为一个整体挂在 BD 上。

中断则通过 `sys_concat_intc`（中断拼接器）汇总后送进 PS。

> 本讲只看 BD 的「骨架」与它如何被工程脚本组装。`openwifi_ip` 内部六 IP 如何拼接是 u2-l2 的内容；PS-PL 的 AXI/DMA/中断细节是 u2-l3 的内容。

#### 4.2.2 核心流程

`system.bd` 本身是 Vivado 导出的文本（JSON 风格），人很难直接读。openwifi-hw 的可读入口其实是 **`boards/openwifi.tcl`**——它把 BD 和所有源码「组装」成一个可综合、可导出镜像的 Vivado 工程。其关键步骤如下：

1. **解析板卡名**：从当前目录名反推 `BOARD_NAME`，再 `source ip/parse_board_name.tcl` 得到目标器件 `part_string`、板卡 `board_part_string`、规模标志 `fpga_size_flag`。
2. **（再次）覆写基带时钟**：把 `NUM_CLK_PER_US` 等宏重新写入 `clock_speed.v`，并按 `fpga_size_flag` 决定是否定义 `SMALL_FPGA`。这是 u1-l4 提到的「基带时钟最终决定点」。
3. **创建工程并选器件**：`create_project ... -part $part_string`，工程名 `openwifi_$BOARD_NAME`。
4. **加入源码与约束**：`source set_files.tcl` 拿到该板卡的文件清单（`system_top.v`、`system_wrapper.v`、`system.bd`、`system.xdc`、ADI 自带约束等），用 `add_files` 加进去；并把综合/仿真顶层都设为 `system_top`。
5. **构建并导出**：`launch_runs impl_1 -to_step write_bitstream` 跑完综合+实现+生成比特流；最后 `write_hw_platform` 导出 `system_top.xsa`。
6. **BD 后处理**（`post_script_common.tcl`）：打开 BD，把 `util_ad9361_divclk` 的输出时钟设为 40MHz，并对各 openwifi IP 执行 `upgrade_ip`。

#### 4.2.3 源码精读

**① 解析板卡名 → 得到 part**

[boards/openwifi.tcl:16-18](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/openwifi.tcl#L16-L18) 从当前工作目录的末尾一段取出 `BOARD_NAME`（例如 `zc706_fmcs2`），然后加载板名解析脚本：

```tcl
set BOARD_NAME [lindex [split [exec pwd] /] end]
source ../../ip/parse_board_name.tcl
```

[ip/parse_board_name.tcl:19-24](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/parse_board_name.tcl#L19-L24) 给出 `zc706_fmcs2` 的器件信息：

```tcl
set part_string "xc7z045ffg900-2"   # Zynq 7045 FPGA
set fpga_size_flag 1                 # 大规模器件
```

这就是「目标器件 part 的来源」——它不在 `system_top.v` 里，而是由板卡名经这张映射表决定。

**② 覆写基带时钟宏**

[boards/openwifi.tcl:20-30](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/openwifi.tcl#L20-L30) 把 `NUM_CLK_PER_US=100`（对应 100MHz 基带时钟）写进 `clock_speed.v`，并按规模决定是否定义 `SMALL_FPGA`，再拷贝到 `tx_intf/rx_intf/xpu` 的 src 目录。这一步覆盖了 `ip_repo_gen.tcl` 先前的写入（脚本里注释 `# This overrides the value in ip_repo_gen.tcl!`）。

**③ 创建工程、选 part**

[boards/openwifi.tcl:45-46](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/openwifi.tcl#L45-L46) 定工程名 `openwifi_$BOARD_NAME`；

[boards/openwifi.tcl:106](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/openwifi.tcl#L106) 创建工程并把 part 设为 `parse_board_name.tcl` 给出的 `part_string`：

```tcl
create_project ${_xil_proj_name_} ./${_xil_proj_name_} -part $part_string
```

**④ 加源码、设顶层、加约束**

工程要加入哪些文件，由板卡目录下的 `set_files.tcl` 决定。[boards/zc706_fmcs2/set_files.tcl:7-18](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/zc706_fmcs2/set_files.tcl#L7-L18) 列出了：

- `adi-hdl/library/common/ad_iobuf.v`（I/O 缓冲原语）
- `src/system_wrapper.v`、`src/system.bd`、`src/system_top.v`
- 约束：ADI 的 `fmcomms2/zc706/system_constr.xdc`、`zc706_system_constr.xdc`，以及 openwifi 自己的 `src/system.xdc`。

[boards/openwifi.tcl:197](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/openwifi.tcl#L197) 把这些源文件加入 `sources_1`；[boards/openwifi.tcl:210](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/openwifi.tcl#L210) 与 [boards/openwifi.tcl:257](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/openwifi.tcl#L257) 把综合与仿真顶层都设为 `system_top`；[boards/openwifi.tcl:224](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/openwifi.tcl#L224) 加入约束文件。

**⑤ 构建并导出 .xsa**

[boards/openwifi.tcl:305-311](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/openwifi.tcl#L305-L311) 跑实现（到 `write_bitstream` 这一步）并导出硬件平台：

```tcl
launch_runs impl_1 -to_step write_bitstream -jobs 8
wait_on_run impl_1
write_hw_platform -fixed -include_bit -force -file ./openwifi_$BOARD_NAME/system_top.xsa
```

这个 `system_top.xsa`（含比特流）就是最终交给软件仓库（`openwifi`）消费的硬件镜像，名字正好来自顶层模块 `system_top`。

**⑥ 约束文件 system.xdc 的作用**

[boards/zc706_fmcs2/src/system.xdc:30-42](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/zc706_fmcs2/src/system.xdc#L30-L42) 是 openwifi 自加的约束，主要内容是给**射频（RF）时钟域与基带（BB）时钟域之间的跨时钟域路径**放宽时序。例如：

```tcl
# relax cross rf and bb domain control of dac_intf
set_max_delay 5 -datapath_only -from .../tx_iq_intf_i/csi_fuzzer_i/iq_out_reg[*]/C ...
set_false_path -through .../dac_intf_i/xpm_cdc_array_single_inst_*/syncstages_ff_reg[3][0]/C
```

这些路径上的信号本来就要经过 `xpm_cdc_*`（Xilinx 跨时钟域同步原语）做同步，所以工具不必卡单周期时序；`system.xdc` 用 `set_max_delay`/`set_false_path` 显式告诉工具「这条路径我已经处理过 CDC，放宽即可」。文件里被注释掉的段落（行 1-29）则是历史上调试时用过、现已关闭的约束。

> 注意区分三个 xdc：ADI 的 `system_constr.xdc`/`zc706_system_constr.xdc` 主要管引脚与 PS 时序；openwifi 的 `system.xdc` 专门管 `openwifi_ip` 内部的 CDC 时序放宽。本讲只看后者。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：把「板卡名 → 目标器件 → 顶层工程名 → 导出镜像名」这条链路走通。
2. **操作步骤**：
   - 在 [ip/parse_board_name.tcl](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/parse_board_name.tcl) 中找到 `zc706_fmcs2` 对应的 `part_string`、`fpga_size_flag`。
   - 在 [boards/openwifi.tcl](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/openwifi.tcl) 中找到：工程名如何拼（`openwifi_$BOARD_NAME`）、part 从哪个变量来（`-part $part_string`）、顶层模块设成什么（`system_top`）、导出文件叫什么（`system_top.xsa`）。
   - 在 [boards/zc706_fmcs2/src/system.xdc](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/zc706_fmcs2/src/system.xdc) 中找出至少两条 `set_false_path`/`set_max_delay`，说明它们针对的是哪两个时钟域。
3. **需要观察的现象**：你会发现 `part_string` 是「板卡名 → 器件型号」的一张固定映射表；换板卡只需改 `BOARD_NAME`，`openwifi.tcl` 完全不用动。
4. **预期结果**：能写出一张表，行为「板卡名 / part / fpga_size_flag / 工程名 / 顶层模块 / 导出镜像」，并填好 `zc706_fmcs2` 这一行。
5. 运行环境：源码阅读，无需 Vivado。若想真正跑 `openwifi.tcl`，需 Vivado 2022.2 + Vitis 并先完成 u1-l4 的依赖准备脚本（耗时较长）。

#### 4.2.5 小练习与答案

**练习 1**：`zc706_fmcs2` 工程的目标 FPGA 器件（part）是哪一个？这个值是写死在 `system_top.v` 里吗？

> **答案**：`xc7z045ffg900-2`（Zynq 7045）。它**没有**写死在 `system_top.v`，而是由 `parse_board_name.tcl` 根据 `BOARD_NAME` 给出 `part_string`，再在 `openwifi.tcl` 第 106 行 `create_project ... -part $part_string` 传入工程。

**练习 2**：`openwifi.tcl` 为什么要把顶层（top）设成 `system_top` 而不是 `system` 或 `system_wrapper`？

> **答案**：`system_top` 是唯一带物理引脚（DDR/AD9361/SPI…）的人写顶层；综合顶层必须是「对外就是芯片引脚」的模块。`system_wrapper` 虽含 BD，但 `system_top` 在其之上还做了引脚重映射与 I/O 缓冲，是更合适的综合根。

**练习 3**：`system.xdc` 里那些 `set_false_path -through .../xpm_cdc_array_single_inst_*/...` 想表达什么？

> **答案**：这些路径是 openwifi IP 内部跨「射频时钟域 ↔ 基带时钟域」的信号，已经用 Xilinx 的 `xpm_cdc_*` 同步原语处理过；用 `set_false_path`/`set_max_delay` 显式放宽，是告诉时序引擎不要再按单周期去卡这些 CDC 路径，避免误报时序违例。

## 5. 综合实践

**任务**：阅读 `system_top.v` 与 `openwifi.tcl`，完成下面这张「顶层工程档案表」（以 `zc706_fmcs2` 为例），并用一句话指出 `system.xdc` 的作用。

| 项目 | 答案 |
| --- | --- |
| 顶层模块名（top） | （填） |
| 目标器件 part | （填） |
| part 的来源（哪个文件/哪段） | （填） |
| Vivado 工程名 | （填） |
| 导出的硬件镜像文件名 | （填） |
| `system.xdc` 的作用 | （填） |

**参考填法**：

- 顶层模块名：`system_top`（见 [openwifi.tcl:210](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/openwifi.tcl#L210)）。
- 目标器件：`xc7z045ffg900-2`（见 [parse_board_name.tcl:19-24](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/parse_board_name.tcl#L19-L24)）。
- part 来源：`parse_board_name.tcl` 根据 `BOARD_NAME` 设置 `part_string`，在 [openwifi.tcl:106](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/openwifi.tcl#L106) 经 `create_project -part` 传入。
- 工程名：`openwifi_zc706_fmcs2`（`openwifi_$BOARD_NAME`，见 [openwifi.tcl:45](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/openwifi.tcl#L45)）。
- 导出镜像：`system_top.xsa`（见 [openwifi.tcl:311](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/openwifi.tcl#L311)）。
- `system.xdc` 作用：为 `openwifi_ip` 内部射频↔基带跨时钟域路径设置 `set_false_path`/`set_max_delay`，放宽已做 CDC 处理的路径时序（见 [system.xdc:30-42](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/zc706_fmcs2/src/system.xdc#L30-L42)）。

**进阶（可选）**：换一块板卡（如 `zcu102_fmcs2`）重做这张表，观察哪些项会变（part、`fpga_size_flag`、`ultra_scale_flag`）、哪些项不变（顶层模块名、镜像名规则）。这会帮你建立「同一套脚本跨板卡复用」的直觉。

## 6. 本讲小结

- openwifi-hw 的综合顶层是一个三层洋葱：**`system_top`（人写、物理引脚 + 重映射）→ `system_wrapper`（Vivado 自动生成的 BD wrapper）→ `system`（block design 本体）**。
- `system_top.v` 声明 FPGA 全部物理引脚（DDR、PS fixed_io、AD9361 LVDS、SPI、GPIO），并用 `ad_iobuf` 做 I/O 缓冲，把 AD9361 的控制信号映射到内部 64 位 GPIO 总线的特定位（如 `up_enable=gpio_o[47]`、`up_txnrx=gpio_o[48]`）。
- `system.bd` 是 IP Integrator 画布，把 Zynq PS、ADI 的 AD9361 数据通路、AXI DMA/互连、中断拼接器，以及 `openwifi_ip` 六 IP 层级连成完整 SoC。
- `openwifi.tcl` 是所有板卡共用的工程脚本：由目录名得 `BOARD_NAME` → `parse_board_name.tcl` 给出 `part_string` → `create_project -part` 建工程 → `set_files.tcl` 加源码与约束 → 顶层设为 `system_top` → 跑到 `write_bitstream` → 导出 `system_top.xsa`。
- 目标器件 part **不在** Verilog 里，而由「板卡名 → 器件型号」映射表决定，这是同套脚本跨板卡复用的关键。
- `system.xdc` 负责 `openwifi_ip` 内部射频↔基带跨时钟域路径的时序放宽（`set_false_path`/`set_max_delay`），与 ADI 自带的引脚/PS 约束分工不同。

## 7. 下一步学习建议

本讲只看了 BD 的「外壳」与工程如何组装。接下来建议：

- **u2-l2 openwifi_ip 层级**：进入 `ip/openwifi_ip.tcl`，看 xpu/tx_intf/rx_intf/openofdm_tx/openofdm_rx/side_ch 六个 IP 是如何拼成 `openwifi_ip` 这个 BD 子层级的，I/Q、控制、CSI 信号如何在它们之间流动。
- **u2-l3 PS-PL 互连**：深入 `connect_openwifi_ip.tcl`，看清 PS 与 PL 之间的 AXI 寄存器、AXI DMA（ACP/HP）、中断三类通路，把本讲里「PS 通过 GPIO/DMA 控制 PL」的直觉落到具体接线。
- **u2-l4 板级配置与时钟体系**：回到 `board_def.v` 与 `clock_speed.v`，系统理解 `NUM_CLK_PER_US`、`SAMPLING_RATE_MHZ`、`SMALL_FPGA` 等宏如何参数化整个设计。

如果你手头有 Vivado 2022.2 环境，可以在跑完 u1-l4 的依赖准备后实际执行一次 `openwifi.tcl`，在 GUI 里打开 `system.bd`，对照本讲的「三层洋葱」结构一一核对——这是把抽象概念坐实的最快方式。

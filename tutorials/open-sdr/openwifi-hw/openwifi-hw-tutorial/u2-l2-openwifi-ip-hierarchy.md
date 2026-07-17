# openwifi_ip 层级：六大 IP 如何拼接

## 1. 本讲目标

上一篇（u2-l1）我们看清了综合顶层 `system_top.v`、自动生成的 `system_wrapper.v` 和 block design（`system.bd`）这「三层洋葱」。本讲要钻进 block design 内部，聚焦其中最重要的一块拼图——**`openwifi_ip` 层级**。

学完本讲你应该能够：

- 说清 `openwifi_ip` 这个层级单元（hierarchical cell）里到底装了哪几个 IP、各自干什么。
- 看懂这些 IP 之间的 **I/Q 数据流、PHY 控制握手、CSI/状态信号** 是如何连起来的，能画出接收链路（ADC→rx_intf→openofdm_rx）和发射链路（tx_intf→openofdm_tx→DAC）。
- 理解 Vivado IP Integrator 里「**层级复用**」的思路：一个层级怎么被 `write_bd_tcl` 导出成脚本、再用 `source` 重新塞进另一个设计。

## 2. 前置知识

阅读本讲前，建议你已经了解上一篇建立的概念。这里再补充几个本讲会用到的术语：

- **IP（Intellectual Property）核**：在 FPGA 设计里可复用的、封装好的模块。它既可以是 Xilinx 官方提供的（如 `axi_dma`、`axi_interconnect`），也可以是用户自研的（本仓库的 `xpu`、`tx_intf` 等）。
- **block design（BD）/ IP Integrator**：Vivado 的图形化连线环境，用「块 + 连线」搭 SoC，而不是手写顶层 Verilog。
- **层级单元（hierarchical cell / hier block）**：BD 里把若干块打包成一个「大块」，对外只暴露少量引脚。`openwifi_ip` 就是这样一个层级单元——它把 openwifi 的 6 个自研 IP 藏在内部，对外只露出 AXI、ADC/DAC、中断等几组接口。这样做的好处是：顶层 BD 不会被几十根连线淹没，而且整个 `openwifi_ip` 可以作为一个整体被导出、迁移到别的工程。
- **AXI-Stream（AXIS）/ AXI4-Lite**：两种 AMAX 总线协议。AXIS 用来传「数据流」（一串连续样点或字节），AXI4-Lite 用来传「寄存器读写」（PS 配置/读取 FPGA 状态）。
- **VLNV**：Vivado 里标识一个 IP 的「身份证」，格式 `厂 商 : 库 : IP名 : 版本`，例如 `user.org:user:xpu:1.0` 表示这是 user.org 自研的 xpu IP 1.0 版。

> 提醒：本仓库自研 IP 的 VLNV 统一以 `user.org:user:` 开头，Xilinx 官方 IP 以 `xilinx.com:ip:` 开头。在 Tcl 里看到这两种前缀，就能立刻区分「这是 openwifi 自己的」还是「Xilinx 标准件」。

## 3. 本讲源码地图

本讲主要阅读以下文件：

| 文件 | 作用 |
| --- | --- |
| `ip/openwifi_ip.tcl` | 普通 Zynq-7000 平台的 `openwifi_ip` 层级脚本，定义过程 `create_hier_cell_openwifi_ip`，实例化 5 个自研 IP + 若干 Xilinx 基础设施 IP 并完成连线。 |
| `ip/openwifi_ip_ultra_scale.tcl` | UltraScale+ 平台（如 zcu102）的对应层级脚本，多了 `side_ch` 这个自研 IP，共 6 个自研 IP，连线也更丰富。 |
| `ip/board_def.v` | 跨 IP 共享的参数定义文件，被多个 IP 源码 `` `include ``，统一采样率等关键常数。 |
| `ip/connect_openwifi_ip.tcl` | （参考）把 `openwifi_ip` 层级接到 Zynq PS 的脚本，下一篇 u2-l3 会详讲，本讲只用它帮助理解层级「对外引脚」的去向。 |
| `README.md` | 「Migrate」一节给出了 `write_bd_tcl` / `source` 层级复用的官方做法。 |

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：

- **4.1 openwifi_ip 层级**：这个层级是什么、怎么被创建、对外暴露什么。
- **4.2 IP 互连**：六个 IP 内部怎么连，发射/接收两条数据链路怎么走。

---

### 4.1 openwifi_ip 层级

#### 4.1.1 概念说明

回忆 u1-l2：`ip/` 目录下有且仅有 **6 个自研 WiFi IP**——`xpu`、`tx_intf`、`rx_intf`、`openofdm_tx`、`openofdm_rx`、`side_ch`（README 明确给出白名单）。但它们在 block design 里不是各自孤立地散落着，而是被一个名为 **`openwifi_ip`** 的层级单元（hierarchical cell）整体打包在一起。

打个比方：如果把整个 SoC block design 比作一台电脑主板，那么 PS（ARM）是 CPU、AD9361 是网卡/射频卡，而 `openwifi_ip` 就是一块插在主板上的「WiFi 协处理器子卡」——上面焊着 6 颗 WiFi 专用芯片，对外只留出几组排针（AXI 总线、ADC/DAC 数据线、中断线）与主板相连。

这样设计有三个好处：

1. **降低顶层复杂度**：顶层 BD 只看到 `openwifi_ip` 一个大块，而不会被 6 个 IP 之间的几十根内部连线弄乱。
2. **可迁移**：整个层级可以作为一个整体被导出（`write_bd_tcl`）再塞进（`source`）另一个 ADI 参考设计（见 4.1.4）。
3. **关注点分离**：自研 WiFi 逻辑（`openwifi_ip` 内）与 ADI 射频数据通路（`openwifi_ip` 外）边界清晰。

> ⚠️ 一个容易被标题误导的点：本讲标题说「六大 IP」，指的是仓库 `ip/` 下的 **6 个自研 IP**。但具体到脚本，**`openwifi_ip.tcl`（普通 Zynq）里只实例化了其中 5 个（没有 `side_ch`）**，而 **`openwifi_ip_ultra_scale.tcl`（UltraScale+）里才实例化了全部 6 个（含 `side_ch`）**。`side_ch` 是侧信道/可观测性通路，是较新设计才加入的。下文会分别说明。

#### 4.1.2 核心流程

`openwifi_ip.tcl` 是一段 Vivado **生成的脚本**（文件开头写明 `This is a generated script based on design: system`）。它的核心是一个 Tcl 过程 `create_hier_cell_openwifi_ip`，流程是：

```text
create_hier_cell_openwifi_ip { parentCell nameHier }
  │
  ├─ 1. 校验 parentCell 存在且是 hier 类型
  ├─ 2. create_bd_cell -type hier openwifi_ip   （新建一个空层级单元）
  ├─ 3. create_bd_intf_pin / create_bd_pin       （为层级开「对外引脚」）
  ├─ 4. create_bd_cell ...（逐个实例化内部 IP）   （xpu/tx_intf/rx_intf/openofdm_tx/openofdm_rx + Xilinx IP）
  ├─ 5. connect_bd_intf_net / connect_bd_net      （把内部 IP 的引脚互相连起来）
  └─ 6. current_bd_instance 回到父级              （恢复上下文）
```

关键在于：**它定义的是「如何重建这个层级」，而不是立即执行**。真正要在某个 BD 里生成 `openwifi_ip` 时，是在该 BD 的上下文里调用 `create_hier_cell_openwifi_ip / openwifi_ip`（参数：父单元、层级名）。这也是 README 迁移文档里 `create_hier_cell_hier_mig / my_new_hierarchy` 的来历。

#### 4.1.3 源码精读

**① 这个脚本是怎么「自我介绍」的**

[ip/openwifi_ip.tcl:84-117](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L84-L117) 定义了过程签名与一堆校验。注意它首先要求父单元必须是 `hier` 类型——也就是说 `openwifi_ip` 必须建在另一个层级或顶层 BD 之内。

脚本开头还嵌入了它被导出时的 Vivado 版本：

```tcl
set scripts_vivado_version 2018.3   ;# openwifi_ip.tcl
```

而 UltraScale+ 版本是：

```tcl
set scripts_vivado_version 2022.2   ;# openwifi_ip_ultra_scale.tcl
```

这两个版本号本身就是判断「哪份脚本对应哪代平台」的线索（参见 [ip/openwifi_ip.tcl:23](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L23) 与 [ip/openwifi_ip_ultra_scale.tcl:23](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip_ultra_scale.tcl#L23)）。

**② 依赖检查清单：自研 5 个 vs 自研 6 个**

[ip/openwifi_ip.tcl:46-55](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L46-L55) 列出了本脚本需要的全部 IP（VLNV）。数一下 `user.org:user:` 开头的：`openofdm_rx`、`openofdm_tx`、`rx_intf`、`tx_intf`、`xpu`——**5 个**自研 IP，外加 Xilinx 的 `axi_dma`、`proc_sys_reset`、`xlslice`。

对比 [ip/openwifi_ip_ultra_scale.tcl:46-57](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip_ultra_scale.tcl#L46-L57)，清单里多了一行 `user.org:user:side_ch:1.0`——这就是第 6 个自研 IP，并多了 Xilinx 的 `xlconcat`。这是两份脚本最本质的差异。

**③ 对外引脚：层级露给外界的「排针」**

[ip/openwifi_ip.tcl:118-143](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L118-L143) 给层级开了一组对外引脚。它们是后续 connect_openwifi_ip.tcl 接到 PS 的「接口面」，可以分四类记：

| 类别 | 引脚 | 方向 | 含义 |
| --- | --- | --- | --- |
| AXI 总线 | `S00_AXI`（Slave）、`M00_AXI`、`M00_AXI1`（Master） | 见 u2-l3 | PS↔PL 寄存器控制、收发数据 DMA |
| 射频数据 | `adc_clk/adc_data/adc_valid/adc_rst`、`dac_data/dac_valid/dac_ready` | I/O | 与 AD9361 ADC/DAC 的样点通路 |
| TX 注入 | `dma_data/dma_valid/dma_ready` | I/O | 待发射数据从 PS 注入 |
| 中断/状态 | `rx_pkt_intr`、`mm2s_introut(1)`、`s2mm_introut(1)`、`tx_itrpt0/1`、`gpio_status` | 多为 O | 上报 PS 的中断与状态 |

> UltraScale+ 版本的对外引脚更多，还包含 `led0..led5`、`gpio_pmod1_*`、`spi_*`、`channel_switch` 等（见 [ip/openwifi_ip_ultra_scale.tcl:128-162](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip_ultra_scale.tcl#L128-L162)）——这些直接驱动板卡 LED/PMOD/SPI，所以放在层级内部、对外引出。

**④ 实例化：把 6 颗「芯片」摆上板**

普通版在 [ip/openwifi_ip.tcl:194-262](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L194-L262) 逐个 `create_bd_cell` 实例化：`openofdm_rx_0`、`openofdm_tx_0`、`rx_intf_0`、`tx_intf_0`、`xpu_0`，加上 Xilinx 的两片 `axi_dma`、三片 `axi_interconnect`、一片 `proc_sys_reset`（`sys_rstgen1`）和两片 `xlslice`。注意每个自研 IP 实例后都顺手把其 `s00_axi` 接口的 `NUM_READ/WRITE_OUTSTANDING` 限到 1（如 [ip/openwifi_ip.tcl:197-200](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L197-L200)），这是把 AXI4-Lite 从设备的并发能力收窄，保证时序稳定的常见做法。

UltraScale+ 版多实例化了 `side_ch_0`（[ip/openwifi_ip_ultra_scale.tcl:224-225](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip_ultra_scale.tcl#L224-L225)）和一片 `xlconcat_0`（把发射 I/Q 拼成一路送给 side_ch 观测）。

**⑤ 跨 IP 共享参数：`board_def.v`**

所有 IP 共享同一份基带参数。[ip/board_def.v:9-13](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/board_def.v#L9-L13) 定义了采样率与每采样时钟数：

```verilog
`define SAMPLING_RATE_MHZ       20
`define NUM_CLK_PER_SAMPLE     ((`NUM_CLK_PER_US)/`SAMPLING_RATE_MHZ)
```

其中 `NUM_CLK_PER_US`（基带时钟频率，单位 MHz）由构建脚本写入另一个文件 `clock_speed.v`（详见 u2-l4）。`board_def.v` 被 `rx_intf`、`tx_intf`、`xpu` 下的多个源码 `` `include "board_def.v" ``（例如 `ip/xpu/src/cca.v`、`ip/rx_intf/src/rx_iq_intf.v`、`ip/tx_intf/src/dac_intf.v` 等 8 处），从而保证六个 IP 对「一个采样点等于多少个时钟周期」达成共识。这正是它们能拼在一起协同工作的底层契约。

#### 4.1.4 代码实践：层级复用（write_bd_tcl / source）

**实践目标**：理解 `openwifi_ip.tcl` 作为「可导出/可重入」层级脚本的本质，验证 README 给出的迁移命令。

**操作步骤**（源码阅读型实践）：

1. 阅读 [README.md:171-183](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L171-L183) 的 *Method 2*。它给出三步：
   ```tcl
   write_bd_tcl -hier_blks [get_bd_cells /hier_mig] ./mig_hierarchy.tcl   ;# 1) 把某层级导出成脚本
   source ./mig_hierarchy.tcl                                             ;# 2) 在新工程里载入脚本
   create_hier_cell_hier_mig / my_new_hierarchy                           ;# 3) 调用过程生成层级
   ```
2. 对照本仓库：`ip/openwifi_ip.tcl` 本身就对应第 1 步产出的那种脚本——它由 `write_bd_tcl` 从某个历史 BD 导出，里面定义了过程 `create_hier_cell_openwifi_ip`。
3. 在 `ip/openwifi_ip.tcl` 里找到过程名（[ip/openwifi_ip.tcl:85](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L85)）和末尾打印可用过程的代码（[ip/openwifi_ip.tcl:374-383](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L374-L383)）。

**需要观察的现象**：脚本结尾的 `available_tcl_procs` 会打印 `create_hier_cell_openwifi_ip parentCell nameHier`——这正是「先 `source` 脚本，再调用此过程」的用法提示。

**预期结果**：你能用自己的话讲清「`openwifi_ip.tcl` 不是构建脚本，而是一份层级蓝图；只要 IP 仓里有这 6 个 IP，任何 ADI 参考设计 `source` 它并调用过程，就能长出一个一模一样的 `openwifi_ip` 层级」。

> 说明：本实践为源码阅读型，不需要安装 Vivado；若要真正跑 `write_bd_tcl`/`source`，需在已打开 openwifi 工程的 Vivado Tcl Console 中执行，具体现象「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么不把 6 个 IP 直接平铺在顶层 BD，而要套一个 `openwifi_ip` 层级？
**答案**：一是降噪——内部几十根连线被收进层级，顶层只暴露少量对外引脚；二是可迁移——整个层级可作为一个整体被 `write_bd_tcl` 导出再 `source` 进新设计；三是关注点分离——自研 WiFi 逻辑与 ADI 射频通路边界清晰。

**练习 2**：仅凭 VLNV 前缀，如何区分 `openwifi_ip.tcl` 里哪些是自研 IP、哪些是 Xilinx 标准件？
**答案**：`user.org:user:` 开头的是自研（xpu/tx_intf/rx_intf/openofdm_tx/openofdm_rx，UltraScale+ 版再加 side_ch），`xilinx.com:ip:` 开头的是 Xilinx 标准件（axi_dma、axi_interconnect、proc_sys_reset、xlslice、xlconcat）。

---

### 4.2 IP 互连

#### 4.2.1 概念说明

把 6 颗「芯片」摆上板之后，关键是用线把它们连起来。`openwifi_ip` 内部的连线可以归成四组：

1. **发射数据链路（TX）**：PS → DMA → `tx_intf`（缓存进 BRAM）→ `openofdm_tx`（生成基带 I/Q）→ `tx_intf` → DAC。
2. **接收数据链路（RX）**：ADC → `rx_intf`（出样点）→ `openofdm_rx`（解出字节/包）→ `rx_intf`（拼 AXI-Stream）→ DMA → PS。
3. **控制与状态（xpu 为中枢）**：`xpu` 像「交通指挥」，通过 `phy_tx_start`、`band/channel`、`mac_addr`、`tx_status`、`backoff_done` 等信号调度 `tx_intf`，通过 `byte_in`、`fcs_ok`、`pkt_header_valid`、`rssi_half_db`、`block_rx_dma_to_ps` 等信号消费 `openofdm_rx`/`rx_intf` 的结果。
4. **可观测性（仅 UltraScale+）**：`side_ch` 旁路挂接，采集 CSI、均衡器、RSSI、TX I/Q、各类状态机信号，再经独立 DMA 上报 PS。

> 名词小贴士：**CSI**（Channel State Information，信道状态信息）指 OFDM 各子载波的信道响应，是 Wi-Fi 感知/定位等研究的关键数据；**BRAM**（Block RAM）是 FPGA 片上内存，这里用来缓存待发射帧。

#### 4.2.2 核心流程

两条物理层数据链路可以浓缩成下面这张数据流向图（以普通版 `openwifi_ip.tcl` 为准，UltraScale+ 版在此基础上给 side_ch 多挂观测抽头）：

```text
==================== 发射链路 (TX) ====================
PS 内存 --(AXI-Stream)--> axi_dma_0(MM2S) --> tx_intf_0[s00_axis]
                                                 |
                              (写 BRAM) tx_intf_0[data_to_acc] --> openofdm_tx_0[bram_din]
                                           openofdm_tx_0[bram_addr] --> tx_intf_0[bram_addr]
                                                 |
                       openofdm_tx_0[result_i/q, result_iq_valid] --> tx_intf_0[rf_i/q_from_acc]
                                                 |
                                  tx_intf_0[dac_data/dac_valid] --> (层级对外 dac_data/dac_valid) --> AD9361 DAC

==================== 接收链路 (RX) ====================
AD9361 ADC --> (层级对外 adc_data/adc_valid) --> rx_intf_0[adc_data/adc_valid]
                                                 |
                       rx_intf_0[sample, sample_strobe] --> openofdm_rx_0[sample_in, sample_in_strobe]
                                                 |           (同一根 sample 还被 xlslice 切成 ddc_i/ddc_q 送 xpu)
                       openofdm_rx_0[byte_out, byte_out_strobe] --> rx_intf_0[byte_in, ...] 及 xpu_0[byte_in, ...]
                       openofdm_rx_0[fcs_ok, pkt_len, pkt_rate, pkt_header_valid ...] --> rx_intf_0 / xpu_0
                                                 |
                                  rx_intf_0[m00_axis] --> axi_dma_1(S2MM) --> (层级对外 M00_AXI) --> PS 内存

==================== 控制中枢 (xpu) ====================
xpu_0 <--> tx_intf_0  : phy_tx_start, phy_tx_started, phy_tx_done, band, channel, mac_addr,
                        tx_status, start_retrans, backoff_done, slice_en, cw ...
xpu_0 <--> rx_intf_0  : mute_adc_out_to_bb, block_rx_dma_to_ps(+_valid), rssi_half_db_lock ...
xpu_0 <--- openofdm_rx_0 / xlslice : byte_in, fcs_ok, rssi_half_db, ddc_i, ddc_q, demod_is_ongoing ...
```

发射握手的时序可以这样理解（伪代码）：

```text
when (xpu 决定发包):
    xpu -> tx_intf: phy_tx_start 等
    tx_intf -> openofdm_tx: phy_tx_start            # 启动基带生成
    openofdm_tx 读 BRAM(bram_addr/bram_din), 逐符号算出 result_i/q
    openofdm_tx -> tx_intf & xpu: phy_tx_started     # 表示波形已开始送出
    ...tx_intf 把 I/Q 推给 DAC...
    openofdm_tx -> tx_intf & xpu: phy_tx_done        # 本帧基带处理结束
```

#### 4.2.3 源码精读

下面把上面流向图里每条关键连线，对应到 `openwifi_ip.tcl` 的真实代码。

**① AXI 总线骨干：S00_AXI（控制）、M00_AXI/M00_AXI1（DMA）**

[ip/openwifi_ip.tcl:285-288](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L285-L288) 把对外 AXI 接口连到内部互连：

- `S00_AXI`（Slave，来自 PS `M_AXI_GP1`）→ `axi_interconnect_1`，再由它分出 M01~M06 六路 AXI4-Lite 分别接到 `tx_intf`/`openofdm_tx`/`rx_intf`/`openofdm_rx`/`xpu` 的 `s00_axi`（见 [ip/openwifi_ip.tcl:279-284](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L279-L284)）。这是「PS 配置/读取每个 IP 寄存器」的控制总线。
- `M00_AXI` → `axi_interconnect_2`（收方向 DMA 读内存）；`M00_AXI1` → `axi_interconnect_0`（发方向 DMA 读内存）。它们最终在 `connect_openwifi_ip.tcl` 里分别接 PS 的 `S_AXI_HP3` 与 `S_AXI_ACP`（u2-l3 详讲）。

**② 发射链路：DMA → tx_intf → openofdm_tx → DAC**

- DMA 送数到 tx_intf：[ip/openwifi_ip.tcl:272](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L272) `axi_dma_0/M_AXIS_MM2S → tx_intf_0/s00_axis`。
- tx_intf 把数据写给 openofdm_tx 的 BRAM：[ip/openwifi_ip.tcl:333](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L333) `tx_intf_0/data_to_acc → openofdm_tx_0/bram_din`；地址由 openofdm_tx 给出 [ip/openwifi_ip.tcl:311](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L311) `openofdm_tx_0/bram_addr → tx_intf_0/bram_addr`。
- openofdm_tx 算好的 I/Q 回送 tx_intf：[ip/openwifi_ip.tcl:312-314](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L312-L314) `result_i → rf_i_from_acc`、`result_q → rf_q_from_acc`、`result_iq_valid → rf_iq_valid_from_acc`。
- tx_intf 驱动 DAC：[ip/openwifi_ip.tcl:331-332](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L331-L332) `tx_intf_0/dac_data → dac_data`、`dac_valid → dac_valid`。
- 发射握手回传 xpu：[ip/openwifi_ip.tcl:322-323](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L322-L323) `phy_tx_done → tx_intf_0/tx_end_from_acc 与 xpu_0/phy_tx_done`；`phy_tx_started` 同理。

**③ 接收链路：ADC → rx_intf → openofdm_rx → DMA**

- ADC 样点进 rx_intf：[ip/openwifi_ip.tcl:293](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L293) `adc_data → rx_intf_0/adc_data`（[ip/openwifi_ip.tcl:295](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L295) `adc_valid`）。
- rx_intf 出样点喂 openofdm_rx：[ip/openwifi_ip.tcl:324-325](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L324-L325) `rx_intf_0/sample → openofdm_rx_0/sample_in`、`sample_strobe → sample_in_strobe`。
- 同一根 sample 顺便切片给 xpu 做 RSSI/CCA：[ip/openwifi_ip.tcl:324](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L324) `sample → xlslice_0/Din、xlslice_1/Din`，再 [ip/openwifi_ip.tcl:341-342](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L341-L342) `xlslice_0[31:16] → xpu_0/ddc_i`、`xlslice_1[15:0] → xpu_0/ddc_q`。
- openofdm_rx 解出的字节/状态广播给 rx_intf 与 xpu：例如 [ip/openwifi_ip.tcl:301](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L301) `byte_out → rx_intf_0/byte_in 与 xpu_0/byte_in`；[ip/openwifi_ip.tcl:304](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L304) `fcs_ok → rx_intf_0/fcs_ok 与 xpu_0/fcs_ok`；`pkt_len/pkt_rate/pkt_header_valid/byte_count` 同样一拖二（[ip/openwifi_ip.tcl:300](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L300), [307-310](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L307-L310)）。
- rx_intf 把有效帧拼成 AXI-Stream 送往 DMA：[ip/openwifi_ip.tcl:287](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L287) `rx_intf_0/m00_axis → axi_dma_1/S_AXIS_S2MM`。
- 收包中断：[ip/openwifi_ip.tcl:317](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L317) `rx_pkt_intr ← rx_intf_0/rx_pkt_intr`。

**④ UltraScale+ 版的 side_ch 抽头**

`openofdm_rx` 把研究用观测信号送给 `side_ch`：CSI 与均衡器在 [ip/openwifi_ip_ultra_scale.tcl:291-295](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip_ultra_scale.tcl#L291-L295)（`csi/csi_valid`、`equalizer/equalizer_valid`）；前导检测、相位等在 [ip/openwifi_ip_ultra_scale.tcl:302-312](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip_ultra_scale.tcl#L302-L312)。side_ch 还接收 xpu 的大量状态机信号（`tx_control_state`、`backoff_state`、`nav_state`、`retrans_in_progress`…，见 [ip/openwifi_ip_ultra_scale.tcl:368-411](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip_ultra_scale.tcl#L368-L411)）。它的数据用独立的 DMA 通路：配置流 [ip/openwifi_ip_ultra_scale.tcl:264](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip_ultra_scale.tcl#L264) `axi_dma_1/M_AXIS_MM2S → side_ch_0/s00_axis`，上报流 [ip/openwifi_ip_ultra_scale.tcl:278](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip_ultra_scale.tcl#L278) `side_ch_0/m00_axis → axi_dma_0/S_AXIS_S2MM`。

> 注意 UltraScale+ 版里两片 DMA 的角色和普通版不同：普通版「axi_dma_0 服务 TX、axi_dma_1 服务 RX」；UltraScale+ 版里 axi_dma_0 的 S2MM 被 side_ch 的上报流占用，axi_dma_1 的 MM2S 被 side_ch 的配置流占用——这是引入 side_ch 后对 DMA 带宽的重新分配。

#### 4.2.4 代码实践：绘制六大 IP 连接示意图

**实践目标**：依据 `openwifi_ip.tcl`（必要时参考 `openwifi_ip_ultra_scale.tcl`）亲手画出 6 个 IP 的互连图，标出收发两条数据链路方向。

**操作步骤**：

1. 准备一张白纸或绘图工具。先画 6 个方框：`xpu`、`tx_intf`、`rx_intf`、`openofdm_tx`、`openofdm_rx`、`side_ch`（普通版无 side_ch，标注「仅 UltraScale+」）。
2. 用本讲 4.2.2 的数据流向图为骨架，逐条到源码里核对：
   - 发射链：`tx_intf[data_to_acc]→openofdm_tx[bram_din]`、`openofdm_tx[result_i/q]→tx_intf[rf_i/q_from_acc]`、`tx_intf[dac_data]→(DAC)`。
   - 接收链：`(ADC)→rx_intf[adc_data]`、`rx_intf[sample]→openofdm_rx[sample_in]`、`openofdm_rx[byte_out]→rx_intf[byte_in]`、`rx_intf[m00_axis]→(DMA)`。
3. 在每条线上用箭头标出方向，并用不同颜色区分「数据流」「控制/握手」「观测抽头」。
4. 在 `xpu` 与 `tx_intf`/`rx_intf`/`openofdm_rx` 之间，至少标出 `phy_tx_start/started/done`、`byte_in`、`fcs_ok`、`rssi_half_db`、`block_rx_dma_to_ps` 这几根代表性控制线。

**需要观察的现象**：画完会发现 `xpu` 几乎和其余每个 IP 都有连线，是名副其实的「控制中枢」；而 `openofdm_rx` 的输出（`byte_out`、`fcs_ok`、`pkt_len`…）往往**一拖二**地同时送给 `rx_intf` 和 `xpu`。

**预期结果**：得到一张能解释「PS 发出的数据如何变成射频波形、空中收到的波形如何变回 PS 内存里的帧」的连接图，且 TX/RX 方向标注正确。若某些 side_ch 相关连线拿不准，可标注「待确认」并在 UltraScale+ 版核对。

> 说明：本实践为源码阅读+绘图型，无需运行 Vivado。

#### 4.2.5 小练习与答案

**练习 1**：`openofdm_rx` 的 `byte_out` 为什么同时连到 `rx_intf_0/byte_in` 和 `xpu_0/byte_in`？
**答案**：因为解出的字节流要被两个消费者使用——`rx_intf` 负责把字节拼成 64bit AXI-Stream 字并附上 FCS/序号送往 DMA（最终到 PS）；`xpu` 则需要实时解析 MAC 头部字段来做地址过滤、ACK 判断、重传等低层 MAC 决策。Tcl 里一根 `connect_bd_net` 把同一个源接到多个目的引脚，正是这种「广播」。

**练习 2**：`rx_intf_0/sample` 这根 64 位线被接到哪几个地方？为什么？
**答案**：它接到 `openofdm_rx_0/sample_in`（送 OFDM 接收机解调）、`xlslice_0/Din`、`xlslice_1/Din`（切片成 `ddc_i`/`ddc_q` 送 `xpu` 做 RSSI/CCA）。同一份基带样点既要做包解调，又要做能量/信道空闲评估，所以一分多路。

**练习 3**：普通版与 UltraScale+ 版在 DMA 用途上的最大区别是什么？
**答案**：普通版 axi_dma_0 专服务 TX、axi_dma_1 专服务 RX；UltraScale+ 版因引入 side_ch，axi_dma_0 的 S2MM 被 side_ch 上报流占用、axi_dma_1 的 MM2S 被 side_ch 配置流占用，DMA 带宽被重新分配以支撑可观测性数据。

---

## 5. 综合实践

**任务：为 `openwifi_ip` 层级补一份「对外接口契约表」+「内部数据流时序简述」。**

1. 通读 [ip/openwifi_ip.tcl:118-143](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L118-L143)，把层级所有对外引脚整理成一张表，列出：引脚名、方向（I/O）、位宽、所属类别（AXI/射频/TX 注入/中断/状态）、预测它会接到 PS 侧的哪个对象（参考 [ip/connect_openwifi_ip.tcl](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/connect_openwifi_ip.tcl)，如 `M00_AXI→S_AXI_ACP`、`rx_pkt_intr→sys_concat_intc/In1` 等）。
2. 用 100~200 字描述「一个下行 Wi-Fi 帧从 PS 内存到 DAC」经过的 IP 顺序与关键信号，再描述「一个上行帧从 ADC 到 PS 内存」的顺序。要求每一步都能在 `openwifi_ip.tcl` 中指出对应的 `connect_bd_*` 行号。
3. （加分项）对比 [ip/openwifi_ip_ultra_scale.tcl](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip_ultra_scale.tcl)，列出 UltraScale+ 版相对普通版新增的对外引脚（`led*`、`gpio_pmod1_*`、`spi_*`、`channel_switch` 等），并思考它们为什么从 `openwifi_ip` 内部引出而非放在更顶层。

这个任务把「层级是什么（4.1）」与「内部怎么连（4.2）」串起来，并为下一篇 u2-l3（PS-PL 互连）提前建立接口面认知。

## 6. 本讲小结

- `openwifi_ip` 是 block design 里的一个**层级单元**，把 openwifi 的自研 WiFi IP 整体打包，对外只暴露 AXI/ADC/DAC/中断等少量接口。
- 仓库 `ip/` 下共 **6 个自研 IP**；`openwifi_ip.tcl`（普通 Zynq）实例化其中 **5 个**，`openwifi_ip_ultra_scale.tcl`（UltraScale+）才实例化全部 **6 个**（含 `side_ch`）。
- **发射链路**：PS→DMA→`tx_intf`（写 BRAM）→`openofdm_tx`（生成 I/Q）→`tx_intf`→DAC；**接收链路**：ADC→`rx_intf`（出样点）→`openofdm_rx`（解字节）→`rx_intf`（拼 AXI-Stream）→DMA→PS。
- `xpu` 是控制中枢，与 `tx_intf`/`rx_intf`/`openofdm_rx` 都有握手/状态连线；`openofdm_rx` 的输出常「一拖二」同时给 `rx_intf` 与 `xpu`。
- `board_def.v` 是被多个 IP `` `include `` 的共享参数文件，约定采样率（20MHz）与每采样时钟数，是六个 IP 协同的底层契约。
- `openwifi_ip.tcl` 本质是 `write_bd_tcl` 导出的**层级蓝图**，可用 `source` + `create_hier_cell_openwifi_ip` 在任何含相同 IP 的设计里重建，这是跨板卡/跨 ADI 版本迁移的关键手法。

## 7. 下一步学习建议

- 下一篇 **u2-l3（PS-PL 互连：AXI、DMA 与中断）** 会从 `openwifi_ip` 的「对外引脚」继续向外，讲清这些 AXI/DMA/中断如何接到 Zynq PS 的 `M_AXI_GP1`、`S_AXI_ACP`、`S_AXI_HP3` 与 `sys_concat_intc`。建议带着本讲整理的「对外接口契约表」去读。
- 之后 **u2-l4（板级配置与时钟体系）** 会深入 `clock_speed.v`/`board_def.v` 里的 `NUM_CLK_PER_US`、`SAMPLING_RATE_MHZ` 等宏，解释本讲提到的「每采样时钟数」是如何随板卡时钟变化的。
- 想提前了解六大 IP 内部细节的读者，可先跳读 `ip/rx_intf/src/rx_intf.v`、`ip/tx_intf/src/tx_intf.v`、`ip/xpu/src/xpu.v` 的端口列表，对照本讲的连接图，看内部信号是否与层级对外引脚对得上。

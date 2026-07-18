# AXI 寄存器映射与软件交互

> 前置讲义：本讲承接 [u2-l3（PS-PL 互连：AXI、DMA 与中断）](u2-l3-ps-pl-axi-dma-intr.md) 与 [u5-l1（xpu 控制核心总览）](u5-l1-xpu-overview.md)。u2-l3 讲清了 PS 的 `M_AXI_GP1` 如何经 `axi_interconnect_1` 一拆七，分发出七组 AXI4-Lite 寄存器接口；u5-l1 点明 `xpu` 的 `slv_reg0~slv_reg63` 是软硬件契约。本讲回答下一个问题：**这一组组 `slv_reg` 在 RTL 里到底是怎么实现的？软件一次 `readl/writel` 又是怎样变成对某个寄存器的读写？**

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 AXI4-Lite 从设备「五通道握手」的工作方式，以及 openwifi 各 IP 用的那套 Xilinx 模板是如何把它简化为「单 outstanding、地址 + 数据同时就绪才写」的。
- 给定一个寄存器编号 N，算出它在字节地址空间里的偏移，并解释 `ADDR_LSB` 与 `OPT_MEM_ADDR_BITS` 在地址解码里的作用。
- 对照五个 `*_s_axi.v`，看出它们在地址位宽、寄存器数量、读写方向、附加输出（如 `slv_reg_wren_signal`、`axi_awaddr_core`）上的差异。
- 以 `xpu` 为例，把 `slv_reg0`（复位）、`slv_reg1`（过滤总闸）、`slv_reg9`（IFS/时隙覆盖）、`slv_reg57/58/59/62/63`（状态回读）等位域，与 `xpu.v` 里的真实用法一一对应。

## 2. 前置知识

- **主从（Master/Slave）**：AXI 是总线协议，PS（ARM）一侧是主，FPGA 里的 IP 一侧是从。主发起读写，从响应。
- **AXI4-Lite**：AXI 的轻量版，每个事务固定传 **32 位** 一个字，没有突发（burst）。它适合传「配置寄存器 / 状态寄存器」这类少量、按字访问的数据；要搬整包 Wi-Fi 数据则用 AXI-Stream + DMA（见 u2-l3）。
- **五个通道**：写地址（AW）、写数据（W）、写响应（B）、读地址（AR）、读数据（R）。每个通道都是一对 `VALID/READY` 握手信号：双方都拉高那一拍，数据才算被对方接收。
- **内存映射（memory-mapped）**：每个 IP 分到一段物理地址，IP 内部再把这段地址切成一个个 32 位「寄存器槽」。软件像访问内存一样 `readl/writel` 某个地址，硬件按地址选中对应槽。
- **字节选通（WSTRB）**：32 位 = 4 字节，`S_AXI_WSTRB[3:0]` 每一位对应一个字节是否要写。这样软件可以只改半个字而不破坏另一半。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `ip/xpu/src/xpu_s_axi.v` | xpu 的 AXI4-Lite 从设备，地址位宽 8、64 槽，本讲主范例 |
| `ip/tx_intf/src/tx_intf_s_axi.v` | tx_intf 从设备，地址位宽 7、32 槽，额外引出译码地址 |
| `ip/rx_intf/src/rx_intf_s_axi.v` | rx_intf 从设备，地址位宽 7、32 槽 |
| `ip/openofdm_tx/src/openofdm_tx_s_axi.v` | openofdm_tx 从设备，寄存器最少（写 3 个） |
| `ip/side_ch/src/side_ch_s_axi.v` | side_ch 从设备，含事件计数回读 |
| `ip/xpu/src/xpu.v` | xpu 顶层，消费 `slv_reg` 位域、驱动回读寄存器（给寄存器「语义」） |
| `ip/connect_openwifi_ip.tcl` | 把各 IP 的 `S00_AXI` 挂到 PS 的 `M_AXI_GP1`（见 u2-l3） |

## 4. 核心概念与源码讲解

### 4.1 AXI4-Lite 从设备寄存器组

#### 4.1.1 概念说明

openwifi 的每个自研 IP（`xpu`、`tx_intf`、`rx_intf`、`openofdm_tx`、`openofdm_rx`、`side_ch`）都带一个 `*_s_axi.v` 文件。它不是算法，而是一层**协议适配壳**：对外讲 AXI4-Lite 协议，对内暴露一堆 `slv_regN`（Slave Register N）给算法逻辑用。文件开头一句 `// based on Xilinx module template` 已点明——它们都由 Vivado「Create Peripheral」向导生成的模板裁剪而来，再按需注释掉用不到的寄存器槽。

它把寄存器分成两类：

- **软件可写（配置类）**：端口方向是 `output wire SLV_REGN`，模板内部 `slv_regN` 是 `reg`，由写通道赋值，再 `assign` 给算法逻辑。算法只读不写。
- **硬件可写、软件可读（状态回读类）**：端口方向是 `input wire SLV_REGN`，算法把状态驱动进来，模板用一个 `always` 把它采样进同名 `slv_regN`，再经读通道返回给软件。

这一壳子的价值在于：算法工程师只管把 `slv_reg9[13:7]` 当作「SIFS 时间」来用，完全不用关心一次 AXI 写事务的五个通道怎么握手。

#### 4.1.2 核心流程

模板采用**最保守、最易理解**的从设备风格——「**单 outstanding、无并发**」：任意时刻最多只接受一笔未完成的写或读。一次 32 位写事务的时序可以概括为：

```text
PS(Master)                          IP(Slave, *_s_axi)
   |  AWVALID + WVALID 同拍拉高          |
   |  ---------------------------------->|  aw_en=1 且 AWVALID&WVALID  →  拉一拍 AWREADY、WREADY
   |                                     |  锁存 axi_awaddr；slv_reg_wren=1，按地址写对应 slv_reg
   |                                     |  拉高 BVALID(BRESP=OKAY)
   |  BREADY --------------------------->|  完成；aw_en 复位，等下一笔
```

关键控制信号 `slv_reg_wren`（写使能）只在「AW 与 W 两个通道同时握手」那一拍为 1：

\[ \text{slv\_reg\_wren} = \text{axi\_wready}\ \&\ S\_AXI\_WVALID\ \&\ \text{axi\_awready}\ \&\ S\_AXI\_AWVALID \]

读事务更简单：AR 通道握手锁存地址，下一拍把 `reg_data_out`（组合逻辑按地址选出的寄存器值）打到 `axi_rdata`，并拉高 `RVALID`，等主设备的 `RREADY`。

#### 4.1.3 源码精读（以 xpu_s_axi.v 为主范例）

xpu 的从设备是五者里最大、也最完整的，地址位宽 8、64 个槽：

模块参数定义数据/地址位宽（[ip/xpu/src/xpu_s_axi.v:13-16](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu_s_axi.v#L13-L16)）——数据 32 位，地址 8 位（256 字节空间，64 个 32 位字）。

xpu 模板比其余四个多了一个 `aw_en` 互锁机制，用来严格保证「写响应未完成前不接受新写」（[ip/xpu/src/xpu_s_axi.v:307-335](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu_s_axi.v#L307-L335)）：复位后 `aw_en=1` 放行；一旦接受了写地址就清 0；等到 `BREADY&BVALID`（主设备取走写响应）才重新置 1。其余四个 IP 删掉了 `aw_en`，仅靠 `~axi_awready` 自锁，逻辑更短但功能等价（仍是单 outstanding）。

写使能与地址译码是写通道的内核（[ip/xpu/src/xpu_s_axi.v:392](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu_s_axi.v#L392) 与 [ip/xpu/src/xpu_s_axi.v:452-459](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu_s_axi.v#L452-L459)）：

```verilog
assign slv_reg_wren = axi_wready && S_AXI_WVALID && axi_awready && S_AXI_AWVALID;
...
case ( axi_awaddr[ADDR_LSB+OPT_MEM_ADDR_BITS:ADDR_LSB] )
  6'h00: for(byte_index=0; ...) if(S_AXI_WSTRB[byte_index])
           slv_reg0[(byte_index*8) +: 8] <= S_AXI_WDATA[(byte_index*8) +: 8];
  ...
```

这段做了三件事：① 只在写握手拍 `slv_reg_wren=1` 才写；② 用 `axi_awaddr` 的高位段选出寄存器号（地址解码，详见 4.2）；③ 按 `WSTRB` 逐字节使能，实现「字节级部分写」。

读通道是组合译码 + 一拍寄存输出（[ip/xpu/src/xpu_s_axi.v:942-1012](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu_s_axi.v#L942-L1012) 选数据，[ip/xpu/src/xpu_s_axi.v:1015-1031](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu_s_axi.v#L1015-L1031) 打一拍）：

```verilog
always @(*) case(axi_araddr[...]) 6'h00: reg_data_out <= slv_reg0; ... endcase
always @(posedge S_AXI_ACLK) if(slv_reg_rden) axi_rdata <= reg_data_out;
```

状态回读类寄存器在文件末尾「Add user logic here」区由用户逻辑驱动。xpu 把 `slv_reg57/58/59/62/63` 接成输入并每拍采样（[ip/xpu/src/xpu_s_axi.v:1066-1072](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu_s_axi.v#L1066-L1072)）：

```verilog
slv_reg57 <= SLV_REG57;  // 状态由 xpu.v 算法驱动进来，这里采样供软件读
```

#### 4.1.4 代码实践：追踪一次写寄存器的握手

**实践目标**：在 RTL 层面看清「软件写一次 `slv_reg0`」经历的完整握手。

**操作步骤**：

1. 打开 [ip/xpu/src/xpu_s_axi.v:392](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu_s_axi.v#L392)，确认 `slv_reg_wren` 的四个相与条件。
2. 设想软件执行 `writel(0x1, base + 0x00)`（写 `slv_reg0=1`，给某子模块软复位，见 4.3）。主设备会在同一拍拉高 `S_AXI_AWVALID`（地址 0x00）与 `S_AXI_WVALID`（数据 1，`WSTRB=0xF`）。
3. 顺着 `slv_reg_wren=1` → `case 6'h00` 命中 → `slv_reg0 <= 1` 走一遍（[ip/xpu/src/xpu_s_axi.v:453-459](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu_s_axi.v#L453-L459)）。
4. 再看写响应 [ip/xpu/src/xpu_s_axi.v:859-863](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu_s_axi.v#L859-L863)：握手拍 `axi_bvalid<=1, axi_bresp<=0`（OKAY），软件收到 B 通道响应，`writel` 返回。

**需要观察的现象**：`AWVALID&WVALID` 同拍出现 → 下一拍 `AWREADY/WREADY` 单拍脉冲 → `slv_reg0` 更新 → `BVALID` 拉高一拍。整个写事务在 2~3 个 `S_AXI_ACLK` 内闭合。

**预期结果**：`slv_reg0` 被稳定写入 0x1，且 `aw_en` 在 `BVALID` 期间保持 0、阻止新事务插入。

> 「待本地验证」：以上时序如需眼见为实，可在 Vivado 中给 xpu 单 IP 建工程，用 AXI VIP 当主设备跑一次写，或综合后用 ILA 抓 AW/W/B 通道（启用方法见 u7-l6）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `slv_reg_wren` 必须同时包含 `axi_awready && S_AXI_AWVALID` 与 `axi_wready && S_AXI_WVALID` 两对信号？
**答**：AXI4-Lite 的写事务要求「写地址（AW）与写数据（W）都有效且都被从设备接受」才算一次完整写。只看一路会漏掉另一路未就绪的情况，可能把无效数据写进寄存器。

**练习 2**：xpu 模板里的 `aw_en` 互锁（[L307-335](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu_s_axi.v#L307-L335)）解决了什么问题？
**答**：保证「上一笔写响应 `BVALID` 还没被主设备取走（`BREADY`）之前，不接受下一笔写地址」，避免两笔写事务在内部 `slv_reg` 写逻辑里重叠，是单 outstanding 风格的兜底。

### 4.2 寄存器映射与地址解码

#### 4.2.1 概念说明

「寄存器映射」回答的是：**软件给出的字节地址，落到哪个 `slv_regN`？** 这由两步决定：

1. **IP 基地址**：由 block design 的地址编辑器给每个 IP 分配一段 4KB（或更小）地址窗口，例如 `xpu` 在 `M_AXI_GP1` 视图下的某段基地址（具体值由 Vivado 在 `system.bd` 的 Address Editor 里确定，不在本仓库源码里写死）。
2. **IP 内偏移**：本 IP 内部把这段窗口按字切成槽，槽号 N 对应字节偏移。

模板用两个 `localparam` 控制切片：

- `ADDR_LSB = C_S_AXI_DATA_WIDTH/32 + 1`：32 位数据时为 **2**，即字节地址低 2 位是「字内字节号」，交给 `WSTRB` 处理。
- `OPT_MEM_ADDR_BITS`：参与译码的地址位数。

寄存器号与字节地址的关系为：

\[ \text{index} = \text{addr}[\,\text{ADDR\_LSB}+\text{OPT\_MEM\_ADDR\_BITS} : \text{ADDR\_LSB}\,],\qquad \text{byte\_offset}(N) = N \times 4 \]

#### 4.2.2 核心流程

地址译码在写、读两条路径上各做一次，用同一段 `case`：写路径按 `axi_awaddr` 选要写哪个 `slv_reg`，读路径按 `axi_araddr` 选要返回哪个 `slv_reg`。模板为 64（或 32）个槽都生成了一个 `case` 分支，开发者按需把用不到的槽**注释掉**——既节省寄存器资源，也让「哪些寄存器真实存在」一目了然。

读路径的 `default` 分支返回 0（[ip/xpu/src/xpu_s_axi.v:1010](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu_s_axi.v#L1010)），所以读一个「未实现」的槽不会卡总线，只会读到 0。

#### 4.2.3 源码精读

地址位宽与译码位宽的定义（[ip/xpu/src/xpu_s_axi.v:167-168](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu_s_axi.v#L167-L168)）：

```verilog
localparam integer ADDR_LSB = (C_S_AXI_DATA_WIDTH/32) + 1;   // = 2
localparam integer OPT_MEM_ADDR_BITS = 5;                     // xpu 用 5 位 → 64 槽
```

于是 xpu 的索引字段是 `axi_awaddr[7:2]`（[ip/xpu/src/xpu_s_axi.v:452](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu_s_axi.v#L452)），64 个槽，地址窗口 256 字节。其余四个 IP 的 `C_S_AXI_ADDR_WIDTH=7`、`OPT_MEM_ADDR_BITS=4`（例如 [ip/rx_intf/src/rx_intf_s_axi.v:134-135](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf_s_axi.v#L134-L135)、[ip/tx_intf/src/tx_intf_s_axi.v:140-141](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_intf_s_axi.v#L140-L141)、[ip/side_ch/src/side_ch_s_axi.v:136-137](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch_s_axi.v#L136-L137)、[ip/openofdm_tx/src/openofdm_tx_s_axi.v:133-134](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/openofdm_tx_s_axi.v#L133-L134)），索引字段是 `addr[6:2]`，32 个槽，窗口 128 字节。

下面这张表把五个从设备的映射规模与方向整理在一起（按各文件端口区与 `case` 实际**未注释**的槽统计）：

| 从设备 | ADDR_WIDTH | OPT_MEM_ADDR_BITS | 译码字段 | 总槽 | 软件可写槽（→算法） | 状态回读槽（算法→） |
|--------|:---:|:---:|:---:|:---:|---|---|
| `xpu_s_axi.v` | 8 | 5 | `addr[7:2]` | 64 | 0-13, 16-22, 26-31（共 27） | 57,58,59,62,63 |
| `tx_intf_s_axi.v` | 7 | 4 | `addr[6:2]` | 32 | 0-2, 4-17 | 21,22,23,24,25,26 |
| `rx_intf_s_axi.v` | 7 | 4 | `addr[6:2]` | 32 | 0-13, 16 | 31 |
| `openofdm_tx_s_axi.v` | 7 | 4 | `addr[6:2]` | 32 | 0,1,2 | 20 |
| `side_ch_s_axi.v` | 7 | 4 | `addr[6:2]` | 32 | 0-12, 19 | 20,21,22, 26-31 |

几个值得注意的差异：

- **`tx_intf` 与 `side_ch` 把译码地址也引出**了：`tx_intf_s_axi.v` 额外输出 `axi_awaddr_core[4:0]`、`axi_araddr_core[4:0]`、`slv_reg_rden`、`slv_reg_wren_delay`（[ip/tx_intf/src/tx_intf_s_axi.v:19-23](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_intf_s_axi.v#L19-L23) 与 [ip/tx_intf/src/tx_intf_s_axi.v:298](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_intf_s_axi.v#L298)、[L692](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_intf_s_axi.v#L692)）。这是因为 `tx_intf` 用了一个共享 BRAM，软件既要写控制寄存器、又要按地址写包数据，索性把「带读/写脉冲的地址总线」整体交给算法侧去复用。`side_ch` 同理（[ip/side_ch/src/side_ch_s_axi.v:19-20](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch_s_axi.v#L19-L20)、[L294](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch_s_axi.v#L294)）。
- **写脉冲对外输出**：`xpu_s_axi.v`（[L20](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu_s_axi.v#L20)、[L449](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu_s_axi.v#L449)）与 `side_ch_s_axi.v`（[L19](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch_s_axi.v#L19)、[L323](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch_s_axi.v#L323)）输出 `slv_reg_wren_signal`（比 `slv_reg_wren` 晚一拍），供算法做「写某寄存器就触发一次动作」的边沿检测（典型如 side_ch 写 `slv_reg2` 触发一次 DMA）。
- **`openofdm_tx_s_axi.v` 顶部 `include "openofdm_tx_pre_def.v"`**（[L1](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/openofdm_tx_s_axi.v#L1)），是它独享的条件编译宏入口（见 u7-l2）。

> 注意：上表中「状态回读槽」在模板里仍写成 `case ... reg_data_out <= slv_regN`，但这些 `slv_regN` 不再由写通道赋值，而是由文件末尾「user logic」区从输入端口采样（如 rx_intf 的 [L773](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf_s_axi.v#L773) `slv_reg31 <= SLV_REG31`）。软件读它们得到的是硬件实时状态。

#### 4.2.4 代码实践：为 xpu 列出寄存器地址偏移表

**实践目标**：把 xpu 的 `slv_reg` 编号换算成字节偏移，做一张软件可直接用的「寄存器速查表」。

**操作步骤**：

1. 由 4.2.1 公式，xpu 的 `byte_offset(N) = N*4`（`ADDR_LSB=2`，字内字节由 `WSTRB` 管）。
2. 遍历 [ip/xpu/src/xpu_s_axi.v:20-84](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu_s_axi.v#L20-L84)（端口区，区分 `output`=可写、`input`=回读）与 [ip/xpu/src/xpu_s_axi.v:452-676](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu_s_axi.v#L452-L676)（写 `case`，确认哪些槽真被赋值，没注释的才有效）。
3. 填出下表（节选）。

**预期结果（节选）**：

| 寄存器 | 偏移 | 方向 | 简述（语义见 4.3） |
|---|---|---|---|
| slv_reg0 | 0x00 | 写 | 各子模块软复位（按位） |
| slv_reg1 | 0x04 | 写 | 过滤源选择 / RX DMA 总闸 |
| slv_reg2 | 0x08 | 写 | TSF 装载值低 32 位 |
| slv_reg3 | 0x0C | 写 | TSF 装载值高位，bit[31] 上升沿触发装载 |
| slv_reg6 | 0x18 | 写 | NAV/DIFS/EIFS/CW 使能、解码后等待时长 |
| slv_reg9 | 0x24 | 写 | IFS/时隙覆盖（bit[31]=1 时生效） |
| slv_reg13 | 0x34 | 写 | SPI 控制禁止 bit[0] |
| slv_reg22 | 0x58 | 写 | 队列 slice 计数终止/选择 |
| slv_reg57 | 0xE4 | 读 | RSSI/CCA/收发状态聚合 |
| slv_reg58 | 0xE8 | 读 | TSF 计时器低 32 位 |
| slv_reg59 | 0xEC | 读 | TSF 计时器高 32 位 |
| slv_reg62 | 0xF8 | 读 | MAC 地址回读（字节序判断） |
| slv_reg63 | 0xFC | 读 | FPGA 版本号 |

（完整 27 个可写槽 + 5 个回读槽，按同样方法补全。空缺槽如 14/15/23/24/25/32-56/60/61 读到 0。）

**需要观察的现象**：地址呈 4 字节步进；`slv_reg63` 落在 0xFC，正好是 256 字节窗口的最后一个字。

#### 4.2.5 小练习与答案

**练习 1**：若把 `OPT_MEM_ADDR_BITS` 从 5 改成 4（xpu 仍保持 8 位地址总线），会发生什么？
**答**：译码字段从 `addr[7:2]` 变成 `addr[6:2]`，最高位 `addr[7]` 不参与译码，0x00~0xFC 与 0x100~0x1FC 会「镜像」到同一组 32 个寄存器，造成地址别名。所以 `OPT_MEM_ADDR_BITS` 必须与寄存器数量匹配。

**练习 2**：软件读 xpu 偏移 `0x10`（即 `slv_reg4`）会得到什么？
**答**：`slv_reg4` 在写 `case` 里有效（[L481-487](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu_s_axi.v#L481-L487)），是普通可写配置寄存器，读回的是软件最后一次写入的值（写后可回读）。若读 `0x38`（`slv_reg14`，被注释），`case` 落 `default`，读回 0。

### 4.3 寄存器语义：软件与 FPGA 的交互契约

#### 4.3.1 概念说明

地址映射只说清了「哪个地址对应哪个 `slv_reg`」，但一个 32 位寄存器里每一位**代表什么**，是算法决定的、写死在 `xpu.v` 等顶层里的。这部分就是「软硬件交互契约」——驱动（openwifi 软件仓库）按这个契约组织位域来读写，FPGA 按这个契约解释每一位。`*_s_axi.v` 只是搬运工，真正的语义在 `xpu.v` 里。

#### 4.3.2 核心流程

寄存器语义在 `xpu.v` 中体现为三类用法：

1. **按位软复位**：把 `slv_reg0` 的不同位接到不同子模块的 `rstn`，软件写某位为 1 就单独复位那个子模块。
2. **参数配置**：把 `slv_reg` 切成若干位域，喂给算法（如时隙时间、门限、使能开关）。
3. **状态回读**：`xpu.v` 用组合/时序逻辑把内部状态拼成 32 位，驱动到 `slv_reg57~63` 的输入端口，供软件读。

#### 4.3.3 源码精读

寄存器声明本身带注释，是最好的语义索引（[ip/xpu/src/xpu.v:159-223](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L159-L223)），例如：

```verilog
wire [...] slv_reg0; // rst
wire [...] slv_reg1; // some source selection
wire [...] slv_reg3; // tsf load value high (the rising edge of msb will trigger loading)
wire [...] slv_reg9; // xIFS and slot time override for debug
wire [...] slv_reg57;//temp reg for rssi readback during idle...
wire [...] slv_reg63;//FPGA version info
```

**① 按位软复位**：`slv_reg0` 的每一位对应一个子模块（[ip/xpu/src/xpu.v:427](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L427)、[L449](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L449)、[L528](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L528)、[L615](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L615)、[L701](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L701)、[L726](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L726)）：

```verilog
.rstn(s00_axi_aresetn&(~slv_reg0[0]))   // 复位子模块0
.rstn(s00_axi_aresetn&(~slv_reg0[6]))   // 复位另一组
```

含义：硬件复位 `s00_axi_aresetn` 与「软件写 1 即复位」的 `slv_reg0[k]` 合成。这正对应 u5-1 提到的「软件写 `slv_reg0` 各位可单独复位某子模块」。

**② RX DMA 过滤总闸**：`slv_reg1[2]` 是软件对硬件 MAC 过滤的最终否决权（[ip/xpu/src/xpu.v:349](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L349)）：

```verilog
assign block_rx_dma_to_ps = (block_rx_dma_to_ps_internal & (~slv_reg1[2]));
```

软件把 `slv_reg1[2]` 写 1，则无论硬件地址过滤结果如何都强制放行到 PS（promiscuous/monitor 模式）。这正是 u5-4「`slv_reg1[2]` 软件总闸」的来源。

**③ IFS/时隙覆盖**：`slv_reg9` 用 bit[31] 当使能开关，覆盖默认的 DCF 时序参数（[ip/xpu/src/xpu.v:331-335](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L331-L335)）：

```verilog
assign slot_time = (slv_reg9[31]? slv_reg9[18:14] : (band==1?(erp_short_slot?9:20):9));
assign sifs_time = (slv_reg9[31]? slv_reg9[13:7]  : (band==1?10:16));
```

默认 `slv_reg9[31]=0` 时用标准值（5GHz SIFS=16µs，2.4GHz SIFS=10µs）；调试时软件写 `slv_reg9[31]=1` 即可手动注入时隙，供 CSMA/CA（u5-2）实验。

**④ 状态回读**：`xpu.v` 把多个内部信号拼进一个回读字。`slv_reg57` 聚合了 RSSI、CCA、收发状态等（[ip/xpu/src/xpu.v:370](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L370)）；`slv_reg58/59` 给出 64 位 TSF 计时器（[L375-376](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L375-L376)）；`slv_reg63` 直接绑到构建期宏 `OPENWIFI_HW_GIT_REV`（[L325](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L325)）——软件一上电读 `slv_reg63` 就知道跑的是哪个版本的 bitstream。

> 一个回读寄存器塞多个字段（如 `slv_reg57`）是 openwifi 的常见做法；`xpu.v` 注释里也坦言「将来宜用 sdpram 按地址选择回读多路信息」。

#### 4.3.4 代码实践：设计一次「读状态 + 改配置」的软件交互

**实践目标**：用本讲的寄存器映射，把一次典型的软件交互翻译成具体的寄存器读写序列（纯源码阅读型，不要求上板）。

**操作步骤**：

1. **读版本号**：软件启动时读 xpu 基地址 + `0xFC`（`slv_reg63`）。对照 [ip/xpu/src/xpu.v:325](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L325)，返回值即 `OPENWIFI_HW_GIT_REV`，与驱动期望版本比对。
2. **单独复位 csma_ca**：假设要复位 CSMA/CA 子模块。在 [ip/xpu/src/xpu.v:427](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L427) 附近找到该子模块用的是 `slv_reg0[0]`（示例），则软件对 `base+0x00` 写 `0x1`、延时几拍、再写 `0x0` 完成一次脉冲复位。
3. **打开 monitor 模式**：对 `base+0x04`（`slv_reg1`）的 bit[2] 置 1。参照 [ip/xpu/src/xpu.v:349](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L349)，此时 `block_rx_dma_to_ps=0`，所有收到的帧（不论地址）都上报 PS。
4. **轮询 RSSI/CCA**：循环读 `base+0xE4`（`slv_reg57`），按 [L370](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L370) 的位拼接解析：低位是 `rssi_half_db`、某位是 `~ch_idle_final` 等，用于实时观测信道占用。

**需要观察的现象**：步骤 2 写脉冲期间，对应子模块内部寄存器被清零；步骤 3 打开后，PS 侧收到的包不再被硬件地址过滤丢弃；步骤 4 读数会随真实信号强度变化。

**预期结果**：四步全部可由 `readl/writel(base+offset)` 完成，每一步的「offset」都能在 4.2.4 的表里查到、「含义」都能在 `xpu.v` 里找到对应行——这就是「寄存器是软硬件契约」的完整闭环。

> 「待本地验证」：实际偏移取决于 `system.bd` 里 xpu 的基地址，需在 Vivado Address Editor 或设备树中确认；语义位域以本仓库当前 HEAD 的 `xpu.v` 为准。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `slv_reg63`（版本号）放在「状态回读」区而不是「可写配置」区？
**答**：它是构建期固化的常量 `OPENWIFI_HW_GIT_REV`（[xpu.v:325](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L325)），由 FPGA 驱动、软件只读。若放可写区，软件误写会覆盖版本信息。

**练习 2**：`slv_reg9[31]` 这个「使能位」设计（[xpu.v:331-335](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L331-L335)）相比「单独一个 enable 寄存器」有什么好处？
**答**：把「是否覆盖」与「覆盖成什么值」放进同一个字，软件一次 32 位写即可原子地切换调试时序，避免出现「先写了新时隙值、还来不及开使能，硬件已经用了半新半旧的值」的中间态。

## 5. 综合实践

**任务**：给 openwifi 的 xpu 做一张「面向驱动开发者的寄存器速查手册」。

要求：

1. 从 [xpu_s_axi.v 的端口区](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu_s_axi.v#L20-L84) 判定每个 `slv_regN` 是「可写」还是「回读」，剔除被注释掉的槽。
2. 用 `byte_offset = N*4` 算出全部偏移，列出 0x00 ~ 0xFC 的完整表（标出空缺槽）。
3. 对每个真实存在的寄存器，回到 [xpu.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L159-L223) 找到它被引用的行，写出每个位域的含义（复位位、使能位、数值位域……）。
4. 为其中 3 个寄存器（建议选 `slv_reg0`、`slv_reg1`、`slv_reg9`）各写一段「C 语言伪代码」，演示驱动应当如何 `readl/writel` 来完成「复位某子模块 / 打开 monitor / 临时改 SIFS」。

这个任务把本讲的三个最小模块串起来：从 AXI4-Lite 壳（4.1）→ 地址映射（4.2）→ 语义契约（4.3），最终产出一份与 openwifi 驱动（软件仓库）对接所需的硬件接口文档。

## 6. 本讲小结

- 每个 openwifi IP 都有一个 `*_s_axi.v`，是基于 Xilinx 模板裁剪的 **AXI4-Lite 从设备壳**：对外讲五通道握手，对内暴露一组 `slv_regN`；它本身不含算法。
- 模板用「单 outstanding」风格：AW 与 W 同拍握手才产生一拍 `slv_reg_wren` 写脉冲；读路径用组合译码 + 一拍寄存输出。xpu 版多了 `aw_en` 互锁，更严格。
- 寄存器号由 `addr[ADDR_LSB+OPT_MEM_ADDR_BITS : ADDR_LSB]` 选出；32 位数据下 `byte_offset(N)=N*4`。xpu 是 8 位地址/64 槽，其余四 IP 是 7 位地址/32 槽，且按需注释掉用不到的槽（读未实现槽返回 0）。
- 五个从设备在「地址位宽 / 可写槽数量 / 回读槽数量 / 是否引出译码地址与写脉冲」上各有差异：`tx_intf`、`side_ch` 把译码地址总线引出供算法复用 BRAM；`xpu`、`side_ch` 输出写脉冲 `slv_reg_wren_signal`。
- 寄存器语义不在壳里、而在顶层（如 `xpu.v`）：`slv_reg0` 做按位软复位、`slv_reg1[2]` 是 RX DMA 过滤总闸、`slv_reg9` 用 bit[31] 覆盖 IFS/时隙、`slv_reg57/58/59/62/63` 聚合状态与版本号——这才是软件与 FPGA 的真正契约。

## 7. 下一步学习建议

- **想动手调试寄存器**：继续学 [u7-l6（GPIO/LED 调试、ILA 与 ENABLE_DBG）](u7-l6-gpio-led-ila-debug.md)，用 ILA 抓 `slv_reg_wren` 与 AW/W/B 通道波形，眼见为实地验证本讲的握手时序。
- **想理解寄存器背后的条件编译**：学 [u7-l2（条件编译与 Verilog 宏体系）](u7-l2-conditional-compile-macros.md)，看 `openofdm_tx_s_axi.v` 顶部 `include "openofdm_tx_pre_def.v"` 这类宏是怎么在打包期生成的。
- **想改一个寄存器并重新集成**：学 [u7-l4（修改并打包自定义 IP）](u7-l4-modify-package-custom-ip.md)，当你需要给某个 `*_s_axi.v` 增删一个 `slv_reg` 槽时，它会告诉你如何重新打包并接回顶层。
- **跨向软件侧**：本仓库止步于 `*_s_axi.v` 这层壳；真正 `readl/writel` 这些寄存器、把它接进 mac80211 的代码在 **openwifi 软件仓库**（驱动），可作为本讲之后的延伸阅读。

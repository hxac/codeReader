# 高级主题：LTSSM、链路状态、性能与调试

> 适用工程：PCIeSquirrel（Screamer PCIe Squirrel，XC7A35T-484，PCIe gen2 x1，FT601 USB3）。
> 本文承接 u3-l2（PCIe 配置空间管理）与 u2-l5（命令/控制寄存器文件与读写协议）。

## 1. 本讲目标

设备「插上去没反应」「lspci 看不到」「速度只有预期一半」——这是把 pcileech-fpga 从「能烧录」推进到「稳定可用」时最常见的三类问题。它们的根因几乎都藏在三处：**PCIe 链路训练状态机（LTSSM）卡在某个状态**、**主机侧链路带宽成为瓶颈**、**板载没有可观察的诊断信号**。本讲围绕这三处，把 pcileech-fpga 已经内置、但散落在各源码文件里的「调试仪表盘」串成一套可操作的方法。

学完后你应当能够：

- 说清 PCIe LTSSM 是什么、它卡住意味着什么，并能在 `pcileech_pcie_cfg_a7.sv` 的只读寄存器表里找到 `pl_ltssm_state`、`pl_sel_lnk_rate`、`pl_sel_lnk_width`、`pl_phy_lnk_up` 等链路状态字段；
- 设计一条「经命令包读回 LTSSM 状态与链路速率」的调试流程，并解释这些字段如何映射到 `lspci` 的输出；
- 用带宽公式解释「为什么 x4 工程和 x1 工程在 USB3 设备上速度几乎一样」，以及 readme 里 `190 MB/s` 这个数字从何而来；
- 读懂 `led_pcie` / `led_com` 两个 LED 的生成逻辑、`tickcount64` 不活动计时器的看门狗行为，以及 `STARTUPE2` 全局复位机制，并据此在现场快速定位故障层。

## 2. 前置知识

本讲默认你已经读过 u1-l4（顶层三大子系统）、u2-l5（fifo 的 `ro`/`rw` 寄存器文件与命令包协议）、u3-l1（PCIe 核心封装）与 u3-l2（配置空间管理）。下面只补三个本讲会用、但前面没展开的概念。

**链路训练（Link Training）**：两台 PCIe 设备（这里一端是目标机 root port，一端是 FPGA）在能传任何 TLP 之前，必须先在物理层上「握手」——协商 lane 极性、lane 间的映射顺序、链路宽度（x1/x2/x4…）、速率（gen1 2.5 GT/s 或 gen2 5 GT/s）。这个握手的执行者就叫 **LTSSM（Link Training and Status State Machine，链路训练与状态机）**。握手没完成，链路就「上不来」（link down），主机根本枚举不到设备。

**GT/s 与 MB/s**：PCIe 用「每秒传输次数 GT/s」描述线速，但每 10 位线上编码只承载 8 位数据（8b/10b 编码，gen1/gen2）。所以数据带宽换算为：

\[
\text{带宽(MB/s)} = \text{GT/s} \times \frac{8}{10} \times \frac{10^9\,\text{b/s}}{8\,\text{B}} \div \text{lane 复用开销}
\]

化简后每条 lane 的「理论数据带宽」约为 \(\text{GT/s} \times 100\ \text{MB/s}\)。例如 gen2（5 GT/s）x1 ≈ 500 MB/s。本讲的性能讨论都建立在这条公式上。

**STARTUPE2**：Xilinx 7 系列 FPGA 的一个底层原语，它内部连着整个芯片的 **GSR（Global Set/Reset，全局置位/复位）** 网络。普通复位只重置你 HDL 里写到的那些触发器，而拉高 `STARTUPE2.GSR` 会同时把芯片里**所有**触发器一次性复位——等价于「不重新加载比特流、但把整个设计打回初始态」。pcileech-fpga 用它实现最重度的「整机复位」。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `PCIeSquirrel/src/pcileech_pcie_cfg_a7.sv` | PCIe 配置面代理人，维护一份状态镜像寄存器表 `ro` | `ro` 表里 LTSSM 与链路状态字段的位布局；命令包读寄存器的解析逻辑 |
| `PCIeSquirrel/src/pcileech_pcie_a7.sv` | PCIe 硬核 IP `pcie_7x_0` 的封装层 | `led_state` 的生成、`user_lnk_up` 的来源、复位层级 |
| `PCIeSquirrel/src/pcileech_squirrel_top.sv` | 顶层模块 | `tickcount64`/`tickcount64_reload`、`rst`、LED 的 OBUF 映射、5 秒重载 |
| `PCIeSquirrel/src/pcileech_fifo.sv` | 路由与控制中枢 | 不活动计时器看门狗、`STARTUPE2` 全局复位、`ro` 表里的 UPTIME |
| `PCIeSquirrel/src/pcileech_com.sv` | FT601 通信核心 | `led_state_txdata` 的生成、`initial_rx` 上电注入 |
| `readme.md` | 项目说明 | 各设备连接方式与传输速率表、x1 足够论 |

## 4. 核心概念与源码讲解

### 4.1 LTSSM 与 PCIe 链路状态寄存器

#### 4.1.1 概念说明

LTSSM 是一个由 PCIe 规范定义的大型状态机，由「Detect → Polling → Configuration → Recovery → L0（正常工作）」等若干大状态构成，每个大状态内部又分若干子状态。Xilinx 7 系列 PCIe 硬核 `pcie_7x_0` 把当前所处的子状态用一个 6 位编码 `pl_ltssm_state[5:0]` 暴露出来。这套状态对调试极其重要：

- 链路正常工作时，`pl_ltssm_state` 停在 **L0** 对应的编码上，`pl_phy_lnk_up=1`，主机 `lspci` 能看到设备。
- 若设备插上去主机完全看不到，往往是 LTSSM 卡在 **Polling**（电气/信号问题，链路根本建立不了 TS 序列）或 **Configuration**（链路能起来但协商宽度/极性失败）。
- 若时好时坏，可能是 **Recovery** 频繁被打断。

关键是：这套状态**不直接暴露给主机**，它是硬核的内部信号。要让主机软件读到，必须有人把它「搬运」到一份可读的寄存器表里。`pcileech_pcie_cfg_a7.sv` 干的就是这件事。

#### 4.1.2 核心流程

```
pcie_7x_0 硬核
   │  pl_ltssm_state[5:0], pl_sel_lnk_rate, pl_sel_lnk_width, pl_phy_lnk_up, …
   ▼
IfPCIeSignals ctx  (一组 wire，在 pcileech_header.svh 里声明)
   │
   ▼
pcileech_pcie_cfg_a7.sv：把这些 wire 逐位 assign 到只读寄存器表 ro[]
   │
   ▼
主机经「CFG 类型命令包」(MAGIC=0x77, type=01) 发读请求 → fifo_64_64 跨时钟域 →
cfg_a7 解析地址/值/掩码 → 把 ro[] 对应 16 位读回 → 经 fifo_32_32_clk2 回主机
```

也就是说，链路状态字段的**信号源头**在硬核，**打包点**在 cfg_a7 的 `ro` 表，**传输通道**是 u3-l2 讲过的「CFG 路径命令包」（MAGIC type=01）。注意区分两条命令路径：

- **CMD 路径**（type=11）→ 进 `pcileech_fifo.sv` 自己的 `ro`/`rw` 表（UPTIME、DRP 读回、PRSNT#/PERST# 等）；
- **CFG 路径**（type=01）→ 进 `pcileech_pcie_cfg_a7.sv` 的 `ro`/`rw` 表（**LTSSM、链路速率/宽度、cfg_mgmt 读回等都在这里**）。

要读 LTSSM，必须走 CFG 路径。

#### 4.1.3 源码精读

**(1) 链路状态字段在 `ro` 表里的位布局**

[PCIeSquirrel/src/pcileech_pcie_cfg_a7.sv:115-130](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_cfg_a7.sv#L115-L130) 把一组物理层状态信号逐位铺进只读寄存器表：

```systemverilog
// PCIe PL PHY
assign ro[85:80]    = ctx.pl_ltssm_state;          // +00A  6-bit LTSSM 子状态
assign ro[87:86]    = ctx.pl_rx_pm_state;          //
assign ro[90:88]    = ctx.pl_tx_pm_state;          // +00B  电源管理状态
assign ro[93:91]    = ctx.pl_initial_link_width;   //
assign ro[95:94]    = ctx.pl_lane_reversal_mode;   //
assign ro[97:96]    = ctx.pl_sel_lnk_width;        // +00C  协商出的链路宽度
assign ro[98]       = ctx.pl_phy_lnk_up;           //       物理链路是否 up
assign ro[99]       = ctx.pl_link_gen2_cap;        //
assign ro[100]      = ctx.pl_link_partner_gen2_supported; //
assign ro[101]      = ctx.pl_link_upcfg_cap;       //
assign ro[102]      = ctx.pl_sel_lnk_rate;         //       协商出的速率(0=gen1,1=gen2)
assign ro[103]      = ctx.pl_directed_change_done; // +00D:
assign ro[104]      = ctx.pl_received_hot_rst;     //
```

`ctx` 里的位宽在前置头里就有声明：[PCIeSquirrel/src/pcileech_header.svh:53-60](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh#L53-L60)（`pl_phy_lnk_up` 1 位、`pl_sel_lnk_rate` 1 位、`pl_sel_lnk_width[1:0]`、`pl_ltssm_state[5:0]`），与上面 `ro` 的切片一一对应。

把上面整理成一张「按字节偏移」读的表（`ro` 是小端位序，地址以 bit 为单位、字节偏移 = bit/8）：

| 字节偏移 | `ro` 位区间 | 信号 | 含义 |
| --- | --- | --- | --- |
| `+008` | `[71:64]` / `[76:72]` / `[79:77]` | `cfg_bus/device/function_number` | 主机枚举出的 BDF |
| `+00A` | `[85:80]` | `pl_ltssm_state` | **6-bit LTSSM 子状态** |
| `+00A` | `[87:86]` | `pl_rx_pm_state` | 接收侧电源管理状态 |
| `+00B` | `[90:88]` | `pl_tx_pm_state` | 发送侧电源管理状态 |
| `+00B` | `[93:91]` | `pl_initial_link_width` | 训练起始宽度 |
| `+00C` | `[97:96]` | `pl_sel_lnk_width` | **协商出的链路宽度**（0=x1,1=x2,2=x4,3=x8） |
| `+00C` | `[98]` | `pl_phy_lnk_up` | **物理链路 up** |
| `+00C` | `[99]` / `[100]` | gen2 能力 / 对端是否支持 gen2 | 速率能力协商 |
| `+00C` | `[102]` | `pl_sel_lnk_rate` | **协商出的速率**（0=gen1 2.5GT/s, 1=gen2 5GT/s） |
| `+00D` | `[103]` / `[104]` | `pl_directed_change_done` / `pl_received_hot_rst` | 速率/宽度切换完成、收到 hot reset |
| `+017` | `[186:184]` | `cfg_pcie_link_state` | 链路管理状态机（L0/L0s/L1…） |

> 关于 LTSSM 6 位编码的具体取值（哪个值代表 Detect、Polling.Active、Configuration、L0……），Xilinx 在 7-Series PCIe LogiCORE IP 文档 **PG054** 的「LTSSM States」一表中有权威定义（常见参考值如 L0 ≈ `0x16`、Detect.Quiet = `0x00`、Polling/Configuration 段在 `0x05`~`0x0D` 附近，但不同核版本略有差异）。**待本地验证**：以你 Vivado 对应版本的 PG054 为准。

**(2) 这些 `ctx` 信号的源头——硬核端口**

`ctx.pl_*` 来自硬核 IP 的物理层状态输出，在 [PCIeSquirrel/src/pcileech_pcie_a7.sv:225-237](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L225-L237)：

```systemverilog
// PCIe core PHY
.pl_initial_link_width      ( ctx.pl_initial_link_width  ),  // -> [2:0]
.pl_phy_lnk_up              ( ctx.pl_phy_lnk_up          ),  // ->
.pl_sel_lnk_rate            ( ctx.pl_sel_lnk_rate        ),  // ->
.pl_sel_lnk_width           ( ctx.pl_sel_lnk_width       ),  // -> [1:0]
.pl_ltssm_state             ( ctx.pl_ltssm_state         ),  // -> [5:0]
```

而 `pl_phy_lnk_up` 同时被硬核以「用户接口」形式另发一份 `user_lnk_up`（[pcileech_pcie_a7.sv:258](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L258)），后者就是驱动 LED 的信号（见 4.3）。

**(3) 主机「读」走的是命令包协议**

`pcileech_pcie_cfg_a7.sv` 用与 fifo 完全同形的命令包解析来读写自己的 `ro`/`rw` 表，见 [pcileech_pcie_cfg_a7.sv:322-335](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_cfg_a7.sv#L322-L335)：

```systemverilog
wire [15:0] in_cmd_address_byte = in_dout[31:16];
wire [17:0] in_cmd_address_bit  = {in_cmd_address_byte[14:0], 3'b000};   // 字节地址→位地址
wire        f_rw                = in_cmd_address_byte[15];                 // 1=rw, 0=ro
wire [15:0] in_cmd_data_in      = (...) ? (f_rw ? rw[...] : ro[...]) : 16'h0000;
wire        in_cmd_read         = in_dout[12] & in_valid;
wire        in_cmd_write        = in_dout[13] & in_cmd_address_byte[15] & in_valid;
```

读 LTSSM 属于「读 `ro`」，故命令包里 `f_rw=0`（即 `address_byte[15]=0`）。地址字段低 15 位左移 3 位得到位地址：LTSSM 在 `ro[85:80]`，位地址 80 对应字节地址 `80/8 = 0x0A`。读回的 16 位会覆盖 `[85:80]`(LTSSM) 等。

> 注意：读回值在 [pcileech_pcie_cfg_a7.sv:348-349](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_cfg_a7.sv#L348-L349) 做了字节倒序（`out_data[15:0] = {data_in[7:0], data_in[15:8]}`），主机软件（LeechCore 的 `device_fpga.c`）会再倒回来。所以「主机看到什么」要按其约定解析。

#### 4.1.4 代码实践

**实践目标**：不依赖现场硬件，纸上设计一条「读回 LTSSM 与链路速率」的命令，并预测解码结果；若有硬件则用 PCILeech/MemProcFS 实测。

**操作步骤（纸面推导）**：

1. 构造一条读 LTSSM 的 CFG 类型命令 64 位字。字段布局（沿用 u2-l5/u3-l2）：
   - `[7:0]` = `0x77`（MAGIC）
   - `[9:8]` = `2'b01`（CFG 类型）
   - `[12]` = `1`（读标志 `in_cmd_read`）
   - `[31:16]` = `0x000A`（字节地址 0x0A，且 `[15]=0` 表示读 `ro`）
   - 其余位 = 0
   → 拼起来为 `0x0000_000A_0000_1177`（小端按字节拼；**待本地验证**：实际发送格式以 `device_fpga.c` 为准）。
2. 同理读链路速率所在的字节 `0x0C`：`[31:16]=0x000C`，得到另一条读命令。
3. 解码：第一条返回字的低 6 位即 `pl_ltssm_state`；第二条返回字的 bit 6（`ro[102]` 相对字节起点 `ro[96]` 的偏移）即 `pl_sel_lnk_rate`，bit 1:0 是 `pl_sel_lnk_width`，bit 2 是 `pl_phy_lnk_up`。

**需要观察的现象**：

- 链路正常时：`pl_phy_lnk_up=1`，`pl_ltssm_state` 稳定在 L0 编码，`pl_sel_lnk_rate=1`（gen2），`pl_sel_lnk_width=0`（x1）。
- 链路起不来时：`pl_phy_lnk_up=0`，`pl_ltssm_state` 在 Polling/Configuration 段反复跳变。

**预期结果**：正常态读到一组稳定值；异常态读到 LTSSM 卡死或频繁跳变。**待本地验证**（具体读命令的发送与回读需通过 PCILeech 主机侧工具，如 `MemProcFS -device fpga` 的调试接口或 LeechCore API 实现）。

#### 4.1.5 小练习与答案

**练习 1**：为什么读 LTSSM 必须用 CFG 类型（type=01）命令，而不能用 CMD 类型（type=11）？
**答案**：因为 LTSSM 字段 `ro[85:80]` 存在 `pcileech_pcie_cfg_a7.sv` 的寄存器表里，而该表只挂在 **CFG 路径**上（u3-l2 的 `dcfg`）。CMD 类型命令只会进 `pcileech_fifo.sv` 自己的表，那张表里没有 LTSSM。

**练习 2**：`pl_sel_lnk_rate` 与 `pl_sel_lnk_width` 分别告诉我们什么？
**答案**：`pl_sel_lnk_rate` 是链路最终协商的线速（0=gen1 2.5 GT/s，1=gen2 5 GT/s）；`pl_sel_lnk_width` 是协商出的 lane 数（0=x1，1=x2，2=x4，3=x8）。两者合起来决定 PCIe 侧的理论带宽上限。

**练习 3**：若读到 `pl_phy_lnk_up=0` 且 `pl_ltssm_state` 长时间停在某个 Polling 子状态，最可能的故障层是什么？
**答案**：物理/信号层——参考时钟、PERST、GTP 走线、AC 耦合电容或 lane 极性有问题，链路连 TS 序列都建立不起来。应优先查 xdc 里的 GTP LOC 与参考时钟约束（见 u5-l2），而非 HDL 逻辑。

### 4.2 性能与瓶颈：为什么 x4 和 x1 一样快

#### 4.2.1 概念说明

pcileech-fpga 是一个「双链路」系统：一端是 **PCIe 链路**（FPGA ↔ 目标机内存），另一端是 **主机链路**（FPGA ↔ 攻击者主机，USB3/Thunderbolt/以太网）。DMA 读出的内存数据要先经 PCIe 进 FPGA，再经主机链路送到攻击者主机。整条管道的吞吐由**最窄的一段**决定（木桶效应）。

一个常见误解是「x4 的 PCIe 一定比 x1 快 4 倍」。在 pcileech-fpga 上这通常不成立，因为主机链路才是瓶颈。

#### 4.2.2 核心流程

数据流的实际带宽取两端较小者：

\[
\text{实测吞吐} \approx \min(\text{PCIe 侧带宽},\ \text{主机链路带宽})
\]

各段的理论值（gen1/gen2 用 8b/10b）：

| 链路段 | 速率 | 理论数据带宽 |
| --- | --- | --- |
| PCIe gen1 x1 | 2.5 GT/s | \(\approx 250\) MB/s |
| PCIe gen2 x1 | 5 GT/s | \(\approx 500\) MB/s |
| PCIe gen2 x4 | 5 GT/s × 4 | \(\approx 2000\) MB/s |
| USB 3.0（FT601） | 5 Gb/s | \(\approx 400\) MB/s 理论，实测约 **190 MB/s** |
| Thunderbolt 3（ZDMA） | 高 | 实测可达 **1000 MB/s** |

把 Squirrel 代入：PCIe 侧 gen2 x1 ≈ 500 MB/s，主机侧 FT601/USB3 实测 ≈ 190 MB/s。于是

\[
\min(500,\ 190) = 190\ \text{MB/s}
\]

瓶颈是 USB3，与 PCIe 用 x1 还是 x4 无关——因为把 PCIe 提到 x4（2000 MB/s）后，`min(2000, 190)` 仍然是 190。这就是 readme 那句「PCILeech FPGA uses PCIe x1 even if more PCIe lanes are available hardware-wise. This is sufficient」的数学原因。只有当主机链路足够宽（如 ZDMA 的 Thunderbolt3）时，x4 才有意义。

#### 4.2.3 源码精读

**（1）速率表与「x1 足够论」**

[readme.md:12-25](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/readme.md#L12-L25) 的设备表里，FT601/USB-C 类设备（CaptainDMA 系列、LeetDMA、Squirrel 等）清一色 `190 MB/s`，而 Thunderbolt3 的 ZDMA 是 `1000 MB/s`：

```
| ZDMA              | Thunderbolt3 | 1000 MB/s | ... | PCIe gen2 x4 |
| CaptainDMA M2     | USB-C        | 190 MB/s  | ... | PCIe gen2 x1-x4 |
| Screamer PCIe Squirrel | USB-C   | 190 MB/s  | ... | XC7A35T-484 |
###### PCILeech FPGA uses PCIe x1 even if more PCIe lanes are available ...
```

注意 CaptainDMA M2 那一行写的是 `PCIe gen2 x1-x4`——硬件支持 x4，但实际跑 x1，因为「sufficient」。

**（2）PCIe 侧协商出的速率/宽度可被读到**

`pl_sel_lnk_rate`（ro[102]）与 `pl_sel_lnk_width`（ro[96:97]）就是「链路最终跑在什么速率/宽度上」的实测值（见 4.1.3）。配合 4.1 的读命令，就能验证「设备确实只协商到了 gen2 x1」——而不是固件声称 x1、实际却掉到 gen1。这能把「速度只有预期一半」的根因（掉到 gen1，250 MB/s；还是 USB 本就如此，190 MB/s）区分开。

**（3）主机侧的「慢」也写在工程里**

USB 侧并非无脑满速。FT601 的 TX FIFO 设了 `prog_full` 阈值 6 / `prog_empty` 阈值 3（见 u2-l2 的 com 模块），主机来不及消费时 FIFO 会反压；这部分开销就是 190 MB/s 与 USB3 理论 400 MB/s 之间差距的来源之一。

#### 4.2.4 代码实践

**实践目标**：用带宽公式解释一个现象，并设计一个判别实验。

**操作步骤**：

1. 给定 Squirrel 跑在 gen2 x1，写出 PCIe 侧理论带宽（500 MB/s）与 USB3 实测（190 MB/s），判断瓶颈。
2. 设想把固件换成 x4（u6-l1 的 `pcileech_pcie_a7x4`），PCIe 侧升到约 2000 MB/s，重新计算 `min(2000, 190)`，回答「速度会提升吗」。
3. 设计判别实验：若现场实测只有 ~95 MB/s（约为 190 的一半），列出两种假设并各给一个验证手段：
   - 假设 A：链路掉到了 gen1（`pl_sel_lnk_rate=0`）。验证：用 4.1 的命令读 `ro[102]`。
   - 假设 B：主机端 USB 调度/反压。验证：观察 `led_com`（见 4.3）是否常亮（TX FIFO 频繁 prog_full）。

**需要观察的现象**：理论上 x1 与 x4 在 USB3 设备上速度相同（均 190 MB/s）；只有 gen1 降速或 USB 反压会让实测更低。

**预期结果**：x4 升级对 USB3 类设备几乎无收益。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 readme 说 x1「sufficient」？
**答案**：因为主机侧 USB3 实测约 190 MB/s，而 PCIe gen2 x1 已达约 500 MB/s，PCIe 不是瓶颈；继续升 x4 不会提升 `min(500→2000, 190)` 的结果。

**练习 2**：ZDMA 用 Thunderbolt3 能跑到 1000 MB/s，说明什么？
**答案**：Thunderbolt3 主机链路带宽远大于 USB3，不再是瓶颈，此时 PCIe 侧带宽（gen2 x4 ≈ 2000 MB/s）才开始发挥作用，故 x4 在 ZDMA 这类设备上才有意义。

**练习 3**：实测速度 ~95 MB/s，如何区分「gen1 降速」与「USB 反压」？
**答案**：读 `ro[102]`(pl_sel_lnk_rate)：若为 0 则是 gen1 降速（PCIe 侧问题）；若为 1 但 `led_com` 频繁亮，则是 USB TX FIFO 反压（主机侧问题）。

### 4.3 LED 诊断与计时器调试机制

#### 4.3.1 概念说明

现场调试时，没有示波器、也来不及连主机读寄存器，最直接的诊断就是板上的 LED。pcileech-fpga 把两个 LED 做成了**有语义的信号灯**：

- `led_pcie`（LD1）：反映 PCIe 链路是否 up——「常亮 = 链路正常」，「慢闪 = 链路没起来」。
- `led_com`（LD2）：反映 USB 侧 TX FIFO 的压力/活动状态。

此外还有两个「计时器型」调试机制：`tickcount64` 衍生的**不活动看门狗**（一段时间没数据就主动告警），以及 `STARTUPE2` 触发的**整机全局复位**（含 5 秒长按重载 PCIe 配置）。这几样加起来，构成了一套不依赖主机的「板载自检」。

#### 4.3.2 核心流程

```
LED：
  pcie_clk_c(100MHz) → tickcount64_pcie_refclk[25] (慢闪方波) ─┐
  pcie_7x_0.user_lnk_up ────────────────────────────────────────┤→ led_state = up || blink → led_pcie (LD1)
  com_tx_prog_full ^ led_pwronblink → led_state_txdata → led_com (LD2)

看门狗：
  每当有数据发给主机 → 刷新 _cmd_timer_inactivity_base = tickcount64
  若启用(rw[16]) 且 tickcount64 - base > 阈值(rw[95:64]) → 发 {0xffff,0xcede} 告警包，自关

全局复位：
  长按 SW2 超 5s → tickcount64_reload > 500000000 → rst_cfg_reload
  主机写 rw[31] 或 rst_cfg_reload → STARTUPE2.GSR = 1 → 整片 FPGA 全局复位
```

#### 4.3.3 源码精读

**（1）`led_pcie` = `user_lnk_up || 慢闪`**

[PCIeSquirrel/src/pcileech_pcie_a7.sv:64-67](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L64-L67)：

```systemverilog
time tickcount64_pcie_refclk = 0;
always @ ( posedge pcie_clk_c )
    tickcount64_pcie_refclk <= tickcount64_pcie_refclk + 1;
assign led_state = user_lnk_up || tickcount64_pcie_refclk[25];
```

`pcie_clk_c` 是经 `IBUFDS_GTE2` 转出的 100 MHz PCIe 参考时钟（见 u3-l1）。计数器 bit[25] 的翻转周期为 \(2^{26}/10^8 \approx 0.67\) s（半周期约 0.34 s），形成慢闪。`user_lnk_up` 一旦为 1（链路 up），`led_state` 被「或」成恒高，LED 转为**常亮**。所以现场经验是：

- **LD1 慢闪** → 链路没起来（LTSSM 未到 L0）；
- **LD1 常亮** → 链路 up。

`led_state` 经顶层 [pcileech_squirrel_top.sv:90](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L90) 的 `OBUF led_ld1_obuf(.O(user_ld1), .I(led_pcie));` 驱动到物理 LED LD1。

**（2）`led_com` = `prog_full ^ 上电闪烁`**

[PCIeSquirrel/src/pcileech_com.sv:155](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_com.sv#L155)：

```systemverilog
assign led_state_txdata     = com_tx_prog_full ^ led_state_invert;
```

`com_tx_prog_full` 是发往 FT601 的 TX FIFO 的「可编程满」标志（阈值 6）。主机来不及消费时 FIFO 接近满，`prog_full` 置位，LED 状态随之翻转——即「USB 侧积压」会在 LD2 上体现为异或后的亮灭变化。`led_state_invert` 来自顶层 [pcileech_squirrel_top.sv:88](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L88) 的上电闪烁信号：

```systemverilog
wire led_pwronblink = ~user_sw1_n ^ (tickcount64[24] & (tickcount64[63:27] == 0));
```

它在上电早期（`tickcount64[63:27]==0`，即前约 1.9 s）以 bit[24] 的频率闪，作为「板子活着、但还没就绪」的视觉提示；之后就绪后 `led_com` 主要反映 TX FIFO 压力。LD2 经 [pcileech_squirrel_top.sv:91](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L91) 的 `OBUF led_ld2_obuf(.O(user_ld2), .I(led_com));` 驱动。

**（3）`tickcount64` 与上电/按键复位**

[PCIeSquirrel/src/pcileech_squirrel_top.sv:79-92](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L79-L92)：

```systemverilog
time tickcount64 = 0;
time tickcount64_reload = 0;
always @ ( posedge clk ) begin
    tickcount64         <= user_sw2_n ? (tickcount64 + 1) : 0;
    tickcount64_reload  <= user_sw2_n ? 0 : (tickcount64_reload + 1);
end
assign rst = ~user_sw2_n || ((tickcount64 < 64) ? 1'b1 : 1'b0);
```

要点：

- 上电前 64 拍（`tickcount64 < 64`）全局 `rst=1`——给所有跨时钟 FIFO、PLL 留稳定时间；
- 按住 SW2 时 `rst=1`，且 `tickcount64_reload` 持续累加；
- 当 `tickcount64_reload > 500000000`（[pcileech_squirrel_top.sv:130](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L130)），即 \(5\times10^8 / 10^8 = 5\) 秒，触发 `rst_cfg_reload`。

**（4）不活动看门狗**

`pcileech_fifo.sv` 把「多久没给主机发数据」做成一个看门狗。基线 `_cmd_timer_inactivity_base` 在每次有 TX 活动时刷新，见 [pcileech_fifo.sv:413-414](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L413-L414)：

```systemverilog
if ( dcom.com_din_wr_en | ~dcom.com_din_ready )
    _cmd_timer_inactivity_base <= tickcount64;
```

一旦空闲时长超过阈值，就主动发一个告警包并自关，[pcileech_fifo.sv:391-398](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L391-L398)：

```systemverilog
else if ( ~_cmd_tx_almost_full & ~in_cmd_write & _cmd_timer_inactivity_enable
          & (_cmd_timer_inactivity_ticks + _cmd_timer_inactivity_base < tickcount64) ) begin
    _cmd_tx_wr_en <= 1'b1;
    _cmd_tx_din[31:16] <= 16'hffff;          // 告警地址标记
    _cmd_tx_din[15:0]  <= 16'hcede;          // 告警魔数
    rw[16] <= 1'b0;                          // 一次性，发完自关
end
```

主机收到 `{0xffff, 0xcede}` 就知道「设备静默超时」。这套机制同时把 `_cmd_timer_inactivity_base` 暴露在 fifo 的 `ro` 表里（[pcileech_fifo.sv:243](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L243) 的 `ro[255:192]`），以及把运行时间 `tickcount64` 暴露为 UPTIME（[pcileech_fifo.sv:241](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L241) 的 `ro[191:128]`）。

**（5）`STARTUPE2` 全局复位**

最重度的复位由 [pcileech_fifo.sv:436-456](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L436-L456) 的 `STARTUPE2` 原语实现：

```systemverilog
STARTUPE2 #(...) i_STARTUPE2 (
    .CLK  ( clk ),
    .GSR  ( rw[RWPOS_GLOBAL_SYSTEM_RESET] | rst_cfg_reload ),  // GLOBAL SYSTEM RESET
    ...
);
```

`GSR` 由两路触发：主机软件写 `rw[31]`（`RWPOS_GLOBAL_SYSTEM_RESET`，定义在 [pcileech_fifo.sv:210](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L210)），或顶层 5 秒长按产生的 `rst_cfg_reload`。拉高 `GSR` 会让整片 FPGA 的所有触发器回到初始值——比普通 `rst` 更彻底，常用于「PCIe 配置改坏了、想让 `initial_rx` 重新注入、让 PCIe 核重训」的场景。

#### 4.3.4 代码实践

**实践目标**：仅凭 LED 与按键，在无主机环境下对一块「插上没反应」的板子做分级定位。

**操作步骤**：

1. 上电后先看 **LD1（led_pcie）**：
   - 慢闪 → 链路没起来 → 怀疑物理层（参考时钟/PERST/GTP 走线），对应 u5-l2、u6-l2 的检查项；
   - 常亮 → 链路 up，问题在主机侧或软件侧。
2. 看 **LD2（led_com）**：常亮或异或后明显变化 → USB TX FIFO 积压，主机没在消费数据。
3. 若怀疑 PCIe 配置异常（如 cfgspace_shadow/bar 改错导致核卡死），**长按 SW2 超过 5 秒**：触发 `rst_cfg_reload → STARTUPE2.GSR`，整机复位并重新跑 `initial_rx` 上电序列（com 模块 [pcileech_com.sv:63-90](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_com.sv#L63-L90) 的注入会重新把 PCIe 核拉上线）。
4. 复位后若 LD1 仍慢闪，基本可判定为物理层故障，需重查 xdc。

**需要观察的现象**：LD1 在链路 up 前慢闪、up 后常亮；长按 SW2 5 秒后整机重启，LD1 重新进入慢闪→常亮的流程。

**预期结果**：能用「LD1 慢闪/常亮 + LD2 状态 + 5 秒重载」三步把故障缩到「物理层 / PCIe 配置 / 主机侧」三个区间之一。**待本地验证**（不同板子 LED 丝印与按键可能不同，以板卡文档为准）。

#### 4.3.5 小练习与答案

**练习 1**：`led_state = user_lnk_up || tickcount64_pcie_refclk[25]` 中，为什么用「或」而不是「与」？
**答案**：用「或」保证链路一旦 up（`user_lnk_up=1`）LED 就恒亮（常亮=正常）；链路没起来时 `user_lnk_up=0`，LED 随 bit[25] 慢闪（慢闪=异常）。若用「与」，链路 up 反而会让 LED 闪、异常时常亮，语义反了。

**练习 2**：`tickcount64_pcie_refclk[25]` 的闪烁周期大约是多少？
**答案**：`pcie_clk_c` 为 100 MHz，bit[25] 半周期为 \(2^{25}/10^8 \approx 0.34\) s，整周期约 0.67 s。

**练习 3**：长按 SW2 超过 5 秒和不按 SW2、只让主机写 `rw[31]`，效果有何异同？
**答案**：两者最终都拉高 `STARTUPE2.GSR`，整机复位效果相同。区别是触发源：前者是物理按键（硬件现场可用），后者是软件命令（需主机链路通）。注意 5 秒长按在期间还会先保持普通 `rst=1`。

## 5. 综合实践

**任务**：把 4.1、4.2、4.3 串起来，设计一份「pcileech-fpga 上电后链路异常」的分级排查 SOP（标准作业流程）。

请按下面的思路整理出一份纸面排查表（无需硬件，重在把因果链理清）：

1. **观察层（无主机）**：记录 LD1、LD2 状态。
   - LD1 慢闪 → 进入第 2 步（物理/训练层）。
   - LD1 常亮但主机 `lspci` 看不到 → 进入第 3 步（配置/枚举层）。
   - LD1 常亮、`lspci` 看得到但速度异常 → 进入第 4 步（性能层）。
2. **训练层**：用 4.1 的 CFG 命令读 `pl_ltssm_state`（ro[85:80]）、`pl_phy_lnk_up`（ro[98]）。若 LTSSM 卡在 Polling/Configuration，结合 u5-l2 复查 GTP LOC、参考时钟、PERST 约束；必要时长按 SW2 5 秒触发 `STARTUPE2` 全局复位后重读。
3. **配置层**：LTSSM 已到 L0、`pl_phy_lnk_up=1`，但 `cfg_bus_number` 读不到有效 BDF，或 `cfgspace_shadow` 的 `cfgtlp_zero`（fifo `rw[203]`）仍为 1 导致影子不可见——参考 u4-l1、u4-l4 排查配置空间影子与设备身份。
4. **性能层**：用 4.2 的方法读 `pl_sel_lnk_rate`（ro[102]）、`pl_sel_lnk_width`（ro[96:97]）确认协商到 gen2 x1；若实测远低于 190 MB/s，结合 LD2 判断是否 USB 反压。
5. **看门狗层**：若主机侧偶发「读到一半卡住」，检查 fifo 的不活动计时器是否发出过 `{0xffff,0xcede}` 告警（参考 4.3.3），并读 UPTIME（ro[191:128]）核对设备是否重启过。

输出：一张「现象 → 寄存器/LED 证据 → 推断的故障层 → 对应讲义」的四列表。这张表就是后续现场调试的 checklist。

## 6. 本讲小结

- LTSSM 是 PCIe 链路训练状态机；pcileech-fpga 把硬核的 `pl_ltssm_state[5:0]`、`pl_phy_lnk_up`、`pl_sel_lnk_rate`、`pl_sel_lnk_width` 等信号铺在 `pcileech_pcie_cfg_a7.sv` 的只读寄存器表 `ro`（`+00A`~`+00D` 段），经 **CFG 类型命令包（MAGIC=0x77, type=01）** 可读回。
- 这些字段把 `lspci` 看不到设备、速度异常等现象，精确归因到「物理训练层」「协商速率/宽度」「设备身份」等不同层。
- 双链路系统的吞吐取 `min(PCIe 侧, 主机侧)`：gen2 x1 ≈ 500 MB/s 已远超 USB3 实测约 190 MB/s，故 USB3 类设备上 x4 与 x1 速度几乎相同；只有 Thunderbolt3（ZDMA）这类宽主机链路才让 x4 有意义——这就是 readme「x1 sufficient」与 `190 MB/s` 的来源。
- `led_pcie = user_lnk_up || 慢闪`：**常亮=链路 up，慢闪=链路没起来**，是最快的现场诊断；`led_com` 反映 USB TX FIFO 的 prog_full 压力。
- `tickcount64` 衍生三套机制：上电前 64 拍复位、fifo 的不活动看门狗（超时发 `{0xffff,0xcede}`）、以及配合 5 秒长按的 `rst_cfg_reload`；后者与主机写 `rw[31]` 一起经 `STARTUPE2.GSR` 触发**整机全局复位**。
- 现场调试顺序建议：先看 LED（无主机）分级 → 再读 LTSSM 寄存器（定位训练层）→ 再查配置/性能 → 必要时长按 5 秒整机复位重试。

## 7. 下一步学习建议

- 若你想把「读 LTSSM」变成可运行的主机工具，建议阅读 LeechCore 仓库（独立项目）中的 `device_fpga.c`，那里实现了与本讲 `ro`/`rw` 位布局一一对应的命令封装与回读解析。
- 若想深入「链路协商速率/宽度」的可控调整，可回到 `pcileech_pcie_cfg_a7.sv` 的 `rw[176:184]`（`pl_directed_link_*` 字段）与 `pcileech_pcie_a7.sv` 的 DRP 通路（u5-l4），研究运行时引导速率切换。
- 若关注 x4 与以太网等变体如何复用本讲的调试机制，可对照 u6-l1（设备变种对比），看 `led_state`/`user_lnk_up` 在 `pcileech_pcie_a7x4` 与 `pcileech_eth` 中是否一致。
- 至此 pcileech-fpga 学习手册的六单元已走完一遍：从项目总览（u1）→ 通信与控制（u2）→ PCIe 与 TLP（u3）→ 配置影子与 BAR 仿真（u4）→ 时序/IP/DRP（u5）→ 变种与现场调试（u6）。建议以本讲的「排查 SOP」为线索，回头重读 u3-l2、u4-l4、u5-l2，把诊断能力固化为肌肉记忆。

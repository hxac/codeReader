# PS-PL 互连：AXI、DMA 与中断

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 Zynq SoC 里 PS（ARM 处理系统）与 PL（FPGA 可编程逻辑）之间共有几类通路，各自传什么。
- 看懂 `connect_openwifi_ip.tcl` 这份脚本把 `openwifi_ip` 层级「焊」到 Zynq PS 上的三条主线：AXI4-Lite 寄存器、AXI DMA 数据、中断。
- 指出发射（TX）数据和接收（RX）数据分别走 PS 的哪一个 AXI 端口（ACP / HP3），并能解释为什么这样分。
- 读懂中断拼接器 `sys_concat_intc` 如何把 7 路 openwifi 中断拼成一个向量送给 PS 的 GIC。
- 区分普通 Zynq（PS7）与 UltraScale+（PS8）两套脚本的差异。

本讲承接 [u2-l2 openwifi_ip 层级](u2-l2-openwifi-ip-hierarchy.md)。上一篇讲的是 `openwifi_ip` 层级「盒子内部」六个 IP 怎么互连；本讲要回答的问题是：**这个盒子造好之后，怎样和 ARM 处理系统（PS）连起来，让软件能配置它、和它收发数据、并响应它的事件。**

## 2. 前置知识

### 2.1 PS 与 PL

Xilinx Zynq 芯片由两部分组成：

- **PS（Processing System）**：一颗 ARM Cortex（Zynq-7000 是 A9，Zynq UltraScale+ 是 A53），跑 Linux 与 openwifi 驱动，是「大脑」。
- **PL（Programmable Logic）**：FPGA 可编程逻辑，openwifi 的全部 WiFi IP（xpu、tx_intf、rx_intf、openofdm_tx、openofdm_rx、side_ch）都实现在这里。

两者之间不能直接「飞线」，必须走 Zynq 预留的标准化接口。

### 2.2 AXI 协议族

AXI（Advanced eXtensible Interface）是 ARM 定义的总线协议，openwifi 用到三种「形态」：

| 形态 | 全称 | 特点 | openwifi 里传什么 |
|------|------|------|-------------------|
| AXI4-Lite | AXI4-Lite | 速率低、地址/数据位宽小（常 32 位）、每次传一两寄存器 | 控制/状态**寄存器**读写 |
| AXI4（内存映射） | AXI Memory-Mapped | 高带宽、带地址、可突发 | **DMA 读写 DDR 内存** |
| AXI-Stream | AXI-Stream | 无地址、纯数据流、有 `tvalid/tready` 握手 | IP 之间传**数据流**（本讲的边界信号） |

一条 AXI 通道分五个独立子通道（读地址、读数据、写地址、写数据、写响应），靠 `VALID/READY` 握手。初学者只需记住：**有握手才算一次成功传输。**

### 2.3 三类 PS-PL 通路

把 WiFi IP 接到 PS，本质就是建立三类通路：

1. **寄存器通路（控制/状态）**：软件读写 IP 里的配置寄存器、读状态。慢，走 AXI4-Lite。
2. **数据通路（收发包）**：待发包从 DDR 内存搬进 PL、收到的包从 PL 搬回 DDR。快，走 AXI DMA。
3. **中断通路（事件）**：PL 发生事件（收到一个包、发完一帧、DMA 完成）时，主动通知 PS。走中断。

本讲的全部内容，就是围绕这三类通路展开。

### 2.4 Zynq PS 的几类 AXI 端口

Zynq PS 对外暴露若干 AXI 端口，方向是从 PS 视角命名的：

- **M_AXI_GP0/GP1**（General Purpose Master）：PS 当**主设备**，去读/写 PL 里的寄存器。openwifi 用 `M_AXI_GP1`。
- **S_AXI_HP0~3**（High Performance Slave）：PL 当**主设备**，经 DMA 高速访问 DDR。HP 端口直连 DDR 控制器，带宽高、不保证与 CPU 缓存一致。openwifi 用 `S_AXI_HP3`。
- **S_AXI_ACP**（Accelerator Coherency Port）：也是 PL 当主设备访问 DDR，但**经过 CPU 的 L2 缓存**，保证与 CPU 缓存一致（coherent）。代价是多一跳延迟。

记住这两个 Slave 端口的区别，是理解「为什么收发走不同端口」的关键。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [ip/connect_openwifi_ip.tcl](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/connect_openwifi_ip.tcl) | **本讲主角**。把 `openwifi_ip` 层级接到普通 Zynq（PS7）的三类通路接线脚本。 |
| [ip/connect_openwifi_ip_ultra_scale.tcl](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/connect_openwifi_ip_ultra_scale.tcl) | UltraScale+（PS8）版本的接线脚本，端口名不同、中断处理方式不同。 |
| [ip/openwifi_ip.tcl](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl) | `openwifi_ip` 层级蓝图（上一篇详讲）。本讲引用它来确认层级「对外引脚」与内部 DMA/互连的对应关系。 |
| boards/zc706_fmcs2/src/system.bd | zc706 板的 block design 快照，**已经把本讲的接线烤进了 BD**，可用来对照验证。 |
| [README.md](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L158-L190) | 「Migrate」小节，说明迁移到新 ADI/Vivado 时如何重用 openwifi_ip 层级（即本脚本的使用场景）。 |

> **关于这份脚本何时运行**：`connect_openwifi_ip.tcl` 是一份独立 Tcl「配方」，它文档化（并可重放）`openwifi_ip` 到 PS 的全部接线。各板的 `system.bd` 已把这些连线固化进去；当你按 README「Migrate」把 openwifi_ip 层级搬进一个新的 ADI 参考设计后，就需要（在 Vivado Tcl 控制台）运行这份脚本把层级重新接到 PS。换句话说：**它不是 `create_ip_repo.sh` → `openwifi.tcl` 自动流程的一环，而是连接 openwifi_ip 层级与 PS 的「桥」**。

本讲覆盖两个最小模块：**「AXI 互连」**（4.1）和 **「DMA 与中断」**（4.2 + 4.3），4.4 是两套脚本差异的进阶对照。

## 4. 核心概念与源码讲解

先给一张全景。`connect_openwifi_ip.tcl` 建立的三类通路，可以这样画：

```
        ┌──────────────────────── Zynq PS (sys_ps7) ────────────────────────┐
        │                                                                   │
        │   M_AXI_GP1 ──────► (寄存器) ──────►   S_AXI_ACP ◄──── (RX DMA)    │
        │                                   ◄── S_AXI_HP3 ◄──── (TX DMA)    │
        │   IRQ_F2P[15:0] ◄── sys_concat_intc (16 拼接)                      │
        └───────────────────────────┬───────────────────────────────────────┘
                                       │ 三类通路
        ┌──────────────────────────────▼──────────────────────────────────┐
        │                      openwifi_ip (PL 层级)                       │
        │  S00_AXI(寄存器从口)  M00_AXI/M00_AXI1(DMA主口)  7×中断输出      │
        └───────────────────────────────────────────────────────────────────┘
```

下面逐条拆开。

---

### 4.1 最小模块一·AXI 互连：寄存器通路

#### 4.1.1 概念说明

软件（openwifi 驱动）要控制 FPGA 里的 IP，必须能「写配置、读状态」。这通过**寄存器**完成：每个 IP 内部有一组 32 位寄存器（如 xpu 的退避参数、tx_intf 的发送使能），软件像访问内存一样按地址读写它们。

- IP 一侧实现的是 **AXI4-Lite 从设备**（Slave），在源码里通常叫 `xxx_s_axi.v`（见 [u7-l1 AXI 寄存器映射](u7-l1-axi-register-map.md)）。
- PS 一侧用 **GP 主端口**（`M_AXI_GP1`）发起读写。
- 一个主端口要连多个从设备，中间需要一个 **AXI Interconnect**（互连）做「1 主 → N 从」的地址分发。

#### 4.1.2 核心流程

```
PS M_AXI_GP1  ──►  openwifi_ip/S00_AXI (层级从口)
                         │
                  axi_interconnect_1 (1 主 → 7 从)
                         │ 分成 7 路
        ┌────────┬────────┬────────┬────────┬────────┬────────┬────────┐
       M00      M01      M02      M03      M04      M05      M06
     dma0_lite tx_intf  ofdm_tx  dma1_lite rx_intf  ofdm_rx   xpu
       (TX)                        (RX)
```

软件一次寄存器读写的握手过程：

1. PS 把目标地址放 `AWADDR`/`ARADDR`，拉高 `VALID`。
2. 互连根据地址译码，选中 7 个从设备之一，转发请求。
3. 被选中的 `*_s_axi` 返回 `READY` 与数据（读）或写响应（写）。
4. PS 收到 `BVALID`/`RVALID`，一次寄存器事务完成。

#### 4.1.3 源码精读

寄存器通路在接线脚本里只有一行——把 PS 的 GP 主端口接到 openwifi_ip 层级的从口：

[connect_openwifi_ip.tcl:9](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/connect_openwifi_ip.tcl#L9) 把 PS 通用主端口 `M_AXI_GP1` 连到 `openwifi_ip/S00_AXI`（寄存器从口）。

那 `S00_AXI` 进了层级之后怎么分给 7 个从设备？看层级蓝图：

[openwifi_ip.tcl:288](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L288) 层级从口 `S00_AXI` 实际接的是 `axi_interconnect_1/S00_AXI`。

[openwifi_ip.tcl:179-184](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L179-L184) `axi_interconnect_1` 被配置成 **`NUM_MI {7}`**（7 个主出），即「1 进 7 出」。

[openwifi_ip.tcl:278-284](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L278-L284) 这 7 路分别接到 7 个从设备的 `s00_axi`/`S_AXI_LITE`：

| 互连输出 | 连到 | 含义 |
|----------|------|------|
| `M00_AXI` | `axi_dma_0/S_AXI_LITE` | **TX** DMA 控制寄存器 |
| `M01_AXI` | `tx_intf_0/s00_axi` | tx_intf 寄存器 |
| `M02_AXI` | `openofdm_tx_0/s00_axi` | openofdm_tx 寄存器 |
| `M03_AXI` | `axi_dma_1/S_AXI_LITE` | **RX** DMA 控制寄存器 |
| `M04_AXI` | `rx_intf_0/s00_axi` | rx_intf 寄存器 |
| `M05_AXI` | `openofdm_rx_0/s00_axi` | openofdm_rx 寄存器 |
| `M06_AXI` | `xpu_0/s00_axi` | xpu 控制寄存器 |

> 这就是为什么 u7-l1 会讲「寄存器映射」：软件通过 `M_AXI_GP1` 看到的是一段连续地址空间，按地址段被分发到这 7 组寄存器。每组的 `*_s_axi.v` 里用 `slv_reg0..N` 落地具体配置位。

#### 4.1.4 代码实践

**实践目标**：亲手验证「一个 GP 主端口 → 7 个寄存器从设备」的拓扑。

**操作步骤**：

1. 打开 `ip/openwifi_ip.tcl`，定位 `axi_interconnect_1` 的 `NUM_MI`（约 L184）。
2. 向下找到 L278–L284 的 7 条 `connect_bd_intf_net ... M0X_AXI` 连线。
3. 对每一条，记下「互连输出引脚 → 目标从设备」。
4. 打开 `ip/connect_openwifi_ip.tcl` 第 9 行，确认 PS 侧入口是 `M_AXI_GP1`。

**需要观察的现象**：7 个从设备里，`axi_dma_0` 与 `axi_dma_1` 是 Xilinx 自带 AXI DMA IP（有自己的 `S_AXI_LITE` 控制口），其余 5 个是 openwifi 自研 IP（`s00_axi`）。

**预期结果**：你应当得到上面那张 7 行表格。寄存器通路「只此一条入口（S00_AXI），内部分 7 路」。

#### 4.1.5 小练习与答案

**练习 1**：如果软件要新增一个自研 IP（比如 side_ch），寄存器通路要怎么改？

**参考答案**：`axi_interconnect_1` 的 `NUM_MI` 要从 7 加到 8，新增一路 `M07_AXI → side_ch_0/s00_axi`，并为 side_ch 分配新的地址段（在 BD 里给该从口设 `ATTR.SLAVE_BASEADDR`/`HIGHADDR`）。注意 UltraScale+ 版层级里 side_ch 已默认包含。

**练习 2**：为什么寄存器通路用 GP 端口，而不是 HP/ACP？

**参考答案**：寄存器读写每次只有一两个 32 位字，对带宽要求极低，但要求地址译码简单、延迟可预测。GP 端口正为此设计；HP/ACP 是给大批量 DMA 数据流准备的，且 ACP 还会绕经 L2 缓存，对寄存器访问既无必要又增加延迟。

---

### 4.2 最小模块二（上）·DMA 数据通路：收发数据走 ACP / HP3

#### 4.2.1 概念说明

寄存器通路太慢，搬不动一整帧 WiFi 数据。真正搬包靠 **DMA（Direct Memory Access）**：openwifi 层级里有**两个** Xilinx `axi_dma` IP（`axi_dma_0`、`axi_dma_1`），它们作为**主设备**直接读写 DDR，把待发包从内存搬进 PL、把收到的包搬回内存。

每个 `axi_dma` 有两个方向：

- **MM2S**（Memory-Mapped to Stream）：从 DDR 读数据 → 变成 AXI-Stream 送进 IP。（发方向）
- **S2MM**（Stream to Memory-Mapped）：把 IP 产生的 AXI-Stream → 写回 DDR。（收方向）

两个 DMA 与两条数据链的对应关系（在 [u2-l2](u2-l2-openwifi-ip-hierarchy.md) 已建立）：

- `axi_dma_0` ↔ **tx_intf**：MM2S 把待发数据送进 `tx_intf/s00_axis`；`tx_intf` 也会把 TX 状态经 `m00_axis`（S2MM）写回内存。
- `axi_dma_1` ↔ **rx_intf**：`rx_intf/m00_axis`（S2MM）把收到的帧写回内存；MM2S 给 `rx_intf/s00_axis` 送配置/控制数据。

#### 4.2.2 核心流程

两个 DMA 的「内存侧」（`M_AXI_MM2S`/`M_AXI_S2MM`/`M_AXI_SG`）各经一个互连，汇聚到层级的两个主口，再接到 PS 的两个 Slave 端口：

```
TX 链 (axi_dma_0):  M_AXI_MM2S/S2MM/SG ─► axi_interconnect_0 ─► M00_AXI1 ─► PS S_AXI_HP3
RX 链 (axi_dma_1):  M_AXI_MM2S/S2MM/SG ─► axi_interconnect_2 ─► M00_AXI   ─► PS S_AXI_ACP
```

为什么要分两个端口？

- **RX（接收）走 ACP**：收到的包要立刻交给 CPU 协议栈处理。ACP 经 L2 缓存，DMA 写入后 CPU 读到的是缓存一致的数据，省去手动 cache flush/invalidate，降低延迟。
- **TX（发射）走 HP3**：待发数据由 CPU 提前准备好、刷进 DDR，PL 只管读出来发。HP 直连 DDR、带宽大、不绕缓存，适合这种「写好后批量读」的场景。

#### 4.2.3 源码精读

接线脚本把两个 DMA 主口分别接到 PS 的 ACP 和 HP3：

[connect_openwifi_ip.tcl:1-2](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/connect_openwifi_ip.tcl#L1-L2) 第 1 行 `M00_AXI → S_AXI_ACP`；第 2 行 `M00_AXI1 → S_AXI_HP3`。

注意：脚本里只写了「层级主口 → PS 端口」这一段；至于 `M00_AXI`/`M00_AXI1` 内部分别服务于哪个 DMA，要看层级蓝图：

[openwifi_ip.tcl:285-286](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L285-L286) 层级主口 `M00_AXI` 接 `axi_interconnect_2/M00_AXI`；`M00_AXI1` 接 `axi_interconnect_0/M00_AXI`。

[openwifi_ip.tcl:270-277](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L270-L277) `axi_interconnect_2` 的三个从入（`S00/S01/S02_AXI`）全部来自 `axi_dma_1`（RX DMA 的 `M_AXI_MM2S`/`S2MM`/`SG`）。

[openwifi_ip.tcl:271-274](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L271-L274) `axi_interconnect_0` 的三个从入全部来自 `axi_dma_0`（TX DMA 的 `M_AXI_MM2S`/`S2MM`/`SG`）。

于是闭环：

| 层级主口 | 经互连 | 服务的 DMA | 方向 | 接 PS 端口 |
|----------|--------|-----------|------|-----------|
| `M00_AXI` | `axi_interconnect_2` | `axi_dma_1`（RX） | 收包 | **`S_AXI_ACP`** |
| `M00_AXI1` | `axi_interconnect_0` | `axi_dma_0`（TX） | 发包 | **`S_AXI_HP3`** |

顺带一提，DMA 的数据位宽与突发：

[openwifi_ip.tcl:145-156](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L145-L156) `axi_dma_0` 配置成 64 位数据宽（`c_m_axi_mm2s_data_width=64`）、使能 scatter-gather（`c_include_sg=1`）、突发 256（`c_mm2s_burst_size=256`），并开启 DRE（动态地址对齐）。`axi_dma_1` 同款配置（L159–L169）。`c_m_axis_mm2s_tdata_width=64` 与 tx_intf/rx_intf 的 `TDATA_NUM_BYTES=8`（64 位）对齐，这正是下一篇 [u3/u4](u3-l1-rx-intf-overview.md) 会讲到的「64bit AXI-Stream 字」。

> **关于时钟**：[connect_openwifi_ip.tcl:19-22](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/connect_openwifi_ip.tcl#L19-L22) 把层级的 `m_axi_mm2s_aclk` 与 PS 三个 AXI 端口的 `*_ACLK` 都接到同一个时钟网络（名为 `FCLK_CLK2`），保证 PS 与 PL 两侧 AXI 同频、无需跨时钟域。需要说明的是：在已提交的 zc706 板 `system.bd` 里，这个名为 `sys_ps7_FCLK_CLK2` 的网络实际由时钟向导 `clk_wiz_0/clk_out1` 驱动（`sys_ps7/FCLK_CLK2` 先喂给 `clk_wiz_0`），脚本是其简化表达——同一原则：**整条 AXI 数据通路共用一个时钟**。

#### 4.2.4 代码实践

**实践目标**：确认收发数据各走哪个 PS 端口，并理出两个 DMA 的内存侧汇聚关系。

**操作步骤**：

1. 读 `ip/connect_openwifi_ip.tcl` 第 1、2 行，记下两个层级主口的目标 PS 端口。
2. 打开 `ip/openwifi_ip.tcl`，找到 L285–L286（层级主口 ↔ 互连）与 L270–L277（互连 ↔ DMA）。
3. 判定 `M00_AXI`、`M00_AXI1` 各自背后是哪个 `axi_dma`。
4. 结合 L272（`axi_dma_0/M_AXIS_MM2S → tx_intf_0/s00_axis`）与 L287（`axi_dma_1/S_AXIS_S2MM ← rx_intf_0/m00_axis`）判断哪个 DMA 是 TX、哪个是 RX。

**需要观察的现象**：`axi_interconnect_0` 三个从入全是 `axi_dma_0`，`axi_interconnect_2` 三个从入全是 `axi_dma_1`；互连与 DMA 是一一对应的。

**预期结果**：

- 发射（TX）：DDR → `axi_dma_0` → `axi_interconnect_0` → 层级 `M00_AXI1` → PS **`S_AXI_HP3`**。
- 接收（RX）：`rx_intf` → `axi_dma_1` → `axi_interconnect_2` → 层级 `M00_AXI` → PS **`S_AXI_ACP`**。

5.（可选，需 Vivado）若你能打开某板工程，在 BD 里点中 `openwifi_ip/M00_AXI`，应看到它连到 `sys_ps7/S_AXI_ACP`，验证脚本与 BD 一致。

#### 4.2.5 小练习与答案

**练习 1**：为什么不让 RX 也走 HP3？

**参考答案**：HP 不保证缓存一致。若 RX 走 HP，DMA 把包写进 DDR 后，CPU 可能从自己的 L1/L2 读到旧数据，必须显式做 cache invalidate，既容易出错又增加延迟。ACP 自动一致，更适合「收完即处理」的 RX。

**练习 2**：`axi_interconnect_0` 与 `axi_interconnect_2` 为什么都配成 `NUM_SI {3}`？

**参考答案**：每个 `axi_dma` 在内存侧有三条主出：`M_AXI_MM2S`（读数据）、`M_AXI_S2MM`（写数据）、`M_AXI_SG`（读散布描述符 scatter-gather）。三条都要访问 DDR，所以用一个 3 从入的互连把它们汇聚成一个层级主口，再接 PS。

---

### 4.3 最小模块二（下）·中断通路：sys_concat_intc 七路拼接

#### 4.3.1 概念说明

PL 发生事件时（收到一帧、发完一帧、DMA 完成一轮），不能让软件去轮询，而要**主动中断** CPU。但 Zynq PS 提供给 PL 的中断管脚很有限：PS7 的 `IRQ_F2P[15:0]` 是一个 **16 位**向量，即最多 16 个中断源。openwifi 要上报的中断有 7 路，但 PS 不可能给每路单独的物理管脚。

解决办法是一个 **`xlconcat`（位拼接器）IP**，在本工程里命名为 `sys_concat_intc`：它有 16 个 1 位输入 `In0..In15`，拼成一个 16 位输出 `dout[15:0]`，整体送给 PS 的 `IRQ_F2P`。PS 的 GIC（通用中断控制器）再把每一位映射成一个中断号交给 CPU。

#### 4.3.2 核心流程

```
openwifi_ip 的 7 个中断输出         sys_concat_intc (xlconcat, 16 位)        PS
   rx_pkt_intr ───────────────────► In1 ┐
   mm2s_introut1 ─────────────────► In2 │
   s2mm_introut ──────────────────► In3 ├─► dout[15:0] ─► IRQ_F2P ─► GIC ─► CPU
   tx_itrpt0 ─────────────────────► In4 │
   tx_itrpt1 ─────────────────────► In5 │
   mm2s_introut ──────────────────► In6 │
   s2mm_introut1 ─────────────────► In7 ┘
   (In0, In8..In15 未用，接常量 0)
```

每一位在 `dout` 中的位置就是它的「中断号槽位」。软件驱动按这个槽位号注册 ISR（中断服务程序）。中断位 \(i\) 到 `dout` 第 \(i\) 位的对应关系可写成：

\[
\text{dout}[i] = \text{In}_i,\qquad i \in \{0,1,\dots,15\}
\]

拼接输出即各输入的按位或组合：

\[
\text{dout} = \sum_{i=0}^{15} \text{In}_i \cdot 2^i
\]

#### 4.3.3 源码精读

`sys_concat_intc` 的「身份」：在板 BD 里它是 Xilinx `xlconcat` IP，配成 16 端口。

[zc706_fmcs2/src/system.bd:4616-4629](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/zc706_fmcs2/src/system.bd#L4616-L4629) `sys_concat_intc` 的 `vlnv` 是 `xilinx.com:ip:xlconcat:2.1`，参数 `NUM_PORTS=16`、`dout_width=16`。

接线脚本把 openwifi 的 7 个中断「改接」到拼接器的 In1–In7。脚本用了一个固定套路——先 `delete_bd_objs` 删掉 ADI 参考设计里这些槽位的默认连线，再 `connect_bd_net` 接上 openwifi 的中断：

[connect_openwifi_ip.tcl:23-36](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/connect_openwifi_ip.tcl#L23-L36) 7 次「删除旧网 + 接入 openwifi 中断」的成对操作。完整对应关系：

| 槽位 | openwifi_ip 输出 | 源头（层级内部，见 openwifi_ip.tcl） | 含义 |
|------|------------------|--------------------------------------|------|
| In1 | `rx_pkt_intr` | `rx_intf_0/rx_pkt_intr`（L317） | **收到一个有效帧** |
| In2 | `mm2s_introut1` | `axi_dma_1/mm2s_introut`（L316） | RX DMA 的 MM2S 完成 |
| In3 | `s2mm_introut` | `axi_dma_1/s2mm_introut`（L318） | RX DMA 的 S2MM 完成 |
| In4 | `tx_itrpt0` | `tx_intf_0/tx_itrpt0`（L320） | TX 中断 0 |
| In5 | `tx_itrpt1` | `tx_intf_0/tx_itrpt1`（L321） | TX 中断 1 |
| In6 | `mm2s_introut` | `axi_dma_0/mm2s_introut`（L315） | TX DMA 的 MM2S 完成 |
| In7 | `s2mm_introut1` | `axi_dma_0/s2mm_introut`（L319） | TX DMA 的 S2MM 完成 |

（openwifi_ip.tcl 的行号见 [L315-L321](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L315-L321)。）

脚本最后一行收尾：

[connect_openwifi_ip.tcl:37](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/connect_openwifi_ip.tcl#L37) `save_bd_design` 保存 block design。

> **板 BD 的版本差异（如实说明）**：上面的 7 路映射是 `ip/connect_openwifi_ip.tcl`（参考脚本）的内容。在已提交的 zc706 板 BD 快照里，`openwifi_ip` 暴露的是**单个** `tx_itrpt`（接 In5），In4 留空；即板 BD 是稍早的版本，参考脚本是最新的「双 TX 中断」版本。两者主体一致，仅 TX 中断粒度不同。读源码时以 `ip/` 下的脚本为准，板 BD 作交叉验证。

#### 4.3.4 代码实践

**实践目标**：把 7 个中断输出、它们的源头、拼接槽位三者对齐。

**操作步骤**：

1. 读 `ip/connect_openwifi_ip.tcl` L23–L36，列出 7 个「In → openwifi_ip 输出」对。
2. 打开 `ip/openwifi_ip.tcl` L315–L321，查出每个层级中断输出背后是 `axi_dma_0/1` 还是 `rx_intf_0`/`tx_intf_0`。
3. 按「TX 相关 / RX 相关」给 7 个中断分组。
4. 在 zc706 的 `system.bd` 里搜索 `sys_concat_intc/In1`、`.../In6` 等，核对板 BD 与脚本是否一致（注意上面提到的 TX 中断差异）。

**需要观察的现象**：`delete_bd_objs [get_bd_nets ps_intr_XX_1]` 这类行说明 ADI 原始设计本就给每个 In 槽位留了一条占位网，openwifi 是「替换」而非「新建」。

**预期结果**：得到上面那张 7 行表，并能指出 **In1（rx_pkt_intr）是软件「收到帧」的核心中断**，收包主路径由它驱动。

#### 4.3.5 小练习与答案

**练习 1**：如果 openwifi 再多一路中断（比如 side_ch 的捕获完成），`sys_concat_intc` 还够用吗？

**参考答案**：够。`NUM_PORTS=16`，目前只用了 In1–In7（In0、In8–In15 空闲）。新增中断接 In8 即可，无需改拼接器宽度；只要软件驱动按新槽位注册 ISR。

**练习 2**：为什么脚本要先 `delete_bd_objs` 再 `connect_bd_net`，而不是直接连？

**参考答案**：ADI 参考设计的 `system.bd` 已经把 `sys_concat_intc/In4..In7` 等槽位连到了它自己的默认中断源（网名 `ps_intr_04_1` 等）。Vivado 规定一个输入引脚只能属于一条网，所以必须先删除原网，再接入 openwifi 的中断，否则会报「pin already connected」。

---

### 4.4 进阶对照·UltraScale+ 版本：PS8、两套中断与注释掉的重连

#### 4.4.1 概念说明

把 openwifi 搬到 Zynq UltraScale+（如 zcu102，PS 称 `sys_ps8`）时，PS 的 AXI 端口名与中断架构都变了：

- 端口名带 `_FPD`（Full Power Domain）后缀，如 `S_AXI_ACP_FPD`、`S_AXI_HP3_FPD`、`M_AXI_HPM0_FPD`。
- 中断不再是单个 16 位向量，而是分成两组：`pl_ps_irq0`、`pl_ps_irq1`，分别送 GIC 的两个区域。openwifi 在 UltraScale+ 版层级里**内部**用 `xlconcat_0`/`xlconcat_1` 先把中断汇聚好，再送 PS。

因此第二份脚本 `connect_openwifi_ip_ultra_scale.tcl` 的寄存器/数据/时钟接线和普通版一一对应，但**中断那段被整段注释掉了**。

#### 4.4.2 核心流程

```
普通 Zynq (PS7):   openwifi_ip 7 中断 ──(connect 脚本手动重连)──► sys_concat_intc ─► IRQ_F2P
UltraScale+ (PS8): openwifi_ip 内部 xlconcat_0/1 ──(层级自带)──► sys_concat_intc_0/1 ─► pl_ps_irq0/1 ─► GICv3
```

#### 4.4.3 源码精读

寄存器与数据通路，命名换 `_FPD`，语义不变：

[connect_openwifi_ip_ultra_scale.tcl:1-4](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/connect_openwifi_ip_ultra_scale.tcl#L1-L4) `M00_AXI → S_AXI_ACP_FPD`、`M00_AXI1 → S_AXI_HP3_FPD`、`S00_AXI → M_AXI_HPM0_FPD`。收发仍分别是 ACP / HP3。

时钟换成 `pl_clk2`：

[connect_openwifi_ip_ultra_scale.tcl:11-14](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/connect_openwifi_ip_ultra_scale.tcl#L11-L14) 层级 AXI 时钟与三个 PS8 端口时钟都接 `sys_ps8/pl_clk2`。

中断段被注释：

[connect_openwifi_ip_ultra_scale.tcl:16-29](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/connect_openwifi_ip_ultra_scale.tcl#L16-L29) 普通版那 7 行 `delete/connect` 在这里全部以 `#` 注释掉，拼接器名换成 `sys_concat_intc_0`。

原因在层级蓝图里：zcu102 板 BD 显示 UltraScale+ 版 `openwifi_ip` **内部**已经实例化了 `xlconcat_0`、`xlconcat_1`（见 [zcu102_fmcs2/src/system.bd](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/zcu102_fmcs2/src/system.bd) 的 `openwifi_ip/xlconcat_0`、`openwifi_ip/xlconcat_1`），PS 侧也用两个拼接器 `sys_concat_intc_0`、`sys_concat_intc_1`（板 BD 内）分别对接 `pl_ps_irq0/1`。也就是说 UltraScale+ 版的中断在层级内部就已经分好组、经层级对外引脚直接进 PS，所以这份「外接脚本」不需要再手动重连，相关行被保留作注释以备对照。

#### 4.4.4 代码实践

**实践目标**：对比两套脚本，理解 PS7→PS8 的命名与中断结构变化。

**操作步骤**：

1. 把 `connect_openwifi_ip.tcl` 与 `connect_openwifi_ip_ultra_scale.tcl` 并排打开。
2. 逐行对照第 1、2、4（或 9）行的端口名，记录 `_FPD`/`HPM0` 等差异。
3. 对照两份脚本的中断段：普通版是 7 行有效 `connect`，UltraScale+ 版是 7 行 `#` 注释。
4.（可选）打开 zcu102 的 `system.bd`，搜索 `openwifi_ip/xlconcat`，确认中断在层级内部已拼接。

**需要观察的现象**：两套脚本的「寄存器 + 数据 + 时钟」部分几乎一一对应，只有中断段处理方式不同。

**预期结果**：能说清「UltraScale+ 把中断拼接上移到了 openwifi_ip 层级内部 + PS8 双 IRQ 组，所以外接脚本里那段重连被注释」。

#### 4.4.5 小练习与答案

**练习 1**：`M_AXI_HPM0_FPD` 对应普通版的哪个端口？

**参考答案**：对应普通 Zynq 的 `M_AXI_GP1`——都是 PS 当主设备、用来访问 PL 寄存器的通用主端口；UltraScale+ 里它属于全功率域（FPD），故名 `M_AXI_HPM0_FPD`。

**练习 2**：如果要在 UltraScale+ 版上调试某个收包中断收不到的问题，应该去哪里看中断拼接？

**参考答案**：因为外接脚本注释了重连，应直接看 `openwifi_ip` 层级内部的 `xlconcat_0`/`xlconcat_1`（在 `openwifi_ip_ultra_scale.tcl` / zcu102 BD 里），以及 PS 侧的 `sys_concat_intc_0/1` 到 `pl_ps_irq0/1` 的连线，而非 `connect_openwifi_ip_ultra_scale.tcl`。

## 5. 综合实践

**任务**：为 openwifi 的 PS-PL 互连画一张完整的「三类通路」接线表，并回答一个排查问题。

1. **寄存器通路**：从 `ip/openwifi_ip.tcl` 的 L278–L284 抄出 7 个从设备，标注每个是「自研 IP」还是「Xilinx DMA」。指出软件经哪一个 PS 端口访问它们。
2. **数据通路**：分别画出 TX 与 RX 的「IP/DMA → 互连 → 层级主口 → PS 端口」链路，标出 ACP 与 HP3。
3. **中断通路**：列出 7 个中断到 `sys_concat_intc` 的槽位，标出哪个是「收到帧」核心中断。
4. **排查情境**：假设 zc706 上「能发包但收不到任何包」。基于本讲的三类通路，你会优先检查哪条链路的哪一段？给出至少两个候选检查点。

**参考思路（排查部分）**：

- RX 数据链：`rx_intf/m00_axis → axi_dma_1(S2MM) → axi_interconnect_2 → M00_AXI → S_AXI_ACP`。检查 `axi_dma_1` 是否使能 S2MM、ACP 端口地址段是否正确、缓存一致性是否处理。
- RX 中断：`rx_pkt_intr → sys_concat_intc/In1 → IRQ_F2P`。检查软件是否按正确的中断号注册了收包 ISR、`rx_pkt_intr` 是否真的被拉高（可用 ILA 抓波形，见 [u7-l6](u7-l6-gpio-led-ila-debug.md)）。

> 若没有硬件，本实践为「源码阅读型」：用上面三张表对照 `connect_openwifi_ip.tcl` 与 `openwifi_ip.tcl`，确认每一行连线都能在源码里找到出处，即算完成。

## 6. 本讲小结

- openwifi 的 PS-PL 互连由三类通路构成：**寄存器（AXI4-Lite）、数据（AXI DMA）、中断（拼接送 GIC）**。
- 寄存器通路：PS `M_AXI_GP1` → `openwifi_ip/S00_AXI` → `axi_interconnect_1`（1→7）分发给 axi_dma_0、tx_intf、openofdm_tx、axi_dma_1、rx_intf、openofdm_rx、xpu 七组寄存器。
- 数据通路：TX 走 `axi_dma_0 → interconnect_0 → M00_AXI1 → S_AXI_HP3`；RX 走 `axi_dma_1 → interconnect_2 → M00_AXI → S_AXI_ACP`——**发用 HP3、收用 ACP**，分别取舍带宽与缓存一致性。
- 中断通路：7 个 openwifi 中断经 `sys_concat_intc`（16 位 `xlconcat`）的 In1–In7 拼成一个向量送 PS `IRQ_F2P`，其中 `In1 = rx_pkt_intr` 是收包核心中断。
- `connect_openwifi_ip.tcl` 是 openwifi_ip 层级接到普通 Zynq（PS7）的接线配方；`connect_openwifi_ip_ultra_scale.tcl` 是 UltraScale+（PS8）版，端口带 `_FPD`、时钟用 `pl_clk2`，中断因在层级内部已拼接而被注释。
- 这份脚本是迁移（README「Migrate」Method 2）时把 openwifi_ip 重新接入新 ADI 设计的「桥」，各板 `system.bd` 是其固化结果。

## 7. 下一步学习建议

- 沿**寄存器通路**深入：去 [u7-l1 AXI 寄存器映射与软件交互](u7-l1-axi-register-map.md)，看 `xpu_s_axi.v` 等 `*_s_axi.v` 如何把 `slv_reg` 落地成具体配置位，以及软件如何按地址读写它们。
- 沿**数据通路**深入：进入 [u3 接收链路](u3-l1-rx-intf-overview.md) 与 [u4 发射链路](u4-l1-openofdm-tx-overview.md)，看 `rx_intf`/`tx_intf` 如何产生与消费 64bit AXI-Stream，再交给这两个 DMA。
- 沿**控制通路**深入：去 [u5-l1 xpu 控制核心总览](u5-l1-xpu-overview.md)，看 xpu 如何在 PL 内部协调 CSMA/CA、重传、ACK——这些最终都通过本讲的寄存器通路被软件配置、通过中断通路回报事件。
- 想做板级移植或升级到新 Vivado/ADI 版本时，回头结合 README 的「Migrate」小节与本讲的两套脚本，理解层级迁移后如何重连 PS。

# 设备变种对比：x4、以太网、FT2232 与 CaptainDMA 家族

## 1. 本讲目标

本讲是「设备变种与二次开发」单元的开篇。前面五个单元我们一直以 **PCIeSquirrel**（x1 + FT601）这一单一工程为样本，建立了 com→fifo→pcie 三大子系统的完整认知。真实仓库里其实并存着十余个「形状不同、骨架相同」的设备工程。

读完本讲，你应当能够：

1. 看懂 x1 与 x4 两种 PCIe 工程在**顶层端口**与**核心封装模块**（`pcileech_pcie_a7` vs `pcileech_pcie_a7x4`）上的差异，并理解为什么 x4 在内核侧不再需要 64↔128 位宽转换。
2. 掌握**通信核心的可替换性**：FT601（USB3）、RMII 以太网、FT2232H（USB2）、Thunderbolt FPGA IO 桥四类物理通路如何共用同一份 `IfComToFifo` 契约。
3. 读懂 **CaptainDMA 家族**按 FPGA 型号（35T/75T/100T）、封装（325/484）与 PCIe 通道（x1/x4）划分子目录的命名规律。
4. 具备把一个新设备的顶层与现有 Squirrel 工程做「端口级 diff」的能力，为下一讲的板卡移植打基础。

## 2. 前置知识

在进入变种对比前，先回顾两个贯穿全讲的关键概念（详细版见 u1-l4、u3-l1）：

- **顶层模块（top）**：每个设备目录里那个唯一的 `*_top.sv`，是 FPGA 对外的「总接线板」。它把物理引脚（时钟、LED、PCIe 金手指、USB/网口焊盘）连到三大子系统（com/fifo/pcie），并声明 5 个 interface 实例把它们串起来。换板卡 = 改顶层 + 改约束。
- **核心封装模块（pcie_a7 / pcileech_com）**：夹在「标准 interface 契约」与「具体物理 IP/芯片」之间的适配层。契约（`IfComToFifo`、`IfPCIeFifoTlp` 等）不变，适配层可换——这正是设备变种能大规模复用同一套 fifo/mux/cfg/tlp 源码的根本原因。

一个直觉比喻：pcileech-fpga 像一套「**主板 + 可换 CPU + 可换网卡**」的 PC。fifo 是主板上的芯片组（永远居中、永远不变），pcie 核是 CPU（有 x1 单核 / x4 四核两款），com 是网卡（有 USB3 / 以太网 / USB2 / 雷电四种「网卡驱动」）。本讲就是这张「配件清单」的横向对比。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [readme.md](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/readme.md) | 全仓库设备总表，列出所有支持/遗留设备、连接方式、速率、FPGA 型号、PCIe 版本。 |
| [PCIeSquirrel/src/pcileech_squirrel_top.sv](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv) | x1 + FT601 基准工程的顶层，本讲作为「对照组」。 |
| [ZDMA/100T/src/pcileech_tbx4_100t_top.sv](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/ZDMA/100T/src/pcileech_tbx4_100t_top.sv) | x4 + Thunderbolt 桥工程的顶层，本讲作为「x4 变种样本」。 |
| [ZDMA/100T/src/pcileech_pcie_a7x4.sv](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/ZDMA/100T/src/pcileech_pcie_a7x4.sv) | x4 版 PCIe 核心封装，对比 PCIeSquirrel 的 `pcileech_pcie_a7.sv`。 |
| [NeTV2/src/pcileech_com.sv](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/NeTV2/src/pcileech_com.sv) | 用条件编译在 FT601 与 RMII 以太网之间切换的 com 模块，演示「通信核心可替换」。 |
| [NeTV2/src/pcileech_eth.sv](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/NeTV2/src/pcileech_eth.sv) | RMII 以太网通信核心，UDP 收发 + DHCP/静态 IP。 |
| [acorn_ft2232h/src/pcileech_com.sv](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/acorn_ft2232h/src/pcileech_com.sv) | FT2232H/FT245 的 8 位 com 变体，对比 FT601 的 32 位。 |
| [CaptainDMA/readme.md](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/CaptainDMA/readme.md) | CaptainDMA 家族说明，含设备—FPGA 工程—固件版本对照表。 |

## 4. 核心概念与源码讲解

### 4.1 设备变种全景与「可替换核心」的设计哲学

#### 4.1.1 概念说明

仓库根目录的 `readme.md` 用两张表把所有设备列得清清楚楚：当前支持设备表与遗留设备表。每行一个设备，关键字段是 **连接方式（Connection）**、**传输速率（Transfer Speed）**、**FPGA 型号**、**PCIe 版本（gen2 x1 / x4）**。

设备虽然多，但它们的「骨架」完全一致——都是 com→fifo→pcie 三大子系统。差别只在两个「可插拔」的位置：

1. **PCIe 核**：`pcileech_pcie_a7`（x1）或 `pcileech_pcie_a7x4`（x4），取决于工程把 Xilinx `pcie_7x_0` IP 配成几通道、几比特数据接口。
2. **通信核心**：连攻击者主机那一端，可能是 FT601（USB3 并行 32 位）、RMII 以太网、FT2232H（USB2 并行 8 位）或 Thunderbolt FPGA IO 桥（BUS_DO/BUS_DI）。

`fifo` 这个居中调度者**永远不变**——这正是为什么 u2 单元花五讲深挖的 MAGIC 路由、mux 打包、寄存器文件在所有设备上都通用。

#### 4.1.2 核心流程

把 readme 设备表按两个维度归类，可以得到一张「变种矩阵」：

```
                 │ 通信核心 = FT601(USB3) │ 以太网      │ FT2232H(USB2) │ Thunderbolt桥
─────────────────┼────────────────────────┼─────────────┼───────────────┼──────────────
PCIe x1          │ Squirrel, CaptainDMA   │             │               │ GBOX(x1)
PCIe x4          │ ac701, CaptainDMA M2x4 │ NeTV2       │ acorn         │ ZDMA
```

为什么 readme 反复强调「**即便硬件支持更多 lane，固件默认也只用 PCIe x1**」？因为整条数据通路的瓶颈在主机侧的 USB/雷电链路，而不在 PCIe 侧。简单算一下 PCIe gen2 的单向可用带宽：

\[
\text{带宽} = 5\,\text{GT/s} \times N_{\text{lane}} \times \frac{8}{10}\,(\text{去 8b/10b 编码}) \div 8\,(\text{bit}\to\text{Byte})
\]

代入 x1：\(5 \times 1 \times 0.8 / 8 = 0.5\,\text{GB/s} = 500\,\text{MB/s}\) 理论值，扣掉 TLP 头开销后实际可用约 \(400\,\text{MB/s}\)。而 USB3 的理论极限也只有 \(400\,\text{MB/s}\) 量级、PCILeech 实测稳定在 \(190\,\text{MB/s}\) 左右。所以 \(190 \ll 400\)，PCIe x1 绰绰有余，x4 对纯 DMA 读写并无提速——只有 ZDMA（雷电3、\(1000\,\text{MB/s}\)）和 GBOX（OCuLink 直连、最高 \(400\,\text{MB/s}\)）才真正吃满了 x4 带宽。

#### 4.1.3 源码精读

设备总表见 [readme.md:L12-L23](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/readme.md#L12-L23)，其中 ZDMA 是 `Thunderbolt3 / 1000 MB/s / XC7A100T-484 / PCIe gen2 x4`，是全表最快的设备；CaptainDMA M2 标注 `PCIe gen2 x1-x4`，意为同一硬件可烧 x1 或 x4 两份固件。

「x1 即足够」的官方说明在 [readme.md:L25](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/readme.md#L25)（表下脚注 `*)`）。

遗留设备表 [readme.md:L57-L64](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/readme.md#L57-L64) 里能看到 NeTV2（`UDP/IP / 7 MB/s / PCIe gen2 x4*`）与 Acorn（`USB2 / 25 MB/s / PCIe gen2 x4*`），正是本讲要对比的以太网与 FT2232H 变种（带 `*` 表示其实可跑 x4）。

#### 4.1.4 代码实践

**实践目标**：用设备总表亲手验证「瓶颈在主机侧、不在 PCIe 侧」这一论断。

**操作步骤**：

1. 打开 [readme.md:L12-L23](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/readme.md#L12-L23)。
2. 找出所有 `Connection == USB-C/USB3` 且 `PCIe Version` 含 `x4` 的行。
3. 把它们的 `Transfer Speed` 与纯 x1 设备（如 Squirrel `190 MB/s`）对比。

**需要观察的现象**：CaptainDMA M2 同时提供 x1 与 x4 固件，但 readme 明确写「x1 与 x4 速度相近，因为被 USB 连接卡住了」。

**预期结果**：USB 类设备的速率都聚拢在 \(190\text{–}220\,\text{MB/s}\)，与 lane 数几乎无关；只有放弃 USB、改用雷电/OCuLink 直连的 ZDMA/GBOX 才能突破到 \(280\text{–}1000\,\text{MB/s}\)。

#### 4.1.5 小练习与答案

**练习 1**：readme 里 `PCIe gen2 x1-x4` 的写法（如 GBOX、CaptainDMA M2）说明什么？

**答案**：同一块硬件、同一份 FPGA 工程，可被配置/综合成 x1 或 x4 两种 PCIe 宽度；具体取决于烧录哪份固件、以及 IP 核 `pcie_7x_0` 在 Vivado 里被设成的 lane 数。

**练习 2**：为什么 NeTV2 用 x4 的 PCIe 核，速率却只有 \(7\,\text{MB/s}\)？

**答案**：因为它的主机侧通信核心是 100M 以太网 UDP（见 4.3），\(100\,\text{Mb/s} \approx 12.5\,\text{MB/s}\) 再扣协议开销远低于 PCIe x4 的容量，瓶颈完全在以太网一端，PCIe 用 x4 只为兼容 NeTV2 板卡的物理走线。

---

### 4.2 pcileech_pcie_a7x4：x4 PCIe 核心封装

#### 4.2.1 概念说明

`pcileech_pcie_a7x4` 是 x4 设备（ZDMA、GBOX、CaptainDMA M2 x4、ac701）的 PCIe 核心封装，对应 u3-l1 讲过的 x1 版本 `pcileech_pcie_a7`。二者职责完全一样——封装 Xilinx 7 系列 PCIe 硬核 `pcie_7x_0`，对外暴露 4 个 interface（`dfifo_cfg`/`dfifo_tlp`/`dfifo_pcie`/`dshadow2fifo`），对内分发 IP 端口。

唯一的实质区别是 **IP 的数据接口位宽**：x1 工程把 `pcie_7x_0` 配成 64 位 AXIS，x4 工程配成 128 位 AXIS。这导致两侧的「位宽适配子模块」不同。

#### 4.2.2 核心流程

```
              x1 工程 (pcileech_pcie_a7)              x4 工程 (pcileech_pcie_a7x4)
              ──────────────────────────              ─────────────────────────────
IP 接口位宽    64-bit AXIS                             128-bit AXIS
接收方向       m_axis_rx(64b) → src64 (64→128) → tlps_rx    m_axis_rx(128b) → src128 (128→128 整形) → tlps_rx
发送方向       tlps_tx → dst64 (128→64) → s_axis_tx(64b)   tlps_tx → dst128 (128→128 整形) → s_axis_tx(128b)
lane 数        pcie_tx/rx_p/n [0:0]                    pcie_tx/rx_p/n [3:0]
```

注意 x4 侧的 `src128`/`dst128` 名字里虽带「128」，但**不是位宽转换**——它们只是把硬核的 `IfPCIeTlpRx128`（22 位 tuser 编码的 SOF/EOF/BAR-hit）格式整理成工程内部统一的 `IfAXIS128`（tkeepdw/tlast/tuser[8:2]）。真正做位宽折半/翻倍的只有 x1 侧的 `src64`/`dst64`。

#### 4.2.3 源码精读

x4 封装的端口声明把 lane 显式写成 4 对差分，见 [pcileech_pcie_a7x4.sv:L13-L34](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/ZDMA/100T/src/pcileech_pcie_a7x4.sv#L13-L34)：`pcie_tx_p/pcie_tx_n [3:0]`、`pcie_rx_p/pcie_rx_n [3:0]`，而 x1 版本是 `[0:0]`（单 lane）。

复位仍分软/硬两条线，与 u3-l1 一致：[pcileech_pcie_a7x4.sv:L54-L55](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/ZDMA/100T/src/pcileech_pcie_a7x4.sv#L54-L55)，`rst_subsys`（软，刷 cfg/tlp 逻辑不动链路）与 `rst_pcie`（硬，连根拔起硬核）。

关键差异在位宽适配：x4 用 [pcileech_tlps128_src128 ... L87-L92](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/ZDMA/100T/src/pcileech_pcie_a7x4.sv#L87-L92) 与 [pcileech_tlps128_dst128 ... L106-L111](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/ZDMA/100T/src/pcileech_pcie_a7x4.sv#L106-L111)；而 x1 的 `pcileech_pcie_a7` 用 `src64`/`dst64`（见 [PCIeSquirrel/src/pcileech_pcie_a7.sv:L87](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L87) 与 [L106](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L106)）。

x4 工程里 `pcie_7x_0` 的收发数据直接是 128 位，见 [pcileech_pcie_a7x4.sv:L132-L145](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/ZDMA/100T/src/pcileech_pcie_a7x4.sv#L132-L145)：`s_axis_tx_tdata [127:0]`、`m_axis_rx_tdata [127:0]`。

#### 4.2.4 代码实践（本讲核心实践任务）

**实践目标**：把 x1 顶层 `pcileech_squirrel_top.sv` 与 x4 顶层 `pcileech_tbx4_100t_top.sv` 做一次端口级 diff，亲手找出 x4 多出的 lane 引脚与雷电桥接口。

**操作步骤**：

1. 并排打开 [pcileech_squirrel_top.sv:L13-L52](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L13-L52) 与 [pcileech_tbx4_100t_top.sv:L13-L46](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/ZDMA/100T/src/pcileech_tbx4_100t_top.sv#L13-L46)。
2. 逐行比对端口列表，把差异填进下表（推荐自己先填，再核对下方「预期结果」）。

| 类别 | Squirrel (x1) | TBx4 (x4) |
| --- | --- | --- |
| 系统时钟 | `clk` + `ft601_clk`（两个独立时钟） | `clk_in`（单 50MHz，再经 `clk_wiz_0` 倍频） |
| PCIe lane | `pcie_tx/rx_p/n [0:0]` | `pcie_tx/rx_p/n [3:0]` |
| PCIe 在位/复位 | `pcie_present`、`pcie_perst_n`（各 1 根） | `pcie_present1/2`、`pcie_perst1_n/2_n`（各 2 根，双路径） |
| 主机通信 | FT601：`ft601_data[31:0]` + be/rxf/txe/wr/... | 雷电桥：`BUS_DO[40:0]`、`BUS_DI[66:0]`、`BUS_DO_CLK`、`BUS_DI_PROG_FULL`、`TB_CONNECT` |
| PCIe 核例化 | `pcileech_pcie_a7` | `pcileech_pcie_a7x4` |

3. 在 x4 顶层找到 `pcie_tx_p [3:0]` 的声明（[pcileech_tbx4_100t_top.sv:L36-L41](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/ZDMA/100T/src/pcileech_tbx4_100t_top.sv#L36-L41)），确认 lane0~3 四对差分。
4. 找到雷电桥接口声明（[pcileech_tbx4_100t_top.sv:L29-L34](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/ZDMA/100T/src/pcileech_tbx4_100t_top.sv#L29-L34)）：`BUS_DO[40:0]` 是桥→FPGA 的数据+控制、`BUS_DI[66:0]` 是 FPGA→桥。

**需要观察的现象**：x4 工程不直接出现 `ft601_*` 任何引脚；通信全部经 `BUS_DO`/`BUS_DI` 这组「抽象并行 FIFO」接口完成，FPGA 本身不参与雷电协议。

**预期结果**：x4 顶层多出 3 对 PCIe lane 差分（lane1/2/3）、双份 present/perst、以及整组 `BUS_DO/BUS_DI` 雷电桥接口；同时多出 `TB_CONNECT`（雷电在位检测）与 `POWER_SW_MODE` 省电逻辑。

> 说明：本实践为「源码阅读型」，无需运行综合；如要进一步验证，可在 Vivado 打开两个工程的 `.xdc`，比对 `PACKAGE_PIN` 行数差异。

#### 4.2.5 小练习与答案

**练习 1**：为什么 x4 工程用 `src128/dst128` 而 x1 用 `src64/dst64`？是否因为 x4 数据量是 x1 的 4 倍所以要 4 倍位宽？

**答案**：不是「4 倍」。`pcie_7x_0` IP 在 x4 配置下每拍本来就是 128 位 AXIS（x1 配置下是 64 位），所以 x4 侧的 `src128/dst128` 是 128→128 的**格式整形**（抽取 22 位 tuser 里的 SOF/EOF/BAR-hit 重组为 `IfAXIS128`），并不改变位宽；x1 侧的 `src64/dst64` 才是真位宽转换（64↔128）。

**练习 2**：x4 顶层 `pcie_present = pcie_present1 && pcie_present2`（[L72](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/ZDMA/100T/src/pcileech_tbx4_100t_top.sv#L72)）为什么是「与」两根线？

**答案**：雷电方案下 PCIe 通路经过桥芯片延伸，板卡用两根独立的 present 信号分别表示「目标机在位」与「雷电链路在位」，二者同时有效才认为 PCIe 目标真正可达，故相与。

---

### 4.3 通信核心的可替换性：FT601 / 以太网 / FT2232H / Thunderbolt 桥

#### 4.3.1 概念说明

所有设备的 `pcileech_com` 模块在面向 fifo 的一侧都呈现**同一个契约** `IfComToFifo.mp_com`（64 位下行 + 256 位上行 + 反压位，见 u2-l1）。差别只在面向物理芯片/网口的另一侧——这就是「**通信核心可替换**」：把外部物理层换掉，只要它最终能把数据凑成 64 位并经双时钟 FIFO 跨到 `clk` 域，fifo 侧完全无感知。

仓库里能见到四类通信核心：

1. **FT601（USB3，32 位并行）**：Squirrel、CaptainDMA、ScreamerM2、ac701、pciescreamer、sp605。
2. **RMII 以太网（UDP）**：NeTV2，由独立的 `pcileech_eth` 模块实现。
3. **FT2232H/FT245（USB2，8 位并行）**：acorn，8 位拼 64 位。
4. **Thunderbolt FPGA IO 桥（BUS_DO/BUS_DI）**：ZDMA、GBOX，由桥芯片代劳雷电协议、对 FPGA 呈现一组并行 FIFO。

#### 4.3.2 核心流程

无论哪种核心，com 模块内部都遵循同一条流水线（以接收方向为例）：

```
物理芯片并行数据 (N 位)
   │  ① 按 N 位宽度拼装成 64 位（FT601: 32→64 拼两拍；FT2232H: 8→64 拼 8 拍；以太网: 字节流凑 4 字节）
   │     期间用同步字(0x66665555 等)做边界重同步
   ▼
   双时钟 FIFO (clk_com → clk)   ── 跨时钟域，前提 2*clk_com < clk
   │
   ▼
   dfifo.com_dout[63:0]  →  交给 fifo 做 MAGIC 路由
```

发送方向对称：fifo 回送的 256 位大包先拆成 32 位（或 8 位）再喂给物理芯片。NeTV2 的 `pcileech_com` 用 Verilog 条件编译 `` `ifdef ENABLE_ETH `` / `` `ifdef ENABLE_FT601 `` 在**同一份源码**里二选一，是观察「可替换」最直观的样本。

#### 4.3.3 源码精读

NeTV2 的 com 在文件头用宏选择通信核心：[NeTV2/src/pcileech_com.sv:L12-L13](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/NeTV2/src/pcileech_com.sv#L12-L13) 启用 `` `define ENABLE_ETH ``、注释掉 `` ENABLE_FT601 ``；模块端口里 FT601 信号与 ETH 信号被 `` `ifdef `` 分隔，见 [L15-L54](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/NeTV2/src/pcileech_com.sv#L15-L54)。ETH 分支例化 `pcileech_eth`：[L217-L245](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/NeTV2/src/pcileech_com.sv#L217-L245)，把 RMII 物理引脚（`eth_rx_data`、`eth_crs_dv`、`eth_tx_en` 等）与同一份 `com_rx_data32`/`core_din` 数据线对接。

`pcileech_eth` 自身是 RMII + UDP 栈：端口见 [pcileech_eth.sv:L12-L45](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/NeTV2/src/pcileech_eth.sv#L12-L45)；DHCP/静态地址策略（先 DHCP 10 秒，超时回退静态）见 [L64-L73](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/NeTV2/src/pcileech_eth.sv#L64-L73)；接收方向把 UDP 字节流每 4 字节拼成一个 32 位字（`dout_RxValid4 == 4'b1111`）见 [L79-L94](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/NeTV2/src/pcileech_eth.sv#L79-L94)。

FT2232H 变体则是 8 位并行：`ft245_data[7:0]` 端口见 [acorn_ft2232h/src/pcileech_com.sv:L22-L29](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/acorn_ft2232h/src/pcileech_com.sv#L22-L29)；8→64 位拼装（拼 8 拍、用 `8'h55`/`56'h66665555666655` 重同步）见 [L72-L93](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/acorn_ft2232h/src/pcileech_com.sv#L72-L93)。对比 FT601 版本是 32→64 拼 2 拍——逻辑同构、只是拼装拍数不同。

Thunderbolt 桥的 com 又是另一变体：它不再有 `ft601_*`，而是接 `BUS_DO/BUS_DI` 与分离的 `clk_comtx/clk_comrx`（收发各自独立时钟），例化见 [pcileech_tbx4_100t_top.sv:L124-L141](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/ZDMA/100T/src/pcileech_tbx4_100t_top.sv#L124-L141)。

#### 4.3.4 代码实践

**实践目标**：验证「四类通信核心在 fifo 侧接口完全一致」。

**操作步骤**：

1. 在仓库中分别打开四个 com 文件：`PCIeSquirrel/src/pcileech_com.sv`（FT601）、`NeTV2/src/pcileech_com.sv`（ETH）、`acorn_ft2232h/src/pcileech_com.sv`（FT245）、`ZDMA/100T/src/pcileech_com.sv`（雷电桥）。
2. 在每个文件里搜索 `IfComToFifo.mp_com dfifo`（fifo 侧端口）。
3. 搜索 `initial_rx` 数组，找到末条 `64'h00000003_80182377`。

**需要观察的现象**：四份源码面向 fifo 的端口声明与上电注入命令**逐字符相同**；不同的只是物理引脚区与字宽拼装段。

**预期结果**：这证明「换通信核心」是一次**外科手术式的局部替换**——保留 fifo 侧契约与上电序列，只改物理层适配，综合即可得到新设备固件。若任一文件找不到上述两处，标注「待本地确认」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 FT2232H 的重同步字是 `56'h66665555666655` 而 FT601 是 `32'h66665555`？

**答案**：重同步字长度 = 拼装位宽。FT601 是 32 位拼 64 位（2 拍），用一个 32 位 `0x66665555` 连续两拍即可对齐；FT2232H 是 8 位拼 64 位（8 拍），需要 8 个字节的同步模式，故用更长的 `0x66665555666655`（结合当前字节 `0x55`）来唯一确定 64 位边界。

**练习 2**：NeTV2 用 `` `define ENABLE_ETH `` 而不是直接删掉 FT601 代码，这种条件编译写法有什么好处？

**答案**：同一份 `pcileech_com.sv` 既能为以太网设备综合，也能（取消两行注释）为 FT601 设备综合，便于在两种硬件间快速切换、共享 bug 修复，避免源码分叉。

---

### 4.4 CaptainDMA 家族的子目录组织

#### 4.4.1 概念说明

大多数设备「一设备一目录」（如 `PCIeSquirrel/`、`NeTV2/`、`ZDMA/100T/`）。CaptainDMA 是个例外——它是「**家族**」，因为同一品牌下有六款不同 FPGA 型号、不同封装、不同 PCIe 通道的板卡。仓库用 `CaptainDMA/<FPGA型号><封装>_<通道>/` 的命名把每款板卡独立成子工程。

#### 4.4.2 核心流程

子目录命名规律：`<型号><封装>_<lane>`

```
35t325_x1   →  Artix-7 XC7A35T、325 球封装、PCIe x1   (CaptainDMA M2 x1)
35t325_x4   →  Artix-7 XC7A35T、325 球封装、PCIe x4   (CaptainDMA M2 x4)
35t484_x1   →  Artix-7 XC7A35T、484 球封装、PCIe x1   (CaptainDMA 4.1th)
75t484_x1   →  Artix-7 XC7A75T、484 球封装、PCIe x1   (CaptainDMA 75T)
100t484-1   →  Artix-7 XC7A100T、484 球封装、PCIe x1  (CaptainDMA 100T / M2 100T)
```

每个子目录都是一个**自洽的 Vivado 工程**：含自己的 `src/`（含设备特定 `*_top.sv` 与 `*.xdc`）、`ip/`、`.tcl` 脚本。`35t325_x4` 的 src 里能看到 `pcileech_pcie_a7x4.sv`（x4 封装）与 `pcileech_35t325_x4_top.sv`（专用顶层），而 x1 子目录则用 `pcileech_pcie_a7.sv`。

#### 4.4.3 源码精读

CaptainDMA 家族概览与「M2 同时提供 x1/x4 两份固件」的说明见 [CaptainDMA/readme.md:L5-L17](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/CaptainDMA/readme.md#L5-L17)；固件—FPGA 工程对照表见 [L77-L84](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/CaptainDMA/readme.md#L77-L84)，每行明确标注对应的 FPGA Project 名（如 `35t325_x4`、`75t484_x1`）。

`35t325_x4` 子目录的 src 内容确认了它是 x4 工程：含 [pcileech_pcie_a7x4.sv](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/CaptainDMA/35t325_x4/src/pcileech_pcie_a7x4.sv) 与 `pcileech_35t325_x4_top.sv`，同时保留 `pcileech_tlps128_bar_controller.sv`、`pcileech_tlps128_cfgspace_shadow.sv`——说明它是较新代际、带 BAR 设备仿真能力的工程（见 u4 单元）。

烧录方式上，CaptainDMA 家族统一用 **CH347 FPGA Tool**（区别于 PCIeSquirrel 的 OpenOCD），见 [CaptainDMA/readme.md:L21](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/CaptainDMA/readme.md#L21) 与 [L31](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/CaptainDMA/readme.md#L31)。

#### 4.4.4 代码实践

**实践目标**：用 `git ls-files` 把握 CaptainDMA 家族的全貌，验证命名规律。

**操作步骤**：

1. 在仓库根目录执行（只读 git 命令）：
   ```bash
   git ls-files 'CaptainDMA/*_top.sv'
   git ls-files 'CaptainDMA/*/pcileech_pcie_a7*.sv'
   ```
2. 把输出按子目录归类，对照 4.4.2 的命名表。

**需要观察的现象**：每个 x1 子目录里是 `pcileech_pcie_a7.sv`，唯一的 x4 子目录（`35t325_x4`）里是 `pcileech_pcie_a7x4.sv`；每款板卡有自己专属的 `*_top.sv`。

**预期结果**：`35t325_x1`、`35t484_x1`、`75t484_x1`、`100t484-1` 用 a7；`35t325_x4` 用 a7x4。`100t484-1`（注意是连字符而非下划线）对应 100T，目录命名的小不一致也值得留意。若本地仓库与上游 HEAD 有差异，以实际 `git ls-files` 输出为准。

#### 4.4.5 小练习与答案

**练习 1**：为什么 CaptainDMA 要拆成 5 个子目录，而不是像 Squirrel 那样一个目录？

**答案**：因为六款板卡用了三种 FPGA（35T/75T/100T）、两种封装（325/484）、两种 lane（x1/x4），综合时器件型号（如 `xc7a35t` vs `xc7a100t`）、引脚约束、PCIe IP 配置都不同。每个子目录锁死一种器件+约束组合，互不干扰，便于分别构建与发布固件。

**练习 2**：CaptainDMA M2 x4 固件版本是 4.15，其他都是 4.14，为什么？

**答案**：据 readme 说明（[L73](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/CaptainDMA/readme.md#L73)），4.15 含一个「仅适用于 x4 设备」的 bug 修复，所以只有 x4 固件升版，x1 固件维持 4.14。

---

## 5. 综合实践

**任务**：为一张「假设的 x4 + USB3」新板卡挑选基准工程，并起草它的顶层端口清单。

背景：假设你拿到一块新板卡——FPGA 为 Artix-7 XC7A100T-484、PCIe 走 x4、主机侧用 FT601 USB3、单 PCIe 槽（单 present/perst）、板载 100MHz 系统时钟。请综合本讲四个模块的知识完成：

1. **选基准**：在仓库现有工程里，哪个最接近？（提示：要 x4 + FT601 + 100T，可参考 `CaptainDMA/35t325_x4` 与 `ZDMA/100T` 与 `ac701_ft601`）。说明你舍弃其他选项的理由（如 ZDMA 是雷电桥不是 FT601）。
2. **端口清单**：参考 [pcileech_tbx4_100t_top.sv:L13-L46](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/ZDMA/100T/src/pcileech_tbx4_100t_top.sv#L13-L46) 的 x4 PCIe 部分 + [pcileech_squirrel_top.sv:L13-L52](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L13-L52) 的 FT601 部分，拼出新顶层应有的端口：`clk`、`ft601_clk`、`pcie_tx/rx_p/n [3:0]`、`pcie_clk_p/n`、`pcie_present`、`pcie_perst_n`、`ft601_data[31:0]` 等一整套。
3. **核心选择**：写出新顶层应例化的 PCIe 封装模块名（`pcileech_pcie_a7x4`）与 com 模块应启用的宏（FT601 版 `pcileech_com`，不需 `ENABLE_ETH`）。
4. **自检**：说明为什么这个组合仍能复用仓库现成的 `pcileech_fifo.sv`、`pcileech_mux.sv`、`pcileech_pcie_cfg_a7.sv`、`pcileech_pcie_tlp_a7.sv` 而无需改动（答：它们只依赖不变的 interface 契约）。

预期产出：一份端口表 + 一段「为何这样选」的 200 字说明。本实践不需要真的综合，重点训练「按需求拼装现有模块」的设备变种思维——这正是下一讲「移植到新板卡」的核心能力。

## 6. 本讲小结

- 设备虽多，骨架唯一：所有工程都是 com→fifo→pcie，**fifo 永远不变**，可变的只有 PCIe 核（x1/x4）与通信核心（FT601/以太网/FT2232H/雷电桥）两个「插槽」。
- x1 与 x4 的本质差别是 `pcie_7x_0` IP 的 AXIS 位宽（64 vs 128）：x1 用 `src64/dst64` 做真位宽转换，x4 用 `src128/dst128` 做纯格式整形；lane 数从 `[0:0]` 变 `[3:0]`。
- 通信核心「可替换」的根基是 fifo 侧统一的 `IfComToFifo` 契约与同一份 `initial_rx` 上电序列；NeTV2 用 `` `ifdef ENABLE_ETH `` 在同一份源码里二选一是最直观样本。
- 由于瓶颈在主机侧 USB/雷电链路（\(190\,\text{MB/s}\) 量级）远低于 PCIe x1 容量（可用 \(\approx 400\,\text{MB/s}\)），固件默认 x1 即足够；x4 仅对雷电/OCuLink 直连的 ZDMA/GBOX 有意义。
- CaptainDMA 是「家族」式组织：`<型号><封装>_<lane>` 命名把六款板卡拆成 5 个独立子工程，烧录统一走 CH347。
- x4 顶层（以 TBx4 为例）相比 Squirrel 多出 3 对 lane 差分、双份 present/perst、整组 `BUS_DO/BUS_DI` 雷电桥接口、`TB_CONNECT` 与 `POWER_SW_MODE` 省电逻辑。

## 7. 下一步学习建议

- **下一讲 u6-l2（移植到新板卡）**：把本讲的「按需求拼装」能力落到一份完整移植 checklist 上——重点学 xdc 引脚重映射、PCIe GTP 通道定位、参考时钟与 PERST/PRSNT 等硬件依赖。
- **延伸阅读**：对比 [GBOX/readme.md](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/GBOX/readme.md) 的 OCuLink 双 PCIe 口方案（PC1 目标机 + PC2 控制机），理解「PCIe-over-PCIe」为何能把带宽推到 \(400\,\text{MB/s}\)。
- **源码深挖**：若想真正吃透 x4，精读 [pcileech_tlps128_src128](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/ZDMA/100T/src/pcileech_pcie_a7x4.sv#L311-L372) 里 `rxd_data_qw` 暂存上半四字的逻辑——它揭示了 128 位硬核在「SOF 落在上半字」时仍需跨拍对齐的细节，是 u3-l5 位宽转换讲义的 x4 补充。
- **回看**：若对「为什么换核心不影响 fifo」仍有疑惑，建议重读 u2-l1（interface 契约）与 u2-l3（fifo MAGIC 路由），它们是本讲所有「可替换」结论的底层保证。

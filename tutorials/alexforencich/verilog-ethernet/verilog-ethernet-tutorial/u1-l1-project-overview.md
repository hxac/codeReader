# 项目定位与能力总览

## 1. 本讲目标

本讲是 verilog-ethernet 学习手册的第一篇。读完本讲，你应当能够：

- 用一两句话说清楚 **verilog-ethernet 是什么**、它解决什么问题。
- 说出它支持的 **以太网速率（1G / 10G / 25G）** 与 **数据通路宽度（8 位 / 64 位）**。
- 认清项目里 **四类顶层模块** 的命名约定（`eth_mac_*`、`eth_phy_10g`、`ip_complete`、`udp_complete`），并知道每一类负责什么。
- 了解本项目 **已被 taxi 仓库接替** 的弃用背景，明白“为什么还要学它”。

本讲只做“建立全局认知”的工作，不展开任何具体模块的内部实现。后续每一篇讲义都会从本讲建立的地图里选取一个点深入。

## 2. 前置知识

本讲是入门第一篇，几乎不需要你预先掌握任何东西。下面几个名词会反复出现，先有个印象即可，后面遇到再细讲：

- **以太网（Ethernet）**：局域网里最常用的数据链路层技术。一根网线两端交换的“帧”，以及如何在这些帧里寻址、校验，都属于以太网的范畴。
- **MAC（Media Access Control）**：媒体访问控制子层。简单理解为“负责把数据封装成以太网帧、并按规则发送/接收的那块硬件逻辑”。
- **PHY（Physical Layer）**：物理层。负责把数字的 0/1 变成能在铜线或光纤上传输的电/光信号。MAC 之上是协议，PHY 之下是物理介质。
- **IP / UDP / ARP**：互联网协议（IP）、用户数据报协议（UDP）、地址解析协议（ARP）。它们是运行在以太网之上的网络层与传输层协议。
- **PTP（Precision Time Protocol）**：精确时间协议，用于让网络里的设备拥有高度一致的时间，常用于工业、金融、5G 等对时间精度敏感的场景。
- **FPGA**：现场可编程门阵列。本项目是一套 **用 Verilog 写的硬件 IP**，最终会被综合成 FPGA（或 ASIC）上的电路，而不是在 CPU 上跑的软件。
- **AXI-Stream**：一种在硬件模块之间“按流水线传递一帧数据”的标准接口约定。本库几乎所有模块都用它收发数据，下一讲会专门讲。

如果你对“硬件描述语言 Verilog”完全陌生，只要记住：**我们看到的每一个 `.v` 文件，描述的是一块电路，而不是一段会被 CPU 逐行执行的程序。**

## 3. 本讲源码地图

本讲主要只读一个文件——项目的 `README.md`。它既是说明书，也是本库最权威的“功能清单”。

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/README.md) | 项目总说明：定位、弃用公告、各模块文档、源码清单、接口约定、测试方法 |

为佐证 README 里提到的模块确实存在，本讲会点到 `rtl/` 目录下的几个代表性文件（仅确认存在、不展开内部）：

| 代表文件 | 所属类别 |
| --- | --- |
| `rtl/eth_mac_1g.v` | 千兆 MAC |
| `rtl/eth_mac_10g.v` | 10G/25G MAC |
| `rtl/eth_phy_10g.v` | 10G/25G PHY |
| `rtl/eth_mac_phy_10g.v` | MAC/PHY 合一模块 |
| `rtl/ip_complete.v` | 千兆 IPv4 协议栈 |
| `rtl/udp_complete.v` | 千兆 UDP 协议栈 |

整个 `rtl/` 目录下共有 98 个 `.v` 源文件（外加 `.py` 生成器脚本），是一个相当大的 IP 库。本讲不要求你记住这些文件，只要知道“按类别去 `rtl/` 里找”即可。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**项目整体能力**、**顶层模块命名约定**、**支持速率与数据通路宽度**。

### 4.1 项目整体能力

#### 4.1.1 概念说明

verilog-ethernet 是 Alex Forencich 维护的一个 **开源以太网 Verilog IP 库**。它的目标是用一套可综合的 RTL 代码，覆盖一块网卡（NIC）在 FPGA 上需要的大多数硬件逻辑：

- 收发以太网帧的 **MAC**。
- 处理物理信号的 **PHY**（10G/25G 的 PCS/PMA）。
- 运行在以太网之上的 **ARP / IPv4 / UDP 协议栈**。
- 实现 **精确时间同步（PTP）** 的时钟与时间戳组件。
- 一整套基于 cocotb 的 **仿真测试平台**。

换句话说，如果你想在一块 FPGA 开发板上做一个“能联网、能收发 UDP 报文”的设计，这个库把从物理层到 UDP 层的大部分积木都备好了，你只需要把它们像搭积木一样组装起来，再加上自己的应用逻辑。

一个非常重要的背景：**这个仓库目前已经废弃（deprecated）**。作者明确说明，所有新功能和修复都会转到继任仓库 [taxi](https://github.com/fpganinja/taxi)。

- 那为什么还要学它？因为 taxi 正是“在 verilog-ethernet 基础上演化出来的下一代”，**两者的架构、模块划分、接口约定高度相似**。读懂 verilog-ethernet，等于读懂了 taxi 的大部分设计思想；而且 verilog-ethernet 历史更久、资料和示例更丰富，是理解这套以太网 IP 设计的最佳起点。

#### 4.1.2 核心流程

从“用户想发一个 UDP 报文”到“报文出现在网线上”，这个库提供的能力可以概括为这条自顶向下的通路：

```
应用逻辑  ──►  UDP 栈 (udp_complete)  ──►  以太网成帧
            (UDP + IP + ARP)              (目的/源 MAC + 类型 + 载荷)
                                              │
                                              ▼
                                            MAC (eth_mac_*)  ──►  PHY (eth_phy_10g)  ──►  网线/光纤
```

- **横向**：应用层 → 传输层（UDP）→ 网络层（IP）→ 地址解析（ARP）→ 链路层（以太网帧）→ MAC → PHY。
- **纵向**：每一层在本库里都对应一组 `.v` 文件，可以单独拿来用，也可以用 `*_complete` 这种顶层模块把它们打包好。

本库的价值正在于此：**每一层既能单独使用，又有现成的“全家桶”顶层**，使用门槛很低。

#### 4.1.3 源码精读

**弃用公告**——README 开头即声明本仓库已被 taxi 取代：

> This repository is superseded by https://github.com/fpganinja/taxi. All new features and bug fixes will be applied there... this repo is deprecated and will not receive any future maintenance or support.

详见 [README.md:9-11](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/README.md#L9-L11)（这段点明：本仓库不再维护，新功能转至 taxi）。

**项目能力总述**——README 的 Introduction 一段把库能做什么讲得很清楚：

详见 [README.md:13-22](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/README.md#L13-L22)（这段说明了：覆盖 gigabit / 10G / 25G，8 位与 64 位数据通路，包含以太网帧、IP/UDP/ARP、完整 UDP/IP 栈、千兆与 10G/25G 的 MAC、10G/25G 的 PCS/PMA PHY、10G/25G 的 MAC/PHY 合一模块、各种 PTP 组件，以及基于 cocotbext-eth 的完整 cocotb 仿真平台）。

要点摘录（中文意译）：

- 「Collection of Ethernet-related components for gigabit, 10G, and 25G packet processing (8 bit and 64 bit datapaths)」——支持三档速率、两种数据宽度。
- 「the components for constructing a complete UDP/IP stack」——提供了搭出完整 UDP/IP 栈所需的全部组件。
- 「full cocotb testbenches that utilize cocotbext-eth」——附带完整的仿真测试，这一点对学习极其重要：你几乎可以为每个模块跑仿真看实际波形。

#### 4.1.4 代码实践

**实践目标**：通过亲手在仓库里找文件，建立“这个库真的实现了 README 所说的东西”的直观感受。

**操作步骤**：

1. 打开本仓库的 `rtl/` 目录（在工作区里就是 `rtl/`）。
2. 用 `ls rtl/ | wc -l` 数一下一共有多少个源文件。
3. 对照 README 的「Source Files」清单（[README.md:429-512](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/README.md#L429-L512)），确认你能在 `rtl/` 下找到 `eth_mac_1g.v`、`eth_phy_10g.v`、`ip_complete.v`、`udp_complete.v`、`ptp_clock.v` 这几个文件。

**需要观察的现象**：

- `rtl/` 下有几十个 `.v` 文件，命名上能看出明显的类别前缀（`eth_mac_*`、`eth_phy_*`、`ip_*`、`udp_*`、`ptp_*`）。

**预期结果**：

- 文件总数约为 98 个 `.v` 文件（外加 `.py` 生成器脚本），与 README 清单一致。

**待本地验证**：如果你在本机拉取仓库，请自行运行上面的命令核对数量；不同提交下数字可能略有变化。

#### 4.1.5 小练习与答案

**练习 1**：README 说这个仓库“deprecated”，那还有必要花时间学它吗？

> **参考答案**：有必要。因为继任仓库 taxi 正是基于本库演进而来，架构与接口高度相似；且 verilog-ethernet 示例与测试更丰富，是理解整套以太网 IP 设计的最佳切入点。学习时只要记住“新项目应优先用 taxi”即可。

**练习 2**：本库除了协议和 MAC/PHY，还提供了哪一类“与报文收发无直接关系”的重要组件？举一个模块名。

> **参考答案**：PTP 精确时间同步组件，例如 `ptp_clock`。

### 4.2 顶层模块命名约定

#### 4.2.1 概念说明

一个有近百个文件的库，初学者最怕“不知道该用哪个”。verilog-ethernet 通过一套 **清晰的命名约定** 来缓解这个问题。你只要记住四个“顶层”名字，就能快速定位到成品模块：

| 命名前缀 / 名字 | 类别 | 一句话用途 |
| --- | --- | --- |
| `eth_mac_*` | MAC | 以太网 MAC，把 AXI-Stream 帧封装成符合以太网规范的码流（含 FCS、帧间间隔等）。 |
| `eth_phy_10g` | PHY | 10G/25G 的 PCS/PMA 物理层，处理 64b/66b 编码、块同步等。 |
| `eth_mac_phy_10g` | MAC + PHY 合一 | 把 10G/25G 的 MAC 与 PHY 合并成一个顶层，对接 serdes。 |
| `ip_complete` / `ip_complete_64` | IP 协议栈 | IPv4 + ARP 一体化，开箱即用的 IP 层。 |
| `udp_complete` / `udp_complete_64` | UDP 协议栈 | UDP + IPv4 + ARP 一体化，开箱即用的 UDP 层。 |

其中带 `_64` 后缀的是 **64 位数据通路**（面向 10G/25G）版本；不带后缀的是 **8 位数据通路**（面向千兆）版本。这个后缀约定非常关键，下一节细讲。

另外还有两个常见后缀值得现在就知道：

- `_fifo`：表示该模块在前后 **加入了 FIFO**，用于跨时钟域或缓冲。例如 `eth_mac_1g_fifo`、`eth_mac_1g_rgmii_fifo`。
- 接口名：`_gmii`、`_rgmii`、`_mii`、`_xgmii` 表示对外使用哪种物理接口约定。

#### 4.2.2 核心流程

当你拿到一个需求，可以按下面这条决策树挑选顶层模块：

```
我要做什么？
│
├─ 只想收发以太网帧（自己处理上层协议）
│     └─► 选 eth_mac_*（按速率与接口选具体型号）
│
├─ 需要 IP 层（能 ping、能和别的设备走 IPv4）
│     └─► 选 ip_complete（1G）或 ip_complete_64（10G/25G）
│
├─ 需要 UDP（应用直接收发 UDP 报文）
│     └─► 选 udp_complete（1G）或 udp_complete_64（10G/25G）
│
├─ 在 10G/25G 且想要 MAC+PHY 一步到位
│     └─► 选 eth_mac_phy_10g（含 _fifo 变体）
│
└─ 想要带跨时钟域缓冲的成品
      └─► 优先选带 _fifo 后缀的版本
```

速率决定后缀（带不带 `_64`），接口决定中间词（`_gmii` / `_rgmii` / `_xgmii`），是否要缓冲决定结尾（`_fifo`）。

#### 4.2.3 源码精读

README 在 Introduction 之后用一段话直接点出了四个顶层名字：

详见 [README.md:24-33](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/README.md#L24-L33)。这段说明了：

- 只需要 IP+ARP：用 `ip_complete`（1G）或 `ip_complete_64`（10G/25G）。
- 需要 UDP+IP+ARP：用 `udp_complete`（1G）或 `udp_complete_64`（10G/25G）。
- 顶层 MAC 是 `eth_mac_*`（多种接口，有/无 FIFO 两种）。
- 顶层 10G/25G PHY 是 `eth_phy_10g`。
- 顶层 10G/25G MAC/PHY 合一是 `eth_mac_phy_10g`。

下面挑四个文档小节，确认每个顶层模块的官方描述（这些都是 README「Documentation」章节里实际存在的条目）：

- **千兆 MAC** `eth_mac_1g`：「Gigabit Ethernet MAC with GMII interface.」见 [README.md:152-154](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/README.md#L152-L154)。
- **10G/25G MAC** `eth_mac_10g`：「10G/25G Ethernet MAC with XGMII interface. Datapath selectable between 32 and 64 bits.」见 [README.md:180-183](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/README.md#L180-L183)。
- **10G/25G PHY** `eth_phy_10g`：「10G/25G Ethernet PCS/PMA PHY.」见 [README.md:219-221](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/README.md#L219-L221)。
- **MAC/PHY 合一** `eth_mac_phy_10g`：「10G/25G Ethernet MAC/PHY combination module with SERDES interface.」见 [README.md:198-200](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/README.md#L198-L200)。
- **千兆 IP 栈** `ip_complete`：「IPv4 module with ARP integration. Top level for gigabit IP stack.」见 [README.md:258-262](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/README.md#L258-L262)。
- **千兆 UDP 栈** `udp_complete`：「UDP module with IPv4 and ARP integration. Top level for gigabit UDP stack.」见 [README.md:363-367](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/README.md#L363-L367)。

可以看到，这些顶层模块对应的 `.v` 文件在 `rtl/` 下都能找到：`rtl/eth_mac_1g.v`、`rtl/eth_mac_10g.v`、`rtl/eth_phy_10g.v`、`rtl/eth_mac_phy_10g.v`、`rtl/ip_complete.v`、`rtl/udp_complete.v`。

#### 4.2.4 代码实践

**实践目标**：把 README 里“抽象的模块名”和 `rtl/` 里“具体的文件”对应起来，建立命名直觉。

**操作步骤**：

1. 打开 [README.md 的 Source Files 清单](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/README.md#L429-L512)。
2. 在其中找到下面四个名字，并记下它们各自对应的 `.v` 文件路径：
   - 千兆 MAC 顶层
   - 10G/25G MAC 顶层
   - 千兆 UDP 栈顶层
   - 10G/25G PHY 顶层
3. 在 `rtl/` 目录里确认这些文件真实存在。

**需要观察的现象**：

- 每个顶层名都能在「Source Files」里找到一行形如 `rtl/xxx.v : 一句说明` 的条目。
- 带接口名的变体（如 `eth_mac_1g_gmii_fifo`）也都能在清单里找到。

**预期结果**：你应该能整理出类似下表（节选）：

| 顶层模块名 | 文件 | 说明 |
| --- | --- | --- |
| `eth_mac_1g` | `rtl/eth_mac_1g.v` | Gigabit Ethernet GMII MAC |
| `eth_mac_10g` | `rtl/eth_mac_10g.v` | 10G/25G Ethernet XGMII MAC |
| `udp_complete` | `rtl/udp_complete.v` | UDP stack (IP-ARP-UDP) |
| `eth_phy_10g` | `rtl/eth_phy_10g.v` | 10G/25G Ethernet PCS/PMA PHY |

**待本地验证**：如果在线浏览 GitHub，可直接点进 `rtl/` 目录核对。

#### 4.2.5 小练习与答案

**练习 1**：`eth_mac_1g` 和 `eth_mac_1g_fifo` 有什么区别？

> **参考答案**：前者是纯 MAC，后者在 MAC 前后集成了 FIFO，用于缓冲和/或跨时钟域，使用时对外多出 `logic_clk` 等逻辑侧时钟接口。

**练习 2**：如果我想要 10G 的完整 UDP 栈，应该选哪个顶层？

> **参考答案**：`udp_complete_64`（64 位数据通路、面向 10G/25G 的 UDP+IP+ARP 一体化顶层）。

### 4.3 支持速率与数据通路宽度

#### 4.3.1 概念说明

硬件设计和软件有一个关键不同：**数据总线的宽度（每个时钟周期传多少比特）是定死的**。在以太网 IP 里，这个宽度直接关系到“能不能跑到那么高的速率”。

verilog-ethernet 明确支持两档数据宽度、三档速率：

- **8 位数据通路**：面向 **千兆（1G，Gigabit）**。每个时钟周期传 1 字节，在 125 MHz 时钟下恰好达到 1 Gbps。
- **64 位数据通路**：面向 **10G / 25G**。每个时钟周期传 8 字节，在 156.25 MHz（10G）或 390.625 MHz（25G，常见配置）下达到对应速率。10G MAC 还支持在 32 位与 64 位之间选择。

为什么是这些数字？因为以太网速率本质上是一个 **“位宽 × 时钟频率”** 的乘积：

\[ \text{吞吐量 (bps)} = \text{数据宽度 (bit)} \times \text{时钟频率 (Hz)} \]

例如千兆：

\[ 8 \,\text{bit} \times 125\,\text{MHz} = 1000\,\text{Mbps} = 1\,\text{Gbps} \]

10G：

\[ 64 \,\text{bit} \times 156.25\,\text{MHz} = 10000\,\text{Mbps} = 10\,\text{Gbps} \]

所以“速率”和“数据通路宽度”在本库里是绑定在一起的——选了某个速率，基本就选定了对应的位宽和后缀约定。

#### 4.3.2 核心流程

本库用 **命名后缀** 把速率/位宽信息编码进模块名，让你一眼看出它的适用场景：

```
模块名末尾
│
├─ 无特殊后缀        ──► 8 位数据通路（千兆），例如 eth_mac_1g、ip_complete、udp_complete
│
├─ _64               ──► 64 位数据通路（10G/25G），例如 axis_xgmii_tx_64、ip_complete_64、udp_complete_64
│
└─ (10G MAC 内部)    ──► 还可在 32/64 位间参数化选择（DATA_WIDTH 参数）
```

在挑选模块时，自问一句：**“我做的是千兆还是 10G/25G？”** 答案直接决定你要不要带 `_64`。

需要注意，64 位通路下会多出一个重要信号 `tkeep`（标识最后一个字里哪些字节有效），而 8 位通路通常用不到它。这一点在下一讲讲 AXI-Stream 时会展开。

#### 4.3.3 源码精读

README 在 Introduction 的第一句就把速率和位宽讲清楚了：

> Collection of Ethernet-related components for gigabit, 10G, and 25G packet processing (8 bit and 64 bit datapaths).

详见 [README.md:14-16](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/README.md#L14-L16)（这句点明了三档速率与两种数据通路宽度）。

`_64` 后缀的语义，可以在文档里多处印证。例如 10G/25G 的 FCS 计算器文档明确写了“with 64 bit datapath for 10G/25G Ethernet”，见 [README.md:97-100](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/README.md#L97-L100)。10G MAC 文档也写明“Datapath selectable between 32 and 64 bits”，见 [README.md:180-183](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/README.md#L180-L183)。

「Common signals」一节也说明了 64 位通路特有信号 `tkeep` 的存在：

> tkeep : Data word valid (width generally KEEP_WIDTH, present on _64 modules)

详见 [README.md:420-427](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/README.md#L420-L427)（这里给出 AXI-Stream 公共信号清单，并标注 `tkeep` 只出现在 `_64` 模块上）。

#### 4.3.4 代码实践

**实践目标**：通过比对成对的 8 位 / 64 位模块，体会“速率与位宽”如何体现在文件名上。

**操作步骤**：

1. 在 `rtl/` 目录里，找出下面三对“8 位版 / 64 位版”的文件：
   - IP 收发：`ip_eth_rx.v` / `ip_eth_rx_64.v`
   - UDP 收发：`udp_ip_tx.v` / `udp_ip_tx_64.v`
   - 协议栈顶层：`ip_complete.v` / `ip_complete_64.v`
2. 在 README「Source Files」清单里找到这六个名字，确认其说明里是否含「64 bit」或「for 10G/25G」字样。

**需要观察的现象**：

- 几乎每一类协议层模块都有成对的 8 位版与 64 位版，64 位版文件名恒以 `_64` 结尾。

**预期结果**：例如 README 中：

- `rtl/ip_complete_64.v` 标注为「IPv4 stack (IP-ARP integration) (64 bit)」。
- `rtl/udp_ip_tx_64.v` 标注为「UDP frame transmitter (64 bit)」。

**待本地验证**：以上说明文字可在 [README.md 的 Source Files 清单](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/README.md#L429-L512) 中逐行核对。

#### 4.3.5 小练习与答案

**练习 1**：为什么千兆用 8 位、10G 用 64 位？能不能反过来？

> **参考答案**：因为吞吐量 = 位宽 × 时钟频率。千兆在 125 MHz 下用 8 位即可满足；10G 若用 8 位则需要 1.25 GHz 的时钟，FPGA 普通布线难以达到，因此改用更宽的 64 位、把时钟降到 ~156 MHz。反过来（千兆用 64 位）技术上可行，但会浪费资源，本库默认不这么做。

**练习 2**：64 位数据通路比 8 位多出一个关键的 AXI-Stream 信号，叫什么？它解决什么问题？

> **参考答案**：`tkeep`。它标记最后一个数据字里哪些字节是有效的，用来表示一帧在非整字边界上结束（例如一帧长度不是 8 的整数倍）。

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个 **“读 README，画项目地图”** 的小任务：

1. 通读 [README.md 的 Introduction（L13-L38）](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/README.md#L13-L38)。
2. 用一张表回答：本项目提供的 **四类顶层模块**（MAC、PHY、IP 栈、UDP 栈）各一个实例名是什么？每个用一句话说明用途。参考答案见下表。

| 类别 | 实例名 | 一句话用途 |
| --- | --- | --- |
| MAC | `eth_mac_1g` | 千兆以太网 MAC，对外提供 GMII 接口。 |
| PHY | `eth_phy_10g` | 10G/25G 的 PCS/PMA 物理层。 |
| IP 栈 | `ip_complete` | 千兆 IPv4 + ARP 一体化顶层。 |
| UDP 栈 | `udp_complete` | 千兆 UDP + IPv4 + ARP 一体化顶层。 |

3. 进阶自检：把上面四个模块都换成 **10G/25G 版本**，应该叫什么？（参考答案：`eth_mac_10g`、`eth_phy_10g`（PHY 本身就是 10G/25G）、`ip_complete_64`、`udp_complete_64`。）
4. 最后，写一句话总结 verilog-ethernet 与 taxi 的关系，提醒自己后续在新项目里该选哪个。（参考要点：verilog-ethernet 已废弃，被 taxi 取代；学它为了理解架构，新项目用 taxi。）

如果你完成了这张表，说明你已经建立了本库的全局地图，可以放心进入下一篇讲义了。

## 6. 本讲小结

- **verilog-ethernet 是一套覆盖物理层到 UDP 层的开源以太网 Verilog IP 库**，包含 MAC、PHY、ARP/IPv4/UDP 协议栈与 PTP 时间同步组件，附带完整 cocotb 仿真。
- **支持三档速率（1G / 10G / 25G）与两档数据通路宽度（8 位 / 64 位）**；速率与位宽绑定，并由文件名后缀 `_64` 标识。
- **四类顶层模块** 是快速上手的抓手：`eth_mac_*`（MAC）、`eth_phy_10g`（PHY）、`ip_complete[_64]`（IP 栈）、`udp_complete[_64]`（UDP 栈）；另有 `eth_mac_phy_10g` 把 10G/25G 的 MAC 与 PHY 合为一体。
- **命名后缀承载语义**：`_64` 表示 64 位通路（10G/25G），`_fifo` 表示带跨时钟域 FIFO，接口词（`_gmii` / `_rgmii` / `_xgmii`）表示对外物理接口。
- **本仓库已被 taxi 取代并停止维护**，但其架构是 taxi 的直接前身，资料更丰富，是学习这套设计的最佳起点；新项目应优先采用 taxi。
- **所有顶层模块都能在 `rtl/` 目录找到对应 `.v` 文件**，README 的「Source Files」清单是查文件最权威的索引。

## 7. 下一步学习建议

本讲只建立了“宏观地图”，还没进入任何模块内部。建议按下面的顺序继续：

1. **下一篇（u1-l2 仓库结构与目录组织）**：深入了解 `rtl/`、`lib/axis/`、`tb/`、`example/`、`syn/`、`scripts/` 各目录的职责，知道“去哪儿找东西”。
2. **紧接着（u1-l3 AXI-Stream 接口约定）**：这是贯穿全库的接口，几乎所有模块都用它收发数据，必须先掌握。重点理解 `tvalid/tready/tlast/tuser/tkeep` 与握手时序。
3. **之后（u1-l4 测试框架与仿真运行方式）**：学会用 cocotb 跑起一个 testbench，从这一刻起你就能“亲手验证”后面学到的每一个模块。

掌握了 u1 这一单元（项目全貌、目录、接口、仿真）之后，再进入 u2（LFSR/CRC 与 FCS）开始自底向上的源码精读之旅。

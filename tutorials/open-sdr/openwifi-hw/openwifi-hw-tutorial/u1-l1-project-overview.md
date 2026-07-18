# 项目总览：什么是 openwifi 与 SDR Wi-Fi

> 本讲是 openwifi-hw 学习手册的第一篇。在进入任何源码之前，我们先把「这个项目到底是什么、它在整套系统里扮演什么角色、遵循什么授权」这三件事讲清楚。后面所有讲义都建立在这个认知之上。

## 1. 本讲目标

学完本讲后，你应该能够：

- 用一句话说清楚 **openwifi** 是什么，以及「SDR Wi-Fi」「全栈（full-stack）」「mac80211 兼容」这几个词分别意味着什么。
- 区分三个容易混淆的名字：`openwifi`（软件/驱动仓库）、`openwifi-hw`（本仓库，FPGA 硬件设计）、`openwifi-hw-img`（预编译镜像仓库），并说明它们的交付关系。
- 说出 openwifi 的授权模式（AGPLv3 + 订阅双授权），以及它依赖的两个第三方模块 **adi-hdl** 与 **openofdm** 的来源和作用。
- 知道贡献代码前需要签署 CLA。

## 2. 前置知识

本讲是真正的「从零开始」，不要求你懂 FPGA 或 Verilog。但有几个名词先解释一下，读后面会更顺：

- **Wi-Fi / IEEE 802.11**：我们日常手机、笔记本连的无线局域网标准。802.11 是一组标准（a/b/g/n/ac/ax……），规定了无线信号怎么编码、怎么排队发送、怎么确认收到。
- **SDR（Software Defined Radio，软件无线电）**：传统无线电（比如 Wi-Fi 芯片）把「信号怎么处理」烧死在专用硬件里；SDR 则尽量把信号处理放到可编程的芯片（FPGA/CPU）里做，这样你**能看到、能改**原本被黑盒封起来的波形处理过程。
- **FPGA（现场可编程门阵列）**：一种可以被重新「接线」的芯片。用硬件描述语言（如 Verilog）写出的电路，会被综合成 FPGA 里的真实逻辑电路。openwifi 的物理层就跑在 FPGA 上。
- **mac80211**：Linux 内核里管理 Wi-Fi 的一个子系统（框架）。绝大多数 Linux Wi-Fi 驱动都基于它。说 openwifi「mac80211 兼容」，意思是它对 Linux 内核来说**看起来就像一张普通的 Wi-Fi 网卡**，标准工具（iw、wpa_supplicant、hostapd）都能直接用。
- **PS / PL**：本项目用的主芯片是 Xilinx Zynq 系列，它把一颗 ARM CPU（叫 **PS**，Processing System）和一片 FPGA（叫 **PL**，Programmable Logic）做到了同一颗芯片里。后续讲义会反复出现这两个词。
- **PHY / MAC**：PHY（物理层）负责「比特 ↔ 无线波形」的转换；MAC（介质访问控制层）负责「什么时候发、发给谁、丢了重发」。openwifi 把一部分低层 MAC 也做到了 FPGA 里。

> 不用现在全部记住。本讲只需要建立整体印象，细节会在后续讲义展开。

## 3. 本讲源码地图

本讲只读「项目名片」级别的文件，不进入 Verilog 源码：

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md) | 项目的第一入口，讲清了定位、支持的板卡、构建流程、授权与第三方依赖。 |
| [LICENSE](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/LICENSE) | 开源许可证全文（AGPLv3）。 |
| [.gitmodules](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/.gitmodules) | git 子模块配置，记录了 adi-hdl 与 openofdm_rx 两个第三方来源。 |
| [CONTRIBUTING.md](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/CONTRIBUTING.md) | 贡献者须知，主要是 CLA 签署要求。 |

---

## 4. 核心概念与源码讲解

本讲对应大纲中的最小模块：**项目定位与授权**。为了讲透，我们把它拆成三个小模块依次学习。

### 4.1 什么是 openwifi：SDR 与全栈 Wi-Fi

#### 4.1.1 概念说明

openwifi 的自我介绍只有一句话，但信息量很大：

> **openwifi:** Linux mac80211 compatible **full-stack** IEEE802.11/Wi-Fi design based on **SDR**.

拆开来看，三个关键词定义了它的独特性：

1. **基于 SDR**：它不依赖厂商黑盒 Wi-Fi 芯片，而是用 FPGA + 射频收发器（Analog Devices AD9361/9364）自己实现无线信号的发送与接收。波形处理的每一步都是公开的源码。
2. **全栈（full-stack）**：从最底层的物理层信号处理（OFDM 调制/解调），到低层 MAC（CSMA/CA 接入、ACK、重传），再到 Linux 内核驱动，一直到用户态工具，全部自主实现并可研究、可修改。
3. **mac80211 兼容**：它不是一个孤立的实验，而是能真正接入 Linux 网络协议栈、像普通网卡一样工作的设计。

一句话定位：**openwifi 把一块 SDR 硬件变成了一张完全可编程、可研究的 Wi-Fi 网卡。**

#### 4.1.2 核心流程

为了理解「全栈」的含义，可以把一条数据从「应用层数据」到「空中无线电波」的下行链路抽象成下面几层（后续讲义会逐层展开，这里只建立全景）：

```text
应用层数据 (socket)
        │  Linux 网络协议栈
        ▼
mac80211 (内核 Wi-Fi 框架)        ← openwifi 驱动在这里挂接
        │  通过 DMA / 寄存器与硬件交互
        ▼
FPGA (PL) ── openwifi 自定义 IP ──┐
        │   · tx_intf  发射接口/缓存          │  这一大块就是
        │   · openofdm_tx  OFDM 发射机        │  openwifi-hw
        │   · xpu  低层 MAC 控制核心          │  本仓库负责的内容
        │   · rx_intf / openofdm_rx  接收链路 │
        ▼                                    │
射频收发器 AD9361 (DAC/ADC)  ────────────────┘
        ▼
天线 / 空中 802.11 信号
```

传统 Wi-Fi 芯片把虚线框里的内容封装成不可见的 ASIC；openwifi 则把这块**全部用源码实现**，这正是它对研究者和工程师的核心价值。

#### 4.1.3 源码精读

README 开篇就给出了 openwifi 的官方定位（注意它描述的是整个 openwifi 项目，而不仅仅是本仓库）：

- [README.md:4](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L4) —— 这一行就是 openwifi 的「一句话定义」，三个关键词（mac80211 compatible、full-stack、SDR）全部出现。

紧跟着的一行说明了项目的开源立场与商业模式：

- [README.md:6](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L6) —— 声明坚持开源为基础，同时提供**订阅（SUBSCRIPTION）**以获取高级功能与专属支持，指向 <https://openwifi.tech>。这就是后面会讲的「双授权」线索。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**（本讲不涉及编译运行）：

1. **实践目标**：学会从 README 快速提取项目定位关键词。
2. **操作步骤**：打开 [README.md:4](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L4)，把这一句里你认为最能体现 openwifi 与「普通 Wi-Fi 芯片」差异的词圈出来。
3. **需要观察的现象**：你会注意到「full-stack」和「SDR」是区别于商用 Wi-Fi 芯片的核心——商用芯片并不会把基带处理暴露为可改的源码。
4. **预期结果**：能用一句话向同事解释「openwifi 是一张用 SDR 实现的、全栈可编程的、Linux 兼容的 Wi-Fi 网卡设计」。
5. 运行结果：本步骤为阅读理解，**无需运行命令**。

#### 4.1.5 小练习与答案

**练习 1**：README 说 openwifi 是「mac80211 compatible」的。这对普通用户意味着什么？

> **参考答案**：意味着装上 openwifi 之后，Linux 内核把它当成一张标准 Wi-Fi 网卡，现有的 `iw`、`wpa_supplicant`、`hostapd` 等工具都能直接使用，不需要专门的私有用户态软件。

**练习 2**：「基于 SDR」与「用普通 Wi-Fi 芯片」实现，最大的区别在哪？

> **参考答案**：SDR 把无线信号的发送/接收处理（调制、FFT、同步等）放到可编程的 FPGA 里做并以源码形式公开；普通 Wi-Fi 芯片把这些处理固化在不可见的专用电路（ASIC）里，用户无法查看或修改。

---

### 4.2 openwifi-hw 的角色：硬件仓库与软件仓库的分工

#### 4.2.1 概念说明

很多人一开始会被三个相似的名字搞晕。这里明确区分：

| 名字 | 是什么 | 内容 |
|------|--------|------|
| **openwifi** | 软件/驱动仓库 | Linux 内核驱动（mac80211 下）、用户态工具、固件加载逻辑等。 |
| **openwifi-hw**（本仓库） | FPGA 硬件设计仓库 | 用 Verilog 写的物理层/低层 MAC IP、板级工程、Vivado 构建脚本。产出是 bitstream。 |
| **openwifi-hw-img** | 预编译镜像仓库 | 存放已经编译好的 FPGA 比特流 `.bit`、`.xsa`、ILA 的 `.ltx` 等文件，给不想自己编译的人直接用。 |

简单记：**软件仓库管「怎么用」，硬件仓库管「怎么造芯片」，镜像仓库管「现成的成品」。**

本仓库（openwifi-hw）在整套系统中的职责，README 写得很直白：

> This repository includes **Hardware/FPGA design**. To be used together with **openwifi** repository (driver and software tools).

#### 4.2.2 核心流程

openwifi-hw 的最终「产物」是一份可以在 Zynq 芯片上加载的 FPGA 设计。它和软件仓库的交接流程大致是：

```text
openwifi-hw (本仓库)
   │  Vivado 综合/实现 → 生成 bitstream
   ▼
.xsa（含 bitstream 的硬件镜像）+ .ltx（调试用，可选）
   │  由 boards/sdk_update.sh 导出到 $OPENWIFI_HW_IMG_DIR
   ▼
openwifi-hw-img / 软件构建环境
   │  与 openwifi 软件仓库一起，构建可启动的系统
   ▼
运行中的 openwifi 节点（ARM 上跑 Linux + FPGA 跑 PHY/MAC）
```

也就是说，openwifi-hw 的交付物**不是可执行程序**，而是「硬件描述 + 编译出的镜像」，这些镜像之后会被 openwifi 软件仓库消费。

#### 4.2.3 源码精读

- [README.md:20](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L20) —— 明确本仓库是「Hardware/FPGA design」，并指出要和 `openwifi` 软件仓库配合使用。
- [README.md:24](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L24) —— 说明预编译文件放在 **openwifi-hw-img** 仓库，且每个板卡的 `boards/$BOARD_NAME/sdk/` 目录下有 `.bit`、`.ltx` 等文件。不想自己编译的人可以直接用这里。
- [README.md:90-L95](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L90-L95) —— `sdk_update.sh` 把 `.xsa`、`.ltx` 以及相关 git 信息存到 `$OPENWIFI_HW_IMG_DIR`，供 openwifi 软件构建环境取用。这是硬件→软件的关键交接点。

#### 4.2.4 代码实践

1. **实践目标**：理清三个仓库的交付关系。
2. **操作步骤**：
   - 阅读上面的 [README.md:20](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L20) 与 [README.md:24](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L24)。
   - 在本地仓库执行 `ls boards/`，观察存在哪些板卡目录（例如 `zc706_fmcs2` 等），这印证了「按板卡组织」的结构。
3. **需要观察的现象**：`boards/` 下按 `BOARD_NAME` 分目录，每个板卡目录里后续会有 `sdk/` 用于存放导出的镜像。
4. **预期结果**：画出「openwifi-hw → .xsa/.ltx → openwifi-hw-img/openwifi 软件」的交付流程图。
5. 运行结果：`ls boards/` 可在本地验证；其余为阅读理解。

#### 4.2.5 小练习与答案

**练习 1**：如果我只想要一个能跑的 openwifi 节点、不想自己编译 FPGA，应该用哪个仓库？

> **参考答案**：直接用 **openwifi-hw-img** 仓库里的预编译镜像（`.bit`/`.xsa`），配合 openwifi 软件仓库即可，不必从 openwifi-hw 源码自己综合。

**练习 2**：openwifi-hw 仓库的最终产物是什么形式？

> **参考答案**：是一份 FPGA 硬件镜像（`.xsa`，其中包含 bitstream；可能还有用于调试的 `.ltx`），而不是一个普通的可执行程序。这些镜像交给软件仓库去打包进可启动系统。

---

### 4.3 授权模式与第三方模块来源

#### 4.3.1 概念说明

使用或二次开发 openwifi 之前，必须搞清楚两件事：**授权**与**第三方依赖**。

**（1）双授权（Dual License）模式**

openwifi 代码采用双授权：

- **AGPLv3（开源许可证）**：这是默认的开源授权。AGPLv3 是一种「强 copyleft」许可证，特别之处在于——**如果你把它做成通过网络对外提供服务的程序，也必须公开你修改后的源码**。这对商业闭源使用是一个强约束。
- **订阅（SUBSCRIPTION）/ 商业授权**：如果你需要在非开源场景下使用，或需要高级功能与专属支持，可以通过 <https://openwifi.tech> 联系获取商业授权。

此外，**贡献代码需要先签署 CLA（贡献者许可协议）**。

**（2）第三方模块**

openwifi 不是从零造所有轮子，它明确建立在两个第三方工作之上：

1. **Analog Devices HDL 参考设计（adi-hdl）**：作为 git 子模块引入，提供 AD9361 射频收发器、AXI DMA、AXI 互连等基础设施。openwifi 是「在 ADI 参考设计之上添加必要的模块与修改」。
2. **openofdm 项目（→ ip/openofdm_rx）**：802.11 OFDM 接收机基于 openofdm 项目（open-sdr 的 fork，dot11zynq 分支），作为子模块映射到 `ip/openofdm_rx`。

> 重要提醒：这些第三方模块有**各自的许可证**。README 明确说，检查并遵守这些模块的许可是**使用者自己的责任**。

#### 4.3.2 核心流程

「复合授权」可以这样理解：你在使用 openwifi 系统时，实际上同时面对多个许可证叠加：

```text
你的 openwifi 系统 = openwifi 自有代码 (AGPLv3 / 或商业订阅)
                   + adi-hdl        (ADI 的许可证)
                   + openofdm_rx    (openofdm 的许可证)
                   + Vivado IP (如 Viterbi Decoder，需要 Xilinx 评估许可)
```

所以合规检查的流程是：先明确你的**使用目的**（开源项目？商业产品？网络服务？），再据此核对 openwifi 自身授权（AGPLv3 是否满足你的场景）以及每个第三方模块的授权。

#### 4.3.3 源码精读

- [LICENSE:1-L7](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/LICENSE#L1-L7) —— 确认本仓库开源许可证是 **GNU AFFERO GENERAL PUBLIC LICENSE（AGPLv3）**。
- [README.md:22](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L22) —— 说明双授权（AGPLv3 为开源授权，非开源/高级功能需联系商业授权），并强调第三方模块的许可证由**使用者自行核对**，还指向了 ADI 的复合授权说明作为范例。
- [.gitmodules:1-L6](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/.gitmodules#L1-L6) —— 两个子模块的真实来源：
  - `adi-hdl` → <https://github.com/analogdevicesinc/hdl.git>（Analog Devices 官方 HDL）。
  - `ip/openofdm_rx` → <https://github.com/open-sdr/openofdm.git>（open-sdr 维护的 openofdm fork）。
- [README.md:192](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L192) —— 明确 openwifi「在 Analog Devices HDL 参考设计之上添加必要的模块/修改」，并建议一般性问题参考 ADI wiki。
- [README.md:194](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L194) —— 说明 802.11 OFDM 接收机基于 openofdm 项目，改进在 open-sdr 的 fork（dot11zynq 分支），映射到 `ip/openofdm_rx`。
- [CONTRIBUTING.md:1](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/CONTRIBUTING.md#L1) —— 贡献前需签署 CLA（个人/实体两种），并发送到指定邮箱。

#### 4.3.4 代码实践

1. **实践目标**：确认两个第三方子模块的来源与作用。
2. **操作步骤**：
   - 打开 [.gitmodules](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/.gitmodules)，读出两个子模块的 `path` 与 `url`。
   - 对照 [README.md:192](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L192) 与 [README.md:194](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L194) 中的说明。
   - （可选）若已克隆仓库，执行只读命令 `git submodule status`，观察两个子模块及其指向的 commit。
3. **需要观察的现象**：`adi-hdl` 指向 analogdevicesinc/hdl；`ip/openofdm_rx` 指向 open-sdr/openofdm。
4. **预期结果**：能写出两句话——「adi-hdl 提供 AD9361 射频与 AXI 数据通路等基础设施」「openofdm_rx 提供 802.11 OFDM 接收机的物理层处理」。
5. 运行结果：`git submodule status` 可在本地验证（只读命令）；若未执行 `git submodule update`，子模块目录可能为空，这是正常的。

#### 4.3.5 小练习与答案

**练习 1**：AGPLv3 与常见的 MIT/Apache 许可证相比，对商业使用最大的额外约束是什么？

> **参考答案**：AGPLv3 有「网络使用即分发」条款——即使你只是把修改后的版本部署成网络服务对外提供，也必须向该服务的用户公开你修改后的源码。这比 MIT/Apache 的约束强得多。

**练习 2**：`ip/openofdm_rx` 这个子模块的代码最初来自哪个项目？

> **参考答案**：来自 [openofdm 项目](https://github.com/jhshi/openofdm)；openwifi 使用的是 open-sdr 的 fork（dot11zynq 分支），并在此基础上做了改进。

**练习 3**：我想给 openwifi-hw 提交一个补丁，第一步该做什么？

> **参考答案**：先签署 CLA（个人或实体版本）并发送到 `Filip.Louagie@UGent.be`，之后才能贡献代码，详见 [CONTRIBUTING.md](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/CONTRIBUTING.md)。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个综合任务（**源码阅读型实践**，无需编译）：

> **任务**：阅读 [README.md](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md)，用自己的话写一段 150 字左右的说明，回答三个问题：
>
> 1. openwifi-hw 在整套 openwifi 系统中承担什么角色？（它产出什么、交给谁）
> 2. 它依赖的两个第三方模块 `adi-hdl` 与 `openofdm` 分别提供什么能力？
> 3. 如果你的公司打算把 openwifi 用在一款要销售的闭源产品里，从授权角度需要注意什么？

**参考作答框架**（请用自己的话改写，不要照抄）：

1. openwifi-hw 是 openwifi 项目的 **FPGA 硬件设计仓库**，用 Verilog 实现物理层与低层 MAC（包括 `xpu`、`tx_intf`、`rx_intf`、`openofdm_tx`、`openofdm_rx`、`side_ch` 等自定义 IP）。它最终产出 `.xsa`/`.ltx` 等硬件镜像，通过 `sdk_update.sh` 交给 openwifi 软件仓库消费，变成可启动的 Wi-Fi 节点。
2. `adi-hdl`（Analog Devices HDL）提供 AD9361 射频收发器驱动、AXI DMA/互连等基础设施；`openofdm`（映射到 `ip/openofdm_rx`）提供 802.11 OFDM 接收机的物理层处理（包检测、同步、FFT、均衡、Viterbi 译码等）。
3. 闭源商业产品通常无法满足 AGPLv3 的开源/网络服务条款，因此需要通过 <https://openwifi.tech> 获取**商业订阅授权**；同时要**自行核对** adi-hdl、openofdm 等第三方模块各自的许可证，以及 Xilinx IP（如 Viterbi Decoder）的许可要求。

> 想再深入一步的话：执行只读命令 `git -C <仓库根目录> submodule status`，把两个子模块的 commit 一并记录下来，作为你研究版本的快照。

## 6. 本讲小结

- **openwifi** 是基于 SDR、Linux mac80211 兼容的 **全栈** IEEE 802.11/Wi-Fi 设计——从物理层波形到内核驱动全部可研究、可修改。
- 三个名字要分清：`openwifi`（软件/驱动）、`openwifi-hw`（本仓库，FPGA 设计）、`openwifi-hw-img`（预编译镜像）。
- openwifi-hw 的产物是 **硬件镜像**（`.xsa`/`.ltx`），不是可执行程序；它通过 `sdk_update.sh` 把镜像交给软件仓库。
- 授权是 **AGPLv3 + 订阅双授权**；贡献代码需先签 **CLA**。
- 两个关键第三方依赖：**adi-hdl**（ADI 参考设计，射频/数据通路基础设施）与 **openofdm**（→ `ip/openofdm_rx`，OFDM 接收机）。
- 第三方模块的许可证需由**使用者自行核对**。

## 7. 下一步学习建议

本讲只读了「项目名片」。要真正理解 openwifi-hw，建议下一步：

- 继续本单元的 **u1-l2（目录结构、子模块与软硬件边界）**：进入 `ip/`、`boards/`、`adi-hdl/` 等目录，看清代码是怎么组织的，以及 PS/PL 的边界。
- 然后 **u1-l3（支持的板卡与运行环境）** 和 **u1-l4（FPGA 构建全流程）**：了解如何在 Vivado 2022.2 中把源码变成镜像。
- 在动手构建之前，**不必**深读 Verilog；等对目录和构建流程有整体把握后，再进入 u2 顶层设计与系统集成。

> 推荐伴随阅读：README 中 [Build FPGA](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L44) 与 [Introduction](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L18) 两节，作为下一讲的预习。

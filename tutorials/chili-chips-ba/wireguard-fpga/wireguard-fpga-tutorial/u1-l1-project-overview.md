# 项目定位、背景与开源技术栈

> 本讲是整本学习手册的**第一篇**。它的作用是帮你建立「全局地图」：在阅读任何一行代码之前，先弄清楚 wireguard-fpga 到底是什么、为什么要造它、它由哪些开源技术拼装而成。后续所有讲义都会反复引用本讲建立的术语和定位。

---

## 1. 本讲目标

学完本讲，你应当能够：

1. 用一句话说清 **wireguard-fpga 是什么**——一个开源的、基于低成本 FPGA（Alinx AX7201，Artix-7）的 WireGuard VPN 硬件实现。
2. 说出项目要解决的**核心矛盾**：软件 WireGuard 跑不到线速，而现有硬件实现又贵且闭源。
3. 列出项目使用的 **6 个开源工具 + 1 个开源 IP 库**（PipelineC / PeakRDL / RISC-V / VProc / OpenXC7 / SV2V / verilog-ethernet），并讲清每一个在本项目里**扮演什么角色**。
4. 理解 Phase1 的 **Proof-of-Concept（PoC）定位**与项目自述的六大挑战。
5. 把本项目与 **Blackwire（100G 专有实现）** 以及**纯软件 WireGuard**做横向对比，理解它的取舍。

---

## 2. 前置知识

本讲假定你是初学者。下面这些名词会在全文反复出现，先用大白话解释一遍：

- **VPN（虚拟专用网络）**：在公共互联网上挖一条「加密隧道」，让两台机器像在同一局域网一样安全通信。WireGuard 是一种现代、简洁的 VPN 协议。
- **FPGA（现场可编程门阵列）**：一种可以通过代码「现场」改变内部电路的芯片。与 CPU「一条一条执行指令」不同，FPGA 可以在硬件电路上并行处理数据，速度极快。
- **RTL（Register Transfer Level，寄存器传输级）**：用硬件描述语言（如 Verilog / SystemVerilog）写成的电路设计，描述数据在寄存器之间如何流动。本项目的数据面（转发 + 加解密）就是用 RTL 写的。
- **SoC（System on Chip，片上系统）**：把 CPU、内存接口、外设、专用加速电路等都集成在一片芯片上的系统。本项目在 FPGA 内部搭建了一个小型 SoC。
- **软 CPU（soft CPU）**：用 FPGA 的逻辑资源「搭」出来的 CPU，不是买来的物理芯片。本项目用的是 RISC-V 架构的软 CPU。
- **HLS（高层综合）**：把 C/C++ 这样的高级语言自动编译成硬件电路（RTL）的技术。本项目的加密核心就用到了类 HLS 工具。
- **CSR（Control and Status Register，控制与状态寄存器）**：硬件里可以被软件读写的「旋钮和仪表盘」，是软件控制硬件、硬件回报状态的唯一通道。
- **wire speed / 线速**：网络设备能以物理链路的满速率（不丢包、不降速）处理数据的能力。本项目追求线速加解密。
- **PoC（Proof of Concept，概念验证）**：只用来证明「这条路走得通」的原型，不是量产产品。

如果你对其中某些概念完全陌生，不必担心——本讲只要求你建立印象，后续单元会逐一深入源码。

---

## 3. 本讲源码地图

本讲主要读两类「入口文档」，它们是理解全项目的钥匙：

| 文件 | 作用 | 本讲怎么用 |
| --- | --- | --- |
| [README.md](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/README.md) | 项目总入口，讲清定位、动机、架构、技术栈、构建与验证。 | 项目的「自我介绍」，本讲引用最多。 |
| [0.doc/0.README.txt](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/0.doc/0.README.txt) | 散落的参考链接清单（NLnet 资助页、Blackwire、CAM/RNG 等技术出处）。 | 佐证项目背景与技术出处。 |

> 提示：本讲是「项目全景篇」，所以引用的是文档而非 `.v/.sv` 代码。从下一篇 [u1-l2](u1-l2-repo-structure.md) 开始，我们才会进入真实代码文件。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 项目背景与动机** —— 它为什么存在。
- **4.2 开源技术栈角色** —— 它由哪些零件拼成。
- **4.3 Phase1 范围与挑战** —— 它现在能做到什么、还差什么。

---

### 4.1 项目背景与动机

#### 4.1.1 概念说明

VPN 是互联网安全的基石，而 WireGuard 正在取代老旧的 OpenVPN / IPSec，成为更现代、更易管理的加密隧道方案。README 开篇就用一句话点题：VPN 是「连接地理上分散的异构网络、在公共共享介质上营造出同质私有网络印象」的一组技术（[README.md:11](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/README.md#L11)）。

但 WireGuard 的现有实现都各有痛点，README 在 [README.md:18](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/README.md#L18) 一段话点出了核心矛盾：

> 软件实现（如 Linux 内核、Tailscale、Netbird）**远达不到线速**；而现有硬件实现**既昂贵又基于闭源 IP 和闭源工具**。

wireguard-fpga 的目标，就是在这两者之间填空：做一个**开源、FPGA、低成本**的实现。你可以把它理解成一个等式：

\[ \text{wireguard-fpga} = \text{WireGuard 协议} + \text{开源 FPGA 硬件} + \text{全套开源工具链} \]

#### 4.1.2 核心流程：项目要补的「三块短板」

把上面那段话拆成项目要解决的三个问题，形成一个简单的动机链：

1. **软件不够快** → 必须用硬件（RTL）做线速转发与加解密。
2. **硬件太贵** → 选一块廉价、大众、支持开源工具的板卡（Alinx AX7201，Artix-7）。
3. **闭源不可持续** → 从门级到综合布局布线，全程使用开源工具和开源 IP。

这三点直接决定了后续所有技术选型，是理解全项目的「为什么」。

#### 4.1.3 源码精读：定位一句话与「回到未来」

README 用一句反引号包裹的话定义了项目的身份——一个**开源、基于 FPGA** 的 WireGuard 实现：

> 这段定位见 [README.md:20](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/README.md#L20)，紧接着它列出了本项目调动的多项开源技术。

紧接着，「Back to the Future（回到未来）」一节给出了让硬件 WireGuard「真正可及」的四个设计承诺（[README.md:42-48](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/README.md#L42-L48)）：

| 承诺 | 含义 |
| --- | --- |
| 廉价硬件平台（四口 1000Base-T） | 用 Alinx AX7201，而非昂贵的加速卡 |
| 自给自足（无需 PC 主机） | 节点独立运行，不依赖外接电脑 |
| 大众化的 Artix-7 FPGA | 便宜、普及的器件 |
| 受开源工具支持 | 全程开源工具链 |
| 全部用 Verilog/SystemVerilog | 用业界最通用的硬件描述语言，而非小众 HDL |

这五条与下文的 Blackwire 对比一脉相承。

#### 4.1.4 代码实践：读一段、画一张对比图

这是一个**源码阅读型实践**，目的是让你亲手把「定位」内化。

1. **实践目标**：在阅读后能口头复述项目定位与动机。
2. **操作步骤**：
   - 打开 [README.md:11-48](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/README.md#L11-L48)，通读「引言 → A Glimpse into History → Back to the Future」三节。
   - 用一张两列表格写下「软件 WireGuard 的痛点」和「硬件 WireGuard（如 Blackwire）的痛点」。
3. **需要观察的现象**：你会发现两类实现的痛点恰好互补——软件慢、硬件贵且闭源。
4. **预期结果**：你能写出一句不超过 30 字的项目定位，例如「开源、低成本 FPGA 上线速的 WireGuard」。
5. 本实践不需要运行命令，属于纯阅读理解。

#### 4.1.5 小练习与答案

**练习 1**：README 说软件 WireGuard「far below the speed of wire」，请结合「软件 vs 硬件」的本质差异，解释为什么软件实现天然难以达到线速。

> **参考答案**：CPU 逐条取指执行，且要经过操作系统网络栈、上下文切换、内存拷贝等开销；而 FPGA 的 RTL 是专用并行电路，数据一到就流过流水线，没有「逐条指令」和「系统调用」的开销，因此硬件更容易逼近物理链路速率。

**练习 2**：「Back to the Future」列出了几条设计承诺？其中「自给自足（w/o requiring PC host）」对部署场景意味着什么？

> **参考答案**：共 4 条主要承诺（廉价平台、自给自足、Artix-7、开源工具，外加 Verilog 这条语言选择）。「自给自足」意味着节点本身就是一个独立网络设备，不必像 Blackwire 那样插在 PC 里当加速卡，更适合边缘/现场部署。

---

### 4.2 开源技术栈角色

#### 4.2.1 概念说明

这个项目最特别的地方在于：**几乎每一个环节都用开源技术**。README 在 [README.md:20-34](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/README.md#L20-L34) 列出了 6 个带 ⭐ 的开源工具，并「接入（taps into）」一个开源 IP 库，把它们组合成一套「软硬协同的 SoC」。理解每个技术扮演什么角色，是后续阅读源码的导航图。

#### 4.2.2 核心流程：从需求到工具的映射

可以按「项目要做的事 → 用哪个开源技术」来理解这一栈：

```
要在线速做加解密          → PipelineC（类HLS，C→RTL）
软硬件要通信（旋钮/仪表）  → PeakRDL（从 SystemRDL 生成 CSR 的 RTL 和 HAL）
要跑控制面（握手/路由管理）→ RISC-V（软 CPU，picoRV32 等）
仿真要又快又准            → VProc（虚拟处理器协同仿真）
要开源综合/布局布线       → OpenXC7（Yosys + nextpnr + Project X-Ray）
开源工具看不懂 SV         → SV2V（SystemVerilog 转 Verilog）
要现成的以太网 MAC/PHY    → verilog-ethernet（alexforencich 的开源 IP 库）
```

#### 4.2.3 源码精读：七项技术各自的角色

下面把 README 的清单逐条对应到项目里的真实用途（行号见 [README.md:22-34](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/README.md#L22-L34)）：

| 开源技术 | README 标注 | 在本项目中的具体用途 |
| --- | --- | --- |
| **PipelineC** | `HLS-like` | 把 C 语言写的 ChaCha20-Poly1305 加解密算法编译成可综合 RTL，让加密核心轻松上硬件。源码在 `3.build/pipelinec_build/`。 |
| **PeakRDL** | `CSR` | 从单一 SystemRDL 规格文件（`csr.rdl`）**自动生成** CSR 的 RTL 和软件 HAL，让软硬件共用唯一真源。 |
| **RISC-V** | `CPU` | 作为片上软 CPU（如 picoRV32）运行控制面软件（WireGuard 握手、路由/密钥管理）。 |
| **VProc** | `CoSim` | 虚拟处理器协同仿真，让软件可以与 HDL 一起仿真，显著加速大型设计的验证。 |
| **OpenXC7** | `PNR` | 为 Xilinx 7 系列 FPGA 提供开源的综合 + 布局布线（Yosys/nextpnr/Project X-Ray），替代闭源 Vivado。 |
| **SV2V** | `SystemVerilog-to-Verilog HDL converter` | 把项目的 SystemVerilog 转成纯 Verilog，以适配对 SV 支持不全的开源工具链。 |
| **verilog-ethernet** | `open-source IP library` | 提供现成的以太网 MAC/PHY 相关 IP（1G GMII、ARP、IP 等），是 alexforencich 的开源以太网栈。 |

补充两个佐证：
- 项目的致谢里特别提到 PipelineC 作者 Julian Kemmerer 帮助把两个加密块「从 C 推进到硬件」（[README.md:346](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/README.md#L346)）。
- README 的 References 区也收录了 verilog-ethernet（Alex's Ethernet Stack，[README.md:76](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/README.md#L76)）和 OpenXC7（[README.md:74](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/README.md#L74)）的出处。

> 名词速查：**PNR（Place and Route，布局布线）** 是把综合后的逻辑门「摆」到 FPGA 物理资源上并「连线」的步骤；**HAL（Hardware Abstraction Layer，硬件抽象层）** 是屏蔽硬件细节、给软件提供统一访问接口的一层代码。

#### 4.2.4 代码实践：绘制「开源技术 → 用途」对照表

这是本讲的**主实践任务**，对应规格要求。

1. **实践目标**：亲手把 7 项开源技术与它们在本项目里的具体用途一一对应，形成你的「导航图」。
2. **操作步骤**：
   - 通读 [README.md:20-34](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/README.md#L20-L34)。
   - 建一张三列表格：`开源技术 | README 里的关键词 | 在本项目的具体用途`。
   - 对其中 2~3 项，进一步用 `grep` 在仓库里找到它的真实落点，例如：
     - PipelineC：查看目录 `3.build/pipelinec_build/` 是否存在；
     - PeakRDL：在 `3.build/` 下找 `csr.rdl` 与生成脚本 `sysrdl_cosim.py`。
3. **需要观察的现象**：你会发现每项技术都对应仓库里一个具体的目录或文件，并非只停留在 README 文字里。
4. **预期结果**：得到一张完整对照表（可作为后续单元的目录索引），并能指出每项技术的「代码落点」。
5. 本实践以阅读和检索为主，无需运行综合命令；运行结果属于「待本地验证」（取决于你是否已装好工具链）。

#### 4.2.5 小练习与答案

**练习 1**：为什么本项目需要 SV2V？它与 OpenXC7 是什么关系？

> **参考答案**：项目的门级设计大量使用 SystemVerilog，而开源工具链 OpenXC7（尤其是其中的 Yosys）对 SV 的支持并不完整。SV2V 把 SV 转成纯 Verilog，再交给 OpenXC7 综合/布线，因此 SV2V 是让 OpenXC7 能「吃下」本项目的桥梁。

**练习 2**：PeakRDL 在本项目里解决了「单一真源」问题。请用自己的话说说：如果没有 PeakRDL，软硬件双方会怎样维护 CSR？

> **参考答案**：如果没有自动生成，硬件工程师手写 RTL 寄存器、软件工程师手写头文件，两边地址、位域、读写属性很容易不一致且难同步。PeakRDL 从同一份 `csr.rdl` 同时产出 RTL 和 HAL，保证两边永远一致，CSR 因此成为软硬件之间可靠的「契约」。

**练习 3**：把 7 项技术分成三类：「生成/综合硬件」「运行/验证软件」「提供现成 IP」，你会怎么分？

> **参考答案**：① 生成/综合硬件：PipelineC（生成加密 RTL）、OpenXC7（综合布线）、SV2V（语言转换）；② 运行/验证软件：RISC-V（运行控制面）、VProc（协同仿真）、PeakRDL（生成软件 HAL，可归此类或「接口」类）；③ 提供现成 IP：verilog-ethernet。（PeakRDL 兼跨硬件 RTL 生成与软件 HAL 生成，是连接两类的桥梁，分类可灵活。）

---

### 4.3 Phase1 范围与挑战

#### 4.3.1 概念说明

在动手前必须明白：**当前仓库处于 Phase1，是一个 PoC**，不是成品。README 的「Scope」一节明确写道：Phase1「主要是一个概念验证，功能不全，绝对不是可部署的产品，而只是一个入口、一块跳板」（[README.md:89](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/README.md#L89)）。

这一点极其重要，因为它解释了你在源码里会看到的「已写好但未上线」的现象——例如完整的 WireGuard 解封装/加解密/路由查找链已经写好，但当前实际编入设计的是直通的 `dpe_dummy_switch`。后续单元（尤其 U4）会如实标注这一现状。

#### 4.3.2 核心流程：六大挑战

README 在「Recognized Challenges」列出了项目自述的 6 大挑战（[README.md:93-106](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/README.md#L93-L106)），可以归纳为：

1. **HW/SW 划分与交互**：片上有 RISC-V CPU，软硬件接口复杂，软件要控制线速数据面骨干。
2. **HW/SW 协同开发、集成与调试**：标准仿真对这么大的设计不现实，因此寄望于 VProc ISS。
3. **真实线速测试**：要在真实速率下验证。
4. **开源工具对 SV 与 FPGA 原语/IP 的支持广度**。
5. **开源工具的 QoR（质量）仍在成熟中**：连 Blackwire 用商业工具 + 高端硅片都遇到时序/拥塞问题。
6. **资金**：多学科复杂项目，资金可能不足以走完全程（Blackwire 就曾因此中断）。

> 名词速查：**QoR（Quality of Results，结果质量）** 指综合布线后设计的时序、面积、功耗等指标好坏；**ISS（Instruction Set Simulator，指令集模拟器）** 是用软件模拟 CPU 指令行为的工具。

#### 4.3.3 源码精读：与 Blackwire 的对比

理解「挑战」最好的方式是看项目怎么和前作 Blackwire 对照。Blackwire 是一个 **100Gbps** 的硬件 WireGuard 交换机，基于 AMD/Xilinx 专有的 Alveo U50 加速卡、只能用专有 Vivado 工具链（[README.md:38](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/README.md#L38)，Ref2 见 [README.md:62](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/README.md#L62)）。下表把两者对比清楚：

| 维度 | Blackwire（前作） | wireguard-fpga（本项目） |
| --- | --- | --- |
| 吞吐 | 100 Gbps | Phase1 PoC，目标线速（四口千兆） |
| 平台 | Alveo U50 加速卡（昂贵、PC 内插卡） | Alinx AX7201（廉价、独立运行） |
| 工具链 | 专有 Vivado | Vivado **与** 开源 OpenXC7 双轨 |
| 硬件描述语言 | SpinalHDL（小众） | Verilog / SystemVerilog（通用） |
| 开源方式 | 被动开源（因财务困难） | 主动、真正的开源精神 |
| 外部依赖 | 需 PC 主机经 PCIe 连接 | 自给自足，无需 PC |

README 还指出一个工程现实：开源工具链尚缺一些 Xilinx 原语支持——例如 `IBUFGDS` 需要改写、`BUFGMUX` 被移除，后者会导致时钟不走专用时钟网络而引入延迟（见 [README.md:305](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/README.md#L305) 提到的 openXC7 + SV2V 移植，以及 `3.build/README.md` 的详述）。这正是上述第 4、5 项挑战的具体体现。

#### 4.3.4 代码实践：把挑战对应到真实条目

1. **实践目标**：把抽象的「挑战」对应到 README 里具体的文字与状态勾选。
2. **操作步骤**：
   - 读 [README.md:88-106](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/README.md#L88-L106)（Scope + Recognized Challenges）。
   - 再往下浏览「Project Execution Plan / Tracking」一节（[README.md:221](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/README.md#L221) 起），看看六大挑战对应了哪些 Take 的勾选状态（✔ 已完成 / [ ] 未完成）。
3. **需要观察的现象**：你会发现 Take1–Take3 基本完成，而 Take4 的「会话维护/安全关闭」、Take5 的「真实系统功能测试/性能测试」仍是未勾选状态。
4. **预期结果**：你能用一句话回答「当前 Phase1 完成了哪些、还差哪些」，并能解释为什么 README 顶部写着「until all checkmarks are in place … use at own risk」。
5. 属于阅读理解型实践，无需运行命令。

#### 4.3.5 小练习与答案

**练习 1**：为什么 README 强调 Phase1「不是可部署的产品」？请结合「Project Execution Plan」里未勾选的条目说明。

> **参考答案**：因为会话维护（Session Maintenance）、安全关闭（Session Closure）、真实系统功能测试、性能测试等关键条目尚未完成（仍是 `[ ]`）。在它们全部完成前，设计不保证可用，README 因此声明「use at own risk」。

**练习 2**：Blackwire 用的是 SpinalHDL，而本项目坚持用 Verilog/SystemVerilog。这一选择背后最重要的理由是什么？

> **参考答案**：SpinalHDL 虽强大但「在业界没扎根」（niche HDL），社区和工具支持有限；而 Verilog/SystemVerilog 是业界最通用的硬件描述语言，门槛低、生态广，更符合「真正可及的开源」这一目标，也更容易被开源工具链（经 SV2V 转换后）处理。

**练习 3**：项目把 OpenXC7 与 Vivado 并列为两条工具链。为什么既要支持闭源的 Vivado，又要费力移植到开源的 OpenXC7？

> **参考答案**：Vivado 成熟、QoR 好，是「能跑通、能收敛时序」的保底参考实现；OpenXC7 则兑现「全开源」承诺，让没有 Vivado 授权的人也能综合布线。双轨并行既保证结果质量，又坚持了开源精神，同时还能在两者间做对比验证（`3.build/README.md` 的 Test example 正是这种对比）。

---

## 5. 综合实践

把本讲三个模块串起来，完成一份**一页纸「项目速览」**：

1. **背景与定位**：用一句话写出 wireguard-fpga 是什么（参考 4.1）。
2. **技术栈导航图**：绘制 4.2 的「开源技术 → 具体用途」对照表，并为每项技术标注它在仓库里的**目录落点**（如 PipelineC → `3.build/pipelinec_build/`，PeakRDL → `3.build/csr_build/` 与 `3.build/sysrdl_cosim.py`）。
3. **现状与边界**：写明这是 Phase1 PoC，列出「已完成」与「未完成」各 2 条（参考 4.3 与 Project Execution Plan）。

完成后，你应当拥有了一张可以随时回看的项目全景图。建议把它保存下来——后续每读一篇讲义，都在这张图上补一处细节。

> 验收标准：如果你能用这张图，向一个没接触过本项目的人解释清楚「它是什么、由什么拼成、现在能做什么」，本讲就达标了。

---

## 6. 本讲小结

- wireguard-fpga 是一个**开源、低成本 FPGA（Alinx AX7201 / Artix-7）** 上的 WireGuard VPN 硬件实现，目标是填补「软件慢、硬件贵且闭源」的空白。
- 它用 **6 个开源工具 + 1 个开源 IP 库**拼装出一套软硬协同 SoC：PipelineC（加密上硬件）、PeakRDL（CSR 单一真源）、RISC-V（控制面软 CPU）、VProc（协同仿真）、OpenXC7（开源综合布线）、SV2V（SV→Verilog）、verilog-ethernet（现成以太网 IP）。
- 它与前作 **Blackwire（100G、专有平台、SpinalHDL）** 形成对照：廉价、自给自足、通用语言、双工具链、真正开源。
- 当前仓库处于 **Phase1 PoC**：是「入口和跳板」，不是成品；完整的 WG 处理链已写好但部分尚未上线（后续单元会如实标注）。
- 项目自述 **6 大挑战**集中在 HW/SW 划分、协同调试、线速测试、开源工具的支持广度与 QoR、以及资金。
- 本讲是纯文档阅读篇，目的是建立全局地图；从下一篇起进入真实源码。

---

## 7. 下一步学习建议

- 推荐紧接着读 **[u1-l2 仓库目录结构与六大子系统导航](u1-l2-repo-structure.md)**：把本讲建立的「技术栈导航图」落到真实的目录树上，学会在仓库里快速定位 HW / SW / Build / Sim 代码。
- 之后再按顺序进入 **u1-l3（硬件平台）** 与 **u1-l4（构建流程总览）**，完成入门层。
- 如果你想先尝一口真实代码，可以直接打开 [README.md 的硬件架构一节](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/README.md)，看看数据面引擎（DPE）那条处理流水线——那是 U4 会深入剖析的内容。

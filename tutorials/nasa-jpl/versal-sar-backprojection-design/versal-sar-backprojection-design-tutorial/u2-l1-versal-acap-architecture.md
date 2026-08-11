# Versal ACAP 异构架构与 NoC

## 1. 本讲目标

在上一篇（u1-l1）里，我们已经建立了 SAR 反投影的直觉，并第一次听说了 Versal ACAP 的「三引擎」。本讲不再重复「SAR 是什么」，而是把镜头拉近，专门回答三个问题：

1. **Versal 芯片内部到底有哪几类计算引擎？它们各擅长什么？** 并把答案映射到本项目的 `design/host`、`design/pl`、`design/aie` 三个目录。
2. **片上网络 NoC 是什么？为什么它是跨域数据搬运的枢纽？** 弄清哪些数据走 NoC、哪些走专用直连流。
3. **各域跑在什么时钟频率上？一个 AIE tile（运算单元）有多少局部存储？** 并用本项目 `common.h` 的宏算一算这些存储够不够用。

学完后，你应该能在脑中画出一幅「数据从 DDR 出发、被各引擎接力处理、最后写回 DDR」的 Versal 功能框图，为后续阅读 AIE 图拓扑（u4）和主机编排（u3）打下硬件层面的基础。

## 2. 前置知识

本讲默认你已经读过 u1-l1，知道以下术语的大致含义。为稳妥起见，这里再用一两句话补一句本讲会用到、但 u1-l1 没展开的概念：

- **异构计算（heterogeneous computing）**：一块芯片里塞进多种不同类型的计算单元（CPU + FPGA + SIMD 阵列），让每种任务交给最擅长它的硬件。这比「一块大 CPU 啥都干」或「把多块芯片用电路板连起来」更省功耗、也更省片外数据搬运。
- **CPU / FPGA / SIMD 阵列**：CPU 擅长「按顺序做控制流和跑操作系统」；FPGA（现场可编程门阵列）擅长「用可重构的电路做位级、流式的定制数据通路」；SIMD 阵列（单指令多数据）擅长「一条指令同时算一批数据」，适合密集向量/矩阵运算。
- **VLIW**：超长指令字，CPU 一次能取出一条很「宽」的指令，里面打包了多个可并行的操作——AIE 内核就用这种结构挖掘指令级并行（ILP）。
- **AXI / AXI4-Stream**：ARM 设计的一套总线协议。AXI（memory-mapped，存储映射）用「地址」读写；AXI4-Stream（流式）没有地址，只管一拍一拍地把数据「倒」过去，靠 `TVALID/TREADY` 握手。
- **DDR**：片外的动态内存（就是日常说的「内存条」那种），容量大但延迟高，是 Versal 各引擎共享的「大盘」。

> 提示：u1-l1 已经讲过 SAR、聚束模式、相位历史、相干累加、双程时延、ACAP、AIE、PL、NoC、OBP 等词。本讲直接使用，不再重定义。

## 3. 本讲源码地图

本讲主要阅读项目自带的设计文档（LaTeX 源码），辅以跨域共享的配置头。

| 文件 | 作用 | 本讲解读重点 |
| --- | --- | --- |
| `doc/sections/intro.tex` | 论文「Introduction」章节，含 Versal ACAP 功能框图与三引擎数据通路描述 | 三引擎定义、本项目「从 ARM 经 NoC 到 AIE」的数据通路 |
| `doc/sections/versal_overview.tex` | 论文「AI Engine Overview」章节，AIE 阵列/并行/数据搬运/时钟/存储速查 | AIE 三级并行、七种数据搬运机制、时钟频率、tile 局部存储规格 |
| `design/common.h` | 三域（Host/AIE/PL）共享的唯一配置头 | 用来做存储容量、带宽的「真值」换算 |

> 说明：这两个 `.tex` 文件不是可执行源码，而是项目作者写给读者的设计说明，是理解硬件映射最权威的「第一手资料」。本讲大量引用其中的原文段落并配永久链接。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**三引擎与异构分工**、**NoC 跨域互联**、**时钟域与 tile 局部存储（32 KB）**。

### 4.1 三引擎与异构分工

#### 4.1.1 概念说明

Versal ACAP（Adaptive Compute Acceleration Platform）是一颗「异构」芯片：在同一块硅片上集成了三类完全不同的计算引擎，各自最适合某一类工作负载。论文 introduction 里的一句话把它定义得很清楚——Versal ACAP 是一个包含 **Scalar Engines（CPU）、Adaptable Engines（FPGA/可编程逻辑）、Intelligent Engines（AI 与 DSP 核）** 的异构计算环境。三类引擎的分工如下：

| 引擎（英文） | 引擎（中文） | 硬件实体 | 最擅长的事 | 在本项目里对应 |
| --- | --- | --- | --- | --- |
| Scalar Engines | 标量引擎 | 双核 ARM Cortex-A72 | 跑 Linux、控制流、任务编排、计时 | `design/host`（主机应用） |
| Adaptable Engines | 适配引擎 | FPGA / 可编程逻辑（PL） | 定制数据通路、DMA 重排、流式拼接 | `design/pl`（HLS 包路由器） |
| Intelligent Engines | 智能引擎 | AI Engine 阵列 + DSP 核 | 密集向量/SIMD 运算 | `design/aie`（反投影内核） |

**为什么要做成异构？** 关键在于「让合适的硬件干合适的活」：
- 控制流（读文件、解析命令行、分配缓冲、启动任务、记时间）是串行的、吃操作系统的——交给 ARM。
- 把 AIE 输出的「乱序数据包」重新拼成一幅连续图像，这种「位级、带握手、定制」的处理交给 FPGA 最自然。
- 反投影要对每个像素做差分距离、相位校正、插值、累加，是海量的浮点向量运算——交给 SIMD 阵列。

如果只用 CPU 做反投影，算不动；如果把控制逻辑塞进 FPGA，开发又慢又别扭。异构的好处是「各取所长」，代价是开发者必须理解三套工具链、并亲自编排跨域的数据流。

#### 4.1.2 核心流程

把三类引擎串起来看一次完整反投影「谁干什么」。注意数据在哪类引擎之间流动：

```text
[ARM Cortex-A72]                 [DDR 片外内存]
   读 CSV、生成像素网格  ──写入──▶  slowtime / rc / 像素
   启动 AIE 图、启动 PL 内核                │
                                            ▼ (经 NoC / GMIO)
                                [AI Engine 阵列]
                                   核心反投影计算：
                                   差分距离→相位校正→插值→累加
                                            │
                                            ▼ (经 PLIO 直连流)
                                  [PL 可编程逻辑]
                                   dma_pkt_router：
                                   把乱序包重排成连续图像
                                            │
                                            ▼ (经 NoC / m_axi)
                                [DDR]  连续图像 ◀── ARM 读回、写文件
```

一句话概括分工：**ARM 管编排，AIE 管算，PL 管拼**。数据的物理搬运则由下一节要讲的 NoC 和专用流共同完成。

#### 4.1.3 源码精读

论文 introduction 对「三引擎 + 数据通路」的描述在 [doc/sections/intro.tex:L74-L91](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/intro.tex#L74-L91)。这段原文把本讲的分工讲得很直白，关键几句译述如下：

- 「我们在双核 Cortex-A72 处理器上设计并集成了一条数据通路，**经过片上网络（NoC）到达 AI Engine**，并用 ping-pong 缓冲、DMA stride 受控重排来高效地喂给 AIE 与 PL 内核。」——点明了 NoC 是 ARM↔AIE 之间的桥梁。
- 「**FPGA 负责承载 DMA 重排逻辑，AI Engine 负责执行核心反投影计算**。」——这一句就是「PL 管拼、AIE 管算」的原始依据。
- 「Cortex-A72 处理器负责编排聚束模式反投影基准测试的启动，并记录执行时间。」——对应 `design/host` 里的计时埋点（u1-l3 / u3-l1 会细讲）。
- 「完成后，AI Engine 把图像**经 PL DMA 重排内核流式输出**，在 DDR 中拼装成一幅连续图像。」——这就是上图里 AIE→PL→DDR 的最后一段。

AIE 作为「智能引擎」的具体形态，见 [doc/sections/versal_overview.tex:L23-L35](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/versal_overview.tex#L23-L35)：它是一片 VLIW 处理器阵列、带 SIMD 向量单元，提供三级并行——ILP（VLIW 单周期多发）、SIMD（向量寄存器）、多核（最多 400 个 tile 同时跑）。这三级并行正是 AIE 适合做反投影密集运算的原因，也是后续 u5 内核讲义里 `aie::vector<float,16>`、`chess_prepare_for_pipelining` 等写法的硬件根基。

#### 4.1.4 代码实践

**实践目标**：把「三引擎」这个抽象概念落到本项目的具体目录上，建立「硬件引擎 ↔ 仓库目录 ↔ 源码语言」的对应表。

**操作步骤**：
1. 打开 [README.md](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/README.md) 的「repo folder hierarchy」目录表。
2. 对照下表，把每个目录归到三类引擎之一（或「工具/文档」）：

| 仓库目录 | 归属引擎 | 你判断的依据 |
| --- | --- | --- |
| `design/host` | Scalar（ARM） | 跑控制流、读 CSV、启动图、计时 |
| `design/pl` | Adaptable（FPGA） | HLS 写的 DMA 包路由器 |
| `design/aie` | Intelligent（AIE） | ADF 图 + 反投影内核 |
| `design/common.h` | 三域共享 | 被三域同时 `#include` |
| `Makefile` / `helper_scripts` / `doc` | 工具/文档 | 构建、部署、说明 |

3. 进入 `design/aie`、`design/pl`、`design/host` 各看一眼文件后缀（`.cc/.h`、`.cpp`、`.cpp`），体会三者虽都是 C/C++，但分别走 AIE 编译器、HLS 编译器、aarch64 交叉编译器（u1-l3 已讲）。

**需要观察的现象**：三域源码在物理上是分开的目录，唯一把它们「绑在一起」的是 `design/common.h` 这一份共享配置。

**预期结果**：你会得到一张「引擎—目录—语言—工具链」的四列表，理解为什么 u1-l2 强调 `common.h` 是三域同步的关键。

#### 4.1.5 小练习与答案

**练习 1**：假设把「DMA 包重排」从 PL 搬到 ARM 上用软件做，会带来什么问题？
**参考答案**：ARM 是标量 CPU，按字节搬运+重排会非常慢，且会和「编排、计时」抢占 CPU；更重要的是 PL 直连 AIE 的流（PLIO）带宽很高，由 ARM 中转要绕道 DDR 两次，反而成了瓶颈。所以这类「定制流式」工作留给 PL。

**练习 2**：本项目里「谁负责记录执行时间」？为什么不能让 AIE 自己计时？
**参考答案**：ARM Cortex-A72 负责计时（`design/host` 用 `CLOCK_MONOTONIC`，见 u3）。AIE tile 上跑的是计算内核、没有跑操作系统，不方便打系统时钟；由统一的 ARM 来分段计时，口径一致、便于对比。

---

### 4.2 NoC 跨域互联

#### 4.2.1 概念说明

**NoC（Network-on-Chip，片上网络）** 是 Versal 内部的「高速路网」。可以把它想象成芯片版的城市路网：路边挂着 DDR 控制器、ARM、PL、AIE 阵列等「建筑」，数据以「数据包/事务」为单位，经路网里的路由器（router/switch）从源头送到目的地。

NoC 和「一根总线」的关键区别在于：
- **多路并行**：总线像单车道，一次只能一个主设备占用；NoC 像多车道立交，多个事务可并行走不同路径，**聚合带宽高得多**。
- **桥接不同域**：ARM、PL、AIE 跑在不同的时钟上、数据位宽也不同（见 4.3）。NoC 在边界做**时钟域跨越（CDC）**和位宽转换，让各域能互通。

在本项目里，NoC 的角色可以用一句话概括：**它是「与 DDR 有关的搬运」都要经过的高速路**。具体有两种用法：

- **GMIO 端口**：DDR ↔ NoC ↔ AIE。AIE 通过 GMIO 直接对 DDR 做突发（burst）读写，突发长度常见 64/128/256 字节。本项目里主机把 slowtime、RC、像素写进 DDR，再由 GMIO 搬进 AIE，靠的就是这条路。
- **存储映射访问（memory-mapped via NoC）**：用于控制或较低带宽的数据搬运——NoC 可以读写 AIE 的局部存储、配置 DMA 描述符。

> ⚠️ 一个容易混淆的点：**PLIO（PL↔AIE）是「直连流」，不走 NoC**。AIE 把图像结果送给 PL 的那条路是 AXI4-Stream 直连，延迟低、带宽满；而 PL 把拼好的图像写回 DDR（`m_axi`）才又走 NoC。换句话说，NoC 管「与 DDR 相关的存取」，PLIO 管「AIE 与 PL 之间的直连流」。

#### 4.2.2 核心流程

把一次反投影里所有「跨域数据搬运」按「是否走 NoC」分类：

```text
数据搬运                          走 NoC？   走的通道
─────────────────────────────────────────────────────────
ARM 写 slowtime/rc/像素 → DDR       是         ARM 的 AXI → NoC → DDR
DDR → AIE 局部存储                  是         GMIO (DDR↔NoC↔AIE)
AIE 内部 tile ↔ tile                否         AXI4-Stream / cascade（阵列内部）
AIE 计算结果 → PL                   否         PLIO 直连流（PL↔AIE）
PL 拼好的图像 → DDR                 是         PL 的 m_axi → NoC → DDR
ARM 读回图像 ← DDR                  是         ARM 的 AXI ← NoC ← DDR
```

结论：**凡是要进出 DDR 的，都过 NoC；凡是在 AIE 阵列内部、或在 AIE 与 PL 之间直连的，有专用通道、不过 NoC**。这个区分在读 `design/aie/graph.h`（u4）时会反复出现——`input_gmio`/`output_plio` 的命名正好对应这两类通道。

#### 4.2.3 源码精读

七种数据搬运机制的清单见 [doc/sections/versal_overview.tex:L41-L64](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/versal_overview.tex#L41-L64)。其中与 NoC 直接相关的两条是：

- **GMIO 端口**（L58-L60）：「在外部 DDR 与 AIE 阵列之间、**经由 NoC** 搬运数据，支持 64/128/256 字节突发。」
- **存储映射访问（NoC）**（L61-L63）：「用于控制或低带宽数据搬运，NoC 可读写局部存储、并用 AXI 存储映射事务配置 DMA 描述符。」

注意同一份清单里 **PLIO 端口**（L55-L57）的措辞是「AIE 阵列与 PL 之间的 AXI4-Stream 连接」——**没有提 NoC**，这印证了上面「PLIO 直连、不过 NoC」的判断。

GMIO/PLIO 的端口规格速查见 [doc/sections/versal_overview.tex:L176-L189](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/versal_overview.tex#L176-L189)：系统级最多各 32 个 GMIO 输入/输出端口；PLIO 接口位宽可配 32/64/128 位，且「每个 32 位占一个 AIE 时钟周期」。这条「32 位/周期」的规格会在 4.3 算带宽时用到。

#### 4.2.4 代码实践

**实践目标**：在还没读 `graph.h` 之前，仅凭命名规则预测每类端口走哪条路、搬什么数据。

**操作步骤**：
1. 在 [doc/sections/versal_overview.tex:L176-L189](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/versal_overview.tex#L176-L189) 记下 GMIO 与 PLIO 的能力（端口数、位宽）。
2. 预测本项目四类端口的归属（**先不要翻 graph.h**）：

| 端口名（预测） | GMIO 还是 PLIO？ | 搬的数据 | 是否走 NoC |
| --- | --- | --- | --- |
| slowtime 进 AIE | ? | 天线几何 | ? |
| RC 数据进 AIE | ? | 距离压缩回波 | ? |
| 目标像素进 AIE | ? | X/Y/Z 像素 | ? |
| 图像结果出 AIE 到 PL | ? | 重建图像 | ? |

3. 等 u4-l3 阅读真实 `graph.h` 后再回来核对。

**需要观察的现象**：你会发现自己能仅凭「数据要不要进出 DDR」推出端口类型——slowtime/RC/像素都要从 DDR 取，所以是 GMIO（走 NoC）；图像要从 AIE 送到 PL 拼接，是直连流，所以是 PLIO（不过 NoC）。

**预期结果**：上表的「?」应分别填成「GMIO/GMIO/GMIO/PLIO」与「是/是/是/否」。这就是「先有直觉、再读源码」的练法。

#### 4.2.5 小练习与答案

**练习 1**：如果想让 AIE 直接从 DDR 读一段大数据块，应该用 PLIO 还是 GMIO？为什么？
**参考答案**：用 GMIO。GMIO 是「DDR ↔ NoC ↔ AIE」的专门通道，支持突发；PLIO 是 PL↔AIE 的直连流，不直接连 DDR。

**练习 2**：为什么作者把「图像 AIE→PL」设计成 PLIO 直连流，而不是让 AIE 经 GMIO 写 DDR、PL 再从 DDR 读？
**参考答案**：直连流延迟低、带宽满，且省去一次「写 DDR + 读 DDR」的双倍带宽与双倍 NoC 占用；图像数据要立刻被 PL 拼接，正好适合流式直连。

---

### 4.3 时钟域与 tile 局部存储（32 KB）

#### 4.3.1 概念说明

**时钟域**：Versal 的四类资源跑在不同的时钟频率上，因为它们用不同的晶体管工艺和架构：

| 资源 | 典型时钟 | 备注 |
| --- | --- | --- |
| ARM Cortex-A72 | ≈ 1.35 GHz | 随器件/速度等级变化 |
| NoC | ≈ 1.0 GHz | 跨域「路网」的统一节拍 |
| AI Engine | ≈ 1.25 GHz | 反投影核心算力来源 |
| PL | ≈ 500 MHz（变化大） | 本项目 PL 包路由器在 312.5 MHz（见 u6） |

频率不同就意味着：数据在两域之间穿过时，必须做**时钟域跨越（CDC）**——用异步 FIFO 之类机制把数据从一个节拍「接」到另一个节拍。在本项目里，NoC（~1.0 GHz）和 PLIO 接口处的 FIFO 天然承担了这个跨越任务，开发者一般不用手写 CDC，但要意识到**「跨域一次」是有延迟成本的**。

**AIE tile 的局部存储**：每个 AIE tile 有 **32 KB 数据存储**，用于放运行时缓冲。这 32 KB 要塞下不少东西：

- ping-pong（双）缓冲；
- 栈、堆、局部变量；
- 同步锁、环形缓冲等。

如果用双缓冲，那么一个 ping-pong「半缓冲」现实地最多到 **16 KB**（因为两半加起来不能超过 32 KB）。此外还有两个相关规格：
- **邻居共享**：一个 tile 可读写最多 3 个相邻 tile 的存储，组成 2×2 区域共 **128 KB**（4 × 32 KB），需用硬件锁协调。
- **程序存储**：另有 **16 KB** 放程序代码，与 32 KB 数据存储分开。

> 重要：这些数字是 **AI Engine（V1）** 的规格，**不是**更新的 AIE-ML 变体（后者存储大小、时钟域都不同）。本项目用的 VCK190 是 V1 器件，所以套用本节这些数字。

#### 4.3.2 核心流程

「32 KB 限容」如何反过来约束内核设计：

```text
内核要处理的每块数据  ──能否塞进 32 KB？──▶  能 → 直接放 tile 局部存储
                                            否 → 拆小 / 用邻居共享扩到 128 KB
                                                  / 改用流式 PLIO/GMIO 边算边搬
```

具体到反投影：RC（距离压缩）数据要被每个重建内核持有以做插值。RC 有多少字节？由 `common.h` 的 `RC_SAMPLES` 决定。下一节的实践就用真值算一下它塞不塞得进 32 KB。

带宽换算的直觉：PLIO「32 位 / AIE 时钟周期」，AIE 时钟 ≈ 1.25 GHz，所以一个 32 位 PLIO 端口的单向带宽约为

\[
B_{\text{PLIO,32b}} \approx 32\text{ 位} \times 1.25\,\text{GHz} \div 8 = 5\,\text{GB/s}
\]

若用 128 位宽 PLIO，则理论上可达约 20 GB/s。这种「频率 × 位宽」的换算，是后面衡量「数据喂得快不快」的基本功。

#### 4.3.3 源码精读

时钟频率与 tile 存储的速查表在 [doc/sections/versal_overview.tex:L191-L226](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/versal_overview.tex#L191-L226)。几条关键原文译述：

- **时钟**（L192-L199）：AIE ≈ 1.25 GHz、NoC ≈ 1.0 GHz、PL ≈ 500 MHz（变化大）、ARM Cortex-A72 ≈ 1.35 GHz。
- **tile 局部数据存储**（L201-L211）：每 tile 共 **32 KB** 运行时数据；用双缓冲时单个 ping-pong 缓冲现实地最多 **16 KB**。
- **邻居共享**（L213-L220）：每 tile 可读写最多 3 个相邻 tile，2×2 区域内合计最多 **128 KB**，需显式加锁协调。
- **程序存储**（L222-L226）：另有 **16 KB** 程序存储，与 32 KB 数据存储分开。

「V1 而非 AIE-ML」的适用范围说明见 [doc/sections/versal_overview.tex:L237-L239](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/versal_overview.tex#L237-L239)。

而本项目的真值参数在 [design/common.h:L17-L45](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/common.h#L17-L45)，其中 `RC_SAMPLES`（L22）= 512，`BC_ELEMENTS`（L45）= 4，是下一节容量换算的输入。

#### 4.3.4 代码实践

**实践目标**：用 `common.h` 的真值，验证 AIE tile 的 32 KB 局部存储对 RC 数据「够用」，建立「宏 → 字节 → 存储上限」的换算直觉。

**操作步骤**：
1. 从 [design/common.h:L22](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/common.h#L22) 读出 `RC_SAMPLES = 512`。
2. 一个 RC 样本是复数浮点（`cfloat` = 实部 + 虚部 = 2 × 4 字节 = 8 字节）。算 RC 数组大小：

\[
\text{RC 字节数} = \text{RC\_SAMPLES} \times 8 = 512 \times 8 = 4096\,\text{B} = 4\,\text{KB}
\]

3. 对照 32 KB 上限：4 KB 只占约 1/8，**绰绰有余**，所以每个重建内核都能把整段 RC 拿在局部存储里反复插值（这正是 u5 要讲的 Data Broadcast 能成立的前提）。
4. 再算广播数据：`BC_ELEMENTS = 4` 个浮点 = 16 字节，可忽略。

**需要观察的现象**：你会发现「`RC_SAMPLES` 不能无限大」——若它大到 RC 数组接近或超过 32 KB，单 tile 就放不下，必须改用流式搬运或邻居共享。默认 512 是安全的。

**预期结果**：RC = 4 KB，远小于 32 KB（也小于一个 16 KB 的 ping-pong 半缓冲）。结论：**当前配置下 RC 可以整段驻留 tile 局部存储**。

> 待本地验证：若你把 `RC_SAMPLES` 改成更大的取值（注意 u1-l4 说只支持 512/256/128/64，所以本项目里不会更大），可用本节的算式自查上限。

#### 4.3.5 小练习与答案

**练习 1**：为什么 NoC 和 AIE 用不同的时钟（~1.0 GHz vs ~1.25 GHz）反而没问题？
**参考答案**：因为跨域处有 CDC 机制（NoC 接口的异步 FIFO 等）。各域按自己最优的频率跑，由接口负责「接拍」，这是异构芯片的常规做法。

**练习 2**：一个 ping-pong「半缓冲」现实地最多多大？为什么不是 32 KB？
**参考答案**：最多约 16 KB。因为 ping-pong 要两半轮流（一边算、一边装），两半共享同一个 tile 的 32 KB 数据存储，所以每半上限约 16 KB。

**练习 3**：若某个内核的工作集是 40 KB，单 tile 放不下，有哪些办法？
**参考答案**：①用邻居共享把 2×2 区域扩到 128 KB；②把数据拆成更小的块、用 ping-pong 边算边搬；③改用 PLIO/GMIO 流式喂入，不在局部存储里一次性放全。

## 5. 综合实践

把本讲三个模块串起来，画一张属于你自己的 **Versal 功能框图**。这是本讲的主实践任务，也是检验你是否真的理解「异构分工 + NoC + 时钟/存储」的标尺。

**实践目标**：在一张图上同时表达「三类引擎的位置」「一次完整反投影的数据通路」「哪些段走 NoC、哪些段直连」。

**操作步骤**：

1. 在纸或绘图工具上画出五个方块：**ARM Cortex-A72**、**PL（FPGA）**、**AI Engine 阵列**、**DDR**、**NoC**。把 NoC 画在中间，其余四个围在四周。
2. 给每个方块标注：典型时钟频率（ARM≈1.35 GHz、PL≈500 MHz、AIE≈1.25 GHz、NoC≈1.0 GHz）和（对 AIE）「每 tile 32 KB 局部存储」。
3. 用箭头画出一次完整反投影的数据通路，并给每条箭头标「通道 + 是否走 NoC」：

```text
            (1) ARM 写 slowtime/rc/像素 ──▶ DDR          [AXI，走 NoC]
            (2) DDR ──▶ AIE                              [GMIO，走 NoC]
            (3) AIE 内部 tile↔tile 计算                   [AXI4-Stream/cascade，不走 NoC]
            (4) AIE ──▶ PL                               [PLIO 直连流，不走 NoC]
            (5) PL ──▶ DDR                               [m_axi，走 NoC]
            (6) ARM ◀── DDR（读回图像）                   [AXI，走 NoC]
```

4. 在图旁写一句话总结三类引擎的分工（ARM 管编排、AIE 管算、PL 管拼）。

**需要观察的现象**：画完你会发现——NoC 像「市中心环线」，所有进出 DDR 的箭头都贴着它；而 AIE↔PL 那条箭头是「专用直达专线」，与 NoC 不沾边。

**预期结果**：一张能用来给同事讲清「数据在 Versal 上怎么流」的框图。后续读 `design/aie/graph.h`（u4）和 `design/host/sar_backproject.cpp`（u3）时，随时可以把新看到的端口/句柄标到这张图上，让它越填越满。

> 如果画完后想自查，可与论文里的 `doc/figures/versal_arch.png`（Versal ACAP Functional Diagram）对照，但请注意那是器件级通用框图，你的图应更聚焦本项目的数据通路。

## 6. 本讲小结

- Versal ACAP 是**异构**芯片：Scalar（ARM Cortex-A72）管控制编排、Adaptable（PL/FPGA）管定制数据通路、Intelligent（AIE 阵列 + DSP）管密集向量运算；本项目把它们分别落到 `design/host`、`design/pl`、`design/aie`。
- 一次反投影的分工是「**ARM 管编排、AIE 管算、PL 管拼**」：ARM 读 CSV/生成像素/启动任务/计时，AIE 做核心反投影，PL 把 AIE 输出的乱序包重排成连续图像。
- **NoC** 是片上「高速路网」，所有进出 DDR 的搬运都走它（GMIO = DDR↔NoC↔AIE；PL 的 `m_axi` 也走 NoC）。
- **PLIO 是 AIE↔PL 的直连流，不过 NoC**——这是读图时常踩的坑，记住「与 DDR 相关才走 NoC」。
- 各域时钟不同（AIE≈1.25 GHz、NoC≈1.0 GHz、PL≈500 MHz、ARM≈1.35 GHz），跨域靠接口处的 CDC 衔接。
- 每个 AIE tile 有 **32 KB** 局部数据存储（双缓冲时半缓冲约 16 KB），邻居共享可扩到 128 KB；本项目默认 `RC_SAMPLES=512` 对应 RC 数组仅 4 KB，能轻松驻留 tile 局部存储。

## 7. 下一步学习建议

本讲建立的是「硬件引擎 + 互联」的全局图景。下一步建议：

1. **学 u2-l2（AI Engine 数据搬运：GMIO、PLIO、缓冲、流与 RTP）**：把本讲粗粒度提到的 GMIO/PLIO/buffer/stream/RTP 逐个讲细，是从「框图」进入「端口 API」的必经一步。
2. **学 u2-l3（ADF 图与数据流执行模型）**：在有了端口概念后，理解 `design/aie/graph.h` 里 kernel/port/connect 的写法和 Kahn 数据驱动执行模型。
3. 之后即可进入 u3（主机编排）和 u4（AIE 图拓扑），那时你会不断把新看到的端口标到本讲第 5 节画的那张框图上。

> 建议在进入下一篇前，先把本讲第 5 节的框图亲手画一遍——它是后续多讲的「随身地图」。

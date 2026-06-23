# Mooncake 项目定位与整体架构

> 单元一 · 第 1 讲（U1-L1）· 入门阶段（beginner）
> 主题：Mooncake 是什么、解决什么问题、KVCache 中心化解耦架构（Prefill/Decode 分离、KVCache 池）以及它在 Kimi 生产环境中的价值。

---

## 1. 本讲目标

学完本讲，你应当能够：

1. 用一句话说清楚 **Mooncake 是什么**、它在 **LLM 推理服务**链路中扮演什么角色。
2. 解释 **KVCache-centric Disaggregated Architecture（以 KVCache 为中心的分离式架构）** 这个核心定位，并能拆出其中的两个关键设计：**Prefill/Decode 解耦** 与 **KVCache 池**。
3. 看懂 Mooncake 的整体架构图，区分 **数据流（data flow）** 与 **控制流（control flow）**。
4. 说出 **Transfer Engine（TE）、Mooncake Store、Master、Client、Mooncake EP、Mooncake PG** 这几个核心组件各自的角色，以及它们之间的分层关系。

本讲是整个学习手册的「第 0 步」，**不要求你已经读过任何源码**，只要求你跟着本讲读完 README 与架构文档。后续每一讲都会深入其中一个组件。

---

## 2. 前置知识

如果你对下面这些词还陌生，没关系，本讲会用最朴素的语言解释。

- **LLM（大语言模型）推理服务**：像 Kimi、ChatGPT 这样的对话产品，背后有一个服务在不停地「读入用户问题 → 生成回答」。这个过程分两个阶段（见下）。
- **Prefill（预填充）阶段**：模型一次性「读完」用户输入的所有 token（比如几万个字的提示词），计算量大、并行度高、耗时长。它直接决定了 **TTFT（Time To First Token，首字延迟）**——用户多久能看到第一个字。
- **Decode（解码）阶段**：模型一个字一个字地「吐」出回答，每一步都依赖上一步的结果。它决定了 **TBT（Time Between Tokens，字间延迟）** 和整体吞吐。
- **KVCache（KV 缓存）**：Transformer 在处理一个 token 时会算出 Key 和 Value 向量。为了避免对历史 token 重复计算，这些向量会被缓存下来，这就是 KVCache。**KVCache 很大**——长上下文场景下动辄几十 GB，它是 LLM 推理里最关键的「中间产物」。
- **RDMA（Remote Direct Memory Access）**：一种能让一台机器**绕过对方 CPU、直接读写对方内存**的高速网络技术。它是 Mooncake 实现「零拷贝、高带宽」数据传输的物理基础。
- **SLO（Service Level Objective，服务等级目标）**：系统承诺的延迟/吞吐指标。Mooncake 必须在满足 SLO 的前提下尽量提升吞吐。

> 一句话直觉：**LLM 推理的核心矛盾是「算力（GPU）紧张」和「KVCache 搬不动、放不下」**。Mooncake 的核心思路是用「多存一点、少算一点」+「把 KVCache 当成一等公民来管理」来破解这个矛盾。

---

## 3. 本讲源码地图

本讲主要阅读**文档与架构图**（这是入门讲，不深入 C++/Python 实现）。下面是你将接触到的关键文件：

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/README.md) | 项目门面：定位、核心架构、各组件一句话介绍、生态集成、快速开始。本讲的主线读物。 |
| [image/architecture.png](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/image/architecture.png) | **核心架构图**：Prefill 池 / Decode 池 / KVCache 池 / 调度器 / Transfer Engine 的关系。 |
| [image/components.png](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/image/components.png) | **开源组件分层图**：应用层 → Store/P2P/EP·PG → Transfer Engine → 硬件层。 |
| [docs/source/design/architecture.md](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/docs/source/design/architecture.md) | Mooncake Store 的架构总览文档，点明「控制流与数据流分离」的设计原则。 |
| [docs/source/design/mooncake-store.md](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/docs/source/design/mooncake-store.md) | Mooncake Store 设计文档：Master / Client 的角色、API、缓存层次。 |
| [docs/source/design/transfer-engine/index.md](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/docs/source/design/transfer-engine/index.md) | Transfer Engine 设计文档：Segment / BatchTransfer 两大抽象。 |

> 后续深入源码的讲义会带你进入 `mooncake-transfer-engine/`、`mooncake-store/`、`mooncake-ep/`、`mooncake-pg/` 这些**源码目录**。本讲先建立全局地图。

---

## 4. 核心概念与源码讲解

### 4.1 Mooncake 的定位：它是什么，解决什么问题

#### 4.1.1 概念说明

Mooncake 是 **Moonshot AI（月之暗面）** 为其 LLM 对话产品 **Kimi** 设计的**推理服务平台（serving platform）**，并在 **FAST 2025** 获得 Best Paper Award 后开源。

它要解决的核心问题是：**当一个 LLM 服务（比如 Kimi）面对海量、长上下文的请求时，如何在不破坏延迟 SLO 的前提下，把整体吞吐尽可能拉高？**

传统做法把 Prefill 和 Decode 挤在同一个 GPU 集群里，会陷入两难：

- **Prefill 是算力密集型**，要吃满 GPU 算力；
- **Decode 是访存密集型**，吃的是显存带宽；
- 两者**资源画像完全相反**，混在一起会互相拖累——Prefill 抢算力时 Decode 卡顿，Decode 占显存时 Prefill 进不去。

Mooncake 的破局思路写在它的副标题里：**"A KVCache-centric Disaggregated Architecture for LLM Serving"**（面向 LLM 服务的、以 KVCache 为中心的分离式架构）。关键词拆开看：

- **Disaggregated（分离/解耦）**：把 Prefill 和 Decode 拆到**两个独立的集群**，各自按自己的资源画像去配 GPU。
- **KVCache-centric（以 KVCache 为中心）**：把 Prefill 算出来的 KVCache 当成一等公民——存起来、复制、跨节点搬运、复用，**用「多存一点」换「少算一点」**。

#### 4.1.2 核心流程

传统 LLM 服务（单集群）的大致流程：

```
请求 → [同一个 GPU 集群：Prefill → 生成 KVCache → 在本机 Decode] → 回复
```

Mooncake 的流程（分离式）：

```
请求 → [Prefill 集群：Prefill，产出 KVCache]
            │  KVCache 卸载到分布式 KVCache 池（CPU/DRAM/SSD）
            ▼
       [分布式 KVCache 池：暂存 / 复制 / 复用 KVCache]
            │  KVCache 按需加载
            ▼
      [Decode 集群：加载 KVCache → 一个字一个字 Decode] → 回复
```

两个集群之间的 KVCache 搬运，就是 **Transfer Engine** 干的活；KVCache 的存取、复制、淘汰，就是 **Mooncake Store** 干的活。

#### 4.1.3 源码精读

README 第一句就明确了 Mooncake 的身份：

> [README.md:29-30](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/README.md#L29-L30)
> Mooncake is the serving platform for Kimi, a leading LLM service provided by Moonshot AI.

README 的 Overview 段落用一句话概括了整个架构的核心思想——**分离 Prefill/Decode 集群**，并**利用 GPU 集群里被闲置的 CPU、DRAM、SSD** 来做一个分离式的 KVCache 池：

> [README.md:77](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/README.md#L77)
> Mooncake features a KVCache-centric disaggregated architecture that separates the prefill and decoding clusters. It also leverages the underutilized CPU, DRAM, and SSD resources of the GPU cluster to implement a disaggregated KVCache pool.

紧接着，README 给出了**核心收益的数据**：在模拟长上下文场景下，吞吐**最高提升 525%**；在 Kimi 真实负载下，**能多承载 75% 的请求**：

> [README.md:81](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/README.md#L81)
> Compared to the baseline method, Mooncake can achieve up to a 525% increase in throughput in certain simulated scenarios while adhering to SLOs. Under real workloads, Mooncake's innovative architecture enables Kimi to handle 75% more requests.

> 为什么强调「CPU/DRAM/SSD 被闲置」？因为推理机里 GPU 是稀缺资源，但同一台机器上的 CPU、内存、SSD 往往没吃满。Mooncake 把 KVCache 池放在这些**已经买了但没用满**的资源上，相当于「免费的存储」，这正是「以存储换算力」能成立的经济基础。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：通过阅读 README 的 Updates 时间线，建立「Mooncake 不只是一个论文 demo，而是被生态广泛采用的生产级组件」的直觉。

**操作步骤**：

1. 打开 [README.md](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/README.md)，找到 `<h2 id="updates">🔄 Updates</h2>`（约第 33 行）。
2. 通读更新列表，**统计 Transfer Engine 被哪些主流推理框架集成**（提示：vLLM、SGLang、TensorRT-LLM、NIXL、LMDeploy、LMCache 等）。
3. 注意 2025-02-25 那条：**FAST 2025 Best Paper Award**。

**需要观察的现象**：你会看到从 2024 年开源 Transfer Engine、到 2025 年开源 Store，再到 2026 年与 vLLM/SGLang/PyTorch 生态深度融合的一条清晰演进线。这说明 Mooncake 的组件（尤其 Transfer Engine 和 Store）**已经是 LLM 推理基础设施的事实标准之一**。

**预期结果**：你能列出至少 3 个集成了 Mooncake 的开源推理框架，并说出它们用 Mooncake 做什么（KVCache 传输 / PD 分离 / 分布式 KVCache 池）。

#### 4.1.5 小练习与答案

**练习 1**：用一句话向一个没听过 Mooncake 的同事解释「Mooncake 是什么」。

> **参考答案**：Mooncake 是 Moonshot AI 为 Kimi 打造的 LLM 推理服务平台，核心思想是把 Prefill 和 Decode 拆成两个集群，并用集群里闲置的 CPU/内存/SSD 做成一个分布式 KVCache 池，从而在不破坏延迟的前提下大幅提升吞吐。

**练习 2**：为什么 Mooncake 要把 KVCache 池放在「CPU/DRAM/SSD」而不是「GPU 显存（VRAM）」上？

> **参考答案**：GPU 显存是推理时最稀缺、最贵的资源，必须留给模型权重和当前正在算的 KVCache；而同一台机器的 CPU/DRAM/SSD 容量大得多且常被闲置，适合做「卸载/暂存/复用」的大容量池。把冷 KVCache 放到这些便宜资源上，正是「以存储换算力」的前提。

---

### 4.2 KVCache 中心化解耦架构（核心架构图）

#### 4.2.1 概念说明

这一节是本讲的重头戏：**看懂那张 architecture.png**。

这张图把 Mooncake 拆成三大块：

1. **Prefill Pool（预填充池）**：由若干 **Prefill Instance** 组成，每个实例带 GPU/VRAM、本地分块调度器和 Paged KVCache。它专做 Prefill。
2. **Decoding Pool（解码池）**：由若干 **Decoding Instance** 组成，每个实例带 GPU/VRAM 和本地调度器。它专做 Decode。
3. **Distributed KVCache Pool（分布式 KVCache 池）**：横跨 CPU/DRAM/SSD 的大容量共享池，Prefill 把算出的 KVCache 卸载到这里，Decode 从这里取。

在这三大块之间，还有三类**调度器（Scheduler）**做决策，以及一个 **KVCache Transfer Engine** 做实际的 KVCache 搬运。

每个阶段都有自己的**优化目标（Optimization Goal）**，这是理解整张图的「题眼」：

| 阶段 | 优化目标 | 约束条件（s.t. = subject to，即「在……前提下」） |
|------|----------|------------------------------------------------|
| Prefill | **最大化 KVCache 复用（max Cache Reuse）** | 满足 TTFT SLO、MFU（算力利用率）下界、KVCache 放得下 DRAM |
| Decode | **最大化吞吐（max Throughput）** | 满足 TBT SLO、KVCache 放得下 VRAM |

> 直觉：Prefill 阶段最贵的是「算」，所以目标是**别重复算**（复用已有 KVCache）；Decode 阶段瓶颈在「带宽」，所以目标是**多塞请求、把吞吐跑满**。两者目标不同，正是要拆成两个集群的根本原因。

#### 4.2.2 核心流程

把图上的箭头翻译成「数据流」和「控制流」两类：

**数据流（Data Flow，KVCache 实际搬运的路径）**：

```
Prefill Instance（生成 KVCache）
        │  ① 卸载（offload）
        ▼
KVCache Transfer Engine ──RDMA 零拷贝──► Distributed KVCache Pool
                                              │  ② 加载（load）
                                              ▼
                                        Decoding Instance（消费 KVCache）
```

关键点：**KVCache 的实际搬运发生在 Prefill/Decode 实例与 KVCache 池之间，由 Transfer Engine 完成。**

**控制流（Control Flow，调度决策的路径）**：

```
请求 ──► Cache-aware Prefill Scheduler ──► 决定分给哪个 Prefill Instance（优先挑缓存命中的）
                                          │
KVCache Balance Scheduler ─────────────► 平衡各处 KVCache 分布
                                          │
Load-balance Decoding Scheduler ────────► 决定分给哪个 Decoding Instance（按负载均衡）
```

- **Cache-aware Prefill Scheduler**：缓存感知的 Prefill 调度器——新请求来了，**优先把它分到「已经有它的前缀 KVCache」的实例**，提高复用率。
- **KVCache Balance Scheduler**：在 KVCache 池层面做负载均衡。
- **Load-balance Decoding Scheduler**：按吞吐/负载把 Decode 任务分到 Decode 实例。

> 一个非常重要的设计原则（下一节也会反复出现）：**控制流和数据流是分离的**。调度器只做「决策」（决定谁存谁取、存在哪），**它不碰 KVCache 的实际字节**；实际搬运交给 Transfer Engine。这样调度路径轻量、数据路径高带宽，各司其职。

#### 4.2.3 源码精读

架构图出现在 README 的 Overview 段，正好嵌在「分离 Prefill/Decode 集群 + 做 KVCache 池」这句话下面：

> [README.md:79](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/README.md#L79)
> ![architecture](image/architecture.png)

紧跟架构图，README 点出了 **KVCache-centric scheduler（以 KVCache 为中心的调度器）** 这个核心，以及它面对「过载」时的杀手锏——**基于预测的提前拒绝策略（prediction-based early rejection policy）**：

> [README.md:81](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/README.md#L81)
> The core of Mooncake is its KVCache-centric scheduler, which balances maximizing overall effective throughput while meeting latency-related Service Level Objectives (SLOs). ... To mitigate these, we developed a prediction-based early rejection policy.

架构设计文档则把「分离 Prefill/Decode」这件事还原成了**工程语言**：用（GPUDirect）RDMA 做零拷贝搬运，并尽量榨干多网卡带宽：

> [docs/source/design/architecture.md:3](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/docs/source/design/architecture.md#L3)
> ... by constructing a multi-level caching pool on high-speed interconnected DRAM/SSD resources. Compared to traditional caching systems, Mooncake utilizes (GPUDirect) RDMA technology to transfer data directly from the initiator's DRAM/VRAM to the target's DRAM/VRAM in a zero-copy manner, while maximizing the use of multi-NIC resources on a single machine.

#### 4.2.4 代码实践（本讲核心实践：画组件关系图）

**实践目标**：用一段文字 + 一个简单的 ASCII 图，把 Mooncake 的组件关系图**自己复现一遍**，并标注数据流与控制流。这是本讲规格里要求的核心实践任务。

**操作步骤**：

1. 打开 [image/architecture.png](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/image/architecture.png)，花 2 分钟看清三大池（Prefill Pool / Decoding Pool / Distributed KVCache Pool）、三个调度器、以及 KVCache Transfer Engine 的位置。
2. 在你的笔记里，照着下面的骨架填空，**把每个方框的角色用自己的话写一句**：

```
                ┌─────────────────────────────┐
   请求 ───────► │ Cache-aware Prefill Scheduler│ ──(控制流)──┐
                └─────────────────────────────┘            │
                                                            ▼
                                          ┌──────────────────────────┐
                                          │   Prefill Pool           │
                                          │  [Prefill Instance] x N  │
                                          │   GPU/VRAM + Paged KVCache│
                                          └────────────┬─────────────┘
                                    ①卸载(数据流,RDMA) │ ②加载(数据流,RDMA)
                                ┌──────────────────────┴──────────────────────┐
                                ▼                                              │
                ┌────────────────────────────────┐    KVCache Transfer Engine │
                │  Distributed KVCache Pool      │◄──────────────────────────┘
                │   (CPU / DRAM / SSD)           │
                │  ◄── KVCache Balance Scheduler │
                └────────────────┬───────────────┘
                                 │ ②加载(数据流,RDMA)
                                 ▼
                          ┌──────────────────────┐
                          │   Decoding Pool      │
                          │ [Decoding Instance]xN│
                          │  GPU/VRAM            │
                          └──────────┬───────────┘
                                     │
   Load-balance Decoding Scheduler ──┘(控制流)
                                     ▼
                                   回复用户
```

3. 用**一段话**总结：哪几条线是数据流（KVCache 实际搬运），哪几条是控制流（调度决策）。标注清楚 Transfer Engine 负责数据流、三个 Scheduler 负责控制流。

**需要观察的现象**：你会清晰地看到——**KVCache Transfer Engine 是唯一搬运字节的组件**，而所有 Scheduler 都只连到「池/实例」做决策，不直接碰字节。这就是「控制流与数据流分离」。

**预期结果**：你能指着图说出一个请求从进入到回复，KVCache 经历了「生成 → 卸载到池 → 从池加载 → 消费」四个环节，以及每个环节由谁负责。

> 说明：上图为**教学示意**，用于帮助理解；精确的组件布局以仓库原图 [architecture.png](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/image/architecture.png) 为准。

#### 4.2.5 小练习与答案

**练习 1**：在架构图里，Prefill Instance 和 Decoding Instance 为什么**不能**合并在同一个池里？

> **参考答案**：两者资源画像相反——Prefill 吃算力、Decode 吃带宽；优化目标也不同——Prefill 追求「缓存复用+控制 TTFT」，Decode 追求「吞吐+控制 TBT」。合并会互相争抢资源、互相拖累，所以必须解耦成两个独立池，各自按画像配置 GPU。

**练习 2**：架构图里标注的 Prefill 阶段约束 `KvCache < DRAM` 和 Decode 阶段约束 `KvCache < VRAM` 分别是什么意思？

> **参考答案**：Prefill 阶段要把算出的 KVCache 卸载到池里的 DRAM，所以 KVCache 体量要放得下 DRAM；Decode 阶段要在 GPU 显存（VRAM）里持有正在用的 KVCache，所以放得下 VRAM。这两个约束本质上是「容量约束」，决定了每个阶段能并行处理多少请求。

---

### 4.3 核心组件关系：Transfer Engine / Store / Master / Client / EP / PG

#### 4.3.1 概念说明

architecture.png 描述的是**逻辑推理架构**（Prefill/Decode 怎么分离）。但当你真正 clone 仓库、去看 `mooncake-*/` 这些目录时，你需要另一张图——**components.png** 描述的是**开源组件的分层关系**。本节把这两张图接起来。

开源后的 Mooncake 由这些**核心组件**构成：

| 组件 | 角色 | 一句话定位 |
|------|------|-----------|
| **Transfer Engine（TE）** | 数据搬运 | Mooncake 的内核：高性能、零拷贝、跨异构存储/网络/加速器的批量数据传输框架。 |
| **Mooncake Store** | 分布式 KVCache 存储引擎 | 建在 TE 之上的分布式 KV 缓存存储，负责 KVCache/模型权重的存取、复制、淘汰、分层缓存。 |
| **Mooncake EP** | 专家并行（Expert Parallelism） | 面向大规模 MoE 推理的、带「活跃 rank 感知」的容错专家并行（类 DeepEP 风格）。 |
| **Mooncake PG** | 进程组（Process Group） | 可作为 `torch.distributed` 后端的进程组，提供容错集合通信与 rank 恢复能力。 |
| **Mooncake P2P Store** | 示例/早期形态 | 一个 P2P 存储示例，演示 TE 的用法。 |

而 **Store** 内部又有两个关键角色（这点很容易被初学者忽略）：

- **Master Service（主节点服务）**：**只管「控制流/元数据」**——集中管理「哪个对象（KVCache）存在哪台机器的哪段内存」，负责空间分配、复制放置、淘汰决策。**它不搬运任何数据字节。**
- **Client（客户端）**：身兼两职——(1) 作为**客户端**向上层应用（如 vLLM）发起 `Put/Get`；(2) 作为**存储节点**贡献一段连续内存给集群（所谓「store server」角色）。**真正的数据搬运发生在 Client 与 Client 之间，绕过 Master。**

components.png 的分层从上到下是：

```
应用层（Applications）：vLLM / SGLang / TensorRT-LLM / LMCache / LMDeploy / ...
        │  通过 C/C++ / Python / Go / Rust API 接入
        ▼
服务层（Backend Services）：Mooncake Store / Mooncake P2P / Mooncake EP·PG
        │  建立在 Transfer Engine 之上
        ▼
内核层：Mooncake Transfer Engine（Batch Transfer 接口 + 多种 Transport）
        │  RDMA / TCP / CXL·SHM·NVMe-oF / MultiNode NVLink / Ascend HIXL ...
        ▼
硬件层：DRAM / VRAM(GPU) / NVMe SSD / RDMA NIC / PCIe ...（NVIDIA / AMD / Ascend / Cambricon / 摩尔线程 / 平头哥 ...）
```

> 关键直觉：**Transfer Engine 是地基**。Store、P2P、EP/PG 都盖在它上面。理解了 TE，就理解了 Mooncake「快」的来源；理解了 Store，就理解了 Mooncake「把 KVCache 当对象管理」的来源。

#### 4.3.2 核心流程

以一次 **KVCache 的 `Put`（写入）+ `Get`（读取）** 为例，把组件串起来（这是后续 Store 讲义的主线，这里先建立直觉）：

```
应用（vLLM）                        Master Service              目标 Client（贡献内存的节点）
    │                                    │                            │
    │── PutStart(key, 长度, 副本数) ────► │  ①分配空间、决定复制放哪      │
    │◄── 返回 replica_list（存哪几个节点） │                            │
    │                                    │                            │
    │── 通过 Transfer Engine 把字节 ─────────────────────────────────► │ ②实际数据搬运
    │   零拷贝 RDMA 写到目标 Client                                        │
    │                                    │                            │
    │── PutEnd(key) ───────────────────► │  ③标记对象可读              │
    │                                    │                            │
   （之后）Get：先问 Master 拿 replica_list，再用 Transfer Engine 从某个 Client 零拷贝读回
```

要点：
- **Master 只参与 ①③（元数据/控制流）**，从不碰字节；
- **字节通过 Transfer Engine 在 Client↔Client 之间直传（②数据流）**；
- 这正是 architecture.md 里「**控制流与数据流分离**」原则的工程落地。

#### 4.3.3 源码精读

README 把每个开源组件的作用各用一段话讲清楚。先看 **Transfer Engine**——它是「Mooncake 的核心」：

> [README.md:90,92](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/README.md#L90-L92)
> The core of Mooncake is the Transfer Engine (TE), a high-performance data transfer framework. TE offers a unified interface for batched data movement across diverse storage, network, and accelerator environments.

再看 **Mooncake Store**——明确它是「建在 Transfer Engine 之上」的分布式 KVCache 存储引擎：

> [README.md:114-116](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/README.md#L114-L116)
> Mooncake Store is a high-performance distributed key-value cache storage engine designed for LLM inference. Built on the Transfer Engine, it stores and manages reusable KV caches and model weights across inference clusters ...

再看 **Mooncake EP 与 PG**——它们把 Mooncake 从「数据搬运」延伸到「容错的分布式执行」：

> [README.md:133-135](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/README.md#L133-L135)
> Mooncake EP and Mooncake PG extend Mooncake from high-performance data movement to fault-tolerant distributed execution for large-scale MoE inference. ... Mooncake PG provides a PyTorch distributed process-group backend ...

「Tensor-Centric 生态」这一段则点明了**分层与数据载体**——张量（Tensor）贯穿全栈，TE 负责搬运、Store 负责管理、Backend 负责弹性计算：

> [README.md:152-154](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/README.md#L152-L154)
> Mooncake establishes a full-stack, Tensor-oriented AI infrastructure where Tensors serve as the fundamental data carrier. The ecosystem spans from the Transfer Engine ... to Mooncake Store ... up to the Mooncake Backend ...

在 Store 设计文档里，明确点出 **Master 与 Client 这两个角色**，并强调 Master 不接管数据流：

> [docs/source/design/mooncake-store.md:26](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/docs/source/design/mooncake-store.md#L26)
> ... there are two key components in Mooncake Store: **Master Service** and **Client**.

> [docs/source/design/mooncake-store.md:76](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/docs/source/design/mooncake-store.md#L76)
> **Note: The Master Service does not take over any data flow, only providing corresponding metadata information.**

Transfer Engine 文档则点出它的**两大核心抽象**——Segment（可被远程读写的连续地址空间）与 BatchTransfer（一组离散空间的批量同步搬运）：

> [docs/source/design/transfer-engine/index.md:4](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/docs/source/design/transfer-engine/index.md#L4)
> Mooncake Transfer Engine is a high-performance, zero-copy data transfer library designed around two core abstractions: Segment and BatchTransfer.

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：把「开源组件分层图（components.png）」与「目录结构」对上号，建立「图上每个方框对应仓库哪个目录」的肌肉记忆。

**操作步骤**：

1. 打开 [image/components.png](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/image/components.png)，找到图上的：**Mooncake Store / Mooncake EP·PG Backend / Mooncake Transfer Engine (TE)** 三个方框。
2. 在仓库根目录对照下列**源码目录**（本讲只看目录名，不读实现）：
   - `mooncake-transfer-engine/` → 对应图中的 **Mooncake Transfer Engine (TE)**
   - `mooncake-store/` → 对应图中的 **Mooncake Store**
   - `mooncake-ep/` → 对应图中的 **Mooncake EP**（专家并行）
   - `mooncake-pg/` → 对应图中的 **Mooncake PG**（进程组 / Backend）
   - `mooncake-p2p-store/` → 对应图中的 **Mooncake P2P**（示例）
   - `mooncake-common/` → 多个组件共享的公共代码
3. 在笔记里画一条**自上而下的依赖箭头**：`应用(vLLM/SGLang) → Store/P2P/EP·PG → Transfer Engine → 硬件`。

**需要观察的现象**：你会确认 **Transfer Engine 处在最底层、是所有上层服务的地基**，而上层服务通过 C/C++/Python/Go/Rust 多语言 API 对外暴露能力。

**预期结果**：你能说出「我想学 KVCache 存取，就去看 `mooncake-store/`；想学高性能数据搬运，就去看 `mooncake-transfer-engine/`；想学 MoE 容错，就去看 `mooncake-ep/` 和 `mooncake-pg/`」。

> 待本地验证：若你已 clone 仓库，可执行 `ls mooncake-*` 自行核对目录名与上表的对应关系。

#### 4.3.5 小练习与答案

**练习 1**：为什么说「Transfer Engine 是 Mooncake 的地基」？

> **参考答案**：因为 Mooncake Store、P2P Store、Mooncake EP/PG 都建立在 Transfer Engine 之上。Store 用 TE 做 KVCache 的零拷贝传输；EP/PG 用 TE/通信能力做分布式执行。TE 提供的「Segment + BatchTransfer」抽象 + 多种 Transport（RDMA/TCP/NVMe-oF/NVLink…）是上层所有服务「快」的共同来源。

**练习 2**：Mooncake Store 里 **Master Service 和 Client** 的分工是什么？为什么数据搬运要「绕过 Master」？

> **参考答案**：Master 只做控制流/元数据（分配空间、记录对象存在哪、复制放置、淘汰决策），Client 既是发请求的客户端、也贡献内存当存储节点。实际 KVCache 字节通过 Transfer Engine 在 Client↔Client 之间直传、绕过 Master——这样 Master 不会成为数据带宽瓶颈，控制路径（轻量 RPC）和数据路径（高带宽 RDMA）彻底分离。

---

## 5. 综合实践

**任务：用一张完整的「组件关系 + 数据流/控制流」图，把本讲三个模块串起来。**

要求你产出一幅**自己画的图**（ASCII 或手绘拍照均可）和**一段 150 字左右的说明**，必须同时包含：

1. **三大逻辑池**：Prefill Pool、Decoding Pool、Distributed KVCache Pool。
2. **三个调度器**：Cache-aware Prefill Scheduler、KVCache Balance Scheduler、Load-balance Decoding Scheduler，并标注它们走的是**控制流**。
3. **KVCache Transfer Engine**，并标注它走的是**数据流**。
4. **开源组件分层**：在最下方标出「应用层 → Store/P2P/EP·PG → Transfer Engine → 硬件」，说明 Transfer Engine 同时是架构图里「KVCache Transfer Engine」的工程实现基础。
5. **一个请求的生命周期**：用编号 ①②③④ 标出请求从进入到回复，KVCache 经历「Prefill 生成 → 卸载入池 → 调度到 Decode 实例 → 从池加载并 Decode」的全过程。

**检查清单（自检）**：

- [ ] 我能区分哪几条线是数据流（搬运字节）、哪几条是控制流（调度决策）。
- [ ] 我说明了 Master 只做元数据、Client↔Client 直传字节。
- [ ] 我点出了 Prefill 与 Decode 两个阶段各自的优化目标与约束。
- [ ] 我把「架构图（逻辑推理架构）」和「components.png（开源组件分层）」这两张图通过 Transfer Engine 联系在了一起。

> 这是「源码阅读型 + 画图型」实践，不需要运行任何命令。完成它意味着你已经建立了 Mooncake 的全局心智模型，可以进入下一讲深入任一组件。

---

## 6. 本讲小结

- **Mooncake 是什么**：Moonshot AI 为 Kimi 打造的 LLM 推理服务平台，FAST 2025 Best Paper，核心定位是 **KVCache-centric Disaggregated Architecture**。
- **核心架构思想**：把 **Prefill 和 Decode 拆成两个独立集群**（资源画像相反、优化目标不同），并用集群里**闲置的 CPU/DRAM/SSD** 做成 **Distributed KVCache Pool**，以「多存少算」换取高吞吐。
- **两个阶段的目标**：Prefill 追求 **最大化 KVCache 复用**（约束 TTFT/MFU/DRAM 容量）；Decode 追求 **最大化吞吐**（约束 TBT/VRAM 容量）。
- **控制流与数据流分离**：调度器（Cache-aware Prefill / KVCache Balance / Load-balance Decoding）只做决策；**KVCache Transfer Engine** 负责实际的零拷贝 RDMA 搬运。
- **开源组件分层**：应用层 → **Mooncake Store / P2P / EP·PG** → **Transfer Engine（地基）** → 硬件层；其中 Store 内部分 **Master（只管元数据）** 与 **Client（既发请求又贡献内存，字节在 Client 间直传）**。
- **收益**：模拟场景吞吐最高 +525%，Kimi 真实负载下多承载 75% 请求。

---

## 7. 下一步学习建议

本讲建立了**全局地图**，下一讲建议沿着地图选一条线深入：

1. **想先搞懂「为什么这么快」** → 进入 **Transfer Engine** 讲义。重点读 `mooncake-transfer-engine/`，理解 `Segment` 与 `BatchTransfer` 两大抽象、拓扑感知路径选择、多网卡带宽聚合。可从 [transfer_engine_bench 示例](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/docs/source/design/transfer-engine/index.md#L92) 跑起来感受带宽。
2. **想先搞懂「KVCache 怎么存取复用」** → 进入 **Mooncake Store** 讲义。重点读 `mooncake-store/src/` 里的 `master_service.cpp`、`real_client.cpp`、`dummy_client.cpp`，理解 `PutStart/PutEnd/Get` 的控制流与数据流。
3. **想搞懂「MoE 推理如何容错」** → 进入 **Mooncake EP / PG** 讲义。重点读 `mooncake-ep/` 与 `mooncake-pg/`，理解「活跃 rank 感知」与「rank 恢复」。

> 阅读顺序建议：**Transfer Engine → Mooncake Store → EP/PG**。因为 Store 和 EP/PG 都依赖 TE，先打地基再看上层会顺畅很多。

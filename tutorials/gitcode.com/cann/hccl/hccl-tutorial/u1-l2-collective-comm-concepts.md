# 集合通信核心概念与算法

## 1. 本讲目标

本讲是阅读 HCCL 源码之前的「领域语言」课。HCCL 的所有代码——算子入口、算法选择器、拓扑适配、执行编排——都是用集合通信领域的术语写成的。如果不先弄懂这些术语，后面看代码会处处卡壳。

学完本讲，你应当能够：

- 准确说出 **rank、通信域、通信算子、通信算法、通信引擎** 这五个核心概念的含义，并能区分它们。
- 看懂 HCCL 用来描述集群网络的 **RankGraph 拓扑模型**：Node、Endpoint、Edge、Link、netLayer、Fabric、TopoInstance。
- 复述 **AllReduce / AllGather / ReduceScatter** 等通信原语的作用与区别。
- 说出 **Ring、RHD、NHR、Pipeline** 等算法各自适合的场景，以及 HCCL 的**分级通信**是如何把一次 AllReduce 拆成「节点内 ReduceScatter → 节点间 AllReduce → 节点内 AllGather」三段的。

> 本讲承接 [u1-l1 HCCL 项目定位与 CANN 软件栈](u1-l1-project-overview.md)。上一讲我们建立了「HCCL 是什么、hccl 与 hcomm 怎么分工、软件分几层」的全局认知；本讲不再重复这些，而是深入「集合通信这件事本身」。

## 2. 前置知识

本讲尽量从零讲起，但有两点背景会让理解更顺畅：

1. **分布式训练为什么需要通信**：数据并行下，每张 NPU 各自处理不同的小批量数据、各自算出梯度，每个训练步必须把所有 NPU 的梯度汇总平均，才能一起更新同一份权重。这个「汇总平均」就是一次 AllReduce。通信的速度直接决定了扩展效率，所以它是个性能瓶颈。
2. **本仓 cann/hccl 与 cann/hcomm 的分工**（见 u1-l1）：hccl 负责算子入口与算法选择编排，hcomm 负责通信域/拓扑管理与底层搬数据原语。本讲讲的是两者共用的「领域概念」，这些概念在两仓的文档里是一致的。

如果你完全没接触过并行计算，也不用担心——本讲会用类比把这些概念讲清楚。

## 3. 本讲源码地图

本讲以 HCCL 的中文文档为「源码」，因为概念定义的权威来源正是这些文档；同时引用少量头文件来把概念落到真实接口上。

| 文件 | 作用 |
| --- | --- |
| [docs/zh/user_guide/concepts.md](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/user_guide/concepts.md) | HCCL 基本概念（rank、通信域、算子、算法）与术语缩略语表。 |
| [docs/zh/architecture/architecture-brief.md](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/architecture/architecture-brief.md) | 软件架构简介，第 2 章「集合通信模型」是本讲拓扑与通信引擎的主要依据。 |
| [docs/zh/user_guide/coll_algo_intro/algo_intro.md](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/user_guide/coll_algo_intro/algo_intro.md) | 通信算法简介：Ring/RHD/NHR/Pipeline 等算法特点、α-β 耗时模型、分级通信原理表。 |
| [docs/zh/user_guide/coll_algo_intro/hierarchical_comm_principle.md](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/user_guide/coll_algo_intro/hierarchical_comm_principle.md) | ReduceScatter / AllGather / AllReduce 三个算子的分级通信流程图解。 |
| [include/hccl.h](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/include/hccl.h) | 对外 C 接口头文件，用来把「通信原语」概念对应到真实函数签名。 |

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：

1. **4.1 基本概念、术语与通信原语**——建立最基础的词汇表。
2. **4.2 通信模型与 RankGraph 拓扑**——HCCL 如何用图来描述集群网络。
3. **4.3 通信引擎与同步机制**——谁在真正搬数据。
4. **4.4 通信算法、α-β 模型与分级通信原理**——算法如何选、为什么分级。

### 4.1 基本概念、术语与通信原语

#### 4.1.1 概念说明

集合通信（Collective Communication）是指「一组进程协同参与的通信操作」。它和「点对点通信」（一个进程发给另一个进程）相对。HCCL 的世界里有四个最基础的概念：

- **rank（通信成员）**：参与通信的最小逻辑实体，每个 rank 有一个从 0 开始的唯一标识。你可以把它想成「一个参与训练的进程」，通常绑定一张 NPU。
- **通信域（Communicator）**：一组 rank 的组合，描述「这次通信的范围」。一个训练任务可以创建多个通信域，一个 rank 也可以同时加入多个通信域。
- **通信算子（Operator）**：在通信域内完成某类通信任务的算子，例如 AllReduce、Broadcast。
- **通信算法（Algorithm）**：针对不同拓扑、数据量、硬件资源，同一个算子会采用不同的实现算法，例如 Ring、Mesh。

这四个概念之间的关系可以类比为一次「会议室讨论」：

| 概念 | 类比 |
| --- | --- |
| rank | 参会的人 |
| 通信域 | 这次会议的所有参会者 |
| 通信算子 | 「每个人都要汇总自己的意见」（AllReduce）这类任务形式 |
| 通信算法 | 完成这个任务的具体流程——是轮流发言（Ring）还是分组汇总（Mesh） |

#### 4.1.2 核心流程

一次集合通信的抽象流程：

```
1. 确定通信域：哪些 rank 参与这次通信，各自的全局 ID 是什么
2. 选择算子：本次要做 AllReduce / AllGather / ReduceScatter / ……
3. 选择算法：根据拓扑和数据量，挑 Ring / RHD / NHR / ……
4. 在通信域内执行算法：各 rank 按算法规定的步骤交换并归约数据
5. 每个 rank 拿到结果，写入自己的输出 buffer
```

注意第 3 步「选择算法」——这正是 HCCL 源码里**算法选择器（Selector）**要做的事，也是后续 u3-l2 会深入的部分。本讲先把「有哪些算法可选」讲清楚。

#### 4.1.3 源码精读

HCCL 的权威概念定义在 `concepts.md`。四个基本概念的原文如下：

> [docs/zh/user_guide/concepts.md:18-21](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/user_guide/concepts.md#L18-L21) 定义了 rank、通信域、通信算子、通信算法。

其中关于组网形态，文档还区分了两种物理边界：

> [docs/zh/user_guide/concepts.md:13-16](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/user_guide/concepts.md#L13-L16) 给出 AI Server（8/16 卡 NPU 组成的服务器）、AI 集群（多台 Server 经交换设备互联），以及**超节点组网**（Server 间用「灵衢总线交换设备」连接）的概念。这三层物理边界正是后面「分级通信」与 `level0/level1/level2` 三级拓扑的由来。

「通信算子」对应到真实代码，就是 `include/hccl.h` 里声明的一组 C 函数。最经典的三个原语签名如下：

> [include/hccl.h:35-37](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/include/hccl.h#L35-L37) 是 `HcclAllReduce`：把所有 rank 的输入做归约（sum/min/max/prod），结果复制到每个 rank 的输出。
>
> [include/hccl.h:67-69](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/include/hccl.h#L67-L69) 是 `HcclReduceScatter`：归约之后**分散**——每个 rank 只拿到归约结果中的一段。
>
> [include/hccl.h:120-121](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/include/hccl.h#L120-L121) 是 `HcclAllGather`：**收集**——每个 rank 把自己的一段凑到一起，所有 rank 都拿到完整拼接结果。

它们的共同参数模式是：`sendBuf`（输入）、`recvBuf`（输出）、`count`（元素个数）、`dataType`（数据类型）、`comm`（通信域）、`stream`（异步流）。带归约语义的算子（AllReduce/ReduceScatter/Reduce）还多一个 `op`（归约类型）。这套统一参数模型是后面 u2-l1 系统梳理全部算子的基础。

最后，HCCL 文档里到处都是缩略语，`concepts.md` 给出了一张完整术语表：

> [docs/zh/user_guide/concepts.md:25-42](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/user_guide/concepts.md#L25-L42) 是术语缩略语表，覆盖 NPU、HCCL、HCOMM、HCCS、HCCP、TOPO、PCIe、QP、SDMA、RDMA、RoCE、AIV、TS、CCU 等。先记住几个高频的：**HCCS**（CPU/NPU 间高速互联）、**RDMA/RoCE**（跨机直接内存访问）、**AIV**（Vector Core）、**TS**（任务调度器）、**CCU**（集合通信加速单元）。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：把抽象的「通信算子」概念对应到真实接口，区分集合通信与点对点通信。

**操作步骤**：

1. 打开 [include/hccl.h](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/include/hccl.h)，浏览其中声明的全部函数。
2. 把它们分成两类：
   - **集合通信（所有 rank 一起参与）**：如 `HcclAllReduce`、`HcclBroadcast`、`HcclReduceScatter`、`HcclAllGather`、`HcclAlltoAll`。
   - **点对点通信（一个发给另一个）**：如 `HcclSend`、`HcclRecv`。
3. 对每个集合通信算子，用一句话写出「输入是什么、输出是什么、谁拿到结果」。

**需要观察的现象**：集合通信算子的签名里**没有** `destRank`/`srcRank` 这类「对端」参数（因为所有 rank 都参与）；而点对点算子 `HcclSend`/`HcclRecv` 带有 `destRank`/`srcRank`（见 [include/hccl.h:154-169](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/include/hccl.h#L154-L169)）。

**预期结果**：你能从参数签名本身判断一个算子是集合通信还是点对点通信，而不必死记。

#### 4.1.5 小练习与答案

**练习 1**：`HcclReduceScatter` 和 `HcclAllGather` 在「结果分布」上有什么本质区别？

**答案**：ReduceScatter 是「归约 + 分散」——每个 rank 最终只拿到**完整归约结果中的一段**；AllGather 是「收集」——每个 rank 最终都拿到**所有 rank 数据的完整拼接**。一个把结果切碎分给每人一段，一个把所有人的碎片凑齐发给每人一份。

**练习 2**：为什么说 `HcclAllReduce` 在效果上可以由「一次 ReduceScatter + 一次 AllGather」组合而成？

**答案**：AllReduce 要求每个 rank 拿到完整的归约结果。先做 ReduceScatter 让每个 rank 拿到归约结果的第 i 段（这一步完成了归约，只是结果分散了）；再做 AllGather 把这些段收集拼接，于是每个 rank 都拿到完整归约结果。这正是 HCCL 分级通信实现 AllReduce 的基本思路（见 4.4）。

### 4.2 通信模型与 RankGraph 拓扑

#### 4.2.1 概念说明

知道了 rank 和通信域，下一个问题是：**这些 rank 在物理上是怎么连起来的？** 带宽、时延都取决于物理连接。HCCL 用一套叫 **RankGraph** 的图模型来描述它。

`architecture-brief.md` 第 2.1 节给出三个核心概念：

| 术语 | 一句话解释 | 主要对应硬件 |
| --- | --- | --- |
| 集合通信域 | 通信执行的上下文，管理参与的实体和资源 | 多个 NPU 组成 |
| Rank | 通信域中的成员，有唯一 Rank ID（从 0 开始） | 一个 NPU |
| RankGraph | rank 间的通信关系图，描述「谁和谁怎么连」 | 网络拓扑 |

RankGraph 进一步引入了一组图论概念，用来精确描述连接关系。初学者不需要一次全记住，但下面这张「类比表」能帮你建立直觉：

| 概念 | 一句话解释 | 类比 |
| --- | --- | --- |
| Node | 图中节点，分通信实体和 Fabric（交换抽象） | 通信实体＝带网口的 NPU；Fabric＝交换机组 |
| Endpoint | Node 的通信设备（逻辑），一个 Endpoint 映射一个物理端口 | NPU 上的网卡口 |
| Edge | Node 间的连接关系，两端是 Endpoint | 网线 |
| Link | 从 Edge 提取的可建链信息（含两端 Endpoint + 协议） | 两台 NPU 间可建链的路径描述 |
| netLayer | 拓扑层级，通信质量逐层增加而递减 | Server 内（Layer0）最快，跨 Server（Layer1）较慢 |
| Fabric | 对交换/路由组的抽象，连上的实体两两互通 | 一台交换机 |
| TopoInstance | 每层内的拓扑实例 | 同机房 8 卡组成的一个 1DMesh 实例 |

这里最关键的是 **netLayer（拓扑层级）**：集群天然分层，Server 内直连最快，跨 Server 经交换机较慢。这个「分层」就是后面分级通信和三级算法（level0/level1/level2）的物理依据。

#### 4.2.2 核心流程

RankGraph 模型有一条清晰的「从连接到通信」的递进链：

```
Edge  （谁和谁连）        —— 物理网线
   ↓ 提取可建链信息
Link  （怎么建链）        —— 含两端 Endpoint + 协议
   ↓ 实例化
Channel（怎么通信）       —— 真正可用的数据通道
```

> 这条递进关系在 `architecture-brief.md` 原文里写得很清楚：见本节 4.2.3 引用的第 2.2 节「递进关系」要点。

#### 4.2.3 源码精读

三个核心概念的权威定义：

> [docs/zh/architecture/architecture-brief.md:45-47](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/architecture/architecture-brief.md#L45-L47) 定义集合通信域、Rank、RankGraph 及其对应硬件。

RankGraph 的拓扑模型与图论概念，连同那条关键的递进关系，都在第 2.2 节：

> [docs/zh/architecture/architecture-brief.md:53-70](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/architecture/architecture-brief.md#L53-L70) 给出 Node / Endpoint / Edge / Link / netLayer / Fabric / TopoInstance 的定义，并在末尾点明「Edge 描述谁和谁连 → Link 描述怎么建链 → Channel 描述怎么通信」。

其中关于 **netLayer** 层级，原文特别强调：

> [docs/zh/architecture/architecture-brief.md:63](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/architecture/architecture-brief.md#L63) 说明：Server 内为 Layer0（如 HCCS 直连），Server 间为 Layer1（如 RoCE 经交换机），通信质量逐层增加而递减。

把拓扑概念往下延伸一层，就是「基础通信」的四个原语概念（Endpoint / Channel / CommMem / CommEngine），它们是 4.3 节的主角，这里先引用定义：

> [docs/zh/architecture/architecture-brief.md:80-85](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/architecture/architecture-brief.md#L80-L85) 定义通信设备(Endpoint)、通信通道(Channel)、通信内存(CommMem)、通信引擎(CommEngine)，并给出组合公式 `Channel = 两端通信设备 + 通信协议 + N Notifys`。

#### 4.2.4 代码实践（图示型）

**实践目标**：用 RankGraph 的术语描述一个真实的小集群拓扑。

**操作步骤**：

1. 假设有这样一个集群：**2 台 Server，每台 4 张 NPU**，Server 内 4 卡通过 HCCS 两两直连（Fullmesh），Server 之间通过一台 RoCE 交换机互联。
2. 在纸上画出这 8 个 rank（Node），标出：
   - 哪些 Edge 属于 **netLayer0**（Server 内 HCCS 直连）。
   - 哪些 Edge 经过 **Fabric**（那台 RoCE 交换机），属于 **netLayer1**。
   - 每台 Server 内的 4 卡构成一个 **TopoInstance**（Fullmesh 实例）。
3. 用 RankGraph 术语写一句话描述：「rank0 到 rank4 的通信需要跨 netLayer，经 Fabric 中转」。

**需要观察的现象**：同一个通信域里，rank 间的连接质量并不均等——Server 内的链路（Layer0）又快又宽，跨 Server 的链路（Layer1）较慢。

**预期结果**：你能用 netLayer / Fabric / TopoInstance 这几个词描述清楚「为什么 HCCL 要分级通信」——因为把大数据量的搬移放在又快又宽的 Layer0 上更划算。这就是 4.4 节分级通信的动机。

#### 4.2.5 小练习与答案

**练习 1**：Edge、Link、Channel 三者的递进关系是什么？

**答案**：Edge 描述「谁和谁连」（物理连接关系，两端是 Endpoint）；Link 在 Edge 基础上提取「怎么建链」（含两端 Endpoint 和协议）；Channel 在 Link 基础上实例化，描述「怎么通信」，是真正可用的数据通道。

**练习 2**：为什么 netLayer 的通信质量是「逐层增加而递减」？

**答案**：因为层级越深，通常经过的交换设备越多、物理距离越远。Server 内（Layer0）多为直连高速互联（如 HCCS），带宽高时延低；跨 Server（Layer1）要经过交换机（如 RoCE），带宽收敛、时延更高。所以把通信尽量放在低层级（快）的链路上更高效。

### 4.3 通信引擎与同步机制

#### 4.3.1 概念说明

拓扑描述了「能怎么连」，但**真正搬数据的执行者**是**通信引擎（CommEngine）**。`architecture-brief.md` 把它定义为「执行通信任务的核心模块」：向上接收通信资源（Endpoint/Channel/CommMem）和任务编排下发的任务，向下通过线程调度器驱动通信硬件搬移数据。

通信引擎内部有两个关键抽象：

- **Thread（线程）**：通信任务的执行上下文，承载一串数据面算子（LocalReduce、ChannelRead/Write、Notify 等）。一个引擎可以含多个 Thread 并发执行。
- **线程执行调度器**：把 Thread 上的算子调度到硬件执行，例如 TS（Task Scheduler）。

> 一句话：**通信引擎 = Thread（执行上下文）+ 线程调度器（调度执行）**。

HCCL 支持四种通信引擎，初学者先抓住三种主流的：

| 通信引擎 | Thread 抽象 | 特点 | 适用场景 |
| --- | --- | --- | --- |
| AICPU_TS | NPU Stream | 不占计算核，用 Task 描述符下发 | 大数据量通信 |
| AIV | AICore Block | 低延迟，但**占 Vector 核** | 小数据低延迟 |
| CCU | Mission | 硬化加速单元、微码执行，高带宽低时延且少占核/带宽 | 专用硬件通信（受片上资源限制，通信域数量有限） |

通信引擎和通信算子/算法是「**正交**」的两个维度：算法决定数据怎么流动（Ring 还是 RHD），引擎决定数据由谁搬（AICPU 还是 AIV 还是 CCU）。这理解了，后面 u5 系列讲三类引擎模板时就不会混淆。

**同步机制**：通信里有两类同步场景——

- **ThreadNotify**：同一通信实体内，Thread 之间发/等同步信号。
- **ChannelNotify**：不同通信实体之间，通过 Channel 上的 notify 协调。

#### 4.3.2 核心流程

以 AICPU_TS 引擎为例，它走「Task 描述符下发」模式，四步流程：

```
1. Host 提交 AICPU Kernel 至任务队列
2. TS 调度器把 AICPU Kernel 分发到 AICPU 执行
3. AICPU 提交通信 Task 描述符至 TS 队列
4. TS 调度器把通信 Task 分发到执行器（真正搬数据）
```

CCU 引擎则走「专用加速单元执行」模式：Host 下发 CCU 指令序列 → CCU Kernel 被调度 → CCU 执行指令流并用 URMA（统一远端内存访问）搬数据。

#### 4.3.3 源码精读

通信引擎的定义与组成，以及「引擎 = Thread + 调度器」的概括：

> [docs/zh/architecture/architecture-brief.md:106-117](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/architecture/architecture-brief.md#L106-L117) 定义 Thread、线程执行调度器、通信硬件、线程间同步，并总结 AICPU_TS 引擎由「AICPU 运行通信 Kernel + TS 调度 Task」协同完成。

四种引擎的对比表与「同一通信域默认只用一种引擎、由算法选择器自动选择」的关键说明：

> [docs/zh/architecture/architecture-brief.md:121-128](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/architecture/architecture-brief.md#L121-L128) 给出 AICPU_TS / CPU_TS / AIV / CCU 的 Thread 抽象、特点与适用场景，并指出「算子开发者通过算法选择器自动选择引擎」。

AICPU_TS 的四步调度流程：

> [docs/zh/architecture/architecture-brief.md:132-143](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/architecture/architecture-brief.md#L132-L143) 描述 AICPU_TS 的 Task 描述符下发模式，要点是「不占计算核，适合大数据高带宽场景」。

CCU 的三步执行流程：

> [docs/zh/architecture/architecture-brief.md:145-157](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/architecture/architecture-brief.md#L145-L157) 描述 CCU 作为 IO Die 专用协处理器、以 Mission 为 Thread 抽象、经 URMA 搬数据，要点是「高带宽低时延但受片上资源限制」。

AIV 与同步机制：

> [docs/zh/architecture/architecture-brief.md:159-169](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/architecture/architecture-brief.md#L159-L169) 描述 AIV 的 Vector Core 执行模式，要点是「低延迟但占 Vector 核，适合小数据」。
>
> [docs/zh/architecture/architecture-brief.md:173-182](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/architecture/architecture-brief.md#L173-L182) 给出 ThreadNotify（同实体内）与 ChannelNotify（跨实体）两类同步。

#### 4.3.4 代码实践（分析型）

**实践目标**：根据场景判断该选哪种通信引擎，建立「引擎选择」的直觉（为后续 u2-l4 的 `ShouldGoCcuFastLaunch` / `HcclAivCacheCheckAndReplay` 做铺垫）。

**操作步骤**：

1. 阅读四种引擎的对比（[architecture-brief.md:121-128](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/architecture/architecture-brief.md#L121-L128)）。
2. 针对下面三个场景，分别给出你倾向的引擎，并说明理由：
   - 场景 A：AllReduce 同步几十 MB 的梯度（大数据量）。
   - 场景 B：每层权重 AllReduce，但每张卡只同步几 KB 的少量 embedding（小数据量、对时延敏感）。
   - 场景 C：硬件支持 CCU，且通信域规模在其能力范围内，追求极致带宽与时延。

**需要观察的现象**：引擎选择本质上是在「是否占计算核 / 时延 / 带宽 / 资源受限」之间权衡。

**预期结果**：场景 A 倾向 AICPU_TS（不占核、适合大数据）；场景 B 倾向 AIV（低延迟，小数据下占 Vector 核的代价可接受）；场景 C 倾向 CCU（硬化加速）。这正是 HCCL 源码里 `HcclGetOpExpansionMode` 设定 `param.engine` 的决策依据（详见 u2-l4）。

#### 4.3.5 小练习与答案

**练习 1**：AICPU_TS 引擎「不占计算核」是什么意思？为什么还适合大数据量？

**答案**：AICPU_TS 由 AICPU（NPU 上独立于 AI Core 计算单元的部分）运行通信 Kernel，通过 TS 下发 Task 描述符来驱动通信硬件搬数据，不挤占 AI Core 的矩阵/向量计算核。大数据量通信受限于带宽而非时延，AICPU_TS 能充分利用带宽且不抢计算资源，所以适合大数据高带宽场景。

**练习 2**：ThreadNotify 和 ChannelNotify 各自用于什么场景？

**答案**：ThreadNotify 用于**同一通信实体内**不同 Thread 之间的同步（Thread 向本实体内另一个 Thread 发/等信号）；ChannelNotify 用于**不同通信实体之间**的同步（通过 Channel 上的 notify 与数据通道向远端实体的 Thread 发/等信号）。

### 4.4 通信算法、α-β 模型与分级通信原理

#### 4.4.1 概念说明

回到第 4.1.1 节的第四个概念——**通信算法**。同一个算子（比如 AllReduce）在不同拓扑、不同数据量、不同硬件资源下，会采用不同的算法实现。HCCL 提供的主要拓扑算法有：

**Server 内通信算法**（根据硬件拓扑自动选择，用户不可配置）：Mesh、Ring、Double-Ring、Star。

**Server 间 / 超节点间通信算法**（自适应选择，可通过 `HCCL_ALGO` 环境变量覆盖）：

| 算法 | 通信步数复杂度 | 特点 | 适合场景 |
| --- | --- | --- | --- |
| **Ring**（环） | 线性 O(p) | 步数多、时延较高，但关系简单、抗拥塞 | Server 数少、数据量小、网络拥塞明显 |
| **RHD**（递归二分倍增） | 对数 O(log p) | 步数少、时延低，但非 2 次幂规模有额外通信量 | Server 数为 2 的整数次幂 |
| **NHR**（非均衡层次环） | 对数 O(log p) | 步数少、时延低 | Server 数较多 |
| **NB**（非均匀 Bruck） | 对数 O(log p) | 步数少、时延低 | Server 数较多 |
| **Pipeline**（流水线） | —— | 并发使用节点内/节点间链路 | 数据量大、每机多卡 |
| **Pairwise**（逐对） | 线性 O(p) | 避免「一打多」 | 仅 AlltoAll 系列，需规避一打多 |
| **AHC**（非对称层次拼接） | —— | 适配多层/非对称分布 | ReduceScatter/AllGather/AllReduce，层次间带宽收敛时收益好 |

**分级通信原理**：因为集群天然分层（netLayer0 节点内快、netLayer1 节点间慢），HCCL 把一次集合通信拆成多级子通信，**把大数据量搬移尽量放在又快又宽的低层级链路上**，使任务编排与拓扑亲和，最大化链路利用率。这就是为什么 AllReduce 会被拆成「节点内 ReduceScatter → 节点间 AllReduce → 节点内 AllGather」三段。

#### 4.4.2 核心流程

**α-β 耗时模型**：HCCL 用 Hockney 的 α-β 模型评估算法耗时。记 α 为节点间固定时延、β 为每字节传输耗时、γ 为每字节归约计算耗时、n 为通信数据量、p 为通信域节点数。则单步传输并归约 n 字节的耗时为：

\[
D = \alpha + n\beta + n\gamma
\]

算法优化的目标就是：减少通信步数（降 α 的总贡献）、减少实际通信数据量（降 β、γ 的总贡献）。

据此可以比较 Ring 与 RHD 的渐近开销。设数据总量为 n、节点数为 p：

- **Ring**：ReduceScatter 与 AllGather 各走 \(p-1\) 步，每步传 \(n/p\)。总步数线性于 p，总耗时渐近：
  \[
  T_{\text{Ring}} \approx 2(p-1)\left(\alpha + \frac{n}{p}\beta + \frac{n}{p}\gamma\right) \approx 2p\alpha + 2n(\beta+\gamma)
  \]
  步数多（\(O(p)\)），所以 α 时延被放大 \(p\) 倍；但通信量恒为 \(2n\)（不含拥塞），关系简单、抗拥塞。

- **RHD**：每步传 \(n/p\)，步数为 \(\lceil\log_2 p\rceil\)。总耗时渐近：
  \[
  T_{\text{RHD}} \approx 2\lceil\log_2 p\rceil\left(\alpha + \frac{n}{p}\beta + \frac{n}{p}\gamma\right)
  \]
  步数少（\(O(\log p)\)），时延项 α 只被放大 \(\log_2 p\) 倍，所以时延低；但当 p 不是 2 的整数次幂时，会引入额外通信量。

这与文档「Ring 通信步数多（线性复杂度）」「RHD 通信步数少（对数复杂度）」的描述完全一致。

**AllReduce 的三级分级流程**（这是本讲核心实践要画的过程）：

```
输入：每个 rank 持有完整长度 n 的梯度张量

阶段一  节点内 ReduceScatter（走又快又宽的 netLayer0，如 HCCS）
   → 每台 Server 内的各 rank 协作归约，结果分散：每个 rank 拿到一段归约结果

阶段二  节点间 AllReduce（走较慢的 netLayer1，如 RoCE）
   → 各 Server 之间对「这一段」做完整 AllReduce，让所有 rank 的该段都收敛到一致

阶段三  节点内 AllGather（再次走 netLayer0）
   → 节点内收集各段，每个 rank 最终拿到完整归约结果
```

关键洞察：阶段二虽然走慢链路，但传输的只是「一段」而非「全量」，数据量被 ReduceScatter 缩小了，所以分级反而更快。

#### 4.4.3 源码精读

算法总览与「同算子多算法」的动机：

> [docs/zh/user_guide/coll_algo_intro/algo_intro.md:3](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/user_guide/coll_algo_intro/algo_intro.md#L3) 说明：针对同一通信算子，随网络拓扑、数据量、硬件资源不同会采用不同算法，HCCL 提供 Mesh、Ring、RHD、Pairwise、Pipeline 等拓扑算法。

Server 内算法（不可配置）与 Server 间算法清单及各自适用场景：

> [docs/zh/user_guide/coll_algo_intro/algo_intro.md:5-7](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/user_guide/coll_algo_intro/algo_intro.md#L5-L7) 指出 Server 内支持 Mesh/Ring/Double-Ring/Star，根据硬件拓扑自动选择、用户无须配置。
>
> [docs/zh/user_guide/coll_algo_intro/algo_intro.md:13-19](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/user_guide/coll_algo_intro/algo_intro.md#L13-L19) 给出 Ring / RHD / NHR / NB / Pipeline / Pairwise / AHC 各自的特点与适用场景。

α-β 耗时模型：

> [docs/zh/user_guide/coll_algo_intro/algo_intro.md:32-44](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/user_guide/coll_algo_intro/algo_intro.md#L32-L44) 定义 α/β/γ/n/p 并给出单步耗时 \(D = \alpha + n\beta + n\gamma\)，说明算法优化的本质是「减少通信步数与实际通信数据量」。

分级通信原理与各算子的分级表（**重点**）：

> [docs/zh/user_guide/coll_algo_intro/algo_intro.md:46-64](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/user_guide/coll_algo_intro/algo_intro.md#L46-L64) 说明 HCCL 按节点内/节点间分两级或加超节点间分三级执行集合通信，并给出 Atlas A2 各算子的分级表。其中 AllReduce 的三段是「Server 内 ReduceScatter → Server 间 AllReduce → Server 内 AllGather」。

AllReduce 分级流程的图解与文字说明：

> [docs/zh/user_guide/coll_algo_intro/hierarchical_comm_principle.md:19-24](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/user_guide/coll_algo_intro/hierarchical_comm_principle.md#L19-L24) 解释：AllReduce 虽拆解为 ReduceScatter + AllGather 两阶段，但不严格遵循二者语义，可以把大数据量通信放在带宽更高的 Server 内——先 Server 内 ReduceScatter，再 Server 间 AllReduce，最后 Server 内 AllGather。配套流程图见 `figures/allreduce_hierarchical_flow.png`。

对比 ReduceScatter 与 AllGather 的分级顺序，能加深理解：

> [docs/zh/user_guide/coll_algo_intro/hierarchical_comm_principle.md:5-10](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/user_guide/coll_algo_intro/hierarchical_comm_principle.md#L5-L10) ReduceScatter 为保证 Server 间数据块连续，先 Server 间、后 Server 内。
>
> [docs/zh/user_guide/coll_algo_intro/hierarchical_comm_principle.md:12-17](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/user_guide/coll_algo_intro/hierarchical_comm_principle.md#L12-L17) AllGather 同理，先 Server 内、后 Server 间。

#### 4.4.4 代码实践（图示型 · 本讲主实践）

**实践目标**：亲手画出 AllReduce 在节点内/节点间两级拓扑下的三段分级过程，并据此说明 Ring 与 RHD 的适用场景差异。

**操作步骤**：

1. 设定一个最小例子：**2 台 Server，每台 2 张 NPU**，共 4 个 rank（rank0~rank3）。每 rank 持有一个长度为 4 的向量，要做 AllReduce（SUM）。例如：
   - rank0: `[1,2,3,4]`，rank1: `[1,1,1,1]`，rank2: `[2,2,2,2]`，rank3: `[0,1,0,1]`
   - 正确结果（逐元素 SUM）应为 `[4,6,6,8]`。
2. 把每个 rank 的向量按 4 段切分（每段 1 个元素），分别属于 rank0/rank1/rank2/rank3 的「那一份」。
3. 在纸上按 HCCL 的分级流程画三张子图：
   - **阶段一 Server 内 ReduceScatter**：Server0（rank0,rank1）内部归约、Server1（rank2,rank3）内部归约，各 rank 得到某一段的部分归约结果。
   - **阶段二 Server 间 AllReduce**：Server0 与 Server1 之间对这一段做 AllReduce，使该段在所有 4 个 rank 间收敛到最终归约值。
   - **阶段三 Server 内 AllGather**：Server 内收集，所有 rank 拿到完整的 `[4,6,6,8]`。
4. 在阶段二上标注「这里走较慢的 netLayer1（RoCE）」，并圈出「传的是一段，不是全量」。
5. 完成图示后，回答两个问题：
   - 如果 Server 间用 **Ring** 算法，步数复杂度是多少？什么时候它比 RHD 更合适？
   - 如果 Server 间用 **RHD** 算法，p=2 时步数是多少？它为什么时延更低？

**需要观察的现象**：阶段二（慢链路）上传输的数据量被阶段一的 ReduceScatter 压缩到了 1/p，所以即便走慢链路，整体也比「不分级的全量 AllReduce」更快。

**预期结果**：

- 阶段二用 Ring：步数为 \(p-1\)（线性），抗拥塞、关系简单，适合 Server 数少/数据量小/网络拥塞明显的场景（见 [algo_intro.md:13](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/user_guide/coll_algo_intro/algo_intro.md#L13)）。
- 阶段二用 RHD：p=2 时步数为 \(\lceil\log_2 2\rceil = 1\) 步，时延项 α 只放大 1 倍，所以时延低；适合 Server 数为 2 的整数次幂的场景（见 [algo_intro.md:14](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/user_guide/coll_algo_intro/algo_intro.md#L14)）。

> 说明：本实践是「纸笔推导型」，不依赖真实硬件。若要在真机上观察分级行为，需上板运行（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 AllReduce 的分级顺序是「节点内 ReduceScatter → 节点间 AllReduce → 节点内 AllGather」，而不是「节点间 ReduceScatter → 节点内 AllReduce → 节点间 AllGather」？

**答案**：因为 AllReduce 的最终输出是完整归约结果，不要求严格遵循 ReduceScatter/AllGather 的语义，所以可以把「大数据量的通信」放到带宽更高的节点内链路（netLayer0）。先在节点内做 ReduceScatter 完成大部分归约并把数据量缩小到 1/p，再让节点间只传这一小段做 AllReduce，最后节点内 AllGather 拼回全量。这样慢链路上传输的数据量最小，整体更快。（见 [hierarchical_comm_principle.md:19-24](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/user_guide/coll_algo_intro/hierarchical_comm_principle.md#L19-L24)）

**练习 2**：假设 Server 数 p=5（不是 2 的整数次幂），节点间选 Ring 还是 RHD 更好？为什么？

**答案**：这是一个权衡。RHD 步数少（\(\lceil\log_2 5\rceil=3\) 步）时延低，但非 2 次幂规模会引入额外通信量；Ring 步数多（4 步）但关系简单、无额外通信量、抗拥塞。若数据量较小，RHD 的额外通信量代价不大、时延优势明显，倾向 RHD；若网络存在明显拥塞或数据量小到时延占主导，Ring 也可能更稳。文档给出的规则正是「Server 数不是 2 的整数次幂但通信数据量较小」时可用 RHD（[algo_intro.md:14](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/user_guide/coll_algo_intro/algo_intro.md#L14)）。实际由 HCCL 自适应选择，也可用 `HCCL_ALGO` 手动指定。

**练习 3**：Pipeline 算法和 Ring/RHD 的核心区别是什么？

**答案**：Pipeline（流水线）的特点是**并发使用节点内与节点间链路**（或超节点内与超节点间链路），让通信在两条链路上重叠进行以提高利用率；而 Ring/RHD 主要描述单层内的数据流动结构。Pipeline 适合数据量大、每机多卡的场景（[algo_intro.md:17](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/user_guide/coll_algo_intro/algo_intro.md#L17)）。

## 5. 综合实践

把本讲的四个模块串起来，完成下面这个「端到端概念推理」小任务。

**场景**：你要在一个 **4 台 Server、每台 8 张 NPU（共 32 rank）** 的集群上，用数据并行训练大模型，每个训练步对梯度做一次 AllReduce（FP32，约几百 MB）。

**任务**：

1. **拓扑建模**（用 4.2 的术语）：画出 4 台 Server、32 个 rank 的 RankGraph 草图。标出 netLayer0（Server 内 8 卡 HCCS 直连）、netLayer1（Server 间 RoCE 经交换机），并指出每台 Server 内的 8 卡构成一个 TopoInstance。

2. **引擎选择**（用 4.3 的术语）：对几百 MB 的梯度 AllReduce，你会优先考虑哪种通信引擎？为什么？（提示：AICPU_TS 适合大数据、不占计算核）

3. **算法与分级**（用 4.4 的术语）：
   - 写出这次 AllReduce 的三段分级流程（节点内 ReduceScatter → 节点间 AllReduce → 节点内 AllGather）。
   - 节点间（阶段二）在 p=4 台 Server 下，对比 Ring（3 步）与 RHD（\(\lceil\log_2 4\rceil=2\) 步），结合 α-β 模型说明哪个时延更低，并指出 RHD 在这里是否满足「2 的整数次幂」条件。

4. **回扣源码**：用一句话说明上述「算法选择」这一步，对应到 HCCL 源码里是哪个组件的职责。（答案：算法选择器 Selector，详见后续 [u3-l2 算法选择器 Selector](u3-l2-selector.md)）

**预期产出**：一张拓扑草图 + 一段引擎/算法选择的推理说明。完成后，你就把「概念 → 拓扑 → 引擎 → 算法 → 分级流程」整条链路打通了，具备了阅读 HCCL 算子源码的领域基础。

## 6. 本讲小结

- **四个基本概念**：rank（通信成员）、通信域（rank 组合）、通信算子（如 AllReduce）、通信算法（如 Ring）。集合通信是「一组进程协同参与」，区别于点对点的 Send/Recv。
- **RankGraph 拓扑模型**用 Node/Endpoint/Edge/Link/netLayer/Fabric/TopoInstance 描述集群网络，核心是 **netLayer 分层**：Server 内（Layer0）快、Server 间（Layer1）慢，这是分级通信的物理依据。
- **通信引擎**是真正搬数据的执行者，等于 Thread（执行上下文）+ 线程调度器；主流三种是 AICPU_TS（不占核、大数据）、AIV（低延迟但占 Vector 核、小数据）、CCU（硬化加速）。引擎与算法是正交的两个维度。
- **通信算法**按复杂度分：Ring 线性 \(O(p)\) 抗拥塞、RHD 对数 \(O(\log p)\) 低时延、NHR/NB 适合节点多、Pipeline 并发用多级链路。
- **α-β 模型** \(D = \alpha + n\beta + n\gamma\) 是评估算法耗时的统一框架，算法优化的本质是减少步数与通信量。
- **分级通信**把一次通信拆成多级子通信，AllReduce = 节点内 ReduceScatter → 节点间 AllReduce → 节点内 AllGather，把大数据量搬移放在又快又宽的低层级链路上。

## 7. 下一步学习建议

本讲建立的是「领域语言」。接下来可以：

1. **进入主链路**：阅读 [u2-l1 对外 API 与通信算子接口](u2-l1-public-api-surface.md)，系统梳理 `hccl.h` 的全部算子，把本讲的「通信原语」概念扩展到完整接口面。
2. **看算法选择落地**：阅读 [u3-l2 算法选择器 Selector](u3-l2-selector.md)，看 HCCL 源码是如何根据引擎和数据量自动选出 `AicpuAllReduceSoleNHR` 这类 algName 的——本讲讲的 Ring/RHD/NHR 就是这里的候选项。
3. **深入引擎实现**：若对 AICPU/AIV/CCU 感兴趣，可跳到 Unit 5 的 [u5-l1 AICPU 模板与 Kernel 下发](u5-l1-aicpu-template-kernel.md) 等讲义，看引擎是如何用模板和 Kernel 下发把数据真正搬出去的。
4. **配置算法**：想动手指定 Server 间算法，可先读环境变量文档 [HCCL_ALGO.md](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/user_guide/hccl_env/HCCL_ALGO.md)，再到 [u4-l3 环境变量与算法配置系统](u4-l3-env-config.md) 看它的解析源码。

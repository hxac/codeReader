# HCCL 项目定位与 CANN 软件栈

## 1. 本讲目标

本讲是整本《HCCL 源码学习手册》的第一讲，目标不写一行复杂代码，而是先把「地图」画清楚。读完本讲，你应当能够：

- 用一句话说清 **HCCL 是什么**、它为谁服务、它的核心能力有哪些。
- 讲明白 **HCCL 算子库（本仓 `cann/hccl`）** 与 **HCOMM 通信基础库（`cann/hcomm`）** 各自负责什么，以及它们为什么被拆成两个仓库。
- 描述 HCCL 在 **CANN 软件栈** 中「承上启下」的位置，并理解支撑这一架构的四条硬约束。
- 识别本项目的入口资料：`README.md`、`AGENTS.md`、`docs/zh/architecture/architecture-brief.md`，知道以后遇到疑问该去哪里查权威答案。

> 这一讲主打「建立全局认知」。具体算子怎么实现、算法怎么选、内核怎么下发，会在后续讲义中逐步展开。

## 2. 前置知识

本讲几乎不需要你懂 C++ 或分布式系统。但有几个名词会反复出现，先建立直觉即可，后面讲义会再展开。

| 名词 | 一句话理解 | 现在只需知道 |
|------|-----------|-------------|
| **NPU**（昇腾 AI 处理器） | 华为的 AI 加速芯片，类似 GPU 的角色 | HCCL 让多块 NPU 协同工作 |
| **CANN**（Compute Architecture for Neural Networks） | 昇腾的软件栈总称，包含驱动、运行时、算子库等 | HCCL 是 CANN 的一个组件 |
| **集合通信（Collective Communication）** | 让一组设备「一起」交换数据的通信方式 | 例如 AllReduce 把每张卡上的梯度加到一起 |
| **通信域（Communicator）** | 参与某次通信的一组成员的上下文 | 类似一个「群聊」，群里每个成员叫一个 Rank |
| **dlsym** | C/C++ 在运行时通过函数名查找并调用动态库符号的机制 | HCCL 用它在运行时才去调用 HCOMM，而不是编译期绑定 |

如果你对「集合通信到底在干什么」还想再补一点背景，本手册的下一讲（u1-l2）会专门讲 rank、通信域、AllReduce/AllGather 等概念。本讲先把仓库和架构关系理清。

## 3. 本讲源码地图

本讲引用的「源码」其实是三份**权威文档/入口文件**——它们是理解整个工程的最佳起点。后续所有讲义都建议把它们当作查证事实的首选。

| 文件 | 作用 | 本讲用到哪部分 |
|------|------|---------------|
| `README.md` | 项目门面：概述、核心能力、目录结构、快速开始、学习教程入口 | 概述与核心能力、HCCL/HCOMM 组成 |
| `AGENTS.md` | 面向 AI Agent 与贡献者的治理主入口：仓库定位、目录结构、**架构约束（硬性）**、构建测试、编码规范 | §1 仓库定位、§3 架构约束 |
| `docs/zh/architecture/architecture-brief.md` | **架构权威来源**：集合通信模型、通信引擎、软件分层逻辑、架构约束说明 | §3 软件分层逻辑与架构约束 |

> 提示：`AGENTS.md` 第 3 节明确写着「架构权威来源」就是 `architecture-brief.md`。当两份文档对同一事实表述不同时，以 `architecture-brief.md` 为准。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，对应规格中的三个最小模块：

- **4.1 HCCL 是什么：定位与核心能力**（对应 README）
- **4.2 HCCL 与 HCOMM 的分层职责与 dlsym 解耦**（对应 AGENTS.md §1）
- **4.3 CANN 软件分层逻辑与架构约束**（对应 architecture-brief §3）

### 4.1 HCCL 是什么：定位与核心能力

#### 4.1.1 概念说明

**HCCL** 的全称是 **Huawei Collective Communication Library**（华为集合通信库），是基于昇腾 AI 处理器的高性能集合通信库。

为什么要单独有这样一个库？因为**大模型训练和推理需要多卡协同，而通信往往是扩展的瓶颈**。举个最直观的例子：数据并行训练里，每张 NPU 处理不同的样本，算完梯度后必须用一次 **AllReduce** 把所有卡的梯度同步求和，才能更新权重。如果这一步慢，再多卡也白搭。HCCL 的任务就是让这种「一群卡之间高效、有序地交换数据」变得又快又可靠。

它的定位用一句话概括：**介于 AI 框架与硬件驱动之间，承上启下**——对上支持 PyTorch、MindSpore 等 AI 框架，对下使能多款昇腾 NPU 之间的通信。

#### 4.1.2 核心流程（核心能力一览）

HCCL 的核心能力可以从六个维度去看。下面的表格把「能力维度」和「具体能力」对应起来，这正是 README 与架构简介里反复强调的能力清单：

| 维度 | 能力 |
|------|------|
| 集合通信原语 | AllReduce、Broadcast、AllGather、ReduceScatter、AlltoAllv、Send、Receive 等 |
| 通信算法 | Ring、Mesh、RHD（Halving-Doubling）、Star + 自研算法 |
| 通信协议 | UBC、UBG、UBoE、RoCE(v2)、HCCS、UB_MEM |
| 执行模式 | 单算子模式 + 图模式 |
| 扩展能力 | 通信算子自定义开发（MC2 框架） |
| 应用场景 | 大模型训练（数据/模型/专家并行）与推理（TP/PP/EP）的集合通信 |

这里出现了很多缩写（Ring、Mesh、RHD、AICPU、AIV、CCU 等），现在不必全部记住。你只需要建立一个印象：**HCCL 不是只有一种「打电话的方式」**，而是针对不同数据量、不同拓扑、不同硬件，提供了多种算法和多种通信引擎，由框架/算子去选择最合适的一种。这些会在 u1-l2（算法概念）和 Unit 5（通信引擎）中深入。

#### 4.1.3 源码精读

先看 README 对 HCCL 的一段权威定义与核心功能列举：

[README.md:L10-L22](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/README.md#L10-L22) —— 这段是 HCCL 的官方概述：说明 HCCL 是「基于昇腾 AI 处理器的高性能集合通信库」，并罗列了五条核心功能（单机/多机通信、多种原语、多种算法、多种链路、单算子与图模式），最后点出它「对上支持 AI 框架、对下使能 NPU」的承上启下定位。

把这段和架构简介里的能力一览表对照看会更清楚：

[architecture-brief.md:L21-L33](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/architecture/architecture-brief.md#L21-L33) —— 这是架构简介 §1.2「HCCL 核心能力一览」，用一张表把能力按六个维度组织（原语/算法/协议/执行模式/扩展/场景），并配图说明 HCCL 在 CANN 栈中「介于 AI 框架与硬件驱动之间」的位置。这张表比 README 更结构化，是后面讲义反复引用的能力清单。

> 注意：README 把执行模式描述为「单算子和图模式两种」；架构简介在此基础上还补充了「通信算子自定义开发」作为扩展能力。两者一致，只是详略不同。

#### 4.1.4 代码实践（源码阅读型）

> **实践目标**：建立「能力维度」的直觉，避免把 HCCL 想成「只有一个 AllReduce」。

操作步骤：

1. 打开 [architecture-brief.md:L21-L33](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/architecture/architecture-brief.md#L21-L33)，对照六行表格。
2. 在笔记里画一张「六行两列」的小表，左列写维度，右列只挑**一个**你最眼熟的词抄下来（例如「集合通信原语 → AllReduce」）。
3. 尝试用一句自己的话回答：**「通信算法（Ring/Mesh/RHD）」和「通信引擎（AICPU/AIV/CCU）」是不是一回事？**

需要观察的现象 / 预期结果：

- 你会发现「算法」回答的是**数据怎么流转**（Ring 是围成一圈传），「引擎」回答的是**谁来搬运数据**（AICPU 是让 NPU 上的控制核去搬）。两者是正交的概念，后续讲义会分别展开。如果你现在分不清，很正常，把这条疑问记下来即可——它正是 u1-l2 和 Unit 5 要解决的。

> 本实践为「源码阅读型」，无需运行命令，也不需要 NPU 环境。

#### 4.1.5 小练习与答案

**练习 1**：README 列出的核心功能里，哪一条说明 HCCL 同时支持两种「调用形态」？

> **参考答案**：「支持单算子和图模式两种执行模式」。单算子模式是框架直接调用一次 `HcclAllReduce` 这样的接口；图模式是把通信算子编进计算图里由图引擎统一调度（详见 u7-l2）。

**练习 2**：HCCL 支持哪些通信链路（高速互联）？至少说出两种。

> **参考答案**：README 明确列出 HCCS、RoCE、PCIe 等；架构简介的能力表还提到 UBC、UBG、UBoE、UB_MEM 等协议。能说出 HCCS（节点内高速互联）和 RoCE（节点间网络）即可。

### 4.2 HCCL 与 HCOMM 的分层职责与 dlsym 解耦

#### 4.2.1 概念说明

这是本讲最重要、也是最容易让初学者迷惑的一点：**HCCL 其实是两个仓库拼起来的**。

官方的「软件组成」定义是：

> **HCCL = HCCL 集合通信算子库（本仓 `cann/hccl`）+ HCOMM 通信基础库（`cann/hcomm`）**

也就是说，你正在阅读的 `cann/hccl` 仓库，只负责「**算子**」这一层——比如 `HcclAllReduce` 的入口、参数校验、算法选择、算法执行编排；而真正「**怎么把数据从一张卡搬到另一张卡**」「**通信域怎么管理**」「**拓扑怎么查询**」这些底层能力，由另一个独立仓库 `cann/hcomm` 提供。

为什么要拆？核心动机是**解耦与独立演进**。两个仓库可以独立编译、独立发版本，HCCL 升级算子不必连带重发整套通信基础库，反之亦然。而把它们在运行时连接起来的机制，叫做 **dlsym 动态加载**——HCCL 在运行时通过函数名去 `libhcomm.so` 里查找并调用 HCOMM 的接口，而不是在编译期硬绑定。

> 对初学者的类比：HCCL 像是「菜谱」（规定做什么菜、按什么步骤），HCOMM 像是「厨房与厨具」（真正把食材搬来搬去、加热出锅）。两者分开维护，菜谱升级不一定要换厨房。dlsym 就是「菜谱在开餐时才去厨房里取对应的厨具」。

#### 4.2.2 核心流程（两仓如何协作）

把一次 HCCL 算子调用粗略地拆成两个阶段，可以很直观地看到两仓的分工：

```text
应用 / AI 框架
     │  调用 HcclAllReduce(...)
     ▼
┌───────────────────────────  cann/hccl（本仓，L1 算子层）──────────────────────────┐
│  ① 算子入口（版本兼容判断、入参校验、日志）                                          │
│  ② OpParam 参数装配                                                                │
│  ③ Selector 算法选择 → 产出 algName（如 AicpuAllReduceSoleNHR）                     │
│  ④ Executor 算法执行编排 → 调用 Template（引擎模板）                                │
└────────────────────────────────────────────────────────────────────────────────────┘
     │  通过 dlsym 调用 HCOMM 接口（运行时动态加载 libhcomm.so）
     ▼
┌───────────────────────────  cann/hcomm（独立仓，L2/L3 通信基础层）──────────────────┐
│  L2 通信域管理（HCCM）：通信域 + 拓扑管理 + 资源管理（控制面）                        │
│  L3 基础通信：通信原语 Write/Read/Reduce + 同步 Notify（数据面）                     │
│     → 真正驱动通信硬件（AICPU / AIV / CCU / RoCE 网卡 / SDMA …）搬运数据             │
└────────────────────────────────────────────────────────────────────────────────────┘
```

关键要点有三：

1. **职责分层**：HCCL 负责「算子与算法编排」，HCOMM 负责「通信域管理与底层搬数据」。
2. **解耦方式**：跨仓调用全部走 `dlsym`，HCCL 不会在编译期 `#include` HCOMM 的私有头文件。
3. **控制面/数据面分离**：HCOMM 内部把「资源管理、拓扑查询」（控制面）和「数据搬运、同步」（数据面）分开，HCCL 算子是数据面的消费方。

> 这三点对应架构的四条「硬约束」，4.3 节会逐一对照源码展开。

#### 4.2.3 源码精读

先看 README 对「软件组成」最简洁的一句话定义：

[README.md:L24-L27](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/README.md#L24-L27) —— 这段把 HCCL 拆成两块：**HCCL 集合通信库**（包含内置与扩展通信算子，提供对外接口，即本仓）与 **HCOMM 通信基础库**（采用分层解耦设计，将通信能力划分为控制面和数据面，独立仓 `cann/hcomm`）。这是区分两个仓库职责的最权威一句话。

再看 `AGENTS.md` §1「仓库定位」，它把 README 的组成关系上升到「架构约束」层面：

[AGENTS.md:L7-L11](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/AGENTS.md#L7-L11) —— 这段定义了 HCCL 在 CANN 中的定位（核心集合通信库，提供原语与点对点通信，支持单算子与图模式），并明确「软件组成」是两仓相加，且「**两仓通过 `dlsym` 动态加载解耦，可独立编译、独立版本演进**」。这就是 4.2.1 节那个等式的权威出处。

最后，关于「dlsym 解耦」这一硬约束，`AGENTS.md` §3 把它写成了不可违反的规则：

[AGENTS.md:L43-L50](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/AGENTS.md#L43-L50) —— 这张「架构约束（硬性，不可违反）⭐」表格共四行，其中「HCCL 与 HCOMM 解耦」一行明确规定：**HCCL 不得 `#include` HCOMM 私有头；不得引入对 `cann/hcomm` 的编译期硬依赖；跨仓调用走 `src/common/hcomm_dlsym/` 的符号表 + `dlsym`**。这条约束决定了整个 `src/common/hcomm_dlsym/` 目录的存在意义（详见 Unit 6）。

> 小结：`README.md` 告诉你「是什么」，`AGENTS.md` 告诉你「必须怎么做、不能怎么做」。读 HCCL 源码时，凡是涉及跨仓调用，都要回到 `src/common/hcomm_dlsym/` 去看符号表，而不是去找 HCOMM 的头文件。

#### 4.2.4 代码实践（源码阅读型）

> **实践目标**：亲眼看到「dlsym 解耦」在工程里落地为目录，而不只是文档里的一句话。

操作步骤：

1. 在仓库根目录查看 `AGENTS.md` §2 给出的目录结构：[AGENTS.md:L13-L27](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/AGENTS.md#L13-L27)。注意 `common/` 一行里列出了 `hcomm_dlsym`。
2. 用文件浏览工具定位目录 `src/common/hcomm_dlsym/`（**示例操作**：在终端执行 `ls src/common/hcomm_dlsym/`）。
3. 你会看到一系列按「域」划分的 `_dl` 文件，例如 `hccl_res_dl.*`、`hcomm_primitives_dl.*`、`hccl_rank_graph_dl.*`。不需要读懂内容，只要确认：**HCCL 调用 HCOMM 的入口都集中在这里**。

需要观察的现象 / 预期结果：

- 你会发现 HCCL 源码里**没有任何一处**直接 `#include` 来自 `cann/hcomm` 的私有头，所有跨仓能力都封装成 `*_dl` 这种「动态加载封装」文件。这正是 4.2.3 节那条硬约束在代码里的体现。后续 Unit 6 会逐个拆解这些 `_dl` 文件。

> 本实践为「目录定位型」，无需编译或运行；如果环境里没有 `src/`（例如只克隆了部分代码），请标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：用一句话区分 `cann/hccl` 和 `cann/hcomm` 各自负责什么。

> **参考答案**：`cann/hccl`（本仓）负责集合通信**算子**——算子入口、参数装配、算法选择与执行编排；`cann/hcomm`（独立仓）负责通信**基础能力**——通信域与拓扑管理、以及 Write/Read/Reduce/Notify 等底层搬数据原语。

**练习 2**：为什么 HCCL 不能在编译期直接 `#include` HCOMM 的头文件？走的是哪条路径？

> **参考答案**：因为架构约束要求两仓「独立编译、独立版本演进」，编译期硬依赖会破坏这种解耦。跨仓调用统一走 `src/common/hcomm_dlsym/` 下的符号表 + 运行时 `dlsym` 动态加载 `libhcomm.so`。

### 4.3 CANN 软件分层逻辑与架构约束

#### 4.3.1 概念说明

4.2 节讲了「两仓」，本节把视角拉高到整个 CANN 软件栈，看 HCCL 在其中的**三层结构**和四条**架构硬约束**。

架构简介把软件分成三层（自上而下）：

| 软件层次 | 职责 | 代码仓位置 |
|----------|------|-----------|
| **HCCL 集合通信算子** | 算子入口 → 算法选择 → 算法执行 | `cann/hccl`（本仓） |
| **HCOMM 集合通信域管理（HCCM）** | 通信域 + 拓扑管理 + 资源管理 | `cann/hcomm` |
| **HCOMM 基础通信** | 资源管理 + 通信原语执行 | `cann/hcomm` |

注意：虽然 HCOMM 是一个仓库，但它的**内部**又分成了「通信域管理（HCCM，L2）」和「基础通信（L3）」两层。而 HCCL（L1）只对应本仓。依赖方向严格**自上而下**：`coll_comm_ops`（HCCL）→ `coll_communicator_mgr` → `base_comm`，下层永远不能反过来依赖上层。

这种分层不是装饰，它通过四条**硬约束**强制落地。理解这四条约束，是理解整个工程「为什么这么组织」的钥匙。

#### 4.3.2 核心流程（四条架构约束）

四条约束是贯穿全手册的「红线」，后面每一讲遇到跨层调用、目录归属、控制面/数据面问题时，都该回这里对照：

1. **分层依赖方向**：上层依赖下层，下层不能反向依赖上层。
   - 例：HCOMM 的 `base_comm` 不能反向依赖 `coll_communicator_mgr`；两者都不能反向依赖 HCCL 的 `coll_comm_ops`。
   - 落到 HCCL：**HCCL 不得被 HCOMM 反向依赖**；HCCL 调 HCOMM 只能走 dlsym。

2. **控制面 / 数据面分离**：
   - **控制面** = 资源管理、拓扑查询。
   - **数据面** = 数据搬运（Write/Read/Reduce）、同步（Notify）。
   - 两层接口独立演进、互不耦合。HCCL 算子属于**数据面消费方**，不得在算子层耦合 HCOMM 控制面的内部实现。

3. **HCCL 与 HCOMM 解耦**：跨仓调用一律走 `dlsym`，两仓独立编译、独立版本演进（4.2 节已展开）。

4. **legacy 不持续演进**：`legacy/` 目录只用于历史版本兼容（如 ascend910、ascend950 旧流程），不承接新特性；新能力一律落在标准目录。

> 还有一条与「新算子落点」相关的实践约束：**官方新算子落 `src/ops/<op>/`；社区试验性算子落 `experimental/ops/<op>/`**，均按 `executor/selector/template` 组织。这条会在 u1-l3（目录结构）和 u7-l3（experimental）详细展开。

#### 4.3.3 源码精读

先看架构简介 §3.1 的三层分层概览：

[architecture-brief.md:L189-L196](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/architecture/architecture-brief.md#L189-L196) —— 这是 §3.1「分层架构概览」，用一张三行表格把「软件层次 / 职责 / 代码仓位置」对齐：HCCL 算子层在 `hccl` 仓，HCOMM 的通信域管理与基础通信两层都在 `hcomm` 仓。这就是 4.3.1 节那张表的权威出处。

再看 `AGENTS.md` §3 把它落成「分层」+「约束」两张表：

[AGENTS.md:L33-L41](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/AGENTS.md#L33-L41) —— 这是 §3 的「分层」表，明确三层名称（coll_comm_ops / coll_communicator_mgr / base_comm）及其仓位置，并在表下一行点明依赖方向「自上而下：`coll_comm_ops`（HCCL）→ `coll_communicator_mgr` → `base_comm`」。

[AGENTS.md:L43-L50](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/AGENTS.md#L43-L50) —— 这是 §3 的「架构约束（硬性，不可违反）⭐」表，把四条约束逐条写成「约束内容 + AI Agent 行为要求」。这张表是后续所有改动的「检查清单」。

最后，架构简介末尾的「软件架构约束说明」是对同一组约束的最终复述：

[architecture-brief.md:L274-L281](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/architecture/architecture-brief.md#L274-L281) —— 这张表用更紧凑的语言重述了四条约束（分层依赖方向、控制面/数据面分离、HCCL 与 HCOMM 解耦、legacy 不持续演进）。当 `AGENTS.md` 与 `architecture-brief.md` 都提到同一约束时，二者表述一致、互为印证。

> 三处引用构成一个闭环：`architecture-brief.md` 给出权威定义 → `AGENTS.md` §3 分层表 + 约束表给出落地要求 → `architecture-brief.md` 末尾再总结。读 HCCL 源码遇到任何架构疑问，先回这三处。

#### 4.3.4 代码实践（源码阅读型）

> **实践目标**：把抽象的「四条约束」落到你能看见的目录与文件上。

操作步骤：

1. 打开架构简介 §3.2 的目标目录结构：[architecture-brief.md:L200-L228](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/architecture/architecture-brief.md#L200-L228)。
2. 在仓库里逐一确认这些目录真实存在（**示例操作**：`ls src/ops/`、`ls src/common/`、`ls include/`、`ls experimental/`）。
3. 回答：约束 3「HCCL 与 HCOMM 解耦」对应到代码，是哪个目录在承担「跨仓符号表 + dlsym」？约束 4「legacy 不持续演进」对应的 `legacy/` 目录在哪个仓？

需要观察的现象 / 预期结果：

- 约束 3 对应本仓的 `src/common/hcomm_dlsym/`（与 4.2.4 节呼应）。
- 约束 4 对应的 `legacy/` 目录位于 **`cann/hcomm` 仓**（架构简介 §3.2 的 HCOMM 目录结构里有 `src/legacy/`，含 `ascend910`、`ascend950` 旧流程兼容代码），**本仓 `cann/hccl` 没有 `legacy/`**——这是一个验证「两仓分工」的好证据。

> 本实践为「目录比对型」，无需编译；若仓库为只读裁剪版，部分目录可能缺失，请标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：三层软件里，哪两层位于同一个仓库？依赖方向是什么？

> **参考答案**：HCOMM 集合通信域管理（HCCM，L2）与 HCOMM 基础通信（L3）同在 `cann/hcomm` 仓；HCCL 算子（L1）在 `cann/hccl` 仓。依赖方向自上而下：HCCL → HCCM → 基础通信，下层不得反向依赖上层。

**练习 2**：「控制面」和「数据面」分别包含哪些能力？HCCL 算子属于哪一面？

> **参考答案**：控制面 = 资源管理、拓扑查询；数据面 = 数据搬运（Write/Read/Reduce）与同步（Notify）。HCCL 算子是**数据面的消费方**，不能在算子层耦合 HCOMM 控制面的内部实现。

**练习 3**：为什么 `legacy/` 不能承接新特性？新能力应该落在哪里？

> **参考答案**：`legacy/` 仅用于历史版本兼容，不持续演进，承接新特性会违背「分层依赖」与可维护性。新能力一律落在标准目录——官方算子落 `src/ops/`，社区试验算子落 `experimental/ops/`。

## 5. 综合实践

> **任务**：阅读 `README.md` 与 `AGENTS.md`，画出 HCCL 在 CANN 软件栈中的位置图，并用一句话区分 `cann/hccl` 与 `cann/hcomm` 各自负责什么，标注两者如何通过 dlsym 解耦。

这是一道把本讲三个模块串起来的小任务。建议按下面四步完成：

1. **画三层位置图**（对应 4.3）：自上而下画四个大框——「AI 框架」「HCCL 算子（本仓，L1）」「HCOMM 通信域管理 + 基础通信（`cann/hcomm`，L2/L3）」「昇腾 NPU 硬件」。用箭头标出自上而下的依赖方向。参考图：[architecture-brief.md:L189-L196](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/architecture/architecture-brief.md#L189-L196)。

2. **在 HCCL 与 HCOMM 之间标注 dlsym**（对应 4.2）：在 L1 和 L2/L3 之间的箭头上写「dlsym 动态加载 `libhcomm.so`」，并在 HCCL 框里写上承担这一职责的目录 `src/common/hcomm_dlsym/`。参考：[README.md:L24-L27](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/README.md#L24-L27)、[AGENTS.md:L7-L11](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/AGENTS.md#L7-L11)。

3. **写一句话区分两仓**（对应 4.2）：在图旁写一句话，例如——
   > 本仓 `cann/hccl` 负责集合通信**算子**（入口/校验/算法选择/执行编排）；`cann/hcomm` 负责通信**基础能力**（通信域与拓扑管理、Write/Read/Reduce/Notify 搬数据原语）；两仓在运行时通过 `dlsym` 动态加载解耦，可独立编译、独立版本演进。

4. **把控制面/数据面标到 HCOMM 框内**（对应 4.3）：在 HCOMM 框里再细分两块——「控制面：通信域/拓扑/资源管理」「数据面：Write/Read/Reduce/Notify」，并标明 HCCL 算子是数据面消费方。参考：[AGENTS.md:L43-L50](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/AGENTS.md#L43-L50)、[architecture-brief.md:L274-L281](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/architecture/architecture-brief.md#L274-L281)。

**预期结果**：一张四层结构图 + 一句两仓分工说明 + dlsym 标注 + 控制面/数据面划分。完成后，你应该能在不看资料的情况下，回答「HCCL 是什么、由哪两部分组成、和 HCOMM 怎么协作、为什么不能反向依赖」这四个问题。

> 本实践为「文档阅读 + 画图型」，不需要 NPU 环境，也不需要编译；推荐用纸笔或任意画图工具完成。

## 6. 本讲小结

- **HCCL 是昇腾 NPU 的高性能集合通信库**，承上启下：对上支持 AI 框架，对下使能多款 NPU 之间通信；核心能力覆盖原语、算法、协议、执行模式、扩展与场景六大维度。
- **HCCL 由两个仓库组成**：本仓 `cann/hccl`（算子库）+ 独立仓 `cann/hcomm`（通信基础库）。前者负责算子入口/校验/算法选择/执行编排，后者负责通信域管理与底层搬数据。
- **两仓通过 dlsym 解耦**：跨仓调用统一走 `src/common/hcomm_dlsym/`，两仓可独立编译、独立版本演进；HCCL 不得在编译期 `#include` HCOMM 私有头。
- **软件分三层**：HCCL 算子（L1，本仓）→ HCOMM 通信域管理 HCCM（L2）→ HCOMM 基础通信（L3），后两层同在 `cann/hcomm`；依赖严格自上而下。
- **四条架构硬约束**：分层依赖方向、控制面/数据面分离、HCCL 与 HCOMM 解耦、legacy 不持续演进——它们是理解工程组织的「红线」。
- **三份入口资料**：`README.md`（是什么）、`AGENTS.md`（必须怎么做）、`docs/zh/architecture/architecture-brief.md`（架构权威来源）。

## 7. 下一步学习建议

本讲建立了「HCCL 是什么、在 CANN 栈中什么位置」的全局认知。建议按以下顺序继续：

- **u1-l2 集合通信核心概念与算法**：补齐 rank、通信域、RankGraph、AllReduce/AllGather/ReduceScatter 等领域概念，理解 Ring/Mesh/RHD 等算法与节点内/节点间分级通信。这是读懂后续所有算子语义的前提。
- **u1-l3 源码目录结构剖析**：对照本讲提到的 `src/ops/`、`op_common/` 四大组件、`experimental/`，把「目录」和「职责」真正对上号。
- **随手翻阅**：`include/hccl.h`（对外算子 API 长什么样）、`docs/zh/architecture/architecture-brief.md` 第 2 节（集合通信模型与通信引擎），先有个直观印象，不要求看懂细节。

> 当你能在不看资料的情况下复述本讲「综合实践」里的那张四层图，就可以放心进入下一讲了。

# u1-l1 HCOMM 是什么：项目定位与整体架构

## 1. 本讲目标

学完本讲，你应该能够：

1. 用一句话说清 HCOMM 与 HCCL 的关系，以及 HCOMM 在 CANN 软件栈中的位置。
2. 解释「控制面 / 数据面」分层解耦设计：哪部分代码管理通信资源，哪部分代码负责搬数据。
3. 说出 HCOMM 支持的通信引擎（AICPU_TS、CPU_TS、AIV、CCU）与通信协议（PCIe、HCCS、RDMA/ RoCE、UB 等）各自的定位。
4. 看懂仓库顶层目录（`src`、`include`、`pkg_inc`、`experimental`、`test`、`docs`、`examples`）的职责，并能判断 `src` 下每个子目录属于控制面还是数据面。

本讲是整套学习手册的第一讲，不要求你写代码，只要求你「读懂地图」——后续所有讲义都会反复引用本讲建立的分层心智模型。

## 2. 前置知识

本讲面向零基础读者，但以下几个名词最好先有个模糊印象：

- **NPU（昇腾设备）**：华为的 AI 加速芯片，类似 GPU。多块 NPU 协同训练大模型时，彼此之间要交换数据（例如同步梯度）。
- **集合通信（Collective Communication）**：一组设备共同参与的通信操作，典型算子有 AllReduce（所有设备把各自数据归约成一份相同结果）、Broadcast、AllGather、ReduceScatter 等。大模型训练中，梯度同步几乎都靠 AllReduce。
- **通信域（Communicator / Comm）**：集合通信执行的上下文，定义「哪些设备参与通信、每个成员的编号（Rank ID）是多少」，并持有通信所需资源。
- **Rank**：通信域中的一个成员，拥有从 0 开始的唯一编号，通常对应一块 NPU。
- **RDMA / RoCE**：Remote Direct Memory Access，远程直接内存访问；RoCE 是基于以太网的一种 RDMA 实现，让两台机器的网卡可以绕过对方 CPU 直接读写对方内存。
- **HCCS**：华为自研的芯片间高速互连总线（Huawei Cache Coherence System），同一台服务器内多块昇腾设备通过它直连，带宽高、时延低。
- **PCIe**：服务器内常见的通用高速总线，也能承担芯片间通信。
- **控制面 / 数据面**：借自网络领域的经典划分——控制面负责「建路」（规划、协商、管理资源），数据面负责「跑车」（真正搬运数据）。HCOMM 也采用了这种划分。

不确定的名词不用急着查，正文遇到时会再解释。

## 3. 本讲源码地图

| 文件 / 目录 | 作用 |
|---|---|
| `README.md` | 项目门面：一句话定位、关键特性、目录结构说明、版本配套与快速入口 |
| `docs/zh/architecture/architecture-brief.md` | 最核心的架构入门文档：集合通信模型、RankGraph 拓扑模型、通信引擎、软件分层逻辑与架构约束 |
| `docs/zh/architecture/overview.md` | 架构总览文档（当前仓库中该文件基本是一张待填充的空白模板，正文以 architecture-brief.md 为准） |
| `src/` | 源码根目录，含 `base_comm`（数据面）、`coll_communicator_mgr`（控制面）、`legacy`（历史兼容） |
| `include/`、`pkg_inc/` | 对外头文件与包间接口头文件（下一讲代码地图会详细展开） |

## 4. 核心概念与源码讲解

本讲的三个最小模块：①README 中的项目定位与关键特性；②architecture-brief 中的软件分层（控制面/数据面）与对外 API 分层；③多引擎多协议与目录职责对照。

### 4.1 项目定位：HCOMM 是什么

#### 4.1.1 概念说明

先回答最基本的问题：HCOMM 和 HCCL 是什么关系？

- **HCCL（Huawei Collective Communication Library）** 是昇腾 NPU 集群的高性能集合通信库，对 AI 框架（PyTorch/MindSpore 等）暴露 AllReduce、Broadcast 等标准集合通信算子。
- **HCOMM（Huawei Communication）** 是 HCCL 背后的**通信基础库**，提供通信域以及通信资源的管理能力。可以把 HCCL 理解为「算子层」，HCOMM 理解为「地基」：算子层负责选算法、编排通信步骤，地基负责建通信域、查拓扑、分配通道和内存、执行数据搬运。

两者的一个重要工程约定是**解耦**：HCCL 算子通过 `dlsym` 动态加载 HCOMM 接口，两个仓库可以独立编译、独立演进（这一点在 4.2.3 的架构约束表中会看到原文）。

#### 4.1.2 核心流程

理解 HCOMM 定位的推理链条：

```text
大模型训练/推理 = 多卡协同
        ↓
迭代末需要 AllReduce 同步梯度 → 通信成为扩展瓶颈
        ↓
需要高性能集合通信库 → HCCL（算子层：算法选择与编排）
        ↓
算子要跑起来需要：通信域、拓扑信息、通道、内存、执行引擎
        ↓
HCOMM（基础库层：统一管理这些资源并提供编程接口）
```

#### 4.1.3 源码精读

README 开头一句话给出定位——HCOMM 是 HCCL 的通信基础库：

> HCOMM（Huawei Communication）是HCCL的通信基础库，提供通信域以及通信资源的管理能力。

见 [README.md:L7-L9](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/README.md#L7-L9)。

紧接着列出三条关键特性：多通信引擎、多通信协议、通信平台与算子开发解耦：

[README.md:L11-L15](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/README.md#L11-L15)

```text
- 支持昇腾设备上的多种通信引擎，充分发挥硬件能力。
- 支持多种通信协议，包括PCIe、HCCS、RDMA等。
- 通信平台与通信算子开发解耦，支持通信算子的独立开发、构建与部署。
```

这三条特性分别对应本讲的 4.3 节（引擎与协议）和 4.2 节（分层解耦），是整个项目的「卖点总纲」。

在架构简介文档中，还能看到 HCCL 的能力全景表（AllReduce/AllGather 等原语、Ring/Mesh 等算法、UBC/UBG/UBoE/RoCE/HCCS 等协议），以及 HCCL 在 CANN 软件栈中「介于 AI 框架与硬件驱动之间」的位置说明：

- HCCL 核心能力表：[docs/zh/architecture/architecture-brief.md:L21-L31](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/docs/zh/architecture/architecture-brief.md#L21-L31)
- HCCL 在 CANN 中的位置：[docs/zh/architecture/architecture-brief.md:L32-L33](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/docs/zh/architecture/architecture-brief.md#L32-L33)

#### 4.1.4 代码实践

**实践：用三句话向别人介绍 HCOMM**

1. **实践目标**：检验你是否真正理解了项目定位，而不是背下了名词。
2. **操作步骤**：
   - 阅读上面引用的 README 概述段落与 architecture-brief 第 1 章。
   - 不看资料，写下三句话回答：①HCOMM 是什么；②它和 HCCL 什么关系；③它解决什么问题。
   - 把你的三句话与 [docs/zh/architecture/architecture-brief.md:L188-L196](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/docs/zh/architecture/architecture-brief.md#L188-L196) 的软件分层表逐条比对。
3. **需要观察的现象**：自己写出的版本是否遗漏了「基础库」「通信域管理」「数据搬运」这三个关键词中的任何一个。
4. **预期结果**：能写出类似「HCOMM 是 HCCL 之下的通信基础库，控制面管通信域/拓扑/资源，数据面提供原语搬数据，HCCL 算子通过它驱动硬件」的表述。
5. 待本地验证（本实践为阅读理解型，无需运行环境）。

#### 4.1.5 小练习与答案

**练习 1**：为什么说「通信是大模型训练的扩展瓶颈」？用 README/架构文档中的信息回答。

**参考答案**：数据并行下每张 NPU 处理不同样本，每次迭代后都要通过 AllReduce 同步梯度；模型/专家并行还依赖 AllGather、ReduceScatter、AlltoAll 协同（见 [architecture-brief.md:L14-L19](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/docs/zh/architecture/architecture-brief.md#L14-L19)）。卡数越多、模型越大，通信量越大，通信时延直接吃掉计算时间，所以高性能集合通信库是规模化的前提。

**练习 2**：HCCL 与 HCOMM 是两个仓库吗？它们如何互相找到对方？

**参考答案**：是两个仓库。HCCL 算子通过 `dlsym` 动态加载 HCOMM 接口，因此两仓可独立编译、独立版本演进（见 4.2.3 引用的架构约束表）。

### 4.2 控制面与数据面：分层解耦设计

#### 4.2.1 概念说明

HCOMM 最核心的设计决策是把通信能力切成两层（README 中称为控制面/数据面，architecture-brief 中称为软件分层）：

- **控制面（coll_communicator_mgr，集合通信域管理层，文档中也写作 HCCM）**：提供拓扑信息查询与通信资源管理。通俗地说，它回答「网络长什么样（拓扑）、我要和谁通信（通信域）、路上有哪些资源可用（通道/线程/内存的分配协调）」。
- **数据面（base_comm，基础通信层）**：提供本地操作、算子间同步、通信操作等数据搬运和计算功能。通俗地说，它回答「数据怎么搬：本地拷贝、本地归约、往对端写/读、发通知等」。

为什么这么拆？README 说得很直白：控制面提供通信资源，数据面提供操作资源的方法，让通信算子开发人员聚焦业务创新，无需关注芯片底层实现细节。此外这种拆分还带来接口独立演进、层间不可反向依赖的工程好处（见 4.2.3）。

数据面内部再分两块：`resources`（通信资源：端点 Endpoint、通道 Channel、通信内存、线程/引擎等）与 `primitives`（通信原语：操作这些资源的 Write/Read/Reduce/Notify 等）。

#### 4.2.2 核心流程

一次集合通信（如 AllReduce）在分层视角下的简化流程：

```text
【控制面 coll_communicator_mgr】
1. 通信域初始化：各 rank 互相发现、交换信息（rank_info_detect）
2. 拓扑构建：RankGraph 建模"谁和谁怎么连"（rank_graph）
3. 资源协调：按拓扑创建端点、通道、内存注册（resource_mgr / 配置 config_mgr）
        ↓  把资源交给
【数据面 base_comm】
4. 算子拿到通道/线程后调用原语：
   本地归约 LocalReduce → 远端写 Write → 通知同步 Notify → ...
5. 通信引擎（AICPU_TS/AIV/CCU...）驱动硬件真正搬移数据
```

记住这条「先建路、后跑车」的主线，后续 u2（控制面）与 u3（数据面）单元就是分别沿这条线的上半段和下半段展开。

#### 4.2.3 源码精读

README 中对控制面/数据面的原始定义：

[README.md:L19-L24](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/README.md#L19-L24)

```text
- 控制面：提供拓扑信息查询与通信资源管理功能。
- 数据面：提供本地操作、算子间同步、通信操作等数据搬运和计算功能。
  控制面提供通信资源，数据面提供操作资源的方法……
```

architecture-brief 中的软件分层表，把「软件层次—职责—代码仓位置」三者对齐：

[docs/zh/architecture/architecture-brief.md:L188-L196](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/docs/zh/architecture/architecture-brief.md#L188-L196)

| 软件层次 | 职责 | 代码仓位置 |
|----|------|--------|
| HCCL 集合通信算子 | 算子入口 → 算法选择 → 算法执行 | hccl 仓 |
| HCOMM 集合通信域管理 | 通信域 + 拓扑管理 + 资源管理 | hcomm / coll_communicator_mgr (HCCM) |
| HCOMM 基础通信 | 资源管理 + 通信原语执行 | hcomm / base_comm |

文档末尾的架构约束表是分层的「法律条文」，三条都值得背下来：

[docs/zh/architecture/architecture-brief.md:L275-L283](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/docs/zh/architecture/architecture-brief.md#L275-L283)

```text
| 分层依赖方向       | 上层依赖下层，下层不能反向依赖上层：
|                    | base_comm 不能反向依赖 coll_communicator_mgr ……
| 控制面/数据面分离   | 资源管理、拓扑查询属控制面；数据搬运与同步属数据面；两层接口独立演进
| HCCL 与 HCOMM 解耦 | HCCL 算子通过 dlsym 动态加载 HCOMM 接口，两仓可独立编译、独立版本演进
| legacy 不持续演进   | legacy/ 仅用于历史版本兼容，不承接新特性
```

注意一个重要事实：仓库里的 `docs/zh/architecture/overview.md` 目前只是一份空白的文档模板（标题为「系统概述/架构分层图/核心模块/数据流/模块依赖关系」，但表格与正文均未填写），见 [docs/zh/architecture/overview.md:L1-L16](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/docs/zh/architecture/overview.md#L1-L16)。学习架构请以 `architecture-brief.md` 和 README 为准，不要在空白模板里找内容。

#### 4.2.4 代码实践

**实践：绘制控制面/数据面模块划分草图（本讲综合实践的前半部分）**

1. **实践目标**：把 README 目录树中 `src` 下每个子目录标注到控制面或数据面，形成一张可长期使用的「代码地图草图」。
2. **操作步骤**：
   - 打开 README 的目录结构说明：[README.md:L30-L70](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/README.md#L30-L70)。
   - 在纸上或任意画图工具中画两个大框：左框写「控制面 coll_communicator_mgr」，右框写「数据面 base_comm」，下方画一个横条写「legacy（历史兼容）」。
   - 把 README 目录树中列出的子目录填进对应框（`communicator`、`config_mgr`、`dfx`、`rank_graph`、`resource_mgr`、`api_c_adpt` → 控制面；`primitives`、`resources`、`common` → 数据面；`ascend910`、`ascend950` → legacy 横条）。
   - 用 `ls src/coll_communicator_mgr` 和 `ls src/base_comm` 对照实际目录，你会发现两个 README 未列出的真实子目录：`coll_communicator_mgr/rank_info_detect`、`coll_communicator_mgr/team`（rank 发现建链与 Team 机制，后续 u2-l5、u2-l6 会讲），以及 `base_comm/dfx`、`base_comm/hcomm_res_mgr.h/.cc`（数据面维测与统一资源管理器，u3-l1 会讲）。把它们补进草图并打上问号标记。
3. **需要观察的现象**：README 目录树与真实目录的差异；哪些目录名你已经能从字面猜出职责。
4. **预期结果**：得到一张包含约 15 个目录的草图，且每个目录都有归属（控制面/数据面/legacy）。
5. 待本地验证（阅读型实践，无需运行环境）。

#### 4.2.5 小练习与答案

**练习 1**：数据面的 `resources` 和 `primitives` 两个子目录，职责有什么不同？

**参考答案**：`resources` 管「通信资源」本身——端点（Endpoint）、通道（Channel）、注册内存（CommMem）、线程/引擎等对象的创建与销毁；`primitives` 是「操作资源的方法」——在通道上 Write/Read、本地拷贝归约、Notify 同步等。对应 README 中「控制面提供通信资源，数据面提供操作资源的方法」在数据面内部的进一步细分（也对应 architecture-brief 3.3 表中 L3-res 与 L3-prim 两层接口）。

**练习 2**：如果有人在 `src/base_comm` 里写了一行 `#include "coll_communicator_mgr/..."`，违反了什么约束？

**参考答案**：违反「分层依赖方向」约束——上层依赖下层、下层不能反向依赖上层，`base_comm` 不能反向依赖 `coll_communicator_mgr`（见 [architecture-brief.md:L275-L283](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/docs/zh/architecture/architecture-brief.md#L275-L283)）。

**练习 3**：`src/legacy/ascend910` 与 `src/legacy/ascend950` 分别是什么？新特性会加进去吗？

**参考答案**：分别是 A2&A3（ascend910）与 A5（ascend950）两代硬件的旧流程兼容代码。legacy 仅用于历史版本兼容、不承接新特性，新能力一律落在标准目录（同上约束表，另见 [README.md:L44-L59](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/README.md#L44-L59) 的目录注释）。阅读源码时应注意区分，避免把 legacy 代码当成新架构来学习。

### 4.3 多引擎多协议与对外接口分层

#### 4.3.1 概念说明

「支持多种通信引擎与协议」是 HCOMM 的第一条关键特性，需要先厘清两组概念。

**通信协议**——数据物理上走什么路：

| 协议 | 一句话说明 |
|---|---|
| HCCS | 服务器内昇腾设备间的高速直连总线，带宽高时延低 |
| PCIe | 服务器内通用高速总线，也可承担设备间通信 |
| RDMA/RoCE | 跨服务器的远程直接内存访问（RoCE 为基于以太网的实现），绕过远端 CPU |
| UBC/UBG/UBoE/UB_MEM | 昇腾 UB 系列网络协议（详见后续讲义与官方文档） |

**通信引擎**——谁来执行通信任务。architecture-brief 把通信引擎定义为「通信实体中执行通信任务的核心模块」：向上接收通信资源与任务编排下发的任务，向下通过线程执行调度器驱动通信硬件搬移数据。常见引擎有四种：

| 通信引擎 | 执行方式 | 特点 | 适用场景 |
|---|---|---|---|
| AICPU_TS | AICPU 运行通信 Kernel 并下发 Task 描述符，TS 调度到硬件 | 不占计算核 | 大数据量通信 |
| CPU_TS | Host CPU 运行通信逻辑，TS 调度下发 | 不占计算核、下发开销大 | Atlas A2 专用 |
| AIV | Vector Core 直接执行通信算子 | 低延迟，但占 Vector 核 | 小数据低延迟 |
| CCU | IO Die 上的专用集合通信协处理器，硬化微码执行 | 高带宽低时延、少占计算核，但支持的通信域数量有限 | 专用硬件通信 |

注意文档中的提示：同一集合通信域默认只用一种引擎，算子开发者通过算法选择器自动选择引擎，无需手工指定。

**对外接口分层**——architecture-brief 3.3 用一张表回答「这么多头文件分别给谁用」，这是你浏览 `include/` 目录前的最佳导航。

#### 4.3.2 核心流程

协议与引擎的选题逻辑可以概括为：

```text
问题：数据从 A 搬到 B，走哪条路（协议）？谁来搬（引擎）？
        ↓
协议选择 ≈ 拓扑决定
    同机内（Layer0）：HCCS / UB_MEM ……
    跨机（Layer1+）：RoCE / UBoE ……
        ↓
引擎选择 ≈ 数据量与延迟要求决定
    大数据高带宽 → AICPU_TS（不占计算核）
    小数据低延迟 → AIV（延迟低但占核）
    专用硬件 → CCU（微码加速）
```

拓扑分层（netLayer）的概念在 architecture-brief 2.2 有精确定义：Server 内为 Layer0（如 HCCS 直连），Server 间为 Layer1（如 RoCE 经交换机），通信质量随层级增加而递减。这一模型会在 u2-l4（RankGraph）中深入。

#### 4.3.3 源码精读

通信引擎四引擎对比表的原文位置：

[docs/zh/architecture/architecture-brief.md:L119-L128](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/docs/zh/architecture/architecture-brief.md#L119-L128)

其中关于「同一通信域默认只用一种引擎」的说明：

[docs/zh/architecture/architecture-brief.md:L127-L128](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/docs/zh/architecture/architecture-brief.md#L127-L128)

对外 API 分层关系表——`include/` 下各头文件的「使用说明书」：

[docs/zh/architecture/architecture-brief.md:L259-L273](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/docs/zh/architecture/architecture-brief.md#L259-L273)

摘录关键行（完整表见原文）：

| 层次 | 接口 | 面向 |
|------|------|------|
| L1 | HCCL 算子（hccl.h） | AI 框架适配层 |
| L2-comm | HCOMM 通信域（hccl_comm.h） | 框架适配层 |
| L2-res | HCOMM 拓扑/资源（hccl_res.h / hccl_channel.h / hccl_rank_graph.h） | 算子开发者 |
| L3-prim | HCOMM 原语（hcomm_primitives.h） | 算子开发者、通信库开发者 |
| L3-res | HCOMM 基础资源（hcomm_res.h / hcomm_channel.h） | 通信库开发者 |
| CCU | include/ccu/（ccu_primitives.hpp 等） | CCU 算子开发者 |

表后还有两句关键结论：**L2-res + L3-prim 是新开放的算子编程接口**（面向自定义通信算子开发），**L3-res + L3-prim 是通信库开发接口**。你现在不需要记住每个头文件，只需记住「头文件是分层分受众的，查表使用」。

这些头文件实际就在仓库顶层 `include/` 目录（可用 `ls include` 验证：`hcomm_primitives.h`、`hcomm_res.h`、`hcomm_channel.h`、`hcomm_res_defs.h`、`hcomm_team_defs.h`、`hccl/`、`ccu/`），与上表一一对应；README 目录树中 `include` 的注释见 [README.md:L60-L62](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/README.md#L60-L62)（`include` 为对外头文件，`pkg_inc` 为包间接口头文件）。

#### 4.3.4 代码实践

**实践：给五个头文件找「主人」**

1. **实践目标**：学会用 API 分层表定位接口，而不是在头文件堆里乱翻。
2. **操作步骤**：
   - 执行 `ls include include/hccl include/ccu`，列出所有对外头文件。
   - 对照 [architecture-brief.md:L259-L273](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/docs/zh/architecture/architecture-brief.md#L259-L273) 的分层表，从中挑 5 个（建议：`hccl/hccl_comm.h`、`hcomm_primitives.h`、`hcomm_res.h`、`hccl/hccl_rank_graph.h`、`ccu/ccu_primitives.hpp`）。
   - 为每个头文件记录三列信息：属于哪一层次（L1/L2/L3/CCU）、面向谁（框架/算子开发者/通信库开发者）、从字面猜测它提供什么能力。
3. **需要观察的现象**：头文件名中的关键词（comm、res、primitives、rank_graph）与分层职责的对应规律。
4. **预期结果**：得到一张 5 行的小表。规律示例：带 `primitives` 的提供数据面原语操作；带 `res` 的提供资源管理；`hccl_comm` 提供通信域生命周期。
5. 待本地验证（仅需能执行 `ls` 的环境；接口详细学习在后续讲义）。

#### 4.3.5 小练习与答案

**练习 1**：AIV 引擎「低延迟但占 Vector 核」是什么意思？为什么小数据场景反而合适？

**参考答案**：AIV 引擎由 Vector Core（本可做向量计算的核）直接执行通信算子，省去了任务描述符下发等中间环节所以延迟低；但执行期间这部分计算核被占用。小数据通信时，传输本身极快，固定开销（下发、调度）占比大，AIV 省掉这些开销收益最大；而大数据时占核时间长、得不偿失（见 [architecture-brief.md:L121-L126](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/docs/zh/architecture/architecture-brief.md#L121-L126) 的引擎对比表）。

**练习 2**：`experimental/` 目录和 `src/` 目录有什么区别？

**参考答案**：`experimental/` 是社区贡献的试验性代码目录，内部目录结构大体与 `src` 保持一致，但不保证新接口的兼容性，也不会被商用版本采纳（见 [architecture-brief.md:L252](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/docs/zh/architecture/architecture-brief.md#L252) 及 [README.md:L63](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/README.md#L63)）。生产代码读 `src/`，研究前沿扩展（如第三方网卡插件）再读 `experimental/`。

## 5. 综合实践

**任务：产出你自己的《HCOMM 一页架构地图》**

把 4.2.4 和 4.3.4 两个实践的成果合并，完成一份一页纸的架构地图，包含三个区块：

1. **分层区块**：画出三层结构——HCCL 算子层（hccl 仓）→ HCOMM 控制面（coll_communicator_mgr）→ HCOMM 数据面（base_comm），并附一条箭头注明「HCCL 通过 dlsym 动态加载 HCOMM」。在控制面框内列出 `communicator / config_mgr / rank_graph / resource_mgr / api_c_adpt / dfx / rank_info_detect / team`，在数据面框内列出 `resources / primitives / common / dfx / hcomm_res_mgr`，在图下方单独放 `legacy`（标注「历史兼容，不持续演进」）。
2. **接口区块**：贴上你整理的 5 个头文件分层表（来自 4.3.4）。
3. **引擎/协议区块**：抄录四引擎对比表的引擎名与一句话特点，并列出 PCIe / HCCS / RDMA(RoCE) / UB 系列协议清单，标注「同机 vs 跨机」的典型用途。

完成后的自检标准：合上所有资料，只看这张图，能否不假思索地回答——「`rank_graph` 在哪个目录、属于哪一面？」「AIV 和 AICPU_TS 怎么选？」「自定义通信算子开发者最先看的头文件是哪几个？」。这张图将贯穿后续所有讲义，每学完一讲都可以回来在对应目录下补充细节。

（本实践为纯阅读绘图型，无需昇腾硬件环境；运行环境相关的实践从 u1-l2 构建讲开始。）

## 6. 本讲小结

- HCOMM 是 HCCL 通信基础库：HCCL 负责「选算法、编排算子」，HCOMM 负责「通信域、拓扑、资源管理与数据搬运」，两仓通过 dlsym 解耦、独立演进。
- HCOMM 内部采用控制面/数据面分离：`coll_communicator_mgr`（通信域、拓扑、配置、资源协调）是控制面，`base_comm`（资源 + 原语）是数据面；依赖方向严格自上而下，`base_comm` 不得反向依赖 `coll_communicator_mgr`。
- 数据面内部再分 `resources`（Endpoint/Channel/内存/线程等资源对象）与 `primitives`（Write/Read/Reduce/Notify 等操作原语）。
- 项目支持多引擎（AICPU_TS、CPU_TS、AIV、CCU，按数据量与延迟取舍）与多协议（PCIe、HCCS、RDMA/RoCE、UB 系列，由拓扑层级决定），引擎由算法选择器自动挑选。
- 对外头文件按「L1 算子 / L2 通信域与拓扑资源 / L3 原语与基础资源 / CCU」分受众分层，`include/` 面向对外、`pkg_inc/` 为包间接口、`experimental/` 为不保证兼容的社区试验代码、`src/legacy/` 为不持续演进的历史兼容代码。
- 仓库中 `docs/zh/architecture/overview.md` 目前是空白模板，架构学习以 `architecture-brief.md` 与 README 为准。

## 7. 下一步学习建议

- 下一讲 **u1-l2（源码构建与运行方式）**：学习 `build.sh` 与 CMake 构建体系，亲手把仓库编译一次，为后续所有需要运行环境的实践做准备。
- 若想先看代码地图，可跳读 **u1-l3（目录结构与代码地图）**，它会展开本讲只点到为止的 `include/`、`pkg_inc/` 与 `src/` 各层头文件。
- 建议同步通读 [docs/zh/architecture/architecture-brief.md](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/docs/zh/architecture/architecture-brief.md) 的第 2 章（集合通信模型、RankGraph、同步机制）——本讲只精读了它的第 1、3 章，第 2 章的概念（Rank、netLayer、Channel、Notify）会在 u2 与 u3 单元反复出现。

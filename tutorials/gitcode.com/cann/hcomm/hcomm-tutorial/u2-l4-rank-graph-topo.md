# 拓扑管理：RankGraph 拓扑信息查询

## 1. 本讲目标

学完本讲，你应该能够：

1. 理解 HCOMM 的拓扑模型：什么是网络分层（netLayer）、拓扑实例（topoInstance）、拓扑类型（CommTopo），以及 Node / Endpoint / Edge / Link 之间的关系。
2. 掌握 `include/hccl/hccl_rank_graph.h` 中一族拓扑查询 C 接口（`HcclRankGraphGetLayers`、`HcclRankGraphGetRanksByLayer`、`HcclRankGraphGetLinks` 等）的参数语义与使用约束。
3. 看懂一条完整的调用链：C 接口 → `api_c_adpt` 适配层 → `RankGraph` 基类 → `RankGraphV1` / `RankGraphV2` 实现，理解物理拓扑信息（rankTable、phy_topo）如何被构建为可供查询的 rank 图。
4. 能动手调用接口打印当前通信域的分层拓扑，并根据返回结果画出拓扑示意图。

本讲承接 u2-l2（`CollComm` 通信域对象）。在 u2-l2 中我们提到 `CollComm` 的资源成员里有一个 `RankGraph`——本讲就深入这个成员，看它是如何建模和暴露集群连接关系的。

## 2. 前置知识

- **通信域与 rank**：通信域（communicator）是一组参与集合通信的进程/设备的抽象，每个成员用一个 `rankId` 标识（回顾 u1-l4）。
- **物理拓扑 vs 逻辑 rank 图**：真实集群中，NPU 卡通过 HCCS 总线连在同一个服务器内，服务器之间通过 RoCE 网络交换机互联，多台服务器还能组成"超节点"（SuperPod）。这些物理连线信息（谁和谁直连、经过几跳）叫**物理拓扑**；而通信算子开发者和算法层更关心"rank 之间能怎么连"——HCOMM 把物理拓扑加工成一张以 rank 为节点、以 Link 为边的图，就是 **rank graph**。
- **为什么需要分层**：大规模 AI 集群都是分级组建的——卡 → 服务器 → 超节点 → 集群。不同层级的连接方式完全不同（服务器内是 HCCS/PCIe，跨服务器是 RoCE），所以 rank 图在"图"之上又加了**拓扑层级（netLayer）**抽象：Layer 0 表示服务器内，Layer 1 表示跨服务器（或超节点内），Layer 2 表示跨超节点。
- **拓扑类型**：每一层有自己的连接形态，比如 1DMesh（一维环/链）、CLOS（交换机全互联网络）等，用 `CommTopo` 枚举描述。
- **ABI 兼容**：跨模块传结构体时，尾部追加字段 + 版本号协商是本仓库反复出现的通用手法（u2-l3 讲过 `HcclCommConfig`），本讲的 `CommLink` 也用了同样的设计，可以对照加深理解。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/hccl/hccl_rank_graph.h](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hccl/hccl_rank_graph.h) | 对外 C 接口头文件：`CommTopo`/`CommLink` 等数据类型定义 + 全部拓扑查询接口声明，注释里带大量使用示例 |
| [src/coll_communicator_mgr/api_c_adpt/coll_comm_rank_graph_a_adpt.cc](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/api_c_adpt/coll_comm_rank_graph_a_adpt.cc) | C 接口适配层：把 `HcclComm` 句柄还原为 `CollComm`，取出其 `RankGraph` 成员，再分派到 V2（新架构）或 V1（老流程）实现 |
| [src/coll_communicator_mgr/rank_graph/rank_graph_base.h](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_graph/rank_graph_base.h) | `RankGraph` 抽象基类：定义拓扑查询的统一虚接口和 netLayer 常量 |
| [src/coll_communicator_mgr/rank_graph/rank_graph.h](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_graph/rank_graph.h) / [rank_graph.cc](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_graph/rank_graph.cc) | `RankGraphV1` 实现：从 rankTable 构建分层信息、推断 rank 对之间的通信协议、生成并缓存 `CommLink` |
| [src/coll_communicator_mgr/rank_graph/rank_graph_v2.h](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_graph/rank_graph_v2.h) | `RankGraphV2` 实现（A5 新架构，委托给 pImpl），供对比了解 |
| [docs/zh/comm_op_dev_guide/prog_models_concepts/topology_model.md](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/docs/zh/comm_op_dev_guide/prog_models_concepts/topology_model.md) | 官方拓扑模型文档：Node / Endpoint / Edge / Link / 拓扑层级的概念定义 |
| [docs/zh/comm_op_dev_guide/ccu_comm_op_dev/query_topo.md](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/docs/zh/comm_op_dev_guide/ccu_comm_op_dev/query_topo.md) | 算子开发指南中的拓扑查询用法与代码示例 |

## 4. 核心概念与源码讲解

### 4.1 拓扑模型：分层、节点与链路

#### 4.1.1 概念说明

通信算子为什么需要拓扑查询？官方文档给出了两个理由：

1. 算子控制面要为数据面创建 Channel，而"不同 rank 是否互连、通过哪些 Endpoint 互连"是建 Channel 的必备信息；
2. 不同集群连接关系不同，算法性能与拓扑强相关，算子要感知拓扑才能选对算法。

因此 HCOMM 对通信域内 rank 间的连接关系做了建模，官方文档 [topology_model.md:14-30](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/docs/zh/comm_op_dev_guide/prog_models_concepts/topology_model.md#L14-L30) 用"图"的 语言定义了五个概念：

- **Node**：图中的节点，分两类——通信实体（rank）和 **Fabric**（交换机/路由网络的抽象，Fabric 只能与通信实体相连，且挂到同一个 Fabric 下的实体两两互通）；
- **Endpoint**：Node 的逻辑端口，一个 Node 可有多个 Endpoint（比如一张卡既有 HCCS 端口又有 RoCE 网口）；
- **Edge**：连接两个 Endpoint 的边；
- **Link**：两个通信实体之间可建链的信息（含两端 Endpoint 描述符与协议）；
- **拓扑层级**：如 Layer 0（服务器内）+ Layer 1（跨服务器），每层有自己的拓扑类型（Fullmesh、CLOS 等）。

#### 4.1.2 核心流程

一次拓扑查询的数据流：

```text
物理世界                     控制面数据结构                    查询接口
-----------                 ----------------                ----------
rankTable（rank 列表）  →   RankGraphV1::Init()         →   HcclRankGraphGetLayers
phy_topo（物理拓扑）        构建 netLayer_ / rankList_       HcclRankGraphGetRanksByLayer
serverToRank_ 等映射        rankSizeList_ / rankIndex_       HcclRankGraphGetTopoTypeByLayer
                           （详见 4.4）                     HcclRankGraphGetLinks
```

层级编号在基类中用常量固定：[rank_graph_base.h:25-27](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_graph/rank_graph_base.h#L25-L27) 定义 `HCCL_NETLAYER_0/1/2`，最多三层。层级的实际含义与芯片类型相关（详见 4.4.2 的 `InitNetLayer`）：

- Layer 0：服务器内（所有芯片都有）；
- Layer 1：910B/910 上是"跨服务器"，910_93 上是"超节点内"；
- Layer 2：仅 910_93 存在，表示"跨超节点"。

#### 4.1.3 源码精读

拓扑类型枚举定义在对外头文件中：

[include/hccl/hccl_rank_graph.h:L38-L46](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hccl/hccl_rank_graph.h#L38-L46) —— `CommTopo` 枚举：CLOS = 0、1DMesh = 1、910_93 拓扑 = 2、310P 拓扑 = 3、A2 AX Server = 4、自定义 = 5。

[include/hccl/hccl_rank_graph.h:L52-L56](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hccl/hccl_rank_graph.h#L52-L56) —— `HcclHeterogMode` 枚举：描述通信域是同构组网（单一芯片）还是 A2/A3 芯片混合的异构组网，供算法层区分对待。

`RankGraph` 基类把"一张可查询的 rank 图"抽象为一组纯虚接口：

[rank_graph_base.h:L23-L53](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_graph/rank_graph_base.h#L23-L53) —— `RankGraph` 类：声明 `GetLinks`、`GetNetLayers`、`GetInstTopoTypeByNetLayer`、`GetInstSizeByNetLayer`、`GetInstRanksByNetLayer`、`GetInstSizeListByNetLayer`、`GetEndpointInfo` 等纯虚函数，默认实现返回 `HCCL_E_NOT_SUPPORT`。子类只需实现自己支持的查询。

> **阅读提示**：`rank_graph/` 目录下还有 `phy_topo`、`phy_topo_builder`、`rank_graph_builder`、`rank_tableinfo` 等子目录，分别负责物理拓扑采集、物理拓扑到 rank 图的转换等，本讲聚焦查询路径，构建器留作延伸阅读。

#### 4.1.4 代码实践

**实践目标**：从官方概念文档出发，建立"层级"直觉。

**操作步骤**：

1. 阅读 [topology_model.md](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/docs/zh/comm_op_dev_guide/prog_models_concepts/topology_model.md)（很短，一页读完），对照其中的示意图理解 Layer 0 / Layer 1。
2. 打开 [rank_graph_base.h:L25-L27](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_graph/rank_graph_base.h#L25-L27)，记住三层编号上限。

**观察现象与预期结果**：能不假思索地回答——"一个 4 机 × 8 卡的 910B 集群，rank 图有几个层级？各层是什么含义？"（答案：2 层，Layer 0 = 机内 8 卡，Layer 1 = 跨机 32 卡互联。）

#### 4.1.5 小练习与答案

**练习 1**：Fabric 节点必须满足什么条件才能被抽象出来？

**答案**：与它相连的通信实体两两之间都可以通过它互通（topology_model.md 第 25 行）；且 Fabric 只能与通信实体相连。

**练习 2**：为什么 Endpoint 不直接挂在 Node 上算作一个，而要允许一个 Node 有多个 Endpoint？

**答案**：一张卡（一个 rank 对应的通信实体）通常同时具备多种物理端口——HCCS 片间总线端口、RoCE 网口、PCIe 端口等。Link 建立在特定 Endpoint 对之间，多 Endpoint 才能表达"同一对 rank 之间有多条不同协议的链路"。

---

### 4.2 对外 C 接口族：hccl_rank_graph.h

#### 4.2.1 概念说明

`include/hccl/hccl_rank_graph.h` 是面向算子开发者的拓扑查询入口（u1-l3 中提到的 L2 层头文件之一）。它把 4.1 的抽象模型暴露为一组 C 函数，全部以 `HcclComm` 句柄为第一参数。接口可以按"查什么"分成四组：

| 分组 | 接口 | 回答的问题 |
| --- | --- | --- |
| 层级总览 | `HcclRankGraphGetLayers` | 当前通信域有哪些层级？ |
| 按层查 rank | `HcclRankGraphGetRanksByLayer` / `GetRankSizeByLayer` / `GetInstSizeListByLayer` | 本 rank 在某层的拓扑实例里有哪些 rank / 多少个？该层共分几个实例、各多大？ |
| 按层查形态 | `HcclRankGraphGetTopoTypeByLayer` | 某层是什么拓扑类型（1DMesh/CLOS…）？ |
| 按秩对查链路 | `HcclRankGraphGetLinks` | 两个 rank 之间有哪些可用链路、走什么协议？ |

另有一批标注 `WARNING: experimental API` 的接口（`GetTopoInstsByLayer`、`GetTopoType`、`GetRanksByTopoInst`、`GetEndpointNum/Desc/Info`）和 `HcclGetHeterogMode`，不保证兼容性，使用前需评估。

#### 4.2.2 核心流程

典型调用序列（以"打印整个分层拓扑"为例）：

```text
1. HcclRankGraphGetLayers(comm, &netLayers, &layerNum)
       → 拿到层级编号数组，例如 [0, 1]
2. 对每个 layer：
   a. HcclRankGraphGetTopoTypeByLayer(comm, layer, &topoType)   → 该层形态
   b. HcclRankGraphGetRanksByLayer(comm, layer, &ranks, &rankNum) → 该层可达 rank 列表
   c. HcclRankGraphGetInstSizeListByLayer(comm, layer, &sizes, &num) → 该层实例划分
3. （可选）HcclRankGraphGetLinks(comm, layer, srcRank, dstRank, &links, &linkNum)
       → 查某对 rank 的链路详情
```

所有"返回列表"的接口都遵守同一约定（重要约束）：

- 返回的指针指向**库内管理的内存**，调用者严禁 `free`；
- 同一通信域**重复调用可能使前次结果失效**，应及时把数据复制到自己的缓冲区。

这本质上是一个"借用（borrow）而非转移所有权"的出参设计，避免了接口层频繁的内存分配与跨语言释放问题。

#### 4.2.3 源码精读

层级总览接口及其注释中的示例：

[include/hccl/hccl_rank_graph.h:L115-L132](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hccl/hccl_rank_graph.h#L115-L132) —— `HcclRankGraphGetLayers`：注释给出了两级拓扑的期望输出（`netLayers = [0,1]`），并明确标注了内存借用约束。

按层查 rank 的接口语义最容易被误解，头文件注释用了一个 32 卡的例子讲透：

[include/hccl/hccl_rank_graph.h:L134-L167](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hccl/hccl_rank_graph.h#L134-L167) —— `HcclRankGraphGetRanksByLayer`：4 server × 8 卡场景下，rank9 查 Layer 0 得 `[8..15]`（同机 8 卡），查 Layer 1 得全部 32 个 rank。注释特别强调：**该接口只反映组网连通性、不反映算法分组**——Layer 1 查的是"该层可连通的范围"，所以是 32 张卡而不是 4 张"对端卡"。

[include/hccl/hccl_rank_graph.h:L169-L187](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hccl/hccl_rank_graph.h#L169-L187) —— `HcclRankGraphGetRankSizeByLayer`：只返回数量的轻量版本，注释说明动机是超大规模集群下返回列表开销大。

[include/hccl/hccl_rank_graph.h:L206-L228](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hccl/hccl_rank_graph.h#L206-L228) —— `HcclRankGraphGetInstSizeListByLayer`：查询该层分为多少个拓扑实例、每个实例多大（例：Layer 0 → `[8,8,8,8]`，Layer 1 → `[32]`）。

链路查询与 `CommLink` 结构体（注意其 ABI 头）：

[include/hccl/hccl_rank_graph.h:L58-L76](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hccl/hccl_rank_graph.h#L58-L76) —— `COMM_LINK_MAGIC_WORD`/`COMM_LINK_VERSION` 常量与 `CommLink` 结构体：开头是 `CommAbiHeader`（版本 + 魔数 + 大小），随后是源/目的 `EndpointDesc`，尾部是 128 字节的属性 union（当前含 `linkProtocol` 与 `hop`）。注释说明：尾部非固定区扩展时 `COMM_LINK_VERSION` 加 1——与 u2-l3 的 `HcclCommConfig` 同款"尾部追加 + 版本协商"手法。

[include/hccl/hccl_rank_graph.h:L85-L112](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hccl/hccl_rank_graph.h#L85-L112) —— 内联函数 `CommLinkInit`：先把整个结构体用 `0xFF` 填充（哨兵值），再写 ABI 头与关键字段。调用方拿到 `CommLink` 后应先检查 `header.magicWord` 与 `header.version` 再解释尾部 union。

[include/hccl/hccl_rank_graph.h:L230-L244](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hccl/hccl_rank_graph.h#L230-L244) —— `HcclRankGraphGetLinks`：给定层级与源/目的 rank，返回两者之间所有可用链路。

> **阅读提示（注释与枚举不一致）**：`HcclRankGraphGetTopoTypeByLayer` 的注释示例（L200-L201）写"netLayer=1 时 topoType=2 (clos)"，但按 L38-L46 的枚举定义 `COMM_TOPO_CLOS = 0`、`COMM_TOPO_1DMESH = 1`，而 `HcclRankGraphGetTopoType` 的示例（L272-L273）则写 layer1 → 0 (clos)，两处注释互相矛盾。以枚举定义和 4.4.3 的实现代码为准：910B 的 Layer 0 返回 1DMESH(1)，Layer 1 返回 CLOS(0)。这也是读源码时"注释仅供参考、代码才是事实"的一个典型例子。

官方使用示例（可直接抄的调用样板）见 [docs/zh/comm_op_dev_guide/ccu_comm_op_dev/query_topo.md:L26-L73](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/docs/zh/comm_op_dev_guide/ccu_comm_op_dev/query_topo.md#L26-L73)：包含 `HcclGetRankId`、`HcclGetRankSize`、`HcclRankGraphGetLinks` 枚举 HCCS 链路、以及用 `netLayersNum == 3` 判断超节点场景的写法。

#### 4.2.4 代码实践

**实践目标**：熟悉头文件中"借用式出参"的调用姿势。

**操作步骤**：

1. 阅读 [hccl_rank_graph.h:L115-L167](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hccl/hccl_rank_graph.h#L115-L167) 中两条接口的全部注释（含示例）。
2. 阅读单元测试 [test/ut/framework/next/ut_HcclRankGraph_API.cc](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/test/ut/framework/next/ut_HcclRankGraph_API.cc)，观察测试代码如何构造 comm、调用接口并断言返回值。

**观察现象与预期结果**：注意测试与示例中对返回指针"只读、不释放、用完即复制"的处理方式；如果你在示例代码里看到对返回数组做了 `memcpy` 到本地 `std::vector`，那就是正确姿势。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `HcclRankGraphGetRankSizeByLayer` 要单独存在，而不是让用户调 `GetRanksByLayer` 后自己取数量？

**答案**：接口注释（L184-L185）说明：超大规模集群下返回完整 rank 列表消耗较多时间和内存，只需要数量时应有轻量路径。

**练习 2**：拿到 `HcclRankGraphGetLinks` 返回的 `CommLink*` 后，第一步应该做什么？

**答案**：先校验 ABI 头——`header.magicWord == COMM_LINK_MAGIC_WORD(0x0f0e0f0f)` 且 `header.version` 是自己能理解的版本，再解释 `srcEndpointDesc`/`dstEndpointDesc`/`linkAttr`；因为尾部 union 是版本化的，盲目解释高版本结构可能读到未知字段。

**练习 3**：8 机 64 卡（每机 8 卡）的 910_93 超节点集群内，若 4 机组成一个超节点，`HcclRankGraphGetLayers` 大概率返回几个层级？

**答案**：3 个——Layer 0（机内）、Layer 1（超节点内 32 卡，910_93 特有语义）、Layer 2（跨超节点，仅当 `superPodToRank_.size() > 1` 时才加入，见 4.4.3 的 `InitNetLayer`）。若只有一个超节点则 Layer 2 不存在，返回 `[0, 1]`。

---

### 4.3 适配层：从 HcclComm 到 RankGraph

#### 4.3.1 概念说明

u2-l1 讲过 `api_c_adpt` 是"C 接口一步进入 C++ 实现"的新式适配层。拓扑查询接口的适配层在 `coll_comm_rank_graph_a_adpt.cc`，它做三件事：

1. 入参判空（`CHK_PTR_NULL`）；
2. 把 `HcclComm`（C 句柄）还原为 `hcclComm*`，取出其 `CollComm`，再取出 `CollComm` 持有的 `RankGraph*`；
3. 用 `HCCLV2_FUNC_RUN` 宏做运行时分派——新架构（V2）走 `RankGraphV2`/`CollComm` 路径并直接返回，老流程（V1）落到后面的 legacy 调用。

这延续了 u2-l1 中"V2/V1 双轨"的架构：同一套 C 接口同时服务 A5 新架构与 A2/A3 老芯片。

#### 4.3.2 核心流程

以 `HcclRankGraphGetLayers` 为例：

```text
HcclRankGraphGetLayers(comm, ...)            (C 接口, coll_comm_rank_graph_a_adpt.cc)
  ├─ CHK_PTR_NULL 参数校验
  ├─ HCCLV2_FUNC_RUN(lambda)                 运行时探测 V2 支持
  │    ├─ GetRankGraphFromComm(comm, &rankGraph)
  │    │     comm → hcclComm* → GetCollComm() → collComm->GetRankGraph()
  │    └─ rankGraph->GetNetLayers(...)       RankGraph 基类虚接口
  │         (V2 命中则到此返回)
  └─ hcclComm->GetNetLayers(...)             V1 老流程兜底路径
```

#### 4.3.3 源码精读

[api_c_adpt/coll_comm_rank_graph_a_adpt.cc:L51-L61](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/api_c_adpt/coll_comm_rank_graph_a_adpt.cc#L51-L61) —— 静态辅助函数 `GetRankGraphFromComm`：三步取指针（comm → CollComm → RankGraph），每步判空。这就是 u2-l2 中"`CollComm` 聚合了 RankGraph 资源成员"在接口层落地的位置。

[api_c_adpt/coll_comm_rank_graph_a_adpt.cc:L98-L120](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/api_c_adpt/coll_comm_rank_graph_a_adpt.cc#L98-L120) —— `HcclRankGraphGetLayers` 完整实现：V2 分支在 lambda 内完成查询并记录 `HCCL_INFO` 日志；若 V2 未启用，落入 `hcclComm->GetNetLayers(...)` 老路径。注意 V2 分支成功后宏会使函数直接返回，两条路径互斥。

[api_c_adpt/coll_comm_rank_graph_a_adpt.cc:L63-L96](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/api_c_adpt/coll_comm_rank_graph_a_adpt.cc#L63-L96) —— `HcclRankGraphGetLinks`：除常规分派外，还先做业务校验（`srcRank == dstRank` 返回 `HCCL_E_PARA`），V2 路径调用 `rankGraph->GetLinks(...)`。

[api_c_adpt/coll_comm_rank_graph_a_adpt.cc:L214-L233](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/api_c_adpt/coll_comm_rank_graph_a_adpt.cc#L214-L233) —— 实验性接口 `HcclRankGraphGetTopoInstsByLayer`：V2 分支里把基类指针 `static_cast` 为 `RankGraphV2*` 再调用——因为 `GetTopoInstsByLayer` 等实验接口只存在于 V2，不在基类虚表里。这是判断"某接口是否 V2 专属"的快速视觉信号。

[rank_graph_v2.h:L16-L47](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_graph/rank_graph_v2.h#L16-L47) —— `RankGraphV2` 类：实现全部基类虚接口并额外提供实验接口，内部通过 `std::unique_ptr<Hccl::IRankGraph> pImpl` 委托给独立实现（pimpl 手法，隔离接口与实现）。

#### 4.3.4 代码实践

**实践目标**：亲手追踪一条 C 接口到实现的完整链路。

**操作步骤**：

1. 从 [coll_comm_rank_graph_a_adpt.cc:L167-L188](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/api_c_adpt/coll_comm_rank_graph_a_adpt.cc#L167-L188)（`HcclRankGraphGetRanksByLayer`）出发。
2. 找到 V2 分支调用的 `rankGraph->GetInstRanksByNetLayer(...)`（下一节 4.4.3）。
3. 画出时序图：`HcclRankGraphGetRanksByLayer` → `GetRankGraphFromComm` → `RankGraph::GetInstRanksByNetLayer`（虚函数）→ `RankGraphV1::GetInstRanksByNetLayer`。

**观察现象与预期结果**：图中每一跳都能标注出所在文件与行号；能指出 V1 分支（`hcclComm->GetInstRanksByNetLayer`）落在 legacy 实现里，本仓库内未展开（属 legacy 老流程）。

#### 4.3.5 小练习与答案

**练习 1**：`HCCLV2_FUNC_RUN` 宏在这里起什么作用？为什么每个接口都要写一遍"V2 lambda + V1 兜底"两段代码？

**答案**：它运行时探测当前通信域是否走 V2 新架构（回顾 u2-l1 的 `hrtGetHcclV2Support` 机制），命中则执行 lambda 并直接返回。两段并存是因为同一 C 接口要同时服务新架构（A5）与老芯片（A2/A3），两套实现的内部对象模型不同，只能在适配层分叉。

**练习 2**：为什么 `HcclRankGraphGetEndpointInfo`（L308-L332）的兜底路径调用的是 `rankGraph->GetEndpointInfo(...)` 而不是 `hcclComm->...`？

**答案**：`GetEndpointInfo` 是 `RankGraph` 基类的纯虚函数（rank_graph_base.h L50-L52），V1 实现同样支持该查询，所以兜底可直接复用基类接口，无需 legacy 侧另建一套。

---

### 4.4 RankGraphV1：分层信息的构建与查询实现

#### 4.4.1 概念说明

`RankGraphV1` 是面向 A2/A3 芯片（910B、910_93、310P 等）的 rank 图实现。它的核心思路是：**初始化时把 rankTable + 拓扑属性加工成几张查找表，查询时只做表查找**。关键成员（见 [rank_graph.h:L85-L99](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_graph/rank_graph.h#L85-L99)）：

| 成员 | 类型 | 含义 |
| --- | --- | --- |
| `rankIndex_` | `unordered_map<rankId, RankGraphInfo>` | rankId → rank 信息 + 该 rank 的所有 Endpoint 描述符 |
| `netLayer_` | `vector<uint32_t>` | 存在的层级编号列表（`GetLayers` 的直接数据源） |
| `rankList_` | `map<level, vector<rankId>>` | 每层的 rank 列表（`GetRanksByLayer` 数据源） |
| `rankSizeList_` | `map<level, vector<rankId>>` | 每层各拓扑实例的大小列表（`GetInstSizeListByLayer` 数据源） |
| `rankPairInfo_` | `map<(layer,src,dst), vector<CommLink>>` | 链路查询缓存 |
| `serverToRank_` / `superPodToRank_` | `map<idx, vector<RankInfo>>` | 服务器/超节点 → rank 的物理分组 |

`RankGraphInfo`（[rank_graph.h:L25-L28](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_graph/rank_graph.h#L25-L28)）内嵌 `RankInfo_t` 和一个 `EndpointDesc` 向量——一个 rank 的多协议端口都展开在这里。

#### 4.4.2 核心流程

**初始化**（`Init`，rank_graph.cc L113-L134）：

```text
Init(rankTable, topoAttr)
  ├─ DevTypeToCommProtocol()      本芯片类型 → 默认网络协议（RoCE/PCIE/...）
  ├─ 逐 rank BuildRankGraphInfo()  rankInfo + IP 列表 → RoCE/HCCS/PCIE 多套 EndpointDesc
  │                                 填入 rankIndex_
  ├─ InitRankInfo()                定位本 rank（rankData_），
  │    ├─ InitServerRankInfo()     建 serverToRank_（server 内按 userRank 排序）
  │    ├─ InitSuperPodRankInfo()   建 superPodToRank_
  │    └─ InitGraphRankInfo()      生成下发 device 侧用的 GraphRankInfo 平表
  ├─ InitNetLayer()                决定层级数量并填充 rankList_ / rankSizeList_
  └─ InitHeterogMode()             收集芯片类型集合 → 同构 / A2+A3 混合 / 报错
```

**层级判定规则**（`InitNetLayer`，伪代码）：

```text
netLayer_ = [L0]                                  # 恒有 L0（机内）
if 服务器数 > 1:
    netLayer_ += [L1]
    910B/910:  L1 的 rankList = 全部 rank（跨机全互联）, sizeList = [总rank数]
    910_93:    L1 的 rankList = 本超节点内的 rank,     sizeList = 各超节点大小
if 910_93 且 超节点数 > 1:
    netLayer_ += [L2]                             # 跨超节点
```

**链路查询**（`GetLinks`）：

```text
GetLinks(netLayer, srcRank, dstRank)
  ├─ 校验两 rank 存在、netLayer ≤ 2
  ├─ GetCommProtocolFromRankInfo()   推断该层两 rank 间的协议
  │    ├─ 同机: L0 → 查询驱动链路类型（HCCS/SIO/PCIE）
  │    │        L1 → 910_93 超节点内为 HCCS；特定条件下 RoCE
  │    └─ 跨机: L1 → 按芯片类型给 RoCE/PCIE/HCCS
  │             L2 → 跨超节点一律 RoCE
  ├─ 协议为 RESERVED → 返回空列表（不连通）
  └─ 查 rankPairInfo_ 缓存，未命中则对 src/dst 的 Endpoint 笛卡尔积
     过滤协议不匹配的组合，逐条 CommLinkInit 后存入缓存
```

#### 4.4.3 源码精读

初始化主流程：

[rank_graph.cc:L113-L134](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_graph/rank_graph.cc#L113-L134) —— `RankGraphV1::Init`：拷贝 rankTable/topoAttr、清空缓存，然后按"协议判定 → 建 rankIndex_ → InitRankInfo → InitNetLayer → InitHeterogMode"顺序装配。

[rank_graph.cc:L28-L54](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_graph/rank_graph.cc#L28-L54) —— `DevTypeToCommProtocol`：芯片类型映射默认网络协议——910 系列走 RoCE，310P/NOSOC 走 PCIE，950/960 暂为 RESERVED（UB 协议待扩展）。

[rank_graph.cc:L56-L111](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_graph/rank_graph.cc#L56-L111) —— `BuildRankGraphInfo`：对 rank 的每个网卡 IP 生成一个 RoCE `EndpointDesc`（IPv4/IPv6 地址、host/device 部署位置、物理 ID 等定位字段），并按芯片类型追加 HCCS、PCIE 协议的"ID 型"端点副本。这就是"一个 Node 多个 Endpoint"的代码落地。

层级构建（本讲最重要的函数）：

[rank_graph.cc:L1008-L1076](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_graph/rank_graph.cc#L1008-L1076) —— `InitNetLayer`：L0 恒存在且 rankList 取本机 rank；`serverToRank_.size() > 1` 时追加 L1，其中 910B/910 的 L1 rankList 展开为全部 rank、sizeList 记为 `[总rank数]`，910_93 的 L1 则以超节点为界；910_93 且多超节点时再追加 L2。这解释了 4.2 中"Layer 1 返回 32 张卡"的语义来源——V1 里 L1 的 rankList 本身就是"该层可连通范围"。

查询实现（薄封装，全部是查表）：

[rank_graph.cc:L430-L439](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_graph/rank_graph.cc#L430-L439) —— `GetNetLayers`：直接返回 `netLayer_.data()` 与大小。这也解释了 4.2.2 的内存借用约束——返回的是成员 `vector` 的内部缓冲，任何触发扩容/重填的操作都会使其失效。

[rank_graph.cc:L485-L500](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_graph/rank_graph.cc#L485-L500) —— `GetInstRanksByNetLayer`（对应 C 接口 `HcclRankGraphGetRanksByLayer`）：校验 `netLayer < netLayer_.size()` 后返回 `rankList_[netLayer]` 的数据指针。

[rank_graph.cc:L441-L467](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_graph/rank_graph.cc#L441-L467) —— `GetInstTopoTypeByNetLayer`（对应 `HcclRankGraphGetTopoTypeByLayer`）：按芯片类型与层级静态给拓扑类型——910_93 的 L0 → `COMM_TOPO_910_93`、L1/L2 → CLOS；910B/910 的 L0 → 1DMESH、L1 → CLOS；310P3 的 L0 → `COMM_TOPO_310P`。

链路查询与缓存：

[rank_graph.cc:L310-L383](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_graph/rank_graph.cc#L310-L383) —— `GetLinks`（对应 `HcclRankGraphGetLinks`）：先用 `GetCommProtocolFromRankInfo` 判协议，RESERVED 则返回空；否则以 `(netLayer, srcRank, dstRank)` 三元组查 `rankPairInfo_` 缓存，未命中时对两端 Endpoint 做笛卡尔积、用 `NeedIgnoreEndPoints` 过滤（如 HCCS-HCCS 但实际链路是 SIO 的特例要保留），逐条 `CommLinkInit` 后入缓存，最后返回 `links.data()`。

[rank_graph.cc:L218-L263](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_graph/rank_graph.cc#L218-L263) —— `GetCommProtocolFromRankInfo`：协议推断的决策树——先要求两端芯片类型一致，再按"同机/跨机 × 层级"分派；跨超节点（L2）固定返回 RoCE。这个函数是"物理拓扑 → 可建链协议"翻译的核心。

[rank_graph.cc:L385-L422](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_graph/rank_graph.cc#L385-L422) —— `InitHeterogMode`：把通信域内所有芯片类型收进 `std::set`——只有 1 种为同构；恰好是 {910B, 910_93} 两种为 A2/A3 混合；其余组合报 `HCCL_E_INTERNAL`。

#### 4.4.4 代码实践

**实践目标**：验证"查询即查表"的论断，并理解一条隐式依赖。

**操作步骤**：

1. 对照 4.4.2 的伪代码通读 [InitNetLayer](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_graph/rank_graph.cc#L1008-L1076)，在纸上为一个"2 超节点 × 2 机 × 8 卡的 910_93 集群（共 32 rank）"手工推演 `netLayer_`、`rankList_`、`rankSizeList_` 三个容器的内容。
2. 思考：若不调用 `InitRankInfo`（即 `rankData_` 未定位），`InitNetLayer` 中哪一行会失败？

**观察现象与预期结果**：

1. 推演结果应为：`netLayer_ = [0,1,2]`；`rankList_[0]` = 本机 8 个 rank，`rankList_[1]` = 本超节点 16 个 rank，`rankList_[2]` = 全部 32 个 rank；`rankSizeList_[0] = [8,8,8,8]`，`rankSizeList_[1] = [16,16]`，`rankSizeList_[2] = [32]`。（待本地验证——以实际集群配置为准。）
2. `InitNetLayer` 第 1013-1018 行用 `rankData_.serverIdx` 查 `serverToRank_`，若 `rankData_` 未定位则查表失败返回 `HCCL_E_INTERNAL`——这就是初始化顺序上 `InitRankInfo` 必须先于 `InitNetLayer` 的原因。

#### 4.4.5 小练习与答案

**练习 1**：`HcclRankGraphGetRanksByLayer` 为什么重复调用会使前次结果失效？从 V1 实现给出解释。

**答案**：实现返回的是成员 `std::vector` 的 `data()`（rank_graph.cc L497）。任何导致该 vector 重新分配或重新填充的后续操作（如再次 Init）都会让旧指针悬空；所以头文件要求"及时复制"。

**练习 2**：`GetLinks` 为什么要维护 `rankPairInfo_` 缓存？

**答案**：链路结果由两端 Endpoint 笛卡尔积 + 协议过滤计算得出（L347-L364），代价高于普通查表；而拓扑在通信域生命周期内不变，按 `(netLayer, srcRank, dstRank)` 缓存一次即可让后续查询 O(1) 命中。

**练习 3**：`NeedIgnoreEndPoints` 里为什么"两个 HCCS 端点 + SIO 链路"要特殊放行？

**答案**：注释（L271-L273）说明两个 HCCS endpoint 之间可能实际是 SIO 链路（910_93 带 SIO 的场景），同理 A+X 两个 mesh 间是 PCIE、310DUO 两 DIE 间是 HCCS——协议匹配不能只看端点声明的协议，还要结合链路探测结果，因此对这类组合显式返回"不忽略"。

## 5. 综合实践

**任务**：在 u1-l4 跑通的 AllReduce 示例基础上，扩展出一个"拓扑打印器"——调用本讲接口把当前通信域的分层拓扑完整打印出来，并据此画出拓扑示意图。

以下为示例代码（非项目原有代码，基于 `examples/01_communicators/01_one_device_per_process/main.cc` 的通信域初始化部分裁剪，只保留拓扑查询相关逻辑）：

```c
// 示例代码：拓扑打印器（在 HcclCommInitRootInfoConfig 成功、comm 有效之后调用）
void PrintTopology(HcclComm comm)
{
    // 第 1 步：有哪些层级
    uint32_t *netLayers = nullptr;
    uint32_t layerNum = 0;
    HCCLCHECK(HcclRankGraphGetLayers(comm, &netLayers, &layerNum));
    printf("layers: num=%u ->", layerNum);
    std::vector<uint32_t> layers(netLayers, netLayers + layerNum); // 立即复制（借用式出参）
    for (auto l : layers) printf(" %u", l);
    printf("\n");

    // 第 2 步：逐层打印 形态 / 实例划分 / 本 rank 可达列表
    for (auto layer : layers) {
        CommTopo topo = COMM_TOPO_RESERVED;
        HCCLCHECK(HcclRankGraphGetTopoTypeByLayer(comm, layer, &topo));

        uint32_t *sizes = nullptr, sizeNum = 0;
        HCCLCHECK(HcclRankGraphGetInstSizeListByLayer(comm, layer, &sizes, &sizeNum));

        uint32_t *ranks = nullptr, rankNum = 0;
        HCCLCHECK(HcclRankGraphGetRanksByLayer(comm, layer, &ranks, &rankNum));

        printf("layer %u: topo=%d insts=[", layer, (int)topo);
        for (uint32_t i = 0; i < sizeNum; i++) printf("%u ", sizes[i]);
        printf("] reachableRanks=[");
        for (uint32_t i = 0; i < rankNum; i++) printf("%u ", ranks[i]);
        printf("]\n");
    }

    // 第 3 步（可选）：任选一对 rank 查链路，观察协议
    // CommLink *links = nullptr; uint32_t linkNum = 0;
    // HCCLCHECK(HcclRankGraphGetLinks(comm, 0, 1, 0, &links, &linkNum));
    // 注意：links/netLayers/ranks/sizes 均为库内内存，严禁 free
}
```

**操作步骤**：

1. 参考 [examples/README.md](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/examples/README.md) 与 u1-l4，复制 `01_one_device_per_process` 示例，在 `HcclCommInitRootInfoConfig` 成功后插入上述 `PrintTopology` 调用（头文件 `#include "hccl/hccl_rank_graph.h"`）。
2. 用 `examples/build.sh` 编译并在昇腾环境运行（单机 8 卡即可）。
3. 根据输出在纸上画图：每个拓扑实例画一个方框（框内列出 ranks），Layer 0 的几个方框再用 Layer 1 的可达关系连接。

**需要观察的现象与预期结果**：

- 单机 8 卡 910B 环境：`layerNum=1`（只有 Layer 0），`topo=1`（1DMESH），`insts=[8]`，`reachableRanks=[0..7]`。
- 多机环境：应观察到 Layer 1 出现，`topo=0`（CLOS），`insts=[32]`，`reachableRanks` 为全部 rank——对照 4.2.3 头文件注释中 32 卡示例逐项印证。
- 若无昇腾硬件：本实践**待本地验证**；可退化为阅读 [test/ut/framework/next/ut_HcclRankGraph_API.cc](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/test/ut/framework/next/ut_HcclRankGraph_API.cc) 中的断言，从断言期望值反推各接口在 mock 拓扑下的返回，同样能完成"画拓扑图"的任务。

## 6. 本讲小结

- HCOMM 用"分层图"建模集群拓扑：Node（rank + Fabric）/ Endpoint / Edge / Link 是图的基本要素，netLayer（L0 机内、L1 跨机或超节点内、L2 跨超节点）叠加在图之上，每层有自己的 `CommTopo` 形态。
- `include/hccl/hccl_rank_graph.h` 提供"查层级 / 按层查 rank / 按层查形态 / 按秩对查链路"四组 C 接口；所有返回列表都是**库内管理的借用式出参**——只读、不释放、及时复制。
- `CommLink` 沿用"ABI 头（魔数+版本+大小）+ 尾部 union 扩展"的兼容设计，解读前必须先校验 `magicWord` 与 `version`；头文件个别注释示例与枚举值不一致，以代码为准。
- 调用链为 `HcclRankGraph*` C 接口 → `coll_comm_rank_graph_a_adpt.cc`（`HCCLV2_FUNC_RUN` 分派，经 `CollComm` 取出 `RankGraph`）→ `RankGraphV1`（A2/A3）或 `RankGraphV2`（A5，pimpl）。
- `RankGraphV1` 的策略是"初始化时建表、查询时查表"：`InitNetLayer` 决定层级并填充 `rankList_`/`rankSizeList_`；`GetLinks` 先推断协议（同机查驱动链路类型、跨机按芯片、跨超节点恒 RoCE），再对 Endpoint 笛卡尔积过滤并缓存到 `rankPairInfo_`。
- 拓扑查询接口是通信算子开发的基石：CCU/AIV/AICPU 三套算子开发指南的"查询拓扑信息"章节（`query_topo.md`）用的正是这一族接口。

## 7. 下一步学习建议

- 下一讲 u2-l5 将讲解 `rank_info_detect` 模块——本讲中 `RankGraphV1::Init` 消费的 `RankTable_t`（每个 rank 的 serverIdx、superPodId、deviceIp 等字段）正是由 rank 信息探测阶段在各进程间交换汇聚而来的，两讲首尾衔接。
- 延伸阅读：`src/coll_communicator_mgr/rank_graph/` 下的 `phy_topo`、`phy_topo_builder`、`rank_graph_builder` 子目录，看物理拓扑如何被采集并转换为 rank 图；以及 [rank_graph_v2.cc](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_graph/rank_graph_v2.cc) 对比 V2 实现差异。
- 结合 u5 单元预读 [docs/zh/comm_op_dev_guide/ccu_comm_op_dev/query_topo.md](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/docs/zh/comm_op_dev_guide/ccu_comm_op_dev/query_topo.md)，思考"按拓扑选算法"（如单级全互联 vs 分级算法）如何利用本讲的查询结果。

# 拓扑适配与拓扑信息 Topo

## 1. 本讲目标

本讲展开 op_common 的第四个组件——**拓扑适配 topo**。学完本讲，你应该能够：

- 说清 `TopoInfo` 与 `TopoInfoWithNetLayerDetails` 两大数据结构各自的字段含义，以及 `topoLevelNums` / `level0Topo` / `netLayerDetails` 分别描述了集群网络的哪一层信息。
- 掌握 `TopoMatchBase` 抽象与 `MatchTopo` 纯虚接口，认识 `topo_match_1d` / `topo_match_multilevel` / `topo_match_ubx` 等「拓扑匹配器家族」。
- 读懂 `TopoMatchMultilevel` 的 `MatchTopo` 编排逻辑与 `TopoForLayer0/1/2` 三步切分，理解它如何为一个 Mesh+NHR 的两级算法产出两级子通信域描述 `AlgHierarchyInfoForAllLevel`。
- 理清本轮新增的 `hostDpuOnly` 字段与 `CalcHostDPUOnly` 判定函数：HCCL 如何识别「框间只有 Host DPU 可达」的专用拓扑，以及这一标志如何反过来影响引擎选择与拓扑退化。

---

## 2. 前置知识

在进入本讲前，请确认你已经掌握下列来自前置讲义的概念：

- **op_common 四大组件与数据流**（u3-l1）：selector 选算法、executor 编排执行、template 搬数据、topo 适配拓扑；前三者每算子私有，**topo 属通信域控制面基础设施、全体算子共享**（位于 `src/ops/op_common/topo`）。整条链路由 `algName` 字符串契约串联。
- **Selector 的产出**（u3-l2）：`Selector()` 产出一个 `algName` 字符串（如 `CcuMSAllReduceSoleMesh`、`AicpuAllReduceSequenceMeshNHR`），作为 executor 注册表的键。algName 末尾的 `Mesh` / `NHR` / `MeshNHR` 等就是**拓扑形态**，它决定了该算法需要哪种拓扑匹配器。
- **RankGraph 分层模型**（u1-l2）：集群网络用 netLayer 分层描述——Server 内（Layer0）链路快、Server 间（Layer1）链路慢、超节点间（Layer2）更慢。这是分级通信与多级子通信域划分的物理依据。
- **α-β 耗时模型与分级通信**（u1-l2）：\( D = \alpha + n\beta + n\gamma \)，把 AllReduce 拆成「节点内 ReduceScatter → 节点间 AllReduce → 节点内 AllGather」，让大数据量搬移落在又快又宽的低层级链路上。

本讲要回答的核心问题是：**一个通信域里有成百上千个 rank，HCCL 怎么把它们按物理拓扑「切」成一层层的小通信域（子组），让算法可以分级编排？** 这正是 topo 子系统的工作。

---

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [src/ops/op_common/inc/alg_param.h](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/inc/alg_param.h) | 定义 `TopoInfo`、`TopoInfoWithNetLayerDetails`（含本轮新增的 `hostDpuOnly` 字段）、`NetLayerDetails`、`AlgHierarchyInfoForAllLevel` 等核心数据结构。 |
| [src/ops/op_common/topo/topo.h](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/topo/topo.h) / [topo.cc](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/topo/topo.cc) | 旧式（legacy）对称拓扑信息计算 `CalcGeneralTopoInfoForA2/A3/Comm`，以及子通信域 rank 换算工具 `GetUserRankBySubCommRank` / `GetSubCommRankByUserRank`。 |
| [src/ops/op_common/topo/topo_host.h](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/topo/topo_host.h) / [topo_host.cc](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/topo/topo_host.cc) | **控制面**：从 RankGraph 提取并填充 `TopoInfoWithNetLayerDetails`，包括 `InitRankInfo`、`CalcTopoShape`、`ExtractNetLayerDetails`、`ExtractTopoDetails`，以及**本轮新增的 `CalcHostDPUOnly`**（判定 Host DPU 专用拓扑并落盘到 `hostDpuOnly` 字段）。 |
| [src/ops/op_common/topo/topo_match_base.h](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/topo/topo_match_base.h) / [topo_match_base.cc](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/topo/topo_match_base.cc) | 拓扑匹配器抽象基类 `TopoMatchBase`，声明纯虚 `MatchTopo`。 |
| [src/ops/op_common/topo/topo_match_multilevel.h](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/topo/topo_match_multilevel.h) / [topo_match_multilevel.cc](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/topo/topo_match_multilevel.cc) | **本讲主角**：多级匹配器，用 `TopoForLayer0/1/2` 把拓扑切成 2~3 级子通信域，用于 Mesh+NHR 这类分级算法；本轮在 `MatchTopo` 中新增了「hostDPUOnly + 两级 CLOS」的退化分支。 |
| [src/ops/op_common/topo/topo_match_1d.h](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/topo/topo_match_1d.h) / [topo_match_1d.cc](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/topo/topo_match_1d.cc) | 单级匹配器，把全体 rank 视为一个扁平 Mesh1D 组，用于 `SoleMesh` 类算法。 |

> 说明：`topo_match_ubx`、`topo_match_3_level`、`topo_match_concurrent`、`topo_match_pcie_mix`、`topo_match_squeeze_2d` 等同属匹配器家族，思路与本讲主角一致，本讲只在「家族总览」处点名，不逐个展开。

---

## 4. 核心概念与源码讲解

本讲按四个最小模块推进：先讲 topo 的**输入数据结构**（`TopoInfo` / `TopoInfoWithNetLayerDetails`），再讲匹配器的**统一抽象**（`TopoMatchBase` / `MatchTopo`），然后精读**多级匹配器**（`TopoMatchMultilevel`），最后解剖**本轮新增的 `CalcHostDPUOnly` 与 `hostDpuOnly`**。

### 4.1 模块一：TopoInfo 与 TopoInfoWithNetLayerDetails 数据结构

#### 4.1.1 概念说明

要理解 topo，先要分清两个层次的概念：

1. **物理拓扑（拓扑事实）**：集群里到底有多少台服务器、每台几张卡、卡和卡之间走什么协议、谁和谁直连。这些事实由 HCOMM 通信域管理（HCCM）层通过 RankGraph 暴露给 HCCL。HCCL 不能自己「看见」网线，只能向 RankGraph 查询。
2. **逻辑分层（拓扑理解）**：算法（如 Mesh+NHR）不关心具体接线，它只关心「**这一层有几个子组、每个子组里有哪些 rank**」。把物理拓扑翻译成算法能消费的多级子组描述，就是 topo 的职责。

对应到代码，这两个层次分别是：

- **`TopoInfo` / `TopoInfoWithNetLayerDetails`**：拓扑的**输入快照**——对本 rank 视角下集群物理拓扑的描述。其中 `TopoInfoWithNetLayerDetails` 是 `TopoInfo` 的派生，附带更丰富的 netLayer（网络层）明细，以及本轮新增的 `hostDpuOnly` 专用拓扑标志。它在 Selector 阶段被一次性算出，缓存复用。
- **`AlgHierarchyInfoForAllLevel`**：拓扑的**输出结论**——按算法层级切好的子通信域 rank 列表。它在 Executor 阶段由拓扑匹配器 `MatchTopo` 产出。

一句话区分：**`topoInfo` 回答「物理网络长什么样」，`algHierarchyInfo` 回答「这个算法要怎么切子组」。**

#### 4.1.2 核心流程

`TopoInfoWithNetLayerDetails` 的填充由 `topo_host.cc` 的控制面函数完成，调用链如下（本轮在 `CalcTopoShape` 末尾新增了一步 `CalcHostDPUOnly`）：

```text
InitRankInfo(comm, topoInfo)            // 入口：先填基类 TopoInfo，再填形状
  ├─ InitRankInfo(comm, TopoInfo*)      // ① 填本 rank / 服务器 / module 基础信息
  │    ├─ CalcMyRankInfo                //    userRank / serverIdx / superPodIdx
  │    ├─ GetPairLinkCounter            //    统计 rank 对之间的链路
  │    └─ SetServerModuleInfo / SetSuperPodInfo
  └─ CalcTopoShape(comm, topoInfo)      // ② 在基类之上填 netLayer 明细
       ├─ ExtractNetLayerDetails        //    netLayerNums / 每层实例大小 / topoLevelNums
       ├─ CalcLevel1Nhr
       ├─ ExtractTopoDetails            //    每层每个 topo 实例的类型与所含 rank
       ├─ CalcLevel0TopoShape / Is2DieFullMesh / CalcLevel0MeshType / CalcLevel2Uboe / CalcLevel2Ubg
       └─ CalcHostDPUOnly               //    【本轮新增】判定框间是否仅 Host DPU 可达
```

关键设计：填充过程**只查询、不下发**——它只通过 `HcclRankGraphGet*` 一族跨仓 dlsym 接口（详见 u6-l2）向 RankGraph 读取事实，不做任何数据搬移，因此属于纯**控制面**动作。

#### 4.1.3 源码精读

**(1) `TopoInfo`：本 rank 的基础拓扑信息**

[alg_param.h:195-213](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/inc/alg_param.h#L195-L213) 定义了基类结构，关键字段含义：

```cpp
struct TopoInfo {
    u32 userRank;                 // 本进程的全局 rankId
    u32 userRankSize;             // 通信域总 rank 数
    u32 serverIdx;                // 本 rank 所在 Server 在 ranktable 中的序号
    u32 superPodIdx;              // 本 rank 所在 SuperPod 的序号
    HcclDevType deviceType;       // 硬件类型（910B/950/960…，影响可用特性）
    u32 deviceNumPerModule;       // 每个 module 的卡数（Server 内）
    u32 serverNumPerSuperPod;     // 每个超节点的服务器个数
    u32 serverNum;                // 服务器数量
    u32 superPodNum;              // 超节点数量
    bool multiModuleDiffDeviceNumMode;        // Server 间卡数不一致（非对称）
    bool multiSuperPodDiffServerNumMode;      // 超节点间 Server 数不一致
    // ...
};
```

注意几个「**是否对称**」的布尔位（`multiModuleDiffDeviceNumMode` 等）：它们决定了后续匹配器能否走对称快路径，或必须走 GCD 切分的非对称路径（见 4.3）。

**(2) `TopoInfoWithNetLayerDetails`：附带 netLayer 明细**

[alg_param.h:216-232](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/inc/alg_param.h#L216-L232) 在基类之上扩展了本讲最关心的字段：

```cpp
struct TopoInfoWithNetLayerDetails : public TopoInfo {
    u32 topoLevelNums = 0;        // 算法可见的逻辑层级数（1/2/3）
    Level0Shape level0Topo;       // Layer0 形状：CLOS / MESH_1D / MESH_1D_CLOS
    bool Level0Nhr{false};        // 各级是否适用 NHR / HD / UB 等链路特性
    bool Level1Nhr{false};
    bool Level1Hd{false};
    bool hostDpuOnly{false};      // 【本轮新增】框间是否仅 Host DPU 可达
    bool level0Symmetric{false};  // Layer0 实例大小是否一致（对称）
    bool level1Symmetric{false};  // Layer1 实例大小是否一致
    Level0MeshType level0MeshType;
    NetLayerDetails netLayerDetails;                 // netLayer 明细（见下）
    std::vector<TopoInstDetails> topoInstDetailsOfLayer;  // 每层每个 topo 实例明细
    // 还自带 Serialize / DeSerialize，用于把拓扑快照拷到 device 侧
};
```

这里出现了几个核心字段，务必记清：

- **`topoLevelNums`**：算法视角的逻辑层级数。注意它和「物理 netLayer 个数」不一定相等——`ExtractNetLayerDetails` 会从最低层开始累加，**直到某一层只有 1 个网络实例（即覆盖了全部 rank）就停止**，由此得到算法真正需要的层级数。
- **`level0Topo`**：Layer0（Server 内）的拓扑形状，是 `CLOS`（交换拓扑）还是 `MESH_1D`（直连 Mesh），决定了 Layer0 用 Mesh 类还是 NHR/CLOS 类算法。它也是本轮新增退化分支（见 4.3/4.4）的判定条件之一。
- **`netLayerDetails`**：netLayer 明细，结构见 [alg_param.h:167-173](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/inc/alg_param.h#L167-L173)：

```cpp
struct NetLayerDetails {
    u32 netLayerNum;                          // 物理 netLayer 个数
    std::vector<u32> netLayers;               // 各 netLayer 的编号，如 {0, 1, 3}
    std::vector<u32> netInstNumOfLayer;       // 每层有几个网络实例（pod）
    std::vector<std::vector<u32>> instSizeListOfLayer;  // 每层各实例含多少 rank
    std::vector<u32> localNetInsSizeOfLayer;  // 本 rank 在每层所属实例的 rank 数
};
```

- **`hostDpuOnly`**（[alg_param.h:227](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/inc/alg_param.h#L227)，本轮新增）：「框间只有 Host DPU 可达」的专用拓扑标志，由 `CalcHostDPUOnly` 在填充 topoInfo 时一次性算出（见 4.4）。它还被加进了序列化/反序列化（写出在 [alg_param.h:265](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/inc/alg_param.h#L265)、读入在 [alg_param.h:317](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/inc/alg_param.h#L317)），保证 device 侧快照与 host 侧判定一致。

举例：8 台服务器、每台 8 卡（共 64 rank），单超节点。则 `netLayers={0,1}`，Layer0 的 `netInstNumOfLayer[0]=8`（8 个 pod），每个 pod `instSizeListOfLayer[0][i]=8`；Layer1 的 `netInstNumOfLayer[1]=1`（一个实例覆盖全部 64 卡）→ 因此 `topoLevelNums=2`。

**(3) `topoLevelNums` 是怎么算出来的**

[topo_host.cc:914-922](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/topo/topo_host.cc#L914-L922) 是关键算法：

```cpp
topoLevelNum = 0;
for (auto layerIdx : netLayers) {
    topoLevelNum++;
    if (netInstNumOfLayer[layerIdx] == 1) {
        break;   // 本层只有一个网络实例 → 已覆盖所有卡，停止累加
    }
}
```

这段循环把「物理 netLayer 列表」压缩成「算法需要的最少层级数」。这正是 `topoLevelNums` 与 `netLayerDetails.netLayerNum` 可能不同的原因。

**(4) `ExtractTopoDetails`：每个实例里有哪些 rank**

[topo_host.cc:953-983](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/topo/topo_host.cc#L953-L983) 逐层逐实例查询 RankGraph，把「每个 topo 实例的类型（`CommTopo`）」和「所含 rank 列表」填进 `TopoInstDetails`（[alg_param.h:174-180](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/inc/alg_param.h#L174-L180)）。这些明细是 4.3 中 `TopoForLayer0` 调用 `HcclRankGraphGetRanksByTopoInst` 的数据基础，也是 4.4 中 `CalcHostDPUOnly` 检查最外层 CLOS 实例的查询来源。

#### 4.1.4 代码实践

> **实践目标**：对照源码字段，建立「集群规模 → `TopoInfoWithNetLayerDetails` 字段值」的直觉。

**操作步骤（源码阅读型，无需 NPU）**：

1. 打开 [alg_param.h:216-232](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/inc/alg_param.h#L216-L232)，逐字段写下你的理解。
2. 假设一个集群：**4 台服务器、每台 4 卡、单超节点，共 16 rank**，Server 内是 Mesh、Server 间是 NHR。请推算：
   - `userRankSize = ?`（答：16）
   - `netLayers = ?`（答：`{0, 1}`，Layer0=Server 内，Layer1=Server 间）
   - `netInstNumOfLayer[0] = ?`（答：4，即 4 个 pod/Server）
   - `instSizeListOfLayer[0] = ?`（答：`{4,4,4,4}`）
   - `netInstNumOfLayer[1] = ?`（答：1，一个实例覆盖全部 16 卡）
   - `topoLevelNums = ?`（答：2）
   - `level0Symmetric = ?`（答：true，每个 pod 都是 4 卡）
   - `hostDpuOnly = ?`（答：本例 Server 间走 NHR（device 侧链路），最外层 endpoint 存在 device 位置链路，所以为 false——完整判定逻辑见 4.4）
3. 阅读循环 [topo_host.cc:914-922](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/topo/topo_host.cc#L914-L922)，确认你推算的 `topoLevelNums` 与代码逻辑一致。

**需要观察的现象**：在实际运行（待本地验证）时，开启 `HCCL_ALG` 日志后会看到类似 `[BaseSelector][ExtractNetLayerDetails] topoLevelNum[2], netLayerNum[2]` 的日志，以及本轮新增的 `Start CalcHostDPUOnly` / `Not using hostdpu because ...` 系列日志，可直接核对。

**预期结果**：能独立写出给定集群规模下三个字段 `topoLevelNums` / `level0Topo` / `netLayerDetails`（至少 `netLayers` 与 `netInstNumOfLayer`）的取值。

#### 4.1.5 小练习与答案

**练习 1**：`topoLevelNums` 和 `netLayerDetails.netLayerNum` 什么时候会不相等？

> **答案**：当高层 netLayer 的实例数已经为 1（即某层一个实例就覆盖了全部 rank）时，循环会提前 `break`，此时 `topoLevelNums < netLayerNum`。典型场景是 ranktable 配置了 `netLayers={0,3}` 但 Layer1 已经只有 1 个实例，算法层只需 2 级即可。

**练习 2**：为什么 `TopoInfoWithNetLayerDetails` 自带 `Serialize/DeSerialize`？

> **答案**：算法资源上下文（`AlgResourceCtxSerializable`，[alg_param.h:472-478](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/inc/alg_param.h#L472-L478)）需要把拓扑快照一起序列化后拷贝到 device 侧，供 device 侧的 kernel 反序列化使用，使 device 侧执行时也能获知拓扑信息。本轮把 `hostDpuOnly` 一并纳入序列化（[alg_param.h:265](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/inc/alg_param.h#L265) / [L317](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/inc/alg_param.h#L317)），两侧字段序号严格对齐，否则反序列化会错位。

---

### 4.2 模块二：TopoMatchBase 抽象与 MatchTopo 接口

#### 4.2.1 概念说明

不同算法对拓扑的「切法」不同：

- **Mesh1D 类算法**（如 `AicpuAllReduceSoleMesh`）：把全体 rank 当成一个扁平的大组，单级通信，不需要分层。
- **Mesh+NHR 类算法**（如 `AicpuAllReduceSequenceMeshNHR`）：需要把 rank 切成「Server 内（Mesh）」和「Server 间同位卡（NHR）」两级。
- **Mesh+UB+NHR 类算法**（超节点 UB 链路场景）：可能要切三层。

如果让每个算法自己写切分逻辑，会到处重复且容易和具体拓扑耦合。HCCL 的做法是抽象出一个**拓扑匹配器基类 `TopoMatchBase`**，把「给定 topoInfo → 切出 algHierarchyInfo」这一动作统一成纯虚接口 `MatchTopo`，再由各子类实现不同切法。算法（确切地说是 executor）在**编译期**通过模板参数选定一个匹配器，运行时只需一句 `topoMatch.MatchTopo(...)`。

#### 4.2.2 核心流程

匹配器在调度链中的位置如下（承接 u3-l1 / u3-l2）：

```text
Selector(comm, param, topoInfo, algName)        // Selector 阶段
  ├─ HcclCalcTopoInfo(comm, param, topoInfo)    // ← topo_host 填充 topoInfo（输入，含 hostDpuOnly）
  └─ ExecuteSelector::Run(...) → algName        // 选出算法名（含拓扑后缀）

HcclExecOp(...) → executor->CalcAlgHierarchyInfo(comm, topoInfo, algHierarchyInfo)  // Executor 阶段
  └─ AlgTopoMatch topoMatch; topoMatch.MatchTopo(comm, topoInfo, algHierarchyInfo)  // ← 匹配器切子组（输出）
executor->CalcRes(...)        // 基于子组算 channel/notify/thread 资源
executor->Orchestrate(...)    // 按层级依次调用 template 搬数据
```

注意两个关键点：

1. **输入 `topoInfo` 由 Selector 阶段的 `HcclCalcTopoInfo` 算出并缓存**——这就是 u3-l1 说的「topo 全体算子共享」的体现，同一通信域的多次调用复用同一份 topoInfo。本轮新增的 `hostDpuOnly` 也在这时算好缓存，供新选择器 SelectorEngine 直接读取（见 4.4）。
2. **输出 `algHierarchyInfo` 由 Executor 阶段的 `MatchTopo` 产出**——因为不同算法（executor）切法不同，必须在 executor 里按其绑定的匹配器现切。

#### 4.2.3 源码精读

**(1) 抽象基类与纯虚 `MatchTopo`**

[topo_match_base.h:55-64](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/topo/topo_match_base.h#L55-L64)：

```cpp
class TopoMatchBase {
public:
    explicit TopoMatchBase();
    virtual ~TopoMatchBase();
    virtual std::string Describe() const = 0;   // 自描述（调试用）
    virtual HcclResult MatchTopo(
        const HcclComm comm, TopoInfoWithNetLayerDetails* topoInfo,
        AlgHierarchyInfoForAllLevel& algHierarchyInfo);   // 纯虚：切子组
};
```

`MatchTopo` 的签名就是本模块的「接口契约」：吃 `topoInfo`（输入），吐 `algHierarchyInfo`（输出）。

**(2) 基类默认实现：防呆**

[topo_match_base.cc:18-26](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/topo/topo_match_base.cc#L18-L26) 给了一个**只报错、不干活**的默认实现：

```cpp
HcclResult TopoMatchBase::MatchTopo(...) {
    HCCL_ERROR("... use proper multi-level interfacce to match topo.", topoInfo->userRank);
    return HcclResult::HCCL_E_INTERNAL;
}
```

这是个防呆设计：若某 executor 误绑了一个未实现 `MatchTopo` 的基类实例，运行时会立刻报错而非静默跑错。

**(3) 输出结构 `AlgHierarchyInfoForAllLevel`**

[alg_param.h:467-469](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/inc/alg_param.h#L467-L469) 定义了匹配器的输出：

```cpp
struct AlgHierarchyInfoForAllLevel {
    std::vector<std::vector<std::vector<u32>>> infos;  // 第一维表示有多少level，第二维是每个level的rankID
};
```

三维语义：`infos[level]` 是该层所有子组的集合；`infos[level][groupIndex]` 是其中一个子组内的全局 rankId 列表。多数算法在本 rank 视角下，每一层只属于一个子组，因此 `infos[level]` 通常 `size()==1`。

**(4) executor 如何调用 `MatchTopo`**

executor 把匹配器作为模板参数 `AlgTopoMatch`，在 `CalcAlgHierarchyInfo` 里实例化并调用。以 all_reduce 顺序执行器为例 [ins_v2_all_reduce_sequence_executor.cc:103-116](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/executor/ins_v2_all_reduce_sequence_executor.cc#L103-L116)：

```cpp
// 使用topo match计算AlgHierarchyInfoForAllLevel
AlgTopoMatch topoMatch;
CHK_RET(topoMatch.MatchTopo(comm, topoInfo, algHierarchyInfo));
```

而 `AlgTopoMatch` 具体是哪个匹配器，是在**注册宏**里编译期绑定的 [ins_v2_all_reduce_sequence_executor.cc:511-515](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/executor/ins_v2_all_reduce_sequence_executor.cc#L511-L515)：

```cpp
REGISTER_EXECUTOR_BY_FOUR_TEMPS(
    HcclCMDType::HCCL_CMD_ALLREDUCE, DpuAllReduceSequenceMeshNHR,
    InsV2AllReduceSequenceExecutor, TopoMatchMultilevel,   // ← 拓扑匹配器在此绑定
    InsTempReduceScatterMesh1DIntra, InsTempReduceScatterMesh1dDpuInter,
    InsTempAllGatherNhrDpuInter, InsTempAllGatherMesh1dIntra);
```

这就是 algName 末尾拓扑后缀（`MeshNHR`）与匹配器（`TopoMatchMultilevel`）的对应关系——**算法名里写明了它要哪种切法**。注意本例 algName 以 `Dpu` 开头，正是 Host DPU（HOSTCPU 引擎）系列的算法（引擎前缀与算法名的关系见 u2-l3 的 `ENGINE_PREFIX_MAP`）。

**(5) 旧式 vs 新式：并存的 `AlgHierarchyInfo`**

为兼容 A2/A3 对称拓扑的老路径，topo 还保留了一套**按维数硬切**的 `AlgHierarchyInfo`（[alg_param.h:426-429](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/inc/alg_param.h#L426-L429)），由 `CalcGeneralTopoInfoForA2/A3/Comm` 填充。以 A3（三级对称）为例 [topo.cc:78-100](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/topo/topo.cc#L78-L100)：

```cpp
algHierarchyInfo.levels = 3;
algHierarchyInfo.infos[COMM_LEVEL0].localRankSize = topoInfo->deviceNumPerModule;       // Server 内卡数
algHierarchyInfo.infos[COMM_LEVEL1].localRankSize = topoInfo->serverNumPerSuperPod;     // 超节点内 Server 数
algHierarchyInfo.infos[COMM_LEVEL2].localRankSize = topoInfo->superPodNum;              // 超节点数
```

它只存 `localRank/localRankSize`（自己在每层的序号与规模），不存具体 rank 列表，依靠「整除取模」的规整假设（见 `GetUserRankBySubCommRank` [topo.cc:117-131](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/topo/topo.cc#L117-L131)）。新式 `topo_match_*` 家族则直接产出 rank 列表、能处理非对称，是当前主路径。两者并存是 u1-l1 提到的「legacy 不持续演进」约束的体现。

#### 4.2.4 代码实践

> **实践目标**：建立「algName 拓扑后缀 ↔ 匹配器」的对应表。

**操作步骤（源码阅读型）**：

1. 在 `src/ops/all_reduce/executor/` 下用搜索定位所有 `REGISTER_EXECUTOR_BY_*` 宏，记录每个注册项的 algName（第 2 个参数）与匹配器（第 4 个参数）。
2. 你会发现规律：algName 含 `SoleMesh` / `SoleNHR` 的通常绑 `TopoMatch1D`（单级）；含 `SequenceMeshNHR` / `ParallelMeshNHR` 的绑 `TopoMatchMultilevel`（多级）；以 `Dpu` 开头的（如 `DpuAllReduceSequenceMeshNHR`）是 Host DPU 场景专属。
3. 阅读 [topo_match_1d.cc:47-53](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/topo/topo_match_1d.cc#L47-L53)，确认 `TopoMatch1D` 的输出就是把全体 rank 塞进 `infos[0][0]`：

```cpp
algHierarchyInfoExector.infos.resize(1);
algHierarchyInfoExector.infos[0].resize(1);
algHierarchyInfoExector.infos[0][0] = rankIds;   // 0~rankSize 全部 rank
```

**预期结果**：能用一句话说出 `TopoMatch1D` 与 `TopoMatchMultilevel` 的区别——前者不分级、全体一组；后者按 netLayer 分级、产出多级子组。

#### 4.2.5 小练习与答案

**练习 1**：为什么匹配器要做成**模板参数**（编译期绑定）而非注册表运行时查找？

> **答案**：匹配器与 executor/template 在注册宏里一次性绑定，编译期即可确定整条「算法×切法×模板」组合，免去运行时查表开销；同时也让编译器对组合做类型检查。这与 selector/executor/template 三大注册表的「按 algName 运行时查」是两个层次：注册表负责找到 executor 实例，模板参数负责该 executor 内部的拓扑切法。

**练习 2**：基类 `MatchTopo` 为什么故意写一个只报错的默认实现？

> **答案**：纯虚函数本不允许实例化基类，但留一个报错的默认实现可作为「未覆写」的兜底防呆，错误信息明确提示「请用正确的 multi-level 接口」，便于定位绑定错误。

---

### 4.3 模块三：TopoMatchMultilevel 的 TopoForLayer0/1/2

#### 4.3.1 概念说明

`TopoMatchMultilevel` 是分级算法（Mesh+NHR、Mesh+UB+NHR）的核心匹配器。它的 `Describe()` 自述为「layer 0 Mesh, layer 1 NHR」[topo_match_multilevel.h:22](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/topo/topo_match_multilevel.h#L22)。它要做的事可以概括为：

> 给定本 rank 视角下的物理拓扑，**自底向上**逐层切出子组——Layer0 是本 rank 所在 pod（Server）内的全体卡；Layer1 是跨 pod 中「与本 rank 同位」的那些卡；Layer2（若有）是跨超节点中「与本 rank 同位」的那些卡。

这里「同位」是关键直觉：若每台 Server 有 4 张卡，那么所有 Server 的「第 2 号卡」就构成一个 Layer1 子组 `{2, 6, 10, 14, ...}`。分级 AllReduce 正是先在 Layer0 内 ReduceScatter，再让每个 Layer1 子组各自做一次小 AllReduce，最后在 Layer0 内 AllGather——把 Server 间的数据量降到 1/N。

#### 4.3.2 核心流程

`MatchTopo` 的整体编排见 [topo_match_multilevel.cc:233-378](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/topo/topo_match_multilevel.cc#L233-L378)，可概括为：

```text
MatchTopo(comm, topoInfo, algHierarchyInfo)
  0. 校验 topoLevelNums ∈ [1,3]、设备类型 shouldGoOutPlace
  1. 查 netLayer 列表；统计每 pod 的 rank 数（instSizeList）；判断是否对称 isSymmetric / layer1Symmetric
  2. 查询 hostDPUOnly（Host DPU 场景）：三级拓扑标记 needDowngrade
  3. 校验：三级拓扑不支持非对称；非对称只支持 Mesh1D
  4. 按 topoLevelNums 决定算法层数 commLayerSize（2 或 3），infos.resize(commLayerSize)
  5. 切 Layer0：
     a.【本轮新增】hostDPUOnly + 两级 + level0Topo==CLOS → 退化：Layer0={myRank}，layer0Size=1
     b. 非对称 → TopoForLayer0(gcdInstSize)（按 GCD 细切 pod）
     c. 对称   → TopoForLayer0()（整个 pod 一组）
  6. TopoForLayer1(...)           // 切 Layer1（pod 间同位）
  7. if (topoLevelNums >= 3): TopoForLayer2(...)   // 切 Layer2（超节点间同位）
```

非对称场景下，Layer0 不是把整个 pod 当一组，而是按各 pod rank 数的**最大公约数（GCD）**再细切，公式为：

\[
g = \mathrm{GCD}(s_0, s_1, \dots, s_{k-1}), \qquad s_i = \text{第 } i \text{ 个 pod 的 rank 数}
\]

每个 pod 按 \(g\) 切成若干子组，使所有 pod 的子组大小一致，从而 Layer1「同位」关系成立。

#### 4.3.3 源码精读

**(1) `MatchTopo` 的对称/非对称判定与层数决定**

[264-357 行] 核心逻辑（节选）：

```cpp
// 2. 取每个 pod 的 rank 数，判断对称性
CHK_RET(HcclRankGraphGetInstSizeListByLayer(comm, 0, &instSizeList, &listSize));
bool isSymmetric = CheckVecElementAllSame(instSizeList, listSize);

// 2.5 查询 Host DPU 场景（按需查询，不复用 topoInfo->hostDpuOnly 字段）
bool hostDPUOnly = false;
if ((CheckHostDPUOnly(comm, topoInfo, hostDPUOnly) == HcclResult::HCCL_SUCCESS) && hostDPUOnly) { ... }

// 3. 决定算法层数：三级且非 HostDPU 降级时用 3，否则用 2
bool needDowngrade = hostDPUOnly && topoInfo->topoLevelNums == COMM_LAYER_SIZE_3;
uint32_t commLayerSize =
    (topoInfo->topoLevelNums == COMM_LAYER_SIZE_3 && !needDowngrade) ? COMM_LAYER_SIZE_3 : COMM_LAYER_SIZE_2;
algHierarchyInfo.infos.resize(commLayerSize);

// 5. 切 Layer0：本轮新增的退化分支优先
if (hostDPUOnly && topoInfo->topoLevelNums == COMM_LAYER_SIZE_2 && topoInfo->level0Topo == Level0Shape::CLOS) {
    algHierarchyInfo.infos[0].push_back({myRank});   // Layer0 退化为单卡
    layer0Size = 1;
} else if (!isSymmetric) {
    uint32_t gcdInstSize = GcdOfInstSizeList(instSizeList, listSize);
    CHK_RET(TopoForLayer0(comm, layer0Size, myRank, algHierarchyInfo, gcdInstSize));
} else {
    CHK_RET(TopoForLayer0(comm, layer0Size, myRank, algHierarchyInfo));
}
// 6. 切 Layer1 / 7. 切 Layer2 ...
```

这里 `COMM_LAYER_SIZE_2` / `COMM_LAYER_SIZE_3` 是常量 2/3（[topo_match_base.h:27-30](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/topo/topo_match_base.h#L27-L30)）。HostDPU 有两处特殊处理：

- **三级降级**（[340-349](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/topo/topo_match_multilevel.cc#L340-L349)）：物理三级拓扑在 Host DPU 场景下降级成算法二级（`needDowngrade`）。
- **两级退化**（[351-357](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/topo/topo_match_multilevel.cc#L351-L357)，**本轮新增**）：`hostDPUOnly` 且 `topoLevelNums==2` 且 `level0Topo==CLOS` 时，两级 Mesh+NHR 算法退化为「单级跨 DPU」——Layer0 只剩自己一张卡（`infos[0][0]={myRank}`、`layer0Size=1`），所有实际通信都发生在最外层的 Layer1 全组上。为什么？因为此时框内卡间没有 device 直连（都汇到 Host DPU），「框内 Mesh 先归约」无从谈起，框内 ReduceScatter/AllGather 步骤形同虚设，不如直接让 Layer1 承担全部数据量。这段代码的日志原文是 `"degenerate level0 to single card, layer1 = full group in top layer"`。

**(2) `TopoForLayer0`：切 pod 内**

[19-103 行](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/topo/topo_match_multilevel.cc#L19-L103) 按 pod 内 topo 实例数分多种情况，对称单实例的核心分支 [57-61](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/topo/topo_match_multilevel.cc#L57-L61)：

```cpp
// Symmetric: 把整个 pod 的 rank 作为一组
std::vector<uint32_t> rankVecLayer0(ranks, ranks + rankNum);
algHierarchyInfo.infos[0].push_back({rankVecLayer0});
layer0Size = rankVecLayer0.size();   // 记下 pod 内卡数，供 Layer1 取同位用
```

非对称分支 [37-56](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/topo/topo_match_multilevel.cc#L37-L56) 则用 `gcdInstSize` 把 pod 切成多个 GCD 大小的子组：

```cpp
uint32_t myIdx = static_cast<uint32_t>(it - ranks);
uint32_t groupId = myIdx / gcdInstSize;          // 本 rank 落在第几个子组
uint32_t startIdx = groupId * gcdInstSize;
uint32_t endIdx = std::min(startIdx + gcdInstSize, rankNum);
std::vector<uint32_t> rankVecLayer0(ranks + startIdx, ranks + endIdx);
algHierarchyInfo.infos[0].push_back({rankVecLayer0});
layer0Size = gcdInstSize;
```

注意 `layer0Size` 在这里被设成 `gcdInstSize`（而非整个 pod 大小），这是后续 Layer1 取同位的基准——非对称时同位关系按 GCD 子组规模对齐。

**(3) `TopoForLayer1`：取「跨 pod 同位卡」——本讲的精髓**

[105-151 行](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/topo/topo_match_multilevel.cc#L105-L151)，核心是同位判定 [126-148](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/topo/topo_match_multilevel.cc#L126-L148)：

```cpp
std::vector<uint32_t> rankVecLayer1WithSameIdx;
for (uint32_t i = 0; i < rankNum; i++) {
    uint32_t rankId = ranks[i];
    if (myRank == rankId) { rankVecLayer1WithSameIdx.push_back(rankId); continue; }
    // 同位判定：rankId 与 myRank 在各自 pod 内的序号必须相同
    if (rankId % layer0Size != myRank % layer0Size) { continue; }
    // 还需二者之间在 Layer1 确有物理链路
    CHK_RET(HcclRankGraphGetLinks(comm, netLayer, myRank, rankId, &links, &linkNum));
    if (linkNum == 0) { continue; }
    rankVecLayer1WithSameIdx.push_back(rankId);
}
algHierarchyInfo.infos[1].push_back({rankVecLayer1WithSameIdx});
```

判定式 `rankId % layer0Size != myRank % layer0Size` 就是「同位」的数学表达：两个 rank 对 `layer0Size`（pod 内卡数）取模相等，意味着它们在各自 pod 内处于相同序号位置。再叠加 `HcclRankGraphGetLinks` 检查物理链路存在性，保证切出的子组真的能互通。在 4.3(1) 的退化分支里 `layer0Size=1`，此时 `rankId % 1 == 0` 恒成立——所有 rank 都是「同位」，Layer1 自然变成全组，与退化的设计意图吻合。

**(4) `TopoForLayer2`：取「跨超节点同位卡」**

[187-231 行](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/topo/topo_match_multilevel.cc#L187-L231) 与 Layer1 同构，只是同位基准从 `layer0Size` 升级为 `layer0Size * layer1Size` [217](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/topo/topo_match_multilevel.cc#L217)：

```cpp
if (rankId % (layer0Size * layer1Size) != myRank % (layer0Size * layer1Size)) { continue; }
```

即：两个 rank 在「pod 内序号 + pod 间序号」复合意义上同位，才归入同一 Layer2 子组。

#### 4.3.4 代码实践

> **实践目标**：用一个具体的 2 级集群，手算 `TopoMatchMultilevel` 为 Mesh+NHR 算法产出的两级子通信域。

**场景**：2 台 Server（pod），每台 4 卡，单超节点，共 8 rank。rank 编号连续：pod0={0,1,2,3}，pod1={4,5,6,7}。Server 内 Mesh、Server 间 NHR，任意同位卡之间都有链路。本 rank 取 `myRank=2`。

**操作步骤（手算 + 源码对照）**：

1. **先推 topoInfo**：`topoLevelNums=2`，`layer0Size=4`，`netLayers={0,1}`，`level0Topo=MESH_1D`，`hostDpuOnly=false`（框间走 device 侧 NHR 链路）。
2. **切 Layer0**（对照 [57-61](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/topo/topo_match_multilevel.cc#L57-L61)）：myRank=2 所在 pod 是 pod0，整个 pod 作为一组 → `infos[0][0] = {0,1,2,3}`，`layer0Size=4`。
3. **切 Layer1**（对照 [126-148](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/topo/topo_match_multilevel.cc#L126-L148)）：遍历 Layer1 全体 rank {0..7}，保留满足 `rankId % 4 == 2 % 4 == 2` 的：
   - 0%4=0 ✗，1%4=1 ✗，2（自身）✓，3%4=3 ✗，4%4=0 ✗，5%4=1 ✗，6%4=2 ✓，7%4=3 ✗
   - 结果 `infos[1][0] = {2, 6}`。
4. **结论**：本 rank=2 的两级子通信域为 Layer0={0,1,2,3}（pod 内 Mesh）、Layer1={2,6}（跨 pod 同位 NHR）。
5. **改一步看退化**：把同一集群改成「框内是 CLOS 交换、框间仅 Host DPU 互通」（即 `hostDpuOnly=true`、`level0Topo=CLOS`），重新走 [351-357](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/topo/topo_match_multilevel.cc#L351-L357) 的退化分支 → `infos[0][0]={2}`、`layer0Size=1`，Layer1 判定 `% 1` 恒真 → `infos[1][0]={0,1,2,3,4,5,6,7}` 全组。

**需要观察的现象**：实际运行（待本地验证）时，对应日志形如：
`[CollAlgFactory] [TopoMatchMultilevel] Rank [2] ... GCD subgroup / layer1 ranks`，Host DPU 场景还能看到 `degenerate level0 to single card, layer1 = full group in top layer`，可核对 `infos` 内容。

**预期结果**：

| 层级 | 子组（常规 NHR 场景） | 算法语义 |
| --- | --- | --- |
| Layer0 | `{0,1,2,3}` | Server 内 Mesh，先做 ReduceScatter |
| Layer1 | `{2,6}` | Server 间同位 NHR，做小 AllReduce |

一次完整 AllReduce 即「Layer0 ReduceScatter → Layer1 AllReduce → Layer0 AllGather」，与 u1-l2 的分级通信原理完全对应；而在 hostDpuOnly+CLOS 场景下退化为「Layer1 全组单级通信」。

#### 4.3.5 小练习与答案

**练习 1**：把上例的 pod 大小改成非对称 pod0=4、pod1=6（共 10 rank），重算 Layer0 与 `layer0Size`。

> **答案**：`GCD(4,6)=2`，故 `layer0Size=2`。myRank=2 在 pod0 中 `myIdx=2`，`groupId=2/2=1`，取 `startIdx=2, endIdx=4` → Layer0 子组 `{2,3}`。注意此时同位基准变成 2，Layer1 会按 `% 2` 取同位。

**练习 2**：为什么 [308-314](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/topo/topo_match_multilevel.cc#L308-L314) 规定「三级拓扑不支持任何非对称」？

> **答案**：三级「同位」要求 rank 在 `layer0Size * layer1Size` 复合维度上对齐，这隐含 Layer0 与 Layer1 都必须对称规整，否则跨超节点的同位关系无法定义、子组无法对齐，因此非对称被直接拒绝（返回 `HCCL_E_NOT_SUPPORT`）。

**练习 3**：`TopoForLayer1` 在同位判定之外，为何还要再查一次 `HcclRankGraphGetLinks`？

> **答案**：取模同位是「逻辑」推断，但物理上两张同位卡之间未必真有直连链路（例如某条线故障或拓扑非全连接）。再查 `linkNum==0` 跳过无链路的 rank，保证切出的子组在物理上确实可通信，是「逻辑切分 + 物理校验」的双重保险。

---

### 4.4 模块四：CalcHostDPUOnly 与 hostDpuOnly 字段（本轮新增）

#### 4.4.1 概念说明

先解释场景术语：**Host DPU** 指服务器主机侧的 DPU/网卡转发单元。多数集群里，Server 内卡间有 device 直连（HCCS/RoCE 直连），Server 间也有 device 侧网络；但在一类专用拓扑中，**框内是 CLOS 交换、框与框之间只有 Host DPU 一条通路**——卡间跨框流量必须绕道主机侧 DPU 转发。此时：

- 「框内 Mesh 先归约再跨框」的分级算法失去意义（框内没有直连可做 Mesh）；
- 引擎层面只有走 HOSTCPU（DPU 前缀算法，如 `DpuAllReduceSequenceMeshNHR`）才是可行路径。

为此，本轮在 topoInfo 里新增了 `hostDpuOnly` 布尔字段，并新增 `CalcHostDPUOnly` 函数在**填充 topoInfo 时一次性算出并缓存**。此前项目里已有一个按需查询的同逻辑函数 `CheckHostDPUOnly`（[op_common.cc:3479](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L3479)，老选择器路径与匹配器仍调用它）；`CalcHostDPUOnly` 的增量价值在于**把结论持久化进 topoInfo 快照**，让新选择器 SelectorEngine、CostTable 过滤等消费方免于反复查询 RankGraph（跨仓 dlsym 调用不便宜）。

#### 4.4.2 核心流程

`CalcHostDPUOnly` 的判定流程（对应 [topo_host.cc:786-865](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/topo/topo_host.cc#L786-L865)）：

```text
CalcHostDPUOnly(comm, topoInfo)
  0. hostDpuOnly = false（默认关）
  1. serverNum == 1        → 直接返回 false   // 单机无需跨框，用不上 DPU
  2. topoLevelNums == 1    → 直接返回 false   // 只有一层拓扑，无框间可言
  3. HcclRankGraphGetLayers 拿到物理 netLayer 列表
  4. 只检查最外层（netLayer >= topoLevelNums - 1 的层）：
     a. 对该层每个拓扑实例：
        - HcclRankGraphGetTopoType：类型必须是 COMM_TOPO_CLOS（交换拓扑）
        - HcclRankGraphGetRanksByTopoInst：该实例必须覆盖全部 rank（rankNum == userRankSize）
        - 遍历该实例的所有 endpoint（HcclRankGraphGetEndpointDesc）：
          * 只要发现一个 DEVICE 位置的 endpoint → 立即返回 false（框间仍有 device 链路）
          * 发现 HOST 位置的 endpoint → 标记 hostDPU = true
  5. hostDPU 为真 → topoInfo->hostDpuOnly = true
```

判定语义可以概括为三个必要条件：**多机、多层、最外层 CLOS 全覆盖且所有 endpoint 都落在 Host 侧**——即「跨框流量除了 Host DPU 无路可走」。

`hostDpuOnly` 的下游影响有三处：

1. **引擎选择**：新选择器 `SelectorEngine::GetEnginePriority` 读到 `hostDpuOnly=true` 时直接只返回 `{HOSTCPU}` 一个候选引擎（[selector_engine.cc:68-75](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_engine.cc#L68-L75)），代码注释明确写着「hostDpuOnly 已在 HcclCalcTopoInfo 中计算并缓存」。
2. **CostTable 过滤**：`cost_table.cc` 的算法过滤规则里多处带 `&& !topo->hostDpuOnly`（如 [cost_table.cc:366](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/cost_table.cc#L366)、[L375](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/cost_table.cc#L375)、[L387](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/cost_table.cc#L387)），在 Host DPU 拓扑下剔除依赖框内 NHR/三级等 device 链路的算法。
3. **拓扑退化**：即 4.3 讲的 `TopoMatchMultilevel` 两级退化分支与既有的三级 `needDowngrade` 降级。

#### 4.4.3 源码精读

**(1) 挂载点：`CalcTopoShape` 的最后一步**

[topo_host.cc:771-784](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/topo/topo_host.cc#L771-L784) 中，`CalcHostDPUOnly` 被追加在 `CalcTopoShape` 的结尾：

```cpp
HcclResult CalcTopoShape(HcclComm comm, TopoInfoWithNetLayerDetails* topoInfo)
{
    // ... CalcLevel0TopoShape / Is2DieFullMesh / CalcLevel0MeshType / CalcLevel2Uboe / CalcLevel2Ubg
    CHK_RET(CalcHostDPUOnly(comm, topoInfo));   // 【本轮新增】
    return HCCL_SUCCESS;
}
```

这保证了只要 topoInfo 被填充，`hostDpuOnly` 就一定有值——它依赖前面的 `topoLevelNums`（`ExtractNetLayerDetails` 算出）与 `serverNum`（`InitRankInfo` 填充），所以放在最后一步是必然的。

**(2) 前置闸门与最外层检查**

[topo_host.cc:786-805](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/topo/topo_host.cc#L786-L805)：

```cpp
HcclResult CalcHostDPUOnly(HcclComm comm, TopoInfoWithNetLayerDetails* topoInfo)
{
    topoInfo->hostDpuOnly = false;
    // 只有一个server，不使用DPU
    if (topoInfo->serverNum == 1) { return HCCL_SUCCESS; }
    // 只有一层topo，不使用DPU
    if (topoInfo->topoLevelNums == 1) { return HCCL_SUCCESS; }
    // ... 取 netLayer 列表，跳过 netLayer < topoLevelNums - 1 的层（只校验最后一个 level）
```

**(3) endpoint 位置判定——一票否决的 DEVICE 检查**

[topo_host.cc:833-862](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/topo/topo_host.cc#L833-L862)：

```cpp
for (uint32_t endPointIdx = 0; endPointIdx < endPointNums; endPointIdx++) {
    EndpointDesc endPointDesc = endPointDescs[endPointIdx];
    if (endPointDesc.loc.locType == ENDPOINT_LOC_TYPE_DEVICE) {
        // 框间仍存在 device 侧链路 → 不是 Host DPU 专用拓扑，直接结束
        return HCCL_SUCCESS;          // hostDpuOnly 保持 false
    } else if (endPointDesc.loc.locType == ENDPOINT_LOC_TYPE_HOST) {
        hostDPU = true;               // 发现 Host 侧 endpoint
    }
}
// ...
if (hostDPU) { topoInfo->hostDpuOnly = true; }
```

注意两处细节：一是 `rankNum != topoInfo->userRankSize` 的实例被 `continue` 跳过——只有「一个 CLOS 实例就覆盖全部 rank」才说明所有卡汇入同一张交换网；二是 `ENDPOINT_LOC_TYPE_DEVICE` 是**立即返回**而非跳过，因为只要框间存在任何 device 链路，「仅 Host DPU」的前提就被推翻，无需再看其他实例。

#### 4.4.4 代码实践（本讲主实践）

> **实践目标**：对照 `TopoInfoWithNetLayerDetails` 字段与 `CalcHostDPUOnly` 源码，说清判定依据与下游影响。

**操作步骤（源码阅读型，无需 NPU）**：

1. 阅读字段声明 [alg_param.h:227](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/inc/alg_param.h#L227) 与序列化两行 [L265](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/inc/alg_param.h#L265) / [L317](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/inc/alg_param.h#L317)，确认 `hostDpuOnly` 已随快照序列化。
2. 精读 [topo_host.cc:786-865](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/topo/topo_host.cc#L786-L865)，回答：判定依据了 `serverNum`、`topoLevelNums`、最外层 CLOS 实例的哪三个查询结果？（`GetTopoType` 类型、`GetRanksByTopoInst` 覆盖面、`GetEndpointDesc` 位置）
3. 用 grep 找出 `hostDpuOnly` 字段（注意非 `CheckHostDPUOnly` 函数）的全部消费方：`selector_engine.cc:68-75` 与 `cost_table.cc:366/375/387`，分别说明它们如何改变引擎候选集与算法过滤结果。
4. 对比 [op_common.cc:3479](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L3479) 的 `CheckHostDPUOnly`（按需查询、不落盘）与 `CalcHostDPUOnly`（填充期算好、写入字段），说明为什么新选择器选择后者。

**需要观察的现象**：待本地验证——在真实多机环境上运行任意集合通信算子并开启 INFO 日志，观察 `Start CalcHostDPUOnly` 后跟的是 `Not using hostdpu because ...`（哪一条理由）还是 `Using host dpu trans.`，据此判断本集群是否命中 Host DPU 专用拓扑。

**预期结果**：能写出一段完整因果链，例如：

> `serverNum>1` 且 `topoLevelNums>=2`，最外层唯一的 CLOS 实例覆盖全部 rank 且其 endpoint 全部位于 HOST → `CalcHostDPUOnly` 置 `hostDpuOnly=true` → 新选择器 `GetEnginePriority` 只返回 `{HOSTCPU}`（Dpu 前缀算法），CostTable 过滤掉依赖框内 NHR/三级链路的算法 → `TopoMatchMultilevel` 把两级 Mesh+NHR 退化为「Layer0 单卡 + Layer1 全组」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `serverNum == 1` 和 `topoLevelNums == 1` 可以直接短路返回 false？

> **答案**：`serverNum==1` 意味着所有 rank 在同一框内，根本不存在「框间」通路问题；`topoLevelNums==1` 意味着算法视角只有一层拓扑（一层实例已覆盖全部 rank），同样没有跨层可达性可言。两种情况都不存在「跨框流量」，自然用不上 Host DPU，提前返回避免无谓的 RankGraph 查询，符合「廉价优先、尽早失败」的校验哲学。

**练习 2**：`CalcHostDPUOnly` 与 `CheckHostDPUOnly` 逻辑几乎相同，为什么要新增一份写进 topoInfo？

> **答案**：`CheckHostDPUOnly` 是按需查询，每次调用都要走一遍 RankGraph 的 dlsym 查询；`CalcHostDPUOnly` 在 `HcclCalcTopoInfo` 填充 topoInfo 时算一次并写入 `hostDpuOnly` 字段，topoInfo 本身按通信域缓存（u2-l2 的 tag 缓存机制），后续 SelectorEngine、CostTable 过滤直接读布尔位即可。代价是两份相同逻辑并存（短期技术债），收益是把查询成本摊销到「每通信域一次」。

**练习 3**：如果最外层 CLOS 实例的 endpoint 中混有一个 DEVICE 位置的链路，`hostDpuOnly` 会是多少？为什么用 `return` 而不是 `continue`？

> **答案**：是 false。因为「仅 Host DPU 可达」是全称判断——只要存在任何一条 device 侧跨框链路，就说明流量可以不绕 DPU，专用拓扑的前提不成立，后面的检查全部失去意义，所以直接返回（`hostDpuOnly` 保持初始 false）；`continue` 只用于「本实例不满足条件、还可以看下一个实例」的场景（如实例类型不是 CLOS、或未覆盖全部 rank）。

---

## 5. 综合实践

**任务**：从一次真实的 algName 出发，端到端追踪 topo 的输入与输出，画出本 rank 的多级子通信域图，并推演 Host DPU 场景下的退化。

**步骤**：

1. 选定算法 `DpuAllReduceSequenceMeshNHR`（见 [ins_v2_all_reduce_sequence_executor.cc:511-515](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/executor/ins_v2_all_reduce_sequence_executor.cc#L511-L515)），确认它绑定的匹配器是 `TopoMatchMultilevel`、四个 template 分别是 `InsTempReduceScatterMesh1DIntra`（Layer0）/ `InsTempReduceScatterMesh1dDpuInter`（Layer1）/ `InsTempAllGatherNhrDpuInter`（Layer1）/ `InsTempAllGatherMesh1dIntra`（Layer0）。
2. 自拟一个 3 级集群（如 2 超节点 × 2 Server × 4 卡 = 16 rank），按本讲方法推算：
   - topoInfo 的 `topoLevelNums`、`netLayers`、`level0Symmetric`；
   - myRank=5 时，`TopoForLayer0/1/2` 各自切出的子组（提示：Layer1 同位基准 `layer0Size=4`，Layer2 同位基准 `layer0Size*layer1Size=8`）。
3. 把结果画成三层同心图：最内层 Layer0 子组、中层 Layer1 子组、外层 Layer2 子组，标注每个子组的 rank 列表。
4. 对照四个 template，标注「Layer0 ReduceScatter → Layer1 ReduceScatter → Layer1 AllGather → Layer0 AllGather」分别落在哪一层子组上。
5. **退化推演**：把该集群改成 Host DPU 专用拓扑（最外层 endpoint 全在 Host 侧），重走一遍：`hostDpuOnly=true` → `needDowngrade` 三级降二级 → 若还是 `level0Topo==CLOS` 的两级形态则触发 [351-357](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/topo/topo_match_multilevel.cc#L351-L357) 的单级退化，画出退化后的两层图并说明哪些 template 步骤实际不再搬数据。

**预期产出**：一张含三层的子通信域示意图 + 一张 Host DPU 退化后的示意图 + 一句话说明 topo 输入（topoInfo，含 `hostDpuOnly`）与 topo 输出（algHierarchyInfo）分别由哪个阶段、哪个函数产生。

> 运行时验证（待本地验证）：在真实 NPU 环境开启 `HCCL_ALG` 日志，可在 `[TopoMatchMultilevel]`、`[ExtractNetLayerDetails]` 与 `CalcHostDPUOnly` 系列日志中核对上述手算结果。

---

## 6. 本讲小结

- **topo 是共享控制面基础设施**：唯一位于 `op_common/topo`、被全体算子复用；它只查询 RankGraph、不下发数据，属纯控制面。
- **两个数据层次**：`TopoInfo`/`TopoInfoWithNetLayerDetails` 是**输入快照**（物理拓扑事实），`AlgHierarchyInfoForAllLevel` 是**输出结论**（按算法层级切好的 rank 子组）。
- **三个核心字段**：`topoLevelNums`（算法可见层级数，由「覆盖全部 rank 即停」算出）、`level0Topo`（Layer0 形状 CLOS/MESH）、`netLayerDetails`（每层实例数与大小明细）。
- **匹配器抽象**：`TopoMatchBase::MatchTopo(topoInfo) → algHierarchyInfo` 是统一契约；executor 通过模板参数在编译期绑定具体匹配器（`TopoMatch1D`/`TopoMatchMultilevel`/`TopoMatchUBX`…）。
- **数据流分两段**：Selector 阶段的 `HcclCalcTopoInfo` 算出并缓存 `topoInfo`；Executor 阶段的 `CalcAlgHierarchyInfo → MatchTopo` 现切出 `algHierarchyInfo`，驱动后续按层级的 template 编排。
- **多级切分精髓**：`TopoMatchMultilevel` 自底向上 `TopoForLayer0/1/2`，Layer1/Layer2 用「取模同位 + 物理链路校验」取跨层同位卡；非对称场景按 GCD 细切 Layer0 以保证同位对齐。
- **本轮新增 hostDpuOnly 体系**：`CalcHostDPUOnly` 在填充 topoInfo 时按「多机 + 多层 + 最外层 CLOS 全覆盖且 endpoint 全在 Host 侧」判定专用拓扑并落盘到 `hostDpuOnly` 字段（随快照序列化）；该标志让新选择器只保留 HOSTCPU 引擎、让 CostTable 剔除依赖 device 链路的算法，并让 `TopoMatchMultilevel` 在两级 CLOS 场景把算法退化为「Layer0 单卡 + Layer1 全组」。

---

## 7. 下一步学习建议

- **进入 executor 内部**（u3-l4）：本讲止步于 `CalcAlgHierarchyInfo` 产出 `algHierarchyInfo`，下一步看 executor 如何**消费**它——`CalcRes` 据此算 channel/notify/thread 资源，`Orchestrate` 据此按层级依次调用 template。
- **进入 template 内部**（u3-l5 / Unit 5）：看 `InsTempReduceScatterMesh1DIntra` 这类模板如何把一个子组内的搬数据落成具体的 kernel/Task 下发。
- **看 hostDpuOnly 的消费端**（u8-l1）：新选择器 `SelectorEngine::GetEnginePriority` 如何用 `hostDpuOnly` 把候选引擎收窄为 `{HOSTCPU}`，CostTable 又如何据此过滤算法——这是本讲字段在「代价模型选择器」体系中的完整闭环。
- **回到控制面/数据面**（u6-l2 / u6-l3）：topo 的输入全部来自 `hccl_rank_graph_dl` 等 dlsym 封装，届时可把本讲的 `HcclRankGraphGet*` 调用与控制面/数据面分离架构对齐，理解「HCCL 算子层不得耦合 HCOMM 控制面内部」这一硬约束。
- **延伸阅读源码**：在 `src/ops/op_common/topo/` 下通读 `topo_match_ubx.cc`、`topo_match_3_level.cc`，对比它们与 `topo_match_multilevel.cc` 在层级处理上的异同，巩固「匹配器家族」的认知。

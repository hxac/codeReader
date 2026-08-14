# 算法执行器 Executor

> 本次更新（HEAD `b16b2eab`）：执行器体系为配合「代价模型选择器（u8）」完成了一次重要扩展——基类 `InsCollAlgBase` 新增 `CalcCostCoeff` / `GetAlgNetMeta` 两个代价申报虚函数；所有注册宏在登记执行器的同时向算法全集 `AllAlgos` 双写登记算法元数据；并新增了 parallel（并行编排）、concurrent（并发编排）、3level（三级编排）等一批新执行器。本讲已同步更新，全部行号与永久链接均已刷新到当前 HEAD。

## 1. 本讲目标

本讲是 op_common 四大组件（selector / executor / template / topo）的第三站。学完本讲，你应当能够：

- 说清 **Executor（算法执行器）** 在整条链路 `Selector → HcclExecOp → Template` 中扮演的角色：它接收 Selector 产出的 `algName`，负责「算资源 + 编排执行」，并**向选择器申报自己的代价**。
- 掌握执行器抽象基类 `InsCollAlgBase` 的核心生命周期：`CalcAlgHierarchyInfo` / `CalcRes` / `Orchestrate` 三个纯虚方法，以及本轮新增的 `CalcCostCoeff` / `GetAlgNetMeta` 两个代价模型申报虚函数。
- 理解 `CollAlgExecRegistryV2` 注册表 + 工厂函数 + 编译期模板绑定 + **`AddAlgToAllAlgos` 双写登记** 的设计：为什么一次注册能同时让「运行期查表」和「新选择器的候选集」都受益。
- 看懂 `HcclExecOp` 如何把 Selector 的产物（字符串 `algName`）一步步变成一次真实的集合通信执行。
- 能读懂 `InsV2AllReduceSequenceExecutor`（顺序编排）与 `InsAllReduceParallelExecutor`（并行编排），理解 **sequence / parallel / concurrent** 三种编排模式的差异，以及它们如何用 `CostAggMode::SUM` / `MAX` 描述自己的代价聚合方式。

本讲承接 [u3-l1 op_common 架构总览](u3-l1-opcommon-overview.md)（四大组件协作与三大注册表）与 [u3-l3 Topo 拓扑适配](u3-l3-topo.md)（`TopoInfoWithNetLayerDetails` 输入与 `AlgHierarchyInfoForAllLevel` 输出），并为 [u3-l5 Template](u3-l5-template.md) 做铺垫——因为执行器的核心工作之一，就是「把模板组织起来」。

---

## 2. 前置知识

阅读本讲前，请确认你已经理解以下概念（在前序讲义中已建立）：

| 概念 | 一句话回顾 | 来源 |
|------|-----------|------|
| `algName` | Selector 产出的字符串契约（如 `CcuMSAllReduceSoleMesh`），是 executor / template 注册表的查表键 | u3-l2 |
| 算法 / 引擎 是正交两个维度 | 算法（Ring/Mesh/NHR…）描述「怎么编排」，引擎（AICPU_TS/AIV/CCU）描述「由谁搬数据」 | u2-l4 |
| 四大组件 | selector 选算法、executor 编排执行并算资源、template 下发 kernel 搬数据、topo 全体共享适配拓扑 | u3-l1 |
| `TopoInfoWithNetLayerDetails` | 物理拓扑快照（`topoLevelNums` / `level0Topo` / `netLayerDetails`） | u3-l3 |
| `AlgHierarchyInfoForAllLevel` | 拓扑匹配器输出的逻辑拓扑，按 `[level][groupIndex] = rank列表` 描述子通信域 | u3-l3 |
| 分级通信 | AllReduce ≈ 节点内 ReduceScatter → 节点间 AllReduce → 节点内 AllGather | u1-l2 |

**三个关键术语补充：**

- **资源（Resource）**：执行一次算法所需的 device 侧对象，包括 thread（执行上下文/任务流）、channel（通信链路）、cclMem（跨 rank 中转缓冲）、notify（同步信号量）等。executor 在执行前必须先「算」出需要多少资源（`AlgResourceRequest`），再由引擎层真正分配出来（`AlgResourceCtxSerializable`）。
- **编排（Orchestrate）**：把多个 template 按「先谁后谁、用哪些 channel、数据从哪搬到哪」组织成一次完整通信。executor 的 `Orchestrate` 方法就是这套编排骨架的具体实现。**编排模式**（sequence 顺序 / parallel 并行 / concurrent 并发）是 executor 的核心分类维度。
- **代价系数（CostModelParam，本轮新增）**：一个三元组 \( (A, B, C) \)——A 描述跨卡传输时间随数据量增长的斜率，B 描述本地拷贝/归约的斜率，C 是固定时延常数项。执行器通过 `CalcCostCoeff` 把「我是由哪几步组成、每步走什么网络、数据占比多少」申报给新选择器（u8-l2 详述），使选择器可以离线估算 \( T(n) = A \cdot n + B \cdot n + C \) 而无需真实跑一遍。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [`src/ops/op_common/executor/executor_v2_base.h`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/executor/executor_v2_base.h) | 执行器抽象基类 `InsCollAlgBase`：三大生命周期纯虚方法 + 本轮新增的 `CalcCostCoeff`/`GetAlgNetMeta` 代价申报虚函数。 |
| [`src/ops/op_common/executor/executor_v2_base.cc`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/executor/executor_v2_base.cc) | `InsCollAlgBase` 的默认实现（`RestoreChannelMap` / `FastLaunch` / `FastLaunchSaveCtxTwoTemplate` 等）。 |
| [`src/ops/op_common/executor/registry/coll_alg_v2_exec_registry.h`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/executor/registry/coll_alg_v2_exec_registry.h) | 注册表 `CollAlgExecRegistryV2` 与一族注册宏；本轮每个宏都追加了 `AddAlgToAllAlgos` 双写登记。 |
| [`src/ops/op_common/executor/registry/coll_alg_v2_exec_registry.cc`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/executor/registry/coll_alg_v2_exec_registry.cc) | 注册表的 `Instance()` 单例、`Register`、`GetAlgExec` 实现。 |
| [`src/ops/op_common/selector/cost_model.h`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/cost_model.h) | 算法全集 `AllAlgos` 与 `AddAlgToAllAlgos` 声明、`CostModelParam` 三元组、`AlgNetMeta` 聚合元数据（本轮新增，本讲只看数据结构）。 |
| [`src/ops/op_common/op_common.cc`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc) | 门面函数 `HcclExecOp` / `HcclGetAlgRes` / `FallbackOp` / `ReSelector`，串联「查注册表 → 算资源 → 编排执行」。 |
| [`src/ops/all_reduce/executor/ins_v2_all_reduce_sequence_executor.h`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/executor/ins_v2_all_reduce_sequence_executor.h) / [`.cc`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/executor/ins_v2_all_reduce_sequence_executor.cc) | 顺序编排执行器 `InsV2AllReduceSequenceExecutor`：4 个模板串行跑完一次分级 AllReduce，本轮新增代价申报实现。 |
| [`src/ops/all_reduce/executor/ins_v2_all_reduce_parallel_executor.h`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/executor/ins_v2_all_reduce_parallel_executor.h) / [`.cc`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/executor/ins_v2_all_reduce_parallel_executor.cc) | **本轮新增**的并行编排执行器 `InsAllReduceParallelExecutor`：每一步框内 mesh 与框间 NHR 同时跑，是 sequence 的「并行化升级版」。 |

> 名字里的 `v2` 表示这是 op_common 的第二代（v2）执行器体系，与早期的 `executor_base.h` 共存。本讲只讲 v2。

---

## 4. 核心概念与源码讲解

### 4.1 InsCollAlgBase 抽象与执行生命周期

#### 4.1.1 概念说明

一个算法要真正跑起来，需要回答三个问题：

1. **我属于哪些子通信域？**（节点内有哪些卡和我一组、节点间又和哪些卡一组）
2. **我需要多少资源？**（几条 thread、几条 channel、多大的中转内存）
3. **数据该怎么一步步搬？**（先搬哪段、后搬哪段、用哪条 channel）

`InsCollAlgBase` 把这三个问题抽象成三个纯虚方法。本轮演进又加了第四个问题——**「我跑一趟大概多贵？」**——由 `CalcCostCoeff` / `GetAlgNetMeta` 两个**带默认空实现**的虚函数回答（默认空实现意味着：未标定的老执行器不申报代价，新选择器会跳过它们）。

| 方法 | 性质 | 调用时机 | 输入 | 输出 |
|------|------|---------|------|------|
| `CalcAlgHierarchyInfo` | 纯虚 | host 侧，资源计算阶段 | `TopoInfoWithNetLayerDetails` | `AlgHierarchyInfoForAllLevel`（逻辑子通信域） |
| `CalcRes` | 纯虚 | host 侧，资源计算阶段 | topoInfo + algHierarchyInfo | `AlgResourceRequest`（资源需求单） |
| `Orchestrate` | 纯虚 | 执行阶段（CCU/AIV 等引擎在 host 调用，AICPU_TS 在 device 内核内调用） | `OpParam` + `AlgResourceCtxSerializable` | 下发若干 template 的 `KernelRun` |
| `CalcCostCoeff`（新增） | 虚，默认返回空 | **选择阶段**，由 `CostModelManager` 遍历 `AllAlgos` 时调用 | comm + topoInfo + algName | `std::vector<CostModelParam>`（每个 template 步骤一组 A/B/C） |
| `GetAlgNetMeta`（新增） | 虚，默认返回空 | 选择阶段，紧跟 `CalcCostCoeff` 之后 | topoInfo | `AlgNetMeta`（各步骤的网络类型 + 代价聚合方式） |

理解这条主线后，再回看三大注册表就豁然开朗：**executor 是「算法 × 引擎 × 拓扑」的编排器，它把 template 当作可复用的搬运积木，并向外声明这套积木组合的代价**。

#### 4.1.2 核心流程

一个 executor 实例的完整生命周期如下（伪代码）：

```
# 1. 注册阶段（程序启动时，静态初始化）—— 本轮起是「双写」
REGISTER_EXECUTOR_BY_FOUR_TEMPS(opType, "DpuAllReduceSequenceMeshNHR",
                                InsV2AllReduceSequenceExecutor, TopoMatchMultilevel,
                                Template0, Template1, Template2, Template3)
   ├─ 编译期把 TopoMatch + 4 个 Template 绑进 Executor 类模板
   ├─ 运行时把 ("ALLREDUCE","DpuAllReduceSequenceMeshNHR") -> creator 登记进 CollAlgExecRegistryV2
   └─ [新增] 同时 AddAlgToAllAlgos(opType, algName, executor名, [4个模板名], 4)
             登记进算法全集 AllAlgos，供新选择器枚举候选

# 2. 选择阶段（新选择器启用时，u8 详述）
for alg in AllAlgos:                       # 遍历算法全集
    exec = Registry.GetAlgExec(opType, alg.algName)
    params = exec->CalcCostCoeff(...)      # 申报 A/B/C（未标定则跳过该算法）
    AlgNetMetaRegistry.Register(algName, exec->GetAlgNetMeta(...))

# 3. 运行期（一次算子调用）
executor = Registry.GetAlgExec(opType, algName)        # 查表，new 出实例（已绑好模板）
executor->CalcAlgHierarchyInfo(comm, topoInfo, hInfo)  # 拓扑切子通信域
executor->CalcRes(comm, param, topoInfo, hInfo, req)   # 算资源需求单
... 引擎层据 req 分配出 resCtx（thread/channel/cclMem）...
executor->Orchestrate(param, resCtx)                   # 编排：依次/并行调 template->KernelRun
```

注意「选择阶段」与「资源计算/执行阶段」是三个不同的时机：代价申报发生在 Selector 内部（算出 algName 之前），而 `CalcAlgHierarchyInfo`/`CalcRes` 与 `Orchestrate` 分属「资源计算阶段」和「执行阶段」，中间隔着「资源真正分配 + 序列化」。

#### 4.1.3 源码精读

抽象基类把三个核心方法声明为纯虚函数，代价申报则给出默认空实现：

> [`executor_v2_base.h:30-62`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/executor/executor_v2_base.h#L30-L62) —— 定义执行器抽象。`CalcAlgHierarchyInfo`/`CalcRes`/`Orchestrate` 三个纯虚方法对应拓扑切分、资源规划、数据编排；上方新增的 `CalcCostCoeff`/`GetAlgNetMeta` 是代价申报钩子。

```cpp
class InsCollAlgBase {
public:
    ...
    // 本轮新增：向代价模型申报 (A, B, C) 系数；默认空实现 = 未标定
    virtual std::vector<CostModelParam>
    CalcCostCoeff(HcclComm comm, TopoInfoWithNetLayerDetails* topoInfo, const char* algName)
    {
        return {};                       // 默认不申报
    }
    // 本轮新增：申报各步骤网络类型与代价聚合方式
    virtual AlgNetMeta GetAlgNetMeta(const TopoInfoWithNetLayerDetails* topoInfo) const
    {
        return {};
    }
    virtual HcclResult CalcAlgHierarchyInfo(...) = 0;   // 阶段一：拓扑切分
    virtual HcclResult CalcRes(...) = 0;                // 阶段二：资源规划
    virtual HcclResult Orchestrate(...) = 0;            // 阶段三：数据编排执行
```

代价相关数据结构定义在选择器侧（u8-l2 展开，这里先认脸）：

> [`cost_model.h:44-48`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/cost_model.h#L44-L48) —— `CostModelParam` 三元组：A 是跨卡传输斜率（受 UB 带宽利用率影响）、B 是本地拷贝/归约斜率、C 是固定时延常数。

> [`cost_model.h:122-143`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/cost_model.h#L122-L143) —— `CostAggMode`（多组 cost 求和 SUM / 取最大值 MAX）与 `AlgNetMeta`：`netTypes` 每个 template 一个、顺序与 A/B/C 一致；`groupSizes` 声明每组的 template 数量。**这个「组」正是编排模式的数学表达：sequence 每组 1 个求和（串行累加），parallel 每组 2 个取 max（并行取最长者）**。

调用点在选择器侧：

> [`cost_model.cc:187-189`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/cost_model.cc#L187-L189) —— `CostModelManager` 遍历算法全集时对每个执行器调用 `exec->CalcCostCoeff(...)`，返回空则打 WARNING 并跳过该算法（未标定算法不进代价表）。

> [`cost_model.cc:202`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/cost_model.cc#L202) —— 紧接着把 `exec->GetAlgNetMeta(topoInfo)` 的结果登记进 `AlgNetMetaRegistry`，供代价聚合时查询。

基类还提供共享工具方法，避免每个子类重复实现：

> [`executor_v2_base.cc:26-42`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/executor/executor_v2_base.cc#L26-L42) —— `RestoreChannelMap` 把 `resCtx.channels`（按 level 组织的通道列表）重整成「`remoteRank -> channel 列表`」的映射，供 `Orchestrate` 阶段按远端 rank 与层级取通道。

> [`executor_v2_base.cc:53-59`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/executor/executor_v2_base.cc#L53-L59) —— `FastLaunch` 默认实现是「不支持」并返回错误：FastLaunch 是 CCU 引擎专属的快速路径，绝大多数执行器不需要它，所以基类给了默认拒绝的实现。

#### 4.1.4 代码实践

**实践目标**：用源码定位法，确认「三大生命周期方法」与「两个代价申报方法」分别被谁调用、何时调用。

**操作步骤**：

1. 打开 [`src/ops/op_common/op_common.cc`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc)，搜索 `executor->CalcAlgHierarchyInfo`、`executor->CalcRes`、`executor->Orchestrate` 三处调用点，记录所在函数与行号。
2. 打开 [`src/ops/op_common/selector/cost_model.cc`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/cost_model.cc)，搜索 `exec->CalcCostCoeff`、`exec->GetAlgNetMeta`，确认它们在哪个管理器方法里被调用。

**预期结果**（待本地核对行号）：

- `CalcAlgHierarchyInfo`（`op_common.cc:1204` 与 `775`）与 `CalcRes`（`op_common.cc:1208` 与 `778`）出现在 `HcclGetAlgRes` / `GeReuseResource`（资源计算阶段）。
- `Orchestrate` 出现在 `HcclExecOp` 的引擎分发分支里（CCU 分支 `752` 行、`else` 默认分支 `760` 行）。
- `CalcCostCoeff` / `GetAlgNetMeta` 出现在 `cost_model.cc` 的 `CostModelManager` 初始化流程里（选择阶段），与执行链路完全解耦。

**观察到的现象**：五个方法分布在三个不同文件、三个不同阶段——这正是「选择（申报代价）→ 资源计算 → 执行」三段式分离的直接证据。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `CalcRes` 的输出叫「Request（需求单）」而不是直接叫「Resource（资源）」？

> **参考答案**：因为 `CalcRes` 只 **声明** 需要「几个 thread、几条 channel、多大内存」，并不真正分配。真正分配由引擎层（`GetAlgResWithEngine`）依据这份需求单完成，产出 `AlgResourceCtxSerializable`。这种「先规划、后分配」的分离，让执行器只关心算法需要什么，不必关心不同引擎的分配细节。

**练习 2**：`CalcCostCoeff` 为什么给默认空实现而不是像 `CalcRes` 那样设成纯虚？

> **参考答案**：因为代价模型是本轮新增能力，存量执行器数量庞大、不可能一次全部标定。默认空实现让未标定的执行器照常编译、照常被旧选择器使用；新选择器侧只需在 `cost_model.cc:189` 检查返回值为空就跳过它（打 `uncalibrated` WARNING）。这是「增量演进出新能力」的典型手法——虚函数默认实现 + 调用方空值检查。

**练习 3**：`Orchestrate` 的签名只接收 `AlgResourceCtxSerializable`，为什么 executor 自己不去重新算 `algHierarchyInfo`？

> **参考答案**：因为 `algHierarchyInfo` 已经在「资源计算阶段」算好，并随 `resCtx` 一起被序列化保存（`AlgResourceCtxSerializable::algHierarchyInfo` 字段）。执行阶段只需从 `resCtx` 里取回（见 4.4.3 中 `algHierarchyInfo_ = resCtx.algHierarchyInfo`），避免重复计算拓扑切分。

---

### 4.2 CollAlgExecRegistryV2 注册表与 AddAlgToAllAlgos 双写登记

#### 4.2.1 概念说明

回顾 u3-l1：Selector 产出的 `algName` 是个字符串。`HcclExecOp` 拿到这个字符串后，要把它「变回一个活生生的执行器对象」——这就是 `CollAlgExecRegistryV2` 注册表的职责。

它解决三个问题：

1. **查表**：给定 `(opType, algName)`，怎么拿到对应的执行器实例？
2. **绑定**：一个执行器需要绑定「拓扑匹配器」和「若干算法模板」，怎么在注册时就把它们绑死？
3. ****（本轮新增）**枚举**：新选择器需要知道「这个算子一共有哪些算法可用」，怎么在不 new 任何执行器的情况下拿到全集？——答案就是注册宏双写 `AddAlgToAllAlgos`。

第二个问题仍是这套设计最巧妙的地方：**executor 是 C++ 类模板，它的模板参数（拓扑匹配器 + N 个 template 类型）在注册宏展开时就已经确定**。运行时查表拿到的，是一个「模板参数全部特化完毕、可以直接 new」的工厂函数。而第三个问题在注册宏里顺手解决：宏参数里的 `#name`、`#insCollAlgBase`、`#InsAlgTemplateN` 本来就是字符串化的，把它们原样登记进 `AllAlgos`，**注册表与候选集天然同步（单一事实来源）**——不存在「注册了执行器但选择器不知道」的漂移。

#### 4.2.2 核心流程

注册表用经典的「**单例 + 二级 map + 工厂函数 + 静态初始化**」四件套：

```
CollAlgExecRegistryV2（单例）                     AllAlgos（算法全集，本轮新增双写目标）
  └─ execCreators_: map< opType, map< algName, Creator > >    └─ AlgElement{ algName, executorName,
              │                                                            templateName[], templateNum, opType }
              └─ Creator = std::function<InsCollAlgBase*()>
                  = DefaultExecCreatorV2<已特化的Executor类型>
```

注册时机靠 **静态变量初始化**：每个 `REGISTER_*` 宏展开成两个全局静态变量——`g_func_##name`（触发注册表登记）和 `g_alg_##name`（触发 AllAlgos 登记）——程序启动时先后执行。

宏族按绑定的模板个数分工：

| 宏 | 绑定内容 | AllAlgos 登记的模板数 |
|----|---------|----------------------|
| `REGISTER_EXECUTOR_IMPL` | 仅执行器类型 | 0（`nullptr, 0`） |
| `REGISTER_EXECUTOR_BY_TOPO` | 拓扑匹配器 | 0 |
| `REGISTER_EXEC_V2` | 拓扑匹配器 + **1 个**模板 | 1 |
| `REGISTER_EXECUTOR_BY_TWO_TEMPS` | 拓扑匹配器 + **2 个**模板 | 2 |
| `REGISTER_EXECUTOR_BY_FOUR_TEMPS` | 拓扑匹配器 + **4 个**模板 | 4 |
| `REGISTER_EXEC_V2_MULTI` | 拓扑匹配器 + **任意**模板 | N（`ALG_GET_COUNT` 计数） |

#### 4.2.3 源码精读

注册表本身是个线程安全的单例 + 二级 map：

> [`coll_alg_v2_exec_registry.h:22-31`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/executor/registry/coll_alg_v2_exec_registry.h#L22-L31) —— `CollExecCreatorV2` 是返回 `InsCollAlgBase*` 的工厂函数；`DefaultExecCreatorV2<P>()` 是默认工厂，`new P()`。P 是已特化好的执行器具体类型。

`AllAlgos` 与 `AddAlgToAllAlgos` 的声明在选择器侧：

> [`cost_model.h:25-42`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/cost_model.h#L25-L42) —— `AlgElement` 记录单个算法的元数据（算法名、执行器名、模板名数组、模板数、算子类型）；`AllAlgos` 是其动态数组；`AddAlgToAllAlgos` 是登记入口。

四模板注册宏的展开（本讲最需要看懂的一段宏）：

> [`coll_alg_v2_exec_registry.h:126-135`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/executor/registry/coll_alg_v2_exec_registry.h#L126-L135) —— `REGISTER_EXECUTOR_BY_FOUR_TEMPS_HELPER`：先把 4 个模板类型字符串化进 `g_alg_templates_[]` 数组，再 `Register(...)` 登记执行器工厂，最后 `AddAlgToAllAlgos(type, #name, #insCollAlgBase, g_alg_templates_..., 4)` 双写登记算法元数据。

```cpp
#define REGISTER_EXECUTOR_BY_FOUR_TEMPS_HELPER(ctr, type, name, insCollAlgBase, \
    AlgTopoMatch, InsAlgTemplate0, InsAlgTemplate1, InsAlgTemplate2, InsAlgTemplate3) \
    static const char* g_alg_templates_##name##_##ctr[]                          \
        = {#InsAlgTemplate0, #InsAlgTemplate1, #InsAlgTemplate2, #InsAlgTemplate3}; \
    static HcclResult g_func_##name##_##ctr = CollAlgExecRegistryV2::Instance().Register( \
        type, std::string(#name),                                                \
        DefaultExecCreatorV2<insCollAlgBase<AlgTopoMatch, InsAlgTemplate0,        \
            InsAlgTemplate1, InsAlgTemplate2, InsAlgTemplate3>>);                 \
    static HcclResult g_alg_##name##_##ctr = AddAlgToAllAlgos(                    \
        type, #name, #insCollAlgBase, g_alg_templates_##name##_##ctr, 4)
```

同一模式贯穿全族，例如单模板宏：

> [`coll_alg_v2_exec_registry.h:84-89`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/executor/registry/coll_alg_v2_exec_registry.h#L84-L89) —— `REGISTER_EXECUTOR_HELPER`：`REGISTER_EXEC_V2` 的底层，同样先 `Register` 再 `AddAlgToAllAlgos(..., 1)`；不绑模板的 `REGISTER_EXECUTOR_BY_TOPO_HELPER`（97-100 行）则登记 `(nullptr, 0)`。

> 宏里反复出现的 `__COUNTER__`、`_HELPER_1` 层层展开，只是为了给每个注册生成 **唯一的静态变量名**（`g_func_##name##_##ctr` / `g_alg_##name##_##ctr`），防止多个注册互相覆盖。`#name` 把第二个参数转成字符串作为查表键。

注册与查找的实现都很简短：

> [`coll_alg_v2_exec_registry.cc:15-31`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/executor/registry/coll_alg_v2_exec_registry.cc#L15-L31) —— `Instance()` 返回函数内静态单例；`Register` 用 `mutex` 保护、重复注册报错。

> [`coll_alg_v2_exec_registry.cc:33-41`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/executor/registry/coll_alg_v2_exec_registry.cc#L33-L41) —— `GetAlgExec` 查表并 `new` 出实例；找不到返回 `nullptr`（DEBUG 日志记录未注册的 tag）。

#### 4.2.4 代码实践

**实践目标**：搞清楚一次 `REGISTER_EXECUTOR_BY_FOUR_TEMPS` 到底产生了哪两笔登记。

**操作步骤**：

1. 在 [`ins_v2_all_reduce_sequence_executor.cc:511-514`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/executor/ins_v2_all_reduce_sequence_executor.cc#L511-L514) 找到注册调用，把它代入 4.2.3 的宏，手写出两个静态变量的完整展开。
2. 写出第一笔登记的键值对：`("HCCL_CMD_ALLREDUCE", "DpuAllReduceSequenceMeshNHR") -> DefaultExecCreatorV2<InsV2AllReduceSequenceExecutor<TopoMatchMultilevel, ...4个模板...>>`。
3. 写出第二笔登记的内容：`AddAlgToAllAlgos(HCCL_CMD_ALLREDUCE, "DpuAllReduceSequenceMeshNHR", "InsV2AllReduceSequenceExecutor", {"InsTempReduceScatterMesh1DIntra", "InsTempReduceScatterMesh1dDpuInter", "InsTempAllGatherNhrDpuInter", "InsTempAllGatherMesh1dIntra"}, 4)`。
4. 再全局搜索 `REGISTER_EXECUTOR_BY_FOUR_TEMPS`，统计 all_reduce 目录下共有几处（提示：sequence、parallel、sequence_aicpu、sequence_aicpu_3level 等文件里都有）。

**预期结果**：你会发现每处注册都自动完成了两笔登记，无需开发者额外写一行「向选择器登记算法」的代码——这就是 `AddAlgToAllAlgos` 双写设计的收益（受益方是 u8 的新选择器 SelectorEngine）。

#### 4.2.5 小练习与答案

**练习 1**：注册宏为什么要在登记 executor 的同时把模板名字符串化存进 `AllAlgos`，而不是让新选择器自己去扫源码目录或维护一份手写清单？

> **参考答案**：因为注册宏是唯一同时知道「算法名、执行器类型、模板列表」的地方。在这里登记，候选集与注册表 **天然同步**——新增一个 `REGISTER_*` 调用，执行器可查、算法可枚举，二者永远不会漂移。若另维护手写清单，每加一个算法要改两处，极易出现「选择器推荐了一个查不到执行器的算法」或「注册了却永远选不上」的不一致。

**练习 2**：如果 Selector 输出的 `algName` 在注册表里查不到，会发生什么？

> **参考答案**：`GetAlgExec` 返回 `nullptr`，`HcclExecOp`（`op_common.cc:663-665`）会用 `CHK_PRT_RET` 报 `Fail to find executor for algName[...]` 并返回 `HCCL_E_PARA`。这正是为什么 Selector 产出的 `algName` 必须与 executor 注册端的字符串 **严格一致**——少一个字母都会导致查表失败。

---

### 4.3 HcclExecOp：从 algName 到执行的全过程

#### 4.3.1 概念说明

`HcclExecOp` 是 op_common 对外的第二个门面函数（第一个是 `Selector`）。它接收 Selector 的产物——`algName` 字符串——把它变成一次真实的集合通信执行。职责拆成三步：

1. **查表实例化**：用 `(opType, algName)` 从注册表拿到 executor。
2. **算资源（或复用资源）**：调用 `HcclGetAlgRes`，内部依次跑 `CalcAlgHierarchyInfo` + `CalcRes`，再由引擎层分配出 `resCtx`。若该算法的资源已缓存（同 algTag 再下发），则直接复用、跳过计算。
3. **引擎分发执行**：按 `param.engine` 进入不同分支，最终调用 `executor->Orchestrate(param, resCtx)`（CCU/AIV-cache/默认分支），或走 AICPU_TS 专属的内核下发路径。

#### 4.3.2 核心流程

```
HcclExecOp(comm, param, topoInfo, algName, resPack)
  │
  ├─ [回退缓存] HcclEngineCtxGet(fallbackTag) 命中 → 重设为 AICPU_TS，递归调用 HcclExecOp
  │
  ├─ executor = CollAlgExecRegistryV2::GetAlgExec(opType, algName)   # 查表
  │
  ├─ HcclGetAlgRes(comm, param, executor, topoInfo, resCtxHost, ...) # 算资源
  │     ├─ TryReuseResource: 若 algTag 已有缓存 ctx → isResourceReused=true，直接返回
  │     └─ (未命中) executor->CalcAlgHierarchyInfo(...)
  │                  executor->CalcRes(...)            -> AlgResourceRequest
  │                  GetAlgResWithEngine(...)           -> 按 req 分配 thread/channel/cclMem
  │                  │  若返回 HCCL_E_UNAVAIL（资源不足）→ FallbackOp 重新选算法
  │
  ├─ ConstructHcclDfxOpInfo(...)                                     # 打点信息
  │
  └─ 按 param.engine 分发：
       ├─ AICPU_TS / CPU: HcclAicpuKernelEntranceLaunch(...)         # 内核下发路径
       ├─ AIV:            HcclAivKernelEntranceLaunch + ExecuteAivCacheLogic
       │                        └─ executor->Orchestrate(param, resCtx)  # 在 cache 逻辑内调用
       ├─ CCU:            executor->Orchestrate(param, resCtx)
       └─ else:           executor->Orchestrate(param, resCtx)
```

两个值得记住的设计：

- **资源复用**：同一通信域反复下发同一种算法（相同 algTag）时，资源只算一次，之后直接从 engineCtx 取回反序列化（`TryReuseResource` + CCU/else 分支里的 `DeSerialize`）。
- **资源不足回退**：`CalcRes` 算出的需求若引擎层无法满足（返回 `HCCL_E_UNAVAIL`），`FallbackOp` 会 `ReSelector` 重选并回退到 AICPU_TS，体现「AICPU 兜底」原则（u3-l2 已讲）。`ReSelector` 内部同样有新旧选择器双路径分支（`op_common.cc:588-593`），与 u3-l1 讲的 `Selector` 保持一致。

#### 4.3.3 源码精读

`HcclExecOp` 函数主体：

> [`op_common.cc:627-646`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L627-L646) —— `HcclExecOp` 入口：先查回退缓存（`HcclEngineCtxGet` 命中则重设 AICPU_TS 并递归），再把 `algName` 写入 `param`。

> [`op_common.cc:663-665`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L663-L665) —— `GetAlgExec(param.opType, algName)` 实例化执行器；查不到则报错返回。

```cpp
std::unique_ptr<InsCollAlgBase> executor =
    CollAlgExecRegistryV2::Instance().GetAlgExec(param.opType, algName);
CHK_PRT_RET(executor.get() == nullptr,
            HCCL_ERROR("Fail to find executor for algName[%s]", algName.c_str()), HCCL_E_PARA);
```

资源计算（含未命中时调用两大生命周期方法）：

> [`op_common.cc:682-690`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L682-L690) —— `HcclGetAlgRes` 返回 `HCCL_E_UNAVAIL` 时触发 `FallbackOp` 回退重选。

> [`op_common.cc:1186-1215`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L1186-L1215) —— `HcclGetAlgRes` 内部：先 `TryReuseResource`（1153-1184 行定义，命中即提前 return），未命中才依次调用 `executor->CalcAlgHierarchyInfo`（1204 行）与 `executor->CalcRes`（1208 行），再交 `GetAlgResWithEngine` 分配。

引擎分发执行：

> [`op_common.cc:712-761`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L712-L761) —— 按 `param.engine` 分发。AICPU_TS/CPU 走 `HcclAicpuKernelEntranceLaunch`（719 行）内核下发；AIV 走 `HcclAivKernelEntranceLaunch` + `ExecuteAivCacheLogic`（726-727 行，其内部调 `Orchestrate`）；CCU 分支（729-752 行）在资源复用时先 `DeSerialize` 反序列化再调 `executor->Orchestrate(param, *resCtxHost)`（752 行）；`else` 默认分支同样调 `Orchestrate`（760 行）。

> [`op_common.cc:560-576`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L560-L576) —— `FallbackOp`：资源不足时经 `ReSelector` 重选算法、把新算法名写进 fallbackCtx 缓存，再递归调 `HcclExecOp`。

> **注意区分引擎差异**：`Orchestrate` 并非在所有引擎都被 host 直接调用。AICPU_TS 引擎把资源序列化拷到 device 后，通过 `HcclLaunchAicpuKernel` 在 AICPU 上跑一个内核，执行器逻辑（含 `Orchestrate`）在 device 内核内执行；而 CCU、AIV 等引擎则在 host 侧直接调用 `executor->Orchestrate`。引擎内部细节属于 Unit 5 的主题，本讲只需记住「`Orchestrate` 是执行器对外暴露的编排入口」。

#### 4.3.4 代码实践

**实践目标**：跟踪 `HcclExecOp` 的三步骨架，验证「查表 → 算资源 → 编排」的顺序。

**操作步骤**：

1. 打开 [`op_common.cc`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc)，定位 `HcclExecOp`（627 行起）。
2. 向下读，依次确认：① `GetAlgExec`（663 行）；② `HcclGetAlgRes`（683 行）；③ 各引擎分支里 `executor->Orchestrate(...)`（752、760 行）。
3. 跳到 `HcclGetAlgRes`（1186 行起），确认 `TryReuseResource` 命中（1195 行）会提前 return，跳过 `CalcAlgHierarchyInfo`/`CalcRes`（1204、1208 行）。

**预期结果**：你会清楚地看到 `Orchestrate` 总是在 `CalcRes` 之后被调用，且二者之间夹着「引擎层资源分配」。同时 `TryReuseResource` 命中时（`isResourceReused==true`）会直接 return，省下重复算资源的开销。

#### 4.3.5 小练习与答案

**练习 1**：`HcclExecOp` 一开头为什么要先查 `fallbackCtx`？

> **参考答案**：这是回退结果的 **持久化记忆**。如果某个通信域的某个算法曾经因资源不足等原因回退到 AICPU_TS，`FallbackOp` 会把回退后的算法名写进 fallbackCtx；后续再次下发同一算法时直接读出，省去「先尝试 → 再失败 → 再回退」的重复过程。体现了「同 tag 的拓扑/资源/回退结果都会被缓存」的整体设计。

**练习 2**：`GetAlgExec` 返回的 `executor` 用 `std::unique_ptr` 持有，意味着什么？

> **参考答案**：每次算子调用都会 `new` 一个全新的执行器实例，函数结束时自动析构。执行器对象本身是「一次性」的轻量编排器，它不持有 device 资源（资源挂在 `resCtx` 上，生命周期由通信域管理）。这种设计避免了执行器实例间的状态串扰——代价模型那侧（4.1.3）同样借用了这一点：`CalcCostCoeff` 也是临时 new 出来调用再销毁的。

---

### 4.4 InsV2AllReduceSequenceExecutor：四模板顺序编排

#### 4.4.1 概念说明

前三个模块讲的都是「骨架」，本模块看「血肉」——AllReduce 算法的一个具体执行器 `InsV2AllReduceSequenceExecutor`。它对应的 `algName` 是 `DpuAllReduceSequenceMeshNHR`，注册时绑定了 **4 个 AICPU 模板** 和多级拓扑匹配器 `TopoMatchMultilevel`。

它的核心思想直接对应 u1-l2 讲过的分级通信：一次 AllReduce 被拆成 4 个**严格串行**的步骤：

```
节点内(Layer0) ReduceScatter  →  节点间(Layer1) ReduceScatter
        →  节点间(Layer1) AllGather  →  节点内(Layer0) AllGather
```

前两步是 ReduceScatter（把每张卡的全量数据切成 N 段，归约后每卡只留一段）；后两步是 AllGather（把归约后的段重新拼回全量）。这正是 `AllReduce ≈ ReduceScatter + AllGather` 的工程实现，且每个原语都做了「节点内 / 节点间」两级拆分。代码里把「节点内」叫 **框内（intra）**，对应 Layer0；把「节点间」叫 **框间（inter / dpu）**，对应 Layer1。

本轮该执行器还补齐了代价申报：`GetAlgNetMeta` 声明 `groupSizes = {1,1,1,1}` 且聚合方式为 `SUM`——**4 步串行，总代价 = 4 步代价之和**，与编排语义严丝合缝。

#### 4.4.2 核心流程

执行器类模板声明（5 个模板参数）：

```
InsV2AllReduceSequenceExecutor<
    AlgTopoMatch,        # 拓扑匹配器，如 TopoMatchMultilevel
    InsAlgTemplate0,     # 框内 ReduceScatter   (Layer0)
    InsAlgTemplate1,     # 框间 ReduceScatter   (Layer1)
    InsAlgTemplate2,     # 框间 AllGather       (Layer1)
    InsAlgTemplate3>     # 框内 AllGather       (Layer0)
```

三个生命周期方法如何各司其职：

| 方法 | 做什么 |
|------|--------|
| `CalcAlgHierarchyInfo` | 构造拓扑匹配器实例，调 `MatchTopo` 把物理拓扑切成两级子通信域 |
| `CalcRes` | 实例化 4 个 template，分别调各自的 `CalcRes` 汇总出「取最大值」的资源需求单 |
| `Orchestrate` | 把 cclMem 切成 ccl-in / ccl-out 两半，按 loop 循环依次调 4 个 template 的 `KernelRun` |
| `CalcCostCoeff`（本轮新增） | 对 4 个模板各调一次静态 `CalcCostCoeff`，产出 4 组 A/B/C（数据占比均 `1/rankSize`） |
| `GetAlgNetMeta`（本轮新增） | 声明 4 步网络类型 MESH/CLOS/MESH/CLOS，`SUM` 聚合，每组 1 个 |

`OrchestrateLoop` 的数据流向（注意 buffer 在 ccl-in / ccl-out / user-out 之间流转）：

```
user-input ──StepOne(RS框内)──> ccl-out ──StepTwo(RS框间)──> ccl-out
                ccl-in(暂存)                  ccl-in(暂存)
        ──StepThree(AG框间)──> ccl-out ──StepFour(AG框内)──> user-output
              ccl-in(暂存)                   ccl-in(暂存)
```

当数据量超过中转内存单次容量时，外层 `for` 循环会把数据分多轮（`loopTimes`）处理，每轮完整跑一遍 4 个步骤。

#### 4.4.3 源码精读

类模板声明，本轮新增两个 override：

> [`ins_v2_all_reduce_sequence_executor.h:28-70`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/executor/ins_v2_all_reduce_sequence_executor.h#L28-L70) —— 类模板声明，5 个模板参数；声明 `CalcCostCoeff`/`GetAlgNetMeta`（36-38 行）与 `Orchestrate`/`CalcRes`/`CalcAlgHierarchyInfo` 三个 override，以及内部的 `OrchestrateLoop`/`InitCommInfo`/`SplitData`。

**代价申报（本轮新增）**：

> [`ins_v2_all_reduce_sequence_executor.cc:22-50`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/executor/ins_v2_all_reduce_sequence_executor.cc#L22-L50) —— `CalcCostCoeff`：先用 `CostModelManager::CalcRankSizeByTopo` 算出两级 rankSize，再对 4 个模板各调静态 `InsAlgTemplateN::CalcCostCoeff`。框内步骤传 `AlgNetType::MESH`、框间传 `CLOS`；每步数据占比 `n = 1/rankSize`（ReduceScatter 后每卡只剩一份）；`needLocalCopy=true` 表示需要计本地拷贝项。

> [`ins_v2_all_reduce_sequence_executor.cc:55-68`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/executor/ins_v2_all_reduce_sequence_executor.cc#L55-L68) —— `GetAlgNetMeta`：`netTypes = {MESH, CLOS, MESH, CLOS}`，`intraGroupMode = CostAggMode::SUM`，`groupSizes = {1,1,1,1}`——4 步串行、代价求和。

**阶段一 `CalcAlgHierarchyInfo`**：构造匹配器并切拓扑。

> [`ins_v2_all_reduce_sequence_executor.cc:106-118`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/executor/ins_v2_all_reduce_sequence_executor.cc#L106-L118) —— `AlgTopoMatch topoMatch; topoMatch.MatchTopo(comm, topoInfo, algHierarchyInfo);`。`AlgTopoMatch` 是模板参数，实例化为 `TopoMatchMultilevel`，其 `MatchTopo` 即 u3-l3 讲的多级匹配，产出两级子通信域。

**阶段二 `CalcRes`**：实例化 4 个 template，各自 `CalcRes` 后取最大值汇总。

> [`ins_v2_all_reduce_sequence_executor.cc:123-173`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/executor/ins_v2_all_reduce_sequence_executor.cc#L123-L173) —— 4 个 template 用 `algHierarchyInfo.infos[0]`（Layer0）或 `infos[1]`（Layer1）构造（135-142 行），分别 `CalcRes`（148-151 行）；153-169 行做 **max 汇总**——源码注释明说「step1、2、3、4为串行，因此slaveThread和对应notify可以复用」，thread/notify 取各步需求的 **max**，channels 只保留 Layer0、Layer1 各一份（171 行）。

**阶段三 `Orchestrate`**：填充成员后调用 `OrchestrateLoop`。

> [`ins_v2_all_reduce_sequence_executor.cc:178-211`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/executor/ins_v2_all_reduce_sequence_executor.cc#L178-L211) —— `Orchestrate` 从 `resCtx` 取回 `algHierarchyInfo_`/`threads_`（192-193 行），用 `rankIdxLevel0_ = myRank_ % size0`、`rankIdxLevel1_ = myRank_ / size0` 算出自己在两级子组中的下标（195-196 行），调 `RestoreChannelMap`（基类工具，见 4.1.3）重整通道，最后进入 `OrchestrateLoop`。

**四个 `KernelRun` 步骤**（本讲实践任务的重点）：

> [`ins_v2_all_reduce_sequence_executor.cc:302-336`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/executor/ins_v2_all_reduce_sequence_executor.cc#L302-L336) —— **步骤一**：`algTemplateStepOne->KernelRun(...)`（336 行），`InsAlgTemplate0` = 框内（Layer0）ReduceScatter，数据从 `param.inputPtr`（user-in）搬到 `cclOutMem`，用 `remoteRankToChannelInfo_[0]`（Layer0 通道）。

> [`ins_v2_all_reduce_sequence_executor.cc:338-376`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/executor/ins_v2_all_reduce_sequence_executor.cc#L338-L376) —— **步骤二**：`algTemplateStepTwo->KernelRun(...)`（375 行），`InsAlgTemplate1` = 框间（Layer1）ReduceScatter，数据在 `cclOutMem` 内就地归约搬运，用 `remoteRankToChannelInfo_[1]`（Layer1 通道）。

> [`ins_v2_all_reduce_sequence_executor.cc:378-410`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/executor/ins_v2_all_reduce_sequence_executor.cc#L378-L410) —— **步骤三**：`algTemplateStepThree->KernelRun(...)`（409 行），`InsAlgTemplate2` = 框间（Layer1）AllGather，复用步骤二的切片信息（`allRankSliceSize` 等），仍用 Layer1 通道。

> [`ins_v2_all_reduce_sequence_executor.cc:412-442`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/executor/ins_v2_all_reduce_sequence_executor.cc#L412-L442) —— **步骤四**：`algTemplateStepFour->KernelRun(...)`（442 行），`InsAlgTemplate3` = 框内（Layer0）AllGather，数据从 `cclOutMem` 搬回 `param.outputPtr`（user-out），用 Layer0 通道。

`SplitData`（数据切分）：

> [`ins_v2_all_reduce_sequence_executor.cc:466-509`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/executor/ins_v2_all_reduce_sequence_executor.cc#L466-L509) —— `SplitData` 用 `RoundUp(dataCount, rankSize)` 把数据均分成 `rankSize` 份（最后一份可能不满），产出每份的 `allRankSliceSize / allRankDispls / allRankProcessedDataCount`，供各 template 知道「每个 rank 对应哪一段」。

**注册宏调用**：

> [`ins_v2_all_reduce_sequence_executor.cc:511-514`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/executor/ins_v2_all_reduce_sequence_executor.cc#L511-L514) —— `REGISTER_EXECUTOR_BY_FOUR_TEMPS(HCCL_CMD_ALLREDUCE, DpuAllReduceSequenceMeshNHR, InsV2AllReduceSequenceExecutor, TopoMatchMultilevel, InsTempReduceScatterMesh1DIntra, InsTempReduceScatterMesh1dDpuInter, InsTempAllGatherNhrDpuInter, InsTempAllGatherMesh1dIntra)`。

把宏的 4 个 template 实参与 4 个步骤对齐，就是本讲的「对照表」：

| 注册宏实参 | 头文件（`template/aicpu/`） | 步骤 | 层级 | 原语 |
|-----------|---------------------------|------|------|------|
| `InsTempReduceScatterMesh1DIntra` | `ins_temp_reduce_scatter_mesh_1D_intra.h` | 步骤一 | Layer0（框内/节点内） | ReduceScatter |
| `InsTempReduceScatterMesh1dDpuInter` | `ins_temp_reduce_scatter_mesh_1D_dpu_inter.h` | 步骤二 | Layer1（框间/节点间） | ReduceScatter |
| `InsTempAllGatherNhrDpuInter` | `ins_temp_all_gather_nhr_dpu_inter.h` | 步骤三 | Layer1（框间/节点间） | AllGather |
| `InsTempAllGatherMesh1dIntra` | `ins_temp_all_gather_mesh_1D_intra.h` | 步骤四 | Layer0（框内/节点内） | AllGather |

> 这 4 个模板都位于 `src/ops/all_reduce/template/aicpu/` 下，是 AICPU 引擎的模板（template 的内部机制是 u3-l5 与 Unit 5 的主题）。

#### 4.4.4 代码实践

**实践目标**：在 `ins_v2_all_reduce_sequence_executor.cc` 中定位 `REGISTER_EXECUTOR_BY_FOUR_TEMPS` 注册，说明 `OrchestrateLoop` 中四个 `KernelRun` 步骤分别对应 ReduceScatter/AllGather 的哪一级（节点内/节点间）——这是本讲的指定实践任务。

**操作步骤**：

1. 打开 [`ins_v2_all_reduce_sequence_executor.cc`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/executor/ins_v2_all_reduce_sequence_executor.cc)，跳到文件末尾（511-514 行），确认注册宏把 4 个模板类型按 `0/1/2/3` 的顺序绑定。
2. 回到 `OrchestrateLoop`（218 行起），依次找到 4 处 `->KernelRun(...)` 调用（336、375、409、442 行），记下每处用的模板变量名（`algTemplateStepOne/Two/Three/Four`）。
3. 对每个 template，看它构造时传的是 `algHierarchyInfo_.infos[0]`（Layer0=节点内）还是 `infos[1]`（Layer1=节点间），以及它用的是哪一层的 channel（`remoteRankToChannelInfo_[0]` 或 `[1]`）。
4. 对比步骤之间有没有任何同步（`Pre/PostSync`、notify）——答案是没有，因为 4 步在同一个 thread 序列里天然串行。

**需要观察的现象**：

- 步骤一（`algTemplateStepOne`）和步骤四（`algTemplateStepFour`）都用 `infos[0]` 构造、用 `remoteRankToChannelInfo_[0]`——**节点内**。
- 步骤二（`algTemplateStepTwo`）和步骤三（`algTemplateStepThree`）都用 `infos[1]` 构造、用 `remoteRankToChannelInfo_[1]`——**节点间**。
- 步骤一二是 ReduceScatter，三四是 AllGather；步骤二、三外面有 `if (tempAlgParamsStepTwo.count != 0)` 守卫——本框该卡没分到数据时跳过框间阶段。
- buffer 流向：步骤一读 user-in 写 ccl-out；步骤二、三在 ccl-out 内就地；步骤四读 ccl-out 写 user-out。

**预期结果**：四个步骤的「原语 × 层级」归纳为「RS-节点内 → RS-节点间 → AG-节点间 → AG-节点内」，与 u1-l2 的分级 AllReduce 模型完全吻合。若结论与之不符，请重新核对模板变量与 `infos` 下标的对应。

> 待本地验证：本实践为源码阅读型实践，结论可由静态阅读得出；若要在运行期验证，需 NPU 环境编译并跑 AllReduce 样例，开启日志后搜索 `[InsV2AllReduceSequenceExecutor][OrchestrateLoop]` 的输出顺序。

#### 4.4.5 小练习与答案

**练习 1**：`CalcRes` 里 4 个 template 的 `slaveThreadNum` 为什么要取 `max` 而不是求和？

> **参考答案**：因为 4 个步骤是 **顺序串行** 执行的，同一时刻只有一个 template 在用 thread，thread 资源可在步骤间复用，取 4 步里需求最大的那个即可覆盖所有步骤（源码 153 行注释直说了这一点）。若是 4 步 **并行**，才需要求和——4.5 的 parallel 扽行器正是求和的实例。

**练习 2**：`GetAlgNetMeta` 返回 `groupSizes = {1,1,1,1}` 且 `CostAggMode::SUM`，这在数学上表达了什么？

> **参考答案**：把编排抽象成「若干组模板、组内按 `intraGroupMode` 聚合、组间默认求和」。sequence 每组只有 1 个模板、共 4 组，SUM 聚合即总耗时 \( T = T_1 + T_2 + T_3 + T_4 \)——严格串行流水线的代价模型。对照 4.5 的 parallel：每组 2 个模板、MAX 聚合，即 \( T = \max(T_{mesh}, T_{clos}) \) 段间求和——并行执行的代价模型。**编排语义被精确编码进了代价元数据**。

**练习 3**：如果把 `OrchestrateLoop` 里 4 个 `KernelRun` 的顺序打乱（比如先 AG 后 RS），会发生什么？

> **参考答案**：结果会错误。这 4 步是有严格数据依赖的顺序流水线：必须先把全量数据 ReduceScatter 成「每卡一段」，归约后才能 AllGather 拼回全量。打乱顺序会读到尚未归约的数据，产生错误结果。这也说明 executor 的「编排」本身就是算法语义的一部分。

---

### 4.5 InsAllReduceParallelExecutor 与编排模式对比（本轮新增）

#### 4.5.1 概念说明

sequence 执行器的痛点在于：4 步完全串行，任何时刻只有一条链路（框内 mesh 或框间 NHR）在搬数据，另一条闲着。本轮新增的 `InsAllReduceParallelExecutor`（并行编排）把数据**按比例拆成两份**，让框内 mesh 与框间 NHR **同时** 各搬一份：

```
sequence（串行）：  RS框内 ──> RS框间 ──> AG框间 ──> AG框内        （一步接一步）
parallel（并行）：  每步 = [框内 mesh ∥ 框间 NHR] 两条腿同时跑
   step1: [RS mesh(数据0) ∥ RS nhr(数据1)]
   step2: [RS nhr(数据0)  ∥ RS mesh(数据1)]   ← 两份数据交换链路
   step3: [AG nhr(数据0)  ∥ AG mesh(数据1)]
   step4: [AG mesh(数据0) ∥ AG nhr(数据1)]
```

其收益逻辑与 u1-l2 的分级通信一脉相承：mesh（框内）与 CLOS/NHR（框间）带宽不同，按 `GetParallelDataSplit` 算出的比例（`CalcParallelDataSplitRatio`，可由内置公式或 `param.opConfig.multipleDimensionSplitRatio` 指定）切数据，使两条链路近似同时干完，缩短整体墙钟时间。

与之对照的还有 **concurrent** 编排（`InsV2AllReduceConcurrentExecutor`，绑定 2 个模板，如 `AicpuAllReduceConcurMeshTwoShotNHR`：框内 two-shot mesh 与框间 NHR 并发跑）以及三级编排（`ins_v2_all_reduce_sequence_executor_aicpu_3level.cc`，Layer0/Layer1/Layer2 三级）。本模块以 parallel 为主线讲透「并行编排」这一类，其余触类旁通。

#### 4.5.2 核心流程

parallel 与 sequence 在**资源汇总**和**执行编排**两个层面都不同：

| 维度 | sequence | parallel |
|------|----------|----------|
| 步骤结构 | 4 步串行，每步 1 个模板 | 4 步，每步 2 个模板**并行** |
| 数据 | 整块依次流过 4 步 | 先按比例切成 part0（走 mesh）/part1（走 NHR）两份 |
| `slaveThreadNum` | `max(各步)`（串行可复用） | `slaveThreadNumIntra + slaveThreadNumInter + 4`（**求和**：两条腿的线程同时活） |
| `notifyNumOnMainThread` | `max(...)` | 固定 `2`（「allreduce用于两个template间同步」——主线程要在两条腿之间做同步） |
| 步间同步 | 无（同 thread 天然有序） | 每步前后 `PreSyncInterThreads` / `PostSyncInterThreads`（栅栏） |
| `GetAlgNetMeta` | `{1,1,1,1}` + SUM | `{2,2,2,2}` + **MAX** |
| 注册名示例 | `DpuAllReduceSequenceMeshNHR` | `AicpuAllReduceParallelMeshNHR` / `CcuSchedAllReduceParallelMeshNHR` |

parallel 的 `GenInsQues`（生成指令队列）主循环骨架：

```
按 GetParallelDataSplit 把本轮数据切成 part0(ratio) / part1(1-ratio)
while 还有数据未处理:
    PreSyncInterThreads()                       # 步前栅栏：两条腿对齐
    RunTemplateIntra0(part0 → mesh RS)  ∥  RunTemplateInter1(part1 → nhr RS)
    PostSyncInterThreads()                      # 步尾栅栏：回主流同步
    PreSyncInterThreads()
    RunTemplateInter0(part0 → nhr RS)  ∥  RunTemplateIntra1(part1 → mesh RS)
    PostSyncInterThreads()
    ...（step3/step4 同理，AG 阶段两条腿继续交错）...
    processedCount += part0 + part1
```

注意两条腿的数据在 step1/step2 之间**交换了链路**（mesh↔nhr）：part0 先框内归约、再框间归约；part1 反过来——保证每份数据都完整经历「框内 RS + 框间 RS + 框间 AG + 框内 AG」四级语义，只是两级之间的先后次序不同（数学上两种次序等价，都得到每卡一份归约结果）。

#### 4.5.3 源码精读

类模板声明：

> [`ins_v2_all_reduce_parallel_executor.h:30-58`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/executor/ins_v2_all_reduce_parallel_executor.h#L30-L58) —— `InsAllReduceParallelExecutor` 同样是 5 模板参数的类模板；除三大生命周期外 override 了 `CalcCostCoeff`/`GetAlgNetMeta` 与 CCU 专属的 `FastLaunch`。头文件里成对的 `RunTemplateIntra0/Inter1/Inter0/Intra1/Inter01/Intra11/Inter11`（94-125 行）就是「每步两腿」的形态学证据。

代价申报（与 sequence 的 SUM 形成鲜明对照）：

> [`ins_v2_all_reduce_parallel_executor.cc:33-74`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/executor/ins_v2_all_reduce_parallel_executor.cc#L33-L74) —— `CalcCostCoeff`：因每步是两腿并行，共申报 **8 组** A/B/C（p0~p7），每腿各自的 rankSize 与数据占比不同（如 `1.0f / 2 / rankSizeLevel0`——注意比 sequence 多除了个 2，因为数据被一分为二、且两腿各承担一半）。

> [`ins_v2_all_reduce_parallel_executor.cc:79-96`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/executor/ins_v2_all_reduce_parallel_executor.cc#L79-L96) —— `GetAlgNetMeta`：`netTypes` 有 8 个（MESH/CLOS 交替 4 组），**`intraGroupMode = CostAggMode::MAX`**、`groupSizes = {2,2,2,2}`——每组 2 个模板取 max（并行），组间求和（4 步串行）。

资源求和（对照 4.4 的取 max）：

> [`ins_v2_all_reduce_parallel_executor.cc:122-269`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/executor/ins_v2_all_reduce_parallel_executor.cc#L122-L269) —— `CalcRes`：4 个 template 各自 `CalcRes` 后，`resourceRequest.slaveThreadNum = slaveThreadNumIntra + slaveThreadNumInter + 4`（208 行）、`notifyNumOnMainThread = 2`（207 行，注释「allreduce用于两个template间同步」）——因为框内、框间两条腿的线程池必须同时存活，不能再复用。CCU 引擎下还把 4 个 template 的 `ccuKernelInfos` 打平进需求单并标记 `resGroup`（179-190 行）。

执行编排（每步两腿 + 栅栏）：

> [`ins_v2_all_reduce_parallel_executor.cc:464-543`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/executor/ins_v2_all_reduce_parallel_executor.cc#L464-L543) —— `Orchestrate`：与 sequence 相同地取回 `resCtx`、算两级 localRankSize、实例化 4 个 template（525-528 行，注意框内/框间各两个：`tempAlgIntra`/`tempAlgIntra1` 用 `infos[0]`，`tempAlgInter`/`tempAlgInter1` 用 `infos[1]`），最后进入 `GenInsQues`。

> [`ins_v2_all_reduce_parallel_executor.cc:545-562`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/executor/ins_v2_all_reduce_parallel_executor.cc#L545-L562) —— `GetParallelDataSplit`：由 `CalcParallelDataSplitRatio`（按两级 rankSize 与链路数估算）或用户 `multipleDimensionSplitRatio` 得到切分比例，产出 `splitDataSize = {ratio, 1-ratio}`。

> [`ins_v2_all_reduce_parallel_executor.cc:1065-1074`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/executor/ins_v2_all_reduce_parallel_executor.cc#L1065-L1074) —— **step1**：`PreSyncInterThreads` 栅栏后，`RunTemplateIntra0(part0)` 与 `RunTemplateInter1(part1)` 背靠背下发给两条腿的线程组，再 `PostSyncInterThreads` 回主流尾同步。step2（1084-1092 行）、step3（1108-1116 行）、step4（1126-1134 行）结构完全相同，只是两腿的链路与模板角色交错互换。

> [`ins_v2_all_reduce_parallel_executor.cc:952-1001`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/executor/ins_v2_all_reduce_parallel_executor.cc#L952-L1001) —— `GenInsQues` 开头：读切分比例、计算 scratch 内存 multiple（两种「intra0+inter1 / intra1+inter0」组合取 max，973-975 行）、再结合 UB 容量上限与 cclMem 大小算出每轮 `sliceCount`，外层 while 分轮处理。

注册宏调用（一批 parallel 算法名一次登记）：

> [`ins_v2_all_reduce_parallel_executor.cc:1362-1394`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/executor/ins_v2_all_reduce_parallel_executor.cc#L1362-L1394) —— 7 个 `REGISTER_EXECUTOR_BY_FOUR_TEMPS`：AICPU 侧 `AicpuAllReduceParallelMeshNHR`（TopoMatchMultilevel）、`InsAllReduceParallelMesh1DNHRPcie`（PcieMix）、`InsAllReduceParallelRSAGUBX`（UBX）、`InsAllReduceParallelRSAGDpu/Uboe`（Squeeze2D）；CCU 侧 `CcuSchedAllReduceParallelMeshNHR`、`CcuAllReduceParallelNHR1DMutiJetty`（mem2mem 模板）。同一个执行器类模板 × 不同拓扑匹配器/引擎模板 = 一族算法。

concurrent 编排对照（触类旁通）：

> [`ins_v2_all_reduce_concurrent_executor.cc:551-553`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/executor/ins_v2_all_reduce_concurrent_executor.cc#L551-L553) —— concurrent 只绑 **2 个模板**（如 `AicpuAllReduceConcurMeshTwoShotNHR` = `InsTempAllReduceMesh1DTwoShot` + `InsTempAllReduceNHR`）：框内直接用 two-shot AllReduce 模板、框间用 NHR，两级**并发**跑完即结束，比 parallel 的四步结构更激进。

> [`ins_v2_all_reduce_sequence_executor_aicpu.cc:772-783`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/executor/ins_v2_all_reduce_sequence_executor_aicpu.cc#L772-L783) —— `InsV2AllReduceSequenceExecutorAicpu` 是 sequence 的 AICPU 变体（如 `AicpuAllReduceSequenceMeshConcurNHR` 绑 ZAxisDetour 绕行模板），另有 3level 变体（`ins_v2_all_reduce_sequence_executor_aicpu_3level.cc`）支持 Layer2 第三级。命名规律：**`<引擎><算子><编排><框内算法><框间算法>`**，如 `CcuSched AllReduce Parallel Mesh NHR`。

#### 4.5.4 代码实践

**实践目标**：对比 `ins_v2_all_reduce_parallel_executor.cc` 与 `ins_v2_all_reduce_sequence_executor.cc`，说明 parallel 与 sequence 编排的差异——这是本讲指定实践任务的后半部分。

**操作步骤**：

1. 打开两个文件的 `CalcRes`：sequence 在 153-171 行做 max 汇总；parallel 在 192-218 行做求和汇总（`slaveThreadNumIntra + slaveThreadNumInter + 4`）。记下两处的关键行。
2. 打开两个文件的 `GetAlgNetMeta`（sequence 55-68 行 / parallel 79-96 行），对比 `groupSizes` 与 `CostAggMode`。
3. 打开两个文件的执行主循环：sequence 的 `OrchestrateLoop`（218-448 行，4 次 `KernelRun` 顺序执行、无同步调用）；parallel 的 `GenInsQues`（952-1152 行，每步 `PreSyncInterThreads` → 两次 `RunTemplate*` → `PostSyncInterThreads`）。
4. 数一数 parallel 每个 `while` 迭代里 `KernelRun`（经 `RunTemplate*` 封装）被调用几次、分几步、每步两腿分别用 Layer0 还是 Layer1 的通道（看 `PrepareResForTemplateResource` 里 `intraTempAlgRes.channels = intraLinks_`、`interTempAlgRes.channels = interLinks_`）。

**需要观察的现象**：

- sequence 的 `CalcRes` 注释写着「step1、2、3、4为串行，因此slaveThread和对应notify可以复用」→ 取 max；parallel 没有这句，改为求和并多出 `+4` 个线程（两腿各自的 main 线程）与 `notifyNumOnMainThread = 2`。
- parallel 每步有显式栅栏（`Pre/PostSyncInterThreads`），sequence 步间无同步——因为 parallel 的两腿在不同 thread 上并发，必须显式对齐。
- parallel 的两份数据（`dataOffset0/currCountPart0` 与 `dataOffset1/currCountPart1`）在 step1→step2 之间交换了链路角色。

**预期结果**：总结出对照表（见 4.5.2），并能回答：为什么 parallel 的 `CalcCostCoeff` 申报 8 组参数而 sequence 只有 4 组——因为 parallel 每步有两个并行的 template 腿，4 步 × 2 腿 = 8 组，配合 `groupSizes={2,2,2,2}` + MAX 聚合，代价模型才能正确估算 \( T = \sum_{i=1}^{4} \max(T_{mesh,i}, T_{clos,i}) \)。

> 待本地验证：本实践为源码阅读型实践；若要在运行期对比两种算法的实际耗时，需 NPU 环境下分别用 `HCCL_ALGO`（u8-l3 的新格式）指定 sequence 与 parallel 算法跑同一 AllReduce 样例并对比 profiling 数据。

#### 4.5.5 小练习与答案

**练习 1**：parallel 执行器为什么需要 `notifyNumOnMainThread = 2`，而 sequence 只需取 max？

> **参考答案**：parallel 的主线程要在每步前后与「两条腿的 main 线程」做双向同步：`PreSyncInterThreads` 需要向两个 template main 各发一个 notify（`syncNotifyOnTemplates_ = {intraNotify, interNotify}`），`PostSyncInterThreads` 又要接收两个回报（`syncNotifyOnMain_ = {0, 1}`），因此主流需要 2 个 notify。sequence 步间无显式同步，主流 notify 只取决于各 template 自身需求，取 max 即可。

**练习 2**：parallel 的 step1 与 step2 之间，两份数据为什么要交换链路（mesh↔nhr），而不是 part0 始终走 mesh？

> **参考答案**：因为 ReduceScatter 的语义要求每份数据都经历「框内归约 + 框间归约」两级。若 part0 只走 mesh、part1 只走 NHR，则 part0 永远没有被框间归约、part1 永远没有被框内归约，结果错误。交换链路后，part0 = mesh-RS 再 nhr-RS、part1 = nhr-RS 再 mesh-RS，两级归约都完成，只是次序不同——数学上等价，工程上让两条链路每一步都有活干。

**练习 3**：给定 `GetAlgNetMeta` 的输出 `{groupSizes, intraGroupMode}`，如何反推一个执行器的编排结构？

> **参考答案**：`groupSizes` 的长度 = 编排的阶段数（串行步数），每个元素 = 该阶段并行执行的 template 数；`intraGroupMode=SUM` 表示组内串行累加、`MAX` 表示组内并行取最长。例如 `{1,1,1,1}+SUM` = 4 步全串行（sequence）；`{2,2,2,2}+MAX` = 4 步、每步 2 模板并行（parallel）；concurrent 类两级并发结构则对应更短的 groupSizes。这样新选择器无需理解执行器内部，仅凭元数据即可建模任意编排的耗时。

---

## 5. 综合实践

**任务**：从「入口 → Selector → HcclExecOp → Executor → Template」走通一次完整的 AllReduce 调用链，重点对比 **sequence 与 parallel 两种编排** 在资源汇总、步间同步、代价申报三个层面的差异，并验证「选择 → 资源计算 → 执行」三阶段的分离。

**步骤**：

1. **入口与 Selector**（复习 u2-l2、u3-l2）：从 `HcclAllReduce` 入口出发，确认它最终调用 `Selector()`，后者（或新选择器 SelectorEngine）产出 `algName`（例如 `DpuAllReduceSequenceMeshNHR` 或 `AicpuAllReduceParallelMeshNHR`）。
2. **查表与候选集**：在 [`op_common.cc`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc) 的 `HcclExecOp`（627 行起）定位 `GetAlgExec`（663 行）；再对照 [`coll_alg_v2_exec_registry.h:126-135`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/executor/registry/coll_alg_v2_exec_registry.h#L126-L135)，说明这次注册同时产生了哪两笔登记。
3. **选择阶段（申报代价）**：在 [`cost_model.cc`](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/cost_model.cc) 找到 `exec->CalcCostCoeff`（187 行）与 `exec->GetAlgNetMeta`（202 行），说明 sequence（SUM，4 组）与 parallel（MAX，8 组）申报的差别如何影响代价估算。
4. **资源计算阶段**：进入 `HcclGetAlgRes`（1186 行起），画出 `CalcAlgHierarchyInfo`（1204 行）→ `CalcRes`（1208 行）→ `GetAlgResWithEngine` 的数据流；对比两个执行器 `CalcRes` 的 max vs 求和汇总。
5. **执行阶段**：回到 `HcclExecOp` 的引擎分发（712-761 行），进入两个执行器的主循环（`OrchestrateLoop` / `GenInsQues`），画出各自步骤顺序、栅栏位置与 buffer 流向。
6. **产出**：用一张双栏对照图（左 sequence / 右 parallel）标注四个关键边界——① algName 字符串契约（Selector↔Executor）；② `AlgResourceRequest`（Executor→引擎层，max vs 求和）；③ `resCtx`（引擎层→Executor）；④ `CostModelParam`/`AlgNetMeta`（Executor→新选择器）。

**自检问题**（答案见各模块小练习）：

- 资源为什么会「复用」？复用时哪两个方法会被跳过？
- parallel 的 `slaveThreadNum` 为什么求和而 sequence 取 max？
- `GetAlgNetMeta` 的 `groupSizes` 与 `CostAggMode` 如何把编排语义编码进代价模型？
- 未 override `CalcCostCoeff` 的执行器在新选择器下命运如何？

> 待本地验证：综合实践以源码阅读 + 画图为主，无需运行环境即可完成；若要结合运行期日志验证，需在 NPU 环境按 u1-l4 编译并运行 AllReduce 样例（u1-l5），开启 `HCCL_INFO` 日志后观察 `HcclGetAlgRes` / `OrchestrateLoop` / `GenInsQues` 的输出顺序与线程数差异。

---

## 6. 本讲小结

- **Executor 是「算法 × 引擎 × 拓扑」的编排器**：它接住 Selector 产出的 `algName`，负责「算资源 + 编排执行 + 申报代价」，但不自己搬数据——搬数据交给它编排的 template（u3-l5）。
- **`InsCollAlgBase` 定义五大方法**：三大生命周期 `CalcAlgHierarchyInfo`（拓扑切子组）/ `CalcRes`（算资源需求单）/ `Orchestrate`（编排执行）为纯虚；本轮新增的 `CalcCostCoeff`/`GetAlgNetMeta` 为默认空实现的代价申报钩子，未标定的执行器会被新选择器跳过。
- **注册表双写登记（本轮新增）**：每个 `REGISTER_*` 宏除了把 `(opType, algName) → 工厂` 登记进 `CollAlgExecRegistryV2`，还把算法元数据（算法名、执行器名、模板名列表）登记进 `AllAlgos`——执行器可查与算法可枚举天然同步，u8 新选择器的候选集由此免费获得。
- **`HcclExecOp` 三步走**：查表实例化（663 行）→ `HcclGetAlgRes` 算资源（683 行，未命中才跑 `CalcAlgHierarchyInfo`/`CalcRes`，资源不足则 `FallbackOp` 回退）→ 引擎分发调 `Orchestrate`（752/760 行）。
- **编排模式是执行器的核心分类维度**：sequence 四步串行（thread 取 max、无步间同步、代价 SUM）；parallel 每步两腿并行（thread 求和、步间栅栏、代价组内 MAX）；concurrent 两级并发、3level 支持第三级。编排语义被精确编码进 `groupSizes` + `CostAggMode`。
- **脆弱的字符串契约依旧**：Selector 产出的 `algName` 必须与 `REGISTER_*` 宏的第二个参数严格一致，否则查表失败——这是新增算法时最易出错的地方。

---

## 7. 下一步学习建议

- **下一讲 [u3-l5 Template](u3-l5-template.md)**：本讲反复出现的 `template->KernelRun` 与静态 `InsAlgTemplateN::CalcCostCoeff` 到底做了什么？请进入 `InsAlgTemplateBase`（`alg_v2_template_base.h`）和具体模板，看 `Describe`/`CalcRes`/`KernelRun` 如何真正下发数据搬移、`CalcCostCoeff` 如何产出 A/B/C。
- **回顾 [u3-l2 Selector](u3-l2-selector.md)**：从 Selector 端反向确认它产出的 `algName`（如 `DpuAllReduceSequenceMeshNHR`、`AicpuAllReduceParallelMeshNHR`）确实能在 executor 注册表里查到，闭合「selector↔executor」契约。
- **进入 [u8 代价模型单元](u8-l2-cost-model-and-table.md)**：本讲新增的 `CalcCostCoeff`/`GetAlgNetMeta`/`AddAlgToAllAlgos` 在 `CostModelManager`/`CostTableManager` 里如何被消费、如何参与「最小代价选算法」，是 u8 的主线。
- **深入引擎（Unit 5）**：本讲提到「AICPU_TS 走内核下发、CCU/AIV 在 host 调 `Orchestrate`」——引擎内部如何把 `Orchestrate` 的编排落到真实硬件，见 [u5-l1 AICPU 模板与 Kernel 下发](u5-l1-aicpu-template-kernel.md)。
- **动手扩展**：若想新增一种算法，最小改动是——在算子的 `selector` 目录产出新的 `algName`，并在算子的 `executor` 目录用合适的 `REGISTER_*` 宏注册一个绑好 template 的执行器（宏会自动完成 AllAlgos 双写），再为新执行器实现 `CalcCostCoeff`/`GetAlgNetMeta` 使其可被新选择器选中。

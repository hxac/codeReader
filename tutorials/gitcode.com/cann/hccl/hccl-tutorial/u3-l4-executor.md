# 算法执行器 Executor

## 1. 本讲目标

本讲是 op_common 四大组件（selector / executor / template / topo）的第三站。学完本讲，你应当能够：

- 说清 **Executor（算法执行器）** 在整条链路 `Selector → HcclExecOp → Template` 中扮演的角色：它接收 Selector 产出的 `algName`，负责「算资源 + 编排执行」。
- 掌握执行器抽象基类 `InsCollAlgBase` 的三个核心生命周期方法：`CalcAlgHierarchyInfo` / `CalcRes` / `Orchestrate`，以及它们各自在 host 侧、device 侧被调用的时机。
- 理解 `CollAlgExecRegistryV2` 注册表 + 工厂函数 + 编译期模板绑定 的设计：为什么「以 `algName` 查注册表」就能拿回一个已经绑好拓扑匹配器和算法模板的执行器。
- 看懂 `HcclExecOp` 如何把 Selector 的产物（字符串 `algName`）一步步变成一次真实的集合通信执行。
- 能读懂 `InsV2AllReduceSequenceExecutor` 这个具体执行器，理解它如何用 **四个模板顺序编排** 完成一次分级 AllReduce（节点内 ReduceScatter → 节点间 ReduceScatter → 节点间 AllGather → 节点内 AllGather）。

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

**两个关键术语补充：**

- **资源（Resource）**：执行一次算法所需的 device 侧对象，包括 thread（执行上下文/任务流）、channel（通信链路）、cclMem（跨 rank 中转缓冲）、notify（同步信号量）等。executor 在执行前必须先「算」出需要多少资源（`AlgResourceRequest`），再由引擎层真正分配出来（`AlgResourceCtxSerializable`）。
- **编排（Orchestrate）**：把多个 template 按「先谁后谁、用哪些 channel、数据从哪搬到哪」组织成一次完整通信。executor 的 `Orchestrate` 方法就是这套编排骨架的具体实现。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [`src/ops/op_common/executor/executor_v2_base.h`](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/executor/executor_v2_base.h) | 执行器抽象基类 `InsCollAlgBase`，定义三大生命周期方法与若干工具方法。 |
| [`src/ops/op_common/executor/executor_v2_base.cc`](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/executor/executor_v2_base.cc) | `InsCollAlgBase` 的默认实现（`RestoreChannelMap` / `FastLaunch` 等）。 |
| [`src/ops/op_common/executor/registry/coll_alg_v2_exec_registry.h`](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/executor/registry/coll_alg_v2_exec_registry.h) | 注册表 `CollAlgExecRegistryV2` 与一族注册宏（`REGISTER_EXEC_V2` / `REGISTER_EXECUTOR_BY_FOUR_TEMPS` 等）。 |
| [`src/ops/op_common/executor/registry/coll_alg_v2_exec_registry.cc`](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/executor/registry/coll_alg_v2_exec_registry.cc) | 注册表的 `Instance()` 单例、`Register`、`GetAlgExec` 实现。 |
| [`src/ops/op_common/op_common.cc`](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc) | 门面函数 `HcclExecOp` / `HcclGetAlgRes`，串联「查注册表 → 算资源 → 编排执行」。 |
| [`src/ops/all_reduce/executor/ins_v2_all_reduce_sequence_executor.h`](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/executor/ins_v2_all_reduce_sequence_executor.h) | 具体执行器 `InsV2AllReduceSequenceExecutor` 的类模板声明。 |
| [`src/ops/all_reduce/executor/ins_v2_all_reduce_sequence_executor.cc`](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/executor/ins_v2_all_reduce_sequence_executor.cc) | 上述执行器的实现，含四模板顺序编排与注册宏调用。 |

> 名字里的 `v2` 表示这是 op_common 的第二代（v2）执行器体系，与早期的 `executor_base.h` 共存。本讲只讲 v2。

---

## 4. 核心概念与源码讲解

### 4.1 InsCollAlgBase 抽象与执行生命周期

#### 4.1.1 概念说明

一个算法要真正跑起来，需要回答三个问题：

1. **我属于哪些子通信域？**（节点内有哪些卡和我一组、节点间又和哪些卡一组）
2. **我需要多少资源？**（几条 thread、几条 channel、多大的中转内存）
3. **数据该怎么一步步搬？**（先搬哪段、后搬哪段、用哪条 channel）

`InsCollAlgBase` 把这三个问题抽象成三个纯虚方法，分别对应三个执行阶段：

| 方法 | 阶段 | 调用时机 | 输入 | 输出 |
|------|------|---------|------|------|
| `CalcAlgHierarchyInfo` | 拓扑切分 | host 侧，资源计算阶段 | `TopoInfoWithNetLayerDetails` | `AlgHierarchyInfoForAllLevel`（逻辑子通信域） |
| `CalcRes` | 资源规划 | host 侧，资源计算阶段 | topoInfo + algHierarchyInfo | `AlgResourceRequest`（资源需求单） |
| `Orchestrate` | 数据编排执行 | 执行阶段（CCU/AIV 等引擎在 host 调用，AICPU_TS 在 device 内核内调用） | `OpParam` + `AlgResourceCtxSerializable`（已分配资源） | 下发若干 template 的 `KernelRun` |

理解这条主线后，再回看三大注册表就豁然开朗：**executor 是「算法 × 引擎 × 拓扑」的编排器，它把 template 当作可复用的搬运积木**。

#### 4.1.2 核心流程

一个 executor 实例的完整生命周期如下（伪代码）：

```
# 1. 注册阶段（程序启动时，静态初始化）
REGISTER_EXECUTOR_BY_FOUR_TEMPS(opType, "DpuAllReduceSequenceMeshNHR",
                                InsV2AllReduceSequenceExecutor, TopoMatchMultilevel,
                                Template0, Template1, Template2, Template3)
   └─ 编译期把 TopoMatch + 4 个 Template 绑进 Executor 类模板
   └─ 运行时把 ("ALLREDUCE","DpuAllReduceSequenceMeshNHR") -> creator 登记进 map

# 2. 运行期（一次算子调用）
executor = Registry.GetAlgExec(opType, algName)        # 查表，new 出实例（已绑好模板）
executor->CalcAlgHierarchyInfo(comm, topoInfo, hInfo)  # 拓扑切子通信域
executor->CalcRes(comm, param, topoInfo, hInfo, req)   # 算资源需求单
... 引擎层据 req 分配出 resCtx（thread/channel/cclMem）...
executor->Orchestrate(param, resCtx)                   # 编排：依次调 template->KernelRun
```

注意 `CalcAlgHierarchyInfo` / `CalcRes` 与 `Orchestrate` 是 **分两个阶段** 调用的：前两个在「资源计算阶段」由 `HcclGetAlgRes` 调用，`Orchestrate` 在「执行阶段」由 `HcclExecOp` 的引擎分发调用。中间隔着「资源真正分配 + 序列化」。

#### 4.1.3 源码精读

抽象基类 `InsCollAlgBase` 把三个核心方法声明为纯虚函数（`= 0`），子类必须实现：

> [`executor_v2_base.h:28-45`](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/executor/executor_v2_base.h#L28-L45) —— 定义执行器抽象。`CalcAlgHierarchyInfo`/`CalcRes`/`Orchestrate` 三个纯虚方法分别对应拓扑切分、资源规划、数据编排三个阶段。

```cpp
class InsCollAlgBase {
public:
    // 阶段一：拓扑切分，产出 AlgHierarchyInfoForAllLevel
    virtual HcclResult CalcAlgHierarchyInfo(
        HcclComm comm, TopoInfoWithNetLayerDetails* topoInfo,
        AlgHierarchyInfoForAllLevel& algHierarchyInfo) = 0;
    // 阶段二：资源规划，产出 AlgResourceRequest（资源需求单）
    virtual HcclResult CalcRes(
        HcclComm comm, const OpParam& param, const TopoInfoWithNetLayerDetails* topoInfo,
        const AlgHierarchyInfoForAllLevel& algHierarchyInfo, AlgResourceRequest& resourceRequest) = 0;
    // 阶段三：数据编排执行，消费已分配的 AlgResourceCtxSerializable
    virtual HcclResult Orchestrate(const OpParam& param, const AlgResourceCtxSerializable& resCtx) = 0;
    ...
};
```

两个核心数据结构是理解输入输出的钥匙：

> [`alg_param.h:394-402`](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/inc/alg_param.h#L394-L402) —— `AlgResourceRequest` 是 `CalcRes` 的输出，即「资源需求单」：要几个从 thread、每 thread 几个 notify、每层要哪些 channel。

> [`alg_param.h:450-452`](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/inc/alg_param.h#L450-L452) —— `AlgHierarchyInfoForAllLevel` 是三维数组 `infos[level][groupIndex] = rank列表`，描述「该算法把通信域切成了几层、每层几个子组、每个子组有哪些 rank」。

基类还提供两个共享工具方法，避免每个子类重复实现：

> [`executor_v2_base.cc:26-42`](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/executor/executor_v2_base.cc#L26-L42) —— `RestoreChannelMap` 把 `resCtx.channels`（按 level 组织的通道列表）重整成「`remoteRank -> channel 列表`」的映射，供 `Orchestrate` 阶段按远端 rank 取通道。

> [`executor_v2_base.cc:53-59`](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/executor/executor_v2_base.cc#L53-L59) —— `FastLaunch` 默认实现是「不支持」并返回错误：因为 FastLaunch 是 CCU 引擎专属的快速路径，绝大多数执行器不需要它，所以基类给了个默认拒绝的实现。

#### 4.1.4 代码实践

**实践目标**：用源码定位法，确认「三大生命周期方法」分别被谁调用、何时调用。

**操作步骤**：

1. 打开 [`src/ops/op_common/op_common.cc`](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc)。
2. 在文件内搜索 `executor->CalcAlgHierarchyInfo`、`executor->CalcRes`、`executor->Orchestrate` 三处调用点。
3. 记录每处调用所在的函数名与行号。

**预期结果**（待本地核对行号）：

- `CalcAlgHierarchyInfo` 与 `CalcRes` 出现在 `HcclGetAlgRes`（资源计算阶段）。
- `Orchestrate` 出现在 `HcclExecOp` 的引擎分发分支里（CCU、AIV 缓存、以及 `else` 默认分支）。

**观察到的现象**：你会发现前两者和后者不在同一个函数里——这正是「资源计算阶段」与「执行阶段」分离的直接证据。如果你只读了 `Orchestrate`，会疑惑资源从哪来；往回追 `HcclGetAlgRes`，就能看到它们是配对的。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `CalcRes` 的输出叫「Request（需求单）」而不是直接叫「Resource（资源）」？

> **参考答案**：因为 `CalcRes` 只 **声明** 需要「几个 thread、几条 channel、多大内存」，并不真正分配。真正分配由引擎层（`GetAlgResWithEngine` → `GetAlgResAICPU` / `GetAlgResCcu` 等）依据这份需求单完成，产出 `AlgResourceCtxSerializable`。这种「先规划、后分配」的分离，让执行器只关心算法需要什么，不必关心不同引擎的分配细节。

**练习 2**：`Orchestrate` 的签名只接收 `AlgResourceCtxSerializable`，为什么 executor 自己不去重新算 `algHierarchyInfo`？

> **参考答案**：因为 `algHierarchyInfo` 已经在「资源计算阶段」算好，并随 `resCtx` 一起被序列化保存了下来（`AlgResourceCtxSerializable::algHierarchyInfo` 字段）。执行阶段只需从 `resCtx` 里把它取回（见 4.4.3 中 `algHierarchyInfo_ = resCtx.algHierarchyInfo`），避免重复计算拓扑切分。

---

### 4.2 CollAlgExecRegistryV2 注册表与编译期模板绑定

#### 4.2.1 概念说明

回顾 u3-l1：Selector 产出的 `algName` 是个字符串。`HcclExecOp` 拿到这个字符串后，要把它「变回一个活生生的执行器对象」——这就是 `CollAlgExecRegistryV2` 注册表的职责。

它解决两个问题：

1. **查表**：给定 `(opType, algName)`，怎么拿到对应的执行器实例？
2. **绑定**：一个执行器需要绑定「拓扑匹配器」和「若干算法模板」，怎么在注册时就把它们绑死？

第二个问题是这套设计最巧妙的地方：**executor 是 C++ 类模板，它的模板参数（拓扑匹配器 + N 个 template 类型）在注册宏展开时就已经确定**。运行时查表拿到的，是一个「模板参数全部特化完毕、可以直接 new」的工厂函数。

#### 4.2.2 核心流程

注册表用经典的「**单例 + 二级 map + 工厂函数 + 静态初始化**」四件套：

```
CollAlgExecRegistryV2（单例）
  └─ execCreators_: map< opType, map< algName, Creator > >
                                              │
                                              └─ Creator = std::function<InsCollAlgBase*()>
                                                  = DefaultExecCreatorV2<已特化的Executor类型>
```

注册时机靠 **静态变量初始化**：每个 `REGISTER_*` 宏都会展开成一个全局静态变量，程序启动时它的初始化表达式执行，调用 `Register(...)` 把自己登记进 map。查找时 `GetAlgExec(opType, tag)` 调用对应 Creator，`new` 出一个实例并用 `unique_ptr` 返回。

为什么需要一族 `REGISTER_*` 宏？因为执行器要绑的 **模板个数不同**：

| 宏 | 绑定内容 | 适用场景 |
|----|---------|---------|
| `REGISTER_EXEC_V2` | 拓扑匹配器 + **1 个**模板 | 单模板编排 |
| `REGISTER_EXECUTOR_BY_TOPO` | 仅拓扑匹配器 | 不绑模板 |
| `REGISTER_EXECUTOR_BY_TWO_TEMPS` | 拓扑匹配器 + **2 个**模板 | 两级编排 |
| `REGISTER_EXECUTOR_BY_FOUR_TEMPS` | 拓扑匹配器 + **4 个**模板 | 四级编排（本讲主角） |
| `REGISTER_EXEC_V2_MULTI` | 拓扑匹配器 + **任意**模板 | 通用扩展 |

#### 4.2.3 源码精读

注册表本身是个线程安全的单例 + 二级 map：

> [`coll_alg_v2_exec_registry.h:22-41`](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/executor/registry/coll_alg_v2_exec_registry.h#L22-L41) —— 注册表定义。`CollExecCreatorV2` 是返回 `InsCollAlgBase*` 的工厂函数；`DefaultExecCreatorV2<P>()` 是默认工厂，`new P()`。二级 map 以 `(opType, tag)` 为键。

```cpp
using CollExecCreatorV2 = std::function<InsCollAlgBase*()>;
template <typename P>
static InsCollAlgBase* DefaultExecCreatorV2() {
    static_assert(std::is_base_of<InsCollAlgBase, P>::value, "...");
    return new (std::nothrow) P();   // 关键：P 是已特化好的执行器类型
}
```

四模板注册宏的展开（这是本讲最需要看懂的一段宏）：

> [`coll_alg_v2_exec_registry.h:98-115`](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/executor/registry/coll_alg_v2_exec_registry.h#L98-L115) —— `REGISTER_EXECUTOR_BY_FOUR_TEMPS` 宏。它把 5 个类型参数（执行器类 + 拓扑匹配器 + 4 个模板）全部填进 `insCollAlgBase<...>` 的模板参数表，再交给 `DefaultExecCreatorV2`，从而把绑定关系在编译期固化。

```cpp
#define REGISTER_EXECUTOR_BY_FOUR_TEMPS(                                            \
    type, name, insCollAlgBase, AlgTopoMatch, InsAlgTemplate0, InsAlgTemplate1,     \
    InsAlgTemplate2, InsAlgTemplate3)                                               \
    ... = CollAlgExecRegistryV2::Instance().Register(                               \
        type, std::string(#name),                                                   \
        DefaultExecCreatorV2<insCollAlgBase<AlgTopoMatch, InsAlgTemplate0,          \
            InsAlgTemplate1, InsAlgTemplate2, InsAlgTemplate3>>)
```

> 宏里反复出现的 `__COUNTER__`、`_HELPER_1` 层层展开，只是为了给每个注册生成一个 **唯一的静态变量名**（`g_func_##name##_##ctr`），防止多个注册互相覆盖。`#name` 则把第二个参数（如 `DpuAllReduceSequenceMeshNHR`）转成字符串作为查表键。

注册与查找的实现都很简短：

> [`coll_alg_v2_exec_registry.cc:15-31`](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/executor/registry/coll_alg_v2_exec_registry.cc#L15-L31) —— `Instance()` 返回函数内静态单例；`Register` 用 `mutex` 保护、重复注册报错。

> [`coll_alg_v2_exec_registry.cc:33-41`](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/executor/registry/coll_alg_v2_exec_registry.cc#L33-L41) —— `GetAlgExec` 查表并 `new` 出实例；若找不到返回 `nullptr`（DEBUG 日志记录未注册的 tag）。

#### 4.2.4 代码实践

**实践目标**：搞清楚「字符串 algName」是怎么变成「带好模板的 executor 对象」的。

**操作步骤**：

1. 在仓库全局搜索 `REGISTER_EXECUTOR_BY_FOUR_TEMPS`、`REGISTER_EXECUTOR_BY_TWO_TEMPS`、`REGISTER_EXEC_V2`，看看各算子分别用了哪种。
2. 选一个 `REGISTER_EXECUTOR_BY_FOUR_TEMPS(...)` 调用，把它展开成普通代码，写出 `Register(...)` 收到的实际 `type`、`name`、`Creator`。
3. 在 `HcclExecOp` 中找到对应的 `GetAlgExec` 调用，确认它传的 `(opType, algName)` 与注册时一致。

**预期结果**：你会看到 AllReduce 用的执行器注册名是 `DpuAllReduceSequenceMeshNHR`（见 4.4.3），而 `HcclExecOp` 查表用的正是 Selector 产出的同一个字符串——这就是 u3-l1 所说的「algName 脆弱字符串契约」。

#### 4.2.5 小练习与答案

**练习 1**：注册宏里的 `DefaultExecCreatorV2<insCollAlgBase<AlgTopoMatch, ...>>`，为什么要写成「嵌套的模板参数」？

> **参考答案**：`insCollAlgBase`（如 `InsV2AllReduceSequenceExecutor`）本身是一个 **类模板**，它的模板参数是「拓扑匹配器类型 + N 个 template 类型」。在宏里把这些类型实参填进去，就得到了一个 **完全特化、可实例化的具体类**，再交给 `DefaultExecCreatorV2` 包装成无参工厂。这样运行时 `GetAlgExec` 拿到的 Creator 已经是「绑好一切」的，直接 `new` 即可。

**练习 2**：如果 Selector 输出的 `algName` 在注册表里查不到，会发生什么？

> **参考答案**：`GetAlgExec` 返回 `nullptr`，`HcclExecOp` 会用 `CHK_PRT_RET(executor.get() == nullptr, ...)` 报 `Fail to find executor for algName[...]` 并返回 `HCCL_E_PARA`。这正是为什么 Selector 产出的 `algName` 必须与 executor 注册端的字符串 **严格一致**——少一个字母都会导致查表失败。

---

### 4.3 HcclExecOp：从 algName 到执行的全过程

#### 4.3.1 概念说明

`HcclExecOp` 是 op_common 对外的第二个门面函数（第一个是 `Selector`）。它接收 Selector 的产物——`algName` 字符串——把它变成一次真实的集合通信执行。可以把它的职责拆成三步：

1. **查表实例化**：用 `(opType, algName)` 从注册表拿到 executor。
2. **算资源（或复用资源）**：调用 `HcclGetAlgRes`，内部依次跑 `CalcAlgHierarchyInfo` + `CalcRes`，再由引擎层分配出 `resCtx`。若该算子的资源已缓存（同 algTag 再下发），则直接复用、跳过计算。
3. **引擎分发执行**：按 `param.engine` 进入不同分支，最终调用 `executor->Orchestrate(param, resCtx)`（CCU/AIV-cache/默认分支），或走 AICPU_TS 专属的内核下发路径。

#### 4.3.2 核心流程

```
HcclExecOp(comm, param, topoInfo, algName, resPack)
  │
  ├─ [回退缓存] 若该算法曾回退过，直接读 fallbackCtx，重设为 AICPU_TS 并递归调用 HcclExecOp
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

- **资源复用**：同一通信域反复下发同一种算法（相同 algTag）时，资源只算一次，之后直接从 engineCtx 取回反序列化。这是 HCCL 性能优化的关键之一。
- **资源不足回退**：`CalcRes` 算出的需求若引擎层无法满足（返回 `HCCL_E_UNAVAIL`），`FallbackOp` 会把执行配置改回 `AICPU_TS` 并重新 `ReSelector`，体现「AICPU 兜底」原则（u3-l2 已讲）。

#### 4.3.3 源码精读

`HcclExecOp` 函数主体：

> [`op_common.cc:617-656`](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L617-L656) —— `HcclExecOp` 入口：先查回退缓存，再把 `algName` 写入 `param`，最后 `GetAlgExec(opType, algName)` 实例化执行器；查不到则报错返回。

```cpp
std::unique_ptr<InsCollAlgBase> executor =
    CollAlgExecRegistryV2::Instance().GetAlgExec(param.opType, algName);
CHK_PRT_RET(executor.get() == nullptr,
            HCCL_ERROR("Fail to find executor for algName[%s]", algName.c_str()), HCCL_E_PARA);
```

资源计算（含未命中时调用两大生命周期方法）：

> [`op_common.cc:672-680`](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L672-L680) —— `HcclGetAlgRes` 返回 `HCCL_E_UNAVAIL` 时触发 `FallbackOp` 回退重选。

> [`op_common.cc:1192-1198`](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L1192-L1198) —— `HcclGetAlgRes` 内部依次调用 `executor->CalcAlgHierarchyInfo` 与 `executor->CalcRes`，这是「资源计算阶段」对执行器两大方法的实际驱动点。

引擎分发执行：

> [`op_common.cc:702-751`](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L702-L751) —— 按 `param.engine` 分发。CCU（`COMM_ENGINE_CCU`）与 `else` 默认分支直接调用 `executor->Orchestrate(param, *resCtxHost)`；AICPU_TS/CPU 走 `HcclAicpuKernelEntranceLaunch` 内核下发；AIV 走 `ExecuteAivCacheLogic`（其内部调 `Orchestrate`）。

> [`op_common.cc:554-570`](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L554-L570) —— `FallbackOp`：资源不足时改回 `AICPU_TS`、`ReSelector` 重选算法、再递归调 `HcclExecOp`。

> **注意区分引擎差异**：`Orchestrate` 并非在所有引擎都被 host 直接调用。AICPU_TS 引擎把资源序列化拷到 device 后，通过 `HcclLaunchAicpuKernel` 在 AICPU 上跑一个内核，执行器逻辑（含 `Orchestrate`）在 device 内核内执行；而 CCU、AIV 等引擎则在 host 侧直接调用 `executor->Orchestrate`。引擎内部细节属于 Unit 5 的主题，本讲只需记住「`Orchestrate` 是执行器对外暴露的编排入口」。

#### 4.3.4 代码实践

**实践目标**：跟踪 `HcclExecOp` 的三步骨架，验证「查表 → 算资源 → 编排」的顺序。

**操作步骤**：

1. 打开 [`op_common.cc`](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc)，定位 `HcclExecOp`（约 617 行起）。
2. 向下读，依次确认：① `GetAlgExec` 在哪一行；② `HcclGetAlgRes` 在哪一行；③ 各引擎分支里 `executor->Orchestrate(...)` 在哪几行。
3. 跳到 `HcclGetAlgRes`（约 1176 行起），确认 `CalcAlgHierarchyInfo` / `CalcRes` 的调用顺序，以及 `TryReuseResource` 命中时会提前 return（跳过这两个调用）。

**预期结果**：你会清楚地看到 `Orchestrate` 总是在 `CalcRes` 之后被调用，且二者之间夹着「引擎层资源分配」。同时 `TryReuseResource` 命中时（`isResourceReused==true`）会直接 return，省下重复算资源的开销。

#### 4.3.5 小练习与答案

**练习 1**：`HcclExecOp` 一开头为什么要先查 `fallbackCtx`？

> **参考答案**：这是回退结果的 **持久化记忆**。如果某个通信域的某个算法曾经因资源不足等原因回退到 AICPU_TS，那么后续再次下发同一算法时，直接从 fallbackCtx 读出已回退的算法名，省去「先尝试 → 再失败 → 再回退」的重复过程。体现了「同 tag 的拓扑/资源/回退结果都会被缓存」的整体设计。

**练习 2**：`GetAlgExec` 返回的 `executor` 用 `std::unique_ptr` 持有，意味着什么？

> **参考答案**：每次算子调用都会 `new` 一个全新的执行器实例，函数结束时自动析构。执行器对象本身是「一次性」的轻量编排器，它不持有 device 资源（资源挂在 `resCtx` 上，生命周期由通信域管理）。这种设计避免了执行器实例间的状态串扰。

---

### 4.4 InsV2AllReduceSequenceExecutor：四模板顺序编排

#### 4.4.1 概念说明

前三个模块讲的都是「骨架」，本模块看一个「血肉」——AllReduce 算法的一个具体执行器 `InsV2AllReduceSequenceExecutor`。它对应的 `algName` 是 `DpuAllReduceSequenceMeshNHR`，注册时绑定了 **4 个 AICPU 模板** 和多级拓扑匹配器 `TopoMatchMultilevel`。

它的核心思想直接对应 u1-l2 讲过的分级通信：一次 AllReduce 被拆成 4 个顺序步骤：

```
节点内(Layer0) ReduceScatter  →  节点间(Layer1) ReduceScatter
        →  节点间(Layer1) AllGather  →  节点内(Layer0) AllGather
```

前两步是 ReduceScatter（把每张卡的全量数据切成 N 段，归约后每卡只留一段）；后两步是 AllGather（把归约后的段重新拼回全量）。这正是 `AllReduce ≈ ReduceScatter + AllGather` 的工程实现，且每个原语都做了「节点内 / 节点间」两级拆分，把大流量放到又快又宽的低层级链路（u1-l2 的 α-β 模型）。

代码里把「节点内」叫 **框内（intra）**，对应 Layer0；把「节点间」叫 **框间（inter / dpu）**，对应 Layer1。

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

`OrchestrateLoop` 的数据流向（注意 buffer 在 ccl-in / ccl-out / user-out 之间流转）：

```
user-input ──StepOne(RS框内)──> ccl-out ──StepTwo(RS框间)──> ccl-out
                ccl-in(暂存)                  ccl-in(暂存)
        ──StepThree(AG框间)──> ccl-out ──StepFour(AG框内)──> user-output
              ccl-in(暂存)                   ccl-in(暂存)
```

当数据量超过中转内存单次容量时，外层 `for` 循环会把数据分多轮（`loopTimes`）处理，每轮完整跑一遍 4 个步骤。

#### 4.4.3 源码精读

类模板声明，明确三大方法都是 override：

> [`ins_v2_all_reduce_sequence_executor.h:28-66`](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/executor/ins_v2_all_reduce_sequence_executor.h#L28-L66) —— 类模板声明，5 个模板参数；声明 `Orchestrate` / `CalcRes` / `CalcAlgHierarchyInfo` 三个 override，以及内部的 `OrchestrateLoop` / `InitCommInfo` / `SplitData`。

**阶段一 `CalcAlgHierarchyInfo`**：构造匹配器并切拓扑。

> [`ins_v2_all_reduce_sequence_executor.cc:56-67`](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/executor/ins_v2_all_reduce_sequence_executor.cc#L56-L67) —— `AlgTopoMatch topoMatch; topoMatch.MatchTopo(comm, topoInfo, algHierarchyInfo);`。`AlgTopoMatch` 是模板参数，实例化为 `TopoMatchMultilevel`，其 `MatchTopo` 即 u3-l3 讲的多级匹配，产出两级子通信域。

**阶段二 `CalcRes`**：实例化 4 个 template，各自 `CalcRes` 后取最大值汇总。

> [`ins_v2_all_reduce_sequence_executor.cc:74-122`](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/executor/ins_v2_all_reduce_sequence_executor.cc#L74-L122) —— 4 个 template 用 `algHierarchyInfo.infos[0]`（Layer0）或 `infos[1]`（Layer1）构造，分别 `CalcRes`；由于 4 步串行，thread/notify 取各步需求的 **max** 复用，channels 只保留 Layer0、Layer1 各一份。

关键细节：`resourceRequest.slaveThreadNum = std::max(步1, 步2)`、`notifyNumPerThread` 也逐位取 max——因为 4 步是 **顺序串行** 的，同一批 thread/notify 可以在不同步骤间复用，所以取最大值即可覆盖所有步骤。

**阶段三 `Orchestrate`**：填充成员后调用 `OrchestrateLoop`。

> [`ins_v2_all_reduce_sequence_executor.cc:124-160`](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/executor/ins_v2_all_reduce_sequence_executor.cc#L124-L160) —— `Orchestrate` 从 `resCtx` 取回 `algHierarchyInfo_` / `threads_`，用 `rankIdxLevel0_ = myRank_ % size0`、`rankIdxLevel1_ = myRank_ / size0` 算出自己在两级子组中的下标，调 `RestoreChannelMap`（基类工具，见 4.1.3）重整通道，最后进入 `OrchestrateLoop`。

**四个 `KernelRun` 步骤**（这是本讲实践任务的重点）：

> [`ins_v2_all_reduce_sequence_executor.cc:257-285`](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/executor/ins_v2_all_reduce_sequence_executor.cc#L257-L285) —— **步骤一**：`algTemplateStepOne->KernelRun(...)`，`InsAlgTemplate0` = 框内（Layer0）ReduceScatter，数据从 `param.inputPtr`（user-in）搬到 `cclOutMem`，用 `remoteRankToChannelInfo_[0]`（Layer0 通道）。

> [`ins_v2_all_reduce_sequence_executor.cc:296-324`](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/executor/ins_v2_all_reduce_sequence_executor.cc#L296-L324) —— **步骤二**：`algTemplateStepTwo->KernelRun(...)`，`InsAlgTemplate1` = 框间（Layer1）ReduceScatter，数据在 `cclOutMem` 内就地归约搬运，用 `remoteRankToChannelInfo_[1]`（Layer1 通道）。

> [`ins_v2_all_reduce_sequence_executor.cc:329-358`](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/executor/ins_v2_all_reduce_sequence_executor.cc#L329-L358) —— **步骤三**：`algTemplateStepThree->KernelRun(...)`，`InsAlgTemplate2` = 框间（Layer1）AllGather，复用步骤二的切片信息（`allRankSliceSize` 等），仍用 Layer1 通道。

> [`ins_v2_all_reduce_sequence_executor.cc:363-391`](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/executor/ins_v2_all_reduce_sequence_executor.cc#L363-L391) —— **步骤四**：`algTemplateStepFour->KernelRun(...)`，`InsAlgTemplate3` = 框内（Layer0）AllGather，数据从 `cclOutMem` 搬回 `param.outputPtr`（user-out），用 Layer0 通道。

`SplitData`（数据切分）：

> [`ins_v2_all_reduce_sequence_executor.cc:412-458`](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/executor/ins_v2_all_reduce_sequence_executor.cc#L412-L458) —— `SplitData` 用 `RoundUp(count, rankSize)` 把数据均分成 `rankSize` 份（最后一份可能不满），产出每份的 `size / count / displs`，供各 template 知道「每个 rank 对应哪一段」。

**注册宏调用**：

> [`ins_v2_all_reduce_sequence_executor.cc:460-463`](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/executor/ins_v2_all_reduce_sequence_executor.cc#L460-L463) —— `REGISTER_EXECUTOR_BY_FOUR_TEMPS(HCCL_CMD_ALLREDUCE, DpuAllReduceSequenceMeshNHR, InsV2AllReduceSequenceExecutor, TopoMatchMultilevel, InsTempReduceScatterMesh1DIntra, InsTempReduceScatterMesh1dDpuInter, InsTempAllGatherNhrDpuInter, InsTempAllGatherMesh1dIntra)`。

把宏的 4 个 template 实参与 4 个步骤对齐，就是本讲的「对照表」：

| 注册宏实参 | 头文件（`template/aicpu/`） | 步骤 | 层级 | 原语 |
|-----------|---------------------------|------|------|------|
| `InsTempReduceScatterMesh1DIntra` | `ins_temp_reduce_scatter_mesh_1D_intra.h` | 步骤一 | Layer0（框内/节点内） | ReduceScatter |
| `InsTempReduceScatterMesh1dDpuInter` | `ins_temp_reduce_scatter_mesh_1D_dpu_inter.h` | 步骤二 | Layer1（框间/节点间） | ReduceScatter |
| `InsTempAllGatherNhrDpuInter` | `ins_temp_all_gather_nhr_dpu_inter.h` | 步骤三 | Layer1（框间/节点间） | AllGather |
| `InsTempAllGatherMesh1dIntra` | `ins_temp_all_gather_mesh_1D_intra.h` | 步骤四 | Layer0（框内/节点内） | AllGather |

> 这 4 个模板都位于 `src/ops/all_reduce/template/aicpu/` 下，是 AICPU 引擎的模板（template 的内部机制是 u3-l5 与 Unit 5 的主题）。

#### 4.4.4 代码实践

**实践目标**：在 `ins_v2_all_reduce_sequence_executor.cc` 中定位 `REGISTER_EXECUTOR_BY_FOUR_TEMPS` 注册，并说明 `OrchestrateLoop` 中四个 `KernelRun` 步骤分别对应 ReduceScatter/AllGather 的哪一级（节点内/节点间）。这是本讲的指定实践任务。

**操作步骤**：

1. 打开 [`ins_v2_all_reduce_sequence_executor.cc`](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/executor/ins_v2_all_reduce_sequence_executor.cc)，跳到文件末尾（约 460 行），确认注册宏把 4 个模板类型按 `0/1/2/3` 的顺序绑定。
2. 回到 `OrchestrateLoop`，依次找到 4 处 `->KernelRun(...)` 调用，记下每处用的模板变量名（`algTemplateStepOne/Two/Three/Four`）和它对应的 `InsAlgTemplateN`。
3. 对每个 template，看它构造时传的是 `algHierarchyInfo_.infos[0]`（Layer0=节点内）还是 `infos[1]`（Layer1=节点间），以及它用的是哪一层的 channel（`remoteRankToChannelInfo_[0]` 或 `[1]`）。

**需要观察的现象**：

- 步骤一（`algTemplateStepOne`）和步骤四（`algTemplateStepFour`）都用 `infos[0]` 构造、用 `remoteRankToChannelInfo_[0]`——**节点内**。
- 步骤二（`algTemplateStepTwo`）和步骤三（`algTemplateStepThree`）都用 `infos[1]` 构造、用 `remoteRankToChannelInfo_[1]`——**节点间**。
- 步骤一二是 ReduceScatter，三四是 AllGather。
- buffer 流向：步骤一读 user-in 写 ccl-out；步骤二、三在 ccl-out 内就地；步骤四读 ccl-out 写 user-out。

**预期结果**：四个步骤的「原语 × 层级」归纳为「RS-节点内 → RS-节点间 → AG-节点间 → AG-节点内」，与 u1-l2 的分级 AllReduce 模型完全吻合。若结论与之不符，请重新核对模板变量与 `infos` 下标的对应。

> 待本地验证：本实践为源码阅读型实践，结论可由静态阅读得出；若要在运行期验证，需要 NPU 环境编译并跑 AllReduce 样例，再借助 HCCL 日志（搜索 `OrchestrateLoop` 相关 `HCCL_INFO`）确认 4 个步骤的实际执行顺序。

#### 4.4.5 小练习与答案

**练习 1**：`CalcRes` 里 4 个 template 的 `slaveThreadNum` 为什么要取 `max` 而不是求和？

> **参考答案**：因为 4 个步骤是 **顺序串行** 执行的，同一时刻只有一个 template 在用 thread。所以 thread 资源可以在步骤间复用，只要取 4 步里需求最大的那个，就能覆盖所有步骤。若是 4 步 **并行**，才需要求和。

**练习 2**：`Orchestrate` 里 `rankIdxLevel0_ = myRank_ % size0`、`rankIdxLevel1_ = myRank_ / size0`，这个计算在表达什么？

> **参考答案**：它把全局 `userRank` 拆成「在 Layer0 子组内的下标」和「在 Layer1 子组内的下标」。可以理解为：把所有 rank 按 Layer0 大小「分框」，`rankIdxLevel0_` 是框内第几张卡，`rankIdxLevel1_` 是第几个框。这与 `SplitData` 的切分逻辑配合，决定本卡在 ReduceScatter/AllGather 时负责哪一段数据。

**练习 3**：如果把 `OrchestrateLoop` 里 4 个 `KernelRun` 的顺序打乱（比如先 AG 后 RS），会发生什么？

> **参考答案**：结果会错误。这 4 步是有严格数据依赖的顺序流水线：必须先把全量数据 ReduceScatter 成「每卡一段」，归约后才能 AllGather 拼回全量；而且必须先节点内、再节点间（RS 阶段），再节点间、后节点内（AG 阶段）。打乱顺序会读到尚未归约的数据，产生错误结果。这也说明 executor 的「编排」本身就是算法语义的一部分。

---

## 5. 综合实践

**任务**：从「入口 → Selector → HcclExecOp → Executor → Template」走通一次完整的 AllReduce 调用链，重点画出 executor 这一段的数据流，并验证「资源计算阶段」与「执行阶段」的分离。

**步骤**：

1. **入口与 Selector**（复习 u2-l2、u3-l2）：从 `HcclAllReduce` 入口出发，确认它最终调用 `Selector()`，后者产出 `algName`（例如 `DpuAllReduceSequenceMeshNHR`）。
2. **查表实例化**：在 [`op_common.cc`](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc) 的 `HcclExecOp` 中，定位 `CollAlgExecRegistryV2::Instance().GetAlgExec(param.opType, algName)`。说明这一步如何用字符串拿回 `InsV2AllReduceSequenceExecutor<...>` 实例。
3. **资源计算阶段**：进入 `HcclGetAlgRes`，画出 `CalcAlgHierarchyInfo`（`TopoMatchMultilevel::MatchTopo` 切两级子组）→ `CalcRes`（4 个 template 各自算资源、取 max 汇总）→ `GetAlgResWithEngine`（引擎层分配 thread/channel/cclMem）的数据流，标注输入输出类型。
4. **执行阶段**：回到 `HcclExecOp` 的引擎分发，找到 `executor->Orchestrate(param, resCtx)`，进入 `OrchestrateLoop`，画出 4 个 `KernelRun` 的顺序与 buffer 流向（user-in → ccl-out → ... → user-out）。
5. **产出**：用一张图把上面四步串起来，标注三个关键边界——① algName 字符串契约（Selector↔Executor）；② `AlgResourceRequest`（Executor→引擎层）；③ `resCtx`（引擎层→Executor）。

**自检问题**（答案见各模块小练习）：

- 资源为什么会「复用」？复用时哪两个方法会被跳过？
- 为什么 executor 的 template 参数要在注册宏里编译期绑定，而不是运行期配置？
- 4 个 `KernelRun` 步骤能否并行？为什么取 max 而非求和？

> 待本地验证：综合实践以源码阅读 + 画图为主，无需运行环境即可完成；若要结合运行期日志验证，需在 NPU 环境按 u1-l4 编译并运行 AllReduce 样例（u1-l5），开启 `HCCL_INFO` 日志后观察 `HcclExecOp` / `OrchestrateLoop` 的输出顺序。

---

## 6. 本讲小结

- **Executor 是「算法 × 引擎 × 拓扑」的编排器**：它接住 Selector 产出的 `algName`，负责「算资源 + 编排执行」，但不自己搬数据——搬数据交给它编排的 template（u3-l5）。
- **`InsCollAlgBase` 定义三大生命周期**：`CalcAlgHierarchyInfo`（拓扑切子组）、`CalcRes`（算资源需求单）、`Orchestrate`（编排执行）。前两者在 host 资源计算阶段调用，后者在执行阶段调用。
- **注册表是「单例 + 二级 map + 工厂」**：`CollAlgExecRegistryV2` 以 `(opType, algName)` 为键；注册宏在编译期把「拓扑匹配器 + N 个 template」绑进 executor 类模板，运行时查表即得「绑好一切」的实例。
- **`HcclExecOp` 三步走**：查表实例化 → `HcclGetAlgRes` 算资源（或复用缓存、或资源不足回退 AICPU_TS）→ 引擎分发调 `Orchestrate`。
- **`InsV2AllReduceSequenceExecutor` 用 4 个 AICPU 模板顺序编排**：RS-节点内 → RS-节点间 → AG-节点间 → AG-节点内，正是分级 AllReduce 的工程落地；串行执行使 thread/notify 可在步骤间复用（取 max）。
- **脆弱的字符串契约**：Selector 产出的 `algName` 必须与 `REGISTER_*` 宏的第二个参数严格一致，否则查表失败——这是新增算法时最易出错的地方。

---

## 7. 下一步学习建议

- **下一讲 [u3-l5 Template](u3-l5-template.md)**：本讲反复出现的 `template->KernelRun` 到底做了什么？请进入 `InsAlgTemplateBase`（`alg_v2_template_base.h`）和具体模板（如 `ins_temp_all_reduce_mesh_1D_one_shot`），看 `Describe` / `CalcRes` / `KernelRun` 如何真正下发数据搬移。
- **回顾 [u3-l2 Selector](u3-l2-selector.md)**：从 Selector 端反向确认它产出的 `algName`（如 `CcuMSAllReduceSoleMesh`、`DpuAllReduceSequenceMeshNHR`）确实能在 executor 注册表里查到，闭合「selector↔executor」契约。
- **深入引擎（Unit 5）**：本讲提到「AICPU_TS 走内核下发、CCU/AIV 在 host 调 `Orchestrate`」——引擎内部如何把 `Orchestrate` 的编排落到真实硬件，见 [u5-l1 AICPU 模板与 Kernel 下发](u5-l1-aicpu-template-kernel.md)。
- **动手扩展**：若想新增一种算法，最小改动是——在算子的 `selector` 目录产出新的 `algName`，并在算子的 `executor` 目录用合适的 `REGISTER_*` 宏注册一个绑好 template 的执行器。先读懂本讲的注册宏族，再动手会少走很多弯路。

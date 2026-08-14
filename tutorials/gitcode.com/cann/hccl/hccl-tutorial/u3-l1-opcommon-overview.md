# op_common 架构与三大注册表总览

## 1. 本讲目标

本讲是 Unit 3 的总览课，目标是帮你建立 **op_common 这一「算子公共骨架」的全局心智模型**。

学完后你应该能够：

1. 说清 op_common 下 **四大组件**（selector / executor / template / topo）各自的职责与协作关系。
2. 识别贯穿整个工程的 **三大注册表**：`SelectorRegistry`、`CollAlgExecRegistryV2`、`InsAlgTemplateRegistry`，以及它们各自的注册宏。
3. 在脑子里画出一条完整的端到端调度链路：

   \[
   \text{HcclAllReduce} \;\xrightarrow{\text{Selector}}\; \text{algName} \;\xrightarrow{\text{HcclExecOp}}\; \text{executor} \;\xrightarrow{\text{Orchestrate}}\; \text{template}
   \]

本讲只搭「骨架」与「数据流」，四大组件各自的内部细节（selector 的优先级遍历、topo 的分级匹配、executor 的资源计算、template 的 kernel 下发）会拆到 u3-l2 ~ u3-l5 逐个深入。读完本讲，你就拿到了后续四讲的「地图」。

## 2. 前置知识

本讲承接 **u2-l4（通信引擎选择与快速路径）**。在进入 op_common 之前，请确认你已经理解以下几点：

- **算子（op）与算法（alg）是两件事**：算子是用户调用的接口（如 `HcclAllReduce`），算法是完成这个算子的具体编排方式（如 Ring、NHR、Mesh）。
- **引擎（engine）与算法正交**：u2-l4 已经讲过，`OpParam.engine`（AICPU_TS / AIV / CCU）在进入 op_common **之前**就由 `HcclGetOpExpansionMode` 设定好了。本讲默认 `param.engine` 已就绪，只关心「算法怎么选、怎么执行」。
- **Selector 产出 algName**、**executor 编排执行**、**template 真正搬数据**、**topo 适配 rankGraph 拓扑**——这四个术语在 u1-l3 的目录结构剖析中已经引入，本讲把它们串成数据流。
- 一些 C++ 概念会在源码里反复出现，这里先点一下：
  - **单例（singleton）**：一个类全局只有一个实例，通过 `Instance()` / `Global()` 获取。三大注册表都是单例。
  - **工厂（factory）**：一个「能生产对象」的函数（通常是返回基类指针的 lambda），让注册表在不知道具体子类类型的前提下创建对象。
  - **静态初始化（static initialization）**：C++ 全局/静态变量在 `main` 之前构造。注册宏正是利用这一点，在程序启动阶段就把所有算法「登记」进注册表。

## 3. 本讲源码地图

本讲涉及的关键文件及其作用：

| 文件 | 作用 |
| --- | --- |
| [src/ops/op_common/op_common.h](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.h) | 声明 `Selector()`、`HcclExecOp()` 等 op_common 对外入口函数 |
| [src/ops/op_common/op_common.cc](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc) | 实现 `Selector()`（拓扑计算 + 算法选择）与 `HcclExecOp()`（查注册表 + 资源计算 + 编排执行） |
| [src/ops/op_common/selector/selector_registry.h](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/selector/selector_registry.h) | 第一大注册表 `SelectorRegistry` 与注册宏 `REGISTER_SELECTOR_BY_OPTYPE` |
| [src/ops/op_common/selector/execute_selector.h](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/selector/execute_selector.h) / [.cc](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/selector/execute_selector.cc) | `ExecuteSelector::Run()`：按优先级遍历 selector 直到 MATCH |
| [src/ops/op_common/selector/auto_selector_base.h](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/selector/auto_selector_base.h) / [.cc](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/selector/auto_selector_base.cc) | selector 抽象基类 `AutoSelectorBase`，按引擎分发到各 `SelectXxxAlgo` |
| [src/ops/op_common/executor/registry/coll_alg_v2_exec_registry.h](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/executor/registry/coll_alg_v2_exec_registry.h) | 第二大注册表 `CollAlgExecRegistryV2` 与注册宏 `REGISTER_EXEC_V2` 等 |
| [src/ops/op_common/executor/executor_v2_base.h](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/executor/executor_v2_base.h) | executor 抽象基类 `InsCollAlgBase`（声明 `CalcAlgHierarchyInfo` / `CalcRes` / `Orchestrate`） |
| [src/ops/op_common/template/registry/alg_v2_template_register.h](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/template/registry/alg_v2_template_register.h) | 第三大注册表 `InsAlgTemplateRegistry` 与注册宏 `REGISTER_TEMPLATE_V2` |
| [src/ops/op_common/template/alg_v2_template_base.h](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/template/alg_v2_template_base.h) | template 抽象基类 `InsAlgTemplateBase`（声明 `Describe` / `CalcRes` / `KernelRun`） |
| [src/ops/all_reduce/selector/all_reduce_auto_selector.cc](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/selector/all_reduce_auto_selector.cc) | AllReduce 的具体 selector，文件末尾注册到注册表 |
| [src/ops/all_reduce/executor/ins_v2_all_reduce_sole_executor.cc](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/executor/ins_v2_all_reduce_sole_executor.cc) | AllReduce 的具体 executor，文件末尾批量注册 algName↔executor↔template |

## 4. 核心概念与源码讲解

### 4.1 op_common 的地位与四大组件职责

#### 4.1.1 概念说明

回忆 u1-l3：`src/ops` 下「一个算子一个目录」，每个算子按 **入口 `_op` + selector + executor + template** 组织。但这四个角色里，**topo 不是某个算子私有的**——拓扑匹配是通信域控制面的基础设施，所以它被抽到 `src/ops/op_common/topo` 下，全体算子共享。

于是 op_common 之下构成了 **四大组件**：

| 组件 | 目录位置 | 私有/共享 | 核心产物 |
| --- | --- | --- | --- |
| **selector**（算法选择器） | 各算子 `selector/` 目录 | 每算子私有 | `algName`（一个字符串） |
| **executor**（算法执行器） | 各算子 `executor/` 目录 | 每算子私有 | 资源请求 + 编排执行 |
| **template**（算法模板） | 各算子 `template/{aicpu,aiv,ccu}` 目录 | 每算子私有 | 真正下发 kernel、搬数据 |
| **topo**（拓扑适配） | `op_common/topo/` | **全体共享** | `TopoInfoWithNetLayerDetails` |

一句话区分：

- **selector 决定「用哪个算法」**——输出一个 `algName` 字符串（例如 `AicpuAllReduceSoleNHR`）。
- **executor 决定「怎么编排这个算法」**——根据算法把任务拆成多级流水线（节点内 ReduceScatter → 节点间 AllReduce → 节点内 AllGather），并计算需要多少 thread/channel 资源。
- **template 决定「真正怎么搬这一级的数据」**——按「算法 × 引擎」组合，下发 AICPU Task / AIV kernel / CCU 指令序列。
- **topo 决定「rank 之间物理上怎么连」**——把 rankGraph 网络拓扑匹配成多级子通信域信息，喂给 selector 和 executor 做决策。

#### 4.1.2 核心流程

这四个组件并非平行调用，而是有明确的 **数据流方向**：

```
入口算子 (HcclAllReduce)
   │  ① 先计算/取缓存拓扑
   ▼
selector  ──产出──►  algName（字符串，如 "AicpuAllReduceSoleNHR"）
   │
   ▼  ② 用 algName 查 executor 注册表
executor  ──产出──►  AlgResourceRequest（thread/channel 数）+ 资源上下文
   │
   ▼  ③ Orchestrate 编排，逐级调用
template  ──产出──►  实际 kernel 下发（数据搬移）
```

注意三点：

1. **数据流是单向的**：selector → executor → template，每一级把上一级的产物当作输入键。
2. **topo 是横向共享输入**：既被 selector 用来判断拓扑形状（`level0Topo` 是 MESH 还是 CLOS），也被 executor 用来切分子通信域。
3. **三大注册表各管一环**：selector 注册表管「谁能选算法」、executor 注册表管「algName 对应哪个 executor」、template 注册表管「template 名对应哪个 template 工厂」。下一节展开。

#### 4.1.3 源码精读

四大组件中，**executor 和 template 的抽象基类**就在 op_common 下，是理解整条链路的「接口契约」。

executor 的抽象基类 `InsCollAlgBase` 声明了三个核心纯虚函数，正是 executor 的生命周期：

[executor_v2_base.h:35-45](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/executor/executor_v2_base.h#L35-L45) —— executor 的三个生命周期接口：`CalcAlgHierarchyInfo`（算分级子通信域信息）、`CalcRes`（算资源需求）、`Orchestrate`（编排执行）。

template 的抽象基类 `InsAlgTemplateBase`（继承自 `CommonAlgTemplateBase`）同样声明了 `Describe` / `CalcRes` / `KernelRun`：

[alg_v2_template_base.h:27-40](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/template/alg_v2_template_base.h#L27-L40) —— template 的接口：`Describe`（自述）、`CalcRes`（算自身资源）、`KernelRun`（下发 kernel 搬数据）、`GetRes`（取资源）。

记住这两个接口契约，后面看 `HcclExecOp` 的源码时就能对上号。

#### 4.1.4 代码实践

**实践目标**：用「四问法」给一个陌生算子做体检，巩固对四大组件职责的理解。

**操作步骤**：

1. 打开 [src/ops/all_reduce/](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce) 目录，找到它的 `selector/`、`executor/`、`template/` 三个子目录。
2. 对每个子目录，问自己一句：「如果删掉它，算子的哪一步会断掉？」
   - 删 selector → 不知道用哪个 algName；
   - 删 executor → 知道算法名但无法编排、无法算资源；
   - 删 template → 有编排但没人真正下发 kernel。
3. 再到 `src/ops/op_common/topo/` 确认 topo 是共享的（不在 all_reduce 目录里）。

**预期结果**：你能用一句话说清「为什么 topo 在 op_common，而 selector/executor/template 在各算子自己的目录」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 selector、executor、template 是「每算子私有」，而 topo 是「全体共享」？

> **参考答案**：selector/executor/template 的逻辑强依赖于具体算子的语义（AllReduce 和 AllGather 的算法完全不同），所以随算子私有；而 topo 描述的是「rank 之间物理怎么连」，与算子无关，属于通信域控制面的基础设施，因此抽到 op_common 下全体共享，避免每个算子重复实现拓扑匹配。

**练习 2**：executor 和 template 都有 `CalcRes`，它们算的是同一份资源吗？

> **参考答案**：不是。executor 的 `CalcRes`（[executor_v2_base.h:39](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/executor/executor_v2_base.h#L39)）算的是「整个算法这一级需要多少 thread/channel/notify」；template 的 `CalcRes`（[alg_v2_template_base.h:34](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/template/alg_v2_template_base.h#L34)）算的是「这一级里搬数据本身需要的更细粒度资源」。template 的 CalcRes 通常被 executor 在编排时聚合调用。

---

### 4.2 入口双函数：Selector 与 HcclExecOp

#### 4.2.1 概念说明

op_common 对外的「门面」是两个函数（都声明在 [op_common.h:35-37](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.h#L35-L37) 与 [op_common.h:169-170](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.h#L169-L170)）：

- `Selector(comm, param, topoInfo, algName)`：**负责「选」**——把拓扑算好，然后跑算法选择器，产出一个 `algName` 写到出参。
- `HcclExecOp(comm, param, topoInfo, algName, resPack)`：**负责「执行」**——拿 `algName` 去查 executor 注册表，算资源，再编排执行。

调用方（如 `AllReduceOutPlaceCommon`）的写法形如：

```cpp
// 示意：调用方先选后执行（伪代码，非项目原样）
CHK_RET(Selector(comm, param, topoInfo, algName));   // ① 选算法，得到 algName
CHK_RET(HcclExecOp(comm, param, topoInfo, algName)); // ② 执行算法
```

注意 `algName` 是 **`Selector` 的出参、`HcclExecOp` 的入参**——它就是两阶段之间的「接力棒」。

#### 4.2.2 核心流程

**`Selector()` 内部流程**（[op_common.cc:83-135](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L83-L135)）：

1. 检查通信域状态（是否 READY）。
2. `HcclCalcTopoInfo`：计算或取缓存的拓扑 `topoInfo`（按 `param.tag` 缓存，同一通信域多次调用共享）。
3. `ExecuteSelector::Run(param, topoInfo, algName)`：跑算法选择，产出 `algName`。
4. `SetCommEngine(param)`：根据选中的算法回填 `param.engine`（因为 selector 内部可能发生引擎回退）。
5. 引擎相关的 kernel 预加载（AICPU 走 `LoadAICPUKernel`，AIV 走 `RegisterKernel`）。
6. `SetOpParamAlgTag`：把 algName 拼成 `param.algTag`（资源缓存键）。

**`HcclExecOp()` 内部流程**（[op_common.cc:617-756](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L617-L756)）：

1. 回退探测：若该通信域该算法之前回退过（`fallbackTag` 命中缓存），直接走回退算法重新执行。
2. **查注册表**：`CollAlgExecRegistryV2::Instance().GetAlgExec(param.opType, algName)` 得到 `executor`。
3. `HcclGetAlgRes`：算资源（内部调用 `executor->CalcAlgHierarchyInfo` + `executor->CalcRes`），失败可触发 `FallbackOp` 回退重选。
4. **按引擎分支编排执行**：AICPU 走 `HcclAicpuKernelEntranceLaunch`，AIV 走 `HcclAivKernelEntranceLaunch` + `ExecuteAivCacheLogic`，CCU / 其他直接 `executor->Orchestrate(param, *resCtxHost)`。

**资源回退** 是一条副链路：当 `HcclGetAlgRes` 返回 `HCCL_E_UNAVAIL`（资源不够），`HcclExecOp` 调 `FallbackOp` → `ReSelector`（强制回退到 AICPU_TS）→ 再 `HcclExecOp`。这保证了「最坏情况下总能用 AICPU 跑通」。

#### 4.2.3 源码精读

`Selector()` 的关键骨架——拓扑计算 + 算法选择 + 引擎回填：

[op_common.cc:84-107](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L84-L107) —— `Selector` 先 `HcclCalcTopoInfo` 取拓扑，再 `collAlgSelector->Run(...)` 产出 algName；若 algName 为空直接报错；随后 `SetCommEngine(param)` 把算法对应的引擎回填进 param（注意：u2-l4 先定了引擎，但 selector 内部仍可能把引擎回退到 AICPU/AIV，这里再校正一次）。

`HcclExecOp()` 里 **查注册表拿 executor** 的那一行，是整个 op_common 最核心的一句：

[op_common.cc:653-655](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L653-L655) —— `CollAlgExecRegistryV2::Instance().GetAlgExec(param.opType, algName)` 用 `(算子类型, 算法名)` 双键查出 executor 工厂并构造实例；查不到就报 `HCCL_E_PARA`。这一句就是把 selector 的产物（algName）交给 executor 的「交接点」。

`HcclGetAlgRes` 里调用 executor 的两个生命周期函数：

[op_common.cc:1193-1198](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L1193-L1198) —— `executor->CalcAlgHierarchyInfo(comm, topoInfo, algHierarchyInfo)` 算分级子通信域信息，`executor->CalcRes(comm, param, topoInfo, algHierarchyInfo, resRequest)` 算资源请求。这两步正好对应 4.1.3 里 `InsCollAlgBase` 的两个纯虚函数。

最后按引擎把执行权交给 executor（CCU 分支为例）：

[op_common.cc:719-742](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L719-L742) —— CCU 引擎在必要时反序列化复用的资源、覆盖主流、捕获从流后，调用 `executor->Orchestrate(param, *resCtxHost)` 真正编排执行。`Orchestrate` 就是 executor 的第三个生命周期函数。

#### 4.2.4 代码实践

**实践目标**：跟踪「接力棒」algName 在两个函数间的传递路径。

**操作步骤**：

1. 在 [op_common.cc](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc) 中搜索 `algName`，确认它在 `Selector()` 的第 102 行被 `ExecuteSelector::Run` 写入，在 `HcclExecOp()` 的第 653 行被 `GetAlgExec` 读取。
2. 再看第 638 行 `sprintf_s(param.algName, ...)`——algName 还被复制进了 `param.algName` 字段（设备侧执行时用）。
3. 找到 `FallbackOp`（第 554 行起）和 `ReSelector`（第 572 行起），观察回退路径里 algName 是如何被重新选择的。

**需要观察的现象**：`algName` 是一个 `std::string`，从 `Selector` 流向 `HcclExecOp`，再流向 `param.algName`（char 数组）流向设备侧。

**预期结果**：你能指出 algName 在源码中的「写入点」「读取点」「回退重写点」三处位置。

#### 4.2.5 小练习与答案

**练习 1**：为什么把「选」和「执行」拆成 `Selector` 和 `HcclExecOp` 两个函数，而不是合并？

> **参考答案**：拆分后，`HcclExecOp` 可以在资源不足（`HCCL_E_UNAVAIL`）时通过 `FallbackOp → ReSelector → HcclExecOp` 重新选一个回退算法再执行，而不需要把回退逻辑塞进主路径。同时 `HcclExecOp` 的回退探测（命中 `fallbackTag`）也能直接跳过 `Selector` 复用历史决策。职责单一让回退与缓存成为可能。

**练习 2**：`HcclExecOp` 里 `GetAlgExec(param.opType, algName)` 用了两个键。为什么需要 `opType`？

> **参考答案**：因为不同算子可能复用相同的算法名字片段，但它们注册在各自的 opType 命名空间下。用 `(opType, algName)` 双键能避免跨算子的名字冲突，也让注册表内部可以用 `map<HcclCMDType, map<string, Creator>>` 的两级结构清晰隔离每个算子的算法集合。

---

### 4.3 三大注册表与注册宏

#### 4.3.1 概念说明

三大组件之所以能被「按名字查到」，靠的是 **三大注册表**（都是线程安全的单例）：

| 注册表 | 单例入口 | 键 | 值 | 谁来查 |
| --- | --- | --- | --- | --- |
| `SelectorRegistry` | `SelectorRegistry::Global()` | `(opType, priority)` | `AutoSelectorBase*` | `ExecuteSelector::Run` |
| `CollAlgExecRegistryV2` | `CollAlgExecRegistryV2::Instance()` | `(opType, algName)` | executor 工厂 `std::function<InsCollAlgBase*()>` | `HcclExecOp` |
| `InsAlgTemplateRegistry` | `InsAlgTemplateRegistry::Instance()` | `templateName` | template 工厂 `std::function<InsAlgTemplateBase*()>` | 按名实例化 template 的路径 |

三者的共同套路是 **「静态初始化 + 工厂」**：

- 每个 `REGISTER_*` 宏展开后是一个 **全局静态变量**，在 `main` 之前求值，副作用就是把「一个工厂」塞进注册表。
- 工厂函数 `DefaultExecCreatorV2<P>()` / `DefaultTemplateCreatorV2<P>()` 内部 `new P()`，用基类指针返回，注册表因此无需知道具体子类。
- 宏里用 `__COUNTER__`（编译器内置的递增计数器）生成全局唯一变量名，保证同一文件里多次注册不重名。

#### 4.3.2 核心流程

以 executor 注册表为例，注册与查询的流程：

```
【程序启动阶段】
  REGISTER_EXEC_V2(opType, AlgName, ExecClass, TopoMatch, TempClass)
        │ 宏展开为全局静态变量
        ▼
  CollAlgExecRegistryV2::Instance().Register(opType, "AlgName",
        DefaultExecCreatorV2<ExecClass<TopoMatch, TempClass>>())
        │
        ▼
  execCreators_[opType]["AlgName"] = 工厂   // 写入两级 map

【运行阶段】
  HcclExecOp 调 GetAlgExec(opType, algName)
        │
        ▼
  查 execCreators_[opType][algName] → 取出工厂 → 工厂() → new 出 InsCollAlgBase* 实例
```

注意 executor 注册表的一个关键设计：注册时传入的 `ExecClass<TopoMatch, TempClass>` 是一个 **已经用模板参数实例化好的具体类**。也就是说，**executor 与 template 的绑定是在注册宏里（编译期）就烘进去的**——algName 一旦确定，executor 类和它要用的 template 类就都定了。这是后面「数据流」能一气呵成的根本原因。

#### 4.3.3 源码精读

**① SelectorRegistry + REGISTER_SELECTOR_BY_OPTYPE**

[selector_registry.h:21-33](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/selector/selector_registry.h#L21-L33) —— 第一大注册表。内部用 `std::map<HcclCMDType, std::map<u32, AutoSelectorBase*>> opTypeImpls_` 双层 map，外层按算子类型、内层按优先级 `u32` 存 selector 指针；`GetSelectorsByOpType` 返回某算子的「优先级→selector」映射，供 `ExecuteSelector::Run` 按优先级遍历。

[selector_registry.h:52-53](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/selector/selector_registry.h#L52-L53) —— `REGISTER_SELECTOR_BY_OPTYPE(optype, priority, selector)` 宏：用 `__COUNTER__` 生成唯一全局变量名，构造时调用 `RegisterByOpType(optype, priority, new selector())`，把一个 selector 实例挂到 `(optype, priority)` 键上。

AllReduce 的 selector 就是在文件末尾用这个宏注册的，优先级为 18：

[all_reduce_auto_selector.cc:724](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/selector/all_reduce_auto_selector.cc#L724) —— `REGISTER_SELECTOR_BY_OPTYPE(HcclCMDType::HCCL_CMD_ALLREDUCE, 18, AllReduceAutoSelector);` 把 AllReduceAutoSelector 登记为 ALLREDUCE 算子的（优先级 18）算法选择器。

**② CollAlgExecRegistryV2 + REGISTER_EXEC_V2**

[coll_alg_v2_exec_registry.h:32-41](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/executor/registry/coll_alg_v2_exec_registry.h#L32-L41) —— 第二大注册表。`std::map<HcclCMDType, std::map<std::string, const CollExecCreatorV2>> execCreators_`，键是 `(算子类型, algName)`，值是工厂 `CollExecCreatorV2`（`std::function<InsCollAlgBase*()>`）；`GetAlgExec` 查表后调用工厂返回 `unique_ptr<InsCollAlgBase>`。

[coll_alg_v2_exec_registry.h:24-30](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/executor/registry/coll_alg_v2_exec_registry.h#L24-L30) —— `DefaultExecCreatorV2<P>()` 工厂模板：`static_assert` 校验 P 必须派生自 `InsCollAlgBase`，然后 `new (std::nothrow) P()` 返回基类指针。注册表因此只依赖基类。

[coll_alg_v2_exec_registry.h:70-71](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/executor/registry/coll_alg_v2_exec_registry.h#L70-L71) —— `REGISTER_EXEC_V2(type, name, insCollAlgBase, AlgTopoMatch, InsAlgTemplate)` 宏：把 executor 类 **连同 TopoMatch 和 Template 两个模板参数一起实例化**，再包成工厂注册到 `(type, name)`。这正是「executor↔template 编译期绑定」的落点。

该头文件还提供了一族按 template 个数递增的宏：`REGISTER_EXECUTOR_BY_TWO_TEMPS`（2 个 template）、`REGISTER_EXECUTOR_BY_FOUR_TEMPS`（4 个 template）、`REGISTER_EXEC_V2_MULTI`（可变个数），用于多级流水线编排。

**③ InsAlgTemplateRegistry + REGISTER_TEMPLATE_V2**

[alg_v2_template_register.h:32-42](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/template/registry/alg_v2_template_register.h#L32-L42) —— 第三大注册表。`std::map<std::string, InsAlgTemplateCreator> tempCreators_`，键是 template 名，值是工厂；`GetAlgTemplate(name)` 按名查并构造。

[alg_v2_template_register.h:44-48](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/template/registry/alg_v2_template_register.h#L44-L48) —— `REGISTER_TEMPLATE_V2(name, insAlgTempBase)` 宏：用 `DefaultTemplateCreatorV2` 把具体 template 类包成工厂，按名注册。

> 说明：`InsAlgTemplateRegistry` 提供了「按名实例化 template」的能力。绝大多数 algName → template 的绑定通过 executor 注册宏在编译期完成（见②）；而一些需要按名字动态实例化 template 的编排路径则会查 `InsAlgTemplateRegistry`。两者是互补关系，本讲只需记住「template 也是一个可按名查询的注册维度」即可，具体消费路径在 u3-l5 详讲。

#### 4.3.4 代码实践

**实践目标**：亲手对照「注册」与「查询」两端，确认注册表是空 table → 填表 → 查表的过程。

**操作步骤**：

1. 打开 [ins_v2_all_reduce_sole_executor.cc:271-273](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/executor/ins_v2_all_reduce_sole_executor.cc#L271-L273)，看到这一行注册：

   ```cpp
   REGISTER_EXEC_V2(
       HcclCMDType::HCCL_CMD_ALLREDUCE, AicpuAllReduceSoleMeshOneShot,
       InsV2AllReduceSoleExecutor, TopoMatch1D, InsTempAllReduceMesh1DOneShot);
   ```

   把它翻译成人话：「把 algName=`AicpuAllReduceSoleMeshOneShot` 绑到 `InsV2AllReduceSoleExecutor<TopoMatch1D, InsTempAllReduceMesh1DOneShot>` 这个已实例化的 executor 类上」。

2. 再打开 [op_common.cc:653](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L653)，看到 `GetAlgExec(HCCL_CMD_ALLREDUCE, "AicpuAllReduceSoleMeshOneShot")` 会把上面注册的工厂取出来 `new` 一个 executor。
3. 用同样的方法读第 460 行的 `REGISTER_EXECUTOR_BY_FOUR_TEMPS`（[ins_v2_all_reduce_sequence_executor.cc:460-463](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/executor/ins_v2_all_reduce_sequence_executor.cc#L460-L463)），它绑定了 **四个** template（对应 ReduceScatter/AllGather 的节点内/节点间两级），体会「template 个数 = 流水线级数」。

**预期结果**：你能口述「注册宏里写的 algName，就是运行时 selector 产出的 algName，二者必须一字不差」。

#### 4.3.5 小练习与答案

**练习 1**：`REGISTER_EXEC_V2` 和 `REGISTER_TEMPLATE_V2` 都用了 `__COUNTER__`。如果不用它、直接用固定变量名，会发生什么？

> **参考答案**：同一文件里多次注册会生成同名的全局静态变量，导致重复定义、链接报错。`__COUNTER__` 每次展开递增，保证每个注册生成唯一变量名（如 `g_func_AicpuAllReduceSoleMeshOneShot_3`），从而允许一个文件里批量注册几十个 algName 而不冲突。

**练习 2**：executor 注册表的工厂返回的是 `InsCollAlgBase*`（基类指针），而不是具体的 `InsV2AllReduceSoleExecutor*`。这样设计的好处是什么？

> **参考答案**：注册表只需依赖基类 `InsCollAlgBase` 的头文件，不必包含每个具体 executor 的头文件，降低耦合；同时 `HcclExecOp` 只通过基类的三个虚函数（`CalcAlgHierarchyInfo`/`CalcRes`/`Orchestrate`）与 executor 交互，新增算法只需新写一个子类并注册，无需改动 `HcclExecOp` 或注册表本身——这就是「对扩展开放、对修改封闭」。

---

### 4.4 端到端数据流：selector → executor → template

#### 4.4.1 概念说明

前三节分别看了组件、入口函数、注册表。本节把它们串成 **一条完整的数据流**，并落到 AllReduce 的真实代码上。

核心思路：**整条链路靠一个字符串 `algName` 串联**。

- selector 产出 `algName`；
- `algName` 是 executor 注册表的查询键；
- executor 注册表把 `algName` 映射到一个「已经绑定好 template 的 executor 实例」；
- executor 在 `Orchestrate` 里驱动它绑定的 template 跑 `KernelRun`。

换句话说，algName 是 **整条链路的「主键」**，selector 负责生成它，executor 注册表负责解释它。

#### 4.4.2 核心流程

以一次 `HcclAllReduce`（假设最终选中 `AicpuAllReduceSoleMeshOneShot`）为例的完整数据流：

```
① HcclAllReduce(...)                              [入口，u2-l2/u2-l3]
       │ FillAllReduceOpParam 装配 OpParam
       ▼
② HcclGetOpExpansionMode → param.engine=AICPU_TS  [引擎选择，u2-l4]
       ▼
③ Selector(comm, param, topoInfo, algName)        [op_common.cc:83]
       ├─ HcclCalcTopoInfo → topoInfo              (topo 组件，共享)
       ├─ ExecuteSelector::Run                     [execute_selector.cc:19]
       │     └─ AllReduceAutoSelector::SelectAicpuAlgo → algName
       │                                           = "AicpuAllReduceSoleMeshOneShot"
       │        ↑                                  (SelectorRegistry 查到它)
       └─ SetCommEngine(param)                     回填 engine
       ▼
④ HcclExecOp(comm, param, topoInfo, algName)      [op_common.cc:617]
       ├─ GetAlgExec(ALLREDUCE, "AicpuAllReduceSoleMeshOneShot")
       │     → new InsV2AllReduceSoleExecutor<TopoMatch1D, InsTempAllReduceMesh1DOneShot>
       │        ↑                                  (CollAlgExecRegistryV2 查到它)
       ├─ HcclGetAlgRes
       │     ├─ executor->CalcAlgHierarchyInfo     算分级子通信域
       │     └─ executor->CalcRes                  算 thread/channel 资源
       └─ executor->Orchestrate(param, resCtx)     [op_common.cc:742/750]
              └─ template->KernelRun(...)          下发 AICPU Task，真正搬数据
                    ↑                              (InsTempAllReduceMesh1DOneShot)
```

三个注册表各司其职，标注如下：

- **SelectorRegistry**（第 ③ 步）：被 `ExecuteSelector::Run` 查询，拿到 `AllReduceAutoSelector` 实例并调用其 `Select`，产出 algName。
- **CollAlgExecRegistryV2**（第 ④ 步上）：被 `HcclExecOp` 查询，用 `(opType, algName)` 拿到 executor 实例。
- **InsAlgTemplateRegistry**（第 ④ 步下，隐式）：template 已在 executor 注册时编译期绑定；按名动态实例化时由它查询。

#### 4.4.3 源码精读

**① selector 按优先级遍历 + 引擎分发**

[execute_selector.cc:41-51](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/selector/execute_selector.cc#L41-L51) —— `ExecuteSelector::Run` 的核心循环：`GetSelectorsByOpType(opType)` 取出该算子的「优先级→selector」map，按优先级从低到高遍历，对每个 selector 调 `Select(...)`，返回 `SelectorStatus::MATCH` 即命中并写入 `selectAlgName` 后返回；全部 NOT_MATCH 则报 `HCCL_E_NOT_SUPPORT`。（这里用 `std::map` 的默认升序，priority 数值小的先跑。）

[auto_selector_base.cc:29-63](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/selector/auto_selector_base.cc#L29-L63) —— `AutoSelectorBase::Select` 按 `opExecuteConfig`（即引擎展开模式）分发：CCU_MS → `SelectCcuMsAlgo`，CCU_SCHED → `SelectCcuScheduleAlgo`，AIV/AIV_ONLY → `SelectAivAlgo`，最后 AICPU_TS/HOSTCPU_TS/CCU_FAIL → `SelectAicpuAlgo`。这些 `SelectXxxAlgo` 是虚函数，由具体算子的 selector（如 `AllReduceAutoSelector`）重写，内部根据拓扑 + 数据量决定 algName。

**② selector 产出真实 algName**

以 AICPU 路径为例，`AllReduceAutoSelector::SelectAicpuAlgo` 在多级拓扑下会选中：

[all_reduce_auto_selector.cc:441-444](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/selector/all_reduce_auto_selector.cc#L441-L444) —— 当 `Level1Nhr` 为真时，`selectAlgName = "AicpuAllReduceSoleNHR"` 并返回 MATCH。这就是 algName 的真实来源。

**③ algName → executor → template（编译期绑定）**

注册端（写表）：

[ins_v2_all_reduce_sole_executor.cc:271-273](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/executor/ins_v2_all_reduce_sole_executor.cc#L271-L273) —— algName `AicpuAllReduceSoleMeshOneShot` 绑定到 `InsV2AllReduceSoleExecutor<TopoMatch1D, InsTempAllReduceMesh1DOneShot>`。注意 `InsTempAllReduceMesh1DOneShot` 就是 template 类——它作为 executor 的模板参数被烘了进去。

查询端（读表）：

[op_common.cc:653-655](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L653-L655) —— 运行时 `GetAlgExec` 用同名 algName 查出上面注册的 executor 实例。

执行端：

[op_common.cc:742](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L742) —— CCU 引擎分支调用 `executor->Orchestrate(param, *resCtxHost)`，executor 内部再驱动它绑定的 template 的 `KernelRun` 下发数据搬移。AICPU 引擎则走 [op_common.cc:709](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L709) 的 `HcclAicpuKernelEntranceLaunch`（Orchestrate 的逻辑被序列化进 AICPU Task 描述符下发）。

> 小结：**写表在 main 之前（静态初始化），读表在算子运行时**。两端靠 algName 这个字符串对齐，这是整条 op_common 链路最核心的设计。

#### 4.4.4 代码实践

**实践目标**：跟踪一条真实 algName 的「产消」链路，验证两端字符串一致。

**操作步骤**：

1. 在 [all_reduce_auto_selector.cc](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/selector/all_reduce_auto_selector.cc) 中搜索字符串 `"CcuMSAllReduceSoleMesh"`（注意双引号），这是 selector 在 CCU_MS 引擎、大数据量、Mesh 拓扑下会产出的 algName（见第 149 行附近）。
2. 再到 [ins_v2_all_reduce_sole_executor.cc:310-311](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/executor/ins_v2_all_reduce_sole_executor.cc#L310-L311) 找到 `REGISTER_EXEC_V2(... CcuMSAllReduceSoleMesh, InsV2AllReduceSoleExecutor, TopoMatch1D, CcuTempAllReduceMesh1D)`——注册端的 algName（无引号，由宏 `#name` 字符串化）与 selector 产出的完全一致。
3. 验证：如果有人把 selector 里的 algName 拼错一个字母，运行时 `GetAlgExec` 会查不到、返回 `nullptr`，触发 [op_common.cc:654-655](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L654-L655) 的 `HCCL_E_PARA` 报错。

**需要观察的现象**：algName 是一个「跨文件、跨编译单元」的隐式契约，没有编译期校验，全靠字符串严格匹配。这是这套注册机制最需要注意维护的地方。

**预期结果**：你能说出「新增一个算法 = 在 selector 里产出一个新 algName + 在 executor 文件里用 REGISTER 宏注册同名 algName」，两端缺一不可。

#### 4.4.5 小练习与答案

**练习 1**：假如某天有人重构，把 selector 产出的 algName 从 `AicpuAllReduceSoleNHR` 改成了 `AicpuAllReduceNHR`，但忘了改 executor 注册，会发生什么？

> **参考答案**：运行时 `HcclExecOp` 调 `GetAlgExec(ALLREDUCE, "AicpuAllReduceNHR")` 查不到（注册表里只有 `AicpuAllReduceSoleNHR`），返回 `nullptr`，[op_common.cc:654-655](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L654-L655) 报 `Fail to find executor for algName[AicpuAllReduceNHR]` 并返回 `HCCL_E_PARA`，算子下发失败。这正说明 algName 是脆弱的字符串契约，改动两端必须同步。

**练习 2**：整条链路里，拓扑信息 `topoInfo` 被哪些组件消费？

> **参考答案**：被三个组件消费——①`Selector` 阶段喂给 selector 的 `SelectXxxAlgo`，用来判断 `level0Topo`（MESH/CLOS）、`topoLevelNums`（几级组网）、数据量阈值从而选 algName；②`HcclGetAlgRes` 阶段喂给 executor 的 `CalcAlgHierarchyInfo`/`CalcRes`，用来切分子通信域、算 channel 数；③template 在 `KernelRun` 时通过资源上下文间接使用拓扑结果。topo 是贯穿全程的共享输入。

---

## 5. 综合实践

**任务**：画出本讲规格要求的「端到端调度图」，并标注三大注册表各负责哪一环。

**操作步骤**：

1. 选定一个具体场景：`HcclAllReduce`，FP32，count=1024，SUM，单机 8 卡 Mesh 拓扑，AICPU 引擎。
2. 按以下顺序在源码中定位并标注每个节点（括号内是文件:行）：
   - 入口装配：`FillAllReduceOpParam`（u2-l3，all_reduce_op.cc）。
   - 引擎选择：`HcclGetOpExpansionMode`（u2-l4，op_common.cc）。
   - 拓扑计算：`HcclCalcTopoInfo`（[op_common.cc:1097](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L1097)）。
   - 算法选择：`ExecuteSelector::Run`（[execute_selector.cc:41](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/selector/execute_selector.cc#L41)）→ 产出 algName（推测为 `AicpuAllReduceSoleMeshOneShot` 或 `AicpuAllReduceSoleMeshTwoShot`，取决于数据量）。
   - executor 查询：`GetAlgExec`（[op_common.cc:653](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L653)）。
   - 资源计算：`CalcAlgHierarchyInfo` + `CalcRes`（[op_common.cc:1193-1198](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L1193-L1198)）。
   - 编排执行：`Orchestrate`（[op_common.cc:750](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L750)）→ template `KernelRun`。
3. 在图上用三种颜色/标记标出三大注册表的「介入点」：
   - 🔵 SelectorRegistry：在 `ExecuteSelector::Run` 内查表拿 selector。
   - 🟢 CollAlgExecRegistryV2：在 `HcclExecOp` 内查表拿 executor。
   - 🟡 InsAlgTemplateRegistry：template 按名实例化时查表（多数情况已编译期绑定）。
4. 在图旁写一句「主键」注解：整条链路由 algName 字符串串联。

**预期产出**：一张包含 7 个节点、3 个注册表标注、1 条 algName 主键流的可视化调度图（手绘或工具画均可）。

**待本地验证**：若你想确认 algName 到底选了哪个，可在算子下发前打开 HCCL_INFO 日志（设置 `HCCL_OP_BASE_LOG_LEVEL=info` 或更高），日志中会打印 `[Algo][Selector] The selector[...] is matched, the selected algo type is <algName>`（见 [execute_selector.cc:46-48](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/selector/execute_selector.cc#L46-L48)），从而验证你的推断。具体日志级别开关以本地 CANN 版本为准。

## 6. 本讲小结

- op_common 由 **四大组件** 构成：selector（选算法，产出 algName）、executor（编排 + 算资源）、template（下发 kernel 搬数据）、topo（共享的拓扑适配）。
- op_common 的两个门面函数：`Selector()` 负责「选」，`HcclExecOp()` 负责「执行」，两者靠 **algName 字符串** 接力。
- 三大注册表都是「单例 + 工厂 + 静态初始化」：`SelectorRegistry`（键 opType/priority）、`CollAlgExecRegistryV2`（键 opType/algName）、`InsAlgTemplateRegistry`（键 template 名）。
- executor 与 template 的绑定在 **注册宏里编译期完成**（template 作为 executor 的模板参数），运行时靠 algName 查 executor 注册表即可拿到「已绑好 template 的 executor」。
- 整条数据流 `selector → executor → template` 由 algName 这一脆弱的字符串契约串联，新增算法必须在 selector（产出 algName）和 executor 注册宏（消费 algName）两端同步。

## 7. 下一步学习建议

本讲搭好了骨架，接下来按 op_common 的组件逐个深入（顺序建议如下）：

1. **u3-l2 算法选择器 Selector**：深入 `ExecuteSelector::Run` 的优先级遍历、`AutoSelectorBase` 的引擎分发、`AllReduceAutoSelector` 如何根据拓扑 + 数据量产出 algName。本讲的 4.4 节已经预告了它的入口。
2. **u3-l3 拓扑适配与拓扑信息 Topo**：深入 `TopoInfoWithNetLayerDetails` 的字段含义与 `topo_match_*` 家族如何把网络匹配成多级子通信域——这是 selector 和 executor 决策的物理依据。
3. **u3-l4 算法执行器 Executor**：深入 `InsCollAlgBase` 的 `CalcAlgHierarchyInfo`/`CalcRes`/`Orchestrate` 三个生命周期函数，以及 `REGISTER_EXECUTOR_BY_FOUR_TEMPS` 如何编排多级流水线。
4. **u3-l5 算法模板 Template**：深入 `InsAlgTemplateBase` 的 `Describe`/`CalcRes`/`KernelRun`，看 template 如何被 executor 组合，以及 `InsAlgTemplateRegistry` 的按名实例化路径。

阅读建议：每篇都先把本讲的「数据流总图」摆在旁边，对照确认当前组件在链路中的位置，避免「只见树木不见森林」。

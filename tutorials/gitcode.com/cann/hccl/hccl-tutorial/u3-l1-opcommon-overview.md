# op_common 架构与三大注册表总览

## 1. 本讲目标

本讲是 Unit 3 的总览课，目标是帮你建立 **op_common 这一「算子公共骨架」的全局心智模型**。

学完后你应该能够：

1. 说清 op_common 下 **四大组件**（selector / executor / template / topo）各自的职责与协作关系。
2. 识别贯穿整个工程的 **三大注册表**：`SelectorRegistry`、`CollAlgExecRegistryV2`、`InsAlgTemplateRegistry`，以及它们各自的注册宏。
3. 理解本轮演进的两个新变化：`Selector`/`ReSelector` 出现 **新旧选择器双路径分支**（`HCCL_USE_NEW_SELECTOR` 开关 + `SelectorEngine`），以及注册宏新增 **`AddAlgToAllAlgos` 静态登记**——executor 注册的同时把算法元数据自动写入「算法全集 AllAlgos」。
4. 在脑子里画出一条完整的端到端调度链路：

   \[
   \text{HcclAllReduce} \;\xrightarrow{\text{Selector}}\; \text{algName} \;\xrightarrow{\text{HcclExecOp}}\; \text{executor} \;\xrightarrow{\text{Orchestrate}}\; \text{template}
   \]

本讲只搭「骨架」与「数据流」，四大组件各自的内部细节（selector 的优先级遍历、topo 的分级匹配、executor 的资源计算、template 的 kernel 下发）会拆到 u3-l2 ~ u3-l5 逐个深入；新选择器 `SelectorEngine` 的代价模型细节属于 Unit 8。读完本讲，你就拿到了后续几讲的「地图」。

## 2. 前置知识

本讲承接 **u2-l4（通信引擎选择与快速路径）**。在进入 op_common 之前，请确认你已经理解以下几点：

- **算子（op）与算法（alg）是两件事**：算子是用户调用的接口（如 `HcclAllReduce`），算法是完成这个算子的具体编排方式（如 Ring、NHR、Mesh）。
- **引擎（engine）与算法正交**：u2-l4 已经讲过，`OpParam.engine`（AICPU_TS / AIV / CCU）在进入 op_common **之前**就由 `HcclGetOpExpansionMode` 设定好了。本讲默认 `param.engine` 已就绪，只关心「算法怎么选、怎么执行」。
- **Selector 产出 algName**、**executor 编排执行**、**template 真正搬数据**、**topo 适配 rankGraph 拓扑**——这四个术语在 u1-l3 的目录结构剖析中已经引入，本讲把它们串成数据流。
- 一些 C++ 概念会在源码里反复出现，这里先点一下：
  - **单例（singleton）**：一个类全局只有一个实例，通过 `Instance()` / `Global()` 获取。三大注册表都是单例。
  - **工厂（factory）**：一个「能生产对象」的函数（通常是返回基类指针的 lambda），让注册表在不知道具体子类类型的前提下创建对象。
  - **静态初始化（static initialization）**：C++ 全局/静态变量在 `main` 之前构造。注册宏正是利用这一点，在程序启动阶段就把所有算法「登记」进注册表。
  - **静态初始化顺序问题（static initialization order fiasco）**：不同编译单元里的全局变量，构造顺序是不确定的。如果一个全局对象在构造时依赖另一个还没构造的全局对象，就会出未定义行为。本轮新增的 `AllAlgos` 用「函数内 static 局部变量」（Meyers 单例）规避了这个问题，后面 4.3 节会看到。

## 3. 本讲源码地图

本讲涉及的关键文件及其作用：

| 文件 | 作用 |
| --- | --- |
| [src/ops/op_common/op_common.h](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.h) | 声明 `Selector()`、`HcclExecOp()` 等 op_common 对外入口函数 |
| [src/ops/op_common/op_common.cc](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc) | 实现 `Selector()`/`ReSelector()`（含新旧选择器双路径分支）与 `HcclExecOp()`（查注册表 + 资源计算 + 编排执行） |
| [src/ops/op_common/selector/selector_registry.h](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_registry.h) | 第一大注册表 `SelectorRegistry` 与注册宏 `REGISTER_SELECTOR_BY_OPTYPE` |
| [src/ops/op_common/selector/selector_engine.h](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_engine.h) / [.cc](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_engine.cc) | 本轮新增的**新选择器** `SelectorEngine`：代价模型驱动的算法选择（Unit 8 详讲，本讲只看它在双路径里的位置） |
| [src/ops/op_common/selector/cost_model.h](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/cost_model.h) | 定义「算法全集」`AllAlgos`/`AlgElement` 与登记函数 `AddAlgToAllAlgos` |
| [src/ops/op_common/selector/cost_model.cc](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/cost_model.cc) | `GetAllAlgos()`/`AddAlgToAllAlgos()` 实现，以及 `CostModelManager::InitCostModel` 如何消费 AllAlgos |
| [src/ops/op_common/selector/execute_selector.h](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/execute_selector.h) / [.cc](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/execute_selector.cc) | 旧选择器入口 `ExecuteSelector::Run()`：按优先级遍历 selector 直到 MATCH |
| [src/ops/op_common/selector/auto_selector_base.h](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/auto_selector_base.h) / [.cc](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/auto_selector_base.cc) | selector 抽象基类 `AutoSelectorBase`，按引擎分发到各 `SelectXxxAlgo` |
| [src/ops/op_common/executor/registry/coll_alg_v2_exec_registry.h](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/executor/registry/coll_alg_v2_exec_registry.h) | 第二大注册表 `CollAlgExecRegistryV2` 与注册宏 `REGISTER_EXEC_V2` 等（本轮所有注册宏都追加了 `AddAlgToAllAlgos` 登记） |
| [src/ops/op_common/executor/executor_v2_base.h](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/executor/executor_v2_base.h) | executor 抽象基类 `InsCollAlgBase`（声明 `CalcAlgHierarchyInfo` / `CalcRes` / `Orchestrate`，本轮新增 `CalcCostCoeff` / `GetAlgNetMeta`） |
| [src/ops/op_common/template/registry/alg_v2_template_register.h](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/registry/alg_v2_template_register.h) | 第三大注册表 `InsAlgTemplateRegistry` 与注册宏 `REGISTER_TEMPLATE_V2` |
| [src/ops/op_common/template/alg_v2_template_base.h](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/alg_v2_template_base.h) | template 抽象基类 `InsAlgTemplateBase`（声明 `Describe` / `CalcRes` / `KernelRun`，本轮新增静态 `CalcCostCoeff`） |
| [src/common/alg_env_config.cc](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_env_config.cc) | 环境变量解析：`HCCL_USE_NEW_SELECTOR` 的默认值与校验、`IsNewSelectorEnabled()` 实现 |
| [src/ops/all_reduce/selector/all_reduce_auto_selector.cc](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/selector/all_reduce_auto_selector.cc) | AllReduce 的具体 selector，文件末尾注册到注册表 |
| [src/ops/all_reduce/executor/ins_v2_all_reduce_sole_executor.cc](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/executor/ins_v2_all_reduce_sole_executor.cc) | AllReduce 的具体 executor，文件末尾批量注册 algName↔executor↔template |

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

需要补充的是：本轮演进在 selector 子系统里又长出了一个「平行物种」——`op_common/selector/selector_engine`。它不再是「每个算子写一个 selector 类」，而是一个**通用引擎**：拿「算法全集 + 代价模型」算出最小代价的 algName。它与旧的 per-op selector 并存，由环境变量开关分流（4.2 节详讲）。

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
3. **注册体系各管一环**：selector 注册表管「谁能选算法」、executor 注册表管「algName 对应哪个 executor」、template 注册表管「template 名对应哪个 template 工厂」；本轮新增的 `AllAlgos` 算法全集则把「全库到底有哪些算法」汇总成一张清单，供新选择器的代价模型使用。下一节展开。

#### 4.1.3 源码精读

四大组件中，**executor 和 template 的抽象基类**就在 op_common 下，是理解整条链路的「接口契约」。

executor 的抽象基类 `InsCollAlgBase` 声明了三个核心纯虚函数，正是 executor 的生命周期：

[executor_v2_base.h:52-62](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/executor/executor_v2_base.h#L52-L62) —— executor 的三个生命周期接口：`CalcAlgHierarchyInfo`（算分级子通信域信息）、`CalcRes`（算资源需求）、`Orchestrate`（编排执行）。

本轮在同一个基类上新增了两个**代价模型挂钩**（带默认空实现，子类可选重写）：

[executor_v2_base.h:37-50](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/executor/executor_v2_base.h#L37-L50) —— `CalcCostCoeff`（向代价模型申报本算法的 A/B/C 开销系数）与 `GetAlgNetMeta`（申报算法的网络元信息）。默认实现返回空，表示「未标定」——新选择器的 `CostModelManager` 会跳过未标定的算法（见 4.3 节）。

template 的抽象基类 `InsAlgTemplateBase`（继承自 `CommonAlgTemplateBase`）同样声明了 `Describe` / `CalcRes` / `KernelRun`：

[alg_v2_template_base.h:28-40](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/alg_v2_template_base.h#L28-L40) —— template 的接口：`Describe`（自述）、`CalcRes`（算自身资源）、`KernelRun`（下发 kernel 搬数据）、`GetRes`（取资源）；第 28 行新增的静态 `CalcCostCoeff` 是 template 侧的代价系数挂钩（具体模板重写它来提供 A/B/C 系数，u3-l5 详讲）。

记住这两个接口契约，后面看 `HcclExecOp` 的源码时就能对上号。

#### 4.1.4 代码实践

**实践目标**：用「四问法」给一个陌生算子做体检，巩固对四大组件职责的理解。

**操作步骤**：

1. 打开 [src/ops/all_reduce/](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce) 目录，找到它的 `selector/`、`executor/`、`template/` 三个子目录。
2. 对每个子目录，问自己一句：「如果删掉它，算子的哪一步会断掉？」
   - 删 selector → 不知道用哪个 algName；
   - 删 executor → 知道算法名但无法编排、无法算资源；
   - 删 template → 有编排但没人真正下发 kernel。
3. 再到 `src/ops/op_common/topo/` 确认 topo 是共享的（不在 all_reduce 目录里）；顺便到 `src/ops/op_common/selector/` 看一眼本轮新增的 `selector_engine.h/.cc`、`cost_model.h/.cc`、`cost_table.h/.cc`——它们也是共享的，属于「新选择器」基础设施。

**预期结果**：你能用一句话说清「为什么 topo 和 SelectorEngine 在 op_common，而 per-op 的 selector/executor/template 在各算子自己的目录」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 selector、executor、template 是「每算子私有」，而 topo 是「全体共享」？

> **参考答案**：selector/executor/template 的逻辑强依赖于具体算子的语义（AllReduce 和 AllGather 的算法完全不同），所以随算子私有；而 topo 描述的是「rank 之间物理怎么连」，与算子无关，属于通信域控制面的基础设施，因此抽到 op_common 下全体共享，避免每个算子重复实现拓扑匹配。本轮的 `SelectorEngine`/`CostModelManager` 也是同理——它们是「按代价统一选算法」的通用机制，不绑定某个算子，所以放在 op_common/selector 下共享。

**练习 2**：executor 和 template 都有 `CalcRes`，它们算的是同一份资源吗？

> **参考答案**：不是。executor 的 `CalcRes`（[executor_v2_base.h:56-59](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/executor/executor_v2_base.h#L56-L59)）算的是「整个算法这一级需要多少 thread/channel/notify」；template 的 `CalcRes`（[alg_v2_template_base.h:37-40](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/alg_v2_template_base.h#L37-L40)）算的是「这一级里搬数据本身需要的更细粒度资源」。template 的 CalcRes 通常被 executor 在编排时聚合调用。

**练习 3**：`InsCollAlgBase::CalcCostCoeff` 为什么给默认空实现，而不是像 `Orchestrate` 那样写成纯虚函数？

> **参考答案**：`Orchestrate` 是执行主链路的必经环节，每个 executor 必须实现；而 `CalcCostCoeff` 只服务于**新选择器**的代价模型，且大量老算法尚未标定系数。写成带默认空实现的虚函数，既不强迫存量 executor 改代码（默认返回空表示「未标定，跳过」），又允许新算法按需重写——这是给存量体系加可选扩展点的典型手法。

---

### 4.2 入口双函数：Selector 与 HcclExecOp（含新旧选择器双路径）

#### 4.2.1 概念说明

op_common 对外的「门面」是两个函数（都声明在 [op_common.h:35-37](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.h#L35-L37) 与 [op_common.h:169-170](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.h#L169-L170)）：

- `Selector(comm, param, topoInfo, algName)`：**负责「选」**——把拓扑算好，然后跑算法选择器，产出一个 `algName` 写到出参。
- `HcclExecOp(comm, param, topoInfo, algName, resPack)`：**负责「执行」**——拿 `algName` 去查 executor 注册表，算资源，再编排执行。

本轮最大的结构变化发生在 `Selector`（以及回退路径 `ReSelector`）内部：算法选择这一步从「单路径」变成了 **双路径分支**：

- **新路径**：环境变量 `HCCL_USE_NEW_SELECTOR=1` 且算子在白名单内 → 走 `SelectorEngine`（代价模型选择器，Unit 8 详讲）。
- **旧路径**：否则 → 走原来的 `ExecuteSelector::Run`（per-op selector 按优先级遍历，u3-l2 详讲）。

两条路径**产出物完全相同**——都是一个 algName 字符串。也就是说，双路径分支只影响「算法怎么选出来」，`Selector` 的其余步骤（引擎回填、kernel 预加载、algTag 设置）和下游 `HcclExecOp` 对两条路径无感知。这是一个非常干净的可切换设计。

调用方（如 `AllReduceOutPlaceCommon`）的写法形如：

```cpp
// 示意：调用方先选后执行（伪代码，非项目原样）
CHK_RET(Selector(comm, param, topoInfo, algName));   // ① 选算法，得到 algName
CHK_RET(HcclExecOp(comm, param, topoInfo, algName)); // ② 执行算法
```

注意 `algName` 是 **`Selector` 的出参、`HcclExecOp` 的入参**——它就是两阶段之间的「接力棒」。

#### 4.2.2 核心流程

**`Selector()` 内部流程**（[op_common.cc:85-141](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L85-L141)）：

1. 检查通信域状态（是否 READY）。
2. `HcclCalcTopoInfo`：计算或取缓存的拓扑 `topoInfo`（按 `param.tag` 缓存，同一通信域多次调用共享）。
3. **算法选择（本轮新增双路径分支）**：
   - 若 `IsNewSelectorEnabled() && SelectorEngine::IsOpSupported(param.opType)` → `SelectorEngine::Global()->Run(...)`；
   - 否则 → `ExecuteSelector::Run(...)`（旧路径）。
4. `SetCommEngine(param)`：根据选中的算法回填 `param.engine`（因为 selector 内部可能发生引擎回退）。
5. 引擎相关的 kernel 预加载（AICPU 走 `LoadAICPUKernel`，AIV 走 `RegisterKernel`）。
6. `SetOpParamAlgTag`：把 algName 拼成 `param.algTag`（资源缓存键）。

`IsOpSupported` 的白名单目前只有三个算子（AllReduce / ReduceScatter / AllGather），其他算子即使开了开关也自动走旧路径——这是新选择器「分批灰度」的策略。

**`HcclExecOp()` 内部流程**（[op_common.cc:627-765](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L627-L765)）：

1. 回退探测：若该通信域该算法之前回退过（`fallbackTag` 命中缓存），直接改走 AICPU 缓存算法重新执行。
2. **查注册表**：`CollAlgExecRegistryV2::Instance().GetAlgExec(param.opType, algName)` 得到 `executor`。
3. `HcclGetAlgRes`：算资源（内部调用 `executor->CalcAlgHierarchyInfo` + `executor->CalcRes`），失败可触发 `FallbackOp` 回退重选。
4. **按引擎分支编排执行**：AICPU 走 `HcclAicpuKernelEntranceLaunch`，AIV 走 `HcclAivKernelEntranceLaunch` + `ExecuteAivCacheLogic`，CCU / 其他直接 `executor->Orchestrate(param, *resCtxHost)`。

**资源回退** 是一条副链路：当 `HcclGetAlgRes` 返回 `HCCL_E_UNAVAIL`（资源不够），`HcclExecOp` 调 `FallbackOp` → `ReSelector`（强制把 `opExecuteConfig` 置为 AICPU_TS 后重新选算法）→ 再 `HcclExecOp`。这保证了「最坏情况下总能用 AICPU 跑通」。注意 `ReSelector` 内部同样有双路径分支——回退后是否走新选择器，仍由开关和白名单决定。

#### 4.2.3 源码精读

`Selector()` 中本轮新增的**双路径分支**——这是本讲最核心的新代码：

[op_common.cc:102-108](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L102-L108) —— 算法选择步：`IsNewSelectorEnabled() && SelectorEngine::IsOpSupported(param.opType)` 同时成立才走新选择器 `SelectorEngine::Global()->Run(...)`；否则走旧路径 `ExecuteSelector::Run(...)`。两个条件是「与」的关系：开关只管「用户想不想用」，白名单只管「这个算子能不能用」，缺一不可。

新选择器的算子白名单定义：

[selector_engine.cc:34-43](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_engine.cc#L34-L43) —— `IsOpSupported` 用静态 `std::set` 白名单声明本迭代新选择器仅支持 AllReduce / ReduceScatter / AllGather，其他算子一律回退老流程。

开关的默认值与校验在环境变量解析层：

[alg_env_config.cc:819-835](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_env_config.cc#L819-L835) —— `HCCL_USE_NEW_SELECTOR` 未设置时默认为 0（走旧选择器），取值只能是 0/1，非法值告警后按默认处理；[alg_env_config.cc:1212](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_env_config.cc#L1212) 的 `IsNewSelectorEnabled()` 就是读取这个线程局部配置的访问函数。

`ReSelector` 里的同款分支（回退路径也保持双路径一致性）：

[op_common.cc:583-593](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L583-L593) —— `ReSelector` 先把 `param.opExecuteConfig` 强制改为 `AICPU_TS`（回退语义），再走与 `Selector` 完全相同的双路径分支重新选算法。

`HcclExecOp()` 里 **查注册表拿 executor** 的那一行，是整个 op_common 最核心的一句：

[op_common.cc:663-665](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L663-L665) —— `CollAlgExecRegistryV2::Instance().GetAlgExec(param.opType, algName)` 用 `(算子类型, 算法名)` 双键查出 executor 工厂并构造实例；查不到就报 `HCCL_E_PARA`。这一句就是把 selector 的产物（algName）交给 executor 的「交接点」。注意它对 algName 来自新选择器还是旧选择器**完全无感知**——两条选择路径最终都汇合到这里。

`HcclGetAlgRes` 里调用 executor 的两个生命周期函数：

[op_common.cc:1202-1208](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L1202-L1208) —— `executor->CalcAlgHierarchyInfo(comm, topoInfo, algHierarchyInfo)` 算分级子通信域信息，`executor->CalcRes(...)` 算资源请求。这两步正好对应 4.1.3 里 `InsCollAlgBase` 的两个纯虚函数。

最后按引擎把执行权交给 executor：

[op_common.cc:719-760](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L719-L760) —— AICPU 引擎走第 719 行的 `HcclAicpuKernelEntranceLaunch`（Orchestrate 的逻辑被序列化进 AICPU Task 描述符下发），AIV 走第 726 行的 `HcclAivKernelEntranceLaunch`，CCU / HOSTCPU 分支在第 752/760 行调用 `executor->Orchestrate(param, *resCtxHost)` 真正编排执行。`Orchestrate` 就是 executor 的第三个生命周期函数。

#### 4.2.4 代码实践

**实践目标**：跟踪「接力棒」algName 在两个函数间的传递路径，并验证双路径分支的开关条件。

**操作步骤**：

1. 在 [op_common.cc](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc) 中搜索 `algName`，确认它在 `Selector()` 的第 104/107 行（两条路径）被写入，在 `HcclExecOp()` 的第 663 行被 `GetAlgExec` 读取。
2. 再看第 648 行 `sprintf_s(param.algName, ...)`——algName 还被复制进了 `param.algName` 字段（设备侧执行时用）。
3. 找到 `FallbackOp`（第 560 行起）和 `ReSelector`（第 578 行起），观察回退路径里 algName 是如何被重新选择的，并确认 `ReSelector` 第 588 行的双路径分支与 `Selector` 第 103 行完全一致。
4. 对照 [selector_engine.cc:37-41](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_engine.cc#L37-L41) 的白名单，回答：`HCCL_USE_NEW_SELECTOR=1` 时下发 `HcclBroadcast` 会走哪条路径？

**需要观察的现象**：`algName` 是一个 `std::string`，从 `Selector`（两条路径之一）流向 `HcclExecOp`，再流向 `param.algName`（char 数组）流向设备侧。

**预期结果**：你能指出 algName 的「写入点（两处）」「读取点」「回退重写点」；并能回答第 4 步——Broadcast 不在白名单，即使开关打开也走旧路径 `ExecuteSelector::Run`。

#### 4.2.5 小练习与答案

**练习 1**：为什么把「选」和「执行」拆成 `Selector` 和 `HcclExecOp` 两个函数，而不是合并？

> **参考答案**：拆分后，`HcclExecOp` 可以在资源不足（`HCCL_E_UNAVAIL`）时通过 `FallbackOp → ReSelector → HcclExecOp` 重新选一个回退算法再执行，而不需要把回退逻辑塞进主路径。同时 `HcclExecOp` 的回退探测（命中 `fallbackTag`）也能直接跳过 `Selector` 复用历史决策。职责单一让回退与缓存成为可能。

**练习 2**：新旧选择器双路径为什么不做成两个独立的入口函数（如 `SelectorNew`/`SelectorOld`），而是在 `Selector` 内部分支？

> **参考答案**：因为两条路径的**契约完全一致**（输入 param/topoInfo，输出 algName），下游（`SetCommEngine`、kernel 预加载、`HcclExecOp`）不需要任何感知。在函数内部分支可以把「用哪条路径」这一决策完全收敛在算法选择这一步，调用方代码零改动；如果拆成两个入口，所有算子入口文件都要加 if/else，开关的侵入面会扩大到全仓库。

**练习 3**：`HcclExecOp` 里 `GetAlgExec(param.opType, algName)` 用了两个键。为什么需要 `opType`？

> **参考答案**：因为不同算子可能复用相同的算法名字片段，但它们注册在各自的 opType 命名空间下。用 `(opType, algName)` 双键能避免跨算子的名字冲突，也让注册表内部可以用 `map<HcclCMDType, map<string, Creator>>` 的两级结构清晰隔离每个算子的算法集合。

---

### 4.3 三大注册表、注册宏与算法全集 AllAlgos

#### 4.3.1 概念说明

三大组件之所以能被「按名字查到」，靠的是 **三大注册表**（都是线程安全的单例）：

| 注册表 | 单例入口 | 键 | 值 | 谁来查 |
| --- | --- | --- | --- | --- |
| `SelectorRegistry` | `SelectorRegistry::Global()` | `(opType, priority)` | `AutoSelectorBase*` | 旧路径 `ExecuteSelector::Run` |
| `CollAlgExecRegistryV2` | `CollAlgExecRegistryV2::Instance()` | `(opType, algName)` | executor 工厂 `std::function<InsCollAlgBase*()>` | `HcclExecOp`（及新选择器 `CostModelManager`） |
| `InsAlgTemplateRegistry` | `InsAlgTemplateRegistry::Instance()` | `templateName` | template 工厂 `std::function<InsAlgTemplateBase*()>` | 按名实例化 template 的路径 |

三者的共同套路是 **「静态初始化 + 工厂」**：

- 每个 `REGISTER_*` 宏展开后是 **全局静态变量**，在 `main` 之前求值，副作用就是把「一个工厂」塞进注册表。
- 工厂函数 `DefaultExecCreatorV2<P>()` / `DefaultTemplateCreatorV2<P>()` 内部 `new P()`，用基类指针返回，注册表因此无需知道具体子类。
- 宏里用 `__COUNTER__`（编译器内置的递增计数器）生成全局唯一变量名，保证同一文件里多次注册不重名。

本轮演进给这个体系加了一个「副产品」：**每条 executor 注册宏在登记工厂的同时，还会调用 `AddAlgToAllAlgos` 把这个算法的元数据（algName、executor 名、template 名列表、template 个数、opType）写进一个全局的「算法全集」`AllAlgos`**。三大注册表回答的是「按 key 查 value」，而 `AllAlgos` 回答的是「**这个库里到底一共有哪些算法**」——它是新选择器代价模型遍历的输入清单。可以把它理解为本轮新增的「第四张表」：一张不是按 key 查找、而是供全量遍历的清单。

#### 4.3.2 核心流程

以 executor 注册表为例，注册与查询的流程（本轮更新后）：

```
【程序启动阶段】
  REGISTER_EXEC_V2(opType, AlgName, ExecClass, TopoMatch, TempClass)
        │ 宏展开为两个全局静态变量
        ├─► g_func_AlgName_N = CollAlgExecRegistryV2::Instance().Register(
        │       opType, "AlgName", DefaultExecCreatorV2<ExecClass<TopoMatch, TempClass>>())
        │     → execCreators_[opType]["AlgName"] = 工厂   // 写入 executor 注册表
        └─► g_alg_AlgName_N = AddAlgToAllAlgos(
                opType, "AlgName", "ExecClass",
                {"TempClass"}, 1)                          // 写入算法全集 AllAlgos

【运行阶段 · 执行链路】
  HcclExecOp 调 GetAlgExec(opType, algName)
        → 查 execCreators_[opType][algName] → 取出工厂 → 工厂() → new 出 InsCollAlgBase* 实例

【运行阶段 · 新选择器链路（HCCL_USE_NEW_SELECTOR=1 时）】
  SelectorEngine::Run → InitCostModel
        → 遍历 AllAlgos 全集 → 逐个 GetAlgExec 拿 executor → exec->CalcCostCoeff 收集 A/B/C 系数
        → 生成 CostTable → 选最小代价 algName
```

注意 executor 注册表的一个关键设计：注册时传入的 `ExecClass<TopoMatch, TempClass>` 是一个 **已经用模板参数实例化好的具体类**。也就是说，**executor 与 template 的绑定是在注册宏里（编译期）就烘进去的**——algName 一旦确定，executor 类和它要用的 template 类就都定了。这是后面「数据流」能一气呵成的根本原因。

而 `AddAlgToAllAlgos` 登记的 `g_alg_templates_*` 字符串数组（`{"InsTempAllReduceMesh1DOneShot"}` 等）只是**元数据快照**，供代价模型和三维命名映射（u8-l3 的 `AlgoNameMapper`）使用，不参与工厂构造。

还有一个值得学习的工程细节：`AllAlgos` 的存储用了「函数内 static 局部变量」（`GetAllAlgos()` 里的 `static AllAlgos globalAllAlgos`）。C++ 保证函数内 static 局部变量在**首次执行到该行时**才构造，因此无论哪个编译单元的注册宏先跑，`GetAllAlgos()` 都能安全返回已构造的对象——天然规避了跨编译单元静态初始化顺序问题。

#### 4.3.3 源码精读

**① SelectorRegistry + REGISTER_SELECTOR_BY_OPTYPE**（未变化，复习）

[selector_registry.h:21-33](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_registry.h#L21-L33) —— 第一大注册表。内部用 `std::map<HcclCMDType, std::map<u32, AutoSelectorBase*>> opTypeImpls_` 双层 map，外层按算子类型、内层按优先级 `u32` 存 selector 指针；`GetSelectorsByOpType` 返回某算子的「优先级→selector」映射，供 `ExecuteSelector::Run` 按优先级遍历。

[selector_registry.h:52-53](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_registry.h#L52-L53) —— `REGISTER_SELECTOR_BY_OPTYPE(optype, priority, selector)` 宏：用 `__COUNTER__` 生成唯一全局变量名，构造时调用 `RegisterByOpType(optype, priority, new selector())`。AllReduce 的 selector 就是这样登记的，优先级 18（[all_reduce_auto_selector.cc:724](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/selector/all_reduce_auto_selector.cc#L724)）。

**② CollAlgExecRegistryV2 + 注册宏（本轮新增 AddAlgToAllAlgos 登记）**

[coll_alg_v2_exec_registry.h:32-41](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/executor/registry/coll_alg_v2_exec_registry.h#L32-L41) —— 第二大注册表本体。`std::map<HcclCMDType, std::map<std::string, const CollExecCreatorV2>> execCreators_`，键是 `(算子类型, algName)`，值是工厂 `CollExecCreatorV2`（`std::function<InsCollAlgBase*()>`）；`GetAlgExec` 查表后调用工厂返回 `unique_ptr<InsCollAlgBase>`。注意它现在 include 了 `cost_model.h`——就是为了调用 `AddAlgToAllAlgos`。

[coll_alg_v2_exec_registry.h:24-31](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/executor/registry/coll_alg_v2_exec_registry.h#L24-L31) —— `DefaultExecCreatorV2<P>()` 工厂模板：`static_assert` 校验 P 必须派生自 `InsCollAlgBase`，然后 `new (std::nothrow) P()` 返回基类指针。注册表因此只依赖基类。

最基础的注册宏（单个 template），注意它现在生成 **两个** 静态变量：

[coll_alg_v2_exec_registry.h:84-95](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/executor/registry/coll_alg_v2_exec_registry.h#L84-L95) —— `REGISTER_EXEC_V2(type, name, insCollAlgBase, AlgTopoMatch, InsAlgTemplate)` 宏展开为：① `g_func_name_ctr` 把 `DefaultExecCreatorV2<insCollAlgBase<AlgTopoMatch, InsAlgTemplate>>` 注册进 executor 注册表（executor↔template 编译期绑定的落点）；② `g_alg_name_ctr` 调 `AddAlgToAllAlgos(type, #name, #insCollAlgBase, g_alg_templates_name_ctr, 1)` 把算法元数据登记进 AllAlgos——其中 `g_alg_templates_*` 是把模板参数字符串化得到的 `{"InsAlgTemplate"}` 数组。

四个 template 的版本同理，登记 4 个 template 名：

[coll_alg_v2_exec_registry.h:126-135](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/executor/registry/coll_alg_v2_exec_registry.h#L126-L135) —— `REGISTER_EXECUTOR_BY_FOUR_TEMPS` 先字符串化四个 template 名存进 `g_alg_templates_*` 数组，注册 executor 工厂后以 `templateNum=4` 调 `AddAlgToAllAlgos`。该头文件还有 `REGISTER_EXECUTOR_BY_TWO_TEMPS`、`REGISTER_EXECUTOR_BY_TOPO`、`REGISTER_EXEC_V2_MULTI`（可变个数，用 `ALG_GET_COUNT` 计数）等一族宏，全部遵循「注册工厂 + 登记 AllAlgos」双写模式。

**③ AllAlgos 算法全集与 AddAlgToAllAlgos（本轮新增）**

[cost_model.h:25-42](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/cost_model.h#L25-L42) —— 「算法全集」的数据结构：`AlgElement` 记录单个算法的五元组（algName、executorName、templateName 数组、templateNum、opType）；`AllAlgos` 是可扩容的动态数组；`AddAlgToAllAlgos` 是登记入口。

[cost_model.cc:20-50](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/cost_model.cc#L20-L50) —— `GetAllAlgos()` 返回函数内 static 局部单例（规避静态初始化顺序问题）；`AddAlgToAllAlgos` 按两倍扩容策略把 `AlgElement` 追加进数组。它在 `main` 之前被所有 executor 注册宏调用，跑完之后 AllAlgos 就是全库算法清单。

**谁消费 AllAlgos？** 两个消费者，都属于新选择器链路：

[cost_model.cc:155-191](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/cost_model.cc#L155-L191) —— `CostModelManager::InitCostModel` 遍历 `GetAllAlgos()` 全集：逐个算法先做拓扑过滤（`IsAlgoMatchTopo`），再 `GetAlgExec` 拿到 executor 实例并调用本轮新增的 `exec->CalcCostCoeff(...)` 收集 A/B/C 代价系数；未标定（返回空）的算法会被跳过。

[selector_engine.cc:125](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_engine.cc#L125) —— `AlgoNameMapper::Global()->Init(*GetAllAlgos())`：把全集初始化进「算法三维命名映射器」，供 HCCL_ALGO 配置与 Tuner 插件使用（u8-l3/u8-l4 详讲）。

**④ InsAlgTemplateRegistry + REGISTER_TEMPLATE_V2**（未变化，复习）

[alg_v2_template_register.h:32-42](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/registry/alg_v2_template_register.h#L32-L42) —— 第三大注册表。`std::map<std::string, InsAlgTemplateCreator> tempCreators_`，键是 template 名，值是工厂；`GetAlgTemplate(name)` 按名查并构造。

[alg_v2_template_register.h:44-48](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/registry/alg_v2_template_register.h#L44-L48) —— `REGISTER_TEMPLATE_V2(name, insAlgTempBase)` 宏：用 `DefaultTemplateCreatorV2` 把具体 template 类包成工厂，按名注册。

> 说明：`InsAlgTemplateRegistry` 提供了「按名实例化 template」的能力。绝大多数 algName → template 的绑定通过 executor 注册宏在编译期完成（见②）；而一些需要按名字动态实例化 template 的编排路径则会查 `InsAlgTemplateRegistry`。两者是互补关系，本讲只需记住「template 也是一个可按名查询的注册维度」即可，具体消费路径在 u3-l5 详讲。

#### 4.3.4 代码实践

**实践目标**：亲手对照「注册」与「查询」两端，并追踪一条 `AddAlgToAllAlgos` 登记记录的完整去向。

**操作步骤**：

1. 打开 [ins_v2_all_reduce_sole_executor.cc:298-300](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/executor/ins_v2_all_reduce_sole_executor.cc#L298-L300)，看到这一行注册：

   ```cpp
   REGISTER_EXEC_V2(
       HcclCMDType::HCCL_CMD_ALLREDUCE, AicpuAllReduceSoleMeshOneShot, InsV2AllReduceSoleExecutor, TopoMatch1D,
       InsTempAllReduceMesh1DOneShot);
   ```

   把它翻译成人话：「把 algName=`AicpuAllReduceSoleMeshOneShot` 绑到 `InsV2AllReduceSoleExecutor<TopoMatch1D, InsTempAllReduceMesh1DOneShot>` 这个已实例化的 executor 类上；**同时**把 `{algName: "AicpuAllReduceSoleMeshOneShot", executor: "InsV2AllReduceSoleExecutor", templates: ["InsTempAllReduceMesh1DOneShot"], num: 1, opType: ALLREDUCE}` 这条记录写进 AllAlgos」。

2. 再打开 [op_common.cc:663](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L663)，看到 `GetAlgExec(HCCL_CMD_ALLREDUCE, "AicpuAllReduceSoleMeshOneShot")` 会把上面注册的工厂取出来 `new` 一个 executor——这是登记的第一去向（执行链路）。
3. 第三去向：顺着 [cost_model.cc:171-191](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/cost_model.cc#L171-L191) 确认 `InitCostModel` 会遍历到这条 `AlgElement`，并调用对应 executor 的 `CalcCostCoeff` 收集代价系数——这是登记的第二去向（新选择器链路）。
4. 用同样的方法读 [ins_v2_all_reduce_sequence_executor_aicpu.cc:772-775](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/executor/ins_v2_all_reduce_sequence_executor_aicpu.cc#L772-L775) 的 `REGISTER_EXECUTOR_BY_FOUR_TEMPS`，它绑定了 **四个** template（对应 ReduceScatter/AllGather 的节点内/节点间两级），登记进 AllAlgos 的 `templateNum` 是 4——体会「template 个数 = 流水线级数」。

**预期结果**：你能口述「注册宏里写的 algName，就是运行时 selector（新旧皆然）产出的 algName，二者必须一字不差；同时这条注册还会自动变成 AllAlgos 里的一条算法元数据，无需任何手工维护」。

#### 4.3.5 小练习与答案

**练习 1**：`REGISTER_EXEC_V2` 和 `REGISTER_TEMPLATE_V2` 都用了 `__COUNTER__`。如果不用它、直接用固定变量名，会发生什么？

> **参考答案**：同一文件里多次注册会生成同名的全局静态变量，导致重复定义、链接报错。`__COUNTER__` 每次展开递增，保证每个注册生成唯一变量名（如 `g_alg_AicpuAllReduceSoleMeshOneShot_3`），从而允许一个文件里批量注册几十个 algName 而不冲突。注意本轮每个注册宏生成 **两个** 静态变量（`g_func_*` 与 `g_alg_*`），靠同一个 `ctr` 值保持后缀一致。

**练习 2**：executor 注册表的工厂返回的是 `InsCollAlgBase*`（基类指针），而不是具体的 `InsV2AllReduceSoleExecutor*`。这样设计的好处是什么？

> **参考答案**：注册表只需依赖基类 `InsCollAlgBase` 的头文件，不必包含每个具体 executor 的头文件，降低耦合；同时 `HcclExecOp` 只通过基类的虚函数与 executor 交互，新增算法只需新写一个子类并注册，无需改动 `HcclExecOp` 或注册表本身——这就是「对扩展开放、对修改封闭」。本轮的 `CalcCostCoeff` 挂钩也受益于此：`CostModelManager` 同样只通过基类指针调用它。

**练习 3**：如果没有 `AddAlgToAllAlgos` 的自动登记，新选择器的代价模型要拿到「全库算法清单」，有哪些办法？各有什么缺点？

> **参考答案**：① 手工维护一张静态清单——每新增一个算法要改两处（注册宏 + 清单），极易漏；② 遍历 `CollAlgExecRegistryV2` 内部的 map——但注册表的 value 只是「匿名工厂」，拿不到 executor/template 的名字元数据（工厂是闭包，无法反查类名），也不适合作为清单对外暴露。让注册宏在登记工厂时顺手 `AddAlgToAllAlgos`，把「新增算法」这个动作的所有副作用收敛在**一处**（注册宏），清单永远与注册表同步，这是单一事实来源（single source of truth）思想的体现。

---

### 4.4 端到端数据流：selector → executor → template（双路径版）

#### 4.4.1 概念说明

前三节分别看了组件、入口函数、注册表。本节把它们串成 **一条完整的数据流**，并落到 AllReduce 的真实代码上。

核心思路：**整条链路靠一个字符串 `algName` 串联**。

- selector（新或旧路径）产出 `algName`；
- `algName` 是 executor 注册表的查询键；
- executor 注册表把 `algName` 映射到一个「已经绑定好 template 的 executor 实例」；
- executor 在 `Orchestrate` 里驱动它绑定的 template 跑 `KernelRun`。

换句话说，algName 是 **整条链路的「主键」**，selector 负责生成它（新选择器按「最小代价」生成，旧选择器按「拓扑 + 数据量阈值 + 优先级」生成），executor 注册表负责解释它。`AllAlgos` 全集则保证新选择器在生成主键时，候选集恰好就是注册表里真实存在的那批算法。

#### 4.4.2 核心流程

以一次 `HcclAllReduce`（假设最终选中 `AicpuAllReduceSoleMeshOneShot`）为例的完整数据流：

```
① HcclAllReduce(...)                              [入口，u2-l2/u2-l3]
       │ FillAllReduceOpParam 装配 OpParam
       ▼
② HcclGetOpExpansionMode → param.engine=AICPU_TS  [引擎选择，u2-l4]
       ▼
③ Selector(comm, param, topoInfo, algName)        [op_common.cc:85]
       ├─ HcclCalcTopoInfo → topoInfo              (topo 组件，共享)
       ├─ 分支 a（新路径，HCCL_USE_NEW_SELECTOR=1 且算子在白名单）:
       │    SelectorEngine::Run                    [selector_engine.cc:181]
       │      ├─ InitCostModel（遍历 AllAlgos 全集 + CalcCostCoeff）
       │      ├─ CostTableGen → 生成代价表
       │      └─ SelectMinCost → algName（最小代价者）
       ├─ 分支 b（旧路径，默认）:
       │    ExecuteSelector::Run                   [execute_selector.cc:41]
       │      └─ AllReduceAutoSelector::SelectAicpuAlgo
       │           → algName = "AicpuAllReduceSoleMeshOneShot"
       │        ↑                                  (SelectorRegistry 查到它)
       └─ SetCommEngine(param)                     回填 engine
       ▼
④ HcclExecOp(comm, param, topoInfo, algName)      [op_common.cc:627]
       ├─ GetAlgExec(ALLREDUCE, "AicpuAllReduceSoleMeshOneShot")
       │     → new InsV2AllReduceSoleExecutor<TopoMatch1D, InsTempAllReduceMesh1DOneShot>
       │        ↑                                  (CollAlgExecRegistryV2 查到它)
       ├─ HcclGetAlgRes
       │     ├─ executor->CalcAlgHierarchyInfo     算分级子通信域
       │     └─ executor->CalcRes                  算 thread/channel 资源
       └─ executor->Orchestrate(param, resCtx)     [op_common.cc:752/760]
              └─ template->KernelRun(...)          下发 AICPU Task，真正搬数据
                    ↑                              (InsTempAllReduceMesh1DOneShot)
```

注册体系各司其职，标注如下：

- **SelectorRegistry**（第 ③ 步分支 b）：被旧路径 `ExecuteSelector::Run` 查询，拿到 `AllReduceAutoSelector` 实例并调用其 `Select`，产出 algName。
- **CollAlgExecRegistryV2**（第 ④ 步上）：被 `HcclExecOp` 查询，用 `(opType, algName)` 拿到 executor 实例；新路径的 `InitCostModel` 也会查它（拿 executor 调 `CalcCostCoeff`）。
- **InsAlgTemplateRegistry**（第 ④ 步下，隐式）：template 已在 executor 注册时编译期绑定；按名动态实例化时由它查询。
- **AllAlgos 算法全集**（第 ③ 步分支 a）：新选择器代价模型的候选清单，由注册宏经 `AddAlgToAllAlgos` 自动维护。

#### 4.4.3 源码精读

**① 旧路径：selector 按优先级遍历 + 引擎分发**

[execute_selector.cc:41-51](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/execute_selector.cc#L41-L51) —— `ExecuteSelector::Run` 的核心循环：`GetSelectorsByOpType(opType)` 取出该算子的「优先级→selector」map，按优先级从低到高遍历，对每个 selector 调 `Select(...)`，返回 `SelectorStatus::MATCH` 即命中并写入 `selectAlgName` 后返回；全部 NOT_MATCH 则报 `HCCL_E_NOT_SUPPORT`。（这里用 `std::map` 的默认升序，priority 数值小的先跑。）

[auto_selector_base.cc:29-63](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/auto_selector_base.cc#L29-L63) —— `AutoSelectorBase::Select` 按 `opExecuteConfig`（即引擎展开模式）分发：CCU_MS → `SelectCcuMsAlgo`，CCU_SCHED → `SelectCcuScheduleAlgo`，AIV/AIV_ONLY → `SelectAivAlgo`，最后 AICPU_TS/HOSTCPU_TS/CCU_FAIL → `SelectAicpuAlgo`。这些 `SelectXxxAlgo` 是虚函数，由具体算子的 selector（如 `AllReduceAutoSelector`）重写，内部根据拓扑 + 数据量决定 algName。

旧路径产出真实 algName 的一例：

[all_reduce_auto_selector.cc:441-444](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/selector/all_reduce_auto_selector.cc#L441-L444) —— 当 `Level1Nhr` 为真时，`selectAlgName = "AicpuAllReduceSoleNHR"` 并返回 MATCH。这就是 algName 在旧路径的真实来源。

**② 新路径：SelectorEngine 按代价选取**

[selector_engine.cc:181-242](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_engine.cc#L181-L242) —— `SelectorEngine::Run` 四步：step 0 初始化 Tuner 插件（每通信域一次）→ step 1 从通信域 ctx 取或初始化 CostModel（按引擎区分 tag）→ step 2 `CostTableGen` 生成代价表（若 Tuner 插件已加载，还会让插件改写 cost）→ step 3 `SelectMinCost` 选最小代价的 algName。产出物与旧路径完全同构——一个 algName 字符串（第 240-241 行日志）。

新路径还有一个与拓扑联动的细节：候选引擎优先级由 `GetEnginePriority` 决定——

[selector_engine.cc:69-75](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_engine.cc#L69-L75) —— 若 `topoInfo->hostDpuOnly` 为真（u2-l3 讲过的本轮新字段：框间仅 Host DPU 可达），候选引擎列表直接收敛为 `{HOSTCPU}`；否则按 `opExecuteConfig` 给出如 `CCU_MS → CCU_SCHED → AICPU_TS` 的回退链。而 algName 属于哪个引擎，靠前缀反查：[selector_engine.cc:45-54](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_engine.cc#L45-L54) 的 `GetEngineByAlgName` 用 `ENGINE_PREFIX_MAP` 逆序匹配 algName 前缀（长前缀优先，`CcuSched` 先于 `CcuMS`）。

**③ algName → executor → template（编译期绑定）**

注册端（写表）：

[ins_v2_all_reduce_sole_executor.cc:298-300](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/executor/ins_v2_all_reduce_sole_executor.cc#L298-L300) —— algName `AicpuAllReduceSoleMeshOneShot` 绑定到 `InsV2AllReduceSoleExecutor<TopoMatch1D, InsTempAllReduceMesh1DOneShot>`。注意 `InsTempAllReduceMesh1DOneShot` 就是 template 类——它作为 executor 的模板参数被烘了进去；同一行还把这条算法登记进 AllAlgos（4.3 节）。

查询端（读表）：

[op_common.cc:663-665](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L663-L665) —— 运行时 `GetAlgExec` 用同名 algName 查出上面注册的 executor 实例。

执行端：

[op_common.cc:752](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L752) —— CCU 等引擎分支调用 `executor->Orchestrate(param, *resCtxHost)`，executor 内部再驱动它绑定的 template 的 `KernelRun` 下发数据搬移。AICPU 引擎则走 [op_common.cc:719](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L719) 的 `HcclAicpuKernelEntranceLaunch`（Orchestrate 的逻辑被序列化进 AICPU Task 描述符下发）。

> 小结：**写表在 main 之前（静态初始化），读表在算子运行时**。两端靠 algName 这个字符串对齐；而 AllAlgos 让「新选择器的候选集」与「注册表的真实内容」由同一个注册宏维护，天然不会漂移。这是整条 op_common 链路最核心的设计。

#### 4.4.4 代码实践

**实践目标**：跟踪一条真实 algName 的「产消」链路，验证两端字符串一致。

**操作步骤**：

1. 在 [all_reduce_auto_selector.cc](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/selector/all_reduce_auto_selector.cc) 中搜索字符串 `"CcuMSAllReduceSoleMesh"`（注意双引号），这是旧路径 selector 在 CCU_MS 引擎、大数据量、Mesh 拓扑下会产出的 algName。
2. 再到 [ins_v2_all_reduce_sole_executor.cc:336-338](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/executor/ins_v2_all_reduce_sole_executor.cc#L336-L338) 找到 `REGISTER_EXEC_V2(... CcuMSAllReduceSoleMesh, InsV2AllReduceSoleExecutor, TopoMatch1D, CcuTempAllReduceMesh1D)`——注册端的 algName（无引号，由宏 `#name` 字符串化）与 selector 产出的完全一致。
3. 换到新路径视角：在 [selector_engine.cc:45-54](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_engine.cc#L45-L54) 确认 `GetEngineByAlgName("CcuMSAllReduceSoleMesh")` 会命中前缀 `CcuMS`（在 `ENGINE_PREFIX_MAP` 中定义于 alg_param.h，见 u2-l3）——同一个 algName 字符串契约在新路径被复用为「引擎归属判据」。
4. 验证：如果有人把 selector 里的 algName 拼错一个字母，运行时 `GetAlgExec` 会查不到、返回 `nullptr`，触发 [op_common.cc:664-665](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L664-L665) 的 `HCCL_E_PARA` 报错；而新路径下拼错的算法根本不会出现在 AllAlgos 里，代价模型直接「看不到」它。

**需要观察的现象**：algName 是一个「跨文件、跨编译单元」的隐式契约，没有编译期校验，全靠字符串严格匹配。这是这套注册机制最需要注意维护的地方。

**预期结果**：你能说出「新增一个算法 = 在 selector 里产出一个新 algName + 在 executor 文件里用 REGISTER 宏注册同名 algName」两端缺一不可；且 REGISTER 宏会自动把新算法放进 AllAlgos，新选择器无需任何额外登记。

#### 4.4.5 小练习与答案

**练习 1**：假如某天有人重构，把 selector 产出的 algName 从 `AicpuAllReduceSoleNHR` 改成了 `AicpuAllReduceNHR`，但忘了改 executor 注册，会发生什么？

> **参考答案**：运行时 `HcclExecOp` 调 `GetAlgExec(ALLREDUCE, "AicpuAllReduceNHR")` 查不到（注册表里只有 `AicpuAllReduceSoleNHR`），返回 `nullptr`，[op_common.cc:664-665](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L664-L665) 报 `Fail to find executor for algName[AicpuAllReduceNHR]` 并返回 `HCCL_E_PARA`，算子下发失败。新路径下则更隐蔽：该算法不在 AllAlgos 里，代价模型直接不把它列为候选，算子不会失败但永远不会选到它。这正说明 algName 是脆弱的字符串契约，改动两端必须同步。

**练习 2**：整条链路里，拓扑信息 `topoInfo` 被哪些组件消费？

> **参考答案**：被多个组件消费——①`Selector` 阶段喂给新旧两条选择路径：旧路径的 `SelectXxxAlgo` 用它判断 `level0Topo`（MESH/CLOS）、`topoLevelNums`（几级组网）、数据量阈值；新路径的 `SelectorEngine` 用它算候选引擎（`hostDpuOnly` → 只剩 HOSTCPU）并初始化 CostModel；②`HcclGetAlgRes` 阶段喂给 executor 的 `CalcAlgHierarchyInfo`/`CalcRes`，用来切分子通信域、算 channel 数；③template 在 `KernelRun` 时通过资源上下文间接使用拓扑结果。topo 是贯穿全程的共享输入。

**练习 3**：新旧两条选择路径会不会选出「注册表里不存在的算法」？

> **参考答案**：理论上都可能（都是字符串拼接），但机制不同地兜底：旧路径选出不存在的 algName 会在 `GetAlgExec` 处报 `HCCL_E_PARA` 当场失败；新路径的候选集直接来自 `AllAlgos`（由注册宏自动维护），天然是注册表全集的子集，且 `InitCostModel` 还会对每条候选再 `GetAlgExec` 验证一次 executor 存在（[cost_model.cc:180-185](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/cost_model.cc#L180-L185)），查不到就跳过。这是新选择器相对旧路径的一个健壮性收益。

---

## 5. 综合实践

**任务**：画出本讲规格要求的「端到端调度图」（双路径版），标注三大注册表 + AllAlgos 各负责哪一环，并回答 `AddAlgToAllAlgos` 静态登记让谁受益。

**操作步骤**：

1. 选定一个具体场景：`HcclAllReduce`，FP32，count=1024，SUM，单机 8 卡 Mesh 拓扑，AICPU 引擎。
2. 按以下顺序在源码中定位并标注每个节点（括号内是文件:行）：
   - 入口装配：`FillAllReduceOpParam`（u2-l3，all_reduce_op.cc）。
   - 引擎选择：`HcclGetOpExpansionMode`（u2-l4，op_common.cc）。
   - 拓扑计算：`HcclCalcTopoInfo`（[op_common.cc:1107](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L1107)）。
   - 算法选择（画成**分叉**）：
     - 分支 a（新）：`SelectorEngine::Run`（[selector_engine.cc:181](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_engine.cc#L181)）→ InitCostModel（遍历 AllAlgos）→ CostTableGen → SelectMinCost → algName；
     - 分支 b（旧）：`ExecuteSelector::Run`（[execute_selector.cc:41](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/execute_selector.cc#L41)）→ 产出 algName（推测为 `AicpuAllReduceSoleMeshOneShot` 或 `AicpuAllReduceSoleMeshTwoShot`，取决于数据量）。
     - 分叉条件标注在分叉点：`IsNewSelectorEnabled() && SelectorEngine::IsOpSupported`（[op_common.cc:103](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L103)）。
   - executor 查询：`GetAlgExec`（[op_common.cc:663](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L663)）——两支汇合点。
   - 资源计算：`CalcAlgHierarchyInfo` + `CalcRes`（[op_common.cc:1204-1208](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L1204-L1208)）。
   - 编排执行：`Orchestrate`（[op_common.cc:752](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L752)）→ template `KernelRun`。
3. 在图上用四种颜色/标记标出注册体系的「介入点」：
   - 🔵 SelectorRegistry：分支 b 内查表拿 per-op selector。
   - 🟢 CollAlgExecRegistryV2：在 `HcclExecOp` 内查表拿 executor（新路径 InitCostModel 也查）。
   - 🟡 InsAlgTemplateRegistry：template 按名实例化时查表（多数情况已编译期绑定）。
   - 🟣 AllAlgos：分支 a 的候选清单（由注册宏经 `AddAlgToAllAlgos` 自动填充）。
4. 在图旁写两句注解：
   - 「主键」：整条链路由 algName 字符串串联，两支汇合于 `GetAlgExec`。
   - 「谁受益于 AddAlgToAllAlgos」：新选择器链路的三个消费者——`CostModelManager::InitCostModel`（遍历全集收集 A/B/C 系数）、`AlgoNameMapper::Init`（构建三维命名映射）、Tuner 插件（经 Enrich 后按三维名改写 cost）；同时算法维护者也受益——新增算法只需写注册宏一处，清单自动同步。

**预期产出**：一张包含 8 个左右节点（含一个分叉与汇合）、4 个注册体系标注、1 条 algName 主键流的可视化调度图（手绘或工具画均可），加一段「AddAlgToAllAlgos 受益者」的文字说明。

**待本地验证**：若你想确认 algName 到底选了哪个，可分别在两种开关下运行并对比日志：旧路径打开 HCCL_INFO 日志会打印 `[Algo][Selector] The selector[...] is matched, the selected algo type is <algName>`（见 [execute_selector.cc:46-48](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/execute_selector.cc#L46-L48)）；新路径（`HCCL_USE_NEW_SELECTOR=1`）会打印 `[SelectorEngine] SelectMinCost: selected algName=<algName>, engine=..., cost=...`（见 [selector_engine.cc:311](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_engine.cc#L311)）。具体日志级别开关以本地 CANN 版本为准。

## 6. 本讲小结

- op_common 由 **四大组件** 构成：selector（选算法，产出 algName）、executor（编排 + 算资源）、template（下发 kernel 搬数据）、topo（共享的拓扑适配）；本轮又新增了共享的 `SelectorEngine`/`CostModelManager`「新选择器」基础设施。
- op_common 的两个门面函数：`Selector()` 负责「选」，`HcclExecOp()` 负责「执行」，两者靠 **algName 字符串** 接力；`Selector`/`ReSelector` 内部本轮新增 **双路径分支**——`HCCL_USE_NEW_SELECTOR=1` 且算子（目前仅 AllReduce/ReduceScatter/AllGather）在白名单时走 `SelectorEngine`，否则走旧 `ExecuteSelector`，下游对路径选择无感知。
- 注册体系是「单例 + 工厂 + 静态初始化」：`SelectorRegistry`（键 opType/priority）、`CollAlgExecRegistryV2`（键 opType/algName）、`InsAlgTemplateRegistry`（键 template 名）；executor 与 template 的绑定在注册宏里**编译期**完成。
- 本轮所有 executor 注册宏新增 **`AddAlgToAllAlgos` 双写**：登记工厂的同时把算法五元组（algName/executor/templates/templateNum/opType）自动写进全局算法全集 `AllAlgos`，供新选择器的 `CostModelManager::InitCostModel`、`AlgoNameMapper` 与 Tuner 插件消费——清单与注册表永远同步，单一事实来源。
- executor 基类本轮新增 `CalcCostCoeff`/`GetAlgNetMeta` 虚函数挂钩（默认空实现），template 基类新增静态 `CalcCostCoeff`，都是为代价模型「申报自身开销」预留的扩展点。
- 整条数据流 `selector（新/旧）→ executor → template` 由 algName 这一脆弱的字符串契约串联，新增算法必须在 selector（产出 algName）和 executor 注册宏（消费 algName）两端同步。

## 7. 下一步学习建议

本讲搭好了骨架，接下来按 op_common 的组件逐个深入（顺序建议如下）：

1. **u3-l2 算法选择器 Selector**：深入旧路径 `ExecuteSelector::Run` 的优先级遍历、`AutoSelectorBase` 的引擎分发、`AllReduceAutoSelector` 如何根据拓扑 + 数据量产出 algName。本讲的 4.4 节已经预告了它的入口。
2. **u3-l3 拓扑适配与拓扑信息 Topo**：深入 `TopoInfoWithNetLayerDetails` 的字段含义（含本轮新增的 `hostDpuOnly`）与 `topo_match_*` 家族如何把网络匹配成多级子通信域——这是新旧两条选择路径决策的物理依据。
3. **u3-l4 算法执行器 Executor**：深入 `InsCollAlgBase` 的 `CalcAlgHierarchyInfo`/`CalcRes`/`Orchestrate` 三个生命周期函数，以及 `REGISTER_EXECUTOR_BY_FOUR_TEMPS` 如何编排多级流水线。
4. **u3-l5 算法模板 Template**：深入 `InsAlgTemplateBase` 的 `Describe`/`CalcRes`/`KernelRun`，看 template 如何被 executor 组合、本轮新增的 `CalcCostCoeff` 如何标定 A/B/C 系数。
5. **Unit 8（代价模型选择器与 Tuner 插件）**：本讲只点到 `SelectorEngine::Run` 的四步流程为止；它的 CostModel/CostTable/三维命名/Tuner 插件在 Unit 8 四讲中展开，学完 u3 全部组件后再读最顺。

阅读建议：每篇都先把本讲的「双路径数据流总图」摆在旁边，对照确认当前组件在链路中的位置，避免「只见树木不见森林」。

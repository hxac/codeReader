# u8-l1 新选择器 SelectorEngine 与双路径分发

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `HCCL_USE_NEW_SELECTOR` 开关的解析规则（默认值、合法取值、非法值行为），以及 `Selector()`/`ReSelector()` 如何据此做新旧选择器的双路径分发。
2. 逐步描述 `SelectorEngine::Run` 的四步流程：Tuner 初始化 → CostModel 获取/初始化 → CostTable 生成（可选 Tuner 修改）→ `SelectMinCost` 选出最小代价算法。
3. 解释 `GetEnginePriority` 的候选引擎回退链，以及 `hostDpuOnly` 拓扑为何直接短路为仅 HOSTCPU(DPU) 引擎。
4. 掌握 `ENGINE_PREFIX_MAP` 如何从 algName 前缀反推引擎，`FilterCmByEngine` 如何把不属于候选引擎的算法从 CostModel 中剔除。
5. 说明 `HCCL_USE_NEW_SELECTOR=1` 但算子不在白名单（AllReduce/ReduceScatter/AllGather）时的回退行为。

本讲是 Unit 8（代价模型选择器与 Tuner 插件）的第一讲，聚焦**选择流程本身**；代价模型的 A/B/C 参数建模与 CostTable 过滤规则在 u8-l2 展开，Tuner 插件在 u8-l4 展开。

## 2. 前置知识

阅读本讲前，你需要具备以下认知（来自前置讲义）：

- **algName 字符串契约**（u3-l1/u3-l2）：旧选择器 `ExecuteSelector` 按优先级遍历 per-op selector，产出一个 algName 字符串（如 `AicpuAllReduceSoleNHR`、`CcuMSAllReduceSoleMesh`），该字符串是 executor/template 注册表的键。新选择器的产出物**仍然是 algName**，下游 executor 完全无感知。
- **引擎与算法正交**（u2-l4）：`OpParam.opExecuteConfig` 在进入 Selector 前由 `HcclGetOpExpansionMode` 设定，是一个「引擎执行配置」枚举（AICPU_TS/AIV/CCU_MS 等）；Selector 选完算法后由 `SetCommEngine` 映射为最终 `CommEngine`。
- **环境变量系统**（u4-l3）：所有 `HCCL_` 环境变量由 `AlgEnvConfig`（`thread_local`）集中收纳，`InitEnvConfig()` 按序调用各 `Parse*` 函数，读端是一组 `GetExternalInput*()` getter。本轮新增的开关 `HCCL_USE_NEW_SELECTOR` 也遵循这一模式。
- **hostDpuOnly**（u2-l3/u3-l3）：`TopoInfoWithNetLayerDetails` 新增的布尔字段，由 topo 侧 `CalcHostDPUOnly` 判定「多机、多层、最外层 CLOS 全覆盖且 endpoint 全在 Host 侧」的专用拓扑，表示框间仅 Host DPU 可达。
- **算法全集 AllAlgos**（u3-l1/u3-l4）：executor 注册宏在登记执行器的同时向 AllAlgos 双写算法元数据，使「注册表里可执行的算法」与「可枚举的候选算法」天然同步——这正是新选择器候选集的单一事实来源。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/ops/op_common/selector/selector_engine.h](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_engine.h) | `SelectorEngine` 类声明：单例入口 `Run`、白名单 `IsOpSupported`、引擎推断 `GetEngineByAlgName`、优先级 `GetEnginePriority`、过滤 `FilterCmByEngine` |
| [src/ops/op_common/selector/selector_engine.cc](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_engine.cc) | 上述方法的全部实现，含 `InitCostModel` 与 `SelectMinCost` |
| [src/ops/op_common/op_common.cc](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc) | 门面函数 `Selector()`（首次选择）与 `ReSelector()`（资源不足回退时的重选），二者均含新旧选择器双路径分支 |
| [src/common/alg_env_config.cc](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_env_config.cc) | `ParseNewSelector()` 解析 `HCCL_USE_NEW_SELECTOR`，`IsNewSelectorEnabled()` 提供读端 getter |
| [src/ops/op_common/inc/alg_param.h](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/inc/alg_param.h) | `OpExecuteConfig` 枚举、`ENGINE_PREFIX_MAP` 前缀反查表、`ENGINE_STR_MAP` 日志映射、`TopoInfoWithNetLayerDetails::hostDpuOnly` |
| src/ops/op_common/selector/execute_selector.cc | 旧选择器 `ExecuteSelector::Run`（本讲作为对照引用） |
| src/ops/op_common/selector/cost_model.h / cost_table.h / src/common/tuner/tuner_setup.h | `CostModelManager`、`CostTableManager`、Tuner 接口（本讲只用到签名，细节见 u8-l2/u8-l4） |

## 4. 核心概念与源码讲解

### 4.1 HCCL_USE_NEW_SELECTOR 开关与双路径分发

#### 4.1.1 概念说明

costmodel 提交引入了一套全新的算法选择方式：不再靠「人工写在 selector 里的 if/else 规则」，而是**离线估算每个候选算法的耗时（代价），选代价最小的**。但新选择器当前只覆盖三个算子，且需要显式开启，因此 HCCL 在同一个门面函数里保留了新旧两条路径，用环境变量 `HCCL_USE_NEW_SELECTOR` 分发——这正是一个典型的「特性开关 + 灰度放量」工程实践：开关关、算子不支持、或者只想回退老流程，都能平滑退回。

#### 4.1.2 核心流程

```text
InitEnvConfig()（每次算子调用早期执行）
    └── ParseNewSelector()
        ├── HCCL_USE_NEW_SELECTOR 未设置 → 默认 false（走旧路径）
        ├── 值为 "0" → false
        ├── 值为 "1" → true
        └── 其他值 → HCCL_E_PARA 报错（EI0001 上报），初始化失败

Selector() / ReSelector()（算子执行链路）
    └── IsNewSelectorEnabled() && SelectorEngine::IsOpSupported(opType)？
        ├── 是 → SelectorEngine::Global()->Run(...)   【新路径：代价模型比价】
        └── 否 → ExecuteSelector().Run(...)           【旧路径：优先级遍历】
    └── 两条路径产出同一个 algName 字符串，后续 SetCommEngine/SetOpParamAlgTag 完全一致
```

#### 4.1.3 源码精读

开关解析遵循环境变量系统的「廉价优先、尽早失败」范式——非法值不静默纠正，而是报参数错误（与 u4-l3 讲过的 `RPT_ENV_ERR + CHK_PRT_RET` 两段式一致）：

[alg_env_config.cc:817-837](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_env_config.cc#L817-L837)——`ParseNewSelector`：未设置时默认关；只认 `"0"`/`"1"`；非法值返回 `HCCL_E_PARA`；合法值落入 `g_algEnvConfig.useNewSelector`（`thread_local`，每线程一份）。读端 getter 是 [alg_env_config.cc:1212](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_env_config.cc#L1212) 的 `IsNewSelectorEnabled()`，下游不直接 `getenv`。该解析在 `InitEnvConfig` 中的调用点见 [alg_env_config.cc:240-250](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_env_config.cc#L240-L250)。

双路径分发本体在门面函数 `Selector()` 中：

[op_common.cc:103-108](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L103-L108)——**新旧选择器双路径分支**：`IsNewSelectorEnabled() && SelectorEngine::IsOpSupported(param.opType)` 同时为真才走新路径 `SelectorEngine::Global()->Run`；否则走旧的 `ExecuteSelector::Run`。注意两个条件是「与」关系——这就是「开关开了但算子不在白名单」时的回退点：`IsOpSupported` 返回 false，整个表达式短路，无缝落入老流程。

无论走哪条路径，产出物都是 `algName` 字符串，之后的处理完全一致：[op_common.cc:113](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L113) 的 `SetCommEngine(param)` 把选择结果映射为最终 `CommEngine`，[op_common.cc:134](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L134) 的 `SetOpParamAlgTag` 生成资源缓存键。**新选择器没有引入任何新的下游契约。**

回退场景（资源不足时经 `FallbackOp` 触发）的 `ReSelector()` 有同样的分支：

[op_common.cc:579-593](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L579-L593)——`ReSelector` 先**强制把 `opExecuteConfig` 重置为 `AICPU_TS`**（AICPU 是万能兜底引擎，见 u3-l2），再走与 `Selector()` 完全相同的双路径分支。这带来一个新路径特有的细节：强制重置后，`GetEnginePriority` 会返回 `{AICPU_TS}` 单元素列表（见 4.3 节），且 CostModel 的 ctx tag 随引擎名变化（见 4.2 节），因此回退重选会命中/生成一份**AICPU 专属的 CostModel 副本**，与首次选择用的 CCU/AIV 副本互不污染。

#### 4.1.4 代码实践

**实践目标**：不依赖 NPU，通过源码阅读验证开关的三种取值行为，并确认白名单外算子的回退路径。

1. 打开 [src/common/alg_env_config.cc:817-837](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_env_config.cc#L817-L837)，对照下表逐行核验 `ParseNewSelector` 的分支：

   | `HCCL_USE_NEW_SELECTOR` 取值 | 结果 |
   | --- | --- |
   | 未设置（`"EmptyString"` 哨兵） | 默认 false，返回 SUCCESS |
   | `"0"` | false，返回 SUCCESS |
   | `"1"` | true，返回 SUCCESS |
   | `"true"` / `"2"` 等其他值 | `HCCL_E_PARA`，上层报 EI0001 |

2. 在仓库中执行 `grep -n "IsNewSelectorEnabled" -r src/`，确认它的全部调用点都在 `op_common.cc` 的 `Selector`/`ReSelector` 两处（即开关只影响算法选择入口，不影响其他子系统）。
3. 假想 `HCCL_USE_NEW_SELECTOR=1` 时下发一次 `HcclBroadcast`：`HcclCMDType::HCCL_CMD_BROADCAST` 不在 4.5 节的白名单集合中，`IsOpSupported` 返回 false → `&&` 短路 → 走 `ExecuteSelector::Run` 老路径。请把这条推理链写下来。

**需要观察的现象 / 预期结果**：这是一道源码推理题，静态阅读即可完成，无需运行。若在真实环境验证（待本地验证）：设置非法值时应观察到 `EI0001` 结构化报错与 `parse HCCL_USE_NEW_SELECTOR failed` 日志；设置 `1` 并执行 Broadcast 时，日志应出现 `Start to execute Selector.` 后紧跟旧路径的 `[Algo][Selector] The selector nums of optype...`，而**不出现** `[SelectorEngine] Run start`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ParseNewSelector` 对非法值返回错误而不是静默回退为默认值 0？
**答案**：静默纠正会让用户以为开关生效了而实际走的老路径，问题难以排查；「尽早失败 + 结构化上报（EI0001）」让配置错误在第一次调用时就暴露。这是 HCCL 环境变量系统统一的『廉价优先、尽早失败』原则（u4-l3）。

**练习 2**：`ReSelector` 为什么在进入双路径分支前先把 `opExecuteConfig` 重置为 `AICPU_TS`？
**答案**：`ReSelector` 只在资源不足回退（`FallbackOp`）时被调用，此时原引擎（如 CCU）的资源申请已失败，需要退到资源最充裕、算法覆盖最全的 AICPU 兜底引擎重选算法；重置后再走选择流程，新路径的 `GetEnginePriority` 据此只给 AICPU 候选，保证重选结果一定落在 AICPU 上。

**练习 3**：新选择器路径下，`SetCommEngine`/`LoadAICPUKernel` 等 Selector 后置步骤需要为新路径单独写一份吗？
**答案**：不需要。双路径分支只替换「algName 怎么选出来」这一步，`op_common.cc:109-139` 的全部后置处理（`SetCommEngine`、AIV_ONLY 检查、`LoadAICPUKernel`/`RegisterKernel`、`SetOpParamAlgTag`）对两条路径共用，algName 字符串契约不变。

### 4.2 SelectorEngine::Run 主流程

#### 4.2.1 概念说明

`SelectorEngine` 是新选择器的唯一执行入口，单例（`Global()`）。它的核心思想是：**算法选择 = 代价表查最小值**。为此它编排了四个参与者：

- **CostModelManager**：初始化 CostModel——对 AllAlgos 全集中每个算法，用其 `CalcCostCoeff` 申报的 A/B/C 系数计算代价参数（u8-l2 详解）。
- **CostTableManager**：针对本次调用的具体数据量/拓扑，把 CostModel 实例化成一张「算法 → cost」表，并按算子规则过滤（u8-l2 详解）。
- **Tuner 插件**：可选的外部 `.so`，在选出算法前有机会修改 cost 表（u8-l4 详解）。
- **comm ctx（通信域上下文）**：CostModel 按「通信域 × 引擎」缓存，同一通信域重复调用不重复建模。

#### 4.2.2 核心流程

```text
SelectorEngine::Run(comm, param, topoInfo, &algName)
│
├─ step 0  Tuner 初始化（每通信域仅一次）
│    └─ comm ctx 查 "tuner_init" 标签 → 未命中则 HcclTunerInit + 落标记
│
├─ step 1  获取或初始化 CostModel（按引擎区分缓存）
│    ├─ ctx tag = "costmodel_" + 引擎名字符串
│    ├─ 命中 → 直接取缓存副本
│    └─ 未命中 → InitCostModel():
│         ① call_once: AlgoNameMapper::Init(AllAlgos)   （全局一次，算法名→三维名映射）
│         ② CostModelManager::InitCostModel() 生成临时 srcCm
│         ③ 深拷贝进 comm ctx（header + 数组 + param 数组逐段拷贝）
│         ④ GetEnginePriority() → FilterCmByEngine()    （剔除非候选引擎的算法）
│         ⑤ FilterCmByHcclAlgo()                        （按 HCCL_ALGO 配置再过滤）
│
├─ step 2.1  CostTableManager::CostTableGen(cm, ct, topoInfo, param)
│            （按本次数据量/算子规则把 CostModel 实例化为 cost 表）
│
├─ step 2.2  （仅当 Tuner 插件已加载）
│    ├─ AlgoNameMapper::Enrich()  填充三维用户可读名
│    └─ HcclTunerCallGetCollInfo() 让插件改 cost，返回是否修改
│
└─ step 3  SelectMinCost(ct) → algName + 回写 param.opExecuteConfig
```

#### 4.2.3 源码精读

[selector_engine.cc:28-32](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_engine.cc#L28-L32)——`Global()` 用函数内静态指针实现惰性单例。

[selector_engine.cc:180-243](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_engine.cc#L180-L243)——`Run` 主体，四个 step 与注释一一对应。几个值得精读的设计点：

- **step 0（L187-194）**：Tuner 初始化用 `HcclEngineCtxGet` 查 `tuner_init` 标签做「每通信域仅一次」守卫——查不到就 `HcclTunerInit` 并 `HcclEngineCtxCreate` 落一个 1 字节标记。Tuner 的生命周期与 costModel 副本无关。
- **step 1（L196-206）**：CostModel 的缓存键是 `costmodel_<引擎名>`（L200 用 `ENGINE_STR_MAP.at(param.opExecuteConfig)` 拼出）。**按引擎区分副本**是刻意设计：回退场景（`ReSelector` 强制 AICPU）引擎变了，tag 也变，会命中/生成另一份副本，引擎间的过滤结果互不污染。
- **step 2.2（L214-225）**：只有 `HcclTunerIsLoaded()` 为真（用户配置了 `HCCL_TUNER_PLUGIN` 且加载成功，见 u8-l4）才走 Tuner；`Enrich` 先把表项填充为用户可配置的三维名，`HcclTunerCallGetCollInfo` 把整张 cost 表交给插件，由插件决定改不改。
- **step 3（L227-237）**：`SelectMinCost` 后无论成败都 `delete[] ct.costs` 释放临时表。

[selector_engine.cc:117-178](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_engine.cc#L117-L178)——`InitCostModel` 私有方法。三个要点：

- L123-126：`std::call_once` 保证 `AlgoNameMapper::Init(*GetAllAlgos())` 进程级只执行一次——用 AllAlgos（executor 注册宏双写登记的算法全集，u3-l4）构建「算法名 → 三维名」映射缓存。
- L129-166：CostModel 含裸指针（`costAlgoParams` 指向数组、数组元素又内嵌 `param` 指针），因此做**两段深拷贝**先进 comm ctx：先拷 `[CostModel header][CostAlgoParams array]` 连续块（L134-146），再逐项为 `param` 数组 new 独立内存（L149-163），随后释放临时 `srcCm`。注释点明动机：comm ctx 持有独立所有权，costModel 作为出参返回且无线程安全问题。
- L168-172：初始化的最后一步就地完成**两级过滤**——`GetEnginePriority` 得到候选引擎 → `FilterCmByEngine` 剔除非候选引擎 → `FilterCmByHcclAlgo`（声明于 [alg_parse.h:62](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_parse.h#L62)，按 `HCCL_ALGO` 配置过滤，规则见 u8-l3）。注意过滤发生在**缓存进 ctx 之前**，所以过滤结果随副本一起被缓存。

与 CostModelManager/CostTableManager 的边界：本讲只需要知道 [cost_model.h:88](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/cost_model.h#L88) 的 `CostModelManager::InitCostModel(comm, topoInfo, costModel)` 负责生成全量代价参数、[cost_table.h:53](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/cost_table.h#L53) 的 `CostTableGen(cm, ct, topoInfo, opParam)` 负责按本次调用实例化过滤——二者内部机制分别是 u8-l2 与 u8-l3 的主题。

#### 4.2.4 代码实践

**实践目标**：以源码阅读方式画出 `SelectorEngine::Run` 的完整流程图，并与旧 `ExecuteSelector::Run` 对比。

1. 通读 [selector_engine.cc:180-243](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_engine.cc#L180-L243)，为每个 step 标注：输入、输出、缓存键（tuner 用 `tuner_init`，costModel 用 `costmodel_<引擎名>`）。
2. 再通读旧路径 [execute_selector.cc:19-55](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/execute_selector.cc#L19-L55)：它从 `SelectorRegistry` 取该算子的 selector 列表，按 priority 升序调用 `Select`，返回 `MATCH` 即止。
3. 画一张双栏对比图（文字版即可）：

   ```text   （示例代码：流程对照，非项目源码）
   旧 ExecuteSelector::Run                新 SelectorEngine::Run
   ─────────────────────────              ─────────────────────────
   注册表取 per-op selectors              step0 Tuner 一次性初始化
        │                                 step1 取/建 CostModel（comm ctx 缓存）
        ▼                                      │（初始化时两级过滤：
   按 priority 逐个 Select()                    引擎过滤 + HCCL_ALGO 过滤）
   MATCH 即返回 algName                        ▼
        │                                 step2 CostTableGen（按本次数据量算 cost）
   全 NOT_MATCH → 失败                     step2.2 Tuner 可改 cost（可选）
                                           step3 SelectMinCost → algName
   ```

4. 思考并记录：两条路径的「选择依据」分别是什么？（旧：selector 代码里固化的拓扑/引擎规则；新：数值化的代价比较 + 可被 Tuner/HCCL_ALGO 干预。）

**需要观察的现象 / 预期结果**：纯源码阅读实践。若在真实环境验证（待本地验证，需 A5 类设备 + `HCCL_USE_NEW_SELECTOR=1`）：日志应依次出现 `[SelectorEngine] Run start`、`costModel initialized and stored in comm ctx`（或 `costModel found in comm ctx`）、`SelectMinCost: costTable count=...`，最后 `selected algName=..., engine=..., cost=...`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 CostModel 要按「通信域 × 引擎」双维度缓存，而不是每个算子调用现算一份？
**答案**：CostModel 的内容只依赖通信域拓扑与候选引擎集合，与本次调用的数据量无关；缓存后同一通信域的重复调用只做轻量的 CostTableGen。按引擎区分副本是为了回退场景（ReSelector 强制 AICPU）不污染原引擎的过滤结果——引擎变更会命中不同 tag 的副本。

**练习 2**：`InitCostModel` 里为什么要 `std::call_once` 调 `AlgoNameMapper::Init`，而不是在构造函数里做？
**答案**：`SelectorEngine` 是惰性单例（首次使用才创建），且 `AlgoNameMapper::Init` 依赖 `GetAllAlgos()` 返回的算法全集；`call_once` 保证多线程首次并发进入 `Run` 时映射只构建一次，同时把初始化时点推迟到真正需要时，避免静态初始化顺序问题。

**练习 3**：`CostTableGen` 之后 cost 表为空（`ct.count <= 0`）时 `Run` 会怎样？
**答案**：只打 `HCCL_WARNING`（L212-213），继续走 `SelectMinCost`；空表中 `minIdx` 保持 -1，`SelectMinCost` 返回 `HCCL_E_NOT_SUPPORT`，`Run` 以失败结束并打 `Run failed, no algorithm selected`。即空表不是静默成功，而是显式失败。

### 4.3 候选引擎优先级 GetEnginePriority 与 hostDpuOnly 短路

#### 4.3.1 概念说明

进入新选择器之前，`param.opExecuteConfig` 已经由 `HcclGetOpExpansionMode` 设定了一个「期望引擎」（u2-l4）。但期望不等于唯一候选：CCU 资源不足或算法未覆盖时需要能退到 AICPU。`GetEnginePriority` 把单一期望引擎展开成一条**按优先级从高到低排列的回退链**，CostModel 过滤与最终比价都只在这条链上的引擎中进行。唯一例外是 `hostDpuOnly` 拓扑：框间只有 Host DPU 可达时，一切 device 侧引擎都无意义，直接短路为仅 HOSTCPU。

#### 4.3.2 核心流程

```text
GetEnginePriority(topoInfo, opExecuteConfig)
    │
    ├─ topoInfo->hostDpuOnly == true ？ ──是──→ 返回 [HOSTCPU]（唯一候选）
    │
    └─ 按 opExecuteConfig 查回退链：
        CCU_MS   → [CCU_MS,   CCU_SCHED, AICPU_TS]
        CCU_SCHED→ [CCU_SCHED, AICPU_TS]
        AIV     → [AIV,      AICPU_TS]
        AIV_ONLY→ [AIV]                （不回退）
        AICPU_TS→ [AICPU_TS]
        HOSTCPU → [HOSTCPU]
        其他    → [AICPU_TS]           （安全兜底）
```

#### 4.3.3 源码精读

[selector_engine.cc:68-100](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_engine.cc#L68-L100)——`GetEnginePriority` 全文。两个关键点：

- L72-75：`hostDpuOnly` 检查放在 switch **之前**，优先于一切引擎配置——只要拓扑判定为 Host DPU 专用（该字段由 topo 侧 `HcclCalcTopoInfo` 计算并缓存，见 u3-l3），无论 `opExecuteConfig` 是什么都只返回 `{HOSTCPU}`。字段定义在 [alg_param.h:227](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/inc/alg_param.h#L227)（`bool hostDpuOnly{false}`，默认 false）。
- L77-99：回退链与 u3-l2 讲过的旧选择器降级链 `CCU_MS→CCU_SCHED→AICPU_TS` 一致——新选择器没有发明新的降级语义，而是把人工 selector 里的降级逻辑**显式数据化**成候选列表。`AIV_ONLY` 不回退（与 `Selector()`/`ReSelector()` 里的 AIV_ONLY 硬检查呼应），`default` 分支安全回退为仅 AICPU。

`OpExecuteConfig` 枚举本身定义于 [alg_param.h:125-136](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/inc/alg_param.h#L125-L136)，注意其中同时存在 `AICPU_TS`（运行时配置值）与 `AICPU`（内部值）、`HOSTCPU_TS` 与 `HOSTCPU` 的区分——ENGINE_PREFIX_MAP 只映射到前者集合。

#### 4.3.4 代码实践

**实践目标**：验证 hostDpuOnly 短路对下游两处（引擎过滤、CostTable）的影响路径。

1. 在 [selector_engine.cc:169-172](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_engine.cc#L169-L172) 确认 `GetEnginePriority` 的返回值被两处消费：`FilterCmByEngine`（4.4 节）与 `CandidateEnginesToPrefixes` → `FilterCmByHcclAlgo`。
2. 推理：hostDpuOnly=true、`opExecuteConfig=CCU_MS` 时，候选链是 `[HOSTCPU]` 而非 `[CCU_MS, CCU_SCHED, AICPU_TS]`——所有 `Aicpu/Aiv/CcuMS/CcuSched` 前缀的算法都被过滤掉，只有 `Dpu` 前缀算法进入比价。
3. 对照 u3-l3 讲过的 `CalcHostDPUOnly` 判定条件（多机、多层、最外层 CLOS 全覆盖且 endpoint 全在 Host 侧），解释为什么这类拓扑下保留 device 引擎候选是纯浪费：CostModel 初始化要对每个算法做带宽参数计算，被过滤的算法白算。

**需要观察的现象 / 预期结果**：源码推理实践。真机验证待本地验证：hostDpuOnly 环境下日志应出现 `[GetEnginePriority] hostDpuOnly=true, return [HOSTCPU].`（L73）。

#### 4.3.5 小练习与答案

**练习 1**：`hostDpuOnly` 的检查为什么放在 `GetEnginePriority` 而不是放在每个算子的 executor 里？
**答案**：它是拓扑级事实（通信域级、一次判定），不是算法级事实。放在候选引擎生成的最上游，一次短路即可让后续的 CostModel 过滤、CostTable 生成、比价全程只见 HOSTCPU 候选，避免在每个下游分散判断；且 hostDpuOnly 已随 TopoInfoWithNetLayerDetails 序列化同步，Selector 侧零额外计算。

**练习 2**：`AIV_ONLY` 在回退链里不回退，与 `op_common.cc` 里哪段代码形成双保险？
**答案**：与 [op_common.cc:114-121](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L114-L121)（`ReSelector` 中同型检查在 L599-606）的 AIV_ONLY 硬检查形成双保险：选择层不给非 AIV 候选，门面层在选择后再次校验 `param.engine != COMM_ENGINE_AIV` 即报 `HCCL_E_NOT_SUPPORT`——即使选择器实现有漏，语义仍不会被破坏。

### 4.4 引擎前缀反查与 CostModel 过滤

#### 4.4.1 概念说明

新选择器面对的 CostModel 里，每个算法条目只有一个 `algName` 字符串（如 `CcuMSAllReduceSoleMesh`），而候选过滤需要知道「这个算法跑在哪个引擎上」。HCCL 没有另建一张「算法 → 引擎」注册表，而是直接利用 algName 的**命名约定**（引擎前缀 + 算子 + 编排 + 拓扑，u3-l2）：`ENGINE_PREFIX_MAP` 记录 5 个引擎前缀，前缀匹配即可反推引擎。这个设计的代价是 algName 前缀成为隐式契约，收益是零冗余数据结构。

#### 4.4.2 核心流程

```text
algName ──rfind(前缀,0)==0 长前缀优先──→ OpExecuteConfig
    CcuSched… → CCU_SCHED     （先匹配，长前缀优先）
    CcuMS…    → CCU_MS
    Aicpu…    → AICPU_TS
    Aiv…      → AIV
    Dpu…      → HOSTCPU
    其他      → AICPU_TS（默认）

FilterCmByEngine(cm, candidateEngines):
    对 cm 中每个 algName 非空的条目:
        engine = GetEngineByAlgName(algName)
        engine ∉ candidateEngines → 该条目 count 置 0（不参与后续比价）
```

#### 4.4.3 源码精读

[alg_param.h:138-150](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/inc/alg_param.h#L138-L150)——`ENGINE_PREFIX_MAP`（前缀 → 引擎）与 `ENGINE_STR_MAP`（引擎 → 日志字符串）。注意 `ENGINE_PREFIX_MAP` 是 `std::map`，key 按字典序排列为 `Aicpu, Aiv, CcuMS, CcuSched, Dpu`——**字典序恰好使 `CcuMS` 排在 `CcuSched` 前面**，正序遍历会先命中短前缀 `CcuMS`，所以反查必须逆序。

[selector_engine.cc:45-54](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_engine.cc#L45-L54)——`GetEngineByAlgName`：`rbegin/rend` **逆序遍历实现长前缀优先**（`CcuSched` 先于 `CcuMS` 被尝试），否则 `CcuSchedAllReduce…` 会被误判为 CCU_MS。无任何前缀命中时默认 AICPU_TS。

[selector_engine.cc:56-66](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_engine.cc#L56-L66)——`CandidateEnginesToPrefixes`：把候选引擎集合转成前缀字符串列表，供 `FilterCmByHcclAlgo`（HCCL_ALGO 配置过滤）把引擎约束传递下去。用 `std::set` 去重保证输出顺序跟随 map 而非入参顺序。

[selector_engine.cc:102-115](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_engine.cc#L102-L115)——`FilterCmByEngine`：对每个 `algName` 非空条目反查引擎，不在候选集合就把条目的 `count` 置 0。一个诚实的细节：头文件 [selector_engine.h:44](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_engine.h#L44) 的注释写的是「count 置 -1」，而实现（L111）实际置 **0**——以代码为准，语义都是「该算法的代价参数条目失效、退出比价」。被过滤条目的 algName 仍保留（置空指针的只有 CostTableGen 一侧），因此后续 CostTable 生成仍能看到这些名字并以 filtered 状态打印（见 4.5 节表格）。

#### 4.4.4 代码实践

**实践目标**：手工执行一遍前缀反查，验证长前缀优先的必要性。

1. 对下列 algName 手工跑 `GetEngineByAlgName`，写出结果（答案见下）：
   - `CcuSchedAllReduceSoleMesh`
   - `CcuMSAllReduceSoleMesh`
   - `AicpuAllReduceSoleNHR`
   - `DpuAllReduceMeshNHR`
2. 假设改成**正序**遍历 `ENGINE_PREFIX_MAP`，`CcuSchedAllReduceSoleMesh` 会被判成什么引擎？为什么会污染 `FilterCmByEngine` 的结果？（提示：map 字典序 `CcuMS` < `CcuSched`，正序先试 `CcuMS`，`rfind("CcuMS",0)` 不命中；再想 `Aicpu` 与 `Aiv`、以及若未来加入 `CcuM` 类前缀的情形。）
3. 用 `grep -rn "CcuSched" src/ops/*/executor/ | head` 找一个 `CcuSched` 前缀的真实注册 algName，确认它确实会被逆序遍历正确归类。

**参考答案（第 1 步）**：CCU_SCHED、CCU_MS、AICPU_TS、HOSTCPU。
**预期结果**：纯推理实践。第 2 步结论：对现有 5 个前缀，`CcuMS` 不是 `CcuSched…` 的前缀（第 4 个字符 M≠S），正序/逆序结果碰巧相同；但逆序遍历是对「前缀之间存在包含关系」的通用防御（如未来出现 `Ccu` 与 `CcuMS` 并存时正序必错），代码注释 `长前缀优先(CcuSched > CcuMS)` 表达的正是这一约定。

#### 4.4.5 小练习与答案

**练习 1**：`ENGINE_PREFIX_MAP` 依赖 algName 命名约定，这带来什么维护约束？
**答案**：任何新算法的 algName 前缀必须与其实际运行引擎一致，否则 `GetEngineByAlgName`/`FilterCmByEngine` 会把它归错引擎——要么被错误过滤（永远选不上），要么被错误保留（比价赢了却无法执行）。这正是 u3-l2 强调的「algName 字符串契约必须与注册严格一致」在新选择器中的又一次体现。

**练习 2**：为什么 `SelectMinCost`（4.5 节）最后还要再调一次 `GetEngineByAlgName(algName)` 回写 `param.opExecuteConfig`？
**答案**：进入 `Run` 时的 `opExecuteConfig` 只是「期望引擎」，最终胜出的算法可能来自回退链上更低的引擎（如 CCU_MS 链上的 AICPU 算法）。回写让 `param` 上的引擎配置与实际选中算法一致，供 `SetCommEngine` 等下游使用。

### 4.5 IsOpSupported 白名单与 SelectMinCost 最小代价选择

#### 4.5.1 概念说明

新选择器是渐进放量的：本迭代只对三个最常用的集合通信算子开放，其余算子一律走老路径——这个边界就是 `IsOpSupported` 白名单。而 `SelectMinCost` 是整条新路径的终点：遍历 CostTable，跳过被过滤条目（algName 为空或 cost 为负），取 cost 最小的算法作为 algName，并把胜出算法的引擎回写进 `param`。

#### 4.5.2 核心流程

```text
IsOpSupported(opType):
    opType ∈ {ALLREDUCE, REDUCE_SCATTER, ALLGATHER} → true，否则 false

SelectMinCost(ct, param, &algName):
    遍历 ct.costs:
        algName == nullptr 或 cost < 0 → 视为 filtered，跳过
        cost < minCost → 更新最小，清空并列集
        cost == minCost → 加入并列集
    无有效条目 → HCCL_E_NOT_SUPPORT
    多个并列最小 → 取首者，HCCL_WARNING 提示
    algName = costs[minIdx].algName
    param.opExecuteConfig = GetEngineByAlgName(algName)   （引擎回写）
```

#### 4.5.3 源码精读

[selector_engine.cc:34-43](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_engine.cc#L34-L43)——`IsOpSupported`：`static const std::set` 内联白名单，仅含 `HCCL_CMD_ALLREDUCE`、`HCCL_CMD_REDUCE_SCATTER`、`HCCL_CMD_ALLGATHER` 三个枚举值。函数级 static 保证集合只构造一次。它被 `op_common.cc` 两处双路径分支调用（4.1 节），是新旧路径的仲裁条件之一。

[selector_engine.cc:245-314](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_engine.cc#L245-L314)——`SelectMinCost`。值得精读的细节：

- L251-253：以 ASCII 表格形式把整张 costTable 打进 INFO 日志（`| idx | algName | engine | cost | status |`），这是排查「为什么选了算法 A 不选 B」的第一手证据——运维/调优时直接看这张表。
- L260、L274：过滤判定是 `name == nullptr || cost < 0.0f`——与 4.4 节呼应：引擎过滤（count 置 0）在 CostModel 层面，CostTableGen 会把不可用条目转成空名或负 cost（具体规则在 u8-l2）。
- L288-293：全部条目被过滤时返回 `HCCL_E_NOT_SUPPORT`——注意这**不会**自动回退老选择器：双路径分支只看「开关 + 白名单」，一旦进入新路径，选不出算法就是失败，由上层错误处理链接管。
- L295-306：cost 并列时取首者并 WARNING 列出全部并列算法——代价模型是浮点估算，并列（尤其多个 0.0）并不罕见，日志留痕便于解释选择结果。
- L308-312：`algName = ct.costs[minIdx].algName` 后立即 `GetEngineByAlgName` 回写 `param.opExecuteConfig`（原因见 4.4 练习 2），并以 `ENGINE_STR_MAP` 打出最终引擎名。

#### 4.5.4 代码实践

**实践目标**：用日志级源码推演一次完整的「过滤 → 比价 → 胜出」过程（无需 NPU 的纸面推演）。

构造一个假想的 costTable（示例数据，非项目真实输出）：

| idx | algName | engine | cost | status |
| --- | --- | --- | --- | --- |
| 0 | `AicpuAllReduceSoleNHR` | AICPU_TS | 1.80 | valid |
| 1 | `AicpuAllReduceSoleMeshOneShot` | AICPU_TS | 0.90 | valid |
| 2 | nullptr | - | - | filtered |
| 3 | `CcuMSAllReduceSoleMesh` | CCU_MS | -1.00 | filtered |

1. 按 `opExecuteConfig=AICPU_TS`、`hostDpuOnly=false` 写出 `GetEnginePriority` 结果（应为 `[AICPU_TS]`），并解释 idx=3 为何 cost 为负：它是 CCU_MS 引擎算法，不在候选链上，`FilterCmByEngine` 已把其 CostModel 条目 count 置 0，CostTableGen 随之判为不可用。
2. 手工执行 `SelectMinCost` 循环：记录 minIdx、minCost、tiedAlgos 的每步变化，写出最终 `algName` 与回写后的 `param.opExecuteConfig`。（答案：minIdx=1，minCost=0.90，algName=`AicpuAllReduceSoleMeshOneShot`，opExecuteConfig=AICPU_TS。）
3. 把上表改成 idx=1 与 idx=3 cost 同为 0.90 且 idx=3 有效，验证并列分支：应选首者（idx=1）并触发 `multiple algos with same cost` WARNING。

**需要观察的现象 / 预期结果**：纸面推演。真机验证待本地验证：A5 类设备上开启 `HCCL_USE_NEW_SELECTOR=1` 与 INFO 日志，可在 host 日志中搜到 `[SelectorEngine]` 前缀的 costTable 表格与 `SelectMinCost: selected algName=...`。

#### 4.5.5 小练习与答案

**练习 1**：`HCCL_USE_NEW_SELECTOR=1` 时下发 AllGatherV，实际走哪条路径？为什么白名单不含 V 类算子？
**答案**：走旧路径。`HCCL_CMD_ALLGATHER_V` 不在 `IsOpSupported` 白名单，`&&` 短路落入 `ExecuteSelector::Run`。V 类（变长）算子的代价建模需要按 counts/displs 数组逐 rank 估算流量，比标量 count 复杂得多，本迭代先覆盖三个最常用、参数最规整的算子，属于渐进放量策略。
**练习 2**：新路径 `SelectMinCost` 失败（`HCCL_E_NOT_SUPPORT`）后会自动回退老选择器再试一次吗？
**答案**：不会。双路径分发点在 `Selector()`/`ReSelector()` 里，只判断「开关 + 白名单」，分支一旦选定便不可逆；新路径内选不出算法直接向上返回错误。要回老路径只能不设 `HCCL_USE_NEW_SELECTOR`（或设 0）。
**练习 3**：`SelectMinCost` 打印表格的行为对调优有什么价值？
**答案**：它把「每个候选算法的引擎归属、估算代价、是否被过滤」全部暴露在 INFO 日志里，配合 Tuner 插件（u8-l4）可以验证「插件把某算法 cost 改小后是否真的胜出」，是代价模型与调优策略的可观测性基础。

## 5. 综合实践

**任务：为一次 AllReduce 调用写出新旧两条路径的完整选择时序，并验证三个分发边界。**

背景设定：8 卡单机通信域，`HCCL_USE_NEW_SELECTOR=1`，`opExecuteConfig` 由环境展开模式决定为 `CCU_MS`，`hostDpuOnly=false`。

1. **画双路径时序图**。左栏旧路径：`Selector()` → `ExecuteSelector::Run`（[execute_selector.cc:19-55](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/execute_selector.cc#L19-L55)，按 priority 遍历 per-op selector 直到 MATCH）；右栏新路径：`Selector()` → `SelectorEngine::Run`（step0 Tuner → step1 CostModel 缓存/初始化（含 `GetEnginePriority` → `FilterCmByEngine` → `FilterCmByHcclAlgo`）→ step2 CostTableGen（+可选 Tuner）→ step3 `SelectMinCost`）。两条路径汇合于 `SetCommEngine` + `SetOpParamAlgTag`。
2. **标注三个分发边界**并各写一句解释：
   - 开关边界：`IsNewSelectorEnabled()`（[alg_env_config.cc:1212](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_env_config.cc#L1212)）；
   - 白名单边界：`IsOpSupported`（仅 AllReduce/ReduceScatter/AllGather）；
   - 回退边界：`ReSelector` 强制 AICPU_TS 后重走同一双路径分支，且因 costModel tag 带引擎名而命中不同副本。
3. **推演本场景候选链**：`CCU_MS` 的回退链是 `[CCU_MS, CCU_SCHED, AICPU_TS]`，所以 costTable 中三种引擎前缀的算法都可能 valid；若 CCU 资源受限导致 CCU 算法在 CostTableGen 阶段被规则过滤，胜出者将是链上更低的 AICPU 算法，且 `param.opExecuteConfig` 被回写为 AICPU_TS——把这条推演写进你的图注。
4. **（选做，待本地验证）**：真机 8 卡环境运行一次 AllReduce，对比开关 0/1 两种配置下 `[Algo][Selector]`（旧）与 `[SelectorEngine]`（新）两组日志，验证你画的时序与真实执行一致。

## 6. 本讲小结

- `HCCL_USE_NEW_SELECTOR` 是新选择器的总开关：未设置默认关、只认 0/1、非法值报 EI0001；`Selector()`/`ReSelector()` 以 `IsNewSelectorEnabled() && IsOpSupported(opType)` 做双路径分发，两路径产出同一 algName 契约，下游无感知。
- `SelectorEngine::Run` 四步：Tuner 每通信域一次初始化 → 按 `costmodel_<引擎名>` 取/建 CostModel（初始化时完成引擎过滤 + HCCL_ALGO 过滤并随副本缓存）→ CostTableGen 按本次调用实例化（可选 Tuner 改 cost）→ SelectMinCost 取最小。
- `GetEnginePriority` 把期望引擎展开为回退链（`CCU_MS→CCU_SCHED→AICPU_TS` 等），AIV_ONLY 不回退；`hostDpuOnly` 拓扑在最上游短路为仅 HOSTCPU，使整个比价只在 DPU 前缀算法中进行。
- `ENGINE_PREFIX_MAP` 依据 algName 命名约定反查引擎，逆序遍历实现长前缀优先；`FilterCmByEngine` 把非候选引擎条目的 count 置 0（头文件注释写 -1，以实现为准）退出比价。
- `IsOpSupported` 白名单仅含 AllReduce/ReduceScatter/AllGather；白名单外算子即便开关打开也走旧路径；新路径内选不出算法直接失败、不自动回退老选择器。
- `SelectMinCost` 以 INFO 日志输出整张 costTable（idx/algName/engine/cost/status），并列取首者并 WARNING，最后用 `GetEngineByAlgName` 把胜出算法的引擎回写 `param.opExecuteConfig`。

## 7. 下一步学习建议

- **u8-l2（CostModel 代价建模与 CostTable 生成）**：本讲把 `CostModelManager::InitCostModel` 与 `CostTableGen` 当黑盒，下一讲拆开它们——A/B/C 三参数如何由各模板的 `CalcCostCoeff` 申报、CostTable 的算子级过滤规则（如 `FilterAllReduce`）如何把不满足条件的算法 cost 置负。
- **u8-l3（算法三维命名与 HCCL_ALGO 解析）**：深入 `AlgoNameMapper::Init/Enrich` 与 `FilterCmByHcclAlgo`/`UpdateCostModelWithAlgo`，理解用户可配置的 engine/executor/template 三维名如何与内部 algName 互转。
- **u8-l4（Tuner 插件框架与实践）**：本讲 step 0/2.2 的 `HcclTunerInit`/`HcclTunerCallGetCollInfo` 的完整机制：`hcclTunerPlugin_v1` C ABI、dlopen 加载、慢调用保护与 examples/06_tuner_plugin 参考实现。
- 源码复习建议：重读 [selector_engine.cc](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_engine.cc) 全文（317 行，可一次读完），对照本讲 4.2 的流程图逐 step 核对。

# u8-l2 CostModel 代价建模与 CostTable 生成

## 1. 本讲目标

在 u8-l1 中我们知道了新选择器 `SelectorEngine::Run` 的四步骨架：Tuner 初始化 → 取/建 CostModel → CostTableGen → SelectMinCost。本讲向下钻一层，读完本讲你应当能够：

1. 说清 `CostModelParam` 的 A/B/C 三参数分别建模什么开销，以及最终耗时公式 \( T(n) = (A/u + B) \cdot D + C \) 中每一项的含义。
2. 理解 `CostModelManager` 如何用一组带宽常数（`InitBandwidth`）和四个 `Calc*Params` 函数算出 A/B/C，以及 `InitCostModel` 如何遍历算法全集 AllAlgos 生成「通信域 × 引擎」粒度的 CostModel。
3. 掌握模板/执行器通过 `CalcCostCoeff`/`GetAlgNetMeta` 两个虚函数向代价模型「申报自身代价」的挂钩机制，以及 `AlgNetMetaRegistry` 登记的组网元数据如何参与计价。
4. 掌握 `CostTableManager` 的 `CostTableGen` → `FilterByRules`（必选/必不选规则）→ `CalcAlgCost`（含 UB 利用率查表）全流程，并理解 `SelectMinCost` 如何挑出最终 algName。

## 2. 前置知识

- **α-β 耗时模型**（u1-l2 已学）：一次通信的耗时 \( T = \alpha + n\beta + n\gamma \)，α 是启动时延，β 是每字节传输开销，γ 是每字节计算（归约）开销。本讲的 A/B/C 三参数正是这一模型的工程化落地：A 对应「跨卡传输的 β」，B 对应「本地拷贝/归约的 β＋γ」，C 对应「α 时延常数」。
- **算法全集 AllAlgos**（u3-l1/u3-l4 已学）：执行器注册宏 `REGISTER_EXEC_V2` 在登记 executor 的同时调用 `AddAlgToAllAlgos` 把算法元数据（algName/executorName/templateName/opType）写入全局 `AllAlgos`，它是新选择器候选集的「单一事实来源」。
- **TopoInfoWithNetLayerDetails**（u3-l3 已学）：拓扑快照，含 `topoLevelNums`（算法可见层级数）、`userRankSize`、`level0Topo`（MESH_1D/CLOS 等形状）、`hostDpuOnly` 等字段，本讲的规则过滤大量依赖它。
- **执行器三层生命周期**（u3-l4 已学）：`CalcAlgHierarchyInfo` → `CalcRes` → `Orchestrate`；本轮在 `InsCollAlgBase` 上新增的 `CalcCostCoeff`/`GetAlgNetMeta` 是与这三步并行的第四类职责——**host 侧离线代价标定**，它不下发任何数据，只产出数字。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/ops/op_common/selector/cost_model.h` | 定义 `CostModelParam`（A/B/C）、`CostModel`、`CostModelManager`、`CalcCostCoeffParam`、`AlgNetMeta`、`AlgNetMetaRegistry` 及算法全集 `AllAlgos` |
| `src/ops/op_common/selector/cost_model.cc` | 带宽常数初始化、A/B/C 四个计算函数、`InitCostModel` 的「拓扑过滤→查执行器→采样系数→登记元数据」流程 |
| `src/ops/op_common/selector/cost_table.h` | 定义 `CostTable`、`AlgFilterRule`（规则结构）、`UbUtilEntry`（UB 利用率查表项）与 `CostTableManager` |
| `src/ops/op_common/selector/cost_table.cc` | 三个算子的规则集构建（`BuildAllReduceRules` 等）、`FilterByRules` 过滤引擎、`CalcAlgCost` 计价、`QueryUbUtil` 查表 |
| `src/ops/all_reduce/template/aicpu/ins_temp_all_reduce_mesh_1D_one_shot.cc` | one-shot Mesh AllReduce 模板，含静态代价标定 `CalcCostCoeff` 的完整实现 |
| `src/ops/op_common/executor/executor_v2_base.h` | `InsCollAlgBase` 基类，声明 `CalcCostCoeff`/`GetAlgNetMeta` 虚函数（默认空实现） |
| `src/ops/all_reduce/executor/ins_v2_all_reduce_sole_executor.cc` | sole 执行器：`CalcCostCoeff` 委托给模板的示例 |
| `src/ops/all_reduce/executor/ins_v2_all_reduce_sequence_executor.cc` | sequence 执行器：聚合并级模板系数、申报多段 `AlgNetMeta` 的示例 |
| `src/ops/op_common/selector/selector_engine.cc` | `SelectorEngine::Run` 的 step 2.1/3 与 `SelectMinCost`（本讲的消费方） |

## 4. 核心概念与源码讲解

### 4.1 CostModelParam：A/B/C 三参数建模

#### 4.1.1 概念说明

新选择器要在**不真正跑通信**的前提下比较几十个候选算法的快慢，因此每个算法必须能用一个统一的解析式估算耗时。HCCL 选择的建模是：把一次算法的耗时拆成三段，每段只看它随数据量 D 的变化关系：

\[
T(D) = \left(\frac{A}{u} + B\right) \cdot D + C
\]

- **A**：跨卡传输时间随 D 的斜率（单位：秒/字节）。它会受到 UB（Unified Buffer，片上统一缓冲）带宽利用率 u 的影响——小数据量时流水线填不满，实际带宽打折扣，所以计价时 A 要除以 u。
- **B**：本地搬运（拷贝＋归约）时间随 D 的斜率，不直接走跨卡链路，故不受 u 影响。
- **C**：与数据量无关的时延常数（任务下发、同步等固定开销），对应 α-β 模型里的 α。

这个结构与 u1-l2 讲过的 \( D = \alpha + n\beta + n\gamma \) 一一对应：C↔α、A↔跨卡 β、B↔本地 β＋γ。区别在于工程实现把「比例 n」吸收进了 A/B 的计算过程（见 4.2.2）。

#### 4.1.2 核心流程

1. 模板在 `CalcCostCoeff` 里用 `CalcCostCoeffParam`（含 rankSize、数据比例 n、组网类型 netType 等）调用 CostModelManager 的四个计算函数，得到一组 `(A, B, C)`。
2. 多级编排的执行器把各子模板的参数顺序拼接成一个 `std::vector<CostModelParam>`——**每一段对应流水线中的一步**。
3. 计价时（4.4.3）对每段分别算 \( (A/u+B)\cdot D+C \)，再按 `AlgNetMeta` 声明的聚合方式（求和/取最大）累加。

#### 4.1.3 源码精读

A/B/C 的定义与注释（注意所有权约定：`CostAlgoParams.param` 指向的内存由 CostModel 深拷贝持有）：

[selector/cost_model.h:L44-L61](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/cost_model.h#L44-L61)——定义 `CostModelParam{float A, B, C}`，注释写明 A 受 UB 带宽利用率影响、B 不受影响、C 是时延常数；`CostModel` 是「算法 → 参数数组」的列表。

传入模板的参数包，把「新增参数要改所有调用点」的问题收敛为「只改一个结构体」：

[selector/cost_model.h:L127-L137](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/cost_model.h#L127-L137)——`CalcCostCoeffParam`：rankSize（本段通信的 rank 数）、n（本段数据占总量的比例）、netType（MESH/CLOS 组网）、needLocalCopy、algName、portNum、comm 与 topoInfo。

#### 4.1.4 代码实践

1. **实践目标**：确认 A/B/C 在日志中的真实数值形态。
2. **操作步骤**：在具备 A5 类设备的环境上设置 `HCCL_USE_NEW_SELECTOR=1` 并打开 INFO 日志，运行一次 8 卡 AllReduce；在日志中检索 `[CalcAlgCost]`。
3. **需要观察的现象**：每个候选算法一行，形如 `algName=AicpuAllReduceSoleMeshOneShot segCount=1 dataSize=... cost=... params: [A=1.7e-11 B=1.5e-11 C=2.0e-06 util=0.71]`。
4. **预期结果**：segCount 与该算法流水线的段数一致（sole 为 1 段，sequence 为 4 段）；util 随 dataSize 变化。无设备时为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 A 要除以利用率 u 而 B 不用？
**答案**：A 建模的是走跨卡链路的传输，其有效带宽受片上 UB 流水线效率影响（小包时利用率低）；B 建模的本地拷贝/归约发生在片内，不存在该瓶颈，因此直接使用标定带宽。

**练习 2**：一个 sequence 编排的算法（RS 节点内 → RS 节点间 → AG 节点间 → AG 节点内）的 `CostAlgoParams.count` 应该是多少？
**答案**：4。执行器按流水线顺序把 4 个子模板各自的 (A,B,C) 拼进同一个 vector，每段对应一步（见 4.3.3 的 sequence 执行器源码）。

### 4.2 CostModelManager：带宽初始化与 A/B/C 参数计算

#### 4.2.1 概念说明

`CostModelManager` 是一个进程级单例（`Global()`），承担两类职责：

- **标定器**：持有一组按引擎/场景区分的带宽常数，对外提供四个纯函数把「数据比例 n × 组网」翻译成 A/B/C。
- **装配器**：`InitCostModel` 遍历 AllAlgos 中每个算法，经拓扑预过滤、执行器查询、`CalcCostCoeff` 采样，装配出一份 CostModel（缓存键为「通信域 × 引擎」，见 u8-l1）。

#### 4.2.2 核心流程

```
InitCostModel(comm, topoInfo, costModel):
  for alg in AllAlgos:                        # 注册宏双写登记的算法全集
    if not IsAlgoMatchTopo(alg, topoInfo):    # 按拓扑层级数预过滤
      continue
    exec = CollAlgExecRegistryV2 查询(alg.opType, alg.algName)
    if exec == nullptr: continue
    params = exec->CalcCostCoeff(...)          # 委托给模板（4.3）
    if params.empty(): continue                # 未标定 → 不参与比价
    AlgNetMetaRegistry.Register(alg.algName, exec->GetAlgNetMeta(topoInfo))
    costModel 追加 {algName, 深拷贝的 params}
```

四个计算函数的数学含义（Bw 为相应场景带宽，单位字节/秒）：

- Mesh 组网的 A（每个对端直连一份满量数据）：
  \[ A_{\text{mesh}} = \frac{n}{Bw_{\text{cross}}} \]
- CLOS 组网的 A（一份满量数据经交换芯片发给其余 rank−1 个对端，可摊到 portNum 个端口）：
  \[ A_{\text{clos}} = \frac{n \cdot (R-1)}{\text{portNum} \cdot Bw_{\text{cross}}} \]
- NHR（递归减半）的 A：数据量本身变为 \( n(R-1) \)：
  \[ A_{\text{nhr}} = \frac{n(R-1)}{Bw_{\text{cross}}} \quad\text{或}\quad \frac{n(R-1)}{\text{portNum}\cdot Bw_{\text{cross}}} \]
- 本地拷贝/归约的 B 与时延 C：
  \[ B = \frac{n}{Bw_{\text{local}}}, \qquad C = c_{\text{engine}} \cdot \text{taskNum} \]
  其中 \( c_{\text{AICPU}} = c_{\text{CCU}} = 2\,\mu s \)、\( c_{\text{AIV}} = 1\,\mu s \)（每个 task 的固定时延）。

#### 4.2.3 源码精读

带宽常数表——注意同一物理动作在不同引擎/场景下带宽差异巨大（如本地拷贝 AICPU 750GB/s vs CCU 200GB/s）：

[cost_model.cc:L73-L89](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/cost_model.cc#L73-L89)——`InitBandwidth` 写死 8 个带宽常数：`localCopyBw_=750GB/s`、`localReduceBw_=483GB/s`、`crossChipBw_=56GB/s`、CCU 场景 `ccuLocalCopyBw_=200GB/s`、CCU 环形场景 `47.6GiB/s` 等。这些是**离线标定值**，不随环境探测刷新。

Mesh/NHR 的 A 参数计算：

[cost_model.cc:L221-L255](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/cost_model.cc#L221-L255)——`CalcMeshParam`：MESH 时 `A = n / crossChipBw_`（每对链路直写一份），CLOS 时 `A = n·(R-1)/(portNum·crossChipBw_)`；`CalcNHRParams`：数据量放大为 `data = n·(R-1)` 再除以（端口数×）跨片带宽。

本地 B 与时延 C：

[cost_model.cc:L257-L295](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/cost_model.cc#L257-L295)——`CalcLocalCopyParams`/`CalcLocalReduceParams` 按 `EngineType` 选带宽（AICPU 默认 / CCU / CCU_CIR_MODE），`B = n / bw`；`CalcLatencyParams` 按 `taskNum × 引擎常数` 算 C（AICPU/CCU 每任务 2µs，AIV 1µs）。

CostModel 的装配主流程：

[cost_model.cc:L155-L219](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/cost_model.cc#L155-L219)——`InitCostModel`：先 `IsAlgoMatchTopo` 按 `topoLevelNums`（1/2/3 级）与 algName 中的编排关键字（Parallel/Sequence/Concur/…）预过滤；再查 `CollAlgExecRegistryV2` 拿执行器；`CalcCostCoeff` 返回空则告警跳过（未标定）；否则深拷贝参数并 `AlgNetMetaRegistry::Global()->Register(algName, exec->GetAlgNetMeta(topoInfo))`。

按拓扑折算每级 rank 数：

[cost_model.cc:L91-L113](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/cost_model.cc#L91-L113)——`CalcRankSizeByTopo`：level0 取 Layer0 首个实例（server 内 mesh 组）的 rank 数，level1 = 总 rank/level0（即 server 数），level2 再除一次。sequence/parallel 执行器用它给各段传正确的 rankSize。

#### 4.2.4 代码实践

1. **实践目标**：验证「未标定即跳过」的过滤行为。
2. **操作步骤**：阅读 `InitCostModel` 中 `params.empty()` 分支（[cost_model.cc:L187-L191](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/cost_model.cc#L187-L191)），再对照 `InsTempAllReduceMesh1DOneShot::CalcCostCoeff` 开头的 `rankSize > 8` 早退（4.3.3）。
3. **需要观察的现象**：在 8 卡以上拓扑的日志中检索 `CalcCostCoeff uncalibrated, skip algName=AicpuAllReduceSoleMeshOneShot`（HCCL_WARNING 级）。
4. **预期结果**：one-shot Mesh 算法不出现在该拓扑的 CostTable 中。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：同样传 `n=1/8`、`rankSize=8`，`CalcMeshParam` 与 `CalcNHRParams` 在 MESH 组网下算出的 A 差多少？
**答案**：Mesh 为 \( A = \frac{1/8}{56\text{GB/s}} \)，NHR 为 \( A = \frac{(1/8)\times 7}{56\text{GB/s}} \)，NHR 是 Mesh 的 7 倍——因为 NHR 每步要搬运 \( n(R-1) \) 的数据量，而 Mesh one-shot 每条直连链路只写一份 n。

**练习 2**：为什么 `InitCostModel` 里查不到执行器的算法只是 WARNING 跳过而不是报错？
**答案**：AllAlgos 与执行器注册表虽然由同一宏双写，但 algName 字符串契约两边各自维护，且不同设备/版本编译产物可能只注册子集；选择器应「尽力比价」而不是因个别算法缺席而整体失败，剩下 0 个候选才在后续 `SelectMinCost` 报 `HCCL_E_NOT_SUPPORT`。

### 4.3 模板/执行器的 CalcCostCoeff 挂钩与 AlgNetMetaRegistry

#### 4.3.1 概念说明

代价系数**不是写在选择器里的查表数据**，而是由每个算法自己「申报」：执行器基类 `InsCollAlgBase` 提供两个虚函数——

- `CalcCostCoeff(comm, topoInfo, algName)`：返回 `std::vector<CostModelParam>`，空 vector 表示「未标定」；
- `GetAlgNetMeta(topoInfo)`：返回 `AlgNetMeta`，描述各段的组网类型（MESH/CLOS，决定查哪张 UB 利用率表）、组内聚合方式（SUM 串行累加 / MAX 并行取最大）与分组大小。

模板层则提供**静态** `CalcCostCoeff(CalcCostCoeffParam)`——它是纯函数，不依赖运行期对象，执行器在编译期就以模板参数绑定了具体模板类型，可以直接以 `InsAlgTemplate::CalcCostCoeff(...)` 的形式调用。`AlgNetMetaRegistry` 是全局单例 map，`InitCostModel` 时登记，`CalcAlgCost` 计价时查询。

#### 4.3.2 核心流程

```
InitCostModel ──查注册表──▶ executor 实例 ──虚函数──▶ CalcCostCoeff
                                                 │ 按 CalcRankSizeByTopo 拆各段 rankSize/n/netType
                                                 ▼
                                       模板静态 CalcCostCoeff(CalcCostCoeffParam)
                                                 │ 调 CostModelManager 四个 Calc* 函数
                                                 ▼
                                       vector<CostModelParam>（每段一个）
executor ──虚函数──▶ GetAlgNetMeta ──▶ AlgNetMetaRegistry.Register(algName, meta)
```

#### 4.3.3 源码精读

基类默认空实现（= 未标定，会被 `InitCostModel` 跳过）：

[executor_v2_base.h:L37-L50](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/executor/executor_v2_base.h#L37-L50)——`InsCollAlgBase::CalcCostCoeff` 默认返回 `{}`，`GetAlgNetMeta` 默认返回空 `AlgNetMeta`；二者与纯虚的 `CalcAlgHierarchyInfo`/`CalcRes` 不同，不是每个执行器都必须实现。

sole 执行器的委托实现——把整份数据的比例 `1/rankSize` 交给模板：

[ins_v2_all_reduce_sole_executor.cc:L39-L64](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/executor/ins_v2_all_reduce_sole_executor.cc#L39-L64)——`InsV2AllReduceSoleExecutor::CalcCostCoeff` 直接 `return InsAlgTemplate::CalcCostCoeff(CalcCostCoeffParam{rankSize, 1.0f/rankSize, AlgNetType::MESH, true, algName})`；`GetAlgNetMeta` 申报单段 MESH、SUM 聚合、`groupSizes={1}`。该执行器经 `REGISTER_EXEC_V2` 绑定到 `AicpuAllReduceSoleMeshOneShot` 等算法（见 [ins_v2_all_reduce_sole_executor.cc:L298-L300](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/executor/ins_v2_all_reduce_sole_executor.cc#L298-L300)）。

模板侧的标定实现（本讲最核心的一段）：

[ins_temp_all_reduce_mesh_1D_one_shot.cc:L23-L52](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/template/aicpu/ins_temp_all_reduce_mesh_1D_one_shot.cc#L23-L52)——`InsTempAllReduceMesh1DOneShot::CalcCostCoeff`：① `rankSize > 8` 返回空（片上资源限制，不标定）；② `portNum`：CLOS 取 8、MESH 取 1；③ `n = param.n * rankSize`——executor 传的是 twoshot 视角的一片（1/R），one-shot 实际整份搬运，这里换算回 `n=1.0`；④ A 用 `CalcMeshParam`，B 拆两步：`needLocalCopy` 时 `CalcLocalCopyParams` 得 B1，`CalcLocalReduceParams` 得 B2，`B = B1 + (R-1)·B2`（1 次本地拷贝 + 对 R−1 个对端数据各做一次本地归约，与 `KernelRun` 中 `LocalCopy` + 循环 `LocalReduce` 的搬移结构一一对应）；⑤ C 用 `taskNum=1`。最终返回单段 `{A, B, C}`。

sequence 执行器的多段聚合：

[ins_v2_all_reduce_sequence_executor.cc:L22-L68](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/executor/ins_v2_all_reduce_sequence_executor.cc#L22-L68)——四段按「RS 节点内(MESH, level0) → RS 节点间(CLOS, level1) → AG 节点间(CLOS, level1) → AG 节点内(MESH, level0)」顺序拼接，每段 `n = 1/rankSize`；`GetAlgNetMeta` 申报 `netTypes={MESH, CLOS, MESH, CLOS}`、`intraGroupMode=SUM`、`groupSizes={1,1,1,1}`——即四个串行步骤的耗时直接相加。

注册表本身：

[cost_model.h:L139-L154](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/cost_model.h#L139-L154)——`AlgNetMeta`：`netTypes`（每段一个，顺序与参数数组一致）、`intraGroupMode`（SUM/MAX）、`groupSizes`（每组段数，空则兜底每组 1 个）；`AlgNetMetaRegistry` 是带互斥锁的 `map<string, AlgNetMeta>` 单例。

#### 4.3.4 代码实践

1. **实践目标**：为新模板补一份代价标定（源码阅读＋纸面推导，不改源码）。
2. **操作步骤**：打开 [ins_temp_all_reduce_mesh_1D_one_shot.cc:L23-L52](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/template/aicpu/ins_temp_all_reduce_mesh_1D_one_shot.cc#L23-L52)，对照其 `KernelRun`/`RunAllReduce`/`PostLocalReduce`（L98-L228）列出全部数据搬移动作；再打开 NHR 模板 `src/ops/all_reduce/template/aicpu/ins_temp_all_reduce_nhr.cc` 的 `CalcCostCoeff`，对比二者 B 项构成差异。
3. **需要观察的现象**：Mesh one-shot 的 B 是「1 次拷贝 + (R−1) 次归约」；NHR 因每轮先 PreCopy 到 cclBuff 再通信后 PostCopy，B 中拷贝次数与通信轮数 \( \lceil \log_2 R \rceil \) 相关。
4. **预期结果**：能口头解释「B 的每一项对应 KernelRun 里的哪一个原语调用」——这正是代价建模不脱离真实搬移结构的证据。

#### 4.3.5 小练习与答案

**练习 1**：executor 传 `n=1/rankSize`，模板里又乘回 `rankSize`，为什么不留直接传 1.0？
**答案**：executor 是编排层，它对 sole/sequence 各段统一按「每段处理一片」的视角切数据（sequence 各段就是 1/R）；one-shot 是特例（整份直写），换算逻辑收敛在模板内部，executor 无需感知 one-shot/two-shot 差异（源码注释「executor 层统一传 twoshot 需要的一片数据大小，template 层做特殊处理」）。

**练习 2**：`AlgNetMeta.intraGroupMode` 何时用 MAX、何时用 SUM？
**答案**：组内各段**并行**执行（如 concurrent/parallel 编排里同时跑的两路）时用 MAX——总耗时取决于最慢一路；组内各段**串行**执行（如 sequence 的四步）时用 SUM。编排语义由此被精确编码进代价元数据（u3-l4 讲过）。

### 4.4 CostTableManager：生成、规则过滤与代价计算

#### 4.4.1 概念说明

CostModel 是「通信域 × 引擎」粒度的缓存（A/B/C 与数据量无关，建一次可复用）；而 CostTable 是**本次调用**的快照：把 CostModel 里的候选按算子规则过滤、按当前 dataSize 计价后的一张 `(algName, cost)` 表。`CostTableManager` 同样是单例，`AlgoCost` 直接复用 Tuner 插件的 `hcclTunerAlgoEntry_t`（u8-l4 主题），使插件能原位改写 cost。

规则（`AlgFilterRule`）分两类：

- **必选（isMustSelect=true）**：条件命中后，CostTable **只保留**规则列出的算法（如保序模式强制选 `AicpuAllReduceStrictOrderedMesh`）；
- **必不选（isMustSelect=false）**：条件命中后，把规则列出的算法**排除**出 CostTable（如 INT8 排除部分 CCU 算法）。

#### 4.4.2 核心流程

```
SelectorEngine::Run (u8-l1)
  └─ CostTableGen(cm, ct, topoInfo, opParam)
       └─ FilterCMByConfig: 按 opType 分发
            ├─ FilterAllReduce  → BuildAllReduceRules()  → FilterByRules
            ├─ FilterAllGather  → BuildAllGatherRules()  → FilterByRules
            └─ FilterReduceScatter → BuildReduceScatterRules() → FilterByRules
  （Tuner 可改 cost，u8-l4）
  └─ SelectMinCost(ct): 跳过 algName 为空或 cost<0 的表项，
     取 cost 最小者回写 algName 与 param.opExecuteConfig
```

`FilterByRules` 内部顺序：先收集命中的必选集合 → 非空则只对必选算法计价并直接返回；否则收集必不选集合 → 逐个算法检查「opType 名字匹配 + 不在必不选集合」→ 对幸存者调 `CalcAlgCost` 填表。**被规则排除的算法根本不会出现在 CostTable 里**（`continue` 跳过，见源码 L602-L611）；而表中 `cost < 0` 的表项是给 Tuner 插件预留的「软过滤」标记，`SelectMinCost` 把它显示为 `filtered` 并跳过（[selector_engine.cc:L254-L292](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_engine.cc#L254-L292)）。此外 HCCL_ALGO 的排除发生在更早的 CostModel 层（`UpdateCostModelWithAlgo` 把不匹配条目的 count 清零，u8-l3 展开）。

#### 4.4.3 源码精读

规则结构与单条 AllReduce 规则：

[cost_table.cc:L39-L76](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/cost_table.cc#L39-L76)——`AlgFilterRule{name, condition(opParam, topoInfo), isMustSelect, algos}`；第一条规则 `int8_skip_ccu_sched_seq`（L71-L76）：条件是 `op.DataDes.dataType == HCCL_DATA_TYPE_INT8`，命中则必不选 `CcuSchedAllReduceSequenceMeshMesh`/`CcuSchedAllReduceSoleMesh`。注释「必不选：int8 排除 ccu sched 的 SequenceMesh1D/Mesh1DMem2Mem」。

必选规则的例子（保序模式）：

[cost_table.cc:L101-L116](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/cost_table.cc#L101-L116)——`order_preserved_group`（rankSize>32 强制 `AllReduceOrderPreservedGroup`）与 `order_preserved`（rankSize≤32 强制 `AicpuAllReduceStrictOrderedMesh`），`isMustSelect=true`。

过滤引擎主体：

[cost_table.cc:L533-L620](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/cost_table.cc#L533-L620)——`FilterByRules`：L549 由 `count × DATATYPE_SIZE_TABLE[dataType]` 算出本次 dataSize；L553-L577 必选短路；L579-L611 收集必不选集合并逐算法过滤（含按 opType 名字预过滤与 AIV_ONLY 场景的告警）；L612-L616 对每个幸存算法 `CalcAlgCost` 填表并打 INFO 日志。

计价公式与 AICPU 下限：

[cost_table.cc:L640-L696](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/cost_table.cc#L640-L696)——`CalcAlgCost`：查 `AlgNetMeta`（组网/分组/聚合方式）与引擎；按 `groupSizes` 分组遍历每段，`QueryUbUtil(netType, dataSize, engine, util)` 得利用率后 `segCost = (A/util + B)·dataSize + C`，组内按 SUM 或 MAX 聚合、组间求和；L672-L674：AICPU_TS 引擎且非 AllGather 时 `cost = max(cost, 0.0005f)`——给 AICPU 路径设 0.5ms 起步价，避免其在小数据场景「纸面太便宜」。

UB 利用率查表：

[cost_table.cc:L709-L750](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/cost_table.cc#L709-L750)——`closUbUtilTable_`/`meshUbUtilTable_` 两张静态表：CLOS 组网 128KB 时利用率仅 0.0276，256MB 升到 0.7644；Mesh 组网 1MB 即有 0.7135、上限约 0.8494。`QueryUbUtil` 用 `lower_bound` 查首个 `upperBound >= dataSize` 的档位，越界取尾档；AIV 引擎额外乘 `0.65/0.85` 折扣；AllGather + CLOS 小于 1MB 时按 1MB 档取值。

SelectMinCost（消费方）：

[selector_engine.cc:L245-L314](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_engine.cc#L245-L314)——逐行打印 `| idx | algName | engine | cost | status |` 表格；`algName == nullptr || cost < 0.0f` 视为 filtered 跳过；取最小 cost（相同 cost 取首个并 WARNING）；最后 `algName = ct.costs[minIdx].algName` 并经 `GetEngineByAlgName` 回写 `param.opExecuteConfig`。全表无效则返回 `HCCL_E_NOT_SUPPORT`。

#### 4.4.4 代码实践：8 卡 1MB AllReduce 的 A/B/C 手工推导

1. **实践目标**：把 4.2/4.3 的公式串起来，算出 `AicpuAllReduceSoleMeshOneShot` 的纸面耗时。
2. **操作步骤**（设 rankSize R=8、dataSize D=1MB=1048576B、单机 8 卡 MESH 组网、AICPU 引擎）：
   - sole executor 传 `n = 1/8`，模板内换算 `n = (1/8)×8 = 1.0`；
   - A：`CalcMeshParam(1.0, MESH, portNum=1, 8)` → \( A = 1 / 56\times 10^9 \approx 1.79\times 10^{-11} \) s/B；
   - B：\( B_1 = 1/750\times 10^9 \approx 1.33\times 10^{-12} \)，\( B_2 = 1/483\times 10^9 \approx 2.07\times 10^{-12} \)，\( B = B_1 + 7B_2 \approx 1.58\times 10^{-11} \) s/B；
   - C：`CalcLatencyParams(taskNum=1, AICPU)` → \( 2\,\mu s \)；
   - 利用率：MESH 表 1MB 档 → u = 0.7135；
   - 计价：\( T = (1.79\times10^{-11}/0.7135 + 1.58\times10^{-11})\times 1048576 + 2\times10^{-6} \approx 4.5\times10^{-5} \) s；再套 AICPU 下限 `max(T, 0.0005)` → 最终 cost = 0.0005 s。
3. **需要观察的现象**：日志中 `[CalcAlgCost] algName=AicpuAllReduceSoleMeshOneShot ... cost=0.0005`，params 里的 A/B/C 数量级与手算一致。
4. **预期结果**：小数据量下 AICPU 各算法大多顶在 0.5ms 下限，此时选择实际由 C 项与规则过滤决定——这解释了为什么小包场景 AIV/CCU（无此下限）更易胜出。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：必选规则与必不选规则同时命中时，以谁为准？
**答案**：以必选为准。`FilterByRules` 先检查 mustSelect 集合，非空则只对必选算法计价并**直接 return**（L559-L577），必不选规则根本不会被执行到。

**练习 2**：`FilterAllReduce` 中 `two_die_regular_only` 规则在什么拓扑下不排除 2Die 算法？
**答案**：仅当 `topo->level0MeshType == Level0MeshType::TWO_DIE_REGULAR` 时条件为假（不排除）；其余拓扑下 2Die 算法被排除出 CostTable（[cost_table.cc:L133-L139](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/cost_table.cc#L133-L139)）。

**练习 3**：为什么 AICPU_TS 要设 `max(cost, 0.0005)` 下限而 CCU/AIV 不设？
**答案**：AICPU 路径有 kernel 加载、Task 描述符组装与 TS 调度等未被 A/B/C 充分刻画的固定开销，小数据时纯公式会显著低估；CCU/AIV 路径更短、C 项标定更接近真实，无需人为托底。

## 5. 综合实践

**任务：给一个假想的新 AllReduce 模板「补齐代价申报」并推演它能否被选中。**

1. 阅读注册链：`REGISTER_EXEC_V2(HCCL_CMD_ALLREDUCE, AicpuAllReduceSoleMeshOneShot, InsV2AllReduceSoleExecutor, TopoMatch1D, InsTempAllReduceMesh1DOneShot)`（[ins_v2_all_reduce_sole_executor.cc:L298-L300](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/executor/ins_v2_all_reduce_sole_executor.cc#L298-L300)），画出「注册宏双写 AllAlgos → InitCostModel 查表采样 → AlgNetMetaRegistry 登记 → FilterByRules 计价 → SelectMinCost 选中」的端到端数据流图，标注每一步产出的数据结构（AlgElement / CostModel / AlgNetMeta / CostTable / algName）。
2. 假设新增模板 `InsTempAllReduceMesh1DThreeShot`（三段搬移：拷贝→写对端→归约两次），写出：(a) 它的静态 `CalcCostCoeff` 中 B 的构成式（参照 4.3.3 的 B1/B2 模式）；(b) sole 执行器是否需要改（不需要——委托逻辑与模板无关，说明为什么）；(c) 若它只支持 rankSize≤4，`InitCostModel` 会如何处理 8 卡拓扑（返回空 → `uncalibrated, skip`）。
3. 为它补一条 `AlgFilterRule`（纸面设计）：仿照 `aiv_data_size_limit`（[cost_table.cc:L470-L485](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/cost_table.cc#L470-L485)）写一个「数据量超过某阈值时必不选该算法」的条件 lambda，并说明它应加入 `BuildAllReduceRules` 还是 `BuildReduceScatterRules`。
4. 验证方式：若可在 A5 环境运行，设 `HCCL_USE_NEW_SELECTOR=1` 后从日志中抄下 `SelectMinCost` 的表格，检查你推演的 cost 排序与实际一致；无设备则标注「待本地验证」。

## 6. 本讲小结

- 耗时模型为 \( T = (A/u + B)\cdot D + C \)：A 是跨卡传输斜率（除以 UB 利用率 u）、B 是本地拷贝+归约斜率、C 是时延常数；A/B/C 由 `CostModelManager` 的标定带宽与四个 `Calc*` 函数计算。
- `InitCostModel` 遍历 AllAlgos：拓扑预过滤 → 查执行器 → `CalcCostCoeff` 采样（空=未标定即跳过）→ `AlgNetMetaRegistry` 登记组网元数据，产出按「通信域 × 引擎」缓存的 CostModel。
- 代价由算法自己申报：执行器虚函数 `CalcCostCoeff`/`GetAlgNetMeta`，模板静态 `CalcCostCoeff(CalcCostCoeffParam)`；多级编排把各段参数拼接成 vector，`AlgNetMeta` 的 SUM/MAX 编码串行/并行语义。
- CostTable 是本次调用的快照：`FilterByRules` 先必选短路、再必不选排除（被排除者不入表），幸存者按 `CalcAlgCost` 计价；AICPU 非 AllGather 有 0.5ms 下限。
- UB 利用率是两张按组网（CLOS/Mesh）分档的静态查表，小数据量 CLOS 利用率可低至 0.0276，AIV 额外乘 0.65/0.85 折扣。
- `SelectMinCost` 跳过 `cost<0`（Tuner 软过滤标记）与空名表项，取最小 cost 回写 algName 与引擎；全无效则 `HCCL_E_NOT_SUPPORT`。

## 7. 下一步学习建议

- 下一讲 **u8-l3 算法三维命名与 HCCL_ALGO 解析**：HCCL_ALGO 的排除发生在 CostModel 层（`UpdateCostModelWithAlgo`），与本章的规则过滤如何配合，是理解「用户配置如何收窄候选集」的钥匙。
- 之后 **u8-l4 Tuner 插件框架**：`AlgoCost` 就是 `hcclTunerAlgoEntry_t`，插件改 cost 与 `cost<0` 软过滤在本讲的 `SelectorEngine::Run` step 2.2 已埋下伏笔。
- 源码延伸阅读：`src/ops/all_reduce/executor/ins_v2_all_reduce_parallel_executor.cc`（MAX 聚合的活例子）、`test/ut/common/alg_parse/update_cost_model_test.cc`（用单测验证 CostModel 过滤规则）。

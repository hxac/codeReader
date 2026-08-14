# 通信引擎选择与快速路径

## 1. 本讲目标

上一讲（u2-l3）我们把 `HcclAllReduce` 的 API 入参装配进了贯穿全链路的 `OpParam`，但那时 `param.engine` 仍然是 `RESERVED`——本讲就回答这个问题：**一次 AllReduce 到底该由哪个通信引擎（AICPU / AIV / CCU）来跑？在进入算法选择器 `Selector()` 之前，还有哪些「抄近道」的快速路径可以提前结束？**

学完本讲你应当能够：

- 说清 `HcclGetOpExpansionMode` 如何通过「决定模式 → 应用模式」两步，把 `param.engine` 从 `RESERVED` 设成 `AICPU_TS / AIV / CCU`。
- 画出 `AllReduceOutPlaceCommon` 从 `FillAllReduceOpParam` 到 `Selector()` 之间的**全部分支及其优先级**（9.0 CCU 老流程、CCU FastLaunch、AIV Cache、单卡、主路径）。
- 识别 CCU FastLaunch 与 AIV Cache Replay 两条快速路径各自的**触发条件**与**收益**（跳过 Selector 与资源计算）。
- 理解 `CommEngine` 枚举的取值，以及它为何定义在 HCOMM 仓而非本仓。

## 2. 前置知识

本讲默认你已经掌握以下前置讲义的内容，这里只做最简回顾，不再展开：

- **u1-l2 通信引擎**：`CommEngine = Thread（执行上下文）+ 线程调度器`，主流引擎是 `AICPU_TS`（不占计算核、大数据量）、`AIV`（低延迟但占 Vector 核、小数据量）、`CCU`（IO Die 硬化加速单元）。引擎与算法（Ring/Mesh/NHR…）是**正交的两个维度**：本讲只决定「用哪个引擎」，具体「用哪个算法」要等到下一讲的 `Selector`。
- **u2-l2 入口与兼容分发**：`HcclAllReduce` 设了两道闸门（版本闸门 `GetHcommVersion() < 9.0.0`、设备闸门 `IsOutPlaceDevice`，仅 `DEV_TYPE_950/960` 为真），通过后才进入 `AllReduceOutPlaceCommon`；`shouldGoOutPlace(deviceType)` 就是「设备是否为 950/960」的判定。
- **u2-l3 OpParam**：`OpParam` 是贯穿「入口→Selector→Executor→Template」的中央参数容器；`FillAllReduceOpParam` 装配好 `inputSize/opType/deviceType` 等字段，但**故意不设 `engine`**，把这件事留给本讲的 `HcclGetOpExpansionMode`。
- **u1-l1 两仓解耦**：本仓 `cann/hccl` 与 `cann/hcomm` 经 dlsym 动态加载 `libhcomm.so` 解耦。`CommEngine` 这类「跨仓类型」定义在 HCOMM 仓的头文件里，本仓通过桩库/`comm_engine_utils.h` 间接引用——这解释了为什么本讲引用的 `CommEngine` 枚举定义不在本仓 `.h` 中。

> 一个贯穿本讲的直觉：**引擎选择是一条「从上到下、命中即返回」的判定链**。HCCL 在真正去 Selector 选算法之前，会先用一连串 `if` 探测「能不能跳过昂贵的资源计算、直接复用之前算好的结果」——这就是所谓「快速路径（Fast Path）」。理解这条链的顺序，是本讲的核心。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| `src/ops/all_reduce/all_reduce_op.cc` | AllReduce 算子入口与执行编排 | `AllReduceOutPlaceCommon` 主流程（本讲主轴） |
| `src/ops/op_common/op_common.cc` | op_common 公共实现 | `HcclGetOpExpansionMode` / `DecideHcclOpExpansionMode` / `ApplyOpExpansionMode`、`ShouldGoCcuFastLaunch`、`HcclAivCacheCheckAndReplay`、`IsAivCacheSupported`、`HcclExecOpCcuFastLaunch` |
| `src/ops/op_common/op_common.h` | op_common 函数声明 | 上述函数的声明位置 |
| `src/ops/op_common/inc/alg_param.h` | OpParam 与相关结构 | `OpParam.engine`、`OpExecuteConfig`、`CcuFastLaunchCtx` |
| `src/common/comm_engine_utils.h` | 引擎字符串映射 | `CommEngine` 各取值的字符串化（本仓内的权威映射） |
| `src/common/hcomm_dlsym/hccl_host_comm_dl.h` | HCOMM dlsym 封装 | `HcclOpExpansionMode` 枚举桩（展开模式） |
| `src/common/alg_type.h` | 算法类型体系 | `HcclAlgoType` 等（算法维度，与引擎正交，仅作背景） |

> 说明：`CommEngine` 枚举**本身**定义在 HCOMM 仓头文件 `hccl_res.h` 中（跨仓），本仓通过 `comm_engine_utils.h` 的字符串映射表把它「透传」出来；`HcclOpExpansionMode` 在 `< 9.1.0_beta.1` 时由本仓桩 `hccl_host_comm_dl.h` 提供，更高版本则来自真实 HCOMM 头。这正是两仓解耦在类型层面的体现。

## 4. 核心概念与源码讲解

### 4.1 CommEngine 枚举与 HcclGetOpExpansionMode

#### 4.1.1 概念说明

`OpParam` 里有一个字段 `engine`，类型是 `CommEngine`，它决定这次算子由哪类硬件执行单元来跑。引擎与算法是两个独立维度——你可以用「AICPU 引擎跑 Ring 算法」，也可以用「CCU 引擎跑 Mesh 算法」。

关键问题：`engine` 是怎么被赋值的？答案是 `HcclGetOpExpansionMode`——它把一个更高层的「展开模式（OpExpansionMode）」翻译成具体的 `CommEngine`。所谓「展开模式」是 HCOMM 侧的配置概念，描述「算子以何种方式展开到硬件」，例如 `AI_CPU`、`AIV`、`CCU_MS`、`CCU_SCHED` 等；HCCL 读到它后再映射成自己关心的 `param.engine` 和 `param.opExecuteConfig`。

#### 4.1.2 核心流程

`HcclGetOpExpansionMode` 是典型的「两步走」：

```text
HcclGetOpExpansionMode(comm, param)
  │
  ├─ 第一步 DecideHcclOpExpansionMode(comm, finalMode)
  │     决定用哪种「展开模式」：
  │       · 优先读 HCOMM 配置 HcclConfigGetInfo(OP_EXPANSION_MODE)
  │       · 对非 OutPlace 设备（非 950/960）或未拿到配置时，
  │         按环境变量优先级判定：AicpuUnfold > AivOnlyMode > AivMode > CcuMSMode > CcuSchedMode
  │
  └─ 第二步 ApplyOpExpansionMode(param, finalMode)
        switch(finalMode) 把模式映射成：
          param.opExecuteConfig (OpExecuteConfig 枚举)
          param.engine           (CommEngine 枚举)
        并按需预热：AICPU 走 LoadAICPUKernel()，AIV 走 RegisterKernel()，CCU 不需要
```

`CommEngine` 的取值（来自本仓 `comm_engine_utils.h` 的字符串映射）：

| CommEngine 值 | 字符串 | 含义 |
|---------------|--------|------|
| `COMM_ENGINE_RESERVED` | RESERVED | 未设定（初始值） |
| `COMM_ENGINE_CPU` / `COMM_ENGINE_CPU_TS` | CPU / CPU_TS | Host CPU 相关 |
| `COMM_ENGINE_AICPU` / `COMM_ENGINE_AICPU_TS` | AICPU / AICPU_TS | AI CPU，不占计算核 |
| `COMM_ENGINE_AIV` | AIV | Vector Core，低延迟 |
| `COMM_ENGINE_CCU` | CCU | IO Die 硬化通信单元 |

#### 4.1.3 源码精读

`OpParam.engine` 的初始值就是 `RESERVED`，这是「待设定」的占位：

- [src/ops/op_common/inc/alg_param.h:587-587](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/inc/alg_param.h#L587-L587) —— `CommEngine engine = CommEngine::COMM_ENGINE_RESERVED;`，`OpParam` 构造时引擎未定。

本仓内 `CommEngine` 各取值的字符串映射（日志里打印引擎名时用）：

- [src/common/comm_engine_utils.h:28-39](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/comm_engine_utils.h#L28-L39) —— `GetCommEngineStatusStrMap()` 给出 RESERVED/CPU/CPU_TS/AICPU/AICPU_TS/AIV/CCU 的字符串表。

`HcclOpExpansionMode` 的桩枚举（仅 `< 9.1.0_beta.1` 编译；更高版本取自真实 HCOMM 头）：

- [src/common/hcomm_dlsym/hccl_host_comm_dl.h:19-28](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/hcomm_dlsym/hccl_host_comm_dl.h#L19-L28) —— 展开模式枚举：`AI_CPU=0 / AIV=1 / HOST=2 / HOST_TS=3 / CCU_MS=4 / CCU_SCHED=5 / AIV_ONLY=6`。

两步走主体：

- [src/ops/op_common/op_common.cc:3148-3166](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L3148-L3166) —— `HcclGetOpExpansionMode`：先 `DecideHcclOpExpansionMode` 决定 `finalMode`，存入 `param.commOpExpansionMode`，再 `ApplyOpExpansionMode` 应用到 `param`。

决定模式时，**OutPlace 设备（950/960）与非 OutPlace 设备走不同路径**：

- [src/ops/op_common/op_common.cc:3168-3209](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L3168-L3209) —— `DecideHcclOpExpansionMode`：优先 `dlHcclConfigGetInfo` 读 HCOMM 配置；若 `!shouldGoOutPlace(deviceType)` 或未拿到配置，则按环境变量 `AicpuUnfold > AivOnlyMode > AivMode > CcuMSMode > CcuSchedMode` 的优先级覆盖（环境变量与配置冲突时，环境变量优先）。

把模式映射成 `engine` 的 `switch`：

- [src/ops/op_common/op_common.cc:3211-3251](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L3211-L3251) —— `ApplyOpExpansionMode`：`AI_CPU→AICPU_TS+LoadAICPUKernel`、`AIV/AIV_ONLY→AIV+RegisterKernel`、`CCU_MS/CCU_SCHED→CCU`、`default→回退 AICPU_TS`。注意 `CCU_MS` 与 `CCU_SCHED` 两种展开模式都映射到同一个 `COMM_ENGINE_CCU`，区别只留在 `param.opExecuteConfig` 里。

`OpExecuteConfig` 的取值（与 `CommEngine` 对偶的另一套内部配置枚举）：

- [src/ops/op_common/inc/alg_param.h:125-135](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/inc/alg_param.h#L125-L135) —— `DEFAULT/HOSTCPU_TS/AICPU_TS/AIV/AIV_ONLY/CCU_MS/CCU_SCHED/AICPU/HOSTCPU/CCU_FAIL`。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：把「展开模式 → 引擎」的映射表亲手抄一遍，建立直觉。
2. **操作步骤**：打开 [op_common.cc:3211-3251](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L3211-L3251) 的 `ApplyOpExpansionMode`，逐个 `case` 记录三列：`HcclOpExpansionMode`、得到的 `param.engine`、得到的 `param.opExecuteConfig`。
3. **需要观察的现象**：注意哪些 `case` 会额外调用 `LoadAICPUKernel()` 或 `RegisterKernel()`，哪些不会（CCU 两个分支没有任何预热调用）。
4. **预期结果**：你应当得到一张 6 行的映射表，其中 `AIV` 与 `AIV_ONLY` 映射到**同一个** `COMM_ENGINE_CCU` 之外的同一个 `COMM_ENGINE_AIV`，区别仅在 `opExecuteConfig`（`AIV` vs `AIV_ONLY`）。
5. **待本地验证**：运行期实际命中哪个分支取决于设备型号与环境变量，可在日志里搜 `[ApplyOpExpansionMode]`（`HCCL_DEBUG` 级别）确认——是否能看到该日志取决于日志级别配置，待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`CCU_MS` 和 `CCU_SCHED` 都映射成 `COMM_ENGINE_CCU`，那它们的区别保存在哪里？后续代码靠什么区分这两种 CCU 子模式？

> **参考答案**：区别保存在 `param.opExecuteConfig`（`CCU_MS` vs `CCU_SCHED`）。后续资源计算（如 `HcclGetCcuKernel` 的注释「CCU_MS 模式 LOOP/CCU_BUF 默认值更大」）会读取 `opExecuteConfig` 来调整 CCU 的资源阈值。

**练习 2**：如果 `DecideHcclOpExpansionMode` 既没拿到 HCOMM 配置、也没设置任何环境变量，`finalMode` 会是什么？`ApplyOpExpansionMode` 会怎么处理？

> **参考答案**：`finalMode` 会停留在 `HCCL_OP_EXPANSION_MODE_INVALID`（或对 OutPlace 设备保留 `dlHcclConfigGetInfo` 的返回值），落到 `ApplyOpExpansionMode` 的 `default` 分支，打印一条 `HCCL_WARNING` 并**回退到 `AICPU_TS`**，同时调用 `LoadAICPUKernel()` 兜底。

---

### 4.2 AllReduceOutPlaceCommon 主流程与单卡分支

#### 4.2.1 概念说明

`AllReduceOutPlaceCommon` 是单算子 AllReduce 的「执行总调度」。它做三件事：装配 OpParam、决定引擎、然后**按固定优先级**逐个探测快速路径，命中任一就提前 `return`；所有快速路径都没命中，才走「正常路径」`Selector() → HcclExecOp()`。

设计动机：`Selector()` 之后要做拓扑匹配、资源计算、channel 申请等重活，代价高。如果一个算子调用满足某些条件（引擎是 CCU 且已有缓存的指令序列；或引擎是 AIV 且已有缓存的指令；或通信域只有一张卡），就可以**完全跳过** Selector 与资源计算，直接回放或就地处理。

#### 4.2.2 核心流程

下面是 `AllReduceOutPlaceCommon` 的完整判定链（**从上到下、命中即返回**）：

```text
AllReduceOutPlaceCommon(sendBuf, recvBuf, count, dataType, op, comm, stream, opMode, resPack, param)
  │
  ├─ 0. FillAllReduceOpParam(...)              # 装配 OpParam（inputSize/opType/deviceType；engine 仍 RESERVED）
  ├─ 1. HcclGetOpExpansionMode(comm, param)    # 设定 param.engine（见 4.1）
  │
  ├─ 【分支 A · 9.0 CCU 老流程】
  │    if (opMode==OPBASE && GetHcommVersion()==9.0.0 && engine==CCU)
  │        return HcclAllReduceInner(...)         # 9.0.0 版本的 CCU 透明回退到老流程
  │
  ├─ 【分支 B · CCU FastLaunch】
  │    if (opMode==OPBASE && ShouldGoCcuFastLaunch(comm, param, &ctx))
  │        return HcclExecOpCcuFastLaunch(...)    # 复用已缓存的 CCU 指令序列
  │
  ├─ 【分支 C · AIV Cache】
  │    if (engine==AIV)
  │        HcclAivCacheCheckAndReplay(comm, param, aivCacheHit)
  │        if (aivCacheHit) return SUCCESS         # 回放已缓存的 AIV 指令
  │
  ├─ 【分支 D · 单卡】
  │    HcclGetRankSize(comm, &userRankSize)
  │    if (userRankSize==1)
  │        SingleRankProc(comm, param); return     # 通信域只有自己，无需通信
  │
  └─ 【主路径】
       Selector(comm, param, topoInfo, algName)    # 正式选算法（下一讲 u3-l2）
       HcclExecOp(comm, param, topoInfo, algName, resPack)
```

**优先级**：A > B > C > D > 主路径。A、B 都要求 `engine==CCU`（B 的 CCU 判定藏在 `ShouldGoCcuFastLaunch` 内部）；C 要求 `engine==AIV`；D 与引擎无关，只看 rank 数。

#### 4.2.3 源码精读

主流程整体（本讲最重要的一段代码）：

- [src/ops/all_reduce/all_reduce_op.cc:189-234](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/all_reduce_op.cc#L189-L234) —— `AllReduceOutPlaceCommon`：装配 → 选引擎 → 四条分支 → 主路径。下面只摘关键几行。

装配与引擎设定（衔接 u2-l3 的 `FillAllReduceOpParam`）：

```cpp
CHK_RET(FillAllReduceOpParam(sendBuf, recvBuf, count, dataType, op, comm, stream, opMode, param));
CHK_RET(HcclGetOpExpansionMode(comm, param));
```

- [src/ops/all_reduce/all_reduce_op.cc:195-197](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/all_reduce_op.cc#L195-L197) —— 先装配 OpParam，再由 `HcclGetOpExpansionMode` 设定 `param.engine`。

分支 A（9.0.0 CCU 老流程）：

```cpp
// 9.0.0 ccu模式走老流程
if (opMode == OpMode::OPBASE && GetHcommVersion() == CANN_VERSION(9, 0, 0)
    && param.engine == CommEngine::COMM_ENGINE_CCU) {
    return HcclAllReduceInner(sendBuf, recvBuf, count, dataType, op, comm, stream);
}
```

- [src/ops/all_reduce/all_reduce_op.cc:200-203](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/all_reduce_op.cc#L200-L203) —— 仅当 `OPBASE`（单算子）+ HCOMM 版本恰好 9.0.0 + CCU 引擎三条件同时成立时，透明回退老流程。

分支 B（CCU FastLaunch）：

- [src/ops/all_reduce/all_reduce_op.cc:205-208](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/all_reduce_op.cc#L205-L208) —— `ShouldGoCcuFastLaunch` 返回 `true` 时，直接 `HcclExecOpCcuFastLaunch`，跳过 Selector。

分支 C（AIV Cache）：

- [src/ops/all_reduce/all_reduce_op.cc:210-216](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/all_reduce_op.cc#L210-L216) —— 仅 `engine==AIV` 时尝试缓存命中；`aivCacheHit` 为真则提前返回。

分支 D（单卡）与主路径：

- [src/ops/all_reduce/all_reduce_op.cc:218-231](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/all_reduce_op.cc#L218-L231) —— `rankSize==1` 走 `SingleRankProc`（无需通信）；否则进入 `Selector() → HcclExecOp()` 正常链路（本讲止步于此，细节交给 u3）。

注意 `AllReduceOutPlace`（单算子）传入 `OpMode::OPBASE`，而图模式 `AllReduceOutPlaceGraphMode` 传入 `OpMode::OFFLOAD`：

- [src/ops/all_reduce/all_reduce_op.cc:270-279](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/all_reduce_op.cc#L270-L279) —— 单算子入口 `AllReduceOutPlace` 用 `OPBASE`；这意味着分支 A、B（都要求 `OPBASE`）**只在单算子模式下生效**，图模式（`OFFLOAD`）不走这两条快速路径。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：在源码里给每条分支打上标签，确认优先级顺序与触发条件。
2. **操作步骤**：打开 [all_reduce_op.cc:189-234](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/all_reduce_op.cc#L189-L234)，对每个 `if`/`return` 标注「分支 A/B/C/D/主路径」及其三个判定条件（`opMode`、`engine`、其他）。
3. **需要观察的现象**：分支 B（`ShouldGoCcuFastLaunch`）的 `if` 里**没有**显式写 `engine==CCU`——确认这个判定是否被藏进了函数内部。
4. **预期结果**：四条快速分支按 A→B→C→D 顺序排列，前三条都和引擎强相关，最后一条（单卡）与引擎无关。
5. **待本地验证**：无。

#### 4.2.5 小练习与答案

**练习 1**：为什么分支 A、B 都只在 `OPBASE` 下生效？图模式（`OFFLOAD`）为什么不走 CCU FastLaunch？

> **参考答案**：图模式由 Graph Engine 预分配 streams / scratch，HCCL 只负责执行，资源来自外部（见 u7-l2）；而 FastLaunch 依赖 HCCL 自己缓存并复用 CCU 指令序列与线程，与图模式的「外部管资源」模型冲突，故 `ShouldGoCcuFastLaunch` 内部直接对 `OFFLOAD` 返回 `false`，分支 A 也显式要求 `OPBASE`。

**练习 2**：如果一次调用 `engine==AIV` 且 AIV Cache 未命中（`aivCacheHit==false`），流程会走到哪里？

> **参考答案**：分支 C 不提前返回，继续向下；若 `rankSize>1` 则走到主路径 `Selector()→HcclExecOp()`，由后续 AIV executor 正常执行（此时 selector 会产出 AIV 类的 algName）。也就是说 AIV Cache 只是「可选加速」，未命中不影响正确性。

---

### 4.3 CCU FastLaunch 快速路径（ShouldGoCcuFastLaunch）

#### 4.3.1 概念说明

CCU 引擎每次执行都要下发一长串「Mission 指令」给 IO Die。如果同一个算子（相同的 algName、相同的数据布局）被反复调用，每次都重新计算资源、重新组装指令序列就很浪费。**CCU FastLaunch** 的思路是：第一次执行后，把算好的指令序列、线程句柄、kernel 数量等打包成一个 `CcuFastLaunchCtx` 缓存起来；下次再遇到「同一把钥匙」的调用，直接取出上下文、绑定主流、调用 executor 的 `FastLaunch` 回放，跳过 Selector 与全部资源计算。

#### 4.3.2 核心流程

```text
ShouldGoCcuFastLaunch(comm, param, &ctx)         # 判定 + 取缓存
  │  前置（任一不满足即 return false）：
  │    · opMode != OFFLOAD              （图模式不走）
  │    · param.engine == CCU            （必须是 CCU）
  │    · SetOpParamFastLaunchTag 成功   （生成 fastLaunchTag 作为缓存键）
  │  查缓存：HcclEngineCtxGet(comm, fastLaunchTag, CCU, &ptr, &size)
  │    命中 → *ctx = 指针；return true
  │    未命中 → return false
  ▼ （命中时主流程调用）
HcclExecOpCcuFastLaunch(comm, param, ctx)
  ├─ 用 algName 从注册表取 executor：CollAlgExecRegistryV2::GetAlgExec(opType, algName)
  ├─ 取通信域 CCL buffer 作为 scratch
  ├─ 绑定主流：HcclThreadAcquireWithStream(... notifyNumOnMainThread ...)
  ├─ （aclgraph 捕获时）CaptureSlaveStreams
  └─ executor->FastLaunch(param, ctx)   # 直接回放缓存的 CCU 指令
```

`CcuFastLaunchCtx` 是一个**变长结构**：头部是定长字段（`algName`、`notifyNumOnMainThread`、`threadNum`、`ccuKernelNum[]`），其后紧跟着一块「`ThreadHandle` 数组 + `CcuKernelSubmitInfo` 数组」的内存，通过指针运算访问——这样一次 `HcclEngineCtxGet` 就能拿到完整上下文。

#### 4.3.3 源码精读

判定与取缓存（注意版本门 `CANN_VERSION_NUM >= 9.1.0`，老版本直接返回 `false`）：

- [src/ops/op_common/op_common.cc:290-321](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L290-L321) —— `ShouldGoCcuFastLaunch`：依次判定 `OFFLOAD`、`engine==CCU`、`SetOpParamFastLaunchTag`，再 `HcclEngineCtxGet` 查 CCU 上下文，命中则返回指针。

关键几行：

```cpp
if (param.opMode == OpMode::OFFLOAD) { return false; }          // 图模式不走
if (param.engine != CommEngine::COMM_ENGINE_CCU) { return false; } // 必须是 CCU
if (SetOpParamFastLaunchTag(param) != HCCL_SUCCESS) { return false; }
...
if (HcclEngineCtxGet(comm, param.fastLaunchTag, CommEngine::COMM_ENGINE_CCU,
                     &fastLaunchCtxPtr, &size) == HCCL_SUCCESS) {
    *ccuFastLaunchCtx = reinterpret_cast<CcuFastLaunchCtx*>(fastLaunchCtxPtr);
    return true;
}
return false;
```

缓存上下文结构（变长布局）：

- [src/ops/op_common/inc/alg_param.h:365-391](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/inc/alg_param.h#L365-L391) —— `CcuFastLaunchCtx`：`algName`/`notifyNumOnMainThread`/`threadNum`/`ccuKernelNum[]` 头部，外加 `GetThreadHandlePtr()`/`GetCcuKernelSubmitInfoPtr()` 用 `offsetof`+指针运算定位尾部两个数组。

回放执行：

- [src/ops/op_common/op_common.cc:460-506](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L460-L506) —— `HcclExecOpCcuFastLaunch`：用 `CollAlgExecRegistryV2::GetAlgExec` 按 `algName` 取 executor，取 CCL buffer，`HcclThreadAcquireWithStream` 绑定主流，最终 `executor->FastLaunch(param, ctx)`。

函数声明位置（声明在 op_common.h）：

- [src/ops/op_common/op_common.h:120-122](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.h#L120-L122) —— `ShouldGoCcuFastLaunch`（返回 `bool`）与 `HcclExecOpCcuFastLaunch`。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：理解 FastLaunch 的缓存键与回放最小集。
2. **操作步骤**：
   - 读 [op_common.cc:290-321](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L290-L321)，找出缓存键是什么（`param.fastLaunchTag`，由 `SetOpParamFastLaunchTag` 生成）。
   - 读 [op_common.cc:460-506](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L460-L506)，列出回放时的四个步骤。
3. **需要观察的现象**：注意 `FastLaunch` 复用的是**已注册的 executor**（通过 `algName` 查注册表），而不是重新跑 Selector。
4. **预期结果**：回放四步 = 取 executor → 取 CCL buffer → 绑定主流 → `executor->FastLaunch`。
5. **待本地验证**：是否真正命中 FastLaunch 取决于是否已有同 key 的 CCU 上下文（需上板多次调用同一算子），日志关键字 `[ShouldGoCcuFastLaunch]` 与 `[HcclExecOpCcuFastLaunch]`，待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`ShouldGoCcuFastLaunch` 为什么把 `engine==CCU` 的判定放在函数内部，而不是写在主流程的 `if` 里？

> **参考答案**：为了职责封装——主流程只关心「能不能走快速路径」（一个 `bool`），引擎是否匹配是快速路径的内部细节。这样主流程的判定链保持简洁，且未来若 AICPU 也引入类似的 FastLaunch，只需改函数内部即可。

**练习 2**：CCU FastLaunch 命中时，`algName` 从哪里来？为什么不需要 `Selector()`？

> **参考答案**：`algName` 存在 `CcuFastLaunchCtx.algName` 里——它是在**第一次**执行时（经过 Selector 选定算法后）连同指令序列一起缓存下来的。命中时直接复用这个 algName 去 `CollAlgExecRegistryV2` 取 executor，所以无需再跑一遍 Selector。

---

### 4.4 AIV Cache Replay 快速路径（HcclAivCacheCheckAndReplay）

#### 4.4.1 概念说明

AIV 引擎跑的是 Vector Core 上的 kernel，HCCL 要为其组装一组「AIV 指令（`AivInstruction`）」再下发。和 CCU 类似，若同一算子反复调用，重复组装指令开销大。**AIV Cache Replay** 在 `engine==AIV` 时尝试用一组特征字段算出缓存键，查到已缓存的指令就直接 `ReplayAivInstructions` 回放，跳过 Selector。

与 CCU FastLaunch 的关键差别：

| 维度 | CCU FastLaunch | AIV Cache Replay |
|------|----------------|------------------|
| 引擎 | CCU | AIV |
| 缓存对象 | `CcuFastLaunchCtx`（指令序列+线程） | `AivInstruction` 指令数组 |
| 缓存键 | `fastLaunchTag` | 由 `AivOpCacheArgs` 计算的哈希 |
| 适用算子 | 走 CCU 的算子 | `IsAivCacheSupported` 限定的一组算子 |
| 模式限制 | 仅 `OPBASE`、非图模式 | 仅 `OPBASE`、非捕获流（非 aclgraph） |

#### 4.4.2 核心流程

```text
HcclAivCacheCheckAndReplay(comm, param, &cacheHit)
  ├─ IsAivCacheSupported(param)?  否 → cacheHit=false; return
  │     支持条件：opType ∈ {ALLGATHER,ALLREDUCE,REDUCE_SCATTER,BROADCAST,REDUCE,ALLTOALL,ALLTOALLV,SCATTER}
  │              && opMode==OPBASE && !IsStreamInCaptureMode(stream)
  ├─ 取 numBlocksLimit（先 aivParam，fallback 到 runtime Vector Core 数）
  ├─ 组装 cacheKey = {commName, opType, root, reduceOp, numBlocksLimit, dataType, count}
  ├─ keyHash = CalcAivCacheKeyHash(cacheKey); ctxTag = BuildAivCacheCtxTag(keyHash)
  ├─ LookupAivCacheCtx(...)   # 查缓存
  │     未命中 → cacheHit=false; return
  │     命中 → cacheHit=true
  ├─ 把 cachedAlgName 写入 param.algName / algTag；注册 DFX
  └─ ReplayAivInstructions(instructions, insCount, param)   # 直接回放
```

缓存键的「维度」决定了哪些变化会让缓存失效：只要 `commName / opType / root / reduceOp / numBlocksLimit / dataType / count` 任一不同，就视为不同算子，不复用。`ALLTOALLV` 比较特殊——`counts` 在 replay 时动态刷新，所以 key 里 `count=0` 不区分。

#### 4.4.3 源码精读

支持性判定（哪些算子 / 哪些模式允许走 AIV Cache）：

- [src/ops/op_common/op_common.cc:377-384](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L377-L384) —— `IsAivCacheSupported`：限定 8 种 opType + `OPBASE` + 非 stream 捕获模式。

```cpp
return (param.opType == HCCL_CMD_ALLGATHER || param.opType == HCCL_CMD_ALLREDUCE
        || param.opType == HCCL_CMD_REDUCE_SCATTER || param.opType == HCCL_CMD_BROADCAST
        || param.opType == HCCL_CMD_REDUCE || param.opType == HCCL_CMD_ALLTOALL
        || param.opType == HCCL_CMD_ALLTOALLV || param.opType == HCCL_CMD_SCATTER)
       && param.opMode == OpMode::OPBASE && !IsStreamInCaptureMode(param.stream);
```

命中与回放主体：

- [src/ops/op_common/op_common.cc:386-458](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L386-L458) —— `HcclAivCacheCheckAndReplay`：组装 `AivOpCacheArgs`、算哈希、`LookupAivCacheCtx` 查缓存，命中则写 `param.algName` 并 `ReplayAivInstructions`。

缓存键组装的关键片段：

```cpp
AivOpCacheArgs cacheKey = {};
cacheKey.commName = param.commName;
cacheKey.opType   = param.opType;
cacheKey.root     = param.root;
cacheKey.reduceOp = param.reduceType;
cacheKey.numBlocksLimit = numBlocksLimit;
...
cacheKey.count    = param.DataDes.count;
cacheKey.dataType = param.DataDes.dataType;
u64 keyHash = CalcAivCacheKeyHash(cacheKey);
```

- [src/ops/op_common/op_common.cc:406-430](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L406-L430) —— 组装键、算哈希、`LookupAivCacheCtx`。

命中后回放：

- [src/ops/op_common/op_common.cc:449-453](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L449-L453) —— `ALLTOALLV` 走 `ReplayAivInstructionsV`，其余走 `ReplayAivInstructions`。

函数声明：

- [src/ops/op_common/op_common.h:124-124](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.h#L124-L124) —— `HcclAivCacheCheckAndReplay` 声明。

#### 4.4.4 代码实践（源码阅读型）

1. **实践目标**：搞清 AIV Cache 的「键维度」与「失效条件」。
2. **操作步骤**：
   - 读 [op_common.cc:406-421](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L406-L421)，列出 `AivOpCacheArgs` 的全部字段。
   - 读 [op_common.cc:412-421](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L412-L421)，对比 `ALLTOALL/ALLTOALLV` 与普通算子取 `count/dataType` 的差异。
3. **需要观察的现象**：注意 `ALLTOALLV` 的 `count=0`（注释「counts 在 replay 时动态刷新，cache key 不区分」）。
4. **预期结果**：键维度 = {commName, opType, root, reduceOp, numBlocksLimit, dataType, count}；任一变化即失效。
5. **待本地验证**：是否命中取决于是否已有同 key 的 AIV 指令缓存，日志关键字 `[HcclAivCacheCheckAndReplay] cache hit`，待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 AIV Cache 要把 `numBlocksLimit`（Vector Core 数）算进缓存键？

> **参考答案**：AIV kernel 的指令编排依赖可用的 Vector Core 数（决定并行度/分块），同一算子在不同 `numBlocksLimit` 下生成的指令不同。把它纳入键，避免在核心数不同的运行环境间错误复用指令。

**练习 2**：aclgraph 捕获模式（`IsStreamInCaptureMode==true`）下，AIV Cache 会被跳过。结合 u2-l2 的「图模式强制开启 EntryLog」与「算子异步下发」，说说为什么捕获流不宜走 Replay？

> **参考答案**：aclgraph 捕获要求算子下发的指令序列在捕获期间是确定、可重放的；而 Cache Replay 走的是「查表→回放已有指令」的旁路，与捕获期「记录下发序列」的语义不一致，可能造成捕获到的图与实际执行不符。故捕获流下 `IsAivCacheSupported` 直接返回 `false`，回退到正常路径。

---

## 5. 综合实践

**任务**：用一张流程图，完整表达 `AllReduceOutPlaceCommon` 中「从 `FillAllReduceOpParam` 到 `Selector()` 之间」的**全部分支及其优先级**，并标注每条分支的触发条件、命中后调用谁、是否提前结束。

**操作步骤**：

1. 重新精读 [src/ops/all_reduce/all_reduce_op.cc:189-234](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/all_reduce_op.cc#L189-L234) 的 `AllReduceOutPlaceCommon`。
2. 画出一张包含以下节点的流程图（手绘或工具皆可）：
   - 起点：`FillAllReduceOpParam` → `HcclGetOpExpansionMode`
   - 分支 A：`OPBASE && HCOMM==9.0.0 && engine==CCU` → `HcclAllReduceInner`（提前结束）
   - 分支 B：`OPBASE && ShouldGoCcuFastLaunch` → `HcclExecOpCcuFastLaunch`（提前结束）
   - 分支 C：`engine==AIV && HcclAivCacheCheckAndReplay 命中` → 提前结束
   - 分支 D：`rankSize==1` → `SingleRankProc`（提前结束）
   - 主路径：`Selector` → `HcclExecOp`
3. 在每条分支旁标注三个细节：**谁判定**（主流程 `if` 还是子函数内部）、**命中代价**（跳过了什么）、**模式限制**（`OPBASE` / `OFFLOAD`）。
4. 用箭头标出优先级方向（自上而下，命中即返回）。

**预期结果**：一张能回答以下问题的图——

- 「9.0.0 设备 + CCU 引擎」走哪条路？（A）
- 「单算子 + CCU + 已有缓存」走哪条路？（B）
- 「AIV 引擎 + 已有指令缓存」走哪条路？（C）
- 「通信域只有一张卡」走哪条路？（D）
- 「以上都不满足」走哪条路？（主路径 Selector）

**待本地验证**：本实践为源码阅读型，无需运行；若想验证运行期实际命中分支，可在上板环境通过日志关键字（`[ShouldGoCcuFastLaunch]`、`[HcclAivCacheCheckAndReplay] cache hit`、`enter SingleRankProc`）确认，具体能否触发取决于设备型号、HCOMM 版本与是否重复调用，待本地验证。

## 6. 本讲小结

- `param.engine` 由 `HcclGetOpExpansionMode` 设定：它先 `DecideHcclOpExpansionMode` 决定展开模式（优先 HCOMM 配置，否则按环境变量优先级），再 `ApplyOpExpansionMode` 用 `switch` 映射成 `CommEngine`（`AICPU_TS / AIV / CCU`）与 `OpExecuteConfig`。
- `AllReduceOutPlaceCommon` 是一条「自上而下、命中即返回」的判定链：9.0 CCU 老流程（A）→ CCU FastLaunch（B）→ AIV Cache（C）→ 单卡（D）→ 主路径 `Selector()→HcclExecOp()`。
- **CCU FastLaunch** 用 `fastLaunchTag` 查 `CcuFastLaunchCtx`，命中后用 algName 取已注册 executor 的 `FastLaunch` 回放，跳过 Selector；仅 `OPBASE` + `engine==CCU`，且需 `CANN_VERSION>=9.1.0`。
- **AIV Cache Replay** 用 `AivOpCacheArgs`（commName/opType/root/reduceOp/numBlocksLimit/dataType/count）算哈希查 `AivInstruction`，命中后 `ReplayAivInstructions` 回放；仅 `OPBASE` + 非捕获流 + 8 种支持的 opType。
- 分支 A、B 都要求 `OPBASE`，图模式（`OFFLOAD`）不走这两条快速路径，体现「图模式资源由 GE 管理」的架构约束。
- 引擎与算法是正交维度：本讲只决定「哪个引擎」，真正选「哪个算法（Ring/Mesh/NHR…）」是下一讲 `Selector` 的事；快速路径的本质是「跳过 Selector 与资源计算、复用已有结果」。

## 7. 下一步学习建议

本讲止步于 `Selector()` 调用——快速路径都没命中时，`Selector` 如何根据引擎、拓扑、数据量选出具体的 `algName`（如 `AicpuAllReduceSoleNHR` / `CcuMSAllReduceSoleMesh`），正是下一讲 **u3-l1（op_common 架构与三大注册表总览）** 与 **u3-l2（算法选择器 Selector）** 的内容。建议：

1. 先读 u3-l1，建立「Selector → HcclExecOp → executor → template」的全局数据流视图，看清本讲的 `HcclExecOp` / `HcclExecOpCcuFastLaunch` 在其中处于哪一环。
2. 再读 u3-l2，理解 `ExecuteSelector::Run` 如何按优先级遍历、产出 algName，与本讲的「分支优先级」思想一脉相承。
3. 若想深入 CCU/AIV 引擎本身（Mission 抽象、URMA、Vector Core kernel），可跳到 Unit 5 的 u5-l3（AIV）与 u5-l4（CCU）；想了解环境变量如何影响 `DecideHcclOpExpansionMode` 的判定，看 u4-l3（环境变量与算法配置系统）。

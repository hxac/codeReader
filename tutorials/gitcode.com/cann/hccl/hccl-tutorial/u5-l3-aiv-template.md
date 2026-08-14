# u5-l3 AIV 模板与 Vector Core 通信

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 AIV 引擎「低延迟但占用 Vector Core」的特点，以及它适合什么场景、不适合什么场景。
2. 掌握 AIV 模板的抽象基类 `AivAlgTemplateBase`：它定义了哪些生命周期接口、哪些由子类覆盖、哪些有默认实现。
3. 掌握 `hccl_aiv_utils` 工具集：`AivOpArgs` 参数包、AIV kernel 二进制的注册与下发、AIV Cache 的数据结构。
4. 对比 `AivTempAllReduceMesh1DOneShot` 与 `AivTempAllReduceMesh1DTwoShot` 两个模板在小数据低延迟与稍大数据量下的取舍。
5. 解释为什么 u2-l4 的入口分发链中，AIV 路径要先做 `HcclAivCacheCheckAndReplay`。
6. 了解本轮演进为 AIV 模板新增的静态代价标定函数 `CalcCostCoeff` 与编译期属性 `TemplateProp props`（均供 u8 的代价模型选择器使用）。

## 2. 前置知识

- **Vector Core（向量核）**：昇腾 NPU 上负责向量计算的核。AICPU 是独立的小 CPU（不占计算核），而 AIV kernel 直接跑在 Vector Core 上。好处是「Host 下发 → 核执行」路径极短、延迟极低；代价是**挤占了 AI 计算可用的 Vector 核**。因此 AIV 引擎适合小数据量、低延迟敏感的集合通信。
- **模板（Template）**：u3-l5 讲过，模板是「算法 × 引擎」组合下真正下发数据搬移指令的抽象。本讲聚焦其中的 AIV 家族。
- **AIV kernel 二进制**：AIV 通信 kernel 是预编译好的二进制（按 `cmdType × dataType × argsType` 组织），Host 侧不「解释执行」算法，只组装一份参数包 `AivOpArgs` 并 launch 对应 kernel，算法逻辑全部在核上执行。
- **代价模型**：u3-l5/u8 提到的 `CostModelParam (A, B, C)` 三元组，建模耗时 \( T(n) = A \cdot n + B \cdot n + C \)，其中 A 是跨卡传输系数、B 是本地拷贝/归约系数、C 是固定时延常数。本轮 AIV 模板新增了静态 `CalcCostCoeff` 函数来申报这组系数。
- **OPBASE 与流捕获**：OPBASE 指单算子执行模式（区别于图模式）。「流捕获」指 aclgraph 场景下 stream 正在被录制成图，此时不能做缓存回放一类的动态行为。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/ops/op_common/template/aiv_alg_template_base.h/.cc` | AIV 模板抽象基类：生命周期接口 + 线程同步 + 默认实现 |
| `src/ops/op_common/template/aiv/hccl_aiv_utils.h/.cc` | AIV 工具集：`AivOpArgs` 参数包、kernel 注册/下发、AIV Cache 结构与读写 |
| `src/ops/all_reduce/template/aiv/aiv_temp_all_reduce_mesh_1D_oneshot.h/.cc` | AIV one-shot Mesh AllReduce 模板 |
| `src/ops/all_reduce/template/aiv/aiv_temp_all_reduce_mesh_1D_twoshot.h/.cc` | AIV two-shot Mesh AllReduce 模板 |
| `src/ops/all_reduce/executor/ins_v2_all_reduce_sole_executor.cc` | 通过 `REGISTER_EXEC_V2` 把 algName 绑定到上述 AIV 模板 |
| `src/ops/op_common/op_common.cc` | `HcclAivCacheCheckAndReplay`（回放入口）与录制/存储流程 |
| `src/ops/op_common/selector/cost_model.h` | `CostModelManager` 提供 A/B/C 各分量的计算接口 |

注意一个目录细节：`aiv_alg_template_base.h` 位于 `src/ops/op_common/template/` 下，而 `hccl_aiv_utils`、kernel 定义等在子目录 `template/aiv/` 下，具体算子的 AIV 模板在 `src/ops/all_reduce/template/aiv/` 下——三层不要混淆。

## 4. 核心概念与源码讲解

### 4.1 AivAlgTemplateBase 抽象

#### 4.1.1 概念说明

`AivAlgTemplateBase` 是所有 AIV 模板的公共基类。与 AICPU 模板（u5-l1）相比，它的「资源观」完全不同：AICPU 模板要申请 thread/notify、走 slave thread 协作；而 AIV 模板把整条算法放进**一个 kernel、多个 Vector 核 block** 里执行，Host 侧只需要：

1. 算好要用几个核（`CalNumBlocks`）；
2. 算好通道资源（`CalcRes`，向 RankGraph 要通信通道）；
3. 组装参数包并下发 kernel（`KernelRun`）。

#### 4.1.2 核心流程

一个 AIV 模板的完整生命周期：

```text
executor 实例化模板（构造函数：拷贝 rank/子通信域/归约类型等快照）
    │
    ├─ CalcRes            # host 侧：申请 level0 通道，规划 scratch
    ├─ CalNumBlocks       # host 侧：决定本次用几个 Vector 核
    └─ KernelRun          # host 侧：组装 AivOpArgs → ExecuteKernelLaunch 下发
                           #     算法逻辑在 Vector Core 上的 AIV kernel 内完成
```

#### 4.1.3 源码精读

基类声明，注意每个虚函数的分工：

[AivAlgTemplateBase 声明:src/ops/op_common/template/aiv_alg_template_base.h#L28-L56](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/aiv_alg_template_base.h#L28-L56)

这段代码定义了 AIV 模板契约：`Describe()` 纯虚（自描述字符串）、`CalcRes`/`KernelRun`/`FastLaunch` 等有「报错兜底」的默认实现（[aiv_alg_template_base.cc:L40-L60](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/aiv_alg_template_base.cc#L40-L60) 中默认实现直接返回 `HCCL_E_INTERNAL`，即子类必须覆盖才算合法模板），`PreSync/PostSync` 提供多线程信号量同步的通用实现。

**本轮新增的编译期模板属性 `props`**：

[TemplateProp 定义:src/ops/op_common/template/common_alg_template_base.h#L20-L22](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/common_alg_template_base.h#L20-L22)

基类在 [aiv_alg_template_base.h:L30-L31](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/aiv_alg_template_base.h#L30-L31) 声明了 `static constexpr TemplateProp props = {};`，同时新增了 `cost_model.h` 与 `common_alg_template_base.h` 两个 include（[L20-L21](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/aiv_alg_template_base.h#L20-L21)）。注意这是 `static constexpr` 成员而非虚函数——子类通过「同名遮蔽」而非 override 来申报属性，且申报发生在**编译期**。目前唯一的字段 `isNhr`（是否为 NHR 算法）会影响 u8 代价模型的 `netType` 选择（`CalcMeshParam` 走 NHR 还是 Mesh 组网模型）；AIV 基类默认空属性（`isNhr=false`），而 NHR 系模板会覆盖为 `props = {.isNhr = true}`（例如 [ins_temp_all_gather_nhr.h:L21](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_gather/template/aicpu/ins_temp_all_gather_nhr.h#L21)）。本讲的 Mesh 1D AIV 模板均不覆盖它。

**本轮新增的代价标定挂钩**：

[aiv_alg_template_base.h:L38](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/aiv_alg_template_base.h#L38)

```cpp
static std::vector<CostModelParam> CalcCostCoeff(CalcCostCoeffParam param) { return {}; }
```

这是一行静态虚替换设计：基类默认返回**空 vector**，语义是「该模板未标定代价」——u8 的新选择器 `CostTableManager` 遇到空返回会直接跳过这个算法，不会拿它参与比价。只有完成标定的子模板（如本讲的 oneshot/twoshot）才覆盖它。

构造函数快照了执行所需的全部上下文：

[构造函数:src/ops/op_common/template/aiv_alg_template_base.cc#L16-L26](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/aiv_alg_template_base.cc#L16-L26)

注意 `tempRankSize_(subCommRanks[0].size())`：模板以**子通信域**（第 0 维）的 rank 数为准，而不是整个通信域——分级通信时 AIV 只负责其中一级。

`CalNumBlocks` 的默认折算规则：

[CalNumBlocks 默认实现:src/ops/op_common/template/aiv_alg_template_base.cc#L68-L78](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/aiv_alg_template_base.cc#L68-L78)

可用核数充足时用满 `tempRankSize_` 个核；不足时退化为 `numBlocksLimit`。子类可以覆盖这个折算（下面 oneshot/twoshot 就各有各的版本）。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：弄清「哪些接口子类必须覆盖、哪些可以继承默认实现」。
2. **操作步骤**：打开 `aiv_alg_template_base.h`，把虚函数分成三列记录在表格里——纯虚（必须覆盖）、默认报错（不覆盖则运行失败）、默认可用（可直接继承）。
3. **需要观察的现象**：`Describe` 是唯一纯虚函数；`CalcRes/KernelRun/FastLaunch` 默认报错；`CalNumBlocks/CalcScratchMultiple/PreSync/PostSync` 默认可用；`CalcCostCoeff` 与 `props` 是静态成员，走「同名遮蔽」而非 override。
4. **预期结果**：得出结论「一个最小合法 AIV 模板 = 实现 Describe + CalcRes + KernelRun」，与 4.4 节 oneshot 模板的 override 列表互相印证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `CalcCostCoeff` 设计成**静态**函数，而 `CalcRes/KernelRun` 是普通虚函数？

**答案**：`CalcRes/KernelRun` 需要读取实例状态（`myRank_`、`subCommRanks_`、`sliceId_` 等），且只在执行期对已实例化的模板调用；而 `CalcCostCoeff` 服务于算法**选择阶段**（u8 的 CostTable 生成），那时还没有任何模板实例、没有 OpParam，选择器只能通过注册表拿到类名直接调用静态函数离线估算，因此必须 static 且参数全部由 `CalcCostCoeffParam` 显式携带。

**练习 2**：`PreSync/PostSync` 用 `HcommThreadNotifyRecordOnThread/WaitOnThread` 做什么？

**答案**：它们是主流/从流之间的信号量同步（post/wait notify），用于多线程模板中主流通知从流开工（PreSync）与主流等待所有从流收工（PostSync）。但注意本讲 oneshot/twoshot 模板的 `CalcRes` 中 `threadNum = 1`，即单线程执行，这对同步接口在 Mesh 1D AIV 模板里实际不启用。

**练习 3**：本轮在基类新增的 `static constexpr TemplateProp props = {};` 与 `static CalcCostCoeff` 都是静态成员，为什么模板的「身份属性」不走虚函数？

**答案**：`props` 与 `CalcCostCoeff` 都服务于 u8 代价模型选择器的**离线比价**阶段——那时没有模板实例（实例化要走 executor 的编译期绑定与运行期工厂），虚函数表无从谈起。`static constexpr` 属性在编译期就随类型确定，注册宏在向 AllAlgos/执行器登记算法元数据时即可携带；子类用同名遮蔽申报（如 NHR 系模板的 `props = {.isNhr = true}`），运行期通过类作用域直接读取，零开销且不依赖实例状态。

### 4.2 hccl_aiv_utils：参数包、内核注册与下发

#### 4.2.1 概念说明

`hccl_aiv_utils` 是 AIV 家族的公共工具层，解决三件事：

1. **参数打包**：`AivOpArgs` 结构把一次通信所需的全部信息（算子类型、输入输出地址、rank/rankSize、三维拓扑、切片参数、核数等）打包成一个 POD 风格结构，直接映射为 kernel 入参。
2. **内核注册**：`RegisterKernel()` 在每个 device 上首次使用时，把预编译的 AIV kernel 二进制按 `(cmdType, dataType, argsType)` 注册成可查的函数句柄。
3. **下发**：`ExecuteKernelLaunch()` 把 `AivOpArgs` 转成 kernel 参数结构并真正 launch；同时它还兼任 **AIV Cache 的录制钩子**。

#### 4.2.2 核心流程

```text
RegisterKernel（每 device 一次）
    遍历 GetAivKernelInfoMap(deviceType)
        加载二进制（静态模式从内嵌数据 / 动态模式从 bin 文件）
        按 (cmdType, dataType, argsType) 注册函数句柄

KernelRun（每次算子调用）
    组装 AivOpArgs
        └─ ExecuteKernelLaunch(opArgs)
             ├─ 若 g_recordingQueue 非空：把 opArgs 录制成 AivInstruction（供 cache 存储/回放）
             └─ 按 cmdType 装配 AivKernelArgs / AivExtraKernelArgs
                  └─ ExecuteKernelLaunchInner → aclrtLaunchKernelWithHostArgs 真正下发
```

#### 4.2.3 源码精读

`AivOpArgs` 参数包：

[AivOpArgs 结构:src/ops/op_common/template/aiv/hccl_aiv_utils.h#L81-L113](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/aiv/hccl_aiv_utils.h#L81-L113)

这个结构就是「Host 与 Vector Core 之间的全部契约」：`cmdType` 选 kernel，`input/output` 是算好的绝对地址，`xRankSize/yRankSize/zRankSize + topo_[]` 用一段连续数组编码三维子通信域拓扑（X 从偏移 0 开始、Y 从 `TOPO_LEN_Y_OFFSET=8`、Z 从 `TOPO_LEN_Z_OFFSET=16`，见 [aiv_alg_template_base.h:L24-L26](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/aiv_alg_template_base.h#L24-L26)），`numBlocks` 是本次启用的 Vector 核数，`sliceId` 每次调用自增用于区分轮次。

内核注册入口：

[RegisterKernel:src/ops/op_common/template/aiv/hccl_aiv_utils.cc#L658-L733](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/aiv/hccl_aiv_utils.cc#L658-L733)

这段代码按 device 做一次性初始化（`registry.initialized` 守卫 + 互斥锁）：先由 `HcclGetDeviceType` 探测芯片代际（u4-l2），再遍历该设备支持的 kernel 清单；`HCCL_STATIC_MODE` 下从内嵌数据加载二进制，否则从文件系统按路径加载；最终以 `GetFuncKey(cmdType, dataType, argsType)` 为键注册句柄。同一个二进制内的多个 kernel 共享一个 `binHandle`。

下发外部接口（含录制钩子）：

[ExecuteKernelLaunch:src/ops/op_common/template/aiv/hccl_aiv_utils.cc#L1076-L1100](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/aiv/hccl_aiv_utils.cc#L1076-L1100)

关键点：函数开头检查 `g_recordingQueue`（thread_local）——若正处于录制状态，就把本次 `opArgs` 转成 `AivInstruction`（把绝对地址换算成相对 `g_baseInputAddr/g_baseOutputAddr` 的偏移，使缓存的指令可在换缓冲后重放）压入队列；`g_recordOnlyMode` 为真时甚至不再真正 launch（图模式离线录制即用此模式，见 [calc_resource_graph_mode.cc:L402-L431](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/interface_graph_mode/calc_resource_graph_mode.cc#L402-L431)）。

随后按 `cmdType` 装配参数：

[参数装配与下发:src/ops/op_common/template/aiv/hccl_aiv_utils.cc#L1102-L1160](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/aiv/hccl_aiv_utils.cc#L1102-L1160)

普通算子走 `AivKernelArgs`（字段与 `AivOpArgs` 一一对应展开），AlltoAllV 额外携带变长 counts/displs 指针走 `AivExtraKernelArgs`；最终都汇入 `ExecuteKernelLaunchInner`（[hccl_aiv_utils.cc:L997-L1073](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/aiv/hccl_aiv_utils.cc#L997-L1073)），其中用函数句柄查表并通过 `aclrtLaunchKernelWithHostArgs` 把参数交给运行时，同时用 `AivLaunchGuard` 计数保证注销时等待在途 launch 完成。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：验证「一个 AivOpArgs 是如何原样变成 kernel 入参的」。
2. **操作步骤**：对照阅读 `hccl_aiv_utils.h` 的 `AivOpArgs`（L81-L113）与 `hccl_aiv_utils.cc` 的 `ExecuteKernelLaunch`（L1131-L1158），逐字段画一条连线：`opArgs.xxx → aivKernelArgs.xxx`。
3. **需要观察的现象**：两边的字段名、顺序几乎完全一致（除 `counter` 三项换算为指针、`extraArgs` 仅 V 算子有）。
4. **预期结果**：得出结论——AIV 下发是「参数直传」模型，Host 不解释算法，因此 AIV 路径的 Host 侧开销极小，这正是它低延迟的根源。

#### 4.2.5 小练习与答案

**练习 1**：`RegisterKernel` 为什么要按 `deviceType` 取 `GetAivKernelInfoMap(deviceType)`，而不是所有设备共用一份 kernel 清单？

**答案**：不同代际芯片（910/910B/950/960，见 u4-l2）的 Vector Core 指令与能力不同，预编译的 AIV 二进制按设备类型区分；按设备类型选清单可以保证只注册当前芯片实际可用的 kernel，同时静态/动态两种加载方式也保证了部署形态（静态库内嵌 vs 独立 bin 文件）的适配。

**练习 2**：录制 `AivInstruction` 时为什么要把 `input/output` 绝对地址换算成相对 `g_baseInputAddr/g_baseOutputAddr` 的偏移？

**答案**：缓存的指令要在**后续调用**中重放，而后续调用的用户缓冲地址大概率不同。存「相对本次基址的偏移」，回放时再用 `param.inputPtr + inputOffset` 重建绝对地址（见 4.3 节 `ReplayAivInstructions`），缓存才能跨调用复用。

### 4.3 AIV Cache 与 AivCacheCheckAndReplay 的关系

#### 4.3.1 概念说明

AIV 的痛点：每次 `HcclAllReduce` 调用都要走完整的「入口校验 → Selector 选算法 → CalcRes 算资源 → KernelRun 下发」链路，而 AIV 恰恰服务小数据场景——**Host 侧固定开销可能超过通信本身**。AIV Cache 的思路：对同一 `(commName, opType, count, dataType, reduceOp, root, numBlocksLimit)` 组合的重复调用，第一次执行时把下发的指令序列录下来，后续直接重放，跳过 Selector 与资源计算。

这就是 u2-l4 中 `AllReduceOutPlaceCommon` 在进入 `Selector()` **之前**先调 `HcclAivCacheCheckAndReplay` 的原因：AIV 的调用模式高度重复（训练循环里成千上万次相同 shape 的 AllReduce），命中缓存时整条主链路都被旁路。

#### 4.3.2 核心流程

```text
第一次调用（未命中）：
    IsAivCacheSupported? ── 是 → 计算 cacheKey 哈希 → Lookup 未命中
        → 正常走 Selector → HcclExecOp
        → 执行前挂 g_recordingQueue（录制模式）
        → executor->Orchestrate 执行（KernelRun 内的 ExecuteKernelLaunch 被逐条录制）
        → StoreAivCacheCtx 把指令序列 + algName 写入通信域 ctx

后续调用（命中）：
    HcclAivCacheCheckAndReplay → Lookup 命中
        → 直接对每条 AivInstruction 换新地址后 ExecuteKernelLaunch 重放
        → 返回，完全跳过 Selector/CalcRes
```

#### 4.3.3 源码精读

缓存启用条件：

[IsAivCacheSupported:src/ops/op_common/op_common.cc#L383-L390](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L383-L390)

白名单覆盖 8 种 opType，且必须同时满足：单算子模式（OPBASE）、流不在捕获中（aclgraph 录制期间不能做动态回放）。

缓存键定义（严格弱序，可作 `std::map` 键）：

[AivOpCacheArgs:src/ops/op_common/template/aiv/hccl_aiv_utils.h#L116-L141](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/aiv/hccl_aiv_utils.h#L116-L141)

七元组逐字段比较；命中即认为「上次的指令序列本次仍适用」。

回放入口：

[HcclAivCacheCheckAndReplay:src/ops/op_common/op_common.cc#L392-L462](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L392-L462)

这段代码先组装 cacheKey（AlltoAllV 的 counts 在回放时动态刷新，故 count 恒置 0），再 `LookupAivCacheCtx` 查通信域 ctx；命中后把缓存的 algName 回填 `param.algName`、注册 DFX 信息，最后调 `ReplayAivInstructions` 重放并上报 profiling。调用点在 [all_reduce_op.cc:L212](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/all_reduce_op.cc#L212)，位于 Selector 之前。

指令重放：

[ReplayAivInstructions:src/ops/op_common/template/aiv/hccl_aiv_utils.cc#L909-L919](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/aiv/hccl_aiv_utils.cc#L909-L919)

逐条取出 `AivInstruction`：换上本次的 `stream/inputPtr/outputPtr`（基址 + 录制偏移），其余参数原样复用，重新 `ExecuteKernelLaunch`。

录制与存储（cache 未命中的正常执行路径中挂录制）：

[录制与存储流程:src/ops/op_common/op_common.cc#L515-L558](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L515-L558)

执行 `Orchestrate` 前创建 `g_recordingQueue` 并记录基址；执行中所有 `ExecuteKernelLaunch` 自动被录制（见 4.2.3）；执行后 `EvictAivCacheIfNeeded` 做环形索引淘汰（最多 `AIV_CACHE_INDEX_MAX_ENTRY = 8192` 条，超限淘汰最老的 20%，常量见 [hccl_aiv_utils.h:L37-L42](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/aiv/hccl_aiv_utils.h#L37-L42)），再 `StoreAivCacheCtx` 把 `AivCacheCtxHeader + algName + AivInstruction[]` 序列化进通信域 ctx（[hccl_aiv_utils.cc:L949-L980](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/aiv/hccl_aiv_utils.cc#L949-L980)）。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：验证「缓存命中时 Selector 完全不执行」。
2. **操作步骤**：在 `src/ops/all_reduce/all_reduce_op.cc` 中找到 `HcclAivCacheCheckAndReplay` 调用（L212 附近），观察其后代码如何依据 `aivCacheHit` 分支——命中时函数直接返回，未命中才继续走 `Selector()`。
3. **需要观察的现象**：命中分支里只做了 algName 回填、DFX 注册和 `ReplayAivInstructions`，没有任何算法选择与资源计算调用。
4. **预期结果**：能够画出「命中路径调用次数 = 常数（与算法/拓扑无关）」的结论；无法上板运行时，此为源码阅读结论，运行日志验证待本地验证（真机上可用 `HCCL_DEBUG` 观察重复调用第二次是否打印 `[HcclAivCacheCheckAndReplay] cache hit`，[op_common.cc:L441](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L441)）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `IsAivCacheSupported` 要求 `!IsStreamInCaptureMode(param.stream)`？

**答案**：流捕获（aclgraph）期间，运行时把 stream 上的操作录制成图，要求操作序列确定、可重放成图。AIV Cache 的「动态查缓存 + 条件回放」行为与录制语义冲突（首次 miss 走全链路、后续 hit 走回放，录出来的图不一致），所以捕获态下禁用缓存。

**练习 2**：缓存键里有 `numBlocksLimit`，为什么？

**答案**：`numBlocksLimit` 决定 `CalNumBlocks` 的折算结果，即 kernel 实际启用的 Vector 核数；核数不同则录制下来的 `AivInstruction.opArgs.numBlocks` 不适用，必须视为不同的缓存条目。`HcclAivCacheCheckAndReplay` 中优先从 `aivParam->aivCoreLimit`、失败再查 runtime 资源（[op_common.cc:L399-L410](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L399-L410)），就是为了在查缓存前先固定这个键分量。

### 4.4 oneshot/twoshot 模板精读与取舍

#### 4.4.1 概念说明

两个模板都实现「单级 Mesh 1D 拓扑上的 AllReduce」，注册于同一个执行器 `InsV2AllReduceSoleExecutor`：

[AIV 模板注册:src/ops/all_reduce/executor/ins_v2_all_reduce_sole_executor.cc#L318-L322](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/executor/ins_v2_all_reduce_sole_executor.cc#L318-L322)

```cpp
REGISTER_EXEC_V2(HcclCMDType::HCCL_CMD_ALLREDUCE, AivAllReduceSoleMeshOneShot,
    InsV2AllReduceSoleExecutor, TopoMatch1D, AivTempAllReduceMesh1DOneShot);
REGISTER_EXEC_V2(HcclCMDType::HCCL_CMD_ALLREDUCE, AivAllReduceSoleMeshTwoShot,
    InsV2AllReduceSoleExecutor, TopoMatch1D, AivTempAllReduceMesh1DTwoShot);
```

注意两者都在 `#ifndef AICPU_COMPILE` 守卫内——AIV 模板只编入 host 形态，不进 AICPU 侧二进制。二者语义差异：

- **one-shot**：每张卡把**全量数据**直接写给所有对端，各卡收到 R−1 份远端拷贝后本地归约一次。通信轮数最少、时延最低，但每卡跨卡流量与归约量都正比于 R，且 cclBuff 要给每个对端留全量槽位（scratch 系数 = rankSize），适合**小数据**。
- **two-shot**：把 AllReduce 拆成 ReduceScatter + AllGather 两步，每步只搬 \( n \)（总量的一部分）。流量/内存占用更省，但步骤更多、时延常数更大，适合**稍大数据量**。

#### 4.4.2 核心流程

以 oneshot `KernelRun` 为主线的装配流程：

```text
IncSliceId()                       # 轮次自增
从 tempAlgParams 取输入输出地址（buff 基址 + 偏移）
    填 cmdType=ALLREDUCE / rank / rankSize / count / dataType / op
    填三维拓扑：subCommRanks_[0]→topo_[0..], [1]→topo_[8..], [2]→topo_[16..]
CalNumBlocks(numBlocks, sliceSize, numBlocksLimit)
    填 slice/repeat 步长参数
ExecuteKernelLaunch(aivAllReduceArgs)   # 下发（可能同时被录制）
```

本轮新增的代价标定，二者建模对比（\( n \) 为每步搬运量占总量的比例）：

- oneshot：\( A = \text{CalcMeshParam}(1, \cdots) \)（全量走一遍跨卡）、\( B = B_{copy}(1) + B_{reduce}(R-1) \)（本地一次拷贝 + R−1 份全量归约）、\( C = \text{CalcLatencyParams}(5) \)（任务步骤最少）。
- twoshot：\( A = \text{CalcMeshParam}(2n, \cdots) \)（RS+AG 两步各搬 \( n \)）、\( B = B_{copy}(n) + B_{reduce}(n(R-1)) \)、\( C = \text{CalcLatencyParams}(15) \)（步骤更多、固定时延更大）。

#### 4.4.3 源码精读

**oneshot 的资源计算与核数折算**：

[oneshot CalcRes/CalNumBlocks:src/ops/all_reduce/template/aiv/aiv_temp_all_reduce_mesh_1D_oneshot.cc#L59-L99](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/template/aiv/aiv_temp_all_reduce_mesh_1D_oneshot.cc#L59-L99)

`CalcRes` 中：单线程（`threadNum=1`，无 slave thread）；通道按拓扑二选一——CLOS 多桥且非 PCIE 混连时走 `CalcChannelRequestMeshClosMultiJetty` 并**只保留 `COMM_PROTOCOL_UB_MEM` 协议的通道**（AIV Mesh 依赖 UB 直访），否则走 `CalcChannelRequestMesh1D`。`CalNumBlocks` 中：核数下限是 `tempRankSize_ + 1`（每 rank 一个 block 再加一个协调 block）。

**oneshot 的 scratch 系数**：

[CalcScratchMultiple:src/ops/all_reduce/template/aiv/aiv_temp_all_reduce_mesh_1D_oneshot.cc#L51-L57](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/template/aiv/aiv_temp_all_reduce_mesh_1D_oneshot.cc#L51-L57)

返回 `tempRankSize_`——one-shot 要接收 R−1 份全量远端数据，cclBuff 需按 rank 数倍扩容，直观体现了它的内存代价。

**oneshot 的 KernelRun**：

[oneshot KernelRun:src/ops/all_reduce/template/aiv/aiv_temp_all_reduce_mesh_1D_oneshot.cc#L101-L158](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/template/aiv/aiv_temp_all_reduce_mesh_1D_oneshot.cc#L101-L158)

整段就是 4.2 节 `AivOpArgs` 的逐字段装配：三维拓扑分别落到 `topo_[0]`/`topo_[8]`/`topo_[16]` 起（Y/Z 仅在子通信域存在时填），最后 `ExecuteKernelLaunch` 下发。**没有任何通信步骤编排**——RS/写远端/本地归约的顺序全部固化在 Vector Core 上的 kernel 二进制里，这正是 AIV 模板与 AICPU 模板（u5-l1，在 `KernelRun` 里逐步调用数据面原语）最大的形态差异。

**本轮新增的 oneshot 代价标定**：

[oneshot CalcCostCoeff:src/ops/all_reduce/template/aiv/aiv_temp_all_reduce_mesh_1D_oneshot.cc#L24-L49](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/template/aiv/aiv_temp_all_reduce_mesh_1D_oneshot.cc#L24-L49)

逐行解读：`portNum` 按组网类型取 8（CLOS）/1（Mesh）；`taskNum=5` 是作者对 oneshot 任务步骤数的预估（传给 `CalcLatencyParams` 算 C）；A 用 `CalcMeshParam(1, ...)`（n=1，全量搬一遍）；B 由两段相加——`needLocalCopy` 时本地拷贝一份全量，再加 R−1 份远端拷贝的本地归约（源码注释明确「同时有 localreduce 和 localcopy，所以需要两个接口并相加」）。这些 `Calc*` 接口的签名与单位约定见 [cost_model.h:L93-L108](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/cost_model.h#L93-L108)。

**twoshot 的差异点**：

[twoshot CalcCostCoeff:src/ops/all_reduce/template/aiv/aiv_temp_all_reduce_mesh_1D_twoshot.cc#L24-L49](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/template/aiv/aiv_temp_all_reduce_mesh_1D_twoshot.cc#L24-L49)

与 oneshot 对照三处变化：`taskNum=15`（步骤更多→C 更大）；A 用 `CalcMeshParam(2 * param.n, ...)`（源码注释「第一步是 reducescatter」，RS 与 AG 各搬 \( n \)）；B 的归约量按 \( n(R-1) \) 计。

[twoshot CalcScratchMultiple/CalNumBlocks:src/ops/all_reduce/template/aiv/aiv_temp_all_reduce_mesh_1D_twoshot.cc#L51-L57](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/template/aiv/aiv_temp_all_reduce_mesh_1D_twoshot.cc#L51-L57) 与 [L88-L100](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/template/aiv/aiv_temp_all_reduce_mesh_1D_twoshot.cc#L88-L100)

scratch 固定系数 4（远小于 oneshot 的 rankSize）；核数折算改为 `coreNumPerRank * (rankSize+1)`——在每 rank 基础份额内取整，可整组放大利用更多核，与 oneshot 的「够用即可」策略不同。

[twoshot KernelRun 关键差异:src/ops/all_reduce/template/aiv/aiv_temp_all_reduce_mesh_1D_twoshot.cc#L110-L111](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/template/aiv/aiv_temp_all_reduce_mesh_1D_twoshot.cc#L110-L111)

twoshot 额外设置 `argsType = KernelArgsType::ARGS_TYPE_TWO_SHOT`，使 `RegisterKernel` 时同一 cmdType+dataType 下选择**另一份 kernel 变体**（`KernelArgsType` 枚举见 [hccl_aiv_utils.h:L44-L48](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/aiv/hccl_aiv_utils.h#L44-L48)）——one/two-shot 的算法差异最终落在两份预编译 kernel 二进制上，Host 侧模板只负责挑参数形态。

#### 4.4.4 代码实践（源码阅读型，对应本讲综合练习的准备）

1. **实践目标**：量化对比 oneshot 与 twoshot 在「小数据低延迟 vs 稍大数据量」下的取舍依据。
2. **操作步骤**：
   - 制作一张对照表，行是「A 的数据量参数 / B 的拷贝量 / B 的归约量 / C 的 taskNum / scratch 系数 / 核数折算 / argsType」，列是 oneshot 与 twoshot，全部数据取自上面引用的源码行。
   - 用代价模型公式 \( T(n) = A \cdot n + B \cdot n + C \) 分析：当总数据量 \( D \) 很小时，C 项主导——twoshot 的 C（taskNum=15）明显大于 oneshot（taskNum=5），oneshot 胜；当 \( D \) 增大后，A/B 项主导——twoshot 的跨卡量（2 份部分量）与 scratch（常数 4）优于 oneshot 的全量 × R 份，twoshot 胜。
3. **需要观察的现象**：两个模板的差异**全部**体现在常量参数与 kernel 变体选择上，`KernelRun` 主体代码几乎逐行相同。
4. **预期结果**：能写出类似结论：「oneshot = 最低步骤数换最大流量/内存，适合微消息；twoshot = 两倍步骤换 O(n) 流量，适合中等消息」；交叉点的具体字节数依赖 `CostModelManager::InitBandwidth` 标定的带宽参数（u8-l2），待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：两个模板共用执行器 `InsV2AllReduceSoleExecutor` 和拓扑匹配器 `TopoMatch1D`，差异都在模板里。这种设计的好处是什么？

**答案**：编排（executor）与拓扑切分（topo）不变，「同一编排下的不同搬运策略」只需增删一个模板文件加一行 `REGISTER_EXEC_V2`，即可被 Selector/新选择器按 algName（`AivAllReduceSoleMeshOneShot/TwoShot`）独立选用；代价标定（`CalcCostCoeff`）也随模板独立申报，选择器可离线比价。

**练习 2**：oneshot 的 `CalcCostCoeff` 中 B2 用 `CalcLocalReduceParams(param.rankSize - 1, EngineType::AICPU, B2)`，引擎参数为何传 `AICPU` 而不是 `AIV`？

**答案**：本地归约（LocalCopy/LocalReduce）执行的是 device 上通用核的拷贝/归约带宽模型，代价模型里 `EngineType` 区分的是「本地计算访存场景」（AICPU/CCU 各有标定带宽，见 [cost_model.h:L112-L119](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/cost_model.h#L112-L119)）与「kernel 启动时延场景」（C 项的 `EngineType::AIV`）；B 项按本地访存场景取 AICPU 标定带宽是模板作者对 kernel 内实际行为（通用拷贝/归约指令）的建模选择，C 项才是真正的引擎差异（AIV launch 时延）。

**练习 3**：`CalcRes` 末尾的 `HCCL_WARNING("Resource calculation is temporarily not performed in the template.")` 说明了什么现状？

**答案**：AIV Mesh 模板当前只申请了通道资源，thread/notify 等其余资源规划尚未在模板内完成（单线程模型下也暂不需要），这是一个标记给后续演进的 TODO 语义警告，不代表执行失败（函数仍返回 `HCCL_SUCCESS`）。

## 5. 综合实践

**任务：画出一次 AIV AllReduce 的完整时序，并标注「第二次调用」的捷径。**

步骤：

1. 以 8 卡、`HcclAllReduce(count=1024, FP32, SUM)`、AIV 引擎为例，从 u2-l4 的 `AllReduceOutPlaceCommon` 出发，沿 `HcclAivCacheCheckAndReplay →（未命中）→ Selector → HcclExecOp → InsV2AllReduceSoleExecutor → AivTempAllReduceMesh1DOneShot → ExecuteKernelLaunch → aclrtLaunchKernelWithHostArgs` 画出第一次调用的完整时序图，标注每一步发生的文件与函数。
2. 在同一张图上画出第二次相同调用的路径：`cacheKey 哈希 → LookupAivCacheCtx 命中 → ReplayAivInstructions（换地址重放）`，用高亮标出被跳过的环节（Selector、CalcRes、CalNumBlocks 装配）。
3. 对照 4.4 节的代价对照表，回答：如果 count 从 1024 增大到 1024×1024，你预期 Selector（或 u8 的新选择器经 `CalcCostCoeff` 比价）会更倾向哪个 algName？依据是什么？
4. 若有真机环境，用 `HCCL_DEBUG` 级别重复运行同一 AllReduce，观察第二次起是否输出 `[HcclAivCacheCheckAndReplay] cache hit`；无真机则标注「待本地验证」。

## 6. 本讲小结

- AIV 引擎把算法逻辑固化在 Vector Core 上的预编译 kernel 中，Host 侧只做「装配 `AivOpArgs` + launch」，路径短、延迟低，但占用 Vector 核，适合小数据场景。
- `AivAlgTemplateBase` 定义 AIV 模板契约：`Describe` 纯虚，`CalcRes/KernelRun/FastLaunch` 默认报错强制子类实现，`CalNumBlocks/同步接口` 提供默认实现；本轮新增静态 `CalcCostCoeff` 挂钩（默认空返回表示「未标定、不参与比价」）与编译期属性 `TemplateProp props`（`isNhr` 影响代价模型 netType 选择，AIV 基类默认空属性）。
- `hccl_aiv_utils` 三件套：`AivOpArgs` 参数包（Host↔核的全部契约）、`RegisterKernel` 按设备注册 kernel 二进制（键为 cmdType×dataType×argsType）、`ExecuteKernelLaunch` 下发并兼任缓存录制钩子。
- AIV Cache 以七元组 `(commName, opType, count, dataType, reduceOp, root, numBlocksLimit)` 为键，首次执行录制指令序列存入通信域 ctx，后续命中直接重放——这是 u2-l4 中 `AivCacheCheckAndReplay` 位于 Selector 之前的原因：旁路整条选算法/算资源链路。
- oneshot（全量直写 + 一次本地归约，scratch=R，taskNum=5）与 twoshot（ReduceScatter+AllGather，scratch=4，taskNum=15，独立 `ARGS_TYPE_TWO_SHOT` kernel 变体）的取舍本质是「步骤数 vs 流量/内存」；本轮两者的 `CalcCostCoeff` 把这一取舍量化为 A/B/C 系数，交给代价模型统一比价。

## 7. 下一步学习建议

- 下一讲 u5-l4 将进入 CCU 引擎模板：硬化通信单元如何用 Mission 抽象经 URMA 搬数据，与本讲 AIV 的「参数直传」模型形成三方对照（AICPU=Task 描述符、AIV=参数包、CCU=指令序列）。
- 建议顺带阅读 `src/ops/op_common/template/aiv/aiv_communication_base_v2.h` 与 `aiv_interface/`、`kernel/` 目录，了解 AIV kernel 侧的组织方式（本讲只覆盖了 host 侧）。
- 若想弄清 A/B/C 系数如何被消费，提前浏览 u8-l2 的 `CostModelManager` 与 `CostTableManager`，并对照本讲 4.4 节的系数推导。

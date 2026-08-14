# AICPU 模板与 Kernel 下发

## 1. 本讲目标

本讲进入 HCCL 三大通信引擎中的 **AICPU_TS 引擎**，聚焦「模板如何把一次集合通信拆成一串数据搬移动作，再以 **Task 描述符**的形式下发」。

读完本讲，你应当能够：

- 说清 AICPU_TS 引擎「通过 Task 描述符下发、不占计算核」的工作方式，并把架构简介里的四步调度（Host 提交 kernel → TS 调度 → AICPU 下发 Task → TS 执行）逐一对应到源码。
- 掌握 `load_kernel` 如何一次性加载 AICPU 通信 kernel 二进制，以及 `kernel_launch` 里的 `HcclLaunchAicpuKernel` 如何作为「运行在 AICPU 上的 kernel 体」组织算子展开。
- 读懂一个具体 AICPU 模板 `InsTempAllReduceNHR`：它的资源计算（`CalcRes`）与执行（`KernelRun`）如何把一次 AllReduce 拆成 ReduceScatter + AllGather，并通过数据面原语产出 Task 描述符。

## 2. 前置知识

本讲建立在 [u3-l5 算法模板 Template] 的认知之上，假定你已经知道：

- **模板（Template）** 是「Selector → Executor → Template」链路的最后一环，按「算法 × 引擎」下发最底层数据搬移指令。模板生命周期分两阶段：host 阶段 `CalcRes` 算资源（控制面），device 阶段 `KernelRun` 真正搬数据（数据面）。
- AICPU 模板走 **编译期绑定**：经 `REGISTER_EXEC_V2` 把 template 作为类型参数烘进 executor，运行时不一定有字符串注册名。
- HCCL 的数据搬移最终落到 **HCOMM 基础通信原语**（Write/Read/Reduce/Notify），跨仓调用统一走 `src/common/hcomm_dlsym/`。

本讲会反复用到架构简介里 AICPU_TS 引擎的 **四步调度模型**，先把它的结论摆出来：

1. Host 提交 AICPU Kernel 至任务队列；
2. TS 调度器将 AICPU Kernel 分发至 AICPU 执行；
3. AICPU 提交通信 Task 描述符至 TS 队列；
4. TS 调度器将通信 Task 分发至执行器。

> 一句话直觉：AICPU_TS 引擎里，**AICPU 上运行的代码本身不是在「算」数据，而是在「填写并投递」数据搬移工单（Task 描述符）**，真正搬数据的是 TS 调度起来的硬件执行器。这就是「不占计算核」的由来。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/ops/op_common/template/aicpu/load_kernel.h/.cc` | 加载 AICPU 通信 kernel 二进制，产出全局句柄 `g_binKernelHandle` |
| `src/ops/op_common/template/aicpu/kernel_launch.h/.cc` | AICPU kernel 入口 `HcclLaunchAicpuKernel`：在 AICPU 上展开算子、下发 Task 描述符，并集成 AICPU Task Cache |
| `src/ops/op_common/template/wrapper/alg_data_trans_wrapper.h/.cc` | 数据面搬移封装：把切片组装成 `HcclHcommBatchTransferDesc` 描述符并经 HCOMM 原语下发（Task 描述符的真正生产者） |
| `src/ops/all_reduce/template/aicpu/ins_temp_all_reduce_nhr.h/.cc` | 具体模板：NHR 算法的 AllReduce，拆成 ReduceScatter + AllGather |
| `src/ops/op_common/op_common.cc` | host 侧 launcher `AicpuKernelLaunch`：用 `g_binKernelHandle` 把 kernel 提交到 stream（对应调度第 1 步） |
| `docs/zh/architecture/architecture-brief.md` | AICPU_TS 四步调度的权威说明（§2.4.1） |

## 4. 核心概念与源码讲解

### 4.1 AICPU Kernel 的加载（load_kernel）

#### 4.1.1 概念说明

AICPU_TS 引擎的「kernel」是一段 **预编译好的二进制程序**，它运行在 NPU 的 AICPU 上（不是 Host CPU，也不是 AI 计算核）。HCCL 把这套通信逻辑编译成一个二进制，运行时通过 CANN ACL 运行时接口把它加载进设备，得到一个句柄 `g_binKernelHandle`；后续所有「提交 AICPU kernel 到 stream」的操作都用这个句柄按 **函数名** 取出对应入口。

要点：

- 这个二进制当前是 `libscatter_aicpu_kernel.json`（历史命名，描述 kernel 配置），由 `ASCEND_HOME_PATH` 指向的 CANN 安装目录提供。
- 加载是 **进程级一次性** 的，靠 `g_binKernelHandle != nullptr` 做幂等守卫；没有卸载点。
- 加载发生在 host 侧，加载结果供 host 侧的 launcher 使用。

#### 4.1.2 核心流程

```text
GetKernelFilePath()                 # 读 ASCEND_HOME_PATH，拼出 .../aicpu/config/ 目录
        |
        v
LoadAICPUKernel()                   # 幂等守卫：已加载则直接返回
        |
        v
LoadBinaryFromFile(jsonPath,        # 经 ACL 运行时把二进制加载进设备
    ACL_RT_BINARY_LOAD_OPT_CPU_KERNEL_MODE, ...)
        |
        v
g_binKernelHandle                   # 全局句柄，供 AicpuKernelLaunch 按 funcName 取函数
```

#### 4.1.3 源码精读

头文件只暴露一个加载函数和一个全局句柄：

[src/ops/op_common/template/aicpu/load_kernel.h:L18-L19](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/template/aicpu/load_kernel.h#L18-L19) —— 声明 `LoadAICPUKernel` 与全局句柄 `g_binKernelHandle`，外部经此句柄按函数名取出 kernel 入口。

`GetKernelFilePath` 解析安装目录，未设置环境变量时回退到默认路径并告警：

[src/ops/op_common/template/aicpu/load_kernel.cc:L18-L35](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/template/aicpu/load_kernel.cc#L18-L35) —— 用 `std::getenv("ASCEND_HOME_PATH")` 定位 CANN 目录，拼出 `.../opp/built-in/op_impl/aicpu/config/`。

`LoadAICPUKernel` 是加载主体，关键是幂等守卫与一次性加载：

[src/ops/op_common/template/aicpu/load_kernel.cc:L38-L56](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/template/aicpu/load_kernel.cc#L38-L56) —— `g_binKernelHandle != nullptr` 直接返回；否则拼出 `libscatter_aicpu_kernel.json`，调 `LoadBinaryFromFile(..., ACL_RT_BINARY_LOAD_OPT_CPU_KERNEL_MODE, 0, g_binKernelHandle)`，失败用 `CHK_PRT_RET` 上报并返回错误码。

加载得到的句柄在 host 侧 launcher 中被使用（对应四步调度的第 1 步）：

[src/ops/op_common/op_common.cc:L948-L962](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L948-L962) —— `AicpuKernelLaunch` 用 `aclrtBinaryGetFunction(g_binKernelHandle, "HcclLaunchAicpuKernel", &funcHandle)` 取出 kernel 函数句柄，随后把它提交到 stream（见 4.2）。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：确认「kernel 二进制只加载一次，且按函数名复用」。
2. **步骤**：
   - 在仓库中搜索 `g_binKernelHandle` 的全部使用点。
   - 搜索 `LoadAICPUKernel(` 的调用方，确认它在算子主链路里被调用一次。
   - 阅读 `load_kernel.cc` 的幂等守卫与注释「当前不提供卸载能力」。
3. **观察**：`LoadAICPUKernel` 如何在重复调用时直接返回；`g_binKernelHandle` 如何被 `AicpuKernelLaunch` 用 `aclrtBinaryGetFunction` 按名取函数。
4. **预期结果**：你应能画出「加载（一次）→ 持有句柄 → 按名取函数 → 提交到 stream」这条链。
5. 运行结果：待本地验证（上板需 NPU 与驱动固件）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `LoadAICPUKernel` 要用 `g_binKernelHandle != nullptr` 做幂等守卫，而不是每次算子调用都重新加载？

**参考答案**：把二进制加载进设备是昂贵操作，且同一进程内通信 kernel 二进制不变；加载一次、全局复用句柄可避免重复开销。源码注释也明确「当前不提供卸载能力，流程上没有点可以卸载」。

**练习 2**：如果用户没有设置 `ASCEND_HOME_PATH`，加载流程会怎样？

**参考答案**：`GetKernelFilePath` 回退到默认路径 `/usr/local/Ascend/cann/` 并打一条 `HCCL_WARNING`，随后照常拼接并尝试加载；若该默认路径下无二进制则 `LoadBinaryFromFile` 失败并报错。

---

### 4.2 Task 描述符的下发（kernel_launch）

#### 4.2.1 概念说明

`HcclLaunchAicpuKernel` 是 AICPU_TS 引擎最核心的函数——**它本身就是运行在 AICPU 上的 kernel 体**。host 侧的 `AicpuKernelLaunch` 用 4.1 加载到的句柄，把这个 kernel 提交到 stream（调度第 1 步）；TS 调度器把它分发到 AICPU 执行（第 2 步）；它在 AICPU 上运行时，组织「算子展开（Orchestrate）」，展开过程中由模板调用数据面原语，产出通信 **Task 描述符** 投递到 TS 队列（第 3 步）；最后 TS 把这些 Task 分发给硬件执行器（第 4 步）。

理解本节要抓住三个要点：

1. **它在 AICPU 上执行，而不是 host 上。** 所以函数签名是 `extern "C" unsigned int HcclLaunchAicpuKernel(OpParam* param)`——`param` 是 host 把整份 `OpParam`（含序列化的资源上下文）作为 kernel 参数搬进 AICPU 的。
2. **它不直接搬数据，而是「展开算子」。** 展开的结果是一串数据面调用（Task 描述符），由 HCOMM 基础通信层和 TS 真正执行。
3. **它有新/老两套流程与一条 Task Cache 快速路径。** `IsOpsV2` 决定走新流程（`CollAlgExecRegistryV2`）还是 legacy 流程；A5（950）等设备还可在使能时直接回放缓存的 Task 序列，跳过重新展开。

> 术语：**算子展开（Orchestrate）**——把一次集合通信算子「展开」成一串有序的数据搬移 + 同步动作。展开动作本身在 AICPU 上发生，展开产出的 Task 才是真正在硬件上跑的。

#### 4.2.2 核心流程

AICPU_TS 四步调度与源码的对应关系：

```text
第1步 Host 提交 AICPU Kernel 到 stream
        └─ host 侧 AicpuKernelLaunch: aclrtLaunchKernelWithConfig(HcclLaunchAicpuKernel, stream)   # op_common.cc

第2步 TS 把 AICPU Kernel 分发到 AICPU 执行
        └─ HcclLaunchAicpuKernel(param) 开始在 AICPU 上运行                                       # kernel_launch.cc L342

第3步 AICPU 提交 Task 描述符到 TS 队列
        ├─ 反序列化资源上下文 resCtx（含 thread/channel/notify）
        ├─ OpOrchestrate: 按 algName 取 executor, 调 executor->Orchestrate
        │       └─ executor 依次调 template->KernelRun
        │              └─ 数据面原语 (LocalCopy / SendRecvBatchWrite / ...) 产出 Task 描述符  # 4.3
        └─ （使能时）整串 Task 被 AICPU Task Cache 缓存，便于下次回放

第4步 TS 把 Task 分发给硬件执行器
        └─ 由 HCOMM/TS 完成（不在本仓源码内，HCCL 只负责投递描述符）
```

入口内部还有一层重要的分发：`IsOpsV2(algName, deviceType)`。algName 以 `"opv2_"` 开头，或设备属于 OutPlace 新流程（A5/950 等），走新流程 `CollAlgExecRegistryV2`；否则走 legacy `CollAlgExecRegistry`。本讲聚焦新流程。

资源上下文（`AlgResourceCtxSerializable`）的来源也值得注意：host 在资源计算阶段把它 **序列化** 进 `param->resCtx`，AICPU kernel 启动后用 `DeserializeResCtx` 还原；为降低反序列化开销，`kernel_launch.cc` 内置了一个按通信域 + algTag 的 `CommDomainCacheManager` 缓存。

#### 4.2.3 源码精读

**入口与幂等环境**——`HcclLaunchAicpuKernel` 改调度策略为 `SCHED_OTHER`、acquire 通信域、按序下发首条 Notify：

[src/ops/op_common/template/aicpu/kernel_launch.cc:L342-L362](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/template/aicpu/kernel_launch.cc#L342-L362) —— kernel 入口：设调度优先级、判空、`HcommAcquireComm`、`HcclOrderLaunchNotifyRecord`。

**新/老流程分发**——`IsOpsV2` 决定走哪条路：

[src/ops/op_common/template/aicpu/kernel_launch.cc:L242-L258](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/template/aicpu/kernel_launch.cc#L242-L258) —— algName 前缀 `"opv2_"` 或 `shouldGoOutPlace(deviceType)` 为真则走 V2 新流程，否则走 legacy。

**资源上下文反序列化与缓存**（新流程核心，第 3 步的准备）：

[src/ops/op_common/template/aicpu/kernel_launch.cc:L413-L451](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/template/aicpu/kernel_launch.cc#L413-L451) —— 先查 `g_cacheManager`，命中且 `IsResCtxCacheReusable`（同通信域句柄）则直接复用；未命中则 `DeserializeResCtx(param)` 还原并 `Put` 回缓存。`DeserializeResCtx` 从 `param->resCtx` 的原始字节反序列化出 `AlgResourceCtxSerializable`。

**算子展开 OpOrchestrate**——这是「下发 Task 描述符」的发动机：

[src/ops/op_common/template/aicpu/kernel_launch.cc:L274-L321](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/template/aicpu/kernel_launch.cc#L274-L321) —— 设置 RTSQ/NotifyWait 超时、主 thread 等 host stream 的通知，然后 `CollAlgExecRegistryV2::Instance().GetAlgExec(opType, algName)` 取 executor，调 `executor->Orchestrate(*param, *resCtxPtr)`。`Orchestrate` 内部依次调用 template 的 `KernelRun`（见 4.3），后者通过数据面原语产出 Task 描述符。

**不使能 Task Cache 时的主路径**——最朴素的「展开即下发」：

[src/ops/op_common/template/aicpu/kernel_launch.cc:L592-L595](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/template/aicpu/kernel_launch.cc#L592-L595) —— `else` 分支直接 `OpOrchestrate(...)`，不经过缓存。

**Task Cache 命中/未命中**——使能时（`AicpuTaskCachePolicy::IsAicpuTaskCacheEnable`）的快速路径：

[src/ops/op_common/template/aicpu/kernel_launch.cc:L515-L591](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/template/aicpu/kernel_launch.cc#L515-L591) —— 未命中：`HcommAicpuTsTaskCacheStart` 通知开始缓存 → `OpOrchestrate` 展开并投递描述符 → `EnforceLaunchTask` 强制把展开相关 SQE 经 `LaunchTask` 落入缓存 → `HcommAicpuTsTaskCacheEnd`；命中：直接 `HcommAicpuTsTaskCacheExecute` 刷新并下发已缓存的 Task 序列。Task Cache 的细节是下一讲 [u5-l2] 的主题，这里只需知道它是「跳过重新展开、回放 Task 序列」的优化。

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：把 AICPU_TS 四步调度逐一对到源码行。
2. **步骤**：
   - 第 1 步：读 `op_common.cc` 的 `AicpuKernelLaunch`，找到 `aclrtBinaryGetFunction` 与 `aclrtLaunchKernelWithConfig`（约 L955、L1006 附近）。
   - 第 2 步：跳到 `kernel_launch.cc` 的 `HcclLaunchAicpuKernel` 入口（L342）。
   - 第 3 步：沿 `OpOrchestrate`（L274）→ `executor->Orchestrate` → template `KernelRun` 跟到 4.3 的 `InsTempAllReduceNHR::KernelRun`。
   - 第 4 步：注意源码注释明确「TS 执行」由 HCOMM/TS 完成，本仓只投递描述符。
3. **观察**：`param` 如何作为 kernel 参数从 host 搬到 AICPU；`resCtx` 如何序列化/反序列化；展开动作如何产生 Task。
4. **预期结果**：你能用一句话指出每一步对应的函数与文件。
5. 运行结果：待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`HcclLaunchAicpuKernel` 为什么需要 `HcommAcquireComm` / `HcommReleaseComm` 这一对调用？

**参考答案**：它在 AICPU 上异步执行，必须保证执行期间通信域对象存活；`AcquireComm` 增加引用、`ReleaseComm` 释放引用，保证生命周期安全（与 P2P 入口 `HcclLaunchP2pAicpuKernel` 的做法一致）。

**练习 2**：`IsOpsV2` 的判定条件有哪两个？为什么需要这套分发？

**参考答案**：algName 以 `"opv2_"` 开头，或 `shouldGoOutPlace(deviceType)` 为真（A5/950 等新设备走 OutPlace 新流程）。新/老流程分别查 `CollAlgExecRegistryV2` 与 `CollAlgExecRegistry`，资源上下文结构也不同（`AlgResourceCtxSerializable` vs `AlgResourceCtx`），故需分发。这是「legacy 不持续演进」约束的直接体现。

---

### 4.3 NHR 模板：把 AllReduce 展开成 Task 描述符（ins_temp_all_reduce_nhr）

#### 4.3.1 概念说明

`InsTempAllReduceNHR` 是一个具体的 AICPU 模板，algName 为 `AicpuAllReduceSoleNHR`（由 selector 产出，见 `all_reduce_auto_selector.cc`）。NHR（Nonuniform Hierarchical Ring）本质是 **递归减半（Recursive Halving）** 模式，属于「两步走（two-shot）」算法：先用 ReduceScatter 把每张卡的一份数据规约到对应卡上，再用 AllGather 把结果广播给所有卡，从而实现 AllReduce。

它的资源计算与执行分别落在模板生命周期的两个阶段：

- **`CalcRes`（host 控制面阶段）**：根据拓扑算出需要多少 channel、thread、notify，填进 `AlgResourceRequest`。
- **`KernelRun`（device 数据面阶段，运行在 AICPU 上）**：把 `PreCopy → (ReduceScatter + AllGather) → PostCopy` 的一串动作展开成数据面原语调用，每个原语产出一批 Task 描述符。

NHR 的通信轮数取决于 rank 数。设子通信域规模为 \(R\)，则通信步数为：

\[ nSteps = \lceil \log_2 R \rceil \]

每步收/发的数据份数逐轮减半，总份数恰为 \(R-1\)，这正是「递归减半」的特征。

#### 4.3.2 核心流程

`KernelRun` 的总体结构（已省略多 thread 同步细节）：

```text
KernelRun(param, tempAlgParams, templateResource):
    1. 取 rankList、本 rank 序号 myRankIdx
    2. 切片：count 按字节切成 R 份（前 R-1 份等长 sliceSize_，末份 tailSize_）
    3. PrepareDataSplitForMultiChannel  # 按多 channel/端口组再细分
    4. PreCopy        # LocalCopy: inputPtr -> hcclBuffer        （产出本地搬移 Task）
    5. for channel in channelsPerRank:
           RunReduceScatter(channel)   # 逐轮 SendRecv[BatchWrite]Reduce  （产出远端搬移+规约 Task）
           RunAllGather(channel)       # 逐轮 SendRecv[BatchWrite]        （产出远端搬移 Task）
    6. PostCopy       # LocalCopy: hcclBuffer -> outputPtr        （产出本地搬移 Task）
```

数据面原语如何变成 Task 描述符？以批量写为例，`RunBatchTransfer` 把每个 `DataSlice` 转成一个 `HcclHcommBatchTransferDesc`（含 transType、dst、src、len/归约信息），收集到一批后调 `HcclHcommBatchTransferOnThread(thread, channel.handle, descs, n)` 一次性投递——这就是「Task 描述符的组装与下发」。

#### 4.3.3 源码精读

**资源计算 `CalcRes`/`GetRes`**——按拓扑算 channel、按 channel 数算 thread/notify：

[src/ops/all_reduce/template/aicpu/ins_temp_all_reduce_nhr.cc:L30-L72](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/template/aicpu/ins_temp_all_reduce_nhr.cc#L30-L72) —— 根据 `level0Topo` 选 `CalcChannelRequestNhrMultiJetty` 或 `CalcChannelRequestNhr` 得到 `level1Channels`；`GetRes` 里 `slaveThreadNum = channelsPerRank_ - 1`、`notifyNumOnMainThread = channelsPerRank_ - 1`，即 thread/notify 数随并发通道数增长。

**`KernelRun` 主干**——PreCopy → ReduceScatter+AllGather → PostCopy：

[src/ops/all_reduce/template/aicpu/ins_temp_all_reduce_nhr.cc:L115-L187](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/template/aicpu/ins_temp_all_reduce_nhr.cc#L115-L187) —— 取 rankList/序号、按字节切片（`sliceSize_ = (count/R)*dataTypeSize`，`tailSize_` 为余数）、`PrepareDataSplitForMultiChannel`、`PreCopy`，随后对每个 channel 依次 `RunReduceScatter` 与 `RunAllGather`，最后 `PostCopy`。

**ReduceScatter 的一轮**——组装收/发切片并调批量写规约原语：

[src/ops/all_reduce/template/aicpu/ins_temp_all_reduce_nhr.cc:L207-L278](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/template/aicpu/ins_temp_all_reduce_nhr.cc#L207-L278) —— 按 `stepInfo`（toRank/fromRank、tx/rx 切片号）为每个切片构造 `DataSlice`，组装 `SendRecvReduceInfo`，依据是否 PCIe（`isDmaRead_`）选 `SendRecvReadReduce` 或 `SendRecvBatchWriteReduce`。这一步把 ReduceScatter 的一轮翻译成一批 Task 描述符。

**通信步数与轮次对端**——递归减半的数学：

[src/ops/all_reduce/template/aicpu/ins_temp_all_reduce_nhr.cc:L451-L459](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/template/aicpu/ins_temp_all_reduce_nhr.cc#L451-L459) —— `GetNHRStepNum` 对 `R-1` 不断右移计数，得到 \( \lceil \log_2 R \rceil \)。

[src/ops/all_reduce/template/aicpu/ins_temp_all_reduce_nhr.cc:L367-L407](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/template/aicpu/ins_temp_all_reduce_nhr.cc#L367-L407) —— 每步 `deltaRank = 1<<step`，对端 `sendToIdx = (myRankIdx + R - deltaRank) % R`、`recvFromIdx = (myRankIdx + deltaRank) % R`，份数 `nSlices = (R-1+(1<<step)) / (1<<(step+1))`——经典的递归减半配对。

**数据面原语如何产 Task 描述符**——这是 Task 下发的真正落点：

[src/ops/op_common/template/wrapper/alg_data_trans_wrapper.cc:L135-L163](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/template/wrapper/alg_data_trans_wrapper.cc#L135-L163) —— `RunBatchTransfer` 把每个切片经 `processSlice` 转成一个 `HcclHcommBatchTransferDesc`，收集成批后调 `HcclHcommBatchTransferOnThread(thread, channel.handle, descs.data(), n)` 一次性投递（即把 Task 描述符交由 HCOMM/TS 执行）。

[src/ops/op_common/template/wrapper/alg_data_trans_wrapper.cc:L481-L514](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/template/wrapper/alg_data_trans_wrapper.cc#L481-L514) —— `SendRecvBatchWriteReduce` 用 `MakeBatchReduceDesc(WRITE_REDUCE, dst, src, count, dataType, reduceOp)` 构造带规约语义的描述符。

[src/ops/op_common/template/wrapper/alg_data_trans_wrapper.cc:L880-L899](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/template/wrapper/alg_data_trans_wrapper.cc#L880-L899) —— `LocalCopy`（用于 PreCopy/PostCopy）调 `HcommLocalCopyOnThread(thread, dst, src, size)`，是一条本地搬移 Task。

> 小结这一节的数据流：`KernelRun` → `RunReduceScatter`/`RunAllGather` → `SendRecvBatchWrite[Reduce]` → `RunBatchTransfer` → `HcclHcommBatchTransferOnThread`（HCOMM 原语）。HCCL 到此为止，后续由 HCOMM + TS 把描述符调度到硬件执行（第 4 步）。

#### 4.3.4 代码实践（源码阅读型）

1. **目标**：跟踪一次 NHR ReduceScatter 一轮如何变成一批 Task 描述符。
2. **步骤**：
   - 从 `ins_temp_all_reduce_nhr.cc::KernelRun` 进入 `RunReduceScatter`。
   - 在 `RunReduceScatter` 中找到 `stepInfo` 循环与 `SendRecvBatchWriteReduce` 调用。
   - 跳到 `alg_data_trans_wrapper.cc::SendRecvBatchWriteReduce` → `DoSendRecvBatchTx` → `RunBatchTransfer`，定位 `MakeBatchReduceDesc` 与 `HcclHcommBatchTransferOnThread`。
3. **观察**：每个 `DataSlice` 如何变成一个 `HcclHcommBatchTransferDesc`；多个描述符如何被收集成一批后一次性投递；notify 如何被 `FuseNotifyToLastWriteReduceDesc` 融合到最后一条写规约上。
4. **预期结果**：你能解释「ReduceScatter 的一轮 = 一批 WRITE_REDUCE 描述符 + 一条 notify」。
5. 运行结果：待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：8 卡 NHR AllReduce 需要几轮 ReduceScatter？每轮分别发/收哪几张卡？

**参考答案**：\( nSteps = \lceil \log_2 8 \rceil = 3 \) 轮。对 rank \(i\)，第 step 轮 `deltaRank = 1<<step`，发给 `(i + 8 − deltaRank) % 8`、收自 `(i + deltaRank) % 8`。即第 0 轮跨度 1（相邻卡）、第 1 轮跨度 2、第 2 轮跨度 4。

**练习 2**：为什么 `CalcScratchMultiple` 返回 1？这意味 NHR 需要多大的 scratch buffer？

**参考答案**：NHR 在原地（in-place 风格）的 hcclBuffer 上做收发与规约，不额外放大缓冲，scratch 倍数为 1（=单份数据大小）。这与 one-shot Mesh（倍数为 R）不同，是 NHR 节省显存的体现（代价是多轮通信）。

**练习 3**：`isDmaRead_` 由什么决定？它如何改变 ReduceScatter 的下发方式？

**参考答案**：由 `IsPcieProtocol(channels)` 判断是否存在 PCIe 链路；为真时用 Read 模式（`SendRecvReadReduce`，远端读），否则用 Write 模式（`SendRecvBatchWriteReduce`，远端写）。两者产出不同 transType 的 Task 描述符（`READ_REDUCE` vs `WRITE_REDUCE`）。

## 5. 综合实践

**任务**：对照架构简介的 AICPU_TS 四步调度，在源码中找到每一步对应的代码阶段，并说明 Task 描述符是如何被组装与下发的。产出一张「四步 → 文件:函数 → 行号 → 一句话说明」的对照表。

参考答案骨架（请到源码中逐行复核）：

| 调度步骤 | 文件:函数 | 行号 | 说明 |
|---------|----------|------|------|
| 1. Host 提交 kernel | `op_common.cc::AicpuKernelLaunch` | L948–L1006 起 | 用 `g_binKernelHandle` 经 `aclrtBinaryGetFunction` 取 `HcclLaunchAicpuKernel`，`aclrtLaunchKernelWithConfig` 提交到 stream |
| 2. TS 分发到 AICPU | `kernel_launch.cc::HcclLaunchAicpuKernel` | L342 起 | kernel 体在 AICPU 上开始执行：acquire 通信域、`IsOpsV2` 分发 |
| 3. AICPU 下发 Task | `kernel_launch.cc::OpOrchestrate` → `executor->Orchestrate` → `ins_temp_all_reduce_nhr.cc::KernelRun` → `alg_data_trans_wrapper.cc::RunBatchTransfer` | L274；L115；L135 | 展开 = 调数据面原语，把切片组装成 `HcclHcommBatchTransferDesc`，经 `HcclHcommBatchTransferOnThread` 投递 |
| 4. TS 执行 Task | （HCOMM/TS，不在本仓） | — | HCCL 只投递描述符，执行由基础通信层与 TS 完成 |

**进阶**：把上表中第 3 步展开成更细的链路图：`KernelRun → PreCopy(LocalCopy) → RunReduceScatter(SendRecvBatchWriteReduce) → RunAllGather(SendRecvBatchWrite) → PostCopy(LocalCopy)`，并标注每一步产出的是 `LOCAL_COPY`、`WRITE_REDUCE` 还是 `WRITE` 类型的 Task 描述符。

完成后建议：开启 `HCCL_DEBUG_CONFIG` 相关日志位（参考 [u4-l3] 环境变量），上板跑一次 8 卡 AllReduce，在日志里搜索 `SendRecvBatchWriteReduce` / `HcclLaunchAicpuKernel` 等关键字，验证你对「展开 → 投递」的理解（运行结果待本地验证）。

## 6. 本讲小结

- AICPU_TS 引擎的 kernel 是预编译二进制，由 `LoadAICPUKernel` 一次性加载为全局句柄 `g_binKernelHandle`，host 侧 launcher 按函数名复用——这是调度第 1 步的前提。
- `HcclLaunchAicpuKernel` 是 **运行在 AICPU 上的 kernel 体**，它把 host 传来的 `OpParam`（含序列化资源上下文）反序列化，按 `algName` 取 executor 并 `Orchestrate` 展开——「展开」就是「下发 Task 描述符」。
- Task 描述符的真正生产者是模板的 `KernelRun` 经数据面原语完成的：切片被组装成 `HcclHcommBatchTransferDesc`，成批地经 HCOMM 原语（如 `HcclHcommBatchTransferOnThread`）投递；真正执行由 HCOMM + TS 完成（第 4 步不在本仓）。
- NHR 模板把 AllReduce 展开为 `PreCopy → ReduceScatter → AllGather → PostCopy`，通信轮数 \( \lceil \log_2 R \rceil \)，是「递归减半」的典型实现。
- `IsOpsV2` 与 AICPU Task Cache 是两条重要旁路：前者在新/老流程间分发，后者（使能时）跳过重新展开、直接回放已缓存的 Task 序列。

## 7. 下一步学习建议

- 紧接 [u5-l2 AICPU Task Cache]：深入 `AicpuTaskCacheKey`/`AicpuTaskCachePolicy`/`AicpuTaskCacheCommManager`，弄清本讲 `enableCache` 分支里 `Start→OpOrchestrate→EnforceLaunchTask→End` 与命中时 `Execute` 的完整回放机制。
- 对比另外两类引擎模板：[u5-l3 AIV 模板]（Vector Core 直接执行、低延迟、占核）与 [u5-l4 CCU 模板]（Mission + URMA 硬化执行），理解为何三者分别适用于小数据低延迟、硬化加速与大数据高带宽。
- 回到 [u6-l2 资源、原语与拓扑的 dlsym 封装]：把本讲里出现的 `HcclHcommBatchTransferOnThread`/`HcommLocalCopyOnThread` 等 HCOMM 原语跟 `hcomm_primitives_dl` 的 dlsym 封装对应起来，打通「HCCL 模板 → dlsym → HCOMM 基础通信」的全链路。

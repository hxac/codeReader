# Tiling 调度与多流执行

## 1. 本讲目标

本讲是「工程化、调试与贡献」单元的第一篇，聚焦 ATB 运行时性能优化的三大进阶手段：**异步 Tiling 拷贝**、**多流执行（图内多流 / 图间多图同步）**、**两段式下发**。

学完本讲，你应当能够：

- 说清楚为什么 Tiling 数据要先在 Host 生成、再拷贝到 Device，以及如何用一条独立的「拷贝流 + Event」把这次拷贝从执行流上剥离出去。
- 掌握 `SetExecuteStreams` + `SetExecuteStreamId` 的「逻辑流号 → 物理流」路由机制，能看懂图算子如何在多条 NPU 流上并行。
- 看懂官方 `multiStream` demo 如何用 `EventParam`（RECORD/WAIT）在两个独立图算子之间做回合制同步。
- 理解 `EXECUTE_PRELAUNCH` / `EXECUTE_LAUNCH` 把一次 `Execute` 拆成两段的意义，以及它如何让相邻算子的 Host 工作与 Device 执行重叠。

> 前置依赖：本讲假设你已学过 **u1-l5（Context 上下文与执行流管理）** 与 **u3-l5（Context 资源池管理）**，了解 TilingBufferPool、Allocator、ExecuteType/LaunchMode 枚举的存在，以及 Operation 的「Setup → Execute」两段式生命周期。

## 2. 前置知识

在进入源码前，先用三段话把背景补齐。

**Tiling 是什么、为什么要拷贝。** 在昇腾（Ascend）平台上，一个 Kernel 真正在 NPU AI Core 上跑之前，Host（CPU）必须先做一次「Tiling」：根据输入形状、dtype、可用核数，把总工作量切成若干块，并算出每块的起止下标、循环次数等调度参数。这些参数被打包成一个 `TilingData` 结构体（见 u3-l4）。Tiling 在 Host 上生成，但 Kernel 在 Device 上读，于是每个算子执行前都必须有一次 `Host → Device` 的 Tiling 拷贝。当算子极多、单算子计算量又不大时，这次拷贝会成为 Host 下发开销的一部分（即 u1-l1 讲过的 Host Bound）。

**ACL 的 Stream 与 Event。** ACL（Ascend Computing Language）把 Device 上的任务组织成「流（`aclrtStream`）」：同一个流里的任务严格按顺序执行，不同流之间的任务可以并行。要让 A 流等 B 流的某个完成点，就用「事件（`aclrtEvent`）」：B 流 `aclrtRecordEvent` 打一个标记，A 流 `aclrtStreamWaitEvent` 阻塞直到该标记完成。这是本讲所有同步逻辑的地基。

**Host 工作与 Device 执行的重叠。** 一次 `Operation::Execute` 内部其实分两件事：① Host 侧的准备工作（校验、形状、Tiling、组 args），② Device 侧真正的 Kernel 下发。常规模式下二者在调用线程里串成一条。如果能让「算子 N 在 Device 上跑」与「算子 N+1 在 Host 上做准备」同时进行，就能把 Host 时间藏进 Device 时间里——这正是两段式下发（4.4 节）与异步 Tiling（4.1 节）的共同目标。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/atb/context.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/context.h) | Context 公开抽象类，声明 `ExecuteType`/`LaunchMode` 枚举、`SetExecuteStreams`/`SetAsyncTilingCopyStatus`/`SetExecuteType` 等接口。 |
| [src/atb/context/context_base.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.h) | Context 的内部实现，持有执行流数组、异步拷贝流、事件数组、`thread_local executeType_` 等成员。 |
| [src/atb/context/context_base.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.cpp) | 异步 Tiling 拷贝流/事件的创建与销毁、多流设置、`SetExecuteType`/`SetLaunchMode` 的实现。 |
| [src/atb/context/tiling_buffer_pool/tiling_buffer_pool.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/tiling_buffer_pool/tiling_buffer_pool.h) | TilingBufferPool 环形缓冲池，Host/Device 各一个，是 Tiling 数据的落脚点。 |
| [src/atb/operation/operation_base.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp) | `CopyTilingToDevice`（异步拷贝的发生地）、`Execute`（两段式分发的入口）、`GetExecuteStream`（streamId 路由）。 |
| [src/atb/operation/operation.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation.cpp) | 全局 `SetExecuteStreamId`，两级 `dynamic_cast` 把逻辑流号写到算子上。 |
| [src/atb/runner/runner.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/runner.cpp) | Runner 按 streamId 切分共享 workspace（`ChangeWorkspaceBufferByExecuteStream`）。 |
| [src/atb/runner/graph_runner.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/graph_runner.cpp) | 图算子逐节点 PreExecute，以及「非主流的张量不参与内存复用」的规则。 |
| [include/atb/common_op_params.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/common_op_params.h) | `EventParam` 结构体（RECORD/WAIT），图间同步的原语。 |
| [example/multiStream/multiStream_singleGraph_demo.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/multiStream/multiStream_singleGraph_demo.cpp) | **图内多流并行** demo：一张图里部分节点走 streamId=1。 |
| [example/multiStream/multiStream_multiGraph_demo.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/multiStream/multiStream_multiGraph_demo.cpp) | **图间同步** demo：两个图算子各绑一条流，用 Event 做回合制同步。 |

## 4. 核心概念与源码讲解

### 4.1 异步 Tiling 拷贝：把 Host→Device 的搬运从执行流上剥离

#### 4.1.1 概念说明

回顾 u3-l5：Context 内部有两个 `TilingBufferPool`——一个在 Host（用 `malloc`），一个在 Device（用 `aclrtMalloc`）。Setup 阶段，Tiling 数据先写进 **Host 池**的一块；Execute 阶段，必须把它拷到 **Device 池**的对应块，Kernel 才能读到。

关键问题是：**这次拷贝放在哪条流上？**

- **默认（同步）模式**：拷贝直接放在「执行流」上。那么执行流上会出现 `拷贝Tiling → 跑Kernel → 拷贝Tiling → 跑Kernel …` 的串行序列，Host 端发拷贝指令、Device 端排队等拷贝完才能跑 Kernel，二者互相阻塞。
- **异步模式**：单独建一条「拷贝流」专门做 Tiling 拷贝，再用 Event 告诉执行流「等这块拷贝完成再跑 Kernel」。于是拷贝可以与执行流上**前一个** Kernel 的计算重叠，把搬运时间藏起来。

开关就是 [context.h:L92](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/context.h#L92) 的 `SetAsyncTilingCopyStatus(bool)`。

#### 4.1.2 核心流程

异步拷贝的核心是一个经典的「生产者—消费者」事件同步，四个 ACL 调用一气呵成：

```
拷贝流(copyStream)         执行流(executeStream)
─────────────────         ─────────────────────
1. CopyHostTilingToDevice  (前一个 Kernel 还在跑…)
2. RecordEvent(E, copy)
                           3. StreamWaitEvent(E)   ← 阻塞，直到 E 完成
                           4. ResetEvent(E, execute)
                           5. LaunchKernel         ← 此时 Tiling 已就位
```

为什么需要 `ResetEvent`？因为同一个 Event 对象会被**循环复用**（见 4.1.3 的轮转数组）。重置后，下次 `RecordEvent` 才能重新代表「新的一次拷贝完成」，避免执行流误等老标记。

为了让「拷贝」与「计算」真正错开多拍，Context 预置了 `MAX_COPY_EVENT_NUM = 10` 个 Event 轮转使用——本质是一个深度为 10 的流水线，允许至多 10 次 Tiling 拷贝在拷贝流上「在途」而不必立刻被消费。用流水线占用度衡量：

\[
\text{吞吐提升上限} \approx \min\!\left(1,\ \frac{T_{\text{copy}}}{T_{\text{kernel}}}\right)^{-1}
\]

即当拷贝时间小于 Kernel 时间时，异步化可把拷贝完全藏进计算里。

#### 4.1.3 源码精读

**① 开关与资源创建。** [context_base.cpp:L128-L143](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.cpp#L128-L143) 的 `SetAsyncTilingCopyStatus` 是幂等开关：传入 `true` 时若尚未创建则调用 `CreateCopyStreamAndEvents`，传入 `false` 时销毁。

`CreateCopyStreamAndEvents`（[context_base.cpp:L198-L221](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.cpp#L198-L221)）建一条拷贝流 + 10 个 Event：

```cpp
aclError ret = aclrtCreateStream(&asyncTilingCopyStream_);          // 拷贝流
...
asyncTilingCopyEvents_.resize(MAX_COPY_EVENT_NUM);                  // MAX_COPY_EVENT_NUM = 10
for (...) {
    ret = aclrtCreateEvent(&asyncTilingCopyEvents_.at(i));          // 10 个复用 Event
}
```

常量定义在同文件顶部（[context_base.cpp:L26-L28](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.cpp#L26-L28)）。

**② Event 的轮转发放。** [context_base.cpp:L157-L171](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.cpp#L157-L171) 的 `GetAsyncTilingCopyEvent` 每次返回当前下标的 Event 并把下标 `+1`，到尾绕回——这就是「环形复用 10 个 Event」的实现。

**③ 拷贝发生地：四个 ACL 调用。** 真正的异步拷贝在 [operation_base.cpp:L716-L749](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L716-L749) 的 `CopyTilingToDevice`，这正是 4.1.2 流程图的源码落地：

```cpp
aclrtStream executeStream = GetExecuteStream(runnerVariantPack_.context);
aclrtStream tilingCopyStream = contextBase->GetAsyncTilingCopyStream();
aclrtEvent tilingCopyEvent = nullptr;
if (tilingCopyStream) {
    tilingCopyEvent = contextBase->GetAsyncTilingCopyEvent();   // 取一个轮转 Event
}
if (tilingCopyStream && tilingCopyEvent) {
    st = CopyHostTilingToDevice(tilingCopyStream);              // ① 拷贝流上搬运
    st = aclrtRecordEvent(tilingCopyEvent, tilingCopyStream) + st;   // ② 打标记
    st = aclrtStreamWaitEvent(executeStream, tilingCopyEvent) + st;  // ③ 执行流等
    st = aclrtResetEvent(tilingCopyEvent, executeStream) + st;       // ④ 重置供复用
} else {
    st = CopyHostTilingToDevice(executeStream);                 // 未开异步：直接在执行流上拷
}
```

这段代码同时回答了一个常见疑问：**异步拷贝只影响「拷贝放在哪条流」，不改变 TilingBufferPool 的块管理**——Host/Device 池依旧按 `blockIndex` 环绕（见 u3-l5 的 [tiling_buffer_pool.h:L16-L39](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/tiling_buffer_pool/tiling_buffer_pool.h#L16-L39)），异步模式只是把「从 Host 块到 Device 块」的那次 `aclrtMemcpy` 挪了条流。

> 注意一个边界：图模式（`GRAPH_LAUNCH_MODE`）下 Tiling 缓冲改走 Allocator 现场申请而非池（见 [context_base.cpp:L173-L191](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.cpp#L173-L191)），因为整图捕获要求地址稳定，环形池的「绕回」会破坏地址一致性。异步拷贝与图模式是正交的两套机制。

#### 4.1.4 代码实践

**目标：** 用日志验证「开启异步 Tiling 后，确实多了一条拷贝流和 10 个 Event」。

**步骤：**

1. 设置环境变量打开 ATB 的 DEBUG 日志（详见 u7-l2）：`export ATB_LOG_LEVEL=DEBUG`。
2. 参照 [multiStream_singleGraph_demo.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/multiStream/multiStream_singleGraph_demo.cpp) 的 `main`，在 `CreateContext` 之后、`Execute` 之前插入一行：
   ```cpp
   context->SetAsyncTilingCopyStatus(true);
   ```
3. 编译运行（参考 4.3.4 的构建步骤）。

**观察现象：** 日志中应出现 `ContextBase SetAsyncTilingCopyStatus create copy stream and events`（[context_base.cpp:L137](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.cpp#L137)），随后每个算子执行时出现 `tiling copy stream is valid, use it to copy tiling to device`（[operation_base.cpp:L738](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L738)）；若不开启，则走 `use execute stream to copy tiling to device` 分支（[operation_base.cpp:L744](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L744)）。

**预期结果：** 两条日志分支的二选一，正好对应同步/异步两条路径。

> 待本地验证：实际的端到端耗时收益需在真实 NPU 上用 `msprof`（见 demo README）测量，CPU 模拟环境无法体现 Device 并行。

#### 4.1.5 小练习与答案

**练习 1：** 为什么异步拷贝需要 10 个 Event 轮转，而不是 1 个？

**答案：** 1 个 Event 只能表示「最近一次拷贝是否完成」，无法区分多次在途的拷贝。当拷贝流连续发出多次 `RecordEvent` 而执行流尚未 `WaitEvent` 时，需要多个 Event 各自记录不同次拷贝的完成点，否则后一次 Record 会覆盖前一次的语义。10 个构成一个深度为 10 的环形缓冲，允许拷贝流领先执行流最多 10 拍。

**练习 2：** 把 `SetAsyncTilingCopyStatus(true)` 连续调用两次会发生什么？

**答案：** 不会创建第二条拷贝流。[context_base.cpp:L130-L134](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.cpp#L130-L134) 用 `asyncTilingCopyStream_ != nullptr` 作为当前状态的判据，状态相同时直接 `return NO_ERROR` 并打印 `do nothing`，是幂等的。

---

### 4.2 多流执行与 streamId 路由（图内多流并行）

#### 4.2.1 概念说明

一张图算子里如果有多个**互不依赖**的节点（比如两条独立支路），它们本可以并行。但默认情况下整个 Context 只有一条执行流，所有节点只能排队。

ATB 的解法是「**逻辑流号 streamId**」：

- Context 通过 `SetExecuteStreams` 挂上**多条物理流**。
- 每个节点用 `SetExecuteStreamId(op, id)` 标一个**逻辑流号**（0、1、2…）。
- 执行时框架把逻辑流号换算成物理流：`物理流 = streams[streamId]`。

这样，标了不同 streamId 的节点会被下发到不同物理流上并行，而框架仍能通过 tensorId 的依赖关系保证数据正确（生产者未完成时消费者会因 tensor 数据未就绪而被正确串行——见 4.2.3 的内存复用规则）。

#### 4.2.2 核心流程

一个 Runner 的 workspace 要被多条流**同时**使用，就不能让两条流写同一块内存。ATB 把调用方传入的整块 workspace 按 streamId **切分**：

\[
\text{offset}(\text{streamId}) = \sum_{i=0}^{\text{streamId}-1} S_i,\qquad
S_i = \text{align}(\text{workspaceSize}_i)
\]

其中 \(S_i\) 是第 \(i\) 条流所需 workspace 的对齐大小。Runner 在执行前把自己的 workspace 指针偏移到属于自己的那段：

```
共享 workspaceBuffer: [ S0 | S1 | S2 | ... ]
                       ↑     ↑
                  streamId=0 streamId=1 的 Runner 从这里开始用
```

这样多流共享一块大内存而互不踩踏，调用方只需一次性分配 `sum(S_i)` 即可。

#### 4.2.3 源码精读

**① 挂多条物理流。** [context_base.cpp:L286-L294](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.cpp#L286-L294) 的 `SetExecuteStreams` 直接替换内部的 `executeStreams_` 数组（注意 `SetExecuteStream` 单流版只改 `executeStreams_.at(0)`，见 [context_base.cpp:L110-L121](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.cpp#L110-L121)——单流其实是多流数组的第 0 项，二者同源）。

**② 逻辑流号 → 物理流的换算。** 全局函数 `SetExecuteStreamId`（[operation.cpp:L24-L39](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation.cpp#L24-L39)）用两级 `dynamic_cast` 兼容内部算子（`OperationBase`）与插件算子（`OperationInfra`），把 streamId 写进算子成员。执行时 [operation_base.cpp:L1366-L1379](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L1366-L1379) 的 `GetExecuteStream` 做换算：

```cpp
std::vector<aclrtStream> streams = context->GetExecuteStreams();
if (streams.size() < (streamId_ + 1)) { ... return nullptr; }   // 越界保护
return streams.at(streamId_);                                    // 逻辑号 → 物理流
```

**③ 多流 workspace 切分。** [runner.cpp:L289-L302](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/runner.cpp#L289-L302) 的 `ChangeWorkspaceBufferByExecuteStream` 正是 4.2.2 公式的实现——前缀和偏移加上本段大小：

```cpp
uint32_t streamId = GetExecuteStreamId(operation_);
runnerVariantPack.workspaceBufferSize = multiStreamWorkspaceSizes_.at(streamId);
uint64_t preWorkspaceSize = 0;
for (size_t i = 0; i < streamId; ++i) {
    preWorkspaceSize += multiStreamWorkspaceSizes_.at(i);   // 前缀和
}
runnerVariantPack.workspaceBuffer += preWorkspaceSize;      // 偏移到本流段
```

而 `multiStreamWorkspaceSizes_` 在 Setup 时被 resize 成「流数」大小（[runner.cpp:L50-L56](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/runner.cpp#L50-L56)），每条流各记一份对齐后的 workspace 需求（[runner.cpp:L75-L80](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/runner.cpp#L75-L80)）。

**④ 图内的内存复用规则要为多流让路。** 这是个容易踩坑的点：[graph_runner.cpp:L66-L84](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/graph_runner.cpp#L66-L84) 的 `SetNonReuseTensors` 把「streamId != 0 的节点的输入张量」标记为**不参与中间张量内存复用**（注释原文：`临时方案，如果不是主流（streamId != 0）的node的inTensors的最大节点数全部设为2048不参与内存复用`）。原因是：主流上的活跃性分析假设严格串行，节点用完即可复用其输入内存；但非主流节点与主流并行，其输入可能在并行期间仍被读，复用会引发数据竞争。保守起见，这些张量直接保留不参与复用，牺牲一点显存换正确性。

**⑤ 图内逐节点下发时传递每流 workspace。** [graph_runner.cpp:L990-L1010](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/graph_runner.cpp#L990-L1010) 在逐节点 PreExecute 前，把 GraphRunner 算好的 `multiStreamWorkspaceSizes_` 拷给每个子 Runner，确保子 Runner 知道所有流的切分信息。

#### 4.2.4 代码实践

**目标：** 看懂 `multiStream_singleGraph_demo.cpp` 如何在一张图里让部分节点走第二条流。

**步骤：**

1. 打开 [multiStream_singleGraph_demo.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/multiStream_singleGraph_demo.cpp)，定位 `CreateGraphOperationForMultiStream`（[L96-L134](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/multiStream_singleGraph_demo.cpp#L96-L134)）。
2. 观察图的拓扑：`mulNode(0,1→3)` 与 `addNode2(0,1→4)` 都只依赖输入 0、1，二者无依赖；`graphNode(4,1→5)` 依赖 addNode2 的输出 4；`mulNode1(5,1→2)` 依赖 graphNode 的输出 5。
3. 注意两行关键赋值（[L126](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/multiStream_singleGraph_demo.cpp#L126) 与 [L131](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/multiStream_singleGraph_demo.cpp#L131)）：
   ```cpp
   SetExecuteStreamId(graphNode.operation, 1);   // 子图节点走流 1
   SetExecuteStreamId(mulNode1.operation, 1);    // 末节点走流 1
   ```
4. 看 `main` 里如何挂两条物理流（[L239-L243](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/multiStream_singleGraph_demo.cpp#L239-L243)）：`streams = {stream1, stream2}` 后 `context->SetExecuteStreams(streams)`。

**需要观察的现象：** 标了 streamId=1 的 `graphNode`/`mulNode1` 被路由到 `stream2`，而其余节点在 `stream1`；两条支路在 NPU 上并行。

**预期结果：** 程序正常跑完 `Single graph multi-stream demo start`，两条流都 `aclrtSynchronizeStream` 成功（[L291-L301](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/multiStream_singleGraph_demo.cpp#L291-L301)）。是否真并行需 `msprof` 看时间轴。

#### 4.2.5 小练习与答案

**练习 1：** 若只调用 `SetExecuteStream(op, 1)` 而从没 `SetExecuteStreams`，`GetExecuteStream` 会怎样？

**答案：** 默认 `Init` 把 `executeStreams_` resize 成 1（`DEFAULT_EXECUTE_STREAM_NUMBER = 1`，[context_base.cpp:L28](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.cpp#L28) 与 [L54](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.cpp#L54)）。若某算子 streamId=1，`GetExecuteStream` 检查 `streams.size()(=1) < streamId_+1(=2)` 成立，打印 `streamId is bigger than actual stream number` 并返回 `nullptr`（[operation_base.cpp:L1373-L1377](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L1373-L1377)），后续 Execute 因流为空报错。

**练习 2：** 为什么非主流节点的输入张量不参与中间内存复用？

**答案：** 见 4.2.3 ④：非主流节点与主流并行执行，其输入张量在并行期间可能仍被读取；若按主流的串行活跃性分析复用其内存，会被并行的写者覆盖，产生数据竞争。保守地不参与复用可保证正确（代价是显存占用略增）。

---

### 4.3 多图多流与 Event 同步（图间同步）

#### 4.3.1 概念说明

4.2 讲的是「一张图内部」用 streamId 并行。本节讲「多个独立图算子之间」如何协同。

典型场景：你有两个大图算子 A 和 B，各自跑在独立的 Context（独立执行流）上以追求并行，但 B 的某个阶段需要等 A 产出的中间结果。直接 `aclrtSynchronizeStream` 会把整条流拉停，粒度太粗。ATB 的方案是把 ACL Event 包装成一个**算子节点** `EventParam`，嵌进图里，在精确的位置打 RECORD/WAIT。

`EventParam` 定义在 [common_op_params.h:L38-L61](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/common_op_params.h#L38-L61)，只有两个字段：`event`（要操作的 ACL Event）和 `operatorType`（`RECORD` / `WAIT`）。

#### 4.3.2 核心流程

「图间回合制同步」靠 Event 的 RECORD/WAIT 配对，分两种模式：

- **先 WAIT 再 RECORD（WR）**：本图先在某处 WAIT（阻塞本流，直到别处 RECORD 这个 Event），再在某处 RECORD（通知别处可以继续）。适合「我必须先等对方给我数据，处理完再通知对方」。
- **先 RECORD 再 WAIT（RW）**：本图先 RECORD（告诉对方我这边完成了），再 WAIT（等对方回应）。

两个图算子一前一后各持半边，就形成了跨图的「握手」。⚠️ 一个硬性前提（[common_op_params.h:L50](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/common_op_params.h#L50) 警告）：要同时支持「先 WAIT 再 RECORD」与「先 RECORD 再 WAIT」，必须用 `aclrtCreateEventWithFlag(&event, ACL_EVENT_SYNC)` 创建 Event，普通 `aclrtCreateEvent` 不行。

#### 4.3.3 源码精读

以 `multiStream_multiGraph_demo.cpp` 为例。

**① 两个 Context、两条流、一个共享 Event。** [multiStream_multiGraph_demo.cpp:L201-L206](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/multiStream/multiStream_multiGraph_demo.cpp#L201-L206) 建两个 Context 各绑一条流：

```cpp
atb::Context *contextWR = nullptr;
atb::CreateContext(&contextWR);
contextWR->SetExecuteStream(stream1);          // 图 WR 跑在 stream1
atb::Context *contextRW = nullptr;
atb::CreateContext(&contextRW);
contextRW->SetExecuteStream(stream2);          // 图 RW 跑在 stream2
```

Event 用带 flag 的方式创建（[L198-L199](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/multiStream/multiStream_multiGraph_demo.cpp#L198-L199)）：

```cpp
aclrtCreateEventWithFlag(&event, ACL_EVENT_SYNC);
```

**② 把 Event 嵌进图节点。** `CreateGraphOperationWithWREvent`（[L98-L140](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/multiStream/multiStream_multiGraph_demo.cpp#L98-L140)）构造一张 5 节点图：`mul → WAIT(event) → add → 子图 → RECORD(event)`。注意 WAIT/Record 节点本身就是一个 `CreateOperation(EventParam, ...)` 产生的算子（[L118-L121](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/multiStream/multiStream_multiGraph_demo.cpp#L118-L121) 与 [L134-L137](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/multiStream/multiStream_multiGraph_demo.cpp#L134-L137)），没有真实计算，纯粹是同步原语。对应的图 RW 版本 `CreateGraphOperationWithRWEvent`（[L142-L184](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/multiStream/multiStream_multiGraph_demo.cpp#L142-L184)）顺序相反。

**③ 各自在自己的 Context 上 Execute，互不阻塞地下发。** [L281-L283](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/multiStream/multiStream_multiGraph_demo.cpp#L281-L283)：

```cpp
operationWR->Execute(packWR, (uint8_t *)workSpaceWR, workspaceSizeWR, contextWR);
operationRW->Execute(packRW, (uint8_t *)workSpaceRW, workspaceSizeRW, contextRW);
```

两次 `Execute` 都立即返回（Device 异步），真正的先后约束由图内嵌的 WAIT/RECORD 在 NPU 上完成。

**④ 最后同步两条流取结果。** [L286-L296](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/multiStream/multiStream_multiGraph_demo.cpp#L286-L296) 分别 `aclrtSynchronizeStream(stream1)`、`aclrtSynchronizeStream(stream2)`。

> 与 4.2 图内多流的区别：图内多流是「一张图、一个 Context、多物理流、靠 streamId 路由」；图间同步是「多张图、多个 Context（各一物理流）、靠 EventParam 节点握手」。前者解决图内并行，后者解决图间协同。

#### 4.3.4 代码实践（本讲主实践）

**目标：** 编译运行 `multiStream` demo，理解图间同步的握手过程。

**步骤：**

1. 按官方 [README.md](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/multiStream/README.md) 先 source 两个环境（CANN 与 ATB 的 `set_env.sh`）。
2. 编辑 [CMakeLists.txt](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/multiStream/CMakeLists.txt#L23) 的 `add_executable`，把源文件改成图间同步 demo：
   ```cmake
   add_executable(multiStreamDemo multiStream_multiGraph_demo.cpp)
   ```
3. 注意 ABI 要与已安装的 `libatb.so` 一致（参见 u1-l3）。若 ATB 用 cxx_abi=0 编译，则：
   ```sh
   mkdir build && cd build
   cmake .. -DUSE_CXX11_ABI=OFF
   cmake --build .
   ./multiStreamDemo
   ```

**需要观察的现象：** 程序打印 `multi graph multi-stream demo start` 后正常退出（返回码 0），说明两个图算子在两条流上通过共享 Event 完成了「先 WAIT 再 RECORD」与「先 RECORD 再 WAIT」的配对握手，没有死锁。

**预期结果：** 正常退出、无 `sync error!`。若把 [L199](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/multiStream/multiStream_multiGraph_demo.cpp#L199) 的 `ACL_EVENT_SYNC` 换成普通 `aclrtCreateEvent(&event)`，在「先 WAIT 后 RECORD」的图里可能死锁——这验证了 4.3.2 的硬性前提。

> 待本地验证：握手时序的真实重叠需 `msprof --application="multiStreamDemo"` 看 stream 时间轴；无 NPU 环境只能做源码阅读。

#### 4.3.5 小练习与答案

**练习 1：** demo 里明明有两个图算子，为什么 `CreateInTensors` 只调用了一次、两图共用同一份输入描述？

**答案：** 两个图算子的输入规格完全相同（都是 2 个 `[2,2]` 的 FP16 张量），demo 为简化把 `intensorDescs` 复用，但各自的 `packWR.inTensors` 与 `packRW.inTensors` 是**各自独立分配**的 Device 内存（[L240-L255](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/multiStream/multiStream_multiGraph_demo.cpp#L240-L255) 两次调 `CreateInTensors`），互不影响。

**练习 2：** 如果两个图算子用同一个 Context、同一条流，还需要 EventParam 吗？

**答案：** 不需要。同一条流内任务严格按下发顺序串行执行，天然有序。EventParam 是为「不同流之间需要精细同步点」而设计的；同流场景下加 Event 反而多余。这也说明 Event 是「跨流同步」原语，不是「跨图」本身的要求。

---

### 4.4 两段式下发：EXECUTE_PRELAUNCH / EXECUTE_LAUNCH

#### 4.4.1 概念说明

回顾 [context.h:L34-L38](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/context.h#L34-L38) 的 `ExecuteType` 三态：

- `EXECUTE_NORMAL`：一次 `Execute` 干完所有事（默认）。
- `EXECUTE_PRELAUNCH`：只做**第一段**——Host 侧的校验、形状、Tiling、args 构造。
- `EXECUTE_LAUNCH`：只做**第二段**——把准备好的任务真正下发到 Device。

为什么要拆？因为「Host 准备」与「Device 执行」用的是不同资源：前者占 CPU，后者占 NPU。把它们拆开后，可以让**两个线程**协作——线程 A 对算子 N+1 做 PRELAUNCH（CPU 忙），同时线程 B 对算子 N 做 LAUNCH（NPU 忙）。理想情况下 Host 时间与 Device 时间完全重叠：

\[
T_{\text{total}} \approx \max(T_{\text{prelaunch}},\ T_{\text{launch}}) \cdot N \;<\; (T_{\text{prelaunch}} + T_{\text{launch}}) \cdot N
\]

这就是两段式下发的收益来源，是缓解 Host Bound 的高级手段。

> 注意区分两个「正交」的概念：`ExecuteType`（PRELAUNCH/LAUNCH）控制**一次 Execute 内部做几段**；`LaunchMode`（KERNEL/GRAPH，[context.h:L45-L48](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/context.h#L45-L48)）控制**单算子下发还是整图捕获**。二者可独立组合。

#### 4.4.2 核心流程

`Execute` 内部根据 `executeType` 选择性地调用 `PreLaunch` 和 `Launch`：

```
executeType == NORMAL    → PreLaunch + Launch      （完整执行）
executeType == PRELAUNCH → 仅 PreLaunch             （Host 准备，留给别的线程 Launch）
executeType == LAUNCH    → 仅 Launch                （用上一轮 PreLaunch 的成果下发）
```

要点：`PreLaunch` 与 `Launch` 共享同一个算子对象的内部状态（`runnerVariantPack_` 等），所以 PRELAUNCH 写入的状态必须被后续的 LAUNCH 正确读回——这意味着 PRELAUNCH 与 LAUNCH 必须配对、且通常在**不同线程**但**同一个算子对象**上发生。

#### 4.4.3 源码精读

**① executeType 是 thread_local。** [context_base.h:L67](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.h#L67) 声明 `static thread_local ExecuteType executeType_;`，定义在 [context_base.cpp:L29](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.cpp#L29)。`thread_local` 是关键：它使「哪个线程做 PRELAUNCH、哪个线程做 LAUNCH」天然分离——两个线程各自 `SetExecuteType` 设自己的角色，互不干扰。

**② Execute 的分支分发。** [operation_base.cpp:L1095-L1133](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L1095-L1133) 是入口：

```cpp
ExecuteType executeType = context->GetExecuteType();
...
if (executeType == EXECUTE_NORMAL || executeType == EXECUTE_PRELAUNCH) {
    st = PreLaunch(variantPack, workspace, workspaceSize, context);   // 第一段
}
if (executeType == EXECUTE_NORMAL || executeType == EXECUTE_LAUNCH) {
    st = Launch();                                                     // 第二段
}
```

NORMAL 命中两个 if（全做）；PRELAUNCH 只命中第一个；LAUNCH 只命中第二个。逻辑简洁。

**③ PreLaunch 做了什么。** [operation_base.cpp:L898-L940](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L898-L940)：先按 `LaunchMode` 分流到 `EagerModePreLaunch` 或 `GraphModePreLaunch`；Eager 路径（[L913-L940](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L913-L940)）里依次做 `ExecuteCheck`（校验）→ `PreExecuteThrow`（含 4.1 讲的 Tiling 拷贝与 Runner 准备）。换言之，**4.1 的异步 Tiling 拷贝发生在 PRELAUNCH 段**，这正是它能与「上一算子的 LAUNCH（Device 执行）」重叠的根本原因。

**④ SetExecuteType 的校验。** [context_base.cpp:L301-L311](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.cpp#L301-L311) 拒绝枚举范围外的值，防止误用。

> 小结一条隐含约定：两段式下发要求调用方自己保证 PRELAUNCH 与 LAUNCH 的配对与跨线程同步（框架不做计数配对检查）。这是面向「高级用户+框架集成方」的能力，普通单线程调用保持 `EXECUTE_NORMAL` 即可。

#### 4.4.4 代码实践

**目标：** 用源码阅读理解两段式的时间线，并设计一个最小验证思路。

**步骤：**

1. 在 [operation_base.cpp:L1095](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L1095) 的 `Execute` 入口与 [L1115](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L1115) 的 `PreLaunch`、[L1122](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L1122) 的 `Launch` 调用处各加一行 `std::cout` 打印当前线程 id 与 executeType。
2. 构造两线程模型（示例代码，非项目原有）：
   ```cpp
   // 线程 A：连续对 op[i] 做 PRELAUNCH
   context->SetExecuteType(atb::EXECUTE_PRELAUNCH);
   op[i]->Execute(pack[i], ws[i], wsSize[i], context);
   // 线程 B：连续对 op[i] 做 LAUNCH（须等 A 准备好 op[i]）
   context->SetExecuteType(atb::EXECUTE_LAUNCH);
   op[i]->Execute(pack[i], ws[i], wsSize[i], context);
   ```

**需要观察的现象：** 日志里 PRELAUNCH 的线程 id 与 LAUNCH 的线程 id 不同；同一算子的 PRELAUNCH 一定先于其 LAUNCH（须用应用层信号量保证）。

**预期结果：** 在 NPU 上，线程 A 准备 op[i+1] 的时间与线程 B 让 op[i] 在 Device 上执行的时间重叠，端到端延迟下降。

> 待本地验证：两段式的正确配对与同步需调用方自行实现（条件变量/信号量），框架不提供；本实践重在理解时序，建议先在单算子上验证 PRELAUNCH→LAUNCH 等价于一次 NORMAL。

#### 4.4.5 小练习与答案

**练习 1：** 为什么 `executeType_` 必须是 `thread_local` 而不是普通成员？

**答案：** 两段式下发的核心是「两个线程各做一段」。若 `executeType_` 是普通成员，两个线程 `SetExecuteType` 会互相覆盖，无法让一个线程稳定处于 PRELAUNCH 角色、另一个处于 LAUNCH 角色。`thread_local` 让每个线程拥有独立的副本，[context_base.cpp:L29](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.cpp#L29) 的定义正服务于这一点。

**练习 2：** `EXECUTE_PRELAUNCH` 之后忘了配对 `EXECUTE_LAUNCH`，直接又对同一算子 `EXECUTE_PRELAUNCH`，会发生什么？

**答案：** 框架不做配对计数校验。第二次 PRELAUNCH 会再次执行 `ExecuteCheck` + `PreExecute`（含 Tiling 拷贝），覆盖第一次准备的状态，而上一次准备好的任务**从未真正下发到 Device**（缺了 Launch）。结果是该算子的计算被静默跳过、Device 上无输出更新。这要求调用方严格保证 PRELAUNCH/LAUNCH 配对（参见 4.4.3 末的隐含约定）。

---

## 5. 综合实践

**任务：** 画一张「多图多流 + 异步 Tiling + 两段式」联合时序图，并标注每条流上的事件。

请结合本讲三个机制，画出如下场景在两条 NPU 流上的时间轴（纸笔或绘图工具均可）：

- 图算子 A（绑 stream1，含异步 Tiling）、图算子 B（绑 stream2，含异步 Tiling）。
- A 内部某节点 WAIT 一个共享 Event，B 内部某节点 RECORD 该 Event（先 RECORD 后 WAIT）。
- 两个图算子都采用两段式：线程 1 对 A、B 轮流 PRELAUNCH，线程 2 对 A、B 轮流 LAUNCH。

**要求在图上标出：**

1. 每个图算子各自的**异步 Tiling 拷贝流**上的 `Copy → RecordEvent` 与执行流上的 `WaitEvent → ResetEvent → Kernel` 配对（对应 4.1）。
2. 跨图的 `EventParam` RECORD（在 B 的 stream2）与 WAIT（在 A 的 stream1）握手点（对应 4.3）。
3. PRELAUNCH（Host，线程 1）与 LAUNCH（Device，线程 2）在时间轴上的重叠区间（对应 4.4）。

**自检问题（写出你的答案）：**

- 若关掉异步 Tiling（`SetAsyncTilingCopyStatus(false)`），时间轴上哪一段会变长？
- 若 A、B 改用同一个 Context 同一条流，EventParam 握手是否还有意义？
- 若线程 1 的 PRELAUNCH 速度快于线程 2 的 LAUNCH，瓶颈在哪一侧？反之呢？

> 这是一个「源码阅读 + 设计」型综合实践，不强求在 NPU 上运行。完成后你应当能用一句话向别人解释清楚：异步 Tiling 省的是「同一条流内拷贝与计算的串行」，多流省的是「图内/图间不依赖节点的串行」，两段式省的是「Host 准备与 Device 执行的串行」——三者作用在不同维度的串行上，可叠加使用。

## 6. 本讲小结

- **异步 Tiling 拷贝**用一个独立的拷贝流 + 10 个轮转 Event（`MAX_COPY_EVENT_NUM`），通过 `RecordEvent/StreamWaitEvent/ResetEvent` 四步，把 `Host→Device` 的 Tiling 搬运从执行流上剥离，使其可与上一个 Kernel 的计算重叠；开关是 `SetAsyncTilingCopyStatus`，发生地是 `OperationBase::CopyTilingToDevice`。
- **图内多流**靠 `SetExecuteStreams`（挂多条物理流）+ `SetExecuteStreamId`（给节点标逻辑流号）+ `GetExecuteStream`（逻辑号→`streams[streamId]`）实现；多流共享一块 workspace，按 streamId 做前缀和切分（`ChangeWorkspaceBufferByExecuteStream`）；非主流节点的输入张量不参与内存复用以避免数据竞争。
- **图间同步**把 ACL Event 包装成 `EventParam`（RECORD/WAIT）算子节点嵌进图里，支持两个独立图算子在各自动 Context/流上做回合制握手；前提是 Event 须用 `aclrtCreateEventWithFlag(&event, ACL_EVENT_SYNC)` 创建。
- **两段式下发**用 `ExecuteType`（`thread_local`）把一次 `Execute` 拆成 PRELAUNCH（Host 准备，含 Tiling 拷贝）与 LAUNCH（Device 下发），让两个线程分别负责一段，实现 Host/Device 时间重叠；它只做分支分发，PRELAUNCH/LAUNCH 的配对与同步由调用方保证。
- 三者**正交**：异步 Tiling 改「拷贝走哪条流」，多流改「节点走哪条物理流」，两段式改「一次 Execute 做几段」；与 `LaunchMode`（KERNEL/GRAPH）也正交，可组合使用。
- 官方 `example/multiStream` 提供了图内多流（`singleGraph`）与图间同步（`multiGraph`）两份可直接编译运行的样例，是理解本讲的最佳入口。

## 7. 下一步学习建议

- **学 u7-l2（日志与性能 Profiling）**：本讲多次提到「真实收益需 `msprof` 测量」与「DEBUG 日志验证分支」，下一讲会系统讲解 `ATB_LOG`、日志环境变量与 `ProfStats`，正好补齐观测手段。
- **回看 u3-l5（Context 资源池管理）**：本讲的异步 Tiling、多流 workspace 切分都建立在 TilingBufferPool/Allocator/RunnerPool 之上，若有疑点可对照复习。
- **阅读进阶源码**：`graph_runner.cpp` 中 `SetNonReuseTensors` 与活跃性分析是图内多流正确性的核心，可顺藤摸到 `tensorMaxNodeIdMap` 的内存复用算法；`operation_base.cpp` 的 `GraphModePreLaunch`（整图捕获路径）展示了 `LaunchMode == GRAPH_LAUNCH_MODE` 时 Tiling 改走 Allocator 的细节，是本讲 4.1 末「正交」论断的源码佐证。
- **动手方向**：尝试在 `multiStream_multiGraph_demo` 基础上，把两个图算子都开启异步 Tiling 并用 `msprof` 抓取时间轴，直观对比「同步 Tiling vs 异步 Tiling」在双流场景下的拷贝/计算重叠效果（待本地 NPU 环境验证）。

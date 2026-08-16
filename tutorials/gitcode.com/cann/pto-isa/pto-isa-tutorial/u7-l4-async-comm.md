# 异步通信与通信引擎：TGetAsync/TPutAsync 与 tget_bandwidth

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 `TGET_ASYNC`/`TPUT_ASYNC` 与同步版 `TGET`/`TPUT` 的本质区别：异步版绕过 UB staging tile，由独立 DMA 引擎直接做 GM 到 GM 的搬运，并立即返回一个 `AsyncEvent` 句柄。
- 掌握异步三件套的使用套路：`BuildAsyncSession()` 建会话 → `TGET_ASYNC()/TPUT_ASYNC()` 发起搬运 → `event.Wait()/Test()` 等完成，并理解 SDMA 的 quiet 语义（等最后一个事件即等所有先前操作）。
- 理解通信引擎的分工：`DmaEngine`（SDMA/URMA）负责点对点异步搬运，`CollEngine::CCU` 负责 A5 上的硬件集合通信，三者互相正交、编译期选择。
- 能读懂 tget_bandwidth 示例，理解「UB 中转通路 ~4 GB/s 封顶 vs SDMA 直达 ~10.5 GB/s」这组数字背后的数据通路差异，并掌握主机侧带宽与设备侧周期两种测量口径。

## 2. 前置知识

本讲建立在前几讲之上，先快速回顾三个概念，再补充两个新概念。

**回顾一：同步 TGET 的 UB 中转通路（u7-l2）**。同步版 `TGET` 的数据流是「远端 GM → 本地 UB staging tile → 本地 GM」，即数据必须先流过 Vector 核的 Unified Buffer，再由 TSTORE 写回 GM。这条路通用性好（支持 2-D、掩码、乒乓切分），但带宽被 UB 吞吐封顶在约 4 GB/s。

**回顾二：事件与流水线（u2-l3、u7-l2）**。跨卡可见性靠 `dcci`/`dsb`/`pipe_barrier` 这类内存序指令，而不是计算指令的事件系统。异步通信的完成检查同样是「轮询内存标志」，这一点在本讲会再次出现。

**回顾三：GlobalTensor 是视图不是缓冲（u2-l1）**。异步指令的操作数是两个 GlobalTensor 视图，实际传输量由视图 shape 算出。

**新概念一：SQE（Send Queue Element）**。SDMA 引擎是一个独立的搬运处理器，AICORE 核不能直接"调用"它，只能把搬运请求写成一个个固定格式的描述符（SQE）填进发送队列（SQ），再写一次队列寄存器"敲门铃"（doorbell）通知引擎取走执行。这类似于网卡驱动填描述符再 ring doorbell 的模式。

**新概念二：quiet 语义**。借用 SHMEM 的术语：发起多个异步操作后，只需等待**最后**一个事件，就能保证此前所有操作都已完成。PTO 的 SDMA 后端用单调递增的 post ID 实现了这一点。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/pto/comm/pto_comm_inst.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/pto_comm_inst.hpp) | 通信指令公共 API 层：`TGET_ASYNC`/`TPUT_ASYNC` 的薄壳声明 |
| [include/pto/comm/comm_types.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/comm_types.hpp) | `DmaEngine` 枚举与 `AsyncEvent` 句柄定义 |
| [include/pto/comm/async_common/async_types.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/async_common/async_types.hpp) | `AsyncSession` 引擎无关会话对象 |
| [include/pto/comm/a2a3/async/TGetAsync.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/async/TGetAsync.hpp) | A2/A3 的 `TGET_ASYNC_IMPL` 引擎路由（仅 SDMA） |
| [include/pto/comm/async_common/TGetAsyncCommonDetail.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/async_common/TGetAsyncCommonDetail.hpp) | SDMA 路径的契约检查与 intrinsic 转发（TPut 版结构完全对称） |
| [include/pto/comm/async_common/async_event_impl.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/async_common/async_event_impl.hpp) | `BuildAsyncSession` 与 `AsyncEvent::Wait/Test` 的引擎分发 |
| [include/pto/comm/async/sdma/sdma_async_intrin.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/async/sdma/sdma_async_intrin.hpp) | SDMA 会话构建与 `__sdma_get_async`/`__sdma_put_async` intrinsic |
| [include/pto/comm/async/sdma/sdma_async_detail_post.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/async/sdma/sdma_async_detail_post.hpp) | Post/Wait 协议核心：SQE 提交、flag SQE、完成轮询 |
| [include/pto/comm/async/sdma/sdma_async_detail_basic.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/async/sdma/sdma_async_detail_basic.hpp) | SQE 描述符填充、事件句柄编解码、workspace 地址换算 |
| [include/pto/comm/async/sdma/sdma_workspace_manager.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/async/sdma/sdma_workspace_manager.hpp) | host 侧 workspace 初始化（host-only 头，不可进设备代码） |
| [include/pto/comm/async_common/ccu_trigger.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/async_common/ccu_trigger.hpp) | CCU 集合通信的 CKE 门触发原语 |
| [kernels/manual/a2a3/tget_bandwidth/tget_bandwidth_kernel.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/tget_bandwidth/tget_bandwidth_kernel.cpp) | 带宽对比示例：kernel 侧 + host 侧编排 |
| [kernels/manual/a2a3/tget_bandwidth/README.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/tget_bandwidth/README.md) | 示例说明与实测性能表 |
| [docs/isa/comm/TGET_ASYNC_zh.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/comm/TGET_ASYNC_zh.md) | TGET_ASYNC 的 ISA 文档（约束、quiet 语义、scratchTile 说明） |

一个重要的后端事实：异步指令**只有真机实现**。后端路由头 [include/pto/comm/pto_comm_instr_impl.hpp:L56-L71](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/pto_comm_instr_impl.hpp#L56-L71) 的 `__CPU_SIM` 分支只包含同步点对点、信号同步与集合通信九个头文件，**不含任何 async 头**——`include/pto/cpu/comm/` 目录下也没有对应桩。所以 `TGET_ASYNC`/`TPUT_ASYNC` 无法在 CPU 仿真下编译运行，本讲的实践以源码阅读为主、真机运行为可选。

## 4. 核心概念与源码讲解

### 4.1 异步指令：TGET_ASYNC/TPUT_ASYNC 的语义

#### 4.1.1 概念说明

同步 `TGET` 是"函数返回即数据就绪"（内部替你做完搬运并打了屏障），代价是数据要流经 UB，且 AICORE 核必须全程参与搬运编排。异步 `TGET_ASYNC` 把这件事交给独立的 DMA 引擎：

- 数据通路变成 **远端 GM → DMA 引擎 → 本地 GM**，完全不经过 UB，也不占用 Vector 核的搬运流水线；
- 调用立即返回一个轻量句柄 `AsyncEvent`，核可以继续做计算，之后再用 `Wait`（阻塞）或 `Test`（非阻塞探测）确认完成；
- 由此获得"通信与计算重叠"的能力——这正是 u7-l5 计算-通信融合算子的基石。

"异步"的本质是**提交-完成分离**：提交时只写描述符和门铃（微秒级），真正的搬放在引擎上异步进行，完成与否通过内存中的标志位获知。

#### 4.1.2 核心流程

使用异步指令的标准三步：

```text
1. 建会话（每 kernel 一次）
   ScratchTile scratchTile;  TASSIGN(scratchTile, 0x0);
   AsyncSession session;
   BuildAsyncSession(scratchTile, sdmaWorkspace, session /*, syncId, baseConfig, channelGroupIdx*/);

2. 发起搬运（可多次）
   AsyncEvent e1 = TGET_ASYNC(dstG, remoteSrcG, session);
   AsyncEvent e2 = TGET_ASYNC(dstG2, remoteSrcG2, session);

3. 等完成（quiet 语义：等最后一个即等全部）
   e2.Wait(session);          // 或 e2.Test(session) 非阻塞探测
```

SDMA 后端的完成协议（post ID 机制）：

- session 内部维护单调递增的 `nextPostId`，每次 Post 加一；
- 每次 Post 在数据 SQE 之后**追加一个 8 字节 flag SQE**，内容就是本次 postId；flag SQE 会被复制到本次 session 用过的**所有**队列各自的 Post Done Record 上；
- `Wait` 只轮询对应 postId 的 Done Record，**不再提交任何 SQE**；
- 由于 postId 单调递增且 flag 覆盖所有用过的队列，等待最新的 postId 就蕴含了此前所有 Post 都已完成——这就是 quiet 语义的形式化保证。

约束（来自 ISA 文档与实现中的断言）：

- 源/目的元素类型、layout 必须一致；
- 源和目的都必须是**扁平连续的逻辑一维**张量（packed 布局 + 前四维全为 1），多维或不连续布局不支持；
- 同一 session 最多 64 个未完成操作（超过会产生背压）；
- session 与 workspace 的生命周期必须覆盖所有相关事件的完成。

#### 4.1.3 源码精读

**公共 API 层**。与所有 PTO 指令一样，`TGET_ASYNC` 是一个薄壳：先等待可选的前置事件，再转发给 `TGET_ASYNC_IMPL`，返回值从 `RecordEvent` 换成了 `AsyncEvent`：

- [include/pto/comm/pto_comm_inst.hpp:L369-L380](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/pto_comm_inst.hpp#L369-L380)——`TGET_ASYNC` 声明：模板参数 `engine` 默认 `DmaEngine::SDMA`，可变参数 `WaitEvents...` 允许把上游流水线事件挂进来（`WaitAllEvents` 展开）。`TPUT_ASYNC` 在 [L338-L349](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/pto_comm_inst.hpp#L338-L349) 结构完全相同。

**两个核心类型**。句柄与会话的定义都在公共类型头里：

- [include/pto/comm/comm_types.hpp:L179-L189](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/comm_types.hpp#L179-L189)——`AsyncEvent` 只有一个 `uint64_t handle`（0 表示无效）加引擎标记，成员函数 `Wait/Test` 声明在这里、实现在别处（见 4.2.3）。
- [include/pto/comm/async_common/async_types.hpp:L128-L148](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/async_common/async_types.hpp#L128-L148)——`AsyncSession` 是**引擎无关**的扁平结构：`contextGm`（workspace 指针）、`tmpBufAddr/tmpBufSize`（UB scratch）、`syncId`、`channelGroupIdx`、`blockBytes/queueNum`（传输分块配置），以及一个 `mutable` 的 `sdmaRuntimeCtx`——SDMA 后端把 post ID 等运行时状态"藏"在 session 里跨调用续传，这就是同一 session 连续多次 Post 仍能保持协议连贯的原因。

**A2/A3 的引擎路由**。架构实现头极薄，只做编译期引擎分派：

- [include/pto/comm/a2a3/async/TGetAsync.hpp:L24-L34](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/async/TGetAsync.hpp#L24-L34)——A2/A3 上 `engine` 只允许 `DmaEngine::SDMA`，否则 `static_assert` 直接编译报错（URMA 是 A5 系硬件才有的引擎，见 4.2）。`TPutAsync.hpp` 与之逐行对称。

**SDMA 路径的契约检查与转发**：

- [include/pto/comm/async_common/TGetAsyncCommonDetail.hpp:L26-L44](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/async_common/TGetAsyncCommonDetail.hpp#L26-L44)——`TGetAsyncIsFlatContiguous1D` 把"扁平连续一维"翻译成两个布尔条件：五维 stride 逐级 packed（最内维为 1、每级等于低维乘积）且前四维形状全为 1。
- [include/pto/comm/async_common/TGetAsyncCommonDetail.hpp:L67-L94](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/async_common/TGetAsyncCommonDetail.hpp#L67-L94)——`TGET_ASYNC_SDMA_IMPL` 全流程：静态断言类型/layout 一致 → 断言指针非空、源和目的都是一维连续 → 用 shape 乘积算元素数并断言 `dstElems >= srcElems` → 调 `sdma::__sdma_get_async`（注意字节数 = 元素数 × sizeof(T)，在调用点算好）→ 把返回的原始 handle 包成 `AsyncEvent`。`TPutAsyncCommonDetail.hpp` 中的 `TPUT_ASYNC_SDMA_IMPL` 是同构的镜像代码，只把 get 换成 put。

**Wait/Test 的引擎分发**：

- [include/pto/comm/async_common/async_event_impl.hpp:L72-L104](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/async_common/async_event_impl.hpp#L72-L104)——`AsyncEvent::Wait/Test` 按 `session.engine` switch 分发到 `SdmaWaitEvent/SdmaTestEvent` 或 URMA 对应实现；`handle == 0` 时直接返回 true（无效事件视为已完成）。

**ISA 文档对照**（建议配合阅读）：

- [docs/isa/comm/TGET_ASYNC_zh.md:L115-L128](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/comm/TGET_ASYNC_zh.md#L115-L128)——quiet 语义的官方表述：多次 `TGET_ASYNC` 后只需对最后一个 `AsyncEvent` 调一次 `Wait`，即可等待所有 pending 操作；同一 session 最多 64 个未完成操作。
- [docs/isa/comm/TGET_ASYNC_zh.md:L96-L113](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/comm/TGET_ASYNC_zh.md#L96-L113)——scratchTile 的定位：它**不是**数据暂存区，只是写/读 SDMA 控制字（flag、sq_tail、channel_info）和轮询完成标志的 UB 工作区，最小 8 字节、推荐 256 字节。

#### 4.1.4 代码实践

**实践目标**：通过纯源码阅读，把 `TGET_ASYNC` 从公共 API 到 intrinsic 的调用链走一遍，画出调用图。

**操作步骤**：

1. 从 [include/pto/comm/pto_comm_inst.hpp:L374-L380](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/pto_comm_inst.hpp#L374-L380) 的 `TGET_ASYNC` 出发，记下它转发到的 `TGET_ASYNC_IMPL`。
2. 打开 [include/pto/comm/a2a3/async/TGetAsync.hpp:L24-L34](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/async/TGetAsync.hpp#L24-L34)，确认 A2/A3 只有 SDMA 分支，进入 `detail::TGET_ASYNC_SDMA_IMPL`。
3. 打开 [include/pto/comm/async_common/TGetAsyncCommonDetail.hpp:L67-L94](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/async_common/TGetAsyncCommonDetail.hpp#L67-L94)，数一数进入 intrinsic 前有几道检查（答案：4 个 `PTO_ASSERT` + 2 个 `static_assert`）。
4. 跟进 [include/pto/comm/async/sdma/sdma_async_intrin.hpp:L183-L196](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/async/sdma/sdma_async_intrin.hpp#L183-L196) 的 `__sdma_get_async`（AsyncSession 版）：注意它在调用前后做了 `LoadSdmaSession` / 写回 `sdmaRuntimeCtx` 两件事——解释为什么必须这样（提示：AsyncSession 是扁平的，SDMA 后端要的是带 `runtimeCtx` 的 `SdmaSession` 视图，post ID 状态必须跨调用持久）。
5. 再对照 `TPutAsyncCommonDetail.hpp` 与 `TGetAsyncCommonDetail.hpp`，统计两个文件的差异行数。

**需要观察的现象**：`TGET_ASYNC` 与 `TPUT_ASYNC` 在 A2/A3 上的实现几乎逐字相同——两个指令共用同一个 `SdmaPostAsync` 后端（见 4.2.3），"读远端"与"写远端"在 SDMA 引擎看来只是 src/dst 地址哪个在远端窗口的区别。

**预期结果**：得到一条 4 层调用链
`TGET_ASYNC（API 薄壳）→ TGET_ASYNC_IMPL（架构路由）→ TGET_ASYNC_SDMA_IMPL（契约检查）→ __sdma_get_async（intrinsic）→ SdmaPostAsync（SQE 协议）`。
若在本地无法编译真机代码，本实践全程只需阅读，标注「待本地验证」的部分仅限真机运行。

#### 4.1.5 小练习与答案

**练习 1**：下面的三个 GlobalTensor 视图，哪些能通过 `TGetAsyncIsFlatContiguous1D` 的检查？
(a) shape=(1,1,1,1,1024)、stride=(1024,1024,1024,1024,1)
(b) shape=(1,1,1,64,256)、stride=(16384,16384,16384,256,1)
(c) shape=(1,1,1,1,1024)、stride=(2048,2048,2048,2048,2)

**答案**：只有 (a) 通过。(b) 的 DIM_3=64 ≠ 1，是逻辑二维，`oneDimLogical` 不满足；(c) 最内维 stride 为 2，是跨步（非 packed）布局，`packedLayout` 不满足。(b)、(c) 都会触发 `TGET_ASYNC_SDMA_IMPL` 中的断言（[TGetAsyncCommonDetail.hpp:L77-L84](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/async_common/TGetAsyncCommonDetail.hpp#L77-L84)）；若确需传二维数据，应先在 host 侧保证内存连续，再按一维视图发起异步搬运，或退回同步 TGET（它支持 2-D 与掩码）。

**练习 2**：一个 kernel 里连续发了 10 次 `TGET_ASYNC`（同一 session），第 3 次返回的事件对象被意外丢弃了。第 10 次的 `Wait` 返回后，能否保证第 3 次的搬运已完成？为什么？

**答案**：能。SDMA 的 post ID 单调递增，且每次 Post 的 flag SQE 会被追加到 session 用过的所有队列上（见 [sdma_async_detail_post.hpp:L187-L198](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/async/sdma/sdma_async_detail_post.hpp#L187-L198)），等待 postId=10 蕴含 postId≤10 的所有 Post 均完成（quiet 语义）。但要注意两点：事件对象本身可以丢弃，**未完成操作数不能超过 64**；且 session 复用/重建前必须等完（[TGET_ASYNC_zh.md:L130-L136](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/comm/TGET_ASYNC_zh.md#L130-L136)）。

**练习 3**：`AsyncEvent::Wait` 的参数为什么要传 `session`，而不是像计算指令事件那样只靠事件本身？

**答案**：因为 `handle` 里只编码了 postId 和队列数（见 4.2.3 的句柄编解码），完成记录的**地址**（`postDoneBase`）、轮询用的 UB scratch、以及 `runtimeCtx` 里的已确认 post ID 缓存都存在 session 中；且引擎选择（SDMA/URMA 走不同的 Wait 实现）也记录在 `session.engine` 上。计算指令的事件是 AICORE 流水线硬件信号，由 (srcPipe, dstPipe, eventId) 三元组直接寻址，不需要软件上下文——这是两套事件系统的本质差异。

### 4.2 通信引擎：SDMA/URMA/CCU 的分工

#### 4.2.1 概念说明

"通信引擎"是 PTO 通信 ISA 里与指令正交的一个维度：指令说"做什么"，引擎说"谁来做"。仓库里有三个名字容易混淆，先把它们摆正：

| 引擎 | 枚举 | 所属指令 | 硬件角色 | 可用性 |
| --- | --- | --- | --- | --- |
| SDMA | `DmaEngine::SDMA` | TGET_ASYNC/TPUT_ASYNC | 芯片内/跨片系统 DMA 引擎，填 SQE 驱动 | A2/A3、A5 |
| URMA | `DmaEngine::URMA` | TGET_ASYNC/TPUT_ASYNC | 用户级 RDMA 引擎（HCCP V2 Jetty），走 WQE/CQE | 仅 NPU_ARCH 3510（Ascend 950PR/DT），CANN ≥ 9.1.0 |
| CCU | `CollEngine::CCU` | 集合通信（TREDUCE/TGATHER/…） | A5 独有的集合通信硬件单元，经 CKE 门触发 | A5；A2/A3 集合通信走 `CollEngine::AIV`（软件编排） |

要点：

- `DmaEngine` 与 `CollEngine` 是**两个独立枚举**：前者选点对点异步搬运后端，后者选集合通信后端（[comm_types.hpp:L121-L135](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/comm_types.hpp#L121-L135)）。"CCU 是第三种异步引擎"更准确的说法是：它是集合通信的硬件加速通路，同样采用"AIV 触发、硬件执行"的异步思想。
- SDMA 与 URMA 各有约束：SDMA 描述自己"支持 2D transfer"（枚举注释），但 PTO 当前异步实现只放开了一维连续；URMA 只做一维，单次 WQE 上限 256 MB（[urma_types.hpp:L30](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/async/urma/urma_types.hpp#L30)），且数据缓冲区须用大页内存分配（`ACL_MEM_MALLOC_HUGE_ONLY`）。
- 引擎选择全部发生在**编译期**（模板参数 + `if constexpr` + 架构宏），换引擎不改业务代码，只改 `BuildAsyncSession` 的调用形式。

#### 4.2.2 核心流程

SDMA 一次 Post（`SdmaPostAsync`）的完整协议：

```text
BeginSdmaPost（准备阶段）
  ├─ BuildTransferConfig：iter_num = ceil(总字节 / block_bytes)，
  │   数据 SQE 按 index % queue_num 轮转分配到多个队列
  ├─ postId = runtimeCtx.nextPostId + 1（单调递增）
  ├─ StoreFlagPayload：把 postId 写进 64 槽 payload 环（postId % 64）；
  │   若要复用槽位，先确认 64 次之前的 Post 已完成（背压点）
  └─ EncodeSdmaEventHandle：handle = (queueCount << 58) | postId

SubmitDataTransferSqes（数据阶段）
  ├─ 逐块 AddOneMemcpySqe：src/dst 拆高低 32 位地址写入 SQE
  └─ sqTail 前移（模 sq_depth 环回）

FinishSdmaPost（发布阶段）
  ├─ PublishDataTransferSqes：dcci 清缓存 + dsb + RingDoorbell（数据 SQE 生效）
  ├─ SubmitFlagTransferSqes：每个用过的队列追加一个 flag SQE
  │   （把 postId 拷到该队列的 Post Done Record）
  ├─ PersistSqTails：把 head/tail 打包写回 channel_info
  └─ 再次 doorbell（flag SQE 生效）→ 返回 AsyncEvent

Wait/Test（完成阶段，不提交任何 SQE）
  ├─ DecodeSdmaEventHandle 还原 postId 与 queueMask
  └─ 轮询各队列 Done Record >= postId（Wait 阻塞自旋，上限 10 万次；
     Test 只查一次立即返回）
```

Post Done Record 是每队列一个 64 字节跨距的内存槽（[sdma_async_detail_basic.hpp:L216-L219](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/async/sdma/sdma_async_detail_basic.hpp#L216-L219)），SDMA 引擎按 SQE 顺序执行，flag SQE 排在数据 SQE 之后，因此"Done Record 里出现 postId"等价于"该队列上 postId 之前的所有数据 SQE 都已搬完"——完成检测的数学基础就是这条**顺序保证**：

\[ \text{DoneRecord}[q] \ge \text{postId} \iff \forall\, \text{SQE} \prec \text{flag}(\text{postId}) \text{ on queue } q:\ \text{done} \]

workspace 内存布局（host 侧 `SdmaWorkspaceManager` 一次性备好，总量 16 KiB + 48 × 512 B）：

```text
workspace（GM，由 SdmaWorkspaceManager::Init 分配并填充）
 ├─ 16 KiB  context 区：BatchWriteFlagInfo + 48 组 × queue_num 个 BatchWriteChannelInfo
 │            （SQ 基址、寄存器基址、队列深度、stream_id——由 AICPU STARS 查询内核填好）
 └─ 48 × 512 B  payload 区：每个通道组一个 64 槽 × 8 B 的 postId flag 环
```

#### 4.2.3 源码精读

**引擎枚举**：

- [include/pto/comm/comm_types.hpp:L121-L124](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/comm_types.hpp#L121-L124)——`DmaEngine`：SDMA（注释 Supports 2D transfer）与 URMA（1D、HCCP V2 Jetty、仅 NPU_ARCH 3510）。紧随其后的 [L132-L135](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/comm_types.hpp#L132-L135) 是 `CollEngine`（AIV/CCU），两个枚举并列出现，直观体现"点对点引擎"与"集合引擎"的分工。

**会话构建（含合法性检查与运行时状态初始化）**：

- [include/pto/comm/async_common/async_event_impl.hpp:L24-L40](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/async_common/async_event_impl.hpp#L24-L40)——引擎无关的 `BuildAsyncSession<SDMA>` 入口：转发给 `BuildSdmaSession`。URMA 专用重载在 [L42-L66](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/async_common/async_event_impl.hpp#L42-L66)（受 `PTO_URMA_SUPPORTED` 宏保护，参数只有 workspace 与目标 rank——URMA 轮询走 `ld_dev`/`st_dev` 硬件原语，不需要 scratchTile）。
- [include/pto/comm/async/sdma/sdma_async_intrin.hpp:L102-L138](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/async/sdma/sdma_async_intrin.hpp#L102-L138)——AsyncSession 版 `BuildSdmaSession` 的完整流程：未指定 `channelGroupIdx` 时默认取 `get_block_idx()`（每个核一个专属通道组，天然隔离）；随后做参数合法性检查（`syncId ≤ 7`、`queue_num` 非零且不超过 48、`channelGroupIdx` 不越界）；把 scratchTile 转成 `TmpBuffer`；最后调 `InitializeRuntimeCtx` 初始化 post ID/队列头尾并回填 `session.sdmaRuntimeCtx`。
- [include/pto/comm/async/sdma/sdma_async_detail_post.hpp:L55-L89](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/async/sdma/sdma_async_detail_post.hpp#L55-L89)——`InitializeRuntimeCtx`：清零各队列 Done Record（`SetValue` 经 UB 中转写 GM）→ `dcci` + `dsb` 保证可见 → 从 workspace 的 channel_info 读回每队列的 head/tail 作为起点。注意第 76-80 行的注释：channel 元数据在核外填充，session 建立时统一做一次 `dcci` 失效，让 Post 热路径不必每次刷缓存——这是一个典型的"把昂贵内存序操作挪出热路径"的工程决策。

**SQE 结构与填充**：

- [include/pto/comm/async/sdma/sdma_types.hpp:L42-L48](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/async/sdma/sdma_types.hpp#L42-L48)——`SdmaConfig`：`block_bytes`（每 SQE 块大小，默认 1 MB）、`per_core_bytes`（本次总量）、`queue_num`、`iter_num`（= ceil(总字节/块大小)，即数据 SQE 数）。
- [include/pto/comm/async/sdma/sdma_types.hpp:L73-L88](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/async/sdma/sdma_types.hpp#L73-L88)——`BatchWriteChannelInfo`：一个队列的元数据（SQ 基址、寄存器基址、深度、流 ID 等），由 host 侧预填、kernel 只读。
- [include/pto/comm/async/sdma/sdma_async_detail_basic.hpp:L130-L185](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/async/sdma/sdma_async_detail_basic.hpp#L130-L185)——`AddOneMemcpySqe`：在 SQ 环的 `sqTail` 位置填一个搬运 SQE，`type = RT_STARS_SQE_TYPE_SDMA`，源/目的地址拆成高低 32 位写入；A5 与 A2/A3 的 SQE 位域不同，用 `#ifdef PTO_NPU_ARCH_A5` 分开两套（跨代 SQE 格式差异被封装在这一个函数里）。

**Post 主流程**：

- [include/pto/comm/async/sdma/sdma_async_detail_post.hpp:L287-L301](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/async/sdma/sdma_async_detail_post.hpp#L287-L301)——`SdmaPostAsync`：`Begin → Submit → Finish` 三段式，`__sdma_get_async` 与 `__sdma_put_async` 共用这一个后端（opcode 传 0）。
- [include/pto/comm/async/sdma/sdma_async_detail_post.hpp:L239-L269](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/async/sdma/sdma_async_detail_post.hpp#L239-L269)——`BeginSdmaPost`：校验 SQ 容量（一次 Post 的 SQE 数必须放得进队列深度）、分配 postId、写 flag payload、编码事件句柄；`postQueueCount` 取"本次数据队列数"与"session 历史用过的队列数"的较大值——这正是 quiet 语义的实现细节：flag SQE 要覆盖**历史所有**队列，等最新 postId 才能保证全部完成。
- [include/pto/comm/async/sdma/sdma_async_detail_post.hpp:L143-L161](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/async/sdma/sdma_async_detail_post.hpp#L143-L161)——`SubmitDataTransferSqes`：按 `index % queue_num` 把数据块轮转铺到多个队列（多队列并行搬运），最后一块按剩余字节数收尾。
- [include/pto/comm/async/sdma/sdma_async_detail_post.hpp:L187-L198](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/async/sdma/sdma_async_detail_post.hpp#L187-L198)——`SubmitFlagTransferSqes`：给每个参与队列追加一个"把 postId 拷到 Done Record"的 flag SQE——完成检测的物化形式。
- [include/pto/comm/async/sdma/sdma_async_detail_post.hpp:L163-L171](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/async/sdma/sdma_async_detail_post.hpp#L163-L171)——`RingDoorbell`：写 SQ 寄存器告知引擎新尾指针（A5 与 A2/A3 寄存器偏移不同）。

**Wait/Test 与句柄编解码**：

- [include/pto/comm/async/sdma/sdma_async_detail_basic.hpp:L29-L39](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/async/sdma/sdma_async_detail_basic.hpp#L29-L39)——关键常量：payload 环深 64（即 64 个未完成操作上限）、轮询上限 10 万次、句柄 layout（6 位队列数 + 58 位 postId）。
- [include/pto/comm/async/sdma/sdma_async_detail_basic.hpp:L46-L66](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/async/sdma/sdma_async_detail_basic.hpp#L46-L66)——`EncodeSdmaEventHandle`/`DecodeSdmaEventHandle`：把 (postId, queueCount) 打包进一个 `uint64_t`。
- [include/pto/comm/async/sdma/sdma_async_detail_post.hpp:L303-L336](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/async/sdma/sdma_async_detail_post.hpp#L303-L336)——`SdmaTestEvent`/`SdmaWaitEvent`：解码句柄得到 postId 与 queueMask，转 `SdmaEventCheck`；blocking 参数区分 Test（查一次）与 Wait（自旋至多 `kPostPollLimit` 次）。注意 Wait 期间**不提交任何 SQE**——等待是纯内存轮询。
- [include/pto/comm/async/sdma/sdma_async_detail_post.hpp:L91-L124](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/async/sdma/sdma_async_detail_post.hpp#L91-L124)——`StoreFlagPayload`：写 payload 环前，若槽位将被复用（postId > 64），先确认 64 次之前的 Post 已完成，否则阻塞等待——背压机制的落点。

**host 侧 workspace 初始化**：

- [include/pto/comm/async/sdma/sdma_workspace_manager.hpp:L47-L58](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/async/sdma/sdma_workspace_manager.hpp#L47-L58)——`SdmaWorkspaceManager` 的职责注释：创建 MC2 STARS 流 → 分配 workspace → 起 AICPU STARS 查询内核填充硬件 SQ 地址/寄存器基址/队列深度。文件头 [L14-L17](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/async/sdma/sdma_workspace_manager.hpp#L14-L17) 明确它是 **host-only** 头，禁止被设备代码 include。
- [include/pto/comm/async_common/sdma_constants.hpp:L22-L27](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/async_common/sdma_constants.hpp#L22-L27)——workspace 尺寸常量：16 KiB context + 48 组 × 512 B payload，总 `kSdmaWorkspaceBytes`。

**CCU 触发原语**（集合通信的硬件通路，与 SDMA 对照）：

- [include/pto/comm/async_common/ccu_trigger.hpp:L27-L30](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/async_common/ccu_trigger.hpp#L27-L30)——CKE 门槽位编码：bit 63 是有效位，低 16 位是参与 rank 掩码，CCU 硬件读走掩码并清槽。
- [include/pto/comm/async_common/ccu_trigger.hpp:L41-L50](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/async_common/ccu_trigger.hpp#L41-L50)——`CkeTrigger`：dcci → 标量写 MMIO 槽位（payload = mask | 有效位）→ dcci → `dsb(DSB_DDR)`。AIV 只负责"敲门"，集合运算由 CCU 硬件完成——与 SDMA 的"AIV 填 SQE + doorbell，搬运由 DMA 引擎完成"是同一个异步思想在两类引擎上的投影。

#### 4.2.4 代码实践

**实践目标**：用具体数字推演一次 SDMA Post 的 SQE 数量与队列占用，验证对协议的理解。

**操作步骤**：

1. 设定场景：传输 2.5 MB，`SdmaBaseConfig` 取默认值 `{block_bytes = 1 MB, comm_block_offset = 0, queue_num = 1}`。
2. 阅读 [sdma_async_detail_basic.hpp:L187-L198](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/async/sdma/sdma_async_detail_basic.hpp#L187-L198) 的 `BuildTransferConfig`，手算 `iter_num`。
3. 阅读 [sdma_async_detail_post.hpp:L143-L161](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/async/sdma/sdma_async_detail_post.hpp#L143-L161) 的 `SubmitDataTransferSqes`，确认最后一块的字节数如何收尾。
4. 数一数这次 Post 总共往 SQ 里放了几个 SQE（数据 + flag）。
5. （可选，真机）用 tget_bandwidth 的 `device_baseline` 模式实测验证：`export TGET_BENCH_MODE=device_baseline; export TGET_DEVICE_BASELINE_BYTES=2621440; export TGET_DEVICE_BASELINE_BLOCK_DIVISOR=1; export TGET_DEVICE_BASELINE_QUEUE_NUM=1; export TGET_DEVICE_BASELINE_POST_COUNT=1;` 再按 [README.md:L161-L187](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/tget_bandwidth/README.md#L161-L187) 的说明运行。

**需要观察的现象**：`iter_num = ceil(2.5 MB / 1 MB) = 3`，前两块各 1 MB，末块 512 KB（`per_core_bytes - index * block_bytes` 收尾）；queue_num=1 时三个数据 SQE 全落同一队列，flag SQE 再加 1 个，单队列共 4 个 SQE。`BLOCK_DIVISOR=1` 的 baseline 模式正是"每 Post 一个数据 SQE + 一个完成 SQE"的最小形态（[README.md:L178-L180](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/tget_bandwidth/README.md#L178-L180)）。

**预期结果**：SQE 计数 = `iter_num + postQueueCount`（此例 3 + 1 = 4）。真机验证部分标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `BuildSdmaSession` 在不指定 `channelGroupIdx` 时默认用 `get_block_idx()`？

**答案**：SDMA 的通道组是全局只有 48 个的共享资源（`kSdmaMaxChannelGroups = 48`）。按核号自动分配，让每个 AICORE 核天然拿到互不相同的通道组，多核并发时互不干扰；只有当多核有意共享队列、或 host 侧做了自定义映射时才需要手工覆盖（[TGET_ASYNC_zh.md:L58](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/comm/TGET_ASYNC_zh.md#L58)）。同时这也是一条纪律：并发 kernel 或同 kernel 多 session 必须使用隔离的通道组。

**练习 2**：SDMA 的 `Wait` 为什么选在 GM 的 Done Record 上轮询，而不是用 AICORE 的 set_flag/wait_flag 事件？

**答案**：完成信号的产生者是 **SDMA 引擎**（它执行 flag SQE、写 Done Record），不是 AICORE 的任何一条流水线。AICORE 事件系统的三元组 (srcPipe, dstPipe, eventId) 只覆盖本核流水线（MTE2/V/M 等），无法接收外部引擎的完成通知；而 GM 轮询配合 `GetValue`（先 DMA 到 UB 再读，[sdma_async_detail_basic.hpp:L112-L128](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/async/sdma/sdma_async_detail_basic.hpp#L112-L128)）对任何外部异步部件都成立。这与 u7-l2 讲过的"跨卡可见性靠内存序指令而非事件系统"一脉相承。

**练习 3**：把 `DmaEngine::URMA` 传给 A2/A3 的 `TGET_ASYNC` 会发生什么？把 `CollEngine::CCU` 传给 A2/A3 的 `TREDUCE` 呢？

**答案**：前者编译失败——[TGetAsync.hpp:L31](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/async/TGetAsync.hpp#L31) 的 `static_assert(engine == DmaEngine::SDMA, ...)` 在编译期拦截（URMA 需要硬件支持，A2/A3 没有）。后者会被路由到 CCU 实现路径（[pto_comm_inst.hpp:L308-L311](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/pto_comm_inst.hpp#L308-L311)），但 CCU 是 A5 独有硬件，A2/A3 上属于不可用能力（u7-l1 已述：架构能力在编译期经架构宏拦截误用）。两者的共同点是：引擎能力是**编译期属性**，误用在编译阶段暴露而不是运行期出错。

### 4.3 带宽测量：tget_bandwidth 示例

#### 4.3.1 概念说明

tget_bandwidth 是一个 2 卡点对点微基准：root 卡分别用同步 `TGET` 与异步 `TGET_ASYNC` 从 peer 卡读同一块对称共享内存，传输量从 4 KB 扫到 4 MB，输出两套指标：

- **主机侧带宽（GB/s）**：host 用 `gettimeofday` 计时（整 kernel 启动 + 设备执行 + 同步），反映端到端吞吐；
- **设备侧平均周期**：kernel 内用 `SYS_CNT` 系统计数器只包住计时循环，剔除 launch 开销，反映设备上纯粹的指令执行代价。

两种口径都只计"从 peer 对称 `sendShmem` 到 root 对称 `recvShmem`"的传输；校验用的 CopyOut 在计时区之外（[README.md:L58-L60](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/tget_bandwidth/README.md#L58-L60)），保证公平。

#### 4.3.2 核心流程

两条被测通路：

```text
TGET（同步，UB 中转）：
  peer GM ──TGET(TLOAD 跨卡)──▶ root UB staging tile ──TSTORE──▶ root GM
  · staging tile 固定 1×1024 元素，大形状靠 kernel 内部分块循环
  · 带宽受 UB 吞吐封顶（实测 ~4 GB/s 量级饱和）

TGET_ASYNC（异步，SDMA 直达）：
  peer GM ──SDMA 引擎（SQE + doorbell）──▶ root GM
  · 不经过 UB、不占用 Vector 流水线
  · 小块有 SQE 提交/完成协议的固定开销；大块逼近引擎线速
```

性能拐点的直觉：异步路径把"每字节的搬运成本"从 UB 双跳降为单跳，但要付出每次 Post 的固定协议成本（写 SQE、doorbell、轮询完成）。传输量越大，固定成本摊得越薄，异步优势越明显；反之小块会被启动开销拖累。用带宽模型表达：

\[ BW_{\text{async}}(s) = \frac{s}{T_{\text{post}} + s / R_{\text{sdma}}}, \qquad BW_{\text{sync}}(s) \approx \min(R_{\text{ub}},\ s / T_{\text{launch}}) \]

其中 \( s \) 是传输字节数，\( T_{\text{post}} \) 是协议固定成本，\( R_{\text{sdma}} \)、\( R_{\text{ub}} \) 分别是两条通路的线速。\( s \to \infty \) 时异步带宽趋近 \( R_{\text{sdma}} \)，同步带宽封顶在 \( R_{\text{ub}} \)。

#### 4.3.3 源码精读

**kernel 侧：同步 TGET 的计时核**：

- [kernels/manual/a2a3/tget_bandwidth/tget_bandwidth_kernel.cpp:L190-L240](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/tget_bandwidth/tget_bandwidth_kernel.cpp#L190-L240)——`ProfileTGetBandwidthKernel`：root 上先 warmup，再 `GetSyscnt()` 取 t0 → 循环 `TGET(recvG, remoteSendG, stagingTile)` + `pipe_barrier(PIPE_ALL)` → 取 t1，差值写入 GM。注意 staging tile 只有 `1×kTileElems`（1024 元素，[L17](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/tget_bandwidth/tget_bandwidth_kernel.cpp#L17)），`ColMaskInternal` 按 elemCount 收紧（[L214-L215](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/tget_bandwidth/tget_bandwidth_kernel.cpp#L214-L215)）——4 MB 的传输要靠 TGET 内部的 2-D 分块循环过 UB，这正是它慢的根源。远端地址由 `CommRemotePtr(hcclCtx, sendShmem, peerRank)` 换算（HCCL 窗口机制，u7-l2 已讲）。

**kernel 侧：异步 TGET_ASYNC 的计时核**：

- [kernels/manual/a2a3/tget_bandwidth/tget_bandwidth_kernel.cpp:L282-L335](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/tget_bandwidth/tget_bandwidth_kernel.cpp#L282-L335)——`ProfileTGetAsyncBandwidthKernel`：与同步版同构的计时骨架，但数据通路完全不同——scratchTile 只有 256 字节（`Tile<Vec, uint8_t, 1, UB_ALIGN_SIZE>`，[L290](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/tget_bandwidth/tget_bandwidth_kernel.cpp#L290)），因为它只承载控制元数据；`BuildAsyncSession`（[L308](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/tget_bandwidth/tget_bandwidth_kernel.cpp#L308)）一次、循环内 `TGET_ASYNC` + `event.Wait(session)`（[L321-L322](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/tget_bandwidth/tget_bandwidth_kernel.cpp#L321-L322)）。对比两版计时核的 tile 声明（1024 元素数据 tile vs 256 字节控制 tile）是理解两条通路差异最直观的一行代码。

**host 侧：单次测量用例**：

- [kernels/manual/a2a3/tget_bandwidth/tget_bandwidth_kernel.cpp:L403-L497](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/tget_bandwidth/tget_bandwidth_kernel.cpp#L403-L497)——`RunSingleBandwidthCase` 的四段结构：warmup → 主机侧计时（`NowMs()` 夹住 timedIters 次 launch，[L428-L435](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/tget_bandwidth/tget_bandwidth_kernel.cpp#L428-L435)）→ 校验（CopyOut 后逐元素比对 `i + peerRank*10000`）→ 设备侧计时核（读回 `profileCycles` 算平均，[L488-L494](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/tget_bandwidth/tget_bandwidth_kernel.cpp#L488-L494)）。两套指标在同一次用例里先后测得。

**host 侧：workspace 与扫参循环**：

- [kernels/manual/a2a3/tget_bandwidth/tget_bandwidth_kernel.cpp:L582-L611](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/tget_bandwidth/tget_bandwidth_kernel.cpp#L582-L611)——root 卡先 `sdmaMgr.Init()`（`SdmaWorkspaceManager`，为异步通路备好 SQ/channel 元数据），随后对 `kBenchBytes` 六个档位（[L24-L26](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/tget_bandwidth/tget_bandwidth_kernel.cpp#L24-L26)：4 KB~4 MB）先测 TGET（`sdmaWorkspace` 传 `nullptr`）再测 TGET_ASYNC（传 `sdmaMgr.GetWorkspaceAddr()`）。

**实测数据与结论**：

- [kernels/manual/a2a3/tget_bandwidth/README.md:L75-L82](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/tget_bandwidth/README.md#L75-L82)——A2/A3 上 float、2 卡点对点实测表：

| 传输量 | TGET 带宽 | TGET_ASYNC 带宽 | TGET 设备周期 | ASYNC 设备周期 |
| ---: | ---: | ---: | ---: | ---: |
| 4 KB | 0.20 GB/s | 0.18 GB/s | 36.28 | 136.51 |
| 16 KB | 0.77 GB/s | 0.64 GB/s | 149.98 | 161.40 |
| 64 KB | 1.50 GB/s | **2.44 GB/s** | 579.62 | 277.25 |
| 256 KB | 2.40 GB/s | **7.90 GB/s** | 2409.72 | 745.54 |
| 1 MB | 2.63 GB/s | **8.98 GB/s** | 9317.93 | 2624.24 |
| 4 MB | 4.85 GB/s | **10.49 GB/s** | 37924.12 | 10273.68 |

- [kernels/manual/a2a3/tget_bandwidth/README.md:L84-L89](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/tget_bandwidth/README.md#L84-L89)——官方分析：TGET 随传输量增大但饱和在 ~4 GB/s（UB 通路天花板）；TGET_ASYNC 在 ≥256 KB 显著领先，4 MB 时约 10.5 GB/s；4 KB 小块反而略慢（SDMA 启动开销）。设备周期口径下 4 MB 差距约 3.7 倍（37924 vs 10274）。
- [kernels/manual/a2a3/tget_bandwidth/README.md:L31-L41](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/tget_bandwidth/README.md#L31-L41)——README 用两张单行图给出数据流对比，以及 SDMA 完成协议（post ID、8 字节 flag SQE、64 槽 payload、host 侧 `SdmaWorkspaceManager`）的概述——与本讲 4.2 的源码分析一一对应。
- 构建与运行方式（真机）：[kernels/manual/a2a3/tget_bandwidth/README.md:L99-L148](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/tget_bandwidth/README.md#L99-L148)——需要 CANN ≥ 8.5.0（TGET）/ ≥ 9.0.0（TGET_ASYNC）、MPICH（不支持 OpenMPI）与至少 2 张昇腾 NPU，`bash run.sh -r npu -v a3 -n 2` 一键运行。

#### 4.3.4 代码实践

**实践目标**：阅读 tget_bandwidth 的 README 与源码，亲手写出发起一次异步远读的完整指令序列，并预测大块传输下哪条通路更快——这正是本讲规格里指定的实践任务。

**操作步骤**：

1. 通读 [kernels/manual/a2a3/tget_bandwidth/README.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/tget_bandwidth/README.md) 的 Overview 与 Data Flow 两节，抄下两条通路的数据流图。
2. 精读 `ProfileTGetBandwidthKernel`（[L190-L240](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/tget_bandwidth/tget_bandwidth_kernel.cpp#L190-L240)）与 `ProfileTGetAsyncBandwidthKernel`（[L282-L335](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/tget_bandwidth/tget_bandwidth_kernel.cpp#L282-L335)），逐行列出两者的差异点（提示：tile 类型与尺寸、session 构建、调用与等待方式、barrier 位置）。
3. 画一张对比图：横轴为「远端 GM → 本地 GM」路径上的部件，分别画出 TGET（经过 UB staging tile、TLOAD/TSTORE 双跳）与 TGET_ASYNC（SQE + doorbell + Done Record 轮询）两条链路。
4. **写下你的预测**：基于 4.3.2 的带宽模型，预测 (a) 大块（≥256 KB）哪条快、快几倍量级；(b) 小块（4 KB）哪条快；(c) 交叉点大概在哪个档位。
5. 翻到 [README.md:L75-L89](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/tget_bandwidth/README.md#L75-L89) 的实测表核对预测。
6. （可选，真机）按 [README.md:L99-L148](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/tget_bandwidth/README.md#L99-L148) 复现测量，再用 `device_baseline` 模式（[L161-L187](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/tget_bandwidth/README.md#L161-L187)）改变 `QUEUE_NUM`/`POST_COUNT` 观察多队列与多 Post 的影响。

**需要观察的现象**：两版计时核的差异集中在三处——数据 tile（1024 元素）换成控制 scratchTile（256 字节）；每次搬运从"TGET + pipe_barrier"变成"TGET_ASYNC + event.Wait(session)"；异步版多出一次性的 `BuildAsyncSession` 与 host 侧的 `SdmaWorkspaceManager::Init`。

**预期结果**：预测应落在——(a) 大块 TGET_ASYNC 显著更快（4 MB 实测约 2.2 倍带宽、3.7 倍设备周期差），因为它绕开了 ~4 GB/s 的 UB 天花板；(b) 4 KB 小块 TGET_ASYNC 略慢（0.18 vs 0.20 GB/s），SDMA 每次 Post 的 SQE/doorbell/轮询协议成本占比过高；(c) 交叉点在 16 KB~64 KB 之间（表中 64 KB 档 ASYNC 已反超）。步骤 6 的真机复现标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：主机侧带宽（4 MB 时 TGET 4.85 GB/s）明显低于设备周期折算的异步带宽口径（10273.68 周期/4 MB），可能的原因有哪些？

**答案**：主机侧计时包含 kernel launch、参数传递与流同步等固定开销（`RunSingleBandwidthCase` 中 `NowMs()` 夹住的是整圈 launch 循环），而设备周期只计 kernel 内计时区（`GetSyscnt()` 之间），二者口径天然不同；此外主机侧还受 launch 频率限制，小块时尤其失真。这正是该示例同时报告两套指标的原因（[README.md:L53-L56](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/tget_bandwidth/README.md#L53-L56)）。

**练习 2**：如果把 tget_bandwidth 的 staging tile 从 `1×1024` 改成 `1×4096` 元素（UB 容量允许），TGET 的饱和带宽会突破 4 GB/s 吗？

**答案**：不会有质变。UB 通路每次搬运要走"远端 GM → UB → 本地 GM"两跳，瓶颈是 UB 的读写吞吐与 TLOAD/TSTORE 的排队，加大 tile 只能减少循环次数、摊薄每次同步开销，对线速上限无益；且 tile 变大后单次 TLOAD/TSTORE 占用流水线更久，乒乓深度不足时反而可能变慢。要突破天花板必须换通路——即 TGET_ASYNC/SDMA。此结论可上真机验证，「待本地验证」。

**练习 3**：示例要求 MPICH 而不支持 OpenMPI（[README.md:L103-L120](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/tget_bandwidth/README.md#L103-L120)），这暴露了什么工程约束？

**答案**：host 侧的 `comm_mpi.h` 硬编码了 MPICH 的 `MPI_COMM_WORLD` 句柄值，即仓库把某个具体 MPI 实现的 ABI 约定固化进了代码。OpenMPI 对通信子使用不同的内部表示，混用时运行期 MPI 调用可能失败。这提醒二次开发者：示例工程为了轻量省掉了 MPI 动态探测，移植到其他 MPI 实现时需要先适配这一层。

## 5. 综合实践

**任务：为「多 expert 权重预取」设计一个异步通信方案并写成伪代码。**

场景：MoE 推理时，本核在下一次 GEMM 计算之前，需要从 3 张peer 卡各预取 1 MB 的 expert 权重到本地 GM；计算与预取要尽量重叠。请完成：

1. **选型**：对比同步 TGET 循环与 TGET_ASYNC 方案，用本讲的带宽模型说明为什么选后者（提示：1 MB 已在拐点之上，且需要与计算重叠）。
2. **通路图**：画出发 3 个 Post 时的 SQE 布局——默认 `queue_num=1`、`block_bytes=1 MB` 时每个 Post 各产生几个数据 SQE 与 flag SQE？同一 session 连发 3 个 Post 的 quiet 语义如何保证只需一次 Wait？
3. **伪代码**：参考 [TGET_ASYNC_zh.md:L174-L205](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/comm/TGET_ASYNC_zh.md#L174-L205) 的批量传输示例与 `ProfileTGetAsyncBandwidthKernel` 的骨架，写出「BuildAsyncSession → 循环 {先发下一轮的 TGET_ASYNC，再做本轮 TMATMUL 计算，最后 event.Wait}」的软件流水伪代码，并标注：scratchTile 声明（256 字节 Vec tile）、事件等待位置、以及为什么 Wait 要放在计算之后而不是每次 Post 之后。
4. **核对约束**：逐条检查 ISA 文档的约束清单（一维连续、64 个未完成上限、session 所有权、workspace 生命周期），确认你的方案没有踩线。
5. **（可选，真机）**：把伪代码落成真 kernel，用 tget_bandwidth 的 `device_baseline` 模式思路测多 Post 场景的完成时间（`TGET_DEVICE_BASELINE_POST_COUNT=3`），验证一次 Wait 覆盖三个 Post 的行为。标注「待本地验证」。

参考要点：第 2 步答案为每个 Post 1 个数据 SQE + 1 个 flag SQE（1 MB 恰好一个块），三个 Post 共 6 个 SQE 落同一队列，postId 为 1/2/3；只 Wait postId=3 的事件即可，因为 flag SQE 顺序执行且 postQueueCount 覆盖历史队列。第 3 步的关键是"预取领先计算一轮"——这正是 u7-l5 gemm_ar 融合算子的核心模式，可作为下一讲的预习。

## 6. 本讲小结

- **异步语义**：`TGET_ASYNC`/`TPUT_ASYNC` 由独立 DMA 引擎做 GM↔GM 直达搬运（不经过 UB staging tile），调用立即返回 `AsyncEvent`，完成靠 `Wait`（阻塞轮询）/`Test`（非阻塞探测）；操作数必须是同类型、同 layout 的扁平连续一维 GlobalTensor 视图。
- **三件套与 quiet 语义**：`BuildAsyncSession()`（scratchTile + host 侧 `SdmaWorkspaceManager` 分配的 workspace）→ 异步指令 → 事件等待；SDMA 用单调递增 post ID + 覆盖所有历史队列的 flag SQE，保证等最后一个事件即等全部（同一 session 最多 64 个未完成操作）。
- **引擎分工**：`DmaEngine`（SDMA：A2/A3/A5 通用，SQE+doorbell；URMA：仅 NPU_ARCH 3510 的 RDMA 引擎，WQE/CQE，单次上限 256 MB）管点对点异步；`CollEngine::CCU`（A5 独有，CKE 门触发）管集合通信。引擎全部编译期选择，误用在 `static_assert`/架构宏处拦截。
- **SDMA 协议**：Post = 数据 SQE（按 `block_bytes` 分块、按 `queue_num` 轮转多队列）+ flag SQE（把 postId 写到各队列 Done Record）+ doorbell；Wait/Test 只轮询内存标志、不提交 SQE；通道组共 48 个，默认按 `get_block_idx()` 自动隔离。
- **带宽结论**：UB 中转的同步 TGET 饱和于 ~4 GB/s；SDMA 直达的 TGET_ASYNC 在 ≥64 KB 反超、4 MB 时约 10.5 GB/s（约 2.2 倍），但 4 KB 小块因 Post 协议固定开销反而略慢——选型要看传输量落点。
- **后端边界**：异步指令只有真机实现，CPU 仿真后端（`pto_comm_instr_impl.hpp` 的 `__CPU_SIM` 分支）不含 async 头文件，功能验证与性能测量都必须上真机。

## 7. 下一步学习建议

- **u7-l5 计算通信融合实战：GEMM + AllReduce 融合算子**：把本讲的"异步预取 + 计算重叠"模式套上完整算子，观察 gemm_ar 如何用通信-计算流水线隐藏 AllReduce 延迟——本讲综合实践的第 3 步就是它的预习。
- **真机复现 tget_bandwidth**：若你有 2 卡以上昇腾环境，跑通 `run.sh -r npu -v a3 -n 2` 并用 `device_baseline` 模式扫描 `QUEUE_NUM`/`POST_COUNT`，把实测数据与 [README.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/tget_bandwidth/README.md) 的参考表对比。
- **延伸阅读**：`include/pto/comm/async/urma/` 下的 `urma_async_intrin.hpp` 与 `urma_workspace_manager.hpp`（URMA 的 WQE/CQE 协议与 SDMA 的 SQE 协议对照着读，能看到同一抽象下两种硬件的映射差异）；`include/pto/comm/async/ccu/` 下的 `ccu_reduce_kernel.hpp` 等（CCU 集合通信的触发内核）。
- **回顾串联**：若对完成检测的内存序细节（dcci/dsb/pipe_barrier）感到生疏，回到 u7-l2 的信号同步一节复习；若对 scratchTile 的 TASSIGN 绑定不熟，回到 u3-l2。

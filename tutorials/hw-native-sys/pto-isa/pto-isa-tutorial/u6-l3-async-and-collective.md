# 异步通信与集合通信（u6-l3）

## 1. 本讲目标

学完本讲，你应该能够：

1. 区分同步通信（TPUT/TGET 经 UB staging tile 中转）与异步通信（`*_ASYNC` 由 DMA 引擎 GM 直达）两条路径的事件模型差异。
2. 掌握 `AsyncSession` / `AsyncEvent` 运行时三件套中剩余两件的使用方式：`BuildAsyncSession` 一次构建、`event.Wait(session)` 阻塞等待与 `Test(session)` 非阻塞探测。
3. 理解 `TPUT_ASYNC_NOTIFY` 的「先写载荷、后置信号」语义，掌握 `Set` 与 `AtomicAdd` 两种通知形式的适用场景，以及 `peer` 参数在 SDMA / URMA / RDMA 三种后端下的不同作用（本版本起 RDMA 后端落地，peer 语义注释同步更新）。
4. 掌握 `TNOTIFY`/`TWAIT`/`TTEST` 信号三指令的配对规则，能写出正确的「发送方置信号 → 接收方等待」序列。
5. 读懂 `demos/baseline/allgather_async` 中三种 allgather 算法（多核 TPUT、多核 TGET、环算法）的多 rank 数据依赖组织，并能对比 SDMA 与 URMA 两条底层传输路径的实现差异。

## 2. 前置知识

本讲默认你已学习 u6-l1（通信 ISA 总览）与 u6-l2（点对点通信），已经知道：

- **rank / peer**：参与通信的 NPU 编号。本 rank 自己的编号叫 rank（URMA 场景下也称 pe），通信目标叫 peer。
- **staging tile**：同步 TPUT/TGET 中数据必经的 UB 中转 tile；而异步指令的数据在 GM 之间由 DMA 引擎直达，UB 里只放控制元数据（scratch tile）。
- **四象限分类**：同步点对点（TPUT/TGET）、集合（TGATHER/TSCATTER/TBROADCAST/TREDUCE）、信号（TNOTIFY/TWAIT/TTEST）、异步（`*_ASYNC`）。
- **事件同步**：PTO 内核内用 `set_flag`/`wait_flag` 表达流水线间依赖（见 u3-l1）；异步通信的完成等待是另一套机制（`AsyncEvent::Wait(session)`），不要混淆。

再补充三个本讲要用的术语：

- **DMA 引擎（DmaEngine）**：执行 GM↔GM 搬运的硬件通道。PTO 目前有 SDMA、URMA、RDMA 三种，编译期由模板参数选择。
- **MR（Memory Region）**：RDMA 类传输要求内存事先「注册」，注册后的地址范围才能被远端直接读写。URMA/RDMA 的 workspace 由 host 侧完成注册，这是它们与 SDMA 的关键差异。
- **quiet 语义**：借用 shmem 术语——对一次批量提交的多个异步操作，只需 `Wait` 最后一个事件，即可排空（drain）此前所有未完成操作。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [include/pto/comm/pto_comm_inst.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/pto_comm_inst.hpp) | 通信指令公共声明层：TPUT/TGET/TNOTIFY/TWAIT/TTEST/集合指令以及本讲主角 TPUT_ASYNC / TPUT_ASYNC_NOTIFY / TGET_ASYNC |
| [include/pto/comm/comm_types.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/comm_types.hpp) | 通信公共类型：`NotifyOp`/`WaitCmp`/`ReduceOp`/`DmaEngine` 枚举、`AsyncEvent`、`Signal`、`ParallelGroup` |
| [include/pto/comm/async_common/async_types.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/async_common/async_types.hpp) | 异步运行时类型：`AsyncSession`（引擎无关会话）与 SDMA 常量/配置 |
| [include/pto/comm/pto_comm_instr_impl.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/pto_comm_instr_impl.hpp) | 通信装配层：按 A2A3/A5/CPU 三宏互选拉入各后端实现头 |
| [pkg_inc/pto/comm/a5/async/TPutAsyncNotify.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/a5/async/TPutAsyncNotify.hpp) | A5 的 TPUT_ASYNC_NOTIFY 实现：SDMA 名义路径的 MTE 回退、URMA 路径与本版本新增的 RDMA 路径 |
| [demos/baseline/allgather_async/csrc/kernel/allgather_kernel.cpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/demos/baseline/allgather_async/csrc/kernel/allgather_kernel.cpp) | SDMA 引擎（A2/A3）的三个 allgather 内核与 host 驱动 |
| [demos/baseline/allgather_async/csrc/kernel/allgather_urma_kernel.cpp](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/demos/baseline/allgather_async/csrc/kernel/allgather_urma_kernel.cpp) | URMA 引擎（A5）的同构三个 allgather 内核与 host 驱动 |
| [docs/isa/comm/TPUT_ASYNC.md](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/docs/isa/comm/TPUT_ASYNC.md) / [docs/isa/comm/TPUT_ASYNC_NOTIFY.md](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/docs/isa/comm/TPUT_ASYNC_NOTIFY.md) / [docs/isa/comm/TREDUCE.md](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/docs/isa/comm/TREDUCE.md) | 三条指令的 ISA 文档（TPUT_ASYNC_NOTIFY 自本版本起拥有独立文档） |

## 4. 核心概念与源码讲解

### 4.1 异步指令族：TPUT_ASYNC / TGET_ASYNC 与 AsyncSession

#### 4.1.1 概念说明

u6-l2 讲过，同步 TPUT/TGET 的数据路径是 `GM → UB staging tile → 远端 GM`，指令内部复用 MTE 流水线逐块滑窗搬运。这条路径的代价是：数据要在片上绕一圈，且指令结束即表示搬运完成（同步语义）。

异步指令族换了引擎也换了编程模型：

- **数据路径**：`本地 GM → DMA 引擎 → 远端 GM`，不经过用户数据 tile。UB 里只保留一个很小的 scratch tile 存放控制字（队列尾指针、完成标志等），它不是载荷缓冲。
- **完成模型**：指令提交后立刻返回一个 `AsyncEvent`，完成与否由调用方择机 `Wait`/`Test`。这使「提交 N 个搬运、最后统一等待」（quiet 语义）成为可能，是计算通信重叠的基础。
- **会话模型**：所有异步指令共享一个 `AsyncSession`——它把引擎类型、workspace 指针、队列索引等杂项打包，`BuildAsyncSession` 构建一次、全程传递，`Wait` 时必须用同一个 session。

#### 4.1.2 核心流程

一次异步搬运的标准生命周期：

```text
host 侧：分配 workspace（SdmaWorkspaceManager / UrmaWorkspaceManager / RDMA 初始化流程）
   ↓
device 侧：
1. TASSIGN(scratchTile, 0x0)          // 绑定 UB scratch（控制元数据用）
2. BuildAsyncSession<engine>(...)      // 构建会话，失败返回 false
3. ev1 = TPUT_ASYNC(dstG, srcG, session)   // 提交换入 SQ/WQE，立即返回
4. ev2 = TPUT_ASYNC(...)                    // 可继续提交（同 session 同队列按序完成）
5. ev_last.Wait(session)              // quiet：排空自上次 Wait 以来的所有操作
```

注意第 3、4 步返回的 `AsyncEvent` 不能作为后续指令的 `WaitEvents` 变参传入——它的等待方法是 `Wait(session)` 而非无参 `Wait()`，必须显式等待。

#### 4.1.3 源码精读

公共声明层中，`TPUT_ASYNC` 的基础重载（无 peer）：

> [include/pto/comm/pto_comm_inst.hpp:343-349](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/pto_comm_inst.hpp#L343-L349) — `TPUT_ASYNC` 模板：编译期选 `DmaEngine`，等待前置事件后转发给 `TPUT_ASYNC_IMPL`，返回 `AsyncEvent`。这是典型的「wrapper 只做三件事」：等待事件、转发操作数、返回事件。

带 `peer` 的重载只在 A5 与 CPU 模拟器下可见：

> [include/pto/comm/pto_comm_inst.hpp:351-367](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/pto_comm_inst.hpp#L351-L367) — 注释写明了 peer 的三种待遇：**URMA 与 RDMA 用 peer 选择「按对端划分的队列」与内存元数据（session 不必绑定对端）；SDMA 忽略 peer，远端地址直接来自 GlobalTensor 的 VA；CPU 模拟器忽略 peer**。`TGET_ASYNC` 的 peer 重载（[include/pto/comm/pto_comm_inst.hpp:411-427](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/pto_comm_inst.hpp#L411-L427)）规则完全相同。

引擎枚举定义：

> [include/pto/comm/comm_types.hpp:122-126](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/comm_types.hpp#L122-L126) — `DmaEngine`：`SDMA`（支持 2D 传输）、`URMA`（1D 传输，HCCP V2 Jetty，仅 NPU_ARCH 3510）、`RDMA`（由 `RdmaBackend` 标识编译进二进制的网卡实现，如 HNS1825）。

会话类型：

> [include/pto/comm/async_common/async_types.hpp:102-125](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/async_common/async_types.hpp#L102-L125) — `AsyncSession` 是引擎无关的会话：既有 SDMA 通用字段（workspace、syncId、channelGroupIdx、blockBytes），也有 URMA 的 `destRankId`/`qpIdx` 与 RDMA 的 `rdmaBackend`/`myPe`。注释特别说明 SDMA 运行时状态 `sdmaRuntimeCtx` 存在 session 里、经 const 引用也会被修改——因为 SDMA 后端要把 postId 等进度跨多次 Post/Wait 传递。

quiet 语义的权威表述：

> [docs/isa/comm/TPUT_ASYNC.md:157-173](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/docs/isa/comm/TPUT_ASYNC.md#L157-L173) — `event.Wait(session)` 阻塞直到**自上次 Wait 以来发出的所有异步操作**完成：SDMA 轮询标志位、URMA 轮询 CQ、RDMA 按 peer 的队列独立跟踪。一个 session 最多 64 个在途操作，之后提交会产生背压。

约束方面（[docs/isa/comm/TPUT_ASYNC.md:121-136](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/docs/isa/comm/TPUT_ASYNC.md#L121-L136)）：源/目的 dtype 与 layout 必须一致；当前 SDMA/URMA 路径只支持**扁平连续逻辑 1D** 张量，不满足时返回 `handle == 0` 的无效事件；RDMA 单次传输不得超过 `0x7fffffff` 字节。

#### 4.1.4 代码实践

**实践 A：读通 SDMA 批量提交示例（源码阅读型）**

1. 实践目标：理解 quiet 语义下「多次提交、一次等待」的写法。
2. 操作步骤：打开 [docs/isa/comm/TPUT_ASYNC.md:219-250](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/docs/isa/comm/TPUT_ASYNC.md#L219-L250) 的 `BatchPut` 示例，观察它如何在一个循环里对 N 个 rank 各发一次 `TPUT_ASYNC`，只保留最后一个事件，循环外一次 `Wait`。
3. 需要观察的现象：循环体内没有任何等待；`lastEvent` 每轮被覆盖。
4. 预期结果：你能回答「为什么覆盖前面的 event 不会泄漏未完成操作」——因为同一 session 的操作按提交顺序完成，等待最后一个即覆盖全部。
5. 若要真机验证，可在多 NPU 环境运行 allgather demo（见 4.5）；无环境则本任务为纯阅读，无需标注待验证。

**实践 B：运行 allgather demo（真机型，可选）**

按 [demos/baseline/allgather_async/README.md:19-23](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/demos/baseline/allgather_async/README.md#L19-L23)：`source set_env.sh` 后 `./run.sh 2`（SDMA，A2/A3）或 `./run.sh 2 Ascend950PR_9599`（URMA，A5）。需要 CANN ≥ 9.0.0 与 MPICH。预期输出为各 rank 打印 `slot[0]=[0,1,2,...] slot[1]=[1000,1001,1002,...]`（见 README 的 Expected Output 节）。无 NPU 环境时标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：同一个内核里先用默认 SDMA 引擎发了一次 `TPUT_ASYNC`，又用 `TPUT_ASYNC<URMA>` 发了一次，合法吗？
答案：编译上两个模板实例化都存在，但两次调用各自需要**匹配引擎的 session**（`BuildAsyncSession<URMA>` 构建的会话带 URMA 元数据）。用 SDMA session 调 URMA 指令会在实现层断言失败（如 A5 notify 实现里的 `session.engine == DmaEngine::RDMA` 检查，见 4.2.3）。此外 URMA 仅在 NPU_ARCH 3510 可用。

**练习 2**：为什么 `AsyncEvent` 不能塞进下一条指令的 `WaitEvents...` 变参？
答案：变参要求每个事件提供无参 `Wait()`，而 `AsyncEvent` 只有 `Wait(session)`（完成判定依赖发起时的 session/引擎）。ISA 文档明确写了这条限制（[docs/isa/comm/TPUT_ASYNC_NOTIFY.md:50-52](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/docs/isa/comm/TPUT_ASYNC_NOTIFY.md#L50-L52)）。

**练习 3**：`scratchTile` 里放的是用户数据吗？
答案：不是。它是控制元数据（SDMA 控制字、队列尾指针、完成标志轮询），载荷在 GM 之间直达（[docs/isa/comm/TPUT_ASYNC.md:138-155](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/docs/isa/comm/TPUT_ASYNC.md#L138-L155)）。推荐类型是 `Tile<TileType::Vec, uint8_t, 1, comm::sdma::UB_ALIGN_SIZE>`（256 字节）。

### 4.2 TPUT_ASYNC_NOTIFY：写远端 + 置信号

#### 4.2.1 概念说明

`TPUT_ASYNC_NOTIFY` 把「搬数据」和「打招呼」合并成一条指令：把非空载荷从本地 GM 写到远端 GM，**然后**更新远端一个 32 位信号。返回的 `AsyncEvent` 同时覆盖载荷传输与信号更新两件事的完成。

它解决的问题是**完成通知的去 host 化**。纯 `TPUT_ASYNC` 的完成只有发送方自己知道（本地 Wait）；接收方要想知道「数据到了」，要么靠 host 侧 barrier，要么靠单独的 `TNOTIFY`（两次操作之间没有顺序保证）。`TPUT_ASYNC_NOTIFY` 在一次调用内保证「载荷先于信号」：接收方看到信号跳变，对应载荷必然已落到远端 GM。

本版本两个重要变化：

1. `TPUT_ASYNC_NOTIFY` 拥有了独立的 ISA 文档 `docs/isa/comm/TPUT_ASYNC_NOTIFY.md`（此前语义散落在头文件注释里）。
2. RDMA 传输后端落地：A5 实现从「RDMA 未实现」的 `static_assert` 拒绝，变为真实的 `TPUT_ASYNC_NOTIFY_RDMA` 路径（仅支持 `Set`）。

#### 4.2.2 核心流程

一次调用的执行顺序（ISA 文档规定）：

1. 等待所有 `events` 前置事件；
2. 从 `srcGlobalData` 向 `dstGlobalData` 传输完整载荷；
3. 载荷传输完成后，按 `notifyOp` 更新 `dstSignalData`。

信号更新的两种形式：

\[ \mathrm{Set}:\quad \mathrm{signal}^{\mathrm{remote}} = \mathrm{signalValue} \]

\[ \mathrm{AtomicAdd}:\quad \mathrm{signal}^{\mathrm{remote}} \mathrel{+}= \mathrm{signalValue} \;(\text{原子}) \]

`AtomicAdd` 的原子性**只作用于信号**：多个生产者可并发对同一信号累加，最终增量是各 `signalValue` 之和；但各生产者的**载荷目的地范围不得重叠**，且原子性不覆盖载荷写。多生产者并发 `Set` 同一信号则无最终值保证——要么每生产者一个独立信号槽，要么改用 `AtomicAdd` 做完成计数。

各引擎能力矩阵（来自 ISA 文档）：

| 引擎 | 平台限制 | 支持的 NotifyOp |
|---|---|---|
| `DmaEngine::SDMA`（默认） | A2/A3 与 A5 | Set、AtomicAdd |
| `DmaEngine::URMA` | Ascend950（NPU_ARCH 3510），CANN ≥ 9.1.0 | Set、AtomicAdd |
| `DmaEngine::RDMA` | Ascend950（NPU_ARCH 3510），目前仅 HNS1825 RoCE | **仅 Set** |

#### 4.2.3 源码精读

公共声明（注意它的可见性条件）：

> [include/pto/comm/pto_comm_inst.hpp:384-396](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/pto_comm_inst.hpp#L384-L396) — `TPUT_ASYNC_NOTIFY` 只在 `PTO_NPU_ARCH_A2A3` 或 `PTO_NPU_ARCH_A5` 下声明，**没有 CPU 模拟器实现**（承接 u6-l1 的结论：装配层 `#if` 与声明层必须咬合）。签名比 `TPUT_ASYNC` 多出 `dstSignalData`、`signalValue`、`notifyOp`、`peer` 四个参数。

本版本更新的 peer 语义注释：

> [include/pto/comm/pto_comm_inst.hpp:369-383](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/pto_comm_inst.hpp#L369-L383) — 头注释列出三种架构路径：A2/A3 SDMA 把载荷与信号提交进同一个 SQ（天然保序）；A5 的 SDMA 名义路径实际是「同步 MTE + Scalar SET/AtomicAdd」的回退，返回 handle 为 0 的已完成事件；A5 的 URMA 与 RDMA 用 peer 选择目标队列与注册内存。diff 显示本版本把「URMA 使用 peer」改写为「URMA 与 RDMA 使用 peer；URMA 额外用它选择 notify 资源区」。

A5 实现的三条分流路径（位于 pkg_inc 内部头）：

> [pkg_inc/pto/comm/a5/async/TPutAsyncNotify.hpp:33-50](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/a5/async/TPutAsyncNotify.hpp#L33-L50) — SDMA 名义的 MTE 回退：先调 `TPUT_ASYNC_MTE_FALLBACK` 搬载荷，`dsb(DSB_DDR)` 确保载荷抵达 DDR，`pipe_barrier(PIPE_ALL)` 排序所有流水线，再发 `TNOTIFY_IMPL` 置信号，最后返回 `AsyncEvent(0, SDMA)`——handle 0 表示「已完成」。注释明确了它的顺序约定：同一 AICore 上的重复调用完全串行。

> [pkg_inc/pto/comm/a5/async/TPutAsyncNotify.hpp:69-91](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/a5/async/TPutAsyncNotify.hpp#L69-L91) — **本版本新增的 RDMA 路径**：校验 session 引擎与 payload/信号合法性后，显式拒绝 `AtomicAdd`（`PTO_ASSERT(false, "...Set only.")` 并返回编码了 `kRdmaUnsupportedOperationError` 的事件句柄），限制载荷 ≤ `0x7fffffff` 字节，最后调 `rdma::WriteNotify(session, dst, src, len, remoteSignal, signalValue, peer)` 提交写+信号。

> [pkg_inc/pto/comm/a5/async/TPutAsyncNotify.hpp:95-124](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/a5/async/TPutAsyncNotify.hpp#L95-L124) — `TPUT_ASYNC_NOTIFY_IMPL` 的编译期分发：SDMA → MTE 回退（忽略 peer）；URMA/RDMA 分别受 `PTO_URMA_SUPPORTED` / `PTO_RDMA_SUPPORTED` 宏保护，未开启时 `static_assert` 直接拦截。这正是「模板参数选引擎、宏守卫拦平台」的双层选择。

RDMA 底层入口（下一讲 u6-l5 的入口，这里只看接缝）：

> [pkg_inc/pto/comm/async/rdma/rdma_async_intrin.hpp:172-177](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/rdma_async_intrin.hpp#L172-L177) — 本版本新增的 `WriteNotify(session, ..., peer)` 重载：先经 `MakeExecContext(session, peer)`（[pkg_inc/pto/comm/async/rdma/rdma_async_intrin.hpp:100-105](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/rdma_async_intrin.hpp#L100-L105)，把 session 的 contextGm/rdmaBackend 与 peer 打包成 `RdmaExecContext`）注入执行上下文，再委托给 ctx 版实现。

> [pkg_inc/pto/comm/async/rdma/rdma_async_intrin.hpp:147-159](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/rdma_async_intrin.hpp#L147-L159) — ctx 版 `WriteNotify`：按 `ctx.backend` switch 分发到具体网卡后端（当前仅 HNS1825），未编译进二进制的后端返回 `kRdmaBackendUnavailableError` 编码句柄。

装配层解析方式的变化：

> [include/pto/comm/pto_comm_instr_impl.hpp:24](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/pto_comm_instr_impl.hpp#L24) 与 [include/pto/comm/pto_comm_instr_impl.hpp:43](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/pto_comm_instr_impl.hpp#L43) — A2A3 与 A5 两个分支里，`TPutAsyncNotify.hpp` 都改为 `"../../../pkg_inc/pto/comm/<arch>/async/TPutAsyncNotify.hpp"` 相对路径包含（注意 a2a3 的 notify 头同样位于 pkg_inc，与本讲源码地图列出的 A5 版对应）。原因：按 CANN 交付约定，不属于对外交付面的内部头放进 `pkg_inc/`，装配层必须用相对路径「逃出」include/ 才能找到它。这是 u1-l3「pkg_inc 存在不暴露的内部头」在通信模块的具体体现。

#### 4.2.4 代码实践

1. 实践目标：整理 `Set` 与 `AtomicAdd` 两种通知形式的语义差异与工程选型。
2. 操作步骤：
   - 阅读 [docs/isa/comm/TPUT_ASYNC_NOTIFY.md:84-113](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/docs/isa/comm/TPUT_ASYNC_NOTIFY.md#L84-L113)（Operation Semantics 一节）与 [docs/isa/comm/TPUT_ASYNC_NOTIFY.md:272-358](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/docs/isa/comm/TPUT_ASYNC_NOTIFY.md#L272-L358)（SDMA Set / SDMA AtomicAdd / URMA Set / URMA AtomicAdd 四个示例）。
   - 对照 [pkg_inc/pto/comm/a5/async/TPutAsyncNotify.hpp:79-83](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/a5/async/TPutAsyncNotify.hpp#L79-L83) 中 RDMA 对 AtomicAdd 的显式拒绝。
3. 需要观察的现象：四份示例代码里，session 构建完全相同，只有 `NotifyOp` 枚举不同；RDMA 则根本没有 AtomicAdd 示例。
4. 预期结果：写出一张三行小结表——「单生产者一次性通知 → Set；多生产者完成计数（SDMA/URMA）→ AtomicAdd；RDMA → 只能 Set，计数需应用层另做」。同时记下文档的两条硬约束：载荷目的地不得与信号重叠、URMA/RDMA 的载荷与信号必须属于同一 peer 且都落在已注册内存区域内。
5. 真机行为「待本地验证」（RDMA 路径需 Ascend950 + HNS1825 网卡）。

#### 4.2.5 小练习与答案

**练习 1**：接收方看到信号变成 1，能立刻读载荷吗？
答案：语义上载荷已抵达远端 GM（信号在载荷之后）。但 ISA 文档的 Completion Semantics 提醒（[docs/isa/comm/TPUT_ASYNC_NOTIFY.md:243-245](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/docs/isa/comm/TPUT_ASYNC_NOTIFY.md#L243-L245)）：指令不保证接收方**已缓存的旧载荷副本**失效，读之前要按目标平台的内存一致性规则保证可见性（如先做一次栅栏/重新加载）。

**练习 2**：为什么 A5 的「SDMA 名义路径」要 `dsb(DSB_DDR)` 加 `pipe_barrier(PIPE_ALL)`？
答案：A5 该路径是软件回退——载荷走 MTE、信号走 Scalar 流水线，两者在不同流水线上异步执行；必须先确保载荷真正落到 DDR（dsb），再对所有流水线排序（pipe_barrier），才能保证「载荷先于信号」这一指令级承诺（[pkg_inc/pto/comm/a5/async/TPutAsyncNotify.hpp:43-48](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/a5/async/TPutAsyncNotify.hpp#L43-L48)）。这与 u3-l1「跨流水线依赖必须显式表达」一脉相承。

**练习 3**：`peer` 在三种引擎下分别怎么用？
答案：SDMA 忽略（远端 VA 来自 GlobalTensor）；URMA 用 peer 选按对端划分的队列、内存元数据，并额外选择 notify 资源区；RDMA 用 peer 选对端队列与注册内存（[include/pto/comm/pto_comm_inst.hpp:377-383](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/pto_comm_inst.hpp#L377-L383)）。

### 4.3 异步事件与 TNOTIFY/TWAIT 的配对

#### 4.3.1 概念说明

异步通信里有**两套「等待」**，层次不同，不能混用：

| 等待 | 对象 | 方法 | 谁在等 |
|---|---|---|---|
| `AsyncEvent::Wait(session)` | 本 rank 提交的 DMA 操作完成 | 轮询 SQ 标志/CQ/CQE | 发送方（本地完成） |
| `TWAIT(signal, cmpValue, cmp)` | 远端写入的 GM 信号值 | 忙等 GM 轮询 | 接收方（远端到达） |

第一套解决「我发出去的数据写完了吗」；第二套解决「对端给我的数据到了吗」。`TPUT_ASYNC_NOTIFY` 恰好把两者串起来：发送方 `Wait` 自己的 AsyncEvent（本地提交完成），接收方 `TWAIT` 信号（远端数据到达）。

信号三指令（u6-l1 已给出分类，这里讲配对）：

- `TNOTIFY(dstSignalData, value, op)`：向远端信号写值（Set）或原子累加（AtomicAdd），无数据载荷。
- `TWAIT(signalData, cmpValue, cmp)`：阻塞直到信号满足比较条件。
- `TTEST(signalData, cmpValue, cmp)`：非阻塞探测，返回 bool。

信号类型强制为 `int32_t`。`comm::Signal` 是单元素信号的别名（[include/pto/comm/comm_types.hpp:203](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/comm_types.hpp#L203)）；`Signal2D<Rows, Cols>` 则把信号排成矩阵，`TWAIT`/`TTEST` 对矩阵要求**全部元素**满足条件。

#### 4.3.2 核心流程

标准的「写完通知」接收方模板（ISA 文档给出的 Receiver 示例）：

```text
发送方（rank A）：
  ev = TPUT_ASYNC_NOTIFY(dstG, srcG, remoteSignal, 1, NotifyOp::Set, session, peer)
  ev.Wait(session)                  // 可选：确保本轮提交完成

接收方（rank B）：
  comm::Signal ready(localSignalPtr);
  TWAIT(ready, 1, comm::WaitCmp::EQ);   // 忙等信号 == 1
  // 按平台一致性规则保证载荷可见后，再读载荷
```

配对规则：

1. `TNOTIFY`/`TPUT_ASYNC_NOTIFY` 的 `dstSignalData` 与 `TWAIT` 的 `signalData` 必须指向**同一远端 GM 地址**（host 侧经通信窗口/HCCL 把同一地址翻译给双方）。
2. 写入的值要与比较条件匹配：`Set + 1` 配 `WaitCmp::EQ, 1`；`AtomicAdd + 1` 通常配 `GE, N`（N 个生产者各加 1）。
3. 信号由调用方分配并初始化（构造 `Signal` 只是包指针，不分配不初始化）。
4. 接收方在进入等待前要保证信号初值已知（demo 里 host 侧把 recvBuf 全部填 -1 就是同一思想）。

#### 4.3.3 源码精读

`AsyncEvent` 类型：

> [include/pto/comm/comm_types.hpp:178-191](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/comm_types.hpp#L178-L191) — `AsyncEvent` 只有三个字段：`handle`（0 表示无效/已完成）、`engine`（等待时要按引擎选择轮询方式）、`urmaTargetCqe`（URMA 专用目标 CQE 计数）。方法 `valid()` 判 handle，`Wait(session)`/`Test(session)` 必须传入发起时的同一个 session。

信号三指令的公共声明：

> [include/pto/comm/pto_comm_inst.hpp:108-113](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/pto_comm_inst.hpp#L108-L113) — `TNOTIFY`：等待前置事件后转发 `TNOTIFY_IMPL`，无返回事件（信号写本身经 Scalar/MTE 完成，由流水线保序）。

> [include/pto/comm/pto_comm_inst.hpp:123-128](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/pto_comm_inst.hpp#L123-L128) — `TWAIT`：阻塞等待；注释说明对信号矩阵是「全部满足」语义。

> [include/pto/comm/pto_comm_inst.hpp:137-142](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/pto_comm_inst.hpp#L137-L142) — `TTEST`：非阻塞探测版本，返回 bool。

两个支撑枚举：

> [include/pto/comm/comm_types.hpp:90-106](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/comm_types.hpp#L90-L106) — `NotifyOp`（AtomicAdd/Set）与 `WaitCmp`（EQ/NE/GT/GE/LT/LE 六种比较）。注意 `NotifyOp::AtomicAdd` 枚举值为 0、`Set` 为 1，书写时不要依赖默认值。

#### 4.3.4 代码实践

1. 实践目标：把「两套等待」的作用域写清楚，避免初学者最常犯的混用。
2. 操作步骤：在 [demos/baseline/allgather_async/csrc/kernel/allgather_kernel.cpp:73-83](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/demos/baseline/allgather_async/csrc/kernel/allgather_kernel.cpp#L73-L83) 中找到 `event.Wait(session)`，回答三个问题：(a) 它等的是远端收到数据，还是本地提交完成？(b) 若把这里的 Wait 删掉，程序结果在什么条件下仍然正确？(c) 接收方如果想确认数据到达，应该改用哪条指令？
3. 需要观察的现象：demo 的正确性其实并不依赖这个 `Wait`——host 侧 `HcclHostBarrier` + `aclrtSynchronizeStream` 兜底了全局可见性。
4. 预期答案：(a) 本地提交完成（SDMA 语义上还覆盖同 session 更早的操作）；(b) 只要 stream 同步与 host barrier 仍在，删除后输出不变——`Wait` 在这里是防御性写法；(c) `TPUT_ASYNC_NOTIFY` + 对端 `TWAIT`，或单独 `TNOTIFY` + `TWAIT`。
5. 真机行为差异（如删除后是否偶发失败）「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：8 个生产者 AIV 各搬运一块数据到同一接收方，接收方想等全部到齐再开工，信号方案怎么设计？
答案：SDMA/URMA 下每块用一个 `AtomicAdd(1)` 到共享信号，接收方 `TWAIT(signal, 8, WaitCmp::GE)`；或每生产者一个独立信号槽 + `Signal2D` 矩阵，`TWAIT` 对矩阵「全部满足」。RDMA 下只能 `Set`，必须用独立信号槽方案（AtomicAdd 不可用）。

**练习 2**：`TTEST` 相比 `TWAIT` 适合什么场景？
答案：需要**轮询推进本核其他工作**的场景——`TTEST` 不阻塞，可在循环里穿插计算/搬运，信号未到先做别的；`TWAIT` 则是纯自旋等待。二者参数完全同构（[include/pto/comm/pto_comm_inst.hpp:123-142](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/pto_comm_inst.hpp#L123-L142)）。

### 4.4 集合通信模式：TREDUCE 与「组合出的 allgather」

#### 4.4.1 概念说明

PTO 的集合指令（TGATHER/TSCATTER/TBROADCAST/TREDUCE）在 u6-l1 已经分类过，本讲聚焦两点：

1. **TREDUCE 的组织方式**是集合指令的典型样板：`ParallelGroup` 把各 rank 的源缓冲打包成一张表，**只有 root 执行**指令，非 root 只需保证源数据就绪。数据面是 root 用 acc/recv 两个 UB tile 逐 rank 拉取、在本地逐元素规约。
2. **allgather 不是一条 PTO 指令**。demo 用点对点异步指令把它**组合**出来。这是 PTO 设计哲学的体现：ISA 只提供原语，上层模式（环算法、多核并行收发）由算子作者编排。对比记忆：TREDUCE 是「指令级集合」（一条指令、root 单方执行），allgather_async 是「算法级集合」（N 条点对点指令、全体 rank 执行）。

数学上，TREDUCE 对有效区每个元素做跨 rank 规约：

\[ \mathrm{dst}^{\mathrm{local}}_{i,j} = \bigoplus_{r=0}^{N-1} \mathrm{src}^{(r)}_{i,j} \]

其中 \(N\) 是 rank 数，\(\oplus \in \{\text{Sum}, \text{Max}, \text{Min}\}\)（`ReduceOp` 枚举）。

#### 4.4.2 核心流程

TREDUCE（AIV 引擎）的数据流：

```text
root 侧：
for r in 0..N-1:                    // 指令内部逐 rank 迭代
    TLOAD(recvTile, parallelGroup[r])   // 远端 rank r 的源 → UB
    accTile ⊕= recvTile                 // 按 ReduceOp 逐元素累加/取最值
TSTORE(dstGlobalData, accTile)       // 结果写回本地 GM
（大张量自动 2D 滑窗分块；乒乓重载用 ping/pong 两个接收 tile 重叠收发）
```

而 allgather（组合式）的多核模式是把「N 份点对点」摊到 N 个核上并行执行——每个核负责一个对端，详见 4.5。

#### 4.4.3 源码精读

TREDUCE 的基本与乒乓两个重载：

> [include/pto/comm/pto_comm_inst.hpp:298-336](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/pto_comm_inst.hpp#L298-L336) — 基本版收 `accTileData + recvTileData` 两 tile；乒乓版收 `accTileData + pingTileData + pongTileData` 三 tile。两个重载都支持 `CollEngine::AIV`（默认，tile 路径）与 `CollEngine::CCU`（AIV 触发 CKE 门，CCU 硬件执行集合；首个变参必须是 `CcuTriggerContext`）。

ParallelGroup：

> [include/pto/comm/comm_types.hpp:36-73](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/comm_types.hpp#L36-L73) — 轻量视图：只持「GlobalTensor 数组指针 + nranks + rootIdx」，设备侧零动态分配。`operator[]` 带越界断言。注意 `rootIdx` 是 root **在组内**的下标，全组各 rank 须传同一个值。

指令语义与约束：

> [docs/isa/comm/TREDUCE.md:5-18](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/docs/isa/comm/TREDUCE.md#L5-L18) — 只有 root 执行；非 root 调用是未定义行为。超过单个 UB tile 容量时自动 2D 滑窗分块。

> [docs/isa/comm/TREDUCE.md:57-73](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/docs/isa/comm/TREDUCE.md#L57-L73) — 约束要点：组内张量 dtype 一致、各 rank 源形状步长一致、目的必须在本地；CCU 路径还需全 rank 经 host 侧注册并 launch（与 AIV 的 root-only 不同）。

#### 4.4.4 代码实践

1. 实践目标：为一个 4 rank 的 Sum 规约写出 ParallelGroup 的组织代码（纸面推演）。
2. 操作步骤：阅读 [docs/isa/comm/TREDUCE.md:84-101](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/docs/isa/comm/TREDUCE.md#L84-L101) 的 `reduce_sum` 示例，然后把 `NRANKS` 具体化为 4，画一张表：每行一个 rank，列出「它是否调用 TREDUCE / 它贡献的 tensors 下标 / 它的源缓冲在哪」。host 侧需要把 4 个 rank 的源缓冲地址（经通信窗口翻译后的 VA）填进 root 的 `tensors[4]` 数组。
3. 需要观察的现象：只有 root 一行的「是否调用」是 yes；`rootIdx` 在 4 份代码里必须同值。
4. 预期结果：你能指出示例中 `my_rank` 参数的用途——它就是传给 `ParallelGroup::Create` 的 `rootIdx`，而不是「自己的编号」。
5. 真机运行（需多 NPU + HCCL）「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：TREDUCE 的乒乓重载为什么需要三个 tile？
答案：`accTile` 跨 rank 累加必须常驻；`pingTile/pongTile` 交替接收「下一个 rank 的数据」与「参与当前计算的数据」，让 TLOAD（收下一块）与计算（规约当前块）重叠——与 u3-l3 的乒乓双缓冲是同一手法，只是搬运源从本地 GM 换成了远端 GM。

**练习 2**：allgather 为什么不做成一条像 TREDUCE 那样的指令？
答案：可以做（TGATHER 就是 gather 的指令化），但 allgather 的价值在于每个 rank 都要拿到全量数据，用点对点原语组合能自由选择算法拓扑（多核并行、环、递归倍增），让算子作者按规模与链路带宽权衡——demo 里三种算法共存正说明了这一点。PTO 把「模式」留给上层，「原语」做进 ISA。

### 4.5 多 rank 协作：allgather_async 的三种算法

#### 4.5.1 概念说明

`demos/baseline/allgather_async` 是多 rank 协作的教科书示例：N 个 rank，各贡献 256 个 `int32_t`（rank r 的值 为 \(r \times 1000 + i\)），allgather 后每个 rank 的 `recvBuf` 里都要有全部 N 份数据。demo 提供两种构建（[demos/baseline/allgather_async/README.md:5-8](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/demos/baseline/allgather_async/README.md#L5-L8)）：

- **A2/A3 构建（SDMA，Demos 1–3）**：远端寻址靠 HCCL 通信窗口（`CommRemotePtr`），host 基础设施是 `SdmaWorkspaceManager`。
- **A5 构建（URMA，Demos 4–6）**：远端寻址靠注册内存基址（`UrmaPeerMrBaseAddr`），host 基础设施是 `UrmaWorkspaceManager`。

两套 host 初始化互不兼容，`SOC_VERSION` 决定编译哪套内核。

#### 4.5.2 核心流程

**算法一/二：多核并行收发**。内核以 `<<<nRanks>>>` 启动，把「对每个对端的通信」摊到每个核：

```text
block_idx == myRank  → 本地拷贝：TLOAD(sendBuf) + TSTORE(recvBuf[myRank])（MTE2/MTE3 事件握手）
block_idx != myRank  → 目标 = block_idx
                       算法一(PUT)：TPUT_ASYNC(远端 recvBuf[myRank], 本地 sendBuf)
                       算法二(GET)：TGET_ASYNC(本地 recvBuf[target], 远端 sendBuf)
```

PUT 是每个 rank「推」出自己的数据（N-1 次远端写），GET 是每个 rank「拉」回别人的数据（N-1 次远端读）——通信总量相同，方向相反。

**算法三：环算法（ring）**。N-1 轮，每轮每 rank 只与右邻居 `(myRank+1) % N` 通信：

```text
round 0：本地拷贝 sendBuf → recvBuf[myRank]；并把 sendBuf 推给右邻居的 recvBuf[myRank]
round r：sendChunkIdx = (myRank - r + N) % N
         把 recvBuf[sendChunkIdx]（上一轮收到的块）转发给右邻居的 recvBuf[sendChunkIdx]
每轮一次独立的 kernel launch，轮间用 host 侧 barrier 保证全局完成
```

每轮每个 rank 收到且仅收到一个新块，N-1 轮后每个 rank 集齐全部 N 块（含本地一块）。环算法把每轮的并发通信从「全连接」降为「N 条邻居链路」，是大规模 rank 下的标准拓扑。

#### 4.5.3 源码精读

SDMA 多核 PUT 内核：

> [demos/baseline/allgather_async/csrc/kernel/allgather_kernel.cpp:40-86](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/demos/baseline/allgather_async/csrc/kernel/allgather_kernel.cpp#L40-L86) — `bid == myRank` 分支做本地 TLOAD/TSTORE（注意 `set_flag/wait_flag` 成对出现两次：MTE2→MTE3 保证数据到齐后才写回，MTE3→MTE2 保证写回完成后 tile 才能复用）；else 分支用 `CommRemotePtr(hcclCtx, recvBuf, target)` 把「本地 recvBuf 指针 + 目标 rank」翻译成目标 rank 视角的远端地址，再 `BuildAsyncSession` + `TPUT_ASYNC` + `Wait`。scratch tile 固定 `TASSIGN(scratchTile, 0x0)`。

SDMA 多核 GET 内核：

> [demos/baseline/allgather_async/csrc/kernel/allgather_kernel.cpp:95-141](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/demos/baseline/allgather_async/csrc/kernel/allgather_kernel.cpp#L95-L141) — 结构与 PUT 对称，只是远端张量作源、本地 `recvBuf + srcRank * ELEM_COUNT` 作目的。注意两个内核里本地拷贝分支的代码完全相同——这是「本地 rank 的数据不经网络」的统一处理。

SDMA 环算法内核与轮次注释：

> [demos/baseline/allgather_async/csrc/kernel/allgather_kernel.cpp:313-322](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/demos/baseline/allgather_async/csrc/kernel/allgather_kernel.cpp#L313-L322) — 注释给出了确切的轮次定义与 chunk 公式 `chunk = (i - r + N) % N`，并说明轮间依赖靠 host barrier。

> [demos/baseline/allgather_async/csrc/kernel/allgather_kernel.cpp:361-380](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/demos/baseline/allgather_async/csrc/kernel/allgather_kernel.cpp#L361-L380) — `round == 0` 先分块本地拷贝（elemCount 大于 tile 容量时按 256 一块滑窗），随后计算 `sendChunkIdx`、选源指针（round 0 用 sendBuf，之后用 recvBuf 对应槽）、`TPUT_ASYNC` 推给右邻居并 `Wait`。

host 侧的轮间同步：

> [demos/baseline/allgather_async/csrc/kernel/allgather_kernel.cpp:425-431](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/demos/baseline/allgather_async/csrc/kernel/allgather_kernel.cpp#L425-L431) — 每轮 `aclrtSynchronizeStream`（本 rank 内核结束）+ `HcclHostBarrier`（全 rank 对齐），共同保证「上一轮所有 SDMA 写全局完成」后才进入下一轮。这是当前版本环算法正确性的基石——内核之间没有设备侧依赖表达。

URMA 版本与 SDMA 的三点差异：

> [demos/baseline/allgather_async/csrc/kernel/allgather_urma_kernel.cpp:11-20](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/demos/baseline/allgather_async/csrc/kernel/allgather_urma_kernel.cpp#L11-L20) — 文件头注释言明「镜像 SDMA 三个算法，替换 host 基础设施」：`CommDeviceContext + SdmaWorkspaceManager + CommRemotePtr` 换成 `UrmaWorkspaceManager + UrmaPeerMrBaseAddr + BuildAsyncSession<URMA>`；非 3510 平台上设备侧指令被 `#ifdef PTO_URMA_SUPPORTED` 编译掉，host runner 仍会跑但校验必然失败。

> [demos/baseline/allgather_async/csrc/kernel/allgather_urma_kernel.cpp:90-102](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/demos/baseline/allgather_async/csrc/kernel/allgather_urma_kernel.cpp#L90-L102) — URMA 的远端寻址：`UrmaPeerMrBaseAddr(urmaWorkspace, target)` 取出目标 rank 注册内存的基址 VA，再加上与本地相同的布局偏移（`kDataOffset + ELEM_COUNT + myPeer * ELEM_COUNT`）拼出远端槽位——**对称缓冲布局**是这种「基址 + 偏移」寻址的前提。session 构建传入 `target`（经典 URMA 形式把目标 rank 绑进 session），调用 `TPUT_ASYNC<URMA>` 时无需再传 peer。

> [demos/baseline/allgather_async/csrc/kernel/allgather_urma_kernel.cpp:157-210](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/demos/baseline/allgather_async/csrc/kernel/allgather_urma_kernel.cpp#L157-L210) — URMA 环算法内核：逻辑与 SDMA 版逐行对应，差别只在远端地址来自 `UrmaPeerMrBaseAddr`、session 构建带 `nextPeer`、引擎模板参数为 `DmaEngine::URMA`。

#### 4.5.4 代码实践

1. 实践目标：写出 N=4 时环算法每轮各 rank 的发送块表格，验证你真的读懂了 chunk 公式。
2. 操作步骤：以 `sendChunkIdx = (myRank - round + 4) % 4`、目的 = rank `(myRank+1) % 4` 的 `recvBuf[sendChunkIdx]` 填表（chunk c 表示「rank c 原产的数据」）。
3. 需要观察的现象：每列（每个发送方）三个轮次恰好发出 chunk {myRank, myRank-1, myRank-2}（模 4）；每行（每个接收方）三轮从左邻居收齐其余三块。
4. 预期结果（答案表，chunk 号 → 目的 rank）：

| 轮次 | rank 0 发送 | rank 1 发送 | rank 2 发送 | rank 3 发送 |
|---|---|---|---|---|
| round 0 | chunk 0 → rank 1 | chunk 1 → rank 2 | chunk 2 → rank 3 | chunk 3 → rank 0 |
| round 1 | chunk 3 → rank 1 | chunk 0 → rank 2 | chunk 1 → rank 3 | chunk 2 → rank 0 |
| round 2 | chunk 2 → rank 1 | chunk 3 → rank 2 | chunk 0 → rank 3 | chunk 1 → rank 0 |

   以 rank 1 为接收方自检：本地拷贝得 chunk 1（slot 1）；round 0 收 chunk 0（slot 0）；round 1 收 chunk 3（slot 3）；round 2 收 chunk 2（slot 2）——四个槽集齐。rank 0 作为接收方在 round 0/1/2 依次收到 chunk 3、chunk 2、chunk 1，同样集齐。
5. 该表为纯推演结果，与源码公式一一对应；真机跑 `./run.sh 4` 的校验输出「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：多核 PUT 与多核 GET 在 N=8 时各产生多少次跨 rank 传输？环算法呢？
答案：多核 PUT 与 GET 都是每个 rank 对其余 7 个 rank 各一次，全网 \(8 \times 7 = 56\) 次传输；环算法每轮全网 8 次、共 7 轮，也是 56 次——总通信量相同，差别在**每轮并发链路数**（全连接 56 条 vs 环 8 条）与**流水深度**（环要 7 轮串行，轮间还有 host barrier 开销）。

**练习 2**：URMA 版为什么能直接「peerBase + 固定偏移」算远端地址，而 SDMA 版要调 `CommRemotePtr`？
答案：URMA 路径里各 rank 的通信缓冲是从同一注册内存布局分配的对称窗口，`UrmaPeerMrBaseAddr` 给出对端注册区基址后，偏移即可本地推算；SDMA 路径经 HCCL 通信窗口，各 rank 窗口基址不同，必须查 `CommDeviceContext` 里的映射表（`CommRemotePtr`）做地址翻译。

**练习 3**：如果把环算法的轮间 host barrier 去掉，会发生什么？
答案：某 rank 可能在对端尚未写完上一轮数据时就启动下一轮读取（转发 `recvBuf[sendChunkIdx]` 时该槽可能是旧值/初值 -1），产生数据竞争。SDMA 的 `Wait` 只约束本 rank 本 session 的提交，不构成跨 rank 依赖——这正是本讲综合实践要改造的点。

## 5. 综合实践

**任务：把环算法从「host barrier 驱动」改造为「TPUT_ASYNC_NOTIFY + TWAIT 驱动」，并分析两条路径的事件序列差异。**

分三步完成：

**第一步（表格，纸面）**：完成 4.5.4 的 N=4 发送块表格，并补一张接收方视角表（每 rank 每轮从谁、收哪个 chunk、落哪个 slot）。两张表合起来就是环算法的完整数据依赖图。

**第二步（文档整理，纸面）**：阅读 [docs/isa/comm/TPUT_ASYNC_NOTIFY.md](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/docs/isa/comm/TPUT_ASYNC_NOTIFY.md)，整理三栏笔记：

1. **两种通知形式**：`Set`（赋值，单生产者/独立信号槽）与 `AtomicAdd`（原子累加，多生产者完成计数）；RDMA 仅 Set。
2. **返回的 AsyncEvent 语义**：完成同时覆盖载荷与信号；handle 0 在 A5 SDMA 回退路径表示「已完成」；`Wait(session)`/`Test(session)` 必须用发起时的 session；同一 session 同一队列按提交顺序完成（quiet）。
3. **peer 的三种待遇**：SDMA 忽略、URMA 选队列+元数据+notify 资源区、RDMA 选对端队列与注册内存。

**第三步（改造设计，纸面 + 可选编码）**：设计「通知驱动」的环 allgather：

- 为每个 rank 的每个 chunk 槽配一个 `comm::Signal`（对称窗口中分配，初值 0），发送方把每轮的 `TPUT_ASYNC` 换成 `TPUT_ASYNC_NOTIFY(..., remoteSignal[chunk], 1, NotifyOp::Set, session, peer)`；
- 接收方在转发（round ≥ 1）与最终读取前，先 `TWAIT(signal[chunk], 1, WaitCmp::EQ)`；
- host 侧撤掉轮间 `HcclHostBarrier`，理论上 N-1 轮可合并为一次内核启动（每 rank 内部循环），依赖全部下沉到设备侧信号。

写出两条路径的事件序列对比（示例格式）：

```text
原路径（每轮）：
  kernel(r) 提交 → event.Wait(本 rank 完成) → aclrtSynchronizeStream
  → HcclHostBarrier（全 rank 对齐，host 参与）
  → kernel(r+1) 启动
改造路径（设备侧）：
  TPUT_ASYNC_NOTIFY(chunk, signal) → 对端 TWAIT(signal) 命中 → 对端转发
  （host 只在全部轮次结束后同步一次）
```

分析要点：(a) 原路径的正确性由 host barrier 全局保证，粒度粗、每轮两次 host 往返；(b) 改造路径把依赖降到「每 chunk 每对 rank」的细粒度，轮次间可流水；(c) 风险——信号初值与复用（多轮复用同一信号槽需要轮号编码进信号值或每轮一个新槽）、A5 SDMA 名义路径是串行 MTE 回退（收益可能不明显，URMA/RDMA 才是设计目标后端）、以及接收方可见性（练习见 4.2.5）。改造后的正确性与性能对比「待本地验证」（至少需要 2 NPU + CANN 环境）。

## 6. 本讲小结

- 异步指令族（`TPUT_ASYNC`/`TGET_ASYNC` 及 notify 变体）由 DMA 引擎做 GM↔GM 直达，UB 中只放控制用 scratch tile；编程模型是「`BuildAsyncSession` 一次构建 → 多次提交 → `Wait` 排空（quiet 语义，同 session 按提交顺序完成）」。
- `TPUT_ASYNC_NOTIFY` = 搬载荷 + 置远端 int32 信号，一次调用内保证「载荷先于信号」；`Set` 适合单生产者，`AtomicAdd` 适合多生产者完成计数；本版本新增的 RDMA 后端仅支持 `Set`。
- `peer` 参数的后端差异是本版本注释更新的核心：SDMA 忽略（地址来自 GlobalTensor VA），URMA/RDMA 用它选按对端划分的队列与注册内存元数据（URMA 还选 notify 资源区）；notify 的 A5 实现头也因此改为从 `pkg_inc` 相对路径解析。
- 两套等待不要混：`AsyncEvent::Wait(session)` 等**本地提交完成**（发送方视角），`TWAIT` 等**远端数据到达**（接收方视角）；`TTEST` 是非阻塞版本。
- TREDUCE 是「指令级集合」（`ParallelGroup` + root 单方执行 + acc/recv 乒乓 tile），allgather 是「算法级集合」（用点对点异步指令组合出多核并行与环两种拓扑）。
- allgather_async 三算法：多核 PUT（N-1 推）、多核 GET（N-1 拉）、环（N-1 轮邻居转发）；SDMA 与 URMA 两个构建的内核逻辑逐行同构，差异集中在远端寻址（HCCL 窗口翻译 vs 注册内存基址 + 对称布局偏移）与 host 基础设施。

## 7. 下一步学习建议

- **u6-l5（异步通信传输后端：SDMA、URMA 与 RDMA）**：本讲我们把 RDMA 当黑盒（只看了 `WriteNotify` 分发口），下一讲深入 `pkg_inc/pto/comm/async/rdma/` 的 `rdma_async_intrin` 入口、`PTO_RDMA_BACKEND=HNS_1825` 的编译期后端选择、端点发现与 MR 注册，以及 `tests/npu/a5/comm/st/testcase/tput_async_rdma` 双 rank ST 用例。
- **u6-l4（GEMM+AllReduce 计算通信融合）**：本讲的 quiet 语义与信号配对是那里「ready queue 解耦计算与通信」的基础，建议接着读 `kernels/manual/a2a3/gemm_ar`。
- 源码延伸阅读：`include/pto/comm/a5/async/TPutAsync.hpp`（A5 的 `TPUT_ASYNC_MTE_FALLBACK` 与 URMA/RDMA 分流）、`docs/isa/comm/communication-runtime.md`（host 侧运行时三件套与 workspace 管理器）、`docs/isa/comm/TGET.md`（同步 GET 与异步 GET 的语义对照）。

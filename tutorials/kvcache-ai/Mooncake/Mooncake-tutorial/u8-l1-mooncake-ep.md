# Mooncake EP：容错专家并行

> 阶段：advanced　｜　依赖：`u2-l6`（Mooncake Backend / PG）
> 代码 HEAD：`1f7f71a18a9dc48e9901d8293c5c3625ba166939`

## 1. 本讲目标

Mooncake EP（Expert Parallelism，专家并行）是 Mooncake 为 MoE（Mixture-of-Experts）推理提供的**低延迟 token 通信运行时**。它沿用了 DeepEP 低延迟模式的编程模型，并在其之上叠加了三件事：Mooncake 设备传输（RDMA / NVLink）、`active_ranks` 秩级容错、以及摩尔线程 MUSA 平台支持。

学完本讲你应该能够：

1. 说清在 MoE 推理里 **dispatch / combine** 到底在搬运什么数据，以及为什么要用「打包成 local-expert-major」这种布局。
2. 解释 `active_ranks` 这个 `[num_ranks]` 张量如何让 EP 在某个 expert rank 失活时**不卡死、不崩**，而是超时后绕过它返回结果。
3. 区分 EP 的三条执行路径：**IBGDA / RDMA 快速路径、P2P / IPC 快速路径、Python fallback**，并知道它们各自的前提条件和性能代价。
4. 看懂开启 `MOONCAKE_EP_USE_MUSA` 之后，构建（torchada 源到源翻译）、抽象层（`mooncake_ep_device.h` / `EP_BF16_SIZE`）和异步语义（**拆分内核 SEND → phase-ack → RECV** 取代 CUDA 协作网格同步）发生了哪些变化。

## 2. 前置知识

在进入源码之前，先把几个容易卡住的术语讲清楚。

**MoE 与专家并行（Expert Parallelism）。** MoE 层里每个 token 只激活少数几个「专家」（`top_k` 个）。如果把全体 `num_experts` 个专家切分到多个 GPU（rank）上，每个 rank 只持有 `num_experts / num_ranks` 个本地专家，就形成了「专家并行」。于是计算一个 MoE 层需要两次跨 rank 通信：

- **Dispatch（派遣）**：本 rank 把 token 的隐状态发往「持有被选中专家的 rank」；接收方把收到的 token 按**本地专家**重新打包，喂给本地专家计算。
- **Combine（合并）**：专家算完后，把输出按原路发回 token 的原始属主 rank，并用路由权重 `topk_weights` 加权求和。

**为什么是「低延迟」内核而不是 `all_to_all`？** 推理（尤其 prefill / decode 的单 batch）对延迟极度敏感。DeepEP 风格的内核直接在 GPU 上用 RDMA write / NVLink 把**每一条 token 消息**写到对端预先注册好的缓冲区里，用 GPU 侧轮询信号量（signal buffer）来确认到达，绕开 CPU 和集合通信库的调度开销。Mooncake EP 沿用这套思路。

**GDR / IBGDA。** GPUDirect RDMA 让网卡直接读写 GPU 显存（不经主机内存搬运）。IBGDA（InfiniBand GPUDirect Async）进一步让 **GPU 内核自己发起 RDMA 操作**，而不必回到 CPU。EP 的快速路径依赖它。

**P2P / IPC。** 同一节点内多张 GPU 通过 NVLink 互相访问显存叫 P2P；CUDA IPC handle 让一个进程拿到另一个进程显存的映射指针。EP 用它做节点内快速路径。

**协作网格（cooperative grid）同步。** CUDA 允许用 `cudaLaunchKernelEx` + `cudaLaunchAttributeCooperative` 启动一个「协作内核」，内核内可以调用 `cooperative_groups::this_grid().sync()`，让**整个网格的所有线程块**同步一次。EP 的 CUDA 路径重度依赖它。MUSA 目前不支持，这是本讲后半段的核心差异。

**BF16 / FP8。** Dispatch 默认传 BF16（`bfloat16`，2 字节）。开启 `use_fp8=True` 时传 FP8 E4M3（1 字节）外加 per-128-channel 的 FP32 scale，带宽减半但专家侧需要反量化。EP 内部用 `EP_BF16_SIZE` 这个宏统一两种平台下「一个 bf16 元素占多少字节」。

## 3. 本讲源码地图

本讲涉及的关键文件及其职责：

| 文件 | 作用 |
| --- | --- |
| [mooncake-ep/include/mooncake_ep_buffer.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-ep/include/mooncake_ep_buffer.h) | `MooncakeEpBuffer` 原生运行时类的声明：缓冲区布局 `BufferPair`、`dispatch`/`combine` 签名、三条路径的判定 `use_fast_path()`、IBGDA / IPC 对等元数据交换接口。 |
| [mooncake-ep/include/mooncake_ep_event.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-ep/include/mooncake_ep_event.h) | `EventHandle`：把 CUDA event 封装成可跨流等待的句柄，供异步 / overlap 使用。 |
| [mooncake-ep/src/ep_py.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-ep/src/ep_py.cpp) | pybind11 绑定：把 C++ 的 `Buffer`、`EventHandle`、`get_ep_buffer_size_hint`、`MAX_QP_COUNT` 暴露成 Python 模块 `mooncake.ep`。 |
| [mooncake-ep/src/mooncake_ep_buffer.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-ep/src/mooncake_ep_buffer.cpp) | `dispatch`/`combine` 的宿主侧实现：张量校验、双缓冲切换、`phases` 选择（CUDA 单内核 vs MUSA 拆分内核）、event / hook 生命周期。 |
| [mooncake-ep/src/mooncake_ep_kernel.cu](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-ep/src/mooncake_ep_kernel.cu) | 设备内核：`dispatch`/`combine` 模板内核、FP8 cast、信号量轮询与超时、`mark_phase_ack`/`wait_phase_ack`（MUSA phase-ack 机制）。 |
| [mooncake-ep/include/mooncake_ep_api.cuh](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-ep/include/mooncake_ep_api.cuh) | 内核宿主入口签名：`dispatch(...)`、`combine(...)`、`mark_phase_ack/wait_phase_ack/mark_and_wait_phase_ack`。 |
| [mooncake-ep/include/mooncake_ep_device.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-ep/include/mooncake_ep_device.h) | **平台抽象层**：FP8 类型、device 内联函数、launch 宏。是 CUDA / MUSA 分叉的单一收口点。 |
| [mooncake-ep/include/mooncake_ep_configs.cuh](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-ep/include/mooncake_ep_configs.cuh) | 编译期常量：`MAX_QP_COUNT`、`LOW_LATENCY_SEND_PHASE/RECV_PHASE`、`EP_BF16_SIZE`。 |
| [mooncake-ep/setup.py](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-ep/setup.py) | 扩展构建脚本：读 `MOONCAKE_EP_USE_MUSA` 环境变量，在 CUDA 与 MUSA（torchada）两套编译参数间切换。 |
| [mooncake-wheel/mooncake/mooncake_ep_buffer.py](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-wheel/mooncake/mooncake_ep_buffer.py) | 面向用户的 Python 包装 `Buffer`：`connect()` 元数据交换、`dispatch`/`combine` 转发、Python fallback 实现。 |
| [docs/source/design/mooncake-ep.md](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/docs/source/design/mooncake-ep.md) | 设计文档：高层数据流、运行时对象、三条路径表、恢复集成。 |

---

## 4. 核心概念与源码讲解

本讲按四个最小模块拆分：**EP Buffer（运行时与缓冲区）**、**dispatch / combine（派遣与合并）**、**active_ranks 容错**、**MUSA 平台支持**。

### 4.1 EP Buffer：运行时对象与缓冲区布局

#### 4.1.1 概念说明

EP 的「Buffer」不是一块单纯的显存，而是一个**有状态的运行时对象** `MooncakeEpBuffer`。它一次构造、长期复用，内部持有：

- 当前 `rank` 与 `num_ranks`；
- 一块 GDR（GPUDirect RDMA）工作区缓冲区 `gdr_buffer`，所有发送 / 接收的信号量与数据都落在里面；
- 两个**设备传输**对象：节点内 NVLink 的 `p2p_transport_`、跨节点 IBGDA 的 `rdma_transport_`；
- 一条专用通信流 `comm_stream`；
- 一块 workspace（内核用的原子计数器等）。

构造它有两种方式：如果上层（比如 Mooncake Backend / PG）已经建好了 `TransferEngine`，EP 就**引用**引擎拥有的传输对象；否则 EP 自己用工厂函数创建并 own 它们（`owned_p2p_transport_` / `owned_rdma_transport_`）。

> 与 DeepEP 的兼容性：Mooncake EP 的 Python 编程模型刻意贴近 DeepEP 低延迟模式——构造一个 `Buffer`、调用 `dispatch` / `combine`、拿到一个 `handle` 再喂回 `combine`。从 DeepEP 迁移的工程师几乎可以无缝切换。

#### 4.1.2 核心流程

缓冲区按**双缓冲（double buffering）**组织，这样上一轮的接收数据和这一轮的发送可以错开。每个缓冲区由 `BufferLayout` 描述四个子缓冲：

```
rdma_send_signal_buffer   // 发送侧信号量（发往对端）
rdma_recv_signal_buffer   // 接收侧信号量（对端写过来，本端轮询）
rdma_send_data_buffer     // 发送侧数据
rdma_recv_data_buffer     // 接收侧数据
```

每轮 dispatch / combine 用 `buffer_idx ^= 1` 在两个缓冲间翻转，并用 `phase_epochs[]` 记录每个缓冲的「轮次」，MUSA 的 phase-ack 机制正是靠这个递增的 epoch 来区分「这一轮」和「上一轮」的信号。

整块工作区的大小由 `get_ep_buffer_size_hint()` 推导，公式来自 `BufferPair` 构造函数（见 4.1.3）。

#### 4.1.3 源码精读

先看运行时类的骨架与关键成员：

[mooncake-ep/include/mooncake_ep_buffer.h:65-91](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-ep/include/mooncake_ep_buffer.h#L65-L91) — 这段声明了 `device_id`/`rank`/`num_ranks`、GDR 缓冲与 `buffer_idx`/`phase_epochs` 双缓冲控制、两个传输对象（`p2p_transport_` 节点内、`rdma_transport_` 跨节点，注释明确说明 `rdma_transport_` 在 IBGDA 不可用时为 `nullptr`）、以及 `ibgda_disabled_` 标志。

双缓冲布局的核心是 `BufferPair` 构造函数：

[mooncake-ep/include/mooncake_ep_buffer.h:40-62](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-ep/include/mooncake_ep_buffer.h#L40-L62) — 这里用 `num_experts * num_max_dispatch_tokens_per_rank * (2 * sizeof(int4) + hidden * EP_BF16_SIZE)` 算出每对 send/recv 数据缓冲的字节数，`num_experts * sizeof(int)` 算出信号量缓冲。注意 `EP_BF16_SIZE` 出现在这里——它是平台无关的「bf16 字节数」抽象（4.4 节详解）。循环 `for (int i = 0; i < 2; ++i)` 把两套子缓冲依次排布，最后 `total_bytes` 累加得到提示大小。

`get_ep_buffer_size_hint` 就是用一个「空指针」`BufferPair` 触发同样的计算，只取 `total_bytes`：

[mooncake-ep/include/mooncake_ep_buffer.h:189-195](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-ep/include/mooncake_ep_buffer.h#L189-L195)

三条路径的判定收口在 `use_fast_path()`：

[mooncake-ep/include/mooncake_ep_buffer.h:135-143](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-ep/include/mooncake_ep_buffer.h#L135-L143) — IBGDA 没被禁用就直接走快速路径；否则检查所有 peer 是否都能 P2P 访问（`allPeersAccessible()`），不行就打 WARNING 并返回 false（触发 Python fallback）。

#### 4.1.4 代码实践

**实践目标**：在不启动多卡的情况下，理解「按峰值 sizing」的含义。

**操作步骤**：

1. 打开 [mooncake-ep/include/mooncake_ep_buffer.h:40-62](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-ep/include/mooncake_ep_buffer.h#L40-L62)。
2. 用纸笔或计算器代入一组真实参数：`num_max_dispatch_tokens_per_rank = 256`、`hidden = 2048`、`num_ranks = 8`、`num_experts = 288`（这些值取自 `test_ep_grid.py` 的测试网格）。
3. 计算 `send_recv_buffer_bytes = 288 * 256 * (2*16 + 2048*2)`，再乘以 4（两套缓冲 × send/recv），加上 `4 * signaling_buffer_bytes`。

**需要观察的现象**：缓冲区大小与 `num_max_dispatch_tokens_per_rank` **线性**相关。设计文档明确提醒：「按峰值派遣需求 sizing，而不是平均请求」——若把它设小，高负载时会溢出。

**预期结果**：你会得到一个上百 MB 量级的数字，直观感受到为什么 EP Buffer 一旦构造好就不应频繁重建（重建 = 重新分配 + 重新交换元数据，很贵）。

> 说明：以上为纯算术推导，未运行命令；具体字节数「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 EP 用双缓冲（`buffer_idx ^= 1`）而不是单缓冲？

**答案**：dispatch/combine 是流水化的——本轮在往 `send` 缓冲写数据并发 RDMA 的同时，上一轮的 `recv` 缓冲可能还在被专家内核读取。双缓冲让「发送」和「接收消费」物理错开，避免读写同一块内存造成的依赖停顿。

**练习 2**：`rdma_transport_` 在什么情况下是 `nullptr`？此时 `get_mr_info()` 返回什么？

**答案**：当运行环境没有可用的 IBGDA / RDMA 设备时，`rdma_transport_` 为 `nullptr`。此时 [mooncake_ep_buffer.h:159-163](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-ep/include/mooncake_ep_buffer.h#L159-L163) 的 `get_mr_info()` 直接返回 `{0, 0}`，Python 侧的 `connect()` 也会因此走不到 IBGDA 分支，最终落到 P2P 或 fallback。

---

### 4.2 dispatch / combine：派遣与合并

#### 4.2.1 概念说明

`dispatch` 与 `combine` 是 EP 暴露给用户的两个核心操作，也是 MoE 层通信的两半。它们的设计哲学和 DeepEP 一致：**一次内核调用完成「计算路由 + 发送 + 接收 + 打包」全流程**，而不是拆成多个集合通信原语。

- `dispatch(x, topk_idx, active_ranks, ...)` → 返回**按本地专家打包好的输入**、每个本地专家的接收计数、以及一个**路由句柄 handle**（含 `src_info`、`layout_range` 等），供后续 `combine` 使用。
- `combine(x, topk_idx, topk_weights, handle, ...)` → 用 handle 还原路由，把专家输出发回各 token 属主并加权求和，返回 `combined_x`。

`handle` 是连接两半的钥匙——它记录了 dispatch 时「这些 token 来自哪个 rank、排在打包缓冲的哪个区间」。**不要跨不相干的 dispatch/combine 对复用 handle**。

#### 4.2.2 核心流程

dispatch 内核的执行可以分成两相（phase），由 `phases` 位掩码控制：

```
相1 SEND（LOW_LATENCY_SEND_PHASE = 1）
  ├─ 数据 warp：对每条 token 做 FP8 cast（若开启），写入 send buffer，
  │             按路由把消息写到对端 recv buffer（IBGDA 或 P2P）
  ├─ 计数 warp：扫描 topk_idx，统计每个专家要发多少 token，
  │             向对端信号量写入 -(count+1) 通知「我发了 N 条」
  └─ 网格同步（CUDA：mc_grid_sync；MUSA：拆内核 + phase-ack，见 4.4）
相2 RECV（LOW_LATENCY_RECV_PHASE = 2）
  ├─ 对每个本地专家：轮询信号量，等对端通知到达（超时则标记 active_ranks=0）
  ├─ atomicAdd 抢占打包区间起始位置，写入 layout_range
  └─ 把数据从 recv buffer 拷进 packed_recv_x（local-expert-major）
```

combine 是镜像过程：SEND 相把本地专家输出按 `layout_range` 发回 token 属主；网格同步后；RECV 相里每个 token 跨所有被选中的专家读结果、乘 `topk_weights`、累加。

`phases` 的位掩码设计让宿主侧可以**灵活拆分**：

- `SEND | RECV`（=3）：CUDA 单内核一次跑完（靠内核内 `mc_grid_sync()`）；
- 先 `SEND` 再 `RECV`：MUSA 用，两次内核启动之间插一个 phase-ack；
- 只 `SEND` 或只 `RECV`：`return_recv_hook=True` 时用，把 RECV 推迟到用户调 `hook()` 时，实现通信 / 计算重叠。

#### 4.2.3 源码精读

先看 dispatch 在 C++ 侧的签名与返回（七元组）：

[mooncake-ep/include/mooncake_ep_buffer.h:108-123](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-ep/include/mooncake_ep_buffer.h#L108-L123) — 返回 `packed_recv_x`（FP8 时还带 `packed_recv_x_scales`）、`packed_recv_count`、`packed_recv_src_info`、`packed_recv_layout_range`，外加 `EventHandle` 和可选的 `recv_hook`。注意 `active_ranks` 是**按引用**传入——内核会就地把它对应源 rank 的位置写成 0。

宿主侧 `phases` 的选择是理解 CUDA / MUSA 差异的入口：

[mooncake-ep/src/mooncake_ep_buffer.cpp:243-254](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-ep/src/mooncake_ep_buffer.cpp#L243-L254) — 非 hook 模式下：CUDA 调一次 `launcher(LOW_LATENCY_SEND_PHASE | LOW_LATENCY_RECV_PHASE)`（单内核 + 内核内网格同步）；MUSA 则先 `launcher(SEND)`、再 `mark_and_wait_peer_send_done()`、最后 `launcher(RECV)`。`launcher` 是个捕获了所有指针的 lambda：

[mooncake-ep/src/mooncake_ep_buffer.cpp:228-242](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-ep/src/mooncake_ep_buffer.cpp#L228-L242) — 它把 `timeout_ticks`、`phases`、所有传输表指针打包传给设备内核 `mooncake::dispatch(...)`。`timeout_ticks` 由 `timeout_us` 换算而来（4.3 节）。

设备内核里「SEND 相发数据」的核心：

[mooncake-ep/src/mooncake_ep_kernel.cu:258-275](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-ep/src/mooncake_ep_kernel.cu#L258-L275) — `mc_route_put` 返回非空表示走 local / P2P 路径（warp 协作拷贝 `UNROLLED_WARP_COPY`）；返回 `nullptr` 表示走 IBGDA（`mc_rdma_put` 直接从源 buffer 发）。这正是「三条路径」在内核里的分流点。`dst_rank = dst_expert_idx / num_local_experts`、`dst_expert_local_idx = dst_expert_idx % num_local_experts` 把全局专家号换算成「哪个 rank + 哪个本地专家」。

「RECV 相打包」的核心：

[mooncake-ep/src/mooncake_ep_kernel.cu:361-400](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-ep/src/mooncake_ep_kernel.cu#L361-L400) — sub-warp 1 轮询信号量（含超时判定，见 4.3），sub-warp 0 用 `atomicAdd` 抢占 `packed_recv_count` 得到 `recv_token_begin_idx`，把 `(count, begin)` 打包写进 `layout_range`，随后多 warp 协作把数据拷进 `packed_recv_x`。这段就是「local-expert-major 打包」的全部秘密。

内核用模板 + `SWITCH_HIDDEN` 在编译期特化不同 `hidden`：

[mooncake-ep/include/mooncake_ep_launch.cuh:66-81](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-ep/include/mooncake_ep_launch.cuh#L66-L81) — 支持 2048 / 2560 / 3072(gpt-oss) / 4096 / 5120 / 6144(qwen3 coder) / 7168 等隐层宽度，`kHidden` 作为编译期常量让向量化展开（`kNumElemsPerRead = sizeof(int4)/EP_BF16_SIZE`）最优。

#### 4.2.4 代码实践

**实践目标**：跟踪 dispatch → 专家计算 → combine 的完整调用链，看清 handle 如何把两半连起来。

**操作步骤**：

1. 打开测试 [mooncake-ep/tests/test_ep_grid.py:118-185](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-ep/tests/test_ep_grid.py#L118-L185)。
2. 注意 119 行 `recv_x, recv_count, handle, event, hook = buf.dispatch(...)`：dispatch 一次返回**打包输入 + handle**。
3. 注意 158–163 行的「mock 专家前向」：直接在 `recv_bf16[le]` 上做逐专家缩放——这就是接收方本地专家消费打包输入的地方。
4. 注意 166–185 行：若 `zero_copy`，用 `get_next_combine_buffer(handle)` 拿到一块 EP 管理的缓冲写入专家输出，再 `buf.combine(..., handle=handle, out=out_tensor)`。
5. 注意 194 行 `testing.assert_close(combined_x, expected_out, ...)`：断言 combine 输出与「逐 token 加权求和」的期望一致。

**需要观察的现象**：handle 是一个普通 Python tuple `(src_info, layout_range, num_max_dispatch_tokens_per_rank, x.size(1), num_experts)`（见 [mooncake_ep_buffer.py:295-301](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-wheel/mooncake/mooncake_ep_buffer.py#L295-L301)），它只是把 dispatch 算出来的路由元数据透传给 combine。

**预期结果**：你能画出 `x → dispatch → (packed_recv_x, handle) → 专家 → combine(handle) → combined_x` 的数据流图。运行该测试需要多卡 + mooncake backend 环境，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`return_recv_hook=True` 时，RECV 相什么时候真正执行？

**答案**：不在 `dispatch` 返回前。宿主侧只跑 `launcher(SEND)` + `mark_send_done()`，把 `launcher(RECV)` 包进返回的 `recv_hook` 闭包里（见 [mooncake_ep_buffer.cpp:269-273](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-ep/src/mooncake_ep_buffer.cpp#L269-L273)）。用户在「想重叠的时机」调 `hook()`，RECV 才在默认流上执行。这让 SEND 通信可以和用户自己的计算重叠。

**练习 2**：为什么 `async` 和 `return_recv_hook` 不能同时为真？

**答案**：见 [mooncake_ep_buffer.cpp:160](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-ep/src/mooncake_ep_buffer.cpp#L160) 的 `EP_HOST_ASSERT(not(async and return_recv_hook))`。两者是互斥的异步策略：`async` 靠返回的 stream event 让当前流等待；`return_recv_hook` 靠显式调 hook 推迟 RECV。同时开会破坏流依赖与生命周期假设。

---

### 4.3 active_ranks 容错：超时感知与秩级失效

#### 4.3.1 概念说明

在大规模 MoE 部署里，某个持有专家的 rank 可能崩溃或卡住。如果 dispatch / combine 内核傻等那个 rank 的信号量，整条推理流水线就会**永久挂死**。EP 的容错思路是：

1. 给每次 dispatch / combine 一个 `timeout_us`（微秒）。
2. 接收侧在轮询信号量时同时看时钟；超过 timeout 仍未等到某个源 rank，就**就地把它在 `active_ranks[src_rank]` 写成 0**。
3. 后续处理跳过这个 rank：它的 token 不计入打包、combine 时不累加它的贡献。
4. 上层（PG / 调度器）据此重新路由、恢复。

`active_ranks` 是一个 `[num_ranks]` 的 int32 张量，1 表示健康、0 表示失活。它**与** Mooncake Backend（PG）层的 active-rank mask 相关但**不自动等同**——PG 那层管的是集合通信的成员健康，EP 这层管的是 EP 通信的源 rank 健康。集成方需要把两者一致地传递（设计文档「Rank activeness」一节专门强调）。

#### 4.3.2 核心流程

超时换算把「微秒」翻译成「GPU 时钟周期」：

\[
\text{timeout\_ticks} = \text{clock\_rate\_khz} \times \text{timeout\_us} / 1000
\]

其中 `clock_rate_khz` 是构造时从设备查到的时钟频率。设备内核里用 `clock64()` 读 GPU 计时器，所以换算必须用同一基准。`timeout_us == -1` 表示**禁用超时**（`timeout_ticks = -1`），内核会无限等——这是健康环境的默认，避免误判。

容错的判定嵌入在 RECV 相的轮询循环里：

```
start = clock64()
while (信号量还是 0):              # 还没收到对端通知
    if timeout_ticks != -1 and (clock64() - start) > timeout_ticks:
        active_ranks[src_rank] = 0   # 判定失活
    if not active_ranks[src_rank]:
        跳过这个 rank                 # 不再死等
```

dispatch 的「跳过」是 `num_recv_tokens = -1` 后 `break`（不计入打包）；combine 的「跳过」是直接 `break`（不累加贡献）。两者都会把失活信息写回 `active_ranks`，供上层读取。

Python fallback 路径也实现了同样的语义：用 `get_active_ranks(backend)` 拿到 PG 的健康 mask，把失活 rank 的 token 数置 0（[mooncake_ep_buffer.py:479-482](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-wheel/mooncake/mooncake_ep_buffer.py#L479-L482)）。

#### 4.3.3 源码精读

dispatch 内核里的超时判定（RECV 相等信号量）：

[mooncake-ep/src/mooncake_ep_kernel.cu:380-397](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-ep/src/mooncake_ep_kernel.cu#L380-L397) — `unsigned long long start_time = clock64()`，循环里 `if (timeout_ticks != -1 && end_time - start_time > timeout_ticks) active_ranks[src_rank] = 0;`，随后 `if (!active_ranks[src_rank]) { num_recv_tokens = -1; break; }`。注意 `active_ranks` 被当作**设备端可写**的指针传入。

combine 内核里同构的超时判定：

[mooncake-ep/src/mooncake_ep_kernel.cu:624-635](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-ep/src/mooncake_ep_kernel.cu#L624-L635) — 同样 `clock64()` 计时，超时写 `active_ranks[src_rank] = 0`，失活则 `break` 跳过该源专家的等待。

宿主侧的超时换算：

[mooncake-ep/src/mooncake_ep_buffer.cpp:193-195](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-ep/src/mooncake_ep_buffer.cpp#L193-L195) — `timeout_ticks = timeout_us == -1 ? -1 : clock_rate_khz * timeout_us / 1000`。

测试里对容错的断言（一个 rank 直接 `os._exit(0)` 模拟崩溃）：

[mooncake-ep/tests/test_ep_grid.py:110-148](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-ep/tests/test_ep_grid.py#L110-L148) — `fail_rank` 进程在 barrier 后 `os._exit(0)`；其余 rank 用 `timeout_us = 5_000_000`（5 秒）做 dispatch，随后断言 `active_ranks[fail_rank].item() == 0` 且「恰好一个 rank 失活」。这把 EP 容错的「可观察行为」写成了测试契约。

#### 4.3.4 代码实践

**实践目标**：理解「timeout 设太小会误杀」的风险。

**操作步骤**：

1. 打开 [mooncake-ep/tests/test_ep_grid.py:110-111](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-ep/tests/test_ep_grid.py#L110-L111)，确认健康场景（`fail_rank == -1`）用 `timeout_us = -1`（禁用超时），故障场景用 5 秒。
2. 假设把故障场景的 `timeout_us` 改成 `100`（100 微秒）。
3. 思考：在一次正常但稍慢的 dispatch 里（比如负载高、RDMA 抖动），信号量到达前就超过了 100us 会发生什么。

**需要观察的现象**：内核会把一个**实际健康**的源 rank 误判为失活（`active_ranks[src_rank]=0`），导致它的 token 被丢弃、combine 输出错乱。测试 143–148 行的断言「Maybe the timeout is too small?」正是提示这种误判。

**预期结果**：timeout 是「容错灵敏度 vs 误判风险」的权衡旋钮。生产环境应设为「略大于最坏正常延迟」。这是一个推理分析练习，**待本地验证**具体阈值。

#### 4.3.5 小练习与答案

**练习 1**：dispatch 标记某 rank 失活后，combine 那一轮还会等它吗？

**答案**：不会。dispatch 写回 `active_ranks[src_rank]=0` 后，由于 `active_ranks` 按引用贯穿 dispatch→combine，combine 内核的轮询循环在 `if (!active_ranks[src_rank]) break;` 处直接跳过（见 [mooncake_ep_kernel.cu:624-635](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-ep/src/mooncake_ep_kernel.cu#L624-L635)）。这就是「combine 绕过失效 rank 返回结果」的机制。

**练习 2**：EP 层的 `active_ranks` 和 PG 层的 active-rank mask 是什么关系？

**答案**：相关但独立。PG 层（`pg.get_active_ranks(backend)`）描述集合通信成员的健康，EP 层的 `active_ranks` 描述 EP 通信源 rank 的健康。EP 的 Python fallback 直接读 PG 的 mask（[mooncake_ep_buffer.py:270-274](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-wheel/mooncake/mooncake_ep_buffer.py#L270-L274)）；而快速路径的 `active_ranks` 由内核超时动态写入。设计文档要求集成方在「调度逻辑 / PG 状态 / EP buffer」三者间一致地传播健康更新。

---

### 4.4 MUSA 平台支持：torchada 构建、设备抽象、拆分内核

#### 4.4.1 概念说明

摩尔线程 MUSA 是 CUDA 之外的另一套 GPU 生态。Mooncake EP 通过三条手段把同一份 `.cu` 源码同时编译到 CUDA 和 MUSA：

1. **torchada 源到源翻译**：构建时用 `torchada` 把代码里的 CUDA API 名（`nvcc`、`torch.cuda.*`、`cudaLaunchKernelEx` 等）文本替换映射成 MUSA 等价物（`mcc`、`torch.musa.*`）。绝大多数代码**不需要改**。
2. **`mooncake_ep_device.h` 平台抽象层**：只把 torchada **翻译不了**的东西收口到一个头文件，用 `#ifdef MOONCAKE_EP_USE_MUSA` 分叉。包括 FP8 类型名、少数 device 内联函数、以及 **launch 宏**。
3. **`EP_BF16_SIZE` 等编译期常量**：绕开 torchada 把 `nv_bfloat16` 映射成 MUSA 不完整类型、导致 `sizeof` 失败的问题。

但 MUSA 有一个**根本性能力缺口**：不支持**协作网格启动**（cooperative launch）。CUDA 路径靠「单内核 + 内核内 `mc_grid_sync()`」让 SEND 相和 RECV 相在同一个内核里同步；MUSA 做不到。于是 EP 在 MUSA 上把内核**拆成 SEND 内核 → phase-ack → RECV 内核**三段，用一块显存的 `ack_buffer` + 递增的 `epoch` 在两次内核启动之间做跨网格同步。

#### 4.4.2 核心流程

CUDA 路径（一次内核）：

```
launcher(SEND | RECV)        # 单个协作内核
  ├─ SEND 相
  ├─ mc_grid_sync()          # cooperative_groups::this_grid().sync()
  └─ RECV 相
```

MUSA 路径（三次启动）：

```
launcher(SEND)               # 内核1：只做 SEND 相
mark_and_wait_phase_ack()    # 内核2：写自己的 epoch 给所有 peer，再轮询等 peer 的 epoch
launcher(RECV)               # 内核3：只做 RECV 相
```

phase-ack 的语义：每个 rank 在 `ack_buffer[rank]` 写下自己的 `epoch`，并经 P2P `mc_route_put` 把这个 epoch 写到每个 peer 的 `ack_buffer[my_rank]` 槽位；然后轮询 `ack_buffer[peer] >= epoch`，等到说明 peer 的 SEND 相已完成、数据已落地，RECV 相可以安全读取。`epoch` 由 `phase_epochs[]` 在每轮递增，避免和上一轮的残留信号混淆。

此外 MUSA 内核内部凡是原本依赖 `mc_grid_sync()` 的地方（如 combine 的 reduce 前），改用 `__syncthreads() + mc_fence() + __syncthreads()` 做**块内**可见性，因为 MUSA 的 `mc_grid_sync()` 是 no-op。

#### 4.4.3 源码精读

**构建侧**——`setup.py` 读环境变量并分叉：

[mooncake-ep/setup.py:7-15](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-ep/setup.py#L7-L15) — `MOONCAKE_EP_USE_MUSA` 为真时强制 `import torchada`，缺失则报错「请先 `pip install torchada`」。

[mooncake-ep/setup.py:38-61](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-ep/setup.py#L38-L61) — MUSA 分支：定义 `-DUSE_MUSA -DMOONCAKE_EP_USE_MUSA=1`，`device_args` 用 `--cuda-gpu-arch=mp_21` / `mp_31`，**不**链接 `ibverbs/mlx5`（`cuda_libraries = []`）；CUDA 分支：`-DUSE_CUDA`、`-Xcompiler` 透传优化项，并尝试链接 CUDA driver stub `libcuda.so`。注意注释「torchada maps the "nvcc" key to "mcc"」——torchada 接管了编译器选择。

**抽象层侧**——`mooncake_ep_device.h` 是 CUDA/MUSA 分叉的唯一收口：

[mooncake-ep/include/mooncake_ep_device.h:10-50](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-ep/include/mooncake_ep_device.h#L10-L50) — MUSA 分支：FP8 用 `musa_fp8.h` 的 `__mt_fp8_storage_t` / `__mt_fp8x2_storage_t` 与 `__musa_cvt_float2_to_fp8x2`；`__ldg`/`__activemask` 退化成普通解引用 / 常量；最关键的是 launch 宏——`EP_LAUNCH_BOUNDS` 被定义为**空**（MUSA 无 `__launch_bounds__`），`SETUP_LAUNCH_CONFIG` 只构造普通 `dim3` + `cudaStream_t`（**无** `cudaLaunchAttributeCooperative`），`LAUNCH_KERNEL` 用 `kernel<<<grid,block,0,stream>>>` 普通 triple-chevron 启动。

[mooncake-ep/include/mooncake_ep_device.h:73-87](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-ep/include/mooncake_ep_device.h#L73-L87) — CUDA 分支：`EP_LAUNCH_BOUNDS` 展开成真正的 `__launch_bounds__(max_threads, min_blocks)`，`SETUP_LAUNCH_CONFIG` 构造 `cudaLaunchConfig_t` 并设 `cudaLaunchAttributeCooperative = 1`，`LAUNCH_KERNEL` 走 `cudaLaunchKernelEx`。两相对照就能看出「协作网格」只属于 CUDA。

`EP_BF16_SIZE` 的由来（在 configs 里）：

[mooncake-ep/include/mooncake_ep_configs.cuh:47-56](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-ep/include/mooncake_ep_configs.cuh#L47-L56) — 注释解释：torchada 把 `nv_bfloat16` 映射成 `__mt_bfloat16`，后者在 MUSA 上是不完整类型，`sizeof` 会失败；而完整的 `mt_bfloat16` 又需要 mcc、不能在宿主 `.cpp` 里 include。所以宿主代码用 `EP_BF16_SIZE`：CUDA 下 `sizeof(nv_bfloat16)`、MUSA 下硬编码 `2`（两者都是 2 字节）。

**异步语义侧**——MUSA 拆分内核 + phase-ack。宿主侧入口已在 4.2.3 看过（[mooncake_ep_buffer.cpp:247-254](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-ep/src/mooncake_ep_buffer.cpp#L247-L254)）。phase-ack 的三个内核：

[mooncake-ep/src/mooncake_ep_kernel.cu:31-50](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-ep/src/mooncake_ep_kernel.cu#L31-L50) — `mark_phase_ack_kernel`：对每个 peer，本地用 `mc_st_release` 写 `ack_buffer[rank]=epoch`，对端用 `mc_route_put` 拿到映射地址后写 `epoch`。这就是「告诉所有 peer：我 SEND 完了」。

[mooncake-ep/src/mooncake_ep_kernel.cu:52-66](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-ep/src/mooncake_ep_kernel.cu#L52-L66) — `wait_phase_ack_kernel`：轮询 `mc_ld_acquire(ack_buffer + peer) < epoch`，超时则 `return`。

[mooncake-ep/src/mooncake_ep_kernel.cu:68-101](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-ep/src/mooncake_ep_kernel.cu#L68-L101) — `mark_and_wait_phase_ack_kernel`：先 mark（同上），`__syncthreads()` 后再 wait——把两步合一，减少一次内核启动。

MUSA 内核内部对「无协作网格」的补偿。dispatch 里 SEND 相的计数 warp 必须参与数据 warp 的 `__syncthreads` 屏障：

[mooncake-ep/src/mooncake_ep_kernel.cu:277-285](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-ep/src/mooncake_ep_kernel.cu#L277-L285) — MUSA 下，计数 warp 用一个和 token 循环**等长**的 `__syncthreads()` 循环来匹配数据 warp 每轮的屏障（CUDA 下数据 warp 用的是 `mc_bar_sync`，行为不同）。

combine 里 RECV 相 reduce 前的同步：

[mooncake-ep/src/mooncake_ep_kernel.cu:637-645](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-ep/src/mooncake_ep_kernel.cu#L637-L645) — 注释直说「`mc_grid_sync()` 在 MUSA 上是 no-op」，所以改用 `__syncthreads(); mc_fence(); __syncthreads();` 让块内线程看到 peer 的写。对照 CUDA 分支的 `mc_grid_sync()`。

`mc_grid_sync` 的平台实现对照（在 transfer-engine 的设备 ops 头里）：

[mooncake-transfer-engine/include/transport/device/cuda/cuda_ops.cuh:124-126](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/include/transport/device/cuda/cuda_ops.cuh#L124-L126) — CUDA：`cooperative_groups::this_grid().sync()`。

[mooncake-transfer-engine/include/transport/device/musa/musa_ops.cuh:123](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/include/transport/device/musa/musa_ops.cuh#L123) — MUSA：`__device__ __forceinline__ void mc_grid_sync() {}`（空函数）。这就是拆分内核的根本原因。

还有一处 MUSA 专属调参：

[mooncake-ep/src/mooncake_ep_kernel.cu:451-456](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-ep/src/mooncake_ep_kernel.cu#L451-L456) — dispatch 的 `kNumWarpGroups` 在 MUSA 上取 5、CUDA 上取 8（注释：MT S5000 受益于略多的 CTA 同时保证 top-k≤11 的 warp 数）。

最后，Python 包装层在 MUSA + `async_finish` 时会发警告，说明语义已变：

[mooncake-wheel/mooncake/mooncake_ep_buffer.py:241-249](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-wheel/mooncake/mooncake_ep_buffer.py#L241-L249) — 提示「MUSA 的 async_finish 用的是拆分 SEND/RECV 内核 + stream event，**不是** CUDA 协作单内核的 async 语义」。

#### 4.4.4 代码实践

**实践目标**：对照 `setup.py` 与 `mooncake_ep_device.h`，说清「开 `MOONCAKE_EP_USE_MUSA` 后构建与异步语义如何改变」。

**操作步骤**：

1. 构建对照：打开 [mooncake-ep/setup.py:38-61](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-ep/setup.py#L38-L61)。列出三处差异：
   - 宏：CUDA 加 `-DUSE_CUDA`；MUSA 加 `-DUSE_MUSA -DMOONCAKE_EP_USE_MUSA=1`。
   - 编译器/架构：CUDA 用 nvcc 默认 arch + `-Xcompiler`；MUSA 用 mcc 的 `--cuda-gpu-arch=mp_21,mp_31`。
   - 链接库：CUDA 链 `ibverbs/mlx5`（+ 可选 cuda stub）；MUSA `cuda_libraries = []`，不依赖 IB verbs。
2. 抽象层对照：打开 [mooncake_ep_device.h:35-50](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-ep/include/mooncake_ep_device.h#L35-L50) 与 [:73-87](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-ep/include/mooncake_ep_device.h#L73-L87)。确认：CUDA 的 `SETUP_LAUNCH_CONFIG` 设了 `cudaLaunchAttributeCooperative=1` 并用 `cudaLaunchKernelEx`；MUSA 的同名宏只构造 `dim3`，用普通 `<<<>>>` 启动。
3. 异步语义对照：打开 [mooncake_ep_buffer.cpp:247-254](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-ep/src/mooncake_ep_buffer.cpp#L247-L254)。画出两条时序：
   - CUDA：`launcher(SEND|RECV)` 单内核，内部 `mc_grid_sync()`。
   - MUSA：`launcher(SEND)` → `mark_and_wait_phase_ack` → `launcher(RECV)` 三段，跨网格同步靠 `ack_buffer` + `epoch`。

**需要观察的现象**：MUSA 把「一次内核调用」拆成「两次数据内核 + 一次 ack 内核」，启动开销更高、且 phase-ack 的 mark/wait 自带 timeout（复用 `timeout_ticks`）。但功能等价——SEND 相落地的数据在 RECV 相开始前对全部块可见。

**预期结果**：你能用一句话总结差异——「CUDA 靠硬件协作网格在单内核内同步；MUSA 因无协作网格，用 ack_buffer + 递增 epoch 在拆分的 SEND/RECV 内核之间做软件跨网格同步」。本步骤为源码阅读型实践，**待本地验证**实际 MUSA 硬件上的行为。

#### 4.4.5 小练习与答案

**练习 1**：为什么 MUSA 路径需要 `phase_epochs[]` 递增，而 CUDA 路径不需要？

**答案**：CUDA 在单内核内用 `mc_grid_sync()` 同步，SEND/RECV 同属一次启动，不存在「上一轮残留信号」的问题。MUSA 把 SEND 和 RECV 拆成两次内核启动，中间隔着 phase-ack；若不区分轮次，RECV 内核可能读到上一轮写进 `ack_buffer` 的旧 epoch 而提前放行。`phase_epochs[]` 每轮自增，`wait` 必须等到「当前 epoch」才算数。

**练习 2**：`mooncake_ep_device.h` 顶部注释说「只包含 torchada 翻译不了的东西」。举一个 torchada **能**翻译、所以不在该头里的例子。

**答案**：`cudaStream_t`、`dim3`、`__syncthreads()`、`cudaGetLastError` 等。MUSA 分支的 `SETUP_LAUNCH_CONFIG` 依然写 `dim3 _grid(num_sms)` 和 `cudaStream_t _stream = stream`，说明这些名字 torchada 能直接映射，无需分叉；真正需要分叉的是「CUDA 有而 MUSA 没有」的协作启动属性和 FP8 类型名。

---

## 5. 综合实践

把四个模块串起来，完成本讲规格里要求的核心任务：**编写伪代码，构造带 `active_ranks` 的 dispatch，模拟某个 expert rank 失活，说明 combine 如何绕过失效 rank 返回结果；并对照 `setup.py` 与 `mooncake_ep_device.h` 说明 MUSA 与 CUDA 的构建 / 异步语义差异。**

### 任务 A：`active_ranks` 容错的伪代码推演

下面是一段**说明性伪代码**（不是项目原有代码，仅用于演示逻辑），对照真实接口签名写成：

```python
# 示例代码（非项目源码，仅演示 active_ranks 容错语义）
import torch
from mooncake.mooncake_ep_buffer import Buffer

num_ranks = 8
# active_ranks：[num_ranks] int32，初始全 1（全部健康）
active_ranks = torch.ones((num_ranks,), dtype=torch.int32, device="cuda")

buf = Buffer(group, num_ep_buffer_bytes)   # group 是 mooncake backend PG

# 模拟：rank 3 持有的专家进程已崩溃（真实场景由 os._exit / 进程死亡触发）
# 这里只需把 timeout_us 设成有限值，让内核在收不到 rank 3 信号时自行判定
timeout_us = 5_000_000  # 5 秒

# 1) dispatch：把 token 发往各 expert-owner rank
recv_x, recv_count, handle, event, hook = buf.dispatch(
    x, topk_idx, active_ranks,
    num_max_dispatch_tokens_per_rank=max_tokens,
    num_experts=num_experts,
    timeout_us=timeout_us,        # 关键：有限值才启用超时判定
    use_fp8=True,
)
event.current_stream_wait()

# 此刻 active_ranks[3] 已被设备内核就地写成 0（见 mooncake_ep_kernel.cu:380-397）
assert active_ranks[3].item() == 0

# 2) 本地专家计算（rank 3 的贡献因失活而缺失，这没关系）
expert_out = run_local_experts(recv_x)

# 3) combine：用同一个 handle 还原路由，把结果发回各 token 属主
#    内核在 RECV 相遇到 active_ranks[3]==0 时直接 break（mooncake_ep_kernel.cu:624-635），
#    不再等待 rank 3，也不把它的（缺失的）贡献累加进 combined_x。
combined_x, event, hook = buf.combine(
    expert_out, topk_idx, topk_weights, active_ranks,
    timeout_us=timeout_us, handle=handle,
)
```

**要点说明（结合源码）**：

- `active_ranks` 在 dispatch 与 combine 间**复用同一个张量**，因此 dispatch 写入的 `0` 会被 combine 读到。
- 容错不是「EP 自己重启 rank」，而是「**不卡死、返回降级结果**」。真正恢复（停发 token 给失效专家、替换 rank 加入）是上层 PG + 调度器的职责（设计文档「Recovery integration」四步）。
- 对照真实测试可验证行为：[test_ep_grid.py:138-148](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-ep/tests/test_ep_grid.py#L138-L148) 断言 `active_ranks[fail_rank].item() == 0` 且恰好一个 rank 失活。

### 任务 B：CUDA 与 MUSA 的差异速查表

对照 `setup.py` 与 `mooncake_ep_device.h`，把差异整理成表：

| 维度 | CUDA 路径 | MUSA 路径（`MOONCAKE_EP_USE_MUSA=1`） |
| --- | --- | --- |
| 构建宏 | `-DUSE_CUDA` | `-DUSE_MUSA -DMOONCAKE_EP_USE_MUSA=1` |
| 编译器 | nvcc | mcc（torchada 把 `nvcc` 键映射成 `mcc`） |
| GPU arch | 默认 | `--cuda-gpu-arch=mp_21,mp_31` |
| 链接库 | `ibverbs, mlx5`（+ 可选 cuda stub） | 无（`cuda_libraries = []`） |
| FP8 类型 | `__nv_fp8_storage_t`（`cuda_fp8.h`） | `__mt_fp8_storage_t`（`musa_fp8.h`） |
| launch 抽象 | `__launch_bounds__` + `cudaLaunchKernelEx` + `cudaLaunchAttributeCooperative=1` | 空 `EP_LAUNCH_BOUNDS` + 普通 `<<<>>>`，无协作属性 |
| `EP_BF16_SIZE` | `sizeof(nv_bfloat16)` | 硬编码 `2` |
| dispatch/combine 时序 | 单内核 `launcher(SEND\|RECV)`，内核内 `mc_grid_sync()` | 三段：`launcher(SEND)` → `mark_and_wait_phase_ack` → `launcher(RECV)` |
| 跨网格同步 | 硬件协作网格 | `ack_buffer` + 递增 `epoch` 的软件 phase-ack |
| `mc_grid_sync()` | `cooperative_groups::this_grid().sync()` | 空函数 `{}`（内核内改用 `__syncthreads+fence`） |
| `kNumWarpGroups`(dispatch) | 8 | 5（MT S5000 调优） |

读完这张表，你应该能回答：「开 `MOONCAKE_EP_USE_MUSA` 后，构建上多了 torchada 翻译、改了编译器/架构/链接库；运行时上，因为 MUSA 没有协作网格，dispatch/combine 从单内核变成了 SEND→phase-ack→RECV 的三段拆分，靠 `ack_buffer`+epoch 做软件同步，`async_finish` 也退化为 stream event 而非协作单内核 async。」

> 以上为源码阅读 + 伪代码推演型综合实践；在真实多卡 / MUSA 硬件上的端到端验证「待本地验证」。

## 6. 本讲小结

- **EP Buffer** 是有状态运行时：双缓冲 GDR 工作区 + 两个设备传输（NVLink P2P / IBGDA RDMA）+ 通信流，按峰值 `num_max_dispatch_tokens_per_rank` sizing，构造后应长期复用。
- **dispatch / combine** 是 MoE 通信的两半，靠 `handle`（`src_info` + `layout_range`）相连；内核用 `phases` 位掩码（SEND/RECV）灵活拆分，支持同步 event、`async`、`return_recv_hook` 三种异步策略。
- **active_ranks 容错**：有限 `timeout_us` 下，设备内核用 `clock64()` 计时，超时就把源 rank 在 `active_ranks[src_rank]` 就地写 0，后续跳过；这是「不卡死、返回降级结果」而非自动恢复。
- **三条执行路径**：IBGDA/RDMA（跨节点 GPU 直发）、P2P/IPC（节点内 NVLink 映射）、Python fallback（`torch.distributed` 集合通信，仅功能正确性）。内核里 `mc_route_put` 返回值决定走哪条。
- **MUSA 支持**靠三层：torchada 源到源翻译、`mooncake_ep_device.h` 平台抽象（FP8 / launch 宏）、`EP_BF16_SIZE` 绕开不完整类型。
- **MUSA 异步语义**：因无协作网格，把 CUDA 的「单内核 + `mc_grid_sync()`」拆成「SEND 内核 → phase-ack（`ack_buffer`+epoch）→ RECV 内核」，内核内的网格同步退化为块内 `__syncthreads+fence`。

## 7. 下一步学习建议

1. **深入 PG 与恢复**：本讲的 `active_ranks` 与 PG 的 active-rank mask 紧密耦合。建议阅读 `docs/source/design/mooncake-backend-pg.md` 与依赖讲义 `u2-l6`，看清 `pg.get_active_ranks(backend)`、`extend_group_size_to`、`recover_ranks` 如何与 EP 的 `Buffer.update_ep_member()`（即 `connect(is_update=True)`）配合完成端到端恢复。
2. **设备传输细节**：本讲把 `mc_route_put` / `mc_rdma_put` / `mc_grid_sync` 当黑盒。想看清三条路径的硬件映射，可读 `mooncake-transfer-engine/include/transport/device/comm_device.cuh` 及 `device/cuda/cuda_ops.cuh`、`device/musa/musa_ops.cuh` 的逐项对照。
3. **动手验证容错**：在有 RDMA 或多卡的环境跑 `mooncake-ep/tests/test_ep_grid.py` 的 `fail_rank=1` 用例，观察 `active_ranks` 被内核改写的过程；再用 profiler 对比 CUDA 单内核与（若可得）MUSA 三段内核的 timeline。
4. **DeepEP 对照**：若你来自 DeepEP，重点对照 `phases` 位掩码、`layout_range` 的 `(begin<<32)|count` 打包、以及本讲独有的 `active_ranks` 超时分支——这是 Mooncake EP 相对 DeepEP 最实质的扩展。

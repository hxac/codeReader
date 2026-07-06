# All-Gather Reduce-Scatter（AGRS）session 模型

## 1. 本讲目标

本讲讲解 DeepEP V2 中独立于 dispatch/combine 之外的另一个实验性通信原语：**AGRS（All-Gather Reduce-Scatter）**。读完后你应当能够：

- 说清 AGRS 与 dispatch/combine 在「实现层级」上的根本差异（host 驱动 vs JIT GPU kernel）。
- 解释 AGRS 为什么采用 **session + slot** 的批量管理模型，以及 `create_agrs_session` / `destroy_agrs_session` 如何用对称内存信号保证缓冲区安全复用。
- 掌握 `agrs_get_inplace_tensor` 的零拷贝（inplace）语义、以及 batched（一次 all-gather 多张量）模式的用法。
- 读懂 `all_gather` 如何用 `cudaMemcpyBatchAsync` 发送数据、用 `cuStreamBatchMemOp` 完成 per-slot 的信号握手、并返回一个 wait hook。
- 能够追踪 `agrs_buffer_offset` 与 `agrs_buffer_slot_idx` 在一个 session 内如何随每次 `all_gather` 累加。

## 2. 前置知识

在进入源码前，先建立几个本讲会用到的概念。本讲承接 [u3-l4 NCCL Gin 后端与对称内存上下文] 的对称内存认知，并复用 [u2-l4 通信-计算重叠] 中的双流 / EventHandle 概念。

**All-Gather 集合通信。** 它是分布式训练里最常见的集合原语之一：每个 rank 持有一份本地数据，all-gather 之后**每个 rank 都拥有所有 rank 数据的拼接**，输出比输入多一个长度为 `num_ranks` 的前导维度。它等价于「每个 rank 把自己的数据广播给所有人，再按 rank 顺序拼起来」。

**NVLink 对称内存（symmetric memory）。** DeepEP 通过 NCCL Gin 在所有 rank 之间建立一块**对称窗口**：每个 rank 在窗口内相同偏移处都映射了自己的显存，且任意 rank 都能直接读写对端的对称地址（见 `get_sym_ptr`）。这意味着「发数据」可以退化成一次普通的显存拷贝——把本 rank 数据写到对端窗口里对应的槽位即可，**不需要启动 GPU kernel**。AGRS 正是建立在这一前提上。

**`cuStreamBatchMemOp`（流批量内存操作）。** 这是 CUDA Driver 提供的一类「流内」操作：可以把多个「写一个 32 位值 / 等待一个 32 位值达到某条件」的微型操作打包成**一条**流命令下发。它在流里有严格的顺序：执行到它时，前面的拷贝必然已完成，后面的读取必然在其后。AGRS 用它实现「数据拷贝完→发完成信号；收到所有人的信号→才允许读结果」的握手，全程不回 CPU、不启动 kernel。

**`cudaMemcpyBatchAsync`（批量异步拷贝）。** CUDA Runtime API（CUDA 12+）提供的接口：一次调用下发**多个**独立的 `cudaMemcpyAsync`，由驱动合并优化。AGRS 用它把本 rank 数据一次性写到所有对端槽位。

**为什么叫 "Reduce-Scatter" 但只讲 all-gather？** AGRS 是这一组集合原语（all-gather / reduce-scatter）的统称，但**当前仓库公开实现的只有 `all_gather`**（见后文 pybind 绑定）。本讲严格按源码讲，只覆盖已实现的 all-gather 路径，不臆测 reduce-scatter。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [csrc/elastic/buffer.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp) | AGRS 的 host 层全部实现：session 配置/生命周期、inplace 张量、`all_gather` 主体、信号握手。本讲的绝对主角。 |
| [csrc/kernels/backend/cuda_driver.cu](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/cuda_driver.cu) | 把 `cuStreamBatchMemOp` 封装成 `batched_write_and_wait`（一次写 + 一次等），是 AGRS 信号握手的底层。 |
| [tests/elastic/test_agrs.py](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_agrs.py) | AGRS 的正确性压测与带宽 profiling，也是本讲代码实践的范本。 |
| [deep_ep/buffers/elastic.py](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py) | Python 接口层：尺寸估算、session 上下文管理器、参数分发。 |
| [deep_ep/include/deep_ep/common/layout.cuh](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/layout.cuh) | `WorkspaceLayout` 中 AGRS 信号区的布局（recv 信号 + session 信号）。 |
| [csrc/kernels/backend/nccl.cu](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/nccl.cu) | `get_sym_ptr`——把本 rank workspace 内的指针翻译成对端 rank 的对称指针。 |

> 关键认知：**AGRS 没有 JIT kernel 文件**。`csrc/kernels/` 下不存在任何 `*agrs*` 源文件，`all_gather` 也不走 [u4 JIT 系统] 的 generate/build/launch 流程。它是 DeepEP 少数完全由 host 侧用 CUDA Runtime/Driver API 拼出来的原语，与 dispatch/combine 的「省 SM 的 GPU kernel」是两条技术路线。

## 4. 核心概念与源码讲解

### 4.1 AGRS 是什么：session + slot 的批量 all-gather 模型

#### 4.1.1 概念说明

AGRS 是 DeepEP 针对**节点内 NVLink** 全互联场景提供的轻量 all-gather 原语。与 dispatch/combine 追求「跨节点、低 SM 占用」不同，AGRS 的设计目标是：**在一个被显式打开的「会话（session）」里，连续做多次 all-gather，并尽量复用同一块缓冲区与同一套同步信号，从而摊薄每次集合通信的固定开销。**

这里有两个关键设计：

- **session（会话）**：一段被 `create_agrs_session` / `destroy_agrs_session` 显式包围的生命周期。缓冲区与信号槽在 session 内被**线性、重复地**使用，session 结束时才整体回收。
- **slot（槽位）**：每一次 `all_gather` 调用（无论内部是单张量还是 batched 多张量）在信号区里占用**一个** slot，用来承载这一次的「数据到达」握手信号。slot 数被上限 `kNumMaxInflightAGRS = 32` 封顶。

为什么要把多次 all-gather 攒进 session？因为 AGRS 的同步信号是一次性批量下发的流操作（见 4.4），把多次操作打包管理可以减少 host 端的状态切换；同时缓冲区按需线性增长（`agrs_buffer_offset` 累加），session 结束才整体释放，避免每次 all-gather 都重新分配/对齐。

#### 4.1.2 核心流程

一个典型的 AGRS 使用周期：

```text
agrs_set_config(num_max_session_bytes, num_max_inflight)   # 一次性配置上限（含 barrier）
create_agrs_session()                                       # offset=0, slot=0, session_idx++
    for 每次 all_gather:                                     #   推进 agrs_buffer_offset, agrs_buffer_slot_idx++
        （可选）agrs_get_inplace_tensor(...) 写数据          #   零拷贝拿本 rank 槽位
        out, handle = all_gather(tensors)                    #   cudaMemcpyBatchAsync + 信号握手
        handle()                                             #   wait hook：等数据到达
destroy_agrs_session()                                       # 发 session barrier，保证所有人退出后才复用
```

两条硬性约束（来自源码断言，详见 4.1.3）：

1. **仅节点内**：必须满足 `num_nvl_ranks == num_ranks`，即所有 rank 都在同一台机器、共享 NVLink 对称窗口。AGRS **不支持跨节点 RDMA**。
2. **至少两 rank**：`num_ranks > 1`，单 rank 无 all-gather 意义。

#### 4.1.3 源码精读

AGRS 的全部 host 状态定义在 `ElasticBuffer` 的私有成员里，分为「session 级」与「session 内」两组：

[csrc/elastic/buffer.hpp:70-78](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L70-L78) 定义了 AGRS 的状态字段——`num_max_agrs_session_bytes` / `num_max_agrs_per_session` 是配置上限，`agrs_session_idx` 单调递增用作信号值，`agrs_buffer_offset` / `agrs_buffer_slot_idx` 在 session 内线性累加。

两条硬性约束写在 `agrs_set_config` 里：

[csrc/elastic/buffer.hpp:377-389](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L377-L389) 中 `EP_HOST_ASSERT(nccl_context->num_ranks > 1)` 与 `EP_HOST_ASSERT(nccl_context->num_nvl_ranks == nccl_context->num_ranks)` 强制「多 rank 且纯 NVLink 节点内」，并把 session 字节数按 32 对齐。

> 注意 `agrs_set_config` 开头先调用了 `barrier(true, true)`——这是为了在切换 AGRS 配置前「刷新」掉之前可能残留在缓冲区里的 dispatch/combine 操作，保证 AGRS 拿到干净的 buffer。这与 [u7-l1 barrier] 的「数据流切换围栏」作用一致。

#### 4.1.4 代码实践

**实践目标**：在单机多卡上跑通 `tests/elastic/test_agrs.py`，并验证 AGRS 的「纯 NVLink」约束。

**操作步骤**：

1. 阅读测试入口 [tests/elastic/test_agrs.py:195-202](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_agrs.py#L195-L202)，确认默认 8 进程、`--num-max-inflight-agrs` 默认 4。
2. 在单机 8 卡上运行：`torchrun --nproc_per_node=8 tests/elastic/test_agrs.py`（或参照 [u1-l4] 的 `torch.multiprocessing.spawn` 方式）。
3. 观察配置打印 [tests/elastic/test_agrs.py:102-106](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_agrs.py#L102-L106)。

**需要观察的现象**：脚本正常跑完 stress 测试并打印每行 `Ranks: x/8 | 8 x {size} ... avg: ... us (inplace=0/1, batched=0/1)` 的带宽行。

**预期结果 / 待本地验证**：单机 NVLink 环境下应通过；若你在多节点集群上运行，构造 `ElasticBuffer` 后调用 `agrs_set_config` 会因 `num_nvl_ranks == num_ranks` 不成立而触发 `EP_HOST_ASSERT` 失败——这正验证了「AGRS 仅节点内」的约束。具体是否可在你的环境复现，待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：AGRS 为什么不允许跨节点（RDMA）？

**答案**：AGRS 的发送与同步完全依赖 NVLink 对称内存的「直接写对端窗口」能力（`get_sym_ptr` + `cudaMemcpyBatchAsync`），而对称窗口只在节点内（`num_nvl_ranks` 范围）有效；跨节点没有这种全局可直接寻址的对称地址，故源码用 `num_nvl_ranks == num_ranks` 断言把跨节点情形直接排除。

**练习 2**：AGRS 与 dispatch/combine 在「实现层级」上有何本质区别？

**答案**：dispatch/combine 是经 [u4 JIT 系统] 运行时编译、再用 `cuLaunchKernelEx` 启动的 **GPU kernel**；AGRS **没有任何 GPU kernel**，发送用 CUDA Runtime 的 `cudaMemcpyBatchAsync`，同步用 Driver 的 `cuStreamBatchMemOp`，全程由 host 在流上拼出来。

---

### 4.2 session 配置与生命周期：set_config / create / destroy

#### 4.2.1 概念说明

一个 session 有三阶段生命周期：**配置 → 使用 → 销毁**。

- **配置（`agrs_set_config`）**：声明这个 buffer 最多能承载多大的单 session（`num_max_session_bytes`）以及一个 session 内最多多少次 all-gather（`num_max_agrs_per_session`，受 `kNumMaxInflightAGRS = 32` 封顶）。配置只在 buffer 创建后调用一次。
- **创建（`create_agrs_session`）**：把 session 内的累加器（`agrs_buffer_offset`、`agrs_buffer_slot_idx`）清零，并把单调递增的 `agrs_session_idx` 加 1。**这个 `agrs_session_idx` 就是后续信号握手里写入与等待的「值」**——它单调递增，配合 GEQ（大于等于）等待，天然区分不同 session 对同一块信号内存的复用。
- **销毁（`destroy_agrs_session`）**：在 comm stream 上发一次「session 级 barrier」——通知所有对端「我已经退出本 session」，并等待所有对端的同样通知。只有大家都退出后，下一次 `create_agrs_session` 才能安全地把缓冲区与信号槽从头复用。

#### 4.2.2 核心流程

session 销毁时的 barrier 协议是本模块的核心。设当前 session 号为 \(k\)，rank 数为 \(N\)，则每个 rank 在自己的 comm stream 上做：

\[ \text{对所有对端 } b: \quad \text{写 } k \to \text{对端的 session\_signal[self]}; \quad \text{等 } \text{session\_signal}[b] \geq k \]

这是典型的 **release/acquire barrier**：写信号代表「我的数据/操作已全部完成、缓冲区可被复用」；等信号代表「我确认所有人都已完成」。注意「写」与「等」被打包进**同一条** `cuStreamBatchMemOp` 命令（`batched_write_and_wait`），由流保证「先写后等」的顺序。

#### 4.2.3 源码精读

`create_agrs_session` 极简——清零累加器、session 号自增：

[csrc/elastic/buffer.hpp:391-397](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L391-L397) 把 `agrs_buffer_offset` 与 `agrs_buffer_slot_idx` 清零，并令 `agrs_session_idx += 1`。

`destroy_agrs_session` 完成上述 session barrier：

[csrc/elastic/buffer.hpp:399-418](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L399-L418) 中，`write_ptrs[i]` 经 `get_sym_ptr` 翻译成对端 workspace 里的 `session_signal[my_rank]` 地址（即「往对端那里、记录我已完成的那个格子」写），`wait_ptrs[i]` 是本 rank 自己的 `session_signal[peer]`（即「等对端在我这里记录完成」）。两者用 `batched_write_and_wait(..., agrs_session_idx)` 一次下发。

信号区在 workspace 里的布局由 `WorkspaceLayout` 固定。AGRS 信号占 `(kNumMaxInflightAGRS + 1) * kNumMaxRanks` 个 int：前 `kNumMaxInflightAGRS` 段是 per-slot 的 recv 信号，最后 1 段是 session 信号：

[deep_ep/include/deep_ep/common/layout.cuh:76-77](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/layout.cuh#L76-L77) 声明 AGRS 信号区大小为 `(kNumMaxInflightAGRS + 1) * kNumMaxRanks * sizeof(int)`——`+1` 那段就是 session 信号。

[deep_ep/include/deep_ep/common/layout.cuh:166-176](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/layout.cuh#L166-L176) 给出两个寻址函数：`get_agrs_recv_signal_ptr(slot, rank_idx)` 按 `slot * kNumMaxRanks + rank_idx` 索引 per-slot 信号；`get_agrs_session_signal_ptr(rank_idx)` 紧跟其后，索引 session 信号。布局被「按上限常量固定」，所以同一块 buffer 可复用于不同 rank 数的配置（与 [u3-l2] 的 WorkspaceLayout 设计原则一致）。

#### 4.2.4 代码实践

**实践目标**：用 Python 上下文管理器 `agrs_new_session` 替代手写 create/destroy，体会 RAII 式生命周期管理。

**操作步骤**：

1. 阅读 [deep_ep/buffers/elastic.py:652-668](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L652-L668)，看 `agrs_new_session` 如何用 `try/finally` 保证 `destroy_agrs_session` 一定被调用。
2. 阅读 profiling 段 [tests/elastic/test_agrs.py:169-174](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_agrs.py#L169-L174) 的 `with buffer.agrs_new_session():` 用法。
3. 自己写一段最小代码（**示例代码**，非项目原有）：

   ```python
   buffer.agrs_set_config(num_max_session_bytes, num_max_inflight)
   with buffer.agrs_new_session():        # 等价于 create + (finally) destroy
       out, handle = buffer.all_gather(t)
       handle()
   ```

**需要观察的现象**：`with` 块退出后，即便 `all_gather` 抛异常，`destroy_agrs_session` 也会被 `finally` 触发。

**预期结果**：上下文管理器能正确成对调用 create/destroy；若漏调 `destroy_agrs_session` 而直接再次 `create_agrs_session`，会在 `EP_HOST_ASSERT(not agrs_in_session)` 处失败（[csrc/elastic/buffer.hpp:392](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L392)）。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 session 信号用 `agrs_session_idx`（单调递增）作写入值，且等待用 GEQ？

**答案**：同一个 slot 的信号内存在不同 session 间会被复用。若用固定值（如 1）并在新 session 重新清零，存在「A rank 已开始新 session 写入、B rank 还在旧 session 等待」的竞态。改用单调递增的 `agrs_session_idx` + GEQ 等待后，新 session 的值严格大于旧值，旧值无法错误地满足新 session 的等待；且无需显式清零信号内存。

**练习 2**：`destroy_agrs_session` 为什么必须先 `stream_wait(comm_stream, compute_stream)`？

**答案**：见 [csrc/elastic/buffer.hpp:404-405](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L404-L405)。destroy 要在 comm stream 上发 session barrier、宣告「缓冲区可被复用」，必须先确保 compute stream 上所有读写该缓冲区的操作（如用户对 `out` 张量的消费）都已排到 comm stream 之前，否则下一个 session 复用缓冲区时会与尚未完成的读写在数据上冲突。

---

### 4.3 零拷贝 inplace 张量：agrs_get_inplace_tensor 与批量模式

#### 4.3.1 概念说明

`all_gather` 的输入张量有两种来源，对应两种工作模式：

- **非 inplace（外部张量）**：输入是用户自己 `torch.randn` 出来的、位于 AGRS 缓冲区**之外**的普通显存。此时 all-gather 需要把本 rank 的数据**拷贝**进缓冲区里本 rank 的槽位，再由对端来取——本 rank 自身这一份拷贝是必要的。
- **inplace（缓冲区内张量）**：用户先用 `agrs_get_inplace_tensor` 在 AGRS 缓冲区里**本 rank 的槽位**上拿到一个 `torch::from_blob` 的视图（零拷贝、不分配新显存），把数据直接写进这个视图，然后再以它为输入调用 `all_gather`。此时本 rank 的数据**已经在它该在的位置**，all-gather 检测到「输入指针落在缓冲区内」后**跳过本 rank 的自拷贝**，少一次 `num_ranks` 中的 1 份拷贝。

另一个正交维度是 **batched（批量）模式**：

- **单张量**：`all_gather(t)`，输入一个张量，返回 `(gathered, handle)`。
- **batched 多张量**：`all_gather([t1, t2, ...])`，输入一组张量，**一次调用**把它们一起 all-gather，返回 `(*gathered_list, handle)`。关键点：**一次 batched 调用只占用一个 slot**（`agrs_buffer_slot_idx += 1`），无论内部有几个张量。这让 slot 预算（上限 32）能容纳更多逻辑操作。

#### 4.3.2 核心流程

inplace 张量的获取与 all-gather 的 offset 推进必须**严格对齐**：

```text
agrs_buffer_offset = O （session 起始为 0）
agrs_get_inplace_tensor([B0, B1])        # 用「局部 offset = O」返回两个 view，不改成员
    view0 = buffer[O + B0*rank_idx : ...]   # 本 rank 在第 0 块的槽位
    view1 = buffer[O + align(B0*N,32) + B1*rank_idx : ...]
    （局部 offset 推进，但成员 agrs_buffer_offset 仍为 O）
把数据写进 view0 / view1
all_gather([view0, view1])                # 读成员 O，推进成员 offset
    offset[0]=O, offset[1]=O+align(B0*N,32)
    inplace 检测：view0/view1 指针 ∈ [buffer, buffer+num_max_agrs_session_bytes) → 跳过自拷贝
    agrs_buffer_offset = O + align(B0*N,32) + align(B1*N,32)
    agrs_buffer_slot_idx += 1             # 整个 batched 调用只占一个 slot
```

> 关键不变式：`agrs_get_inplace_tensor` 是 `const` 方法，**只读** `agrs_buffer_offset`、用局部变量推进，**不修改成员**；真正推进成员 `agrs_buffer_offset` 的是紧随其后的 `all_gather`。因此二者必须用相同的 shape 序列、紧挨着调用，offset 才能对上（否则 inplace 自拷贝跳过会出错）。

#### 4.3.3 源码精读

`agrs_get_inplace_tensor` 用 `torch::from_blob` 在缓冲区本 rank 槽位上建视图，**不改成员 offset**：

[csrc/elastic/buffer.hpp:420-436](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L420-L436) 中 `int64_t offset = agrs_buffer_offset;` 是局部拷贝，循环内 `offset += ...` 只推进局部量；返回的张量地址是 `buffer + offset + num_bytes * rank_idx`——即本 rank 在该块中的槽位。

`all_gather` 的 inplace 检测与 offset/slot 推进在循环头部：

[csrc/elastic/buffer.hpp:444-459](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L444-L459) 中：
- `x_offset = ptr_diff(x.data_ptr(), buffer)` 算输入指针相对缓冲区头的偏移；
- `is_inplace = 0 <= x_offset and x_offset < num_max_agrs_session_bytes` 判定是否落在 AGRS 区；
- `num_copies += num_ranks - is_inplace`——inplace 时少算本 rank 这一拷贝；
- `agrs_buffer_offset += align(x.nbytes() * num_ranks, 32)` 推进成员 offset（每块要容纳所有 rank 的副本，故乘 `num_ranks`）。

[csrc/elastic/buffer.hpp:494-497](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L494-L497) 紧接着 `agrs_buffer_slot_idx += 1`——确认**每次 `all_gather` 调用（含 batched）只占一个 slot**。

Python 侧的 batched/single 分发：

[deep_ep/buffers/elastic.py:706-726](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L706-L726) 中，单张量走 `all_gather((t,))` 返回 `tensors[0]`，序列走 batched 返回 `*tensors`。

#### 4.3.4 代码实践

**实践目标**：对比 inplace 与非 inplace、单张量与 batched 四种组合，观察它们如何消耗缓冲区与 slot。

**操作步骤**：

1. 阅读 [tests/elastic/test_agrs.py:57-84](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_agrs.py#L57-L84) 的 `do_all_gather`，看它如何用 `is_inplace` 决定是否先 `agrs_get_inplace_tensor` + `copy_`。
2. 阅读 profiling 段 [tests/elastic/test_agrs.py:162-187](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_agrs.py#L162-L187) 的四象限循环（`is_inplace ∈ {False,True}` × `is_batched ∈ {False,True}`）。
3. 运行该 profiling，记录四象限的 `avg us` 与 `GB/s`。

**需要观察的现象**：inplace 模式通常比非 inplace 略快（省一次自拷贝）；batched 模式在多张量时摊薄了每次调用的固定开销。

**预期结果**：四象限都能通过正确性断言（见 [tests/elastic/test_agrs.py:136-138](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_agrs.py#L136-L138) 的 `torch.equal(results[i], refs[i])`）。具体带宽数字待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：若把 `agrs_get_inplace_tensor` 拿到的 view 的 shape 改大，再传给 `all_gather`，会发生什么？

**答案**：`all_gather` 内部用 `x.nbytes()` 推进 offset（[buffer.hpp:454](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L454)），inplace 校验 [buffer.hpp:455](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L455) 要求 `x.data_ptr() == buffer + offset[i] + x.nbytes() * rank_idx`。若 view 被改大，该等式不再成立，会触发 `EP_HOST_ASSERT` 失败；即使绕过，也会让后续张量 offset 错位、数据错乱。这就是为何 `agrs_get_inplace_tensor` 与 `all_gather` 必须用一致 shape。

**练习 2**：一次 batched 的 3 张量 `all_gather` 占几个 slot？消耗多少 offset？

**答案**：占 1 个 slot（`agrs_buffer_slot_idx += 1` 仅一次）；offset 推进量为 \(\sum_{i=0}^{2} \text{align}(B_i \cdot N, 32)\)，其中 \(B_i\) 是第 i 个张量的字节数、\(N\) 是 rank 数。

---

### 4.4 all_gather 的实现：cudaMemcpyBatchAsync 发送 + 信号握手

#### 4.4.1 概念说明

`all_gather` 的主体把三件事在 **comm stream** 上串成一条流序：**发送数据 → 发/等完成信号 → 记录 event 给计算流等**。三件事都不启动 GPU kernel：

1. **发送（`cudaMemcpyBatchAsync`）**：本 rank 把自己的数据写到**每个**对端 rank 的对称槽位。对端槽位地址由 `get_sym_ptr` 把本 rank 的缓冲区指针翻译成对端的对称指针。inplace 张量对应的「自拷贝」（`src_ptr == dst_ptr`）被显式跳过。
2. **信号握手（`cuStreamBatchMemOp`，封装为 `batched_write_and_wait`）**：发完数据后，本 rank 向每个对端的 `recv_signal[slot][self]` 写入 `current_session`（告诉对端「我发给你的数据到了」），同时在同一条命令里等待本 rank 的 `recv_signal[slot][peer] >= current_session`（等对端宣告「发给我的数据到了」）。这是一次 per-slot 的 all-to-all barrier。
3. **wait hook（`EventHandle`）**：在 comm stream 上 record 一个 event，返回一个 callable；用户调用它时，计算流 `stream_wait` 这个 event，从而保证「读 `out` 之前，数据真的到了」。

返回的 `out` 张量是**急切构造（eagerly built）**的缓冲区视图（`torch::from_blob`，前导维 `num_ranks`），但它们**此时未必已填好数据**——必须调用 handle（wait）之后才能安全读取。

#### 4.4.2 核心流程

设当前 session 号 \(k\)、本次 slot 号 \(s\)、rank 数 \(N\)：

```text
1) 组装拷贝列表（对每个输入张量 j、每个目标 rank r）：
     src = tensors[j].data_ptr()
     dst = get_sym_ptr(buffer + offset[j] + nbytes_j * self_rank, r)   # 对端槽位
     若 src != dst（非自拷贝）：加入 (src, dst, nbytes_j)
2) stream_wait(comm_stream, compute_stream)                             # 等输入数据就绪
3) cudaMemcpyBatchAsync(所有 (src,dst,size), comm_stream)               # 一次下发所有发送
4) 信号握手（对每个对端 b）：
     写 k → get_sym_ptr(recv_signal[s][self], b)   （在对端记录我已完成）
     等  recv_signal[s][b] >= k                    （等对端记录它已完成）
   一次 batched_write_and_wait 下发
5) 构造 out[j] = from_blob(buffer + offset[j], [N, *shape])            # 急切视图
6) event = EventHandle(comm_stream)                                     # 在 comm stream record
   返回 (out, handle)；handle() 做 stream_wait(compute_stream, event)
```

数据正确性依赖两条流序约束：拷贝（步骤 3）排在信号写（步骤 4）之前，故写信号时数据必然已落地对端；用户读 `out`（步骤 6 之后）排在 handle 的 event 等待之后，而 event 又排在信号等之后，故读时所有对端数据必然已到。

#### 4.4.3 源码精读

发送数据的拷贝列表组装与 `cudaMemcpyBatchAsync` 下发：

[csrc/elastic/buffer.hpp:465-492](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L465-L492) 中，双重循环枚举 `(目标 rank i, 输入张量 j)`，用 `get_sym_ptr(... buffer + offset[j] + x.nbytes() * rank_idx, dst_rank_idx)` 算出对端在该块中**本 rank 对应**的槽位地址；`if (src_ptr != dst_ptr)` 跳过自拷贝；最后用 `cudaMemcpyBatchAsync` 一次下发。注意 `attrs` 设置了 `cudaMemcpyFlagPreferOverlapWithCompute`，允许拷贝与计算重叠。

per-slot 的信号握手：

[csrc/elastic/buffer.hpp:494-506](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L494-L506) 中，`write_ptrs[i]` 是「对端 workspace 里、记录我（self）完成的格子」（经 `get_sym_ptr` 翻译），`wait_ptrs[i]` 是「本 rank workspace 里、记录对端完成的格子」；二者与 `current_session` 一起交给 `batched_write_and_wait`。

`batched_write_and_wait` 的底层实现——把多个 `WRITE_VALUE_32` 与 `WAIT_VALUE_32(GEQ)` 打包进一条 `cuStreamBatchMemOp`：

[csrc/kernels/backend/cuda_driver.cu:45-52](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/cuda_driver.cu#L45-L52) 中，先把所有 write 操作填进 `ops` 前 `write_ptrs.size()` 项，再把所有 wait 操作填进剩余项，统一调 `lazy_cuStreamBatchMemOp` 下发。

[csrc/kernels/backend/cuda_driver.cu:12-29](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/cuda_driver.cu#L12-L29) 是单个操作的构造：`CU_STREAM_MEM_OP_WRITE_VALUE_32` 写一个 32 位值，`CU_STREAM_MEM_OP_WAIT_VALUE_32` 配 `CU_STREAM_WAIT_VALUE_GEQ` 等待「大于等于」。

> `cuStreamBatchMemOp` 是 Driver API，DeepEP 用 [csrc/utils/lazy_driver.hpp:21-31](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/utils/lazy_driver.hpp#L21-L31) 的 `dlopen("libcuda.so.1")` + `dlsym` 惰性加载（见 [lazy_driver.hpp:55](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/utils/lazy_driver.hpp#L55) 注册的 `cuStreamBatchMemOp`），避免安装期硬链接 driver。

对称指针翻译 `get_sym_ptr`：

[csrc/kernels/backend/nccl.cu:142-145](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/nccl.cu#L142-L145) 中，先算 `ptr` 相对本 rank 窗口头的偏移，再加到目标 rank 的窗口头 `nvl_window_ptrs[dst_rank_idx]` 上——这正是「对称窗口内相同偏移即对端对应地址」的含义（承接 [u3-l4]）。

最后是 wait hook 的构造：

[csrc/elastic/buffer.hpp:508-523](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L508-L523) 急切构造 `out` 视图（前导维 `num_ranks`），在 comm stream 建 `EventHandle`，并返回一个 lambda：它先断言「仍在同一 session（`current_session == agrs_session_idx`）」、且「调用时的 compute stream 与下发时一致」，再 `stream_wait(compute_stream, event)`。这正是 [u2-l4] 双流模型里「通信流 record、计算流 wait」的标准重叠原语。

#### 4.4.4 代码实践

**实践目标**：在一个 session 内连续做 2 次 `all_gather`（一次单张量、一次 batched），手动追踪 `agrs_buffer_offset` 与 `agrs_buffer_slot_idx` 的累加过程。

**操作步骤**：

1. 准备一个单机 8 卡环境（\(N=8\)），shape \(S=(32,64,2048)\)，dtype=bfloat16（每元素 2 字节），故单个张量 \(B = 32 \times 64 \times 2048 \times 2 = 8\,388\,608\) 字节。
2. 用 [deep_ep/buffers/elastic.py:458-477](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L458-L477) 的 `get_agrs_num_max_session_bytes` 算单 session 所需字节，再经 [deep_ep/buffers/elastic.py:479-495](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L479-L495) 的 `get_agrs_buffer_size_hint` 取 2 MB 对齐值作为 `num_bytes` 建缓冲区，并 `agrs_set_config`。
3. 写一段最小代码（**示例代码**，参考 [tests/elastic/test_agrs.py:57-84](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_agrs.py#L57-L84) 的 `do_all_gather`）：

   ```python
   with buffer.agrs_new_session():
       # 第 1 次：单张量
       t0 = torch.randn(S, dtype=torch.bfloat16, device='cuda')
       out0, h0 = buffer.all_gather(t0)      # offset: 0 -> align(B*N,32); slot: 0 -> 1
       h0()                                    # 等数据到达
       # 第 2 次：batched 两张量
       t1 = torch.randn(S, dtype=torch.bfloat16, device='cuda')
       t2 = torch.randn(S, dtype=torch.bfloat16, device='cuda')
       o1, o2, h12 = buffer.all_gather([t1, t2])  # offset: align(B*N,32) -> +2*align(B*N,32); slot: 1 -> 2
       h12()
   ```

4. 对照源码手算累加值。

**需要观察的现象 / 预期结果**：以 \(N=8\)、\(B=8\,388\,608\) 为例，\(\text{align}(B \cdot N, 32) = 67\,108\,864\) 字节（64 MiB）。
- `create_agrs_session` 后：`agrs_buffer_offset = 0`、`agrs_buffer_slot_idx = 0`。
- 第 1 次单张量后：`agrs_buffer_offset = 64 MiB`、`agrs_buffer_slot_idx = 1`（占用 slot 0）。
- 第 2 次 batched 两张量后：`agrs_buffer_offset = 64 + 2×64 = 192 MiB`、`agrs_buffer_slot_idx = 2`（整个 batched 调用只占 slot 1 一次）。

可见 batched 模式用 **1 个 slot** 完成了 2 个张量的 all-gather——这正是它「slot 高效」的体现。你可以把 `EP_BUFFER_DEBUG` 之类调试开关打开（若存在）或在 C++ 侧加一行临时打印来核对这两个成员值（**注意：改源码仅用于本地学习，勿提交**）。具体能否在你的环境精确复现上述数字，待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `out` 张量在 `all_gather` 返回时就构造好，却必须调用 handle 后才能读？

**答案**：`out` 是缓冲区的急切视图（[buffer.hpp:509-514](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L509-L514)），构造它不需要数据就绪；但来自对端的数据要等信号握手（[buffer.hpp:494-506](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L494-L506)）完成、且 handle 把 comm stream 的 event 同步到 compute stream 之后才必然到达（[buffer.hpp:517-522](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L517-L522)）。提前读会读到未初始化或旧数据。

**练习 2**：信号握手里「写」和「等」能否拆成两次 `cuStreamBatchMemOp`？

**答案**：技术上可以，但源码刻意打包成**一次** `batched_write_and_wait`（[cuda_driver.cu:45-52](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/cuda_driver.cu#L45-L52)）。好处是：单条流命令里 write 排在 wait 之前由流序保证，且只下发一次、减少 host 与驱动之间的往返；拆开则多一次下发，且语义上要自行保证「写」先于「等」。

**练习 3**：若把信号等待从 `CU_STREAM_WAIT_VALUE_GEQ` 改成 `EQ`（等于），会出什么问题？

**答案**：GEQ 容忍「信号值 ≥ 期望」即可放行，配合单调递增的 `agrs_session_idx` 能正确处理跨 session 复用；若改成 EQ（严格等于），一旦某次写入因任何原因使信号值超过当前 session 号（理论上不会，但属于防御），等待会永久挂起。更重要的是，GEQ 是 release/acquire barrier 的标准用法，EQ 不适合这种「计数式」完成信号。

## 5. 综合实践

把本讲四个模块串起来，完成一个「**随机压力下的 AGRS 正确性自检**」小任务，本质上是读懂并复述 [tests/elastic/test_agrs.py:20-54](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_agrs.py#L20-L54) 的 `generate_stress_ops` 在做什么，然后回答：

1. 它在一个 session 内随机混合了哪几种操作？（提示：`create_session` / `ag`（可 inplace、可 batched）/ `fetch`（调用 wait hook）/ `destroy_session` + 重建。）
2. 为什么 stress 测试里 `ag` 操作的 inflight 数受 `num_max_inflight_agrs` 限制？这与本讲的 `agrs_buffer_slot_idx` / `kNumMaxInflightAGRS` 有什么关系？
3. 为什么每次 `destroy_session` 后必须再 `create_session` 才能继续 `ag`？（联系 4.2 的 session barrier。）
4. `fetch` 步骤里 `for h in wait_handles: h()` 之后再 `out.clone()`，为什么必须先 `h()` 再 clone？（联系 4.4 的 wait hook。）

进阶：把 stress 迭代数 `--num-stress-iterations` 调大（如 16），观察在随机 inplace/batched 组合下是否始终通过 [tests/elastic/test_agrs.py:136-138](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_agrs.py#L136-L138) 的逐位 `torch.equal` 断言。这能验证 session 复用、slot 累加、inplace 检测三条机制在乱序压力下的正确性。

## 6. 本讲小结

- AGRS 是 DeepEP 针对节点内 NVLink 的实验性 all-gather 原语，**没有 GPU kernel**：发送用 `cudaMemcpyBatchAsync`、同步用 `cuStreamBatchMemOp`，全程 host 在 comm stream 上拼出。
- 采用 **session + slot** 模型：一个 session 内线性复用缓冲区（`agrs_buffer_offset` 累加）与信号槽（`agrs_buffer_slot_idx` 每次 `all_gather` 加 1，上限 32）。
- session 生命周期由 `agrs_set_config`（配置+barrier）/ `create_agrs_session`（清零+session 号自增）/ `destroy_agrs_session`（session barrier）三段构成；单调递增的 `agrs_session_idx` 作信号值、配 GEQ 等待，安全支持跨 session 复用。
- `agrs_get_inplace_tensor` 用 `torch::from_blob` 在缓冲区本 rank 槽位建零拷贝视图（**不改成员 offset**），配合 `all_gather` 的指针落点检测跳过自拷贝；batched 模式一次调用多张量只占一个 slot。
- `all_gather` 的正确性靠流序保证：`cudaMemcpyBatchAsync`（发送）→ `batched_write_and_wait`（per-slot all-to-all 信号 barrier）→ `EventHandle`（让计算流等）；`out` 是急切视图，必须调 wait hook 后才能读。
- 两条硬约束：`num_ranks > 1` 且 `num_nvl_ranks == num_ranks`（纯节点内 NVLink）。

## 7. 下一步学习建议

- **回到 barrier 的信号机制**：本讲的 `cuStreamBatchMemOp` 信号握手与 [u7-l1 跨 rank GPU Barrier] 的 `red.release.sys`/`ld.acquire.sys` 是两类不同的 release/acquire 实现（前者 host 流操作、后者 cooperative kernel），建议对比阅读 [csrc/kernels/elastic/barrier.hpp] 与 [csrc/kernels/backend/cuda_driver.cu]，体会「何时用 kernel、何时用流操作」。
- **深入对称内存**：本讲反复用到 `get_sym_ptr`，其根基是 [u3-l4] 的 NCCL Gin 对称窗口。建议回到 [csrc/kernels/backend/symmetric.hpp] 理解 CUDA VMM 如何把显存暴露给所有 peer。
- **reduce-scatter 的留白**：当前仓库只实现了 all-gather。若未来出现 `reduce_scatter`，它很可能复用本讲的 session/slot 与信号框架，只是在发送侧叠加归约。可以关注 `csrc/elastic/buffer.hpp` 与 pybind 绑定 [csrc/elastic/buffer.hpp:1359-1363](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L1359-L1363) 的演进。
- **与 dispatch/combine 的对照**：学完本讲后，可重读 [u5-l1] 与 [u6-l1]，从「是否需要 GPU kernel、是否跨节点、同步用什么」三个维度，建立 DeepEP 各通信原语的全景对照表。

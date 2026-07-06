# Pipeline Parallelism：环形 send/recv

## 1. 本讲目标

本讲讲解 DeepEP V2 中**流水线并行（Pipeline Parallelism, PP）**的两个原语 `pp_send` / `pp_recv`。它们是独立于 dispatch/combine 之外的另一类通信：在训练大模型时，把不同 transformer 层切分到不同 rank（流水线级），相邻级之间需要把激活张量沿「环」向前/向后传递。

读完本讲，你应当能够：

1. 说清楚为什么 PP 只允许与**环形相邻 rank**（prev / next）通信，以及这个约束如何在 host 断言里被强制。
2. 掌握 `pp_set_config` 的两个参数（`num_max_tensor_bytes` / `num_max_inflight_tensors`），并能解释缓冲区为何需要 **4 倍因子**。
3. 读懂 `pp_send_impl` / `pp_recv_impl` 两个 GPU 内核：TMA 本地搬运 → RDMA put 跨节点投递 → NCCL Gin 信号握手，以及 `slot_idx = count % num_max_inflight_tensors` 的轮转双缓冲。
4. 理解「GPU 侧超时 + trap」如何把死锁变成可定位的崩溃，以及 `num_sms` 如何决定并行度。

本讲承接 [u7-l1（barrier 同步）](u7-l1-barrier.md)（PP 复用同样的 NCCL Gin 信号与 grid 同步原语）与 [u3-l4（NCCL Gin 对称内存）](u7-l1-barrier.md)（`gin.put` / `gin.signal` 跨 rank 寻址）。

## 2. 前置知识

- **流水线并行（Pipeline Parallelism）**：把模型按层切分到多个 rank，每个 rank 只持有若干层。前向时激活从 rank 0 → rank 1 → … → rank N-1 逐级传递，反向时反向回流。这与 EP（专家并行，按「专家」切分）正交，二者常组合使用。
- **环形拓扑（ring）**：把 N 个 rank 排成一圈，每个 rank 只与「前一个 `(r-1) % N`」和「后一个 `(r+1) % N`」直接通信。1F1B（one-forward-one-backward）等主流 PP 调度都建立在这个环形假设上。
- **NCCL Gin**：DeepEP V2 的轻量通信后端，复用已有 NCCL communicator，提供 `gin.put`（RDMA 写远端）与 `gin.signal`（远端计数信号）两个原语。详见 [u3-l4](u7-l1-barrier.md)。
- **对称内存（symmetric memory）**：所有 rank 在各自的 NCCL 窗口里布局完全一致，因此「本 rank 计算出的偏移」直接等于「对端 rank 同一偏移」，`gin.put` 只需把本地源地址与对称偏移传过去即可。详见 [u3-l4](u7-l1-barrier.md)。
- **TMA（Tensor Memory Access）**：Hopper 架构的异步批量拷贝引擎，配合 mbarrier 做异步同步，是内核里搬运张量的主力。详见 [u8-l1](u8-l1-ptx-tma-mbarrier.md)。
- **JIT 编译**：`pp_send_impl` / `pp_recv_impl` 是模板，运行时把 `kNumSMs`、`kNumRanks`、`kNumSmemBytes`、`kNumTimeoutCycles` 烘焙成编译期常量后再实例化。详见 [U4](u4-l1-jit-overview.md)。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [csrc/elastic/buffer.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp) | host 层：`pp_set_config` / `pp_send` / `pp_recv` 三个 C++ 方法，含邻居断言与超时换算 |
| [csrc/kernels/elastic/pp_send_recv.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/pp_send_recv.hpp) | JIT 启动器：`PPSendRuntime` / `PPRecvRuntime` 与 `launch_pp_send` / `launch_pp_recv` |
| [deep_ep/include/deep_ep/impls/pp_send_recv.cuh](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/pp_send_recv.cuh) | 真 GPU 内核：`pp_send_impl` / `pp_recv_impl` / `tma_copy` / `check_signal` |
| [deep_ep/include/deep_ep/common/layout.cuh](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/layout.cuh) | `WorkspaceLayout` 中的 `get_pp_send_count_ptr` / `get_pp_recv_count_ptr` 计数槽 |
| [deep_ep/include/deep_ep/common/comm.cuh](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/comm.cuh) | `timeout_while` + `ptx::trap` 超时保护 |
| [deep_ep/buffers/elastic.py](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py) | Python 接口与 `get_pp_buffer_size_hint`（4 倍因子公式） |
| [tests/elastic/test_pp.py](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_pp.py) | 压力测试与带宽基准 |

## 4. 核心概念与源码讲解

### 4.1 环形通信模型：为什么 PP 只允许 prev / next 邻居

#### 4.1.1 概念说明

PP 的物理模型是「流水线」：rank `r` 只需要把激活送给下一级 `(r+1) % N`（前向）或上一级 `(r-1+N) % N`（反向/梯度）。这把 all-to-all 退化为环上点对点，每 rank 只维护两条逻辑链路，缓冲区、QP、信号槽都可以按「正好两个邻居」精确分配，不必为「任意 rank 对」预留资源。

DeepEP 把这个物理约束写成了**硬性 host 断言**：调用 `pp_send(t, dst)` 或 `pp_recv(t, src)` 时，`dst` / `src` 必须等于预先算好的 `next_rank_idx` 或 `prev_rank_idx`，否则直接抛异常。这避免用户误把 PP 当成通用 send/recv 用。

#### 4.1.2 核心流程

1. `pp_set_config` 时，host 根据 `rank_idx` 与 `num_ranks` 预计算两个邻居：
   - `prev_rank_idx = (rank_idx + num_ranks - 1) % num_ranks`
   - `next_rank_idx = (rank_idx + 1) % num_ranks`
2. 之后每次 `pp_send` / `pp_recv`，host 断言目标 rank ∈ {prev, next}。
3. 内核侧用一个极简函数 `get_buffer_offset` 把「prev / next」映射成下标 `0` / `1`，从而定位本 rank 缓冲区里的两个方向槽。

> 注意：N=2 时 prev 与 next 都指向对方（`(0+2-1)%2=1`、`(0+1)%2=1`），两邻居重合，这是合法的退化情形，也是本讲综合实践要用的最小配置。

#### 4.1.3 源码精读

邻居的预计算在 `pp_set_config` 里：

[csrc/elastic/buffer.hpp:327-337](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L327-L337) —— `pp_set_config` 先 `barrier` 刷新之前的操作，断言缓冲区足够大，再算出 `prev_rank_idx` / `next_rank_idx`，并把张量字节数对齐到 32。

邻居约束的强制在 `pp_send` / `pp_recv` 入口：

[csrc/elastic/buffer.hpp:339-356](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L339-L356) —— `pp_send`：断言 `dst_rank_idx == prev_rank_idx or dst_rank_idx == next_rank_idx`，再用 `num_sms == 0 ? 全部 SM : num_sms` 决定并行度，把 `num_gpu_timeout_cycles` 传给启动器。

[csrc/elastic/buffer.hpp:358-375](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L358-L375) —— `pp_recv`：同样的邻居断言（这次是 `src_rank_idx`），其余参数与 send 对称。

内核侧「prev/next → 0/1」的映射只有三行：

[deep_ep/include/deep_ep/impls/pp_send_recv.cuh:11-16](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/pp_send_recv.cuh#L11-L16) —— `get_buffer_offset<kNumRanks>(src, dst)`：若 `dst` 是 `src` 的后继则返回 `(0,1)`，否则返回 `(1,0)`。这两个整数就是「本 rank 在对端视角的槽位号」与「对端在本 rank 视角的槽位号」。

#### 4.1.4 代码实践

**实践目标**：直观感受邻居约束与 N=2 退化。

**操作步骤**：
1. 构造一个 `ElasticBuffer` 并调用 `pp_set_config`。
2. 在 rank 0 上尝试 `buffer.pp_send(t, dst=(rank_idx + 2) % num_ranks)`（跳过相邻 rank）。
3. 观察是否触发 `EP_HOST_ASSERT`。

**预期结果**：当 `num_ranks >= 3` 时，`(rank_idx + 2) % num_ranks` 既不是 prev 也不是 next，host 断言失败、抛出异常；`num_ranks == 2` 时 prev==next==1，发送合法。具体运行行为**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：若 `num_ranks == 1`，PP 还能用吗？

**答案**：不能。PP 要求至少两个 rank 才能形成环，`test_pp.py` 第 54 行也写了 `assert num_ranks > 1`。单 rank 没有「邻居」可通信。

**练习 2**：`prev_rank_idx` 为什么写成 `(rank_idx + num_ranks - 1) % num_ranks` 而不是 `(rank_idx - 1) % num_ranks`？

**答案**：C/C++ 的 `%` 对负数可能返回负值（`rank_idx == 0` 时 `-1 % N` 在多数实现里是 `-1`），先加 `num_ranks` 再取模可保证结果落在 `[0, num_ranks)`，是处理环形下标的常规技巧。

---

### 4.2 配置与缓冲区布局：pp_set_config 与 4 倍因子

#### 4.2.1 概念说明

PP 与 dispatch/combine 共用同一个 `ElasticBuffer` 的 GPU buffer 区段。在切换到 PP 模式前，必须告诉缓冲区「这次要传的张量多大、同时最多在途几个」，这就是 `pp_set_config(num_max_tensor_bytes, num_max_inflight_tensors)` 的职责。它做三件事：

1. 先 `barrier` 一次，保证之前 dispatch/combine 的数据全部落地，不被 PP 覆盖。
2. 记录张量上限（按 32 字节对齐，配合 `LDG.256`）与最大在途数。
3. 算出两个邻居 rank（见 4.1）。

#### 4.2.2 核心流程：4 倍因子从哪来

缓冲区需要同时容纳**两个方向 × 两个角色**：

- 两个方向：prev 邻居、next 邻居；
- 两个角色：发送缓冲区（send buffer，本地写、对端读）、接收缓冲区（recv buffer，对端写、本地读）。

因此总共有 `2 (方向) × 2 (角色) = 4` 个区域，每个区域还要能放下 `num_max_inflight_tensors` 个张量做双缓冲流水线。于是缓冲区字节数 = `num_max_tensor_bytes × num_max_inflight_tensors × 4`，这就是 **4 倍因子**。

每个区域里的槽位用**取模轮转**复用：

\[
\text{slot\_idx} = \text{count} \bmod \text{num\_max\_inflight\_tensors}
\]

`count` 是单调递增的累计计数。第 `count` 个张量落到槽 `count % N`，第 `count + N` 个张量会**复用**同一个槽——只要此时对端已经消费完第 `count` 个即可，靠信号握手保证（4.3）。这就是「在途数 = 流水线深度」的含义。

四个区域的下标编排：

| 区域下标 | 角色 | 含义 |
|----------|------|------|
| `0` | recv | 来自「在本地视角为槽 0 的邻居」的接收区 |
| `1` | recv | 来自另一邻居的接收区 |
| `2` | send | 发往「在本地视角为槽 0 的邻居」的发送区 |
| `3` | send | 发往另一邻居的发送区 |

内核里 send buffer 用 `(dst_idx_in_local + 2)`（落到 2 或 3），recv buffer 用 `(local_idx_in_dst + 0)`（落到 0 或 1）。

#### 4.2.3 源码精读

Python 侧的 4 倍因子公式（带 2 MB 对齐）：

[deep_ep/buffers/elastic.py:438-456](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L438-L456) —— `get_pp_buffer_size_hint`：注释明说「Each buffer (send and recv, * 2) contains prev and next rank (* 2) in the ring」，返回 `align(num_max_tensor_bytes * num_max_inflight_tensors * 2 * 2, buffer_alignment)`，即 2 MB 对齐的 4 倍。

host 侧的等价断言（用同一个 4 倍关系校验传入的 `num_bytes` 够大）：

[csrc/elastic/buffer.hpp:327-337](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L327-L337) —— 关键断言 `num_max_tensor_bytes * num_max_inflight_tensors * 2 * 2 <= num_buffer_bytes`，与 Python hint 公式一一对应。

槽位轮转与四区寻址在 `pp_send_impl` 里：

[deep_ep/include/deep_ep/impls/pp_send_recv.cuh:125-139](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/pp_send_recv.cuh#L125-L139) —— 读 workspace 里的 `send_count`，`slot_idx = send_count % num_max_inflight_tensors`；send buffer 落在 `(dst_idx_in_local + 2) * inflight + slot_idx`，recv buffer（对端将读取的对称地址）落在 `(local_idx_in_dst + 0) * inflight + slot_idx`。

计数槽本身存放在 workspace 控制平面里：

[deep_ep/include/deep_ep/common/layout.cuh:153-164](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/layout.cuh#L153-L164) —— `get_pp_send_count_ptr(offset)` 与 `get_pp_recv_count_ptr(offset)` 各占 `2 * sizeof(int64_t)`（两个邻居各一个 64 位计数），紧跟在 channel/AGRS 信号区之后，是对称内存里所有 rank 共享可见的状态。

#### 4.2.4 代码实践

**实践目标**：手算并核对缓冲区大小。

**操作步骤**：
1. 取 `num_max_tensor_bytes = 4096 × 7168 × 2`（BF16，4096 token × 7168 hidden），`num_max_inflight_tensors = 4`。
2. 按 4 倍因子手算所需字节：`B = align32(tensor_bytes) × 4 × 4`，再 2 MB 对齐。
3. 调用 `ElasticBuffer.get_pp_buffer_size_hint(...)` 与手算值对比。

**预期结果**：两者量级一致（提示函数只是「最小建议」，实际构造 `ElasticBuffer` 时给的 `num_bytes` 可以更大）。具体数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `num_max_inflight_tensors` 设成 1，会失去什么？

**答案**：失去流水线重叠能力——发送方必须等接收方消费完上一个张量、释放信号后才能复用唯一那个槽（`slot_idx = count % 1 == 0` 恒为 0），退化为「发一个、等一个」的停顿式通信，无法掩盖 RDMA 延迟。

**练习 2**：4 倍因子里，send 和 recv 区域为何要**同时**为两个邻居都预留？

**答案**：因为同一个 rank 在一个 step 里可能既要向前（next）送激活、又要向后（prev）送梯度（1F1B 调度），两条链路并发，必须各自有独立缓冲区才不会互相覆盖。

---

### 4.3 send/recv 内核：TMA 搬运 + RDMA put + 信号握手

#### 4.3.1 概念说明

`pp_send_impl` / `pp_recv_impl` 是两个 cooperative GPU 内核（`__launch_bounds__(32, 1)`，每个 block 一个 warp，`num_sms` 个 block）。它们用**三段式**完成一次跨 rank 传输：

- **send**：① 本地把源张量 TMA 拷进 send buffer；② grid 同步；③ 由 SM 0 发起一次 `gin.put`（RDMA 写到对端 recv buffer 的对称地址），并递增「数据就绪」信号。
- **recv**：① 轮询等待对端的「数据就绪」信号到达；② 本地把 recv buffer 里的数据 TMA 拷出到用户输出张量；③ grid 同步；④ 由 SM 0 递增「槽位释放」信号，告诉发送方这个槽可以复用了。

send 侧在写之前也会先等一个「释放」信号——确认目标槽位已被上一轮的接收方消费（`send_count - num_max_inflight_tensors + 1`），从而保证取模轮转不会踩到还没读走的旧数据。两个方向的信号握手正好闭环。

#### 4.3.2 核心流程（伪代码）

```
pp_send_impl(x, dst):
    count   = ldg(send_count_ptr)              # 本 rank 已发送数
    slot    = count % inflight
    send_buf = buffer + (dst_local + 2)*inflight*bytes + slot*bytes   # 本地源
    recv_buf = buffer + (local_in_dst + 0)*inflight*bytes + slot*bytes # 对端目的（对称偏移）

    # 等待对端释放该槽（目标 count - inflight + 1 已被消费）
    check_signal(gin, release_sig_idx, count - inflight + 1)
    tma_copy(x -> send_buf)         # 各 SM 各搬一段，cooperative
    grid.sync()
    if sm_idx == 0:
        gin.put(recv_buf, send_buf, num_bytes, dst)   # RDMA 写远端
        gin.signal(dst, data_ready_sig_idx)           # 通知对端
        send_count_ptr += 1
```

```
pp_recv_impl(x, src):
    count   = ldg(recv_count_ptr)               # 本 rank 已接收数
    slot    = count % inflight
    recv_buf = buffer + (src_local + 0)*inflight*bytes + slot*bytes

    check_signal(gin, data_ready_sig_idx, count + 1)  # 等对端第 count+1 个到位
    tma_copy(recv_buf -> x)
    grid.sync()
    if sm_idx == 0:
        gin.signal(src, release_sig_idx)              # 释放该槽
        recv_count_ptr += 1
```

四个信号槽的含义（`kNumRanks` 是基址偏移）：`[kNumRanks, kNumRanks+2)` 是两个方向的「数据就绪」计数，`[kNumRanks+2, kNumRanks+4)` 是两个方向的「槽位释放」计数；`get_buffer_offset` 算出的 0/1 下标正好选中本方向那一对。

#### 4.3.3 源码精读

send 内核全貌：

[deep_ep/include/deep_ep/impls/pp_send_recv.cuh:114-165](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/pp_send_recv.cuh#L114-L165) —— `pp_send_impl`：先 `check_signal` 等待接收方释放槽位（target `send_count - num_max_inflight_tensors + 1`），再用 `tma_copy` 把 `x` 搬进 send buffer，grid 同步后由 SM 0 执行 `gin.put` + `gin.signal`。

recv 内核全貌：

[deep_ep/include/deep_ep/impls/pp_send_recv.cuh:167-212](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/pp_send_recv.cuh#L167-L212) —— `pp_recv_impl`：`check_signal` 等数据就绪（target `recv_count + 1`），`tma_copy` 把 recv buffer 搬到用户输出张量 `x`，再 grid 同步、SM 0 发「释放」信号。

信号等待的实现（用 `ld.acquire.sys` 强序读 + 超时回调）：

[deep_ep/include/deep_ep/impls/pp_send_recv.cuh:18-36](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/pp_send_recv.cuh#L18-L36) —— `check_signal`：从 Gin signals 表里取信号指针，`ptx::ld_acquire_sys` 读计数，`>= target` 即就绪；`is_last_check` 为真时调用 `timeout_print` 打印诊断。

TMA 双缓冲流水线搬运（`kNumStages = 2`）：

[deep_ep/include/deep_ep/impls/pp_send_recv.cuh:38-112](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/pp_send_recv.cuh#L38-L112) —— `tma_copy`：把张量按 `kNumTMAAlignBytes`（32B）切块，跨 SM 等分（`num_tma_blocks_per_sm`），用 2 级 mbarrier 流水线「装载-存储-预取」交替进行，是 send/recv 复用的纯本地高速拷贝器。

启动器把内核参数装箱：

[csrc/kernels/elastic/pp_send_recv.hpp:64-95](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/pp_send_recv.hpp#L64-L95) —— `launch_pp_send`：`LaunchArgs(num_sms, 32, num_smem_bytes, 1, true)` 表示 `grid=num_sms` 个 block、每 block 32 线程、cluster=1、**cooperative=true**（内核里要用 `this_grid().sync()`），最后 `PPSendRuntime::launch` 下发。

> 代码生成技巧：`generate_impl` 用 `reinterpret_cast<void*>(&pp_send_impl<kNumSMs, kNumRanks, kNumSmemBytes, kNumTimeoutCycles>)` 强制 nvcc 实例化这组特化（详见 [u4-l2](u4-l2-kernel-codegen.md)），四个模板参数都在编译期固定。

#### 4.3.4 代码实践

**实践目标**：在内核里定位「四区寻址」与「信号下标」，验证它们与 4.2 的公式一致。

**操作步骤**：
1. 打开 [pp_send_recv.cuh:125-139](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/pp_send_recv.cuh#L125-L139)，对照 4.2.2 的区域表，确认 send buffer 落在区域 2/3、recv buffer 落在区域 0/1。
2. 阅读 [pp_send_recv.cuh:142-164](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/pp_send_recv.cuh#L142-L164)：send 等的「释放」信号下标是 `kNumRanks + dst_idx_in_local + 2`，发的「数据就绪」信号下标是 `local_idx_in_dst + kNumRanks`。
3. 阅读 [pp_recv_impl](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/pp_send_recv.cuh#L167-L212)：recv 等的「数据就绪」下标 `src_idx_in_local + kNumRanks`、发的「释放」下标 `kNumRanks + local_idx_in_src + 2`，确认与 send 端成对匹配。

**预期结果**：对同一对 (S→R, R=next(S))，send 发出的「数据就绪」下标 == recv 等待的下标；recv 发出的「释放」下标 == send 等待的下标。可手算 `get_buffer_offset` 的两个返回值验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 send 在拷数据**之前**就要等「释放」信号，而不是拷完再等？

**答案**：因为 send 要先把数据写进 send buffer 的某个槽，而这个槽可能正被上一轮的 RDMA put 使用（数据还没被对端读走）。必须先确认对端已消费完该槽（释放信号到达 `count - inflight + 1`），才能安全覆盖，否则会写坏对端尚未读取的数据。

**练习 2**：`gin.put` 与 `tma_copy` 各自负责「跨节点」还是「节点内」？

**答案**：`tma_copy` 是**纯本地**拷贝（GPU HBM ↔ 共享内存 ↔ GPU HBM，借助对称内存窗口里的本地 send buffer）；`gin.put` 才是**跨 rank**的 RDMA 投递（把本地 send buffer 写到对端 recv buffer 的对称地址）。两者串行：先本地搬好，再一次性 RDMA 推走。

---

### 4.4 槽位轮转、双缓冲与超时保护

#### 4.4.1 概念说明

本模块把三件事串起来：(a) 取模轮转如何实现流水线深度；(b) 双缓冲 TMA 如何隐藏拷贝延迟；(c) GPU 侧超时如何把死锁变成可定位的崩溃。

**(a) 轮转即流水线**：`slot_idx = count % num_max_inflight_tensors`。若 `inflight = 4`，则连续 4 次 send 分别落到槽 0/1/2/3，第 5 次回到槽 0——只要第 5 次执行时第 1 次的数据已被对端消费（释放信号到达），就能无缝复用。这样发送方最多有 `inflight` 个张量「在途」未确认，RDMA 往返延迟被这 `inflight` 个槽并行吸收。

**(b) TMA 双缓冲**：`tma_copy` 用 `kNumStages = 2` 级 mbarrier 流水线——装载第 `i` 段的同时存储第 `i-2` 段、预取第 `i+2` 段，让 HBM↔smem 的往返延迟被两段数据重叠，是本地拷贝跑满带宽的关键。

**(c) GPU 超时**：PP 是协作内核、全程不回 CPU，一旦对端崩溃或路由出错，`check_signal` 会永久空转。DeepEP 用 `timeout_while` 在 GPU 侧计时，超时后先打印诊断信息、再 `ptx::trap()` 触发可定位的内核崩溃，而不是让 GPU 默默挂死。超时阈值在 host 侧由秒数 × 时钟频率换算成 GPU 周期数，并烘焙为模板常量 `kNumTimeoutCycles`。

#### 4.4.2 核心流程

超时换算（host 侧）：

\[
\text{num\_gpu\_timeout\_cycles} = \text{num\_gpu\_timeout\_secs} \times \text{clock\_rate(Hz)}
\]

默认 `num_gpu_timeout_secs = 100`（[elastic.py:L245](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L245)）。`clock_rate` 由 `DeviceRuntime` 从 `cudaDeviceProp` 读出（kHz → Hz），详见 [u4-l4](u4-l4-launch-framework.md)。

GPU 侧超时循环（伪代码）：

```
start = clock64()
while signal < target:
    if clock64() - start >= kNumTimeoutCycles:
        timeout_print()                          # 打印 "recv buffer is full/empty"
        再空等 1 秒让其他线程也打印
        ptx::trap()                              # 触发崩溃
```

#### 4.4.3 源码精读

超时换算与烘焙：

[csrc/elastic/buffer.hpp:120-123](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L120-L123) —— `num_gpu_timeout_cycles = num_gpu_timeout_secs * device_runtime->get_clock_rate()`，随后作为模板参数 `kNumTimeoutCycles` 编进 `pp_send_impl` / `pp_recv_impl`。

超时循环本体：

[deep_ep/include/deep_ep/common/comm.cuh:30-49](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/comm.cuh#L30-L49) —— `timeout_while`：用 `clock64()` 计时，超时后空等 `kNumOneSecCycles`（让诊断信息打印完整）再 `ptx::trap()`，把死锁暴露成内核错误而非静默挂起。

超时回调在 send/recv 里的具体文案：

[pp_send_recv.cuh:142-149](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/pp_send_recv.cuh#L142-L149) —— send 超时打印 `"DeepEP PP send timeout, recv buffer is full"`（对端来不及消费）；[pp_send_recv.cuh:193-200](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/pp_send_recv.cuh#L193-L200) —— recv 超时打印 `"DeepEP PP recv timeout, recv buffer is empty"`（对端没发或丢失），文案直接点明死锁方向。

SM 分配的默认值与可覆盖：

[csrc/elastic/buffer.hpp:351](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L351) —— `num_sms == 0 ? jit::device_runtime->get_num_sms() : num_sms`：Python 默认 `num_sms=0` 表示「用满全部 SM」，需要与计算重叠时可显式传更小的值，把部分 SM 让给计算流（与 dispatch 的 `prefer_overlap_with_compute` 同思路）。

#### 4.4.4 代码实践

**实践目标**：复现 `test_pp.py` 的压力测试，并观察「在途数」对带宽的影响。

**操作步骤**：
1. 在单机多卡上运行 `python tests/elastic/test_pp.py --num-processes 2 --num-max-inflight-tensors 4`。
2. 阅读测试里的 profiling 段：它会分别用 `num_concurrent ∈ {1,2,3}`、`hide_rdma_latency ∈ {True, False}` 组合跑 send/recv，并打印每次的 us 与 GB/s。

**需要观察的现象**：
- `hide_rdma_latency=True` 时，send 之后插入了 `_sleep`，recv 带宽分子变成 `2 * num_max_tensor_bytes`（因为发送与接收的量都被计入），体现 RDMA 延迟被掩盖。
- `num_concurrent` 增大时，多个 pp_send 连续下发，配合 `num_max_inflight_tensors` 形成在途流水线，单次延迟被摊薄。

**预期结果**：单机（纯 NVLink，无 RDMA）下 send 带宽接近 NVLink 峰值；多机才会触发真正的 RDMA 路径。具体数字**待本地验证**（取决于硬件与 `get_rdma_gbs` 探测结果）。

#### 4.4.5 小练习与答案

**练习 1**：把 `num_max_inflight_tensors` 从 4 调到 1，再跑 profiling，send/recv 带宽会怎样？

**答案**：会显著下降。`inflight=1` 时每次 send 必须等上一次的 recv 完全释放该唯一槽位（信号往返），RDMA 延迟无法被并行吸收，吞吐被「单张量往返延迟」卡住；带宽公式里分子不变但分母（耗时）变大，故 GB/s 下降。

**练习 2**：超时为什么用「GPU 周期数」而不是「wall-clock 秒」？

**答案**：PP 内核全程在 GPU 上跑、不回 CPU，无法直接读系统时钟；而 `clock64()` 是 GPU 提供的免费硬件计数器。host 把「期望秒数 × GPU 时钟频率」换算成周期数烘焙为编译期常量，内核只需比较 `clock64()` 差值即可，零额外开销。

## 5. 综合实践

参照 [tests/elastic/test_pp.py](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_pp.py)，实现一个**最小的两 rank 环形往返**，把本讲的「邻居约束、4 倍因子、轮转双缓冲、超时保护」全部串起来。

**任务**：rank 0 生成一个随机 BF16 张量 `t`，`pp_send` 给 rank 1；rank 1 `pp_recv` 收到后，再 `pp_send` 回 rank 0；rank 0 `pp_recv` 收回，验证 `result == t`，并测量往返延迟。

下面是示例代码（基于 test_pp.py 的初始化模式，**示例代码，未在本机运行过**）：

```python
# 示例代码：最小两 rank 环形往返
import math, torch, torch.distributed as dist
import deep_ep
from deep_ep.utils.envs import init_dist, dist_print

def run(local_rank, num_local_ranks, shape=(4096, 7168)):
    rank, num_ranks, group = init_dist(local_rank, num_local_ranks)
    assert num_ranks == 2  # 本例固定两 rank

    num_max_tensor_bytes = math.prod(shape) * 2          # BF16
    num_max_inflight_tensors = 4
    buffer = deep_ep.ElasticBuffer(
        group, explicitly_destroy=True, allow_hybrid_mode=False,
        num_bytes=deep_ep.ElasticBuffer.get_pp_buffer_size_hint(
            num_max_tensor_bytes, num_max_inflight_tensors))
    buffer.pp_set_config(num_max_tensor_bytes, num_max_inflight_tensors)

    next_rank = (rank + 1) % num_ranks   # rank0->1, rank1->0
    prev_rank = (rank + num_ranks - 1) % num_ranks

    if rank == 0:
        t = torch.randn(shape, dtype=torch.bfloat16, device='cuda')
        buffer.pp_send(t, next_rank)             # rank0 -> rank1
        result = torch.empty_like(t)
        buffer.pp_recv(result, prev_rank)        # rank1 -> rank0（回程）
        assert torch.equal(result, t), 'round-trip mismatch'
        dist_print(f'rank0 round-trip OK, bytes={num_max_tensor_bytes}')
    else:
        buf = torch.empty(shape, dtype=torch.bfloat16, device='cuda')
        buffer.pp_recv(buf, prev_rank)           # 收 rank0
        buffer.pp_send(buf, next_rank)           # 原样送回 rank0

    buffer.barrier(use_comm_stream=True, with_cpu_sync=True)
    buffer.destroy()
    dist.destroy_process_group()

if __name__ == '__main__':
    torch.multiprocessing.spawn(run, args=(2,), nprocs=2)
```

**关注点**：
1. **邻居约束**：两 rank 时 `next_rank == prev_rank == 1`（rank 0）或 `== 0`（rank 1），故 send/recv 目标都合法。若改成跳两级 `+2`，会触发 host 断言。
2. **缓冲区大小**：`get_pp_buffer_size_hint` 返回的是 `tensor_bytes × 4 × 4` 再 2 MB 对齐。
3. **正确性**：`pp_recv` 后 `buf` 内容应与 rank 0 发出的 `t` 逐位相等——这就是 test_pp.py 里 `assert torch.equal(result, tensor)` 的来源。
4. **延迟测量**：可仿照 test_pp.py 的 profiling 段，用 `bench_kineto` 包住 send+recv 往返，得到 us 级延迟。

运行环境与具体数字**待本地验证**（需要至少 2 张 SM90 GPU + NCCL）。

## 6. 本讲小结

- PP 是 DeepEP 独立于 dispatch/combine 之外的**点对点环形通信**原语，物理模型决定它只与 prev/next 邻居通信，该约束在 host 层用 `EP_HOST_ASSERT` 硬性强制。
- `pp_set_config` 切换到 PP 模式：先 `barrier` 刷新旧操作，记录张量上限（32 对齐）与最大在途数，并算出两个邻居 rank。
- 缓冲区需要 **4 倍因子** = 2 方向（prev/next）× 2 角色（send/recv），每个角色下再分 `num_max_inflight_tensors` 个槽位做流水线。
- 槽位用 `slot_idx = count % num_max_inflight_tensors` 取模轮转，配合 send/recv 两对 NCCL Gin 信号（数据就绪 + 槽位释放）形成无锁握手。
- 内核三段式：`tma_copy`（本地双缓冲搬运）→ grid 同步 → SM 0 执行 `gin.put`/`gin.signal`（跨 rank RDMA）。
- 死锁保护靠 GPU 侧 `timeout_while` + `ptx::trap()`：超时阈值由「秒 × 时钟频率」烘焙为编译期 `kNumTimeoutCycles`，超时打印方向性文案（buffer full/empty）后崩溃。

## 7. 下一步学习建议

- 阅读 [u7-l4（AGRS session 模型）](u7-l4-agrs.md)：AGRS 同样建立在 NCCL Gin 信号之上，但用 session + slot 模型做批量 all-gather，可与 PP 的「环形点对点」对比理解两类 collectives。
- 回看 [u7-l1（barrier）](u7-l1-barrier.md)：`pp_set_config` 内部调用的 `barrier(false, true)` 正是 u7-l1 讲的 GPU 级 release/acquire 同步，PP 复用它做模式切换的数据可见性保证。
- 深入 [u8-l1（PTX/TMA/mbarrier）](u8-l1-ptx-tma-mbarrier.md)：本讲里 `tma_copy` 的双缓冲流水线、`ld_acquire_sys` / `fence.proxy.async.shared::cta` 都封装在 `common/ptx.cuh`，读懂它们能帮你理解 PP 内核为何这样写内存序。
- 动手实验：在 `test_pp.py` 的 profiling 段基础上，画一张「`num_max_inflight_tensors` × `num_concurrent`」的带宽热力图，观察在途数对 RDMA 延迟掩盖的实际效果（多节点环境效果最明显）。

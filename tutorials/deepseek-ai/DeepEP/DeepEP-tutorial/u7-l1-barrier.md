# 跨 rank GPU Barrier 同步机制

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚为什么 DeepEP 需要一个**完全在 GPU 侧完成**的跨 rank barrier，而不能依赖 `ncclGroupEnd` 或 CPU 侧的 `cudaStreamSynchronize`。
- 读懂 `barrier_impl` 内核如何用**对称内存信号**（NVLink）与 **Gin signal**（RDMA）两类原语让一张 GPU 上所有 SM 与所有 rank 互相同步。
- 区分 `sequential=True/False` 两种模式在同步严格性、SM 占用与适用场景上的差异。
- 解释 barrier 在 `engram_write`、`pp_set_config`、`destroy` 等操作前后为何被用来**保证数据可见性**。

本讲属于实验性特性单元（U7）的第一篇，依赖你已经学过 U3（拓扑域、对称内存、WorkspaceLayout）。本讲不涉及 dispatch/combine 主链路，只聚焦「同步原语」本身。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**(1) 什么是 barrier。** Barrier 是一种集合同步：所有参与者都必须「到达」之后，任何一个参与者才能「离开」。最朴素的实现是 CPU 侧的 `dist.barrier()`——每个进程在 GPU 任务跑完后调一次 `cudaDeviceSynchronize`，再用 NCCL/锁交换一个「我到了」的消息。问题是：这要求 **CPU 介入**，而 DeepEP 的内核（dispatch/combine/engram/PP/AGRS）几乎全是**纯 GPU kernel 链**，中间没有 CPU 参与的空隙。如果每两步都要回到 CPU 做同步，通信-计算重叠就被彻底打断。

**(2) 为什么必须 GPU 级。** DeepEP 的内核之间靠 PDL（`cudaGridDependencySynchronize`）在 GPU 侧串接（见 U5/U6），全程不回 CPU。当一条 kernel 链里某一步需要「等所有 rank 都把上一阶段的数据写完」时，只能由 **GPU kernel 自己**去读/写一段所有 rank 共享的信号内存来完成等待。这就是 `barrier_impl` 存在的意义——它本身也是一个 kernel，跑在 GPU 上，用对称内存做信号。

**(3) 两类物理链路对应两类信号。** 回顾 U3：节点内 rank 通过 **NVLink** 共享对称寻址域（LSA），可以用 `get_sym_ptr` 直接拿到对端 GPU 显存地址，写一个 `int` 即可；节点间 rank 只能用 **RDMA**，走 NCCL Gin 的 `signal` 接口投递一个递增计数。所以 barrier 也分 scaleup（NVLink）与 scaleout（RDMA）两套实现，hybrid 模式下还要把它们组合起来。

下表给出本讲会用到的关键术语：

| 术语 | 含义 |
|---|---|
| sense-reversing barrier | 用「正/负方向交替累加」避免每轮重置信号的屏障算法 |
| Gin signal | NCCL Gin 后端提供的跨节点计数信号（`ncclGin_SignalInc`） |
| QP flush | 把某个 QP 上已下发但未抵达对端的 RDMA 写强制推完成的操作 |
| phase / sign | NVLink barrier 信号槽编号与累加方向，编码在 counter 的低 2 位 |
| grid sync | cooperative kernel 里全 grid 所有 block 的同步（`this_grid().sync()`） |

## 3. 本讲源码地图

本讲涉及的关键文件与各自职责：

| 文件 | 作用 |
|---|---|
| [csrc/elastic/buffer.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp) | host 层：`ElasticBuffer::barrier()` 封装，以及在 engram/pp/destroy 中的调用点 |
| [csrc/kernels/elastic/barrier.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/barrier.hpp) | JIT 启动器：`BarrierRuntime`（代码生成 + 启动）与 `launch_barrier`（决定 SM 数） |
| [deep_ep/include/deep_ep/impls/barrier.cuh](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/barrier.cuh) | 真正的 GPU kernel `barrier_impl`，分 sequential / parallel 两支 |
| [deep_ep/include/deep_ep/common/comm.cuh](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/comm.cuh) | 核心同步原语：`gpu_barrier`、NVLink barrier、Gin barrier、`timeout_while` |
| [deep_ep/include/deep_ep/common/layout.cuh](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/layout.cuh) | `WorkspaceLayout`：barrier 信号在 workspace 最前 16 字节的布局 |
| [deep_ep/include/deep_ep/common/ptx.cuh](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/ptx.cuh) | PTX 原语：`red_add_rel_sys`、`ld_acquire_sys` |
| [tests/elastic/test_barrier.py](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_barrier.py) | 基准测试：循环 1000 次 barrier 测延迟 |

调用方向（自上而下）：

```
Python buffer.barrier()
   └─ C++ ElasticBuffer::barrier()        [buffer.hpp]
        └─ launch_barrier(...)            [barrier.hpp]   ← 决定 num_sms
             └─ BarrierRuntime::generate + build + launch  [JIT]
                  └─ GPU kernel barrier_impl<...>          [barrier.cuh]
                       └─ comm::gpu_barrier(...)           [comm.cuh]
                            ├─ nvlink_barrier_wo_local_sync   (scaleup, NVLink)
                            └─ gin_barrier_wo_local_sync      (scaleout, RDMA)
```

## 4. 核心概念与源码讲解

### 4.1 GPU 级 barrier 的必要性与整体架构

#### 4.1.1 概念说明

设想一个典型场景：rank 0 通过 RDMA `put` 把一批 token 写到了 rank 1 的 buffer 里，紧接着 rank 1 要读这批 token 去做 GEMM。问题是 RDMA 写是异步的——`put` 返回时数据可能还在网线上。rank 1 怎么知道「rank 0 已经写完了」？

三种解法：

1. **CPU 同步**：rank 0 写完调 `cudaStreamSynchronize`，再用 NCCL all-reduce 通知 rank 1，rank 1 收到后再继续。代价：CPU 必须参与，kernel 链被打断。
2. **每条 RDMA 写都带一个完成信号**：精确但昂贵，每个 token 一个信号开销巨大。
3. **批量 barrier**：一批 RDMA 写全部下发完之后，统一做一次「全员到齐」的同步。DeepEP 选的就是这条。

barrier 的语义是 **release/acquire**：调用 barrier 之前本 rank 写的所有数据（无论 NVLink 还是 RDMA），在 barrier 返回之后对**所有 rank** 都可见。这正是 `engram_write`、`pp_set_config` 等操作需要它的原因。

#### 4.1.2 核心流程

一次 `buffer.barrier()` 的端到端流程：

1. host 决定在哪个流上执行（comm stream 或 compute stream），必要时让该流等一下当前 compute stream。
2. 可选地 `cudaDeviceSynchronize`（`with_cpu_sync=true` 时），确保本 GPU 所有先前工作入队完毕。
3. 下发 `barrier_impl` kernel（一个 cooperative kernel，1 或 2 个 SM）。
4. kernel 内部：先 flush 本 rank 所有未完成的写，再向所有 peer 投递「我到了」信号，再轮询等待收到所有 peer 的信号。
5. kernel 返回后，可选地再 `cudaDeviceSynchronize` 让 CPU 也等到 barrier 完成。
6. 若用了 comm stream，让 compute stream 等 comm stream（保证后续算子看到 barrier 之后的状态）。

#### 4.1.3 源码精读

host 层封装在 [csrc/elastic/buffer.hpp:181-208](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L181-L208)，三个参数的含义在注释里很清楚：

- `use_comm_stream`：在通信流还是当前计算流上执行。
- `with_cpu_sync`：前后是否各加一次 `cudaDeviceSynchronize`。
- `sequential`：是否串行做 scaleout 与 scaleup（默认 true）。

注意 `use_comm_stream=true` 时，前后各有一次 `stream_wait`：先让 comm stream 等 compute stream（拿到最新写），barrier 完成后再让 compute stream 等 comm stream（把 barrier 的可见性传递回计算流）。这两次 `stream_wait` 用 CUDA event 完成，开销远低于 `cudaDeviceSynchronize`。

超时周期在构造期就算好并烘焙成 kernel 的编译期常量（见 U4-l4 的 DeviceRuntime）。在 [csrc/elastic/buffer.hpp:120-123](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L120-L123)，秒数乘以 GPU 时钟频率得到周期数：

```cpp
this->num_gpu_timeout_cycles = static_cast<int64_t>(num_gpu_timeout_secs);
this->num_gpu_timeout_cycles *= jit::device_runtime->get_clock_rate();
```

启动器 `launch_barrier` 在 [csrc/kernels/elastic/barrier.hpp:56-84](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/barrier.hpp#L56-L84)，最关键的一行决定 SM 数量：

```cpp
// only the parallel hybrid kernel needs 2 SMs; the sequential mode does scaleout and
// scaleup one after another, so a single SM is sufficient.
const auto num_sms = (not sequential and num_scaleout_ranks > 1) ? 2 : 1;
```

即：**只有「并行 + 多节点」才用 2 个 SM**（一个做 scaleup、一个做 scaleout，并发）；其余情况 1 个 SM 足矣。`kNumThreads=512`、`cooperative=true`（构造 `LaunchArgs` 的最后一个 `true`）保证 kernel 内部可以用 `this_grid().sync()`。

注意构造函数末尾 [csrc/elastic/buffer.hpp:137-139](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L137-L139) 有一句重要注释：构造期**不调**本 barrier（因为此时各 rank 的 NCCL 窗口尚未全部注册就绪），而是依赖 Python 侧的 `dist.barrier()` 完成构造期同步；workspace 在 [csrc/elastic/buffer.hpp:130](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L130) 被 `cudaMemset` 清零，这是 sense-reversing 算法初值正确的必要条件。

#### 4.1.4 代码实践

**实践目标**：验证 host 层三个参数的实际效果。

**操作步骤**：

1. 打开 `tests/elastic/test_barrier.py`，找到 `test_barrier` 里的 `loop_barrier`，它调用的是无参 `buffer.barrier()`（即 `use_comm_stream=True, with_cpu_sync=False, sequential=True`）。
2. 临时改成 `buffer.barrier(use_comm_stream=True, with_cpu_sync=True, sequential=True)`，再跑一次。

**需要观察的现象**：`with_cpu_sync=True` 时，每次 barrier 前后都会有一次 `cudaDeviceSynchronize`，1000 次循环的延迟会**显著增大**（CPU↔GPU 往返本身就有几微秒开销）。

**预期结果**：`with_cpu_sync=False` 的延迟应为个位数~十几微秒量级；`with_cpu_sync=True` 会明显变大。若你观察到的差异不明显，可能是 CPU 同步被流水线掩盖，可适当增大 `num_tests`。

> 待本地验证：具体微秒数依赖你的硬件（NVLink 拓扑、GPU 型号），本讲不预设数值。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `barrier_impl` 必须是 cooperative kernel（`cooperative=true`）？

**参考答案**：因为单 SM 内的 `__syncthreads()` 只能同步一个 block，而 barrier 需要让「同一 GPU 上参与信号交换的所有 SM」彼此等待（见 4.2/4.3 中 `this_grid().sync()` 的使用）。cooperative launch 保证了 grid 级同步原语可用，且所有 block 能并发驻留。

**练习 2**：构造函数末尾为什么刻意不调用本 barrier？

**参考答案**：构造期各 rank 的 NCCL 对称内存窗口注册顺序未定，rank A 调 barrier 时 rank B 的窗口可能还没映射好，信号写入会落空。改用 Python 侧 `dist.barrier()`（走标准 NCCL 集合通信）保证所有 rank 都构造完毕后再继续。

---

### 4.2 NVLink 对称内存信号 barrier（节点内 scaleup）

#### 4.2.1 概念说明

节点内的 rank 共享 NVLink 对称寻址域（LSA，见 U3-l1）。这意味着 rank 0 可以通过 `ncclGetLsaPointer` 直接拿到 rank 1 显存里**同一偏移**的地址，读写它就像访问本机远端显存一样，无需 RDMA。于是 scaleup barrier 可以做得非常轻：每个 rank 在共享 workspace 里**预先约定**一段信号区，每个 rank 往「自己的那一格」写一个数，再去读「所有 rank 的格子」累加值是否凑齐。

为避免每轮 barrier 都要把信号重置回 0（这本身又是一次跨 rank 同步），这里用了经典的 **sense-reversing**（方向反转）技巧：奇偶轮交替地「加 1」与「减 1」，目标值也随之在 `N` 与 `0` 间切换，信号值在两轮之间自然回到可用状态。

#### 4.2.2 核心流程

信号区只有 16 字节，布局在 workspace 最前面（见 [layout.cuh:24](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/layout.cuh#L24) 的 `kNumBarrierSignalBytes=16`）。它被拆成三段：

| 偏移 | 内容 | 大小 |
|---|---|---|
| 0 | `counter`（uint64） | 8 字节 |
| 8 | signal 槽 phase 0（int） | 4 字节 |
| 12 | signal 槽 phase 1（int） | 4 字节 |

`counter` 的低 2 位编码当前轮的状态：

- bit 0 = `phase`：选哪个 signal 槽（0 或 1）。
- bit 1 = `sign`：本轮加 1 还是减 1。

每轮 barrier 的逻辑（伪代码）：

```
status  = counter & 3
phase   = status & 1
sign    = status >> 1
delta   = sign ? -1 : +1
target  = sign ?  0 : kNumRanks

# 每个 rank 往对端 phase 槽里写 delta
for dst in 0..kNumRanks:
    red_add_rel_sys(signal[phase] @ dst_rank, delta)

# 计数本轮完成
counter += 1

# 等本地 signal[phase] 累加到 target
while ld_acquire_sys(signal[phase]) != target:
    if 超时: trap
```

由于 `counter` 每轮 +1，4 轮一个循环：`+1/+1/-1/-1`，两个 signal 槽交替使用且值在每个槽上自然恢复。这就是 sense-reversing。

#### 4.2.3 源码精读

核心实现在 [deep_ep/include/deep_ep/common/comm.cuh:88-129](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/comm.cuh#L88-L129)，逐段看：

```cpp
const int status = static_cast<int>((*workspace.get_nvl_barrier_counter_ptr()) & 3);
const int phase = status & 1, sign = status >> 1;
```

读 counter 低 2 位解析出 phase 与 sign。`get_nvl_barrier_counter_ptr` 直接返回 workspace 起始地址（[layout.cuh:82-84](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/layout.cuh#L82-L84)），`get_nvl_barrier_signal_ptr(phase)` 返回偏移 `(2+phase)*sizeof(int)` 处（[layout.cuh:86-88](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/layout.cuh#L86-L88)），即 8 字节后第 0/1 个 int 槽。

```cpp
if (thread_idx < kNumRanks) {
    const auto dst_ptr =
        gin.get_sym_ptr<ncclTeamTagLsa>(workspace.get_nvl_barrier_signal_ptr(phase), thread_idx);
    ptx::red_add_rel_sys(dst_ptr, sign ? -1 : 1);
}
```

每个 thread 负责一个对端 rank：用 `get_sym_ptr<ncclTeamTagLsa>` 把本 rank 的 signal 地址翻译成对端 rank 的对称地址（[handle.cuh:64-92](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/handle.cuh#L64-L92) 内部调 `ncclGetLsaPointer`），再用 `red_add_rel_sys` 做一次 **release 语义**的原子加。`red_add_rel_sys` 对应 PTX `red.release.sys.global.add.s32`（[ptx.cuh:273-275](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/ptx.cuh#L273-L275)），`.sys` scope 保证跨 GPU 可见、`.release` 保证此前的 NVLink 写在对端读到信号前全部落地。

```cpp
if (thread_idx == 0)
    atomicAdd(workspace.get_nvl_barrier_counter_ptr(), 1);
```

thread 0 把 counter +1，推进到下一轮（这同时也为下一次 barrier 设定了 phase/sign）。

```cpp
const auto target = sign ? 0 : kNumRanks;
timeout_while<kNumTimeoutCycles>(thread_idx == 0, [=](const bool& is_last_check) {
    const auto signal = ptx::ld_acquire_sys<int>(workspace.get_nvl_barrier_signal_ptr(phase));
    if (signal == target) return true;
    ...
});
```

只有 thread 0 轮询本地 signal 槽，用 **acquire 语义**读（`ld.acquire.sys`，[ptx.cuh:289-302](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/ptx.cuh#L289-L302)），保证读到 target 后、后续读到的对端数据都是 barrier 之后的状态。`timeout_while` 在 [comm.cuh:30-49](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/comm.cuh#L30-L49)：超时前打印一行诊断信息，再等 1 秒（`kNumOneSecCycles=2e9`，模拟 2GHz，[comm.cuh:13](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/comm.cuh#L13)）让所有线程打印完，最后 `trap()` 卡死内核——这是避免一个 rank 卡住时其他 rank 无限等待、把死锁暴露成可定位的崩溃。

数学上，counter 的演化满足模 4 循环：

\[
\text{counter} \bmod 4 \in \{0,1,2,3\} \Rightarrow
\begin{cases}
0: \text{phase}=0,\ \text{sign}=0,\ \text{加}+1,\ \text{target}=N \\
1: \text{phase}=1,\ \text{sign}=0,\ \text{加}+1,\ \text{target}=N \\
2: \text{phase}=0,\ \text{sign}=1,\ \text{加}-1,\ \text{target}=0 \\
3: \text{phase}=1,\ \text{sign}=1,\ \text{加}-1,\ \text{target}=0
\end{cases}
\]

#### 4.2.4 代码实践

**实践目标**：理解 signal 区的 16 字节布局与 sense-reversing 的初值要求。

**操作步骤**（源码阅读型实践）：

1. 在 [layout.cuh:43-80](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/layout.cuh#L43-L80) 的 `get_num_bytes()` 中确认 barrier 信号区只占最前面的 `kNumBarrierSignalBytes=16` 字节，其余是 notify reduction 等区域。
2. 对照 [buffer.hpp:130](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L130) 的 `cudaMemset(workspace, 0, ...)`，回答：若 workspace 没有清零，第一轮 barrier（counter=0, sign=0, target=N）会发生什么？

**需要观察的现象**：纯阅读，无需运行。

**预期结果**：若 signal 槽初值非 0，第一轮每个 rank 加 +1 后，signal 不会精确等于 `N`，barrier 会一直轮询直到超时 trap。这解释了为何构造期必须清零 workspace。

#### 4.2.5 小练习与答案

**练习 1**：为什么用两个 signal 槽（phase 0/1）而不是一个？

**参考答案**：用两个槽可以错开「正在被写的轮」与「正在被读的轮」。虽然 sense-reversing 本身已保证一轮内读写同一槽也能正确（读在写之后），但 phase 槽让相邻两轮使用不同地址，减少同一缓存行的竞争抖动，也使逻辑更清晰。

**练习 2**：`red.release.sys` 与 `ld.acquire.sys` 的 release/acquire 配对在这里起什么作用？

**参考答案**：release 保证「本 rank 在 barrier 前的所有 NVLink 写」先于信号原子加被对端观察到；acquire 保证「读到 target 后，本 rank 后续读到的对端数据」都是 barrier 之后的状态。这对构成了跨 rank 的 release/acquire 内存序，正是 barrier「之前写的都可见」语义的实现。

---

### 4.3 RDMA Gin 信号 barrier 与 QP flush（节点间 scaleout）

#### 4.3.1 概念说明

跨节点 rank 不共享对称寻址域，无法用 `get_sym_ptr` 直接寻址，只能走 RDMA。DeepEP 用 NCCL Gin 后端的 **signal** 接口实现 scaleout barrier：每个 rank 通过 `gin.signal` 向每个对端 rank 投递一个「递增」信号（`ncclGin_SignalInc{rank_idx}`），对端累加收到的信号数，凑齐 `kNumRanks` 个即表示全员到齐。

但 RDMA 有个坑：先前下发的 RDMA `put` 写可能还滞留在网卡队列里没真正抵达对端。如果直接发 barrier 信号，对端可能在数据还没落地时就看到信号、误以为可以读了。所以 scaleout barrier 在发信号前必须先 **flush 所有 QP**——把本 rank 持有的所有 QP 上未完成的写强制推完成。

#### 4.3.2 核心流程

`gin_barrier_wo_local_sync`（[comm.cuh:131-181](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/comm.cuh#L131-L181)）两步走：

1. **Flush 阶段**（仅 `kFlushStores=true` 时）：所有 warp 合作遍历全部 QP，对每个 QP 调 `ncclGin(...).flush(ncclCoopWarp())`，然后 grid 同步。这一步把 release 语义铺到所有队列。
2. **Signal + Wait 阶段**（仅 SM 0 执行）：用 QP 0 向每个对端 rank 发 `SignalInc{rank_idx}`，然后轮询本 rank 的 signal shadow counter 是否累加到本轮 target。

第二步是一个**计数信号**屏障：每个 rank 发出的信号值是自己的 `rank_idx`（一个递增量），接收端有一个 64 位 shadow 计数器 `*shadow_ptr`，每轮 `++(*shadow_ptr)` 得到本轮期望值 target，等收到的累加信号 `>= target` 即可。shadow counter 的存在让多轮 barrier 不必重置信号。

#### 4.3.3 源码精读

flush 阶段：

```cpp
if constexpr (kFlushStores) {
    for (int i = global_warp_idx; i < num_qps; i += kNumSMs * kNumWarps) {
        ncclGin(nccl_dev_comm, i, NCCL_GIN_RESOURCE_SHARING_CTA).flush(ncclCoopWarp());
    }
    (gridDim.x > 1) ? cooperative_groups::this_grid().sync() : __syncthreads();
}
```

`num_qps = kNumQPs == kFlushAllAllocatedQPs ? nccl_dev_comm.ginContextCount : kNumQPs`——当上层传 `kFlushAllAllocatedQPs=-1`（见 [comm.cuh:28](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/comm.cuh#L28)）时，flush 全部分配的 QP。barrier 用的正是这个值（见 4.4 中 `comm::kFlushAllAllocatedQPs`）。`flush` 后必须 grid sync，因为后续只有 SM 0 发信号，要确保所有 SM 的 flush 都完成。

signal + wait 阶段（仅 `sm_idx == 0`）：

```cpp
const ncclGin gin(nccl_dev_comm, 0, NCCL_GIN_RESOURCE_SHARING_CTA);
for (int i = thread_idx; i < kNumRanks; i += kNumThreads)
    gin.signal(team, i, ncclGin_SignalInc{static_cast<ncclGinSignal_t>(rank_idx)});

for (int i = thread_idx; i < kNumRanks; i += kNumThreads) {
    const auto signal_idx = static_cast<ncclGinSignal_t>(i);
    const auto shadow_ptr = gin.getSignalShadowPtr(signal_idx);
    const auto target = ++(*shadow_ptr);
    ...
    timeout_while<...>([=](const bool& is_last_check) {
        const auto signal = ptx::ld_acquire_sys<uint64_t>(signal_ptr);
        if (signal >= target) return true;
        ...
    });
}
```

`team` 的选择见 [comm.cuh:154-155](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/comm.cuh#L154-L155)：scaleout 用 `ncclTeamRail`（按 rail/节点分组），scaleup（非 NVLink 退化时）用 `ncclTeamWorld`。`rank_idx` 在 [comm.cuh:140](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/comm.cuh#L140) 按 team 类型取 scaleout 或 scaleup 索引。

注意代码里有一条 `TODO(NCCL)` 注释（[comm.cuh:160](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/comm.cuh#L160)）：作者希望未来 NCCL 官方的 wait signal API 加入超时检查后直接复用，目前是自建 `timeout_while` 轮询。`signal_ptr` 的取法较底层（[comm.cuh:166-167](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/comm.cuh#L166-L167)），直接从 `gdaki->signals_table.buffer` 取，绕过封装以降低延迟。

#### 4.3.4 代码实践

**实践目标**：理解 flush 与 signal 的先后关系，以及「不 flush 会怎样」。

**操作步骤**（源码阅读 + 推理型实践）：

1. 在 `gpu_barrier`（[comm.cuh:208-264](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/comm.cuh#L208-L264)）里找到入口处的 TMA store 等待：

   ```cpp
   if constexpr (kFlushStores) {
       ptx::tma_store_commit();
       ptx::tma_store_wait();
       __syncwarp();
   }
   ```

   它先确保本 SM 的 TMA 异步 store 都提交完，再进入 flush。
2. 推理：如果某个 dispatch 内核在 barrier 前**没有** flush（即上层调用时 `kFlushStores=false`），barrier 还能保证可见性吗？

**需要观察的现象**：纯推理。

**预期结果**：不能。`kFlushStores=false` 跳过 flush，信号会先于 RDMA 写抵达，对端可能在数据落地前读到旧值。所以 `gpu_barrier` 在 [comm.cuh:228-230](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/comm.cuh#L228-L230) 有断言 `EP_STATIC_ASSERT(not kFlushStores, "No data to be flushed")`——当你选 `kSyncAtStart=false`（不在开头同步）时，强制要求 `kFlushStores=false`，即「不打算 flush 就别声称要 flush」。这把 release 语义的约束做进了编译期。

#### 4.3.5 小练习与答案

**练习 1**：为什么 flush 阶段要遍历**所有** QP，而不是只 flush QP 0（发信号用的那个）？

**参考答案**：因为先前的数据写可能分散在任意一个 QP 上（dispatch/combine 按 channel 分配 QP）。只 flush QP 0 无法保证其他 QP 上的写已抵达。barrier 是全局语义，必须把所有可能的 QP 都推完成。

**练习 2**：shadow counter（`++(*shadow_ptr)`）解决了什么问题？

**参考答案**：它让多轮 barrier 复用同一组 signal 槽而不必清零。每轮 target = 上一轮 target + 1，收到的累加信号单调递增，只要 `signal >= target` 即视为本轮到齐。避免了「重置信号」这个本身又需要同步的操作。

---

### 4.4 hybrid 两级 barrier：sequential 与 parallel 两种模式

#### 4.4.1 概念说明

多节点 hybrid 模式下，barrier 必须同时覆盖 scaleout（跨节点 RDMA）与 scaleup（节点内 NVLink）两个域。DeepEP 提供两种组合方式：

- **sequential（串行）**：先做完 scaleout barrier，再做 scaleup barrier。只要 1 个 SM，严格保证「跨节点的写先于节点内可见」。
- **parallel（并行）**：用 2 个 SM，一个做 scaleup、一个做 scaleout，同时进行。延迟更低（取两者较慢的一个而非求和），但要求 SM 数 ≥ 2。

注意 `scaleout` barrier 的 flush 还肩负一个额外职责：把 scaleout 阶段发出的 RDMA 请求**冲刷干净**，让节点内的 scaleup barrier 能看到这些写。

#### 4.4.2 核心流程

`barrier_impl` 在 [barrier.cuh:11-40](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/barrier.cuh#L11-L40) 按 `kSequential` 模板参数分两支：

```
sequential = true:
    if num_scaleout_ranks > 1:
        gpu_barrier(do_scaleout=true,  do_scaleup=false, flush=true,  syncAtStart=true,  syncAtEnd=false)
    gpu_barrier(do_scaleout=false, do_scaleup=true,  flush=true,  syncAtStart=true,  syncAtEnd=true, kFlushStores=true)
    # 第二次的 kFlushStores=true 会冲刷第一次 scaleout 发出的 RDMA 请求

sequential = false (parallel):
    gpu_barrier(do_scaleout=true, do_scaleup=true)   # 单次调用，内部并行
```

注意 sequential 分支里**两次** `gpu_barrier` 调用的模板参数不同（看 [barrier.cuh:24-39](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/barrier.cuh#L24-L39)）：第一次只做 scaleout 且 `syncAtEnd=false`（不收尾），第二次只做 scaleup 且 `syncAtEnd=true`（收尾）、同时 `kFlushStores=true` 把 scaleout 的 RDMA 请求冲刷掉。注释 [barrier.cuh:31](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/barrier.cuh#L31) 明确写了这点。

#### 4.4.3 源码精读

`gpu_barrier` 的总调度在 [comm.cuh:213-264](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/comm.cuh#L213-L264)。入口先做 TMA store 等待与（可选）grid sync，然后按 `do_scaleout`/`do_scaleup` 路由：

```cpp
do_scaleout &= kNumScaleoutRanks > 1;
do_scaleup  &= kNumScaleupRanks > 1;
if (do_scaleup and do_scaleout) {
    // 并行：SM 0 做 scaleup，其余 SM 做 scaleout
    EP_DEVICE_ASSERT(kNumSMs >= 2 and "At least 2 SMs for a hybrid barrier");
    if (sm_idx == 0) {
        scaleup_barrier_wo_local_sync<...>(...);
        if constexpr (kFlushStores) cooperative_groups::this_grid().sync();
    } else {
        scaleout_barrier_wo_local_sync<..., kNumSMs - 1, ...>(..., sm_idx - 1, ...);
    }
} else if (do_scaleup)  { scaleup_barrier_wo_local_sync<...>(...); }
else if (do_scaleout)   { scaleout_barrier_wo_local_sync<...>(...); }
```

并行分支里 SM 0 单独做 scaleup，其余 SM（注意传入的是 `kNumSMs - 1` 与 `sm_idx - 1`）合作做 scaleout 的 flush。末尾 [comm.cuh:262-263](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/comm.cuh#L262-L263) 的 `kSyncAtEnd` 控制是否 grid sync 收尾。

`scaleup_barrier_wo_local_sync`（[comm.cuh:185-196](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/comm.cuh#L185-L196)）按 `kIsScaleupNVLink` 分流：是 NVLink 域就走 4.2 的对称内存版，否则退化成 `ncclTeamWorld` 的 Gin 版。`scaleout_barrier_wo_local_sync`（[comm.cuh:200-206](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/comm.cuh#L200-L206)）固定走 `ncclTeamTagRail` 的 Gin 版。

单机（`num_scaleout_ranks == 1`）时，`do_scaleout` 恒为 false，只剩 scaleup 分支，与 sequential/parallel 无关——这也是为什么单机测试看不出两种模式的 SM 差异（见综合实践）。

JIT 代码生成侧，[barrier.hpp:32-45](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/barrier.hpp#L32-L45) 的 `generate_impl` 把 `sequential`、`num_scaleout_ranks`、`num_scaleup_ranks`、SM 数、线程数都填进 `barrier_impl<>` 模板尖括号，由 nvcc 实例化出对应特化（回顾 U4-l2 的模板实例化技巧）。

#### 4.4.4 代码实践

**实践目标**：对比 sequential 与 parallel 在延迟上的差异（或确认单机下无差异）。

**操作步骤**：

1. 给 `tests/elastic/test_barrier.py` 加一个命令行参数：

   ```python
   parser.add_argument('--sequential', type=int, default=1, help='1=sequential, 0=parallel')
   ```
2. 在 `loop_barrier` 里传入：

   ```python
   def loop_barrier(num_tests=1000):
       for i in range(num_tests):
           buffer.barrier(sequential=bool(args.sequential))
   ```
3. 分别用 `--sequential 1` 和 `--sequential 0` 各跑一次。

**需要观察的现象**：

- **单机 8 卡**（`num_scaleout_ranks == 1`）：两次延迟应**几乎相同**。因为此时 `do_scaleout` 被屏蔽，两种模式都只跑 scaleup，且 [barrier.hpp:70](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/barrier.hpp#L70) 的 `num_sms` 都是 1。
- **多节点**（`num_scaleout_ranks > 1`）：parallel（`--sequential 0`）应更快，因为它并发执行两级 barrier；SM 数为 2。

**预期结果**：单机下两者延迟量级一致，证明差异只在多节点 hybrid 下才出现。若你只有单机环境，本实践的意义在于**验证「单机下两种模式等价」**这一结论，而非看到性能差。

> 待本地验证：多节点的具体加速比依赖 RDMA/NVLink 带宽比，本讲不预设数值。

#### 4.4.5 小练习与答案

**练习 1**：为什么 parallel 模式要求 `kNumSMs >= 2`？单 SM 能并行做两级 barrier 吗？

**参考答案**：parallel 把 scaleup 与 scaleout 分给不同 SM 并发执行，单 SM 无法同时驻留两套逻辑（且 scaleout 的 flush 需要全 grid 合作）。代码用 `EP_DEVICE_ASSERT(kNumSMs >= 2)` 在设备侧强制这一点。

**练习 2**：sequential 模式第二次 `gpu_barrier`（scaleup）为何要带 `kFlushStores=true`？

**参考答案**：第一次 scaleout barrier 发出的 RDMA 写请求需要被冲刷到对端节点，节点内的 scaleup barrier 才能在节点内看到这些数据。第二次带 `kFlushStores=true` 正是为此冲刷 scaleout 残留的请求（见 [barrier.cuh:31](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/barrier.cuh#L31) 注释）。

---

### 4.5 host 层封装与 barrier 的可见性保证场景

#### 4.5.1 概念说明

理解了 GPU 侧实现后，最后看 host 层如何使用 barrier。DeepEP 内部**很多操作都把 barrier 当作「围栏」**——在切换数据流向、刷新配置、销毁资源前后调一次，确保所有 rank 看到一致的状态。这些调用统一走 [csrc/elastic/buffer.hpp:181-208](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L181-L208) 的 `ElasticBuffer::barrier()`，区别只在三个参数的组合。

#### 4.5.2 核心流程

三个典型场景：

| 场景 | 调用 | 参数 | 为什么需要 |
|---|---|---|---|
| 销毁缓冲区 | `destroy()` | `barrier(true, true)` | 退出前确保所有 rank 的未完成工作都落地，再释放 NCCL 资源 |
| Engram 写存储 | `engram_write` 前后 | `barrier(false, true)` ×2 | 前一次确保上一次 fetch 已结束；后一次确保本 rank 写入的 CPU 段存储对所有 rank 可见 |
| 刷新 PP 配置 | `pp_set_config` | `barrier(false, true)` | 切换 tensor 尺寸/inflight 数前，冲刷所有先前 PP send/recv |

它们的共同模式：`use_comm_stream=false`（在当前 compute stream 上，因为数据写在 compute stream）、`with_cpu_sync=true`（host 需要在 barrier 后读到一致状态，比如 `engram_write` 后 host 要记录 entry 数）。

#### 4.5.3 源码精读

`destroy()` 在 [csrc/elastic/buffer.hpp:152-166](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L152-L166)：

```cpp
void destroy() {
    EP_HOST_ASSERT(not destroyed);
    barrier(true, true);            // 收尾所有 GPU 工作
    CUDA_RUNTIME_CHECK(cudaFreeHost(host_workspace));
    nccl_context->finalize();
    destroyed = true;
}
```

`use_comm_stream=true` 是为了在 comm stream 上做最终同步（comm stream 承载了所有通信）。`with_cpu_sync=true` 保证 `cudaFreeHost` 执行时所有 rank 都已不再访问 host workspace。

`engram_write` 在 [csrc/elastic/buffer.hpp:210-240](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L210-L240) 首尾各一次 barrier：

```cpp
void engram_write(const torch::Tensor& storage, ...) {
    barrier(false, true);   // 确保之前的 engram_fetch 已完成
    ... cudaMemcpyAsync(... storage → CPU 段 ...) ...
    barrier(false, true);   // 确保写入对其他 rank 的 engram_fetch 可见
}
```

`pp_set_config` 在 [csrc/elastic/buffer.hpp:327-337](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L327-L337)：

```cpp
void pp_set_config(...) {
    barrier(false, true);   // 冲刷上一组 PP 操作
    ...
    this->num_max_pp_tensor_bytes = math::align<int64_t>(num_max_tensor_bytes, 32);
    this->num_max_pp_inflight_tensors = num_max_inflight_tensors;
}
```

PP 用环形缓冲区传递 tensor，配置切换（尺寸/inflight 变化）必须等所有在飞的 tensor 都落地，否则新旧配置混用会越界。

Python 层暴露在 [deep_ep/buffers/elastic.py:497](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L497)，签名 `barrier(use_comm_stream=True, with_cpu_sync=False, sequential=True)`；C++ 绑定在 [csrc/elastic/buffer.hpp:1353](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L1353) 的 `.def("barrier", &ElasticBuffer::barrier)`。注意 Python 默认 `with_cpu_sync=False`，而内部调用都显式传 `true`——因为用户主动调 barrier 时通常不需要 CPU 阻塞，而内部场景需要。

#### 4.5.4 代码实践

**实践目标**：体会 barrier 作为「围栏」在数据流切换中的作用。

**操作步骤**（阅读 + 推理型实践）：

1. 阅读 `engram_fetch` 的实现（[csrc/elastic/buffer.hpp:242-325](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L242-L325)），注意它返回的 callable 里调了 `launch_engram_fetch_wait`。
2. 思考：为什么 `engram_write` 开头的 `barrier(false, true)` 必须带 `with_cpu_sync=true`？如果改成 `false` 会怎样？

**需要观察的现象**：纯推理。

**预期结果**：`engram_write` 会修改 host 成员变量（`num_engram_entries` 等，[buffer.hpp:221](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L221)），并紧接着发起 `cudaMemcpyAsync`。`with_cpu_sync=true` 确保前一轮 fetch 的 wait 已在 host 侧确认完成、资源可安全复用；若改 false，host 可能在 fetch 还在用时就把存储覆盖掉，产生数据竞争。

#### 4.5.5 小练习与答案

**练习 1**：用户在 Python 里直接调 `buffer.barrier()`（全默认参数）和内部 `engram_write` 调的 `barrier(false, true)` 有什么区别？

**参考答案**：用户调用走 comm stream 且不阻塞 CPU（`use_comm_stream=True, with_cpu_sync=False`），适合插在用户 kernel 链里做轻量围栏；内部调用走 compute stream 且阻塞 CPU（`use_comm_stream=False, with_cpu_sync=true`），因为内部需要在 host 读到一致状态后立即修改成员变量、发起新的 memcpy。

**练习 2**：`destroy()` 里为什么用 `use_comm_stream=true` 而其他内部场景用 `false`？

**参考答案**：`destroy` 是全局收尾，要保证 comm stream（承载所有跨 rank 通信）上的工作全部完成后再释放 NCCL 资源；而 engram/pp 的数据写发生在 compute stream 上，需要与 compute stream 对齐，故用 `false` 在 compute stream 上做围栏。

---

## 5. 综合实践

**任务**：把本讲的三条主线——NVLink 信号 barrier、RDMA Gin signal barrier、sequential/parallel 模式——串起来，做一次「带诊断的 barrier 基准」。

**操作步骤**：

1. 复制 `tests/elastic/test_barrier.py` 为本地实验脚本（不要改原测试文件）。
2. 在 `test_barrier` 里，先打印 buffer 的逻辑域规模与 QP 数（已有），确认你的环境是单机还是多节点。
3. 加 `--sequential` 参数（见 4.4.4），分别测 sequential/parallel 两种模式的延迟。
4. 进一步加 `--with-cpu-sync` 参数，把 `loop_barrier` 改成 `buffer.barrier(with_cpu_sync=bool(args.with_cpu_sync), sequential=bool(args.sequential))`，测四种组合：
   - `(sequential=True,  with_cpu_sync=False)`
   - `(sequential=True,  with_cpu_sync=True)`
   - `(sequential=False, with_cpu_sync=False)`
   - `(sequential=False, with_cpu_sync=True)`
5. 把四种延迟填入下表：

   | sequential | with_cpu_sync | barrier 延迟 (us) |
   |---|---|---|
   | True | False | |
   | True | True | |
   | False | False | |
   | False | True | |

**需要观察与解释的现象**：

- `with_cpu_sync` 对延迟的影响应远大于 `sequential`（因为 CPU 同步引入了 host↔device 往返）。
- 单机环境下 `sequential` 的影响应可忽略（理由见 4.4.4）。
- 结合源码解释：为什么 `with_cpu_sync=True` 的两次 `cudaDeviceSynchronize`（[buffer.hpp:189 与 203](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L188-L203)）会成为延迟主导项。

**预期结果**：能画出一张表，并用本讲学到的「SM 数决定」「CPU 同步开销」「单机无 scaleout」三点解释每行的数字。多节点环境还应看到 parallel < sequential。

> 待本地验证：本实践的核心是「能解释数字为何如此」，而非追求特定绝对值。

## 6. 本讲小结

- DeepEP 需要一个**纯 GPU 侧**的跨 rank barrier，因为它的 kernel 链不回 CPU；`barrier_impl` 自身就是一个 cooperative kernel，用对称内存信号完成同步。
- **NVLink scaleup barrier** 采用 sense-reversing 算法：counter 低 2 位编码 phase/sign，每轮交替加减 1，两个 signal 槽交替使用，配合 `red.release.sys`/`ld.acquire.sys` 的 release/acquire 内存序保证可见性。
- **RDMA scaleout barrier** 用 NCCL Gin 的 `SignalInc` 计数信号 + shadow counter 复用槽位；发信号前必须 **flush 所有 QP**，把先前 RDMA 写推完成，否则对端会读到未落地数据。
- **sequential** 模式串行做 scaleout→scaleup（1 SM，scaleup 阶段顺带 flush scaleout 残留）；**parallel** 模式用 2 SM 并发执行两级 barrier，仅多节点 hybrid 下才有性能收益与 SM 差异。
- host 层 `ElasticBuffer::barrier(use_comm_stream, with_cpu_sync, sequential)` 是统一入口，被 `destroy`、`engram_write`、`pp_set_config` 等当作「围栏」使用，保证数据流切换与资源释放时所有 rank 状态一致。
- 超时机制用 `timeout_while` + `trap()` 把死锁暴露成可定位的崩溃，避免无限等待。

## 7. 下一步学习建议

- **U7-l2（Engram）**：barrier 在 engram_write 前后的围栏作用会在那里完整展开，你会看到 CPU 段存储 + RDMA get 的两段式异步设计如何依赖本讲的可见性保证。
- **U7-l3（PP send/recv）**：pp_set_config 前的 barrier 是环形传递正确的前提，下一讲会讲清 prev/next 相邻 rank 的通信约束与双缓冲。
- **U8-l1（PTX 原语）**：本讲用到的 `red_add_rel_sys`、`ld_acquire_sys`、`tma_store_commit/wait` 都定义在 `common/ptx.cuh`，U8 会系统讲解 TMA、mbarrier、fence.proxy 等底层原语。
- 若想深入内存序理论，建议阅读 CUDA C++ Programming Guide 的「Memory Consistency Model」一节，理解 `.sys`/`.gpu` scope 与 release/acquire 语义的形式化定义。

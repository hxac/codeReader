# PTX 内联函数：TMA 加载与栅栏

## 1. 本讲目标

本讲承接 u6-l1（内核入口：SM90 FP8 GEMM 1D1D）和 u6-l2（MMA 抽象），继续下钻到 DeepGEMM 设备 kernel 最底层的「积木」——`ptx/` 与 `comm/` 目录下的内联 PTX（Parallel Thread eXecution，NVIDIA GPU 的底层指令集）封装。

读完本讲，你应当能够：

1. 说清楚 **TMA**（Tensor Memory Access，张量内存访问）异步拷贝指令在 DeepGEMM 里是如何被 C++ 函数封装的，以及它与 **mbarrier**（内存屏障）如何配合完成「生产者-消费者」同步。
2. 区分 **cluster 同步**（cluster_sync，cluster 内多 CTA 同步）与 **grid 同步**（grid_sync，整个 grid 跨 SM 同步）两种粒度，并能判断 grid_sync 为何**只被 Mega MoE 使用、普通 GEMM 不用**。
3. 看懂 `ld_st.cuh` 中 `relaxed` / `acquire` / `release` 三种内存序语义，以及 `gpu` 与 `sys` 两种同步范围（scope）的区别，理解原子指令如何跨 SM、跨 GPU 协作。

---

## 2. 前置知识

### 2.1 PTX 是什么

GPU 代码通常用 CUDA C++ 写，但真正在硬件上执行的是一种叫 **PTX** 的指令集（一种虚拟 ISA）。高级语法（如 `__shared__`、`atomicAdd`）最终都会被编译成 PTX 指令。

有时高级语法不够用——比如需要精确控制某条 TMA 指令的缓存提示、或需要一条 CUTLASS/CuTe 没有封装的 mbarrier 指令——这时就用 `asm volatile("...")` **内联 PTX** 来手写指令。`ptx/*.cuh` 就是这样一组薄封装。

### 2.2 关键术语速查

| 术语 | 含义 |
|------|------|
| **CTA**（Cooperative Thread Array） | 一个 thread block，对应一个「block」 |
| **cluster** | Hopper（SM90）起新增的层级：多个 CTA 组成一个 cluster，可共享「分布式共享内存」（DSMEM） |
| **grid** | 一次 kernel launch 的所有 CTA 的集合 |
| **TMA** | 硬件张量内存访问单元，可异步、批量地把全局内存（gmem）瓦片搬进共享内存（smem），或反向搬出 |
| **mbarrier** | 内存屏障，一种异步同步原语，靠「事务计数（transaction count）」判断一批异步拷贝是否完成 |
| **proxy（代理）** | GPU 上不同执行域：`generic`（常规）、`tensormap`（张量描述符）、`shared::cta`（共享内存）等，跨 proxy 数据可见性需 `fence` |

### 2.3 内存序与同步范围

本讲会反复出现两组修饰词，先建立直觉：

- **内存序**（memory ordering）：
  - `relaxed`：不做任何排序保证，最快但最弱。
  - `release`：本次写入之前，本线程的所有读写都不会被重排到它之后（相当于「我写完广播出去了」）。
  - `acquire`：本次读取之后，本线程的所有读写都不会被重排到它之前（相当于「我收到了广播」）。
  - `release`/`acquire` 成对使用，能建立跨线程的「先写后读」可见性。
- **同步范围**（scope）：
  - `gpu`：可见性覆盖整个 GPU（一个设备内的所有 SM）。
  - `sys`：可见性覆盖整个系统（跨多个 GPU，如经 NVLink 互联的对卡）。

> 直觉：单卡内多 SM 协作用 `gpu` 范围就够；多卡（Mega MoE 的 NVLink 通信）必须用 `sys` 范围。

### 2.4 承接前讲

u6-l1 已建立「1 个 TMA warp-group + 若干 math warp-group」的双线程分工、双缓冲 mbarrier、软件流水线（`full_barriers` / `empty_barriers`）的心智模型。本讲回答：**这些 mbarrier 和 TMA 调用背后，到底是哪几条 PTX 指令在支撑？** u6-l2 讲了 MMA（WGMMA/UMMA）指令本身，本讲补充它外围的「数据搬运与同步」指令。

---

## 3. 本讲源码地图

本讲涉及的关键文件都在设备侧（`deep_gemm/include/deep_gemm/` 下）：

| 文件 | 作用 |
|------|------|
| [`ptx/tma.cuh`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/ptx/tma.cuh) | TMA 异步拷贝、mbarrier 原语、tensor-map 描述符 fence/replace 的 PTX 封装 |
| [`comm/barrier.cuh`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/comm/barrier.cuh) | cluster 同步、grid 同步、NVLink 跨卡屏障 |
| [`ptx/ld_st.cuh`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/ptx/ld_st.cuh) | 共享/全局内存加载存储、原子操作（relaxed/acquire/release）、ldmatrix/stmatrix |

辅助阅读（用到这些原语的真实 kernel）：

| 文件 | 作用 |
|------|------|
| [`impls/sm90_fp8_gemm_1d1d.cuh`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh) | 普通 GEMM 内核：演示 TMA→mbarrier→WGMMA 的同步序列 |
| [`impls/sm100_fp8_fp4_mega_moe.cuh`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_mega_moe.cuh) | Mega MoE 内核：演示 grid_sync 与 1D TMA 原语 |

---

## 4. 核心概念与源码讲解

本讲拆为三个最小模块：**TMA PTX 封装**、**cluster/grid 同步**、**加载与原子语义**。

### 4.1 TMA PTX 封装

#### 4.1.1 概念说明

TMA 是 Hopper/Blackwell 上专门做「批量异步内存搬运」的硬件单元。相比传统逐字节 `cp.async`，TMA 一次能把一个完整的「瓦片（tile）」从全局内存搬进共享内存，CPU/GPU 线程无需逐字节搬运，只需发一条指令然后去做别的事（比如算上一批数据），搬完后靠 **mbarrier** 通知。

DeepGEMM 在 `ptx/tma.cuh` 里封装了两类东西：

1. **tensor-map（张量描述符）相关指令**：TMA 搬运需要一个 128 字节的描述符（`TmaDescriptor`，u4-l2 详述）来告诉硬件「从哪块全局内存、按什么形状/步长搬」。这个描述符本身在共享内存里时，可以用 PTX 指令就地修改它的全局地址或步长，从而让同一个描述符服务变长的 K 轴分组（K-grouped GEMM）。
2. **TMA 搬运 + mbarrier 同步指令**：发出搬运、登记「期待多少字节到达」、等待搬运完成。

> ⚠️ 重要区分：普通 GEMM（`sm90_fp8_gemm_1d1d`）走的是 CuTe 的 **2D 瓦片 TMA**（`tma::copy` / `cp.async.bulk.tensor.2d`），它由 CuTe 的 `ClusterTransactionBarrier` 封装 `arrive_and_expect_tx` / `wait`。而 `ptx/tma.cuh` 里这套**裸 PTX 封装**（`tma_load_1d` + 裸 `mbarrier_*`）是 **1D 批量 TMA**，主要服务 **Mega MoE** 的 dispatch pull / combine 阶段。两者底层是同一族 PTX 指令，只是瓦片维度（2D vs 1D）和封装层次不同。

#### 4.1.2 核心流程

一次「TMA 加载到完成」的标准三步：

```
① 生产者发搬运：tma_load_1d(目标smem, 源gmem, mbarrier, 字节数)
   └─ PTX: cp.async.bulk ... mbarrier::complete_tx::bytes
② 生产者登记期待到达的字节数：mbarrier_arrive_and_set_tx(mbarrier, 字节数)
   └─ PTX: mbarrier.arrive.expect_tx
③ 消费者等待搬运完成：mbarrier_wait_and_flip_phase(mbarrier, phase)
   └─ PTX: mbarrier.try_wait.parity （自旋直到奇偶相位翻转）
```

这里的关键机制是 **phase（相位）翻转子**。mbarrier 内部维护一个事务计数：`expect_tx` 设定「期待 N 字节」，每次 TMA 完成会扣减计数；当计数归零，屏障的**奇偶相位**翻转。消费者用 `try_wait.parity` 等待「当前相位」翻转——这是一种无需重置计数器的、天然支持软件流水线的同步方式（每次循环阶段交替使用 stage 0 / stage 1 的缓冲，相位也交替 0/1）。

`tensor_map_replace_global_addr_in_smem` 等指令则用于：当 K 轴分组切换到下一个 expert 时，不重新构造整个 128B 描述符，而是在共享内存里**就地改写**它的全局基地址和内维步长，再 `fence` 让改动对硬件可见。

#### 4.1.3 源码精读

**① 1D 批量 TMA 加载**（发出搬运 + 登记完成通知）：

[deep_gemm/include/deep_gemm/ptx/tma.cuh:63-77](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/ptx/tma.cuh#L63-L77) —— `tma_load_1d` 把一段全局内存按字节数批量搬进共享内存，完成事件写进指定 mbarrier，并带 L2 缓存提示 `EVICT_FIRST`（这些数据马上用完就丢，别污染 L2）：

```cpp
asm volatile(
    "cp.async.bulk.shared::cluster.global.mbarrier::complete_tx::bytes.L2::cache_hint [%0], [%1], %2, [%3], %4;\n" ::
    "r"(...dst_ptr...), "l"(src_ptr), "r"(num_bytes),
    "r"(...mbarrier_ptr...), "l"(hint) : "memory");
```

**② 登记期待到达的字节数**：

[deep_gemm/include/deep_gemm/ptx/tma.cuh:41-45](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/ptx/tma.cuh#L41-L45) —— `mbarrier_arrive_and_set_tx` 告诉 mbarrier「本批期待到达 `num_bytes` 字节，我已抵达」，这是 TMA 完成判断的依据：

```cpp
asm volatile("mbarrier.arrive.expect_tx.shared::cta.b64 _, [%1], %0; \n\t" ::
             "r"(num_bytes), "r"(...mbarrier_ptr...));
```

**③ 消费者等待搬运完成（相位翻转）**：

[deep_gemm/include/deep_gemm/ptx/tma.cuh:47-61](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/ptx/tma.cuh#L47-L61) —— `mbarrier_wait_and_flip_phase` 用一个自旋循环反复 `try_wait.parity`，直到相位翻转（`@P1 bra DONE` 跳出），末尾 `phase ^= 1` 翻转本地相位变量以备下一轮：

```cpp
asm volatile(
    "{ .reg .pred P1; \n"
    "LAB_WAIT: \n"
    "mbarrier.try_wait.parity.shared::cta.b64 P1, [%0], %1, %2; \n"
    "@P1 bra DONE; \n"
    "bra     LAB_WAIT; \n"
    "DONE: }" :: ... );
phase ^= 1;
```

**④ 就地改写 tensor-map 描述符**：

[deep_gemm/include/deep_gemm/ptx/tma.cuh:18-22](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/ptx/tma.cuh#L18-L22) —— `tensor_map_replace_global_addr_in_smem` 把描述符的全局基地址换成下一个 expert 的指针（服务 K-grouped）；配套的 `tensor_map_release_gpu` / `tensor_map_acquire_gpu`（[tma.cuh:9-16](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/ptx/tma.cuh#L9-L16)）是跨 proxy 的 fence，保证「在共享内存里改的描述符」对「TMA 硬件代理」可见。

**真实使用**：[sm100_fp8_fp4_mega_moe.cuh:539](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_mega_moe.cuh#L539) 与 [sm100_fp8_fp4_mega_moe.cuh:550-555](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_mega_moe.cuh#L550-L555) 完整复现了上面 ①②③ 三步，用于 Mega MoE 的 dispatch pull 阶段。

#### 4.1.4 代码实践

**实践目标**：在普通 SM90 GEMM 里，确认 CuTe 的 2D TMA 封装与裸 PTX 1D TMA 封装之间的对应关系。

**操作步骤**：

1. 打开 [sm90_fp8_gemm_1d1d.cuh](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh)，定位 TMA warp-group 分支（`warp_idx >= kNumMathThreads / 32`）。
2. 找到 [sm90_fp8_gemm_1d1d.cuh:213](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh#L213)（`empty_barriers[stage_idx]->wait(phase ^ 1)`）与 [sm90_fp8_gemm_1d1d.cuh:221-225](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh#L221-L225)（4 次 `tma::copy` + `full_barrier.arrive_and_expect_tx(...)`）。
3. 对照 `ptx/tma.cuh` 的 `mbarrier_arrive_and_set_tx`（[tma.cuh:41-45](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/ptx/tma.cuh#L41-L45)），体会 CuTe 的 `arrive_and_expect_tx` 就是它的上层包装。

**需要观察的现象**：math 侧 [sm90_fp8_gemm_1d1d.cuh:271](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh#L271) 的 `full_barriers[stage_idx]->wait(phase)` 与 TMA 侧的 `arrive_and_expect_tx` 正好成对——一个登记、一个等待，构成 stage 级握手。

**预期结果**：你能画出一对 `full_barrier` 上「arrive_and_expect_tx（TMA 侧）↔ wait（math 侧）」、一对 `empty_barrier` 上「arrive（math 侧）↔ wait（TMA 侧）」的双向握手。此实践为源码阅读型，**无需运行 GPU**。

#### 4.1.5 小练习与答案

**练习 1**：`tma_load_1d` 的 PTX 里有个修饰 `.mbarrier::complete_tx::bytes`，它和 `mbarrier_arrive_and_set_tx` 是什么关系？

> **答**：`complete_tx::bytes` 表示「本搬运完成的字节数会被自动累加进 mbarrier 的事务计数」；`mbarrier_arrive_and_set_tx` 则登记「期待到达的字节数」。两者配合：TMA 完成扣减计数、`arrive_and_expect_tx` 设定目标，计数归零时相位翻转，等待方就此被唤醒。

**练习 2**：`mbarrier_wait_and_flip_phase` 末尾为什么要有 `phase ^= 1`？

> **答**：mbarrier 用奇偶相位标记「这一轮是否完成」。本轮等待的是相位 A，完成后硬件翻转到 B；消费者要把本地记录的相位也翻转到 B，这样下一轮就能正确等待「B 翻回 A」。这避免了重置计数器，天然适配 `iter_idx % kNumStages` 的多 stage 软件流水线。

---

### 4.2 cluster 与 grid 同步

#### 4.2.1 概念说明

DeepGEMM 需要在不同粒度上做同步：

- **cluster 同步**：SM90 引入的 cluster 概念，让 2~16 个 CTA 共享分布式共享内存（DSMEM），可做 TMA multicast（多播）。当 multicast 开启时，同一个 cluster 内的多个 CTA 必须在「init barrier」「开始加载」等节点对齐，这时用 `cluster_sync_with_relaxed_arrive`。
- **grid 同步**：把**整个 grid 的所有 SM** 同步到同一处。这是「持久化 kernel + 多阶段协作」的关键能力，普通 GEMM 用不到（每块独立），但 **Mega MoE** 必须用——它在一个 kernel 里依次跑 dispatch、Linear1、SwiGLU、Linear2、combine 多个阶段，阶段之间所有 SM 必须汇合。
- **NVLink 屏障**：更进一步，跨**多张 GPU**（多 rank）同步，配合对称内存实现 NVLink 通信。

#### 4.2.2 核心流程

**cluster 同步**极简：

```
cluster_sync_with_relaxed_arrive():
  cute::cluster_arrive_relaxed()   // 所有 CTA 抵达（弱排序）
  cute::cluster_wait()             // 等所有 CTA 到齐
```

**grid 同步**则用「全局内存里的一个原子计数器」模拟 grid 级屏障，思路来自 `cooperative_groups::this_grid().sync()`：

```
grid_sync(workspace, sm_idx, thread_idx, sync_scope):
  sync_scope()                       // ① CTA 内线程对齐，只让 thread 0 参与原子
  if thread_idx == 0:
      old = atomic_add_rel(count_ptr, sm_idx==0 ? (TAG - (N-1)) : 1)
      do:
          new = ld_acq(count_ptr)    // ② 自旋读，acquire 语义
          超时则 printf + 断言失败
      while ((new ^ old) & TAG) == 0 // ③ 等 TAG 位翻转 ⇒ 全员到齐
  sync_scope()                       // ④ CTA 内线程再次对齐
```

核心巧思：**最后一个到达的 SM**。前 `N-1` 个 SM 各 `+1`，SM 0 反向「加一个很大的 `TAG` 再减 `N-1`」，正好把计数器的**最高位（TAG = 0x80000000）**翻起来。其他 SM 用 `ld_acq` 自旋读，一旦看到最高位被翻起 `(new ^ old) & TAG != 0`，说明最后一个 SM 到了，全员就绪。

> 关键点：`atomic_add_rel`（release）写、`ld_acq`（acquire）读，这对 release/acquire + `gpu` scope 保证了「计数器值」与「数据」的跨 SM 可见性。NVLink 版同理，只是 scope 换成 `sys`。

#### 4.2.3 源码精读

**① cluster 同步**：

[deep_gemm/include/deep_gemm/comm/barrier.cuh:14-19](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/comm/barrier.cuh#L14-L19) —— 注释点明它比 `cute::cluster_sync` 略快，但内存序保证更弱（用 `arrive.relaxed`）：

```cpp
CUTLASS_DEVICE void cluster_sync_with_relaxed_arrive() {
    cute::cluster_arrive_relaxed();
    cute::cluster_wait();
}
```

**② grid 同步**：

[deep_gemm/include/deep_gemm/comm/barrier.cuh:21-44](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/comm/barrier.cuh#L21-L44) —— 注意 `atomic_add_rel`（release）登记 + `ld_acq`（acquire）自旋的组合，以及 60 秒（`kNumTimeoutCycles = 60e9`，2GHz）超时保护：

```cpp
static constexpr uint32_t kFinishSumTag = 0x80000000u;
...
const auto old_value = ptx::atomic_add_rel(
    count_ptr, sm_idx == 0 ? (kFinishSumTag - (kNumSMs - 1)) : 1);
do {
    new_value = ptx::ld_acq(count_ptr);
    if (clock64() - start_clock >= kNumTimeoutCycles) { ... DG_DEVICE_ASSERT(false ...); }
} while (((new_value ^ old_value) & kFinishSumTag) == 0);
```

超时常量定义在 [barrier.cuh:12](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/comm/barrier.cuh#L12)：`60ll * 2000000000ll`（2 GHz 下 60 秒）。

**③ 谁在用 grid_sync？** 全仓搜索 `grid_sync<` 只有 3 处命中：`barrier.cuh` 内部 2 处（被 `nvlink_barrier` 复用），外加 **2 个 Mega MoE 内核**：

- [sm100_fp8_fp4_mega_moe.cuh:382](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_mega_moe.cuh#L382) —— Mega MoE 在 dispatch 阶段结束后调用 `grid_sync<kNumSMs, kDispatchGridSyncIndex>(...)`。
- `sm100_bf16_mega_moe.cuh:339`（BF16 版 Mega MoE，同理）。

**④ 谁在用 cluster_sync？** 普通 GEMM 才用：[sm90_fp8_gemm_1d1d.cuh:148](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh#L148) —— `kNumTMAMulticast > 1` 时走 cluster 同步，否则退化为 `__syncthreads()`：

```cpp
(kNumTMAMulticast > 1) ? comm::cluster_sync_with_relaxed_arrive() : __syncthreads();
```

> ✅ 本讲核心结论：**普通 GEMM 不使用 grid_sync**。普通 GEMM 每个 block 独立调度（u6-l4 的持久化调度器），block 之间无数据依赖，只需 cluster 内（multicast 时）或 CTA 内同步。grid_sync 是 Mega MoE 这种「单 kernel 多阶段、阶段间全局汇合」场景的专属工具。

**⑤ NVLink 跨卡屏障**：

[deep_gemm/include/deep_gemm/comm/barrier.cuh:46-89](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/comm/barrier.cuh#L46-L89) —— 在 grid_sync 基础上，由 SM 0 经对称内存 `sym_buffer.map(...)` 向远端 rank 发 `red_add_rel_sys`（reduce，sys 范围，见 4.3），再用 `ld_acq_sys` 轮询本地 signal 计数，实现跨卡汇合。

#### 4.2.4 代码实践

**实践目标**：确认「普通 GEMM 不用 grid_sync」这一结论，并理解 multicast 与 cluster 同步的关系。

**操作步骤**：

1. 在仓库内执行只读搜索 `grid_sync<`，记录命中文件（应只有 `barrier.cuh` + 两个 `*_mega_moe.cuh`）。
2. 在 `impls/` 内搜索 `cluster_sync`，记录命中文件（应包含 `sm90_fp8_gemm_1d1d.cuh`、`sm90_fp8_gemm_1d2d.cuh`、`sm90_bf16_gemm.cuh`、`sm100_bf16_gemm.cuh`、`sm100_fp8_fp4_gemm_1d1d.cuh` 等普通 GEMM）。
3. 阅读 [sm90_fp8_gemm_1d1d.cuh:138-148](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh#L138-L148)：看 barrier 初始化 `empty_barriers[i]->init(kNumTMAMulticast * kNumMathThreads / 32)` 如何依赖 multicast 数。

**需要观察的现象**：`kNumTMAMulticast` 决定了同步原语的选择——`==1` 用 `__syncthreads()`（纯 CTA 内），`>1` 用 cluster 同步（cluster 内多 CTA）。

**预期结果**：你能解释「multicast 需要多个 CTA 协作把数据多播进各自 DSMEM，所以必须 cluster 同步；非 multicast 单 CTA 自己加载自己消费，`__syncthreads` 足矣」。此实践为源码阅读型。

#### 4.2.5 小练习与答案

**练习 1**：grid_sync 里，为什么只有 SM 0 用 `atomic_add_rel(count_ptr, kFinishSumTag - (kNumSMs - 1))`，而其它 SM 用 `+1`？

> **答**：这是一个「最后一个到达者翻标志位」的技巧。设 N 个 SM：N-1 个各 `+1` 累计 `N-1`；SM 0 加 `TAG - (N-1)`（`TAG=0x80000000`）。总和 `TAG - (N-1) + (N-1) = TAG`，恰好把最高位翻起，且不污染低 31 位之外。任何 SM 用 `ld_acq` 读到「最高位相对自己登记时的 old_value 翻转」即知全员到齐。

**练习 2**：为什么普通 GEMM 不需要 grid_sync？

> **答**：普通 GEMM 的调度器（u6-l4）让每个 block 独立领取若干输出瓦片计算，block 之间无写后读依赖，互不通信；唯一需要协作的是 multicast 时 cluster 内多个 CTA，用 cluster_sync 即可。grid_sync 的开销（全局原子 + 自旋）只对「单 kernel 多阶段、阶段间全局数据依赖」的 Mega MoE 才划算。

---

### 4.3 加载与原子语义

#### 4.3.1 概念说明

`ptx/ld_st.cuh` 是一组「各种加载/存储/原子指令」的封装，分三大类：

1. **共享内存读写**：`ld_shared` / `st_shared`。普通 GEMM 里，math 线程从共享内存读缩放因子（SF）就靠它。
2. **带内存序的全局内存访问**：`ld_acq`（acquire 读）、`st_relaxed_sys`（relaxed 写）、`ld_acq_sys` / `ld_acq_gpu` 等，用于 grid_sync / NVLink 等跨线程、跨卡同步场景。
3. **原子/规约操作**：`atomic_add*`（返回旧值）、`red_add*` / `red_or_*`（reduce，不返回值，更轻）、以及矩阵加载存储 `ldmatrix` / `stmatrix`。

理解这些封装的关键是 **内存序后缀** 与 **scope 后缀**：

| 后缀 | 含义 | 典型用途 |
|------|------|----------|
| `relaxed` | 无排序保证 | 单线程内的普通读写 |
| `acquire` | 读后建屏障 | grid_sync 自旋读计数器 |
| `release`（`rel`） | 写前建屏障 | grid_sync 登记到达 |
| `.gpu` scope | 全 GPU 可见 | 单卡多 SM 协作（grid_sync） |
| `.sys` scope | 全系统可见 | 跨卡（NVLink）协作 |

还有一个重要区分：**`atom`（atomic）vs `red`（reduce）**。
- `atom.global.add`：原子加并**返回旧值**（需要返回值时用）。
- `red.global.add`：原子加但**不返回值**（只管加，更省指令，不需要返回值时优先用）。

#### 4.3.2 核心流程

以 grid_sync 为例，它正好把 4.3 的「release 写 + acquire 读」用全：

```
登记到达（写）：atomic_add_rel(count_ptr, delta)
   └─ PTX: atom.release.gpu.global.add.u32  （release + gpu scope，返回 old_value）
轮询全员到齐（读）：ld_acq(count_ptr)
   └─ PTX: ld.acquire.gpu.global.b32        （acquire + gpu scope，自旋读）
```

以 NVLink 屏障为例（跨卡）：

```
向远端 rank 发信号（写）：red_add_rel_sys(remote_ptr, ±1)
   └─ PTX: red.release.sys.global.add.s32   （reduce + release + sys scope，不返回）
轮询本卡 signal（读）：ld_acq_sys(signal_ptr)
   └─ PTX: ld.acquire.sys.global.s32        （acquire + sys scope，自旋读）
```

> 规律：**单卡**协作一律 `gpu` scope，**跨卡**协作一律 `sys` scope；需要旧值用 `atom`，不需要用 `red`。

#### 4.3.3 源码精读

**① 共享内存读（普通 GEMM 读 SF）**：

[deep_gemm/include/deep_gemm/ptx/ld_st.cuh:101-105](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/ptx/ld_st.cuh#L101-L105) —— `ld.shared.u32` 把共享内存地址（经 `__cvta_generic_to_shared` 转成 smem 地址）读进寄存器；另有 `float` / `float2` / `float4` / `uint4` 重载（[ld_st.cuh:107-123](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/ptx/ld_st.cuh#L107-L123), [125-129](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/ptx/ld_st.cuh#L125-L129)）。

**真实使用**：[sm90_fp8_gemm_1d1d.cuh:275-276](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh#L275-L276) —— math 线程用 `ptx::ld_shared(smem_sfa[stage_idx] + r_0)` 读 A 的缩放因子（必须在 `warpgroup_arrive` 之前读完，注释强调避免下一 block 污染）。

**② acquire 语义读（grid_sync 轮询）**：

[deep_gemm/include/deep_gemm/ptx/ld_st.cuh:169-173](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/ptx/ld_st.cuh#L169-L173) —— `ld.acquire.gpu.global.b32`，gpu scope 的 acquire 读：

```cpp
CUTLASS_DEVICE uint32_t ld_acq(const uint32_t* ptr) {
    uint32_t ret;
    asm volatile("ld.acquire.gpu.global.b32 %0, [%1];" : "=r"(ret) : "l"(ptr));
    return ret;
}
```

跨卡的 `ld_acq_sys` 有多个重载：[ld_st.cuh:175-179](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/ptx/ld_st.cuh#L175-L179)（b64）、[ld_st.cuh:228-238](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/ptx/ld_st.cuh#L228-L238)（s32/u32），全部用 `.sys` scope。

**③ release 语义原子加（grid_sync 登记）**：

[deep_gemm/include/deep_gemm/ptx/ld_st.cuh:198-202](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/ptx/ld_st.cuh#L198-L202) —— `atomic_add_rel` 是 release + gpu scope 的原子加，返回旧值：

```cpp
CUTLASS_DEVICE uint32_t atomic_add_rel(const uint32_t* ptr, const uint32_t& value) {
    uint32_t ret;
    asm volatile("atom.release.gpu.global.add.u32 %0, [%1], %2;" : "=r"(ret) : "l"(ptr), "r"(value));
    return ret;
}
```

**④ reduce（不返回值）原子加**：

[deep_gemm/include/deep_gemm/ptx/ld_st.cuh:204-210](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/ptx/ld_st.cuh#L204-L210) —— `red_add` 是 `red.gpu.global.add`，**没有**返回值，比 `atom` 轻。跨卡的 `red_add_rel_sys` 在 [ld_st.cuh:224-226](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/ptx/ld_st.cuh#L224-L226)（`red.release.sys.global.add.s32`），被 NVLink 屏障 [barrier.cuh:68](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/comm/barrier.cuh#L68) 用来向远端 rank 发信号。

**⑤ 条件加载（谓词）**：

[deep_gemm/include/deep_gemm/ptx/ld_st.cuh:247-259](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/ptx/ld_st.cuh#L247-L259) —— `ld_gez_pred`：仅当 `pred >= 0` 才真正发起一次 256B 宽度的全局加载，否则返回 0。用 PTX 谓词（`setp.ge.s32 p, ...; @p ld ...`）实现，常用于带掩码的越界保护。

**⑥ 矩阵加载存储 ldmatrix/stmatrix**：

[deep_gemm/include/deep_gemm/ptx/ld_st.cuh:24-40](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/ptx/ld_st.cuh#L24-L40) —— `SM90_U32x2_LDSM_N` 等封装 `ldmatrix.sync.aligned`，专门把共享内存里的数据按张量核友好的布局加载进寄存器；SM100 还有 `b8` 位宽的 stmatrix（[ld_st.cuh:78-98](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/ptx/ld_st.cuh#L78-L98)）。

#### 4.3.4 代码实践

**实践目标**：把 grid_sync / NVLink 屏障里用到的内存序与 scope 对应到具体 PTX 后缀，验证「单卡 gpu / 跨卡 sys」「需旧值 atom / 不需 red」两条规律。

**操作步骤**：

1. 在 [barrier.cuh:28-41](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/comm/barrier.cuh#L28-L41)（grid_sync）找到 `ptx::atomic_add_rel` 与 `ptx::ld_acq` 两个调用点。
2. 在 `ld_st.cuh` 找到它们的实现，记录 PTX 后缀：`atom.release.gpu.global.add.u32` 与 `ld.acquire.gpu.global.b32`。
3. 在 [barrier.cuh:60-83](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/comm/barrier.cuh#L60-L83)（NVLink 屏障）找到 `ptx::red_add_rel_sys` 与 `ptx::ld_acq_sys`，记录后缀 `red.release.sys.global.add.s32` 与 `ld.acquire.sys.global.*`。
4. 列表对比两处的 scope 与是否返回值。

**需要观察的现象**：grid_sync 全是 `gpu` scope，NVLink 全是 `sys` scope；grid_sync 用 `atom`（要 old_value 算 tag），NVLink 发信号用 `red`（只管加不关心旧值）。

**预期结果**：得到一张对照表，验证两条规律成立。此实践为源码阅读型，**无需运行 GPU**。

#### 4.3.5 小练习与答案

**练习 1**：`atomic_add_rel` 和 `red_add_rel` 都能做「加」，区别在哪？grid_sync 为什么用前者？

> **答**：`atom.*.add` 返回**加之前的旧值**，`red.*.add`（reduce）**不返回值**，后者指令更轻。grid_sync 需要 `old_value` 来计算「自己登记时的 tag」，以便后续用 `(new ^ old) & kFinishSumTag` 判断是否翻转，所以必须用 `atom`。NVLink 发信号只把远端计数器加一下、不关心旧值，故用 `red` 省开销。

**练习 2**：`ld_acq` 和 `ld_acq_sys` 的区别是什么？分别在哪个屏障里用？

> **答**：`ld_acq` 是 `ld.acquire.gpu`（gpu scope，单卡内可见），用于 grid_sync 在单卡轮询计数器；`ld_acq_sys` 是 `ld.acquire.sys`（sys scope，跨卡可见），用于 NVLink 屏障轮询跨卡 signal。scope 越大开销越大，按需选最小够用的。

**练习 3**：[sm90_fp8_gemm_1d1d.cuh:274](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh#L274) 注释说「all shared memory read must be prior to `warpgroup_arrive`」，为什么？

> **答**：`warpgroup_arrive`（u6-l2 的 WGMMA fence）之后会开始新的 MMA 流水，若 SF 读取被重排到下一 stage，可能读到被下一个 block 覆盖后的共享内存内容，导致缩放因子错配。用 `ld_shared` 在 `arrive` 之前把 SF 读进寄存器，是保证正确性的顺序约束。

---

## 5. 综合实践

**综合实践目标**：把三个最小模块串起来，完整跟踪一次普通 SM90 FP8 GEMM 的「TMA 加载 → mbarrier 等待 → WGMMA 计算」同步序列，并给出 grid_sync 适用边界的判断。

**任务步骤**：

1. **画同步序列时序图**。阅读 [sm90_fp8_gemm_1d1d.cuh:209-301](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh#L209-L301)，分别画出 TMA warp-group 与 math warp-group 在一个 `k_block` 循环里的指令序列，重点标注以下握手点（用 stage_idx / phase 标注）：
   - TMA 侧：`empty_barriers[stage_idx]->wait(phase ^ 1)`（[L213](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh#L213)）→ `tma::copy` 四连发（[L221-224](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh#L221-L224)）→ `full_barrier.arrive_and_expect_tx(...)`（[L225](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh#L225)）。
   - math 侧：`full_barriers[stage_idx]->wait(phase)`（[L271](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh#L271)）→ `ptx::ld_shared` 读 SF（[L275-281](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh#L275-L281)）→ `ptx::warpgroup_arrive/commit_batch/wait<0>`（[L287-298](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh#L287-L298)）→ `empty_barrier_arrive(stage_idx)`（[L301](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh#L301)）。
2. **标注指令归属**。在时序图旁，标注每一步对应 `ptx/*.cuh` 的哪个函数（如 `arrive_and_expect_tx` ↔ [tma.cuh:41-45](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/ptx/tma.cuh#L41-L45) 的 `mbarrier_arrive_and_set_tx`、`ld_shared` ↔ [ld_st.cuh:101-105](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/ptx/ld_st.cuh#L101-L105)）。
3. **判断 grid_sync 边界**。回答：这条同步链里**有没有** grid_sync？为什么？再对比 [sm100_fp8_fp4_mega_moe.cuh:382](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_mega_moe.cuh#L382) 的 `grid_sync` 调用，说明二者差异。

**预期结果**：

- 一张清晰的 TMA/math 双线程时序图，体现 `full_barriers`（TMA→math，数据就绪）与 `empty_barriers`（math→TMA，缓冲可复用）的双向 mbarrier 握手。
- 结论：**普通 GEMM 的同步链只用 mbarrier（数据级）+ cluster_sync/`__syncthreads`（multicast 时），完全不用 grid_sync**。grid_sync 是 Mega MoE 在阶段切换处的全局汇合点，开销显著，只在确有跨 SM 数据依赖时才用。

> ⚠️ 本综合实践为**源码阅读型**，全程基于只读阅读与画图，**不要求运行 GPU**。若想进一步验证，可在具备 Hopper/Blackwell 的机器上用 `DG_JIT_DUMP_SASS=1`（见 u10-l4）编译该 kernel，观察 `cp.async.bulk` 与 `mbarrier.try_wait.parity` 的 SASS/PTX 命中。

---

## 6. 本讲小结

- **TMA PTX 封装**：`ptx/tma.cuh` 把 TMA 异步搬运（`cp.async.bulk`）与 mbarrier 同步原语（`arrive.expect_tx` / `try_wait.parity`）封装成 C++ 函数；其 1D 批量版（`tma_load_1d` + 裸 `mbarrier_*`）服务 Mega MoE，2D 瓦片版（CuTe 的 `tma::copy` + `ClusterTransactionBarrier`）服务普通 GEMM，底层是同一族 PTX。
- **mbarrier 相位机制**：`arrive_and_expect_tx` 登记期待字节数、TMA 完成扣减计数、计数归零翻转奇偶相位，消费者 `try_wait.parity` 自旋等待，天然适配多 stage 软件流水线。
- **cluster vs grid 同步**：cluster_sync（cluster 内多 CTA，multicast 用）属普通 GEMM；grid_sync（全 grid 跨 SM）用「最后一个到达者翻 tag 位」的全局原子技巧，**只被 Mega MoE 使用**；NVLink 屏障进一步跨卡（`sys` scope）。
- **加载与原子语义**：`ld_st.cuh` 用 `relaxed`/`acquire`/`release` 三种内存序 × `gpu`/`sys` 两种 scope 表达跨线程/跨卡可见性；`atom`（返回旧值）与 `red`（不返回，更轻）按需选择。
- **关键结论**：普通 GEMM 的同步链是「mbarrier 数据握手 + cluster/CTA 同步」，**不含 grid_sync**；grid_sync 是「单 kernel 多阶段」场景（Mega MoE）的专属全局汇合工具。
- **跨层衔接**：本讲补全了 u6-l1 双缓冲流水线背后的 PTX 指令细节，并为 u8（Mega MoE 融合内核）的 grid_sync / NVLink 通信重叠打下基础。

---

## 7. 下一步学习建议

1. **横向对比 MMA 封装**：回到 u6-l2 的 [`ptx/wgmma.cuh`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/ptx/wgmma.cuh)（`warpgroup_arrive` / `commit_batch` / `wait<0>`），把本讲的「数据搬运同步」与「计算提交同步」对齐看，理解一次 GEMM 的完整同步三角：**mbarrier（数据）+ wgmma fence/wait（计算）+ cluster/grid（协作）**。
2. **纵向进入调度器**：阅读 u6-l4（分块调度与 L2 swizzle），理解 `full_barriers`/`empty_barriers` 之外，block 在 SM 间如何被持久化调度，以及 multicast 的对齐约束如何反作用于 cluster 同步。
3. **进入 Mega MoE**：本讲已埋下 `grid_sync`、`nvlink_barrier`、1D TMA pull/combine 的伏笔，建议进入 u8-l1（Mega MoE 概念与对称内存）和 u8-l4（融合 mega 内核与通信重叠），看这些原语如何被组装成一个跨卡融合 mega-kernel。
4. **动手验证（可选）**：若手头有 SM90/SM100 机器，结合 u10-l4 的 `DG_JIT_DUMP_PTX/SASS=1` 与 `compute-sanitizer`，实际 dump 一个普通 GEMM kernel，确认其中没有 `grid_sync` 对应的全局原子自旋，只有 mbarrier 与（multicast 时）cluster barrier。

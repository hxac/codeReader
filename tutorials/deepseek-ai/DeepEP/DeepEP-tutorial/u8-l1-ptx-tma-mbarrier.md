# PTX 原语：TMA、mbarrier 与 fence.proxy

## 1. 本讲目标

学完本讲后，读者应该能够：

1. 说清楚 DeepEP V2 在 [`deep_ep/include/deep_ep/common/ptx.cuh`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/ptx.cuh) 里用内联汇编封装了哪几类底层 PTX 指令（TMA、mbarrier、fence、cp.async、原子归约等）。
2. 解释 **TMA（cp.async.bulk）异步批量拷贝** 与 **mbarrier 同步原语** 是如何配对使用的——即「发起 TMA → 期望字节数 arrive → 等待 phase 翻转」这一标准握手。
3. 讲明白为什么在「线程用普通指令写共享内存」与「TMA 读/写同一块共享内存」之间，必须插入一条 `fence.proxy.async.shared::cta`，缺失它会导致怎样的内存序问题（结合近期 commit #642）。
4. 了解 [`deep_ep/include/deep_ep/common/math.cuh`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/math.cuh) 里 `align`、`advance_ptr`、`encode_decode_positive` 等小工具在内核里的用途。

本讲是 **U8 底层原语** 的第一讲，承接 [u5-l1 直接模式 Dispatch](u5-l1-direct-dispatch.md) 中「notify / dispatch warps 协作」的结论，把视角从「warp 怎么分工」下沉到「单条 PTX 指令为什么这么写」。

## 2. 前置知识

### 2.1 共享内存与「代理（proxy）」模型

在 Hopper（SM90）之前的 GPU 上，线程访问共享内存（shared memory）只有一条路径：普通的 `ld`/`st` 指令。但从 Hopper 开始，GPU 引入了一条**独立的异步数据通路**——TMA（Tensor Memory Access），它由一个叫 **async proxy（异步代理）** 的硬件单元负责，可以让一个线程「下单」搬运一大块数据（global ↔ shared），而线程本身不必逐字搬运。

这就出现了**两个代理**：

| 代理 | 典型指令 | 谁在执行 |
| --- | --- | --- |
| generic proxy（通用代理） | `ld.global`、`st.shared`、`atomicAdd` 等 | 线程自己 |
| async proxy（异步代理） | `cp.async.bulk`（TMA）、`mbarrier.*` | 异步硬件单元 |

关键点：**这两个代理各自有一份对共享内存的「可见性视图」，彼此并不自动同步。** 线程用 `st.shared` 写入的共享内存数据，TMA（async proxy）去读时**未必能立刻看到**；反之亦然。这正是本讲后半部分 `fence.proxy.async.shared::cta` 要解决的问题。

### 2.2 PTX 与内联汇编

PTX（Parallel Thread Execution）是 NVIDIA GPU 的「接近汇编」的指令集。CUDA C++ 的编译器会把高级代码翻译成 PTX，再生成机器码（SASS）。当我们想用一条 CUDA C++ 没有直接暴露的高级指令（比如 TMA、mbarrier）时，就用 `asm volatile("...");` 把 PTX 文本**内联**进 C++ 代码。DeepEP 几乎所有底层原语都是这样写的。

如果你对 `__syncwarp()`、`__ldg()`、warp shuffle 等 CUDA 基础原语还不熟，建议先补一补再读源码精读部分。本讲假设你已读过 [u5-l1](u5-l1-direct-dispatch.md)，知道 dispatch 内核里有「notify warps + dispatch warps」的分工。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [`deep_ep/include/deep_ep/common/ptx.cuh`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/ptx.cuh) | 全部底层 PTX 原语封装，命名空间 `deep_ep::elastic::ptx` | TMA load/store、mbarrier init/arrive/wait、`fence.proxy.async.shared::cta` |
| [`deep_ep/include/deep_ep/common/math.cuh`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/math.cuh) | 设备/主机通用的算术小工具，命名空间 `deep_ep::elastic::math` | `align`、`ceil_div`、`advance_ptr`、`encode_decode_positive` |
| [`deep_ep/include/deep_ep/common/layout.cuh`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/layout.cuh) | workspace / token / buffer 的内存布局结构体 | mbarrier 在 token 布局里的位置、`kNumTMAAlignBytes` 对齐 |
| [`deep_ep/include/deep_ep/impls/dispatch.cuh`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch.cuh) | 直接模式 dispatch 内核（u5-l1 已讲 warp 分工） | 这些 PTX 原语被串成一条「load → fence → wait → store」的真实链路 |
| [`csrc/kernels/legacy/utils.cuh`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/legacy/utils.cuh) | V1 遗留内核的工具头 | commit #642 在此新增了 `fence_view_async_shared()` |

> 说明：V2 的内核（`deep_ep/include/deep_ep/impls/*.cuh`）是 header-only、运行时 JIT 实例化的（见 [u4-l1](u4-l1-jit-overview.md)）；V1 的 `csrc/kernels/legacy/*.cu` 是安装期编译的。本讲以 V2 的 `ptx.cuh` 为主，V1 的 `utils.cuh` 仅在「综合实践」里作为 commit 对比对象。

## 4. 核心概念与源码讲解

### 4.1 PTX 内联汇编与 GPU 内存代理模型

#### 4.1.1 概念说明

`ptx.cuh` 是 DeepEP 与 GPU 硬件「讨价还价」的地方：哪些数据搬运用最省 SM 的 TMA，哪些计数同步用 mbarrier，哪些跨 rank 信号用 `release/acquire` 语义的原子操作。要读懂这些封装，先得建立两个心智模型：

1. **代理模型**（见 2.1）：generic proxy 与 async proxy 各有独立视图。
2. **作用域（scope）模型**：每条同步指令都带一个作用域后缀，常见的有：
   - `.shared::cta`：仅对当前 CTA（即一个 thread block）内的共享内存生效。
   - `.shared::cluster`：对一个 cluster 内多个 CTA 生效（Hopper 引入 thread block cluster）。
   - `.global` + `.sys`：跨 GPU、甚至跨节点的系统级可见性（`.sys` 最强也最贵）。

`ptx.cuh` 里几乎所有指令都明写了作用域，例如 `mbarrier.init.shared::cta.b64` 表示「在 CTA 作用域的共享内存里初始化一个 64 位 mbarrier」。

#### 4.1.2 核心流程

读 `ptx.cuh` 的一条经验流程：

```text
看函数名 → 看asm里的PTX指令助记符 → 看地址转换 __cvta_generic_to_shared
       → 看输入输出约束 ("r"/"l"/"+r") → 看 "memory" clobber
```

其中两个高频技巧：

- `__cvta_generic_to_shared(ptr)`：把 C++ 的泛型指针转成**共享内存地址**，因为 PTX 的 shared 指令要求地址是 shared 段的形式。
- `asm volatile(... : : ... : "memory")` 末尾的 `"memory"` 是**编译器屏障**，告诉编译器「不要把这条指令前后的内存访问重排到它外面」，这是保证内存序的第一道闸。

#### 4.1.3 源码精读

先看命名空间入口和一个最简单的原语 `trap`（用于超时死锁时主动崩溃，便于定位）：

[mbarrier 主机占位结构与 32 字节对齐常量 — deep_ep/include/deep_ep/common/ptx.cuh:10-16](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/ptx.cuh#L10-L16)

```cpp
struct alignas(8) mbarrier { uint64_t __placeholder; };
using arrival_phase = uint32_t;
// More than TMA, `longlong4` requires 32 bytes aligned
static constexpr int kNumTMAAlignBytes = 32;
```

注意 `mbarrier` 这里只是一个**主机侧占位结构**——它和设备侧 `cuda::barrier<thread_scope_block>` 一样大（一个 64 位原子），目的是让 `sizeof(mbarrier)` 在主机和设备上保持一致，从而能在布局结构体（layout.cuh）里预留固定字节数。`kNumTMAAlignBytes = 32` 是整条数据通路的对齐基准：TMA 要求 16 字节对齐，但 DeepEP 还会用到 `longlong4`（32 字节），所以统一按 32 对齐。

[trap 原语 — deep_ep/include/deep_ep/common/ptx.cuh:21-23](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/ptx.cuh#L21-L23) 就是 `asm volatile("trap;");`，命中时 GPU 直接抛硬件异常。

再看一个 warp 级选举原语 `elect_one_sync`，它在 TMA/mbarrier 调用前被用来**只让一个线程下单**（TMA 与 mbarrier 指令通常只需一个代表线程发起）：

[elect_one_sync — deep_ep/include/deep_ep/common/ptx.cuh:37-53](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/ptx.cuh#L37-L53)

```cpp
asm volatile(
    "{ .reg .b32 %%rx; .reg .pred %%px;\n"
    "  elect.sync %%rx|%%px, %1;\n"
    "  @%%px mov.s32 %0, 1; }\n"
    : "+r"(pred) : "r"(0xffffffff));
return pred;
```

`elect.sync` 是 SM90 引入的硬件「选一个幸运 lane」指令，比传统的 `lane_idx == 0` 更高效且不依赖线程位置。在 dispatch 内核里你会反复看到 `if (ptx::elect_one_sync()) ptx::tma_load_1d(...)` 这种「选举代表 → 下单 TMA」的组合。

#### 4.1.4 代码实践

**实践目标**：熟悉 `ptx.cuh` 的文件骨架与命名约定。

**操作步骤**：

1. 打开 [`deep_ep/include/deep_ep/common/ptx.cuh`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/ptx.cuh)。
2. 用编辑器搜索 `__cvta_generic_to_shared`，统计它出现在多少个函数里（提示：几乎所有操作 shared 内存的原语）。
3. 搜索 `"memory"`，观察哪些原语带了编译器屏障、哪些没带，思考为什么。

**需要观察的现象**：TMA/mbarrier/原子类原语普遍带 `"memory"` clobber；而纯读取寄存器结果的 warp shuffle 类原语（`exchange`/`match`）不带。

**预期结果**：你会得出一个直觉——「凡是对共享/全局内存有副作用、且依赖顺序的，就必须加 `"memory"`」。这是理解后续 fence 的铺垫。

#### 4.1.5 小练习与答案

**练习 1**：`mbarrier` 结构体为什么要 `alignas(8)`？
**答案**：因为它底层是一个 64 位原子（`uint64_t`），PTX 的 `mbarrier.*.b64` 指令要求 8 字节对齐访问，否则行为未定义。

**练习 2**：`kNumTMAAlignBytes` 为什么取 32 而不是 TMA 文档里的 16？
**答案**：DeepEP 在同一条数据通路上还会用 `longlong4`（32 字节）做向量化读写（见 `ldg(const longlong4_t*)`），取 32 能同时满足 TMA 与 `longlong4` 的对齐要求。

---

### 4.2 TMA 异步批量拷贝（cp.async.bulk）

#### 4.2.1 概念说明

TMA 是 Hopper 引入的「硬件 DMA 引擎」：一条指令就能让硬件在 **global memory 与 shared memory 之间**搬运一整块连续字节（最多数十 KB），期间线程可以继续干别的活。相比传统的「每个 lane 用 `ld.global` 拼出一条向量加载」，TMA 有三大好处：

1. **省寄存器、省线程开销**：一个线程下单，硬件搬运，不必全员上阵。
2. **省 SM 资源**：这是 DeepEP 能把通信内核的 SM 占用压到极低的关键（见 [u1-l1](u1-l1-project-overview.md) 性能表里「SM 占用最多降到 1/4」）。
3. **天然支持跨 cluster 的分布式 shared memory**：TMA 可以直接读写 cluster 内其它 CTA 的 shared memory。

DeepEP 把 TMA 的两个方向封装成 `tma_load_1d`（global → shared）和 `tma_store_1d`（shared → global），外加 `tma_store_commit` / `tma_store_wait` 管理提交与等待。

#### 4.2.2 核心流程

一次完整的 TMA store（shared → global）典型链路是：

```text
1. 线程把数据(含metadata)写进 smem        ← generic proxy 写
2. tma_store_fence()                      ← fence.proxy.async.shared::cta（见 4.4）
3. elect_one_sync() 选一个线程
4. tma_store_1d(gmem_dst, smem_src, n)    ← async proxy 下单
5. tma_store_commit()                     ← cp.async.bulk.commit_group
6. (可选) 继续干别的，重叠时间
7. tma_store_wait()                       ← cp.async.bulk.wait_group，确保落地
```

而 TMA load（global → shared）则更依赖 mbarrier 来通知「数据到了」，详见 4.3。

TMA 还带一个 **L2 cache hint**：搬来的数据如果很快就用、之后不再需要，可以告诉硬件「尽快驱逐」（`kEvictFirst`）；如果是 store 出去、马上要被对端读，则用 `kEvictNormal`。

#### 4.2.3 源码精读

TMA load 的核心封装：

[tma_load_1d — deep_ep/include/deep_ep/common/ptx.cuh:115-127](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/ptx.cuh#L115-L127)

```cpp
asm volatile(
    "cp.async.bulk.shared::cluster.global.mbarrier::complete_tx::bytes"
    ".L2::cache_hint [%0], [%1], %2, [%3], %4;\n" ::
    "r"(..cvta..(dst_ptr)), "l"(src_ptr), "r"(num_bytes),
    "r"(..cvta..(ptr)), "l"(hint) : "memory");
```

逐段拆这条冗长的助记符：

- `cp.async.bulk`：TMA 的基础指令族。
- `.shared::cluster.global`：从 global 搬到 cluster 作用域的 shared（dst 是 smem）。
- `.mbarrier::complete_tx::bytes`：**搬完后通过 mbarrier 通知，并以字节为单位累计 tx（transfer）计数**——这是 TMA 与 mbarrier 握手的关键。
- `.L2::cache_hint`：附带缓存提示。
- 五个操作数依次是：smem 目的地址、gmem 源地址、字节数、关联的 mbarrier 地址、cache hint。

TMA store 的封装：

[tma_store_1d — deep_ep/include/deep_ep/common/ptx.cuh:129-139](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/ptx.cuh#L129-L139) 用的是 `cp.async.bulk.global.shared::cta.bulk_group`，注意它带 `.bulk_group`，意味着这条 store 会被编入一个 **bulk group**，必须配 `tma_store_commit` 提交、`tma_store_wait` 等待：

[tma_store_commit / tma_store_wait — deep_ep/include/deep_ep/common/ptx.cuh:101-108](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/ptx.cuh#L101-L108)

```cpp
__forceinline__ __device__ void tma_store_fence() {
    asm volatile("fence.proxy.async.shared::cta;");
}
template <int kNumRemainingWaits = 0>
__forceinline__ __device__ void tma_store_wait() {
    asm volatile("cp.async.bulk.wait_group %0;" ::"n"(kNumRemainingWaits) : "memory");
}
```

`tma_store_wait` 的模板参数 `kNumRemainingWaits` 表示「允许还剩几个未完成的 group 就返回」，默认 0 表示「全部落地才返回」。在 dispatch 内核里你会看到 `tma_store_wait<1>()`——允许还剩 1 个 group，用来做双缓冲流水线。

cache hint 的两个枚举值：

[TMACacheHint — deep_ep/include/deep_ep/common/ptx.cuh:110-113](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/ptx.cuh#L110-L113) 给出 `kEvictFirst = 0x12f0...`（搬来快用快弃）与 `kEvictNormal = 0x1000...`（store 出去给对端读）。

#### 4.2.4 代码实践

**实践目标**：在真实 dispatch 内核里看一条 TMA load 是怎么下单的。

**操作步骤**：

1. 打开 [`deep_ep/include/deep_ep/impls/dispatch.cuh`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch.cuh)，定位到第 287~292 行的 `// Issue data TMA` 注释块。
2. 注意它被包在 `if (ptx::elect_one_sync())` 里——只有一个线程下单。
3. 紧接着（295~312 行）是 SF（scaling factor）的搬运，用的是 `cp_async_ca` + `cp_async_mbarrier_arrive`，**不是** TMA。思考：为什么 hidden 用 TMA，而 SF 用 `cp.async`？

**需要观察的现象**：hidden 是大块连续字节（如 7168×2=14336 字节），适合 TMA；SF 数据量小、形状不规则，用 `cp.async.ca`（按 4/8/16 字节缓存异步加载）更灵活，并同样通过 `cp_async_mbarrier_arrive` 把到达事件挂到同一个 mbarrier 上。

**预期结果**：你会理解 DeepEP 是**按数据形状选搬运工具**的：大块走 TMA，碎块走 cp.async，但都汇拢到同一个 mbarrier 上做完成同步。结论可直接对照 [cp_async_ca / cp_async_mbarrier_arrive — deep_ep/include/deep_ep/common/ptx.cuh:145-157](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/ptx.cuh#L145-L157)。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `tma_load_1d` 不需要 `tma_store_commit`，而 `tma_store_1d` 需要？
**答案**：load 的完成由关联的 mbarrier 直接通知（`complete_tx::bytes` 累计到 mbarrier），不需要 group；而 store 把若干次写入编入 bulk group，必须显式 `commit_group` 提交、`wait_group` 等待。

**练习 2**：`tma_store_wait<1>()` 比 `tma_store_wait<0>()` 有什么用？
**答案**：允许还剩 1 个未完成 group 就返回，相当于「不阻塞当前这一帧的 store」，从而与下一帧的 TMA load 形成**双缓冲流水线**，隐藏延迟。

---

### 4.3 mbarrier 同步原语

#### 4.3.1 概念说明

mbarrier（memory barrier）是 Hopper 引入的**共享内存中的硬件同步对象**，本质上是一个 64 位计数器，支持「到达（arrive）」与「等待（wait）」两种操作。它可以用来同步：

- 一组线程（thread block 内）；
- TMA 的异步搬运（TMA arrive）；
- `cp.async`（通过 `cp.async.mbarrier.arrive`）。

相比老式的 `__syncthreads()`，mbarrier 的最大优势是**可以与异步代理握手**：TMA 搬完一块数据后自动 arrive，线程 wait 到翻转就知道「数据已就绪」，期间线程可以去干别的。DeepEP 用它把「TMA load 下单」和「等数据真正落到 smem」解耦。

mbarrier 用一个 **phase（相位）** 位来区分「本轮」与「下一轮」：每次满足到达计数，phase 翻转一次；线程 `try_wait.parity` 等的就是「phase 与自己手里的不同」。

#### 4.3.2 核心流程

DeepEP 里 mbarrier 的标准生命周期：

```text
1. mbarrier_init_with_fence(ptr, arrive_count=1)   ← 初始化 + fence.mbarrier_init.release.cluster
2. (生产者) 发起 tma_load_1d(..., ptr, num_bytes)  ← TMA 会自动 arrive 并累计 tx 字节
   (可选) cp_async_mbarrier_arrive(ptr)            ← cp.async 也挂到同一 mbarrier
3. (消费者) mbarrier_arrive_and_set_tx(ptr, num_bytes)  ← 线程侧补一个 expect_tx
4. (消费者) mbarrier_wait_and_flip_phase(ptr, phase)    ← 轮询 try_wait.parity，成功后 phase ^= 1
5. —— 此时 smem 里的数据已就绪，线程可读 ——
6. (循环回到 2，处理下一个 token；phase 已翻转，可复用同一 mbarrier)
```

为什么第 3 步线程还要再 `arrive_and_set_tx` 一次？因为初始化时 `arrive_count=1`，意味着 mbarrier 期望「1 次 arrive」就翻转。而 TMA 的 arrive 是**按字节数累计**的——线程需要通过 `expect_tx` 告诉 mbarrier「我期望收到 num_bytes 字节的 TMA 数据」，这样 TMA 搬完后 arrive 才会让计数达标。线程自己再 arrive 一次，凑齐 `arrive_count`。

#### 4.3.3 源码精读

初始化：

[mbarrier_init_with_fence — deep_ep/include/deep_ep/common/ptx.cuh:56-60](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/ptx.cuh#L56-L60)

```cpp
asm volatile("mbarrier.init.shared::cta.b64 [%1], %0;" ::
             "r"(arrive_count), "r"(..cvta..(ptr)));
asm volatile("fence.mbarrier_init.release.cluster;" ::);
```

注意它后面紧跟一条 `fence.mbarrier_init.release.cluster`——这条 fence 保证「init 的写」对 cluster 内其它 CTA 可见（DeepEP 在 cluster 模式下跨 CTA 复用 mbarrier）。

arrive 与 arrive+expect_tx：

[mbarrier_arrive / mbarrier_arrive_and_set_tx — deep_ep/include/deep_ep/common/ptx.cuh:67-75](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/ptx.cuh#L67-L75) 分别是 `mbarrier.arrive.shared::cta.b64` 与 `mbarrier.arrive.expect_tx.shared::cta.b64`。后者多带一个字节数，告诉 mbarrier 期望的 TMA 字节量。

等待并翻转 phase：

[mbarrier_wait_and_flip_phase — deep_ep/include/deep_ep/common/ptx.cuh:77-90](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/ptx.cuh#L77-L90)

```cpp
asm volatile(
    "{ .reg .pred P1; \n"
    "LAB_WAIT: \n"
    "  mbarrier.try_wait.parity.shared::cta.b64 P1, [%0], %1, %2; \n"
    "  @P1 bra DONE; \n"
    "  bra LAB_WAIT; \n"
    "DONE: }" ::
    "r"(..cvta..(ptr)), "r"(phase), "r"(0x989680));
phase ^= 1;
```

这是一个**自旋等待**：`try_wait.parity` 把结果写进谓词 `P1`，成功就跳到 DONE，失败就回到 LAB_WAIT 继续转。第三个操作数 `0x989680`（= 10,000,000）是**单次 try_wait 的等待周期数**（约 10M 个时钟周期，起忙等退避作用）。等待成功后 C++ 侧 `phase ^= 1` 翻转相位，供下一轮使用。

cp.async 的到达挂钩：

[cp_async_mbarrier_arrive — deep_ep/include/deep_ep/common/ptx.cuh:154-157](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/ptx.cuh#L154-L157) 用 `cp.async.mbarrier.arrive.shared::cta.b64`——当 cp.async 那批搬运完成时，自动给 mbarrier 发一个 arrive，从而让 TMA + cp.async **共用同一个 mbarrier**。

#### 4.3.4 代码实践

**实践目标**：在 dispatch 内核里追踪一次 mbarrier「init → load → arrive → wait」的完整握手。

**操作步骤**：

1. 在 [`dispatch.cuh`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch.cuh) 跳到 270 行附近，看 `// Init TMA` 块：`phase = 0`、`mbarrier_init_with_fence(mbarrier_ptr, 1)`。
2. 顺着 for 循环看 289 行的 `tma_load_1d`（TMA 自动 arrive）和 310 行的 `cp_async_mbarrier_arrive`（SF 挂同一 mbarrier）。
3. 再看 354~360 行的注释 `// Wait TMA load arrival`：

[dispatch.cuh 等待 TMA 到达 — deep_ep/include/deep_ep/impls/dispatch.cuh:354-360](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch.cuh#L354-L360)

```cpp
// NOTES: this arrive must be after the `ptx::cp_async_mbarrier_arrive`
if (ptx::elect_one_sync()) {
    ptx::mbarrier_arrive_and_set_tx(mbarrier_ptr, kNumHiddenBytes);
    ptx::mbarrier_wait_and_flip_phase(mbarrier_ptr, phase);
}
```

**需要观察的现象**：注释明确强调「这个 arrive 必须在 `cp_async_mbarrier_arrive` 之后」——因为 `expect_tx` 只声明了 hidden 的字节数，而 cp.async 的 SF 字节是另算的；如果顺序颠倒，mbarrier 可能在 SF 还没到时就因为 hidden 字节达标而提前翻转，导致读到未初始化的 SF。

**预期结果**：你会理解 mbarrier 的 `expect_tx` 是**按字节精确计数**的，多条异步源（TMA + cp.async）必须依次挂上、且线程侧 `set_tx` 要覆盖全部期望字节，才能保证「全部就绪」语义正确。

#### 4.3.5 小练习与答案

**练习 1**：`mbarrier_wait_and_flip_phase` 里为什么用 `try_wait` 而不是 `wait`？
**答案**：`try_wait.parity` 带一个超时周期数（`0x989680`），失败就回到循环重试；这样既能在数据未就绪时让出硬件资源（避免死等），又方便上层配合 `timeout_while` 做死锁检测（见 [u7-l1](u7-l1-barrier.md) 的 `trap`）。

**练习 2**：初始化时 `arrive_count=1`，但 TMA 和 cp.async 都会 arrive，会不会重复 arrive 导致计数错乱？
**答案**：不会。TMA 与 cp.async 的 arrive 走的是 **tx 字节通道**（由 `expect_tx` 声明），不直接消耗 `arrive_count`；线程侧那次 `mbarrier_arrive_and_set_tx` 才是消耗 `arrive_count=1` 的那一次。三者协同：tx 字节达标 + 1 次线程 arrive = 翻转。

---

### 4.4 fence.proxy.async.shared::cta：异步代理桥接（本讲重点）

#### 4.4.1 概念说明

回顾 2.1 的代理模型：**generic proxy 的写** 和 **async proxy（TMA）的读/写** 是两条独立通路，对同一块共享内存的可见性**不保证按程序顺序传递**。这意味着一个致命陷阱：

> 线程用普通 `st.shared` 写了 metadata（比如 `src_token_global_idx`），紧接着发起 `tma_store_1d` 想把「metadata + hidden」一起搬走。但 TMA（async proxy）**可能读到 fence 之前的旧值**——因为它没看到 generic proxy 刚写的新值。

`fence.proxy.async.shared::cta` 就是解决这个问题的指令。它的语义是：

> 把**当前线程在本 fence 之前通过 generic proxy 发起的共享内存写**，排到**本线程在本 fence 之后通过 async proxy 发起的操作**之前，作用域是 CTA 内的共享内存。

用人话说：**「我刚（普通指令）写进 smem 的东西，紧接着的 TMA 必须看得到。」**

DeepEP 把它封装成两个名字：V2 的 [`ptx::tma_store_fence`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/ptx.cuh#L101-L103)（语义偏向「store 前让 metadata 可见」），以及 V1 遗留的 [`fence_view_async_shared`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/legacy/utils.cuh#L385-L393)（语义偏向「view/消费完后再 re-arm」）。两者底层是同一条 PTX 指令。

#### 4.4.2 核心流程

DeepEP 里有**两种**典型用法，对应两个方向：

**用法 A：生产者侧（generic 写 → async 读）**

```text
线程写 smem(metadata/linked_list/topk_weights)   ← generic proxy 写
ptx::tma_store_fence()                            ← fence.proxy.async.shared::cta
tma_store_1d(gmem, smem, n)                       ← async proxy 读 smem 并搬走
```

这是 dispatch/combine 内核里最高频的模式，确保 TMA store 把线程刚写的 metadata 一起正确搬走。

**用法 B：消费者侧 re-arm（commit #642 修复的场景）**

```text
mbarrier_wait  ← TMA load 的数据已到 smem，线程消费它（generic 读/写）
... 业务逻辑 ...
fence_view_async_shared()                         ← fence.proxy.async.shared::cta
mbarrier_arrive(empty_barriers[stage])            ← 通知生产者：这块 stage 我用完了，可重用
```

commit #642 正是在 V1 低延迟 combine 内核（`internernode_ll.cu`）的消费者侧补上了这条 fence——此前缺它，导致多级流水线里出现数据相关的偶发错误。

#### 4.4.3 源码精读

V2 的封装（一行 PTX）：

[tma_store_fence — deep_ep/include/deep_ep/common/ptx.cuh:101-103](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/ptx.cuh#L101-L103)

```cpp
__forceinline__ __device__ void tma_store_fence() {
    asm volatile("fence.proxy.async.shared::cta;");
}
```

注意它**没有** `"memory"` clobber——因为这条 PTX 本身就是硬件内存序指令，语义已经足够强，编译器屏障是多余的。

用法 A 的真实调用点在直接模式 dispatch 里：

[dispatch 写完 metadata 后插 fence — deep_ep/include/deep_ep/impls/dispatch.cuh:329-334](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch.cuh#L329-L334)

```cpp
// Add source metadata (rank index and token index)
// Please ensure no TMA buffer shared memory writes after this part
if (ptx::elect_one_sync())
    *tma_buffer.get_src_token_global_idx_ptr() = rank_idx * kNumMaxTokensPerRank + token_idx;
ptx::tma_store_fence();
__syncwarp();
```

源码注释「**Please ensure no TMA buffer shared memory writes after this part**」点明了设计意图：此后不再有对 TMA buffer 的 generic 写，fence 一插，前面所有 metadata 写就「定型」了，紧接着的 `tma_store_1d`（见 366~378 行）能读到完整一致的 token。

用法 B（commit #642 修复点）在 V1 遗留头里：

[fence_view_async_shared — csrc/kernels/legacy/utils.cuh:385-393](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/legacy/utils.cuh#L385-L393)

```cpp
__device__ __forceinline__ void fence_view_async_shared() {
    asm volatile (
        "{\n\t"
        "fence.proxy.async.shared::cta; \n"
        "}\n" :: : "memory");
}
```

它在 `internernode_ll.cu` 的低延迟 combine 流水线里被调用，紧贴 `mbarrier_arrive(empty_barriers[stage_idx])` 之前（见 commit d4f41e4）。这里的 `empty_barriers` 是一个**多级环形缓冲**的「槽位空闲」mbarrier：消费者线程用 generic proxy 读完这一 stage 的数据后，必须先 fence，再 arrive 告诉生产者「stage 空了，可以再 TMA load 进来」。缺 fence 时，消费者对 smem 的 generic 操作可能尚未对 async proxy 可见，生产者下一轮 TMA load 与残留的 generic 访问产生竞争。

#### 4.4.4 代码实践（对应大纲指定任务）

**实践目标**：结合 commit #642，说清楚「缺这条 fence 会出什么内存序问题」。

**操作步骤**：

1. 用 `git show d4f41e4` 查看该 commit 的完整 diff（已在仓库历史中）。你会看到它只改了两个文件：`csrc/kernels/legacy/internode_ll.cu`（加一行 `fence_view_async_shared();`）和 `csrc/kernels/legacy/utils.cuh`（新增该函数）。
2. 注意 commit message 的两条提交节点：
   - 「Add fence.proxy.async.shared::cta between mbarrier wait and TMA load.」
   - 「Move fence before `mbarrier_arrive`.」
   说明作者最初想把 fence 放在 wait 与 load 之间，最终改为放在 `mbarrier_arrive` 之前——即「消费完 → fence → 通知可重用」。
3. 对照 V2 的 [`dispatch.cuh:329-334`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch.cuh#L329-L334) 的用法 A，理解两种用法的对称性：一个在「generic 写 → async 读」之间，一个在「async 写落地 → generic 消费 → 再触发 async」之间。

**需要观察的现象 / 预期结果**：能用一句话回答「缺失该 fence 会导致什么」——

> 缺失 `fence.proxy.async.shared::cta` 时，线程通过 generic proxy 对共享内存的写（或读）不会被保证对 async proxy 可见/有序。在用法 A 里，TMA store 会搬走**陈旧的 metadata**（如错误的 `src_token_global_idx`、未更新的 `topk_weights`），导致对端 rank 收到的 token 与路由表对不上；在用法 B（commit #642）里，消费者对 stage 的 generic 访问可能与生产者下一轮 TMA load 竞争，造成**偶发的、与数据和时序相关的脏读**。这类 bug 极难复现（多数情况下硬件恰好按序执行），但在大规模、高负载、特定 SM 调度下会以「偶发 token 错乱」或「校验不通过」的形式出现。

> 本地验证：commit #642 作用于 V1 低延迟路径（IBGDA），需要 InfiniBand + GPUDirect RDMA 硬件才能复现原始 bug。若手头只有单机 NVLink，可在阅读层面完成上述因果分析；若想实测，可对照 V2 的 `tests/elastic/test_ep.py` 在 `do_cpu_sync` 与最坏情况分配两种模式下长时间循环跑 dispatch+combine，观察无 fence 版本是否出现偶发错误（**待本地验证**，因为修复已合入主线，需手动回退该行才能复现）。

#### 4.4.5 小练习与答案

**练习 1**：既然 `fence.proxy.async.shared::cta` 已经是硬件序指令，为什么 V1 的 `fence_view_async_shared` 还多带了 `"memory"` clobber，而 V2 的 `tma_store_fence` 没有？
**答案**：`"memory"` 是给**编译器**看的屏障，防止编译器把前后的 C++ 内存访问重排到 fence 之外。V2 版本依赖调用点本身的 `__syncwarp()` 与注释约束（「no smem writes after this part」）来阻止编译器重排；V1 版本则更保守地用 `"memory"` 显式禁止编译器重排。两者对硬件的语义一致。

**练习 2**：能不能用 `__syncthreads()` 或 `__syncwarp()` 替代 `fence.proxy.async.shared::cta`？
**答案**：不能完全替代。`__syncthreads`/`__syncwarp` 同步的是**线程**，解决的是「线程间看到彼此的写」；而 `fence.proxy.async` 同步的是**同一线程的两个代理**（generic vs async），解决的是「TMA 看到线程的写」。即便所有线程都同步了，TMA 仍可能看不到 generic 写——必须用这条 proxy fence。

---

### 4.5 math.cuh：对齐、指针运算与 encode_decode_positive

#### 4.5.1 概念说明

[`math.cuh`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/math.cuh) 是一组**设备/主机双端**（`__device__ __host__`）的算术小工具，被 layout、内核 host 侧、device 侧广泛复用。它们看似平凡，却是 TMA 对齐与跨 rank 计数同步的基石。本讲关注三个：`align`/`ceil_div`（对齐）、`advance_ptr`/`ptr_diff`（指针运算）、`encode_decode_positive`/`is_decoded_positive_ready`（就绪编码）。

#### 4.5.2 核心流程

`encode_decode_positive` 是一个**对合函数（involution）**：\( f(f(x)) = x \)。

\[ f(x) = -x - 1 \]

它的设计目标是「让 0 兼任初值与未就绪哨兵，用负号位充当就绪标志」。具体用法（对照 `dispatch.cuh:188-191` 与 `hybrid_dispatch.cuh:198-203` 的真实调用）：

```text
生产者算出真实计数 c >= 0，写入 stored = f(c) = -c - 1   ← 恒 ≤ -1（负数）
消费者读取 stored，计算 decoded = f(stored) = -stored - 1
  若 stored == 0（从未写入）→ decoded = -1 < 0 → is_decoded_positive_ready = false（未就绪）
  若 stored == -6（c=5）    → decoded =  5  ≥ 0 → ready=true，且 decoded 就是原始计数 5
```

于是内存初值 0 天然表示「未就绪」，任何已写入值都是负数；消费者解码后 ≥0 即就绪，且直接拿回原始计数。这避免了「用 -1 当哨兵、但合法计数也可能是 -1」的歧义，也省去单独的 ready 标志位。

#### 4.5.3 源码精读

对齐工具：

[align / ceil_div — deep_ep/include/deep_ep/common/math.cuh:6-23](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/math.cuh#L6-L23)

```cpp
template <typename T>
__forceinline__ __device__ __host__ T ceil_div(T a, T b) { return (a + b - 1) / b; }
template <typename T, bool kDoCeilAlignment = true>
__forceinline__ __device__ __host__ T align(T a, T b) {
    return (kDoCeilAlignment ? ceil_div(a, b) : (a / b)) * b;
}
```

`align(a, b)` 把 `a` 向上（`kDoCeilAlignment=true`）或向下取整到 `b` 的倍数。在 `layout.cuh` 里它被用来把 hidden/sf/metadata/mbarrier 各段都按 `kNumTMAAlignBytes=32` 对齐，这正是 TMA 不越界、不错位的前提。

就绪编码：

[is_decoded_positive_ready / encode_decode_positive — deep_ep/include/deep_ep/common/math.cuh:25-33](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/math.cuh#L25-L33)

```cpp
template <typename dtype_t>
__forceinline__ __device__ __host__ bool is_decoded_positive_ready(const dtype_t& value) { return value >= 0; }
template <typename dtype_t>
__forceinline__ __device__ __host__ dtype_t encode_decode_positive(const dtype_t& value) { return -value - static_cast<dtype_t>(1); }
```

指针运算：

[advance_ptr / ptr_diff — deep_ep/include/deep_ep/common/math.cuh:35-42](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/math.cuh#L35-L42) 提供「按字节推进指针」「算两指针字节差」，是 layout.cuh 里所有 `get_xxx_ptr()` 的底层积木。

#### 4.5.4 代码实践

**实践目标**：在 layout.cuh 里看 `align` 如何被用来摆放 mbarrier。

**操作步骤**：

1. 打开 [`layout.cuh`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/layout.cuh)，看 [`TokenLayout::get_num_bytes`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/layout.cuh#L201-L208)：hidden、sf、metadata、mbarrier 四段**各自** `math::align(..., ptx::kNumTMAAlignBytes)` 后再相加。
2. 再看 [`get_mbarrier_ptr`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/layout.cuh#L246-L248)：mbarrier 被放在 metadata 段对齐之后的位置。

**需要观察的现象**：mbarrier 是 token 布局的**最后一段**，且仅当 `kWithMBarrier=true`（dispatch 主 kernel）时才占位；combine 的 epilogue 用 `BufferLayout<false>` 不带 mbarrier。

**预期结果**：你会理解「为什么每个 dispatch warp 的 TMA buffer 里都嵌了一个 mbarrier」——它跟 token 数据打包在一起，TMA 搬运时不会动它，但它就在 smem 里，线程可以原地 init/wait，省去单独的同步缓冲。

#### 4.5.5 小练习与答案

**练习 1**：验证 `encode_decode_positive` 是对合函数。
**答案**：\( f(f(x)) = -(-x-1) - 1 = x + 1 - 1 = x \)。故对合，编码与解码用同一函数。

**练习 2**：若把内存初值从 0 改成 -1，`encode_decode_positive` 方案还能区分「未写入」与「写入 0」吗？
**答案**：不能。初值 -1 时 `f(-1) = 0 ≥ 0` 会被误判为就绪（计数 0）。这正是方案刻意选 0 当初值的原因：0 解码得 -1 <0，天然未就绪；而任何真实写入都是负数。

---

## 5. 综合实践

把本讲四个最小模块（TMA、mbarrier、fence.proxy、math 工具）串起来，做一次「阅读型跟踪」：

**任务**：在直接模式 dispatch 内核 [`dispatch.cuh:270-385`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch.cuh#L270-L385) 中，为一个 token 的处理画一张「时序—代理」图，要求：

1. 横轴是时间，纵轴分两轨：**generic proxy（线程）** 与 **async proxy（TMA/cp.async/mbarrier）**。
2. 在图上标出至少这些事件，并注明它们落在哪条轨：
   - `mbarrier_init_with_fence`（init + cluster fence）
   - `tma_load_1d`（hidden，async）
   - `cp_async_ca` + `cp_async_mbarrier_arrive`（SF，async，挂同一 mbarrier）
   - 线程写 `topk_idx` / `topk_weights` / `src_token_global_idx`（generic 写）
   - **`tma_store_fence`**（跨代理桥，画一条连接两轨的箭头）
   - `mbarrier_arrive_and_set_tx` + `mbarrier_wait_and_flip_phase`（线程侧 arrive + 自旋等翻转）
   - `tma_store_1d`（async store，读 smem 含 metadata）+ `tma_store_commit`
3. 在图旁用一句话回答：**如果把第 5 步的 `tma_store_fence` 删掉，哪一步会读到陈旧数据？后果是什么？**

**参考答案要点**：

- 删掉 fence 后，第 7 步的 `tma_store_1d`（async proxy 读 smem）可能读不到第 4 步线程写的 `src_token_global_idx` / `topk_weights` 的新值。
- 后果：搬到对端 rank 的 token 携带了**错误的路由元数据**，combine 阶段按 `recv_src_metadata` 反向路由时会把这些 token 送回错误的 rank / 错误的槽位，表现为 token 错乱或校验失败（对照 [u6-l1](u6-l1-combine-main.md) 的反向路由表）。
- 进一步：因为这是「偶发的、依赖硬件调度」的可见性问题，在轻负载下大概率不复现，正是 commit #642 类 bug 难以定位的原因。

> 若想在真实硬件上「看到」这条 fence 的效果，可手动在本地分支回退 `dispatch.cuh:333` 那一行 `ptx::tma_store_fence();`，然后用 `tests/elastic/test_ep.py` 在大 `--num-tokens`、多轮循环下跑正确性比对（**待本地验证**，且需要多卡 Hopper 环境）。

## 6. 本讲小结

- `ptx.cuh` 是 DeepEP 与 GPU 硬件的接口层，靠内联汇编封装了 TMA、mbarrier、fence、cp.async、原子归约等 PTX 指令，并统一用 `__cvta_generic_to_shared` 做地址段转换。
- **TMA（`cp.async.bulk`）** 用一条指令搬运大块连续字节，是 DeepEP 把通信内核 SM 占用压到极低的关键；load 用 mbarrier 的 `complete_tx::bytes` 通知完成，store 用 bulk group + `commit/wait` 管理。
- **mbarrier** 是共享内存里的硬件同步对象，靠 `arrive_count` + `expect_tx` 字节计数 + phase 翻转，把「TMA/cp.async 异步搬运完成」与「线程消费」解耦；`try_wait.parity` 自旋等待还兼顾超时检测。
- **`fence.proxy.async.shared::cta`** 是 generic proxy 与 async proxy 之间的可见性桥梁：generic 写之后、async 读之前必须插；commit #642 在 V1 低延迟路径补的就是它，缺失会导致 TMA 读到陈旧 smem、产生偶发数据错乱。
- **`math.cuh`** 提供 `align`（TMA 对齐基石）、`advance_ptr`（布局指针运算）、`encode_decode_positive`（对合编码，让 0 兼任初值与未就绪哨兵、负号位当就绪标志）等工具，被 layout 与内核广泛复用。

## 7. 下一步学习建议

1. **横向对比 V1 的同名封装**：读 [`csrc/kernels/legacy/utils.cuh`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/legacy/utils.cuh)，对比 V1 与 V2 的 TMA/mbarrier 封装差异，为 [u9-l1 遗留 Buffer](u9-l1-legacy-buffer.md) 做铺垫。
2. **深入 barrier.cuh**：本讲的 mbarrier 是 CTA 内同步；跨 rank 的 GPU barrier 用的是 `red.release.sys`/`ld.acquire.sys` 等系统级原子，详见 [u7-l1 跨 rank GPU Barrier](u7-l1-barrier.md) 与 [`barrier.cuh`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/barrier.cuh)。
3. **看 hybrid 链路的 signaled tail**：hybrid dispatch/combine 用 mbarrier + signaled tail 做 RDMA/NVLink 两级流水线同步，建议结合 [u5-l2 Hybrid Dispatch](u5-l2-hybrid-dispatch.md) 重读 [`hybrid_dispatch.cuh`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/hybrid_dispatch.cuh) 里 `mbarrier_*` 与 `tma_*` 的交替出现位置。
4. **动手验证代理模型**：NVIDIA 的 PTX 文档对 `fence.proxy.async` 与 mbarrier 有权威描述，建议对照阅读 `fence.proxy` 与 `mbarrier` 两节，把本讲的直觉固化为可复述的硬件语义。

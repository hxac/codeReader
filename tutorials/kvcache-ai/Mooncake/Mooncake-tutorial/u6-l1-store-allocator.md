# 缓冲区分配与内存池（Allocator）

> 单元 6 · 第 1 讲
> 依赖：建议先学完 [u5-l5 Segment 与 Replica 数据模型](u5-l5-segment-replica-model.md)，了解 Segment、Replica、ReplicaType 等概念后再读本讲。

## 1. 本讲目标

Mooncake Store 把「数据放在哪」拆成两个层次的问题：

- **段内**：一个 Segment 持有一段连续大内存，谁负责在这段内存里切出一个个小 buffer？→ **Buffer Allocator**。
- **跨段**：一次 `Put` 要写 N 个副本，该挑哪些 Segment 来放这些副本？→ **AllocationStrategy**。

此外，客户端为了加速读取，还会在本地维护一块「热缓存」，只缓存真正频繁访问的 key → **LocalHotCache**；而涉及 GPU 与主机之间的搬运，则需要一块固定内存（pinned memory）缓冲池 → **PinnedBufferPool**。

学完本讲，你应当能够：

1. 说出 **CacheLib / Offset / Simple** 三类 Buffer Allocator 的差异与各自适用场景。
2. 说清 **RandomAllocationStrategy / FreeRatioFirstAllocationStrategy / CxlAllocationStrategy** 三种副本放置策略的选段逻辑，并能解释 FreeRatioFirst 在什么分布下能均衡各段利用率。
3. 描述 **LocalHotCache** 的 LRU 淘汰流程，以及 Count-Min Sketch 如何做「频率准入控制」。
4. 理解 PinnedBufferPool 的复用机制与有界性。

## 2. 前置知识

在进入源码前，先用大白话过一遍几个关键词。

- **Segment（段）**：Store 里一个逻辑存储单元，背后对应一段连续内存（或磁盘区域）。多个 Segment 可以挂载在同一个节点上。详见 u5-l5。
- **Replica（副本）**：同一份数据的多个拷贝，分散在不同 Segment 上以容错。
- **Allocator（分配器）**：在一段连续内存里「切蛋糕」的工具。给定一个请求大小，返回一段可用内存；用完后归还。
- **Slab 分配**：CacheLib 使用的一种经典分配方式：把大内存切成固定大小的「slab」（Mooncake 中通常是 4MB），再在 slab 内部按若干离散 size class 分配，能有效减少外部碎片，但分配粒度受 size class 限制。
- **Offset 分配（best-fit / bin-based）**：另一种分配思路，用一个偏移量（offset）描述每段已分配区域，能精确给出「最大连续空闲块」有多大，适合需要精确空闲度信息的场景。
- **LRU（Least Recently Used）**：缓存淘汰策略，最近最少使用的优先被换出。
- **Count-Min Sketch（CMS）**：一种概率数据结构，用很小的内存近似统计每个 key 的访问次数，存在少量过估（只高不低），常用于「频率准入」判断。
- **Pinned memory（固定内存 / 锁页内存）**：被锁定在物理内存、不会被操作系统换出的内存，GPU 与主机之间的 DMA 拷贝带宽比普通可换页内存高 10x~100x。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [mooncake-store/include/allocator.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/allocator.h) | Buffer Allocator 的接口与三类实现声明（CacheLib / Offset / Simple），以及 `AllocatedBuffer` 句柄。 |
| [mooncake-store/src/allocator.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/allocator.cpp) | 上述分配器的实现：构造、allocate、deallocate、`getLargestFreeRegion` 等。 |
| [mooncake-store/include/allocation_strategy.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/allocation_strategy.h) | `AllocatorManager` 容器、`AllocationStrategy` 抽象接口与三种策略实现、工厂函数。 |
| [mooncake-store/include/local_hot_cache.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/local_hot_cache.h) / [mooncake-store/src/local_hot_cache.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/local_hot_cache.cpp) | `LocalHotCache`（LRU 热缓存）与 `LocalHotCacheHandler`（异步填充）。 |
| [mooncake-store/include/count_min_sketch.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/count_min_sketch.h) | `CountMinSketch`，热缓存频率准入的核心数据结构。 |
| [mooncake-store/include/pinned_buffer_pool.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/pinned_buffer_pool.h) | `PinnedBufferPool`，固定内存缓冲池（用于 D2H 搬运）。 |
| [mooncake-store/include/types.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/types.h) | `AllocationStrategyType` 等枚举与常量。 |
| [mooncake-store/include/master_config.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_config.h) | 把配置字符串解析成 `AllocationStrategyType`。 |
| [mooncake-store/src/master_service.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp) | `PutStart` 路径调用策略 `Allocate`，把分配器与策略串联起来的入口。 |
| [mooncake-store/include/client_service.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/client_service.h) / [mooncake-store/src/client_service.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/client_service.cpp) | 客户端读取路径上的热缓存查询、频率准入与异步填充。 |
| [mooncake-store/tests/allocation_strategy_test.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/tests/allocation_strategy_test.cpp) | 策略单元测试，含 FreeRatioFirst 利用率均衡实验。 |

## 4. 核心概念与源码讲解

### 4.1 Buffer Allocator：把大内存切成 buffer

#### 4.1.1 概念说明

一个 Segment 在挂载时会拿到一段连续内存（例如 4GB）。我们不可能用 `malloc/free` 一个个小对象来管理它——那样既慢又容易碎片化。Buffer Allocator 就是「这段内存的内部管家」：给定一个请求大小 `size`，返回一个可用的 buffer；用完后归还，归还后的空间可以被再次分配。

Mooncake 提供三种实现，它们都继承自同一个抽象基类 `BufferAllocatorBase`：

- **CachelibBufferAllocator**：基于 Meta 开源的 CacheLib 的 slab 分配器，工业级、稳定，是默认选择。代价是它不精确追踪「最大连续空闲块」有多大。
- **OffsetBufferAllocator**：基于一个「偏移量分配器」（bin-based best-fit），能精确报告当前最大连续空闲块，适合需要按空闲度做调度（如 FreeRatioFirst）的场景。
- **SimpleAllocator**：直接分配真实内存并返回裸指针（`void*`），主要用在客户端一侧，不复用 `AllocatedBuffer` 句柄体系。

三者返回的「句柄」是 `AllocatedBuffer`，它用 RAII 管理生命周期：构造时持有分配器的弱引用，析构时自动 `deallocate`。

`ReplicaType` 枚举描述这块内存的副本用途，决定分配/释放时更新哪一类指标：

[mooncake-store/include/allocator.h:21-27](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/allocator.h#L21-L27) —— 定义 `ReplicaType`，区分 MEMORY / DISK / LOCAL_DISK / NOF_SSD / ALL。

#### 4.1.2 核心流程

一个分配器的标准生命周期：

```
挂载 Segment
   │  构造 BufferAllocator(base, size)
   ▼
┌─────────────────────────────────────┐
│ allocate(size)  ──►  AllocatedBuffer │  (可用空间不足时返回 nullptr)
└─────────────────────────────────────┘
   │  业务读写这块 buffer
   ▼
AllocatedBuffer 析构 (RAII)
   │  weak_ptr.lock() 拿到分配器
   ▼
deallocate(handle) ──► 归还空间
```

关键点：

- **best-effort 的失败语义**：分配器在空间不足时返回 `nullptr`，调用方（策略层）据此跳过该 Segment、尝试别的。
- **指标联动**：每次 allocate/deallocate 都会更新 `cur_size_` 并同步到 `MasterMetricManager`，用于全局可观测。
- **对齐要求**：CacheLib 要求基地址至少 8 字节对齐，且总大小须是 4MB（slab 大小）的整数倍，因此实践中基地址常用 `0x100000000`（4GB）这样的大值。

#### 4.1.3 源码精读

**抽象接口** —— 所有分配器必须实现这 7 个方法。注意 `getLargestFreeRegion()` 是策略层做过滤的关键：

[mooncake-store/include/allocator.h:106-127](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/allocator.h#L106-L127) —— `BufferAllocatorBase` 纯虚接口，注释明确 `getLargestFreeRegion` 只是 best-effort 估计。

其中 CacheLib 实现恒返回 `kAllocatorUnknownFreeSpace`（一个极大的哨兵值），保证它「永远被视为候选」：

[mooncake-store/include/allocator.h:29-31](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/allocator.h#L29-L31) —— 定义哨兵常量 `kAllocatorUnknownFreeSpace = size_t 最大值`。
[mooncake-store/include/allocator.h:177-179](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/allocator.h#L177-L179) —— CacheLib 分配器始终返回该哨兵。

**RAII 句柄** —— `AllocatedBuffer` 析构时，若分配器还活着就自动归还：

[mooncake-store/src/allocator.cpp:20-29](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/allocator.cpp#L20-L29) —— 析构函数：`weak_ptr` 还能 `lock()` 就 `deallocate(this)`；若已失效说明 Segment 已卸载，无需再维护指标。

这个句柄还能序列化成 `Descriptor`，供 Transfer Engine 跨节点寻址（含 buffer 地址、协议、传输端点）：

[mooncake-store/src/allocator.cpp:32-47](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/allocator.cpp#L32-L47) —— `get_descriptor()` 把 buffer 打包成可传输描述符；CXL 协议下端点被替换为段名。

**CacheLib 分配** —— 注意 `padding_size`：会把请求大小向上对齐到 slab 的最小分配粒度 `kMinSliceSize`：

[mooncake-store/src/allocator.cpp:124-155](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/allocator.cpp#L124-L155) —— `CachelibBufferAllocator::allocate`：`max(size, kMinSliceSize)` 后调用 CacheLib；成功则 `cur_size_` 自增并更新 master 指标。

**Offset 分配** —— 它用 `OffsetAllocator`，并能精确报告最大空闲块：

[mooncake-store/src/allocator.cpp:237-280](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/allocator.cpp#L237-L280) —— `OffsetBufferAllocator::allocate`：拿到 RAII handle，把指针与 handle 一并塞进 `AllocatedBuffer`；释放靠 handle 析构完成。

[mooncake-store/src/allocator.cpp:305-322](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/allocator.cpp#L305-L322) —— `getLargestFreeRegion` 读取 `storageReport().largestFreeRegion`，返回真实值（这才是 FreeRatioFirst 能用上它的原因）。

Offset 分配器在构造时还要根据段大小推算两张容量表的上下限（用于管理空闲块元数据）：

[mooncake-store/src/allocator.cpp:199-213](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/allocator.cpp#L199-L213) —— `init_capacity` 限制在 [1K, 64K]，`max_capacity` 限制在 [1M, 64M]，随段大小缩放。

#### 4.1.4 代码实践

**实践目标**：通过阅读与对比，理解 CacheLib 与 Offset 两类分配器在「空闲度可观测性」上的根本差异。

**操作步骤（源码阅读型）**：

1. 打开 [mooncake-store/tests/allocation_strategy_test.cpp:56-70](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/tests/allocation_strategy_test.cpp#L56-L70) 中的 `CreateTestAllocator`，看测试如何用一个开关 `BufferAllocatorType` 在两种实现间切换。
2. 跟踪一条调用链：`OffsetBufferAllocator::allocate` → `offset_allocator_->allocate(size)` → 失败返回 nullptr；再对照 `CachelibBufferAllocator::allocate` 的 `memory_allocator_->allocate(...)`。
3. 思考：为什么 CacheLib 的 `getLargestFreeRegion` 恒返回哨兵值，而 Offset 返回真实值？（提示：slab 分配的离散 size class 让「连续空闲块」概念不直接适用。）

**需要观察的现象**：CacheLib 永远返回同一个极大值；Offset 的返回值会随分配/释放动态变化。

**预期结果**：你能用一句话说清「需要精确空闲度选段 → 选 Offset；需要工业级稳定分配 → 选 CacheLib」。

**运行验证（可选）**：构建并运行该测试，命令形如（具体目标名以仓库 CMake 为准，待本地验证）：

```bash
# 在仓库根目录
cmake -S . -B build && cmake --build build -j
ctest --test-dir build -R allocation_strategy --output-on-failure
```

#### 4.1.5 小练习与答案

**练习 1**：`AllocatedBuffer` 为什么用 `std::weak_ptr` 持有分配器，而不是 `std::shared_ptr`？

> **答案**：避免循环引用。`AllocatedBuffer` 由分配器的 `shared_from_this()` 创建，若再以 `shared_ptr` 反向持有分配器，两者将互相引用、永远无法释放。`weak_ptr` 在析构时 `lock()` 失败即说明分配器已销毁，可安全跳过归还。

**练习 2**：CacheLib 分配器把 `padding_size = max(size, kMinSliceSize)`，这对很小的请求（如 100 字节）意味着什么？

> **答案**：会按最小 slab 分配粒度 `kMinSliceSize` 分配，实际占用比请求值大。这是 slab 分配的固有代价，换取分配速度与抗碎片。因此 `cur_size_` 统计的是「请求大小」而非「实际 slab 占用」，指标含义需注意。

---

### 4.2 AllocationStrategy：选哪个段放副本

#### 4.2.1 概念说明

Buffer Allocator 解决的是「段内怎么切」，AllocationStrategy 解决的是「一次写 N 个副本，挑哪些段」。它面向的不是单个 buffer，而是「一个 slice 的若干副本」。

策略层操作一个 `AllocatorManager` 容器：它把「段名 → 该段的分配器列表」组织起来，并额外维护一个 `names_` 数组，方便按下标随机挑选段。注意类注释强调：**线程安全由 `SegmentManager` 的 `segment_mutex_` 在外部保证**，策略自身不加锁。

Mooncake 提供三种策略：

- **RandomAllocationStrategy**：随机均匀地选段，简单、几乎无开销，但不会感知各段负载。
- **FreeRatioFirstAllocationStrategy**：随机采样若干候选段，按「空闲比例」降序排序，优先往更空的段放。继承自 Random，未满足时回退到纯随机。
- **CxlAllocationStrategy**：专门面向 CXL（Compute Express Link）共享内存场景，直接用调用方指定的首选段。

`AllocationStrategyType` 枚举把它们串起来，并被配置字符串驱动：

[mooncake-store/include/types.h:463-467](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/types.h#L463-L467) —— `RANDOM / FREE_RATIO_FIRST / CXL` 三种枚举。

#### 4.2.2 核心流程

三种策略都遵循统一的 **best-effort 语义**：尽力满足请求的副本数，但若资源不足，能放几个放几个；只有「一个都放不下」时才返回错误。每个 slice 的多个副本**保证落在不同段**上（冗余要求）。

**Random 的选段流程**：

```
1. 校验参数；若没有段可用 → NO_AVAILABLE_HANDLE
2. 若只有一个段 → 直接试这一个
3. 先尝试 preferred_segments（优先段），成功即占用
4. 还没满 → 从一个随机起点开始轮询 names_，依次尝试
      (跳过 excluded 与已用段；最多尝试 min(100, 段数) 次)
5. best-effort：返回已分到的副本（可能少于请求）
```

**FreeRatioFirst 的选段流程**：

```
1~3. 与 Random 相同（含 preferred 处理）
4. 计算还需 remaining = replica_num - 已分到数
5. 采样：从随机起点取 min(6 * remaining, 总段数) 个连续候选段
6. 对每个候选算 free_ratio = free_bytes / capacity，按降序排序
7. 从最空的开始依次尝试分配（跳过 excluded/used）
8. 仍未满足 → 回退到 Random 轮询
```

核心直觉：**用很小的采样开销（与副本数同阶）换取「往更空的段倾斜」**。当各段容量不等时，按「比例」而非「绝对剩余」比较，能让大小段的利用率趋于一致。

设某段容量为 \(C_i\)、已用为 \(U_i\)，则其空闲比例：

\[
r_i = \frac{C_i - U_i}{C_i} = 1 - \frac{U_i}{C_i}
\]

\(r_i\) 越大说明该段越空（利用率越低）。策略优先选 \(r_i\) 大的段，等价于优先填利用率低的段，从而把各段利用率 \(U_i/C_i\) 拉平。

#### 4.2.3 源码精读

**AllocatorManager 容器** —— `addAllocator` 同时维护 `names_` 数组与映射：

[mooncake-store/include/allocation_strategy.h:44-50](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/allocation_strategy.h#L44-L50) —— `addAllocator`：段名未见过才加入 `names_`，再追加到映射的向量里。
[mooncake-store/include/allocation_strategy.h:96](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/allocation_strategy.h#L96) —— `getNames()` 返回段名数组，供随机选段。
[mooncake-store/include/allocation_strategy.h:102-110](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/allocation_strategy.h#L102-L110) —— `getAllocators(name)` 取某段的分配器列表。

**Random 策略** —— 重点看 preferred 先行 + 随机轮询 + best-effort：

[mooncake-store/include/allocation_strategy.h:249-305](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/allocation_strategy.h#L249-L305) —— 先消费 preferred 段，再从随机起点轮询；`max_retry = min(kMaxRetryLimit, names.size())`，且只在「一个都没分到」时返回 `NO_AVAILABLE_HANDLE`。

`allocateSingle` 是「在某段内尝试分配一个副本」的助手：单分配器走快路径，多分配器时随机选起点依次试：

[mooncake-store/include/allocation_strategy.h:333-360](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/allocation_strategy.h#L333-L360) —— `allocateSingle`：段内多分配器时随机起跑，循环一圈找到第一个能分配的。

**FreeRatioFirst 策略** —— 核心是采样 + 排序：

[mooncake-store/include/allocation_strategy.h:428-479](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/allocation_strategy.h#L428-L479) —— 取 `min(6 * remaining, names.size())` 个候选，算 `free_ratio` 后降序排序，从最空段开始尝试；仍未满足则回退 Random 轮询。
[mooncake-store/include/allocation_strategy.h:519](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/allocation_strategy.h#L519) —— `kCandidateMultiplier = 6`，即「每还需 1 个副本就采样 6 个候选」。
[mooncake-store/include/allocation_strategy.h:521-538](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/allocation_strategy.h#L521-L538) —— `getSegmentFreeRatio`：累加段内所有分配器的容量与已用，返回 \((C - U) / C\)。

**CXL 策略** —— 直接用 `preferred_segments[0]` 指定的 CXL 段，并把 buffer 描述符改写为 CXL 寻址：

[mooncake-store/include/allocation_strategy.h:561-592](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/allocation_strategy.h#L561-L592) —— 取首选 CXL 分配器分配，再 `change_to_cxl` 把地址改为 CXL 偏移表示。

**工厂与配置**：

[mooncake-store/include/allocation_strategy.h:605-617](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/allocation_strategy.h#L605-L617) —— `CreateAllocationStrategy` 按枚举返回具体策略实例。
[mooncake-store/include/master_config.h:447-461](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_config.h#L447-L461) —— 把配置字符串 `random` / `free_ratio_first` / `cxl` 解析成枚举（大小写敏感，未知值回退 random）。

**串联入口** —— master 在 `PutStart` 时拿到 `AllocatorManager`，调用策略 `Allocate`：

[mooncake-store/src/master_service.cpp:1662-1664](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L1662-L1664) —— `allocation_strategy_->Allocate(allocator_manager, value_length, config.replica_num, preferred_segments)`，正是策略与分配器握手的地方。

#### 4.2.4 代码实践

**实践目标**：对比 Random 与 FreeRatioFirst 的选段逻辑，并设计实验说明 FreeRatioFirst 在何种访问分布下更能均衡各段利用率。

**操作步骤**：

1. 精读两个 `Allocate`：[allocation_strategy.h:206-306](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/allocation_strategy.h#L206-L306)（Random）与 [allocation_strategy.h:386-515](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/allocation_strategy.h#L386-L515)（FreeRatioFirst）。重点比较第 4 步「随机轮询」与「采样+排序」的差异。
2. 运行现成的均衡实验：[allocation_strategy_test.cpp:561-647](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/tests/allocation_strategy_test.cpp#L561-L647) `FreeRatioFirstLoadBalancingDistribution`。它建了 3 个**容量不等**的段（32MB / 64MB / 128MB），连续分配 3000 个 64KB，最后断言三段利用率之差 `< 15%`。
3. **设计对照实验**：复制该测试，把三段容量改成**相等**（都 64MB），分别用 Random 和 FreeRatioFirst 各跑一遍，统计每段分到的分配数与利用率。预期：等容量下两者利用率都接近均匀，差异主要出现在「容量不等」时。

**需要观察的现象**：
- 容量不等时：Random 的分配数会大致均匀（按段计数接近 1:1:1），但**利用率**严重倾斜（小段被打爆、大段很闲）；FreeRatioFirst 的利用率则被拉平。
- 容量相等时：两者表现接近，FreeRatioFirst 多出的采样排序开销几乎换不到收益（可参考同文件 `PerformanceComparison` 测试的耗时对比）。

**预期结果**：你能得出结论——**FreeRatioFirst 的价值在「段异构（容量/负载不均）」时最大**；当所有段同质且负载均匀时，Random 已经够好，FreeRatioFirst 仅增加少量常数开销。性能对比可参考 [allocation_strategy_test.cpp:650-711](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/tests/allocation_strategy_test.cpp#L650-L711)。

> 若本地环境不便编译 C++，标注「待本地验证」，但阅读断言与打印逻辑即可理解结论。

#### 4.2.5 小练习与答案

**练习 1**：为什么 FreeRatioFirst 采样数是 `6 * remaining` 而不是「全部段」？

> **答案**：副本数 `remaining` 通常很小（1~3），全量排序是 \(O(M \log M)\)（M 为段数，可达数百）。采样 `6 * remaining` 个候选后排序是 \(O(1)\) 级别，开销极低却足以在概率上覆盖到「较空的段」。这是「以很小的代价逼近最优负载均衡」的工程折中。注释原文也是这个意思（采样 2N 在文档注释里，代码里乘子是 6）。

**练习 2**：副本数请求 5、可用段只有 3 个时，`Allocate` 返回什么？

> **答案**：按 best-effort 语义，会成功返回 3 个副本（受限于段数），且分布在 3 个不同段。只有「一个副本都分不到」时才返回 `NO_AVAILABLE_HANDLE`。这正是测试 `InsufficientAllocatorsForReplicas` 验证的行为（见 [allocation_strategy_test.cpp:356-392](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/tests/allocation_strategy_test.cpp#L356-L392)）。

**练习 3**：`preferred_segments` 和 `excluded_segments` 同时包含同一个段时，谁优先？

> **答案**：exclude 优先。代码里每个分支都先判 `excluded_segments.contains(...)` 再判 used，所以即便被列为 preferred 也会被排除（见 [allocation_strategy_test.cpp:522-557](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/tests/allocation_strategy_test.cpp#L522-L557) 的冲突用例）。

---

### 4.3 LocalHotCache：LRU + Count-Min Sketch 准入控制

#### 4.3.1 概念说明

读取 KV 时，数据通常要从远端 Segment 经 Transfer Engine 搬过来，开销不小。如果某个 key 被频繁读取，每次都走网络就太浪费。`LocalHotCache` 是**客户端本地**的一块热缓存：把高频 key 的数据拷一份在本地内存里，下次命中就直接本地读，跳过网络。

但「本地内存有限」，不能什么 key 都缓存——否则冷 key 会把热 key 挤出去（缓存污染）。Mooncake 用两层机制解决：

- **LRU 淘汰**：缓存满了，淘汰最近最少使用的块。
- **频率准入（Count-Min Sketch）**：一个 key 第一次被读不立刻进缓存，只有当它的访问次数达到阈值（默认 2）才「晋升」进热缓存。这样一次性扫过的冷 key 不会污染缓存。

为了让「填充缓存」不阻塞读取主路径，填充是**异步**的：读取完成后把「待填充任务」丢给后台 worker 线程，由它把数据拷进缓存块。这引出一个一致性问题——填充过程中 key 可能被删除/覆盖，于是又引入了 **token（generation/epoch）** 机制来作废过期的异步填充。

#### 4.3.2 核心流程

**读取路径上的热缓存交互**（在 `Client::Get` 中）：

```
Get(key)
  │
  ├─ RedirectToHotCache(key)  ──► GetHotKey(key)
  │        命中? 是 → 改写 replica 描述符指向本地块, cache_used=true
  │        命中? 否 → cache_used=false
  ▼
  TransferRead(replica, slices)        // 命中则本地 memcpy，未命中则走 TE
  │
  ▼
  ReleaseHotKey(key)                   // 命中才释放，减少引用计数
  │
  ▼
  ShouldAdmitToHotCache(key, cache_used)
        cache_used? 是 → 不再晋升（已在缓存）
        否 → CountMinSketch.increment(key) >= 阈值? 
              是 → ProcessSlicesAsync → 异步填充
              否 → 跳过（频率不够，不污染缓存）
```

**Count-Min Sketch 的统计原理**：维护 \(d\) 行、每行 \(w\) 个计数器（Mooncake 默认 \(w=4096, d=4\)，计数器是 `uint8_t`）。对 key 用 \(d\) 个不同哈希各映射到一列并各自 `+1`，估计值取 \(d\) 个计数器的**最小值**：

\[
\hat{f}(key) = \min_{i=1}^{d} \text{table}\big[i,\; h_i(key)\bmod w\big]
\]

取最小值是为了抑制哈希碰撞带来的**过估**（CMS 只会高估、不会低估）。当所有计数器累计自增达到 \(w \times d\) 时，自动把全部计数器右移一位（即除以 2）做**衰减（aging）**，让最近访问的权重更高、避免计数器饱和到 255。

**LRU 块管理**：所有缓存块是定长的（默认 16MB）。新 key 要进缓存时，`GetFreeBlock` 从 LRU 尾部向前找第一个「没人正在用（`ref_count==0`）」的块作为牺牲者，淘汰旧映射、重用该块。为降低锁争用，读取时只给块打一个 `accessed` 原子标记（**延迟 touch**），在下次需要独占锁时（如淘汰前）再统一 `drainDeferredTouches` 把命中的块挪到队首。

#### 4.3.3 源码精读

**准入决策** —— 一行代码点睛：自增 CMS 并比较阈值：

[mooncake-store/include/client_service.h:640-648](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/client_service.h#L640-L648) —— `ShouldAdmitToHotCache`：`cache_used` 为真或无热缓存直接返回 false；否则 `admission_sketch_->increment(key) >= admission_threshold_` 才放行。

**读取路径调用点** —— 注意「命中则不晋升」的优化注释：

[mooncake-store/src/client_service.cpp:1120-1124](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/client_service.cpp#L1120-L1124) —— 频率准入：命中（cache_used）时跳过晋升与计数，避免重复填充。

**热缓存查询与改写** —— 命中时把远端地址替换成本地块地址：

[mooncake-store/src/client_service.cpp:1473-1496](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/client_service.cpp#L1473-L1496) —— `RedirectToHotCache`：`GetHotKey` 拿到本地块，校验大小一致后改写 `buffer_address_` 与 `transport_endpoint_`。

**Count-Min Sketch** —— `increment` 取最小值并触发衰减：

[mooncake-store/include/count_min_sketch.h:25-39](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/count_min_sketch.h#L25-L39) —— `increment`：每行哈希定位、自增（封顶 255）、取最小值；累计达到 `width * depth` 则 `decayLocked`。
[mooncake-store/include/count_min_sketch.h:72-79](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/count_min_sketch.h#L72-L79) —— `decayLocked`：全部计数器右移 1 位（÷2），重置计数。

**准入阈值配置** —— 默认 2，可用环境变量覆盖（范围 1~255）：

[mooncake-store/src/client_service.cpp:4033-4053](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/client_service.cpp#L4033-L4053) —— 创建 `CountMinSketch`，并解析 `MC_STORE_LOCAL_HOT_ADMISSION_THRESHOLD`（默认 2）。

**LRU 牺牲者选择** —— 从尾部向前找空闲块：

[mooncake-store/src/local_hot_cache.cpp:373-417](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/local_hot_cache.cpp#L373-L417) —— `GetFreeBlock`：反向遍历 LRU 找 `ref_count==0` 的牺牲者，从链表摘除并清理旧 key 映射；全在用则返回 nullptr。

**延迟 touch 的回收** —— 命中块被打标记，淘汰前先批量化重排：

[mooncake-store/src/local_hot_cache.cpp:419-442](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/local_hot_cache.cpp#L419-L442) —— `drainDeferredTouches`：遍历把 `accessed=true` 的块 `splice` 到队首并更新映射迭代器。
[mooncake-store/src/local_hot_cache.cpp:133-152](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/local_hot_cache.cpp#L133-L152) —— `GetHotKey`：只自增 `ref_count` 并置 `accessed` 标记，不立刻重排 LRU（读锁下也不能改链表结构）。

**构造与分块** —— 整块大内存切成定长块，全部初始入 LRU：

[mooncake-store/src/local_hot_cache.cpp:20-66](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/local_hot_cache.cpp#L20-L66) —— 构造：`memfd`（跨进程共享）或 `malloc`（私有）申请大块，按 `block_size_`（默认 16MB）切分，每块入 `lru_queue_`。

#### 4.3.4 代码实践

**实践目标**：通过调准入阈值，观察 Count-Min Sketch 如何影响「哪些 key 能进热缓存」。

**操作步骤（源码阅读 + 配置实验型）**：

1. 阅读准入链路：`ShouldAdmitToHotCache`（[client_service.h:640-648](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/client_service.h#L640-L648)）→ `ProcessSlicesAsync`（[client_service.cpp:4079-4100](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/client_service.cpp#L4079-L4100)）→ `LocalHotCacheHandler::SubmitPutTask`（[local_hot_cache.cpp:491-561](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/local_hot_cache.cpp#L491-L561)）。
2. 阅读热缓存单测 [mooncake-store/tests/client_local_hot_cache_test.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/tests/client_local_hot_cache_test.cpp)，看它如何断言命中/未命中行为。
3. **配置实验**：用一个能反复读同一批 key 的小客户端，分别设置环境变量：
   - `MC_STORE_LOCAL_HOT_ADMISSION_THRESHOLD=1`（来者不拒，第一次读即晋升）
   - `MC_STORE_LOCAL_HOT_ADMISSION_THRESHOLD=5`（更严格，需读 5 次才晋升）
   
   构造一个「80% 读集中在 20% 热 key、其余冷 key 各读一次」的访问分布，统计两种阈值下热缓存的命中率与缓存块占用。

**需要观察的现象**：
- 阈值=1：缓存很快被填满，冷 key 也会占块，热 key 可能被冷 key 挤出（缓存抖动）。
- 阈值=5：只有真热 key 进缓存，命中率更稳定，但首几次读会 miss（晋升延迟）。

**预期结果**：你直观感受到「频率准入」是在「命中率」与「抗污染」间做权衡，阈值是那个旋钮。若无法本地跑客户端，标注「待本地验证」，但读 `admission_threshold_` 默认值 2 与 CMS 的 increment/decay 即可理解机制。

#### 4.3.5 小练习与答案

**练习 1**：Count-Min Sketch 的估计值为什么取多行的**最小值**而不是平均？

> **答案**：哈希碰撞只会让计数器**多加**（其他 key 的访问也算进同一格），所以估计只会偏高、不会偏低。取最小值能挑出「碰撞最少的那行」，最大限度抑制过估；取平均反而会被碰撞严重的行拉高。

**练习 2**：异步填充用 token（cache_epoch + key_generation）解决什么问题？

> **答案**：填充是异步的，从「决定填充」到「worker 真正写入」之间，key 可能被 `RemoveHotKey`/`Put` 覆盖。若不作废，worker 会把**旧数据**写进缓存，造成脏读。token 在提交时快照当前 generation/epoch，写入时再校验；一旦不一致（generation 被 bump 或 epoch 被推进），就丢弃这次填充、把块还回池子（见 [local_hot_cache.cpp:82-93](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/local_hot_cache.cpp#L82-L93)）。

**练习 3**：为什么 `GetHotKey` 只打 `accessed` 标记而不立刻把块挪到 LRU 队首？

> **答案**：`GetHotKey` 持的是**读锁**（`shared_lock`），多个读可并发；若立刻改链表结构会破坏并发读的安全性。改成打原子标记、延迟到独占锁（如淘汰前的 `drainDeferredTouches`）再批量化重排，既保证了 LRU 时效性，又大幅降低锁争用。

---

### 4.4 PinnedBufferPool：固定内存缓冲池

#### 4.4.1 概念说明

当数据要在 GPU 与主机内存之间搬运（D2H / H2D）时，使用**固定内存（pinned / page-locked memory）**能获得比普通可换页内存高 10x~100x 的 DMA 带宽。但固定内存的分配/释放（如 `cudaMallocHost`）本身较贵，频繁分配会拖慢传输。`PinnedBufferPool` 就是一个**线程安全、有界**的固定内存缓冲池：用完的 buffer 不立刻释放，而是缓存起来下次复用；池子满了才真正释放，防止固定内存无限增长。

它与前面的 Buffer Allocator 体系**不同**：它不继承 `BufferAllocatorBase`，也不属于某个 Segment，而是传输路径上专用的临时缓冲中转站。

#### 4.4.2 核心流程

```
Acquire(size)
   │  锁内扫描 pool_，找第一个 capacity >= size 的块
   │  找到 → 取出（O(1) 交换删除）返回
   │  没找到 → 释放锁，AllocNew(size) 走平台固定分配 API
   ▼
业务用这块 buffer 做 D2H/H2D 搬运
   │
   ▼
Release(buf)
   │  锁内：pool_.size() < max_pool_size_? 
   │    是 → push_back 缓存复用
   │    否 → FreeBuffer 立即释放（有界保护）
```

平台分派：CUDA/MUSA/MACA/HYGON/COREX 用 `cudaMallocHost`，HIP 用 `hipHostMalloc`，Ascend 用 `aclrtMallocHost`，其余平台退化为 `new char[]`（普通内存，性能较低）。释放时按 `is_pinned` 标志选择对应的 free API。

#### 4.4.3 源码精读

[mooncake-store/include/pinned_buffer_pool.h:46-60](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/pinned_buffer_pool.h#L46-L60) —— `Acquire`：锁内线性找首个够大的块，用「交换到末尾再 pop」做 O(1) 删除；都没有就调 `AllocNew`。
[mooncake-store/include/pinned_buffer_pool.h:62-70](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/pinned_buffer_pool.h#L62-L70) —— `Release`：池未满则缓存，满了则立即 `FreeBuffer`，保证固定内存有上界。
[mooncake-store/include/pinned_buffer_pool.h:81-115](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/pinned_buffer_pool.h#L81-L115) —— `AllocNew`：按编译宏分派各平台的固定内存分配 API，失败时退化为 `new char[]`。
[mooncake-store/include/pinned_buffer_pool.h:33](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/pinned_buffer_pool.h#L33) —— `kDefaultMaxPoolSize = 32`，默认最多缓存 32 块。

#### 4.4.4 代码实践

**实践目标**：理解「有界复用」如何兼顾复用收益与内存安全。

**操作步骤（源码阅读型）**：

1. 读 `Acquire` 的「首够大（first-fit）」策略：它返回的是首个 `capacity >= size` 的块，可能比请求大不少。思考这会带来什么浪费（内部碎片），以及为何仍可接受。
2. 读 `Release` 的有界分支：池满即释放。思考若没有这个上界，长时间高频传输会让固定内存涨到多少。
3. 跟踪调用方：用 `Grep` 搜 `PinnedBufferPool` 在 `file_storage.cpp` / `client_service.cpp` 中的使用，看它服务于哪类搬运（提示：SSD/NoF 与 GPU 相关路径）。

**需要观察的现象**：`is_pinned` 标志如何决定释放时走 `cudaFreeHost` 还是 `delete[]`。

**预期结果**：你能说清「复用省的是分配开销，有界省的是内存占用，二者靠 `max_pool_size_` 平衡」。若想实测，可在调用点加日志统计 Acquire 命中率（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：`Acquire` 用 first-fit 而非 best-fit，会有什么副作用？

> **答案**：可能把一个大块分配给一个小请求，造成内部碎片（块内剩余空间浪费），后续大请求反而拿不到合适块而触发新分配。好处是实现简单、O(n) 扫描即可；在传输缓冲大小相对集中时副作用可控。

**练习 2**：为什么池满时选择「立即释放」而不是「LRU 淘汰」？

> **答案**：固定内存块之间无价值差异（都是定长缓冲），LRU 的收益不明显，而立即释放实现最简单且能严格守住上界。32 块的上界本身就是工程经验值，超出后及时回收比维护淘汰链更轻量。

---

## 5. 综合实践

把本讲四个模块串起来，做一个**端到端追踪任务**：模拟一次 `Put` + 多次 `Get`，画出数据「落在哪个段、是否进热缓存」的完整决策。

**任务描述**：

1. 假设集群有 3 个异构段：`segA`(32MB)、`segB`(64MB)、`segC`(128MB)，策略设为 `free_ratio_first`，副本数 1。连续写入若干 64KB 的 key。
2. 用本讲的公式手工推演前 ~10 次写入分别落在哪个段（每次都要更新该段的 `U_i`，重算 `r_i`）。验证你的推演是否符合「优先填利用率低的段」。
3. 再换 `random` 策略重做一遍，对比两段在「容量不等」下的利用率分布差异。
4. 接着为某个 key 模拟「被读 3 次」：第 1 次读（CMS 计数=1，未达阈值 2，不晋升）、第 2 次读（计数=2，触发异步填充）、第 3 次读（命中热缓存，本地返回）。画出 CMS 计数、token 校验、LRU 块状态的变化。

**交付物**：
- 一张选段决策表（前 10 次写入 × 三策略的落段结果）。
- 一段对 FreeRatioFirst 在异构段下「拉平利用率」的定量解释（用 \(r_i = 1 - U_i/C_i\)）。
- 热缓存三次读取的状态流转图。

**参考验证**：你的选段结果应与 [allocation_strategy_test.cpp:561-647](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/tests/allocation_strategy_test.cpp#L561-L647) 的「利用率差 < 15%」断言方向一致；热缓存行为应与 `ShouldAdmitToHotCache` 的阈值逻辑一致。

## 6. 本讲小结

- **Buffer Allocator** 是段内内存管家，三类实现分工：CacheLib（slab，工业稳定，空闲度不可知）、Offset（bin-based，空闲度精确可查）、Simple（客户端裸内存）。
- **`getLargestFreeRegion`** 的差异是后续策略能否做「按空闲度选段」的前提：CacheLib 恒返回哨兵值，Offset 返回真实最大空闲块。
- **AllocationStrategy** 解决跨段副本放置：Random 简单均匀、FreeRatioFirst 采样排序往更空的段倾斜、CXL 走专用段；三者都是 best-effort，保证副本落不同段。
- **FreeRatioFirst** 按 \(r_i = (C_i - U_i)/C_i\) 选段，在**段异构（容量/负载不均）**时最能拉平利用率，同质负载下与 Random 接近。
- **LocalHotCache** 用 LRU 淘汰 + Count-Min Sketch 频率准入，只缓存高频 key；异步填充配 token（generation/epoch）防止脏写。
- **PinnedBufferPool** 提供有界、可复用的固定内存缓冲，服务于 GPU/SSD 搬运路径，靠 `max_pool_size_` 平衡复用与内存安全。

## 7. 下一步学习建议

- 本讲聚焦「分配与选段」，但尚未涉及**段如何挂载/卸载**、以及 `AllocatorManager` 的线程安全如何由 `SegmentManager` 保证。建议接着学习 **SegmentManager** 相关讲义（对应单元 6 后续内容）。
- 想理解 `Allocate` 的结果如何变成可寻址的 `Replica::Descriptor` 并被 Transfer Engine 使用，可复习 [u5-l5 Segment 与 Replica 数据模型](u5-l5-segment-replica-model.md)，再进入 Transfer Engine 的传输路径讲义（单元 2、3）。
- 对热缓存的多进程共享（`use_shm=true` + memfd）与 dummy client 场景感兴趣，可精读 [local_hot_cache.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/local_hot_cache.cpp) 的 `GetBlockOffset` 与 [client_local_hot_cache_test.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/tests/client_local_hot_cache_test.cpp)。
- 若关心策略在更大规模下的表现，可阅读 [mooncake-store/benchmarks/allocation_strategy_bench.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/benchmarks/allocation_strategy_bench.cpp) 的基准测试。

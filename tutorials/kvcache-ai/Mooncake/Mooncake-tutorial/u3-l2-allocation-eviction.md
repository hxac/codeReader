# u3-l2: 内存分配与驱逐策略

本讲义深入讲解 Mooncake Store 的内存管理机制，包括内存分配器、分配策略、驱逐策略和 Lease 机制。

## 最小模块 1：内存分配器

### 概念说明

内存分配器是 Mooncake Store 中的基础内存管理组件，负责高效地分配和释放内存块。Mooncake Store 中的内存分配器管理的是 Client 注册的内存段，而不是 Master Service 自身的内存。当 Client 通过 `MountSegment` 请求向 Master 注册一段连续内存区域时，Master 会创建相应的内存分配器来管理这段内存。

内存分配器需要解决以下问题：
- **高效分配**：支持高并发的分配请求，最小化分配延迟
- **碎片控制**：减少内存碎片化，提高内存利用率
- **线程安全**：在多线程环境下安全地管理内存

Mooncake Store 提供了两种内存分配器实现：`OffsetBufferAllocator`（推荐）和 `CachelibBufferAllocator`（已废弃）。

### 伪代码或流程

内存分配器的核心操作流程：

```
分配操作：
function allocate(size):
    if size == 0:
        return error
    buffer = internal_allocator.allocate(size)
    if buffer == nullptr:
        return error
    return AllocatedBuffer(buffer, size)

释放操作：
function deallocate(handle):
    if handle == nullptr:
        return
    internal_allocator.deallocate(handle.buffer_ptr)
    handle.status = UNREGISTERED

查询容量：
function capacity():
    return total_size_

查询已用容量：
function size():
    return current_size_.load()
```

### 原理分析

#### OffsetBufferAllocator 原理

`OffsetBufferAllocator` 基于 [OffsetAllocator](https://github.com/sebbbi/OffsetAllocator) 实现，使用基于 bin（桶）的分配策略：

1. **Bin 分割**：将可分配空间按大小分割成多个 bin，每个 bin 管理特定大小范围的内存块
2. **O(1) 分配**：通过 offset 索引实现常数时间复杂度的分配操作
3. **最小碎片**：bin-based 策略有效减少外部碎片，提高内存利用率

数学上，假设有 \( n \) 个 bin，每个 bin \( i \) 管理大小范围为 \([2^i, 2^{i+1})\) 的内存块。分配大小为 \( s \) 的请求时，选择满足 \( 2^i \geq s \) 的最小 bin \( i \)，时间复杂度为 \( O(1) \)。

#### CachelibBufferAllocator 原理

`CachelibBufferAllocator` 基于 Facebook 的 [CacheLib](https://github.com/facebook/CacheLib) 实现，使用 slab 分配策略：

1. **Slab 分割**：将内存划分为多个固定大小的 slab
2. **Slab 内分配**：在 slab 内部进行小对象的快速分配
3. **碎片抵抗**：通过同类大小对象聚合减少碎片

CacheLib 在对象大小变化较大的工作负载下表现不佳，因此已被标记为废弃。

### 代码实践

#### BufferAllocatorBase 接口定义

https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/allocator.h#L106-L127

这段代码定义了所有内存分配器必须实现的基础接口，包括分配、释放、容量查询等核心操作。

#### OffsetBufferAllocator 实现

https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/allocator.h#L204-L249

这段代码实现了 OffsetBufferAllocator 类，它封装了 OffsetAllocator 并实现了 BufferAllocatorBase 接口。关键成员包括：
- `offset_allocator_`：实际的 offset allocator 实例
- `segment_name_`：段名称，用于标识内存段
- `total_size_`：总容量
- `cur_size_`：当前已用容量（原子变量）

#### 内存分配操作

https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/allocator.h#L214

这是 `allocate` 方法的声明，实际实现在对应的源文件中。分配操作会调用底层的 OffsetAllocator 来分配内存块。

#### AllocatorManager 管理多个分配器

https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/allocation_strategy.h#L26-L120

`AllocatorManager` 负责管理所有可用的内存分配器，提供添加、删除、查询分配器的功能。它使用 `segment_name` 到分配器列表的映射来支持同一 segment 上的多个分配器。

### 练习题

1. 为什么 Mooncake Store 推荐使用 OffsetBufferAllocator 而不是 CachelibBufferAllocator？
2. 内存分配器管理的是谁的内存？为什么这样设计？
3. 在多线程环境下，如何保证 `cur_size_` 的正确性？
4. 什么是内存碎片？OffsetBufferAllocator 如何减少碎片？

### 答案

1. **答案**：OffsetBufferAllocator 在 LLM 推理工作负载下表现更好，因为它使用基于 bin 的分配策略，能够有效控制碎片化并提供 O(1) 的分配性能。CachelibBufferAllocator 在对象大小变化较大的工作负载下碎片化严重，已被标记为废弃。

2. **答案**：内存分配器管理的是 Client 注册的内存段，而不是 Master Service 自身的内存。这样设计符合 Mooncake Store 的"控制流与数据流分离"原则，Master 只负责元数据管理和空间分配，实际数据存储在 Client 的内存中。

3. **答案**：`cur_size_` 使用 `std::atomic_size_t` 类型声明（见第 241 行），通过原子操作保证多线程环境下的正确性，避免了显式锁的开销。

4. **答案**：内存碎片是指内存中存在大量无法利用的小空闲块。OffsetBufferAllocator 通过基于 bin 的分配策略，将相似大小的对象聚合在同一个 bin 中，减少了外部碎片的产生。同时，offset-based 的分配方式也减少了内部碎片。

---

## 最小模块 2：分配策略

### 概念说明

分配策略（AllocationStrategy）负责在分布式环境中选择合适的存储段来放置对象副本。Mooncake Store 的设计目标是：
- **负载均衡**：在多个存储段之间均匀分布数据，避免热点
- **高可用性**：通过多副本机制保证数据的可用性
- **动态扩展**：支持运行时添加/删除存储段

分配策略的核心挑战在于如何在多个可用段中选择最优的分配位置。Mooncake Store 提供了三种内置策略：`RandomAllocationStrategy`（默认）、`FreeRatioFirstAllocationStrategy` 和 `CxlAllocationStrategy`。

### 伪代码或流程

分配策略的核心流程：

```
分配 N 个副本：
function Allocate(slice_length, replica_num, preferred_segments, excluded_segments):
    if slice_length == 0 or replica_num == 0:
        return error INVALID_PARAMS
    
    replicas = []
    used_segments = set()
    
    # 第一阶段：优先段分配
    for segment in preferred_segments:
        if segment not in excluded_segments and segment not in used_segments:
            buffer = allocate_from_segment(segment, slice_length)
            if buffer != nullptr:
                replicas.add(Replica(buffer, segment))
                used_segments.add(segment)
                if len(replicas) == replica_num:
                    return replicas
    
    # 第二阶段：随机/按比例分配剩余副本
    while len(replicas) < replica_num and try_count < max_retry:
        segment = select_segment(all_segments, used_segments, excluded_segments)
        if segment != nullptr:
            buffer = allocate_from_segment(segment, slice_length)
            if buffer != nullptr:
                replicas.add(Replica(buffer, segment))
                used_segments.add(segment)
        try_count++
    
    if len(replicas) > 0:
        return replicas  # Best-effort: 可能少于 replica_num
    else:
        return error NO_AVAILABLE_HANDLE
```

### 原理分析

#### RandomAllocationStrategy 原理

`RandomAllocationStrategy` 采用纯随机选择策略，其核心思想是：
- **随机性**：通过随机选择避免人为偏差
- **简单性**：实现简单，无需维护复杂的状态
- **高效性**：O(N) 时间复杂度，N 为副本数

数学上，假设有 \( m \) 个可用段，需要分配 \( n \) 个副本。随机策略的选择概率为：
\[ P(\text{选择段 } i) = \frac{1}{m} \]

在 \( n \) 次选择中，段 \( i \) 被选中的期望次数为：
\[ E[i] = \frac{n}{m} \]

#### FreeRatioFirstAllocationStrategy 原理

`FreeRatioFirstAllocationStrategy` 采用 Best-of-N 策略，其核心思想是：
1. **采样候选段**：随机采样 \( K = \min(6n, m) \) 个候选段，其中 \( n \) 为剩余副本数
2. **按空闲比例排序**：计算每个候选段的空闲比例 \( \text{ratio}_i = \frac{\text{free}_i}{\text{capacity}_i} \)
3. **选择最优段**：按空闲比例降序排序，选择前 \( n \) 个段进行分配

这种策略的优势在于：
- **加速收敛**：新加入的空段具有最高的空闲比例，自然优先被选中
- **负载均衡**：倾向于选择空闲比例高的段，促进负载均衡
- **低开销**：采样和排序的开销为 \( O(K \log K) \)，由于 \( K \) 通常较小（1-3 个副本对应 6-18 个候选段），开销可控。

数学上，段 \( i \) 被选中的概率与空闲比例正相关：
\[ P(\text{选择段 } i) \propto \frac{\text{free}_i}{\text{capacity}_i} \]

#### CxlAllocationStrategy 原理

`CxlAllocationStrategy` 是专用于 CXL（Compute Express Link）内存硬件的策略：
- **强制选择**：必须指定 `preferred_segments`，且强制选择第一个段
- **单副本**：仅支持单副本分配
- **特殊标记**：分配的缓冲区会被标记为 CXL 类型

这种策略适用于需要显式控制数据放置位置的异构内存场景。

### 代码实践

#### AllocationStrategy 接口定义

https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/allocation_strategy.h#L132-L186

这段代码定义了分配策略的接口，包括 `Allocate`（分配多个副本）和 `AllocateFrom`（从指定段分配单个副本）两个核心方法。

#### RandomAllocationStrategy 实现

https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/allocation_strategy.h#L206-L306

`RandomAllocationStrategy` 的核心实现包括：
- 第 231-245 行：单段快速路径
- 第 249-269 行：优先段处理阶段
- 第 273-299 行：随机分配阶段

#### FreeRatioFirstAllocationStrategy 实现

https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/allocation_strategy.h#L382-L539

`FreeRatioFirstAllocationStrategy` 的核心实现包括：
- 第 432-450 行：采样候选段并计算空闲比例
- 第 453-456 行：按空闲比例降序排序
- 第 459-479 行：从排序后的候选段中分配
- 第 485-509 行：回退到随机分配

#### 空闲比例计算

https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/allocation_strategy.h#L521-L538

`getSegmentFreeRatio` 方法计算段的空闲比例：`free_ratio = total_free / total_capacity`。如果总容量为 0，返回 0.0。

### 练习题

1. 为什么 FreeRatioFirstAllocationStrategy 要采样 \( 6n \) 个候选段，而不是全部段？
2. 在什么场景下应该使用 RandomAllocationStrategy，什么场景下应该使用 FreeRatioFirstAllocationStrategy？
3. 为什么分配策略采用 best-effort 语义，而不是 all-or-nothing？
4. CxlAllocationStrategy 的限制是什么？为什么有这些限制？

### 答案

1. **答案**：采样 \( 6n \) 个候选段是在负载均衡效果和性能开销之间的折衷。采样全部段需要遍历所有段（可能在大量段的情况下开销大），而采样过少可能导致无法找到合适的段。6 倍采样系数在实验中被证明是较好的平衡点。

2. **答案**：RandomAllocationStrategy 适合稳定的集群（段很少加入/离开）且追求最大吞吐量的场景，因为它的开销最小。FreeRatioFirstAllocationStrategy 适合需要更好负载均衡和动态扩容的场景，特别是在段容量不均匀或频繁加入新段的情况下。

3. **答案**：Best-effort 语义可以提高系统的可用性和灵活性。在资源受限的情况下，分配部分副本总比完全不分配要好，这样至少可以提供一定程度的可用性。调用方可以根据实际分配的副本数决定是否接受结果。

4. **答案**：CxlAllocationStrategy 的限制包括：仅支持单副本分配、不支持 `AllocateFrom` 接口、必须指定 `preferred_segments`。这些限制是因为 CXL 内存是特殊的硬件资源，通常需要显式控制数据放置位置，且可能不支持跨段分配。

---

## 最小模块 3：驱逐策略

### 概念说明

驱逐策略（EvictionStrategy）负责在内存空间不足时选择合适的对象进行驱逐，以释放空间给新的对象。Mooncake Store 的驱逐策略需要解决以下问题：
- **选择驱逐对象**：如何选择哪些对象应该被驱逐
- **保证一致性**：如何避免驱逐正在使用的对象
- **提高命中率**：如何最大化缓存命中率

Mooncake Store 目前提供了两种驱逐策略：`LRUEvictionStrategy`（默认）和 `FIFOEvictionStrategy`。LRU（Least Recently Used，最近最少使用）是一种经典的缓存替换策略，基于局部性原理，认为最近被访问的对象更有可能再次被访问。

### 伪代码或流程

#### LRU 驱逐策略流程

```
添加键：
function AddKey(key):
    if key in all_key_idx_map:
        all_key_list_.erase(all_key_idx_map_[key])
        all_key_idx_map_.erase(key)
    all_key_list_.push_front(key)
    all_key_idx_map_[key] = all_key_list_.begin()

更新键（访问时）：
function UpdateKey(key):
    if key in all_key_idx_map:
        all_key_list_.erase(all_key_idx_map_[key])
        all_key_list_.push_front(key)
        all_key_idx_map_[key] = all_key_list_.begin()

移除键：
function RemoveKey(key):
    if key in all_key_idx_map:
        all_key_list_.erase(all_key_idx_map_[key])
        all_key_idx_map_.erase(key)

驱逐键：
function EvictKey():
    if all_key_list_.empty():
        return ""
    evicted_key = all_key_list_.back()
    all_key_list_.pop_back()
    all_key_idx_map_.erase(evicted_key)
    return evicted_key
```

### 原理分析

#### LRU 策略原理

LRU 策略的核心思想是**时间局部性**（Temporal Locality）：如果一个对象最近被访问过，那么它在未来很可能再次被访问。

LRU 使用双向链表 + 哈希表的数据结构：
- **双向链表**：维护访问顺序，链表头部是最常访问的对象，尾部是最少访问的对象
- **哈希表**：存储键到链表节点的映射，支持 O(1) 时间查找和更新

访问对象时，将对象移动到链表头部；驱逐时，选择链表尾部的对象。

数学上，假设对象 \( i \) 在时间 \( t \) 被访问，其访问概率可以建模为：
\[ P(\text{访问对象 } i \text{ 在 } t+\Delta t) \propto e^{-\lambda_i \Delta t} \]

其中 \( \lambda_i \) 是对象 \( i \) 的访问速率。LRU 策略倾向于驱逐访问速率低的对象。

#### FIFO 策略原理

FIFO（First-In-First-Out）策略按照对象进入缓存的顺序进行驱逐，先进入的对象先被驱逐。

FIFO 的实现更简单，只需要维护一个队列：
- **入队**：新对象添加到队尾
- **出队**：驱逐时选择队首对象

FIFO 不考虑访问频率，只考虑进入时间，因此在许多工作负载下性能不如 LRU。

#### 安全驱逐保证

Mooncake Store 的驱逐策略包含多层安全检查：
1. **Lease 检查**：有活跃 lease 的对象不会被驱逐
2. **完整性检查**：未完成 `PutEnd` 的对象不会被驱逐
3. **Pin 检查**：Hard pinned 对象不会被驱逐；Soft pinned 对象在无其他候选时才会被驱逐
4. **组驱逐**：对于分组的对象，会解析当前组成员并尝试一起驱逐

### 代码实践

#### EvictionStrategy 接口定义

https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/eviction_strategy.h#L16-L41

这段代码定义了驱逐策略的接口，包括 `AddKey`、`UpdateKey`、`RemoveKey`、`EvictKey` 等核心方法。

#### LRUEvictionStrategy 实现

https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/eviction_strategy.h#L43-L77

`LRUEvictionStrategy` 的实现包括：
- 第 45-54 行：`AddKey` 方法，将键添加到链表头部
- 第 56-65 行：`UpdateKey` 方法，将键移动到链表头部
- 第 67-77 行：`EvictKey` 方法，选择链表尾部的键进行驱逐

#### FIFOEvictionStrategy 实现

https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/eviction_strategy.h#L79-L97

`FIFOEvictionStrategy` 的实现更简单，只在 `AddKey` 时添加到链表头部，`UpdateKey` 不做任何操作。

#### 驱逐触发条件

https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/design/mooncake-store.md#L585-L588

根据设计文档，驱逐在以下两种情况下触发：
1. `PutStart` 请求因内存不足而失败
2. 存储空间使用率达到高水位线（默认 95%，可配置）

驱逐目标是释放一定比例的空间（默认 5%，可配置）。

### 练习题

1. LRU 策略为什么使用双向链表 + 哈希表的组合？单用其中一种有什么问题？
2. 在什么场景下 FIFO 策略可能比 LRU 策略表现更好？
3. 为什么 Mooncake Store 选择"近似 LRU"而不是"精确 LRU"？
4. 驱逐策略如何与 Lease 机制协同工作？

### 答案

1. **答案**：双向链表支持 O(1) 时间插入和删除，但不支持 O(1) 时间查找；哈希表支持 O(1) 时间查找，但不维护顺序。两者结合可以实现 O(1) 时间查找、插入、删除和更新，这是 LRU 策略的高效实现。

2. **答案**：FIFO 策略在某些访问模式下可能表现更好，例如顺序扫描工作负载（每个对象只访问一次），或者在实现简单性比性能更重要的场景。FIFO 不需要维护访问顺序，开销更低。

3. **答案**：精确 LRU 需要在每次访问时更新链表，在高并发场景下可能导致锁竞争严重。近似 LRU 可以通过定期更新、概率更新等方式减少锁竞争，虽然在精确性上有所损失，但在性能上可能有显著提升。

4. **答案**：驱逐策略在执行驱逐前会检查对象的 lease 状态。如果对象有活跃的 lease（未过期），则跳过该对象。这保证了正在被使用的对象不会被驱逐，避免数据不一致。当 lease 过期后，对象可以被驱逐。

---

## 最小模块 4：Lease 机制

### 概念说明

Lease 机制是 Mooncake Store 保证读写一致性的核心机制。当一个 Client 正在读取对象时，该对象不应该被驱逐或删除，否则可能导致数据不一致或不完整。

Lease 机制为每个对象提供一个临时的保护期，在保护期内对象不会被驱逐或删除。Lease 的核心思想是：
- **时间窗口保护**：在指定的时间窗口内保护对象
- **自动刷新**：对象被访问时自动延长 lease
- **防止竞态**：避免读写操作之间的竞态条件

Lease 机制与 Pin 机制协同工作：Hard Pin 提供永久保护（仅可通过显式 `Remove` 删除），Soft Pin 提供优先级保护（优先驱逐非 soft pinned 对象），Lease 提供临时保护（读写操作期间）。

### 伪代码或流程

#### Lease 生命周期

```
授予 Lease：
function GrantLease(ttl, soft_ttl):
    now = current_time()
    lease_timeout = max(lease_timeout, now + ttl)
    if soft_pin_enabled:
        soft_pin_timeout = max(soft_pin_timeout, now + soft_ttl)

检查 Lease 过期：
function IsLeaseExpired():
    now = current_time()
    return now >= lease_timeout

检查是否需要刷新：
function NeedsLeaseRefresh(ttl, soft_ttl):
    now = current_time()
    if lease_timeout <= now + ttl/2:
        return true  # lease 即将过期，需要刷新
    return false

对象访问时：
function OnObjectAccess():
    if NeedsLeaseRefresh(ttl, soft_ttl):
        GrantLease(ttl, soft_ttl)

驱逐检查：
function CanEvict():
    if IsLeaseExpired() == false:
        return false  # 有活跃 lease，不能驱逐
    if is_hard_pinned:
        return false  # hard pinned，不能驱逐
    if is_soft_pinned and allow_evict_soft_pinned == false:
        return false  # soft pinned 且配置不允许驱逐
    return true  # 可以驱逐
```

### 原理分析

#### Lease 时间窗口原理

Lease 机制基于时间窗口模型：在时间窗口 \([t_{\text{start}}, t_{\text{end}}]\) 内，对象受到保护。

假设当前时间为 \( t \)，Lease 过期时间为 \( t_{\text{lease}} \)，则对象受保护的条件是：
\[ t < t_{\text{lease}} \]

当对象被访问时，Lease 会延长：
\[ t_{\text{lease}} \leftarrow \max(t_{\text{lease}}, t + \text{ttl}) \]

这种设计保证了 Lease 只会延长，不会缩短，避免频繁的 Lease 刷新。

#### Lease 与 Soft Pin 的区别

| 特性 | Lease | Soft Pin |
|------|-------|----------|
| **生命周期** | 短期（秒级，默认 5 秒） | 中期（分钟级，默认 30 分钟） |
| **刷新方式** | 访问时自动刷新 | 访问时自动刷新 |
| **驱逐优先级** | 有 lease 时不驱逐 | 优先驱逐非 soft pinned 对象 |
| **过期处理** | 过期后可驱逐 | 过期后按普通对象处理 |
| **配置参数** | `default_kv_lease_ttl` | `default_kv_soft_pin_ttl` |

#### Lease 在读写流程中的作用

1. **Get 流程**：
   - Client 调用 `GetReplicaList` 请求对象位置
   - Master 授予 lease（延长对象的 lease_timeout）
   - Master 返回副本列表和 lease 过期时间
   - Client 在 lease 过期内读取数据
   - 如果 lease 过期，读取失败

2. **Put 流程**：
   - Client 调用 `PutStart` 请求分配空间
   - Master 分配空间但不授予 lease（对象尚未完成）
   - Client 写入数据
   - Client 调用 `PutEnd` 标记写入完成
   - Master 授予 lease（对象现在可读）

3. **驱逐流程**：
   - 驱逐线程检查对象的 lease 状态
   - 如果对象有活跃 lease，跳过该对象
   - 如果对象 lease 已过期，可以驱逐

### 代码实践

#### ObjectMetadata 中的 Lease 字段

https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/master_service.h#L852-L854

这段代码定义了 `ObjectMetadata` 中的 lease 相关字段：
- `lease_timeout`：Lease 过期时间（硬 lease，读写操作授予）
- `soft_pin_timeout`：Soft pin 过期时间（软 pin，重要对象标记）
- `hard_pinned`：是否 hard pinned（永久保护，仅可通过 `Remove` 删除）

#### GrantLease 方法

https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/master_service.h#L1014-L1025

`GrantLease` 方法授予 lease，只延长不缩短：
```cpp
lease_timeout = std::max(lease_timeout, now + std::chrono::milliseconds(ttl));
```

#### IsLeaseExpired 方法

https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/master_service.h#L1040-L1049

`IsLeaseExpired` 方法检查 lease 是否过期：
```cpp
return std::chrono::system_clock::now() >= lease_timeout;
```

#### QueryResult 返回 Lease 信息

https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/client_service.h#L44-L57

`QueryResult` 包含副本列表和 lease 过期时间，Client 可以根据 lease 过期时间判断读取操作是否可以完成。

#### Lease 配置参数

https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/master_config.h#L125-L127

Lease 相关的配置参数包括：
- `default_kv_lease_ttl`：默认 lease TTL（毫秒），默认 5000（5 秒）
- `default_kv_soft_pin_ttl`：默认 soft pin TTL（毫秒），默认 1800000（30 分钟）
- `allow_evict_soft_pinned_objects`：是否允许驱逐 soft pinned 对象，默认 true

### 练习题

1. 为什么 Lease 只延长不缩短？如果改为"更新到当前时间 + ttl"会有什么问题？
2. 在分布式环境下，Lease 机制如何避免时钟漂移问题？
3. 如果 Client 在 lease 过期内无法完成读取操作，应该如何处理？
4. Lease 机制与数据库中的锁机制有什么异同？

### 答案

1. **答案**：如果 Lease 采用"更新到当前时间 + ttl"的策略，在某些情况下可能导致 Lease 缩短，使得原本受保护的对象提前失去保护。例如，如果系统时钟被回调，或者多个请求并发刷新 lease，可能导致 lease_timeout 变小。使用 `max` 操作确保 lease 只会延长，不会缩短，提高了系统的鲁棒性。

2. **答案**：Mooncake Store 的 Lease 机制主要在 Master 端维护，Master 的系统时钟作为权威时间源。Client 端收到的 lease_timeout 是 Master 计算的绝对时间，Client 只需要判断当前时间是否超过 lease_timeout。这种设计避免了分布式时钟同步问题，因为所有时间判断都基于 Master 的时钟。

3. **答案**：如果 Client 在 lease 过期内无法完成读取操作，操作会失败，Client 需要重新发起 `GetReplicaList` 请求获取新的 lease。这种设计保证了数据一致性，避免了读取到不完整或不一致的数据。Client 应该根据操作的预期时间调整 lease ttl，或者在 lease 快过期时主动刷新。

4. **答案**：Lease 机制与数据库锁机制都用于保护资源，但设计目标不同：
   - **保护粒度**：Lease 保护对象级别，数据库锁可以保护行、表、页面等多个级别
   - **持有者**：Lease 可以被多个 Client 同时持有（读场景），数据库锁通常是排他的或共享的
   - **生命周期**：Lease 有固定的 TTL，自动过期；数据库锁需要显式释放
   - **竞争处理**：Lease 过期后自动失效，无需等待；数据库锁可能需要等待锁释放或超时

---

## 总结

本讲义覆盖了 Mooncake Store 的四个核心内存管理机制：

1. **内存分配器**：负责高效的内存分配和释放，OffsetBufferAllocator 使用基于 bin 的策略提供 O(1) 分配性能。
2. **分配策略**：负责在分布式环境中选择合适的存储位置，RandomAllocationStrategy 追求最大吞吐量，FreeRatioFirstAllocationStrategy 提供更好的负载均衡。
3. **驱逐策略**：负责在内存不足时选择合适的对象进行驱逐，LRU 策略基于时间局部性原理提高缓存命中率。
4. **Lease 机制**：保证读写一致性，通过时间窗口保护正在被使用的对象。

这些机制协同工作，构成了 Mooncake Store 高效、可靠的内存管理系统，为 LLM 推理工作负载提供了优化的缓存解决方案。

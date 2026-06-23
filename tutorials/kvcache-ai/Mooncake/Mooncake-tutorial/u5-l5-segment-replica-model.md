# Segment 与 Replica 数据模型

## 1. 本讲目标

本讲聚焦 Mooncake Store 控制面里最核心的一组「数据结构」：**Segment / NoFSegment** 与 **Replica**。它们是 Master Service 维护对象元数据、做副本调度与淘汰的基石。学完本讲你应该能够：

1. 说清 **Segment 与 NoFSegment** 各自描述什么、二者的字段为何几乎相同却要分两套，以及 `MountedSegment` / `MountedNoFSegment` 在它们之上挂了什么。
2. 说清 **SegmentStatus**（段的挂载生命周期）与 **ReplicaStatus**（副本的写入生命周期）是两套**互相独立**的状态机，分别归谁管。
3. 掌握 **Replica 的四种类型**（MEMORY / DISK / LOCAL_DISK / NOF_SSD）各自存什么、由谁创建、`Replica` 类如何用一个 `std::variant` 同时承载这四种数据。
4. 画出 **Replica 的状态机**：哪条路径真正在主代码里发生（PROCESSING ⇄ COMPLETE），哪些枚举值只是「定义了但当前主路径不写入」（INITIALIZED / REMOVED / FAILED）。
5. 解释 **`Replica::Descriptor`** 为什么是「可序列化的位置信息」，它如何把 Master 内存里活的对象变成能跨进程/跨 RPC 传递的「地址票据」。
6. 对照源码说清 **MEMORY / LOCAL_DISK / NOF_SSD 三类副本分别如何被 Master 调度分配、又如何被淘汰**。

> 本讲是「数据模型」讲，重在把结构讲透。具体分配策略算法（random / free_ratio_first / cxl）、淘汰算法细节、租约/软硬 pin，会在后续讲义展开。

## 2. 前置知识

本讲默认你已具备（对应依赖讲义）：

- **Store 总体架构（依赖 u5-l1）**：你需要知道 Store 由「Master Service（控制面）」与「Client（数据面）」组成，一笔 `Put` 被拆成 `PutStart`（控制面分配副本）→ 数据面 `TransferWrite` → `PutEnd`（控制面标 COMPLETE）。本讲里的 Segment/Replica 正是 `PutStart`/`GetReplicaList` 这类控制面调用所读写的对象。
- **C++ 基础**：`std::variant`（带标签的联合体）、`std::visit`（按当前持有的类型派发）、`std::holds_alternative`（判断 variant 当前持有哪种类型）。`Replica` 类用 `std::variant` 把四种副本数据塞进同一个成员，这是本讲要重点读的设计。
- **序列化直觉**：Master 是一个独立进程，Client 是另一个进程，二者靠 RPC 通信。Master 内存里「活的对象」（带指针、带分配器弱引用）没法直接传过去，必须先「拍扁」成一份只含值（地址、大小、endpoint 字符串）的描述符——这就是 `Descriptor` 存在的理由。

### 为什么需要 Segment，又为什么需要 Replica？

先用一个比喻建立直觉：

> 想象 Master 是一个仓库管理员。**Segment 是「货架」**——它描述「集群里有一块连续的存储空间，基址在哪、多大、归哪个 Client、用什么传输协议」。**Replica 是「货架上的一个货位」**——它描述「某个 key 的一份数据，落在哪个货架的哪个位置、当前是正在写还是已写完」。

一个 key 可以有**多份 Replica**（冗余），每份落在不同 Segment 上；读写时 Master 先在元数据里找到「这个 key 有哪些 COMPLETE 的 Replica、各自在哪个 Segment 的哪个偏移」，再把这堆「地址票据」交给数据面去真正搬数据。所以 Segment 是「资源池的粒度」，Replica 是「一次分配/一个副本的粒度」。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 | 本讲怎么用 |
|---|---|---|
| mooncake-store/include/types.h | 基础类型：`Segment`、`NoFSegment`、`UUID`、各种枚举与常量 | 看 Segment/NoFSegment 的字段定义 |
| mooncake-store/include/segment.h | `SegmentStatus`、`MountedSegment`/`MountedNoFSegment`、`LocalDiskSegment`、Segment/NoF 两套 Manager | 看段在 Master 侧的挂载生命周期与「挂载态」结构 |
| mooncake-store/include/allocator.h | `ReplicaType` 枚举（MEMORY/DISK/LOCAL_DISK/NOF_SSD/ALL）、`AllocatedBuffer::Descriptor` | 看 ReplicaType 的权威定义、内存描述符的底层形态 |
| mooncake-store/include/replica.h | `ReplicaStatus`、`ReplicateConfig`、`Replica` 类及其内嵌 `Descriptor` | 本讲主战场：副本的状态机、四种数据变体、可序列化描述符 |
| mooncake-store/include/allocation_strategy.h | `AllocationStrategy` 接口与三种实现 | 看副本「诞生」时被赋予什么初始状态 |
| mooncake-store/include/rpc_types.h | `GetReplicaListResponse` 等含 `Replica::Descriptor` 的 RPC 结构 | 看 Descriptor 如何被打包进 RPC 应答 |
| mooncake-store/include/master_service.h | `ObjectMetadata`（一个 key 的全部副本容器） | 看副本如何被存放、遍历、弹出、删除 |
| mooncake-store/src/master_service.cpp | 控制面实现 | 看副本的创建（PROCESSING/COMPLETE）、`mark_complete`/`mark_processing`、淘汰与 offload/promotion |

一个容易混淆的点：**`SegmentStatus` 管的是「货架（段）的挂载生命周期」，`ReplicaStatus` 管的是「货位（副本）的写入生命周期」。** 二者都有 OK/COMPLETE 之类的词，但归属完全不同，本讲 4.1 和 4.4 会分别拆开。

## 4. 核心概念与源码讲解

### 4.1 Segment 与 NoFSegment：两套并行的存储资源池

#### 4.1.1 概念说明

`Segment` 描述「集群里一段连续的存储空间」。它是最朴素的资源单位：Client 启动时把自己贡献的一段内存通过 `MountSegment` 登记给 Master，Master 就得到一个 `Segment` 记录。字段含义：

- `id`：段的全局唯一标识（`UUID = std::pair<uint64_t,uint64_t>`）。
- `name`：**逻辑名**，用于「优先分配」（`preferred_segment` 就是按 name 匹配的）。
- `base` / `size`：段的基址与大小。
- `te_endpoint`：传输引擎的点对点寻址端点（ip:port），数据面靠它找到这段内存。
- `protocol`：传输协议（如 `"tcp"`）。

`NoFSegment` 字段几乎一模一样（`id/name/base/size/te_endpoint`），唯独**没有 `protocol`**。它专门描述「NVMe-oF SSD 段」——即挂到 Master 这边、由 Master 统一分配空间的远端 SSD 池。两套结构分立，是因为内存段和 NoF SSD 段在「归属、传输方式、淘汰策略」上都不同，Master 用两个独立的 `Manager` 分别管它们。

> 名字里的 **NoF = NVMe-over-Fabrics**，一种把 NVMe SSD 通过网络暴露出来的协议。Mooncake 用它做一层「Master 集中管理的 SSD 缓存池」，与「Client 自带本地内存段」是两条数据路径。

#### 4.1.2 核心流程

一个 Segment 在 Master 侧的生命周期：

```
Client 启动 / mount  ──▶  MountedSegment{ segment, status=OK, buf_allocator }
                              │
                              │  （可分配状态：IsSegmentAllocatable == true）
                              │
   drain / unmount  ─────────▶│  status: OK → DRAINING → DRAINED → UNMOUNTING
                              │  （GRACEFULLY_UNMOUNTING 是带定时器的优雅卸载分支）
                              ▼
                          从 mounted_segments_ 移除
```

注意：**段的卸载是「先停止分配、再迁走数据、最后删除」的渐进过程**（DRAINING 还能读、DRAINED 等待卸载），而不是一刀切。这保证了正在读的副本不会因为段突然消失而失败。

#### 4.1.3 源码精读

**Segment 结构定义**——`YLT_REFL` 宏是为序列化框架（ylt struct_pack/struct_json）注册字段，使 Segment 可被序列化（fork 序列化、快照都用得上）：

```cpp
struct Segment {
    UUID id{0, 0};
    std::string name{};  // Logical segment name used for preferred allocation
    uintptr_t base{0};
    size_t size{0};
    // TE p2p endpoint (ip:port) for transport-only addressing
    std::string te_endpoint{};
    std::string protocol;
    Segment() = default;
};
YLT_REFL(Segment, id, name, base, size, te_endpoint, protocol);
```
—— [mooncake-store/include/types.h:L448-L458](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/types.h#L448-L458)

**NoFSegment 结构定义**——字段几乎相同，但缺 `protocol`：

```cpp
struct NoFSegment {
    UUID id{0, 0};
    std::string name{};
    uintptr_t base{0};
    size_t size{0};
    std::string te_endpoint{};
    NoFSegment() = default;
};
YLT_REFL(NoFSegment, id, name, base, size, te_endpoint);
```
—— [mooncake-store/include/types.h:L472-L481](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/types.h#L472-L481)

**SegmentStatus 枚举**——段的挂载生命周期（注意它和 ReplicaStatus 是两回事）：

```cpp
enum class SegmentStatus {
    UNDEFINED = 0,  // Uninitialized
    OK,             // Segment is mounted and available for allocation
    DRAINING,       // Segment remains readable but accepts no new allocations
    DRAINED,        // Segment has been drained and awaits unmount
    GRACEFULLY_UNMOUNTING,  // Readable, no new allocations, timer running
    UNMOUNTING,             // Segment is under unmounting
};
```
—— [mooncake-store/include/segment.h:L21-L28](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/segment.h#L21-L28)

**「挂载态」结构**——`MountedSegment` 在裸 `Segment` 之上多挂了 `status`（生命周期）和 `buf_allocator`（真正干「分配/回收空间」活的分配器）：

```cpp
struct MountedSegment {
    Segment segment;
    SegmentStatus status;
    std::shared_ptr<BufferAllocatorBase> buf_allocator;
};

struct MountedNoFSegment {
    NoFSegment segment;
    UUID client_id;
    SegmentStatus status;
    std::shared_ptr<BufferAllocatorBase> buf_allocator;
};
```
—— [mooncake-store/include/segment.h:L49-L60](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/segment.h#L49-L60)

这两套结构对应两个独立的 Manager（`SegmentManager` 管内存/CXL 段、`NoFSegmentManager` 管 NoF SSD 段），各自维护 `mounted_segments_` 映射与 `AllocatorManager`。这也解释了为什么后面 Replica 的分配会走两条不同入口（见 4.6 节）。

#### 4.1.4 代码实践

**实践目标**：确认「内存段」与「NoF 段」在 Master 里是两套独立的 Manager，且只有 `status == OK` 的段才会进入分配器池。

**操作步骤**：

1. 打开 [segment.h:L399-L463](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/segment.h#L399-L463)（`SegmentManager`）与 [:L465-L535](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/segment.h#L465-L535)（`NoFSegmentManager`），对比二者的私有成员。
2. 注意注释 `// allocator_manager_ only contains allocators whose segment status is OK.`（见 [:L445-L446](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/segment.h#L445-L446)）。

**需要观察的现象**：`AllocatorManager`（分配器池）只收录 `OK` 状态的段。一旦段进入 `DRAINING`，它的分配器会被从池里摘掉（`PrepareUnmountSegment` 干这事），于是新分配不会再落到它头上，但已存在的副本仍可读——这正是「先停分配、再迁数据」机制的落点。

**预期结果**：你能用自己的话说清「为什么 `MountedSegment` 要把 `status` 和 `buf_allocator` 分开存」——`status` 决定「还能不能分」，`buf_allocator` 决定「怎么分」，二者解耦后才能实现渐进式卸载。

#### 4.1.5 小练习与答案

**练习 1**：`Segment` 和 `NoFSegment` 字段几乎一样，为什么不复用同一个结构？

> **参考答案**：因为二者在「归属、传输、淘汰」上属于不同资源池：内存段归贡献它的 Client、用各自的 `protocol` 传输、走 `BatchEvict`；NoF 段是 Master 集中管理的 SSD 池、走专用 NoF 传输、走 `NoFBatchEvict`。字段相似只是巧合，语义与生命周期差异要求两套 Manager 分治。强行复用会让分配/淘汰逻辑里到处出现 `if (is_nof)` 分叉，不如分型清晰。

**练习 2**：`SegmentStatus::DRAINING` 和 `DRAINED` 的区别是什么？为什么需要拆成两步？

> **参考答案**：`DRAINING` = 「不再接受新分配，但已有副本仍可读」，用于把段上现存数据迁走（drain job）；`DRAINED` = 「数据已迁完，等待卸载」。拆两步是为了在「停止写入」和「彻底删除」之间留出把数据搬到别处的时间窗口，避免段一卸载、上面的副本就集体消失导致读失败。

---

### 4.2 Replica：一个对象副本的完整描述

#### 4.2.1 概念说明

`Replica` 是「某个 key 的一份数据副本」在 Master 内存里的表示。它的精妙之处在于：**用一个 `std::variant` 同时容纳四种不同介质的数据**——内存副本、NoF SSD 副本、磁盘副本、本地磁盘副本。这样一份代码就能统一管理「这个副本是什么类型、存在哪、多大」，而不必为每种介质写一个类。

每个 `Replica` 自带：

- `id_`：全局唯一副本 ID（自增原子计数器 `next_id_` 生成）。
- `data_`：`std::variant`，按类型持有不同的 `*ReplicaData`。
- `status_`：`ReplicaStatus`，写入生命周期。
- `refcnt_`：引用计数，标记「是否正被传输/迁移占用」（`is_busy()` 即 `refcnt_ > 0`）。

副本不可拷贝（`delete` 了拷贝构造/赋值），但可移动——移动时会把源对象标记成 `UNDEFINED`，防止析构时重复扣减指标。

#### 4.2.2 核心流程

一个 Replica 从生到灭：

```
分配阶段:  allocation_strategy_->Allocate(...)  ──▶  Replica(status=PROCESSING)   [memory/nof/disk]
           offload 完成（memory→local_disk）      ──▶  Replica(status=COMPLETE)     [local_disk]
写入收尾:  PutEnd / CopyEnd / NotifyPromotionSuccess ──▶ mark_complete()  PROCESSING→COMPLETE
原地改写:  UpsertStart(同尺寸)                   ──▶ mark_processing()  COMPLETE→PROCESSING
移除:      EraseReplicas / PopReplicas / EraseReplicaByID ──▶ 从 replicas_ 容器直接 erase（结构性删除）
```

> 关键诚实结论：**删除副本靠的是「从容器里 erase」，而不是把它置成 `REMOVED` 状态**。`REMOVED`/`FAILED`/`INITIALIZED` 这三个枚举值在当前主代码路径里并不被写入（仅在 Python 绑定与字符串映射里出现）。本讲 4.4 会把「设计上的状态机」和「实际发生的迁移」分开讲清楚。

#### 4.2.3 源码精读

**四种副本数据变体**——每种介质一个 POD-like 结构，描述「这份数据落在哪」：

```cpp
struct MemoryReplicaData {
    std::unique_ptr<AllocatedBuffer> buffer;
};
struct NoFReplicaData {
    std::unique_ptr<AllocatedBuffer> buffer;
};
struct DiskReplicaData {
    std::string file_path;
    uint64_t object_size = 0;
};
struct LocalDiskReplicaData {
    UUID client_id;
    uint64_t object_size = 0;
    std::string transport_endpoint;
};
```
—— [mooncake-store/include/replica.h:L163-L180](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/replica.h#L163-L180)

注意两个内存类副本（MEMORY/NOF）持有 `AllocatedBuffer`（带分配器弱引用的真实缓冲），而两个磁盘类副本只存「定位信息」（`file_path` 或 `client_id + transport_endpoint`）——因为磁盘数据不在 Master 进程里，Master 只记「去哪读」。

**四个重载构造函数**——按介质选构造函数，`id_` 由原子计数器自增：

```cpp
// memory replica constructor
Replica(std::unique_ptr<AllocatedBuffer> buffer, ReplicaStatus status)
    : id_(next_id_.fetch_add(1)),
      data_(MemoryReplicaData{std::move(buffer)}),
      status_(status), refcnt_(0) {}

// nof ssd replica constructor（靠 replica_type 区分 MEMORY / NOF_SSD）
Replica(std::unique_ptr<AllocatedBuffer> buffer, ReplicaStatus status,
        ReplicaType replica_type)
    : id_(next_id_.fetch_add(1)), status_(status), refcnt_(0) {
    if (replica_type == ReplicaType::MEMORY) {
        data_ = MemoryReplicaData{std::move(buffer)};
    } else if (replica_type == ReplicaType::NOF_SSD) {
        data_ = NoFReplicaData{std::move(buffer)};
    } else {
        LOG(ERROR) << "Invalid buffered replica type: " << replica_type;
    }
}

// local disk replica constructor
Replica(UUID client_id, uint64_t object_size,
        std::string transport_endpoint, ReplicaStatus status)
    : id_(next_id_.fetch_add(1)),
      data_(LocalDiskReplicaData{client_id, object_size,
                                 std::move(transport_endpoint)}),
      status_(status), refcnt_(0) { ... }
```
—— [mooncake-store/include/replica.h:L210-L248](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/replica.h#L210-L248)（disk 构造在 [:L229-L237](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/replica.h#L229-L237)）

**类型判定与类型派发**——靠 `std::holds_alternative` 判类型，靠 `ReplicaTypeVisitor` + `std::visit` 把 `data_` 映射成 `ReplicaType`：

```cpp
[[nodiscard]] ReplicaType type() const {
    return std::visit(ReplicaTypeVisitor{}, data_);
}
[[nodiscard]] bool is_memory_replica() const {
    return std::holds_alternative<MemoryReplicaData>(data_);
}
// ... is_nof_replica / is_disk_replica / is_local_disk_replica 同理

struct ReplicaTypeVisitor {
    ReplicaType operator()(const MemoryReplicaData&) const { return ReplicaType::MEMORY; }
    ReplicaType operator()(const NoFReplicaData&) const { return ReplicaType::NOF_SSD; }
    ReplicaType operator()(const DiskReplicaData&) const { return ReplicaType::DISK; }
    ReplicaType operator()(const LocalDiskReplicaData&) const { return ReplicaType::LOCAL_DISK; }
};
```
—— [mooncake-store/include/replica.h:L331-L365](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/replica.h#L331-L365) 与 [:L452-L465](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/replica.h#L452-L465)

**引用计数**——标记副本是否正被占用，淘汰/迁移时要避开 busy 的副本：

```cpp
[[nodiscard]] bool is_busy() const { return refcnt_.load() > 0; }
void inc_refcnt() { refcnt_.fetch_add(1); }
void dec_refcnt() { refcnt_.fetch_sub(1); }
```
—— [mooncake-store/include/replica.h:L325-L329](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/replica.h#L325-L329) 与 [:L444-L448](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/replica.h#L444-L448)

例如 offload 任务会 `inc_refcnt()`（[:L1959](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L1959)），完成后再 `dec_refcnt()`（[:L3477](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L3477)），确保 offload 进行中该 MEMORY 副本不会被淘汰。

#### 4.2.4 代码实践

**实践目标**：在源码里追踪「一个 MEMORY Replica 是怎么被构造出来的」，确认它诞生时的初始状态。

**操作步骤**：

1. 打开 [allocation_strategy.h:L232-L244](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/allocation_strategy.h#L232-L244)（`RandomAllocationStrategy::Allocate` 单段快路径）。
2. 看 `replicas.emplace_back(std::move(buffer), ReplicaStatus::PROCESSING, replica_type);` 这一行。

**需要观察的现象**：副本诞生时直接是 `PROCESSING`，而不是 `INITIALIZED`。再对比 `FreeRatioFirstAllocationStrategy`（[:L419-L420](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/allocation_strategy.h#L419-L420)）和 `CxlAllocationStrategy`（[:L587-L588](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/allocation_strategy.h#L587-L588)），都是 `PROCESSING`。

**预期结果**：你能说清「`INITIALIZED` 在枚举注释里写作『Space allocated, waiting for write』，但当前三种分配策略都直接给 `PROCESSING`，主路径跳过了 INITIALIZED」。这是源码阅读型实践——以代码为准，而非以注释为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `MemoryReplicaData` 持有 `unique_ptr<AllocatedBuffer>`，而 `LocalDiskReplicaData` 只存 `client_id + transport_endpoint`？

> **参考答案**：MEMORY 副本的数据**就在 Master 进程能管的内存里**（通过段分配器分配出的 buffer），所以持有 buffer 句柄，能拿到真实地址/偏移。LOCAL_DISK 副本的数据在**某个 Client 的本地磁盘上**，Master 进程根本碰不到那块磁盘的字节，只能记录「去哪个 client、用什么 endpoint 读」这种定位信息。这是「数据在本地」与「数据在远端」的根本差异。

**练习 2**：`Replica` 为什么禁用拷贝、只允许移动？

> **参考答案**：因为副本持有 `unique_ptr`（独占 buffer）和原子 `refcnt_`，且析构时要按介质扣减全局指标（`dec_allocated_file_size`）。如果允许拷贝，会出现两个对象指向同一 buffer、析构时重复扣指标的问题。移动语义把所有权整体转移，并把源标记成 `UNDEFINED`（见 [:L266-L274](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/replica.h#L266-L274)），保证「一份副本、一份指标、一个有效状态」。

---

### 4.3 ReplicaType 与四种副本类型

#### 4.3.1 概念说明

`ReplicaType` 枚举定义在 `allocator.h`（不是 `replica.h`——注意别找错文件）：

```cpp
enum class ReplicaType {
    MEMORY,      // Memory replica
    DISK,        // Disk replica
    LOCAL_DISK,  // Local disk replica
    NOF_SSD,     // Nvme-oF SSD replica
    ALL,         // All memory and NoF replicas in put finalize path
};
```
—— [mooncake-store/include/allocator.h:L21-L27](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/allocator.h#L21-L27)

四种介质副本的含义与差别（**DISK 与 LOCAL_DISK 最容易混，务必分清**）：

| 类型 | 数据实际在哪 | 由谁创建 | 定位字段 | 典型场景 |
|---|---|---|---|---|
| `MEMORY` | 某段分配出的内存 buffer | `PutStart` 时分配策略创建（PROCESSING） | buffer 的地址/偏移/endpoint | 热数据，主读写路径 |
| `NOF_SSD` | Master 集中管的 NVMe-oF SSD 池 | `PutStart` 时 `nof_replica_num>0` 创建（PROCESSING） | buffer 的地址/偏移/endpoint | 一层 Master 管的 SSD 缓存 |
| `DISK` | Master 进程所在文件系统的本地文件（`root_fs_dir_`） | `PutStart` 且 `use_disk_replica_` 时创建（PROCESSING） | `file_path` | 较老的「Master 本地磁盘副本」特性 |
| `LOCAL_DISK` | **某个 Client 的本地磁盘** | MEMORY 副本被淘汰时 offload 生成（**直接 COMPLETE**） | `client_id + transport_endpoint` | 内存换出的 L2 层，命中可 promote 回内存 |

关键区分：**`DISK` 是「Master 自己机器上的磁盘文件」（Master 管文件路径）；`LOCAL_DISK` 是「别的 Client 节点上的磁盘」（Master 只记是哪个 client、怎么连）。** 二者虽然都叫 disk，但归属和访问方式完全不同。`ALL` 不是一种真实介质，而是 `PutEnd` 收尾时的「批量谓词」，表示「把内存副本和 NoF 副本一起标 COMPLETE」（见 4.4.3 的 PutEnd 谓词）。

`ReplicateConfig` 决定一次 `PutStart` 要几份哪种副本：`replica_num` 控制内存副本数，`nof_replica_num` 控制 NoF 副本数：

```cpp
struct ReplicateConfig {
    size_t replica_num{1};
    size_t nof_replica_num{0};
    bool with_soft_pin{false};
    bool with_hard_pin{false};
    std::vector<std::string> preferred_segments{};
    std::vector<std::string> preferred_nof_segments{};
    bool prefer_alloc_in_same_node{false};
    ObjectDataType data_type{ObjectDataType::UNKNOWN};
    std::optional<std::vector<std::string>> group_ids{};
    ...
};
```
—— [mooncake-store/include/replica.h:L81-L106](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/replica.h#L81-L106)

`DetermineReplicaWriteMode` 根据这两个数把写入模式分成三档，影响「分不到全量副本时是否算失败」：

```cpp
inline ReplicaWriteMode DetermineReplicaWriteMode(const ReplicateConfig& config) {
    if (config.replica_num == 1 && config.nof_replica_num == 1) {
        return ReplicaWriteMode::FLEXIBLE_DUAL_REPLICA;   // 1 内存 + 1 NoF，灵活
    }
    if (config.replica_num > 1 || config.nof_replica_num > 1) {
        return ReplicaWriteMode::RELIABLE_MULTI_REPLICA;  // 多副本，严格
    }
    return ReplicaWriteMode::SINGLE_REPLICA;              // 单副本
}
```
—— [mooncake-store/include/replica.h:L146-L161](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/replica.h#L146-L161)

#### 4.3.2 核心流程

`PutStart` 里「内存副本」与「NoF 副本」走两条独立的分配入口（同一函数内先后两次调用分配策略，分别用不同的 `AllocatorManager`）：

```
AllocateAndInsertMetadata(config):
  ├─ if replica_num > 0:
  │     segment_manager_.getAllocatorAccess()  ──▶ Allocate(..., ReplicaType::MEMORY 默认)
  │     → 得到若干 PROCESSING 的 MEMORY 副本
  ├─ if nof_replica_num > 0 (且 USE_NOF 且有 nof 段):
  │     nof_segment_manager_.getAllocatorAccess() ──▶ Allocate(..., ReplicaType::NOF_SSD)
  │     → 得到若干 PROCESSING 的 NOF_SSD 副本
  ├─ if use_disk_replica_:
  │     直接 new 一个 DISK 副本(file_path, PROCESSING)
  └─ 把所有副本塞进 ObjectMetadata，存入 metadata shard
```

#### 4.3.3 源码精读

**内存副本分配入口**——用 `segment_manager_` 的分配器池，默认 `ReplicaType::MEMORY`：

```cpp
if (config.replica_num > 0) {
    ScopedAllocatorAccess allocator_access = segment_manager_.getAllocatorAccess();
    const auto& allocator_manager = allocator_access.getAllocatorManager();
    ...
    auto allocation_result = allocation_strategy_->Allocate(
        allocator_manager, value_length, config.replica_num, preferred_segments);
    ...
    replicas = std::move(allocation_result.value());
}
```
—— [mooncake-store/src/master_service.cpp:L1650-L1681](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L1650-L1681)

**NoF 副本分配入口**——用 `nof_segment_manager_` 的分配器池，显式传 `ReplicaType::NOF_SSD`，且受 `#ifdef USE_NOF` 编译开关保护：

```cpp
#ifdef USE_NOF
    if (config.nof_replica_num > 0 &&
        nof_segment_manager_.getMountedSegmentCount() > 0) {
        ScopedAllocatorAccess allocator_access = nof_segment_manager_.getAllocatorAccess();
        const auto& allocator_manager = allocator_access.getAllocatorManager();
        std::vector<std::string> preferred_segments = config.preferred_nof_segments;
        auto allocation_result = allocation_strategy_->Allocate(
            allocator_manager, value_length, config.nof_replica_num,
            preferred_segments, std::set<std::string>(), ReplicaType::NOF_SSD);
        ...
    }
#endif
```
—— [mooncake-store/src/master_service.cpp:L1683-L1715](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L1683-L1715)

**DISK 副本创建**——`use_disk_replica_` 开关打开时，按 key 解析出文件路径，直接造一个 DISK 副本（PROCESSING）：

```cpp
if (use_disk_replica_) {
    std::string file_path = ResolvePathFromKey(key, root_fs_dir_, cluster_id_);
    replicas.emplace_back(file_path, value_length, ReplicaStatus::PROCESSING);
}
```
—— [mooncake-store/src/master_service.cpp:L1741-L1746](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L1741-L1746)

**`Allocate` 的 best-effort 语义**——接口注释明确「凑不齐请求副本数就尽量多分，至少分到 1 个才算成功」：

```cpp
/**
 * ... best-effort semantics: if the full requested replica count cannot be
 * satisfied, the method will allocate as many replicas as possible across
 * different segments. For each slice, replicas are guaranteed to be placed on
 * different segments to ensure redundancy.
 * ...
 * - On failure: ErrorCode::NO_AVAILABLE_HANDLE if no replicas can be allocated ...
 */
virtual tl::expected<std::vector<Replica>, ErrorCode> Allocate(
    ..., const ReplicaType replica_type = ReplicaType::MEMORY) = 0;
```
—— [mooncake-store/include/allocation_strategy.h:L122-L167](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/allocation_strategy.h#L122-L167)

这意味着 MEMORY 与 NOF_SSD 都遵循同一套 best-effort 分配（只是用的 `AllocatorManager` 不同），而 DISK 与 LOCAL_DISK 不走分配策略、用各自的方式产生。

#### 4.3.4 代码实践

**实践目标**：区分 `DISK` 与 `LOCAL_DISK` 两种「磁盘副本」的构造入口与归属。

**操作步骤**：

1. 找 DISK 构造入口：`replicas.emplace_back(file_path, value_length, ReplicaStatus::PROCESSING)`（[:L1741-L1746](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L1741-L1746)）。注意它由 `use_disk_replica_`（Master 配置）触发，文件路径由 `ResolvePathFromKey` 在 **Master 的 `root_fs_dir_`** 下解析。
2. 找 LOCAL_DISK 构造入口：`Replica replica(client_id, metadata.data_size, metadata.transport_endpoint, ReplicaStatus::COMPLETE)`（[:L3484-L3488](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L3484-L3488)）。注意它由 **offload 完成回调**触发，且 `client_id` 是**远端 Client**、状态直接是 COMPLETE。

**需要观察的现象**：两者诞生语境完全不同——DISK 在 `PutStart` 主写入路径里、和 MEMORY 副本一起产生；LOCAL_DISK 在「内存副本被淘汰、offload 到某 Client 本地磁盘」的异步回调里产生，且一出生就 COMPLETE（因为数据是 offload 过去的、早已写好）。

**预期结果**：你能用一句话区分——「DISK = Master 本机磁盘文件副本（PutStart 时产生，PROCESSING）；LOCAL_DISK = 远端 Client 本地磁盘副本（offload 时产生，直接 COMPLETE）」。

#### 4.3.5 小练习与答案

**练习 1**：`ReplicaType::ALL` 不是一种介质，那它是干什么用的？

> **参考答案**：它是 `PutEnd` 收尾时的「批量谓词参数」。`PutEnd` 收到一个 `ReplicaType`，用它在 `VisitReplicas` 里选出要标 COMPLETE 的副本：传 `ALL` 表示「内存副本和 NoF 副本一起标」（但要排除 handle 已失效的），传 `MEMORY` 只标内存副本，传 `NOF_SSD` 只标 NoF 副本。详见 4.4.3 的 PutEnd 谓词代码。所以 `ALL` 是「作用域选择器」，不是真实存储介质。

**练习 2**：为什么 NoF 副本的分配代码要被 `#ifdef USE_NOF` 包起来？

> **参考答案**：NoF（NVMe-oF）依赖特定的 SSD 硬件与运行时，不是所有部署都有。用编译开关 `USE_NOF` 控制，可以让没装 NoF 的构建直接裁掉这段代码（[:L1809-L1815](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L1809-L1815) 里若未定义 USE_NOF 却请求 `nof_replica_num>0` 会直接返回 `INVALID_PARAMS`，提示「nof_pool_disabled」）。这是「可选特性按编译开关裁剪」的常见做法。

---

### 4.4 ReplicaStatus 状态机

#### 4.4.1 概念说明

`ReplicaStatus` 描述一个副本的**写入生命周期**：

```cpp
enum class ReplicaStatus {
    UNDEFINED = 0,  // Uninitialized
    INITIALIZED,    // Space allocated, waiting for write
    PROCESSING,     // Write in progress
    COMPLETE,       // Write complete, replica is available
    REMOVED,        // Replica has been removed
    FAILED,         // Failed state (can be used for reassignment)
};
```
—— [mooncake-store/include/replica.h:L51-L58](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/replica.h#L51-L58)

注释描述的是**设计意图**（一个理想状态机），但「以代码为准」地看，当前主路径实际只用到其中一部分。本讲特别强调把二者分清，避免你被注释误导。

#### 4.4.2 核心流程

**实际发生的迁移（以代码为准）**：

```
                 ┌──────────────────────────────┐
   内存/NoF/DISK  │  PROCESSING  ←──mark_processing()──┐
   分配策略创建 ──▶│  (写入中)    │                    │ UpsertStart
                 └──────┬───────┘                    │ (同尺寸原地改写)
                        │ mark_complete()             │
                        │ (PutEnd/CopyEnd/            │
                        │  NotifyPromotionSuccess)    │
                        ▼                             │
                 ┌──────────────┐─────────────────────┘
   LOCAL_DISK    │   COMPLETE   │
   offload 创建 ─▶│  (可读，Get 唯一认它)
                 └──────┬───────┘
                        │ EraseReplicas / PopReplicas / EraseReplicaByID
                        ▼
                  从 replicas_ 容器 erase（结构性删除）
```

状态迁移方法只有两个，且各自有严格的「前置状态」校验：

```cpp
void mark_complete() {
    if (status_ == ReplicaStatus::PROCESSING) {
        status_ = ReplicaStatus::COMPLETE;
    } else if (status_ == ReplicaStatus::COMPLETE) {
        LOG(WARNING) << "Replica already marked as complete";
    } else {
        LOG(ERROR) << "Invalid replica status: " << status_;
    }
}

void mark_processing() {
    if (status_ == ReplicaStatus::COMPLETE) {
        status_ = ReplicaStatus::PROCESSING;
    } else {
        LOG(ERROR) << "Cannot mark_processing from status: " << status_;
    }
}
```
—— [mooncake-store/include/replica.h:L426-L442](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/replica.h#L426-L442)

也就是说：

- `mark_complete()` 只接受 `PROCESSING → COMPLETE`；对 `COMPLETE` 幂等（打 WARNING），对其他状态报错。
- `mark_processing()` 只接受 `COMPLETE → PROCESSING`（用于 Upsert 同尺寸原地改写，先把可读副本退回写入态，避免改写中途被读到脏数据）。

**关于 INITIALIZED / REMOVED / FAILED（重要诚实结论）**：

- `INITIALIZED`：枚举注释写作「Space allocated, waiting for write」，但全仓搜索 `ReplicaStatus::INITIALIZED`，它只出现在 [replica.h:L67](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/replica.h#L67)（字符串映射）和 Python 绑定 [store_py.cpp:L1764](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/store_py.cpp#L1764)。**主分配路径直接用 `PROCESSING` 创建副本，跳过了 INITIALIZED。**
- `REMOVED` / `FAILED`：同样只在字符串映射（[replica.h:L70-L71](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/replica.h#L70-L71)）和 Python 绑定（[store_py.cpp:L1767-L1768](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/store_py.cpp#L1767-L1768)）出现，`master_service.cpp` 里**没有任何地方把副本置成 REMOVED 或 FAILED**。
- **删除副本靠的是「从容器里 erase」**（`EraseReplicas` / `PopReplicas` / `EraseReplicaByID`），而不是先置 `REMOVED` 再清理。

因此画状态机时，「设计态」有 6 个值，但「主代码实际迁移」只有 `PROCESSING ⇄ COMPLETE` 两个方向加上容器级删除。把这三个保留态当作「为未来/外部工具预留的语义」，而非当前活跃迁移，是准确的读法。

#### 4.4.3 源码精读

**PutEnd 把副本标 COMPLETE**——`VisitReplicas(谓词, mark_complete)`，谓词按 `ReplicaType` 选出要收尾的副本（注意 `ALL` 的语义）：

```cpp
metadata.VisitReplicas(
    [replica_type](const Replica& replica) {
        if (replica_type == ReplicaType::ALL) {
            return (replica.is_memory_replica() && !replica.has_invalid_mem_handle()) ||
                   (replica.is_nof_replica() && !replica.has_invalid_nof_handle());
        }
        if (replica_type == ReplicaType::MEMORY) {
            return replica.is_memory_replica() && !replica.has_invalid_mem_handle();
        }
        if (replica_type == ReplicaType::NOF_SSD) {
            return replica.is_nof_replica() && !replica.has_invalid_nof_handle();
        }
        return replica.type() == replica_type;
    },
    [](Replica& replica) { replica.mark_complete(); });
```
—— [mooncake-store/src/master_service.cpp:L1930-L1948](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L1930-L1948)

注意 `has_invalid_mem_handle()`（[replica.h:L367-L373](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/replica.h#L367-L373)）会排除「分配器已失效（段已卸载）」的副本，避免把一个无效副本标成可读。

**UpsertStart 把 COMPLETE 退回 PROCESSING**——同尺寸原地改写时，先把已可读的副本退回写入态，防止改写中途被 Get 读到半新半旧的数据：

```cpp
// Mark COMPLETE → PROCESSING so readers won't see stale data
// mid-transfer.  The key becomes unreadable until UpsertEnd.
metadata.VisitReplicas(
    &Replica::fn_is_completed,
    [](Replica& replica) { replica.mark_processing(); });
```
—— [mooncake-store/src/master_service.cpp:L2304-L2308](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L2304-L2308)

**Promotion 成功也走 mark_complete**——L2→L1 提升时，先 `PromotionAllocStart` 分配一个 PROCESSING 的 MEMORY 副本，client 从 LOCAL_DISK 读数据回写到这个 MEMORY 副本，完成后再精确地按 task 记录的 `alloc_id` 标 COMPLETE（不笼统地标「第一个 PROCESSING」，以防并发 Put 误标别人的副本）：

```cpp
Replica* staged = metadata.GetReplicaByID(task_it->second.alloc_id);
if (staged != nullptr && staged->is_memory_replica() && staged->is_processing()) {
    staged->mark_complete();
    committed = true;
}
```
—— [mooncake-store/src/master_service.cpp:L3843-L3848](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L3843-L3848)

**副本的「容器级删除」**——`ObjectMetadata` 用 `std::vector<Replica> replicas_` 存一个 key 的全部副本，删除就是从 vector 里弹出/擦除（按谓词或按 ID）：

```cpp
std::vector<Replica> PopReplicas(const std::function<bool(const Replica&)>& pred_fn) {
    auto partition_point = std::partition(replicas_.begin(), replicas_.end(),
        [pred_fn](const Replica& replica) { return !pred_fn(replica); });
    std::vector<Replica> popped_replicas;
    if (partition_point != replicas_.end()) { ... }
    return popped_replicas;
}
size_t EraseReplicas(const std::function<bool(const Replica&)>& pred_fn) {
    auto erased_replicas = PopReplicas(pred_fn);
    return erased_replicas.size();
}
bool EraseReplicaByID(const ReplicaID& id) {
    auto num_erased = EraseReplicas([&id](const Replica& r) { return r.id() == id; });
    return num_erased > 0;
}
```
—— [mooncake-store/include/master_service.h:L874-L981](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_service.h#L874-L981)（`replicas_` 声明在 [:L1102](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_service.h#L1102)）

被弹出的副本进入 `PopReplicas` 的返回值（往往送进 `DiscardedReplicas` 做延迟释放，避免还有 reader 持有地址），其析构会按介质扣减指标——这就是删除的全部机制，**没有 `REMOVED` 状态参与**。

#### 4.4.4 代码实践

**实践目标**：用 `git grep` / 全仓搜索，亲自验证「INITIALIZED / REMOVED / FAILED 在主路径是否被写入」，养成「以代码为准」的读源码习惯。

**操作步骤**：

1. 在仓库根目录执行（仅阅读，勿改源码）：`grep -rn "ReplicaStatus::INITIALIZED\|ReplicaStatus::REMOVED\|ReplicaStatus::FAILED" --include=*.cpp --include=*.h`。
2. 数一数命中点分别落在哪些文件：你应该只在 `replica.h`（字符串映射）、`store_py.cpp`（Python 枚举绑定）看到它们，**不会**在 `master_service.cpp` 看到任何「置成这三个状态」的赋值。
3. 再搜 `mark_complete` 和 `mark_processing`，确认真正的状态迁移只有这两个方向。

**需要观察的现象**：枚举有 6 个值，但真正驱动迁移的写入点只有 `PROCESSING`（创建）、`mark_complete()`、`mark_processing()` 三处来源；INITIALIZED/REMOVED/FAILED 在 C++ 主逻辑里是「未被写入的保留态」。

**预期结果**：你得出结论——「本讲规格里提到的 INITIALIZED→PROCESSING→COMPLETE 主链，在当前代码里实际是 PROCESSING→COMPLETE（创建即 PROCESSING）；FAILED/REMOVED 是预留语义，删除走容器 erase」。这是一个「注释/规格 vs 代码」的典型差异，记录下来比盲目相信注释更有价值。

#### 4.4.5 小练习与答案

**练习 1**：`mark_complete()` 为什么对「已经是 COMPLETE」的情况只打 WARNING 而不报错？

> **参考答案**：因为重复标 COMPLETE 是「无害的幂等行为」——副本已经可读，再标一次不会改变任何对外可见性。把它当 WARNING 记录（便于排查「谁重复调了 PutEnd」），而不是 ERROR 中断流程，体现了「宽容输入、严格状态」的设计：状态机本身保证不会从 COMPLETE 走错，所以重复调用可以安全忽略。

**练习 2**：如果未来要给副本加「失败重分配」能力（FAILED 态），现有代码哪些地方需要改？

> **参考答案**：需要 (a) 在 `Replica` 上加一个 `mark_failed()` 方法（类似 `mark_complete`，带前置状态校验）；(b) 在写入/传输失败处（如 `PutEnd` 发现某副本传输失败、或 transfer 回调报错）把对应副本置 FAILED；(c) 让 `GetReplicaList` 的 `fn_is_completed` 谓词自然排除 FAILED（因为它只挑 COMPLETE，已经满足）；(d) 加一条「FAILED 副本可被重分配」的清理路径。这正是枚举注释里 `FAILED // can be used for reassignment` 预留的扩展点——目前还没接上。

---

### 4.5 Replica::Descriptor：可序列化的位置信息

#### 4.5.1 概念说明

`Replica` 是 Master **进程内存里活的对象**：持有 `unique_ptr<AllocatedBuffer>`（带分配器弱引用 `weak_ptr`）、原子 refcnt、不可拷贝。这种对象**没法跨进程/跨 RPC 传**。于是 `Replica` 提供一个内嵌结构 `Descriptor`——把副本「拍扁」成一份**只含值**（副本 ID、定位信息、状态）的可序列化快照。

`Descriptor` 的核心是又一个 `std::variant`，这次装的是四种「定位描述符」：

```cpp
struct Descriptor {
    ReplicaID id;
    std::variant<MemoryDescriptor, NoFDescriptor, DiskDescriptor,
                 LocalDiskDescriptor> descriptor_variant;
    ReplicaStatus status;
    YLT_REFL(Descriptor, id, descriptor_variant, status);
    // ... 一组 is_xxx() / get_xxx_descriptor() 辅助方法
};
```
—— [mooncake-store/include/replica.h:L467-L569](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/replica.h#L467-L569)

四种定位描述符与四种副本数据一一对应，但只保留「定位所需的最小字段」：

```cpp
struct MemoryDescriptor  { AllocatedBuffer::Descriptor buffer_descriptor; };  // 内存副本
struct NoFDescriptor      { AllocatedBuffer::Descriptor buffer_descriptor; };  // NoF 副本
struct DiskDescriptor     { std::string file_path{}; uint64_t object_size = 0; };       // DISK
struct LocalDiskDescriptor{ UUID client_id; uint64_t object_size = 0;
                            std::string transport_endpoint; };                            // LOCAL_DISK
```
—— [mooncake-store/include/replica.h:L182-L203](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/replica.h#L182-L203)

其中 `AllocatedBuffer::Descriptor`（内存/NoF 副本的底层定位）是数据面真正拿来寻址的最小集合：大小、缓冲地址、协议、传输端点。

```cpp
struct Descriptor {
    uint64_t size_;
    uintptr_t buffer_address_;
    std::string protocol_;
    std::string transport_endpoint_;
    YLT_REFL(Descriptor, size_, buffer_address_, protocol_, transport_endpoint_);
};
```
—— [mooncake-store/include/allocator.h:L77-L84](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/allocator.h#L77-L84)

> 这正好印证 u5-l1 的关键结论：**控制面的输出（`Replica::Descriptor`）就是数据面的输入**。`PutStart`/`GetReplicaList` 返回一堆 Descriptor，`TransferWrite`/`TransferRead` 拿着 Descriptor 里的地址和 endpoint 用 TE 搬数据。Descriptor 是两个平面唯一的接缝。

#### 4.5.2 核心流程

```
Master 内存里的 Replica（活对象）
        │  replica.get_descriptor()
        ▼
Replica::Descriptor（值快照：id + 定位 variant + status）
        │  被 emplace 进 RPC 应答（GetReplicaListResponse / CopyStartResponse ...）
        ▼
经 RPC 序列化（YLT_REFL 注册字段）传给 Client 进程
        │
        ▼
Client 用 descriptor_variant 里的地址/endpoint 发起 TE 传输
```

`get_descriptor()` 按 `data_` 当前持有的类型，逐分支填出对应的定位描述符：

```cpp
inline Replica::Descriptor Replica::get_descriptor() const {
    Replica::Descriptor desc;
    desc.id = id_;
    desc.status = status_;
    if (is_memory_replica()) {
        const auto& mem_data = std::get<MemoryReplicaData>(data_);
        MemoryDescriptor mem_desc;
        if (mem_data.buffer) {
            mem_desc.buffer_descriptor = mem_data.buffer->get_descriptor();
        } else { ... LOG(ERROR) ...; }
        desc.descriptor_variant = std::move(mem_desc);
    } else if (is_nof_replica()) { ... }        // NoFDescriptor
    else if (is_disk_replica()) { ... }          // DiskDescriptor: file_path + object_size
    else if (is_local_disk_replica()) { ... }    // LocalDiskDescriptor: client_id + endpoint
    return desc;
}
```
—— [mooncake-store/include/replica.h:L585-L630](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/replica.h#L585-L630)

#### 4.5.3 源码精读

**Descriptor 进 RPC 应答**——`GetReplicaListResponse` 就是一组 Descriptor 加一个 lease TTL，`YLT_REFL` 注册字段以便序列化框架编解码：

```cpp
struct GetReplicaListResponse {
    std::vector<Replica::Descriptor> replicas;
    uint64_t lease_ttl_ms;
    ...
};
YLT_REFL(GetReplicaListResponse, replicas, lease_ttl_ms);
```
—— [mooncake-store/include/rpc_types.h:L32-L42](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/rpc_types.h#L32-L42)

复制（Copy）、迁移（Move）、提升（Promotion）等 RPC 应答也都以 Descriptor 为载荷，因为它们本质上都是「告诉 Client 一份副本在哪」：

```cpp
struct CopyStartResponse {
    Replica::Descriptor source;
    std::vector<Replica::Descriptor> targets;
};                                              // 复制：源 + 目标副本位置
struct PromotionAllocStartResponse {
    Replica::Descriptor memory_descriptor;
};                                              // L2→L1 提升：staged 的 PROCESSING MEMORY 副本
```
—— [mooncake-store/include/rpc_types.h:L89-L102](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/rpc_types.h#L89-L102)

**段名反查**——`get_segment_names()` 让 Master 能从副本反查它落在哪个段（用于 offload 时找段对应的 client），MEMORY/NoF 副本返回段名，磁盘类副本返回空：

```cpp
inline std::vector<std::optional<std::string>> Replica::get_segment_names() const {
    if (is_memory_replica()) {
        const auto& mem_data = std::get<MemoryReplicaData>(data_);
        std::vector<std::optional<std::string>> segment_names;
        if (mem_data.buffer && mem_data.buffer->isAllocatorValid()) {
            segment_names.push_back(mem_data.buffer->getSegmentName());
        } else {
            segment_names.push_back(std::nullopt);
        }
        return segment_names;
    } else if (is_nof_replica()) { ... }
    return std::vector<std::optional<std::string>>();
}
```
—— [mooncake-store/include/replica.h:L632-L654](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/replica.h#L632-L654)

#### 4.5.4 代码实践

**实践目标**：跟随一条「Descriptor 从产生到被消费」的链路，理解它为什么是「最小必要信息」。

**操作步骤**：

1. 起点：[master_service.cpp:L1753-L1755](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L1753-L1755)，`PutStart` 里 `const auto desc = replica.get_descriptor();` 把刚分配的活 Replica 拍扁成 Descriptor。
2. 传输：这些 Descriptor 作为 `PutStart` 的返回值（`std::vector<Replica::Descriptor>`）经 RPC 回到 Client。
3. 终点：在数据面 `TransferWrite(replica_descriptor, slices)`（见 u5-l1 的 client_service.cpp）里，Descriptor 被读出地址/endpoint，交给 TE。
4. 对照 [allocator.h:L77-L84](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/allocator.h#L77-L84) 的 `AllocatedBuffer::Descriptor`，确认它恰好是 TE 寻址所需的最小集合（size + address + protocol + endpoint），不多不少。

**需要观察的现象**：Descriptor 里**没有** `weak_ptr`、没有 `refcnt`、没有分配器对象——这些都是 Master 进程内部的、不可跨进程的活状态。拍扁时只保留了「Client 端真正需要的定位信息」。

**预期结果**：你能解释「为什么 Replica 和 Replica::Descriptor 要分成两个结构」——前者是 Master 内部的富对象（带所有权、带并发控制），后者是跨进程的瘦快照（只带定位）。这是控制面/数据面解耦在数据结构层面的体现。**待本地验证**：若本地能跑 Store，可在 `get_descriptor()` 临时加一行日志（仅调试，勿提交），观察一次 `PutStart` 返回的 Descriptor 里 `buffer_address_` 与 `transport_endpoint_` 的实际值，再对照数据面传输目标是否一致。

#### 4.5.5 小练习与答案

**练习 1**：`Replica::Descriptor` 的 `descriptor_variant` 为什么还要再用一个 `std::variant`，而不是直接把四种定位字段都平铺进 Descriptor？

> **参考答案**：因为不同介质副本的定位信息**结构不同**（内存/NoF 是 buffer descriptor，DISK 是 file_path，LOCAL_DISK 是 client_id+endpoint）。平铺会造成「每种介质只用其中几个字段、其余空着」的浪费，且无法在类型层面区分。用 variant 既省空间（同一时刻只存一种），又让 `is_memory_replica()`/`get_memory_descriptor()` 这类访问器能在编译期做类型安全派发，拿错类型直接抛 `runtime_error` 而不是返回垃圾值。

**练习 2**：Client 拿到一个 `Replica::Descriptor` 后，如果它对应的源 Replica 在 Master 端已经被淘汰了，会发生什么？

> **参考答案**：Descriptor 是一份**快照**，它本身不会因为源 Replica 被删而自动失效——Client 手里这份地址票据还在。这就是为什么 `GetReplicaList` 要在返回前**授予 lease**（见 u5-l1）：lease 期间 Master 保证不删/不淘汰该对象，从而保证 Client 手里的 Descriptor 在数据传输完成前一直有效。若读太慢导致 lease 过期，Client 会判 `LEASE_EXPIRED` 失败，宁可重试也不用可能已失效的地址。Descriptor 的「快照性」与 lease 的「有效期」是配套设计。

---

### 4.6 三类副本的调度与淘汰（综合）

#### 4.6.1 概念说明

把 4.3 的「创建」和 4.4 的「状态」串起来，看 MEMORY / LOCAL_DISK / NOF_SSD 三类副本在 Master 眼里的完整「调度—淘汰」轨迹。这是本讲实践任务要求解释的核心。

#### 4.6.2 核心流程

```
MEMORY 副本：
  调度  PutStart → AllocateAndInsertMetadata → segment_manager_ 池 → Allocate(MEMORY) → PROCESSING
        PutEnd → mark_complete() → COMPLETE（Get 可见）
  淘汰  EvictionThreadFunc: global_mem_used_ratio > high_watermark 或 need_mem_eviction_
        → BatchEvict → PopReplicas 弹出 MEMORY 副本
        → （可选）offload 到某 Client 本地磁盘，生成 LOCAL_DISK 副本

LOCAL_DISK 副本（不是 PutStart 调度的，而是 MEMORY 淘汰的产物）：
  产生  offload 完成回调 → new Replica(client_id, size, endpoint, COMPLETE) → 直接可读
  提升  Get 命中「只有 LOCAL_DISK」→ PromotionAllocStart 分配 PROCESSING 的 MEMORY 副本
        → Client 从 LOCAL_DISK 读回写到 MEMORY → NotifyPromotionSuccess → mark_complete()

NOF_SSD 副本：
  调度  PutStart（nof_replica_num>0, USE_NOF, 有 nof 段）→ nof_segment_manager_ 池 → Allocate(NOF_SSD) → PROCESSING
        PutEnd(ALL 或 NOF_SSD) → mark_complete() → COMPLETE
  淘汰  EvictionThreadFunc: global_nof_used_ratio > nof_high_watermark 或 need_nof_eviction_
        → NoFBatchEvict（独立的 NoF 淘汰路径，与内存淘汰分开）
```

三类副本的关键差异：MEMORY 与 NOF_SSD 在 `PutStart` 时调度（同一函数两入口），LOCAL_DISK 在淘汰 offload 时产生；MEMORY 走 `BatchEvict`、NOF_SSD 走 `NoFBatchEvict`、LOCAL_DISK 靠 promotion 提升回内存（不直接被「淘汰」，而是其持有 client 失联时随 client 过期清理）。

#### 4.6.3 源码精读

**内存淘汰触发**——后台线程监控内存使用率水位与 `need_mem_eviction_` 信号（后者由 `PutStart` 分配失败时置位，见 [:L1674](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L1674)）：

```cpp
double used_ratio = MasterMetricManager::instance().get_global_mem_used_ratio();
if (used_ratio > eviction_high_watermark_ratio_ ||
    (need_mem_eviction_ && eviction_ratio_ > 0.0)) {
    ...
    BatchEvict(evict_ratio_target, evict_ratio_lowerbound);
    ...
}
```
—— [mooncake-store/src/master_service.cpp:L3953-L3975](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L3953-L3975)

**NoF 淘汰触发**——独立的 `NoFBatchEvict`，监控的是 NoF 池的水位与 `need_nof_eviction_`（由 NoF 分配失败置位，见 [:L1705](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L1705)），与内存淘汰解耦：

```cpp
#ifdef USE_NOF
    double nof_used_ratio = MasterMetricManager::instance().get_global_nof_used_ratio();
    if (nof_used_ratio > nof_eviction_high_watermark_ratio_ ||
        (need_nof_eviction_ && nof_eviction_ratio_ > 0.0)) {
        ...
        NoFBatchEvict(nof_evict_ratio_target, nof_evict_ratio_lowerbound);
    }
#endif
```
—— [mooncake-store/src/master_service.cpp:L3991-L4004](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L3991-L4004)

**MEMORY → LOCAL_DISK 的 offload 产生点**——`PutEnd` 标完 COMPLETE 后，若开启 offload，把 COMPLETE 的 MEMORY 副本推入 offload 队列（`inc_refcnt` 防淘汰），offload 完成回调里（[:L3484-L3488](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L3484-L3488)）造出直接 COMPLETE 的 LOCAL_DISK 副本加入对象：

```cpp
metadata.VisitReplicas(
    [](const Replica& replica) {
        return replica.is_completed() && replica.is_memory_replica();
    },
    [this, &object_id, &tenant_state](Replica& replica) {
        auto result = PushOffloadingQueue(object_id, replica);
        if (result) {
            replica.inc_refcnt();
            tenant_state.offloading_tasks.emplace(...);
        }
    });
```
—— [mooncake-store/src/master_service.cpp:L1950-L1965](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L1950-L1965)

#### 4.6.4 代码实践

**实践目标**（对应本讲规格的代码实践任务）：画出 Replica 状态机图，并解释三类副本的调度/淘汰——**只读源码，不改源码**。

**操作步骤**：

1. **画状态机**：以 4.4.2 的迁移图为准，画出实际发生的迁移（PROCESSING ⇄ COMPLETE），用实线；再用虚线标出「设计上存在但主路径未写入」的 INITIALIZED/REMOVED/FAILED，并在图注里写明「删除 = 容器 erase，非 REMOVED 态」。
2. **填三类副本轨迹表**：按下表，每格给出对应源码行号锚点（调度入口、初始状态、收尾方法、淘汰入口）：

   | 副本类型 | 调度入口 | 初始状态 | 标 COMPLETE 处 | 淘汰/演变 |
   |---|---|---|---|---|
   | MEMORY | `AllocateAndInsertMetadata` L1650 | PROCESSING | PutEnd `mark_complete` L1948 | `BatchEvict` L3973；可 offload 成 LOCAL_DISK |
   | LOCAL_DISK | offload 回调 L3484 | COMPLETE | （一出生即 COMPLETE） | promotion 提升回 MEMORY（L3846） |
   | NOF_SSD | `AllocateAndInsertMetadata` L1683 | PROCESSING | PutEnd `mark_complete` L1948 | `NoFBatchEvict` L4003 |

3. **解释调度差异**：用自己的话写一段——为什么 MEMORY/NOF 在 PutStart 调度、LOCAL_DISK 却在淘汰时才产生？

**需要观察的现象**：三类副本的「调度入口」分布在两个完全不同的时机（PutStart 主路径 vs 淘汰 offload 回调），「淘汰入口」也是两条独立路径（BatchEvict vs NoFBatchEvict），LOCAL_DISK 则根本不走「淘汰」而是走「提升」。

**预期结果**：一张自洽的状态机图 + 一张三类副本轨迹表 + 一段调度差异解释，每个论断都有源码行号支撑。

#### 4.6.5 小练习与答案

**练习 1**：为什么 MEMORY 副本要分 `BatchEvict`、NOF 副本要分 `NoFBatchEvict`，而不统一一套淘汰？

> **参考答案**：因为两套副本处在**不同的资源池**（`segment_manager_` vs `nof_segment_manager_`）、有不同的水位指标（`global_mem_used_ratio` vs `global_nof_used_ratio`）和不同的高水位阈值（`eviction_high_watermark_ratio_` vs `nof_eviction_high_watermark_ratio_`）。SSD 池容量大但读写慢，内存池容量小但快，二者的淘汰压力、淘汰代价不同，必须独立调控水位，否则一个池的压力会错误地传导到另一个池。

**练习 2**：LOCAL_DISK 副本「不被淘汰」，那它什么时候消失？

> **参考答案**：两条途径。(1) 它被 promotion 提升回 MEMORY 后，源 LOCAL_DISK 副本 `dec_refcnt`（[:L3851-L3853](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L3851-L3853)），后续可被清理；(2) 更根本地，LOCAL_DISK 数据归属某个 Client，当该 Client 失联/过期，`CleanupStaleHandles` 会清掉 `has_stale_local_disk_client()` 为真的副本（[replica.h:L383-L398](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/replica.h#L383-L398)）。也就是说 LOCAL_DISK 的生命周期绑定在「持有它的 Client 是否存活」上，而非绑定在水位淘汰上。

---

## 5. 综合实践

**任务**：把本讲的数据模型串成一张「Segment → Replica → Descriptor」的纵向追踪图，并据此回答三道诊断题。

**步骤**：

1. **画资源—副本—描述符三层图**：
   - 第一层（资源池）：画出 `SegmentManager`（内存/CXL 段，含 `AllocatorManager`）与 `NoFSegmentManager`（NoF 段）两个并列的池，标注 `MountedSegment`/`MountedNoFSegment` 的 `status`（OK 才进池）。
   - 第二层（副本）：画出一个 `ObjectMetadata`，里面是 `std::vector<Replica>`，画出四种 `Replica` 变体（MEMORY/DISK/LOCAL_DISK/NOF_SSD）及其 `status`（PROCESSING/COMPLETE）。
   - 第三层（描述符）：从某个 MEMORY `Replica` 画一根「`get_descriptor()` 拍扁」的箭头，指向 `Replica::Descriptor`，再指向 `GetReplicaListResponse.replicas`，最终指向数据面 TE。
2. **标状态迁移**：在第二层上标出 `mark_complete()`（PutEnd）、`mark_processing()`（UpsertStart）、容器 erase（删除）三处迁移，用不同颜色区分。
3. **诊断题**（用源码验证作答）：
   - (a) 一个 key 同时有一份 MEMORY 和一份 LOCAL_DISK 副本，`GetReplicaList` 会返回几份？为什么？（提示：看 `fn_is_completed` 只挑 COMPLETE，而 LOCAL_DISK 一出生就 COMPLETE，MEMORY 也是 COMPLETE——两者都会被返回，数据面可任选。）
   - (b) 若 `nof_replica_num=1` 但集群没有任何 NoF 段（`getMountedSegmentCount()==0`），`PutStart` 会失败吗？为什么？（提示：看 [:L1684-L1685](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L1684-L1685) 的进入条件，以及 `DetermineReplicaWriteMode` 把 `replica_num=1,nof_replica_num=1` 判为 `FLEXIBLE_DUAL_REPLICA`。）
   - (c) 为什么 `Replica::Descriptor` 里没有 `refcnt`？这会带来什么后果，Master 又用什么机制兜底？（提示：Descriptor 是快照；refcnt 是 Master 内部并发状态，不跨进程；后果是 Client 手里地址可能失效，兜底是 lease。）

**预期产出**：一张三层追踪图 + 三道诊断题的源码级答案。不确定处标注「待本地验证」并写明推断依据。

## 6. 本讲小结

- **Segment / NoFSegment** 是「货架」级资源单位：内存段归 `SegmentManager`、NoF 段归 `NoFSegmentManager`，二者字段相似但分型；只有 `SegmentStatus::OK` 的段才进分配器池，卸载走 DRAINING→DRAINED→UNMOUNTING 渐进过程。
- **SegmentStatus（段挂载生命周期）与 ReplicaStatus（副本写入生命周期）是两套独立状态机**，不要把 OK/COMPLETE 混为一谈。
- **`Replica` 用一个 `std::variant` 统一承载四种介质副本**（MEMORY/DISK/LOCAL_DISK/NOF_SSD），靠 `is_xxx()` 判类型、`ReplicaTypeVisitor` 派发类型；不可拷贝、可移动，移动后源置 `UNDEFINED` 防重复扣指标。
- **DISK ≠ LOCAL_DISK**：DISK 是 Master 本机磁盘文件副本（PutStart 产生，PROCESSING）；LOCAL_DISK 是远端 Client 本地磁盘副本（offload 产生，**直接 COMPLETE**）。
- **ReplicaStatus 实际迁移只有 `PROCESSING ⇄ COMPLETE`**（`mark_complete` / `mark_processing`）；INITIALIZED/REMOVED/FAILED 是「定义了但主路径不写入」的保留态，删除副本靠「从 `replicas_` 容器 erase」而非置 REMOVED——以代码为准，而非以注释为准。
- **`Replica::Descriptor` 是跨进程的瘦快照**：`get_descriptor()` 把活 Replica 拍扁成「id + 定位 variant + status」，经 RPC（`GetReplicaListResponse` 等）传给 Client，成为数据面 TE 寻址的唯一依据——这正是控制面输出 = 数据面输入的接缝。
- **三类副本调度/淘汰各异**：MEMORY 与 NOF_SSD 在 `PutStart` 调度（两入口）、分别走 `BatchEvict` / `NoFBatchEvict`；LOCAL_DISK 是 MEMORY 淘汰 offload 的产物、靠 promotion 提升回内存、随持有 Client 存活而存在。

## 7. 下一步学习建议

- **分配策略深入**：精读 [allocation_strategy.h:L202-L600](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/allocation_strategy.h#L202-L600) 的三种策略（Random / FreeRatioFirst / CXL），理解 best-effort 分配、preferred segment、`used_segments` 去重如何保证「同 key 多副本落不同段」。对应后续「分配策略」主题讲义。
- **淘汰算法深入**：精读 `master_service.cpp` 的 `BatchEvict`（[:L5244](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L5244)）与 `NoFBatchEvict`，理解高水位触发、`evict_ratio_target`/`lowerbound` 计算、软硬 pin 与 lease 对淘汰的豁免。对应后续「淘汰与租约」主题讲义。
- **多级存储（offload/promotion）深入**：沿 `PushOffloadingQueue`（[:L3500](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L3500)）→ offload 完成回调（[:L3484](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L3484)）→ `PromotionAllocStart` → `NotifyPromotionSuccess`（[:L3813](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L3813)）读完整条 L1(MEMORY)⇄L2(LOCAL_DISK) 流动链路。
- **推荐源码入口**：以本讲的 `Replica` 类（replica.h:L205）和 `ObjectMetadata`（master_service.h:L803）为锚点，向外辐射阅读 `AllocateAndInsertMetadata`（master_service.cpp:L1629）与 `EvictionThreadFunc`（master_service.cpp:L3953），即可把「副本的生、住、异、灭」读透。

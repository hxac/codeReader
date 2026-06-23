# Mooncake Store 总体架构：控制面与数据面分离

## 1. 本讲目标

本讲是「Store 主题」的第一讲，目标是让你从宏观上建立对 Mooncake Store 整体架构的认知。学完本讲你应该能够：

1. 说清 Mooncake Store 中**两个核心组件**（Master Service 与 Client）各自的职责，并理解「控制面（control plane）」与「数据面（data plane）」的边界划在哪里。
2. 说清一笔 `Put` 和一笔 `Get` 从发起到完成，分别经历了哪些步骤：哪一步走控制面（查/改 Master 元数据），哪一步走数据面（用 Transfer Engine 直传数据）。
3. 理解 Store 在「裸 Transfer Engine」之上提供了哪些增值（副本分配、淘汰、多级存储、租约），以及这些增值为什么没有拖慢数据面。
4. 能够根据源码画出 `Put` 的完整时序图，并标注控制面/数据面边界。

> 本讲不深入 Master 内部的分配器、淘汰算法、租约/软硬 pin 等细节，这些会在后续讲义展开。本讲只做「地图级」的认知。

## 2. 前置知识

本讲默认你已经具备以下背景（对应依赖讲义）：

- **Transfer Engine（TE）基础（依赖 u2-l6）**：TE 是 Mooncake 的底层数据传输引擎，能通过 RDMA/TCP 在节点之间做零拷贝、满带宽的批量传输。你需要知道 TE 提供 `registerLocalMemory`（注册本地内存段）、`submitTransfer`（发起读/写传输）这类能力。本讲中数据面的所有「搬数据」最终都落到 TE。
- **元数据服务（依赖 u1-l5）**：TE 自身需要一个外部 metadata server（etcd / Redis / HTTP）来登记各节点段的网络位置。**请注意：这个 TE 的 metadata server 与 Store 的 Master Service 是两个不同的东西**，本讲会反复强调这一点。
- **KV 缓存的概念**：Mooncake Store 是一个「分布式 KV 缓存」，`key` 是用户给的字符串，`value` 是一段字节（在 LLM 场景里通常是 KV cache 张量）。`Put` 写入，`Get` 读出。

### 什么是「控制面 / 数据面」？

这是分布式系统里一个非常常见的设计模式，先用一个生活化的比喻建立直觉：

> 想象一个大型快递系统。**调度中心**知道「每个仓库在哪、哪个仓库有空位、某个包裹放在哪个仓库的哪个货架」，但它**从不亲自搬运包裹**；真正搬包裹的是**货车司机**，司机之间点对点地把货从一个仓库送到另一个仓库。

- **控制面（Control Plane）= 调度中心**：只管「元数据」和「调度决策」（谁把数据放在哪、能不能读、要不要淘汰），**不搬数据**。
- **数据面（Data Plane）= 货车司机**：只管「实际搬数据」，走网络直传，追求高带宽、零拷贝。

为什么要分开？因为这两类工作的「性能特征」完全不同：控制面是**小请求、强一致、要加锁**；数据面是**大流量、要满带宽、怕加锁**。把它们解耦后，元数据的一致性不会拖慢数据传输的速度。Mooncake Store 就是这套思想的典型实现。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 | 本讲怎么用 |
|---|---|---|
| [docs/source/design/mooncake-store.md](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/docs/source/design/mooncake-store.md) | Store 官方设计文档 | 架构总览、组件职责、RPC 协议定义的权威来源 |
| mooncake-store/include/master_service.h | `MasterService` 类声明 | 控制面的 C++ 接口（PutStart/PutEnd/GetReplicaList/MountSegment） |
| mooncake-store/src/master_service.cpp | `MasterService` 实现 | 控制面逻辑的真正实现 |
| mooncake-store/include/real_client.h | `RealClient` 类声明 | 面向上层的客户端（同时承担 client + store server 双角色） |
| mooncake-store/src/client_service.cpp | `Client` 类实现（数据面编排） | `Put`/`Get` 的「控制面调用 + 数据面传输」协同代码 |
| mooncake-store/src/master_client.cpp | `MasterClient`（Master RPC 客户端封装） | Client 如何用 RPC 调用 Master 的控制面接口 |

一个容易混淆的点：源码里有三个「Client」相关类，本讲需要区分清楚：

- `RealClient`（real_client.h）：面向上层（Python/推理引擎）的客户端类，封装了对外 API。
- `Client`（client_service.h / client_service.cpp）：`RealClient` 内部持有的**数据面编排器**，负责「先问 Master，再用 TE 搬数据」。源码里成员名叫 `client_`。
- `MasterClient`（master_client.cpp）：`Client` 内部持有的**控制面 RPC 客户端**，专门负责和 Master 通信，成员名叫 `master_client_`。

记住这条调用链：`RealClient::put → client_->Put → master_client_.PutStart / TransferWrite / PutEnd`。

## 4. 核心概念与源码讲解

### 4.1 控制面与数据面分离：Store 架构总览

#### 4.1.1 概念说明

设计文档开篇就给出了 Store 的两大核心组件：**Master Service** 和 **Client**。

> As shown in the figure above, there are two key components in Mooncake Store: **Master Service** and **Client**.
> —— [docs/source/design/mooncake-store.md:26](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/docs/source/design/mooncake-store.md#L26)

这两者正好对应控制面与数据面：

- **Master Service = 控制面**：一个独立进程，集中管理整个集群的逻辑存储空间。它负责节点加入/离开、对象空间分配、元数据维护、副本放置调度、淘汰决策。**它不搬运任何数据**——文档用加粗的一句话明确这一点：
  > **Note: The Master Service does not take over any data flow, only providing corresponding metadata information.**
  > —— [docs/source/design/mooncake-store.md:76](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/docs/source/design/mooncake-store.md#L76)

- **Client = 数据面**：这里的 `Client` 并不只是「发请求的一方」，它身兼两职（设计文档原文）：
  > In Mooncake Store, the `Client` class is the only class defined to represent the client-side logic, but it serves **two distinct roles**:
  > 1. As a **client**, it is invoked by upper-layer applications to issue `Put`, `Get` ... requests.
  > 2. As a **store server**, it hosts a segment of contiguous memory that contributes to the distributed KV cache ... Data transfer is actually from one `Client` to another, bypassing the `Master Service`.
  > —— [docs/source/design/mooncake-store.md:32-L34](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/docs/source/design/mooncake-store.md#L32-L34)

这段话是理解整个架构的钥匙：**数据实际上是「Client → Client」直接传输的，绕过 Master**。Master 只是在传输前后被「问一下」。

#### 4.1.2 核心流程

把一次存储操作拆成「先问路、再走路」，可以用下面这张时序图概括：

```
       上层应用                     Client(数据面)                  Master Service(控制面)        其他 Client(数据面/store server)
           │                              │                                  │                                │
   Put()   │ ──────────────────────────▶ │                                  │                                │
           │                              │  ① PutStart(key, size, config) ──▶│  分配副本空间，返回 replica     │
           │                              │  ◀────────── replica descriptors ─│  描述符(在哪台机的哪段内存)     │
           │                              │  ② 用 TE 把数据直传 ──────────────────────────────────────────────▶│ 持有内存段，承接写入
           │                              │                                                                  │
           │                              │  ③ PutEnd(key) ────────────────▶│  标记 replica=COMPLETE，可读   │
   Get()   │ ──────────────────────────▶ │                                  │                                │
           │                              │  ④ GetReplicaList(key) ─────────▶│  返回可用副本位置 + 授予 lease │
           │                              │  ⑤ 用 TE 从某副本读数据 ◀────────────────────────────────────────│ 持有内存段，提供读取
           │  ◀────────── 数据 ──────────│                                  │                                │
```

关键边界：

- ①③④ 是**控制面**：小请求，走 RPC 到 Master，加锁、改元数据。
- ②⑤ 是**数据面**：大数据，走 TE 直传，Client↔Client，绕过 Master。

#### 4.1.3 源码精读

**Master Service 的控制面身份**——它是唯一集中管理集群资源的类，类注释直接点明它是「master server」：

```cpp
/*
 * @brief MasterService is the main class for the master server.
 * Lock order: To avoid deadlocks, the following lock order should be followed:
 * 1. client_mutex_
 * 2. metadata_shards_[shard_idx_].mutex
 * 3. segment_mutex_
 */
class MasterService {
```
—— [mooncake-store/include/master_service.h:61-L68](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-store/include/master_service.h#L61-L68)

注意它内部有一套严格的锁层级（client → 元数据分片 → segment），这正是「控制面要保证强一致」的体现。它的元数据还是**分片**的（`kNumShards = 1024`），以降低锁竞争：

```cpp
static constexpr size_t kNumShards = 1024;  // Number of metadata shards
struct MetadataShard {
    mutable SharedMutex mutex;
    std::unordered_map<std::string, TenantState> tenants GUARDED_BY(mutex);
};
std::array<MetadataShard, kNumShards> metadata_shards_;
```
—— [mooncake-store/include/master_service.h:1151-L1176](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-store/include/master_service.h#L1151-L1176)

**Client 的双角色**——`RealClient` 既是对外 API 入口（`put`/`get_into`），也通过 `global_segment_size` 决定是否贡献内存：

```cpp
class RealClient : public PyClient {
   public:
    RealClient();
    static std::shared_ptr<RealClient> create();

    int setup_real(
        const std::string &local_hostname, const std::string &metadata_server,
        size_t global_segment_size = 1024 * 1024 * 16,   // 贡献给集群的内存段大小
        size_t local_buffer_size = 1024 * 1024 * 16,     // 本地暂存缓冲大小
        const std::string &protocol = "tcp",
        ...
        const std::string &master_server_addr = "127.0.0.1:50051",
        const std::shared_ptr<TransferEngine> &transfer_engine = nullptr,
        ...);

    int put(const std::string &key, std::span<const char> value,
            const ReplicateConfig &config = ReplicateConfig{});
    int64_t get_into(const std::string &key, void *buffer, size_t size);
```
—— [mooncake-store/include/real_client.h:68-L127](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-store/include/real_client.h#L68-L127)

设计文档说明，通过把这两个 size 之一设为 0，可以让一个实例变成「纯 client」或「纯 server」：

> If `global_segment_size` is set to zero, the instance functions as a **pure client** ... If `local_buffer_size` is set to zero, it acts as a **pure server** ...
> —— [docs/source/design/mooncake-store.md:36-L38](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/docs/source/design/mooncake-store.md#L36-L38)

**容易踩的坑（重要）**：Store 的 Master Service 和 TE 的 metadata server **不是同一个东西**：

> The `Master Service` runs as an independent process and exposes RPC services to external components. Note that the `metadata service` required by the `Transfer Engine` (via etcd, Redis, or HTTP, etc.) is not included in the `Master Service` and needs to be deployed separately.
> —— [docs/source/design/mooncake-store.md:30](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/docs/source/design/mooncake-store.md#L30)

也就是说，一个完整的 Store 集群里通常有**三类进程**：Master Service（控制面）、TE metadata server（如 etcd，给 TE 找段地址用）、以及若干 Client（数据面，同时贡献内存）。

#### 4.1.4 代码实践

**实践目标**：用源码确认「Master 与 Client 是两个独立进程/角色」，并定位它们各自的启动参数。

**操作步骤**：

1. 在设计文档里找到「部署方式（Default mode / High availability mode）」一节，确认 Master 既可单节点运行，也可多节点 + etcd 选主。
2. 在 `master_service.h` 中找到 `MountSegment` 接口（Client 把自己的内存段「挂载」给 Master 的入口）。

**需要观察的现象**：Client 通过 `MountSegment` 把本地一段连续内存登记到 Master，Master 据此建立「全局资源池」视图。这正是「store server 角色」向「控制面」注册自己的过程：

```cpp
auto MountSegment(const Segment& segment, const UUID& client_id)
    -> tl::expected<void, ErrorCode>;
```
—— [mooncake-store/include/master_service.h:97-L98](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-store/include/master_service.h#L97-L98)

**预期结果**：你应能用自己的话描述「Master 全程不持有这段内存的字节，只持有它的元数据（基址、大小、归属 client）」。

#### 4.1.5 小练习与答案

**练习 1**：如果 Master 进程挂了，正在进行的 `Put`/`Get` 数据传输（TE 已经发起）会立刻失败吗？为什么？

> **参考答案**：TE 的数据传输是 Client↔Client 点对点的，不经过 Master。所以「已经发起的传输」本身不依赖 Master 在线。但传输前后的「问路」步骤（PutStart/GetReplicaList/PutEnd）会失败——没有 Master 就没人分配副本、没人改元数据状态。这正是控制面与数据面分离带来的副作用：数据面短暂地能独立存活，但控制面一挂，新的存储操作就无法完成。

**练习 2**：为什么设计上要让元数据 `kNumShards = 1024` 分片，而不是一把大锁？

> **参考答案**：控制面是强一致、加锁的，而 LLM 推理会并发产生海量的 `Put`/`Get` 元数据请求。一把全局锁会成为吞吐瓶颈；按 `hash(key)` 分到 1024 个 shard，可以让不同 key 的元数据操作并行加锁，把锁竞争摊薄到 1/1024。

---

### 4.2 Master Service：控制面（元数据 + 调度）

#### 4.2.1 概念说明

Master Service 作为控制面，承担三类职责：

1. **资源池管理**：维护「集群里有哪些内存段、各段还剩多少空间」（通过 `MountSegment` / `UnmountSegment`）。
2. **对象元数据维护**：对每个 key，记录它的副本放在哪些段、各副本的状态（INIT/COMPLETE/FAILED 等）。
3. **调度决策**：决定新副本放到哪些段（分配策略）、哪些对象该被淘汰、给读操作授予租约。

设计文档用 protobuf 形式给出了 Master 对外的 RPC 服务定义，这是理解控制面边界的最权威资料。其中和读写最相关的两对接口是 `PutStart`/`PutEnd` 与 `GetReplicaList`。

#### 4.2.2 核心流程

一笔 `Put` 在控制面被拆成**两步**（这是设计的关键）：

1. **PutStart**：Client 告诉 Master「我要写一个 key，长度 X，要 N 副本」。Master 调用分配策略，在合适的段里预留空间，返回 `replica_list`（每个副本的段名 + 偏移 + 大小 + 状态）。此时副本状态是「已分配、待写入」。
2. **PutEnd**：Client 写完数据后通知 Master，Master 把副本状态置为 `COMPLETE`，此后该 key 才对 `Get` 可见。

为什么要拆成两步？文档原文：

> The need for both start and end steps ensures that other Clients do not read partially written values, preventing dirty reads.
> —— [docs/source/design/mooncake-store.md:256](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/docs/source/design/mooncake-store.md#L256)

即：**用两阶段提交避免脏读**——只有写完并 `PutEnd` 之后，副本才会出现在 `GetReplicaList` 的结果里。

#### 4.2.3 源码精读

**PutStart 的 RPC 定义**（控制面协议）：

```protobuf
message PutStartRequest {
  required string key = 1;
  required int64 value_length = 2;
  required ReplicateConfig config = 3;
  repeated uint64 slice_lengths = 4;
};
message PutStartResponse {
  required int32 status_code = 1;
  repeated ReplicaInfo replica_list = 2;  // Master 分配好的副本信息
};
```
—— [docs/source/design/mooncake-store.md:240-L252](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/docs/source/design/mooncake-store.md#L240-L252)

**PutStart 的 C++ 实现**——做参数校验、获取对象操作锁、最后委托给 `AllocateAndInsertMetadata` 真正分配空间并写入元数据：

```cpp
auto MasterService::PutStart(const UUID& client_id, const std::string& key,
                             const std::string& tenant_id,
                             const uint64_t slice_length,
                             const ReplicateConfig& config)
    -> tl::expected<std::vector<Replica::Descriptor>, ErrorCode> {
    const auto object_id = MakeObjectIdentity(key, tenant_id);
    if ((config.replica_num == 0 && config.nof_replica_num == 0) ||
        key.empty() || slice_length == 0) {
        ...
        return tl::make_unexpected(ErrorCode::INVALID_PARAMS);
    }
    ...
    [[maybe_unused]] auto object_operation_lock =
        AcquireObjectOperationLock(object_id.tenant_id, object_id.user_key);
    ...
                return AllocateAndInsertMetadata(shard, client_id, key,
                                                 slice_length, config, group_id,
                                                 object_id.tenant_id, now);
```
—— [mooncake-store/src/master_service.cpp:1787-L1791](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-store/src/master_service.cpp#L1787-L1791)（分配调用见 [:L1892-L1894](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-store/src/master_service.cpp#L1892-L1894)）

注意几个控制面的典型特征：参数强校验、按对象粒度加锁（`AcquireObjectOperationLock`）、基于分片读写锁访问元数据。**全程没有任何数据拷贝**——它只产出「描述符」。

**GetReplicaList 的 C++ 实现**——它只遍历元数据、收集「已 COMPLETE」的副本描述符，并授予租约（lease），同样**不碰数据字节**：

```cpp
auto MasterService::GetReplicaList(const std::string& key,
                                   const std::string& tenant_id)
    -> tl::expected<GetReplicaListResponse, ErrorCode> {
    ...
    {
        MetadataAccessorRO accessor(this, object_id);
        ...
        const auto& metadata = accessor.Get();
        std::vector<Replica::Descriptor> replica_list;
        metadata.VisitReplicas(
            &Replica::fn_is_completed, [&replica_list](const Replica& replica) {
                replica_list.emplace_back(replica.get_descriptor());
            });
        ...
        // Grant a lease to the object so it will not be removed
        // when the client is reading it.
```
—— [mooncake-store/src/master_service.cpp:1444-L1466](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-store/src/master_service.cpp#L1444-L1466)（授予租约见 [:L1481-L1483](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-store/src/master_service.cpp#L1481-L1483)）

这里有个重要细节：`VisitReplicas(&Replica::fn_is_completed, ...)` 只挑状态为 `COMPLETE` 的副本返回。这就是「两阶段 Put 防脏读」在 Get 侧的体现——还没 `PutEnd` 的副本对读不可见。

#### 4.2.4 代码实践

**实践目标**：阅读 Master RPC 协议，理解 `ReplicaInfo` / `BufHandle` 的状态机。

**操作步骤**：

1. 打开设计文档的 protobuf 定义段，找到 `BufHandle` 的 `BufStatus` 枚举（INIT/COMPLETE/FAILED/UNREGISTERED）和 `ReplicaInfo` 的 `ReplicaStatus` 枚举。
2. 对照上面 `GetReplicaList` 里 `fn_is_completed` 的过滤逻辑，思考：如果一个副本是 `INIT` 状态，它会被返回给 Get 吗？

**需要观察的现象**：`BufStatus` 和 `ReplicaStatus` 是两套独立但相关的状态机——前者描述「某段内存空间」的生命周期，后者描述「某个副本」的生命周期。

**预期结果**：能说清「PutStart 让副本进入 INITIALIZED/分配态，TE 写完 + PutEnd 后转为 COMPLETE，Get 只认 COMPLETE」。

#### 4.2.5 小练习与答案

**练习 1**：`PutStart` 返回的 `replica_list` 里包含了什么信息？Client 拿到它之后下一步干什么？

> **参考答案**：包含每个副本落在哪个段（segment name）、段的基址、在该段内的偏移和大小、以及副本状态。Client 拿到这些「地址描述符」后，就知道该把数据写到哪些远端 Client 的内存里——接下来由数据面（TE）按这些地址做实际传输。

**练习 2**：为什么 `GetReplicaList` 要在返回前「授予租约（lease）」？

> **参考答案**：因为 Get 拿到副本地址后，数据传输（数据面）需要一段时间。在这段时间里，如果不加保护，该对象可能被别人 `Remove` 或被淘汰，导致读到的地址已失效（甚至读到被覆盖的数据）。租约保证「只要 lease 没过期，对象就不会被删除/淘汰」，从而保证读到一致的数据。

---

### 4.3 Client：数据面（基于 TE 的实际数据传输）

#### 4.3.1 概念说明

数据面的核心是 `Client` 类（client_service.h / client_service.cpp）。它做两件事：

1. **编排（orchestration）**：把一次 `Put`/`Get` 拆成「控制面调用 + 数据面传输」的正确顺序。
2. **传输**：用 `TransferEngine` 把数据搬到/搬出远端 Client 的内存段。

`Client` 内部持有三个关键依赖（在源码里你能看到它们作为成员出现）：

- `master_client_`：`MasterClient` 类型，控制面 RPC 客户端，封装了 `PutStart`/`PutEnd`/`GetReplicaList` 等调用。
- `transfer_engine_`：`TransferEngine` 类型，数据面引擎，负责注册内存、发起传输。
- `transfer_submitter_`：在 TE 之上封装的批量传输提交器（`submitRangeRead` / `submitBatchPutReplica` 等）。

#### 4.3.2 核心流程

数据面的传输动作可以概括为：

```
传输写 (TransferWrite):  本地 slices ──TE──▶ 远端段内某偏移
传输读 (TransferRead):   远端段内某偏移 ──TE──▶ 本地 slices
```

注意 `Slice` 这个概念：一个 value 可能被切成多个 `Slice`（分片），分别传到不同的副本段，从而利用多网卡的聚合带宽。这也呼应了设计文档里「striping and parallel I/O」的特性。

#### 4.3.3 源码精读

**控制面 RPC 的客户端封装**——`MasterClient::PutStart` 把请求发给 Master 进程：

```cpp
MasterClient::PutStart(const std::string& key, ...) {
    ScopedVLogTimer timer(1, "MasterClient::PutStart");
    ...
    auto result = invoke_rpc<&WrappedMasterService::PutStart, ...>(...);
    ...
}
```
—— [mooncake-store/src/master_client.cpp:560-L571](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-store/src/master_client.cpp#L560-L571)

**数据面传输的真正执行**——`TransferReadInternal` 把读请求交给 `transfer_submitter_`（TE 之上的封装），返回一个 `future`，`get()` 时才真正完成传输：

```cpp
ErrorCode Client::TransferReadInternal(
    const Replica::Descriptor& replica_descriptor, std::vector<Slice>& slices,
    uint64_t src_offset) {
    if (!transfer_submitter_) {
        LOG(ERROR) << "TransferSubmitter not initialized";
        return ErrorCode::INVALID_PARAMS;
    }
    auto future = transfer_submitter_->submitRangeRead(replica_descriptor,
                                                       slices, src_offset);
    ...
    return future->get();
}
```
—— [mooncake-store/src/client_service.cpp:3430-L3448](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-store/src/client_service.cpp#L3430-L3448)

`TransferWrite` 则把动作类型设为 `TransferRequest::WRITE`：

```cpp
ErrorCode Client::TransferWrite(const Replica::Descriptor& replica_descriptor,
                                std::vector<Slice>& slices) {
    return TransferData(replica_descriptor, slices, TransferRequest::WRITE);
}
```
—— [mooncake-store/src/client_service.cpp:3450-L3453](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-store/src/client_service.cpp#L3450-L3453)

注意：数据面函数的签名里出现的是 `Replica::Descriptor`——这正是控制面 `PutStart`/`GetReplicaList` 返回的「地址描述符」。**控制面的输出，就是数据面的输入**，这就是两个平面耦合的唯一接缝。

#### 4.3.4 代码实践

**实践目标**：在源码里定位「控制面 RPC 客户端」和「数据面传输器」这两个成员，确认它们是不同的对象。

**操作步骤**：

1. 在 `client_service.cpp` 中搜索 `master_client_.`（控制面调用前缀）和 `transfer_submitter_` / `transfer_engine_`（数据面对象）。
2. 数一数 `master_client_.` 出现了多少次、都调用了哪些方法；再看 `transfer_submitter_` 调用了哪些方法。

**需要观察的现象**：`master_client_` 只出现于「查/改元数据」的语境（PutStart、PutEnd、GetReplicaList、BatchQueryIp 等）；`transfer_submitter_` 只出现于「搬数据」的语境。两者井水不犯河水。

**预期结果**：你能用一句话总结——「`master_client_` 是控制面代理，`transfer_engine_`/`transfer_submitter_` 是数据面引擎，`Client` 类把它们编排到正确的时序里」。

#### 4.3.5 小练习与答案

**练习 1**：`Replica::Descriptor` 是由谁产生、由谁消费的？

> **参考答案**：由控制面产生——Master 在 `PutStart`/`GetReplicaList` 时分配/查询并返回；由数据面消费——`TransferWrite`/`TransferRead` 拿着 descriptor 里的段地址和偏移，通过 TE 真正读写。

**练习 2**：为什么读传输用 `future->get()` 这种异步风格？

> **参考答案**：数据面追求高带宽，单笔大对象会被切成多个 slice 并发传输。`submitRangeRead` 提交后立即返回 future，让调用方可以继续提交其他传输、或并行处理多个 key；`get()` 处再等待聚合完成。这种「提交-等待」分离能压榨多 NIC 的并行带宽，而控制面 RPC（小请求）则用同步等待即可。

---

### 4.4 Put 完整流程：控制面与数据面协同

#### 4.4.1 概念说明

把前面三节拼起来，看一笔完整的 `Put` 如何贯穿两个平面。`RealClient::put` 是对外入口，它内部委托给 `Client::Put`，后者才是真正体现「控制面 + 数据面协同」的地方。

#### 4.4.2 核心流程

一笔 `Put` 的完整时序（这是本讲的核心，建议记牢）：

```
RealClient::put(key, value)
   └─(拷贝 value 到本地注册内存, 切成 slices)
   └─ client_->Put(key, slices, config)              # Client::Put 编排
        ├─ ① master_client_.PutStart(...)   ──控制面──▶ Master 分配副本, 返回 descriptors
        │      (失败: NO_AVAILABLE_HANDLE → 触发淘汰; OBJECT_ALREADY_EXISTS → 直接返回)
        ├─ ② for 每个 replica:
        │      TransferWrite(replica, slices) ──数据面──▶ TE 把 slices 写到该 replica 的远端段
        ├─    DetermineFinalizeDecision(...)          # 根据传输成败决定 PutEnd 还是 PutRevoke
        └─ ③ master_client_.PutEnd(key, end_type)     ──控制面──▶ Master 标记副本 COMPLETE
               (或 PutRevoke —— 全失败时回收空间)
```

三个边界非常清晰：①③ 控制面、② 数据面。

#### 4.4.3 源码精读

`Client::Put` 的实现是本讲最重要的代码段，逐段看：

```cpp
tl::expected<void, ErrorCode> Client::Put(const ObjectKey& key,
                                          std::vector<Slice>& slices,
                                          const ReplicateConfig& config) {
    // 准备每个 slice 的长度
    std::vector<size_t> slice_lengths;
    for (size_t i = 0; i < slices.size(); ++i) {
        slice_lengths.emplace_back(slices[i].size);
    }
    ...
    // ① 控制面：请求 Master 分配副本空间
    auto start_result = master_client_.PutStart(key, slice_lengths, client_cfg);
    if (!start_result) {
        ErrorCode err = start_result.error();
        if (err == ErrorCode::OBJECT_ALREADY_EXISTS) { ... return {}; }
        if (err == ErrorCode::NO_AVAILABLE_HANDLE) { ... }   // 空间不足, 可能触发淘汰
        ...
    }
    ...
    // ② 数据面：对每个副本, 用 TE 写数据
    for (const auto& replica : start_result.value()) {
        if (replica.is_memory_replica() || replica.is_nof_replica()) {
            const auto replica_type = replica.is_memory_replica()
                                          ? ReplicaType::MEMORY
                                          : ReplicaType::NOF_SSD;
            ErrorCode transfer_err = TransferWrite(replica, slices);   // ← 数据面
            if (transfer_err != ErrorCode::OK) {
                transfer_summary.RecordFailure(replica_type, transfer_err);
                continue;   // 单个副本失败不致命, best-effort
            }
            transfer_summary.RecordSuccess(replica_type);
        }
    }
    ...
    // 根据成败决定收尾方式
    const auto finalize_decision =
        DetermineFinalizeDecision(config, transfer_summary);

    // ③ 控制面：成功则 PutEnd 标记 COMPLETE
    if (finalize_decision.end_type.has_value()) {
        auto end_result =
            master_client_.PutEnd(key, *finalize_decision.end_type);
        ...
    }
    // 全失败则 PutRevoke 回收空间
    if (finalize_decision.revoke_type.has_value()) {
        auto revoke_result =
            master_client_.PutRevoke(key, *finalize_decision.revoke_type);
        ...
    }
```
—— [mooncake-store/src/client_service.cpp:1486-L1585](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-store/src/client_service.cpp#L1486-L1585)

（① PutStart 在 [:L1505](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-store/src/client_service.cpp#L1505)；② TransferWrite 在 [:L1551](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-store/src/client_service.cpp#L1551)；③ PutEnd 在 [:L1572](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-store/src/client_service.cpp#L1572)）

注意这段代码体现了几个关键设计：

- **best-effort 副本**：`TransferWrite` 单个副本失败只是 `continue`，不会立刻整体失败。只要至少有一个副本成功，`DetermineFinalizeDecision` 就可能选择 `PutEnd`。这与设计文档「allocates as many replicas as possible」一致。
- **传输计费**：循环外有 `t0_put` 计时和 `metrics_->transfer_metric.put_latency_us.observe(us_put)`，把延迟归到「数据面」指标，而非控制面。
- **收尾决策分离**：成功走 `PutEnd`，全失败走 `PutRevoke`（避免 zombie 对象占用空间）。

而 `RealClient::put` 只是更上一层：先在本地注册内存里分配缓冲、`memcpy` 数据、切成 slices，再调 `client_->Put`：

```cpp
tl::expected<void, ErrorCode> RealClient::put_internal(...) {
    ...
    auto alloc_result = client_buffer_allocator->allocate(value.size_bytes());
    ...
    auto &buffer_handle = *alloc_result;
    memcpy(buffer_handle.ptr(), value.data(), value.size_bytes());
    std::vector<Slice> slices = split_into_slices(buffer_handle);
    auto put_result = client_->Put(key, slices, config);   // ← 进入 Client::Put 编排
    ...
}
```
—— [mooncake-store/src/real_client.cpp:1688-L1699](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-store/src/real_client.cpp#L1688-L1699)

#### 4.4.4 代码实践

**实践目标**：跟随本讲的「代码实践任务」——画出一笔 Put 的完整流程图，标注控制面/数据面边界。

**操作步骤**：

1. 在 `Client::Put`（client_service.cpp:1486）设三个「心理断点」：`PutStart`、`TransferWrite`、`PutEnd`。
2. 用纸/任意画图工具画一条从「上层应用」到「Master」再到「远端 Client」的时间轴。
3. 把三步分别标注为「控制面」「数据面」「控制面」。
4. 进阶：在 `PutStart` 失败分支上，画出 `NO_AVAILABLE_HANDLE` 时「Master 内部触发淘汰线程 → 释放空间 → 重试」的旁路（参考设计文档 Eviction Policy 一节）。

**需要观察的现象**：你会发现数据面（TransferWrite）这一步是唯一「大数据、跨网络」的步骤，其余都是小 RPC。

**预期结果**：得到一张和本讲 4.1.2 节时序图一致的图，并能把每一步对应到精确的源码行号。**待本地验证**：如果你本地能跑 Store，可以在 `Client::Put` 三处加临时日志（仅阅读/调试用，不要提交），观察一次真实 Put 的执行顺序。

#### 4.4.5 小练习与答案

**练习 1**：假设配置了 3 副本，但集群只剩 1 个段有空间，`PutStart` 会怎样？后续 `PutEnd` 还会调用吗？

> **参考答案**：`PutStart` 是 best-effort 的——能分多少就分多少，至少 1 个就返回（不会因为凑不齐 3 个而失败，除非一个都分不到才返回 `NO_AVAILABLE_HANDLE`）。本例会返回 1 个副本描述符。之后 `TransferWrite` 只写这 1 个副本，`DetermineFinalizeDecision` 仍会判定成功并调用 `PutEnd`，对象以 1 副本（而非 3 副本）可见。

**练习 2**：如果 3 个副本的 `TransferWrite` 全部失败，`Client::Put` 会调用 `PutEnd` 吗？为什么？

> **参考答案**：不会。`DetermineFinalizeDecision` 会发现没有任何成功副本，于是 `end_type` 为空、`revoke_type` 有值，走 `PutRevoke` 回收 `PutStart` 分配的空间。这避免了 zombie 对象——如果只 PutStart 不 PutEnd/Revoke，Master 会在 `put_start_release_timeout`（默认 10 分钟）后兜底回收，但主动 Revoke 能立刻释放。

---

### 4.5 Get 完整流程：控制面与数据面协同

#### 4.5.1 概念说明

`Get` 的结构和 `Put` 对称，但更简单：先查路（控制面），再读数据（数据面）。它的一个核心特点是**租约（lease）保护**：查路时 Master 授予 lease，读数据期间对象不可被删/淘汰；若读太慢、lease 过期，则本次 Get 视为失败（防止读到被覆盖的脏数据）。

#### 4.5.2 核心流程

```
RealClient::get / get_into
   └─ client_->Get(key, slices)
        ├─ ④ Query(key) = master_client_.GetReplicaList(key)  ──控制面──▶ Master 返回 COMPLETE 副本 + 授予 lease
        │      得到 QueryResult{ replicas[], lease_expiry }
        ├─ ⑤ FindFirstCompleteReplica(replicas)                # 在多个副本里挑一个可读的
        └─ TransferReadRange(replica, slices, src_offset)       ──数据面──▶ TE 从该副本读数据
               读完后检查 lease 是否过期: 过期 → LEASE_EXPIRED 失败
```

#### 4.5.3 源码精读

**入口与控制面查询**：

```cpp
tl::expected<void, ErrorCode> Client::Get(const std::string& object_key,
                                          std::vector<Slice>& slices) {
    auto query_result = Query(object_key);            // ④ 控制面
    if (!query_result) {
        return tl::unexpected(query_result.error());
    }
    return Get(object_key, query_result.value(), slices);
}
```
—— [mooncake-store/src/client_service.cpp:938-L944](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-store/src/client_service.cpp#L938-L944)

`Query` 里就是一次控制面 RPC，并把 lease TTL 折算成本地过期时间：

```cpp
tl::expected<QueryResult, ErrorCode> Client::Query(
    const std::string& object_key) {
    std::chrono::steady_clock::time_point start_time =
        std::chrono::steady_clock::now();
    auto result = master_client_.GetReplicaList(object_key);
    ...
    return QueryResult(
        std::move(result.value().replicas),
        start_time + std::chrono::milliseconds(result.value().lease_ttl_ms));
}
```
—— [mooncake-store/src/client_service.cpp:1010-L1021](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-store/src/client_service.cpp#L1010-L1021)

**数据面读取 + lease 检查**：

```cpp
tl::expected<void, ErrorCode> Client::Get(const std::string& object_key,
                                          const QueryResult& query_result,
                                          std::vector<Slice>& slices,
                                          uint64_t src_offset) {
    Replica::Descriptor replica;
    ErrorCode err = FindFirstCompleteReplica(query_result.replicas, replica);  // ⑤ 挑副本
    ...
    auto t0_get = std::chrono::steady_clock::now();
    err = TransferReadRange(replica, slices, src_offset);   // ← 数据面读
    ...
    if (err != ErrorCode::OK) {
        LOG(ERROR) << "transfer_read_range_failed key=" << object_key;
        return tl::unexpected(err);
    }
    if (query_result.IsLeaseExpired()) {                    // 读太慢 → lease 过期
        LOG(WARNING) << "lease_expired_before_data_transfer_completed key="
                     << object_key;
        return tl::unexpected(ErrorCode::LEASE_EXPIRED);
    }
    return {};
}
```
—— [mooncake-store/src/client_service.cpp:1129-L1165](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-store/src/client_service.cpp#L1129-L1165)

`IsLeaseExpired()` 的检查是 Get 相比 Put 多出来的一环，体现了「数据面耗时长 → 控制面授权可能失效」的协调机制。

#### 4.5.4 代码实践

**实践目标**：对比 Put 与 Get，理解两者在「控制面调用次数」和「数据面方向」上的差异。

**操作步骤**：

1. 在 `Client::Put` 里数控制面调用（PutStart + PutEnd/Revoke = 2 次）；在 `Client::Get` 里数控制面调用（GetReplicaList = 1 次）。
2. 比较数据面方向：Put 是 `TransferWrite`（本地→远端），Get 是 `TransferReadRange`（远端→本地）。

**需要观察的现象**：Get 的控制面只有一次「读」（GetReplicaList），不像 Put 那样要「改状态」（PutEnd）。因为读不改变对象元数据，只刷新 lease。

**预期结果**：能解释为什么「Get 的控制面开销比 Put 小」。**待本地验证**：若本地可跑，用批量 `BatchGet`（client_service.cpp:947）观察「一次 BatchGetReplicaList + 多次并行 TransferRead」如何摊薄控制面开销。

#### 4.5.5 小练习与答案

**练习 1**：`FindFirstCompleteReplica` 为什么要在 Client 端再挑一次「COMPLETE 副本」？Master 的 `GetReplicaList` 不是已经只返回 COMPLETE 了吗？

> **参考答案**：主要有两重意义。一是防御性编程：副本状态在 RPC 往返期间理论上可能变化，Client 端再校验一次更稳健。二是为「多副本选优」留扩展点——多个 COMPLETE 副本时，未来可基于本地性、负载选最优副本（当前实现取第一个）。这也契合设计文档「Client can select an appropriate replica for reading based on this information」。

**练习 2**：如果数据面读得非常慢，慢到 lease 过期才读完，Get 返回失败——但此时数据其实已经读到本地了，这会出问题吗？

> **参考答案**：返回 `LEASE_EXPIRED` 是刻意的保守策略：lease 过期意味着对象在此期间可能被删除或覆盖，读到的数据一致性无法保证，因此丢弃、当作失败。宁可重试，也不返回可能脏的数据。这是「强一致」的代价，也是控制面（lease）对数据面（传输）施加的安全边界。

---

## 5. 综合实践

**任务**：把本讲所有概念串起来，完成一份「Store 架构 + Put 全流程」的标注图，并据此回答三个诊断题。

**步骤**：

1. **画组件图**：画出「上层应用 / RealClient / Client(数据面编排) / MasterClient(控制面 RPC) / Master Service(控制面) / TE metadata server / 远端 Client(数据面)」这些角色，用线连起来，区分「控制面线（虚线）」和「数据面线（实线）」。
2. **画 Put 时序**：复刻本讲 4.4.2 的时序，但每一步都要标注对应源码行号（client_service.cpp 的 1505 / 1551 / 1572）。
3. **标注边界**：用一条竖线把图分成左半「控制面」、右半「数据面」，确保 PutStart/PutEnd 在左、TransferWrite 在右。
4. **诊断题**（用源码验证，写出答案）：
   - (a) 如果 Master 在 `PutStart` 之后、`PutEnd` 之前崩溃，这笔 Put 最终会怎样？（提示：看 master_service.h 的 zombie 清理参数 `put_start_release_timeout` 与设计文档 Zombie Object Cleanup 一节）
   - (b) 一个只配了 `global_segment_size=0` 的 Client，能发起 `Put` 吗？能承接别人的写入吗？（提示：看设计文档 4.1.3 节的 pure client/pure server）
   - (c) `Client::Put` 里「数据面延迟」被记到哪个指标？为什么不计入控制面延迟？（提示：看 `metrics_->transfer_metric.put_latency_us` 的位置）

**预期产出**：一张自洽的架构图 + 三道诊断题的源码级答案。如果某题不确定，标注「待本地验证」并写出你的推断依据。

## 6. 本讲小结

- Mooncake Store 由 **Master Service（控制面）** 和 **Client（数据面）** 两个核心组件构成；**数据实际是 Client↔Client 直传的，绕过 Master**。
- **Master 只管元数据和调度，不搬数据**（设计文档 L76）；它的元数据按 1024 分片降低锁竞争，用对象级操作锁保证一致。
- **控制面的输出（`Replica::Descriptor`）就是数据面的输入**——`PutStart`/`GetReplicaList` 返回地址描述符，`TransferWrite`/`TransferRead` 拿着描述符用 TE 搬数据。这是两个平面的唯一接缝。
- 一笔 **Put = 控制面(PutStart) → 数据面(TransferWrite) → 控制面(PutEnd/Revoke)**，三步交替，且副本是 best-effort 的。
- 一笔 **Get = 控制面(GetReplicaList，授予 lease) → 数据面(TransferRead)**，读完后检查 lease 是否过期，过期则判失败以保证强一致。
- Store 相对裸 TE 的增值（副本分配、淘汰、租约、防脏读的两阶段 Put、多级存储）**几乎全部落在控制面**，因此数据面仍能保持零拷贝、满带宽。

## 7. 下一步学习建议

- **控制面深入**：阅读 `master_service.cpp` 中的 `AllocateAndInsertMetadata` 和分配策略（`allocation_strategy.h`），理解 `random` / `free_ratio_first` / `cxl` 三种策略如何选段。对应后续讲义（分配策略主题）。
- **淘汰与租约**：精读设计文档的 Eviction Policy / Lease / Soft Pin / Hard Pin 节，以及 `master_service.h` 里 `BatchEvict`、`GrantLease` 的实现，理解控制面如何在内存压力下保住正在读写的对象。
- **数据面深入**：回到 Transfer Engine（u2-l6），精读 `transfer_submitter_` 如何把一个 value 切成多 slice、跨多 NIC 并发提交，理解「满带宽」是怎么来的。
- **多级存储**：阅读设计文档 Multi-layer Storage Support 与 `file_storage.h`，理解数据面如何把冷数据异步落到 SSD（NoF/DFS），以及读路径如何在内存未命中时回源。
- **推荐源码入口**：以本讲的 `Client::Put`（client_service.cpp:1486）和 `Client::Get`（client_service.cpp:938）为锚点，向外辐射阅读 `master_client.cpp`（控制面 RPC 封装）和 `transfer_task.cpp`（数据面任务），就能把两个平面读透。

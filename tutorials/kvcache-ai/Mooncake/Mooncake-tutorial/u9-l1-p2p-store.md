# P2P Store：无 Master 的对等存储

## 1. 本讲目标

本讲是「高级主题」单元的第一讲，聚焦仓库里一个相对独立、用 **Go 语言**实现的组件——`mooncake-p2p-store`。学完本讲你应该能够：

1. 说清 **P2P Store 为什么没有中心化 Master**，它用什么代替了 Master 的角色（答案：etcd 元数据 + BitTorrent 式对等分发），以及这种「无 master」架构适合什么场景（checkpoint 分发、大模型权重广播）。
2. 掌握四个核心 API 的 **BitTorrent 式语义**：`Register`（做种，只写元数据不传数据）、`GetReplica`（拉取副本并自动把自己变成新的数据源）、`DeleteReplica`/`Unregister`（停止对外供源）。
3. 读懂 etcd 在其中扮演的「全局目录 + 乐观锁」双重角色：所有节点通过 etcd 发现谁有数据（`Get`/`List`），又通过 etcd 的 `ModRevision` 事务实现无锁并发更新元数据（`Update`）。
4. 理解 `mooncake-p2p-store` 与生产级 `checkpoint-engine` 的**演进关系**：前者是教学/参考实现，后者是同一思想的高性能工业版本。
5. 能根据源码画出「一个文件被 Register 后，多个节点相互 GetReplica 形成蜂群（swarm）分发」的流程图，并对比它与中心化 Mooncake Store 的差异。

> 本讲只讲 P2P Store 这一组件。它的数据面完全复用了 C++ 的 Transfer Engine（通过 cgo 桥接），所以本讲会引用 TE，但不会深入 TE 内部（那是 u2 单元的内容）。

## 2. 前置知识

本讲默认你已经具备以下背景（对应依赖讲义）：

- **Mooncake Store 总体架构（依赖 u5-l1）**：你需要知道「中心化 Store」长什么样——有一个 `MasterService` 进程集中管理全局存储空间、做副本分配和淘汰。本讲的 P2P Store 正是**没有这个 Master**，理解了「为什么中心化 Store 需要 Master」，才能理解 P2P Store「去掉 Master」之后用什么补上。
- **Transfer Engine 架构（依赖 u2-l1）**：P2P Store 的所有「搬数据」都交给 TE。你需要知道 TE 的基本概念：`registerLocalMemory`（把本地一段内存注册成可被远端访问的 segment）、`openSegment`（打开远端节点的 segment 拿到一个 ID）、`submitTransfer`（对一个 batch 提交读/写请求）。
- **etcd 基础**：etcd 是一个分布式的、强一致的 key-value 存储。本讲会用到它的三个特性：普通 `Get`/`Put`、基于 key 前缀的 `Range` 查询、以及最关键的 **`Txn` 事务 + `ModRevision` 乐观并发控制**（CAS）。如果你没接触过，可以把 etcd 想象成「一个支持原子 compare-and-set 的全局字典」。

### 什么是「BitTorrent 式分发」？

先用一个生活化的比喻建立直觉：

> 想象一个老师要把一份 10GB 的课件发给全班 100 个学生。
> - **中心化方式（中心化 Store / 单点下载）**：每个学生都从老师那里拉 10GB。老师家的上行带宽很快被 100 路下载打满，成为瓶颈。
> - **BitTorrent 方式（P2P Store）**：老师只把课件「登记」到一个公告板上（不做种时也不亲自发完整副本），谁要课件就从**任意已经拿到课件的同学**那里拉；而且**每拉到一份，自己就立刻变成新的供源**。学生越多，可供下载的源越多，整体分发越快，老师的上行带宽也不会被打爆。

P2P Store 就是把这套思想套在 GPU 集群的 checkpoint 分发上。其中：

- **公告板 = etcd**：登记「哪个文件、分成了哪些块、每块在哪些节点上（Gold 原始副本 + Replica 副本）」。
- **老师 = 调用 `Register` 的节点（做种者 / seeder）**。
- **学生 = 调用 `GetReplica` 的节点**，拉完自动升级成新的供源。

### 「无 master」到底省掉了什么？

回顾 u5-l1：中心化 Store 有一个 `MasterService` 进程，专门负责「谁的数据放在哪、能不能读、要不要淘汰」这些**控制面**决策，并维护一份全局元数据。它的好处是集中、可控；代价是：Master 是单点（虽然可以 HA）、是元数据的瓶颈、是故障域的中心。

P2P Store 的做法是**把这份「全局元数据」直接外包给 etcd**，并且**不在任何节点上跑集中的调度逻辑**——每个节点自己读 etcd、自己挑源、自己拉数据、拉完自己回写 etcd。这样：

- 没有专门的「Store master 进程」要部署和容灾（只需要一个现成的 etcd 集群）。
- 没有「数据必须经过的中央调度点」，数据始终是节点之间点对点直传。

代价是：P2P Store **不做副本分配策略、不做淘汰、不做多级存储、不做租约**——它只解决「把一个大对象尽快、不堵塞地广播给很多节点」这一件事。这就是它和中心化 Store 的本质分工差异。

## 3. 本讲源码地图

P2P Store 的全部代码都在 `mooncake-p2p-store/` 下，体量很小（核心约 400 行 Go）。本讲涉及的关键文件如下：

| 文件 | 作用 | 本讲怎么用 |
|---|---|---|
| [core.go](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/core.go) | `P2PStore` 主类型与全部对外 API（`Register`/`GetReplica`/`DeleteReplica`/`Unregister`/`List`） | 本讲的主战场，对应「P2P Store 核心」模块 |
| [metadata.go](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/metadata.go) | `Payload`/`Shard`/`Location` 数据模型 + etcd 封装（`Create`/`Put`/`Update`/`Get`/`List`） | 对应「元数据/目录」模块，讲清 etcd 的乐观锁 |
| [catalog.go](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/catalog.go) | 进程内的本地目录（记录「本进程是否持有某个对象的副本」） | 「元数据/目录」模块的本地一侧 |
| [registered_memory.go](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/registered_memory.go) | 本地内存注册管理（引用计数 + 分块并发注册到 TE） | 解释「注册内存很慢、所以要分块并发」 |
| [transfer_engine.go](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/transfer_engine.go) | 用 **cgo** 封装 C++ Transfer Engine 的 C 接口 | 「示例」模块里讲数据面如何复用 TE |
| [error.go](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/error.go) | 全部错误定义 | 理解各 API 的错误语义 |
| [p2p-store-example.go](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/example/p2p-store-example.go) | 示例程序，模拟 trainer 做种 + inferencer 拉取 | 对应「示例」模块，是代码实践的入口 |

一个容易混淆的点：P2P Store 里有两套「目录」：

- **全局目录 = etcd**：跨节点共享，存「每个对象的全部分块及其所有副本位置」。这是真正的元数据来源。
- **本地目录 = `Catalog`（catalog.go）**：每个进程内存里的一张表，只记录「本进程当前持有哪些对象的副本」。它用来防止对同一个对象重复 `Register`/`GetReplica`，并记住本地内存以便后续 `Unregister`。

记住这条主线：**控制面 = etcd（全局），数据面 = TE（点对点），本地 `Catalog` 只是缓存本进程的状态。**

## 4. 核心概念与源码讲解

### 4.1 P2P Store 核心：无 master 的对象分发

#### 4.1.1 概念说明

`P2PStore` 是整个组件的门面类型，定义非常精简——它把四样东西组合在一起：

```go
type P2PStore struct {
    metadataConnString string
    localServerName    string   // 本节点在集群中的唯一名（ip:port）
    catalog            *Catalog // 本地目录
    memory             *RegisteredMemory // 本地内存注册管理
    metadata           *Metadata // etcd 封装
    transfer           *TransferEngine // TE（cgo）
}
```

> —— [core.go:33-40](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/core.go#L33-L40)

注意这里**没有任何类似 `MasterClient` 的成员**——对比 u5-l1 里中心化 Store 的 `RealClient` 持有一个 `master_client_` 去 RPC 调用 Master，P2P Store 只持有一个 `metadata`（直连 etcd）和一个 `transfer`（直连 TE）。这就是「无 master」在数据结构上的直接体现：**没有可调度的中心节点，只有一张共享的元数据表和一个点对点传输引擎**。

#### 4.1.2 核心流程：初始化

`NewP2PStore` 做三件事：连 etcd、建 TE 并装 transport、分配本地可注册内存池。

```go
func NewP2PStore(metadataConnString string, localServerName string, nicPriorityMatrix string) (*P2PStore, error) {
    metadata, err := NewMetadata(metadataConnString, METADATA_KEY_PREFIX)
    ...
    transfer, err := NewTransferEngine(metadataConnString, localServerName, localIpAddressCStr, rpcPort)
    ...
    if len(nicPriorityMatrix) == 0 {
        err = transfer.installTransport("tcp", nicPriorityMatrix)   // 无 NIC 矩阵 → 走 TCP
    } else {
        err = transfer.installTransport("rdma", nicPriorityMatrix)  // 有 NIC 矩阵 → 走 RDMA
    }
    ...
}
```

> —— [core.go:57-89](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/core.go#L57-L89)

几个要点：

- `METADATA_KEY_PREFIX = "mooncake/checkpoint/"`（[core.go:31](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/core.go#L31)）：所有对象在 etcd 里的 key 都加这个前缀，说明 P2P Store 的设计目标就是 checkpoint 分发。
- transport 选择很朴素：传了 NIC 优先级矩阵就上 RDMA，否则退化 TCP（[core.go:70-78](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/core.go#L70-L78)）。注意官方文档说明示例目前仅支持 RDMA。
- `MAX_CHUNK_SIZE = 4096 * 1024 * 1024`（4GiB）（[core.go:30](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/core.go#L30)）：本地内存注册的粒度上限。注释提醒「内存注册是一个**慢**操作」，所以大于这个值的 buffer 会被拆成多块分别注册（详见 4.3.2）。

#### 4.1.3 核心流程：Register（做种）

`Register` 是 BitTorrent 语义里「做种（seeding）」的等价物。**关键认知：Register 不搬运任何数据，只把元数据写进 etcd。** 源码里它做的事是：

1. 校验参数、防止重复注册（`catalog.Contains`）。
2. 把传入的每段内存注册到 TE（`store.memory.Add`），并按 `maxShardSize` 把对象**逻辑切分成多个 shard**。
3. 每个 shard 记录一个 `Gold`（原始副本）位置，指向**本节点**的内存地址。此时 `ReplicaList` 为空。
4. 把整个 `Payload`（含所有 shard）写进 etcd（`forceCreate=true` 用 `Put` 覆盖，否则用 `Create` 保证 key 不存在）。

```go
for ; offset < size; offset += maxShardSize {
    shardLength := maxShardSize
    if shardLength > size-offset {
        shardLength = size - offset
    }
    goldLocation := Location{
        SegmentName: store.localServerName,   // Gold 永远指向「做种者自己」
        Offset:      uint64(addr) + offset,
    }
    shard := Shard{
        Length:      shardLength,
        Gold:        []Location{goldLocation},
        ReplicaList: nil,                      // 做种时还没有任何副本
    }
    payload.Shards = append(payload.Shards, shard)
}
```

> —— [core.go:153-169](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/core.go#L153-L169)

接着写入 etcd 并登记本地目录（注意 `IsGold: true`）：

```go
if forceCreate {
    err = store.metadata.Put(ctx, name, &payload)     // 覆盖式写入
} else {
    err = store.metadata.Create(ctx, name, &payload)  // 仅当 key 不存在才写
}
...
params := CatalogParams{IsGold: true, AddrList: addrList, SizeList: sizeList, MaxShardSize: maxShardSize}
store.catalog.Add(name, params)
```

> —— [core.go:172-191](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/core.go#L172-L191)

> **直觉小结**：`Register` 完成后，etcd 里多了一条记录，内容大致是「对象 `foo/bar`，分成 N 块，每一块的 Gold 在做种者节点 X 的某地址」。**此时一个字节的数据都没在节点间流动**——这正是 BitTorrent「先登记，再按需分发」的特点。

#### 4.1.4 核心流程：GetReplica（拉取并自动成为新源）

这是 P2P Store 最核心、最能体现「蜂群」语义的 API。它的逻辑分三步：

**第一步：读元数据，挑源拉数据。** 调 `doGetReplica`，它为每个 shard 启一个 goroutine 并发地从某个源拉取。

```go
for ; offset < size; offset += maxShardSize {
    source := addr + uintptr(offset)
    shard := payload.Shards[taskID]
    taskID++
    wg.Add(1)
    go func() {
        defer wg.Done()
        err = store.performTransfer(ctx, source, shard)  // 每个 shard 一个 goroutine 并发拉
        ...
    }()
}
```

> —— [core.go:271-286](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/core.go#L271-L286)

**第二步：选源策略（BitTorrent 的精髓）。** `performTransfer` 里对每个 shard，第一次尝试用 `shard.GetLocation(0)`——它会**随机**挑一个副本，而且**优先从 ReplicaList 里挑，挑不到才用 Gold**：

```go
func (s *Shard) getRandomLocation() *Location {
    r := rand.New(rand.NewSource(time.Now().UnixNano()))
    if len(s.ReplicaList) > 0 {
        index := r.Intn(len(s.ReplicaList))
        return &s.ReplicaList[index]   // 副本存在时，优先随机挑一个副本
    } else if len(s.Gold) > 0 {
        index := r.Intn(len(s.Gold))
        return &s.Gold[index]          // 没副本才退回原始 Gold
    }
    return nil
}
```

> —— [metadata.go:77-87](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/metadata.go#L77-L87)

这条「优先副本、随机分散」的策略，就是**把下载压力从做种者（Gold）转移到已经拿到的副本上**——做种者越多的人，蜂群扩张越快，做种者的上行带宽越不会被压垮。如果某次拉取失败，`performTransfer` 会重试，并依次尝试其它源（`getRetryLocation`，[metadata.go:89-98](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/metadata.go#L89-L98)），重试上限为 \(\max(3,\,\text{shard.Count()})\)（[core.go:364-367](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/core.go#L364-L367)）。

**第三步：把自己登记成新副本。** 数据拉完后，调 `updatePayloadMetadata`，给每个 shard 的 `ReplicaList` 追加**本节点**的位置，再写回 etcd：

```go
replicaLocation := Location{
    SegmentName: store.localServerName,   // 把「我」加进副本列表
    Offset:      uint64(addr) + offset,
}
payload.Shards[taskID].ReplicaList = append(payload.Shards[taskID].ReplicaList, replicaLocation)
```

> —— [core.go:432-438](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/core.go#L432-L438)

登记时本地目录记的是 `IsGold: false`（[core.go:446-452](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/core.go#L446-L452)），区分「我是做种者」和「我只是个副本」。

> **直觉小结**：`GetReplica` 不是「下载完就结束」的一次性操作，而是「下载 + 自我宣告成源」的复合动作。这正是 BitTorrent「下载者即上传者」的语义。所以文档里强调：调过 `GetReplica` 之后，本节点也会被别人拉取，除非显式 `DeleteReplica`。

#### 4.1.5 核心流程：Unregister / DeleteReplica（停止供源）

两个「停止对外供源」的 API，区别在于调用者身份：

- **`Unregister`**：做种者（Gold）调用。它把每个 shard 的 `Gold` **清空**（`payload.Shards[index].Gold = nil`，[core.go:210-212](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/core.go#L210-L212)），并注销本地内存。此时如果还有 Replica 存在，对象在 etcd 里依然可被拉取（从副本拉）；如果连副本也没了，`Update` 发现 `payload.IsEmpty()` 会直接把 key 删掉（见 4.2.4）。
- **`DeleteReplica`**：副本持有者调用。它只从每个 shard 的 `ReplicaList` 里**移除本节点**（保留其它副本，[core.go:482-490](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/core.go#L482-L490)），再注销本地内存。

两者都用「读 → 改 → 乐观锁写」的循环（见 4.2.4），以应对并发修改。

#### 4.1.6 核心流程：List（全局清单）

`List` 直接委托给 etcd 的前缀范围查询，把命中的 `Payload` 转成精简的 `PayloadInfo` 返回：

```go
func (store *P2PStore) List(ctx context.Context, namePrefix string) ([]PayloadInfo, error) {
    var result []PayloadInfo
    payloadList, err := store.metadata.List(ctx, namePrefix)
    ...
}
```

> —— [core.go:239-255](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/core.go#L239-L255)

它体现了「etcd 就是全局目录」——任何一个节点都能在不联系任何「master」的情况下，列出集群里所有的对象。

#### 4.1.7 源码精读小结

把四个 API 的语义列成一张表：

| API | 谁调用 | 对 etcd 元数据的影响 | 对数据的影响 | BitTorrent 类比 |
|---|---|---|---|---|
| `Register` | 做种者 | 新建 key，写 `Gold` | 无（不传数据） | 做种 / seeding |
| `GetReplica` | 下载者 | 读 + 给 shard 追加 `Replica` | 从某源点对点拉取 | 下载即做种 |
| `Unregister` | 做种者 | 清空 `Gold`，可能删 key | 注销本地内存 | 撤种 |
| `DeleteReplica` | 副本持有者 | 移除自己的 `Replica` | 注销本地内存 | 停止做种 |
| `List` | 任意节点 | 只读 | 无 | 查看蜂群清单 |

#### 4.1.8 代码实践：跟踪 Register → GetReplica 的元数据演变

这是一个**源码阅读型实践**，目标是亲手验证「Gold 与 Replica 如何随操作变化」。

1. **实践目标**：用一个具体的 2 节点场景，追踪 etcd 里某对象元数据在 Register / GetReplica 前后的字段变化。
2. **操作步骤**：
   - 阅读 [core.go:123-192](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/core.go#L123-L192)（`Register`）和 [core.go:424-465](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/core.go#L424-L465)（`updatePayloadMetadata`）。
   - 假设做种者 `10.0.0.2:12345` 对一个 128MB、`maxShardSize=64MB` 的对象调用 `Register`。回答：此时 `Shards` 数组有几个元素？每个元素的 `Gold` 和 `replica_list` 分别是什么？
   - 再假设下载者 `10.0.0.3:12346` 调用 `GetReplica` 成功。回答：`updatePayloadMetadata` 执行后，每个 shard 的 `replica_list` 变成什么？
3. **需要观察的现象**：`Gold` 段名始终是做种者 `10.0.0.2`；GetReplica 之后 `replica_list` 多出一条段名为 `10.0.0.3` 的位置。
4. **预期结果**：
   - Register 后：`shards` 有 2 个（128MB / 64MB）；每个 `gold=[{segment_name:"10.0.0.2:12345", offset:...}]`，`replica_list=[]`。
   - GetReplica 后：每个 shard 的 `replica_list=[{segment_name:"10.0.0.3:12346", offset:...}]`。
5. **待本地验证**：若要实测，需先编译 `p2p-store-example`（见 4.3），并启动一个本地 etcd（`etcd` 默认监听 `localhost:2379`），再用 `etcdctl get mooncake/checkpoint/foo/bar` 观察真实 JSON。

#### 4.1.9 小练习与答案

**练习 1**：如果做种者 `Unregister` 之后，没有任何节点曾经 `GetReplica` 过，etcd 里这个 key 还在吗？

**参考答案**：不在。`Unregister` 清空所有 shard 的 `Gold` 后，`ReplicaList` 本来就为空，于是 `Payload.IsEmpty()` 返回 `true`（[metadata.go:100-107](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/metadata.go#L100-L107)），`Update` 走删除分支把 key 删掉（[metadata.go:162-170](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/metadata.go#L162-L170)）。

**练习 2**：`GetReplica` 内部有一个 `for` 循环，在 `doGetReplica` 之后会重新 `Get` 一次元数据并判断 `isSubsetOf`（[core.go:345-361](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/core.go#L345-L361)）。这个循环解决什么问题？

**参考答案**：解决「拉数据期间元数据被别人改了」的并发问题。如果在拉取过程中，别的节点并发 `Unregister` 或 `DeleteReplica` 导致某些源消失了，`revision` 会变化且旧 payload 不再是新的子集，于是需要重拉，保证拉到的数据仍然一致。

### 4.2 元数据/目录：etcd 的乐观锁与本地 Catalog

#### 4.2.1 概念说明

P2P Store 的「无 master」之所以能成立，关键在于 etcd 同时承担了两件事：

1. **全局目录**：谁有什么对象、对象分了哪些块、每块在哪些节点（`Get`/`List`）。
2. **并发安全的更新通道**：多个节点会同时往同一个对象的 `ReplicaList` 里追加自己的位置（GetReplica）、或同时移除自己（DeleteReplica）。如果没有并发控制，后写会覆盖先写，丢失别人的更新。P2P Store 用 etcd 的 **`ModRevision` + `Txn` 事务**实现了**乐观并发控制（OCC）**，无需任何分布式锁。

本地一侧的 `Catalog`（catalog.go）则只是一张进程内的 `map`，记录本进程持有哪些对象，避免重复操作、记住本地内存句柄。

#### 4.2.2 数据模型：Payload / Shard / Location

etcd 里每个对象对应一个 JSON，结构在源码注释里画得很清楚（[metadata.go:27-44](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/metadata.go#L27-L44)）。对应的 Go 类型：

```go
type Location struct {
    SegmentName string `json:"segment_name"`  // 哪个节点（= TE 的 segment 名）
    Offset      uint64 `json:"offset"`        // 该节点内存中的偏移
}

type Shard struct {
    Length      uint64     `json:"size"`
    Gold        []Location `json:"gold"`         // 原始副本（做种者）
    ReplicaList []Location `json:"replica_list"` // 后来的副本（下载者升级而来）
}

type Payload struct {
    Name         string   `json:"name"`
    Size         uint64   `json:"size"`
    SizeList     []uint64 `json:"size_list"`
    MaxShardSize uint64   `json:"max_shard_size"`
    Shards       []Shard  `json:"shards"`
}
```

> —— [metadata.go:46-63](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/metadata.go#L46-L63)

要点：

- `SegmentName` 复用了 TE 的 segment 概念——它就是 `localServerName`（`ip:port`）。也就是说，「Location 指向哪个节点」和「TE 怎么打开那个节点的 segment」是同一套命名。
- `Gold` 与 `ReplicaList` 的区分是 P2P Store 区分「做种者」和「副本」的唯一依据，也决定了选源策略和 `Unregister`/`DeleteReplica` 的不同行为。

#### 4.2.3 核心流程：Create / Put / Get / List

四个基本操作直接映射 etcd 原语：

- **`Create`**：用 `Txn` 保证「仅当 key 不存在（Version=0）才写入」，等价于原子 `INSERT IF NOT EXISTS`：

```go
txnResp, err := metadata.etcdClient.Txn(ctx).
    If(clientv3.Compare(clientv3.Version(key), "=", 0)).
    Then(clientv3.OpPut(key, string(jsonData))).
    Commit()
```

> —— [metadata.go:131-148](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/metadata.go#L131-L148)

这保证 `Register`（非 `forceCreate`）时不会覆盖别人已注册的同名对象。

- **`Put`**：无条件覆盖（[metadata.go:150-158](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/metadata.go#L150-L158)），用于 `forceCreate=true` 的 Register。
- **`Get`**：读 key，**同时返回 `ModRevision`**（[metadata.go:187-202](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/metadata.go#L187-L202)）。这个 revision 是乐观锁的关键，下面马上用到。
- **`List`**：用前缀范围扫描（`startRange ~ startRange+0xFF`，[metadata.go:204-221](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/metadata.go#L204-L221)）。`0xFF` 作为上界是个常见技巧：因为 etcd 的 `WithRange` 是「左闭右开」，用一个比任何合法字符都大的字节当上界，就能匹配「所有以该前缀开头」的 key。

#### 4.2.4 核心流程：Update 的乐观并发控制（OCC）

这是本模块最值得精读的部分。`Update` 的并发模型是经典的「读时拿版本号 → 写时 CAS 版本号」：

```go
func (metadata *Metadata) Update(ctx context.Context, name string, payload *Payload, revision int64) (bool, error) {
    key := metadata.keyPrefix + name
    if payload.IsEmpty() {
        // 所有副本都没了 → 删除 key（同样带 CAS）
        txnResp, err := metadata.etcdClient.Txn(ctx).
            If(clientv3.Compare(clientv3.ModRevision(key), "=", revision)).
            Then(clientv3.OpDelete(key)).
            Commit()
        ...
        return txnResp.Succeeded, nil
    } else {
        // 正常更新（带 CAS）
        txnResp, err := metadata.etcdClient.Txn(ctx).
            If(clientv3.Compare(clientv3.ModRevision(key), "=", revision)).
            Then(clientv3.OpPut(key, string(jsonData))).
            Commit()
        ...
        return txnResp.Succeeded, nil
    }
}
```

> —— [metadata.go:160-185](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/metadata.go#L160-L185)

`If(ModRevision(key) == revision)` 的语义是：「**只有当这个 key 当前的修改版本号，等于我读出来的那个版本号时，才执行 Then**」。也就是说：如果在我读出来之后、写回去之前，有别人改过这个 key，那 `ModRevision` 已经变了，事务失败，`Succeeded=false`。

调用方（如 `updatePayloadMetadata`、`Unregister`、`DeleteReplica`）拿到 `Succeeded=false` 后怎么办？看一个典型的重试循环：

```go
for {
    // ... 修改 payload ...
    success, err := store.metadata.Update(ctx, name, payload, revision)
    ...
    if success {
        // 写成功，收工
        ...
        return nil
    } else {
        // 别人抢先改了 → 重新读最新值和版本号，再来一次
        payload, revision, err = store.metadata.Get(ctx, name)
        ...
    }
}
```

> —— [core.go:424-464](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/core.go#L424-L464)

这就是**无锁并发更新**的完整套路：用 etcd 事务当原子 CAS，失败就重读重试，最终所有人追加的副本位置都不会丢。代价是高并发下可能多次重试，但对「广播 checkpoint」这种写并发不算极端的场景完全够用。

> **公式化**：设两个节点同时 `GetReplica`，各自读到 revision \(r\)，各自追加自己的位置后尝试 `Update(_, _, r)`。etcd 只会让其中一个成功（其 CAS 把 revision 推进到 \(r+1\)），另一个的 `If(ModRevision==r)` 失败，于是重读到 \(r+1\)，在已含对方副本的新值上再追加自己，再次 CAS 到 \(r+2\)。最终结果 \(\to\) 两个副本位置都被保留。这就替代了「Master 串行化处理」。

#### 4.2.5 本地目录 Catalog

`Catalog` 极其简单，就是一个带 `sync.Mutex` 的 `map[string]CatalogParams`：

```go
type Catalog struct {
    entries map[string]CatalogParams
    mu      sync.Mutex
}
```

> —— [catalog.go:26-29](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/catalog.go#L26-L29)

它提供 `Contains`/`Get`/`Add`/`Remove` 四个方法，唯一作用是：

- `Register`/`GetReplica` 开头用 `catalog.Contains(name)` 拦截「同一对象在本进程被重复打开」（返回 `ErrPayloadOpened`，[core.go:134-136](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/core.go#L134-L136)）。官方文档明确：「A file can only be pulled once on the same P2PStore instance.」
- `Unregister`/`DeleteReplica` 用 `catalog.Get` 拿回当初记录的 `AddrList`/`SizeList`，据此注销本地内存。

它**不参与跨节点一致性**，纯粹是单进程内的状态备忘。

#### 4.2.6 代码实践：观察乐观锁的重试

1. **实践目标**：理解高并发 `GetReplica` 时 `updatePayloadMetadata` 的重试行为。
2. **操作步骤**：
   - 阅读 [core.go:424-465](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/core.go#L424-L465)，在 `if success { ... } else { ... }` 的 `else` 分支里**脑内**（或本地fork后）加一行日志，例如 `log.Printf("CAS failed, retrying for %s", name)`。
   - 设想 3 个节点 A/B/C 几乎同时对一个新对象 `GetReplica`（此时只有 Gold=做种者）。
3. **需要观察的现象**：理论上至少有一个节点会进入 `else` 分支重试（因为它读到的 revision 被另一个节点的成功 CAS 推进了）。
4. **预期结果**：3 个节点最终都能成功，最终 etcd 里每个 shard 的 `replica_list` 含 3 个不同的 `segment_name`，不会因为并发覆盖而丢失任何一个。**待本地验证**（需要真实多进程 + etcd 环境）。
5. **注意**：本实践若改源码加日志，请在自己的 fork 中进行；本仓库禁止修改源码，仅作理解用途。

#### 4.2.7 小练习与答案

**练习 1**：`Update` 为什么要区分 `payload.IsEmpty()` 走 `OpDelete`、否则走 `OpPut`？

**参考答案**：当一个对象的所有 `Gold` 和 `ReplicaList` 都为空时，说明集群里已经没有任何节点持有它的数据，这个 key 已无意义，直接删除可以避免 etcd 里堆积「空对象」垃圾。`IsEmpty()` 就是逐 shard 检查 `Gold` 和 `ReplicaList` 是否都为空（[metadata.go:100-107](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/metadata.go#L100-L107)）。

**练习 2**：如果 etcd 本身挂了，P2P Store 还能完成一次 `GetReplica` 吗？为什么？

**参考答案**：不能。因为 `GetReplica` 的第一步就是 `metadata.Get` 读元数据（[core.go:338-344](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/core.go#L338-L344)），拿不到「数据在哪个节点」就无从发起 TE 传输。这正是「无 master = 把控制面完全交给 etcd」的代价：etcd 是唯一的控制面依赖，也是单点风险所在（生产环境用 etcd 集群缓解）。

### 4.3 示例与数据面：cgo 桥接 Transfer Engine

#### 4.3.1 概念说明

P2P Store 用 Go 写控制面（etcd 交互、分片调度、乐观锁），但**数据面完全复用 C++ 的 Transfer Engine**——因为高带宽 RDMA 传输的实现天然属于 C++。Go 和 C++ 之间通过 **cgo** 桥接：`transfer_engine.go` 用 `import "C"` 调用 TE 暴露的 C 接口（`transfer_engine_c.h`）。

这种「Go 控制面 + C++ 数据面」的分工很常见：Go 拿来快速写并发编排和元数据逻辑，C++ 负责性能敏感的内存注册和 RDMA 收发。

#### 4.3.2 核心流程：本地内存的分块并发注册

在讲示例之前，先看 `RegisteredMemory.Add`——它解释了为什么 `MAX_CHUNK_SIZE` 那么重要。内存注册（让一段内存可被 RDMA 远端访问）是**慢操作**，所以大 buffer 被切成 `maxChunkSize`（4GiB）的块，**每块一个 goroutine 并发注册**：

```go
for offset := uint64(0); offset < length; offset += memory.maxChunkSize {
    chunkSize := memory.maxChunkSize
    if chunkSize > length-offset {
        chunkSize = length - offset
    }
    wg.Add(1)
    go func(offset, chunkSize uint64) {
        defer wg.Done()
        baseAddr := addr + uintptr(offset)
        err := memory.engine.registerLocalMemory(baseAddr, chunkSize, location)
        ...
    }(offset, chunkSize)
}
```

> —— [registered_memory.go:71-95](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/registered_memory.go#L71-L95)

它还做了引用计数（`refCount`）和重叠检测（`ErrAddressOverlapped`，[registered_memory.go:41-60](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/registered_memory.go#L41-L60)）：同一段内存被多次 `Add` 只会增加计数，`Remove` 时减到 0 才真正注销——这样 `Register` 和 `GetReplica` 复用同一段内存时不会重复注册。

#### 4.3.3 核心流程：performTransfer 的一次 TE 读

`performTransfer` 展示了「拉一个 shard」如何翻译成 TE 的调用序列。一次成功读取的步骤是：

1. `allocateBatchID(1)`：申请一个大小为 1 的传输 batch。
2. `openSegment(location.SegmentName, retryCount==0)`：打开源节点的 segment 拿到 `targetID`。第一次尝试用缓存版本（`openSegment`），重试用不缓存版本（`openSegmentNoCache`，[transfer_engine.go:170-187](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/transfer_engine.go#L170-L187)）。
3. 构造一个 `OPCODE_READ` 请求：从源 segment 的 `TargetOffset` 读 `Length` 字节，写到本地 `source` 地址。
4. `submitTransfer` + 轮询 `getTransferStatus` 直到完成。

```go
request := TransferRequest{
    Opcode:       OPCODE_READ,        // 读：把远端数据拉到本地
    Source:       uint64(source),     // 本地目标地址
    TargetID:     targetID,           // 远端 segment ID
    TargetOffset: location.Offset,    // 远端偏移
    Length:       shard.Length,
}
err = store.transfer.submitTransfer(batchID, []TransferRequest{request})
```

> —— [core.go:383-394](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/core.go#L383-L394)

> **方向辨析**：`OPCODE_READ` 在 TE 语义里是「**从远端 segment 读到本地**」（远端→本地），`OPCODE_WRITE` 是「把本地写到远端 segment」。P2P Store 的下载者用 READ 把数据「拉」到自己这里。

#### 4.3.4 核心流程：P2P handshake 与动态端口

值得一提的是 TE 支持 `metadata_conn_string == "P2PHANDSHAKE"` 模式——此时不需要任何外部 metadata server，节点间直接握手，RPC 端口动态分配。`GetLocalIpAndPort` 就是为了在这种模式下让调用者拿到实际监听地址去告诉对端：

```go
// GetLocalIpAndPort returns the local IP address and port that the
// TransferEngine is listening on. This is particularly useful in P2P
// handshake mode (metadata_conn_string == "P2PHANDSHAKE"), where the
// RPC port is dynamically assigned at initialization time and callers
// need to discover the actual listening address to share with peers.
```

> —— [transfer_engine.go:205-222](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/transfer_engine.go#L205-L222)

不过 P2P Store 的示例默认仍用 etcd（见 4.3.5），P2P handshake 是 TE 层提供的另一种「更轻」的元数据模式（u7 单元有专门讲解）。

#### 4.3.5 示例程序：trainer 做种 + inferencer 拉取

`p2p-store-example.go` 把 P2P Store 包装成一个命令行 demo，模拟「训练完成后把模型权重广播给一批推理节点」。它用 `-cmd` 区分两种角色：

- **trainer**（`doTrainer`）：`mmap` 一块匿名内存当「模型文件」→ `Register`（做种）→ `List` 打印清单 → `sleep 100s`（留时间给 inferencer 来拉）→ `Unregister`（撤种）。

```go
addr, err := syscall.Mmap(-1, 0, fileSize, syscall.PROT_READ|syscall.PROT_WRITE, syscall.MAP_ANON|syscall.MAP_PRIVATE)
...
err = store.Register(ctx, name, addrList, sizeList, MAX_SHARD_SIZE, MEMORY_LOCATION, true)
...
fmt.Println("Idle for 100 seconds, now you can start another terminal to simulate inference")
time.Sleep(100 * time.Second)
err = store.Unregister(ctx, name)
```

> —— [p2p-store-example.go:71-123](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/example/p2p-store-example.go#L71-L123)

- **inferencer**（`doInferencer`）：`mmap` 同样大小的空内存 → `GetReplica`（拉取，拉完自动成源）→ `DeleteReplica`（停止对外供源，释放内存）。

```go
err = store.GetReplica(ctx, name, addrList, sizeList)
...
err = store.DeleteReplica(ctx, name)
```

> —— [p2p-store-example.go:160-194](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/example/p2p-store-example.go#L160-L194)

命令行参数（[p2p-store-example.go:40-47](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/example/p2p-store-example.go#L40-L47)）：`--metadata_server`（默认 `localhost:2379`）、`--local_server_name`（默认取 hostname）、`--device_name`（默认 `mlx5_2`）、`--file_size_mb`（默认 2048）、`--nic_priority_matrix`（高级）。

#### 4.3.6 代码实践：画出蜂群分发流程图（本讲核心实践）

这是讲义规格里要求的综合实践，目标是把「无 master 对等分发」可视化，并与中心化 Store 对比。

1. **实践目标**：阅读 [core.go](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/core.go) 与 [p2p-store-example.go](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/example/p2p-store-example.go)，画出「一个对象被 Register 后，多个节点相互 GetReplica（拉到的还能被再拉）」的数据分发流程图。
2. **操作步骤**：
   - 假设 1 个 trainer（T）+ 3 个 inferencer（A、B、C），对象分 4 个 shard，初始 `Gold=[T]`。
   - 标注每一步「谁读了 etcd、谁从谁那里拉、拉完谁把自己写回 etcd」。
   - 参考选源策略 [metadata.go:77-87](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/metadata.go#L77-L87)（优先副本、随机分散）。
3. **需要观察的现象**：随着 A/B/C 陆续完成 GetReplica，可供选择的源从「只有 T」逐步变成「T + 已完成的副本」，越靠后的节点越可能从「非 T」的副本拉，T 的上行压力逐步下降。
4. **预期结果（参考流程图）**：

```text
          ┌──────────── etcd (全局目录 + 乐观锁) ────────────┐
          │  foo/bar: shards[*].gold=[T], replica_list=[…]   │
          └──────────────────────────────────────────────────┘
              ▲ 写Gold         ▲ 读+写Replica       ▲ 读+写Replica       ▲ 读+写Replica
              │ Register        │ GetReplica          │ GetReplica          │ GetReplica
        ┌─────┴────┐       ┌───┴────┐           ┌───┴────┐           ┌───┴────┐
        │ trainer T│       │   A    │           │   B    │           │   C    │
        │ (做种者) │       │(副本①)│           │(副本②)│           │(副本③)│
        └─────┬────┘       └───┬────┘           └───┬────┘           └────────┘
              │   TE READ       │ TE READ             │ TE READ
              │<────────────────│ (A 从 T 拉)         │
              │                 │                     │ (B 从 T 或 A 拉, 随机)
              │                 │<────────────────────│
              │                 │                     │
   注：C 完成时 replica_list 已含 A、B, C 很可能从 A 或 B 拉, T 几乎不再被压
```

5. **对比中心化 Store（u5-l1）的差异**：填下表（答案见「综合实践」第 3 节）。

| 维度 | 中心化 Mooncake Store | P2P Store |
|---|---|---|
| 控制面 | ? | ? |
| 数据源 | ? | ? |
| 副本/淘汰 | ? | ? |
| 适用场景 | ? | ? |

6. **待本地验证**：若要实跑，按 [docs/source/design/p2p-store.md](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/docs/source/design/p2p-store.md) 的步骤：先 `cmake .. -DWITH_P2P_STORE=ON && make -j` 编出 `build/mooncake-p2p-store/p2p-store-example`，启动 etcd，在一个终端跑 `--cmd=trainer`，在另外几个终端跑 `--cmd=inferencer`，观察吞吐（程序会打印 `throughput (GB/s)`）。**注意示例目前仅支持 RDMA，需要真实 RNIC 环境。**

#### 4.3.7 小练习与答案

**练习 1**：示例里 trainer 用 `syscall.Mmap` 分配内存，而不是 `make([]byte, ...)`。为什么？

**参考答案**：`Mmap`（`MAP_ANON|MAP_PRIVATE`）分配的是页对齐、地址稳定的大段匿名内存，且其地址可以被安全地传给 TE 做内存注册（`registerLocalMemory` 需要真实虚拟地址）。`make` 出的切片底层数组地址在 GC/扩容时可能变化，不适合长期注册给 RDMA。结束时也用配对的 `Munmap` 释放。

**练习 2**：为什么 `performTransfer` 第一次 `openSegment` 用缓存版本，重试时用不缓存版本？

**参考答案**：正常路径下源 segment 是稳定的，用缓存的 `openSegment` 可以复用已建立的连接、避免重复握手开销。但重试通常意味着上一次传输失败——可能是源 segment 信息过期（比如那个节点重启、换了端口），所以重试用 `openSegmentNoCache` 强制重新解析、绕过可能脏掉的缓存，提高恢复成功率（[transfer_engine.go:170-187](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/transfer_engine.go#L170-L187)）。

### 4.4 演进关系：P2P Store 与 checkpoint-engine

#### 4.4.1 概念说明

`mooncake-p2p-store` 是一个**教学/参考实现**——代码量小、逻辑清晰，适合理解「无 master 对等分发」的思想。但生产环境里，Mooncake 团队把它演进成了一个独立的、更高性能的项目 **`checkpoint-engine`**。仓库 README 明确记录了这一演进：

> **Sept 10, 2025**: The official & high-performance version of Mooncake P2P Store is open-sourced as [checkpoint-engine](https://github.com/MoonshotAI/checkpoint-engine/). It has been successfully applied in K1.5 and K2 production training, updating Kimi-K2 model (1T parameters) across thousands of GPUs in ~20s.
> —— [README.md:54](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/README.md#L54)

也就是说：

- **本仓库里的 `mooncake-p2p-store`**：保留作为参考实现，用来展示「无 master + etcd 元数据 + BitTorrent 式 Register/GetReplica」的核心设计。
- **`checkpoint-engine`（独立仓库）**：同一思想的工业级版本，针对大规模训练（千卡级别、1T 参数模型）做了深度性能优化，已在 Kimi K1.5/K2 生产训练中落地（约 20 秒更新 Kimi-K2 的 1T 参数）。

#### 4.4.2 为什么要演进成独立项目？

从本讲读到的源码可以推测几个优化方向（这些是 checkpoint-engine 在生产中真正要解决的，本参考实现并未深究）：

- **选源策略**：本实现只是「优先副本、随机挑」（[metadata.go:77-87](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/metadata.go#L77-L87)）。千卡场景下需要更聪明的调度（按拓扑就近、按带宽负载均衡），否则跨交换机拉取会成瓶颈。
- **元数据扩展性**：本实现把「每个 shard 的全量 Gold+ReplicaList」序列化成**单个 etcd value**（[metadata.go:150-158](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/metadata.go#L150-L158)）。当副本数上千时，单 value 会膨胀，每次 `Update` 都要全量重写——这在千卡规模下会成为 etcd 的压力点。
- **并发更新热度**：本实现的乐观锁在「大量节点同时 GetReplica」时会频繁 CAS 失败重试（4.2.6）。生产实现通常会用分片元数据或更细粒度的并发控制来降低冲突。

这些正是把 P2P Store「思想」带向生产规模时必须做的工程化，也就是 checkpoint-engine 存在的意义。**学习本讲，是为了理解 checkpoint-engine 背后的核心设计原型。**

## 5. 综合实践

把本讲三个最小模块（P2P Store 核心、元数据/目录、示例）串起来，完成下面这个任务。

**任务：完整复现一次「1 做种 + 多下载」的蜂群分发，并解释每一步的元数据与数据流。**

1. **源码精读（核心 + 元数据模块）**：从 [core.go:329-362](https://github.com/kvcache-ai/Mooncake/blob/ef0312f80ab524f023878ffcd37cef111726cf26/mooncake-p2p-store/src/p2pstore/core.go#L329-L362) 的 `GetReplica` 出发，写出它的三步流程（读元数据 → 并发拉每个 shard → 回写自己为副本），并指出每一步分别调用了 `Metadata` 的哪个方法、用了 etcd 的哪个特性（普通 Get / CAS Update / 前缀 List）。

2. **画流程图（示例模块）**：完成 4.3.6 的蜂群分发流程图，把 T/A/B/C 四个节点在 4 个 shard 上的拉取关系画清楚，标注「第一次只能从 T 拉、之后可从任意副本拉」。

3. **填对比表（对照 u5-l1）**：补全 4.3.6 末尾的对比表。参考答案：

   | 维度 | 中心化 Mooncake Store | P2P Store |
   |---|---|---|
   | 控制面 | 独立的 `MasterService` 进程，集中调度 | 无 master，etcd 当共享目录 + 乐观锁 |
   | 数据源 | Master 分配的若干目标副本段 | 任意持有 Gold/Replica 的对等节点（BitTorrent 式） |
   | 副本/淘汰 | 有副本数管理、LRU 淘汰、多级存储、租约/pin | 无淘汰、无多级、无租约；只增删副本位置 |
   | 适用场景 | 通用分布式 KV 缓存（在线推理 KV pool） | 大对象一次性广播（checkpoint / 权重分发） |

4. **反思题**：如果让你把 P2P Store 用在「在线推理的常驻 KV cache 池」（像中心化 Store 那样高频随机读写），它会缺什么？（提示：没有按 key 读写、没有容量回收、没有淘汰、每次 GetReplica 只能拉一次。）

## 6. 本讲小结

- **P2P Store 是「无 master」的对等存储**：没有中心化的 `MasterService`，控制面完全交给 etcd，数据面复用 C++ Transfer Engine（Go 通过 cgo 桥接）。
- **核心是 BitTorrent 式语义**：`Register`=做种（只写 etcd 元数据，Gold 指向自己，不传数据）；`GetReplica`=拉取并自动把自己登记成新副本（写入 ReplicaList），下载者即上传者。
- **选源策略是蜂群的关键**：`getRandomLocation` 优先从 ReplicaList 随机挑、挑不到才用 Gold，从而把压力从做种者转移到副本，节点越多分发越快。
- **etcd 同时扮演「全局目录」和「乐观锁通道」**：`Get`/`List` 提供发现能力；`Update` 用 `ModRevision` 的 `Txn` CAS 实现「读-改-写」无锁并发，失败则重读重试，保证多节点并发追加副本不丢失。
- **本地 `Catalog` 只做进程内备忘**：防重复打开、记住本地内存句柄，不参与跨节点一致性。
- **演进关系**：`mooncake-p2p-store` 是参考实现；其高性能工业版本已独立为 `checkpoint-engine`（2025-09-10 开源），用于 Kimi K1.5/K2 千卡训练的 checkpoint 分发。本仓库的 P2P Store 是理解 checkpoint-engine 核心设计的原型。

## 7. 下一步学习建议

- **对照中心化 Store**：回到 [u5-l1](u5-l1-store-architecture.md) 重读 Master 的控制面职责，体会「为什么 P2P Store 能省掉 Master、代价是什么」。重点比较 P2P Store 的 etcd 乐观锁与 Store Master 的集中式分配。
- **深入数据面**：P2P Store 的 `performTransfer` 只是 TE 的薄封装。想搞懂「batch/segment/READ 真正怎么跑」，继续读 u2 单元（尤其是 [u2-l1](u2-l1-te-architecture-core.md) 的 TE 架构、u2-l5 的 segment 管理）。
- **元数据后端**：本讲的 etcd 只是 TE 支持的元数据后端之一。u7-l3 会讲 P2P handshake / HTTP / etcd / Redis 四种元数据后端的选择与权衡——其中 P2P handshake 模式（本讲 4.3.4 提到的动态端口）甚至能连 etcd 都省掉。
- **走向生产**：读完本讲，建议去 [`checkpoint-engine`](https://github.com/MoonshotAI/checkpoint-engine/) 看它如何在「同样的思想」上解决千卡规模下的选源、元数据分片、并发冲突等工程问题——这是本参考实现有意省略的部分。

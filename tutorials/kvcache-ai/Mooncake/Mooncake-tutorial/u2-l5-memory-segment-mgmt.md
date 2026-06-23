# 内存注册与段（Segment）生命周期管理

> 所属单元：第 2 单元 · Transfer Engine 核心机制
> 学习阶段：intermediate
> 依赖讲义：[u2-l1 TransferEngine 架构与核心抽象](u2-l1-te-architecture-core.md)

## 1. 本讲目标

学完本讲后，你应该能够：

1. 准确使用 `registerLocalMemory` 的五个参数：`addr`、`length`、`location`、`remote_accessible`、`update_metadata`，并能说清后三个参数分别影响什么。
2. 说清楚 **「段（Segment）」从打开（open）到关闭（close）再到移除（remove）** 的完整生命周期，以及它和元数据服务之间的「发布—更新—移除」关系。
3. 区分两条容易混淆的线：**单条内存注册**（每次都同步更新一次元数据服务）与 **批量内存注册**（先在本地累积、最后一次性发布元数据）。
4. 读懂批量注册基准脚本 `batch_register_bench.py`，并能动手写一个批量注册脚本，观察注册条目数量、耗时，以及 `unregister` 之后远端是否还能访问。

一句话总结：本讲解答「**我有一块内存，怎么把它登记进 Mooncake，让对端能看见、能访问？登记完之后，它的生命周期又是怎样流转的？**」

---

## 2. 前置知识

本讲假设你已经：

- 读完 u2-l1，知道 `TransferEngine` 是一个门面（facade），真正干活的是 `TransferEngineImpl`，它内部又持有一个 `TransferMetadata` 和若干个 `Transport`。
- 最好读过 [u2-l2 TransferMetadata：段与缓冲区的元数据协调](u2-l2-transfer-metadata.md)，知道「段描述符 `SegmentDesc`」和「缓冲区描述符 `BufferDesc`」长什么样、如何被编码成 JSON 写进元数据服务。

如果你对下面几个名词还不熟，先看这里的 30 秒解释：

| 名词 | 通俗解释 |
| --- | --- |
| Segment（段） | 一个进程向集群「登记」出来的一个逻辑身份，名字通常是 `ip:port`。一个 `TransferEngine` 实例通常只有 **一个本地段**，它的 ID 恒为 `LOCAL_SEGMENT_ID = 0`。 |
| Buffer（缓冲区） | 段内的一段 **具体内存**（连续地址 + 长度）。`registerLocalMemory` 注册的就是一个 buffer，它会作为一个条目被追加进「本地段」的 `buffers` 列表里。 |
| 内存注册（Memory Registration） | 把一段用户内存「登记」给传输层。对 RDMA 来说，这一步会向网卡申请 `lkey`/`rkey`（本地/远端访问密钥），并把内存 **pin 住**（禁止换页）。 |
| 元数据服务 | 集中式 KV 存储（etcd / redis / HTTP）或点对点握手。段描述符（含其中所有 buffer 条目）就写在这里，供对端读取。 |

> ⚠️ **本讲最容易踩的概念坑**：「注册一块内存」**不会**在元数据服务里新增一个「段」。
> 每个 `TransferEngine` 只有 **一个本地段**（`LOCAL_SEGMENT_ID = 0`，名字是 `local_server_name`）。
> 你每次调用 `registerLocalMemory`，都是往这 **同一个段** 的 `buffers` 列表里 **追加一个 buffer 条目**，然后把这个段描述符（带着更新后的 `buffers`）整体重新写回元数据服务。
> 所以「批量注册 N 块内存」=「本地段的 `buffers` 列表从 0 长到 N」，而元数据服务里的 **段 key 数量始终是 1**。这一点请牢记，下面的源码和实践都围绕它展开。

---

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [transfer_engine.h](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/transfer_engine.h) | 对外门面类 `TransferEngine` 的声明，列出了 `registerLocalMemory`、`openSegment`、`closeSegment`、`registerLocalMemoryBatch` 等全部公开 API。 |
| [transfer_engine_impl.h](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/transfer_engine_impl.h) | `TransferEngineImpl` 的声明，包含私有结构体 `MemoryRegion` 和本地内存登记表 `local_memory_regions_`。 |
| [transfer_engine_impl.cpp](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_engine_impl.cpp) | 内存注册/注销、段打开/关闭/移除、重叠检测、批量注册的实现主体。本讲最核心的文件。 |
| [transport/transport.h](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/transport/transport.h) | `Transport` 抽象基类，定义了 `SegmentID`/`SegmentHandle` 类型、`BufferEntry` 结构，以及 `registerLocalMemory` 等 **私有纯虚函数**（由各具体传输实现）。 |
| [rdma_transport.cpp](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/rdma_transport.cpp) | RDMA 传输对内存注册的具体实现。用来讲解「注册一块内存时网卡层到底做了什么」，以及批量注册如何避免「逐块更新元数据」。 |
| [transfer_metadata.cpp](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp) | `addLocalMemoryBuffer`/`updateLocalSegmentDesc`/`removeLocalMemoryBuffer` 等：内存条目如何被写进段描述符并发布到元数据服务。 |
| [batch_register_bench.py](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/example/batch_register_bench.py) | 官方批量注册基准脚本，本讲实践环节的参考模板。 |

下面进入核心讲解。我们按「**单条注册 API → 段生命周期 → 批量注册**」三个最小模块依次展开。

---

## 4. 核心概念与源码讲解

### 4.1 内存注册 API：registerLocalMemory 的五个参数

#### 4.1.1 概念说明

在 Mooncake 里，**「注册一块内存」是让对端能看见并访问这块内存的前置条件**。没注册过的内存，网卡拿不到它的密钥，远端也就无法对它发起 RDMA 读写。

对外暴露的注册入口是 `TransferEngine::registerLocalMemory`，它的完整签名如下（参数都带默认值，方便简单调用）：

```cpp
int registerLocalMemory(void* addr, size_t length,
                        const std::string& location = kWildcardLocation,
                        bool remote_accessible = true,
                        bool update_metadata = true);
```

五个参数的含义：

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `addr` | （必填） | 待注册内存的起始地址。 |
| `length` | （必填） | 内存长度（字节）。**不能为 0**。 |
| `location` | `kWildcardLocation`（即 `"*"`） | 这块内存「属于哪里」。常见取值：`"cpu"`、`"cuda:0"`、`"*"`。传 `"*"` 表示让引擎 **自动探测** 内存所在位置（CPU 还是 GPU、落在哪块 numa）。 |
| `remote_accessible` | `true` | 这块内存是否允许 **远端访问**。对 RDMA 而言基本恒为 `true`（要给远端发 `rkey`）；为 `false` 时通常用于「只在本机用的内存」。 |
| `update_metadata` | `true` | 注册完之后，是否 **立刻把更新后的段描述符同步写回元数据服务**。设为 `false` 表示「先别发布，我稍后会批量发布」。**这是批量注册能提速的关键开关**，详见 4.3。 |

> 小贴士：`remote_accessible` 这个参数在当前 RDMA 实现里实际上被忽略了（函数体内有一行 `(void)remote_accessible;`），它更多是为将来「区分本机内存与可远端内存」预留的语义位。但在 `TransferEngineImpl` 的本地登记表里，它会被如实记录下来。

#### 4.1.2 核心流程

调用 `registerLocalMemory(addr, length, location, remote_accessible, update_metadata)` 后，引擎内部依次做这些事：

```
1. 重叠检测：检查 [addr, addr+length) 是否与已注册的某块内存重叠
   → 重叠：返回 ERR_ADDRESS_OVERLAPPED
   → 长度为 0：返回 ERR_INVALID_ARGUMENT
2. 遍历所有已安装的 Transport（rdma / tcp / nvlink ...），
   对每个 transport 调用 transport->registerLocalMemory(...)。
   （RDMA 层在此向网卡注册 MR、收集 lkey/rkey、把 buffer 追加进本地段，
    并根据 update_metadata 决定是否立即发布元数据。）
3. 任一 transport 失败 → 立即返回错误码。
4. 全部成功 → 在本地登记表 local_memory_regions_ 里记下这块内存。
5. 返回 0。
```

注意：第 2 步是「**先去网卡注册、再更新元数据**」，顺序很重要——如果先发布元数据再去网卡注册，对端就可能拿到一个「能看见地址、但没有访问密钥」的半成品 buffer，从而访问失败。

#### 4.1.3 源码精读

先看公开 API 的声明，确认五个参数的默认值：

[mooncake-transfer-engine/include/transfer_engine.h:99-104](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/transfer_engine.h#L99-L104) —— 声明了 `registerLocalMemory` 与 `unregisterLocalMemory`，`location` 默认 `kWildcardLocation`、`remote_accessible`/`update_metadata` 默认 `true`。

再看 `TransferEngineImpl` 里的实现，这是本模块最关键的一段：

[mooncake-transfer-engine/src/transfer_engine_impl.cpp:564-587](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_engine_impl.cpp#L564-L587) —— `registerLocalMemory` 的实现：先做重叠检测与零长检测，再 `for (auto transport : multi_transports_->listTransports())` 逐个 transport 注册（任一失败即返回），最后加锁把 `{addr, length, location, remote_accessible}` 写进本地登记表。

注意它把 `update_metadata` 原样 **透传** 给了 `transport->registerLocalMemory(...)`，真正「是否发布元数据」的决定权在每个 transport 手里。

注销流程是对称的：

[mooncake-transfer-engine/src/transfer_engine_impl.cpp:589-599](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_engine_impl.cpp#L589-L599) —— `unregisterLocalMemory`：逐个 transport 注销，再从本地登记表 `eraseMemoryRegionLocked(addr)` 删除条目。`update_metadata` 同样透传。

本地登记表本身是一张以地址为键的 `std::map`，记录了所有已注册内存区域，供重叠检测使用：

[mooncake-transfer-engine/include/transfer_engine_impl.h:392-399](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/transfer_engine_impl.h#L392-L399) —— 私有结构体 `MemoryRegion { addr, length, location, remote_accessible }` 与 `using MemoryRegionMap = std::map<uintptr_t, MemoryRegion>`，后者即本地内存登记表 `local_memory_regions_` 的类型。

重叠检测用了「红黑树下界查找」的经典技巧：在一个按起始地址有序的 `std::map` 里，判断新区间是否与已存在区间相交，只需检查 `upper_bound(addr)` 的前驱与后继两个候选区间：

[mooncake-transfer-engine/src/transfer_engine_impl.cpp:782-809](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_engine_impl.cpp#L782-L809) —— `hasOverlapLocked`：先看「包含 `addr` 的区间」，再看 `lower_bound(addr)` 指向的下一个区间以及它的前驱，只要任一区间与 `[addr, addr+length)` 相交就判定为重叠。时间复杂度 \( O(\log n) \)。

\[ \text{overlap}(a_1, l_1,\ a_2, l_2) \iff a_1 < a_2 + l_2 \,\land\, a_2 < a_1 + l_1 \]

最后，把视线下沉到 RDMA 传输，看「注册一块内存」在网卡层真正做了什么：

[mooncake-transfer-engine/src/transport/rdma_transport/rdma_transport.cpp:199-320](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/rdma_transport.cpp#L199-L320) —— `RdmaTransport::registerLocalMemoryInternal`：① 必要时对大块内存做并行 pre-touch（加速 pin）；② 在所有 RDMA context 上 `registerMemoryRegion` 拿到 `lkey`/`rkey`；③ 若 `location == "*"` 则自动探测内存位置；④ 组装 `BufferDesc`，调用 `metadata_->addLocalMemoryBuffer(buffer_desc, update_metadata)` 把条目追加进本地段。

关键就是第 ④ 步这一行：

[mooncake-transfer-engine/src/transport/rdma_transport/rdma_transport.cpp:317](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/rdma_transport.cpp#L317) —— `int rc = metadata_->addLocalMemoryBuffer(buffer_desc, update_metadata);` 把 buffer 追加进段描述符，`update_metadata` 决定是否当场发布。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：通过跟踪调用链，把「用户调一次 `register_memory`」到「buffer 出现在段描述符里」的完整路径走通，理解 `update_metadata` 这一个布尔值是如何影响「要不要写元数据服务」的。

**操作步骤**：

1. 在 Python 侧，`engine.register_memory(addr, size)` 直接转调 C++。阅读绑定：

   [mooncake-integration/transfer_engine/transfer_engine_py.cpp:802-806](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L802-L806) —— `registerMemory` 仅把 `uintptr_t` 转成指针，调用 `engine_->registerLocalMemory(buffer, capacity, location)`，**没有传 `remote_accessible`/`update_metadata`**，因此它们都取默认值 `true`。这解释了为什么 Python 用户「注册完即可被远端访问」。

2. 顺着这条链往下读：`TransferEngine::registerLocalMemory`（门面，[transfer_engine.cpp:379-392](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_engine.cpp#L379-L392)）→ `TransferEngineImpl::registerLocalMemory`（4.1.3）→ `RdmaTransport::registerLocalMemoryInternal`（4.1.3）→ `TransferMetadata::addLocalMemoryBuffer`。

3. 打开 `addLocalMemoryBuffer`，看清「追加 + 条件发布」：

   [mooncake-transfer-engine/src/transfer_metadata.cpp:1094-1106](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L1094-L1106) —— 先拷贝一份新的 `SegmentDesc`、把 `buffer_desc` `push_back` 进 `LOCAL_SEGMENT_ID` 段的 `buffers`，再判断：`if (update_metadata) return updateLocalSegmentDesc();`。**这就是 `update_metadata` 开关的落点**。

**需要观察的现象 / 预期结果**：

- 单条 `register_memory` 走的是「`update_metadata=true`」分支，因此每注册一块，都会触发一次 `updateLocalSegmentDesc()` → `storage_plugin_->set(...)`（写元数据服务）。注册 N 块就写 N 次元数据服务。
- 这条链路完整后，你应该能回答：**「为什么单条注册 N 块内存会比较慢？因为每块都要同步写一次元数据服务。」**——这正是 4.3 批量注册要解决的问题。

> 本实践为源码阅读型，无需运行；若要实测耗时，请配合 4.3 / 第 5 节的脚本。

#### 4.1.5 小练习与答案

**练习 1**：如果用默认参数连续两次 `registerLocalMemory` 注册了 **同一段地址** 的内存，第二次会返回什么？为什么？

> **参考答案**：返回 `ERR_ADDRESS_OVERLAPPED`。因为 `registerLocalMemory` 第一步就是重叠检测 `checkOverlap(addr, length)`，[transfer_engine_impl.cpp:568-572](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_engine_impl.cpp#L568-L572) 在检测到重叠时直接返回该错误码。Mooncake 不支持重叠或重复注册同一段内存。

**练习 2**：`location` 传 `"*"` 与传 `"cpu"`，对 RDMA 注册流程有什么不同？

> **参考答案**：传 `"*"` 时，`registerLocalMemoryInternal` 会调用 `getMemoryLocation(addr, length, ...)` 自动探测内存所在位置，把探测到的真实位置（如 `cpu`）写进 `buffer_desc.name`（见 [rdma_transport.cpp:302-310](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/rdma_transport.cpp#L302-L310)）；传具体位置（如 `"cpu"`）则直接采用该值，跳过探测。`location` 最终用于拓扑选路（决定走哪块网卡），是性能调优时可显式指定的参数。

---

### 4.2 段（Segment）的生命周期：打开、关闭、移除与元数据发布

#### 4.2.1 概念说明

「段」是元数据层面的概念。回到第 2 节那张表：**一个 `TransferEngine` 实例只有一个本地段**（`LOCAL_SEGMENT_ID = 0`，名字是 `local_server_name`）。对端要访问你的内存，必须先「打开」你这个段，拿到 `SegmentID`，才能在这个段里找具体的 buffer。

围绕段，引擎提供了三个公开 API：

| API | 作用 |
| --- | --- |
| `openSegment(name)` | 按段名（通常是 `ip:port`）解析出一个 `SegmentID`。本质是「从元数据服务把段描述符拉下来并缓存」。**它不会创建段，只是「打开/解析」**。 |
| `closeSegment(handle)` | 关闭一个已打开的段句柄。**在当前实现里是空操作（直接返回 0）**，是为将来释放资源预留的接口。 |
| `removeLocalSegment(name)` | 从 **本地缓存** 中移除一个段（清掉 `segment_name_to_id_map_` 等本地映射）。注意：它不直接删除元数据服务里的持久条目。 |

需要特别区分 **两条「段」的生命周期线**，它们容易混淆：

- **本地段的发布/更新/移除（元数据服务侧）**：本地段在引擎初始化时被 `addLocalSegment` 写进本地映射，随后每次 `addLocalMemoryBuffer(update_metadata=true)` 都会把整段重新 `set` 到元数据服务；引擎销毁（transport 析构）时再 `removeSegmentDesc` 从元数据服务删除。
- **远端段的打开/关闭/移除（本地缓存侧）**：对端用 `openSegment` 把别人的段描述符拉进 **自己的本地缓存**，`removeLocalSegment` 则清掉 **自己缓存里** 的某个段。这些操作只动本地缓存，不影响对方在元数据服务里的条目。

#### 4.2.2 核心流程

**本地段（自己）从无到有到销毁**：

```
引擎 init()
  └─ transport->install() 内部调用 addLocalSegment(LOCAL_SEGMENT_ID, name, desc)
       → 把 {0 -> desc, name -> 0} 写进本地映射（尚未发布）
首次 registerLocalMemory(update_metadata=true)
  └─ addLocalMemoryBuffer → updateLocalSegmentDesc() → storage_plugin_->set(name, json)
       → 本地段第一次真正写进元数据服务
之后每注册/注销一块内存
  └─ buffers 增减 + 再次 set → 元数据服务里的段不断被「整体覆盖更新」
引擎销毁 / transport 析构
  └─ metadata_->removeSegmentDesc(name) → storage_plugin_->remove(name)
       → 从元数据服务删除整段
```

**远端段（别人）在本地的打开/关闭**：

```
openSegment("172.31.6.162:12345")
  └─ getSegmentID(name)
       → 先查本地缓存 segment_name_to_id_map_
       → 未命中则 getSegmentDesc(name) 从元数据服务拉取
       → 分配一个递增的 SegmentID、写进缓存、返回
closeSegment(handle)  → 当前为空操作，返回 0
removeLocalSegment(name)  → 仅清本地缓存映射（不动元数据服务）
```

#### 4.2.3 源码精读

先看三个公开 API 在 `TransferEngineImpl` 里的实现：

[mooncake-transfer-engine/src/transfer_engine_impl.cpp:506-529](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_engine_impl.cpp#L506-L529) —— `openSegment`：去掉段名开头的多余 `/`，然后调 `metadata_->getSegmentID(name)` 拿到 `SegmentID` 返回。（Barex 协议还会额外 `OpenChannel`，可暂时忽略。）

[mooncake-transfer-engine/src/transfer_engine_impl.cpp:546-548](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_engine_impl.cpp#L546-L548) —— `closeSegment`：**当前实现直接 `return 0;`**，是一个预留的空操作。

[mooncake-transfer-engine/src/transfer_engine_impl.cpp:550-557](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_engine_impl.cpp#L550-L557) —— `removeLocalSegment`：去掉多余 `/` 后调用 `metadata_->removeLocalSegment(name)`。

再看元数据层。`getSegmentID` 是「打开远端段」的核心——「先查缓存、未命中再走网络拉取」的双检模式：

[mooncake-transfer-engine/src/transfer_metadata.cpp:1039-1059](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L1039-L1059) —— `getSegmentID`：读锁下查 `segment_name_to_id_map_`；未命中则在 **不持锁** 的情况下 `getSegmentDesc(name)`（可能涉及网络 I/O），再在写锁下双检后分配 `SegmentID = next_segment_id_.fetch_add(1)` 并缓存。

「发布本地段」的入口是 `updateLocalSegmentDesc`，它把整段重新写回元数据服务：

[mooncake-transfer-engine/src/transfer_metadata.cpp:1061-1073](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L1061-L1073) —— `updateLocalSegmentDesc`：读出 `LOCAL_SEGMENT_ID` 对应的 `SegmentDesc`，转交 `updateSegmentDesc(desc->name, *desc)`，后者会把段编码成 JSON 并 `storage_plugin_->set(...)` 写进元数据服务（见 [transfer_metadata.cpp:472-484](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L472-L484)）。

`addLocalSegment` 与 `removeLocalSegment` 只动 **本地映射**（不碰存储插件）：

[mooncake-transfer-engine/src/transfer_metadata.cpp:1075-1092](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L1075-L1092) —— `addLocalSegment` 把 `{id->desc, name->id}` 写进本地两个 map；`removeLocalSegment` 反向 `erase` 这两个 map 里的条目。两者都不调用 `storage_plugin_`。

真正「从元数据服务删除整段」的是 `removeSegmentDesc`，它在 transport 析构时被调用：

[mooncake-transfer-engine/src/transfer_metadata.cpp:487-507](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L487-L507) —— `removeSegmentDesc`：在 P2P 握手模式下清本地 map；否则 `storage_plugin_->remove(name)` 删除持久条目。

[mooncake-transfer-engine/src/transport/rdma_transport/rdma_transport.cpp:84-92](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/rdma_transport.cpp#L84-L92) —— `~RdmaTransport()` 析构函数里调用 `metadata_->removeSegmentDesc(local_server_name_)`，引擎退出时自动清理自己在元数据服务里的段。

最后，本地段是在引擎初始化时第一次写进本地映射的：

[mooncake-transfer-engine/src/transport/rdma_transport/rdma_transport.cpp:400-401](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/rdma_transport.cpp#L400-L401) —— `metadata_->addLocalSegment(LOCAL_SEGMENT_ID, local_server_name_, std::move(desc))`：把本地段（含设备描述、拓扑）登记进本地映射，供后续 `addLocalMemoryBuffer` 向其 `buffers` 追加条目。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：分清「`openSegment` 是解析而非创建」「`closeSegment` 当前是空操作」「`removeLocalSegment` 只清本地缓存」三件事，避免在实际使用中误解这些 API 的副作用。

**操作步骤**：

1. 阅读 `openSegment` → `getSegmentID` 这条链（4.2.3），确认它 **不会** 调用任何 `storage_plugin_->set`，因此不会在元数据服务里新增条目。
2. 阅读 `closeSegment`（[transfer_engine_impl.cpp:546-548](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_engine_impl.cpp#L546-L548)），确认它当前只是 `return 0;`。
3. 对比 `removeLocalSegment`（[transfer_metadata.cpp:1084-1092](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L1084-L1092)，只清本地 map）与 `removeSegmentDesc`（[transfer_metadata.cpp:487-507](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L487-L507)，调 `storage_plugin_->remove`）。

**需要观察的现象 / 预期结果**：

- 你应该得出结论：**要让一个本地段从元数据服务消失，正确做法是让引擎正常析构**（transport 析构会 `removeSegmentDesc`）；仅调用 `removeLocalSegment` 只会影响本进程的缓存视图。
- 也就是说：一个进程异常退出（没走析构）后，它的段条目可能会 **残留** 在元数据服务里，需要靠心跳/探活（`probePeerAliveByID`）或手动清理来处理。

> 本实践为源码阅读型，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：`openSegment` 返回的 `SegmentID` 是对方段的「真实 ID」吗？

> **参考答案**：不是。`SegmentID` 是 **本进程本地分配** 的一个递增编号（`next_segment_id_.fetch_add(1)`），用来在本地的 `segment_id_to_desc_map_` 里索引从元数据服务拉来的段描述符。两个不同进程对「同一段」拿到的 `SegmentID` 几乎一定不同；真正全局唯一的是段名（`ip:port`）。`SegmentHandle` 与 `SegmentID` 都是 `uint64_t`，见 [transport.h:50-51](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/transport/transport.h#L50-L51)。

**练习 2**：如果一个 target 进程注册了 3 块内存，元数据服务里这个 target 对应的「段 key」有几个？段里的 `buffers` 数组长度是多少？

> **参考答案**：段 key **只有 1 个**（名字是 target 的 `local_server_name`）。段描述符里的 `buffers` 数组长度为 **3**。每次注册都是往同一个段的 `buffers` 里追加，再把整段 `set` 覆盖回去。这正是第 2 节强调的那条规则。

---

### 4.3 批量内存注册：registerLocalMemoryBatch 与 update_metadata 语义

#### 4.3.1 概念说明

在 KV cache 这类场景里，一台机器往往要一次注册 **几十块**、每块几个 GB 的内存（模拟多租户 / 分片的 KV 池）。如果用 4.1 的「单条注册」，每注册一块都要：

1. 向网卡注册 MR（慢，尤其大内存要 pin）；
2. **同步写一次元数据服务**（`update_metadata=true` 默认开启）。

N 块内存 = N 次网卡注册 + **N 次元数据服务写入**。第 ② 步在网络往返上会显著拖慢启动。

`registerLocalMemoryBatch` 的核心优化思路是：**把「更新元数据服务」这件事攒到最后只做一次**。

具体做法：批量注册时，每一块内存仍然要向网卡注册 MR（这步省不掉），但调用 transport 层时传 `update_metadata=false`，即「注册完先别发布」；等这一批全部注册成功，再 **调用一次** `updateLocalSegmentDesc()` 把整段一次性写回元数据服务。

| 维度 | 单条注册（N 块） | 批量注册（N 块） |
| --- | --- | --- |
| 网卡 MR 注册次数 | N | N |
| 元数据服务写入次数 | **N** | **1** |
| 是否可并行注册 MR | 否（逐块串行） | **是**（见下文 `std::async`） |
| 典型场景 | 少量、零散内存 | 启动期大批量 KV 池 |

#### 4.3.2 核心流程

```
registerLocalMemoryBatch([buf0..bufN-1], location)
  1. 对每块 buffer 做重叠检测（任一重叠即返回 ERR_ADDRESS_OVERLAPPED）
  2. for 每个 transport:
       transport->registerLocalMemoryBatch(buffer_list, location)
         —— 以 RDMA 为例：
            a. 对每块 buffer 并行(std::async)调用 registerLocalMemoryInternal
               (force_sequential=true 避免嵌套并行, update_metadata=false 不逐块发布)
            b. 全部完成后，调用一次 metadata_->updateLocalSegmentDesc()  ← 唯一一次发布
  3. 加锁，把 N 块内存一次性写进本地登记表 local_memory_regions_
  4. 返回 0
```

注意三个细节：

- **`force_sequential=true`**：批量内部已用 `std::async` 做了并行，所以单块内部要 **关闭** parallel MR 注册，避免「线程里再开线程」的嵌套并行。
- **`update_metadata=false`**：逐块注册阶段不发元数据。
- **末尾一次 `updateLocalSegmentDesc`**：这才是批量注册能省时间的关键。

#### 4.3.3 源码精读

先看 `TransferEngineImpl` 层的批量入口：

[mooncake-transfer-engine/src/transfer_engine_impl.cpp:721-740](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_engine_impl.cpp#L721-L740) —— `registerLocalMemoryBatch`：① 逐块重叠检测；② `for (auto transport : ...)` 调 transport 层的 batch 版本；③ 加锁把每块写进本地登记表。注意第 ③ 步用的是 `location`、`remote_accessible` 硬编码为 `true`。

再看 RDMA 传输对批量注册的实现，这是「攒一次发布」的真正落点：

[mooncake-transfer-engine/src/transport/rdma_transport/rdma_transport.cpp:433-473](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/rdma_transport.cpp#L433-L473) —— `RdmaTransport::registerLocalMemoryBatch`：用 `std::async(std::launch::async, ...)` 为每块内存并发调用 `registerLocalMemoryInternal(..., true, false, true)`（依次是 `remote_accessible=true`、`update_metadata=false`、`force_sequential=true`），收集每个 future 的结果，**最后** `return metadata_->updateLocalSegmentDesc();` 只发布一次。

对比单条注册里那一行 `addLocalMemoryBuffer(buffer_desc, update_metadata)`（4.1.3），就能清楚看到：批量注册把 N 次 `update_metadata=true` 换成了「N 次 `false` + 1 次集中发布」。

批量注销是对称设计：

[mooncake-transfer-engine/src/transport/rdma_transport/rdma_transport.cpp:475-493](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/rdma_transport.cpp#L475-L493) —— `unregisterLocalMemoryBatch`：并发注销每块（`update_metadata=false`），末尾一次 `updateLocalSegmentDesc()`。

Python 侧的批量入口与 `BufferEntry` 的组装：

[mooncake-integration/transfer_engine/transfer_engine_py.cpp:778-789](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L778-L789) —— `batchRegisterMemory`：把 `addresses`/`capacities` 两个列表打包成 `std::vector<BufferEntry>`，调用 `engine_->registerLocalMemoryBatch(buffers, location)`。`BufferEntry` 就是 `{void* addr; size_t length;}`（[transport.h:381-384](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/transport/transport.h#L381-L384)）。

最后看官方基准脚本里「单条 vs 批量」的对比写法，作为实践的模板：

[mooncake-transfer-engine/example/batch_register_bench.py:190-206](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/example/batch_register_bench.py#L190-L206) —— `--use_batch_api` 为真时调用 `engine.batch_register_memory(addrs, sizes)`；否则循环 `engine.register_memory(addr, size)`。两种方式都会被 `time.time()` 计时并打印「总耗时 / 每块耗时」。

脚本里内存是用页对齐 `mmap` 分配的（默认尝试大页 `MAP_HUGETLB`，失败回退到普通页），这一点对实践很重要：

[mooncake-transfer-engine/example/batch_register_bench.py:102-134](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/example/batch_register_bench.py#L102-L134) —— `allocate_block`：用 `ctypes` 调 `libc.mmap` 分配页对齐内存，先试 hugepage 再回退 4KB 页。

#### 4.3.4 代码实践（动手运行型）

**实践目标**：参考 `batch_register_bench.py`，写一个 **最小化** 的批量注册脚本，对比「单条注册」与「批量注册」的耗时，并验证 `unregister` 后被注销的 buffer 无法再被远端访问。

下面给出 **示例代码**（改编自 `batch_register_bench.py`，非项目原有文件），保存为 `mini_batch_register.py`：

```python
# 示例代码：改编自 mooncake-transfer-engine/example/batch_register_bench.py
import ctypes, ctypes.util, time, argparse

def alloc_block(size):
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    libc.mmap.restype = ctypes.c_void_p
    flags = 0x02 | 0x20  # MAP_PRIVATE | MAP_ANONYMOUS
    ptr = libc.mmap(None, size, 1 | 2, flags, -1, 0)  # PROT_READ|PROT_WRITE
    assert ptr and ptr != ctypes.c_void_p(-1).value
    return ptr

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--local_server_name", required=True)  # 例: 127.0.0.1:12345
    ap.add_argument("--metadata_server", default="P2PHANDSHAKE")
    ap.add_argument("--protocol", default="tcp")
    ap.add_argument("--num_blocks", type=int, default=8)
    ap.add_argument("--block_size_mb", type=float, default=64)
    ap.add_argument("--use_batch_api", action="store_true")
    args = ap.parse_args()

    from mooncake.engine import TransferEngine
    engine = TransferEngine()
    assert engine.initialize(args.local_server_name, args.metadata_server,
                            args.protocol, "") == 0

    size = int(args.block_size_mb * 1024 * 1024)
    addrs = [alloc_block(size) for _ in range(args.num_blocks)]

    t0 = time.time()
    if args.use_batch_api:
        rc = engine.batch_register_memory(addrs, [size] * len(addrs))
    else:
        for a in addrs:
            rc = engine.register_memory(a, size)
            if rc != 0:
                break
    dt = time.time() - t0
    print(f"num_blocks={args.num_blocks} batch={args.use_batch_api} "
          f"reg_time={dt:.3f}s per_block={dt/args.num_blocks*1000:.1f}ms rc={rc}")

    # 让本地段的 buffers 列表可被远端解析（getFirstBufferAddress 会 openSegment）
    first = engine.get_first_buffer_address(args.local_server_name)
    print(f"first_buffer_address=0x{first:x} (注册条目数 num_blocks={args.num_blocks})")

    # 注销第一块，再观察远端访问 —— 详见第 5 节综合实践的两机配合
    engine.unregister_memory(addrs[0])
    print("unregistered first block; 远端再次访问该地址应失败（待本地验证）")

if __name__ == "__main__":
    main()
```

**操作步骤**（需要两台可互通的机器，或本机两个进程；无 RDMA 时用 `--protocol tcp`）：

1. 安装 Python 包：`pip install -e mooncake-wheel`（参考脚本头部说明）。
2. 在 **target** 机器上运行（先不批量）：
   `python mini_batch_register.py --local_server_name <target_ip>:12345 --protocol tcp --num_blocks 8 --block_size_mb 64`
3. 再加 `--use_batch_api` 重跑一次，对比两次打印的 `reg_time` 与 `per_block`。
4. （可选）把 `--num_blocks` 调大到 40、`--block_size_gb 4`，更接近真实 KV 池规模，观察耗时差异被放大的现象。

**需要观察的现象 / 预期结果**：

- ✅ **注册条目数量**：target 打印的 `num_blocks` 等于你注册的块数；这是「本地段 `buffers` 列表的长度」。元数据服务里 **段 key 仍只有 1 个**（如使用 etcd，可用 `etcdctl get --prefix ''` 观察到 target 的段名对应唯一一个 key，其 JSON 的 `buffers` 数组长度等于 `num_blocks`）。
- ✅ **耗时**：在块数较多时，`--use_batch_api` 的总注册时间通常 **明显小于** 逐块注册（因为元数据服务写入从 N 次降到 1 次，且 MR 注册可并行）。具体数值 **待本地验证**（取决于网卡、是否 hugepage、元数据服务延迟）。
- ⚠️ **unregister 后远端访问**：在 target 上 `unregister_memory(addrs[0])` 后，该地址对应的 buffer 会从段的 `buffers` 中移除并重新发布。**initiator 在刷新其段描述符缓存后**，再对该地址发起读取应返回非 0（失败）。但由于缓存刷新时机依赖 `getSegmentDescByName(force_update)`/`syncSegmentCache`，**「失败是否立即发生」待本地验证**——这一点正是 4.2 强调的「远端看到的段是缓存视图」。

> 安全提示：`mmap` 分配的内存不会自动释放；在真实程序里注销后应 `munmap` 回收。示例为简化省略。

#### 4.3.5 小练习与答案

**练习 1**：批量注册为什么要在每块的 `registerLocalMemoryInternal` 里传 `force_sequential=true`？

> **参考答案**：因为批量注册已经用 `std::async(std::launch::async, ...)` 为每块内存开了一个任务线程做 **块间并行**（[rdma_transport.cpp:449-458](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/rdma_transport.cpp#L449-L458)）。如果每块内部再开「多 context 并行注册 MR」（即 `use_parallel_reg`），就会形成「线程里再开线程」的嵌套并行，反而增加调度开销。`force_sequential=true` 让单块内部串行注册各 context，避免嵌套。

**练习 2**：如果批量注册到第 5 块时网卡注册失败，前 4 块会不会被发布到元数据服务？

> **参考答案**：会留在 **本地段描述符**里（`addLocalMemoryBuffer(update_metadata=false)` 已把它们追加进 `LOCAL_SEGMENT_ID` 的 `buffers`），但因为第 5 块失败后函数提前返回，**末尾那次 `updateLocalSegmentDesc()` 不会执行**，所以这批结果 **不会被发布到元数据服务**。不过 4 块的 MR 已在网卡上注册成功、且已进入本地登记表——这是批量 API 当前的一个粗糙点（缺乏像多协议 `mp_registerLocalMemory` 那样的回滚，见 [transfer_engine_impl.cpp:604-689](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_engine_impl.cpp#L604-L689)）。生产中遇到批量失败应整体注销重试。

---

## 5. 综合实践：批量注册并验证「unregister 即不可远端访问」

把本讲三个模块串起来，设计一个贯穿性任务：**在 target 上批量注册 N 块内存，从 initiator 读取其中一块；然后 target 注销该块，再次从 initiator 读取，验证访问失败。**

**目标**：亲手验证下面这条因果链——

```
register_memory / batch_register_memory
    → 本地段 buffers 增长 → 发布到元数据服务
    → initiator openSegment + 读取成功
unregister_memory
    → 本地段 buffers 缩减 → 重新发布
    → initiator 刷新缓存后读取失败
```

**操作步骤**（双机/双进程，无 RDMA 用 TCP）：

1. **target** 端：基于第 4.3.4 节脚本，注册 N 块内存后 `signal.pause()` 保持运行，并打印供 initiator 使用的「第一块地址」（即 `get_first_buffer_address` 的返回值）。
2. **initiator** 端：参考官方基准的 `run_initiator`（[batch_register_bench.py:223-272](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/example/batch_register_bench.py#L223-L272)）：本机注册一块接收缓冲区，`engine.get_first_buffer_address(target_name)` 拿到 target 第一块地址，用 `engine.transfer_sync_read(target_name, recv_addr, remote_base, length)` 读取。
3. **第一次读取**：预期返回 0（成功），证明 target 的第一块内存已通过元数据服务被 initiator 解析并访问。
4. **target 注销第一块**：在 target 上 `engine.unregister_memory(first_block_addr)`。
5. **initiator 再次读取同一地址**：
   - 预期 **返回非 0（失败）**，因为该 buffer 已从 target 的段中移除。
   - 若仍读成功，说明 initiator 用的是 **缓存的旧段描述符**；此时在 initiator 调一次 `engine.get_engine().syncSegmentCache()`（对应 [transfer_engine.h:177](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/transfer_engine.h#L177)）强制刷新后再读，应观察到失败。

**需要观察的现象 / 预期结果**：

| 阶段 | target 元数据服务 | initiator 读取同一地址 |
| --- | --- | --- |
| 批量注册 N 块后 | 段 key ×1，buffers 长度 = N | 成功（rc=0） |
| 注销第 1 块后 | 段 key ×1，buffers 长度 = N-1 | 刷新缓存后失败（rc≠0） |
| target 进程退出后 | 段 key 被 `removeSegmentDesc` 删除 | openSegment 失败 / 段不可达 |

> 具体返回码与「刷新前能否读到」的精确行为 **待本地验证**（依赖元数据服务实现与缓存策略 `globalConfig().metacache`）。本实践的重点是理解因果链，而非记住某个固定返回码。

---

## 6. 本讲小结

- **`registerLocalMemory(addr, length, location, remote_accessible, update_metadata)`** 是注册内存的对外入口；`location` 控制内存位置/选路（`"*"` 自动探测），`update_metadata` 控制注册后是否 **立即把段写回元数据服务**。
- 注册一块内存 = 向所有 transport 注册 MR + 把一个 `BufferDesc` **追加进唯一的本地段（`LOCAL_SEGMENT_ID = 0`）**；注册前会做 \( O(\log n) \) 重叠检测，重叠或零长直接报错。
- **段（Segment）≠ buffer**：每个引擎只有一个本地段；注册 N 块内存只让段的 `buffers` 长到 N，元数据服务里的段 key 始终是 1 个。
- 段的生命周期：本地段在 init 时 `addLocalSegment`、随每次注册 `updateLocalSegmentDesc` 发布、在 transport 析构时 `removeSegmentDesc` 删除；`openSegment` 是「解析+缓存」远端段、`closeSegment` 当前为空操作、`removeLocalSegment` 只清本地缓存。
- **批量注册** `registerLocalMemoryBatch` 的优化核心是「逐块 `update_metadata=false` + 末尾一次 `updateLocalSegmentDesc`」，把元数据服务写入从 N 次降到 1 次，并可并行注册 MR。
- `unregister_memory` 后，对应 buffer 从段中移除并重新发布；远端在刷新段缓存后对该地址的访问将失败——这是「远端看到的是缓存视图」的直接体现。

---

## 7. 下一步学习建议

1. **深入元数据编码**：本讲只用到 `addLocalMemoryBuffer`/`updateLocalSegmentDesc`，建议接着读 [u2-l2 TransferMetadata：段与缓冲区的元数据协调](u2-l2-transfer-metadata.md)，弄清 `SegmentDesc`/`BufferDesc` 如何编码成 JSON、又如何被对端解码。
2. **理解重叠检测的数据结构**：本讲的 `local_memory_regions_` 是 `std::map`，`hasOverlapLocked` 用下界查找做区间相交判定。可顺带阅读 [transfer_engine_impl.cpp:756-809](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_engine_impl.cpp#L756-L809) 的 `findMemoryRegionContaining`。
3. **多协议批量注册的回滚机制**：若编译时打开 `ENABLE_MULTI_PROTOCOL`，`mp_registerLocalMemory` 提供了 **失败回滚**（`rollbackAllRegistrations`），是比 `registerLocalMemoryBatch` 更健壮的批量注册。建议对比 [transfer_engine_impl.cpp:604-689](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_engine_impl.cpp#L604-L689)。
4. **动手跑基准**：在有两台 RDMA 机器时，按 `batch_register_bench.py` 头部 Usage 跑 target/initiator 两端，对比 `--use_batch_api` 开关下的注册耗时与传输吞吐，体会批量注册在真实硬件上的收益。

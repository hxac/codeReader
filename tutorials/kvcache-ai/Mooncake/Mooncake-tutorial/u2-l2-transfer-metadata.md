# TransferMetadata：段与缓冲区的元数据协调

> 所属单元：第 2 单元 · Transfer Engine 核心机制
> 学习阶段：intermediate
> 依赖讲义：[u2-l1 TransferEngine 架构与核心抽象](u2-l1-te-architecture-core.md)

## 1. 本讲目标

学完本讲后，你应该能够：

1. 说清楚 **「段（Segment）」和「缓冲区（Buffer）」** 在 Mooncake 传输引擎里到底是什么，以及它们为什么需要一份「元数据」来被别的进程看见。
2. 看懂 `SegmentDesc` / `BufferDesc` / `DeviceDesc` 这一组结构体，并能解释它们每一个字段在做什么。
3. 理解元数据是如何被 **编码成 JSON 写入元数据服务、再被对端解码回来** 的，以及不同传输协议（rdma / tcp / cxl / nvlink …）在编码上的差异。
4. 掌握 `TransferMetadata` 这个「协调者」类的作用：本地缓存、段注册流程、以及它是如何通过 **插件** 对接 etcd / redis / HTTP 元数据服务的。
5. 理解 **peer 发现与握手** 流程：`MetadataStoragePlugin`（中心化）与 `HandShakePlugin`（点对点握手）两条路径，以及 RDMA 传输所需的「交换信息」是如何分成段描述与握手描述两步完成的。

一句话总结：本讲解答「**我的内存，对方是怎么知道的？**」这个问题。

## 2. 前置知识

本讲假设你已经：

- 读过 u1-l5，启动过一次 HTTP 元数据服务器，并完成过一次跨进程传输。
- 读过 u2-l1，知道 `TransferEngine` 是一个门面（facade），它把真正的实现委托给 `TransferEngineImpl`，而 `TransferEngineImpl` 又持有一个 `TransferMetadata` 成员。

如果你对下面几个名词还不熟，先看这里的 30 秒解释：

| 名词 | 通俗解释 |
| --- | --- |
| Segment（段） | 一个进程向集群「登记」出来的一个逻辑身份。可以理解为「我是 `192.168.1.10:12345` 这台机器上的一个传输参与者」。一个进程通常只有一个本地段。 |
| Buffer（缓冲区） | 段内的一段**具体内存**（一段连续地址 + 长度）。真正要传的字节就在 buffer 里。 |
| 元数据（Metadata） | 描述「段 / buffer 长什么样」的结构化信息（地址、长度、RDMA 密钥、网卡 gid……）。**数据走高速通道（RDMA/TCP），元数据走控制通道（元数据服务或握手）。** |
| 元数据服务 | 一个集中式的 KV 存储（etcd / redis / 一个 HTTP 服务），所有进程都往里写、从里读元数据。 |
| 握手（Handshake） | 两个进程之间直接建立 TCP 连接、互相交换必要信息的动作。下面会看到，它既可用来交换 RDMA 连接参数，也可用来在「没有元数据服务」时直接交换整个段描述。 |

> 关于 spec 中提到的 `RdmaExchange`：仓库里**并没有**一个叫 `RdmaExchange` 的结构体。RDMA 所需的「交换信息」在源码里被拆成了两半：① **网卡与内存密钥**放在 `SegmentDesc`（`DeviceDesc.gid/lid`、`BufferDesc.rkey/lkey`）里，随段一起登记；② **队列对编号 QP** 在建立连接时才交换，放在 `HandShakeDesc.qp_num` 里。本讲会按这个真实拆分来讲解，不臆造结构体。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [transfer_metadata.h](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/transfer_metadata.h) | `TransferMetadata` 类与全部描述符结构体（`SegmentDesc`/`BufferDesc`/`DeviceDesc`/`HandShakeDesc` 等）的声明。 |
| [transfer_metadata_plugin.h](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/transfer_metadata_plugin.h) | 两个插件接口：`MetadataStoragePlugin`（中心化存储）、`HandShakePlugin`（点对点握手）。 |
| [transfer_metadata.cpp](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp) | 编码 / 解码、本地缓存、段注册、握手收发等全部实现。 |
| [transfer_metadata_plugin.cpp](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata_plugin.cpp) | 插件的具体实现：etcd / redis / HTTP 三个存储插件，以及基于 socket 的 `SocketHandShakePlugin`。 |
| [transfer_metadata_dump.cpp](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata_dump.cpp) | 调试用：把当前内存里的段描述、RPC 路由表打印出来。实践环节会用到。 |
| [common.h](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/common.h) | `LOCAL_SEGMENT_ID` 常量、握手报文的 `HandShakeRequestType` 枚举，以及 socket 帧的 `writeString`/`readString`。 |

下面进入核心讲解。我们按「**先看描述符长什么样 → 看它怎么变成 JSON → 看谁在协调这些动作 → 看两条对外通道（存储插件 + 握手插件）**」的顺序，分成四个最小模块。

---

## 4. 核心概念与源码讲解

### 4.1 段与缓冲区描述符：SegmentDesc / BufferDesc / DeviceDesc

#### 4.1.1 概念说明

要做一次跨进程的 `write`（把本地内存写到对端），发送方至少需要知道关于对端的几件事：

- 对端有**哪些网卡**？每张网卡的地址（gid / lid）是什么？——否则 RDMA 连不上。
- 对端**注册了哪些内存**？这些内存的虚拟地址、长度是多少？——否则不知道写到哪。
- 如果是 RDMA，还需要对端为这块内存分配的**远程密钥 rkey**——RDMA 硬件靠它授权远端访问。

Mooncake 把这些信息组织成一组结构体，统称「描述符（descriptor）」。它们的关系是：

```
SegmentDesc（一个进程的对外身份）
 ├── protocol            "rdma" / "tcp" / "cxl" / "nvlink" ...
 ├── devices[]           DeviceDesc：每张网卡的标识（name/lid/gid/eid）
 ├── topology            多网卡之间的优先级矩阵（选路用）
 ├── buffers[]           BufferDesc：每块已注册内存的描述
 ├── nvmeof_buffers[]    NVMe-oF 专用（文件路径）
 ├── cxl_name/cxl_base_addr  CXL 专用
 └── rank_info           Ascend/NPU 专用
```

注意：**同一个 `SegmentDesc` 结构体被复用于所有协议**，不同协议只填其中的部分字段。这是为什么后面的 encode/decode 里有那么多 `if (protocol == "rdma")` 分支。

#### 4.1.2 核心流程

描述符在系统里的生命周期：

```
[本进程]                                    [元数据服务 / 对端进程]
 TransportEngine.registerLocalMemory(addr, len)
        │  委托给 transport（如 TcpTransport / RdmaTransport）
        ▼
 transport 填充一个 BufferDesc                  （addr/length/rkey/lkey）
        │  调用 metadata_->addLocalMemoryBuffer(buf, update=true)
        ▼
 TransferMetadata 把 buf 追加进本地段的 buffers[]
        │  调用 updateLocalSegmentDesc() → updateSegmentDesc()
        ▼
 encodeSegmentDesc(desc)  ──→ JSON ──→ storage_plugin->set(key, json)  ──→  [KV 库里存了一份]

[对端进程]
 metadata_->getSegmentDescByName("192.168.1.10:12345")
        │  storage_plugin->get(key) ──→ JSON
        ▼
 decodeSegmentDesc(json) ──→ SegmentDesc（含 buffers[]、devices[]）
        │  缓存到本地 segment_id_to_desc_map_
        ▼
 之后 submitTransfer 时，transport 用 desc->buffers[i].rkey 发起 RDMA write
```

关键点：**数据通路（RDMA/TCP）和控制通路（元数据）是分开的**。元数据只负责「让对方知道我的内存长什么样」，真正的字节流不走元数据服务。

#### 4.1.3 源码精读

**`SegmentDesc` 结构体**——一个段对外公布的全部信息：

[transfer_metadata.h:L88-L121](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/transfer_metadata.h#L88-L121) — 定义了 `name`、`protocol`、`devices[]`、`topology`、`buffers[]`、`nvmeof_buffers[]`、`cxl_*`、`rank_info`、`tcp_data_port`、`rdma_server_name` 等字段。其中末尾的 `nicPathServerName()` 是一个小工具：在双网卡场景下，RDMA 可达地址可能和 TCP 段名不同，这里决定构造「NIC 路径」时用哪个名字。

**`BufferDesc` 结构体**——一块内存的描述：

[transfer_metadata.h:L52-L65](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/transfer_metadata.h#L52-L65) — 关键字段：

| 字段 | 含义 | 主要用于 |
| --- | --- | --- |
| `name` | 通常是本段 server name | 所有协议 |
| `addr` | 内存起始虚拟地址 | rdma/tcp/ub… |
| `length` | 内存长度 | 所有协议 |
| `lkey` / `rkey` | RDMA 本地/远程密钥（每张网卡一个，所以是 vector） | **rdma** |
| `shm_name` | 共享内存对象名 | nvlink / hip（同机跨进程） |
| `offset` | 在 CXL 设备内存中的偏移 | cxl |
| `tseg` / `l_seg_index` | UB/URMA 的段标识 | ub/urma |

> 注意 `lkey`/`rkey` 是 `vector<uint32_t>` 而不是单个值：因为一块内存可能在**多张网卡**上都注册了 MR（memory region），每张网卡有自己的密钥。RDMA transport 填充时按网卡顺序逐个 push：

[rdma_transport.cpp:L294-L298](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/rdma_transport.cpp#L294-L298) — 遍历所有 RDMA context，把每张网卡的 `lkey(addr)` / `rkey(addr)` 收集进 `buffer_desc`。对 TCP 而言则简单得多，只需要 name/addr/length：

[tcp_transport.cpp:L703-L716](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/tcp_transport/tcp_transport.cpp#L703-L716) — TCP 的 `registerLocalMemory` 构造 `BufferDesc` 后调用 `metadata_->addLocalMemoryBuffer(...)`。

**`DeviceDesc` 结构体**——一张网卡的标识：

[transfer_metadata.h:L45-L50](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/transfer_metadata.h#L45-L50) — `name`（设备名）、`lid`（IB 本地 ID）、`gid`（IB 全局 ID，RDMA 寻址用）、`eid`（UB/URMA 端点 ID）。

**调试用的 `SegmentDesc::dump()`**：当你想知道当前进程内存里的描述符长什么样时，看这个函数最直观：

[transfer_metadata_dump.cpp:L19-L39](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata_dump.cpp#L19-L39) — 把 name / protocol / topology / devices / buffers / nvmeof_buffers / timestamp 逐行 `LOG(INFO)` 出来。实践环节会用它做对照。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：把 `SegmentDesc::dump()` 的输出和结构体字段一一对应起来。
2. **步骤**：
   - 打开 [transfer_metadata_dump.cpp:L19-L39](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata_dump.cpp#L19-L39)。
   - 对照 [transfer_metadata.h:L88-L121](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/transfer_metadata.h#L88-L121) 的字段。
3. **观察现象**：注意 `dump()` 里 `buffers` 只打印了 `addr` 和 `addr+length` 的区间，**没有打印 rkey/lkey**——因为这些是密钥，日志里默认不打。
4. **预期结果**：你能复述「一个段 = 名称 + 协议 + 网卡列表 + 缓冲区列表 + 拓扑」，并且知道每个字段属于哪个协议分支。
5. 运行行为「待本地验证」：`dump()` 不在你正常路径上调用；只有 `dumpMetadataContent()` 在**取段失败**时才会触发（见 4.3）。要主动看到 dump 输出，可在调试时手动调用。

#### 4.1.5 小练习与答案

**Q1**：为什么 `BufferDesc::lkey` 和 `rkey` 是 `vector` 而不是单个 `uint32_t`？
> **答**：因为一块内存可能在多张 RDMA 网卡上都注册了 MR，每张网卡分配独立的 lkey/rkey。发送方需要为「走哪张网卡」选择对应的密钥，所以按网卡顺序存成数组。见 [rdma_transport.cpp:L294-L298](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/rdma_transport.cpp#L294-L298)。

**Q2**：`SegmentDesc` 里 `buffers` 和 `nvmeof_buffers` 为什么是两个并列的数组，而不是合并？
> **答**：因为 NVMe-oF 的「缓冲区」语义完全不同——它描述的是远端**文件路径**和每个节点上的本地路径映射（`local_path_map`），而不是内存地址区间。用不同的结构体（`NVMeoFBufferDesc`，见 [transfer_metadata.h:L67-L71](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/transfer_metadata.h#L67-L71)）更清晰，编码时也只在 `protocol == "nvmeof"` 分支里处理。

---

### 4.2 元数据的编码与解码

#### 4.2.1 概念说明

描述符是 C++ 结构体，但元数据服务（etcd/redis/HTTP）只认字符串。所以需要一对函数：

- **`encodeSegmentDesc`**：`SegmentDesc`（struct）→ `Json::Value`。本进程注册内存后调用。
- **`decodeSegmentDesc`**：`Json::Value` → `SegmentDesc`（struct）。对端读到 JSON 后调用。

为什么用 JSON 而不是二进制？

1. 元数据是**低频控制面**操作（一次注册、偶尔刷新），对体积不敏感，可读性更重要。
2. JSON 天然跨语言——Python 版的元数据服务、Go 版的元数据服务都能直接读写同一份内容。

#### 4.2.2 核心流程

编码流程（以 RDMA 为例）：

```
encodeSegmentDesc(desc, json)
  ├─ json["name"] = desc.name
  ├─ json["protocol"] = desc.protocol          // "rdma"
  ├─ json["tcp_data_port"] = ...
  ├─ json["timestamp"] = 当前时间
  ├─ for device in devices:  json["devices"][i] = {name, lid, gid}
  ├─ for buffer in buffers:  json["buffers"][i] = {name, addr, length, rkey[], lkey[]}
  └─ json["priority_matrix"] = topology.toJson()
```

解码流程是镜像，但多了一步**完整性校验**：如果关键字段缺失（比如 `gid` 为空、`rkey.size() != lkey.size()`），就认为这份元数据「损坏」，返回 `nullptr` 并打 WARNING。这很重要——集群里经常有「半死不活」的节点留下了残缺的元数据。

#### 4.2.3 源码精读

**编码主入口与协议分发**：

[transfer_metadata.cpp:L277-L323](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L277-L323) — `encodeSegmentDesc` 开头先处理多协议（`ENABLE_MULTI_PROTOCOL`，CXL+TCP/RDMA 组合），然后写入公共字段 `name`/`protocol`/`tcp_data_port`/`timestamp`/`rdma_server_name`。

**RDMA 分支的编码**（这是最完整的分支，值得精读）：

[transfer_metadata.cpp:L325-L353](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L325-L353) — 对 `rdma`/`barex`/`efa` 三种协议，把 `devices`（name/lid/gid）和 `buffers`（name/addr/length/rkey[]/lkey[]）序列化成 JSON 数组，最后附上 `priority_matrix`。

其余分支（`ub`/`tcp`/`nvlink`/`cxl`/`ascend`）的结构完全类似，只是字段不同。比如 TCP 分支只编码 `name/addr/length`，不带任何 RDMA 密钥：

[transfer_metadata.cpp:L377-L386](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L377-L386) — TCP 分支。

**解码与校验**（RDMA 分支）：

[transfer_metadata.cpp:L666-L708](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L666-L708) — 注意第 690-699 行的校验：

```cpp
if (buffer.name.empty() || !buffer.addr || !buffer.length ||
    buffer.rkey.empty() ||
    buffer.rkey.size() != buffer.lkey.size()) {
    LOG(WARNING) << "Corrupted segment descriptor ...";
    return nullptr;
}
```

这段代码确保：名字非空、地址非零、长度非零、rkey 非空、且 rkey 与 lkey 数量一致。任一不满足就判定元数据损坏。这是防御性编程的典型例子——元数据来自不可信的共享存储。

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：跟踪一个字段从 struct 到 JSON 再回来的完整路径。
2. **步骤**：
   - 选定字段 `BufferDesc.rkey`。
   - 在 [transfer_metadata.cpp:L344-L346](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L344-L346) 看它如何被编码进 `rkeyJSON` 数组。
   - 在 [transfer_metadata.cpp:L686-L689](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L686-L689) 看它如何被解码回来。
   - 在 [transfer_metadata.cpp:L690-L699](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L690-L699) 看它的校验规则。
3. **观察现象**：编码时 push 顺序 = 解码时 push 顺序，所以「第 i 个 rkey 对应第 i 张网卡」这个约定在两端必须一致。
4. **预期结果**：你能解释「为什么 rkey 和 lkey 必须等长」——它们是按网卡一一配对的。

#### 4.2.5 小练习与答案

**Q1**：如果对端进程用的是更老/更新版本的 Mooncake，JSON 里多了一个字段，解码会出错吗？
> **答**：不会。解码用的是 `segmentJSON["xxx"]` 按名取值，多余字段会被忽略；缺失字段则取到默认空值（随后被校验逻辑拦截）。这是 JSON 方案比二进制 schema 更鲁棒的地方。

**Q2**：`encodeSegmentDesc` 在写入 `timestamp` 时调用的是哪个函数？为什么每次注册都要刷新时间戳？
> **答**：调用 `getCurrentDateTime()`（见 [transfer_metadata.cpp:L320](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L320)）。时间戳用于让对端判断这份元数据「有多新」——例如发现对端重启后重新注册了，可以用时间戳区分新旧。

---

### 4.3 TransferMetadata：协调者、本地缓存与段注册流程

#### 4.3.1 概念说明

`TransferMetadata` 是整个元数据子系统的**门面与协调者**。`TransferEngineImpl` 持有它（[transfer_engine_impl.cpp:L202](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_engine_impl.cpp#L202) 处调用 `addRpcMetaEntry`），所有 transport 也共享同一个 `metadata_` 指针。

它有三重身份：

1. **本地段管理者**：维护本进程那个唯一的本地段（`LOCAL_SEGMENT_ID = 0`），把每次 `registerLocalMemory` 追加进它的 `buffers[]`，并择机把整个段写回元数据服务。
2. **远端段缓存**：对端的段描述读回来后，缓存进 `segment_id_to_desc_map_`（id → desc）和 `segment_name_to_id_map_`（name → id），避免每次传输都打元数据服务。
3. **插件持有者**：持有 `storage_plugin_`（中心化 KV）和 `handshake_plugin_`（点对点握手）两个插件。

它还支持两种运行模式：

- **中心化模式**（默认）：conn_string 形如 `etcd://host:port`、`redis://host:port`、`http://host:port/metadata`。元数据存进集中式 KV。
- **P2P 握手模式**：conn_string 等于字面量 `"P2PHANDSHAKE"`。没有中心元数据服务，段描述通过对端点对点握手直接交换。

#### 4.3.2 核心流程

**构造时的初始化**：

```
TransferMetadata(conn_string)
  ├─ next_segment_id_ = 1                          // 远端段 id 从 1 开始递增
  ├─ protocol = extractProtocolFromConnString()     // "etcd"/"redis"/"http"/...
  ├─ 读取 MC_METADATA_CLUSTER_ID → custom_key       // 多集群隔离用
  ├─ common_key_prefix_ = "mooncake/" + custom_key
  ├─ rpc_meta_prefix_   = common_key_prefix_ + "rpc_meta/"
  ├─ handshake_plugin_ = HandShakePlugin::Create()  // 总是创建
  └─ if conn_string == "P2PHANDSHAKE": p2p_handshake_mode_ = true; return
     else storage_plugin_ = MetadataStoragePlugin::Create()
```

**key 的命名规则**（理解这个，才能在实践环节找到正确的 key）：

[transfer_metadata.cpp:L169-L184](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L169-L184) — `getFullMetadataKey`：

- 段名不含 `/` → key = `mooncake/ram/<segment_name>`
- 段名已含 `/` → key = `mooncake/<segment_name>`（视为已是完整路径）
- RPC 路由 → key = `mooncake/rpc_meta/<server_name>`（在 `addRpcMetaEntry` 里拼接，[L1167](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L1167)）

**注册一块内存并写回元数据**：

[transfer_metadata.cpp:L1094-L1106](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L1094-L1106) — `addLocalMemoryBuffer`：

```cpp
auto new_segment_desc = std::make_shared<SegmentDesc>();
auto &segment_desc = segment_id_to_desc_map_[LOCAL_SEGMENT_ID];
*new_segment_desc = *segment_desc;   // COW：复制一份再改
segment_desc = new_segment_desc;
segment_desc->buffers.push_back(buffer_desc);
if (update_metadata) return updateLocalSegmentDesc();
```

这里有个值得注意的细节——**写时复制（COW）**：它先 `*new = *old` 复制整个段，再往新副本里 push，最后原子地替换指针。这样正在读旧 desc 的其他线程不会看到半修改状态。随后 `updateLocalSegmentDesc()` → `updateSegmentDesc()` 把新段编码成 JSON 写进元数据服务：

[transfer_metadata.cpp:L466-L485](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L466-L485) — `updateSegmentDesc` 调用 `encodeSegmentDesc` 后 `storage_plugin_->set(...)`。注意 P2P 模式下直接 `return 0`（不写中心存储）。

**对端读段 + 缓存**：

[transfer_metadata.cpp:L973-L1009](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L973-L1009) — `getSegmentDescByName`：

1. 若开启了元数据缓存（`globalConfig().metacache`）且非强制刷新，先查本地缓存命中就直接返回。
2. 本地段（`LOCAL_SEGMENT_ID`）永远直接返回，不打网络。
3. 否则调用 `getSegmentDesc(name)` 去远端取，取到后分配一个递增的 `segment_id` 存进两张 map。

#### 4.3.3 源码精读

**构造函数**：

[transfer_metadata.cpp:L130-L165](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L130-L165) — 看清 key 前缀如何拼、两个插件如何创建、P2P 模式如何提前返回。`MC_METADATA_CLUSTER_ID` 环境变量允许同一套 etcd 服务多个互相隔离的 Mooncake 集群。

**协议解析小工具**：

[transfer_metadata.cpp:L42-L49](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L42-L49) — `extractProtocolFromConnString`：取 `://` 前面的部分，没有则默认 `"etcd"`。

**`LOCAL_SEGMENT_ID` 常量**：

[common.h:L57](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/common.h#L57) — 值为 `0`。本进程自己的段永远占用 id 0，远端段从 1 开始（`next_segment_id_.store(1)`）。

#### 4.3.4 代码实践（配置观察型）

1. **目标**：观察「元数据缓存」开关和「集群隔离」前缀的效果。
2. **步骤**：
   - 阅读 [transfer_metadata.cpp:L976-L981](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L976-L981)，确认 `globalConfig().metacache` 为真时会跳过网络。
   - 阅读构造函数对 `MC_METADATA_CLUSTER_ID` 的处理 [transfer_metadata.cpp:L136-L147](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L136-L147)。
3. **观察现象（待本地验证）**：
   - 设 `MC_METADATA_CLUSTER_ID=teamA` 后，所有 key 会带前缀 `mooncake/teamA/...`，与不设该变量的集群互不可见。
   - 关掉 metacache 后，每次 `getSegmentDescByName` 都会打一次元数据服务（可从元数据服务日志或网络抓包观察到请求量上升）。
4. **预期结果**：理解「前缀隔离多个集群」与「缓存降低控制面压力」两个机制。

#### 4.3.5 小练习与答案

**Q1**：`addLocalMemoryBuffer` 为什么要先复制一份 `SegmentDesc` 再修改，而不是直接 push？
> **答**：为了无锁读者的安全。`segment_id_to_desc_map_[LOCAL_SEGMENT_ID]` 存的是 `shared_ptr<SegmentDesc>`，其他传输线程可能正持有旧 desc 的 shared_ptr 在读。COW（复制-修改-原子替换指针）保证读者看到的永远是一个完整一致快照，不会读到「push 到一半」的状态。见 [transfer_metadata.cpp:L1098-L1102](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L1098-L1102)。

**Q2**：P2P 模式下，`updateSegmentDesc` 还会把段写进 etcd 吗？
> **答**：不会。P2P 模式下 `storage_plugin_` 根本没创建，`updateSegmentDesc` 开头判断 `p2p_handshake_mode_` 后直接 `return 0`（[transfer_metadata.cpp:L468-L470](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L468-L470)）。段描述会在握手时按需直接发给对端（见 4.4）。

---

### 4.4 元数据插件与 peer 握手

#### 4.4.1 概念说明

`TransferMetadata` 把「元数据存到哪」「怎么和对端握手」这两件事抽象成了两个插件接口：

- **`MetadataStoragePlugin`**：一个极简的 KV 接口（`get` / `set` / `remove`），三个实现：`EtcdStoragePlugin`、`RedisStoragePlugin`、`HTTPStoragePlugin`。这是**中心化**通道。
- **`HandShakePlugin`**：点对点通道。负责监听端口接收握手、主动连接对端发送握手、交换元数据、发通知、发存活探针。目前仓库内只有 `SocketHandShakePlugin`（基于 TCP socket + JSON 帧协议）一个实现。

为什么握手要独立于存储？因为有些信息（比如 RDMA 的 QP 编号）**每条连接都不一样、且时效性强**，不适合塞进全局 KV；而且 P2P 模式下根本没有 KV，所有段描述都要靠握手直接交换。

#### 4.4.2 核心流程

**存储插件的选择（工厂方法）**：

```
MetadataStoragePlugin::Create(conn_string)
  ├─ parseConnectionString() → ("etcd", "host:2379")
  ├─ if proto=="etcd":  return EtcdStoragePlugin
  ├─ if proto=="redis": return RedisStoragePlugin (读 MC_REDIS_USERNAME/PASSWORD/DB_INDEX)
  └─ if proto=="http"/"https": return HTTPStoragePlugin
     else LOG(FATAL)
```

**一次完整的「建立到对端的连接」握手**（发送方视角）：

```
sendHandshake(peer_server_name, local_desc, &peer_desc)
  ├─ getRpcMetaEntry(peer)        // 先查对端的 RPC 地址（ip:port）
  │     └─ 命中缓存？否则 storage_plugin->get("mooncake/rpc_meta/<peer>")
  ├─ local = TransferHandshakeUtil::encode(local_desc)   // HandShakeDesc → JSON
  ├─ handshake_plugin_->send(ip, port, local, &peer)     // TCP 连接 + 收发
  └─ TransferHandshakeUtil::decode(peer, peer_desc)       // JSON → HandShakeDesc
       └─ if !peer_desc.reply_msg.empty(): 握手被拒
```

**接收方视角（守护线程）**：`startHandshakeDaemon` 起一个线程 `accept` 连接，每来一个连接就读一帧 JSON，根据帧头的 `type` 字段分发到不同回调（Connection / Metadata / Notify / Probe）。

**socket 帧格式**（这是握手协议的底层细节）：

```
[8 字节 length][1 字节 type][length-1 字节 JSON]
```

`type` 取自 `HandShakeRequestType`：`Connection=0`、`Metadata=1`、`Notify=2`、`Probe=3`。读取方用首字节是否 `<= Probe` 来判断是新协议（有 type 字节）还是旧协议（无 type 字节，兼容老版本）。

#### 4.4.3 源码精读

**`MetadataStoragePlugin` 接口**——只有三个纯虚函数：

[transfer_metadata_plugin.h:L21-L31](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/transfer_metadata_plugin.h#L21-L31) — `get`/`set`/`remove`，参数是 `string key` 与 `Json::Value value`。

**工厂方法**：

[transfer_metadata_plugin.cpp:L544-L596](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata_plugin.cpp#L544-L596) — 按 protocol 前缀分发到三个实现。注意 redis 实现会读 `MC_REDIS_USERNAME` / `MC_REDIS_PASSWORD` / `MC_REDIS_DB_INDEX` 三个环境变量做鉴权与选库（[L555-L581](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata_plugin.cpp#L555-L581)）。HTTP 插件用 curl，把 key 拼成 `?key=<urlencoded>` 的 query string（[L254-L261](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata_plugin.cpp#L254-L261)）——这一点对实践环节直接 curl 抓取 JSON 至关重要。

**`HandShakePlugin` 接口**：

[transfer_metadata_plugin.h:L33-L73](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/transfer_metadata_plugin.h#L33-L73) — 关键方法：`startDaemon`（监听）、`send`（建连握手）、`exchangeMetadata`（P2P 交换段描述）、`sendNotify` / `sendProbe`，以及四个回调注册函数。

**`HandShakeDesc` 的编解码**——握手时交换的 RDMA 连接参数：

[transfer_metadata.cpp:L66-L128](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L66-L128) — `TransferHandshakeUtil::encode/decode`。它编码的字段是 `HandShakeDesc`（声明在 [transfer_metadata.h:L132-L148](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/transfer_metadata.h#L132-L148)）：`local_nic_path` / `local_lid` / `local_gid` / `peer_nic_path` / `qp_num[]`（RDMA 队列对编号）/ `reply_msg`（出错时的拒绝原因）。

> 这里就能回答「RdmaExchange 到底是什么」：RDMA 建链所需的全部信息 = **段描述里的 `devices`(gid/lid) + `buffers`(rkey)**（让对端知道我的网卡和内存密钥）**+ 握手时的 `qp_num`**（让对端知道我的队列对编号）。三者合起来才是完整的「RDMA exchange」。`gid` 决定往哪张卡的哪个端口连，`rkey` 决定能写哪块内存，`qp_num` 决定发给哪个接收队列。

**发起握手**：

[transfer_metadata.cpp:L1270-L1289](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L1270-L1289) — `sendHandshake`：先 `getRpcMetaEntry` 拿到对端 ip:port，再 encode → plugin `send` → decode，最后检查 `reply_msg` 是否为空（非空表示被拒）。

**启动握手守护线程**：

[transfer_metadata.cpp:L1240-L1268](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L1240-L1268) — `startHandshakeDaemon` 注册 `on_connection_callback`：收到对端握手 → decode 出 `peer_desc` → 调用上层 `on_receive_handshake` 回调生成本地 `local_desc` → encode 回发。

**socket 帧的读写**：

[common.h:L392-L409](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/common.h#L392-L409) — `writeString`：先写 8 字节 length，再写 1 字节 type，最后写 JSON 正文。
[common.h:L445-L485](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/common.h#L445-L485) — `readString`：读 length（带 `MC_HANDSHAKE_MAX_LENGTH` 上限防 DoS），读正文，用首字节判断协议新旧。

[common.h:L59-L66](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/common.h#L59-L66) — `HandShakeRequestType` 枚举。

**守护线程的 accept 循环**：

[transfer_metadata_plugin.cpp:L730-L829](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata_plugin.cpp#L730-L829) — `SocketHandShakePlugin` 的 listener 线程：`accept` → `readString` → 按 `type` 分发到对应回调 → `writeString` 回复 → `shutdown` + 等客户端关闭。这是握手协议的服务端实现全貌。

**P2P 模式下的段描述直接交换**：

[transfer_metadata.cpp:L877-L936](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L877-L936) — `getSegmentDesc`：中心化模式走 `storage_plugin_->get`；P2P 模式则把本地段 encode 后调用 `handshake_plugin_->exchangeMetadata(ip, port, ...)` 直接和对端互换，再 decode 对端发来的 JSON。开头 `auto [ip, port] = parseHostNameWithPort(segment_name)` 说明 P2P 模式下「段名」本身就是 `ip:port`。

#### 4.4.4 代码实践（抓取 JSON，对照源码解释字段）⭐ 本讲主实践

这是 spec 指定的实践任务：**在传输示例运行时，抓取元数据服务里某个 segment 的 JSON 描述，对照源码解释每个字段的含义**。

1. **实践目标**：亲眼看到「一个 RDMA 段」在元数据服务里到底长什么样，并把每个 JSON 字段映射回 `encodeSegmentDesc` 的代码行。

2. **操作步骤**：

   a. 启动一个 HTTP 元数据服务（u1-l5 用过的轻量服务），默认监听 `127.0.0.1:8080`：

   ```bash
   cd mooncake-transfer-engine/example/http-metadata-server-python
   python3 bootstrap_server.py
   ```

   该服务把所有 KV 暴露为 `GET/PUT/DELETE /metadata?key=<key>`，见 [bootstrap_server.py:L27](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/example/http-metadata-server-python/bootstrap_server.py#L27) 与 [L41-L48](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/example/http-metadata-server-python/bootstrap_server.py#L41-L48)。C++ 版的 HTTP 元数据服务路由完全一致：`GET /metadata?key=<key>`，见 [http_metadata_server.cpp:L25-L45](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-store/src/http_metadata_server.cpp#L25-L45)。

   b. 在另一个终端跑一个传输示例（如 `transfer_engine_bench` 或 u1-l5 的双端示例），让某个进程用 `metadata_server=127.0.0.1:8080` 注册了至少一段内存。设本端 server name 为 `127.0.0.1:<rpc_port>`（即段名）。

   c. 用 curl 抓取这一段的 JSON（key 前缀来自 [getFullMetadataKey](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L169-L184)）：

   ```bash
   curl -s "http://127.0.0.1:8080/metadata?key=mooncake/ram/127.0.0.1:<rpc_port>" | python3 -m json.tool
   ```

   也可以顺便抓 RPC 路由表：

   ```bash
   curl -s "http://127.0.0.1:8080/metadata?key=mooncake/rpc_meta/127.0.0.1:<rpc_port>" | python3 -m json.tool
   ```

   d. 对照下表解释每个字段（以 RDMA 段为例）：

   | JSON 字段 | 来自代码 | 含义 |
   | --- | --- | --- |
   | `name` | [L317](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L317) | 段名，通常即 server name（`ip:rpc_port`） |
   | `protocol` | [L318](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L318) | 传输协议，如 `"rdma"` |
   | `tcp_data_port` | [L319](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L319) | TCP 数据通道端口（TCP/降级传输用） |
   | `timestamp` | [L320](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L320) | 这次注册的时间，用于判断新旧 |
   | `rdma_server_name` | [L321-L323](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L321-L323) | 仅双网卡（`MC_RDMA_BIND_ADDRESS`）时出现，RDMA 可达名 |
   | `devices[].name` | [L331](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L331) | 网卡设备名，如 `roceP1` |
   | `devices[].lid` | [L332](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L332) | IB 子网本地 ID |
   | `devices[].gid` | [L333](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L333) | IB 全局 ID，RDMA 寻址用 |
   | `buffers[].name` | [L341](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L341) | buffer 名 |
   | `buffers[].addr` | [L342](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L342) | 内存起始地址（十六进制） |
   | `buffers[].length` | [L343](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L343) | 内存长度 |
   | `buffers[].rkey[]` | [L344-L346](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L344-L346) | 远程访问密钥，每张网卡一个 |
   | `buffers[].lkey[]` | [L347-L349](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L347-L349) | 本地访问密钥，与 rkey 一一对应 |
   | `priority_matrix` | [L353](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L353) | 多网卡选路优先级矩阵（`topology.toJson()`） |

3. **需要观察的现象**：
   - `rkey` 与 `lkey` 是等长的数组，长度 = 注册了 MR 的网卡数量。
   - 如果用 TCP 协议跑，JSON 里**没有** `devices` / `rkey` / `lkey` / `priority_matrix`，只有 `buffers[].{name,addr,length}`——印证了 4.2 说的「按协议分支编码」。
   - RPC 路由表那个 key 的值非常简单：`{"ip_or_host_name": "...", "rpc_port": ...}`（见 [transfer_metadata.cpp:L1164-L1167](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L1164-L1167)）。

4. **预期结果**：你能指着抓到的任意一行 JSON，说出它对应 `encodeSegmentDesc` 的哪一行、存进哪个结构体字段、对端拿去干什么。

5. **说明**：具体的段名（`<rpc_port>` 的值）取决于你启动示例时的参数，本讲无法替你确定，请以本地实际抓到的 key 为准。`curl` 抓取的具体输出「待本地验证」。

> 备选方案（无法运行示例时）：源码阅读型实践——阅读 [transfer_metadata_dump.cpp:L67-L91](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata_dump.cpp#L67-L91) 的 `dumpMetadataContentUnlocked()`，它会把「缓存里的全部段描述 + 本地/远端 RPC 路由」一次性 LOG 出来，等价于把上面 curl 到的两类内容在进程内部打印一遍。

#### 4.4.5 小练习与答案

**Q1**：为什么 `qp_num` 放在握手时交换的 `HandShakeDesc` 里，而不是放进 `SegmentDesc` 一起登记到元数据服务？
> **答**：因为 QP（队列对）是**每条连接**独立创建的，A 连 B 和 C 连 B 用的是 B 上不同的 QP；而且 QP 状态在连接建立过程中会迁移（RTS/RTT 等）。把它塞进全局共享的段描述既语义不符、也会造成写放大。所以段描述只登记「相对稳定」的网卡 gid 与内存 rkey，QP 在真正建链时通过握手按需交换。见 [transfer_metadata.cpp:L66-L93](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L66-L93)。

**Q2**：`readString` 为什么要用 `MC_HANDSHAKE_MAX_LENGTH` 限制读取长度（默认 1MB）？
> **答**：防 DoS。socket 上先读到的是对端声称的 8 字节 length，若不设上限，恶意/异常对端可以声称一个超大 length 让本端分配巨量内存。限制在 1MB~128MB 之间（可配）把风险约束住。见 [common.h:L411-L436](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/common.h#L411-L436) 与 [common.h:L456-L459](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/common.h#L456-L459)。

---

## 5. 综合实践

把本讲四个模块串起来，做一个小型端到端追踪任务（不需要 RDMA 硬件，TCP 即可）：

**任务**：跑一个双进程 TCP 传输，从「注册内存」一直追到「对端读到段描述」，沿途标注每一步落在哪个源码函数。

**建议步骤**：

1. 用 `metadata_server=127.0.0.1:8080` 启动 HTTP 元数据服务（[bootstrap_server.py](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/example/http-metadata-server-python/bootstrap_server.py)）。
2. 启动 sender：`init("127.0.0.1:8080")` → `registerLocalMemory(buf, len, ...)`。追踪调用链：
   - `TransferEngineImpl::registerLocalMemory`（[transfer_engine_impl.cpp:L564-L587](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_engine_impl.cpp#L564-L587)）
   - → transport 的 `registerLocalMemory` 构造 `BufferDesc`（[tcp_transport.cpp:L703-L716](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/tcp_transport/tcp_transport.cpp#L703-L716)）
   - → `metadata_->addLocalMemoryBuffer`（[transfer_metadata.cpp:L1094-L1106](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L1094-L1106)）
   - → `updateSegmentDesc` → `encodeSegmentDesc` → `storage_plugin_->set`（[transfer_metadata.cpp:L466-L485](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L466-L485)）
3. 这时用 4.4.4 的 curl 抓取 `mooncake/ram/127.0.0.1:<port>`，确认 JSON 里出现了你注册的那块 buffer（`addr`/`length` 对得上）。
4. 启动 receiver，对 sender 发起一次 `write`。追踪对端如何拿到段描述：
   - `getSegmentDescByName("127.0.0.1:<port>")`（[transfer_metadata.cpp:L973-L1009](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L973-L1009)）
   - → `getSegmentDesc` → `storage_plugin_->get` → `decodeSegmentDesc`（[transfer_metadata.cpp:L877-L936](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L877-L936)）
   - → 命中握手时 `sendHandshake` 顺带建好连接（[transfer_metadata.cpp:L1270-L1289](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L1270-L1289)）
5. **产出**：画一张时序图，横轴是时间，纵轴是「sender 进程 / 元数据服务 / receiver 进程」，把上面每一步标在对应泳道上，并注明涉及的源码文件:行号。

> 如果手头没有可运行的双端环境，这个任务也可以纯阅读完成：按上面列出的调用链，逐个打开 permalink，用自己的话复述每一步输入输出。

## 6. 本讲小结

- **段（Segment）是身份，缓冲区（Buffer）是内存**。一个进程向集群登记一个段，段里挂着若干 buffer 描述。`SegmentDesc` / `BufferDesc` / `DeviceDesc` 是这组信息的载体（[transfer_metadata.h:L45-L121](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/transfer_metadata.h#L45-L121)）。
- **数据走高速通道，元数据走控制通道**。`encodeSegmentDesc` 把 struct 变 JSON 写进元数据服务，`decodeSegmentDesc` 在对端把它变回 struct，并做完整性校验（[transfer_metadata.cpp:L277-L464](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L277-L464) 与 [L612-L856](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L612-L856)）。
- 编解码**按协议分支**：rdma 带 devices/rkey/lkey/priority_matrix，tcp 只带 addr/length，cxl 带 offset，nvlink 带 shm_name……同一个结构体被不同协议复用。
- `TransferMetadata` 是**协调者**：管本地段（COW 更新）、缓存远端段、持有两个插件；支持中心化（etcd/redis/http）与 P2P 握手两种模式（[transfer_metadata.cpp:L130-L165](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L130-L165)）。
- **两个插件接口**：`MetadataStoragePlugin`（`get/set/remove` 三实现）是中心化 KV 通道；`HandShakePlugin`（socket + JSON 帧）是点对点握手通道，负责交换 RDMA 的 QP 等连接参数（[transfer_metadata_plugin.h:L21-L73](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/transfer_metadata_plugin.h#L21-L73)）。
- **RDMA 的「交换信息」其实是三部分**：段描述里的 `devices`(gid/lid) + `buffers`(rkey/lkey) + 握手时的 `qp_num`。仓库里没有名为 `RdmaExchange` 的结构体，这三个部分共同构成完整的 RDMA 建链信息。

## 7. 下一步学习建议

- **继续向下读 transport**：本讲只讲了「元数据怎么流动」。下一站建议读 `rdma_transport` / `tcp_transport`，看它们拿到 `SegmentDesc` 后，如何用 `rkey` + `qp_num` 真正发起一次 RDMA write——把 4.4 提到的「三部分交换信息」在数据通路上闭环。
- **读 `Topology`**：段描述里的 `priority_matrix` 来自 [topology.h](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/topology.h)。它决定多网卡场景下优先走哪条路，是理解 Mooncake 多网卡选路的关键。
- **回到上层**：如果想看 `TransferMetadata` 是怎么被 `TransferEngineImpl` 编排进 `init` / `submitTransfer` 全流程的，重温 u2-l1，并关注 `MultiTransport` 如何把多个 transport 的段注册结果汇总到同一个 `metadata_`。
- **存储插件扩展**：如果将来要接入自研的元数据后端（比如 Consul、ZooKeeper），只需继承 `MetadataStoragePlugin` 实现 `get/set/remove` 三个方法，再在 [transfer_metadata_plugin.cpp:L544-L596](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata_plugin.cpp#L544-L596) 的工厂里加一个分支即可——这是本讲插件机制最直接的实战出口。

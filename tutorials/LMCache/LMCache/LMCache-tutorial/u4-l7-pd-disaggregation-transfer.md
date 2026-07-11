# 讲义：PD 分离与传输通道

## 1. 本讲目标

Prefill/Decode 分离（PD Disaggregation）是大规模 LLM 服务里提升吞吐的关键架构：把「算长 prompt」的 prefill 和「逐 token 生成」的 decode 拆到两组不同的 GPU 实例上，让两类截然不同的负载各自跑到最优。但拆开之后，prefill 算出来的 KV cache 必须以某种方式搬到 decode 节点，否则 decode 节点就要重算一遍，分离就毫无意义。

本讲聚焦 LMCache 解决「跨节点搬 KV」的具体机制——**传输通道（transfer channel）**。学完后你应该能够：

1. 说清楚 PD 分离的动机，以及 KV cache 从 prefill worker 流转到 decode worker 的完整链路。
2. 读懂 `PDBackend` 如何以一个存储后端的身份扮演 sender / receiver / both 三种角色，完成「远端分配 + 零拷贝搬运 + 代理通知」三件事。
3. 区分仓库里**两套并存的传输通道抽象**：引擎内的 `v1/transfer_channel/`（`BaseTransferChannel`，PD 实际用的那一套）与分布式 L2 的 `v1/distributed/transfer_channel/`（`TransferChannelContext`，给 P2P L2 用），并能说明它们的接口与 NIXL 实现的差别。
4. 理解 NIXL/RDMA 的两阶段握手与「一侧写（one-sided WRITE）」模型，以及 UCX 后端如何在 NVLink / RDMA / TCP / 共享内存之间自动选择底层传输。

---

## 2. 前置知识

在进入正文前，请确认你已经理解下面几个概念（本讲建立在 u2-l3「存储后端层次」与 u3-l1「多进程架构总览」之上）：

- **KV cache 与 prefill/decode**：注意力机制为历史 token 缓存的 Key/Value 张量。prefill 阶段一次性算完整个 prompt 的 KV，decode 阶段逐 token 追加。详见 u1-l1。
- **存储后端契约 `StorageBackendInterface` / `AllocatorBackendInterface`**：LMCache 把所有「能按 `CacheEngineKey` 存取 `MemoryObj`」的组件抽象成后端。PD 后端是其中一个特殊后端。详见 u2-l3。
- **`MemoryObj` 与内存分配器**：LMCache 对一块 KV 内存的统一包装，带 `address`（在预分配大 buffer 里的偏移）、`ref_count`、`meta` 等。详见 u4-l6。
- **no fate-sharing**：把 KV 管理从引擎进程里拆出来，引擎崩了不连累 cache。PD 分离把这一思想推到「prefill 与 decode 也是两个独立进程/实例」。详见 u3-l1。
- **ZMQ 的 REQ/REP/ROUTER/DEALER/PUSH socket 模型**：本讲里控制面消息（分配请求、握手、代理通知）大量用 ZMQ。REQ/REP 是严格一问一答，PUSH 是单向投递。
- **RDMA 与 NIXL**：RDMA（Remote Direct Memory Access）允许一块 GPU/网卡直接读写另一台机器的内存，不经对方 CPU。NIXL（NVIDIA Inference Transfer Library，前身 ai-dynamo/nixl）是 NVIDIA 提供的统一传输抽象库，把「GPU 显存 / CPU 内存的一段区域注册 + 远端读写」封装成 agent + descriptor list + transfer handle 的 API，底层可走 UCX，而 UCX 会自动选择 NVLink、InfiniBand/RoCE、TCP、CUDA IPC、共享内存中最合适的一条。
- **零拷贝**：控制消息（key、block id、地址偏移）走 ZMQ；KV 张量字节本身从不序列化进 ZMQ，而是靠 NIXL 直接 DMA 到对方已注册的显存/内存。这一点和 u3-l2 的 MP IPC 哲学一致。

> 一个贯穿全讲的核心不变量：**KV 字节永远零拷贝，ZMQ 只搬运元数据。**

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [lmcache/v1/storage_backend/pd_backend.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/pd_backend.py) | PD 后端本体。以 `AllocatorBackendInterface` 身份同时实现 sender（把 KV 推给 decode）与 receiver（在 decode 侧落盘并供 retrieve）两套逻辑。 |
| [lmcache/v1/transfer_channel/abstract.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/transfer_channel/abstract.py) | **引擎内**传输通道抽象 `BaseTransferChannel`：`lazy_init_peer_connection` / `batched_write` / `batched_read` 等。PD 实际用的那一套。 |
| [lmcache/v1/transfer_channel/nixl_channel.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/transfer_channel/nixl_channel.py) | `BaseTransferChannel` 的 NIXL 实现 `NixlChannel`，含两阶段握手与一侧读写。 |
| [lmcache/v1/transfer_channel/\_\_init\_\_.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/transfer_channel/__init__.py) | 工厂 `CreateTransferChannel`，按 `channel_type`（`nixl` / `mock_memory`）路由。 |
| [lmcache/v1/distributed/transfer_channel/abstract.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/transfer_channel/abstract.py) | **分布式 L2** 传输通道抽象：`TransferChannelContext` / `TransferChannelServer` / `TransferChannelClient`，只支持 read。 |
| [lmcache/v1/distributed/transfer_channel/impl/nixl_impl.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/transfer_channel/impl/nixl_impl.py) | 上面那套抽象的 NIXL 实现 `NixlTransferChannelContext/Server/Client`。 |
| [lmcache/v1/distributed/transfer_channel/api.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/transfer_channel/api.py) | 分布式层的线缆数据类型 `TransferChannelAddress`、`TransferChannelReadResult`。 |
| [lmcache/v1/config.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/config.py) | `pd_*` / `transfer_channel` / `nixl_backends` 配置项定义与 PD 校验。 |
| [lmcache/integration/vllm/vllm_v1_adapter.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/integration/vllm/vllm_v1_adapter.py) | `DisaggSpec` 数据结构与 `transfer_spec` 如何从 vLLM 请求流进 `batched_submit_put_task`。 |
| [examples/disagg_prefill/1p1d/configs/](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/examples/disagg_prefill/1p1d) | 真实可跑的 1-prefill-1-decode 配置（prefiller / decoder 各一份 YAML）。 |

> 设计文档：[docs/design/v1/pd_async_reservation_design.md](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/docs/design/v1/pd_async_reservation_design.md) 讲异步 PD 后端的「预留式准入控制」如何避免多请求并发分配导致的死锁。

---

## 4. 核心概念与源码讲解

### 4.1 PD 分离总览：动机、角色与端到端链路

#### 4.1.1 概念说明

为什么要 PD 分离？prefill 是**计算密集**（要为整段 prompt 算满 KV），decode 是**访存密集**（每步只算一个 token，但要读全部历史 KV）。两者混跑在同一批 GPU 上会互相干扰：一个长 prompt 的 prefill 会卡住一堆正在 decode 的短请求。把它们拆到「prefill 集群」和「decode 集群」后，各自能独立批处理、独立调显存，吞吐显著提升。

但分离带来一个新问题：**decode 节点没有这段 prompt 的 KV**。两条出路：

1. decode 节点重算 prompt（浪费，等于没分离）；
2. **把 prefill 节点算出的 KV 搬到 decode 节点**——这正是 LMCache 的传输通道要做的。

LMCache 的设计哲学是：PD 传输也走「存储后端」这套抽象。prefill 节点的 `LMCacheEngine` 把 KV `store` 进一个叫 `PDBackend` 的后端，而这个后端并不真的存——它立刻把数据通过 NIXL 推给 decode 节点；decode 节点的 `PDBackend` 收到后落进自己的显存 buffer，供后续 `retrieve` 使用。这就是 `PDBackend` 类文档里那句「At the sender side, it will never save anything but directly write the data to the receiver side」的含义。

PD 链路里有三种角色（由 `pd_role` 配置）：

- **sender**（prefill 侧）：算完 KV 就往 receiver 推，并在 prefill 结束时通知代理。
- **receiver**（decode 侧）：监听分配请求，在本地 buffer 里给远端 KV 腾位置，收下后供 retrieve。
- **both**：双向模式——既能往对端写新 KV，也能反向查询/读取对端已经缓存的 KV（用于「decode 侧也缓存了某些块、prefill 侧复用」的场景）。

#### 4.1.2 核心流程

一条请求从进 prefill 到在 decode 上复用的端到端时序：

```text
[Router/Proxy]                         [Prefill Worker (sender)]              [Decode Worker (receiver)]
    |  ① 发请求(max_tokens=1)  ────────►|                                       |
    |                                   |  ② prefill 算完 KV                     |
    |                                   |  ③ engine.store(keys, mem_objs,        |
    |                                   |        transfer_spec=DisaggSpec)       |
    |                                   |  ④ PDBackend.batched_submit_put_task:  |
    |                                   |     - ZMQ REQ ► AllocRequest ─────────►|  ⑤ 收 AllocRequest
    |                                   |     ◄ AllocResponse (remote_indexes) ◄──|     分配 buffer slot，pin
    |                                   |  ⑥ NIXL WRITE (一侧直写 receiver 显存) ─►|  (零拷贝，不经 receiver CPU)
    |                                   |  ⑦ 最后一chunk: ZMQ PUSH ► ProxyNotif ─►|                                       |
    |  ⑧ router 收到通知，知道 KV 备好了                                          |
    |  ⑨ 发完整请求给 decode ──────────────────────────────────────────────────►|  ⑩ decode retrieve(KV 已在本地)
```

关键点：步骤 ④–⑥ 是本讲的核心——**控制面用 ZMQ（分配、通知），数据面用 NIXL（RDMA 直写）**。步骤 ⑦ 的代理通知（`ProxyNotif`）是给外部 router 的信号：prefill 的 KV 已经在 decode 侧就位，可以把请求转给 decode 了。`examples/disagg_prefill_mp/README.md` 描述的就是这套「router 等 prefill store 完成事件再转发」的协作。

#### 4.1.3 源码精读：配置与接线

PD 由 `enable_pd: True` 开启。所有 `pd_*` 字段集中定义在配置表里（回顾 u1-l5：「一张表驱动一切」）：

[lmcache/v1/config.py:228-312](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/config.py#L228-L312) —— 定义 `enable_pd`、`pd_role`、`pd_buffer_size`、`pd_buffer_device`、`pd_peer_host`、`pd_peer_init_port`、`pd_peer_alloc_port`、`pd_peer_query_port`、`pd_proxy_host/port`、`pd_backend_mode`、`pd_skip_proxy_notification`、`pd_bidirectional`，以及传输相关 `transfer_channel`、`nixl_backends`。注意端口字段是 `list[int]`——按 tensor-parallel rank 索引，每个 rank 一个端口。

`_validate_config` 里对 PD 做跨字段约束，最关键的两条：

[lmcache/v1/config.py:745-784](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/config.py#L745-L784) —— (a) PD 强制 `save_unfull_chunk=True`（否则最后一个不满的 chunk 会被丢弃，KV 传不全，decode 结果就错了）；(b) receiver 的 `retrieve_locations` 必须是 `["PDBackend"]`、`store_location` 不能是 `PDBackend`——因为当前 PDBackend 是单向的（producer → receiver）。

后端工厂在组装存储后端时，按 `pd_backend_mode` 选择同步或异步实现：

[lmcache/v1/storage_backend/\_\_init\_\_.py:132-143](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/__init__.py#L132-L143) —— `enable_pd` 为真时，`pd_backend_mode=="async"` 选 `PDBackendAsync`（默认，跑 asyncio 事件循环 + 预留式准入），否则选本讲精读的同步版 `PDBackend`。两者共享同一套 `PDConfig` 与消息协议，区别在调度模型。

`transfer_spec`（即 `DisaggSpec`）从 vLLM 请求一路传到后端：

[lmcache/integration/vllm/vllm_v1_adapter.py:82-92](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/integration/vllm/vllm_v1_adapter.py#L82-L92) —— `DisaggSpec` 携带 `req_id`、`receiver_host`、`receiver_init_port`、`receiver_alloc_port`、`receiver_query_port`（皆按 rank）、`is_last_prefill`、`num_transferred_tokens`、`total_chunks`。它来自请求的 `kv_transfer_params["disagg_spec"]`（由外部 router 注入），存进 `RequestTracker.disagg_spec`，最终作为 `transfer_spec` 参数传给 `PDBackend.batched_submit_put_task`。若 `transfer_spec is None`，说明是本地请求，后端直接跳过传输（见 4.2.3）。

#### 4.1.4 代码实践：读真实配置，还原角色

**实践目标**：通过两份真实 YAML，亲眼确认 sender 与 receiver 各自需要哪些字段，并验证「端口按 rank 索引」「buffer 对齐到 chunk」两个事实。

**操作步骤**：

1. 打开 [examples/disagg_prefill/1p1d/configs/lmcache-prefiller-pd-with-remote-config.yaml](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/examples/disagg_prefill/1p1d/configs/lmcache-prefiller-pd-with-remote-config.yaml)（sender）。注意它**没有** `pd_peer_*` 字段——sender 是连接的发起方，peer 信息在运行期通过 `DisaggSpec` 由 router 告诉它；它只需要 `pd_proxy_host/port`（发完成通知）。
2. 打开 [examples/disagg_prefill/1p1d/configs/lmcache-decoder-pd-with-remote-config.yaml](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/examples/disagg_prefill/1p1d/configs/lmcache-decoder-pd-with-remote-config.yaml)（receiver）。注意它有 `pd_peer_host`、`pd_peer_init_port: 7300`、`pd_peer_alloc_port: 7400`——receiver 要 **bind** 这些端口等 sender 连。
3. 对照 4.1.3 的校验逻辑，确认 receiver 的 `retrieve_locations: ["PDBackend"]` 与 `store_location: "RemoteBackend"`，满足「receiver 不能把 PD 当 store」的约束。

**需要观察的现象 / 预期结果**：

- sender 配置里有 `pd_role: sender` + proxy 信息，无 peer 端口；receiver 配置里有 `pd_role: receiver` + peer 端口。两者 `transfer_channel: "nixl"`、`nixl_backends: [UCX]` 一致。
- 两份都写了 `pd_buffer_size`。按代码 `initialize_allocator` 的对齐逻辑（见 4.2），实际分配的字节数会被向下取整到「单个 chunk 字节数」的整数倍。

> ⚠️ 待本地验证：如果你手头有 GPU，可以照 `examples/disagg_prefill_mp/README.md` 起两个 vLLM 实例（`kv_role:"kv_both"`）+ proxy，发一条 curl 请求，观察 prefill 日志里出现 `NIXL write` / proxy 通知，decode 日志里出现 cache 命中。无 GPU 环境下，本实践退化为纯配置阅读。

#### 4.1.5 小练习与答案

**Q1**：为什么 PD 模式下 `_validate_config` 要强制 `save_unfull_chunk=True`？

**答**：一段 prompt 的 KV 会被切成多个 chunk，最后一个 chunk 通常是不满的（token 数 < chunk_size）。如果 `save_unfull_chunk=False`，这个尾巴 chunk 会被丢弃，导致传给 decode 的 KV 不完整，decode 继续生成时 attention 会读到错误上下文，输出错乱。PD 必须传完整 KV，故强制开启。

**Q2**：sender 配置里为什么不需要 `pd_peer_init_port`？

**答**：因为 sender 是连接发起方。它在运行期从 `DisaggSpec`（由 router 在请求里注入）拿到 receiver 的 host/port，再通过 `_ensure_peer_connection` 懒建立连接（见 4.2.3）。而 receiver 必须在启动时就 bind 固定端口等连接，所以 receiver 配置才需要写死 `pd_peer_*` 端口。

---

### 4.2 PDBackend：sender 与 receiver 的收发编排

#### 4.2.1 概念说明

`PDBackend` 是 PD 链路的中枢。它实现 `AllocatorBackendInterface`（既是存储后端、又是内存分配器），但行为与普通后端截然不同：

- 它**不在本地磁盘/远端**存数据，而是用一块预分配的 GPU/CPU 大 buffer（`PagedCpuGpuMemoryAllocator`）当 KV 暂存区。
- 同一个类，按 `pd_role` 走完全不同的初始化分支：sender 只搭「往对端写」的管道；receiver 只搭「收对端写」的管道；both 两者都搭。
- 通信骨架是 ZMQ + msgspec：用 `PDMsg` 这个带 tag 的联合体（`AllocRequest` / `AllocResponse` / `ProxyNotif` / `CacheQueryRequest` / `CacheQueryResponse`）在两端交换**元数据**；真正的 KV 字节走 `transfer_channel`（NIXL）。

#### 4.2.2 核心流程

**Sender 写路径**（`batched_submit_put_task`，这是被 `engine.store` 调用的入口）：

```text
1. 若 transfer_spec is None → 本地请求，返回 None（不传）
2. 给所有 mem_obj ref_count_up
3. _ensure_peer_connection  ← 首次对某 receiver 建立 NIXL peer + ZMQ REQ 分配 socket（幂等）
4. _remote_allocate          ← ZMQ REQ 发 AllocRequest(keys,fmt,shape,dtype,last_chunk_toks)
                                 receiver 回 AllocResponse(already_sent_indexes, remote_indexes)
5. 过滤掉 already_sent 的 mem_obj（去重，ref_count_down），得到真正要发的子集
6. 把 ("write", mem_objs, channel_spec, keys, callback, transfer_spec, future) 投入 _nixl_queue
7. 立即返回 [future]  ← 调用方不阻塞
```

worker 线程 `_nixl_worker_loop` 从队列取出 `"write"`，在 `_nixl_agent_lock` 保护下调 `transfer_channel.batched_write`（NIXL 一侧写），完成后：若 `is_last_prefill` 就给 proxy 发 `ProxyNotif`（ZMQ PUSH），跑 `on_complete_callback`，最后 `future.set_result(num_written)`。

**Receiver 收路径**：

```text
_mem_alloc_loop（后台线程，ZMQ REP）:
  1. recv AllocRequest
  2. _allocate_and_put:
       for each key:
         若 contains(key, pin=True) → 该 key 之前已发过，记入 already_send_indexes
         否则在本地 buffer 分配 mem_obj（busy-loop 重试直到成功），put(key, mem_obj)，
              把 mem_obj.meta.address 记入 alloc_indexes（这就是 remote_indexes）
  3. send AllocResponse(already_sent_indexes=..., remote_indexes=alloc_indexes)
```

注意 receiver **不主动收数据**——NIXL WRITE 是一侧操作，sender 直接 DMA 进 receiver 的显存。receiver 要做的只是在「正确的偏移」预留好 buffer slot，把偏移（`address`）回给 sender。这就是 `get_blocking` 那句「we assume that the key must be in local data because we are using a push-based transfer」的由来。

#### 4.2.3 源码精读

先看消息定义与角色校验：

[lmcache/v1/storage_backend/pd_backend.py:41-86](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/pd_backend.py#L41-L86) —— `PDMsgBase` 是 `msgspec.Struct, tag=True`，解码时按 tag 还原成具体子类。`AllocRequest.keys` 的长度即 chunk 数；`last_chunk_toks` 单独传，因为最后一块可能不满。

[lmcache/v1/storage_backend/pd_backend.py:113-119](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/pd_backend.py#L113-L119) —— `PDConfig.from_cache_engine_config` 断言 `role in ["sender","receiver","both"]`，并按角色决定哪些字段必填（receiver 必须有 peer 端口；sender 默认要有 proxy；both 两者都要）。

构造期创建传输通道与 worker 线程：

[lmcache/v1/storage_backend/pd_backend.py:254-275](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/pd_backend.py#L254-L275) —— 调 `CreateTransferChannel(...)` 拿到 `NixlChannel`（见 4.3），随后立刻起一个专用 daemon 线程 `_nixl_worker_loop`。注释点明动机：所有 NIXL GPU 操作集中在这个单线程，避免与 vLLM worker 抢 CUDA context，从而避免 vLLM v0.19.0 多进程执行器的 RPC 超时。

buffer 对齐逻辑（解释 4.1.4 里说的「向下取整」）：

[lmcache/v1/storage_backend/pd_backend.py:316-334](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/pd_backend.py#L316-L334) —— 先算单个 chunk 字节数 `chunk_size_bytes`，再把 `pd_buffer_size` 向下取整为它的整数倍 `aligned_buffer_size`，余数不分配。若对齐后为 0（buffer 比一个 chunk 还小）直接抛错。

sender 的发送主流程：

[lmcache/v1/storage_backend/pd_backend.py:498-598](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/pd_backend.py#L498-L598) —— `batched_submit_put_task`。要点：(1) `transfer_spec is None` 直接 `return None`（本地请求跳过）；(2) 用 `already_sent_indexes` 去重——若 receiver 已经有某些 key（之前发过），就不再重发并 `ref_count_down`；(3) 真正的 NIXL `batched_write` 不在这里调，而是塞进 `_nixl_queue`，返回 `Future`，保证 vLLM worker 线程不阻塞。

worker 线程的三种操作：

[lmcache/v1/storage_backend/pd_backend.py:600-700](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/pd_backend.py#L600-L700) —— `_nixl_worker_loop` 处理 `"write"` / `"notify_only"` / `"read"` 三类任务。`write` 分支在 `_nixl_agent_lock` 内调 `batched_write`，成功且 `is_last_prefill` 时向 proxy PUSH `ProxyNotif`，最后无论成败都 `ref_count_down` 释放 sender 暂存区，并 resolve future。`notify_only` 用于「数据已发过、只需补发完成通知」的场景；`read` 服务于双向模式（4.2.4 之外的进阶路径）。

receiver 的分配循环：

[lmcache/v1/storage_backend/pd_backend.py:910-977](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/pd_backend.py#L910-L977) —— `_allocate_and_put` 对每个 key：若已存在就 `contains(pin=True)` 记为已发；否则分配（失败时 busy-loop 重试到 `pd_allocation_timeout_sec`），`put(key, mem_obj)`，把 `mem_obj.meta.address` 作为 `remote_index` 返回。`_mem_alloc_loop` 是 REP socket 的事件循环，注释解释了「可以先 put 进后端，因为 decode 看到请求前 proxy 必须先收到 ack」这一时序保证。

push 语义下的取回：

[lmcache/v1/storage_backend/pd_backend.py:1040-1046](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/pd_backend.py#L1040-L1046) —— `get_blocking` 直接断言 key 必在 `self.data`，因为数据是 sender 主动推过来的，receiver 只要 allocate 时就登记好了。

#### 4.2.4 代码实践：跟踪一条 AllocRequest 的来回

**实践目标**：理解「sender 远端分配 → receiver 本地落盘 → 返回 remote_indexes」这一去重 + 零拷贝协作。

**操作步骤**：

1. 在 [pd_backend.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/pd_backend.py) 里定位三个方法：`_get_remote_alloc_request`（L467-495，把 keys/mem_objs 打包成 `AllocRequest`）、`_remote_allocate`（L457-465，ZMQ REQ 发、收 `AllocResponse`）、`_allocate_and_put`（L910-951，receiver 侧处理）。
2. 假设 sender 要发 3 个 chunk（key A/B/C），但 receiver 已经有 B（之前请求发过）。手工推演：
   - `_remote_allocate` 发 `AllocRequest(keys=[A,B,C])`；
   - receiver `_allocate_and_put` 对 A、C 分配新 slot 得到 address `a1, c1`，对 B 命中 `contains(pin=True)` 记 `already_send_indexes=[1]`；
   - 回 `AllocResponse(already_sent_indexes=[1], remote_indexes=[a1, c1])`；
   - sender 过滤掉 index 1 的 B（`ref_count_down`），只把 A、C 塞进 `_nixl_queue`，`channel_spec={"receiver_id":..., "remote_indexes":[a1,c1]}`。

**需要观察的现象 / 预期结果**：

- 同一段 prompt 被两次 prefill 时，第二次大量 chunk 命中 `already_sent_indexes`，`mem_objs_to_send` 变短，NIXL 实际传输量下降——这就是 PD 路径里的隐式去重。
- NIXL WRITE 用的是 `remote_indexes`（receiver buffer 偏移），而**不是 key**——KV 字节直接落到 receiver 显存的指定偏移，receiver CPU 全程不碰这些字节。

#### 4.2.5 小练习与答案

**Q1**：`_nixl_worker_loop` 为什么必须是**单**线程，且持有 `_nixl_agent_lock`？

**答**：NIXL agent 内部维护 peer handshake 状态与 transfer handler 表，并发调用会引起 CUDA context 争用与状态错乱；同时 vLLM worker 线程在跑模型 forward，若 NIXL 在同一线程阻塞（`batched_write` 里轮询 `check_xfer_state`），会触发 vLLM 多进程执行器的 RPC 超时。单 worker 线程 + 锁既串行化了 agent 状态，又把阻塞搬离了 vLLM 关键路径。

**Q2**：receiver 的 `get_blocking` 敢断言「key 一定在本地」，依据是什么？万一 sender 还没传完呢？

**答**：依据是时序契约——receiver 在 `_allocate_and_put` 里**先**把 mem_obj 登记进 `self.data`，**再**回 `AllocResponse`；sender 收到响应后才开始 NIXL WRITE。而 decode 引擎只有在外部 router 收到 `ProxyNotif`（prefill 全部传完）之后才会把请求转给 decode。所以等 decode 调 `retrieve` 时，KV 一定已就位。这是控制面（ZMQ 通知）为数据面（RDMA）兜底的典型设计。

---

### 4.3 v1/transfer_channel：PD 的传输底座 BaseTransferChannel 与 NixlChannel

#### 4.3.1 概念说明

`PDBackend` 不直接调 NIXL，而是调 `BaseTransferChannel` 这个抽象。它是「**进程内**（in-process）传输通道」：同一个 worker 进程持有一个 channel 对象，channel 内部封装了 NIXL agent 与 ZMQ 握手逻辑。

抽象提供两类原语（见 [abstract.py:150-246](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/transfer_channel/abstract.py#L150-L246)）：

- **成对原语 `batched_send` / `batched_recv`**：两端都要参与（类似 socket 的 send/recv）；
- **一侧原语 `batched_write` / `batched_read`**：只在发起方调用，对端不感知（依赖 RDMA 一侧读写）。PD 用的是这一对。

以及 `lazy_init_peer_connection`：首次与某 peer 通信时建立 NIXL 连接。工厂 `CreateTransferChannel` 按 `channel_type` 路由，目前只支持 `"nixl"` 与测试用 `"mock_memory"`：

[lmcache/v1/transfer_channel/\_\_init\_\_.py:41-63](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/transfer_channel/__init__.py#L41-L63) —— 断言 `channel_type in ["nixl","mock_memory"]`，`"nixl"` 分支延迟导入 `NixlChannel` 并要求 kwargs 里有 `backends`。

> 谁在用这套？grep 显示：`pd_backend.py`、`pd_backend_async.py`、`p2p_backend.py`。也就是说 **PD（与 legacy p2p）共享 `v1/transfer_channel` 这一套**。

#### 4.3.2 核心流程

**两阶段握手**（`lazy_init_peer_connection`，发起方；对端在 `_init_loop` 的 REP socket 上响应）。之所以分两阶段，代码注释明确写了：合在一起做会让 nixl 在首次请求时卡死（handle 永远返回 `"PROC"`）。

```text
阶段 1：交换 agent 元数据
  发起方 ──NixlInitRequest(local_meta_bytes)──►  对端
  发起方 ◄──NixlInitResponse(remote_meta_bytes)──  对端
  双方各自 add_remote_agent(对端 meta)  → 得到 remote_agent_name

阶段 2：注册彼此的内存描述符
  发起方 ──NixlMemRegRequest(local_xfer_dlist_bytes)──►  对端
  发起方 ◄──NixlMemRegResponse(remote_xfer_dlist_bytes)──  对端
  双方各自 deserialize + prep_xfer_dlist → 得到 remote_xfer_handlers[peer_id]
```

握手之后，双方都持有「对端那段 buffer 的 transfer descriptor handle」，于是可以发起一侧传输。

**一侧写**（`batched_write`，sender 调）：

```text
handle = nixl_agent.make_prepped_xfer(
            "WRITE",
            local_xfer_handler,           # 本地 buffer 的 page 列表
            get_local_mem_indices(objects), # 要写的本地页偏移
            remote_xfer_handlers[receiver_id],  # 对端 buffer handle
            transfer_spec["remote_indexes"])    # 对端目标页偏移
nixl_agent.transfer(handle)
轮询 check_xfer_state(handle) 直到 DONE（或 ERR 抛错）
```

`get_local_mem_indices` 把 `MemoryObj` 映射成它在大 buffer 里的页索引（`mem_obj.meta.address`）。读写都靠「本地索引 + 对端索引」配对，KV 字节由 NIXL 直接 DMA，对端 CPU 不参与。

#### 4.3.3 源码精读

抽象一侧原语：

[lmcache/v1/transfer_channel/abstract.py:215-246](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/transfer_channel/abstract.py#L215-L246) —— `batched_write` / `batched_read` 的契约，都接收 `objects/buffers` 与可选 `transfer_spec`，返回成功搬运的对象数。注释强调「Read and Write only need to be called on one side」。

NIXL 写实现：

[lmcache/v1/transfer_channel/nixl_channel.py:419-460](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/transfer_channel/nixl_channel.py#L419-L460) —— `batched_write`。`transfer_spec` 必须含 `receiver_id` 与 `remote_indexes`（由 PDBackend 的 `AllocResponse` 提供）。`make_prepped_xfer("WRITE", ...)` 之后 `transfer`，然后 `while` 轮询状态：`"ERR"` 抛 `RuntimeError`，`"PROC"` 继续 sleep，`"DONE"` 跳出。读实现 [L462-505](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/transfer_channel/nixl_channel.py#L462-L505) 结构对称，差别是方向 `"READ"`、`transfer_spec` 含 `sender_id`。

两阶段握手发起方：

[lmcache/v1/transfer_channel/nixl_channel.py:121-180](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/transfer_channel/nixl_channel.py#L121-L180) —— 注意它每次握手都用一个**临时** REQ socket（`init_tmp_socket`），结束后 `close()`；握手产物 `remote_xfer_handlers` 存进 `self.remote_xfer_handlers_dict[peer_id]`，后续传输直接复用。

NIXL agent 与内存注册：

[lmcache/v1/transfer_channel/nixl_channel.py:615-688](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/transfer_channel/nixl_channel.py#L615-L688) —— `NixlAgentWrapper`。关键点：(1) `[buffer_ptr, buffer_ptr+buffer_size)` 这段连续内存按 `page_size` 切成一个个定长描述符，整体 `register_memory` + `prep_xfer_dlist`，得到本地 `xfer_handler`；(2) 设备类型映射：`cpu → "cpu"`，`{cuda,xpu,hpu} → "VRAM"`（[L662-673](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/transfer_channel/nixl_channel.py#L662-L673)），其他抛错；(3) `tp_rank` 作为 NIXL 的 `dev_id` 写进描述符，保证多卡场景下各 rank 的内存不混淆。

对端响应循环：

[lmcache/v1/transfer_channel/nixl_channel.py:299-332](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/transfer_channel/nixl_channel.py#L299-L332) —— `_init_loop` 在 REP socket 上接 `NixlInitRequest`/`NixlMemRegRequest`，分别回 agent meta 与本地 xfer descs，并把对端 handle 存好。两个阶段的注释（L310-316）正是「为何分两阶段」的权威解释。

#### 4.3.4 代码实践：换底层传输，观察接口不变

**实践目标**：体会「抽象接口稳定、底层实现可换」的设计。

**操作步骤**：

1. 读 [mock_memory_channel.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/transfer_channel/mock_memory_channel.py)（同目录），它是 `BaseTransferChannel` 的纯内存 mock 实现，用于无 NIXL/GPU 环境跑测试。
2. 对照 `NixlChannel.batched_write` 与 mock 版的 `batched_write`：两者**签名完全一致**（`objects, transfer_spec` → `int`），但一个走 RDMA、一个走进程内拷贝。
3. （可选）读 `tests/v1/storage_backend/test_pd_backend_buffer_alignment.py` 等测试，看测试如何用 mock channel 验证 PDBackend 行为而不需要真 GPU。

**需要观察的现象 / 预期结果**：

- `PDBackend` 里 `self.transfer_channel.batched_write(...)` 这一行对 NIXL 与 mock 都成立——这就是抽象的意义。
- mock 实现里没有 `check_xfer_state` 轮询（同步内存拷贝立即完成），而 NIXL 版必须轮询到 `DONE`。

> 待本地验证：若装了 `nixl`（或 `nixl_cu12`/`nixl_cu13`），可在两进程间跑 `lmcache/tools/transfer_channel_benchmark/benchmark.py` 度量吞吐；否则只能源码阅读。

#### 4.3.5 小练习与答案

**Q1**：`batched_write` 里 `transfer_spec["remote_indexes"]` 是相对什么的偏移？

**答**：是 receiver 在它自己那段已注册 buffer 里的**页偏移**（页大小 = `align_bytes` = 单个 chunk 字节数）。它由 receiver 在 `_allocate_and_put` 时分配 `mem_obj` 得到的 `mem_obj.meta.address` 经 `AllocResponse.remote_indexes` 回传给 sender。sender 据此告诉 NIXL「把本地这些页写到对端那些页」。

**Q2**：为什么握手要分「交换 agent meta」和「注册内存描述符」两阶段，不能一锅端？

**答**：代码注释（nixl_channel.py L310-316）给出实证原因：合在一起会让 nixl 在首次请求时卡住，handle 状态恒为 `"PROC"`。拆成两阶段——先让双方 agent 互相认识（`add_remote_agent`），再交换内存描述符（`prep_xfer_dlist`）——能让 transfer handle 正常进入 `DONE`。这是一个由底层库行为倒推出来的工程约束。

---

### 4.4 v1/distributed/transfer_channel：L2 P2P 抽象与 NIXL 实现

#### 4.4.1 概念说明

仓库里还有**第二套**传输通道抽象，位于 `v1/distributed/transfer_channel/`。它和 4.3 那套**不是同一个东西**，服务的层级也不同：

| 维度 | `v1/transfer_channel/`（4.3） | `v1/distributed/transfer_channel/`（本节） |
| --- | --- | --- |
| 核心抽象 | `BaseTransferChannel`（一个对象） | `TransferChannelContext` + `Server` + `Client`（三个角色） |
| 调用方 | `PDBackend`、legacy `p2p_backend` | `l2_adapters/p2p_l2_adapter`、MP `p2p_controller` |
| 模型 | 进程内对象，直接 `batched_write/read` | 全局单例 Context 持有 server 与一组 client，支持地址翻译 |
| 支持操作 | 一侧 WRITE + 一侧 READ | **只支持 READ**（P2P 场景目前只读对端） |
| 地址表达 | `MemoryObj.meta.address`（页索引） | `TransferChannelAddress(offset, size)`（L1 buffer 内字节偏移） |
| 工厂 | `CreateTransferChannel`（if 分支） | `register_transfer_channel_factory`（注册表 + 自动发现） |

简言之：4.3 是「引擎里直接拿来搬 PD 的 KV」，本节是「分布式 L2 层用来跨实例 P2P 读 L1 缓存」。两者都有 NIXL 实现，但抽象层级和适用场景不同。学习时务必分清，否则会被同名概念绕晕。

#### 4.4.2 核心流程

这套抽象把一次跨节点读拆成「Context 持有传输引擎 + Server 握手 + Client 发起读」：

```text
每个 LMCache 节点启动时:
  TransferChannelContext（单例）
    ├─ 持有一个 nixl agent，注册本节点整个 L1 buffer
    ├─ 持有一个 TransferChannelServer（bind listen_url，等别人来连）
    └─ 持有若干 TransferChannelClient（按 peer advertise_url 缓存，可主动建或被动建）

跨节点读 (P2P prefetch):
  1. ctx.get_transfer_channel_address([(offset,size),...])  # L1 偏移 → TC 地址
  2. client = ctx.get_transfer_channel_client(peer_url)     # 首次会触发 _connect 握手
  3. task_id = client.submit_read(local_addrs, remote_addrs)
  4. while not (r := client.query_read_status(task_id)).is_finished(): ...
```

握手也是两阶段（`InitReq`/`InitResp` 交换 agent meta，`MemRegReq`/`MemRegResp` 交换 xfer descs），但和 4.3 的差别在于：本节用独立的 `InitReq`/`MemRegReq` 消息类，且 server 端在收到 `MemRegReq` 时会**被动**创建一个 client 指回发起方（双向 P2P）。

#### 4.4.3 源码精读

抽象三个角色：

[lmcache/v1/distributed/transfer_channel/abstract.py:25-49](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/transfer_channel/abstract.py#L25-L49) —— `TransferChannelServer`：`__init__(listen_url, advertise_url, l1_memory_desc)` + `close()`。只负责握手元数据交换（文件头注释 L1-L12 说明它不挂到 LMCache MQ，因为「overkill」）。

[lmcache/v1/distributed/transfer_channel/abstract.py:52-90](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/transfer_channel/abstract.py#L52-L90) —— `TransferChannelClient`：`submit_read(local_addresses, remote_addresses) -> task_id` 与 `query_read_status(task_id) -> TransferChannelReadResult`。注释明确「Only reads are supported for P2P for now」。

[lmcache/v1/distributed/transfer_channel/abstract.py:93-166](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/transfer_channel/abstract.py#L93-L166) —— `TransferChannelContext`：单例，提供 `get_transfer_channel_server/client/address`、`remove_transfer_channel_client`、`get_num_connected_clients`、`close`。其中 `get_transfer_channel_client` 的 Notes（L112-L118）指出：对 NIXL 这类双向传输，client 既可能主动建、也可能在对端连过来时被动建，对调用方透明。

线缆数据类型：

[lmcache/v1/distributed/transfer_channel/api.py:10-47](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/transfer_channel/api.py#L10-L47) —— `TransferChannelAddress(offset, size)` 是 L1 buffer 内字节偏移；`TransferChannelReadResult(finished, succeeded_mask)` 用逐对象 bool 列表表达「哪些读成了」，飞行中 `succeeded_mask` 为空。

NIXL 实现的 client：

[lmcache/v1/distributed/transfer_channel/impl/nixl_impl.py:119-156](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/transfer_channel/impl/nixl_impl.py#L119-L156) —— `submit_read`。关键：它把 `TransferChannelAddress` 经 `addresses_to_indices` 转成 NIXL 页索引，`make_prepped_xfer("READ", local_handle, local_idx, remote_handle, remote_idx)`、`transfer`，然后把 `(handle, remote_addresses)` 存进 `_tasks[task_id]` 返回 task_id。查询状态见 [L158-191](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/transfer_channel/impl/nixl_impl.py#L158-L191)：`"PROC"` 返回未完成、`"DONE"` 返回全 True 掩码、`"ERR"` 返回全 False，终态时释放 handle。

地址翻译（这一套特有的步骤）：

[lmcache/v1/distributed/transfer_channel/impl/nixl_impl.py:399-412](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/transfer_channel/impl/nixl_impl.py#L399-L412) —— `addresses_to_indices`：`(offset,size)` → 页索引列表，要求 `offset` 对齐 `self._align`，页数 `ceil(size/align)`。

两阶段握手（发起方 `_connect`）：

[lmcache/v1/distributed/transfer_channel/impl/nixl_impl.py:547-594](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/transfer_channel/impl/nixl_impl.py#L547-L594) —— 阶段 1 发 `InitReq(agent_meta)` 收 `InitResp`，`add_remote_agent`；阶段 2 发 `MemRegReq(xfer_descs, advertise_url)` 收 `MemRegResp`，`deserialize_descs` + `prep_xfer_dlist` 得 remote handle。带 60s 握手超时（`_HANDSHAKE_TIMEOUT_MS`，[L41](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/transfer_channel/impl/nixl_impl.py#L41)），避免错 URL 永久阻塞。

工厂自注册（「定义即注册」模式，对比 4.3 的 if 分支工厂）：

[lmcache/v1/distributed/transfer_channel/impl/nixl_impl.py:600-626](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/transfer_channel/impl/nixl_impl.py#L600-L626) —— 文件底部 `register_transfer_channel_factory("nixl", create_nixl_transfer_channel_context)`。这与 u4-l3 的 L2 adapter 注册、u4-l4 的 SERDE 注册是同一套路：模块 import 时自注册，重名抛错，新增实现只需加文件 + 一行 register。

Context 构造（注册整段 L1）：

[lmcache/v1/distributed/transfer_channel/impl/nixl_impl.py:309-367](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/transfer_channel/impl/nixl_impl.py#L309-L367) —— 构造时一次性 `register_memory` 整个 L1 buffer，按 `_align` 切页 `prep_xfer_dlist` 得 `local_handle`，序列化 xfer descs 备握手时交换；并 eager 建一个 server。`_load_nixl`（[L47-58](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/transfer_channel/impl/nixl_impl.py#L47-L58)）会依次尝试 `nixl._api` / `nixl_cu12._api` / `nixl_cu13._api`，兼容 CUDA 12/13 后缀包。

#### 4.4.4 代码实践：对比两套抽象的「读」

**实践目标**：把 4.3 的 `batched_read` 与本节的 `submit_read/query_read_status` 并排放，理解「同步轮询」与「异步任务 + 状态查询」两种风格。

**操作步骤**：

1. 打开 [nixl_channel.py:462-505](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/transfer_channel/nixl_channel.py#L462-L505)（4.3 的 `batched_read`）和 [nixl_impl.py:119-191](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/transfer_channel/impl/nixl_impl.py#L119-L191)（本节的 `submit_read`/`query_read_status`）。
2. 列一张对比表，列项：调用入口、是否阻塞、结果表达、地址形式、谁持有 task 状态。
3. 打开 [l2_adapters/p2p_l2_adapter.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/p2p_l2_adapter.py)，搜索 `submit_read` / `query_read_status`，看 L2 P2P 适配器如何用这套抽象把「L2 lookup 命中」转成「跨节点 L1 读」。

**需要观察的现象 / 预期结果**：

- 4.3 的 `batched_read` 内部自带 `while` 轮询，**返回时一定已完成**（同步语义）；本节 `submit_read` 立即返回 `task_id`，调用方需自行 `query_read_status` 轮询（异步语义），更适合 L2 控制器的 poll-event-fd 编排（回顾 u4-l3 的 submit→poll→query 三段式）。
- 两者底层都是 `make_prepped_xfer + transfer + check_xfer_state`，差别在「状态机暴露给谁」。

#### 4.4.5 小练习与答案

**Q1**：本节这套为什么只支持 READ，不支持 WRITE？

**答**：它的使用场景是分布式 L2 的 P2P 预取——某节点发现另一节点的 L1 缓存了某个 key，想把那份 KV 拉到本地 L1 供后续 retrieve。这是「读对端」语义，写回对端 L1 没有需求。抽象接口（`abstract.py` 注释）与实现都明确「Only reads are supported for P2P for now」。PD 的「写对端」需求由 4.3 那套 `batched_write` 满足。

**Q2**：`get_transfer_channel_client` 的注释说 client 可能被「被动创建」，这是什么意思？

**答**：NIXL 是双向传输库。当 peer A 主动 connect 到本节点 B 的 server 时，B 的 server 在 `MemRegReq` 处理里会顺手 `register_client` 建一个指回 A 的 client（[nixl_impl.py:283-291](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/transfer_channel/impl/nixl_impl.py#L283-L291)）。于是 B 后续无需再主动 connect A，就能直接读 A 的内存。这种「连进来即建立反向通道」对调用方透明。

---

## 5. 综合实践：画出 P→D 端到端链路并标注传输实现

> 这是本讲规格里指定的综合实践任务。建议在读完 4.1–4.4 后完成，把全讲知识串成一张图。

**任务**：对照 [examples/disagg_prefill_mp/](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/examples/disagg_prefill_mp) 与 [examples/disagg_prefill/1p1d/configs/](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/examples/disagg_prefill/1p1d)，画出「P worker prefill → 通过 pd_backend / transfer_channel → D worker 接收复用」的完整链路图，并在每一段标注使用的传输实现（ZMQ socket 类型 / NIXL 操作 / 传输介质）。

**建议产出格式**（你可以画在纸上或用文本框图）：

```text
┌─────────────── Prefill Worker (pd_role=sender) ───────────────┐
│ vLLM forward 算完 KV                                           │
│   └─ LMCacheEngine.store(keys, mem_objs, transfer_spec=...)    │
│       └─ StorageManager.batched_put → PDBackend.batched_submit_put_task
│           │                                                    │
│           ├─[ZMQ REQ/REP, msgpack AllocRequest]──► Receiver    │  控制面：远端分配
│           ◄─[ZMQ REP, AllocResponse(remote_indexes)]──         │
│           │                                                    │
│           └─ _nixl_queue.put("write") → _nixl_worker_loop      │
│               └─ NixlChannel.batched_write                     │
│                   └─[NIXL WRITE, UCX→NVLink/RDMA]──► D GPU显存 │  数据面：零拷贝RDMA
│                                                                │
│           └─[ZMQ PUSH, ProxyNotif(req_id)]──► Proxy/Router     │  控制面：完成通知
└────────────────────────────────────────────────────────────────┘

┌─────────────── Decode Worker (pd_role=receiver) ──────────────┐
│ _mem_alloc_loop (ZMQ REP bind pd_peer_alloc_port)              │
│   └─ _allocate_and_put: 本地 buffer 分配 mem_obj, put(self.data)│
│ get_blocking(key) → 直接返回已落位的 mem_obj（push 语义）       │
│ vLLM retrieve 复用，无需重算                                    │
└────────────────────────────────────────────────────────────────┘
```

**完成检查清单**（每条都要能在源码里指到行号）：

1. ✅ 控制面 AllocRequest/AllocResponse 走 ZMQ REQ/REP——指 [pd_backend.py:457-465](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/pd_backend.py#L457-L465) 与 [L879-882](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/pd_backend.py#L879-L882)。
2. ✅ 数据面 KV 字节走 NIXL WRITE（UCX）——指 [nixl_channel.py:419-460](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/transfer_channel/nixl_channel.py#L419-L460)，经 worker 线程 [pd_backend.py:633-637](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/pd_backend.py#L633-L637)。
3. ✅ 完成通知走 ZMQ PUSH——指 [pd_backend.py:413-420](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/pd_backend.py#L413-L420)（建 PUSH socket）与 [L646-650](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/pd_backend.py#L646-L650)（发 ProxyNotif）。
4. ✅ receiver 不主动收数据，靠 push 语义——指 [pd_backend.py:1040-1046](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/pd_backend.py#L1040-L1046)。
5. ✅ 传输介质由 UCX 自动选择（NVLink/RDMA/TCP/SHM）——指 [nixl_channel.py:648-656](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/transfer_channel/nixl_channel.py#L648-L656)（`nixl_agent_config(backends=["UCX"])`）。

**进阶思考**（可选）：如果把这套换成同机两进程，UCX 会优先走 CUDA IPC / 共享内存（同机无需 RDMA）；跨机则走 InfiniBand/RoCE 或退化为 TCP。这就是「同一份代码、不同传输介质」的收益。

---

## 6. 本讲小结

- **PD 分离把 prefill 与 decode 拆到不同实例**，LMCache 用 `PDBackend` 这个存储后端把 prefill 算出的 KV 跨节点搬到 decode，避免重算。
- **KV 字节永远零拷贝**：控制面（分配请求、完成通知、握手）走 ZMQ，数据面走 NIXL 一侧 WRITE，receiver CPU 不碰 KV 字节，只预留 buffer 偏移。
- **`PDBackend` 一类三用**：sender（推 KV + 通知 proxy）、receiver（分配 slot + 供 retrieve）、both（双向复用）；真正的 NIXL 调用被搬到专用单 worker 线程，避开 vLLM 的 CUDA context 与 RPC 超时。
- **仓库有两套传输通道抽象，务必分清**：引擎内 `v1/transfer_channel/`（`BaseTransferChannel`，PD 实际用的，支持 WRITE+READ）与分布式 L2 `v1/distributed/transfer_channel/`（`TransferChannelContext/Server/Client`，P2P 预取用，只 READ）。
- **两套都靠 NIXL 落地**，握手都是两阶段（agent meta → 内存描述符），底层走 UCX，自动在 NVLink/RDMA/TCP/共享内存里选最优介质。
- **去重与时序契约**：`already_sent_indexes` 让重复 prefill 不重传；receiver 敢断言「key 必在本地」，靠的是「先登记再回响应、proxy 收通知后才转发 decode」这一串控制面保证。

---

## 7. 下一步学习建议

- **异步 PD 后端**：本讲精读的是同步版 `PDBackend`，生产默认走 `pd_backend_async.py`。建议接着读它，重点看 asyncio 事件循环、预留式准入控制（[pd_async_reservation_design.md](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/docs/design/v1/pd_async_reservation_design.md)）如何避免多请求并发分配的死锁。
- **MP 多进程下的 PD**：把本讲的「进程内 PDBackend」与 u3-l1/u3-l2 的 MP 架构结合——当 `kv_connector=LMCacheMPConnector` 时，PD 传输会经 MP server/IPC（ZMQ DEALER/ROUTER + CUDA IPC），与本讲的直连 ZMQ+UCX 路径不同，可对照 `vllm_multi_process_adapter.py`。
- **分布式 L2 P2P**：本讲 4.4 的抽象服务于 `l2_adapters/p2p_l2_adapter.py`，建议结合 u4-l2/u4-l3，看一个节点如何通过 coordinator 发现另一节点缓存、再用 `submit_read` 把 KV 拉回本地 L1。
- **端到端跑通**：照 `examples/disagg_prefill_mp/README.md` 或 `examples/disagg_prefill/` 在双 GPU 机器上起 1p1d + proxy，用 `vllm bench serve` 度量 TTFT/吞吐，亲眼看 PD 分离的收益。

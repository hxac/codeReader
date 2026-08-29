# KV 事件流：publisher 与 ZMQ 线格式

## 1. 本讲目标

上一讲（u6-l2）我们看了路由器的"决策大脑" `RoutingHost`：它给候选 worker 打分时，需要知道**每个 worker 的显存里到底缓存了哪些 token 块**。这份情报不是天上掉下来的——它由每个引擎 worker 主动上报的一连串 **KV 事件（KV events）** 汇聚而成。

学完本讲，你应当能够：

1. 完整说出一条 KV 事件从引擎内部到路由器索引器的**四段旅程**，并解释为什么中间要绕一段 ZMQ。
2. 读懂 `KvEventBatch` / `RawKvEvent` 的**线格式（wire format）**：三帧 ZMQ 消息、MessagePack 数组、以及每个字段为什么这样设计。
3. 解释事件洪峰的三道闸门：**过滤器（normalizer）**、**合并器（batching）**、**去重器（dedup）**，以及 `Cleared` 事件为何被特殊对待。
4. 用 mocker 后端 + kv 路由跑通事件链路，借助现有日志与 Prometheus 指标统计**每秒事件数**，并理解 batching 参数背后的内存/延迟取舍。

## 2. 前置知识

本讲假设你已完成 u6-l2。需要回顾的概念：

- **块哈希（BlockHash）与序列哈希**：u4-l3 讲过，请求的 token 序列被切成固定大小的块（默认 16 token/块），每块算出一个哈希。"相同前缀 ⇒ 相同前缀块哈希"是整套 KV 路由的数学地基。KV 事件里搬运的正是这些块哈希，**而不是 KV 数据本身**——记住这一点：事件流是"目录更新"，不是"数据搬运"（数据搬运是 u7-l3 的 NIXL 干的活）。
- **事件面（event plane）**：u3-l5 讲过，Dynamo 有别于请求面的第二条消息通道，走 NATS 或 ZMQ，语义是 at-most-once 的"尽力而为广播"。本讲的最后一跳就落在事件面的 `kv-events` 主题上。
- **RoutingHost 与 indexer**：u6-l2 讲过，路由器靠 indexer 维护"worker → 块哈希集合"的映射，KV 重合度打分查的就是它。本讲讲的正是**喂饱 indexer 的那条管道**。
- **PUB/SUB 模式**：ZMQ 的 PUB 套接字单向广播，SUB 端订阅主题。PUB 不关心有没有人听，消息发了就发了——这天然契合"KV 事件是建议性情报"的语义。
- **MessagePack（msgpack）**：一种二进制 JSON，Python 侧由 `msgspec` 库编码，Rust 侧由 `rmp_serde` 解码。vLLM 引擎用它编码 KV 事件。

一个值得先建立的全局直觉：**KV 事件流是"单向、无确认、可丢失"的情报广播，但 `Cleared` 例外**。这个不对称性贯穿本讲所有源码，也是 `publisher/AGENTS.md` 里写明的架构契约。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| [lib/llm/src/kv_router/publisher/mod.rs](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/publisher/mod.rs) | `KvEventPublisher` 主结构：装配监听器、事件处理器、向发现面广播事件源 |
| [lib/llm/src/kv_router/publisher/zmq_listener.rs](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/publisher/zmq_listener.rs) | 订阅引擎的 ZMQ PUB，解码三帧消息并过滤归一化 |
| [lib/llm/src/kv_router/publisher/event_processor.rs](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/publisher/event_processor.rs) | 异步主循环：保持顺序、检测事件号空洞、驱动合并与发布 |
| [lib/llm/src/kv_router/publisher/batching.rs](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/publisher/batching.rs) | 相邻可兼容事件的合并器（洪峰第一道减震） |
| [lib/llm/src/kv_router/publisher/dedup.rs](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/publisher/dedup.rs) | 按 (dp_rank, tier, domain) 的引用计数去重 |
| [lib/llm/src/kv_router/publisher/sinks.rs](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/publisher/sinks.rs) | 出口：写本地索引器 + 按上限切批发往事件面 |
| [lib/kv-router/src/zmq_wire/types.rs](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/zmq_wire/types.rs) | 线格式类型定义：`KvEventBatch`、`RawKvEvent` |
| [lib/kv-router/src/zmq_wire/mod.rs](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/zmq_wire/mod.rs) | `decode_event_batch` 与 `ZmqEventNormalizer`（过滤策略） |
| [lib/mocker/src/services/zmq_events.rs](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/mocker/src/services/zmq_events.rs) | **生产端**：mocker 把事件编码成 vLLM 原生格式发到 ZMQ PUB |
| [lib/llm/src/kv_router/indexer/recovery/subscriber.rs](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/indexer/recovery/subscriber.rs) | **消费端**：路由器订阅 `kv-events` 主题回填索引器 |
| [lib/llm/src/kv_router/metrics.rs](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/metrics.rs) | `KvPublisherMetrics`：本讲实践要用的五个计数器 |

---

## 4. 核心概念与源码讲解

### 4.1 情报链总览：一次 KV 事件的四段旅程

#### 4.1.1 概念说明

先把整条链路一口气画出来。一条"块被缓存了"的情报，从引擎到路由器要走四段：

```text
┌────────────────────────── worker 进程（引擎侧）──────────────────────────┐
│                                                                          │
│  ① 引擎产出          mocker 引擎 / vLLM 引擎                              │
│       │                （算出块哈希，产生 Store/Remove 意图）                │
│       ▼                                                                  │
│  ② ZmqKvEventSink    绑定 tcp://0.0.0.0:PORT 的 ZMQ PUB                  │
│       │                编码为 msgpack KvEventBatch，三帧广播               │
│       ▼               （lib/mocker/src/services/zmq_events.rs）           │
│  ───────────── 同进程回环，但走真实网络线格式 ─────────────                 │
│       ▼                                                                  │
│  ③ zmq_listener      SUB 订阅 tcp://127.0.0.1:PORT                        │
│       │                解码 → 过滤 → 归一化为 PlacementEvent               │
│       ▼               （lib/llm/.../publisher/zmq_listener.rs）           │
│  ④ event_processor   合并(batching) → 去重(dedup) → 切批(sinks)            │
│       │                写本地索引器 + 发往事件面主题 "kv-events"            │
└───────│──────────────────────────────────────────────────────────────────┘
        ▼  事件面（ZMQ 或 NATS，u3-l5 讲过的 EventPublisher）
   ┌────────────────────────── 路由器/frontend 进程 ─────────────────────────┐
   │  ⑤ subscriber.rs   EventSubscriber 订阅 kv-events                        │
   │       ▼            .typed::<Vec<RouterEvent>>()                          │
   │  ⑥ indexer         更新 radix tree（u6-l4 的主角）                        │
   │       ▼                                                                  │
   │  ⑦ RoutingHost     KV 重合度打分 → 选 worker（u6-l2 的主角）              │
   └─────────────────────────────────────────────────────────────────────────┘
```

**为什么要绕一段 ZMQ？** 看似多余——同进程自己发给自己——实则有深刻的工程理由：vLLM/SGLang 是独立的 Python 引擎，Dynamo 的 worker 接入层是 Rust。ZMQ PUB 是两个语言生态都成熟、零依赖耦合的接缝：引擎侧只管往 socket 里丢事件（哪怕没人订阅也不阻塞推理主循环），Rust 侧负责一切重活（解码、过滤、合并、转发）。mocker 忠实地复刻了这条生产路径，所以你在 mocker 上做的实验对 vLLM 部署同样成立。

第二个理由是**隔离故障域**：ZMQ 解码失败、洪峰、下游卡顿，全部被挡在 listener 这一层，绝不会反压到引擎的推理循环。`publisher/AGENTS.md` 把这条写成了硬性契约："ZMQ ingestion must not wait for downstream processing or publication."

#### 4.1.2 核心流程

以 mocker 为例，装配点在 `lib/llm/src/mocker.rs` 的 `start_engines`：当启动参数带 `zmq_kv_events_port` 时，同时创建"生产端 sink"和"消费端 publisher"，让事件走完整的 ZMX 线格式绕行；不带该参数时，引擎直接经进程内 channel 进入第 ④ 段（`publish_batch`），跳过 ②③。这个开关正是本讲综合实践要做 A/B 实验的旋钮。

```text
if args.zmq_kv_events_port.is_some():
    zmq_port = base_port + dp_rank
    sink    = ZmqKvEventSink::new(zmq_port, replay_port, dp_rank, block_size)   # ②
    source  = KvEventSourceConfig::Zmq { endpoint: tcp://127.0.0.1:{zmq_port}, topic: "" }
    relay   = KvEventPublisher::new_with_local_indexer(endpoint, ..., source)   # ③④
else:
    relay   = KvEventPublisher::new_with_local_indexer(endpoint, ..., None)     # 只有 ④
```

#### 4.1.3 源码精读

mocker 装配双端的关键代码——注意"生产者绑 PUB、中继订 SUB"这对孪生调用：

- [lib/llm/src/mocker.rs:635-659](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/mocker.rs#L635-L659) —— 当配置了 `zmq_kv_events_port` 时，先 `ZmqKvEventSink::new(zmq_port, ...)` 绑定生产端，再用 `KvEventSourceConfig::Zmq { endpoint: tcp://127.0.0.1:{zmq_port} }` 构造中继端。注意端口按 `dp_rank` 偏移：每个 DP rank 一个独立 PUB 口。
- [lib/llm/src/mocker.rs:660-666](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/mocker.rs#L660-L666) —— 返回的 `KvEventPublishers::new(None, Some(sink))` 第一个参数是 `None`：**进程内直发通道被显式关掉**，保证事件必须走 ZMQ 绕行，不会双份。

而 `KvEventPublisher` 本体是个"装配工"，三件事在构造函数里一次完成：

- [lib/llm/src/kv_router/publisher/mod.rs:190-205](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/publisher/mod.rs#L190-L205) —— 结构体只有五个字段：`kv_block_size`、可选 `source`（ZMQ 监听器句柄）、`cancellation_token`、`worker_id`、无界 channel 发送端 `tx`，以及跨任务共享的事件号计数器 `next_event_id`。
- [lib/llm/src/kv_router/publisher/mod.rs:312-341](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/publisher/mod.rs#L312-L341) —— `mpsc::unbounded_channel::<Vec<PlacementEvent>>()` 是 listener 与 processor 之间的传送带；若有 ZMQ 源则先 `KvEventSource::start` 把 listener 拉起来（跑在 `runtime().secondary()` 线程池，u3-l1 讲过 secondary 池的职责）。
- [lib/llm/src/kv_router/publisher/mod.rs:361-455](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/publisher/mod.rs#L361-L455) —— 另一个 spawned 任务先为事件面创建 `EventPublisher`，向发现面注册 `EventSource` 广播（下文 4.4 详解），最后 `start_event_processor(...)` 进入主循环。

#### 4.1.4 代码实践

**实践：静态确认"事件源开关"的两条路径**

1. **实践目标**：在动手跑集群前，先在源码层面确认"带 `--zmq-kv-events-ports` 与不带"分别走哪条路径，避免实验时张冠李戴。
2. **操作步骤**：
   - 阅读 [lib/llm/src/mocker.rs:630-700](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/mocker.rs#L630-L700)，找到 `match endpoint` 的三个分支。
   - 阅读 [components/src/dynamo/mocker/args.py:516-532](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/mocker/args.py#L516-L532)，确认 CLI 标志名与"每个 worker 一个端口、DP rank 再偏移"的约定。
3. **需要观察的现象**：`Some(endpoint) if args.zmq_kv_events_port.is_some()` 分支同时构造了 sink 与 relay；`Some(endpoint)` 分支只构造 relay 且第三参传 `None`。
4. **预期结果**：能画出两条路径的事件流图——路径 A（默认）`引擎 → channel → event_processor`；路径 B（带端口）`引擎 → ZmqKvEventSink → ZMQ PUB → zmq_listener → channel → event_processor`。
5. 本实践为纯源码阅读，无需运行验证。

#### 4.1.5 小练习与答案

**练习 1**：mocker 在同一进程里既绑 PUB 又订 SUB，为什么不等价于"直接函数调用"，而非要走一遍网络线格式？

**答案**：因为要复刻 vLLM 的真实生产路径。vLLM 引擎（Python）与 Dynamo worker（Rust）是两个语言、常常是两个进程，ZMQ + msgpack 是它们之间唯一的公共语言。mocker 走同一条路径，意味着线格式编解码、过滤、洪峰处理这些代码在测试环境和生产环境是被同一段代码覆盖的——这也让 mocker 上的路由实验结论可以外推到 vLLM 部署。

**练习 2**：如果 ZMQ listener 崩了，引擎的推理主循环会怎样？

**答案**：完全不受影响。PUB 套接字是单向 fire-and-forget，引擎只管往 socket 写。listener 崩溃的后果由 [lib/llm/src/kv_router/publisher/mod.rs:87-117](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/publisher/mod.rs#L87-L117) 的 `supervise_zmq_listener` 兜底：监听任务异常退出时记录 error 日志并 `cancellation_token.cancel()`，把整个 KV 事件源安全停掉（路由器随后会通过发现面感知源消失），但绝不反向波及引擎。

---

### 4.2 zmq_listener：解码 vLLM 原生线格式

#### 4.2.1 概念说明

这一段解决的问题是：**怎么把引擎吐出来的一串字节，变成 Rust 侧可信的、带身份的结构化事件**。

线格式由两个文件定义：

- [lib/kv-router/src/zmq_wire/types.rs](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/zmq_wire/types.rs)：类型定义（本讲的"词汇表"）
- [lib/kv-router/src/zmq_wire/mod.rs](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/zmq_wire/mod.rs)：解码入口与过滤策略

这套类型在模块文档里写明："mirror the Python `msgspec`-defined structures emitted by vLLM engines over ZMQ PUB sockets"——即**线格式归 vLLM 定义，Dynamo 是兼容的读者**。这是为什么它放在独立的 `dynamo-kv-router` crate 里、且不依赖 dynamo runtime：任何需要解码这份格式的代码都能复用。

#### 4.2.2 核心流程

**外层：三帧 ZMQ 消息**

```text
帧 0：主题（topic，订阅过滤用，mocker 里为空字节串）
帧 1：序号 sequence —— 8 字节大端 u64，生产端每发一批 +1
帧 2：负载 payload —— msgpack 编码的 KvEventBatch
```

**内层：`KvEventBatch` 是一个三元组数组**（不是对象！）：

```text
[ ts,                // f64 Unix 时间戳
  [RawKvEvent, ...], // 事件列表
  dp_rank ]          // Option<i32>，数据并行秩
```

它实现了自定义 `Deserialize`，从 `(f64, Vec<RawKvEvent>, Option<i32>)` 元组反序列化——见 [types.rs:19-32](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/zmq_wire/types.rs#L19-L32)。为什么用数组而不是字段名？msgspec 的 `array_like=True` 编码比 map 省字节，在每秒上万事件的热路径上这点带宽是真实成本。

**事件层：`RawKvEvent` 四个变体**（[types.rs:91-150](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/zmq_wire/types.rs#L91-L150)），以 `#[serde(tag = "type")]` 内部标签区分：

| 变体 | 语义 | 关键字段 |
|------|------|---------|
| `BlockStored` | 一批块被缓存 | `block_hashes`、`parent_block_hash`（链式前缀指针）、`token_ids`、`block_size`、`medium`（存储层）、`lora_name`、`cache_namespace`（线字段名 `cache_salt`）、`block_mm_infos`（多模态）、`group_idx`、`locality`、`ownership` |
| `BlockRemoved` | 一批块被驱逐 | `block_hashes`、`medium`、`group_idx` |
| `AllBlocksCleared` | 该 (worker, dp_rank) 的全部缓存清空 | 仅 `ownership` |
| `Ignored` | 占位符，无信息 | 无 |

三个值得注意的细节设计：

1. **`BlockHashValue::Signed(u64) / Unsigned(u64)`**（[types.rs:34-48](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/zmq_wire/types.rs#L34-L48)）：不同引擎产出的块哈希可能是带符号或不带符号 64 位数，解码时统一归一到 `u64`（负数走 `cast_unsigned` 回环）。这是一条典型的"宽容读者"兼容缝。
2. **`Locality::Unknown` 用 `#[serde(other)]` 捕获未来新增值**（[types.rs:61-68](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/zmq_wire/types.rs#L61-L68)）：反序列化不因陌生枚举值而失败，把"认不出"推迟到下游按策略拒绝。N-2 滚动升级兼容（`lib/llm/AGENTS.md` 的契约）在这里落地。
3. **`ownership` 缺省即 `Framework`**（[types.rs:70-89](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/zmq_wire/types.rs#L70-L89)）：`kvcr` 表示 KVCR（KVCacheResidency）所有权的富化事件，当前 listener 对它**fail closed**（直接过滤），但解码层保留区分能力。

**位置层：解码之后还要"过安检"**

`zmq_listener` 对每个 `RawKvEvent` 依次做：过滤（`preprocess_with_reason`）→ 分配事件号 → 转换（`normalize_preprocessed`）成 `PlacementEvent`。过滤有 11 种理由（[zmq_wire/mod.rs:69-98](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/zmq_wire/mod.rs#L69-L98)），常见的几种：

- `ignored_event`：占位符直接丢。
- `non_local_locality`：非本机块（`Remote`/`Unknown`），当前没有共享索引消费者，按策略丢弃。
- `unknown_medium`：认不出的存储介质（如新版 vLLM 的 FS/OBJ 层）。
- `non_main_attention_group` / `unlearned_group_idx`：非主注意力缓存组（如 Mamba/SSM 状态），不参与 KV 重合度。

**过滤的一个精妙细节**：过滤发生在分配事件号**之前**。如果先发号再丢弃，事件号序列会出现空洞，而下游 `event_processor` 会把空洞误判成"引擎丢了事件"（见 4.3 的 gap 检测）。[zmq_wire/mod.rs:162-170](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/zmq_wire/mod.rs#L162-L170) 的注释明确写了这个陷阱："Otherwise the listener would accept the event, burn a next_event_id, and only drop it in conversion, leaving an id gap the event processor mistakes for an engine drop."

#### 4.2.3 源码精读

- [lib/llm/src/kv_router/publisher/zmq_listener.rs:26-45](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/publisher/zmq_listener.rs#L26-L45) —— `decode_zmq_kv_batch`：先断言三帧，`frames.pop()` 依次取出 payload 与 sequence，`u64::from_be_bytes` 还原序号，最后 `decode_event_batch(&payload)` 做 msgpack 反序列化。帧数不对或序号非 8 字节都直接报错丢弃整批。
- [lib/kv-router/src/zmq_wire/mod.rs:41-43](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/zmq_wire/mod.rs#L41-L43) —— 解码入口只有一行 `rmps::from_slice(payload)`，复杂的容错全部交给 `deserialize.rs` 的自定义实现（同时支持"按字段名的 map 事件"和"按位置的三元组事件"两种形态，详见 [zmq_wire/README.md](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/zmq_wire/README.md)）。
- [lib/llm/src/kv_router/publisher/zmq_listener.rs:81-98](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/publisher/zmq_listener.rs#L81-L98) —— 主循环用 `tokio::select! { biased; ... }`：**先查取消令牌再收消息**，保证关停不被消息洪峰饿死；recv 出错或流结束都跳出循环。
- [lib/llm/src/kv_router/publisher/zmq_listener.rs:118-146](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/publisher/zmq_listener.rs#L118-L146) —— 对批内每个事件：记 `received` 指标 → `preprocess_with_reason` 过滤（失败记 `filtered` 指标并 `continue`）→ `next_event_id.fetch_add(1)` 原子分号 → `normalize_preprocessed` 转换（返回 None 记 `conversion_issue`）→ 通过者记 `accepted` 指标。
- [lib/llm/src/kv_router/publisher/zmq_listener.rs:147-161](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/publisher/zmq_listener.rs#L147-L161) —— `Stored` 事件若 `blocks` 为空记一条 `suspicious` 指标（可疑但放行）；整批转换完通过 `tx.send(events)` 一次性发给 event_processor，**一个原生批 = 一个 `Vec<PlacementEvent>`**，这个"原生批边界"是 4.3 合并逻辑的重要概念。
- [lib/llm/src/kv_router/publisher/zmq_listener.rs:166-170](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/publisher/zmq_listener.rs#L166-L170) —— 退出时 debug 日志带上 `messages_processed` 总数：这是实践中统计事件总量的现成抓手。

生产端（mocker）对照，验证三帧格式与字段如何被造出来：

- [lib/mocker/src/services/zmq_events.rs:207-216](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/mocker/src/services/zmq_events.rs#L207-L216) —— 发送三帧 `vec![Vec::new()（空主题）, seq_num.to_be_bytes()（大端序号）, payload]`，每发一批 `seq_num += 1`。
- [lib/mocker/src/services/zmq_events.rs:245-271](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/mocker/src/services/zmq_events.rs#L245-L271) —— `encode_event_batch` 构造 `(timestamp, events, Some(dp_rank))` 三元组再 `rmp_serde::to_vec_named`，与消费端的自定义元组 `Deserialize` 严格对偶。
- [lib/mocker/src/services/zmq_events.rs:273-316](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/mocker/src/services/zmq_events.rs#L273-L316) —— `convert_to_zmq_events` 把内部 `KvCacheEvent` 翻译成线格式 `ZmqRawKvEvent`：`token_ids` 长度必须等于 `block_hashes.len() * block_size`（有断言），`Cleared` 被翻译成**空数组**（mocker 不通过 ZMQ 发 Cleared，原因见 4.3）。
- [lib/mocker/src/services/zmq_events.rs:438-525](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/mocker/src/services/zmq_events.rs#L438-L525) —— 测试 `encoded_batch_decodes_through_router_zmq_wire`：**生产端编码 → 消费端解码的闭环验证**，断言 dp_rank、块哈希、token_ids、medium 全部还原正确。这条测试是两个 crate 之间线格式契约的守护者。

#### 4.2.4 代码实践

**实践：跑通线格式的编解码闭环测试，并解剖一帧真实消息**

1. **实践目标**：确认"生产端编码的字节，消费端能无损还原"，并亲手解出一帧三消息的结构。
2. **操作步骤**：
   - 运行闭环测试（只依赖 Rust，无需 GPU/网络）：
     ```bash
     cargo test -p dynamo-mocker encoded_batch_decodes_through_router_zmq_wire
     ```
   - 再跑消费端解码的全量测试，覆盖 map/positional 两种事件形态：
     ```bash
     cargo test -p dynamo-kv-router zmq_wire
     ```
   - 阅读测试 [lib/mocker/src/services/zmq_events.rs:438-525](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/mocker/src/services/zmq_events.rs#L438-L525)，对照本节的三帧图，在纸上标出：`Vec::new()` 是哪帧、`to_be_bytes()` 是哪帧、`payload` 是哪帧。
3. **需要观察的现象**：两条测试命令全部通过；测试里能看到 `Device` 层省略 `medium`、`HostPinned` 层填 `"CPU_PINNED"` 的差异。
4. **预期结果**：通过 + 能独立复述"一帧 ZMQ 消息 = 主题 + 8 字节大端序号 + msgpack 三元组"。
5. 如本机未配置 Rust 工具链，标注「待本地验证」；测试名与文件路径均已核对，命令本身来自仓库测试约定（`cargo test -p <crate>`）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `KvEventBatch` 反序列化自元组而不是结构体？这带来什么兼容负担？

**答案**：因为生产端 vLLM 用 Python `msgspec` 的 `array_like=True` 编码，数组比 map 省带宽。兼容负担是**位置敏感**：新增字段只能追加在尾部，且后段字段依赖前段占位。[zmq_wire/README.md](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/zmq_wire/README.md) 明确记录了这一约定——例如想携带 `group_idx` 就必须先补齐 5–8 号位的 `None` 占位。

**练习 2**：`locality` 字段用 `#[serde(other)]` 把未知值收进 `Unknown`，随后又在过滤器里被丢弃。为什么不干脆让反序列化直接失败？

**答案**：反序列化失败会**连坐整批**——一个认不出的 locality 会让同批所有正常事件一起丢失，且日志里只有一条笼统的 decode 错误。先解码成功、再按策略过滤，可以把"这一批里有 N 个非本地事件被丢弃"精确记进 `kv_publisher_zmq_filtered_events_total` 指标，运维可观测；同时满足 N-2 滚动升级期"新版本 worker 的字段不击穿旧版本 frontend"的契约。

**练习 3**：mocker 为什么不通过 ZMQ 发送 `AllBlocksCleared`？

**答案**：`convert_to_zmq_events` 对 `Cleared` 返回空 vec（[zmq_events.rs:314](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/mocker/src/services/zmq_events.rs#L314)）。因为 `Cleared` 在消费端是"必须跨所有物理层完成的排序屏障"（见 4.3.2），语义远强于普通 fire-and-forget 事件；mocker 的引擎直发通道（不走 ZMQ 的那条）仍然可以传递 Cleared，而 ZMQ 的 at-most-once 语义无法承载它的强保证。

---

### 4.3 event_processor 与 batching：驯服事件洪峰

#### 4.3.1 概念说明

一个真实推理集群里 KV 事件有多凶？每生成 16 个 token 就可能产生一个 `BlockStored`，每个被驱逐的块产生 `BlockRemoved`；几十个 worker × 每秒几十次前向，事件量轻松达到**每秒数万**。而路由器的 indexer 每次只需回答"这个请求的前缀在谁那里"——**中间状态越少越快**。

于是 publisher 在引擎与事件面之间放了三道减震闸：

1. **合并（batching.rs）**：把相邻、可兼容的 Store/Remove 合并成一个大事件。10 个连续 Store 合成 1 个，下游 indexer 就只处理 1 次树更新。
2. **去重（dedup.rs）**：vLLM 会对同一块哈希发多次 Store/Remove（引用计数语义），publisher 用本地 refcount 抵消掉冗余的 Remove。
3. **切批（sinks.rs）**：发往事件面前按"事件数 ≤128、块数 ≤8192"切块，避免超过 NATS 1 MiB 的 `max_payload`。

同时有一条**不可妥协的红线**：出站事件号 `next_publish_id` 严格单调递增、永不复用（[event_processor.rs:222-225](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/publisher/event_processor.rs#L222-L225) 有 `checked_add` + panic 守护）。消费端依赖这个性质检测丢失。

#### 4.3.2 核心流程

主循环是一个三臂 `tokio::select!`：

```text
loop {
  select! {
    ① 取消信号   → flush 残留 → 发布 → 退出
    ② rx.recv() 收到一个原生批 Vec<PlacementEvent>
       │  先整批处理完，不回到 select!（关键！）
       │  for 每个事件:
       │     a. 检查原始 event_id 是否跳号 → 记 engines_dropped_events 指标 + warn
       │     b. Stored/Removed → batching_state.push(...)（可能就地合并）
       │     c. Cleared → 先 flush 挂起的事件 → 清 dedup 引用计数
       │              → emit Cleared → 若本地索引器 reset 失败
       │                → 撤销整个事件源（cancellation_token.cancel()）
       │  批尾：若 timeout=None 或已到期 → flush 兼容尾部
       │  publish_output(...) 发往事件面
    ③ 超时唤醒（仅 timeout_ms=Some 时挂起条件成立）
       → flush 挂起事件 → 发布
  }
}
```

**合并的兼容性判据**（`PlacementEventCoalescer::push`，[batching.rs:42-71](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/publisher/batching.rs#L42-L71)）——两个事件可合并，当且仅当：

- 归属键 `Placement` 相同（同一个 worker 身份 + 同一存储层 + 同一 residency domain）；
- `dp_rank` 相同（不同 DP rank 的同哈希块是独立缓存，绝不能混）；
- 数据形态兼容：
  - `Stored + Stored`：**前一个的最后一个块哈希必须等于后一个的 `parent_hash`**（[batching.rs:90-98](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/publisher/batching.rs#L90-L98)）。这保持了链式前缀结构——合并后的大事件仍然是一条合法的块链，消费端 radix tree 能照常按前缀索引。
  - `Removed + Removed`：无条件可合并（块哈希集合求并）。
- 触发 flush 的六个边界：事件种类变了 / DP rank 变了 / 存储层变了 / Store 父链断了 / 块数达到上限 `max_batch_blocks`（默认 128）/ 超时到期。

**`batching_timeout_ms` 的三档语义**（这是本讲实践要调的参数）：

- `None`（**默认**，`DEFAULT_BATCHING_TIMEOUT_MS`）：在每个**原生批边界**处 flush 兼容尾部。批内仍合并，批间不跨。
- `Some(0)`：等效 `None`。
- `Some(ms)`：兼容尾部可以**跨原生批**继续挂起，直到截止时间或另一 flush 条件。合并率更高、事件更少，但情报更延迟——路由器在窗口期内看到的是略旧的世界。上限 15000 ms，超出会被钳制并告警（[mod.rs:63-64, 299-310](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/publisher/mod.rs#L63-L64)）。

**`Cleared` 为什么是例外**：普通事件是"建议性"的（丢一两个只会让打分略偏），但 `Cleared` 宣告"该 (worker_id, dp_rank) 的全部缓存都没了"。如果它在本地索引器上应用失败而照常发布，路由器会拿着一份"本地没删、远端已清"的分裂视图继续把请求路由过去。所以 [event_processor.rs:107-134](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/publisher/event_processor.rs#L107-L134) 在 `applied == false` 时不仅丢弃该事件，还**取消整个事件源**——源码注释写得很直白："Once the local reset barrier fails, withdrawing the complete source is the only safe way to avoid advertising a cursor whose local snapshot has diverged."

**去重的引用计数规则**（[dedup.rs:66-102](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/publisher/dedup.rs#L66-L102)）：

```text
Store:  refcount[(dp_rank, tier, domain)][block_hash] += 1   （事件照常放行）
Remove: refcount -= 1
          └─ 归零   → 放行（真的没人引用了）
          └─ 仍 >0  → 过滤掉（还有别的副本在用）
          └─ 未跟踪 → 防御性放行（宁可多删，不可漏删——索引器删不存在的键是幂等的）
```

#### 4.3.3 源码精读

- [lib/llm/src/kv_router/publisher/event_processor.rs:20-32](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/publisher/event_processor.rs#L20-L32) —— 循环前的三件状态：`BatchingState`（含合并器与出站事件号）、`EventDedupFilter`、`last_raw_input_id`（入站事件号游标）。
- [lib/llm/src/kv_router/publisher/event_processor.rs:52-54](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/publisher/event_processor.rs#L52-L54) —— `'event_batch:` 标签循环上的注释点明关键约束："Process the complete source list before returning to `select!`"——一个原生批必须原子处理完，否则超时/取消可能从批中间劈开事件序列。
- [lib/llm/src/kv_router/publisher/event_processor.rs:55-77](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/publisher/event_processor.rs#L55-L77) —— 入站空洞检测：`raw_event_id > last_id + 1` 时计算 `gap`，warn 日志 + `increment_engines_dropped_events(gap)`。注意这个指标**只反映进入本处理器之前的丢失**（即引擎或 ZMQ 段丢的），合并与去重不会触发它——[publisher/README.md](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/publisher/README.md) 特意澄清过这个常见误读。
- [lib/llm/src/kv_router/publisher/event_processor.rs:87-100](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/publisher/event_processor.rs#L87-L100) —— `Removed`/`Stored` 进合并器；`Cleared` 走独立屏障分支：先 `flush` 挂起事件，再 `dedup.clear_rank_domain(...)` 清空该 rank 的引用计数。
- [lib/llm/src/kv_router/publisher/event_processor.rs:143-159](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/publisher/event_processor.rs#L143-L159) —— 批尾 flush 判定 + 超时臂 `if timeout_ms.is_some() && batching_state.has_pending()`：没挂起事件时这个分支根本不参与 select，避免空转。
- [lib/llm/src/kv_router/publisher/batching.rs:20-31](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/publisher/batching.rs#L20-L31) —— 合并器的设计自述："deliberately owns no event IDs, timers, indexers, publishers, or error policy"——它是纯函数式累加器，两条 publisher 生命周期（legacy 与 state-agent）共用它而不共享提交语义。
- [lib/llm/src/kv_router/publisher/batching.rs:42-71](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/publisher/batching.rs#L42-L71) —— `push` 最多返回**两个**就绪输出：`Cleared` 到来时先冲掉挂起值再自己作为屏障通过（`[self.flush(), Some(event)]`）；或"不兼容事件紧跟挂起事件"时先冲旧的、新的自己成批。
- [lib/llm/src/kv_router/publisher/batching.rs:59-67](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/publisher/batching.rs#L59-L67) —— 块数上限在**每个完整源事件之后**检查（`if event_block_count(pending) >= self.max_batch_blocks`），且单事件超上限时原样放行不拆分——上限约束的是合并，不是拆源。
- [lib/llm/src/kv_router/publisher/batching.rs:182-226](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/publisher/batching.rs#L182-L226) —— `emit_ready` 是合并/去重/出号的汇合点：先按 dedup 规则过滤数据，再把 `event_id` 覆写为本地的 `next_publish_id`（**入站号与出站号是两套独立编号**），最后交给 `emit` 出口。
- [lib/llm/src/kv_router/publisher/mod.rs:63-65](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/publisher/mod.rs#L63-L65) —— 三个默认值常量：`MAX_BATCHING_TIMEOUT_MS = 15_000`、`DEFAULT_BATCHING_TIMEOUT_MS = None`、`DEFAULT_MAX_BATCH_BLOCKS = 128`。
- [lib/bindings/python/rust/llm/kv.rs:1156-1229](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/bindings/python/rust/llm/kv.rs#L1156-L1229) —— Python 侧构造入口：`batching_timeout_ms` 是 `#[pyo3(signature)]` 中的具名参数，docstring 给出了使用建议（"Use 50 to allow compatible tails to span lists for up to 50 ms"）。**这是从 Python 调节合并窗口的唯一旋钮**。

观测指标（五个计数器，全部注册在组件级 `MetricsHierarchy`，自动带 `dynamo_component_` 前缀与 `dynamo_namespace`/`dynamo_component`/`worker_id` 标签）：

- [lib/llm/src/kv_router/metrics.rs:91-102](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/metrics.rs#L91-L102) —— 结构定义；指标名常量在 [lib/runtime/src/metrics/prometheus_names.rs:786-800](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/runtime/src/metrics/prometheus_names.rs#L786-L800)。

| 指标 | 标签 | 含义 |
|------|------|------|
| `dynamo_component_kv_publisher_zmq_events_total` | `stage`（received/accepted）、`event_type`（stored/removed/cleared/ignored） | ZMQ 中继两阶段计数，差值即被过滤量 |
| `dynamo_component_kv_publisher_zmq_filtered_events_total` | `event_type`、`reason`（11 种） | 解码后被过滤器丢弃的事件 |
| `dynamo_component_kv_publisher_zmq_conversion_issues_total` | `event_type`、`reason` | 转换返回 None 的事件 |
| `dynamo_component_kv_publisher_zmq_suspicious_events_total` | `event_type`、`reason` | 可疑但放行（如空 blocks 的 Store） |
| `dynamo_component_kv_publisher_engines_dropped_events_total` | 无 | 入站事件号空洞推算的引擎侧丢失 |

#### 4.3.4 代码实践

**实践：用现成单测观察 timeout 对合并行为的影响**

1. **实践目标**：不启动集群，仅通过单元测试验证"`None` 按原生批 flush、`Some(ms)` 跨批合并尾部"这两条语义，并读出合并率。
2. **操作步骤**：
   ```bash
   # 事件处理器主循环的行为测试（合著于 publisher/tests.rs 的 event_processor_tests 模块）
   cargo test -p dynamo-llm test_no_timeout_flushes_each_native_list_independently
   cargo test -p dynamo-llm test_timeout_merges_compatible_tails_across_native_lists
   # 参数化批量测试：3/5/10/20 个事件在有无 timeout 下的批次形态
   cargo test -p dynamo-llm test_run_event_processor_loop_batches_removed_events
   # 空闲后首事件立即冲刷、随后恢复合并
   cargo test -p dynamo-llm test_first_event_after_idle_flushes_immediately_then_batches
   ```
   然后打开 [lib/llm/src/kv_router/publisher/tests.rs:2444](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/publisher/tests.rs#L2444) 与 [tests.rs:2471](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/publisher/tests.rs#L2471) 两个测试的断言体，数一数各自期望的输出批数。
3. **需要观察的现象**：四条命令全部通过；`no_timeout` 测试断言每个原生批独立成一批输出，`timeout` 测试断言跨批的兼容尾部被合并进同一批。
4. **预期结果**：合并率 = 输入事件数 ÷ 输出批数。timeout 版本的输出批数显著少于 no_timeout 版本。把两个数字记下来，作为第 5 节综合实践里"调参收益"的基准预期。
5. 本实践只需 Rust 工具链；若未配置则标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：合并 `Stored` 事件时为什么必须检查"前一个事件的最后一个块哈希 == 后一个的 `parent_hash`"，而 `Removed` 事件不需要任何检查？

**答案**：`Stored` 事件携带链式前缀结构（`parent_hash` 指向上一个块），合并后的数据要能被消费端 radix tree 当作一条连续前缀链索引——乱序拼接会造出物理上不存在的"前缀路径"，污染重合度查询。`Removed` 只是无序的块哈希集合求并，顺序无关。

**练习 2**：假设把 `batching_timeout_ms` 从默认 `None` 调成 `5000`（5 秒），分别会对：(a) 事件面上的消息条数、(b) 路由器索引器的新鲜度、(c) publisher 进程内存，产生什么影响？

**答案**：(a) 大幅减少——兼容的 Store/Remove 尾部能跨原生批持续累积；(b) 变差——路由器最长看到 5 秒前的缓存视图，刚缓存的块可能还没被索引，重合度打分偏低、KV 命中率下降；(c) 上升——挂起事件（含 token_ids、块哈希、多模态元数据）要在内存里存活至多 5 秒。三者的权衡正是"消息条数 ↔ 情报延迟 ↔ 内存占用"的三角，且上限被 `MAX_BATCHING_TIMEOUT_MS = 15_000` 钳死。

**练习 3**：`emit_ready` 里把事件的 `event_id` 覆写为本地 `next_publish_id`，入站号与出站号为什么必须是两套独立计数？

**答案**：入站号由 ZMQ listener 分配，用于**本处理器检测上游丢失**（`raw_event_id` 跳号 = 引擎/ZMQ 段丢事件）；出站号在合并后重新编（10 个事件并成 1 个，出站只有 1 个号），用于**消费端检测本段丢失**，且契约要求严格单调、永不复用。若共用一套编号，合并会天然造成出站跳号，消费端会误报丢失。

---

### 4.4 sinks 与事件面：RouterEvent 出海与路由器的回程

#### 4.4.1 概念说明

事件经过合并去重后成为最终的 `RouterEvent`——这是 Dynamo 内部的**规范事件格式**，与 vLLM 线格式 `RawKvEvent` 是两个层次：后者是"引擎方言"，前者是"普通话"。

```text
RouterEvent {
    worker_id: u64,                 // 谁发的
    storage_tier: StorageTier,      // 缓存在哪一层（Device/HostPinned/Disk/External）
    residency_domain: ...,          // 归属域（Worker 或 CacheOwner）
    event: KvCacheEvent {           // 什么事
        event_id: u64,              //   出站单调号
        dp_rank: u32,               //   哪个 DP rank
        data: Stored | Removed | Cleared,
    },
    state_source: Option<...>,      // 稳态源标识（CacheOwner 事件必带，放最后保位置兼容）
}
```

出口做两件事（`sinks.rs` 的 `emit`）：

1. **写本地索引器**（若启用 `enable_local_indexer`）：worker 进程内也维护一份 radix tree，供路由器在**重连/补课**时直接来查（这就是 4.1 图里省略的"恢复旁路"）。
2. **发事件面**：按上限切批后交给 `EventPublisher`，主题固定为 `kv-events`（[lib/kv-router/src/protocols.rs:23](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/protocols.rs#L23) 的 `pub const KV_EVENT_SUBJECT: &str = "kv-events";`）。

还有一件容易被忽略的事：**publisher 要在发现面上"挂牌"**。路由器怎么知道该订阅谁？答案不是硬编码，而是 publisher 注册一条 `DiscoverySpec::EventSource`，把"我在这台 worker 的这个 endpoint 上、以 publisher_id X 的身份、往 kv-events 主题发事件、恢复查询端点在 Y"写进 etcd/file。路由器的 `kv_source_watch` 监听这类记录，动态决定订阅谁。

#### 4.4.2 核心流程

```text
emit(local_indexer, worker_id, tier, domain, event)
  ├─ RouterEvent::with_residency_domain(...)      # 组装规范事件
  ├─ admit_local_event(local_indexer, &evt)       # ① 本地索引器应用（Cleared 在此跨层完成）
  │     └─ 失败 → warn（普通事件 fail-open）
  └─ output.push(evt)                             # ② 进入待发布列表

publish_output(publisher, worker_id, &output)
  └─ EventPlanePublisher::publish_events
       └─ event_plane_event_batches(events, 128, 8192)   # 切批
            └─ self.0.publish(&batch)                    # 发往 kv-events 主题
                 └─ 失败 → 记录并继续（best-effort，无重试）
```

消费端（路由器/frontend 进程）的订阅循环：`run_subscription_supervisor` 按发现面给出的 endpoint 建立 `EventSubscriber::for_endpoint_id_with_transport(drt, kv_state_endpoint, KV_EVENT_SUBJECT, transport_kind)`，再 `.typed::<Vec<RouterEvent>>()` 反序列化回规范事件，喂给 indexer。订阅失败按 100ms 起步、上限 5 秒的指数退避重试。

#### 4.4.3 源码精读

- [lib/llm/src/kv_router/publisher/sinks.rs:26-27](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/publisher/sinks.rs#L26-L27) —— 两个批次上限常量：128 事件 / 8192 块。注释解释了来源：NATS Core 默认 `max_payload` 是 1 MiB，生产形态的批次（含稀疏单块事件、多模态对象）在线宽回归测试下都低于该值；单个超限事件仍完整发出（宁可超限也不拆语义）。
- [lib/llm/src/kv_router/publisher/sinks.rs:107-142](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/publisher/sinks.rs#L107-L142) —— `event_plane_event_batches` 的切批算法：贪心追加，直到任一上限将破则在**事件边界**切开，保证永不拆单个事件。
- [lib/llm/src/kv_router/publisher/sinks.rs:158-177](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/publisher/sinks.rs#L158-L177) —— `emit`：先 `admit_local_event`（内部走 `LocalKvIndexer::apply_event_with_buffer`），失败只 warn 不阻断发布——普通事件本地失败是 fail-open；返回的 `applied` 布尔值只有 `Cleared` 关心（4.3 讲的屏障语义）。
- [lib/llm/src/kv_router/publisher/sinks.rs:79-103](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/publisher/sinks.rs#L79-L103) —— `EventPlanePublisher` 的批发布：逐批 `publish`，失败累计进 `PublishFailures`，最后聚合为一条带"多少次尝试、丢多少事件、首个错误"摘要的 Err——**记了账但不重试**，这是 AGENTS.md "no retries" 契约的落地。
- [lib/llm/src/kv_router/publisher/mod.rs:411-445](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/publisher/mod.rs#L411-L445) —— 发现面挂牌：`DiscoveredKvEventSource { kv_state_endpoint, worker, publisher_id, recovery_target }` 序列化为 metadata，包进 `DiscoverySpec::EventSource { scope, topic: KV_EVENT_SUBJECT, publisher_id, metadata }` 注册。`recovery_target` 来自 `start_worker_kv_query_endpoint`（本地索引器开启时才有），失败则降级为"live-only KV source"并记 error。
- [lib/llm/src/kv_router/publisher/mod.rs:188-189](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/publisher/mod.rs#L188-L189) —— 结构体 doc 注释交代了一条重要生命周期约束：引擎侧 publisher 的寿命与这里广播的 publisher_id 绑定，**不支持引擎独立重启而 Dynamo 端存活**；未来要支持就必须先发有序的 rank 级 `Cleared` 或换新 publisher_id。
- [lib/llm/src/kv_router/indexer/recovery/subscriber.rs:71-118](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/indexer/recovery/subscriber.rs#L71-L118) —— 消费端订阅：从 `membership_watch` 解析出 `kv_state_endpoint`，建立带传输类型指定的 `EventSubscriber`，`.typed::<Vec<RouterEvent>>()` 得到类型化流；失败时退避重试并刷新 mismatch 指标。
- [lib/llm/src/kv_router/indexer/recovery/subscriber.rs:120-126](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/indexer/recovery/subscriber.rs#L120-L126) —— 防呆细节：订阅建好后**重读 watch**，若 endpoint 已变（worker 恰在此刻换源）则丢弃刚建的订阅重绑——注释写明"rejects a stale endpoint binding"。
- [lib/runtime/src/system_status_server.rs:174-177](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/runtime/src/system_status_server.rs#L174-L177) 与 [system_status_server.rs:301-315](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/runtime/src/system_status_server.rs#L301-L315) —— `/metrics` 路由遍历 DRT 下所有子注册表输出合并的 Prometheus 文本。给进程设 `DYN_SYSTEM_PORT=<正数>` 即可启用（默认 -1 关闭）——这是综合实践抓指标的开关。

#### 4.4.4 代码实践

**实践：追踪"路由器怎么找到事件源"的发现链**

1. **实践目标**：把 4.1 图中省略的"发现面挂牌 → 订阅"这半程补全，能独立说出路由器订阅目标的来源。
2. **操作步骤**：
   - 依次阅读 [mod.rs:411-445](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/publisher/mod.rs#L411-L445)（写侧挂牌）与 [subscriber.rs:71-118](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/indexer/recovery/subscriber.rs#L71-L118)（读侧解析），对照 `DiscoveredKvEventSource` 的四个字段。
   - 再看 [lib/llm/src/discovery/kv_source_watch.rs:264-295](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/discovery/kv_source_watch.rs#L264-L295)，确认 watch 侧如何校验 `scope` 与 `topic == KV_EVENT_SUBJECT`，排除别的 EventSource 记录。
3. **需要观察的现象**：写侧的 `metadata` 字段与读侧 `KvSourceMembershipView` 的解析字段一一对应；`recovery_target` 只在本地索引器成功启动时非空。
4. **预期结果**：能写出一句完整的话——"publisher 在发现面注册一条 EventSource 记录（topic=kv-events + publisher_id + 可选 recovery 端点），路由器的 kv_source_watch 监听到后按 endpoint 建立类型化订阅"。
5. 纯源码阅读，无需运行。

#### 4.4.5 小练习与答案

**练习 1**：`RouterEvent.state_source` 的注释要求"Keep this field last so legacy positional MessagePack remains prefix-compatible"。这保护的是哪种升级场景？

**答案**：N-2 滚动升级中新版本 worker 对旧版本 frontend 的方向。MessagePack 若按位置编码，新版本在结构体中间插入字段会让旧读者的所有后段字段错位；把新字段放最后，旧读者按前缀解码仍然得到完整合法的旧语义，新字段被忽略——正是 `lib/llm/AGENTS.md` "tolerant readers and conservative writers" 原则的字段级落地。

**练习 2**：普通 Store 事件写本地索引器失败只 warn 放行（fail-open），`Cleared` 失败却要撤销整个事件源（fail-closed）。用一个具体故障场景解释为什么必须区别对待。

**答案**：Store 失败的后果是本地索引器少记一些块——路由器的重合度打分略偏低，多算一点 prefill，无害。`Cleared` 失败的后果是本地索引器仍认为该 worker 缓存着全部块，而事件面已经广播"全清"——两边视图分裂。此后路由器会把带该前缀的请求继续派给这个实际已空缓存的 worker，每次都全量重算 prefill 却查不出原因；更糟的是 recovery 旁路会把本地这份错误快照当成权威状态供人查询。撤源（路由器转去别的源或重新恢复）是唯一能保证不再基于分裂状态做决策的处理。

---

## 5. 综合实践

### 综合实践：量化 mocker 集群的 KV 事件洪峰，并标定 batching 参数

这是贯穿本讲的主实验：**跑通"引擎 → ZMQ 线格式 → 中继 → 事件面 → 路由器"全链路，用现有指标测出每秒事件数，再做一次 timeout A/B，写出你自己的取舍结论。**

#### 目标

1. 确认 kv 路由模式下 mocker 确实在发 KV 事件，且事件走的是 ZMQ 线格式路径。
2. 用 Prometheus 指标（而非猜测）测出事件速率与过滤率。
3. 亲手验证 `batching_timeout_ms` 的三档语义对消息条数的影响，形成一张"参数 → 事件条数 → 新鲜度"的取舍表。

#### 步骤一：启动 frontend（kv 路由）

参考 [examples/router/custom-policy-example/README.md:280-288](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/examples/router/custom-policy-example/README.md#L280-L288) 的已知可用组合：

```bash
python -m dynamo.frontend \
  --router-mode kv \
  --discovery-backend file \
  --http-port 8000
```

#### 步骤二：启动 mocker worker（开启 ZMQ 事件口 + 指标口）

```bash
DYN_SYSTEM_PORT=9001 \
python -m dynamo.mocker \
  --model-path Qwen/Qwen3-0.6B \
  --discovery-backend file \
  --num-workers 1 \
  --zmq-kv-events-ports 5557
```

三个关键点：

- `--zmq-kv-events-ports 5557` 触发 4.1 讲的路径 B（完整 ZMQ 绕行）；省略它则走进程内直发路径 A。建议先跑 A 记一组数据，再加此参数跑 B 对比。
- `DYN_SYSTEM_PORT=9001` 打开 worker 进程的系统状态服务，`/metrics` 会输出本讲的 `dynamo_component_kv_publisher_*` 计数器（见 [system_status_server.rs:174-177](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/runtime/src/system_status_server.rs#L174-L177)）。
- 用 `--num-workers 1`：mocker 的多个 worker 各建一个 DistributedRuntime（[components/src/dynamo/mocker/main.py:171-181](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/mocker/main.py#L171-L181)），同进程多 runtime 争一个系统端口的绑定行为「待本地验证」，单 worker 最稳。

#### 步骤三：制造流量并采样事件速率

```bash
# 持续发请求（每条都会产生 BlockStored/BlockRemoved）
for i in $(seq 1 200); do
  curl -s localhost:8000/v1/chat/completions \
    -H 'Content-Type: application/json' \
    -d '{"model":"Qwen/Qwen3-0.6B","messages":[{"role":"user","content":"Hello!"}],"max_tokens":64}' \
    > /dev/null
done
```

两次采样求速率（间隔 30 秒各抓一次）：

```bash
curl -s localhost:9001/metrics | grep kv_publisher_zmq_events_total
# 记下两组数字后：每秒事件数 = (第二次 - 第一次) / 30
```

关注三行输出并计算两个比值：

- `stage="received"` 与 `stage="accepted"` 之差 = 被 normalizer 过滤掉的事件（对应 `zmq_filtered_events_total` 的各 reason）。
- `dynamo_component_kv_publisher_engines_dropped_events_total` 应当为 0——非零说明 ZMQ 段在丢事件（PUB 无订阅者时静默丢弃、或 HWM 溢出），值得排查。

辅助的日志抓手（不想抓指标时用）：worker 退出时的 debug 日志 `"ZMQ listener exiting, reason: ..., messages processed: N"`（[zmq_listener.rs:166-170](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/publisher/zmq_listener.rs#L166-L170)）直接给出事件总数；想把每批都看见可用 `RUST_LOG=dynamo_llm=trace` 打开 [zmq_listener.rs:110-116](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/publisher/zmq_listener.rs#L110-L116) 的 trace 日志（注意日志量很大，只短时开启）。

#### 步骤四：batching 参数 A/B

mocker 的 Python 启动器当前没有暴露 `batching_timeout_ms` 的 CLI 透传，所以分两条路做：

- **静态路（一定能做）**：4.3.4 的四条 `cargo test` 已经给出 `None` vs `Some(ms)` 的批数差异，把它作为理论基准。
- **动态路（需要改一行示例代码）**：mocker 走的是 Rust 侧默认值（[mocker.rs:652-659](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/mocker.rs#L652-L659) 调 `new_with_local_indexer` 时第 7 参传 `None`）。若你从 Python 侧自己构造 publisher，则可用 [lib/bindings/python/rust/llm/kv.rs:1182](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/bindings/python/rust/llm/kv.rs#L1182) 暴露的具名参数 `batching_timeout_ms=50` 做实验。**本教程禁止改源码**，动态路属于可选延伸，改前先在临时分支操作。

#### 需要观察的现象（ checklist）

- [ ] frontend 日志出现 KV 路由相关输出（u6-l2 的 `[ROUTING]` / Formula 观测三件套）。
- [ ] worker 日志出现 `"ZmqKvEventSink bound to tcp://0.0.0.0:5557"`（[zmq_events.rs:74](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/mocker/src/services/zmq_events.rs#L74)）与 `"KVEventPublisher connecting to ZMQ endpoint tcp://127.0.0.1:5557"`（[zmq_listener.rs:58-62](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/publisher/zmq_listener.rs#L58-L62)）——**这两个日志同时出现即证明完整绕行生效**。
- [ ] `/metrics` 中 `received` 与 `accepted` 均在增长。
- [ ] 重复发送**相同前缀**的请求若干次后，frontend 的 KV 命中率指标（u6-l2 提过的 `router_kv_hit_rate`）应上升——这是 indexer 被事件流喂饱的端到端证据。

#### 预期结果（写入你的实验记录）

| 观测项 | 预期 |
|--------|------|
| `engines_dropped_events_total` | 恒为 0 |
| `received` − `accepted` | 小的非负数（过滤掉的主要是非主注意力组/占位事件；mocker 通常接近 0） |
| 每秒事件数量级 | 与 (请求数 × 每请求块数) 同阶，具体数值「待本地验证」 |
| ZMQ 路径 vs 直发路径 | 事件语义等价，ZMQ 路径多一跳解码开销 |
| timeout 调大 | 消息条数下降、索引器新鲜度下降、publisher 内存上升（静态测试可证） |

#### 故障排查提示

- `/metrics` 没有任何 `kv_publisher` 指标：确认 `DYN_SYSTEM_PORT` 已设且为正数，且 curl 的是 **worker** 端口不是 frontend 的 8000。
- `received` 恒为 0：检查是否漏了 `--zmq-kv-events-ports`（此时走直发路径，不经过 ZMQ 中继，这些计数器自然不动）。
- 请求 404/模型不匹配：`--model-path` 与请求体里的 `model` 字段要一致（u1-l2 讲过分词器按模型名加载）。

## 6. 本讲小结

- **KV 事件是"目录"不是"数据"**：事件流只搬运块哈希与增删事实；KV 张量本身的传输是 u7-l3 NIXL 的职责。整条链是 worker →（ZMQ 线格式）→ 中继 →（事件面 `kv-events` 主题）→ 路由器 indexer 的单向广播。
- **线格式归引擎定义，Dynamo 做兼容读者**：三帧 ZMQ 消息（主题 + 8 字节大端序号 + msgpack 三元组 `[ts, events, dp_rank]`），`RawKvEvent` 四变体，哈希值兼容有符号/无符号，未知枚举靠 `#[serde(other)]` 前向兼容，新字段只能尾部追加。
- **洪峰三道闸**：normalizer 在**分配事件号之前**过滤（避免出站号空洞被误读为丢失）；`PlacementEventCoalescer` 只合并同归属、同 DP rank、且 Store 父链连续的相邻事件（默认上限 128 块）；dedup 按 (dp_rank, tier, domain) 引用计数抵消冗余 Remove。
- **`batching_timeout_ms` 是消息条数 ↔ 情报新鲜度 ↔ 内存 的三角旋钮**：默认 `None` 按原生批 flush，设为正数可跨批合并尾部，上限 15 秒；从 Python 侧经 PyO3 具名参数调节。
- **`Cleared` 是唯一的强语义屏障**：普通事件 at-most-once、无确认、无重试、失败仅记账；`Cleared` 必须在本地索引器跨层完成后才能发布，失败即撤销整个事件源——这个不对称是 `publisher/AGENTS.md` 的架构契约。
- **出站事件号严格单调、永不复用**，且与入站号是两套独立计数——消费端靠它检测丢失，合并逻辑靠它不产生虚假丢失告警。

## 7. 下一步学习建议

本讲讲完了"情报如何抵达路由器"。自然的推进方向：

- **u6-l4（基数树与 KV 索引）**：本讲 4.4 把 `RouterEvent` 交给了 indexer 就停笔了；下一讲深入 `lib/kv-router/src/indexer/radix_tree.rs`，看这些事件如何被组织成"哪些前缀缓存在哪个 worker"的压缩基数树，以及重合度查询的精确算法。这是本讲的直接下游。
- **恢复与补课机制**：本讲多次提到 `recovery_target` 与本地索引器旁路。想深挖可读 [lib/llm/src/kv_router/indexer/recovery/](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/indexer/recovery/subscriber.rs)——路由器重连后如何从 worker 查询端点把错过的状态补回来（`direct_zmq.rs` 还提供了绕过事件面直连 ZMQ 的第二条恢复路径）。
- **state_agent 路径**：本讲的 legacy publisher 之外还有一个为 vLLM KVCR 富化事件准备的 state-agent 生命周期（[publisher/state_agent.rs](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/publisher/state_agent.rs)，约 9 万字符）。它复用本讲的解码/合并/去重，但拥有独立的身份验证与有序 reset 语义——适合作为对照阅读，理解 `publisher/AGENTS.md` 里"shared processing / intentional differences"两条原则如何分界。
- **动手方向**：把综合实践的取舍表做完整（timeout 三档 × 两种路径），再对照 u6-l2 的 `router_kv_hit_rate` 指标，你就能回答"事件延迟换消息条数，到底牺牲了多少命中率"——这是把 u6 前三讲串成闭环的最好练习。

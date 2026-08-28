# 请求面 Pipeline：Ingress / Egress / PushRouter

## 1. 本讲目标

前两讲我们解决了两个问题：u3-l2 讲清了「服务目录」——一个 worker 如何把自己登记到 namespace/component/endpoint 路径下让客户端找到；u3-l3 讲清了「引擎抽象」——`AsyncEngine<Req, Resp, E>` 如何统一描述各种计算逻辑。

但这两个问题之间还缺一环：**客户端进程里的一个 `SingleIn<T>`，是怎么变成字节、穿过网络、最终变成 worker 进程里一次 `generate()` 调用的？** 这条链路就是本讲的主角——请求面（request plane）。

学完本讲你应该能够：

1. 画出一次跨进程调用在请求面上的完整时序，说清「请求通道」与「响应通道」是两条独立连接，以及为什么响应通道要由 worker 主动回拨。
2. 说清 `PushRouter` / `AddressedPushRouter` / `RequestPlaneClient` 三层各自的职责边界。
3. 区分两层编解码：传输层的 `TcpRequestMessage`、`TwoPartCodec`（怎么切成帧），与应用层的 `RequestPlanePayloadCodec`（帧里的 payload 用 JSON 还是 MessagePack）。
4. 解释 `PushWorkHandler` 的「推送语义」——为什么 `handle_payload` 返回 `()` 而不返回响应。
5. 列举 `RouterMode` 的七种模式，说清哪几种需要占用记账（occupancy）、哪几种根本不经过 `generate`。
6. 解释本次新增的 `dispatch_kv_admitted` 为什么跳过共享过载复查（`OverloadCheck::AlreadyAdmitted`），而普通直发 `dispatch_exact` 仍要复查。
7. 解释 #12004 在 `addressed_router.rs` 新增的 `FirstResponseGuard` 保留派发：为什么前端解码图像占用的 NIXL 注册内存必须活到目标 worker 的**首个响应**，而不是活到请求发出；以及 1024 个进程级保留派发上限的 `ResourceExhausted` 保护。
8. 不改一行源码，用现成的日志与 Prometheus 指标测出一条请求的平均 payload 大小。

## 2. 前置知识

本讲假设你已读过 u3-l2（服务注册模型）与 u3-l3（引擎抽象）。以下概念会直接使用：

- **`SingleIn<T>` / `ManyOut<U>`**：`Context<T>`（一进）与 `ResponseStream<U>`（多出）。Dynamo 里几乎所有 LLM 引擎都是 `AsyncEngine<SingleIn<T>, ManyOut<U>, Error>` 形态——一条请求、一串流式响应。
- **Instance / endpoint 路径**：`namespace/component/endpoint` 三段静态坐标 + `instance_id` 动态标识。客户端通过 `list_and_watch` 订阅到一组 instance。
- **`Context` 与取消链**：`context.stop_generating()` / `kill()`，以及 `is_stopped()` / `is_killed()`。请求面的许多分支都在区分「客户端主动取消」与「真实网络故障」。

另外补充四个本讲会反复出现的底层概念：

- **`bytes::Bytes`**：Rust 生态里引用计数的不可变字节块，切片不拷贝。请求面全程用它搬运负载，是理解「零拷贝路径」的前提。
- **call-home（回拨）模式**：不是「客户端连上 worker 然后一直读同一个连接」，而是客户端先在自己进程里起一个 TCP 流服务器、注册一个待领取的流，把「怎么找到我」写成 `ConnectionInfo` 塞进请求信封发给 worker；worker 收到后**主动反向连接**客户端，把响应帧推回去。这个设计让响应可以独立于请求连接做长生命周期流式传输。
- **信封（envelope）**：一帧外层消息里同时携带「控制信息」（JSON 编码的 `RequestControlMessage`）和「数据负载」（按 payload codec 编码的业务数据）。控制面信息和数据面信息走同一个帧、但用不同的编码规则。
- **过载准入（admission）**：负载感知路由在**选点那一刻**就要判断「这个 worker 还接不接得住」，并把这条请求计入它的占用。准入是选点方（路由宿主）的职责，而 `PushRouter` 里还有一份**共享的** client 级过载集合（`client.routing_instances().is_overloaded(...)`）——理解「谁已经查过、谁还要再查」是本次更新的关键。
- **NIXL 注册内存（registered memory）**：跨进程 GPU/主机间零拷贝传输的前提是把一段内存「注册」到 NIXL，拿到一个可序列化的描述符（descriptor），对端凭描述符直接 RDMA/NVLink 读这块内存。**注册关系由一个 Rust 值的生命周期守护**——那个 `Arc` 被 drop，注册就被撤销，对端再按描述符去读就是未定义行为。这条约束是 4.5 节整个 `FirstResponseGuard` 机制的出发点。

## 3. 本讲源码地图

本讲主体在 `lib/runtime/src/pipeline/network/` 这棵子树，4.5 节的守卫机制会跨出这棵子树、一直追到 lib/llm 的生产方与传播方：

| 文件 | 作用 |
|------|------|
| [lib/runtime/src/pipeline/network.rs](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network.rs) | 请求面「公共词汇表」：`RequestPlanePayloadCodec`、`RequestControlMessage`、`StreamSender/StreamReceiver`、`StreamOptions`、`Ingress`、`PushWorkHandler`、`NetworkStreamWrapper`、`Egress` 全部定义在这里 |
| [lib/runtime/src/pipeline/network/ingress.rs](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/ingress.rs) | 接收端模块入口，只有共享的 `drain_inflight` 优雅关停辅助函数 |
| [lib/runtime/src/pipeline/network/ingress/push_handler.rs](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/ingress/push_handler.rs) | **接收端核心**：`Ingress` 如何实现 `PushWorkHandler`，解码信封、回拨客户端、泵出响应流 |
| [lib/runtime/src/pipeline/network/ingress/shared_tcp_endpoint.rs](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/ingress/shared_tcp_endpoint.rs) | worker 进程共享的 TCP 监听器：按 `endpoint_path` 分发到 handler，维护有界工作队列 |
| [lib/runtime/src/pipeline/network/ingress/push_endpoint.rs](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/ingress/push_endpoint.rs) | NATS 遗留形态的 endpoint 包装（TCP 模式下不走这里） |
| [lib/runtime/src/pipeline/network/egress.rs](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress.rs) | 发送端模块入口，只有子模块声明 |
| [lib/runtime/src/pipeline/network/egress/push_router.rs](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/push_router.rs) | **发送端核心之一**：`PushRouter` 选 worker、记账、故障检测；`RouterMode` 与本次新增的 `OverloadCheck`/`dispatch_kv_admitted` 定义于此 |
| [lib/runtime/src/pipeline/network/egress/addressed_router.rs](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/addressed_router.rs) | **发送端核心之二**：`AddressedPushRouter` 组装信封、发送、等待 worker 回拨、解码响应流；本次 #12004 在文件头部新增 `FirstResponseGuard` 保留派发机制（4.5 节专讲） |
| [lib/runtime/src/pipeline.rs](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline.rs) | 请求面公共再导出口：`attach_first_response_guard` / `propagate_first_response_guard` 从这里暴露给 lib/llm |
| [lib/llm/src/migration.rs](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/migration.rs) | 守卫的**生产方**：frontend 引擎在请求携带解码媒体时把 NIXL 注册内存句柄 attach 进 context |
| [lib/llm/src/kv_router/prefill_router/mod.rs](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/kv_router/prefill_router/mod.rs) | 守卫的**传播方**：prefill 路由器把守卫带进派生出的 prefill 请求 context |
| [lib/runtime/src/pipeline/network/codec.rs](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/codec.rs) | TCP 请求帧 `TcpRequestMessage` 与响应 `TcpResponseMessage` 的线格式 |
| [lib/runtime/src/pipeline/network/codec/two_part.rs](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/codec/two_part.rs) | `TwoPartCodec`：把「控制头 + 数据体」切成一帧的编解码器 |
| [lib/runtime/src/pipeline/network/manager.rs](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/manager.rs) | `NetworkManager`：唯一读取网络环境变量、按 `RequestPlaneMode` 创建 server/client 的地方 |
| [lib/llm/src/kv_router/routing_host/kv.rs](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/kv_router/routing_host/kv.rs) | 本次新增 API 的**调用方**：KV 路由宿主在选点完成后用 `dispatch_kv_admitted` 直发 |

一个容易混淆的提示：`egress.rs` 与 `ingress.rs` 两个文件本身几乎是空壳（只有 `pub mod` 声明），真正的实现都在同名子目录里。这是「目录即命名空间」的组织方式，读源码时别被空文件骗了。

## 4. 核心概念与源码讲解

### 4.1 Egress：从 `PushRouter` 到 `AddressedPushRouter` 的发送链

#### 4.1.1 概念说明

发送端（客户端/frontend 进程）分三层，每层只回答一个问题：

| 层 | 回答的问题 | 关键类型 |
|----|-----------|---------|
| 选谁 | 这条请求发给哪个 worker？worker 挂了怎么办？ | `PushRouter<T, U>` |
| 怎么送 | 选定的 worker 地址是什么？信封怎么组装、怎么发？ | `AddressedPushRouter` |
| 走什么线 | TCP 还是 NATS？socket 怎么写？ | `RequestPlaneClient`（trait object） |

这个分层的精妙之处在于**接缝（seam）的显式化**：`AddressedPushRouter` 实现了一个专门的 `StreamingDispatch` trait，注释里明说「Selection, occupancy, fault detection, and migration stay in `PushRouter` above the seam; only the transport below it changes」。也就是说，如果你想换一种传输（比如换成共享内存或 QUIC），只需要实现 `StreamingDispatch`，上面的选点、故障检测、迁移逻辑原样复用。

最底层由 `NetworkManager` 屏蔽：它是全代码库里**唯一**读取网络相关环境变量、唯一知道传输具体类型的地方，其余代码只拿 `Arc<dyn RequestPlaneClient>` 这种 trait object。

#### 4.1.2 核心流程

一次 unary（`SingleIn` → `ManyOut`）跨进程调用的完整时序：

```
客户端进程                                    worker 进程
──────────                                    ───────────
PushRouter::round_robin(request)
  ├─ select_untracked_worker()     ──选 instance_id
  └─ generate_with_fault_detection_prepared()
       ├─ resolve_transport()      ──查 instance → tcp 地址
       ├─ check_workers_available()──过载检查（OverloadCheck::Required 时）
       └─ AddressedPushRouter::dispatch_and_finalize()
            ├─ register_streams()  ──在本地 TcpStreamServer 上
            │                        预注册一个待领取的响应流，
            │                        拿到 ConnectionInfo
            ├─ associate_instance() ──墓碑检查（worker 是否刚被摘除）
            ├─ build_request_envelope()
            │     ├─ RequestControlMessage (JSON)   ← 控制头
            │     ├─ payload_codec.encode(&request) ← 数据体
            │     └─ TwoPartCodec::encode_message() ← 打成一帧
            ├─ dispatch_buffer()
            │     ├─ 注入 trace / request-id / x-frontend-send-ts-ns
            │     └─ req_client.send_request(addr, buffer, headers)
            │                          ──────────────►  SharedTcpServer 监听
            │                                             ├─ 解出 endpoint_path
            │                                             ├─ 查 handler 表
            │                                             └─ 入有界工作队列
            │                                                               │
            │                                          handle_work_item()
            │                                          PushWorkHandler::handle_payload()
            │                                               │ (详见 4.3)
            ├─ detect_worker_rejection_response(ack)  ◄──── 空 ACK = 已入队
            │
            ├─ response_stream_provider.await()   ←──── worker 回拨本进程，
            │   （oneshot，阻塞到回拨成功）              把 StreamSender 领走
            │
            └─ decode_response_stream()  ← 把 Bytes 解回 U，
               包上故障检测 + 可选超时，返回 ManyOut<U>

  【守卫分支】AsyncEngine::generate 的入口会先查 context 里有没有
   FirstResponseGuard（4.5 节）。有 → 整条 dispatch_and_finalize 被
   spawn 进独立任务，调用方立刻拿到一个"占位流"，守卫与信号量许可
   一起活到 worker 的首个响应项；无 → 走上面的常规路径。
```

注意两个「反直觉」的点：

1. **请求通道只传一帧，然后就被复用了。** 客户端发出信封、收到一个空的 ACK，请求通道的使命就结束了。后续的所有响应帧走的是 worker 主动建立的那条**新**连接。
2. **客户端会阻塞在 `oneshot` 上等回拨。** `response_stream_provider.await()` 要等 worker 真正连回来、把 `StreamSender` 交出去才 resolve。如果 worker 在建立响应流之前就死了，`oneshot` 被 drop，客户端收到 `RecvError`，映射成可迁移的 `Disconnected` 错误。

第 3 个反直觉的点由 4.5 节展开：**常规路径里 `dispatch_and_finalize` 的整个生命周期都嵌在调用方的 future 里**——调用方被取消（客户端断连、上游超时），这个 future 连同它持有的所有 RAII 资源一起被 drop。这对绝大多数请求是正确的；但对「请求里携带了供远端读取的注册内存」的请求是致命的：调用方一取消，注册就被撤销，而远端 worker 可能还在按描述符读那块内存。

#### 4.1.3 源码精读

**① `PushRouter` 的字段——三层职责的物证**

[lib/runtime/src/pipeline/network/egress/push_router.rs:130-159](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/push_router.rs#L130-L159) 定义了 `PushRouter` 的核心字段：`client` 负责从 etcd 拿远端 instance 信息，`router_mode` 决定选点策略，`picker` 是「共享的、与调度器无关的策略状态」。

最关键的是 `addressed` 字段——它就是那个传输接缝：

```rust
/// The final hop: after selecting an instance, `PushRouter` hands it to this
/// `StreamingDispatch` (the request-plane `AddressedPushRouter` by default).
/// A trait object so an alternate transport can swap it out.
addressed: Arc<dyn StreamingDispatch<T, U>>,
```

构造函数 [push_router.rs:623-686](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/push_router.rs#L623-L686) 里的 `let addressed = addressed_router(&client.endpoint).await?;` 一行，就是把默认的 `AddressedPushRouter` 装进这个接缝，随后立即 type-erase 成 `Arc<dyn StreamingDispatch<T, U>>`。

**② `AddressedPushRouter` 的两个半边**

[lib/runtime/src/pipeline/network/egress/addressed_router.rs:507-514](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/addressed_router.rs#L507-L514)：

```rust
#[derive(Clone)]
pub struct AddressedPushRouter {
    // Request transport (unified trait object - works with all transports)
    req_client: Arc<dyn RequestPlaneClient>,

    // Response transport (TCP streaming - unchanged)
    resp_transport: Arc<tcp::server::TcpStreamServer>,
}
```

两个字段正好对应两条通道：`req_client` 发请求信封，`resp_transport` 是**本进程**的响应流服务器（worker 要回拨的目标）。构造入口 [addressed_router.rs:516-544](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/addressed_router.rs#L516-L544) 从 `NetworkManager` 拿 client、从 `DistributedRuntime` 拿流服务器——这印证了「响应流服务器是进程级共享的」。

值得注意：**`#[derive(Clone)]` 是本次 #12004 新加的**。保留派发（4.5 节）要把整个 router clone 进一个独立 spawn 的任务里，这要求 router 本身可克隆——而它天然满足：两个字段都是 `Arc`，克隆只是引用计数加一。

**③ 发送主体 `dispatch_and_finalize`**

这是本讲最重要的一个函数，[addressed_router.rs:597-747](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/addressed_router.rs#L597-L747)。它的注释写得很清楚：wire 形态由输入推断——`input_stream = Some` 就是双向流式（header-only 信封），`input_stream = None` + `request = Some` 就是 unary（两段式信封）。

先注册流（[addressed_router.rs:623-628](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/addressed_router.rs#L623-L628)）：

```rust
let (send_registered, recv_registered) = self
    .register_streams(engine_ctx.clone(), enable_request_stream, true)
    .await?;
let recv_registered = recv_registered.ok_or_else(|| {
    anyhow::anyhow!("response stream registration missing despite enable_response_stream")
})?;
```

`register_streams`（[addressed_router.rs:749-791](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/addressed_router.rs#L749-L791)）在本地流服务器上按需注册「发送半边」和「接收半边」，返回的 `RegisteredStream` 里除了 `ConnectionInfo` 还有一个 `oneshot::Receiver`。

然后是墓碑检查（[addressed_router.rs:630-653](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/addressed_router.rs#L630-L653)）：如果发现面已经把这个 worker 摘掉了，就**在往请求面写字节之前**快速失败，返回 `Disconnected`，让上层走迁移重试，而不是发出去石沉大海。

发送（[addressed_router.rs:664-666](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/addressed_router.rs#L664-L666)）：

```rust
let tx_start = Instant::now();
let request_plane_response = self.dispatch_buffer(address, buffer, context.id()).await?;
REQUEST_PLANE_SEND_SECONDS.observe(tx_start.elapsed().as_secs_f64());
```

`dispatch_buffer`（[addressed_router.rs:793-822](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/addressed_router.rs#L793-L822)）注入三类 header：trace 上下文、`request-id`、`x-frontend-send-ts-ns`（墙钟纳秒时间戳，worker 侧用它算网络传输耗时）。返回值是请求面 ACK 字节。

接着解析 ACK（[addressed_router.rs:668-679](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/addressed_router.rs#L668-L679)）：worker 的拒绝会体现在 ACK 而不是响应流上，所以要**先**检查再等回拨，否则会一直等一个永远不会建立的响应连接。`detect_worker_rejection_response`（[addressed_router.rs:825-849](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/addressed_router.rs#L825-L849)）识别 `Server overloaded:` / `Server unavailable:` 两种前缀。

最后等回拨并解码（[addressed_router.rs:703-746](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/addressed_router.rs#L703-L746)）：`recv_registered.into_parts()` 会**解除** RAII 清理（领取后，这个 subject 由 worker 的回拨或发现面的 watcher 来收割，不再由本端清理）；`response_stream_provider.await` 的三种结局——`Ok(Ok(stream))` 正常、`Ok(Err(e))` 映射 `CannotConnect`（worker 本地 setup 失败）、`Err(_)` 即 oneshot 被 drop，映射 `Disconnected`。

**④ 传输接缝 trait**

[addressed_router.rs:925-965](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/addressed_router.rs#L925-L965) 定义 `StreamingDispatch`：`generate`（unary）、`generate_bidirectional`（流式输入）、`on_instance_removed` / `on_instance_added`（发现面驱动的清理回调）。

doc 注释里有一条**强约束**值得抄下来：实现方必须把故障暴露为顶层 `crate::error::ErrorType` 变体（`CannotConnect` / `Disconnected` / `ConnectionTimeout` / `ResponseTimeout` / `WorkerOverloaded` / `ResourceExhausted` / `Cancelled`），否则 `wrap_with_fault_detection` 的「报 down / 报过载 / 迁移」逻辑全部失效。这是类型系统外的隐式契约。

**⑤ `NetworkManager` 屏蔽传输差异**

[lib/runtime/src/pipeline/network/manager.rs:237-242](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/manager.rs#L237-L242)：

```rust
pub fn create_client(&self) -> Result<Arc<dyn RequestPlaneClient>> {
    match self.mode {
        RequestPlaneMode::Tcp => self.create_tcp_client(),
        RequestPlaneMode::Nats => self.create_nats_client(),
    }
}
```

`RequestPlaneMode` 是 u3-l1 讲过的三个正交开关之一。切换 `DYN_REQUEST_PLANE=nats` 后，`AddressedPushRouter` 里 `req_client` 指向的实现就换成了 NATS 客户端，而上面的选点与故障检测代码**一行不改**——这就是接缝的价值。

#### 4.1.4 代码实践

**实践目标**：不改任何源码，通过 trace 日志亲眼看到「控制头字节数 + 数据体字节数」，验证你对发送链的理解。

**操作步骤**：

1. 终端 A 启动 worker（沿用 u2-l1 的 hello_world 示例）：

   ```bash
   cd examples/custom_backend/hello_world
   DYN_DISCOVERY_BACKEND=file DYN_EVENT_PLANE=zmq \
     python hello_world.py
   ```

2. 终端 B 启动 client，并把 `dynamo_runtime` 域的日志开到 `trace`：

   ```bash
   cd examples/custom_backend/hello_world
   DYN_DISCOVERY_BACKEND=file DYN_EVENT_PLANE=zmq \
     RUST_LOG=dynamo_runtime=trace \
     python client.py
   ```

3. 观察 client 进程的 stderr。

**需要观察的现象**：client 侧应出现类似这样的日志行（来自 `build_request_envelope`）：

```
packaging two-part message; ctrl: 210 bytes, data: 24 bytes
```

以及 `awaiting transport handshake`、`creating tcp response stream` 等行。试着把 `client.py` 里 `client.generate("world,sun,moon,star")` 的字符串加长一倍，重启后对比 `data:` 后面的数字。

**预期结果**：`ctrl`（控制头，JSON 编码的 `RequestControlMessage`）长度基本不变，因为它只含 request-id、connection_info、metadata 等；`data`（payload codec 编码的业务数据）长度随请求内容线性增长。这直观地印证了「两层编解码」的分离。

**说明**：本实践未实际运行验证，具体日志措辞与字节数以本地输出为准（标注为「待本地验证」）。若 `RUST_LOG` 过滤后看不到，去掉域过滤直接用 `RUST_LOG=trace` 再试。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `dispatch_and_finalize` 要在 `dispatch_buffer` 之后、`response_stream_provider.await` 之前先调用 `detect_worker_rejection_response`？

**答案**：worker 的过载/不可用拒绝体现在请求面 ACK 的字节里，而不是响应流上。如果不先检查，客户端会一直阻塞在等待回拨的 `oneshot` 上——而 worker 已经拒绝了这个请求，永远不会回拨建立响应流，请求就悬挂到超时。源码注释原话是「Short-circuit before waiting on a response-plane connection the worker will never open」。

**练习 2**：`resolve_transport` 里，当选中的 instance 已经从发现面消失时，`TransportFallback::Deny` 和 `TransportFallback::Allow` 行为有何不同？`direct()` 用的是哪种？

**答案**：`Deny` 直接返回 `ErrorType::CannotConnect`（精确目标语义，不做任何重选）；`Allow` 会从当前可路由的 free 列表里另选一个 worker 并打 warn 日志「Instance disappeared during routing, reselecting」。`direct()`（[push_router.rs:998-1010](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/push_router.rs#L998-L1010)）用的是 `Deny`；而 `direct_within` / `dispatch_preselected_prepared` 允许 fallback，`direct_within` 还能用 `TransportFallback::Within(allowed)` 把重选范围限定在调用者给的集合内（LoRA 副本集过滤就用它）。

---

### 4.2 `RequestPlanePayloadCodec` 与两层编解码

#### 4.2.1 概念说明

请求面上有**两层**完全不同的编解码，初学者最容易把它们混为一谈：

| 层 | 问题 | 类型 | 谁决定 |
|----|------|------|--------|
| 成帧层（framing） | 字节流上怎么切出一个完整的消息？ | `TcpRequestMessage` / `TwoPartCodec` / `TcpResponseCodec` | 传输协议（TCP 长度前缀） |
| 负载层（payload codec） | 帧里的业务数据用什么 serde 格式？ | `RequestPlanePayloadCodec`（Json / Msgpack） | 环境变量 + 目标 worker 的广告 |

「成帧」解决的是 TCP 字节流的边界问题——TCP 只给你一串无边界的字节，你需要长度前缀才知道一个消息从哪开始到哪结束。「负载」解决的是序列化效率问题——JSON 可读但冗长，MessagePack 紧凑但不可读。

一个关键的、容易被忽略的细节：**控制头永远是 JSON，只有数据体跟随 payload codec**。这是刻意的线兼容设计——`RequestPlanePayloadCodec` 枚举的 serde 默认值是 `Json`，注释写明「The serde default deliberately remains JSON for wire compatibility with control messages produced before the payload codec field existed」。也就是说，老版本的 worker 收到新版本 frontend 发的 `RequestControlMessage`，多余字段会被忽略、缺失字段取默认 `json`，两边仍然能对话。

#### 4.2.2 核心流程

**unary 请求帧的完整线格式**（`TcpRequestMessage`）：

```
┌─────────────────────┬──────────────────────┬─────────────────────┬──────────────────────┬─────────────────────┬───────────────┐
│ endpoint_path_len   │ endpoint_path        │ headers_len         │ headers (JSON)       │ payload_len         │ payload       │
│ u16 big-endian      │ UTF-8 字符串          │ u16 big-endian      │ HashMap<String,String>│ u32 big-endian      │ 原始字节        │
│ 2 字节               │                      │ 2 字节               │                      │ 4 字节               │               │
└─────────────────────┴──────────────────────┴─────────────────────┴──────────────────────┴─────────────────────┴───────────────┘
```

其中 `payload` 那一段本身又是一帧 `TwoPartMessage`：

```
┌──────────────┬──────────────┬──────────────┬─────────────────────────────┬─────────────────────────────┐
│ header_len   │ body_len     │ checksum     │ header                      │ data                        │
│ u64          │ u64          │ u64 (xxh3)   │ JSON 的 RequestControlMessage│ payload_codec 编码的业务数据  │
└──────────────┴──────────────┴──────────────┴─────────────────────────────┴─────────────────────────────┘
```

注意嵌套关系：`TcpRequestMessage` 是**外层**（传输成帧），它整个 `payload` 字段装的是**内层**的 `TwoPartMessage`（信封）。外层管「这个 worker 进程的哪个 endpoint 处理这帧」，内层管「这条请求的控制信息与业务数据」。

`endpoint_path` 的格式是 `{instance_id:x}/{endpoint_name}`（见 [component/endpoint.rs:341-344](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/component/endpoint.rs#L341-L344) 的 `format!("{}:{}/{:x}/{}", tcp_host, tcp_port, connection_id, endpoint_id.name)`），而完整的传输地址是 `host:port/{instance_id:x}/{endpoint_name}`——多个 worker 共享同一个 TCP server 时（`--num-workers > 1`），靠 instance_id 部分区分路由。

**payload codec 的决策优先级**：

```
发送方：
  payload_codec_for_worker(instance)
    = instance.request_plane_codec     ← worker 在发现面广告的值
      .unwrap_or(Json)                 ← 老版本 worker 没广告 → 兜底 JSON

进程级默认值（决定「广告什么」）：
  RequestPlanePayloadCodec::configured()
    = DYN_REQUEST_PLANE_CODEC 环境变量
      None | "" | "msgpack" | 非法值 → Msgpack
      "json"                          → Json
```

#### 4.2.3 源码精读

**① `RequestPlanePayloadCodec` 枚举与编解码**

[lib/runtime/src/pipeline/network.rs:57-65](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network.rs#L57-L65)：

```rust
#[derive(Debug, Clone, Copy, Default, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum RequestPlanePayloadCodec {
    /// The serde default deliberately remains JSON for wire compatibility with
    /// control messages produced before the payload codec field existed.
    #[default]
    Json,
    Msgpack,
}
```

`encode` / `decode` 是薄薄的一层分发（[network.rs:102-114](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network.rs#L102-L114)）：Json 走 `serde_json::to_vec` / `from_slice`，Msgpack 走 `rmp_serde::to_vec_named` / `from_slice`。

**② 进程级缓存与环境变量解析**

[network.rs:67-93](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network.rs#L67-L93)。注意 `REQUEST_PLANE_PAYLOAD_CODEC: OnceLock`（[network.rs:44](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network.rs#L44)）——**首次读取后进程内缓存**，之后改环境变量无效。环境变量名定义在 [config/environment_names.rs:717-721](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/config/environment_names.rs#L717-L721)，注释明确写了默认是 `msgpack`。

值得留意一个「看起来像 bug 其实是设计」的点：`from_config_value` 里**非法值也落到 Msgpack**，还会打一条 warn。因为 Msgpack 是当前推荐默认，所以「配错了」宁可回到默认也不报错。

**③ 发送方按目标 worker 协商**

[addressed_router.rs:327-335](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/addressed_router.rs#L327-L335)：

```rust
fn payload_codec_for_worker(instance: Option<&Instance>) -> RequestPlanePayloadCodec {
    instance
        .and_then(|instance| instance.request_plane_codec)
        .unwrap_or(RequestPlanePayloadCodec::Json)
}
```

而 worker 侧在注册 endpoint 时把进程级配置广告出去，[component/endpoint.rs:186](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/component/endpoint.rs#L186) 的 `Instance` 字段 `request_plane_codec: Some(RequestPlanePayloadCodec::configured())`。

**④ 信封组装**

[addressed_router.rs:265-325](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/addressed_router.rs#L265-L325) 的 `build_request_envelope` 是两层编解码的汇合点：先 `serde_json::to_vec(&control_message)` 得到控制头（有 128 KiB 上限，见 `CONTROL_MESSAGE_MAX_BYTES` 于 [addressed_router.rs:248](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/addressed_router.rs#L248)），再 `payload_codec.encode(req)` 得到数据体，最后：

```rust
let msg = match data {
    Some(d) => TwoPartMessage::from_parts(ctrl.into(), d.into()),   // unary
    None => TwoPartMessage::from_header(ctrl.into()),               // 双向流式
};
let codec = TwoPartCodec::default();
let buffer = codec.encode_message(msg)?;
```

**⑤ `TcpRequestMessage` 的编码与零拷贝优化**

[codec.rs:264-303](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/codec.rs#L264-L303) 的 `encode` 把四段长度/内容依次写进 `BytesMut` 再 `freeze()` 成 `Bytes`。而 [codec.rs:308-334](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/codec.rs#L308-L334) 的 `into_frame` 是发送路径上的优化：只拼协议头、把 payload 保留为独立 `Bytes` 块，避免先把整个帧拍平造成一次大拷贝。对应的单测 [codec.rs:541-591](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/codec.rs#L541-L591) 里甚至断言了 `frame.payload.as_ptr() == payload.as_ptr()`——指针相同，证明确实零拷贝。

**⑥ `TwoPartCodec` 的解码**

[codec/two_part.rs:41-109](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/codec/two_part.rs#L41-L109)。三段定长头共 24 字节（header_len u64 + body_len u64 + checksum u64）。两个细节：

- **checksum 只在 debug 构建校验**（[two_part.rs:81-101](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/codec/two_part.rs#L81-L101)），release 版写 dummy 0、读侧跳过——用 xxh3 校验换性能。
- 解码是**增量友好**的：字节不够就返回 `Ok(None)`，等下一次有数据再试，这正是 tokio `Decoder` 的标准姿势，天然适配 TCP 半包。

**⑦ 最大消息尺寸**

[network.rs:40-55](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network.rs#L40-L55)：默认 32 MiB，可用 `DYN_TCP_MAX_MESSAGE_SIZE` 覆盖，client、server、零拷贝解码三条路径共用这一个 `OnceLock` 值。

#### 4.2.4 代码实践

**实践目标**：用现成的单元测试验证「控制头永远 JSON、数据体跟随 codec」这一线兼容设计。

**操作步骤**：

```bash
cargo test -p dynamo-runtime --lib \
  pipeline::network::tests::legacy_frontend_control_message_defaults_payload_codec_to_json
cargo test -p dynamo-runtime --lib \
  pipeline::network::tests::request_plane_payload_codec_configuration_defaults_to_msgpack
cargo test -p dynamo-runtime --lib \
  pipeline::network::tests::request_plane_payload_codec_round_trips_response_wrapper_json_and_msgpack
```

**需要观察的现象**：三个测试都通过。重点读第一个测试（[network.rs:515-552](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network.rs#L515-L552)）：一段**不含** `payload_codec` 字段的 JSON 反序列化成 `RequestControlMessage` 后，`message.payload_codec` 自动等于 `Json`，并且能用它成功解码一段 JSON payload——这就是「老 frontend 发的消息新 worker 也能读」的机制证明。

**预期结果**：三条测试全部 PASS。若想进一步，把第一个测试 JSON 里加上 `"payload_codec": "msgpack"` 再手动跑一次，应看到第二个测试断言的行为（等于 `Msgpack`）。此实践需本地有 Rust 工具链，结果标注为「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：如果 frontend 进程设了 `DYN_REQUEST_PLANE_CODEC=json`，而 worker 进程设了 `DYN_REQUEST_PLANE_CODEC=msgpack`（都没重启过），会发生什么？

**答案**：不会坏。发送方用 `payload_codec_for_worker`，**以 worker 在发现面广告的值为准**（worker 广告的是它自己的 `configured()`，即 msgpack），所以 frontend 会用 msgpack 编码数据体，worker 用 msgpack 解码，两边一致。环境变量影响的是「本进程广告什么」，不是「本进程发送时用什么」——发送时永远迁就接收方。只有当目标 worker 是不广告 codec 的老版本时才兜底 JSON。

**练习 2**：为什么 `RequestControlMessage` 坚持用 JSON 而不跟随 payload codec？

**答案**：控制头是协商**之前**就要被对方读懂的信息——`payload_codec` 字段本身就在控制头里！如果控制头也用 msgpack 编码，接收方在解析之前无法知道该用什么解码，形成鸡生蛋问题。此外保持控制头为 JSON 还带来线兼容收益：serde 默认值让新旧版本的字段可以互相缺省。

---

### 4.3 Ingress 与 `PushWorkHandler`：接收端的推送语义

#### 4.3.1 概念说明

接收端（worker 进程）要回答的问题是：「一帧字节到达后，怎么把它变成一次引擎调用，并把结果送回去？」

`PushWorkHandler` 是这一切的入口 trait，它的名字里那个 **Push** 是理解本讲的关键。看它的签名：

```rust
#[async_trait]
pub trait PushWorkHandler: Send + Sync {
    async fn handle_payload(
        &self,
        payload: Bytes,
        request_id: Option<String>,
    ) -> Result<(), PipelineError>;
```

**返回 `()`，不返回响应。** 这不是疏忽——响应根本不走这个返回值，而是走一条**完全独立的旁路**：worker 拿到信封里客户端预注册的 `ConnectionInfo`，主动回拨过去，把响应帧一条一条**推**上去。`handle_payload` 只负责「把请求处理完」这个动作本身，处理结果的生命周期由另一个异步任务（泵）管理。

这种「推送语义」带来的好处：

1. **请求连接可以立即释放**——worker 收到信封、回一个空 ACK 就完了，不需要在这条连接上等生成结束。
2. **流式天然支持**——生成一个 token 推一个 token，客户端边收边渲染。
3. **背压清晰**——响应通道的 mpsc 缓冲满了 `send` 就会挂起（`DEFAULT_SEND_BUFFER_COUNT = 64` 帧），自然把压力传回引擎。

代价是**终止协议必须显式**：连接断开不代表流结束，所以需要一个明确的 `complete_final: true` 终止帧，以及一个在流真正可用前先行发送的 `prologue`。

#### 4.3.2 核心流程

worker 进程内的处理链：

```
SharedTcpServer (监听)
  └─ ZeroCopyTcpDecoder 解帧 → TcpRequestMessage
      └─ 取 endpoint_path = "{instance_id:x}/{endpoint_name}"
          └─ 查 handlers 表 → 找到该 endpoint 的 Arc<dyn PushWorkHandler>
              └─ 构造 WorkItem 入有界队列（满了 try_reserve 直接拒绝并计数）
                  └─ dispatcher 拉取 → handle_work_item()
                      ├─ 从 headers 取 x-frontend-send-ts-ns 算网络传输耗时
                      ├─ 从 trace headers 建 span、取 request-id
                      └─ handler.handle_payload(payload, request_id)
                          └─ Ingress::handle_payload_shared()
                              ├─ ① 指标 guard：requests_total++、inflight++、
                              │     request_bytes += payload.len()
                              ├─ ② parse_and_build_request()  [IngressDispatch]
                              │     ├─ TwoPartCodec 解出 (control, data)
                              │     ├─ serde_json 解出 RequestControlMessage
                              │     ├─ payload_codec.decode::<T>(data)
                              │     └─ Context::with_id_and_metadata(t, id, metadata)
                              ├─ ③ TcpClient::create_response_stream(context, conn_info)
                              │     ← worker 主动回拨客户端
                              ├─ ④ segment.generate(request) → ManyOut<U>
                              ├─ ⑤ publisher.send_prologue(None 或错误)
                              │     ← 告诉客户端"流已就绪"或"generate 失败"
                              └─ ⑥ pump_response_stream(stream, publisher, codec)
                                    ├─ 每帧: NetworkStreamWrapper{data: Some(u), complete_final: false}
                                    ├─ encode → publisher.send(bytes)
                                    ├─ 失败时按 is_stopped()/is_killed() 分类计错
                                    └─ 末帧: NetworkStreamWrapper{data: None, complete_final: true}
```

流终止的三段协议：

| 帧 | 内容 | 作用 |
|----|------|------|
| prologue | `ResponseStreamPrologue{error: Option<String>}` | 在 `generate` 允许返回**之前**发送；有错则客户端直接收到错误而不创建流 |
| 数据帧 × N | `NetworkStreamWrapper{data: Some(u), complete_final: false}` | 正常内容 |
| 终止帧 | `NetworkStreamWrapper{data: None, complete_final: true}` | 显式结束标志 |

客户端 `decode_response_stream` 对终止协议的三个防御（[addressed_router.rs:170-246](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/addressed_router.rs#L170-L246)）：看到 `complete_final` 后又来数据 → 报「this should never happen」；流断且 `is_complete_final` 为 false 且 context 已 stopped → 静默结束（客户端取消）；流断且未 stopped → 合成 `Disconnected` 错误（worker 掉了）。

#### 4.3.3 源码精读

**① `PushWorkHandler` 与 `NetworkStreamWrapper`**

trait 定义在 [network.rs:844-867](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network.rs#L844-L867)。它有三个方法：`handle_payload`（核心）、`add_metrics`（把 endpoint 的指标注入）、`set_endpoint_health_check_notifier`（金丝雀健康检查的定时器重置通知，带默认空实现保证向后兼容）。

终止帧的载体 [network.rs:913-918](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network.rs#L913-L918)：

```rust
#[derive(Serialize, Deserialize, Debug, PartialEq, Eq)]
pub struct NetworkStreamWrapper<U> {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub data: Option<U>,
    pub complete_final: bool,
}
```

它上方的块注释（[network.rs:869-911](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network.rs#L869-L911)）完整解释了为什么不能用 `Annotated` 直接做这件事（`U` 可能已经是 `Annotated` 会双重包装），以及为什么截断检测必须在 egress 侧做（只有它能区分网络截断与正常结束）。这条注释是本讲最值得通读的一段文档。

**② `Ingress` 的结构与两种构造路径**

[network.rs:746-752](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network.rs#L746-L752)：

```rust
pub struct Ingress<Req: PipelineIO, Resp: PipelineIO, Adapter = SerdeIngressPayloadAdapter> {
    segment: OnceLock<Arc<SegmentSource<Req, Resp>>>,
    metrics: OnceLock<Arc<WorkHandlerMetrics>>,
    endpoint_health_check_notifier: OnceLock<Arc<tokio::sync::Notify>>,
    payload_adapter: Arc<Adapter>,
}
```

三个 `OnceLock` 说明 `Ingress` 是「构造后单次装配」的：segment（指向引擎的管线段）、metrics、健康通知器各只能设一次。

两条构造路径：`for_engine`（[network.rs:771-774](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network.rs#L771-L774)）是 u3-l3 讲过的「把引擎挂上 endpoint」的标准方式——内部 `frontend.link(backend)?.link_terminal(frontend)?` 闭合成环；`attach` 则允许先建空 Ingress 再事后接 segment。

第三个泛型参数 `Adapter` 是本讲的一个扩展点：默认 `SerdeIngressPayloadAdapter`，但你可以在 `for_engine_with_adapter` 里换成自己的实现（比如对多模态请求做特殊解码），见 [network.rs:822-836](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network.rs#L822-L836)。

**③ `IngressDispatch`：单目与双向的分流**

[push_handler.rs:367-375](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/ingress/push_handler.rs#L367-L375) 定义了这个内部 trait，注释点明它的存在意义：**捕获单目与双向两种 wire 形态的差异，把其余所有东西（指标 guard、响应流打开、`segment.generate`、prologue、泵）都留在共享的 `handle_payload_shared` 里**。

单目实现（[push_handler.rs:377-434](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/ingress/push_handler.rs#L377-L434)）里有一个防御：收到 header-only 信封就报「unary engine received a header-only envelope」——因为单目引擎的业务数据就在 data 半边，没有 data 就没有请求。

双向实现（[push_handler.rs:436-564](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/ingress/push_handler.rs#L436-L564)）则反过来拒绝带 data 的信封，然后**worker 主动回拨** `request_stream_connection_info`（`TcpClient::create_request_stream`，[push_handler.rs:499-512](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/ingress/push_handler.rs#L499-L512)），并 spawn 一个 forwarder 任务把原始字节逐帧解成 `T` 喂给引擎的输入流。注意 forwarder 每轮都查 `is_killed() || is_stopped()`，避免在引擎已经放弃后还往一个没人消费的 channel 里灌数据。

**④ `handle_payload_shared` 的共享主体**

[push_handler.rs:579-694](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/ingress/push_handler.rs#L579-L694)。

指标 guard 的 RAII 设计（[push_handler.rs:594-607](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/ingress/push_handler.rs#L594-L607)）：

```rust
let _inflight_guard = self.metrics().map(|m| {
    m.request_counter.inc();
    m.inflight_requests.inc();
    m.request_bytes.inc_by(payload.len() as u64);
    ...
    RequestMetricsGuard { ... }
});
```

`RequestMetricsGuard`（[push_handler.rs:125-141](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/ingress/push_handler.rs#L125-L141)）的 `Drop` 实现里做 `inflight_requests.dec()` 和 `request_duration.observe()`——**无论函数从哪个分支退出，指标都会被正确收尾**。函数末尾还有一句 `drop(_inflight_guard);`，显式保证 guard 活到函数最后一行（否则 Rust 的 drop 顺序可能在 pump 完成前就释放它）。

网络传输耗时（[push_handler.rs:617-620](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/ingress/push_handler.rs#L617-L620)）：用 worker 收到时的墙钟 `t2` 减去信封里的 `frontend_send_ts_ns`（t1），观测到 `WORK_HANDLER_NETWORK_TRANSIT_SECONDS`。注意它依赖两台机器的 NTP 同步，跨机只当近似值看。

回拨（[push_handler.rs:625-638](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/ingress/push_handler.rs#L625-L638)）：`tcp::client::TcpClient::create_response_stream(request.context(), response_connection_info, ...)`——这一行就是「worker 主动连回客户端」的物化。注释里留了一句历史包袱：「eventually have a handler class which will returned an abstracted object, but for now, we only support tcp here」。

prologue（[push_handler.rs:656-684](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/ingress/push_handler.rs#L656-L684)）：`segment.generate()` 成功就 `send_prologue(None)` 并观测 TTFR 指标；失败则 `send_prologue(Some(error_string))` 然后 `Err(e)?` 提前返回。**prologue 是在 `generate` 被允许返回之前就发出的**——这样客户端不会在引擎还没准备好时就一直空等。

**⑤ `pump_response_stream`：泵与错误分类**

[push_handler.rs:155-298](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/ingress/push_handler.rs#L155-L298)。这是接收端最精巧的一段，核心是一个循环加大量「分类判断」：

- 编码失败 → 计 `serialization` 错误、不再发终止帧、break。
- `publisher.send` 失败 → **按 context 状态分类**：
  - `is_stopped()` 为真 → 客户端正常收完主动断开，只打 warn 不计错；
  - 否则 → 真实故障，打 error、调 `context.stop_generating()` 止血、计 `publish_response` 错误。
- 成功且非错误帧 → 通知健康检查定时器重置（错误帧不能证明引擎健康）。
- 末尾终止帧发送失败 → 又一层分类（[push_handler.rs:267-289](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/ingress/push_handler.rs#L267-L289)）：`is_stopped() && !is_killed()` 视为对端已拆除、只打 debug；**killed 或仍 attached 都算真实错误**。

那段关于 `is_stopped() && !is_killed()` 的长注释（[push_handler.rs:256-266](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/ingress/push_handler.rs#L256-L266)）解释了为什么条件要这么窄：`is_stopped()` 的定义是 `state != Live`，所以 `kill()` 之后它**也为真**；而 TCP 读错误会触发 `kill()`，如果只判 `is_stopped()` 就会把真实连接故障静默吞掉。这是一个把「语义重叠的三态」掰开用的好例子，配套的四个单测（[push_handler.rs:893-943](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/ingress/push_handler.rs#L893-L943)）分别覆盖 Stop / Kill / ConnectionReadError / TransportOnly 四种竞态。

**⑥ worker 侧的分发：`SharedTcpServer`**

[shared_tcp_endpoint.rs:333-384](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/ingress/shared_tcp_endpoint.rs#L333-L384) 的 `handle_work_item` 是从 socket 到 handler 的最后一跳：先从 `x-frontend-send-ts-ns` 算传输耗时，再从 trace headers 建 span、提取 request-id，最后 `service_handler.handle_payload(work_item.payload, request_id)`。

按 endpoint 路由的逻辑在 [shared_tcp_endpoint.rs:641-677](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/ingress/shared_tcp_endpoint.rs#L641-L677)：解出 `request_msg.endpoint_path()`，查 `handlers` 表，查不到就 warn「No handler found for endpoint」。注册/注销则通过 [shared_tcp_endpoint.rs:516](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/ingress/shared_tcp_endpoint.rs#L516) 的 `self.handlers.insert(endpoint_path, handler)` 与 `unregister_endpoint`。

这条链路的最上游是 `EndpointConfigBuilder::start_with_registration`（[component/endpoint.rs:133-160](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/component/endpoint.rs#L133-L160)），它把 handler 注册到进程级 `request_plane_server()`，并构造带 TCP 地址的 `TransportType` 写进发现面——这正好接上 u3-l2 讲的注册流程。

#### 4.3.4 代码实践

**实践目标**：不写 Rust，用 worker 进程自带的 Prometheus 指标统计 10 条请求的平均 payload 大小，做成可复用的小工具。

**原理**：`handle_payload_shared` 里那行 `m.request_bytes.inc_by(payload.len() as u64)` 已经把每条请求的信封字节数累加进了 `dynamo_component_request_bytes_total`，同时 `request_counter.inc()` 累加 `dynamo_component_requests_total`。**你不需要加任何日志或中间层——指标已经在那里了。**

平均 payload 大小：

\[ \bar{B} = \frac{\Delta\,\texttt{request\_bytes\_total}}{\Delta\,\texttt{requests\_total}} \]

**操作步骤**：

1. 给 worker 进程开启状态服务器（默认关闭，[config.rs:104-111](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/config.rs#L104-L111) 说明 `-1` 为禁用）：

   ```bash
   cd examples/custom_backend/hello_world
   DYN_DISCOVERY_BACKEND=file DYN_EVENT_PLANE=zmq \
   DYN_SYSTEM_PORT=8099 \
     python hello_world.py
   ```

2. 验证指标端点：

   ```bash
   curl -s http://127.0.0.1:8099/metrics | grep -E 'dynamo_component_(requests|request_bytes)_total'
   ```

   应能看到形如 `dynamo_component_requests_total{dynamo_namespace="hello_world",...} 0` 的行（带自动注入的 namespace/component/endpoint 标签）。

3. 另开终端跑 client。原始 `client.py` 是无限循环，先用 `timeout` 截断，或直接写个 10 次的驱动脚本（**示例代码**，保存为 `bench.py` 放在 hello_world 目录外自行运行）：

   ```python
   import asyncio, uvloop
   from dynamo.runtime import DistributedRuntime, dynamo_worker

   @dynamo_worker()
   async def worker(runtime: DistributedRuntime):
       endpoint = runtime.endpoint("hello_world.backend.generate")
       client = await endpoint.client()
       await client.wait_for_instances()
       for i in range(10):
           stream = await client.generate("world,sun,moon,star")
           async for _ in stream:
               pass
           print(f"request {i+1}/10 done")

   if __name__ == "__main__":
       uvloop.install(); asyncio.run(worker())
   ```

4. 跑完后再抓一次指标，算差值。

**需要观察的现象**：`requests_total` 增加 10；`request_bytes_total` 增加一个正数。把两者的增量相除得到平均信封字节数。

**预期结果**：平均字节数大约在几百字节量级（控制头 JSON 一两百字节 + 数据体几十字节），且把 `generate(...)` 的参数字符串加倍后重跑，平均值会明显上升——与 4.1.4 的 trace 日志观察互相印证。

**注意事项**：
- `DYN_SYSTEM_PORT` 必须在**启动时**设置，运行中无法开启。
- 若 8099 被占用换一个端口；`DYN_SYSTEM_HOST` 默认绑定本机。
- 本实践未实际运行，具体指标标签集合与数值以本地输出为准（「待本地验证」）。若 `/metrics` 里找不到这两个名字，用 `curl -s .../metrics | grep dynamo_component` 先看有哪些可用指标。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `PushWorkHandler::handle_payload` 返回 `Result<(), PipelineError>` 而不是返回响应？

**答案**：因为响应走的是与请求完全独立的旁路。worker 在 `handle_payload` 内部自己回拨客户端拿到 `StreamSender`，然后由 `pump_response_stream` 把生成结果逐帧推上去。返回值只表达「这次处理有没有在框架层出错」，不承载业务数据。这样设计的直接收益是：请求通道收到 ACK 即可复用，响应可以做长生命周期流式传输，且背压经由响应通道的 mpsc 缓冲自然传导。

**练习 2**：客户端已经收完全部响应并主动断开，worker 发终止帧 `complete_final: true` 时 `publisher.send` 失败。这个失败会被计入 `errors_total` 吗？

**答案**：不会，前提是 context 处于 `Stopped` 且**未**被 `Killed`。源码条件是 `context.is_stopped() && !context.is_killed()`，此时只打 debug 日志「client already torn down」。但如果 context 被 `kill()` 过（比如 TCP 读错误触发的硬取消），或者 context 仍为 Live（真实传输故障），同样的失败**会**计入 `publish_final` 错误。窄条件的理由：`is_stopped()` 的语义是 `state != Live`，`kill()` 也会让它为真，只判它会把真实连接故障静默吞掉。

**练习 3**：`DEFAULT_SEND_BUFFER_COUNT` 是 64，这个数字控制什么？调大调小各有什么代价？

**答案**：它控制单个流的「socket 任务」与「引擎消费者/生产者」之间 mpsc channel 的缓冲帧数（[network.rs:423-426](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network.rs#L423-L426) 的注释说明这是保留了 TCP 传输历史上硬编码的值）。缓冲满时 `send` 挂起，形成背压。调大：吞吐更平滑、更能吸收消费者抖动，但每流内存占用上升、取消时可能多泵几帧才感知；调小：背压来得更快、取消传播更及时，但高吞吐下容易频繁挂起。它通过 `StreamOptions::send_buffer_count` 配置（[network.rs:449-453](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network.rs#L449-L453)）。

---

### 4.4 `RouterMode`：七种路由模式与占用记账

#### 4.4.1 概念说明

`RouterMode` 回答「选谁」这个问题。它定义在 [push_router.rs:187-199](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/push_router.rs#L187-L199)：

```rust
pub enum RouterMode {
    RoundRobin,
    Random,
    PowerOfTwoChoices,
    KV,
    Direct,
    LeastLoaded,
    DeviceAwareWeighted,
}
```

七种模式可以按三个维度分组记忆：

| 维度 | 模式 | 特征 |
|------|------|------|
| **无状态轮询** | RoundRobin、Random | 不看负载，纯轮转/随机；有独立 picker，无占用记账 |
| **负载感知** | PowerOfTwoChoices、LeastLoaded、DeviceAwareWeighted | 需要占用计数（`requires_occupancy() == true`），选择与记账耦合 |
| **外部决策** | KV、Direct | `PushRouter` 不做选择——`route_policy()` 返回 `None`，`picker` 字段为 `None` |

第三组最容易被误解。源码在 `router_mode` 字段的注释里写得很直白：

> Setting this to KV means we never intend to call `generate` on this PushRouter. We are not using it as an AsyncEngine. Instead we will decide whether to call random/round_robin/direct ourselves and call them directly. dynamo-llm's KV Routing does this.

也就是说，KV 感知路由（u6 的主角）会**绕过** `PushRouter::generate`，自己算出 best worker id 之后调 `direct()` 精确投递。`Direct` 同理，由上游 `RoutingHost` 指定目标。

而「外部决策」这组又分两种 subtly 不同的入口：

| 入口 | 过载检查 | 语义 |
|------|---------|------|
| `direct()` / `dispatch_exact()` | **查**（`OverloadCheck::Required`） | 我只是指定了目标，没做过负载准入，发送前还要复核一遍共享过载集合 |
| `dispatch_kv_admitted()`（本次新增） | **不查**（`OverloadCheck::AlreadyAdmitted`） | KV 选点阶段已经做过载准入，且准入可能刚把本请求自己的负载同步发布出去，再查就会误拒自己 |

#### 4.4.2 核心流程

负载感知三模式的「选择 + 记账」一体化流程：

```
least_loaded / power_of_two_choices / device_aware_weighted
  ├─ occupancy_state.select_and_admit(picker, candidates, ctx)
  │     ├─ 选出 worker_id
  │     └─ 原子地 +1 占用计数，返回 counter        ← 选择与记账是原子的
  ├─ OccupancyPermit::from_counter(state, id, counter) ← RAII 许可证
  └─ dispatch_selected(instance_id, request, Some(permit), prepare)
        └─ permit.into_tracked_stream(stream)
              └─ OccupancyTrackedStream 包装响应流
                    ├─ poll_next 返回 Ready(None)（正常结束） → release()
                    ├─ poll_next 返回 Ready(Some(item)) 且 item 有错 → release()
                    └─ Drop → release()
```

**为什么选择和计数必须原子？** 如果先选再记，两个并发请求可能都选中同一个「当前最闲」的 worker，等计数落地时它已经不闲了——经典的 check-then-act 竞态。`select_and_admit` 把两步合成一步。

**为什么用 RAII 许可证而不是手动减？** 因为响应流的生命周期横跨整个生成过程，中间任何一条路径（正常结束、出错、客户端取消、连接断开）都必须释放占用。`OccupancyTrackedStream` 的 `Drop` 实现保证「忘了减」在 Rust 里根本写不出来。

P2C（power-of-two-choices）的直觉：随机抽两个候选，选在途数更少的那个。它不需要全局扫描，却能用 \(O(1)\) 的代价获得接近 least-loaded 的效果——这是负载均衡领域的经典结论。

发送前过载检查的两种模式（本次重构后的统一入口）：

```
generate_with_fault_detection_prepared_inner(instance, request, fallback, overload_check, prepare)
  ├─ resolve_transport(instance, fallback)          ← 总是执行：发现面解析 + 消失时按 fallback 重选
  ├─ if overload_check == Required:
  │     check_workers_available(instance)           ← 复核共享 client 过载集合
  │       └─ is_overloaded(instance) → WorkerOverloaded 错误
  ├─ prepare(&mut request, instance)
  └─ addressed.generate(...) → wrap_with_fault_detection(stream)  ← 总是执行：响应流故障检测
```

`OverloadCheck` 枚举只有两个值（[push_router.rs:208-212](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/push_router.rs#L208-L212)）：`Required`（要查）与 `AlreadyAdmitted`（已准入，跳过）。

#### 4.4.3 源码精读

**① 模式的能力声明**

[push_router.rs:276-301](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/push_router.rs#L276-L301) 集中了四个声明式方法：

```rust
pub fn is_kv_routing(&self) -> bool { *self == RouterMode::KV }
pub fn is_direct_routing(&self) -> bool { *self == RouterMode::Direct }

/// Whether this mode admits requests against host-owned occupancy counters.
pub const fn requires_occupancy(self) -> bool {
    matches!(
        self,
        Self::PowerOfTwoChoices | Self::LeastLoaded | Self::DeviceAwareWeighted
    )
}

fn route_policy(self) -> Option<RoutePolicy> {
    match self {
        ...
        Self::KV | Self::Direct => None,
    }
}
```

`telemetry_label`（[push_router.rs:264-274](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/push_router.rs#L264-L274)）给每种模式一个稳定的指标标签（如 `"round-robin"`、`"power-of-two-choices"`），有专门单测锁死这些字符串，防止误改导致指标断代。

**② 构造时的三份 picker**

[push_router.rs:304-315](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/push_router.rs#L304-L315)：

```rust
fn route_pickers(router_mode: RouterMode)
    -> (Arc<RoutePicker>, Arc<RoutePicker>, Option<Arc<RoutePicker>>)
{
    let round_robin = Arc::new(RoutePicker::new(RoutePolicy::RoundRobin));
    let random = Arc::new(RoutePicker::new(RoutePolicy::Random));
    let configured = match router_mode { ... };
    (round_robin, random, configured)
}
```

为什么无论配置什么模式都要建 round_robin 和 random 两份？因为 KV 模式的持有者仍可能显式调 `router.round_robin(...)` 或 `router.random(...)`（KV 未命中时的降级路径）。这样「配置的模式」与「随时可用的静态策略」互不干扰，各自的游标状态独立演进——配套单测 [push_router.rs:2238-2257](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/push_router.rs#L2238-L2257) 验证了 random 选过之后 round_robin 的游标不受影响。

**③ `AsyncEngine` 分发**

[push_router.rs:1975-1991](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/push_router.rs#L1975-L1991)：`PushRouter` 自己也实现了 `AsyncEngine<SingleIn<T>, ManyOut<U>, Error>`，`generate` 里按模式分发到 `random` / `round_robin` / `power_of_two_choices` / `least_loaded` / `device_aware_weighted`，而对 KV 和 Direct 直接 `anyhow::bail!`。**注意 bail 是运行时错误而不是编译期约束**——这就是为什么 `router_mode` 字段注释要反复强调「我们从不打算在 KV 模式下调 generate」。

**④ 故障检测与隔离：四个入口汇聚到一个 `_inner`**

本次 #13861 把原来的两个发送函数重构成了一条四层调用链，全部落在 [push_router.rs:1654-1752](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/push_router.rs#L1654-L1752)：

| 函数 | 行号 | 职责 |
|------|------|------|
| `generate_with_fault_detection` | [1654-1667](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/push_router.rs#L1654-L1667) | 无 prepare 的薄包装，固定 `OverloadCheck::Required` |
| `generate_with_fault_detection_inner` | [1669-1685](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/push_router.rs#L1669-L1685) | 加上 `overload_check` 参数的转发层 |
| `generate_with_fault_detection_prepared` | [1687-1705](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/push_router.rs#L1687-L1705) | 带 prepare 回调的公开入口，固定 `Required` |
| `generate_with_fault_detection_prepared_inner` | [1707-1752](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/push_router.rs#L1707-L1752) | **真正的实现** |

唯一的实现体里，过载检查被一行 `if` 守住了（[push_router.rs:1733-1735](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/push_router.rs#L1733-L1735)）：

```rust
let (instance_id, address, transport_kind, instance) =
    self.resolve_transport(instance_id, fallback)?;
if matches!(overload_check, OverloadCheck::Required) {
    self.check_workers_available(instance_id, &request_id)?;
}
```

注意被跳过的**只有** `check_workers_available`：`resolve_transport`（发现面还在不在）和后面的 `wrap_with_fault_detection`（响应流故障检测）对 KV 准入直发照常生效。也就是说这个优化砍掉的是「重复的过载复核」，不是安全网。

`check_workers_available`（[push_router.rs:1758-1795](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/push_router.rs#L1758-L1795)）查的是 `routing_instances.is_overloaded(instance_id)`——**共享的 client 级过载集合**。命中就构造 `ErrorType::WorkerOverloaded`（注意：这是背压语义，不是故障语义）。

`wrap_with_fault_detection`（[push_router.rs:1881-1966](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/push_router.rs#L1881-L1966)）把响应流再包两层：

- **错误分类层**：`is_inhibited`（[push_router.rs:39-52](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/push_router.rs#L39-L52)）列出的错误类型会触发 `report_instance_down`（把 worker 从本进程的可路由集合里摘掉）；`WorkerOverloaded` 则走 `mark_overloaded_immediate`（背压路径，**不是**故障路径，等 worker 的下一次负载事件来刷新）。
- **不活跃超时层**：若设置了 `DYN_HTTP_BACKEND_STREAM_TIMEOUT_SECS`，用 `tokio::select! { biased; ... }` 包住流，超时产出合成 `ResponseTimeout` 并隔离 worker。

`is_inhibited` 里 `StreamIncomplete` 那条注释值得单独读：流中途断掉意味着 worker 丢了这个请求，必须隔离，否则迁移重试可能在发现面摘除生效前**又选回同一个死 worker**。

**⑤ 新增：`dispatch_kv_admitted`——KV 准入后的免复查直发**

这是本次更新（#13861「own load state per routing context」）落在请求面上的核心改动，[push_router.rs:1159-1180](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/push_router.rs#L1159-L1180)：

```rust
/// Dispatch exactly to a worker whose KV selection step already performed
/// overload admission.
///
/// Discovery and fault detection are still enforced. The shared client
/// overload state is not rechecked because admission may synchronously
/// publish this request's own load before dispatch begins.
pub async fn dispatch_kv_admitted(
    &self,
    request: SingleIn<T>,
    instance_id: u64,
) -> anyhow::Result<ManyOut<U>> {
    if !self.router_mode.is_kv_routing() {
        anyhow::bail!("admitted dispatch is only valid in KV routing mode");
    }
    self.generate_with_fault_detection_inner(
        instance_id,
        request,
        TransportFallback::Deny,
        OverloadCheck::AlreadyAdmitted,
    )
    .await
}
```

三个值得咀嚼的设计点：

1. **为什么必须跳过复查？** 注释给出了答案：准入（admission）可能在 dispatch 开始前**同步发布本请求自身的负载**。想象这个时序——KV 路由器在选点时判断「worker A 还能接」，于是把这条请求计入 A 的负载并立刻发布出去；如果随后的 `check_workers_available` 再去读共享过载集合，看到的正是刚被自己这条请求推高的数字，可能立刻判 A 过载并拒绝。**路由器会被自己刚记的账拒之门外。** 这不是理论风险，配套单测就是专门为它写的（见 ⑦）。
2. **守卫很严**：`router_mode` 不是 KV 就直接 `bail!`。因为这个入口的前提是「上游做过 KV 准入」，其他模式调用它等于凭空跳过一次应有的检查。
3. **`TransportFallback::Deny`**：与 `direct()` 一致的精确目标语义——KV 选点已经定了人，不做隐式重选（重选会破坏 KV 亲和性，把请求发去一个没有对应 KV 缓存的 worker）。

调用方在 lib/llm 的 KV 路由宿主里，[lib/llm/src/kv_router/routing_host/kv.rs:197-199](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/kv_router/routing_host/kv.rs#L197-L199)：

```rust
let dispatch = self
    .inner
    .dispatch_kv_admitted(updated_request, selection.worker.worker_id);
```

这正是 4.4.1 里「外部决策」组的落地：选点由 `routing_host/kv_selection.rs` 完成，`PushRouter` 只负责把已准入的请求送到那个人手上。

**⑥ worker 消失的两种时机**

`resolve_transport`（[push_router.rs:1802-1875](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/push_router.rs#L1802-L1875)）处理「选中之后、发出之前」worker 消失；而 `spawn_instance_removal_watcher`（[push_router.rs:362-465](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/push_router.rs#L362-L465)）处理「发出之后」：它订阅发现面的增删事件，worker 被摘除时调 `dispatch.on_instance_removed(eid)` → `AddressedPushRouter::cancel_instance_streams` 取消该 instance 的所有待领取响应流注册，让阻塞在 oneshot 上的请求立刻拿到 `Disconnected` 而不是傻等。

注意 `ENDPOINT_WATCHER_ACTIVE` 这个 `OnceLock<DashMap>`：**每个 endpoint 只起一个 watcher**，跨所有 `PushRouter` 实例共享；条目在 watcher 退出时移除（`GuardRelease` 的 `Drop`），这样后来的 router 还能重新武装。注释特意说明「a leaked entry silently disables removal cancellation until process restart」——这是防泄漏的关键防线。

**⑦ 验证新行为的单测**

[push_router.rs:3508-3569](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/push_router.rs#L3508-L3569) 的 `admitted_dispatch_does_not_reject_the_load_it_just_booked` 用一个 `RecordingDispatch`（[push_router.rs:3377-3393](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/push_router.rs#L3377-L3393)，把每次 dispatch 录下来的假传输层）做了三步对比实验：

1. 手动 `client.set_overloaded_instances(&[instance_id])` 把唯一 worker 标成过载；
2. 调 `dispatch_exact` → 断言失败且错误是 `WorkerOverloaded`，且 `RecordingDispatch` **一次都没收到**（被过载闸拦在传输之前）；
3. 同样的过载状态下调 `dispatch_kv_admitted` → 断言**成功**，且 `RecordingDispatch` 收到恰好一次、目标就是那个 instance。

同一份过载集合、同一个 worker，两种入口一拒一放——这条测试就是 `OverloadCheck` 两个值的语义边界最精确的文档。

**⑧ 与 u3-l2 的衔接**

`select_untracked_worker`（[push_router.rs:762-773](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/push_router.rs#L762-L773)）里的 `self.client.routing_instances().free_ids()`，正是 u3-l2 讲过的 `discovered → routable → free` 漏斗的最后一层。`empty_free_pool_error`（[push_router.rs:731-751](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/push_router.rs#L731-L751)）区分两种空池：有 routable 但都过载 → `ResourceExhausted`（客户端该重试）；完全没有 routable → `Unavailable`。

#### 4.4.4 代码实践

**实践目标**：用现有单测验证 P2C 的核心不变量与 KV 准入直发的新语义，直观感受「选择与记账原子化」和「谁查过载、谁免复查」这两件事。

**操作步骤**：

```bash
# P2C 在负载悬殊时绝不选被压制的一方
cargo test -p dynamo-runtime --lib \
  push_router::tests::p2c_never_selects_dominated_worker

# 占用计数随流结束/出错正确释放
cargo test -p dynamo-runtime --lib \
  push_router::tests::occupancy_tracked_stream_decrements_on_completion
cargo test -p dynamo-runtime --lib \
  push_router::tests::occupancy_tracked_stream_releases_before_yielding_error

# 两个显式静态 picker 各自保持独立状态
cargo test -p dynamo-runtime --lib \
  push_router::tests::explicit_static_pickers_keep_policy_specific_state

# KV 准入直发不拒绝自己刚记的负载（本次新增）
cargo test -p dynamo-runtime --lib \
  push_router::tests::admitted_dispatch_does_not_reject_the_load_it_just_booked
```

**需要观察的现象**：全部通过。三个重点：

- `p2c_never_selects_dominated_worker`（[push_router.rs:2429-2451](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/push_router.rs#L2429-L2451)）：worker 3 的负载被刷到 100，另外两个是 0，然后做 1000 次 P2C 选择，断言 worker 3 被选中 **0 次**。
- `occupancy_tracked_stream_releases_before_yielding_error`：错误帧必须在被 yield 出去**之前**就释放占用，否则重试逻辑看到错误时计数还没减，会误判负载。
- `admitted_dispatch_does_not_reject_the_load_it_just_booked`（[push_router.rs:3508-3569](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/push_router.rs#L3508-L3569)）：同一个被手动标记过载的 worker，`dispatch_exact` 报 `WorkerOverloaded`、`dispatch_kv_admitted` 畅通。

**预期结果**：五条测试全部 PASS（待本地验证）。

**源码阅读型对照（无需运行）**：把 [push_router.rs:1146-1180](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/push_router.rs#L1146-L1180) 里紧挨着的 `dispatch_exact` 与 `dispatch_kv_admitted` 并排读，写一张两列对照表：入参、guard 条件、`TransportFallback`、`OverloadCheck`、doc 注释里各自声明的适用场景。两张表的差异只有一列——但那一列就是整个 #13861 在请求面上的足迹。

**延伸（可选，需改源码并重新编译）**：在 [push_router.rs:992](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/push_router.rs#L992) `OccupancyPermit::from_counter(...)` 之后加一行 `tracing::info!(instance_id, load = state.load(instance_id), "P2C admitted");`，重跑 4.3.4 的 hello_world 实践并把 client 的 router 模式换成 power-of-two-choices，就能在日志里看到每次准入时的实时占用。注意本仓库禁止提交此类调试改动，实验后请还原。

#### 4.4.5 小练习与答案

**练习 1**：`select_next_worker()` 在 `RouterMode::LeastLoaded` 下返回什么？为什么？想拿到「下一个会被选的 worker」该怎么办？

**答案**：返回 `None`。因为 LeastLoaded（以及 P2C、DeviceAwareWeighted）的选择必须与占用记账原子耦合，而 `select_next_worker` 是一个不带记账的「看一眼」接口——如果它返回了 id 却没 +1，调用者随后用这个 id 发请求就会绕过记账，把占用计数搞错。所以这些模式直接返回 `None`（[push_router.rs:1452-1455](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/push_router.rs#L1452-L1455)）。想拿「会被选的 worker」应该用 `peek_next_worker()`，它走 `occupancy_state.peek(...)` 只看不占。配套单测 `least_loaded_peek_returns_available_worker_select_stays_none` 精确锁定了这对语义。

**练习 2**：KV 路由模式下误调了 `PushRouter::generate` 会怎样？那 `dispatch_kv_admitted` 在非 KV 模式下调用又会怎样？

**答案**：前者 `anyhow::bail!("KV routing should not call generate on PushRouter")`，请求直接失败；后者同样 `bail!("admitted dispatch is only valid in KV routing mode")`。两个守卫都是运行时而非编译错误，因为 `RouterMode` 是**运行时配置**（来自 CLI/环境变量/DGD），同一个二进制可能被配置成任何模式；Rust 的类型系统无法在编译期区分「这个 `PushRouter` 是 KV 模式构造的」。`dispatch_kv_admitted` 的守卫尤其重要：它的语义前提是「上游已做过载准入」，非 KV 模式下调用它等于凭空跳过一次本应执行的 `check_workers_available`。

**练习 3**：`dispatch_kv_admitted` 跳过了 `check_workers_available`，它跳过了哪些别的检查吗？如果那个 worker 在选点后、发送前从发现面消失了会怎样？

**答案**：只跳过了过载复查这一项。`resolve_transport`（发现面还在不在、要不要按 fallback 重选）和 `wrap_with_fault_detection`（响应流的报 down / 报过载 / 迁移 / 超时）都照常执行——而且它用的是 `TransportFallback::Deny`，所以 worker 消失时会直接得到 `ErrorType::CannotConnect`（精确目标语义，不做隐式重选，因为重选会破坏 KV 亲和性），错误类型仍然落在 `StreamingDispatch` 的契约集合里，迁移逻辑照常工作。免掉的只是「重复读一次共享过载集合」这一次内存读与潜在的自误拒。

**练习 4**：`DeviceAwareWeighted` 模式解决什么问题？`DYN_ENCODER_CUDA_TO_CPU_RATIO` 默认 8 意味着什么？

**答案**：解决异构 worker（CPU 编码器 + GPU 加速器）混部时的放置问题。多模态请求的视觉编码部分可以跑在 CPU 上，但 CPU 慢、GPU 快，不能均摊。这个比例表示「1 个非 CPU worker 的预算相当于 8 个 CPU worker」，即默认倾向把编码负载压向 CPU、把 GPU 留给计算密集部分（[push_router.rs:1342-1346](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/push_router.rs#L1342-L1346)）。它还能结合 `MultimodalCacheIndex` 判断「这个 worker 是否已缓存了本请求需要的 embedding」——**完整命中**可绕过加权记账（因为无需再编码），部分命中仍按比例走（[push_router.rs:1296-1305](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/push_router.rs#L1296-L1305) 注释）。如果只剩一种设备类型，它自然退化成 least-loaded。

---

### 4.5 `FirstResponseGuard`：保留派发与前端资源保活

#### 4.5.1 概念说明

本节讲 #12004（「vLLM 前端图像解码 + E/P/D 分离」）落在**请求面**上的那一半改动。先说清楚它要解决的问题。

**问题的起点：图像像素放在前端的内存里，读的人在另一个进程。** 开启前端解码后，Rust 前端自己把图像解码成像素，放进一块 **NIXL 注册内存**，然后只把一个可序列化的描述符 `RdmaMediaDataDescriptor` 放进请求发出去——编码 worker 拿着描述符经 RDMA/NVLink **直接读前端那块内存**，像素不随请求复制。看 [lib/llm/src/preprocessor/media/rdma.rs:39-59](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/rdma.rs#L39-L59)，这个结构体大部分字段都要上线（`nixl_metadata`、`nixl_descriptor`、`content_hash`），唯独一个字段被 `#[serde(skip)]`：

```rust
// reference to the actual data, kept alive while the rdma descriptor is alive
#[serde(skip, default)]
pub(crate) source_storage: Option<Arc<nixl::NixlRegistered<SystemStorage>>>,
```

这个不上线的 `Arc` 就是**保活句柄**：它活着，NIXL 注册就有效；它被 drop，注册被撤销，远端再按描述符去读就是读一块已注销的内存。所以问题变成——**谁持有这个 `Arc`，持有多久？**

**为什么常规请求面给不出正确答案。** 回看 4.1 的时序图：`dispatch_and_finalize` 嵌在调用方的 future 里，调用方被取消或超时，future 连同所有 RAII 资源一起被 drop。更糟的是这条链路中间还有一个「交接点」：`migration.rs` 构造完请求就调 `next_generate.generate(request)`，随后 `prefill_router` 会把请求**拆成两半**、把 prefill 半边 spawn 到后台（[prefill_router/mod.rs:385](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/kv_router/prefill_router/mod.rs#L385) 的注释「spawn prefill in background and proceed to decode immediately」）。前端的调用栈很快就不持有任何指向那块内存的东西了——而编码 worker 还没来得及读。

**解法：把「保活义务」变成一个可以在 context 里传递、且只能被消费一次的守卫。** 这就是 `FirstResponseGuard`。它被 attach 进请求的 context，随请求穿过路由层，最终由请求面的**最后一跳** `AddressedPushRouter` 取出并执行「保留派发」（retained dispatch）——把派发动作移出调用方的 future、放进一个独立 spawn 的任务，让守卫活到目标 worker 产出**首个响应项**为止。

三个关键词的对应关系：

| 术语 | 含义 | 源码落点 |
|------|------|---------|
| 守卫（guard） | 一个类型擦除的 RAII 句柄 `Arc<dyn Any + Send + Sync>`，实际装着 `Vec<Arc<NixlRegistered<SystemStorage>>>` | `EngineContextGuard`，[engine.rs:94](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/engine.rs#L94) |
| 保留派发（retained dispatch） | 派发与首响应被「保留」在独立任务里，不受调用方取消影响；尾流交还调用方 | `dispatch_with_first_response_guard` |
| 首响应（first response） | worker 响应流的第一个 item——释放守卫的时点 | `response.next().await` 之后 |

#### 4.5.2 核心流程

守卫从诞生到释放的完整链路：

```
① 生产（frontend 引擎层，lib/llm/src/migration.rs:415-429）
     请求携带 MultimodalData::Decoded(descriptor)
       └─ 遍历 multi_modal_data，filter_map 出每个 descriptor.source_storage
       └─ 非空 → attach_first_response_guard(&mut request, Arc::new(source_guards))
             └─ context.insert(KEY, FirstResponseGuard::new(guard))

② 传播（路由层，lib/llm/src/kv_router/prefill_router/mod.rs:391-400）
     prefill_router 把请求拆成 prefill / decode 两半时
       └─ propagate_first_response_guard(&context, &mut prefill_context)
             └─ 把同一个 Arc<Mutex<Option<..>>> 克隆进派生 context（take-once 共享）

③ 消费（请求面最后一跳，addressed_router.rs:888-922）
     AsyncEngine::generate 入口
       └─ guard.take()                      ← 全链路只有一次成功
           ├─ None → 常规 dispatch_and_finalize（嵌在调用方 future 里）
           └─ Some(guard)
               ├─ try_acquire_retained_dispatch_permit(&RETAINED_.._PERMITS)
               │     └─ 1024 满 → ErrorType::ResourceExhausted
               ├─ tokio::spawn(独立任务)
               │     ├─ dispatch.await → 出错：oneshot 发 Err，任务退出时 drop 守卫
               │     ├─ 构造"占位流"，oneshot 先交还调用方   ← 调用方不干等
               │     ├─ response.next().await               ← 等首个响应项
               │     ├─ drop(guard)     ← 此刻才释放 NIXL 注册内存
               │     ├─ drop(permit)    ← 此刻才归还信号量
               │     └─ first_tx.send((first, response))    ← 首项 + 尾流交给占位流
               └─ 调用方拿到占位流，正常 poll / 取消（取消只影响尾流）
```

**为什么释放点是「首个响应项」，而不是「请求发出」或「流结束」？**

- **「请求发出」太早。** 信封送达 ≠ worker 已经读完内存。worker 从收到信封到真正发起远端读之间还有排队、解码元数据、建流等一串步骤。
- **「流结束」太晚。** 图像像素只在**编码**阶段被读一次；编码完成后进入 prefill/decode 的长生成阶段，那块内存再也不需要。等到流结束才释放，等于让一块可能很大的注册内存（一张高分辨率图的 RGB 字节）陪跑整个生成过程，还白占一个保留派发名额。
- **「首个响应项」恰好。** worker 侧的实现约定是：在开始任何昂贵工作（包括读远端内存）**之前**先产出首响应项。首项一到，读已发生，释放安全。

**为什么必须有 1024 上限？** 因为保留派发**故意摆脱了调用方的取消约束**，代价就是它也可能摆脱一切超时。源码注释写得很直白（[addressed_router.rs:51-52](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/addressed_router.rs#L51-L52)）：

> A timeout cannot safely release registered memory while a remote read may still be active.

也就是说，**不能**给这个阶段加超时来自动释放——超时触发时远端读可能还在进行，释放注册内存恰恰制造它想避免的事故。既然时间维度不能兜底，就只能从**数量维度**兜底：进程级最多 1024 个并发保留派发，超出直接报 `ResourceExhausted`，让上游的准入/扩缩逻辑感知到并做出反应，而不是无限堆积。

#### 4.5.3 源码精读

**① 上下文键、上限与进程级信号量**

[addressed_router.rs:50-55](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/addressed_router.rs#L50-L55)：

```rust
const FIRST_RESPONSE_GUARD_CONTEXT_KEY: &str = "dynamo.request_plane.first_response_guard";
// A timeout cannot safely release registered memory while a remote read may
// still be active. Bound the detached pre-first-response phase process-wide.
const MAX_RETAINED_FIRST_RESPONSE_DISPATCHES: usize = 1024;
static RETAINED_FIRST_RESPONSE_DISPATCH_PERMITS: LazyLock<Arc<Semaphore>> =
    LazyLock::new(|| Arc::new(Semaphore::new(MAX_RETAINED_FIRST_RESPONSE_DISPATCHES)));
```

三个细节：键是一个字符串常量，走的是 `Context` 的类型化插入（按类型 `FirstResponseGuard` 取回，键只是命名空间）；上限是 `LazyLock` 的**进程级**静态量，跨所有 `PushRouter`/endpoint 共享——也就是说 1024 是整个 frontend 进程的全局预算，不是每个 endpoint 1024 个。

**② `FirstResponseGuard` 的 take-once 语义**

[addressed_router.rs:57-72](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/addressed_router.rs#L57-L72)：

```rust
#[derive(Clone)]
struct FirstResponseGuard {
    guard: Arc<Mutex<Option<EngineContextGuard>>>,
}

impl FirstResponseGuard {
    fn take(&self) -> Option<EngineContextGuard> {
        self.guard.lock().take()
    }
}
```

外面一层 `Arc<Mutex<Option<...>>>` 是为了**跨 context 共享同一份一次性所有权**。`propagate` 把它克隆进派生 context 时克隆的是这个外层 `Arc`——所以源 context 和派生 context 看到的是**同一个** `Option`，谁先 `take()` 谁拿到守卫，另一个拿到 `None`。这保证一条逻辑请求无论被路由层拆分、复制出多少个派生 context，保活义务只被执行一次。配套单测 `propagated_first_response_guard_is_taken_once`（[addressed_router.rs:1125-1147](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/addressed_router.rs#L1125-L1147)）断言的正是「derived 拿到之后 source 再 take 是 `None`」。

**③ attach / propagate 与再导出**

[addressed_router.rs:74-98](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/addressed_router.rs#L74-L98)：

```rust
/// Keep a frontend-owned resource alive until the addressed worker produces
/// its first response item or closes the response stream.
pub fn attach_first_response_guard<T: Data>(
    context: &mut context::Context<T>,
    guard: EngineContextGuard,
) { ... }

/// Share a take-once first-response guard with a derived request context.
pub fn propagate_first_response_guard<S: Data, T: Data>(
    source: &context::Context<S>,
    target: &mut context::Context<T>,
) -> Result<(), Error> { ... }
```

注意 `attach` 收的类型是 `EngineContextGuard`——就是 [engine.rs:94](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/engine.rs#L94) 的 `pub type EngineContextGuard = Arc<dyn Any + Send + Sync>`。请求面**不知道也不需要知道**里面装的是 NIXL 注册内存还是别的什么资源；它只知道「这是个必须活到首响应的 RAII 句柄」。这种类型擦除让机制对任何前端持有资源通用。

两个函数都从 [pipeline.rs:21-24](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline.rs#L21-L24) 再导出，lib/llm 通过 `dynamo_runtime::pipeline::{attach_first_response_guard, propagate_first_response_guard}` 使用——这就是把「请求面机制」与「LLM 域用法」解开的接缝。

**④ 许可获取与错误类型**

[addressed_router.rs:100-110](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/addressed_router.rs#L100-L110) 用 `try_acquire_owned`（**非**阻塞）：拿不到立刻构造 `ErrorType::ResourceExhausted`，message 是 `"retained request dispatch limit reached"`。这里用 `try_` 而不是 `acquire().await` 是有意的——如果在这里排队等许可，等于把「保留派发不受调用方约束」的初衷又交还给了调用方，调用方取消时等待中的派发一样会被砍。

`ResourceExhausted` 这个选择也不是随手定的：它正好落在 4.1 讲过的 `StreamingDispatch` 契约集合里，`wrap_with_fault_detection` 与上层迁移逻辑认识它；同时 4.4 讲过 `empty_free_pool_error` 在「有 routable 但都过载」时也报 `ResourceExhausted`——同一语义（「现在真没有容量了，上层请重试/扩容」）复用同一类型。

**⑤ 保留派发的主体**

[addressed_router.rs:112-168](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/addressed_router.rs#L112-L168) 的 `dispatch_with_first_response_guard` 是本节的核心。开头注释一句话讲清了边界：

```rust
// Only dispatch and the first response are detached from the caller. The tail
// is handed back so normal stream polling and cancellation stay on the caller.
```

**只**把「派发 + 首响应」从调用方剥离；尾流交还，正常的流轮询与取消仍归调用方管。函数体分四步：

1. `tokio::spawn` 一个独立任务，把 `dispatch` future、`guard`、`permit` 三者 move 进去——从这一刻起它们的生命周期与调用方脱钩。
2. 任务里 `dispatch.await`，失败则经 oneshot 把错误发回调用方并 return（此时 `guard`/`permit` 随任务结束被 drop，资源正确释放）。
3. 成功则**先**构造一个「占位流」（`async_stream::stream!` 里 `first_rx.await`），经 `dispatch_tx.send(Ok(handoff))` 交还调用方——注意这一步在等首响应**之前**，所以调用方立刻拿到流对象，不必同步阻塞。
4. **然后**才 `response.next().await` 等首响应项，拿到后依次 `drop(guard)`、`drop(permit)`，最后 `first_tx.send((first, response))` 把首项和尾流一起交给占位流继续产出。

如果占位流的消费者把流 drop 了（调用方取消），`first_rx` 会被 drop，`async_stream` 分支走 `Err(_)`，产出一个合成错误 `retained request dispatch ended before first response handoff`——但独立任务**不会**被连带取消，它仍会等完首响应、正确释放守卫与许可。这正是「保留」的含义。

**⑥ 消费点：`AsyncEngine::generate` 的分支**

[addressed_router.rs:888-922](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/addressed_router.rs#L888-L922)。`AddressedPushRouter` 对 `SingleIn<AddressedRequest<T>>` 的 `AsyncEngine` 实现现在是这个形状：

```rust
async fn generate(&self, request: SingleIn<AddressedRequest<T>>) -> Result<ManyOut<U>, Error> {
    let (addressed_request, context) = request.transfer(());
    let (request, address, instance_info) = addressed_request.into_parts();

    let first_response_guard = context
        .get_optional::<FirstResponseGuard>(FIRST_RESPONSE_GUARD_CONTEXT_KEY)
        .map_err(Error::msg)?;

    if let Some(guard) = first_response_guard.and_then(|guard| guard.take()) {
        let permit =
            try_acquire_retained_dispatch_permit(&RETAINED_FIRST_RESPONSE_DISPATCH_PERMITS)?;
        let router = self.clone();
        let dispatch = async move {
            router.dispatch_and_finalize::<T, U>(&context, address, instance_info.as_ref(), Some(&request), None).await
        };
        return dispatch_with_first_response_guard(dispatch, guard, permit).await;
    }

    self.dispatch_and_finalize::<T, U>(&context, address, instance_info.as_ref(), Some(&request), None).await
}
```

值得注意三点。**其一**，分支判断的依据是 `guard.take()` 返回 `Some` 与否，而不是「请求里有没有解码媒体」——请求面完全不感知多模态，它只认守卫，职责边界干净。**其二**，`let router = self.clone()` 正是 4.1 里那个新加的 `#[derive(Clone)]` 的用武之地：dispatch future 要 `'static` 才能 spawn，router 必须被搬进去。**其三**，守卫分支只覆盖 **unary** 路径（`dispatch_bidirectional` 不走这里）——因为解码媒体只出现在 unary 请求里，双向流式请求没有「一次性携带媒体」的语义。

**⑦ 生产方：frontend 引擎把句柄装进请求**

[lib/llm/src/migration.rs:415-429](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/migration.rs#L415-L429)：

```rust
let source_guards = self
    .request
    .multi_modal_data
    .as_ref()
    .into_iter()
    .flat_map(|media| media.values())
    .flatten()
    .filter_map(|item| match item {
        MultimodalData::Decoded(descriptor) => descriptor.source_storage.clone(),
        _ => None,
    })
    .collect::<Vec<_>>();
if !source_guards.is_empty() {
    attach_first_response_guard(&mut request, Arc::new(source_guards));
}
```

位置很讲究：它在 `next_generate.generate(request)`（[migration.rs:430](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/migration.rs#L430)）**之前**执行——也就是在请求离开 frontend 引擎、进入路由/请求面的最后一刻装上守卫。`filter_map` 只认 `MultimodalData::Decoded` 变体，`Url`/`RawUrl` 两种线格式返回 `None`（它们没有前端注册内存，无需保活）；一条请求带多张图时收集成 `Vec`，整体作为一个 guard attach。`Arc::new(source_guards)` 又套了一层 `Arc`，把它变成 `Arc<dyn Any>` 所需的类型擦除形态。

**⑧ 传播方：prefill 路由器把守卫带进派生 context**

[lib/llm/src/kv_router/prefill_router/mod.rs:391-400](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/kv_router/prefill_router/mod.rs#L391-L400)：

```rust
let tracker = prefill_req.tracker.clone();
let mut prefill_context =
    Context::with_id_and_metadata(prefill_req, request_id.clone(), metadata.clone());
propagate_first_response_guard(&context, &mut prefill_context)?;
```

没有这一行，守卫就断在拆分点：prefill_router 用 `Context::with_id_and_metadata` **新建**了一个 context（只搬运 id 与 metadata），原 context 里的类型化条目默认不跟随。而这恰好是编码 worker 所在的那半边请求——E/P/D 拓扑里读前端内存的正是 prefill/encode 路径。紧挨着的 `session_affinity` 注入（[mod.rs:395-400](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/kv_router/prefill_router/mod.rs#L395-L400)）是同一个模式：**凡是新建派生 context 的地方，必须显式决定哪些条目要跟过去**。

**⑨ 三条单测锁死的关键不变量**

| 测试 | 行号 | 锁死的不变量 |
|------|------|-------------|
| `propagated_first_response_guard_is_taken_once` | [1125-1147](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/addressed_router.rs#L1125-L1147) | 守卫只被 take 一次；被 take 走的那份 drop 时触发 `DropSignal`，证明 RAII 链路通 |
| `first_response_guard_outlives_cancelled_dispatch_waiter` | [1149-1214](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/addressed_router.rs#L1149-L1214) | **调用方的 future 被 abort，守卫不死**；首响应到达后守卫才释放、许可才归还 |
| `dropping_handed_off_tail_closes_upstream` | [1215-1246](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/addressed_router.rs#L1215-L1246) | 首响应后 drop 调用方的流，会正确关闭上游尾流——「保留」不等于「泄漏」 |

第二条测试的名字就是机制的规格说明，它的实验步骤值得读一遍：spawn 一个 waiter 包着 `dispatch_with_first_response_guard`，等 dispatch 开始后 **`waiter.abort()`**，断言此刻守卫**没有**被 drop（`guard_dropped_rx.try_recv() == Err(Empty)`）且许可**没有**归还（再次 `try_acquire` 失败并报 `ResourceExhausted`）；然后放行 dispatch、发出首响应，断言守卫释放、许可归还。取消调用方 → 资源仍然活着；首响应到达 → 资源正确释放。**这两个断言合起来才是这个机制的完整定义。**

#### 4.5.4 代码实践

**实践目标**：不改源码，跑通三条新单测，并用其中一条的断言结构回答「保留派发到底保留了什么」。

**操作步骤**：

```bash
# 三条守卫单测（可以一次跑全）
cargo test -p dynamo-runtime --lib \
  addressed_router::tests::propagated_first_response_guard_is_taken_once
cargo test -p dynamo-runtime --lib \
  addressed_router::tests::first_response_guard_outlives_cancelled_dispatch_waiter
cargo test -p dynamo-runtime --lib \
  addressed_router::tests::dropping_handed_off_tail_closes_upstream

# 或者用名字过滤一次跑完
cargo test -p dynamo-runtime --lib first_response_guard
```

**需要观察的现象**：三条测试全部 PASS。然后做一个静态对照——打开 [addressed_router.rs:1149-1214](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/addressed_router.rs#L1149-L1214)，把测试里 `waiter.abort()` 之后的三条断言抄下来：

1. `assert_eq!(guard_dropped_rx.try_recv(), Err(TryRecvError::Empty));` —— 守卫未释放；
2. `let error = try_acquire_retained_dispatch_permit(&permits).unwrap_err();` + `match_error_chain(..., &[ErrorType::ResourceExhausted], ...)` —— 许可未归还，且再次获取会得到 `ResourceExhausted`；
3. `release_dispatch_tx.send(()).unwrap(); raw_tx.send(...).await.unwrap();` 之后守卫释放、许可归还 —— 释放点由**首响应**驱动。

**预期结果**：三条 PASS。如果第 2 条断言里的 `match_error_chain` 失败，说明你把许可获取看成了阻塞等待——重读 4.5.3 的 ④。此实践需本地 Rust 工具链，标注「待本地验证」。

**延伸（源码阅读型，无需运行）**：沿着守卫的传播路径做一次「断点标注」练习——在 [migration.rs:428](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/migration.rs#L428)、[prefill_router/mod.rs:394](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/kv_router/prefill_router/mod.rs#L394)、[addressed_router.rs:896](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/addressed_router.rs#L896)、[addressed_router.rs:158](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/addressed_router.rs#L158) 四处各写一行注释，说明这一步守卫「在谁手里、接下来去哪」，然后回答：如果把 `prefill_router/mod.rs:394` 这一行删掉，整条链会在哪一步失效？失效的表现是什么（提示：不是报错，而是 worker 在读一块已被注销的内存）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `dispatch_with_first_response_guard` 要在等首响应**之前**先把占位流交还调用方？如果把这两步对调会发生什么？

**答案**：因为「等首响应」可能耗时不可控（worker 排队、引擎冷启动），而调用方需要尽快拿到一个 `ManyOut<U>` 返回值去挂接自己的后续逻辑（流式转发、指标、取消注册）。对调之后，调用方的 `generate()` 调用会一直阻塞到 worker 首响应——表面上守卫还活着，实际上调用方被拖死在一个它无法取消的等待里，整个「保留派发让调用方自由」的设计就退化成了同步调用。源码里 `dispatch_tx.send(Ok(handoff))` 在 [addressed_router.rs:155](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/addressed_router.rs#L155)，`response.next().await` 在 [157](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/addressed_router.rs#L157)，顺序是刻意的。

**练习 2**：`MAX_RETAINED_FIRST_RESPONSE_DISPATCHES = 1024` 触发时返回 `ResourceExhausted`。为什么不能用「给保留阶段加个超时、超时后释放守卫」来替代这个上限？

**答案**：因为超时触发时无法区分「worker 真的死了」和「worker 只是慢、远端读正在进行」。注册内存的释放必须保证**没有任何远端读还在进行**——这正是 [addressed_router.rs:51-52](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/runtime/src/pipeline/network/egress/addressed_router.rs#L51-L52) 注释的原话「A timeout cannot safely release registered memory while a remote read may still be active」。一个错误的超时释放会制造它本来要防止的事故：worker 按描述符读一块已注销的内存。所以时间维度不可用，只能用数量维度兜底——用 `try_acquire` 立刻失败，把「容量耗尽」如实上报给能做出正确反应的上层（重试、扩容、降级）。

**练习 3**：一条请求带 3 张图、每张图一个 `source_storage`。守卫会被 attach 几次？`take()` 会成功几次？如果这条请求经过 prefill_router 拆成 prefill/decode 两半，又是几次？

**答案**：attach **1** 次。[migration.rs:415-426](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/migration.rs#L415-L426) 把三个 `source_storage` 收进**一个** `Vec`，整体作为一个 guard（`Arc::new(source_guards)`）insert 进 context——保活是「这一条请求的全部媒体」一个整体，不是每张图一个守卫。`take()` 全链路只成功 **1** 次，因为 `Mutex<Option<..>>` 的 `take()` 把 `Some` 换成 `None`，后来者只能拿到 `None`。经过 prefill_router 拆分后依旧：`propagate` 克隆的是外层 `Arc<Mutex<..>>`，源 context 与 prefill context **共享同一个** `Option`，谁先消费谁得，仍然只有 1 次。这就是 take-once 语义要解决的场景——派生 context 不管有多少个，保活义务不重复、也不遗漏。

---

## 5. 综合实践

把本讲五个模块串成一个任务：**给 hello_world 加一个「请求面观测台」，回答五个问题——一条请求的信封有多大？控制头和数据体各占多少？msgpack 比 JSON 省多少？KV 准入直发与普通直发在过载检查上差在哪？取消调用方时，保留派发到底保住了什么？**

**步骤**：

1. **铺底**：按 4.3.4 启动 worker（`DYN_SYSTEM_PORT=8099`），按 4.1.4 启动 client（`RUST_LOG=dynamo_runtime=trace`）。跑 10 条请求。

2. **总量**：从 `/metrics` 取 `dynamo_component_request_bytes_total` 与 `dynamo_component_requests_total` 的增量，算出平均信封字节数 \(\bar{B}\)。注意这个数字包含 `TwoPartMessage` 的 24 字节定长头（header_len + body_len + checksum 各 8 字节），以及外层 `TcpRequestMessage` 的协议头（2 + endpoint_path + 2 + headers JSON + 4 字节）。

3. **分解**：从 client 的 trace 日志读出 `ctrl: N bytes, data: M bytes`，得到控制头与数据体的精确拆分。对比 \(\bar{B}\) 与 \(24 + N + M\)，差值就是外层成帧开销。

4. **对照**：把 worker 的 `DYN_REQUEST_PLANE_CODEC` 分别设为 `msgpack` 和 `json` 各跑一轮（**注意要重启 worker**，因为 `OnceLock` 只读一次环境变量），对比 `data:` 字节数的差异。hello_world 的请求体是一个短字符串，差异可能不明显；可以改用 sample 后端发一条带长 system prompt 的 chat 请求让差异放大。

5. **过载语义对照**（源码阅读 + 单测，不依赖集群）：跑 4.4.4 的 `admitted_dispatch_does_not_reject_the_load_it_just_booked`，然后写一段约 200 字的说明，回答——为什么「准入可能已同步发布本请求自身负载」就必须跳过共享复查？如果把 `OverloadCheck::AlreadyAdmitted` 改成 `Required`，这个测试会在哪一步失败？

6. **守卫生命周期对照**（源码阅读 + 单测）：跑 4.5.4 的 `first_response_guard_outlives_cancelled_dispatch_waiter`，把 `waiter.abort()` 之后与首响应之后的两 组断言各抄一遍，用红笔在两组之间画一条竖线，写上「这条线就是保留派发的语义边界：线以上调用方说了不算，线以下首响应说了算」。再画一张完整的守卫流转图（①生产 → ②传播 → ③消费 → 释放），标注每一步的文件与行号。

7. **整理**：写成一页实验记录，包含拓扑图、每轮的原始读数、计算过程、结论。

**预期结论形态**：控制头大小基本恒定（由 request-id 长度与 connection_info 决定），数据体随请求内容线性增长；msgpack 对结构化数据（多字段、嵌套）的压缩收益显著，对短字符串收益很小；成帧开销是固定的几十字节常量；`dispatch_kv_admitted` 与 `dispatch_exact` 的差别只在过载闸那一行 `if`，其余安全网全部保留；保留派发保住的是「派发 + 首响应」这两步的资源存活权，尾流仍归调用方。这些结论直接决定你在生产上该不该为 payload codec 纠结——**短请求不值得，长上下文/多模态请求很值得**。

全部步骤均可在无 GPU、无 etcd/NATS 的本地环境完成（`DYN_DISCOVERY_BACKEND=file` + `DYN_EVENT_PLANE=zmq`）。所有读数标注「待本地验证」。

## 6. 本讲小结

- **请求面是两条独立的连接**：请求通道只传一帧信封、收到空 ACK 即复用；响应通道由 worker 依据信封里的 `ConnectionInfo` 主动回拨客户端预注册的流服务器。这种 call-home 模式让请求连接轻量化、响应可以长流式传输。
- **发送端三层分工**：`PushRouter` 管选谁与故障检测，`AddressedPushRouter` 管信封组装与回拨等待，`RequestPlaneClient` 管具体传输。`StreamingDispatch` trait 把后两层之间的接缝显式化，`NetworkManager` 是唯一读取网络环境变量的地方——切换 TCP/NATS 不影响上层一行代码。
- **两层编解码必须分清**：成帧层（`TcpRequestMessage` 的长度前缀、`TwoPartCodec` 的 24 字节三段头）解决字节流边界；负载层（`RequestPlanePayloadCodec` 的 Json/Msgpack）解决业务序列化。**控制头永远是 JSON**，payload codec 以目标 worker 在发现面广告的值为准，老 worker 兜底 JSON。
- **推送语义的核心是 `handle_payload` 返回 `()`**：响应走旁路。终止靠显式的三段协议——prologue（流就绪或失败）→ 数据帧 × N → `complete_final: true` 终止帧。错误分类反复使用 `is_stopped() && !is_killed()` 这种窄条件，因为 `is_stopped()` 的语义是 `state != Live`，`kill()` 也会让它为真。
- **指标不需要你自己加**：`request_bytes` / `requests_total` / `inflight_requests` / `errors_total` 已经在 `handle_payload_shared` 里通过 RAII guard 维护，开 `DYN_SYSTEM_PORT` 就能从 `/metrics` 抓到。
- **`RouterMode` 七种分三组**：无状态轮询（RoundRobin/Random）、负载感知（P2C/LeastLoaded/DeviceAwareWeighted，选择与占用记账原子耦合、用 RAII 许可证随流释放）、外部决策（KV/Direct，不经过 `generate`，由上游 `RoutingHost` 或 KV 路由器显式指定目标后调 `direct()`）。
- **本次新增 `OverloadCheck` 二值化过载闸**：发送主链路统一收敛到 `generate_with_fault_detection_prepared_inner`，普通路径（`direct`/`dispatch_exact` 等）带 `Required` 复核共享过载集合；新增的 `dispatch_kv_admitted` 带 `AlreadyAdmitted` 免复查——因为 KV 准入可能已同步发布本请求自身的负载，再查会自误拒。发现面解析与响应流故障检测两条安全网都不受影响。
- **`FirstResponseGuard` 把「保活义务」变成请求面的一等公民**：前端解码图像占用的 NIXL 注册内存由一个不上线的 `Arc` 守护，attach 进请求 context（migration.rs）、跨路由层 take-once 传播（prefill_router/mod.rs）、在最后一跳被消费（addressed_router.rs）。保留派发把「派发 + 首响应」spawn 进独立任务，使守卫活过调用方的取消；释放点选在**首个响应项**——「请求发出」太早，「流结束」太晚。因为不能安全地给这个阶段加超时（远端读可能仍在进行），改用进程级 1024 个保留派发的信号量兜底，超限报 `ResourceExhausted`。请求面全程不感知多模态，只认守卫。

## 7. 下一步学习建议

本讲讲完了「一条请求怎么在网络上传送」，接下来的自然走向有四条：

1. **u3-l5（传输层：etcd + NATS/ZMQ）**：本讲的 `RequestPlaneClient` 下面是 TCP，事件面走的是 NATS/ZMQ。那一讲讲控制面（etcd lease/lock）与事件面（PubSub）的物理载体，与本讲构成 `lib/runtime/src/transports/` 的全景。
2. **u4-l2（HttpService：OpenAI 兼容 HTTP 服务）**：本讲的 `PushRouter` 是 frontend 内部把请求送到 worker 的那一跳；u4-l2 讲 HTTP 请求怎么从外部进入 frontend，包括并发许可与背压——你会看到 `WorkerOverloaded` / `ResourceExhausted` 这些本讲出现的错误类型如何被翻译成 HTTP 状态码。
3. **u6-l2（Rust 路由核心：routing_host、负载上下文与调度）**：本讲的 `dispatch_kv_admitted` 是为谁开的口子？就是 `lib/llm/src/kv_router/routing_host/kv.rs` 里那个 KV 路由宿主。#13861 的另一半改动在那里——`routing_load.rs` 新增的 `RoutingLoadContext` 让每个路由上下文自持负载状态，学完 u6 你就能把「选点时准入 → 直发时免复查」这条线完整地画出来。
4. **u8-l9（前端图像解码与 E/P/D 多模态分离）**：本讲 4.5 节只讲了守卫机制本身——它是为谁服务的？答案在那里的端到端链路：前端解码 → 描述符注入请求 → 守卫 attach/propagate/take → 编码 worker 经 NIXL 读像素 → 嵌入缓存键。学完那一讲，你就能把 `source_storage` 这个 `#[serde(skip)]` 字段从诞生到释放的每一跳都对应到真实代码。

阅读源码时建议带着这两个问题去：`pump_response_stream` 里那四种竞态的单测为什么必须成对出现？`spawn_instance_removal_watcher` 的 `GuardRelease` 如果漏写会发生什么？这两个问题的答案能检验你是否真的理解了「取消传播」与「一次性资源」这两个贯穿 Dynamo 全局的主题——4.5 节的 `FirstResponseGuard` 正是这两个主题在多模态场景下的合流：既要让资源活过取消，又只能被消费一次。

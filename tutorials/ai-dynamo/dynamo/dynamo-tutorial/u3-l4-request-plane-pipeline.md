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
7. 不改一行源码，用现成的日志与 Prometheus 指标测出一条请求的平均 payload 大小。

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

## 3. 本讲源码地图

本讲聚焦 `lib/runtime/src/pipeline/network/` 这棵子树：

| 文件 | 作用 |
|------|------|
| [lib/runtime/src/pipeline/network.rs](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network.rs) | 请求面「公共词汇表」：`RequestPlanePayloadCodec`、`RequestControlMessage`、`StreamSender/StreamReceiver`、`StreamOptions`、`Ingress`、`PushWorkHandler`、`NetworkStreamWrapper`、`Egress` 全部定义在这里 |
| [lib/runtime/src/pipeline/network/ingress.rs](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/ingress.rs) | 接收端模块入口，只有共享的 `drain_inflight` 优雅关停辅助函数 |
| [lib/runtime/src/pipeline/network/ingress/push_handler.rs](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/ingress/push_handler.rs) | **接收端核心**：`Ingress` 如何实现 `PushWorkHandler`，解码信封、回拨客户端、泵出响应流 |
| [lib/runtime/src/pipeline/network/ingress/shared_tcp_endpoint.rs](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/ingress/shared_tcp_endpoint.rs) | worker 进程共享的 TCP 监听器：按 `endpoint_path` 分发到 handler，维护有界工作队列 |
| [lib/runtime/src/pipeline/network/ingress/push_endpoint.rs](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/ingress/push_endpoint.rs) | NATS 遗留形态的 endpoint 包装（TCP 模式下不走这里） |
| [lib/runtime/src/pipeline/network/egress.rs](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress.rs) | 发送端模块入口，只有子模块声明 |
| [lib/runtime/src/pipeline/network/egress/push_router.rs](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/push_router.rs) | **发送端核心之一**：`PushRouter` 选 worker、记账、故障检测；`RouterMode` 与本次新增的 `OverloadCheck`/`dispatch_kv_admitted` 定义于此 |
| [lib/runtime/src/pipeline/network/egress/addressed_router.rs](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/addressed_router.rs) | **发送端核心之二**：`AddressedPushRouter` 组装信封、发送、等待 worker 回拨、解码响应流 |
| [lib/runtime/src/pipeline/network/codec.rs](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/codec.rs) | TCP 请求帧 `TcpRequestMessage` 与响应 `TcpResponseMessage` 的线格式 |
| [lib/runtime/src/pipeline/network/codec/two_part.rs](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/codec/two_part.rs) | `TwoPartCodec`：把「控制头 + 数据体」切成一帧的编解码器 |
| [lib/runtime/src/pipeline/network/manager.rs](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/manager.rs) | `NetworkManager`：唯一读取网络环境变量、按 `RequestPlaneMode` 创建 server/client 的地方 |
| [lib/llm/src/kv_router/routing_host/kv.rs](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/kv_router/routing_host/kv.rs) | 本次新增 API 的**调用方**：KV 路由宿主在选点完成后用 `dispatch_kv_admitted` 直发 |

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
```

注意两个「反直觉」的点：

1. **请求通道只传一帧，然后就被复用了。** 客户端发出信封、收到一个空的 ACK，请求通道的使命就结束了。后续的所有响应帧走的是 worker 主动建立的那条**新**连接。
2. **客户端会阻塞在 `oneshot` 上等回拨。** `response_stream_provider.await()` 要等 worker 真正连回来、把 `StreamSender` 交出去才 resolve。如果 worker 在建立响应流之前就死了，`oneshot` 被 drop，客户端收到 `RecvError`，映射成可迁移的 `Disconnected` 错误。

#### 4.1.3 源码精读

**① `PushRouter` 的字段——三层职责的物证**

[lib/runtime/src/pipeline/network/egress/push_router.rs:130-159](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/push_router.rs#L130-L159) 定义了 `PushRouter` 的核心字段：`client` 负责从 etcd 拿远端 instance 信息，`router_mode` 决定选点策略，`picker` 是「共享的、与调度器无关的策略状态」。

最关键的是 `addressed` 字段——它就是那个传输接缝：

```rust
/// The final hop: after selecting an instance, `PushRouter` hands it to this
/// `StreamingDispatch` (the request-plane `AddressedPushRouter` by default).
/// A trait object so an alternate transport can swap it out.
addressed: Arc<dyn StreamingDispatch<T, U>>,
```

构造函数 [push_router.rs:623-686](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/push_router.rs#L623-L686) 里的 `let addressed = addressed_router(&client.endpoint).await?;` 一行，就是把默认的 `AddressedPushRouter` 装进这个接缝，随后立即 type-erase 成 `Arc<dyn StreamingDispatch<T, U>>`。

**② `AddressedPushRouter` 的两个半边**

[lib/runtime/src/pipeline/network/egress/addressed_router.rs:384-390](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/addressed_router.rs#L384-L390)：

```rust
pub struct AddressedPushRouter {
    // Request transport (unified trait object - works with all transports)
    req_client: Arc<dyn RequestPlaneClient>,

    // Response transport (TCP streaming - unchanged)
    resp_transport: Arc<tcp::server::TcpStreamServer>,
}
```

两个字段正好对应两条通道：`req_client` 发请求信封，`resp_transport` 是**本进程**的响应流服务器（worker 要回拨的目标）。构造入口 [addressed_router.rs:407-420](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/addressed_router.rs#L407-L420) 从 `NetworkManager` 拿 client、从 `DistributedRuntime` 拿流服务器——这印证了「响应流服务器是进程级共享的」。

**③ 发送主体 `dispatch_and_finalize`**

这是本讲最重要的一个函数，[addressed_router.rs:473-623](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/addressed_router.rs#L473-L623)。它的注释写得很清楚：wire 形态由输入推断——`input_stream = Some` 就是双向流式（header-only 信封），`input_stream = None` + `request = Some` 就是 unary（两段式信封）。

先注册流（[addressed_router.rs:499-504](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/addressed_router.rs#L499-L504)）：

```rust
let (send_registered, recv_registered) = self
    .register_streams(engine_ctx.clone(), enable_request_stream, true)
    .await?;
let recv_registered = recv_registered.ok_or_else(|| {
    anyhow::anyhow!("response stream registration missing despite enable_response_stream")
})?;
```

`register_streams`（[addressed_router.rs:631-667](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/addressed_router.rs#L631-L667)）在本地流服务器上按需注册「发送半边」和「接收半边」，返回的 `RegisteredStream` 里除了 `ConnectionInfo` 还有一个 `oneshot::Receiver`。

然后是墓碑检查（[addressed_router.rs:506-529](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/addressed_router.rs#L506-L529)）：如果发现面已经把这个 worker 摘掉了，就**在往请求面写字节之前**快速失败，返回 `Disconnected`，让上层走迁移重试，而不是发出去石沉大海。

发送（[addressed_router.rs:540-542](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/addressed_router.rs#L540-L542)）：

```rust
let tx_start = Instant::now();
let request_plane_response = self.dispatch_buffer(address, buffer, context.id()).await?;
REQUEST_PLANE_SEND_SECONDS.observe(tx_start.elapsed().as_secs_f64());
```

`dispatch_buffer`（[addressed_router.rs:676-698](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/addressed_router.rs#L676-L698)）注入三类 header：trace 上下文、`request-id`、`x-frontend-send-ts-ns`（墙钟纳秒时间戳，worker 侧用它算网络传输耗时）。返回值是请求面 ACK 字节。

接着解析 ACK（[addressed_router.rs:548-555](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/addressed_router.rs#L548-L555)）：worker 的拒绝会体现在 ACK 而不是响应流上，所以要**先**检查再等回拨，否则会一直等一个永远不会建立的响应连接。`detect_worker_rejection_response`（[addressed_router.rs:703-725](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/addressed_router.rs#L703-L725)）识别 `Server overloaded:` / `Server unavailable:` 两种前缀。

最后等回拨并解码（[addressed_router.rs:579-622](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/addressed_router.rs#L579-L622)）：`recv_registered.into_parts()` 会**解除** RAII 清理（领取后，这个 subject 由 worker 的回拨或发现面的 watcher 来收割，不再由本端清理）；`response_stream_provider.await` 的三种结局——`Ok(Ok(stream))` 正常、`Ok(Err(e))` 映射 `CannotConnect`（worker 本地 setup 失败）、`Err(_)` 即 oneshot 被 drop，映射 `Disconnected`。

**④ 传输接缝 trait**

[addressed_router.rs:795-819](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/addressed_router.rs#L795-L819) 定义 `StreamingDispatch`：`generate`（unary）、`generate_bidirectional`（流式输入）、`on_instance_removed` / `on_instance_added`（发现面驱动的清理回调）。

doc 注释里有一条**强约束**值得抄下来：实现方必须把故障暴露为顶层 `crate::error::ErrorType` 变体（`CannotConnect` / `Disconnected` / `ConnectionTimeout` / `ResponseTimeout` / `WorkerOverloaded` / `ResourceExhausted` / `Cancelled`），否则 `wrap_with_fault_detection` 的「报 down / 报过载 / 迁移」逻辑全部失效。这是类型系统外的隐式契约。

**⑤ `NetworkManager` 屏蔽传输差异**

[lib/runtime/src/pipeline/network/manager.rs:237-242](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/manager.rs#L237-L242)：

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

**答案**：`Deny` 直接返回 `ErrorType::CannotConnect`（精确目标语义，不做任何重选）；`Allow` 会从当前可路由的 free 列表里另选一个 worker 并打 warn 日志「Instance disappeared during routing, reselecting」。`direct()`（[push_router.rs:998-1010](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/push_router.rs#L998-L1010)）用的是 `Deny`；而 `direct_within` / `dispatch_preselected_prepared` 允许 fallback，`direct_within` 还能用 `TransportFallback::Within(allowed)` 把重选范围限定在调用者给的集合内（LoRA 副本集过滤就用它）。

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

`endpoint_path` 的格式是 `{instance_id:x}/{endpoint_name}`（见 [component/endpoint.rs:341-344](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/component/endpoint.rs#L341-L344) 的 `format!("{}:{}/{:x}/{}", tcp_host, tcp_port, connection_id, endpoint_id.name)`），而完整的传输地址是 `host:port/{instance_id:x}/{endpoint_name}`——多个 worker 共享同一个 TCP server 时（`--num-workers > 1`），靠 instance_id 部分区分路由。

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

[lib/runtime/src/pipeline/network.rs:57-65](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network.rs#L57-L65)：

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

`encode` / `decode` 是薄薄的一层分发（[network.rs:102-114](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network.rs#L102-L114)）：Json 走 `serde_json::to_vec` / `from_slice`，Msgpack 走 `rmp_serde::to_vec_named` / `from_slice`。

**② 进程级缓存与环境变量解析**

[network.rs:67-93](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network.rs#L67-L93)。注意 `REQUEST_PLANE_PAYLOAD_CODEC: OnceLock`（[network.rs:44](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network.rs#L44)）——**首次读取后进程内缓存**，之后改环境变量无效。环境变量名定义在 [config/environment_names.rs:717-721](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/config/environment_names.rs#L717-L721)，注释明确写了默认是 `msgpack`。

值得留意一个「看起来像 bug 其实是设计」的点：`from_config_value` 里**非法值也落到 Msgpack**，还会打一条 warn。因为 Msgpack 是当前推荐默认，所以「配错了」宁可回到默认也不报错。

**③ 发送方按目标 worker 协商**

[addressed_router.rs:204-208](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/addressed_router.rs#L204-L208)：

```rust
fn payload_codec_for_worker(instance: Option<&Instance>) -> RequestPlanePayloadCodec {
    instance
        .and_then(|instance| instance.request_plane_codec)
        .unwrap_or(RequestPlanePayloadCodec::Json)
}
```

而 worker 侧在注册 endpoint 时把进程级配置广告出去，[component/endpoint.rs:186](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/component/endpoint.rs#L186) 的 `Instance` 字段 `request_plane_codec: Some(RequestPlanePayloadCodec::configured())`。

**④ 信封组装**

[addressed_router.rs:146-202](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/addressed_router.rs#L146-L202) 的 `build_request_envelope` 是两层编解码的汇合点：先 `serde_json::to_vec(&control_message)` 得到控制头（有 128 KiB 上限，见 `CONTROL_MESSAGE_MAX_BYTES` 于 [addressed_router.rs:125](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/addressed_router.rs#L125)），再 `payload_codec.encode(req)` 得到数据体，最后：

```rust
let msg = match data {
    Some(d) => TwoPartMessage::from_parts(ctrl.into(), d.into()),   // unary
    None => TwoPartMessage::from_header(ctrl.into()),               // 双向流式
};
let codec = TwoPartCodec::default();
let buffer = codec.encode_message(msg)?;
```

**⑤ `TcpRequestMessage` 的编码与零拷贝优化**

[codec.rs:264-303](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/codec.rs#L264-L303) 的 `encode` 把四段长度/内容依次写进 `BytesMut` 再 `freeze()` 成 `Bytes`。而 [codec.rs:308-334](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/codec.rs#L308-L334) 的 `into_frame` 是发送路径上的优化：只拼协议头、把 payload 保留为独立 `Bytes` 块，避免先把整个帧拍平造成一次大拷贝。对应的单测 [codec.rs:541-591](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/codec.rs#L541-L591) 里甚至断言了 `frame.payload.as_ptr() == payload.as_ptr()`——指针相同，证明确实零拷贝。

**⑥ `TwoPartCodec` 的解码**

[codec/two_part.rs:41-109](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/codec/two_part.rs#L41-L109)。三段定长头共 24 字节（header_len u64 + body_len u64 + checksum u64）。两个细节：

- **checksum 只在 debug 构建校验**（[two_part.rs:81-101](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/codec/two_part.rs#L81-L101)），release 版写 dummy 0、读侧跳过——用 xxh3 校验换性能。
- 解码是**增量友好**的：字节不够就返回 `Ok(None)`，等下一次有数据再试，这正是 tokio `Decoder` 的标准姿势，天然适配 TCP 半包。

**⑦ 最大消息尺寸**

[network.rs:40-55](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network.rs#L40-L55)：默认 32 MiB，可用 `DYN_TCP_MAX_MESSAGE_SIZE` 覆盖，client、server、零拷贝解码三条路径共用这一个 `OnceLock` 值。

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

**需要观察的现象**：三个测试都通过。重点读第一个测试（[network.rs:515-552](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network.rs#L515-L552)）：一段**不含** `payload_codec` 字段的 JSON 反序列化成 `RequestControlMessage` 后，`message.payload_codec` 自动等于 `Json`，并且能用它成功解码一段 JSON payload——这就是「老 frontend 发的消息新 worker 也能读」的机制证明。

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

客户端 `decode_response_stream` 对终止协议的三个防御（[addressed_router.rs:52-118](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/addressed_router.rs#L52-L118)）：看到 `complete_final` 后又来数据 → 报「this should never happen」；流断且 `is_complete_final` 为 false 且 context 已 stopped → 静默结束（客户端取消）；流断且未 stopped → 合成 `Disconnected` 错误（worker 掉了）。

#### 4.3.3 源码精读

**① `PushWorkHandler` 与 `NetworkStreamWrapper`**

trait 定义在 [network.rs:844-867](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network.rs#L844-L867)。它有三个方法：`handle_payload`（核心）、`add_metrics`（把 endpoint 的指标注入）、`set_endpoint_health_check_notifier`（金丝雀健康检查的定时器重置通知，带默认空实现保证向后兼容）。

终止帧的载体 [network.rs:913-918](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network.rs#L913-L918)：

```rust
#[derive(Serialize, Deserialize, Debug, PartialEq, Eq)]
pub struct NetworkStreamWrapper<U> {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub data: Option<U>,
    pub complete_final: bool,
}
```

它上方的块注释（[network.rs:869-911](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network.rs#L869-L911)）完整解释了为什么不能用 `Annotated` 直接做这件事（`U` 可能已经是 `Annotated` 会双重包装），以及为什么截断检测必须在 egress 侧做（只有它能区分网络截断与正常结束）。这条注释是本讲最值得通读的一段文档。

**② `Ingress` 的结构与两种构造路径**

[network.rs:746-752](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network.rs#L746-L752)：

```rust
pub struct Ingress<Req: PipelineIO, Resp: PipelineIO, Adapter = SerdeIngressPayloadAdapter> {
    segment: OnceLock<Arc<SegmentSource<Req, Resp>>>,
    metrics: OnceLock<Arc<WorkHandlerMetrics>>,
    endpoint_health_check_notifier: OnceLock<Arc<tokio::sync::Notify>>,
    payload_adapter: Arc<Adapter>,
}
```

三个 `OnceLock` 说明 `Ingress` 是「构造后单次装配」的：segment（指向引擎的管线段）、metrics、健康通知器各只能设一次。

两条构造路径：`for_engine`（[network.rs:771-774](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network.rs#L771-L774)）是 u3-l3 讲过的「把引擎挂上 endpoint」的标准方式——内部 `frontend.link(backend)?.link_terminal(frontend)?` 闭合成环；`attach` 则允许先建空 Ingress 再事后接 segment。

第三个泛型参数 `Adapter` 是本讲的一个扩展点：默认 `SerdeIngressPayloadAdapter`，但你可以在 `for_engine_with_adapter` 里换成自己的实现（比如对多模态请求做特殊解码），见 [network.rs:822-836](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network.rs#L822-L836)。

**③ `IngressDispatch`：单目与双向的分流**

[push_handler.rs:367-375](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/ingress/push_handler.rs#L367-L375) 定义了这个内部 trait，注释点明它的存在意义：**捕获单目与双向两种 wire 形态的差异，把其余所有东西（指标 guard、响应流打开、`segment.generate`、prologue、泵）都留在共享的 `handle_payload_shared` 里**。

单目实现（[push_handler.rs:377-434](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/ingress/push_handler.rs#L377-L434)）里有一个防御：收到 header-only 信封就报「unary engine received a header-only envelope」——因为单目引擎的业务数据就在 data 半边，没有 data 就没有请求。

双向实现（[push_handler.rs:436-564](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/ingress/push_handler.rs#L436-L564)）则反过来拒绝带 data 的信封，然后**worker 主动回拨** `request_stream_connection_info`（`TcpClient::create_request_stream`，[push_handler.rs:499-512](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/ingress/push_handler.rs#L499-L512)），并 spawn 一个 forwarder 任务把原始字节逐帧解成 `T` 喂给引擎的输入流。注意 forwarder 每轮都查 `is_killed() || is_stopped()`，避免在引擎已经放弃后还往一个没人消费的 channel 里灌数据。

**④ `handle_payload_shared` 的共享主体**

[push_handler.rs:579-694](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/ingress/push_handler.rs#L579-L694)。

指标 guard 的 RAII 设计（[push_handler.rs:594-607](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/ingress/push_handler.rs#L594-L607)）：

```rust
let _inflight_guard = self.metrics().map(|m| {
    m.request_counter.inc();
    m.inflight_requests.inc();
    m.request_bytes.inc_by(payload.len() as u64);
    ...
    RequestMetricsGuard { ... }
});
```

`RequestMetricsGuard`（[push_handler.rs:125-141](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/ingress/push_handler.rs#L125-L141)）的 `Drop` 实现里做 `inflight_requests.dec()` 和 `request_duration.observe()`——**无论函数从哪个分支退出，指标都会被正确收尾**。函数末尾还有一句 `drop(_inflight_guard);`，显式保证 guard 活到函数最后一行（否则 Rust 的 drop 顺序可能在 pump 完成前就释放它）。

网络传输耗时（[push_handler.rs:617-620](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/ingress/push_handler.rs#L617-L620)）：用 worker 收到时的墙钟 `t2` 减去信封里的 `frontend_send_ts_ns`（t1），观测到 `WORK_HANDLER_NETWORK_TRANSIT_SECONDS`。注意它依赖两台机器的 NTP 同步，跨机只当近似值看。

回拨（[push_handler.rs:625-638](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/ingress/push_handler.rs#L625-L638)）：`tcp::client::TcpClient::create_response_stream(request.context(), response_connection_info, ...)`——这一行就是「worker 主动连回客户端」的物化。注释里留了一句历史包袱：「eventually have a handler class which will returned an abstracted object, but for now, we only support tcp here」。

prologue（[push_handler.rs:656-684](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/ingress/push_handler.rs#L656-L684)）：`segment.generate()` 成功就 `send_prologue(None)` 并观测 TTFR 指标；失败则 `send_prologue(Some(error_string))` 然后 `Err(e)?` 提前返回。**prologue 是在 `generate` 被允许返回之前就发出的**——这样客户端不会在引擎还没准备好时就一直空等。

**⑤ `pump_response_stream`：泵与错误分类**

[push_handler.rs:155-298](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/ingress/push_handler.rs#L155-L298)。这是接收端最精巧的一段，核心是一个循环加大量「分类判断」：

- 编码失败 → 计 `serialization` 错误、不再发终止帧、break。
- `publisher.send` 失败 → **按 context 状态分类**：
  - `is_stopped()` 为真 → 客户端正常收完主动断开，只打 warn 不计错；
  - 否则 → 真实故障，打 error、调 `context.stop_generating()` 止血、计 `publish_response` 错误。
- 成功且非错误帧 → 通知健康检查定时器重置（错误帧不能证明引擎健康）。
- 末尾终止帧发送失败 → 又一层分类（[push_handler.rs:267-289](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/ingress/push_handler.rs#L267-L289)）：`is_stopped() && !is_killed()` 视为对端已拆除、只打 debug；**killed 或仍 attached 都算真实错误**。

那段关于 `is_stopped() && !is_killed()` 的长注释（[push_handler.rs:256-266](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/ingress/push_handler.rs#L256-L266)）解释了为什么条件要这么窄：`is_stopped()` 的定义是 `state != Live`，所以 `kill()` 之后它**也为真**；而 TCP 读错误会触发 `kill()`，如果只判 `is_stopped()` 就会把真实连接故障静默吞掉。这是一个把「语义重叠的三态」掰开用的好例子，配套的四个单测（[push_handler.rs:893-943](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/ingress/push_handler.rs#L893-L943)）分别覆盖 Stop / Kill / ConnectionReadError / TransportOnly 四种竞态。

**⑥ worker 侧的分发：`SharedTcpServer`**

[shared_tcp_endpoint.rs:333-384](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/ingress/shared_tcp_endpoint.rs#L333-L384) 的 `handle_work_item` 是从 socket 到 handler 的最后一跳：先从 `x-frontend-send-ts-ns` 算传输耗时，再从 trace headers 建 span、提取 request-id，最后 `service_handler.handle_payload(work_item.payload, request_id)`。

按 endpoint 路由的逻辑在 [shared_tcp_endpoint.rs:641-677](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/ingress/shared_tcp_endpoint.rs#L641-L677)：解出 `request_msg.endpoint_path()`，查 `handlers` 表，查不到就 warn「No handler found for endpoint」。注册/注销则通过 [shared_tcp_endpoint.rs:516](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/ingress/shared_tcp_endpoint.rs#L516) 的 `self.handlers.insert(endpoint_path, handler)` 与 `unregister_endpoint`。

这条链路的最上游是 `EndpointConfigBuilder::start_with_registration`（[component/endpoint.rs:133-160](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/component/endpoint.rs#L133-L160)），它把 handler 注册到进程级 `request_plane_server()`，并构造带 TCP 地址的 `TransportType` 写进发现面——这正好接上 u3-l2 讲的注册流程。

#### 4.3.4 代码实践

**实践目标**：不写 Rust，用 worker 进程自带的 Prometheus 指标统计 10 条请求的平均 payload 大小，做成可复用的小工具。

**原理**：`handle_payload_shared` 里那行 `m.request_bytes.inc_by(payload.len() as u64)` 已经把每条请求的信封字节数累加进了 `dynamo_component_request_bytes_total`，同时 `request_counter.inc()` 累加 `dynamo_component_requests_total`。**你不需要加任何日志或中间层——指标已经在那里了。**

平均 payload 大小：

\[ \bar{B} = \frac{\Delta\,\texttt{request\_bytes\_total}}{\Delta\,\texttt{requests\_total}} \]

**操作步骤**：

1. 给 worker 进程开启状态服务器（默认关闭，[config.rs:104-111](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/config.rs#L104-L111) 说明 `-1` 为禁用）：

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

**答案**：它控制单个流的「socket 任务」与「引擎消费者/生产者」之间 mpsc channel 的缓冲帧数（[network.rs:423-426](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network.rs#L423-L426) 的注释说明这是保留了 TCP 传输历史上硬编码的值）。缓冲满时 `send` 挂起，形成背压。调大：吞吐更平滑、更能吸收消费者抖动，但每流内存占用上升、取消时可能多泵几帧才感知；调小：背压来得更快、取消传播更及时，但高吞吐下容易频繁挂起。它通过 `StreamOptions::send_buffer_count` 配置（[network.rs:449-453](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network.rs#L449-L453)）。

---

### 4.4 `RouterMode`：七种路由模式与占用记账

#### 4.4.1 概念说明

`RouterMode` 回答「选谁」这个问题。它定义在 [push_router.rs:187-199](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/push_router.rs#L187-L199)：

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

`OverloadCheck` 枚举只有两个值（[push_router.rs:208-212](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/push_router.rs#L208-L212)）：`Required`（要查）与 `AlreadyAdmitted`（已准入，跳过）。

#### 4.4.3 源码精读

**① 模式的能力声明**

[push_router.rs:276-301](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/push_router.rs#L276-L301) 集中了四个声明式方法：

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

`telemetry_label`（[push_router.rs:264-274](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/push_router.rs#L264-L274)）给每种模式一个稳定的指标标签（如 `"round-robin"`、`"power-of-two-choices"`），有专门单测锁死这些字符串，防止误改导致指标断代。

**② 构造时的三份 picker**

[push_router.rs:304-315](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/push_router.rs#L304-L315)：

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

为什么无论配置什么模式都要建 round_robin 和 random 两份？因为 KV 模式的持有者仍可能显式调 `router.round_robin(...)` 或 `router.random(...)`（KV 未命中时的降级路径）。这样「配置的模式」与「随时可用的静态策略」互不干扰，各自的游标状态独立演进——配套单测 [push_router.rs:2238-2257](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/push_router.rs#L2238-L2257) 验证了 random 选过之后 round_robin 的游标不受影响。

**③ `AsyncEngine` 分发**

[push_router.rs:1975-1991](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/push_router.rs#L1975-L1991)：`PushRouter` 自己也实现了 `AsyncEngine<SingleIn<T>, ManyOut<U>, Error>`，`generate` 里按模式分发到 `random` / `round_robin` / `power_of_two_choices` / `least_loaded` / `device_aware_weighted`，而对 KV 和 Direct 直接 `anyhow::bail!`。**注意 bail 是运行时错误而不是编译期约束**——这就是为什么 `router_mode` 字段注释要反复强调「我们从不打算在 KV 模式下调 generate」。

**④ 故障检测与隔离：四个入口汇聚到一个 `_inner`**

本次 #13861 把原来的两个发送函数重构成了一条四层调用链，全部落在 [push_router.rs:1654-1752](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/push_router.rs#L1654-L1752)：

| 函数 | 行号 | 职责 |
|------|------|------|
| `generate_with_fault_detection` | [1654-1667](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/push_router.rs#L1654-L1667) | 无 prepare 的薄包装，固定 `OverloadCheck::Required` |
| `generate_with_fault_detection_inner` | [1669-1685](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/push_router.rs#L1669-L1685) | 加上 `overload_check` 参数的转发层 |
| `generate_with_fault_detection_prepared` | [1687-1705](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/push_router.rs#L1687-L1705) | 带 prepare 回调的公开入口，固定 `Required` |
| `generate_with_fault_detection_prepared_inner` | [1707-1752](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/push_router.rs#L1707-L1752) | **真正的实现** |

唯一的实现体里，过载检查被一行 `if` 守住了（[push_router.rs:1733-1735](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/push_router.rs#L1733-L1735)）：

```rust
let (instance_id, address, transport_kind, instance) =
    self.resolve_transport(instance_id, fallback)?;
if matches!(overload_check, OverloadCheck::Required) {
    self.check_workers_available(instance_id, &request_id)?;
}
```

注意被跳过的**只有** `check_workers_available`：`resolve_transport`（发现面还在不在）和后面的 `wrap_with_fault_detection`（响应流故障检测）对 KV 准入直发照常生效。也就是说这个优化砍掉的是「重复的过载复核」，不是安全网。

`check_workers_available`（[push_router.rs:1758-1795](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/push_router.rs#L1758-L1795)）查的是 `routing_instances.is_overloaded(instance_id)`——**共享的 client 级过载集合**。命中就构造 `ErrorType::WorkerOverloaded`（注意：这是背压语义，不是故障语义）。

`wrap_with_fault_detection`（[push_router.rs:1881-1966](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/push_router.rs#L1881-L1966)）把响应流再包两层：

- **错误分类层**：`is_inhibited`（[push_router.rs:39-52](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/push_router.rs#L39-L52)）列出的错误类型会触发 `report_instance_down`（把 worker 从本进程的可路由集合里摘掉）；`WorkerOverloaded` 则走 `mark_overloaded_immediate`（背压路径，**不是**故障路径，等 worker 的下一次负载事件来刷新）。
- **不活跃超时层**：若设置了 `DYN_HTTP_BACKEND_STREAM_TIMEOUT_SECS`，用 `tokio::select! { biased; ... }` 包住流，超时产出合成 `ResponseTimeout` 并隔离 worker。

`is_inhibited` 里 `StreamIncomplete` 那条注释值得单独读：流中途断掉意味着 worker 丢了这个请求，必须隔离，否则迁移重试可能在发现面摘除生效前**又选回同一个死 worker**。

**⑤ 新增：`dispatch_kv_admitted`——KV 准入后的免复查直发**

这是本次更新（#13861「own load state per routing context」）落在请求面上的核心改动，[push_router.rs:1159-1180](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/push_router.rs#L1159-L1180)：

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

调用方在 lib/llm 的 KV 路由宿主里，[lib/llm/src/kv_router/routing_host/kv.rs:197-199](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/kv_router/routing_host/kv.rs#L197-L199)：

```rust
let dispatch = self
    .inner
    .dispatch_kv_admitted(updated_request, selection.worker.worker_id);
```

这正是 4.4.1 里「外部决策」组的落地：选点由 `routing_host/kv_selection.rs` 完成，`PushRouter` 只负责把已准入的请求送到那个人手上。

**⑥ worker 消失的两种时机**

`resolve_transport`（[push_router.rs:1802-1875](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/push_router.rs#L1802-L1875)）处理「选中之后、发出之前」worker 消失；而 `spawn_instance_removal_watcher`（[push_router.rs:362-465](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/push_router.rs#L362-L465)）处理「发出之后」：它订阅发现面的增删事件，worker 被摘除时调 `dispatch.on_instance_removed(eid)` → `AddressedPushRouter::cancel_instance_streams` 取消该 instance 的所有待领取响应流注册，让阻塞在 oneshot 上的请求立刻拿到 `Disconnected` 而不是傻等。

注意 `ENDPOINT_WATCHER_ACTIVE` 这个 `OnceLock<DashMap>`：**每个 endpoint 只起一个 watcher**，跨所有 `PushRouter` 实例共享；条目在 watcher 退出时移除（`GuardRelease` 的 `Drop`），这样后来的 router 还能重新武装。注释特意说明「a leaked entry silently disables removal cancellation until process restart」——这是防泄漏的关键防线。

**⑦ 验证新行为的单测**

[push_router.rs:3508-3569](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/push_router.rs#L3508-L3569) 的 `admitted_dispatch_does_not_reject_the_load_it_just_booked` 用一个 `RecordingDispatch`（[push_router.rs:3377-3393](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/push_router.rs#L3377-L3393)，把每次 dispatch 录下来的假传输层）做了三步对比实验：

1. 手动 `client.set_overloaded_instances(&[instance_id])` 把唯一 worker 标成过载；
2. 调 `dispatch_exact` → 断言失败且错误是 `WorkerOverloaded`，且 `RecordingDispatch` **一次都没收到**（被过载闸拦在传输之前）；
3. 同样的过载状态下调 `dispatch_kv_admitted` → 断言**成功**，且 `RecordingDispatch` 收到恰好一次、目标就是那个 instance。

同一份过载集合、同一个 worker，两种入口一拒一放——这条测试就是 `OverloadCheck` 两个值的语义边界最精确的文档。

**⑧ 与 u3-l2 的衔接**

`select_untracked_worker`（[push_router.rs:762-773](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/push_router.rs#L762-L773)）里的 `self.client.routing_instances().free_ids()`，正是 u3-l2 讲过的 `discovered → routable → free` 漏斗的最后一层。`empty_free_pool_error`（[push_router.rs:731-751](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/push_router.rs#L731-L751)）区分两种空池：有 routable 但都过载 → `ResourceExhausted`（客户端该重试）；完全没有 routable → `Unavailable`。

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

- `p2c_never_selects_dominated_worker`（[push_router.rs:2429-2451](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/push_router.rs#L2429-L2451)）：worker 3 的负载被刷到 100，另外两个是 0，然后做 1000 次 P2C 选择，断言 worker 3 被选中 **0 次**。
- `occupancy_tracked_stream_releases_before_yielding_error`：错误帧必须在被 yield 出去**之前**就释放占用，否则重试逻辑看到错误时计数还没减，会误判负载。
- `admitted_dispatch_does_not_reject_the_load_it_just_booked`（[push_router.rs:3508-3569](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/push_router.rs#L3508-L3569)）：同一个被手动标记过载的 worker，`dispatch_exact` 报 `WorkerOverloaded`、`dispatch_kv_admitted` 畅通。

**预期结果**：五条测试全部 PASS（待本地验证）。

**源码阅读型对照（无需运行）**：把 [push_router.rs:1146-1180](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/push_router.rs#L1146-L1180) 里紧挨着的 `dispatch_exact` 与 `dispatch_kv_admitted` 并排读，写一张两列对照表：入参、guard 条件、`TransportFallback`、`OverloadCheck`、doc 注释里各自声明的适用场景。两张表的差异只有一列——但那一列就是整个 #13861 在请求面上的足迹。

**延伸（可选，需改源码并重新编译）**：在 [push_router.rs:992](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/push_router.rs#L992) `OccupancyPermit::from_counter(...)` 之后加一行 `tracing::info!(instance_id, load = state.load(instance_id), "P2C admitted");`，重跑 4.3.4 的 hello_world 实践并把 client 的 router 模式换成 power-of-two-choices，就能在日志里看到每次准入时的实时占用。注意本仓库禁止提交此类调试改动，实验后请还原。

#### 4.4.5 小练习与答案

**练习 1**：`select_next_worker()` 在 `RouterMode::LeastLoaded` 下返回什么？为什么？想拿到「下一个会被选的 worker」该怎么办？

**答案**：返回 `None`。因为 LeastLoaded（以及 P2C、DeviceAwareWeighted）的选择必须与占用记账原子耦合，而 `select_next_worker` 是一个不带记账的「看一眼」接口——如果它返回了 id 却没 +1，调用者随后用这个 id 发请求就会绕过记账，把占用计数搞错。所以这些模式直接返回 `None`（[push_router.rs:1452-1455](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/push_router.rs#L1452-L1455)）。想拿「会被选的 worker」应该用 `peek_next_worker()`，它走 `occupancy_state.peek(...)` 只看不占。配套单测 `least_loaded_peek_returns_available_worker_select_stays_none` 精确锁定了这对语义。

**练习 2**：KV 路由模式下误调了 `PushRouter::generate` 会怎样？那 `dispatch_kv_admitted` 在非 KV 模式下调用又会怎样？

**答案**：前者 `anyhow::bail!("KV routing should not call generate on PushRouter")`，请求直接失败；后者同样 `bail!("admitted dispatch is only valid in KV routing mode")`。两个守卫都是运行时而非编译错误，因为 `RouterMode` 是**运行时配置**（来自 CLI/环境变量/DGD），同一个二进制可能被配置成任何模式；Rust 的类型系统无法在编译期区分「这个 `PushRouter` 是 KV 模式构造的」。`dispatch_kv_admitted` 的守卫尤其重要：它的语义前提是「上游已做过载准入」，非 KV 模式下调用它等于凭空跳过一次本应执行的 `check_workers_available`。

**练习 3**：`dispatch_kv_admitted` 跳过了 `check_workers_available`，它跳过了哪些别的检查吗？如果那个 worker 在选点后、发送前从发现面消失了会怎样？

**答案**：只跳过了过载复查这一项。`resolve_transport`（发现面还在不在、要不要按 fallback 重选）和 `wrap_with_fault_detection`（响应流的报 down / 报过载 / 迁移 / 超时）都照常执行——而且它用的是 `TransportFallback::Deny`，所以 worker 消失时会直接得到 `ErrorType::CannotConnect`（精确目标语义，不做隐式重选，因为重选会破坏 KV 亲和性），错误类型仍然落在 `StreamingDispatch` 的契约集合里，迁移逻辑照常工作。免掉的只是「重复读一次共享过载集合」这一次内存读与潜在的自误拒。

**练习 4**：`DeviceAwareWeighted` 模式解决什么问题？`DYN_ENCODER_CUDA_TO_CPU_RATIO` 默认 8 意味着什么？

**答案**：解决异构 worker（CPU 编码器 + GPU 加速器）混部时的放置问题。多模态请求的视觉编码部分可以跑在 CPU 上，但 CPU 慢、GPU 快，不能均摊。这个比例表示「1 个非 CPU worker 的预算相当于 8 个 CPU worker」，即默认倾向把编码负载压向 CPU、把 GPU 留给计算密集部分（[push_router.rs:1342-1346](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/push_router.rs#L1342-L1346)）。它还能结合 `MultimodalCacheIndex` 判断「这个 worker 是否已缓存了本请求需要的 embedding」——**完整命中**可绕过加权记账（因为无需再编码），部分命中仍按比例走（[push_router.rs:1296-1305](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/push_router.rs#L1296-L1305) 注释）。如果只剩一种设备类型，它自然退化成 least-loaded。

---

## 5. 综合实践

把本讲四个模块串成一个任务：**给 hello_world 加一个「请求面观测台」，回答四个问题——一条请求的信封有多大？控制头和数据体各占多少？msgpack 比 JSON 省多少？KV 准入直发与普通直发在过载检查上差在哪？**

**步骤**：

1. **铺底**：按 4.3.4 启动 worker（`DYN_SYSTEM_PORT=8099`），按 4.1.4 启动 client（`RUST_LOG=dynamo_runtime=trace`）。跑 10 条请求。

2. **总量**：从 `/metrics` 取 `dynamo_component_request_bytes_total` 与 `dynamo_component_requests_total` 的增量，算出平均信封字节数 \(\bar{B}\)。注意这个数字包含 `TwoPartMessage` 的 24 字节定长头（header_len + body_len + checksum 各 8 字节），以及外层 `TcpRequestMessage` 的协议头（2 + endpoint_path + 2 + headers JSON + 4 字节）。

3. **分解**：从 client 的 trace 日志读出 `ctrl: N bytes, data: M bytes`，得到控制头与数据体的精确拆分。对比 \(\bar{B}\) 与 \(24 + N + M\)，差值就是外层成帧开销。

4. **对照**：把 worker 的 `DYN_REQUEST_PLANE_CODEC` 分别设为 `msgpack` 和 `json` 各跑一轮（**注意要重启 worker**，因为 `OnceLock` 只读一次环境变量），对比 `data:` 字节数的差异。hello_world 的请求体是一个短字符串，差异可能不明显；可以改用 sample 后端发一条带长 system prompt 的 chat 请求让差异放大。

5. **过载语义对照**（源码阅读 + 单测，不依赖集群）：跑 4.4.4 的 `admitted_dispatch_does_not_reject_the_load_it_just_booked`，然后写一段约 200 字的说明，回答——为什么「准入可能已同步发布本请求自身负载」就必须跳过共享复查？如果把 `OverloadCheck::AlreadyAdmitted` 改成 `Required`，这个测试会在哪一步失败？

6. **整理**：写成一页实验记录，包含拓扑图、每轮的原始读数、计算过程、结论。

**预期结论形态**：控制头大小基本恒定（由 request-id 长度与 connection_info 决定），数据体随请求内容线性增长；msgpack 对结构化数据（多字段、嵌套）的压缩收益显著，对短字符串收益很小；成帧开销是固定的几十字节常量；`dispatch_kv_admitted` 与 `dispatch_exact` 的差别只在过载闸那一行 `if`，其余安全网全部保留。这些结论直接决定你在生产上该不该为 payload codec 纠结——**短请求不值得，长上下文/多模态请求很值得**。

全部步骤均可在无 GPU、无 etcd/NATS 的本地环境完成（`DYN_DISCOVERY_BACKEND=file` + `DYN_EVENT_PLANE=zmq`）。所有读数标注「待本地验证」。

## 6. 本讲小结

- **请求面是两条独立的连接**：请求通道只传一帧信封、收到空 ACK 即复用；响应通道由 worker 依据信封里的 `ConnectionInfo` 主动回拨客户端预注册的流服务器。这种 call-home 模式让请求连接轻量化、响应可以长流式传输。
- **发送端三层分工**：`PushRouter` 管选谁与故障检测，`AddressedPushRouter` 管信封组装与回拨等待，`RequestPlaneClient` 管具体传输。`StreamingDispatch` trait 把后两层之间的接缝显式化，`NetworkManager` 是唯一读取网络环境变量的地方——切换 TCP/NATS 不影响上层一行代码。
- **两层编解码必须分清**：成帧层（`TcpRequestMessage` 的长度前缀、`TwoPartCodec` 的 24 字节三段头）解决字节流边界；负载层（`RequestPlanePayloadCodec` 的 Json/Msgpack）解决业务序列化。**控制头永远是 JSON**，payload codec 以目标 worker 在发现面广告的值为准，老 worker 兜底 JSON。
- **推送语义的核心是 `handle_payload` 返回 `()`**：响应走旁路。终止靠显式的三段协议——prologue（流就绪或失败）→ 数据帧 × N → `complete_final: true` 终止帧。错误分类反复使用 `is_stopped() && !is_killed()` 这种窄条件，因为 `is_stopped()` 的语义是 `state != Live`，`kill()` 也会让它为真。
- **指标不需要你自己加**：`request_bytes` / `requests_total` / `inflight_requests` / `errors_total` 已经在 `handle_payload_shared` 里通过 RAII guard 维护，开 `DYN_SYSTEM_PORT` 就能从 `/metrics` 抓到。
- **`RouterMode` 七种分三组**：无状态轮询（RoundRobin/Random）、负载感知（P2C/LeastLoaded/DeviceAwareWeighted，选择与占用记账原子耦合、用 RAII 许可证随流释放）、外部决策（KV/Direct，不经过 `generate`，由上游 `RoutingHost` 或 KV 路由器显式指定目标后调 `direct()`）。
- **本次新增 `OverloadCheck` 二值化过载闸**：发送主链路统一收敛到 `generate_with_fault_detection_prepared_inner`，普通路径（`direct`/`dispatch_exact` 等）带 `Required` 复核共享过载集合；新增的 `dispatch_kv_admitted` 带 `AlreadyAdmitted` 免复查——因为 KV 准入可能已同步发布本请求自身的负载，再查会自误拒。发现面解析与响应流故障检测两条安全网都不受影响。

## 7. 下一步学习建议

本讲讲完了「一条请求怎么在网络上传送」，接下来的自然走向有三条：

1. **u3-l5（传输层：etcd + NATS/ZMQ）**：本讲的 `RequestPlaneClient` 下面是 TCP，事件面走的是 NATS/ZMQ。那一讲讲控制面（etcd lease/lock）与事件面（PubSub）的物理载体，与本讲构成 `lib/runtime/src/transports/` 的全景。
2. **u4-l2（HttpService：OpenAI 兼容 HTTP 服务）**：本讲的 `PushRouter` 是 frontend 内部把请求送到 worker 的那一跳；u4-l2 讲 HTTP 请求怎么从外部进入 frontend，包括并发许可与背压——你会看到 `WorkerOverloaded` / `ResourceExhausted` 这些本讲出现的错误类型如何被翻译成 HTTP 状态码。
3. **u6-l2（Rust 路由核心：routing_host、负载上下文与调度）**：本讲的 `dispatch_kv_admitted` 是为谁开的口子？就是 `lib/llm/src/kv_router/routing_host/kv.rs` 里那个 KV 路由宿主。#13861 的另一半改动在那里——`routing_load.rs` 新增的 `RoutingLoadContext` 让每个路由上下文自持负载状态，学完 u6 你就能把「选点时准入 → 直发时免复查」这条线完整地画出来。

阅读源码时建议带着这两个问题去：`pump_response_stream` 里那四种竞态的单测为什么必须成对出现？`spawn_instance_removal_watcher` 的 `GuardRelease` 如果漏写会发生什么？这两个问题的答案能检验你是否真的理解了「取消传播」与「一次性资源」这两个贯穿 Dynamo 全局的主题。

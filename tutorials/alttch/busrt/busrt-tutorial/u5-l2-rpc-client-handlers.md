# RpcClient 与 RpcHandlers 处理器

## 1. 本讲目标

本讲接着 [u5-l1](u5-l1-rpc-protocol.md) 讲过的 RPC **协议与帧格式**，往上走一层，讲解 RPC 的**客户端与处理器**实现（源码集中在 `src/rpc/async_client.rs`）。

读完本讲，你应该能够：

1. 用 `RpcClient`（实现 `Rpc` trait）向任意对端发起 `notify` / `call0` / `call` 三类调用，并说清它们在「是否需要回复」上的区别。
2. 实现 `RpcHandlers` trait，写出一个能响应方法调用、接收通知、处理非 RPC 帧的 RPC 服务端。
3. 看懂 `processor()` 事件循环：它如何把一帧 `Message` 拆成 `RpcEvent`、分发给对应 handler、再把回复帧送回调用方。
4. 理解 `Options` 的阻塞模式（`blocking_notifications` / `blocking_frames`）与 `task_pool` 选项的取舍，以及阻塞模式下的「死锁陷阱」。

本讲只覆盖**异步 RPC**（`rpc` feature）。同步版本（`rpc-sync` feature）在 [u7-l4](u7-l4-sync-client.md) 单独讲解。

## 2. 前置知识

本讲建立在以下已学内容之上，不再重复：

- **[u5-l1](u5-l1-rpc-protocol.md)**：RPC 复用传输层 `Message` 帧，载荷首字节区分 4 种动作——通知 `0x00`、请求 `0x01`、回复 `0x11`、错误 `0x12`；`RpcEvent` 是对原始帧的零拷贝语义视图；`id == 0` 表示「不需要回复」。
- **[u4-l1](u4-l1-async-client-trait.md)**：`AsyncClient` 是统一异步客户端契约；`OpConfirm`（`Option<oneshot::Receiver<...>>`）是否为 `Some` 由 `QoS::needs_ack()` 决定；`EventChannel` 是入站帧通道。
- **[u4-l2](u4-l2-ipc-client.md)**：外部 `ipc::Client` 经 socket 连接代理，有握手、心跳、会断线。
- **[u2-l1](u2-l1-core-types.md)**：`QoS` 是两个正交位——低位 `needs_ack()`、高位 `is_realtime()`。

两个本讲要用到、但需点明的事实：

1. **为什么需要 RPC 层？** 传输层（`ipc::Client` / `broker::Client`）只负责「把一帧字节可靠地送到对端」。RPC 层在这之上约定了「这一帧字节里的首字节是动作码、后面跟着 id / method / params」，并自动完成「登记一个等待回复的 oneshot、收到回复帧后按 id 兑现」这套**请求-回复配对**的簿记工作。没有 RPC 层，调用方就得自己手写这套配对逻辑。

2. **`rmp_serde` 与 `broker-rpc` feature 的关系。** `From<rmp_serde::encode::Error> for RpcError` 这类错误转换被 `#[cfg(feature = "broker-rpc")]` 守卫（见后文）。所以**凡是想在 handler 里用 `rmp_serde::to_vec(...)?` 并让 `?` 自动转成 `RpcError` 的代码，都必须启用 `broker-rpc` feature**（它 = `broker + rpc + rmp-serde`）。这正是官方示例 `client_rpc_handler` 的 `required-features` 写成 `["ipc", "rpc", "broker-rpc"]` 的原因——它明明是个纯客户端，却需要 `broker-rpc`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/rpc/async_client.rs` | 本讲主角。定义 `Options`、`RpcHandlers` trait、`Rpc` trait、`RpcClient`、`processor()` 事件循环 |
| `src/rpc/mod.rs` | 上一讲的主角。提供 `RpcEvent` / `RpcError` / `RpcResult` / `prepare_call_payload` 等协议层定义，本讲复用 |
| `examples/client_rpc.rs` | 纯调用方示例：`RpcClient::new0`（无 handler），演示 `call0` 与 `call` |
| `examples/client_rpc_handler.rs` | 服务端示例：实现 `RpcHandlers`，用原子计数器响应 `test` / `get` / `add` |

> 提示：`rpc` 模块本身由 `#[cfg(any(feature = "rpc", feature = "rpc-sync"))]` 守卫（见 [src/lib.rs:515-516](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L515-L516)）；本讲的 `async_client.rs` 子模块则在内部用 `#[cfg(feature = "rpc")]` 二级守卫（见 [src/rpc/mod.rs:4-5](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L4-L5)）。

## 4. 核心概念与源码讲解

### 4.1 RpcClient 与 Rpc trait：发起 RPC 调用

#### 4.1.1 概念说明

`RpcClient` 是把一个底层 `AsyncClient`（内部 `broker::Client` 或外部 `ipc::Client`）**包了一层**后得到的「RPC 客户端」。它额外做了两件事：

1. 维护一个递增的 `call_id`，给每个需要回复的调用编一个唯一号；
2. 维护一张「`call_id` → oneshot 回复通道」的登记表（`CallMap`），并在后台跑一个 `processor()` 任务，把收到的回复帧按 id 兑现给对应的调用。

`Rpc` trait 则是这套能力的**调用接口**，对外只暴露 `notify` / `call0` / `call` 三种调用方式（外加 `client()` 拿回底层句柄、`is_connected()` 查状态）。这样上层业务代码只依赖 `Rpc` trait，与具体传输解耦。

#### 4.1.2 核心流程

三种调用按「是否需要回复」区分：

| 方法 | 动作码 | 请求帧 id | 是否登记等待 | 返回值 |
| --- | --- | --- | --- | --- |
| `notify` | `0x00`（通知） | 无 id | 否 | `Result<OpConfirm, Error>` |
| `call0` | `0x01`（请求） | `[0,0,0,0]` | 否（id=0 表示不需回复） | `Result<OpConfirm, Error>` |
| `call` | `0x01`（请求） | 自增 `call_id` | 是 | `Result<RpcEvent, RpcError>` |

`call` 的完整往返流程（调用方视角）：

```text
1. 自增 call_id（到 u32::MAX 后回绕到 1）
2. prepare_call_payload(method, call_id)  -> [0x01][id:4][method][0x00]
3. 建 oneshot channel，以 call_id 为 key 写入 calls 表
4. 锁底层 client，zc_send 发出请求帧（若有 timeout，包一层 tokio::time::timeout）
5. 若 QoS.needs_ack()（OpConfirm 为 Some），先等待 ACK 确认
6. await oneshot rx —— 等 processor 收到 Reply 帧后兑现
7. 尝试把结果转成 RpcError：是错误帧则返回 Err，否则返回 Ok(RpcEvent)
```

注意 `call0` 与 `notify` 的区别：两者都不等回复，但 `call0` 走的是**请求**帧（`0x01` + method + params，只是 id 填全零），语义上「调用一个方法但不关心返回」；`notify` 走的是**通知**帧（`0x00`，没有 method 概念，整段 payload 就是通知数据）。

#### 4.1.3 源码精读

`RpcClient` 的结构（[src/rpc/async_client.rs:125-134](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L125-L134)）：

```rust
pub struct RpcClient {
    call_id: SyncMutex<u32>,                       // 自增调用号
    timeout: Option<Duration>,                     // 来自底层 client
    client: Arc<Mutex<dyn AsyncClient>>,           // 包裹的底层客户端
    processor_fut: Arc<SyncMutex<JoinHandle<()>>>, // 后台事件循环句柄
    pinger_fut: Option<JoinHandle<()>>,            // 心跳任务（仅外部客户端）
    calls: CallMap,                                // call_id -> oneshot 回复通道
    connected: Option<Arc<atomic::AtomicBool>>,    // 连接状态 beacon
}
```

`CallMap` 的类型（[src/rpc/async_client.rs:90](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L90)）说明它就是一张按 `call_id` 索引的 oneshot 发送端表：

```rust
type CallMap = Arc<SyncMutex<BTreeMap<u32, oneshot::Sender<RpcEvent>>>>;
```

`Rpc` trait 的方法签名（[src/rpc/async_client.rs:92-123](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L92-L123)），注意三个调用方法的返回类型差异。

`call()` 的核心实现（[src/rpc/async_client.rs:370-420](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L370-L420)），关键片段：

```rust
// 1. 自增 call_id，到 u32::MAX 回绕到 1（避免 0，0 表示“不需回复”）
let call_id = { /* 自增逻辑，回绕到 1 */ };
// 2. 拼请求前缀 [0x01][id:4][method][0x00]
let payload = prepare_call_payload(method, &call_id.to_le_bytes());
// 3. 登记 oneshot 回复通道
let (tx, rx) = oneshot::channel();
self.calls.lock().insert(call_id, tx);
// 4-5. 发送 + 可选 ACK 确认（带 timeout），出错则从 calls 表移除并返回
// 6. 等回复
let result = rx.await.map_err(Into::<Error>::into)?;
// 7. 是错误帧就转成 RpcError，否则原样返回
if let Ok(e) = RpcError::try_from(&result) { Err(e) } else { Ok(result) }
```

其中 `unwrap_or_cancel!` 宏（[src/rpc/async_client.rs:391-401](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L391-L401)）负责在发送或确认失败时**清理 calls 表里登记的通道**，避免泄漏。

`call0()`（[src/rpc/async_client.rs:353-366](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L353-L366)）和 `notify()`（[src/rpc/async_client.rs:341-352](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L341-L352)）都不登记回复通道：

```rust
// call0：id 填全零，直接发送，不等回复
let payload = prepare_call_payload(method, &[0, 0, 0, 0]);
self.client.lock().await.zc_send(target, payload.into(), params, qos).await

// notify：载荷首字节是 RPC_NOTIFICATION(0x00)
self.client.lock().await
    .zc_send(target, (&[RPC_NOTIFICATION][..]).into(), data, qos).await
```

> 旁注：`zc_send` 是 `AsyncClient` 的零拷贝发送方法，它接收 header（这里是 RPC 控制前缀）与 payload 两段，避免拼接。

#### 4.1.4 代码实践

**目标**：阅读 `call()`，理解 `call_id` 的回绕与「id=0 表示不需回复」的约定。

**步骤**：

1. 打开 [src/rpc/async_client.rs:377-387](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L377-L387)，找到 `call_id` 自增块。
2. 思考：为什么回绕目标是 `1` 而不是 `0`？（提示：结合上一讲 `is_response_required()` 的判据 `id != 0`）。
3. 再对照 `call0()`（[src/rpc/async_client.rs:360](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L360)），它传的是 `[0, 0, 0, 0]`，验证「id=0」正是 `call0`「不等回复」的实现机制。

**预期结果**：你能用一句话解释「`call0` 为什么不会卡在等回复」——因为它发的 id 是 0，而 `processor` 只对 `id > 0` 的请求登记回复（见 4.4）。

> 本实践为源码阅读型，无需运行。

#### 4.1.5 小练习与答案

**练习 1**：`call()` 在发送失败（第 4 步）时，为什么要从 `calls` 表里 `remove(&call_id)`？

**答案**：因为回复通道 `tx` 已经登记进表，若发送失败却不清除，这个永远不会被兑现的 `tx` 会一直留在表里造成泄漏；`unwrap_or_cancel!` 宏正是为此在每条错误路径上做清理（[src/rpc/async_client.rs:391-401](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L391-L401)）。

**练习 2**：`call()` 最后一步既 `await` 了 `opc`（OpConfirm），又 `await` 了 `rx`（oneshot 回复）。这两次等待分别对应什么？

**答案**：`opc` 对应 `QoS::Processed` 的 **ACK 确认**（代理收到帧的回执，QoS::No 时 `opc` 为 `None` 跳过）；`rx` 对应 **RPC 回复帧**（对端 handler 处理完后回送的结果）。两者是不同层级的确认：前者是传输层「帧送达」，后者是应用层「方法执行完」。

---

### 4.2 RpcHandlers trait：实现 RPC 服务端

#### 4.2.1 概念说明

如果说 `Rpc` trait 是「怎么调用别人」，那么 `RpcHandlers` trait 就是「怎么响应别人的调用」。一个 `RpcClient` 在创建时可以挂一组 handlers，于是它既是**调用方**（能用 `call`/`notify` 主动发起），又是**服务方**（能响应别人发来的请求/通知）。这正是 BUS/RT RPC 的双向特性——任何客户端都可以既当 caller 又当 callee。

`RpcHandlers` 有三个回调：

| 回调 | 触发条件 | 返回值 |
| --- | --- | --- |
| `handle_call(event)` | 收到 RPC **请求**帧（`0x01`，且会尝试回复） | `RpcResult`（`Ok(Some(数据))` / `Ok(None)` / `Err(RpcError)`） |
| `handle_notification(event)` | 收到 RPC **通知**帧（`0x00`） | 无返回（通知不需回复） |
| `handle_frame(frame)` | 收到**非 RPC** 帧（广播、发布订阅等，`kind != Message` 解析失败或非 Message） | 无返回 |

`RpcResult` 的类型定义在 [src/rpc/mod.rs:351-352](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L351-L352)：`Result<Option<Vec<u8>>, RpcError>`。三种结果分别对应：返回有数据的结果、返回空结果、返回错误。

#### 4.2.2 核心流程

实现一个 RPC 服务端的标准套路：

```text
1. 定义 struct，持有需要跨调用共享的状态
2. #[async_trait] impl RpcHandlers for MyStruct
   - handle_call：用 event.parse_method() 拿方法名，match 分发
   - handle_notification：处理通知
   - handle_frame：处理非 RPC 帧（如订阅到的主题消息）
3. 创建底层 client（ipc::Client 或 broker::Client）
4. RpcClient::new(client, handlers) —— 把 handlers 挂上去
5. 主循环里 while rpc.is_connected() { sleep } 保持运行
```

**关键陷阱（官方注释反复强调）**：handler 可能在后台被**并发**启动多个实例（见 4.3 / 4.4 的 `spawn!`），所以 handler 内部可变状态**必须**用原子类型或 `Mutex`/`RwLock` 保护。`examples/client_rpc_handler.rs` 的计数器用的就是 `atomic::AtomicU64`。

#### 4.2.3 源码精读

`RpcHandlers` trait 定义（[src/rpc/async_client.rs:66-76](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L66-L76)），注意默认实现：`handle_call` 默认返回「方法未找到」错误：

```rust
#[async_trait]
pub trait RpcHandlers {
    async fn handle_call(&self, event: RpcEvent) -> RpcResult {
        Err(RpcError::method(None))
    }
    async fn handle_notification(&self, event: RpcEvent) {}
    async fn handle_frame(&self, frame: Frame) {}
}
```

`DummyHandlers`（[src/rpc/async_client.rs:78-88](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L78-L88)）是给「只调用、不响应」的客户端用的占位实现，`RpcClient::new0` 就用它。

官方 handler 示例（[examples/client_rpc_handler.rs:27-70](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/client_rpc_handler.rs#L27-L70)）是最好的教材，关键片段：

```rust
struct MyHandlers {
    // handler 会被并发启动多实例，共享状态必须原子化
    counter: atomic::AtomicU64,
}

#[async_trait]
impl RpcHandlers for MyHandlers {
    async fn handle_call(&self, event: RpcEvent) -> RpcResult {
        match event.parse_method()? {        // 方法名（UTF-8 字符串）
            "test" => { /* 返回 {"ok": true} */ }
            "get"  => { /* 返回 {"value": counter} */ }
            "add"  => {
                let params: AddParams = rmp_serde::from_slice(event.payload())?;
                self.counter.fetch_add(params.value, atomic::Ordering::SeqCst);
                Ok(None)                     // 无返回数据
            }
            _ => Err(RpcError::method(None)),// 未知方法
        }
    }
    async fn handle_notification(&self, event: RpcEvent) { /* 打印通知 */ }
    async fn handle_frame(&self, frame: Frame) { /* 打印非 RPC 帧 */ }
}
```

这里有三个要点呼应上一讲：

- `event.parse_method()`（[src/rpc/mod.rs:114-117](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L114-L117)）把 `method()` 的字节切片转成 `&str`。
- `event.payload()`（[src/rpc/mod.rs:79-82](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L79-L82)）返回**去掉 RPC 控制前缀后**的纯参数字节——上一讲讲过的 `payload_pos` 在这里发挥作用。
- `add` 用 `fetch_add` 原子累加，正应了「并发安全」的要求。

> 旁注：`handle_call` 里 `rmp_serde::to_vec_named(&payload)?` 的 `?` 需要 `From<rmp_serde::encode::Error> for RpcError`，而它被 `#[cfg(feature = "broker-rpc")]` 守卫（[src/rpc/mod.rs:300-309](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L300-L309)）。这就是该示例 `required-features` 必须含 `broker-rpc` 的根因。

#### 4.2.4 代码实践

**目标**：读懂 handler，解释为什么 `counter` 不能用普通 `u64`。

**步骤**：

1. 打开 [examples/client_rpc_handler.rs:16-20](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/client_rpc_handler.rs#L16-L20)，注意 struct 注释明确写了「all RPC handlers are launched in parallel multiple instances」。
2. 假设把 `counter: atomic::AtomicU64` 改成 `counter: u64`，在 `add` 里写 `self.counter += params.value;`，思考编译会发生什么。

**预期结果**：编译失败。因为 `handle_call(&self, ...)` 是 `&self`（不可变借用），不能直接修改字段；即便用 `Cell`/`RefCell`，并发场景下也会数据竞争。原子类型是这里的正确选择。

> 本实践为源码阅读 + 心智模型推演型，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：`handle_call` 返回 `Ok(None)` 和返回 `Ok(Some(vec![]))` 有区别吗？对端 `call()` 收到的是什么？

**答案**：有区别。`Ok(None)` 表示「有结果但无数据」，processor 构造回复帧时 payload 段为空（[src/rpc/async_client.rs:204-206](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L204-L206) 走 `(&[][..]).into()` 分支）；`Ok(Some(data))` 则把 `data` 作为回复 payload。对端 `call()` 两种情况都收到 `Ok(RpcEvent)`，区别仅在 `result.payload()` 的长度。

**练习 2**：如果客户端只想「调用方法但不处理任何回复/通知」，应该用哪个构造函数？为什么？

**答案**：用 `RpcClient::new0(client)`（[src/rpc/async_client.rs:275-277](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L275-L277)），它挂的是 `DummyHandlers`——`handle_call` 默认对所有方法返回「未找到」。这样客户端只发起调用、不响应任何入站请求。`examples/client_rpc.rs` 就是这么用的。

---

### 4.3 Options：阻塞模式与任务池

#### 4.3.1 概念说明

`Options` 控制 `processor()` 如何**调度** handler。默认情况下，每个 handler 都在**后台任务**里并发执行——好处是不阻塞事件循环、吞吐高，坏处是「事件可能乱序处理」。

当你需要**严格顺序**处理时，可以开启阻塞模式：让 handler 在事件循环里**就地同步**执行，一个处理完才处理下一个。但这带来一个致命陷阱——见 4.3.2。

`task_pool` 选项则允许你用一个自定义的 `tokio-task-pool` 池来限流/复用后台任务，而不是无限制地 `tokio::spawn`。

#### 4.3.2 核心流程

三个选项的语义：

| 选项 | 作用 | 取值 |
| --- | --- | --- |
| `blocking_notifications` | 通知帧**就地同步**处理 | `bool` |
| `blocking_frames` | 非 RPC 帧**就地同步**处理 | `bool` |
| `with_task_pool(pool)` | 用自定义任务池 spawn handler | `Option<Arc<Pool>>` |

**阻塞模式的死锁陷阱**（官方注释原文，[src/rpc/async_client.rs:24-34](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L24-L34)）：

> WARNING: when handling frames in blocking mode, it is forbidden to use the current RPC client directly or with any kind of bounded channels, otherwise the RPC client may get stuck!

原因：阻塞模式下，事件循环正在 `await handle_call(...)`，如果 handler 内部又去**调用同一个 RPC client**（比如 `rpc.call(...)`），而这个调用需要**回送回复帧**——但回送帧要经过同一个事件循环处理，而事件循环正卡在当前 handler 上，于是**死锁**。

#### 4.3.3 源码精读

`Options` 结构与建造者方法（[src/rpc/async_client.rs:35-63](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L35-L63)）：

```rust
#[derive(Default, Clone, Debug)]
pub struct Options {
    blocking_notifications: bool,
    blocking_frames: bool,
    task_pool: Option<Arc<Pool>>,
}

impl Options {
    pub fn new() -> Self { Self::default() }
    pub fn blocking_notifications(mut self) -> Self { self.blocking_notifications = true; self }
    pub fn blocking_frames(mut self) -> Self { self.blocking_frames = true; self }
    pub fn with_task_pool(mut self, pool: Pool) -> Self { self.task_pool = Some(Arc::new(pool)); self }
}
```

注意 `task_pool` 的类型 `Arc<Pool>` 来自 `tokio-task-pool` crate（`rpc` feature 引入，见 [Cargo.toml:79](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/Cargo.toml#L79)）。

> 旁注：注意请求（`Request`）**没有** `blocking_call` 选项——请求处理**总是**走后台 `spawn!`（见 4.4）。只有通知和非 RPC 帧可选拖塞。这是有意设计：请求需要回送回复，若就地阻塞处理，回复帧的发送反而会和事件循环争用，更容易出问题；所以请求一律异步。

#### 4.3.4 代码实践

**目标**：理解阻塞模式的适用场景与陷阱边界。

**步骤**：

1. 阅读 [src/rpc/async_client.rs:24-34](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L24-L34) 的完整注释。
2. 构造一个心智实验：假设你用 `Options::new().blocking_frames()` 创建 `RpcClient`，然后在 `handle_frame` 里调用 `rpc.client().lock().await.call(...)`，画出「事件循环等 handler → handler 等回复 → 回复要进事件循环」的循环等待图。
3. 给出两种安全的替代做法：（a）不要在阻塞 handler 里调用本 client；（b）改用默认（非阻塞）模式，让 handler 在独立任务里跑。

**预期结果**：你能复述官方警告——「阻塞模式下禁止直接或通过有界通道使用当前 RPC client」。

> 本实践为心智模型推演型，无需运行（实际复现死锁会卡住进程，不建议在练习中真跑）。

#### 4.3.5 小练习与答案

**练习 1**：为什么请求处理（`Request`）不提供 `blocking_call` 选项？

**答案**：因为请求处理完需要回送回复帧，而回复帧的发送要经过同一个事件循环。若就地阻塞处理请求，处理过程中事件循环被占用，回复帧无法及时送出，极易死锁或严重延迟；所以请求一律走后台 `spawn!`（[src/rpc/async_client.rs:188](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L188)）。

**练习 2**：`with_task_pool` 相比默认的 `tokio::spawn` 有什么实际好处？

**答案**：默认 `tokio::spawn` 对每个 handler 起一个无限制的独立任务，高负载下可能瞬间产生海量任务、内存暴涨且乱序。`tokio-task-pool` 提供容量限制与任务复用，可以**限流**（控制并发上限）并**带 id 标识**（`Task::new(fut).with_id(...)`，见 [src/rpc/async_client.rs:150](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L150)），适合需要背压或指标统计的场景。

---

### 4.4 processor() 事件循环：分发与回复

#### 4.4.1 概念说明

`processor()` 是 `RpcClient` 的**心脏**：一个后台 tokio 任务，不断从底层 `EventChannel` 取帧，按帧类型分发到 handler，并负责**构造回复帧**送回调用方。`RpcClient` 在创建时就 `tokio::spawn` 了它（[src/rpc/async_client.rs:301-307](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L301-L307)）。

它是把上一讲的**协议解析**（`RpcEvent::try_from`）和本讲的**处理器调度**粘合在一起的地方。

#### 4.4.2 核心流程

事件循环主结构（[src/rpc/async_client.rs:159-262](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L159-L262)）：

```text
while let Ok(frame) = rx.recv().await {        // 从事件通道取帧
    if frame.kind() == Message {
        解析为 RpcEvent:
          Notification -> (阻塞?) 就地 / (否则) spawn handle_notification
          Request      -> 取 id；spawn handle_call；
                          id>0 才回复：Ok->[0x11][id:4]+data，Err->[0x12][id:4][code:2]+data
                          按 is_realtime 选 QoS，zc_send 回 sender
          Reply/ErrorReply -> 按 id 查 calls 表，oneshot 兑现；查不到 warn "orphaned"
    } else {  // 非 RPC 帧
        (阻塞?) 就地 / (否则) spawn handle_frame
    }
}
```

请求→回复的完整往返（串起 4.1 和本节）：

```text
[调用方]                            [代理]                         [服务方 processor]
 call(id=7, "add")
   登记 calls[7]=tx
   zc_send 请求帧 ──────────────►  转发 ──────────────────────►  收到 Message
                                                                   解析 Request, id=7
                                                                   spawn handle_call
                                                                   得 Ok(None)
                                                                   构造 [0x11][7:4] 回复
                                                                   zc_send ──────►
 收到 Message(Reply,id=7)  ◄────  转发 ◄──────────────────────
   解析 Reply
   calls.remove(7) -> tx
   tx.send(event)
 rx.await 拿到 result ─────────►  call() 返回 Ok(RpcEvent)
```

#### 4.4.3 源码精读

`spawn!` 宏（[src/rpc/async_client.rs:147-158](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L147-L158)）是调度的核心开关——有 `task_pool` 就用池，否则 `tokio::spawn`：

```rust
macro_rules! spawn {
    ($task_id: expr, $fut: expr) => {
        if let Some(ref pool) = opts.task_pool {
            let task = Task::new($fut).with_id($task_id);
            if let Err(e) = pool.spawn_task(task).await { error!("Unable to spawn RPC task: {}", e); }
        } else {
            tokio::spawn($fut);
        }
    };
}
```

请求处理与回复构造（[src/rpc/async_client.rs:174-234](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L174-L234)），关键点：

```rust
RpcEventKind::Request => {
    let id = event.id();
    // id>0 才需要回复：记下对端名和 client 句柄
    let ev = if id > 0 {
        Some((event.frame().sender().to_owned(), processor_client.clone()))
    } else { None };
    let h = handlers.clone();
    spawn!("rpc.request", async move {
        // 回复 QoS 镜像请求的 realtime 位，保持延迟等级
        let qos = if event.frame().is_realtime() { QoS::RealtimeProcessed } else { QoS::Processed };
        let res = h.handle_call(event).await;
        if let Some((target, cl)) = ev {
            match res {
                Ok(v)  => { /* 回复帧 = [RPC_REPLY][id:4] + v */ }
                Err(e) => { /* 错误帧 = [RPC_ERROR][id:4][code:2] + e.data */ }
            }
            // 用 zc_send 把回复帧送回 target（请求的发起者）
        }
    });
}
```

两个细节值得记住：

1. **回复 QoS 镜像请求**（[src/rpc/async_client.rs:189-193](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L189-L193)）：实时请求的回复也是实时的（`RealtimeProcessed`），非实时则 `Processed`，保证端到端延迟等级一致。
2. **回复帧手工拼接**（[src/rpc/async_client.rs:210-231](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L210-L231)）：`Ok` 走 `RPC_REPLY(0x11)` + id；`Err` 走 `RPC_ERROR(0x12)` + id + 错误码。这正是上一讲约定的帧格式在本讲的「生产侧」落地。

回复帧在**调用方**的兑现（[src/rpc/async_client.rs:235-248](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L235-L248)）：

```rust
RpcEventKind::Reply | RpcEventKind::ErrorReply => {
    let id = event.id();
    if let Some(tx) = { calls.lock().remove(&id) } {
        let _r = tx.send(event);   // 兑现 4.1 里 call() 登记的 oneshot
    } else {
        warn!("orphaned RPC response: {}", id);  // 超时已清理 / 重复回复
    }
}
```

`RpcClient::init` 里的事件循环与心跳启动（[src/rpc/async_client.rs:292-331](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L292-L331)）：它把底层 client 包进 `Arc<Mutex<dyn AsyncClient>>`、`tokio::spawn(processor(...))`，并且**仅当底层 client 提供了 timeout**（即外部 IPC 客户端）时才起 pinger——pinger 每 `timeout/2` 发一次 ping，ping 失败就 `abort` 掉 processor（[src/rpc/async_client.rs:310-321](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L310-L321)）。内部客户端没有 timeout，故不起心跳。

`Drop` 实现（[src/rpc/async_client.rs:428-433](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L428-L433)）会在 `RpcClient` 销毁时 `abort` processor 和 pinger，保证后台任务不泄漏。

#### 4.4.4 代码实践

**目标**：追踪一次 `call` 的完整数据流，把 4.1（调用侧）与 4.4（服务侧）串起来。

**步骤**：

1. 准备一份纸笔，画出三个泳道：**调用方** / **代理** / **服务方 processor**。
2. 从 [src/rpc/async_client.rs:370](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L370) 的 `call()` 出发，标出：登记 `calls[id]`、`zc_send` 请求帧。
3. 跳到 [src/rpc/async_client.rs:174](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L174) 的 `Request` 分支，标出：`handle_call`、构造回复帧、`zc_send` 回 sender。
4. 回到 [src/rpc/async_client.rs:235](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L235) 的 `Reply` 分支，标出：`calls.remove(id)`、`tx.send`。
5. 最后回到 `call()` 的 [第 414 行](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L414) `rx.await`，标出兑现点。

**预期结果**：你得到一张完整的请求-回复时序图，能指出 `calls` 表在哪一行写入、哪一行兑现、哪一行可能 warn「orphaned」。

> 本实践为调用链追踪型，无需运行。

#### 4.4.5 小练习与答案

**练习 1**：什么情况下会触发 `warn!("orphaned RPC response: {}", id)`？

**答案**：当 processor 收到一个 Reply/ErrorReply 帧，但在 `calls` 表里**找不到**对应 id 的 oneshot 时。常见原因：调用方 `call()` 已超时，`unwrap_or_cancel!` 把该 id 的 `tx` 从表里移除了；之后迟到的回复帧到达，就成了「孤儿」（[src/rpc/async_client.rs:243-247](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L243-L247)）。

**练习 2**：为什么 pinger 只对**外部** IPC 客户端启动，内部 `broker::Client` 不启动？

**答案**：pinger 的启动条件是 `timeout.map(...)`（[src/rpc/async_client.rs:310](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L310)），而 `timeout` 来自 `client.get_timeout()`。内部客户端 `get_timeout()` 返回 `None`（见 [u4-l1](u4-l1-async-client-trait.md)），且内部通信在同一进程内永不「断线」，不需要心跳探活；只有经 socket 的外部客户端才有 timeout、才需要 pinger 定期 ping 探活并在失联时 `abort` processor。

---

## 5. 综合实践

把本讲的四个模块串起来，端到端跑通一次 RPC。本实践直接使用官方两个示例，它们正好是「同一套 add/get」的 caller 与 callee。

### 5.1 目标

- 启动一个独立代理 `busrtd`。
- 启动服务端 `client_rpc_handler`（注册名 `test.client.rpc`，提供 `add`/`get`/`test`）。
- 启动客户端 `client_rpc`（调用 `add(10)` 累加，再 `get` 读回）。
- 验证累加结果，并对照源码理解整条调用链。

### 5.2 操作步骤

1. **启动代理**（参照 [test.sh:9-10](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/test.sh#L9-L10)，监听 `/tmp/busrt.sock`）：

   ```sh
   cargo run --release --features server,rpc --bin busrtd -- -B /tmp/busrt.sock
   ```

2. **另开一个终端，启动服务端**（注意需要 `broker-rpc` feature，原因见 4.2.3）：

   ```sh
   cargo run --example client_rpc_handler --features "ipc rpc broker-rpc"
   ```

   预期看到 `Waiting for frames to test.client.rpc`。

3. **再开一个终端，启动客户端**（[examples/client_rpc.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/client_rpc.rs)）：

   ```sh
   cargo run --example client_rpc --features "ipc rpc"
   ```

### 5.3 需要观察的现象

- 客户端先 `call0("test", ...)`（不等回复），再 `call("add", {value:10})`（带 ACK 确认的累加），最后 `call("get", ...)` 读回。
- 客户端终端打印出一个数字（`Amount.value`，见 [examples/client_rpc.rs:38-42](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/client_rpc.rs#L38-L42)）。
- 服务端终端**不应**打印 `add` 相关内容（`add` 返回 `Ok(None)`，只是累加，无通知输出）。

### 5.4 预期结果

- 客户端打印 `10`（首次运行；若不重启服务端再次运行客户端，因服务端 counter 是进程内原子变量、持续累加，会打印 `20`、`30`……——这正好印证 4.2 讲的「counter 是跨调用共享的原子状态」）。

### 5.5 扩展（可选）

把服务端的 `handle_call` 改成同时支持 `sub(value)`（减法），并在客户端加一次 `sub` 调用，重新观察 `get` 的返回。注意：修改后需重新编译服务端示例（`cargo run --example ...` 会自动重编译）。

> 如果你的环境无法编译运行（缺 Rust 工具链或依赖），可退化为「源码阅读型实践」：按 4.4.4 的步骤画时序图，并口述 `add(10)` 这一帧从客户端 `call()` 到服务端 `handle_call` 再到客户端 `get` 读回的完整路径。

## 6. 本讲小结

- `RpcClient` 把底层 `AsyncClient` 包了一层，实现 `Rpc` trait，提供 `notify`（通知 `0x00`）/ `call0`（请求 `0x01`+id=0，不等回复）/ `call`（请求 `0x01`+自增 id，等回复）三种调用。
- `call()` 的核心簿记：自增 `call_id`（回绕到 1，避开 0）、登记 `calls[id]=oneshot`、发送、等 ACK（若 QoS 需要）、等回复、清理。
- `RpcHandlers` trait 有三个回调：`handle_call`（响应请求，返回 `RpcResult`）、`handle_notification`（处理通知）、`handle_frame`（处理非 RPC 帧）；handler 会被并发启动多实例，共享状态必须原子化。
- `processor()` 是 RPC 心脏：取帧 → 解析 `RpcEvent` → 按种类分发；对 `id>0` 的请求构造回复帧（`0x11`/`0x12`）回送，对 Reply 按 id 兑现 oneshot。
- `Options` 的 `blocking_notifications`/`blocking_frames` 提供顺序处理（但有「禁止在阻塞 handler 内调用本 client」的死锁陷阱），`with_task_pool` 提供限流；请求处理一律异步 spawn，无阻塞选项。
- 回复 QoS 镜像请求的 `is_realtime` 位；pinger 只对有 timeout 的外部客户端启动；`Drop` 时 abort 后台任务。

## 7. 下一步学习建议

- **[u5-l3 自定义 Broker RPC 与核心 RPC 接口](u5-l3-custom-broker-rpc.md)**：把本讲的 handlers 挂到**嵌入式 Broker** 上（`set_core_rpc_client`），并学习代理内置的 `.broker` 核心 RPC 方法（`test`/`info`/`stats`/`client.list`）。
- **[u7-l2 游标 cursors](u7-l2-cursors.md)**：在 RPC 之上构建流式数据传输，会用到这里学到的 `Rpc`/`RpcHandlers` 接口。
- **[u7-l4 同步客户端](u7-l4-sync-client.md)**：对照本讲的异步 `Rpc`/`RpcHandlers`，看同步版本 `SyncRpc`/`SyncRpcHandlers` 的接口与阻塞 `Processor::run` 事件循环的取舍。
- 继续阅读源码建议：精读 [src/rpc/async_client.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs) 的 `processor()` 全文，并对照 [src/rpc/mod.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs) 的 `RpcEvent::try_from`，把「解析」与「调度」两侧彻底打通。

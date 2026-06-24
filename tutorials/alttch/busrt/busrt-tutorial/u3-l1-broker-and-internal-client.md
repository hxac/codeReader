# 创建 Broker 与注册内部客户端

## 1. 本讲目标

学完本讲后，你应该能够：

- 理解 BUS/RT 的「嵌入模式」：把代理（broker）直接放进自己的 Tokio 进程，让同一进程内的多个任务通过消息总线通信。
- 用 `Broker::new()` / `Broker::create(&Options)` 创建一个内存中的代理实例。
- 用 `Broker::register_client(name)` 注册一个**内部客户端**，拿到一个 `broker::Client` 句柄。
- 理解内部客户端（`BusRtClientKind::Internal`）与外部 IPC 客户端在行为上的关键差异（背压、超时、连接状态）。
- 能够参照官方 `examples/inter_thread.rs` 写出一个最小的「两个内部客户端互相收发消息」的程序。

本讲不涉及任何 socket、序列化或 RPC，**所有通信都发生在一个进程内的内存通道里**——这是理解 BUS/RT 最纯粹、最快的起点。

## 2. 前置知识

本讲假设你已经掌握了前两单元的内容，尤其是：

- **公共类型契约**（u2-l1）：`Frame = Arc<FrameData>`、`QoS`（`needs_ack` / `is_realtime`）、`EventChannel`、`OpConfirm`。本讲会反复用到它们。
- **零拷贝载荷 `borrow::Cow`**（u2-l2）：内部客户端的发送方法签名是 `payload: Cow<'async_trait>`，发送端会把 `Cow` 通过 `to_vec()` 收成一块完整缓冲。
- **线上协议帧格式**（u2-l3）：你已经知道外部客户端通过 socket 收发 9 字节 / 6 字节帧头。本讲的内部客户端**完全不走这套线上字节流**，但它复用了同样的 `FrameData` 结构来描述一帧消息。

两个需要再强调的小概念：

- **`async_channel::bounded(N)`**：Tokio 生态里有界异步通道，容量为 `N`。生产者往里塞值，消费者从里取值；当缓冲区满时，`send().await` 会**挂起等待**，形成天然的背压（backpressure）。BUS/RT 的每个客户端都有一个这样的入站通道。
- **`Arc<T>`**：原子引用计数的共享指针。BUS/RT 用 `Arc<BusRtClient>` 让「代理内部的客户端表项」和「返回给用户的 `Client` 句柄」共享同一份状态，二者之一被释放都不会立刻销毁底层资源。

## 3. 本讲源码地图

本讲只涉及两个源码文件，体量都很克制：

| 文件 | 作用 |
| --- | --- |
| [src/broker.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs) | 代理核心。定义 `Broker`、`broker::Client`（内部客户端）、`BusRtClient`、`BrokerDb`，以及 `register_client`、`send!` 等分发宏。本讲的全部源码精读都集中在这里。 |
| [examples/inter_thread.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/inter_thread.rs) | 官方的「线程间通信」示例，是本讲代码实践的蓝本。60 行不到，演示了嵌入代理、注册 3 个内部客户端、点对点 `send` 与广播 `send_broadcast`。 |

辅助理解（不在本讲精读范围，但会被引用）：

- [src/client.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/client.rs)：定义了统一的 `AsyncClient` trait，内部客户端 `broker::Client` 就是它的一个实现。这一 trait 将在 u4-l1 详讲。
- [src/lib.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs)：提供 `SECONDARY_SEP`（值为 `"%%"`）、`FrameData` 的访问方法 `sender()` / `payload()` / `topic()` 等。

> 提示：`broker.rs` 整体受 `broker` feature 守卫；`register_client`、`Broker::new/create` 都不需要 `rpc` feature。但官方示例 `inter_thread.rs` 调用了 `init_default_core_rpc()`，所以它的 `required-features` 是 `["broker", "rpc"]`（见 [Cargo.toml:106-108](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/Cargo.toml#L106-L108)）。本讲的代码实践会去掉对 RPC 的依赖，最小只需 `broker` feature。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. **4.1** 创建嵌入式代理：`Broker::new` 与 `Broker::create`
2. **4.2** 注册内部客户端：`register_client` 与 `BusRtClientKind::Internal`
3. **4.3** 内部客户端 `broker::Client`：`AsyncClient` 的线程内实现
4. **4.4** 走通线程间通信：`inter_thread.rs` 示例解析

### 4.1 创建嵌入式代理：Broker::new 与 Broker::create

#### 4.1.1 概念说明

BUS/RT 有两种使用姿势：

- **独立服务模式**：编译出 `busrtd` 二进制常驻，外部进程通过 Unix socket / TCP / WebSocket 连入（u1-l2 已讲）。
- **嵌入模式**：在你自己的 Tokio 程序里直接 `new` 一个 `Broker`，它就是一个普通的 Rust 值，存活在你的进程内存里。

嵌入模式下，同一进程内的多个异步任务可以通过这个代理互相发消息，**完全不经过任何 socket 序列化**——帧直接以 `Arc<FrameData>` 的形式在内存通道里传递。这就是「线程间通信（inter-thread）」这个名字的由来。

> 为什么要这么设计？因为工业物联网场景里，一个节点上常常有很多本地组件需要互相协作。走 socket 要序列化、要拷贝、要握手；而走进程内通道，一帧消息只是一个 `Arc` 引用计数的增减，几乎零成本。嵌入模式让 BUS/RT 既能当「跨进程总线」，也能当「进程内总线」，两种姿势共用同一套分发逻辑。

#### 4.1.2 核心流程

创建代理的流程极简：

1. 调用 `Broker::new()` 或 `Broker::create(&Options)`。
2. 内部构造一个 `BrokerDb`（代理的「注册表 + 计数器」内核），包进 `Arc`。
3. 返回一个 `Broker` 值，它持有 `db: Arc<BrokerDb>`、若干服务句柄、队列大小等配置。

两种构造方式的区别只有一点：`create` 接受 `Options`，可以开启 `force_register`（名字冲突时踢掉旧连接）和实时模式下的异步分配器（`with_async_allocator`，u7-l1 会讲）。`new()` 等价于用默认 `Options` 构造。

#### 4.1.3 源码精读

先看 `Broker` 结构体本身（[src/broker.rs:1049-1055](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1049-L1055)）：

```rust
pub struct Broker {
    db: Arc<BrokerDb>,
    services: Vec<JoinHandle<()>>,
    queue_size: usize,
    direct_alloc_limit: Option<usize>,
    async_allocator: Option<Arc<dyn AsyncAllocator + Send + Sync + 'static>>,
}
```

- `db` 是代理的内核，所有客户端注册表、订阅表、收发计数都在里面。
- `services` 存放各监听服务（unix/tcp/websocket）的 `JoinHandle`，嵌入模式下如果不 spawn 任何 server，它就是空的。
- `queue_size` 是每个客户端入站通道的容量，默认 `DEFAULT_QUEUE_SIZE = 8192`。

两个构造方法（[src/broker.rs:1337-1353](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1337-L1353)）：

```rust
impl Broker {
    pub fn new() -> Self {
        Self::default()
    }
    pub fn create(opts: &Options) -> Self {
        let db = BrokerDb {
            force_register: opts.force_register,
            ..Default::default()
        };
        Self {
            db: Arc::new(db),
            services: <_>::default(),
            queue_size: DEFAULT_QUEUE_SIZE,
            direct_alloc_limit: opts.direct_alloc_limit,
            async_allocator: opts.async_allocator.clone(),
        }
    }
```

`new()` 直接走 `Default`（[src/broker.rs:1295-1305](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1295-L1305)），`force_register` 默认为 `false`。`create()` 把 `Options` 里的配置注入 `BrokerDb` 和 `Broker`。

`BrokerDb` 的内核长这样（[src/broker.rs:658-670](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L658-L670)）：

```rust
struct BrokerDb {
    clients: SyncMutex<HashMap<String, BrokerClient>>,
    broadcasts: SyncMutex<BroadcastMap<BrokerClient>>,
    subscriptions: SyncMutex<SubMap<BrokerClient>>,
    #[cfg(feature = "rpc")]
    rpc_client: Arc<Mutex<Option<RpcClient>>>,
    r_frames: atomic::AtomicU64,   // 收到的总帧数
    r_bytes: atomic::AtomicU64,
    w_frames: atomic::AtomicU64,   // 分发出的总帧数
    w_bytes: atomic::AtomicU64,
    startup_time: Instant,
    force_register: bool,
}
```

三张核心映射（`clients` / `broadcasts` / `subscriptions`）就是代理的全部「路由表」——它们将在 u3-l2 详讲。本讲你只需知道：内部客户端注册后会被放进 `clients`。

#### 4.1.4 代码实践

1. **实践目标**：建立「代理是一个普通 Rust 值」的直觉。
2. **操作步骤**：在本机克隆 busrt 仓库后，直接阅读 [src/broker.rs:1337-1353](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1337-L1353)，确认 `new` 与 `create` 的差异仅来自 `Options`。
3. **需要观察的现象**：`create` 没有 `mut self`，它消费 `&Options` 产生一个全新的 `Broker`。
4. **预期结果**：理解 `new()` ≡ `create(&Options::default())`，唯一的差别是 `Options` 能打开 `force_register` 与实时分配器。

#### 4.1.5 小练习与答案

**练习 1**：如果想让两个内部客户端用同一个名字注册、后注册的踢掉前一个，应该用 `new()` 还是 `create()`？

**参考答案**：用 `create(&Options::default().force_register(true))`。`force_register` 会被注入到 `BrokerDb`，注册时遇到名字冲突（`ErrorKind::Busy`）就触发踢出逻辑。

**练习 2**：`Broker::new()` 返回的 `Broker` 是否需要 `mut`？

**参考答案**：取决于后续调用。`register_client`、`info`、`stats`、`announce` 等都只取 `&self`，不需要 `mut`；但 `spawn_unix_server` / `spawn_tcp_server` 等会向 `services` 推入句柄，签名是 `&mut self`。所以只做内部客户端通信时，`let broker = Broker::new();` 即可（无需 `mut`）。

### 4.2 注册内部客户端：register_client 与 BusRtClientKind::Internal

#### 4.2.1 概念说明

光有代理还不够，要发消息必须先「注册一个客户端」。注册会做两件事：

1. 在代理内核里建一个客户端表项（`BusRtClient`），其中包含一个**有界入站通道** `tx: async_channel::Sender<Frame>`——别人发给你的消息都从这条通道进来。
2. 返回给调用者一个轻量句柄 `broker::Client`，它持有 `Arc<BusRtClient>`（共享表项）和通道的接收端 `rx: EventChannel`。

`BusRtClientKind` 区分了四种客户端来源（[src/broker.rs:498-504](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L498-L504)）：`Internal`（本讲的内部客户端）、`LocalIpc`（Unix socket）、`Tcp`、`WebSocket`。本讲的内部客户端就是 `Internal`。

#### 4.2.2 核心流程

`Broker::register_client(name)` 的内部步骤（对应 [src/broker.rs:1394-1415](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1394-L1415)）：

1. 从 `name` 里解析出**主名**（primary name）：若 `name` 含 `%%`（`SECONDARY_SEP`），取分隔符之前的部分；否则整个 `name` 即主名。
2. 用 `BusRtClient::new(...)` 创建表项，`kind` 固定为 `BusRtClientKind::Internal`，通道容量取 `self.queue_size`（默认 8192）。
3. 把表项交给 `BrokerDb::register_client`，后者调用 `insert_client` 写入 `clients` 映射，并向广播表、订阅表注册（还会自动订阅 `.broker/warn`）。
4. 返回一个 `broker::Client`，内含 `Arc<BusRtClient>`、`Arc<BrokerDb>`、入站通道接收端 `rx`。

其中 `BusRtClient::new` 创建有界通道并装配各种统计计数器（[src/broker.rs:546-585](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L546-L585)）：

```rust
let digest = submap::digest::sha256(name);
let (tx, rx) = async_channel::bounded(queue_size);   // 有界入站通道
let primary = name == primary_name;
let (disconnect_trig, disconnect_listener) = triggered::trigger();
```

注意 `digest = sha256(name)`：客户端的身份比较是基于名字的 SHA-256 摘要，`PartialEq`/`Ord` 都比的是 `digest`（[src/broker.rs:587-605](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L587-L605)），这样不同长度的名字也能在 `HashSet` / `BTreeMap` 里稳定排序与去重。

#### 4.2.3 源码精读

公开入口 `Broker::register_client`（[src/broker.rs:1394-1415](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1394-L1415)）：

```rust
pub async fn register_client(&self, name: &str) -> Result<Client, Error> {
    let client_primary_name = name
        .find(SECONDARY_SEP)
        .map_or_else(|| name, |pos| &name[..pos]);
    let (c, rx, _) = BusRtClient::new(
        name,
        client_primary_name,
        self.queue_size,
        BusRtClientKind::Internal,
        None,
        None,
    );
    let client = Arc::new(c);
    self.db.register_client(client.clone()).await?;
    Ok(Client {
        name: name.to_owned(),
        bus: client,
        db: self.db.clone(),
        rx: Some(rx),
        secondary_counter: atomic::AtomicUsize::new(0),
    })
}
```

底层 `BrokerDb::register_client`（[src/broker.rs:725-756](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L725-L756)）负责把表项塞进 `clients`，并在 `force_register` 开启、名字冲突时踢掉旧实例：

```rust
async fn register_client(&self, client: Arc<BusRtClient>) -> Result<(), Error> {
    let name = client.name.clone();
    // ...
    match self.insert_client(client.clone()) {
        Ok(()) => {}
        Err(e) if e.kind() == ErrorKind::Busy && allow_force => {
            let prev_c = self.clients.lock().remove(&name);
            if let Some(prev) = prev_c {
                warn!("disconnecting previous instance of {}", name);
                self.drop_client(&prev);
                prev.disconnect_trig.trigger();
            }
            self.insert_client(client)?;
        }
        Err(e) => return Err(e),
    }
    // ...
}
```

真正写表的动作在 `insert_client`（[src/broker.rs:757-791](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L757-L791)）：若 `clients` 里该名字对应的 `Entry` 是 `Vacant`（空闲），就插入并把 `registered` 标志置真，同时向广播表、订阅表登记，并**自动订阅 `.broker/warn`** 主题；如果名字已存在，返回 `Error::busy(...)`。

#### 4.2.4 代码实践

1. **实践目标**：亲手注册两个内部客户端，观察名字冲突时的错误类型。
2. **操作步骤**：在 `examples/` 下新建 `my_register.rs`，写一个最小程序注册 `"a"` 两次（不开 `force_register`），打印第二次的结果。
3. **需要观察的现象**：第二次 `register_client("a")` 返回的 `Result`。
4. **预期结果**：第二次返回 `Err`，其 `kind()` 为 `ErrorKind::Busy`（对应 u2-l1 讲过的线上错误码体系）。
5. 如果无法确定运行结果，明确写「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：注册名字 `"worker.1%%0"` 时，客户端的**主名**是什么？它是 primary 还是 secondary？

**参考答案**：主名是 `"worker.1"`（取 `%%` 之前的部分）。因为完整 `name`（`"worker.1%%0"`）不等于主名，所以 `primary = false`，它是 secondary 客户端。

**练习 2**：为什么客户端的身份比较要用 `sha256(name)` 摘要而不是直接比 `String`？

**参考答案**：因为客户端会被放进 `HashSet`/`BTreeMap`（订阅表、广播表），用固定长度的摘要做 `PartialEq`/`Ord`/`Hash` 既稳定又避免了长名字的反复比较开销；同时摘要在订阅掩码匹配（`submap` 库）里也被用作去重键。

### 4.3 内部客户端 broker::Client：AsyncClient 的线程内实现

#### 4.3.1 概念说明

`broker::Client` 是返回给你的那个句柄（[src/broker.rs:250-256](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L250-L256)）。它实现了统一的 `AsyncClient` trait（[src/client.rs:9-66](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/client.rs#L9-L66)），所以它和未来的 IPC 客户端（u4）用的是**同一套方法名**：`send` / `send_broadcast` / `publish` / `subscribe` / `ping`……这意味着你写代码时不必关心对方是进程内客户端还是远端客户端，接口一致。

但实现细节有本质区别。内部客户端的几个关键特征：

- **没有连接概念**：`is_connected()` 永远返回 `true`，`ping()` 直接 `Ok(())`，`get_timeout()` 返回 `None`，`get_connected_beacon()` 返回 `None`。
- **背压而非丢弃**：当入站通道满了，内部客户端会让发送方**阻塞等待**，而外部 IPC 客户端满了会被**强制注销**。
- **`header` 字段被使用**：在线程内通信里，`FrameData.header` 可以携带元数据而零拷贝（IPC 通信里 header 一律为 `None`，详见 `FrameData::header()` 的注释 [src/lib.rs:488-494](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L488-L494)）。

#### 4.3.2 核心流程

以点对点 `send` 为例（[src/broker.rs:348-368](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L348-L368)），一帧消息从发送到投递的路径：

1. `Client::send(target, payload, qos)` 把 `payload`（一个 `Cow`）通过 `payload.to_vec()` 收成一块 `Vec<u8>`。
2. 调用 `send!` 宏（[src/broker.rs:111-143](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L111-L143)）：在 `db.clients` 里按 `target` 名字查到目标客户端，构造一个 `Arc<FrameData>`（`kind = Message`、`sender = 自己的名字`），更新收发字节计数。
3. 调用 `safe_send_frame!` 宏（[src/broker.rs:83-109](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L83-L109)）把这一帧塞进目标客户端的 `tx` 通道。
4. 目标客户端的接收端（`rx`，即 `EventChannel`）拿到这帧，调用 `frame.sender()` / `frame.payload()` 读取。

关于背压的判断，`safe_send_frame!` 里有一段关键分支（[src/broker.rs:83-109](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L83-L109)）：当 `tx.is_full()` 时，**内部客户端**会带可选超时地 `tx.send($frame).await`（阻塞等待空位），而**外部客户端**则 `unregister_client` 后关闭通道、返回 `Error::not_delivered()`。

关于 QoS：内部客户端的 `send` 把 `qos.is_realtime()` 传给 `FrameData.realtime`，并通过 `make_confirm_channel!`（[src/broker.rs:71-81](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L71-L81)）决定是否返回一个已经「预兑现」的确认通道——注意内部客户端的确认是**立刻成功**的（它不像外部客户端要等代理回 `OP_ACK`），因为投递本身就是把 `Arc` 塞进通道，没有网络往返。

关于背压的吞吐含义：广播 / 发布是扇出（fan-out）操作，一帧要发给 `k` 个订阅者，因此写计数按订阅者数量倍增：

\[
\text{w\_bytes} \mathrel{+}= \text{len} \times k
\]

这条式子对应 `send_broadcast!` / `publish!` 宏里的 `fetch_add(len * subs.len())`（[src/broker.rs:165-168](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L165-L168) 与 [src/broker.rs:202-205](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L202-L205)）。

#### 4.3.3 源码精读

`broker::Client` 结构体（[src/broker.rs:250-256](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L250-L256)）：

```rust
pub struct Client {
    name: String,
    bus: Arc<BusRtClient>,      // 共享的内核表项
    db: Arc<BrokerDb>,          // 共享代理内核（用于查路由表）
    rx: Option<EventChannel>,   // 入站通道接收端，可被 take 走
    secondary_counter: atomic::AtomicUsize,
}
```

最能体现「内部客户端」身份的几个 `AsyncClient` 方法（[src/broker.rs:456-479](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L456-L479)）：

```rust
fn take_event_channel(&mut self) -> Option<EventChannel> { self.rx.take() }
async fn ping(&mut self) -> Result<(), Error> { Ok(()) }
fn is_connected(&self) -> bool { true }
fn get_timeout(&self) -> Option<Duration> { None }
fn get_connected_beacon(&self) -> Option<Arc<atomic::AtomicBool>> { None }
fn get_name(&self) -> &str { self.name.as_str() }
```

点对点 `send`（[src/broker.rs:348-368](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L348-L368)）：

```rust
async fn send(
    &mut self, target: &str, payload: Cow<'async_trait>, qos: QoS,
) -> Result<OpConfirm, Error> {
    let len = payload.len() as u64;
    send!(self.db, self.bus, target, None, payload.to_vec(), 0, len,
          qos.is_realtime(), self.get_timeout())?;
    make_confirm_channel!(qos)
}
```

注意 `self.get_timeout()` 对内部客户端是 `None`，因此传进 `safe_send_frame!` 后，**满队列时内部发送方会无限阻塞**——这正是进程内背压的来源。

生命周期方面，`broker::Client` 实现了 `Drop`（[src/broker.rs:492-496](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L492-L496)）：句柄被释放时自动从代理内核注销（调用 `drop_client`，[src/broker.rs:817-841](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L817-L841)），但**不会发送 announce**。源码注释建议你优先显式调用 `unregister()`（[src/broker.rs:482-490](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L482-L490)）。

#### 4.3.4 代码实践

1. **实践目标**：对比内部客户端与「一个想象中的外部客户端」在满队列时的行为差异。
2. **操作步骤**：阅读 `safe_send_frame!`（[src/broker.rs:83-109](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L83-L109)），找到 `BusRtClientKind::Internal` 分支与 `else`（外部）分支。
3. **需要观察的现象**：内部分支用 `tx.send($frame).await`（带可选 `time::timeout`），外部分支用 `unregister_client` + `tx.close()` + `Error::not_delivered()`。
4. **预期结果**：理解「内部客户端的背压靠阻塞、外部客户端的背压靠断连」这一设计取舍。

#### 4.3.5 小练习与答案

**练习 1**：为什么内部客户端的 `ping()` 直接返回 `Ok(())`，而 IPC 客户端的 `ping()` 需要真正发一帧 `PING_FRAME`？

**参考答案**：内部客户端和代理同在一个进程，不存在「连接是否存活」的网络问题，`is_connected()` 恒为 `true`，所以 `ping` 没有实际语义；IPC 客户端需要用 ping 探测远端代理是否还在线（u2-l3 讲过 `PING_FRAME` 是 9 字节全零帧）。

**练习 2**：如果接收端任务一直不消费 `rx`，发送端用 `QoS::No` 持续 `send`，最终会怎样？

**参考答案**：目标客户端的入站通道（容量 8192）会被填满，之后 `safe_send_frame!` 走 `Internal` 分支，因 `get_timeout()` 为 `None`，`tx.send($frame).await` **无限阻塞**，发送端被背压挡住，停止继续注入消息。

### 4.4 走通线程间通信：inter_thread.rs 示例解析

#### 4.4.1 概念说明

官方示例 [examples/inter_thread.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/inter_thread.rs) 把前面三个模块串成一个可运行程序：创建代理 → 注册 3 个内部客户端 → 用 `tokio::spawn` 起两个发送循环（点对点 + 广播）→ 主任务在接收端循环打印。它同时还 `spawn_unix_server` 开了一个对外 socket，但那不是本讲重点。

#### 4.4.2 核心流程

```
main
 ├─ Broker::new()                      // 创建代理
 ├─ broker.init_default_core_rpc()     // 可选：挂载 .broker 内置 RPC（u5-l3）
 ├─ broker.spawn_unix_server(...)      // 可选：对外开放（u6-l2）
 ├─ register_client("worker.1")        // 发送方
 ├─ register_client("worker.2")        // 接收方（取走 rx）
 ├─ register_client("worker.3")        // 广播方
 ├─ take_event_channel() -> rx         // worker.2 的入站通道
 ├─ spawn: worker.1 每秒 send("worker.2", "hello")
 ├─ spawn: worker.3 每秒 send_broadcast("worker.*", ...)
 └─ while let Ok(frame) = rx.recv():   // worker.2 收消息并打印 sender/payload
```

#### 4.4.3 源码精读

创建并注册（[examples/inter_thread.rs:13-28](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/inter_thread.rs#L13-L28)）：

```rust
let mut broker = Broker::new();
broker.init_default_core_rpc().await?;
broker.spawn_unix_server("/tmp/busrt.sock", ServerConfig::default()).await?;
let mut client1 = broker.register_client("worker.1").await?;
let mut client2 = broker.register_client("worker.2").await?;
let mut client3 = broker.register_client("worker.3").await?;
let rx = client2.take_event_channel().unwrap();
```

发送循环（点对点与广播，[examples/inter_thread.rs:29-50](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/inter_thread.rs#L29-L50)）：

```rust
tokio::spawn(async move {
    loop {
        client1.send("worker.2", "hello".as_bytes().into(), QoS::No).await.unwrap();
        sleep(SLEEP_STEP).await;
    }
});
tokio::spawn(async move {
    loop {
        client3.send_broadcast("worker.*", "this is a broadcast message".as_bytes().into(), QoS::No).await.unwrap();
        sleep(SLEEP_STEP).await;
    }
});
```

接收循环（[examples/inter_thread.rs:51-57](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/inter_thread.rs#L51-L57)）：

```rust
while let Ok(frame) = rx.recv().await {
    println!("{}: {}",
        frame.sender(),
        std::str::from_utf8(frame.payload()).unwrap_or("something unreadable"));
}
```

注意 `"hello".as_bytes().into()` 这个 `.into()`：它把 `&[u8]` 转成 `borrow::Cow<'_, u8>`（u2-l2 讲过的 `Borrowed` 变体），零拷贝地交给 `send`。

#### 4.4.4 代码实践

1. **实践目标**：跑通官方 inter_thread 示例，亲眼看到线程间消息投递。
2. **操作步骤**：在仓库根目录执行 `cargo run --example inter_thread --features broker,rpc`（也可用项目自带的 `./test.sh server` 启动一个等价服务端）。
3. **需要观察的现象**：终端每秒打印一行，sender 在 `worker.1`（点对点 hello）和 `worker.3`（广播）之间交替。
4. **预期结果**：看到形如 `worker.1: hello` 与 `worker.3: this is a broadcast message` 的循环输出。若同时用 `busrt` CLI 连 `/tmp/busrt.sock` 并取个 `worker.N` 的名字，还能收到 worker.3 的广播。
5. 如果无法确定运行结果，明确写「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：示例里 `client2` 同时收到了 worker.1 的点对点消息和 worker.3 的广播消息，它是怎么「订阅」到广播的？

**参考答案**：`send_broadcast("worker.*", ...)` 用广播掩码 `worker.*` 匹配所有名字以 `worker.` 开头的客户端（广播表用 `.` 作分隔符、`*` 作通配符，见 `BrokerDb` 的 `Default`，[src/broker.rs:676-681](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L676-L681)）。worker.2 注册时已自动登记进广播表，因此匹配命中——它**不需要显式 subscribe** 就能收广播（广播与 pub/sub 是两种不同的模式，u3-l3 会详讲）。

**练习 2**：如果删掉 `client2.take_event_channel().unwrap()` 这一行会怎样？

**参考答案**：`rx` 留在 `client2` 内部而不会被消费，入站通道会逐渐填满；当超过 8192 后，发送方（worker.1 / worker.3）会被背压阻塞在 `tx.send().await`，从而停止打印。`take_event_channel()` 的意义就是把接收端「拿走」交给一个专门的消费循环。

## 5. 综合实践

把本讲的知识串起来，写一个**最小化的双客户端 ping 程序**（不依赖 socket、不依赖 RPC）：

> 任务：注册两个内部客户端 `worker.1`（发送方）和 `worker.2`（接收方）。让 worker.1 每秒向 worker.2 点对点 `send` 一条带序号的消息 `"hello #N"`；worker.2 收到后打印 `sender` 与 `payload`。运行约 5 秒后正常退出。

下面是一个可参考的完整实现（**示例代码**，可直接作为 `examples/my_inter_thread.rs`）：

```rust
// 示例代码：最小化的双客户端线程间通信
use busrt::broker::Broker;
use busrt::client::AsyncClient;   // send / take_event_channel 来自这个 trait
use busrt::QoS;
use std::time::Duration;
use tokio::time::sleep;

const SLEEP_STEP: Duration = Duration::from_secs(1);

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    // 1. 创建嵌入式代理（进程内，不走任何 socket）
    let broker = Broker::new();
    // 2. 注册发送方与接收方两个内部客户端
    let mut sender = broker.register_client("worker.1").await?;
    let mut receiver = broker.register_client("worker.2").await?;
    // 3. 取出接收方的入站通道，交给主循环消费
    let rx = receiver.take_event_channel().unwrap();

    // 4. 发送方每秒向 worker.2 点对点发一条带序号的消息
    tokio::spawn(async move {
        let mut n: u64 = 0;
        loop {
            n += 1;
            let msg = format!("hello #{}", n);
            // "hello #N".as_bytes().into() 把 &[u8] 零拷贝转成 borrow::Cow
            sender
                .send("worker.2", msg.as_bytes().into(), QoS::No)
                .await
                .unwrap();
            sleep(SLEEP_STEP).await;
        }
    });

    // 5. 接收方循环：打印 sender 与 payload
    while let Ok(frame) = rx.recv().await {
        println!(
            "{}: {}",
            frame.sender(), // -> &str，本例恒为 "worker.1"
            std::str::from_utf8(frame.payload()).unwrap_or("something unreadable")
        );
    }
    Ok(())
}
```

**如何运行**：

- 放进 busrt 仓库的 `examples/` 目录后，在 `Cargo.toml` 追加一段（最小仅需 `broker` feature）：

  ```toml
  [[example]]
  name = "my_inter_thread"
  required-features = ["broker"]
  ```

- 然后执行 `cargo run --example my_inter_thread --features broker`。

**验证要点**：

1. 终端每秒打印一行 `worker.1: hello #N`，`N` 递增。
2. 把发送方的 `QoS::No` 换成 `QoS::Processed`：因为内部客户端的确认在 `make_confirm_channel!` 里是**立刻兑现**的（无网络往返），打印频率不会变化，但 `send` 的返回值会带一个已就绪的 `OpConfirm`（u2-l1）。
3. 故意不让接收端消费（删掉 `take_event_channel` 的消费循环），观察发送端在约 8192 条之后被背压挡住——验证 4.3 节讲的「内部客户端满队列阻塞」。
4. 若 `examples/` 放置或 `Cargo.toml` 改动方式不确定，运行结果「待本地验证」。

> 进阶：把上面程序改成三个客户端，让 `worker.3` 用 `send_broadcast("worker.*", ...)` 每 2 秒广播一次，验证 worker.2 能同时收到点对点和广播两类消息（这正是官方 inter_thread 示例的形态）。

## 6. 本讲小结

- **嵌入模式**下，`Broker` 只是一个普通 Rust 值，`Broker::new()` / `Broker::create(&Options)` 创建它，内核 `BrokerDb` 装着三张路由表（clients / broadcasts / subscriptions）和收发计数器。
- `Broker::register_client(name)` 注册一个 `BusRtClientKind::Internal` 的内部客户端，返回 `broker::Client` 句柄；名字含 `%%` 时取分隔符前为主名，secondary 客户端的主名逻辑由此而来。
- 内部客户端通过有界通道 `async_channel::bounded(8192)` 收消息，`take_event_channel()` 拿走接收端交给消费循环。
- `broker::Client` 实现了统一 `AsyncClient` trait，但 `is_connected` 恒真、`ping` 空操作、`get_timeout` 为 `None`——**没有连接概念**。
- 满队列时内部客户端**阻塞发送方**（背压），而外部 IPC 客户端会被**强制断连**，这是两种客户端最关键的行为差异（`safe_send_frame!`）。
- `broker::Client` 被丢弃时自动从内核注销，但不发 announce；推荐显式 `unregister()`。

## 7. 下一步学习建议

本讲把「进程内通信」走通了，但三张路由表、secondary 客户端、广播/订阅掩码这些机制只点到了名字。建议接下来：

- **u3-l2 BrokerDb：客户端注册表与订阅映射**：精读 `BrokerDb`、`BusRtClient`、`register_client`/`insert_client`/`drop_client` 的全部细节，理解主/二级客户端关系与 `force_register` 冲突处理。
- **u3-l3 三种通信模式：send、broadcast 与 publish**：精读 `send!` / `send_broadcast!` / `publish!` 三个分发宏，理解点对点、广播掩码（`?`/`*`）、订阅掩码（`+`/`#`）与 `exclude` 排除机制的区别。
- 之后再进入 **u4 IPC 客户端**，对比外部客户端如何用同一套 `AsyncClient` trait 走 socket 实现连接、握手与帧编解码。

# Primitives 与 Mux/DeMux

## 1. 本讲目标

上一讲《u7-l1 Session 内部与 Runtime》我们已经下钻到 `net/` 层，看到公开 `Session` 如何委托给 `SessionInner`，以及 `Runtime` 如何承载整节点的连接状态。本讲要回答一个更尖锐的问题：

> 当你调用一次 `publisher.put(...)`，这份数据「从你的应用代码」走到「网线上的字节」，中间到底穿过了哪些组件？反方向，对端发来的字节又是如何回到你的 `subscriber.recv_async()`？

读完本讲，你应当能够：

1. 说清 `Primitives` 与 `EPrimitives` 这**两张投递契约**各自服务谁、为何要分两张。
2. 说清 `DeMux` 如何把「入站网络消息」分用（demultiplex）进路由器，`Mux` 如何把「出站路由消息」多路复用（multiplex）到传输层。
3. 画出一条 `Put` 消息从本端 API 到对端 API 的**完整双向流转图**，并标注每一步经过的 `Primitives / Mux / DeMux / Face` 组件。

本讲是后续《u8 路由（HAT 拓扑）》与《u9 传输与链路层》的「十字路口」——它把路由层和传输层缝合在一起。

## 2. 前置知识

本讲假设你已掌握《u7-l1》的结论，特别是这几个概念：

- **`Session` 与 `SessionInner`**：公开 `Session` 是 `Arc<SessionInner>` 薄壳；`SessionInner` 持有 `runtime` 和 `state: RwLock<SessionState>`，并在 `state` 里以 `Option<Arc<dyn Primitives>>` 持有「出站出口」。
- **`net/` 是分界线**：稳定 API 与内部实现的边界，`Primitives` trait 把出站动作收口，API 层从不直接碰 socket。
- **`Face`（会话面）**：`Runtime::init` 经 `runtime.new_primitives → Gateway::new_session` 开出的一张 `Face`，它「双向使用」——对 `Session` 是出站 `Primitives`，对路由器持有入站回调。

此外，请回忆协议消息的分层（《u10-l1》会展开，这里只需直觉）：Zenoh 的网络层消息主要有 `Declare`（声明订阅/可查询/表达式）、`Push`（推送数据，即 Pub/Sub 的载体）、`Request`/`Response`/`ResponseFinal`（查询/应答）、`Interest`（声明式兴趣）。本讲会把它们当作「要搬运的货物」，不展开字段。

> 术语提示：**Mux = Multiplexer（多路复用器）**，把多路来源的消息合并到一条传输通道上发出去；**DeMux = Demultiplexer（分用器）**，把一条传输通道上收到的消息按类型分发到不同的处理逻辑。这两个词来自通信领域，Zenoh 借用它们描述「出站汇聚」与「入站分流」。

## 3. 本讲源码地图

本讲聚焦三个文件，它们都在 `zenoh/src/net/primitives/` 下：

| 文件 | 作用 |
| --- | --- |
| [zenoh/src/net/primitives/mod.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/primitives/mod.rs) | 定义两张投递契约 `Primitives`（出站到路由）与 `EPrimitives`（路由到出口），以及空实现 `DummyPrimitives`。 |
| [zenoh/src/net/primitives/demux.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/primitives/demux.rs) | `DeMux`：实现传输层入站回调 `TransportPeerEventHandler`，把网络消息分用进 `Face`（路由）。 |
| [zenoh/src/net/primitives/mux.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/primitives/mux.rs) | `Mux` / `McastMux`：实现 `EPrimitives`，把路由层送来的消息多路复用到一个 `TransportUnicast` / `TransportMulticast` 上发出。 |

为了讲清「组件如何被拼装」，还会引用几个缝合点：

| 文件 | 作用 |
| --- | --- |
| [zenoh/src/net/routing/dispatcher/face.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/face.rs) | `Face` 既是路由入口（实现 `Primitives`），又持有 `FaceState.primitives: Arc<dyn EPrimitives>` 作为出口。 |
| [zenoh/src/net/routing/gateway.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/gateway.rs) | `new_session` / `new_transport_unicast`：把 `Mux` 与 `DeMux` 用同一张 `Face` 缝合起来。 |
| [zenoh/src/net/routing/dispatcher/pubsub.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/pubsub.rs) | `route_data`：Pub/Sub 数据的「路由函数」，出站和入站都会经过它。 |

## 4. 核心概念与源码讲解

### 4.1 Primitives / EPrimitives：两张「投递契约」

#### 4.1.1 概念说明

先把最关键的直觉记在心里——**一条消息永远走「源 → Face(路由) → 目标 Face 的出口」这三段路**：

- **入口（`Primitives`）**：把消息交进路由织网（routing fabric）。谁来调用？本端是 API `Session`（要发出去），对端是 `DeMux`（刚从网线收进来）。两者都调用 `Face` 上的 `Primitives` 方法。
- **出口（`EPrimitives`）**：把消息送出某张 `Face`。对于传输 face，出口是 `Mux`（送上链路）；对于本地 face，出口是 `WeakSession`（送回本地订阅者回调）。

于是出现两张 trait 并非重复，而是**职责正交**：

| 维度 | `Primitives` | `EPrimitives` |
| --- | --- | --- |
| 服务对象 | 本地 API `Session`（出站）与 `DeMux`（入站） | 路由织网向某张 face 投递 |
| 可见性 | `pub`（net 模块公开） | `pub(crate)`（内部） |
| 消息包装 | 裸消息 `&mut Push` 等 | `Declare`/`Interest` 包在 `RoutingContext` 里 |
| 返回值 | `()`（无返回） | `bool`（是否真的发出，便于拦截器/超时处理） |
| 额外方法 | `send_close`、`send_push_consume(.., consume)` | 无 `send_close`，无 `consume` |

为什么 `EPrimitives` 要返回 `bool`？因为路由器需要知道「这条消息到底有没有被发出去」——如果被拦截器（interceptor）挡掉了，路由器要补发一个 `ResponseFinal`/`DeclareFinal`，否则请求方会一直傻等到超时。这一点在 4.2、4.3 会看到具体落点。

#### 4.1.2 核心流程

两张契约的方法几乎一一对应，覆盖了全部网络消息类型：

```
Primitives（入口契约）           EPrimitives（出口契约）
──────────────────────           ──────────────────────
send_interest                    send_interest   -> bool
send_declare                     send_declare    -> bool
send_push_consume(.., consume)   send_push       -> bool
send_push   (= consume=true)     ─
send_request                     send_request    -> bool
send_response                    send_response   -> bool
send_response_final              send_response_final -> bool
send_close                       ─
```

`RoutingContext<Msg>` 是 `EPrimitives` 的「信封」，内部只有一个懒计算的完整 key expression：

```rust
pub(crate) struct RoutingContext<Msg> {
    pub(crate) msg: Msg,
    pub(crate) full_expr: OnceCell<String>,
}
```

它把「消息体」和「这条消息对应的完整 key 表达式」绑在一起，供拦截器做匹配（拦截器需要知道完整 key 才能判断要不要放行）。

#### 4.1.3 源码精读

两张 trait 的定义在 mod.rs，紧挨着：

[zenoh/src/net/primitives/mod.rs:28-50](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/primitives/mod.rs#L28-L50) —— **`Primitives`：入口契约**。注意 `send_push` 是带默认实现的语法糖，等价于 `send_push_consume(.., true)`：

```rust
pub trait Primitives: Send + Sync {
    fn send_interest(&self, msg: &mut Interest);
    fn send_declare(&self, msg: &mut Declare);
    fn send_push_consume(&self, msg: &mut Push, reliability: Reliability, consume: bool);
    #[inline(always)]
    fn send_push(&self, msg: &mut Push, reliability: Reliability) {
        self.send_push_consume(msg, reliability, true)
    }
    fn send_request(&self, msg: &mut Request);
    fn send_response(&self, msg: &mut Response);
    fn send_response_final(&self, msg: &mut ResponseFinal);
    fn send_close(&self);
    fn as_any(&self) -> &dyn Any;
}
```

[zenoh/src/net/primitives/mod.rs:52-66](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/primitives/mod.rs#L52-L66) —— **`EPrimitives`：出口契约**。`send_declare`/`send_interest` 收的是 `RoutingContext<&mut ..>`，其余收裸消息，全部返回 `bool`：

```rust
pub(crate) trait EPrimitives: Send + Sync {
    fn as_any(&self) -> &dyn Any;
    fn send_interest(&self, ctx: RoutingContext<&mut Interest>) -> bool;
    fn send_declare(&self, ctx: RoutingContext<&mut Declare>) -> bool;
    fn send_push(&self, msg: &mut Push, reliability: Reliability) -> bool;
    fn send_request(&self, msg: &mut Request) -> bool;
    fn send_response(&self, msg: &mut Response) -> bool;
    fn send_response_final(&self, msg: &mut ResponseFinal) -> bool;
}
```

`as_any()` 在两张 trait 里都出现，是「类型擦除后再认回来」的口子——`FaceState` 正是靠它把 `Arc<dyn EPrimitives>` `downcast` 回 `Mux`/`McastMux`，从而读出拦截器链（见 4.3）。

[zenoh/src/net/primitives/mod.rs:68-119](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/primitives/mod.rs#L68-L119) —— **`DummyPrimitives`**：两张 trait 的空实现。`Primitives` 的方法全是空函数体，`EPrimitives` 的方法一律返回 `false`（「我什么都没发出去」）。它用在「只收不发」的 face 上（如多播对端 face），避免 `Option<dyn EPrimitives>` 的解包烦恼。

#### 4.1.4 代码实践

**实践目标**：用眼睛走一遍「两张契约的对照」，建立肌肉记忆。

**操作步骤**：

1. 打开 [zenoh/src/net/primitives/mod.rs:28-66](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/primitives/mod.rs#L28-L66)。
2. 在纸上画一张两列对照表：左列 `Primitives` 的每个方法，右列 `EPrimitives` 对应方法。
3. 标出三处「不对称」：① `Primitives` 多了 `send_push_consume` 的 `consume` 形参与 `send_push` 默认实现；② `Primitives` 多了 `send_close`；③ `EPrimitives` 的 `send_declare`/`send_interest` 多了 `RoutingContext` 信封、且全部返回 `bool`。

**需要观察的现象**：两张 trait 的方法名几乎一致，差异全部集中在「谁来调用、要不要回话」。

**预期结果**：你能脱稿说出「`Primitives` = 入口（路由进），`EPrimitives` = 出口（路由出），返回 bool 是为了拦截器/超时兜底」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `EPrimitives::send_push` 不需要 `consume` 参数，而 `Primitives::send_push_consume` 需要？

> **参考答案**：`consume` 控制「路由转发时是否可以就地消费（move）这条消息」。入口 `Primitives` 由路由器内部调用，可能要把同一条消息转发给多个下游 face，因此需要用 `consume` 区分「只剩一个下游时可直接 move」与「多下游时先 clone」。出口 `EPrimitives` 是终点站（送上网线或送给本地订阅者），消息送出即结束，无需再谈复用，故无此参数。

**练习 2**：`DummyPrimitives` 的 `EPrimitives` 实现为什么全部返回 `false`？

> **参考答案**：`bool` 表示「这条消息是否真的被发出」。`DummyPrimitives` 用于只收不发（或尚无真实出口）的 face，它什么都不会发，所以诚实地返回 `false`，让路由器知道这次投递没成功，避免误判。

---

### 4.2 DeMux：入站分用（网络 → 路由）

#### 4.2.1 概念说明

传输层（`zenoh-transport`）从一条链路上连续收到字节帧，解码成一条条 `NetworkMessageMut`（网络消息）。但传输层**不该知道**这些消息要送去哪——那是路由层的事。于是传输层定义了一个极简的回调接口 `TransportPeerEventHandler`，只问一句：「我收到一条消息，怎么办？」

`DeMux` 就是这个回调的**路由器侧实现**：它收到一条网络消息，按消息类型（`Push`/`Declare`/`Interest`/`Request`/`Response`/...）**分用**到 `Face` 上对应的 `Primitives` 方法，交给路由织网处理。

一句话：**`DeMux` 把「传输层的字节流」翻译成「对 `Face` 的一组方法调用」。**

#### 4.2.2 核心流程

```
网卡字节 → TransportUnicast 解码 → NetworkMessageMut
   │
   ▼
TransportPeerEventHandler::handle_message(DeMux)
   │
   ├─ (可选) 经入站拦截器 ingress interceptor 过滤
   │
   ▼  按 NetworkBodyMut 类型分用
match msg.body {
   Push(m)        => face.send_push(m, reliability)        // → route_data
   Declare(m)     => face.send_declare(m)                   // → 注册/转发声明
   Interest(m)    => face.send_interest(m)                  // → 声明式兴趣
   Request(m)     => face.send_request(m)                   // → 路由查询
   Response(m)    => face.send_response(m)                  // → 路由应答
   ResponseFinal  => face.send_response_final(m)
   OAM(m)         => 特殊管理消息，直接进 HAT
}
```

关键在于：`DeMux` 持有一张 `Face`，调用的 `face.send_*` 正是 4.1 里 `Face` 对 `Primitives` 的实现——也就是「路由入口」。所以入站消息一到 `DeMux`，就立刻进入了和「本端 API 发出消息」**完全相同**的路由函数。

#### 4.2.3 源码精读

[zenoh/src/net/primitives/demux.rs:37-58](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/primitives/demux.rs#L37-L58) —— **`DeMux` 的结构**：持有要投递的 `face`、可选的 `transport`（OAM 消息要用）、入站拦截器链、本端 `zid`：

```rust
pub struct DeMux {
    pub(crate) face: Face,
    pub(crate) transport: Option<TransportUnicast>,
    pub(crate) interceptor: Arc<ArcSwapOption<InterceptorsChain>>,
    zid: ZenohIdProto,
}
```

[zenoh/src/net/primitives/demux.rs:119-180](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/primitives/demux.rs#L119-L180) —— **`handle_message`：分用主逻辑**。先可选地过入站拦截器；若拦截器**挡掉**了 `Request`/`Interest`，DeMux 会**主动补发** `ResponseFinal`/`DeclareFinal`，以免请求方挂死（这是 4.1 讲的「bool 返回值」在入站侧的对称体现）：

```rust
fn handle_message(&self, mut msg: NetworkMessageMut) -> ZResult<()> {
    // ... tracing span ...
    if has_interceptor(&self.interceptor) {
        if let Some(interceptor) = self.interceptor.load().as_ref() {
            // Request 被挡 → 补 ResponseFinal，避免请求方超时
            // Interest 被挡 → 补 DeclareFinal
            // 其它被挡 → 直接 return Ok(())
        }
    }
    match msg.body { /* 见下方 */ }
}
```

[zenoh/src/net/primitives/demux.rs:180-222](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/primitives/demux.rs#L180-L222) —— **按类型分用到 `Face`**。这就是「分用」的落点，每种网络消息对应 `Face` 的一个 `Primitives` 方法：

```rust
match msg.body {
    NetworkBodyMut::Push(m)          => self.face.send_push(m, msg.reliability),
    NetworkBodyMut::Declare(m)       => self.face.send_declare(m),
    NetworkBodyMut::Interest(m)      => self.face.send_interest(m),
    NetworkBodyMut::Request(m)       => self.face.send_request(m),
    NetworkBodyMut::Response(m)      => self.face.send_response(m),
    NetworkBodyMut::ResponseFinal(m) => self.face.send_response_final(m),
    NetworkBodyMut::OAM(m)           => { /* 进 HAT 处理 */ }
}
```

注意 `Push` 走的是 `face.send_push`（默认实现 → `send_push_consume(.., true)`），最终落到 `route_data`——和出站共用同一个路由函数（见 4.1.3 引用的 face.rs:670）。

[zenoh/src/net/primitives/demux.rs:231-233](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/primitives/demux.rs#L231-L233) —— **`closed` 回调**：传输层发现连接断开时调用，DeMux 转而调用 `face.send_close()`，让路由器清理这张 face 上的全部订阅/可查询/兴趣与路由表项。

#### 4.2.4 代码实践

**实践目标**：看清 DeMux 的「分用」本质，确认它不关心消息内容、只做派发。

**操作步骤**：

1. 打开 [zenoh/src/net/primitives/demux.rs:180-222](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/primitives/demux.rs#L180-L222)。
2. 数一下 `match` 臂：除 `OAM` 外，其余 6 个分支是否与 `Primitives` trait 的方法一一对应？
3. 在 `Push` 分支旁批注：「这里调用的 `face.send_push` 最终会进入 `route_data`」。

**需要观察的现象**：DeMux 自身没有任何业务逻辑，纯粹是「按消息类型选方法」的派发器。

**预期结果**：你确认了「传输层 → DeMux → Face(Primitives) → route_data」这条入站链路的每一跳都有据可查。

**待本地验证**：可选地，开启 tracing（`RUST_LOG=zenoh::net::primitives::demux=debug`）运行任一 pub/sub 示例，观察日志中 `demux` span 的 `zid` 与 `src` 字段——它会在每条入站消息时打印来源 face。

#### 4.2.5 小练习与答案

**练习 1**：`DeMux` 为什么在拦截器挡掉 `Request` 时要补发一个 `ResponseFinal`？

> **参考答案**：请求方发出 `Request` 后会等待对应的 `Response` 与表示「回答完毕」的 `ResponseFinal`，否则会一直等到超时。若入站拦截器把 `Request` 直接吞掉，路由器既不会路由它、也不会产生应答，请求方就会挂死；所以 DeMux 替它补一个 `ResponseFinal`，让请求方尽快拿到「无人应答」的结论。

**练习 2**：`DeMux` 与 `Face` 谁更「靠内」？

> **参考答案**：`DeMux` 更靠外（靠近传输层），`Face` 更靠内（路由织网）。`DeMux` 持有 `Face`，把外部网络消息翻译成对 `Face` 的方法调用，相当于传输层与路由层之间的适配器。

---

### 4.3 Mux：出站多路复用（路由 → 网络）

#### 4.3.1 概念说明

`Mux` 与 `DeMux` 镜像对称：`DeMux` 把入站消息「分用」进路由，`Mux` 把路由层送来的出站消息「多路复用」到一条传输通道上发出。

具体地，`Mux` 实现 `EPrimitives`（4.1 的出口契约）。路由函数 `route_data` 在算出「这条 Push 要发给下游 face X」后，就调用 `face_X.primitives.send_push(..)`——而 `face_X.primitives` 正是挂在 X 这张传输 face 上的 `Mux`。`Mux` 把 `Push`（路由消息）包成 `NetworkMessageMut`（传输消息），交给 `TransportUnicast::schedule`，由传输层批处理、分片后送上链路。

一句话：**`Mux` 把「对 Face 出口的方法调用」翻译成「Transport 上的一次 schedule」。**

#### 4.3.2 核心流程

```
route_data 选定下游 face
   │  调 dst_face.primitives.send_push(msg, reliability)   // primitives = Mux
   ▼
Mux::send_push
   │  把 Push 包成 NetworkMessageMut { body: Push(..), reliability }
   ▼
Mux::schedule(msg)
   ├─ can_schedule: 经出站拦截器 egress interceptor（不通过则补 ResponseFinal/reject_interest）
   └─ handler.schedule(msg)   // TransportUnicast::schedule -> ZResult<bool>
          ▼
       传输层批处理/分片/按优先级入队 → 网卡字节
```

`Mux` 还有一个 `face: OnceLock<WeakFace>` 字段——它在构造时为空，等这张 face 在 `gateway` 里建好后才回填。它的用途有二：① 出站拦截器需要解析「发送方 face」对应的 key 前缀映射（`get_sent_mapping`）；② 当 `Interest` 被拦截器拒绝时，回退去 `face.reject_interest(..)` 取消挂起的 current interest。

#### 4.3.3 源码精读

[zenoh/src/net/primitives/mux.rs:38-75](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/primitives/mux.rs#L38-L75) —— **`Mux` 结构与 `schedule`**。`handler` 就是这张 face 对应的 `TransportUnicast`；`schedule` = 拦截器放行后调 `handler.schedule(msg)`：

```rust
pub struct Mux {
    pub handler: TransportUnicast,
    pub(crate) interceptor: ArcSwapOption<InterceptorsChain>,
    pub(crate) face: OnceLock<WeakFace>,
}

#[inline(always)]
fn schedule(&self, mut msg: NetworkMessageMut) -> bool {
    self.can_schedule(&mut msg) && self.handler.schedule(msg).unwrap_or(false)
}
```

`TransportUnicast::schedule` 的契约是 `fn schedule(&self, msg: NetworkMessageMut) -> ZResult<bool>`（见 [io/zenoh-transport/src/unicast/transport_unicast_inner.rs:101](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/transport_unicast_inner.rs#L101)）。`Mux` 把它的 `ZResult<bool>` 用 `unwrap_or(false)` 折成 `bool`——出错也算「没发出」，与 `EPrimitives` 契约一致。

[zenoh/src/net/primitives/mux.rs:140-222](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/primitives/mux.rs#L140-L222) —— **`Mux` 对 `EPrimitives` 的实现**。每个方法都遵循同一个套路：把路由消息包成 `NetworkMessageMut`，再 `schedule`。以 `send_push` 最简洁：

```rust
fn send_push(&self, msg: &mut Push, reliability: Reliability) -> bool {
    let msg = NetworkMessageMut {
        body: NetworkBodyMut::Push(msg),
        reliability,
    };
    self.schedule(msg)
}
```

而 `send_request` 展示了「bool 返回值的兜底用法」——若拦截器挡掉请求，`Mux` 主动补发 `ResponseFinal`，避免请求方超时（与 DeMux 入站侧对称）：

```rust
fn send_request(&self, msg: &mut Request) -> bool {
    // ... 包成 NetworkMessageMut ...
    if self.can_schedule(&mut msg) {
        self.handler.schedule(msg).unwrap_or(false)
    } else {
        // 被拦截器挡掉：补发 ResponseFinal，让请求方尽快收到「无应答」
        match self.face.get().and_then(|f| f.upgrade()) {
            Some(face) => face.send_response_final(&mut ResponseFinal { rid: request_id, .. }),
            None => tracing::error!("Uninitialized multiplexer!"),
        }
        false
    }
}
```

[zenoh/src/net/primitives/mux.rs:229-419](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/primitives/mux.rs#L229-L419) —— **`McastMux`**：多播孪生体，结构与 `Mux` 几乎一致，区别是 `handler: TransportMulticast`、`face: OnceLock<Face>`（多播场景 face 直接强持有）。它同样实现 `EPrimitives`，把消息 schedule 到多播传输上。

#### 4.3.4 代码实践

**实践目标**：亲手把 `route_data → Mux → Transport` 这条出站最后一段走通。

**操作步骤**：

1. 打开 [zenoh/src/net/routing/dispatcher/pubsub.rs:272-277](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/pubsub.rs#L272-L277)，看到路由函数对每个下游 face 的调用闭包：

   ```rust
   let send_push = |dst_face: &FaceState, msg: &mut Push, reliability: Reliability| {
       if dst_face.primitives.send_push(msg, reliability) { /* stats */ }
   };
   ```
   这里 `dst_face.primitives` 就是 4.1 说的「出口」——对传输 face 即 `Mux`。
2. 跟进到 [zenoh/src/net/primitives/mux.rs:178-184](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/primitives/mux.rs#L178-L184)（`Mux::send_push`），确认它把 `Push` 包成 `NetworkMessageMut` 后 `schedule`。
3. 再跟进 [zenoh/src/net/primitives/mux.rs:71-74](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/primitives/mux.rs#L71-L74)（`schedule`），看到最终落在 `self.handler.schedule(msg)`，即 `TransportUnicast::schedule`。

**需要观察的现象**：路由层把消息交给 `dst_face.primitives`（trait 对象），并不关心它背后到底是 `Mux` 还是 `WeakSession`——这就是 `EPrimitives` 作为抽象出口的威力。

**预期结果**：你能写出「`route_data` → `dst_face.primitives.send_push` (=Mux) → `Mux::schedule` → `TransportUnicast::schedule`」这条精确调用链。

#### 4.3.5 小练习与答案

**练习 1**：`Mux` 的 `face` 字段为什么是 `OnceLock<WeakFace>` 而不是构造时就传入？

> **参考答案**：存在循环依赖——`Face` 的 `FaceState.primitives` 指向 `Mux`，而 `Mux` 又需要回指 `Face`。两者无法在彼此的构造函数里同时拿到对方。Zenoh 的做法是：先 `Mux::new`（此时 face 为空），再把 `Mux` 塞进 `FaceStateBuilder` 建 face，最后用 `mux.face.set(Face::downgrade(&face))` 回填（见 4.4 的 gateway.rs:325）。用 `Weak` 是为了避免 `Arc` 循环引用导致内存泄漏。

**练习 2**：`Mux::send_push` 与 `Mux::send_request` 在被拦截器挡掉时的行为有何不同？为什么？

> **参考答案**：`send_push` 被 `schedule` 内部的 `can_schedule` 挡掉时直接返回 `false`，不补发任何东西——因为 Pub/Sub 是「发出去就行」的 fire-and-fororget，丢一条不会让谁挂死。`send_request` 被挡掉时会补发一个 `ResponseFinal`——因为 Query/Reply 有强请求-应答配对语义，请求方在等 `ResponseFinal` 才会结束，不补发就会一直等到超时。

---

### 4.4 缝合点：一张 Face，两端 Mux/DeMux

理解了三个组件，还需要看它们如何被拼成「一张完整的 face」。这一节是综合实践的前置，重点读 `gateway.rs`。

[zenoh/src/net/routing/gateway.rs:264-355](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/gateway.rs#L264-L355) —— **`new_transport_unicast`：把 Mux 与 DeMux 缝在同一张 Face 上**。这是全讲最关键的一段拼装：

```rust
let mux = Arc::new(Mux::new(transport.clone(), InterceptorsChain::empty()));   // 出口
// ... 用 FaceStateBuilder 建 face，把 mux 作为它的 primitives（出口）...
newface.set_interceptors_from_factories(&tables.data.interceptors, ...);         // 装拦截器
let _ = mux.face.set(Face::downgrade(&face));                                   // 回填 Mux 的 face
// ... HAT 初始化 ...
Ok(Arc::new(DeMux::new(face, Some(transport), ingress, this_zid)))              // 入口，共用同一张 face
```

读法：
- `Mux::new`（gateway.rs:280）造出**出口** `Mux`，作为 `FaceState.primitives` 挂到 face 上。
- `mux.face.set(..)`（gateway.rs:325）回填 `Mux` 对 face 的弱引用，闭合 4.3.5 说的循环。
- `DeMux::new(face, ..)`（gateway.rs:349）造出**入口** `DeMux`，持有同一张 `Face`。

于是同一张 `Face` 同时是：对 `DeMux` 的路由入口（`Primitives`），和对路由织网的传输出口（其 `primitives` 字段 = `Mux`）。

[zenoh/src/net/routing/gateway.rs:220-262](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/gateway.rs#L220-L262) —— **`new_session`：本地 API face**。调用方把本地 `WeakSession`（实现 `EPrimitives`）作为 `e_primitives` 传入，成为本地 face 的 `primitives`（出口=送回本地订阅者）；返回的 `Face` 作为 `Arc<dyn Primitives>` 给 API `Session` 当出口。

[zenoh/src/api/session.rs:3472-3508](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L3472-L3508) —— **`WeakSession` 的 `EPrimitives` 实现**：把出口调用委托回自己的 `Primitives` 实现（送回本地回调），并固定返回 `false`（本地投递不算「发到网上」，无需 stats/拦截器兜底）。

[zenoh/src/net/runtime/mod.rs:399-415](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/mod.rs#L399-L415) —— **`new_primitives`**：`Runtime` 调 `router.new_session(e_primitives)` 得到 `(face_id, Arc<dyn Primitives>)`，后者就是交给 API `Session` 的那张 `Face`。这正是《u7-l1》说的「`init` 经 `new_primitives → new_session` 开一张 Face」。

## 5. 综合实践

**任务**：画出一条 `Put` 消息「本端 API → 网络 → 对端 API」的完整流转图，标注每一步经过的 `Primitives / Mux / DeMux / Face` 组件。

**操作步骤**：

1. 准备一张白纸（或文本文件），画两个节点框：左 `Node A`（发布者）、右 `Node B`（订阅者），中间一条「网络」。
2. 在 `Node A` 内部，从上到下画出站链路：
   - `Publisher::put` / `Session::put` resolve
   - → 调 `primitives.send_push_consume(..)`，其中 `primitives` = 本地 `Face`（`Arc<dyn Primitives>`）
   - → `Face::send_push_consume` → `route_data`（[pubsub.rs:232](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/pubsub.rs#L232)）
   - → `route_data` 选出下游 face，调 `dst_face.primitives.send_push(..)`，`dst_face.primitives` = **`Mux`**
   - → `Mux::send_push` 包成 `NetworkMessageMut` → `Mux::schedule` → `TransportUnicast::schedule`
   - → 字节上链路
3. 在 `Node B` 内部，画出对称的入站链路：
   - 链路字节 → `TransportUnicast` 解码成 `NetworkMessageMut`
   - → `TransportPeerEventHandler::handle_message`，handler = **`DeMux`**（[demux.rs:120](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/primitives/demux.rs#L120)）
   - → `match Push(m) => face.send_push(m, reliability)`，`face` = 对端那张 `Face`（`Primitives`）
   - → `Face::send_push` → `route_data`（同一个路由函数！）
   - → `route_data` 把消息送到本地 face，调 `local_face.primitives.send_push(..)`，`local_face.primitives` = **`WeakSession`**（[session.rs:3486](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L3486)）
   - → `WeakSession` 的 `Primitives::send_push` 把 `Sample` 推进本地 `Subscriber` 的 handler
   - → 用户的 `sub.recv_async().await` 拿到 `Sample`
4. 用高亮笔标出三个关键对称点：
   - **同一个 `route_data`** 在出站和入站都被调用（只是源 face 不同）。
   - **同一个 `Face` 既是 `Primitives`（入口）又持有 `EPrimitives`（出口）**。
   - **`Mux` 与 `WeakSession` 都实现 `EPrimitives`**，区别只在「送上链路」还是「送回本地回调」。

**需要观察的现象**：图中应当出现两次 `route_data`、一次 `Mux`、一次 `DeMux`、两张 `Face`（A 的传输 face、B 的传输 face）、外加每端一张本地 face。

**预期结果**：你能指着图说——「出站最后一步是 `Mux`，入站第一步是 `DeMux`，中间的路由逻辑两边复用同一个 `route_data`，靠 `EPrimitives` 抽象把 `Mux` 和 `WeakSession` 统一成同一种出口」。这就是本讲要建立的完整心智模型。

**待本地验证**（可选）：用 `RUST_LOG=zenoh::net::primitives=trace,zenoh::net::routing::dispatcher::pubsub=trace cargo run --example z_pub` 与对端 `z_sub` 跑一次，在日志里依次找到 `send_push`、`demux`、`send_push` 三类 span，与你的图逐一对应。

## 6. 本讲小结

- Zenoh 用**两张投递契约**缝合路由层与传输层：`Primitives`（入口，把消息交进路由织网）与 `EPrimitives`（出口，把消息送出某张 face）；两者方法几乎一一对应，差异在可见性、`RoutingContext` 信封、`bool` 返回值与 `consume`/`send_close`。
- `EPrimitives` 返回 `bool` 不是装饰：它让路由器知道消息是否真的发出，从而在被拦截器挡掉时补发 `ResponseFinal`/`DeclareFinal`，避免请求方挂死——这是入站 `DeMux` 与出站 `Mux` 共同遵守的兜底约定。
- **`DeMux`** 实现传输层回调 `TransportPeerEventHandler::handle_message`，按 `NetworkBodyMut` 类型把入站消息**分用**到 `Face` 的 `Primitives` 方法（即路由入口）。
- **`Mux`** / **`McastMux`** 实现 `EPrimitives`，把路由层送来的消息包成 `NetworkMessageMut` 后 **`schedule`** 到 `TransportUnicast`/`TransportMulticast`（即传输出口）。
- 一条 `Put` 的完整链路是：`API → Face(Primitives) → route_data → 下游 face.primitives(=Mux, EPrimitives) → Transport → [网络] → Transport → DeMux → Face(Primitives) → route_data → 本地 face.primitives(=WeakSession, EPrimitives) → 本地 Subscriber`。
- `gateway.rs` 的 `new_transport_unicast` 把 `Mux`（出口）与 `DeMux`（入口）缝在**同一张 `Face`** 上；`new_session` 则把本地 `WeakSession` 作为本地 face 的出口。`Face` 因此「既是路由入口、又持出口」，是整个双向链路的中枢。

## 7. 下一步学习建议

- 下一讲《u7-l3 Runtime 编排器：scouting 与建连》会讲这些传输 face 是**何时、如何**被建出来的——即 `TransportManager` 如何接受新连接、进而触发 `new_transport_unicast` 缝合出 Mux/DeMux。读完那一讲，本讲的「缝合点」就有了时间轴。
- 之后进入《u8 路由（HAT 拓扑）》：本讲反复出现的 `route_data`、`Face`、`Tables`、`HatTrait` 将在那里展开，你会看到「路由函数如何根据拓扑策略选择下游 face」。
- 若想先看「字节如何被 Mux 之后的传输层打包」，可跳读《u9-l4 批处理、分片与优先级管道》，理解 `TransportUnicast::schedule` 背后的 batch/pipeline/priority。

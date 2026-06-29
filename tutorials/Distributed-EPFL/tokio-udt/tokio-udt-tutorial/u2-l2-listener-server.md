# 服务端监听：UdtListener::bind 与 accept

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 `UdtListener::bind` 从「创建一个 UDT socket」到「进入 `Listening` 状态」之间到底做了哪几件事；
- 解释 `accept` 是如何从一个名为 `queued_sockets` 的集合里取出**已经完成握手**的连接，而握手本身又是由谁来完成的；
- 理解 `accept_notify`（一个 `tokio::sync::Notify`）为何能让 `accept` 在没有新连接时**挂起而不忙等**，以及为什么「订阅 future」这件事必须发生在持锁期间；
- 说明为什么 `rendezvous`（会合）模式下 `listen` / `accept` 会被显式拒绝。

本讲承接上一讲（u2-l1，客户端 `UdtConnection::connect`）。客户端负责主动握手发起，服务端则负责被动等待并把握手完成的连接交给上层。两者拼起来，才是一次完整的「连接建立」。

## 2. 前置知识

阅读本讲前，建议你已经了解：

- **客户端连接主流程**（u2-l1）：`UdtConnection` 是一个薄壳，内部持有一个 `Arc<UdtSocket>`（类型别名 `SocketRef`），所有逻辑都委托给底层 socket。本讲的 `accept` 最终也是返回一个 `UdtConnection`。
- **公共 API 边界**（u1-l4）：`UdtListener` 是 crate 对外暴露的 5 个类型之一；`UdtConfiguration` 中的 `rendezvous`、`accept_queue_size` 等字段会影响本讲的行为。
- **全局单例 `Udt`**（初步印象即可，细节在 u3-l1）：进程级只有一个 `Udt` 引擎，它持有一张「所有 socket」的注册表。`UdtListener::bind` 和 `accept` 都要反复读写这个单例。

几个本讲会用到的 Tokio 同步原语，先用一句话建立直觉：

- `tokio::sync::RwLock`：异步读写锁，`.write().await` / `.read().await` 获取。之所以用「异步锁」而不是 `std::sync::RwLock`，是因为临界区里可能要 `.await`（比如发送握手包），持有标准锁跨 `.await` 点会死锁。
- `tokio::sync::Notify`：一个「通知」原语。`notify_one()` 像按一下门铃；`notified()` 返回一个 future，会一直挂起直到有人按门铃（或按过门铃之后再订阅也能立刻醒）。本讲的 `accept_notify` 就是它。

## 3. 本讲源码地图

本讲主要涉及两个文件，并顺带引用三个支撑文件来还原「连接是怎么进入队列」的：

| 文件 | 作用 |
| --- | --- |
| `src/listener.rs` | `UdtListener` 的全部实现：`bind` / `accept` / `local_addr` / `socket_id`。本讲的主角。 |
| `src/lib.rs` | 顶层文档与 `pub use listener::UdtListener` 导出；README 里的服务端示例也嵌在此处。 |
| `src/udt.rs`（支撑） | 全局引擎 `Udt`：`new_socket` 创建并注册 socket，`bind` 绑定地址，`new_connection` 把握手完成的新连接塞进 `queued_sockets` 并按门铃。 |
| `src/socket.rs`（支撑） | `UdtSocket` 的 `queued_sockets` / `accept_notify` 字段定义；`listen_on_handshake` 处理进来的握手包；`UdtStatus` 状态机。 |
| `src/queue/rcv_queue.rs`（支撑） | 接收 worker：把「目标 socket id == 0」的握手包路由给 listener。 |

## 4. 核心概念与源码讲解

### 4.1 `UdtListener::bind`：从创建 socket 到进入 Listening

#### 4.1.1 概念说明

在 UDT 里，「服务端」要做的事情和 TCP 服务端几乎一样：先绑一个本地端口，然后告诉协议栈「我在这等着，谁来连我都接」。`UdtListener` 就是这个角色的载体。

它和客户端的 `UdtConnection` 一样，本质上只是**包着一个底层 `UdtSocket`** 的薄壳：

[src/listener.rs:9-11](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/listener.rs#L9-L11) —— `UdtListener` 内部只持有一个 `SocketRef`（即 `Arc<UdtSocket>`）。

差别在于：客户端的 socket 走 `Connecting → Connected`，而服务端这个 socket 会被「登记为某个多路复用器（multiplexer）的 listener」，并进入 `Listening` 状态。此后所有到达该 UDP 端口、目标 socket id 为 0 的握手包，都会被路由到它头上（见 4.2）。

需要特别强调一点：**listener 自己并不直接持有「半连接」或「已完成连接」**。握手是由后台 worker + 全局引擎异步完成的，listener 只是「被通知有新连接好了，去队列里取」。

#### 4.1.2 核心流程

`bind` 的执行过程可以拆成四步：

```
1. new_socket：在全片 Udt 引擎里新建一个 Stream 类型的 socket 并登记
2. 拒绝 rendezvous：会合模式不支持 listen
3. udt.bind(socket_id, bind_addr)：创建/复用一个 multiplexer（含 UDP socket 与收发 worker），并 open() 该 socket
4. 把这个 socket 设为 multiplexer 的 listener，并把状态置为 Listening
```

状态机的迁移为：`Init → Opened（open()）→ Listening`。其中第 3 步的 `udt.bind` 内部已经调用了 `socket.open()`（`Init → Opened`），第 4 步再把它推到 `Listening`。

#### 4.1.3 源码精读

先看 `bind` 的完整代码，它由三段加锁的代码块组成：

[src/listener.rs:14-46](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/listener.rs#L14-L46) —— `bind` 主体：建 socket → 拒绝 rendezvous → 绑定地址 → 登记 listener 并置 Listening。

**第 1 步：创建并注册 socket。**

[src/listener.rs:15-18](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/listener.rs#L15-L18) 调用了引擎的 `new_socket`，注意返回后立刻 `.clone()`——克隆的是 `Arc`，所以拿到的是同一个底层 socket 的引用计数副本（这样 `udt` 的写锁可以马上释放）。`new_socket` 的实现：

[src/udt.rs:78-92](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L78-L92) —— 分配一个新 `socket_id`，构造 `UdtSocket`，并插入 `self.sockets` 注册表（如果 id 撞了才报错，正常情况几乎不会）。`get_new_socket_id` 用 `wrapping_sub(1)` 递减，保证 id 唯一。

**第 2 步：拒绝 rendezvous。**

[src/listener.rs:20-25](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/listener.rs#L20-L25) —— 若配置里 `rendezvous == true`，直接返回 `ErrorKind::Unsupported`。原因见 4.1.4 的练习。`rendezvous` 字段在配置里标注为「NOT IMPLEMENTED」（`src/configuration.rs:42-43`），默认 `false`。

**第 3 步：绑定地址（创建/复用 multiplexer）。**

[src/listener.rs:29-32](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/listener.rs#L29-L32) 调用 `Udt::bind`：

[src/udt.rs:177-189](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L177-L189) —— 校验 socket 仍是 `Init`（否则报「socket already binded」），然后 `update_mux`，最后 `socket.open()`。

`update_mux` 是真正「开 UDP socket + 起 worker」的地方：

[src/udt.rs:191-225](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L191-L225) —— 若 `reuse_mux` 为真且已有同端口、同 mss、可复用的 multiplexer，就直接复用；否则新建一个 `UdtMultiplexer`（它会 `bind` 一个真实 UDP socket），登记进 `self.multiplexers`，并 `UdtMultiplexer::run(mux)` 启动收发两个 worker。一个 multiplexer 对应一个 UDP socket。（multiplexer 内部细节留到 u3-l3。）

**第 4 步：登记为 listener 并置 Listening。**

[src/listener.rs:34-43](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/listener.rs#L34-L43) —— 关键两行：把 `mux.listener` 设为当前 socket，再把状态写成 `Listening`。`mux.listener` 是 multiplexer 持有的一个「可选 listener」槽位，接收 worker 正是靠它判断「这个握手包该交给谁」（见 4.2.3）。

辅助方法也在这里：

[src/listener.rs:96-102](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/listener.rs#L96-L102) —— `local_addr` 返回底层 UDP socket 的本地地址；`socket_id` 返回这个 listener 的 id。

#### 4.1.4 代码实践

**实践目标**：确认 `bind` 各阶段的副作用与状态迁移。

**操作步骤**：

1. 在 [src/listener.rs:14](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/listener.rs#L14) 的 `bind` 入口、第 18 行（`new_socket` 之后）、第 32 行（`udt.bind` 之后）、第 40 行（置 `Listening` 之前）各加一行 `eprintln!`，打印 `self.socket.socket_id` 与 `socket.status()`。
2. 用 README 的服务端示例（[README.md:34-60](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/README.md#L34-L60)）写一个最小程序，调用 `UdtListener::bind((Ipv4Addr::UNSPECIFIED, 9000).into(), None).await`。

**需要观察的现象**：依次打印 `Init`（构造时的初值）→ `Opened`（`udt.bind` 内 `open()` 之后）→ 最终进入 `Listening`。

**预期结果**：状态在 `udt.bind` 返回后为 `Opened`，在 `bind` 返回前变为 `Listening`，并打印 `Now listening on ...`。**待本地验证**：你是否能看到中间的 `Opened`，取决于 `eprintln!` 插入点是否在状态被改写之前。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `bind` 要分三段获取 `Udt::get().write().await`，而不是一次性把整段逻辑包在一把锁里？

**参考答案**：第 1 段持有写锁只是为了 `new_socket`；之后第 3 段的 `udt.bind` → `update_mux` 内部要 `UdtMultiplexer::bind(...).await`（真实 UDP 绑定，可能耗时且有 `.await`）。把所有逻辑塞进一把锁会让锁持有时间过长，且容易和后台 `cleanup_worker`（每秒拿写锁做 GC）相互阻塞。分段获取、用完即放，能缩短临界区。

**练习 2**：如果把 `rendezvous` 设成 `true` 再调 `bind`，会在哪一步失败、返回什么错误？

**参考答案**：在 [src/listener.rs:20-25](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/listener.rs#L20-L25) 失败，返回 `ErrorKind::Unsupported`，文案为 `listen is not supported in rendezvous connection setup`。会合模式下两端都是「主动方」，没有被动监听的概念，所以 listen/accept 不适用。

### 4.2 `accept`：从 `queued_sockets` 取出已握手完成的连接

#### 4.2.1 概念说明

`accept` 做的事情听起来简单：**取一个已经握手完成的连接，返回 `(对端地址, UdtConnection)`**。但关键问题是——这个「已完成连接」是谁、什么时候放进来的？

答案是：**不是 listener 自己做的，而是后台接收 worker + 全局引擎协作完成的**。整个链路是：

```
客户端发握手包(目标 socket id = 0)
        │
        ▼
rcv_queue worker 收到包，发现 socket id == 0 且是 handshake
        │  → 路由给 mux.listener（即我们的 UdtListener 底层 socket）
        ▼
listener.listen_on_handshake：回 SYN cookie（connection_type 1→-1 往返）
        │  → 校验 cookie、版本、socket_type 通过后
        ▼
Udt::new_connection：新建一个 per-connection socket，完成握手，
        │  把它的 id 插入 listener.queued_sockets，并 accept_notify.notify_one()
        ▼
accept 被唤醒，从 queued_sockets 取出这个 id，包成 UdtConnection 返回
```

注意一个重要事实：**listener 的底层 socket 和「每个被接受的连接」是不同的 socket**。listener socket 永远停留在 `Listening`，每接受一个连接，引擎就 `new_connection` 出一个新的 socket 进入 `Connected`。这和 BSD socket 的 `accept` 语义一致——返回的是新 fd。

#### 4.2.2 核心流程

`accept` 的循环逻辑（伪代码）：

```
loop {
    if status != Listening: 报错返回
    加写锁 queue = queued_sockets.write()
    if queue 非空:
        id = 取最小那个 id; queue.remove(id); break   // 取到一个
    else:
        notified = accept_notify.notified()           // 关键：在持锁期间订阅
    // 释放写锁
    notified.await                                      // 挂起等门铃
}
拿到 accepted_socket_id 后：
    从 Udt 引擎 get_socket(id)
    取 peer_addr
    返回 (peer_addr, UdtConnection::new(socket))
```

这里有两个设计要点，稍后在源码精读里展开：

1. `queued_sockets` 是 `BTreeSet<SocketId>`，`.iter().next()` 取的是**最小 id**，并非严格按到达顺序——这是个小细节，实践中影响不大。
2. `notified()` future 必须**在还持有 `queued_sockets` 写锁的时候**创建。这是为了避免「丢通知」竞态（见 4.3）。

#### 4.2.3 源码精读

**先看连接是怎么进队列的（生产者侧）。**

接收 worker 的分发逻辑：当收到的包目标 socket id 为 0 且是 handshake 时，交给 listener：

[src/queue/rcv_queue.rs:162-184](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L162-L184) —— `socket_id == 0` 分支：读 `mux.listener`，若存在就调用 `listener.listen_on_handshake(addr, handshake)`。（这是 4.1 第 4 步「登记 listener」的直接使用者。）

listener 的握手处理（SYN cookie 协商细节留到 u8-l1，这里只看它如何最终触发入队）：

[src/socket.rs:376-393](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L376-L393) —— 若 `connection_type == 1`（客户端首次握手），回一个带 SYN cookie 的响应，然后 `return Ok(())`（此时还没入队）。

[src/socket.rs:402-429](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L402-L429) —— 客户端带 cookie 二次回访（`connection_type == -1`）时：校验 cookie、`udt_version`、`socket_type`，全部通过后调用 `Udt::get().write().await.new_connection(self, addr, hs)`。

真正「入队 + 按门铃」发生在 `new_connection` 末尾：

[src/udt.rs:161-174](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L161-L174) —— 新 socket 完成 `connect_on_handshake`（进入 `Connected`）后，登记进 `self.sockets` 与 `self.peers`，然后 `listener_socket.queued_sockets.write().await.insert(ns_id)`，紧接着 `listener_socket.accept_notify.notify_one()`。这两行就是「生产者」的全部动作。

> 顺带一提，`new_connection` 开头还会先查 `peers` 注册表，若同一对端已有现存连接则复用（[src/udt.rs:100-132](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L100-L132)）；并且入队前会检查 `accept_queue_size` 上限（默认 1000，[src/udt.rs:145-147](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L145-L147)）。这两个细节在并发多客户端时才显现，本讲点到为止。

**再看消费者侧——`accept` 本身。**

[src/listener.rs:48-94](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/listener.rs#L48-L94) —— `accept` 全貌。开头 [src/listener.rs:49-56](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/listener.rs#L49-L56) 又检查了一次 `rendezvous`（拒绝会合模式），与 `bind` 里的检查形成双保险。

核心循环：

[src/listener.rs:58-76](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/listener.rs#L58-L76) —— 先校验 `status == Listening`（否则报 `socket is not in listening state`）；拿 `queued_sockets` 写锁，若非空就 `iter().next()` 取最小 id 并 `remove`，`break`；否则构造 `notified` future（注意它在一个内层 `{ }` 块里，块结束时锁就释放了），出块后 `notified.await`。

循环退出后，把 id 还原成连接：

[src/listener.rs:78-93](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/listener.rs#L78-L93) —— 用只读锁 `Udt::get().read().await` + `get_socket(id)` 取出已连接 socket（`get_socket` 会过滤掉 `Closed`，[src/udt.rs:50-57](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L50-L57)），读 `peer_addr`，最后 `UdtConnection::new(accepted_socket)` 包装返回。`UdtConnection::new` 是 `pub(crate)` 的薄壳构造器（[src/connection.rs:15-17](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/connection.rs#L15-L17)），与 u2-l1 客户端用的是同一个。

相关字段定义：

[src/socket.rs:70-71](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L70-L71) —— `queued_sockets: TokioRwLock<BTreeSet<SocketId>>`（异步读写锁 + 集合），`accept_notify: Notify`。这两个字段同时存在于 listener 底层 socket 上。

#### 4.2.4 代码实践

**实践目标**：通过「读源码 + 读断言」理解 `queued_sockets` 的生产消费节奏，而不依赖运行。

**操作步骤**：

1. 在 [src/udt.rs:172-173](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L172-L173) 处确认：生产者插入 id 后**立刻** `notify_one()`，二者之间没有 `.await`，因此通知不会丢失。
2. 跟踪一次完整入队：`rcv_queue.rs:173`（`listen_on_handshake`）→ `socket.rs:425-428`（`new_connection`）→ `udt.rs:172`（insert）→ `udt.rs:173`（notify_one）。
3. 回答：如果上层迟迟不调 `accept`，同时有 1001 个客户端同时握手完成，会发生什么？

**需要观察的现象 / 预期结果**：第 1001 个会触发 [src/udt.rs:145-147](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L145-L147) 的 `Too many queued sockets` 错误（`accept_queue_size` 默认 1000），`new_connection` 返回 `Err`，握手对该客户端失败。**待本地验证**：实际触发条件还取决于 worker 并发与 `BTreeSet` 的瞬时长度。

#### 4.2.5 小练习与答案

**练习 1**：`queued_sockets.iter().next()` 取的是「最早到达」的连接吗？为什么？

**参考答案**：不一定是。`queued_sockets` 是 `BTreeSet<SocketId>`，按 `SocketId`（一个 `u32`）**数值升序**排列，`.iter().next()` 取的是数值最小的 id。由于 id 是随机分配的（`new_socket` 用 `rand::random()` 初始化 `next_socket_id`），最小 id 与「最早到达」通常没有对应关系。如果业务对「先来后到」敏感，这是需要注意的点。

**练习 2**：`accept` 取出 id 之后，用 `Udt::get().read().await` 而不是直接持有连接，可能出什么问题？

**参考答案**：`get_socket` 会过滤掉 `Closed` 状态的 socket（[src/udt.rs:50-57](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L50-L57)）。极端情况下，一个 id 刚入队、上层还没 `accept`，连接却因对端异常被 `garbage_collect_sockets` 置为 `Closed`/移除，此时 `get_socket` 返回 `None`，`accept` 报 `invalid socket id when accepting connection`（[src/listener.rs:79-84](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/listener.rs#L79-L84)）。这是一种「取出来发现已经没了」的竞态，调用方需处理这个错误。

### 4.3 `accept_notify`：用 Notify 异步等待新连接

#### 4.3.1 概念说明

如果没有新连接，`accept` 不能死循环空转（忙等会烧 CPU），也不能 `sleep` 一下再轮询（既慢又浪费）。理想做法是：**「没有连接就挂起，等有人放进来时再把我叫醒」**。

`tokio::sync::Notify` 正是干这个的。可以把它想成一个门铃：

- `accept_notify.notified()` —— 「我按这个门铃订阅，没响就一直睡」；
- `accept_notify.notify_one()` —— `new_connection` 在塞完连接后「按一下门铃」。

但 `Notify` 有一个经典陷阱：**如果你在「检查队列」和「订阅门铃」之间松手，恰好这一刻有人按了门铃，你就会错过这次通知、永远睡死**。tokio-udt 用一个精巧的写法规避了它。

#### 4.3.2 核心流程

防丢通知的关键模式是「**在持有队列写锁的期间创建 `notified` future**」：

```
let notified = {
    加写锁 queue = queued_sockets.write()
    if 非空: 取出并 break
    accept_notify.notified()        // ← 订阅动作发生在锁内
};                                  // ← 锁在这里释放
notified.await                      // ← 释放锁之后才等待
```

为什么这样能避免竞态？因为 `notify_one()`（生产者，[src/udt.rs:172-173](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L172-L173)）也要拿 `queued_sockets` 的写锁（`listener_socket.queued_sockets.write().await.insert(...)`）。于是「检查队列 + 订阅」和「插入 + 通知」被同一把锁串行化：

- 要么生产者先拿到锁：插入了 id、按了门铃，然后释放锁；消费者后拿到锁时看到队列非空，直接取走，根本不用等门铃。
- 要么消费者先拿到锁：看到队列空，**在锁内**订阅好 future 再释放锁；生产者之后才拿到锁插入并按门铃——由于订阅已经成立，这次按铃一定能唤醒消费者。

两种顺序都不会丢通知。这就是把 `notified()` 放进内层 `{}` 块（与 `queue` 同生命周期）的根本原因。

#### 4.3.3 源码精读

消费者侧的订阅：

[src/listener.rs:58-76](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/listener.rs#L58-L76) —— 重点看 [src/listener.rs:73](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/listener.rs#L73) 的 `self.socket.accept_notify.notified()` 位于 `let mut queue = ... .write().await` 之后、块结束 `}` 之前；而 [src/listener.rs:75](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/listener.rs#L75) 的 `notified.await` 在块外（锁已释放）。这正是上节描述的「锁内订阅、锁外等待」。

外层 `loop` 也是有意为之：被唤醒后还要**重新检查**队列与状态——唤醒只代表「可能有连接」，不代表「一定有」。若被虚假唤醒或队列又被别人取空，循环会再次走到订阅分支，不会错误返回空。

生产者侧的按铃：

[src/udt.rs:172-173](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L172-L173) —— `insert(ns_id)` 与 `accept_notify.notify_one()` 之间没有任何 `.await`，通知紧跟插入、原子的。

字段本身：

[src/socket.rs:70-71](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L70-L71) —— `queued_sockets` 用 `TokioRwLock`（异步锁，因为临界区跨 `.await`），而 `Notify` 本身是并发安全的不需要外层锁。

> 对比客户端（u2-l1）的 `wait_for_connection`：那里是「持锁判状态 + `connect_notify.notified().await`」，思路完全一样——都是「锁内检查 + 锁内/紧邻订阅 + 锁外等待」。这套模式贯穿 tokio-udt 的所有异步等待点（还包括 `rcv_notify`、`ack_notify`），值得记牢。

#### 4.3.4 代码实践

**实践目标**：验证「锁内订阅」对正确性的必要性（源码阅读型）。

**操作步骤**：

1. 假想一个「错误版本」：把 [src/listener.rs:73](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/listener.rs#L73) 的 `self.socket.accept_notify.notified()` 挪到内层 `{}` **之外**（即先释放锁、再订阅）。
2. 构造这样一个时序：消费者拿到锁、发现空、释放锁；**就在释放锁之后、订阅之前**，生产者拿到锁插入 id 并 `notify_one()`；消费者随后才订阅。
3. 推演结果。

**需要观察的现象 / 预期结果**：生产者的 `notify_one()` 发生在「还没有人订阅」时，按 tokio `Notify` 的语义，这次通知会被「记住」为一次 permit（`notify_one` 会存一个未消费的许可）。因此实际上……**这个假想版本在 `Notify` 的语义下恰好可能不死锁**——这正是 `Notify` 相比 `oneshot` 的好处。但即便如此，把它放进锁内仍然是**更稳健、更易推理**的写法：它让正确性只依赖「锁的互斥」，而不依赖读者对 `Notify` permit 语义的精确理解。请对照 [src/listener.rs:67-75](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/listener.rs#L67-L75) 的实际写法体会作者的取舍。**待本地验证**：可写一个最小测试，用 `tokio::time::timeout` 包住 `accept`，人工控制 `new_connection` 的插入时机来观察唤醒行为。

#### 4.3.5 小练习与答案

**练习 1**：`accept` 外层为什么必须是 `loop`，而不能写「`notified.await` 一次后直接取队列」？

**参考答案**：因为唤醒只表示「可能有连接」。如果多个 `accept` 任务（或被错误地多次唤醒）竞争同一个 listener，或者唤醒到达时连接已被 GC 移除，循环就需要重新检查状态和队列。`loop` 保证了「只有真正取到 id 才 `break`」，否则重新订阅，从而正确处理虚假唤醒与竞态。

**练习 2**：`notify_one()` 一次只会唤醒一个等待者。如果同时有 5 个并发 `accept` 调用，又有 5 个连接先后到达，它们会被一一配对吗？

**参考答案**：会，但顺序不保证与「`accept` 发起顺序」对应。每个连接到达时 `notify_one()` 唤醒一个挂起的 `accept`（被唤醒者从 `notified().await` 返回后回到循环顶部重新抢锁取 id）。5 个连接 = 5 次按铃 = 唤醒 5 个等待者，最终 5 次 `accept` 各返回一个不同的连接。由于 `BTreeSet` 取最小 id，且 tokio 调度顺序不定，具体配对取决于调度。

## 5. 综合实践：写一个回显（echo）listener

把本讲三块内容串起来，做一个最小可用服务端：接受连接后，把读到的数据**原样回写**，再用上一讲（u2-l1）的客户端验证。

**实践目标**：跑通「listener bind → accept → spawn 回显任务 → 客户端收发回显」的完整闭环。

**操作步骤**：

1. 在仓库根目录新建一个 example 或 binary（例如 `src/bin/udt_echo_server.rs`，Cargo 会自动发现，见 u1-l2）。下面是示例代码（**非仓库原有代码**）：

```rust
// 示例代码：回显 UDT 服务端
use std::net::Ipv4Addr;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio_udt::UdtListener;

#[tokio::main]
async fn main() -> tokio::io::Result<()> {
    let port = 9000;
    let listener = UdtListener::bind((Ipv4Addr::UNSPECIFIED, port).into(), None).await?;
    println!("Echo server listening on {port}");

    loop {
        let (addr, mut connection) = listener.accept().await?;   // 本讲 4.2
        println!("Accepted from {addr}");
        tokio::task::spawn(async move {
            let mut buf = vec![0u8; 64 * 1024];
            loop {
                match connection.read(&mut buf).await {          // AsyncRead，见 u2-l1
                    Ok(0) => break,                                // 对端关闭
                    Ok(n) => {
                        if let Err(e) = connection.write_all(&buf[..n]).await { // 回显
                            eprintln!("write to {addr} failed: {e}");
                            break;
                        }
                    }
                    Err(e) => {
                        eprintln!("read from {addr} failed: {e}");
                        break;
                    }
                }
            }
        });
    }
}
```

2. 写一个最小客户端（基于 u2-l1，发送后尝试读回）：

```rust
// 示例代码：回显验证客户端
use std::net::Ipv4Addr;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio_udt::UdtConnection;

#[tokio::main]
async fn main() -> tokio::io::Result<()> {
    let mut conn = UdtConnection::connect((Ipv4Addr::LOCALHOST, 9000), None).await?;
    let msg = b"hello-udt-echo";
    conn.write_all(msg).await?;
    let mut buf = vec![0u8; msg.len()];
    conn.read_exact(&mut buf).await?;          // 期望收到原样回显
    assert_eq!(&buf, msg);
    println!("echo ok: {}", String::from_utf8_lossy(&buf));
    Ok(())
}
```

3. 分别运行：先 `cargo run --bin udt_echo_server`，再另开终端跑客户端。

**需要观察的现象**：

- 服务端先打印 `Now listening on ...`（来自 `bind`，[src/listener.rs:42](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/listener.rs#L42)），随后 `Echo server listening on 9000`。
- 客户端连接后，服务端打印 `Accepted from 127.0.0.1:xxxxx`（来自 `accept` 返回的 `addr`）。
- 客户端打印 `echo ok: hello-udt-echo`，说明数据经「客户端写 → 服务端读 → 服务端写回 → 客户端读」一个来回。

**预期结果**：客户端的 `assert_eq!` 通过。**待本地验证**：UDT 在回环（localhost）上的握手与回显应当稳定通过；若失败，先用 `UDT_DEBUG=1` 环境变量运行（[src/udt.rs:18-19](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L18-L19)）查看内部诊断输出。

**延伸思考**：把回显任务里的 `connection` 改成「先读完一个缓冲再统一写回」与「边读边写」两种模式，观察对吞吐的影响。这会自然引出后续发送/接收缓冲（u5）与拥塞控制（u7）的话题。

## 6. 本讲小结

- `UdtListener` 是一个薄壳，内部持有一个 `Arc<UdtSocket>`；`bind` 经「`new_socket` 创建登记 → `udt.bind` 起 multiplexer（UDP socket + 收发 worker）→ 登记 `mux.listener` 并置 `Listening`」四步就绪。
- 服务端 socket 与「每个被接受的连接」是**不同的 socket**：listener 始终停在 `Listening`，握手由后台 worker + 全局引擎 `new_connection` 异步完成，产出一个新 `Connected` socket。
- `accept` 的职责只是「消费者」：从 `queued_sockets`（一个 `TokioRwLock<BTreeSet<SocketId>>`）取出已完成握手的 id，再包成 `UdtConnection` 返回。
- `accept_notify`（`tokio::sync::Notify`）让 `accept` 在空闲时挂起而非忙等；「锁内订阅 future、锁外 await」的写法配合 `new_connection` 里「insert 后立刻 `notify_one`」，把生产消费串行化在同一把锁上，避免丢通知。
- `rendezvous`（会合）模式被显式拒绝：`bind` 和 `accept` 都会在 `rendezvous == true` 时返回 `ErrorKind::Unsupported`，因为该模式在 tokio-udt 中尚未实现。
- 队列容量受 `accept_queue_size`（默认 1000）限制，超出会令新连接握手失败；取出后若连接已被 GC 置为 `Closed`，`get_socket` 返回 `None`，`accept` 报错——调用方需处理这类竞态。

## 7. 下一步学习建议

本讲把「服务端被动等待 + 接受连接」讲完了，但刻意回避了几个深水区：

1. **握手的线上细节**：`listen_on_handshake` 里的 SYN cookie 怎么算、`connection_type` 1↔-1 的往返、版本不匹配时的 1002 拒绝，都在 **u8-l1（连接建立与握手：SYN cookie）** 详细拆解。
2. **全局引擎 `Udt` 本身**：`sockets` / `multiplexers` / `peers` 三张注册表、`cleanup_worker` 每秒做 GC，是 **u3-l1（全局单例 Udt 与 socket/mux 注册表）** 的主题——理解它之后，你对本讲里 `accept` 取出连接又「可能被 GC 抢先移除」的竞态会有更完整的认识。
3. **被接受的连接如何收发数据**：`UdtConnection` 的 `AsyncRead`/`AsyncWrite` 在 u2-l1 已初识，其 `poll_read`/`poll_write` 内部的 `spawn + Notify` 唤醒取舍留到 **u8-l3（异步桥接）**。

建议按「u3-l1 → u8-l1 → u8-l3」的顺序补齐，先建立引擎与握手的整体图景，再回头体会本讲 `accept` 在其中的位置。

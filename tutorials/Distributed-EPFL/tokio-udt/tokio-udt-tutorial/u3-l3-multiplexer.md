# 多路复用器 UdtMultiplexer：共享 UDP 与 worker

## 1. 本讲目标

本讲是「运行时引擎与整体架构」单元的第三篇。前两讲我们认识了进程级单例 `Udt`（u3-l1）和单个 `UdtSocket` 的结构与状态机（u3-l2）。本讲要回答一个关键问题：**多个 `UdtSocket` 是怎么共用底层网络资源的？**

学完本讲你应当能够：

1. 说清「一个 multiplexer 对应几个 UDP socket、几个 worker 任务」这个架构事实。
2. 解释为什么多个 `UdtSocket` 可以共享同一个 UDP socket，以及 `reuse_mux=true` 时复用一个 multiplexer 必须满足的三个条件。
3. 读懂 `UdtMultiplexer::new` / `bind` 如何用 `socket2` 创建并配置原始 UDP socket（缓冲区大小、`SO_REUSEPORT`、非阻塞）。
4. 读懂 `UdtMultiplexer::run` 启动的「接收 worker」与「发送 worker」各自负责什么，以及它们如何通过 `socket_id` 把数据包分发给正确的 `UdtSocket`。

---

## 2. 前置知识

在进入本讲前，请确认你理解以下几个概念（前序讲义已建立）：

- **UDP 与 UDT 的关系**：UDT 是构建在 UDP 之上的可靠传输协议。对操作系统而言，UDT 看起来只是一堆普通的 UDP 数据报；可靠性、拥塞控制全在应用层实现（见 u1-l1）。
- **全局单例 `Udt`**：进程内只有一个 `Udt` 实例，它持有两张注册表——`sockets`（所有 `UdtSocket`）和 `multiplexers`（所有 multiplexer）（见 u3-l1）。
- **`UdtSocket` 是簿记容器**：每个 `UdtSocket` 持有发送缓冲、接收缓冲、拥塞控制等状态，但它自己**并不直接拥有 UDP socket**（见 u3-l2）。
- **`Arc` 与 `Weak`**：`Arc<T>` 是线程安全的引用计数智能指针（强引用）；`Weak<T>` 是弱引用，不阻止被指向的对象被释放。两者配合可以避免「循环引用」导致内存泄漏。

还需要一个本讲会反复用到的术语：

- **多路复用（multiplexing）**：把多条逻辑流混在一条物理通道上传输。在 UDT 里，多个 `UdtSocket`（多条逻辑 UDT 连接）共用同一个 UDP socket（一条物理通道）。每个 UDT 包的包头里都带着一个「目标 socket id」，接收端靠它把包分发给正确的逻辑连接。这个分发动作叫**解复用（demultiplexing）**。

---

## 3. 本讲源码地图

本讲聚焦一个文件，并涉及它周围的几个协作者：

| 文件 | 角色 | 本讲用到什么 |
| --- | --- | --- |
| [src/multiplexer.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/multiplexer.rs) | **本讲主角**，multiplexer 的全部实现 | 结构体定义、`new_udp_socket`、`new`/`bind`、`run` |
| [src/udt.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs) | 全局引擎 | `update_mux`：决定「复用已有 mux」还是「新建 mux 并 run」 |
| [src/socket.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs) | `UdtSocket` | `multiplexer` 字段、`set_multiplexer`、`rcv_queue.push_back` |
| [src/queue/snd_queue.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_queue.rs) | 发送调度队列 | `worker`：发送 worker 的主循环 |
| [src/queue/rcv_queue.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs) | 接收分发队列 | `worker`：接收 worker 的主循环与按 socket_id 分发 |
| [src/configuration.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/configuration.rs) | 配置 | `udp_reuse_port`、`reuse_mux`、`mss` 字段 |

一句话概括：`UdtMultiplexer` 把「一个 UDP socket」和「两个 worker」打包成一个可被多个 socket 共享的资源单元；而「要不要新建一个这样的单元」由全局 `Udt::update_mux` 决定。

---

## 4. 核心概念与源码讲解

本讲拆为三个最小模块：

- **4.1** 多路复用的必要性：一个 UDP socket 服务多个 UdtSocket
- **4.2** `UdtMultiplexer` 结构与 `new` / `bind` 创建 UDP socket
- **4.3** `run`：启动接收与发送两个 worker

### 4.1 多路复用的必要性：一个 UDP socket 服务多个 UdtSocket

#### 4.1.1 概念说明

最朴素的实现是「一条 UDT 连接 = 一个 UDP socket」。但这会带来问题：当服务端要同时处理成百上千个客户端连接时，就要创建成百上千个 UDP socket，每个 socket 都是一个内核对象，都要参与 epoll/polling，资源与上下文切换开销都很大。

UDT 的设计选择是**让多个逻辑连接共用同一个 UDP socket**：

- 服务端只在一个端口上 listen（对应一个 UDP socket）。
- 所有客户端发来的包都进到这同一个 UDP socket。
- 接收 worker 读出每个包后，根据包头里的「目标 socket id」字段，把包**分发**给对应的逻辑连接（`UdtSocket`）。

这个「共享 UDP socket + 按 socket_id 分发」的角色，就是 **multiplexer**。它既是 UDP socket 的持有者，也是收发 worker 的宿主。

#### 4.1.2 核心流程

一个 multiplexer 的工作回路可以画成：

```text
        ┌─────────────────────────────────────────────────────────┐
        │              UdtMultiplexer (一个 UDP socket)           │
        │                                                         │
        │   UDP socket ◄──────┐              ┌──────► UDP socket  │
        │      ▲             │              │           │        │
        │      │             │              │           │        │
        │  rcv_queue.worker  │              │   snd_queue.worker │
        │  (接收 worker)     │              │   (发送 worker)    │
        │      │             │              │           ▲        │
        │      │ 按 dest_socket_id 分发     │           │ 调度    │
        │      ▼             │              │           │        │
        │   socketA  socketB  socketC ...   │   socketA/B/C ...  │
        │   (UdtSocket，每个是独立逻辑连接) │                    │
        │                                     │                    │
        └─────────────────────────────────────────────────────────┘
```

要点：

1. **一个 multiplexer 持有一个 UDP socket**（`channel: Arc<UdpSocket>`）。
2. **一个 multiplexer 跑两个 worker 任务**：接收 worker 负责从 UDP socket 读包并分发；发送 worker 负责按时间调度各 socket 的发送。
3. 分发的依据是每个 UDT 包包头里的 **目标 socket id**（见 u4 包格式单元）。

#### 4.1.3 源码精读

先看 multiplexer 持有哪些 socket 的引用。`UdtSocket` 通过一个弱引用指回自己的 multiplexer：

[src/socket.rs:72](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L72) — `multiplexer: RwLock<Weak<UdtMultiplexer>>`，socket 对 mux 是**弱引用**，这样当 mux 不再被全局表持有时可以被回收，避免循环引用。

[src/socket.rs:218-224](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L218-L224) — `set_multiplexer` 把弱引用写进去；`multiplexer()` 用 `upgrade()` 尝试拿回强引用 `Arc`，失败（mux 已销毁）就返回 `None`。

反过来，multiplexer 也不直接持有 socket 列表——socket 的引用分散在两个队列里（`socket_refs: BTreeMap<SocketId, Weak<UdtSocket>>`），同样是弱引用。这种「双向都是 `Weak`」的设计让全局 `Udt` 的垃圾回收能干净地工作（见 u3-l1、u8-l2）。

再看接收 worker 如何按 `dest_socket_id` 分发：

[src/queue/rcv_queue.rs:162-198](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L162-L198) — 这就是解复用逻辑的核心。关键三段：

- `socket_id == 0`：UDT 规定 `socket_id == 0` 的包是握手包，路由给这个 multiplexer 的 listener（`mux.listener`），交给 `listener.listen_on_handshake` 处理（握手细节见 u8-l1）。
- `socket_id != 0` 且能找到对应 socket：调用 `socket.process_packet(packet)` 把包交给那条逻辑连接，再做定时器检查。
- 找不到 socket：打印调试日志后丢弃（rendezvous 模式的处理还是 TODO）。

注意分发前还有一个安全校验：`socket.peer_addr() == Some(addr) && socket.status().is_alive()`——既核对来源地址，又要求连接存活，否则忽略。

#### 4.1.4 代码实践

**实践目标**：亲手验证「一条 UDP socket 上的包被按 socket_id 分发到不同 UdtSocket」这个事实。

**操作步骤（源码阅读型）**：

1. 打开 [src/queue/rcv_queue.rs:162-198](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L162-L198)，定位 `socket_id == 0` 分支、`get_socket` 分支、找不到的兜底分支。
2. 在三个分支分别加一行调试日志（仅在 `UDT_DEBUG` 环境变量打开时输出），例如：

   ```rust
   // 示例代码：仅用于观察分发路径，不修改协议逻辑
   if *UDT_DEBUG { eprintln!("dispatch: socket_id={} (handshake)", socket_id); }
   ```

3. 启动一个 `udt_receiver`（服务端，listen 在 9000），再用**两个**不同终端各启动一个 `udt_sender` 连到同一 `127.0.0.1:9000`：

   ```bash
   UDT_DEBUG=1 cargo run --bin udt_receiver
   # 另外两个终端：
   UDT_DEBUG=1 cargo run --bin udt_sender
   ```

**需要观察的现象**：

- 服务端只有**一个**监听端口（9000），却同时服务两条数据流——证明共享了同一个 UDP socket。
- 你添加的日志里应出现两个不同的非零 `socket_id`，分别对应两条连接的包。

**预期结果**：两个 sender 的吞吐都在 receiver 端被正确累加，且日志显示接收 worker 把不同 `socket_id` 的包分发给了不同 socket。如果运行环境受限，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 multiplexer 与 socket 互相都用 `Weak` 引用，而不是一边用 `Arc`？

> **参考答案**：`UdtMultiplexer` 持有长期运行的两个 worker 任务，`UdtSocket` 又会被这些 worker 引用到。如果 mux→socket 或 socket→mux 用强引用 `Arc`，就会形成引用计数永远降不到 0 的「循环引用」，导致两个对象都无法释放、内存泄漏。两边都用 `Weak`，真正的所有权交给全局 `Udt` 的 `sockets` / `multiplexers` 表（持有 `Arc`），这样 GC 时只要全局表删掉强引用，对象就能被回收。

**练习 2**：接收 worker 在分发前为什么检查 `socket.peer_addr() == Some(addr)`？

> **参考答案**：UDP 没有连接的概念，任何人都能往一个端口发包。`dest_socket_id` 只能说明「这个包想去某条逻辑连接」，但来源地址可能根本不是这条连接的对端（比如伪造、乱串、旧连接的迟到包）。核对来源地址可以防止把陌生来源的包误交给某条连接，是一个安全与正确性双重校验。

---

### 4.2 UdtMultiplexer 结构与 new / bind 创建 UDP socket

#### 4.2.1 概念说明

`UdtMultiplexer` 是一个普通结构体，它把以下几样东西打包在一起：

- 一个 **UDP socket**（`channel: Arc<UdpSocket>`，tokio 的异步 UDP socket）。
- 一个**发送调度队列** `snd_queue` 和一个**接收分发队列** `rcv_queue`。
- 一些元信息：`id`、`port`、`mss`、`reusable`，以及一个可选的 `listener`。

创建 multiplexer 的入口有两个：`new`（客户端侧，让内核分配临时端口）和 `bind`（服务端侧，绑定到指定端口）。两者底层都调用 `new_udp_socket`，用 `socket2` crate 创建并配置原始 UDP socket。

为什么用 `socket2` 而不是直接 `tokio::net::UdpSocket::bind`？因为 tokio 的 `UdpSocket` 暴露的配置选项有限，而 `socket2::Socket` 可以在 `bind` **之前**精细设置收发缓冲区大小（`SO_RCVBUF`/`SO_SNDBUF`）和端口复用（`SO_REUSEPORT`）——UDT 是高吞吐协议，这些内核参数对性能影响很大。

#### 4.2.2 核心流程

`new_udp_socket` 的步骤：

```text
1. 确定 bind 地址（None 则用 0.0.0.0:0，由内核选临时端口）
2. 按地址族选 domain（IPv4 → Domain::IPV4，IPv6 → Domain::IPV6）
3. 在 spawn_blocking 里（这些是阻塞式系统调用）：
   a. Socket::new(domain, DGRAM, None)       // 建原始数据报 socket
   b. set_recv_buffer_size(udp_rcv_buf_size)  // SO_RCVBUF
   c. set_send_buffer_size(udp_snd_buf_size)  // SO_SNDBUF
   d. set_reuse_port(udp_reuse_port)          // SO_REUSEPORT
   e. set_nonblocking(true)                   // tokio 要求非阻塞
   f. bind(addr)                               // 绑定
   g. UdpSocket::from_std(...)                 // 转成 tokio 异步 socket
```

注意第 3 步被包在 `tokio::task::spawn_blocking` 里：`socket2` 的这些操作本质是同步系统调用，放到阻塞线程池执行，避免阻塞 tokio 的异步运行时线程。

`new` 与 `bind` 的区别只在于 `bind_addr` 是 `None` 还是 `Some`：

- `new(... None)` → 客户端，`bind_addr` 默认 `0.0.0.0:0`，端口由内核选。
- `bind(... Some(addr))` → 服务端，绑定到指定地址/端口。

构造完成后，两者都调用 `mux.rcv_queue.set_multiplexer(&mux)`，把队列对 mux 的弱引用补上（接收 worker 在分发握手包时要回查 `mux.listener`，见 4.1.3）。

#### 4.2.3 源码精读

先看结构体本身：

[src/multiplexer.rs:14-25](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/multiplexer.rs#L14-L25) — `UdtMultiplexer` 的全部字段。`channel: Arc<UdpSocket>` 就是那个被共享的 UDP socket；`snd_queue` / `rcv_queue` 是两个队列；`listener: RwLock<Option<SocketRef>>` 记录挂在这个 mux 上的监听 socket（一个 mux 至多一个 listener，但可以有任意多个普通连接 socket）。

`new_udp_socket` 是创建 UDP socket 的核心：

[src/multiplexer.rs:28-51](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/multiplexer.rs#L28-L51) — 用 `socket2::Socket` 依次设置缓冲区、端口复用、非阻塞，再 `bind`，最后 `from_std` 转成 tokio 的异步 `UdpSocket`。注意 `set_recv_buffer_size` / `set_send_buffer_size` 的实际值会被内核的 `net.core.rmem_max` / `wmem_max` 截断（见 u2-l3 配置讲义）。

`new`（客户端，临时端口）：

[src/multiplexer.rs:53-75](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/multiplexer.rs#L53-L75) — `new_udp_socket(config, None)` 让内核选端口，`channel.local_addr()?.port()` 读回实际端口，`reusable: config.reuse_mux`、`mss: config.mss` 从配置填入。

`bind`（服务端，指定端口）：

[src/multiplexer.rs:77-100](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/multiplexer.rs#L77-L100) — 与 `new` 几乎一样，差别只在 `new_udp_socket(config, Some(bind_addr))` 绑定到指定地址。

另外提一下两个发送入口 `send_to` 与 `send_mmsg_to`，它们是 worker 实际把包送上网卡的出口：

[src/multiplexer.rs:102-104](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/multiplexer.rs#L102-L104) — `send_to`：单包发送，序列化后调一次 `channel.send_to`。

[src/multiplexer.rs:106-159](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/multiplexer.rs#L106-L159) — `send_mmsg_to`：批量发送。Linux 下用 `sendmmsg` 一次系统调用发多个包（快路径）；非 Linux 回退为循环 `send_to`。这是平台优化的重点，留到 u8-l4 精讲。

#### 4.2.4 代码实践

**实践目标**：理清 `new_udp_socket` 中每一行配置对内核 socket 的实际影响，并区分 `new` 与 `bind` 的使用场景。

**操作步骤（源码阅读 + 参数实验型）**：

1. 打开 [src/multiplexer.rs:41-46](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/multiplexer.rs#L41-L46)，逐行写出每个 `socket.xxx` 调用对应的 `setsockopt` 选项（提示：`set_recv_buffer_size` → `SO_RCVBUF`，`set_reuse_port` → `SO_REUSEPORT`）。
2. 打开 [src/configuration.rs:56-72](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/configuration.rs#L56-L72)，记下 `udp_snd_buf_size` / `udp_rcv_buf_size` / `udp_reuse_port` 的默认值（都是 `8_000_000` 字节、`8_000_000` 字节、`false`）。
3. 跟踪调用关系：`UdtListener::bind` → 全局 `udt.bind` → `update_mux` → `UdtMultiplexer::bind`（服务端走 `bind`）；`UdtConnection::connect` → `update_mux` → `UdtMultiplexer::new`（客户端走 `new`）。

**需要观察的现象**：在 Linux 上可以修改 `udp_reuse_port = true`，再在 receiver 启动前用 `sysctl` 查看并放宽 `net.core.rmem_max`，观察 receiver 吞吐是否变化。

**预期结果**：你能用一句话说清「服务端必须用 `bind`（因为要知道端口）、客户端用 `new`（端口无所谓、内核选即可）」。性能数据**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`new_udp_socket` 为什么要放在 `spawn_blocking` 里执行？`UdpSocket::from_std` 之后还能算阻塞吗？

> **参考答案**：`socket2::Socket::new`、`set_*`、`bind` 都是直接发起系统调用的同步函数，可能阻塞（尤其在高负载或涉及内核锁时）。把它们放进 `spawn_blocking` 是为了不占用 tokio 的异步工作线程。而 `UdpSocket::from_std` 只是把已建好的标准库 socket 句柄「包装」成 tokio 的异步类型，它本身不发起网络系统调用，不算阻塞；转换后的 socket 已被设为非阻塞，后续的异步 `send_to`/`recv_from` 由 tokio 用 epoll/io_uring 驱动。

**练习 2**：`UdtMultiplexer` 的 `reusable` 字段和 `UdtConfiguration::udp_reuse_port` 是同一个东西吗？

> **参考答案**：不是，二者作用于不同层次。`udp_reuse_port` 对应内核 socket 选项 `SO_REUSEPORT`，控制**多个 UDP socket 能否同时绑定同一端口**（用于多进程/多线程负载均衡）；`reusable`（来自 `reuse_mux`）是 **UDT 应用层**的概念，控制「新建 socket 时能否复用一个已存在的 multiplexer（连同它的 UDP socket 和 worker）」。一个写在 `new_udp_socket` 里给内核看，一个写在 `update_mux` 里给 UDT 自己看。

---

### 4.3 run：启动接收与发送两个 worker

#### 4.3.1 概念说明

multiplexer 创建出来后，它持有的 UDP socket 不会自己收发包——需要有人不断去读它、也有人不断往里写。这就是 `UdtMultiplexer::run` 的职责：**启动两个长期运行的 tokio 任务**：

- **接收 worker**（`rcv_queue.worker`）：循环从 UDP socket 读包 → 反序列化 → 按 `socket_id` 分发给对应 `UdtSocket` → 顺便做定时器检查。
- **发送 worker**（`snd_queue.worker`）：维护一个按时间排序的「待发送 socket」调度队列，到点了就取出对应 socket，问它「现在该发哪些包」，再批量发出去。

这两个 worker 就是整个 UDT 收发数据的「心脏」，只要 multiplexer 还活着，它们就一直在跳。

#### 4.3.2 核心流程

`run` 本身极简，只是 spawn 两个任务：

```text
UdtMultiplexer::run(mux):
    tokio::spawn( async { mux.rcv_queue.worker().await } )   // 接收 worker
    tokio::spawn( async { mux.snd_queue.worker().await } )   // 发送 worker
```

**接收 worker 主循环**（`rcv_queue.worker`）：

```text
loop:
    1. 批量收包（receive_packets，最多一次收 mss*100 字节 / 最多 100 个包）
    2. 反序列化每个包为 UdtPacket
    3. for 每个包:
         取 dest_socket_id
         if == 0:           交给 listener 处理握手
         elif 找到 socket:   socket.process_packet + check_timers
         else:               丢弃（或调试打印）
    4. 顺手处理「定时器检查」队列（每 100ms 检查一次各 socket 的定时器）
```

**发送 worker 主循环**（`snd_queue.worker`）：

```text
loop:
    看堆顶节点（最早到点的 socket）:
      若已到点:  pop → 取 socket → next_data_packets() 得到要发的包
                 → 通过内部 channel 交给一个「真正发包」的子任务 send_data_packets
                 → 重新插入「下次发送时刻」
      若未到点:  select { sleep_until(到点) | notify 唤醒 }
      若堆空:    等待 notify 唤醒
```

注意一个细节：**`run` 只在「新建」一个 multiplexer 时被调用一次**，复用已有 mux 时不会再次 `run`。也就是说，无论有多少个 socket 共享一个 mux，这个 mux 始终只有那两个 worker。

#### 4.3.3 源码精读

`run` 的全部代码：

[src/multiplexer.rs:167-173](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/multiplexer.rs#L167-L173) — 两个 `tokio::spawn`，分别跑 `rcv_queue.worker` 和 `snd_queue.worker`。注意第一个 spawn 内部 clone 了一份 `mux`，因为闭包要 move 进任务。

那么 `run` 在哪里、什么时候被调用？答案在全局引擎的 `update_mux`：

[src/udt.rs:191-225](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L191-L225) — 这是「复用 vs 新建」的决策点。

- [src/udt.rs:196-208](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L196-L208) 是**复用分支**：当 `reuse_mux` 为真、`bind_addr` 有端口（`port > 0`）时，遍历已有 multiplexer，只要满足「`mux.reusable` 且 `mux.port == port` 且 `mux.mss == socket_mss`」三个条件，就把当前 socket 挂到这个老 mux 上（`set_multiplexer`）并直接返回——**不会**再 `run`。
- [src/udt.rs:212-224](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L212-L224) 是**新建分支**：`UdtMultiplexer::new` / `bind` 创建后，`UdtMultiplexer::run(mux)` 启动两个 worker。`run` 只出现在这里。

这正是本讲实践任务要回答的「复用条件」：端口相同、mss 相同、且老 mux 当时是 `reusable`（即创建时 `reuse_mux=true`）。

接下来看两个 worker 的实现。接收 worker（精读分发段）已在 4.1.3 给出，这里补充它的整体骨架与批量收包缓冲：

[src/queue/rcv_queue.rs:137-160](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L137-L160) — 开头 `let mut buf = vec![0_u8; self.mss as usize * 100];` 预留了「最多 100 个 mss 大小」的缓冲，配合 Linux 的 `recvmmsg` 一次系统调用可接收多个包（recvmmsg 细节见 u8-l4）。`UDP_RCV_TIMEOUT = 30µs`：收不到包时短暂等待，避免空转。

发送 worker（`snd_queue.worker`）：

[src/queue/snd_queue.rs:64-112](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_queue.rs#L64-L112) — 关键看点：

- 内部又 `tokio::spawn` 了一个**子任务**（[src/queue/snd_queue.rs:67-75](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_queue.rs#L67-L75)），它从 mpsc channel 收 `(socket, packets)` 并真正调 `socket.send_data_packets`。也就是说发送侧其实是「调度 worker + 发包子任务」两级。
- 主循环用 `BinaryHeap` 按「发送时刻」排序（`Ord` 反转使最小时间戳在堆顶，见 [src/queue/snd_queue.rs:18-23](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_queue.rs#L18-L23)），到点 pop 出 socket，调 `next_data_packets` 问它要包（这条主流程的细节见 u6-l1）。
- 未到点时 `select` 等待「定时到点」或「`notify` 唤醒」（[src/queue/snd_queue.rs:101-110](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_queue.rs#L101-L110)）。`notify` 由 `insert` / `update` 在有新数据要发时触发（[src/queue/snd_queue.rs:114-125](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_queue.rs#L114-L125)）。

所以严格地说：**一个 multiplexer 对应 1 个 UDP socket、2 个 worker 任务（接收 worker + 发送调度 worker），其中发送调度 worker 还会额外 spawn 一个「真正发包」的子任务。**

最后补一句：新连接握手成功后，会把自己登记进接收队列，让接收 worker 之后能把包分发给它：

[src/socket.rs:209-211](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L209-L211) — `mux.rcv_queue.push_back(self.socket_id)`，把新连接的 `socket_id` 放进接收队列的待检查列表，随后 `update` 维护它的轮转（[src/queue/rcv_queue.rs:41-52](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L41-L52)）。

#### 4.3.4 代码实践

**实践目标**：亲手回答本讲标题里的核心问题——「一个 multiplexer 对应几个 UDP socket、几个 worker 任务」，并验证 `run` 只在新建 mux 时调用。

**操作步骤（源码追踪型）**：

1. 在 [src/udt.rs:223](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L223)（`UdtMultiplexer::run(mux)`）处加一条调试日志，例如：

   ```rust
   // 示例代码：观察 mux 新建并 run 的时机
   if *UDT_DEBUG { eprintln!("NEW mux id={} port={}", mux_id, mux.port); }
   ```

2. 在 [src/multiplexer.rs:168](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/multiplexer.rs#L168) 与 [src/multiplexer.rs:172](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/multiplexer.rs#L172) 两个 spawn 各加一条日志（如 `"rcv worker started"` / `"snd worker started"`）。
3. 分别用**两种配置**启动「同一端口的服务端 + 多个客户端」，观察日志：
   - 配置 A（默认 `reuse_mux = true`）：第一个 socket 新建 mux 并 run；后续同端口、同 mss 的 socket **复用** mux，不再 run。
   - 配置 B（`reuse_mux = false`）：每个 socket 都新建自己的 mux 并 run。

**需要观察的现象**：

- 配置 A：`NEW mux` 日志只出现一次；之后多个连接共享同一个 mux。
- 配置 B：每个连接都打印一次 `NEW mux`，各自有独立 UDP socket 与 worker。

**预期结果**：你能明确回答——**复用模式下 1 个 UDP socket 服务全部连接；非复用模式下每个连接一个 UDP socket**；而无论哪种，每新建一个 mux 都恰好 spawn 2 个 worker 任务（外加发送侧 1 个子任务）。运行结果**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么复用已有 mux 时不再调用 `run`？如果误调了会怎样？

> **参考答案**：复用 mux 时它早就已经 `run` 过、两个 worker 正在跑。若再 `run` 一次，就会对同一个 UDP socket 再启动一对 worker，导致**两个接收 worker 同时读同一个 socket**——会引发争抢、包被随机分到其中一个 worker、重复处理或丢失，完全错误。所以 `run` 必须是「每个 mux 一生只调用一次」，放在新建分支里。

**练习 2**：发送 worker 为什么不直接在循环里调 `send_data_packets`，而是先通过 mpsc channel 交给一个子任务？

> **参考答案**：为了**解耦「调度」与「实际发包」**。调度 worker 的职责是严格按时间点决定「现在该轮到谁」，必须轻量、不能被某次耗时的系统调用（网络阻塞）卡住；而 `send_data_packets` 涉及实际的 UDP 写（甚至 `sendmmsg`），可能耗时。用一个有界 channel（容量 50）把「要发的包」投递给独立的发包子任务，可以让调度循环不被单次发包阻塞，多个 socket 的发包还能在子任务里得到并发处理。这是典型的生产者-消费者分工。

**练习 3**：`snd_queue` 用 `BinaryHeap` 而 `rcv_queue` 用 `VecDeque`，为什么不一样？

> **参考答案**：发送侧需要**按发送时刻调度**——谁的时刻最早谁先发，这是优先级语义，用 `BinaryHeap`（最小堆，靠 `Ord` 反转实现）能 O(log n) 取出最早到点的 socket。接收侧没有「按时刻」的需求，它只是维护一个「最近被收过包、需要定期检查定时器」的 socket 轮转列表，先进先出 + 去重即可，用 `VecDeque` 配合 `retain`/`push_back`（[src/queue/rcv_queue.rs:48-52](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L48-L52)）足够。

---

## 5. 综合实践

把本讲三个模块串起来，做一次完整的「multiplexer 生命周期追踪」。

**任务**：在本地用一个 receiver + 两个 sender，结合两种配置，画出 multiplexer 的创建/复用与 worker 任务图。

**步骤**：

1. 在以下 4 处加调试日志（全部包在 `if *UDT_DEBUG { ... }` 里）：
   - `UdtMultiplexer::new` 完成时（[src/multiplexer.rs:53-75](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/multiplexer.rs#L53-L75)）打印 `"new mux id={} port={}"`。
   - `UdtMultiplexer::bind` 完成时（[src/multiplexer.rs:77-100](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/multiplexer.rs#L77-L100)）打印 `"bind mux id={} port={}"`。
   - `update_mux` 的复用分支命中时（[src/udt.rs:202-204](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L202-L204)）打印 `"reuse mux id={}"`。
   - `run` 里两个 spawn（[src/multiplexer.rs:167-173](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/multiplexer.rs#L167-L173)）打印 `"rcv/snd worker started for mux id={}"`。
2. 配置 A（默认 `reuse_mux=true`）：启动 receiver（会 `bind` 一个 mux）+ 两个 sender（各自 `new` 一个客户端 mux）。观察每个进程各 spawn 了几个 worker。
3. 配置 B（自定义 `reuse_mux=false` 的 listener）：再启动一个 listener 显式不复用，看它是否独立建 mux。
4. 整理观察结果，填下表：

   | 进程 | UDP socket 数 | mux 数 | worker 任务数 | 说明 |
   | --- | --- | --- | --- | --- |

**验收标准**：你能据此回答——receiver 进程内「1 个 mux = 1 个 UDP socket = 2 个 worker 任务（+ 发送子任务）」；当多个连接复用 mux 时这三者数量都不变。如无法本地运行，至少完成「源码追踪型」版本的日志设计，并标注**待本地验证**。

---

## 6. 本讲小结

- **multiplexer 是 UDP socket 与 worker 的打包单元**：一个 `UdtMultiplexer` 持有恰好 1 个 `Arc<UdpSocket>`、1 个 `snd_queue`、1 个 `rcv_queue`，外加元信息 `id/port/mss/reusable/listener`（[src/multiplexer.rs:14-25](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/multiplexer.rs#L14-L25)）。
- **多个 UdtSocket 共享一个 UDP socket**：接收 worker 按 UDT 包头里的 `dest_socket_id` 做解复用分发（[src/queue/rcv_queue.rs:162-198](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L162-L198)）；`socket_id == 0` 的握手包路由给 listener。
- **socket 与 mux 双向弱引用**：`UdtSocket.multiplexer` 是 `Weak`，队列里的 socket 也是 `Weak`，避免循环引用、配合全局 GC。
- **`new_udp_socket` 用 `socket2` 精细配置**：在 `spawn_blocking` 里设收发缓冲区（`SO_RCVBUF/SO_SNDBUF`）、`SO_REUSEPORT`、非阻塞，再 `bind`、转 tokio socket（[src/multiplexer.rs:28-51](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/multiplexer.rs#L28-L51)）。
- **复用 vs 新建由 `update_mux` 决定**：`reuse_mux=true` 且端口相同、mss 相同、老 mux `reusable` 时复用（不 `run`）；否则新建并 `run`（[src/udt.rs:191-225](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L191-L225)）。
- **`run` 启动 2 个 worker 任务**：接收 worker（收包+分发+定时器检查）与发送调度 worker（按时刻调度，另有 1 个发包子任务）（[src/multiplexer.rs:167-173](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/multiplexer.rs#L167-L173)）。

---

## 7. 下一步学习建议

本讲讲清了「multiplexer 的骨架与两个 worker 的职责」，但 worker 内部的细节留在了后续讲义：

- **想深入发送 worker 内部**：去 u5-l1（发送队列与发送缓冲）看 `BinaryHeap` 调度、`SndBuffer` 的切片与重传，以及 u6-l1（发送主流程）看 `next_data_packets` 如何受拥塞窗口限流。
- **想深入接收 worker 内部**：去 u5-l2（接收队列与接收缓冲）看 `RcvBuffer` 的乱序重组，以及 u6-l2（接收与 ACK）看 `process_data` 如何检测丢包、生成 ACK。
- **想看平台快路径**：本讲提到的 `sendmmsg`/`recvmmsg` 与 Linux 专用 `timerfd` 高精度定时，在 u8-l4（平台快路径）有完整对比。
- **想理解握手如何路由到 listener**：`socket_id == 0` 分支调用的 `listen_on_handshake` 与 SYN cookie，在 u8-l1（握手与 SYN cookie）。

建议顺序：u5（数据通路）→ u6（可靠性）→ u7（拥塞控制）→ u8（生命周期与平台优化），把 multiplexer 这颗「心脏」的每条血管都走一遍。

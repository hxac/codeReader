# 接收侧：收包队列 UdtRcvQueue 与接收缓冲 RcvBuffer

## 1. 本讲目标

本讲拆解 tokio-udt **接收侧**的两条主线：一个是 multiplexer 级别的「收包队列」`UdtRcvQueue`，负责从共享 UDP socket 批量收包并按目标 socket 分发；另一个是 socket 级别的「接收缓冲」`RcvBuffer`，负责把乱序到达的数据包重组为按序可读的字节流。

学完后你应当能够：

- 说清楚 `UdtRcvQueue::worker` 的主循环：收包 → 反序列化 → 按 `dest_socket_id` 分发 → 周期性检查定时器。
- 解释 `dest_socket_id == 0` 的握手包为何要被特殊路由到 listener。
- 读懂 `RcvBuffer` 用 `BTreeMap` 做乱序插入、用 `next_to_read` / `next_to_ack` 两个游标做按序读取的设计，并能讲清「读窗口跨最大值回绕时拼接两段 range」的实现。
- 把 `ack_data`（推进可读水位）与 `has_data_to_read`（判断是否有数据可读）的协作，对应到 tokio `Notify` 唤醒读者的异步模式上。

本讲只覆盖「收包分发、握手路由、接收缓冲重组」三件事；数据包内部的位域格式见 u4-l2，丢包后发 NAK、生成 ACK 等可靠性细节见 u6-l2。

## 2. 前置知识

在进入源码前，先用通俗语言过一遍本讲需要的基础概念。

- **UDP 是无连接、不可靠的**：UDP 只管把一个个数据报（datagram）扔出去，不保证到达、不保证顺序、可能重复。UDT 要在 UDP 之上提供「可靠、按序」的流式传输，就必须自己在应用层做「乱序重组 + 丢包重传」。
- **多路复用 / 解复用（multiplexing / demultiplexing）**：一个 multiplexer 只持有一个 UDP socket，却要服务多条逻辑连接（多个 `UdtSocket`）。收包时必须根据包头里的「目标 socket id」把包送到正确的连接，这个动作叫**解复用**。这部分背景在 u3-l3 已建立。
- **`BTreeMap` 是有序映射**：键按顺序排列，可以用 `range(a..b)` 取出一段连续键区间。`RcvBuffer` 正是利用「有序」来快速定位乱序包该插在哪里、可读区间是哪一段。
- **tokio 的 `select!` 与 `readable()`**：`UdpSocket::readable()` 返回一个 future，当 socket 可读时完成；`select!` 让我们在「socket 可读」和「睡一小会儿」之间二选一，避免空转忙等。
- **`ReadBuf<'_>`**：tokio 异步读取用的缓冲区句柄，`remaining()` 返回还能写多少字节，`put_slice(&[u8])` 往里追加数据。
- **循环序列号**：UDT 的包序号在 31 位空间里循环递增，会从最大值「回绕」回 0。两套比较语义很关键——派生 `Ord` 是原始 `u32` 顺序（给 `BTreeMap` 排序用），`Sub` 返回带符号的「环上最短距离」（给窗口/缺口判断用）。详见 u4-l4。
- **`tokio::sync::Notify` 唤醒模式**：生产者推进状态后 `notify_waiters()`，消费者被唤醒后重新检查条件——这是 tokio 版的「条件变量」。本讲会看到 `rcv_notify` 如何在 `ack_data` 之后唤醒被阻塞的读取者（u2-l2 的 `accept_notify`、u3-l2 的三个 Notify 是同类设计）。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 角色 | 一句话职责 |
| --- | --- | --- |
| `src/queue/rcv_queue.rs` | 数据通路 | multiplexer 级收包队列：批量收 UDP 包、按 `dest_socket_id` 解复用分发、周期性驱动各 socket 的定时器。 |
| `src/queue/rcv_buffer.rs` | 数据通路 | socket 级接收缓冲：用 `BTreeMap` 存乱序到达的数据包，维护两个游标实现按序读取与流控。 |
| `src/queue/mod.rs` | 入口 | `queue` 子模块声明，把 `RcvBuffer` / `UdtRcvQueue` 等以 `pub(crate) use` 重导出。 |
| `src/packet.rs` | 协议格式 | `UdtPacket` 统一入口：`deserialize` / `get_dest_socket_id` / `handshake` 三个方法被收包 worker 直接调用。 |
| `src/socket.rs` | 数据通路 / 可靠性 | 提供 worker 回调进入点：`process_packet` / `process_data` / `check_timers` / `recv` / `poll_recv`，并持有 `RcvBuffer`。 |
| `src/multiplexer.rs` | 入口 | `UdtMultiplexer` 持有 `rcv_queue` 与 `listener`，并在 `run` 中 spawn 接收 worker。 |

> 提醒：`UdtRcvQueue`、`RcvBuffer` 都是 `pub(crate)` 内部类型，不出现在公共 API 里（公共 API 边界见 u1-l4）。用户只通过 `UdtConnection::read` 间接触发它们。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **UdtRcvQueue 收包分发**——批量收包、解复用、定时器驱动。
2. **握手路由**——`dest_socket_id == 0` 的特殊路径。
3. **RcvBuffer 乱序重组 / 读取**——两个游标、回绕拼接、与 `Notify` 的协作。

### 4.1 UdtRcvQueue：批量收包与解复用分发

#### 4.1.1 概念说明

`UdtRcvQueue` 是**每个 multiplexer 恰好一个**的接收队列（u3-l3 已建立「一个 mux 一个 UDP socket、一个发送队列、一个接收队列」的结构）。它的职责不是「存数据」，而是「**收包 + 分发 + 驱动定时器**」：

- **收包**：从共享的 `Arc<UdpSocket>` 一次尽量多收几个 UDP 包（Linux 上用 `recvmmsg` 批量收，最多 100 个）。
- **分发**：根据每个包的 `dest_socket_id` 找到对应的 `UdtSocket`，把包交给它的 `process_packet`。
- **驱动定时器**：即使某条连接暂时没收到包，它的各种定时器（ACK 定时、EXP 超时等，见 u7-l3）也需要周期性触发。`UdtRcvQueue` 额外维护一张「活跃 socket 列表」，每 100ms 唤醒一次其中的 socket 去跑 `check_timers`。

要特别区分队列里的两个字段，初学者很容易混淆：

- `socket_refs`：**解复用注册表**（`SocketId → Weak<UdtSocket>`），收包后用它找 socket。
- `sockets`：**定时器轮转表**（`VecDeque<(Instant, SocketId)>`），驱动周期性 `check_timers`，与「存包」无关。

#### 4.1.2 核心流程

收包 worker 一次主循环做两件事：先「收包并分发」，再「扫描定时器表」。伪代码如下：

```
loop:
    buf = [0u8; mss * 100]               # 一次性大缓冲，复用
    msgs = receive_packets(buf)          # 批量收，Linux 用 recvmmsg

    if msgs 为空:
        select! { 睡 30µs , 或 socket.readable() }   # 不忙等
        本轮不分发
    else:
        for (nbytes, addr) in msgs:
            packet = UdtPacket::deserialize(buf[..nbytes])   # 失败则丢弃
            socket_id = packet.get_dest_socket_id()
            if socket_id == 0:
                路由到 listener.listen_on_handshake(...)     # 握手特殊路径
            else if 找得到 socket 且 peer_addr 匹配 且 存活:
                socket.process_packet(packet)                # 业务分发
                socket.check_timers()                        # 顺便跑定时器
                update(socket_id)                            # 该 socket 移到定时器表队尾
            else:
                忽略（调试打印）

    # 定时器扫描：把「超过 100ms 没活动」的 socket 从队首弹出，跑 check_timers
    for socket_id in 队首 elapsed > 100ms 的项:
        socket.check_timers()
        update(socket_id)                                    # 重新排到队尾，刷新时间戳
```

两个值得注意的设计：

- **「到点收」与「可读即收」混合**：没有包时不是死循环 `recv`，而是 `select!` 在「睡 30µs」和「socket 可读」之间竞争——要么马上被可读事件唤醒，要么最多空转 30 微秒。这是「忙等」与「纯阻塞」之间的折中。
- **`check_timers` 的双重触发**：收到包时**就地**调一次 `check_timers`（行内）；没收到包时，靠定时器表每 100ms 兜底调一次。因此每条活跃连接的定时器至多延迟约 100ms 触发。

#### 4.1.3 源码精读

先看结构体定义与两个常量：

[src/queue/rcv_queue.rs:18-19](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L18-L19) 定义了两个时间常量：`TIMERS_CHECK_INTERVAL = 100ms`（多久跑一次兜底定时器），`UDP_RCV_TIMEOUT = 30µs`（空收时的短暂睡眠）。

[src/queue/rcv_queue.rs:21-28](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L21-L28) 是结构体本体：`channel` 是共享 UDP socket，`mss` 决定单包最大长度，`multiplexer` 是指向所属 mux 的 `Weak`（避免循环引用，配合全局 GC，见 u3-l3），`socket_refs` 是解复用注册表，`sockets` 是定时器轮转表。

收包的核心是平台分支函数。Linux 走 `recvmmsg` 一次收一批：

[src/queue/rcv_queue.rs:73-119](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L73-L119) 把传入的大 `buf` 按 `mss` 切成最多 100 个分片，作为 `recvmmsg` 的 iov 缓冲，用 `try_io(Interest::READABLE, ...)` 在 socket 可读时**一次系统调用收多个包**，再把每个包的 `(字节数, 对端地址)` 收集返回。这是 u8-l4 要细讲的 Linux 快路径。非 Linux 走 [src/queue/rcv_queue.rs:121-135](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L121-L135)，用循环 `try_recv_from` 逐个收，遇到 `WouldBlock` 即停。

整个收包主循环在 worker 方法里：

[src/queue/rcv_queue.rs:137-160](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L137-L160) 先分配 `mss * 100` 字节的大缓冲（只分配一次，循环复用），调用 `receive_packets`；若没收到包，就用 `select!` 在 `sleep(UDP_RCV_TIMEOUT)` 和 `self.channel.readable()` 之间二选一；收到了就把每个 `(nbytes, addr)` 与对应缓冲分片 zip，逐个 `UdtPacket::deserialize`，解析失败用 `filter_map(...).ok()?` 静默丢弃（UDP 不可靠，丢掉坏包是合理策略，对应 u4-l1 讲过的「`.ok()?` 丢弃」）。

分发逻辑是本模块的重点：

[src/queue/rcv_queue.rs:162-198](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L162-L198) 取出 `packet.get_dest_socket_id()` 作为解复用键。三条分支：

1. `socket_id == 0`：握手包特殊路径（见 4.2）。若不是握手包则直接返回 `InvalidData` 错误——`socket_id == 0` 且非握手属于协议违例。
2. 能找到 socket 且 `peer_addr() == Some(addr)` 且 `status().is_alive()`：调 `socket.process_packet(packet)`（按数据/控制包分流），再 `socket.check_timers()`，最后 `self.update(socket_id)` 把它移到定时器表队尾。`peer_addr` 校验防止伪造源地址的包污染连接。
3. 其它：忽略（`UDT_DEBUG` 打开时打印），留有 rendezvous 模式的 TODO。

`get_socket` 做解复用键查找，带一层缓存：

[src/queue/rcv_queue.rs:58-71](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L58-L71) 先查本地 `socket_refs` 缓存（`Weak<UdtSocket>`），命中且能 `upgrade()` 就直接用；未命中再去全局 `Udt::get().read().await.get_socket(socket_id)` 查（u3-l1 的总账本），并回填缓存。这样热点连接的解复用无需每次都拿全局读锁。

定时器扫描段：

[src/queue/rcv_queue.rs:200-220](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L200-L220) 从 `sockets` 队首不断弹出「时间戳已超过 100ms」的项，对每个 socket 跑 `check_timers()` 并 `update()` 把它重新排到队尾。配合 `update` 的实现 [src/queue/rcv_queue.rs:48-52](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L48-L52)（`retain` 删旧记录、`push_back` 加新时间戳），「最近活跃的 socket 总在队尾、最久没活动的总在队首」，保证每条活跃连接大约每 100ms 被兜底检查一次。

最后，这条 worker 由 multiplexer 在 `run` 里 spawn：

[src/multiplexer.rs:167-173](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/multiplexer.rs#L167-L173) 每个 mux 启动时 spawn 接收 worker 与发送 worker 各一个。接收 worker 的存在周期与 mux 相同。

#### 4.1.4 代码实践

**实践目标**：在源码层面跟踪一次「收包 → 分发 → 定时器」的完整路径，并理解空收时的退避。

**操作步骤（源码阅读型）**：

1. 打开 `src/queue/rcv_queue.rs`，定位 `worker` 方法（L137）。
2. 顺着调用链读：`receive_packets`（L73 / L121）→ `UdtPacket::deserialize`（`src/packet.rs` L26）→ `get_dest_socket_id`（`src/packet.rs` L12）→ `process_packet`（`src/socket.rs` L435）→ `check_timers`（`src/socket.rs` L887）。
3. 在 L162 的 `for (packet, addr) in packets.into_iter().flatten()` 处， mentally 跟一个数据包走「分支 2」：`get_socket` → `peer_addr` 校验 → `process_packet` → `check_timers` → `update`。

**需要观察的现象（若本地改代码加日志）**：在 L141 的 `receive_packets` 返回后打印 `msgs.len()`，在 L182 的 `get_socket` 命中后打印 `socket_id`。跑 u1-l2 的 `udt_sender` / `udt_receiver` 一对进程，应能看到 receiver 侧每轮收到若干包、并按 `socket_id` 分发到对应连接。

**预期结果**：高负载时 `msgs.len()` 经常接近批量上限；空闲时 worker 大量时间花在 L154 的 `select!` 上（要么被 `readable()` 立即唤醒，要么睡满 30µs）。**待本地验证**（本讲不修改源码，仅描述应观察到的现象）。

#### 4.1.5 小练习与答案

**练习 1**：`UdtRcvQueue` 里的 `sockets` 字段和 `socket_refs` 字段分别装的是什么？为什么不能合并成一个？

> **答案**：`socket_refs` 是解复用注册表（`SocketId → Weak<UdtSocket>`），收包后用来「按 id 找 socket」；`sockets` 是定时器轮转表（`VecDeque<(Instant, SocketId)>`），用来「按时间轮转驱动 check_timers」。两者用途完全不同——前者是「id → 对象」映射，后者是「带时间戳的轮转队列」，故不能合并。

**练习 2**：worker 在没收到包时为什么不直接 `continue`，而要走 `select! { sleep(30µs), readable() }`？

> **答案**：直接 `continue` 会变成 100% CPU 的忙等空转。`select!` 在「马上可读」时被 `readable()` 立即唤醒（低延迟），在不可读时最多睡 30µs（让出 CPU）。这是延迟与 CPU 占用的折中。

**练习 3**：为什么对找到的 socket 还要再判一次 `socket.peer_addr() == Some(addr)`？

> **答案**：`dest_socket_id` 是包里声明的目标，但 UDP 源地址可被伪造。校验「包的实际对端地址 == 该连接已记录的 peer_addr」能挡住来源不匹配的包，避免把陌生对端的数据塞进既有连接。

### 4.2 握手路由：dest_socket_id == 0 的特殊路径

#### 4.2.1 概念说明

当一个客户端想连到服务端时，它面临一个「先有鸡还是先有蛋」的问题：握手包要填 `dest_socket_id`，可客户端此时还不知道服务端 socket 的 id。UDT 的约定是：**第一轮握手包的 `dest_socket_id` 填 0**，表示「发给这个 UDP 端口上的 listener，无论它是谁」。

因此 `socket_id == 0` 在收包 worker 里被当作「握手广播地址」特殊处理：只有握手包（Handshake 控制包）才允许走这条路，由该 mux 上注册的 listener 接手；任何 `socket_id == 0` 的非握手包都视为协议错误。

这部分依赖 u4-l3 讲过的控制包格式（Handshake 类型、`connection_type` 三态）与 u8-l1 将要详述的握手时序，本讲只关注「收包侧如何把它路由出去」。

#### 4.2.2 核心流程

```
收到的 packet，其 get_dest_socket_id() == 0:
    if packet.handshake() 返回 Some(handshake):     # 是握手控制包
        mux = self.multiplexer.upgrade()             # 拿到所属 mux
        listener = mux.listener.read().await         # 读 mux 上的 listener
        if let Some(listener) = listener:
            listener.listen_on_handshake(addr, handshake).await   # 交给 listener
    else:
        返回 InvalidData("received non-hanshake packet with socket 0")
```

要点：

- `mux.listener` 是 `RwLock<Option<SocketRef>>`，一个 mux 至多一个 listener（u3-l3）。客户端发起的 `connect` 发出首个 `connection_type = 1` 的握手包（u2-l1），就靠这里被 listener 接到。
- 只有「mux 上确实注册了 listener」时握手才会被处理；否则该握手包被静默丢弃（没有 listener 就没人应答）。

#### 4.2.3 源码精读

握手路由分支就在分发循环里：

[src/queue/rcv_queue.rs:164-181](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L164-L181) 当 `socket_id == 0` 时，先用 `packet.handshake()` 判定它是不是握手包；是则取出 `self.multiplexer` 的 `Weak` 并 `upgrade()`，再读 `mux.listener`，若存在 listener 就调 `listener.listen_on_handshake(addr, handshake)`；若 `socket_id == 0` 却不是握手包，直接返回 `InvalidData` 错误。

判定「是不是握手包」靠 `UdtPacket::handshake`：

[src/packet.rs:41-49](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/packet.rs#L41-L49) 只有 `Control(Handshake)` 变体返回 `Some(&HandShakeInfo)`，其余都返回 `None`。这是 u4-l1 讲过的「按类型分发」的一个具体应用。

`get_dest_socket_id` 则抹平了数据包与控制包的字段位置差异，统一给出解复用键：

[src/packet.rs:12-17](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/packet.rs#L12-L17) 控制包取 `p.dest_socket_id`，数据包取 `p.header.dest_socket_id`，让收包侧无需关心包类型即可拿到目标 id。

而 listener 字段本身定义在 multiplexer 上：

[src/multiplexer.rs:23-24](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/multiplexer.rs#L23-L24) （`pub(crate) rcv_queue: UdtRcvQueue` 与 `pub listener: RwLock<Option<SocketRef>>`），`UdtListener::bind` 时会把自己写进这个 `listener` 槽位（u2-l2），握手包才能在此被接住。

#### 4.2.4 代码实践

**实践目标**：搞清「`socket_id == 0` 的握手包如何变成一条新连接」的入口。

**操作步骤（源码阅读型）**：

1. 从 [src/queue/rcv_queue.rs:173](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L173) 的 `listener.listen_on_handshake(addr, handshake).await` 出发。
2. 跳到 `src/socket.rs` 的 `listen_on_handshake`（L376）阅读其签名与首部，理解它如何回 SYN cookie 并最终经全局 `Udt::new_connection` 建立新 socket（详见 u8-l1）。
3. 回头确认：客户端首个握手包的 `dest_socket_id` 为 0、`connection_type = 1`（u2-l1 的 `UdtSocket::connect`）。

**需要观察的现象**：一个 listener、一个 connect 的客户端，应只触发一次 `socket_id == 0` 分支；之后的双向数据包 `dest_socket_id` 都是非 0（指向已建立连接的 socket）。

**预期结果**：`socket_id == 0` 分支只在握手阶段出现，数据传输阶段不再进入。握手细节与 SYN cookie 的容错逻辑（`-1` 校验）见 u8-l1，本讲不展开。

#### 4.2.5 小练习与答案

**练习 1**：为什么第一轮握手包的 `dest_socket_id` 要填 0？

> **答案**：客户端发起连接时尚不知道服务端 socket 的 id，无法填写有效目标。约定填 0 表示「发给本 UDP 端口上的 listener」，由收包 worker 特殊路由。

**练习 2**：如果 `socket_id == 0` 的包不是握手包，会发生什么？

> **答案**：worker 在 [src/queue/rcv_queue.rs:176-181](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L176-L181) 返回 `InvalidData("received non-hanshake packet with socket 0")` 错误，worker 任务会因此终止（`run` 里 `.unwrap()`）。这是协议违例的防御性处理。

**练习 3**：一个 multiplexer 上能注册几个 listener？如果没有 listener，握手包会怎样？

> **答案**：至多一个（`listener: RwLock<Option<SocketRef>>`）。若没有 listener，`&*listener` 为 `None`，`if let Some(listener)` 不匹配，握手包被静默丢弃——没有 listener 就无人应答。

### 4.3 RcvBuffer：乱序重组与按序读取

#### 4.3.1 概念说明

`RcvBuffer` 是**每个 socket 一个**的接收缓冲，装在 `UdtSocket` 里（`rcv_buffer: Mutex<RcvBuffer>`）。它解决两个问题：

- **乱序重组**：UDP 包可能乱序到达、可能重复、可能丢包后由对端重传补上。`RcvBuffer` 用 `BTreeMap<SeqNumber, UdtDataPacket>` 按序号存放已到达的包，重复包用 `entry().or_insert()` 自动去重。
- **按序读取**：用户调用 `read` 时只能拿到「按序连续」的数据。`RcvBuffer` 用两个游标界定「可读窗口」：
  - `next_to_read`：**读游标**，下一个要交给用户的序号，在 `read_buffer` 里随消费推进。
  - `next_to_ack`：**确认水位**，接收方愿意向上报告「已连续收到此处」的高水位，在 `send_ack` 里通过 `ack_data` 推进。

可读窗口就是循环区间 \([next\_to\_read,\ next\_to\_ack)\)，其长度（循环意义下）为：

\[
\text{readable} = next\_to\_ack - next\_to\_read \quad(\ge 0)
\]

这个窗口在 31 位循环空间里会从最大值回绕回 0，于是它在「原始 `u32` 顺序」下可能表现为**两段**，这正是 `read_buffer` 要拼接两段 range 的原因（见 4.3.3）。

> 注意区分两种比较：`RcvBuffer` 内部所有范围/区间判断都用 `SeqNumber` **派生 `Ord`（原始 `u32` 顺序）**，因为 `BTreeMap` 就是按它排键的；而 `ack_data` 里判断「是否前进」用的是 `Sub` 返回的**循环带符号距离**。这套区分在 u4-l4 已建立。

#### 4.3.2 核心流程

数据从「到达」到「被读走」的流程：

```
process_data(packet):                      # 收包 worker 经 process_packet 调入
    seq = packet.header.seq_number
    offset = seq - last_sent_ack            # 循环距离
    if offset < 0: 丢弃（太老的包）          # 已确认过的重复/迟到包
    if available_buf_size < offset: 丢弃     # 流控：缓冲不够，先别收这么远
    rcv_buffer.insert(packet)               # 进 BTreeMap，自动按序号归位、去重
    if (seq - curr_rcv_seq_number) > 1:     # 中间有缺口 → 丢包
        记 rcv_loss_list 并立即发 NAK        # 可靠性，见 u6-l2 / u6-l3
    （后续 send_ack 阶段）
    send_ack: 若 seq 越过 last_sent_ack:
        rcv_buffer.ack_data(seq)            # 推进 next_to_ack（右扩可读窗口）
        rcv_notify.notify_waiters()         # 唤醒被阻塞的读取者

recv / poll_recv（用户读）:
    if !rcv_buffer.has_data_to_read():      # 可读窗口里没有包
        spawn 等待 rcv_notify，Pending
    written = rcv_buffer.read_buffer(buf)   # 按序拷出，推进 next_to_read
```

要点：

- **写入即归位**：`insert` 把包塞进 `BTreeMap`，键就是 `seq_number`，天然按序排列、天然去重，不需要手动排序。
- **可读 = 已连续**：`next_to_ack` 只在「连续数据」向前推进时才移动（缺口未填满前不会越过缺口），所以可读窗口 \([next\_to\_read,\ next\_to\_ack)\) 内一定是有序、完整的，`read_buffer` 可以线性地一口气拷出。
- **生产者推进 + 唤醒，消费者检查 + 消费**：`ack_data` 右扩窗口后立刻 `rcv_notify.notify_waiters()`；阻塞中的读取者被唤醒后重新 `has_data_to_read()` 判断，再 `read_buffer` 消费。这是 tokio 版「条件变量」模式（u2-l2 的 `accept_notify` 同款）。

#### 4.3.3 源码精读

结构体只有四个字段：

[src/queue/rcv_buffer.rs:6-12](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_buffer.rs#L6-L12) `packets: BTreeMap<SeqNumber, UdtDataPacket>` 存乱序包，`max_size` 是容量（流控用），`next_to_read` / `next_to_ack` 是两个游标。

[src/queue/rcv_buffer.rs:15-22](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_buffer.rs#L15-L22) 构造时两个游标都初始化为 `initial_seq_number`（即连接的 ISN，由 `UdtSocket::new` 传入，见 [src/socket.rs:110-113](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L110-L113)），此时可读窗口长度为 0。`max_size` 来自 `configuration.rcv_buf_size`，默认 `DEFAULT_UDT_BUF_SIZE * 2 = 163840` 个包（见 [src/configuration.rs:62](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/configuration.rs#L62) 与 [src/configuration.rs:3-5](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/configuration.rs#L3-L5)）。

插入与去重：

[src/queue/rcv_buffer.rs:28-31](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_buffer.rs#L28-L31) `entry(seq_number).or_insert(packet)`——键已存在则不动，自动忽略重复/重传包。

流控可用空间：

[src/queue/rcv_buffer.rs:24-26](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_buffer.rs#L24-L26) `max_size - packets.len()`。`process_data` 在 [src/socket.rs:704-717](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L704-L717) 里据此决定是否收包：若 `available_buf_size < offset`（要收的序号太靠前、缓冲装不下）就丢弃当前包并返回，避免缓冲溢出。

推进确认水位：

[src/queue/rcv_buffer.rs:38-42](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_buffer.rs#L38-L42) 只有当 `(to - self.next_to_ack) > 0`（循环 `Sub` 返回正数，即 `to` 在环上确实在前方）时才把 `next_to_ack` 前移。它由 `send_ack` 在 [src/socket.rs:812-820](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L812-L820) 调用：当 `seq_number - last_sent_ack > 0` 时 `ack_data(seq_number)`、更新 `last_sent_ack`、并 `rcv_notify.notify_waiters()` 唤醒读取者。这正是「生产者推进水位 + 通知」的一侧。

判断是否有数据可读（含回绕）：

[src/queue/rcv_buffer.rs:44-57](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_buffer.rs#L44-L57) 用**派生 `Ord`** 比较 `first = next_to_read` 与 `last = next_to_ack`：

- `first <= last`（原始顺序未跨边界）：在 `range(first..last)` 里找有没有包。
- 否则（窗口跨过最大值回绕）：分别在 `range(first..=max())` 与 `range(zero..last)` 两段里找，任一段有包即返回 true。

按序读取（本模块的核心，重点讲回绕拼接）：

[src/queue/rcv_buffer.rs:59-96](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_buffer.rs#L59-L96) 先判 `next_to_read == next_to_ack` 直接返回 0（窗口为空）。否则构造迭代器：

```
if next_to_read <= next_to_ack:                      # 未回绕
    packets.range(next_to_read .. next_to_ack)
        .chain(packets.range(zero .. zero))           # 空 range，仅为统一类型
else:                                                 # 回绕
    packets.range(next_to_read ..= SeqNumber::max())  # 后半段：到最大值
        .chain(packets.range(zero .. next_to_ack))    # 前半段：从 0 起
```

「在 `next_to_read` 跨越 max 回绕时拼接两段 range」就体现在 `else` 分支：可读窗口在循环空间里是一段连续区间，但映射到原始 `u32` 顺序后，被最大值「切断」成了 `[next_to_read, max]` 和 `[0, next_to_ack)` 两段，需要用 `chain` 把它们首尾拼起来，才能按序遍历完整个可读窗口。

关于 `if` 分支里那个看似多余的 `range(zero..zero)`：它是**类型统一**的手段。`BTreeMap::range` 无论接收 `a..b` 还是 `a..=b`，返回的都是同一个迭代器类型 `btree_map::Range`；两个分支都要 `chain` 两个 `Range` 才能让 `if/else` 两臂类型一致，于是非回绕分支补了一个空范围（`zero..zero` 是空集，不影响结果）。

随后的循环（L80-L89）：对每个 `(key, packet)`，若 `buf.remaining() < packet.data.len()` 就 `break`（用户缓冲装不下整包，停下来等下次再读），否则 `buf.put_slice(&packet.data)` 拷贝、累计 `written`、把 `key` 加入待删列表、并把 `next_to_read = *key + 1`（循环加法 `Add<i32>`，自动回绕）。循环结束后统一删除已消费的键。注意它**整包整包**地拷，不会把一个包拆开。

消费侧的入口：

[src/socket.rs:1059](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1059)（`recv`）与 [src/socket.rs:1094](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1094)（`poll_recv`）都在确认 `has_data_to_read()` 为真后调用 `rcv_buffer().read_buffer(...)`；其中 `rcv_buffer()` 是 [src/socket.rs:154-156](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L154-L156) 提供的 `MutexGuard` 取锁小帮手。

#### 4.3.4 代码实践

**实践目标**：手工验证 `read_buffer` 的回绕拼接逻辑，并刻画 `ack_data` 与 `has_data_to_read` 的协作。

**操作步骤（源码阅读 + 手工推演）**：

1. 假设 `SeqNumber::max()` 对应原始值 `0x7fff_ffff`。构造一个回绕场景：`next_to_read = 0x7fff_fffe`，`next_to_ack = 2`（即窗口跨过 max）。此时 `first > last`，走 `else` 分支，应遍历 `range(0x7fff_fffe ..= max)` 与 `range(0 .. 2)` 两段。
2. 在脑中向 `packets` 插入键 `0x7fff_fffe`、`0x7fff_ffff`、`0`、`1` 四个包，调用 `read_buffer`，验证它按 `0x7fff_fffe → 0x7fff_ffff → 0 → 1` 的顺序输出，且每步 `next_to_read` 经 `+1` 回绕推进（`0x7fff_ffff + 1 = 0`）。
3. 对照 `ack_data`（[src/queue/rcv_buffer.rs:38-42](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_buffer.rs#L38-L42)）与 `send_ack` 里的调用（[src/socket.rs:812-820](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L812-L820)）：`ack_data` 右扩 `next_to_ack` → 紧接着 `rcv_notify.notify_waiters()` → 被唤醒的 `poll_recv` 重判 `has_data_to_read()`（现为 true）→ `read_buffer` 消费并右移 `next_to_read`。

**需要观察的现象**：回绕场景下 `read_buffer` 仍能输出单调递增（循环意义）的序号序列；非回绕场景下走 `if` 分支，那段空 `range(zero..zero)` 不产生任何元素。

**预期结果**：手工推演与代码行为一致。若想跑真实用例，可为 `RcvBuffer` 补一个单元测试（见小练习 3），但 `RcvBuffer` 是 `pub(crate)`，测试需放在 crate 内 `#[cfg(test)]` 模块。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`next_to_read` 和 `next_to_ack` 分别由谁推进？可读窗口是哪一个区间？

> **答案**：`next_to_read` 由 `read_buffer` 在消费包时推进（`next_to_read = *key + 1`）；`next_to_ack` 由 `send_ack` 经 `ack_data` 在连续数据到达时推进。可读窗口是循环区间 \([next\_to\_read,\ next\_to\_ack)\)。

**练习 2**：`read_buffer` 的 `if` 分支里为什么要 `chain(packets.range(zero..zero))` 这段空范围？

> **答案**：为了让 `if/else` 两臂产生相同的迭代器类型（`Chain<btree_map::Range, btree_map::Range>`）。回绕分支必须 `chain` 两段真实范围，非回绕分支只有一段，于是补一个空范围凑齐相同的 `chain` 结构；`zero..zero` 是空集，不影响结果。

**练习 3**：为 `RcvBuffer` 设计一个回绕场景的单元测试，验证按序读取。

> **答案**（示例代码，非项目原有代码）：
> ```rust
> // 示例代码：放在 crate 内的 #[cfg(test)] 模块
> // 设 max = 0x7fff_ffff
> let mut buf = RcvBuffer::new(16, 0x7fff_fffe.into()); // next_to_read = next_to_ack = 0x7fff_fffe
> // 模拟 send_ack 把 next_to_ack 推进到 2（跨过 max 回绕）
> buf.ack_data(2.into());
> // 插入窗口内的 4 个包（乱序、含回绕）
> buf.insert(make_data_packet(0.into()));
> buf.insert(make_data_packet(0x7fff_ffff.into()));
> buf.insert(make_data_packet(1.into()));
> buf.insert(make_data_packet(0x7fff_fffe.into()));
> let mut out = tokio::io::ReadBuf::new(&mut [0u8; 1024]);
> let n = buf.read_buffer(&mut out);
> // 期望按序读到 0x7fff_fffe → 0x7fff_ffff → 0 → 1
> assert!(n > 0);
> ```
> 关键是验证回绕时输出顺序仍按循环递增。

## 5. 综合实践

把三个模块串起来，完整跟踪一次收包的全链路，并用它解释 `read_buffer` 的回绕与游标协作。

**任务**：

1. **跟一次收包链路**：以 [src/queue/rcv_queue.rs:137](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L137) 的 `worker` 为起点，依次标注并复述：
   - `receive_packets`（L73 / L121）批量收包；
   - `UdtPacket::deserialize`（[src/packet.rs:26](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/packet.rs#L26)）按首比特分发解析；
   - `get_dest_socket_id`（[src/packet.rs:12](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/packet.rs#L12)）取出解复用键；
   - `process_packet`（[src/socket.rs:435](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L435)）按数据/控制分流；
   - `check_timers`（[src/socket.rs:887](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L887)）顺便驱动定时器。
2. **解释回绕拼接**：用自己的话讲清 `RcvBuffer::read_buffer`（[src/queue/rcv_buffer.rs:59-96](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_buffer.rs#L59-L96)）在 `next_to_read` 跨越 max 回绕时，如何用 `chain` 把 `[next_to_read, max]` 与 `[0, next_to_ack)` 两段 `range` 拼成一个有序遍历；并说明非回绕分支那段空 `range(zero..zero)` 是为统一 `if/else` 类型。
3. **解释游标协作**：复述 `ack_data`（推进 `next_to_ack`）→ `rcv_notify.notify_waiters()`（唤醒）→ `has_data_to_read()`（重判）→ `read_buffer`（消费、推进 `next_to_read`）这条「生产者—通知—消费者」链路，并指出它就是 tokio 版的条件变量模式。

**验收标准**：能不查代码画出「worker 主循环 → 解复用 → process_data 插入 RcvBuffer → send_ack 推进 next_to_ack 并唤醒 → recv/read_buffer 消费」的时序，并能解释回绕分支为何是两段 range。

## 6. 本讲小结

- `UdtRcvQueue` 是 multiplexer 级的收包队列，worker 主循环做两件事：**批量收包并按 `dest_socket_id` 解复用分发**，以及**每 100ms 兜底驱动各 socket 的 `check_timers`**。
- 收包用 `select! { sleep(30µs), readable() }` 在「立即可读」与「短暂退避」间折中，避免忙等；Linux 下用 `recvmmsg` 一次最多收 100 个包。
- `dest_socket_id == 0` 是握手广播地址：只有握手包允许走这条路，由 mux 上唯一的 listener 经 `listen_on_handshake` 接手。
- `RcvBuffer` 用 `BTreeMap<SeqNumber, UdtDataPacket>` 存乱序包，`entry().or_insert()` 天然去重；两个游标 `next_to_read`（读指针）与 `next_to_ack`（确认水位）界定可读窗口 \([next\_to\_read,\ next\_to\_ack)\)。
- `read_buffer` 用**派生 `Ord`** 判断窗口是否跨最大值回绕：未回绕取一段 `range`，回绕时 `chain` 两段 `range` 拼接，并用一个空 `range(zero..zero)` 统一 `if/else` 的迭代器类型。
- `ack_data` 推进水位后立即 `rcv_notify.notify_waiters()`，唤醒阻塞的 `recv`/`poll_recv` 重判 `has_data_to_read` 再消费——这是贯穿项目的 `Notify` 唤醒模式。

## 7. 下一步学习建议

- **可靠性主线**：本讲只说到「`process_data` 发现缺口就记 `rcv_loss_list` 并发 NAK」，丢包链表 `LossList` 的区间合并/拆分与 NAK 编码见 u6-l3，ACK 的 light/full 两种形态与 `ack_window` 见 u6-l2、u6-l4。
- **定时器细节**：`check_timers` 内部的 ACK 定时、EXP 指数退避、超时重传、Broken 判定见 u7-l3。
- **带宽估计**：`process_data` 里调用的 `flow.on_pkt_arrival` / `on_probe1_arrival` / `on_probe2_arrival` 属于 `UdtFlow` 的到达速率估计，见 u7-l1。
- **发送侧对照**：本讲是接收侧，对应的发送侧调度队列与发送缓冲见 u5-l1，两边结构对称，对照阅读能加深对「队列管调度、缓冲管数据」分工的理解。

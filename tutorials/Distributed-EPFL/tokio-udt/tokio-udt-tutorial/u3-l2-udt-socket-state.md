# UdtSocket 核心结构与状态机

## 1. 本讲目标

本讲是进阶单元（u3）的第二篇，承接 [u3-l1](u3-l1-global-udt-registry.md) 讲过的「全局单例 `Udt` 与三张注册表」。在那里我们知道了：所有 `UdtSocket` 都被装进全局 `Udt::sockets` 这张表里统一管理。本讲就钻进这张表里的「一行」——一个 `UdtSocket` 长什么样、内部有哪些状态。

学完后你应当能够：

1. 说出 `UdtSocket` 的全部字段分别属于哪一类（身份 / 服务端监听 / 基础设施 / 数据通路 / 拥塞控制 / 簿记 / 异步唤醒），并能解释每个字段为什么用 `Mutex` / `RwLock` / `Notify`。
2. 画出 `UdtStatus` 八种状态的合法迁移图，并指出 `is_alive()` 涵盖其中哪几种。
3. 说明 `connect_notify` / `rcv_notify` / `ack_notify` 三个 `Notify` 分别在「哪里被唤醒」「唤醒谁」。
4. 理解 `SocketState` 作为「收发簿记容器」与 `SndBuffer` / `RcvBuffer`（真正存数据）的分工差异。

本讲**不**展开算法细节：握手协议留给 [u8-l1](u8-l1-handshake-syn-cookie.md)，可靠性（ACK/NAK/重传）留给 u6，拥塞控制留给 u7。本讲只看「容器与状态」本身。

## 2. 前置知识

本讲需要你已建立以下认知（来自 u1 ~ u3-l1），这里只做一句话回顾，不再展开：

- **`SocketRef` 就是 `Arc<UdtSocket>`**：全局注册表里存的是 `Arc` 引用，多个 owner 共享同一个 socket（见 u3-l1）。
- **`std::sync::Mutex` 与 `tokio::sync::Mutex` 的区别**：前者是普通阻塞互斥锁，**绝不能跨 `.await` 持有**（否则会阻塞整个 tokio worker 线程）；后者是「异步感知」锁，`lock().await` 在争用时会让出执行权。tokio-udt 里有意识地混用两者。
- **`tokio::sync::Notify`**：一个异步信号量，`notified().await` 等待通知，`notify_waiters()` 唤醒所有等待者。常配合「锁内订阅、锁外 await」的模式使用（见 u2-l2 的 `accept_notify`）。
- **序列号是循环的**：UDT 用 31 位序列号空间，`-` 运算返回 `i32` 表示「前后方向与距离」（见 u4-l4，本讲只需知道 `isn - 1` 这种写法合法）。

一个关键直觉先放在这里，后面会反复印证：**`UdtSocket` 是一个「身份 + 一堆用不同锁保护的小账本 + 几个唤醒铃铛」的组合体**，它本身几乎不含算法，算法都委托给 `flow` / `rate_control` / `state` 这些子对象。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| [src/socket.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs) | `UdtSocket` 定义、收发主流程、握手、状态机 | 结构体字段、`UdtStatus`、三个 `Notify`、关键迁移点 |
| [src/state/socket_state.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/state/socket_state.rs) | `SocketState` 簿记容器定义与初始化 | 收发游标、loss list、定时器字段的分组 |
| [src/state/mod.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/state/mod.rs) | `state` 子模块入口，`pub(crate) use` 重导出 `SocketState` | 模块可见性 |
| [src/listener.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/listener.rs) | `UdtListener::bind` | `Init → Listening` 的迁移点 |
| [src/udt.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs) | 全局引擎与 GC | `Closing → Closed` 的迁移点 |

## 4. 核心概念与源码讲解

### 4.1 UdtSocket 结构体：字段分类与同步原语选择

#### 4.1.1 概念说明

`UdtSocket` 是整个 crate 最核心的结构体——每一条 UDT 连接（无论客户端还是服务端）在内存里就是一个 `UdtSocket` 实例，被 `Arc` 包成 `SocketRef` 后塞进全局注册表。

它看起来字段很多（25 个），但可以归成 **7 类**，一旦分类就不再凌乱：

1. **身份与对端信息**：`socket_id`、`socket_type`、`initial_seq_number`、`start_time`、`peer_addr`、`peer_socket_id`。
2. **状态机**：`status`（一把锁保护一个枚举值）。
3. **服务端监听专用**：`queued_sockets`、`accept_notify`、`listen_socket`（普通连接这几项不用）。
4. **基础设施**：`multiplexer`（持有的多路复用器，弱引用）、`configuration`（配置）。
5. **数据通路**：`snd_buffer`、`rcv_buffer`（真正存字节的地方）。
6. **拥塞控制子系统**：`flow`、`rate_control`。
7. **簿记与异步唤醒**：`state`（协议游标账本）、`connect_notify`、`rcv_notify`、`ack_notify`。

#### 4.1.2 核心流程：为什么混用三种同步原语

`UdtSocket` 的字段在锁的选择上有一套清晰规则，理解它能解释后面几乎所有代码的写法：

- **`std::sync::Mutex` / `RwLock`**：用于「短临界区」——加锁、改个值、立刻释放，**全程不跨 `.await`**。`status`、`peer_addr`、`peer_socket_id`、`state`、`snd_buffer`、`rcv_buffer`、`flow`、`rate_control`、`configuration`、`multiplexer` 都是这种。它的优点是开销小、不需要 tokio 运行时参与。
- **`tokio::sync::RwLock`**（即代码里的 `TokioRwLock`）：用于 `queued_sockets`。它是异步感知锁，用在 u2-l2 讲过的「生产者 insert 后 notify_one、消费者在 accept 里读」的协调场景，配合 `Notify` 完成异步握手通知。
- **`tokio::sync::Notify`**：三个 `*_notify` 字段。它不是「锁」，而是「铃铛」——某处 `notify_waiters()` 摇铃，另一处 `notified().await` 被唤醒。专门解决「我现在没数据/没连上，挂起等条件满足」的异步等待问题。

一句话记忆：**改值用 `std` 锁，等条件用 `Notify`，跨 await 协调用 `tokio` 锁。**

#### 4.1.3 源码精读

结构体定义：[src/socket.rs:60-86](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L60-L86) —— 这是本讲的「主角」，25 个字段全部在此。

其中身份与基础设施相关字段：

[src/socket.rs:62-68](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L62-L68) 给出 `socket_id`（本地 socket id）、`status`（状态机，用 `Mutex` 保护）、`socket_type`（流式 / 数据报）、`listen_socket`（若本 socket 是被 listener 派生的，这里记录它的「父」listener id，供 GC 清理 queued_sockets 用）、`peer_addr` / `peer_socket_id`（对端地址与 id，连接建立前为 `None`，用 `Mutex` 保护）、`initial_seq_number`（ISN，随机生成，是收发两个序列号空间的基准）。

服务端监听专用字段：[src/socket.rs:70-71](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L70-L71) —— `queued_sockets` 是「已握手完成、等待 `accept` 取走」的连接 id 集合（`TokioRwLock<BTreeSet<SocketId>>`），`accept_notify` 是通知 `accept` 调用者的铃铛。这两个字段普通客户端连接用不到。

数据通路与拥塞控制字段：[src/socket.rs:75-81](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L75-L81) —— `snd_buffer` / `rcv_buffer`（真正存待发 / 待读字节）、`flow`（带宽与 RTT 估计）、`rate_control`（拥塞窗口与发送周期）、`state`（协议游标账本，4.3 节详讲）。

三个唤醒铃铛：[src/socket.rs:83-85](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L83-L85) —— `connect_notify` / `rcv_notify` / `ack_notify`，4.1.4 与 4.2 会逐一对应「谁摇铃、谁被叫醒」。

`multiplexer` 为何用 `Weak` 而非 `Arc`：[src/socket.rs:72](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L72) —— socket 持有 `Weak<UdtMultiplexer>`，反过来 multiplexer 又持有 socket 的 `Arc`。双向都强引用会形成循环引用导致内存泄漏，所以 socket 这一侧用弱引用，需要用时再 `.upgrade()` 取回 `Arc`（见 [src/socket.rs:222-224](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L222-L224)）。

构造函数初始化：[src/socket.rs:89-124](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L89-L124) —— 注意初始状态写死为 `UdtStatus::Init`（第 101 行），ISN 默认随机（第 96 行 `SeqNumber::random`），`multiplexer` 初始为空 `Weak`（第 108 行），三个 buffer / flow / rate_control / state 都用各自的 `new` 现场创建。

#### 4.1.4 代码实践：三个 Notify 的对应关系

**实践目标**：把三个 `Notify` 字段与它们的「摇铃点」和「等待点」一一对应。

**操作步骤**：

1. 在 `src/socket.rs` 中分别搜索 `connect_notify`、`rcv_notify`、`ack_notify` 的所有出现。
2. 把每处出现归到两类：`notify_waiters()`（摇铃 / 生产侧）还是 `notified()`（等待 / 消费侧）。

**预期结果**（你可以直接核对）：

| Notify | 摇铃处（生产侧） | 等待处（消费侧） | 含义 |
|--------|------------------|------------------|------|
| `connect_notify` | [socket.rs:489](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L489)（握手回包后置 Connected）+ `notify_all` | [socket.rs:1229](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1229)（`wait_for_connection`） | 唤醒客户端 `connect` 循环里等待握手的任务 |
| `rcv_notify` | [socket.rs:819](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L819)（`send_ack` 推进 `last_sent_ack`，新数据可读）+ `notify_all` | [socket.rs:1215](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1215)（`wait_for_data_to_read`） | 唤醒 `recv` / `poll_read` 等待数据的任务 |
| `ack_notify` | [socket.rs:555](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L555)（`process_ctrl` 处理 ACK 后释放发送缓冲）+ `notify_all` | [socket.rs:1243](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1243)（`wait_for_next_ack_or_empty_snd_buffer`） | 唤醒 `poll_write`（发送缓冲满时等 ACK 腾位）与 `close` 的 linger 等待 |

需要观察的现象：注意 `notify_all()`（[socket.rs:1199-1203](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1199-L1203)）一次性摇响三个铃铛——这是关闭 / Shutdown 时的「叫醒所有可能挂起的任务，让它们重新检查状态」的标准做法。

> 说明：以上对应关系是源码阅读结论，无需运行即可核对；若想动态验证，可在每个 `notify_waiters()` 处临时加一行 `eprintln!`，再跑 `cargo run --bin udt_sender` 配合 `udt_receiver` 观察打印顺序（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：`socket_type` 字段既不是 `Mutex` 也不是 `RwLock`，而是一个裸的 `SocketType`。为什么它不需要锁？

**参考答案**：`socket_type` 在 `UdtSocket::new` 时确定（流式或数据报），连接整个生命周期内不可变，所以是只读字段，无需任何同步保护。同理 `socket_id`、`initial_seq_number`、`start_time` 也是构造后不变的裸字段。

**练习 2**：假设有人误把 `state` 字段从 `std::sync::Mutex` 改成 `tokio::sync::Mutex`，现有代码还能编译运行，但有什么潜在问题？

**参考答案**：功能上仍正确，但 `tokio::sync::Mutex` 开销更大（需要运行时参与、分配permit），而 `state` 的所有临界区都是「改几个游标值就释放」的短操作、从不跨 `.await`，用异步锁是浪费。这正是作者有意识地选 `std` 锁的原因。

### 4.2 UdtStatus 状态机

#### 4.2.1 概念说明

每条 UDT 连接都有一个生命周期：刚创建 → 绑定 / 发起连接 → 连上 → 正常收发 → 关闭 → 被回收。`UdtStatus` 用 8 个枚举值刻画这条生命线上的「当前阶段」。状态机的作用有两个：

1. **给收发逻辑当守卫**：比如 `send` 要求状态必须是 `Connected`（[socket.rs:991](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L991)），`next_data_packets` 要求 `is_alive()`（[socket.rs:227](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L227)）。
2. **给 GC 当判据**：全局 `garbage_collect_sockets` 按 `Broken` / `Closing` 区分两种回收路径（见 u3-l1）。

#### 4.2.2 核心流程：八种状态与合法迁移

`UdtStatus` 的 8 个值定义在 [src/socket.rs:1271-1281](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1271-L1281)：

```
Init, Opened, Listening, Connecting, Connected, Broken, Closing, Closed
```

合法迁移图（箭头标注触发点）：

```
                       ┌─────────────┐
                  new  │    Init     │  ← 构造函数
                       └──────┬──────┘
                              │ open()                 [socket.rs:151, 在 connect/bind 中调用]
                       ┌──────▼──────┐
              ┌────────│   Opened    │
              │        └──────┬──────┘
   listener   │               │ connect()          [socket.rs:1116]
   bind       │        ┌──────▼──────┐
   [listener  │        │ Connecting  │
   .rs:40]    │        └──────┬──────┘
              │               │ 收到握手回包置 Connected   [socket.rs:488 或 202]
              │        ┌──────▼──────┐
   ┌──────────▼───┐    │  Connected  │◄──────── 服务端 new_connection 经
   │  Listening   │    └──┬──┬───┬──┘        connect_on_handshake 也到这 [socket.rs:202]
   └──────┬───────┘       │  │   │
          │               │  │   │ 收到 Shutdown [socket.rs:654] / close() [socket.rs:1195]
          │ 收到          │  │   │
          │ Shutdown /    │  │   ▼
          │ close()       │  │ ┌────────┐
          │               │  │ │Closing │
          │               │  │ └───┬────┘
          │   各种异常     │  │     │ GC: garbage_collect_sockets
          │  (超时/NAK/    │  │     │  [udt.rs:256]
          │   ACK 异常)    │  │     ▼
          │     [532/647/  │  │ ┌────────┐
          │      935]      │  │ │ Closed │ (终态)
          │               │  │ └────────┘
          │      ┌────────▼┐ │
          └─────►│ Broken  │◄┘ GC 对 Broken 先 spawn close() → 走到 Closing → Closed
                 └─────────┘   [udt.rs:242-246, 254-257]
```

文字版要点：

- **正常客户端路径**：`Init → Opened → Connecting → Connected`。
- **正常服务端路径**：`Init → Opened → Listening`（listener 本身）；它派生出的每条新连接则走 `connect_on_handshake` 直接落到 `Connected`（[socket.rs:202](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L202)）。
- **关闭路径**：`Connected → Closing → Closed`。`Closing` 由 `close()`（主动）或收到 `Shutdown` 包（被动，[socket.rs:654](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L654)）置位；`Closed` 由后台 GC（[udt.rs:256](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L256)）最终置位。
- **异常路径**：`Connected` / `Connecting` 可直接跳到 `Broken`（ACK 序号异常 [socket.rs:532](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L532)、NAK 异常 [socket.rs:647](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L647)、EXP 超时 [socket.rs:935](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L935)）。`Broken` 不是终态——GC 会对它 `spawn(close())`，从而经 `Closing` 到 `Closed`。

#### 4.2.3 is_alive：哪几个状态算「活着」

`is_alive()` 的定义在 [src/socket.rs:1284-1286](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1284-L1286)：

```rust
pub(crate) fn is_alive(&self) -> bool {
    *self != UdtStatus::Broken && *self != UdtStatus::Closing && *self != UdtStatus::Closed
}
```

即「活着」= `Init` / `Opened` / `Listening` / `Connecting` / `Connected` 这 5 种；`Broken` / `Closing` / `Closed` 这 3 种「已死」。注意 `Init` 也算「活着」——含义是「还没进入终态 / 关闭中」，并不代表「一定能收发」。`is_alive()` 主要用于两处：

- `next_data_packets`（[socket.rs:227](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L227)）：死了就不再发包。
- `recv` / `poll_recv`（[socket.rs:1023](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1023)、[1073](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1073)）：死了但缓冲里还有未读数据时，允许「排干」缓冲再报错，避免丢数据。

#### 4.2.4 代码实践：追踪一次客户端连接的状态变化

**实践目标**：用断点 / 日志确认 `Init → Opened → Connecting → Connected` 的真实迁移点。

**操作步骤**：

1. 打开 [src/socket.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs)，在以下 4 处临时加 `eprintln!("status -> {:?}", self.status());`：
   - `open()` 末尾（[第 152 行](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L150-L152)）→ 应看到 `Opened`；
   - `connect()` 中置 `Connecting` 之后（[第 1116 行](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1116)）→ 应看到 `Connecting`；
   - `process_ctrl` post-connect 分支置 `Connected` 之后（[第 488 行](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L488)）→ 应看到 `Connected`；
   - `close()` 末尾（[第 1195 行](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1195)）→ 应看到 `Closing`。
2. 跑一对 `udt_sender` / `udt_receiver`，发完数据后让 sender 退出（触发 `close`）。

**需要观察的现象**：日志按 `Init(构造) → Opened → Connecting → Connected → ... → Closing` 顺序出现；约 1 秒后 GC 把它变成 `Closed`（这一步发生在 udt.rs，本实践的日志看不到，需在 [udt.rs:256](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L256) 单独加日志确认）。**注意**：修改源码仅用于本地学习观察，验证后请还原，不要提交。

#### 4.2.5 小练习与答案

**练习 1**：服务端的 listener socket 自己会进入 `Connected` 状态吗？

**参考答案**：不会。listener socket 停在 `Listening`（[listener.rs:40](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/listener.rs#L40)），它不参与数据收发；每条被接受的连接是 `Udt::new_connection` 派生出的**新** socket，那个新 socket 才进入 `Connected`。

**练习 2**：`Broken` 和 `Closing` 都属于「已死」（`is_alive() == false`），但 GC 对它们的处理不同。请说出差异。

**参考答案**：对 `Broken`，GC 会 `spawn` 一个异步 `close()`（[udt.rs:242-246](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L242-L246)），让它先走完 `close()` 把状态推到 `Closing`；对已经是 `Closing` 的，GC 直接从注册表移除并置 `Closed`（[udt.rs:254-257](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L254-L257)）。所以 `Broken` 需要先「补做一次关闭」，`Closing` 已关闭完只等摘除。

### 4.3 SocketState：收发簿记容器

#### 4.3.1 概念说明

`SocketState` 是 socket.rs 里那个 `state: Mutex<SocketState>` 字段所装的对象（[src/socket.rs:81](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L81)）。它和 `SndBuffer` / `RcvBuffer` 的分工是理解本模块的关键：

- `SndBuffer` / `RcvBuffer`：**存真正的字节 / 数据包**，体量随数据量变化。
- `SocketState`：**存协议游标、loss list、定时器状态等「簿记信息」**，体量固定（与连接一一对应），随收发不断更新。

打个比方：`SndBuffer`/`RcvBuffer` 是「仓库」，`SocketState` 是仓库墙上那块「进出货台账 + 各种定时闹钟」。可靠性（u6）和拥塞控制（u7）算法的几乎所有状态变量都挂在这块台账上。

#### 4.3.2 核心流程：字段分组

`SocketState` 的字段（[src/state/socket_state.rs:9-38](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/state/socket_state.rs#L9-L38)）可分 5 组：

| 分组 | 字段 | 作用 |
|------|------|------|
| 心跳 / EXP | `last_rsp_time` | 最近一次收到对端任何响应的时刻，EXP 超时判定基准 |
| 接收簿记 | `last_sent_ack`、`last_sent_ack_time`、`curr_rcv_seq_number`、`last_ack_seq_number`、`rcv_loss_list`、`last_ack2_received` | 已确认到哪、当前收到的最大序号、接收端丢失区间、ACK2 去重 |
| 发送簿记 | `last_ack_received`、`last_data_ack_processed`、`curr_snd_seq_number`、`snd_loss_list`、`last_ack2_sent_back`、`last_ack2_time` | 对方确认到哪、已发出最大序号、发送端丢失区间、ACK2 发送节流 |
| 发送定时 / 节流 | `next_ack_time`、`interpacket_interval`、`interpacket_time_diff`、`pkt_count`、`light_ack_counter`、`exp_count`、`next_data_target_time` | ACK 定时、包间隔节拍、light ACK 计数、EXP 退避计数、下一批发送目标时刻 |
| RTT 测量 | `ack_window` | 把 ACK 序号映射到发送时刻，用于收到 ACK2 后算 RTT |

两个值得记住的设计点：

1. **接收与发送各有一套游标**：如接收侧 `curr_rcv_seq_number`（已连续收到的最大序号）、发送侧 `curr_snd_seq_number`（已发出的最大序号）。两者各自循环，互不干扰。
2. **接收与发送各有一个 `LossList`**：`rcv_loss_list` 记「我该收但没收到的区间」（用来催对端重传 / 发 NAK 的依据）；`snd_loss_list` 记「对端报告丢了、我需要重传的区间」。它们是 u6-l3 的主角，本讲只认其「存在与归属」。

初始化时（[src/state/socket_state.rs:40-72](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/state/socket_state.rs#L40-L72)）所有游标都以传入的 `isn`（初始序列号）为基准，例如 `curr_rcv_seq_number = isn - 1`、`curr_snd_seq_number = isn - 1`、`last_ack_received = isn`，含义是「还没收到 / 还没发出任何包」。`ack_window` 容量给 1024（[第 70 行](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/state/socket_state.rs#L70)），`interpacket_interval` 初始 1µs（[第 51 行](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/state/socket_state.rs#L51)）。

#### 4.3.3 源码精读

`SocketState` 结构与初始化：[src/state/socket_state.rs:9-72](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/state/socket_state.rs#L9-L72) —— 注意它的 `new` 签名是 `new(isn: SeqNumber, _configuration: &UdtConfiguration)`，第二个参数目前以下划线开头表示**暂未使用**（预留将来根据配置初始化某些字段），这是一个值得留意的「待完成」信号。

模块导出：[src/state/mod.rs:1-3](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/state/mod.rs#L1-L3) —— `SocketState` 以 `pub(crate)` 重导出，意味着它对 crate 内可见、对库用户不可见（符合 u1-l4 讲的「只露 5 个公共类型」边界）。

`SocketState` 在 `UdtSocket` 中的接入：[src/socket.rs:118](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L118) —— 构造时用 `Mutex::new(SocketState::new(...))` 包好。所有访问都经私有辅助方法 `state()`（[src/socket.rs:166-168](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L166-L168)），它返回 `std::sync::MutexGuard`，调用方拿到 guard 后改值，作用域结束自动释放——绝不跨 `.await`。

`SYN_INTERVAL` 常量对初始化的影响：[src/socket.rs:25](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L25) 定义 `SYN_INTERVAL = 10ms`，被 `SocketState::new` 用于 `next_ack_time = now + SYN_INTERVAL`（首条 ACK 的触发时刻），它也是后续 EXP / ACK2 节流的基准间隔。

#### 4.3.4 代码实践：台账与仓库的分工

**实践目标**：体会「`SocketState` 存簿记、`SndBuffer`/`RcvBuffer` 存数据」的分工。

**操作步骤**：

1. 在 [src/socket.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs) 中找到 `send` 方法（[第 984 行](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L984)）。
2. 观察它分别动了哪两类对象：
   - 数据侧：`self.snd_buffer.lock().unwrap().add_message(...)`（[第 1007-1010 行](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1007-L1010)）——把字节切片放进**仓库**；
   - 簿记侧：`self.state().last_rsp_time = Instant::now()`（[第 1004 行](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1004)）——刷新**台账**上的 EXP 基准时刻，避免刚有数据要发就误触发超时。

**需要观察的现象**：一次 `send` 同时触碰了 `snd_buffer`（仓库）和 `state`（台账），但两者各用各的 `Mutex`，互不嵌套加锁——这是避免死锁的重要约定（多个 `std::sync::Mutex` 字段从不嵌套持锁）。

> 这是源码阅读型实践，无需运行即可完成观察。

#### 4.3.5 小练习与答案

**练习 1**：`curr_snd_seq_number` 和 `last_ack_received` 都在 `SocketState` 里，它们各自代表什么？它们的差值（在循环算术语义下）有什么物理含义？

**参考答案**：`curr_snd_seq_number` 是「已发出的最大序号」，`last_ack_received` 是「对端已确认到的序号」。在 31 位循环算术下，二者之差近似等于「当前在途、尚未被确认的数据量」，这正是 `next_data_packets` 里 `curr_snd_seq_number - last_ack_received` 与拥塞窗口比较（[socket.rs:306](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L306)）的依据。

**练习 2**：为什么 `SocketState` 用一个 `Mutex` 整体保护，而不是给每个游标单独加锁？

**参考答案**：这些游标高度耦合——一次收发包往往要**原子地**更新多个游标（例如处理一个 ACK 要同时动 `last_ack_received`、`last_data_ack_processed`、`snd_loss_list`、`ack_notify` 等）。用一个锁整体保护能保证这些更新是一个原子事务，避免读到中间状态；代价是并发度低，但 socket 内部本就是单逻辑连接，争用极少，权衡合理。

## 5. 综合实践

把本讲三块内容串起来，完成下面这张「UdtSocket 全景表」与「状态迁移 + 唤醒对应」的核对任务（纯源码阅读，不修改功能）：

1. **字段全景表**：依据 [src/socket.rs:60-86](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L60-L86)，列出全部 25 个字段，按下表四列填写：

   | 字段 | 分类（7 类之一） | 同步原语 | 一句话用途 |
   |------|------------------|----------|-----------|

2. **状态迁移图**：画出 4.2.2 的迁移图，并在图上用两种颜色标注：`is_alive() == true` 的 5 个状态（`Init`/`Opened`/`Listening`/`Connecting`/`Connected`）与 `is_alive() == false` 的 3 个状态（`Broken`/`Closing`/`Closed`）。

3. **Notify 对应**：对照 4.1.4 的表格，在源码中亲自确认每个 `Notify` 的「摇铃点」与「等待点」，并回答：为什么 `close()` 末尾的 `notify_all()`（[socket.rs:1199-1203](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1199-L1203)）必须一次摇响三个铃铛，而不是只摇一个？

**预期成果**：一份完整的字段表 + 一张带颜色标注的状态图 + 对 `notify_all` 必要性的一句话解释（提示：关闭时挂起的任务可能在等连接、等数据、等 ACK 中的任意一种，必须全部唤醒让它们重新检查 `is_alive()` 后报错返回）。

## 6. 本讲小结

- `UdtSocket` 是 25 个字段的组合体，按 **身份 / 监听 / 基础设施 / 数据通路 / 拥塞 / 簿记 / 唤醒** 7 类归整后就不再凌乱。
- 锁的选择有规可循：**改值用 `std::sync::Mutex/RwLock`（短临界区、不跨 await），跨 await 协调用 `tokio::sync::RwLock`，等条件用 `tokio::sync::Notify`**。
- `UdtStatus` 有 8 态：正常客户端走 `Init → Opened → Connecting → Connected`，服务端 listener 停在 `Listening`；关闭走 `Closing → Closed`；异常跳 `Broken` 后由 GC 补做 `close` 再到 `Closed`。
- `is_alive()` 涵盖 `Init/Opened/Listening/Connecting/Connected` 5 态，用于收发守卫与「缓冲排干」判断。
- 三个 `Notify` 分工明确：`connect_notify` 唤醒等握手的客户端、`rcv_notify` 唤醒等数据的读取者、`ack_notify` 唤醒等发送缓冲腾位的写入者；`notify_all()` 在关闭时一次叫醒所有人。
- `SocketState` 是「收发簿记账本」（游标、loss list、定时器、ack_window），与存数据的 `SndBuffer`/`RcvBuffer` 分工不同；用一个 `Mutex` 整体保护以保证多游标更新的原子性。

## 7. 下一步学习建议

- 下一篇 [u3-l3](u3-l3-multiplexer.md) 讲 `UdtMultiplexer`——多个 `UdtSocket` 如何共享同一个 UDP socket 与收发 worker，本讲的 `multiplexer: RwLock<Weak<UdtMultiplexer>>` 字段在那里展开。
- 想深入 `SocketState` 里那些游标的**用法**，跳到 u6 单元（可靠性）：`curr_snd_seq_number` / `snd_loss_list` 在 [u6-l1](u6-l1-send-main-flow.md) 与 [u6-l3](u6-l3-loss-list-nak.md) 详讲；`ack_window` 在 [u6-l4](u6-l4-ack2-rtt.md)。
- 想了解 `exp_count` / `next_ack_time` / `light_ack_counter` 这些定时器字段的**判定逻辑**，看 [u7-l3](u7-l3-timers.md)（check_timers）。
- 想看 `flow` / `rate_control` 这两个拥塞子系统的内部，去 u7：[u7-l1](u7-l1-flow-estimation.md)、[u7-l2](u7-l2-rate-control.md)。

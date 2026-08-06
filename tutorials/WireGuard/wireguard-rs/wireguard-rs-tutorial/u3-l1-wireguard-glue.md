# WireGuard 胶水层与设备状态

## 1. 本讲目标

本讲是第 3 单元（WireGuard 核心与 IO 工作线程）的第一篇，目标是打开 `src/wireguard/wireguard.rs` 这个「胶水层」，看清它是怎样把两个纯引擎——握手模块 `handshake::Device` 与路由器 `router::Device`——粘合成一台可运行的 WireGuard 设备的。

学完本讲你应该能够：

- 说出 `WireguardInner` 每个字段的作用，并解释 `peers`、`router`、`queue` 三者如何分工。
- 跟着 `WireGuard::new` 走完一次构造：创建路由器、创建握手队列、按 CPU 核数启动握手工作线程池。
- 解释 `up` / `down` 对路由器与各 peer 定时器的联动影响。
- 读懂 `add_peer` 如何「同时」在握手设备和路由器里建出一个 peer，以及为何插入期间要持有 `enabled` 的读锁。
- 说清 `add_tun_reader` / `add_udp_reader` 如何挂载 IO 工作线程，以及 `WaitCounter` 如何让主线程在 `wg.wait()` 阻塞、在所有 TUN reader 退出后苏醒。

本讲把 `handshake` 与 `router` 的内部当成黑盒（它们分别属于第 4、5 单元），只关心胶水层如何调度它们。

## 2. 前置知识

在进入胶水层之前，请确认你已经理解以下概念（它们在依赖讲义中已建立）：

- **握手与数据面分离**（u1-l1）：握手模块用 Noise IK 协商出对称会话密钥，路由器用这些密钥对传输报文做 ChaCha20-Poly1305 加解密与 cryptokey 路由。
- **平台抽象 trait**（u2-l1）：协议核心写成 `WireGuard<T: Tun, B: UDP>`，TUN 与 UDP 都是泛型 trait，`T::Reader` / `T::Writer` / `B::Reader` / `B::Writer` 是关联类型，编译期绑定具体平台。
- **main.rs 装配脚本**（u1-l4）：`main()` 先绑 UAPI、建 TUN，再调用 `WireGuard::new` / `add_tun_reader` / `add_udp_reader`，最后在 `wg.wait()` 阻塞。

本讲会反复用到两个 Rust 习惯用法，先做个 30 秒回顾：

| 习惯用法 | 作用 | 本讲中的体现 |
| --- | --- | --- |
| `Arc<Inner>` + `Deref` | 多线程共享状态，外部类型只包一层 `Arc` | `WireGuard<T,B>` 包 `Arc<WireguardInner<T,B>>`，并 `Deref` 到内部 |
| `Mutex` + `Condvar` | 条件变量等待某个条件成立 | `WaitCounter` 用它实现「等所有 reader 退出」 |

还有一个关键背景：WireGuard 的并发模型是**「少量常驻工作线程 + 跨线程通道」**，而不是异步 runtime。胶水层会亲手 `thread::spawn` 三类工作线程：`tun_worker`（出站）、`udp_worker`（入站分用）、`handshake_worker`（驱动握手状态机）。本讲聚焦它们的**创建与挂载**，每个 worker 的内部循环留给 u3-l2 / u3-l3 / u4-l6。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [src/wireguard/wireguard.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs) | 胶水层主体：`WireguardInner` 字段、`WireGuard::new/up/down/add_peer/add_tun_reader/add_udp_reader/wait`、`WaitCounter` |
| [src/wireguard/mod.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/mod.rs) | 子模块声明与对外导出 `pub use wireguard::WireGuard` |
| [src/wireguard/peer.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/peer.rs) | 胶水层视角的「peer 状态」`PeerInner<T,B>`：统计、握手时间戳、定时器持有 |
| [src/wireguard/queue.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/queue.rs) | `ParallelQueue<T>`：单生产者多消费者的握手任务队列 |
| [src/wireguard/workers.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs) | 三类工作线程函数与 `HandshakeJob` 枚举（本讲只看签名与挂载点） |
| [src/wireguard/constants.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/constants.rs) | 时间轮与队列容量常量（`TIMERS_*`、`MAX_QUEUED_INCOMING_HANDSHAKES`） |

一句话定位：`wireguard.rs` 是「装配车间」，它不实现任何密码学，只负责把 `handshake::Device` 和 `router::Device` 摆到正确位置、接上正确的线程与通道。

## 4. 核心概念与源码讲解

### 4.1 WireGuard\<T,B\> 与 WireguardInner：胶水层的字段与组合关系

#### 4.1.1 概念说明

`src/wireguard/mod.rs` 顶部有一段定位说明，最能概括本讲的视角：

> The code at this level serves to "glue" the handshake state-machine and the crypto-key router code together, e.g. every WireGuard peer consists of one handshake peer and one router peer.

这句话点明了胶水层的核心职责：**一个逻辑上的 WireGuard peer = 一个握手 peer + 一个路由器 peer**。握手 peer 负责「协商密钥」，路由器 peer 负责「用密钥收发数据」。胶水层把它们成对创建、成对启停、成对销毁。

为此胶水层定义了两层类型：

- `WireguardInner<T,B>`：真正的设备状态，所有字段都在这里。
- `WireGuard<T,B>`：对外句柄，内部只有一个 `Arc<WireguardInner<T,B>>`，并实现 `Clone`/`Deref`，方便把句柄克隆到每个工作线程里。

这种「外壳 `Arc` + `Deref` 到内部」的写法，让 `wg.peers`、`wg.router` 这种字段访问写起来就像直接长在 `WireGuard` 上，但实际状态被引用计数共享。

#### 4.1.2 核心流程

胶水层把设备状态拆成「7 类字段」，分工如下：

```
WireguardInner
├── id                 ← 仅用于日志标识
├── runner             ← hjul 时间轮，驱动所有 peer 的定时器
├── enabled            ← 设备是否处于 up 状态（RwLock<bool>）
├── tun_readers        ← WaitCounter：追踪存活的 tun_worker 数量
├── mtu                ← 当前 MTU（AtomicUsize，0 表示 down）
├── peers              ← handshake::Device：按公钥 / receiver id 管理 peer
├── router             ← router::Device：加解密 + cryptokey 路由的数据面
└── last_under_load    ┐
    pending            ├← 握手相关的 DoS 状态
    queue              ┘  queue = ParallelQueue<HandshakeJob>
```

其中 `peers`（控制面/握手）和 `router`（数据面）是两大引擎，`queue` 是把入站握手报文与本地触发握手的需求派发给 `handshake_worker` 池的通道。其余字段要么是状态开关（`enabled`、`mtu`），要么是退出同步（`tun_readers`），要么是 DoS 判定（`last_under_load`、`pending`）。

#### 4.1.3 源码精读

先看外壳与内部的结构定义：

[src/wireguard/wireguard.rs:32-65](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L32-L65) — 定义 `WireguardInner<T,B>`（所有设备状态）与 `WireGuard<T,B>`（仅包一层 `Arc`）。

注意 `peers` 的类型参数特别长（所以源码标了 `#[allow(clippy::type_complexity)]`）：

```rust
pub peers: RwLock<
    handshake::Device<router::PeerHandle<B::Endpoint, PeerInner<T, B>, T::Writer, B::Writer>>,
>,
```

它表达的就是「**一个握手 peer 关联到一个路由器 peer 句柄**」：`handshake::Device` 的 opaque（附加数据）类型是 `router::PeerHandle<...>`。当我们通过握手拿到新密钥时，就能顺藤摸瓜找到对应的路由器 peer，把密钥交给它。

而 `router` 字段则独立持有路由器：

```rust
pub router: router::Device<B::Endpoint, PeerInner<T, B>, T::Writer, B::Writer>,
```

路由器的回调 opaque 类型是 `PeerInner<T,B>`——也就是胶水层在 [src/wireguard/peer.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/peer.rs) 里定义的 peer 状态（统计字节、握手时间戳、定时器）。`PeerInner` 反向持有 `wg: WireGuard<T,B>`，从而路由器回调里能反过来调用胶水层（例如触发握手）：

[src/wireguard/peer.rs:18-39](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/peer.rs#L18-L39) — `PeerInner<T,B>` 的字段：`wg` 反向引用、`pk` 公钥、收发字节统计、`timers` 等。

外壳的 `Deref` 与 `Clone` 让句柄可以被廉价地复制进每个工作线程：

[src/wireguard/wireguard.rs:75-88](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L75-L88) — `Deref` 把 `WireGuard` 透明当作 `WireguardInner`，`Clone` 仅克隆 `Arc`。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：建立「一个逻辑 peer = 握手 peer + 路由器 peer」的直觉。
2. **步骤**：
   - 打开 `src/wireguard/wireguard.rs`，定位 `peers` 字段，看清它的 opaque 类型是 `router::PeerHandle`。
   - 打开 `src/wireguard/peer.rs`，确认 `PeerInner` 里有个 `wg: WireGuard<T,B>` 字段（反向引用）。
3. **观察**：你会发现胶水层、握手、路由器三者通过泛型参数互相「指认」对方，没有任何 trait object，全部静态分发。
4. **预期结果**：能在纸上画出 `WireguardInner.peers(opaque=PeerHandle)` 与 `WireguardInner.router(opaque=PeerInner)` 的双向引用关系。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `WireGuard<T,B>` 只包一个 `Arc`，却能在不同线程里调用 `wg.peers.write()`？

> **答案**：`Arc` 让多个线程共享同一份 `WireguardInner`；`WireGuard` 实现了 `Deref` 到 `WireguardInner`，所以 `wg.peers` 拿到的是 `RwLock<...>` 的共享引用，真正的并发安全由 `RwLock`/`AtomicUsize` 等内部同步原语保证，而非 `Arc` 本身。

**练习 2**：`peers` 用的是 `RwLock`，而 `mtu` 用的是 `AtomicUsize`，为什么不让 `mtu` 也用锁？

> **答案**：`mtu` 是单个整数、在 worker 热路径上被频繁读取（`tun_worker`/`udp_worker` 每收发一个包都读一次），用原子读 `Ordering::Relaxed` 代价最小；而 `peers` 是复杂结构（含两张映射表），需要「读多写少」且遍历期间要防止结构被改，所以用 `RwLock`。

---

### 4.2 WireGuard::new 与 ParallelQueue：构造与握手工作线程池

#### 4.2.1 概念说明

`WireGuard::new` 是整台设备的「总装线」。它要完成三件事：

1. 创建握手任务队列 `ParallelQueue`，并把它的多个接收端分发给一组 `handshake_worker` 线程；
2. 创建路由器 `router::Device`（路由器内部也会自建一组 worker，本讲把它当黑盒）；
3. 把所有状态装进 `Arc<WireguardInner>`，返回外壳句柄。

这里出现一个核心数据结构 `ParallelQueue<T>`：它是「**单发送端、多接收端**」的扇出队列——一个生产者 `send`，N 个 worker 各持一个 `Receiver` 抢着 `recv`，谁空闲谁取走任务。这正好匹配握手处理的特征：握手报文可能成群到达（被 DoS 时尤其多），需要多个 CPU 核心并行消化。

#### 4.2.2 核心流程

`ParallelQueue::new(queues, capacity)` 的流程：

```
创建 1 条 crossbeam 有界通道 bounded(capacity)
  ├── 发送端 tx        → 包装成 ParallelQueue（唯一生产者入口）
  └── 接收端 rx.clone() × queues  → 返回 Vec<Receiver>，每个 worker 拿一个
```

于是 N 个 worker 共享同一组通道接收端，crossbeam 的多消费者语义保证**每个任务恰好被一个 worker 取走**。容量 `capacity=128` 起到背压作用：当握手积压超过 128 时，`send` 会阻塞生产者（`udp_worker`），自然形成反压。

`WireGuard::new` 的流程：

```
cpus = num_cpus::get()                      // 物理核数
(tx, rxs) = ParallelQueue::new(cpus, 128)   // 握手队列：cpus 个接收端
router = router::Device::new(cpus, writer)  // 路由器：内部也起 cpus 个 worker
装填 WireguardInner { enabled=false, mtu=0, queue=tx, ... }
for rx in rxs:                               // 每个 rx 对应一个 handshake_worker 线程
    spawn(handshake_worker(wg.clone(), rx))
返回 wg
```

注意两点：设备初始 `enabled=false`、`mtu=0`，即**构造完成时设备是 down 的**，要等 `up(mtu)` 才真正开始工作；handshake worker 数量等于「物理 CPU 核数」。

#### 4.2.3 源码精读

先看 `ParallelQueue` 本体——它只是对一条 crossbeam 通道的薄包装：

[src/wireguard/queue.rs:4-37](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/queue.rs#L4-L37) — `ParallelQueue`：`new` 克隆出 N 个接收端，`send` 转发，`close` 把发送端置 `None` 使后续 `send` 静默丢弃。

`new` 的关键：所有接收端 `rx.clone()` 来自同一条通道，因此多 worker 是「竞争消费」而非「各收各的」：

[src/wireguard/queue.rs:15-27](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/queue.rs#L15-L27) — `ParallelQueue::new`，每个 queue 对应一个 receiver 克隆。

再看 `WireGuard::new` 全貌：

[src/wireguard/wireguard.rs:268-302](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L268-L302) — 构造：取 CPU 核数、建握手队列、建路由器、装填状态、循环 spawn handshake_worker。

值得逐行看的两段：

```rust
let cpus = num_cpus::get();
let (tx, mut rxs) = ParallelQueue::new(cpus, 128);
```

以及最后启动 worker 的循环——它从 `rxs` 里逐个 `pop()` 接收端，每弹出一个就 spawn 一个线程：

[src/wireguard/wireguard.rs:295-299](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L295-L299) — 「start handshake workers」：每个接收端对应一个 `handshake_worker` 线程。

`runner`（时间轮）也在 `new` 里创建，参数来自常量：

[src/wireguard/wireguard.rs:290](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L290) — `Runner::new(TIMERS_TICK, TIMERS_SLOTS, TIMERS_CAPACITY)`。

这三个常量定义在：

[src/wireguard/constants.rs:41-49](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/constants.rs#L41-L49) — `TIMERS_TICK=100ms`（时间轮分辨率）、`TIMERS_SLOTS=2000`（槽数，由 200s/100ms 推出）、`TIMERS_CAPACITY=16`（初始容量）。

也就是说，定时器时间轮的最长寿命是 200 秒（`TIMER_MAX_DURATION`），正好覆盖 WireGuard 最长的定时器（`REJECT_AFTER_TIME=180s`）。关于定时器本身的细节属于 u7-l1。

#### 4.2.4 代码实践（源码阅读 + 推理型）

1. **目标**：验证「worker 数量 = CPU 核数」与队列容量。
2. **步骤**：在 `WireGuard::new` 中找到 `num_cpus::get()` 与 `ParallelQueue::new(cpus, 128)`，记录这两个数。
3. **观察**：在你的机器上 `num_cpus::get()` 返回多少？路由器 `router::Device::new(num_cpus::get(), writer)` 是否用了**同一个**核数？
4. **预期结果**：确认握手 worker 池与路由器 worker 池都用 `num_cpus::get()`，二者规模一致但互相独立（各自有自己的通道）。

#### 4.2.5 小练习与答案

**练习 1**：`ParallelQueue` 用的是 `bounded(128)`，如果改成无界通道会有什么风险？

> **答案**：失去背压。被 DoS 时握手报文可能无限堆积，内存被耗尽；有界通道则让生产者（`udp_worker`）在 128 个待处理任务时阻塞，把压力反传给收包侧，避免 OOM。

**练习 2**：为什么是 `rxs.pop()` 逐个取出接收端来 spawn，而不是 `for rx in &rxs`？

> **步骤提示**：看 `new` 里 `rxs` 的类型（`Vec<Receiver>` 的所有权）。
> **答案**：每个 `Receiver` 需要被**移动**进各自的工作线程（`handshake_worker(&wg, rx)` 按值接收 `rx`），所以必须消耗 `Vec` 的所有权逐个 `pop`； borrowing 无法把引用交给存活可能与主线程一样长的线程。

---

### 4.3 up / down / add_peer：设备启停与对等体装配

#### 4.3.1 概念说明

`up` / `down` 是设备的「总开关」，由 TUN 事件线程在收到网卡 Up/Down 时调用（见 u1-l4）。它们要协调**两个引擎**：

- 路由器：`router.up()` / `router.down()` 控制是否允许加解密与收发；
- 每个 peer 的定时器：`start_timers()` / `stop_timers()` 控制重传、保活、密钥过期等定时事件。

`add_peer` 则是「装配一个逻辑 peer」的入口：它要在握手设备和路由器里**同时**注册一个 peer，并为它创建定时器。难点在于：add_peer 可能和 up/down **并发**发生（UAPI 线程在加 peer，TUN 事件线程在切 up/down），需要一把锁来避免竞态。

#### 4.3.2 核心流程

`down()` 流程：

```
取 enabled 写锁（与 up 互斥）
若已 down：直接返回（幂等）
mtu ← 0                     // 让 worker 丢弃后续报文
router.down()               // 路由器停止发送
for peer in peers:
    peer.stop_timers()       // 停所有定时器
    peer.down()              // 路由器侧：清零密钥（zero_keys）
enabled ← false
```

`up(mtu)` 流程对称：

```
取 enabled 写锁
mtu ← mtu
若已 up：直接返回
router.up()
for peer in peers:
    peer.up()
    peer.start_timers()
enabled ← true
```

`add_peer(pk)` 流程：

```
取 peers 写锁；若 pk 已存在 → 返回 false
取 enabled 读锁（关键！阻止 up/down 同时发生）
timers = Timers::new(..., *enabled)          // 用快照决定是否立即起定时器
peer = router.new_peer(PeerInner{...})        // 在路由器建 peer，拿到 PeerHandle
peers.add(pk, peer)                           // 在握手设备注册同一个 PeerHandle
```

#### 4.3.3 源码精读

`down` 与 `up` 的注释本身就是最好的说明——down 会让设备「停止一切后续动作/定时器、阻止发送，但保留状态，并继续在两端消费并丢弃报文」：

[src/wireguard/wireguard.rs:118-175](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L118-L175) — `down` 与 `up`：先取 `enabled` 写锁、设 mtu、联动 router、遍历 peers 起/停定时器。

注意遍历 peers 时用的是 `self.peers.write()`：

```rust
for (_, peer) in self.peers.write().iter() {
    peer.stop_timers();
    peer.down();
}
```

这里取**写锁**是为了在遍历期间独占 peer 表（防止 add/remove 改表导致迭代器失效）。`peer.down()` 会清零该 peer 的密钥（路由器 `PeerHandle::down` 调用 `zero_keys`，见 [src/wireguard/router/peer.rs:414-418](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L414-L418)）；`peer.stop_timers()` 经路由器句柄的 `Deref` 链作用到该 peer 的 `Timers`（`stop_timers`/`start_timers` 定义在 [src/wireguard/timers.rs:46](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/timers.rs#L46) 与 [src/wireguard/timers.rs:71](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/timers.rs#L71)，定时器内部细节见 u7-l1）。

现在看本讲的重头戏 `add_peer`：

[src/wireguard/wireguard.rs:205-233](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L205-L233) — `add_peer`：先查重、取 `enabled` 读锁、建定时器、建路由器 peer、注册到握手设备。

关键的锁片段（**本讲综合实践的主题**）：

[src/wireguard/wireguard.rs:211-215](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L211-L215) — `// prevent up/down while inserting`：取 `enabled` 读锁并把当前 `*enabled` 快照传给 `Timers::new`。

为什么是读锁？因为 `up`/`down` 取的是 `enabled` 的**写锁**，而 RwLock 的「多读单写」语义保证：只要 `add_peer` 持有读锁，`up`/`down` 就拿不到写锁、必须等待；反之亦然。这一行同时办了两件事：

1. **互斥**：阻止 add_peer 与 up/down 并发，避免「peer 加到一半状态被翻转」的竞态。
2. **快照**：把 `*enabled`（一个 `bool`）传给 `Timers::new`，让新 peer 的定时器按「设备当前是否已 up」正确初始化——如果设备已 up，新 peer 应立即开始定时器；若 down，则不应启动。

随后两步分别建路由器 peer 和握手 peer，`PeerInner{...}` 字面量就是 u4.1 提到的胶水层 peer 状态：

[src/wireguard/wireguard.rs:217-232](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L217-L232) — `router.new_peer(PeerInner{...})` 拿到 `PeerHandle`，再 `peers.add(pk, peer)` 把**同一个句柄**存进握手设备，完成「一个逻辑 peer = 握手 peer + 路由器 peer」的配对。

> 关于「为什么插入时要取 enabled 的读锁」的完整答案，见本讲第 5 节综合实践。

#### 4.3.4 代码实践（修改 + 观察型）

1. **目标**：直观感受 up/down 与 add_peer 的互斥。
2. **步骤**：在 `down()` 第一行 `let mut enabled = self.enabled.write();` 之后加 `log::debug!("{} : down acquired enabled write lock", self);`，在 `add_peer` 的 `let enabled = self.enabled.read();` 之后加 `log::debug!("{} : add_peer acquired enabled read lock", self);`。
3. **观察**：把日志级别调到 `debug` 运行（参考 u1-l5：日志在降权后才初始化），在 `wg(8)` 连续 set/remove peer 的同时反复 `ip link set wg0 down/up`。
4. **预期结果**：日志中会出现 down 与 add_peer 交替获取锁的记录，但**绝不会**出现二者同时持有的交叉（RwLock 保证）。如果你看到了交叉，说明 RwLock 被破坏了——这是不可能的。**待本地验证**具体日志顺序。

#### 4.3.5 小练习与答案

**练习 1**：`down()` 里已经取了 `enabled` 写锁，为什么遍历 peers 时还要 `self.peers.write()`，而不是 `self.peers.read()`？

> **答案**：`enabled` 锁防止的是 up/down 并发；`peers` 锁防止的是「遍历期间 peer 表被 add/remove 改动」。两者保护不同资源。遍历本身只要 `&self` 即可，但项目选择取写锁以独占表——既保证迭代器稳定，也保证 down 期间不会被并发修改。

**练习 2**：`add_peer` 里 `peers.contains_key(&pk)` 为真时直接返回 `false`。如果不做这个查重，会发生什么？

> **答案**：`router.new_peer` 会再建一个路由器 peer，`peers.add` 会覆盖握手设备里的旧条目。结果是同一个公钥对应多个路由器 peer，密钥路由混乱、内存泄漏。查重保证了幂等。

---

### 4.4 add_tun_reader / add_udp_reader：挂载 IO 工作线程

#### 4.4.1 概念说明

胶水层的另一项核心工作是**挂载 IO**。回忆 u2-l1：`PlatformTun::create` 返回多个 `T::Reader` + 一个 `T::Writer` + 一个 `Status`；`PlatformUDP::bind` 返回多个 `B::Reader` + 一个 `B::Writer` + 一个 `Owner`。胶水层对「多个 reader」的处理方式是：**给每个 reader 各 spawn 一个工作线程**。

- `add_tun_reader(reader)`：为这个 TUN reader spawn 一个 `tun_worker`，负责「从 TUN 读明文 IP 包 → 交给路由器加密发送」（出站）。
- `add_udp_reader(reader)`：为这个 UDP reader spawn 一个 `udp_worker`，负责「从 UDP 读密文报文 → 按类型分流到握手队列或路由器」（入站）。

这种「每 reader 一线程」的设计天然支持多队列网卡（multi-queue）与 IPv4/IPv6 双栈分离——这正是 u2-l3 里 Linux 双栈 UDP 的消费方式。

#### 4.4.2 核心流程

```
add_tun_reader(reader):
    tun_readers.increase()            // 计数 +1
    spawn:
        tun_worker(wg, reader)        // 出站循环
        tun_readers.decrease()        // 线程退出时计数 -1

add_udp_reader(reader):
    spawn:
        udp_worker(wg, reader)        // 入站循环（退出即线程结束）

set_writer(writer):
    router.set_outbound_writer(writer)  // 把 UDP writer 交给路由器用于发送
```

注意不对称性：`add_tun_reader` 会 `increase` 计数，`add_udp_reader` 不会。原因在于 `wg.wait()` 等的是 `tun_readers`——**主线程以「所有 TUN worker 退出」作为退出的判据**，而 UDP worker 的生命周期跟随 socket：socket 关闭时 `udp_worker` 的 `reader.read` 报错、`return`，线程自然结束（详见 u3-l3）。

#### 4.4.3 源码精读

[src/wireguard/wireguard.rs:240-262](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L240-L262) — `add_udp_reader`、`set_writer`、`add_tun_reader`：均为 `thread::spawn` 一个 worker，tun 侧额外维护 `tun_readers` 计数。

`add_tun_reader` 的关键细节——先 `increase` 再 spawn，在线程体内最后 `decrease`：

[src/wireguard/wireguard.rs:251-262](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L251-L262) — `add_tun_reader`：`increase` → spawn →（线程内）`tun_worker` → `decrease`。

顺序很重要：必须**先 increase 再 spawn**，否则存在「spawn 了但还没 increase，主线程却开始 wait」的窗口；`decrease` 放在 `tun_worker` 返回之后，保证计数准确反映存活线程数。

worker 函数本身（本讲只看签名，内部循环见 u3-l2/u3-l3）：

[src/wireguard/workers.rs:57-146](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L57-L146) — `tun_worker`（出站）与 `udp_worker`（入站分用）的签名与循环骨架。

`set_writer` 把 UDP 的 writer 注入路由器，路由器加密完报文后就用它发出去：

[src/wireguard/wireguard.rs:247-249](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L247-L249) — `set_writer` 转发到 `router.set_outbound_writer`（路由器侧实现见 [src/wireguard/router/device.rs:253](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs#L253)）。

#### 4.4.4 代码实践（源码阅读型）

1. **目标**：理清 reader → worker 的映射关系。
2. **步骤**：回到 `src/main.rs`（u1-l4 已读过），找到 `plt::Tun::create(...)` 返回的 `readers` 是如何被 `wg.add_tun_reader(r)` 逐个挂载的；再找到 UDP 的 readers 如何被 `add_udp_reader` 挂载。
3. **观察**：数一下从 `Tun::create` 拿到几个 reader，就有几个 `tun_worker` 线程；UDP 侧同理（IPv4/IPv6 两个 reader → 两个 `udp_worker`）。
4. **预期结果**：在笔记里列出「N 个 tun_worker + M 个 udp_worker + cpus 个 handshake_worker」这张线程清单，标注各自读的是哪个 reader/队列。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `add_udp_reader` 不像 `add_tun_reader` 那样调用 `tun_readers.increase()`？

> **答案**：`wg.wait()` 以「TUN worker 全部退出」为退出判据，所以只有 tun_worker 需要被计数。UDP worker 的退出由 socket 关闭驱动（`reader.read` 报错即 `return`），不参与主线程的退出等待。

**练习 2**：如果一台机器有 4 个 TUN reader，`add_tun_reader` 被调用 4 次，`tun_readers` 计数会变成多少？主线程何时从 `wait()` 返回？

> **答案**：计数变为 4。只有当 4 个 `tun_worker` 全部退出（每个退出时 `decrease` 一次，计数归 0），`wait()` 才返回。任意一个还在运行，主线程都会继续阻塞。

---

### 4.5 WaitCounter：主线程阻塞与优雅退出

#### 4.5.1 概念说明

`WaitCounter` 是一个非常小的同步原语，但它承担了「**主线程如何优雅退出**」的全部职责。它本质是一个引用计数 + 条件变量：`increase` 让计数 +1，`decrease` 让计数 -1，`wait` 阻塞直到计数归 0。

在 wireguard-rs 里，它专门用来追踪**存活的 tun_worker 数量**。`main()` 在做完所有装配后调用 `wg.wait()`，主线程就此挂起；当所有 TUN reader 被关闭、所有 `tun_worker` 退出，计数归 0，主线程被唤醒、进程结束。

#### 4.5.2 核心流程

```
WaitCounter = (Mutex<usize>, Condvar)   // 计数 + 条件变量

increase(): *n += 1
decrease(): assert(*n>0); *n -= 1; if *n==0 { notify_all() }
wait():     while *n > 0 { condvar.wait() }   // 虚假唤醒时重新检查
```

经典条件变量使用范式：`wait` 必须在 `while` 循环里检查条件，以应对**虚假唤醒**（spurious wakeup）——即使没有人 `notify`，`wait` 也可能自行返回，所以醒来后必须重新判断 `*n > 0`。

#### 4.5.3 源码精读

`WaitCounter` 的定义就是一个元组结构体：

[src/wireguard/wireguard.rs:67-115](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L67-L115) — `WaitCounter(StdMutex<usize>, Condvar)` 及其 `wait`/`new`/`decrease`/`increase`。

`wait` 的经典写法：

[src/wireguard/wireguard.rs:92-97](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L92-L97) — `while *nread > 0 { nread = self.1.wait(nread).unwrap(); }`：持锁检查条件，`Condvar::wait` 会原子地释放锁并睡眠，被唤醒时重新拿锁。

`decrease` 在归零时 `notify_all`，唤醒 `wait`：

[src/wireguard/wireguard.rs:103-110](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L103-L110) — `decrease`：断言不会减到负数，归零时 `notify_all`。

> 小细节：源码用 `std::sync::Mutex`（别名 `StdMutex`）而非 `spin::Mutex`，因为 `Condvar` 必须配合标准库 `Mutex` 使用——这是 `spin` 与 `std` 同步原语不互通的一个现实约束。所以类型上方有 `#[allow(clippy::mutex_atomic)]`（clippy 建议「用原子替换 Mutex+bool」，但这里需要 Condvar，无法替换）。

最后是对外的 `wait` 入口：

[src/wireguard/wireguard.rs:264-266](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L264-L266) — `WireGuard::wait` 直接转发到 `tun_readers.wait()`，这就是 `main()` 里 `wg.wait()` 的落点。

#### 4.5.4 代码实践（推理型）

1. **目标**：理解 `wg.wait()` 何时返回、进程如何退出。
2. **步骤**：顺着调用链读：`main()` → `wg.wait()` → `tun_readers.wait()`。再假设「TUN 设备被关闭」会发生什么：`LinuxTunReader::read` 会报错 → `tun_worker` 里 `break` → 线程体内 `tun_readers.decrease()`。
3. **观察**：当最后一个 `tun_worker` 退出，`decrease` 把计数减到 0 并 `notify_all`，`wait` 返回，`main` 返回，进程结束。
4. **预期结果**：能复述「关闭 TUN → reader 报错 → worker 退出 → 计数归零 → 主线程苏醒 → 进程退出」这条链。**待本地验证**：实际可通过 `ip link delete wg0` 触发。

#### 4.5.5 小练习与答案

**练习 1**：`wait` 里为什么是 `while *nread > 0` 而不是 `if *nread > 0`？

> **答案**：防御虚假唤醒。`Condvar::wait` 可能在未被通知的情况下自行返回；用 `if` 会在这种情况下错误地继续往下执行（误以为计数已归零）。`while` 保证每次醒来都重新检查条件。

**练习 2**：`decrease` 里有 `assert!(*nread > 0)`。这个断言在什么情况下会触发？

> **答案**：当 `decrease` 被调用次数多于 `increase` 时——即有 worker 在未被 `add_tun_reader` 计数的情况下退出。在正确用法下不可能触发；它是一道防御性断言，捕获「配对错误」类 bug。

---

## 5. 综合实践

本讲的综合实践就是本讲义规格中指定的任务：**为 `WireGuard::add_peer` 添加一段日志，打印新增 peer 的 id 与当前 peer 总数，并解释为何插入时要取 `enabled` 的读锁。**

### 实践目标

- 把本讲四个最小模块（字段结构、装配、启停、worker 挂载）串起来：你要改的 `add_peer` 同时涉及 `peers`（4.1）、`enabled` 锁（4.3）、`PeerInner.id`（4.1）。
- 用一条日志验证「一个逻辑 peer = 握手 peer + 路由器 peer」真的被成对创建。

### 操作步骤

1. 打开 [src/wireguard/wireguard.rs:205-233](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L205-L233) 的 `add_peer`。
2. 把 `PeerInner { id: OsRng.gen(), ... }` 里的 id 提到外面，便于日志引用。
3. 在 `peers.add(...)` 成功后（或之前）打印日志。

示例代码（标注为「示例代码」，非项目原有）：

```rust
pub fn add_peer(&self, pk: PublicKey) -> bool {
    let mut peers = self.peers.write();
    if peers.contains_key(&pk) {
        return false;
    }

    // prevent up/down while inserting
    let enabled = self.enabled.read();

    let id: u64 = OsRng.gen(); // ← 提取出来，供日志使用

    let timers = Timers::new::<T, B>(self.clone(), pk, *enabled);

    let peer: router::PeerHandle<B::Endpoint, PeerInner<T, B>, T::Writer, B::Writer> =
        self.router.new_peer(PeerInner {
            id, // ← 复用同一个 id
            pk,
            wg: self.clone(),
            // ...其余字段不变
        });

    let ok = peers.add(pk, peer).is_ok();

    // ↓↓↓ 示例代码：新增的日志 ↓↓↓
    // peers.add 成功前 peers.len() 是旧值；成功后 +1 即为新的总数
    log::info!(
        "{} : add_peer {:?} (id={}) {}，当前 peer 总数 = {}",
        self,
        pk,
        id,
        if ok { "成功" } else { "失败" },
        peers.len(), // add 之后 len 已包含新 peer
    );

    ok
}
```

> 说明：`peers.len()` 来自 `handshake::Device::len()`（[src/wireguard/handshake/device.rs:70](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L70)），返回 `pk_map` 中的 peer 数量。

4. 编译：`cargo build`（注意：根据 u1-l2，日志要在降权后才初始化；若想在本实践里快速看到日志，可临时把 `main.rs` 里日志初始化提前，或在测试里用 `dummy` 平台直接调用 `add_peer` 并设置 `RUST_LOG=info`）。

### 需要观察的现象

- 用 `wg set` 添加一个 peer 后，日志应打印出该 peer 的 `id`（一个随机 u64）、公钥指纹、以及 `当前 peer 总数 = 1`。
- 再加一个不同公钥的 peer，`总数` 应变为 2。
- 用**相同公钥**再 set 一次，因 `contains_key` 命中，函数提前 `return false`，**不会**打印这条日志——验证了查重逻辑。

### 预期结果

日志输出形如（具体 id 与公钥随机）：

```
wireguard(7f3a) : add_peer PublicKey(...) (id=1234567890) 成功，当前 peer 总数 = 1
```

### 解释：为何插入时要取 enabled 的读锁

这是本实践要求说清的核心问题，对应 [src/wireguard/wireguard.rs:211-215](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L211-L215)。两个原因：

1. **互斥（防止竞态）**：`up` 和 `down` 在 [src/wireguard/wireguard.rs:129](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L129) 与 [src/wireguard/wireguard.rs:155](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L155) 取的是 `enabled` 的**写锁**。RwLock 的「多读单写」规则保证：`add_peer` 持有读锁期间，`up`/`down` 无法拿到写锁。这样就避免了「add_peer 建到一半（比如刚建好路由器 peer、还没注册到握手设备）时，被并发的 down 把设备状态翻转、把定时器停掉」这种中间态竞态。注释 `// prevent up/down while inserting` 说的就是这件事。

2. **快照（正确初始化定时器）**：读锁顺带取出了 `*enabled` 这个 `bool`，作为参数传给 `Timers::new::<T, B>(self.clone(), pk, *enabled)`。它告诉定时器「设备现在是否已 up」：若已 up，新 peer 应立即启动定时器；若 down，则不应启动。因为持有读锁期间 `enabled` 不会被改写，这个快照是一致的——不会出现「读到 true，但正要起定时器时设备已被 down」的不一致。

简言之：**读锁既是一道互斥闸（挡住 up/down），又是一次一致性快照（决定新 peer 的初始定时器状态）**。这是 RwLock 在「读多写少 + 需要快照」场景下的典型用法。

## 6. 本讲小结

- `WireguardInner` 把设备状态分成三类：**两个引擎**（`peers` 握手 + `router` 数据面）、**IO 调度**（`queue` 握手队列 + `runner` 时间轮）、**状态/同步**（`enabled`、`mtu`、`tun_readers`、`pending`、`last_under_load`）。`WireGuard<T,B>` 只是包一层 `Arc` 并 `Deref` 到内部。
- 「一个逻辑 peer = 一个握手 peer + 一个路由器 peer」：`peers` 的 opaque 类型是 `router::PeerHandle`，`router` 的 opaque 类型是 `PeerInner`，二者通过泛型互相指认。
- `WireGuard::new` 按 `num_cpus::get()` 创建握手队列与 worker 池，并创建路由器（同样按核数起 worker）；设备初始 `enabled=false`、`mtu=0`（down 态）。
- `up`/`down` 取 `enabled` 写锁、联动 `router.up/down`、并遍历 peers 起/停定时器与清零密钥；二者都是幂等的。
- `add_peer` 取 `enabled` **读锁**，既互斥 up/down，又为 `Timers::new` 提供「设备是否已 up」的一致快照。
- `add_tun_reader` / `add_udp_reader` 给每个 reader spawn 一个 worker；只有 tun_worker 会被 `tun_readers` 计数，`wg.wait()` 据此阻塞直到所有 tun_worker 退出。
- `WaitCounter = (Mutex<usize>, Condvar)`，用经典的 `while` 循环检查条件以防御虚假唤醒，是主线程优雅退出的核心。

## 7. 下一步学习建议

本讲把三类 worker（`tun_worker`/`udp_worker`/`handshake_worker`）当作黑盒，只看了它们的创建与挂载。接下来的三篇讲义会逐一打开它们：

- **u3-l2 TUN 工作线程：出站加密入口**——精读 `tun_worker` 的循环、`padding()` 对齐函数、`wg.router.send` 的入队逻辑。
- **u3-l3 UDP 工作线程：入站消息分用**——精读 `udp_worker` 如何按消息 type 字段把报文分流到握手队列或路由器，以及 `wg.pending` 与 `wg.queue.send` 的配合。
- **u4-l6 握手工作线程：驱动状态机**——精读 `handshake_worker` 如何消费 `HandshakeJob`、判定 under-load、调用 `device.process/begin` 并把新密钥交给 peer。

如果你想先横向了解「被胶水层粘合的两个引擎」，可以跳读第 4 单元（握手 Noise 协议）与第 5 单元（路由器数据面）的总览篇（u4-l1、u5-l1），但建议先按顺序完成 u3-l2、u3-l3，把 worker 的视图补全。另外，定时器细节（`runner` 时间轮、`Timers`、`Callbacks`）放在 u7-l1，本讲涉及的 `start_timers/stop_timers` 只是它的入口。

# 路由器总览与并发任务模型

## 1. 本讲目标

本讲是「路由器与数据面」单元（u5）的**总览篇**。读完本讲，你应当能够：

- 说清路由器（router）在 WireGuard 数据面中的定位：它是握手模块协商出会话密钥之后的「加解密 + cryptokey 路由」引擎，本身不含任何密码学握手逻辑。
- 画出路由器的**两层队列模型**：一层 `ParallelQueue` 负责把任务派发给线程池并行处理，一层 per-peer 的 `Queue` 负责按入队顺序串行完成副作用——即「并行加密、有序发送」。
- 读懂 `DeviceInner` 的字段含义，以及 `DeviceHandle::new / send / recv / new_peer` 四个入口各自做了什么。
- 解释 `EncryptionState` / `DecryptionState` 与 `recv` 表（receiver id → 解密状态）的对应关系。
- 说清 `worker` 函数为何在 `parallel_work()` 之后一定要调用 `queue().consume()`，以及 `Drop for DeviceHandle` 如何优雅关闭线程池。

本讲只做**架构与流程**层面的串联，把具体的 ChaCha20-Poly1305 加密细节（u5-l2）、解密与防回放细节（u5-l3）、`Queue` 内部位运算（u5-l4）、cryptokey 路由表（u5-l5）、KeyWheel 密钥轮转（u5-l6）、防回放位图（u5-l7）都留给后续讲义深入。

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（来自 u1 / u3）：

- **握手与数据面分离**：握手模块用 Noise IK 协商出对称会话密钥 `KeyPair`；路由器拿到 `KeyPair` 后，用 ChaCha20-Poly1305 对传输报文做对称加解密。本讲的 `EncryptionState` / `DecryptionState` 就是路由器存放 `KeyPair` 的地方。
- **胶水层装配**（u3-l1）：`WireGuard::new` 内部调用 `router::Device::new(num_cpus::get(), writer)` 创建路由器，并按 CPU 核数 spawn 等量的 `handshake_worker`。注意：这里的 `router::Device` 实际上是 `DeviceHandle` 的别名（见后文「源码地图」）。
- **两类工作线程**（u3-l2 / u3-l3）：`tun_worker` 从 TUN 读明文 IP 包后调用 `wg.router.send(msg)` 进入**出站**管道；`udp_worker` 从 UDP 读密文报文后调用 `wg.router.recv(src, msg)` 进入**入站**管道。本讲要回答的就是：报文进入 `send` / `recv` 之后，路由器内部到底发生了什么。
- **泛型 + 关联类型**（u2-l1）：路由器写成 `Device<E: Endpoint, C: Callbacks, T: tun::Writer, B: udp::Writer<E>>`，用编译期泛型对接平台 IO，换取数据面热路径的零成本抽象。

补充两个本讲会用到的术语：

- **nonce（计数 nonce）**：WireGuard 传输报文的 AEAD nonce 为 12 字节，前 4 字节恒为 0、后 8 字节为计数器 `counter`。每个发送密钥最多能用 `REJECT_AFTER_MESSAGES`（2⁶⁴ − 2¹⁶）个 nonce。
- **receiver id**：握手成功后，响应方为每个接收密钥分配的 32 位临时标识，写在传输报文头里，让接收方据此查到对应的解密状态。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [src/wireguard/router/device.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs) | 路由器的**核心**：`DeviceInner` 状态、`EncryptionState`/`DecryptionState`、`DeviceHandle` 及其 `new/send/recv/new_peer`、`Drop`。 |
| [src/wireguard/router/worker.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/worker.rs) | `JobUnion` 枚举与 `worker` 线程函数：从派发队列取任务，先并行后保序。 |
| [src/wireguard/router/queue.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/queue.rs) | **保序队列** `Queue<J>`：`SequentialJob`/`ParallelJob` trait，`contenders` 原子互斥的 `consume()`。 |
| [src/wireguard/queue.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/queue.rs) | **派发队列** `ParallelQueue<T>`：crossbeam 有界通道，单发送端 + N 个克隆接收端。 |
| [src/wireguard/router/types.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/types.rs) | `Callbacks` trait（回调钩子）与 `RouterError` 错误枚举。 |
| [src/wireguard/router/mod.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/mod.rs) | 模块导出：`pub use device::DeviceHandle as Device;` 等常量与重导出。 |

辅助文件（本讲引用少量片段，深入阅读见后续讲义）：

- `src/wireguard/router/peer.rs`：`PeerInner`、`KeyWheel`、`Peer::send`。
- `src/wireguard/router/send.rs` / `receive.rs`：`SendJob` / `ReceiveJob`（u5-l2 / u5-l3 精读）。
- `src/wireguard/router/constants.rs`：队列容量常量。

## 4. 核心概念与源码讲解

### 4.1 路由器的定位与核心数据模型

#### 4.1.1 概念说明

路由器是 WireGuard 的「数据面引擎」。它的输入有两种：

- **出站**：本机内核经 TUN 送来的**明文 IP 包** → 加密 → 从 UDP 发给对端。
- **入站**：从 UDP 收到的**密文传输报文** → 解密 → 鉴别归属 → 经 TUN 送回本机内核。

路由器**不做握手**，密钥由握手模块（u4）协商好之后，以 `KeyPair` 的形式注入路由器。路由器的全部状态围绕一个问题展开：「**给定一个要发/收的包，该用哪个 peer 的哪个密钥处理？**」因此它的字段可以分成三类：IO 读写器、密钥/路由查找表、任务队列。

#### 4.1.2 核心流程

路由器把设备级状态集中在 `DeviceInner`，逻辑上是这样组织的：

```
DeviceInner
├── IO 写出端
│   ├── inbound : T           // 写明文包回 TUN（内核侧）
│   └── outbound: (bool, B?)  // 写密文包到 UDP；bool=是否启用，B=UDP writer
├── 密钥 / 路由查找表
│   ├── recv   : HashMap<u32, DecryptionState>  // 入站：receiver id → 解密状态
│   └── table  : RoutingTable<Peer>             // 出站：目的 IP → peer
└── 任务派发队列
    └── work   : ParallelQueue<JobUnion>        // 喂给工作线程池
```

每个 peer 则额外持有自己的密钥状态与两条保序队列（详见 4.3）。设备级表 + peer 级密钥，共同回答了上面那个问题。

#### 4.1.3 源码精读

`DeviceInner` 的字段定义在 [src/wireguard/router/device.rs:25-39](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs#L25-L39)，每个字段都有注释说明用途：

- `inbound: T` —— 持有 TUN writer，入站解密后的明文包通过它写回内核。
- `outbound: RwLock<(bool, Option<B>)>` —— 元组第一项是「启用位」（`up`/`down` 切换），第二项是可选的 UDP writer（设备启动并绑定 UDP 后才由 `set_outbound_writer` 填入）。
- `recv: RwLock<HashMap<u32, Arc<DecryptionState<…>>>>` —— **receiver id → 解密状态**的映射表，是入站方向的核心查找结构。
- `table: RoutingTable<Peer<…>>` —— cryptokey 路由表，做最长前缀匹配（u5-l5）。
- `work: ParallelQueue<JobUnion<…>>` —— 派发给工作线程池的任务通道。

两个「密钥状态」结构体也在同一文件紧随其后定义：

[src/wireguard/router/device.rs:41-44](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs#L41-L44) 的 `EncryptionState` 只有两个字段：`keypair: Arc<KeyPair>` 与 `nonce: u64`（**下一个可用 nonce**）。出站每发一个包，nonce 自增 1；到达上限 `REJECT_AFTER_MESSAGES` 即视为密钥过期。

[src/wireguard/router/device.rs:46-51](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs#L46-L51) 的 `DecryptionState` 字段更丰富：

- `keypair: Arc<KeyPair>` —— 接收密钥；
- `confirmed: AtomicBool` —— 该密钥是否已被首个成功报文「确认」（驱动 KeyWheel 轮转，见 u5-l6）；
- `protector: Mutex<AntiReplay>` —— **per-peer 的防回放过滤器**（u5-l7），这也是为什么防回放必须串行；
- `peer: Peer<…>` —— 反向指向所属 peer，便于解密后写 TUN 与触发回调。

> 注意 `EncryptionState` 没有「confirmed」字段，而 `DecryptionState` 没有「nonce」字段——**发送方需要计数 nonce，接收方需要防回放**，两者职责不对称，字段也因此不同。

`Callbacks` trait 与 `RouterError` 定义在 [src/wireguard/router/types.rs:29-35](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/types.rs#L29-L35) 与 [src/wireguard/router/types.rs:37-44](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/types.rs#L37-L44)。`Callbacks` 是路由器向「上层」（实际是 `PeerInner`，u7-l1 实现）暴露的四个回调钩子：`send` / `recv` / `need_key` / `key_confirmed`，关联类型 `Opaque` 是绑定到每个 peer 的不透明上下文。`RouterError` 列出了路由器所有失败语义：`NoCryptoKeyRoute`（出站找不到 peer）、`MalformedTransportMessage`（入站报文头畸形）、`UnknownReceiverId`（入站找不到解密状态）、`NoEndpoint`、`SendError`。

#### 4.1.4 代码实践

**实践目标**：建立「字段 → 职责」的直觉。

1. 打开 [src/wireguard/router/device.rs:25-51](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs#L25-L51)。
2. 对 `DeviceInner`、`EncryptionState`、`DecryptionState` 的**每个字段**写一句中文注释，回答两个问题：这个字段在出站路径用、入站路径用、还是两者都用？这个字段的生命周期是多长（瞬态 / 随 peer 存在 / 随设备存在）？
3. 思考：为什么 `recv` 表放在**设备级**（`DeviceInner`），而加密状态 `enc_key` 放在 **peer 级**（`PeerInner`）？

**预期结果**：你会得出——入站要先按 receiver id 在设备级表里定位到「哪个 peer 的哪把接收密钥」，所以 `recv` 必须设备级、且按 id 而非 peer 组织；而出站已经先由 `table` 选定了 peer，加密状态自然挂在 peer 上。这是「先选 peer 再选密钥」与「先选密钥再定 peer」两条路径的不对称所致。

> 待本地验证：若你想确认字段归属，可在 `peer.rs` 中看到 `enc_key`、`keys`（KeyWheel）确实位于 `PeerInner`。

#### 4.1.5 小练习与答案

**练习 1**：`DecryptionState` 里的 `confirmed: AtomicBool` 初始值由什么决定？为什么用原子类型？

**参考答案**：初始值取自 `keypair.initiator`——见 [src/wireguard/router/peer.rs:133-142](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L133-L142) 的 `DecryptionState::new`。发起方（initiator）握手产出的是已确认密钥，故初值为 `true`；响应方产出未确认密钥，初值为 `false`，等首个传输报文到来时转正。用原子类型是因为多个入站 worker 线程会并发读改它（`swap`），而确认只需做一次。

**练习 2**：`RouterError::UnknownReceiverId` 会在哪个入口、什么条件下产生？

**参考答案**：在 `recv` 入口（4.5）查 `recv` 表时，若报文头里的 `f_receiver` 在表中找不到对应 `DecryptionState` 即返回此错误，通常意味着该 receiver id 已被回收（密钥过期或 peer 被移除）或对端用了未知 id。

---

### 4.2 两层队列模型与 worker 函数（并发任务模型）

#### 4.2.1 概念说明

这是本讲最关键的一节。路由器要同时满足两个看似矛盾的需求：

1. **高吞吐**：加解密是 CPU 密集型，必须用多线程**并行**处理。
2. **保序**：对同一个 peer，报文必须按到达/提交的顺序完成最终副作用（出站按 nonce 递增发出、入站按序写 TUN 并更新防回放窗口）。

如果只用一条队列 + 一个线程，能满足保序但浪费多核；如果只用一条 crossbeam 通道 + N 个消费者，能并行但**不保序**——先入队的包可能因调度晚于后入队的包完成。

wireguard-rs 的解法是**两层队列**：

- **派发队列 `ParallelQueue`**（[src/wireguard/queue.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/queue.rs)）：一条 crossbeam 有界通道，单发送端、N 个克隆接收端。哪个 worker 空闲，哪个就抢到下一个任务——负责**并行**。
- **保序队列 `Queue`**（[src/wireguard/router/queue.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/queue.rs)）：**每个 peer 每个方向各一条**（出站 `outbound`、入站 `inbound`）。任务创建时先 `push` 进这里占住「顺序槽位」，再克隆一份扔进 `ParallelQueue` 触发并行处理——负责**保序**。

一个任务对象（`Arc` 共享）因此同时存在于两条队列里：保序队列决定它「何时、以什么顺序」做副作用，派发队列决定它「由哪个线程、何时」做并行计算。

#### 4.2.2 核心流程

一个 worker 线程的主循环非常简单（先看骨架，源码在 4.2.3）：

```text
loop {
    job = 派发队列.recv()        // 阻塞等任务；通道关闭则退出
    job.parallel_work()          // ① 并行阶段：加密 / 解密（CPU 密集，多线程并发）
    job.queue().consume()        // ② 保序阶段：尝试推进本 peer 本方向的顺序队列
}
```

- ① `parallel_work()` 把 `ready` 标志置位（加密完成 / 解密完成）。
- ② `consume()` 检查**队首**任务是否 `ready`：是则弹出并执行 `sequential_work()`（真正的副作用：发 UDP / 写 TUN / 更新防回放），不是则停下等待。

关键设计：`consume()` **只处理队首且已就绪**的任务，绝不开小差跳过未就绪的任务——这正是保序的来源。哪怕第 5 个包先加密完，只要第 1 个包还没加密完，第 5 个包就得在队列里等着。

`consume()` 如何在多线程下既保证「同一队列同一时刻只有一个线程在跑 `sequential_work`」，又不让线程傻等？靠一个 `contenders`（竞争者计数）原子变量，采用「**接力棒**」模式：

```text
进入 consume：
  pos = contenders.fetch_add(1)
  若 pos > 0：说明已有 drainer（抽干者）在工作，我登记一下就返回，把活留给它
  若 pos == 0：我成为 drainer，进入临界区

临界区（drainer）：
  while 还有新竞争者登记过:
      while 队首任务 is_ready():
          弹出队首 → sequential_work()
      把自己计入的份额从 contenders 里减掉，
      剩余值 = 自己抽干期间新登记的竞争者数；若 >0 就再抽一轮
```

数学上，设某轮 drainer 自己计入 1、抽干期间又有 k 个线程登记，则 `contenders` 在它 `fetch_sub` 前为 `1+k`，减后为 `k`；只要 `k>0` 它就继续循环，把那 k 个线程「本想自己做」的活一并干掉。这样新来的线程几乎总是「登记后立刻返回」，由现任 drainer 顺手处理，既互斥又无忙等。

#### 4.2.3 源码精读

派发队列 `ParallelQueue` 的实现极其精简——本质就是一条 crossbeam 有界通道，把同一个接收端克隆 N 份分给 N 个 worker（[src/wireguard/queue.rs:15-37](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/queue.rs#L15-L37)）：

- `new(queues, capacity)` 创建容量为 `capacity` 的有界通道，克隆 `queues` 个接收端；
- `send(v)` 把任务投递进去（`let _ = s.send(v)` 忽略错误，即满则阻塞、关闭则丢弃）；
- `close()` 把发送端置 `None`——所有接收端随后会收到 `RecvError`，这是优雅关闭的关键（见 4.6）。

两个 trait 把任务抽象成「两阶段」：[src/wireguard/router/queue.rs:9-19](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/queue.rs#L9-L19)。`SequentialJob` 要求实现 `is_ready()` 与 `sequential_work(self)`；`ParallelJob: SequentialJob` 再加 `queue()` 与 `parallel_work()`。

`Queue` 结构与 `consume()` 在 [src/wireguard/router/queue.rs:21-27](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/queue.rs#L21-L27) 与 [src/wireguard/router/queue.rs:44-91](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/queue.rs#L44-L91)。`consume()` 的核心是 `contenders.fetch_add(1, SeqCst)`：[src/wireguard/router/queue.rs:46-50](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/queue.rs#L46-L50) 判断是否首个竞争者；[src/wireguard/router/queue.rs:62-83](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/queue.rs#L62-L83) 是只处理 `is_ready()` 队首的内层循环；[src/wireguard/router/queue.rs:89-90](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/queue.rs#L89-L90) 用 `fetch_sub` 重算剩余竞争者决定是否再来一轮。

`JobUnion` 与 `worker` 函数在 [src/wireguard/router/worker.rs:10-35](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/worker.rs#L10-L35)：

```rust
pub enum JobUnion<...> {
    Outbound(SendJob<...>),
    Inbound(ReceiveJob<...>),
}

pub fn worker<...>(receiver: Receiver<JobUnion<...>>) {
    loop {
        match receiver.recv() {
            Err(_) => break,                         // 通道关闭 → 线程退出
            Ok(JobUnion::Inbound(job)) => {
                job.parallel_work();
                job.queue().consume();
            }
            Ok(JobUnion::Outbound(job)) => {
                job.parallel_work();
                job.queue().consume();
            }
        }
    }
}
```

注意入站与出站**共用同一套 worker 线程池**、共用同一条 `ParallelQueue`——`JobUnion` 就是把两类任务塞进同一条通道的手段。两类任务的处理代码完全对称：都是「先 `parallel_work`，再 `consume`」。`consume()` 通过 `job.queue()` 各自回到自己 peer 的 `outbound` 或 `inbound` 保序队列，所以不同 peer、不同方向之间互不干扰，只在「同一 peer 同一方向」内保序。

队列容量常量在 [src/wireguard/router/constants.rs:3-9](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/constants.rs#L3-L9)：派发队列容量 `PARALLEL_QUEUE_SIZE = 4 × MAX_QUEUED_PACKETS = 4096`，保序队列容量 `INORDER_QUEUE_SIZE = MAX_QUEUED_PACKETS = 1024`。保序队列满时 `push` 直接返回 `false`（丢包，见 4.4 / 4.5）。

#### 4.2.4 代码实践

**实践目标**：亲手验证 `consume()` 的接力棒语义。

1. 阅读 [src/wireguard/router/queue.rs:44-91](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/queue.rs#L44-L91) 的 `consume()`，再阅读同文件自带的并发测试 [src/wireguard/router/queue.rs:107-169](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/queue.rs#L107-L169)（`test_consume_queue`）。
2. 用一段文字（或注释）回答：**为什么 `worker` 在 `parallel_work()` 之后必须调用 `queue().consume()`？如果删掉这一行会怎样？**
3. 给出你的推断后，对照下面的预期结果自检。

**需要观察的现象**：`parallel_work()` 只是把 `ready` 置位，并不执行任何副作用（不发包、不写 TUN）。副作用全在 `sequential_work()` 里，而 `sequential_work()` **只会被 `consume()` 调用**。

**预期结果**：若删掉 `consume()`，`ready` 会被置位但永远没人执行 `sequential_work()`——报文加密完了却永远不会被发出/写入 TUN，数据面彻底瘫痪。`consume()` 是「并行阶段完成」到「按序完成副作用」之间的**唯一桥梁**，且它顺带用接力棒机制保证了同一队列内的串行与有序。

> 待本地验证：你可以临时在测试里构造一个 `Queue`，`push` 一个 `ready=false` 的任务后调用 `consume()`，观察 `sequential_work` 不会被执行；再 `parallel_work` 后 `consume`，观察它被执行且只执行一次。

#### 4.2.5 小练习与答案

**练习 1**：两个出站报文 A（先提交）和 B（后提交）属于同一个 peer。若 B 的加密先于 A 完成，最终哪个先被发出？为什么？

**参考答案**：A 先发。因为保序队列 `outbound` 按 `push` 顺序排列（A 在 B 前），`consume()` 只在队首 `is_ready()` 时才弹出处理。B 加密完进入 `consume` 时，发现队首 A 尚未 `ready`，只能停下；等 A 也 `ready` 后，drainer 会先弹 A 执行 `sequential_work`（发出），再弹 B。所以加密可并行、发送必保序。

**练习 2**：`consume()` 里若把 `if pos > 0 { return; }` 改成「所有线程都进入临界区」会破坏什么不变量？

**参考答案**：会破坏「同一队列同一时刻只有一个线程执行 `sequential_work`」的互斥不变量。后果是：多个线程可能同时 `pop_front` 并并发执行 `sequential_work`，导致 (a) 同一 peer 的出站报文并发发送、顺序错乱；(b) 入站的 `AntiReplay::update` 与 TUN 写入并发，防回放窗口与交付顺序被破坏。

---

### 4.3 出站入口 send：从明文 IP 包到加密派发

#### 4.3.1 概念说明

`DeviceHandle::send` 是出站方向的入口，由 `tun_worker`（u3-l2）调用。它只做两件「轻量」的事：**选 peer**、**派发任务**；真正昂贵的加密在 worker 的 `parallel_work` 里异步完成。

#### 4.3.2 核心流程

```text
send(msg):                          // msg 前 16 字节是预留的传输头前缀
  packet = msg[SIZE_MESSAGE_PREFIX..]      // 跳过前缀，得到 IP 包
  peer = table.get_route(packet)           // 按目的 IP 最长前缀匹配选 peer
  if peer 不存在 → 返回 NoCryptoKeyRoute
  peer.send(msg, stage=true)               // 进入 peer 级出站逻辑
      └─ 若有可用加密密钥：
           · 创建 SendJob（绑定 nonce、keypair）
           · push 进 peer.outbound 保序队列（满则丢）
           · nonce += 1
           · work.send(JobUnion::Outbound(job))   // 扔进派发队列
      └─ 若无密钥 / 密钥过期：
           · 把 msg 暂存进 staged_packets
           · 触发 Callbacks::need_key（请求重新握手）
```

无密钥时**不丢包**而是暂存（`staged_packets`），等握手成功后由 `send_staged` 补发——这是用户数据不丢的关键（u5-l6 详述）。

#### 4.3.3 源码精读

`send` 入口在 [src/wireguard/router/device.rs:181-201](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs#L181-L201)：

```rust
pub fn send(&self, msg: Vec<u8>) -> Result<(), RouterError> {
    debug_assert!(msg.len() > SIZE_MESSAGE_PREFIX);
    let packet = &msg[SIZE_MESSAGE_PREFIX..];              // 跳过传输头前缀
    let peer = self.state.table.get_route(packet)          // 按 IP 目的地址选 peer
        .ok_or(RouterError::NoCryptoKeyRoute)?;
    peer.send(msg, true);                                  // 派发给 peer
    Ok(())
}
```

`SIZE_MESSAGE_PREFIX = size_of::<TransportHeader>() = 16` 字节，定义在 [src/wireguard/router/mod.rs:26-32](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/mod.rs#L26-L32)。这 16 字节是传输头（[src/wireguard/router/messages.rs:7-13](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/messages.rs#L7-L13)：`f_type` 4 字节 + `f_receiver` 4 字节 + `f_counter` 8 字节），由 `tun_worker` 预留、由 `SendJob::parallel_work` 就地填写。

peer 级的出站逻辑在 [src/wireguard/router/peer.rs:252-298](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L252-L298)，关键三段：

- 无密钥时暂存并请求新密钥（[peer.rs:256-263](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L256-L263)）；
- nonce 到达上限则过期密钥（[peer.rs:265-272](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L265-L272)，阈值 `REJECT_AFTER_MESSAGES - 1`）；
- 创建 `SendJob`、`push` 保序队列、`nonce += 1`、派发（[peer.rs:273-297](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L273-L297)）。

`SendJob` 本身的加密封装 `parallel_work`（ring 的 `seal_in_place_separate_tag`）与发送 `sequential_work`（`peer.send_raw` + `C::send` 回调）在 [src/wireguard/router/send.rs:59-110](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/send.rs#L59-L110) 与 [src/wireguard/router/send.rs:119-136](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/send.rs#L119-L136)，u5-l2 会逐行精读，本讲只需知道它实现了 4.2 里的两个 trait。

#### 4.3.4 代码实践

**实践目标**：理解 nonce 分配与保序队列的关系。

1. 阅读 [src/wireguard/router/peer.rs:273-298](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L273-L298)，注意 `state.nonce += 1` 是在 `self.outbound.push(job.clone())` 成功**之后**才执行的。
2. 回答：nonce 是在「派发时」还是「加密时」分配的？为什么 nonce 必须按递增顺序分配、且与保序队列的入队顺序一致？

**预期结果**：nonce 在**派发时**（`peer.send` 内）分配，而非加密时。因为发送方必须保证 nonce 单调递增，接收方的防回放窗口依赖于此；而保序队列又保证 `sequential_work`（真正发出）按入队顺序进行——入队顺序即 nonce 递增顺序，两者一致才能让对端按递增 counter 收到报文。若 nonce 在并行加密阶段才分配，多线程竞争会导致 counter 乱序。

#### 4.3.5 小练习与答案

**练习**：`send` 用 `table.get_route(packet)` 选 peer，这里查的是 IP 包的**目的地址**；而入站 `recv` 用 `check_route` 校验的是**源地址**（见 4.5 与 u5-l5）。为什么出站按目的、入站按源？

**参考答案**：出站是「我要把包送给谁」——按目的 IP 在 allowed-ips 表里查负责该子网的 peer；入站是「这个解密后的包真的是这个 peer 应该发的吗」——按源 IP 反查，确认源地址落在该 peer 声明的 allowed-ips 内，防止被冒充 peer 身份的攻击者注入伪造包。

---

### 4.4 入站入口 recv：从密文报文到解密派发

#### 4.4.1 概念说明

`DeviceHandle::recv` 是入站方向的入口，由 `udp_worker`（u3-l3）调用。它与 `send` 结构对称：**先定位解密状态**、**再派发 `ReceiveJob`**。定位的依据不是 IP 地址，而是传输报文头里的 **receiver id**。

#### 4.4.2 核心流程

```text
recv(src, msg):
  (header, _) = LayoutVerified::new_from_prefix(msg)   // 零拷贝解析传输头
  if 解析失败 → MalformedTransportMessage
  dec = recv.get(header.f_receiver)                     // 按 receiver id 查解密状态
  if 不存在 → UnknownReceiverId
  job = ReceiveJob::new(msg, dec, src)
  if dec.peer.inbound.push(job):                        // 进保序队列（满则丢）
      work.send(JobUnion::Inbound(job))                 // 扔进派发队列
```

#### 4.4.3 源码精读

`recv` 入口在 [src/wireguard/router/device.rs:211-250](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs#L211-L250)：

```rust
let (header, _) = match LayoutVerified::new_from_prefix(&msg[..]) {
    Some(v) => v,
    None => return Err(RouterError::MalformedTransportMessage),
};
// ...
let dec = self.state.recv.read();
let dec = dec.get(&header.f_receiver.get())
    .ok_or(RouterError::UnknownReceiverId)?;
let job = ReceiveJob::new(msg, dec.clone(), src);
// 1. add to sequential queue (drop if full)
// 2. then add to parallel work queue (wait if full)
if dec.peer.inbound.push(job.clone()) {
    self.state.work.send(JobUnion::Inbound(job));
}
```

注意注释点明的两步顺序：**先入保序队列（满则丢），再入派发队列（满则等）**。这个顺序很重要——若先入派发队列、再入保序队列失败，worker 拿到任务后 `job.queue()` 找不到自己，保序就会被破坏。`header.f_receiver.get()` 读出的是小端 4 字节 receiver id。

`recv` 表（`HashMap<u32, Arc<DecryptionState>>`）的填充与清理由 `add_keypair` / `PeerHandle::Drop` / `zero_keys` 维护：握手产出新接收密钥时插入（[peer.rs:470-475](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L470-L475)），peer 被移除或密钥轮转时移除旧 id。这就是「receiver id → 解密状态」表与 4.1 的 `DecryptionState` 的连接点。

`ReceiveJob` 的两阶段在 [src/wireguard/router/receive.rs:66-125](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/receive.rs#L66-L125)（`parallel_work`：AEAD 解密 + cryptokey 校验，失败则把缓冲区截断为 0）与 [src/wireguard/router/receive.rs:134-185](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/receive.rs#L134-L185)（`sequential_work`：防回放 `update`、`confirm_key`、更新端点、写 TUN、`C::recv` 回调）。注意源码注释明确指出**防回放必须在串行阶段做**——否则并发调度会让本应落在窗口内的包因乱序被判为窗口外而误丢。u5-l3 会逐行精读。

#### 4.4.4 代码实践

**实践目标**：体会「并行解密、串行防回放」的分工。

1. 阅读 [src/wireguard/router/receive.rs:55-65](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/receive.rs#L55-L65) 的注释，它解释了为何 replay protection 不能放在并行阶段。
2. 用一句话写下：解密（`open_in_place`）与 cryptokey 校验为何可以并行，而防回放 `update` 与写 TUN 为何必须串行？

**预期结果**：解密与 cryptokey 校验只依赖单个报文自身（密文、密钥、源地址），彼此无共享可变状态，天然可并行；防回放 `update` 读写共享的滑动窗口、`confirm_key` 读写 KeyWheel、写 TUN 需要按序——都涉及 per-peer 的共享可变状态，必须串行，且必须按 counter 顺序（保序队列保证）。

#### 4.4.5 小练习与答案

**练习**：`recv` 里查找 `recv` 表时只用了读锁（`self.state.recv.read()`）。如果在查找期间另一个线程正在 `add_keypair` 插入新 id，会出错吗？

**参考答案**：不会。`RwLock` 的读锁允许多个读者并发；`add_keypair` 要写锁，会等到所有读锁释放。当前报文查到的 `Arc<DecryptionState>` 即便随后被从表里移除，`Arc` 的引用计数也保证 `DecryptionState` 活到该报文处理完毕。这是用 `Arc` + `RwLock` 实现「查表后无锁使用」的常见模式。

---

### 4.5 设备生命周期：DeviceHandle 的创建、new_peer 与优雅关闭

#### 4.5.1 概念说明

`DeviceHandle` 是路由器对外暴露的「句柄」——它既持有可克隆的设备状态 `Device`（`Arc<DeviceInner>`），也持有所有 worker 线程的 `JoinHandle`。它的 `new` 负责装配状态 + 启动线程池，`Drop` 负责优雅关闭线程池。注意 [src/wireguard/router/mod.rs:34](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/mod.rs#L34) 把它重导出为 `Device`，所以胶水层写的是 `router::Device`，实质就是 `DeviceHandle`。

#### 4.5.2 核心流程

**创建**（`DeviceHandle::new`）：

```text
new(num_workers, tun):
  (work, consumers) = ParallelQueue::new(num_workers, PARALLEL_QUEUE_SIZE)
  device = Device { inner: Arc::new(DeviceInner{ work, inbound:tun, outbound:(true,None), recv:{}, table:new() }) }
  for rx in consumers:
      spawn worker(rx)            // 启动 num_workers 个 worker
  return DeviceHandle { state: device, handles: threads }
```

**优雅关闭**（`Drop for DeviceHandle`）：

```text
drop:
  work.close()                    // 关闭派发队列发送端
  for handle in handles:
      handle.thread().unpark()    // 唤醒可能 parked 的 worker
      handle.join()               // 等待 worker 退出
```

#### 4.5.3 源码精读

`DeviceHandle::new` 在 [src/wireguard/router/device.rs:106-135](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs#L106-L135)。注意初始状态：`outbound` 的启用位是 `true`（设备创建即允许发送，但 UDP writer 为 `None`，要等 `set_outbound_writer` 填入才能真正发出）；`recv` 表与 `table` 都为空。worker 数量等于传入的 `num_workers`，胶水层传的是 `num_cpus::get()`——见 [src/wireguard/wireguard.rs:276-277](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L276-L277)。

`new_peer` 在 [src/wireguard/router/device.rs:172-174](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs#L172-L174)，只是把设备状态克隆一份交给 `peer::new_peer` 去构造 `PeerInner`（含各自的 `outbound`/`inbound` 保序队列与 `KeyWheel`），返回一个 `PeerHandle`。`PeerHandle` 的 `Drop` 会把 peer 从路由表与 `recv` 表中摘除（[peer.rs:144-185](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L144-L185)），保证「peer 删除即其所有 id 可回收」。

`Drop for DeviceHandle` 在 [src/wireguard/router/device.rs:87-103](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs#L87-L103)：

```rust
fn drop(&mut self) {
    self.state.work.close();                  // ① 关闭派发队列
    while let Some(handle) = self.handles.pop() {
        handle.thread().unpark();             // ② 唤醒
        handle.join().unwrap();               // ③ 等待退出
    }
}
```

为什么这是「优雅」的？`work.close()` 把 `ParallelQueue` 内的 `Sender` 置 `None`（[queue.rs:35-37](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/queue.rs#L35-L37)）。crossbeam 通道的语义是：**发送端全部丢弃后，接收端先把已排队消息全部取完，再返回 `Err(Disconnected)`**。因此 worker 不会丢掉任何已在通道里的任务——它们会被完整地 `parallel_work` + `consume` 处理完，然后 `recv()` 才返回 `Err`，worker 函数 `break` 退出，`join()` 成功返回。`unpark()` 是给可能处于 `recv()` 阻塞中的线程的一个唤醒提示。最终所有 worker 干净退出、线程池完整关闭。

`up` / `down` / `set_outbound_writer` / `send_raw` 这些辅助方法在 [src/wireguard/router/device.rs:137-165](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs#L137-L165) 与 [src/wireguard/router/device.rs:253-255](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs#L253-L255)：`up`/`down` 切换 `outbound` 启用位（设备 down 时静默丢弃出站，不报错）；`set_outbound_writer` 注入 UDP writer；`send_raw` 是握手报文用的裸发送路径（同样受启用位约束）。

#### 4.5.4 代码实践

**实践目标**：验证「优雅关闭 = 不丢已入队任务」。

1. 阅读 [src/wireguard/queue.rs:29-37](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/queue.rs#L29-L37) 的 `send` 与 `close`，再阅读 [src/wireguard/router/device.rs:87-103](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs#L87-L103) 的 `Drop`。
2. 写一段说明回答：**`Drop for DeviceHandle` 如何优雅关闭线程池？如果它只调用 `work.close()` 而不 `join`，会有什么问题？**
3. 对照下面的预期结果自检。

**预期结果**：`close()` 让通道进入「排空后断开」状态；各 worker 把剩余任务处理完后 `recv()` 返回 `Err` 并退出循环；`join()` 等待它们全部结束，确保 `DeviceHandle` 析构完成时线程池已彻底停止、无悬挂线程。若不 `join`，worker 线程会在后台继续运行并可能访问已被释放的 `Arc<DeviceInner>`——实际上由于 `Arc` 引用计数，`DeviceInner` 不会立即释放，但线程池会脱离管理，与 `DeviceHandle` 的生命周期解耦，违背「句柄析构即停止」的语义，也妨碍有序关闭与资源回收。

#### 4.5.5 小练习与答案

**练习 1**：`DeviceHandle::new` 里 `outbound` 初始化为 `(true, None)`。设备刚创建、还没 `set_outbound_writer` 时，若有报文走 `send_raw` 发送，会发生什么？

**参考答案**：见 [src/wireguard/router/device.rs:137-145](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs#L137-L145)：`bind.0` 为 `true` 但 `bind.1` 为 `None`，`if let Some(bind) = bind.1.as_ref()` 不匹配，直接返回 `Ok(())`——静默丢弃，不报错。这对应「设备尚未绑定 UDP」的过渡态。

**练习 2**：为什么 worker 数量取 `num_cpus::get()` 而不是固定值（如 4）？

**参考答案**：因为加解密是 CPU 密集型，worker 数应匹配硬件并行度以充分利用多核；少于核数则浪费算力，远多于核数则线程频繁切换反而降低吞吐。`num_cpus::get()` 让线程池规模自适应运行环境。

---

## 5. 综合实践

把本讲的两条主线串起来，完成下面的**源码阅读型实践**（无需运行，重在理解）：

**任务**：用一段连贯的文字（约 300 字）+ 一张流程草图，完整描述一个**出站明文 IP 包**从进入 `DeviceHandle::send` 到被对端收到的全过程中，**路由器内部**发生的事，并特别解释清楚下面两个问题（即本讲规格指定的核心实践任务）：

1. **`worker` 为何在 `parallel_work()` 之后必须调用 `queue().consume()`？**
2. **`Drop for DeviceHandle` 如何优雅关闭线程池？**

**建议步骤**：

1. 从 `tun_worker` 调用 `wg.router.send(msg)` 起笔（u3-l2）。
2. 依次标注经过的函数：`DeviceHandle::send` → `table.get_route` → `peer.send` → `SendJob::new` → `outbound.push`（保序队列）→ `work.send(JobUnion::Outbound)`（派发队列）→ 某 worker `recv` 到任务 → `parallel_work`（加密、置 ready）→ `consume`（接力棒、队首就绪则 `sequential_work`：`send_raw` 发 UDP + `C::send` 回调）。
3. 在草图里**用两种颜色/标记**区分「并行阶段」与「保序串行阶段」，并标出 `ParallelQueue` 与每 peer 的 `Queue` 各自的位置。
4. 在结尾单独回答上面两个问题，引用本讲给出的行号佐证（`consume` 见 [queue.rs:44-91](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/queue.rs#L44-L91)；`Drop` 见 [device.rs:87-103](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs#L87-L103)）。

**自检要点**：

- 你的说明里是否点明了「删掉 `consume()` 则副作用永不执行」？
- 是否点明了 `close()` 利用 crossbeam 「排空后断开」语义保证不丢已入队任务、`join()` 保证线程彻底退出？
- 流程草图里出站与入站是否都共用同一个 worker 线程池（`JobUnion`）？

> 待本地验证：若你想运行验证，可参考 [src/wireguard/router/tests/mod.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/tests/mod.rs) 与 [src/wireguard/tests.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/tests.rs) 中的端到端测试，在调试器里跟踪一个包的完整路径。

## 6. 本讲小结

- 路由器是 WireGuard 的数据面引擎，只做**加解密 + cryptokey 路由**，密钥由握手模块注入；设备级状态集中在 `DeviceInner`（`inbound`/`outbound` IO、`recv` 表、`table` 路由表、`work` 派发队列）。
- 核心是**两层队列模型**：`ParallelQueue`（crossbeam 通道，单发多收）负责并行派发；per-peer per-direction 的 `Queue`（`contenders` 原子接力棒）负责按序完成副作用，实现「并行加密、有序发送」。
- `worker` 函数对 `Inbound`/`Outbound` 任务对称处理：`parallel_work()`（加密/解密、置 ready）→ `queue().consume()`（只处理队首已就绪任务，串行执行 `sequential_work`）。`consume()` 是并行阶段到有序副作用的唯一桥梁。
- 出站入口 `send`：跳过 16 字节传输头前缀 → `table.get_route` 按目的 IP 选 peer → `peer.send` 分配 nonce、入保序队列、派发；无密钥则暂存 `staged_packets` 并触发 `need_key`。
- 入站入口 `recv`：零拷贝解析传输头 → 按 `f_receiver` 查 `recv` 表得 `DecryptionState` → 创建 `ReceiveJob` → 先入保序队列、再入派发队列。
- `EncryptionState`（keypair + nonce）管发送、`DecryptionState`（keypair + confirmed + AntiReplay + peer）管接收，不对称设计对应「发送计数 nonce、接收防回放」的职责差异。
- `DeviceHandle` 持有状态与线程句柄；`Drop` 用 `work.close()` + `join()` 优雅关闭——借助 crossbeam「排空后断开」语义保证已入队任务不丢、线程干净退出。

## 7. 下一步学习建议

本讲建立了路由器的全局骨架，后续讲义会逐层拆开每个部件：

- **u5-l2 发送管道（SendJob）**：精读 `send.rs` 的 `parallel_work` 如何用 ring 构造 nonce、就地 ChaCha20-Poly1305 加密、写 `TransportHeader`。
- **u5-l3 接收管道（ReceiveJob）**：精读 `receive.rs` 的解密、cryptokey 校验、认证失败截断、防回放与 `confirm_key`。
- **u5-l4 有序队列 Queue**：深入 `contenders` 的无锁互斥位运算与保序正确性证明。
- **u5-l5 cryptokey 路由表**：treebitmap 最长前缀匹配、`get_route`/`check_route`。
- **u5-l6 KeyWheel 与 Peer 生命周期**：`next/current/previous` 三密钥轮转、`staged_packets`、`confirm_key`。
- **u5-l7 防回放窗口（RFC 6479）**：滑动位图的 `check`/`update`。

建议下一讲直接进入 **u5-l2**，因为 `SendJob` 是把本讲的「两阶段任务」抽象落地为具体加密代码的最直接样本，读完它你会对 `parallel_work` / `sequential_work` 有具象认识。

# 有序队列 Queue：并行加密、有序发送

## 1. 本讲目标

读完本讲，你应当能够：

- 说清 `SequentialJob` 与 `ParallelJob` 两个 trait 各自的职责，以及为什么要把一个报文的处理拆成「并行阶段」和「串行阶段」。
- 读懂 `Queue::push` / `Queue::consume` 的实现，并能解释 `consume` 里那行看似古怪的 `fetch_sub(contenders, …)` 到底在做什么。
- 用一个原子计数器 `contenders` 解释 wireguard-rs 如何「不依赖操作系统互斥锁」实现「同一 peer 同一时刻只有一个线程执行串行副作用，且持有者会持续排空新到达的任务」。
- 说清「队首 ready 闸门」如何保证报文严格按入队顺序完成串行处理，以及 `ready` 标志的 `Release/Acquire` 如何把并行加密的结果安全发布给串行阶段。
- 自己写出一个多生产者、多消费者并发压测，断言每个任务恰好执行一次且顺序正确。

本讲是 u5-l1（路由器总览）的深化，专注其中「per-peer 保序队列」这一颗螺丝钉。握手 / 加密 / 路由的细节已在前序讲义讲过，本讲只把它们当调用方。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**直觉一：为什么需要「并行加密、有序发送」。**
WireGuard 的数据面要做两件事：对报文做 ChaCha20-Poly1305 加解密（CPU 密集、可并行），以及把报文按序交出去（发送到 UDP / 写入 TUN，且要更新计数器、防回放位图等共享状态）。如果整个流程串行，多核 CPU 就浪费了；如果整个流程并行，那么先加密完的报文可能先被发出去，对端看到的报文序号就会乱跳，防回放窗口、TCP 流都会受影响。所以理想形态是：**加密尽量并行，但同一 peer 的「发送/落盘」这一步必须按报文进入管道的先后顺序串行执行。**

**直觉二：两个名字里都有 queue 的东西不是一回事。**
仓库里有两个完全不同的队列，初读极易混淆：

| 名称 | 定义文件 | 类型 | 作用域 | 容量 |
|---|---|---|---|---|
| `ParallelQueue<T>` | `src/wireguard/queue.rs` | 对 crossbeam 有界通道的薄封装，单发送方 / 多接收方 | **设备级**，把 `JobUnion` 任务扇出给工作线程池 | `PARALLEL_QUEUE_SIZE = 4096` |
| `Queue<J>` | `src/wireguard/router/queue.rs` | 自研的「并行做 parallel_work、串行做 sequential_work」保序队列 | **每个 peer 一个**（`inbound` + `outbound`） | `INORDER_QUEUE_SIZE = 1024` |

本讲讲的是后者 `Queue<J>`。前者只是「把任务丢给线程池」的传送带，在 u5-l1 已交代过它的角色。

**直觉三：用「抢号 + 自减」代替互斥锁。**
`Queue::consume` 没有用 `Mutex` 来守护「谁有资格执行串行工作」，而是用一个 `AtomicUsize` 计数器 `contenders`：第一个抢到 0 号的人进入临界区并把队列里所有就绪任务做完；后来的人只把计数器加一就立刻返回，等于投了一张「还有活儿要干」的票。持有者做完一轮后，根据这张票的数量决定是否再扫一轮。这是一种把「互斥」和「唤醒」合并进同一个原子变量的无锁设计。

> 名词速查：
> - **parallel_work**：可并行、无副作用（或副作用可重入）的重活，如加解密。
> - **sequential_work**：对顺序敏感、会修改共享状态的收尾，如发包、更新防回放、回调定时器。
> - **ready**：一个 `AtomicBool`，标记该任务的 parallel_work 是否已完成。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `src/wireguard/router/queue.rs` | **本讲主角**。定义 `SequentialJob` / `ParallelJob` 两个 trait，以及 `Queue<J>` 的 `push` / `consume` 与 `contenders` 互斥算法，并自带并发测试。 |
| `src/wireguard/router/worker.rs` | 工作线程函数 `worker`：收到任务后先 `parallel_work()`，再 `job.queue().consume()`——这是触发保序管道的关键一行。 |
| `src/wireguard/router/send.rs` | `SendJob`：出站任务。展示一个类型如何同时实现 `ParallelJob`（加密）和 `SequentialJob`（发包 + 回调）。 |
| `src/wireguard/router/receive.rs` | `ReceiveJob`：入站任务。同样是两个 trait 的成对实现，便于对照。 |
| `src/wireguard/router/peer.rs` | `PeerInner` 持有 `outbound: Queue<SendJob>` 与 `inbound: Queue<ReceiveJob>`；`Peer::send` 演示「先 `push` 进保序队列、再 `work.send` 进扇出队列」的标准用法。 |
| `src/wireguard/router/device.rs` | `DeviceInner.work: ParallelQueue<JobUnion>` 与 `recv`/`DeviceHandle::new`，交代两级队列如何被装配起来。 |
| `src/wireguard/router/constants.rs` | `INORDER_QUEUE_SIZE`、`PARALLEL_QUEUE_SIZE`、`MAX_QUEUED_PACKETS` 三个容量常量。 |

依赖方向：本讲承接 u5-l1，是路由器「两层队列模型」里 per-peer 这一层的精读。

## 4. 核心概念与源码讲解

### 4.1 两个 trait 的分工：ParallelJob 与 SequentialJob

#### 4.1.1 概念说明

`Queue<J>` 并不关心「任务具体是什么」，它只规定任务必须能回答两个问题：

1. 你准备好被串行处理了吗？—— `is_ready() -> bool`。
2. 请执行你的串行副作用（消耗自身）。—— `sequential_work(self)`。

这两者合起来就是 [`SequentialJob`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/queue.rs#L9-L13) trait。任何想进保序队列的类型都得实现它。

而 [`ParallelJob`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/queue.rs#L15-L19) 在它之上多加了两点：任务能回传「我属于哪个 `Queue`」（`queue(&self) -> &Queue<Self>`），以及任务能做「可并行的重活」（`parallel_work(&self)`）。注意 `ParallelJob: Sized + SequentialJob`——必须先是个 `SequentialJob` 才能升级成 `ParallelJob`。

为什么 `parallel_work` 取 `&self` 而 `sequential_work` 取 `self`？因为并行阶段允许多线程同时摸同一个任务（任务内部用 `Arc<Inner>` + 锁保护），而串行阶段「消费」任务、把它从队列里彻底取走，所以按值拿走所有权（`pop_front` 后调用一次即销毁），从类型上保证「恰好执行一次」。

#### 4.1.2 核心流程

一个报文被处理的生命周期被切成两段，由 worker 串起来：

```
peer.send(msg)
  ├── outbound.push(job.clone())        # ① 进 per-peer 保序队列（排队等串行）
  └── device.work.send(JobUnion(job))   # ② 进设备级扇出队列（等并行）

pool worker 收到 JobUnion::Outbound(job):
  ├── job.parallel_work()               # ③ 加密（多线程并发，无副作用收尾）
  └── job.queue().consume()             # ④ 触发该 peer 保序队列的串行排空
        └── 队首就绪？ → pop_front → job.sequential_work()  # ⑤ 按序发包+回调
```

③ 可以乱序完成（哪个 worker 先拿到就先做），但 ⑤ 一定按 ① 的入队顺序发生——这正是本队列要保证的不变量。

#### 4.1.3 源码精读

两个 trait 的定义非常短，先贴全：

[SequentialJob 与 ParallelJob trait 定义：src/wireguard/router/queue.rs:9-19](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/queue.rs#L9-L19) —— `SequentialJob` 只问「是否就绪 / 怎么收尾」，`ParallelJob` 在其之上追加「属于哪个队列 / 怎么做并行重活」。

再看一个真实任务如何成对实现这两段。出站任务 [`SendJob`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/send.rs#L52-L110) 的 `parallel_work` 做 ChaCha20-Poly1305 就地加密，末尾把 `ready` 置位：

```rust
// send.rs:59-109（节选）
fn parallel_work(&self) {
    // …就地加密 msg、写入 TransportHeader、追加 AEAD 标签…
    self.0.ready.store(true, Ordering::Release);   // 发布：加密完成
}
```

而它的 [`sequential_work`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/send.rs#L119-L136) 负责把密文发出去并触发定时器回调：

```rust
// send.rs:119-135（节选）
fn sequential_work(self) {
    let msg = job.buffer.lock();
    let xmit = job.peer.send_raw(&msg[..]).is_ok();          // 发包
    C::send(&job.peer.opaque, msg.len(), xmit, &job.keypair, job.counter); // 回调
}
```

入站任务 [`ReceiveJob`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/receive.rs#L48-L125) 结构对称：`parallel_work` 解密 + cryptokey 路由校验，`sequential_work` 做防回放、密钥确认、写 TUN、回调。两段职责的切分原则在 u5-l3 已讲过（无副作用放并行、顺序敏感放串行），本讲只需记住：**队列不关心这两段具体做什么，它只调度「何时调用 sequential_work」。**

#### 4.1.4 代码实践

**实践目标**：亲手确认「并行 / 串行」两段在每个真实任务里都成对出现，并看清谁触发谁。

**操作步骤**：

1. 打开 `src/wireguard/router/send.rs`，定位 `impl ParallelJob for SendJob` 与 `impl SequentialJob for SendJob` 两个块。
2. 打开 `src/wireguard/router/worker.rs`，确认 `worker` 对 `Outbound` 和 `Inbound` 两个变体都是「先 `parallel_work()` 再 `queue().consume()`」。
3. 在 `SendJob::parallel_work` 末尾、`ready.store(...)` 之前临时加一行 `log::trace!("send job {} encrypted", job.counter);`，在 `sequential_work` 开头加 `log::trace!("send job {} sending", job.counter);`。

**需要观察的现象**：以 `RUST_LOG=trace` 运行端到端测试时，同一个 counter 的 `encrypted` 与 `sending` 之间可能插入其它 counter 的 `encrypted`（说明加密乱序），但 `sending` 行的 counter 序列对同一个 peer 是单调递增的（说明发送有序）。

**预期结果**：加密日志可交错，发送日志保序。运行结果**待本地验证**（本讲未实际执行）。

#### 4.1.5 小练习与答案

**Q1**：`parallel_work(&self)` 为什么不取 `&mut self`？  
**A**：任务通过 `Arc<Inner>` 共享，多个 worker 线程可能同时持有同一任务的克隆；可变状态（缓冲区、`ready`）都封装在 `Inner` 里用锁 / 原子量保护，因此对外只需 `&self`。

**Q2**：如果某个任务只实现了 `SequentialJob`、没实现 `ParallelJob`，它还能被本系统调度吗？  
**A**：不能进 worker 线程池——`worker` 收到的是 `JobUnion<SendJob/ReceiveJob>`，二者都实现了 `ParallelJob`，`worker` 正是靠 `job.parallel_work()` + `job.queue().consume()` 驱动整个管道。`ParallelJob` 的存在是「能被扇出并行处理」的前提。

---

### 4.2 Queue 的结构、push/consume 与两级队列模型

#### 4.2.1 概念说明

[`Queue<J>`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/queue.rs#L21-L27) 是一个定容的、per-peer 的保序队列，只有两个字段：

- `contenders: AtomicUsize` —— 互斥 + 唤醒合一的原子计数器（4.3 详解）。
- `queue: Mutex<ArrayDeque<[J; INORDER_QUEUE_SIZE]>>` —— 一个固定容量的环形缓冲，存放等待串行处理的任务。

[`INORDER_QUEUE_SIZE`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/constants.rs#L3-L9) 由 `MAX_QUEUED_PACKETS = 1024` 派生。用 `ArrayDeque<[J; N]>` 这种定长数组-backed 的环形队列，好处是 push 时不做堆分配、内存占用可预测；代价是满了就拒绝。`push` 在满时返回 `false`，调用方据此实现背压（见 4.2.3）。

它只有两个公开方法：[`push`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/queue.rs#L40-L42) 把任务塞到队尾，[`consume`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/queue.rs#L44-L91) 尝试排空队首所有就绪任务。

#### 4.2.2 核心流程

把 4.1.2 的图补全成「两级队列」的全貌：

```
                 ┌──────────── 设备级 ParallelQueue（扇出，容量 4096）────────────┐
   peer.send ──▶ │ Outbound(jobA) │ Inbound(jobB) │ …  ──▶ 被 N 个 pool worker 抢
                 └─────────────────────────────────────────────────────────────┘
                                            │ 每个 worker：
                                            │   job.parallel_work()      # 并行加密/解密
                                            │   job.queue().consume() ────┐
                                            ▼                            ▼
   peer A 的 outbound Queue<SendJob>（容量 1024）   peer A 的 inbound Queue<ReceiveJob>
   [ job1 | job2 | job3 ]  ◀─ push 入队          ┌──────────────────────────┐
                          consume 按 ready 闸门   │ 只有「持锁者」一个线程按  │
                          逐个 pop_front 收尾      │ 入队顺序 sequential_work │
                                                  └──────────────────────────┘
```

关键点：**任务被克隆成两份**。一份进 per-peer 的 `Queue`（用来排队 + 保序 + 执行 `sequential_work`），一份进设备级 `ParallelQueue`（被某个 worker 拿去做 `parallel_work` 并触发 `consume`）。两份共享同一个 `Arc<Inner>`，所以并行阶段写完的加密结果，串行阶段能看到——可见性由 4.4 讲的 `Release/Acquire` 保证。

#### 4.2.3 源码精读

`push` 极简——拿锁、塞队尾、返回是否成功：

```rust
// queue.rs:40-42
pub fn push(&self, job: J) -> bool {
    self.queue.lock().push_back(job).is_ok()
}
```

调用方如何处理「满」？看出站 [`Peer::send`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L273-L297)：

```rust
// peer.rs:277-297（节选）
let job = SendJob::new(msg, state.nonce, state.keypair.clone(), self.clone());
if self.outbound.push(job.clone()) {   // ① 进保序队列
    state.nonce += 1;
    (Some(job), false)
} else {
    (None, false)                       // 队列满：丢弃，nonce 不前进
}
// …
if let Some(job) = job {
    self.device.work.send(JobUnion::Outbound(job))  // ② 进扇出队列
}
```

注意三件事：① 先 `push` 进保序队列、再 `work.send` 进扇出队列，顺序不能反（否则 worker 可能在任务还没入保序队列时就 `consume` 空扫）；② `push` 失败时 nonce 不自增，所以「没发出去的序号」不会被浪费，只是这个包被丢掉（过载丢包，背压）；③ 任务被 `clone()` 成两份分发。

入站方向 [`Device::recv`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs#L244-L248) 完全对称：`inbound.push(job.clone())` 成功后才 `work.send`。

per-peer 的两个队列在 [`PeerInner`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L41-L42) 里声明：

```rust
// peer.rs:41-42
pub(super) outbound: Queue<SendJob<E, C, T, B>>,
pub(super) inbound:  Queue<ReceiveJob<E, C, T, B>>,
```

设备的扇出队列在 [`DeviceInner`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs#L38) 里：

```rust
// device.rs:38
pub(super) work: ParallelQueue<JobUnion<E, C, T, B>>,
```

#### 4.2.4 代码实践

**实践目标**：用一张纸把「两级队列 + 任务克隆」走通，确认顺序敏感的副作用只发生在 per-peer 队列里。

**操作步骤**：

1. 在 `peer.rs:277`（`outbound.push`）处确认「先入保序队列」。
2. 在 `device.rs:246`（`inbound.push`）处确认入站同样是「先入保序队列、后入扇出队列」。
3. 阅读源码回答：如果调换 `peer.rs` 中 `push` 与 `work.send` 的顺序，会出现什么问题？

**需要观察的现象 / 预期结果**：若先 `work.send` 后 `push`，worker 可能在 `push` 完成前就调用 `consume()`；此时该任务还不在保序队列里，`consume` 扫不到它，串行副作用被推迟到下一次任意 `consume`——既破坏「及时性」，也可能在极端时序下让队首被一个尚未 push 的逻辑位置卡住。这就是「先 push、再扇出」顺序不可反的根因。结论性推理，**待本地验证**。

#### 4.2.5 小练习与答案

**Q1**：`INORDER_QUEUE_SIZE` 是多少？为什么用定长 `ArrayDeque` 而非 `VecDeque`？  
**A**：`1024`（= `MAX_QUEUED_PACKETS`）。定长数组-backed 环形队列在 push 时不做堆分配、容量可预测，适合数据面热路径；满了直接返回 `Err` 让调用方背压丢包，避免无界堆积导致内存爆炸。

**Q2**：`push` 返回 `false` 时，出站路径对这个包做了什么？  
**A**：直接丢弃（不暂存、不重试），且 `state.nonce` 不自增。这是一种过载保护：队列满说明对端或本端处理不过来，丢包比堆积更安全。

---

### 4.3 contenders：用一个原子计数同时实现互斥与唤醒

#### 4.3.1 概念说明

[`consume`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/queue.rs#L44-L91) 是本讲最难的一段。它要解决一个并发难题：

- **同一时刻只能有一个线程在执行某 peer 的 `sequential_work`**（否则共享状态会乱）。
- 但又**不能让其它线程阻塞睡眠**——因为调用 `consume` 的就是干完 `parallel_work` 的 worker，让它们睡眠会浪费线程、增加唤醒延迟。
- 而且**只要有新任务到达，持有者就该继续干**，不能干完一轮就走人、留下新任务没人管。

`contenders: AtomicUsize` 一根变量同时承担三个角色：**锁**（谁抢到 0 谁进）、**票箱**（后来者投「还有活」的票）、**重入计数**（持有者据此决定要不要再扫一轮）。

#### 4.3.2 核心流程

`consume` 的伪代码（与源码一一对应）：

```
consume():
    pos = contenders.fetch_add(1, SeqCst)      # ① 抢号（拿旧值，计数+1）
    if pos > 0:                                 # ② 已有持锁者 → 投票后直接返回
        return
    # pos == 0：我赢得了临界区
    c = 1                                       # 我自己算 1 票
    while c > 0:
        while 队列非空 且 队首.is_ready():        # ③ 只 pop「队首且就绪」者
            job = pop_front()
            job.sequential_work()               #    按序收尾（此时已释放队列锁）
        # 本轮已扫空所有当前就绪的队首
        old = contenders.fetch_sub(c, SeqCst)   # ④ 归还 c 个名额，拿回旧值
        c = old - c                             # ⑤ 剩余 = 本轮期间新到的票数
    # c == 0：本轮处理期间无人新到，退出
```

要点：

- ① `fetch_add` 是「读旧值并自增」的原子操作，`pos` 是自增**前**的值。
- ② `pos > 0` 说明在我之前已经有人把计数从 0 顶到了 ≥1，即已有持锁者；我只留下 +1 当作「还有任务」的选票，立刻返回，绝不睡眠。
- ④⑤ 是精髓。设本轮开始时我持有的票数为 `c`，`fetch_sub(c)` 把原子从旧值 `old` 减到 `old − c` 并返回 `old`；于是新票数 `c' = old − c`。`old` 之所以可能大于 `c`，正是因为 ② 中那些「投票后返回」的线程把计数抬高了。`c'` 就是「我干活期间又来了几个请求」。
  - 若 `c' == 0`：没人来，退出。
  - 若 `c' > 0`：有新任务可能就绪了，回到 while 顶部再扫一轮（临界区重入，但我仍是唯一持锁者）。

可以证明：**任一时刻最多有一个线程处于 `pos == 0` 之后的临界区**（因为只有第一个把计数从 0 顶到 1 的人 `pos` 才是 0，其后所有人的 `pos ≥ 1`）。这就是无锁互斥。

用数学表达持有者退出条件：记第 k 轮结束时原子计数旧值为 \( old_k \)、本轮持有票数为 \( c_k \)，则

\[
c_{k+1} = old_k - c_k,\quad \text{当且仅当 } c_{k+1}=0 \text{ 时退出。}
\]

由于每一轮新到的请求都体现为 \( old_k > c_k \)，该式把「是否有新工作量」直接编码进了「是否继续循环」。

#### 4.3.3 源码精读

[consume 全貌：src/wireguard/router/queue.rs:44-91](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/queue.rs#L44-L91)。逐段对应：

```rust
// queue.rs:46-50 —— ① 抢号；② 已有持锁者则登记后返回
let pos = self.contenders.fetch_add(1, Ordering::SeqCst);
if pos > 0 {
    assert!(usize::max_value() > pos, "contenders overflow");
    return;
}
```

```rust
// queue.rs:53-90 —— 临界区：持续扫空就绪队首，再据票数决定重入
let mut contenders = 1;                       // myself
while contenders > 0 {
    // …（debug 断言互斥）…
    loop {
        let mut queue = self.queue.lock();
        match queue.front() {                 // 只看队首
            None => break,
            Some(job) => if !job.is_ready() { break; }   // 队首未就绪 → 停
        };
        let job = queue.pop_front().unwrap(); // 取走队首
        debug_assert!(job.is_ready());
        mem::drop(queue);                     // ★ 先放手队列锁
        job.sequential_work();                //   再做串行副作用
    }
    // …（debug 释放标志）…
    contenders = self.contenders.fetch_sub(contenders, Ordering::SeqCst) - contenders;
}
```

三处值得划重点：

1. **队列锁在 `sequential_work` 之前就被 `mem::drop` 释放**（第 79 行）。这意味着「执行串行副作用」时不持有 `queue` 的 `Mutex`，生产者可以继续 `push`，也不会因 `sequential_work` 内部若再取同一把锁而自死锁。
2. **`fetch_sub` 的操作数是局部变量 `contenders`，不是常量 1**（第 89 行）。这是重入的关键：它一次性归还「我这一轮持有的全部票数」，而不是只减 1。若写成 `fetch_sub(1)`，多出来的票会永远留在计数器里，导致计数无法归零、系统卡死。
3. **`#[cfg(debug)] _flag`（第 25-26、56-60、85-86 行）** 是一把只在 debug 构建中存在的 `spin::Mutex`，用 `try_lock().expect(...)` 断言「真的只有一个线程进来了」。如果 `contenders` 机制失效、出现两个持锁者，第二次 `try_lock` 会失败并 panic，把并发 bug 暴露在测试期。

#### 4.3.4 代码实践

**实践目标**：用日志直观看到「多数 `consume` 调用都是 `pos > 0` 提前返回，只有少数真正进入临界区」。

**操作步骤**：

1. 在 `queue.rs` 顶部确认 `contenders` 是 `AtomicUsize`（第 22 行）。
2. 在第 46 行后插入临时日志：
   ```rust
   log::trace!("consume: pos = {}, thread = {:?}", pos, std::thread::current().id());
   ```
3. 在第 53 行（进入临界区）插入 `log::trace!("consume: ENTER critical section");`。

**需要观察的现象**：高并发下，`pos = 0` 的日志远少于 `pos > 0` 的日志；`ENTER` 总是紧跟某个 `pos = 0`，且两次 `ENTER` 之间不会交叉（同 peer 的串行段不重叠）。

**预期结果**：`pos > 0` 占多数（投票即返回），`pos == 0` 是少数「苦力」。运行结果**待本地验证**。

#### 4.3.5 小练习与答案

**Q1**：把第 89 行的 `fetch_sub(contenders, …)` 改成 `fetch_sub(1, …)` 会怎样？  
**A**：会卡死。假设持有者第一轮 `c = 1`，期间又来了 2 个投票（计数被抬到 3）。正确实现 `fetch_sub(1)` 返回 3、新 `c = 2`，继续扫两轮；但若只减 1，计数从 3 变 2、`c` 还是按返回值算成 `3 − 1 = 2`——这一步恰好对。问题出在多轮累积：当有多个持票人时，单次 `fetch_sub(1)` 无法把「我代持的全部票」一次清掉，计数永远卡在 ≥1，新的 `consume` 永远 `pos > 0` 提前返回，没人再进临界区 → 死锁。所以必须用局部 `contenders` 一次归还。

**Q2**：为什么 `consume` 用 `SeqCst` 而不像 `ready` 那样用 `Release/Acquire`？  
**A**：`contenders` 既是锁又是唤醒信号，需要所有线程对「谁先到 0」「计数当前值」达成全局一致的总顺序；`SeqCst` 提供最强保证、最不易出错。这里是控制平面的一次性同步，不是数据面热路径，性能代价可接受。

---

### 4.4 保序原理：队首 ready 闸门与 Release/Acquire 可见性

#### 4.4.1 概念说明

有了互斥还不够，还要保证 **`sequential_work` 的执行顺序严格等于 `push` 的入队顺序**。`Queue` 用一个极简规则实现：**每次只看队首（`front`），队首就绪才 pop，否则立刻停**。这意味着即便队首后面的任务早就 `parallel_work` 完了，也得等队首就绪并被处理后才能轮到它——后到的就绪任务永远无法「插队」。

但「队首就绪」还牵涉一个内存可见性问题：`parallel_work` 在 worker 线程 A 里写完了加密数据并置 `ready=true`，而 `consume` 在线程 B 里读 `ready`；B 看到 `true` 时，必须也能看到 A 写的那些密文字节，否则 `sequential_work` 会读到半成品。这个「发布」语义由 `ready: AtomicBool` 的 `Release/Acquire` 配对保证。

#### 4.4.2 核心流程

保序与可见性合在一起：

```
worker A:  parallel_work 写密文 ──▶ ready.store(true, Release)
                                         │ Acquire 建立 happens-before
worker B:  consume ──▶ front.is_ready() ──┴─▶ 看到 true ⇒ 也看到密文 ⇒ pop_front ⇒ sequential_work
```

可见性公式：若线程 A 的 `store(true, Release)` 与线程 B 的 `load(Acquire)` 构成同步关系，则 A 在 store 之前的所有写操作（加密结果）对 B 在 load 之后的所有读操作可见。即

\[
A \xrightarrow{\text{hb}} \text{store}(\text{Release}) \xrightarrow{\text{syncs-with}} \text{load}(\text{Acquire}) \xrightarrow{\text{hb}} B
\]

#### 4.4.3 源码精读

队首闸门在 [`consume`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/queue.rs#L63-L83) 的内层循环：

```rust
// queue.rs:63-83（节选）
loop {
    let mut queue = self.queue.lock();
    match queue.front() {                       // 只看队首
        None => break,
        Some(job) => {
            if !job.is_ready() {                // 队首没就绪 → 立刻停，绝不跳过
                break;
            }
        }
    };
    let job = queue.pop_front().unwrap();
    // … job.sequential_work(); …
}
```

`ready` 的发布 / 订阅配对散落在 SendJob / ReceiveJob 里。出站侧：[`parallel_work` 以 Release 发布](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/send.rs#L107-L109)，[`is_ready` 以 Acquire 订阅](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/send.rs#L115-L117)：

```rust
// send.rs:108 —— 并行阶段写完
self.0.ready.store(true, Ordering::Release);
// send.rs:116 —— 串行阶段判读
fn is_ready(&self) -> bool { self.0.ready.load(Ordering::Acquire) }
```

入站 [`ReceiveJob`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/receive.rs#L122-L131) 完全相同的 `Release`/`Acquire` 配对。最后，worker 的触发链条在 [`worker.rs`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/worker.rs#L25-L32)：

```rust
// worker.rs:25-32
Ok(JobUnion::Inbound(job)) => {
    job.parallel_work();        # 并行阶段（store Release）
    job.queue().consume();      # 触发保序排空（load Acquire + 串行收尾）
}
```

把三件事串起来就是「并行加密、有序发送」：parallel_work 可乱序完成并各自 Release 发布；consume 用队首闸门强制按入队顺序 Acquire 读取并 sequential_work。

#### 4.4.4 代码实践

**实践目标**：验证「队首未就绪会卡住其后所有就绪任务」这一保序特性。

**操作步骤**：

1. 阅读 `queue.rs` 第 67-74 行，确认判定只针对 `front()`，没有任何「跳过未就绪、处理后面就绪」的分支。
2. 设计一个心智实验（不必真跑）：构造 3 个任务 push 进队列，使 1 号任务的 `is_ready()` 暂时返回 `false`、2/3 号返回 `true`；然后调用 `consume()`。

**需要观察的现象 / 预期结果**：`consume` 在看到 1 号未就绪时立即 `break`，2、3 号即使就绪也不会被处理，直到 1 号变就绪并被消费后，2、3 才依次按序处理。这正是「按入队顺序、不插队」的语义，也是数据面报文序号不会乱跳的根本保障。属推理结论，**待本地验证**。

#### 4.4.5 小练习与答案

**Q1**：如果队首任务永远不变 `ready`（比如它的 parallel_work 因异常没走到置位），会发生什么？  
**A**：其后的所有任务（即便已就绪）都被「卡」在队首闸门后无法 sequential_work，形成对该 peer 的线头阻塞（head-of-line blocking）。因此 parallel_work 必须保证无论成功失败都把 `ready` 置位——这正是 SendJob/ReceiveJob 在 `parallel_work` 末尾无条件 `store(true, Release)` 的原因（失败时它们把缓冲区截断为 0，让串行阶段解析失败而安全跳过，但 `ready` 一定为 true）。

**Q2**：把 `ready.store(true, Release)` 改成 `Relaxed` 会出什么问题？  
**A**：互斥与保序本身不坏（它们由 `contenders` 与队首闸门保证），但 `consume` 线程可能看到 `ready == true` 却读到尚未对它可见的密文缓冲——`sequential_work` 会发出/写入半成品数据。`Release/Acquire` 是「发布加密结果」所必需的可见性保证。

---

## 5. 综合实践

**任务**：参照 `queue.rs` 自带的 [`test_consume_queue`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/queue.rs#L106-L169)，编写一个多生产者 / 多消费者并发压测，断言两件事：① 每个任务被**恰好执行一次**（无丢失、无重复）；② 每个生产者推入的任务**按入队顺序完成**（FIFO）。

把下面的测试追加进 `src/wireguard/router/queue.rs` 的 `mod tests`（已包含必要的额外 `use`）。这是**示例代码**（待本地验证）：

```rust
// 示例代码：多生产者 / 多消费者保序压测
use std::sync::Mutex;
use std::collections::HashMap;

#[test]
fn test_ordered_consume_multi_producer_multi_consumer() {
    // 每个 job 带一个 id；sequential_work 把 id 追加到共享日志
    struct TestJob {
        id: usize,
        log: Arc<Mutex<Vec<usize>>>,
    }
    impl SequentialJob for TestJob {
        fn is_ready(&self) -> bool { true }
        fn sequential_work(self) {            // 取 self：类型上保证只调一次
            self.log.lock().unwrap().push(self.id);
        }
    }

    let queue = Arc::new(Queue::<TestJob>::new());
    let log = Arc::new(Mutex::new(Vec::new()));

    const PRODUCERS: usize = 4;
    const PER_PRODUCER: usize = 500; // 总计 2000 > INORDER_QUEUE_SIZE(1024)，触发背压

    // 生产者：每个线程独占一段连续 id，按升序 push
    let mut producers = Vec::new();
    for p in 0..PRODUCERS {
        let (queue, log) = (queue.clone(), log.clone());
        producers.push(thread::spawn(move || {
            let base = p * PER_PRODUCER;
            for i in 0..PER_PRODUCER {
                let id = base + i;
                // 队列满时先 consume 腾位再重试（背压）
                while !queue.push(TestJob { id, log: log.clone() }) {
                    queue.consume();
                }
            }
        }));
    }

    // 消费者：并发反复 consume（多数会 pos>0 投票返回，少数进入临界区）
    let mut consumers = Vec::new();
    for _ in 0..4 {
        let queue = queue.clone();
        consumers.push(thread::spawn(move || {
            for _ in 0..100_000 {
                queue.consume();
            }
        }));
    }

    for h in producers { h.join().unwrap(); }
    for h in consumers { h.join().unwrap(); }
    queue.consume(); // 收尾：所有线程退出后 contenders 必为 0，这一次排空兜底

    let executed = log.lock().unwrap();

    // ① 恰好一次：每个 id 计数恰为 1
    let mut counts: HashMap<usize, usize> = HashMap::new();
    for &id in executed.iter() { *counts.entry(id).or_insert(0) += 1; }
    for p in 0..PRODUCERS {
        for i in 0..PER_PRODUCER {
            let id = p * PER_PRODUCER + i;
            assert_eq!(counts.get(&id), Some(&1), "job {id} 未被恰好执行一次");
        }
    }

    // ② 保序：每个生产者的 id 在日志中保持升序（FIFO）
    for p in 0..PRODUCERS {
        let base = p * PER_PRODUCER;
        let seq: Vec<usize> = executed.iter()
            .copied()
            .filter(|&id| (base..base + PER_PRODUCER).contains(&id))
            .collect();
        let expected: Vec<usize> = (base..base + PER_PRODUCER).collect();
        assert_eq!(seq, expected, "生产者 {p} 的任务未按入队顺序完成");
    }
}
```

**为什么这样设计能验证我们要的结论**：

- **恰好一次**：`sequential_work(self)` 按值消费，被 `pop_front` 取出后只调一次；并发靠 `contenders` 互斥保证不会两个线程同时处理同一队首。用 `HashMap` 计数兜底，捕捉任何重复或丢失。
- **保序**：多生产者下「全局 FIFO 顺序」是非确定的（不同生产者的 push 在时间上交错），但**同一生产者的 push 是它自己线程内的顺序执行**，因此该生产者的 id 在 ArrayDeque 中的相对先后不变，消费出来必然升序。于是逐生产者过滤后断言升序，既诚实地反映了并发真相，又严格验证了 FIFO。
- **背压**：总任务数 2000 大于 `INORDER_QUEUE_SIZE`(1024)，`push` 必然有失败；生产者用「失败就 consume 腾位再重试」处理，正好覆盖满队列路径。
- **活性（不死锁）**：所有消费者线程 join 后，`contenders` 必然归 0（任何持有者最终都会因 `c'==0` 退出），因此最后一次 `queue.consume()` 一定能进入临界区并把剩余就绪任务全部排空——测试不会挂死。

**操作步骤**：

1. 把上述测试粘进 `src/wireguard/router/queue.rs` 的 `#[cfg(test)] mod tests { … }`。
2. 运行 `cargo test --lib test_ordered_consume_multi_producer_multi_consumer`（可多跑几次 / 加 `--release` 看稳定性）。
3. 尝试把第 4.3.5 练习里的「`fetch_sub(contenders)` 改成 `fetch_sub(1)`」套进本测试，观察是否挂死，以反面验证重入计数的必要性。

**预期结果**：测试通过；改坏 `fetch_sub` 后测试会死锁或计数对不上。运行结果**待本地验证**。

## 6. 本讲小结

- `Queue<J>` 是 **per-peer** 的保序队列，和设备级的 `ParallelQueue<T>`（crossbeam 扇出通道）是两个不同的东西，别混淆。
- 处理被拆成两段：`ParallelJob::parallel_work`（可并行的重活，`&self`）与 `SequentialJob::sequential_work`（顺序敏感的收尾，`self`）；`is_ready()` 是两段之间的就绪闸门。
- 调用方先用 `push` 把任务克隆一份进保序队列，再用 `work.send` 把另一份丢给线程池；worker 干完 `parallel_work` 后调用 `job.queue().consume()` 驱动串行排空。
- `consume` 用单个原子计数器 `contenders` 同时实现「无锁互斥」与「持续排空」：`fetch_add` 抢号、`pos>0` 投票返回、`fetch_sub(局部c)` 一次性归还本轮全部票数并据此决定重入。
- 保序靠「只看队首、队首未就绪即停」实现，后到的就绪任务无法插队；加密结果的可见性靠 `ready` 的 `Release`（写）/ `Acquire`（读）配对发布。
- 容量 `INORDER_QUEUE_SIZE = 1024`，满则 `push` 返回 `false`，由调用方做背压丢包；`consume` 在 `sequential_work` 前就释放队列锁，避免自死锁并允许并发 push。

## 7. 下一步学习建议

- **u5-l2 / u5-l3**：回到 `SendJob` / `ReceiveJob`，结合本讲看清「parallel_work 加解密 → consume 按 Release/Acquire + 队首闸门有序收尾」在真实加解密场景下如何落地。
- **u5-l7（防回放）**：`sequential_work` 里那行 `protector.lock().update(counter)` 必须串行，正是因为本讲的保序与互斥——防回放位图依赖「同一 peer 的报文按序、单线程地更新」。
- **u7-l4（测试策略）**：`queue.rs` 的 `test_fuzz_queue` 跑了 100 万次随机 push/consume，可与本讲综合实践对照，体会项目用「并发模糊测试」压并发原语的思路。
- 想吃透无锁算法，可顺带读 crossbeam 文档与 Rust 内存模型材料，对照本讲 `SeqCst` vs `Release/Acquire` 的取舍。

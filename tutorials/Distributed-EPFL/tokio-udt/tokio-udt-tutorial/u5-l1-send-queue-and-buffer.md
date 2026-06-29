# 发送侧：调度队列 UdtSndQueue 与发送缓冲 SndBuffer

## 1. 本讲目标

本讲进入「数据通路」的发送侧。学完后你应当能够：

- 说清 **UdtSndQueue** 如何用一个 `BinaryHeap` 充当「定时调度器」，决定「哪个 socket、在什么时刻」被允许发包。
- 读懂 **worker 主循环**：它如何在「睡到指定时刻」与「被新任务唤醒」之间做 `select`，又如何用一个独立的 mpsc 任务把「调度」和「真正发包」解耦。
- 理解 **SndBuffer** 如何把一条用户消息按 MSS 切片、如何标记消息分片位置、如何支持重传读取与 TTL 过期丢弃。
- 解释 SndBuffer 里 `current_position`（已发游标）与 `ack_data` 推进的队首（已确认游标）这两个概念为什么必须分开维护。
- 把整条发送主链路 `send → add_message → update_snd_queue → worker → next_data_packets → send_data_packets` 画成一张时序图。

本讲只讲「队列与缓冲」这两个数据结构本身，**不**展开拥塞窗口怎么算、丢包怎么检测、ACK 怎么生成——那些留到第 6 单元。本讲里出现 `window_size`、`snd_loss_list` 等术语时，你只需知道「它们决定能不能发新数据 / 要不要重传」，细节后续再讲。

## 2. 前置知识

本讲假设你已经读过：

- **u3-l2 UdtSocket 核心结构与状态机**：知道 `UdtSocket` 持有 `snd_buffer`、`state`（`SocketState`）等字段，知道 `is_alive()`、`UdtStatus::Connected` 等状态判定。
- **u3-l3 多路复用器 UdtMultiplexer**：知道一个 multiplexer 持有 1 个 `UdtSndQueue`、1 个 `UdtRcvQueue`，并在 `run` 中 `tokio::spawn` 了发送 worker（`snd_queue.worker()`）。
- **u4-l2 数据包格式**：知道数据包 = 16 字节包头 + payload，知道 `PacketPosition`（First/Last/Middle/Only）标记消息分片，知道 `seq_number` 是 31 位循环序号。
- **u4-l4 序列号循环算术**：知道 `SeqNumber - SeqNumber` 返回 `i32`（环上带符号距离），`SeqNumber + 1` 会非负回绕。

几个本讲要用到、但属于后续讲义的关键事实，先记住结论即可：

- `state.curr_snd_seq_number`：本端「下一个要分配」的发送序号。
- `state.last_ack_received`：对端最新 ACK 确认到的序号（已确认水位）。
- `state.last_data_ack_processed`：本端发送缓冲里「已经按 ACK 释放过」的水位。
- `snd_loss_list`：待重传的序号集合。
- `interpacket_interval`：相邻两个包之间的目标间隔（初值 1µs），是速率控制的输出。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲角色 |
| --- | --- | --- |
| `src/queue/mod.rs` | queue 子模块的入口，声明 4 个子模块并 `pub(crate) use` 重导出 | 告诉你 `UdtSndQueue` / `SndBuffer` 在 crate 内如何被引用 |
| `src/queue/snd_queue.rs` | 发送调度队列 `UdtSndQueue`：定时弹出的 `BinaryHeap` + worker 主循环 | 本讲核心之一 |
| `src/queue/snd_buffer.rs` | 发送缓冲 `SndBuffer`：消息切片、重传读取、TTL 丢弃 | 本讲核心之二 |
| `src/socket.rs` | 发送主链路的「粘合层」：`send`、`update_snd_queue`、`next_data_packets`、`send_data_packets` | 把队列和缓冲串起来的调用方 |

一句话概括分工：**队列管「时序」（什么时候发），缓冲管「数据」（发什么、还能不能重传）**。

## 4. 核心概念与源码讲解

### 4.1 UdtSndQueue：用 BinaryHeap 做定时调度

#### 4.1.1 概念说明

UDT 不是「有数据就立刻把网卡打满」。它要按速率控制给出的节奏，**在指定时刻**才允许某个 socket 取下一批包。于是需要一个「定时器集合」：

- 里面存很多「事件」，每个事件 = `(目标时刻, socket_id)`。
- 每次都要取出「时刻最早」的那个事件。
- 新事件可能随时插入，而且可能比当前最早的事件还早（比如发生丢包要立刻重传）。

这正是**优先队列（最小堆）**的典型用法。Rust 标准库的 `BinaryHeap` 是**最大堆**，所以要让「时刻最小」的排在堆顶，需要在 `Ord` 比较里做一次**反转**。

注意：`UdtSndQueue` 是**每个 multiplexer 一个**（见 u3-l3），它服务的不是某一条连接，而是**复用同一个 UDP socket 的所有 socket**。堆里同时混着多条连接的发送事件，谁的时刻到了就把谁「弹」出来处理。这正是「多路复用」在发送侧的体现：一个 worker、一个堆，调度多条连接。

#### 4.1.2 核心流程

调度队列的工作方式像一个「带闹钟的待办清单」：

```
事件集合（最小堆，按 timestamp 升序）
  ┌────────────────────────────┐
  │ (t=10:00:01.000, socket A) │ ← 堆顶：最早
  │ (t=10:00:01.200, socket B) │
  │ (t=10:00:01.500, socket A) │
  └────────────────────────────┘

worker 循环：
  1. 看堆顶
  2. 堆顶.timestamp <= 现在？
       是 → 弹出，处理这个 socket（问它要下一批包）
       否 → 睡到堆顶.timestamp，或被新事件唤醒（二者先到先走）
  3. 堆空 → 一直睡，直到被新事件唤醒
```

关键点：处理完一个 socket 后，worker 会用 socket 返回的「下一个目标时刻」**把它重新插回堆里**。所以一个有数据要发的 socket 会**周期性地自我重新入队**，直到它没有更多数据可发（此时不再入队，离开调度）。

#### 4.1.3 源码精读

先看队列里存的事件类型和它的排序：

[src/queue/snd_queue.rs:12-23](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_queue.rs#L12-L23) — `SendQueueNode` 只有两个字段：`timestamp` 和 `socket_id`；`Ord` 实现里那句 `.reverse()` 是本结构的灵魂——它让最大堆表现为「timestamp 越小越靠堆顶」。

```rust
struct SendQueueNode {
    timestamp: Instant,
    socket_id: SocketId,
}

impl Ord for SendQueueNode {
    // Send queue should be sorted by smaller timestamp first
    fn cmp(&self, other: &Self) -> Ordering {
        self.timestamp.cmp(&other.timestamp).reverse()
    }
}
```

再看队列结构体本身：

[src/queue/snd_queue.rs:31-37](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_queue.rs#L31-L37) — 四个字段：`queue`（堆本体，用 `Mutex` 保护）、`notify`（用来唤醒 worker 的 `tokio::sync::Notify`）、`start_time`（队列创建时刻，后面会当「立刻执行」的哨兵值用）、`socket_refs`（`socket_id → Weak<UdtSocket>` 的本地缓存）。

```rust
pub(crate) struct UdtSndQueue {
    queue: Mutex<BinaryHeap<SendQueueNode>>,
    notify: Notify,
    start_time: Instant,
    socket_refs: Mutex<BTreeMap<SocketId, Weak<UdtSocket>>>,
}
```

新事件入队的入口有两个：`insert` 和 `update`。

[src/queue/snd_queue.rs:114-125](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_queue.rs#L114-L125) — `insert` 把节点 push 进堆，然后**只在「新节点恰好成了堆顶」时**才 `notify_one()`。这个判断很重要：worker 现在可能正睡到某个更晚的时刻，只有当新事件比当前所有事件都早（成为堆顶）时，才值得提前叫醒它。

```rust
pub fn insert(&self, ts: Instant, socket_id: SocketId) {
    let mut sockets = self.queue.lock().unwrap();
    sockets.push(SendQueueNode { socket_id, timestamp: ts });
    if let Some(node) = sockets.peek() {
        if node.socket_id == socket_id {
            self.notify.notify_one();
        }
    }
}
```

[src/queue/snd_queue.rs:127-150](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_queue.rs#L127-L150) — `update(socket_id, reschedule)` 是 socket 主动「请求被调度」的入口。它有一个精妙的小技巧：把 `timestamp` 设成 `self.start_time`（队列创建时刻），而 `now >= start_time` 永远成立，所以这等价于「立刻执行」。它分两种语义：

- `reschedule=false`（普通「我有新数据了」）：socket 不在堆里就插一条「立刻执行」；已经在堆里就**什么都不做**（已经在排队，别重复插）。
- `reschedule=true`（丢包/超时，「我要立刻重传」）：尽量把该 socket 的时刻改成 `start_time` 并叫醒 worker。

```rust
pub fn update(&self, socket_id: SocketId, reschedule: bool) {
    if reschedule {
        let mut sockets = self.queue.lock().unwrap();
        if let Some(mut node) = sockets.peek_mut() {
            if node.socket_id == socket_id {
                node.timestamp = self.start_time;   // 立刻执行
                self.notify.notify_one();
                return;
            }
        };
    };
    if !self.queue.lock().unwrap().iter().any(|n| n.socket_id == socket_id) {
        self.insert(self.start_time, socket_id);     // 不在堆里 → 入队立刻执行
    } else if reschedule {
        self.remove(socket_id);                       // 在堆里但不是堆顶 → 拆掉重插
        self.insert(self.start_time, socket_id);
    }
}
```

> 小提示：`peek_mut()` 拿到的是堆顶的可变引用，所以「改 `start_time`」只能作用于**当前堆顶**那个节点；若目标 socket 不是堆顶，就走 `remove + insert` 的重建路径。

#### 4.1.4 代码实践

**目标**：验证「`.reverse()` 让最大堆变成按时刻升序弹出」这一点，是整个调度正确的前提。

**操作步骤**（纯源码阅读型，无需运行）：

1. 打开 [src/queue/snd_queue.rs:18-23](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_queue.rs#L18-L23)。
2. 假设 `Ord` 里**没有** `.reverse()`，回答：堆顶会变成 `timestamp` 最大的节点，worker 第一步 `peek()` 看到的会是「最晚」的事件，会发生什么？
3. 接着看 worker 主循环里 `if node.timestamp <= Instant::now()` 的判断：在「没有 reverse」的错误实现下，这个判断何时为真？系统会表现出什么症状（提示：发包严重延迟或卡住）。

**需要观察的现象 / 预期结果**：

- 正确实现下，堆顶总是「最早到点」的事件，worker 能及时发包。
- 去掉 `.reverse()` 后，堆顶变成最晚的事件，`timestamp <= now` 长期为假，worker 会一直 `sleep_until` 一个很远的未来时刻，**新数据发不出去**。这反过来说明：这一行 `.reverse()` 不是可有可无的注释，而是正确性的关键。

> 待本地验证：第 3 步的行为推断可以通过临时改本地副本（**不要提交**）跑 `cargo run --bin udt_sender` 观察吞吐是否归零来确认。

#### 4.1.5 小练习与答案

**练习 1**：`insert` 里为什么只在「新节点成为堆顶」时才 `notify_one()`，而不是每次插入都通知？

**参考答案**：worker 当前要么在处理某个事件，要么在 `sleep_until(堆顶时刻)`。只有当新事件的时刻**比当前堆顶还早**（即新事件自己成了堆顶）时，才可能需要让 worker 提前醒来；否则 worker 既有的「睡到原堆顶时刻」计划仍然正确，多通知只会造成无谓唤醒。

**练习 2**：`update` 用 `self.start_time` 当 timestamp 来表达「立刻执行」。这种写法相对直接用 `Instant::now()` 有什么细微差别？是否一定等价？

**参考答案**：`start_time` 是队列创建时刻，`now` 一定 `>= start_time`，所以二者都能保证「`timestamp <= now` 成立、立刻被弹出」。差别在于：若用 `now()`，多个几乎同时入队的「立刻执行」节点之间会有纳秒级先后；用同一个 `start_time` 则它们的 timestamp 完全相同，弹出顺序由堆的内部比较（此时退化为 `socket_id` 不影响，因为 `Ord` 先比 timestamp 再……实际只比 timestamp）决定。对本协议而言二者都正确，`start_time` 只是更省一次系统调用。

---

### 4.2 worker 主循环：在「到点发送」与「notify 唤醒」之间 select

#### 4.2.1 概念说明

`UdtSndQueue::worker()` 是一个**永不返回的 async 函数**，由 multiplexer 在 `run` 时 `spawn`（见 u3-l3）。它是发送侧的「心脏」，持续不断地：

1. 看堆顶事件有没有到点；
2. 到点了，就问对应 socket「你现在要发哪些包」；
3. 把 socket 重新排进堆里（用 socket 给出的下一个目标时刻）；
4. 把要发的包丢给一个**独立的发送任务**去做真正的 UDP I/O。

这里有两件事值得单独点出：

- **调度与发包解耦**：worker 只负责「决定发什么、什么时候发」，真正的 `send_mmsg_to`（系统调用）交给另一个 `tokio::spawn` 的任务通过 mpsc channel 接收。这样调度循环不会被偶发的 UDP 发送阻塞卡住。
- **两种「醒」**：worker 睡着时有两个醒来的理由——「睡到点了」（`sleep_until`）或「有更新更急的事件插进来了」（`notify`）。用 `tokio::select!` 让二者谁先到就谁走。

#### 4.2.2 核心流程

```
worker() 启动：
  建一个 mpsc channel（容量 50），spawn 一个「发送任务」专门 rx.recv() 后调 send_data_packets

  loop {
      1. 锁堆，peek 堆顶：
           有堆顶 且 timestamp <= now  → Ok(pop 出来的 node)
           有堆顶 但还没到点          → Err(Some(堆顶 timestamp))
           堆空                       → Err(None)

      2. 分支：
         Ok(node)：
           get_socket(node.socket_id)
           packets = socket.next_data_packets().await
           若 Some((packets, ts))：
               insert(ts, socket_id)          // 用返回的下一时刻重新入队
               tx.send((socket, packets))     // 交给发送任务
           若 None：                           // 暂时没东西可发
               不重新入队 → 该 socket 暂离调度

         Err(Some(ts))：                      // 堆顶还没到点
           select! {
               sleep_until(ts) 完成 → 继续（去点重新看堆顶）
               notify 唤醒       → 继续（多半是有更急的事件）
           }

         Err(None)：                          // 堆空
           notify.notified().await            // 死等，直到有事件 insert 进来
  }
```

注意 `Ok(node)` 分支里的关键设计：**只有 `next_data_packets` 返回 `Some` 时才 `insert` 重新入队**。返回 `None` 意味着「这条连接现在没数据可发 / 被拥塞窗口卡住了」，于是它**主动离开**调度堆，直到下次有事件（新 `send`、对端 ACK 推进、丢包、超时）调用 `update_snd_queue` 把它重新塞回来。这避免了一个空闲连接在堆里反复空转。

#### 4.2.3 源码精读

[src/queue/snd_queue.rs:64-75](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_queue.rs#L64-L75) — worker 开头先建 channel 并 spawn 发送任务。容量常量 `TOKIO_CHANNEL_CAPACITY = 50`。这个任务拿到 `(socket, packets)` 后调用 `send_data_packets`，把调度与真正的 UDP I/O 隔离开。

```rust
pub async fn worker(&self) -> Result<()> {
    let (tx, mut rx) = tokio::sync::mpsc::channel(TOKIO_CHANNEL_CAPACITY);
    tokio::spawn(async move {
        while let Some((socket, packets)) = rx.recv().await {
            let socket: SocketRef = socket;
            socket.send_data_packets(packets).await
                .expect("failed to send packets")
        }
    });
    // ……主循环……
```

[src/queue/snd_queue.rs:77-111](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_queue.rs#L77-L111) — 主循环本体，三分支对应上面的流程图：

```rust
loop {
    let next_node = {
        let mut sockets = self.queue.lock().unwrap();
        let first_node = sockets.peek();
        match first_node {
            Some(node) => {
                if node.timestamp <= Instant::now() { Ok(sockets.pop().unwrap()) }
                else { Err(Some(node.timestamp)) }
            }
            None => Err(None),
        }
    };
    match next_node {
        Ok(node) => {
            if let Some(socket) = self.get_socket(node.socket_id).await {
                if let Some((packets, ts)) = socket.next_data_packets().await? {
                    self.insert(ts, node.socket_id);          // 重新入队
                    tx.send((socket, packets)).await.unwrap();// 交给发送任务
                }
            }
        }
        Err(Some(ts)) => {
            tokio::select! {
                _ = Self::sleep_until(ts) => {}
                _ = self.notify.notified() => {}
            }
        }
        _ => { self.notify.notified().await; }   // 堆空
    }
}
```

[src/queue/snd_queue.rs:161-172](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_queue.rs#L161-L172) — `sleep_until` 在 Linux 上用 `tokio_timerfd::Delay`（高精度、基于 timerfd），其它平台回退到 `tokio::time::sleep_until`。为什么 Linux 要专门用 timerfd，留到 u8-l4 平台快路径讲，这里只需知道：发送调度频率很高，需要更精准的定时器。

#### 4.2.4 代码实践

**目标**：跟踪「一个 socket 被弹出后，如何决定自己下一步的命运（重新入队 or 暂离）」。

**操作步骤**：

1. 阅读 [src/queue/snd_queue.rs:92-100](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_queue.rs#L92-L100) 的 `Ok(node)` 分支。
2. 找到 `next_data_packets` 在什么情况下返回 `Ok(None)`（提示：见 [src/socket.rs:307-310](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L307-L310) 拥塞窗口已满、[src/socket.rs:328-332](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L328-L332) 缓冲已空）。
3. 回答：当 `next_data_packets` 返回 `None` 时，worker 既不 `insert` 也不报错，那这个 socket 之后靠什么「复活」？

**需要观察的现象 / 预期结果**：

- 预期结论：socket 靠**外部再次调用 `update_snd_queue`** 复活。具体地：
  - 缓冲空后，用户下一次 `send()` 会 `update_snd_queue(false)`（见 4.3.3）。
  - 拥塞窗口卡住后，对端发来 ACK 推进 `last_ack_received` 时，[src/socket.rs:554](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L554) 的 `update_snd_queue(false)` 会把它重新塞回堆。
  - 丢包/超时时，`update_snd_queue(true)` 会以「立刻执行」重新入队。

> 待本地验证：可在 `worker` 的 `Ok(node)` 分支与 `next_data_packets` 返回 `None` 处各加一行 `eprintln!`，跑 `udt_sender`/`udt_receiver`，观察「暂离 → 复活」的节奏。

#### 4.2.5 小练习与答案

**练习 1**：worker 主循环为什么要把「真正发包」交给一个独立的 mpsc 任务，而不是在弹出节点后直接 `await send_data_packets`？

**参考答案**：调度循环要同时服务多条连接、且要严格按时刻弹出下一个事件。若直接在循环里 `await` 发包，一旦某次 UDP 发送（系统调用）变慢，整个 multiplexer 的调度都会被拖住，导致**其它连接**的发送时刻被错过。用 channel + 独立任务把 I/O 异步化、解耦后，调度循环只负责「快速决定发什么」，I/O 阻塞不会反压到调度。channel 容量 50 起到一定的缓冲与背压作用。

**练习 2**：`Err(None)`（堆空）分支用的是 `self.notify.notified().await`（死等），而 `Err(Some(ts))` 用的是 `select! { sleep_until(ts), notify }`。为什么前者不需要 `select!`？

**参考答案**：堆空意味着没有任何一个 socket 在等待发送，worker 此刻**没有任何「到点」事件**可等，唯一能让它醒来的就是「有新事件被 `insert`/`update` 进来」，而这一定伴随 `notify_one()`。所以只需等 `notify`，不需要 `sleep_until`。`Err(Some(ts))` 分支则同时存在「堆顶到点」和「可能插入更急事件」两种唤醒来源，才需要 `select!`。

---

### 4.3 SndBuffer：切片、重传与 TTL，以及两个游标

#### 4.3.1 概念说明

如果说 `UdtSndQueue` 管「什么时候发」，那 `SndBuffer` 管「发什么」。每个 `UdtSocket` 持有自己的 `SndBuffer`。它要做三件事：

1. **接收用户消息并切片**：用户调用 `send(&[u8])` 给一段可能很大的字节流，`SndBuffer` 把它按 `payload_size`（≈ MSS）切成一个个 `SndBufferBlock`，并给同一消息的所有块打上相同的 `msg_number` 和正确的 `PacketPosition`。
2. **供新数据发送读取**：worker 要发新数据时，按顺序取出一批块，每块分配一个递增的 `seq_number`，组装成数据包。
3. **供重传读取 + TTL 丢弃**：某块丢了要重传时，能按序号定位到对应块重新打包；若该消息已超过生存时间（TTL），则整条消息丢弃并发 `MsgDropRequest`。

`SndBuffer` 内部是一个 `VecDeque<SndBufferBlock>`，块按发送顺序排列。理解它的关键，是搞清楚**两个「水位」**：

- **队首（已确认水位）**：`VecDeque` 的 front 永远是「最早一个还没被对端确认的块」。已被确认的块通过 `ack_data` 从队首 `pop_front` 丢弃。
- **`current_position`（已发水位）**：已经「作为新数据取出并发送过至少一次」的块的范围边界。`[front, current_position)` 之间的块 = 已发但未确认（在途），`[current_position, len)` = 还没发的新数据。

这就是本讲练习要回答的核心：**确认（ack）和发送（send）是两条速度不同的水位线，必须分开维护。**

#### 4.3.2 核心流程

```
用户 send(data)：
  add_message(data, ttl, in_order)
    ├─ data.chunks(payload_size) 切片
    ├─ 容量检查：buffer.len() + 切片数 > max_size → 返回 OutOfMemory
    ├─ 给每个 chunk 赋 msg_number、PacketPosition(Only/First/Middle/Last)、origin_time
    └─ next_msg_number += 1

worker 发新数据（next_data_packets 的 None→新数据分支）：
  fetch_batch(seq_number, ...)
    ├─ buffer.range(current_position..).take(100)   // FETCH_BATCH_SIZE
    ├─ 每个 block 调 as_data_packet，seq_number 每块 +1
    └─ current_position += 取出的块数

对端 ACK 到达 → ack_data(offset)：
  for _ in 0..offset { buffer.pop_front(); current_position -= 1; }
  // 队首丢弃已确认块；current_position 同步下移，保持指向同一逻辑块

丢包重传（next_data_packets 的 Some→重传分支）：
  read_data(offset, seq_number, ...)
    ├─ buffer.get(offset) 取该块
    ├─ 若 has_expired() → 推进 current_position 跨过整条过期消息，返回 Err((msg_number, msg_len))
    │                      （上层据此发 MsgDropRequest）
    └─ 否则 → as_data_packet 打包返回，用于重传
```

关于 TTL：`add_message` 时 `ttl=None`（普通流式数据，见 [src/socket.rs:1010](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1010) 传的就是 `None`），所以 `has_expired()` 恒为 `false`，走 MsgDrop 路径的机会主要面向消息模式（messaging）下的带 TTL 发送。本讲了解这条分支存在即可。

#### 4.3.3 源码精读

先看块和缓冲的结构：

[src/queue/snd_buffer.rs:13-21](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_buffer.rs#L13-L21) — 一个 `SndBufferBlock` 持有 `data`（`Bytes`）、所属 `msg_number`、`origin_time`、可选 `ttl`、`in_order`、`position`。

[src/queue/snd_buffer.rs:51-58](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_buffer.rs#L51-L58) — `SndBuffer` 字段：`max_size`（块数上限）、`buffer: VecDeque<SndBufferBlock>`、`payload_size`、`next_msg_number`、`current_position`。注意 `current_position` 是 `usize`，是 buffer 的**下标**。

切片逻辑在 `add_message`：

[src/queue/snd_buffer.rs:71-102](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_buffer.rs#L71-L102) — 重点三段：容量检查（满了返回 `OutOfMemory`，这正是 `poll_write` 会 Pending 的根因）、`chunks(payload_size)` 切片、按 `idx` 与总片数 `chunks_len` 决定 `PacketPosition`（只有一片→`Only`，第一片→`First`，最后一片→`Last`，中间→`Middle`，与 u4-l2 讲的分片编码对齐）。

```rust
let chunks = data.chunks(self.payload_size);
let chunks_len = chunks.len();
if self.buffer.len() + chunks_len > self.max_size as usize {
    return Err(Error::new(ErrorKind::OutOfMemory, "Send buffer is full"));
}
// ……position 规则：
// idx==0 && chunks_len==1 → Only
// idx==0                  → First
// idx==chunks_len-1       → Last
// else                    → Middle
self.next_msg_number = self.next_msg_number + 1;
```

新数据读取 `fetch_batch`：

[src/queue/snd_buffer.rs:144-162](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_buffer.rs#L144-L162) — 从 `current_position` 开始 `range`，最多取 `FETCH_BATCH_SIZE=100` 个块，逐块打包并把 `seq_number` 递增，最后 `current_position += blocks.len()`。这就是「已发水位」向前推进的地方。

```rust
let blocks: Vec<_> = self.buffer
    .range(self.current_position..)
    .take(FETCH_BATCH_SIZE)
    .map(|block| {
        let packet = block.as_data_packet(seq_number, dest_socket_id, start_time);
        seq_number = seq_number + 1;
        packet
    })
    .collect();
self.current_position += blocks.len();
```

已确认推进 `ack_data`：

[src/queue/snd_buffer.rs:104-110](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_buffer.rs#L104-L110) — 这是理解「两个游标」的关键代码。每 `pop_front` 一个已确认块，`current_position` 就 `-1`。

```rust
pub fn ack_data(&mut self, offset: i32) {
    for _ in 0..offset {
        if self.buffer.pop_front().is_some() {
            self.current_position -= 1;
        }
    }
}
```

**为什么 `current_position` 要跟着减？** 因为 `current_position` 是 `VecDeque` 的下标。`pop_front` 会让所有元素整体左移一位（原下标 `i` 的块变成下标 `i-1`）。若不让 `current_position` 同步下移，它就会指到「后一个」块上去，发新数据时就会**跳块**。所以这里 `-1` 是为了在「队首被裁掉」后，仍指向同一个逻辑块。

重传读取 `read_data`：

[src/queue/snd_buffer.rs:112-142](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_buffer.rs#L112-L142) — 按 `offset` 取单个块用于重传；若该块 `has_expired()`，则把 `current_position` 跨过整条过期消息（同一 `msg_number` 的连续块），并返回 `Err((msg_number, msg_len))`，让上层 [src/socket.rs:275-290](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L275-L290) 发 `MsgDropRequest` 并推进 `curr_snd_seq_number`。

把缓冲和队列粘起来的，是 socket 侧的两个函数：

[src/socket.rs:984-1013](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L984-L1013) — `UdtSocket::send`：校验 Stream 模式 + `Connected` 状态；若缓冲原为空，顺手把 `last_rsp_time` 刷成现在（**延迟 EXP 超时定时器**，避免刚发数据就被误判超时）；然后 `add_message(data, None, false)`；最后 `update_snd_queue(false)` 把自己排进调度堆。

[src/socket.rs:978-982](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L978-L982) — `update_snd_queue` 只是把请求转发给所在 multiplexer 的 `snd_queue.update(socket_id, reschedule)`。

```rust
pub fn send(&self, data: &[u8]) -> Result<()> {
    // ……校验……
    if self.snd_buffer.lock().unwrap().is_empty() {
        self.state().last_rsp_time = Instant::now(); // 延迟 EXP 定时器
    }
    self.snd_buffer.lock().unwrap().add_message(data, None, false)?;
    self.update_snd_queue(false);  // 排进调度堆
    Ok(())
)
```

#### 4.3.4 代码实践

**目标**：亲手推演一次「发送 → 确认」过程中，`current_position` 与队首如何分别前进。

**操作步骤**（纸笔推演型）：

设 `payload_size = 4`，`max_size` 足够大。用户连续两次 `send`：

- `send(b"ABCDEFGH")`（8 字节 → 切成 2 块：`ABCD`、`EFGH`，同 `msg_number=0`）
- `send(b"IJKL")`（4 字节 → 1 块 `IJKL`，`msg_number=1`）

1. 画出每次 `add_message` 后 `buffer` 的内容与 `current_position`。
2. 假设 worker 先后两次 `fetch_batch`：第一次取 2 块（`ABCD`/`EFGH`），第二次取 1 块（`IJKL`）。标出每次 `fetch_batch` 后 `current_position` 的值。
3. 此时对端 ACK 确认了前 2 个块（`offset=2`），`ack_data(2)` 执行后，`buffer` 与 `current_position` 变成什么？

**需要观察的现象 / 预期结果**：

- 初始 `add_message` 后：`buffer = [ABCD, EFGH, IJKL]`，`current_position = 0`。
- 第一次 `fetch_batch` 取 2 块后：`current_position = 2`（`[0,2)` = ABCD/EFGH 已发，`[2,3)` = IJKL 未发）。
- 第二次 `fetch_batch` 取 1 块后：`current_position = 3`（全部已发）。
- `ack_data(2)`：`pop_front` 两次（ABCD、EFGH 被丢弃），`current_position` 从 3 减到 1。此时 `buffer = [IJKL]`，`current_position = 1`，含义是「IJKL 仍是下标 0 那个块，且它已发（current_position=1 ≥ 它的下标 0+1）」。注意：**虽然 `current_position` 数值从 3 变成 1，但它指向的逻辑块（IJKL）没变**——这正是「`-1` 保指向」的体现。

> 待本地验证：可给 `SndBuffer` 的 `add_message`/`fetch_batch`/`ack_data` 各加一行 `dbg!`（本地副本，勿提交），用 `cargo test` 跑 `snd_buffer` 相关单测观察真实数值序列。

#### 4.3.5 小练习与答案

**练习 1**：`add_message` 的容量检查用 `self.buffer.len() + chunks_len > self.max_size`。`max_size` 的单位是「字节数」还是「块数」？

**参考答案**：是**块数**。`self.buffer.len()` 是 `VecDeque<SndBufferBlock>` 的长度（块数），`chunks_len` 是这次消息切出来的块数，二者相加与 `max_size`（`u32`）比较。所以 `max_size` 限制的是「缓冲里最多存多少个 MSS 大小的块」，对应配置里的 `snd_buf_size`（以包数计，见 u2-l3）。

**练习 2**：`read_data` 在块过期时返回 `Err((msg_number, msg_len))`。为什么返回的是「整条消息」的信息（`msg_len` = 同 `msg_number` 的连续块数），而不是单个块？

**参考答案**：UDT 的 TTL 是**消息级**语义——一条消息要么完整送达，要么整体作废。单个块过期意味着整条消息已无意义，继续重传同一消息的其它块是浪费。因此上报「整条消息的 msg_number 与长度」，让上层一次性发一个覆盖 `[start, end]` 区间的 `MsgDropRequest`（见 [src/socket.rs:275-282](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L275-L282)），并直接把 `curr_snd_seq_number` 推过这条消息，避免后续无谓重传。

**练习 3**：为什么 `ack_data` 要写成「成功 `pop_front` 一次，`current_position` 才 `-1`」（带 `if`），而不是无条件 `-1`？

**参考答案**：防御性写法：`pop_front` 在缓冲已空时返回 `None`。若调用方传入的 `offset` 大于当前缓冲块数（理论上不应发生，但 ACK 可能因序号回绕/重复而偏大），无条件 `-1` 会导致 `current_position`（`usize`）下溢 panic。带 `if` 保证「只在真正丢弃一个块时才下移游标」，二者始终同步。

---

## 5. 综合实践

**任务**：把本讲的两条主线（调度队列 + 发送缓冲）串起来，画出**一次完整发送**的时序图，并解释「两个游标」为什么要分开。

### 第 1 步：画出时序图

跟踪链路 `socket.send → SndBuffer::add_message → update_snd_queue → snd_queue.worker → next_data_packets → send_data_packets`。请按下面的参与者补全箭头方向与调用点（用本讲给出的源码行号标注）：

```
用户代码        UdtSocket          SndBuffer        UdtSndQueue        worker/发送任务       对端/UDP
  │   send(data)  │                    │                  │                    │                  │
  │──────────────▶│                    │                  │                    │                  │
  │               │  add_message(data) │                  │                    │                  │
  │               │───────────────────▶│ (切片+position)  │                    │                  │
  │               │  update_snd_queue(false)              │                    │                  │
  │               │──────────────────────────────────────▶│ update(socket_id,false)               │
  │               │                    │            insert(start_time) notify_one                 │
  │               │                    │                  │                    │                  │
  │               │             （worker 主循环被唤醒，弹出该 socket）                              │
  │               │  next_data_packets()│                  │                    │                  │
  │               │◀───────────────────│ fetch_batch ─────│                    │                  │
  │               │  返回 Some((packets, ts))              │                    │                  │
  │               │                    │  insert(ts) 重新入队; tx.send((socket,packets))          │
  │               │                    │                  │───────────────────▶│ send_data_packets│
  │               │                    │                  │                    │─────────────────▶│ send_mmsg_to
```

请在本地用纸或编辑器把这张图补全，并在每个箭头上标注对应的源码位置：

- `send` → [src/socket.rs:984-1013](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L984-L1013)
- `add_message` → [src/queue/snd_buffer.rs:71-102](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_buffer.rs#L71-L102)
- `update_snd_queue` → [src/socket.rs:978-982](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L978-L982)
- `update/insert` → [src/queue/snd_queue.rs:114-150](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_queue.rs#L114-L150)
- worker 弹出 → [src/queue/snd_queue.rs:77-111](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_queue.rs#L77-L111)
- `next_data_packets`（新数据分支调 `fetch_batch`）→ [src/socket.rs:311-333](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L311-L333)
- `fetch_batch` → [src/queue/snd_buffer.rs:144-162](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_buffer.rs#L144-L162)
- `send_data_packets` → [src/socket.rs:774-782](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L774-L782)

### 第 2 步：解释「两个游标」为什么必须分开

结合 4.3 的推演，回答：为什么 `SndBuffer` 不能只用一个游标（比如只记「已确认」或只记「已发」），而必须同时维护 `current_position`（已发）和「队首」（已确认）？

**参考要点**（写进你的笔记）：

1. **发送速度 > 确认速度**：发送方会先把数据发出去（`current_position` 推进），等对端 ACK 回来才确认（`ack_data` 推进队首）。两条水位线天然不同步。
2. **中间这段 `[队首, current_position)` 正是「在途未确认」数据**：它既是拥塞窗口的计算依据（`curr_snd_seq_number - last_ack_received`，见 [src/socket.rs:306](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L306)），也是丢包时 `read_data` 要重新读取的范围。若合并成一个游标，就无法表达「已发但还没被确认」这个中间态。
3. **重传与发新数据共用同一个 buffer**：`fetch_batch` 从 `current_position` 往后取新数据，`read_data` 按 `offset`（相对队首）取在途数据重传。两个读位置互不干扰，正因有两个游标把它们隔开。

> 待本地验证：跑 `cargo run --bin udt_receiver` 与 `cargo run --bin udt_sender`，对照你画的时序图，在心里把 sender 每秒打印的 `get_congestion_window_size`（在途上限）与「已发未确认块数」对应起来——后者正是这两个游标之间的那段。

## 6. 本讲小结

- **UdtSndQueue 是一个按时刻升序弹出的最小堆**：靠 `Ord` 里的 `.reverse()` 把标准库的最大堆翻转成「timestamp 越小越先出」，每个 multiplexer 一个，混合调度所有复用连接。
- **worker 用 `select!` 在 `sleep_until(堆顶时刻)` 与 `notify` 之间二选一**：堆空时只等 `notify`；处理完一个 socket 后**只在它还有数据时**才用返回的 `ts` 重新入队，否则让它暂离调度。
- **调度与发包解耦**：worker 通过容量 50 的 mpsc channel 把 `(socket, packets)` 交给独立任务做 `send_data_packets`，避免 UDP I/O 阻塞调度。
- **SndBuffer 把消息按 `payload_size` 切片**，用 `PacketPosition`（Only/First/Middle/Last）标记分片，同消息共享 `msg_number`；满了返回 `OutOfMemory`。
- **两个游标必须分开**：队首（`ack_data` 推进的已确认水位）与 `current_position`（`fetch_batch` 推进的已发水位）之间的差，正是「在途未确认」数据，同时服务于拥塞窗口与重传。
- **`update`/`insert` 用 `start_time` 当哨兵**表达「立刻执行」，`send()` 用 `update_snd_queue(false)` 入队、丢包/超时用 `update_snd_queue(true)` 抢占重排。

## 7. 下一步学习建议

本讲只讲了「队列与缓冲」这两个数据结构和它们的 worker。要让发送真正「靠谱」，还差三块拼图，正好是第 6 单元的内容：

- **u6-l1 socket 发送主流程**：深入 `next_data_packets` 的三条规则（重传优先、拥塞窗口/flow window 限流、probe 包不延迟）——本讲里 `fetch_batch`/`read_data` 的**调用方**就在那里。
- **u6-l2 接收数据与 ACK 生成**：看对端的 ACK 是怎么产生的，它如何驱动本讲的 `ack_data` 推进队首。
- **u6-l3 丢包检测与 NAK** + **u6-l4 ACK2 与 RTT**：理解 `snd_loss_list`（本讲重传读取的依据）是怎么被填进去的。

建议你下一讲带着一个问题去读：「`next_data_packets` 返回 `None` 让 socket 暂离调度堆之后，到底是哪一条来自对端的反馈把它重新唤醒？」这个答案会把本讲的 `UdtSndQueue` 和第 6 单元的可靠性机制彻底打通。

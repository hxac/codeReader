# socket 发送主流程：next_data_packets 与拥塞窗口

## 1. 本讲目标

本讲聚焦发送侧最核心的一个函数：`UdtSocket::next_data_packets`。它是发送队列 worker 调用 socket 时执行的「下一步发什么、什么时候发」决策点。读完本讲，你应当能够：

1. 说出 `next_data_packets` 的三段判定顺序：**先重传、再窗口限流、最后节奏补偿**。
2. 解释 `snd_loss_list.pop_after` 如何让**重传优先于新数据**。
3. 解释为什么拥塞窗口和 flow window 要取最小值，以及在「在途包数超过窗口」时为何返回 `None`。
4. 解释 `probe` 包（每 16 个出现一次）为何绕过 interpacket 节奏延迟。
5. 读懂 `interpacket_time_diff` 这个「时间信用」如何累积与消耗，从而把一个粗粒度的定时器驱动改造成平滑的包级 pacing。

本讲只讲「决定发什么」，不讲「拥塞窗口如何被调节」（那是 u7 的事），也不讲「包怎么打成字节流」（那是 u4-l2 的事）。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**直觉一：UDT 的发送是被一个全局定时器轮询驱动的，而不是「写一次立刻发一次」。** 用户调用 `UdtConnection::write` 最终只是把数据塞进发送缓冲 `SndBuffer`，真正把数据搬上网卡的是 multiplexer 上的发送 worker。worker 维护一个最小堆调度队列，每个 socket 在堆里有一个「下一次该被轮询的时刻」；到点了 worker 就调用该 socket 的 `next_data_packets`，问它「现在该发哪些包，下次什么时候再来问我」。这一问一答就是本讲的全部。

**直觉二：发送要同时满足「可靠」和「不超速」两个约束。** 可靠性意味着丢了的包要优先补发；不超速意味着在途未确认的包数不能超过网络当前允许的窗口。当这两个约束都满足时，才轮到「按节奏发新数据」。

**直觉三：「序列号」是循环的。** 本讲里频繁出现 `curr_snd_seq_number - last_ack_received` 这样的减法，它是循环空间上的带符号距离，返回 `i32`，结果可正可负（详见 u4-l4）。`>` 比较在循环语义下表示「在途包数确实超过了窗口」。这一点很重要：这里的减法是循环 `Sub`，不是普通整数减法。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `src/socket.rs` | 定义 `UdtSocket::next_data_packets`，本讲的绝对主角；也含 `update_snd_queue`、`cc_update`、`send` 等周边入口。 |
| `src/queue/snd_buffer.rs` | 发送缓冲 `SndBuffer`：`read_data`（按 seq 读旧块，用于重传）、`fetch_batch`（取新块）、`ack_data`（确认水位推进）。 |
| `src/queue/snd_queue.rs` | 发送调度队列 `UdtSndQueue`：worker 主循环——到点 pop、调用 `next_data_packets`、把返回的 `target_time` 重新入队。 |
| `src/state/socket_state.rs` | `SocketState` 簿记字段：`interpacket_interval`、`interpacket_time_diff`、`next_data_target_time`、`curr_snd_seq_number`、`last_ack_received`、`last_data_ack_processed`、`snd_loss_list`。 |
| `src/rate_control.rs` | `RateControl` 的只读 getter：`get_congestion_window_size`、`get_pkt_send_period`（如何被调节见 u7-l2）。 |
| `src/loss_list.rs` | `LossList::pop_after`：从丢失区间里取出「下一个要重传的 seq」。 |

## 4. 核心概念与源码讲解

### 4.1 next_data_packets：发送主流程的全貌与调度回路

#### 4.1.1 概念说明

`next_data_packets` 是一个**纯决策函数**：它不直接碰网卡，只回答 worker 两个问题——

1. 现在要发哪些数据包？（返回 `Vec<UdtDataPacket>`，可能为重传包，也可能是新数据）
2. 下次什么时候再来问我？（返回一个 `Instant` 作为「重新入队时刻」）

如果当前没有任何可发的东西（socket 已死、被窗口卡住、缓冲空、要发的块已过期），它返回 `Ok(None)`，**worker 就不会把这个 socket 重新入队**，于是该 socket 暂时退出调度，直到某个外部事件（新数据到来、收到 ACK、检测到丢包）通过 `update_snd_queue` 把它重新塞回队列。

整个决策遵循一个固定的优先级顺序，这也是本讲的三条主线：

```
1. socket 还活着吗？        否 → 返回 None
2. 有需要重传的丢失包吗？    有 → 读旧块，返回重传包（重传优先）
3. 在途包数超过窗口了吗？    超 → 返回 None（窗口限流）
4. 缓冲里还有新数据吗？      无 → 返回 None
   有 → 取一批新数据，计算下次发送时刻，返回
```

#### 4.1.2 核心流程

完整调用回路长这样：

```
send(用户数据)
  └─ SndBuffer::add_message    (切片入缓冲)
  └─ update_snd_queue(false)   (把 socket 重新塞进调度堆)
        ↓
snd_queue.worker 主循环
  ├─ 堆顶 node 的时刻到了？
  │     ├─ 没到 → sleep_until 或等 notify
  │     └─ 到了 → pop, 调 socket.next_data_packets()
  │                ├─ Some((packets, ts)) → insert(ts) 重新入队 + 发包
  │                └─ None                  → 不重新入队，socket 暂停调度
```

关键点：**返回 `None` 时 socket 不再被自动调度**。所以 `None` 是一个「暂停」信号，必须由 `update_snd_queue` 在条件变化时把它重新唤醒。

#### 4.1.3 源码精读

先看函数签名与「是否还活着」的入口检查：

[next_data_packets 入口与存活检查(src/socket.rs:226-236)](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L226-L236)

这段先取 `now = Instant::now()`，并初始化 `probe = false`。`is_alive()` 排除了 `Broken`/`Closing`/`Closed` 状态（见 u3-l2）；socket 一旦不存活，立即返回 `None`，worker 就不会再调度它。

再看 worker 是怎么消费这个返回值的，这是理解 `target_time` 用途的关键：

[worker 到点后调用 next_data_packets 并用返回的 ts 重新入队(src/queue/snd_queue.rs:92-100)](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_queue.rs#L92-L100)

注意第 95–97 行：只有当 `next_data_packets` 返回 `Some((packets, ts))` 时，才会 `self.insert(ts, ...)` 把 socket 以 `ts` 为下次时刻重新塞回堆，并把 packets 交给独立任务去真正发包（`send_data_packets`）。返回 `None` 时这一步被跳过——这就是「暂停调度」的实现。

#### 4.1.4 代码实践

**实践目标**：确认 `None` 会暂停调度，并找到唤醒它的入口。

**操作步骤**：

1. 打开 `src/socket.rs`，定位 `next_data_packets`（约 226 行）与 `update_snd_queue`（约 978 行）。
2. 用搜索找出所有 `update_snd_queue(` 的调用点，记下每一处的行号与上下文（ACK 处理、NAK 处理、check_timers、send）。

**需要观察的现象**：你会看到 ACK 处理调用 `update_snd_queue(false)`，而 NAK/超时调用 `update_snd_queue(true)`。

**预期结果**：

- 窗口限流返回 `None`（4.3 节）后，socket 暂停调度；下一条 ACK 到来时，[ACK 处理末尾的 update_snd_queue(false)](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L547-L555) 会把它重新入队，于是 `next_data_packets` 被再次调用，窗口检查可能这次就通过了。
- `update_snd_queue` 的实现在 [src/socket.rs:978-982](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L978-L982)，它把工作委托给 `mux.snd_queue.update`。

**待本地验证**：可在 `next_data_packets` 返回 `None` 的三个分支各加一行 `eprintln!`，跑 sender/receiver 观察哪条分支被频繁触发。

#### 4.1.5 小练习与答案

**练习 1**：为什么 worker 在 `next_data_packets` 返回 `None` 时不把 socket 重新入队？如果不这么做会怎样？

**参考答案**：返回 `None` 表示「此刻没有可发的东西」（被窗口卡住或缓冲空）。若仍重新入队，worker 会陷入「到点 → 问 → 没东西 → 立刻又到点」的死循环（忙等），白白占用 CPU。正确做法是暂停调度，等外部事件（ACK 推进水位、新数据到来、丢包检测）通过 `update_snd_queue` 把它唤醒。

**练习 2**：`next_data_packets` 返回的元组 `(Vec<UdtDataPacket>, Instant)` 里，第二个元素被 worker 用在哪一行？

**参考答案**：在 [snd_queue.rs:96](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_queue.rs#L95-L96) 的 `self.insert(ts, node.socket_id)`——它把 socket 以 `ts` 为「下次轮询时刻」重新塞回调度堆。

---

### 4.2 重传优先：snd_loss_list 与 read_data

#### 4.2.1 概念说明

可靠性要求：一旦发现某个包丢了（通过收到的 NAK 或超时检测），它必须比新数据更早被重发。tokio-udt 用一个「发送侧丢失链表」`snd_loss_list`（`LossList` 类型，内部是 `BTreeMap`，存的是丢失区间）来记录所有尚未重传的丢失 seq。

`next_data_packets` 在做任何「发新数据」的判断之前，先问一句：「`snd_loss_list` 里有东西吗？」有就先重传，没有才考虑新数据。这就是**重传优先**。

#### 4.2.2 核心流程

```
to_resend = snd_loss_list.pop_after(last_data_ack_processed)
            // 取出 >= 已确认水位 的下一个丢失 seq
  ├─ Some(seq):
  │     offset = seq - last_data_ack_processed   // 循环 Sub，结果为索引
  │     if offset < 0 → 异常，返回 None
  │     block = SndBuffer::read_data(offset, seq, ...)  // 按索引读旧块
  │       ├─ 块已过期(TTL) → 发 MsgDropRequest，清 loss 区间，返回 None
  │       ├─ 索引越界       → 返回 None
  │       └─ 正常           → 返回 [该重传包]
  └─ None:
        → 进入「发新数据」分支（4.3 节）
```

两个关键概念：

- **`last_data_ack_processed`**：已确认水位。它以下的块已经从 `SndBuffer` 里 `ack_data` 弹出，所以当前缓冲里 `buffer[0]` 恰好对应 `seq == last_data_ack_processed`。
- **`offset = seq - last_data_ack_processed`**：用循环减法把目标 seq 换算成缓冲索引。这样就能从仍保留在缓冲里的旧块中，按索引直接读出要重传的 payload，而不需要按内容查找。

#### 4.2.3 源码精读

先看取重传目标的那一小段：

[取出重传目标 seq 并换算成缓冲 offset(src/socket.rs:240-253)](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L240-L253)

这段同时做了一件容易被忽略的事：第 242–246 行计算 `data_delay = now - next_data_target_time`（这次比上次计划晚了多久）并累加进 `interpacket_time_diff`。这正是节奏补偿信用的入口——重传同样会消耗这个信用（详见 4.3 节）。第 250–252 行调用 [LossList::pop_after(src/loss_list.rs:121-141)](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/loss_list.rs#L121-L141) 取出下一个要重传的 seq。

再看拿到 seq 后的处理：

[重传分支：读旧块，过期则发 MsgDropRequest(src/socket.rs:255-294)](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L255-L294)

第 264–269 行调用 [SndBuffer::read_data(src/queue/snd_buffer.rs:112-142)](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_buffer.rs#L112-L142) 按索引读旧块。注意 `read_data` 的返回值是 `Result<UdtDataPacket, (MsgNumber, usize)>`：成功返回重组好的重传包；失败时返回 `(msg_number, msg_len)`，含义见下。

`read_data` 的内部三分支值得单独看：

[read_data：过期块 / 正常块 / 越界(src/queue/snd_buffer.rs:112-142)](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_buffer.rs#L112-L142)

- 第 120–135 行：块已过 TTL（[has_expired](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_buffer.rs#L24-L29)）。此时不再重传整条消息，而是向前扫描出整条消息的长度 `msg_len`，返回 `Err((msg_number, msg_len))`，让上层发一个 `MsgDropRequest` 通知对端丢弃整条消息（流式模式下 `ttl` 一般是 `None`，所以这条分支主要服务于 messaging 模式的带 TTL 消息）。
- 第 137 行：正常块，调用 [as_data_packet](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_buffer.rs#L31-L48) 给旧 payload 重新打上当前 seq/timestamp 包头，得到重传包。
- 第 140 行：索引越界（缓冲里没有这个块了），返回 `Err((zero, 0))`，上层据此返回 `None`。

回到上层第 271–291 行的过期处理：发完 `MsgDropRequest` 后，第 286 行用 [snd_loss_list.remove_all](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L286-L289) 清掉这段丢失区间，并把 `curr_snd_seq_number` 推进到过期消息末尾，最后返回 `None`。

#### 4.2.4 代码实践

**实践目标**：验证「重传包读的是旧 payload、打的是新包头」。

**操作步骤**：

1. 读 [SndBufferBlock 的字段(src/queue/snd_buffer.rs:13-21)](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_buffer.rs#L13-L21)：注意它存的是 `data: Bytes`、`msg_number`、`origin_time`、`ttl`、`position`，**没有存 seq_number**。
2. 读 [as_data_packet(src/queue/snd_buffer.rs:31-48)](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_buffer.rs#L31-L48)，看 seq_number 是从参数传入的（即调用方算出的 `seq`），而不是从块里取的。

**需要观察的现象**：同一个 `SndBufferBlock` 在初次发送和重传时，data/msg_number/position 都不变，但 seq_number 和 timestamp 会不同。

**预期结果**：这解释了 UDT 的一个重要特性——**重传包复用原始 payload，但携带重新计算的 seq 与时间戳**。因此接收侧不能用 seq 简单去重，而要靠 `RcvBuffer` 的 `entry().or_insert()`（见 u5-l2）。

**待本地验证**：在 `as_data_packet` 里临时打印 `(self.position, seq_number)`，制造一次丢包（用 `tc`/防火墙随机丢包或人为触发 NAK），观察同一块被打印两次、seq 递增。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `offset = seq - last_data_ack_processed` 能直接当作 `SndBuffer` 的数组索引？

**参考答案**：因为 `ack_data` 一旦确认水位推进，就会从 `buffer` 头部 `pop_front`（见 [ack_data](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_buffer.rs#L104-L110)），并同步推进 `current_position`。所以保留在缓冲里的第一个块 `buffer[0]` 恰好对应 `seq == last_data_ack_processed`，目标 seq 相对它的循环偏移量就是数组下标。

**练习 2**：`read_data` 返回 `Err((MsgNumber, usize))` 中，`usize` 表示什么？上层据此做了什么？

**参考答案**：`usize` 是过期消息**包含的块数**（向前扫描同 `msg_number` 的连续块得到）。上层据此构造一个覆盖 `[seq, seq+msg_len-1]` 区间的 `MsgDropRequest` 发给对端，并从 `snd_loss_list` 移除该区间、推进 `curr_snd_seq_number`，相当于「放弃重传这条过期消息」。

---

### 4.3 拥塞窗口限流、probe 与 interpacket 节奏补偿

#### 4.3.1 概念说明

当没有重传任务时，函数进入「发新数据」分支。这里有两层独立的速度约束，叠加后才决定「能不能发、什么时候发」：

1. **窗口约束（能不能发）**：在途未确认的包数 \( \text{inflight} = \text{curr\_snd\_seq\_number} - \text{last\_ack\_received} \) 不得超过窗口。窗口取两个值的**最小值**：
   - `congestion_window_size`：拥塞控制器（`RateControl`）按网络反馈动态调出的窗口（u7-l2 详讲）。
   - `flow_window_size`：接收方在握手/ACK 里 advertised 的接收能力（流控窗口）。

   \[ \text{window} = \min(\text{flow\_window\_size},\ \text{congestion\_window\_size}) \]

   取最小值是因为：发送速率既要尊重对端「别发太快我收不过来」（流控），也要尊重网络「别发太多会拥塞丢包」（拥塞控制），任何一方告急都必须限速。

2. **节奏约束（什么时候发）**：即使窗口允许，包也不能一股脑全发出去。`RateControl` 还给一个 `pkt_send_period`（相邻包间隔），要求包与包之间至少间隔这么久，这就是 **packet pacing**。

probe 包是这条节奏规则的一个例外：当 `curr_snd_seq_number` 是 16 的倍数时，这一批被标记为 `probe`，**绕过节奏延迟**，立刻发送。原因会在 4.3.3 解释（与接收侧的带宽估计配对）。

#### 4.3.2 核心流程

发新数据分支的伪代码：

```
window = min(flow_window_size, congestion_window_size)
inflight = curr_snd_seq_number - last_ack_received   // 循环 Sub
if inflight > window:
    next_data_target_time = now      // 重置，不积累信用
    interpacket_time_diff = ZERO
    return None                      // 窗口限流，暂停调度

packets = SndBuffer::fetch_batch(curr_snd_seq_number + 1, ...)
if packets 为空:
    next_data_target_time = now      // 缓冲空，暂停调度
    interpacket_time_diff = ZERO
    return None

curr_snd_seq_number += packets.len()
if curr_snd_seq_number % 16 == 0:
    probe = true

if probe:
    return Some((packets, now))      // probe 立即发，不延迟、不消耗信用

// —— 以下为节奏补偿 ——
interval = interpacket_interval * packets.len()      // 这批「应」占用的时间
if interpacket_time_diff >= interval:
    interpacket_time_diff -= interval
    target_time = now                // 有足够信用，立即发
else:
    target_time = now + interval - interpacket_time_diff
    interpacket_time_diff = ZERO     // 信用不足，等够再发
return Some((packets, target_time))
```

节奏补偿的关键是 `interpacket_time_diff` 这个**时间信用**（time credit）。把它理解成一个「可以提前透支的时间预算」：

- **累积**：每次进入函数时，`data_delay = now - next_data_target_time`（这次比上次计划晚了多少）被加进信用。如果调度器因为忙而来不及按时轮询，累积的「欠债时间」就变成信用——可以用来让后续批次立即发送，形成**追赶突发**（catch-up burst）。
- **消耗**：每发一批 N 个包，理应占用 \( \text{interval} = \text{interpacket\_interval} \times N \) 的时间。若信用够，扣掉 interval 立即发；若不够，就等到 `now + interval - 信用`，并把信用清零。

这样，即便底层是一个粗粒度的、靠 `sleep_until`/`notify` 驱动的定时器（见 u8-l4 的 timerfd），也能逼近「每个包精确间隔 `interpacket_interval`」的理想 pacing，同时允许调度抖动时适度突发追赶。

#### 4.3.3 源码精读

先看窗口取最小值与窗口限流：

[窗口 = min(flow, congestion)，超窗返回 None(src/socket.rs:295-310)](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L295-L310)

第 296–300 行读 [get_congestion_window_size(src/rate_control.rs:83-85)](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/rate_control.rs#L83-L85)；第 301–304 行取 `min(flow_window_size, congestion_window_size)`；第 306 行的减法是循环 `Sub`（返回 `i32`），`>` 在循环语义下表示「在途包数确实超过窗口」。注意第 307–309 行：返回 `None` 前把 `next_data_target_time` 设成 `now`、`interpacket_time_diff` 清零——**故意不积累信用**，避免窗口打开后一次性突发过量。

接着是取新数据与 probe 标记：

[fetch_batch 取新数据并标记 probe(src/socket.rs:311-334)](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L311-L334)

第 311 行调用 [SndBuffer::fetch_batch(src/queue/snd_buffer.rs:144-162)](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_buffer.rs#L144-L162)，从 `current_position`（已发水位）往后取最多 `FETCH_BATCH_SIZE=100` 个块（[常量定义](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_buffer.rs#L10)），并把 `current_position` 前移。第 317–322 行推进 `curr_snd_seq_number` 并同步给 `RateControl`（拥塞算法要用它判断慢启动，见 u7-l2）。第 323–325 行：**若新 seq 是 16 的倍数，标记 probe**。

> 关于 probe 为何绕过节奏：接收侧的带宽估计（u7-l1）依赖「包对」(packet pair) 测量——它观察 seq `%16==0` 和 `%16==1` 两个相邻 probe 包的到达间隔来估算链路带宽。为了让这个间隔反映真实瓶颈而非被发送方人为拉大，probe 批次必须**不被 pacing 延迟**，尽快背靠背发出。第 338–340 行正是为此直接返回 `now`。

最后是节奏补偿计算：

[节奏补偿：信用累积与消耗(src/socket.rs:342-353)](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L342-L353)

对照 4.3.2 的伪代码逐行读即可。`interpacket_interval` 的来源是 [cc_update(src/socket.rs:882-885)](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L882-L885)：每次 `check_timers` 时从 `RateControl::get_pkt_send_period`（[rate_control.rs:79-81](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/rate_control.rs#L79-L81)）刷新。默认初值是 `1µs`（见 [SocketState::new](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/state/socket_state.rs#L51-L52)）。

#### 4.3.4 代码实践

**实践目标**：在 `next_data_packets` 里精确标注「重传优先」「窗口限流返回 None」「probe 直接返回 now」三段逻辑，并解释 `interpacket_time_diff` 的累积与补偿。

**操作步骤**：

1. 打开 `src/socket.rs`，按下面的映射标注三段逻辑：
   - **重传优先**：[第 255–294 行](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L255-L294)——`to_resend` 是 `Some` 时走重传，连注释 `// Loss retransmission has priority`（第 257 行）都在明示优先级。
   - **窗口限流返回 None**：[第 306–310 行](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L306-L310)——`inflight > window` 直接 `return Ok(None)`。
   - **probe 直接返回 now**：[第 323–325 行](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L323-L325)（标记）+ [第 338–340 行](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L338-L340)（`return Ok(Some((packets, now)))`，跳过第 342–353 的节奏计算）。
2. 在一张纸上画一条时间轴，模拟两次连续调用：
   - 第 1 次：`interpacket_time_diff = 0`，发 2 个包，`interval = 2µs`，则 `target = now + 2µs`，信用清零。
   - 假设 worker 因忙直到 `now + 5µs` 才回来：`data_delay = 5µs`，信用变为 `5µs`。第 2 次发 2 个包：信用 `5µs ≥ interval 2µs`，于是 `target = now`（立即发），信用扣到 `3µs`。

**需要观察的现象**：第 2 次本应等到 `now + 2µs`，但因为前一次「迟到」积累了信用，它被允许立即发送。

**预期结果**：这正是「调度抖动时的追赶突发」。若连续多批都迟到，信用会一直累积，允许一次较大突发把欠下的发送量补回来；一旦调度恢复准时，信用耗尽后又会回到每批等 `interval` 的稳态。

**待本地验证**：上述时间轴是推演，未运行。可在第 245、345、348 行各打印 `interpacket_time_diff`，跑 `udt_sender` 观察其随时间的变化曲线。

#### 4.3.5 小练习与答案

**练习 1**：为什么窗口要取 `flow_window_size` 和 `congestion_window_size` 的**最小值**，而不是其中某一个？

**参考答案**：它们代表两类不同的上限——`flow_window_size` 是接收方 advertised 的接收能力（流控，防止淹没对端缓冲），`congestion_window_size` 是发送方按网络拥塞反馈调出的窗口（防丢包）。发送速率必须同时满足这两个约束，因此取最小值；任何一个变小都会立即收紧发送窗口。

**练习 2**：probe 批次为什么跳过第 342–353 行的节奏补偿，直接返回 `now`？

**参考答案**：probe 包（`seq % 16 == 0`）与下一个包（`seq % 16 == 1`）构成接收侧带宽估计用的「包对」。接收方靠测量这两个包的到达间隔来推断链路带宽（u7-l1）。若发送方给 probe 加上 pacing 延迟，会人为拉大这个间隔，导致带宽估计失真。所以 probe 必须「尽快背靠背发出」，因而绕过节奏延迟，也不消耗 `interpacket_time_diff` 信用。

**练习 3**：窗口限流返回 `None` 时，为什么要把 `interpacket_time_diff` 清零、`next_data_target_time` 设成 `now`？

**参考答案**：返回 `None` 后 socket 暂停调度，直到下一条 ACK 到来（由 `update_snd_queue(false)` 唤醒）。若不清零信用，这段等待时间会让 `data_delay` 变成很大的正值、累积成巨量信用，导致窗口一打开就一次性突发过量。清零信用、并把目标时间钉在 `now`，保证窗口打开后是从零开始按节奏平滑发送，而非补偿性爆发。

## 5. 综合实践

把本讲三条主线串起来：用 `udt_sender`/`udt_receiver` 跑一次传输，结合源码阅读，画一张「单次发送决策」的状态图。

1. **运行**：按 u1-l2 的方式起一对收发端，让它跑出稳定吞吐。
2. **阅读**：完整通读 [next_data_packets(src/socket.rs:226-354)](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L226-L354) 一次，确认它的四个出口：
   - 出口 A（226–236）：socket 不存活 → `None`。
   - 出口 B（255–294）：重传 → 返回重传包（或过期发 Drop 后 `None`）。
   - 出口 C（295–334）：窗口/缓冲 → `None`，或取新数据。
   - 出口 D（338–353）：probe 立即发，或按节奏算 `target_time`。
3. **画图**：以「worker 到点调用 `next_data_packets`」为入口，画出上述四个出口的分支树，并在每个出口旁标注「worker 是否重新入队」与「信用如何变化」。
4. **思考题**：在回环（localhost）环境下，`congestion_window_size` 几乎不收缩（见 u7-l2），`flow_window_size` 由对端 advertised 决定。请推断：回环下 `next_data_packets` 最常走哪个出口？发送节奏主要由 `interpacket_interval`（≈1µs）还是由窗口决定？（提示：稳态下 `inflight` 会逼近窗口，所以两者交替生效——窗口允许就发一批，发完按 `interval` 排队，ACK 回来推进水位后又允许下一批。）

> 说明：第 4 步为源码推理，标注「待本地验证」的部分请通过加日志确认。

## 6. 本讲小结

- `next_data_packets` 是发送侧的纯决策函数，只回答「发什么 + 下次何时再问」，返回 `None` 会让 socket 暂停调度，直到 `update_snd_queue` 把它重新唤醒。
- 决策顺序固定为三步：**重传优先 → 窗口限流 → 按节奏发新数据**，对应「可靠 / 不超量 / 平滑」三个目标。
- 重传优先由 `snd_loss_list.pop_after` 取出丢失 seq，用循环减法 `seq - last_data_ack_processed` 换算成 `SndBuffer` 索引，经 `read_data` 读旧块；过期块则改发 `MsgDropRequest`。
- 窗口取 `min(flow_window_size, congestion_window_size)`，当在途包数 `curr_snd_seq_number - last_ack_received` 超窗时返回 `None`。
- `interpacket_time_diff` 是一个「时间信用」：调度迟到时累积（允许追赶突发），每批发送时按 `interpacket_interval × N` 消耗，从而把粗粒度定时器逼近精确的包级 pacing。
- probe 包（`seq % 16 == 0`）跳过节奏补偿直接立即发送，为接收侧的「包对」带宽估计服务。

## 7. 下一步学习建议

本讲只讲了「窗口与节奏怎么用」，但没讲「窗口与 `interpacket_interval` 是怎么被调出来的」。接下来的学习路径：

- **u6-l2 接收数据与 ACK 生成**：看 `last_ack_received` 是怎么被接收方的 ACK 推进的——这是本讲窗口限流能「解除」的根本原因。
- **u7-l1 流量与带宽估计 UdtFlow**：理解 probe 包到了接收侧之后，`arrival_window`/`probe_window` 是如何把包对间隔换算成带宽与 `flow_window_size` 的。
- **u7-l2 速率控制 RateControl**：理解 `congestion_window_size` 与 `pkt_send_period`（即本讲的 `interpacket_interval`）在慢启动与 AIMD 下是如何动态变化的，把本讲「只读 getter」补成完整闭环。
- **u8-l4 平台快路径**：理解 worker 的 `sleep_until` 在 Linux 上为何用 `timerfd` 而非 `tokio::time::sleep`，这对本讲「按 `target_time` 精准唤醒」至关重要。

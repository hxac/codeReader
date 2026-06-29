# 接收数据与 ACK 生成

## 1. 本讲目标

在上一讲（u5-l2）里，我们已经看清「数据包怎么进到 `RcvBuffer`」这一段——收包 worker 把 UDP 包解复用、按 `dest_socket_id` 分发到 `UdtSocket::process_packet`，再由 `process_data` 把数据包塞进 `BTreeMap`。但**收下来只是第一步**：UDP 不可靠，包会丢、会乱序、会重复。要让上层看到一个连续可靠的字节流，接收侧必须做三件事：

1. **检测丢包**：发现「期望的下一个序号」和「实际收到的序号」之间有缺口。
2. **立即请求重传**：一旦发现缺口，马上发一个 NAK 控制包给对端。
3. **周期性确认**：不断发 ACK 告诉对端「我连续收到了哪里」，并顺带把网络质量（RTT、带宽、缓冲余量）反馈回去，驱动发送方的拥塞控制。

本讲精读 `process_data`（负责前两件事）与 `send_ack`（负责第三件事），并讲清 `ack_window` 这个「记账本」为什么是 ACK 机制不可或缺的一环。学完后你应当能够：

- 准确指出 `process_data` 中「过期包丢弃」「丢包检测并发 NAK」「推进接收水位」三段代码的位置与作用。
- 说清楚 `send_ack` 何时走轻量 ACK（light）、何时走完整 ACK（full），以及完整 ACK 携带了哪些拥塞反馈信息。
- 解释 `ack_window.store` 为什么必须和 ACK 序号递增配合，它为谁服务。

## 2. 前置知识

阅读本讲前，请确认你已掌握以下概念（前序讲义已建立）：

- **UDT 包的两大类**（u4-l1/u4-l3）：数据包（`UdtDataPacket`）承载用户字节，控制包（`UdtControlPacket`）承载信令。ACK、NAK、ACK2 都是控制包，靠首比特区分。
- **循环序列号**（u4-l4）：`SeqNumber` 是 31 位循环空间上的序号，两个序号相减返回 `i32`（带符号的环上最短距离），而不是一个新的 `SeqNumber`。本讲里大量的 `seq - state.xxx` 判断（如 `> 1`、`< 0`）都依赖这个语义。
- **`RcvBuffer` 的两个游标**（u5-l2）：`next_to_read`（读指针）与 `next_to_ack`（确认水位）。`next_to_read..next_to_ack` 之间的包才是「可读窗口」——也就是说，**数据要等到被 ACK 推进水位后，才真正对上层可读**。这一点本讲会再次用到。
- **`SocketState` 是收发簿记容器**（u3-l2）：`curr_rcv_seq_number`、`last_sent_ack`、`last_ack2_received`、`rcv_loss_list`、`ack_window` 等游标都住在 `SocketState` 里，受一把 `Mutex` 保护。

本讲完全聚焦于「接收侧如何反馈发送侧」。发送侧收到这些 ACK/NAK 之后怎么处理（推进发送水位、触发重传、更新 RTT、调节窗口），属于 u6-l1 与 u6-l4 的范畴，本讲只在必要时点一句接口位置，不展开。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用到什么 |
|------|------|--------------|
| `src/socket.rs` | 连接核心，收发决策都在这里 | `process_data`、`send_ack`、以及调用它们的 `check_timers` |
| `src/queue/rcv_buffer.rs` | 接收缓冲，乱序重组 + 按序读取 | `insert`、`get_available_buf_size`、`ack_data`、`has_data_to_read` |
| `src/ack_window.rs` | ACK 序号 → 发送时刻的记账本 | `store`、`get` |
| `src/control_packet.rs` | 控制包的构造与字段定义 | `new_ack`、`new_nak`、`AckInfo`、`AckOptionalInfo` |
| `src/state/socket_state.rs` | 收发簿记字段集合 | 接收相关游标字段的定义与初值 |
| `src/loss_list.rs` | 丢失区间容器 | `rcv_loss_list` 的 `insert` / `peek_after` |
| `src/flow.rs` | 到达速率 / 带宽估计 | `on_pkt_arrival`、`get_pkt_rcv_speed`、`get_bandwidth` |

记忆口诀：**`socket.rs` 出决策、`rcv_buffer.rs` 存数据、`ack_window.rs` 记时刻、`control_packet.rs` 造包、`socket_state.rs` 当账本**。

## 4. 核心概念与源码讲解

### 4.1 接收侧可靠性：三件事与一次「水位推进」

#### 4.1.1 概念说明

可靠的接收侧本质上是在维护一条「**我连续收到了哪里**」的进度线，并围绕这条线做三件事。这条进度线由两个关键游标刻画：

- `curr_rcv_seq_number`：当前**已连续收到的最大序号**。下一个期望收到的就是它 `+1`。
- `last_sent_ack`：上一次在 ACK 里**对外报告过的水位**。它和 `curr_rcv_seq_number` 之间的差，就是「收下了但还没确认」的量。

围绕这两个游标，接收侧对每一个到达的数据包做分诊：

1. **这个包是不是来得太晚？**（序号已经在已确认水位之下 → 重复/迟到，丢弃）
2. **这个包前面有没有缺口？**（序号比期望大超过 1 → 中间丢了包 → 记进 `rcv_loss_list` 并立刻发 NAK）
3. **把这个包的序号并入进度线。**（推进 `curr_rcv_seq_number`，或在补齐缺口时把对应项从 `rcv_loss_list` 移除）

这三步正好对应 `process_data` 的三段，也是本讲第一节要精读的核心。至于「周期性地把进度线通过 ACK 报告出去」，那是 `send_ack` 的事，留到 4.2。

> 名词解释：
> - **缺口（gap）**：期望收到 `N`，却收到了 `N+3`，那么 `N`、`N+1`、`N+2` 是缺口。
> - **水位（watermark）**：「我连续收到了哪里」的边界值，是个排他的上界（即「收到此处之前、不含此处」）。

#### 4.1.2 核心流程

单个数据包到达后的分诊流程（伪代码）：

```
process_data(packet):
    state.last_rsp_time = now          # 记录"对端还活着"，喂给 EXP 超时判定
    state.pkt_count += 1               # 喂给 ACK/light-ACK 节拍器
    flow.on_pkt_arrival(now)           # 喂给到达速率估计（u7-l1）

    seq  = packet.seq_number
    base = state.last_sent_ack
    offset = seq - base                # 循环带符号差（i32）

    if offset < 0:                     # (A) 过期包：序号在已确认水位之下
        return                         #     → 丢弃，什么都不做

    if rcv_buffer 剩余空间 < offset:    # (B) 没地方放：序号太超前
        return                         #     → 丢弃（等对端重传/降速）

    rcv_buffer.insert(packet)          # 存进乱序缓冲

    if seq - curr_rcv_seq_number > 1:  # (C) 检测到缺口
        rcv_loss_list.insert(curr+1, seq-1)
        立即 send NAK(缺口区间)        # 不等定时器，马上催重传

    if 是消息的最后一个分片(payload 不满):
        state.next_ack_time = now      # 提前触发一次 ACK

    if seq - curr_rcv_seq_number > 0:  # (D) 顺序到达，推进进度线
        curr_rcv_seq_number = seq
    else:                              # 补上了之前的缺口
        rcv_loss_list.remove(seq)
```

注意三个「丢弃点」(A)(B)(D 的 else 分支) 的区别：

- (A) 是「已经确认过的旧包」，纯重复，直接丢。
- (B) 是「序号太超前、缓冲装不下」，本实现选择丢掉这个包——它会进入缺口、被 NAK 重新请求。这与「减少缓冲压力」的取舍有关。
- (C) 之后那段，`seq` 落在缺口之内时（`seq - curr_rcv_seq_number <= 0`，即 `seq <= curr_rcv_seq_number`），说明它是某个之前缺失、现在补到的包，于是从 `rcv_loss_list` 里移除，表示「这个洞补上了」。

#### 4.1.3 源码精读

整个 `process_data` 函数：

[src/socket.rs:675-757](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L675-L757) —— 接收一个数据包，完成「记账 + 丢包检测 + 即时 NAK + 推进水位」。

函数开头先更新两个全局计数器，它们分别是 EXP 超时判定和 ACK 节拍器的输入：

[src/socket.rs:676-681](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L676-L681) —— `last_rsp_time = now` 表示「刚收到对端响应，连接没死」；`pkt_count += 1` 累计自上次 ACK 以来的收包数。

接着把到达事件喂给 `UdtFlow` 做到达速率与带宽估计（详见 u7-l1），这里只关注 `seq % 16` 的探针包成对测量：

[src/socket.rs:685-694](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L685-L694) —— `PROBE_MODULO == 16`，序号能被 16 整除的包记 `probe1` 时刻，余 1 的包记 `probe2` 时刻，两者之差用于估计链路带宽。

**(A) 过期包丢弃**——这是本讲实践任务要求标注的第一段：

[src/socket.rs:698-702](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L698-L702) —— `offset = seq - last_sent_ack`，若 `offset < 0` 说明这个包的序号在「已确认水位」之下，是迟到/重复包，直接 `return Ok(())` 丢弃。

为什么用 `last_sent_ack` 而不是 `curr_rcv_seq_number` 作基准？因为「已确认」意味着「已经通过 ACK 告诉过对端我收到了」，对这种包再处理纯属浪费；用确认水位当截断线，能稳稳挡住重复包。

**(B) 缓冲空间检查 + 入队**：

[src/socket.rs:704-717](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L704-L717) —— 先看 `RcvBuffer` 还剩多少格子（`get_available_buf_size`），若放不下这个序号对应的超前量就丢弃；否则 `rcv_buffer.insert(packet)` 入队。`insert` 内部用 `entry().or_insert()` 保证重复序号不会覆盖。

对应缓冲侧的实现：

[src/queue/rcv_buffer.rs:24-31](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_buffer.rs#L24-L31) —— `get_available_buf_size = max_size - 已存包数`；`insert` 用 `BTreeMap` 的 `entry().or_insert()` 去重。

**(C) 丢包检测 + 立即 NAK**——这是实践任务要求标注的第二段，也是本节重点：

[src/socket.rs:719-742](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L719-L742) —— 当 `seq_number - curr_rcv_seq_number > 1` 时判定中间有缺口：把 `(curr_rcv_seq_number + 1, seq_number - 1)` 记进 `rcv_loss_list`，并**立即**构造并发送一个 NAK，不等待任何定时器。

NAK 携带的 `loss_info` 是一个 `Vec<u32>`，编码方式很关键（也是 u6-l3 的核心，这里先看产生方）：

[src/socket.rs:729-738](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L729-L738) —— 缺口只有一个序号时，`loss_info = [seq-1]`；缺口是一段区间时，`loss_info = [start | 0x8000_0000, end]`，用最高位 `0x8000_0000` 标记「后面还跟了一个结束序号」。

把最高位当「这是区间起点、下一项是终点」的标记位，单点丢失则不带这个标记位、只发一个 u32。这套编码在发送侧 `process_ctrl` 的 NAK 分支（u6-l3）会被反向解析。

**(D) 推进进度线**——实践任务要求标注的第三段：

[src/socket.rs:748-754](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L748-L754) —— 若 `seq - curr_rcv_seq_number > 0`，说明这个包是新的最大序号，推进 `curr_rcv_seq_number`；否则（`seq <= curr_rcv_seq_number`）说明它填补了之前的某个缺口，于是把它从 `rcv_loss_list` 移除。

注意中间还有一个小优化：消息的最后一个分片（payload 不满 MSS）会提前触发一次 ACK，避免对端空等：

[src/socket.rs:744-746](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L744-L746) —— `payload_len < max_payload_size` 时把 `next_ack_time` 拉到 `now`，让下一次 `check_timers` 立刻发 ACK。

#### 4.1.4 代码实践

**目标**：在源码层面把 `process_data` 的三段「丢弃 / NAK / 推进」对号入座，并验证你对循环减法的理解。

**步骤**：

1. 打开 [src/socket.rs:675-757](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L675-L757)。
2. 在三段关键代码处用注释或笔记标注：
   - 「offset<0 丢弃」→ 第 698–702 行。
   - 「插入 loss_list 并发 NAK」→ 第 719–742 行。
   - 「更新 curr_rcv_seq_number」→ 第 748–754 行。
3. 做一次「循环减法」推演：假设 `last_sent_ack = 5`（已确认水位），`curr_rcv_seq_number = 5`，依次到达 `seq = 5, 6, 9`。
   - `seq=5`：`offset = 5 - 5 = 0`（不过期），`9` 还没到，无缺口，`9-5>0` 不成立（实际 `5-5=0`，不推进，落到 else 把 5 从 loss_list 移除——但 loss_list 此时是空的）。
   - `seq=6`：`6-5>0` 成立，`curr_rcv_seq_number = 6`。
   - `seq=9`：`9-6>1` 成立 → 缺口 `(7,8)` 进 loss_list，发 NAK `[7 | 0x80000000, 8]`；随后 `9-6>0` 成立，`curr_rcv_seq_number = 9`。

**需要观察的现象 / 预期结果**：你应当能口算出 NAK 的 `loss_info` 字节内容，并理解「为什么 seq=5 这一步不会推进 curr_rcv_seq_number」。若推演结果与代码逻辑一致，说明你掌握了「环上带符号差」与「缺口编码」两个要点。

> 说明：本实践是源码阅读型，不修改源码、不要求运行；若想运行观察，可用 u1-l2 的 `udt_sender`/`udt_receiver` 配合人工丢包（如用 `tc`/防火墙规则丢一部分 UDP 包）观察 NAK 触发，但构造稳定丢包场景较繁琐，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`process_data` 里判断「过期包」用的是 `seq - last_sent_ack < 0`，为什么不用 `seq - curr_rcv_seq_number < 0`？

**参考答案**：`curr_rcv_seq_number` 是「连续收到的最大序号」，但缺口内的包（序号比它小）恰恰是合法的、需要补的包，不能当过期包丢掉；只有低于「已确认水位 `last_sent_ack`」的包才是真正的迟到/重复包。用确认水位当截断线，既挡住了重复包，又不会误杀缺口补传。

**练习 2**：若一次到达的 `seq` 让缺口恰好是单点（`curr_rcv_seq_number + 1 == seq - 1`），NAK 的 `loss_info` 是什么？若是区间呢？

**参考答案**：单点时 `loss_info = [(seq-1).number()]`，一个 u32、不带最高位标记；区间时 `loss_info = [(curr+1).number() | 0x8000_0000, (seq-1).number()]`，两个 u32，第一个的最高位 `0x8000_0000` 表示「下一项是区间终点」。对应代码在 [src/socket.rs:729-738](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L729-L738)。

---

### 4.2 send_ack：轻量 ACK 与完整 ACK 的两条路径

#### 4.2.1 概念说明

NAK 是「出了事才喊」（事件驱动、即时），ACK 则是「没事也定时汇报」（时间驱动、周期性）。但每收一个包就发一个带全部拥塞反馈的 ACK 太贵，于是 UDT 把 ACK 分成两档：

- **完整 ACK（full ACK）**：携带 ACK 序号 + `AckOptionalInfo`（RTT、RTT 方差、可用缓冲、包到达速率、链路带宽）。它既推进发送方的水位，又把网络质量反馈给拥塞控制，还**期望对端回一个 ACK2** 以便测量 RTT。代价大、频率低。
- **轻量 ACK（light ACK）**：只带一个「我连续收到哪里」的水位，没有 ACK 序号、没有可选信息、不期望 ACK2。代价小、频率高（每 64 个包一次）。

> 为什么 light ACK 不带 ACK 序号？因为 ACK 序号的作用是让对端回 ACK2、从而测量 RTT。light ACK 不需要这条 RTT 测量链路，省掉序号就能让对端的 `process_ctrl` 走一条更短的分支（见 [src/socket.rs:493-503](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L493-L503) 的 `None` 分支）。

两条路径由 `send_ack(light: bool)` 的布尔参数选择，而**何时调用、传 true 还是 false**，由 `check_timers` 决定（4.3 节会看调用方）。

#### 4.2.2 核心流程

`send_ack` 的流程可以拆成四段：

```
send_ack(light):
    # 第 1 段：算"这次确认到哪个序号"
    seq = rcv_loss_list.peek_after(curr_rcv_seq_number + 1)
          ?? curr_rcv_seq_number + 1
    if seq == last_ack2_received: return      # 对端已确认收到过这个水位的 ACK，不必重发

    # 第 2 段：light 分支——只发水位，立即返回
    if light:
        send Ack(next_seq=seq, ack_number=0, info=None)
        return

    # 第 3 段：full 分支——先决定"要不要发、要不要推进本地缓冲水位"
    to_ack = seq - last_sent_ack
    match to_ack:
        > 0:  rcv_buffer.ack_data(seq)        # 推进 next_to_ack → 数据变可读
              last_sent_ack = seq
              rcv_notify.notify_waiters()     # 唤醒等待读取的上层
        == 0: if 距上次发 ACK < rtt + 4*rtt_var: return   # 节流：重复 ACK 发太密就压一压
        < 0:  return                          # 异常/回退，不发

    # 第 4 段：构造 full ACK（带可选信息）并发送，然后记账
    if last_sent_ack > last_ack2_received:
        last_ack_seq_number += 1              # ACK 序号自增
        info = AckOptionalInfo{ rtt, rtt_var, avail_buf, ... }
        if 距上次带宽测量 > SYN_INTERVAL:     # 10ms 才测一次，省 CPU
            info.pack_recv_rate = flow.get_pkt_rcv_speed()
            info.link_capacity  = flow.get_bandwidth()
        send Ack(seq=last_sent_ack, ack_number=last_ack_seq_number, info=Some(info))
        ack_window.store(last_sent_ack, last_ack_seq_number)   # 为 ACK2/RTT 埋点
```

有几个设计要点值得记住：

1. **确认水位受缺口约束**：`peek_after` 找出「下一个已知缺口」的起点，如果有缺口，`seq` 就停在缺口前——不能谎报「我收到了缺口后面的包」。只有无缺口时才用 `curr_rcv_seq_number + 1`。
2. **full ACK 才推进 `RcvBuffer` 的可读水位**：light ACK 不调 `ack_data`、不 `notify`。也就是说，上层 `recv` 能读到的数据量，受 full ACK 节奏约束。
3. **重复 ACK 节流**：当 `to_ack == 0`（没有新进度）时，若距上次发 ACK 不足 \(\text{rtt} + 4\cdot\text{rtt\_var}\)，就压住不发——这正是 TCP 风格的 RTO 下界，避免 ACK 风暴。
4. **带宽测量限频**：`pack_recv_rate` / `link_capacity` 每 `SYN_INTERVAL`（10ms）才重新算一次，因为中位数过滤（见 u7-l1）开销不小。

#### 4.2.3 源码精读

整个 `send_ack`：

[src/socket.rs:784-880](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L784-L880) —— 入参 `light: bool` 决定走哪条路径。

**第 1 段——计算确认水位**：

[src/socket.rs:785-798](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L785-L798) —— 用 `rcv_loss_list.peek_after(curr_rcv_seq_number + 1)` 找下一个缺口起点；找不到才用 `curr_rcv_seq_number + 1`。若该水位等于 `last_ack2_received`（对端已就此水位回过 ACK2），直接返回不发。

`peek_after` 的语义（在 u6-l3 详讲，此处只看接口）：

[src/loss_list.rs:144-160](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/loss_list.rs#L144-L160) —— 只读地返回「`after` 及之后的第一个丢失序号」，不改容器。

**第 2 段——light ACK**：

[src/socket.rs:800-810](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L800-L810) —— `light == true` 时构造一个 `ack_number = 0`、`info = None` 的 ACK 发出即返回。注释「Save time on buffer processing and bandwidth measurement」点明了它省掉了什么：不调 `rcv_buffer.ack_data`（不推进可读水位）、不算带宽。

**第 3 段——full ACK 的「发不发 / 推不推进」决策**：

[src/socket.rs:812-833](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L812-L833) —— 用 `to_ack = seq - last_sent_ack` 的三态分支：`Greater` 推进缓冲水位并唤醒读者；`Equal` 做 \(\text{rtt} + 4\cdot\text{rtt\_var}\) 节流；`Less` 直接返回。

`Equal` 分支里的节流条件正是经典 RTO 估计：

\[
\text{若 } \Delta t_{\text{last\_sent\_ack}} < \text{rtt} + 4\cdot\text{rtt\_var} \text{，则不发}
\]

推进 `RcvBuffer` 水位对应这两个调用：

[src/socket.rs:817-819](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L817-L819) —— `rcv_buffer().ack_data(seq_number)` 把 `next_to_ack` 推到 `seq`，`last_sent_ack = seq`，然后 `rcv_notify.notify_waiters()` 唤醒等数据的 `recv`。

缓冲侧的实现：

[src/queue/rcv_buffer.rs:38-42](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_buffer.rs#L38-L42) —— 仅当 `to - next_to_ack > 0`（循环差为正）才推进，防止水位倒退。

**第 4 段——构造 full ACK + 记账**：

[src/socket.rs:835-869](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L835-L869) —— 当 `last_sent_ack > last_ack2_received`（即这个水位对端还没确认收到过 ACK）时：`last_ack_seq_number += 1` 自增 ACK 序号，组装 `AckOptionalInfo`，按 `SYN_INTERVAL` 限频填充速率/带宽，最后 `new_ack(..., Some(ack_info))`。

`AckOptionalInfo` 的字段就是 full ACK 反馈给发送方拥塞控制的全部信息：

[src/control_packet.rs:329-337](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L329-L337) —— `rtt`、`rtt_variance`（微秒）、`available_buf_size`（接收方剩余缓冲，单位包）、`pack_recv_rate`（包/秒）、`link_capacity`（包/秒）。

注意 `available_buf_size` 被 `max(.., 2)` 兜底，保证至少报告 2，避免发送方把窗口算成 0 而停发：

[src/socket.rs:845-848](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L845-L848) —— `std::cmp::max(available_buf_size, 2)`。

发送之后立刻记账：

[src/socket.rs:871-877](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L871-L877) —— `ack_window.store(last_sent_ack, last_ack_seq_number)`。这一步是 4.3 节的主角。

最后看一眼控制包构造函数，确认 `ack_number` 写进 `additional_info`、`info` 写进 `AckInfo`：

[src/control_packet.rs:88-104](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L88-L104) —— `new_ack` 把 ACK 序号放进固定头的 `additional_info`，把水位与可选信息放进 `AckInfo`。`info == None` 时序列化只写 4 字节水位（light ACK），`Some` 时写 24 字节（full ACK），对应 [src/control_packet.rs:311-326](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L311-L326)。

#### 4.2.4 代码实践

**目标**：分清 light / full 两条路径各自做了什么、没做什么，并理解「full ACK 才推进可读水位」这一耦合。

**步骤**：

1. 对比 [src/socket.rs:800-810](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L800-L810)（light）与 [src/socket.rs:812-877](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L812-L877)（full），列一张表，逐项打勾：

   | 动作 | light ACK | full ACK |
   |------|:---------:|:--------:|
   | 发送控制包 | ✅ | ✅ |
   | 带 ACK 序号（`ack_number`） | ❌（为 0） | ✅ |
   | 带 `AckOptionalInfo` | ❌ | ✅ |
   | `rcv_buffer.ack_data`（推进可读水位） | ❌ | ✅ |
   | `rcv_notify.notify_waiters`（唤醒读者） | ❌ | ✅ |
   | `ack_window.store`（为 RTT 记账） | ❌ | ✅ |

2. 思考题：既然 light ACK 不推进 `next_to_ack`，那么「上层 `recv` 能读多少数据」由谁决定？答案是——**由 full ACK 的节奏决定**。结合 [src/queue/rcv_buffer.rs:44-57](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_buffer.rs#L44-L57) 的 `has_data_to_read`（以 `next_to_ack` 为上界）验证：只有 full ACK 调过 `ack_data` 之后，对应序号范围才进入可读窗口。

**预期结果**：你能用一句话回答「light ACK 与 full ACK 的本质区别」——light 只是对对端的水位通报（轻、频繁、无反馈），full 才同时完成「推进本地可读水位 + 反馈拥塞信息 + 启动 RTT 测量」三件事。

#### 4.2.5 小练习与答案

**练习 1**：`send_ack` 第 1 段里 `if seq_number == state.last_ack2_received { return Ok(()); }` 的作用是什么？

**参考答案**：`last_ack2_received` 是「对端已经通过 ACK2 确认收到过的水位」。如果本次要确认的水位和它相同，说明这个水位对端已经知道了，再发 ACK 是冗余的，于是提前返回，省一次发包。这是一个去重/节流优化。见 [src/socket.rs:794-796](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L794-L796)。

**练习 2**：full ACK 在 `to_ack == 0`（没有新进度）时仍可能发送，触发条件是什么？为什么不直接禁止重复 ACK？

**参考答案**：当 `last_sent_ack_time.elapsed() >= rtt + 4*rtt_var` 时，即使没有新进度也允许重发一次 full ACK（[src/socket.rs:821-828](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L821-L828)）。原因是：如果之前的 ACK 或对端的 ACK2 丢了，发送方会一直等不到确认而误判丢包；隔一个 RTO 量级重发一次「保活 ACK」能修复这种 ACK 丢失，且不会因为发太密而形成风暴。

---

### 4.3 ack_window 记录：为 ACK2 与 RTT 测量埋点

#### 4.3.1 概念说明

完整 ACK 带了一个自增的 ACK 序号（`last_ack_seq_number`），它的唯一用途是：**让发送方回一个 ACK2**，好让接收方（注意角色！）测量 RTT。

等等——这里容易把角色搞反。让我们厘清：在 UDT 的 RTT 测量里，**数据发送方**才是「发 ACK、收 ACK2、测 RTT」的一方？不对。重新看代码：`send_ack` 是在**收到数据包**的一侧执行的（它读 `curr_rcv_seq_number`、`rcv_loss_list` 这些接收游标）。所以是**数据接收方**发 ACK（带序号 `ack_seq`），**数据发送方**收到后回 ACK2（带同一个 `ack_seq`），**数据接收方**再凭 `ack_seq` 查出「我当初发这个 ACK 的时刻」，算出 RTT。

> 这条 ACK→ACK2→RTT 链路的两端都在「数据接收方」一侧完成记账与计算，中间只夹着发送方回送一个轻飘飘的 ACK2。因此 `ack_window` 这个「ACK 序号 → 发送时刻」的表，**归接收方持有**。

为什么需要这张表？因为 ACK 是异步的：发出去之后，ACK2 可能在很久以后才回来（也可能丢）。必须把「这个 ACK 序号对应的水位 + 发送时刻」存下来，等 ACK2 回来时按序号查表，才能算出 \(\text{RTT} = t_{\text{now}} - t_{\text{stored}}\)。

#### 4.3.2 核心流程

`AckWindow` 是一个固定容量（构造时传入，本实现是 1024）的滑动表，键是 ACK 序号 `AckSeqNumber`，值是 `(SeqNumber, Instant)`——即「这个 ACK 确认到的数据水位」与「发出 ACK 的时刻」。

```
store(seq, ack):           # 发 full ACK 后立刻调用
    if 已存满:                # LRU 淘汰：弹出最老的 ack，删掉它的记录
        oldest = keys.pop_front()
        acks.remove(oldest)
    keys.push_back(ack)
    acks.insert(ack, (seq, Instant::now()))

get(ack):                  # 收到 ACK2 时按序号查表
    return acks[ack].map(|(seq, ts)| (seq, ts.elapsed()))   # elapsed = now - stored
```

容量上限 1024 意味着：如果连续发出超过 1024 个 ACK 都没等到对应 ACK2（极端情况），最老的记录会被挤掉，那条 RTT 测量就丢了——但因为 ACK2 总会很快回来，正常情况下表远不会满。

设计上用了两个结构配合实现「固定容量 + O(log n) 查找 + O(1) 淘汰」：

- `BTreeMap<AckSeqNumber, (SeqNumber, Instant)>`：按 ACK 序号查记录。
- `VecDeque<AckSeqNumber>`：按插入顺序记录「谁是最老的」，淘汰时 `pop_front`。

这种「BTreeMap 存内容 + VecDeque 存顺序」的组合，是手写 LRU 的常见轻量做法。

#### 4.3.3 源码精读

`AckWindow` 结构与两个方法：

[src/ack_window.rs:5-33](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/ack_window.rs#L5-L33) —— `size` 控制容量，`acks` 是序号→记录的映射，`keys` 是插入顺序队列。

`store` 的 LRU 淘汰：

[src/ack_window.rs:21-28](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/ack_window.rs#L21-L28) —— 满了就先 `pop_front` 取出最老的 ACK 序号、从 `acks` 删掉，再插入新记录，时间戳取 `Instant::now()`。

`get` 用 `elapsed()` 直接返回「现在距存储时刻的时长」：

[src/ack_window.rs:30-32](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/ack_window.rs#L30-L32) —— 返回 `(seq, ts.elapsed())`，其中 `elapsed()` 就是 RTT。

容量初值在 `SocketState::new` 里固定为 1024：

[src/state/socket_state.rs:70](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/state/socket_state.rs#L70) —— `ack_window: AckWindow::new(1024)`。

**生产端**——在 `send_ack` 发出 full ACK 后立即 `store`（已在 4.2.3 看过）：

[src/socket.rs:871-877](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L871-L877) —— `state.ack_window.store(last_sent_ack, last_ack_seq_number)`，把「本次 ACK 的水位」和「ACK 序号」存下，时刻在 `store` 内部取 `Instant::now()`。

**消费端**——收到 ACK2 后，按 `ack_seq` 查表算 RTT（属于 u6-l4 的核心，这里只点出对接关系）：

[src/socket.rs:581-601](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L581-L601) —— `process_ctrl` 的 `Ack2` 分支：`ack_window.get(ack_seq)` 拿到 `(seq, rtt)`，用 `rtt` 与当前 `flow.rtt` 的绝对差更新 `rtt_var`，再用 `rtt` 更新 `flow.rtt`（EWMA 平滑）。

可以看到 `ack_window` 是连接「发 ACK」与「收 ACK2」两端的唯一桥梁：没有它，ACK2 回来时就无法知道这个序号对应哪一刻、算不出 RTT。这就是为什么 `send_ack` 在发完 full ACK 后**必须** `ack_window.store`——light ACK 不带序号、不期望 ACK2，所以不需要存。

#### 4.3.4 代码实践

**目标**：把「ACK → ACK2 → RTT」这条链路的两端连起来，验证 `ack_window` 是中间不可或缺的桥梁。

**步骤**：

1. 阅读生产端 [src/socket.rs:871-877](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L871-L877)（`store`）与消费端 [src/socket.rs:581-601](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L581-L601)（`get`）。
2. 回答三个问题：
   - 如果把 `ack_window.store(...)` 这一行注释掉，会发生什么？→ ACK2 回来时 `get` 永远返回 `None`，RTT 无法更新，`flow.rtt` 停在初值 100ms，进而 ACK 节流、EXP 超时、拥塞窗口全都基于失真的 RTT。
   - 为什么 `store` 必须在 `send_packet` **之后**而不是之前？→ 如果先 store 再发送失败，表里会留下一个永远等不到 ACK2 的「孤儿」记录（虽不致命，但浪费一个表项）；先确认发出成功再记账更干净。
   - 为什么 light ACK 不调用 `store`？→ light ACK 的 `ack_number` 恒为 0，发送方根本不会为它回 ACK2，存了也没人会来查。
3.（可选，源码阅读型）对照 [src/ack_window.rs:21-32](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/ack_window.rs#L21-L32)，手推一次：连续 `store` 1025 次（不调 `get`），第 1 条记录会被淘汰；此时若收到对应第 1 个 ACK 序号的 ACK2，`get` 返回 `None`，该次 RTT 测量丢失。

**预期结果**：你能清晰说出 `ack_window` 在整条 RTT 测量链路中的定位——**它是接收方本地的一张「ACK 序号 → 发送时刻」账本，专门服务于 ACK2 回来后的 RTT 计算**。

#### 4.3.5 小练习与答案

**练习 1**：`AckWindow` 为什么同时用 `BTreeMap` 和 `VecDeque` 两个结构？只用一个行不行？

**参考答案**：`BTreeMap` 负责「按 ACK 序号 O(log n) 查记录」，`VecDeque` 负责「按插入顺序 O(1) 找最老、做 LRU 淘汰」。只用 `BTreeMap` 无法高效知道「哪个最老」（要遍历）；只用 `VecDeque` 无法按任意序号查找（要线性扫描）。两者配合才同时满足「查找快 + 淘汰快」。见 [src/ack_window.rs:6-10](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/ack_window.rs#L6-L10)。

**练习 2**：为什么 `ack_window` 的容量设成 1024 而不是无限大或很小（比如 4）？

**参考答案**：RTT 测量要求「ACK2 在对应记录被淘汰之前回来」。容量太小（如 4）时，只要短时间内连发几个 full ACK 还没等到 ACK2，老记录就被挤掉，RTT 测量频繁失败；无限大则内存无界增长。1024 是一个经验性的足够余量——正常网络下 ACK2 远比这个快回来，表几乎不会满，既保证测量成功率，又封顶了内存。值定义在 [src/state/socket_state.rs:70](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/state/socket_state.rs#L70)。

**练习 3**：`ack_window.get` 返回的 `Duration` 是「存储时刻到当前时刻」，这个时长精确等于 RTT 吗？

**参考答案**：不精确等于端到端 RTT，而是「接收方发 ACK → 发送方处理 → 发送方回 ACK2 → 接收方收到 ACK2」这一段的耗时，包含了发送方的处理延迟与排队时间，是 RTT 的一个上界估计。UDT 用它配合 EWMA（`flow.update_rtt`，系数 7/8）平滑后作为 RTT 估计。详见 u6-l4 与 u7-l1。

---

## 5. 综合实践

把本讲三个最小模块串起来，做一次「**带丢包的接收全流程推演**」。这是一道源码阅读 + 纸上推演题，不修改源码。

**场景设定**：接收方初始 `curr_rcv_seq_number = 9`，`last_sent_ack = 9`，`last_ack2_received = 5`，`last_ack_seq_number = 0`，`rcv_loss_list` 为空，`flow.rtt = 100ms`，`flow.rtt_var = 50ms`。数据包按以下顺序到达：`seq = 10, 13, 11, 10`。

**任务**：

1. 对每个包，逐行走 `process_data`（[src/socket.rs:675-757](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L675-L757)），记录它：
   - 是否被「offset<0 丢弃」？
   - 是否触发「插入 loss_list + 发 NAK」？若是，写出 NAK 的 `loss_info`。
   - 是否「推进 `curr_rcv_seq_number`」？推进到多少？还是「从 loss_list 移除」？
2. 推演结束后，假设 `check_timers` 此刻触发一次 full ACK（`send_ack(false)`），走 [src/socket.rs:784-880](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L784-L880)：
   - 第 1 段算出的 `seq_number`（确认水位）是多少？（提示：受 `rcv_loss_list.peek_after` 约束）
   - 走到第 3 段时 `to_ack` 是哪一态？是否推进 `RcvBuffer` 水位、是否唤醒读者？
   - 第 4 段是否构造 full ACK？`ack_number` 自增到多少？`ack_window.store` 存了哪对值？

**参考推演**（请先自己做再对照）：

- `seq=10`：offset = 10−9 = 1（不过期）；无缺口（10−9 = 1，不 >1）；`10−9>0` → `curr_rcv_seq_number = 10`。
- `seq=13`：offset = 13−9 = 4（不过期）；`13−10 = 3 > 1` → 缺口 `(11,12)` 进 `rcv_loss_list`，发 NAK `loss_info = [11 | 0x8000_0000, 12]`；`13−10>0` → `curr_rcv_seq_number = 13`。
- `seq=11`：offset = 11−9 = 2（不过期）；`11−13 = -2`，不 >1，不发 NAK；`11−13 > 0` 不成立 → 落 else，`rcv_loss_list.remove(11)`（缺口缩成只剩 12）。注意：缺口还没补全，`curr_rcv_seq_number` 仍是 13。
- `seq=10`（重复）：offset = 10−9 = 1（不过期，因为 `last_sent_ack` 还是 9）；`10−13` 不 >1；`10−13 > 0` 不成立 → else 分支 `rcv_loss_list.remove(10)`，但 10 不在 loss_list 里，无操作。

此刻状态：`curr_rcv_seq_number = 13`，`rcv_loss_list` 含 `{12}`，`last_sent_ack = 9`，`last_ack2_received = 5`，`last_ack_seq_number = 0`。

`send_ack(false)` 推演：

- 第 1 段：`peek_after(curr_rcv_seq_number + 1 = 14)`。注意 `rcv_loss_list` 里有个 12，它比查找点 14 还要小——线性看似乎「不在 `[14, ∞)` 内」。但 `peek_after` 有三个分支：第一分支 `range(..=14)` 找 `end >= 14` 的区间（12 的 end=12，不满足）；第二分支 `range(14..)` 找 `>= 14` 的键（没有）；于是落到**第三分支 `iter().next()`**——它返回 BTreeMap 里数值最小的键 = 12。**这是循环序列号意义上的「回绕」查找**：在 31 位循环空间里，从 14 往前走（14→15→…→max→0→…→12），12 仍在「之后」。所以 `peek_after(14)` 返回 `Some(12)`，`seq = 12`。这恰好是正确的水位——缺口在 12，意味着「连续只收到 11 及以前」，所以 ACK 水位必须停在 12，不能谎报收到 13。`12 != last_ack2_received(5)`，继续。
- 第 3 段：`to_ack = 12 − 9 = 3 > 0` → Greater：`rcv_buffer.ack_data(12)`（让 11 及以前的数据变可读，13 因越过缺口仍不可读）、`last_sent_ack = 12`、`rcv_notify.notify_waiters()`。
- 第 4 段：`last_sent_ack(12) > last_ack2_received(5)` 成立 → `last_ack_seq_number` 自增到 1；构造 `AckOptionalInfo`（含 rtt=100000µs、rtt_var=50000µs 等）；发 full ACK(ack_number=1, next_seq=12, info=Some)；`ack_window.store(12, 1)`。

> 关键收获：`send_ack` 的水位**绝不会越过已知缺口**。哪怕缺口落在 `curr_rcv_seq_number` 之下（本例 12 < 13），`peek_after` 也会靠循环回绕分支把它找出来，把水位钳制在缺口处。这正是「可靠」的体现——接收方绝不在 ACK 里声称自己收到了实际上有缺口的数据。请回到 [src/socket.rs:785-798](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L785-L798) 与 [src/loss_list.rs:144-160](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/loss_list.rs#L144-L160) 复核 `peek_after` 的三分支语义，确认它与上述推演一致。

完成本题后，你就把「丢包检测 → 即时 NAK → 缺口补传 → full ACK 推进水位与反馈拥塞 → ack_window 记账」整条接收侧反馈链路走通了一遍。

## 6. 本讲小结

- **`process_data` 三段**：`offset < 0` 丢弃过期/重复包（[src/socket.rs:698-702](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L698-L702)）；`seq - curr_rcv_seq_number > 1` 时把缺口写入 `rcv_loss_list` 并**立即**发 NAK（[src/socket.rs:719-742](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L719-L742)）；最后按「新最大序号推进 / 否则从 loss_list 移除」更新 `curr_rcv_seq_number`（[src/socket.rs:748-754](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L748-L754)）。
- **NAK 的 loss_info 编码**：单点丢一个 u32；区间丢两个 u32，第一个用最高位 `0x8000_0000` 标记「后跟终点」。
- **light vs full ACK**：light 只通报水位（`ack_number=0`、`info=None`），不推进缓冲、不算带宽；full 带 ACK 序号与 `AckOptionalInfo`，推进 `RcvBuffer` 可读水位、唤醒读者、反馈拥塞信息。上层能读多少数据由 full ACK 节奏决定。
- **full ACK 的节流**：无新进度时，仅在距上次发 ACK 超过 \(\text{rtt} + 4\cdot\text{rtt\_var}\) 才重发一次保活 ACK。
- **`ack_window.store` 的意义**：发 full ACK 后存下「水位 + ACK 序号 + 时刻」，是 ACK2 回来后计算 RTT 的唯一依据；light ACK 不存。容量 1024 的 LRU 由 `BTreeMap + VecDeque` 实现。
- **角色提醒**：ACK/NAK/ACK2 这套反馈机制的两端记账都在「**数据接收方**」一侧——接收方发 ACK、收 ACK2、用 `ack_window` 测 RTT。

## 7. 下一步学习建议

本讲讲清了「接收侧如何检测丢包、发 NAK、发 ACK 并为 RTT 记账」，但这些反馈信息在**发送侧**被消费的细节还没展开。建议按以下顺序继续：

1. **u6-l3 丢包检测与 NAK：LossList**：本讲只用了 `rcv_loss_list` 的 `insert`/`peek_after`/`remove` 接口，下一讲会深入 `LossList` 的区间合并、拆分、跨 `max` 回绕实现，以及发送侧 `process_ctrl` 如何**解析** NAK 的 loss_info 编码（本讲是产生方，u6-l3 是消费方）。
2. **u6-l4 ACK2、AckWindow 与 RTT 测量**：本讲点到了 `ack_window.get` 的消费端（[src/socket.rs:581-601](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L581-L601)），下一讲会完整讲清 ACK2 分支如何更新 `rtt`/`rtt_var`，以及它和 full ACK 携带的 `AckOptionalInfo.rtt` 这**两条** RTT 更新路径的区别。
3. **u7-l1 流量与带宽估计 UdtFlow**：本讲反复提到的 `on_pkt_arrival`、`get_pkt_rcv_speed`、`get_bandwidth`（它们填进 full ACK 的 `AckOptionalInfo`）下一讲会展开「中位数 ±8 倍过滤」「probe 包成对测带宽」的算法。
4. **u7-l3 定时器：EXP、keep-alive 与超时重传**：本讲的 `send_ack` 是被 `check_timers` 调用的，下一讲会完整讲清 `check_timers` 如何调度 ACK 定时、light ACK、EXP 退避与超时重传。

建议在进入下一讲前，先把本讲「综合实践」的推演独立做一遍——能顺畅推完，说明接收侧反馈链路你已经吃透了。

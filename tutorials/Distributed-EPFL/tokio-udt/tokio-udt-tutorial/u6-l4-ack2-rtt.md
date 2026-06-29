# ACK2、AckWindow 与 RTT 测量

## 1. 本讲目标

本讲解决一个看似简单却贯穿整个 UDT 拥塞控制的问题：**发送方怎么知道“一个数据包从发出到被确认到底花了多久”？**

这段时长就是 **RTT（Round-Trip Time，往返时延）**。它是 UDT 几乎所有定时器与调节器的输入：EXP 超时判定用 `rtt + 4*rtt_var`，发送节奏（pacing）、ACK 间隔都要参考它。RTT 估得不准，重传要么过早（乱发）、要么过晚（空等）。

UDT 用一个非常巧妙的闭环来测 RTT：接收方发 ACK 时带一个递增的“ACK 序号”，发送方收到后回一个 **ACK2**（只回序号、不回数据）。接收方只要记下“我哪一刻发出这个 ACK 序号”，收到对应 ACK2 时用当前时刻减去它，就得到了这一对控制包的真实往返时间。

学完本讲，你应当能：

- 说清 `AckWindow` 如何在“固定容量、序号循环递增”的约束下做 LRU 淘汰，并返回“从存储到现在经过了多久”。
- 跟踪 `process_ctrl` 的 `Ack2` 分支，复述 RTT 与 rtt_var 是怎么算出来的。
- 区分更新 RTT 的两条路径（ACK2 反馈 vs ACK 可选信息 `extra.rtt`），并说明在单向传输中哪条路径才真正喂给发送方的拥塞控制。

本讲承接 u6-l2（`send_ack` 如何分配 ACK 序号、如何 `ack_window.store`），是它的“闭环下半场”。

## 2. 前置知识

### 2.1 RTT 与 RTT 方差（rtt_var）

- **RTT**：一个包从“我发出”到“我收到对端确认”所经历的时间。
- **rtt_var（RTT 方差）**：衡量 RTT 的抖动幅度。方差越大，说明网络延迟越不稳定，定时器要留更多余量。

UDT 不直接用单次测量值，而是用 **EWMA（指数加权移动平均，Exponentially Weighted Moving Average）** 平滑，避免单次毛刺主导估计。EWMA 的特点是“老值占大头、新值占小头”，既跟得上趋势又不至于抖动。

### 2.2 ACK / ACK2 的角色与序号空间

回顾 u4-l3 与 u6-l2：

- **ACK**（控制包类型 `0x0002`）：接收方发给发送方，通报“我已收到哪些数据”。**full ACK** 还附带 `AckOptionalInfo`（RTT、带宽等拥塞反馈），并带一个递增的 **ACK 序号**（`AckSeqNumber`，31 位循环空间）。
- **ACK2**（控制包类型 `0x0006`）：发送方收到 full ACK 后回给接收方，**只回那个 ACK 序号**，不带任何数据。它的唯一用途就是“关上”接收方打开的那只“计时秒表”。

请务必记住角色对应关系：

- **接收方**发 ACK → **发送方**回 ACK2。
- 因此“发 ACK、等 ACK2、算 RTT”这个闭环，**跑在接收方一侧**。也就是说，RTT 是接收方测出来的；发送方需要靠接收方在 full ACK 里“汇报”才能拿到（见 4.3）。

### 2.3 SeqNumber / AckSeqNumber 的循环算术

`AckSeqNumber` 与 `SeqNumber` 都是 31 位循环空间上的序列号（u4-l4）。两号相减返回 `i32`，表示“环上带符号最短距离”。本讲里多处出现 `seq - state.last_ack2_received > 0`，用的就是这套语义——判断“这次确认的数据是否比我上次记的更靠后”。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
|------|------|-----------|
| `src/ack_window.rs` | ACK 序号 → 发送时刻 的记事本 | `store` / `get` 的固定容量淘汰与 `elapsed` |
| `src/socket.rs` | 收发决策与控制包处理 | `process_ctrl` 的 `Ack2` 分支；`send_ack` 里的 `ack_window.store` |
| `src/flow.rs` | 流量与 RTT 的平滑器 | `update_rtt` / `update_rtt_var` 的 EWMA 系数 |
| `src/control_packet.rs` | 控制包格式 | `AckOptionalInfo` 结构、`ack_seq_number()` 提取 |
| `src/state/socket_state.rs` | socket 级簿记 | `ack_window` 字段与初始容量 `AckWindow::new(1024)` |

## 4. 核心概念与源码讲解

### 4.1 AckWindow：把 ACK 序号映射到发送时刻

#### 4.1.1 概念说明

接收方每次发出一个 full ACK，就“按下秒表”：记下“这个 ACK 序号是在哪一刻发出的”。等对应的 ACK2 回来，只要凭序号查回那一刻，就能算出经过了多久。

`AckWindow` 就是这本“序号 → 发出时刻”的记事本。它要解决两个现实约束：

1. **序号会一直递增**（循环空间里往前走），记事本不能无限增长——必须**固定容量、淘汰最旧**。
2. **ACK2 不一定回来**（UDP 不可靠，ACK2 也可能丢），所以记下来的条目很可能永远查不到——这无所谓，最旧的会被自然淘汰。

#### 4.1.2 核心流程

`AckWindow` 用“两个数据结构记同一份内容”的经典手法做 LRU：

- `acks: BTreeMap<AckSeqNumber, (SeqNumber, Instant)>` —— 按 ACK 序号快速查“当时确认到的数据水位 SeqNumber + 发出时刻 Instant”。
- `keys: VecDeque<AckSeqNumber>` —— 一个**插入顺序队列**，队首即“最旧”，用来知道该淘汰谁。

容量固定为 `size`（本实现里是 1024，见 4.1.3）。

```
store(seq, ack):                       # 发出一个 full ACK 时调用
  if keys 已满 (>= size):
      oldest = keys.pop_front()        # 队首最旧
      acks.remove(oldest)              # 从 BTreeMap 同步删掉
  keys.push_back(ack)                  # 新序号入队尾
  acks.insert(ack, (seq, now()))       # 记下"此刻"

get(ack):                              # 收到 ACK2 时查回
  return acks[ack].map(|(seq, ts)| (seq, ts.elapsed()))
                                       # elapsed() = now - ts，正好是往返耗时
```

关键点：`get` 返回的 `Duration` 就是“从 store 到 get 之间真实流逝的时间”——也就是这一对 ACK/ACK2 的往返时间。**测量的“减法”由 `Instant::elapsed()` 完成**，`AckWindow` 自己不存 Duration，只存起点 `Instant`。

#### 4.1.3 源码精读

`AckWindow` 的结构体定义（[src/ack_window.rs:5-10](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/ack_window.rs#L5-L10)）：`size` 是容量上限，`acks` 是查询表，`keys` 是淘汰顺序队列。注意 `keys` 用 `VecDeque::with_capacity(size)` 预分配，避免高频 store 时的反复扩容。

`store`（[src/ack_window.rs:21-28](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/ack_window.rs#L21-L28)）：先判满、淘汰队首、再插入。`Instant::now()` 是“按下秒表”的时刻。

`get`（[src/ack_window.rs:30-32](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/ack_window.rs#L30-L32)）：`ts.elapsed()` 把存储的绝对时刻换算成“到现在经过多久”。查不到返回 `None`（说明这个序号已被淘汰，或 ACK2 来迟了）。

容量从哪里来？在 socket 创建时固定写死（[src/state/socket_state.rs:70](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/state/socket_state.rs#L70)）：

```rust
ack_window: AckWindow::new(1024),
```

即每个 socket 的 AckWindow 最多记住 1024 个“未关闭的 ACK”。

> **为什么用 BTreeMap + VecDeque 而不是单一结构？** 见 4.1.4 的实践任务。一句话预告：两种访问模式（“按序号随机查”与“按插入时间淘汰最旧”）各自最适合同一种结构，组合后两个操作都是 O(log n) / O(1)，且实现简单、无需手写 LRU 链表。

#### 4.1.4 代码实践

**实践目标**：理解 AckWindow 的固定容量淘汰为何要双结构。

**操作步骤**：

1. 打开 `src/ack_window.rs`，通读这 33 行。
2. 假设 `size = 3`，手动模拟连续 `store` 五次（序号 A1..A5），每次都先问“keys 满了吗？满了就丢队首”。在纸上写下每一步 `acks` 与 `keys` 的内容。
3. 模拟收到一个迟到的 ACK2(A2)：调用 `get(A2)`。

**需要观察的现象**：

- `store(A4)` 时 `keys` 长度到 3，`A1` 被从队首弹出，`acks` 里 `A1` 也随之删除。
- `get(A2)` 时 `A2` 可能已被淘汰（取决于你模拟到第几步），返回 `None`——这正是“ACK2 丢失/迟到”的优雅退化。
- `get` 返回的 `Duration` 来自 `ts.elapsed()`，而非存储时算好的差值。

**预期结果**：你能解释“满容量后每存一个就淘汰最旧一个、且两个结构必须同步删除”。如果无法本地编译运行，可标注「待本地验证」后纯靠纸面推演完成。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `store` 里淘汰时要从 `keys`（VecDeque）取队首，而不是直接遍历 `acks`（BTreeMap）找最小序号？

**参考答案**：ACK 序号是循环递增的（u4-l4），但“最旧”是指**插入时间最早**，不一定等于“序号数值最小”（序号会回绕）。`keys` 按 store 的真实先后顺序排列，队首一定是物理上最早写入的，与回绕无关。BTreeMap 按序号数值排序，回绕后会出错。

**练习 2**：如果某个 ACK2 永远丢失，对应的条目会怎样？

**参考答案**：它会一直留在 `acks`/`keys` 里，直到队列满 1024 后被自然淘汰。不存在内存泄漏，因为容量固定为 1024。

---

### 4.2 ACK2 反馈环：process_ctrl 的 Ack2 分支如何测出 RTT

#### 4.2.1 概念说明

4.1 讲了“秒表怎么记”。本节讲“秒表怎么停、停了之后怎么算”。

当发送方收到接收方的 full ACK，它会回一个 ACK2（带相同序号）。接收方收到这个 ACK2 时进入 `process_ctrl` 的 `Ack2` 分支，做三件事：

1. 用序号去 `AckWindow` 查回“当时发出 ACK 的时刻”，算出本次往返 `rtt`。
2. 用 `|本次 rtt − 当前平滑 rtt|` 作为抖动样本，更新 `rtt_var`。
3. 把 ACK 里通报的数据水位记到 `last_ack2_received`（供 4.3 提到的“是否该再带 ACK 序号”判定用）。

注意第 2 步：**rtt_var 的输入不是 rtt 本身，而是“本次测量偏离当前估计的程度”**——这正是统计学里“方差”的直觉（偏离均值的幅度）。

#### 4.2.2 核心流程

```
process_ctrl(Ack2):
  ack_seq = packet.ack_seq_number()        # 从包头 additional_info 取出序号
  window = state.ack_window.get(ack_seq)   # 查回 (数据水位 seq, 往返时长 rtt)
  if window 是 Some((seq, rtt)):
      rtt_abs_diff = |rtt - flow.rtt|      # 本次样本与当前平滑值的绝对偏差
      flow.update_rtt_var(rtt_abs_diff)    # 用偏差更新方差
      flow.update_rtt(rtt)                 # 用样本更新 RTT
      if seq - last_ack2_received > 0:     # 循环减法：更靠后才记
          last_ack2_received = seq
```

数学上，EWMA 平滑后（系数见 4.3）：

\[
\text{rtt\_var} \leftarrow \frac{3}{4}\,\text{rtt\_var} + \frac{1}{4}\,|\,\text{rtt}_{\text{sample}} - \text{rtt}\,|
\]

\[
\text{rtt} \leftarrow \frac{7}{8}\,\text{rtt} + \frac{1}{8}\,\text{rtt}_{\text{sample}}
\]

#### 4.2.3 源码精读

ACK2 分支在 `process_ctrl` 中（[src/socket.rs:581-601](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L581-L601)）：

```rust
ControlPacketType::Ack2 => {
    let ack_seq = packet.ack_seq_number().unwrap();
    let window = self.state().ack_window.get(ack_seq);
    if let Some((seq, rtt)) = window {
        let mut flow = self.flow.write().unwrap();
        let rtt_abs_diff = {
            if rtt > flow.rtt { rtt - flow.rtt } else { flow.rtt - rtt }
        };
        flow.update_rtt_var(rtt_abs_diff);
        flow.update_rtt(rtt);
        drop(flow);
        let mut state = self.state();
        if (seq - state.last_ack2_received) > 0 {
            state.last_ack2_received = seq;
        }
    }
}
```

读这段要抓三个细节：

- **`packet.ack_seq_number()`**：ACK2 包没有单独的“ACK 序号”字段，它**复用控制包公共头的 `additional_info` 字段**来装序号（[src/control_packet.rs:106-109](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L106-L109)，对 `Ack`/`Ack2` 都返回 `additional_info`）。这是 u4-l3 讲过的“`additional_info` 是类型复用字段”。
- **`rtt_abs_diff` 手写绝对值**：标准库的 `Duration` 没有 `abs()`，所以用 if/else 分两支算 `|rtt - flow.rtt|`。
- **`seq - last_ack2_received > 0`**：循环减法（u4-l4），判断“这次确认到的数据水位是否比我上次记的更靠后”，更靠后才更新——防止乱序到达的 ACK2 把水位往回拨。

配套地，`ack_window` 是在哪里被 `store` 的？在**接收方**的 `send_ack` 里：发出 full ACK 后立即记下（[src/socket.rs:871-877](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L871-L877)），其中序号 `last_ack_seq_number` 在构造该 ACK 前刚刚自增（[src/socket.rs:838](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L838)）。所以“按秒表”与“停秒表”用同一个递增序号配对，闭环成立。

> **角色再次强调**：`store` 在接收方的 `send_ack`，`get` 在接收方的 `Ack2` 分支。两边都在**接收方**——因为 ACK 是接收方发的、ACK2 是发送方回给接收方的。RTT 由接收方测出。

#### 4.2.4 代码实践

**实践目标**：跟踪一个 ACK 序号从“按下秒表”到“停表算 RTT”的完整旅程。

**操作步骤**：

1. 在 `send_ack` 的 `state.ack_window.store(...)`（socket.rs:876）处，记下当前 `last_ack_seq_number` 的值与 `Instant::now()`（脑内或加一行 `eprintln!`）。
2. 在 `Ack2` 分支的 `let window = ...ack_window.get(ack_seq)`（socket.rs:583）处，打印 `ack_seq` 与返回的 `rtt`。
3. 运行 `cargo run --bin udt_receiver` 与 `cargo run --bin udt_sender`（u1-l2），观察日志。

**需要观察的现象**：

- 每次 store 的序号，过一会儿会在 get 处以相同序号出现（ACK2 回来了）。
- `rtt` 在本地回环下应当是几十微秒到几毫秒量级；丢包或跨网络时会变大、会抖动。
- 偶尔会有 store 了但迟迟没 get 的序号——那是对端 ACK2 没回或被淘汰了。

**预期结果**：你能复述“序号是闭环的配对钥匙，`elapsed()` 完成测量”。若不在本地跑，标注「待本地验证」，改为纸面跟踪 u6-l2 里 send_ack 产出的 ACK 序号如何流到本分支。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `rtt_var` 用 `|sample - rtt|` 作输入，而不是直接用 `sample`？

**参考答案**：rtt_var 衡量的是“抖动/偏离程度”。`|sample - rtt|` 正是单次样本偏离当前估计的幅度，物理含义就是方差。直接用 `sample` 会让方差随 RTT 水涨船高，失去“衡量稳定性”的意义。

**练习 2**：`drop(flow)` 之后才去更新 `last_ack2_received`，为什么要先 drop？

**参考答案**：`flow` 是 `self.flow` 的写锁。后续 `self.state()` 取的是另一把锁（`state` 的锁）。先释放 flow 写锁，避免同时持有两把锁带来的死锁风险和锁粒度膨胀——这是 Rust 里“用完即 drop 以收窄临界区”的常见写法。

---

### 4.3 两条 RTT 更新路径：flow 的 EWMA 平滑与可选信息路径对比

#### 4.3.1 概念说明

`flow`（`UdtFlow`）里持有被平滑后的 `rtt` 与 `rtt_var`，并提供两个 EWMA 更新方法（4.2.2 的公式）。有趣的是，**这两个方法有两个不同的调用方**，喂进来的“样本”含义并不一样：

| 路径 | 触发者 | 样本来源 | 样本性质 |
|------|--------|----------|----------|
| **ACK2 路径** | 接收方 | `ack_window.get()` 算出的本次往返 | 本地**直接**测量的单次往返 |
| **可选信息路径** | 发送方 | full ACK 里的 `extra.rtt` / `extra.rtt_variance` | **对端**已经平滑过的估计值 |

关键洞察：**在单向大数据传输（如 `udt_sender → udt_receiver`）中，发送方根本不收数据，也就从不发 ACK、自然也收不到 ACK2**。于是发送方的 `flow.rtt` 只能靠“可选信息路径”更新——它读的是接收方在 full ACK 里汇报的、接收方自己用 ACK2 测出来的 RTT。

而接收方一侧，两条路径都可能触发（它既收数据发 ACK，又可能在双向时收数据）。但接收方不跑拥塞控制的发送节奏，所以它的 RTT 测量主要意义是“汇报给发送方”。

#### 4.3.2 核心流程

可选信息路径（在 `Ack` 分支的 `Some(extra)` 子分支里），发送方收到 full ACK 后：

```
flow.update_rtt(Duration::from_micros(extra.rtt))          # 对端汇报的 rtt
flow.update_rtt_var(Duration::from_micros(extra.rtt_variance))  # 对端汇报的 rtt_var
rate_control.set_rtt(flow.rtt)                              # 喂给拥塞控制
... rate_control.on_ack(seq); self.cc_update();             # 更新发送节奏
```

注意它与 ACK2 路径的两点不同：

1. **样本是“对端汇报值”而非“本地测量”**：`extra.rtt` 是接收方写入 ACK 的 `flow.rtt.as_micros()`（见 4.3.3），已经是接收方 EWMA 平滑过一轮的值；发送方再 `update_rtt` 又平滑一轮——**双重平滑**，更稳但也更滞后。
2. **rtt_var 的输入不同**：ACK2 路径喂的是 `|sample - rtt|`（本地偏差）；可选信息路径喂的是 `extra.rtt_variance`（对端已算好的方差），即 `(3/4)*自己 + (1/4)*对端方差`。

而 `update_rtt` / `update_rtt_var` 本身的 EWMA 系数是两条路径共用的（[src/flow.rs:96-102](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/flow.rs#L96-L102)）：

\[
\text{rtt} \leftarrow \tfrac{7}{8}\,\text{rtt} + \tfrac{1}{8}\,\text{new}, \qquad
\text{rtt\_var} \leftarrow \tfrac{3}{4}\,\text{rtt\_var} + \tfrac{1}{4}\,\text{new}
\]

初值（[src/flow.rs:31-32](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/flow.rs#L31-L32)）：`rtt = 100ms`、`rtt_var = 50ms`——握手后到第一次测量前的保守起步值。

#### 4.3.3 源码精读

可选信息路径在 `Ack` 分支（[src/socket.rs:558-560](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L558-L560)）：

```rust
let mut flow = self.flow.write().unwrap();
flow.update_rtt(Duration::from_micros(extra.rtt.into()));
flow.update_rtt_var(Duration::from_micros(extra.rtt_variance.into()));
```

`extra` 就是 `AckOptionalInfo`（[src/control_packet.rs:329-337](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L329-L337)），其中 `rtt` / `rtt_variance` 都是 **微秒为单位的 `u32`**。它由接收方的 `send_ack` 填充：`rtt: flow.rtt.as_micros()...`（[src/socket.rs:842-844](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L842-L844)）。注意这条赋值恰好出现在 4.2.3 提到的 `ack_window.store` 附近——**同一个 full ACK 既“按下接收方的秒表”，又“把接收方的 RTT 汇报给发送方”**，一包两用。

EWMA 平滑器（[src/flow.rs:96-102](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/flow.rs#L96-L102)）：

```rust
pub fn update_rtt(&mut self, new_val: Duration) {
    self.rtt = (7 * self.rtt + new_val) / 8;
}
pub fn update_rtt_var(&mut self, new_val: Duration) {
    self.rtt_var = (3 * self.rtt_var + new_val) / 4;
}
```

> **关于“哪条更精确”**：ACK2 路径用本次往返的**原始单点测量**做样本（只过一次 EWMA），对网络变化更敏感、更“新鲜”；可选信息路径用的是**对端已平滑一轮**的值（再过一次 EWMA，双重平滑），更稳但更滞后。所以单论“对当前网络状态的即时反映”，ACK2 路径更精确。但**对于纯发送方，可选信息路径是唯一选择**——它不收数据、发不了 ACK、收不到 ACK2，只能信对端的汇报。两条路径分工互补：接收方本地精测（ACK2），再通过 full ACK 把结果汇报给无法自测的发送方（可选信息）。

#### 4.3.4 代码实践

**实践目标**：对比两条 RTT 更新路径的输入差异，理解为何单向传输下发送方依赖可选信息路径。

**操作步骤**：

1. 在 ACK2 分支（socket.rs:593-594）与可选信息分支（socket.rs:559-560）各加一行 `eprintln!`，打印当前角色（可用 `self.socket_id` 区分收发两端）与传入 `update_rtt` 的样本值。
2. 运行 `udt_sender` ↔ `udt_receiver`（单向传输：sender 只发、receiver 只收）。
3. 观察哪一端打印了 ACK2 路径、哪一端打印了可选信息路径。

**需要观察的现象**：

- **接收方**（udt_receiver）几乎只走 ACK2 路径——它在发 ACK、收 ACK2。
- **发送方**（udt_sender）几乎只走可选信息路径——它在收 full ACK、读 `extra.rtt`，自己从不发 ACK。
- 发送方读到的 `extra.rtt` 与接收方 ACK2 路径算出的 `rtt` 量级一致（因为前者本就是后者平滑后汇报上来的）。

**预期结果**：你能用一句话回答“为什么发送方的拥塞控制（EXP 超时、发送节奏）必须依赖可选信息路径”：因为发送方不收数据、无法自测 RTT。若不在本地运行，标注「待本地验证」，改为静态分析：sender 的 `process_ctrl` 永远进不了 `Ack2` 分支（它从不开秒表），只能进 `Ack(Some(extra))` 分支。

#### 4.3.5 小练习与答案

**练习 1**：可选信息路径喂给 `update_rtt_var` 的是 `extra.rtt_variance`，而 ACK2 路径喂的是 `|sample - rtt|`。这两种输入各自有什么含义？

**参考答案**：ACK2 路径用“本次样本偏离当前估计的幅度”当方差输入，是标准的“本地单点偏差”；可选信息路径直接采用“对端算好的方差”，相当于把自己的方差与对端方差做加权融合。前者反映本地即时抖动，后者是对端视角的平滑方差。

**练习 2**：若把 `update_rtt` 的系数从 `(7*rtt + new)/8` 改成 `(rtt + new)/2`，RTT 估计会变得更快还是更慢地跟随真实变化？会有什么副作用？

**参考答案**：会变得**更快**跟随（新样本权重从 1/8 升到 1/2），响应更灵敏；副作用是对单次毛刺更敏感，RTT 估计抖动变大，进而让依赖 `rtt + 4*rtt_var` 的 EXP 超时判定变得不稳定，可能引发误重传。

---

## 5. 综合实践

把本讲三块知识串起来，完成一次“RTT 测量闭环”的全程跟踪：

1. **画出时序图**：在一张图上画出接收方（R）与发送方（S）之间的一次 full ACK / ACK2 往返。标注：R 在 `send_ack` 里 `last_ack_seq_number` 自增（socket.rs:838）→ 构造 full ACK 带序号与 `extra.rtt`（socket.rs:842-865）→ 发出后 `ack_window.store`（socket.rs:876）→ S 收到进 `Ack(Some(extra))` 分支，回 ACK2 并用 `extra.rtt` 更新自己的 `flow.rtt`（socket.rs:504-560）→ R 收到 ACK2 进 `Ack2` 分支，`ack_window.get` 算出本地 RTT 并更新 `flow.rtt`/`rtt_var`（socket.rs:581-601）。
2. **标注测量点**：在图上用两个箭头标出“秒表按下（store 的 `Instant::now()`）”和“秒表停止（get 的 `ts.elapsed()`）”，确认两者都在接收方一侧。
3. **回答关键问题**：在一个 sender 只发、receiver 只收的场景里，**发送方的 `flow.rtt` 是通过哪条路径更新的？为什么它没法用 ACK2 路径？**（答：可选信息路径；因为它不收数据、不发 ACK、收不到 ACK2，没有本地秒表可停。）
4. **进阶（可选）**：在两处 `update_rtt` 调用旁加日志，实际跑一对 sender/receiver，验证你画时序图时的角色判断。

## 6. 本讲小结

- **AckWindow** 用 `BTreeMap`（按序号查）+ `VecDeque`（按插入顺序淘汰最旧）双结构实现固定容量（1024）的 LRU，`get` 返回的 `Duration` 由 `Instant::elapsed()` 现算，只存起点不存时长。
- **ACK2 闭环**：接收方发 full ACK 时 `store` 按下秒表，发送方回 ACK2（复用 `additional_info` 装序号），接收方收到后 `get` 停表，`elapsed()` 即本次往返。
- **RTT 方差的输入是偏差**：ACK2 路径用 `|sample − rtt|` 作 `rtt_var` 输入，体现“偏离估计的幅度”这一方差直觉。
- **EWMA 平滑**：`rtt = (7rtt + new)/8`、`rtt_var = (3rtt_var + new)/4`，初值 100ms / 50ms，既跟趋势又抗毛刺。
- **两条路径分工**：ACK2 路径是接收方的本地直接测量（单次 EWMA、更即时）；可选信息路径是发送方读对端汇报值（双重平滑、更稳），且是**纯发送方唯一的 RTT 来源**。

## 7. 下一步学习建议

本讲把 RTT / rtt_var 这两个“地基变量”讲清了，它们马上要被消费：

- **u7-l1（UdtFlow）**：`flow.rtt` 与 `rtt_var` 不仅被本讲更新，还参与接收方的带宽/包速率估计，并被打包进 `AckOptionalInfo` 上报。建议接着读 `flow.rs` 的 `get_pkt_rcv_speed` / `get_bandwidth`，看清 RTT 与带宽如何一起进 ACK。
- **u7-l3（定时器）**：EXP 超时判定核心式 `exp_count * (rtt + 4*rtt_var) + SYN_INTERVAL`（socket.rs:926）直接消费本讲的两个变量。学完 u7-l3 你会明白“为什么 rtt_var 估错会导致误判对端掉线”。
- 如果想立刻看到 RTT 的“实战效果”，回到 u1-l2 跑 sender/receiver 时，可结合本讲在 `update_rtt` 处加日志，观察 RTT 在丢包前后的变化曲线。

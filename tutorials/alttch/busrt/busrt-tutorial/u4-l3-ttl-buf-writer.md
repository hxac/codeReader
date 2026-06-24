# TtlBufWriter：TTL 缓冲与刷新策略

## 1. 本讲目标

本讲聚焦 BUS/RT 出站数据写入的「最后一公里」：`TtlBufWriter`。

学完后你应该能够：

- 说清 `Flush` 三种取值（`No` / `Scheduled` / `Instant`）各自的语义，以及它们如何由 `QoS::is_realtime()` 映射而来。
- 解释 `TtlBufWriter` 如何用一个后台 `flusher` 任务 + 一个容量为 1 的信号通道，把「成批写入」与「低延迟实时」这对矛盾统一起来。
- 理解 `Drop` 实现为何能在连接销毁时仍然保证缓冲区被刷新。
- 能沿着 `ipc::Client` 与 `broker::handle_writer` 的代码路径，追踪一条 `QoS::No` 消息与一条 `QoS::Realtime` 消息分别走 `Flush::Scheduled` 与 `Flush::Instant` 的全过程。

## 2. 前置知识

在继续之前，请确认你已掌握以下概念（它们在前序讲义中已建立）：

- **两个正交的 QoS 位**：`QoS` 用两个位编码两件互相独立的事。低位 `needs_ack()`（是否等代理回 `OP_ACK`），高位 `is_realtime()`（是否要求即时刷新出站）。参见 [u2-l1 核心类型](u2-l1-core-types.md) 与 [u4-l2 ipc::Client](u4-l2-ipc-client.md)。
- **发送宏族**：`ipc::Client` 的所有发送方法最终都收敛到 `send_frame_and_confirm!`，它把一帧拆成「帧头 buf」与「payload」两次 `write`。参见 [u4-l2](u4-l2-ipc-client.md)。
- **BufWriter**：tokio 的 `BufWriter<W>` 在内部维护一块缓冲区，`write_all` 只是把字节拷进缓冲，真正写到 socket 需要 `flush()`。
- **零拷贝载荷 `borrow::Cow`**：发送方法收到的 payload 是 `Cow`，进入 socket 路径时用 `as_slice()` 取只读切片。参见 [u2-l2 零拷贝载荷模型](u2-l2-zero-copy-cow.md)。

本讲要回答的核心问题是：**既然 BufWriter 已经能缓冲，为什么 BUS/RT 还要再包一层 `TtlBufWriter`？答案在于「什么时候刷新」需要一个可由 QoS 驱动、可批量合并、且掉电不丢的策略。**

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [src/comm.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/comm.rs) | 定义 `Flush` 枚举与 `TtlBufWriter`，是本讲的主角。 |
| [src/ipc.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs) | 外部客户端用 `Writer` 枚举把 Unix/Tcp/WebSocket 三种写半部都包成 `TtlBufWriter`，并通过发送宏决定 `Flush`。 |
| [src/broker.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs) | 服务端每个连接的 `PeerConnection` 同样持有 `TtlBufWriter`，`handle_writer` 在序列化出站帧时决定刷新策略。 |
| [src/lib.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs) | 提供 `DEFAULT_BUF_TTL`（10µs）、`DEFAULT_BUF_SIZE`（8192）等默认值与 `QoS` 定义。 |

---

## 4. 核心概念与源码讲解

### 4.1 Flush 枚举：三种刷新策略

#### 4.1.1 概念说明

写 socket 的开销绝大部分来自系统调用（`write`/`send`）本身，而不是搬字节的成本。如果每发一个小帧就触发一次系统调用，吞吐会被 syscall 数量压垮；但如果永远不主动刷新，消息又会一直躺在缓冲区里，延迟趋近无穷。

BUS/RT 用一个三态枚举把「这一笔写入之后要不要刷新、怎么刷新」明确化：

- **`Flush::No`**：只把字节追加进缓冲，什么都不触发。用于「我后面还有字节要跟着写」的场景（典型是帧头之后紧接 payload）。
- **`Flush::Scheduled`**：追加字节，并确保「稍后」有一个刷新被安排上（默认 10µs 后）。在等待期间到达的更多写入会被合并到同一次刷新里——这就是**批量吞吐**的来源。
- **`Flush::Instant`**：追加字节后立刻 `flush()`，一次都不等。这是**实时低延迟**的来源。

#### 4.1.2 核心流程

`Flush` 与 QoS 的映射非常简洁：只看 `is_realtime()` 这一个布尔位。

```
QoS::is_realtime() == true  ──▶  Flush::Instant   （立刻刷新）
QoS::is_realtime() == false ──▶  Flush::Scheduled （延迟合并）
```

注意：**是否实时** 与 **是否需要 ACK** 是两个独立的位。`QoS::Processed`（要 ACK 但不实时）和 `QoS::No`（都不要）在刷新策略上完全相同，都走 `Scheduled`；只有 `QoS::Realtime` 和 `QoS::RealtimeProcessed` 才走 `Instant`。

#### 4.1.3 源码精读

`Flush` 枚举定义在 [src/comm.rs:8-13](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/comm.rs#L8-L13)，三个变体没有附加数据：

```rust
#[derive(Debug, Copy, Clone, Eq, PartialEq)]
pub enum Flush {
    No,
    Scheduled,
    Instant,
}
```

把布尔位翻译成刷新策略的桥梁是 `From<bool> for Flush`，见 [src/comm.rs:15-24](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/comm.rs#L15-L24)：

```rust
impl From<bool> for Flush {
    fn from(realtime: bool) -> Self {
        if realtime { Flush::Instant } else { Flush::Scheduled }
    }
}
```

这里只接受 `true → Instant / false → Scheduled` 两种结果，恰好对应 `is_realtime()` 的两值语义——也就是说，凡是走「真实 payload 写入」的地方，永远不会是 `Flush::No`；`No` 专门留给帧头/帧内 header 等中间片段。

QoS 的位定义见 [src/lib.rs:352-369](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L352-L369)，`is_realtime()` 检测高位 `0b10`：

```rust
pub enum QoS { No = 0, Processed = 1, Realtime = 2, RealtimeProcessed = 3 }
impl QoS {
    pub fn is_realtime(self) -> bool { self as u8 & 0b10 != 0 }
    pub fn needs_ack(self) -> bool   { self as u8 & 0b1  != 0 }
}
```

于是完整的 QoS → Flush 映射表如下：

| QoS | u8 | needs_ack | is_realtime | payload 的 Flush |
| --- | --- | --- | --- | --- |
| `No` | 0 | 否 | 否 | `Scheduled` |
| `Processed` | 1 | 是 | 否 | `Scheduled` |
| `Realtime` | 2 | 否 | 是 | `Instant` |
| `RealtimeProcessed` | 3 | 是 | 是 | `Instant` |

#### 4.1.4 代码实践

**实践目标**：亲手验证 `QoS → Flush` 的位映射。

**操作步骤**：

1. 打开 [src/lib.rs:352-369](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L352-L369) 与 [src/comm.rs:15-24](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/comm.rs#L15-L24)。
2. 在脑中（或一张纸上）对四个 QoS 值分别计算 `as u8 & 0b10` 与 `& 0b1`，填出上表。
3. 对照表格回答：`QoS::Processed` 的 payload 会走哪种 Flush？它与 `QoS::No` 在刷新延迟上有区别吗？

**预期结果**：`Processed` 与 `No` 的刷新延迟**没有区别**（都是 `Scheduled`），它们的差异只在「是否等 ACK」，与刷新策略正交。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `From<bool> for Flush` 没有产出 `Flush::No` 的分支？`No` 是给谁用的？

> **答案**：`No` 表示「这次写入之后还有后续字节，先别刷新」。它只用于一帧内部的多段写入（如先写帧头、再写 payload），由调用方显式传入；而 `From<bool>` 服务于「这一段是一帧的最后一段，要不要立刻刷新」的二选一决策，故只产生 `Instant`/`Scheduled`。

**练习 2**：如果有一条消息既要求实时、又要求确认送达，应该用哪个 QoS？它的 payload 走哪种 Flush？

> **答案**：`QoS::RealtimeProcessed`（=3），两个位都置 1；payload 走 `Flush::Instant`（因为 `is_realtime()` 为真），同时 `needs_ack()` 为真会登记一个 oneshot 等代理回 `OP_ACK`。

---

### 4.2 TtlBufWriter 的结构、new 与 write

#### 4.2.1 概念说明

`TtlBufWriter<W>` 是一个对底层异步 writer `W`（Unix/Tcp 的写半部、WebSocket 写半部）的包装。它在 tokio 的 `BufWriter<W>` 外面又加了三样东西：

1. 一把 `tokio::sync::Mutex`，让「写缓冲」与「后台 flusher 刷新缓冲」这两个并发任务互斥，避免数据竞争。
2. 一个信号通道 `tx`，用来通知 flusher「该刷新了」。
3. 一个后台 `flusher` 任务，负责在收到信号后等 `ttl` 再刷新。

它的设计目标是：**对调用方暴露一个简单的 `write(buf, flush)` 接口，把「缓冲、合并、定时、掉电刷新」全部封装在内部。**

#### 4.2.2 核心流程

`write(buf, flush)` 的决策树：

```
write(buf, flush):
  1. lock 互斥锁，拿到 BufWriter
  2. write_all(buf)              # 字节先进缓冲（必定发生）
  3. 根据 flush 分支：
       Instant   ─▶ writer.flush() 立刻刷新整个缓冲
       Scheduled ─▶ 若信号通道为空(tx.is_empty())，发一个信号通知 flusher
       No        ─▶ 什么都不做
  4. 返回 write_all 的结果
```

关键细节：

- **`Instant` 刷新的是整个 `BufWriter`**，所以在这之前用 `Flush::No` 写进去的帧头也会被一起冲出去——这就是为什么帧头永远写 `No`、只让 payload 决定刷新。
- **`Scheduled` 只在 `tx.is_empty()` 时发信号**：如果已经有一个刷新挂在那儿等着，就不必再发一个，避免 flusher 做冗余刷新。字节本身已经在缓冲里，不会丢。

#### 4.2.3 源码精读

结构体定义见 [src/comm.rs:26-31](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/comm.rs#L26-L31)：

```rust
pub struct TtlBufWriter<W> {
    writer: Arc<Mutex<BufWriter<W>>>,
    tx: async_channel::Sender<()>,
    dtx: Option<oneshot::Sender<()>>,
    flusher: JoinHandle<()>,
}
```

四个字段：共享的带锁 BufWriter、给 flusher 的信号发送端、给「drop 刷新」任务的触发器、flusher 任务的句柄。

构造函数 `new` 见 [src/comm.rs:37-64](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/comm.rs#L37-L64)，它创建容量为 `cap` 的 BufWriter、容量为 1 的信号通道，并 spawn 出两个后台任务（flusher 与 drop 刷新，后者在 4.4 节详讲）：

```rust
pub fn new(writer: W, cap: usize, ttl: Duration, timeout: Duration) -> Self {
    let writer = Arc::new(Mutex::new(BufWriter::with_capacity(cap, writer)));
    let (tx, rx) = async_channel::bounded::<()>(1);
    // flusher future（见 4.3）
    let flusher = tokio::spawn(async move { /* ... */ });
    // ... drop future（见 4.4）
}
```

注意 `cap` 直接成为 `BufWriter` 的容量，默认 `DEFAULT_BUF_SIZE = 8192`（见 [src/lib.rs:44-45](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L44-L45)）。

核心写入方法 `write` 见 [src/comm.rs:66-75](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/comm.rs#L66-L75)，对应上面的决策树：

```rust
pub async fn write(&mut self, buf: &[u8], flush: Flush) -> std::io::Result<()> {
    let mut writer = self.writer.lock().await;
    let result = writer.write_all(buf).await;
    if flush == Flush::Instant {
        writer.flush().await?;
    } else if flush == Flush::Scheduled && self.tx.is_empty() {
        let _ = self.tx.send(()).await;
    }
    result
}
```

逐行解读：

- 第 67 行先抢锁——保证 flusher 不会在「写一半」时把缓冲刷出去。
- 第 68 行 `write_all` 是必做动作，无论哪种 Flush 都先把字节入缓冲。
- 第 69-70 行 `Instant` 分支：在**同一把锁**内立刻 `flush()`，把刚写入的（以及此前累积的）字节全部冲到 socket。
- 第 71-73 行 `Scheduled` 分支：只有通道空时才发信号，这是合并刷新的关键。

#### 4.2.4 代码实践

**实践目标**：理解「帧头写 `No`、payload 写 `Instant`」如何保证一帧被完整刷新。

**操作步骤**：

1. 阅读 [src/comm.rs:66-75](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/comm.rs#L66-L75) 的 `write`。
2. 思考：如果帧头也用 `Flush::Instant` 写、payload 紧随其后用 `Flush::No` 写，会出现什么问题？

**需要观察的现象 / 预期结果**：帧头会在 payload 还没入缓冲时就被单独 `flush()` 冲到 socket，对端 `handle_read` 读到一个不完整的帧会报 `broken frame` / `Unexpected EOF`。因此 BUS/RT 选择「头部 `No`、尾部决定刷新」，确保整帧原子地离开缓冲。

#### 4.2.5 小练习与答案

**练习 1**：`write` 里 `Scheduled` 分支的 `&& self.tx.is_empty()` 如果去掉，会发生什么？

> **答案**：每次 `Scheduled` 写入都发一个信号，flusher 就会在每次 ttl 后做一次刷新，退化为「几乎每条消息一次刷新」，合并效果丧失，吞吐下降。`is_empty()` 检查保证「一个 ttl 窗口内最多挂一个刷新请求」。

**练习 2**：`Instant` 分支为什么不需要检查 `tx.is_empty()`？

> **答案**：`Instant` 直接在当前锁内同步 `flush()`，根本不依赖 flusher 任务，也没有发信号；即便 flusher 之后还会因之前的 `Scheduled` 信号再刷一次，那也是幂等的（`BufWriter::flush` 在缓冲已空时是廉价空操作）。

---

### 4.3 flusher future 与 TTL 定时刷新（吞吐关键）

#### 4.3.1 概念说明

`Scheduled` 刷新并不是「写完立刻刷」，而是「写完后等一个很短的 ttl 再刷」。这段等待时间就是给后续消息「搭便车」的窗口：在 ttl 窗口内到达的所有写入，都会被同一次 `flush()` 一起送出。

`ttl` 默认只有 **10 微秒**（`DEFAULT_BUF_TTL`），足够把突发的小消息合并成一次系统调用，又短到对延迟几乎无感。

#### 4.3.2 核心流程

flusher 任务是一个无限循环，每次循环做三件事：

```
loop:
  1. rx.recv().await        # 等一个「该刷新了」信号；通道关闭则退出
  2. Timer::after(ttl).await # 故意等 ttl，给后续写入搭便车的时间
  3. lock writer，flush()    # 把窗口内累积的所有字节一次冲出
```

合并效应可以用一个简单的模型刻画。设消息到达速率为 \(r\)（条/秒），ttl 窗口为 \(t\)，则一个窗口内平均搭便车的消息数为 \(r \cdot t\)，于是每秒刷新次数（系统调用频率）约为：

\[
\text{flush 率} \;\approx\; \min\!\left(r,\;\frac{1}{t}\right)
\]

- 当 \(r\) 很小（稀疏消息），几乎每条都单独触发一次刷新，flush 率 \(\approx r\)，延迟仅多一个 \(t\)。
- 当 \(r\) 很大（高吞吐突发），flush 率被 \(1/t\) 封顶。默认 \(t = 10\mu s\) 时，封顶约 \(10^{5}\) 次/秒——远低于「每条消息一次 syscall」的开销，这正是批量吞吐的来源。

对比 `Instant`：实时消息的 flush 率恒等于 \(r\)（每条必刷），换取的是**零 ttl 等待**的最低延迟。这就是「吞吐 vs 延迟」的开关，由 QoS 的高位 `is_realtime()` 选择。

#### 4.3.3 源码精读

flusher future 定义在 `new` 内部，见 [src/comm.rs:42-49](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/comm.rs#L42-L49)：

```rust
let flusher = tokio::spawn(async move {
    while rx.recv().await.is_ok() {
        async_io::Timer::after(ttl).await;
        if let Ok(mut writer) = tokio::time::timeout(timeout, wf.lock()).await {
            let _r = tokio::time::timeout(timeout, writer.flush()).await;
        }
    }
});
```

逐行解读：

- 第 43 行 `rx.recv().await.is_ok()`：阻塞等信号；当 `TtlBufWriter` 被 drop、`tx` 关闭时 `recv` 返回 `Err`，循环自然结束。
- 第 44 行 `Timer::after(ttl).await`：**故意延迟**——这是合并窗口本身。注意它发生在 `recv` 之后、`lock` 之前，等待期间不持锁，所以这段时间内 `write` 仍能把新字节追加进缓冲。
- 第 45-46 行：抢锁与刷新都包了 `timeout`（默认 1 秒，来自 `DEFAULT_TIMEOUT`），防止一个慢 socket 把 flusher 永久卡死，进而拖垮所有出站刷新。

`DEFAULT_BUF_TTL` 与 `DEFAULT_BUF_SIZE` 的定义见 [src/lib.rs:44-45](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L44-L45)：

```rust
pub const DEFAULT_BUF_TTL: Duration = Duration::from_micros(10);
pub const DEFAULT_BUF_SIZE: usize = 8192;
```

#### 4.3.4 代码实践

**实践目标**：把 `buf_ttl` 当成可调旋钮，理解它对延迟与吞吐的影响。

**操作步骤**：

1. 找到客户端配置入口 [src/ipc.rs:104-107](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L104-L107) 的 `Config::buf_ttl`，以及服务端 [src/broker.rs:878-880](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L878-L880) 的 `ServerConfig::buf_ttl`。
2. 在一个测试程序里把 `buf_ttl` 调大到例如 `Duration::from_millis(50)`，用 `QoS::No` 连发 1000 条小消息到 `busrt` CLI 的 `listen`，测量端到端延迟与总耗时。
3. 再把 `buf_ttl` 调回 `Duration::from_micros(10)` 重测，对比两组数据。

**需要观察的现象 / 预期结果**：大 ttl 下单条 `QoS::No` 消息延迟上升（最坏多约一个 ttl），但突发吞吐更高（系统调用更少）；小 ttl 下延迟更低、syscall 更频繁。**待本地验证**具体数值。

#### 4.3.5 小练习与答案

**练习 1**：fluser 为什么在 `recv` 之后、`lock` 之前等待 ttl，而不是先锁再等？

> **答案**：如果在持锁期间等待 ttl，那么整个 ttl 窗口内 `write` 都拿不到锁，新消息根本进不了缓冲，合并就无从谈起。把等待放在锁外，等待期间 `write` 可以持续追加字节，等结束后一次 `flush` 才能真正合并它们。

**练习 2**：把 ttl 设为 0（`Duration::ZERO`）会怎样？它和 `Instant` 等价吗？

> **答案**：不完全等价。ttl=0 时 `Scheduled` 仍然要走「发信号 → flusher 唤醒 → lock → flush」的异步往返，存在任务调度延迟与一次额外的锁竞争；而 `Instant` 是在 `write` 持有的同一把锁内同步 flush，路径更短、延迟更低。所以实时消息仍应显式用 `QoS::Realtime` 走 `Instant`。

---

### 4.4 Drop 安全：连接销毁时保证刷新

#### 4.4.1 概念说明

连接被关闭、客户端被 drop 时，缓冲区里可能还躺着没来得及刷新的字节。如果不处理，这些字节就丢了。`TtlBufWriter` 通过第二个后台任务保证：**结构体一旦 drop，缓冲区会被最终刷新一次。**

#### 4.4.2 核心流程

`Drop` 实现做两件事：

```
drop:
  1. abort flusher 任务        # 不再需要定时刷新了
  2. 通过 dtx 发一个信号        # 唤醒 drop 刷新任务
```

drop 刷新任务在 `new` 里 spawn，它一直阻塞在 `drx.await` 上，收到信号后抢锁并 flush 一次：

```
drop 刷新任务:
  1. drx.await                  # 等 Drop 发来的信号
  2. lock writer，flush()       # 把残留字节送出
```

为什么用一个独立的 `oneshot` + 后台任务，而不是直接在 `Drop::drop` 里同步 flush？因为 `Drop::drop` 是同步函数，不能 `.await`；而 flush 是异步操作。把异步 flush 委托给一个已 spawn 的任务，`drop` 里只发信号，就绕开了「同步析构里做异步 IO」的矛盾。

#### 4.4.3 源码精读

drop 刷新任务的 spawn 见 [src/comm.rs:53-57](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/comm.rs#L53-L57)：

```rust
// this future works on drop
tokio::spawn(async move {
    let _r = drx.await;
    let mut writer = wf.lock().await;
    let _r = tokio::time::timeout(timeout, writer.flush()).await;
});
```

`Drop` 实现见 [src/comm.rs:78-83](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/comm.rs#L78-L83)：

```rust
impl<W> Drop for TtlBufWriter<W> {
    fn drop(&mut self) {
        self.flusher.abort();
        let _ = self.dtx.take().unwrap().send(());
    }
}
```

注意第 81 行 `self.dtx.take().unwrap()`：`dtx` 字段是 `Option<oneshot::Sender<()>>`，`take()` 把它取出并发送，恰好触发一次；`unwrap` 安全是因为正常生命周期内 `dtx` 只在这里被取走一次。

字段 `dtx` 与 `flusher` 的声明回到 [src/comm.rs:29-30](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/comm.rs#L29-L30)，两者在 `new` 末尾一同装配进返回值（[src/comm.rs:58-63](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/comm.rs#L58-L63)）。

#### 4.4.4 代码实践

**实践目标**：确认「drop 时也会刷新」这一安全保证的代码依据。

**操作步骤**：

1. 阅读 [src/comm.rs:78-83](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/comm.rs#L78-L83) 的 `Drop` 与 [src/comm.rs:53-57](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/comm.rs#L53-L57) 的 drop 刷新任务。
2. 追踪：客户端正常退出时，`ipc::Client::drop`（[src/ipc.rs:549-553](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L549-L553)）只 `abort` 了读循环 `reader_fut`，并没有显式 flush——那么残留字节由谁刷新？

**预期结果**：`Client` 的 `writer` 字段（`Writer` 枚举，内含 `TtlBufWriter`）在 `Client` drop 时随之析构，触发 `TtlBufWriter::drop`，由 drop 刷新任务完成最后一次 flush。即「最末端的字节」由 `TtlBufWriter` 自己兜底。

#### 4.4.5 小练习与答案

**练习 1**：为什么不在 `Drop::drop` 里直接调用 `writer.flush()`？

> **答案**：`Drop::drop` 是同步函数，不能 `.await`；而 `BufWriter::flush` 是异步方法。BUS/RT 选择 spawn 一个常驻任务、在 `drop` 里只通过 `oneshot` 发信号的方式，把异步 flush 从同步析构里解耦出来。

**练习 2**：`Drop` 里先 `flusher.abort()` 再发 drop 信号，顺序能反过来吗？

> **答案**：顺序在实践中影响不大（drop 刷新任务拿的是同一把 `writer` 锁，fluser 已被 abort 不会与之竞争），但先 abort flusher 语义上更清晰：明确「不再接受新的定时刷新」，只做最终的兜底刷新。

---

### 4.5 flush 参数在 ipc::Client 与 broker 中的传递

#### 4.5.1 概念说明

`Flush` 是个「纯策略枚举」，它必须从两个源头被正确地喂给 `TtlBufWriter::write`：客户端发送侧（`ipc::Client`）和服务端出站侧（`broker::handle_writer`）。两边规则一致：**帧头/中间片段用 `No`，由 payload 的 realtime 位决定 `Instant`/`Scheduled`，握手与心跳等控制字节用 `Instant`。**

#### 4.5.2 核心流程

客户端一帧的两次写入（`send_frame_and_confirm!`）：

```
send_frame_and_confirm!(buf, payload, qos):
  1. (可选) 若 needs_ack，登记 oneshot 到 responses 表
  2. writer.write(buf,     Flush::No)                   # 帧头：不刷新
  3. writer.write(payload, qos.is_realtime().into())    # payload：决定刷新
```

服务端出站一帧（`handle_writer`）同样两段：

```
handle_writer 收到一帧:
  1. 序列化 6 字节头 + sender/topic 进 buf
  2. writer.write(&buf,        Flush::No)                 # 头部：不刷新
  3. (可选) writer.write(header, Flush::No)               # 帧内 header：不刷新
  4. writer.write(payload,     frame.realtime.into())     # payload：决定刷新
```

两边都用 `Flush::No` 写头部、用 `realtime` 位决定 payload 刷新——这是协议一致的体现。

#### 4.5.3 源码精读

**客户端侧**。`ipc::Client` 用 `Writer` 枚举把三种传输统一成 `TtlBufWriter`，见 [src/ipc.rs:55-60](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L55-L60)：

```rust
enum Writer {
    Unix(TtlBufWriter<unix::OwnedWriteHalf>),
    Tcp(TtlBufWriter<tcp::OwnedWriteHalf>),
    WebSocket(TtlBufWriter<WsWriteHalf>),
}
```

`Writer::write` 透传给内部 `TtlBufWriter::write`，见 [src/ipc.rs:63-70](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L63-L70)。三处传输在 `connect_broker` 里各自 `TtlBufWriter::new`，参数完全一致（`buf_size, buf_ttl, timeout`），例如 TCP 分支 [src/ipc.rs:363-368](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L363-L368)、Unix 分支 [src/ipc.rs:338-343](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L338-L343)、WebSocket 分支 [src/ipc.rs:301-306](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L301-L306)。

决定刷新策略的核心是 `send_frame_and_confirm!`，见 [src/ipc.rs:165-180](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L165-L180)：

```rust
macro_rules! send_frame_and_confirm {
    ($self: expr, $buf: expr, $payload: expr, $qos: expr) => {{
        let rx = if $qos.needs_ack() { /* 登记 oneshot */ } else { None };
        send_data_or_mark_disconnected!($self, $buf, Flush::No);
        send_data_or_mark_disconnected!($self, $payload, $qos.is_realtime().into());
        Ok(rx)
    }};
}
```

第 176 行帧头 `Flush::No`、第 177 行 payload `qos.is_realtime().into()` —— 这就是「`QoS::No` → `Scheduled`、`QoS::Realtime` → `Instant`」的精确落点。中间的 `send_data_or_mark_disconnected!`（[src/ipc.rs:148-163](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L148-L163)）在写入超时或出错时，会把 `connected` 置 false、`abort` 读循环，标记连接断开。

心跳 `ping` 是另一条直接走 `Instant` 的路径，见 [src/ipc.rs:530-534](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L530-L534)：

```rust
async fn ping(&mut self) -> Result<(), Error> {
    send_data_or_mark_disconnected!(self, PING_FRAME, Flush::Instant);
    Ok(())
}
```

PING 必须立刻送出（它就是用来探测连接活性的），所以无条件 `Instant`。

**服务端侧**。每个对端连接的 `PeerConnection` 持有 `writer: TtlBufWriter<W>`，见 [src/broker.rs:1265](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1265)；它在 `handle_connection` 里由 `ServerConfig` 的 `buf_size/buf_ttl` 构造，见 [src/broker.rs:1188](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1188)，而 `ServerConfig` 的默认值取自 `DEFAULT_BUF_SIZE/DEFAULT_BUF_TTL`，见 [src/broker.rs:846-863](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L846-L863)。

握手阶段（`handle_peer`）用 `write_and_flush!` 宏无条件 `Flush::Instant`，见 [src/broker.rs:1743-1747](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1743-L1747)：

```rust
macro_rules! write_and_flush {
    ($buf: expr) => { time::timeout(timeout, writer.write($buf, Flush::Instant)).await??; };
}
```

握手字节（greetings、`RESPONSE_OK`、错误码）必须即时送达对端，否则握手会卡在 `chat` 的 `read_exact` 上超时，故一律 `Instant`。

业务帧出站在 `handle_writer`，见 [src/broker.rs:2213-2263](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L2213-L2263)，关键三行：

```rust
write_data!(&buf, Flush::No);                          // L2255 头部
if let Some(header) = frame.header() {
    write_data!(header, Flush::No);                    // L2257 帧内 header
}
write_data!(frame.payload(), frame.realtime.into());   // L2259 payload 决定刷新
```

注意第 2259 行用的是 `frame.realtime`（入站帧自带的实时位），而不是服务端自己的 QoS——也就是说，**实时性是端到端透传的**：客户端用 `QoS::Realtime` 发出的帧，到代理出站时仍按 realtime 立即刷新，延迟优势不会被代理的缓冲层抹平。

#### 4.5.4 代码实践

**实践目标**：完整追踪 `QoS::No` 与 `QoS::Realtime` 两条消息在 `ipc::Client` 中的不同代码路径，并解释延迟差异。

**操作步骤**（源码阅读型实践）：

1. 从 `ipc::Client` 的 `publish` 方法 [src/ipc.rs:454-461](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L454-L461) 出发，确认它调用 `send_frame!(...)`。
2. 跟到 `send_frame!`（[src/ipc.rs:199-232](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L199-L232)），它拼好 `buf`（9 字节头 + topic）后调用 `send_frame_and_confirm!`。
3. 进入 `send_frame_and_confirm!`（[src/ipc.rs:165-180](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L165-L180)），定位到：
   - **`QoS::No`**：`needs_ack()=false`（不登记 oneshot）、`is_realtime()=false`，于是 payload 走 `Flush::Scheduled` → 进入 `TtlBufWriter::write` 的 [src/comm.rs:71-73](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/comm.rs#L71-L73) 分支，发信号给 flusher，flusher 等 ttl(10µs) 后刷新。
   - **`QoS::Realtime`**：`needs_ack()=false`、`is_realtime()=true`，payload 走 `Flush::Instant` → 进入 [src/comm.rs:69-70](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/comm.rs#L69-L70) 分支，在当前锁内立刻 `flush()`。
4. 在两条路径旁各标注一个「延迟来源」清单。

**需要观察的现象 / 预期结果**：画出两条路径的分叉图——它们在 `send_frame_and_confirm!` 第 177 行的 `$qos.is_realtime().into()` 处分道扬镳。`QoS::No` 路径多出「发信号 → flusher 唤醒 → 等 ttl → 抢锁 → flush」这一串异步开销（最坏约一个 ttl）；`QoS::Realtime` 路径省掉了 ttl 等待与跨任务调度，在 `write` 持锁期间同步刷新，**因此实时消息能获得更低延迟**。

#### 4.5.5 小练习与答案

**练习 1**：服务端 `handle_writer` 用 `frame.realtime` 而非某个固定 QoS 来决定刷新，这意味着什么？

> **答案**：意味着实时性是端到端透传的。客户端发出的 realtime 帧，经过代理转发时仍按 realtime 立即刷新出站；代理不会因为自己的缓冲策略把实时帧拖成普通帧，端到端低延迟得以保持。

**练习 2**：握手与 PING 为什么都强制 `Flush::Instant`，而不走 `Scheduled`？

> **答案**：握手是阻塞式的——双方都在 `read_exact` 等对方字节，若走 `Scheduled` 等 ttl 才刷新，握手延迟会叠加甚至触发超时失败；PING 是活性探测，延迟送达就失去了探测意义。两者都属于「必须立刻送达」的控制字节，故无条件 `Instant`。

---

## 5. 综合实践

把本讲的知识串起来，做一个「延迟对比」小实验。

**任务**：启动一个 BUS/RT 代理与一个监听客户端，用同一个发送客户端分别以 `QoS::No` 和 `QoS::Realtime` 各发 100 条带时间戳的小消息，测量两类消息从发送到对端收到的端到端延迟分布。

**建议步骤**：

1. 用 `test.sh server` 或嵌入方式起一个监听 `/tmp/busrt.sock` 的代理（参见 [u1-l2](u1-l2-build-and-run.md)）。
2. 写一个 listener：`ipc::Client::connect` 后 `subscribe("#", QoS::No).await`，在 `take_event_channel` 的循环里收到帧即记录当前时刻。
3. 写一个 sender：连同一个代理，先发 100 条 `QoS::No`，再发 100 条 `QoS::Realtime`，payload 里放发送时刻。
4. 对比两组延迟。

**预期结果**（**待本地验证**）：`QoS::Realtime` 组的延迟分布应明显更窄、更稳定（无 ttl 等待）；`QoS::No` 组平均延迟略高（多约一个 `buf_ttl` 量级），但突发场景下系统调用更少、吞吐更高。若把 `buf_ttl` 调大，`QoS::No` 组延迟会进一步上升——这正是 4.3 节模型的实证。

> 说明：本实践需要自行编写示例程序（项目 examples 中无完全对应的延迟基准脚本），可参照 [examples/client_sender.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/client_sender.rs) 与 [examples/client_listener.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/client_listener.rs) 的结构。

## 6. 本讲小结

- `Flush` 是三态策略枚举：`No`（只入缓冲，用于帧头/中间片段）、`Scheduled`（延迟合并刷新）、`Instant`（立即刷新）。`From<bool>` 把 `is_realtime()` 映射成 `Instant`/`Scheduled`，`No` 只由调用方显式使用。
- `TtlBufWriter` = `BufWriter` + `Mutex` + 信号通道 + 后台 flusher。`write(buf, flush)` 把字节入缓冲后，按 Flush 决定立即刷新、发信号、或不动作。
- flusher 任务在收到信号后**先等 ttl 再刷新**，等待不持锁，从而把 ttl 窗口内的多条消息合并成一次系统调用；默认 ttl=10µs。刷新率随消息率上升被 \(1/ttl\) 封顶，这是吞吐的来源。
- `Instant` 在 `write` 持有的同一把锁内同步 `flush()`，省掉 ttl 等待与跨任务调度，这是实时低延迟的来源；二者由 QoS 高位 `is_realtime()` 切换。
- `Drop` 通过一个常驻的 drop 刷新任务保证连接销毁时缓冲被最终刷新一次，解决了「同步析构里做异步 IO」的矛盾。
- 客户端 `send_frame_and_confirm!` 与服务端 `handle_writer` 遵循同一规则：头部/帧内 header 用 `Flush::No`，payload 用 `realtime` 位决定刷新；握手与 PING 等控制字节强制 `Instant`。服务端用 `frame.realtime` 透传实时性，保证端到端低延迟。

## 7. 下一步学习建议

- 本讲只讲了「写出缓冲」。读取侧的 `BufReader` 与 `handle_read` 帧解析循环在 [u4-l2](u4-l2-ipc-client.md) 已覆盖，可对照复习「读缓冲 vs 写缓冲」的不对称设计。
- 实时刷新只是实时特性的冰山一角。`rt` feature 还会把含自旋锁的 `parking_lot::Mutex` 换成无自旋的 `parking_lot_rt` 版本，并支持 `AsyncAllocator` 把大块内存分配移出实时运行时——详见 [u7-l1 实时特性](u7-l1-realtime.md)。
- 若想看 `TtlBufWriter` 在更高层的应用，可继续阅读 RPC 层 [u5-l2 RpcClient 与 RpcHandlers](u5-l2-rpc-client-handlers.md)，RPC 调用最终也会经同一套发送宏与缓冲层落地。

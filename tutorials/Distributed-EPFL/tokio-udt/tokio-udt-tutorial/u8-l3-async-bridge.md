# 异步桥接：AsyncRead/AsyncWrite 与 Notify

## 1. 本讲目标

本讲聚焦 tokio-udt 的「异步外壳」：`UdtConnection` 是如何实现 `AsyncRead` / `AsyncWrite` 这两个 trait，从而让用户能像用 `TcpStream` 一样调用 `read` / `write` / `flush` / `shutdown` 的。

学完本讲，你应当能够：

1. 说清 `poll_read` / `poll_write` / `poll_flush` / `poll_shutdown` 四个 poll 方法各自委托给底层 `UdtSocket` 的哪个同步方法，以及何时返回 `Pending`。
2. 解释当 `poll_write` 遇到 `OutOfMemory`（发送缓冲满）、当 `poll_read` 暂时没有数据可读时，为什么选择 `tokio::spawn` 一个等待任务、再用 `Notify` 唤醒外层 waker，而不是「把 waker 注册进 socket」。
3. 辨析这种「spawn + Notify 唤醒」模式相比传统「intrusive waker」的优点与潜在开销，并理解它在 UDT 周期性事件下为何是「最终一致、自愈」的。

本讲承接 u2-l1（已建立 `UdtConnection` 是包裹 `Arc<UdtSocket>` 的薄壳、`connect` 走 `wait_for_connection` 的认知），深入到四个 poll 方法的实现细节。

## 2. 前置知识

- **Rust 的 `Future` 与 `Poll`**：`Future::poll` 返回 `Poll::Ready(T)`（完成）或 `Poll::Pending`（未就绪，需等被唤醒）。返回 `Pending` 的前提是「将来某个时刻一定会调用 `cx.waker().wake()`」，否则这个 future 永远不会被再次轮询，等同于永久挂起。这是本讲所有讨论的根基。
- **`AsyncRead` / `AsyncWrite` trait**：tokio 对「字节流读 / 写」的异步抽象。核心方法是 `poll_read(self, cx, buf)` 与 `poll_write(self, cx, buf)`，签名里都带一个 `cx: &mut Context<'_>`，它携带着当前 task 的 waker。
- **`tokio::sync::Notify`**：一个轻量的「事件通知」原语。`notify_waiters()` 唤醒**当前已注册**的所有等待者；`notified()` 返回一个 future，**第一次被 poll 时才注册** waker。注意：`notify_waiters()` **不会**像 `notify_one()` 那样预留一个 permit——这点在本讲的「潜在开销」分析里很关键。
- **`tokio::spawn`**：把一个 future 丢到 runtime 后台独立运行，立即返回一个 `JoinHandle`，调用者不必 await。本讲大量用它来「解耦 poll 与真正的等待」。
- **背压（backpressure）**：当生产者（写方）速度超过消费者（读方/网络）时，系统必须能反过来「卡住」生产者，而不是无限堆积。本讲中 `OutOfMemory` 就是发送侧背压的信号。

> 如果你尚未读过 u2-l1，请先确认：`UdtConnection` 内部只有一个字段 `socket: SocketRef`（即 `Arc<UdtSocket>`），所有逻辑都委托给它。

## 3. 本讲源码地图

本讲几乎只读一个文件，辅以少量 socket 侧支撑方法。

| 文件 | 作用 |
|------|------|
| [src/connection.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/connection.rs) | `UdtConnection` 全部实现，含 `AsyncRead` / `AsyncWrite` 的四个 poll 方法。本讲主角。 |
| [src/socket.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs) | `UdtSocket`。本讲用到它的同步方法 `send` / `poll_recv` / `snd_buffer_is_empty`，以及异步等待方法 `wait_for_data_to_read` / `wait_for_next_ack_or_empty_snd_buffer`，还有三个 `Notify` 字段及其 `notify_waiters()` 触发点。 |
| [src/queue/snd_buffer.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_buffer.rs) | `SndBuffer`。本讲用到 `add_message` 在缓冲满时返回 `OutOfMemory` 的逻辑，确认背压信号的来源。 |

一条贯穿全讲的认知主线：

> `UdtConnection` 的 poll 方法本身**不等待、不阻塞**——它只做一次同步探测；如果探测结果是「现在还不行」，就 `spawn` 一个后台任务去 `await` 一个 `Notify`，等 `Notify` 触发时再 `waker.wake()` 把外层 future 重新放回就绪队列。

## 4. 核心概念与源码讲解

### 4.1 为什么 poll 不能「直接等」：同步 socket 与异步外壳的缝隙

#### 4.1.1 概念说明

设计上有一个张力：

- 底层 `UdtSocket` 的核心收发方法（`send`、`poll_recv`、`snd_buffer_is_empty`）都是**同步的**——它们只看当前状态、立刻返回结果，不接收 `Context` / waker，也不会跨 `.await`。这是合理的设计：socket 是被多线程共享的 `Arc`，它的状态由后台收发 worker（rcv_queue / snd_queue 的 worker）持续驱动，与某个具体 task 的 waker 无关。
- 而 `UdtConnection` 要实现 `AsyncRead` / `AsyncWrite`，poll 方法签名强制带 `cx: &mut Context`，并且**返回 `Pending` 时必须保证将来会有人调用 `cx.waker().wake()`**。

这两者之间的「缝隙」正是本讲要桥接的地方：socket 不知道 waker，poll 又必须承诺唤醒。tokio-udt 选择的填缝方式就是「spawn 一个会 await Notify 的中间任务」。

#### 4.1.2 核心流程

四个 poll 方法的处理范式高度一致，可以抽象成下面这个伪代码：

```
fn poll_xxx(self, cx, ...) -> Poll<...> {
    // 第 1 步：同步探测底层 socket 的当前状态
    match self.socket.同步探测(...) {
        Ready(结果) => return Ready(结果),       // 现在就行，立即返回
        还不行 => {
            // 第 2 步：现在不行——但绝不能干等
            let waker = cx.waker().clone();        // 拿到当前 task 的 waker 副本
            let socket = self.socket.clone();      // Arc 克隆，move 进任务
            tokio::spawn(async move {
                socket.等待对应Notify().await;     // 后台 await，不阻塞 poll
                waker.wake();                      // 触发后唤醒外层 task
            });
            return Poll::Pending;                  // 承诺：spawn 的任务会唤醒你
        }
    }
}
```

关键三点：

1. poll 方法**本身不 `.await`**——它在「同步探测」后立刻返回，绝不占用线程。
2. `Pending` 的「将来一定唤醒」承诺，由 `spawn` 出去的那个任务兑现。
3. 唤醒外层 task 的机制不是「socket 存 waker」，而是「后台任务 await 一个 `Notify`，`Notify` 触发后调 `waker.wake()`」。

#### 4.1.3 源码精读

先看 `UdtConnection` 的结构，确认它真的只是一个薄壳：

[src/connection.rs:10-12](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/connection.rs#L10-L12) —— `UdtConnection` 只持有一个 `socket: SocketRef`（`Arc<UdtSocket>`），没有任何自己的缓冲或状态。这意味着所有「等」的逻辑都必须去问 socket。

再看三个 `Notify` 字段的声明，它们是本讲所有唤醒的物理来源：

[src/socket.rs:83-85](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L83-L85) —— 三个 `Notify`：`connect_notify`（等握手完成）、`rcv_notify`（等有数据可读）、`ack_notify`（等发送缓冲腾出空间）。本讲主要关心后两个。

> 小结：socket 是「waker 无感知」的同步状态机；poll 必须「承诺唤醒」。这个缝隙由 spawn + Notify 来填。下面三节分别落到 `poll_read/poll_write`、唤醒模式本身、`poll_flush/poll_shutdown`。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：确认「socket 的核心方法确实是同步的、不接 waker」。
2. **步骤**：打开 [src/socket.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs)，分别查看 `send`（984 行起）、`poll_recv`（1065 行起）、`snd_buffer_is_empty`（1149 行起）的签名。
3. **观察**：`send` 与 `snd_buffer_is_empty` 是普通 `fn`（同步返回），`poll_recv` 虽带 `Poll` 字样但**签名里没有 `cx` / waker**——它只是把「能不能立刻读到」这一瞬间的判断同步返回，并不承诺任何唤醒。
4. **结论**：正因为这些方法不持 waker，`UdtConnection` 的 poll 方法才必须在返回 `Pending` 时自己想办法「将来唤醒」。

#### 4.1.5 小练习与答案

**练习 1**：`Poll::Pending` 如果返回了，但永远没有人调用 `waker.wake()`，会发生什么？

> **答案**：这个 future（以及它所在的 task）将永远停在 `Pending`，不会再被 runtime 轮询，等同于永久挂起（俗称「丢唤醒 / lost wakeup」）。这正是本讲反复强调「必须承诺唤醒」的原因。

**练习 2**：为什么不在 poll 方法里直接 `socket.wait_for_xxx().await`？

> **答案**：因为 `poll` 是 `fn` 不是 `async fn`，且它是被外层 runtime 同步调用的入口，绝不能阻塞；而且 `poll_xxx` 的语义就是「快速探测、立刻返回」。直接在里面 `.await` 既不符合 trait 签名，也会把等待强塞进调用线程。所以等待被转移到一个独立 `spawn` 的任务里。

---

### 4.2 poll_read / poll_write：同步探测 + spawn 唤醒

#### 4.2.1 概念说明

这是本讲最核心的两个方法，分别对应「读」和「写」。

- **`poll_write`**：把用户要发的字节交给底层 `socket.send`。`send` 是「整条消息原子入队」的——它把整段 `buf` 切成 MSS 大小的块、共享同一个 msg_number 一次性塞进发送缓冲（详见 u5-l1）。因此 `poll_write` 成功时直接报告 `buf.len()` 全部写入（不像 TCP 那样可能只写一部分）。当发送缓冲**已满**时，`send` 会返回 `ErrorKind::OutOfMemory`，这就是发送侧的背压信号——此时 `poll_write` 必须返回 `Pending`，等缓冲腾出空间再让外层重试。
- **`poll_read`**：把底层 `socket.poll_recv` 的「现在能否读到」结果转译给 `AsyncRead`。当暂时没有数据可读时，`poll_recv` 返回 `Poll::Pending`，`poll_read` 便转入 spawn 唤醒分支，等新数据到达再唤醒。

#### 4.2.2 核心流程

**poll_write 决策树**：

```
poll_write(buf):
  调 socket.send(buf)
  ├─ Ok            → Ready(Ok(buf.len()))        # 全量入队成功
  └─ Err
       ├─ OutOfMemory → spawn wait_for_next_ack_or_empty_snd_buffer
       │                → wake() → return Pending  # 缓冲满，背压
       └─ 其它错误     → Ready(Err(该错误))         # 直接报错
```

**poll_read 决策树**：

```
poll_read(buf):
  调 socket.poll_recv(buf)
  ├─ Ready(Ok(n))  → Ready(Ok(()))                # 读到了
  ├─ Ready(Err)    → Ready(Err)                   # 出错（如断连）
  └─ Pending       → spawn wait_for_data_to_read
                      → wake() → return Pending   # 暂无数据，等
```

两条决策树形状几乎一样，区别只在于「不行」时 spawn 的等待对象不同：写侧等 `ack_notify`（缓冲腾位），读侧等 `rcv_notify`（数据到来）。

#### 4.2.3 源码精读

**poll_read** —— [src/connection.rs:98-117](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/connection.rs#L98-L117):

```rust
fn poll_read(self: Pin<&mut Self>, cx: &mut Context<'_>, buf: &mut ReadBuf<'_>) -> Poll<Result<()>> {
    match self.socket.poll_recv(buf) {
        Poll::Ready(res) => Poll::Ready(res.map(|_| ())),
        Poll::Pending => {
            let waker = cx.waker().clone();
            let socket = self.socket.clone();
            tokio::spawn(async move {
                socket.wait_for_data_to_read().await;
                waker.wake();
            });
            Poll::Pending
        }
    }
}
```

注意三处细节：① `cx.waker().clone()`——必须克隆，因为 waker 要被 move 进 spawned task；② `self.socket.clone()` 是廉价的 `Arc` 克隆；③ spawned 任务先 `await` 等待，再 `waker.wake()`——顺序不能反。

**poll_write** —— [src/connection.rs:119-137](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/connection.rs#L119-L137):

```rust
fn poll_write(self: Pin<&mut Self>, cx: &mut Context<'_>, buf: &[u8]) -> Poll<Result<usize>> {
    let buf_len = buf.len();
    match self.socket.send(buf) {
        Ok(_) => Poll::Ready(Ok(buf_len)),
        Err(err) => match err.kind() {
            ErrorKind::OutOfMemory => {
                let waker = cx.waker().clone();
                let socket = self.socket.clone();
                tokio::spawn(async move {
                    socket.wait_for_next_ack_or_empty_snd_buffer().await;
                    waker.wake();
                });
                Poll::Pending
            }
            _ => Poll::Ready(Err(err)),
        },
    }
}
```

只有 `OutOfMemory` 这一种错误会进入「等」分支；其它错误（`NotConnected`、`InvalidInput` 等）一律直接 `Ready(Err)`。

**背压信号从哪来** —— `send` 调用 `snd_buffer.add_message`，后者在缓冲满时返回 `OutOfMemory`：

[src/queue/snd_buffer.rs:71-79](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_buffer.rs#L71-L79):

```rust
pub fn add_message(&mut self, data: &[u8], ttl: Option<u64>, in_order: bool) -> IoResult<()> {
    let msg_number = self.next_msg_number;
    let now = Instant::now();
    let chunks = data.chunks(self.payload_size);
    let chunks_len = chunks.len();
    if self.buffer.len() + chunks_len > self.max_size as usize {
        return Err(Error::new(ErrorKind::OutOfMemory, "Send buffer is full"));
    }
    // ...
}
```

满的判定就是：

\[
\text{当前块数} + \text{本次新增块数} > \text{max\_size}
\]

其中 `max_size` 就是配置里的 `snd_buf_size`（默认 81920 包），在 [src/socket.rs:109](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L109) 处以 `SndBuffer::new(configuration.snd_buf_size)` 传入。这条 `OutOfMemory` 一路从 `add_message` → `send` → `poll_write` 冒泡上来，最终触发 spawn 等待。

**poll_recv 的同步探测** —— 确认它真的只做瞬间判断、不持 waker：[src/socket.rs:1087-1089](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1087-L1089) 中 `if !self.rcv_buffer().has_data_to_read() { return Poll::Pending; }`——只看「现在有没有」，没有就 `Pending`，把「将来怎么醒」完全甩给上层（即本讲的 spawn 模式）。

#### 4.2.4 代码实践（源码阅读型 + 可选小型实验）

1. **目标**：亲手观察「发送缓冲满 → OutOfMemory → spawn 等待 → ACK 腾位 → 唤醒」这条背压链。
2. **步骤（阅读型）**：
   - 在 [src/connection.rs:128](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/connection.rs#L128) 的 spawn 闭包里（`socket.wait_for_next_ack_or_empty_snd_buffer().await;` 之后、`waker.wake();` 之前）加一行 `eprintln!("[poll_write] 缓冲曾满，现已腾位，唤醒写者");`。
   - 在 [src/connection.rs:110](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/connection.rs#L110) 的 spawn 闭包里同样加 `eprintln!("[poll_read] 暂无数据，现已就绪，唤醒读者");`。
3. **步骤（运行型，待本地验证）**：跑通自带的 `cargo run --bin udt_receiver` 与 `cargo run --bin udt_sender`（见 u1-l2），把 sender 的发送量调大到足以填满 81920 包缓冲（例如制造一个「读得很慢」的 receiver——可以让 receiver 每收到一批数据 `sleep` 一会）。也可以写一个自定义 echo 程序：客户端用 `AsyncWriteExt::write_all` 灌入远超缓冲容量的数据，服务端故意延迟 `read`。
4. **观察与预期（待本地验证）**：当发送缓冲被填满时，stderr 应反复打印 `[poll_write] 缓冲曾满…`，说明背压链被触发；接收侧在数据断续到达时会打印 `[poll_read] 暂无数据…`。吞吐曲线应呈现「填满 → 卡住 → 腾位 → 继续」的锯齿。
5. **注意**：不要假装已经跑过——以上运行结果需在你的本地环境验证。阅读型部分（看链路）则不依赖运行。

#### 4.2.5 小练习与答案

**练习 1**：`poll_write` 成功时为什么直接返回 `Ok(buf_len)`，而不是像 TCP 那样可能返回一个小于 `buf_len` 的「实际写入字节数」？

> **答案**：因为底层 `send` → `add_message` 是「整段原子入队」的：要么整段 `buf` 全部切块进缓冲（成功），要么因为缓冲放不下而 `OutOfMemory`。不存在「只入队了一部分」的中间态，所以 `poll_write` 要么全成功（报告 `buf_len`）、要么 `Pending`（满）、要么 `Err`（其它错误）。调用方因此无需处理半写。

**练习 2**：除 `OutOfMemory` 外，`poll_write` 还可能直接 `Ready(Err)` 的两种常见情况是什么？

> **答案**：看 [src/socket.rs:984-996](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L984-L996)：① socket 不是 `Stream` 类型 → `InvalidInput`；② socket 状态不是 `Connected`（例如已 `Closing` / `Broken`）→ `NotConnected`。这两种都是确定性错误，直接上报，不进入 spawn 等待。

---

### 4.3 spawn + Notify 唤醒模式：wait_for_* 的角色与取舍

#### 4.3.1 概念说明

4.2 里反复出现的 `wait_for_data_to_read` 与 `wait_for_next_ack_or_empty_snd_buffer` 是本节主角。它们是 `UdtSocket` 上的 `pub(crate) async fn`，把「检查条件 + 订阅 Notify + 等待」打包成一个可 await 的整体，既被 `poll_read`/`poll_write` 的 spawn 闭包使用，也被 `recv` / `close` 等异步方法直接使用（同一份代码两种用法，避免重复）。

本节要回答 practice_task 的核心问题：**为什么不把 waker 直接注册进 socket，而要走 spawn + Notify？**

#### 4.3.2 核心流程

以 `wait_for_next_ack_or_empty_snd_buffer` 为例，它内部遵循经典的「**锁内查条件、锁外订阅等待**」模式：

```
wait_for_next_ack_or_empty_snd_buffer():
  锁 snd_buffer
  ├─ 缓冲已空 → 直接返回（None，不用等）          # 条件已满足
  └─ 缓冲非空 → 取出 ack_notify.notified() future
  释放锁
  await 该 future                                 # 此时才真正注册 waker
```

对应的「唤醒方」在后台 worker 里：

```
（接收 worker，process_ctrl 的 Ack 分支）
收到数据 ACK
  → snd_buffer.ack_data(offset)    # 弹出已确认的块，缓冲腾出空间
  → update_snd_queue(false)
  → ack_notify.notify_waiters()    # 唤醒所有等缓冲腾位的任务（含 spawn 出来的那个）
```

读侧对称：`process_data` 推进可读水位后调 `rcv_notify.notify_waiters()`（见下文源码）。

#### 4.3.3 源码精读

**写侧等待** —— [src/socket.rs:1237-1248](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1237-L1248):

```rust
pub(crate) async fn wait_for_next_ack_or_empty_snd_buffer(&self) {
    if let Some(notified) = {
        let snd_buffer = self.snd_buffer.lock().unwrap();
        if snd_buffer.is_empty() {
            None
        } else {
            Some(self.ack_notify.notified())
        }
    } {
        notified.await
    }
}
```

**读侧等待** —— [src/socket.rs:1205-1221](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1205-L1221)，结构完全同构：锁内查 `has_data_to_read()`，没有则订阅 `rcv_notify.notified()`，锁外 await。

**唤醒方（写）** —— 收到 ACK 腾出空间后唤醒：[src/socket.rs:547-555](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L547-L555):

```rust
self.snd_buffer.lock().unwrap().ack_data(offset);
// ...
state.last_data_ack_processed = seq;
self.update_snd_queue(false);
self.ack_notify.notify_waiters();   // ← 腾位后唤醒写侧等待者
```

**唤醒方（读）** —— 推进可读水位后唤醒：[src/socket.rs:817-819](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L817-L819):

```rust
self.rcv_buffer().ack_data(seq_number);
state.last_sent_ack = seq_number;
self.rcv_notify.notify_waiters();   // ← 有新数据可读，唤醒读侧等待者
```

#### 4.3.4 代码实践（设计辨析型 —— 本讲的 practice_task 核心）

**任务**：解释 `poll_write` 为何在 `OutOfMemory` 时返回 `Pending` 并 spawn `wait_for_next_ack_or_empty_snd_buffer`；对比 `poll_read` 的同类模式；并讨论这种实现相对「把 waker 注册进 socket」的优点与潜在开销。

**参考分析**：

*为什么 spawn `wait_for_next_ack_or_empty_snd_buffer`？* 因为 `OutOfMemory` 的唯一解药是「缓冲腾出空间」，而腾空间只发生在**收到数据 ACK** 时（`ack_data` 弹出已确认块）。`ack_notify` 正是由 ACK 分支触发的（上文 4.3.3），所以等它就是等「腾位事件」。`poll_read` 同理：没数据的唯一解药是「新数据到达」，而 `rcv_notify` 由 `process_data` 触发，所以等它。

*优点（相比「把 waker 存进 socket」）*：

1. **socket 保持 waker 无感知**。`UdtSocket` 是跨线程共享的 `Arc`，状态由后台 worker 驱动。如果改成「socket 内存一个 `Option<Waker>`」，就必须处理：waker 的 `Send + Sync`、多个 poller 抢着注册同一个槽、过期 waker 的替换、以及「该唤醒读还是写」的多槽管理。spawn + Notify 把这些复杂性完全挡在 socket 之外，socket 的核心方法（`send` / `poll_recv`）依旧是纯同步、可独立测试的。
2. **复用现有等待原语**。`wait_for_data_to_read` / `wait_for_next_ack_or_empty_snd_buffer` 本来就服务于 `recv` 和 `close`（见 u2-l1 的 `wait_for_connection` 同款写法）。poll 方法只是「包一层 spawn + wake」就接上了同一套机制，没有重复造轮子。
3. **解耦清晰**。「探测」在 poll（同步）里，「等待」在 spawned task（异步）里，「触发」在后台 worker（事件驱动）里，三者各司其职。

*潜在开销 / 风险*：

1. **每次 `Pending` 都 spawn 一个新任务**。任务有分配与调度成本。若外层 future 在仍 `Pending` 期间被多次轮询（虚假唤醒、或多个写者并发），会 spawn 出多个等待任务；它们在同一个 `notify_waiters()` 上**集体被唤醒**（轻度「惊群」），各自调一次 `waker.wake()`，外层被重复调度。多数情况下外层重试一次就成功，多余的 wake 是冗余开销。
2. **理论上存在「丢唤醒」窗口**。看 4.3.3 的写侧等待：先「锁内查缓冲非空」、再「锁外 await（首次 poll 才注册 waker）」。在「释放锁」与「首次 poll 注册 waker」之间，如果恰好一个 ACK 到达并调了 `ack_notify.notify_waiters()`，由于此刻还没有已注册的等待者、且 `notify_waiters()` 不像 `notify_one()` 那样预留 permit，这一次通知就丢了。该 spawned 任务可能就此挂住。
3. **但它不是「永久死锁」，而是「自愈」**：UDT 的 ACK 是周期性事件（`check_timers` 里还有 EXP 定时器、keep-alive 等周期触发，见 u7-l3），即便错过一次 `ack_notify`，下一个 ACK 会再次 `notify_waiters()` 把它唤醒。因此实际表现是「**轻微延迟重试**」而非「死锁」。读侧也类似——数据持续到达会反复触发 `rcv_notify`。这是该设计「能用」的关键前提：**依赖事件的高频性来弥补 `notify_waiters` 无 permit 的语义**。

> 结论：tokio-udt 用「正确性靠事件高频自愈、复杂度靠 spawn 下沉」换取了 socket 实现的简洁。在 UDT 这类「持续有 ACK / 数据流」的场景下是合理的工程取舍；但在「事件极稀疏」的连接上，开销与延迟会相对明显。

#### 4.3.5 小练习与答案

**练习 1**：`wait_for_next_ack_or_empty_snd_buffer` 为什么要在**持锁**时检查 `is_empty()`、却在**释放锁后**才 `await`？

> **答案**：检查条件必须在持锁时做，才能看到一致的缓冲状态（避免「检查时非空、刚放手就被 ACK 清空」的错配）。而 `await`（尤其首次 poll 注册 waker）可能让出执行权、耗时较长，绝不能在持锁时进行，否则会长时间占用 `snd_buffer` 的锁、阻塞发送主流程。所以采用「锁内取条件 + 取 future，锁外 await」的经典写法。

**练习 2**：如果把 `notify_waiters()` 换成 `notify_one()`，能消除 4.3.4 提到的「丢唤醒窗口」吗？

> **答案**：能缓解但不能完全等同于「waker 注册进 socket」。`notify_one()` 会预留一个 permit，使得「先 notify、后 notified()」的顺序也能被观察到，从而缩小丢唤醒窗口。但 spawn 模式的其它开销（每次 Pending 都新建任务、可能的惊群）依然存在。真正的「intrusive waker」方案（socket 持有并替换 waker、就绪时精确唤醒唯一一个）能避免这些开销，但代价是 socket 要承担 waker 生命周期管理——这正是 tokio-udt 刻意回避的复杂度。

**练习 3**：在「单向高速发送、对端只收不发」的场景里，发送方会收到 ACK 吗？`ack_notify` 还会被触发吗？

> **答案**：会。ACK 是**接收方**对收到的数据生成的反馈（见 u6-l2 的 `send_ack`），与「对端是否主动发数据」无关。只要接收方在收数据并回 ACK，发送方的 `process_ctrl` Ack 分支就会执行 `ack_notify.notify_waiters()`。所以即便单向传输，写侧的等待任务也能被正常唤醒。

---

### 4.4 poll_flush / poll_shutdown

#### 4.4.1 概念说明

- **`poll_flush`**：`AsyncWrite` 的语义是「把已写入的数据真正推出去」。对 tokio-udt 而言，「推出去」≈「发送缓冲排空」（即所有块都已被 ACK 确认并弹出）。所以 `poll_flush` 的判断极其简单：缓冲空了就 `Ready(Ok(()))`，否则 spawn 等待腾位。
- **`poll_shutdown`**：表示「我写完了，关闭写半边」。tokio-udt 把它实现为「触发 `socket.close()`」——而 `close()` 本身是 `async` 且可能 linger（逗留等待排空，见 u8-l2），所以 `poll_shutdown` 同样用 spawn 模式把 `close()` 推到后台。

> 重要区分：`flush` 只保证「发送缓冲清空」，**不等于**对端已收到或已确认到底；它不等价于 TCP 语义下严格的「对端 ack」。`shutdown` 则会真正进入关闭流程。

#### 4.4.2 核心流程

```
poll_flush:
  socket.snd_buffer_is_empty()?
  ├─ true  → Ready(Ok(()))                          # 已排空
  └─ false → spawn wait_for_next_ack_or_empty_snd_buffer
              → wake() → Pending                     # 等排空

poll_shutdown:
  status == Closed? → Ready(Ok(()))                  # 已关，幂等
  否则 → spawn socket.close().await
         → wake() → Pending                          # 后台关闭，完成后唤醒
```

注意 `poll_flush` 复用的等待对象与 `poll_write` 的 OutOfMemory 分支**完全相同**（都是 `wait_for_next_ack_or_empty_snd_buffer`）——因为两者等的都是「缓冲腾位/排空」这一类事件。

#### 4.4.3 源码精读

**poll_flush** —— [src/connection.rs:139-152](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/connection.rs#L139-L152):

```rust
fn poll_flush(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Result<()>> {
    match self.socket.snd_buffer_is_empty() {
        true => Poll::Ready(Ok(())),
        false => {
            let waker = cx.waker().clone();
            let socket = self.socket.clone();
            tokio::spawn(async move {
                socket.wait_for_next_ack_or_empty_snd_buffer().await;
                waker.wake();
            });
            Poll::Pending
        }
    }
}
```

**poll_shutdown** —— [src/connection.rs:154-165](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/connection.rs#L154-L165):

```rust
fn poll_shutdown(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Result<()>> {
    if self.socket.status() == UdtStatus::Closed {
        return Poll::Ready(Ok(()));
    }
    let socket = self.socket.clone();
    let waker = cx.waker().clone();
    tokio::spawn(async move {
        socket.close().await;
        waker.wake();
    });
    Poll::Pending
}
```

注意 `poll_shutdown` 多了一个「已 `Closed` 则直接返回」的幂等守卫——避免重复关闭。它 spawn 的 `socket.close()` 是 u8-l2 详述的完整关闭流程（linger 等待 → 发 Shutdown 包 → 置 `Closing` → notify_all）。`close()` 收尾时会调 `notify_all`：

[src/socket.rs:1199-1203](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1199-L1203):

```rust
fn notify_all(&self) {
    self.accept_notify.notify_waiters();
    self.rcv_notify.notify_waiters();
    self.connect_notify.notify_waiters();
}
```

> 观察细节：`notify_all` 唤醒的是 `accept_notify` / `rcv_notify` / `connect_notify`，**不包含** `ack_notify`。这意味着关闭时主要唤醒「等数据 / 等握手 / 等 accept」的等待者；写侧等待者（等 `ack_notify`）不在 `notify_all` 范围内——这进一步印证 4.3 的结论：写侧等待更多依赖 ACK 事件的高频性，而非关闭时的统一唤醒。

#### 4.4.4 代码实践（源码阅读型）

1. **目标**：理解 `poll_flush` 与 `poll_write` 为何共用同一个等待对象，以及 `poll_shutdown` 的幂等性。
2. **步骤**：
   - 对比 [src/connection.rs:128-131](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/connection.rs#L128-L131)（`poll_write` 的等待闭包）与 [src/connection.rs:145-148](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/connection.rs#L145-L148)（`poll_flush` 的等待闭包），确认它们字节级几乎相同。
   - 读 [src/socket.rs:1149-1151](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1149-L1151)，确认 `snd_buffer_is_empty()` 就是 `self.snd_buffer.lock().unwrap().is_empty()`——`poll_flush` 的「已排空」判定与 `wait_for_next_ack_or_empty_snd_buffer` 内部的「提前返回」判定用的是同一个 `is_empty()`。
3. **观察与预期**：两者共用 `wait_for_next_ack_or_empty_snd_buffer`，是因为「缓冲腾位」和「缓冲排空」本质是同一类事件的两个观察角度——都在等 `ack_notify`。`poll_shutdown` 在 `Closed` 时直接 `Ready`，保证多次 shutdown 不重复进入关闭流程。
4. **运行型（待本地验证）**：在一个客户端里依次调用 `write_all` → `flush().await` → `shutdown().await`，配合 4.2.4 加的日志，观察 flush 期间是否打印腾位日志、shutdown 后 socket 状态是否变为 `Closing`/`Closed`。

#### 4.4.5 小练习与答案

**练习 1**：`poll_flush` 返回 `Ready(Ok(()))` 是否意味着对端已经收到并确认了所有数据？

> **答案**：不完全是。`poll_flush` 的判定是「发送缓冲为空」，即所有块都已被**本侧的 ACK 处理弹出**（`ack_data`）。一个块能被 `ack_data` 弹出，意味着收到了覆盖它的数据 ACK——所以「缓冲空」确实意味着「已确认」。但要强调：它不等同于 TCP 那种由内核保证的严格语义，且 `flush` 之后若再 `write`，缓冲又会非空。本讲把它表述为「发送缓冲排空」更准确。

**练习 2**：为什么 `poll_shutdown` 要先判 `status == Closed`？

> **答案**：为了幂等。`AsyncWriteExt::shutdown` 可能被多次调用（例如 Drop 时再调一次）。若不拦截，会重复 spawn `close()`，而 `close()` 内部虽有自身的幂等守卫（u8-l2：`Closed`/`Closing` 直接返回），但提前在外层短路能避免无谓的 spawn 开销，语义也更清晰。

**练习 3**：`close()` 的 `notify_all` 不唤醒 `ack_notify`，这对一个正在 `poll_flush`（等缓冲排空）的 task 有什么影响？

> **答案**：理论上，若缓冲在关闭瞬间恰好非空、且之后不再有新 ACK，`poll_flush` 的 spawned 等待任务不会被 `notify_all` 唤醒。但实际上关闭流程（linger 循环）本身会 await `wait_for_next_ack_or_empty_snd_buffer`，且残留数据要么被重传/确认、要么随 `Broken`/超时被清理，后续事件仍会触发 `ack_notify`。这再次体现了「依赖事件高频自愈」的设计前提；在边界情况下 flush 的最终唤醒可能有延迟。

## 5. 综合实践

**任务：把四个 poll 方法串成一条「带背压的回显」链路，并画出唤醒时序。**

1. **阅读阶段**：以本讲四个 poll 方法为骨架，把它们的「同步探测点 → spawn 的等待对象 → 唤醒方」整理成一张表：

   | poll 方法 | 同步探测点 | 不行时 spawn 的等待 | 唤醒方（notify_waiters 调用处） |
   |-----------|-----------|---------------------|--------------------------------|
   | `poll_read` | `socket.poll_recv` | `wait_for_data_to_read` | `process_data` 的 `rcv_notify`（[socket.rs:819](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L819)） |
   | `poll_write` | `socket.send`（`OutOfMemory`） | `wait_for_next_ack_or_empty_snd_buffer` | `process_ctrl` Ack 的 `ack_notify`（[socket.rs:555](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L555)） |
   | `poll_flush` | `socket.snd_buffer_is_empty` | 同 `poll_write` | 同 `poll_write` |
   | `poll_shutdown` | `status == Closed` | `socket.close()` | close 内 `notify_all`（[socket.rs:1199-1203](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1199-L1203)） |

2. **时序图阶段**：画出「写满 → 背压 → ACK 到达 → 唤醒 → 继续写 → flush → shutdown」的完整时序，标注每一步哪个 `Notify` 被谁触发、哪个 spawned 任务被唤醒、哪个外层 waker 被 `wake()`。

3. **实验阶段（待本地验证）**：基于 README 的客户端/服务端示例，写一个「快速写、慢速读」的回显程序：客户端用 `write_all` 灌入远超 81920 包的数据，服务端每次 `read` 后 `sleep`。在四个 spawn 闭包里各加一行 `eprintln!`，运行后核对：背压期间应看到 `poll_write` 的腾位日志反复出现；读侧应看到 `poll_read` 的就绪日志；最后 `flush`/`shutdown` 各打印一次。

4. **反思阶段**：基于观察，用一两句话回答：在本场景下，spawn + Notify 的「自愈」是否足够及时？如果连接是「极低频事件」（例如几分钟才一个包），这套模式会出现什么问题？

## 6. 本讲小结

- `UdtConnection` 是包裹 `Arc<UdtSocket>` 的薄壳；四个 poll 方法都遵循「**同步探测 → 行就 Ready，不行就 spawn 一个等 Notify 的任务再 wake 外层**」的统一范式。
- `poll_write` 只在 `OutOfMemory`（发送缓冲满，来自 [snd_buffer.rs:78](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_buffer.rs#L78)）时返回 `Pending` 并 spawn `wait_for_next_ack_or_empty_snd_buffer`；其余错误直接 `Ready(Err)`。成功时整段入队、报告 `buf_len` 全量写入。
- `poll_read` 把 `socket.poll_recv` 的瞬间结果转译给 `AsyncRead`，无数据时 spawn `wait_for_data_to_read`（订阅 `rcv_notify`）。
- 写侧 `ack_notify` 由收到数据 ACK 时触发（[socket.rs:555](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L555)），读侧 `rcv_notify` 由 `process_data` 推进可读水位时触发（[socket.rs:819](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L819)）。
- 选择 spawn + Notify 而非「waker 注册进 socket」，是为了让 socket 保持 waker 无感知、纯同步可测；代价是每次 `Pending` 都新建任务、有轻度惊群，且 `notify_waiters` 无 permit 导致理论上存在丢唤醒窗口——但 UDT 的周期性 ACK/EXP 事件使其「自愈」，表现为轻微延迟而非死锁。
- `poll_flush` 判缓冲是否排空（与写侧共用等待对象），`poll_shutdown` spawn `close()` 并对 `Closed` 幂等短路。

## 7. 下一步学习建议

- **向「关闭」深入**：本讲的 `poll_shutdown` 只是入口，真正的 `close()` 流程（linger 等待、Shutdown 包、状态迁移、GC）在 **u8-l2** 已详述，可对照阅读以补全「shutdown 之后发生了什么」。
- **向「握手」深入**：`connect_notify` 在本讲只一笔带过，完整的 `Init → Opened → Connecting → Connected` 握手与 `wait_for_connection` 见 **u2-l1** 与 **u8-l1**（SYN cookie）。
- **向「事件驱动」深入**：本讲反复强调「唤醒靠后台 worker 的事件」。这些事件（ACK 生成、`check_timers`、EXP/keep-alive）的来源在 **u6-l2**（接收与 ACK）、**u7-l3**（定时器）中系统讲解，读完会更清楚 spawn 任务「到底在等谁」。
- **动手对比**：可尝试在本地的 fork 里把某个 poll 方法改写成「intrusive waker」（在 socket 上加一个 `Waker` 槽），实测它在「事件稀疏」连接下的延迟差异，从而切身理解本讲讨论的工程取舍。

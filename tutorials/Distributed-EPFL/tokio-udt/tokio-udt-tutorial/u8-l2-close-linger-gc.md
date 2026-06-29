# 关闭、linger 与垃圾回收

## 1. 本讲目标

本讲聚焦 UDT 连接的「最后一公里」：连接如何被优雅地关闭、半开/损坏的连接如何被回收。读完本讲你应当能够：

- 说清 `UdtSocket::close()` 的执行顺序：linger 等待 → 摘除调度 → 发 Shutdown → 状态迁移 → 唤醒等待者。
- 解释 linger 循环的三个退出条件，以及 `linger_timeout` 为 `None` 与 `Some(...)` 时的行为差异。
- 理解为什么「发 Shutdown」「置 `Closing`」这些事由 socket 自己做，而「从全局注册表删除」「置 `Closed`」却要交给后台 GC。
- 复述 `garbage_collect_sockets` 为何分成 Broken（spawn 一次 `close()`）与 Closing（直接摘除并置 `Closed`）两个阶段。

本讲是专家层「连接生命周期」的第二篇，承接 u8-l1（握手建立），覆盖连接生命周期的终点。

## 2. 前置知识

本讲假设你已经掌握（来自 u3-l1、u3-l2）：

- **全局 `Udt` 单例**：进程级 `OnceCell<RwLock<Udt>>`，持有 `sockets`、`multiplexers`、`peers` 三张注册表，并跑一个每秒一次的 `cleanup_worker`。
- **`UdtSocket` 结构**：每条连接的核心对象，字段分身份、数据通路、拥塞控制、簿记、异步唤醒几类；其中 `multiplexer` 字段是 `RwLock<Weak<UdtMultiplexer>>`（弱引用），三个 `Notify`（`connect_notify`/`rcv_notify`/`ack_notify`）用于异步唤醒。
- **`UdtStatus` 状态机**：八态（`Init`/`Opened`/`Listening`/`Connecting`/`Connected`/`Broken`/`Closing`/`Closed`），`is_alive()` 排除 `Broken`/`Closing`/`Closed`。
- **发送缓冲 `SndBuffer`**：只保存**尚未被 ACK 的数据块**（u5-l1），`ack_data` 推进队首水位；缓冲为空 ⟺ 所有已发数据都已被对端确认。

补充两个本讲要用的小术语：

- **linger（逗留）**：关闭时先不立即断开，而是等待一段缓冲排空的时间，尽量把未确认的数据可靠送达。
- **Shutdown 包**：UDT 控制包的一种（类型码 `0x0005`），专门用于通知对端「我要关闭了」，是对称关闭的信令。

## 3. 本讲源码地图

| 文件 | 本讲关注的点 |
| --- | --- |
| `src/socket.rs` | `close()` 主流程、linger 循环、`wait_for_next_ack_or_empty_snd_buffer`、`notify_all`、收包侧的 Shutdown 分支、`UdtStatus`/`is_alive` |
| `src/udt.rs` | `garbage_collect_sockets` 两阶段回收、`cleanup_worker` 的 1 秒节奏、`Udt::get` 启动 worker |
| `src/configuration.rs` | `linger_timeout` 字段定义与默认值 |
| `src/control_packet.rs` | `new_shutdown` 构造、Shutdown 的类型码 |
| `src/connection.rs` | `UdtConnection::close` 与 `poll_shutdown` 如何委托给 socket |
| `src/queue/snd_queue.rs` | `UdtSndQueue::remove`——从共享调度堆里摘除本 socket 的事件 |

## 4. 核心概念与源码讲解

本讲拆为三个最小模块：**close + linger**、**Shutdown 包 / 状态迁移**、**garbage_collect_sockets**。

### 4.1 close + linger：优雅关闭的入口

#### 4.1.1 概念说明

关闭一条可靠传输连接，难点不在「断开」，而在「**断开前要不要把没确认的数据送出去**」。TCP 用 SO_LINGER 选项解决这个问题；UDT 同样设计了 linger 语义：调用 `close()` 时，先在 `linger_timeout` 时长内等待发送缓冲排空，超时则放弃未确认的数据。

`UdtSocket::close()` 是这一切的入口。它由两条路径触发：

1. 用户主动关闭：`UdtConnection::close()`（[connection.rs:89-91](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/connection.rs#L89-L91)）或 `AsyncWrite::poll_shutdown`（[connection.rs:154-165](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/connection.rs#L154-L165)），最终都委托给 `socket.close()`。
2. 全局 GC 触发：对 `Broken` 连接，GC 会 spawn 一个任务去跑 `close()`（见 4.3）。

#### 4.1.2 核心流程

`close()` 的执行顺序固定为五步：

```
close()
  │
  ├─ ① 幂等守卫：已 Closed / Closing → 直接返回
  │
  ├─ ② 读取 linger_timeout（None → Duration::ZERO）
  │
  ├─ ③【linger 循环】
  │     while 状态==Connected  &&  发送缓冲非空  &&  未超时:
  │         await ack_notify   # 等下一个 ACK 推进水位，或缓冲变空
  │
  ├─ ④ 拆调度：从 mux.snd_queue 移除本 socket 的事件
  │        若本 socket 正是 mux 的 listener → 清空 mux.listener
  │
  ├─ ⑤ 仍是 Connected？→ best-effort 发一个 Shutdown 控制包
  │
  ├─ ⑥ status = Closing
  └─ ⑦ notify_all()：唤醒 accept / read / connect 等待者
```

linger 循环的退出条件是三个谓词的逻辑**与**，任一为假即跳出：

\[ \text{继续逗留} \;=\; (\text{status} = \text{Connected}) \;\land\; \neg\,\text{snd\_buffer\_is\_empty}() \;\land\; (\text{elapsed} < \text{linger\_timeout}) \]

三个退出条件的含义：

1. **状态离开 `Connected`**：linger 期间对端发来了 Shutdown（我们被切到 `Closing`），或连接被判 `Broken`——既然已经断了，没必要再等。
2. **缓冲变空**：所有未确认数据都已被 ACK，优雅送达完成，正常退出。
3. **超时**：`linger_timeout` 用尽，放弃剩余未确认数据，强制往下走。

#### 4.1.3 源码精读

先看 `close()` 的幂等守卫与 linger 配置读取：

[socket.rs:1153-1164](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1153-L1164) —— 入口先读状态，若是 `Closed`/`Closing` 直接 `return`（幂等，重复关闭无副作用）；否则记录起始时刻 `now`，从配置读 `linger_timeout`，**`None` 视作 `Duration::ZERO`**。

```rust
pub async fn close(&self) {
    let status = self.status();
    if status == UdtStatus::Closed || status == UdtStatus::Closing {
        return;
    }
    let now = Instant::now();
    let linger_timeout = self
        .configuration.read().unwrap().linger_timeout
        .unwrap_or(Duration::ZERO);
```

注意 `unwrap_or(Duration::ZERO)` 这一行：尽管 `Default` 实现里 `linger_timeout` 默认是 `Some(10s)`（[configuration.rs:46-47](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/configuration.rs#L46-L47)、[configuration.rs:66](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/configuration.rs#L66)），但如果用户显式传 `None`，则 **完全不逗留**——linger 循环条件 `elapsed < 0` 永远为假，循环体一次都不执行，立即跳到摘除调度与发 Shutdown。

接着是 linger 循环本体：

[socket.rs:1166-1171](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1166-L1171) —— 三个谓词的 `while`，循环体 `await wait_for_next_ack_or_empty_snd_buffer()`。

```rust
while self.status() == UdtStatus::Connected
    && !self.snd_buffer_is_empty()
    && now.elapsed() < linger_timeout
{
    self.wait_for_next_ack_or_empty_snd_buffer().await;
}
```

`wait_for_next_ack_or_empty_snd_buffer` 是典型的「锁内判条件、锁外订阅 future」模式（tokio 版条件变量）：

[socket.rs:1237-1248](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1237-L1248) —— 锁住 `snd_buffer`，若已空直接返回（外层循环重判后退出）；否则订阅 `ack_notify`，锁外 `await`。

```rust
pub(crate) async fn wait_for_next_ack_or_empty_snd_buffer(&self) {
    if let Some(notified) = {
        let snd_buffer = self.snd_buffer.lock().unwrap();
        if snd_buffer.is_empty() { None }
        else { Some(self.ack_notify.notified()) }
    } {
        notified.await
    }
}
```

`ack_notify` 何时被唤醒？在处理 ACK、推进发送缓冲水位时（见 u6-l4）。每来一个 ACK，已确认的块从 `SndBuffer` 移除，缓冲逐步变空；最终 `snd_buffer_is_empty()` 为真，linger 循环正常退出——这就是「优雅送达后关闭」。

`snd_buffer_is_empty` 的实现只是锁住缓冲判空（[socket.rs:1149-1151](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1149-L1151)）。记住：`SndBuffer` 只存**未确认**块，所以「空」即「全部已确认」。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `linger_timeout` 取值对关闭行为的影响。

**操作步骤**：

1. 打开 [socket.rs:1153-1197](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1153-L1197)，在 linger 循环进入处、循环每次醒来后、退出循环处各加一行 `eprintln!`（示例代码，**不要提交**）：

   ```rust
   // 示例代码：仅用于本地观察，勿提交
   eprintln!("[close] enter linger, empty={}, elapsed={:?}",
             self.snd_buffer_is_empty(), now.elapsed());
   ```

2. 基于仓库自带的 `udt_sender`/`udt_receiver`（见 u1-l2），让 sender 在发送若干数据后**真正调用** `connection.close().await`（仓库里这行目前被注释掉，见 `src/bin/udt_sender.rs`）。

3. 分别用两份配置跑 sender：一份默认 `linger_timeout = Some(10s)`，一份显式设 `None`。

**需要观察的现象**：

- 默认配置下，`[close] enter linger` 会重复打印，且 `empty` 从 `false` 逐步变 `true`，最终退出——这说明 close 等到了缓冲排空。
- `None` 配置下，循环体**一次都不执行**，直接跳到发 Shutdown；若此刻缓冲非空，那部分数据被丢弃。

**预期结果 / 待本地验证**：在回环（localhost）环境下 ACK 几乎瞬时，默认 10s linger 通常在毫秒级就因「缓冲空」退出，看不出超时；要观察「超时退出」，需人为制造高延迟或大缓冲。若无法构造该环境，明确记为「待本地验证」，重点理解三个退出条件的逻辑即可。

#### 4.1.5 小练习与答案

**练习 1**：linger 循环里为什么用 `self.status() == UdtStatus::Connected` 作为条件，而不是只判缓冲和超时？

**参考答案**：linger 期间对端可能主动发来 Shutdown（把我们切到 `Closing`）或连接被判 `Broken`。此时继续等 ACK 已无意义（对端不会再回 ACK），用状态作条件能在这种情况下**立即跳出**，避免无谓等待满 `linger_timeout`。

**练习 2**：把 `linger_timeout` 设为 `None` 与设为 `Some(Duration::ZERO)`，行为有区别吗？

**参考答案**：没有区别。`None` 经 `unwrap_or(Duration::ZERO)` 变成 `Duration::ZERO`，`Some(Duration::ZERO)` 本身就是零；两者的 `elapsed < linger_timeout`（即 `elapsed < 0`）恒为假，linger 循环都不执行。

### 4.2 Shutdown 包与状态迁移

#### 4.2.1 概念说明

linger 结束后，`close()` 做两件「对外可见」的事：**给对端发一个 Shutdown 控制包**，然后**把自己的状态翻成 `Closing`** 并唤醒所有等待者。Shutdown 是 UDT 关闭的对称信令——A 关闭时给 B 发 Shutdown，B 收到后也进入关闭流程。关闭因此是双向的。

注意状态迁移的**终点不是 `Closed`，而是 `Closing`**。`close()` 不会把状态置成 `Closed`，也不会把自己从全局 `sockets` 注册表里删掉——那是 GC 的活（见 4.3）。这是一个刻意的设计分工。

#### 4.2.2 核心流程

发送侧（`close()` 内）：

```
摘除 mux.snd_queue 后
  └─ 仍是 Connected？ ─是→ new_shutdown(peer_socket_id) → send_packet（best-effort）
                     └─ 置 status = Closing → notify_all()
```

接收侧（`process_ctrl` 的 Shutdown 分支，对端发来的 Shutdown 到达时）：

```
收到 Shutdown 控制包
  └─ 置 status = Closing → notify_all()
```

合法的状态迁移路径汇总（关闭相关）：

\[ \text{Connected} \;\xrightarrow{\text{close() / 收到 Shutdown}}\; \text{Closing} \;\xrightarrow{\text{GC 阶段二}}\; \text{Closed} \]

\[ \text{任意活态} \;\xrightarrow{\text{长时间无响应}}\; \text{Broken} \;\xrightarrow{\text{GC 阶段一 spawn close()}}\; \text{Closing} \;\xrightarrow{\text{GC 阶段二}}\; \text{Closed} \]

#### 4.2.3 源码精读

先看发送 Shutdown 与状态迁移的代码：

[socket.rs:1183-1196](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1183-L1196) —— 注意 `if self.status() == UdtStatus::Connected` 这个**二次判断**：只有 linger 之后仍是 `Connected` 才发 Shutdown；若 linger 期间已被对端切到 `Closing` 或判 `Broken`，就不再发（避免给一个已经断开/正在断开的连接重复发信令）。

```rust
if self.status() == UdtStatus::Connected {
    let shutdown = UdtControlPacket::new_shutdown(self.peer_socket_id().unwrap());
    self.send_packet(shutdown.into()).await.unwrap_or_else(|err| {
        if *UDT_DEBUG { eprintln!("Failed to send shutdown packet: {}", err); }
    });
}
*self.status.lock().unwrap() = UdtStatus::Closing;
self.notify_all();
```

Shutdown 包的构造非常简单——固定头里 `packet_type = Shutdown`，无额外负载：

[control_packet.rs:78-86](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L78-L86) —— `dest_socket_id` 取对端 socket id（让对端的接收 worker 能按 `dest_socket_id` 路由到对应 socket）。

Shutdown 是 best-effort 的：`send_packet` 失败时 `unwrap_or_else` 只在 `UDT_DEBUG` 开启时打印一行，**不影响关闭流程**——UDT 建立在不可靠 UDP 上，Shutdown 丢了的话，对端最终会靠 EXP 超时（u7-l3）进入 `Broken` 再被 GC，不依赖这一个包。

再看接收侧——对端发来的 Shutdown 到达时：

[socket.rs:653-656](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L653-L656) —— `process_ctrl` 的 Shutdown 分支：直接置 `Closing` 并 `notify_all`，**不调用本地的 `close()`**。

```rust
ControlPacketType::Shutdown => {
    *self.status.lock().unwrap() = UdtStatus::Closing;
    self.notify_all();
}
```

这有一层含义：被对端关闭的一方，只做「置 Closing + 唤醒」，不会 linger、不会主动发 Shutdown、不会从 snd_queue 摘除自己。它的最终清理同样靠 GC（4.3）兜底——这也解释了为什么 GC 必须能处理「没经过 `close()` 直接进入 `Closing`」的 socket。

`notify_all` 把三个 `Notify` 一并唤醒（[socket.rs:1199-1203](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1199-L1203)）：`accept_notify`（等服务端 accept 的人）、`rcv_notify`（等读数据的人）、`connect_notify`（等握手的人）。它们醒来后会重判状态，发现 `is_alive()` 为假（[socket.rs:1283-1287](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1283-L1287)），从而向上层返回关闭/错误。

#### 4.2.4 代码实践

**实践目标**：理解关闭的对称性，并验证「Shutdown best-effort」。

**操作步骤**：

1. 阅读 [socket.rs:1183-1196](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1183-L1196) 与 [socket.rs:653-656](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L653-L656)，在纸上画出 A、B 双方都调用 `close()` 时控制包的流向：A→B 发 Shutdown（B 的 `process_ctrl` 置 Closing），B→A 发 Shutdown（A 的 `process_ctrl` 置 Closing）。
2. 设置环境变量 `UDT_DEBUG=1` 跑 sender/receiver（`UDT_DEBUG` 在 [udt.rs:18-19](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L18-L19) 开启），强制让对端先于 Shutdown 包关闭，观察是否出现 `Failed to send shutdown packet` 日志。

**需要观察的现象**：正常双向关闭时无错误日志；若制造 Shutdown 发送失败，会看到该日志，但关闭流程仍继续。

**预期结果 / 待本地验证**：回环环境很难让 `send_packet` 失败；可改用「先 kill 对端进程再 close」近似。若无法稳定复现，记为「待本地验证」，重点理解 `unwrap_or_else` 不阻断关闭这一设计。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `close()` 里发 Shutdown 之前要再判一次 `status == Connected`，而不是 linger 一结束就直接发？

**参考答案**：linger 循环可能因「状态离开 Connected」而退出（对端在 linger 期间发来 Shutdown，或被判 Broken）。这种情况下连接已经在断开流程中，再发一个 Shutdown 是多余的，甚至可能发到一个已经无效的对端。二次判断保证只在「自己主动关闭、且对端尚未关闭」时才发 Shutdown。

**练习 2**：`close()` 结束时状态是 `Closing` 还是 `Closed`？谁负责把它变成 `Closed`？

**参考答案**：`close()` 结束时状态是 `Closing`。`Closed` 由全局 `garbage_collect_sockets` 的阶段二负责——它从 `sockets` 注册表删除该 socket 并把状态置为 `Closed`。

### 4.3 garbage_collect_sockets：全局注册表的最终清理

#### 4.3.1 概念说明

`close()` 只持有 `&self`（一个 socket 引用），它**够不着**全局 `Udt` 单例里的 `sockets` 注册表——把自己从那张 `BTreeMap` 里删掉，需要拿到全局 `Udt` 的**写锁**。而 `close()` 本身可能在 linger、发包，再去抢全局写锁会有锁顺序与死锁风险。于是 tokio-udt 把「从注册表删除」这件事**外包给一个后台 GC worker**：`close()` 只负责把状态翻到 `Closing`，GC 周期性地扫一遍注册表，把 `Closing`/`Broken` 的 socket 收掉。

GC 分两个阶段，处理两类「该被回收」的 socket：

- **`Broken`**：连接已损坏（如对端长时间无响应，由 EXP 定时器判定，见 u7-l3）。这种 socket 还**没经过 `close()`**，需要 GC 帮它补做一次关闭。
- **`Closing`**：已经经过 `close()`（用户主动、收到对端 Shutdown、或上一轮 GC 对 Broken 补做的 close）。

#### 4.3.2 核心流程

```
cleanup_worker（每 1 秒一轮，持有 Udt 写锁）:
  garbage_collect_sockets():
    ┌─ 阶段一：处理 Broken
    │    for 每个 status==Broken 的 socket:
    │        若有 listen_socket → 从该 listener 的 queued_sockets 摘除
    │        tokio::spawn { sock.close().await }   # 不 await，立即返回
    │
    └─ 阶段二：处理 Closing
         for 每个 status==Closing 的 socket:
             从 self.sockets 删除
             status = Closed
```

为什么阶段一要 `spawn` 而不是直接 `await close()`？因为此时**正持有全局 `Udt` 写锁**，而 `close()` 是 `async`、可能 linger 长达 10 秒、还要发包、抢其它锁。若直接 `await`，全局写锁会被占住数秒，**阻塞所有其它 socket 的 bind/accept/connect/new_connection**。`spawn` 把 `close()` 丢到后台任务，GC 立即放手写锁往下走。被 spawn 的 `close()` 会在后台把状态翻到 `Closing`，等下一轮 GC 的阶段二再摘除。

为什么阶段二能直接做、不用 spawn？阶段二只是「从 map 删一项 + 改一个状态字段」，纯内存、无 `await`、瞬时完成，在持锁状态下做是安全的。

#### 4.3.3 源码精读

GC 的两个阶段：

[udt.rs:227-259](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L227-L259) —— 先看阶段一（Broken）：

```rust
for (_, sock) in self.sockets.iter()
    .filter(|(_, s)| s.status() == UdtStatus::Broken)
{
    if let Some(listen_socket_id) = sock.listen_socket {
        if let Some(listener) = self.sockets.get(&listen_socket_id) {
            listener.queued_sockets.write().await.remove(&sock.socket_id);
        }
    }
    tokio::spawn({
        let sock = sock.clone();
        async move { sock.close().await }
    });
}
```

阶段一做两件事：

1. 若这个 Broken socket 是被某个 listener accept 出来、还排在 `queued_sockets` 里（即还没被 `accept` 取走），先把它从队列摘掉——否则 listener 的 accept 队列里会留一个永远取不出的僵尸 id，后续 `accept` 取到它会因已 `Closed` 而报错（u2-l2 提到的竞态）。
2. `tokio::spawn` 一个任务跑 `sock.close()`——**注意没有 `await`**。这一步正是「 Broken → 补做 close → Closing」的桥梁。

阶段二（Closing）：

```rust
let to_remove: Vec<_> = self.sockets.iter()
    .filter(|(_, s)| s.status() == UdtStatus::Closing)
    .map(|(socket_id, _)| *socket_id)
    .collect();
for socket_id in to_remove {
    if let Some(sock) = self.sockets.remove(&socket_id) {
        *sock.status.lock().unwrap() = UdtStatus::Closed;
    }
}
```

阶段二先把待删 id 收集到 `Vec`（避免在迭代 `BTreeMap` 时修改它），再逐个 `remove` 并置 `Closed`。`self.sockets.remove` 返回的 `Arc<UdtSocket>` 在循环结束、所有强引用消失后被 drop，socket 真正释放。

为什么用 `to_remove` 先收集再删？因为不能在 `BTreeMap` 的迭代过程中修改它（Rust 的借用规则会直接拒绝）。这是「收集 → 改」的标准模式。

GC worker 的 1 秒节奏：

[udt.rs:261-269](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L261-L269) —— 无限循环：拿 `Udt` 写锁跑一次 GC，再 `sleep` 1 秒。

```rust
fn cleanup_worker() {
    tokio::spawn(async {
        let udt = Self::get();
        loop {
            udt.write().await.garbage_collect_sockets().await;
            sleep(std::time::Duration::from_secs(1)).await;
        }
    });
}
```

`cleanup_worker` 在 `Udt::get()` 首次初始化单例时被 `spawn`（[udt.rs:37-42](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L37-L42)），整个进程只跑一个。

**1 秒节奏是工程取舍**：

- **够频繁**：Closing/Broken 的 socket 不会在注册表里堆积太久，缓冲与相关资源能及时释放。
- **够稀疏**：每次 GC 要抢全局 `Udt` 写锁，1 秒一次不会与正常的 bind/accept/connect 频繁争锁。处于 Closing/Broken 的 socket 本就不再处理业务流量，晚 1 秒回收完全可接受。

补一个细节：4.1 里 `close()` 还做了「从 `mux.snd_queue` 移除」和「清空 `mux.listener`」。这两个操作不在 GC 里、而在 `close()` 里，是因为它们操作的是 **multiplexer 的内部结构**（`close()` 通过 `multiplexer()` 弱引用升级拿到 mux 的 `Arc`，[socket.rs:222-224](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L222-L224)），不碰全局 `Udt` 锁，因此可以安全地在 `close()` 内完成。看一下这段：

[socket.rs:1173-1179](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1173-L1179) —— 从共享调度堆里摘除本 socket 的待发事件；若自己正是该 mux 的 listener，清空 `mux.listener`。

```rust
if let Some(mux) = self.multiplexer() {
    mux.snd_queue.remove(self.socket_id);
    let listener_id = mux.listener.read().await.clone().map(|s| s.socket_id);
    if listener_id == Some(self.socket_id) {
        *mux.listener.write().await = None;
    }
}
```

- `mux.snd_queue.remove`：`UdtSndQueue` 是**每个 mux 一个、被该 mux 上所有 socket 共享**的调度堆（[snd_queue.rs:32-37](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_queue.rs#L32-L37)）。不摘除的话，发送 worker 会继续为这个死 socket 唤醒、尝试发包（[snd_queue.rs:152-159](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_queue.rs#L152-L159) 的 `remove` 用过滤重建 BinaryHeap 的方式丢掉匹配项）。
- 清空 `mux.listener`：服务端 listener socket 关闭后，该 mux 不再处理 `dest_socket_id==0` 的握手包（无 listener 可路由），mux 本身也才可以被进一步回收。

#### 4.3.4 代码实践

**实践目标**：跟踪一个 Broken 连接从「判坏」到「彻底从注册表消失」的全过程，体会两阶段回收。

**操作步骤**：

1. 阅读 [udt.rs:227-259](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L227-L259) 的两阶段，确认：阶段一处理 `Broken`、阶段二处理 `Closing`，且阶段一用 `spawn`、阶段二用同步删除。
2. 在 `garbage_collect_sockets` 的阶段一 `spawn` 前与阶段二 `remove` 前各加一行 `eprintln!`（示例代码，打印 `socket_id` 与 `status`）。
3. 用 `udt_sender`/`udt_receiver` 建立连接，发送一些数据后**强制 kill sender 进程**（模拟对端消失），让 receiver 侧的连接靠 EXP 超时被判 `Broken`（u7-l3：`exp_count > 16` 且超过 5 秒）。

**需要观察的现象**：

- 某轮 GC 打印「阶段一：socket X 状态 Broken」——此时 spawn 了 `close()`。
- 约 1 秒后下一轮 GC 打印「阶段二：socket X 状态 Closing」——此时从 `sockets` 删除并置 `Closed`。
- 之后再无该 socket 的 GC 日志——它已彻底消失。

**预期结果 / 待本地验证**：EXP 判 Broken 需要数秒（受 RTT 与 `exp_count` 影响），整个过程从 kill 到阶段二回收大约十几秒量级。若本地 RTT 过低导致 EXP 行为不明显，记为「待本地验证」，重点是把两阶段日志的时间差与 1 秒 GC 节奏对上。

#### 4.3.5 小练习与答案

**练习 1**：阶段一为什么用 `tokio::spawn { sock.close().await }` 而不是直接 `sock.close().await`？

**参考答案**：此时 GC 持有全局 `Udt` 写锁。`close()` 是 `async`，可能 linger 最长 10 秒、还要发包、抢其它锁；直接 `await` 会让全局写锁被占住数秒，阻塞所有其它 socket 的 bind/accept/connect。`spawn` 把关闭丢到后台，GC 立即释放写锁继续；被 spawn 的 `close()` 把状态翻到 `Closing`，交给下一轮 GC 的阶段二摘除。

**练习 2**：一个由「用户主动 `close()`」正常关闭的连接，会经过阶段一吗？为什么？

**参考答案**：不会。用户主动 `close()` 会把状态从 `Connected` 直接置为 `Closing`，从不经过 `Broken`。阶段一只筛 `Broken`，所以它只会被阶段二命中（摘除并置 `Closed`）。阶段一专门服务于「没机会正常 `close()` 就已损坏」的连接。

**练习 3**：阶段二为什么要先把 `socket_id` 收集进 `to_remove: Vec`，再循环删除，而不是边迭代 `self.sockets` 边删？

**参考答案**：不能在迭代 `BTreeMap` 的同时修改它——既违反 Rust 借用规则（迭代持有不可变借用，`remove` 需要可变借用），也会破坏树结构导致迭代器失效。先收集再删是处理「迭代中删除」的标准安全模式。

## 5. 综合实践

把三个模块串起来，**画一张完整的连接生命周期状态图与时序**：

1. **状态图**：在 `UdtStatus` 八态里标出本讲涉及的迁移边：
   - `Connected --close()--> Closing --GC阶段二--> Closed`
   - `Connected --收到对端 Shutdown--> Closing --GC阶段二--> Closed`
   - `任意活态 --EXP 超时--> Broken --GC阶段一 spawn close()--> Closing --GC阶段二--> Closed`
2. **时序追踪**：选一条「服务端 listener accept 出的连接，客户端突然 kill」的场景，按时间轴写出：
   - 客户端进程消失 → receiver 侧收不到任何包 → EXP 定时器 `exp_count` 线性增长 → 超过阈值置 `Broken`（u7-l3）。
   - 下一轮 GC（≤1 秒内）阶段一命中：若该连接还在 `queued_sockets` 则摘除；`spawn close()`。
   - 后台 `close()`：因状态是 `Broken`（非 `Connected`），linger 循环跳过、不发 Shutdown，只做 snd_queue 摘除，置 `Closing`，`notify_all`。
   - 再下一轮 GC 阶段二命中：从 `sockets` 删除，置 `Closed`，`Arc` 引用归零后真正释放。
3. **对照源码核验**：每一步都标出对应的源码位置（如「EXP 判 Broken」在 `check_timers`，「阶段一 spawn」在 [udt.rs:242-246](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L242-L246)）。

完成后，你应当能向别人解释：**为什么 tokio-udt 的关闭要拆成「socket 自管 teardown + GC 自管注册表」两层，而不是 close() 一把梭。**

## 6. 本讲小结

- `close()` 是优雅关闭的入口，顺序为：幂等守卫 → 读 `linger_timeout`（`None` 即 `ZERO`）→ linger 循环（等缓冲排空或超时）→ 从 `mux.snd_queue` 摘除并按需清空 `mux.listener` → 仍是 `Connected` 才 best-effort 发 Shutdown → 置 `Closing` → `notify_all`。
- linger 循环三个退出条件：「状态离开 Connected」「缓冲空」「超时」任一为真即跳出；`None`/`ZERO` 表示不逗留。
- Shutdown 是对称信令：`close()` 发出、对端 `process_ctrl` 的 Shutdown 分支接收（同样置 `Closing` 并 `notify_all`，但不调用本地 `close()`）；发送失败不阻断关闭。
- `close()` 把状态停在 `Closing`、不碰全局注册表——这是刻意分工：teardown 由 socket 自管，注册表删除外包给 GC，避免 `close()` 抢全局 `Udt` 写锁带来的死锁与长持锁。
- `garbage_collect_sockets` 两阶段：阶段一处理 `Broken`（先从 listener 队列摘除，再 `spawn` 一个 `close()` 补做关闭，**不 await** 以免占住全局写锁）；阶段二处理 `Closing`（先收集 id 再删除，置 `Closed`）。
- `cleanup_worker` 每 1 秒一轮，是「及时回收」与「少抢全局写锁」之间的工程取舍。

## 7. 下一步学习建议

- **u8-l3 异步桥接**：本讲多次提到 `notify_all` 唤醒等待者，下一讲专门讲 `UdtConnection` 的 `poll_read`/`poll_write`/`poll_flush`/`poll_shutdown` 如何用「spawn 一个任务等 Notify，条件满足后 `wake`」的模式把这些唤醒接到 tokio 的任务系统上（`poll_shutdown` 正是 spawn `close()` 的那一处，[connection.rs:154-165](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/connection.rs#L154-L165)）。
- **u7-l3 定时器**：本讲的 `Broken` 状态由 EXP 定时器判定，建议回头结合 `check_timers` 的 `exp_count` 指数/线性退避与「5 秒 / 16 倍」阈值，理解连接是在什么条件下被判坏的。
- **延伸阅读**：对照参考实现 UDT4 的 `CUDT::close()` 与垃圾回收，体会 tokio-udt 因「async + 全局单例 + 弱引用 mux」而做出的取舍（spawn 补做 close、GC 两阶段）。

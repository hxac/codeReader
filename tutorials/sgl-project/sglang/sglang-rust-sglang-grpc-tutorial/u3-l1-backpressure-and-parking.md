# 背压与 pending-send 停泊机制

## 1. 本讲目标

本讲是专家层（u3）的第一篇，聚焦 `sglang-grpc` 桥接层最精妙的一处设计：**当 Python 生产者推送响应 chunk 的速度快于 gRPC 客户端消费的速度时，Rust 侧如何既不丢数据、又不阻塞 Python 的 GIL，还能优雅地把「请慢一点」的信号反向通知回 Python。**

读完本讲，你应当能够：

- 说清 `try_send_chunk` 的三个分支（`Ok` / `Full` / `Closed`）各自代表什么、产出哪种 `ChunkSendStatus`。
- 解释「通道满 → `tokio_handle.spawn` 异步重发 → `mark_send_ready` → `notify_ready`」这条停泊链路每一步的动机。
- 理解 `register_pending_send` 用 `HashSet` 实现的「单停泊位不变量」，并能说清为何它返回 `false` 时要立刻 `close_channel_with_error(ChannelFull)`。
- 掌握 `set_on_ready_for_rid` 的「晚注册也能补发就绪信号」边沿语义，以及 `ready_callbacks` / `ready_signals` 两张表的分工。
- 理解终端 chunk（`Finished` / `Error`）排空后「不再触发 `on_ready`」的生产者契约。

## 2. 前置知识

本讲假设你已经读过 **u2-l5（PyBridge 与请求通道架构）** 和 **u2-l6（回调机制：ChunkCallback 与 JsonChunkCallback）**。下面快速回顾两个关键事实，本讲会反复用到：

1. **每个请求有一条有界 mpsc 通道。** `create_channel` 建立一条容量为 `response_channel_capacity`（默认 `64`）的 `tokio::sync::mpsc::channel`，`Sender` 存进 `BridgeState.channels`，`Receiver` 交给 server.rs 的流式 RPC 去消费。Python 生产者（经由 `RuntimeHandle`）不直接持有 `Sender`，而是通过回调把 chunk 推给 Rust，由 Rust 决定怎么入通道。

2. **回调 `__call__` 的返回值是 `ChunkSendStatus`。** Python 调 `chunk_callback(chunk, finished=..., error=...)` 时，Rust 的 `try_send_chunk` 会返回一个三态枚举 `ChunkSendStatus`：`Ready`（立即入队成功）、`Pending`（通道满，已停泊异步重发）、`Closed`（通道已关闭，不要再推了）。Python 侧正是靠这个返回值实现流控的。

如果你对下面这些 Tokio / PyO3 概念还不熟，本讲会顺带解释：

- **`try_send` vs `send`**：`mpsc::Sender::try_send` 是非阻塞的——满了立刻返回 `Err(TrySendError::Full)`，绝不等待；`send().await` 是异步阻塞的——满了就挂起当前 task，直到有空位。
- **`Handle::spawn`**：拿到 Tokio 运行时句柄后，可以在任意线程（包括持有 GIL 的回调线程）上向运行时投递一个 future，由 worker 池异步执行。这正是「不在 GIL 线程上 `.await`」的关键。
- **GIL（全局解释器锁）**：Python 的回调线程持有 GIL 才能碰 Python 对象。本讲里 `try_send_chunk` 是在回调线程、持有 GIL 的上下文里被调用的；而异步重发发生在 Tokio worker 线程，需要时再 `Python::with_gil` 重新获取。

> 本讲的全部逻辑都在 [`src/bridge.rs`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs) 这一个文件里。为帮助理解生产者侧，会少量引用 Python 侧 [`python/sglang/srt/entrypoints/grpc_bridge.py`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/entrypoints/grpc_bridge.py)。

## 3. 本讲源码地图

| 文件 | 关键符号 | 作用 |
| --- | --- | --- |
| `src/bridge.rs` | `try_send_chunk` | 背压核心入口：非阻塞投递 + 通道满时停泊异步重发 + 通道关闭时终止。 |
| `src/bridge.rs` | `register_pending_send` | 把 rid 登记进 `pending_sends`，实现「单停泊位」不变量。 |
| `src/bridge.rs` | `mark_send_ready` / `notify_ready` | 停泊 chunk 排空后，取出（或暂存）就绪回调并触发它。 |
| `src/bridge.rs` | `set_on_ready_for_rid` / `clear_on_ready_for_rid` | Python 注册 / 注销「通道恢复就绪」回调，支持晚注册补发。 |
| `src/bridge.rs` | `close_channel_with_error` | 通道异常终止的统一收口：清账 + 记 `TerminalError` + 通知 Python abort。 |
| `src/bridge.rs` | `BridgeState` / `TerminalError` / `ChunkSendStatus` | 背压涉及的共享状态与枚举定义。 |
| `src/server.rs` | `closed_stream_status` / `terminal_error_status` | 消费侧把 `TerminalError` 翻译成 gRPC `Status`，与本讲的终止错误对接。 |

## 4. 核心概念与源码讲解

本讲按调用顺序拆成 5 个最小模块：先看核心入口 `try_send_chunk`，再依次看它调用的 `register_pending_send`、`mark_send_ready`、`set_on_ready_for_rid`，最后看所有异常路径汇入的 `close_channel_with_error`。

### 4.1 背压核心入口：try_send_chunk 与三种发送结局

#### 4.1.1 概念说明

设想一个流式生成请求：Python 的 `TokenizerManager` 每生成一个 token 就推一个 `Data` chunk 给 Rust，Rust 再经 tonic 流式 RPC 转发给客户端。问题在于——**生产者（Python）和消费者（gRPC 客户端）的速度并不一致**。客户端可能因为网络慢、或者干脆「连上但不读」，导致 tonic 这一侧迟迟不来拉数据。

如果 Rust 用一条**无界**通道，Python 会无限制地往里塞 chunk，内存最终被撑爆；如果用一条**有界**通道并在生产者线程上 `send().await`，又会把 Python 的 GIL 线程（或 TokenizerManager 的事件循环）卡住——整个服务都被一个慢客户端拖死。

`sglang-grpc` 的解法是**有界通道 + 非阻塞投递 + 异步兜底停泊**：

- 平时用 `try_send`（非阻塞）投递，通道有位就立刻成功，生产者拿到 `Ready`，继续全速生产。
- 通道满了，**不在当前线程等待**，而是把这个 chunk「停泊（park）」进一个 Tokio task 里异步 `send().await`，同时给生产者返回 `Pending`，让它**主动暂停**。
- 通道已经关闭（消费侧早退出 / 客户端断开），直接走终止路径，返回 `Closed`，让生产者停手。

这套机制把「背压」一分为二：**容量为 C 的有界通道**承担常规流量控制（最多缓存 C 个 chunk），**「单停泊位」的异步重发**承担溢出时的最后一道缓冲与反向通知。C 的默认值定义在这里：

[bridge.rs:L35-L35](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L35-L35) — 默认通道容量 `DEFAULT_RESPONSE_CHANNEL_CAPACITY = 64`，即常规窗口大小。

#### 4.1.2 核心流程

`try_send_chunk` 的三分支可以用下面这张状态流转图概括：

```
                sender.try_send(msg)
                       │
        ┌──────────────┼──────────────┐
        ▼              ▼              ▼
       Ok(())      Full(msg)      Closed(_)
        │              │              │
        │         是终端chunk?         │
        │         ┌────┴────┐         │
        │         ▼         ▼         │
        │       是/否     (注册停泊)   │
        │         │   spawn异步重发    │
        │   Ok分支:终端则清账          │
        │         │                   │
        ▼         ▼                   ▼
    Ready     Pending              Closed
              (停泊)             (close_channel_with_error
                                  → ClientDisconnected)
```

三个返回值的语义：

- **`Ok(()) → ChunkSendStatus::Ready`**：立即入队成功。如果这条 chunk 是终端 chunk（`Finished` / `Error`），顺手 `remove_channel_refs` 把这个 rid 的全部账目清掉，因为请求已经结束，通道可以报废了。
- **`Err(TrySendError::Full(msg)) → ChunkSendStatus::Pending`**：通道满。先 `register_pending_send` 占停泊位；若占位失败（已有停泊）直接 `Closed`；否则 `tokio_handle.spawn` 一个 task 异步把 `msg` 送进去，送完用 `mark_send_ready` 反向通知。详见 4.2 / 4.3。
- **`Err(TrySendError::Closed(_)) → ChunkSendStatus::Closed`**：通道已关闭（`Receiver` 被丢掉，通常是消费侧流提前结束）。立即 `close_channel_with_error(ClientDisconnected)`，详见 4.5。

注意一个细节：**终端标记 `terminal` 在函数最开头就算好了**，`Ok` 与 `Full` 两个分支都要用它。终端 chunk 无论走哪条路，最终都要触发「清账」；区别只在于 `Ok` 分支同步清，`Full` 分支由异步 task 在送完后清。

#### 4.1.3 源码精读

先看函数签名与 `terminal` 标记的计算：

[bridge.rs:L524-L534](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L524-L534) — 入参除了 `msg: ResponseChunk`，还显式带上 `state`、`runtime_handle`、`tokio_handle`、`sender`。注意它是一个**自由函数**（不是 `PyBridge` 的方法），因为两个回调 `ChunkCallback` / `JsonChunkCallback` 都要复用它，把所需依赖当参数传进来更解耦。第 533 行 `let terminal = msg.is_terminal();` 提前算出是否终端。

`ResponseChunk::is_terminal` 的定义：

[bridge.rs:L20-L24](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L20-L24) — 只有 `Finished` 和 `Error` 是终端；`Data` 不是。终端意味着「这是这个请求的最后一条 chunk」。

**`Ok` 分支**——立即成功，终端则清账：

[bridge.rs:L535-L540](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L535-L540) — `Ok(())` 时若 `terminal` 为真，调 `remove_channel_refs(rid, state)` 清掉该 rid 在五张表里的全部记录（通道、停泊位、就绪回调、就绪信号、——注意终端错误表 `terminal_errors` 不在这里清，因为正常结束不该有终止错误）。然后返回 `Ready`。

**`Full` 分支**——本讲的重头戏，先占停泊位：

[bridge.rs:L541-L555](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L541-L555) — `register_pending_send` 返回 `false`（说明该 rid 已经有一个停泊 chunk 了，详见 4.2），打一条告警日志，调 `close_channel_with_error(..., TerminalError::ChannelFull)`，返回 `Closed`。这是「单停泊位」被破坏时的防御性关停。

占位成功后，把后续异步 task 需要的所有权 `clone` 出来，然后 `spawn`：

[bridge.rs:L557-L594](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L557-L594) — 注意四样东西都被显式克隆/拥有化：`rid_owned: String`、`state: Arc`（clone 引用计数）、`runtime_handle: PyObject`（`clone_ref(py)`，需要在 GIL 下增引用）、`sender: Sender`（mpsc 的 `Sender` 是 `Clone` 的）。这是因为 `spawn` 的闭包要 `'static` + `Send`，不能借用回调线程的栈变量。闭包内部：
- `sender.send(msg).await` 成功后，若是终端 chunk，`remove_channel_refs` 清账并 `return`（**不触发 `on_ready`**，见 4.3 的生产者契约）；若非终端，`Python::with_gil` 后调 `mark_send_ready`，拿到回调就 `notify_ready`。
- `send` 返回 `Err`（通道在等待期间被关闭），说明客户端断了，`close_channel_with_error(ClientDisconnected)`。

最后这个分支返回 `Pending`（第 594 行）——告诉 Python「这条 chunk 已停泊，请暂停生产等通知」。

**`Closed` 分支**——通道已死：

[bridge.rs:L596-L605](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L596-L605) — `TrySendError::Closed` 意味着 `Receiver` 已经被 drop（消费侧流结束）。直接 `close_channel_with_error(ClientDisconnected)` 并返回 `Closed`。

`ChunkSendStatus` 三个变体就是上面三个返回值的归宿：

[bridge.rs:L69-L75](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L69-L75) — `Ready` / `Pending` / `Closed`。它带 `#[pyclass(eq, eq_int)]`，会暴露给 Python，Python 用相等比较判断状态（见 4.5 末尾与 u2-l6）。

#### 4.1.4 代码实践

**实践目标**：把 `try_send_chunk` 的三分支与 `ChunkSendStatus` 三态对应清楚，并验证「终端 chunk 走 `Ok` 分支也会清账」。

**操作步骤**：

1. 打开 [bridge.rs:L524-L607](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L524-L607)，把三个 `match` 臂各自返回的 `ChunkSendStatus` 抄下来。
2. 对照 [bridge.rs:L69-L75](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L69-L75)，确认三个变体名。
3. 追问自己一个问题：假设一条 `Finished` chunk 走的是 `Ok` 分支（通道没满），它会不会触发 `remove_channel_refs`？会不会触发 `on_ready`？

**需要观察的现象 / 预期结果**：

| `try_send` 结果 | chunk 是否终端 | 返回值 | 是否清账 | 是否触发 on_ready |
| --- | --- | --- | --- | --- |
| `Ok(())` | 否（Data） | `Ready` | 否 | 否（本来就成功，无需通知） |
| `Ok(())` | 是（Finished/Error） | `Ready` | 是（`remove_channel_refs`） | 否 |
| `Full` | 否（Data） | `Pending` | 由异步 task 在送完后 `mark_send_ready` | 是（经 `notify_ready`） |
| `Full` | 是（Finished/Error） | `Pending` | 由异步 task 在送完后清账 | **否**（终端契约，见 4.3） |
| `Closed` | 任意 | `Closed` | 是（`close_channel_with_error`） | 否 |

> 待本地验证：上表第 4 行（`Full` + 终端）是最容易被忽略的契约，4.3 会专门解释为什么终端 chunk 排空后**不再**通知就绪。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `try_send_chunk` 用 `try_send`（非阻塞）而不是直接 `send().await`？

**参考答案**：因为 `try_send_chunk` 运行在**回调线程、且持有 GIL** 的上下文里（它是从 `ChunkCallback::__call__` 调进来的）。如果在这里 `send().await` 等一个慢客户端腾出通道空位，就会长时间霸占 GIL，把整个 Python 运行时（包括其他无关请求）一起卡死。`try_send` 保证回调线程「要么立刻成功、要么立刻返回 `Full` 把决定权交给异步层」，GIL 持有时间被压缩到最小。

**练习 2**：`Full` 分支里，`tokio_handle.spawn` 投递的闭包为什么要单独 `clone` 出 `state`、`runtime_handle`、`sender`，而不能直接捕获 `&self`？

**参考答案**：`spawn` 要求闭包是 `'static + Send`，不能借用回调线程栈上的局部变量或 `&self`。因此把 `state`（`Arc`，clone 计数）、`runtime_handle`（`PyObject`，`clone_ref` 在 GIL 下增引用）、`sender`（`Sender: Clone`）、`rid`（`to_string()` 拥有化）都克隆成独立所有权，闭包才能被丢进 Tokio worker 池异步执行。

### 4.2 停泊注册：register_pending_send 与「单停泊位」不变量

#### 4.2.1 概念说明

`Full` 分支里第一件事就是 `register_pending_send(rid, state)`。它的作用是：**在共享账本里标记「这个 rid 当前有一个 chunk 正在停泊（异步重发中）」**。

为什么需要这个标记？因为停泊是用 `tokio_handle.spawn` 起的异步 task，它和回调线程是**并发**的。设想没有这个标记会发生什么：

1. 通道满，chunk A 停泊，spawn 一个 task_A 去 `send(A).await`。
2. 在 task_A 把 A 送进去**之前**，Python 又推来 chunk B（通道还是满的），又 spawn 一个 task_B。
3. task_A、task_B 都在等空位，A 和 B 的先后顺序无法保证，甚至可能乱序到达客户端。
4. 更糟的是，如果客户端一直不消费，停泊 task 会无限堆积，等于把「无界」问题从通道转移到了 task 池。

`sglang-grpc` 的选择是**强制每个 rid 至多有一个停泊 chunk**——这就是「单停泊位不变量」。它依赖一个朴素的数学性质：

> 停泊位是一个集合 `pending_sends ⊆ { rid }`，对任意 rid，`rid ∈ pending_sends` 是布尔值（在 / 不在），而不是计数。因此「停泊位数」天然只能取 0 或 1。

记 `parked(rid)` 为 rid 当前停泊的 chunk 数，则系统维护的不变量是：

\[
\forall\, rid:\quad parked(rid) \le 1
\]

`register_pending_send` 就是这个不变量的「守门员」。

#### 4.2.2 核心流程

```
register_pending_send(rid, state):
    lock state
    return pending_sends.insert(rid.to_string())   # HashSet::insert
```

`HashSet::insert` 的返回值是关键：

- 返回 `true`：rid **原先不在**集合里，本次是新插入 → `parked(rid)` 从 0 变 1，占位成功，调用方继续 spawn 异步 task。
- 返回 `false`：rid **已经在**集合里（已有一个停泊 chunk） → `parked(rid)` 已经是 1，本次是第二个 → **不变量被违反**，调用方应立即关停通道。

为什么「不变量被违反」等同于「客户端不消费、生产者又抢着推」？因为正常情况下，生产者在收到 `Pending` 后会**主动暂停**、等 `on_ready` 通知才推下一条（见 4.3 与综合实践）。也就是说，只要生产者守规矩，第二个停泊永远不会出现。一旦出现，就说明协议被破坏（比如某条非流式路径没等通知就连续推），此时最安全的做法是**果断关停这条流**，而不是继续堆积。

#### 4.2.3 源码精读

`register_pending_send` 只有三行，但信息密度很高：

[bridge.rs:L476-L479](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L476-L479) — 加锁后 `state.pending_sends.insert(rid.to_string())`，**直接返回 `HashSet::insert` 的布尔结果**。注意它没有自定义错误类型，而是把「是否重复」这个布尔信号原样上抛，由调用方 `try_send_chunk` 解释。

`pending_sends` 字段定义在 `BridgeState`：

[bridge.rs:L39-L46](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L39-L46) — `pending_sends: HashSet<String>`，和 `channels` / `ready_callbacks` / `ready_signals` / `terminal_errors` 一起被同一把 `Mutex` 保护（见 u2-l5 的 `BridgeStateRef = Arc<Mutex<BridgeState>>`）。`HashSet`（而非 `HashMap<rid, count>`）从类型层面就锁死了「在 / 不在」的二值语义，防止任何人写出「累加计数」的代码——这是用类型表达不变量的好例子。

回到调用点看 `false` 分支的处置：

[bridge.rs:L541-L555](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L541-L555) — `if !register_pending_send(...)` 即「占位失败（已有停泊）」，告警日志写得直白：*"received another chunk before the parked chunk drained; closing stream"*（停泊 chunk 还没排空就又来了一条；关闭流）。随后 `close_channel_with_error(..., TerminalError::ChannelFull)`，返回 `Closed`。

`TerminalError::ChannelFull` 的语义：

[bridge.rs:L48-L67](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L48-L67) — `ChannelFull { rid }` 的 `message()` 是 *"gRPC response channel full for {rid}: client not consuming"*。它会在消费侧被 `terminal_error_status` 映射成 gRPC 的 `RESOURCE_EXHAUSTED`（资源耗尽），见 4.5。

#### 4.2.4 代码实践

**实践目标**：验证「单停泊位」是用 `HashSet` 的布尔语义实现的，并理解它如何阻止停泊堆积。

**操作步骤**：

1. 读 [bridge.rs:L476-L479](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L476-L479)，确认返回的就是 `HashSet::insert` 的结果。
2. 在 [bridge.rs:L39-L46](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L39-L46) 找到 `pending_sends: HashSet<String>`。
3. 思考：如果有人把 `pending_sends` 改成 `HashMap<String, usize>` 并用 `*entry.or_insert(0) += 1`，会丢掉哪一层保护？

**预期结果**：改成计数 map 后，`parked(rid)` 可以无限增长，单停泊位不变量失效，停泊 task 会无限制堆积，最终回到「无界」问题。`HashSet` 的二值性是一种**编译期可读的不变量约束**。

> 待本地验证：本实践为源码阅读型，无需运行。若想观察「`false` 分支被触发」的告警日志，需要在客户端完全不消费的前提下，让生产者在收到 `Pending` 后仍连续推第二条 chunk——正常 Python 路径不会这么做，因此这条日志在生产中极少出现，属于防御性兜底。

#### 4.2.5 小练习与答案

**练习 1**：`register_pending_send` 返回 `false` 时，`try_send_chunk` 为什么选择 `close_channel_with_error(ChannelFull)` 而不是「把第二条 chunk 也停泊起来排队」？

**参考答案**：两个原因。(1) **有序性**：多个停泊 task 并发 `send().await`，无法保证 A 先于 B 入队，客户端可能收到乱序 chunk。(2) **有界性**：若允许排队，停泊 task 会随生产者速度无限堆积，等于把内存爆炸从「通道」搬到「task 池」，背压形同虚设。单停泊位 + 违反即关停，是用一次确定的失败换取「永不失控」。

**练习 2**：占位成功后，`pending_sends` 里的这个 rid 何时被移除？

**参考答案**：在停泊 chunk 异步送完后，由 `mark_send_ready` 的 `state.pending_sends.remove(rid)` 移除（见 [bridge.rs:L481-L490](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L481-L490)）。若停泊的是终端 chunk，则由 `remove_channel_refs`（内部调 `remove_channel_refs_locked`，见 4.5）连同其他账目一起清掉。此外 `abort` / `abort_all` / `create_channel` 等路径也会清理它。

### 4.3 就绪边沿通知：mark_send_ready 与 notify_ready

#### 4.3.1 概念说明

停泊 chunk 被 `spawn` 的异步 task 终于送进通道后，系统还需要做一件关键的事：**把「通道又有空位了，你可以继续生产了」这个信号传回 Python**。否则 Python 永远停在 `await ready_event.wait()` 上，请求就僵死了。

这条反向通知链由两个小函数组成：

- `mark_send_ready`：在共享账本里「结算」这次停泊——移除停泊标记，并决定**当下**能不能立刻通知 Python。
- `notify_ready`：实际调用 Python 注册的 `on_ready` 回调。

这里有一个**边沿触发（edge-triggered）**的设计要点：通知应该在「从满变不满」的那个**瞬间**发生，而且**只发生一次**。不能反复通知（会让 Python 误以为有很多空位而猛推），也不能漏通知（会让 Python 永远卡住）。

另外有一条与终端 chunk 相关的**生产者契约**：如果停泊的是 `Finished` / `Error` 这种终端 chunk，它排空后**不应该**再触发 `on_ready`——因为终端 chunk 意味着请求已经结束，生产者本就不该再推任何东西；若此时还发「就绪」信号，反而可能误导生产者继续推。

#### 4.3.2 核心流程

```
# 异步 task 内（tokio worker 线程）
sender.send(msg).await:
    Ok(()):
        if terminal:               # Finished / Error
            remove_channel_refs(rid)   # 清账，直接 return，不通知
            return
        else:                      # Data
            Python::with_gil:
                callback? = mark_send_ready(rid)
                if callback: notify_ready(rid, callback)

mark_send_ready(rid):
    lock state
    pending_sends.remove(rid)              # 结算停泊位
    if ready_callbacks 有 rid:              # Python 已注册回调
        return Some(callback.clone)        # 立刻拿来通知
    else:                                  # Python 还没注册
        ready_signals.insert(rid)          # 留个「曾经就绪过」的信号
        return None                        # 本次不通知，等注册时补发
```

`mark_send_ready` 的两条出路对应「注册早于排空」和「排空早于注册」两种时序：

- **Python 先注册、后排空**（常见）：`ready_callbacks` 里已有回调 → 取出回调，`notify_ready` 立即触发。
- **排空先于注册**（竞态）：`ready_callbacks` 还没有 → 往 `ready_signals` 塞一个标记，本次不通知；等 Python 稍后调 `set_on_ready` 时，发现 `ready_signals` 里有标记，就**当场补发**一次通知（见 4.4）。

#### 4.3.3 源码精读

`mark_send_ready`：

[bridge.rs:L481-L490](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L481-L490) — 三步：`pending_sends.remove(rid)`（结算停泊位）；若 `ready_callbacks.get(rid)` 有值，`clone_ref` 出回调返回 `Some`；否则 `ready_signals.insert(rid)` 并返回 `None`。**关键**：移除停泊标记和查回调在**同一个临界区**里，保证「结算」与「决定是否通知」是原子的，不会出现「停泊位移除了、回调却没取到」的中间态。

`notify_ready`：

[bridge.rs:L492-L496](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L492-L496) — 直接 `callback.call0(py)` 调用 Python 的零参 `on_ready`。失败只打 `warn` 日志、不向上传播——因为这是「通知」，通知失败不应该把整条异步 task 搞崩。

终端 chunk 不通知的契约，体现在 `spawn` 闭包里：

[bridge.rs:L562-L577](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L562-L577) — 第 565-570 行：若 `terminal`，`remove_channel_refs(&rid_owned, &state)` 后直接 `return`，**跳过** `mark_send_ready` / `notify_ready`。注释写得很清楚：*"Terminal chunks end the producer contract; no further on_ready signal is fired after a parked Finished/Error drains."*（终端 chunk 终结生产者契约；停泊的 Finished/Error 排空后不再触发就绪信号）。只有非终端 `Data`（第 572-576 行）才会走 `mark_send_ready` → `notify_ready`。

为什么终端 chunk 要特别对待？因为终端 chunk 是请求的**最后一条**。生产者收到 `Pending` 后，对终端 chunk 的处理是「直接结束、不再等通知」（见综合实践里的 Python 侧 `_send_with_backpressure`：`if kwargs.get("finished"): return True`）。如果 Rust 还多发一次就绪信号，生产者已经退出循环，这个信号成了无人接收的孤儿，徒增混乱；更危险的是，若生产者逻辑有 bug 还在循环里，这个信号会诱导它继续推一条根本不存在的下一条 chunk。所以「终端不通知」是和 Python 侧契约对齐的必要保证。

#### 4.3.4 代码实践

**实践目标**：用一张表区分「终端 chunk 排空」与「非终端 chunk 排空」两条路径的动作差异。

**操作步骤**：

1. 读 [bridge.rs:L562-L592](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L562-L592)，把 `Ok(())` 分支里 `if terminal { ... return; }` 与 `else { mark_send_ready ... }` 两条路径分别列出来。
2. 对照 [bridge.rs:L481-L490](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L481-L490)，确认 `mark_send_ready` 内部「移除停泊位」与「查/留回调」是否在同一把锁里。

**预期结果**：

| 停泊 chunk 类型 | 排空后动作 | 移除 `pending_sends`？ | 触发 `on_ready`？ |
| --- | --- | --- | --- |
| `Data`（非终端） | `mark_send_ready` → `notify_ready` | 是（`mark_send_ready` 内） | 是（若回调已注册）或暂存信号（见 4.4） |
| `Finished` / `Error`（终端） | `remove_channel_refs` 后 `return` | 是（`remove_channel_refs_locked` 内） | **否** |

> 待本地验证：本实践为源码阅读型。终端不通知的契约是本讲最隐蔽的一处，建议结合 4.4 与综合实践一起理解。

#### 4.3.5 小练习与答案

**练习 1**：`mark_send_ready` 为什么要把「移除停泊位」和「取回调」放在同一个锁临界区，而不是分两次加锁？

**参考答案**：若分两次加锁，中间存在窗口：停泊位移除了，但还没取回调时，另一个线程（比如 `set_on_ready_for_rid`）可能改 `ready_callbacks` / `ready_signals`，导致「该通知的没通知」或「重复通知」的竞态。同一把锁保证「结算 + 决定通知策略」是一个原子决策，边沿触发的「恰好一次」语义才成立。

**练习 2**：假设停泊的是一条 `Finished` chunk，它在异步 task 里排空后调用了 `remove_channel_refs`。这个 rid 的 `ready_callbacks` 会被清掉吗？如果 Python 之前注册过 `on_ready`，会怎样？

**参考答案**：会被清掉。`remove_channel_refs` → `remove_channel_refs_locked` 会移除该 rid 在 `channels` / `pending_sends` / `ready_callbacks` / `ready_signals` 四张表的全部记录（见 [bridge.rs:L463-L469](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L463-L469)）。Python 注册的 `on_ready` 不会被调用（终端不通知），注册的回调对象随表项移除而释放。这与「请求已结束、通道已报废」的语义一致。

### 4.4 晚注册补发：set_on_ready_for_rid 与双表设计

#### 4.4.1 概念说明

4.3 留了一个悬念：如果**停泊 chunk 排空（`mark_send_ready`）发生在 Python 注册回调（`set_on_ready`）之前**，怎么办？此时 `ready_callbacks` 里还没有回调，`mark_send_ready` 只能往 `ready_signals` 塞个标记就返回 `None`——通知被「暂存」了。

`set_on_ready_for_rid` 就是来收拾这个竞态的。它的职责是：**Python 注册 `on_ready` 时，如果发现 `ready_signals` 里有「曾经就绪过」的标记，就立刻补发一次通知，并清掉标记**。这样无论「注册」和「排空」谁先谁后，通知都不会丢：

- 注册早于排空 → `mark_send_ready` 直接取回调通知（4.3 已述）。
- 排空早于注册 → `set_on_ready_for_rid` 补发通知。

这就是 `ready_callbacks`（存回调）和 `ready_signals`（存「曾经就绪」的边沿标记）**两张表分工**的意义——前者是「回调本体」，后者是「错过的事件回放」。这套设计保证了边沿信号**不丢失、不重复**。

`clear_on_ready_for_rid` 则是收尾：请求结束时注销回调，避免对同一个 rid 重复触发通知。

#### 4.4.2 核心流程

```
set_on_ready_for_rid(rid, on_ready):
    lock state:
        ready_callbacks.insert(rid, on_ready)     # 注册回调
        had_signal = ready_signals.remove(rid)    # 顺带取走「曾经就绪」标记
    if had_signal:                                 # 排空早于注册 → 补发
        on_ready.call0()                           # 立即通知一次

clear_on_ready_for_rid(rid):
    lock state:
        ready_callbacks.remove(rid)
        ready_signals.remove(rid)
```

两个函数都在 Python 侧的回调对象 `ChunkCallback` / `JsonChunkCallback` 上暴露为 `set_on_ready` / `clear_on_ready` 方法，由 Python 的 `_install_on_ready` / `_uninstall_on_ready` 调用。

#### 4.4.3 源码精读

`set_on_ready_for_rid`：

[bridge.rs:L498-L515](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L498-L515) — 第 505-510 行是一个块表达式：加锁后 `ready_callbacks.insert` 注册回调，同时 `ready_signals.remove(rid)` 取出标记（`HashSet::remove` 返回布尔：是否原本存在）。`should_notify` 为 `true` 即「排空早于注册」，出锁后第 511-513 行 `on_ready.call0(py)` 补发通知。**注意锁在补发前就 `drop` 了**——通知是跨语言调用，可能耗时，不能放在临界区里，否则阻塞其他请求拿锁。

`clear_on_ready_for_rid`：

[bridge.rs:L517-L522](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L517-L522) — 同时移除回调和信号。注释强调 *"Do not call set_on_ready again for the same rid"*（不要对同一 rid 再次 set_on_ready）——因为 clear 之后该 rid 的就绪通知链已彻底终止。

Python 侧如何使用这两个方法：

[grpc_bridge.py:L156-L171](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/entrypoints/grpc_bridge.py#L156-L171) — `_install_on_ready`：检查 `chunk_callback` 有没有 `set_on_ready` 属性（Rust 回调对象有，普通 Python 回调可能没有，所以用 `getattr` 容错）；建一个 `asyncio.Event`，定义 `_on_ready` 闭包用 `loop.call_soon_threadsafe(ready_event.set)` 把「Rust worker 线程发来的通知」安全地投递回 TokenizerManager 的事件循环（**线程安全的关键**：`call_soon_threadsafe` 是唯一允许从其他线程往 asyncio loop 投递回调的接口）；然后调 `set_on_ready(_on_ready)` 把闭包注册进 Rust。

[grpc_bridge.py:L173-L181](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/entrypoints/grpc_bridge.py#L173-L181) — `_uninstall_on_ready`：请求结束时调 `clear_on_ready`。

> 顺带注意：`set_on_ready` / `clear_on_ready` 这两个 PyO3 方法定义在两个回调类里，分别见 [bridge.rs:L622-L628](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L622-L628)（`ChunkCallback`）和 [bridge.rs:L710-L716](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L710-L716)（`JsonChunkCallback`），两者都只是转发给 `set_on_ready_for_rid` / `clear_on_ready_for_rid`。

#### 4.4.4 代码实践

**实践目标**：验证「双表 + 补发」机制能覆盖「注册」与「排空」的所有时序组合。

**操作步骤**：

1. 读 [bridge.rs:L498-L515](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L498-L515)，确认 `should_notify` 取自 `ready_signals.remove(rid)`。
2. 回到 [bridge.rs:L481-L490](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L481-L490)，确认 `mark_send_ready` 在「没回调」时会 `ready_signals.insert(rid)`。
3. 列出两种时序下，`on_ready` 最终被调用几次。

**预期结果**（边沿信号「恰好一次」）：

| 时序 | `mark_send_ready` 动作 | `set_on_ready_for_rid` 动作 | `on_ready` 调用次数 |
| --- | --- | --- | --- |
| 先注册，后排空 | 取到回调 → `notify_ready` | 注册时 `ready_signals` 为空，不补发 | 1 |
| 先排空，后注册 | 没回调 → `ready_signals.insert` | 注册时发现标记 → `call0` 补发 | 1 |

两种时序下都是恰好 1 次，不丢不重。

> 待本地验证：本实践为源码阅读型。「恰好一次」的保证依赖 `mark_send_ready` 和 `set_on_ready_for_rid` 各自的临界区原子性，以及 `ready_signals` 作为「错过事件回放」的二值标记。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `ready_signals` 用 `HashSet<String>`（在 / 不在）而不是 `HashMap<String, usize>`（计数）？

**参考答案**：因为「就绪」是**边沿事件**而非**电平事件**——只需要表达「发生过至少一次就绪、还没被消费」这个布尔事实。每注册一次回调至多补发一次通知，不需要知道「发生过几次」。用 `HashSet` 从类型上禁止计数，保证补发「恰好一次」。这与 4.2 的 `pending_sends` 用 `HashSet` 是同一套设计哲学。

**练习 2**：`set_on_ready_for_rid` 为什么要在调用 `on_ready.call0()` **之前** `drop(state)`（即先出锁再通知）？

**参考答案**：`on_ready.call0(py)` 是跨语言调用 Python 代码，可能耗时且不可控。若持着 `BridgeState` 的锁去调，会阻塞其他所有需要拿这把锁的请求（其他 rid 的 `try_send_chunk`、`create_channel`、`abort` 等），把一个请求的通知延迟放大成全局阻塞。先出锁再通知，把临界区缩到最小，是高并发服务的基本功。

### 4.5 关停与终止错误：close_channel_with_error

#### 4.5.1 概念说明

前面几节反复出现 `close_channel_with_error`，它是**所有异常终止路径的统一收口**。无论是 `ChannelFull`（单停泊位被破坏）、`ClientDisconnected`（通道关闭 / 客户端断开），还是后续 u3-l2 会讲的 `Aborted`，最终都汇入这一个函数。

它的职责三件事，**顺序很重要**：

1. **清账**：把这个 rid 在共享账本里的全部记录抹掉（通道、停泊位、就绪回调、就绪信号），让 rid 重新变「干净」。
2. **记终止错误**：往 `terminal_errors` 表里塞一条 `TerminalError`，作为「这条流为什么死」的凭证，留给消费侧（server.rs）来取。
3. **通知 Python abort**：调 `runtime_handle.abort(rid, false)`，让 Python 侧也停止生成、释放资源。

注意第 2 步和第 1 步的微妙关系：`remove_channel_refs_locked` 会清掉 `channels` / `pending_sends` / `ready_callbacks` / `ready_signals` 四张表，但**不清** `terminal_errors`；而 `close_channel_with_error` 紧接着往 `terminal_errors` 里 `insert`。这样「清旧账」与「立新案」分工明确，不会互相覆盖。

消费侧（server.rs 的流式 / 一元 RPC）在通道返回 `Ok(None)`（流关闭）时，会调 `closed_stream_status` 去 `terminal_errors` 里取这个凭证，翻译成对应的 gRPC `Status` 返回给客户端。

#### 4.5.2 核心流程

```
close_channel_with_error(rid, error: TerminalError):
    lock state:
        remove_channel_refs_locked(state, rid)   # 清四张表
        terminal_errors.insert(rid, error)        # 立案
    drop(state)
    runtime_handle.abort(rid, false)              # 通知 Python（忽略错误）

# 消费侧（server.rs）
recv → Ok(None)（通道关闭）:
    closed_stream_status(rid):
        if terminal_errors 有 rid:
            return (terminal_error_status(error), should_abort=false)
        else:
            return (Status::internal("stream closed ..."), should_abort=true)
```

`terminal_error_status` 把 `TerminalError` 三变体映射到两类 gRPC 状态码：

| `TerminalError` 变体 | gRPC Code | 含义 |
| --- | --- | --- |
| `ChannelFull` | `RESOURCE_EXHAUSTED` | 服务端资源（通道）耗尽，客户端消费太慢 |
| `ClientDisconnected` | `CANCELLED` | 客户端已断开 |
| `Aborted` | `CANCELLED` | 请求被主动取消 |

#### 4.5.3 源码精读

`close_channel_with_error`：

[bridge.rs:L449-L461](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L449-L461) — 加锁后 `remove_channel_refs_locked(&mut state, rid)` 清四张表，`terminal_errors.insert(rid, error)` 立案，`drop(state)` 出锁，最后 `let _ = runtime_handle.call_method1(py, "abort", (rid, false))` 通知 Python（`let _ =` 表示**忽略返回值与错误**——通知失败也不影响已经完成的清账与立案）。注意它需要一个 `py: Python<'_>` token，因此所有调用点都处于持 GIL 的上下文。

`remove_channel_refs_locked`：

[bridge.rs:L463-L469](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L463-L469) — 移除 `channels` / `pending_sends` / `ready_callbacks` / `ready_signals` 四张表里该 rid 的记录，返回「是否曾经存在过任何一项」（供 `abort` 判断 rid 是否活跃用）。**刻意不碰 `terminal_errors`**——那张表由 `close_channel_with_error` / `abort` / `remove_channel` 各自按语义管理。

`remove_channel_refs` 是 `remove_channel_refs_locked` 的加锁包装：

[bridge.rs:L471-L474](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L471-L474) — 给异步 task（不持锁的上下文）提供一个「自己加锁再清」的入口，被 `try_send_chunk` 的 `Ok` 终端分支和 `Full` 终端分支（经 `remove_channel_refs`）调用。

消费侧的衔接——`closed_stream_status` 与 `terminal_error_status`：

[server.rs:L188-L197](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L188-L197) — 通道返回 `Ok(None)` 时：若 `take_terminal_error(rid)` 取到了凭证（说明是 Rust 侧主动立案的终止，如 `ChannelFull` / `ClientDisconnected`），返回对应 `Status` 且 `should_abort=false`（已经处理过，无需再 abort）；否则返回一个泛化的 `Status::internal` 且 `should_abort=true`（异常关闭，需要补一个 abort 给 Python）。

[server.rs:L199-L207](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L199-L207) — `ChannelFull` → `Status::resource_exhausted`；`ClientDisconnected` / `Aborted` → `Status::cancelled`。这就是本讲所有终止错误最终到达客户端时变成的 gRPC 状态码。

最后，`TerminalError` 三变体与消息模板：

[bridge.rs:L48-L67](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L48-L67) — 每个变体都带 `rid`，`message()` 把 rid 拼进错误描述，方便排查是哪个请求触发的。

#### 4.5.4 代码实践

**实践目标**：把本讲的三个异常终止触发点与它们产出的 `TerminalError`、gRPC Code 串起来。

**操作步骤**：

1. 在 [bridge.rs](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs) 里 grep 三处 `close_channel_with_error(` 的调用点，记录每处传入的 `TerminalError` 变体。
2. 对照 [bridge.rs:L449-L461](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L449-L461)，确认每次都会「清四表 + 立案 + 通知 Python abort」。
3. 对照 [server.rs:L199-L207](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L199-L207)，写出每个 `TerminalError` 对应的 gRPC Code。

**预期结果**：

| 触发点（bridge.rs） | 传入的 `TerminalError` | gRPC Code |
| --- | --- | --- |
| `Full` 分支：`register_pending_send` 返回 `false`（L547-L554） | `ChannelFull` | `RESOURCE_EXHAUSTED` |
| `Full` 分支：异步 `send().await` 返回 `Err`（L579-L590） | `ClientDisconnected` | `CANCELLED` |
| `Closed` 分支：`TrySendError::Closed`（L596-L604） | `ClientDisconnected` | `CANCELLED` |

> 待本地验证：本实践为源码阅读型。`ChannelFull` 路径在生产中极少触发（需客户端完全不消费且生产者未按 Pending 暂停）；`ClientDisconnected` 在客户端主动断开时较常见。

#### 4.5.5 小练习与答案

**练习 1**：`close_channel_with_error` 里 `runtime_handle.abort` 的返回值被 `let _ =` 丢弃了，为什么？如果 abort 调用失败，会出什么问题？

**参考答案**：因为此时 Rust 侧的「清账 + 立案」已经完成（rid 的通道已移除、`terminal_errors` 已写入），这是给消费侧的权威凭证。`abort` 只是顺带通知 Python「也停一下吧」，属于 best-effort：即使它失败（比如 Python 侧已经自己停了、或抛异常），也不应回滚已经完成的 Rust 侧清理——回滚反而会让消费侧取不到 `terminal_errors`、陷入 `Ok(None)` 却无凭证的歧途。所以丢弃返回值是有意为之。

**练习 2**：消费侧 `closed_stream_status` 在「取到 `terminal_errors` 凭证」时返回 `should_abort=false`，在「没取到」时返回 `should_abort=true`。为什么反过来？

**参考答案**：取到凭证说明是 Rust 侧**主动立案**的终止（`close_channel_with_error` 已经调过 `abort` 通知 Python），不需要消费侧再 abort 一次；没取到凭证说明是**异常关闭**（通道莫名其妙没了，Rust 侧没走过 `close_channel_with_error`），Python 可能还在傻等，所以消费侧需要补一个 abort 把取消传回去（这正是 u3-l2「中止传播」要讲的内容）。

## 5. 综合实践

**综合任务**：画出一次「通道满 → 停泊 → 异步重发 → 反向通知 → 恢复生产」的完整时序图，并用一句话说明「单停泊位不变量」如何让停泊数量恒不超过 1。

请按以下顺序阅读源码并完成时序图（推荐用纸笔或文本画）：

1. 生产者推送 chunk A：Python 侧 [grpc_bridge.py:L116-L154](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/entrypoints/grpc_bridge.py#L116-L154) 的 `_send_with_backpressure` 调 `chunk_callback(...)`，进入 Rust [bridge.rs:L524-L607](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L524-L607) 的 `try_send_chunk`。
2. `try_send` 返回 `Full`：[bridge.rs:L541-L555](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L541-L555) 占停泊位，[bridge.rs:L557-L594](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L557-L594) `spawn` 异步 task，返回 `Pending`。
3. Python 收到 `Pending`：`_send_with_backpressure` 走到 `await ready_event.wait()`（[grpc_bridge.py:L136-L154](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/entrypoints/grpc_bridge.py#L136-L154)），**主动暂停**，不再推下一条。
4. Tokio worker 排空 A：`sender.send(A).await` 成功后 [bridge.rs:L572-L576](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L572-L576) 调 `mark_send_ready`（[bridge.rs:L481-L490](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L481-L490)）取出回调，`notify_ready`（[bridge.rs:L492-L496](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L492-L496)）触发 Python 的 `_on_ready`。
5. Python 恢复：`_on_ready` 经 `call_soon_threadsafe(ready_event.set)`（[grpc_bridge.py:L163-L164](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/entrypoints/grpc_bridge.py#L163-L164)）唤醒事件循环，Python 清掉 event、推下一条 chunk B。

**参考时序图**（四个参与者：Python TM 事件循环、Rust 回调线程持 GIL、Tokio worker、gRPC 客户端消费侧）：

```
Python(TM loop)      Rust回调(GIL)        Tokio worker          gRPC客户端
    |                     |                    |                     |
    | chunk A (Data)      |                    |                     |
    |-------------------->|                    |                     |
    |                     | try_send(A)→Full   |                     |
    |                     | register_pending→true                    |
    |                     | spawn(send A)----->|                     |
    |<--Pending------------|                    |                     |
    | await ready_event    |                    |                     |
    | .wait()（暂停）      |                    | send(A).await       |
    |                      |                    |  （客户端终于读了一个）|
    |                      |                    | mark_send_ready     |
    |                      |                    |  pending.remove     |
    |                      |                    |  ready_callbacks→Some|
    |                      |<--callback.call0---|                     |
    |                      | _on_ready          |                     |
    |                      | call_soon_threadsafe(ready_event.set)    |
    | ready_event=set      |                    |                     |
    | 清event, 推chunk B   |                    |                     |
    |-------------------->| try_send(B)→Ok     |                     |
    |<--Ready--------------|                    |                     |
    |                      |                    |                     |<--读到A、B...
```

**关键结论（回答实践任务的第二个问题）**：`register_pending_send` 返回 `false`（即 `pending_sends` 里已有该 rid，说明已有一个停泊 chunk 还没排空）时，意味着生产者在收到上一条的 `Pending` 后**没有暂停**就又推了一条——这破坏了「收到 Pending 必须等 on_ready 才能再推」的协议。若放任第二条也停泊，多个 `send().await` task 并发会导致**乱序**与**无限堆积**，背压形同虚设。因此 `try_send_chunk` 立即 `close_channel_with_error(ChannelFull)`（[bridge.rs:L547-L554](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L547-L554)），用一次确定的 `RESOURCE_EXHAUSTED` 失败换取「停泊数恒不超过 1」的不变量。

> 进阶观察（待本地验证）：若想亲眼看到 `ChannelFull` 告警，可尝试构造一个「客户端连上流式 RPC 但完全不读响应」的场景，并把 `response_channel_capacity` 调到很小的值（参见 u1-l4 的 `start_server` 参数归一化），让通道更快填满。但请注意，仅靠合法的 Python 生产者路径（遵守 `Pending` 暂停）通常**不会**触发第二条停泊，`ChannelFull` 是兜底保护而非常态路径。

## 6. 本讲小结

- `try_send_chunk` 是背压核心入口，用非阻塞 `try_send` 把 GIL 线程的等待降到零，三分支产出 `Ready` / `Pending` / `Closed` 三态。
- 通道满时不在回调线程等待，而是 `register_pending_send` 占停泊位 + `tokio_handle.spawn` 异步 `send().await`，把阻塞转移给 Tokio worker。
- `pending_sends` 是 `HashSet`，从类型上锁死「单停泊位不变量」`parked(rid) ≤ 1`；第二个停泊请求会触发 `ChannelFull`（`RESOURCE_EXHAUSTED`）的防御性关停。
- 反向通知靠 `mark_send_ready` + `notify_ready`，是边沿触发、恰好一次；终端 chunk 排空后**不**通知，与 Python 侧「终端即结束」契约对齐。
- `ready_callbacks`（回调本体）与 `ready_signals`（错过的事件回放）双表设计，保证「注册」与「排空」任意时序下通知都不丢不重。
- 所有异常终止汇入 `close_channel_with_error`：清四表 → 立案 `terminal_errors` → best-effort 通知 Python abort；消费侧再经 `closed_stream_status` / `terminal_error_status` 翻译成 gRPC `Status`。

## 7. 下一步学习建议

- 本讲只讲了「停泊与反向通知」，但**谁在消费侧触发 abort、谁把取消传回 Python** 还没展开——这正是 **u3-l2（中止传播：RequestAbortGuard 与 abort/abort_all）** 的主题，重点读 `server.rs` 的 `RequestAbortGuard`（RAII 式 drop 触发 abort）与 `bridge.rs` 的 `abort` / `abort_all`。
- 若想把 `TerminalError` → gRPC `Status` 的映射表彻底吃透，接着读 **u3-l3（错误映射：PyErr 到 gRPC Status）**，它会补上 `pyerr_to_status`、`recv_chunk_with_timeout` 的 `DEADLINE_EXCEEDED` 超时路径。
- 本讲频繁出现的 `tokio_handle.spawn` / `Python::with_gil` 涉及 Tokio 运行时与 GIL 的协作，**u3-l5（Tokio 运行时与 GIL 协作模型）** 会系统讲解为何 gRPC 服务独占一个 OS 线程、回调为何要保存 `tokio_handle`。
- 想验证理解，可回头重读 [bridge.rs:L524-L607](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L524-L607) 的 `try_send_chunk`，试着不看讲义复述三分支的全部副作用。

# 中止传播：RequestAbortGuard 与 abort/abort_all

## 1. 本讲目标

本讲解决一个核心问题：**当一个 gRPC 请求被提前结束（客户端取消、超时、协议违例，或服务端主动 abort_all）时，Rust 端如何把「停止生产」的信号可靠地传回 Python 运行时，避免 Python 在无人消费的通道上空转浪费 GPU？**

学完本讲，你应当能够：

- 说清 `RequestAbortGuard` 的 RAII 设计：`new` / `disarm` / `abort_now` / `Drop` 四条处置路径分别用在什么场景。
- 解释为什么 `spawn_abort` 必须用 `spawn_blocking` 把 GIL 调用挪出 Tokio worker。
- 读懂 `PyBridge::abort` 的两个分支（单请求 `abort` 与批量 `abort_all`）在 `BridgeState` 五张表上的清理动作差异。
- 理解 `remove_channel_refs_locked` 作为「四表统一清理原语」被哪些调用方复用。
- 说清 `abort_all` 为什么在 server 层触发「仅限可信客户端」的安全告警。

## 2. 前置知识

本讲是专家层内容，承接两篇进阶讲义。开始前请确认你理解以下概念（若不熟，先回看对应讲义）：

- **流式 RPC 与 `async_stream::stream!`**（u2-l3）：响应流是一个 `async_stream` 生成器，在 `loop` 里反复 `recv_chunk_with_timeout`，按 `ResponseChunk::Data/Finished/Error` 与 `Ok(None)` 四分支处理。本讲的 guard 就创建在 `stream!` 块的最开头。
- **`PyBridge` 与 `BridgeState` 五张表**（u2-l5）：桥接层用一把 `std::sync::Mutex` 护住五张以 `rid` 为主键的表——`channels`（mpsc `Sender`）、`pending_sends`、`ready_callbacks`、`ready_signals`、`terminal_errors`。`rid` 是请求唯一标识，`TerminalError`（`ChannelFull` / `ClientDisconnected` / `Aborted`）是通道的终止「案底」。

补充两个本讲会用到的 Rust 基础概念：

- **RAII（Resource Acquisition Is Initialization）**：资源的生命周期绑定到对象的生命周期，对象析构（`Drop`）时自动释放。本讲把「向 Python 发 abort」这件事绑定到 guard 的 `Drop`，于是「忘记调用清理」在语言层面变成不可能——只要 guard 还 armed，被 drop 时就一定触发 abort。
- **GIL（Global Interpreter Lock）**：CPython 的全局解释器锁，任何调用 Python C API 的代码都必须先持有 GIL。`Python::with_gil(...)` 是 PyO3 获取 GIL 的入口。GIL 调用是同步阻塞的，不能卡住 Tokio 的异步 worker。

## 3. 本讲源码地图

本讲只涉及两个文件，但它们分处「消费侧」与「桥接侧」：

| 文件 | 角色 | 本讲关注的核心符号 |
| --- | --- | --- |
| `rust/sglang-grpc/src/server.rs` | gRPC 服务实现（消费侧） | `RequestAbortGuard`、`spawn_abort`、`recv_terminal_chunk_for_request`、`closed_stream_status`、`abort` RPC |
| `rust/sglang-grpc/src/bridge.rs` | Rust↔Python 桥接（清理执行侧） | `PyBridge::abort`、`close_channel_with_error`、`remove_channel_refs_locked`、`TerminalError`、`BridgeState` |

一句话定位：**server 侧负责「决定要不要 abort 并触发」，bridge 侧负责「真正清理通道表并跨 GIL 调用 Python」。** 两者通过 `Arc<PyBridge>` 与自由函数 `spawn_abort` 衔接。

## 4. 核心概念与源码讲解

### 4.1 RAII 中止 guard：RequestAbortGuard

#### 4.1.1 概念说明

先建立直觉。考虑一个流式 `generate` RPC：Rust 把请求交给 Python，Python 边生成 token 边通过 `chunk_callback` 往 mpsc 通道里塞 `ResponseChunk::Data`，Rust 的 `stream!` 循环从通道里取出并 `yield` 给 tonic，tonic 再发给 gRPC 客户端。

现在客户端中途取消（按了 Ctrl+C、网络断了、或应用主动 cancel 了 RPC）。tonic 会丢弃这个响应流的 `Future`，于是 `stream!` 生成器在某个 `await` 点被中止，其所有局部变量（包括 `receiver`）被 drop。**问题来了：Python 此时并不知道客户端已经走了，仍在辛辛苦苦地生成下一个 token、往通道里塞数据**——只是通道的 `Receiver` 端已经没了，塞进去的 chunk 会触发 `TrySendError` 或最终被丢弃。这些 token 的计算是要烧 GPU 的，必须尽快叫停。

`RequestAbortGuard` 就是为这个场景设计的「自动断电开关」：

- 每个响应流（或一元收银台）开始时，创建一个 **armed（装弹）** 的 guard，记下 `bridge` 与 `rid`。
- 流**正常结束**（收到 `Finished`/`Error`）时，主动 `disarm()`（拆弹）——Python 自己已经结束了，无需再 abort。
- 流**异常结束**（超时、协议违例、通道异常关闭）时，主动 `abort_now()`。
- 无论哪条路径，如果 guard 被销毁时**仍处于 armed**（最典型就是 tonic 因客户端断连而 drop 掉整个流，根本没机会走到任何显式分支），`Drop` 兜底触发 abort。

这就是 RAII 的威力：你不需要在每个可能的中断点都手写「记得 abort」，语言保证「只要还 armed 就一定会 abort」。

#### 4.1.2 核心流程

guard 的状态机只有一个布尔位 `armed`，但处置路径有三条外加一个兜底：

```
                  创建 RequestAbortGuard (armed = true)
                            │
        ┌───────────────────┼────────────────────┐
        ▼                   ▼                    ▼
    正常完成             主动中止             被丢弃(drop)
   disarm()           abort_now()           (兜底路径)
   armed=false        armed=false
   不调 Python        spawn_abort(...)

   注意：abort_now 与 Drop 内部都有
        if self.armed { armed=false; spawn_abort(...) }
   所以「armed」是单次触发（one-shot）标志，
   无论走哪条路径，armed 一旦置 false 就不会再 abort。
```

三条路径在不同收 chunk 场景下的选择（以 `recv_terminal_chunk_for_request` 为代表）：

| 收到的结果 | 含义 | guard 处置 |
| --- | --- | --- |
| `Ok(Some(Finished/ Error))` | 收到正常终止 chunk | `disarm()` |
| `Ok(Some(Data))` | 一元 RPC 不该收到中间数据，协议违例 | `abort_now()` |
| `Ok(None)` | 通道关闭且无终止 chunk | 看 `closed_stream_status`：已有案底→`disarm`；否则→`abort_now` |
| `Err(DeadlineExceeded)` | 超时 | `abort_now()` |
| `Err(其他)` | 其他 gRPC 错误 | `disarm()` |

#### 4.1.3 源码精读

先看 guard 的数据结构与构造。它持有一份 `Arc<PyBridge>`、请求 id 和 armed 标志：

[rust/sglang-grpc/src/server.rs:88-92](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L88-L92) —— `RequestAbortGuard` 结构体定义，三个字段 `bridge` / `rid` / `armed`。

构造函数 `new` 永远以 `armed: true` 出厂，这意味着「只要创建了 guard，就默认需要对它负责」：

[rust/sglang-grpc/src/server.rs:95-101](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L95-L101) —— `new` 把 `armed` 硬编码为 `true`，调用方无法创建一个「天生 disarm」的 guard。

`disarm` 是最朴素的处置——只把标志位置 false，什么都不做：

[rust/sglang-grpc/src/server.rs:103-105](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L103-L105) —— `disarm` 仅置 `armed = false`，用于「Python 已自行结束、无需再 abort」的正常完成路径。

`abort_now` 是「主动中止」：先判断 armed，再置 false 并触发 `spawn_abort`。注意 `armed = false` 写在 `if` 内部，保证「只触发一次」：

[rust/sglang-grpc/src/server.rs:107-112](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L107-L112) —— `abort_now` 的 one-shot 语义：armed 为真才 abort，并立即自降为 false，防止后续 `Drop` 重复 abort。

`Drop` 实现与 `abort_now` 的 `if` 分支完全同构——这正是 RAII 的精髓，把显式 `abort_now` 的逻辑复用到「忘了调用」的兜底场景：

[rust/sglang-grpc/src/server.rs:115-123](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L115-L123) —— `Drop::drop`：若销毁时仍 armed，说明响应流被提前丢弃（典型即客户端断连），调用 `spawn_abort` 把取消传回 Python。

注释点明了关键设计：「不阻塞 Tokio worker」——为什么 `spawn_abort` 而不是直接 `bridge.abort()`，见 4.2。

最后看 guard 在真实 RPC 里的两个使用位点。**流式 RPC**（以 `text_generate` 为例）在 `stream!` 块开头创建 guard：

[rust/sglang-grpc/src/server.rs:242-283](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L242-L283) —— `text_generate` 的 `stream!` 循环：`Finished`/`Error` 走 `disarm`，`Ok(None)` 经 `closed_stream_status` 决策，超时 `Err` 走 `abort_now`。

**一元收银台** `recv_terminal_chunk_for_request`（被 `text_embed`/`embed`/`classify`/`recv_json_response` 共用）同样创建 guard，但对 `Data` 多了一条「协议违例即 abort」的分支：

[rust/sglang-grpc/src/server.rs:141-186](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L141-L186) —— `recv_terminal_chunk_for_request`：四个分支分别对应正常完成、协议违例、通道关闭、超时/错误，每条都明确处置 guard。

这里要特别澄清一个容易混淆的点：**`Ok(None)` 不等于「客户端断开」**。

- 客户端断开（tonic 丢弃响应流 `Future`）会让 `stream!` 生成器在 `await` 点被整体 drop，`abort_guard` 与 `receiver` 一起被析构——走的是 **`Drop` 兜底路径**，根本不会产生 `Ok(None)`。
- `Ok(None)` 表示的是 **mpsc 通道的 `Receiver` 收到 `None`**，即「所有 `Sender` 都已 drop，且没有产出终止 chunk」。它通常由 `close_channel_with_error`（见 4.4）触发——该函数从 `channels` 表移除该 rid 的 sender 引用。此时 `closed_stream_status` 负责裁决：

[rust/sglang-grpc/src/server.rs:188-197](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L188-L197) —— `closed_stream_status`：若 `take_terminal_error` 拿到已记录的案底（如 `ChannelFull`），就报该错误且 `should_abort=false`（Python 那边已知情，无需再 abort）；否则视为「流异常关闭」，`should_abort=true` 让 guard 去 abort Python。

#### 4.1.4 代码实践

**实践目标**：通过阅读源码，建立「收 chunk 结果 → guard 处置」的完整映射，并验证 one-shot 语义。

**操作步骤**：

1. 打开 `rust/sglang-grpc/src/server.rs`，定位 `impl Drop for RequestAbortGuard`（约 115 行）与 `fn abort_now`（约 107 行），对比两者的 `if self.armed { ... }` 块。
2. 跳到 `recv_terminal_chunk_for_request`（约 141 行），逐个分支确认 guard 调用的是 `disarm` 还是 `abort_now`。
3. 再看 `text_generate` 的 `stream!` 循环（约 242 行），对比它和一元收银台在 `Ok(Some(Data))` 分支上的差异。

**需要观察的现象**：

- `abort_now` 与 `Drop::drop` 的触发体完全一致（都是 `spawn_abort(self.bridge.clone(), self.rid.clone())`），区别仅在调用时机。
- `text_generate` 对 `Data` 是正常 `yield`（流式本就要多次 Data），而一元收银台对 `Data` 判定为协议违例并 `abort_now()`。

**预期结果**：你能画出一张「四种 recv 结果 × 两种调用点（流式/一元）」的 guard 处置矩阵。

**待本地验证**：若想动态确认 one-shot，可在 `abort_now` 的 `if` 内临时加一行 `tracing::info!(rid=%self.rid, "abort_now fired")`，构造一个先 `abort_now()` 再让 guard 自然 drop 的场景，观察日志只打印一次（本讲不改源码，仅作思路说明）。

#### 4.1.5 小练习与答案

**练习 1**：假设把 `Drop::drop` 整个删掉，只保留 `abort_now`，系统会出现什么问题？

**参考答案**：客户端断连时 tonic 直接 drop 掉 `stream!` 生成器，`abort_guard` 在没有任何显式 `disarm`/`abort_now` 调用的情况下被析构。没有 `Drop` 兜底，Python 就永远不会收到 abort，继续在无人消费的通道上烧 GPU 生成 token，造成资源泄漏。`Drop` 正是为了覆盖「所有未显式处置」的路径。

**练习 2**：`abort_now` 里为什么把 `self.armed = false` 写在 `if self.armed { ... }` 内部，而不是函数开头无条件置 false？

**参考答案**：为了维持 one-shot 单次触发。如果 guard 已经被 `disarm` 过（armed=false），再调用 `abort_now` 时 `if` 条件不成立，既不会 abort 也不会改变状态；若把 `armed=false` 写在 `if` 外面，虽然行为等价（反正都是 false），但会把「对一个已 disarm 的 guard 调 abort_now」静默接受，掩盖调用方的逻辑错误。写在内部让语义更清晰：只有「真的触发了一次 abort」才翻转标志。

---

### 4.2 spawn_abort：把 GIL 调用挪出 Tokio worker

#### 4.2.1 概念说明

`Drop::drop` 是同步函数，它运行在「正在销毁 guard 的那个线程」上。对于流式 RPC，guard 的销毁发生在 tonic 丢弃响应流时——而 tonic 的响应流跑在 **Tokio worker 线程**上。

问题：`PyBridge::abort` 内部要用 `Python::with_gil(...)` 持锁并调用 Python 的 `abort` 方法（见 4.3），这是**同步阻塞**调用。如果在 Tokio worker 线程上直接同步等 GIL，就会卡住这个 worker，导致同一个 runtime 上的其他异步任务被拖慢，严重时引发死锁（GIL 与某些异步锁形成环等待）。

`spawn_abort` 的职责就是化解这个矛盾：**不在当前线程同步等 GIL，而是把 abort 工作扔到 Tokio 的阻塞线程池（`spawn_blocking`）里异步执行，立即返回。** 这样 `Drop` 瞬间完成，Tokio worker 不被阻塞；真正的 GIL 调用在阻塞池的一个专用线程上发生。

#### 4.2.2 核心流程

```
spawn_abort(bridge, rid)
        │
        ▼
tokio::runtime::Handle::try_current()   ← 探测当前是否在 Tokio 运行时上下文内
        │
   ┌────┴─────┐
   ▼          ▼
 Ok(handle)  Err(_)   ← 不在运行时内（罕见，如测试/关停后）
   │          │
   ▼          ▼
handle       tracing::warn!(跳过 abort)
.spawn_blocking(move || {
    let _ = bridge.abort(&rid, false);   ← 在阻塞线程上持 GIL 调 Python
})
```

两个细节：

- **`try_current` 而非 `current`**：`Handle::current()` 在没有运行时时会 panic，而 `try_current` 返回 `Result`。`spawn_abort` 选择优雅降级——找不到运行时就记一条 warn 跳过，绝不让 drop 路径 panic。
- **`let _ =` 丢弃返回值**：`bridge.abort` 返回 `PyResult<()>`，但这里是 best-effort 清理，abort 失败（比如 Python 方法抛异常）也无法向谁汇报，故静默丢弃。

#### 4.2.3 源码精读

[rust/sglang-grpc/src/server.rs:125-139](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L125-L139) —— `spawn_abort`：用 `Handle::try_current` 探测运行时，成功则 `spawn_blocking` 异步执行 `bridge.abort(&rid, false)`，失败则 warn 跳过。

注意它**固定传 `abort_all = false`**：guard 触发的 abort 永远是「单个 rid」的中止，绝不会升级成 `abort_all`。批量 abort 是一个有安全含义的管理操作，只能由 `abort` RPC 显式发起（见 4.3）。

对比一下同一文件里其他「需要持 GIL」的调用点，它们用的是 `tokio::task::spawn_blocking`（显式前缀），原因完全相同——例如 `health_check`：

[rust/sglang-grpc/src/server.rs:563-572](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L563-L572) —— `health_check` 用 `spawn_blocking` 包住 `bridge.health_check()`（内部 `Python::with_gil`），与 `spawn_abort` 同属「把同步 GIL 调用移出异步 worker」的模式。

> 关于 `Handle::try_current()` 与 `tokio::task::spawn_blocking()` 的区别：前者借当前已存在的 runtime 调度阻塞任务；后者是等价的显式写法。`spawn_abort` 写成 `handle.spawn_blocking(...)` 是因为它先拿到了 `handle`，语义一致。这条规则属于 Tokio 运行时约定，非本项目特有，标注「待确认」可向 Tokio 官方文档核实。

#### 4.2.4 代码实践

**实践目标**：确认「同步 GIL 调用都被 `spawn_blocking` 包裹」这一约定在本文件中的一致性。

**操作步骤**：

1. 在 `rust/sglang-grpc/src/server.rs` 中搜索 `spawn_blocking`，列出所有命中点（如 `tokenize` 的 Python 回退、`get_model_info`、`list_models`、`health_check` 等）。
2. 对每个命中点，确认它内部最终都走到了 `Python::with_gil`（可能跨 `bridge.rs` 的方法）。
3. 对比 `spawn_abort` 用的是 `Handle::try_current().spawn_blocking`，而其他点用 `tokio::task::spawn_blocking`——两者等价。

**需要观察的现象**：所有「async fn 内调 Python」的地方，没有一处是直接 `Python::with_gil` 同步阻塞在 worker 上的。

**预期结果**：你得到一份「async 上下文 → 阻塞池 → GIL」的三段式调用清单，印证本 crate 的并发纪律（详见 u3-l5）。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `spawn_abort` 改成直接 `let _ = bridge.abort(&rid, false);`（去掉 `spawn_blocking`），会有什么后果？

**参考答案**：`Drop::drop` 会在 tonic worker 线程上同步等 GIL。轻则拖慢该 worker 上排队的其他异步任务，重则在 GIL 被 Python 主线程长期持有时让 worker 长时间空转，甚至与其他等待 GIL/异步锁的代码形成死锁。`spawn_blocking` 把这次阻塞挪到专用阻塞线程，worker 立即获释。

**练习 2**：为什么 `spawn_abort` 在 `Err(_)`（无运行时）分支只 warn 而不 panic？

**参考答案**：因为它最常在 `Drop` 路径被调用，而 `Drop` 里 panic 几乎等于「析构期间中毒」，会让对象销毁链路半途崩溃。abort 本身是 best-effort 的资源回收，即便偶尔跳过，最坏后果只是 Python 多生成几个 token 后自行结束；不值得用 panic 来换取「绝不漏 abort」。

---

### 4.3 PyBridge::abort / abort_all：批量 drain 与单个清理

#### 4.3.1 概念说明

`RequestAbortGuard` 只负责「决定 abort 并异步触发」，真正「清理通道表 + 跨 GIL 调 Python」的脏活在 `PyBridge::abort`。它用同一个方法签名承载两种语义：

- **单请求 abort**（`abort_all = false`）：只中止指定 `rid`。这是 guard 与单 rid `abort` RPC 走的路径。
- **批量 abort_all**（`abort_all = true`）：中止**当前所有在途请求**。只有 `abort` RPC 显式传 `abort_all = true` 才会走，guard 永远不会触发（见 4.2.3）。

两者的共同目标是：让 Python 停止为这些 rid 继续生成，并在 `BridgeState` 里立下「`TerminalError::Aborted`」案底，好让仍在等待的 `recv_terminal_chunk_for_request` / `stream!` 能通过 `closed_stream_status` 把取消翻译成正确的 gRPC `CANCELLED` 状态码（错误映射详见 u3-l3）。

#### 4.3.2 核心流程

```
PyBridge::abort(rid, abort_all)
        │
        ├─ 若 !abort_all && rid 为空 → 直接返回 PyValueError（参数校验）
        │
        ▼
   ┌──── abort_all ? ────┐
   │ true                │ false
   ▼                     ▼
 持锁：                  持锁：
   channels.drain()        remove_channel_refs_locked(state, rid)
     → 收集所有 rid         → 返回 was_active
   pending_sends.clear()  若 was_active：
   ready_callbacks.clear()   terminal_errors.insert(Aborted{rid})
   ready_signals.clear()  否则：
   对每个 rid：              debug log「忽略非活跃 rid」
     terminal_errors
       .insert(Aborted{rid})
   should_call_python = true   should_call_python = was_active
   │                     │
   └──────────┬──────────┘
              ▼
   if !should_call_python { return Ok(()) }   ← 单 abort 命中非活跃 rid 时提前返回
              │
              ▼
   Python::with_gil(|py| runtime_handle.call_method1("abort", (rid, abort_all)))
```

两个关键设计点：

- **批量分支总是调 Python**（`should_call_python = true`）：因为 `drain()` 清空了全部在途请求，必须通知 Python 全部停掉。
- **单请求分支按 `was_active` 决定**：如果该 rid 根本不在 `channels` 表里（早已结束或从未存在），就既不立案底也不调 Python，只记一条 debug 日志——避免为「abort 一个早已完成的请求」去无谓地打扰 Python。

#### 4.3.3 源码精读

入口先做空 rid 校验（与 server 侧 `abort` RPC 的校验呼应，见 4.4 实践）：

[rust/sglang-grpc/src/bridge.rs:213-218](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L213-L218) —— `abort` 开头：非 `abort_all` 时空 rid 直接 `PyValueError`。

**`abort_all` 分支**——一次性清空四张「活的」表，并为每个 rid 立案底：

[rust/sglang-grpc/src/bridge.rs:220-238](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L220-L238) —— `abort_all` 分支：`channels.drain()` 拿走全部 rid，`clear()` 另三表，逐 rid 写 `TerminalError::Aborted`，记 `affected` 数。

注意它**不清空 `terminal_errors`**——因为刚为每个 rid 写入了 `Aborted`，若再 clear 就自相矛盾了。这与 `create_channel`（建新通道时清旧案底）形成对照。

**单请求分支**——委托给 `remove_channel_refs_locked`（见 4.4），按返回值决定是否立案底：

[rust/sglang-grpc/src/bridge.rs:239-250](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L239-L250) —— 单 abort 分支：`was_active` 为真才写 `Aborted` 案底并调 Python，否则只 debug 日志。

**跨 GIL 调 Python**——统一在分支之后执行，且 `call_method1("abort", (rid, abort_all))` 把两个参数原样透传给 Python 端的 `RuntimeHandle.abort`：

[rust/sglang-grpc/src/bridge.rs:252-261](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L252-L261) —— 收尾：`should_call_python` 为假则提前返回；否则持 GIL 调 `runtime_handle.abort(rid, abort_all)`。

这里有个**锁与 GIL 的顺序纪律**值得强调：所有对 `BridgeState` 的写操作都在 `lock_or_recover` 的临界区内完成并 `drop(state)` 释放锁，**之后**才进入 `Python::with_gil`。绝不在持 bridge 锁的同时去抢 GIL——这避免了「bridge 锁 ↔ GIL」形成跨线程的锁序环。回顾 `close_channel_with_error` 也遵守同样的纪律：

[rust/sglang-grpc/src/bridge.rs:449-461](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L449-L461) —— `close_channel_with_error`：先在临界区内 `remove_channel_refs_locked` + 写案底，`drop(state)` 后才 `call_method1("abort", ...)`，与 `PyBridge::abort` 同构。

最后看 server 侧的 `abort` RPC 怎么调它，以及那条安全告警：

[rust/sglang-grpc/src/server.rs:654-674](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L654-L674) —— `abort` RPC：校验 rid/abort_all 组合，`abort_all` 时 warn「仅限可信客户端」，再委托 `bridge.abort`。

#### 4.3.4 代码实践

**实践目标**：解释 `abort_all` 触发的安全告警，并对比单 abort 与 abort_all 在表清理上的差异。

**操作步骤**：

1. 打开 `rust/sglang-grpc/src/server.rs` 的 `abort` RPC（约 654 行），读 `if req.abort_all { tracing::warn!(...) }` 那条日志。
2. 打开 `rust/sglang-grpc/src/bridge.rs` 的 `abort`（约 213 行），对比 `abort_all` 分支的 `drain()/clear()` 与单 abort 分支的 `remove_channel_refs_locked`。
3. 回看 `run_grpc_server` 上方的 `TODO(grpc-auth)` 注释（约 973-977 行），确认当前 gRPC listener 是否有鉴权。

**需要观察的现象 / 需要回答的问题**：

- 为什么 `abort_all` 要 warn「仅限可信客户端」？
- 单 abort 命中一个「非活跃 rid」时，Python 会被调用吗？

**预期结果**：

- **告警原因**：`abort_all` 会 `drain()` 掉**全部**在途请求的通道、通知 Python 中止**全部**生成任务。在多租户共享同一个 gRPC server 的部署里，任何一个能连上该端口的客户端都能用一次 `abort_all` 把**所有其他租户**的请求一并干掉——这是一种跨租户的拒绝服务（DoS）能力。server 层因此把它标注为「admin 级」操作；而当前 `run_grpc_server` 的 listener 是**未鉴权**的（见 `TODO(grpc-auth)`），所以必须靠部署侧保证该端口只暴露给可信客户端（如仅限本机或内网控制面）。
- **非活跃 rid**：`was_active` 为 false，`should_call_python` 随之为 false，方法在写案底之前就 `return Ok(())`，**不会**调用 Python。

**待本地验证**：若要动态确认，可写一个调用 `abort(rid="never-existed", abort_all=false)` 的场景，观察日志出现「Ignoring abort for inactive gRPC request id」且 Python 侧 `abort` 方法未被调用（本讲不改源码，仅作思路说明）。

#### 4.3.5 小练习与答案

**练习 1**：`abort_all` 分支为什么用 `channels.drain()` 而不是遍历 `channels.keys()` 再逐个 `remove`？

**参考答案**：`drain()` 在一次临界区内把整张表清空并交出所有权（返回所有 `(rid, sender)`），既高效（一次操作）又原子（不会因为在遍历中修改而触发借用错误）。逐个 `remove` 不仅啰嗦，还可能在每次删除后让 `keys()` 迭代器失效。此外 `drain()` 拿到的 rid 列表正好用于随后逐个写 `TerminalError::Aborted`。

**练习 2**：单 abort 分支里，如果 `remove_channel_refs_locked` 返回 `false`（该 rid 在四张表里都不存在），会发生什么？这个设计合理吗？

**参考答案**：`was_active=false` → 不写案底、`should_call_python=false` → 提前 `return Ok(())`，只记一条 debug 日志。合理：对一个早已结束或从未存在的 rid 调 abort，既没有通道要清，也没有 Python 任务要停，调用 Python 反而是浪费 GIL；静默忽略 + 日志足以满足可观测性。

---

### 4.4 remove_channel_refs_locked：四表统一清理原语

#### 4.4.1 概念说明

回顾 u2-l5：一个 rid 的「活的状态」散落在 `BridgeState` 的四张表里——`channels`（mpsc sender）、`pending_sends`（背压停泊位，见 u3-l1）、`ready_callbacks`（背压就绪回调）、`ready_signals`（错过的就绪事件回放）。要彻底「终结」一个 rid，必须把这四张表里它的痕迹全部抹掉，漏一张都会留下僵尸引用。

`remove_channel_refs_locked` 就是这个「统一清理原语」。它被设计成**接收已持锁的 `&mut BridgeState`**（名字里的 `_locked` 后缀是本 crate 的约定，表示「调用方已持有锁，函数内不再加锁」），避免重复加锁/锁序问题。它返回一个布尔值表示「这些表里到底有没有这个 rid」，供调用方决策后续动作。

#### 4.4.2 核心流程

```
remove_channel_refs_locked(state: &mut BridgeState, rid)
        │
        ├─ had_channel  = state.channels.remove(rid).is_some()
        ├─ had_pending  = state.pending_sends.remove(rid)
        ├─ had_callback = state.ready_callbacks.remove(rid).is_some()
        ├─ had_signal   = state.ready_signals.remove(rid)
        │
        ▼
   返回 had_channel || had_pending || had_callback || had_signal
   （四表任意一张命中即为 true）
```

> 注意：它**故意不碰 `terminal_errors`**。是否立/清案底是调用方的职责——`abort` 立案底，`remove_channel` 清案底，语义不同，不能混在原语里。

#### 4.4.3 源码精读

[rust/sglang-grpc/src/bridge.rs:463-469](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L463-L469) —— `remove_channel_refs_locked`：四张表各删一次，返回「是否命中任意一张」。

它有三个主要调用方，恰好覆盖三种「终结 rid」的语义：

| 调用方 | 位置 | 调用后对 `terminal_errors` 的处理 | 含义 |
| --- | --- | --- | --- |
| `PyBridge::abort`（单分支） | bridge.rs ~241 | 命中则 `insert(Aborted)` | 主动取消，立案底让消费侧报 `CANCELLED` |
| `PyBridge::remove_channel` | bridge.rs ~437-441 | `terminal_errors.remove(rid)` | 提交失败回滚（见 u2-l5），清掉一切痕迹 |
| `close_channel_with_error` | bridge.rs ~457 | `insert(传入的 error)` | 通道异常（满/断连），立对应案底 |

`PyBridge::remove_channel` 是「干净回滚」的范例——清四表后再额外删案底，确保一个 rid 重用时 `create_channel` 看到的是白纸：

[rust/sglang-grpc/src/bridge.rs:437-441](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L437-L441) —— `remove_channel`：`remove_channel_refs_locked` + `terminal_errors.remove`，提交失败时彻底回滚。

而 `close_channel_with_error` 是「带病因终结」——清四表后写入调用方指定的 `TerminalError`，再 best-effort 通知 Python abort：

[rust/sglang-grpc/src/bridge.rs:449-461](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L449-L461) —— `close_channel_with_error`：清四表 + 写指定案底 + `drop(state)` 后通知 Python abort（锁/GIL 顺序纪律见 4.3.3）。

附带一个便捷包装 `remove_channel_refs`（加锁版），供「只需清理、不在意返回值」的场景（如 `try_send_chunk` 成功投递终端 chunk 后）使用：

[rust/sglang-grpc/src/bridge.rs:471-474](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L471-L474) —— `remove_channel_refs`：自行 `lock_or_recover` 后委托给 `_locked` 版本，丢弃返回值。

#### 4.4.4 代码实践

**实践目标**：把「终结 rid」的三种语义与它们对 `terminal_errors` 的处理对应起来。

**操作步骤**：

1. 在 `rust/sglang-grpc/src/bridge.rs` 搜索 `remove_channel_refs_locked`，列出所有调用点。
2. 对每个调用点，看紧跟其后对 `terminal_errors` 的操作是 `insert` 还是 `remove`，填入上面的表格。
3. 思考：为什么原语本身不处理 `terminal_errors`？

**需要观察的现象**：三种调用方对案底的处理互不相同（立 `Aborted` / 清空 / 立传入病因），证明「案底管理」是调用方语义、不该下沉到原语。

**预期结果**：你理解了「四表清理」与「案底管理」是正交的两个关注点，原语只管前者。

**待本地验证**：可参照 `bridge/tests.rs` 的 `terminal_error_messages_include_request_id` 风格，写一个针对 `remove_channel_refs_locked` 的单元测试（构造一个 `BridgeState`，往四表各塞同一个 rid，断言调用后返回 `true` 且四表都不再含该 rid；再断言对一个陌生 rid 返回 `false`）。注意 `BridgeState` 与该函数目前都是 crate 私有，测试需放在 `bridge/tests.rs`（`use super::*;`）内才能访问。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `remove_channel_refs_locked` 不顺便把 `terminal_errors` 也清掉？

**参考答案**：因为不同调用方对案底的处理相反——`abort` 要**立**案底，`remove_channel` 要**清**案底，`close_channel_with_error` 要**改写**案底。把案底操作塞进原语会让它无法满足所有调用方，只能让每个调用方各取所需。原语只负责「抹掉四张活表里的痕迹」这一所有调用方都需要的共同动作。

**练习 2**：函数名后缀 `_locked` 表达了什么约定？如果调用方忘了自己已经持锁，直接在另一个线程又调了一次加锁版 `remove_channel_refs`，会发生什么？

**参考答案**：`_locked` 约定「调用方已持有 `state` 的锁，本函数不再加锁、直接操作 `&mut BridgeState`」。它接收 `&mut` 而非 `&BridgeStateRef`，编译期就保证「必须正持有可变的锁守卫才能调用」。至于「同一 rid 在不同线程被并发清理」：`Mutex` 保证同一时刻只有一个线程能进入临界区，两次清理会串行化；第二次 `remove` 各表都会 miss（返回 false），不会出错，只是多一次无操作。

---

## 5. 综合实践

**任务**：画出从「gRPC 客户端中途取消一个流式 `generate` 请求」到「Python 停止生成」的完整时序，并标注每一步发生在哪个线程、动到了 `BridgeState` 的哪张表。

**步骤**：

1. 从 tonic 丢弃响应流 `Future` 开始写起。
2. 标注 `stream!` 生成器被 drop → `RequestAbortGuard::drop` 在 **Tokio worker 线程** 上执行。
3. 标注 `drop` 调 `spawn_abort` → `Handle::try_current()` 成功 → `spawn_blocking` 把任务扔到**阻塞线程池**。
4. 在阻塞线程上：`bridge.abort(&rid, false)` → 单 abort 分支 → `lock_or_recover` 拿锁 → `remove_channel_refs_locked` 清四表 → 写 `terminal_errors[rid] = Aborted` → `drop(state)` → `Python::with_gil` 调 `runtime_handle.abort(rid, false)`。
5. 在 **Python 主线程**（持有 GIL）：`RuntimeHandle.abort` 停止该 rid 的生成任务。

**自检问题**（用本讲知识回答）：

- 第 3 步为什么不能直接在 worker 线程上同步调 `bridge.abort`？（答：GIL 同步阻塞会卡住 worker，见 4.2.1。）
- 如果这个 rid 在客户端取消**之前**就已经因为通道满被 `close_channel_with_error` 关闭过，第 4 步的 `remove_channel_refs_locked` 会返回什么？Python 还会被调用吗？（答：返回 false——四表里已无该 rid；`was_active=false` → 不调 Python，见 4.3.2。）
- 假设这时又有一个并发的 `abort_all` RPC 进来，会不会把这个刚被单个 abort 处理过的 rid 再处理一次？（答：不会——单个 abort 已 `remove_channel_refs_locked`，`abort_all` 的 `drain()` 拿到的活跃集合里已不含它。）

**预期产出**：一张标注了线程归属（Python 主线程 / Tokio worker / 阻塞池）与表操作（channels/pending_sends/ready_callbacks/ready_signals/terminal_errors）的时序图或编号清单。

**待本地验证**：本任务为源码阅读型实践，不要求实际运行。若想验证线程归属，可在 `spawn_abort` 的 `spawn_blocking` 闭包内与 `RequestAbortGuard::drop` 内各加一行 `tracing::info!(thread = ?std::thread::current().name(), ...)`（本讲不改源码），观察两者线程名不同。

## 6. 本讲小结

- **RAII 中止**：`RequestAbortGuard` 用一个 `armed` 布尔位把「向 Python 发 abort」绑定到对象生命周期；`disarm`（正常完成）、`abort_now`（主动中止）、`Drop`（兜底，典型即客户端断连）三条路径共享 one-shot 语义——armed 一旦置 false 就不再重复 abort。
- **不阻塞 worker**：`Drop` 与 `abort_now` 都通过 `spawn_abort` 把同步 GIL 调用 `spawn_blocking` 到阻塞线程池，`try_current` 保证无运行时也不 panic，且固定传 `abort_all=false`。
- **两个 abort 语义**：`PyBridge::abort` 的 `abort_all` 分支用 `drain()`+`clear()` 批量清四表并为每个 rid 立 `Aborted` 案底、必调 Python；单请求分支委托 `remove_channel_refs_locked`，按 `was_active` 决定是否立案底与调 Python。
- **锁/GIL 顺序纪律**：所有 `BridgeState` 写操作在临界区内完成并释放锁后，才进入 `Python::with_gil`，避免「bridge 锁 ↔ GIL」锁序环。
- **四表清理原语**：`remove_channel_refs_locked` 负责抹掉 channels/pending_sends/ready_callbacks/ready_signals 四张表里的痕迹，故意不碰 `terminal_errors`——案底管理是调用方的语义（abort 立 / remove_channel 清 / close_channel_with_error 改写）。
- **安全告警**：`abort_all` 是跨租户 DoS 能力，server 层 warn「仅限可信客户端」，而当前 gRPC listener 未鉴权（`TODO(grpc-auth)`），须靠部署侧隔离。

## 7. 下一步学习建议

本讲把「取消信号如何从 Rust 传回 Python」讲透了，但还有两个紧密相邻的主题：

- **错误映射**（u3-l3）：本讲反复提到 `TerminalError::Aborted` 会被翻译成 gRPC `CANCELLED`。下一篇精读 `terminal_error_status` / `closed_stream_status` / `pyerr_to_status`，把 `TerminalError` 三变体与 `PyErr` 系统性地映射到 gRPC `Status` 状态码。
- **背压与停泊**（u3-l1）：本讲的 `remove_channel_refs_locked` 会清掉 `pending_sends` 这张「停泊位」表——这张表的作用、单停泊位不变量 `parked(rid) ≤ 1`、以及 `try_send_chunk` 的 Full 分支如何与 abort 协作，是 u3-l1 的主题。

建议接着读 u3-l3，把「中止 → 状态码」的最后一环补上；若更关心吞吐与背压，可先读 u3-l1 再回看本讲的 `pending_sends` 清理动作。

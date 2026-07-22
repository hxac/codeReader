# 错误映射：PyErr 到 gRPC Status

## 1. 本讲目标

本讲是专家层「错误与生命周期」的关键一篇。学完后你应当能够：

- 说出 sglang-grpc 服务端把内部错误翻译成 gRPC `Status` 的**两条独立来源**：Python 异常（`PyErr`）与桥接终止错误（`TerminalError`）。
- 默写 `pyerr_to_status` 的「客户端错误 vs 服务端错误」两分法，并解释它为何要重新获取 GIL 做 `is_instance_of` 判别。
- 默写 `TerminalError` 三个变体到 gRPC Code 的一一映射（`ChannelFull → RESOURCE_EXHAUSTED`、`ClientDisconnected/Aborted → CANCELLED`）。
- 解释 `recv_chunk_with_timeout` 如何产出 `DEADLINE_EXCEEDED`，以及它与 `RequestAbortGuard` 的协作。
- 说清 `closed_stream_status` 在「已有终端错误」与「流异常关闭」两种情形下的二分判定，以及 `should_abort` 布尔位的语义。

本讲承接 u2-l3（流式 RPC）、u2-l4（一元 RPC）中已经出现的 `Status::internal`、`closed_stream_status`、`recv_terminal_chunk_for_request` 等调用点，把视角从「谁调用了它」收拢到「它内部如何把错误分类」。u3-l2 讲过的 `RequestAbortGuard` 在这里只作为调用方出现，不再展开。

## 2. 前置知识

阅读本讲前，建议你已经掌握：

- **gRPC `Status` 与 Code**：tonic 用 `tonic::Status` 携带一个 `tonic::Code` 枚举（如 `InvalidArgument`、`Internal`、`Cancelled`、`DeadlineExceeded`、`ResourceExhausted`）和一段人类可读 message。客户端据此决定重试、报错还是提示用户。本讲关注的是「服务端如何选对 Code」。
- **PyO3 的 `PyErr`**：Python 侧抛出的任何异常，在 Rust 里都被封装成 `pyo3::PyErr`。`PyValueError`、`PyTypeError`、`PyRuntimeError` 是常见具体类型。`is_instance_of::<T>` 用来判别一个 `PyErr` 是不是某个具体 Python 异常的实例。
- **GIL（全局解释器锁）**：访问 Python 对象（包括读取 `PyErr` 的真实类型）必须持有 GIL。本讲会看到 `Python::with_gil(...)` 的典型用法。
- **`ResponseChunk` 与通道**（u2-l5）：Rust 与 Python 之间用 `tokio::sync::mpsc` 通道传 `ResponseChunk`，消费侧 `receiver.recv()` 返回 `Ok(None)` 表示通道已关闭、再无 chunk。
- **`TerminalError`**（u2-l5 已提及）：桥接层在背压失败、客户端断开、主动中止时立下的「终止错误案底」，存放在 `BridgeState::terminal_errors` 表里。

一句话回顾定位：sglang-grpc 是「Rust gRPC 前端 + Python 调度后端」。请求要跨 GIL 调 Python，响应要跨通道流回 Rust，两端都可能出错。本讲就是这套「错误翻译层」的说明书。

## 3. 本讲源码地图

本讲几乎全部聚焦在一个文件上：

| 文件 | 作用 |
| --- | --- |
| [rust/sglang-grpc/src/server.rs](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs) | gRPC 服务实现，本讲的四个映射函数（`pyerr_to_status`、`terminal_error_status`、`closed_stream_status`、`recv_chunk_with_timeout`）全部在此。 |
| [rust/sglang-grpc/src/bridge.rs](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs) | 定义 `TerminalError` 枚举与 `message()`，以及 `take_terminal_error()` 取案底的入口。 |
| [rust/sglang-grpc/src/server/tests.rs](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server/tests.rs) | `terminal_error_status` 的映射单测，是本讲实践题的范本。 |
| [rust/sglang-grpc/src/bridge/tests.rs](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge/tests.rs) | `TerminalError::message()` 含 rid 的单测。 |

错误映射在调用链中的位置可以画成下面这张「漏斗图」：

```
        Python 世界                      Rust 世界
   ┌─────────────────┐            ┌──────────────────────┐
   │ submit_request  │──PyErr────▶│ pyerr_to_status      │──▶ INVALID_ARGUMENT / INTERNAL
   │ tokenize_py ... │            └──────────────────────┘
   └─────────────────┘
                          (响应阶段)
   ┌─────────────────┐            ┌──────────────────────┐
   │ ChunkCallback   │──通道满/断─▶│ TerminalError 案底   │──▶ terminal_error_status
   │ try_send_chunk  │            │ take_terminal_error  │──▶ RESOURCE_EXHAUSTED / CANCELLED
   └─────────────────┘            └──────────────────────┘
                                        ▲
                                        │ Ok(None) 流关闭
                                  closed_stream_status（二分判定）
                                        ▲
                                        │ 超时
                                  recv_chunk_with_timeout ──▶ DEADLINE_EXCEEDED
```

记牢这张图，本讲 5 个最小模块就是在解释图里的四个箭头盒子。

## 4. 核心概念与源码讲解

### 4.1 错误源全景与 TerminalError

#### 4.1.1 概念说明

sglang-grpc 的服务端错误**不是一个来源，而是两个**，这一点是理解整讲的前提：

1. **Python 异常（`PyErr`）**：发生在「调用 Python 方法」时。典型场景是 `bridge.submit_request(...)` 调 `runtime_handle.submit_request` 时 Python 抛错，或 `tokenize_py`/`health_check` 等同步直调失败。这类错误的特点是「Rust 拿到一个活的 `PyErr` 对象，需要当场翻译」。

2. **桥接终止错误（`TerminalError`）**：发生在「响应流传输过程」中。典型场景是通道被撑满（背压失败）、客户端提前断开、或被 `abort` 主动中止。这类错误不会立刻抛给 RPC 调用方，而是先以「案底」形式存进 `BridgeState::terminal_errors` 表，等消费侧 `receiver.recv()` 拿到 `Ok(None)`（通道关闭）时再去翻案底。

此外还有一类「就地构造」的 `Status`，例如 `classify` 发现 `text` 与 `input_ids` 都为空时直接 `Status::invalid_argument(...)`，或收到 `ResponseChunk::Error(msg)` 时直接 `Status::internal(msg)`。它们不经过映射函数，是显式的内联翻译。

本小节聚焦第二类错误的载体：`TerminalError`。

#### 4.1.2 核心流程

`TerminalError` 是一个只有三个变体的枚举，每个变体都只携带一个 `rid`（请求唯一标识），用来在 message 里指明是哪个请求出了问题：

```text
TerminalError
├── ChannelFull { rid }         // 通道满：客户端消费太慢，背压兜底也失败
├── ClientDisconnected { rid }  // 客户端断开：receiver 端被丢弃 / sender.send 失败
└── Aborted { rid }             // 主动中止：abort RPC 或 abort_all 触发
```

它带一个 `message()` 方法，把变体翻译成人类可读的英文句子，供 `Status` 的 message 字段使用。`message()` 是纯 Rust 字符串拼接，**不持 GIL、不做 IO**，因此可以被任意线程安全调用。

#### 4.1.3 源码精读

`TerminalError` 与 `message()` 定义在 bridge.rs 顶部：

[rust/sglang-grpc/src/bridge.rs:48-67](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L48-L67) — 定义三个变体及 `message()`，每条 message 都把 `rid` 嵌进去，便于排障时定位请求。

注意 `#[derive(Debug, Clone)]`：`TerminalError` 会被复制进 `terminal_errors` 表、也会被 `take_terminal_error` 取出后传给 `terminal_error_status` 消费，因此必须 `Clone`。

案底的「写入」与「取出」分布在两处：

- **写入**：背压与中止逻辑里，例如 [rust/sglang-grpc/src/bridge.rs:541-554](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L541-L554) 在通道满且已有停泊 chunk 时，立 `TerminalError::ChannelFull` 案底并关流；[rust/sglang-grpc/src/bridge.rs:596-604](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L596-L604) 在 sender 已关闭时立 `ClientDisconnected` 案底。这些写入都封装在 `close_channel_with_error` 里（[rust/sglang-grpc/src/bridge.rs:449-457](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L449-L457)）。
- **取出**：消费侧在通道关闭后用 `take_terminal_error` 翻案底。[rust/sglang-grpc/src/bridge.rs:443-446](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L443-L446) — 注意是 `remove`（取出即删），保证同一个终止错误不会被消费两次。

`take_terminal_error` 返回 `Option<TerminalError>`：`Some` 表示「有案底，是已知终止原因」；`None` 表示「没有案底，流是不明原因关闭的」。这个 `Option` 正是 4.5 节 `closed_stream_status` 二分判定的依据。

#### 4.1.4 代码实践

**实践目标**：通过阅读单测，确认 `message()` 一定会把 `rid` 写进错误描述。

**操作步骤**：

1. 打开 [rust/sglang-grpc/src/bridge/tests.rs:3-9](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge/tests.rs#L3-L9)。
2. 阅读断言 `assert!(error.message().contains("rid"))`。
3. 对照 `message()` 的三个分支，确认每个分支的 `format!` 都嵌入了 `{rid}`。

**需要观察的现象**：三个分支的 message 文案不同（`channel full` / `client disconnected` / `Request aborted`），但都包含 rid 字面量。

**预期结果**：在 sglang-grpc 目录执行 `cargo test terminal_error_messages_include_request_id` 应通过（待本地验证，因该 crate 依赖 PyO3 扩展环境，可能需要先准备好 Python 侧构建）。

#### 4.1.5 小练习与答案

**练习 1**：`TerminalError` 为什么必须 `Clone`？如果去掉 `Clone` 会有什么问题？

> **答案**：案底要被存入 `terminal_errors: HashMap<String, TerminalError>`（写入时 move 进去），再被 `take_terminal_error` 取出后传值给 `terminal_error_status(error)` 消费。此外 `close_channel_with_error` 按 `error: TerminalError` 接收，调用方（如 `try_send_chunk`、`abort`）可能在多处复用同一错误；`#[derive(Default)]` 的 `BridgeState` 也要求其值类型可克隆以支持有损恢复（`lock_or_recover` 把锁中毒后的状态原样取回）。去掉 `Clone` 后，多处复用与有损恢复路径都会无法编译。

**练习 2**：`ChannelFull` 与 `ClientDisconnected` 在用户感知上最大的区别是什么？

> **答案**：`ChannelFull` 是「服务端背压兜底失败」——客户端还在连着，但消费太慢，服务端主动判定无法继续；`ClientDisconnected` 是「客户端已经不在了」——网络层断开或 receiver 被丢弃。两者都会关流，但前者指向「客户端太慢」、后者指向「客户端已走」，排障方向不同。

### 4.2 pyerr_to_status：Python 异常的两分法

#### 4.2.1 概念说明

`pyerr_to_status` 解决的问题是：**拿到一个 `PyErr`，该给它配什么 gRPC Code？**

最朴素的做法是全部映射成 `INTERNAL`（服务端内部错误）。但这会让「客户端传了非法参数」和「服务端真的崩了」混在一起，客户端无法区分「我改下参数重试就行」和「服务端有 bug、重试也没用」。

gRPC 协议里，`INVALID_ARGUMENT`（Code 3）专门表示「客户端传入了非法参数」，是非幂等的客户端错误——重试同样的请求还会失败；`INTERNAL`（Code 13）表示服务端内部故障。本函数的核心就是把 Python 的 `ValueError`/`TypeError` 这两类「明显是参数问题」的异常识别出来，归到 `INVALID_ARGUMENT`，其余全部 `INTERNAL`。

#### 4.2.2 核心流程

判别逻辑是一个**两分法**（dichotomy），可以写成：

\[
\text{code}(\text{err}) =
\begin{cases}
\text{INVALID\_ARGUMENT}, & \text{err instanceof } (\text{ValueError} \lor \text{TypeError}) \\
\text{INTERNAL}, & \text{otherwise}
\end{cases}
\]

实现要点有三：

1. **重新获取 GIL**：`err.is_instance_of::<PyValueError>(py)` 需要访问 Python 类型对象，必须持 GIL，因此包在 `Python::with_gil(|py| ...)` 里。
2. **短路或**：用 `||` 串联两个判别，命中任一即为客户端错误。
3. **message 统一拼接**：无论哪一支，message 都是 `format!("{}: {}", context, err)`，`context` 是调用方传入的场景描述（如 `"Failed to submit request"`），`err` 的 `Display` 实现会给出 Python traceback 摘要。

#### 4.2.3 源码精读

[rust/sglang-grpc/src/server.rs:60-76](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L60-L76) — `pyerr_to_status` 的文档注释与实现。注释明确说明了「`PyValueError`/`PyTypeError` = 客户端坏输入 → `INVALID_ARGUMENT`；其余（典型 `PyRuntimeError` 与 tokenizer manager 抛出的 traceback）→ `INTERNAL`」。

调用点遍布 server.rs，例如 [rust/sglang-grpc/src/server.rs:236](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L236)（text_generate 提交请求失败）、[rust/sglang-grpc/src/server.rs:503](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L503)（tokenize 的 Python 回退失败）、[rust/sglang-grpc/src/server.rs:671](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L671)（abort 失败）。它们统一写法是：

```rust
.map_err(|e| pyerr_to_status(e, "Failed to submit request"))?;
```

注意 `e`（`PyErr`）是**按值**传入并被消费的——`pyerr_to_status` 在持 GIL 判别类型后，把 `err` 的 `Display` 结果拼进 message，`PyErr` 本身随后 drop。

一个值得留意的细节：`abort` RPC 在 server.rs 里有两道关卡。先是 [rust/sglang-grpc/src/server.rs:659-663](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L659-L663) 直接判空 `rid` 并返回 `Status::invalid_argument`；若没拦住，bridge 侧的 `abort` 还会再 `Err(PyValueError::new_err(...))` 一次（见 [rust/sglang-grpc/src/bridge.rs:214-218](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L214-L218)），这个 `PyValueError` 经 [rust/sglang-grpc/src/server.rs:671](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L671) 的 `pyerr_to_status` 同样会被识别成 `INVALID_ARGUMENT`——两条路径殊途同归。

#### 4.2.4 代码实践

**实践目标**：验证 `pyerr_to_status` 对 `PyValueError` 与 `PyRuntimeError` 给出不同 Code。

**操作步骤**：

1. 阅读 [rust/sglang-grpc/src/server.rs:66-69](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L66-L69)，确认判别条件是 `is_instance_of::<PyValueError>` 与 `is_instance_of::<PyTypeError>` 的或。
2. （可选，示例代码）在 `server/tests.rs` 里仿照 `terminal_error_status_maps_*` 的风格，加一个需要 Python 解释器的测试。**注意**：`pyerr_to_status` 当前并非 `pub`，直接单测它需要先将其改为 `pub(crate)` 并在 tests 模块里 `use super::pyerr_to_status;`。下面是一段**示例代码**（非项目原有代码），仅说明思路：

```rust
// 示例代码：演示思路，pyerr_to_status 需先改为 pub(crate) 才能这样测
#[test]
fn pyerr_to_status_classifies_value_error_as_invalid_argument() {
    pyo3::Python::with_gil(|py| {
        let err = pyo3::exceptions::PyValueError::new_err("bad input");
        let status = pyerr_to_status(err, "ctx");
        assert_eq!(status.code(), tonic::Code::InvalidArgument);
    });
}
```

**需要观察的现象**：`PyValueError` → `InvalidArgument`；换成 `PyRuntimeError` → `Internal`。

**预期结果**：待本地验证（该测试依赖 Python 解释器，且需要先把目标函数开放为 `pub(crate)`，不建议直接改源码；可作为阅读型练习理解判别逻辑即可）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `pyerr_to_status` 不把所有 Python 异常都映射成 `INTERNAL`？给出一个具体的客户端体验差异。

> **答案**：全映射成 `INTERNAL` 会让客户端无法区分「参数错」与「服务端崩」。例如客户端传了一个非法 `rid` 或空 `text`，Python 抛 `ValueError`；若映射成 `INVALID_ARGUMENT`，客户端 SDK 看到 Code 3 就知道「改参数重试」；若映射成 `INTERNAL`，客户端会误以为是服务端故障而盲目重试、告警，放大问题。

**练习 2**：`is_instance_of::<PyTypeError>` 这一支为什么必要？`TypeError` 在 SGLang 的 Python 后端里通常意味着什么？

> **答案**：`TypeError` 表示「类型不对」，例如把字符串传给了期望 list 的字段、或 None 传给了必填参数。这同样是客户端的锅，应归 `INVALID_ARGUMENT`。在 SGLang Python 后端里，`req_dict` 字段类型不匹配会在调度器入口抛 `TypeError`，把它和 `ValueError` 并列能让所有「输入校验类」异常统一落到客户端错误码。

### 4.3 terminal_error_status：TerminalError 到 gRPC Code

#### 4.3.1 概念说明

`terminal_error_status` 把 `TerminalError` 翻译成 `Status`。与 `pyerr_to_status` 不同，这里的输入已经是 Rust 自己定义的三选一枚举，**不需要 GIL**，映射是纯函数式的 `match`。

设计意图是为每种「桥接层失败」配一个语义最贴切的 gRPC Code：

- `ChannelFull`（通道满）→ `RESOURCE_EXHAUSTED`（Code 8）：资源用尽。通道容量是有限资源，被撑满意味着下游消费速率跟不上。
- `ClientDisconnected`（客户端断开）→ `CANCELLED`（Code 1）：调用被取消。
- `Aborted`（主动中止）→ `CANCELLED`（Code 1）：同样是「调用不再继续」。

注意后两者合并到同一分支，因为对客户端而言「我自己断了」和「服务端主动中止我」的处置是一样的：都不再期待结果。

#### 4.3.2 核心流程

映射表（本讲的「核心契约」）：

| TerminalError 变体 | 触发场景 | gRPC Code | Code 数值 | 客户端建议 |
| --- | --- | --- | --- | --- |
| `ChannelFull { rid }` | 通道满且已有停泊 chunk（背压兜底失败） | `RESOURCE_EXHAUSTED` | 8 | 提高消费速率 / 增大通道容量 / 限流 |
| `ClientDisconnected { rid }` | sender 已关闭或 `send().await` 失败 | `CANCELLED` | 1 | 检查客户端是否提前退出 |
| `Aborted { rid }` | `abort` RPC 或 `abort_all` 主动中止 | `CANCELLED` | 1 | 正常的中止语义 |

message 统一取自 `error.message()`（4.1 节），保证 rid 始终出现在错误描述里。

#### 4.3.3 源码精读

[rust/sglang-grpc/src/server.rs:199-207](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L199-L207) — `terminal_error_status` 实现。先用 `let message = error.message();` 取出描述（在 match 之前，确保三个分支都能用同一个 `message`），再 `match error` 分发 Code。

注意两点：

1. `message` 在 match 前绑定，是**先取描述再 move `error`** 的写法——`error` 在 match 分支里被部分 move，因此必须先把它需要的 `message` 拷出来。
2. `ClientDisconnected` 与 `Aborted` 用 `|` 合并模式，共享 `Status::cancelled(message)`。

单测在 [rust/sglang-grpc/src/server/tests.rs:23-39](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server/tests.rs#L23-L39)，覆盖了 `ChannelFull → ResourceExhausted` 与 `Aborted → Cancelled` 两条。`ClientDisconnected` 与 `Aborted` 共用分支，故单测只验其一即可保证合并分支正确。

#### 4.3.4 代码实践

**实践目标**：把本节的映射表亲手用单测复现一遍（这正是讲义规格要求的「映射表」实践）。

**操作步骤**：

1. 打开 [rust/sglang-grpc/src/server/tests.rs:23-39](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server/tests.rs#L23-L39)。
2. 阅读已有的两个测试：`terminal_error_status_maps_channel_full_to_resource_exhausted` 与 `terminal_error_status_maps_abort_to_cancelled`。
3. 参照风格，**为 `ClientDisconnected` 补一个测试**（示例代码，非项目原有）：

```rust
// 示例代码：补测 ClientDisconnected 分支
#[test]
fn terminal_error_status_maps_client_disconnected_to_cancelled() {
    let status = terminal_error_status(TerminalError::ClientDisconnected {
        rid: "rid".to_string(),
    });
    assert_eq!(status.code(), Code::Cancelled);
}
```

**需要观察的现象**：`ClientDisconnected` 与 `Aborted` 走同一 match 分支，都应是 `Cancelled`。

**预期结果**：补测后 `cargo test terminal_error_status` 三个用例全绿（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ChannelFull` 映射成 `RESOURCE_EXHAUSTED` 而不是 `CANCELLED`？两者都是「拿不到结果」。

> **答案**：`RESOURCE_EXHAUSTED` 准确表达「有限资源（通道容量）被耗尽」，根因是下游消费速率跟不上生产速率，客户端**有可能**通过降速、批量消费或调大容量来成功重试；而 `CANCELLED` 暗示「调用本身被取消」，通常不该盲目重试。语义不同，排障与重试策略也不同。

**练习 2**：若要新增一个 `TerminalError::Timeout { rid }` 变体，本函数与相关测试需要改哪些地方？

> **答案**：(1) bridge.rs 的 `TerminalError` 枚举加变体、`message()` 加分支；(2) `terminal_error_status` 的 `match` 加一条分支（如 `Status::deadline_exceeded(message)`）；(3) 因为现在 `ClientDisconnected | Aborted` 不再穷尽所有非 ChannelFull 情况，编译器会强制你补上新分支；(4) server/tests.rs 补一个映射单测。这正是「`match` 穷尽性」带来的安全性。

### 4.4 recv_chunk_with_timeout 与 DEADLINE_EXCEEDED 超时

#### 4.4.1 概念说明

前两节的映射函数处理的是「已经发生的错误」。本节的 `recv_chunk_with_timeout` 处理的是「等不到响应」——消费侧从通道收 chunk，若超过 `response_timeout` 还没收到，就判定超时。

超时在 gRPC 里映射成 `DEADLINE_EXCEEDED`（Code 4）。它与前面的错误源不同：它不是 Python 抛的、也不是桥接层立的案底，而是**消费侧的时间到判定**，由 `tokio::time::timeout` 直接产出。

#### 4.4.2 核心流程

函数骨架是一个 `timeout(dur, future).await`：

```text
recv_chunk_with_timeout(receiver, response_timeout, msg_fn)
  └─ timeout(response_timeout, receiver.recv()).await
       ├─ Ok(Some(chunk)) → Ok(Some(chunk))   // 正常收到
       ├─ Ok(None)      → Ok(None)             // 通道关闭，原样透传给上层
       └─ Err(Elapsed)  → Err(Status::deadline_exceeded(msg_fn()))  // 超时
```

其中 `receiver.recv()` 返回 `Option<ResponseChunk>`：`Some` 是收到 chunk，`None` 是通道关闭。`timeout` 把「未来完成」与「超时」二选一：

- 内层 `Ok(Some(chunk))` / `Ok(None)`：通道有结果（chunk 或关闭），原样透传。
- 外层 `Err(Elapsed)`：时间到，构造 `DEADLINE_EXCEEDED`，message 由调用方传入的闭包 `timeout_message()` 现场生成（这样不同调用点可以给出不同描述，如一元用 `"Request timed out after Ns"`，流式用 `"Stream chunk timed out"`）。

#### 4.4.3 源码精读

[rust/sglang-grpc/src/server.rs:78-86](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L78-L86) — `recv_chunk_with_timeout` 实现。`timeout(response_timeout, receiver.recv()).await` 的 `map_err` 把 `Elapsed` 错误翻译成 `Status::deadline_exceeded(timeout_message())`。

两个调用场景的 message 闭包不同：

- 一元收银台 [rust/sglang-grpc/src/server.rs:149-151](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L149-L151)：`format!("Request timed out after {}s", response_timeout.as_secs())`。
- 流式循环 [rust/sglang-grpc/src/server.rs:245](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L245)：`"Stream chunk timed out".to_string()`。

**与 RequestAbortGuard 的协作**（u3-l2 已讲 guard，这里只看接点）：超时返回的 `Status` 其 Code 是 `DeadlineExceeded`，上层 `recv_terminal_chunk_for_request` 会据此决定是否中止 Python。[rust/sglang-grpc/src/server.rs:177-184](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L177-L184) — 若 `status.code() == DeadlineExceeded` 则 `abort_guard.abort_now()`（把取消传回 Python，避免空烧 GPU），否则 `disarm()`（正常结束，无需中止）。流式循环 [rust/sglang-grpc/src/server.rs:277-281](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L277-L281) 的 `Err(status)` 分支更直接：无论什么 status 都 `abort_guard.abort_now()`。

#### 4.4.4 代码实践

**实践目标**：理解超时阈值的来源与归一化，并追踪一次超时如何同时产生 `DEADLINE_EXCEEDED` 与一次 Python abort。

**操作步骤**：

1. 阅读 [rust/sglang-grpc/src/server.rs:78-86](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L78-L86)，确认超时由 `tokio::time::timeout` 产出。
2. 回顾 u1-l4：`response_timeout` 来自 `start_server` 的 `response_timeout_secs` 参数，默认 `DEFAULT_RESPONSE_TIMEOUT_SECS = 300`（[rust/sglang-grpc/src/server.rs:27](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L27)），传 0 时回退默认。
3. 追踪调用链：`recv_terminal_chunk_for_request`（[L141-186](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L141-L186)）→ `Err(status)` 分支 → `DeadlineExceeded` 判定 → `abort_guard.abort_now()` → `spawn_abort` → `bridge.abort(&rid, false)`。

**需要观察的现象**：超时不只是给客户端一个 `DEADLINE_EXCEEDED`，还会触发一次给 Python 的 abort，让后端停止为这个请求继续推理。

**预期结果**：能在源码里完整画出「`timeout` 到期 → `Status::deadline_exceeded` → `abort_now` → `spawn_blocking(bridge.abort)`」这条链。本实践为源码阅读型，无需运行。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `timeout_message` 用闭包 `impl FnOnce() -> String` 而不是直接传 `String`？

> **答案**：`FnOnce` 闭包是**惰性求值**——只有在真的超时（`Err(Elapsed)`）时才会调用 `timeout_message()` 构造字符串。如果直接传 `String`，每次调用 `recv_chunk_with_timeout` 都会预先构造一次 message（例如 `format!("Request timed out after {}s", ...)`），即便绝大多数请求正常完成、根本用不上。闭包把字符串构造的开销推迟到真正超时才发生。流式 RPC 在循环里反复调用本函数，这个优化尤其有意义。

**练习 2**：一元收银台里，为什么超时要 `abort_now`，而收到 `Finished` 终止 chunk 时却 `disarm`？

> **答案**：收到 `Finished` 说明请求正常结束，Python 生产者已经收尾，无需再发 abort；而超时意味着 Python 可能**还在继续生产**（GPU 还在算），必须主动 abort 才能止损。`disarm` 是「放下武器」——告诉 guard「请求已正常结束，drop 时不要再 abort」；`abort_now` 则是「立即触发一次 abort 并 disarm」。

### 4.5 closed_stream_status：流关闭的二分判定

#### 4.5.1 概念说明

当 `recv_chunk_with_timeout` 返回 `Ok(None)`，意味着 `receiver.recv()` 拿到 `None`——**通道被关闭了，而且没有附带 chunk**。这是消费侧最微妙的情形，因为它有两种完全不同的成因：

1. **「带案底的关闭」**：桥接层在 `close_channel_with_error` 里**先立 TerminalError 案底、再关闭通道**。这种关闭是「有意的、有原因的」，案底里写明了是 ChannelFull / ClientDisconnected / Aborted 中的哪一种。此时应直接用 `terminal_error_status` 翻译案底，**不需要再 abort** Python——因为案底本来就是桥接层（在 Python 侧的回调线程里）立的，Python 那边已经知情。

2. **「无案底的关闭」**：通道莫名其妙就关了，`terminal_errors` 表里没有对应 rid。这通常是异常情况（例如 receiver 被某处意外 drop、或生产者没发终端 chunk 就退出）。此时没有已知原因可用，只能给一个兜底的 `Status::internal("gRPC response stream closed before a terminal response")`，并且 `should_abort = true`——因为不知道 Python 是否还在生产，保险起见发一次 abort。

`closed_stream_status` 就是做这个二分判定的函数，它返回一个**元组 `(Status, bool)`**：第二个布尔位 `should_abort` 告诉调用方「要不要顺手 abort Python」。

#### 4.5.2 核心流程

判定逻辑：

```text
closed_stream_status(bridge, rid)
  └─ bridge.take_terminal_error(rid)
       ├─ Some(error) → (terminal_error_status(error), false)   // 有案底：用案底，不 abort
       └─ None        → (Status::internal("...closed before..."), true)  // 无案底：兜底 + abort
```

关键点：`take_terminal_error` 是 `remove`（取出即删），所以这条判定是**一次性**的——案底被消费后，同一个 rid 再查就是 `None`。

`should_abort` 的语义对照表：

| 情形 | Status 来源 | should_abort | 理由 |
| --- | --- | --- | --- |
| 有 TerminalError 案底 | `terminal_error_status(error)` | `false` | 案底由桥接层在 Python 回调里立，Python 已知情，无需重复 abort |
| 无案底 | 兜底 `Status::internal(...)` | `true` | 不明原因关闭，保险起见通知 Python 停产 |

#### 4.5.3 源码精读

[rust/sglang-grpc/src/server.rs:188-197](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L188-L197) — `closed_stream_status` 实现。`if let Some(error) = bridge.take_terminal_error(rid)` 走案底分支，`else` 走兜底分支。

调用点有两处，处理 `should_abort` 的方式完全对称：

- 一元收银台 [rust/sglang-grpc/src/server.rs:168-176](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L168-L176)：`if should_abort { abort_guard.abort_now() } else { abort_guard.disarm() }`。
- 流式循环 [rust/sglang-grpc/src/server.rs:267-276](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L267-L276)：同样的 if/else。

这种「`closed_stream_status` 返回 `(Status, should_abort)`，调用方据此二选一处置 guard」的写法，把「翻译错误码」与「决定是否中止」这两件事**解耦**——前者是纯函数式判定，后者涉及 GIL 调用，放在一起会让临界区变复杂。

#### 4.5.4 代码实践

**实践目标**：动手验证「有案底」与「无案底」两条路径产出不同 Code 与不同 `should_abort`。

**操作步骤**：

1. 阅读 [rust/sglang-grpc/src/server.rs:188-197](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L188-L197)，确认 `take_terminal_error` 的 `Some/None` 决定走向。
2. 对照 [rust/sglang-grpc/src/bridge.rs:443-446](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L443-L446)，确认 `take_terminal_error` 是 `remove`（取出即删）。
3. （示例代码，非项目原有）若想单测 `closed_stream_status`，需要构造一个装了案底的 `PyBridge`。由于 `terminal_errors` 是 `BridgeState` 的私有字段、且 `PyBridge` 构造需要 Python `runtime_handle`，**直接单测较重**。更轻的做法是阅读 `close_channel_with_error`（[rust/sglang-grpc/src/bridge.rs:449-457](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L449-L457)），确认它**总是先立案底再关流**，从而保证 `closed_stream_status` 的「有案底」分支在生产路径里确实可达。

**需要观察的现象**：`close_channel_with_error` 内部会 `state.terminal_errors.insert(rid, error)`（在关流之前），这正是 `Ok(None)` 后能 `take_terminal_error` 命中案底的根本原因。

**预期结果**：能在源码里讲清「为什么通道关闭时绝大多数情况下都能在案底表里查到原因」——因为唯一会关流的函数 `close_channel_with_error` 总是先立案底。本实践为源码阅读型。

#### 4.5.5 小练习与答案

**练习 1**：为什么「有案底」时 `should_abort = false`？案底明明也可能是 `Aborted`。

> **答案**：案底是由 `close_channel_with_error` 在**持有 GIL 的回调线程**里立的（见 `try_send_chunk` 的 Full/Closed 分支与 `abort` 逻辑），那一刻 Python 侧已经被通知（或本身就是因为 Python 侧的中止触发的）。因此消费侧再发一次 abort 是多余的，甚至可能与正在进行的 abort 竞争。`should_abort = false` 表示「Python 已知情，消费侧只需把错误码翻译出去」。

**练习 2**：如果 `take_terminal_error` 改成「peek 不删除」（只查不删），会对 `closed_stream_status` 造成什么隐患？

> **答案**：同一个 rid 的通道关闭后，若案底不删除，后续若有任何代码（例如重试逻辑或日志巡检）再次调用 `closed_stream_status`，会再次命中 `Some` 分支并重复返回同一个 `Status`；更危险的是，案底会无限堆积、泄漏内存。`remove` 语义保证了「一案底一消费」的线性性，是正确性而非优化。

## 5. 综合实践

把本讲五个模块串起来，做一个「错误映射全表」整理任务：

**任务**：在 `sglang-rust-sglang-grpc-tutorial/` 下新建一个笔记（或直接在本讲义末尾手写），完成下面三件事。

1. **TerminalError → gRPC Code 映射表**（对应 4.3 节实践）。把 `ChannelFull / ClientDisconnected / Aborted` 三个变体、各自的触发源码行号、对应的 gRPC Code（`RESOURCE_EXHAUSTED / CANCELLED / CANCELLED`）、客户端处置建议，整理成一张表。要求每行附一个永久链接指向「立案底」的源码位置（提示：`ChannelFull` 在 [bridge.rs:541-554](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L541-L554)，`ClientDisconnected` 在 [bridge.rs:596-604](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L596-L604)，`Aborted` 在 [bridge.rs:245](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L245) 附近）。

2. **`pyerr_to_status` 两分法追踪**（对应 4.2 节实践）。写一段文字说明：调用方传入的 `PyErr` 如何在 `Python::with_gil` 内被 `is_instance_of::<PyValueError> || is_instance_of::<PyTypeError>` 判别，命中走 `Status::invalid_argument`、未命中走 `Status::internal`；并解释 message 为何统一用 `format!("{}: {}", context, err)`。

3. **一次 `Ok(None)` 的完整旅程**（综合 4.4 与 4.5）。画出从「`recv_chunk_with_timeout` 返回 `Ok(None)`」开始，经过 `closed_stream_status` 的二分判定，到「决定 `should_abort`、构造最终 `Status`」的时序图。要求标注：案底存在时走哪条路、案底不存在时走哪条路、超时（`Err(status)`）又走哪条路。

**验收标准**：三件事都附有指向真实源码行的永久链接，且没有编造的函数名或行号。映射表中的 Code 必须与 [server/tests.rs:23-39](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server/tests.rs#L23-L39) 的单测断言一致。

## 6. 本讲小结

- sglang-grpc 服务端的错误有**两个独立来源**：跨 GIL 调 Python 产生的 `PyErr`，与桥接层响应流传输出错产生的 `TerminalError`；二者分别由 `pyerr_to_status` 和 `terminal_error_status` 翻译。
- `pyerr_to_status` 用「`PyValueError`/`PyTypeError` → `INVALID_ARGUMENT`，其余 → `INTERNAL`」的两分法，让客户端能区分「参数错」与「服务端故障」；判别必须重新获取 GIL。
- `TerminalError` 三变体到 gRPC Code 的一一映射：`ChannelFull → RESOURCE_EXHAUSTED`、`ClientDisconnected/Aborted → CANCELLED`；`message()` 始终内嵌 rid 便于排障。
- `recv_chunk_with_timeout` 用 `tokio::time::timeout` 产出 `DEADLINE_EXCEEDED`，并通过惰性闭包 `timeout_message` 避免无谓的字符串构造；超时会触发 `abort_now` 把取消传回 Python。
- `closed_stream_status` 对 `Ok(None)` 做二分判定：有 TerminalError 案底则用其翻译且 `should_abort=false`，无案底则兜底 `INTERNAL` 且 `should_abort=true`；返回 `(Status, bool)` 把「翻译错误码」与「决定是否中止」解耦。
- 所有映射函数都是纯函数式的、不跨 `await`、临界区极短，符合桥接层「先在锁内写账本、释锁后再持 GIL」的纪律。

## 7. 下一步学习建议

- 接下来读 **u3-l4「Tonic 服务引导、消息大小上限与优雅关停」**，看 `run_grpc_server` 如何把这里的 `Status` 经由 tonic transport 层送回客户端，以及 `serve_with_incoming_shutdown` 与错误路径的关系。
- 若想深入「案底是怎么被立的」，重读 **u3-l1「背压与 pending-send 停泊机制」**中 `close_channel_with_error` 与 `try_send_chunk` 的 Full/Closed 分支——本讲只用了它们的产物（TerminalError），那里讲的是它们的产生过程。
- 若对「abort 如何把取消可靠传回 Python」感兴趣，重读 **u3-l2「中止传播：RequestAbortGuard 与 abort/abort_all」**，本讲的 `should_abort` / `abort_now` 全部由那里的 guard 机制承载。
- 想动手扩展错误映射的同学，可以参照 **u3-l6「测试组织与扩展实践」**，为本讲提到的 `pyerr_to_status`（需先开放为 `pub(crate)`）或 `closed_stream_status` 补单测，练习「映射函数 → 单测断言 Code」的闭环。

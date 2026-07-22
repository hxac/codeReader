# Tonic 服务引导、消息大小上限与优雅关停

## 1. 本讲目标

本讲是专家层（u3）的第四篇，聚焦 `sglang-grpc` 这个原生 gRPC 服务从「开机」到「关机」的工程化细节。读完本讲，你应当能够：

- 说清 `run_grpc_server` 这个异步入口的完整引导顺序：解析地址、把标准库 `TcpListener` 移交 Tokio、组装 `SglangServiceImpl`、挂到 tonic `Server` 上。
- 理解为什么 tonic 默认的 4 MiB 消息上限不够用，以及 `DEFAULT_GRPC_MAX_MESSAGE_SIZE`（64 MiB）和 `SGLANG_TONIC_PAYLOAD` 环境变量如何共同决定编解码上限。
- 逐分支讲清 `resolve_max_message_size` 对「合法正数 / 0 / 非数字字符串 / 未设置」四种输入的不同处理与回退策略。
- 追踪从 Python 调用 `GrpcServerHandle.shutdown()` 到 tonic `Server` 真正停止的完整信号链：`Notify::notify_one()` → `shutdown.notified().await` → `serve_with_incoming_shutdown`。

本讲只关心「服务怎么起来、能吃多大的包、怎么优雅停下」这三件事，不展开具体的 RPC 业务逻辑（那是 u2 的主题），也不展开并发背压（u3-l1）与中止传播（u3-l2）。

## 2. 前置知识

本讲承接 u1-l4（`start_server` 与 `GrpcServerHandle` 生命周期）与 u2-l2（`SglangServiceImpl` 总览），下面只用通俗语言补充几个本讲用得到、但前序讲义未细讲的概念。

- **tonic transport Server**：tonic 是 Rust 的 gRPC 框架。`tonic::transport::Server` 是「传输层服务器」，它接受 TCP 连接、完成 HTTP/2 握手、把每条连接上的 RPC 分发给对应的 service。本讲的 `run_grpc_server` 做的就是「建一个 Server，把 `SglangServiceServer` 挂上去，开始接客」。
- **消息大小上限（max message size）**：gRPC over HTTP/2 在传输一条请求或响应时，本质上是一个完整的 protobuf 序列化字节流。为防止恶意/超大包把内存打爆，tonic 对「解码（收到的请求）」和「编码（发出的响应）」各设一个字节上限。默认只有 4 MiB，对 LLM 场景远远不够。
- **`Notify`**：Tokio 提供的异步「一次性通知」原语。你可以把它理解成一盏信号灯：任意一方调用 `notify_one()`，等待方那一句 `notified().await` 就会被唤醒。它不携带数据，只负责「叫醒」。
- **优雅关停（graceful shutdown）**：服务收到停止信号后，不再接受新连接/新请求，但允许正在进行中的请求尽量跑完，最后再退出进程/线程，避免「一刀切」导致的请求丢失。

## 3. 本讲源码地图

本讲涉及两个源文件：

| 文件 | 作用 |
| --- | --- |
| `rust/sglang-grpc/src/server.rs` | gRPC 服务的实现与引导：`SglangServiceImpl`、`resolve_max_message_size`、`run_grpc_server`。 |
| `rust/sglang-grpc/src/lib.rs` | PyO3 入口与生命周期：`start_server`、`GrpcServerHandle`（含 `shutdown`/`is_alive`）、`Notify` 的创建与跨线程传递。 |
| `rust/sglang-grpc/src/server/tests.rs` | 单元测试，含 `resolve_max_message_size_honors_env_var`，是理解消息上限解析的最佳参照。 |

调用关系总览：

```text
lib.rs::start_server
  ├── 创建 Arc<Notify> shutdown
  ├── spawn OS 线程 "sglang-grpc"
  └── 线程内 rt.block_on(server.rs::run_grpc_server(listener, bridge, shutdown, timeout))
                                  │
                                  ├── resolve_max_message_size()  ← 读 SGLANG_TONIC_PAYLOAD
                                  ├── 组装 SglangServiceImpl
                                  ├── tonic Server.serve_with_incoming_shutdown(..., shutdown.notified())
                                  └── （收到 notify）优雅停止

lib.rs::GrpcServerHandle::shutdown   ← Python 调用入口
  ├── shutdown.notify_one()          ← 点亮信号灯，唤醒上面的 notified().await
  └── join_handle.join()             ← 等 OS 线程退出
```

## 4. 核心概念与源码讲解

### 4.1 run_grpc_server：服务引导主流程

#### 4.1.1 概念说明

`run_grpc_server` 是 tonic 服务真正的「开机」函数。它是一个 `async fn`，运行在 `start_server` 专门 spawn 的那条名为 `sglang-grpc` 的 OS 线程里（线程内由 Tokio 运行时 `block_on` 驱动，参见 u1-l4）。

它需要解决三件事：

1. **接管监听器**：地址绑定已经在 `start_server` 里用标准库 `TcpListener::bind` 完成了（这样端口占用能在启动期立刻报错，而不是等到异步运行时里才报）。这里要把它从标准库世界「搬」到 Tokio 世界。
2. **组装 service**：把 `bridge` 与 `response_timeout` 塞进 `SglangServiceImpl`，再用 tonic 生成的 `SglangServiceServer` 包一层，并设置消息大小上限。
3. **开机并等待关停信号**：调用 `serve_with_incoming_shutdown`，它一边接客，一边监听一个「关停 future」；关停 future 一完成，tonic 就进入优雅停止流程。

#### 4.1.2 核心流程

```text
输入: std::net::TcpListener, Arc<PyBridge>, Arc<Notify>, response_timeout
  │
  1. listener.local_addr()            → 拿到实际监听地址（用于日志）
  2. tokio::net::TcpListener::from_std → 标准库 listener 转 Tokio listener
  3. 构造 SglangServiceImpl { bridge, response_timeout }
  4. max_message_size = resolve_max_message_size()
  5. svc = SglangServiceServer::new(service)
            .max_decoding_message_size(max)
            .max_encoding_message_size(max)
  6. Server::builder().add_service(svc)
       .serve_with_incoming_shutdown(TcpListenerStream(listener), 关停 future)
  7. （阻塞在此）直到关停 future 完成、所有连接处理结束
  │
返回: Ok(())（或 tonic 报错）
```

第 2 步是关键细节：`from_std` 要求传入的 `TcpListener` 必须是**非阻塞**模式。这就是为什么 u1-l4 里 `start_server` 在 `bind` 之后还要调用 `listener.set_nonblocking(true)`——如果不设，这里 `from_std` 会失败。

#### 4.1.3 源码精读

函数签名与文档注释（含一条 TODO：当前 listener 无鉴权，暴露前需加上与 HTTP 服务一致的 API/admin key 校验）：

[server.rs:978-983](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L978-L983) —— `run_grpc_server` 的入口，接收四个参数：标准库 `TcpListener`、桥接器、关停信号 `Arc<Notify>`、响应超时。

接管监听器与组装 service：

[server.rs:984-989](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L984-L989) —— `local_addr()` 取真实地址；`from_std` 把标准库 listener 转成 Tokio listener；随后用 `bridge` 和 `response_timeout` 构造 `SglangServiceImpl`（参见 u2-l2，它只有这两个字段）。

设置消息上限并开机：

[server.rs:991-1004](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L991-L1004) —— 先用 `resolve_max_message_size()` 拿到上限（4.2 详解），用 `max_decoding_message_size` / `max_encoding_message_size` 同时约束收发，再用 `serve_with_incoming_shutdown` 把监听器交给 tonic，第二个参数是关停 future（4.3 详解）。

`serve_with_incoming_shutdown` 的返回值 `.await?` 会一直挂起，直到服务正常停止或出错；函数最后返回 `Ok(())`：

[server.rs:1004-1007](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L1004-L1007)。

#### 4.1.4 代码实践

**实践目标**：确认「监听器接管」这一步对非阻塞模式的前置依赖。

1. 打开 `rust/sglang-grpc/src/server.rs`，定位到 `run_grpc_server` 的 `tokio::net::TcpListener::from_std(listener)?` 这一行。
2. 再打开 `rust/sglang-grpc/src/lib.rs`，找到 `start_server` 里的 `listener.set_nonblocking(true)`。
3. **需要观察的现象**：两者构成一条契约——`start_server` 设置非阻塞，`run_grpc_server` 才能安全 `from_std`。
4. **思考题**：假设有人删掉了 `set_nonblocking(true)`，`from_std` 在 Tokio 1.x 下会怎样？（提示：Tokio 的 `from_std` 在非阻塞未设置时通常会返回错误，拒绝接管一个会阻塞 event loop 的 fd。）
5. **预期结果**：你应当能用自己的话说明「为什么端口绑定放在 `start_server`、而 `from_std` 放在 `run_grpc_server`」——前者保证启动期报错时机，后者保证 listener 在异步世界里可被多路复用。

> 待本地验证：`from_std` 在不同 Tokio 版本下的确切报错文案以本地 `cargo doc --open` 中 `tokio::net::TcpListener::from_std` 的文档为准。

#### 4.1.5 小练习与答案

**练习 1**：`run_grpc_server` 为什么不自己 `TcpListener::bind`，而要接收一个已经 bind 好的 listener？

**参考答案**：因为 bind 失败（端口被占用）属于「启动期错误」，应该在 `start_server` 同步阶段就抛成 `PyRuntimeError`，让 Python 调用方立即知道；如果放到异步 `run_grpc_server` 里才 bind，错误要等到 OS 线程起来、运行时 `block_on` 之后才暴露，错误时机更晚、更难定位。

**练习 2**：`run_grpc_server` 的返回类型是 `Result<(), Box<dyn std::error::Error + Send + Sync>>`，为什么错误类型不用 tonic 的 `Status`？

**参考答案**：`Status` 是「单条 RPC 的业务错误码」，而 `run_grpc_server` 报的是「整个服务起不来/崩了」的启动级错误（如 `local_addr` 失败、tonic 传输层错误）。用 `Box<dyn Error + Send + Sync>` 是 Rust 里常见的「聚合多种来源错误」的兜底写法，这些错误最终在 `start_server` 的 spawn 闭包里只被 `tracing::error!` 记录，不会回传成某条 RPC 的状态码。

---

### 4.2 消息大小上限：DEFAULT_GRPC_MAX_MESSAGE_SIZE 与 resolve_max_message_size

#### 4.2.1 概念说明

gRPC 默认对单条消息（一次请求或一次响应的 protobuf 序列化字节流）有大小限制。tonic 的默认值是 4 MiB。对 SGLang 这种 LLM 服务来说，这个默认值会经常踩雷：

- **多模态输入**：图片、视频帧序列化后体积很大。
- **OpenAI 透传**：`sglang-grpc` 把整个 OpenAI 格式的请求当作不透明 `bytes json_body` 透传（参见 u2-l1 的 proto 契约），长 prompt + 多轮对话很容易超过 4 MiB。
- **流式响应的单个 chunk**：虽然流式把响应拆成多块，但每一块仍受编码上限约束。

因此 `sglang-grpc` 把默认上限提高到 64 MiB，并允许用环境变量 `SGLANG_TONIC_PAYLOAD` 覆盖。换算关系：

\[
64\ \text{MiB} = 64 \times 1024 \times 1024\ \text{字节} = 67{,}108{,}864\ \text{字节}
\]

#### 4.2.2 核心流程

`resolve_max_message_size` 是一个无参函数，读环境变量 `SGLANG_TONIC_PAYLOAD` 并返回字节数。它的判定逻辑可以画成一张三态决策表：

| `SGLANG_TONIC_PAYLOAD` 的值 | `raw.parse::<usize>()` 结果 | 分支 | 返回值 | 日志 |
| --- | --- | --- | --- | --- |
| 未设置（`Err`） | —— | `Err(_) =>` | `DEFAULT_GRPC_MAX_MESSAGE_SIZE`（64 MiB） | 无 |
| 合法正数，如 `"1048576"` | `Ok(n)` 且 `n > 0` | `Ok(n) if n > 0 =>` | `n`（原样采纳） | `info!` |
| `"0"` | `Ok(0)` | `_ =>`（通配） | 默认值 | `warn!` |
| 非数字，如 `"not-a-number"` | `Err` | `_ =>`（通配） | 默认值 | `warn!` |

注意第二层 `match` 的关键设计：解析成功但 `n == 0` 会落入 `_` 通配分支，被当作非法值回退。这是因为 0 字节上限意味着任何请求都无法通过，等于把服务「哑掉」，属于明显的误配置，宁可回退到默认值并告警。

#### 4.2.3 源码精读

默认上限常量及其注释（解释 64 MiB 的取值理由：为多模态输入和 OpenAI JSON 透传体留余量，远高于 tonic 的 4 MiB 解码默认）：

[server.rs:29-31](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L29-L31) —— `pub const DEFAULT_GRPC_MAX_MESSAGE_SIZE: usize = 64 * 1024 * 1024;`

解析函数（含 TODO：计划将来把 `SGLANG_TONIC_PAYLOAD` 提升为正式的 `--grpc-max-message-size` 启动参数）：

[server.rs:33-58](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L33-L58) —— `resolve_max_message_size`。外层 `match` 区分「变量存在 vs 不存在」；内层对 `raw.parse::<usize>()` 再 `match`，合法正数走 `Ok(n) if n > 0`，其余（0、解析失败）统一走 `_` 打 `warn!` 并回退默认。

回退时的告警日志会把非法原值和默认值都打出来，方便运维定位：

[server.rs:47-54](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L47-L54) —— 非法值分支，`tracing::warn!` 带 `value = %raw` 与 `default = ...`。

最佳参照是单元测试 `resolve_max_message_size_honors_env_var`，它把四种情况串成一个测试，并写了 `SAFETY` 注释说明为什么必须串行（环境变量是进程级全局状态，多测试并行会互相踩）：

[server/tests.rs:41-75](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server/tests.rs#L41-L75) —— 依次断言「未设置→默认」「`1048576`→原样」「`not-a-number`→默认」「`0`→默认」。

#### 4.2.4 代码实践

**实践目标**：亲手验证 `resolve_max_message_size` 对四种输入的行为，并对照测试断言。

1. 阅读 [server.rs:37-58](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L37-L58) 的函数体，在纸上分别代入 `SGLANG_TONIC_PAYLOAD=1048576`、`=0`、`=not-a-number`、未设置 四种情况，写出返回值。
2. 对照 [server/tests.rs:44-75](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server/tests.rs#L44-L75) 的 `assert_eq!`，确认你的推演与测试一致。
3. （可选）尝试运行该测试。在 `rust/sglang-grpc` 目录下：

   ```bash
   cargo test --test '' resolve_max_message_size_honors_env_var
   ```

   > 待本地验证：本 crate 是 `crate-type = ["cdylib"]` 且默认开启 `pyo3/extension-module`（见 `Cargo.toml`）。在某些工具链下直接 `cargo test` 会因为扩展模块不链接 libpython 而报链接错误；若遇到，可尝试 `cargo test --no-default-features` 或参照仓库根目录的 CI 脚本里的实际测试命令。本步属于「能跑就跑」，跑不通也不影响上面两步的源码阅读结论。

4. **需要观察的现象**：测试是单线程串行的（`SAFETY` 注释已说明），四个断言必须按顺序通过；任意一个返回值偏差都会被 `assert_eq!` 抓住。
5. **预期结果**：四种输入分别返回 `67_108_864`、`1_048_576`、`67_108_864`、`67_108_864`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `n == 0` 要被当作非法值回退，而不是「合法但表示无限制」？

**参考答案**：在 tonic 的语义里，`max_decoding_message_size(0)` 表示「最多接受 0 字节」，等于拒绝一切请求——这是一种危险的误配置，而非「无限制」。代码作者选择把它归入通配 `_` 分支、回退默认值并告警，是把「明显的错误配置」安全地兜底，而不是让服务静默哑掉。真正的「无限制」在 tonic 里需要传一个很大的数，而非 0。

**练习 2**：如果想新增一个「上限不得低于某阈值（如 1 MiB）」的校验，应该改在哪里？为什么不会影响 `serve_with_incoming_shutdown`？

**参考答案**：改在 `resolve_max_message_size` 的 `Ok(n) if n > 0 =>` 分支即可（例如再加一个 `if n < 1024*1024 { 回退 }`）。这个函数是纯计算、不跨 `await`，被 `run_grpc_server` 在开机时调用一次。`serve_with_incoming_shutdown` 只消费它返回的最终数值，对校验逻辑无感知，因此不需要改动。

---

### 4.3 优雅关停信号链：serve_with_incoming_shutdown 与 Notify

#### 4.3.1 概念说明

tonic 提供两种开机方式：

- `serve(incoming, ...)`：一直跑到进程被杀。
- `serve_with_incoming_shutdown(incoming, signal)`：额外接收一个 future `signal`；当 `signal` 完成（complete）时，tonic 开始**优雅关停**——停止接受新连接，让进行中的请求尽量收尾，然后 `serve_with_incoming_shutdown` 的 future 才 resolve。

`sglang-grpc` 用的就是第二种。那个 `signal` future 是：

```rust
async move {
    shutdown.notified().await;
    tracing::info!("gRPC server shutting down");
}
```

也就是说，**关停的触发点就是 `shutdown.notified().await` 被唤醒**。而能唤醒它的，正是 `Arc<Notify>` 的另一端调用 `notify_one()`——这发生在 `GrpcServerHandle::shutdown`（4.4 详解）。

#### 4.3.2 核心流程

完整的「关停信号链」可以画成一条单向因果链：

```text
Python 调用 handle.shutdown()
        │
        ▼
lib.rs: shutdown.notify_one()        ← Arc<Notify> 的非异步通知
        │  （唤醒任意一个 .notified().await 等待者）
        ▼
server.rs: shutdown.notified().await 完成
        │  （关停 future 的函数体继续往下执行）
        ▼
打印 "gRPC server shutting down"
        │  （关停 future 整体 complete）
        ▼
tonic Server 进入优雅关停：拒绝新连接，收尾进行中请求
        │
        ▼
serve_with_incoming_shutdown 的 future resolve
        │
        ▼
run_grpc_server 返回 Ok(())
        │
        ▼
lib.rs 线程闭包结束，OS 线程退出
        │
        ▼
handle.shutdown() 里的 join_handle.join() 返回，shutdown() 调用返回
```

关键点：`Notify::notified()` 是「边沿触发」式的等待——如果在调用 `notify_one()` 时还没有人 `await` 在 `notified()` 上，这次通知默认会「丢失」。那为什么这里没问题？因为 `serve_with_incoming_shutdown` 在服务一开机就会 `await` 那个关停 future，而 `shutdown()` 只可能在服务已经起来之后才被 Python 调用。所以「先 await 后 notify」的时序天然成立。

#### 4.3.3 源码精读

关停 future 的构造与开机（这是本模块最核心的一段）：

[server.rs:998-1004](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L998-L1004) —— `tonic::transport::Server::builder().add_service(svc).serve_with_incoming_shutdown(...)`。第一个参数 `TcpListenerStream::new(listener)` 把 Tokio 监听器包成 tonic 能消费的「连接流」；第二个参数 `async move { shutdown.notified().await; ... }` 就是关停 future，它捕获了 `run_grpc_server` 入参里的 `Arc<Notify>`。

注意这个 `async move` 闭包**move 捕获了 `shutdown`**（`Arc<Notify>` 的所有权）。这份数据流要回溯到 `start_server`：在那里 `Notify::new()` 被创建、克隆出 `shutdown_clone`，再随线程闭包 move 进 `run_grpc_server`：

[lib.rs:233-245](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L233-L245) —— `let shutdown = Arc::new(Notify::new());` 与 `let shutdown_clone = shutdown.clone();`，随后 spawn 的线程里调用 `server::run_grpc_server(listener, bridge_clone, shutdown_clone, response_timeout)`。

而留在 Python 句柄那一份 `shutdown`（没有 clone 出去的原始 `Arc`）则被存进 `GrpcServerHandle`：

[lib.rs:253-256](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L253-L256) —— `Ok(GrpcServerHandle { shutdown, join_handle: Some(join_handle) })`。

于是同一个 `Arc<Notify>` 被两端持有：服务线程内 `await notified()`，Python 句柄端 `notify_one()`。这就是优雅关停能跨线程打通的根因。

#### 4.3.4 代码实践

**实践目标**：从 `notify_one()` 出发，手动追踪信号如何穿越线程边界。

1. 打开 `rust/sglang-grpc/src/lib.rs`，定位到 `GrpcServerHandle::shutdown` 里的 `self.shutdown.notify_one()`（4.4 会精读）。
2. 问自己：这个 `self.shutdown` 和 `run_grpc_server` 里 `notified().await` 的那个 `shutdown`，是不是同一个 `Arc<Notify>`？
3. 回到 `start_server`，找到 `let shutdown = Arc::new(Notify::new());`、`let shutdown_clone = shutdown.clone();` 以及构造 `GrpcServerHandle { shutdown, ... }` 三处，确认：
   - `shutdown_clone` 走进了服务线程 → `run_grpc_server` → `notified().await`。
   - 原 `shutdown` 走进了 `GrpcServerHandle` → 等 Python 调用 `notify_one()`。
4. **需要观察的现象**：两份引用指向**同一个** `Notify` 内部状态（`Arc` 的引用计数为 2）。
5. **预期结果**：你能用自己的话讲清「Python 线程调一次 `notify_one()`，为什么能唤醒另一条 OS 线程里 Tokio 上的 `notified().await`」——因为 `Notify` 内部是原子/共享状态，`Arc` 让两端看到的是同一个信号灯，唤醒不依赖线程局部状态。

#### 4.3.5 小练习与答案

**练习 1**：如果 `serve_with_incoming_shutdown` 的关停 future 里没有 `shutdown.notified().await`，只写了 `tracing::info!(...)` 然后立刻结束，会发生什么？

**参考答案**：关停 future 会在开机后几乎立刻 complete，tonic 会立刻进入优雅关停——等于服务一启动就停了。这正好说明 `notified().await` 是「让 future 挂起、直到收到信号才放行」的关键；没有它，关停 future 形同虚设。

**练习 2**：`Notify` 默认是「通知可能丢失」的（store=false 语义）。本设计为什么不需要担心通知丢失？

**参考答案**：因为时序上 `notified().await` 一定先于 `notify_one()` 发生。服务开机时 `serve_with_incoming_shutdown` 就开始 `await` 关停 future，而 `notify_one()` 只能由 Python 调用 `handle.shutdown()` 触发，这只可能发生在服务已经运行之后。所以总是一个「等待者先就位、通知后到达」的顺序，不会出现「notify 先发、没人等、通知丢失」的情况。

---

### 4.4 Python 侧关停入口：GrpcServerHandle::shutdown 与 is_alive

#### 4.4.1 概念说明

`GrpcServerHandle` 是返回给 Python 的句柄（`#[pyclass]`）。它持有两样东西：

- `shutdown: Arc<Notify>` —— 与服务线程共享的那个信号灯。
- `join_handle: Option<std::thread::JoinHandle<()>>` —— spawn 出来的 `sglang-grpc` OS 线程的句柄，用 `Option` 包裹是为了能在 `shutdown` 时 `take()` 出来 `join()`（join 之后置 None，避免重复 join）。

它对 Python 暴露两个方法：

- `shutdown(&mut self)`：触发优雅关停并**同步等待**服务线程退出。
- `is_alive(&self)`：探测服务线程是否还在运行。

#### 4.4.2 核心流程

```text
GrpcServerHandle::shutdown(&mut self)
  ├── 1. self.shutdown.notify_one()      ← 点亮信号灯（见 4.3）
  │        此时服务线程里的 notified().await 被唤醒 → tonic 优雅关停 → run_grpc_server 返回
  └── 2. if let Some(handle) = self.join_handle.take() {
              handle.join()               ← 阻塞当前（Python）线程，等待服务 OS 线程结束
         }

GrpcServerHandle::is_alive(&self)
  └── self.join_handle.as_ref()
         .is_some_and(|h| !h.is_finished())   ← 有句柄且线程未结束 → true
```

这里有一个**顺序约束**：必须**先 `notify_one()` 再 `join()`**。如果反过来先 `join()`，Python 线程会死等一个永远不会自己结束的服务线程（因为还没人通知它停），造成死锁。`notify_one()` 是「请开始停」，`join()` 是「等它停完」。

#### 4.4.3 源码精读

句柄结构体：

[lib.rs:21-25](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L21-L25) —— `#[pyclass] struct GrpcServerHandle { shutdown: Arc<Notify>, join_handle: Option<std::thread::JoinHandle<()>> }`。

`shutdown` 方法（本模块的 Python 侧入口）：

[lib.rs:29-35](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L29-L35) —— 先 `self.shutdown.notify_one()`，再 `take()` 出 join handle 调 `.join()`。`let _ = handle.join();` 用 `let _ =` 显式丢弃 join 返回值（线程闭包返回 `()`），也吞掉了可能的 panic。

`is_alive` 方法：

[lib.rs:37-40](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L37-L40) —— 用 `JoinHandle::is_finished()` 判断线程是否已结束。注意 `is_finished` 是「保守且非阻塞」的：它只在运行时确知线程已结束时才返回 true，可能略有延迟，但适合做存活探测。

#### 4.4.4 代码实践

**实践目标**：理解 `shutdown()` 的两段式语义，以及 `Option<JoinHandle>` 的「一次性」。

1. 阅读 [lib.rs:30-35](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L30-L35)。
2. 思考：为什么 `join_handle` 要用 `Option` 包裹、并用 `take()` 取出？如果连续调用两次 `handle.shutdown()` 会怎样？
3. **需要观察的现象**：
   - 第一次调用：`notify_one()` 发信号 → `take()` 取出 `Some(handle)` → `join()` 阻塞到服务线程退出 → 返回。
   - 第二次调用：`notify_one()` 再发一次信号（无副作用，没有等待者了）→ `take()` 取出 `None` → `if let Some` 不进入 → 立即返回。
4. **预期结果**：`shutdown()` 是幂等安全的——多次调用不会 panic、不会重复 join。这正是 `Option + take` 带来的保证。
5. 延伸思考：调用 `shutdown()` 之后立刻调 `is_alive()`，在 `join()` 已经返回之后应当返回 `false`（因为 `join_handle` 已是 `None`，`as_ref()` 为 `None`，`is_some_and` 短路返回 false）。

> 待本地验证：上述「二次调用幂等」与「shutdown 后 is_alive 为 false」的结论，可在能编译本扩展的环境里写一个最小 Python 脚本（`start_server` 后连续两次 `handle.shutdown()`）验证。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `shutdown()` 里的 `self.shutdown.notify_one()` 和 `handle.join()` 交换顺序，调用 `shutdown()` 会怎样？

**参考答案**：会死锁。`join()` 会阻塞 Python 线程等待服务线程结束，但服务线程还在 `serve_with_incoming_shutdown` 里正常运行，根本没人发关停信号——它永远不会自己结束，于是 Python 线程永远 join 不回来。正确顺序必须是「先 notify（请求停止）→ 再 join（等待停止完成）」。

**练习 2**：`is_alive` 用的是 `JoinHandle::is_finished()`，为什么不用一个自己维护的 `AtomicBool` 来更精确地表示「服务是否已开机成功」？

**参考答案**：`is_finished()` 只能告诉你「这条 OS 线程是否还在跑」，回答的是「服务进程层是否存活」，而非「服务是否已就绪」。Python 侧判断「服务是否真正可接受请求」通常应通过 `health_check` RPC（参见 u2-l4）来探测，而不是靠线程存活状态。因此 `is_alive` 故意保持轻量、非阻塞，职责单一；把「就绪」语义留给 health check，避免用一个布尔位给出误导性的「已就绪」承诺。

---

## 5. 综合实践

把本讲的三条主线（消息上限解析、服务引导、优雅关停信号链）串成一个端到端的追踪任务。

### 任务一：`SGLANG_TONIC_PAYLOAD` 三态行为表

阅读 [server.rs:37-58](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L37-L58) 的 `resolve_max_message_size`，填写下表（在每种输入下列出：走了哪个 `match` 分支、返回值、是否打日志、日志级别）：

| `SGLANG_TONIC_PAYLOAD` | 分支 | 返回值 | 日志 |
| --- | --- | --- | --- |
| 非数字字符串（如 `"abc"`） | ? | ? | ? |
| `"0"` | ? | ? | ? |
| 合法正数（如 `"134217728"`） | ? | ? | ? |
| 未设置 | ? | ? | ? |

填完后对照 [server/tests.rs:44-75](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server/tests.rs#L44-L75) 自查。

**参考答案**：

| 输入 | 分支 | 返回值 | 日志 |
| --- | --- | --- | --- |
| `"abc"` | 内层 `match` 的 `_` 通配（`parse` 返回 `Err`） | `67_108_864`（默认 64 MiB） | `warn!`（带 `value="abc"`） |
| `"0"` | 内层 `match` 的 `_` 通配（`Ok(0)` 不满足 `n > 0`） | `67_108_864` | `warn!`（带 `value="0"`） |
| `"134217728"` | 内层 `match` 的 `Ok(n) if n > 0` | `134_217_728`（原样采纳） | `info!`（带 `bytes=134217728`） |
| 未设置 | 外层 `match` 的 `Err(_) =>` | `67_108_864` | 无日志 |

### 任务二：从 `notify_one()` 到 tonic 停止的全链路追踪

从 [lib.rs:31](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L31) 的 `self.shutdown.notify_one()` 出发，按顺序列出信号经过的每一个代码点（给出文件名:行号），直到 tonic Server 停止、`run_grpc_server` 返回：

1. [lib.rs:31](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L31) `notify_one()` 发出通知。
2. 该 `Arc<Notify>` 与服务线程里那份是同一个，来源见 [lib.rs:233-234](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L233-L234) 的 `Arc::new` + `clone`。
3. [server.rs:1001](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L1001) `shutdown.notified().await` 被唤醒。
4. [server.rs:1002](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L1002) 打印 `"gRPC server shutting down"`，关停 future complete。
5. tonic Server 开始优雅关停，`serve_with_incoming_shutdown` 的 future 在 [server.rs:1004](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L1004) `.await?` resolve。
6. [server.rs:1006](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L1006) `run_grpc_server` 返回 `Ok(())`。
7. 服务线程闭包（[lib.rs:240-248](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L240-L248)）结束，OS 线程退出。
8. [lib.rs:33](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L33) `handle.join()` 返回，`shutdown()` 调用结束。

### 任务三（源码阅读型）：消息上限如何生效到收发两端

确认 `resolve_max_message_size()` 的返回值被同时应用到了**解码（接收请求）**和**编码（发出响应）**两端。定位 [server.rs:991-994](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L991-L994)，说明：为什么需要同时设 `max_decoding_message_size` 和 `max_encoding_message_size`，只设其中一个会怎样？

**参考答案**：解码上限管「客户端发来的请求体大小」（如超大 prompt、多模态附件），编码上限管「服务端发回的响应体大小」（如一次性返回的长文本、大 embedding）。两者是不同方向、不同风险来源，tonic 对它们分别计费。只设一个会导致另一个方向仍用 4 MiB 默认值，依然可能在那个方向触发 `RESOURCE_EXHAUSTED` 类错误。

## 6. 本讲小结

- `run_grpc_server` 是 tonic 服务的开机入口，负责把标准库 `TcpListener` 经 `from_std` 移交 Tokio、组装 `SglangServiceImpl`、设置消息上限并用 `serve_with_incoming_shutdown` 开机。
- 默认消息上限 `DEFAULT_GRPC_MAX_MESSAGE_SIZE = 64 MiB`，远高于 tonic 的 4 MiB 默认，为多模态输入和 OpenAI JSON 透传体留足余量。
- `resolve_max_message_size` 对 `SGLANG_TONIC_PAYLOAD` 做三态处理：合法正数原样采纳（`info!`）、0 或非数字回退默认（`warn!`）、未设置直接默认。
- 优雅关停靠一个 `Arc<Notify>` 跨线程打通：服务线程 `await notified()`，Python 句柄 `notify_one()`；时序上「先 await 后 notify」保证通知不丢失。
- `GrpcServerHandle::shutdown` 是两段式：**先 `notify_one()` 再 `join()`**，顺序不可颠倒；`Option<JoinHandle> + take` 让 `shutdown()` 幂等、可安全多次调用。
- `is_alive` 基于 `JoinHandle::is_finished()`，职责单一（只回答线程是否存活），「服务就绪」应交给 `health_check` RPC 探测。

## 7. 下一步学习建议

- **u3-l5（Tokio 运行时与 GIL 协作模型）**：本讲的 `Arc<Notify>`、spawn 出来的 `sglang-grpc` 线程、`sglang-grpc-tokio` worker 池都属于「线程与运行时」话题；u3-l5 会把这条线完整讲清，建议紧接着读。
- **u3-l6（测试组织与扩展实践）**：本讲引用的 `resolve_max_message_size_honors_env_var` 是一个典型的「env 串行测试」，u3-l6 会系统讲解这类测试的 `SAFETY` 约定与扩展写法。
- **延伸阅读源码**：想确认「关停时进行中的 RPC 会被如何处置」，可结合 u3-l2（`RequestAbortGuard` 与 abort 传播）阅读 `serve_with_incoming_shutdown` 触发后、各流式 RPC 的 `Ok(None)` / `Err` 分支如何收尾。

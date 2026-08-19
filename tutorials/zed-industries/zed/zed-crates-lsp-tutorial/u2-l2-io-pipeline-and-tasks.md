# u2-l2 new_internal 与三路 IO 任务管线

## 1. 本讲目标

上一讲我们看到了 `LanguageServer::new` 如何 spawn 一个真实的语言服务器子进程，并把它的 stdin/stdout/stderr 三路管道连同 `Child` 句柄一起交给了 `new_internal`。本讲就钻进这个构造函数，学完后你应当能够：

1. 解释 `new_internal` 为什么把三路 IO 定义成 `AsyncWrite`/`AsyncRead` 泛型参数，而不是直接使用 `ChildStdin`/`ChildStdout` 等具体类型，以及这让 `FakeLanguageServer` 如何「零成本」复用整套运行时。
2. 逐字段说出 `LanguageServer` 结构体中通道、handler 表、任务句柄、barrier 各自的作用，并解释为什么大量字段是 `Arc<Mutex<...>>`。
3. 读懂 `handle_outgoing_messages` 如何做 Content-Length 写帧、flush，以及 `output_done` barrier 如何保证「Exit 通知先落盘、子进程后 kill」的顺序。
4. 读懂 `handle_stderr` 的逐行读取循环、`io_handlers` 分发与 `stderr_capture` 追加这三件事。
5. 手画出一条从「调用方发出消息」到「字节到达子进程」、再从「子进程输出字节」到「前台 handler 被调用」的完整数据流图——它是后面所有讲义的地图。

## 2. 前置知识

本讲是全手册「结构最密集」的一讲，先把几个基础设施讲清楚：

- **`AsyncRead` / `AsyncWrite`（futures 库）**：异步世界里的 `Read`/`Write` trait。进程的 stdout 实现 `AsyncRead`（我们从它读），stdin 实现 `AsyncWrite`（我们往它写）。把它们写成**泛型参数 + trait 约束**（静态分发），而不是 `Box<dyn AsyncRead>`（动态分发），既避免了每次 IO 的虚函数开销，也让编译器把具体类型单态化进来。
- **`async_channel` 通道**：Zed 大量使用的异步 MPSC 通道。`unbounded()` 创建无界通道；`Sender::close()` 会关闭整个通道——注意它的语义是「禁止再发送，但已经缓冲的消息仍会被接收端一条条取走，取空之后 `recv()` 才返回 `Err`」。这一点是理解 shutdown 时序的关键（本讲 4.4 节有实证）。
- **`Arc<Mutex<...>>` 共享状态**：`Arc` 让多个异步任务持有同一份数据的引用计数句柄，`parking_lot::Mutex` 保证任一时刻只有一个任务在改。本讲中三类 handler 表都是这种形态，因为「注册 handler 的代码」和「查表调用 handler 的后台任务」运行在不同线程。
- **`postage::barrier`**：一个「发送端全部 drop 时，接收端 `recv()` 才完成」的零载荷同步原语，类似不携带值的 oneshot。专门用来表达「某件事彻底结束了」。
- **GPUI 的两种 spawn**：`cx.spawn(...)` 在**前台线程**运行异步闭包（闭包拿到 `&mut AsyncApp`，可以安全地更新 GPUI entity）；`cx.background_spawn(...)` 把任务丢到**后台线程池**。本讲会看到一个精心设计的分工：需要回调前台 handler 的任务用前者，纯 IO 的任务用后者。
- **`BufReader` / `BufWriter`**：带缓冲的读写包装器。读侧把零散的 `read` 攒成批量；写侧把多次 `write` 攒在内存里，等 `flush()` 一次性写出——LSP 一条消息一次 flush，正好构成一个「帧」。
- **`futures::join!` 与 `Option::or`**：`join!` 并发等待多个 future 全部完成；`log_err()` 会把 `Err` 记进日志并折叠成 `None`，于是 `stdout.or(stderr)` 表示「stdout 任务成功就取其结果，否则取 stderr 任务的」。

如果这些名词里有陌生的，不必现在深究实现，记住上面一句话的直觉即可，源码里见到时能对上号就行。

## 3. 本讲源码地图

本讲全部源码都在 lsp crate 的两个文件里：

| 文件 | 本讲涉及范围 | 作用 |
| --- | --- | --- |
| [crates/lsp/src/lsp.rs](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs) | L79-L143、L426-L776、L1110-L1167、L1614-L1629、L1750-L1756、L1845-L1927 | 库入口：handler 类型别名、`LanguageServer` 结构体、`new`/`new_internal`、三个 IO 处理协程、shutdown、`notify_internal`、`Drop`、`FakeLanguageServer` |
| [crates/lsp/src/input_handler.rs](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/input_handler.rs) | L20-L137 | stdout 分帧读取器 `LspStdoutHandler`（u1-l3 已精读，本讲只引用其接口） |

先给一张总览图，后面每个模块都是在放大它的某一段（记号：`→A→` 表示经过函数/类型 A，`==>` 表示字节/消息流动）：

```text
调用方（前台）                       后台任务                          子进程
──────────────                    ──────────                       ──────
request_internal ──────┐
                       ├────────==> outbound_tx(无界) ──==> handle_outgoing_messages ──==> stdin
notify_internal ──==> notification_tx(无界) ──==> 序列化任务 ──┘         (写帧+flush)      │
unhandled_notification_wrapper ────────┘                                                    │
                                                                                        语言服务器
                                                                                            │
stdout ──> LspStdoutHandler(handler 循环) ──> incoming_messages(容量128) ──> handle_incoming_messages
                              │                                                (前台 cx.spawn)
                              └─按 RequestId 摘 response_handlers ──────────> 唤醒等待中的 request future
stderr ──> handle_stderr ──> io_handlers(IoKind::StdErr) + stderr_capture
```

## 4. 核心概念与源码讲解

### 4.1 为什么把进程 IO 抽象成泛型：new_internal 的签名

#### 4.1.1 概念说明

`LanguageServer::new` 只负责「spawn 出一个真的进程」，spawn 完成后它做的最后一件事是把三路管道交给 `new_internal`。`new_internal` 才是真正的构造器：所有通道、handler 表、后台任务都在这里诞生。

它的三路 IO 参数不是 `ChildStdin`、`ChildStdout`、`ChildStderr` 这些具体类型，而是三个泛型参数 `Stdin`、`Stdout`、`Stderr`，约束只有：

- `Stdin: AsyncWrite + Unpin + Send + 'static`
- `Stdout: AsyncRead + Unpin + Send + 'static`
- `Stderr: AsyncRead + Unpin + Send + 'static`

这样做有一个非常实际的动机：**测试时不启动任何真实进程**。测试用 `async_pipe::pipe()`（一对内存管道）伪造出同样满足这些约束的读写端，`new_internal` 对此毫不知情、也不关心。换句话说，「如何获得字节流」与「拿到字节流之后怎么跑协议」被彻底解耦了——这正是 u4-l3 要讲的 `FakeLanguageServer` 能用同一套代码同时模拟客户端与服务器的根基。

另外注意两个参数是 `Option`：`stderr: Option<Stderr>` 与 `server: Option<Child>`。进程场景下二者都有值；Fake 场景下 `stderr` 传 `None`（跳过 stderr 任务）、`server` 传 `None`（没有子进程可 kill）。

#### 4.1.2 核心流程

```text
new_internal(stdin, stdout, stderr, server, ...)
  ├── 约束：stdin 可异步写，stdout/stderr 可异步读，全部 Send + 'static
  ├── 建通道：outbound(无界, String)、notification(无界, NotificationSerializer)、barrier
  ├── 建 handler 表：notification_handlers / response_handlers / pending_respond_tasks / io_handlers
  ├── spawn 三个 IO 任务 + 一个通知序列化任务   ← 4.3 节展开
  └── 组装 LanguageServer 结构体               ← 4.2 节展开
```

#### 4.1.3 源码精读

[crates/lsp/src/lsp.rs:L497-L517](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L497-L517) 是 `new_internal` 的完整签名：三个 IO 泛型参数各自的 trait 约束、`stderr` 与 `server` 两个 `Option` 参数，以及一个 `on_unhandled_notification` 回调（未处理消息的兜底钩子，fake 用它把消息转发给测试断言）。

而 Fake 对这套泛型的「欺骗」手法在 [crates/lsp/src/lsp.rs:L1852-L1904](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1852-L1904)：`FakeLanguageServer::new` 创建两条 `async_pipe::pipe()`，然后**交叉**连接调用两次 `new_internal`——

```rust
// 示例代码（节选自上链真实源码，注释为本讲添加）
let (stdin_writer, stdin_reader) = async_pipe::pipe();     // 管道 A
let (stdout_writer, stdout_reader) = async_pipe::pipe();   // 管道 B

// 真实客户端：写 A 的写端，读 B 的读端
let mut server = LanguageServer::new_internal(..., stdin_writer, stdout_reader, None, None, ...);
// 假服务器：读 A 的读端（拿到客户端写的东西），写 B 的写端（模拟服务器输出）
let mut server = LanguageServer::new_internal(..., stdout_writer, stdin_reader, None, None, ...);
```

两次调用返回的两个 `LanguageServer`，一个充当「Zed 侧客户端」，一个充当「语言服务器侧」——因为 LSP 双方本来就说同一种协议，同一个运行时可以两头通用。这是本 crate 最漂亮的设计决策之一。

#### 4.1.4 代码实践

1. **实践目标**：亲手确认「管道交叉」的方向感，避免以后读 Fake 测试时方向搞反。
2. **操作步骤**：
   - 打开 [crates/lsp/src/lsp.rs:L1845-L1927](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1845-L1927)。
   - 在纸上写下四个管道端点：`stdin_writer`、`stdin_reader`、`stdout_writer`、`stdout_reader`。
   - 分别标出第一次 `new_internal`（客户端侧，L1860-L1874）与第二次（fake 侧，L1879-L1901）各自拿到哪两个端点。
3. **需要观察的现象**：客户端侧拿 `stdin_writer` + `stdout_reader`；fake 侧拿 `stdin_reader` + `stdout_writer`——同一个管道的写端在一方手里、读端在另一方手里，恰好构成全双工回路。
4. **预期结果**：你能不假思索地回答「客户端写给 stdin 的字节，最终从 fake 的哪个端点被读出来」（答案：`stdin_reader`，即 fake 那个 `LanguageServer` 的 stdout 分帧读取入口）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `new_internal` 的 `Stdin` 约束从 `AsyncWrite` 改成具体类型 `ChildStdin`，会失去什么？

**答案**：`FakeLanguageServer` 将无法把 `async_pipe` 的写端当作「stdin」传进来（它不是 `ChildStdin`），要么为 `ChildStdin` 写一层包装适配器，要么放弃复用、为 fake 另写一套客户端。泛型约束把「进程管道」降格为「任何可异步写的字节流」，测试才可能不碰进程。

**练习 2**：`server: Option<Child>` 参数为什么对 fake 要传 `None`？

**答案**：`Child` 是 `util::command` spawn 出来的子进程句柄，仅用于 shutdown 末尾的 `child.kill()`（L1162）。Fake 没有子进程，传 `None` 后 `server.lock().take().map(|mut child| child.kill())` 对 `None` 不做任何事，关闭流程其余部分照常走。

**练习 3**：`'static` 约束在这里为什么是必要的？

**答案**：三个 IO 任务被 `cx.spawn`/`cx.background_spawn` 启动后生命周期脱离构造函数栈帧，任务可能运行到进程结束。Rust 要求被 move 进 `'static` 任务的所有值不借用栈上数据，因此 IO 对象必须拥有 `'static` 生命周期。

### 4.2 LanguageServer 结构体：字段与 Arc<Mutex<...>> 共享状态

#### 4.2.1 概念说明

`LanguageServer` 是一个纯数据结构：它不持有任何执行逻辑，只持有「状态 + 通往各任务的把手」。理解它的字段分组，比逐个背字段更重要：

- **出站通道（2 个）**：`outbound_tx`（已序列化好的 JSON 字符串）、`notification_tx`（还没序列化的通知）。
- **handler 表（4 个，全部 `Arc<Mutex<...>>`）**：入站通知表、响应回调表、待响应任务表、IO 观测表。
- **任务把手与同步原语**：`io_tasks`（两个任务句柄）、`output_done_rx`（barrier 接收端）。
- **子进程与身份信息**：`server`、`server_id`、`name`、`process_name`、`binary` 等。

为什么 handler 表都是 `Arc<Mutex<...>>`？因为存在**三方并发**：前台代码随时调用 `on_notification`/`on_request` 往表里**插入**；后台的 `handle_incoming_messages` 任务循环里**查表并调用**；shutdown 时还要把整张表**掏空**（`take()`）。`Arc` 让任务克隆一份句柄带走，`Mutex` 保证插入与查表互斥。

还有两个容易忽略的细节设计：`response_handlers` 的内层是 `Option<HashMap>`——`None` 表示「会话已终结，不再接受任何响应」；`io_tasks` 和 `output_done_rx` 外层的 `Mutex<Option<...>>` 则实现了「只能被 `take()` 一次」的语义，保证 shutdown 流程不会被并发执行两次。

#### 4.2.2 核心流程

不需要流程图，用一张字段清单表代替（按数据流方向分组）：

| 字段 | 类型（简化） | 数据流向 | 一句话作用 |
| --- | --- | --- | --- |
| `outbound_tx` | `Sender<String>` | 前台 → 写出任务 | 已序列化消息的出站总入口 |
| `notification_tx` | `Sender<NotificationSerializer>` | 前台 → 序列化任务 | 待序列化通知的入口（延迟序列化） |
| `next_id` | `AtomicI32` | 前台自增 | 出站请求 id 分配器 |
| `notification_handlers` | `Arc<Mutex<HashMap<&'static str, ...>>>` | 前台写 / stdout 任务读 | method → 入站通知处理函数 |
| `response_handlers` | `Arc<Mutex<Option<HashMap<RequestId, ...>>>>` | 前台写 / stdout 任务读 | 请求 id → 响应回调 |
| `pending_respond_tasks` | `Arc<Mutex<HashMap<RequestId, Task>>>` | 前台写 / stdout 任务删 | 供 `$/cancelRequest` 取消的响应任务表 |
| `io_handlers` | `Arc<Mutex<HashMap<i32, ...>>>` | 前台写 / 三路 IO 任务读 | 全量报文观测（LSP 日志面板） |
| `io_tasks` | `Mutex<Option<(Task, Task)>>` | shutdown 取走 | 持有输入/输出任务句柄 |
| `output_done_rx` | `Mutex<Option<barrier::Receiver>>` | shutdown 取走 | 「输出任务已收尾」信号 |
| `server` | `Arc<Mutex<Option<Child>>>` | shutdown 取走 kill | 子进程句柄 |
| `capabilities` | `RwLock<ServerCapabilities>` | initialize 写 / 前台读 | 服务器能力快照 |
| `workspace_folders` | `Option<Arc<Mutex<BTreeSet<Uri>>>>` | 双向 | 与 project 层共享的工作区集合 |
| `root_uri` | `Uri` | 只读 | initialize 时上报的根路径 |

注意入站方向为什么**没有**通道字段：`LspStdoutHandler` 内部自建了 `incoming_messages` 接收端，被 `handle_incoming_messages` 局部持有，不进入结构体（u1-l3 讲过它的容量 128 有界设计）。

#### 4.2.3 源码精读

[crates/lsp/src/lsp.rs:L114-L143](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L114-L143) 是结构体定义本体。可以看到 `io_tasks` 上有 `#[allow(clippy::type_complexity)]`——两个 `Task<Option<()>>` 组成的嵌套 Option 确实复杂，但每一个包裹都有职责：外层 `Mutex` 保护并发 `take`，内层 `Option` 表达「可空」（Fake 无进程、shutdown 后被取走）。

四张表的**值类型**定义在 [crates/lsp/src/lsp.rs:L79-L82](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L79-L82)，四个类型别名浓缩了各自的调用形态：

- `NotificationHandler = Box<dyn Send + FnMut(Option<RequestId>, Value, &mut AsyncApp)>`——`FnMut` 说明同一 handler 会被反复调用，且需要 `&mut AsyncApp`（这就是 4.3 节 stdout 任务必须跑在前台的原因）；
- `ResponseHandler = Box<dyn Send + FnOnce(Result<String, Error>) -> Task<()>>`——`FnOnce` 说明一次性：响应匹配上就被消费掉；
- `IoHandler = Box<dyn Send + FnMut(IoKind, &str)>`——拿到流类别与原始文本；
- `PendingRespondTasks` 即 `Arc<Mutex<HashMap<RequestId, Task<()>>>>`，删掉条目即取消任务。

与之配套的 [crates/lsp/src/lsp.rs:L84-L90](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L84-L90) 定义了 `IoKind` 的三个变体 `StdOut`/`StdIn`/`StdErr`，是 `io_handlers` 的第一个入参。

#### 4.2.4 代码实践

1. **实践目标**：把「字段 → 谁写、谁读」内化成条件反射。
2. **操作步骤**：逐个取出下表左列字段，在源码里各找一处「写」和一处「读」：
   - `response_handlers`：写处在 `request_internal`（注册回调），读处在 [input_handler.rs:L114-L119](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/input_handler.rs#L114-L119)（按 id `remove` 并调用）；
   - `notification_handlers`：写处在 `on_custom_notification`，读处在 [lsp.rs:L687-L695](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L687-L695)；
   - `io_handlers`：写处在 `on_io`，读处在 [lsp.rs:L762-L764](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L762-L764)（写出方向）与 [input_handler.rs:L103-L105](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/input_handler.rs#L103-L105)（stdout 方向）。
3. **需要观察的现象**：每个字段的两端分属「前台构造/调用处」与「后台任务循环」两份代码。
4. **预期结果**：你会确认所有跨线程字段都是 `Arc` 克隆进任务的，没有任何一处把 `&self` 直接传进后台任务——这是 Rust 异步代码「共享状态必须显式」的典型样本。（本练习为源码阅读型，无需运行，结论可直接从代码结构得出。）

#### 4.2.5 小练习与答案

**练习 1**：`response_handlers` 为什么是 `Option<HashMap>` 而 `notification_handlers` 不是？

**答案**：响应是「欠条」语义：会话终结（输出任务退出、shutdown）时必须把所有欠条作废，否则发出请求的一方会永远挂起。整表 `take()` 置 `None` 是最快的作废方式，[lsp.rs:L659-L665](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L659-L665) 与 [lsp.rs:L753-L758](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L753-L758) 各有一个 `defer` 块负责这件事。通知处理函数没有「等待结果」的一方，作废无意义，所以永远是普通表。

**练习 2**：`output_done_rx` 外面包 `Mutex<Option<...>>` 而不是直接存 `Receiver`，解决了什么问题？

**答案**：`barrier::Receiver` 的 `recv()` 需要 `&mut self`，而 `shutdown(&self)` 只有 `&self`；更关键的是 shutdown 必须幂等/一次性——`self.output_done_rx.lock().take().unwrap()`（[lsp.rs:L1119](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1119)）在第一次调用时取走值，第二次调用会在更早的 `io_tasks.lock().take()?` 处直接返回 `None`，并发重复 shutdown 被天然挡住。

**练习 3**：`capabilities` 为什么用 `RwLock` 而其他表用 `Mutex`？

**答案**：`capabilities` 是「一次写、频繁读」：initialize 握手时写入一次，之后前台随时通过 `capabilities()` 读取。`RwLock` 允许并发读不互斥；handler 表则是「边插边查删」的短临界区，`Mutex` 更轻更快。锁类型的选择如实反映了访问模式。

### 4.3 new_internal 组装：通道、handler 表与三个 IO 任务

#### 4.3.1 概念说明

如果说结构体是「骨架」，`new_internal` 的函数体就是「点火」：创建通道、创建共享表、启动任务、最后把所有把手装进结构体返回。这里最值得咀嚼的是**四个任务的分工与线程归属**：

1. `stdout_input_task`——用 `cx.spawn` 跑在**前台**。原因藏在 `NotificationHandler` 的签名里：handler 需要 `&mut AsyncApp`，只有前台任务拿得到。它内部委托给 `handle_incoming_messages`（主循环是 u3-l4 的主题）。
2. `stderr_input_task`——`cx.background_spawn` 跑在后台，纯 IO 无前台需求；`stderr` 为 `None` 时用 `Task::ready(None)` 占位。
3. `input_task`——把上面两个 `join!` 起来归并成一个句柄，同样存进结构体。
4. `output_task`——后台运行 `handle_outgoing_messages`，是唯一的写出任务（4.4 节精读）。

外加一个不起眼但关键的**通知序列化任务**：从 `notification_rx` 收「序列化闭包」，执行闭包得到 JSON 字符串，再转发进 `outbound_tx`。它让 `notify` 的调用方（通常是持锁的前台代码）不必现场付出序列化成本——这是 u3-l1 的伏笔，这里只需认识这条「二级传送带」。

还有一段容易漏看的 `unhandled_notification_wrapper`：它包装了外部传入的 `on_unhandled_notification` 回调，当消息无人处理且**带着请求 id**（说明是服务器发给客户端的请求）时，自动回一条 `Unrecognized method`（错误码 -32601）的 JSON-RPC 错误响应，防止服务器傻等。

#### 4.3.2 核心流程

```text
new_internal:
  1. outbound_tx/rx   = unbounded::<String>()            # 出站总通道
     output_done      = barrier::channel()               # 写任务结束信号
     notification/response/pending/io 四张空表
  2. stdout_input_task = cx.spawn( 前台 )
       └─ handle_incoming_messages(stdout, wrapper, 三张表, cx)
  3. stderr_input_task = background_spawn( handle_stderr ) 或 Task::ready(None)
  4. input_task  = background_spawn( join!(stdout_task, stderr_task).or )
  5. output_task = background_spawn( handle_outgoing_messages(stdin, outbound_rx, ...) )
  6. 序列化任务 = background_spawn( notification_rx → 执行闭包 → outbound_tx ).detach()
  7. 组装并返回 LanguageServer
```

出站方向的通道拓扑（消息单位标注在括号里）：

```text
request_internal ──────────────────────────────┐(String)
unhandled_notification_wrapper 的响应 ─────────┤
                                               ∨
notify_internal ──> notification_tx(闭包) ──> 序列化任务 ──> outbound_tx ──> output_task ──> stdin
```

两条入边最终汇入同一个 `outbound_tx`，再由唯一的写出任务按到达顺序写出——通道天然保证了「请求与通知的全局发送顺序」。

#### 4.3.3 源码精读

[crates/lsp/src/lsp.rs:L518-L525](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L518-L525)：一口气创建 `outbound` 无界通道、barrier 通道和四张共享表。注意 `response_handlers` 初始化时内层就是 `Some(HashMap)`，等待日后被 `take()` 作废。

[crates/lsp/src/lsp.rs:L527-L566](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L527-L566)：`stdout_input_task` 的创建。内层先构造 `unhandled_notification_wrapper`（克隆一份 `outbound_tx` 作为错误响应的回程通道，见 L528-L548），再克隆四张表的 `Arc`，最后 `cx.spawn` 一个前台任务调用 `handle_incoming_messages(...).log_err().await`。对照 [crates/lsp/src/lsp.rs:L654](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L654)：该函数最后一个参数正是 `cx: &mut AsyncApp`——前台归属在此有据可查。

[crates/lsp/src/lsp.rs:L567-L581](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L567-L581)：stderr 任务（后台、无 AsyncApp 参数）与 `input_task` 的归并——`futures::join!` 等两路输入全部结束后，`stdout.or(stderr)` 折叠为一个 `Option<()>`（`log_err` 已把错误记日志并折叠为 `None`）。

[crates/lsp/src/lsp.rs:L582-L591](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L582-L591)：`output_task` 把 `stdin`、`outbound_rx`、`output_done_tx` 和两张表（`response_handlers`、`io_handlers`）一并 move 进 `handle_outgoing_messages`。

[crates/lsp/src/lsp.rs:L598-L612](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L598-L612)：序列化任务。`while let Ok(serializer) = notification_rx.recv().await` 期间执行 `(serializer.0)()` 得到字符串转发；循环因通道关闭退出后调用 `outbound_tx.close()`——**两级通道的关闭由此级联**：关 `notification_tx` → 序列化任务退出 → 关 `outbound_tx` → 写出任务排空后退出。注意它 `.detach()` 了，不存句柄，生命周期完全由通道关闭决定。

[crates/lsp/src/lsp.rs:L613-L639](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L613-L639)：最终组装。`process_name` 从 `binary.path` 的文件名推导（`rust-analyzer` 之类），`configuration` 初始化为 `settings: Value::Null` 的默认配置，`io_tasks` 装入 `(input_task, output_task)`，`output_done_rx`、`server` 各自入位。

#### 4.3.4 代码实践

1. **实践目标**：验证「stdout 任务在前台、其余在后台」这一分工，并亲眼看到两级通道的级联关闭。
2. **操作步骤**：
   - 在本地分支给三处加 `log::info!`（示例代码，仅用于观察，观察完删除）：`new_internal` 中 `cx.spawn` 闭包开头、`handle_outgoing_messages` 循环退出后、序列化任务 `outbound_tx.close()` 之前；
   - 运行 `cargo test -p lsp test_fake -- --nocapture`（本 crate 测试通过 `zlog::init_test` 初始化日志，见 [crates/lsp/src/lsp.rs:L2100-L2103](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L2100-L2103)）。
3. **需要观察的现象**：drop(server) 之后，三条日志的先后顺序应为「序列化任务收尾 → 写出任务退出 →（barrier 唤醒）shutdown 收尾」。
4. **预期结果**：日志顺序印证 4.3.2 的级联描述。具体输出格式取决于 zlog 配置，**待本地验证**；若 `--nocapture` 下看不到 info 级日志，可临时用 `eprintln!` 替代观察。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `handle_incoming_messages` 必须跑在前台（`cx.spawn`），而 `handle_outgoing_messages` 可以跑在后台？

**答案**：入站分发要调用 `NotificationHandler`，其签名第三个参数是 `&mut AsyncApp`（L79），只有 GPUI 前台执行器能提供——handler 里通常要 `entity.update(cx, ...)` 更新 UI 状态。写出方向只消费 `String` 与 `IoHandler`（都不需要 `AsyncApp`），与 UI 无关，放后台避免占用前台线程。

**练习 2**：`unhandled_notification_wrapper` 里为什么要检查 `msg.id` 是否存在？

**答案**：JSON-RPC 里带 id 的是请求（对方在等响应），不带 id 的是通知（无需响应）。只有请求才必须回 `MethodNotFound`（-32601）错误响应，否则服务器会一直挂等；对通知回响应反而是协议错误。判断依据正是 u1-l2 讲过的「id 的有无」。

**练习 3**：序列化任务退出后为什么要 `outbound_tx.close()`？直接让任务结束不行吗？

**答案**：`outbound_tx` 还有别的持有者（结构体字段、wrapper 克隆、shutdown 克隆），仅序列化任务结束不会让通道关闭，`handle_outgoing_messages` 的 `recv()` 会一直阻塞。显式 `close()` 才能触发「排空缓冲 → recv 返回 Err → 写任务退出 → barrier 唤醒」的连锁，shutdown 流程才能推进到 kill 子进程。

### 4.4 handle_outgoing_messages：写帧、flush 与 barrier 收尾

#### 4.4.1 概念说明

这是整个 crate 唯一向子进程写字节的地方。它做四件事：观测、写帧、flush、收尾。

**写帧**是 u1-l3 读帧的镜像：LSP over stdio 用 HTTP 风格分帧，一条消息 = `Content-Length: N\r\n\r\n` + N 字节 JSON。读侧按头部长度切流，写侧就按正文长度拼头。这里有个务实的小优化：长度数字用 `write!` 写进复用的 `content_len_buffer`，而不是每次 `format!` 分配新 `String`。

**flush** 与帧对齐：`BufWriter` 把四次 `write_all`（头名、长度、定界符、正文）攒在缓冲里，一次 `flush` 落进管道。一条消息一次 flush，服务器永远收到完整的一帧。

**收尾**分两层：进入函数先注册一个 `defer`，任务无论怎么退出都把 `response_handlers` 掏空（欠条作废）；循环正常结束后 `drop(output_done_tx)` 点亮 barrier，告诉 shutdown「所有出站字节（包括 Exit 通知）都已经 flush 完了，你可以 kill 了」。这个顺序是 LSP 规范要求「exit 通知之后进程才应退出」在客户端侧的体现——如果先 kill 后写，Exit 通知就永远送不到了。

#### 4.4.2 核心流程

```text
handle_outgoing_messages(stdin, outbound_rx, output_done_tx, response_handlers, io_handlers):
  defer: 退出时 response_handlers.lock().take()          # 作废所有欠条
  stdin = BufWriter::new(stdin)
  loop {
    message = outbound_rx.recv().await  # Err(通道关闭且已排空) 时退出
    io_handlers 逐个回调 (IoKind::StdIn, &message)        # 日志面板数据源
    写 "Content-Length: " + len(message) + "\r\n\r\n" + message
    stdin.flush()
  }
  drop(output_done_tx)                                     # 点亮 barrier
```

排空语义值得强调：`async_channel` 的 `close()` 并不丢弃已缓冲消息，接收端会把它们**全部取完**才看到 `Err`。所以 shutdown 里「发送 Exit 通知 → 关闭 notification_tx」之后，Exit 这条字符串仍会被本循环取出并写出——这不是推测，[crates/lsp/src/lsp.rs:L2184-L2188](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L2184-L2188) 的 `test_fake` 在 `drop(server)` 后断言 fake 收到了 `notification::Exit`，若 close 会丢缓冲，该断言必挂。

#### 4.4.3 源码精读

[crates/lsp/src/lsp.rs:L742-L776](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L742-L776) 是完整函数（不足 35 行）：

- L752-L758：`BufWriter` 包装 + `util::defer` 注册响应表作废——与入站任务 L659-L665 的 defer 遥相呼应，两个方向任一死亡都会终结所有未完成请求；
- L760-L764：`recv` 到消息后先 `log::trace!` 再分发给全部 `io_handlers`，此时消息还是**分帧前的纯 JSON 文本**，所以 LSP 日志面板里看到的是可读 JSON 而非带头的原始字节；
- L766-L772：写帧四连 + flush。`CONTENT_LEN_HEADER` 常量（`"Content-Length: "`，[L45](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L45)）与读侧 [input_handler.rs:L92-L93](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/input_handler.rs#L92-L93) 用的同一个，读写天然对称；
- L774：显式 `drop(output_done_tx)`——虽然函数返回也会 drop，但显式写出表达了「这一行就是给 barrier 的信号」的意图。

barrier 的消费方在 [crates/lsp/src/lsp.rs:L1158-L1163](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1158-L1163)：shutdown 发出 Exit、关闭序列化通道之后 `output_done.recv().await`，醒来才 `server.lock().take().map(|mut child| child.kill())`。u2-l4 会展开整个 shutdown，这里只需记住 barrier 的位置：**它是「写干净」与「杀进程」之间的同步点**。

#### 4.4.4 代码实践

1. **实践目标**：亲眼看到写出方向的两帧字节长什么样，理解 `io_handlers` 拿到的是帧前文本。
2. **操作步骤**：
   - 写一个临时测试（可仿照 [input_handler.rs:L146-L188](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/input_handler.rs#L146-L188) 的 `test_backpressure_when_messages_are_not_consumed` 的管道手法）：用 `async_pipe::pipe()` 的写端充当「假 stdin」传给 `LanguageServer::new_internal` 构造一个 server（fake 风格，`stderr`/`server` 传 `None`）；
   - 在 `on_io` 注册的回调里把 `IoKind::StdIn` 的文本 push 进 `Vec<String>`；
   - `server.notify::<notification::Initialized>(InitializedParams {})` 之后从管道读端 `read_exact` 按 `Content-Length` 手工解一帧。
3. **需要观察的现象**：`on_io` 收到的是 `{"jsonrpc":"2.0","method":"initialized","params":{}}` 这样的纯 JSON；管道里的字节则是 `Content-Length: N\r\n\r\n` 前缀 + 该 JSON。
4. **预期结果**：两种形态一一对应，前缀中的 N 恰等于 JSON 字节数。运行方式：`cargo test -p lsp <你的测试名> -- --nocapture`。具体 JSON 字段顺序可能因 serde 版本而异，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么写帧用的长度是 `message.len()`（字节长度）而不是字符数？如果 JSON 里含中文注释类转义，两者还相等吗？

**答案**：LSP 分帧按**字节**计数。`String::len()` 在 Rust 里就是字节数（UTF-8 编码后），与 `Content-Length` 语义一致。字符数（`chars().count()`）会小于字节数——含非 ASCII 字符时用字符数当长度，读侧会少读字节、错位解析下一条消息。`message.len()` 恰好是对的。

**练习 2**：如果去掉 `stdin.flush()`，会发生什么？

**答案**：`BufWriter` 会把消息留在内存缓冲里不发给服务器；后续消息继续累积。轻则延迟增大，重则服务器长时间收不到 initialize/exit 等关键消息，整个握手或关闭流程停摆。每消息一次 flush 保证帧的完整送达。

**练习 3**：函数开头的 `defer` 作废 `response_handlers` 后，正在 `await` 响应的请求 future 会怎样？

**答案**：响应回调表被掏空，之后任何按 id 查表都查不到；而等待中的 future 实际挂在 `request_internal` 的 oneshot 通道上，通道发送端（注册进表的 handler）被整表丢弃时 drop，接收端收到 `Canceled`，future 以错误收场——这正是「server shut down」快速失败路径的一部分（细节在 u3-l2 展开）。

### 4.5 handle_stderr：逐行读取与两条观测通道

#### 4.5.1 概念说明

stderr 不是协议通道——语言服务器把日志、崩溃信息、调试输出写到这里，格式随心所欲。所以 `handle_stderr` 完全不做 JSON 解析，只按**行**切分（`read_until(b'\n')`），把每一行送往两个目的地：

1. `io_handlers`（以 `IoKind::StdErr`）——与 stdin/stdout 报文汇入同一张观测表，LSP 日志面板因此能显示三路流量的完整时间线；
2. `stderr_capture`——上一讲 (u2-l1) 说过的「阀门」：它是个 `Arc<Mutex<Option<String>>>`，调用方在启动前设为 `Some(String::new())` 就开启捕获；本循环每行 `push_str` 追加；一旦 initialize 失败，project 层把它读出来拼进用户可见的错误信息，帮助诊断「服务器起来了但握手就崩了」的场景。

读到 EOF（`bytes_read == 0`）意味着服务器进程把 stderr 关了（通常就是退出了），循环以 `Ok(())` 结束——stderr 读尽不算错误。

#### 4.5.2 核心流程

```text
handle_stderr(stderr, io_handlers, stderr_capture):
  stderr = BufReader::new(stderr)
  loop {
    n = stderr.read_until(b'\n', &mut buffer)
    n == 0 → return Ok(())                       # EOF：服务器关闭了 stderr
    文本合法 UTF-8 时：
      io_handlers 逐个回调 (IoKind::StdErr, line) # 观测通道 1
      stderr_capture 若为 Some → push_str(line)   # 观测通道 2（诊断阀门）
    yield_now()                                   # 防止刷屏式输出饿死同线程任务
  }
```

注意与 stdout 路径的关键差异：stderr **没有**有界通道、没有 handler 表分发、没有响应匹配——它是纯旁路观测流，永不参与 RPC 语义。

#### 4.5.3 源码精读

[crates/lsp/src/lsp.rs:L707-L740](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L707-L740) 全函数：

- L721-L724：`read_until(b'\n', ...)` 读一行，返回 0 字节即 EOF，`return Ok(())`；
- L726-L730：仅当行内容是合法 UTF-8 才分发（`if let Ok(message)`），二进制垃圾直接静默跳过，不让一条坏行杀死整个读取循环；
- L732-L734：`stderr_capture.lock().as_mut()` 拿到 `Some` 才追加——阀门开着的证据就是这个 `Option`；
- L738：与 `handle_incoming_messages` L702 相同的 `yield_now()`，注释直言「Don't starve the main thread when receiving lots of messages at once」——某些服务器启动时会瞬间倾泻数百行 stderr，不让步的话同执行器上的其他任务会被饿住。

任务侧的接线在 [crates/lsp/src/lsp.rs:L567-L577](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L567-L577)：`stderr` 为 `Some` 才 `background_spawn`，否则 `Task::ready(None)` 占位保持类型一致——这样 `input_task` 的 `join!` 永远等两路，无需分支。

#### 4.5.4 代码实践

1. **实践目标**：观察「stderr 一行 → io_handler 一次回调 → capture 一次追加」的完整链路。
2. **操作步骤**：
   - 仿照 `test_fake`（[lsp.rs:L2105-L2189](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L2105-L2189)）构造 server 与 fake，但这次给「fake 那一侧」的 `new_internal` 传入真实的 stderr 管道：`let (err_writer, err_reader) = async_pipe::pipe();`，把 `err_writer` 当作 fake 侧的 stdin 之外的……
   - 更简单的路径：直接对**客户端侧** server 调 `on_io` 收集 `IoKind::StdErr`，然后从 fake 侧无法直接产 stderr（fake 传了 `None`）——因此本实践改用真实进程：用 `LanguageServerBinary { path: "sh".into(), arguments: vec!["-c".into(), "echo boom >&2; sleep 3600".into()], env: None }` 调 `LanguageServer::new` 启动（承接 u2-l1 的实践），`on_io` 里过滤 `IoKind::StdErr` 收集文本，同时把 `stderr_capture` 初始化为 `Some(String::new())`。
3. **需要观察的现象**：`on_io` 回调收到 `"boom\n"` 一行；`stderr_capture` 锁内字符串同样含 `boom`。
4. **预期结果**：两条通道内容一致。注意不要 `initialize`（该假服务器不会说 LSP），观察完直接 drop 触发关闭。本实践依赖本机有 `sh`，行为**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：为什么 stderr 不像 stdout 那样套一层 `LspStdoutHandler` 式的分帧与有界队列？

**答案**：stderr 没有 Content-Length 分帧（那只是 stdout 上 JSON-RPC 的约定），内容是自由文本日志；它也不进任何分发/背压链路——前台消费慢不会拖垮它，因为它的终点（观测回调 + 字符串追加）都是同步轻操作。给它上队列只会增加复杂度而没有收益。

**练习 2**：`stderr_capture` 为什么由调用方决定开或关，而不是无条件捕获？

**答案**：捕获意味着每行 stderr 都追加进一个常驻 `String`，长跑服务器（数小时会话）会无限增长内存。所以设计成阀门：启动阶段开启用于诊断 initialize 失败（u2-l1 的 `stderr_capture` 参数），握手成功后即可置 `None` 停止追加。`Option<String>` 让「停止」只是 `lock()` 后判 `None`，零额外结构。

**练习 3**：如果服务器在握手阶段就崩溃退出，stderr 任务、stdout 任务、屏障各自会发生什么？

**答案**：进程退出后三路管道相继 EOF：stderr 循环 `read_until` 返回 0 → `Ok(())` 结束；stdout 侧 `read_headers` 读到 EOF → `bail!("cannot read LSP message headers")`（u1-l3），任务以错误结束并被 `log_err` 记录；前台等待 initialize 响应的请求 future 因 `response_handlers` 被defer 作废/通道关闭而报错。崩溃证据则完整躺在 `stderr_capture` 里等调用方读取。

## 5. 综合实践

**任务：手绘「三路 IO 任务管线」数据流图，并标注同步点。**

不看本讲正文，只对着 [crates/lsp/src/lsp.rs:L497-L640](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L497-L640) 与 [crates/lsp/src/input_handler.rs](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/input_handler.rs)，画一张覆盖以下要求的图：

1. **三条出站入边**汇入 `outbound_tx`：`request_internal`（直发 String）、`notify_internal`（经 `notification_tx` → 序列化任务）、`unhandled_notification_wrapper`（错误响应）；
2. **写出路径**：`outbound_rx` → `handle_outgoing_messages`（写帧 + flush）→ 子进程 stdin，并注明 `io_handlers(IoKind::StdIn)` 在写帧**前**被调用；
3. **读入路径**：子进程 stdout → `LspStdoutHandler::handler`（分帧）→ 两分支（`NotificationOrRequest` 进容量 128 的 `incoming_messages`；`AnyResponse` 按 `RequestId` 摘 `response_handlers`）→ `handle_incoming_messages` 查 `notification_handlers` 分发；
4. **stderr 旁路**：`handle_stderr` → `io_handlers(IoKind::StdErr)` + `stderr_capture`；
5. **两个同步点**用醒目记号标出：`output_done` barrier（写任务 `drop(output_done_tx)` → shutdown `recv()`），以及两个 `defer` 作废 `response_handlers` 的位置（入站任务与写出任务各一）；
6. 每条边标注对应的**字段名或函数名**，每个任务标注它跑在**前台还是后台**。

完成后与第 3 节的总览图互相校对，重点自查三个最容易画错的点：`io_handlers` 在 stdin/stdout/stderr 三处都被调用（不是只有 stdout）；`output_done` barrier 只挂在**写出**任务上；`notification_tx` 与 `outbound_tx` 是**串联**两级而非并联两条写出路径。这张图建议保留——u3 系列讲请求/通知机制、u2-l4 讲 shutdown 时序时，它就是你的导航地图。

（本实践为源码阅读型，无需运行任何命令即可完成；若想验证图中某条边，可参照 4.3.4 的日志埋点方法**待本地验证**。）

## 6. 本讲小结

- `new_internal` 用 `AsyncWrite`/`AsyncRead` 泛型约束接受三路 IO，把「字节从哪来」与「协议怎么跑」解耦；`FakeLanguageServer` 用两条 `async_pipe` 交叉连接、调用两次 `new_internal`，同一套运行时同时充当客户端与假服务器。
- `LanguageServer` 结构体 = 出站通道（`outbound_tx` 直发、`notification_tx` 延迟序列化）+ 四张 `Arc<Mutex<...>>` 共享 handler 表 + 任务把手与 barrier；`Mutex<Option<...>>` 的 `take()` 模式保证了 shutdown 的一次性。
- 四个任务各就其位：stdout 分发跑**前台**（handler 需要 `&mut AsyncApp`），stderr 读取、消息写出、通知序列化跑**后台**；`input_task` 用 `join!` 归并两路输入。
- `handle_outgoing_messages` 是唯一写出点：`io_handlers` 观测帧前 JSON → 拼 `Content-Length` 头 → `BufWriter` 四连写 + flush；退出时 `defer` 作废响应表、`drop(output_done_tx)` 点亮 barrier。
- `handle_stderr` 是纯旁路：按行读取，UTF-8 合法则分发 `io_handlers(IoKind::StdErr)` 并向 `stderr_capture` 追加（诊断阀门），EOF 优雅结束，`yield_now` 防饿死。
- 关闭级联：关 `notification_tx` → 序列化任务退出并 `outbound_tx.close()` → 写任务排空缓冲（Exit 通知必达）→ barrier 唤醒 shutdown → kill 子进程；`async_channel` 的 close 排空语义是这条链的粘合剂。

## 7. 下一步学习建议

本讲补齐了「构造与管线」，接下来两条线都通：

- **主线继续**：u2-l3《initialize 握手与客户端能力声明》——管线就绪后的第一次真实使用：`Initialize` 请求如何借 `request_internal` 走本讲的出站通道，`ServerCapabilities` 如何写进 `capabilities: RwLock`，以及 `server_info` 如何回填 `version`/`process_name` 字段。
- **平行深入**：u3-l1《发送通知与延迟序列化》专门拆解本讲埋下的 `NotificationSerializer`——为什么发的是「闭包」而不是字符串，两级通道的顺序保证如何成立。
- 想先看「死亡路径」的读者可以跳读 u2-l4《shutdown 关闭流程与 Drop 自动善后》，它把本讲的 barrier、`io_tasks.take()`、通道级联全部串成一条时间线。

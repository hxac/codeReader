# PyBridge 与请求通道架构

## 1. 本讲目标

在前几讲里，我们反复看到 `server.rs` 里一行不起眼的代码：

```rust
self.bridge.submit_request(&rid, "generate", req_dict)
```

它把一个 gRPC 请求「扔」给某个叫 `bridge` 的东西，然后拿到一个 `Receiver`，再用 `async_stream` 从这个 `Receiver` 里把响应 chunk 一条条吐回给客户端。`SglangServiceImpl` 持有的这个 `bridge: Arc<PyBridge>`，就是本讲的全部主角。

本讲要回答的核心问题是：**这个 `PyBridge` 到底是什么？它如何在不阻塞 Rust 异步线程的前提下，把请求交给 Python、再把 Python 流式吐出来的结果收回来？**

读完本讲，你应当能够：

- 说出 `PyBridge` 这个结构体持有哪些字段、每个字段从哪里来、起什么作用，并能解释它为什么同时持有「Python 句柄」和「Tokio 句柄」两样东西。
- 画出 `BridgeState` 里那 5 张表（`channels` / `pending_sends` / `ready_callbacks` / `ready_signals` / `terminal_errors`）的用途，并理解它们为什么必须被**同一把锁**护住。
- 读懂 `create_channel` 的去重逻辑：遇到重复的 `rid` 时返回什么错误、注册新通道时又顺手清掉了哪些旧账目。
- 读懂 `submit_request` 的三段式流程：建通道 → 跨 GIL 调 Python → 失败回滚，并解释「Python 调用失败时为什么要 `remove_channel`」。
- 理解 `lock_or_recover` 这个小工具如何让「某次请求 panic」不至于毒死整个服务。

本讲**只讲桥接层的「骨架」**：数据结构、共享状态、建通道、提请求。通道里的 chunk 是怎么被消费的（流式 RPC）属于 u2-l3；回调对象 `ChunkCallback` / `JsonChunkCallback` 的内部细节属于 u2-l6；通道满了怎么办的**背压停泊**机制属于 u3-l1。本讲结束时，你脑子里应有一张「`PyBridge` 是个共享账本，每个请求在账本里开一页通道」的图。

## 2. 前置知识

进入源码前，先把三个概念讲清楚，它们是看懂 `bridge.rs` 的钥匙。

### 2.1 为什么需要「桥」：Rust 异步世界与 Python GIL 世界

gRPC 服务端是 Rust 写的、跑在 Tokio 异步运行时上——成百上千个并发请求可以「同时」在少量的 worker 线程上交错推进。但 SGLang 真正的推理引擎（调度器、分词器管理器、模型）**全在 Python 那一侧**。这就产生了一个根本矛盾：

- Rust 这边是**异步、多线程、非阻塞**的；
- Python 那边有 **GIL**（全局解释器锁），且推理本身是**长耗时、阻塞**的。

如果让 Tokio worker 线程同步调用 `python.generate()`，这个线程就会被 Python 推理一直占住，无法服务其他请求——异步运行时就被「堵死」了。

`PyBridge` 的解法是 **「请求级 mpsc 通道 + 回调」** 的解耦模型：

1. Rust 为每个请求开一条 `tokio::sync::mpsc` 通道（有界缓冲）。
2. Rust 把请求交给 Python，**同时**交给 Python 一个 PyO3 回调对象。
3. Python 在自己的线程里跑推理，每产出一个 chunk，就**短暂持有 GIL** 调一下回调，回调把 chunk 塞进 mpsc 通道的 `Sender`。
4. Rust 的 tonic handler 在**另一条异步任务**里从 `Receiver` 取 chunk，流式发给客户端。

于是 Rust 异步线程和 Python 推理线程之间，唯一的耦合点就是那条**有界通道**——这正是后面背压（u3-l1）的着力点。

### 2.2 `mpsc` 通道、`Sender` 与 `Receiver`

`tokio::sync::mpsc::channel(cap)` 会创建一条**多生产者、单消费者**的异步通道，返回一对 `(Sender, Receiver)`：

- `Sender` 可以被 `clone`，多个地方都能往里塞东西（本讲里 Python 回调持有它）。
- `Receiver` 只有一个，负责按顺序取出（本讲里 tonic handler 持有它）。
- `cap` 是**缓冲容量上限**。通道已满时，`try_send` 立即返回错误而不阻塞——这是非阻塞背压的关键。

本讲里每个请求会开一条容量为 `response_channel_capacity`（默认 64）的通道。缓冲占用满足：

\[ 0 \le \text{占用} \le C, \quad C = \text{response\_channel\_capacity} \]

当占用涨到 \( C \) 时，下一次 `try_send` 返回 `Full`，背压机制启动（详见 u3-l1）。直觉上，若生产端写入速率 \( \lambda_p \) 大于消费端排空速率 \( \lambda_c \)，缓冲占用会持续增长直至触顶。

### 2.3 GIL、`Python::with_gil` 与 `PyObject`

- **GIL**：CPython 的全局解释器锁，任何线程要执行 Python 字节码都必须先拿到它。
- **`Python::with_gil(closure)`**：PyO3 提供的「获取 GIL 并在闭包里使用」的入口。在 Rust 异步线程里**不能**长时间持 GIL，否则会卡住所有 Python 线程；`with_gil` 通常只包裹一小段「调方法、取结果」的同步代码。
- **`PyObject`**：一个拥有所有权的、类型擦除的 Python 对象指针。本讲里 `PyBridge` 持有的 `runtime_handle: PyObject`，就是 Python 端那个 `RuntimeHandle` 对象（u1-l1 已介绍，它有 `submit_request` / `abort` 等方法）。

> 名词速查：`rid` = **request id**，每个请求的唯一身份证号（一般是 `uuid`，见 u2-l2）。本讲里 `rid` 是一切表的**主键**：通道、回调、待发记录、终端错误，全都按 `rid` 索引。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件展开：

| 文件 | 作用 |
| --- | --- |
| [`rust/sglang-grpc/src/bridge.rs`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L84-L92) | 桥接层全部家当：`ResponseChunk`/`TerminalError` 类型、`PyBridge` 结构体、共享账本 `BridgeState`、建通道 `create_channel`、提请求 `submit_request`、回调对象、背压与中止辅助函数、单元测试模块。 |

为了讲清「`PyBridge` 从哪来」和「通道被谁消费」，还要顺带看两处**入口与出口**：

| 文件 | 作用 |
| --- | --- |
| [`rust/sglang-grpc/src/lib.rs`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L226-L232) | `start_server` 里构造 `Arc<PyBridge>`、把 Tokio handle 注入桥接层的地方。 |
| [`rust/sglang-grpc/src/server.rs`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L233-L236) | tonic handler 调 `submit_request` 拿 `Receiver`、再从 `take_terminal_error` 取终端错误的地方。 |

> 定位提示：`bridge.rs` 全文约 805 行，结构很规整——顶部是类型定义（13–75）、`lock_or_recover`（77–82）、`PyBridge` 及其方法（84–447）、一批背压/中止辅助自由函数（449–607）、两个 `#[pyclass]` 回调（610–783）、最后是测试模块（803–805）。本讲主要在 37–207、315–335、437–469 这几个区段里活动。

## 4. 核心概念与源码讲解

### 4.1 PyBridge：桥接层的「外壳」

#### 4.1.1 概念说明

`PyBridge` 是整个桥接层的**唯一公开类型**（`server.rs` 只认得它）。它是一个普通结构体（不是 `#[pyclass]`，所以 Python 看不见它——它纯粹是 Rust 内部用的），职责是把「与 Python 通信所需的一切」收拢到一个对象里：

- **Python 侧句柄**：`runtime_handle`（那个带 `submit_request` 等方法的 `RuntimeHandle`）。
- **Tokio 侧句柄**：`tokio_handle`，给回调在通道满时 `spawn` 异步发送任务用（u3-l1）。
- **共享账本**：`state`，一把锁护住的所有 per-request 状态。
- **配置**：`response_channel_capacity`（通道容量）、`context_len`（模型上下文长度，给分词/校验用）。
- **可选的 Rust 原生分词器**：`rust_tokenizer`（u2-l8 详讲）。

你可以把 `PyBridge` 想象成「前台」：它记得 Python 老板在哪（`runtime_handle`）、记得异步调度中心在哪（`tokio_handle`）、还管着一本登记簿（`state`），每个进来的请求先在登记簿上开一页。

#### 4.1.2 核心流程

`PyBridge` 自身的生命周期很短：它在 `start_server` 里被**构造一次**，包进 `Arc` 后被 `clone` 给每一个 tonic handler。此后它的字段基本不再变（`state` 内部的内容会变，但 `state` 这个 `Arc` 指针本身不变）。流程是：

1. `start_server` 解析参数、归一化 `response_channel_capacity`。
2. 构造多线程 Tokio 运行时，`rt.handle().clone()` 拿到 handle。
3. `PyBridge::new(...)` 把 `runtime_handle`、分词器、`context_len`、容量、handle 组装成桥。
4. 包成 `Arc<PyBridge>`，`clone` 给 `SglangServiceImpl`（u2-l2）和服务线程。
5. 之后所有请求都通过这**同一个** `Arc<PyBridge>` 提交。

#### 4.1.3 源码精读

结构体定义只列出 6 个字段，没有别的——「外壳」名副其实：

每个请求级状态全在 `state` 里，结构体本身是无状态的「配置 + 句柄」容器：

[`rust/sglang-grpc/src/bridge.rs:84-92`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L84-L92) —— `PyBridge` 持有 Python 句柄、Tokio 句柄、共享状态、分词器与两项配置。

构造函数 `new` 除了搬运字段，还做了一处**契约校验**：

[`rust/sglang-grpc/src/bridge.rs:95-114`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L95-L114) —— `debug_assert!(response_channel_capacity > 0, ...)`，断言容量必须为正。

这条断言的意义是：**「容量归一化」是 `start_server` 的责任**（u1-l4 讲过：传 0 时回退默认 64）。到 `PyBridge::new` 这一层，容量必须已经是合法正数；如果是 0，说明上游出了 bug，开发构建里直接 panic 暴露问题，而不是让 `mpsc::channel(0)` 默默造出一个容量为 0 的奇怪通道。

真正组装 `PyBridge` 的地方在 `lib.rs`：

[`rust/sglang-grpc/src/lib.rs:226-232`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L226-L232) —— `start_server` 把 `runtime_handle`、`rust_tokenizer`、`tokenizer_info.context_len`、归一化后的 `response_channel_capacity`、`tokio_handle` 依次喂给 `PyBridge::new`。

注意第 231 行传入的是 `tokio_handle`，它是 [`rust/sglang-grpc/src/lib.rs:224`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L224) 里 `rt.handle().clone()` 得到的——这就是「PyBridge 为什么能拿到 Tokio 调度器」的源头。

#### 4.1.4 代码实践

1. **实践目标**：确认「同一个 `PyBridge` 被所有请求共享」这一事实。
2. **操作步骤**：在 `server.rs` 里搜索 `self.bridge`，数一下有多少处调用；再到 `lib.rs` 确认 `bridge` 只被 `Arc::new` 构造了一次（`Arc::new(PyBridge::new(...))`），随后通过 `bridge_clone` 传递。
3. **观察现象**：你会发现 `SglangServiceImpl` 的 `bridge: Arc<PyBridge>` 字段（[server.rs:22](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L22)）是 `Arc`，所有 handler 共享同一个底层 `PyBridge`。
4. **预期结果**：`PyBridge` 全局唯一，per-request 的差异完全由 `state` 内部的 `rid` 键来区分——这正是「外壳共享、账本分流」的设计。

> 待本地验证：若想亲眼看到「单例」，可在 `run_grpc_server` 构造 `SglangServiceImpl` 处打印一次 `Arc::strong_count(&bridge)`，运行后观察它随并发请求数是否增长（应增长，因为每个在途 handler 都 `clone` 了一次 `Arc`）。

#### 4.1.5 小练习与答案

**练习 1**：`PyBridge` 为什么不是 `#[pyclass]`？如果把它做成 `#[pyclass]` 暴露给 Python 会有什么问题？

**答案**：`PyBridge` 持有 `tokio::runtime::Handle`、`BridgeState`（含一堆 `PyObject`）等 Rust 内部结构，本就是 Rust 侧的「调度中枢」，Python 不需要也不应当直接操作它。Python 只需要操作回调对象（`ChunkCallback`）和 `RuntimeHandle`。把它暴露给 Python 会泄漏内部实现、扩大攻击面，且无收益。

**练习 2**：`PyBridge::new` 里为什么用 `debug_assert!` 而不是普通 `assert!` 或 `if ... return Err`？

**答案**：`debug_assert!` 只在 debug 构建生效，release 构建会被编译掉、零开销。容量归一化是 `start_server` 的既定职责（u1-l4 已保证传 0 回退默认），到这一层为 0 属于「不该发生的程序员错误」而非「运行时用户错误」，用 debug 断言既能在开发期暴露 bug，又不在生产期付出代价。

---

### 4.2 BridgeState：被一把锁护住的共享账本

#### 4.2.1 概念说明

`BridgeState` 是真正的「共享可变状态」。前面说过 `PyBridge` 是无状态外壳，那么所有 per-request 的动态信息——谁开了通道、谁还没发完、谁出错关停了——全记在 `BridgeState` 这本账上。它有 5 张表，全部以 `rid`（String）为主键：

| 字段 | 类型 | 作用 |
| --- | --- | --- |
| `channels` | `HashMap<String, Sender<ResponseChunk>>` | **核心表**：每个在途请求的通道发送端。有它，回调才能把 chunk 推给对应 handler。 |
| `pending_sends` | `HashSet<String>` | 背压标记：某 `rid` 当前有一个 chunk「停泊」在异步任务里等待通道腾位（u3-l1）。 |
| `ready_callbacks` | `HashMap<String, PyObject>` | Python 注册的「通道恢复」回调 `on_ready`：通道排空后通知 Python 继续生产（u3-l1）。 |
| `ready_signals` | `HashSet<String>` | 边沿缓冲：通道已排空、但 Python 还没注册 `on_ready`，先记一笔，等注册时补发信号（u3-l1）。 |
| `terminal_errors` | `HashMap<String, TerminalError>` | 终端错误记录：某 `rid` 因何原因被关停（`ChannelFull` / `ClientDisconnected` / `Aborted`），供 handler 取出转成 gRPC 状态码（u3-l3）。 |

其中 `channels` 是本讲的主角；后四张表主要服务于背压与中止（u3-l1/u3-l2），本讲只需知道它们「存在且按 `rid` 索引」即可。

> 账本里流动的数据类型是 `ResponseChunk`（[bridge.rs:13-24](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L13-L24)）：`Data`（中间 chunk）/ `Finished`（正常收尾）/ `Error`（异常收尾），后两者是「终端 chunk」。`TerminalError`（[bridge.rs:48-67](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L48-L67)）则是「为什么这条通道被强制关停」的三类原因。

#### 4.2.2 核心流程

关键设计：**这 5 张表被同一把 `std::sync::Mutex` 护住**，整体再包进 `Arc`，得到类型别名：

```text
type BridgeStateRef = Arc<Mutex<BridgeState>>;
```

为什么是**一把**锁而不是 5 把？因为很多操作需要**原子地**读写多张表。比如 `create_channel` 既要往 `channels` 插入新通道，又要顺手清掉同一 `rid` 在 `terminal_errors` / `ready_callbacks` / `ready_signals` / `pending_sends` 里可能残留的旧账目——这五步必须「要么全做、要么全不做」，否则别的线程会读到「半更新」的中间状态。用一把锁串行化，是最简单正确的做法。

为什么是 `std::sync::Mutex` 而不是 `tokio::sync::Mutex`？因为**临界区极短**（几次 `HashMap` 插入/删除），且**从不跨 `.await` 持有**。`std::Mutex` 在这里更轻、更快；如果用 `tokio::Mutex` 且不慎跨 `.await`，反而可能死锁或阻塞运行时。

#### 4.2.3 源码精读

类型别名与账本定义：

[`rust/sglang-grpc/src/bridge.rs:37`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L37) —— `type BridgeStateRef = Arc<Mutex<BridgeState>>`，这把共享账本的「地址」会被 `clone` 进每个回调对象。

[`rust/sglang-grpc/src/bridge.rs:39-46`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L39-L46) —— `BridgeState` 的 5 张表。注意 `#[derive(Default)]`：空 `HashMap`/`HashSet` 可零值初始化，所以 `BridgeState::default()` 就是一本空账本。

`PyBridge` 在构造时新建一本空账本：

[`rust/sglang-grpc/src/bridge.rs:106-113`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L106-L113) —— `state: Arc::new(Mutex::new(BridgeState::default()))`，整本账在 `PyBridge` 诞生时就一份，之后靠 `Arc` 共享。

#### 4.2.4 代码实践

1. **实践目标**：验证「5 张表共享一把锁」对一致性是必需的。
2. **操作步骤**：阅读 `create_channel`（4.4 节）和 `remove_channel_refs_locked`（[bridge.rs:463-469](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L463-L469)），数一数单个函数里连续修改了几张表。
3. **观察现象**：`create_channel` 在持锁期间一次改了 `channels`/`terminal_errors`/`ready_callbacks`/`ready_signals`/`pending_sends` 共 5 张表中的 5 项；`remove_channel_refs_locked` 同样一次改 4 张表。
4. **预期结果**：如果这些表各自有独立的锁，那么「插入 channel 后、删除旧 terminal_error 前」这个窗口里，别的线程会读到不一致状态。一把锁把整个函数体变成原子操作，消除了这个窗口。

#### 4.2.5 小练习与答案

**练习 1**：`BridgeState` 为什么用 `#[derive(Default)]` 就够了，而 `ResponseChunk` / `TerminalError` 没有也没有必要有 `Default`？

**答案**：`BridgeState` 全是 `HashMap`/`HashSet`，它们的 `Default` 都是「空集合」，正好对应「服务刚启动、无任何在途请求」的初始状态。而 `ResponseChunk`/`TerminalError` 是「某个具体事件」的枚举，没有自然合理的「默认事件」，强行加 `Default` 反而误导。

**练习 2**：假设把 `channels` 单独拆出来用一把锁、其余 4 张表共用另一把锁，`create_channel` 的去重逻辑还安全吗？

**答案**：不安全。`create_channel` 必须在「检查 `channels` 是否含 `rid`」与「插入新 channel 并清旧账」之间不允许别的线程插入操作；拆成两把锁后，两次加锁之间会有窗口，可能出现两个线程都通过去重检查、都插入通道的竞态。所以这里必须用单一临界区。

---

### 4.3 lock_or_recover：防「锁中毒」的安全锁

#### 4.3.1 概念说明

Rust 的 `std::sync::Mutex` 有一个特性：**如果某个线程在持有锁时 panic**（比如解包 `None`、数组越界），这把锁会被标记为「中毒」（poisoned）。此后任何线程再 `.lock()` 它，都会返回 `Err(PoisonError)`。

如果在 gRPC 服务里直接写 `self.state.lock().unwrap()`，那么**一次请求处理中的 panic 就会让整个 `BridgeState` 永久不可用**——后续每个请求一拿锁就跟着 panic，服务等于挂了。这显然太脆弱。

`lock_or_recover` 就是为这个场景兜底的小工具：**拿不到锁（中毒了）时，不 panic，而是记一条警告日志，然后强行取出内部数据继续用**。

#### 4.3.2 核心流程

```
lock_or_recover(mutex, name):
    mutex.lock() 成功  → 直接返回 guard
    mutex.lock() 失败(中毒) → tracing::warn!(...) 记日志
                           → poisoned.into_inner() 取出内部数据
                           → 返回 guard（基于可能不一致的数据）
```

关键点：`PoisonError::into_inner()` 把「被中毒的数据」原样交还。这是一种**有损恢复**——数据可能处于 panic 前的半更新状态，但「服务还能跑」比「服务整体崩溃」更重要。毕竟这只是一次推理请求的账本，最坏情况是某条请求结果异常，不至于拖垮整个进程。

#### 4.3.3 源码精读

这个函数是泛型的（`<T>`），可护住任意类型的 `Mutex`：

[`rust/sglang-grpc/src/bridge.rs:77-82`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L77-L82) —— `lock_or_recover` 用 `unwrap_or_else` 处理 `PoisonError`：记 `tracing::warn!`（带 `mutex = name` 标签，方便定位是哪把锁中了毒），再 `into_inner()` 取回数据。

注意第二个参数 `name: &'static str`：调用方传入 `"state"` 这样的字面量，纯粹是为了在日志里标明「是 `state` 这把锁中毒了」，便于运维定位。本文件里所有加锁点（`create_channel`、`submit_request`、`abort`、`remove_channel`、回调的 `__call__`、背压辅助函数等）**统一**走这个函数，因此整个桥接层对「锁中毒」都具备同样的恢复能力。

#### 4.3.4 代码实践

1. **实践目标**：体会「中毒」与「恢复」的差别。
2. **操作步骤**：在 `bridge.rs` 里用 `Grep` 搜索 `lock_or_recover`，统计它被调用了多少次（预期十几次）。再对比 `lib.rs` 里 `GrpcServerHandle` 等其他模块是否也有类似处理。
3. **观察现象**：所有访问 `BridgeState` 的入口都经过 `lock_or_recover`，没有任何一处裸写 `.lock().unwrap()`。
4. **预期结果**：这保证了「即使某条请求的处理逻辑 panic，账本锁也能被恢复，服务继续运行」，是桥接层的健壮性基石。

> 待本地验证：在测试里人为往某个持锁闭包里塞一个 `panic!()`，观察后续 `lock_or_recover` 是否打出 `Recovering from poisoned gRPC bridge mutex` 警告且不崩溃。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `lock_or_recover` 换回 `.lock().unwrap()`，一次 panic 的后果是什么？

**答案**：锁被永久标记中毒，之后**所有**请求只要碰 `BridgeState`（建通道、发 chunk、中止……）都会因 `unwrap()` 再次 panic，整个 gRPC 服务事实上瘫痪。`lock_or_recover` 把「单点 panic」隔离成「一条警告 + 可能脏的数据」，避免雪崩。

**练习 2**：`into_inner()` 取回的数据可能「脏」，桥接层为什么不担心？

**答案**：因为账本里每条记录都按 `rid` 索引、且是「覆盖式」写入（`insert` 会覆盖旧值），脏数据最多影响正在出问题的那条 `rid`；新请求用新 `rid`，开新通道，不受旧脏数据影响。再加上 `create_channel` 注册新通道时会顺手清掉同 `rid` 的旧账（4.4 节），进一步限制了脏数据的波及范围。

---

### 4.4 create_channel：建通道 + rid 去重 + 旧状态清理

#### 4.4.1 概念说明

`create_channel(rid)` 是「在账本上为某个请求开一页」的入口。它做三件事：

1. **建通道**：用 `mpsc::channel(response_channel_capacity)` 造一对 `(Sender, Receiver)`。
2. **去重**：如果 `rid` 已经是「在途」状态（`channels` 里已有），拒绝重复开页，返回错误。
3. **清旧账**：如果是新 `rid`（或旧 `rid` 的上一轮已彻底结束），在插入新 `Sender` 的同时，把该 `rid` 在其余 4 张表里可能残留的旧记录全部删掉。

#### 4.4.2 核心流程

```
create_channel(rid):
    (sender, receiver) = mpsc::channel(容量)
    加锁 state
    if state.channels 含 rid:        # 在途重复
        返回 Err(PyRuntimeError("Duplicate active gRPC request id: {rid}"))
    state.channels.insert(rid, sender)         # 注册新通道
    state.terminal_errors.remove(rid)          # 清旧终端错误
    state.ready_callbacks.remove(rid)          # 清旧就绪回调
    state.ready_signals.remove(rid)            # 清旧就绪信号
    state.pending_sends.remove(rid)            # 清旧停泊标记
    返回 Ok(receiver)
```

注意一个**容易看错的细节**：去重检查命中（`channels` 已含 `rid`）时，函数**直接返回错误，不做任何清理**——因为那个 `rid` 的通道还活着，绝不能动它。清旧账只在「新通道注册成功」这一分支发生，清理的是**上一轮已结束**的同名 `rid` 残留（比如客户端复用一个固定 `rid`，而上一轮请求已经彻底收尾、通道已移除，但 `terminal_errors` 之类的旁账可能还在）。

为什么注册新通道时要清旧账？因为 `rid` 在正常流程里是 `uuid`（几乎不会重号），但**客户端可以自带 `rid`**（见 server.rs 里 [`server.rs:227-230`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L227-L230) 的 `unwrap_or_else(|| uuid::new_v4())`——客户端传了就用客户端的）。客户端复用 `rid` 重发请求时，必须保证上一轮的终端错误、停泊标记等不会污染这一轮。

#### 4.4.3 源码精读

[`rust/sglang-grpc/src/bridge.rs:130-145`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L130-L145) —— `create_channel` 全貌。

逐行对照上面的流程：

- **L131**：`mpsc::channel(self.response_channel_capacity)` 建通道——容量来自 `PyBridge` 配置（u1-l4 归一化后的值，默认 64）。
- **L132**：`lock_or_recover` 拿锁（4.3 节）。
- **L133–138**：去重检查。命中就返回 [`PyRuntimeError`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L1)（`"Duplicate active gRPC request id: {rid}"`），**不清理**。
- **L139**：注册新 `Sender`。
- **L140–143**：清掉该 `rid` 在另外 4 张表里的残留——`terminal_errors` / `ready_callbacks` / `ready_signals` / `pending_sends`。
- **L144**：返回 `Receiver`（交给 server.rs 的 handler）。

注意返回的是 `PyResult<Receiver<ResponseChunk>>`：`Receiver` 是「单消费者」端，只有一个，谁拿到 `Receiver` 谁就负责消费这条通道（即 tonic handler）；`Sender` 留在账本里供回调使用。

#### 4.4.4 代码实践（本讲核心实践之一）

1. **实践目标**：精确回答「遇到重复 `rid` 时返回什么错误、清掉了哪些旧状态」。
2. **操作步骤**：
   - 打开 [`bridge.rs:130-145`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L130-L145)。
   - 区分两个分支：**去重命中**（`channels` 已含 `rid`）与**注册成功**。
3. **观察现象**：
   - 去重命中：返回 `PyRuntimeError("Duplicate active gRPC request id: {rid}")`，**且不清任何旧状态**——现存通道原封不动。
   - 注册成功：插入新 `Sender`，并清掉该 `rid` 的 `terminal_errors`、`ready_callbacks`、`ready_signals`、`pending_sends` 四项旁账。
4. **预期结果**：你能口头复述「重复在途 `rid` → 拒绝；复用已结束 `rid` → 允许并清旧账」这条规则，并能指出清理发生在**非重复分支**。
5. **延伸**：思考「为什么不在去重命中分支也清旧账」——答案见下方练习 2。

> 待本地验证：写一个最小 Rust 测试，构造 `PyBridge`（需 mock 或仅测 `BridgeState` 行为），对同一 `rid` 连续调两次 `create_channel`：第一次应 `Ok`，第二次应返回含 `"Duplicate"` 的 `PyRuntimeError`。

#### 4.4.5 小练习与答案

**练习 1**：`create_channel` 返回的 `Receiver` 只有一份，如果被丢弃了会怎样？

**答案**：`Receiver` 被 drop 后，对应的 `Sender` 再 `send` 会失败（`TrySendError::Closed`）。在 `try_send_chunk`（[bridge.rs:596-605](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L596-L605)）里这会被识别为客户端断开，触发 `close_channel_with_error(ClientDisconnected)`。所以「`Receiver` 被丢」会自动收敛成一条终端错误，不会泄漏。

**练习 2**：去重命中时（`channels` 已含 `rid`）为什么**不**顺手清掉 `terminal_errors[rid]` 之类的旁账？

**答案**：因为去重命中意味着该 `rid` 的通道**还活着**，旁账里如果真有 `terminal_errors[rid]` 那才是严重的不一致（活通道却记着终端错误）——但更根本的是：这条路径的语义是「拒绝重复请求」，任何修改账本都是错上加错。正确做法是只读检查、立即拒绝，把现存通道的状态原样保留给正在处理它的那个 handler。

---

### 4.5 submit_request：跨 GIL 把请求交给 Python + 失败清理

#### 4.5.1 概念说明

`submit_request(rid, req_type, req_dict)` 是数据型 RPC（generate / embed / classify）的公共提交入口。它把「建通道」和「跨 GIL 调 Python」串起来：

1. 调 `create_channel(rid)` 拿 `Receiver`。
2. 持 GIL，把 `req_dict`（Rust 的 `serde_json::Value` map）转成 Python `dict`，再造一个 `ChunkCallback` 回调对象。
3. 组装 `kwargs`：`{req_type, req_dict, chunk_callback}`，调 `runtime_handle.submit_request(**kwargs)` 把请求交给 Python。
4. **失败回滚**：如果 Python 调用抛 `PyErr`，立刻 `remove_channel(rid)` 把刚开的通道撤掉，再把错误向上传。

#### 4.5.2 核心流程

```
submit_request(rid, req_type, req_dict):
    receiver = create_channel(rid)?                      # 先在账本开页
    rid_owned = rid.to_string()
    result = Python::with_gil(|py| {                      # 跨进 Python 世界
        py_req_dict = json_map_to_pydict(py, req_dict)?   # serde_json map → PyDict
        callback    = make_chunk_callback(py, rid_owned)? # 造 PyO3 回调
        kwargs = {req_type, req_dict: py_req_dict, chunk_callback: callback}
        runtime_handle.call_method("submit_request", (), kwargs)?  # 交给 Python
    })
    match result:
        Ok(()) => Ok(receiver)                # 成功：把消费端交给 handler
        Err(e) => { remove_channel(rid); Err(e) }  # 失败：撤通道，传错误
```

**为什么失败要 `remove_channel(rid)`？** 这是最关键的一点。`create_channel` 已经把通道注册进了账本，意味着 Python 回调「理论上」已经能往里塞 chunk。但如果 `call_method("submit_request", ...)` 抛了错，说明 **Python 那一侧根本没成功接到这个请求**（或没来得及记录）——于是**没有任何人会再往这条通道推终端 chunk**。如果此时不撤通道：

- 账本里留下一条「僵尸通道」：`Sender` 在，但没人会再发数据。
- handler 那边拿着 `Receiver` 等啊等，永远等不到 `Finished`/`Error`，要么挂死、要么等超时（u2-l3 的 `recv_chunk_with_timeout`）。
- 更糟的是，`rid` 被这条僵尸占着，客户端若用同 `rid` 重发，会被 `create_channel` 的去重逻辑挡掉（4.4 节）。

所以 `remove_channel(rid)` 是「把半开的页撕掉」：它不仅删 `channels` 里的 `Sender`，还顺手清 `terminal_errors` 等旁账（见 [bridge.rs:437-441](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L437-L441) 与 [`remove_channel_refs_locked`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L463-L469)），让这条 `rid` 彻底回到「从未开过」的干净状态。

#### 4.5.3 源码精读

`submit_request` 全貌：

[`rust/sglang-grpc/src/bridge.rs:177-207`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L177-L207) —— 三段式：建通道 → 跨 GIL 调 Python → 成败分流。

几个要点：

- **L183**：`create_channel(rid)?` 用 `?` 把「重复 `rid`」之类错误直接向上传——此时还没建任何东西，无需清理。
- **L186–198**：整个 Python 交互被 `Python::with_gil` 包住。注意 [`make_chunk_callback`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L147-L156) 在造回调时，把 `state.clone()`（账本的 `Arc`）、`runtime_handle.clone_ref(py)`、`tokio_handle.clone()` 都塞进了回调对象——这正是「回调能反向操作账本、能在通道满时 `spawn` 异步任务」的根。
- **L190–196**：`kwargs` 的三个键 `req_type` / `req_dict` / `chunk_callback`，正是 Python 端 `RuntimeHandle.submit_request` 期望的参数（u1-l1 提到的 `chunk_callback` 就是这里造的 PyO3 对象）。
- **L200–206**：成败分流。`Ok(())` 返回 `receiver`；`Err(err)` 先 `self.remove_channel(rid)` 再 `Err(err)`——**先撤后传**，保证错误传到上层时账本已干净。

`remove_channel` 的实现：

[`rust/sglang-grpc/src/bridge.rs:437-441`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L437-L441) —— 先 `remove_channel_refs_locked` 删 `channels`/`pending_sends`/`ready_callbacks`/`ready_signals`，再单独删 `terminal_errors[rid]`。

它委托给 [`remove_channel_refs_locked`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L463-L469)（[bridge.rs:463-469](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L463-L469)），后者一次删 4 张表并返回「是否真删到了东西」（布尔值，给 `abort` 等判断用，u3-l2 详讲）。

调用端长这样（server.rs 的 `text_generate`）：

[`rust/sglang-grpc/src/server.rs:233-236`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L233-L236) —— handler 调 `submit_request`，用 `map_err` 把 `PyErr` 转成 gRPC `Status`（错误映射见 u3-l3）。

> 同构的兄弟方法：`submit_json`（[bridge.rs:315-335](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L315-L335)）是控制型/OpenAI 型 RPC 的提交入口，结构与 `submit_request` 几乎一致（建通道 → 跨 GIL → 失败 `remove_channel`），区别只在于它用 `make_json_callback` 造 JSON 回调、并用闭包 `call` 抽象了「具体调哪个 Python 方法」。`submit_flush_cache` / `submit_openai` 等都只是给它传不同闭包。

#### 4.5.4 代码实践（本讲核心实践之二）

1. **实践目标**：精确回答「Python 调用失败时为什么要 `remove_channel(rid)`」。
2. **操作步骤**：
   - 阅读 [`bridge.rs:200-206`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L200-L206) 的 `Err` 分支。
   - 追 `remove_channel` → `remove_channel_refs_locked`（[bridge.rs:463-469](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L463-L469)），列出它删了哪些表。
3. **观察现象**：失败分支删除了 `channels`、`pending_sends`、`ready_callbacks`、`ready_signals`、`terminal_errors` 五张表里与该 `rid` 相关的全部条目。
4. **预期结果**：你能解释「不删则通道变僵尸、`Receiver` 永远等不到终端 chunk、`rid` 还被占着挡重发」这一连串后果，从而理解回滚的必要性。
5. **延伸阅读**：对照 `submit_json`（[bridge.rs:328-334](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L328-L334)）的 `Err` 分支，确认它也有同样的 `remove_channel(rid)`——说明这是桥接层的统一契约。

> 待本地验证：在 `submit_request` 的 `Err` 分支临时加一行 `tracing::warn!(rid, "submit_request rolled back channel after python error: {err}");`，构造一个会让 Python `submit_request` 抛错的请求（如传非法 `req_type`），观察日志是否打出回滚告警，并用调试器确认账本里已无该 `rid`。

#### 4.5.5 小练习与答案

**练习 1**：`create_channel(rid)?` 这一步如果失败（重复 `rid`），需不需要 `remove_channel`？

**答案**：不需要。`create_channel` 失败意味着通道**根本没注册进账本**（重复检查命中时直接返回错误，没执行 `insert`），账本里没有任何需要清理的新东西。只有「注册成功之后」的 Python 调用失败，才需要回滚。代码里 `create_channel` 用 `?` 提前返回、不进清理分支，正体现了这一点。

**练习 2**：`submit_request` 把整个 Python 调用包在 `Python::with_gil` 里。如果 Python 的 `submit_request` 内部又去跑了一段很慢的同步推理，会发生什么？

**答案**：GIL 会被这次调用一直占着，期间**所有** Python 线程都无法执行字节码——这正是 2.1 节警告的「别在异步线程里同步阻塞 Python」。所以 SGLang 的 `RuntimeHandle.submit_request` 设计上**不**同步跑完推理，而是「登记请求 + 立即返回」，真正的推理在 Python 的调度器线程里异步进行，chunk 事后通过回调推回来。`submit_request` 持 GIL 的时间应很短（建任务、入队）。

**练习 3**：`kwargs` 里为什么把 `chunk_callback` 作为参数显式传给 Python，而不是让 Python 自己从某处取？

**答案**：因为回调是 **per-request** 的，它内部绑定了这个请求的 `rid` 和对应的通道 `Sender`（通过 `state` 的 `Arc`）。把回调作为参数传，是「告诉 Python：这个请求的 chunk 请用这个特定回调推回来」的最直接方式，避免 Python 端再用 `rid` 去查表。回调对象的构造见 `make_chunk_callback`（[bridge.rs:147-156](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L147-L156)）。

## 5. 综合实践

把本讲的知识串起来，做一个「**手动追踪一次请求在账本里的完整一生**」的阅读型实践。

**任务**：以 `text_generate` 为例，从「客户端发来请求」到「通道从账本里消失」，画出 `BridgeState` 里该 `rid` 条目的状态变迁，并标注每一步对应源码的位置。

**操作步骤**：

1. **入口**：读 [`server.rs:222-236`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L222-L236)。确定 `rid`（客户端传或 `uuid` 生成）→ `submit_request(&rid, "generate", req_dict)`。
2. **开页**：进入 [`bridge.rs:177-207`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L177-L207) 的 `submit_request`，`create_channel` 把 `Sender` 写进 `channels[rid]`，并清掉同 `rid` 旧账。
3. **交 Python**：`make_chunk_callback` 造回调（绑 `rid` + 账本 `Arc` + handle），`kwargs` 传给 `runtime_handle.submit_request`。
4. **失败路径假设**：若 Python 抛错，`remove_channel`（[bridge.rs:437-441](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L437-L441)）把该 `rid` 从五张表里抹掉，结束。
5. **成功路径继续**：拿到 `Receiver`，进入流式循环（u2-l3）。Python 每产出一个 chunk，回调 `__call__`（[bridge.rs:631-694](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L631-L694)）经 `try_send_chunk`（[bridge.rs:524-607](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L524-L607)）塞进通道。
6. **收尾**：收到终端 chunk（`Finished`/`Error`）时，`try_send_chunk` 的 `Ok` 分支调 `remove_channel_refs`（[bridge.rs:536-538](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L536-L538)）把 `channels[rid]` 等删掉；若通道被强制关停，则 `close_channel_with_error`（[bridge.rs:449-461](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L449-L461)）先把 `TerminalError` 写进 `terminal_errors[rid]`，等 handler 通过 [`take_terminal_error`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L443-L446)（见 [`server.rs:188-197`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L188-L197) 的 `closed_stream_status`）取出并转成 gRPC 状态码。

**产出**：一张状态变迁表，形如：

| 时刻 | 事件 | `channels[rid]` | `terminal_errors[rid]` | 源码 |
| --- | --- | --- | --- | --- |
| t0 | `create_channel` | 插入 `Sender` | 删除（清旧账） | bridge.rs:139–143 |
| t1 | Python 调用成功 | 在 | — | bridge.rs:200–201 |
| t1' | Python 调用失败 | 删除 | 删除 | bridge.rs:202–205 → 437–441 |
| t2 | 正常收 `Finished` | 删除 | — | bridge.rs:536–538 |
| t2' | 通道满/断开 | 删除 | 插入 `TerminalError` | bridge.rs:547–554 / 597–604 |

**预期结果**：你能解释「`rid` 在账本里从无到有、再从有到无」的完整闭环，并指出**每一个让条目消失的出口**（成功收尾、失败回滚、强制关停），从而验证账本不会泄漏。

## 6. 本讲小结

- **`PyBridge` 是桥接层外壳**：持有 Python `runtime_handle`、Tokio `tokio_handle`、共享账本 `state`、分词器与配置；全局单例（`Arc`），per-request 差异全靠 `state` 里的 `rid` 区分。
- **`BridgeState` 是共享账本**：5 张以 `rid` 为主键的表（`channels` / `pending_sends` / `ready_callbacks` / `ready_signals` / `terminal_errors`），被同一把 `std::sync::Mutex` 护住以保证多表操作的原子性；临界区短且不跨 `.await`。
- **`lock_or_recover` 防锁中毒**：把 `.lock().unwrap()` 换成「中毒则记警告 + `into_inner` 取回」，让单次 panic 不至于雪崩式拖垮整个服务。
- **`create_channel` 建通道 + 去重 + 清旧账**：重复在途 `rid` 返回 `PyRuntimeError("Duplicate active gRPC request id: ...")` 且不动现存通道；注册新通道时顺手清掉该 `rid` 在其余 4 张表里的残留。
- **`submit_request` 三段式**：`create_channel` → `Python::with_gil` 调 `runtime_handle.submit_request(req_type, req_dict, chunk_callback)` → 失败 `remove_channel(rid)` 回滚。回滚是必需的，否则通道变僵尸、`Receiver` 永远等不到终端 chunk、`rid` 还被占着挡重发。
- **回看路径**：`remove_channel` / `remove_channel_refs_locked` / `try_send_chunk` / `close_channel_with_error` 共同构成了「通道从账本里消失」的全部出口，保证账本不泄漏。

## 7. 下一步学习建议

本讲讲清了「账本怎么开页、怎么撤页」，但**通道里的 chunk 是怎么被消费的**、**回调对象内部长什么样**还没展开。建议按顺序继续：

1. **u2-l6 回调机制**：精读 `ChunkCallback.__call__` 与 `JsonChunkCallback.__call__`，看 Python 推过来的 dict / bytes 如何被提取成 `ResponseData`、如何决定 `Data`/`Finished`/`Error`，以及 `meta_info` 为何逐值做 JSON 编码。这是「Python → Rust」那一半数据流的细节。
2. **u2-l7 请求字典构建**：精读 `utils/request_utils.rs` 的 `build_*_dict` 与 `py_utils.rs` 的 `json_map_to_pydict`，理解 `submit_request` 收到的那个 `req_dict` 是怎么从 proto 消息一步步变成 Python dict 的。
3. **u3-l1 背压与 pending-send 停泊**：本讲多次提到「通道满时启动背压」却没展开——那里会精读 `try_send_chunk` 的 `TrySendError::Full` 分支、`register_pending_send`、`mark_send_ready` 与 `on_ready` 信号链，是 `BridgeState` 后四张表真正发挥作用的地方。
4. **u3-l2 中止传播**：精读 `abort` / `abort_all` 如何批量 `drain` 通道、写入 `TerminalError::Aborted`，以及 `RequestAbortGuard` 如何在响应流提前结束时把取消传回 Python。

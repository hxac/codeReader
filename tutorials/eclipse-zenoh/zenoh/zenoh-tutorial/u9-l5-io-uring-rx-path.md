# io_uring 接收路径：零拷贝异步读

## 1. 本讲目标

本讲是「内部架构（三）：传输与链路层」单元的最后一讲，专门拆解 Zenoh 在本次更新中新增的 **Linux io_uring 接收路径**。学完后你应该能够：

- 说清 `zenoh-uring` 内部 crate 的 `Reader` / arena / window 设计，以及它如何用 io_uring 的 multishot `RecvMulti` 实现异步、零拷贝的接收。
- 掌握 `TransportManagerState.uring` 是如何由 `batch_size` 与 `link_rx_buffer_size` 计算并初始化的，以及初始化失败时如何**优雅回退**到 tokio RX。
- 理解 `rx_task` 如何根据「uring 是否可用」与「链路能否给出 `fd`」这两个条件，在 `rx_task_uring` 与 `rx_task_non_uring` 之间分派。
- 知道 streamed（流式，如 TCP）与非 streamed（数据报，如 connected UDP）链路分别走 `setup_fragmented_read` / `setup_read`，以及为什么**未连接 UDP** 必须回退到 tokio。

本讲是 u9-l1（Link 层）、u9-l2（Transport 层）、u9-l4（批处理/分片）的延续，把「接收」这一侧补完。

## 2. 前置知识

阅读本讲前，建议先建立以下直觉（对应前置讲义）：

- **io_uring 是什么**：Linux 5.1+ 提供的异步 I/O 接口。用户态把「我想做的事」写进**提交队列（Submission Queue, SQ）**，内核做完后把结果写进**完成队列（Completion Queue, CQ）**，整个过程不需要每次系统调用都陷入内核，也不需要 epoll 那种「就绪后再 read」的两段式。可以把它理解为一个「内核态的待办事项邮箱」。
- **multishot 接收与 provided buffers**：io_uring 的 `RecvMulti`（对应 `IORING_OP_RECV_MULTISHOT`）是一次提交、持续产出多个完成事件的接收操作；配合 **buffer group / provided buffers**，内核收到数据后直接写进预先注册好的缓冲池里，再在完成事件里告诉用户「我用了池子里第几号缓冲」——这就是零拷贝的关键。
- **Zenoh 的接收路径（u9-l4）**：对端发来的字节先被解析成一个个 **batch**（一帧），再由 `read_messages` 解码成 `TransportMessage` / `NetworkMessage`。流式链路（TCP）每帧前有 2 字节长度前缀，数据报链路（UDP）则没有。
- **`RBatch<TBuffer>`**：接收侧的「批量读取器」，封装了一段字节缓冲与解码器。本讲会看到它为何被泛型化。
- **`RawFd`**：Unix 系下「文件描述符」的原始整数表示。socket 在内核里就是一个 fd，把 fd 交给 io_uring，io_uring 才能直接对它做异步操作。

一句话定位：io_uring 在 Zenoh 里是一个**可选的、Linux 专用的接收加速器**，不是必需品——任何一步走不通都会回退到原来的 tokio 异步读路径，功能完全等价，只是更快。

## 3. 本讲源码地图

本讲涉及的关键文件按「由内到外」排列：

| 文件 | 作用 |
| --- | --- |
| `commons/zenoh-uring/src/lib.rs` | 内部 crate 入口，仅 Linux 编译，转发 `linux` 模块。 |
| `commons/zenoh-uring/src/linux/api/reader/mod.rs` | `Reader`：io_uring reactor 主循环、`setup_read`/`setup_fragmented_read`、multishot 接收。 |
| `commons/zenoh-uring/src/linux/api/reader/rx_buffer.rs` | `RxBuffer`：借用 arena 内存的零拷贝缓冲，drop 时归还。 |
| `commons/zenoh-uring/src/linux/api/reader/fragmented_batch.rs` | `FragmentedBatch`/`DefragmentationState`：跨多个 `RxBuffer` 的流式帧重组。 |
| `commons/zenoh-uring/src/linux/reader/window.rs` | `RxWindow`：解析 2 字节长度前缀、累积成完整 batch 的状态机。 |
| `commons/zenoh-uring/src/linux/api/reader/read_task.rs` | `ReadTask`：一次接收任务的句柄与生命周期（启动/停止/错误传递）。 |
| `io/zenoh-transport/src/uring.rs` | `Uring`：传输层对 `Reader` 的薄封装，按 batch/link 配置算尺寸。 |
| `io/zenoh-transport/src/manager.rs` | `TransportManagerState.uring` 的初始化与回退。 |
| `io/zenoh-transport/src/unicast/universal/link.rs` | `rx_task` 分派、`rx_task_uring`、streamed/非 streamed 分支。 |
| `io/zenoh-transport/src/common/batch.rs` | 泛型 `RBatch<TBuffer>` 与 `initialize_uring`。 |
| `io/zenoh-link-commons/src/unicast.rs` | `LinkUnicastTrait::get_fd` 抽象方法。 |
| `io/zenoh-links/zenoh-link-{tcp,udp,…}/src/unicast.rs` | 各链路对 `get_fd` 的具体实现（给 fd 或 bail）。 |

## 4. 核心概念与源码讲解

### 4.1 zenoh-uring crate：Reader、arena 与 io_uring 异步读模型

#### 4.1.1 概念说明

`zenoh-uring` 是一个**内部 crate**（不稳定、不对外），它在 `commons` 第 1 层，把 Linux io_uring 的复杂 API 封装成一个简单的「读模型」。它的核心抽象是 `Reader`：一个 `Reader` 代表**一个 io_uring reactor**，可以在其上挂载任意多条链路的接收任务。

为什么需要它？原来的 tokio 接收路径是「epoll 就绪 → `read` 系统调用 → 拷贝到用户缓冲」——每来一批数据至少一次系统调用、一次拷贝。io_uring 把这两步都省了：内核直接把数据写进预先注册的缓冲池（**provided buffers**），完成后只在 CQ 里贴一张「用了 X 号缓冲、长度 Y」的便签。在高吞吐场景下（大量小 batch），这能显著降低 CPU 开销。

`Reader` 的设计有三个关键零件：

- **arena（缓冲池）**：`BatchArena` → `ReservableArena` → `GroupedArena`，层层包装。它预分配一批定长缓冲，注册成 io_uring 的 buffer group，供 `RecvMulti` 直接写入。
- **window（重组窗口）**：流式链路的 `RecvMulti` 返回的字节块**不保证按 batch 边界对齐**，`RxWindow` 负责按 2 字节长度前缀把碎片拼回完整 batch。
- **reactor 主循环**：一个跑在 `ZRuntime::RX` 上的阻塞线程，反复处理 CQ 事件与外部命令（启动/停止某条链路的接收）。

#### 4.1.2 核心流程

一个 `Reader` 的生命周期大致如下：

1. `Reader::new(batch_size, batch_count)`：创建 eventfd 唤醒器、命令通道、退出标志；创建 io_uring ring（4096 项）；创建 arena；在 `ZRuntime::RX` 上 `spawn_blocking` 启动 reactor 线程。
2. 某条链路就绪后，调用 `setup_read(fd, cb)` 或 `setup_fragmented_read(fd, cb)`：经命令通道向 reactor 发 `StartRx(fd, …)`，reactor 为它分配一个 buffer group、提交一个 `RecvMulti` multishot，并把分配的 `index` 回传。
3. reactor 主循环：从 CQ 取完成事件 → 若是接收事件，用 `buffer_select` 取出缓冲编号，构造 `Arc<RxBuffer>` 调用回调；缓冲用尽（`ENOBUFS`）或 multishot 结束时自动重新提交 `RecvMulti`。
4. 链路关闭时，`ReadTask::stop()` 发 `StopRx(index)`，reactor 取消该 multishot 并释放上下文。

数据流通路（以非 streamed 为例）：

```
内核 → provided buffer(arena) → Arc<RxBuffer> → 回调 → ZSlice → RBatch → read_messages
                                              ↑
                                   drop RxBuffer 时归还 arena
```

#### 4.1.3 源码精读

crate 入口仅在 Linux 下编译并整体导出 `linux` 模块：

[commons/zenoh-uring/src/lib.rs:20-24](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-uring/src/lib.rs#L20-L24) —— 非 Linux 平台整个 crate 为空，保证可移植性。

`Reader` 是一个可克隆的句柄，内部是 `Arc<ReaderInner>`，外加一个 `watch::Receiver<String>` 用来感知 reactor 是否异常退出：

[commons/zenoh-uring/src/linux/api/reader/mod.rs:55-59](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-uring/src/linux/api/reader/mod.rs#L55-L59)

`Reader::new` 创建唤醒器、命令通道、退出标志，并启动 reactor 线程：

[commons/zenoh-uring/src/linux/api/reader/mod.rs:89-107](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-uring/src/linux/api/reader/mod.rs#L89-L107)

注意第 90 行给 `batch_size` 预留了 50% 余量，以减少 `ENOBUFS`（缓冲不够）错误。

reactor 线程内部构造 io_uring ring 与 arena。ring 用了 `setup_submit_all`（一次性提交全部）、`setup_defer_taskrun`（把内核侧任务执行推迟到 `enter` 时，减少上下文切换）、`setup_single_issuer`（单线程提交优化）：

[commons/zenoh-uring/src/linux/api/reader/mod.rs:112-124](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-uring/src/linux/api/reader/mod.rs#L112-L124)

当 reactor 收到 `StartRx(fd, …)` 命令时，为该 fd 分配一个 `BufferGroup`（arena 里的一组 provided buffers），提交一个 `RecvMulti` multishot，并把该上下文登记到 `context_storage`：

[commons/zenoh-uring/src/linux/api/reader/mod.rs:147-161](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-uring/src/linux/api/reader/mod.rs#L147-L161)

`read_multi` 处理一个接收完成事件：失败时按错误码分派（`ENOBUFS` → 重启 multishot、`ECANCELED` → bail）；成功时用 `buffer_select` 取出缓冲编号，构造 `Arc<RxBuffer>` 并回调，必要时重启 multishot：

[commons/zenoh-uring/src/linux/api/reader/mod.rs:297-355](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-uring/src/linux/api/reader/mod.rs#L297-L355)

`RxBuffer` 是零拷贝的关键：它的 `data` 是 `&'static mut [u8]`，**直接借用 arena 的内存**（不拷贝），实现 `ZSliceBuffer` 从而可被 `ZSlice` 包裹；`Drop` 时把缓冲归还给 arena 供下次复用：

[commons/zenoh-uring/src/linux/api/reader/rx_buffer.rs:24-29](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-uring/src/linux/api/reader/rx_buffer.rs#L24-L29) —— 字段定义。

[commons/zenoh-uring/src/linux/api/reader/rx_buffer.rs:59-63](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-uring/src/linux/api/reader/rx_buffer.rs#L59-L63) —— drop 归还。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：理解 multishot 接收的「重启」语义。
2. **步骤**：打开 [commons/zenoh-uring/src/linux/api/reader/mod.rs](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-uring/src/linux/api/reader/mod.rs#L297-L355)，找到 `read_multi`，回答：`ENOBUFS` 与 `more == false`（`IORING_CQE_F_BUFFER` 注释处）这两种情况下，代码都做了什么共同的动作？为什么要这么做？
3. **预期**：两者都会**重新提交一个 `RecvMulti`**。因为 multishot 接收要么因缓冲池耗尽（`ENOBUFS`）暂停，要么因内核一次只补一个缓冲（`more` 标志清零）而结束，必须重新提交才能继续接收。
4. 待本地验证（可选）：开启 `uring_trace` feature 编译 `zenoh-uring` 的测试，运行 `cargo test -p zenoh-uring --features uring_trace`，观察 trace 日志中的 `Read multishot entry` 计数。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Reader::new` 要在 `batch_size` 基础上加 50% 余量？
**答案**：io_uring 的 provided buffer 必须能完整容纳一次 `RecvMulti` 返回的字节块。若缓冲刚好等于 batch 大小，遇到略长的读数会触发 `ENOBUFS` 并频繁重启 multishot；预留余量可显著降低该概率。

**练习 2**：`RxBuffer::data` 为什么用 `&'static mut [u8]` 这种看似危险的生命周期？
**答案**：arena 的内存在 reactor 运行期间确实存活，但 Rust 无法表达「存活到 RxBuffer drop 为止」这种区域约束，故用 `'static` 配合 `unsafe`。安全性由 arena 的 `recycle_batch`（drop 时归还、不可重复使用）在运行时保证。

---

### 4.2 TransportManager 的 Uring 状态与初始化回退

#### 4.2.1 概念说明

`Reader` 是 per-「io_uring reactor」的，但 Zenoh 不需要每条链路各起一个 reactor。`TransportManager` 在构造时**最多创建一个** `Uring` 实例，存放在 `TransportManagerState.uring: Option<Uring>` 里，所有 unicast 链路共享它的 `Reader`（`Reader` 是 `Clone` 的 `Arc` 句柄）。

最关键的设计是「**失败即回退**」：io_uring 的可用性受内核版本、运行时权限、资源等影响，初始化可能失败。Zenoh 的策略是——**失败时只打一条 warn 日志，把 `uring` 置为 `None`，所有链路随后自动走 tokio 路径**。因此 uring 是加速器而非必需品。

#### 4.2.2 核心流程

`TransportManager` 构造时（`zenoh::open` 阶段）：

1. 读取配置 `batch_size` 与 `link_rx_buffer_size`。
2. 调 `Uring::new(batch_size, link_rx_buffer_size)`：
   - 给 `batch_size` 加 2 字节（流式帧的 2 字节长度前缀）。
   - 计算缓冲数量 `batch_count`。
   - 创建 `Reader`。
3. 用 `.map_err(打 warn).ok()` 把可能的 `Err` 转成 `None` 存入 state。

缓冲数量 `batch_count` 的计算公式为：

\[
\text{batch\_count} = \max\!\left(\left\lfloor \frac{\text{link\_rx\_buffer\_size}}{\text{batch\_size} + 2} \right\rfloor,\ 16\right)
\]

而 `Reader` 内部每个缓冲的实际容量再额外加 50% 余量：

\[
\text{buffer\_len} = \left\lfloor (\text{batch\_size} + 2) \times 1.5 \right\rfloor
\]

`max(..., 16)` 保证即使配置的 RX 缓冲很小，也至少有 16 个缓冲可供 multishot 循环使用，避免频繁 `ENOBUFS`。

#### 4.2.3 源码精读

`Uring` 是对 `Reader` 的薄封装，只多了一层「按传输配置算尺寸」的逻辑：

[io/zenoh-transport/src/uring.rs:20-34](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-transport/src/uring.rs#L20-L34) —— `Uring::new` 加 2 字节长度前缀、用 `max(.../batch_size, 16)` 算 `batch_count`。

`TransportManagerState` 的字段仅在 `uring+linux` feature 下存在：

[io/zenoh-transport/src/manager.rs:178-179](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-transport/src/manager.rs#L178-L179)

初始化处的回退逻辑——`.map_err` 里打 warn，`.ok()` 把 `Err` 变 `None`：

[io/zenoh-transport/src/manager.rs:506-513](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-transport/src/manager.rs#L506-L513)

这两个配置字段本身定义在 `TransportManagerConfig`：

[io/zenoh-transport/src/manager.rs:116-128](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-transport/src/manager.rs#L116-L128) —— `batch_size` 与 `link_rx_buffer_size`。

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：确认「回退是进程级一次性决定，而非每条链路重新尝试」。
2. **步骤**：在 [io/zenoh-transport/src/manager.rs](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-transport/src/manager.rs#L506-L513) 中确认 `uring` 只在 `TransportManagerState` 构造时赋值一次；再用 Grep 搜索 `state.uring` 在整个 `io/zenoh-transport` 中的所有读取点，确认它们都只做 `.is_some()` / `.as_ref()` 判断，而不会再次尝试初始化。
3. **预期**：`uring` 一旦为 `None`，整个进程的生命周期内所有链路都走 tokio 路径；这是有意为之，避免每条链路重复付出初始化开销。
4. 待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `link_rx_buffer_size` 配置成比 `batch_size + 2` 还小，`batch_count` 会是多少？
**答案**：`floor(小于 1 的值) = 0`，但 `max(0, 16) = 16`，所以是 16。这保证了最小可用缓冲数。

**练习 2**：为什么用 `.ok()` 而不是 `?` 把错误向上传播？
**答案**：`?` 会让 `TransportManager::build` 失败，从而导致整个 `zenoh::open` 失败。Zenoh 希望 io_uring 不可用时仍能正常运行（只是慢一点），故吞掉错误、降级为 `None`。

---

### 4.3 rx_task 分派与泛型 RBatch / initialize_uring

#### 4.3.1 概念说明

每条 unicast 链路在 `start_rx` 时会 spawn 一个 `rx_task`。这个任务第一件事就是**分派**：决定本链路走 uring 还是 tokio。分派依据两个条件，**必须同时满足**才走 uring：

1. **进程级**：`transport.manager.state.uring.is_some()`（reactor 初始化成功）。
2. **链路级**：`link.link.get_fd().is_ok()`（本链路能给出裸 fd）。

任一不满足，立刻回退到 `rx_task_non_uring`。

本模块还要回答一个看似独立、实则同源的问题：为什么 `RBatch` 要泛型化为 `RBatch<TBuffer>`，并新增 `initialize_uring`？因为 tokio 路径读出来的是连续的 `ZSlice`，而 uring 的 streamed 路径读出来的是**多片 `ZBuf`**（一个 batch 跨多个 `RxBuffer`）。为了让同一套 `read_messages` 解码逻辑同时服务两条路径，`RBatch` 必须能容纳不同的缓冲类型，解码入口也必须能处理「压缩批次需要先解压到连续缓冲」的情况。

#### 4.3.2 核心流程

```
start_rx
   │
   ▼
rx_task ──▶ uring.is_some() && get_fd().is_ok() ?
   │                        │
   │ 是                     │ 否
   ▼                        ▼
rx_task_uring           rx_task_non_uring  (tokio read_loop, 见 u9-l4)
   │
   ├─ is_streamed()? ── 是 ──▶ setup_fragmented_read  (回调收到 FragmentedBatch)
   │                 否 ──▶ setup_read               (回调收到 Arc<RxBuffer>)
   │
   ▼
 tokio::select! { 读错误 / reactor 结束 / lease 超时 / 取消 }
   │
   ▼
 uring_read_task.stop()
```

无论哪条路径，最终都把一个 `RBatch<_>` 交给 `read_messages` 解码。`read_messages` 是泛型的：

[io/zenoh-transport/src/unicast/universal/rx.rs:235-241](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-transport/src/unicast/universal/rx.rs#L235-L241) —— `read_messages<TBuffer: BacktrackableReader + Buffer + Debug>`，同一套解码器服务两条路径。

#### 4.3.3 源码精读

分派逻辑在 `rx_task` 开头，极其精炼：

[io/zenoh-transport/src/unicast/universal/link.rs:373-385](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-transport/src/unicast/universal/link.rs#L373-L385) —— 两个条件都满足才 `return rx_task_uring(...).await`，否则落到下面的 `rx_task_non_uring`。

`RBatch` 的泛型定义（注意 `TBuffer: BacktrackableReader + Buffer` 约束）：

[io/zenoh-transport/src/common/batch.rs:424-440](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-transport/src/common/batch.rs#L424-L440)

`initialize_uring` 是 uring 路径专用的「帧头拆分 + 可选解压」入口。它先 `split_uring` 读出 batch 头字节；若启用了 `transport_compression` 且头部标记了压缩，就把负载解压到外部传入的缓冲（即 `pool` 分配的 MTU 缓冲），并返回解压后的新 batch；否则返回 `None` 表示「无需解压、直接用原缓冲解码」：

[io/zenoh-transport/src/common/batch.rs:465-503](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-transport/src/common/batch.rs#L465-L503)

`DecompressUring` trait 把两种缓冲类型的解压结果统一化：`RBatch<ZSlice>` 解压后原地替换（`Result = ()`）；`RBatch<ZBufReader>` 因为不能原地改类型，返回一个全新的 `RBatch<ZSlice>`：

[io/zenoh-transport/src/common/batch.rs:505-525](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-transport/src/common/batch.rs#L505-L525)

这正是「同一套解码逻辑兼容两种缓冲」的关键。

#### 4.3.4 代码实践（源码阅读型）

1. **目标**：追踪 uring 路径下，一个压缩 batch 与一个未压缩 batch 分别如何被处理。
2. **步骤**：打开 [io/zenoh-transport/src/unicast/universal/link.rs](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-transport/src/unicast/universal/link.rs#L631-L697) 的 `rx_task_uring`，找到 `DefragmentationState::Fragmented(buf)` 分支，对照 `initialize_uring` 的返回值，回答：当 `initialize_uring` 返回 `Some` 时调用 `read_batch` 传的是什么？返回 `None` 时呢？
3. **预期**：`Some(decompressed_batch)` 时传解压后的 `RBatch<ZSlice>`（连续缓冲）；`None` 时传原始的 `RBatch<ZBufReader>`（多片缓冲），由泛型 `read_messages` 直接解码。
4. 待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：分派条件为什么需要「两个都满足」而不是只看 `uring.is_some()`？
**答案**：即使 reactor 初始化成功，某些链路（如 TLS、QUIC、未连接 UDP）也无法给出裸 fd，没有 fd 就无法提交 `RecvMulti`，必须再判断 `get_fd().is_ok()` 并按链路回退。

**练习 2**：`RBatch<ZBufReader<'_>>` 与 `RBatch<ZSlice>` 在解压时的返回类型为何不同？
**答案**：`ZSlice` 是单段连续内存，解压结果可原地替换其 `buffer` 字段；`ZBufReader` 是多片缓冲的只读游标，解压需要一块连续内存承载，故返回一个全新的 `RBatch<ZSlice>`。

---

### 4.4 get_fd 与 setup_read / setup_fragmented_read（streamed vs 非 streamed）

#### 4.4.1 概念说明

拿到 fd 之后，`rx_task_uring` 还要再分一次岔：**链路是否是流式的（`is_streamed()`）**。

- **流式链路（TCP、unixsock stream、vsock、unixpipe）**：字节是连续的「河流」，没有天然的「一条消息边界」。io_uring 的 `RecvMulti` 返回的字节块大小任意，可能包含半个 batch、一个 batch、或三个半 batch。所以必须用 `setup_fragmented_read` + `RxWindow` 按 2 字节长度前缀把碎片**拼回完整 batch**。
- **数据报链路（connected UDP）**：每个数据报天然有边界，一次 `RecvMulti` 返回就是一个完整 batch（或空）。用更简单的 `setup_read`，回调直接收到 `Arc<RxBuffer>`。

而**未连接 UDP（unconnected UDP）是一个特例**：它的 socket 被多个 peer 共享，tokio 路径靠源地址做**demux（分用）**。如果把它的裸 fd 交给 io_uring `RecvMulti`，内核会把**任何 peer** 的数据报都投递到这条链路，绕过 demux，造成数据串流。因此未连接 UDP 的 `get_fd` 直接 `bail!`，强制回退 tokio。

#### 4.4.2 核心流程

streamed 路径的重组状态机 `RxWindow` 有三个状态：

```
Initial ──(读到 2 字节长度 size)──▶ Accumulating(size)
   ▲                                    │
   │                                    │ 累积满 size 字节
   │                                    ▼
   └────────── on_batch(FragmentedBatch) ◀── (可能剩余字节回到 Initial 或 SizeFragmented)

SizeFragmented：上一次只读到 1 字节长度（半个 size），等下一个缓冲补齐。
```

每个 `FragmentedBatch` 经 `defragment()` 转成 `Single(ZSlice)`（单缓冲）或 `Fragmented(ZBuf)`（多缓冲拼成的 ZBuf）。

#### 4.4.3 源码精读

`LinkUnicastTrait::get_fd` 是新增的抽象方法，仅 `uring+linux` 下存在：

[io/zenoh-link-commons/src/unicast.rs:95-96](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-link-commons/src/unicast.rs#L95-L96)

**能给 fd 的链路**（裸 socket）：TCP 直接取 `as_raw_fd`：

[io/zenoh-links/zenoh-link-tcp/src/unicast.rs:203-209](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-links/zenoh-link-tcp/src/unicast.rs#L203-L209)

**主动 bail 的链路**：TLS（fd 在 TLS 层之下，交给 uring 读到的是密文）：

[io/zenoh-links/zenoh-link-tls/src/unicast.rs:265-268](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-links/zenoh-link-tls/src/unicast.rs#L265-L268)

QUIC 同理（quinn 不暴露 fd）：

[io/zenoh-links/zenoh-link-quic/src/unicast.rs:176-180](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-links/zenoh-link-quic/src/unicast.rs#L176-L180)

**最关键的未连接 UDP**——注释完整解释了回退原因：

[io/zenoh-links/zenoh-link-udp/src/unicast.rs:276-294](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-links/zenoh-link-udp/src/unicast.rs#L276-L294) —— `Connected` 给 fd；`Unconnected` 与 `Reliable` `bail!`。

`Reader` 提供的两个 setup 方法：

[commons/zenoh-uring/src/linux/api/reader/mod.rs:67-87](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-uring/src/linux/api/reader/mod.rs#L67-L87) —— `setup_fragmented_read` 用 `RxWindow` 包裹回调（拼帧）；`setup_read` 直接用原回调。

`RxWindow` 解析长度前缀、累积碎片的 `push`（Initial 状态分支）：

[commons/zenoh-uring/src/linux/reader/window.rs:56-125](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-uring/src/linux/reader/window.rs#L56-L125)

`FragmentedBatch::defragment` 把一或多个 `RxBuffer` 转成 `ZSlice` 或 `ZBuf`：

[commons/zenoh-uring/src/linux/api/reader/fragmented_batch.rs:44-71](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-uring/src/linux/api/reader/fragmented_batch.rs#L44-L71)

最后是 `rx_task_uring` 的 streamed/非 streamed 分支：

[io/zenoh-transport/src/unicast/universal/link.rs:631-677](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-transport/src/unicast/universal/link.rs#L631-L677) —— streamed：`setup_fragmented_read`，回调处理 `Single`/`Fragmented` 两种 defragment 结果，并按是否解压选 batch。

[io/zenoh-transport/src/unicast/universal/link.rs:678-697](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-transport/src/unicast/universal/link.rs#L678-L697) —— 非 streamed：`setup_read`，回调把 `Arc<RxBuffer>` 转 `ZSlice` 直接解码。

退出时的多路选择（错误/结束/lease 超时/取消）与 `stop()`：

[io/zenoh-transport/src/unicast/universal/link.rs:700-721](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-transport/src/unicast/universal/link.rs#L700-L721)

#### 4.4.4 代码实践（动手运行型，需 Linux）

1. **目标**：用 tracing 日志确认一条 TCP unicast 链路走了 `rx_task_uring`，并验证未连接 UDP 会回退。
2. **环境**：Linux 内核 ≥ 5.1（建议 5.10+），Rust 工具链就绪。
3. **步骤**：
   - 编译 examples 并开启 uring 特性（uring 特性位于 `zenoh` crate，需通过它传播）：
     ```bash
     cargo build --release --example z_sub --example z_pub --features zenoh/uring
     ```
   - 终端 A（订阅端，开 tracing）：
     ```bash
     RUST_LOG="zenoh_uring=debug,zenoh_transport::unicast::universal::link=debug" \
       ./target/release/examples/z_sub
     ```
   - 终端 B（发布端）：
     ```bash
     ./target/release/examples/z_pub
     ```
4. **需要观察的现象**：
   - 订阅端日志应出现 `Setting up fragmented read task for fd: <n>`（来自 [setup_fragmented_read](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-uring/src/linux/api/reader/mod.rs#L67-L87)），说明 TCP（streamed）走了 uring 路径。
   - 若内核/权限不支持 io_uring，则会看到 `io_uring reactor init failed, falling back to tokio RX`（来自 [manager.rs:509](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-transport/src/manager.rs#L506-L513)），此时不会有 fragmented read 的日志。
5. **预期结果**：TCP 链路优先走 uring；若用未连接 UDP（默认 `z_pub`/`z_sub` 走 scouting 多播会用到 UDP，但建连默认走 TCP），把 listen/connect 端点显式改成 `udp/...` 且为未连接形态时，应回退 tokio（不出现 setup_read 日志）。
6. 若本地无法运行或日志行不可复现，请标注「待本地验证」。

> 说明：`connected UDP` 需要特殊配置才会出现；日常 `z_pub`/`z_sub` 默认用 TCP，是观察 uring 最直接的场景。

#### 4.4.5 小练习与答案

**练习 1**：为什么 streamed 链路必须用 `RxWindow`，而非 streamed 不用？
**答案**：streamed 字节流无消息边界，`RecvMulti` 返回的块可能跨 batch，必须按 2 字节长度前缀重新切分累积；非 streamed（数据报）每块天然是一个完整 batch，无需重组。

**练习 2**：未连接 UDP 若强行交给 io_uring `RecvMulti` 会发生什么？
**答案**：未连接 UDP socket 被多 peer 共享，`RecvMulti` 会把任意 peer 的数据报都投递进来，绕过 tokio 路径按源地址的 demux，导致数据串流到错误的链路。故其 `get_fd` 直接 `bail!`。

**练习 3**：`ReadTask` 的 `Drop` 实现做了什么？为什么 `rx_task_uring` 退出时还要再调一次 `stop().await`？
**答案**：`Drop` 发送 `StopRx(index)` 命令取消 multishot；`stop().await` 额外**等待 reactor 真正销毁该上下文**（循环消费 `error_receiver`），确保资源在任务结束前已释放，避免 reactor 残留悬空上下文。

## 5. 综合实践

把本讲四个模块串起来，完成一次「**判定 + 解释**」综合任务：

**任务**：假设你在 Linux 上以 `zenoh/uring` 特性编译并运行了一个 router，它同时承载三条 unicast 链路：一条 TCP、一条 TLS、一条 connected UDP。请回答：

1. 进程启动时，`TransportManager` 会创建几个 `Uring`？为什么？  
   *（提示：见 4.2，是 per-manager 而非 per-link。）*
2. 这三条链路各自会走 `rx_task_uring` 还是 `rx_task_non_uring`？分别走 `setup_read` 还是 `setup_fragmented_read`？  
   *（提示：TCP→streamed+有 fd；TLS→bail 无 fd；connected UDP→非 streamed+有 fd。）*
3. 若运行环境的内核非常老、io_uring 初始化失败，三条链路的行为分别是什么？整个 router 还能正常收数据吗？  
   *（提示：见 4.2 的 `.ok()` 回退；功能不受影响，仅性能降级。）*

**交付物**：写一段不少于 10 行的中文说明，画出三条链路的分派结果表（链路 / 是否走 uring / setup 方法 / 原因），并给出第 3 问的结论。

参考答案要点：

| 链路 | 走 uring? | setup 方法 | 原因 |
| --- | --- | --- | --- |
| TCP | 是 | `setup_fragmented_read` | streamed，`get_fd` 返回裸 fd |
| TLS | 否（回退 tokio） | — | `get_fd` bail（fd 在 TLS 之下，读到密文） |
| connected UDP | 是 | `setup_read` | 非 streamed，`get_fd` 返回裸 fd |

内核不支持时：`uring` 为 `None`，三条链路**全部**走 `rx_task_non_uring`，router 仍正常工作。

## 6. 本讲小结

- `zenoh-uring` 内部 crate 用一个 `Reader`（io_uring reactor）+ arena（provided buffers）+ window（流式重组）实现了零拷贝异步接收，核心是 multishot `RecvMulti`。
- `TransportManager` 最多创建一个共享 `Uring`，存于 `state.uring: Option<Uring>`；初始化失败时 `.ok()` 静默回退为 `None`，整个进程改走 tokio。
- `rx_task` 用「`uring.is_some()` 且 `get_fd().is_ok()`」两个条件分派；任一不满足即走 `rx_task_non_uring`。
- streamed 链路走 `setup_fragmented_read`（`RxWindow` 按 2 字节长度前缀拼帧），非 streamed 走 `setup_read`（数据报天然有边界）。
- 各链路按能否给出裸 fd 决定回退：TCP/unixsock/vsock/unixpipe/connected UDP 给 fd；TLS/QUIC/WS/serial/未连接 UDP `bail!`——未连接 UDP 因共享 socket、会绕过 demux 故必须回退。
- `RBatch<TBuffer>` 泛型化 + `initialize_uring` + `DecompressUring` 让同一套 `read_messages` 解码逻辑同时服务 tokio（`ZSlice`）与 uring（`ZSlice` 或 `ZBufReader`）两条路径，并兼容压缩批次。

## 7. 下一步学习建议

- **横向对比**：回到 u9-l4 与本讲的 `rx_task_non_uring`，对比 tokio 的 `read_loop` 与 uring 的 `rx_task_uring`，体会「就绪模型（epoll）」与「完成模型（io_uring）」在 Zenoh 代码结构上的差异。
- **深入缓冲层**：阅读 u10-l3（ZBuf / ZSlice 零拷贝缓冲），理解 `ZBufReader` 为何能实现 `Buffer` trait 从而充当 `RBatch` 的缓冲类型——这是 uring streamed 路径能复用解码器的底层支撑。
- **链路层补全**：结合 u9-l1 的 `LocatorInspector` 与本讲的 `get_fd` 表，整理一张「每条链路的能力矩阵」（可靠 / 多播 / streamed / 给 fd），这是后续做链路选型与性能调优的基础。
- **关注演进**：io_uring 接入是较新的机制，建议持续关注 `commons/zenoh-uring/` 与 `io/zenoh-transport/src/unicast/universal/link.rs` 的后续提交，尤其是 QUIC/TLS 暴露 fd 的 `TODO`（见 quinc/serial 的 `get_fd` 注释）一旦解决，对应链路也将切到 uring 路径。

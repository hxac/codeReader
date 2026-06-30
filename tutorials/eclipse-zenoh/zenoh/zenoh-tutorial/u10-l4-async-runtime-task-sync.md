# 异步基础：runtime / task / sync / result

## 1. 本讲目标

本讲属于「内部架构（四）：协议模型与基础 crate」单元，聚焦支撑 Zenoh 全部异步运行的四个内部基础 crate。读完本讲你应当能够：

- 说清 `zenoh-runtime` 的 `ZRuntime` 为什么把 Tokio 运行时拆成 `Application / Acceptor / TX / RX / Net` 五个**命名执行器**，以及它们各自负责什么。
- 掌握 `zenoh-task` 的 `TaskController`（批量管理）与 `TerminatableTask`（单个管理）两种「可取消任务」模式，会用 `terminate_all` 带超时地优雅关停一组后台任务。
- 理解 `zenoh-sync` 的 `Condition` 异步条件变量，以及它和 `Signal`、`FifoQueue` 的关系。
- 知道 `zenoh-result` 的 `ZResult<T>` / `Error` / `zerror!` 统一错误模型，理解 Zenoh 为什么不直接用 `Result<T, Box<dyn Error>>`。

本讲是承上启下的「地基」：上面几讲的 `Runtime::start`、`TransportManager`、路由 worker 都跑在这些执行器上，下面的协议消息、线编码也都靠这些基础原语来同步与报错。依赖《u7-l1 Session 内部与 Runtime》已经建立的「Runtime 承载节点全部连接状态」的认知。

## 2. 前置知识

在进入 Zenoh 的源码前，先用通俗语言把几个 Tokio 概念补齐（如果你已经熟悉，可跳到第 3 节）。

- **Future 与 async/await**：`Future` 是「一个还没算完、将来会有结果的计算」。`async fn` 把函数体编译成一个状态机 `Future`，`await` 是「挂起、等结果」的暂停点。一个 `Future` 自己不会跑，必须有人不断「唤醒（poll）」它。
- **Runtime（运行时）**：负责「不断 poll 那些 Future」的调度器。Tokio 的多线程运行时有一池工作线程（worker threads）不断从任务队列里取任务执行；遇到 `await` 就挂起当前任务、去执行别的，从而实现并发。
- **spawn**：把一个 `Future` 丢给运行时，让它后台独立运行，返回 `JoinHandle`。Zenoh 几乎所有「循环任务」（收包、发包、心跳、树计算）都是 spawn 出来的。
- **block_in_place / block_on**：在异步上下文里「同步地阻塞当前线程」去等一个 `Future`。容易死锁，要小心。
- **CancellationToken / TaskTracker**：这是 `tokio_util` 提供的两个关停原语。`CancellationToken` 是一个可取消的开关，`cancel()` 后所有 `.cancelled()` 的 `await` 立即返回；`TaskTracker` 记录所有由它 spawn 的任务，`close()` 后不再接收新任务、`wait()` 等所有在册任务结束。Zenoh 的 `TaskController` 正是这两者的组合包装。

一句话总览这四个 crate 的分工：

| crate | 解决的问题 | 一句话 |
| --- | --- | --- |
| `zenoh-runtime` | 「任务跑在哪个线程池」 | 提供五个命名 Tokio 执行器，按职责隔离 |
| `zenoh-task` | 「一堆后台任务怎么优雅关停」 | `TaskController` 批量取消 + 带超时等待 |
| `zenoh-sync` | 「任务之间怎么互相等待/通知」 | `Condition` 条件变量、`Signal` 一次性信号等 |
| `zenoh-result` | 「错误怎么统一表达」 | `ZResult<T>` + `zerror!` 宏 |

## 3. 本讲源码地图

本讲涉及的关键文件（均在 `commons/` 下，属内部 crate，不保证稳定）：

- [`commons/zenoh-runtime/src/lib.rs`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-runtime/src/lib.rs) —— `ZRuntime` 枚举、`ZRuntimePool` 全局池、`spawn`/`block_in_place`。
- [`commons/zenoh-macros/src/zenoh_runtime_derive.rs`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-macros/src/zenoh_runtime_derive.rs) —— 为 `ZRuntime` 生成 `iter`/`init`/`Borrow<RuntimeParam>` 的派生宏，是理解「环境变量如何变成运行时」的钥匙。
- [`commons/zenoh-task/src/lib.rs`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-task/src/lib.rs) —— `TaskController` 与 `TerminatableTask`。
- [`commons/zenoh-sync/src/lib.rs`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-sync/src/lib.rs) —— 同步原语总入口（导出 `Condition`/`Signal`/`FifoQueue`/`Mvar` 等）。
- [`commons/zenoh-sync/src/condition.rs`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-sync/src/condition.rs) —— `Condition` 条件变量实现。
- [`commons/zenoh-sync/src/signal.rs`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-sync/src/signal.rs) —— `Signal` 一次性触发器（作为对照）。
- [`commons/zenoh-sync/src/fifo_queue.rs`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-sync/src/fifo_queue.rs) —— 真实使用 `Condition` 的有界队列，是本讲的最佳范例。
- [`commons/zenoh-result/src/lib.rs`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-result/src/lib.rs) —— `Error`/`ZResult`/`ZError`/`zerror!`/`bail!`。

辅助印证「真实用法」的文件：`zenoh-ext/src/group.rs`（`TaskController` 批量关停）、`io/zenoh-transport/src/**`（命名执行器实战）、`commons/zenoh-config/...`（运行时可调静态量）。

## 4. 核心概念与源码讲解

本讲按四个最小模块拆分：`ZRuntime`、`TaskController`、`Condition`、`ZResult`。它们彼此正交：`ZRuntime` 决定「在哪跑」，`TaskController` 决定「怎么停」，`Condition` 决定「怎么等」，`ZResult` 决定「错了怎么办」。

### 4.1 ZRuntime：命名执行器（zenoh-runtime）

#### 4.1.1 概念说明

Tokio 默认给你「一个」多线程运行时，所有 `spawn` 的任务挤在同一个工作线程池里。这对普通应用没问题，但对一个高吞吐、低延迟的网络协议栈是危险的：如果「收包（RX）」「发包（TX）」「路由计算（Net）」「应用回调（App）」全挤在一个池里，那么一个慢回调或一次昂贵的树计算就可能把收包线程饿死，导致心跳超时、连接被断。

`zenoh-runtime` 的解法是 **按职责把任务分到不同的命名执行器**：每个 `ZRuntime` 变体对应一个独立的 Tokio 运行时，各有自己的工作线程数。这样「RX 收包」永远不会因为「App 回调慢」而被拖住。这是 Zenoh 区分多个命名执行器的根本原因——**隔离故障域、保证快路径不被慢路径阻塞**。

#### 4.1.2 核心流程

五个命名执行器的职责划分（结合传输层实战用法推断）：

| 变体 | serde 名 | 默认 worker_threads | 典型用途 |
| --- | --- | --- | --- |
| `Application` | `app` | 1 | 用户回调、应用层任务（如 SHM provider 的阻塞调用、group 心跳） |
| `Acceptor` | `acc` | 1 | 接受新连接（accept 循环） |
| `TX` | `tx` | 1 | 发送侧任务（写 socket） |
| `RX` | `rx` | 2 | 接收侧任务（读 socket、解析批） |
| `Net` | `net` | 1 | 网络/路由相关任务（如 transport 删除、lowlatency 收包） |

整体生命周期：

1. 进程启动，`lazy_static` 初始化全局 `ZRUNTIME_POOL`（含五个 `OnceLock<Runtime>` 槽位，此时**还没**创建真正的 Tokio 运行时）。
2. 某处第一次调用 `ZRuntime::RX.spawn(...)`，触发 `ZRuntimePool::get`，`OnceLock::get_or_init` 才惰性调用 `init()` 真正 `build` 出 Tokio 运行时。
3. 之后所有对该执行器的 `spawn` 都落在同一个运行时上。
4. 进程退出、`ZRuntimePool` drop 时，对每个运行时调 `shutdown_timeout(1s)` 等待收尾。

配置在「进程启动那一刻」一次性读取环境变量 `ZENOH_RUNTIME`（RON 格式）解析为 `GlobalRuntimeParam`，之后不再变化。

```
ZENOH_RUNTIME 环境变量 (RON)
        │  (派生宏 zenoh_runtime_derive 解析)
        ▼
GlobalRuntimeParam { app, acc, tx, rx, net }   ← 每个是 RuntimeParam
        │
        ▼  首次 spawn 时惰性 build
ZRuntimePool: { app→Runtime, acc→Runtime, tx→Runtime, rx→Runtime, net→Runtime }
        │
        ▼  Deref<Target=Handle>
ZRuntime::RX.spawn(fut)  ==  Handle::spawn(fut)  (落在 RX 运行时)
```

#### 4.1.3 源码精读

`ZRuntime` 是一个五变体枚举，每个变体用 `#[param(...)]` 标注默认 `worker_threads`：

[commons/zenoh-runtime/src/lib.rs:103-128](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-runtime/src/lib.rs#L103-L128) —— `ZRuntime` 枚举定义，注意 `RX` 默认 2 个工作线程，其余 1 个。

`RuntimeParam` 描述单个执行器的「建造图纸」，`build` 真正造出 Tokio 运行时：

[commons/zenoh-runtime/src/lib.rs:46-84](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-runtime/src/lib.rs#L46-L84) —— 关键点：`new_multi_thread()` 多线程调度器、`thread_name_fn` 把线程命名为 `<zrt>-<id>`（如 `rx-0`，便于用 `top -H`/`htop` 定位）、必须 `enable_io()`+`enable_time()` 才能用异步 IO 与定时器。

`spawn` 本身极薄，只是 `Deref` 到 `Handle` 再 spawn：

[commons/zenoh-runtime/src/lib.rs:131-140](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-runtime/src/lib.rs#L131-L140) —— `ZRuntime::spawn`；开启 `tracing-instrument` feature 时会把当前 `tracing::Span` 附着到任务上（分布式追踪用）。

[commons/zenoh-runtime/src/lib.rs:166-171](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-runtime/src/lib.rs#L166-L171) —— `Deref<Target = Handle>`，所以 `ZRuntime` 用起来就像一个 Tokio `Handle`。

全局池的惰性初始化与 **handover（移交）** 机制是理解可调性的关键：

[commons/zenoh-runtime/src/lib.rs:215-232](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-runtime/src/lib.rs#L215-L232) —— `get` 先读该变体的 `RuntimeParam`，若 `handover` 指向另一个执行器，就**重定向**到那个执行器（如配置 `rx: (handover: app)` 后，所有 `RX` 任务其实跑在 `app` 运行时上）。这让运维能在不重启不改代码的前提下「合并/拆分」执行器做调优。

`block_in_place` 是 Zenoh 里少数「同步等异步」的入口，它主动检查不能在单线程运行时上用：

[commons/zenoh-runtime/src/lib.rs:142-163](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-runtime/src/lib.rs#L142-L163) —— 若当前是 `CurrentThread` 调度器会直接 panic（提示改用 `multi_thread`），否则用 `tokio::task::block_in_place` 把当前工作线程转为阻塞态再 `block_on`。

派生宏 `RegisterParam` 生成了 `iter()`、`init()` 和 `Borrow<RuntimeParam>`，并把环境变量解析成全局参数：

[commons/zenoh-macros/src/zenoh_runtime_derive.rs:206-236](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-macros/src/zenoh_runtime_derive.rs#L206-L236) —— `ZENOH_RUNTIME` 环境变量用 RON 解析为 `ZRUNTIME_PARAM`；`init()` 按「变体 → 对应 param → `build(variant)`」造运行时。

> 说明：源码中 `zrt.init()`、`ZRuntime::iter()`、`zrt.borrow()` 这些在 `lib.rs` 里**找不到定义**——它们是上面这个派生宏在编译期生成的，直接读 `lib.rs` 会困惑，必须配合 `zenoh_runtime_derive.rs` 一起看。

#### 4.1.4 代码实践

**实践目标**：理解命名执行器的隔离意义，并学会用 `ZENOH_RUNTIME` 调参。

由于 `zenoh-runtime` 是内部 crate（不在 crates.io 发布），推荐用**源码阅读 + 现场调参**两步：

1. **操作步骤（源码阅读）**：在 `io/zenoh-transport/src/` 下用搜索查看五类执行器各被谁用。可参考：
   - 收包用 `RX`：[io/zenoh-transport/src/unicast/universal/rx.rs:73](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/universal/rx.rs#L73)
   - 发包用 `TX`：[io/zenoh-transport/src/unicast/universal/link.rs:246](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/universal/link.rs#L246)
   - 阻塞式收尾用 `Net` 的 `spawn_blocking`：[io/zenoh-transport/src/unicast/universal/tx.rs:175](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/universal/tx.rs#L175)
2. **操作步骤（调参）**：启动任意 zenoh 程序前设置环境变量，例如：
   ```bash
   ZENOH_RUNTIME='( rx: (worker_threads: 4), app: (worker_threads: 2) )' ./target/release/zenohd
   ```
3. **需要观察的现象**：用 `htop` 查看线程名，应能看到形如 `rx-0`、`rx-1`、`rx-2`、`rx-3`、`app-0`、`app-1`、`tx-0`、`net-0` 的线程；改 `worker_threads` 后对应名字的线程数量随之变化。
4. **预期结果**：线程数与配置一致，证明 RON 配置被 `ZRUNTIME_PARAM` 正确解析。
5. **若无法运行**：明确标注「待本地验证」，至少完成第 1 步的源码阅读，整理出「哪个执行器跑哪类任务」的对照表。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `RX` 的默认 `worker_threads` 是 2，而 `TX`/`Net` 只有 1？
**参考答案**：收包侧要同时做「读 socket」和「解析批 / 分用消息」两件事，且收包是吞吐瓶颈、不能被解析拖住，所以多给一个线程；发包侧消息已经组装好、写 socket 相对轻量，单线程足够，多了反而增加锁竞争与乱序风险。

**练习 2**：配置 `rx: (handover: app)` 后，调用 `ZRuntime::RX.spawn(f)` 的任务实际跑在哪个运行时上？
**参考答案**：跑在 `app` 运行时上。`ZRuntimePool::get` 检测到 `RX` 的 `handover = Some(Application)`，于是重定向到 `Application` 对应的 `OnceLock` 槽位，相当于「合并」了 RX 与 App 两个执行器。

### 4.2 TaskController / TerminatableTask：可取消任务管理（zenoh-task）

#### 4.2.1 概念说明

后台任务（心跳、收包、路由计算）必须在关停时被**确定性地取消**，否则要么泄漏（任务一直跑、进程退不掉），要么卡死（任务持有锁、drop 时死锁）。直接 `JoinHandle::abort()` 太粗暴——被 abort 的任务可能在任意 `await` 点被打断，处于半成品状态；而让它「自然结束」又要求任务自己周期检查「是否该退了」。

`zenoh-task` 给出两套互补工具：

- **`TaskController`**：批量管理「一组」任务。内部是 `tokio_util` 的 `TaskTracker`（追踪所有 spawn 的任务）+ `CancellationToken`（取消信号）的组合。`terminate_all(timeout)` 一次性取消全部并带超时等待。
- **`TerminatableTask`**：管理「单个」任务，且自带 RAII——`Drop` 时自动调用 `terminate(10s)`，适合「一个结构体对应一个后台任务」的场景。

#### 4.2.2 核心流程

`TaskController` 有两类 spawn 方法，区别在「如何被终止」：

| 方法 | 任务如何结束 | 适用场景 |
| --- | --- | --- |
| `spawn` / `spawn_with_rt` | 任务必须**自己**响应 `get_cancellation_token()` 才能退，或能自然跑完 | 任务里有不可中断的资源，需要清理 |
| `spawn_abortable` / `spawn_abortable_with_rt` | `terminate_all` 后**下次 `await` 立即被打断**，返回 `None` | 任务可随时丢弃、无副作用 |

`terminate_all` 的关停流程（同步阻塞）：

```
terminate_all(timeout)
   └─ ResolveFuture::new(async {               ← 用 zenoh-core 把 async 跑成同步
         tokio::time::timeout(timeout,         ← 带超时保护
            terminate_all_async()
         ).await
      }).wait()
         └─ terminate_all_async:
               1. tracker.close()   ← 拒绝新任务
               2. token.cancel()    ← 广播取消信号
               3. tracker.wait()    ← 等所有在册任务结束
```

关键设计：`terminate_all` 是**同步阻塞**函数（返回 `usize` = 未结束任务数），所以可以放在 `Drop` 里调用——这正是 `group.rs` 的做法。

#### 4.2.3 源码精读

`TaskController` 结构极简，就是两个 `tokio_util` 原语的组合：

[commons/zenoh-task/src/lib.rs:29-50](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-task/src/lib.rs#L29-L50) —— `tracker: TaskTracker` + `token: CancellationToken`；`#[derive(Clone)]` 使其可被多个所有者共享（克隆只是增加引用）。

「可 abort」任务的实现靠 `run_until_cancelled_owned`——任务跑到第一次能被取消时返回 `None`：

[commons/zenoh-task/src/lib.rs:54-73](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-task/src/lib.rs#L54-L73) —— `into_abortable` 包一层「子 token + 原任务」的竞争；`spawn_abortable` 把它交给 `tracker.spawn`，故能被 `tracker` 追踪、被 `token.cancel()` 打断。

`spawn_abortable_with_rt` 允许**指定在哪个命名执行器上跑**（衔接上一节的 `ZRuntime`）：

[commons/zenoh-task/src/lib.rs:76-85](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-task/src/lib.rs#L76-L85) —— `tracker.spawn_on(self.into_abortable(future), &rt)`，即「可取消 + 指定运行时」二合一。

`terminate_all` 的同步等待实现：

[commons/zenoh-task/src/lib.rs:128-146](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-task/src/lib.rs#L128-L146) —— `ResolveFuture::new(...).wait()` 是 `zenoh-core` 提供的「在专用线程上把 async 跑成同步」的工具，使得这里能在 `Drop` 等同步上下文里调用；超时则记 `tracing::error!` 并返回仍未结束的任务数。

`TerminatableTask` 的 RAII 关停——`Drop` 即 `terminate(10s)`：

[commons/zenoh-task/src/lib.rs:163-167](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-task/src/lib.rs#L163-L167) —— 只要 `TerminatableTask` 被 drop，就带 10 秒超时地终止它，避免任务逃逸。

[commons/zenoh-task/src/lib.rs:188-206](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-task/src/lib.rs#L188-L206) —— `spawn_abortable` 用 `tokio::select!` 让「取消信号」与「任务本身」赛跑，谁先ready 谁赢。

**真实用法**（最佳范例）：`zenoh-ext` 的 `Group` 持有一个 `TaskController`，启动四个后台任务，drop 时统一关停：

[zenoh-ext/src/group.rs:404-411](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh-ext/src/group.rs#L404-L411) —— `TaskController::default()` 后连续 `spawn_abortable` 四个任务（keep_alive、net_event、query、watchdog）。

[zenoh-ext/src/group.rs:188-193](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh-ext/src/group.rs#L188-L193) —— `impl Drop for Group` 里 `self.task_controller.terminate_all(Duration::from_secs(10))`，10 秒内没退完就记错并放弃。

#### 4.2.4 代码实践

**实践目标**：复刻「`ZRuntime` spawn 两个任务 + `TaskController` 优雅终止」的核心模式，观察取消过程。

由于 `zenoh-runtime`/`zenoh-task` 是内部 crate，这里给出**两个**等价实践：① 用 Tokio 原语复刻（可直接 `cargo new` 运行，行为与 `TaskController` 完全一致，因为它就是这两个原语的包装）；② 源码阅读型，验证真实代码。

**① 可运行复刻（示例代码，标注非项目原代码）**：

```rust
// Cargo.toml 需要：tokio (full), tokio-util
use std::time::Duration;
use tokio_util::{sync::CancellationToken, task::TaskTracker};

#[tokio::main(flavor = "multi_thread", worker_threads = 2)]
async fn main() {
    let tracker = TaskTracker::new();
    let token = CancellationToken::new();

    // 任务 A：可 abort，模拟周期收包
    let t = token.clone();
    tracker.spawn(async move {
        loop {
            tokio::select! {
                _ = t.cancelled() => { println!("A: 收到取消，退出"); break; }
                _ = tokio::time::sleep(Duration::from_millis(200)) => {
                    println!("A: 收一个包");
                }
            }
        }
    });

    // 任务 B：可 abort，模拟周期发包
    let t = token.clone();
    tracker.spawn(async move {
        loop {
            tokio::select! {
                _ = t.cancelled() => { println!("B: 收到取消，退出"); break; }
                _ = tokio::time::sleep(Duration::from_millis(300)) => {
                    println!("B: 发一个包");
                }
            }
        }
    });

    tokio::time::sleep(Duration::from_secs(1)).await; // 跑一会
    println!("=== 触发 terminate_all ===");
    // 对应 TaskController::terminate_all_async 的三步
    tracker.close();
    token.cancel();
    // 对应 terminate_all 的带超时等待
    let _ = tokio::time::timeout(Duration::from_secs(5), tracker.wait()).await;
    println!("=== 全部结束 ===");
}
```

1. **操作步骤**：`cargo new task_demo && cd task_demo`，把上面代码贴进 `src/main.rs`，`cargo run`。
2. **需要观察的现象**：先看到 A/B 交替打印「收/发包」；1 秒后打印「触发 terminate_all」，紧接着 A、B 各自打印「收到取消，退出」，最后「全部结束」。
3. **预期结果**：两个任务都在 `token.cancel()` 后的**下一次 `select!` 轮询**立即退出，主线程的 `tracker.wait()` 随即返回，不会等到 5 秒超时。
4. **若运行报错**：确认 `tokio-util` 已加且开启了对应 feature；若仍不行标「待本地验证」。

**② 源码阅读型**：阅读上面引用的 `zenoh-ext/src/group.rs:404-411` 与 `:188-193`，确认「四个 spawn_abortable + Drop 里 terminate_all(10s)」的真实结构，与本实践一一对应。

#### 4.2.5 小练习与答案

**练习 1**：`spawn` 和 `spawn_abortable` 的关键区别是什么？如果一个任务正在持有 `Mutex` 守卫，用哪个更安全？
**参考答案**：`spawn_abortable` 会在 `terminate_all` 后的下一个 `await` 点直接丢掉任务（返回 `None`），可能让任务停在「持有锁但未释放」的中间状态；`spawn` 的任务不会被强杀，必须自己监听 `get_cancellation_token()` 走正常退出路径、在退出前释放锁。所以持有 `Mutex` 守卫的任务应优先用 `spawn` 并自行响应取消。

**练习 2**：为什么 `terminate_all` 要做成同步阻塞函数，而不是 `async fn`？
**参考答案**：因为它最典型的调用点就是 `Drop`，而 `Drop::drop` 是同步的、不能 `await`。借助 `zenoh_core::ResolveFuture::new(...).wait()` 在专用线程上把 async 关停流程跑成同步，才能在析构里安全调用。

### 4.3 Condition：异步条件变量（zenoh-sync）

#### 4.3.1 概念说明

条件变量（condition variable）是经典的同步原语：一个任务因为「某个条件不满足」（如队列空了）而**挂起等待**，另一个任务在「条件可能满足了」（如往队列里放了东西）时**通知**它醒来。POSIX 的 `pthread_cond_t` 就是这个意思。

`zenoh-sync::Condition` 是它的异步版本，底层用 `event_listener` crate 实现：`wait` 注册一个监听器并挂起当前 async 任务（不占线程），`notify_one`/`notify_all` 唤醒。配合 `Mutex` 一起用，遵循「持锁检查条件 → 释放锁并等待 → 被唤醒后重新拿锁」的经典模式。

> 同 crate 的 `Signal`（[signal.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-sync/src/signal.rs)）是另一个常用原语，但它是一次性触发的「关机信号」（用 `Semaphore` + `AtomicBool` 实现，触发后所有现在和将来的 `wait` 都立刻返回），与可反复使用的 `Condition` 互补。

#### 4.3.2 核心流程

`Condition` 的典型用法（生产者-消费者）：

```
消费者 (pull)                         生产者 (push)
─────────────                        ─────────────
loop {                                loop {
  guard = lock(buffer)                  guard = lock(buffer)
  if let Some(e) = pull() {             if !full() { push(x); }
     drop(guard)                        drop(guard)
     notify_one(not_full)    ───────►   notify_one(not_empty)   ◄── 唤醒消费者
     return e                           return
  }                                    // 满了：等
  not_empty.wait(guard).await  ◄── 挂起，释放 guard
}                                      not_full.wait(guard).await
}
```

注意三个细节：
1. `wait` 的参数是 `AsyncMutexGuard`（Tokio 异步锁的守卫），`wait` 内部会**先 drop 掉 guard**再挂起，避免「持锁等待」死锁。
2. 唤醒后需要 `loop` 重新检查条件——这是条件变量的标准「while 循环」写法（防止虚假唤醒）。
3. `notify_*` 用 `notify_additional_relaxed`，只唤醒「当前正在等」的监听器，不会「存」通知。

#### 4.3.3 源码精读

`Condition` 结构与四个方法：

[commons/zenoh-sync/src/condition.rs:26-65](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-sync/src/condition.rs#L26-L65) —— 内部就一个 `Event`；`wait` 先 `event.listen()` 注册监听器、`drop(guard)` 释放锁、再 `listener.await` 挂起；`waiter` 是同步版本（返回一个 `Pin<Box<EventListener>>` 供后续 `await`）；`notify_one`/`notify_all` 分别唤醒一个/全部。

[commons/zenoh-sync/src/lib.rs:28-50](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-sync/src/lib.rs#L28-L50) —— `zenoh-sync` 的模块总入口，导出 `event`/`fifo_queue`/`lifo_queue`/`object_pool`/`mvar`/`condition`/`signal`/`cache` 等同步原语。

**最佳范例——有界 FIFO 队列**用两个 `Condition`（`not_empty`/`not_full`）做生产者-消费者同步：

[commons/zenoh-sync/src/fifo_queue.rs:20-34](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-sync/src/fifo_queue.rs#L20-L34) —— 队列持有 `not_empty`、`not_full` 两个条件变量和一个 `Mutex<RingBuffer>`。

[commons/zenoh-sync/src/fifo_queue.rs:48-82](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-sync/src/fifo_queue.rs#L48-L82) —— `push`：满了就 `not_full.wait(guard).await`，放进去后 `not_empty.notify_one()`；`pull`：空了就 `not_empty.wait(guard).await`，取出来后 `not_full.notify_one()`。这就是上面流程图的真实代码。

#### 4.3.4 代码实践

**实践目标**：用 `Condition` 把一个普通 `Mutex<Vec>` 改造成「阻塞式有界队列」，体会 wait/notify 的配对。

由于 `zenoh-sync` 也是内部 crate，这里给出 **`event_listener` + `tokio::sync::Mutex` 的直接复刻**（`Condition` 本就是 `event_listener::Event` 的薄包装），可直接运行：

```rust
// Cargo.toml: tokio (full), event-listener
use std::sync::Arc;
use tokio::sync::Mutex;

struct BoundedQueue<T> {
    buf: Mutex<Vec<T>>,
    cap: usize,
    not_empty: event_listener::Event,
    not_full: event_listener::Event,
}

impl<T: Clone> BoundedQueue<T> {
    fn new(cap: usize) -> Self {
        Self { buf: Mutex::new(Vec::new()), cap,
               not_empty: event_listener::Event::new(),
               not_full: event_listener::Event::new() }
    }
    async fn push(&self, x: T) {
        loop {
            let g = self.buf.lock().await;
            if g.len() < self.cap { g.into_inner().push(x); break; }
            let ln = self.not_full.listen();
            drop(g);          // 关键：等待前释放锁
            ln.await;
        }
        self.not_empty.notify_additional_relaxed(1);
    }
    async fn pop(&self) -> T {
        loop {
            let g = self.buf.lock().await;
            if let Some(e) = g.into_inner().first().cloned() {
                // 简化：仅演示，真正 remove 会更严谨
                let mut g2 = self.buf.lock().await;
                let e = g2.remove(0);
                drop(g2);
                self.not_full.notify_additional_relaxed(1);
                return e;
            }
            let ln = self.not_empty.listen();
            drop(g);
            ln.await;
        }
    }
}

#[tokio::main(flavor = "multi_thread")]
async fn main() {
    let q = Arc::new(BoundedQueue::<i32>::new(2));
    let qp = q.clone();
    let producer = tokio::spawn(async move {
        for i in 0..5 { qp.push(i).await; println!("push {i}"); }
    });
    tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    for _ in 0..5 { println!("pop {}", q.pop().await); }
    producer.await.unwrap();
}
```

1. **操作步骤**：新建项目，依赖 `tokio`、`event-listener`，贴入运行。
2. **需要观察的现象**：`push` 会先连续打两条（队列容量 2），之后被 `not_full` 阻塞，直到消费者 `pop` 出一个并 `notify` 才继续。
3. **预期结果**：最终 0..5 全部 push 成功并被 pop 出来，顺序保持 FIFO。
4. **若无法运行**：标「待本地验证」，转而阅读 `fifo_queue.rs` 的真实实现并口述 push/pull 的 wait/notify 配对。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Condition::wait` 要先 `drop(guard)` 再 `listener.await`？不 drop 会怎样？
**参考答案**：`guard` 持有互斥锁，若持着锁再挂起，生产者拿不到锁就无法 `push`、也就无法 `notify_one`，于是消费者永远等不到唤醒——经典死锁。先释放锁，生产者才能改状态并通知。

**练习 2**：`Condition` 与同 crate 的 `Signal` 有何不同？
**参考答案**：`Condition` 可反复触发，每次 `notify_one` 只唤醒「当前正等」的监听器，不保存通知；`Signal` 是一次性的，`trigger` 后用一个置位标志 + 大量信号量许可，使**所有现在和将来**的 `wait` 都立刻返回，适合做「进程关停」这种一次性事件。

### 4.4 ZResult / Error：统一错误模型（zenoh-result）

#### 4.4.1 概念说明

Zenoh 是一个**可跨 `no_std` 环境**的协议栈（部分 crate 需能在嵌入式、无标准库环境编译）。标准库的 `std::error::Error` 和 `Box<dyn Error>` 在 `no_std` 下不可用，所以 Zenoh 自建了一套错误模型 `zenoh-result`：

- `Error`：`Box<dyn IError + Send + Sync + 'static>`，其中 `IError` 在 `std` 下就是 `std::error::Error`，在 `no_std` 下是自建的等价 trait。
- `ZResult<T>`：`Result<T, Error>` 的别名，是整个 Zenoh 代码库统一的返回类型。
- `ZError`：Zenoh 自己的错误结构体，额外携带「文件名、行号、负数 errno、可选 source 链」。
- `zerror!` / `bail!` / `to_zerror!`：构造与传播错误的宏。

这套模型让所有 crate 都能用同一个 `ZResult`，配合 `?` 运算符和 `Into<Error>` 转换，错误处理风格高度统一。

#### 4.4.2 核心流程

错误从「产生」到「展示」的链路：

```
zerror!("连接失败: {}", addr)            ← 宏：捕获 file!()/line!()
   │   构造 ZError { error, file, line, errno: -128, source: None }
   ▼
.into() → Box<dyn IError+Send+Sync>      ← 即 Error
   ▼
ZResult<T> = Result<T, Error>            ← 函数返回值
   │   ? 运算符逐层传播（任意 Into<Error> 都能转）
   ▼
Display：  "<msg> at <file>:<line>. - Caused by <source>"   ← 给人看
```

关键点：
- `zerror!(...)` 形如 `format!`，自动塞入当前 `file!()` 与 `line!()`。
- `zerror!(($errno) ...)` 可指定一个**负数** errno（用 `NegativeI8` 保证编译期为负）。
- `bail!(...)` 是 `return Err(zerror!(...).into())` 的简写。
- `ErrNo` trait 提供 `errno() -> NegativeI8`，便于程序化区分错误类型。

#### 4.4.3 源码精读

核心类型别名——一行定义了整个生态的返回类型：

[commons/zenoh-result/src/lib.rs:89-98](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-result/src/lib.rs#L89-L98) —— `IError` 在 `std`/`no_std` 下分别取 `std::error::Error` 与自建 trait；`Error = Box<dyn IError + Send + Sync + 'static>`；`ZResult<T> = Result<T, Error>`。

`NegativeI8`——编译期保证为负的「错误码」类型：

[commons/zenoh-result/src/lib.rs:104-119](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-result/src/lib.rs#L104-L119) —— `new` 在 `v >= 0` 时直接 panic，所以 errno 只能是负数；`MIN = i8::MIN = -128` 用作「未指定 errno」的默认值。

`ZError` 结构与构造：

[commons/zenoh-result/src/lib.rs:121-151](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-result/src/lib.rs#L121-L151) —— 持有 `anyhow::Error`（便于 `format!` 式构造）、`file`、`line`、`errno`、`source`；`set_source` 串成错误链。

`Display` 输出格式（`Debug` 也复用它，使日志友好）：

[commons/zenoh-result/src/lib.rs:165-179](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-result/src/lib.rs#L165-L179) —— 形如 `连接失败: 1.2.3.4 at net/runtime.rs:42. - Caused by ...`。

三个宏：

[commons/zenoh-result/src/lib.rs:303-338](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-result/src/lib.rs#L303-L338) —— `zerror!` 多种重载（带 errno、带 source、字面量、表达式、`format!` 风格）；`bail!` 即 `return Err(zerror!(...).into())`。

`ErrNo` trait 与对 `dyn Error` 的实现：

[commons/zenoh-result/src/lib.rs:222-246](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-result/src/lib.rs#L222-L246) —— 任意 `dyn Error` 都能尝试 downcast 成 `ZError` 取 errno，取不到就返回 `i8::MIN`。

> 印证：公开 API 层 `zenoh::Result` 就是它的别名，见 [`zenoh/src/lib.rs`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs) 中 `pub type Result<T> = ZResult<T>;` 的 re-export（在《u1-l4》已建立此认知）。

#### 4.4.4 代码实践

**实践目标**：体会 `ZResult` + `zerror!` + `?` 的统一错误处理风格。由于 `zenoh-result` 依赖 `anyhow`，下面用 `anyhow` + 自建别名复刻其**等价用法**（可直接运行）：

```rust
// Cargo.toml: anyhow
use anyhow::anyhow;
type Error = Box<dyn std::error::Error + Send + Sync + 'static>;
type ZResult<T> = Result<T, Error>;

fn parse_port(s: &str) -> ZResult<u16> {
    let n: u32 = s.parse().map_err(|e| anyhow!("端口不是数字: {e}"))?;
    if n > 65535 {
        // 对应 bail!(...)
        return Err(anyhow!("端口超出范围: {n}").into());
    }
    Ok(n as u16)
}

fn build_endpoint(host: &str, port: &str) -> ZResult<String> {
    let p = parse_port(port)?;            // ? 自动转换 Error
    Ok(format!("tcp/{host}:{p}"))
}

fn main() {
    match build_endpoint("127.0.0.1", "99999") {
        Ok(ep) => println!("endpoint = {ep}"),
        Err(e) => println!("失败: {e}"),    // 形如 失败: 端口超出范围: 99999
    }
}
```

1. **操作步骤**：新建项目，贴入运行；再分别把端口改成 `abc`、`7447` 各跑一次。
2. **需要观察的现象**：`99999` 报「端口超出范围」；`abc` 报「端口不是数字」；`7447` 输出 `tcp/127.0.0.1:7447`。
3. **预期结果**：所有错误经 `?` 层层传播，最终在 `main` 统一打印，无需手写错误类型。
4. **源码阅读补充**：在仓库里搜索 `zerror!(` 与 `bail!(`（如 `zenoh/src/net/runtime/` 下），观察真实代码如何用这两个宏构造错误并串接 source。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `Error` 用 `Box<dyn IError + Send + Sync>` 而不是某个具体的 enum？
**参考答案**：Zenoh 跨非常多 crate，各层有各自的错误来源（IO、配置、编解码、协议）；用 trait object 可以让任何实现了错误 trait 的类型统一装箱，配合 `Into<Error>` 让 `?` 自动转换，避免定义一个庞大无比的「全局错误 enum」并频繁 match。`Send + Sync` 约束则保证错误可跨线程传递（异步任务常跨线程）。

**练习 2**：`ZError` 里为什么存 `file` 和 `line`？
**参考答案**：`zerror!` 宏在调用点用 `file!()`/`line!()` 捕获出错位置，使最终 `Display` 输出形如「... at net/runtime.rs:42.」，便于在不带完整堆栈的 release 构建里快速定位错误来源。

## 5. 综合实践

把四个模块串起来，完成一个**「带优雅关停的后台任务组」**设计任务，模拟 Zenoh 路由器里一组后台 worker 的生命周期管理。

任务：假设你要写一个「数据采集器」，它有：① 一个 RX 任务周期产生数据；② 一个 worker 任务消费数据；③ 一个监控任务定期打印状态。三者都要能被统一优雅关停，且关停要带超时保护。

要求：

1. **选用执行器**：参照《4.1》，说明你会把这三个任务分别 spawn 到哪个 `ZRuntime`（提示：RX 类放 `RX`、消费放 `Net` 或 `Application`、监控放 `Application`），并解释为什么不全塞进一个执行器。
2. **关停编排**：参照《4.2》，用 `TaskController`（或其等价的 `TaskTracker`+`CancellationToken`）把三个任务包起来；选 `spawn` 还是 `spawn_abortable`？说明取舍（提示：消费任务若持有队列锁，宜用 `spawn` + 自检 token）。
3. **任务间同步**：参照《4.3》，RX 与 worker 之间用一个基于 `Condition` 的有界队列（可直接用 `FifoQueue` 的思路）做生产者-消费者；监控任务用一个 `Signal` 或周期 `sleep`。
4. **错误处理**：参照《4.4》，所有可能失败的步骤（建连、解析）返回 `ZResult`，用 `zerror!`/`?` 传播；在主流程用 `match` 区分正常结束与错误。
5. **交付**：写一段不少于 200 字的中文说明，讲清「任务在哪跑（执行器）→ 怎么互相同步（Condition）→ 出错怎么办（ZResult）→ 怎么关停（terminate_all 带超时）」四件事，并指出这套设计与 `zenoh-ext/src/group.rs` 的对应关系。

参考方向（不必实现，重在说清设计）：你的说明应能映射到 `Group` 持有 `task_controller: TaskController` + `cond: Condition` + 各 `spawn_abortable` 任务 + `Drop` 里 `terminate_all(10s)` 的真实结构（见 [zenoh-ext/src/group.rs:164-193](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh-ext/src/group.rs#L164-L193)）。

## 6. 本讲小结

- **`ZRuntime`** 把 Tokio 运行时按职责拆成 `Application/Acceptor/TX/RX/Net` 五个命名执行器，隔离故障域，保证收发包等快路径不被应用回调等慢路径阻塞；可用 `ZENOH_RUNTIME` 环境变量（RON）在启动时调参，`handover` 能合并执行器。
- **`TaskController`** = `TaskTracker` + `CancellationToken`，批量管理可取消任务；`terminate_all(timeout)` 是同步阻塞的关停入口，常放在 `Drop` 里；`TerminatableTask` 是单任务 RAII 版本。
- **`Condition`** 是基于 `event_listener` 的异步条件变量，配合 `Mutex` 实现「持锁检查→释放锁等待→被唤醒」的经典模式，`FifoQueue` 是其最佳范例；与一次性 `Signal` 互补。
- **`ZResult<T>` / `Error` / `zerror!`** 是跨 `no_std` 的统一错误模型，用 `Box<dyn IError + Send + Sync>` + `anyhow` 让全生态用同一个返回类型与 `?` 传播。
- 这四个 crate 是地基：执行器决定「在哪跑」，任务控制器决定「怎么停」，条件变量决定「怎么等」，结果类型决定「错了怎么办」——上面所有网络层/路由层讲义都跑在这套地基上。
- 阅读内部 crate 时要留意**派生宏**：`ZRuntime` 的 `init`/`iter`/`Borrow` 由 `zenoh_runtime_derive.rs` 生成，单看 `lib.rs` 会找不到定义。

## 7. 下一步学习建议

- 本讲是「内部架构（四）」的最后一篇基础讲义。建议回头重读《u7-l1 Session 内部与 Runtime》，用本讲的 `ZRuntime` 视角重新审视 `Runtime::start` 把哪些任务 spawn 到了哪个执行器。
- 接着进入第 11 单元「路由器与插件系统」：《u11-l1 zenohd》会用到本讲的 `TaskController`（如 `TransportManager::task_controller`）做关停；`zenohd` 的主循环退出也依赖这套机制。
- 想深入调优的读者，可阅读 `io/zenoh-transport/src/` 下 `universal/tx.rs`、`universal/rx.rs`、`lowlatency/*` 中所有 `ZRuntime::*..spawn` 调用点，画出「每条收发链路用了哪个执行器」的完整地图。
- 对 `no_std` 感兴趣的读者，可追踪 `zenoh-result` 在 `cfg(not(feature = "std"))` 下的 `IError` 自实现，理解 Zenoh 如何兼顾嵌入式与服务器两种场景。

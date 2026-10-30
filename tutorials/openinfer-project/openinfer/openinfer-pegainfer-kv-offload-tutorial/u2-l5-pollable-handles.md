# u2-l5 可轮询句柄 OffloadHandle：poll/wait/settle 与 drop 语义

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `OffloadHandle<T>` 的封装结构：一个 tokio oneshot 接收端包着一层 `Result<T, EngineError>`。
2. 区分三种「收账」方式 —— `poll`（调度器非阻塞 tick）、`wait`（同步线程阻塞）、`settle`（异步上下文 await）—— 各自服务哪种调用方。
3. 解释「drop 句柄 = 分离观察者，而不是取消 I/O」这条设计，以及它为什么让 `keep_alive` 的生命周期与句柄无关。
4. 会用 `in_flight()`（公开）与 `settled()`（crate 私有）写出**不依赖 GPU** 的句柄行为测试。

本讲只精读一个文件 [src/handle.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/handle.rs#L1-L98)（不到 100 行），但它是 save（u2-l6）、query/load（u2-l7）两条链路的共同收账口，先把它的语义吃透，后两讲会顺很多。

## 2. 前置知识

### 2.1 oneshot 通道：一次性的单值信箱

tokio 的 `oneshot::channel()` 返回一对 `(Sender, Receiver)`：

- 通道里**最多只有一个值**，`Sender::send` 只能成功一次；
- `Receiver` 取值有三种方式：`try_recv()`（非阻塞试探）、`blocking_recv()`（阻塞当前线程）、`.await`（异步等待）；
- 若 `Sender` 在发送前被 drop，`Receiver` 的任何取值方式都会得知「对方永远不会回话了」——这是 drop 语义能作为**信号**的基础。

`OffloadHandle` 就是把这个信箱的收件侧包装成了本 crate 的公共语言。

### 2.2 三层结果语义：`Option<Result<T, EngineError>>`

`poll()` 的返回类型是嵌套的两层，读代码时要拆开：

| 返回值 | 通道层含义 | 业务层含义 | 调用方动作 |
|---|---|---|---|
| `None` | 在途（`Empty`） | I/O 还没完成 | 保留句柄，下个 tick 再 poll |
| `Some(Ok(v))` | 已回话且成功 | I/O 完成，结果 `v` | 收账，丢弃句柄 |
| `Some(Err(e))` | 已回话但失败，或通道关闭 | I/O 失败 / worker 消失 | 按错误处理，丢弃句柄 |

初学者常见的误读是把 `None` 当成「失败」——恰恰相反，`None` 是这套设计里**最正常的态**：绝大多数 tick 里，句柄都在途。

### 2.3 调度器 tick 模型（承接 u1-l2）

pegainfer 的服务主循环是同步线程，每个 step 边界跑一次「清扫」：问一遍所有在途请求「你的 I/O 好了吗」。所以句柄必须支持**非阻塞试探**，绝不能在服务循环里 `await` 或死等——[src/handle.rs:1-7](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/handle.rs#L1-L7) 的模块文档把这条红线写在了最前面：*submit on the engine, then poll at step boundaries — never block the serving loop*（括号里的 #799/#802 是仓库历史上的 PR 编号，指向这条设计的来由）。

### 2.4 承接上一讲

u2-l4 讲了引擎构造与注册，末尾留了 `write_barrier` 这个字段。注意它存的是 `Vec<JoinHandle<()>>`（任务句柄）而**不是** `SaveHandle`——为什么引擎自己另存一份凭据，正是本讲 4.1 要回答的问题之一。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| [pegainfer-kv-offload/src/handle.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/handle.rs#L1-L98) | 本讲主角：`OffloadHandle<T>`、三个类型别名、`QueryHit`/`QueryOutcome`、两个测试构造器 |
| [pegainfer-kv-offload/src/engine.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L1-L505) | 句柄的**生产者**：`submit_save`/`submit_query`/`load` 在哪里创建 oneshot、在哪里 send |
| [pegainfer-kv-offload/src/lib.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/lib.rs#L27-L33) | 导出面：哪些句柄类型跨出 crate 边界 |
| [pegainfer-qwen3/src/executor.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L2624-L2651) | 句柄的**消费者**：qwen3 executor 每个 tick 怎么 poll、什么场合才 wait |

## 4. 核心概念与源码讲解

### 4.1 OffloadHandle\<T\>：oneshot 封装与 poll / wait / settle

#### 4.1.1 概念说明

`OffloadEngine` 的每个异步操作（save、query、load）都提交到 pegaflow 的 tokio runtime 上跑，但**提交动作本身立刻返回**。调用方拿到的是一根「回执」——`OffloadHandle`，将来凭它取结果。

这个抽象解决的问题是：**提交侧与观察侧的线程模型解耦**。提交发生在同步的调度器线程；执行发生在 tokio runtime 的工作线程；收账则可能有三种姿势（逐 tick 试探 / 同步阻塞 / 异步等待），取决于收账方自己是什么。一根句柄同时服务三种收账方，靠的就是 oneshot 接收端天然支持的三种取值方式。

#### 4.1.2 核心流程

```
提交侧（engine.rs，持有 offload runtime）        观察侧（调用方线程，任意时刻）
───────────────────────────────────────────     ─────────────────────────────────
let (tx, rx) = oneshot::channel();
runtime.spawn(async move {                       let mut h = OffloadHandle { rx }
    let r = 执行I/O(...).await;
    if tx.send(r) 被拒 → 记 warn 日志;            每个 tick:
    drop(keep_alive);   // 任务内部的事            h.poll() ─┬─ None      → 在途，留着下个 tick
});                                                        ├─ Some(Ok)  → 收账，丢弃句柄
                                                           └─ Some(Err) → 收账（失败）

收账的三种姿势：
  poll(&mut self)        非阻塞试探，可反复调用直到 Some   → 调度器 tick
  wait(self)             阻塞当前线程直到出结果            → 同步线程一次性收账
  settle(self).await     异步等待                          → 异步上下文
```

关键时序点：spawn 出去的任务**不依赖任何人的关注**就会跑完；句柄只是观察它的一个窗口。

#### 4.1.3 源码精读

**结构体定义** —— 整个抽象就包着一个字段（[src/handle.rs:17-19](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/handle.rs#L17-L19)）：

```rust
pub struct OffloadHandle<T> {
    rx: oneshot::Receiver<Result<T, EngineError>>,
}
```

`T` 是操作的载荷类型（save/load 是 `()`，query 是 `QueryOutcome`），错误统一用 pegaflow 的 `EngineError`（从 `lib.rs` 再导出，见 u1-l1）。

**poll：三态翻译机**（[src/handle.rs:38-47](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/handle.rs#L38-L47)）：

```rust
pub fn poll(&mut self) -> Option<Result<T, EngineError>> {
    match self.rx.try_recv() {
        Ok(result) => Some(result),
        Err(oneshot::error::TryRecvError::Empty) => None,
        Err(oneshot::error::TryRecvError::Closed) => Some(Err(EngineError::Storage(
            "offload worker dropped reply".into(),
        ))),
    }
}
```

- `Empty`（还没回话）→ `None`，即「在途」；
- `Ok`（回话了）→ 原样透传 `Some(result)`；
- `Closed`（发送端没回话就消失了）→ **翻译成一个错误**而不是继续装作在途。为什么？`Closed` 的现实来源是 worker 任务在回复前被取消——比如 u2-l3 讲过的 host drop 时的 runtime 关闭。此时「没有答案」是一个**既成事实**，若返回 `None`，调度器会永远等一个不会来的答案（空转）；返回 `Some(Err(...))` 则把事实显式化：这笔 I/O 作废，请走降级路径。这与全仓库「宁显式失败、不静默挂起」的取向一致。

注意 `poll` 只借 `&mut self`（`try_recv` 需要），**不消耗句柄**——同一根句柄要跨很多个 tick 反复试探。首次得到 `Some` 后调用方就应消费并丢弃它（4.1.4 会看 qwen3 怎么做）。

**wait：同步线程的终局收账**（[src/handle.rs:49-54](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/handle.rs#L49-L54)）：

```rust
pub fn wait(self) -> Result<T, EngineError> {
    self.rx
        .blocking_recv()
        .unwrap_or_else(|_| Err(EngineError::Storage("offload worker dropped reply".into())))
}
```

`blocking_recv` 会阻塞到出结果；同样把「通道关闭」翻译成同一个 `Storage` 错误。它**消耗 self**：取过值之后 oneshot 就完成了，句柄在语义上一次性。tokio 规定 `blocking_recv` 不能在异步上下文里调用（会 panic），所以 engine 的同步包装入口都先过一道 [assert_outside_runtime](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L50-L60)，把误用变成带方法名的清晰断言（debug 断言）而不是 tokio 的隐晦 panic。

**settle：异步上下文的对偶**（[src/handle.rs:56-62](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/handle.rs#L56-L62)）：`pub async fn settle(self) -> Result<T, EngineError>`，文档注释明说它是 poll/wait 之于「同步调度线程」那一对 dual，给异步调用方用。诚实说明：截至本讲 HEAD，用 `grep` 全仓库检索不到 `.settle(` 的调用点——它是对外预留的公开 API（本 crate 的下游目前都是同步调度器）。

**谁在 wait**：crate 内两个同步包装 [save_blocking](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L299-L306) 与 [query](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L381-L390) 都是 `submit_xxx(...).wait()` 一行收口；qwen3 里也有一个真正的生产调用方——[wait_ready_prefetch](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L2653-L2671) 在没有别的本地在途请求、可以让一个请求「插队等完」的场合直接 `handle.wait()`（L2669）。

**谁在 poll**：qwen3 executor 每个 step 的 [drain_ready_prefetch](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L2624-L2651)，核心三行：

```rust
let Some(result) = handle.poll() else { continue; };   // 在途 → 本 tick 跳过
// Some 到手后立刻 std::mem::replace 换相，句柄随之被消费、不再 poll
```

用 `let-else` 表达「没好就跳过」，好了一次性结清——这就是 poll 的标准用法。

**对比：write_barrier 为什么存 JoinHandle 而不是 SaveHandle**（[src/engine.rs:276-281](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L276-L281)）：`SaveHandle` 已经交给调用方，调用方随时可能 drop 它（fire-and-forget 的 `save` 甚至立刻 drop，见 [src/engine.rs:287-294](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L287-L294)）；引擎要能独立于调用方地等完所有 save（u2-l8 的 flush 屏障），就必须自己留一份凭据——spawn 返回的 `JoinHandle`。另外 `JoinHandle.await` 覆盖**整个任务**（含 `tx.send` 之后的 `drop(keep_alive)` 和失败日志），而 `SaveHandle` 只在 `tx.send` 那一刻就绪。两份凭据观察同一次 I/O 的不同切面：一份给调用方（「数据已捕获」），一份给引擎（「任务彻底跑完」）。

#### 4.1.4 代码实践

**实践目标**：不写代码，先把「谁生产句柄、谁消费句柄、用哪种方式消费」这条链走一遍，画出时序图。

**操作步骤**：

1. 在 `pegainfer-kv-offload/src/engine.rs` 里找 `oneshot::channel()` 的全部出现点（共三处：`submit_save` L260、`submit_query` L344、`flush_saves` L434——第三处是屏障完成信号，不是 `OffloadHandle`）。
2. 对 `submit_save`（[L246-L282](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L246-L282)）逐行回答：`tx.send` 在任务的哪个阶段执行？`keep_alive` 在 `tx.send` 之前还是之后 drop？`SaveHandle::from_rx(rx)` 返回给谁？
3. 在 `pegainfer-qwen3/src/executor.rs` 里检索 `.poll()` 与 `.wait()`（各一处：L2630、L2669），确认它们分别位于 `drain_ready_prefetch`（每 tick 扫描）和 `wait_ready_prefetch`（无本地在途时的阻塞等待）。
4. 画一张时序图：横轴为时间，两条竖线分别是「调度器线程」与「offload runtime worker」，标出 submit、spawn、D2H 执行、`tx.send`、poll 得到 None / Some 的相对顺序。

**需要观察的现象**：`tx.send(result)` 位于 D2H 完成之后、`drop(keep_alive)` 之前——即调用方 poll 到 `Some(Ok)` 时，数据拷贝已经落地（这正是 `SaveHandle` 文档注释的承诺：*the source GPU blocks are reusable from that point*）。

**预期结果**：时序图中 poll 的 `Some` 一定发生在 worker 线程 `tx.send` 之后；`None` 则可能出现在 submit 后的任意多个 tick 里。

#### 4.1.5 小练习与答案

**练习 1**：`wait` 和 `settle` 为什么按值消耗 `self`，而 `poll` 只需要 `&mut self`？

**答案**：`wait`/`settle` 是**终局**收账——oneshot 的值被取走后通道即完成，句柄在语义上一次性，按值拿走所有权正好让编译器阻止二次收账；`poll` 设计出来就是要跨很多个 tick 反复试探同一根句柄，必须可重复调用，所以只借 `&mut self`（`try_recv` 本身要求可变借用）。

**练习 2**：如果 tokio runtime 的工作线程在 `tx.send` 之前 panic（或被 host drop 时的 runtime 关闭取消），调用方分别在 `poll`/`wait`/`settle` 三条路径上会感知到什么？

**答案**：三条路径感知到的是同一事实的三种投影——oneshot 发送端消失、通道关闭。`poll` 把它映射为 `Some(Err(EngineError::Storage("offload worker dropped reply")))`；`wait`（`blocking_recv` 返回 `Err`）与 `settle`（`await` 返回 `Err`）经 `unwrap_or_else` 映射成同一个错误。没有一条路径会假装「还在途」。

**练习 3**：模块文档说「Dropping a handle detaches the observer, not the I/O」。结合 4.3.3 的 `submit_save` 源码，指出「I/O 继续跑」具体由哪几行代码保证。

**答案**：其一，任务由 `self.host.runtime.spawn(...)` 启动（[engine.rs:261](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L261)），其存活由 runtime 与存进 `write_barrier.pending_saves` 的 `JoinHandle` 保证，与 `rx` 的存亡无关；其二，`drop(keep_alive)` 在任务体内、`tx.send` 之后执行（[engine.rs:274](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L274)），pin 的释放同样不经过句柄。所以句柄 drop 只丢掉「观察结果的资格」，D2H 拷贝、pin 保全、失败日志三件事照常发生。

### 4.2 三个类型别名与 QueryHit / QueryOutcome

#### 4.2.1 概念说明

同一根 `OffloadHandle` 承载三种操作，差别只在载荷类型 `T`。于是用类型别名把「这根句柄等的是什么」写在类型上：

- `LoadHandle`：CPU→GPU 恢复，DMA 完成时兑现，载荷 `()`；
- `SaveHandle`：GPU→CPU 保存，host 层捕获完数据时兑现（此后源 GPU 块可复用）；
- `QueryHandle`：前缀查询，兑现为一个 `QueryOutcome`。

**可见性是刻意设计的**（[src/handle.rs:21-31](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/handle.rs#L21-L31)）：`LoadHandle` 是 `pub`，`SaveHandle`/`QueryHandle` 是 `pub(crate)`。理由看 [lib.rs 的导出面](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/lib.rs#L27-L30)就清楚——只有会**跨出 crate 边界**的句柄才公开：

| 类型 | 可见性 | 跨 crate 逃逸路径 |
|---|---|---|
| `LoadHandle` | `pub` | `pub fn load(...) -> Result<LoadHandle, _>`（[engine.rs:398-415](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L398-L415)）；qwen3 把它存进自己的预取状态机 |
| `SaveHandle` | `pub(crate)` | 公开的 `save` 返回 `()`、`save_blocking` 直接返回 `Result`，句柄从不逃逸 |
| `QueryHandle` | `pub(crate)` | 公开的 `query` 直接返回 `Result<QueryOutcome>`，句柄从不逃逸 |

qwen3 侧的真实证据：[executor.rs:1041-1045](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L1041-L1045) 的预取阶段枚举里，`Loading { reservation, handle: LoadHandle }` —— 一根 `LoadHandle` 要在请求状态机里**活过多个调度 tick**，所以它必须是公开类型；而 save/query 的句柄在 crate 内部当场 `wait` 结清，没必要暴露。

#### 4.2.2 核心流程

`QueryHandle` 的载荷 `QueryOutcome` 是一个两态枚举，其中一态**不是终态**：

```
submit_query(req_id, hashes)
        │
        ▼
  QueryOutcome::Ready(QueryHit)          ← 终态：本地 host 层已命中
        │   hit.num_blocks 块已用 lease 钉住
        │   （num_blocks == 0 ⇒ 全 miss，lease == None）
        ▼
  或 QueryOutcome::Loading               ← 非终态：pegaflow 正在后台
        │                                  从远端 peer / SSD 拉缺失前缀
        └── 下个 tick 用同一个 req_id 重新 submit_query 轮询
              └── 拉取结束 → Ready（带回拉到的块）
                  或退化 → Ready（普通本地命中计数）
```

u2-l7 会精读 lease 的完整生命周期，这里只需记住：`QueryHit` 里的 `lease` 是「命中块的所有权凭证」，`load` 会消费它。

#### 4.2.3 源码精读

**三个别名**（[src/handle.rs:21-31](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/handle.rs#L21-L31)）：

```rust
pub type LoadHandle = OffloadHandle<()>;                    // L22
pub(crate) type SaveHandle = OffloadHandle<()>;             // L26
pub(crate) type QueryHandle = OffloadHandle<QueryOutcome>;  // L31
```

**QueryHit**（[src/handle.rs:80-86](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/handle.rs#L80-L86)）：

```rust
pub struct QueryHit {
    pub lease: Option<QueryLeaseId>,
    pub num_blocks: usize,
}
```

文档注释明确：`num_blocks == 0` 表示全 miss，此时 `lease` 必为 `None`（没有命中块就无从钉起）。`QueryLeaseId` 是 pegaflow 的类型，经 [lib.rs:33](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/lib.rs#L33) 再导出，让下游不必直接依赖 pegaflow-core。

**QueryOutcome**（[src/handle.rs:88-98](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/handle.rs#L88-L98)）：`Ready(QueryHit)` 终态；`Loading` 非终态，注释写明两件事——(1) 它只在配置了更深层数据面（远端 peer 或 SSD）时出现，纯 host 内存部署永远见不到它；(2) 它的收账方式是**用同一个 `req_id` 重查**，而不是等同一根句柄。

还有一条与 drop 相关的契约写在 [engine.rs:325-327](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L325-L327) 的文档注释里：query 句柄若在兑现前被 drop，`Ready` 结果里的 lease 会吊着直到 TTL 过期——所以「每笔提交的 query 都 poll 到兑现为止」是调用方的纪律（对 abandoned `LoadHandle` 同理）。drop 句柄无害是有边界的：对 save 无害（见 4.3），对 query 有代价（lease 滞留）。

#### 4.2.4 代码实践

**实践目标**：用一张表格 + 一张状态图固化「可见性跟随逃逸路径」「Loading 非终态」两个认知。

**操作步骤**：

1. 打开 [src/lib.rs:24-34](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/lib.rs#L24-L34)，把 8 个自有导出 + 3 个再导出列成表，每行标注「服务于 save / query / load / P2P / 哈希」中的哪条链。
2. 画出 `QueryOutcome` 的状态图（4.2.2 的文字版转成图），标注：哪个态终态、哪个态需要带 `req_id` 重查、lease 在哪个态诞生、被谁消费。
3. 到 qwen3 的 [executor.rs:2143-2161](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L2143-L2161)（`start_prefetch_load`）看一眼 `LoadHandle` 的诞生现场：`offload.load(lease, page_ids)` 返回的句柄被存进哪个结构。

**需要观察的现象**：`start_prefetch_load` 里若 GPU 块预留失败，代码会先 `release_query_lease(lease)` 再放弃——印证 4.2.3 末尾的 lease 纪律。

**预期结果**：表格里 `LoadHandle`/`QueryOutcome`/`QueryHit`/`QueryLeaseId` 都标注到 load/query 链；`SaveHandle` 根本不出现在公开导出里。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `SaveHandle`/`QueryHandle` 是 `pub(crate)` 而 `LoadHandle` 是 `pub`？

**答案**：可见性跟随逃逸路径。`load` 是公开方法且返回 `LoadHandle`，qwen3 要把它存进 `PrefetchPhase::Loading`、跨多个 tick 持有，所以必须公开；save/query 的公开入口（`save`/`save_blocking`/`query`）直接返回 `()` 或 `Result<QueryOutcome>`，句柄在 crate 内部当场结清，从不逃逸，`pub(crate)` 足矣。

**练习 2**：`QueryOutcome::Loading` 为什么既不是错误也不是终态？它在什么部署下根本不会出现？

**答案**：`Loading` 表示 pegaflow 正在后台从远端 peer（P2P）或 SSD 拉取缺失前缀，是一段**进行中**的合法状态，正确的收账方式是带同一个 `req_id` 下个 tick 重查；只有配置了更深层数据面时才会出现，纯 host 内存部署永远只会有 `Ready`（[src/handle.rs:88-98](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/handle.rs#L88-L98)）。

**练习 3**：`QueryHit { num_blocks: 0, lease: Some(...) }` 这个组合可能出现在合法代码里吗？

**答案**：不可能。零命中就没有可钉住的块，文档注释（[src/handle.rs:80-83](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/handle.rs#L80-L83)）明确 `num_blocks == 0` 意为全 miss 且 `lease` 为 `None`；engine 的空 query 快速路径与零块分支也都构造 `lease: None`（[engine.rs:334-339](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L334-L339)、[L357-363](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L357-L363)）。

### 4.3 settled 与 in_flight：两个构造器（兼作引擎快速路径）

#### 4.3.1 概念说明

`OffloadHandle` 不一定要来自一次真实的异步 I/O。它有两个「凭空造句柄」的构造器：

- `settled(result)`：生来就已兑现的句柄——把一个**已知的结论**包装成句柄形态，让调用方的收账代码无需分支。它不只为测试服务：engine 的两个**空输入快速路径**在产品代码里用它短路。
- `in_flight()`：返回 `(句柄, Sender)` 对——把「何时兑现」的指挥棒交给测试，让测试在**自己的时间表**上结算。它是 `pub` 的，专为下游调度器测试准备（文档注释写明：*scheduler tests exercise the parked/in-flight path, then resolve it on their own schedule*）。诚实说明：用 `grep` 检索全仓库，`in_flight()` 在本 HEAD 没有树内调用方——它就是留给外部/未来测试的公开口。

两个构造器合起来回答一个问题：**怎么在没有 pegaflow worker、没有 GPU 的情况下测试调度器的收账逻辑**——`settled` 造「已完成」，`in_flight` 造「未完成、可任意时刻完成」，覆盖收账代码的两条分支。

#### 4.3.2 核心流程

`settled` 的内部动作（[src/handle.rs:64-70](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/handle.rs#L64-L70)）：

```
let (tx, rx) = oneshot::channel();
tx.send(result);          // 立即发送，值已进信箱
OffloadHandle { rx }      // 生来即兑现：首次 poll 一定得 Some(result)
```

`in_flight` 则只是把通道两端原样交出（[src/handle.rs:72-77](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/handle.rs#L72-L77)），谁都不 send，句柄停在 `None` 态，直到测试（或 drop sender）改变它。

engine 里的两个真实 `settled` 调用：

1. **空 save 短路**（[src/engine.rs:252-255](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L252-L255)）：`submit_save` 收到空块列表，不 spawn 任务，直接 `SaveHandle::settled(Ok(()))`——调用方首次 poll 就结清，省一次 runtime 调度。
2. **空 query 短路**（[src/engine.rs:334-339](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L334-L339)）：`submit_query` 收到空 hash 列表，直接返回 `Ready(QueryHit { lease: None, num_blocks: 0 })`——「没有可查的就等于全 miss」。

#### 4.3.3 源码精读

两个构造器并排看（[src/handle.rs:63-78](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/handle.rs#L63-L78)）：

```rust
/// A handle that is already settled with `result` — lets scheduler tests
/// drive their admission poll path without a pegaflow worker.
pub(crate) fn settled(result: Result<T, EngineError>) -> Self { ... }

/// A handle plus the sender that settles it — scheduler tests exercise
/// the parked/in-flight path, then resolve it on their own schedule.
pub fn in_flight() -> (Self, oneshot::Sender<Result<T, EngineError>>) { ... }
```

再对照 `submit_save` 里「真句柄」的诞生地（[src/engine.rs:260-281](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L260-L281)），看 drop 语义如何落到 `keep_alive` 上：

```rust
let (tx, rx) = oneshot::channel();
let handle = self.host.runtime.spawn(async move {
    let result = engine.batch_save_kv_blocks_from_ipc(...).await;
    if let Err(unobserved) = tx.send(result)   // 接收端已 drop？
    && let Err(e) = unobserved
    {
        log::warn!("pegaflow save failed (best-effort): {e}");  // 失败只能记日志
    }
    drop(keep_alive);   // ← pin 在任务里，不在句柄里
});
...
SaveHandle::from_rx(rx)
```

三个要点：

1. **`keep_alive` 被 move 进 spawn 的任务**，它的生命周期跟着**任务**走，不跟着句柄走。调用方 drop `SaveHandle`（fire-and-forget 的 `save` 在 [L293](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L287-L294) 里当场 drop）**不会**提前释放源块的 pin——pin 要等到 D2H 真正落地之后（REUSE CONTRACT，u2-l6 详讲）。
2. **接收端 drop 时的失败兜底**：`tx.send` 被拒说明没人观察这次 save 了，失败再不记日志就永远丢失——所以记 `warn`。这就是「drop 句柄 = 分离观察者，I/O 与错误处理继续」的完整含义：**数据和 pin 照常，只是没人看见结果**。
3. `drop(keep_alive)` 位于 `tx.send` **之后**：观察者 poll 到 `Some(Ok)` 时拷贝必然已落地；此刻 guard 可能晚几条指令才析构，但无害——guard 防的是「落地**之前**被复用」，落地之后晚放行只是保守了一瞬。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：写一个**不需要 GPU、不需要 pegaflow worker** 的行为测试，验证 poll 的三态翻译，尤其是 `TryRecvError::Closed → Some(Err(...))` 这个分支。

**操作步骤**：

1. 新建集成测试文件 `pegainfer-kv-offload/tests/handle_poll.rs`（示例代码，属于你自己的练习副本，不属于仓库源码）：

   ```rust
   use pegainfer_kv_offload::{EngineError, OffloadHandle};

   #[test]
   fn poll_is_none_while_in_flight_then_resolves() {
       let (mut handle, tx) = OffloadHandle::<()>::in_flight();
       // 还没人回话：在途 → None
       assert!(handle.poll().is_none());
       // "worker" 现在回话
       tx.send(Ok(())).unwrap();
       assert!(matches!(handle.poll(), Some(Ok(()))));
   }

   #[test]
   fn dropped_sender_maps_to_storage_error() {
       let (mut handle, tx) = OffloadHandle::<()>::in_flight();
       drop(tx); // worker 在回复前消失（如 host drop 时的 runtime 关闭）
       assert!(matches!(
           handle.poll(),
           Some(Err(EngineError::Storage(_)))
       ));
   }
   ```

2. 运行（纯 CPU 测试，按仓库惯例加 `--release`；首次编译需拉取 pegaflow-core git 依赖，见 u1-l3）：

   ```bash
   cargo test -p pegainfer-kv-offload --test handle_poll --release
   ```

3. （可选深入）`settled` 是 `pub(crate)`，外部测试够不到它；想连它一起测，可临时在 `src/handle.rs` 末尾追加一个内联测试模块（验证后还原或保留，由你决定；tokio 以 `features = ["full"]` 成为 crate 直接依赖，`#[tokio::test]` 可直接用）：

   ```rust
   #[cfg(test)]
   mod tests {
       use super::*;

       #[test]
       fn settled_is_born_with_the_answer() {
           let mut h: OffloadHandle<()> = OffloadHandle::settled(Ok(()));
           assert!(matches!(h.poll(), Some(Ok(()))));
       }

       #[tokio::test]
       async fn settle_awaits_the_reply() {
           let (h, tx) = OffloadHandle::<()>::in_flight();
           tx.send(Ok(())).unwrap();
           assert!(h.settle().await.is_ok());
       }
   }
   ```

4. 回答思考题（写在测试文件注释里）：`poll` 里 `TryRecvError::Closed` 分支为什么映射成错误而不是 `None`？

**需要观察的现象**：

- 第一个测试里，`send` 之前 poll 得 `None`，`send` 之后立即 poll 得 `Some(Ok(()))`（oneshot 投递是同步完成的，不需要等待）；
- 第二个测试里，drop sender 后第一次 poll 就得 `Some(Err(Storage(_)))`。

**预期结果**：两个测试全部通过；`cargo test ... -- --nocapture` 下无任何输出（没有 GPU 路径被触碰）。思考题参考答案：`Closed` 表示「回答永远不会来」（worker 被取消或通道耗尽），返回 `None` 会谎报在途、让调度器无限空转；`Some(Err)` 把「无答案」显式化为可降级的失败。本实践的运行结果**待本地验证**（本讲义撰写环境未执行编译）。

**附加观察（只阅读，不断言）**：首次 poll 得到 `Some` 后若**再** poll 一次同一个句柄会得到什么？oneshot 是单值通道，取走即耗尽，具体返回值取决于 tokio 的内部实现——这正是生产代码遵守「恰好收账一次」的原因：qwen3 在首次 `Some` 后立即 `std::mem::replace` 换相、消费句柄，从不二次 poll。若你想满足好奇，可以在练习副本里打印实测结果记录下来，但不要把断言写进测试。

#### 4.3.5 小练习与答案

**练习 1**：`submit_save` 对空块列表为什么返回 `settled(Ok(()))` 而不是照常 spawn 一个「什么都不做」的任务？

**答案**：空列表无事可做，结论此刻已知；`settled` 让调用方首次 poll 就结清，省掉一次 runtime 调度与一次跨线程交接，同时保证收账代码完全无分支（它根本不知道有快速路径存在）。

**练习 2**：`in_flight()` 为什么必须把 `oneshot::Sender` 一起还给调用方，而不是只给句柄？

**答案**：句柄只包了通道的接收端；「何时兑现、兑现成什么」的控制权在发送端。测试要按自己的时间表先观察 `None`、再触发 `Some`，就必须同时拿到发送端——这就是返回 `(Self, Sender)` 元组的原因。

**练习 3**：fire-and-forget 的 `save()`（[engine.rs:287-294](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L287-L294)）拿到 `SaveHandle` 后立刻 drop。源 GPU 块的 pin（`keep_alive`）会因此提前释放吗？save 失败时错误去哪了？

**答案**：不会。`keep_alive` 被 move 进 spawn 出的任务，任务会跑到 D2H 落地之后才 `drop(keep_alive)`，与句柄存亡无关（[engine.rs:261-274](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L261-L274)）。失败时 `tx.send` 因接收端已 drop 而被拒，代码随即 `log::warn!("pegaflow save failed (best-effort): ...")`——错误降级为日志，这正是 fire-and-forget 的契约：丢一次 save 只损未来的命中率，不损正确性（承接 u1-l2）。

## 5. 综合实践

**任务：产出一份《OffloadHandle 收账规格》**，把本讲三块内容串起来。

1. **跑通行为测试**：完成 4.3.4 的 `tests/handle_poll.rs`（两用例通过），若愿意再补 4.3.4 第 3 步的内联 `settled`/`settle` 测试。
2. **补第三态**：给 `handle_poll.rs` 增加一个用例——用 `in_flight()` 造句柄，`tx.send(Err(EngineError::Storage("mock".into())))`，断言 poll 得到 `Some(Err(_))`。这验证「错误也能作为答案进信箱」，与「通道关闭产生的错误」是两个不同来源。
3. **整理规格表**：以「方法 × 调用方线程模型 × 阻塞性 × 消耗方式 × 失败语义」为列，写 poll/wait/settle 三行的对照表；再单列一行「drop 句柄」，注明它对 I/O、`keep_alive`、失败日志、query lease 各自的影响（前三个无影响，最后一个有代价——lease 滞留到 TTL）。
4. **回接生产代码**：在表中每个方法旁边标注它的真实生产调用点（poll → qwen3 `drain_ready_prefetch` 的每 tick 扫描；wait → `save_blocking`/`query`/`wait_ready_prefetch`；settle → 暂无树内调用方，预留）。

验收标准：测试全绿；规格表能让一个没读过 handle.rs 的同事正确回答「调度器 tick 里拿到 None 该怎么办」「fire-and-forget 的 save 失败了谁会知道」这两个问题。

## 6. 本讲小结

- `OffloadHandle<T>` = `oneshot::Receiver<Result<T, EngineError>>` 的一层包装，是 save/query/load 三条链路统一的收账口；`poll` 的 `None` 表示在途，是常态而非错误。
- 三种收账方式对应三类调用方：`poll`（`&mut self`，非阻塞、可反复）服务调度器 tick；`wait`（消耗 self，阻塞）服务同步线程的一次性结账；`settle`（异步）是给异步上下文预留的对偶，当前树内无调用方。
- drop 句柄 = 分离观察者：spawn 出的任务、`keep_alive` 的 pin、失败时的 `warn` 日志都不经过句柄，照常执行到 D2H 落地；但 query 句柄提前 drop 会让 `Ready` 的 lease 滞留到 TTL。
- `poll` 把 `TryRecvError::Closed` 显式翻译成 `Storage` 错误——「没有答案」必须上报，不能谎报在途。
- 可见性跟随逃逸路径：只有 `LoadHandle` 公开（qwen3 要跨 tick 持有），`SaveHandle`/`QueryHandle` 都在 crate 内当场结清。
- `settled`/`in_flight` 两个构造器让无 GPU、无 worker 的句柄行为测试成为可能；`settled` 还承担 engine 的两个空输入快速路径（空 save / 空 query）。

## 7. 下一步学习建议

下一讲 **u2-l6（save 链路）** 将沿着本讲的 `submit_save` 展开：`build_saves` 的跨层扇出、ORDERING CONTRACT（只能保存封块后的块）与 REUSE CONTRACT（`keep_alive` 持 pin 到 D2H 落地）——4.3.3 里那几行「pin 不在句柄里」的代码会在那里得到完整论证。之后 **u2-l7（query/load/lease）** 精读 `QueryOutcome::Loading` 的重查循环与 lease 的完整生命周期。

建议在进入 u2-l6 前做的源码热身：

- 重读 [src/engine.rs:246-306](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L246-L306)，找出 `save`、`save_blocking`、`submit_save` 三者对 `SaveHandle` 的三种处置（立刻 drop / wait / 存进屏障）。
- 对照 qwen3 的 [drain_ready_prefetch](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L2624-L2651)，体会「poll 到 Some 后 `std::mem::replace` 换相、恰好收账一次」的调度器模式——u3-l3 会从状态机角度再次回到这里。

# 协程、线程池与后台任务

## 1. 本讲目标

3FS 是一个高并发、重 IO 的分布式系统：一个 storage 节点要同时处理成千上万的 `batchRead` / `update` RPC，一个 mgmtd 节点要周期性地发心跳、续租约、推进路由版本号。如果用「一个连接一个线程」的传统模型，线程数会被打爆，上下文切换开销吃掉吞吐。3FS 的解法是全面拥抱 **C++20 协程（coroutine）**。

本讲是公共基础设施单元（u2）的第三篇，承接 [u2-l1 服务骨架](u2-l1-service-skeleton.md) 与 [u2-l2 RPC 与序列化](u2-l2-rpc-and-serde.md)。读完本讲你应当能够：

- 看懂 `CoTask<T>` / `CoTryTask<T>` 这两个随处可见的类型别名，理解协程如何挂起与恢复、如何被取消。
- 用 `CoroSynchronized<T>` 保护跨协程共享的数据。
- 区分 `CPUExecutorGroup`、`DynamicCoroutinesPool`、`PriorityCoroutinePool` 三类执行池各自的适用场景。
- 读懂 `BackgroundRunner` 如何驱动「周期性后台任务」，并理解它优雅停止的机制。
- 在真实服务代码（storage / mgmtd / meta / client）中定位上述设施的实际用法。

## 2. 前置知识

- **什么是协程**：普通函数只能「调用 → 执行完 → 返回」，中途不能暂停。协程（coroutine）可以在执行到 `co_await` 时把自己**挂起**（保存现场、让出线程），等条件满足再**恢复**继续执行。这样几千个正在等网络/磁盘的协程可以共用十几个线程，谁就绪谁跑，线程几乎不空转。
- **C++20 协程与 folly**：标准库只给了协程的「编译器机制」，没给现成的任务类型。3FS 直接复用 Facebook 的 **folly** 库（`folly/experimental/coro`）提供的 `Task`、`SharedMutex`、`sleep` 等现成组件，自己只包了薄薄一层别名与封装。
- **执行器（Executor）**：协程恢复时需要在一个线程上跑，这个「在哪跑」由 Executor 决定。3FS 用 `folly::CPUThreadPoolExecutor` 作为底层线程池。
- **协作式取消（cooperative cancellation）**：协程不会被打断，需要自己主动检查「取消令牌」。3FS 用 `CancellationToken` 在关闭流程里通知所有协程「该退出了」。
- 建议先读过 [u2-l1](u2-l1-service-skeleton.md)（`beforeStart` / `afterStart` 生命周期钩子）和 [u2-l2](u2-l2-rpc-and-serde.md)（RPC 的 `sendAsync` 挂起协程等回包），本讲讲的就是这些协程**跑在哪里、谁在调度它们**。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/common/utils/Coroutine.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/Coroutine.h) | 协程原语：`CoTask` / `CoTryTask` 别名、`IsCoTask` 特征萃取、取消相关类型。 |
| [src/common/utils/CoroSynchronized.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/CoroSynchronized.h) | 协程友好的共享互斥包装器，保护跨协程共享对象。 |
| [src/common/utils/CPUExecutorGroup.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/CPUExecutorGroup.h) / [.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/CPUExecutorGroup.cc) | 一组 `CPUThreadPoolExecutor`，支持多种任务派发策略（共享队列 / work-stealing / 轮询等）。 |
| [src/common/utils/DynamicCoroutinesPool.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/DynamicCoroutinesPool.h) / [.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/DynamicCoroutinesPool.cc) | 「N 个常驻协程 + 有界任务队列」的协程池，协程数可热更新。 |
| [src/common/utils/PriorityCoroutinePool.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/PriorityCoroutinePool.h) | 带优先级的协程池，job 自带优先级。 |
| [src/common/utils/BackgroundRunner.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/BackgroundRunner.h) / [.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/BackgroundRunner.cc) | 周期性/一次性后台协程任务调度器，带协作取消与优雅停止。 |
| [src/storage/service/Components.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/Components.h) / [StorageServer.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageServer.cc) | 真实用法：storage 按 RPC methodId 路由到不同 `DynamicCoroutinesPool`。 |
| [src/mgmtd/background/MgmtdBackgroundRunner.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdBackgroundRunner.cc) / [src/client/mgmtd/MgmtdClient.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/mgmtd/MgmtdClient.cc) | 真实用法：mgmtd / client 的周期性后台任务。 |
| [src/mgmtd/service/MgmtdState.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/MgmtdState.h) | 真实用法：`CoroSynchronized` 保护共享路由数据、`CoTryTask` 操作签名。 |
| [src/meta/components/GcManager.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/GcManager.cc) | 真实用法：`PriorityCoroutinePool` + `BackgroundRunner` 配合做 GC。 |

## 4. 核心概念与源码讲解

### 4.1 协程原语：CoTask / CoTryTask 与 CoroSynchronized

#### 4.1.1 概念说明

3FS 把 folly 的协程类型重新起了两个短名字，让全项目书写统一：

- **`CoTask<T>`** 就是一段「返回 `T` 的异步计算」。它就是 `folly::coro::Task<T>` 的别名。函数体里出现 `co_await` / `co_return`，它就是协程。它**懒执行**——你拿到一个 `CoTask` 时它还没开始跑，必须 `.scheduleOn(&executor).start()` 才真正调度。
- **`CoTryTask<T>`** 是 3FS 自己的约定：协程的返回值不是 `T`，而是 `Result<T>`（成功/错误的带状态结果），对 `void` 特化为 `Result<Void>`。这样 RPC 失败、FDB 冲突等错误**用返回值而非异常**传递，调用方用 `RETURN_ON_ERROR` / `co_await co_awaitTry` 处理，链路里不会到处 `try/catch`。这是 3FS 错误处理的主基调。

**为什么用协程而非裸线程？** 一个 storage 节点可能同时有数万个 `batchRead` 在等 RDMA 回包、等 AIO 落盘。若每个请求占一个 OS 线程，光是线程栈就要几十 GB，上下文切换更是灾难。协程在 `co_await` 时几乎零成本地挂起（只保存寄存器与局部变量到堆上的协程帧），让出线程给别人；就绪后再恢复。于是「几万并发 + 十几线程」成为可能。

**取消（cancellation）**：协程不会被强行打断，关闭时要靠协作。3FS 暴露了 folly 的三个类型：`CancellationToken`（被动观察「是否被要求取消」）、`CancellationSource`（主动发起取消）、`OperationCancelled`（取消时抛出的异常类型）。服务停止时发一个取消信号，各个在 `co_await` 上的协程就会收到并优雅退出。

#### 4.1.2 核心流程

定义并启动一个协程的典型写法（伪代码）：

```cpp
// 1) 定义：返回 CoTask<T>，体内用 co_await/co_return
CoTask<int> fetchValue() {
  auto v = co_await someAsyncIO();   // 挂起，让出线程
  co_return v + 1;                   // 恢复后返回
}

// 2) 启动：指定在哪个 executor 上跑，再 start() 取 future
auto fut = fetchValue().scheduleOn(&executor).start();
int result = std::move(fut).get();
```

错误传播用 `CoTryTask` + `Result`（伪代码）：

```cpp
CoTryTask<void> doOp() {
  auto r = co_await riskyStep();     // riskyStep 返回 CoTryTask<X>
  RETURN_ON_ERROR(r);                // 失败则 co_return 错误，不抛异常
  co_return Void{};                  // 成功
}
```

异常与取消的处理用 folly 的 `co_awaitTry`（把异常转成 `Try` 对象，便于 `co_await`）和 `co_withCancellation`（给内部协程附加一个取消令牌）。

#### 4.1.3 源码精读

协程别名本体极简——`Coroutine.h` 全文就是把 folly 的类型包了一层：

```cpp
// src/common/utils/Coroutine.h:L10-L14
template <typename T>
using CoTask = folly::coro::Task<T>;

template <typename T>
using CoTryTask = CoTask<std::conditional_t<std::is_void_v<T>, hf3fs::Result<Void>, hf3fs::Result<T>>>;
```

[Coroutine.h:L10-L14](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/Coroutine.h#L10-L14) 定义了两个核心别名：`CoTask<T>` 直接复用 folly，`CoTryTask<T>` 把返回类型包成 `Result<T>`（`void` 时为 `Result<Void>`），这就是「错误用返回值传递」的来源。

取消相关类型同样只是别名：

```cpp
// src/common/utils/Coroutine.h:L35-L37
using CancellationToken = folly::CancellationToken;
using CancellationSource = folly::CancellationSource;
using OperationCancelled = folly::OperationCancelled;
```

[Coroutine.h:L35-L37](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/Coroutine.h#L35-L37) 暴露取消三件套，所有「优雅停止」逻辑都建立在这三个类型上。

真实服务里的签名随处可见，例如 mgmtd 的状态对象同时用到了 `CoTryTask`、`CoTask` 与协程锁：

```cpp
// src/mgmtd/service/MgmtdState.h:L27-L28
CoTryTask<void> validateAdmin(const core::ServiceOperation &ctx, const flat::UserInfo &userInfo);
CoTask<std::optional<flat::MgmtdLeaseInfo>> currentLease(UtcTime now);
```

[MgmtdState.h:L27-L28](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/MgmtdState.h#L27-L28) 是一对典型对比：`validateAdmin` 可能因鉴权失败而返回错误，故用 `CoTryTask<void>`；`currentLease` 只是查当前租约（用 `optional` 表示「无」），不涉及错误传播，用普通 `CoTask<optional<...>>`。

**协程友好的共享互斥**：当多个协程要读写同一份数据时，不能用 `std::mutex`（那会阻塞整个线程，把同线程上的其它协程一起卡死）。`CoroSynchronized<T>` 用 `folly::coro::SharedMutex`（读写锁）把对象包起来，加锁动作本身是 `co_await`，挂起而非阻塞：

```cpp
// src/common/utils/CoroSynchronized.h:L42-L50
CoTask<SharedLockPtr> coSharedLock() {
  auto lock = co_await sharedMu_.co_scoped_lock_shared();   // 协程读锁
  co_return SharedLockPtr(std::move(lock), obj_);
}

CoTask<LockPtr> coLock() {
  auto lock = co_await sharedMu_.co_scoped_lock();          // 协程写锁
  co_return LockPtr(std::move(lock), obj_);
}
```

[CoroSynchronized.h:L42-L50](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/CoroSynchronized.h#L42-L50) 提供两个加锁协程：`coSharedLock()` 返回一个**读锁守卫**（`co_await` 它才能拿到，守卫析构时自动释放锁），`coLock()` 返回**写锁守卫**。守卫重载了 `->` 和 `*`，拿到后就能像指针一样访问被保护的 `obj_`。

mgmtd 正是用它保护全局路由数据与会话表：

```cpp
// src/mgmtd/service/MgmtdState.h:L60-L61
CoroSynchronized<MgmtdData> data_;
CoroSynchronized<ClientSessionMap> clientSessionMap_;
```

[MgmtdState.h:L60-L61](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/MgmtdState.h#L60-L61) 把核心的 `MgmtdData`（含 node/chain/chainTable/target 等路由信息）和客户端会话表都放进 `CoroSynchronized`，这样大量只读的 `GetRoutingInfo` 请求可以共享读锁并发，少数写操作才拿写锁。

> 补充：同一文件里还有一把单独的 `folly::coro::Mutex writerMu_`，配合 `coScopedLock()` 形成「写操作全程持锁」的粗粒度保护（[MgmtdState.h:L54-L58](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/MgmtdState.h#L54-L58)），与 `CoroSynchronized` 的细粒度读写锁形成互补。这告诉我们：3FS 里加锁也分层次，按场景选不同的协程锁。

#### 4.1.4 代码实践

**实践目标**：在真实代码里分辨 `CoTask` 与 `CoTryTask`，体会「错误用返回值传递」。

1. 打开 [src/mgmtd/service/MgmtdState.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/MgmtdState.h)。
2. 找到 `validateAdmin`（`CoTryTask<void>`）与 `currentLease`（`CoTask<optional<...>>`）这两个声明（L27–L28）。
3. 用 `Grep` 在 `src/mgmtd` 下搜索这两个函数的实现，观察：
   - `validateAdmin` 的实现里是否出现 `co_return` 一个错误（如 `Status`/`Result`），而**没有** `throw`。
   - `currentLease` 的实现里 `co_return` 的是否是「值或 `std::nullopt`」，与错误无关。
4. **需要观察的现象**：`CoTryTask` 路径的失败是「正常返回值」，调用方靠检查返回值分支；`CoTask` 路径要么成功要么抛异常。
5. **预期结果**：你能用一句话总结「何时该用 `CoTryTask`、何时用 `CoTask`」——会失败、且失败需要被上层当普通结果处理的操作用前者。

### 4.2 执行池：CPUExecutorGroup / DynamicCoroutinesPool / PriorityCoroutinePool

#### 4.2.1 概念说明

协程必须**跑在某个 Executor（线程池）上**。3FS 提供了三个层次的池，职责不同：

- **`CPUExecutorGroup`**：最底层的「一组 `CPUThreadPoolExecutor`」。它不直接理解协程任务，只提供线程与任务队列。核心变量是**派发策略** `ExecutorStrategy`：
  - `SHARED_QUEUE`：所有线程共用一个队列（退化为单个 `CPUThreadPoolExecutor`）。
  - `SHARED_NOTHING`：每个线程一个独立队列，任务进来固定分给某个线程。
  - `WORK_STEALING`：每个线程独立队列，空闲时去别人队列「偷」任务。
  - `ROUND_ROBIN`：多个独立队列，按轮询分配（**默认策略**，默认 32 线程）。
  - `GROUP_WAITING_4` / `GROUP_WAITING_8`：线程按 4/8 个一组共享一个无界队列。
  
  它还提供三种挑选线程的方式：`pickNext()`（原子轮询）、`randomPick()`（随机）、`pickNextFree()`（探测 4 个队列选最闲的，负载均衡）。

- **`DynamicCoroutinesPool`**：「**N 个常驻协程 + 一个有界任务队列**」的生产者-消费者模型。生产者往队列塞 `CoTask<void>`，池里固定数量的协程不断取出并 `co_await` 执行。特点是 `threads_num`（底层线程数）和 `coroutines_num`（常驻协程数）**都可热更新**——运行期改配置即可扩缩容，不必重启。

- **`PriorityCoroutinePool<Job>`**：与 `DynamicCoroutinesPool` 思路一致，但任务队列是**带优先级**的 `PriorityUnboundedQueue`，每个 `Job` 提交时带一个优先级，高优先级先被消费。注意头文件里写着 `// todo: support hot update coroutine numbers`，即它的协程数热更新尚未完全支持。

一句话区分：`CPUExecutorGroup` 是「**线程资源**」，`DynamicCoroutinesPool` 是「**协程资源 + 任务队列**」，`PriorityCoroutinePool` 在后者基础上加了「**优先级**」。

#### 4.2.2 核心流程

**CPUExecutorGroup** 的构造按策略创建若干 `CPUThreadPoolExecutor`：

```text
构造(threadCount, name, strategy):
  switch strategy:
    SHARED_QUEUE   -> 1 个 executor，threadCount 个线程共用队列
    SHARED_NOTHING -> threadCount 个 executor，各 1 线程，独立队列
    WORK_STEALING  -> threadCount 个 executor，各 1 线程，可互相偷任务
    ROUND_ROBIN    -> threadCount 个 executor，各 1 线程，轮询入队（默认）
    GROUP_WAITING_N-> threadCount/N 个 executor，各 N 线程，组内无界队列
挑线程: pickNext() 原子自增取模；pickNextFree() 探测 4 个取最闲
```

**DynamicCoroutinesPool** 是生产者-消费者：

```text
start(): setCoroutinesNum(N)
setCoroutinesNum(N):
  若 N > 当前: 循环 N 次，启动 run() 协程(挂在 executor_ 上)
  若 N < 当前: 循环(当前-N)次，往队列塞 nullptr 作为「退出哨兵」
run():                        # 每个常驻协程的循环体
  loop:
    task = co_await queue_.co_dequeue()
    if task == nullptr: co_return      # 收到哨兵，退出
    co_await *task                      # 执行用户任务
enqueue(task): queue_.enqueue(task)    # 生产者接口
```

**PriorityCoroutinePool** 与之类似，区别在 `enqueue(job, priority)` 带优先级、消费协程 `run()` 从 `PriorityUnboundedQueue` 取 job 再 `co_await handler(job)`。

#### 4.2.3 源码精读

`CPUExecutorGroup` 的策略枚举与默认配置：

```cpp
// src/common/utils/CPUExecutorGroup.h:L11-L23
enum class ExecutorStrategy {
  SHARED_QUEUE,  // fallback to CPUThreadPoolExecutor
  SHARED_NOTHING,
  WORK_STEALING,
  ROUND_ROBIN,
  GROUP_WAITING_4,
  GROUP_WAITING_8,
};

struct Config : public ConfigBase<Config> {
  CONFIG_ITEM(threadCount, 32);
  CONFIG_ITEM(strategy, ExecutorStrategy::ROUND_ROBIN);
};
```

[CPUExecutorGroup.h:L11-L23](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/CPUExecutorGroup.h#L11-L23) 说明默认 32 线程、默认 `ROUND_ROBIN` 策略；`.cc` 里的构造函数据此 switch 创建底层 executor（[CPUExecutorGroup.cc:L57-L82](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/CPUExecutorGroup.cc#L57-L82)）。

负载均衡挑选 `pickNextFree` 探测 4 个队列取最闲：

```cpp
// src/common/utils/CPUExecutorGroup.cc:L91-L108（节选）
auto start = next_.fetch_add(1, std::memory_order_acq_rel);
auto probeSize = std::min(size(), 4UL);
auto minPos = size() + 1;
auto minSize = std::numeric_limits<size_t>::max();
for (size_t i = 0; i < probeSize; ++i) {
  auto pos = (start + i) % size();
  auto queueSize = get(pos).getTaskQueueSize();
  if (queueSize < minSize) { minSize = queueSize; minPos = pos; }
}
return get(minPos);
```

[CPUExecutorGroup.cc:L91-L108](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/CPUExecutorGroup.cc#L91-L108) 是「探测式负载均衡」：以原子轮询的起点为基准，连续看 4 个队列的积压任务数，选最短的那个，避免把任务堆给某个繁忙线程。

`DynamicCoroutinesPool` 的配置项——注意 `threads_num` 和 `coroutines_num` 都是 `CONFIG_HOT_UPDATED_ITEM`（可热更新）：

```cpp
// src/common/utils/DynamicCoroutinesPool.h:L16-L20
class Config : public ConfigBase<Config> {
  CONFIG_ITEM(queue_size, 1024u);
  CONFIG_HOT_UPDATED_ITEM(threads_num, 8ul, ConfigCheckers::checkPositive);
  CONFIG_HOT_UPDATED_ITEM(coroutines_num, 64u, ConfigCheckers::checkPositive);
};
```

[DynamicCoroutinesPool.h:L16-L20](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/DynamicCoroutinesPool.h#L16-L20) 给出默认：队列容量 1024、8 个底层线程、64 个常驻协程。生产者接口只有一行：

```cpp
// src/common/utils/DynamicCoroutinesPool.h:L30
void enqueue(CoTask<void> &&task) { queue_.enqueue(std::make_unique<CoTask<void>>(std::move(task))); }
```

[DynamicCoroutinesPool.h:L30](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/DynamicCoroutinesPool.h#L30) 是唯一的提交入口——把一个 `CoTask<void>` 塞进有界队列，由池内常驻协程消费。

消费协程的循环体与「哨兵退出」机制：

```cpp
// src/common/utils/DynamicCoroutinesPool.cc:L67-L82
CoTask<void> DynamicCoroutinesPool::run() {
  SCOPE_EXIT { afterCoroutineStop(); };
  while (true) {
    auto task = co_await queue_.co_dequeue();
    if (task == nullptr) { co_return; }            // 收到 nullptr 哨兵，退出
    auto result = co_await folly::coro::co_awaitTry(std::move(*task));
    if (UNLIKELY(result.hasException())) {
      XLOGF(FATAL, "DynamicCoroutinesPool has exception: {}", result.exception().what());
      co_return;
    }
  }
}
```

[DynamicCoroutinesPool.cc:L67-L82](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/DynamicCoroutinesPool.cc#L67-L82) 展示了池协程的本质：无限循环取任务、`co_await` 执行。`nullptr` 是缩容时的退出哨兵（见 `setCoroutinesNum` 的 else 分支 [L59-L63](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/DynamicCoroutinesPool.cc#L59-L63) 往队列塞 nullptr）。注意：任务里抛出**未捕获**异常会被判为 `FATAL`，所以提交进池的任务要自己处理好错误（这正是 `CoTryTask` 的用武之地）。

`PriorityCoroutinePool` 的启动与提交（结构清晰，值得对照）：

```cpp
// src/common/utils/PriorityCoroutinePool.h:L40-L46
Result<Void> start(Handler handler, CPUExecutorGroup &grp) {
  auto coroutines = config_.coroutines_num();
  for (auto i = 0u; i < coroutines; ++i) {
    futures_.push_back(run(handler, i).scheduleOn(&grp.pickNext()).start());   // 均匀散布到 grp
  }
  return Void{};
}
```

```cpp
// src/common/utils/PriorityCoroutinePool.h:L61
void enqueue(Job job, int8_t priority) { queue_.addWithPriority(std::move(job), priority); }
```

[PriorityCoroutinePool.h:L40-L46](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/PriorityCoroutinePool.h#L40-L46) 启动时把 `coroutines_num` 个消费协程**均匀散布**到一个 `CPUExecutorGroup` 上（`grp.pickNext()`）；[L61](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/PriorityCoroutinePool.h#L61) 的 `enqueue` 则带优先级入队。

**真实用法：storage 按 RPC 类型路由到不同协程池**。这是全项目对 `DynamicCoroutinesPool` 最重要的使用：

```cpp
// src/storage/service/Components.h:L80-L92
inline DynamicCoroutinesPool &getCoroutinesPool(uint16_t methodId) {
  if (LIKELY(config.use_coroutines_pool_read()) && methodId == StorageSerde<>::batchReadMethodId) {
    return readPool;
  }
  if (LIKELY(config.use_coroutines_pool_update()) &&
      (methodId == StorageSerde<>::writeMethodId || methodId == StorageSerde<>::updateMethodId)) {
    return updatePool;
  }
  if (methodId == StorageSerde<>::syncStartMethodId || methodId == StorageSerde<>::getAllChunkMetadataMethodId) {
    return syncPool;
  }
  return defaultPool;
}
```

[Components.h:L80-L92](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/Components.h#L80-L92) 是「**按 RPC methodId 把请求分流到隔离的协程池**」的核心：读请求（`batchRead`）进 `readPool`、写请求（`write`/`update`）进 `updatePool`、同步/元数据请求进 `syncPool`，其余进 `defaultPool`。这四个池（[L115-L118](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/Components.h#L115-L118)）彼此隔离，**避免一类慢请求把另一类饿死**——例如大量慢写不会堵死读。

把 methodId 映射到池的「钩子」注册在服务启动时：

```cpp
// src/storage/service/StorageServer.cc:L29-L36
groups().front()->setCoroutinesPoolGetter([this](const serde::MessagePacket<> &packet) -> DynamicCoroutinesPool & {
  switch (packet.serviceId) {
    case StorageSerde<>::kServiceID:
      return components_.getCoroutinesPool(packet.methodId);   // 按 methodId 选池
    default:
      return components_.defaultPool;
  }
});
```

[StorageServer.cc:L29-L36](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageServer.cc#L29-L36) 把上面那个映射注册成 `setCoroutinesPoolGetter` 回调——每来一个 RPC 包，框架就用它决定「这个请求该丢进哪个协程池」。这正是 [u2-l2](u2-l2-rpc-and-serde.md) 讲的 `(serviceId, methodId)` 门牌号在这里被用来做**流量隔离**。

`PriorityCoroutinePool` 的真实用法在 meta 的 GC：先用优先级池跑 GC 任务，再用 `BackgroundRunner` 周期扫描目录（下一节展开）：

```cpp
// src/meta/components/GcManager.cc:L650-L652
gcWorkers_ = std::make_unique<PriorityCoroutinePool<GcTask>>(config_.gc().workers());
gcWorkers_->start(folly::partial(&GcManager::runGcTask, this), exec);
```

[GcManager.cc:L650-L652](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/GcManager.cc#L650-L652) 创建带优先级的 GC 协程池并启动——GC 任务可以按优先级插队处理。

#### 4.2.4 代码实践

**实践目标**：理解 storage 如何用隔离的协程池实现「读写流量互不干扰」。

1. 打开 [src/storage/service/Components.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/Components.h)，阅读 `getCoroutinesPool`（L80–L92）与四个池成员（L115–L118）。
2. 打开 [src/storage/service/StorageServer.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageServer.cc) 的 `beforeStart`（L26–L39），看 `setCoroutinesPoolGetter` 如何把 methodId 接到池上。
3. 在 `src/storage/service/Components.h` 的 `Config` 里找到 `coroutines_pool_read` / `coroutines_pool_update`（L59–L62）和 `use_coroutines_pool_read` / `use_coroutines_pool_update`（L63–L64）这两组配置。
4. **需要观察的现象**：读和写各自有独立的 `DynamicCoroutinesPool`，且能通过 `use_coroutines_pool_*` 开关在「用池 / 不用池」间切换。
5. **预期结果**：你能解释——如果把 `use_coroutines_pool_read` 设为 `false`，读请求会落到 `defaultPool`；这说明「池」是可配置的流量隔离手段，而非硬编码。若想本地验证行为差异，可改配置后观察监控指标，**待本地验证**。

### 4.3 后台任务：BackgroundRunner

#### 4.3.1 概念说明

很多工作不是「被动响应请求」，而是「**主动周期性执行**」：mgmtd 每隔几秒发一次心跳、续一次租约、推进一次路由版本号；client 定时刷新路由、续客户端会话。这些就是**后台任务**。

`BackgroundRunner` 就是 3FS 统一的「周期性后台协程调度器」。它的三个核心概念：

- **`TaskGetter`**：一个返回 `CoTask<void>` 的工厂函数，调用它得到「执行一轮」的协程。
- **`IntervalGetter`**：返回这一轮结束后**还要等多久**再跑下一轮（一个 `Duration`）。
- **协作取消 + 闩（latch）**：停止时发取消信号，所有任务协程退出后闩归零，`stopAll()` 才返回，保证优雅停止。

它还提供 `startOnce`：只跑一轮就结束（`IntervalGetter` 为空），适合「启动时做一次初始化」的场景。

#### 4.3.2 核心流程

```text
start(name, taskGetter, intervalGetter):
  latch 计数 +1
  把 run(name,...) 协程(附带取消令牌)调度到 executor 上启动

run(name, taskGetter, intervalGetter):     # 每个后台任务的协程
  loop i = 1,2,...:
    记录 start 时间
    res = co_await co_awaitTry(taskGetter())   # 执行一轮(异常转 Try)
    若 res 是 OperationCancelled: break        # 被取消，退出
    若 res 是其它异常: FATAL                    # 后台任务不容许未知异常
    若没有 intervalGetter: break               # startOnce 模式，跑一次就结束
    interval = intervalGetter()
    若 interval == 0: break
    若 now < start + interval: co_await sleep(剩余时间)   # 补齐到固定周期
  清空捕获的资源
  latch.countDown()                            # 通知 stopAll 我退出了

stopAll():
  stopping_ = true; cancel_.requestCancellation()
  co_await latch.wait()                        # 等所有任务 countDown
```

关键细节：**等待时长是从「本轮开始」算的**（`start + interval`），而不是「本轮结束后」再等 `interval`。所以即使某一轮执行耗时较长，整体节奏仍尽量贴近配置的周期；只有当执行时间超过 `interval` 时才会立刻进入下一轮（`sleep` 为 0）。

#### 4.3.3 源码精读

`BackgroundRunner` 的对外接口只有四个：

```cpp
// src/common/utils/BackgroundRunner.h:L19-L23
using TaskGetter = std::function<CoTask<void>()>;
using IntervalGetter = std::function<Duration()>;
bool start(String taskName, TaskGetter taskGetter, IntervalGetter intervalGetter);
bool startOnce(String taskName, TaskGetter taskGetter);
CoTask<void> stopAll();
```

[BackgroundRunner.h:L19-L23](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/BackgroundRunner.h#L19-L23) 定义了「任务工厂 + 间隔工厂」的回调签名，以及 `start`（周期）/ `startOnce`（一次）/ `stopAll`（停止）三个动作。

启动逻辑：自增闩、把带取消令牌的 `run` 协程调度出去：

```cpp
// src/common/utils/BackgroundRunner.cc:L17-L29（节选）
bool BackgroundRunner::start(String taskName, TaskGetter taskGetter, IntervalGetter intervalGetter) {
  auto lock = std::unique_lock(mutex_);
  if (stopping_) { return false; }
  if (auto n = latch_.increase(); n == 0) { latch_.reset(); }
  co_withCancellation(cancel_.getToken(), run(std::move(taskName), std::move(taskGetter), std::move(intervalGetter)))
      .scheduleOn(getExecutor())
      .start();
  return true;
}
```

[BackgroundRunner.cc:L17-L29](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/BackgroundRunner.cc#L17-L29) 用 `co_withCancellation(token, run(...))` 给整个后台协程挂上取消令牌；`scheduleOn(getExecutor()).start()` 才真正把它跑起来。若已在停止（`stopping_`），直接拒绝启动。

运行循环——这是 `BackgroundRunner` 的灵魂：

```cpp
// src/common/utils/BackgroundRunner.cc:L83-L106（节选）
XLOGF(INFO, "BackgroundRunner: {} start", taskName);
for (int64_t i = 1;; ++i) {
  auto start = SteadyClock::now();
  auto res = co_await co_awaitTry(taskGetter());          // 执行一轮
  HANDLE_CO_TRY_EXCEPTION();                              // 取消->break, 其它异常->FATAL
  if (!intervalGetter) { break; }                         // startOnce: 无间隔, 跑一次结束
  auto interval = intervalGetter().asUs();
  if (interval == 0_us) { break; }
  auto now = SteadyClock::now();
  if (now < start + interval) {                           // 补齐到固定周期
    auto sleepTime = std::chrono::duration_cast<std::chrono::microseconds>(start + interval - now);
    auto res = co_await co_awaitTry(folly::coro::sleep(sleepTime));
    HANDLE_CO_TRY_EXCEPTION();
  }
}
```

[BackgroundRunner.cc:L83-L106](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/BackgroundRunner.cc#L83-L106) 是周期循环：执行 `taskGetter()` → 处理异常 → 算剩余时间 → `co_await sleep`。注意 `HANDLE_CO_TRY_EXCEPTION` 宏（定义在 [L74-L81](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/BackgroundRunner.cc#L74-L81)）：**只有 `OperationCancelled` 是预期退出，其它异常直接 `FATAL`**——后台任务绝不能静默崩掉。

优雅停止靠取消信号 + 闩：

```cpp
// src/common/utils/BackgroundRunner.cc:L62-L71
CoTask<void> BackgroundRunner::stopAll() {
  {
    auto lock = std::unique_lock(mutex_);
    stopping_ = true;
    cancel_.requestCancellation();        // 通知所有任务协程退出
  }
  co_await latch_.wait();                 // 等它们都 countDown
}
```

[BackgroundRunner.cc:L62-L71](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/BackgroundRunner.cc#L62-L71) 展示优雅停止：发取消信号，正在 `sleep` 或 `co_await taskGetter()` 的协程会收到 `OperationCancelled` 进而 `break`、`countDown`；`stopAll` 等到闩归零才返回。析构函数还会兜底（[L45-L51](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/BackgroundRunner.cc#L45-L51)）：若仍有人在跑，用 `blockingWait(stopAll())` 阻塞等停。

**真实用法一：mgmtd 的 10 个周期性后台任务**。这是 `BackgroundRunner` 最典型的批量用法：

```cpp
// src/mgmtd/background/MgmtdBackgroundRunner.cc:L37-L80（节选）
void MgmtdBackgroundRunner::start() {
  if (backgroundRunner_) {
    backgroundRunner_->start("extendLease",
        [this] { return leaseExtender_->extend(); },
        state_.config_.extend_lease_interval_getter());
    backgroundRunner_->start("checkHeartbeat",
        [this] { return heartbeatChecker_->check(); },
        state_.config_.check_status_interval_getter());
    backgroundRunner_->start("sendHeartbeat",
        [this] { return heartbeater_->send(); },
        state_.config_.send_heartbeat_interval_getter());
    backgroundRunner_->start("bumpRoutingInfoVersion",
        [this] { return routingInfoVersionUpdater_->update(); },
        state_.config_.bump_routing_info_version_interval_getter());
    // ... 共 10 个 start(...)
  }
}
```

[MgmtdBackgroundRunner.cc:L37-L80](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdBackgroundRunner.cc#L37-L80) 用**同一个 `BackgroundRunner`** 跑了 `extendLease` / `checkHeartbeat` / `sendHeartbeat` / `bumpRoutingInfoVersion` 等 10 个周期任务，每个都有独立的 `IntervalGetter`（间隔来自配置，因此可热更新）。停止时只需一次 `co_await backgroundRunner_->stopAll()`（[L82-L86](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdBackgroundRunner.cc#L82-L86)），所有任务一起优雅退出。

**真实用法二：client 侧的自动刷新与心跳**：

```cpp
// src/client/mgmtd/MgmtdClient.cc:L240-L261（节选）
void startBackgroundTasksWithLock() {
  backgroundRunner_ = std::make_unique<BackgroundRunner>(backgroundExecutor_);
  if (config_.enable_auto_refresh()) {
    backgroundRunner_->start("AutoRefresh",
        [this] { return autoRefresh(); }, config_.auto_refresh_interval_getter());
  }
  if (config_.enable_auto_heartbeat()) {
    backgroundRunner_->start("AutoHeartbeat",
        [this] { return autoHeartbeat(); }, config_.auto_heartbeat_interval_getter());
  }
  if (config_.enable_auto_extend_client_session()) {
    backgroundRunner_->start("AutoExtendClientSession",
        [this] { return autoExtendClientSession(); }, config_.auto_extend_client_session_interval_getter());
  }
}
```

[MgmtdClient.cc:L240-L261](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/mgmtd/MgmtdClient.cc#L240-L261) 展示 client 用 `BackgroundRunner` 跑「自动刷新路由 / 自动心跳 / 自动续客户端会话」，且每个任务都受配置开关控制（可按需启停）。

#### 4.3.4 代码实践

**实践目标**：追踪一个周期性后台任务从注册到退出的完整生命周期。

1. 打开 [src/mgmtd/background/MgmtdBackgroundRunner.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdBackgroundRunner.cc)，挑一个任务，比如 `bumpRoutingInfoVersion`（L63–L66）。
2. 用 `Grep` 在 `src/mgmtd` 找 `routingInfoVersionUpdater_->update()` 的实现（即 `MgmtdRoutingInfoVersionUpdater::update`），确认它返回 `CoTask<void>`。
3. 回到 [BackgroundRunner.cc:L83-L106](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/BackgroundRunner.cc#L83-L106) 的循环，脑补：每轮调用 `update()` → 等待 `bump_routing_info_version_interval` → 再下一轮。
4. 想象服务收到停止信号：`stopAll()` 发取消令牌 → 若此时协程正 `co_await sleep`，会立刻收到 `OperationCancelled` → `HANDLE_CO_TRY_EXCEPTION` 命中取消分支 `break` → `countDown`。
5. **需要观察的现象**：周期由「执行耗时 + sleep 补齐」共同决定，且停止是即时的（不等当前 sleep 跑完）。
6. **预期结果**：你能画出这个任务的一张状态时序图：`start → run(第1轮) → sleep → run(第2轮) → ... → 收到取消 → break → countDown`。

### 4.x.5 小练习与答案（本讲合并）

**练习 1**：为什么 `CoroSynchronized` 内部用 `folly::coro::SharedMutex` 而不是 `std::shared_mutex`？如果换成后者会发生什么？

> **答案**：`std::shared_mutex` 的 `lock()` 是**阻塞**调用——会卡住整个 OS 线程。而一个线程上可能跑着成百上千个协程，卡住线程就把这些协程全堵死了。`folly::coro::SharedMutex` 的 `co_scoped_lock()` 是 `co_await`，抢不到锁时**挂起当前协程**（让出线程给别人），锁可用再恢复，所以不会连累同线程的其它协程。

**练习 2**：`DynamicCoroutinesPool` 把协程数从 64 缩到 32 时，多出来的 32 个协程是怎么停掉的？

> **答案**：`setCoroutinesNum` 的 else 分支（[DynamicCoroutinesPool.cc:L59-L63](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/DynamicCoroutinesPool.cc#L59-L63)）往队列里塞 32 个 `nullptr`。消费协程 `run()` 从队列 `co_dequeue` 出 `nullptr` 时（[L72-L73](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/DynamicCoroutinesPool.cc#L72-L73)）执行 `co_return` 退出。即「哨兵法」优雅缩容——不会硬杀协程，而是让它们自然消费完手头任务后遇到哨兵退出。

**练习 3**：`BackgroundRunner::run` 里遇到 `OperationCancelled` 是 `break`，遇到其它异常却是 `FATAL`。为什么这么设计？

> **答案**：`OperationCancelled` 是**预期的**停止信号（服务正常关闭时发出），应当优雅退出。其它异常意味着后台任务逻辑出 bug 或环境异常（比如空指针、FDB 反复冲突未捕获），若只 `break` 这个任务就**静默消失**了，集群会出现「心跳不发了 / 路由版本不推进了」却无人知晓的隐患。直接 `FATAL` 让进程崩溃重启，由 supervisor（systemd）拉起，是最安全的选择——这体现了 3FS「fail-fast」的运维哲学。

## 5. 综合实践

把本讲三个最小模块串起来，做一个**端到端源码追踪**：一次 storage 的读请求，是如何被「协程 + 执行池」承接，而 mgmtd 又是如何用「后台任务」维护这套系统运转的。

**任务**：写一份追踪笔记，回答下面的问题链（全部基于真实源码，不要编造）。

1. **协程入口**：一个 `batchRead` RPC 到达 storage。阅读 [StorageServer.cc:L29-L36](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageServer.cc#L29-L36) 的 `setCoroutinesPoolGetter`，说明框架是如何根据 `serviceId`/`methodId` 决定把这个请求丢进 `readPool`（`DynamicCoroutinesPool`）的。
2. **池内执行**：请求被丢进 `readPool` 后，阅读 [DynamicCoroutinesPool.cc:L67-L82](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/DynamicCoroutinesPool.cc#L67-L82)，说明是「哪个协程」最终 `co_await` 了你的请求处理逻辑、这个协程跑在哪个底层线程池上（提示：`executor_`，配置见 [DynamicCoroutinesPool.h:L16-L20](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/DynamicCoroutinesPool.h#L16-L20)）。
3. **挂起与并发**：处理逻辑内部如果 `co_await` 了一个 AIO 读（等待 SSD），此时这个协程会怎样？为什么同一时刻能有成千上万个这样的请求并发，而底层只有 8 个线程？（用本讲「协程挂起不占线程」的原理解释。）
4. **隔离的意义**：假设此时另有大量 `update`（写）请求涌入。阅读 [Components.h:L80-L92](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/Components.h#L80-L92)，说明写请求进的是 `updatePool` 而非 `readPool`，这种**池隔离**如何防止「写洪峰饿死读」。
5. **后台维护者**：storage 自己和它依赖的 mgmtd 都有周期性后台任务在维护集群状态。阅读 [MgmtdBackgroundRunner.cc:L37-L80](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdBackgroundRunner.cc#L37-L80)，指出是哪个后台任务在**周期性地推进路由版本号**（让 client 能拉到最新的 chain/target 视图），并说明它的周期由哪个配置项控制。
6. **优雅停止**：最后描述整个 storage 服务关闭时，`readPool`（[DynamicCoroutinesPool.cc:L25-L36](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/DynamicCoroutinesPool.cc#L25-L36)）和 mgmtd 的 `backgroundRunner_`（[MgmtdBackgroundRunner.cc:L82-L86](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdBackgroundRunner.cc#L82-L86)）分别是如何被停掉的，二者机制有何相似之处。

**产出**：一张包含「RPC 到达 → 选池 → 池协程消费 → 挂起等 IO → 完成」的时序图，加一段说明「周期性后台任务如何独立于请求处理持续维护集群」。完成后，你就把**协程原语、执行池、后台任务**三者在一个真实请求路径上串通了。

## 6. 本讲小结

- 3FS 全面基于 folly 的 C++20 协程：`CoTask<T>` 是基本异步计算，`CoTryTask<T>` 把返回值包成 `Result<T>` 让错误用返回值（而非异常）传递；二者都是别名，本体在 [Coroutine.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/Coroutine.h)。
- 协程在 `co_await` 上几乎零成本挂起，让「数万并发 + 十几线程」成为可能；跨协程共享数据要用协程友好的 `CoroSynchronized<T>`（`co_await` 加锁而非阻塞线程）。
- 执行池分三层：`CPUExecutorGroup` 提供**线程资源**（默认 32 线程 ROUND_ROBIN，`pickNextFree` 探测式负载均衡）；`DynamicCoroutinesPool` 是**N 协程 + 有界队列**的生产者-消费者（协程数可热更新，靠 `nullptr` 哨兵缩容）；`PriorityCoroutinePool` 在后者基础上加**优先级**。
- storage 用 4 个隔离的 `DynamicCoroutinesPool`（read/update/sync/default）按 RPC methodId 分流，实现读写流量互不饿死；meta 的 GC 用 `PriorityCoroutinePool` 让任务按优先级插队。
- `BackgroundRunner` 是周期性后台协程调度器：`TaskGetter` 产出「一轮」、`IntervalGetter` 决定周期、`OperationCancelled` 才允许优雅退出（其它异常 FATAL），靠 `CancellationSource` + `CountDownLatch` 实现一次 `stopAll()` 停掉全部任务。mgmtd 用它跑 10 个周期任务，client 用它跑自动刷新/心跳。
- 这套设施是 [u2-l1](u2-l1-service-skeleton.md) 服务骨架的「肌肉」：服务在 `beforeStart` 注册 RPC 与协程池（如 [StorageServer.cc:L29-L36](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageServer.cc#L29-L36)），在生命周期里启动/停止后台任务——骨架相同，血肉（协程与池）不同。

## 7. 下一步学习建议

- **纵向深入 storage 读路径**：本讲只到「请求进 `readPool`」。下一站读 [u5-l2 读路径：批量读与 AIO](u5-l2-read-path-aio.md)，看池里的协程如何 `co_await` AIO、如何分配 RDMA buffer，把协程挂起点对应到真实 IO。
- **纵向深入 mgmtd 后台任务**：本讲把 mgmtd 的 10 个后台任务当作 `BackgroundRunner` 的样例。读 [u3-l2 节点注册、心跳与租约续期](u3-l2-registration-heartbeat.md)、[u3-l3 主选举与故障切换](u3-l3-primary-election.md)、[u3-l6 路由信息分发](u3-l6-routing-and-config.md)，看 `sendHeartbeat` / `extendLease` / `bumpRoutingInfoVersion` 这些 `TaskGetter` 背后到底干了什么。
- **横向补齐公共基础设施**：协程要跑在线程池上，而 RPC 回包要靠网络层送达。继续读 [u2-l4 网络层：TCP 与 RDMA 传输](u2-l4-network-rdma.md)，理解 `IOWorker` / `Processor` 如何把网络事件转成协程的恢复；以及 [u2-l5 配置系统与热更新](u2-l5-config-system.md)，理解本讲反复出现的 `CONFIG_HOT_UPDATED_ITEM`（如协程数、后台任务间隔）是如何被热更新的。
- **动手验证（可选）**：跑 `tests/common/utils/TestDynamicCoroutinesPool.cc`（用 `Grep` 在仓库找到对应的 CMake target），观察 `DynamicCoroutinesPool` 的 `Normal` / `ManyTasks` / `HotUpdated` 三个用例如何验证「热更新协程数」行为，这是把本讲知识落到测试断言上的好方式。

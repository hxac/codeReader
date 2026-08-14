# 连接池执行器与线程模型

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `ConnectPoolExecutor` 的任务模型：任务怎么入队、怎么被工作线程领取、状态怎么迁移。
2. 掌握公共组件 `ThreadPool`（可弹性扩缩的核心/临时线程池）与 `PeriodicTask`（可中断的周期任务）的使用方式。
3. 理解异步建链的并发控制策略：同一远端串行、不同远端并行、队列容量上限、状态防竞态规则。
4. 会阅读 `tests/cpp/hixl` 下与并发相关的单元测试，并能从断言反推设计意图。

本讲是单元三中「偏基础设施」的一讲：前几讲关注链路怎么建（`HixlClient`、`ClientHandler`、`EndpointMatcher`），本讲关注这些动作在哪些线程上、以什么并发规则执行。

## 2. 前置知识

阅读本讲前，建议先回顾以下概念（均在前序讲义中出现过）：

- **异步建链接口**：`Hixl::ConnectAsync` / `DisconnectAsync` / `GetAsyncConnectStatus`，以及七态的 `AsyncConnectStatus` 状态机（u2-l4）。
- **生产者-消费者队列**：一类线程往队列里放任务（生产者），另一类线程从队列里取任务执行（消费者），用「互斥锁 + 条件变量」协调双方。本讲的三个组件全部建立在这个经典模型上。
- **条件变量的谓词等待**：`cv.wait(lock, pred)` 会先判断 `pred()`，为假则释放锁并睡眠，被唤醒后重新加锁再判断，直到谓词为真才返回。这是避免「虚假唤醒」和「错过的唤醒」的标准写法。
- **ACL Context（设备上下文）**：昇腾运行时中，与某个 device 绑定的执行环境。在 CANN 上，很多 `aclrt*` / CS 层调用要求当前线程绑定了正确的 context——这一点直接决定了本讲的一个关键设计：工作线程开工前必须先恢复 context。

如果对「单边通信」「endpoint 匹配」等业务概念已经模糊，可先回看 u1-l1 与 u3-l3，再回来读本讲。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/hixl/engine/connect_pool_executor.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/connect_pool_executor.h) | `ConnectPoolExecutor` 类声明：任务结构体、公开接口、任务队列与状态表两组数据成员 |
| [src/hixl/engine/connect_pool_executor.cc](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/connect_pool_executor.cc) | 核心实现：初始化/关闭、任务提交、工作线程主循环、状态防竞态规则 |
| [src/hixl/common/thread_pool.h](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/thread_pool.h) | 通用线程池声明，含模板成员 `commit`（返回 `std::future`） |
| [src/hixl/common/thread_pool.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/thread_pool.cc) | 线程池实现：核心线程/临时线程、弹性扩容、优雅销毁 |
| [src/hixl/common/periodic_task.h](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/periodic_task.h) | 周期任务声明：`State` 结构体 + `Start/Stop/IsRunning` |
| [src/hixl/common/periodic_task.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/periodic_task.cc) | 周期任务实现：可中断等待、异常兜底 |
| [src/hixl/engine/hixl_impl.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_impl.cc) | `ConnectPoolExecutor` 的唯一上层调用方：`Initialize/Finalize/ConnectAsync/DisconnectAsync` |
| [src/hixl/engine/hixl_options.h](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_options.h) | `ConnectPoolConfig`：`thread_num` / `task_queue_capacity` 两个可调参数的定义 |
| [tests/cpp/hixl/common/thread_pool_ut.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/cpp/hixl/common/thread_pool_ut.cc) | 线程池单元测试（本讲代码实践的主战场） |
| [tests/cpp/hixl/engine/hixl_api_unittest.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/cpp/hixl/engine/hixl_api_unittest.cc) | 引擎层 API 测试，含连接池初始化失败回滚用例 |

## 4. 核心概念与源码讲解

### 4.1 ConnectPoolExecutor：异步建链的任务模型

#### 4.1.1 概念说明

回顾 u2-l4：`Hixl::Connect` 是同步接口，会一直阻塞到建链成功或超时。如果用户要同时和几十个远端建链（PD 分离集群里 Decoder 要连多个 Prompt 节点），逐个同步建链的串行等待是不可接受的。`ConnectPoolExecutor` 就是为此存在的**异步任务执行器**：

- 用户调用 `ConnectAsync` 时，真正的工作（内部仍是那一个同步 `Connect`）被打包成 `std::function<void()>` 丢进队列，接口立刻返回；
- 一组固定数量的工作线程从队列里领取任务执行；
- 执行结果通过一张 `remote_engine → AsyncConnectStatus` 的状态表暴露给 `GetAsyncConnectStatus` 查询。

它的任务结构体非常朴素——「是不是建链任务、连谁、干什么」三件事：

```cpp
// src/hixl/engine/connect_pool_executor.h L29-L33
struct ConnectPoolExecutorTask {
  bool is_connect;
  AscendString remote_engine;
  std::function<void()> task;
};
```

这段定义了任务的三要素：`is_connect` 只用来决定进入 `CONNECTING` 还是 `DISCONNECTING` 中间态；`remote_engine`（形如 `ip:port` 的字符串）既是任务的去重键，也是状态表的 key；`task` 是真正的建链/断链闭包。[connect_pool_executor.h:L29-L33](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/connect_pool_executor.h#L29-L33)

注意一个容易混淆的点：**`ConnectPoolExecutor` 不是本讲 4.2 节那个通用 `ThreadPool` 的封装**。它是独立实现的一套小执行器，专为一个特定约束（同一远端串行）而设计；`ThreadPool` 则是 `src/hixl/common` 下的通用组件，别处（如 `UbClientHandler`）在使用。两者形似而神不同，这是读源码时要留意的。

#### 4.1.2 核心流程

一次异步建链的完整生命周期：

```text
用户线程                                 工作线程(worker 0..N-1)
────────                                ──────────────────────
ConnectAsync(remote)
  └─ Submit(task, remote, true)
       ├─ 加锁 task_queue_mutex_
       ├─ 未初始化? → FAILED
       ├─ 队列满?     → RESOURCE_EXHAUSTED
       ├─ SetStatus(remote, CONNECT_PENDING)
       ├─ 任务入队 task_list_
       └─ notify_one()
                                        被 cv 唤醒（谓词：存在「remote 不在 doing 集合」的任务）
                                        加锁，扫描队列，跳过正在执行的 remote
                                        取出任务，加入 task_doing_set_
                                        SetStatus(remote, CONNECTING)
                                        解锁
                                        task()  ← 内部调用同步 engine_->Connect(...)
                                        SetStatus(remote, CONNECTED / CONNECT_FAILED)
                                        加锁，从 doing 集合摘除，notify_one()

GetAsyncConnectStatus(remote) ← 用户随时轮询状态表
```

状态迁移链（成功路径）：

\[ \text{CONNECT\_PENDING} \rightarrow \text{CONNECTING} \rightarrow \text{CONNECTED} \]

失败路径以 `CONNECT_FAILED` 终止；断链任务对应 `DISCONNECT_PENDING → DISCONNECTING → NOT_CONNECT`（`NOT_CONNECT` 不是存储状态，而是把该远端从表中抹除的信号）。

三条并发控制规则值得单独记住：

1. **同远端串行**：`task_doing_set_` 保证同一 `remote_engine` 同一时刻只有一个任务在执行；对同一远端的第二个任务只能留在队列里等。
2. **异远端并行**：不同远端的任务互不阻塞，只要还有空闲 worker 就能并行执行——这正是高并发建链的来源。
3. **队列有界**：容量默认 128，满了直接返回 `RESOURCE_EXHAUSTED`，不阻塞提交者。

#### 4.1.3 源码精读

**初始化与参数**。默认 2 线程、128 队列容量，可通过 `GlobalResourceConfig` 里的 `connect_pool.thread_num` / `connect_pool.task_queue_capacity` 覆盖，并有硬性上下限（线程 1..64，容量 1..65535）：

```cpp
// src/hixl/engine/connect_pool_executor.cc L16-L22
constexpr const int32_t kOptionConnectPoolThreadNum = 2;
constexpr const int32_t kOptionConnectPoolTaskQueueCapacity = 128;
constexpr const int32_t kLimitThreadNumMin = 1;
constexpr const int32_t kLimitThreadNumMax = 64;
constexpr const int32_t kLimitTaskQueueCapacityMin = 1;
constexpr const int32_t kLimitTaskQueueCapacityMax = 65535;
```

这是默认值与合法区间的唯一事实来源。[connect_pool_executor.cc:L16-L22](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/connect_pool_executor.cc#L16-L22) 对应的配置结构体在 [hixl_options.h:L34-L37](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_options.h#L34-L37)（`ConnectPoolConfig` 两个 `std::optional<int32_t>` 字段）。

初始化主体先校验参数区间（越界返回 `PARAM_INVALID`），然后做一件容易被忽略但很关键的事——**捕获当前线程的 ACL context**，最后才拉起 worker 线程：

```cpp
// src/hixl/engine/connect_pool_executor.cc L53-L57
HIXL_CHK_STATUS_RET(ctx_.GetCurrentContext(), "Failed to capture acl context");
is_initialized_.store(true, std::memory_order::memory_order_relaxed);
for (int32_t i = 0; i < thread_num_; ++i) {
  workers_.emplace_back([this, i]() { WorkerHandler(i); });
}
```

因为 `Initialize` 是在用户线程上被调用的，此处 `GetCurrentContext` 把调用者的设备上下文存进成员 `ctx_`；worker 线程是新建的，没有 context，所以每个 worker 开工第一件事就是 `ctx_.SetCurrentContext()` 把它恢复（见下文 `WorkerHandler`）。这样建链任务里的 CS 层调用才能找到正确的 device。[connect_pool_executor.cc:L53-L57](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/connect_pool_executor.cc#L53-L57)

**任务提交**。`Submit` 是唯一的生产者入口，全程序列很短：

```cpp
// src/hixl/engine/connect_pool_executor.cc L85-L101（节选）
std::unique_lock<std::mutex> lock(task_queue_mutex_);
if (!IsInitialized()) { return FAILED; }
if (task_list_.size() >= static_cast<std::size_t>(task_queue_capacity_)) {
  return RESOURCE_EXHAUSTED;
}
// 新任务插入队尾
SetStatus(remote_engine, is_connect ? AsyncConnectStatus::CONNECT_PENDING
                                    : AsyncConnectStatus::DISCONNECT_PENDING);
task_list_.emplace_back(ConnectPoolExecutorTask{is_connect, remote_engine, task});
task_queue_cv_.notify_one();
```

注意 `CONNECT_PENDING` 是在**入队时**就写入状态表的，也就是说 `ConnectAsync` 一返回，用户就能查到 `CONNECT_PENDING`——状态查询永远有东西可查，不会出现「任务还没被领取、查状态却像没发起过」的真空期。[connect_pool_executor.cc:L85-L101](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/connect_pool_executor.cc#L85-L101)

**工作线程主循环**。这是整个执行器最核心的 60 行，值得逐段读：

```cpp
// src/hixl/engine/connect_pool_executor.cc L146-L200（节选，注释为本讲添加）
void ConnectPoolExecutor::WorkerHandler(const int32_t worker_id) {
  HIXL_CHK_STATUS(ctx_.SetCurrentContext(), "Failed to set acl context");   // ① 恢复 context
  auto cv_wait_func = [this]() {
    if (!IsInitialized()) { return true; }                                  // ② 关停也可唤醒
    for (const auto &t : task_list_) {
      if (task_doing_set_.count(t.remote_engine) == 0) { return true; }     // ③ 有可领的任务
    }
    return false;
  };
  while (true) {
    ConnectPoolExecutorTask executor_task{false, "", nullptr};
    {
      std::unique_lock<std::mutex> lock(task_queue_mutex_);
      task_queue_cv_.wait(lock, cv_wait_func);
      if (!IsInitialized()) { break; }                                      // ④ 关停退出
      if (task_list_.empty()) { continue; }                                 // ⑤ 虚假唤醒
      for (auto it = task_list_.begin(); it != task_list_.end(); ++it) {
        // 保证一个 remote_engine 同一时刻只做一个任务                        // ⑥ 同远端串行
        if (task_doing_set_.count(it->remote_engine) == 0) {
          task_doing_set_.emplace(it->remote_engine);
          SetStatus(it->remote_engine, it->is_connect ? AsyncConnectStatus::CONNECTING
                                                     : AsyncConnectStatus::DISCONNECTING);
          executor_task = std::move(*it);
          task_list_.erase(it);
          break;                                                            // ⑦ 领一个就走
        }
      }
    }                                                                       // ⑧ 出临界区再执行
    if (executor_task.task != nullptr) {
      executor_task.task();                                                 // ⑨ 真正干活（可能秒级）
      std::lock_guard<std::mutex> lock(task_queue_mutex_);
      task_doing_set_.erase(executor_task.remote_engine);                   // ⑩ 归还远端
      task_queue_cv_.notify_one();                                          // ⑪ 唤醒等同一远端的 worker
    }
  }
}
```

几个设计细节：

- **谓词 ③ 是「有可领的任务」而不是「队列非空」**：如果队列里只剩某个远端的任务、而该远端正在被别的 worker 执行，当前 worker 应继续睡，而不是空转。
- **⑧ 出锁后执行任务**：`task()` 可能阻塞数秒（建链超时上限），若持锁执行，其他 worker 的领队操作和用户的 `Submit` 全部被卡死——这是手写执行器最常见的错误，此处用「临界区只做摘取、干活在锁外」规避。
- **⑪ 归还时只 `notify_one`**：归还一个远端只可能让一个等待该远端的 worker 获得可领任务，唤醒一个就够。

完整代码见 [connect_pool_executor.cc:L146-L203](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/connect_pool_executor.cc#L146-L203)。

**状态表的防竞态规则**。`SetStatus` 不只是简单的写 map，它实现了「用户最新操作优先」：

```cpp
// src/hixl/engine/connect_pool_executor.cc L104-L126（节选）
if (status == CONNECTING || status == CONNECTED || status == CONNECT_FAILED) {
  const auto &it = task_result_.find(remote_engine);
  if (it != task_result_.end() && it->second == DISCONNECT_PENDING) { return; }
}
if (status == DISCONNECTING || status == NOT_CONNECT) {
  const auto &it = task_result_.find(remote_engine);
  if (it != task_result_.end() && it->second == CONNECT_PENDING) { return; }
}
if (status == NOT_CONNECT) { task_result_.erase(remote_engine); }
else { task_result_[remote_engine] = status; }
```

它防的是这样一个竞态：worker A 正在执行对远端 X 的 `Connect`（慢），此时用户提交了对 X 的 `DisconnectAsync`，状态表被写成 `DISCONNECT_PENDING`。随后 A 的 connect 结束、想把状态改成 `CONNECTED`——如果放行，用户就会看到 `DISCONNECT_PENDING` 之后状态又「倒退」成 `CONNECTED`，查询结果自相矛盾。所以规则是：凡是落后任务想写的中间态/终态（CONNECTING/CONNECTED/CONNECT_FAILED、DISCONNECTING/NOT_CONNECT），只要用户已经提交了反向操作（表里是 `*_PENDING`），一律丢弃。[connect_pool_executor.cc:L104-L126](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/connect_pool_executor.cc#L104-L126)

`GetStatus` 则是纯读：查不到按 `NOT_CONNECT` 处理，还有一个批量版本把整张表拷出去，见 [connect_pool_executor.cc:L128-L140](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/connect_pool_executor.cc#L128-L140)。

**上层怎么用它**。`HixlImpl` 是唯一调用方。初始化时在引擎初始化成功之后拉起执行器，失败则回滚引擎（不留半初始化状态，呼应 u2-l1）：

```cpp
// src/hixl/engine/hixl_impl.cc L92-L98
Status ret = connect_pool_executor_.Initialize(parsed_options);
if (ret != SUCCESS) {
  engine_->Finalize();
  engine_.reset();
  HIXL_LOGE(ret, "Failed to initialize ConnectPoolExecutor.");
  return ret;
}
```

[hixl_impl.cc:L92-L98](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_impl.cc#L92-L98)；`Finalize` 中则先 `Shutdown` 执行器再销毁引擎（[hixl_impl.cc:L102-L110](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_impl.cc#L102-L110)），顺序不能反——worker 手里还攥着 `engine_` 的引用时不能先把引擎拆了。

`ConnectAsync` 把同步 `Connect` 包成闭包并定义终态写回规则（`SUCCESS` 与 `ALREADY_CONNECTED` 都算连上，正是 u2-l4 讲过的幂等语义）：

```cpp
// src/hixl/engine/hixl_impl.cc L145-L153
auto task = [this, remote_engine, timeout_in_millis]() {
  Status ret = engine_->Connect(remote_engine, timeout_in_millis);
  const auto status = (ret == SUCCESS || ret == ALREADY_CONNECTED)
                          ? AsyncConnectStatus::CONNECTED
                          : AsyncConnectStatus::CONNECT_FAILED;
  connect_pool_executor_.SetStatus(remote_engine, status);
};
return connect_pool_executor_.Submit(task, remote_engine, true);
```

[hixl_impl.cc:L142-L154](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_impl.cc#L142-L154)。`DisconnectAsync` 同构，见 [hixl_impl.cc:L156-L166](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_impl.cc#L156-L166)。

**优雅关停**。`Shutdown` 先置 `is_initialized_ = false` 并 `notify_all`（让所有 worker 的谓词 ② 成立、从 ④ 退出），再逐个 `join`：

```cpp
// src/hixl/engine/connect_pool_executor.cc L62-L81（节选）
{
  std::lock_guard<std::mutex> lock(task_queue_mutex_);
  if (!IsInitialized()) { return; }
  is_initialized_.store(false, std::memory_order::memory_order_relaxed);
  task_queue_cv_.notify_all();
}
for (auto &worker : workers_) {
  if (worker.joinable()) { worker.join(); }
}
workers_.clear();
```

注意与 `ThreadPool::Destroy` 的一个语义差异：这里队列中**未执行的任务会被直接丢弃**（worker 退出时只打一条 `N task remain` 的警告日志，见 [connect_pool_executor.cc:L168-L171](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/connect_pool_executor.cc#L168-L171)），而不是排空执行。因此正确用法是 `Finalize` 前先等所有异步任务到终态，这与 u2-l4 强调的「断链完成后再 Finalize」合同一致。

#### 4.1.4 代码实践

**实践一：从测试验证「连接池参数校验 + 回滚」**

1. 实践目标：通过现成单测确认 `connect_pool.thread_num` 非法时 `Initialize` 失败且回滚干净。
2. 操作步骤：阅读 [hixl_api_unittest.cc:L295-L307](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/cpp/hixl/engine/hixl_api_unittest.cc#L295-L307) 的 `TestInitializeRollsBackEngineWhenConnectPoolInitializationFails`；如本地已按 u1-l2 跑通测试环境，可执行 `bash tests/run_test.sh -s hixl` 后在 `build_out/report` 中查看该用例结果。
3. 需要观察的现象：`thread_num` 设为 `"0"`（低于下限 1）时 `Initialize` 返回 `PARAM_INVALID`；清空 options 后同一个 `Hixl` 对象再次 `Initialize` 返回 `SUCCESS`，随后 `ConnectAsync` 正常返回 `SUCCESS`。
4. 预期结果：第二次初始化能成功，证明第一次失败后引擎与执行器均被完整回滚、对象可复用。完整运行输出待本地验证（依赖 CANN stub 测试环境）。

**实践二：给异步建链补一条时序说明（本讲指定实践任务）**

1. 实践目标：把 4.1.2 的流程图落到真实代码行，写出一份可复查的时序说明。
2. 操作步骤：从 [hixl_impl.cc:L303-L315](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_impl.cc#L303-L315)（`Hixl::ConnectAsync` 外壳）出发，沿 `HixlImpl::ConnectAsync → Submit → WorkerHandler → engine_->Connect` 逐函数标注「发生在用户线程还是 worker 线程」。
3. 需要观察的现象：至少能指出两个必须跨线程共享的状态（状态表 `task_result_` 与 ACL context `ctx_`）以及各自的保护手段（`task_result_mutex_` / 初始化时捕获+worker 恢复）。
4. 预期结果：产出一份类似 4.1.2 的时序文本，但每一行都带源码行号。

#### 4.1.5 小练习与答案

**练习 1**：如果两个 worker 同时领取了同一远端 X 的两个 connect 任务，会发生什么？源码里哪一行防止了这种情况？

答案：会导致 X 的两条链路并发建立、状态表被交叉写入，出现不可预期的结果。防止它的是 [connect_pool_executor.cc:L179-L180](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/connect_pool_executor.cc#L179-L180)：领队前检查 `task_doing_set_.count(it->remote_engine) == 0`，把 remote 加入 doing 集合后其他 worker 对同一 remote 的任务只能跳过，留在队列里等归还。

**练习 2**：`Submit` 返回 `RESOURCE_EXHAUSTED`（u2-l2 讲过它通常可重试）时，用户应如何恢复？

答案：说明队列已积压到 `task_queue_capacity`（默认 128）。可以等待在途任务完成后重试 `ConnectAsync`，或在 Initialize 时通过 `GlobalResourceConfig` 调大 `connect_pool.task_queue_capacity`（上限 65535），或调大 `thread_num` 加快消化速度。因为该错误不破坏任何状态（任务根本没有入队），直接重试是安全的。

**练习 3**：为什么 `WorkerHandler` 里执行 `task()` 时不能持有 `task_queue_mutex_`？

答案：`task()` 内部是同步 `engine_->Connect`，受超时参数控制，最坏可阻塞数秒。若持锁执行：其他 worker 无法领任务（生产者-消费者停摆）、用户线程 `Submit` 会阻塞在 `task_queue_mutex_` 上，整个实例的建链能力退化为单线程甚至死等。源码用两个独立临界区（摘取一个、归还一个）保证干活在锁外，见 [connect_pool_executor.cc:L188-L200](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/connect_pool_executor.cc#L188-L200)。

### 4.2 ThreadPool：可弹性扩缩的通用线程池

#### 4.2.1 概念说明

`ThreadPool` 是 `src/hixl/common` 下的通用线程池，与连接池执行器解决不同的问题：

- `ConnectPoolExecutor`：任务间有「按 remote 串行」的语义约束，状态对外可见；
- `ThreadPool`：任务之间完全独立，只追求吞吐，支持**弹性扩缩**——常驻「核心线程」+ 按需创建、用完即退的「临时线程」，形似 Java `ThreadPoolExecutor` 的 core/max 模型，且 `commit` 是模板函数，能返回带类型的 `std::future`。

它在引擎内的典型用户是 `UbClientHandler::ConnectHandles`（u3-l2 讲过：UB 链路要按 `CommType` 并发建多条链）：

```cpp
// src/hixl/engine/ub_client_handler.cc L250-L260（节选）
ThreadPool thread_pool("ub_connect", handles.size());
for (const auto &[type, handle] : handles) {
  futures.emplace_back(thread_pool.commit([handle, timeout_ms, type, &context]() -> Status {
    HIXL_CHK_STATUS_RET(context.SetCurrentContext(), "SetCurrentContext failed");
    HIXL_CHK_STATUS_RET(HixlCSClientConnect(handle, timeout_ms), ...);
    return SUCCESS;
  }));
}
```

这里创建了一个「线程数 = 待建链路数」的临时池，每种 `CommType` 一条线程并发调用 CS 层连接接口，再用 future 收集结果——注意它同样先做 `SetCurrentContext`，与 4.1 节的 context 约束一致。[ub_client_handler.cc:L244-L268](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/ub_client_handler.cc#L244-L268)

#### 4.2.2 核心流程

```text
构造 ThreadPool(name, min, max)
  └─ 创建 min 个核心线程，各自跑 ThreadFunc 循环

commit(func, args...)
  ├─ 已停止? → 返回无效 future
  ├─ 任务包成 packaged_task 入队
  ├─ 空闲线程 == 0 且 总数 < max? → AddTemporaryThread() 扩容
  └─ notify_one

ThreadFunc（每线程）
  循环: PopTask（无任务则睡眠）
    ├─ 停止且队列空 → 退出
    ├─ 核心线程: idle--, busy++ → task() → busy--, idle++
    └─ 临时线程: 执行完当前任务即退出（exit after task）

Destroy()
  ├─ 置 is_stopped_，notify_all
  ├─ join 所有线程（核心线程会把队列中剩余任务做完才退出）
  └─ join 临时线程
```

与 `ConnectPoolExecutor` 的一个重要差异：`PopTask` 的等待谓词是 `is_stopped_ || !tasks_.empty()`，且停止后核心线程会**先排空队列再退出**（`is_stopped_ && tasks_.empty()` 才返回 false），所以 `Destroy` 是「排空式」关停，不丢任务——对照 4.1 节连接池的「丢弃式」关停，这是两种刻意的取舍。

#### 4.2.3 源码精读

**构造与参数防御**。`min`、`max` 都做了钳制（min 至少 1，max 不小于 min）：

```cpp
// src/hixl/common/thread_pool.cc L14-L28（节选）
ThreadPool::ThreadPool(std::string thread_name_prefix, const uint32_t min_size, const uint32_t max_size)
    : thread_name_prefix_(std::move(thread_name_prefix)),
      ...
      min_thrd_num_(min_size < 1U ? 1U : min_size),
      max_thrd_num_(max_size < min_thrd_num_ ? min_thrd_num_ : max_size) {
  ...
  for (uint32_t i = 0U; i < min_thrd_num_; ++i) {
    pool_.emplace_back(&ThreadFunc, this, i, false, nullptr);
  }
}
```

[thread_pool.cc:L14-L28](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/thread_pool.cc#L14-L28)。构造即启动全部核心线程，无需显式 start。

**commit：扩容决策**。`commit` 是头文件里的模板成员，入队后依据三个原子计数决定是否扩容：

```cpp
// src/hixl/common/thread_pool.h L63-L72（节选）
uint32_t idle_num = idle_thrd_num_.load();
uint32_t busy_num = busy_thrd_num_.load();
uint32_t total_num = total_thrd_num_.load();
if (idle_num == 0U && task_queue_size > 0U && total_num < max_thrd_num_) {
  HIXL_LOGI("[ThreadPool:%s] scaling up, ...", ...);
  AddTemporaryThread();
}
cond_var_.notify_one();
```

三个条件缺一不可：没有空闲线程、队列确实有积压、还没到上限。线程命名也在此体系内完成——每个线程被命名为 `前缀_core_序号` / `前缀_temp_序号`（[thread_pool.cc:L89-L95](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/thread_pool.cc#L89-L95)），方便在 `ps -T` 或日志里定位。[thread_pool.h:L39-L76](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/thread_pool.h#L39-L76) 是完整的 `commit` 实现，值得整读：它展示了 `packaged_task + future` 的标准封装手法。

**临时线程的一生**。`ThreadFunc` 中临时线程与核心线程走同一循环，但退出条件不同：

```cpp
// src/hixl/common/thread_pool.cc L146-L160（节选）
if (!is_temporary) { --thread_pool->idle_thrd_num_; }
++thread_pool->busy_thrd_num_;
task();
--thread_pool->busy_thrd_num_;
if (is_temporary && !thread_pool->is_stopped_) {
  --thread_pool->total_thrd_num_;
  thread_pool->LogTempThreadExit("exit after task", thread_idx);
  break;                                    // 临时线程：干完一件就退
}
if (!is_temporary) { ++thread_pool->idle_thrd_num_; }
```

[thread_pool.cc:L127-L165](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/thread_pool.cc#L127-L165)。临时线程「一事一毕」：处理完一个任务就自减总数并退出，已退出的线程句柄由下次 `AddTemporaryThread` 时的 `CleanupFinishedTempThreads` 惰性回收（[thread_pool.cc:L113-L125](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/thread_pool.cc#L113-L125)）。

**排空式销毁**。`Destroy` 置停止标志后，核心线程因 `PopTask` 返回 false 的条件是「已停止 **且** 队列空」（[thread_pool.cc:L102-L111](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/thread_pool.cc#L102-L111)），因此积压任务会被做完；随后逐个 join 并吞掉 join 异常，见 [thread_pool.cc:L34-L70](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/thread_pool.cc#L34-L70)。

#### 4.2.4 代码实践

1. 实践目标：通过单测亲眼验证「核心线程占满时自动扩临时线程、且不超过 max」。
2. 操作步骤：阅读 [tests/cpp/hixl/common/thread_pool_ut.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/cpp/hixl/common/thread_pool_ut.cc) 中的 `ScaleUpWhenAllCoreThreadsBusy`（L86-L112）与 `NoScaleUpBeyondMaxSize`（L114-L137）。若本地测试环境可用，运行 `bash tests/run_test.sh -s hixl` 定位 thread_pool 相关用例。
3. 需要观察的现象：测试先用两个会阻塞的任务占满 2 个核心线程，再提交新任务；`max_concurrent` 计数器记录到的最大并发数落在 `(core, max]` 区间；第二个用例中无论压多少任务，并发数都被 `max_size` 封顶。
4. 预期结果：`EXPECT_GE(max_concurrent, 3)` 与 `EXPECT_LE(max_concurrent, 3)` 分别通过，证明扩容与封顶逻辑正确。运行输出待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`ThreadPool pool("x", 2, 2)` 会扩容吗？测试里哪个用例覆盖了这种情况？

答案：不会，min == max 时 `total < max_thrd_num_` 永远为假，`AddTemporaryThread` 不会被触发，退化为固定大小池。覆盖用例是 `NoScaleUpWhenMinEqualsMax`（[thread_pool_ut.cc:L168-L191](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/cpp/hixl/common/thread_pool_ut.cc#L168-L191)），断言 `max_concurrent == 2`。

**练习 2**：`commit` 在池已 `Destroy` 后调用会发生什么？如何感知？

答案：返回一个无效的 `std::future`（默认构造），不抛异常也不阻塞。感知方式是检查 `future.valid()`，`CommitReturnsInvalidFutureWhenStopped` 用例（[thread_pool_ut.cc:L260-L266](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/cpp/hixl/common/thread_pool_ut.cc#L260-L266)）断言 `EXPECT_FALSE(future.valid())`。注意区分：`ConnectPoolExecutor::Submit` 在同样场景返回的是错误码 `FAILED`，两个组件的错误风格不同。

**练习 3**：为什么临时线程做完一个任务就退出，而不是回到循环继续等？

答案：临时线程本来就是为吸收瞬时洪峰而创建的；洪峰退去后若它常驻，池的线程数只增不减，违背 min/max 语义且浪费资源。「一事一毕 + 惰性回收」让池自动回落到 min 个核心线程。

### 4.3 PeriodicTask：可中断的周期任务

#### 4.3.1 概念说明

`PeriodicTask` 解决「每隔固定时间做一件事」的需求，例如周期性心跳探测、周期性统计信息 dump。它看似简单，但有两个工程质量点值得学习：

1. **可中断等待**：不用 `sleep_for(interval)` 再循环——那样 `Stop` 时最坏要等满一个周期才能退出。它用 `cv.wait_for` 的谓词版本，`Stop` 置位后立即唤醒，退出延迟接近零。
2. **异常兜底**：回调在独立线程里执行，若回调抛出异常且无人捕获，将导致 `std::terminate` 直接杀死进程。`Run` 里用 try-catch 把异常转成错误日志，保证周期任务永不因回调崩溃而拖垮进程。

引擎内的实际用户是 `FabricMemStatistic`（FabricMem 模式的统计 dump，成员 `dump_task_`，见 [fabric_mem_statistic.h:L84](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_statistic.h#L84)），u5 单元会再遇到它。

#### 4.3.2 核心流程

```text
Start(interval, task)
  ├─ 校验 interval > 0 且 task 非空
  ├─ 已在运行? → 直接返回 SUCCESS（幂等）
  └─ 存状态，拉起 Run 线程

Run 循环:
  cv.wait_for(lock, interval, [停止?])   ← 每周期醒一次，或被 Stop 立即唤醒
  ├─ 谓词成立(已停止) → 退出
  ├─ 取当前 task（锁内快照）
  └─ task()  ← 锁外执行；异常被 catch 并记日志

Stop()
  ├─ 置 running=false，清空 task
  ├─ notify_all（立即打断等待）
  └─ join 工作线程
```

计时语义是「从上次醒来到下次醒来间隔 `interval`」，即**周期含执行时间**：回调耗时越长，实际执行频率越低，不会产生任务堆积（每个周期最多执行一次，且下一次等待从醒来时刻重新计）。

#### 4.3.3 源码精读

**状态与线程解耦**。全部可变状态装进一个 `shared_ptr<State>`，工作线程持有同一份共享状态：

```cpp
// src/hixl/common/periodic_task.h L39-L50
struct State {
  mutable std::mutex mutex;
  std::condition_variable cv;
  std::function<void()> task;
  std::chrono::milliseconds interval{0};
  std::atomic<bool> running{false};
};
static void Run(std::shared_ptr<State> state);
std::shared_ptr<State> state_{std::make_shared<State>()};
std::thread worker_;
```

[periodic_task.h:L38-L51](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/periodic_task.h#L38-L51)。类本身不可拷贝不可移动（L29-L32 的 delete 声明），避免 `State` 被意外复制出两份。

**可中断等待的核心**：

```cpp
// src/hixl/common/periodic_task.cc L59-L81（节选）
void PeriodicTask::Run(std::shared_ptr<State> state) {
  while (state->running.load(std::memory_order_acquire)) {
    std::function<void()> task;
    {
      std::unique_lock<std::mutex> lock(state->mutex);
      if (state->cv.wait_for(lock, state->interval,
                             [state]() { return !state->running.load(std::memory_order_acquire); })) {
        break;                                   // 谓词为真 = 被 Stop 叫醒
      }
      task = state->task;                        // 锁内取快照
    }
    if (!task) { continue; }
    try {
      task();                                    // 锁外执行 + 异常兜底
    } catch (const std::exception &e) {
      HIXL_LOGE(FAILED, "Periodic task callback threw exception:%s", e.what());
    } catch (...) {
      HIXL_LOGE(FAILED, "Periodic task callback threw unknown exception.");
    }
  }
}
```

[periodic_task.cc:L59-L81](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/periodic_task.cc#L59-L81)。`wait_for` 带谓词时返回值的含义要读准：返回 true 表示**谓词成立**（即被停止），返回 false 表示**超时到达**（该干活了）。取任务快照后立刻出锁执行，回调再慢也不会挡住 `Stop` 抢锁。

`Start` 的幂等与 `Stop` 的先改状态再唤醒，见 [periodic_task.cc:L23-L53](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/periodic_task.cc#L23-L53)；析构函数自动 `Stop`（L19-L21），成员式使用（如 `dump_task_`）无需手写清理。

#### 4.3.4 代码实践

1. 实践目标：确认 PeriodicTask 的三个行为合同——参数校验、幂等 Start、Stop 即时生效。
2. 操作步骤：对照 [periodic_task.cc:L23-L37](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/periodic_task.cc#L23-L37) 写出三个假想用例的断言：① `Start(0ms, f)` 应返回 `PARAM_INVALID`；② `Start(100ms, f)` 后再次 `Start(50ms, g)` 应返回 `SUCCESS` 且原任务继续按 100ms 跑；③ `Start(3600000ms, f)`（1 小时周期）后立刻 `Stop()`，进程应几乎瞬间结束而不等 1 小时。
3. 需要观察的现象：第 ③ 点最能体现设计——如果实现用的是 `sleep_for`，测试进程会挂满 1 小时；用 `wait_for` 谓词版则毫秒级退出。
4. 预期结果：三个断言均与源码行为一致。仓库 tests 目录下暂无 period_task 专属单测，以上为「源码阅读型实践」，运行结果待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Run` 里要在锁内把 `state->task` 拷出来再执行，而不是直接调 `state->task()`？

答案：`Stop` 会把 `state->task` 置为 nullptr（[periodic_task.cc:L47](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/periodic_task.cc#L47)）。若持锁调用回调，`Stop` 抢不到锁，无法及时打断；若不持锁直接访问成员，则与 `Stop` 的清空构成数据竞争。锁内拷贝快照、锁外执行，既消除竞争又不阻塞停止路径。

**练习 2**：PeriodicTask 与「在 ThreadPool 里 commit 一个自循环任务」相比，各自适合什么场景？

答案：`PeriodicTask` 适合长期、固定节奏的后台工作（心跳、统计 dump），特点是单线程独占、可即时停止、异常不外泄；`ThreadPool` 适合大量离散的一次性任务，追求吞吐和弹性并发。把周期任务塞进线程池会长期占用一个线程席位，破坏池的弹性语义。

## 5. 综合实践

**任务：为一组远端设计并发建链方案并验证。**

假设你的 client 实例需要同时与 16 个远端 engine 建链（PD 分离中 Decoder 连接 16 个 Prompt 的典型规模），请完成：

1. **方案选型**（源码阅读）：分别评估三种做法——逐个同步 `Connect`、`ConnectPoolExecutor`（默认 2 线程）、把 `engine_->Connect` 包成 lambda 丢给自建 `ThreadPool("grp", 16, 16)`。从总耗时、失败传播、context 处理三个维度对比。提示：前两者直接复用 [hixl_impl.cc:L142-L154](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_impl.cc#L142-L154) 与 [ub_client_handler.cc:L244-L268](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/ub_client_handler.cc#L244-L268) 的现成模式。
2. **参数推演**：设单次建链超时 3 秒、16 个远端中 1 个必然超时。计算默认 2 线程下最坏总耗时（提示：15 个成功任务分两路并行 + 1 个超时任务，量级为 \(\lceil 16/2 \rceil \times 3\text{s}\) 上下界分析），据此给出 `connect_pool.thread_num` 的建议值，并写出对应的 `OPTION_GLOBAL_RESOURCE_CONFIG` JSON 片段。
3. **实验验证**（如本地有双 device 环境）：改写 u1-l3 的 quickstart，client 侧用 `ConnectAsync` + 批量 `GetAsyncConnectStatus` 轮询替代同步 `Connect`，打印每个远端从 `CONNECT_PENDING` 到终态的状态迁移序列；对比同步版本的总建链耗时。无硬件环境时，改为在 [hixl_api_unittest.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/cpp/hixl/engine/hixl_api_unittest.cc) 中追找一个已有的 `ConnectAsync` 调用点，说明桩环境下它为何能立刻返回 `SUCCESS`（答案线索：`Submit` 只做入队，不等待任务执行）。

## 6. 本讲小结

- `ConnectPoolExecutor` 是异步建链的专属执行器：`ConnectAsync` 只做「置 `*_PENDING` + 入队」，真正的同步 `Connect` 在 worker 线程上执行，结果写入 `remote_engine → AsyncConnectStatus` 状态表。
- 其并发控制三规则：同远端串行（`task_doing_set_`）、异远端并行（N 个 worker）、队列有界（默认 128，满则 `RESOURCE_EXHAUSTED`）；线程数与容量可经 `connect_pool.thread_num/task_queue_capacity` 调整（1..64 / 1..65535）。
- 状态表有「用户最新操作优先」的防竞态规则：落后任务不得把 `*_PENDING` 倒退回旧终态；worker 全程持用户线程捕获的 ACL context，保证 CS 层调用落到正确 device。
- `ThreadPool` 是通用弹性线程池（核心线程常驻 + 临时线程一事一毕），`commit` 返回 `std::future`，`Destroy` 排空队列不丢任务——与连接池的「丢弃式」关停形成对照。
- `PeriodicTask` 用 `wait_for` 谓词等待实现即时可停，回调异常被兜底转日志，周期任务的三个质量样板：参数校验、幂等 Start、析构自动 Stop。
- 手写执行器的两条铁律在本讲反复出现：临界区只做摘取/归还、干活在锁外；跨线程执行设备相关操作前先恢复 context。

## 7. 下一步学习建议

- **下一讲 u3-l5（Proxy 层）**将离开线程模型，转向引擎如何用 dlopen 封装 DCMI/DSM/HAL 等昇腾底层接口，理解 4.1 节中 worker 线程恢复 context 之后真正调到的硬件探测代码在哪里。
- 想看异步建链的消费端，可先读 [tests/cpp/hixl/engine/hixl_api_unittest.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/cpp/hixl/engine/hixl_api_unittest.cc) 中 `TestInitializeRollsBackEngineWhenConnectPoolInitializationFails` 与 `TestAlreadyConnectedFailed`，把状态机断言与 u2-l4 的七态图对照。
- 进入单元四（CS 通信服务）前，建议重读本讲的 `WorkerHandler` 主循环——CS 层的 `MsgReceiver` 事件循环与它同属「条件变量 + 谓词 + 锁外执行」家族，届时可互相印证。

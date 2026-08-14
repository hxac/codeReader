# 连接池执行器与线程模型

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `ConnectPoolExecutor` 的任务模型：任务队列、按 `remote_engine` 去重、状态表三件套如何协作。
2. 解释异步建链的并发控制策略：为什么「同一个远端同一时刻只执行一个任务」，以及 SetStatus 的防竞态规则。
3. 掌握 `ThreadPool`（可弹性扩缩的核心+临时线程池）和 `PeriodicTask`（可中断的周期任务）两个公共并发组件的用法与适用场景。
4. 能够阅读 tests/cpp/hixl 下与 engine 相关的单测，并独立写出异步建链的时序说明。

## 2. 前置知识

本讲是专家层内容，假设你已学完 u3-l2（ClientHandler 体系）并了解 u2-l4（建链与断链）。再补充几个并发编程基础概念：

- **生产者-消费者模型**：一类线程往队列里放任务（生产者），另一类线程从队列取任务执行（消费者）。中间用「互斥锁 + 条件变量」协调：互斥锁保护队列不被并发改坏，条件变量让消费者在队列空时休眠、有任务时被唤醒。
- **条件变量的谓词等待**：`cv.wait(lock, pred)` 表示「拿到锁后检查 pred，不满足就释放锁睡眠，直到被唤醒且 pred 满足才返回」。这是避免「虚假唤醒」造成错误的标准写法。
- **ACL 上下文（aclrtContext）**：昇腾运行时的线程级上下文。ACL 的许多接口要求「调用它的线程绑定了上下文」，因此在工作线程里执行设备相关操作前，必须先 `SetCurrentContext`。
- **`std::future` / `packaged_task`**：C++ 标准库的异步结果传递机制——把可调用对象打包成 task，通过 future 在提交方拿到返回值。
- **回顾一个关键事实**（u2-l4 已讲）：`ConnectAsync` 提交的任务内部执行的仍然是同一个同步 `Connect`，异步只是把「阻塞等待」从用户线程搬到了池里的工作线程。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `src/hixl/engine/connect_pool_executor.h/.cc` | 异步建链/断链任务执行器：任务队列 + 工作线程 + 状态表 |
| `src/hixl/common/thread_pool.h/.cc` | 通用弹性线程池：核心线程 + 临时线程，支持 future 返回值 |
| `src/hixl/common/periodic_task.h/.cc` | 周期任务：单线程按固定间隔执行回调，可随时停止 |
| `src/hixl/engine/hixl_impl.cc` | `ConnectPoolExecutor` 的使用方：ConnectAsync/DisconnectAsync 在这里提交任务 |
| `src/hixl/engine/ub_client_handler.cc` | `ThreadPool` 的使用方：UB 多链路并发建链 |
| `src/hixl/cs/msg_handler.cc` | `ThreadPool` 的另一个使用方：CS server 侧消息处理 |
| `src/hixl/fabric_mem/fabric_mem_statistic.cc/.h` | `PeriodicTask` 的使用方：统计信息周期落盘 |
| `tests/cpp/hixl/engine/hixl_api_unittest.cc` | 覆盖 ConnectPoolExecutor 初始化回滚、ConnectAsync 的单测 |

三者的分工一句话概括：`ConnectPoolExecutor` 是「面向建链场景的专用执行器」，`ThreadPool` 是「面向通用计算任务的弹性线程池」，`PeriodicTask` 是「面向定时巡检的单线程闹钟」。`ConnectPoolExecutor` 并不基于 `ThreadPool` 实现，它自带一套更简单的固定线程 worker 循环——这是本讲的一个重要观察。

## 4. 核心概念与源码讲解

### 4.1 ConnectPoolExecutor：异步建链的任务执行器

#### 4.1.1 概念说明

u2-l4 讲过：`ConnectAsync` 入队即返回，真正的建链由后台完成，用户用 `GetAsyncConnectStatus` 轮询七态状态机（NOT_CONNECT / CONNECT_PENDING / CONNECTING / CONNECTED / CONNECT_FAILED / DISCONNECT_PENDING / DISCONNECTING）。本讲回答「后台是谁」——就是 `ConnectPoolExecutor`。

它要解决三个问题：

1. **在哪个线程执行同步 Connect？** 建链涉及 endpoint 拉取、socket 握手等阻塞操作，不能占用用户调用线程，需要专门的工作线程池。
2. **多个远端同时建链怎么办？** 大规模组网（如 PD 分离中一台 Prompt 要连多台 Decoder）需要并行建链，但**同一个远端的两个任务并行执行毫无意义甚至有害**（两个 Connect 同时操作同一个 ClientHandler），必须按远端串行。
3. **状态如何与用户操作保持一致？** 用户先 `ConnectAsync(A)` 又立刻 `DisconnectAsync(A)`，状态表不能被先完成的旧任务翻回去。

#### 4.1.2 核心流程

一次 `ConnectAsync("ip:port")` 的完整生命周期：

```text
用户线程                                工作线程 (worker 0..N-1)
────────                                ───────────────────────
Hixl::ConnectAsync
  └─ HixlImpl::ConnectAsync
       ├─ 构造 task lambda
       │    （内部调用 engine_->Connect，
       │     完成后 SetStatus(CONNECTED/CONNECT_FAILED)）
       └─ ConnectPoolExecutor::Submit
            ├─ 加 task_queue_mutex_
            ├─ 未初始化? → FAILED
            ├─ 队列满(≥capacity)? → RESOURCE_EXHAUSTED
            ├─ SetStatus(CONNECT_PENDING)      ── 记入状态表
            ├─ task_list_.emplace_back         ── 入队尾
            └─ notify_one                      ══╗
返回 SUCCESS（不代表连上）                        ╔═唤醒
                                              cv.wait 醒来
                                              ├─ 遍历 task_list_ 找第一个
                                              │   remote_engine 不在
                                              │   task_doing_set_ 中的任务
                                              ├─ task_doing_set_.insert   ── 占位
                                              ├─ SetStatus(CONNECTING)
                                              ├─ 取出任务、出队
                                              └─ 释放锁后执行 task()
                                                   └─ engine_->Connect(...)
                                                        └─ SetStatus(CONNECTED)
                                              ├─ 重新加锁
                                              ├─ task_doing_set_.erase    ── 释放占位
                                              └─ notify_one（唤醒可能
                                                 被占位阻塞的其它 worker）
```

两个设计要点：

- **锁的粒度**：`task()`（真正的建链，可能阻塞数秒）在**锁外**执行；锁只保护队列和占位集合的短临界区。
- **两把锁分离**：`task_queue_mutex_` 保护任务队列/占位集，`task_result_mutex_` 保护状态表，二者独立加锁，状态读写不会和任务调度互相阻塞。

#### 4.1.3 源码精读

先看数据模型。任务是一个三元组，执行器内部维护四块状态：

[connect_pool_executor.h:L29-L71](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/connect_pool_executor.h#L29-L71)：定义 `ConnectPoolExecutorTask{is_connect, remote_engine, task}` 与 `ConnectPoolExecutor` 类。`task_list_` 是待执行队列（`std::list`，支持中途摘除任意元素）；`task_doing_set_` 是「正在执行」的远端集合；`task_result_` 是以远端为 key 的状态表；`ctx_` 是捕获的 ACL 上下文，用于在工作线程里恢复设备上下文。

初始化时确定线程数与队列容量，默认值与上下限如下：

[connect_pool_executor.cc:L16-L22](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/connect_pool_executor.cc#L16-L22)：默认 2 个工作线程、队列容量 128；合法范围线程数 [1, 64]、容量 [1, 65535]，越界直接 `PARAM_INVALID`。

[connect_pool_executor.cc:L33-L60](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/connect_pool_executor.cc#L33-L60)：`Initialize` 从 `HixlOptions` 的 `GlobalResourceCfg` 读取可选的 `connect_pool.thread_num` / `connect_pool.task_queue_capacity`（缺省用默认值），校验范围后**先捕获当前线程的 ACL 上下文再启动 worker**——顺序很关键：worker 一启动就会用到 ctx，所以捕获必须发生在 spawn 之前。

`Submit` 是唯一的生产者入口：

[connect_pool_executor.cc:L83-L102](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/connect_pool_executor.cc#L83-L102)：三步检查（已初始化、队列未满）后，先把状态置为 `CONNECT_PENDING`/`DISCONNECT_PENDING`（**入队即置 PENDING**，这样用户在任务真正执行前查询就能看到「已受理」而非 NOT_CONNECT），再入队尾并 `notify_one`。队列满返回 `RESOURCE_EXHAUSTED`——这是 u2-l2 讲过的「可恢复错误」，等一会重试即可。

worker 循环是并发控制的精华：

[connect_pool_executor.cc:L146-L203](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/connect_pool_executor.cc#L146-L203)：`WorkerHandler` 首先在工作线程里恢复 ACL 上下文（L148），然后进入主循环：

- L149-L160 谓词函数：`IsInitialized()` 为 false（要关停）或**队列里存在某个未被执行中任务占用的远端**时才醒。注意「有可执行的任务才醒」——如果队列里的远端全都在 `task_doing_set_` 中，worker 继续睡，避免空转。
- L177-L187 挑任务：从头遍历队列，跳过所有 `remote_engine` 已在 `task_doing_set_` 中的任务，找到第一个空闲远端，**插入占位集合 → 置 CONNECTING/DISCONNECTING → 摘出队列**，三步在同一临界区内完成。
- L190-L200 在锁外执行 `task()`，执行完重新加锁清除占位并 `notify_one`——唤醒其它可能正等待该远端释放的 worker。

`SetStatus` 的防竞态规则（u2-l4 提过「用户最新操作优先」，这里看实现）：

[connect_pool_executor.cc:L104-L126](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/connect_pool_executor.cc#L104-L126)：两条丢弃规则——(a) 要写 CONNECTING/CONNECTED/CONNECT_FAILED，但当前是 `DISCONNECT_PENDING`：说明用户已提交断链，旧的建链任务结果不应覆盖，直接 return；(b) 要写 DISCONNECTING/NOT_CONNECT，但当前是 `CONNECT_PENDING`：说明用户在建链排队期间又提交了断链又被更晚的操作覆盖……实际含义是：**排队中的更新操作比执行中的旧任务结果优先**。另外 `NOT_CONNECT` 不是写入而是 erase（L121-L122），让状态表自动「遗忘」已断开的远端，避免无限膨胀。

最后看使用方如何提交任务：

[hixl_impl.cc:L144-L166](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_impl.cc#L144-L166)：`ConnectAsync` 构造的 lambda 内部调用同步 `engine_->Connect`，把返回码翻译成 `CONNECTED`/`CONNECT_FAILED` 写入状态表（`ALREADY_CONNECTED` 也算成功）；`DisconnectAsync` 同理，完成后置 `NOT_CONNECT`。这印证了 u2-l4 的结论：**异步接口 = 同步实现 + 后台执行 + 状态表**。

[hixl_impl.cc:L92-L98](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_impl.cc#L92-L98)：`Initialize` 中连接池初始化失败会回滚（`engine_->Finalize()` 并 reset），不留半初始化状态；[hixl_impl.cc:L102-L110](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_impl.cc#L102-L110)：`Finalize` 先 `Shutdown` 连接池再终结引擎——顺序保证工作线程不会引用已销毁的引擎。

关停流程：

[connect_pool_executor.cc:L62-L81](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/connect_pool_executor.cc#L62-L81)：`Shutdown` 置 `is_initialized_ = false` 并 `notify_all`，worker 在谓词检查处发现未初始化即 break 退出（**队列中剩余任务被丢弃**，日志会打印 remain 数量），最后 join 全部 worker。析构函数自动调用 Shutdown（L27-L31），防止漏关。

#### 4.1.4 代码实践

**实践：用单测验证连接池参数校验与回滚**

1. **实践目标**：通过现成单测理解 ConnectPoolExecutor 的初始化约束和它在 Hixl 生命周期中的位置。
2. **操作步骤**：
   - 打开 [tests/cpp/hixl/engine/hixl_api_unittest.cc:L295-L307](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/cpp/hixl/engine/hixl_api_unittest.cc#L295-L307)，阅读 `TestInitializeRollsBackEngineWhenConnectPoolInitializationFails`。
   - 在测试环境（无需真实 NPU，测试用 stub 桩，参见 u1-l2）执行：`bash tests/run_test.sh -s hixl`。
   - 想单独跑该用例，可在 `build_test` 目录下用 `--gtest_filter` 过滤（gtest 标准用法）。
3. **需要观察的现象**：该用例先传入 `connect_pool.thread_num = "0"`（低于下限 1），断言 `Initialize` 返回 `PARAM_INVALID`——这正是 [connect_pool_executor.cc:L46-L47](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/connect_pool_executor.cc#L46-L47) 的范围检查；随后清空 options 重新 Initialize 成功，并 `ConnectAsync` 提交一个任务，验证回滚后引擎可以正常重新初始化。
4. **预期结果**：测试通过；日志中能看到 `ConnectPoolExecutor initialize start/success`、`submit task connect to ...` 等 L34/L58/L98 处的日志。
5. 若本地无法运行，标注「待本地验证」，改为纯源码阅读：对照断言逐一找出对应的检查代码行。

#### 4.1.5 小练习与答案

**练习 1**：如果用户对同一远端快速连续调用两次 `ConnectAsync`，会发生什么？会创建两个连接吗？

**答案**：不会。两次 `Submit` 会入队两个任务，但 worker 的 `task_doing_set_` 保证同一远端串行：第一个任务执行时第二个在队列等待。第二个任务执行时内部 `engine_->Connect` 返回 `ALREADY_CONNECTED`（u2-l4 讲过的幂等语义），lambda 把它翻译成 `CONNECTED` 状态，效果等价于一次建链。状态表按远端为 key，天然去重。

**练习 2**：为什么 `task()` 必须在锁外执行？如果放进锁内会怎样？

**答案**：`task()` 内部是同步建链，可能阻塞数百毫秒甚至到超时上限。若在 `task_queue_mutex_` 临界区内执行，期间所有其它 worker 的取任务、用户的 `Submit`（新的 ConnectAsync）全部被阻塞，整个连接池退化为单线程甚至卡死；`Shutdown` 也拿不到锁。锁外执行 + `task_doing_set_` 占位，既保证同远端串行，又不阻塞无关远端的调度。

**练习 3**：`Submit` 返回 `SUCCESS` 意味着连接成功吗？用户该如何判断真正的结果？

**答案**：不意味着。`SUCCESS` 只表示任务已受理入队（状态已置 `CONNECT_PENDING`）。真正的结果要通过 `GetAsyncConnectStatus` 轮询，直到状态变为终态 `CONNECTED` 或 `CONNECT_FAILED`。这和 u2-l5 讲过的「传输查询返回 SUCCESS 不等于传输成功」是同一种设计哲学：接口返回码表示「调用本身是否被受理」，业务结果经出参/状态表传递。

### 4.2 ThreadPool：可弹性扩缩的通用线程池

#### 4.2.1 概念说明

`ConnectPoolExecutor` 是建链专用的；引擎里还有大量「普通异步计算任务」需要一个通用线程池。`src/hixl/common/thread_pool.h` 提供的 `ThreadPool` 有两个 `ConnectPoolExecutor` 没有的能力：

1. **返回值支持**：`commit` 是模板函数，返回 `std::future`，提交方可以拿到任务返回值——比如 UB 建链需要知道每条链路的成败。
2. **弹性伸缩**：核心线程（min_size）常驻；当无空闲线程且未到 max_size 时，为每个新任务加一个**临时线程**，临时线程执行完一个任务就退出。适合突发洪峰（如 CS server 瞬时涌入大量连接请求）而不想常驻太多线程的场景。

#### 4.2.2 核心流程

```text
commit(task):
  打包为 packaged_task 入队 tasks_
  if (无空闲线程 && 总线程 < max_size):
      AddTemporaryThread()        # 弹性扩容
  notify_one

ThreadFunc（核心/临时两种身份）:
  while 未停止:
      PopTask()  # 无任务则睡眠
      执行 task
      if 是临时线程: 线程数减一，直接退出   # 用完即弃
      else: 回到等待                        # 常驻循环
```

临时线程的生命周期是「一任务一线程」：执行完当前任务（或池已停止且队列空）就退出，并通过 `finished` 标志让主线程稍后回收 join。

#### 4.2.3 源码精读

[thread_pool.h:L40-L76](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/thread_pool.h#L40-L76)：`commit` 模板——把任意可调用对象和参数 bind 成 `packaged_task`，入队后返回 future；L63-L70 是扩容决策：`idle == 0 && 队列非空 && total < max` 时调用 `AddTemporaryThread`。池已停止时返回空 future（调用方需检查 future 有效性）。

[thread_pool.cc:L14-L28](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/thread_pool.cc#L14-L28)：构造函数钳位参数（min 至少 1，max 不小于 min），启动 min_size 个核心线程，并初始化 idle/busy/total 三个计数器。

[thread_pool.cc:L72-L87](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/thread_pool.cc#L72-L87)：`AddTemporaryThread`——先清理已退出的旧临时线程（join 并从 list 摘除），再创建新临时线程，线程索引沿用递增的 `total_thrd_num_`。

[thread_pool.cc:L127-L165](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/thread_pool.cc#L127-L165)：`ThreadFunc` 是核心/临时线程共用的主循环。注意几个细节：L133 给线程设置名字（`前缀_core_序号` / `前缀_temp_序号`），方便 top/pstack 排查；L146-L160 用 idle/busy 计数区分身份——临时线程执行完一个任务（L153-L157）或池停止（L139-L145）就自减 total 并退出，核心线程则回到 `PopTask` 继续等。L162-L163 置 finished 标志供主线程回收。

[thread_pool.cc:L102-L111](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/thread_pool.cc#L102-L111)：`PopTask` 的谓词是 `is_stopped || !tasks_.empty()`；L105 `if (is_stopped_ && tasks_.empty()) return false`——**停止后仍会排干队列中剩余任务**才退出，这一点与 ConnectPoolExecutor 的「丢弃剩余任务」策略不同，值得对比记忆。

两个真实使用点：

- [ub_client_handler.cc:L250-L254](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/ub_client_handler.cc#L250-L254)：UB handler 对同一远端的**多条 CommType 链路**并发建链——按 handle 数量开一个局部 `ThreadPool("ub_connect", handles.size())`，每个 future 对应一条链路的建链结果，用 future 收集回写 `connected_types_`。这是「用 future 拿返回值」的典型范例。
- [msg_handler.cc:L24-L30](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/msg_handler.cc#L24-L30)：CS server 侧消息处理器用 `ThreadPool("cs_server", 4, 1024)` 处理控制面请求——4 个常驻 + 上限 1024 的巨大弹性空间，正对应「server 可能瞬时收到大量连接/注册消息」的洪峰场景。

#### 4.2.4 代码实践

**实践：阅读 ThreadPool 单测并理解弹性行为**

1. **实践目标**：通过单测断言理解核心/临时线程的伸缩行为。
2. **操作步骤**：
   - 打开 [tests/cpp/hixl/common/thread_pool_ut.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/cpp/hixl/common/thread_pool_ut.cc)，找出验证「临时线程扩容」与「停止后排干任务」的用例。
   - 运行 `bash tests/run_test.sh -s hixl`（stub 环境，无需 NPU）。
3. **需要观察的现象**：日志中 `[ThreadPool:...] add temp thread, idx:...`（对应 [thread_pool.cc:L85](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/thread_pool.cc#L85)）与 `temp thread exit after task`（L155）成对出现，验证「一任务一临时线程」。
4. **预期结果**：测试通过；能在报告中确认提交 N 个任务时 total 线程数的上升与回落轨迹。
5. 无法运行则标注「待本地验证」，改为源码阅读：手动跟踪一次 `commit` → 扩容 → 临时线程退出 → `CleanupFinishedTempThreads` 回收的全过程。

#### 4.2.5 小练习与答案

**练习 1**：`ThreadPool` 与 `ConnectPoolExecutor` 的 worker 循环都用了条件变量，二者的唤醒谓词有何本质区别？

**答案**：ThreadPool 的谓词是「池停止或队列非空」——任何任务都可被任何线程执行；ConnectPoolExecutor 的谓词还多一层「队列中存在未被占用的远端」——因为同一远端同一时刻只允许一个任务执行，即使队列非满非空，worker 也可能因全部远端都在执行中而继续睡眠。前者是纯生产者-消费者，后者是「带资源占位的串行化调度」。

**练习 2**：`ThreadPool::Destroy` 和 `ConnectPoolExecutor::Shutdown` 对未执行任务的处理策略分别是什么？为什么可以这样差异？

**答案**：ThreadPool 停止后仍会执行完队列中剩余任务（L105 的 `is_stopped_ && tasks_.empty()` 才退出）；ConnectPoolExecutor 则直接丢弃剩余任务并打印 remain 数量。原因在于任务性质：线程池里的多是已受理的计算任务（如消息处理），丢弃会造成请求丢失；而建链任务在 Finalize 时引擎本身都要销毁了，连接不再有意义，且用户本就应通过状态表轮询结果，丢弃后状态停在 PENDING 也随对象一起消亡。

### 4.3 PeriodicTask：可中断的周期任务

#### 4.3.1 概念说明

第三类并发需求是「每隔固定时间做一件事」：心跳探活（u2-l4 提过 ClientManager 10 秒心跳）、统计信息周期落盘等。最朴素的写法是 `while(true) { sleep(interval); do(); }`，但它有个致命缺陷：**无法及时停止**——`Stop` 时最多要等一个 interval 才能退出。`PeriodicTask` 用 `condition_variable::wait_for` 替代 sleep：等待期间任何时刻 `notify` 都能立刻打断，实现「秒级可停止的周期执行」。

#### 4.3.2 核心流程

```text
Start(interval, task):
  校验 interval > 0、task 非空
  若已在运行 → 直接返回 SUCCESS（幂等）
  保存 interval/task，置 running = true，启动工作线程

Run(state):                       # 工作线程
  while running:
      wait_for(lock, interval, [&]{ return !running; })
      # 若因 Stop 被唤醒（running=false）→ break，不执行本轮
      取出 task 副本（持锁）
      在锁外执行，并捕获所有异常（防止单次异常杀死循环线程）

Stop():
  置 running = false，清空 task，notify_all，join 工作线程
```

#### 4.3.3 源码精读

[periodic_task.h:L39-L50](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/periodic_task.h#L39-L50)：状态封装在 `shared_ptr<State>` 里——`Run` 是静态函数，线程入口持有 State 的共享所有权，即例 `PeriodicTask` 对象先析构，工作线程仍能安全访问 State（Stop 中先 join 再返回，实际两者生命周期同步）。

[periodic_task.cc:L23-L37](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/periodic_task.cc#L23-L37)：`Start` 校验参数、幂等检查后启动线程；注意 L33 是 `state->task = std::move(task)`，此后每次执行从 State 取最新回调。

[periodic_task.cc:L59-L81](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/periodic_task.cc#L59-L81)：`Run` 主循环——L64-L67 `wait_for` 带谓词「!running」，返回 true 表示是被 Stop 打断的，直接 break；L68 在持锁状态下取出 task **副本**后释放锁，L73-L79 在锁外执行且 catch 所有异常并记日志——单次回调抛异常不会杀死巡检线程，这是后台任务的健壮性基本功。

[periodic_task.cc:L39-L53](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/periodic_task.cc#L39-L53)：`Stop` 置 running=false、清空 task、notify_all 后 join——由于 Run 用的是 `wait_for`，即使在 interval 中段也能瞬间被唤醒退出，最长等待只是一次回调的执行时长而非一个周期。

典型使用点（FabricMem 统计落盘）：

[fabric_mem_statistic.cc:L149-L155](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_statistic.cc#L149-L155)：`StartPeriodicDump` 用 `dump_task_.Start(kStatisticTimerPeriodMs, [this]{ Dump(); })` 周期打印传输统计（成员声明见 [fabric_mem_statistic.h:L84](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_statistic.h#L84)）；析构函数会自动 `Stop()`（[periodic_task.cc:L19-L21](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/periodic_task.cc#L19-L21)），成员式持有即可做到 RAII。

#### 4.3.4 代码实践

**实践：对比 sleep 轮询与 wait_for 的停止延迟**

1. **实践目标**：直观理解 PeriodicTask「可中断」的价值。
2. **操作步骤**（示例代码，非项目原有，可放在任意 C++ 练习文件中编译运行，不修改本仓库源码）：
   ```cpp
   #include <chrono>
   #include <iostream>
   #include "common/periodic_task.h"

   int main() {
     hixl::PeriodicTask task;
     int n = 0;
     task.Start(std::chrono::milliseconds(5000), [&n]() { std::cout << "tick " << ++n << "\n"; });
     std::this_thread::sleep_for(std::chrono::milliseconds(600)); // 远小于一个周期
     auto t0 = std::chrono::steady_clock::now();
     task.Stop();
     auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                 std::chrono::steady_clock::now() - t0).count();
     std::cout << "Stop() took " << ms << "ms\n";  // 期望接近 0 而非 4400ms
     return 0;
   }
   ```
3. **需要观察的现象**：`Stop()` 几乎立即返回（毫秒级），且没有输出任何 tick；若把 Start 换成 `while(running) std::this_thread::sleep_for(5s)` 的朴素实现，Stop 需要等到本轮 sleep 结束。
4. **预期结果**：打印 `Stop() took 0ms`（或个位数毫秒）。
5. 若无法编译（需要链接 hixl common 库），标注「待本地验证」，改为阅读 [periodic_task.cc:L64-L67](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/periodic_task.cc#L64-L67) 并口头推演两种实现的停止时序。

#### 4.3.5 小练习与答案

**练习 1**：PeriodicTask 为什么把 task 存在 State 里、由静态 `Run` 访问，而不是像 ConnectPoolExecutor 那样直接访问成员？

**答案**：为了解耦对象生命周期与线程生命周期。`Run` 持有 `shared_ptr<State>`，即使 PeriodicTask 对象本身被移动/析构（析构里 Stop 已保证 join，但 shared_ptr 提供额外保险），线程访问的 State 依然有效，避免悬挂引用。ConnectPoolExecutor 的 worker 用 `this` 捕获，依赖「Shutdown 先 join 再销毁对象」的顺序保证，两种做法都正确，但前者更防御。

**练习 2**：如果周期回调执行时间超过 interval，PeriodicTask 会发生什么？会并发重入吗？

**答案**：不会重入。`Run` 是单线程顺序循环：wait_for 返回后同步执行 task，执行完才进入下一轮 wait_for。回调超时只会导致下一轮被推迟（周期漂移），绝不会出现两个回调并发执行，因此回调内部无需考虑自并发。

## 5. 综合实践

**任务：为一次「并发异步建链」写出完整时序说明，并做一张三个并发组件的选型对照表。**

具体步骤：

1. **构造场景**：一台 Prompt（client 角色）需要连接 3 台 Decoder（`10.1.1.10:26200`、`10.1.1.11:26200`、`10.1.1.12:26200`），模拟代码（示例代码）对三个远端连续调用 `ConnectAsync`，随后轮询 `GetAsyncConnectStatus(map 版本)` 直到全部终态。
2. **源码侧**：对照 [hixl_impl.cc:L144-L154](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_impl.cc#L144-L154) 与 [connect_pool_executor.cc:L83-L203](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/connect_pool_executor.cc#L83-L203)，画出时序图，要求标出：三次 Submit 的 PENDING 写入、默认 2 个 worker 如何并行领取两个不同远端、第三个任务在队列中的等待、每个任务锁外的 Connect 与终态写入。
3. **分析单测**：阅读 [hixl_api_unittest.cc:L295-L307](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/cpp/hixl/engine/hixl_api_unittest.cc#L295-L307)，用 2-3 句话写清每个 `EXPECT_EQ` 断言要保护的契约（提示：第一个断言保护「连接池参数越界 → Initialize 整体失败并回滚」，第二个保护「回滚后可重新初始化」，第三个保护「异步提交被受理」）。
4. **输出选型表**（参考答案框架）：

| 维度 | ConnectPoolExecutor | ThreadPool | PeriodicTask |
|---|---|---|---|
| 线程模型 | 固定 N 线程（默认 2） | 核心 + 弹性临时线程 | 单线程 |
| 返回值 | 无（结果走状态表） | `std::future` | 无 |
| 串行化约束 | 同一 remote_engine 串行 | 无 | 单线程天然串行 |
| 停止策略 | 丢弃剩余任务 | 排干剩余任务 | 立即中断等待 |
| 典型用户 | ConnectAsync/DisconnectAsync | UB 多链路建链、CS server 消息 | 心跳、统计落盘 |

5. **进阶（可选）**：把 `connect_pool.thread_num` 配置为 1，推演时序图如何变化（三个远端完全串行建链，总耗时约为三者之和），说明线程数对批量建链吞吐的影响。

## 6. 本讲小结

- `ConnectPoolExecutor` 是异步建链的专用执行器：`Submit` 入队即置 PENDING 并返回，worker 线程在锁外执行同步 Connect，结果写入按远端索引的状态表；队列满返回可恢复的 `RESOURCE_EXHAUSTED`。
- 核心并发策略是 `task_doing_set_` 占位：同一 `remote_engine` 同一时刻只有一个任务在执行，不同远端并行；锁只保护短临界区，长阻塞操作全部在锁外。
- `SetStatus` 的防竞态规则保证「用户最新操作优先」：排队中的断链请求不被滞后的旧建链结果覆盖，`NOT_CONNECT` 用 erase 让状态表自动收缩。
- `ThreadPool` 是通用弹性线程池：`commit` 返回 future，无空闲线程且未到上限时按需创建「一任务一线程」的临时线程，适合 CS server 这类洪峰场景；停止时会排干队列。
- `PeriodicTask` 用 `wait_for` + 谓词替代 sleep，实现秒级可停止的周期执行，回调异常被捕获不会杀死循环线程，典型用途是统计落盘等巡检任务。
- 三者生命周期都遵循 RAII/显式 Shutdown：HixlImpl 的 Finalize 先关连接池再终结引擎，Initialize 失败则回滚，不留半初始化状态。

## 7. 下一步学习建议

- 下一讲 u3-l5「Proxy 层：屏蔽昇腾硬件差异」将离开并发主题，进入引擎如何用 dlopen 动态封装昇腾系统接口。
- 想继续深挖并发的读者，建议顺藤摸瓜阅读：`OptionalAclrtContext`（`src/hixl/common/optional_aclrt_context.h`，本讲反复出现的上下文捕获/恢复组件）和 [client_manager.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/client_manager.cc) 中 per-engine 互斥与心跳探活的实现（u2-l4 的下沉版）。
- 建议同时跑一遍 [tests/cpp/hixl/common/thread_pool_ut.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/cpp/hixl/common/thread_pool_ut.cc) 与 [tests/cpp/hixl/engine/hixl_api_unittest.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/cpp/hixl/engine/hixl_api_unittest.cc)，把本讲的时序推演和真实日志互相印证。

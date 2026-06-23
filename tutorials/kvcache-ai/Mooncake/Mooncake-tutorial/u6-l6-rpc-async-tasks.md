# RPC 服务与异步 Copy/Move/Drain 任务

> 阶段：advanced ｜ 依赖讲义：u5-l2、u5-l3
> 本讲聚焦 `mooncake-store`：Master 如何对外暴露 RPC、如何把「副本复制/迁移」抽象成异步任务下发给 Client 执行，以及 DrainJob 这个更高层的「批量迁移作业」是如何被切片、调度、重试并最终收尾的。

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清楚 `mooncake-store` 基于 `ylt/coro_rpc` 的 RPC 服务是如何搭建起来的，`struct_pack` 在其中扮演什么角色。
2. 描述 `ClientTaskManager` 内部「待派发 / 执行中 / 已完成」三张表的关系，以及任务状态机 `PENDING → PROCESSING → SUCCESS/FAILED` 的流转。
3. 跟踪一条 Copy/Move 任务从 `CreateCopyTask`/`CreateMoveTask` 创建、被 `FetchTasks` 拉取、在 Client 端 `ExecuteTask` 执行、再到 `MarkTaskToComplete` 回报的完整链路。
4. 画出 DrainJob 的状态流转图，理解它如何把「抽干一个 segment」拆成若干 Move 任务，以及它的三级失败/重试/超时统计。

## 2. 前置知识

在进入源码前，先建立几个直觉。

**RPC 与 coro_rpc。** Master 进程需要被很多 Client 调用（put/get、mount segment、查询副本等）。Mooncake 没有自己造 RPC 轮子，而是用了 [yaln/ylt](https://github.com/Alibaba/async_simple) 生态里的 `coro_rpc`：一个基于 C++ 协程的、编译期即可完成序列化注册的 RPC 框架。服务端只要写一个普通成员函数，再用 `server.register_handler<&类::方法>(&对象)` 注册一次，框架就能自动把它的参数和返回值序列化后通过网络传给客户端。这里的「自动序列化」用的就是 **struct_pack**——ylt 自带的、零运行时开销的二进制序列化库。

**struct_pack vs struct_json。** ylt 里有两个易混的序列化工具：
- `struct_pack`：二进制、紧凑、快，coro_rpc 默认用它序列化 RPC 报文。
- `struct_json`：把结构体转成可读 JSON 字符串。

两者都靠 `YLT_REFL(结构体名, 字段1, 字段2, ...)` 这个宏来做「字段反射」。你会看到本讲的请求/响应结构体（如 `CreateDrainJobRequest`）大量出现 `YLT_REFL`，它们既被 coro_rpc 用 struct_pack 走线，又有一个特别的字段 `Task::payload` 本身就是一个 struct_json 生成的 JSON 字符串（这样做是为了让任务内容可被日志/调试直接读出来）。

**tl::expected。** 几乎所有 RPC 方法的返回类型都是 `tl::expected<T, ErrorCode>`：成功时装着 `T`，失败时装着 `ErrorCode`。这是「带错误码的 optional」，比抛异常更明确，也比返回指针更安全。

**RAII 锁与读写锁。** 任务管理器用一把共享读写锁（`SharedMutex`）保护内部表。读多写少的场景下，并发读用共享锁、修改用独占锁。代码里把「拿到锁 + 访问表」封装成 RAII 对象 `ScopedTaskReadAccess` / `ScopedTaskWriteAccess`，构造时加锁、析构时解锁，避免手动管理锁的遗漏。

**UUID。** 任务、客户端、作业都用 `UUID` 唯一标识（`boost::hash<UUID>` 让它能放进哈希表）。

**「下发-拉取」异步模式。** Master 不主动推送任务，而是把任务放进「派发队列」；Client 启动一个轮询线程，每隔 1 秒主动 `FetchTasks` 拉取属于自己的任务，执行完再回报。这是经典的 pull 模型，避免了 Master 维护到每个 Client 的反向连接。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [mooncake-store/include/rpc_service.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/rpc_service.h) | 声明 `WrappedMasterService`（包在 `MasterService` 外面、专门给 coro_rpc 用的薄壳）和 `RegisterRpcService`。 |
| [mooncake-store/src/rpc_service.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/rpc_service.cpp) | `WrappedMasterService` 的实现：每个方法做「计时 + 指标 + 转发」；以及把全部方法注册进 coro_rpc server。 |
| [mooncake-store/src/master.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master.cpp) | Master 进程入口：构造 coro_rpc server、注册服务、启动。 |
| [mooncake-store/include/rpc_helper.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/rpc_helper.h) | `execute_rpc` 模板：统一处理「日志 + 指标 + 错误统计」。 |
| [mooncake-store/include/task_manager.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/task_manager.h) | 任务状态机、`Task` 结构、`ClientTaskManager` 及 RAII 访问器。 |
| [mooncake-store/src/task_manager.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/task_manager.cpp) | 提交/弹出/完成/清理任务的核心逻辑，以及任务表的序列化（用于 HA 快照）。 |
| [mooncake-store/include/master_service.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_service.h) | `MasterService` 真正的业务类；声明了 Copy/Move/Drain 的全部方法和 `DrainJob` 结构。 |
| [mooncake-store/src/master_service.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp) | Copy/Move 任务的创建、派发、完成；DrainJob 的创建、调度、收尾。 |
| [mooncake-store/include/rpc_types.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/rpc_types.h) | 跨进程的请求/响应结构体：`TaskAssignment`、`TaskCompleteRequest`、`CreateDrainJobRequest`、`QueryJobResponse` 等。 |
| [mooncake-store/src/client_service.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/client_service.cpp) | Client 端：轮询拉取任务、执行 Copy/Move、回报完成。 |
| [mooncake-store/include/transfer_task.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/transfer_task.h) | Client 执行 Copy/Move 时真正搬运数据的 `TransferSubmitter`（选 memcpy / transfer engine / 文件读等策略）。 |

## 4. 核心概念与源码讲解

### 4.1 coro_rpc 服务：WrappedMasterService 与 RegisterRpcService

#### 4.1.1 概念说明

`MasterService` 是一个又大又重的业务类，里面持有段管理、元数据、分配器、各种后台线程。把它直接暴露给 RPC 框架不太干净。于是 Mooncake 在它外面套了一层 **`WrappedMasterService`**：

- 这一层只负责「RPC 协议适配」：计时、打日志、更新指标计数器，然后把调用**转发**给内部的 `master_service_`。
- 真正的业务逻辑全部在 `MasterService` 里。

coro_rpc 的约定是：注册进 server 的方法必须是某个对象的成员函数。所以 `WrappedMasterService` 就是那个「被注册的对象」。`RegisterRpcService(server, wrapped)` 这一个函数把 `WrappedMasterService` 上的几十个成员函数一次性注册进 server，从此客户端就能用「函数名 + struct_pack 参数」远程调用它们。

#### 4.1.2 核心流程

1. Master 进程启动，构造一个 `coro_rpc::coro_rpc_server`（绑定端口、线程数、超时）。
2. 构造一个 `WrappedMasterService`（内部含一个 `MasterService`）。
3. 调用 `RegisterRpcService(server, wrapped)`，把所有方法注册进去。
4. `server.async_start()` 开始监听。
5. 客户端用 `coro_rpc_client` 调用对应方法名，coro_rpc 自动用 struct_pack 序列化参数/返回值。
6. 服务端的 `WrappedMasterService::方法` 被回调 → 调 `execute_rpc` 计时/计数 → 转发给 `master_service_`。

#### 4.1.3 源码精读

**`WrappedMasterService` 的薄壳结构。** 它私有持有一个 `MasterService`，公开方法只是转发器：

[rpc_service.h:272-274](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/rpc_service.h#L272-L274) —— 私有成员 `master_service_`，真正干活的业务对象。

一个典型的转发方法（`CreateCopyTask`）只做三件事：计时、计数、转发：

[rpc_service.cpp:950-964](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/rpc_service.cpp#L950-L964) —— `execute_rpc` 是个模板，接收：RPC 名、真正执行业务的 lambda、打印请求参数的 lambda、请求计数 lambda、失败计数 lambda。`execute_rpc` 内部就是「计时 → 打请求 → 计数 → 执行 → 失败则失败计数 → 打响应」：

[rpc_helper.h:39-58](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/rpc_helper.h#L39-L58) —— 注意它用 `requires TlExpected<...>` 约束「业务函数必须返回 `tl::expected`」，编译期就保证错误处理风格统一。

**Drain 相关的三个方法没有走 `execute_rpc`，直接转发**（因为它们后续会被独立统计）：

[rpc_service.cpp:1164-1177](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/rpc_service.cpp#L1164-L1177) —— `CreateDrainJob`/`QueryDrainJob`/`CancelDrainJob` 直接委托给 `master_service_`。

**注册阶段。** `RegisterRpcService` 是一长串 `register_handler`：

[rpc_service.cpp:1189-1192](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/rpc_service.cpp#L1189-L1192) —— 注册 `ExistKey` 等方法。每个 `register_handler<&WrappedMasterService::方法>(&wrapped_master_service)` 都把「成员函数指针 + 对象指针」交给 server。本讲关心的几个注册在末尾：

[rpc_service.cpp:1314-1324](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/rpc_service.cpp#L1314-L1324) —— `CreateCopyTask`、`CreateMoveTask`、`QueryTask`、`FetchTasks`、`MarkTaskToComplete` 全部注册。这就是「Copy/Move 异步任务」对外的 RPC 入口。

**Server 构造与启动（Master 进程入口）。**

[master.cpp:1116-1120](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master.cpp#L1116-L1120) —— 构造 `coro_rpc_server`，传入线程数、端口、地址、连接超时、TCP_NODELAY。接着第 1121-1124 行根据环境变量 `MC_RPC_PROTOCOL=rdma` 选择是否初始化 IB verbs（即 RPC 也能走 RDMA）。

[master.cpp:1125-1140](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master.cpp#L1125-L1140) —— 构造 `WrappedMasterService`，再调用 `RegisterRpcService(server, *wrapped_master_service)` 完成注册。之后才 `async_start`。

> 关于序列化的关键点：coro_rpc 用 **struct_pack** 自动序列化 `WrappedMasterService` 各方法的参数与返回值（例如 `FetchTasks` 返回的 `std::vector<TaskAssignment>`）。能被 struct_pack 处理的前提是结构体有 `YLT_REFL` 反射，见 [rpc_types.h:242-243](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/rpc_types.h#L242-L243)（`TaskAssignment` 的反射）。

#### 4.1.4 代码实践

**实践目标：用 glog VERBOSE 日志观察一次 RPC 的完整计时链路。**

1. 操作步骤：
   - 阅读 [rpc_helper.h:39-58](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/rpc_helper.h#L39-L58)，理解 `execute_rpc` 里 `ScopedVLogTimer`、`inc_req_metric`、`inc_fail_metric` 三者的顺序。
   - 启动 Master 时设置 `GLOG_v=1`（打开 VLOG(1)）。例如：
     ```bash
     GLOG_v=1 ./mooncake_master --rpc_port=50001
     ```
   - 用任意 Client 触发一次 `CreateCopyTask`（可在测试或 Python 客户端里调用）。
2. 需要观察的现象：日志里出现一行 `CreateCopyTask` 的请求日志（带 `key=..., tenant_id=..., targets_size=...`）和一行响应日志（带耗时）。指标 `create_copy_task_requests` 计数 +1。
3. 预期结果：成功时无 `inc_create_copy_task_failures`；失败（如 key 不存在）时失败计数 +1，且 `master_service_` 返回的 `ErrorCode` 会被原样带回客户端。
4. 待本地验证：具体命令行参数以仓库 `README`/`docs` 的部署说明为准；若无法实际运行，至少对照源码确认日志与指标的触发点。

#### 4.1.5 小练习与答案

**Q1：为什么 `WrappedMasterService::CreateDrainJob` 没有用 `execute_rpc`？**
参考答案：`CreateDrainJob`/`QueryDrainJob`/`CancelDrainJob` 直接转发给 `master_service_`（见 [rpc_service.cpp:1164-1167](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/rpc_service.cpp#L1164-L1167)），没有像 Copy/Move 那样附带请求/失败指标。这说明 Drain 类操作目前没有单独的 RPC 级指标埋点——如果要监控 DrainJob 的创建失败率，需要在这里补 `execute_rpc`。

**Q2：coro_rpc 是怎么知道一个 `std::vector<TaskAssignment>` 该如何序列化的？**
参考答案：靠 `YLT_REFL(TaskAssignment, id, type, payload, ...)` 提供的编译期字段反射（[rpc_types.h:242-243](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/rpc_types.h#L242-L243)）。struct_pack 据此为每个字段生成序列化/反序列化代码，`std::vector` 是 struct_pack 内置支持的容器。

---

### 4.2 TaskManager：Master 侧的任务队列与生命周期

#### 4.2.1 概念说明

Copy/Move 是「异步任务」：Master 创建任务时不立刻搬运数据，而是把任务记下来，等 owning client（数据所在段的 client）来拉取执行。承载这套「记账」的就是 `ClientTaskManager`。

它内部维护四样东西：
- **`all_tasks_`**：所有任务的总账本，`task_id -> Task`。
- **`pending_tasks_`**：每个 client 的「待派发队列」（FIFO），`client_id -> queue<task_id>`。
- **`processing_tasks_`**：每个 client 的「执行中集合」，`client_id -> set<task_id>`。
- **`finished_task_history_`**：完成顺序的双端队列，用来对老任务做 LRU 清理。

任务本身的状态机很简单：

```
PENDING ──(被 pop)──> PROCESSING ──(回报成功)──> SUCCESS
                              └──(回报失败/超时)──> FAILED
```

`SUCCESS` 和 `FAILED` 都是「终态」，由 `is_finished_status()` 判定。

#### 4.2.2 核心流程

1. **提交（submit_task）**：生成 UUID → 校验 pending 上限 → 写入 `all_tasks_` + 该 client 的 pending 队列，状态 `PENDING`。
2. **弹出（pop_tasks）**：Client 调 `FetchTasks` → 从该 client 的 pending 队列弹出至多 `batch_size` 个、且不超过 `max_total_processing_tasks_` → 状态置 `PROCESSING`，移入 processing 集合，返回给 Client。
3. **完成（complete_task）**：Client 回报 → 校验调用方是否是该 task 的 owner、状态是否合法 → 置终态，从 processing 集合移除，加入 finished 历史。
4. **清理**：
   - `prune_finished_tasks`：finished 历史超过上限时，丢弃最老的任务记录。
   - `prune_expired_tasks`：pending 超时（默认 300s）或 processing 超时（默认 300s）的任务，自动判 `FAILED`。

并发安全：所有读写都通过 RAII 访问器 `ScopedTaskReadAccess`（共享锁）/ `ScopedTaskWriteAccess`（独占锁）进行，访问器析构即解锁。

#### 4.2.3 源码精读

**任务状态机与 `Task` 结构。**

[task_manager.h:36-45](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/task_manager.h#L36-L45) —— `TaskStatus` 四态 + `is_finished_status`（只有 SUCCESS/FAILED 算终态）。

[task_manager.h:68-92](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/task_manager.h#L68-L92) —— `Task` 结构。关键字段：`payload`（一段 JSON 字符串，承载具体 copy/move 参数）、`assigned_client`（谁该来拉取执行）、`max_retry_attempts`。`mark_processing()` / `mark_complete()` 是状态流转的两个动作，都会刷新 `last_updated_at`（超时判定依赖它）。

**负载（payload）的类型化封装。** 任务分两类，负载结构不同，但都被序列化成 JSON 字符串塞进 `Task::payload`：

[task_manager.h:94-108](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/task_manager.h#L94-L108) —— `ReplicaCopyPayload`（一对多：source → targets）与 `ReplicaMovePayload`（一对一：source → target）。

[task_manager.h:110-130](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/task_manager.h#L110-L130) —— `TaskPayloadTraits` 把 `TaskType` 映射到负载类型；`serialize_payload` 用 `struct_json::to_json` 生成 payload 字符串。这里体现了「struct_json 只负责 payload 这一个字段，而整个 TaskAssignment 走线用的是 struct_pack」。

**提交任务。**

[task_manager.cpp:29-56](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/task_manager.cpp#L29-L56) —— 先查 `max_total_pending_tasks_` 上限（防止单点堆积），再生成去重 UUID，最后同时写 `all_tasks_` 和 `pending_tasks_[client_id]`。

**弹出任务（最关键的一段）。**

[task_manager.cpp:58-106](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/task_manager.cpp#L58-L106) —— 注意三个细节：
- 弹出数量受 `batch_size` 和全局 `max_total_processing_tasks_` 双重限制（第 70-74 行）。
- 弹出即 `mark_processing()`，从 pending 计数减一、processing 计数加一（第 91-100 行）。
- 这是一个「写」操作（修改了任务状态），所以 `FetchTasks` 用的是 `get_write_access()`。

**完成任务。**

[task_manager.cpp:108-152](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/task_manager.cpp#L108-L152) —— 三道校验：完成状态必须是终态、任务必须存在、**回报者必须是 `assigned_client`**（第 126-130 行，防止别的 client 乱报别人的任务）。幂等：已经是终态再回报返回 `OK`（第 132-136 行）。

**超时清理（防卡死）。**

[task_manager.cpp:163-253](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/task_manager.cpp#L163-L253) —— pending 用 `created_at` 判超时、processing 用 `last_updated_at` 判超时；超时即 `mark_complete(FAILED, "... timeout")`。默认两个超时都是 300s（见 [types.h:122-130](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/types.h#L122-L130)）。

**配置项。** 任务管理器的容量/超时参数集中在 `TaskManagerConfig`：

[master_config.h:864-872](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_config.h#L864-L872) —— `max_total_finished_tasks`、`max_total_pending_tasks`、`max_total_processing_tasks`、两个 timeout、`max_retry_attempts`。

#### 4.2.4 代码实践

**实践目标：读懂任务三态流转，并验证「owner 校验」。**

1. 操作步骤：
   - 对照 [task_manager.cpp:58-106](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/task_manager.cpp#L58-L106) 与 [task_manager.cpp:108-152](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/task_manager.cpp#L108-L152)，画出一次 `submit → pop → complete` 时，`all_tasks_` / `pending_tasks_` / `processing_tasks_` / `finished_task_history_` 这四个容器里 task_id 的进出情况。
   - 在 `complete_task` 的第 126 行（`task.assigned_client != client_id` 分支）处加一行 `LOG(INFO)`，打印被拒绝的 `client_id` 与 `assigned_client`。
2. 需要观察的现象：正常流程下，回报者 == owner；若人为用错误 client_id 调用 `MarkTaskToComplete`，会命中该分支并返回 `ErrorCode::ILLEGAL_CLIENT`。
3. 预期结果：任务不会因「错误客户端回报」而误判完成。
4. 注意：此为源码阅读型实践，不要提交对源码的修改（讲义要求不改源码；上述加日志仅为本地学习目的，验证后请还原）。

#### 4.2.5 小练习与答案

**Q1：`FetchTasks` 调用的是 `get_write_access()`，为什么不是读访问？**
参考答案：因为 `pop_tasks` 会修改任务状态（`PENDING→PROCESSING`）、增减 pending/processing 计数、改写 `Task` 对象。这些都属于「写」，必须持独占锁（见 [task_manager.cpp:58-106](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/task_manager.cpp#L58-L106)）。

**Q2：一个任务被 pop 后、Client 还没回报就崩溃了，会发生什么？**
参考答案：任务停留在 `PROCESSING`。当 `now - last_updated_at > processing_task_timeout_sec_`（默认 300s）时，`prune_expired_tasks` 会把它判成 `FAILED` 并移出 processing 集合（[task_manager.cpp:238-246](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/task_manager.cpp#L238-L246)）。这就是「processing 超时」兜底。

**Q3：为什么任务表还要做序列化（`TaskManagerSerializer`）？**
参考答案：用于 HA/快照。Master 重启或主备切换时，需要把任务表持久化/恢复，避免在途任务丢失。[task_manager.cpp:291-343](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/task_manager.cpp#L291-L343) 用 msgpack 打包再 zstd 压缩。

---

### 4.3 Copy/Move 任务：下发与执行

#### 4.3.1 概念说明

现在把 RPC 层和任务管理器串起来。一次「副本复制」的完整链路是：

1. **创建**：外部调用 `CreateCopyTask(key, tenant, targets)`。Master 校验对象存在、目标段已挂载且可分配，**随机挑一个源副本段**，确定 owning client，然后用 `submit_task_typed<REPLICA_COPY>` 把任务塞进该 client 的 pending 队列。
2. **派发**：owning client 的轮询线程每秒 `FetchTasks`，拉到任务。
3. **执行**：client 用线程池跑 `ExecuteTask`，反序列化 payload，调用本地的 `Copy(...)`/`Move(...)`，真正用 `TransferSubmitter`（`transfer_task.h`）搬数据。
4. **回报**：成功则 `MarkTaskToComplete(SUCCESS)`；某些错误（如空间不足 `NO_AVAILABLE_HANDLE`）在 client 侧就地重试（最多 `max_retry_attempts` 次），其它错误直接回报 `FAILED`。

Move 与 Copy 几乎一样，区别仅在于负载是一对一（`source→target`）且 Move 完成后源副本会被移除（语义上是「搬」而不是「复制」）。

#### 4.3.2 核心流程

```
          ┌────────── Master ──────────┐                      ┌──── Client(owning) ────┐
外部请求 │ CreateCopyTask              │                      │ TaskPollThread (1s)    │
  ──────►│  校验 + 选源段 + 选 owner    │                      │   FetchTasks(batch)    │
        │  submit_task_typed<COPY>     │◄───── pop_tasks ──────┤   ExecuteTask          │
        │   (pending 队列)             │                      │   struct_json 解 payload│
        │                              │                      │   Copy()/Move() 搬数据  │
        │                              │◄── MarkTaskToComplete ┤   (成功: SUCCESS 回报)  │
        │   complete_task              │                      │   (NO_AVAILABLE_HANDLE │
        └──────────────────────────────┘                      │    就地重试)            │
                                                                └────────────────────────┘
```

#### 4.3.3 源码精读

**创建 Copy 任务。**

[master_service.cpp:6730-6786](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L6730-L6786) —— 关键步骤：
- 第 6745-6758 行：校验每个 target 段「已挂载且可分配」。
- 第 6768-6770 行：在源副本段里**随机**挑一个（负载均衡，避免总打同一个源）。
- 第 6772-6779 行：查出该源段的 owning client。
- 第 6780-6785 行：`submit_task_typed<TaskType::REPLICA_COPY>`，把负载交给任务管理器。

**创建 Move 任务。** 与 Copy 对称，区别是负载为单一 `target`，且必须显式指定 `source`，并校验 source 确实是当前某个副本段：

[master_service.cpp:6788-6843](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L6788-L6843) —— 注意第 6820-6825 行校验 `source` 必须在对象现有副本段集合里。

**FetchTasks（Master 侧）。** 就是把 pop 出来的 `Task` 转成跨进程的 `TaskAssignment`：

[master_service.cpp:6856-6866](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L6856-L6866) —— `TaskAssignment` 由 `Task` 构造（见 [rpc_types.h:224-243](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/rpc_types.h#L224-L243)），只携带 client 执行所需的最小信息（id/type/payload/重试上限）。

**MarkTaskToComplete（Master 侧）。**

[master_service.cpp:6868-6880](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L6868-L6880) —— 直接委托 `complete_task`。

**Client 侧：轮询 + 派发。**

[client_service.cpp:3500-3531](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/client_service.cpp#L3500-L3531) —— `PollAndDispatchTasks` 每轮 `FetchTasks(kTaskBatchSize)`，`kTaskBatchSize=16`（[client_service.h:905-906](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/client_service.h#L905-L906)），轮询间隔 1000ms。拉到后逐个 `SubmitTask`，丢进 `task_thread_pool_`（4 线程）异步执行。

**Client 侧：执行 + 回报 + 就地重试。** 这是整条链路的执行核心：

[client_service.cpp:3549-3654](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/client_service.cpp#L3549-L3654) —— 几个要点：
- 第 3555-3577 行：按 `type` 用 `struct_json::from_json` 把 `assignment.payload` 反序列化成 `ReplicaCopyPayload`/`ReplicaMovePayload`，再调 `Copy(...)`/`Move(...)`。
- 第 3594-3605 行：成功 → `MarkTaskToComplete(SUCCESS)`。
- 第 3607-3632 行：**只有 `NO_AVAILABLE_HANDLE`（目标段没空间）才就地重试**，退避 `50*(retry+1)` ms，上限 `assignment.max_retry_attempts`（默认 10）。
- 第 3633-3651 行：其它错误（`OBJECT_NOT_FOUND`、`REPLICA_NOT_FOUND` 等）直接 `MarkTaskToComplete(FAILED)`——因为这些重试也没用。

> 客户端真正搬数据时，`Copy()`/`Move()` 内部会用到 [transfer_task.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/transfer_task.h) 里的 `TransferSubmitter`，由它根据源/目标位置选择 `LOCAL_MEMCPY` / `TRANSFER_ENGINE`(RDMA/TCP) / `FILE_READ` 等策略。本讲不深入数据搬运细节（那是 u5 系列的内容），只需知道「执行 Copy/Move 最终落到 TransferSubmitter」。

#### 4.3.4 代码实践

**实践目标：跟踪一条 Copy 任务从创建到回报的全链路。**

1. 操作步骤：
   - 在 Master 端依次定位：[master_service.cpp:6780-6785](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L6780-L6785)（提交）→ [task_manager.cpp:37-53](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/task_manager.cpp#L37-L53)（落 pending）。
   - 在 Client 端依次定位：[client_service.cpp:3502-3510](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/client_service.cpp#L3502-L3510)（拉取）→ [client_service.cpp:3555-3565](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/client_service.cpp#L3555-L3565)（执行）→ [client_service.cpp:3594-3605](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/client_service.cpp#L3594-L3605)（回报）。
   - 用一张表记录每个环节操作的是哪个容器 / 调用了哪个 RPC。
2. 需要观察的现象：一次成功的 Copy，在 Master 日志里依次出现 `CreateCopyTask`、`FetchTasks`、`MarkTaskToComplete`；任务状态 `PENDING→PROCESSING→SUCCESS`。
3. 预期结果：任务最终进入 `finished_task_history_`，`succeeded` 类指标 +1。
4. 待本地验证：若没有多机环境，可阅读 `mooncake-store/tests/master_service_test.cpp` 中涉及 Copy/Move 的断言来理解预期行为（搜索 `CreateCopyTask`/`CreateMoveTask`）。

#### 4.3.5 小练习与答案

**Q1：`CreateCopyTask` 为什么要在源副本段里随机挑一个，而不是固定第一个？**
参考答案：做源端负载均衡。对象可能有多个副本段，固定一个会让它成为热点；随机挑选把读压力分散开（见 [master_service.cpp:6768-6770](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L6768-L6770)）。

**Q2：Client 端的「就地重试」和 Master 端的「processing 超时」会不会冲突？**
参考答案：会协作但层级不同。Client 就地重试只针对 `NO_AVAILABLE_HANDLE`，最多 `max_retry_attempts`（默认 10）次、每次退避几十到几百 ms，远小于 Master 的 processing 超时 300s。所以正常情况下 Client 重试会在 Master 超时之前完成；若 Client 进程挂了不再回报，才由 Master 的 300s 超时兜底判 FAILED。

**Q3：`Task::payload` 为什么用 JSON 字符串而不是直接 struct_pack 整个负载结构？**
参考答案：为了让 payload 可被日志/调试直接读懂，并且让 `TaskAssignment` 这一层保持「与具体负载类型解耦」——Client 端拿到后自己根据 `type` 选择 `ReplicaCopyPayload`/`ReplicaMovePayload` 反序列化（见 [client_service.cpp:3555-3577](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/client_service.cpp#L3555-L3577)）。代价是 payload 这一段不走二进制，略大略慢，但任务量级下可接受。

---

### 4.4 DrainJob：批量迁移作业的生命周期

#### 4.4.1 概念说明

「Drain（抽干）」是运维场景：要把某个 segment 上的数据全部迁走，以便安全下线/重挂该段。一个段上可能有成千上万个对象，逐个手动迁不现实。于是有了 **DrainJob**：

- 它把「抽干一个或多个 segment」建模成一个**作业（Job）**。
- 作业内部把每个待迁对象切片成一个个 **Move 任务**（复用 4.3 的机制），并限制并发度 `max_concurrency`（默认 4）。
- Master 用一个专门的 `JobDispatchThreadFunc` 后台线程，每 500ms 巡检一次所有作业，做「回收完成的任务 → 判是否整体完成 → 没完成就再切片补任务」。
- 作业有自己的状态机（`JobStatus`），与单个 `Task` 的状态机是**两套独立但联动**的状态。

DrainJob 还自带一套**失败/重试统计**：每个「迁移单元（unit）」最多重试 `kMaxDrainUnitRetries=3` 次，超过则记为「终态失败」，避免坏数据卡死整个作业。

#### 4.4.2 核心流程

**两个状态机：**

任务级（每个 Move task）：`PENDING → PROCESSING → SUCCESS/FAILED`（见 4.2）。

作业级（`JobStatus`，[rpc_types.h:129-136](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/rpc_types.h#L129-L136)）：

```
CREATED ──(首次 Schedule)──> PLANNING ──(切片完成/有任务在跑)──> RUNNING
                                                                  │
                                  ┌───────────────────────────────┤
                                  ▼                               ▼
                            SUCCEEDED                       FAILED
                  (所有源段已无残留对象)            (残留对象全为终态失败 unit)
                                  │                               │
                                  └────── CANCELED ◄──────────────┘
                                  (CancelDrainJob: 仅在无活跃任务时允许)
```

**段级（`SegmentStatus`，伴随 Job 流转）：** `OK ──(CreateDrainJob)──> DRAINING ──(该段对象清空)──> DRAINED`；若取消或终态失败，段状态会被还原回 `OK`（见 [master_service.cpp:7035-7044](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L7035-L7044) 与 [7307-7319](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L7307-L7319)）。

**每轮巡检（`ProcessDrainJobs`）对一个非终态 Job 做三件事：**

1. `RefreshDrainJobTasks`：扫描该 Job 的活跃任务，把已完成的回收掉，统计成功/失败/迁移字节数，累计重试次数。
2. `MaybeCompleteDrainJob`：如果没有任何活跃任务，判断能否收尾（成功 / 终态失败 / 还要继续）。
3. `ScheduleDrainJobTasks`：若没收尾，就再扫描元数据，挑出新的待迁对象，补发 Move 任务直到填满 `max_concurrency` 个槽位。

**三级失败/重试体系（务必区分）：**

| 层级 | 触发者 | 阈值 | 作用对象 |
| --- | --- | --- | --- |
| Client 就地重试 | Client `ExecuteTask` | `max_retry_attempts`=10 | 单个 task（仅 `NO_AVAILABLE_HANDLE`） |
| Master 任务超时 | `prune_expired_tasks` | 300s | 单个 task（判 FAILED） |
| DrainJob 单元重试 | `Schedule/Refresh` | `kMaxDrainUnitRetries`=3 | 一个 (tenant,key,source) unit |

#### 4.4.3 源码精读

**DrainJob 数据结构。**

[master_service.h:1863-1881](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_service.h#L1863-L1881) —— 关键字段：
- `active_tasks`：当前派出去还没收回来的 Move 任务（`task_id -> ActiveDrainTask`）。
- `completed_unit_keys` / `terminal_failed_unit_keys`：已完成 / 终态失败的 unit（去重用）。
- `retry_counts`：每个 unit 已重试次数。
- 统计：`succeeded_units` / `failed_units` / `blocked_units` / `migrated_bytes`。

[master_service.h:1853-1861](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_service.h#L1853-L1861) —— `ActiveDrainTask` 记录一个在途 Move 任务的 (tenant, key, source, target, bytes, unit_key)。

[master_service.h:1883](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_service.h#L1883) —— `kMaxDrainUnitRetries = 3`。

**创建作业。**

[master_service.cpp:6930-6970](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L6930-L6970) —— 先 `ValidateDrainRequestLocked`（段存在、状态 OK、target 可分配），再把待 drain 段状态置 `DRAINING`（第 6942-6953 行，失败会回滚已改的段状态），最后构造 `DrainJob`（状态 `CREATED`）放进 `drain_jobs_`。**注意：创建时不立即切片**，真正的切片发生在后台线程。

**请求结构。**

[rpc_types.h:165-170](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/rpc_types.h#L165-L170) —— `CreateDrainJobRequest`：`segments`（要抽干的段）、`target_segments`（可选，指定迁往何处；为空则自动按利用率挑）、`max_concurrency`（默认 4）。

**后台巡检线程。**

[master_service.cpp:7355-7361](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L7355-L7361) —— `JobDispatchThreadFunc` 循环 `ProcessDrainJobs()` + sleep 500ms。线程在 MasterService 启动时拉起：

[master_service.cpp:328-331](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L328-L331) —— 启动 `job_dispatch_thread_`。

**巡检主体。**

[master_service.cpp:7327-7353](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L7327-L7353) —— 先快照所有 Job（持 `job_mutex_`），再逐个加 Job 自己的锁处理；终态 Job 直接跳过；非终态的依次 `Refresh → MaybeComplete → Schedule`。

**回收已完成任务（Refresh）。**

[master_service.cpp:7098-7133](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L7098-L7133) —— 遍历 `active_tasks`，查任务表：
- 任务已不在表里 → 视为失败，unit 进终态失败集合（第 7105-7109 行）。
- 还在跑 → 跳过。
- 终态 → 成功则 `succeeded_units++`、累加 `migrated_bytes`、unit 进 `completed_unit_keys`；失败则 `failed_units++`、累加 `retry_counts`，达到 3 次进终态失败集合（第 7116-7127 行）。
- 最后把已完成的从 `active_tasks` 移除（第 7130-7132 行）。

**切片补任务（Schedule）。**

[master_service.cpp:7135-7247](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L7135-L7247) —— 这是 DrainJob 的「大脑」：
- 第 7140-7145 行：活跃任务数已达 `max_concurrency`，直接置 `RUNNING` 返回（限流）。
- 第 7156-7212 行：扫描全量元数据（所有 shard、所有 tenant/key），对每个落在待 drain 段上的对象生成一个 `DrainPlan`。**跳过**已完成、活跃中、终态失败的 unit；**阻塞**硬钉住/租约未过期/副本未完成/有在途复制任务的对象（计入 `blocked_units`）；其余用 `SelectDrainTargetForKey` 挑一个利用率最低的目标段。最多填满剩余槽位 `slots`。
- 第 7216-7242 行：对每个 plan 调 `CreateMoveTask`。成功则记入 `active_tasks`；某些错误（`NO_AVAILABLE_HANDLE` 等）算「阻塞」；其它算失败并累计重试，达 3 次进终态失败。

**收尾判定（MaybeComplete）。**

[master_service.cpp:7249-7325](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L7249-L7325) —— 只有 `active_tasks` 为空才判定（第 7250-7252 行）。然后再次扫描元数据，看哪些段上还有残留对象：
- 全部段都无残留 → `SUCCEEDED`，并把已清空的段置 `DRAINED`（第 7278-7294 行）。
- 残留对象的 unit **全部**是终态失败 → `FAILED`，段状态还原 `OK`（第 7296-7324 行）。
- 否则返回 `false`（继续下一轮）。

**查询与取消。**

[master_service.cpp:6972-7005](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L6972-L7005) —— `QueryDrainJob` 返回 `QueryJobResponse`（含 succeeded/failed/blocked/active/migrated_bytes 等统计）。

[master_service.cpp:7007-7046](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L7007-7046) —— `CancelDrainJob` 只在「无活跃任务」时允许，否则返回 `UNAVAILABLE_IN_CURRENT_STATUS`（避免在有在途搬运时强行取消）。

#### 4.4.4 代码实践（本讲核心实践任务）

**实践目标：画出 DrainJob 从创建、切片下发、客户端执行到 MaybeComplete 收尾的状态流转图。**

1. 操作步骤：
   - 阅读以下入口，理清调用关系：
     - 创建：[master_service.h:682-683](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_service.h#L682-L683) 声明 → [master_service.cpp:6930-6970](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L6930-L6970) 实现。
     - 切片下发：[master_service.cpp:7216-7242](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L7216-L7242)（Schedule 内调 `CreateMoveTask`）。
     - 客户端执行：复用 4.3，[client_service.cpp:3549-3654](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/client_service.cpp#L3549-L3654)。
     - 收尾：[master_service.cpp:7249-7325](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L7249-L7325)（MaybeComplete）。
   - 用下面给出的「参考答案图」核对自己的理解，并为图上每条边标注对应的源码行号。

2. 参考状态流转图（DrainJob 视角，每个「迁移单元 unit」是一个 (tenant, key, source_segment) 三元组）：

   ```
   CreateDrainJob
        │ 段 OK→DRAINING；Job=CREATED；不切片
        ▼
   ┌───────────── 后台 JobDispatchThread 每 500ms 巡检 ─────────────┐
   │                                                                │
   │  ScheduleDrainJobTasks                                         │
   │   Job: CREATED→PLANNING→RUNNING                                │
   │   扫描元数据 → 每个 unit 生成 DrainPlan → CreateMoveTask        │
   │   ┌─────────────────────── Move Task ───────────────────────┐  │
   │   │ PENDING →(FetchTasks)→ PROCESSING →(client Copy/Move)   │  │
   │   │   成功: SUCCESS   失败: FAILED(或 client 就地重试)        │  │
   │   └─────────────────────────────────────────────────────────┘  │
   │                                                                │
   │  RefreshDrainJobTasks (回收)                                    │
   │   SUCCESS → succeeded_units++, migrated_bytes+=bytes,          │
   │             completed_unit_keys.insert(unit)                   │
   │   FAILED  → failed_units++, retry_counts[unit]++               │
   │             达 kMaxDrainUnitRetries(3) → terminal_failed         │
   │                                                                │
   │  MaybeCompleteDrainJob (仅当 active_tasks 为空)                 │
   │   残留对象=0              → SUCCEEDED, 段→DRAINED              │
   │   残留对象全为 terminal   → FAILED,     段→OK(还原)            │
   │   否则                   → 继续下一轮 (return false)           │
   └────────────────────────────────────────────────────────────────┘
        │ 终态
        ▼
   SUCCEEDED / FAILED / CANCELED(Query/Cancel 只读/受限取消)
   ```

3. 需要观察的现象：一个真实的 DrainJob，其 `QueryJobResponse.status` 会随时间从 `CREATED` 走到 `RUNNING`，`succeeded_units` 逐步增长、`active_units` 在 0~`max_concurrency` 之间波动，最终落到 `SUCCEEDED`（或 `FAILED`）。
4. 预期结果：能用上图向他人解释「为什么 DrainJob 创建后不会立即有任务、为什么并发被限制在 `max_concurrency`、什么条件下才会判 SUCCEEDED」。
5. 待本地验证：图中的迁移字节数、unit 计数等可在 `QueryDrainJob` 返回值里核对（[rpc_types.h:172-188](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/rpc_types.h#L172-L188)）。

#### 4.4.5 小练习与答案

**Q1：DrainJob 创建后，第一批 Move 任务什么时候才被发出去？**
参考答案：不是立即。`CreateDrainJob` 只置 Job=CREATED 并把段置 DRAINING。真正的切片由后台 `JobDispatchThreadFunc` 在下一次巡检（最多 500ms 后）调用 `ScheduleDrainJobTasks` 完成（见 [master_service.cpp:6930-6970](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L6930-L6970) 与 [7355-7361](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L7355-L7361)）。

**Q2：`blocked_units` 与 `failed_units` 有什么区别？**
参考答案：`blocked` 表示对象「暂时不能迁」（硬钉住、租约未过期、副本未完成、有在途复制任务、目标段没空间等），下一轮还可能成功；`failed` 表示迁移确实失败并累计了重试。`blocked` 不消耗 `retry_counts`，`failed` 才会（见 [master_service.cpp:7188-7194](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L7188-L7194) 与 [7229-7241](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L7229-L7241)）。

**Q3：为什么 `CancelDrainJob` 在有活跃任务时拒绝取消？**
参考答案：活跃任务意味着数据正在被搬运，强行取消会留下半迁移状态。代码要求 `active_tasks` 为空才能取消（[master_service.cpp:7022-7027](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L7022-L7027)），保证取消时机是「干净的」。

## 5. 综合实践

**任务：用一张完整的「时序 + 双状态机」图，把本讲三个最小模块（coro_rpc 服务、TaskManager、Copy/Move/Drain 任务）串起来，并标注每一步对应的源码位置。**

要求：

1. 画一条从「外部调用 `CreateDrainJob`」到「`QueryDrainJob` 返回 `SUCCEEDED`」的完整时序，纵轴含四个角色：调用方、Master(`WrappedMasterService`/`MasterService`/`ClientTaskManager`)、JobDispatchThread、Client。
2. 在时序上分别标注：
   - 何时发生 coro_rpc 的 struct_pack 序列化（RPC 边界）。
   - 何时 `Task` 状态从 `PENDING→PROCESSING→SUCCESS`。
   - 何时 `JobStatus` 从 `CREATED→PLANNING→RUNNING→SUCCEEDED`。
   - 何时 `SegmentStatus` 从 `OK→DRAINING→DRAINED`。
3. 用一张表列出三级失败/重试体系（Client 就地重试 / Master 任务超时 / DrainJob unit 重试），并指出各自源码位置。
4. 验收：随机挑图上一个箭头（例如「Refresh 回收一个失败的 Move 任务」），能说出它改了 `DrainJob` 的哪些字段、对应 [master_service.cpp:7120-7127](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L7120-L7127)。

> 提示：4.4.4 已经给出 DrainJob 视角的流转图，本实践要求你把它和「RPC 边界」「Task 状态机」叠加成一张端到端图。画完后，建议对照 `mooncake-store/tests/master_service_test.cpp` 里 Drain 相关用例（搜索 `Drain`）核对你对状态的预期。

## 6. 本讲小结

- `mooncake-store` 用 `ylt/coro_rpc` 暴露 RPC：`WrappedMasterService` 是 `MasterService` 的薄壳，`RegisterRpcService` 一次性注册全部方法；RPC 参数/返回值由 **struct_pack** 自动序列化，请求/响应结构体靠 `YLT_REFL` 提供反射。
- `ClientTaskManager` 是 Master 侧的异步任务账本，用 pending/processing/finished 三组容器 + 一把共享读写锁（RAII 访问器）维护任务 `PENDING→PROCESSING→SUCCESS/FAILED` 生命周期，并有 pending/processing 超时（默认 300s）兜底。
- Copy/Move 任务走「下发-拉取」模型：Master `CreateCopyTask`/`CreateMoveTask` 选源段、定 owner、入队；Client 每秒 `FetchTasks` 拉取，线程池 `ExecuteTask` 反序列化 payload 后用 `TransferSubmitter` 搬数据，再 `MarkTaskToComplete` 回报；空间不足时 Client 就地重试。
- `Task.payload` 故意用 struct_json 的 JSON 字符串，便于日志可读并与具体负载类型解耦；而整个 `TaskAssignment` 走线仍用 struct_pack。
- DrainJob 是「抽干 segment」的批量作业：后台线程每 500ms 巡检，做 `Refresh（回收）→ MaybeComplete（收尾判定）→ Schedule（补切片）`，并发受 `max_concurrency` 限制，每个 unit 最多重试 3 次。
- 三级失败/重试体系各司其职：Client 就地重试（NO_AVAILABLE_HANDLE，10 次）、Master 任务超时（300s 判 FAILED）、DrainJob unit 重试（3 次进终态失败）。

## 7. 下一步学习建议

- **深入数据搬运**：本讲的 `Copy()`/`Move()` 最终落到 [transfer_task.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/transfer_task.h) 的 `TransferSubmitter`，建议接着读 `transfer_task.cpp` 的 `selectStrategy`，理解 `LOCAL_MEMCPY`/`TRANSFER_ENGINE`/`FILE_READ` 的选择逻辑（这正是 u5 系列讲过的传输引擎在 store 侧的应用）。
- **HA 与任务持久化**：`TaskManagerSerializer`（[task_manager.cpp:291-433](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/task_manager.cpp#L291-L433)）负责把任务表做 msgpack+zstd 快照，可结合 HA 主备切换（`mooncake-store/include/ha`）学习在途任务如何不丢。
- **段状态机**：DrainJob 牵动了 `SegmentStatus`（OK/DRAINING/DRAINED/UNMOUNTING），可继续读 `segment_manager` 相关代码，理解段挂载、优雅卸载与 drain 的关系。
- **测试驱动理解**：阅读 `mooncake-store/tests/master_service_test.cpp` 与 `transfer_task_test.cpp` 中 Copy/Move/Drain 的断言，用测试用例反向验证本讲的状态流转图。

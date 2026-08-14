# 数据传输与通知：TransferSync/Async 与 Notify

## 1. 本讲目标

学完本讲，你应该能够：

- 掌握 `TransferSync` 与 `TransferAsync` / `GetTransferStatus` 两组传输接口的正确用法与适用场景。
- 理解一次调用中批量 `TransferOpDesc` 的组织方式，以及「一次接口调用 = 一批数据块搬运」的设计。
- 弄清 `TransferReq` 句柄的完整生命周期：由 `TransferAsync` 产生、在引擎内登记、被首次查询到终态后即失效。
- 掌握 `SendNotify` / `GetNotifies` 通知机制：它走的是控制面 socket，只能由 client 发给 server，且是「取走即清空」的队列语义。

本讲是单元二（HIXL Engine 公开 API 精读）的最后一讲，承接 u2-l4 的建链结果，把 HIXL 的「数据面主链路」走完。

## 2. 前置知识

阅读本讲前，请确认理解以下概念（均在前面讲义中建立）：

- **单边传输**：只需一端（client）发起调用，另一端（server）无需参与即可直接读写其注册内存（u1-l1、u1-l3）。
- **TransferOp 与 TransferOpDesc**：`READ`/`WRITE` 是按发起方视角定义的方向；`TransferOpDesc` 是 `local_addr + remote_addr + len` 三元组，支持放进 `vector` 批量下发（u2-l2）。
- **注册内存与建链**：传输前两端必须完成 `RegisterMem`，发起方必须已 `Connect`（或开启 `auto_connect` 选项）（u2-l3、u2-l4）。
- **TransferStatus**：`WAITING / COMPLETED / TIMEOUT / FAILED` 四态，其中 `TIMEOUT` 在当前版本暂不支持，实际状态迁移主要发生在其余三态之间（u2-l2）。
- **控制面与数据面分离**：HIXL 在建链阶段用一条 TCP socket（`ctrl_socket_`）交换 endpoint 信息，数据传输走 HCCS/RDMA 数据面（u1-l3、u2-l4）。本讲会发现：Notify 通知复用了这条控制面 socket。
- **错误码判断**：`Status` 是 `uint32_t`，唯一正确判断方式是 `== hixl::SUCCESS`（u2-l2）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `include/hixl/hixl.h` | 公开类 `hixl::Hixl`，传输与通知接口的声明及参数约束（本讲关注 L124-L170） |
| `include/hixl/hixl_types.h` | `TransferStatus`、`TransferArgs`、`GetTransferStatusArgs`、`TransferResult`、`NotifyDesc` 等数据结构 |
| `src/hixl/engine/hixl_impl.cc` | Pimpl 实现层：门卫检查、`CheckTransferOpDescs` 参数校验、向引擎转发 |
| `src/hixl/engine/hixl_engine.cc` | 引擎层：auto_connect 自动建链/断链、profiling 埋点、请求台账登记与查询编排 |
| `src/hixl/engine/hixl_client.cc` | 链路层：`HixlClient` 持有 `req_map_` 请求表，把请求下发到 client handler；Notify 的 socket 发送与应答接收 |
| `src/hixl/engine/client_manager.cc` | `ClientManager` 维护 `req → client` 全局索引与按提交顺序排列的请求列表 |
| `src/hixl/engine/hixl_server.cc` | server 侧 Notify 处理：注册消息处理器、入队、应答 |
| `src/hixl/common/ctrl_msg.h` | 控制面消息常量：Notify 长度上限（1024）与队列上限（4096）、`NotifyMsg` 结构 |
| `examples/cpp/hixl_example_d2rd.cpp` | 单进程双引擎样例，演示批量 `TransferOpDesc` + `TransferSync` |
| `examples/cpp/hixl_example_d2rh.cpp` | 双向异步样例，演示 `TransferAsync` + `GetTransferStatus` 轮询（本讲实践的参考） |

调用链总览（自上而下五层）：

```
Hixl (公开类，日志+参数门卫)
  └─ HixlImpl (Pimpl，impl_ 非空 + 引擎已初始化 + op_descs 校验)
      └─ HixlEngine (auto_connect 建链/断链、profiling、请求台账)
          └─ ClientManager (req→client 索引，按提交顺序的请求列表)
              └─ HixlClient (req_map_ 请求表，下发到 client handler)
                  └─ ClientHandler (direct / UB，具体链路实现，u3-l2 精读)
```

## 4. 核心概念与源码讲解

本讲拆为三个最小模块：**同步传输**、**异步传输与状态查询**、**通知机制**。

### 4.1 模块一：TransferSync 同步传输

#### 4.1.1 概念说明

`TransferSync` 是最简单的传输接口：调用线程阻塞，直到这批 `op_descs` 全部搬运完成（或超时/失败）才返回。它适合「一次搬一批、搬完才继续」的场景，例如推理引擎启动时同步拉取权重。

它的核心价值在于**批量**：一次调用可以携带 N 个 `TransferOpDesc`，引擎将其作为一个整体下发。相比循环调用 N 次单条传输，批量下发摊薄了接口调用与链路下发开销——这正是 KV Cache 分块传输（把连续大缓冲区切成若干 block）的典型用法。

#### 4.1.2 核心流程

```
用户调用 TransferSync(remote_engine, operation, op_descs, timeout)
  1. Hixl 外壳：impl_ 非空？timeout > 0？
  2. HixlImpl：引擎已初始化？CheckTransferOpDescs（地址非空）
  3. HixlEngine：
     a. AutoConnect：未开启 auto_connect 时等价于"查链路"，未建链返回 NOT_CONNECTED；
        开启 auto_connect 时自动建链（失败自动断链回滚）
     b. 打 profiling 埋点（READ/WRITE 分别对应不同事件类型）
     c. 委托 client_ptr->TransferSync(...)
  4. HixlClient：op_descs 非空？已连接？已 finalize？
     加锁后交给 client_handler_->TransferSync 真正下发到数据面
  5. 阻塞等待完成 → 返回 SUCCESS / 错误码
```

注意第 3a 步：`TransferSync` **不负责建链**（除非开启 `auto_connect` 选项）。默认模式下，调用前必须先显式 `Connect`，否则得到 `NOT_CONNECTED`。

#### 4.1.3 源码精读

公开接口声明，批量语义一目了然——`op_descs` 是 vector：

- [include/hixl/hixl.h:L117-L125](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl.h#L117-L125)：`TransferSync` 接口声明，注释明确 `op_descs` 为「批量操作的本地以及远端地址」。

外壳层做超时参数门卫（超时必须大于 0）：

- [src/hixl/engine/hixl_impl.cc:L351-L364](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_impl.cc#L351-L364)：`Hixl::TransferSync` 外壳，先记录日志，再检查 `impl_` 非空与 `timeout_in_millis > 0`（否则 `PARAM_INVALID`），最后转发给 impl。

Pimpl 层做批量描述符的逐条校验：

- [src/hixl/engine/hixl_impl.cc:L24-L32](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_impl.cc#L24-L32)：`CheckTransferOpDescs` 遍历每条 desc，要求 `local_addr` 与 `remote_addr` 都非空，否则返回 `PARAM_INVALID`。这是所有传输接口（同步与异步）共用的第一道参数闸。
- [src/hixl/engine/hixl_impl.cc:L179-L187](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_impl.cc#L179-L187)：`HixlImpl::TransferSync` 依次检查引擎非空、已初始化、descs 合法，然后转发给 `engine_->TransferSync`。

引擎层的 auto_connect 与错误回滚：

- [src/hixl/engine/hixl_engine.cc:L191-L213](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_engine.cc#L191-L213)：`HixlEngine::TransferSync`。先经 `AutoConnect` 拿到 `client_ptr`（未建链且未开 auto_connect 时返回 `NOT_CONNECTED`），再按 READ/WRITE 打 profiling 埋点，然后调用 `client_ptr->TransferSync`；若传输失败且开启了 auto_connect，会调用 `AutoDisconnect` 把刚才自动建立的链路拆掉，不留脏状态。
- [src/hixl/engine/hixl_engine.cc:L381-L411](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_engine.cc#L381-L411)：`AutoConnect` 的实现——`auto_connect_` 关闭时仅查表（`GetClient`），找不到即 `NOT_CONNECTED`；开启时则现场 `GetOrCreateClient` 建链。

链路层最后检查并加锁下发：

- [src/hixl/engine/hixl_client.cc:L170-L183](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_client.cc#L170-L183)：`HixlClient::TransferSync` 检查 `op_descs` 非空、handler 就绪、`is_connected_`、未被 finalize，然后持互斥锁调用 `client_handler_->TransferSync`。注意这里有 per-client 互斥：同一个远端的传输在链路层是串行的。

样例中批量 desc 的组织方式（8MB 缓冲切成 512 个 16KB 块）：

- [examples/cpp/hixl_example_d2rd.cpp:L208-L225](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_d2rd.cpp#L208-L225)：`Transfer()` 函数循环构造 `kXferBlockCount`（512）条 `TransferOpDesc`，每条对应一个 16KB block 的本地偏移与远端偏移，然后**一次** `TransferSync(ctx_b.name, WRITE, descs, kXferTimeout)` 下发全部块。这是「一次调用搬一批」的标准写法。

#### 4.1.4 代码实践

**实践目标**：直观感受批量下发的意义。

**操作步骤**：

1. 打开 [examples/cpp/hixl_example_d2rd.cpp:L208-L225](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_d2rd.cpp#L208-L225)，确认 `descs.reserve(kXferBlockCount)` 与循环填充逻辑。
2. 阅读文件头部常量 [examples/cpp/hixl_example_d2rd.cpp:L21-L23](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_d2rd.cpp#L21-L23)：`kXferBufSize = 8MB`、`kXferBlockSize = 16KB`、`kXferBlockCount = 8MB / 16KB = 512`。
3. 在有昇腾硬件的环境运行（参考 u1-l2/u1-l5 的构建与启动方式）：
   `./hixl_example_d2rd --protocol=roce:device --device=0,2 --version=1`

**需要观察的现象**：一次 `TransferSync` 调用完成 512 个 block 的搬运；日志输出 `[INFO] Transfer completed` 与 `[INFO] Verify success`。

**预期结果**：校验通过，B 端 8MB 设备内存全部变为填充值 0xAA。

**待本地验证**：本实践依赖双 device 互通的昇腾环境；若无硬件，可只完成步骤 1-2 的源码阅读部分。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `timeout_in_millis` 传成 0 或负数，会发生什么？在哪一层被拦截？

答案：返回 `hixl::PARAM_INVALID`。拦截点在 `Hixl` 外壳层：[src/hixl/engine/hixl_impl.cc:L356](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_impl.cc#L356) 的 `HIXL_CHK_BOOL_RET_STATUS(timeout_in_millis > 0, PARAM_INVALID, ...)`，请求根本不会到达引擎层。

**练习 2**：某条 `TransferOpDesc` 的 `remote_addr` 忘记赋值（为 0），会在哪里失败？返回什么错误码？

答案：在 `HixlImpl` 层的 `CheckTransferOpDescs` 失败，返回 `PARAM_INVALID`。见 [src/hixl/engine/hixl_impl.cc:L28-L29](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_impl.cc#L28-L29)——`local_addr` 与 `remote_addr` 任一为空都会被拒绝。注意这只校验「指针非空」；地址是否落在已注册区间，由更底层的 client handler / CS 层在选路时校验（承接 u2-l3）。

**练习 3**：未调用 `Connect` 直接 `TransferSync`，且初始化时没有开启 `OPTION_AUTO_CONNECT`，会返回什么？

答案：返回 `NOT_CONNECTED`。路径是 `HixlEngine::TransferSync` → `AutoConnect`：`auto_connect_` 为 false 时只执行 `client_manager_.GetClient`，查不到该远端的 client 即返回 `NOT_CONNECTED`，见 [src/hixl/engine/hixl_engine.cc:L383-L390](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_engine.cc#L383-L390)。

### 4.2 模块二：TransferAsync 异步传输与 GetTransferStatus 状态查询

#### 4.2.1 概念说明

`TransferSync` 会阻塞调用线程——如果应用想在等待传输完成的同时继续做计算（通信与计算重叠，这正是 PD 分离场景的核心诉求），就需要异步接口。

`TransferAsync` 把「下发」与「完成」解耦：

- 下发后立即返回一个不透明句柄 `TransferReq`（`void*`，只持有不解引用）；
- 用户稍后通过 `GetTransferStatus` 查询该请求的状态，轮询直到 `COMPLETED` 或 `FAILED`。

`TransferArgs` 里的 `user_data` 是给用户挂上下文的钩子：下发时传入任意指针，批量查询时会在 `TransferResult` 中原样带回，省去用户自己维护 `req → 上下文` 的映射。

与同步接口的另两个差异值得注意：

1. `TransferAsync` **没有超时参数**——超时语义交给状态查询与用户自己的轮询策略；
2. 它**支持 auto_connect 隐式建链**（引擎内部用固定 3 秒的 `kAutoConnectTimeout`），与同步版本行为一致。

#### 4.2.2 核心流程

请求的完整生命周期：

```
TransferAsync(engine, op, descs, args, req)
  ├─ 门卫检查（impl_ / 引擎初始化 / descs 非空地址）
  ├─ HixlEngine：AutoConnect（固定 3s 超时）→ client_ptr->TransferAsync
  │    └─ HixlClient：req_map_[req] = {下发时刻, op}   ← 链路级台账
  ├─ HixlEngine：client_manager_.RegisterTransferReq(req, client, user_data)
  │             ← 引擎级台账：req→client 映射 + 按提交顺序的请求列表
  └─ 返回 SUCCESS，用户持有 req

GetTransferStatus(req, status)   ← 单个查询
  ├─ 引擎层：GetClientByReq(req) 查台账
  │    ├─ 查不到 → PARAM_INVALID（请求已完成或不存在），status 置 FAILED
  │    └─ 查到 → client->GetTransferStatus(req, status)
  ├─ status != WAITING（终态）→ EraseTransferReq(req)   ← 一次性语义！
  └─ 查询出错 → 摘除请求 + auto_connect 时自动断链

GetTransferStatus(args, results) ← 批量查询
  ├─ GetOrderedReqs(0)：按提交顺序取出所有在途请求
  ├─ 逐个查询；max_query_count 限制本次最多返回多少条
  ├─ skip_waiting=true 时结果中不包含仍在 WAITING 的请求
  └─ 某远端 client 查询失败 → 该远端断链一次，其后续请求直接报 FAILED
```

最重要的规则：**`TransferReq` 是一次性句柄**。一旦某次查询返回终态（`COMPLETED` 或 `FAILED`），请求即从两级台账（`ClientManager` 与 `HixlClient::req_map_`）中摘除，之后再拿同一个 `req` 查询会得到 `PARAM_INVALID`。这决定了正确的使用模式是「循环轮询直到终态，然后不再碰这个 req」。

#### 4.2.3 源码精读

接口声明与 `user_data` 钩子：

- [include/hixl/hixl.h:L127-L154](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl.h#L127-L154)：`TransferAsync`（出参 `req` 句柄）、单 req 查询与批量查询（`GetTransferStatusArgs` 进、`vector<TransferResult>` 出）三连声明。
- [include/hixl/hixl_types.h:L74-L92](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl_types.h#L74-L92)：`TransferStatus` 四态枚举、`TransferArgs.user_data`、`GetTransferStatusArgs`（`max_query_count` 默认 `UINT32_MAX`、`skip_waiting` 默认 false）、`TransferResult`（带回 `req + user_data + status`）。

引擎层：下发并登记台账：

- [src/hixl/engine/hixl_engine.cc:L215-L237](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_engine.cc#L215-L237)：`HixlEngine::TransferAsync`。注意两点：auto_connect 用的是常量 `kAutoConnectTimeout = 3000`（定义在 [src/hixl/engine/hixl_engine.cc:L27](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_engine.cc#L27)）；下发成功后立刻 `client_manager_.RegisterTransferReq(req, client_ptr, optional_args.user_data)` 把请求登记进全局台账——这就是后续任何查询都能凭 `req` 找到归属 client 的原因。
- [src/hixl/engine/client_manager.cc:L127-L135](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/client_manager.cc#L127-L135)：`RegisterTransferReq` 同时维护两份数据——`req_to_client_` 哈希表（req → 弱引用 client + user_data + 迭代器）与 `ordered_reqs_` 按提交顺序的列表，后者支撑批量查询的有序遍历。

链路层：记录下发时刻供 profiling 用：

- [src/hixl/engine/hixl_client.cc:L185-L197](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_client.cc#L185-L197)：`HixlClient::TransferAsync` 下发后把 `TransferInfo{当前系统周期, operation}` 存入 `req_map_[req]`——查询到 `COMPLETED` 时用这个起始时刻上报「从下发到完成」的耗时。

单个请求查询与一次性语义：

- [src/hixl/engine/hixl_engine.cc:L239-L259](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_engine.cc#L239-L259)：`HixlEngine::GetTransferStatus`（单 req 版本）。`GetClientByReq` 查不到时返回 `PARAM_INVALID`，错误信息写得很直白：「request has been completed or does not exist」；关键一行是 L255-L257：`status != WAITING` 即 `EraseTransferReq(req)`——终态查询后请求立即失效。
- [src/hixl/engine/hixl_client.cc:L199-L233](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_client.cc#L199-L233)：`HixlClient::GetTransferStatus`。`COMPLETED` 时上报带耗时的 profiling 事件并 `RemoveTransferReq`；`FAILED` 时同样摘除（但返回值仍是 `SUCCESS`——失败信息通过出参 `status` 传递，这是容易踩坑的点：**查询接口返回 SUCCESS 不代表传输成功**）。

批量查询的编排：

- [src/hixl/engine/hixl_engine.cc:L261-L307](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_engine.cc#L261-L307)：批量版 `GetTransferStatus`。`GetOrderedReqs(0)` 取全部在途请求按提交序遍历；`max_query_count` 截断本次返回条数；`skip_waiting` 跳过仍在等待的请求；某远端 client 查询失败时该远端只断链一次（`disconnected_engines` 去重），其后同一远端的剩余请求直接以 `FAILED` 结果返回，不再逐个查询。

异步轮询的标准写法（参考样例）：

- [examples/cpp/hixl_example_d2rh.cpp:L278-L287](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_d2rh.cpp#L278-L287)：while 循环中反复调用 `GetTransferStatus(req, st)` 直到两个方向的请求都离开 `WAITING`，并用 `poll_count` 上限防止死循环。这就是官方推荐的轮询骨架。

#### 4.2.4 代码实践

**实践目标**：编写一个批量异步传输练习——一次 `TransferAsync` 下发多个 `TransferOpDesc`，用 `GetTransferStatus` 轮询直到完成，再校验数据。

**操作步骤**（基于 `hixl_example_d2rd.cpp` 修改，该样例已具备双引擎、注册、建链的完整脚手架）：

1. 复制 `examples/cpp/hixl_example_d2rd.cpp` 为 `hixl_example_d2rd_async.cpp`（示例代码，仅作练习用，不要提交到仓库）。
2. 将 `Transfer()` 函数中的同步调用替换为「异步下发 + 轮询」。替换后的核心片段（**示例代码**）：

```cpp
int32_t Transfer(EngineCtx &ctx_a, EngineCtx &ctx_b) {
  std::vector<TransferOpDesc> descs;              // 与原版相同：构造 512 条 desc
  descs.reserve(kXferBlockCount);
  for (size_t i = 0; i < kXferBlockCount; ++i) {
    TransferOpDesc desc{};
    desc.local_addr = reinterpret_cast<uintptr_t>(ctx_a.dev_buf) + i * kXferBlockSize;
    desc.remote_addr = reinterpret_cast<uintptr_t>(ctx_b.dev_buf) + i * kXferBlockSize;
    desc.len = kXferBlockSize;
    descs.push_back(desc);
  }
  TransferArgs args{};                            // user_data 此处留空
  TransferReq req = nullptr;
  auto ret = ctx_a.engine.TransferAsync(ctx_b.name, WRITE, descs, args, req);
  if (ret != SUCCESS) {
    printf("[ERROR] TransferAsync failed, ret=%u\n", ret);
    return -1;
  }
  TransferStatus st = TransferStatus::WAITING;
  int32_t poll_count = 0;
  while (st == TransferStatus::WAITING) {         // 轮询直到终态
    ret = ctx_a.engine.GetTransferStatus(req, st);
    if (ret != SUCCESS) {                         // 请求不存在/查询失败
      printf("[ERROR] GetTransferStatus failed, ret=%u\n", ret);
      return -1;
    }
    if (++poll_count > 1000000) {
      printf("[ERROR] poll timeout\n");
      return -1;
    }
  }
  if (st != TransferStatus::COMPLETED) {
    printf("[ERROR] transfer failed, status=%d\n", static_cast<int32_t>(st));
    return -1;
  }
  printf("[INFO] Async transfer completed after %d polls\n", poll_count);
  return 0;
}
```

3. 在样例的 `CMakeLists.txt` 中仿照 `hixl_example_d2rd` 增加一个目标（或临时替换原目标名），重新执行 `bash build.sh --examples`。
4. 运行：`./hixl_example_d2rd_async --protocol=roce:device --device=0,2 --version=1`。

**需要观察的现象**：

- 轮询次数 `poll_count`：16KB × 512 块的 HCCS 传输通常在若干次轮询内完成；对比把 `kXferBlockSize` 调大/调小后轮询次数与总耗时的变化。
- `Verify` 阶段输出 `[INFO] Verify success`（B 端 8MB 全为 0xAA）。
- 练习加深：故意在轮询结束后**再查一次**同一个 `req`，观察返回值——应得到 `PARAM_INVALID`（请求已被台账摘除），验证「一次性句柄」语义。

**预期结果**：异步版本与原同步版本校验结果一致，均 `Verify success`。

**待本地验证**：以上运行现象依赖真实昇腾双 device 环境；无硬件时完成代码改写与第 3 步的编译验证即可（若交叉编译环境可用），并在笔记中标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `GetTransferStatus` 返回 `SUCCESS` 还不能断定传输成功？

答案：`SUCCESS` 只表示「查询这个动作成功了」。传输结果在出参 `status` 里：`HixlClient::GetTransferStatus` 在请求 `FAILED` 时同样返回 `SUCCESS`，仅把 `status` 置为 `TransferStatus::FAILED`（见 [src/hixl/engine/hixl_client.cc:L227-L230](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_client.cc#L227-L230)）。正确的判断是「接口返回 SUCCESS 且 `status == COMPLETED`」。

**练习 2**：请求达到终态后再查一次同一个 `req` 会怎样？

答案：返回 `PARAM_INVALID`，且出参 `status` 被置为 `FAILED`。因为引擎层在首次查到非 `WAITING` 状态时已调用 `EraseTransferReq` 摘除台账（[src/hixl/engine/hixl_engine.cc:L255-L257](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_engine.cc#L255-L257)），第二次查询时 `GetClientByReq` 返回空指针，走到 L241-L245 的 `PARAM_INVALID` 分支。

**练习 3**：批量查询接口 `GetTransferStatus(args, results)` 中 `skip_waiting` 与 `max_query_count` 分别解决什么问题？

答案：`skip_waiting`（默认 false）为 true 时，仍在 `WAITING` 的请求不出现在结果里——适合「只关心已完结请求」的事件驱动式处理；`max_query_count`（默认 `UINT32_MAX`）限制单次查询最多返回的条数——适合控制每次循环的处理量、避免结果集过大。两者语义见 [include/hixl/hixl_types.h:L81-L85](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl_types.h#L81-L85)，实现见 [src/hixl/engine/hixl_engine.cc:L298-L304](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_engine.cc#L298-L304)。

### 4.3 模块三：SendNotify / GetNotifies 通知机制

#### 4.3.1 概念说明

传输接口搬运的是**数据**；Notify 机制传递的是**信号**。典型场景：client 把 KV Cache 搬到 server 后，还需要告诉 server「这批数据写完了、可以开始算了」——这种轻量的控制消息就走 `SendNotify`。

理解 Notify 的四个关键点：

1. **方向固定**：只能 client → server。`SendNotify` 从 client 发往远端 engine，`GetNotifies` 只收集「当前 Hixl 作为 server 收到」的通知（头文件注释写得很清楚）。
2. **走控制面**：Notify 复用建链时建立的那条 TCP 控制面 socket（`ctrl_socket_`），序列化为 JSON 文本发送，**不占用数据面带宽**。
3. **同步应答**：`SendNotify` 会阻塞等待 server 回 `NotifyAck`（或超时），所以它是同步接口。
4. **队列 + 取走即清空**：server 把通知逐条放进内存队列（上限 4096 条），`GetNotifies` 一次把队列**整体 move 出来并清空**——两次调用之间没有去重，调用方需自己及时消费。

#### 4.3.2 核心流程

```
client 侧：
SendNotify(remote_engine, notify{name, notify_msg}, timeout)
  ├─ Hixl 外壳：name / notify_msg 长度 ≤ 1024，timeout > 0
  ├─ HixlEngine：GetClient(remote_engine)，未建链 → NOT_CONNECTED
  └─ HixlClient::SendNotify：
       序列化为 JSON：{"name": ..., "notify_msg": ...}
       经 ctrl_socket_ 发送 [header | msg_type=kNotify | json body]
       阻塞等待 server 的 kNotifyAck 应答（含 result 字段）

server 侧（初始化时已注册 kNotify 处理器）：
收到 kNotify 消息 → ProcessNotifyMsg：
  ├─ 校验：队列 < 4096 条、name ≤ 1024、msg ≤ 1024
  ├─ 合法则构造 NotifyDesc 追加进 notify_messages_ 队列
  └─ 回发 NotifyAck（result = SUCCESS / RESOURCE_EXHAUSTED / PARAM_INVALID）

用户在 server 进程调用 GetNotifies(notifies)：
  └─ HixlServer：notifies = std::move(notify_messages_)  ← 取走即清空
```

#### 4.3.3 源码精读

接口声明——注意 `GetNotifies` 无参数、无远端概念：

- [include/hixl/hixl.h:L156-L170](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl.h#L156-L170)：`SendNotify`（client 向 server 发送）与 `GetNotifies`（取回**所有**已收到通知并清空）的声明。
- [include/hixl/hixl_types.h:L94-L97](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl_types.h#L94-L97)：`NotifyDesc` 仅含 `name` 与 `notify_msg` 两个 `AscendString` 字段——刻意保持极简，通知不用于携带大数据。

外壳层的长度与超时校验：

- [src/hixl/engine/hixl_impl.cc:L391-L407](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_impl.cc#L391-L407)：`Hixl::SendNotify` 定义 `kMaxNotifyLength = 1024`，分别校验 `name` 与 `notify_msg` 长度不超限、`timeout > 0`，否则 `PARAM_INVALID`。
- [src/hixl/engine/hixl_impl.cc:L409-L415](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_impl.cc#L409-L415)：`Hixl::GetNotifies` 外壳，仅做 impl 非空检查后转发。

引擎层：SendNotify 要求已建链：

- [src/hixl/engine/hixl_engine.cc:L323-L338](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_engine.cc#L323-L338)：`HixlEngine::SendNotify` 直接 `GetClient` 查链路，查不到返回 `NOT_CONNECTED`——与传输接口不同，Notify **没有 auto_connect 隐式建链**，必须先显式建链。
- [src/hixl/engine/hixl_engine.cc:L340-L347](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_engine.cc#L340-L347)：`HixlEngine::GetNotifies` 转发给 `server_.GetNotifies`。

client 侧：JSON 序列化 + socket 发送 + 等待应答：

- [src/hixl/engine/hixl_client.cc:L33-L45](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_client.cc#L33-L45)：`SerializeNotifyMsg` 把通知序列化为 `{"name": ..., "notify_msg": ...}` 的 JSON；`ParseNotifyAckResult` 从应答 JSON 中提取 `result` 字段。
- [src/hixl/engine/hixl_client.cc:L369-L406](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_client.cc#L369-L406)：`HixlClient::SendNotify` 全流程——再次校验长度（与 ctrl_msg.h 的常量对齐）、序列化、经 `ctrl_socket_` 依次发送 header、`CtrlMsgType::kNotify`、JSON body 三段，最后调用 `RecvNotifyAck` 阻塞等待应答。
- [src/hixl/engine/hixl_client.cc:L298-L335](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_client.cc#L298-L335)：`RecvNotifyAck` 校验 magic 与消息类型 `kNotifyAck`，解析出 server 侧处理结果并原样返回。

server 侧：注册处理器、入队、应答：

- [src/hixl/common/ctrl_msg.h:L18-L20](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/ctrl_msg.h#L18-L20)：`kMaxNotifyNameLen = 1024`、`kMaxNotifyMsgLen = 1024`、`kMaxNotifyQueueSize = 4096` 三个控制面常量。
- [src/hixl/engine/hixl_server.cc:L184-L191](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_server.cc#L184-L191)：server 初始化时向 CS 层注册 `kNotify` 消息的处理器，回调进入 `ProcessNotifyMsg`。
- [src/hixl/engine/hixl_server.cc:L193-L239](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_server.cc#L193-L239)：`ProcessNotifyMsg`——解析 JSON 后三级校验（队列满 → `RESOURCE_EXHAUSTED`；name 超长 → `PARAM_INVALID`；msg 超长 → `PARAM_INVALID`），合法则构造 `NotifyDesc` 追加进 `notify_messages_`，最后无论成败都回发 `NotifyAck` 把 result 带回 client。
- [src/hixl/engine/hixl_server.cc:L177-L182](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_server.cc#L177-L182)：`HixlServer::GetNotifies` 持锁执行 `notifies = std::move(notify_messages_)`——move 之后源 vector 变空，「取走即清空」的语义由这一行直接实现。

#### 4.3.4 代码实践

**实践目标**：在 4.2.4 的异步传输练习基础上补全「传输完成 → 通知对端」的完整闭环。

**操作步骤**（继续修改你的练习样例，**示例代码**）：

1. 在 client 侧（ctx_a）轮询到 `COMPLETED` 之后追加：

```cpp
NotifyDesc notify{};
notify.name = "xfer_done";
notify.notify_msg = "512 blocks written";       // 内容任意，长度 ≤ 1024
ret = ctx_a.engine.SendNotify(ctx_b.name, notify, 5000);
if (ret != SUCCESS) {
  printf("[ERROR] SendNotify failed, ret=%u\n", ret);
  return -1;
}
```

2. 在 server 侧（ctx_b，本样例同进程，代表远端角色）调用：

```cpp
std::vector<NotifyDesc> notifies;
ret = ctx_b.engine.GetNotifies(notifies);
if (ret == SUCCESS) {
  for (const auto &n : notifies) {
    printf("[INFO] Got notify: name=%s, msg=%s\n", n.name.GetString(), n.notify_msg.GetString());
  }
}
```

3. 编译后运行（同 4.2.4 第 3-4 步）。

**需要观察的现象**：

- server 侧打印出 `name=xfer_done` 的通知内容；
- 再调用一次 `GetNotifies`：第二次返回的 vector 应为空（取走即清空）；
- 反向实验：把 `notify.name` 填成超过 1024 字节的字符串，观察 `SendNotify` 返回 `PARAM_INVALID`；若先 `Disconnect` 再 `SendNotify`，观察返回 `NOT_CONNECTED`。

**预期结果**：通知在传输完成后被 server 正确取出，两次 `GetNotifies` 的结果验证队列清空语义。

**待本地验证**：依赖昇腾双 device 环境；无硬件时可对照 [src/hixl/engine/hixl_server.cc:L193-L239](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_server.cc#L193-L239) 逐行推演上述三个反向实验各自命中的分支。

#### 4.3.5 小练习与答案

**练习 1**：Notify 走的是数据面（HCCS/RDMA）还是控制面？依据是什么？

答案：控制面。`HixlClient::SendNotify` 全程使用 `ctrl_socket_`（建链阶段建立的 TCP socket）发送，见 [src/hixl/engine/hixl_client.cc:L387-L400](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_client.cc#L387-L400)——发送的是 header + msg_type + JSON 字符串，完全不经过 client handler 的数据通路。

**练习 2**：server 长时间不调用 `GetNotifies` 会发生什么？

答案：通知在 `notify_messages_` 队列中累积，超过 `kMaxNotifyQueueSize = 4096` 条后，server 的 `ProcessNotifyMsg` 对新到通知返回 `RESOURCE_EXHAUSTED` 并通过 NotifyAck 带回，client 侧 `SendNotify` 随即以 `RESOURCE_EXHAUSTED` 失败（见 [src/hixl/engine/hixl_server.cc:L204-L207](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_server.cc#L204-L207)）。该错误属于「资源被占满」类，消费队列后可恢复重试。

**练习 3**：`GetNotifies` 的「取走即清空」语义对调用方意味着什么约束？

答案：每次 `GetNotifies` 返回的通知必须当场处理完，不能指望下次再查——`HixlServer::GetNotifies` 用 `std::move(notify_messages_)` 把队列搬空（[src/hixl/engine/hixl_server.cc:L180](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_server.cc#L180)）。若业务需要保留，调用方应自行把结果存入自己的容器；同时注意该接口不区分通知来自哪个远端 client（`NotifyDesc` 中无来源字段），需要来源信息时要把来源编码进 `name`/`notify_msg` 内容里。

## 5. 综合实践

把本讲三个模块串成一个完整任务：**改写 `hixl_example_d2rd` 为「异步传输 + 完成通知」版本**。

任务要求：

1. **批量异步下发**：保持 512 条 `TransferOpDesc` 的构造不变，把 `TransferSync` 替换为 `TransferAsync`，并在 `TransferArgs.user_data` 中挂一个自定义结构指针（例如携带本次传输的序号与字节数）。
2. **轮询至终态**：用 `GetTransferStatus(req, status)` 轮询直到离开 `WAITING`；注意区分「接口返回值」与「status 出参」两层语义。
3. **user_data 回传验证**：额外调用一次批量查询版本 `GetTransferStatus(GetTransferStatusArgs{}, results)`（在另一次下发后、终态查询前），确认 `TransferResult.user_data` 能把你挂的结构指针原样带回。
4. **完成通知**：传输完成后由 client `SendNotify` 告知 server，server 侧 `GetNotifies` 取出并打印，形成「数据 + 信号」双通道闭环。
5. **数据校验与清理**：沿用样例的 `Verify()` 与 `Finalize()`，保持「断链 → 解注册 → 释放内存 → Finalize」顺序（承接 u2-l3 的顺序合同）。

验收标准：`Verify success`、server 打印出通知内容、笔记中记录轮询次数与总耗时。无硬件环境时，完成代码改写 + 编译，并对照本讲源码链接逐条标注每步对应的引擎内部分支，标注「待本地验证」。

## 6. 本讲小结

- **传输接口分同步与异步两族**：`TransferSync` 阻塞至整批完成；`TransferAsync` 立即返回 `TransferReq` 句柄，配合 `GetTransferStatus` 轮询，支撑通信与计算重叠。
- **批量是第一公民**：一次调用携带整个 `vector<TransferOpDesc>`（样例中 512 个 16KB 块），第一道闸是 `HixlImpl` 层的 `CheckTransferOpDescs`（地址非空，否则 `PARAM_INVALID`）。
- **`TransferReq` 是一次性句柄**：引擎在 `ClientManager`（req→client 索引 + 提交序列表）和 `HixlClient::req_map_` 两级台账登记请求，首次查到终态即摘除，之后再查返回 `PARAM_INVALID`。
- **查询 SUCCESS ≠ 传输成功**：传输失败通过出参 `status`（`FAILED`）传递；批量查询还提供 `skip_waiting` / `max_query_count` 两个流量控制参数，并在某远端故障时自动断链、其后续请求批量报 `FAILED`。
- **auto_connect 下的隐式建链/断链**：传输前自动建链（异步路径固定 3 秒超时），失败时自动回滚断链；但 `SendNotify` 无此待遇，未建链直接 `NOT_CONNECTED`。
- **Notify 是控制面信号通道**：client → server 单向、JSON 文本、同步等待 Ack；server 侧队列上限 4096 条、字段上限 1024 字节，`GetNotifies` 取走即清空。

## 7. 下一步学习建议

单元二到此完结，你已经把 `hixl::Hixl` 全部五组接口（生命周期、内存、链路、传输、通知）的源码链路走完。接下来进入单元三「Engine 内部机制」：

- **u3-l1 Engine 抽象体系与工厂**：本讲多次出现的 `engine_->TransferSync` 背后，`Engine`/`CommEngine` 抽象基类与 `EngineFactory` 如何创建具体引擎。
- **u3-l2 ClientHandler**：本讲最深的下沉点停在 `client_handler_->TransferSync`——direct 与 UB 两类 handler 如何把一批 desc 真正发到 HCCS/RDMA 链路上，是自然的下一站。
- **u3-l4 连接池执行器与线程模型**：若你想进一步理解 auto_connect 心跳探活与请求台账的并发保护，可结合 `ClientManager` 与 `ConnectPoolExecutor` 的实现阅读。

建议阅读顺序：先 u3-l1 补齐抽象层地图，再 u3-l2 沿本讲的 `client_handler_` 调用点继续向下挖。

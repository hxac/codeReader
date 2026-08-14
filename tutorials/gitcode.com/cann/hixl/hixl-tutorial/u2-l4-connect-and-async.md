# 建链与断链：Connect/Disconnect 及异步版本

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 `Connect`/`Disconnect`/`ConnectAsync`/`DisconnectAsync`/`GetAsyncConnectStatus` 五个公开链路接口的语义差异与使用时机。
2. 跟踪一次同步建链从 `Hixl::Connect` 到 `HixlClient::Connect` 的完整五层下沉路径。
3. 理解 `HixlClient` 如何用「控制面 socket + endpoint 匹配 + ClientHandler」完成一条远端链路的建立。
4. 理解 `ClientManager` 如何以 `remote_engine` 字符串为 key 管理多条链路，并提供幂等建链、心跳探活与请求索引。
5. 掌握 `AsyncConnectStatus` 七态状态机的迁移规则，理解 `ConnectPoolExecutor` 如何保证「同一远端同一时刻只执行一个建链/断链任务」。

## 2. 前置知识

- **建链（Connect）在 HIXL 中指什么**：u1-l3 已讲过 HIXL 是单边通信库，传输前 client 需要与 server 协商出可用的传输链路（HCCS 或 RDMA）。所谓「建链」，就是 client 通过 server 的控制面 socket 拿到对端 endpoint 列表，与本端 endpoint 匹配选出 link pair，再创建底层通道的过程。建链完成后才有数据面。
- **remote_engine 标识**：形如 `ip:port`（ipv4）或 `[ip]:port`（ipv6）的字符串，是远端 Hixl 实例的唯一标识，也是 `ClientManager` 里每条链路的 key。
- **控制面与数据面分离**：控制面走 TCP socket（`ctrl_socket_`），用来交换 endpoint 信息、发送 Notify 和心跳；数据面走 HCCS/RDMA。本讲会同时看到这两条通道。
- **ALREADY_CONNECTED**：一个特殊的非 `SUCCESS` 状态码，表示「链路已存在」，`ClientManager::GetOrCreateClient` 命中已有 client 时返回它，属于可恢复的业务提示而非错误。
- **u2-l3 的约束回顾**：解注册内存前必须断开全部链路（`HixlEngine::DeregisterMem` 会检查 `client_manager_.IsEmpty()`），所以断链顺序在工程上很重要。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/hixl/hixl.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl.h) | 公开类 `hixl::Hixl`，声明 Connect/Disconnect/ConnectAsync/DisconnectAsync/GetAsyncConnectStatus |
| [include/hixl/hixl_types.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl_types.h) | `AsyncConnectStatus` 七态枚举 |
| [src/hixl/engine/hixl_impl.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_impl.cc) | Pimpl 实现：门卫检查后转发给 engine；异步接口在这里包装成任务提交给 ConnectPoolExecutor |
| [src/hixl/engine/hixl_engine.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_engine.cc) | 引擎层：组装 `ClientConfig`，调用 `ClientManager` |
| [src/hixl/engine/hixl_client.h](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_client.h) / [.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_client.cc) | 一条远端链路的全生命周期管家：endpoint 协商、建链、传输、心跳、断链 |
| [src/hixl/engine/client_manager.h](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/client_manager.h) / [.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/client_manager.cc) | 多链路管理器：client 字典、per-engine 互斥、心跳线程、TransferReq 索引 |
| [src/hixl/engine/connect_pool_executor.h](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/connect_pool_executor.h) / [.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/connect_pool_executor.cc) | 异步建链/断链线程池与 `AsyncConnectStatus` 状态表 |
| [examples/cpp/hixl_example_quickstart.cpp](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_quickstart.cpp) | 本讲实践要改写的样例 |

## 4. 核心概念与源码讲解

### 4.1 同步建链与断链：从 Hixl::Connect 到 HixlClient::Connect

#### 4.1.1 概念说明

`Connect` 是同步接口：调用线程会一路阻塞到链路真正可用（或超时失败）才返回。`Disconnect` 同理。当你的业务只需要在启动阶段一次性连上少数几个远端（例如 PD 分离中 Decoder 启动时连 Prompt），同步接口最简单直接。

而当你需要**同时与几十上百个远端建链**（例如一张路由表中所有节点），逐个同步 Connect 的总耗时是各条链路串行累加的，此时就轮到 `ConnectAsync` 登场（见 4.4）。

#### 4.1.2 核心流程

一次同步 `Connect(remote_engine, timeout)` 的下沉路径：

```text
Hixl::Connect                      公开外壳，锁 + 门卫
  └─ HixlImpl::Connect            检查 engine 非空且已初始化
      └─ HixlEngine::Connect      组装 ClientConfig（含本端注册内存列表）
          └─ ClientManager::GetOrCreateClient
              ├─ 已存在？ → 返回 ALREADY_CONNECTED（幂等）
              └─ 新建：
                  ├─ CreateClient → ParseListenInfo 解析 ip/port
                  │   └─ HixlClient::Initialize  控制面协商 + 匹配 endpoint + 创建 handler
                  ├─ HixlClient::SetLocalMemInfo  把本端已注册内存告知 handler
                  └─ HixlClient::Connect          真正建立数据面链路
```

`Disconnect(remote_engine)` 则走 `ClientManager::DestroyClient`：从字典摘除、清理请求索引、调用 `HixlClient::Finalize` 断链销毁。

#### 4.1.3 源码精读

公开接口声明，5 个链路接口默认超时都是 1000ms：[include/hixl/hixl.h:75-L114](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl.h#L75-L114)——`Connect`(L75)、`Disconnect`(L83)、`ConnectAsync`(L91)、`DisconnectAsync`(L99)、两个重载的 `GetAsyncConnectStatus`(L107/L114)。

Impl 层的同步 Connect 只做门卫检查后转发：[src/hixl/engine/hixl_impl.cc:128-L133](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_impl.cc#L128-L133)。

引擎层组装配置并委托管理器，注意两处细节——禁止自己连自己、`ALREADY_CONNECTED` 被当作特殊状态透传：[src/hixl/engine/hixl_engine.cc:143-L166](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_engine.cc#L143-L166)。

断链相对简单，直接销毁对应 client：[src/hixl/engine/hixl_engine.cc:168-L178](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_engine.cc#L168-L178)。

quickstart 样例中的典型用法（client 在注册内存之后、传输之前调用）：[examples/cpp/hixl_example_quickstart.cpp:162-L168](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_quickstart.cpp#L162-L168)，`Connect → TransferSync → Disconnect` 的顺序正是 u1-l3 总结的调用合同。

#### 4.1.4 代码实践

1. **实践目标**：验证 `Connect` 的幂等语义与错误路径。
2. **操作步骤**：在 quickstart 样例的 `Connect` 成功后，紧接着再调用一次 `ctx.engine.Connect(kServerEngine, kTimeoutMs)`，打印返回值；再尝试 `Connect(kClientEngine, ...)`（连自己）并打印返回值。
3. **需要观察的现象**：第二次 Connect 不应报致命错误；连自己的调用应被拒绝。
4. **预期结果**：第二次 Connect 返回 `ALREADY_CONNECTED`（不等于 `hixl::SUCCESS`，但链路仍可用）；连自己返回 `PARAM_INVALID`（来自 [hixl_engine.cc:145-L148](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_engine.cc#L145-L148) 的自连检查）。
5. 以上行为推断自源码，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `HixlEngine::Connect` 对 `ALREADY_CONNECTED` 用 `HIXL_CHK_BOOL_RET_SPECIAL_STATUS` 而不是普通错误检查？

**答案****：`ALREADY_CONNECTED` 表示链路已存在，属于「不是新建立但结果符合预期」的可恢复状态，需要原样透传给用户而不是记为失败并改写返回值；普通检查宏会把它当错误统一包装。

**练习 2**：如果跳过 `Connect` 直接 `TransferSync`，会在哪一层被拦截？

**答案**：在 `HixlClient::TransferSync` 中被 `is_connected_` 门卫拦截，返回 `NOT_CONNECTED`（见 [hixl_client.cc:175](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_client.cc#L175)）。更早的话，若链路从未建立，`ClientManager::GetClient` 找不到 client，引擎层就会先失败。

### 4.2 HixlClient：一条远端链路的全生命周期管家

#### 4.2.1 概念说明

`HixlClient` 是「本端到某一个远端 engine」这条链路的具象：它持有 server 的 ip/port、两端 engine 标识、RDMA 服务参数、控制面 socket (`ctrl_socket_`)、以及真正干活的 `client_handler_`。一个 `HixlClient` 实例对应恰好一个远端，多个远端就是多个实例，由 `ClientManager` 统一保管。

类定义见 [src/hixl/engine/hixl_client.h:40-L145](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_client.h#L40-L145)。注意头文件注释里明确 `mutex_` 的约束：「所有方法串行执行，不支持并发调用」（L141），同一 client 上的操作由内部互斥保证安全，但用户不应在多线程里同时操作同一条链路。

`ClientConfig`（[hixl_client.h:28-L38](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_client.h#L28-L38)）是创建 client 的参数包：endpoint 列表、两端 engine 标识、RDMA tc/sl、超时、可选 qos 与 max_active_channels、以及 `is_lazy` 懒建链开关。

#### 4.2.2 核心流程

`HixlClient::Initialize` 的协商流程（注意：建 client 时就完成了控制面协商，`Connect` 只是最后按下数据面的「启动键」）：

```text
1. CtrlMsgPlugin::Connect(ip, port)          建立 TCP 控制面连接
2. SendEndpointInfoReq(kGetEndpointInfoReq)  索要对端 endpoint 列表
3. RecvEndpointInfoResp                      收 JSON 并反序列化为 remote_endpoint_list
4. EndpointMatcher::MatchEndpoints           本端×对端匹配 → link_pairs_ + handler 类型
5. ClientHandlerFactory::Create              按匹配结果创建 Direct/UB 等 handler
```

`HixlClient::Connect` 把工作委托给 `client_handler_->Connect(timeout)`，成功后置 `is_connected_ = true`。`Finalize` 负责断链：关控制面 socket、调 handler 的 Finalize、清空传输请求表。`CheckAlive` 在控制面上发一个心跳报文探测对端是否存活。

#### 4.2.3 源码精读

Initialize 全流程，注意 `HIXL_DISMISSABLE_GUARD` 保证中途失败时关闭 socket、成功时解除该保护：[src/hixl/engine/hixl_client.cc:53-L92](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_client.cc#L53-L92)。其中 L69-L71 调用 `EndpointMatcher::MatchEndpoints` 完成本端与远端 endpoint 的两两匹配（u3-l3 会深入），L88 经工厂创建 handler（u3-l2 会深入）。

Connect 本体，注意 `ALREADY_CONNECTED` 被豁免 dump_guard（已连上不算失败）：[src/hixl/engine/hixl_client.cc:150-L168](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_client.cc#L150-L168)。

Finalize 断链，幂等（重复调用直接返回 SUCCESS）：[src/hixl/engine/hixl_client.cc:260-L285](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_client.cc#L260-L285)。

CheckAlive 心跳：拼一个 `kHeartBeat` 控制消息发送，若 errno 属于连接断开族（EPIPE/ECONNRESET/ETIMEDOUT 等，见 [hixl_client.cc:47-L50](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_client.cc#L47-L50)）则关 socket 返回 FAILED，否则当普通告警处理：[src/hixl/engine/hixl_client.cc:337-L367](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_client.cc#L337-L367)。

#### 4.2.4 代码实践

1. **实践目标**：用日志观察一次建链的协商细节。
2. **操作步骤**：构建带样例的工程（u1-l2 的 `bash build.sh --examples`），运行 quickstart 前把 HIXL 日志级别调到能输出 EVENT（日志环境变量的具体名称参见 `src/hixl/common/hixl_log.h` 与 docs），然后启动 server/client。
3. **需要观察的现象**：日志中应依次出现 `[HixlClient] RecvEndpointInfoResp: receiving remote_endpoint_list body ...`、`[HixlClient] link selected, ... handler:xxx, pair_count:N`、`[HixlClient] connect link start/success`。
4. **预期结果**：能从日志读出实际选中的 handler 类型与 link pair 数量，与 u1-l5 讲的 `--protocol` 参数对上。
5. 具体日志级别开关名**待确认**（需查看 `hixl_log.h` 中注册级别的方式），整体流程**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `HixlClient` 的控制面协商（endpoint 交换）放在 `Initialize` 而不是 `Connect` 里？

**答案**：协商是「准备材料」——知道对端有哪些 endpoint、选出 link pair、创建好 handler；`Connect` 只是执行数据面链路建立。把两者分开后，`ClientManager` 可以在 `Initialize` 与 `Connect` 之间插入 `SetLocalMemInfo`（把本端注册内存先行告知 handler），保证数据面一建立即可传输。

**练习 2**：`CheckAlive` 发送心跳失败但 errno 不在断开族（例如临时 EAGAIN 类可重试错误）时返回什么？

**答案**：返回 `SUCCESS` 并打告警日志（[hixl_client.cc:362-L364](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_client.cc#L362-L364)）。设计意图是单次瞬时失败不触发断链，只有确定性断开才销毁链路。

### 4.3 ClientManager：多条远端链路的管理者

#### 4.3.1 概念说明

一个 Hixl 实例可能同时连接多个远端（多 Prompt、多 Decoder），`ClientManager` 就是这些 `HixlClient` 的容器与协调者。它的职责有四块：

1. **client 字典**：`clients_` 以 `remote_engine` 字符串为 key 存 `shared_ptr<HixlClient>`，实现 `Connect` 的幂等。
2. **per-engine 互斥**：`client_mutexes_` 为每个 remote_engine 单独配一把互斥锁，让**不同**远端的建链/销毁可以并发，**同一**远端的操作串行。
3. **心跳线程**：仅当初始化时开启了 `auto_connect` 选项（u2-l1 提过的 `OPTION_AUTO_CONNECT`）才启动，每 10 秒对全部 client `CheckAlive`，失活的自动销毁。
4. **TransferReq 索引**：`ordered_reqs_`（list，保序）+ `req_to_client_`（hash map）把异步传输句柄映射回所属 client，支撑按序轮询状态（u2-l5 会用到 `GetOrderedReqs`）。

#### 4.3.2 核心流程

`GetOrCreateClient` 的逻辑：

```text
取 remote_engine 对应的 per-engine 锁并持有
├─ 查 clients_ 命中 → 返回 ALREADY_CONNECTED
└─ 未命中：
    CreateClient：解析 remote_engine → new HixlClient → Initialize（控制面协商）
    SetLocalMemInfo：登记本端内存
    Connect：建立数据面
    失败 → fail_guard 自动 Finalize 半成品 client
    成功 → 加入 clients_ 字典
```

心跳循环（`auto_connect=true` 时）：

```text
每 10s：
  拷贝一份 clients_ 快照（避免持锁调用）
  对每个 client 调 CheckAlive
  返回 FAILED → DestroyClient（自动清理失活链路）
```

#### 4.3.3 源码精读

GetOrCreateClient 全文，注意 fail_guard 在 L81 注册、成功路径 L87 解除：[src/hixl/engine/client_manager.cc:63-L93](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/client_manager.cc#L63-L93)。`CreateClient` 里用 `ParseListenInfo` 从 remote_engine 字符串解析 ip/port（[client_manager.cc:47-L61](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/client_manager.cc#L47-L61)），这与 u2-l1 讲的引擎标识解析是同一个函数。

心跳的启动条件与间隔常量：[src/hixl/engine/client_manager.cc:22-L45](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/client_manager.cc#L22-L45)（`kHeartbeatIntervalMs = 10000`），SendHeartbeat 的快照遍历与自动销毁：[client_manager.cc:232-L246](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/client_manager.cc#L232-L246)。

DestroyClient：摘字典 → 清请求索引 → Finalize client → 回收 per-engine 锁：[src/hixl/engine/client_manager.cc:182-L205](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/client_manager.cc#L182-L205)。若 remote_engine 不存在，返回 `NOT_CONNECTED`（这就是对未建链的远端调 `Disconnect` 时的返回值）。

#### 4.3.4 代码实践

1. **实践目标**：搞清楚多远端建链在管理器层面的行为。
2. **操作步骤**：写一个源码阅读笔记任务——从 `HixlEngine::Connect` 出发，回答三个问题：(a) 两个线程同时对**同一** remote_engine 调 `Connect` 会发生什么？(b) 同时对**两个不同** remote_engine 呢？(c) 为什么 `SendHeartbeat` 要先拷贝 `clients_` 快照而不是直接遍历？
3. **需要观察的现象**：无运行需求，纯代码推演，但要能在源码中指出对应锁的行号。
4. **预期结果**：(a) 第二个线程在 per-engine 锁上等待，进入后查 `clients_` 命中，返回 `ALREADY_CONNECTED`；(b) 各持各的 per-engine 锁，真正并发建链；(c) 避免持 `mutex_` 期间调用可能阻塞的 `CheckAlive`/`DestroyClient`，防止与 `GetClient` 等读操作互相卡死。
5. 结论可直接由 [client_manager.cc:63-L93](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/client_manager.cc#L63-L93) 与 [client_manager.cc:232-L246](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/client_manager.cc#L232-L246) 推出，属源码阅读型实践，可直接完成。

#### 4.3.5 小练习与答案

**练习 1**：`DestroyClient` 最后为什么要把 per-engine 锁也从 `client_mutexes_` 里删掉（`DestroyClientMutex`）？

**答案**：per-engine 锁只为「对该远端的创建/销毁临界区」而存在，链路销毁后继续保留会让 `client_mutexes_` 随历史远端数量无限增长；删除即可回收。又因为锁本身是 `shared_ptr`，正在使用它的线程仍持有引用，不会悬空。

**练习 2**：`ClientManager::Finalize` 里为什么先 `stop_signal_ = true` 并 `notify_all`、`join` 心跳线程，然后才逐个 Finalize client？

**答案**：防止心跳线程在 client 已被销毁后又对其调用 `CheckAlive`/`DestroyClient` 造成竞态；先停线程再清理是标准的「生产者先停」关闭顺序。

### 4.4 异步建链：ConnectPoolExecutor 与 AsyncConnectStatus 状态机

#### 4.4.1 概念说明

`ConnectAsync` 把建链这件事变成一个任务丢进线程池，调用立即返回；链路进展通过 `GetAsyncConnectStatus` 查询。状态由 `AsyncConnectStatus` 七态枚举描述（[include/hixl/hixl_types.h:99-L107](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl_types.h#L99-L107)）：

| 状态 | 含义 |
| --- | --- |
| `NOT_CONNECT` | 从未发起或已彻底断开（状态表里没有记录就是它） |
| `CONNECT_PENDING` | 任务已入队，还没轮到执行 |
| `CONNECTING` | worker 正在执行建链 |
| `CONNECTED` | 建链成功 |
| `CONNECT_FAILED` | 建链失败 |
| `DISCONNECT_PENDING` / `DISCONNECTING` | 断链任务的入队/执行中 |

`ConnectPoolExecutor` 是这个机制的执行引擎：默认 **2 个 worker 线程**、任务队列容量 **128**（可被 `GlobalResourceConfig` 的 `connect_pool.thread_num` / `task_queue_capacity` 覆盖，范围分别限 [1,64] 与 [1,65535]，见 [connect_pool_executor.cc:16-L22](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/connect_pool_executor.cc#L16-L22) 与 [connect_pool_executor.cc:33-L60](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/connect_pool_executor.cc#L33-L60)）。

#### 4.4.2 核心流程

状态迁移图：

```text
Submit(ConnectAsync)          worker 取任务            task 执行完
NOT_CONNECT ──→ CONNECT_PENDING ──→ CONNECTING ──┬──→ CONNECTED      (engine_->Connect 成功或 ALREADY_CONNECTED)
                                                 └──→ CONNECT_FAILED (其它返回值)

Submit(DisconnectAsync)        worker 取任务           task 执行完
任意状态 ──→ DISCONNECT_PENDING ──→ DISCONNECTING ──→ NOT_CONNECT（记录被删除）
```

两条防竞态规则（`SetStatus` 中的「用户侧最新操作优先」逻辑，[connect_pool_executor.cc:104-L126](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/connect_pool_executor.cc#L104-L126)）：

- 当前状态是 `DISCONNECT_PENDING` 时，拒绝被旧的 connect 任务结果（CONNECTING/CONNECTED/CONNECT_FAILED）覆盖；
- 当前状态是 `CONNECT_PENDING` 时，拒绝被旧的 disconnect 任务结果（DISCONNECTING/NOT_CONNECT）覆盖。

也就是说，用户刚刚提交的新意图优先于后台旧任务的滞后回报。此外 worker 用 `task_doing_set_` 保证**同一 remote_engine 同一时刻只执行一个任务**（[connect_pool_executor.cc:177-L187](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/connect_pool_executor.cc#L177-L187)）。

值得注意的是：异步任务内部执行的仍然是**同一个同步** `engine_->Connect`——`ConnectAsync` 不是另一条建链代码路径，只是把同步路径搬进后台线程并外接一张状态表。另一个细节是 worker 启动时会 `ctx_.SetCurrentContext()` 恢复初始化线程的 aclrt 上下文（[connect_pool_executor.cc:146-L148](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/connect_pool_executor.cc#L146-L148)），因为 ACL 的设备操作绑定线程上下文。

#### 4.4.3 源码精读

`ConnectAsync` 把同步 Connect 包成 lambda 并提交，任务结束时按返回值写终态：[src/hixl/engine/hixl_impl.cc:142-L154](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_impl.cc#L142-L154)。`ALREADY_CONNECTED` 也算 `CONNECTED`（L148-L149）。

`DisconnectAsync` 同构，终态固定写回 `NOT_CONNECT`：[src/hixl/engine/hixl_impl.cc:156-L166](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_impl.cc#L156-L166)。

状态查询两个重载——查单个（查不到返回 `NOT_CONNECT`）与查全部：[src/hixl/engine/hixl_impl.cc:168-L177](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_impl.cc#L168-L177)，底层实现在 [connect_pool_executor.cc:128-L140](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/connect_pool_executor.cc#L128-L140)。

`Submit`：队满返回 `RESOURCE_EXHAUSTED`，入队即写 `CONNECT_PENDING`/`DISCONNECT_PENDING`：[connect_pool_executor.cc:83-L102](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/connect_pool_executor.cc#L83-L102)。

Worker 主循环：条件变量等到有「可执行任务」（该 remote 未在执行中）才醒，取出时写 `CONNECTING`/`DISCONNECTING`，执行完从 `task_doing_set_` 摘除并唤醒下一个 worker：[connect_pool_executor.cc:146-L203](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/connect_pool_executor.cc#L146-L203)。

`HixlImpl::Finalize` 会先 `connect_pool_executor_.Shutdown()` 再销毁引擎（[hixl_impl.cc:102-L110](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_impl.cc#L102-L110)），Shutdown 置位停止标志并 join 全部 worker（[connect_pool_executor.cc:62-L81](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/connect_pool_executor.cc#L62-L81)），保证 `Finalize` 返回后不再有后台建链在跑。

#### 4.4.4 代码实践

见下文第 5 节综合实践（把 quickstart 的同步 Connect 换成 ConnectAsync + 轮询）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `GetAsyncConnectStatus` 查询一个从未发起过连接的 remote_engine 不报错，而是返回 `NOT_CONNECT`？

**答案**：`ConnectPoolExecutor::GetStatus` 在 `task_result_` 表中找不到 key 时直接把出参置为 `NOT_CONNECT` 并返回 `SUCCESS`（[connect_pool_executor.cc:128-L133](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/connect_pool_executor.cc#L128-L133)）。「无记录」本身就是一种合法状态（从未连接或已彻底断开），把异常查询变成正常状态查询，用户侧代码更简单。

**练习 2**：假设你在 ConnectAsync 后立刻 DisconnectAsync，随后旧 connect 任务执行成功了，最终状态是什么？

**答案**：最终是 `DISCONNECT_PENDING` → `DISCONNECTING` → `NOT_CONNECT`（断链任务完成后记录被删除）。中途旧 connect 任务想写 `CONNECTED` 时，会被 `SetStatus` 的第一条防竞态规则拦下（当前已是 `DISCONNECT_PENDING`），保证用户看到的最后状态与最后一次操作一致，而不是被滞后的旧结果翻转。

**练习 3**：`Submit` 返回 `RESOURCE_EXHAUSTED` 意味着什么？该怎么办？

**答案**：任务队列已满（默认 128 个待执行任务），说明积压的建/断链任务远超 worker 消化速度。可以等待任务消化后重试，或通过 `GlobalResourceConfig` 调大 `connect_pool.task_queue_capacity` / `thread_num`（上限 65535 / 64）。

## 5. 综合实践

**任务**：改写 quickstart 样例，把同步 `Connect` 替换为 `ConnectAsync` + `GetAsyncConnectStatus` 轮询，亲眼看到一次异步建链的状态迁移。

**改造点**在 [examples/cpp/hixl_example_quickstart.cpp:162](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_quickstart.cpp#L162)。示例代码（非项目原有代码）：

```cpp
// 原代码：
// HixlExitOnFailure(ctx.engine.Connect(kServerEngine, kTimeoutMs), "Connect");

// 改为异步建链 + 轮询：
HixlExitOnFailure(ctx.engine.ConnectAsync(kServerEngine, kTimeoutMs), "ConnectAsync");
hixl::AsyncConnectStatus st = hixl::AsyncConnectStatus::NOT_CONNECT;
for (int i = 0; i < kTimeoutMs; ++i) {          // 最多轮询 timeout_ms 次
  HixlExitOnFailure(ctx.engine.GetAsyncConnectStatus(kServerEngine, st), "GetAsyncConnectStatus");
  printf("[INFO] connect status = %d\n", static_cast<int>(st));
  if (st == hixl::AsyncConnectStatus::CONNECTED) { break; }
  if (st == hixl::AsyncConnectStatus::CONNECT_FAILED) { HixlExitOnFailure(false, "ConnectAsync failed"); }
  usleep(1000);                                  // 1ms 轮询间隔
}
```

**操作步骤**：

1. 按 u1-l2 构建样例：`bash build.sh --examples`。
2. 按上述补丁修改 quickstart（可复制为独立文件并在 examples 的 CMake 中登记，或直接改后本地编译验证、验证完还原）。
3. 按 u1-l3 的方式在两台互通 device 上分别启动 server 与 client。
4. 为更容易观察到中间状态，可以在轮询前人为加 `usleep`，或把 client 的 `kTimeoutMs` 调大。

**需要观察的现象与预期结果**：

- client 侧打印的状态序列应呈现 `CONNECT_PENDING(1) → CONNECTING(2) → CONNECTED(3)` 的迁移；若 server 未启动，则最终停在 `CONNECT_FAILED(4)`。
- `ConnectAsync` 本身应立即返回 `SUCCESS`（它只负责入队）。
- 建链成功后 `TransferSync` 与 `Disconnect` 行为与原版完全一致——验证了「异步只是换了执行时机，链路产物相同」。
- 本实践需要真实的双 device 环境，**待本地验证**。

## 6. 本讲小结

- 链路接口共 5 个：`Connect`/`Disconnect`（同步、阻塞到结果）、`ConnectAsync`/`DisconnectAsync`（入队即返回）、`GetAsyncConnectStatus`（单查/全查状态）。
- 同步建链五层下沉：`Hixl` → `HixlImpl`（门卫）→ `HixlEngine`（组装 ClientConfig，禁止自连，透传 `ALREADY_CONNECTED`）→ `ClientManager`（幂等获取或创建）→ `HixlClient`。
- `HixlClient` 是单条远端链路的管家：`Initialize` 阶段就完成控制面 socket 协商与 endpoint 匹配并创建 handler，`Connect` 只负责数据面建立；同一 client 的所有方法内部串行。
- `ClientManager` 以 `remote_engine` 为 key 管理多链路：per-engine 互斥让不同远端可并发建链；`auto_connect` 开启时心跳线程每 10s 探活并自动清理失活链路。
- 异步建链由 `ConnectPoolExecutor`（默认 2 线程、队列容量 128）执行，任务内部仍是同一个同步 `engine_->Connect`；`AsyncConnectStatus` 七态状态机用 `SetStatus` 的防竞态规则保证「用户最新操作优先于滞后回报」，且同一远端同一时刻只执行一个任务。

## 7. 下一步学习建议

链路建好之后，下一讲 **u2-l5 数据传输与通知** 将沿着本讲建立的 `HixlClient`/`ClientManager` 继续下沉：`TransferSync`/`TransferAsync` 如何经 `ClientHandler` 真正搬运数据、`GetTransferStatus` 如何利用本讲提到的 `GetOrderedReqs` 请求索引按序轮询。如果你对建链时「endpoint 如何匹配出 link pair」「Direct/UB handler 如何选择」感兴趣，可以提前跳到单元三的 u3-l2（ClientHandler）与 u3-l3（Endpoint 生成与匹配）；想了解支撑异步建链的通用并发组件（ThreadPool、PeriodicTask），见 u3-l4。

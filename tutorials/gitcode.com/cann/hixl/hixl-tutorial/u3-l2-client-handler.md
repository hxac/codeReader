# ClientHandler：不同链路的传输处理策略

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `IClientHandler` 接口面包含哪几组能力，以及它在 HixlClient 与 CS 层之间的位置。
2. 对比 `DirectClientHandler`（单链路直通）与 `UbClientHandler`（多链路分组、按内存类型选路）两种实现在建链、注册、传输上的差异。
3. 解释 `ClientHandlerFactory::Create` 的分派逻辑，以及 `handler_type` 这一决策结果从哪里来（`EndpointMatcher` 的优先级匹配规则表）。
4. 梳理一次 `TransferAsync` 调用从 `HixlClient` 下沉到具体 ClientHandler、再到 CS 层 C 接口的完整分派路径。

## 2. 前置知识

阅读本讲前，你需要先理解以下概念（均在前几讲建立）：

- **单边传输与 CS 层**：HIXL 的数据面由 CS 模块（`src/hixl/cs/`）承担，对外暴露 `extern "C"` 风格的 `HixlCSClientCreate/HixlCSClientConnect/HixlCSClientBatchPutAsync` 等接口（见 u1-l4 的 API 地图与 u4 单元预告）。`HixlClientHandle` 是 CS 层 client 的不透明句柄。
- **engine 标识与建链流程**：u2-l4 讲过，`HixlClient::Initialize` 阶段会通过控制面 TCP socket 与对端交换 endpoint 信息。本讲要回答的问题是：交换完 endpoint 之后，「用哪条链路、由谁来管」是如何决定的。
- **Endpoint 与链路类型**：endpoint 是通信资源的标识，携带 `protocol`（如 uboe、roce、hccs、ub_ctp）、`placement`（device/host）、`plane`、`dst_eid` 等字段。一个 engine 通常会暴露**多条**不同协议的 endpoint（u3-l3 将专门讲 endpoint 的生成）。
- **MemType 与传输方向**：MEM_DEVICE/MEM_HOST 两类内存 × 本地/远端两个位置，组合出 D2D、D2H、H2D、H2H 四种传输路径（u1-l5 的路径记号）。
- **auto_connect（懒惰建链）**：u2-l4/u2-l5 提过，开启 `auto_connect` 选项后传输接口可隐式建链。本讲会看到它在 handler 层的真正落点：`is_lazy` 标志。

一个直观的比喻：`HixlClient` 像一位「发货员」，它知道收货方是谁（remote_engine），但一个收货方可能有多个仓库入口（多条链路）。ClientHandler 就是「物流方案」——有的方案只走一个固定入口（Direct），有的方案要根据货物种类（device 内存还是 host 内存）分拣到不同入口（UB）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/hixl/engine/client_handler.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/client_handler.h) | 定义 `IClientHandler` 纯虚接口、`CommType` 枚举与状态转换辅助函数 |
| [src/hixl/engine/client_handler_factory.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/client_handler_factory.h) | 定义 `HandlerCreateArgs`（handler 创建参数包）与 `ClientHandlerFactory` |
| [src/hixl/engine/client_handler_factory.cc](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/client_handler_factory.cc) | 工厂分派：按 `handler_type` 创建 Direct 或 UB handler |
| [src/hixl/engine/direct_client_handler.cc](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/direct_client_handler.cc) | 单链路实现：只持有 matched_pairs[0]，一对一封装 CS C 接口 |
| [src/hixl/engine/ub_client_handler.cc](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/ub_client_handler.cc) | 多链路实现：按 CommType 持有多把 handle，按内存类型分类下发 |
| [src/hixl/engine/hixl_client.cc](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_client.cc) | 调用方：endpoint 交换 → 匹配 → 工厂创建 → 后续所有操作转发给 handler |
| [src/hixl/engine/endpoint_matcher.cc](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/endpoint_matcher.cc) | 决策源头：优先级规则表决定 `handler_type` 与 matched_pairs |
| [src/hixl/engine/client_handler_config_helper.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/client_handler_config_helper.h) | 把 qos/max_active_channels 可选项拼成 JSON 配置串传给 CS 层 |

## 4. 核心概念与源码讲解

### 4.1 IClientHandler 接口：一条链路的统一抽象

#### 4.1.1 概念说明

u3-l1 讲过引擎层的 `Engine` 抽象：`HixlEngine` 内部按值持有被动方 `HixlServer` 与主动方管理器 `ClientManager`，而 `ClientManager` 为每个 remote_engine 维护一个 `HixlClient`。`HixlClient` 再往下，就是本讲的主角 `IClientHandler`。

为什么需要这一层抽象？因为「与一个远端 engine 通信」在不同硬件拓扑下是完全不同的工作量：

- 有的场景两端之间只有**一条**可用链路（比如一条 RoCE 网卡链路），handler 只需一对一封装 CS 接口；
- 有的场景（同实例 UB，即同一通信实例内的 UnifiedBus 内存语义）两端之间有**一组**链路，device 内存和 host 内存要走不同的通道，甚至一次批量传输里的多个 op 要**拆开**分发给不同通道。

`IClientHandler` 把这两种形态统一成同一组接口，让 `HixlClient` 完全不必关心底层有几条链路。

#### 4.1.2 核心流程

`IClientHandler` 的接口面与 `Hixl` 公开 API 一一对应（少了通知——通知走控制面 socket，不经过 handler）：

```text
Connect(timeout_ms)                    建立数据面链路
RegisterMem(mem_info)                  在链路上注册内存
TransferSync(op_descs, op, timeout)    同步批量传输
TransferAsync(op_descs, op, req)       异步批量传输，产出 TransferReq
GetTransferStatus(req, status)         查询异步传输状态
Finalize()                             释放全部资源（内存句柄、CS handle）
Dump(reason, level)                    打印内部状态（事件日志/错误日志）
```

此外，client_handler.h 还定义了本讲最核心的词汇表 `CommType`：8 种通信类型。其中 4 种 UB 路径类型（UB_D2D/UB_H2D/UB_D2H/UB_H2H）由「本地内存类型 × 远端内存类型」组合决定，另外 4 种（ROCE/HCCS/UBOE/UBG）是单一协议链路类型。

#### 4.1.3 源码精读

接口定义只有 13 行，是典型的「能力清单」式抽象基类：

[client_handler.h:74-86](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/client_handler.h#L74-L86)：定义 `IClientHandler` 纯虚接口，7 个纯虚函数覆盖建链、内存注册、同步/异步传输、状态查询、资源释放与状态转储，全部函数都是虚的，两个具体实现各自给出自己的语义。

[client_handler.h:25-34](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/client_handler.h#L25-L34)：定义 `CommType` 枚举，前 4 个值是 UB 组的四种内存路径组合（D2D/H2D/D2H/H2H），后 4 个值是独立协议类型（ROCE/HCCS/UBOE/UBG）。注意 UBG 的字符串名是 "UB_RTP"（见 [client_handler.h:68](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/client_handler.h#L68)），日志里看到 UB_RTP 就是对应这个枚举值。

[client_handler.h:38-49](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/client_handler.h#L38-L49)：`ToTransferStatus` 把 CS 层的 `HixlCompleteStatus` 翻译回公开 API 的 `TransferStatus`（u2-l2 讲过）。两个实现共用这个内联函数，避免重复。

#### 4.1.4 代码实践

**实践目标**：不运行任何程序，仅凭头文件把「接口能力清单」背下来，并确认两个实现类确实实现了全部接口。

**操作步骤**：

1. 打开 `src/hixl/engine/client_handler.h`，数一数纯虚函数个数（应为 7 个）。
2. 打开 `src/hixl/engine/direct_client_handler.h` 与 `src/hixl/engine/ub_client_handler.h`，逐个核对两个类的 public 方法是否与接口一一对应（都是 `override`）。
3. 对照 `include/hixl/hixl.h`（u2-l1），把 `Hixl` 的传输类公开接口与 `IClientHandler` 的接口连线。

**需要观察的现象**：`Hixl` 公开 API 中「通知」（SendNotify/GetNotifies）在 `IClientHandler` 中**没有**对应接口——通知不走数据面 handler。

**预期结果**：得到一张三列对照表：`Hixl` 公开接口 → `HixlClient` 方法 → `IClientHandler` 纯虚函数；通知一列在第三栏为空。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `GetNotifies` 不出现在 `IClientHandler` 接口里？

**答案**：通知（notify）是控制面信号，复用建链时的 TCP 控制面 socket（`HixlClient::SendNotify` 直接写 `ctrl_socket_`，见 [hixl_client.cc:369-406](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_client.cc#L369-L406)），不经过 CS 数据面，因此与 handler 无关。这也呼应了 u1-l3 讲过的「控制面与数据面分离」。

**练习 2**：`CommType::COMM_TYPE_UB_D2D` 与 `CommType::COMM_TYPE_ROCE` 表达的是同一维度的概念吗？

**答案**：不是。前 4 个 UB 类型描述的是「内存路径组合」（本地/远端内存类型决定），后 4 个描述的是「协议链路类型」（由匹配到的 endpoint 协议决定）。UB 类型只在 UB 分组匹配成功后由 `ParseCommType` 推导出来（见 4.3 节）。

---

### 4.2 DirectClientHandler：单链路直通

#### 4.2.1 概念说明

`DirectClientHandler` 是「一条 endpoint 对」的忠实封装：构造时只取 `args.matched_pairs[0]`，创建**一个** CS client handle，之后所有接口调用都一对一转译成 CS 层 C 接口。它适用于 HCCS、RoCE、UBOE、UB_RTP 这些「一条链路解决所有内存路径」的场景——这些协议本身不区分本地内存是 device 还是 host，由底层链路自行承载。

#### 4.2.2 核心流程

```text
Create(args):
  取 matched_pairs[0]
  EndpointConfig → EndpointDesc（两端各一次转换）
  组装 HixlClientDesc（server ip/port、两端 endpoint、rdma tc/sl）
  拼装 global_resource_config（qos / max_active_channels，可选）
  HixlCSClientCreate(...) → 得到唯一 handle_

Connect:      HixlCSClientConnect(handle_, timeout)（已连接则幂等返回 ALREADY_CONNECTED）
RegisterMem:  HixlCSClientRegMem(handle_, ...)
TransferAsync: WRITE→HixlCSClientBatchPutAsync / READ→HixlCSClientBatchGetAsync
              complete_handle 存入 complete_handles_ 映射表，req 即该句柄
TransferSync: WRITE→HixlCSClientBatchPutSync / READ→HixlCSClientBatchGetSync
GetTransferStatus: 查表 → HixlCSClientQueryCompleteStatus → ToTransferStatus
              查到终态即从表中摘除（与 u2-l5 讲的「首查终态即移除」一致）
Finalize:     逐个 UnregMem → HixlCSClientDestroy
```

注意 `WRITE` 对应 Put、`READ` 对应 Get——Put/Get 是按发起方视角命名的（把本地数据放到远端 / 从远端取回本地），与 u2-l2 讲的 TransferOp 语义完全对应。

#### 4.2.3 源码精读

[direct_client_handler.cc:23-47](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/direct_client_handler.cc#L23-L47)：`DirectClientHandler::Create`。只使用 `args.matched_pairs[0]`（第 24 行），把两端 EndpointConfig 转成 EndpointDesc 后调用 `HixlCSClientCreate` 创建唯一的 CS client handle。第 38-41 行通过 `ClientHandlerConfigHelper` 拼装可选的全局资源配置串。

[direct_client_handler.cc:78-100](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/direct_client_handler.cc#L78-L100)：`TransferAsync`。把公开 API 的 `TransferOpDesc` 三元组逐条转成 CS 层 `HixlOneSideOpDesc`（remote_buf/local_buf/len），按 `operation == WRITE` 分派到 `HixlCSClientBatchPutAsync` 或 `HixlCSClientBatchGetAsync`，最后把返回的 `CompleteHandle` 存入 `complete_handles_` 映射并把首地址强转为 `TransferReq` 返回给上层。

[direct_client_handler.cc:118-150](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/direct_client_handler.cc#L118-L150)：`GetTransferStatus`。用 `std::scoped_lock lock(mutex_, complete_handles_mutex_)` 同时锁住操作互斥与句柄表；找不到 req 返回 `PARAM_INVALID`（对应 u2-l5 讲的「再查已移除的请求报参数错」）；查到 `WAITING` 保留表项继续轮询，查到任何终态（COMPLETED 或失败）都把表项摘除。

[direct_client_handler.cc:152-167](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/direct_client_handler.cc#L152-L167)：`Finalize`。先清空在途请求表，再逐个解注册内存句柄，最后销毁 CS client handle。顺序是「请求 → 内存 → 链路」，保证不悬挂。

#### 4.2.4 代码实践

**实践目标**：验证「req 就是 CompleteHandle」这一映射关系，理解 u2-l5 中 TransferReq 的真正来源。

**操作步骤**：

1. 阅读 [direct_client_handler.cc:96-99](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/direct_client_handler.cc#L96-L99)，注意 `req = static_cast<TransferReq>(complete_handle)` 与 `complete_handles_[req] = complete_handle`。
2. 再阅读 [hixl_client.cc:185-197](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_client.cc#L185-L197)，注意 handler 返回 req 后，`HixlClient` 又把它登记进自己的 `req_map_`。
3. 画出这条「一个指针、两张台账」的链路图。

**需要观察的现象**：同一个 `TransferReq` 指针同时是 handler 层 `complete_handles_` 的 key 和 `HixlClient::req_map_` 的 key。

**预期结果**：理解 u2-l5 所说「请求登记在 ClientManager 与 req_map_ 两级台账」在 handler 层的落点——其实一共三级：`complete_handles_`（handler）、`req_map_`（HixlClient）、以及 ConnectPoolExecutor/ClientManager 侧的管理记录。用户拿到的 TransferReq 是不透明指针，只有持有台账的一方才能解释它。

#### 4.2.5 小练习与答案

**练习 1**：如果用户对一个已经查询到 COMPLETED 的 req 再次调用 GetTransferStatus，DirectClientHandler 会怎么返回？

**答案**：首次查到终态时表项已被 erase（[direct_client_handler.cc:148](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/direct_client_handler.cc#L148)），第二次查表时 `find` 落空，返回 `PARAM_INVALID` 且 status 置为 FAILED（[direct_client_handler.cc:126-130](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/direct_client_handler.cc#L126-L130)）。这正是 u2-l5 总结的行为。

**练习 2**：为什么 `TransferSync` 不需要 `complete_handles_` 表？

**答案**：同步接口阻塞到整批完成或超时，CS 层的 `HixlCSClientBatchPutSync/HixlCSClientBatchGetSync` 直接返回最终状态，不存在需要后续轮询的中间句柄，因此无需登记。

---

### 4.3 UbClientHandler：多链路分组与按内存分类选路

#### 4.3.1 概念说明

`UbClientHandler` 面对的是同实例（same-instance）UB 场景：endpoint 匹配会产生**一组** `matched_pairs`，典型情况下包含 D2D、D2H、H2D、H2H 四种 CommType 各一对。它为每一对各创建一个 CS client handle，用 `std::map<CommType, HixlClientHandle>` 持有。

它的三个独有职责：

1. **内存类型登记**：维护 `local_segments_`（本端注册内存的区间表）与 `remote_segments_`（通过控制面拉取的对端内存区间表）。
2. **传输分类**：每次传输前，根据每个 op 的本地/远端地址落在哪张区间表里，推导出该 op 的 MemType 组合，从而决定 CommType，把一批 op 拆分到不同链路。
3. **懒惰建链**：lazy 模式下 Connect 只是「记账」，真正的建链推迟到首次传输、且只建本次传输用到的链路类型。

#### 4.3.2 核心流程

传输分类的判定逻辑可以写成如下伪代码：

```text
对每个 op:
  local_type  = local_segments_ 中包含 [local_addr, local_addr+len) 的 Segment 的 MemType
  remote_type = remote_segments_ 中包含 [remote_addr, remote_addr+len) 的 Segment 的 MemType
  任一查不到 → PARAM_INVALID（地址未注册）
  CommType = (local_type, remote_type) 的组合：
      (DEVICE, DEVICE) → UB_D2D
      (DEVICE, HOST)   → UB_D2H
      (HOST,   DEVICE) → UB_H2D
      (HOST,   HOST)   → UB_H2H
按 CommType 分桶 → table

TransferAsync:
  ClassifyTransfers → table
  lazy 模式则先 EnsureLinksConnected(需要的类型)
  对每个 (type, descs)：用对应 handle 下发 BatchPutAsync/BatchGetAsync
  一个 TransferReq 聚合多个 BatchHandle（不再是单句柄！）
```

这就是 u2-l3 讲过的 Segment 抽象的用武之地：client handler 侧按内存类型各建一张有序区间表，「多区间拼接覆盖判定」正是为了支撑这里的 `Contains(addr, addr+len)` 查询。

懒惰建链的状态迁移：

```text
lazy 模式:
  Connect(第 1 次)  → connect_triggered_ = true，直接返回 SUCCESS（不碰链路）
  TransferSync/Async → EnsureLinksConnected(本次需要的 types)
                        只连接 connected_types_ 中还没有的类型
  Connect(第 2 次)   → ALREADY_CONNECTED
```

#### 4.3.3 源码精读

[ub_client_handler.cc:152-199](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/ub_client_handler.cc#L152-L199)：`UbClientHandler::Create`。遍历**全部** matched_pairs（与 Direct 只取 [0] 形成对比），为每对各调一次 `HixlCSClientCreate`，按 `pair.type` 存入 `handles_` 映射。随后第 193-196 行复用 `ctrl_socket` 向 server 拉取对端内存信息并构建 `remote_segments_`；第 182-186 行的 scope guard 保证后续步骤失败时已创建的 handles 被 Finalize 释放，避免泄漏。

[ub_client_handler.cc:96-129](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/ub_client_handler.cc#L96-L129)：匿名空间中的 `FetchRemoteMemInfo`。在控制面 socket 上发送 `kGetMemInfoReq`，接收 `kGetMemInfoResp`，body 是 JSON 数组，逐条解析出 `{type, addr, size}`。第 34 行限制 body 上限 4MB 是防御性校验。这就是「对端内存台账」的来源。

[ub_client_handler.cc:562-592](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/ub_client_handler.cc#L562-L592)：`ClassifyTransfers`，本类的心脏。先用 `ge::AddOverflow` 防止地址区间加法溢出，再分别在 `local_segments_`/`remote_segments_` 中查 MemType（查不到即返回 PARAM_INVALID，日志明确提示「内存未在连接前注册」），最后按 4.3.2 的真值表推导 CommType 并分桶。注意它加锁读取两张区间表（`local_seg_mutex_`/`remote_seg_mutex_`），且是 const 成员函数。

[ub_client_handler.cc:201-223](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/ub_client_handler.cc#L201-L223)：`Connect` 的三分支：非 lazy 模式直接 `ConnectHandles` 连全部链路（已连接则幂等返回 ALREADY_CONNECTED）；lazy 模式首次调用只置 `connect_triggered_` 并返回成功；再次调用返回 ALREADY_CONNECTED。

[ub_client_handler.cc:244-268](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/ub_client_handler.cc#L244-L268)：`ConnectHandles`。用临时 `ThreadPool("ub_connect", handles.size())` 并行连接多条链路，每个线程先 `SetCurrentContext` 恢复 ACL context 再调 `HixlCSClientConnect`；随后逐个 future 收集结果，成功的类型写入 `connected_types_`，任一失败立即返回。`OptionalAclrtContext` 的存在是因为子线程需要继承调用线程的 device context。

[ub_client_handler.cc:336-377](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/ub_client_handler.cc#L336-L377)：`TransferAsync`。先 `ClassifyTransfers` 分桶；lazy 模式下用分桶结果推导「本次需要哪些类型」并调 `EnsureLinksConnected`（只补缺失类型，见 [ub_client_handler.cc:225-242](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/ub_client_handler.cc#L225-L242)）；然后逐桶用各自 handle 下发异步批量操作。关键差异在第 373-375 行：`req = batch_handles[0].handle`，但 `complete_handles_[req]` 存的是**整组** BatchHandle——一个 TransferReq 聚合了多条链路上的多个 CS 句柄。

[ub_client_handler.cc:424-474](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/ub_client_handler.cc#L424-L474)：`GetTransferStatus` 的「聚合语义」：遍历该 req 的全部 BatchHandle 逐条查询，只要有一条还在 WAITING，整体就是 WAITING（不摘表）；全部 COMPLETED 才是 COMPLETED（摘表）；任何一条失败立刻整体失败（摘表）。这与 Direct 的单句柄查询语义在用户看来完全一致——多链路的复杂性被 handler 吸收了。

[ub_client_handler.cc:379-422](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/ub_client_handler.cc#L379-L422)：`TransferSync`。同样先分桶，lazy 模式下用 `ComputeRemainingMs`（[ub_client_handler.cc:131-140](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/ub_client_handler.cc#L131-L140)）把「总超时」换算成「剩余超时」再补建链路；逐桶串行调用 CS client 的 `BatchTransferSync`（这里直接 `static_cast<HixlCSClient*>` 使用 C++ 接口而非 C 包装），每一桶都重新计算剩余时间，保证总耗时不超过用户给的 timeout_ms。

#### 4.3.4 代码实践

**实践目标**：通过日志观察一次 UB 传输的链路分类结果（无硬件环境时改为源码推演）。

**操作步骤**：

1. 打开 [ub_client_handler.cc:586-589](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/ub_client_handler.cc#L586-L589)，抄下 CommType 真值表。
2. 假设一次 TransferAsync 下发 4 个 op：2 个落在（本地 device、远端 device）区间，2 个落在（本地 host、远端 device）区间，写出分桶结果。
3. （可选，需昇腾环境）跑通 u1-l3 的 quickstart 后，把引擎日志级别调到 EVENT，观察 `[HixlClient] link selected ... handler:UB` 与 `EndpointMatcher pair[...]` 日志（[hixl_client.cc:73-83](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_client.cc#L73-L83)）。**待本地验证**。

**需要观察的现象**：步骤 2 中 4 个 op 应被拆成两桶：UB_D2D 桶 2 条、UB_H2D 桶 2 条；两个桶各产生一个 CompleteHandle，聚合进同一个 TransferReq。

**预期结果**：手工分桶结果与 `ClassifyTransfers` 的真值表一致；能解释为什么这个 req 的 GetTransferStatus 要等两个 handle 都完成才算 COMPLETED。

#### 4.3.5 小练习与答案

**练习 1**：如果用户注册了本地内存但忘了让对端注册（remote_segments_ 为空），传输时会发生什么？

**答案**：`ClassifyTransfers` 在查 `remote_segments_` 时找不到覆盖 `remote_addr` 的区间，返回 `PARAM_INVALID`，日志提示 "Remote memory range is not registered before connection"（[ub_client_handler.cc:579-585](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/ub_client_handler.cc#L579-L585)）。注意 remote_segments_ 是 Create 时一次性拉取的，因此「先建链后在对端注册内存」的顺序在 UB handler 下行不通——这解释了 u1-l3 强调的「先注册、后建链」合同。

**练习 2**：lazy 模式（auto_connect）下，第一次只传 host→device 数据，D2D 链路会被建立吗？

**答案**：不会。`TransferAsync` 只用本次分桶得到的类型（UB_H2D）调用 `EnsureLinksConnected`，`connected_types_` 中没有 D2D 就不会被触碰（[ub_client_handler.cc:341-347](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/ub_client_handler.cc#L341-L347) 与 [ub_client_handler.cc:228-237](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/ub_client_handler.cc#L228-L237)）。直到某次传输真的出现 device 内存对，D2D 链路才按需建立。

**练习 3**：`TransferSync` 里为什么要反复调用 `ComputeRemainingMs` 而不是每桶都用原始 timeout_ms？

**答案**：用户给的 timeout_ms 是**整次调用**的总预算。分桶后逐桶串行执行，若每桶都重用全额超时，最坏情况总耗时可达 桶数 × timeout_ms。用起始时间点折算剩余时间（[ub_client_handler.cc:131-140](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/ub_client_handler.cc#L131-L140)）才能保证整体不超预算。

---

### 4.4 ClientHandlerFactory 与 handler 选择机制

#### 4.4.1 概念说明

工厂本身极其朴素：一个静态 `Create`，按 `args.handler_type` 二选一。真正的「决策」发生在更早的地方——`HixlClient::Initialize` 调用 `EndpointMatcher::MatchEndpoints`，由一张**静态优先级规则表**同时决定两件事：

1. 选哪几对 endpoint（matched_pairs）；
2. 用哪种 handler（handler_type：DIRECT 还是 UB）。

`HandlerCreateArgs` 则是贯穿两者的参数包：它携带 server 地址、RDMA tc/sl、matched_pairs、qos/max_active_channels 可选项、is_lazy、timeout、ctrl_socket、两端 engine 标识——工厂与两个 Create 静态方法需要的全部信息。

#### 4.4.2 核心流程

决策全流程：

```text
HixlClient::Initialize
  ├─ 控制面 socket 连接 + 交换 endpoint 列表（u2-l4 讲过）
  ├─ EndpointMatcher::MatchEndpoints(local, remote, matched_pairs, handler_type)
  │    ├─ IsCrossInstance：比较两端首个 endpoint 的 net_instance_id
  │    └─ TryMatchByPriority：按规则表逐条尝试
  │         规则表（同实例）：① UB GROUP（优先）
  │                           ② hccs/device ③ uboe/device ④ ub_rtp/device
  │                           ⑤ roce/device ⑥ roce/host
  │         规则表（跨实例）：① uboe/device ② ub_rtp/device
  │                           ③ roce/device ④ roce/host
  │         特例：同实例且两端 ub_ctp endpoint 全为 device 时
  │               先试「仅 D2D 的 UB 分组」规则
  │         命中一条 → handler_type = 规则指定的类型，停止
  │         全部落空 → PARAM_INVALID（endpoint 匹配失败）
  └─ ClientHandlerFactory::Create(args)
       handler_type == DIRECT → DirectClientHandler::Create
       否则                   → UbClientHandler::Create
```

`is_lazy` 的来源也要在这里点破：`HixlEngine::FillClientConfigFields` 把引擎的 `auto_connect_` 选项直接填进 `config.is_lazy`（[hixl_engine.cc:362-373](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_engine.cc#L362-L373)），显式 `Connect` 时则强制 `config.is_lazy = false`（[hixl_engine.cc:155](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_engine.cc#L155)）。也就是说：**auto_connect 开启 ⇒ 隐式建链走 lazy 路径 ⇒ UB handler 把建链推迟到传输时刻**。

配置 helper 的作用则很小但很关键：CS 层的 `HixlClientConfig.global_resource_config` 期望 JSON 字符串，而 qos/max_active_channels 是 `std::optional`；直接 `json.dump()` 空 json 会得到 `"null"` 字符串而非空串，所以 helper 在两个可选项都缺省时强制返回空串（[client_handler_config_helper.h:24-37](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/client_handler_config_helper.h#L24-L37)）。

#### 4.4.3 源码精读

[client_handler_factory.cc:18-33](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/client_handler_factory.cc#L18-L33)：工厂分派全貌。`handler_type == DIRECT` 时创建 `DirectClientHandler`，否则创建 `UbClientHandler`；任一 Create 失败记 ERROR 日志并返回 nullptr，由 `HixlClient::Initialize` 的 `HIXL_CHECK_NOTNULL` 兜底报错。没有第三个分支——handler 种类与链路种类的多对多关系被压成了二选一。

[client_handler_factory.h:25-47](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/client_handler_factory.h#L25-L47)：`HandlerCreateArgs` 参数包。注意 `HandlerType` 枚举只有 DIRECT/UB 两个值；`matched_pairs` 是 `EndpointPair{local, remote, type}` 的数组；`ctrl_socket` 被一并传给 handler——UB handler 正是用它拉取对端内存信息。

[endpoint_matcher.cc:35-63](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/endpoint_matcher.cc#L35-L63)：两张静态优先级规则表。同实例表第一条是 GROUP+UB（「same-instance prefers ub group」），后面全是 DIRECT 的兜底链（hccs → uboe → ub_rtp → roce/device → roce/host）；跨实例表没有 UB 组，全部是 DIRECT。**这就是 handler 选择的全部判断条件：匹配到 UB 分组 ⇒ UB handler；匹配到任何单一协议 ⇒ DIRECT handler。**

[endpoint_matcher.cc:230-279](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/endpoint_matcher.cc#L230-L279)：`TryMatchByPriority`。先处理特例（同实例、两端 ub_ctp 全 device 时尝试「仅 D2D 的 UB 分组」规则，命中时同样是 UB handler 但只有一对链路），再按 cross_instance 与否选表逐条尝试；命中即写 `handler_type = rule.handler_type` 并打 EVENT 日志（含 reason 字段，排查时非常有用）；全部失败返回 `PARAM_INVALID`——这就是「endpoint 匹配失败」的错误码。

[endpoint_matcher.cc:189-213](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/endpoint_matcher.cc#L189-L213)：`TryMatchGroup`，UB 分组的配对算法。把远端 UB endpoint 建成 `{dst_eid, plane, placement}` 的匹配表；本地 endpoint 依次尝试与 device/host 两种远端 placement 配对，用 `expected` 去重保证每种 CommType 只产生一对，最多收集 `kMaxUbCsClientNum` 对。

[hixl_client.cc:67-91](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_client.cc#L67-L91)：`HixlClient::Initialize` 的收尾三步：MatchEndpoints → 打 EVENT 日志（含 `handler:%s` 和每对链路的 comm_type）→ 组装 HandlerCreateArgs 并调工厂。第 84-87 行的聚合初始化列表按 `HandlerCreateArgs` 字段声明顺序填参，阅读时可对照 4.4.3 第二条链接的字段表。

#### 4.4.4 代码实践

**实践目标**：梳理一次 `TransferAsync` 从 `Hixl::TransferAsync` 到 CS 层 C 接口的完整分派路径，画出类图，并总结 handler 选择的判断条件。

**操作步骤**：

1. 自顶向下读五个调用点，每处记一行笔记：
   - `src/hixl/engine/hixl_impl.cc` 中 `HixlImpl::TransferAsync`（门卫检查 + 引擎分派，u2-l5 已读）；
   - `src/hixl/engine/hixl_engine.cc` 中 `HixlEngine::TransferAsync`（auto_connect 时先 AutoConnect）；
   - [hixl_client.cc:185-197](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_client.cc#L185-L197)（校验连接状态 + 转发 + req_map_ 登记）；
   - [direct_client_handler.cc:78-100](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/direct_client_handler.cc#L78-L100) 与 [ub_client_handler.cc:336-377](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/ub_client_handler.cc#L336-L377)（两条分叉）；
   - 最终的 `HixlCSClientBatchPutAsync/HixlCSClientBatchGetAsync`。
2. 画类图（mermaid 或手绘均可），核心关系：

   ```text
   HixlEngine ──持有──> ClientManager ──per remote_engine──> HixlClient
   HixlClient ──unique_ptr──> IClientHandler <──继承── DirectClientHandler
                                                  └──继承── UbClientHandler
   DirectClientHandler ──1 个 handle──> CS 层 (HixlCSClient*)
   UbClientHandler     ──map<CommType, handle>──> CS 层 (HixlCSClient*)
   ClientHandlerFactory ..创建.. IClientHandler（输入 HandlerCreateArgs）
   EndpointMatcher ..产出 handler_type + matched_pairs..> HandlerCreateArgs
   ```

3. 用一句话写出 handler 选择条件，再对照 [endpoint_matcher.cc:46-59](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/endpoint_matcher.cc#L46-L59) 校对。

**需要观察的现象**：路径上每层各加了一道什么检查（空参、连接状态、dump_guard、地址注册校验、分桶），形成层层收窄的漏斗。

**预期结果**：一条完整的时序笔记，形如：

`Hixl::TransferAsync → HixlImpl（门卫）→ HixlEngine（auto_connect 探活）→ HixlClient（状态校验 + dump_guard）→ IClientHandler::TransferAsync → [Direct: 直接转 C 接口 | UB: ClassifyTransfers 分桶 → (lazy 补链路) → 逐桶转 C 接口] → 返回 TransferReq → HixlClient req_map_ 登记`。

handler 选择条件的一句话版本：**同实例且两端能配出 UB 分组（或「全 device 的 ub_ctp」特例）时选 UB handler，其余一切命中单一协议规则（hccs/uboe/ub_rtp/roce）或跨实例的场景都选 DIRECT handler。**

#### 4.4.5 小练习与答案

**练习 1**：为什么跨实例规则表里没有 UB 分组规则？

**答案**：UB（ub_ctp 协议）分组依赖同一网络实例内的 UnifiedBus 内存语义（通过 dst_eid/plane 匹配多对 device/host endpoint）。跨实例（`net_instance_id` 不同）意味着两端不在同一通信实例内，UB 语义不可用，只能走 uboe/ub_rtp/roce 这类网络协议的单链路，因此全部是 DIRECT 规则（[endpoint_matcher.cc:35-44](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/endpoint_matcher.cc#L35-L44)）。具体协议能力边界以芯片形态为准（待确认）。

**练习 2**：如果把 `ClientHandlerFactory::Create` 改成在 UB 创建失败时自动降级为 DIRECT，会有什么问题？

**答案**：matched_pairs 的形态与 handler 类型是绑定的：UB 分组会产出多对、且传输时依赖 `ClassifyTransfers` 按内存分类选路；DirectClientHandler 只消费 `matched_pairs[0]`，静默降级会丢弃其余链路对，且后续按地址分类的传输会被发到唯一 handle 上，行为不再正确。所以工厂失败只能整体失败（返回 nullptr → Initialize 报错），这也解释了源码里「失败即终止」的设计。

**练习 3**：`HandlerCreateArgs.ctrl_socket` 为什么必须传进 handler？

**答案**：`UbClientHandler::Create` 需要复用这条已经建立的控制面 socket 向 server 发送 `kGetMemInfoReq` 拉取对端内存信息（[ub_client_handler.cc:194](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/ub_client_handler.cc#L194)）。这是一次性的创建期动作——对端内存在 server 侧建链导入后不再变化，所以 remote_segments_ 不需要后续刷新。

## 5. 综合实践

**任务：为「一次 UB 模式的混合内存批量传输」写出完整的分派说明书。**

设定场景：auto_connect 开启，client 端注册了一块 device 内存和一块 host 内存，server 端同样注册了这两类内存；endpoint 匹配结果为 UB handler、4 对链路（D2D/D2H/H2D/H2H 各一对）。用户直接调用 `TransferAsync`，下发 6 个 op：3 个 device→device、2 个 host→device、1 个 device→host。

请完成：

1. **建链时序**：说明这次调用之前用户没有显式 Connect，从 `HixlEngine::TransferAsync` 的 auto_connect 分支开始，追踪到 `UbClientHandler::EnsureLinksConnected`，写出哪些链路被建立、哪些没有（提示：分桶后只需要 UB_D2D 与 UB_H2D 两种类型；H2D/H2H 两对链路保持未连接）。
2. **分派路径**：按 4.4.4 的格式写出这次调用经过的每一层与每层的关键行为。
3. **状态聚合**：预测这次调用返回的 TransferReq 在 `complete_handles_` 里聚合了几个 BatchHandle；写出第 2 次、第 3 次 GetTransferStatus 可能的返回（考虑两个桶完成时刻不同）。
4. **失败注入**：假设其中一个 op 的远端地址超出对端注册区间，指出错误在哪一层、哪个函数被拦下、返回什么错误码。

参考要点（完成后自查）：

- 建链只补 `connected_types_` 缺失且本次需要的类型（[ub_client_handler.cc:225-242](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/ub_client_handler.cc#L225-L242)）；
- 2 个 BatchHandle 聚合进 1 个 req（[ub_client_handler.cc:371-375](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/ub_client_handler.cc#L371-L375)）；
- 部分完成时整体返回 WAITING 且不摘表，全部完成才返回 COMPLETED 并摘表（[ub_client_handler.cc:438-473](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/ub_client_handler.cc#L438-L473)）；
- 未注册地址在 `ClassifyTransfers` 被 PARAM_INVALID 拦下（[ub_client_handler.cc:579-585](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/ub_client_handler.cc#L579-L585)），此时尚未触碰任何 CS 接口。

## 6. 本讲小结

- `IClientHandler`（[client_handler.h:74-86](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/client_handler.h#L74-L86)）是 `HixlClient` 与 CS 层之间的策略抽象，接口面与 `Hixl` 传输类公开 API 对应，通知除外（走控制面）。
- `DirectClientHandler` 只消费 `matched_pairs[0]`，一对一封装 CS C 接口，适用于 HCCS/RoCE/UBOE/UB_RTP 等单协议链路；一个 TransferReq 对应一个 CompleteHandle。
- `UbClientHandler` 持有 `map<CommType, handle>`，用 `ClassifyTransfers` 按「本地/远端内存类型」把一批 op 分桶到不同链路；一个 TransferReq 聚合多个 BatchHandle，状态查询呈「全完成才算完成」的聚合语义。
- UB handler 的懒惰建链是 auto_connect 选项的落点：`is_lazy` 来自引擎 `auto_connect_`（显式 Connect 时强制 false），首次 Connect 只记账，真正的建链推迟到首次传输且只建需要的类型。
- handler 类型由 `EndpointMatcher` 的静态优先级规则表决定：同实例优先 UB 分组，其余（含全部跨实例场景）走 DIRECT 单协议规则；全部规则落空返回 PARAM_INVALID。
- `ClientHandlerFactory::Create` 只做二选一分派；`ClientHandlerConfigHelper` 负责 qos/max_active_channels 可选项到 JSON 配置串的翻译，并特判空配置返回空串。

## 7. 下一步学习建议

- 下一讲 u3-l3 将逆流而上，讲清 matched_pairs 的原料从哪来：`EndpointGenerator`（local_comm_res、rootinfo builder 等生成方式）与 endpoint 的字段语义，与本讲的 `EndpointMatcher` 正好构成「生成 → 匹配」的完整链路。
- 想先看被动方视角的读者可以跳到 u4-l1/u4-l2：本讲反复出现的 `HixlCSClientCreate` 等 C 接口在 server 侧的对应物（MsgHandler/MsgReceiver）将在那里展开。
- 对并发基础设施感兴趣的同学建议预习 u3-l4：本讲 `ConnectHandles` 用到的 `ThreadPool` 与 `OptionalAclrtContext` 会在那一讲系统讲解。
- 建议动手实验：在 `tests/cpp/hixl/engine/hixl_client_unittest.cc` 与 `hixl_engine_unittest.cc` 中搜索 ClientHandler 相关断言，观察单测如何桩替 CS 层来隔离验证 handler 的分派逻辑。

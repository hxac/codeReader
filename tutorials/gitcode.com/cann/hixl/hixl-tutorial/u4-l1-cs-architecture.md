# u4-l1 CS 通信服务总体架构

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 CS（Communication Server/Service）模块在 HIXL Engine 中的位置：它是引擎数据面的最底层实现，向上被 `HixlServer`/`DirectClientHandler` 封装，向下通过 proxy 层调用 Hcomm 接口族。
2. 掌握 `include/cs/hixl_cs.h` 这组 `extern "C"` 接口的完整能力范围：server 5 个接口 + client 10 个接口，以及句柄、错误码、描述结构体的约定。
3. 理解 CS server 与 CS client 的分工：server 是被动方（监听、登记、导出内存），client 是唯一主动方（建链、导入内存、发起传输）。
4. 画出一次完整交互中「建链消息」与「内存注册消息」在 TCP 控制面上的流向。

本讲是单元四的第一课，后续三讲（消息处理器、Channel/TransferPool、全局配置）都会在本讲建立的整体图景上展开。

## 2. 前置知识

阅读本讲前，你需要先理解以下几个在前面讲义中已建立的概念，这里做简要回顾：

- **控制面与数据面分离**（u1-l3、u3-l3）：HIXL 用一条 TCP socket 交换「端点信息、内存描述、建链请求」等控制消息，真正的数据搬运则走 HCCS/RDMA 等硬件链路。CS 模块同时实现了这两面：控制面是本讲的 `Listen`/`Connect`/消息收发，数据面是 `Endpoint`/`Channel`/传输接口。
- **EndpointDesc 与协议**（u3-l3）：端点是「一条可用链路的本地端资源描述」，含 protocol（hccs/roce/uboe/ub_ctp 等）、commAddr、loc（host/device 位置）等字段。CS 层直接消费 `hcomm_res_defs.h` 中的 `EndpointDesc`，这是 `hixl_cs.h` 唯一 include 的项目外头文件。
- **Engine 抽象**（u3-l1）：`HixlEngine` 内部按值持有 `HixlServer`（被动方），后者封装的就是本讲的 `HixlCSServer`；client 侧 `DirectClientHandler` 一对一封装 `HixlCSClient` 的 C 接口（u3-l2）。
- **注册内存**（u2-l3）：把「地址+长度」登记为对端可直接单边访问的授权。CS 层的注册粒度是 `Endpoint`：同一块内存要在**每一个**匹配的端点上各注册一次。
- **MemHandle / CompleteHandle**：均为 `void*` 不透明句柄，用户只持有、用于后续反查，绝不能解引用。

还有一个新术语需要提前解释——**epoll**：Linux 提供的 I/O 多路复用机制，让一个线程同时监视多个文件描述符（socket），谁的缓冲区有数据就处理谁。CS server 的监听线程就是用一个 `epoll_wait` 循环同时服务所有已接入的 client。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/cs/hixl_cs.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/cs/hixl_cs.h) | CS 公开 C 接口：全部函数声明、句柄/错误码/描述结构体定义 |
| [src/hixl/cs/hixl_cs.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs.cc) | C 接口的实现层：参数检查、句柄与 C++ 对象互转、转发到 `HixlCSServer`/`HixlCSClient` |
| [src/hixl/cs/hixl_cs_server.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_server.cc) | server 实现：端点创建、内存登记、TCP 监听与 epoll 事件循环、四类控制消息处理 |
| [src/hixl/cs/hixl_cs_server.h](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_server.h) | `HixlCSServer` 类定义与成员布局（clients/reg_mems/channels 三张表） |
| [src/hixl/cs/hixl_cs_client.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_client.cc) | client 实现：建链握手、远端内存导入、host/device 两条传输路径、完成状态查询 |
| [src/hixl/common/ctrl_msg.h](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/ctrl_msg.h) | 控制面消息类型枚举（`CtrlMsgType`）与请求/应答结构体 |

## 4. 核心概念与源码讲解

### 4.1 HixlCs：extern "C" 接口层

#### 4.1.1 概念说明

`hixl_cs.h` 是 CS 模块唯一对外的头文件。与 `hixl::Hixl` 的 C++ 类接口（u2 单元）不同，它是一组纯 C 风格接口：`extern "C"` 函数 + `void*` 句柄 + 裸 `uint32_t` 错误码。这样做有两个动机：

1. **隔离 ABI**：CS 层位于引擎最底层，接口稳定后用 C ABI 可以避免 C++ 名称修饰和 STL 类型跨边界传递的问题。
2. **角色清晰**：它把「一次单边传输」拆成两个显式角色——server（被动提供内存的一方）和 client（主动发起传输的一方），比 `Hixl` 类的「双角色合一」更贴近底层模型。事实上 `Hixl` 类的 `Initialize` 带 port 时就在内部走 server 路径，不带 port 走 client 路径（u2-l1），而这两条路径最终都落到本讲的 C 接口上。

#### 4.1.2 核心流程

C 接口层本身没有状态，每个函数都遵循同一模式：

```text
入参判空 → handle 静态转换回 C++ 对象 → 调用对象方法 → 错误码透传
```

server 侧 5 个接口的推荐调用序列：

```text
HixlCSServerCreate(desc, config, &handle)   // 创建并初始化
  → HixlCSServerRegMem(...)                  // 可选，注册供远端读写的内存
  → HixlCSServerListen(handle, backlog)      // 启动 TCP 监听线程
  → ... 被动等待 client 接入 ...
  → HixlCSServerUnregMem(...)                // 可选
  → HixlCSServerDestroy(handle)              // 收线程、关 fd、释放端点
```

client 侧 10 个接口的推荐调用序列：

```text
HixlCSClientCreate(desc, config, &handle)
  → HixlCSClientConnect(handle, timeout_ms)        // TCP + 三步握手（见 4.3.2）
  → HixlCSClientGetRemoteMem(...)                  // 拉取并导入 server 注册的内存
  → HixlCSClientRegMem(...)                        // 可选，注册本地内存（WRITE 场景）
  → BatchPutAsync / BatchGetAsync / BatchPutSync / BatchGetSync
  → HixlCSClientQueryCompleteStatus(...)           // 异步任务轮询至终态
  → HixlCSClientUnregMem(...)
  → HixlCSClientDestroy(handle)
```

#### 4.1.3 源码精读

**句柄与错误码约定**。四个句柄类型全部是 `void*`；错误码只有三个，比 `hixl::` 命名空间下的 9 个精简得多（103901 段的 NOT_CONNECTED 等由引擎层自行处理，不会穿透到 C 接口）：

[include/cs/hixl_cs.h:L18-L30](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/cs/hixl_cs.h#L18-L30) —— `extern "C"` 开始、`HixlServerHandle/HixlClientHandle/CompleteHandle/MemHandle` 四个 `void*` 句柄、`HIXL_SUCCESS/HIXL_PARAM_INVALID/HIXL_TIMEOUT/HIXL_FAILED` 四个错误码常量（数值与 `hixl::` 命名空间一致）。

**描述结构体**。server 与 client 的「名片」：

[include/cs/hixl_cs.h:L42-L58](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/cs/hixl_cs.h#L42-L58) —— `HixlClientDesc` 携带 local/remote 两个 `EndpointDesc` 指针与 server 的 ip:port（另有 tc/sl 两个 RDMA 服务质量字段）；`HixlServerDesc` 携带端点列表 + 监听 ip:port。注意两者的 `reserved` 字段把结构体补齐到 128 字节，这是 ABI 预留，不要读写（与 u2-l2 的规则一致）。

**传输描述与完成状态**：

[include/cs/hixl_cs.h:L60-L71](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/cs/hixl_cs.h#L60-L71) —— `HixlOneSideOpDesc` 是最小三元组 `remote_buf/local_buf/len`（对比 `hixl::TransferOpDesc` 的 `local_addr/remote_addr/len`，字段顺序相反，因为这里固定以 client 为第一视角）；`HixlCompleteStatus` 四态与 `hixl::TransferStatus` 对应。

**C 实现层的典型转发**。以 server 创建为例：

[src/hixl/cs/hixl_cs.cc:L23-L47](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs.cc#L23-L47) —— `HixlCSServerCreate` 做四件事：① 出参句柄置空 + 入参判空；② 校验 `server_port <= 65535`；③ 用 `GlobalConfig::Parse(..., kServer)` 解析配置串；④ `new HixlCSServer` 后调用 `Initialize`，失败则由 `HIXL_DISMISSABLE_GUARD` 回滚 `delete server`，成功才把指针写回 `*server_handle`。这套「scope guard 保证不留半初始化对象」的写法与 `HixlImpl::Initialize`（u2-l1）如出一辙。

[src/hixl/cs/hixl_cs.cc:L126-L148](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs.cc#L126-L148) —— `HixlCSClientBatchPutAsync` 展示了 client 传输接口的公共套路：`list_num == 0` 直接拒绝对（`HIXL_PARAM_INVALID`）；转换句柄后调 `client->BatchTransferAsync(false /* is_get */, ...)`——注意 `is_get` 布尔参数：`false` 是 Put（WRITE），`true` 是 Get（READ），四个传输 C 接口最终汇聚到同两个 C++ 方法。

另外还有一个不在 C 头文件里的 C++ 扩展接口：

[src/hixl/cs/hixl_cs.cc:L247-L255](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs.cc#L247-L255) —— `hixl::HixlCSServerRegProc` 允许引擎内部（如 notify 消息）向 server 注册自定义 `CtrlMsgType` 处理器，这是引擎层扩展 CS 控制面的钩子。

#### 4.1.4 代码实践

**实践目标**：不动手跑程序，纯靠「读头文件 + 交叉验证」掌握 C 接口全景，为后面画交互图做准备。

**操作步骤**：

1. 打开 [include/cs/hixl_cs.h](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/cs/hixl_cs.h)，把 15 个函数（server 5 + client 10）抄成一张表，标注每个函数：属于哪个角色、是同步还是异步、有没有出参句柄。
2. 用 grep 验证「引擎内谁在调用这些 C 接口」：

   ```bash
   grep -rn "HixlCSClientBatchGetAsync\|HixlCSServerCreate" src/hixl/engine/ --include="*.cc"
   ```

3. 对照 u3-l2 讲过的 `DirectClientHandler`，确认 `Put/Get` 与 `HixlCSClientBatchPutAsync/BatchGetAsync` 的对应关系。

**需要观察的现象**：grep 结果应显示 `DirectClientHandler` 的 `Put`/`Get` 实现里出现这些 C 函数；`HixlCSServerCreate` 则出现在 `HixlServer`（`src/hixl/engine/hixl_server.cc`）的初始化路径中。

**预期结果**：得到一张「C 接口 → 引擎调用者」映射表，其中 `HixlCSClientQueryCompleteStatus` 对应 `DirectClientHandler` 的状态查询，`HixlCSServerRegProc` 被 notify 相关处理器注册使用。若某条映射在源码中找不到调用者，标注「待本地验证」而不是猜测。

#### 4.1.5 小练习与答案

**练习 1**：`HixlCSClientConnect` 的注释说它是「同步建链入口」，这与 `hixl::Hixl::ConnectAsync`（u2-l4）矛盾吗？

**答案**：不矛盾。分层不同：CS 层的 Connect 本身是阻塞的（TCP 连接 + 三步握手做完才返回）；`Hixl::ConnectAsync` 的「异步」是引擎层用 `ConnectPoolExecutor` 线程池（u3-l4）把这次阻塞调用搬到后台线程执行实现的。异步是上层特性，底层调用始终同步。

**练习 2**：为什么 `HixlOneSideOpDesc` 里没有内存类型（MemType）字段，而 `CommMem` 里有？

**答案**：内存类型在**注册时**就已经随 `CommMem`（type/addr/size）登记到端点和 `HixlMemStore` 台账里了；下发传输时，client 用 `ValidateAddress` 按地址反查台账即可校验合法性并推断 host/device 路径（见 4.3.3）。传输描述只负责描述「从哪搬到哪、搬多少」。

**练习 3**：`HixlCSServerCreate` 失败时为什么用户不需要调用 `HixlCSServerDestroy`？

**答案**：因为 [hixl_cs.cc:L39-L43](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs.cc#L39-L43) 中 `HIXL_DISMISSABLE_GUARD(rollback, ...)` 在 `Initialize` 失败路径上自动 `delete server`，且 `*server_handle` 保持为函数开头设置的 `nullptr`——创建失败不产出任何需要用户管理的资源。

### 4.2 HixlCSServer：被动方的全貌

#### 4.2.1 概念说明

`HixlCSServer` 是 CS 架构中的被动方。回顾 u1-l3 的结论——单边通信中被动方工作量近乎为零——在 CS 层这句话的具体含义是：

- server **不发起**任何传输。它只做三件事：创建端点并登记内存、监听 TCP 等待 client 接入、响应 client 发来的控制消息。
- 数据面动作（如让本端内存可被远端 DMA）在**注册端点/内存时**就完成了；之后即使 server 进程完全空闲，client 也能持续读写它注册过的内存。

但它绝非「零成本」：server 要维护三张表（成员见 [hixl_cs_server.h:L72-L96](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_server.h#L72-L96)）：

| 成员 | 内容 | 作用 |
| --- | --- | --- |
| `clients_` | fd → `MsgReceiver` | 每个已接入 client 的消息接收器 |
| `reg_mems_` | MemHandle → `vector<EndpointMemInfo>` | 一次注册在多个端点上的登记记录 |
| `channels_` | fd → `EndpointChannelInfo` | 每条控制连接对应的数据面 channel |

#### 4.2.2 核心流程

server 的生命周期与事件循环：

```text
Initialize:
  逐个创建 Endpoint（endpoint_store_.CreateEndpoint）
  注册 4 类控制消息处理器:
    kMatchEndpointReq   → MatchEndpointMsg   （匹配端点）
    kCreateChannelReq   → CreateChannel      （建数据面 channel）
    kGetRemoteMemReq    → ExportMem          （导出内存描述）
    kDestroyChannelReq  → DestroyChannel     （拆 channel）
  初始化传输完成标志（host/device 各一块 8 字节内存）

Listen:
  TCP bind+listen(ip_, port_) → listen_fd 加入 epoll
  启动 listener 线程循环 DoWait()

DoWait（单线程事件循环）:
  epoll_wait(100ms 超时)
  ├─ fd == listen_fd → Accept 新 client，建 MsgReceiver，加入 clients_
  ├─ 事件含 ERR/HUP/RDHUP → CleanupClient(fd)：发 kDestroyChannelReq、摘 epoll、关 fd
  └─ 事件含 EPOLLIN → ProClientMsg：receiver->IRecv 收完整消息 → msg_handler_ 分发
```

「传输完成标志」（trans finished flag）是理解 server 被动性的关键设计：server 在初始化时把一块初值为 1 的 8 字节内存按固定 tag（`_hixl_builtin_host_trans_flag` / `_hixl_builtin_dev_trans_flag`）注册到端点上。client 完成一批传输后，从远端**再单边读一次**这块 flag，读到 1 即知链路可用、传输已落地——完成感知本身也是单边操作，server 全程不知情。

#### 4.2.3 源码精读

**初始化与消息处理器注册**：

[src/hixl/cs/hixl_cs_server.cc:L211-L242](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_server.cc#L211-L242) —— `Initialize` 遍历端点列表创建端点，随后用 lambda 把四种 `CtrlMsgType` 逐一绑定到成员函数。这四类消息就是 client 建链握手（4.3.2）会发出的全部请求。`AreUbCtpEndpointsAllDevice` 等辅助函数决定端点是否需要 host VA 映射（u3-l3 讲过的 UB 场景）。

**传输完成标志的注册**：

[src/hixl/cs/hixl_cs_server.cc:L114-L141](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_server.cc#L114-L141) —— `InitTransFinishedFlag` 把端点按 locType 分成 host/device 两组分别处理；[L143-L168](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_server.cc#L143-L168) 中 host 侧用 `posix_memalign` 按页对齐分配（RDMA 锁页内存要求）、写入初值 1、注册到每个 host 端点；device 侧（[L170-L209](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_server.cc#L170-L209)）则初始化 server 自己的 `TransferPool` 并用 `aclrtMalloc` 分配 device 内存。这也解释了为什么 server 虽不传输、却也需要 TransferPool——完成标志与 notify 地址解析都依赖它。

**多端点注册同一块内存**：

[src/hixl/cs/hixl_cs_server.cc:L318-L356](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_server.cc#L318-L356) —— `RegisterMem` 是 server 侧内存注册的核心：遍历**所有**端点，跳过不匹配该内存类型的端点（`ShouldRegisterEndpointForMem`，如 UB 端点上 host 内存只注册到 host 位置端点），在每个匹配端点上各调一次 `endpoint->RegisterMem`，把全部 `EndpointMemInfo` 存入 `reg_mems_`，返回给用户的句柄取第一个端点的句柄（仅作索引键用）。`DeregisterMem`（[L358-L374](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_server.cc#L358-L374)）按台账反向逐一注销。这正是 u2-l3 说过「server 区间来自建链导入、幂等」的底层原因。

**端点匹配请求的处理**：

[src/hixl/cs/hixl_cs_server.cc:L398-L440](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_server.cc#L398-L440) —— `MatchEndpointMsg`：client 送来目标 `EndpointDesc`，server 用 `endpoint_store_.MatchEndpoint` 找到本地匹配端点，查询其监听端口（经 `HcommProxy::EndpointGetListenPort`，即 u3-l5 讲过的弱符号 proxy），分配一个进程级递增的 `channel_index`（`g_next_server_channel_index`，保证两端能拼出一致的 channel 名），打包成 `MatchEndpointResp` 回发。

**建 channel 请求的处理**：

[src/hixl/cs/hixl_cs_server.cc:L442-L480](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_server.cc#L442-L480) —— `CreateChannel`：根据请求中的 qos/tc/sl/重试参数构造 `ChannelDesc`（`channel_type = kServer`），在匹配到的端点上创建数据面 channel，并把 `fd → channel` 记入 `channels_`。对比 4.3.3 将看到的 client 侧：client 用 `ChannelType::kClient` 在**本端**端点上也建一个 channel——两端各建一半，拼成一条完整链路。

**监听与事件循环**：

[src/hixl/cs/hixl_cs_server.cc:L552-L567](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_server.cc#L552-L567) —— `Listen`：`CtrlMsgPlugin::Listen` 完成 bind/listen，listen_fd 加入 epoll 后启动 `listener_` 线程循环调 `DoWait`。

[src/hixl/cs/hixl_cs_server.cc:L609-L646](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_server.cc#L609-L646) —— `DoWait`：单次 `epoll_wait` 最多取 128 个事件（`kMaxEventsNum`）；新连接走 Accept + `SetTcpNoDelay/KeepAlive` + 建 `MsgReceiver`；断连事件（EPOLLERR/HUP/RDHUP）触发 `CleanupClient`，它会伪造一条 `kDestroyChannelReq` 消息交给 `msg_handler_`，复用正常拆链逻辑清理该 client 的 channel。

#### 4.2.4 代码实践

**实践目标**：通过日志观察一个真实 server 的初始化与事件循环行为。

**操作步骤**：

1. 按 u1-l2 构建样例（`bash build.sh --examples`），按 u1-l3 的 quickstart 流程在 device 0 上启动 server 角色：

   ```bash
   ./build_out/examples/hixl_example_quickstart --role server --local_engine 192.168.x.x:26000
   ```

2. 把日志级别调到 INFO（HIXL 日志默认输出 INFO 级事件），观察以下两类日志行是否出现（这些字符串来自上述源码的 `HIXL_EVENT/HIXL_LOGI`）：
   - `[HixlServer] init success, endpoint_list_num:...`
   - `[HixlServer] start to listen on <ip>:<port>`
   - `[HixlServer] accept socket success, client fd:...`（client 接入后）
3. 再启动 client，观察 server 侧依次出现 `SendMatchEndpointResp success`、`CreateChannel success` 日志。

**需要观察的现象**：server 启动即打印 init/listen 日志并阻塞等待；client 接入后才出现 accept、MatchEndpoint、CreateChannel 三条日志——印证「被动方只在被请求时工作」。

**预期结果**：日志顺序为 init → listen →（client 接入）→ accept → match endpoint → create channel。若无昇腾硬件，此实践无法运行，改为「待本地验证」，退路是纯阅读 `Initialize`（L211-L242）与 `DoWait`（L609-L646）并在注释里标注每个日志点的触发条件。

#### 4.2.5 小练习与答案

**练习 1**：server 的 listener 线程模型是「每连接一线程」还是「单线程 epoll」？依据是什么？

**答案**：单线程 epoll。`Listen` 只启动一个 `listener_` 线程（[L560-L565](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_server.cc#L560-L565)），`DoWait` 用 `epoll_wait` 一次最多处理 128 个就绪事件（`kMaxEventsNum=128`，注释明确写「减少 epoll 系统调用」）。控制面消息量小（只在建链/注册时交互），单线程足够且免锁竞争。

**练习 2**：`CleanupClient` 为什么要伪造一条 `kDestroyChannelReq` 消息而不是直接删 `channels_[fd]`？

**答案**：因为删表之外还必须在**端点上**销毁数据面 channel（`endpoint->DestroyChannel`），否则端点内部资源泄漏。伪造消息走 `msg_handler_` 分发，复用 `DestroyChannel` 处理器里「查表 → 端点销毁 → 擦表」的完整逻辑，避免两处维护同一套清理代码。

**练习 3**：server 端点上有 host 和 device 两种端点时，`RegisterMem` 注册一块 host 内存，两个端点都会登记吗？

**答案**：不一定。`ShouldRegisterEndpointForMem`（[L85-L97](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_server.cc#L85-L97)）规定：非 UB 端点一律登记；UB 端点上 host 内存只登记到 host 位置端点（或需要 host VA 映射的端点），device 内存只登记到 device 位置端点。若最终一个端点都没匹配上，注册失败返回 PARAM_INVALID（[L348-L349](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_server.cc#L348-L349)）。

### 4.3 HixlCSClient：主动方的建链与传输

#### 4.3.1 概念说明

`HixlCSClient` 是唯一主动方，CS 层所有「发起」类动作都在这里：发起 TCP 连接、发起三步握手、拉取远端内存、下发读写。它内部维护：

- `local_endpoint_`：本端端点对象（数据面资源的持有者）；
- `mem_store_`（`HixlMemStore`，u2-l3 已讲）：本端注册内存 + 远端导入内存的合并台账，传输前校验地址；
- `client_channel_handle_`：握手成功后唯一的数据面 channel 句柄——注意 CS client 与引擎层 `ClientManager`（u2-l4）不同，**一个 CS client 实例只对应一条链路、一个远端**，多远端要建多个实例。

#### 4.3.2 核心流程

`Connect` 内部的「三步握手」是本讲最重要的流程（这条链路在引擎层被 `HixlClient::Initialize` 触发，u2-l4 讲过控制面 socket 建立于 Initialize 阶段，底层正是这里）：

```text
client                                server（epoll 循环）
  │ CtrlMsgPlugin::Connect(ip, port)     │
  │ ────── TCP 三次握手 ──────────────►   │ accept，建 MsgReceiver
  │                                      │
  │ ① kMatchEndpointReq (目标端点描述)    │
  │ ─────────────────────────────────►   │ MatchEndpointMsg:
  │                                      │   端点匹配 + 查监听端口
  │ ① kMatchEndpointResp                 │   + 分配 channel_index
  │ ◄─────────────────────────────────   │
  │                                      │
  │ ② kGetRemoteMemReq (远端句柄)         │
  │ ─────────────────────────────────►   │ ExportMem:
  │ ② kGetRemoteMemResp (内存描述列表)    │   导出全部注册内存
  │ ◄─────────────────────────────────   │
  │ [本地] MemImport 逐条导入远端内存      │
  │                                      │
  │ ③ kCreateChannelReq (本端点+对端点    │
  │    +qos/tc/sl/重试参数)               │
  │ ─────────────────────────────────►   │ CreateChannel:
  │                                      │   server 侧建 channel(kServer)
  │ [本地] Endpoint::CreateChannel        │
  │        (kClient) —— 两端并发建        │
  │ ③ kCreateChannelResp                  │
  │ ◄─────────────────────────────────   │
  │ Connect 完成，channel_handle 就绪     │
```

传输路径按本端端点位置二选一（`BatchTransferAsync/Sync` 的分派逻辑）：

```text
IsDeviceEndpoint(local)?
 ├─ 是 → BatchTransferDeviceAsync/Sync:
 │        从 TransferPool 取 slot → desc 拷入 device 内存
 │        → 分块(每块≤128个desc)下发 HixlBatchGet/HixlBatchPut 内核
 │        → kernel 完成后写 notify flag → host 轮询 flag 得知完成
 └─ 否(host) → BatchTransferHostAsync/Sync:
          HcommProxy::ReadNbiOnThread/WriteNbiOnThread 逐条下发
          → 末尾追加一次对远端 trans_flag 的 8 字节 READ
          → host 轮询本地 flag_queue 得知完成
```

注意：client 端点在 host 位置时传输由**本进程线程**发起（`*OnThread` 系列）；在 device 位置时传输由**device 上的 kernel**发起（`HixlBatchGet/HixlBatchPut`，即 u4-l4 将讲的 `load_kernel` 加载的内置算子）。两条路径的「完成感知」都复用了 server 注册的 trans_flag——这就是 4.2.1 所说完成感知也是单边操作的具体落点。

#### 4.3.3 源码精读

**Create：三段式初始化**：

[src/hixl/cs/hixl_cs_client.cc:L337-L377](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_client.cc#L337-L377) —— `Create` 依次执行：`GlobalConfig::Parse(kClient)` 解析配置 → 构造 `Endpoint`（同时传入 local/remote 描述）→ `InitDeviceResource`（device 端点时初始化 TransferPool）→ `InitBaseClient`（初始化本端端点；host 端点时额外调用 `InitFlagQueue` 注册 host 完成标志队列，见 [L247-L276](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_client.cc#L247-L276)）→ `InitRdmaRetryConfig`（读 `HCCL_RDMA_RETRY_CNT`/`HCCL_RDMA_TIMEOUT` 环境变量）→ `InitNotifyResources`。`pool_rollback` guard 保证任一步失败都回滚 TransferPool。

**Connect 与三步握手**：

[src/hixl/cs/hixl_cs_client.cc:L1177-L1194](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_client.cc#L1177-L1194) —— `Connect` 先 TCP 连 server，再调 `ExchangeEndpointAndCreateChannel` 完成全部协议交互。

[src/hixl/cs/hixl_cs_client.cc:L1196-L1242](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_client.cc#L1196-L1242) —— `ExchangeEndpointAndCreateChannel` 是握手的精确实现，与上面流程图一一对应：`SendMatchEndpointRequest` → `RecvMatchEndpointResponse`（拿到 `remote_endpoint_handle_` 和 `channel_index`）→ **在 CreateChannel 之前先预取远端内存**（`GetRemoteMemImpl`，注释标明 prefetch，让建链一次往返就把内存导入做完）→ `SendCreateChannelRequest` 的同时**在本端并发**调 `local_endpoint_->CreateChannel(kClient)`（两端同时建各自的半条 channel，缩短建链墙钟时间）→ `RecvCreateChannelResponse` 确认 server 侧成功。

**内存注册与地址校验**：

[src/hixl/cs/hixl_cs_client.cc:L380-L416](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_client.cc#L380-L416) —— `RegMemLocked`：先 `CheckMemoryForRegister` 拒绝与已记录内存重叠的注册；调 `local_endpoint_->RegisterMem`；host 内存且端点需要 VA 映射时，经 `HostRegisterProxy` 取得设备侧映射地址（`register_dev_addr`，即 u2-l3 讲过的主机地址→设备地址替换）；最后 `RecordMemory(false, ...)` 记入台账。传输前的地址合法性校验在 `ValidateAddress`（[L446-L450](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_client.cc#L446-L450)）调用 `mem_store_.BatchValidateMemoryAccess` 完成——CS 层的「未注册地址不得传输」安全闸就在这里。

**远端内存拉取与导入**：

[src/hixl/cs/hixl_cs_client.cc:L1278-L1296](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_client.cc#L1278-L1296) —— `GetRemoteMemImpl`：发 `kGetRemoteMemReq` → 收 `GetRemoteMemResp`（JSON 序列化的内存描述列表，server 侧对应 `ExportMem`）→ `ImportRemoteMem` 逐条 `MemImport` 把远端内存映射进本端地址空间，并 `RecordMemory(true, ...)` 记为远端内存。这就是 u2-l3「server 区间来自建链导入」的 client 侧另一半。

**host 路径传输与完成感知**：

[src/hixl/cs/hixl_cs_client.cc:L495-L507](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_client.cc#L495-L507) —— `BatchTransferTask`：按方向确定 dst/src（Get 时本端是 dst），逐条调 `TransferWithRetry`（内部是 `HcommProxy::ReadNbiOnThread/WriteNbiOnThread`，遇 `HCCL_E_AGAIN` 先 Fence 再重试，20 分钟总超时），最后统一 `ChannelFenceOnThread` 落栅栏。

[src/hixl/cs/hixl_cs_client.cc:L508-L553](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_client.cc#L508-L553) —— `BatchTransferHostAsync` 尾部：从 flag 队列取一个空闲槽位，对远端 trans_flag 发起 8 字节 READ——本地 flag 变 1 即代表本批（含这次 flag 读取）全部完成。`CheckStatusHost`（[L1061-L1083](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_client.cc#L1061-L1083)）轮询该 flag，读到 1 就回收槽位并报告 COMPLETED。

**device 路径传输**：

[src/hixl/cs/hixl_cs_client.cc:L852-L906](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_client.cc#L852-L906) —— `BatchTransferDeviceAsync`：取共享 slot → 分配 host_flag 与 device 侧 desc 缓冲 → `LaunchDeviceChunkedKernels` 分块（每块 ≤ `kMaxKernelBatchSize`=128 条）下发 `HixlBatchGet/HixlBatchPut` 内核 → 末尾 `aclrtMemcpyAsync` 把 device 常量 1 拷回 host_flag 作为完成信号。`CheckStatusDevice`（[L1085-L1121](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_client.cc#L1085-L1121)）除轮询 flag 外还检查 slot 的 err_flag 与 `transfer_failure_latched_`（传输失败闩锁：一旦 FAILED/TIMEOUT，后续所有新传输被拒绝，见 `BatchTransferAsync` 开头的闩锁检查：[L1025-L1031](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_client.cc#L1025-L1031)）。

**句柄魔术字分发**：

[src/hixl/cs/hixl_cs_client.cc:L1129-L1154](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_client.cc#L1129-L1154) —— `CheckStatusLocked` 读 handle 首 4 字节魔术字（`kDeviceCompleteMagic`/`kRoceCompleteMagic`）判断它是 device 路径还是 host 路径的完成句柄，再分派到对应检查函数。用户拿到的 `CompleteHandle` 是不透明的，但内部一定是这两种之一。

#### 4.3.4 代码实践

**实践目标**：跟踪一次 `HixlCSClientConnect` 的完整调用链，写出每一步发出的控制面消息与对应 server 处理器（本讲综合实践的第 2 部分）。

**操作步骤**：

1. 从 [src/hixl/cs/hixl_cs_client.cc:L1196](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_client.cc#L1196) 的 `ExchangeEndpointAndCreateChannel` 出发，逐个跳进 `ConnMsgHandler::SendMatchEndpointRequest` / `MemMsgHandler::SendGetRemoteMemRequest` / `ConnMsgHandler::SendCreateChannelRequest`（位于 `src/hixl/cs/conn_msg_handler.cc` 与 `mem_msg_handler.cc`，u4-l2 精读），确认每个 Send 所用的 `CtrlMsgType`。
2. 对照 [src/hixl/common/ctrl_msg.h:L29-L45](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/ctrl_msg.h#L29-L45) 的枚举值核对消息编号。
3. 在 server 侧 [hixl_cs_server.cc:L222-L236](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_server.cc#L222-L236) 找到每个 Req 的注册处理器，形成「消息 → server 处理函数」对照。

**需要观察的现象**：`ExchangeEndpointAndCreateChannel` 中三个 Send 调用的顺序是 MatchEndpoint → GetRemoteMem → CreateChannel，且 GetRemoteMem 在 CreateChannel **之前**（预取）。

**预期结果**：得到一张四列表格：消息类型（编号）| client 侧发送函数 | server 侧处理器 | 携带的关键信息。此实践为源码阅读型，无需硬件，可直接完成。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ExchangeEndpointAndCreateChannel` 要在 CreateChannel 之前先预取远端内存？

**答案**：把内存导入合并进建链的一次交互窗口。建链后用户通常立刻要传输，若等首次传输才拉取内存，需要额外的往返（且可能与传输争抢控制面 socket）。预取的代价是建链稍长，收益是建链返回后 `tag_mem_descs_` 已就绪——连 server 的 trans_flag 地址也一并导入（`EnsureDeviceRemoteFlagInited` 在 [L1292-L1294](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_client.cc#L1292-L1294) 被提前调用，注释明确写「避免传输时引入耗时」）。

**练习 2**：CS client 的 `BatchTransferHostAsync` 在任务末尾多发一次 8 字节 READ，读的是什么？为什么读它等于「确认完成」？

**答案**：读的是 server 初始化时注册的 trans_flag（初值 1）。host 路径的 `*OnThread` 读写不保证顺序可见性，但最后一次 READ 排在本批所有传输之后；当本地 flag 队列槽位读到 1，说明排在它前面的整批传输都已在链路上完成。这用一次单边读实现了完成确认，server 无需任何配合。

**练习 3**：`transfer_failure_latched_` 闩锁被置位后，调用 `HixlCSClientBatchPutAsync` 会发生什么？为什么要这样设计？

**答案**：直接返回 FAILED（`BatchTransferAsync` 入口的闩锁检查），不下发任何新任务。设计动机：device 路径失败后 slot/channel 可能处于不一致状态（err_flag 已置位、kernel 可能挂起），继续下发只会叠加不可预测的失败；闩锁强制用户 Destroy 后重建 client，把错误处理变成确定性的。这比「尽力继续」更适合底层传输库。

## 5. 综合实践

**任务：绘制 CS server 与 CS client 的完整交互示意图，标注建链与内存注册消息的流向。**

这是本讲规格中指定的实践任务，综合了 4.1～4.3 的全部内容。要求：

1. **画两个泳道**（server 进程 / client 进程），中间一条 TCP 控制面、下方一条数据面（HCCS/RDMA）。
2. **按时间序标注以下阶段**，每条消息注明 `CtrlMsgType` 编号与方向：
   - server 先行：`HixlCSServerCreate`（端点创建 + trans_flag 注册）→ `RegMem`（用户内存，标注「在端点上登记、可多次」）→ `Listen`（epoll 循环启动）；
   - client 建链：`Create` → `Connect` 内部三步握手（MatchEndpoint → GetRemoteMem → CreateChannel），标注 client 在 ③ 步「并发在本端建 kClient channel」；
   - 内存注册流向：server 的 `RegMem` 是**本地静默动作**（不产生消息！），远端感知靠握手第 ② 步的 `kGetRemoteMemReq/Resp` + `MemImport`——在图上用不同颜色区分「本地操作」与「控制面消息」；
   - 传输：client 单向箭头走数据面（host 路径 `*OnThread` / device 路径 `HixlBatchGet/Put` 内核），末尾一次 8 字节 flag READ，server 无任何参与动作。
3. **验证图**：把图中每条消息与本地操作映射回源码行号（用本讲给出的永久链接），任何在源码中找不到依据的箭头都删掉。
4. 若有环境，跑一遍 quickstart（u1-l3），对照 4.2.4 的日志顺序校验图中 server 侧事件次序；无环境则标注「待本地验证」。

**预期产出**：一张消息序列图 + 一张「消息/操作 → 源码位置」对照表。这张图也是阅读 u4-l2（消息处理器细节）、u4-l3（Channel/TransferPool）时的导航地图。

## 6. 本讲小结

- CS 模块是 HIXL Engine 数据面的最底层：`hixl_cs.h` 提供 15 个 `extern "C"` 接口（server 5 + client 10），句柄全部是 `void*`，错误码精简为 SUCCESS/PARAM_INVALID/TIMEOUT/FAILED 四个。
- server 是彻底的被动方：初始化时创建端点、注册 trans_flag、启动单线程 epoll 循环；数据面动作在注册时完成，之后只需响应四类控制消息（MatchEndpoint/CreateChannel/GetRemoteMem/DestroyChannel）。
- client 是唯一主动方：`Connect` 内部完成「匹配端点 → 预取并导入远端内存 → 两端并发建 channel」三步握手；一个 CS client 实例只对应一条链路。
- 一次内存注册在 server 侧要落到**每一个匹配端点**上（`reg_mems_` 台账记录全部 `EndpointMemInfo`），注销时反向逐一清理。
- 传输按本端端点位置分两条路径：host 端点走本进程线程（`ReadNbi/WriteNbiOnThread` + 重试），device 端点走 device 内核（`HixlBatchGet/HixlBatchPut`，每块 ≤128 条 desc）；完成感知统一复用 server 注册的 8 字节 trans_flag，本身就是一次单边读。
- 传输失败闩锁 `transfer_failure_latched_` 保证 FAILED/TIMEOUT 后不再接受新任务，把错误恢复收敛为「销毁重建」。

## 7. 下一步学习建议

下一讲 **u4-l2 消息处理器与接收器** 将放大本讲的控制面：`MsgHandler` 如何按 `CtrlMsgType` 分发、`MsgReceiver` 如何在 TCP 流上切出完整消息帧、`conn_msg_handler`/`mem_msg_handler` 如何构造本讲看到的请求与应答。建议先阅读 [src/hixl/cs/msg_handler.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/msg_handler.cc) 与 [src/hixl/common/ctrl_msg.h](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/ctrl_msg.h) 中的消息结构体定义，带着本讲交互图中的疑问去读：一条 `CreateChannelReq` 从字节流到 `HixlCSServer::CreateChannel` 的完整旅程是怎样的。

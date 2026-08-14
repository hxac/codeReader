# Rank 信息探测与建链：rank_info_detect

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚通信域初始化的 bootstrap 阶段里，各 rank 之间是如何互相"找到彼此"并交换信息的。
2. 理解 root rank（服务端）与非 root rank（客户端）在这个阶段各自做了什么，以及"root 信息交换"的完整消息序列。
3. 掌握 SocketAgent 的"长度前缀"消息成帧协议，理解为什么不能直接裸发字节流。
4. 理解白名单（Whitelist）机制如何从 JSON 配置文件一路生效到 socket 建链过滤，以及它在安全上的作用。
5. 对比 u1-l4 中已经用过的 root info 初始化方式与 rank table 初始化方式在底层的差异。

## 2. 前置知识

在进入源码前，先澄清几个概念（部分在 u1-l4、u2-l4 已建立，这里从本讲视角补充）：

- **bootstrap（自举）**：分布式系统中，一群起初互不知晓的进程，从"零信息"开始互相发现、交换地址、最终形成一张全局视图的过程。HCOMM 的 bootstrap 只解决一个问题：把每个 rank 的本地设备/地址信息收集到 root，拼成全局 RankTable 再广播回去。
- **root rank 与 agent**：调用 `HcclGetRootInfo` 的那个 rank 充当服务端（源码中叫 server/root，内部载体是 `RankInfoDetectService`）；其余所有 rank 充当客户端（源码中叫 agent/client，内部载体是 `RankInfoDetectClient`）。
- **root info 的本质**：u1-l4 中我们说 root info 是 4108 字节"不透明块"。本讲揭晓它在 V2（A5 新架构）下的真实内容——一个 `HcclRootHandleV2` 结构体，核心就是 `ip + listenPort + identifier` 三元组，即"root 在哪、监听哪个端口、这次探测的唯一标识是什么"。
- **RankTable**：全局 rank 信息表，每个条目描述一个 rank 的设备号、各网络层（netLayer）地址等。它就是 u2-l4 中 `RankGraphV1::Init` 建表消费的数据，本讲模块产出的正是它。
- **tag（标签）**：HCCP 底层 socket API 里挂在连接上的字符串标识，白名单规则以 tag 为单位下发，只有携带正确 tag 的连接才被放行。
- **TCP 粘包问题**：流式 socket 上连续 send 的多条消息可能被合并接收。经典解法是"长度前缀成帧"：先发 8 字节长度，再发正文。本讲的 SocketAgent 就是这个模式。

## 3. 本讲源码地图

本讲聚焦 `src/coll_communicator_mgr/rank_info_detect/` 目录：

| 文件 | 作用 |
| --- | --- |
| `rank_info_detect.h/.cc` | 编排入口 `RankInfoDetect`：root 侧 `SetupServer`、agent 侧 `SetupAgent`、状态等待 `WaitComplete` |
| `rank_info_detect_service.h/.cc` | root 侧服务：接收所有 agent 连接、收集并整合全局 RankTable、广播结果 |
| `rank_info_detect_client.h/.cc` | agent 侧客户端：构造本地 RankTable、连接 root、上报/接收/校验 |
| `socket_agent.h/.cc` | socket 消息收发代理，实现长度前缀成帧协议 |
| `whitelist.h/.cc` | 白名单单例：加载 JSON 配置、提供 host IP 白名单查询 |
| `bootstrap_ip.h/.cc` | bootstrap IP 选择：从本机网卡中按环境变量/白名单挑出通信 IP |
| `root_handle_v2.h` | `HcclRootHandleV2` 结构体定义（root info 的 V2 真身） |
| `root_info_detect_provider.cc` | 把 `RankInfoDetect` 能力注册成 bridge 回调（`GetRootInfoImpl`/`DetectRankTableImpl`） |
| `rank_info_dispatcher.h/.cc` | root 侧多线程广播器，用 epoll + 工作线程并发下发 RankTable |

上游调用方在 `src/legacy/ascend950/framework/entrance/op_base/op_base_v2.cc`（经 bridge 槽位转发，见 4.1.3）。

## 4. 核心概念与源码讲解

### 4.1 模块一：RankInfoDetect —— bootstrap 的总编排

#### 4.1.1 概念说明

`RankInfoDetect` 是本目录的唯一门面。同一个类，在 root rank 上走 `SetupServer`（拉起探测服务线程），在其他 rank 上走 `SetupAgent`（作为客户端连上去）。它解决的问题是：

1. **我在哪**：确定本机用于 bootstrap 的 IP（bootstrap IP）和监听端口。
2. **root 在哪**：root 把 `ip + port + identifier` 打包成 `HcclRootHandleV2` 交给应用层广播（示例中就是 MPI_Bcast）。
3. **何时算完**：agent 侧需要一个机制等待 root 侧探测服务结束（`WaitComplete` + 全局状态表）。

#### 4.1.2 核心流程

root 侧 `SetupServer` 的流程：

```text
HcclGetRootInfo（应用调用，root rank）
  └─ bridge 转发 → GetRootInfoImpl
       └─ RankInfoDetect::SetupServer(rootHandle)
            1. HccpPeerManager::Init —— host 网卡使能
            2. GetBootstrapIp(devPhyId) —— 选出本机 bootstrap IP
            3. GetHostListenPort() —— 决定监听端口
            4. ServerInit() —— 创建 serverSocket 并监听
            5. GetRootHandle(rootHandle) —— 生成 identifier，填充 root info
            6. GetHandleAndAddHostSocketWhitelist() —— 下发白名单（含 tag）
            7. detach 一个线程跑 SetupRankInfoDetectService —— 真正的探测服务
       应用把 rootHandle 广播给所有 rank
```

agent 侧 `SetupAgent` 的流程：

```text
HcclCommInitRootInfo（各 rank 调用）
  └─ ... → DetectRankTableImpl
       └─ RankInfoDetect::SetupAgent(rankSize, rankId, rootHandle)
            1. 网卡使能（PeerManager + HdcManager）
            2. GetBootstrapIp —— 同样要选出本机 IP
            3. ClientInit(rootHandle) —— 按 rootHandle.ip/port 建 clientSocket
            4. RankInfoDetectClient::Setup —— 上报、收全局表（见 4.4）
       └─ WaitComplete —— 等 root 侧服务状态变为 IDLE
       └─ GetRankTable —— 交给上层建通信域
```

#### 4.1.3 源码精读

先看门面类的声明，注意它同时持有 server/client 两套状态：

[rank_info_detect.h:31-64](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/rank_info_detect.h#L31-L64) —— `RankInfoDetect` 类：对外只有 4 个方法（`SetupServer`/`SetupAgent`/`GetRankTable`/`WaitComplete`）；私有成员里 `hostIp_`/`hostPort_` 是本机 bootstrap 地址，`hostSocketWlist_`/`wlistInfo_` 是白名单相关数据，`g_detectServerStatus_` 是静态的"监听端口 → 服务状态"并发映射表。

root info 的 V2 真身：

[root_handle_v2.h:24-29](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/root_handle_v2.h#L24-L29) —— `HcclRootHandleV2` 只有四个字段：`ip`（最长 64 字节）、`listenPort`（默认 `HCCL_INVALID_PORT`）、`netMode`（固定 HDC）、`identifier`（最长 128 字节）。u1-l4 中那块 4108 字节不透明 buffer，在 V2 路径下装的就是这个结构体。

`SetupServer` 主体：

[rank_info_detect.cc:76-118](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/rank_info_detect.cc#L76-L118) —— 按注释分四步：①创建 serverSocket 并监听；②构建 rootHandle；③端口和 identifier 就绪后下发白名单（tag 依赖二者，所以必须放在后面）；④detach 线程运行探测服务，主线程立即返回——这就是为什么应用拿到 root info 时服务才刚开始收连接。

端口决策逻辑：

[rank_info_detect.cc:304-328](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/rank_info_detect.cc#L304-L328) —— `GetHostListenPort` 的三级决策：配置了端口范围则返回无效值（后面走 `PreemptPortManager` 抢占式选端口）；配置了 `basePort` 则用 `basePort + devPhyId`（每张卡错开一个端口）；都没配则在默认范围 \[60000, 60015\] 内轮询抢占。起始端口常量定义在 [rank_info_detect.cc:35-36](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/rank_info_detect.cc#L35-L36)。

identifier 的生成：

[rank_info_detect.cc:330-344](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/rank_info_detect.cc#L330-L344) —— identifier 是 `IP_port_devPhyId_时间戳` 拼接的字符串，保证同一机器反复初始化通信域时每次探测有唯一标识。它后面会被拼进 socket tag，参与白名单匹配。

agent 侧入口：

[rank_info_detect.cc:222-246](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/rank_info_detect.cc#L222-L246) —— `SetupAgent`：网卡使能 → 取本机 IP → `ClientInit` 用 `rootHandle.ip`/`rootHandle.listenPort` 构造 clientSocket → 创建 `RankInfoDetectClient` 并调 `Setup` 拿回 rankTable。

那么谁调用 `SetupServer`/`SetupAgent`？答案是一层"bridge 注册"机制：

[root_info_detect_provider.cc:37-74](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/root_info_detect_provider.cc#L37-L74) —— `GetRootInfoImpl`：创建 `RankInfoDetect` 对象、`SetupServer(rootHandle)`，然后把 `HcclRootHandleV2` memcpy 进对外的 `HcclRootInfo` 固定大小缓冲区——对外接口布局不变，内部实现换成 V2。

[root_info_detect_provider.cc:76-108](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/root_info_detect_provider.cc#L76-L108) —— `DetectRankTableImpl`：agent 侧对应实现。注意最后 `detectContext = rankInfoDetectAgent;` —— 把对象指针做类型擦除交给调用方持有，保证探测资源在通信域初始化完成前不被析构（注释里写明了这个意图）。

消费方在 legacy 入口层：

[op_base_v2.cc:583-590](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/legacy/ascend950/framework/entrance/op_base/op_base_v2.cc#L583-L590) —— `HcclGetRootInfoV2` 只做一件事：从 bridge 槽位取 `getRootInfo` 回调并转发。

[op_base_v2.cc:1752-1759](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/legacy/ascend950/framework/entrance/op_base/op_base_v2.cc#L1752-L1759) —— `RootInfoDetect` 同样转发 `detectRankTable` 回调。这与 u2-l1 讲过的弱符号/dlsym 解耦一脉相承：legacy 入口保持符号不变，实现在 hcomm 侧通过 [root_info_detect_provider.cc:114-122](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/root_info_detect_provider.cc#L114-L122) 的静态 Registrar 在库装载期注册进槽位（bridge 注册/查询的实现见 [root_info_detect_bridge.cc:26-50](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/root_info_detect_bridge.cc#L26-L50)，只允许完整注册一次、禁止运行期替换）。

`WaitComplete` 的轮询等待：

[rank_info_detect.cc:375-416](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/rank_info_detect.cc#L375-L416) —— agent 侧收完广播后并非立即结束：还要按监听端口查 `g_detectServerStatus_`，轮询（每毫秒一次）直到 root 侧服务状态到达期望值（IDLE）；若状态为 ERROR 则抛内部异常，超时则上报 EI0015 并抛 `TimeoutException`。这个设计确保 agent 不会在 root 还没清理完 socket 前就往下走。

#### 4.1.4 代码实践

**实践目标**：理清 `HcclGetRootInfo` 在 V2 路径下的完整调用链，确认"root info 里到底装了什么"。

**操作步骤**（源码阅读型实践，无需硬件）：

1. 打开 [rank_info_detect.cc:330-371](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/rank_info_detect.cc#L330-L371)，阅读 `GetRootHandle`，列出 `HcclRootHandleV2` 四个字段各自的取值来源。
2. 反向追一步：在 `src/` 全局搜索 `SetupServer`（用 Grep），确认除 `root_info_detect_provider.cc` 外没有其他调用方。
3. 再正向追一步：阅读 [op_base_v2.cc:1752-1759](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/legacy/ascend950/framework/entrance/op_base/op_base_v2.cc#L1752-L1759)，画出"应用 → HcclCommInitRootInfo → ... → DetectRankTableImpl → SetupAgent"的时序草图。

**需要观察的现象 / 预期结果**：草图上应呈现三层（legacy 入口层 → bridge 槽位 → hcomm 实现层），且 root 侧 `SetupServer` 是"异步拉线程"而 agent 侧 `SetupAgent` 是同步阻塞直至拿到 RankTable。本实践为纯阅读，运行现象待本地验证（如需日志佐证，可在昇腾环境设置 HCCL 日志级别后运行 u1-l4 的示例，观察 `HcclGetRootInfoV2 success ... host ip[...] port[...]` 类日志）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `SetupServer` 里下发白名单必须放在 `GetRootHandle` 之后？

**答案**：白名单条目的 tag 由 `RANK_INFO_DETECT_TAG + identifier + 监听端口` 拼接（见 [rank_info_detect.cc:192](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/rank_info_detect.cc#L192)），而 identifier 又依赖实际监听端口和时间戳。端口在 `ServerInit` 监听后才能最终确定（抢占模式下是系统分配的），所以必须"先监听 → 再生成 rootHandle → 再按最终 tag 下发白名单"，代码注释也明确了这个顺序（L98-99）。

**练习 2**：`WaitComplete` 为什么用"轮询 1ms"而不是条件变量？

**答案**：状态表 `g_detectServerStatus_` 是 `UniversalConcurrentMap`（进程内跨对象共享的并发 map），server 线程与 agent 调用方分属两个对象，用一个简单轮询避免了跨对象的条件变量同步复杂度；等待时长通常只是服务收尾的毫秒级窗口，代价可控。超时上限来自 `GetLinkTimeOut()` 配置，防止死循环。

### 4.2 模块二：SocketAgent —— 长度前缀消息成帧

#### 4.2.1 概念说明

bootstrap 阶段双方要交换多种消息（agentId 字符串、rankSize 整数、RankTable 字节流），但底层 socket 是字节流，没有"消息边界"。`SocketAgent` 是一个极薄的封装（总共不到 40 行实现），用"先发 8 字节长度、再发正文"的协议解决粘包/拆包问题，并把收发失败统一转成异常。

#### 4.2.2 核心流程

```text
发送方                          接收方
SendMsg(data, len):             RecvMsg(msg, &len):
  Send(len 的 8 字节)   ─────→    Recv 8 字节得到 len
  Send(data, len 字节)  ─────→    校验 0 < len ≤ 100MB
                                  Recv len 字节填入 msg
```

任意一方失败都抛 `SocketException`，由上层（service/client）的异常处理统一走失败广播或超时路径。

#### 4.2.3 源码精读

[socket_agent.h:19-28](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/socket_agent.h#L19-L28) —— `SocketAgent` 只包一个裸 `Socket*` 指针，不拥有资源，两个方法 `SendMsg`/`RecvMsg`。

[socket_agent.cc:19-34](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/socket_agent.cc#L19-L34) —— `SendMsg`：两次 Send——先 8 字节 `u64` 长度，再正文。任何一步失败抛 `SocketException`。

[socket_agent.cc:36-56](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/socket_agent.cc#L36-L56) —— `RecvMsg`：先收长度，做合法性校验（长度为 0 或超过 `MAX_BUFFER_LEN` 直接返回 false，防止对端异常数据撑爆内存），再按长度收正文。`MAX_BUFFER_LEN = 100MB` 定义在 [root_handle_v2.h:32](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/root_handle_v2.h#L32)，这是单条 bootstrap 消息的上限——大集群的 RankTable 也装得下。

#### 4.2.4 代码实践

**实践目标**：验证"长度前缀"协议，并理解它与裸 socket 的差异。

**操作步骤**（源码阅读型）：

1. 阅读 [socket_agent.cc:36-56](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/socket_agent.cc#L36-L56)，数一数一次 `SendMsg`/`RecvMsg` 各触发几次底层 `Socket::Send`/`Recv`。
2. 思考实验：如果 agent 连发 `SendAgentIdAndRankSize` 的两条消息（见 4.4.3）而 root 用裸 `Recv` 一次性读 16+4 字节，会发生什么？

**需要观察的现象 / 预期结果**：每次收发各 2 次系统调用；思考实验的答案是——TCP 可能把两条消息合并投递，也可能拆开，裸读无法确定边界，这正是长度前缀存在的意义。待本地验证：可写一个 20 行的 Python socket 小脚本（`struct.pack('<Q', len)` + body）模拟同样协议观察粘包行为。

#### 4.2.5 小练习与答案

**练习 1**：`RecvMsg` 里 `revMsgLen > MAX_BUFFER_LEN` 的校验防的是什么攻击/故障？

**答案**：防对端发来的长度字段异常（对端 bug、内存越界、或恶意/损坏数据）导致本端按超大长度分配/读取。结合 `revMsgLen == 0` 的拒绝，保证了接收长度始终在 (0, 100MB] 的合理区间。

**练习 2**：`SocketAgent` 为什么持有裸指针 `Socket*` 而不是 `shared_ptr`？

**答案**：Socket 的生命周期由外层 `RankInfoDetectService`/`RankInfoDetectClient` 持有（`connSockets_`/`clientSocket_` 成员），`SocketAgent` 只是在已有连接上做消息收发的工具，不参与所有权管理，避免循环引用和多余引用计数开销。

### 4.3 模块三：Whitelist —— bootstrap 的安全闸门

#### 4.3.1 概念说明

bootstrap 服务在 host 网卡上监听一个 TCP 端口，这意味着集群中任何能路由到该端口的进程都可能尝试连接。白名单机制的作用是：**只有来自配置文件中列出的 IP、且携带正确 tag 的连接，才会被底层（HCCP/RA）放行**。它分两层生效：

1. **选 IP 时过滤**：`GetBootstrapIp` 只允许从白名单内的网卡里选 bootstrap IP。
2. **建链时过滤**：root 侧把白名单 IP + tag 通过 `HrtRaSocketWhiteListAdd` 下发到底层，连接建立时按 tag 匹配。

#### 4.3.2 核心流程

```text
白名单配置文件（JSON，路径来自 HCCL_WHITELIST_FILE）
  { "host_ip": ["192.168.1.10", "192.168.1.11", ...] }
        │ Whitelist::LoadConfigFile（解析+校验）
        ▼
Whitelist 单例（map: WhiteListType → vector<IpAddress>）
        │                          │
        ▼                          ▼
GetBootstrapIp 选 IP 时过滤    SetupServer 建链时：
（不在名单内的网卡不参与）      RaSocketWhitelist{remoteIp, connLimit, tag}
                               → HrtRaSocketWhiteListAdd 下发底层
        ...探测结束时...
TearDown → HrtRaSocketWhiteListDel 撤销规则
```

若环境变量 `HCCL_WHITELIST_DISABLE` 置 1（即 `GetWhitelistDisable()` 为真），则整条链路跳过——开发/测试环境常用。

#### 4.3.3 源码精读

[whitelist.h:22-33](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/whitelist.h#L22-L33) —— `Whitelist` 是 Meyers 单例（与 u2-l2 的 `CollCommMgr` 同一手法），内部用 `map<WhiteListType, vector<IpAddress>>` 按类型存名单，目前只有 `HCCL_WHITELIST_HOST` 一种类型，`HCCL_WHITELIST_RESERVED` 是预留位。

[whitelist.cc:42-96](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/whitelist.cc#L42-L96) —— `LoadConfigFile`：路径为空抛参数异常；文件打不开或 JSON 解析失败抛内部异常（日志会提示检查 json 格式）；解析成功后逐条把 `host_ip` 数组里的字符串转成 `IpAddress` 存入名单。

[whitelist.cc:98-111](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/whitelist.cc#L98-L111) —— `GetHostIp`：取 `host_ip` 字段，缺失则抛异常——即配置文件里必须有这个 key。

**第一层生效点——选 IP 时过滤**：

[bootstrap_ip.cc:232-265](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/bootstrap_ip.cc#L232-L265) —— `GetBootstrapIp`：查缓存 → `HrtGetHostIf` 拿全部网卡 → 白名单使能时经 `GetAllValidHostIfInfos` 过滤 → `FindLocalHostIp` 按 `HCCL_IF_IP`（精确 IP）→ `HCCL_SOCKET_IFNAME`（网卡名，支持 `^` 取反前缀）→ normal/docker/lo 网卡类别的优先级选出 IP → 缓存按 devPhyId 存储。过滤逻辑在 [bootstrap_ip.cc:50-65](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/bootstrap_ip.cc#L50-L65)：白名单使能时只有 IP 出现在名单里的网卡才是"有效网卡"。

**第二层生效点——建链时按 tag 放行**：

[rank_info_detect.cc:184-200](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/rank_info_detect.cc#L184-L200) —— `AddHostSocketWhitelist`：为名单中每个 IP 构造 `RaSocketWhitelist{remoteIp, connLimit=8, tag}`，tag 为 `"rank_info_detect_default_tag_" + identifier + "_" + port`，经 `HrtRaSocketWhiteListAdd` 下发。`connLimit=8` 对应单机 8 卡场景（常量注释 `HCCL_AISERVER_DEVICE_NUM`，[rank_info_detect.cc:38](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/rank_info_detect.cc#L38)）。

[rank_info_detect.cc:214-216](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/rank_info_detect.cc#L214-L216) —— 关键呼应：agent 侧 `ClientInit` 用**同一个 tag 公式**（`rootHandle.identifier + serverPort`）创建 clientSocket。两端 tag 一致，底层建链时才能匹配上 root 下发的白名单条目——这就是"白名单在连接建立时生效"的具体机制：identifier 通过 root info 广播到所有 rank，等于把"这次探测的入场券"随 root info 一起发了出去。

撤销与清理：

[rank_info_detect_service.cc:432-465](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/rank_info_detect_service.cc#L432-L465) —— 服务析构 `TearDown`：断开所有连接 → 白名单使能时 `HrtRaSocketWhiteListDel` 删除规则 → 释放监听端口 → 销毁 socket 句柄 → 网卡去初始化。白名单规则与探测服务同生命周期，探测结束即收回入场资格。

#### 4.3.4 代码实践

**实践目标**：亲手构造一份白名单配置并追踪它两条生效路径。

**操作步骤**：

1. 参照 [whitelist.cc:98-111](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/whitelist.cc#L98-L111) 的解析要求，写一个 `wl.json`：`{"host_ip": ["<本机某网卡IP>"]}`。
2. 通读 [rank_info_detect.cc:120-140](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/rank_info_detect.cc#L120-L140)（`GetHostSocketHandle`）与 [rank_info_detect.cc:164-200](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/rank_info_detect.cc#L164-L200)（`GetHandleAndAddHostSocketWhitelist`/`AddHostSocketWhitelist`），在纸上写出：名单 IP → `RaSocketWhitelist` 字段值 → 下发 API → 何时删除。
3. 反向实验（阅读推演）：若 `HCCL_WHITELIST_DISABLE=0`（使能）但 `HCCL_WHITELIST_FILE` 未设置，对照 [bootstrap_ip.cc:23-48](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/bootstrap_ip.cc#L23-L48) 推断启动时的行为。

**需要观察的现象 / 预期结果**：步骤 3 的推演结论是 `GetHostSocketWhitelist` 发现文件名为空直接抛 `InternalException`（日志提示 `HCCL_WHITELIST_DISABLE variable is [0], but HCCL_WHITELIST_FILE is not set`）——使能白名单就必须提供名单文件，这是"安全默认"取向。真实运行现象待本地验证（需昇腾环境 + 对应环境变量）。

#### 4.3.5 小练习与答案

**练习 1**：tag 里为什么要拼上 `identifier`（含时间戳），只用固定前缀不行吗？

**答案**：固定 tag 意味着上一次（或另一个）通信域探测的连接凭证仍然有效，可能被复用绕过本次探测的隔离。identifier 含 IP、端口、devPhyId 和时间戳，保证每次探测的 tag 全局唯一，配合 TearDown 时的 `WhiteListDel`，实现"一次探测一套入场券，用完即焚"。

**练习 2**：白名单在两条路径上分别过滤什么？

**答案**：路径一（`GetBootstrapIp`）过滤**本端可选的网卡/IP**——不在名单里的网卡根本不会被选为 bootstrap 通信口；路径二（`AddHostSocketWhitelist` + tag）过滤**对端接入连接**——只有名单内 IP 且携带匹配 tag 的 client 连接才被底层放行。前者管"我连谁/用什么连"，后者管"谁能连我"。

### 4.4 模块四：root 信息交换的完整消息序列

#### 4.4.1 概念说明

本模块把 service 和 client 串起来，回答本讲核心问题：**"root 信息交换"到底传了哪些消息、按什么顺序、出错怎么办**。此外还包含两个容易忽略的细节：rankSize 一致性校验（防止各 rank 认知的集群规模不一致）和 TLS 开关一致性校验（安全配置对齐）。

#### 4.4.2 核心流程

以 rankSize=N 的集群为例，一次完整的消息序列（全部走 SocketAgent 的长度前缀协议）：

```text
root（RankInfoDetectService）                各 agent（RankInfoDetectClient，共 N 个）
────────────────────────────                ─────────────────────────────────────────
SetupServer：监听、发白名单、拉线程
                                            Setup：
                                            1. ConstructRankTable —— 读 /etc/hccl_rootinfo.json
                                               或 TopoAddrInfoGet 拿本机拓扑 JSON，
                                               抽出本机条目组成 localRankTable
                                            2. Connect + CheckStatus —— 连 root，轮询至 OK
①接受连接（GetConnections 循环 N 次）  ◄──── 3. SendMsg(agentId)     // 16 字符定长串
②对每个连接：                          ◄──── 4. SendMsg(rankSize)
   RecvRemoteAgentId（存入 connSockets_           // server 用第一个 rankSize 定 N，
   以 agentId 为 key）                              并与后续 rankSize 逐一比对
   RecvRemoteRankSize + VerifyRemoteRankSize
③GetRankTable：对每个连接      ◄──── 5. SendMsg(localRankTable 字节流 + step=0)
   RecvRankInfoMsg + ParseRankTable                // 消息格式 [ranktable(nB)][step(4B)]
   （校验 step 一致后合并，按 rankId 排序）
④BroadcastRankTable（多线程+epoll） ────► 6. RecvMsg(全局 rankTable + step + failedAgentIdList)
                                                  // [ranktable(nB)][step(4B)][failedAgentIdList]
                                            7. VerifyRankTable：
                                               rankCount == rankSize、rankTable_.Check()、
                                               TLS 开关一致性校验
⑤TearDown：删白名单、关 socket、释放端口
                                            8. WaitComplete —— 等 root 状态变 IDLE
```

失败路径（任一 agent 建链超时）：root 在 `GetConnections` 超时后把未连上的 rankId 拼成 `failedAgentIdList`（"临终遗言"）随广播下发，agent 收到非空列表即打 ERROR 日志；超时的 agent 自己 `sleep 20s` 再退出（`WAIT_ERROR_BROADCAST_TIME`，[rank_info_detect_client.h:29-33](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/rank_info_detect_client.h#L29-L33)），确保正常 agent 能收到遗言后才让上层报错退出。

#### 4.4.3 源码精读

root 侧三步主流程：

[rank_info_detect_service.cc:31-41](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/rank_info_detect_service.cc#L31-L41) —— `Setup` 就是序列图的骨架：`GetConnections`（收连接）→ `GetRankTable`（收本地表并整合）→ `BroadcastRankTable`（广播全局表）。

[rank_info_detect_service.cc:43-140](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/rank_info_detect_service.cc#L43-L140) —— `GetConnections`：以"第一个连上来的 rank 上报的 rankSize"为期望连接数，循环 `Accept`（`GetStatus` 带剩余超时），每连上一个就做 agentId + rankSize 的接收与校验；超时则上报 EI0015、把缺口 rank 记入失败列表；一个都没连上则直接抛异常。

[rank_info_detect_service.cc:304-335](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/rank_info_detect_service.cc#L304-L335) —— `RecvAndVerifyRemoteAgentIdAndRankSize`：收 agentId（还检查重复连接）、存 `connSockets_`、收 rankSize 并交给 `VerifyRemoteRankSize`——首个 rankSize 作为基准，之后任何不一致都失败，防止"有的进程以为集群是 8 卡、有的以为是 16 卡"这种静默错配。

[rank_info_detect_service.cc:142-173](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/rank_info_detect_service.cc#L142-L173) —— `GetRankTable` 逐连接收字节流、`ParseRankTable` 解析、按 rankId 排序；`BroadcastRankTable` 委托给 `RankInfoDispather`（多线程 epoll 并发广播，见 [rank_info_dispatcher.cc:36-55](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/rank_info_dispatcher.cc#L36-L55)，线程数按 rankNum 计算、有上限）。

agent 侧五步：

[rank_info_detect_client.cc:150-172](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/rank_info_detect_client.cc#L150-L172) —— `Setup`：构造 localRankTable → （必要时）host 监听端口探测 → 连接 root → 发 agentId/rankSize → 发本地表 → 收全局表。与序列图一一对应。

[rank_info_detect_client.cc:212-226](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/rank_info_detect_client.cc#L212-L226) —— `SendAgentIdAndRankSize`：agentId 是"左补零到 16 位"的 rankId 字符串（如 rank 3 → `"0000000000000003"`），定长便于服务端以字符串为 key 管理连接。

[rank_info_detect_client.cc:228-246](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/rank_info_detect_client.cc#L228-L246) —— `SendLocalRankTable`：`BinaryStream` 序列化本地表后追加 `currentStep_`，即消息格式 `[ranktable(n字节)][step(4字节)]`。step 是双方同步的阶段计数器，服务端在 [rank_info_detect_service.cc:238-265](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/rank_info_detect_service.cc#L238-L265) 的 `ParseRankTable` 里校验收到的 step 与自己当前 step 一致，不一致即抛异常——防止消息错位。

[rank_info_detect_client.cc:320-374](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/rank_info_detect_client.cc#L320-L374) —— `ConstructRankTable`：本地表的数据来源有二——优先读 `/etc/hccl_rootinfo.json`（手工部署场景），否则经 `TopoAddrInfoGet` 从系统取本机拓扑 JSON；单 P（rankSize=1）走捷径直接构造单条目；之后还可能对 netLayer≥3 的 host RoCE 地址做主备探测（`SelectLocalHostBackupAddr`，用 `HcommEndpointCreate/Destory` 探测可用性后改写上报地址）——这解释了为什么 bootstrap 之外的数据面地址也是"探测过再上报"。

[rank_info_detect_client.cc:558-597](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/rank_info_detect_client.cc#L558-L597) —— `ParseRankTable`/`RecvRankTable`：接收格式为 `[ranktable(n字节)][step(4字节)][failedAgentIdList]`，比上行消息多一个失败列表字段；`failedAgentIdList` 非空即打印 root 的"临终遗言"。

[rank_info_detect_client.cc:599-621](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/rank_info_detect_client.cc#L599-L621) —— `VerifyRankTable` 三道校验：rankCount 与本端认知的 rankSize 相等、`rankTable_.Check()` 内容校验、TLS 开关一致性。TLS 校验实现在 [rank_info_detect_client.cc:659-715](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/rank_info_detect_client.cc#L659-L715)：把所有 rank 的 tlsStatus 分成 Enable/Disable/Unknown 三组，Enable 与 Disable 并存时报 EI0016 错误（部分卡开 TLS、部分不开是不允许的），仅存在 Unknown 时打告警。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：完整梳理"root 信息交换"的消息序列，并说明 whitelist 在连接建立时如何生效。

**操作步骤**：

1. 打开 [rank_info_detect_client.cc:150-172](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/rank_info_detect_client.cc#L150-L172)（client 五步）和 [rank_info_detect_service.cc:31-41](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/rank_info_detect_service.cc#L31-L41)（server 三步），对照 4.4.2 的序列图，为图中每个编号箭头标注：发送函数 → 消息内容 → 接收函数 → 校验点。
2. 追踪 whitelist 生效点：从 [rank_info_detect.cc:92-99](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/rank_info_detect.cc#L92-L99)（下发）到 [rank_info_detect.cc:214-216](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/rank_info_detect.cc#L214-L216)（agent 用同公式 tag 建连），再到 [rank_info_detect_service.cc:441-446](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/rank_info_detect_service.cc#L441-L446)（结束时删除），画出白名单规则的生命周期线。
3. 思考题（写进你的笔记）：如果把 root info 里的 `listenPort` 改成 0（无效端口），序列会在哪一步、以什么错误终止？对照 [rank_info_detect_client.cc:180-210](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/rank_info_detect_client.cc#L180-L210) 的 `CheckStatus` 与 `WAIT_ERROR_BROADCAST_TIME` 给出结论。

**需要观察的现象 / 预期结果**：

- 你应得到一张 6 类消息的收发表（agentId、rankSize、localRankTable+step、全局 rankTable+step+failedList），并确认每条消息都经 SocketAgent 长度前缀成帧。
- 思考题预期：agent 连接/等待状态在超时（`GetLinkTimeOut()` 配置）后报 EI0015，并 sleep 20 秒（测试构建下 1 秒）再抛 `TimeoutException`，目的是给其他 agent 留出接收 root 失败广播的时间窗口。
- 运行现象待本地验证（需多机或单机多卡昇腾环境；可配合 u1-l4 的 `01_one_device_per_process` 示例与 HCCL 日志观察 `RankInfoDetectService`/`RankInfoDetectClient` 的收发日志）。

#### 4.4.5 小练习与答案

**练习 1**：server 如何知道要等多少个连接？如果两个 agent 上报的 rankSize 不同会怎样？

**答案**：`GetConnections` 初始只等 1 个连接，用第一个连上来的 agent 上报的 rankSize 作为期望值（见 [rank_info_detect_service.cc:327-331](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/rank_info_detect/rank_info_detect_service.cc#L327-L331) 的 `RecvAndVerifyRemoteAgentIdAndRankSize`）；后续每个 rankSize 都要和 `VerifyRemoteRankSize` 里的基准比对，不一致立即失败。这保证"集群规模"这一全局共识在 bootstrap 阶段就被强制统一。

**练习 2**：消息里附带 step 字段的目的是什么？

**答案**：step 是两端同步递增的阶段号（client 发送后自增、server 收到后自增）。server 校验 `receivedStep == currentStep_`，一旦消息错位（如重连后旧消息迟到、状态不同步）立刻暴露，避免把错误阶段的数据合并进全局表。这是无状态字节流协议上低成本的状态机校验。

**练习 3**：对比 u1-l4：root info 方式和 rank table 方式（`HcclCommInitClusterInfoConfig` 读共享 JSON）的底层差异是什么？

**答案**：root info 方式下 RankTable 是**运行时协商生成**的——本讲的 service/client 交换各自 `ConstructRankTable` 抽出的本地条目，root 合并成全局表再广播，还附带 rankSize/TLS 一致性校验和地址主备探测；rank table 方式则是**外部预生成**的——所有 rank 直接读同一个 JSON 文件（`/etc/hccl_rootinfo.json` 的角色类似），跳过了 socket 协商，但要求文件内容全局一致且由部署方保证正确。前者适合动态组网（配合外部广播 root info 即可），后者适合固定拓扑的批量部署。

## 5. 综合实践

**任务：为 bootstrap 探测写一份"消息序列 + 故障传播"说明书。**

假设你负责向新同事解释一次 4 机 × 8 卡（rankSize=32）训练任务启动时 bootstrap 阶段发生了什么，请基于本讲源码完成：

1. **时序图**：画出 root 与 agent 的完整消息序列（含白名单下发、6 类消息、TearDown、WaitComplete），标注每条消息的格式（长度前缀 + 正文结构）。
2. **故障剧本**：任选两个故障（建议：某个 agent 进程启动晚于 root 的 `GetLinkTimeOut`；某两卡 TLS 配置不一致），从源码指出各自在哪一行被检测、错误码（EI0015/EI0016）、以及错误如何传播到全部 rank（提示：`failedAgentIdList` 广播 + `WAIT_ERROR_BROADCAST_TIME` 的 20 秒等待）。
3. **安全说明**：用三句话向运维解释白名单：配置文件怎么写、两条生效路径分别挡住什么、探测结束后规则去向。

全部结论必须能落到具体文件与行号；无法在本地复现的运行现象标注"待本地验证"。

## 6. 本讲小结

- `RankInfoDetect` 是 bootstrap 门面：root 走 `SetupServer`（异步拉服务线程），agent 走 `SetupAgent`（同步拿 RankTable），入口经 `RootInfoDetectBridge` 从 legacy 层转发，保持接口符号稳定。
- root info 的 V2 真身是 `HcclRootHandleV2{ip, listenPort, netMode, identifier}`，identifier 含 IP/端口/devPhyId/时间戳，是本次探测的全局唯一标识。
- `SocketAgent` 用"8 字节长度前缀 + 正文"解决流式 socket 的消息边界问题，长度上限 100MB，失败统一转异常。
- 白名单双层生效：选 bootstrap IP 时过滤本端网卡，建链时以"tag = 前缀 + identifier + 端口"下发底层放行规则，探测结束即删除。
- 消息序列为：agentId → rankSize → localRankTable(+step) 上行；全局 rankTable(+step+failedAgentIdList) 下行；伴随 rankSize 一致性、step 一致性、TLS 一致性三道校验。
- 失败时 root 广播"临终遗言"（未建链 rankId 列表），超时 agent sleep 20 秒再退出，保证错误信息全网可见。

## 7. 下一步学习建议

下一讲 u2-l6 将进入 **Team 机制**（`HcclWorldTeamCreate` 与通道批量创建），看通信域之上如何组织 world/sub team 与通道资源。随后第 3 单元转入数据面 base_comm；与本讲直接衔接的还有两个方向：

1. **顺着 RankTable 往下**：重读 u2-l4 的 `RankGraphV1::Init`，体会本讲产出的 RankTable 如何被消费建图。
2. **顺着 Socket 往下**：`Socket`、`PreemptPortManager`、`HostSocketHandleManager` 的实现位于 base_comm 的 HCCP 资源模块（u4-l2 将展开），本讲的端口抢占（默认范围 60000~60015）正是依赖它。

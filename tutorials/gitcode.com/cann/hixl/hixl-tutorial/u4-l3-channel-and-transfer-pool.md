# u4-l3 Channel、TransferPool 与 Endpoint 存储

## 1. 本讲目标

上一讲（u4-l2）我们看清了 CS 控制面的「信使」——MsgHandler 如何用消息队列分发 MatchEndpoint、CreateChannel、GetRemoteMem、DestroyChannel 四类消息。本讲往下走一层，认识被这些消息驱动的三个**数据面核心构件**：

1. **Endpoint / EndpointStore**：端点（一条物理/虚拟链路的本端地址标识）如何被创建、登记、按描述匹配、销毁。
2. **Channel**：建立在两个匹配端点之上的数据面通道如何创建（含两端一致的 channelName 生成规则）、如何等待建链完成、如何销毁。
3. **TransferPool**：设备侧传输资源的「槽位池」——context、stream、Hcomm thread、notify、err_flag 等昂贵资源的预分配、租借、回收与故障重建。

学完本讲，你应该能独立回答：

- client 发来的 MatchEndpoint 请求最终落在哪个数据结构上做匹配？匹配规则是什么？
- 两端 Channel 如何保证 channelName 一致、建链状态如何轮询？
- 一次传输请求从 `Acquire` 槽位到 `Release`/`Abort` 归还，完整生命周期是什么？

## 2. 前置知识

在进入源码前，先澄清几个本讲反复出现的概念（承接 u4-l1/u4-l2 已建立的认识）：

- **EndpointDesc 与 EndpointHandle**：`EndpointDesc` 是 Hcomm 层的端点描述（协议 roce/hccs/ub 系、位置 device/host、通信地址 commAddr 等）；`EndpointHandle`（`void*`）是 Hcomm 库返回的端点实例句柄。u4-l2 讲过的 `dst_ep_handle`「通行证」就是某个 `EndpointHandle`。
- **EndpointStore**：进程内所有端点实例的容器，`map<EndpointHandle, EndpointPtr>`，server 端持有（见 `src/hixl/cs/hixl_cs_server.h:89` 的成员 `endpoint_store_`）。client 的 MatchEndpoint 请求最终就是在这里查找。
- **Channel**：两个端点之间的数据面通路。控制面 TCP 只用来「协商」，真正的单边读写走 Channel。创建 Channel 时两端必须使用**相同的 channelName**，否则 Hcomm 层无法把两端的 socket 角色配对。
- **槽位（Slot）**：一次设备侧传输需要一整套运行时资源——独立的 ACL context、默认 stream、一个 Hcomm thread（通信线程）、一个 notify（设备侧通知信号量）和一个 err_flag（错误标志字节）。这些资源创建昂贵，HIXL 把它们预分配成固定大小的池，按需租借。
- **HcommProxy / Hcomm 层**：u3-l5 讲过的弱符号代理层，`HcommEndpointCreate`、`HcommChannelCreate`、`HcommMemReg`、`HcommThreadAlloc` 等真正的底层实现都在 Hcomm 库中，本讲的 `Endpoint`/`Channel`/`TransferPool` 都是它们的 C++ 封装。
- **ACL（Ascend Computing Language）**：昇腾设备运行时 API，如 `aclrtCreateContext`、`aclrtCreateNotify`、`aclrtLaunchKernel`。槽位资源大多用 ACL API 创建。
- **引用计数与失败闩锁**：u4-l1 提过 `transfer_failure_latched_`——一旦传输以 FAILED/TIMEOUT 失败，槽位不再走普通 `Release`，而是走 `Abort` 强制重建。本讲会看到这两条回收路径的分叉点。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/hixl/cs/endpoint.h` / `endpoint.cc` | 单个端点的封装：创建/销毁、内存注册/导出/导入、Channel 的创建与销毁、主机内存 VA 映射 |
| `src/hixl/cs/endpoint_store.h` / `endpoint_store.cc` | 端点容器：登记、按句柄查找、按 EndpointDesc 匹配、批量销毁；内含按协议区分的 `EndpointDesc` 相等性比较 |
| `src/hixl/cs/channel.h` / `channel.cc` | 单条数据面通道：调用 Hcomm 建链、轮询建链状态、销毁 |
| `src/hixl/cs/transfer_pool.h` / `transfer_pool.cc` | 设备侧传输资源槽位池：per-device 单例、槽位初始化、Acquire/Release/Abort、同步上下文内核 |
| `src/hixl/cs/hixl_cs_client.cc`（节选） | 槽位的实际消费者：`AcquireSharedSlot`/`ReleaseSharedSlotRef`、内核分块下发 |
| `src/hixl/cs/hixl_cs_server.cc`（节选） | server 侧 TransferPool 初始化与端点创建入口 |

## 4. 核心概念与源码讲解

本讲的最小模块为：**Channel、TransferPool、EndpointStore**（Endpoint 是 EndpointStore 的元素，与 Channel 关系紧密，作为模块一的一部分讲解）。

### 4.1 Endpoint 与 EndpointStore：端点的封装与存储

#### 4.1.1 概念说明

「端点」回答的问题是：**本进程在某个协议（HCCS/RoCE/UB）的哪个地址上可被对端访问**。一个进程可能同时在多个协议上有端点（例如 A5 芯片同时有 ScaleOut RoCE 端点和 UB 端点，见 u3-l3 的 EndpointGenerator），因此需要：

- `Endpoint` 类：封装单个端点，职责包括生命周期（Initialize/Finalize）、内存登记（RegisterMem/DeregisterMem）、内存导出/导入（ExportMem/MemImport——这正是 u4-l2 中 GetRemoteMem 消息背后的操作）、以及在该端点上创建/销毁 Channel。
- `EndpointStore` 类：把所有 `Endpoint` 实例收进一张 `map<EndpointHandle, EndpointPtr>`，对外提供创建、句柄查找、**按 EndpointDesc 匹配**和批量销毁。

匹配（MatchEndpoint）是关键：client 只知道对端的 `EndpointDesc`（描述「我要连谁」），而 server 侧持有的是一批已初始化的端点实例。`EndpointStore::MatchEndpoint` 用逐条比较把描述翻译成句柄，失败返回 `nullptr` 并报 `PARAM_INVALID`——这就是 u3-l3 讲过的「endpoint 匹配失败返回 PARAM_INVALID」在 CS 层的落点。

#### 4.1.2 核心流程

端点在 server 侧的完整生命流程：

```text
CreateEndpointList (u3-l3 的 EndpointGenerator 产出)
        │
        ▼
EndpointStore::CreateEndpoint ──► Endpoint::Initialize ──► HcommEndpointCreate
        │                                                        │
        ▼                                                        ▼
endpoints_[handle] = EndpointPtr                        handle_（底层句柄）
        │
        │  client 的 MatchEndpoint 请求到达（u4-l2 的 MsgHandler 分发）
        ▼
EndpointStore::MatchEndpoint(req.dst) ── operator== 按协议逐字段比较 ──► 命中返回 EndpointPtr
        │
        ├──► Endpoint::RegisterMem（内存消息，u4-l2 的 MemMsgHandler）
        ├──► Endpoint::CreateChannel（建链消息，本讲 4.2）
        └──► EndpointStore::Finalize ──► 逐个 Endpoint::Finalize（销毁 Channel → 解注册内存 → 销毁端点）
```

#### 4.1.3 源码精读

**端点创建与登记**。`StoreEndpoint` 先真正初始化端点（调 Hcomm），成功后才加锁入表——失败时不会留下半成品：

- [src/hixl/cs/endpoint.cc:L94-L100](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/endpoint.cc#L94-L100)：`Endpoint::Initialize` 调 `HcommProxy::EndpointCreate` 把 `EndpointDesc` 变成底层句柄 `handle_`，全程持互斥锁。
- [src/hixl/cs/endpoint_store.cc:L29-L35](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/endpoint_store.cc#L29-L35)：`StoreEndpoint` 先 Initialize、取句柄，再存入 `endpoints_` map。两个 `CreateEndpoint` 重载（[L16-L27](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/endpoint_store.cc#L16-L27)）只差在是否显式指定 host VA 映射开关。

**按描述匹配**。匹配的核心是内联的 `operator==`，比较字段**按协议不同而不同**：

- [src/hixl/cs/endpoint_store.cc:L55-L77](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/endpoint_store.cc#L55-L77)：`operator==` 规则表——HCCS 比 `commAddr.id`；UB 系（UBC_TP/UBC_CTP/UBG）比 EID 字节串；RoCE/UBOE 比地址类型加 IPv4/IPv6 地址。协议不同直接判不等。注意：**未覆盖的协议组合返回 false**，所以「描述写错协议」等价于匹配失败。
- [src/hixl/cs/endpoint_store.cc:L79-L90](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/endpoint_store.cc#L79-L90)：`MatchEndpoint` 线性扫描所有端点，命中即返回 `EndpointPtr` 并回填 `endpoint_handle`，全不命中记 ERROR 日志并返回 `nullptr`。

**端点上的内存登记与 host VA 映射**。`RegisterMem` 有一个重要分支——device 端点上的 host 内存需要先做主机内存注册映射：

- [src/hixl/cs/endpoint.cc:L151-L191](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/endpoint.cc#L151-L191)：当内存类型是 `COMM_MEM_TYPE_HOST` 且端点启用 `need_host_va_mapping_` 时，先经 `HostRegisterProxy::RegisterByDev` 得到设备侧映射地址，再把它**伪装成 device 内存**去调 `HcommMemReg`（u2-l3 讲过的 register_dev_addr 机制在 CS 层的实现）。注册结果连同 `registered_dev_mem` 一起记入 `reg_mems_` 台账，ScopeGuard 保证失败回滚。

是否默认启用映射由构造函数决定：

- [src/hixl/cs/endpoint.cc:L27-L38](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/endpoint.cc#L27-L38)：匿名命名空间的 `IsDefaultHostVaMappingEnabled`——device 位置且协议为 UBOE/UBG/UBC_CTP 时默认开启；`IsHostVaMappingEnabledForPair` 额外处理 UBC_CTP 需对端也在 device 的特例。
- [src/hixl/cs/endpoint.cc:L210-L221](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/endpoint.cc#L210-L221)：`ExportMem` 遍历 `reg_mems_`，对每块内存调 `HcommMemReg` 的逆操作 `HcommMemExport` 生成可发给对端的导出描述——u4-l2 中 client `GetRemoteMem` 拉到的 JSON 里的 `export_desc` 就来自这里；对端用 [src/hixl/cs/endpoint.cc:L270-L277](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/endpoint.cc#L270-L277) 的 `MemImport` 把描述重新变成本地可用的 `CommMem`。

**销毁顺序**。`Endpoint::Finalize` 体现「后建先拆」：

- [src/hixl/cs/endpoint.cc:L102-L137](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/endpoint.cc#L102-L137)：先销毁所有 Channel，再逐块 `HcommMemUnreg` 解注册内存（host 映射内存还要 `HostRegisterProxy::UnregisterByDev` 反注册），最后 `HcommEndpointDestroy` 销毁端点本身；任何一步失败只记录并继续，返回首个错误。
- [src/hixl/cs/endpoint_store.cc:L92-L106](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/endpoint_store.cc#L92-L106)：`EndpointStore::Finalize` 遍历销毁全部端点后清空 map，聚合首个失败状态返回。

#### 4.1.4 代码实践

**实践目标**：验证 MatchEndpoint 的匹配规则，理解「什么描述能匹配上、什么匹配不上」。

**操作步骤**：

1. 打开 [src/hixl/cs/endpoint_store.cc:L55-L77](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/endpoint_store.cc#L55-L77)，为每种协议各写一行「比较字段」笔记。例如：HCCS→`commAddr.id`；UBG→EID 字节串；ROCE→地址类型+IP。
2. 在 [src/hixl/cs/hixl_cs_server.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_server.cc) 中找到 `MatchEndpoint` 的调用点（`CreateChannel` 消息处理内，约 L409 附近 `endpoint_store_.MatchEndpoint(req.dst, handle)`），确认请求体里的 `dst` 字段类型就是 `EndpointDesc`。
3. 构造三组假想的 client 请求描述，对照 `operator==` 手工推演匹配结果：
   - 协议 HCCS、id 相同、locType 不同；
   - 协议 ROCE、同为 IPv4、地址不同；
   - 协议 UBOE、地址相同。

**需要观察的现象 / 预期结果**：按 `operator==` 的实现，第一组**能匹配**（HCCS 分支只比 `commAddr.id`，不看 locType）；第二组不匹配（IP 不同）；第三组能匹配（地址类型与地址都相同）。若想在真机上验证，可运行 u1-l3 的 quickstart 并把 client 的 `local_endpoint` 描述改错一个字段，观察 server 日志出现 `Failed to match endpoint`（[endpoint_store.cc:L88](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/endpoint_store.cc#L88)）。真机行为：待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `StoreEndpoint` 要先 `Initialize` 成功后才把端点放进 map，而不是先入表再初始化？

**答案**：`Initialize` 调 `HcommEndpointCreate` 可能失败（如端点描述非法、底层资源不足）。先入表会把「未初始化成功的端点」暴露给 `MatchEndpoint`/`GetEndpoint` 的调用方，后续在其上建 Channel 会拿到空 `handle_` 而崩溃。先初始化后入表保证表内端点必然可用，失败时无需回滚清理（见 [endpoint_store.cc:L29-L35](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/endpoint_store.cc#L29-L35)）。

**练习 2**：`MatchEndpoint` 匹配失败返回什么错误码？这个错误码最终会被谁看到？

**答案**：`MatchEndpoint` 返回 `nullptr` 并打 `PARAM_INVALID` 日志；调用它的 server 消息处理（u4-l2 的 MatchEndpoint 消息分支）会把失败写进 `MatchEndpointResp.result` 经控制面 TCP 发回 client，client 的 `Connect` 因此失败。这与 u3-l3 讲的「匹配失败返回 `hixl::PARAM_INVALID`」一致。

**练习 3**：`Endpoint::Finalize` 里为什么销毁顺序是 Channel → 内存 → 端点，反过来行不行？

**答案**：不行。Channel 建立在端点之上、且其建链时（见 4.2）通过内存交换缓存了对端内存引用；若先 `HcommEndpointDestroy`，未销毁的 Channel 句柄就变成悬空引用；若先解注册内存，仍在工作的 Channel 上的传输会访问未注册地址。后建先拆是资源依赖的通用安全序。

### 4.2 Channel：数据面链路的创建与销毁

#### 4.2.1 概念说明

Channel 是两端之间真正承载单边读写的通路。它的创建有一个隐蔽但关键的约束：**两端的 HcommChannelCreate 必须使用相同的 channelName**，Hcomm 库靠这个名字把一端的 CLIENT 角色 socket 和另一端的 SERVER 角色 socket 配对。channelName 由「client 端点地址 + server 端点地址 + server 监听端口 + channel_index」拼成，四个成分都是两端共识信息，因此能独立计算出相同结果——这解释了 u4-l2 中 `channel_index` 为什么要由 server 原子计数器统一发放：它是 channelName 的组成部分之一。

`Channel` 类本身极薄：持有 `channel_handle_`，提供 Create/Destroy；真正有内容的是创建流程中的**状态轮询**——`HcommChannelCreate` 是异步的，返回后还要轮询 `HcommChannelGetStatus` 直到建链完成或失败。

#### 4.2.2 核心流程

```text
Endpoint::CreateChannel(channel_desc)
  │  1. 按 endpoint 位置选引擎: HOST→COMM_ENGINE_CPU, DEVICE→COMM_ENGINE_AICPU
  │  2. InitChannelDesc: 组装 HcommChannelDesc + 生成 channelName
  ▼
Channel::Create(ep_handle, ch_desc, engine, timeout_ms)
  │  3. HcommChannelCreate（异步发起）
  │  4. WaitChannelConnected: 轮询 ChannelGetStatus
  │        status==0 完成 → SUCCESS
  │        status==1 进行中 → 睡 1µs 再查，超 deadline 返回 TIMEOUT
  │        status==2/3 终态失败 → 返回 FAILED
  │  5. 失败则 ChannelDestroy 兜底清理
  ▼
channels_[channel_handle] = ChannelPtr（登记回 Endpoint 的 map）
```

#### 4.2.3 源码精读

**ChannelDesc 的字段**：

- [src/hixl/cs/channel.h:L28-L37](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/channel.h#L28-L37)：`ChannelDesc` 包含对端 `EndpointDesc`、RoCE 专用的 tc/sl/重试参数、角色类型（kClient/kServer，与 Hcomm 的 `HcommSocketRole` 对齐，见 [L23-L26](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/channel.h#L23-L26)）、进程级自增的 `channel_index` 和 `qos`。

**channelName 生成（两端一致性的核心）**：

- [src/hixl/cs/endpoint.cc:L40-L54](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/endpoint.cc#L40-L54)：`BuildChannelName` 先按本端角色把 client/server 端点排到固定位置（保证两端拼接顺序一致），再拼 `client_commAddr_server_commAddr_port_channel_index`，并校验长度不超过 `HCOMM_CHANNEL_NAME_MAX_LEN`。
- [src/hixl/cs/endpoint.cc:L56-L81](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/endpoint.cc#L56-L81)：`InitChannelDesc` 把 `ChannelDesc` 翻译成 Hcomm 的 `HcommChannelDesc`：写入角色、对端端点、`notifyNum=1`、`exchangeAllMems=true`（建链时自动交换两端已注册内存，对应 u4-l2 「server 区间来自建链导入」）；RoCE 协议额外填 tc/sl/重试/QP 数。

**创建与状态轮询**：

- [src/hixl/cs/channel.cc:L28-L44](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/channel.cc#L28-L44)：`WaitChannelConnected` 的轮询循环——注释明确列出状态码语义（0 完成 / 1 进行中 / 2、3 终态失败），遇到 2/3 立即返回 FAILED 不再等待；超时用 `steady_clock` 对 deadline 判断，轮询间隔仅 1 微秒（低延迟优先）。
- [src/hixl/cs/channel.cc:L47-L67](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/channel.cc#L47-L67)：`Channel::Create` 调 `HcommChannelCreate` 后等待连接，失败时**立即调 `ChannelDestroy` 兜底**并把句柄清零，避免泄漏半成品通道。
- [src/hixl/cs/channel.cc:L73-L79](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/channel.cc#L73-L79)：`Channel::Destroy` 直接调 `HcommChannelDestroy`。

**入口与登记**：

- [src/hixl/cs/endpoint.cc:L223-L255](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/endpoint.cc#L223-L255)：`Endpoint::CreateChannel`——按 `locType` 选引擎（HOST→CPU 引擎、DEVICE→AICPU 引擎，其他值报 `PARAM_INVALID`），组装描述后创建 Channel 并登记进 `channels_` map。这是 u4-l2 中 CreateChannel 消息在两端的最终落点：client 与 server **并发**各自调一次（client 侧调用见 [src/hixl/cs/hixl_cs_client.cc:L1234](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_client.cc#L1234)），靠相同 channelName 汇合。
- [src/hixl/cs/endpoint.cc:L257-L268](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/endpoint.cc#L257-L268)：`Endpoint::DestroyChannel` 按 handle 查表销毁；找不到（重复销毁）返回 `PARAM_INVALID`。

#### 4.2.4 代码实践

**实践目标**：手工推演 channelName 的两端一致性，理解为什么两端能「各自独立算出同一个名字」。

**操作步骤**：

1. 阅读 [endpoint.cc:L40-L54](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/endpoint.cc#L40-L54)，注意 `channel_desc.channel_type` 如何决定哪个端点排前。
2. 假设 client 端点 commAddr.id=0x11、server 端点 commAddr.id=0x22、server 监听端口 10000、channel_index=1，分别在「client 侧调用（角色 kClient）」和「server 侧调用（角色 kServer）」两个视角下写出 channelName 字符串。
3. 对照 u4-l2 的 [src/hixl/cs/conn_msg_handler.cc:L95-L126](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/conn_msg_handler.cc#L95-L126)，确认 `channel_index` 与 `remote_listen_port` 都来自 `MatchEndpointResp`——即 client 是从 server 学到的。

**需要观察的现象 / 预期结果**：两个视角算出的 channelName 完全相同（如 `..._..._10000_1` 的形式），因为 client/server 端点的排列由角色归一化、port 与 index 来自同一份 server 下发的应答。若想看真实值，可在真机跑 quickstart 并 grep 日志关键字 `channelName=`（[endpoint.cc:L79](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/endpoint.cc#L79)）。日志输出：待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`WaitChannelConnected` 的轮询间隔是 1 微秒，为什么敢用这么激进的间隔？超时是怎么保证的？

**答案**：轮询本身只是调 `HcommChannelGetStatus` 读一个状态值，开销极小；建链通常在毫秒级完成，粗粒度轮询会白白增加建链延迟。超时不用计数次数而是用 `steady_clock` 计算绝对 deadline（[channel.cc:L29-L43](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/channel.cc#L29-L43)），不受每次查询耗时不均的影响。

**练习 2**：如果 client 和 server 拼出的 channelName 不一致，会发生什么？哪些成分可能导致不一致？

**答案**：Hcomm 层无法把两端 socket 配对，`WaitChannelConnected` 会一直处于「进行中」直至超时返回 TIMEOUT。可能的不一致来源：两端对 client/server 端点排列不一致（代码用角色归一化避免了）、`port` 不同（client 从 `MatchEndpointResp` 学习 server 监听端口，避免了）、`channel_index` 不同（由 server 原子计数器统一发放，避免了）——三个成分各有专门的同步机制，这正是 u4-l2 讲 `channel_index` 发放机制的原因。

**练习 3**：`Endpoint::CreateChannel` 中 `CommEngine` 是如何决定的？为什么 HOST 端点不能走 AICPU 引擎？

**答案**：按 `endpoint_.loc.locType` 二选一：HOST→`COMM_ENGINE_CPU`、DEVICE→`COMM_ENGINE_AICPU`，其他 locType 直接 `PARAM_INVALID`（[endpoint.cc:L225-L233](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/endpoint.cc#L225-L233)）。HOST 端点没有关联的 AI Core/AICPU 资源，其数据面拷贝由本进程 CPU 线程完成（u4-l1 讲的 host 路径 ReadNbi/WriteNbiOnThread），故必须选 CPU 引擎。

### 4.3 TransferPool：设备传输资源槽位池

#### 4.3.1 概念说明

TransferPool 回答的问题是：**一次 device 端点的传输需要哪些运行时资源，如何高效复用**。直接动机：

- 每次传输若临时创建 ACL context、stream、Hcomm thread、notify，代价是毫秒级甚至更高，无法支撑高 QPS 的异步传输；
- Hcomm thread 是设备侧通信线程，创建后还需要在**设备内核侧登记上下文**（thread/notify/err_flag 三元组），销毁时同样要同步——这是一套昂贵的双向注册协议。

因此 TransferPool 采用**预分配 + 槽位租借**模型：初始化时一次性建好 `pool_size` 个 Slot（默认 128，上限 4096），每个 Slot 内含 `ctx/stream/thread/notify/err_flag` 全套资源；传输前 `Acquire` 取一个空闲槽，传输完成后 `Release` 归还；若传输以 FAILED/TIMEOUT 失败（失败闩锁触发），则走 `Abort`——销毁并**原地重建**槽内全部资源，防止被污染的 stream/notify 影响后续传输。

三个设计要点：

1. **per-device 单例注册表**：`GetInstance(device_id)` 用静态 map 保证每张卡一个池，server 与 client 共用（都调它，引用计数管理生命周期）。
2. **共享槽位（shared slot）**：client 侧多个并发传输可复用同一个槽（`active_slot_` + `shared_ptr` 引用计数），最后一个引用释放时才真正归还池。
3. **同步上下文内核 `HixlSyncTransferContext`**：新增/删除 thread 上下文不是简单调用，而是下发一个设备内核去改 AICPU 侧的状态表，且带 30 秒重试——DELETING 是中间态，需等内核确认。

#### 4.3.2 核心流程

**池与槽位的宏观生命周期**：

```text
首个使用者 (HixlCSServer/HixlCSClient 初始化)
  └─ GetInstance(device_id) ──► 新建 TransferPool
       └─ Initialize(pool_size=128 默认)
            ├─ aclrtCreateContext(rts_context_)          # 池级 RTS context
            ├─ EnsureDeviceKernelsLocked                  # 加载 HixlBatchGet/Put/SyncContext 内核
            ├─ EnsureErrFlagMemLocked                     # 按 SoC 分配 err_flag 块 (V2/V3 设备侧 / V5 主机侧)
            ├─ InitAllSlotsLocked                         # 逐槽: ctx→stream→thread→notify→err_flag
            └─ AddTransferContextsLocked                  # 内核登记所有 thread 上下文 (INITIALIZED)

每次传输 (client 侧):
  AcquireSharedSlot ──► pool->Acquire() 从 free_list_ 取槽  (或复用 active_slot_)
       │
       ▼
  LaunchDeviceChunkedKernels: desc 分块 (≤128/块) → 每块下发 HixlBatchGet/Put 内核
       │  (每 1920 条或最后一块插入 notify wait 完成感知)
       ▼
  传输结束 ReleaseDevCompleteHandle ──► ReleaseSharedSlotRef
       ├─ 正常: use_count==1 → 清 err_flag → pool->Release() → 槽回 free_list_
       └─ 失败闩锁: use_count==1 → pool->Abort() → 拆毁并原地重建该槽

最后一个使用者 Finalize() (ref_cnt 归零)
  └─ AbortInUseStreams → DeleteTransferContextsLocked(内核侧删除) → DeinitAllSlotsLocked
```

**单个槽位的状态视角**：

```text
            Acquire                       Release
  FREE ────────────────► IN_USE ────────────────────► FREE
   ▲                        │                          │
   │                        │ Abort (失败闩锁)          │
   │                        ▼                          │
   │              [abort: stream abort → notify reset  │
   │               → thread ctx 删除 → context 销毁     │
   │               → 重新 InitOneSlot + ctx 登记]        │
   └────────── 重建成功 ─────┘   重建失败 → 槽永久离线(in_use=false 但不回表)
```

#### 4.3.3 源码精读

**per-device 单例与引用计数**：

- [src/hixl/cs/transfer_pool.cc:L44-L63](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/transfer_pool.cc#L44-L63)：`GetInstance` 用静态 `unordered_map<int32_t, unique_ptr<TransferPool>>` 注册表，按 device_id 查找或创建。
- [src/hixl/cs/transfer_pool.cc:L80-L109](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/transfer_pool.cc#L80-L109)：`Initialize`——已初始化时只做 `ref_cnt_ += 1`（且要求 pool_size 一致否则 `PARAM_INVALID`）；首次初始化建 RTS context、初始化全部资源，`HIXL_DISMISSABLE_GUARD` 保证失败回滚。
- [src/hixl/cs/transfer_pool.cc:L143-L160](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/transfer_pool.cc#L143-L160)：`Finalize` 只减引用，归零才真正拆池；拆池前先 `AbortInUseStreamsLocked` 中止仍在用的 stream。

谁在调用？client 侧 [src/hixl/cs/hixl_cs_client.cc:L300-L313](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_client.cc#L300-L313) 与 server 侧 [src/hixl/cs/hixl_cs_server.cc:L175-L187](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_server.cc#L175-L187) 都以 `global_config_.MaxActiveChannels().value_or(kDefaultTransferPoolSize)` 初始化，默认值 128（[hixl_cs_client.cc:L37](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_client.cc#L37)，server 侧同值并注释「与 HixlCSClient 侧设备池大小一致」）。同卡上 server+client 各持一份引用，先退出的那方 `Finalize` 只减计数——这就是引用计数存在的意义。

**槽位的数据结构**：

- [src/hixl/cs/transfer_pool.h:L64-L75](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/transfer_pool.h#L64-L75)：私有 `Slot` 结构——`in_use`、`ctx`、`stream`、`thread`（Hcomm 通信线程句柄）、`notify`/`notify_id`/`notify_addr`/`notify_len`（完成感知信号量及其设备地址）、`err_flag_host_addr`/`err_flag_dev_addr`（每槽 1 字节错误标志）。
- [src/hixl/cs/transfer_pool.h:L32-L45](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/transfer_pool.h#L32-L45)：对外租借形态 `SlotHandle`——Slot 全字段的值拷贝加 `device_id`/`slot_index`，使用者不回指池内部，归还时凭这两个 ID 定位。
- [src/hixl/cs/transfer_pool.cc:L283-L290](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/transfer_pool.cc#L283-L290)：`InitOneSlotLocked` 的初始化次序：context → 默认 stream（并设 `ACL_STOP_ON_FAILURE` 失败模式）→ Hcomm thread → notify → 挂接 err_flag 字节。依赖关系决定次序：stream 属于 context，notify_addr 解析依赖 notify。

**Acquire / Release / Abort 三条路径**：

- [src/hixl/cs/transfer_pool.cc:L162-L180](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/transfer_pool.cc#L162-L180)：`Acquire` 从 `free_list_`（deque）头部取下标、标记 `in_use`、填 `SlotHandle` 返回；池空返回 `RESOURCE_EXHAUSTED`。
- [src/hixl/cs/transfer_pool.cc:L182-L203](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/transfer_pool.cc#L182-L203)：`Release` 做防御性校验（device_id 匹配、下标合法、确在 in_use）后清 err_flag、归还 free_list。重复 Release 只告警不崩溃。
- [src/hixl/cs/transfer_pool.cc:L303-L319](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/transfer_pool.cc#L303-L319)：`AbortSlotByIndexLocked` 的四步：中止 stream 运行时（`aclrtStreamAbort`）→ 重置 notify → 经同步上下文内核删除 thread 上下文并 `HcommThreadFree` → 销毁 context；然后 `ReinitSlotAfterAbortLocked`（[L388-L403](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/transfer_pool.cc#L388-L403)）原地重建槽并重新登记上下文，成功才回 free_list；重建失败则槽离线（不再归还）。

**err_flag：按 SoC 世代二选一的映射策略**：

- [src/hixl/cs/transfer_pool.cc:L582-L615](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/transfer_pool.cc#L582-L615)：`EnsureErrFlagMemLocked` 分配 `pool_size` 字节的错误标志块——V2/V3 走 `AllocErrFlagDeviceMappedLocked`（先分配设备内存再向主机映射），V5 走 `AllocErrFlagHostMappedLocked`（先分配锁页主机内存再向设备映射），其他 SoC 跳过。两条路径里 HostRegister 失败都只是降级（无映射继续跑），不视为致命错误。
- [src/hixl/cs/transfer_pool.cc:L645-L653](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/transfer_pool.cc#L645-L653)：`AssignSlotErrFlagLocked` 给每个槽切 1 字节：内核侧传输出错时置位 `err_flag_dev_addr`，host 侧轮询 `err_flag_host_addr` 感知失败（配合 u4-l1 的失败闩锁）。

**同步上下文内核**：thread 上下文在 AICPU 侧的状态（INITIALIZED/DELETING/DELETED）必须由设备内核改写：

- [src/hixl/cs/transfer_pool.cc:L679-L728](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/transfer_pool.cc#L679-L728)：`LaunchSyncContextKernelLocked` 把 entry 列表（thread/op/notify_id/err_flag）拷入设备内存，启动 `HixlSyncTransferContext` 内核（10 秒超时同步），再拷回每条的状态。
- [src/hixl/cs/transfer_pool.cc:L730-L756](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/transfer_pool.cc#L730-L756)：`SyncContextsLocked` 的重试循环——状态为 DELETING（中间态）的条目每 100ms 重发一次，30 秒 deadline；超时后若 op 是 DELETE 则打事件日志并**强制视为成功**（[L796-L808](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/transfer_pool.cc#L796-L808)），若 op 是 ADD 则返回 TIMEOUT。删除可以「宽容超时」（thread 反正要释放），新增不能（thread 马上要用）。
- [src/hixl/cs/transfer_pool.cc:L810-L817](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/transfer_pool.cc#L810-L817)：池初始化时 `AddTransferContextsLocked` 批量登记全部槽，销毁时 `DeleteTransferContextsLocked` 批量删除。

**传输如何消费槽位（client 侧）**：

- [src/hixl/cs/hixl_cs_client.cc:L628-L649](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_client.cc#L628-L649)：`AcquireSharedSlot`——已有活跃槽直接共享（`shared_ptr` 引用计数 +1，日志可见 ref_before/ref_after），否则从池 `Acquire` 新槽并存为 `active_slot_`。
- [src/hixl/cs/hixl_cs_client.cc:L651-L683](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_client.cc#L651-L683)：`ReleaseSharedSlotRef`——最后一个引用（`use_count()==1`）释放时清 err_flag，然后**按失败闩锁分叉**：正常走 `pool->Release`，闩锁开启走 `pool->Abort`。
- [src/hixl/cs/hixl_cs_client.cc:L720-L734](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_client.cc#L720-L734)：`LaunchDeviceChunkedKernels`——desc 列表按 `kMaxKernelBatchSize=128`（[L50](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_client.cc#L50)）分块，每 1920 条（`kNotifyWaitTaskInterval`，[L51](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_client.cc#L51)）或最后一块插入一次 notify wait——即完成感知不是每块一次，而是周期性插入。
- [src/hixl/cs/hixl_cs_client.cc:L767-L793](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_client.cc#L767-L793)：`BuildDeviceChunkParam` 把槽位资源注入内核参数：`param.thread` 来自槽位、`param.channel` 是 4.2 建立的 channel 句柄、完成感知时填 `remote_flag_addr`（server 的 trans_flag）与本槽的 `notify_addr/notify_id`；HCCS 协议额外置 `use_notify_record=1`。

#### 4.3.4 代码实践（本讲综合实践预演）

**实践目标**：梳理一条传输请求进入 TransferPool 后的完整生命周期（入队、执行、完成、回收），画出状态流转图。

**操作步骤**：

1. 以 [hixl_cs_client.cc:L628-L649](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_client.cc#L628-L649)（AcquireSharedSlot）为起点，向上找到它的调用方（设备传输入口函数内），向下依次阅读：`AllocateDeviceDescBuf`（desc 拷入设备）→ `LaunchDeviceChunkedKernels`（分块下发内核）→ `ReleaseDevCompleteHandle`（[L600-L625](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_client.cc#L600-L625)）→ `ReleaseSharedSlotRef`（[L651-L683](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_client.cc#L651-L683)）。
2. 对每个环节记下：输入是什么、用的槽位哪个字段、失败时走哪条分支。
3. 特别注意两个分叉点：
   - `ReleaseSharedSlotRef` 里 `use_count()==1` 的判断——共享槽何时才真正归还；
   - `transfer_failure_latched_` 的判断——Release 与 Abort 的分野。
4. 把上述过程画成一张状态图（参考 4.3.2 中的两张图核对），节点至少包含：FREE → IN_USE（可能被多个 handle 共享）→ RELEASE 归还 / ABORT 重建 / ABORT 重建失败离线。

**需要观察的现象 / 预期结果**：

- 若在真机（CANN 环境）运行 u1-l3 的 quickstart，开 `HIXL_LOGI` 级日志可看到 `[HixlClient] Acquired new slot. slot_index=N`、`Reusing active slot ... ref_before/ref_after`、`Released slot to pool` / `Aborted slot on last ref after latched failure` 成对出现——正常路径只有 Acquire/Release，Abort 只在人为注入错误（如 kill 对端）后出现。日志输出：待本地验证。
- 源码推演（无硬件也可完成）：一张包含「入队（Acquire/共享）→ 执行（内核分块 + notify wait）→ 完成（host_flag/notify 感知）→ 回收（Release 或 Abort）」四阶段的状态图，其中回收阶段标注双路径。

**预期结果**：能独立复述「一条 TransferAsync 从拿槽到还槽」的全链路，并指出失败闩锁在哪一行改变了回收路径。

#### 4.3.5 小练习与答案

**练习 1**：为什么 server 和 client 在同一张卡上各自 `Initialize` TransferPool 却只会有一个池实例？`pool_size` 不一致会发生什么？

**答案**：`GetInstance` 的静态注册表按 device_id 去重，第二个使用者拿到同一个指针；`Initialize` 发现 `inited_` 已为真时只增加 `ref_cnt_`，但若传入的 `pool_size` 与首次不同则返回 `PARAM_INVALID`（[transfer_pool.cc:L86-L97](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/transfer_pool.cc#L86-L97)）。两侧默认值统一为 128（server 侧注释明确「与 HixlCSClient 侧设备池大小一致」），若用户通过 `global_config_.MaxActiveChannels()` 配置，两端配置不同会直接初始化失败。

**练习 2**：`Release` 和 `Abort` 都能把槽还给池，为什么不统一用 `Abort`（更彻底）或者统一用 `Release`（更便宜）？

**答案**：统一 `Release` 不安全——失败的传输可能污染槽内 stream（有未完成的异常任务）和 notify（有未消费的信号），直接复用会让下一个使用者的完成感知错乱；统一 `Abort` 太贵——每次传输都要销毁并重建 context/stream/thread 并与设备内核同步两次（DELETE+ADD 各最多 30 秒重试窗口）。因此用失败闩锁区分：正常路径 `Release`（只清 1 字节 err_flag），失败路径 `Abort` 原地重建（[hixl_cs_client.cc:L671-L680](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_client.cc#L671-L680)）。

**练习 3**：`HandleSyncContextTimeout` 里为什么 DELETE 超时可以「强制成功」而 ADD 超时必须返回 TIMEOUT？

**答案**：DELETE 的目标是释放 thread，即使 AICPU 侧状态还停留在 DELETING，后续 `HcommThreadFree` 仍会执行，残留状态只影响那个即将废弃的 thread，不值得为它让整个销毁流程失败——所以打事件日志后视为成功（[transfer_pool.cc:L796-L808](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/transfer_pool.cc#L796-L808)）。ADD 则相反：thread 马上要被传输内核使用，上下文未登记成功就放行，后续内核下发会踩到未初始化的 AICPU 上下文，必须以 TIMEOUT 失败并走回滚。

**练习 4**：`AcquireSharedSlot` 里「共享活跃槽」意味着多个并发传输复用同一个 stream/thread，这为什么是安全的？

**答案**：设备侧传输以内核任务的形式异步下发给同一个 stream，ACL stream 本身保证任务**按下发顺序**执行；完成感知用同一 notify 周期性插入（每 1920 条 desc 一次），多个 handle 各自等自己的查询点即可。槽位的引用计数（`shared_ptr` 的 `use_count`）保证最后一个 handle 归还前槽不会被回收（[hixl_cs_client.cc:L628-L683](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_client.cc#L628-L683)）。代价是任一传输失败会闩锁并 Abort 整个槽，殃及同槽的其他传输——这是用「隔离性」换「资源效率」的取舍。

## 5. 综合实践

把三个模块串成一条线：**从 MatchEndpoint 到一次设备传输的资源闭环**。

任务：编写一份「CS 数据面资源清单」文档，包含三张图和一组结论：

1. **端点匹配图**：以 u1-l3 quickstart 为场景，画出 server 启动时 EndpointGenerator 产出 N 个 EndpointDesc → `EndpointStore::CreateEndpoint` 建立 N 个端点 → client MatchEndpoint 请求按 `operator==` 命中其一的流程，标注匹配比较字段（HCCS→id、UB→EID、RoCE→IP）。
2. **建链配对图**：画 client/server 两侧并发的 `Endpoint::CreateChannel`，标注 channelName 四个成分的来源（两端 commAddr、server 监听 port、server 发放的 channel_index），以及 `WaitChannelConnected` 的状态码（0/1/2/3）与超时路径。
3. **槽位生命周期状态图**（4.3.4 实践的产出）：FREE → IN_USE（共享）→ Release/Abort/Abort 失败离线，标注失败闩锁分叉点的源码行号。
4. **结论**：回答一个综合问题——「如果把 `MaxActiveChannels` 从 128 调成 8，同时并发跑 16 条异步传输，会发生什么？」提示：从 `Acquire` 的 `RESOURCE_EXHAUSTED`（[transfer_pool.cc:L167-L171](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/transfer_pool.cc#L167-L171)）和共享槽机制两个角度分析：共享槽使并发传输**可能**挤在少数槽上而不报错，但跨槽并行的并行度受池大小限制。

本实践纯源码阅读即可完成；若在真机环境，可补充运行 quickstart 抓取 `slot_index` 相关日志验证状态图（待本地验证）。

## 6. 本讲小结

- **EndpointStore** 是 server 侧端点台账（`map<EndpointHandle, EndpointPtr>`），`MatchEndpoint` 用按协议定制的 `operator==`（HCCS 比 id、UB 比 EID、RoCE 比 IP）把 client 的描述翻译成句柄，匹配失败报 `PARAM_INVALID`；`Endpoint` 同时托管内存登记（含 UBOE/UBG/UBC_CTP 的 host VA 映射分支）与 Channel 表。
- **Channel** 创建的两难由 channelName 解决：`client_commAddr + server_commAddr + server_port + channel_index` 四个两端共识成分拼出唯一名字，两端并发 `HcommChannelCreate` 后由 Hcomm 配对；`WaitChannelConnected` 以 1µs 间隔轮询，状态 2/3 为终态失败、超时返回 TIMEOUT。
- **TransferPool** 是 per-device 单例的槽位池（默认 128 槽、上限 4096、引用计数管理 server/client 共享生命周期），每槽预置 ctx/stream/Hcomm thread/notify/err_flag 全套资源；thread 上下文的增删要经 `HixlSyncTransferContext` 设备内核同步，DELETING 中间态 100ms 重试、30 秒 deadline。
- 槽位回收双路径：正常 `Release`（清 err_flag 回表），失败闩锁触发 `Abort`（中止 stream → 重置 notify → 内核删上下文 → 销毁 context → 原地重建，重建失败则槽永久离线）。
- client 的**共享槽**机制让多个并发传输复用 `active_slot_`（shared_ptr 引用计数），最后一个引用释放时才真正归还池——资源效率与故障隔离的折中。
- 设备传输内核按 128 条 desc 分块下发，每 1920 条插入一次 notify wait 完成感知；内核参数中的 thread/channel/notify 全部来自本讲的槽位与通道。

## 7. 下一步学习建议

本讲补齐了 CS 数据面的三大构件，u4 单元只剩最后一块拼图：

- **u4-l4 CS 全局配置、Notify 解析与内核加载**：本讲多处埋了引子——`global_config_.MaxActiveChannels()`（池大小配置）、`NotifyAddrResolver::Resolve`（槽位 notify_addr 从哪来）、`LoadDeviceKernelAndGetHandles`（HixlBatchGet/Put 内核如何加载）——都将在下一讲展开。建议先重读本讲的 [transfer_pool.cc:L242-L255](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/transfer_pool.cc#L242-L255) 与 [L663-L677](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/transfer_pool.cc#L663-L677) 带着问题过去。
- 若想横向对照「另一个槽位池实现」，可预习单元五的 `src/hixl/fabric_mem/fabric_mem_slot_pool.cc`（FabricMem 模式的 SlotPool），比较两者在资源种类与回收策略上的差异。
- 若对设备内核侧（HixlBatchGet/Put 的 AICPU 实现）感兴趣，可用 `Grep` 搜索 `HixlOneSideOpParam` 的定义位置，追踪内核侧如何消费本讲注入的 thread/notify/channel 参数。

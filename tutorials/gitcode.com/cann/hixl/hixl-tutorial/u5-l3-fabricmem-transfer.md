# FabricMem 传输服务：host 与 aicpu 路径

## 1. 本讲目标

上一讲（u5-l2）我们打通了 FabricMem 的内存底座：VMM 虚拟内存、共享句柄交换与 AsyncSlot 槽位池。本讲沿着数据继续往下走，回答三个问题：

1. 一次 FabricMem 传输请求从 `TransferSync`/`TransferAsync` 进入后，由谁负责搬到设备上？——答案是「传输服务」，而且有 **host** 和 **AICPU** 两条互斥的实现路径。
2. 通道（Channel）是如何建立、查找、保活和销毁的？——`FabricMemChannelManager` 是唯一管理者。
3. AICPU 路径里，描述符如何被拆分、打包、下发到 A3 的 RTSQ 队列？——`FabricMemAicpuDispatcher` 负责这最后一公里。

学完本讲，你应该能画出 FabricMem 传输的完整组件交互图，并能根据配置判断一次传输走的是哪条路径。

## 2. 前置知识

- **控制流（control stream）与数据流**：昇腾 ACL 中 `aclrtStream` 是设备上的任务队列。host 路径直接在控制流上发 `aclrtMemcpyAsync` 拷贝指令；AICPU 路径则在控制流上只发一个「AICPU 内核启动」，真正的 SDMA 搬运指令由 AICPU 内核写进另一条 RTSQ 队列。
- **RTSQ / SDMA / SQE**：A3 芯片上 AICPU 可以直接向一个 RTSQ（Receive/Submission Transfer Queue）写入 SDMA（System DMA）SQE（Submission Queue Entry），即「AICPU unfold（展开）」——把通信栈从 AICPU 上的完整协议栈精简为裸队列操作。SQE 中的 task_id 是 16 位，需要主机侧维护计数器分配。
- **notify**：设备侧完成信号（`aclrtNotify`）。AICPU 内核在搬运完成后向 notify 发 NotifyRecord，主机用 `aclrtWaitAndResetNotify` 等待，从而感知「这一批搬完了」。
- **host flag（完成标志）**：一块 8 字节锁页主机内存。设备把常量 1 写进来（D2H 拷贝），主机轮询这个 volatile 值即可零开销判断异步传输是否完成——比每次调用 `aclrtStreamQuery` 便宜。
- **读写锁 `std::shared_mutex`**：共享锁允许多个传输并行提交；断链时取独占锁排空（drain）所有在途提交。这是 host 路径并发模型的核心。
- **VMM 地址翻译**：上一讲讲过，远端共享句柄导入后会得到「新 VA」，`TransferOpDesc` 里用户给的是「旧 VA」，传输前必须查表翻译。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/hixl/fabric_mem/fabric_mem_transfer_service.h/.cc` | 传输服务抽象基类：持有槽位池与通道管理器，提供地址翻译、host flag、流查询等公共能力 |
| `src/hixl/fabric_mem/fabric_mem_host_transfer_service.cc` | host 路径实现：直接用 `aclrtMemcpyAsync` 提交拷贝，读写锁并发模型 |
| `src/hixl/fabric_mem/fabric_mem_aicpu_transfer_service.cc` | AICPU 路径实现：经 dispatcher 下发 AICPU 内核，per-channel 绑定槽位模型 |
| `src/hixl/fabric_mem/fabric_mem_channel_manager.h/.cc` | 通道管理器：Connect/Disconnect、通道台账、keepalive 保活、请求路由 |
| `src/hixl/fabric_mem/fabric_mem_aicpu_dispatcher.h/.cc` | AICPU 下发器：描述符拆分、内核启动、RTSQ 参数构造、TransferContext 同步 |
| `src/hixl/fabric_mem/fabric_mem_aicpu_types.h` | host 与 AICPU 二进制共享的 ABI：描述符、内核参数、资源结构 |
| `src/hixl/fabric_mem/fabric_mem_types.h` | `AsyncSlot`/`AsyncRecord` 等内部数据结构 |
| `src/hixl/engine/fabric_mem_engine.cc` | 引擎入口：按配置二选一创建传输服务实例 |

## 4. 核心概念与源码讲解

### 4.1 FabricMemTransferService：两条路径的公共底座

#### 4.1.1 概念说明

`FabricMemTransferService` 是传输服务的抽象基类。它做两件事：

1. **定义接口**：`TransferSync`/`TransferAsync`/`GetTransferStatus`/`CleanupAsyncTransfer` 四个纯虚接口，与 HIXL 公开 API 的传输组一一对应；建链组（Connect/Disconnect 等）则直接实现为对通道管理器的转发。
2. **收拢公共逻辑**：槽位池初始化、地址翻译、host flag 读写、流状态查询、统计上报——这些 host/AICPU 两条路径都要用的能力全部放在基类，子类只实现「怎么把数据搬上设备」这一处差异。

选择哪个子类由初始化参数决定，见 `FabricMemTransferServiceInitParam`：

[src/hixl/fabric_mem/fabric_mem_transfer_service.h:32-45](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_transfer_service.h#L32-L45)

这段结构体定义了传输服务的全部初始化输入。注意第 43 行注释：`enable_aicpu_unfold` 为 true 时选 `FabricMemAicpuTransferService`，否则选 `FabricMemHostTransferService`；同时 `task_stream_num` 在 AICPU 模式下必须为 1。

引擎侧的决策点在 `FabricMemEngine::InitTransferService`：

[src/hixl/engine/fabric_mem_engine.cc:71-91](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/fabric_mem_engine.cc#L71-L91)

这里把引擎配置打包成 `FabricMemTransferServiceInitParam`，并用一个简单的 if/else 创建 AICPU 或 host 传输服务实例——这就是两条路径的唯一分叉点。

#### 4.1.2 核心流程

`InitCommon`（两条路径共用的初始化）流程：

```text
校验参数（device_id、max_stream_num、task_stream_num、statistic、local_memory）
计算每个槽位消耗的流数：
    host 路径:  streams_per_slot = task_stream_num
    AICPU 路径: streams_per_slot = task_stream_num * 2   ← 控制流 + RTSQ 工作流成对
max_async_slot_num = max_stream_num / streams_per_slot
初始化 slot_pool_（容量 = max_async_slot_num）
初始化 dev_const_one_（设备上的常量 1，供 host flag 置位用）
初始化 channel_manager_（传入 slot_pool/control_server/aclrt_context 等）
```

析构与 `Finalize` 的顺序是刻意的：先 `channel_manager_.Finalize()`（会中止在途传输并释放槽位），再 `slot_pool_.AbortAndDestroyAll()`。头文件里成员声明顺序也保证了这一点：

[src/hixl/fabric_mem/fabric_mem_transfer_service.h:128-131](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_transfer_service.h#L128-L131)

注释说明：`slot_pool_` 必须声明在 `channel_manager_` 之前，C++ 成员按声明逆序析构，因此槽位池比管理器活得更久——管理器的 keepalive/断链路径还要把槽位还回池里。

#### 4.1.3 源码精读

**（1）AICPU 槽位的流开销翻倍**：

[src/hixl/fabric_mem/fabric_mem_transfer_service.cc:95-99](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_transfer_service.cc#L95-L99)

一个 AICPU 槽位为每条控制流配一条「仅设备侧的 RTSQ 工作流」，所以耗的流数是 host 槽位的两倍，槽位池容量要按此折算。

**（2）传输前的地址翻译**：

[src/hixl/fabric_mem/fabric_mem_transfer_service.cc:255-272](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_transfer_service.cc#L255-L272)

`ResolveTransferAddrs` 对每个 op 做两件事：远端地址经 `TransOpAddr` 在「新 VA → 旧 VA」表中查区间翻译（找不到说明地址未注册，返回 `PARAM_INVALID`——这就是 FabricMem 版的「未注册地址不能传输」安全闸）；若本端是主机内存（用 `aclrtPointerGetAttributes` 判断），再让 `local_memory_->TranslateLocalHostOpAddrs` 把本端地址也换成 fabric 视角 VA。

区间包含判定 `IsRangeContained` 处理了偏移上溢：先算 `offset = old_addr - base`，再用 `len <= size - offset` 避免 `offset + len` 回绕。

**（3）host flag 完成轮询**：

[src/hixl/fabric_mem/fabric_mem_transfer_service.cc:192-217](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_transfer_service.cc#L192-L217)

`AppendHostFlagCopies` 在每条流末尾追一条「设备常量 1 → 主机 flag」的 D2H 异步拷贝；`AllHostFlagsDone` 逐个 volatile 读 flag，全为 1 后再插一道 acquire 内存栅栏，保证此前的 D2H DMA 写（包括数据本体）对当前线程可见。异步完成判定因此变成「读一个主机内存变量」，代价极低。

#### 4.1.4 代码实践

**实践目标**：确认两条路径在初始化参数上的差异约束。

1. 打开 `src/hixl/fabric_mem/fabric_mem_transfer_service.cc`，记录 `InitCommon` 中 `streams_per_slot` 在 `enable_aicpu_unfold=true/false` 时的取值。
2. 打开 `src/hixl/fabric_mem/fabric_mem_aicpu_transfer_service.cc` 的 `Initialize`（L34-35）和 `src/hixl/engine/fabric_mem_engine.cc` 的 `ApplyFabricMemoryOptions`（L161-173），记录两处对 `task_stream_num` 的校验。
3. 假设 `max_stream_num=32`、`task_stream_num=1`，手算两种模式下 `max_async_slot_num` 各是多少（host：32；AICPU：16）。

**需要观察的现象 / 预期结果**：这是一道纯源码阅读练习，无需硬件。预期你会发现「同样 32 条流，AICPU 模式可并发的槽位只有 host 模式的一半」——这是理解后续 per-channel 绑定槽位设计的伏笔。

#### 4.1.5 小练习与答案

**练习 1**：`FabricMemTransferService` 的建链接口（Connect 等）为什么不需要做成虚函数？
**答案**：建链逻辑与传输路径无关——两条路径都只是转发给 `channel_manager_`（见 `fabric_mem_transfer_service.cc:135-165`），差异只存在于「数据怎么搬」，所以基类直接实现即可。

**练习 2**：`TransferReq` 句柄里装的是什么？
**答案**：一个自增的 `req_id`（`next_req_id_`），经 `reinterpret_cast` 装进 `void*`。它不是指针，而是到通道管理器 `req_2_channel_` 路由表的查表键。

**练习 3**：为什么 `AllHostFlagsDone` 里 `slot.host_flags.empty()` 要返回 false？
**答案**：没有 flag 意味着没有可判定的完成证据（可能是异常路径构造的槽位），宁可认为「未完成」让调用方走流查询兜底，也不能凭空宣布完成。

### 4.2 FabricMemChannelManager：通道的建立、路由与保活

#### 4.2.1 概念说明

FabricMem 模式下「通道（Channel）」不再对应 CS 路径的 Hcomm 队列，而是一个纯软件对象：**一条到远端 engine 的逻辑连接**，核心内容是导入后的远端内存（`FabricMemRemoteMemory`）和一个 keepalive TCP fd。通道管理器维护 `remote_engine 字符串 → channel` 的台账，并向传输路径提供：

- **建链**：从远端控制面拉取共享句柄，导入为本端可 DMA 的内存；
- **传输上下文**：`BuildTransferContext` 一次性取出地址翻译表与统计句柄；
- **请求路由**：`req_id → channel`，供 `GetTransferStatus` 定位记录；
- **保活**：周期心跳，失联自动断链（auto_connect 开启时）。

#### 4.2.2 核心流程

建链（`Connect` → `FetchAndInstallRemote`）：

```text
快速检查 channels_（持 channels_mutex_）：已存在 → ALREADY_CONNECTED
connect_mutex_ 串行化 Fetch：
    FabricMemControlClient::Fetch   ← 控制面 TCP 拉取 ShareHandleInfo 列表 + keepalive fd
    channels_mutex_ 下二次检查（防并发重复安装）
    Import(share_handles) → FabricMemRemoteMemory
    装配 FabricMemChannel，登记 channels_[remote]，注册统计通道
```

注意锁纪律：**网络 I/O 期间不持有 `channels_mutex_`**（头文件注释 L118-119 明文规定），否则一个慢对端会卡住所有通道操作。

断链（`DisconnectRemote`）按两条路径分流中止在途传输：

```text
channel->disconnecting = true            ← 原子置位，新传输快速失败
enable_aicpu_unfold ?
    是：持 transfer_mu → AbortAicpuChannelLocked（排空整条通道）
    否：持 submit_gate 独占锁（drain）→ 摘走全部 async_records
        → AbortSlotStreams 中止同步槽 → 释放槽位、撤销路由
之后：控制面发送 Disconnect、关闭 keepalive fd、RemoveChannelEntryLocked
```

#### 4.2.3 源码精读

**（1）Channel 数据结构——两种并发模型并存于一个结构体**：

[src/hixl/fabric_mem/fabric_mem_channel_manager.h:34-56](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_channel_manager.h#L34-L56)

`FabricMemChannel` 的字段分成两组：host 路径用 `submit_gate`（读写锁）+ `active_sync_slots`；AICPU 路径用 `transfer_mu`（互斥锁）+ `bound_slot`（每通道一个绑定槽位，流水化提交共享）。头文件注释写明了各自的语义与「两种锁绝不同时持有」的禁令。

**（2）锁次序规则**：

[src/hixl/fabric_mem/fabric_mem_channel_manager.h:118-129](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_channel_manager.h#L118-L129)

host 路径锁序 `submit_gate → req_route_mutex_/records_mutex`，AICPU 路径锁序 `transfer_mu → records_mutex/req_route_mutex/pool_mutex_`，且反向与混合均被禁止。这是阅读两条路径所有并发代码的「宪法」。

**（3）建链与远端内存安装**：

[src/hixl/fabric_mem/fabric_mem_channel_manager.cc:101-127](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_channel_manager.cc#L101-L127)

`FetchAndInstallRemote` 是 `Connect` 与 `EnsureConnected` 的公共实现，唯一差异是「发现已连接时返回什么」：前者返回 `ALREADY_CONNECTED`（幂等告警），后者返回 `SUCCESS`（EnsureConnected 本就只求「在」）。`HIXL_DISMISSABLE_GUARD` 保证失败路径关闭 keepalive fd。

**（4）keepalive 心跳**：

[src/hixl/fabric_mem/fabric_mem_channel_manager.cc:419-456](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_channel_manager.cc#L419-L456)

`SendOutboundHeartbeats` 的工程细节很值得学：先在锁内对每个 keepalive fd `dup()` 一份快照再出锁发送——dup 共享同一打开文件描述，即便并发的 Disconnect 关掉了原 fd，发送也不会写到已被复用的 fd 号上；慢对端不会拖住 Disconnect。心跳失败且开启 auto_connect 时自动调用 `DisconnectDeadRemote` 清理失活通道。默认检查间隔 10 秒（`kDefaultKeepaliveCheckIntervalMs`，L24）。

#### 4.2.4 代码实践

**实践目标**：追踪一次 `Connect` 的完整调用链。

1. 从 `FabricMemTransferService::Connect`（`fabric_mem_transfer_service.cc:135-137`）出发，进入 `FabricMemChannelManager::Connect`。
2. 记录沿途经过的锁：`channels_mutex_`（快速检查）→ `connect_mutex_`（Fetch 串行化）→ `channels_mutex_`（安装）。
3. 找到 `FabricMemControlClient::Fetch` 的声明（`fabric_mem_control.h`），确认它返回的两样东西：`share_handles` 和 `keepalive_fd`。
4. 画出时序图：本端 Connect → 控制面 TCP → 对端 ControlServer（上讲的 provider 回调在这里把 `GetShareHandles()` 交给请求方）→ 本端 Import → 通道入表。

**需要观察的现象 / 预期结果**：源码阅读型实践，无需硬件。预期产出一张包含「用户线程、控制面 TCP、对端 server」三个泳道的时序图，并标注三把锁各自守护的临界区。运行行为待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `EnsureConnected` 存在，而不让调用方都直接用 `Connect`？
**答案**：`Connect` 对「已连接」返回 `ALREADY_CONNECTED`（非 SUCCESS），传输前隐式建链（auto_connect）需要一个「已在就算成功」的语义，避免把正常情况当错误上报。

**练习 2**：`AbortAndClearHostChannelRecords` 为什么要先取 `submit_gate` 独占锁再摘记录？
**答案**：独占锁会等所有正在提交的传输退出（drain）。拿到锁之后，任何传输要么已完成提交并登记了槽位/记录（会被这里的中止覆盖），要么还没开始（会被 `disconnecting` 标志拒绝）——保证「先中止、再解除内存映射」不留悬挂流。

**练习 3**：`req_2_channel_` 路由表的生命周期和 `async_records` 有什么区别？
**答案**：`async_records` 挂在 channel 上，随传输完成/中止摘除；`req_2_channel_` 是全局表，请求终态后由 `RemoveReqRoute` 清除。断链会把两者一并清空，之后 `GetTransferStatus` 返回 `PARAM_INVALID`（请求不存在）。

### 4.3 FabricMemHostTransferService：host 路径

#### 4.3.1 概念说明

host 路径是「朴素但并行」的实现：把每个 `TransferOpDesc` 直接翻译成一条 `aclrtMemcpyAsync`（ACL_MEMCPY_DEVICE_TO_DEVICE——因为两端地址都已是 VMM 统一编址的设备视角 VA），轮流撒到槽位的多条流上。它的特点是：

- 每次传输从池里**独占借一个槽位**，用完归还——传输之间完全独立；
- 同一通道上多个传输持 `submit_gate` **共享锁**并行提交；
- 完成判定优先用 host flag 轮询，兜底 `aclrtStreamQuery`/`aclrtSynchronizeStream`。

#### 4.3.2 核心流程

同步传输：

```text
PrepareChannelTransfer     → 取 channel + 构造传输上下文（地址翻译表/统计句柄）
slot_pool_.AcquireWithTimeout
IssueSyncCopy:
    disconnecting 快速检查（无锁）
    ResolveTransferAddrs（锁外，只读私有快照）
    取 submit_gate 共享锁 → 二次检查 disconnecting
    登记 slot 到 channel->active_sync_slots
    ProcessCopyWithAsync：逐 op 下发 aclrtMemcpyAsync
WaitControlStreamsWithTimeout   → 按剩余超时逐流同步
UnregisterSyncSlot + Release + 统计上报
```

异步传输在提交后多做两件事：`AppendHostFlagCopies` 追加完成标志拷贝；把 slot 与统计信息打包成 `AsyncRecord` 登记进 `channel->async_records` 并登记路由——注意登记发生在**提交之后、释放共享锁之前**，这个窗口保证并发断链必然能看到该记录。

`GetTransferStatus` 的完成判定是三级火箭：

```text
持 records_mutex 定位记录
  ① AllHostFlagsDone？→ 是：直接 CompleteAsyncTransferAndUpdateStats（跳过流同步）
  ② 否则 aclrtStreamQuery 全部流 → WAITING 则返回「还在跑」
  ③ 流查询异常 → HandleAsyncStreamQueryFailure（释放槽位、status=FAILED）
终态时摘记录 + RemoveReqRoute
```

#### 4.3.3 源码精读

**（1）拷贝下发——host 路径的心脏**：

[src/hixl/fabric_mem/fabric_mem_host_transfer_service.cc:274-298](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_host_transfer_service.cc#L274-L298)

ops 轮流分配到 `slot.streams` 上（round-robin），WRITE 把 local 拷到 remote、READ 反向，全部标 `ACL_MEMCPY_DEVICE_TO_DEVICE`。这就是 host 路径的全部搬运逻辑——没有内核、没有描述符，ACL runtime 替你拆分调度。

**（2）同步传输的提交与等待**：

[src/hixl/fabric_mem/fabric_mem_host_transfer_service.cc:44-69](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_host_transfer_service.cc#L44-L69)

`IssueSyncCopy` 的注释讲清了三段式设计：锁外快速拒绝（`disconnecting` 原子读）、锁外地址解析（只碰私有数据）、共享锁内「先登记后提交」。失败防护用 `HIXL_DISMISSABLE_GUARD`（见 `TransferSync` L83-86）保证借出的槽位在任何失败路径都被注销并归还。

**（3）状态查询的单一临界区**：

[src/hixl/fabric_mem/fabric_mem_host_transfer_service.cc:182-203](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_host_transfer_service.cc#L182-L203)

查找记录、host flag 轮询、流查询兜底、摘除记录全部包在一个 `records_mutex` 临界区内——防止并发的断链中止在「查到记录」和「摘除记录」之间把槽位移走销毁；真正可能阻塞的流同步留到锁外，此时记录已归本线程所有。

#### 4.3.4 代码实践

**实践目标**：数一数 host 路径一次异步传输要碰几把锁，理解「并行度从哪来」。

1. 通读 `TransferAsync`（L143-168）与 `IssueAsyncCopyAndRegister`（L104-141），列出所有加锁点：`channels_mutex_`（GetChannel/BuildTransferContext 各一次短临界区）、`submit_gate` 共享锁、`records_mutex`、`req_route_mutex_`。
2. 回答：两个线程对同一远端同时 `TransferAsync`，哪些操作真正串行？（答：只有几段短临界区；拷贝提交因共享锁而并行。）
3. 回答：两个线程对不同远端同时 `TransferAsync` 呢？（答：连 submit_gate 都不同，完全并行。）

**需要观察的现象 / 预期结果**：源码阅读型实践。预期结论：host 路径的并行度 = 通道间无限并行 + 通道内提交并行，瓶颈只在槽位池容量（`max_stream_num / task_stream_num`）。

#### 4.3.5 小练习与答案

**练习 1**：host 路径为什么不需要 `transfer_mu` 这样的通道级互斥？
**答案**：每次传输用私有槽位和私有流，之间没有共享的设备侧队列；提交临界区都是短暂簿记，读写锁足够。AICPU 路径因为整条通道共享一个控制流和绑定槽位，才需要互斥。

**练习 2**：`GetTransferStatus` 返回 SUCCESS 但 `status=FAILED` 是什么情况？
**答案**：函数本身执行成功（SUCCESS 表示「查询这个动作完成」），但流查询报告流异常（`HandleAsyncStreamQueryFailure`，L221-227），传输失败经出参传递——与 u2-l5 讲过的「查询成功 ≠ 传输成功」契约一致。

**练习 3**：`CleanupAsyncTransfer` 为什么直接 `Release(slot, true)` 而不等待？
**答案**：这是用户主动放弃一个未完成的请求；`true` 表示「中止式归还」，由槽位池负责停流后回收，不会等 DMA 自然结束。

### 4.4 FabricMemAicpuTransferService：AICPU 路径

#### 4.4.1 概念说明

AICPU 路径把「逐 op 发 memcpy」换成「发一个 AICPU 内核，由内核直接向 RTSQ 写 SDMA SQE」。收益是绕开 ACL 拷贝框架的调度开销、批量下发效率更高；代价是并发模型必须重写：

- 每条通道只有一个**绑定槽位**（`bound_slot`），通道内所有流水化提交共享它——对齐 u4-l3 讲过的 hixl_cs 共享槽思想；
- 通道内用 `transfer_mu` 互斥：一次只允许一个 Acquire→Issue→Complete 序列在跑，但 `TransferAsync` 提交完就返回，下一个 Async 可立即复用槽位，不必等 `GetTransferStatus`；
- 通道之间仍然并行（各持各的槽位）；
- AICPU 内核的启动状态写在设备侧 status buffer，完成时要回读校验。

#### 4.4.2 核心流程

绑定槽位的获取与归还（`AcquireBoundSlotLocked`/`ReleaseBoundSlotLocked`）：

```text
获取（调用者已持 transfer_mu）：
    bound_slot_refs > 0 → 直接拷贝 channel->bound_slot，refs++   ← 复用
    否则从池里借新槽 → EnsureAicpuRtsqStreams 建 RTSQ 工作流
        → has_aicpu_unfold = true
        → dispatcher.AddTransferContext(slot)   ← 设备侧登记 TransferContext
        → channel->bound_slot = slot; refs = 1

归还（成功路径）：
    DestroyOwnedHostFlags → refs-- → 归零时把完整槽位还池复用
失败路径不走这里，走 AbortAicpuChannelLocked（销毁式）
```

同步传输主线：

```text
持 transfer_mu
AcquireBoundSlotLocked
计算剩余超时 → rtsq_timeout_ms（内核尊重同一截止时间；0 会变成内核默认 60s，所以预算耗尽必须直接报 TIMEOUT）
IssueCopyLocked：地址翻译 → dispatcher.Submit
WaitControlStreamsWithTimeout
CompleteSyncTransferLocked：CheckRequestStatus 回读内核状态 → 释放资源 → 归还槽位 → 统计
```

失败清理的关键规则（`CleanupFailedTransferLocked`）：**一旦 resource 已分配，说明内核可能已入队并会读取它，共享控制流不可再信任，必须整条通道中止**（`AbortAicpuChannelLocked` 排空所有在途请求）；resource 尚未分配则什么都没上设备，通道无恙，仅归还槽位。

#### 4.4.3 源码精读

**（1）初始化的可用性探测**：

[src/hixl/fabric_mem/fabric_mem_aicpu_transfer_service.cc:33-46](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_aicpu_transfer_service.cc#L33-L46)

若 dispatcher 初始化失败（无内核二进制或非 A3 RTSQ 运行时），返回 `UNSUPPORTED` 而非 `FAILED`——引擎据此可以做「请求了 AICPU 但环境不支持」的显式降级判断。同时强制 `task_stream_num == 1`。

**（2）绑定槽位的复用**：

[src/hixl/fabric_mem/fabric_mem_aicpu_transfer_service.cc:82-111](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_aicpu_transfer_service.cc#L82-L111)

注释直接点明这是模仿 hixl_cs 的 `AcquireSharedSlot`：refs>0 时零成本复用；新借的槽位要先 `EnsureAicpuRtsqStreams` 配工作流、`AddTransferContext` 在设备侧登记上下文，任一步失败由 slot_guard 中止归还。

**（3）异步传输的流水化**：

[src/hixl/fabric_mem/fabric_mem_aicpu_transfer_service.cc:223-260](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_aicpu_transfer_service.cc#L223-L260)

与 host 路径最大的不同：全程持 `transfer_mu`，但提交完（`RegisterAsyncTransferRecord`）就解锁返回——头文件 `FabricMemChannel` 的注释（`fabric_mem_channel_manager.h:50-52`）解释了为什么这样够用：下一个 `TransferAsync` 可以不等人查询直接复用 bound_slot，通道内一次一个、通道间并行。每次异步还要 `AllocateTransferHostFlags` 现场分配专属 host flag（清掉池里借来的共享视图），因为多个流水化请求各自需要独立的完成标志。

**（4）同步超时的毫秒截断**：

[src/hixl/fabric_mem/fabric_mem_aicpu_transfer_service.cc:206-212](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_aicpu_transfer_service.cc#L206-L212)

注释揭示一个内核 ABI 陷阱：内核把 `timeout_ms==0` 当作「用异步默认 60s」。若调用方预算已耗尽或不足 1ms，换算出的 `remaining_ms` 为 0，绝不能传 0（会被放大成 60s），必须自己报 `TIMEOUT`。

#### 4.4.4 代码实践

**实践目标**：对比两条路径对「同一远端连续 4 次 TransferAsync」的处理。

1. 读 host 路径 `TransferAsync`（4.3 节）：4 次调用借 4 个槽位、4 组流，全部并行在跑。
2. 读 AICPU 路径 `TransferAsync`（4.4 节）：4 次调用在 `transfer_mu` 上排队，但共享同一个 bound_slot 与控制流，前一个提交返回后下一个即可进入——流水化而非并行。
3. 写下两种模型各自的适用判断：通道内大并发小传输（host 更并行）vs 单通道深度流水 + 通道数多（AICPU 批量下发更省）。
4. 再看 `CleanupAsyncTransfer`（L366-390）：AICPU 版对「已提交请求」的清理为什么必须整通道中止（提示：L385-387 注释）。

**需要观察的现象 / 预期结果**：源码阅读型实践。预期产出一张两列对照表（锁 / 槽位 / 提交返回时机 / 失败影响面）。真实吞吐对比待在 A3 环境本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 AICPU 异步传输要为每次请求单独分配 host flag，而 host 路径用池里的？
**答案**：AICPU 通道上多个流水化请求共享一条控制流，若共享池 flag，先完成的 D2H 置位会让所有请求都「看起来完成」。每次请求配专属 flag（`AllocateTransferHostFlags` 置 `owns_host_flags=true`）才能独立判定。

**练习 2**：`CleanupFailedTransferLocked` 里「resource 为空则通道无恙」的依据是什么？
**答案**：resource（descriptor/status buffer）非空意味着内核已可能入队并读取它；为空说明失败发生在任何设备交互之前，没有污染共享控制流，归还槽位即可（L137-141）。

**练习 3**：AICPU 路径 `GetTransferStatus` 与 host 路径的实现差在哪？
**答案**：入口先锁 `transfer_mu`（host 没有）；完成判定同样是 host flag 优先，但完成后的校验多一步——回读设备侧 status buffer 确认内核启动本身没报错（`CheckRequestStatus`，L339-353），启动失败也要走整通道中止。

### 4.5 FabricMemAicpuDispatcher：描述符拆分与内核下发

#### 4.5.1 概念说明

dispatcher 是 host 侧的「AICPU 内核启动器」。它的定位写在头文件注释里：只上传解析好的 VMM 描述符、入队一个 AICPU 算子；设备侧二进制自己完成 RTSQ/SDMA 直接提交，不依赖任何通信栈。三个内核函数来自独立的 FabricMem AICPU 包：

- `HixlFabricMemBatchRead` / `HixlFabricMemBatchWrite`：批量搬运内核；
- `HixlSyncTransferContext`：TransferContext 的 ADD/DELETE 同步内核。

关键常量：每内核最多 128 条描述符（`kMaxDescriptorsPerKernelLaunch`）、单 SQE 上限 4GB（`kMaxSdmaTransferBytes` = uint32 最大值）、两次 NotifyRecord 之间最多 1920 个在途 SQE（`kFabricMemMaxInFlightRtsqTasks`）。

#### 4.5.2 核心流程

`Submit` 的完整流水：

```text
校验（resource 未被占用；AICPU 槽位必须恰好 1 流/1 工作流/1 notify）
BuildDeviceDescriptors：
    按 READ/WRITE 定方向（src/dst 对调）
    每个 op 按 4GB 上限切块 ← 一条设备描述符 = 恰好一条 SDMA SQE
launch_count = ceil(desc_count / 128)
分配三块设备内存：descriptor_buffer / status_buffer(每 launch 一个 u32) / kernel_args_buffer
LaunchDescriptorBatches：每 128 条一批
    构造 FabricMemAicpuKernelParam（含 RTSQ 元数据 + notify + 状态地址）
    BuildRtsqKernelParam：从工作流取 sq_id/cq_id/stream_id，预占连续 task_id（16 位截断）
    LaunchKernel：上传参数 + aclrtLaunchKernelV2
    累计 SQE ≥1920 或到达尾部 → emit_notify_record=1 并 aclrtWaitAndResetNotify
CheckRequestStatus（完成时）：回读 status buffer，全 0 才算成功
```

主机在提交阶段就用 notify 等待做节流：队列深度预算 1920 = RTSQ 容量减去 NotifyRecord 本身与环形队列留空的一格（`kFabricMemMinRtsqDepth = 1920 + 2` 的注释解释了这个不等式）。发布永不等待容量、队列满即视为模型被违反——这是「主机精确计数 SQE」换来的确定性。

TransferContext 生命周期：绑定槽位建立时 `AddTransferContext`（以 `notifies[0]` 指针值为合成 key，设备侧初始化上下文）；槽位销毁时 `DeleteTransferContext`。删除可能遇到设备侧 try_lock 失败（`DELETING` 中间态），以 100ms 间隔重试、总预算 30 秒，超时对 DELETE 强制清理——与 u4-l3 讲过的 hixl_cs `HixlSyncTransferContext` 内核同步机制同源。

#### 4.5.3 源码精读

**（1）host 与 AICPU 共享的 ABI**：

[src/hixl/fabric_mem/fabric_mem_aicpu_types.h:43-74](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_aicpu_types.h#L43-L74)

`FabricMemAicpuKernelParam` 的每个字段都有注释：RTSQ 三件套（id/stream_id/task_id）、逻辑 CQ id、超时、notify 元数据、状态地址、TransferContext key。注意 `emit_notify_record` 特意用 `uint32_t` 而非 bool——host 与 AICPU 二进制独立打包，ABI 上不能用宽度不定的类型。

**（2）描述符切分——一条描述符一条 SQE**：

[src/hixl/fabric_mem/fabric_mem_aicpu_dispatcher.cc:70-88](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_aicpu_dispatcher.cc#L70-L88)

注释说明了为什么在 host 侧而非内核侧切分：只有保证「一描述符 = 一 SQE」，host 才能精确计算一次启动占用多少 RTSQ 条目，从而在队列填满前用 NotifyRecord 等待做节流。超 4GB 的 op 按 `kMaxSdmaTransferBytes` 循环切块，地址与剩余长度同步推进。

**（3）批启动与 notify 节流**：

[src/hixl/fabric_mem/fabric_mem_aicpu_dispatcher.cc:398-432](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_aicpu_dispatcher.cc#L398-L432)

`LaunchOneDescriptorBatch` 决定是否在批尾发 NotifyRecord（`emit_notify = 累计≥1920 || 到达传输尾部`），构造内核参数后 `BuildRtsqKernelParam` 预占本批的连续 task_id，`LaunchKernel` 入队，最后 `aclrtWaitAndResetNotify` 排空——控制流在此处等待 AICPU 把此前的 SQE 消化完，队列永远不会溢出。

**（4）RTSQ 参数与 task_id 分配**：

[src/hixl/fabric_mem/fabric_mem_aicpu_dispatcher.cc:220-251](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_aicpu_dispatcher.cc#L220-L251)

从工作流取出 `sq_id`/`cq_id`/`stream_id` 填进内核参数；`ReserveRtsqTaskIds` 按 sq_id 维护主机侧计数器，发号后截断为 `uint16_t`（A3 SQE ABI）。唯一需要锁的状态就是这张计数表，且是叶子锁——dispatcher 本身不在提交路径持任何锁（头文件注释 L29-33）。

**（5）初始化的优雅降级**：

[src/hixl/fabric_mem/fabric_mem_aicpu_dispatcher.cc:131-144](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_aicpu_dispatcher.cc#L131-L144)

内核二进制或 A3 RTSQ 运行时缺失时，打 WARN 日志、卸载半加载的 binary、返回 `UNSUPPORTED`——「不可用」是可探测的环境状态而非故障。

#### 4.5.4 代码实践

**实践目标**：手算一次 AICPU 传输的内核启动次数与 SQE 占用。

1. 设一次 `TransferAsync` 下发 300 个 op，每个 8MB。按 `BuildDeviceDescriptors` 规则：8MB < 4GB，无需切块，共 300 条设备描述符。
2. 按 `kMaxDescriptorsPerKernelLaunch=128` 计算启动次数：⌈300/128⌉ = 3 次（128+128+44）。
3. 按 SQE 计数推演 notify 时机：`LaunchOneDescriptorBatch` 中 `next_task_count` 依次为 128、256、300+1（尾部强制 emit）——第 3 批因 `transfer_tail` 触发 notify，累计始终 < 1920，所以全程只有尾部一次 `aclrtWaitAndResetNotify`。
4. 再算 5000 个 op 的场景：累计会在第 15 批（1920）触发一次中途 notify，随后重新计数，尾部再一次。

**需要观察的现象 / 预期结果**：纸面推演即可完成；可与 `CountKernelLaunches`（L41-45）和 `LaunchDescriptorBatches`（L435-453）的循环逻辑互相印证。内核侧行为待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `Submit` 失败后 resource 不立即释放，而要交给 `AbortSlot`？
**答案**：失败可能发生在部分内核已入队之后，设备可能仍在读这些 buffer；只有等内核退出后（Abort 流程先停内核）才能安全释放（L481-484 注释）。

**练习 2**：`transfer_ctx_key` 为什么用 notify 指针值当 key？
**答案**：需要一个通道绑定槽位唯一的稳定标识，而 AICPU 模式没有 Hcomm 线程句柄可用；槽位的 `notifies[0]` 地址唯一且生命周期与槽位一致，故作「合成 key」（`fabric_mem_types.h:57-59` 注释）。

**练习 3**：dispatcher 的并发模型为什么可以「提交路径无锁」？
**答案**：所有针对同一通道的状态（bound_slot、工作 RTSQ、TransferContext）已被上层的 `transfer_mu` 串行化，不同通道各持独立槽位天然并行；唯一共享的 task_id 计数表用独立叶子锁保护（`fabric_mem_aicpu_dispatcher.h:29-33` 注释）。

## 5. 综合实践

**任务：写一份《FabricMem 传输路径选择决策说明》**，把本讲四个模块串起来。建议包含以下小节，全部结论必须给出源码行号依据：

1. **触发条件**：从 [examples/cpp/fabric_mem_d2d.cpp:51-53](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/fabric_mem_d2d.cpp#L51-L53) 出发——样例只设了 `EnableUseFabricMem=1`，没有显式配 `enable_aicpu_unfold`，结合 [src/hixl/engine/fabric_mem_engine.cc:145-175](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/fabric_mem_engine.cc#L145-L175)（默认 `value_or(true)`）与 [src/hixl/engine/hixl_options.cc:150-155](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_options.cc#L150-L155)（JSON 键 `fabric_memory.enable_aicpu_unfold`），说明：默认走 AICPU 路径；要通过 `GlobalResourceConfig` 选项显式置 false 才落回 host 路径，且此时才允许 `task_stream_num > 1`。
2. **运行时降级**：AICPU 请求但环境不支持（无内核二进制 / 非 A3 RTSQ / 无法解析物理设备号）时的返回值链：dispatcher 返回 `UNSUPPORTED`（`fabric_mem_aicpu_dispatcher.cc:131-144`）→ 服务 Initialize 透传 `UNSUPPORTED`（`fabric_mem_aicpu_transfer_service.cc:37-41`）→ 引擎 Initialize 失败。
3. **并发模型对照表**：锁、槽位归属、提交返回时机、失败影响面、流消耗（4.4.4 实践的产出）。
4. **容量换算**：给定 `max_stream_num`，两种模式各能容纳多少并发槽位（4.1.4 实践的产出）。
5. **决策建议**：什么业务形态选哪条路径，并注明「带宽结论待 A3 环境实测」。

有条件的话，在 A3 环境分别以两种配置跑 `fabric_mem_d2d` 样例，用统计输出（下讲 u5-l4 详述）验证你的决策建议；没有硬件则全篇标注「待本地验证」。

## 6. 本讲小结

- `FabricMemTransferService` 是传输服务基类，收拢槽位池、地址翻译（新旧 VA 双端翻译 + 未注册即 `PARAM_INVALID`）、host flag 完成轮询与统计上报；子类只实现搬运差异，`enable_aicpu_unfold` 一个布尔决定 host/AICPU 二选一。
- `FabricMemChannelManager` 以 `remote_engine` 为 key 管理通道台账：建链 = 控制面拉取共享句柄 → Import → 装配 Channel；`req_2_channel_` 路由表支撑状态查询；keepalive 用 dup-fd 快照实现锁外心跳与失联自动断链。
- host 路径逐 op 发 `aclrtMemcpyAsync`，私有槽位 + `submit_gate` 共享锁，通道内外皆并行；完成判定优先轮询 host flag（含 acquire 栅栏），兜底流查询。
- AICPU 路径经 dispatcher 发 AICPU 内核，per-channel 绑定槽位 + `transfer_mu` 通道内互斥但流水化（提交即返回、复用槽位）；resource 一旦分配，任何失败都升级为整通道中止。
- dispatcher 在 host 侧把 op 切成「一描述符一 SQE」，每 128 条一批启动内核，累计 1920 个在途 SQE 或到达尾部插 NotifyRecord 节流；RTSQ 元数据（sq/cq/task_id）由 host 构造，task_id 主机发号 16 位截断。
- 两条路径共享同一套安全闸语义：未注册地址不能传输、断链先 drain 再 unmap、abort-before-unmap。

## 7. 下一步学习建议

下一讲 **u5-l4 FabricMem 实战：d2d 样例端到端** 将运行 `examples/cpp/fabric_mem_d2d.cpp`，把本讲的配置开关、通道建立与传输路径在真实环境串成闭环，并学习 `FabricMemStatistic` 如何输出本讲反复出现的 transfer_cost / real_copy_cost 统计。预习时建议先读 `src/hixl/fabric_mem/fabric_mem_statistic.cc` 的周期 Dump 逻辑，再回顾 `fabric_mem_config.h` 的全部配置项。

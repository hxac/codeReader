# 传输后端：TransferEngine 与 HIXL 适配

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `TransferEngine` 抽象接口的完整清单，以及它为什么要把「建链、内存注册、传输」三类能力收拢到一个抽象里。
2. 讲清 `TransferEngineFactory` 如何根据 `llm.TransferBackend` 选项在 HCCL 原生后端（`CommTransferEngine`）和 HIXL 后端（`HixlTransferEngine`）之间二选一。
3. 精读 `HixlTransferEngine` 与 `HixlEntity` 的实现，理解 LLM-DataDist 的 KV Cache 语义如何被翻译成 HIXL Engine 的 Connect / TransferSync 调用。
4. 说明 `CommAdapter` 作为 HCCL 动态库桥接层的职责（dlopen、符号加载、统计埋点、错误码翻译）。
5. 理解 `AdxlInnerEngine` 的历史角色：它既是废弃 ADXL 公开接口的实现内核，又被 HIXL Engine 侧的 `CommEngine` 复用，其内部的 `BufferTransferService` 是「中转 buffer 传输模式」的载体。

## 2. 前置知识

本讲假设你已完成 u6-l1～u6-l4，以下概念直接使用不再展开：

- **LlmDataDist 两层 Pimpl**：公开类 `LlmDataDist` → api 层 `LlmDataDistImpl` → 实现层 `LLMDataDistV2`（u6-l2）。
- **CommEntity / CommEntityManager**：每条点对点链路对应一个 `CommEntity`，管理器以远端 cluster_id 为 key 维护台账（u6-l2）。
- **HcclOneSideOpDesc**：LLM-DataDist 内部的传输任务展开形态，即「本地地址 + 远端地址 + 字节数」三元组（u6-l5）。
- **HIXL Engine 内部 Engine 接口**：`src/hixl/engine/engine.h` 中的抽象基类，接口面与公开类 `hixl::Hixl` 基本对应（u3-l1）。

本讲新引入的基础概念：

- **适配器模式（Adapter Pattern）**：当已有系统（LLM-DataDist）期望一个接口 A（TransferEngine），而底层能力提供方（HIXL Engine）暴露的是接口 B 时，写一个实现 A、内部调用 B 的中间类，即为适配器。`HixlTransferEngine` 就是教科书式的适配器。
- **dlopen / dlsym**：Linux 动态库加载接口。运行时打开 `.so` 文件、按符号名查函数指针，避免编译期硬链接依赖。`CommAdapter` 用它加载 HCCL 的 `libhcomm.so`。
- **控制面 socket 与数据面分离**：HIXL 后端建链时除了 HIXL Engine 自己的 Connect，还会额外建一条 TCP 连接向对端索取 cache table 地址——这条 TCP 属于「控制面」，真正搬 KV Cache 数据走的是 HIXL 的「数据面」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/llm_datadist/transfer_engine/transfer_engine.h` | `TransferEngine` 抽象基类与 `TransferEngineFactory` 工厂声明 |
| `src/llm_datadist/transfer_engine/transfer_engine.cc` | 工厂实现：按 `llm.TransferBackend` 选项选择后端 |
| `src/llm_datadist/transfer_engine/hixl_transfer_engine.h` / `.cc` | HIXL 后端适配器：选项翻译、引擎创建、建链、内存注册 |
| `src/llm_datadist/transfer_engine/comm_transfer_engine.h` | HCCL 原生后端，内部委托 `LLMLinkManager`（兼容旧逻辑，本讲对照讲） |
| `src/llm_datadist/link_mgr/hixl_entity.h` / `.cc` | HIXL 后端的链路实体：Connect + 索取 cache table + 批量传输 |
| `src/llm_datadist/comm_adapter/comm_adapter.cc` | HCCL 动态库桥接单例：dlopen、符号加载、统计、错误码翻译 |
| `src/llm_datadist/adxl/adxl_inner_engine.h` / `.cc` | ADXL 历史引擎内核，含 `BufferTransferService` 中转传输模式 |
| `src/hixl/engine/comm_engine.h` | HIXL Engine 侧 `CommEngine`，按值持有 `AdxlInnerEngine`（历史纽带） |
| `docs/zh/design/llm-datadist_supporting_the_hixl_transfer_backend.md` | 本特性的设计文档（需求、类图、时序图） |
| `examples/python/hixl_transfer_backend_sample.py` | 端到端使用样例 |

## 4. 核心概念与源码讲解

### 4.1 TransferEngine 抽象与工厂

#### 4.1.1 概念说明

在 HIXL 后端出现之前，LLM-DataDist 的建链、内存注册和传输逻辑与 HCCL 通信域接口是「焊死」的：`LLMLinkManager` 直接调 HCCL 建链，`CommMemManager` 直接调 HCCL 注册内存。设计文档明确了重构动机——HCCL 提供了单边通信库（即 HIXL Engine 对接的底层能力），LLM-DataDist 需要对接它以支持更多芯片类型和通信能力，因此把传输层抽象为可插拔后端（[docs/zh/design/llm-datadist_supporting_the_hixl_transfer_backend.md:11-15](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/design/llm-datadist_supporting_the_hixl_transfer_backend.md#L11-L15)）。

`TransferEngine` 就是这个抽象：把「建链断链 + 内存注册注销 + 传输」三类能力统一为一个纯虚接口集，上层（`LLMDataDistV2`、`CommMemManager`、`DataCacheEngine`）只面向这个接口编程。

#### 4.1.2 核心流程

工厂决策过程：

```text
LLMDataDistV2 初始化
  └─ TransferEngineFactory::Create(options, cluster_id)
       ├─ options 中无 llm.TransferBackend → CommTransferEngine（HCCL 原生，默认）
       ├─ llm.TransferBackend == "hixl"    → HixlTransferEngine（本讲主角）
       └─ 其他值                           → 打日志 + 返回 nullptr（初始化失败）
```

选定后端后，`LLMDataDistV2` 会把 `CommEntityManager` 和 `CacheManager` 两个指针「注入」给引擎（`SetCommEntityManager` / `SetCacheManager`），因为 HIXL 后端建链后要自己往实体管理器里挂 `HixlEntity`，回查 cache table 时要用 `CacheManager`。

#### 4.1.3 源码精读

`TransferEngine` 抽象基类的接口全景——注意它不是 `hixl::Engine` 的复制，而是 LLM-DataDist 视角的最小集：

- [src/llm_datadist/transfer_engine/transfer_engine.h:24-37](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/transfer_engine/transfer_engine.h#L24-L37)：声明生命周期（`Initialize`/`Finalize`）、内存注册（`RegisterMem`/`UnregisterMem`）、批量建链断链（`LinkClusters`/`UnlinkClusters`）三组纯虚接口。
- [src/llm_datadist/transfer_engine/transfer_engine.h:38-44](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/transfer_engine/transfer_engine.h#L38-L44)：单链路 `Link`/`Unlink`（基于 HCCL 通信域与 rank table 的旧式建链）、`QueryRegisterMemStatus` 与 `SwitchRole`——这些接口是 HCCL 后端的语义，HIXL 后端多数返回不支持。
- [src/llm_datadist/transfer_engine/transfer_engine.h:46-52](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/transfer_engine/transfer_engine.h#L46-L52)：`SetCommEntityManager`/`SetCacheManager` 两个非虚的依赖注入方法，`cluster_id_` 与两个管理器指针放在 `protected` 段供子类使用。

工厂实现——后端选择唯一决策点：

- [src/llm_datadist/transfer_engine/transfer_engine.cc:30-42](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/transfer_engine/transfer_engine.cc#L30-L42)：`Create` 先查 `llm_datadist::OPTION_TRANSFER_BACKEND` 选项；查不到直接返回 `CommTransferEngine`（保持旧行为完全兼容，不设置选项的老程序零改动）；值为 `"hixl"` 返回 `HixlTransferEngine`；其他值报 `LLM_PARAM_INVALID` 并返回空指针。

`CommTransferEngine` 是旧逻辑的「包装壳」而非新实现：

- [src/llm_datadist/transfer_engine/comm_transfer_engine.h:41-43](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/transfer_engine/comm_transfer_engine.h#L41-L43)：它唯一的成员是 `std::unique_ptr<LLMLinkManager>`，即 u6-l2 精读过的旧建链管理器，所有接口都委托给它。这条路径最终经 `CommAdapter` 调 HCCL，正是 4.3 节的内容。

#### 4.1.4 代码实践

**实践目标**：亲手验证后端选择的分支逻辑，并理解「不设置选项 = 老行为」的兼容性设计。

**操作步骤**：

1. 打开 [src/llm_datadist/transfer_engine/transfer_engine.cc:30-42](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/transfer_engine/transfer_engine.cc#L30-L42)，确认三个分支。
2. 用 `Grep` 在仓库中搜索 `OPTION_TRANSFER_BACKEND`（定义在公开头文件 `include/llm_datadist/llm_datadist.h`），列出它在 C++、Python 两侧被读取的全部位置。
3. 回顾 u6-l2 讲过的 `LLMDataDistV2::Initialize`，确认 `TransferEngineFactory::Create` 的调用时机在五大组件装配的哪一步。

**需要观察的现象**：`OPTION_TRANSFER_BACKEND` 在 Python 侧对应配置名 `transfer_backend`（见 `examples/python/hixl_transfer_backend_sample.py:64` 的 `llm_config.transfer_backend = "hixl"`），C++ 侧则是 options map 中的一个字符串键——两侧最终汇合到同一个工厂分支。

**预期结果**：能画出「选项 → 工厂 → 后端实例」的三行决策表；无选项时确认走 `CommTransferEngine`。

#### 4.1.5 小练习与答案

**练习 1**：为什么不把 `Link`/`QueryRegisterMemStatus` 从基类里删掉，反正 HIXL 后端都不支持？

**答案**：基类接口是「并集设计」——以 HCCL 后端的历史语义为底，HIXL 后端对不适用的接口返回 `LLM_FEATURE_NOT_ENABLED` 而非编译错误。这样上层调用代码无需按后端类型写 if-else，且未来新后端可以按需实现子集。代价是调用方必须处理「不支持」错误码。

**练习 2**：`TransferEngine` 为什么需要 `SetCacheManager` 注入？HIXL 后端哪里用到了它？

**答案**：HIXL 后端建链时要向对端汇报本端 cache table 的地址和大小（用于对端单边读），这个信息存在 `CacheManager` 里。`HixlTransferEngine::InitMsgProcessor` 注册的回调中调用了 `cache_manager_->GetCacheTableBufferAndSize()`（见 4.2.3 节）。

### 4.2 HixlTransferEngine：HIXL 后端适配器

#### 4.2.1 概念说明

`HixlTransferEngine` 是本讲的核心。它做四件事：

1. **门槛校验**：HIXL 后端只支持「远端 cache 可直接访问」模式，且必须配置本端监听 ip:port。
2. **选项翻译**：把 `llm.` 前缀的 LLM-DataDist 选项翻译成 HIXL 引擎选项，未识别的忽略并打日志。
3. **引擎创建**：直接调用 HIXL **内部** 的 `hixl::EngineFactory::CreateEngine`（注意：不是公开类 `hixl::Hixl`，因为同进程内无需再包一层公开外壳）。
4. **语义映射**：LLM-DataDist 的建链 → `HixlEntity`（Connect + TCP 索取 cache table）；传输 → `TransferSync`。

一个容易忽略的细节：`Initialize` 里硬编码了 `hixl_options[hixl::OPTION_BUFFER_POOL] = "0:0"`，即显式关闭 HIXL 引擎内部的 buffer 池（中转传输模式，见 4.4 节），LLM-DataDist 场景下要求纯直达传输。

#### 4.2.2 核心流程

HIXL 后端一次完整建链（`LinkClusters`）的流程：

```text
LinkClusters(clusters, rets, timeout)
  ├─ 创建 16 线程的 LLMThreadPool
  ├─ 对每个 cluster 提交任务（任务内先 aclrtSetCurrentContext 恢复 ACL 上下文）
  │    └─ LinkCluster(cluster, timeout)
  │         ├─ 校验 remote_ip_infos.size() == 1（多点暂不支持）
  │         ├─ new HixlEntity(remote_ip, remote_port, engine_.get())
  │         ├─ EntityMemInfo 初始化（host/device 注册内存池）
  │         ├─ entity->Initialize(timeout)
  │         │    ├─ engine_->Connect("ip:port")          ← HIXL 数据面建链
  │         │    ├─ CtrlMsgPlugin::Connect(...)          ← 控制面 TCP
  │         │    ├─ 发送 kGetCacheTableReq
  │         │    └─ RecvCacheTableResp → 记录对端 cache table 为远端 device 内存
  │         ├─ entity->SetInfo() / MarkEntityIdle()
  │         └─ comm_entity_manager_->AddEntity(remote_cluster_id, entity)
  └─ 收集全部 future 结果到 rets
```

传输路径（`HixlEntity::BatchTransfer`）：

```text
DataCacheEngine 展开 KV Cache 传输任务
  └─ CommEntityManager::GetEntityByRemoteClusterId → HixlEntity
       └─ BatchTransfer(tasks, is_put, reversed)
            ├─ 逐个把 HcclOneSideOpDesc{localAddr, remoteAddr, count}
            │  翻译成 hixl::TransferOpDesc{local_addr, remote_addr, len}
            │  （reversed 为 true 时先交换 local/remote）
            └─ engine_->TransferSync(remote_engine, is_put ? WRITE : READ, op_descs, timeout)
```

`is_put` 与传输方向的对应关系是一个关键映射：LLM-DataDist 的 PUT（把数据写到对端）→ HIXL 的 `WRITE`，GET（从对端读回）→ `READ`。`reversed` 标志来自 u6-l5 讲过的 Job 体系——服务端代客户端执行「客户端视角的 GET」时，方向要翻转。

#### 4.2.3 源码精读

**选项翻译表**——LLM-DataDist 选项与 HIXL 引擎选项的对照：

- [src/llm_datadist/transfer_engine/hixl_transfer_engine.cc:29-46](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/transfer_engine/hixl_transfer_engine.cc#L29-L46)：`LLMDataDist2HixlOptions` 用一张静态 map 做键名翻译，共四条：`llm.RdmaTrafficClass`→`hixl::OPTION_RDMA_TRAFFIC_CLASS`、`llm.RdmaServiceLevel`→`hixl::OPTION_RDMA_SERVICE_LEVEL`、`llm.LocalCommRes`→`adxl.LocalCommRes`（历史命名）、`llm.GlobalResourceConfig`→`hixl::OPTION_GLOBAL_RESOURCE_CONFIG`；翻译不到的选项打 INFO 日志后忽略，不报错。

**初始化门槛**：

- [src/llm_datadist/transfer_engine/hixl_transfer_engine.cc:77-95](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/transfer_engine/hixl_transfer_engine.cc#L77-L95)：`Initialize` 先强制校验两件事——`llm.EnableRemoteCacheAccessible` 必须为 true（HIXL 后端不支持本地索引模式），以及 `OPTION_LISTEN_IP_INFO`（ip + port）必须配置；随后拼出 `local_engine_ = "ip:port"`。带端口意味着两侧都作为 HIXL server 监听，这正是设计文档「两侧均指定 ip port，均作为 server」的由来。

**引擎创建与消息处理器注册**：

- [src/llm_datadist/transfer_engine/hixl_transfer_engine.cc:96-106](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/transfer_engine/hixl_transfer_engine.cc#L96-L106)：先捕获当前 ACL context（`aclrtGetCurrentContext`），设置 `OPTION_BUFFER_POOL="0:0"`，翻译选项，然后 `hixl::EngineFactory::CreateEngine(local_engine_, hixl_options, parsed_options)` 创建内部引擎并 `Initialize`。捕获 context 是因为后续 16 线程池里的建链任务必须先 `aclrtSetCurrentContext` 才能操作设备资源。
- [src/llm_datadist/transfer_engine/hixl_transfer_engine.cc:48-75](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/transfer_engine/hixl_transfer_engine.cc#L48-L75)：`InitMsgProcessor` 向 HIXL 引擎注册 `kGetCacheTableReq` 消息的回调处理器：当对端通过控制面发来「索取 cache table」请求时，本端从 `cache_manager_` 取出 cache table 缓冲区地址和大小，按 HIXL 控制消息三段式帧格式（header + msg_type + body，复用 u4-l2 讲过的 `CtrlMsgHeader`/`kMagicNumber`）应答。这就是 `SetCacheManager` 注入的用途。

**内存注册直通**：

- [src/llm_datadist/transfer_engine/hixl_transfer_engine.cc:113-127](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/transfer_engine/hixl_transfer_engine.cc#L113-L127)：`RegisterMem` 把 `CommMemType` 翻译成 `hixl::MemType`（HOST→MEM_HOST，否则 MEM_DEVICE），组装 `hixl::MemDesc{addr, len}` 后直接调 `engine_->RegisterMem`。u6-l3 讲过的 `CommMemManager` 按 (addr,size) 去重后，最终落点就是这里。

**建链**：

- [src/llm_datadist/transfer_engine/hixl_transfer_engine.cc:129-154](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/transfer_engine/hixl_transfer_engine.cc#L129-L154)：`LinkCluster` 的完整编排在 4.2.2 的流程图中；注意 `ScopeGuard guard([entity]() { entity->Finalize(); })` 保证中途失败时回滚，全部成功后 `Dismiss` 取消防护。
- [src/llm_datadist/transfer_engine/hixl_transfer_engine.cc:156-181](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/transfer_engine/hixl_transfer_engine.cc#L156-L181)：`LinkClusters` 为每个集群在线程池提交一个任务，任务内先恢复 ACL context 再 `LinkCluster`；逐个 `future.get()` 收集结果填入 `rets`。这延续了 u6-l2 旧链路「每集群一个任务并发建链」的编排方式。

**不支持与角色语义**：

- [src/llm_datadist/transfer_engine/hixl_transfer_engine.cc:223-244](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/transfer_engine/hixl_transfer_engine.cc#L223-L244)：`Link`/`Unlink`/`QueryRegisterMemStatus` 直接返回 `LLM_FEATURE_NOT_ENABLED`——这三个接口是 HCCL 通信域（rank table）语义，HIXL 后端没有对应物。
- [src/llm_datadist/transfer_engine/hixl_transfer_engine.cc:246-261](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/transfer_engine/hixl_transfer_engine.cc#L246-L261)：`SwitchRole` 与角色无关直接返回成功，但若调用时传入了 listen ip:port，会校验其与初始化时的 `local_engine_` 一致——因为 HIXL 后端两侧都是 server，监听地址在生命周期内不允许漂移。这呼应 u6-l6「SetRole 实为监听 daemon 停启」的结论：HIXL 后端没有 daemon 可停，只剩一致性校验。

**HixlEntity——数据面传输的最终落点**：

- [src/llm_datadist/link_mgr/hixl_entity.cc:21-51](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/hixl_entity.cc#L21-L51)：`Initialize` 两步走——先 `engine_->Connect` 建立 HIXL 数据面链路，再经 `CtrlMsgPlugin::Connect` 建一条独立 TCP，发 `kGetCacheTableReq` 索取对端 cache table，把应答的 `{addr, size}` 作为一条 `COMM_MEM_TYPE_DEVICE` 远端内存登记到实体的 `GetRemoteMems()`。此后本端即可单边读对端 cache table，实现 u6-l5 讲过的 `PullCacheByGet` 直查模式。
- [src/llm_datadist/link_mgr/hixl_entity.cc:83-103](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/hixl_entity.cc#L83-L103)：`BatchTransfer` 把 `std::list<HcclOneSideOpDesc>` 逐个翻译成 `hixl::TransferOpDesc`（`reversed` 时交换本地/远端地址），最后一次 `engine_->TransferSync(remote_engine, is_put ? WRITE : READ, op_descs, timeout)` 批量下发——HIXL 的「批量 op_descs 是第一公民」（u2-l5）在此得到直接复用，LLM-DataDist 侧无需自己分批。
- [src/llm_datadist/link_mgr/hixl_entity.cc:75-81](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/hixl_entity.cc#L75-L81)：`Finalize` 以默认 30 秒超时 `engine_->Disconnect`；`force` 为 true 时跳过断链（用于进程退出等不允许阻塞的场景）。

#### 4.2.4 代码实践

**实践目标**：通过阅读样例确认 HIXL 后端的完整配置合同，并用文字/图形复现「初始化 → 建链 → 传输」的适配层次。

**操作步骤**：

1. 打开 [examples/python/hixl_transfer_backend_sample.py:64-68](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/python/hixl_transfer_backend_sample.py#L64-L68)，找到三行关键配置：`transfer_backend = "hixl"` 与两个进程各自的 `listen_ip_info`（端口 26000/26001）。
2. 追踪 Python 侧配置如何变成 C++ 选项：在 `src/python/llm_datadist/llm_datadist/v2/llm_datadist.py` 中搜索 `TransferBackend`，注意第 431-436 行附近会在设置 transfer_backend 时自动补上 `"llm.EnableRemoteCacheAccessible" = "1"`——这正是 4.2.3 节第一道门槛校验的 Python 侧对应物。
3. 对照本讲 4.2.2 的两个流程图，把它们抄录/重画成自己的版本，并在每个箭头上标注源码文件与行号。

**需要观察的现象**：Python 包装层替用户自动设置了 `EnableRemoteCacheAccessible=1`，而 C++ 裸调用的用户必须自己设置，否则 `HixlTransferEngine::Initialize` 会以 `LLM_PARAM_INVALID` 拒绝。

**预期结果**：得到一张从 `LlmDataDist`（Python/C++）到 `LLMDataDistV2` → `TransferEngineFactory` → `HixlTransferEngine` → `hixl::Engine`（内部 Engine 接口）→ CS 层的分层适配图。若想在真实昇腾环境运行样例验证，需按 u1-l2 完成 CANN 环境准备后执行（**待本地验证**）。

#### 4.2.5 小练习与答案

**练习 1**：`HixlTransferEngine` 为什么持有的是 `std::unique_ptr<hixl::Engine>`（内部抽象基类）而不是公开类 `std::unique_ptr<hixl::Hixl>`？

**答案**：两者接口面几乎相同（u3-l1），但 `hixl::Engine` 是同进程内部使用的轻量接口，少了公开外壳的门卫检查与日志包装；LLM-DataDist 本身就在同一个代码仓库/进程内，自己已做过参数校验，直接用内部接口更直接，也拿到了 `RegisterCallbackProcessor` 这类只在内部接口暴露的扩展点（用于注册 cache table 回调）。

**练习 2**：为什么 `LinkClusters` 的线程任务里要先 `aclrtSetCurrentContext(rt_context_)`？

**答案**：ACL 的 context 是线程绑定的：worker 线程默认没有 context，任何设备资源操作（建链中 HIXL 引擎内部的 aclrt 调用）都会失败。所以初始化时在主线程捕获 `rt_context_`，任务内先恢复。这与 u3-l4 讲过的 ConnectPoolExecutor「worker 须先恢复初始化时捕获的 ACL context」是同一铁律。

**练习 3**：设计文档说 HIXL 后端「不支持双向建链」。结合源码说明若两个集群互相调用 `LinkLlmClusters` 会发生什么。

**答案**：`LinkCluster` 每次都会 new 一个新的 `HixlEntity` 并 `AddEntity`；而 HIXL 引擎层面对同一 `ip:port` 的重复 Connect 返回幂等的 `ALREADY_CONNECTED`（u2-l4），数据面链路只有一条。文档表述的是产品约束：HIXL 后端下只有发起 `LinkClusters` 的一侧（client 角色）能主动读写，若需双向互访需两侧都发起建链——两侧各自作为 server 监听，因此各自向对方 Connect 是允许的（见设计文档时序图说明，[docs/zh/design/llm-datadist_supporting_the_hixl_transfer_backend.md:283](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/design/llm-datadist_supporting_the_hixl_transfer_backend.md#L283)）。

### 4.3 CommAdapter：HCCL 原生后端的桥接

#### 4.3.1 概念说明

`CommAdapter` 是 HCCL 原生后端（`CommTransferEngine` → `LLMLinkManager` → `CommEntity` 这条旧链路）的最底层出口：一个进程级单例，负责加载 HCCL 的 `libhcomm.so`、解析全部单边通信符号、在每个调用点上做耗时统计。它对应设计文档类图中的 `CommAdapter` 节点——「提供底层建链断链、内存注册注销和数据传输」（[docs/zh/design/llm-datadist_supporting_the_hixl_transfer_backend.md:165](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/design/llm-datadist_supporting_the_hixl_transfer_backend.md#L165)）。

理解它的意义在于对照：HIXL 后端（`HixlTransferEngine`）把「加载动态库、翻译错误码、统计耗时」这些活全部下沉给了 HIXL Engine 自己的 proxy/CS 层（u3-l5、u4 系列），LLM-DataDist 侧只剩薄薄一层适配；而 HCCL 原生后端这些活都在 LLM-DataDist 侧由 `CommAdapter` 承担。抽象出 `TransferEngine` 之后，两套实现可以在同一个上层之下并存。

#### 4.3.2 核心流程

```text
CommEntity / CommMemManager 需要调用 HCCL 能力
  └─ CommAdapter::GetInstance()（首次调用触发 LoadSo）
       ├─ dlopen("libhcomm.so", RTLD_NOW | RTLD_GLOBAL)
       ├─ 对 12 个符号逐个 dlsym：HcclExchangeMemDesc、HcclCommInitClusterInfoMemConfig、
       │   HcclCommDestroy、HcclBatchPut、HcclBatchGet、HcclRemapRegistedMemory、
       │   HcclRegisterGlobalMem、HcclDeregisterGlobalMem、HcclCommBindMem、
       │   HcclCommUnbindMem、HcclCommPrepare
       └─ 任何必需符号缺失 → 初始化失败
调用点统一形态：
  DlHcclXxx(...) {
    记起始时间 → 调函数指针 → 记结束时间 → CommStatisticManager 累计耗时 → 返回
  }
```

#### 4.3.3 源码精读

- [src/llm_datadist/comm_adapter/comm_adapter.cc:21-33](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/comm_adapter/comm_adapter.cc#L21-L33)：以常量字符数组声明 `.so` 名与全部符号名，共 12 个——建链族（InitClusterInfoMemConfig/CommDestroy/CommPrepare/BindMem/UnbindMem）、内存族（ExchangeMemDesc/RegisterGlobalMem/DeregisterGlobalMem/RemapRegistedMemory）、传输族（BatchPut/BatchGet）。
- [src/llm_datadist/comm_adapter/comm_adapter.cc:46-101](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/comm_adapter/comm_adapter.cc#L46-L101)：`LoadSo` 加互斥锁后 dlopen + 逐符号 `FunctionLoader::load`，任一必需符号为空即失败；`so_handle_` 非空则直接返回（幂等）。
- [src/llm_datadist/comm_adapter/comm_adapter.cc:188-202](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/comm_adapter/comm_adapter.cc#L188-L202)：`DlHcclBatchPut` 是典型的「统计包装」形态——前后取 `steady_clock` 时间戳，差值交给 `CommStatisticManager::AddBatchPutCost`。对比可见 `DlHcclBatchGet` 没有做同样的耗时包装，两兄弟实现并不完全对称。
- [src/llm_datadist/comm_adapter/comm_adapter.cc:258-269](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/comm_adapter/comm_adapter.cc#L258-L269)：`CommUtils::ConvertCommErrorToStatus` 把 HCCL 错误码翻译成 LLM-DataDist 错误码的映射表：`HCCL_E_PARA`→`LLM_PARAM_INVALID`、`HCCL_E_TIMEOUT`→`LLM_TIMEOUT`、`HCCL_E_NOT_SUPPORT`→`LLM_FEATURE_NOT_ENABLED`，其余落 default。这是两套错误码体系（0x5010Bxxx 段 vs HcclResult）的边界翻译点。
- 调用方示例（旧后端数据面）：[src/llm_datadist/adxl/comm_channel.cc:461-464](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/adxl/comm_channel.cc#L461-L464)：`CommChannel` 在传输时按方向调 `DlHcclBatchGet` / `DlHcclBatchPut`，签名与 HIXL 后端的 `TransferSync(READ/WRITE)` 一一对应。

#### 4.3.4 代码实践

**实践目标**：数清 `CommAdapter` 的全部符号并对照 HIXL 公开 API，建立「旧后端能力 → 新后端能力」的映射直觉。

**操作步骤**：

1. 阅读 [src/llm_datadist/comm_adapter/comm_adapter.cc:21-33](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/comm_adapter/comm_adapter.cc#L21-L33)，把 12 个符号按「建链 / 内存 / 传输」三类抄成表格。
2. 对照 `include/hixl/hixl.h` 的接口清单（u2-l1），给每个 HCCL 符号找到对应或最接近的 HIXL 接口（例如 `HcclBatchPut` ↔ `TransferSync(WRITE)`、`HcclRegisterGlobalMem` ↔ `RegisterMem`、`HcclCommInitClusterInfoMemConfig` ↔ `Connect`）。
3. 用 `Grep` 搜索 `GetInstance().DlHccl` 统计 `CommAdapter` 的调用点分布在哪些文件。

**需要观察的现象**：`CommAdapter` 的调用点几乎全部落在 `src/llm_datadist/adxl/` 目录（comm_channel.cc、channel_msg_handler.cc 等）——即旧后端的实现主体就是 ADXL 目录那套代码，这为 4.4 节的历史叙事埋下伏笔。

**预期结果**：一张三列映射表：HCCL 符号 / 所属类别 / 对应 HIXL 接口。这是纯源码阅读实践，无需硬件。

#### 4.3.5 小练习与答案

**练习 1**：`CommAdapter` 用 `RTLD_GLOBAL` 打开动态库，与常用的 `RTLD_LOCAL` 有何区别？这里为什么可能需要 GLOBAL？

**答案**：`RTLD_LOCAL`（默认）使符号只对本 dlopen 的调用方可见；`RTLD_GLOBAL` 把符号放入全局符号表，供后续加载的其他库解析。HCCL 运行时可能再加载插件库，这些插件需要回引 `libhcomm.so` 中的符号，因此用 GLOBAL。代价是增大符号冲突风险。

**练习 2**：为什么 `DlHcclRegisterGlobalMem` 等函数内部判空后返回 `HCCL_E_NOT_SUPPORT`，而不是像 `LoadSo` 那样直接失败？

**答案**：`LoadSo` 只保证符号当时存在；HCCL 版本升级后某些接口可能消失。注册/绑定族接口在不同 CANN 版本间可用性不同，调用点判空返回 NOT_SUPPORT 允许调用方走降级路径（经 `ConvertCommErrorToStatus` 翻译成 `LLM_FEATURE_NOT_ENABLED`），这与 u3-l5 讲过的 HcommProxy「弱符号 + 缺失返回不支持而非故障」策略同构。

### 4.4 AdxlInnerEngine 的历史角色与 BufferTransferService

#### 4.4.1 概念说明

`AdxlInnerEngine`（`src/llm_datadist/adxl/adxl_inner_engine.h/.cc`）是理解整个仓库演进史的一把钥匙。它的定位随时间变过两次：

1. **最初**：它是公开 ADXL 接口（`include/adxl/adxl_engine.h`，u8-l4 将详述）的实现内核——一套完整的「注册内存、建链、同步/异步传输、通知」引擎。
2. **现在**：它被 HIXL Engine 侧的 `CommEngine` 按值持有（[src/hixl/engine/comm_engine.h:62](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/comm_engine.h#L62)），成为 `EngineFactory` 兜底分支（u3-l1）的实现体；同时 `src/llm_datadist/api/adxl_engine_impl.cc` 仍用它支撑废弃的 ADXL 公开接口。也就是说：**HIXL Engine 的最老后端，正是复用了 LLM-DataDist 目录下的 ADXL 内核**——目录归属与组件边界在这里是交叉的（u1-l4 已提示过头文件与实现目录非一一对应）。

`BufferTransferService` 是 `AdxlInnerEngine` 内的可选传输模式：当直连链路（如跨机 RoCE）对某些内存组合不可用时，用一块预分配的 device buffer 做中转。注意 HIXL 后端在 `HixlTransferEngine::Initialize` 里用 `OPTION_BUFFER_POOL="0:0"` 显式关闭了这个模式（4.2.3 节），因为 LLM-DataDist 的 KV Cache 传输要求零拷贝直达。

#### 4.4.2 核心流程

`AdxlInnerEngine` 内部组合了四个协作组件（见成员声明 [src/llm_datadist/adxl/adxl_inner_engine.h:84-108](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/adxl/adxl_inner_engine.h#L84-L108)）：

```text
AdxlInnerEngine
  ├─ ChannelMsgHandler msg_handler_   ← 消息处理器：建链消息、内存注册（经 CommAdapter→HCCL）
  ├─ ChannelManager channel_manager_  ← 数据通道池（水位线控制）
  ├─ BufferTransferService (可选)     ← 中转 buffer 传输模式
  └─ SegmentTable segment_table_      ← 已注册内存区间表（判定每条 op 的内存类型组合）
```

传输前的类型判定是它的特色逻辑：对每条 `TransferOpDesc`，用 `SegmentTable` 分别查本地地址与远端地址落在哪块注册内存上，得到「本地类型 × 远端类型」组合，再结合 READ/WRITE 方向推导出 8 种 `TransferType` 之一（如 `kReadRD2H`、`kWriteD2RH`）——这正是 u1-l5 样例里那些路径记号的判定来源。

#### 4.4.3 源码精读

- [src/llm_datadist/adxl/adxl_inner_engine.cc:38-62](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/adxl/adxl_inner_engine.cc#L38-L62)：匿名命名空间中的 `DetermineTransferType`——纯函数，按 `TransferOp`（READ/WRITE）与「本地内存类型 × 远端内存类型」二维组合返回 8 种 `TransferType`。它印证了 u1-l5 的结论：路径记号由两端内存类型拼成、与方向正交。
- [src/llm_datadist/adxl/adxl_inner_engine.cc:379-389](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/adxl/adxl_inner_engine.cc#L379-L389)：`GetTransferType` 的调用侧——用 `segment_table_->FindSegment` 分别查本地与远端区间，查不到的默认按 `MEM_HOST` 处理，据此决定是否需要走 buffer 中转（`need_buffer`）。
- [src/llm_datadist/adxl/adxl_inner_engine.cc:250-303](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/adxl/adxl_inner_engine.cc#L250-L303)：`InitBufferTransferService` 的装配过程——解析 buffer_size 与 npu_pool_size 两个参数（`npu_pool_size == 0` 时整段跳过，即 `"0:0"` 配置的含义）；创建两个 `LlmMemPool`（复用 u6-l8 将讲的内存子系统）并各自 `aclrtMalloc` 一块高带宽内存、整体注册；最后用池列表构造 `BufferTransferService`。`LLM_DISMISSABLE_GUARD` 保证中途失败时逆序回滚已注册句柄与已分配内存。
- [src/llm_datadist/adxl/adxl_inner_engine.cc:334-344](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/adxl/adxl_inner_engine.cc#L334-L344)：`RegisterMem`/`DeregisterMem` 委托给 `msg_handler_`（其内部经 `CommAdapter::DlHcclRegisterGlobalMem` 等 HCCL 符号落地），方法开头 `hixl::TemporaryRtContext with_context(aclrt_context_)` 是「临时切换线程 context」的 RAII 包装，与 4.2 练习 2 的机制相同。
- [src/hixl/engine/comm_engine.h:62](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/comm_engine.h#L62)：`CommEngine` 按值持有 `adxl::AdxlInnerEngine adxl_inner_engine_`——HIXL Engine 的「兜底引擎」就是 ADXL 内核的壳，这是「ADXL → HIXL」演进在代码里留下的最直接的物理证据。

#### 4.4.4 代码实践

**实践目标**：梳理 `AdxlInnerEngine` 的两个使用方，验证「一个内核、两个外壳」的历史结论。

**操作步骤**：

1. 用 `Grep` 在全仓库搜索 `adxl_inner_engine.h` 的 include 位置，确认只有三处：`src/llm_datadist/api/adxl_engine_impl.cc`（ADXL 公开接口外壳）、`src/hixl/engine/comm_engine.h` 与 `src/hixl/engine/engine_factory.cc`（HIXL Engine 侧外壳）。
2. 打开 [src/hixl/engine/comm_engine.h:62](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/comm_engine.h#L62) 与 `src/llm_datadist/api/adxl_engine_impl.cc`，各找出一个转发到 `adxl_inner_engine_` 成员方法的最短调用链。
3. 回看 [src/llm_datadist/transfer_engine/hixl_transfer_engine.cc:97-98](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/transfer_engine/hixl_transfer_engine.cc#L97-L98) 的 `OPTION_BUFFER_POOL = "0:0"`，再到 [src/llm_datadist/adxl/adxl_inner_engine.cc:255](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/adxl/adxl_inner_engine.cc#L255) 确认 `npu_pool_size == 0` 时 buffer 服务整段跳过，闭环理解「HIXL 后端关闭中转模式」的传导路径。

**需要观察的现象**：`hixl_transfer_engine.cc` 设置的 `"0:0"` 一路传导到 `AdxlInnerEngine::InitBufferTransferService` 的提前返回——一个配置键穿过两层组件边界最终关闭一个可选特性。

**预期结果**：一张「AdxlInnerEngine 使用方关系图」，标注两个外壳各自的入口接口；以及一句结论：新 HIXL 后端（FabricMem/CS 直连路径）与老的 buffer 中转模式在 HIXL 后端下互斥。纯源码阅读实践，无需硬件。

#### 4.4.5 小练习与答案

**练习 1**：`DetermineTransferType` 一共能返回多少种组合？与 u1-l5 的样例记号如何对应？

**答案**：2 种操作（READ/WRITE）× 4 种「本地 × 远端」内存组合（H2H、H2rD、D2rH、D2D）= 8 种 `TransferType`。例如 `kReadRD2H` = READ 且本地 host、远端 device；`kWriteD2RH` = WRITE 且本地 device、远端 host，对应 d2rh 样例的路径。

**练习 2**：既然 `AdxlInnerEngine` 在 `src/llm_datadist/` 目录下，为什么说它同时属于「HIXL Engine 的后端」？

**答案**：组件归属由使用关系决定而非目录决定。`CommEngine` 是 `hixl::Engine` 的子类（u3-l1 讲过的 EngineFactory 兜底分支），它把 Engine 接口的调用全部转发给按值持有的 `AdxlInnerEngine`；因此走 CommEngine 路径的 HIXL 调用，最终代码就是在 `src/llm_datadist/adxl/` 里执行的。

**练习 3**：`BufferTransferService` 与 u6-l5 讲过的 D2H/H2D Job 的「device 中转 buffer」是不是同一个东西？

**答案**：不是。D2H/H2D Job 的中转 buffer 是 LLM-DataDist Job 体系在业务层为衔接 host/device 内存差异设计的两段搬运；`BufferTransferService` 是 `AdxlInnerEngine` 在引擎层为「直连链路不可用的内存组合」准备的可选池化中转，且在 HIXL 后端下被显式关闭。二者层级不同、开关独立。

## 5. 综合实践

把本讲四个模块串成一个任务——**手工绘制 HIXL 后端全链路分层图并撰写接入动机说明**：

1. **画图**（纸或 mermaid 均可）：纵向分五层，从上到下依次为
   - 用户层：`LlmDataDist`（C++/Python，标注 `transfer_backend="hixl"` 与 `listen_ip_info` 两个关键配置）；
   - 实现层：`LLMDataDistV2`（标注它注入 `CommEntityManager`/`CacheManager`）；
   - 后端抽象层：`TransferEngineFactory` → `HixlTransferEngine`（标注选项翻译表与 `EnableRemoteCacheAccessible` 门槛）；
   - HIXL 引擎层：`hixl::EngineFactory::CreateEngine` 创建的内部引擎 → ClientHandler → CS 层（u3/u4 已学，画到 CS 层即可）；
   - 链路实体：`HixlEntity`（标注 Connect、kGetCacheTableReq、BatchTransfer→TransferSync 三个动作）。
   在层间箭头上标注每次跨越所发生的「翻译」：选项键名翻译、`CommMemType`→`hixl::MemType`、`HcclOneSideOpDesc`→`hixl::TransferOpDesc`、`is_put`→`WRITE/READ`。
2. **对照验证**：图中每个框、每条边都要能对应到本讲引用过的具体文件与行号；对应不上的回源码补齐。
3. **写动机说明**（300 字以内）：依据 [docs/zh/design/llm-datadist_supporting_the_hixl_transfer_backend.md:3-15](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/design/llm-datadist_supporting_the_hixl_transfer_backend.md#L3-L15) 与 [62-65](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/design/llm-datadist_supporting_the_hixl_transfer_backend.md#L62-L65)，用自己的话回答：为什么把传输层抽象为后端？为什么选 HIXL 作为新后端？（提示：支持更多芯片类型与通信能力、复用 HIXL 的链路管理与 CS 能力、兼容 1.3 版本 LocalCommRes 描述的 ub_ctp/roce 通信资源。）
4. **可选上机**：在具备双 device 的环境运行 `examples/python/hixl_transfer_backend_sample.py`，用 `ASCEND_SLOG_PRINT_TO_STDOUT=1` 观察建链日志中出现的 `listen option`、`Start to link cluster` 字样，与图中节点对照（**待本地验证**）。

## 6. 本讲小结

- `TransferEngine` 是 LLM-DataDist 传输层的可插拔后端抽象，收拢建链、内存注册、传输三类能力；`TransferEngineFactory` 按 `llm.TransferBackend` 选项二选一：缺省走 HCCL 原生的 `CommTransferEngine`（完全兼容旧行为），`"hixl"` 走 `HixlTransferEngine`，其他值初始化失败。
- `HixlTransferEngine` 是适配器范本：校验门槛（必须 `EnableRemoteCacheAccessible` + 监听 ip:port）、四条选项键名翻译、直接创建 HIXL 内部 `Engine`、注册 `kGetCacheTableReq` 回调向对端汇报 cache table；`Link/Unlink/QueryRegisterMemStatus` 返回 `LLM_FEATURE_NOT_ENABLED`，`SwitchRole` 只做监听一致性校验。
- `HixlEntity` 承载链路实体：建链 = `engine_->Connect`（数据面）+ TCP 索取对端 cache table（控制面）；传输 = 把 `HcclOneSideOpDesc` 列表翻译成 `hixl::TransferOpDesc` 后一次 `TransferSync(WRITE/READ)`，批量语义由 HIXL 原生承接。
- `CommAdapter` 是旧后端的 HCCL 桥接单例：dlopen `libhcomm.so`、加载 12 个符号、逐调用点统计耗时，并把 HCCL 错误码翻译为 LLM-DataDist 错误码；HIXL 后端让这些职责全部下沉到了 HIXL 引擎内部。
- `AdxlInnerEngine` 是历史枢纽：既是废弃 ADXL 公开接口的内核，又被 `CommEngine` 按值持有而成为 HIXL Engine 的兜底实现；其 `BufferTransferService` 中转模式被 HIXL 后端以 `OPTION_BUFFER_POOL="0:0"` 显式关闭。

## 7. 下一步学习建议

本讲是单元六实现层的主线收官。建议：

1. 补齐同目录的 `comm_transfer_engine.cc` 与 `LLMLinkManager` 的对照阅读（u6-l2 已建时序图），体会「抽象后新旧后端编排方式的同与异」。
2. 若你关注 Python 视角，先修 u7-l2（LLM-DataDist Python 接口），再跑 u7-l3 的 PD 分离端到端样例——`hixl_transfer_backend_sample.py` 正是本讲反复引用的样例。
3. 性能与调试方向：阅读 u8-l3 的统计组件（本讲出现的 `CommStatisticManager` 即其中一员），理解两级统计管理器如何覆盖新旧两条后端链路。
4. 历史脉络方向：u8-l4 将系统梳理 ADXL 接口体系与新旧迁移清单，本讲 4.4 节的 `AdxlInnerEngine`/`BufferTransferService` 是其直接前置。

# 初始化与集群建链（u6-l2）

## 1. 本讲目标

上一讲（u6-l1）我们建立了 LLM-DataDist 的接口全景图：`LlmDataDist` 的 15 个公开接口分生命周期、链路、Cache、传输四组，角色只是业务标签。本讲下沉到实现层，沿「Initialize → SetRole → LinkLlmClusters」这条主线精读源码。学完本讲你应该能够：

1. 说出 `LlmDataDist::Initialize` 从公开外壳到 `LLMDataDistV2` 的两层管线，以及每一层各做了什么检查与装配。
2. 画出一次 `LinkLlmClusters` 的完整时序图：client 主动连接 → 控制面 TCP 握手交换 `LLMExchangeInfo` → 两端各自生成 rank table → 两端并发创建 HCCL comm 与 `CommEntity`。
3. 解释 rank table 的生成机制：`comm_res` JSON 的 version 协商、V1/V2 两种生成器、rank id 如何按 server_id 排序分配。
4. 说清 `CommEntityManager` 与 `CommLinkManager`/`LLMLinkManager` 各自管理什么：前者是「远端集群 → 通信实体」的台账（带 FSM 线程），后者是建链/断链的编排器（含 16 线程并发建链池）。

## 2. 前置知识

阅读本讲前，你应当已了解 u6-l1 的以下概念，这里做简要回顾与补充：

- **PD 分离与角色**：推理引擎把 Prefill（Prompt）与 Decode（Decoder）部署在两个集群，KV Cache 需要跨集群搬运。`LlmRole` 只是业务标签；真正决定「谁监听、谁主动连」的是 `OPTION_LISTEN_IP_INFO`（配置了 ip:port 的一端会启动监听 daemon，成为握手意义上的 server）。
- **cluster 与 CacheIndex**：每个 `LlmDataDist` 实例属于一个 `cluster_id`，跨集群寻址用 `CacheIndex{cluster_id, cache_id, batch_index}`。
- **Pimpl 惯用法**：公开头文件只前置声明实现类，用 `unique_ptr` 持有，使 `include/llm_datadist/llm_datadist.h` 不暴露内部依赖。本讲会看到两层 Pimpl：`LlmDataDist::LlmDataDistImpl`（api 层）套 `llm::LLMDataDistV2`（内部实现层）。
- **HCCL 与 CommAdapter**：默认传输后端基于 HCCL（华为集合通信库）的 `HcclComm` 通信域。LLM-DataDist 不直接链接 HCCL，而是通过 `CommAdapter` 以动态加载（dlopen）方式调用 `DlHcclCommInitClusterInfoMemConfig`、`DlHcclExchangeMemDesc` 等接口——这是 u6-l7 的主题，本讲只需知道这些 `Dl` 前缀函数是「经适配层转发的 HCCL 调用」。
- **rank table**：HCCL 建通信域需要一份描述「本集群 + 对端集群各有哪些机器/网卡/卡」的 JSON（`server_list` → `device_list` → `device_id/device_ip/rank_id`）。LLM-DataDist 的建链过程有一半工作就是在拼这张表。
- **错误码**：LLM-DataDist 内部用 `ge::Status`（`ge::SUCCESS` 为 0），外壳层再经 `TransRetToLlmCodes` 翻译成 `0x5010Bxxx` 段的 `LLM_*` 错误码（见 u6-l1）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/llm_datadist/api/llm_datadist_impl.cc](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/api/llm_datadist_impl.cc) | `LlmDataDist::LlmDataDistImpl`：公开接口的第一层实现，做选项解析、参数转换，然后转发给 `LLMDataDistV2` |
| [src/llm_datadist/llm_datadist_v2.cc](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/llm_datadist_v2.cc) / [llm_datadist_v2.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/llm_datadist_v2.h) | 内部实现层：装配 TransferEngine、CacheManager、CommEntityManager 等五大组件，并统一做计时统计与 ACL context 切换 |
| [src/llm_datadist/transfer_engine/transfer_engine.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/transfer_engine/transfer_engine.cc) | `TransferEngineFactory::Create`：按 `llm.TransferBackend` 选项选择默认 HCCL 后端还是 HIXL 后端 |
| [src/llm_datadist/transfer_engine/comm_transfer_engine.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/transfer_engine/comm_transfer_engine.cc) | 默认后端：创建并持有 `LLMLinkManager`，把建链/断链/角色切换全部委托给它 |
| [src/llm_datadist/link_mgr/comm_link_manager.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/comm_link_manager.cc) / [.h](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/comm_link_manager.h) | 建链基类：HCCL comm 生命周期、内存注册句柄表、`comm_id` 状态机、后台 PrepareMem 线程池 |
| [src/llm_datadist/link_mgr/llm_link_manager.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/llm_link_manager.cc) / [.h](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/llm_link_manager.h) | 建链子类：`LinkClusters/UnlinkClusters/SwitchRole` 的并发编排（每集群一个异步任务） |
| [src/llm_datadist/link_mgr/link_msg_handler.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/link_msg_handler.cc) / [.h](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/link_msg_handler.h) | 控制面协议：监听 daemon、`kConnect/kDisconnect/kStatus` 三种消息、`LLMExchangeInfo` 交换、建链核心流程 `ExchangeInfoProcess` |
| [src/llm_datadist/common/rank_table_generator.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/common/rank_table_generator.cc) / [rank_table_generator_v1.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/common/rank_table_generator_v1.cc) | rank table 工厂与 V1 生成器：comm_res 版本协商、双端合并、rank id 分配 |
| [src/llm_datadist/link_mgr/comm_entity_manager.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/comm_entity_manager.cc) / [.h](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/comm_entity_manager.h) | 通信实体台账：远端 cluster_id → `CommEntity` 映射、FSM 驱动线程、注册内存池 |

## 4. 核心概念与源码讲解

### 4.1 LlmDataDistImpl 与 LLMDataDistV2：两层初始化管线

#### 4.1.1 概念说明

用户调用的 `LlmDataDist::Initialize` 背后有两条实现层：

1. **api 层** `LlmDataDist::LlmDataDistImpl`（位于 `src/llm_datadist/api/`）：负责「公开类型 → 内部类型」的翻译。它把 `AscendString` 选项表补齐成内部需要的键（role、listen ip/port、ge.exec.deviceId 等），把 `ClusterInfo`/`IpInfo` 转成 `llm::ClusterInfo`，再做一层参数校验（timeout > 0、role 合法等）。
2. **实现层** `llm::LLMDataDistV2`（位于 `src/llm_datadist/` 根目录）：真正的装配车间。它创建五大组件——`TransferEngine`（传输后端）、`CacheManager`、`CommEntityManager`、`CommMemManager`、`DataCacheEngine`——并把它们互相接线，还创建独立的 ACL context、启动统计定时器。

为什么分两层？api 层隔离「公开 ABI 类型」（`AscendString`、`LlmRole` 等定义在公开头文件），实现层可以自由使用内部类型（`ge::Status`、nlohmann::json 等）而不污染公开头。这与 u2-l1 讲过的 `Hixl`/`HixlImpl` 是同一种 Pimpl 思路的套用。

#### 4.1.2 核心流程

`Initialize` 的两层流程：

```text
用户: LlmDataDist::Initialize(options)
  └─ LlmDataDistImpl::Initialize(options)              [api 层]
       ├─ 幂等检查：已初始化则直接返回 SUCCESS
       ├─ 解析 llm.DeviceId（必填，不能为空）
       ├─ 补键：ge.exec.deviceId / ge.exec.sessionDeviceId / 内部 role 键
       ├─ 解析 llm.ListenIpInfo（可选）→ 拆成内部 listenIp / listenPort 两个键
       ├─ 强制设置 enableRemoteCacheAccessible = "1"（新接口只走远端 Cache 可访问路径）
       └─ LLMDataDistV2::LLMDataDistInitialize(init_options)
            ├─ DoInitialize
            │    ├─ TransferEngineFactory::Create(options, cluster_id)   ← 后端分叉点
            │    ├─ 创建 CacheManager / CommMemManager / CommEntityManager / DataCacheEngine 并互相 Set
            │    └─ DoInnerInitialize(device_id, ...)
            │         ├─ aclrtSetDevice + aclrtCreateContext（独立 context）
            │         ├─ GlobalMemManager / CommMemManager / TransferEngine
            │         │  / DataCacheEngine / CommEntityManager 依次 Initialize（失败逆序回滚）
            │         └─ 启动 80 秒周期的统计 Dump 定时器
            └─ is_initialized_ = true
```

关键点：`TransferEngineFactory::Create` 是**传输后端唯一分叉点**——不配 `llm.TransferBackend` 用默认 HCCL 后端（`CommTransferEngine`），配 `"hixl"` 用 HIXL 后端（`HixlTransferEngine`，u6-l7 精读）。本讲的建链主线走默认后端。

#### 4.1.3 源码精读

api 层初始化的选项补齐逻辑：[src/llm_datadist/api/llm_datadist_impl.cc:L181-L219](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/api/llm_datadist_impl.cc#L181-L219)。这段代码做了三件事：`ParseDeviceId` 强制要求 `llm.DeviceId`（L187-190）；把 device id 同步到 ge 选项并注入内部 role 键（L191-198）；若配置了 `llm.ListenIpInfo` 则解析成 ip/port 两个内部键（L207-214），最后强制打开远端 Cache 可访问开关（L215）并调用 `LLMDataDistInitialize`（L216）。注意 L186 的 `LLM_DISMISSABLE_GUARD`：初始化中途失败会自动清空 `device_ids_`，不留半初始化状态。

失败逆序回滚的装配车间：[src/llm_datadist/llm_datadist_v2.cc:L22-L57](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/llm_datadist_v2.cc#L22-L57)。`DoInnerInitialize` 先 `aclrtSetDevice`/`aclrtCreateContext` 建立本库专属的 ACL context（L24、L38），再按依赖顺序初始化五个组件（L40-46），任何一步失败都由 L27 的 scope guard 逆序 Finalize；全部成功后 dismiss guard 并启动统计定时器（L48-54）。

后端选择工厂：[src/llm_datadist/transfer_engine/transfer_engine.cc:L30-L42](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/transfer_engine/transfer_engine.cc#L30-L42)。读 `llm.TransferBackend`：缺省 → `CommTransferEngine`；等于 `"hixl"` → `HixlTransferEngine`；其他值报 `LLM_PARAM_INVALID`。

api 层建链入口的参数翻译：[src/llm_datadist/api/llm_datadist_impl.cc:L226-L233](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/api/llm_datadist_impl.cc#L226-L233)。`LinkLlmClusters` 先检查已初始化与 `timeout > 0`，再用 `ConvertClusterInfos`（L53-66）把公开的 `ClusterInfo` 逐字段（含 ip 字符串转整数）翻译成内部类型，最后转发 `LinkClusters`。`SetRole` 的实现在 [L280-L304](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/api/llm_datadist_impl.cc#L280-L304)：只做 role 合法性与 listen 选项解析，真正的「有链不能切角色」检查在更下层的 `LLMLinkManager::SwitchRole`。

实现层对每个公开调用的统一包装：[src/llm_datadist/llm_datadist_v2.cc:L334-L347](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/llm_datadist_v2.cc#L334-L347)。`LLMDataDistV2::LinkClusters` 的模式是「检查 is_initialized → 临时切换 ACL context（`TemporaryRtContext`）→ 调 transfer_engine_ → 计入耗时统计」。文件中 `PullCache`/`TransferCache`/`UnlinkClusters` 等全部接口都是同一模板，这个模式值得记住——以后读任何 LLM-DataDist 接口实现都能对号入座。

#### 4.1.4 代码实践

**实践目标**：不写代码，验证「两条初始化防线」的实际行为差异。

**操作步骤**：

1. 打开 [src/llm_datadist/api/llm_datadist_impl.cc:L183-L190](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/api/llm_datadist_impl.cc#L183-L190)，记录两条早退分支的条件：已初始化（幂等返回 SUCCESS）、`llm.DeviceId` 为空。
2. 打开 [src/llm_datadist/llm_datadist_v2.cc:L90-L98](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/llm_datadist_v2.cc#L90-L98)，确认第二层还有 `mutex_ + is_initialized_` 原子标志的并发防线。
3. 在 `tests/cpp/llm_datadist/` 目录下搜索同时覆盖「重复 Initialize」「缺 DeviceId」的用例：`grep -rn "DeviceId\|Already initialized" tests/cpp/llm_datadist/`。

**需要观察的现象**：哪一层先拦截非法输入；测试如何用桩环境（无 NPU）构造 options 触发这些分支。

**预期结果**：api 层拦截参数类错误（DeviceId 缺失、role 非法、timeout ≤ 0），实现层用锁保证并发 Initialize 只执行一次。测试断言与源码检查条件一一对应。

**待本地验证**：grep 结果与用例断言细节需在本地仓库执行确认。

#### 4.1.5 小练习与答案

**练习 1**：`Initialize` 中 L215 强制设置 `enableRemoteCacheAccessible = "1"`，这个开关最终影响哪个组件的初始化行为？

**答案**：该标志一路传到 `CommEntityManager::Initialize(!remote_cache_accessible)`（[llm_datadist_v2.cc:L45-L46](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/llm_datadist_v2.cc#L45-L46)）：取反后作为 `start_service` 参数，决定是否启动 FSM 驱动线程（`HandleCacheRequest`）。开关为真时 `start_service=false`，因为新接口下实体由 FSM 主动驱动而非被动响应。

**练习 2**：为什么 `LLMDataDistV2` 每个接口都要包一层 `TemporaryRtContext`？

**答案**：库在 `DoInnerInitialize` 里创建了**独立的** ACL context（L38），而用户调用 LLM-DataDist 接口时线程上挂的可能是用户自己的 context。HCCL/ACL 运行时接口依赖当前线程 context，因此进入库实现前必须临时切到库自己的 context，调用结束再恢复——这和 u3-l4 讲过的「worker 线程须先恢复 context」是同一类问题。

**练习 3**：`Finalize` 的清理顺序为什么是 `UnlinkAllClusters` 在前、`transfer_engine_->Finalize()` 在后？

**答案**：见 [llm_datadist_v2.cc:L100-L119](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/llm_datadist_v2.cc#L100-L119)。断链要走 HCCL 通信域销毁，必须趁传输后端（HCCL 适配层）还活着时完成；先 Finalize 引擎会导致 Unlink 无法调用 `DlHccl` 系列函数。这是「先拆上层使用者、再拆底层服务」的通用清理顺序。

### 4.2 LLMLinkManager：LinkClusters 并发编排与角色切换

#### 4.2.1 概念说明

默认后端 `CommTransferEngine` 在初始化时创建一个 `LLMLinkManager` 并把所有链路操作委托给它（[comm_transfer_engine.cc:L25-L30](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/transfer_engine/comm_transfer_engine.cc#L25-L30)）。`LLMLinkManager` 继承自 `CommLinkManager`：

- **`CommLinkManager`（基类）**：管理 HCCL comm 的底层生命周期——`Link`（单通信域建链，走 rank table）、`Unlink`、`QueryRegisterMemStatus`、全局内存注册句柄表 `handles_`、`comm_id → CommStatus` 状态表和后台 16 线程的 `PrepareMem` 池。
- **`LLMLinkManager`（子类）**：面向「集群」的高层编排——`LinkClusters` 把每个远端集群作为一个独立任务提交到线程池并发建链；`SwitchRole` 管理监听 daemon 的启停。

`CommEntityManager` 则是两者共同写入的**实体台账**：`map<entity_id, CommEntity>` 加 `map<peer_cluster_id, entity_id>` 双索引，还跑着一个名为 `ge_llm_fsm` 的线程周期驱动所有实体的状态机（u6-l6 精读 FSM）。

#### 4.2.2 核心流程

`LinkLlmClusters` 端到端调用链（默认后端）：

```text
LlmDataDist::LinkLlmClusters(clusters, rets, timeout)
  └─ LlmDataDistImpl::LinkLlmClusters      参数翻译（IpInfo.ip 字符串→整数）
      └─ LLMDataDistV2::LinkClusters        计时 + context 切换
          └─ CommTransferEngine::LinkClusters
              └─ LLMLinkManager::LinkClusters
                   ├─ 校验 clusters 非空
                   ├─ 创建临时线程池 "llm_link_mem"（16 线程）
                   ├─ 对每个 cluster 提交任务：
                   │    LinkMsgHandler::LinkCluster(cluster, timeout)   ← 4.3 精读
                   └─ 逐个 future.get() 收集 rets（首个失败即返回，但全部收集）
```

`SetRole` 的调用链与检查点：

```text
LlmDataDist::SetRole(role, options)
  └─ LlmDataDistImpl::SetRole        解析 role 字符串 + listen ip/port
      └─ LLMDataDistV2::SwitchRole
          └─ CommTransferEngine::SwitchRole
              └─ LLMLinkManager::SwitchRole
                   ├─ 检查 GetLinkSize()==0，否则返回 LLM_EXIST_LINK   ← 「先断链再切角色」的强制点
                   └─ 按 listen 选项差异：停旧 daemon → 起新 daemon（或保持/关闭）
```

#### 4.2.3 源码精读

每集群一个任务的并发建链：[src/llm_datadist/link_mgr/llm_link_manager.cc:L43-L68](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/llm_link_manager.cc#L43-L68)。这段代码为每次调用现场创建 16 线程的临时池（L46），每个 `cluster` 一个 lambda 任务（内部先恢复 ACL context 再调 `msg_handler_.LinkCluster`），最后逐个 `get()` 把结果写进 `rets`（L61-67）——注意 `LLM_CHK_STATUS` 只打日志不返回，保证 `rets` 总是与 `clusters` 等长对齐，调用方可逐集群判断成败。

初始化时启动监听 daemon：[src/llm_datadist/link_mgr/llm_link_manager.cc:L23-L41](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/llm_link_manager.cc#L23-L41)。先初始化基类与消息处理器（L25-27），再检查 `kLlmOptionListenPort`：配置了端口的一端调 `msg_handler_.StartDaemon(ip, port)` 变成握手 server（L36）。这印证了 u6-l1 的结论：**谁配 `OPTION_LISTEN_IP_INFO` 谁监听，与 Prompt/Decoder 角色无关**。

角色切换的双保险：[src/llm_datadist/link_mgr/llm_link_manager.cc:L101-L138](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/llm_link_manager.cc#L101-L138)。L103 检查 `GetLinkSize() == 0`（实体数非零即报 `LLM_EXIST_LINK`，提示先 Unlink）；随后的分支处理三种情况：新端口与旧端口相同（什么都不做）、不同（先 `StopDaemon` 再 `StartDaemon`）、未配置 listen 选项（只停旧 daemon）。角色字符串本身被 `(void)role` 忽略——切换的实质只是监听身份的变化。

基类的 comm_id 状态机：[src/llm_datadist/link_mgr/comm_link_manager.cc:L332-L383](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/comm_link_manager.cc#L332-L383)。`CommLinkManager::Link` 是单通信域路径：校验 cluster_name 与本地 cluster 在表中（L334-338）→ 组装 `HcclCommConfig`（含可选 RDMA traffic class/service level）→ 调 `DlHcclCommInitClusterInfoMemConfig` 建 HCCL comm（L364-367）→ 生成 `comm_id`、登记 `CommStatus{PREPARING}` 并把 `PrepareMem` 任务提交到常驻线程池（L369-380）。链路上限 512 条（L25、L343）。

断链与 PrepareMem 的竞态防护：[src/llm_datadist/link_mgr/comm_link_manager.cc:L385-L426](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/comm_link_manager.cc#L385-L426)。`Unlink` 先置 `unlink_flag`（L393），然后自旋等待 `prepare_mem_flag` 清零（L396-411，1ms 间隔）——后台内存准备任务会在关键节点检查 unlink_flag 并自行中止（`CheckUnlink`，L130-137），两者配合避免「边建边拆」。等待结束后 `DestroyRes` 销毁实体与 comm，并回收 future。

实体台账的增删查：[src/llm_datadist/link_mgr/comm_entity_manager.cc:L22-L32](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/comm_entity_manager.cc#L22-L32)（`AddEntity` 双表写入 + 自增 entity_id）、[L34-L63](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/comm_entity_manager.cc#L34-L63)（`DestroyEntity`：FSM 服务已启动时只标记 DESTROYED，由 FSM 线程异步真正删除）、[L65-L87](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/comm_entity_manager.cc#L65-L87)（查询时过滤 DESTROYED/INIT 态实体——处于这两种状态的链路对上层「不存在」）。

#### 4.2.4 代码实践

**实践目标**：验证 `LinkClusters` 的并发模型与返回值语义。

**操作步骤**：

1. 阅读 [llm_link_manager.cc:L43-L68](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/llm_link_manager.cc#L43-L68)，数一数：一次 `LinkLlmClusters` 调用最多同时存在几个线程池？（提示：外层调用方的、`LinkClusters` 现建的 16 线程池、基类 `CommLinkManager` 的常驻 16 线程 `ge_llm_link` 池。）
2. 构造一个假想调用：`clusters = [{remote_cluster_id=2, remote_ip_infos=[{ip, port}]}, {remote_cluster_id=3, ...}]`，其中一个远端不可达。跟踪 L60-67 的循环，写出 `rets` 的内容与函数返回值。
3. 对照公开头文件 [include/llm_datadist/llm_datadist.h](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/llm_datadist/llm_datadist.h) 中 `LinkLlmClusters` 的注释，确认「部分成功」时用户应如何感知。

**需要观察的现象**：`rets` 与 `clusters` 的下标对应关系；整体返回值是否为第一个失败项。

**预期结果**：`rets.size() == clusters.size()` 恒成立；不可达集群对应下标为 `LLM_LINK_FAILED`（来自 `MsgHandlerPlugin::Connect` 的失败码），可达集群为 SUCCESS；函数返回第一个非 SUCCESS 的状态码。

**待本地验证**：第 3 步的注释表述需对照本地头文件确认。

#### 4.2.5 小练习与答案

**练习 1**：`CommLinkManager::Link` 与 `LLMLinkManager::LinkClusters` 是什么关系？新代码应该走哪条？

**答案**：`Link` 是「单 HCCL 通信域」的低层路径，需要调用方自己提供 rank_table 和 cluster2rank 映射，主要服务于 ADXL 兼容层（u8-l4）；`LinkClusters` 是新接口 `LinkLlmClusters` 的实现路径，rank table 由 `RankTableGenerator` 在握手后自动生成。新代码走 `LinkClusters`。

**练习 2**：`SwitchRole` 为什么要求 `GetLinkSize() == 0`？`GetLinkSize` 统计的是什么？

**答案**：见 [comm_entity_manager.cc:L113-L122](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/comm_entity_manager.cc#L113-L122)：它统计所有未处于 `FSM_DESTROYED_STATE` 的实体数。已有链路时切换监听身份会导致对端持有的连接信息失效，因此强制先 `UnlinkLlmClusters`（对应 u6-l1 说的 `LLM_EXIST_LINK` 约束——本讲定位到了它的确切实现位置）。

**练习 3**：`Unlink` 为什么要自旋等待而不是直接销毁？

**答案**：`PrepareMem` 在后台线程池执行，包含 HCCL 内存交换等不可中断点。直接销毁会与后台任务竞争同一 `HcclComm`。`unlink_flag` + `prepare_mem_flag` 两个原子标志实现了协作式取消：后台任务在安全点检查并退出，主线程确认退出后再统一销毁。

### 4.3 LinkMsgHandler：控制面握手协议

#### 4.3.1 概念说明

`LinkMsgHandler` 是建链的「协议引擎」，承载一条自定义的 TCP 控制面协议。消息只有三种（[link_msg_handler.h:L23](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/link_msg_handler.h#L23)）：

- `kConnect`（1）：携带 `LLMExchangeInfo`，发起建链；
- `kDisconnect`（2）：携带 `LLMDisconnectInfo`，发起断链；
- `kStatus`（3）：应答方回给发起方的执行结果（`error_code` + `error_message`）。

`LLMExchangeInfo`（[link_msg_handler.h:L25-L37](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/link_msg_handler.h#L25-L37))是握手的载荷：本端 cache table 的地址与大小、req/resp 缓冲区地址与大小、`cluster_id`、`comm_res`（通信资源 JSON，rank table 的原料）、`timeout` 与 `force_link`。消息体用 nlohmann::json 序列化（`Serialize`/`Deserialize`，[link_msg_handler.cc:L80-L102](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/link_msg_handler.cc#L80-L102)）。

**主动方**（未配 listen 的一端）执行 `LinkCluster` 充当 client；**被动方**的 `StartDaemon` 注册 `ConnectedProcess` 回调，daemon 线程 accept 后执行 `ProcessConnectRequest`。两端**对称地**各自创建一个指向对方的 `CommEntity` 并登记进 `CommEntityManager`——没有传统意义上的「server 持有连接表」，双方台账对等。

#### 4.3.2 核心流程

一次建链的完整时序（client = 主动发起方，server = 监听方）：

```text
client (LinkCluster)                        server daemon (ConnectedProcess)
────────────────────────                    ────────────────────────────────
GenerateLocalCommRes（必要时现算 comm_res）
TCP Connect(ip, port, timeout)  ──────────►  accept
CreateEntityMemInfo（req/resp 缓冲区）
组装 LLMExchangeInfo（本端 cache table
  + comm_res + cluster_id + timeout）
SendMsg(kConnect, exchange_info) ─────────►  CreateEntityMemInfo
                                            组装本端 LLMExchangeInfo
RecvMsg(kConnect)  ◄───────────────────────  SendMsg(kConnect, exchange_info)
ExchangeInfoProcess(peer_info):             ProcessConnectRequest 继续：
  1. GenerateRankInfo（合并两端 comm_res    ExchangeInfoProcess(peer_info):
     → rank_table + local/peer rank_id）      （与 client 完全对称的 5 步）
  2. force_link 时先 DestroyEntity 旧实体
  3. 检查 ALREADY_LINK（已存在同 cluster 实体）
  4. new CommEntity + EntityCommInfo
     （hcclCommName = "llm_datadist_<对端>_<本端>"，
       内含 rank_table → 初始化 HCCL comm）
  5. SetEntityMemInfo（登记对端三块远端内存）
     + AddEntity 入台账 + MarkEntityIdle
RecvMsg(kStatus)   ◄───────────────────────  SendMsg(kStatus, {error_code})
校验对端状态码 → 返回
```

两端必须使用**相同的** `hcclCommName` 才能配成同一个 HCCL 通信域：client 侧拼 `"llm_datadist_" + 本端cluster + "_" + 对端cluster`（[link_msg_handler.cc:L399-L400](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/link_msg_handler.cc#L399-L400)），server 侧拼 `"llm_datadist_" + 对端cluster + "_" + 本端cluster`（[L204-L205](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/link_msg_handler.cc#L204-L205)）——两个字符串里数字顺序互换但内容一致，这是两端「约名」的巧妙手法。

#### 4.3.3 源码精读

client 侧发起：[src/llm_datadist/link_mgr/link_msg_handler.cc:L363-L413](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/link_msg_handler.cc#L363-L413)。流程为：校验 `remote_ip_infos.size()==1`（L364，当前仅支持单 IP）→ `GenerateLocalCommRes`（L368，若用户没配 `llm.LocalCommRes` 则按本地 IP + device 现算，见 [L337-L349](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/link_msg_handler.cc#L337-L349)）→ TCP 连接（L373）→ 组装并发送 `kConnect`（L381-L395）→ 收对端 `kConnect` 后本地执行 `ExchangeInfoProcess`（L404）→ 最后收对端 `kStatus` 并校验其 `error_code`（L406-L408）。L401-403 的注释值得精读：`force_link` 的取值用于规避自连接场景下 server daemon 线程先完成 AddEntity 导致 client 侧命中 `ALREADY_LINK` 的竞态。

server 侧应答：[src/llm_datadist/link_mgr/link_msg_handler.cc:L178-L214](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/link_msg_handler.cc#L178-L214)。`ProcessConnectRequest` 先把自己本地的 `LLMExchangeInfo` 发回去（注意顺序：**先回自己的信息再处理对方的请求**），再用 scope guard 保证无论如何都回一条 `kStatus`（L194-L198），最后调 `ExchangeInfoProcess` 执行与 client 对称的建链。

建链核心五步：[src/llm_datadist/link_mgr/link_msg_handler.cc:L291-L335](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/link_msg_handler.cc#L291-L335)。`ExchangeInfoProcess`：L299 生成 rank table（4.4 精读）；L301-304 force_link 时先销毁旧实体；L306-308 已存在实体则报 `LLM_ALREADY_LINK`（重复建链的幂等防线）；L310-326 创建 `CommEntity` 与 `EntityCommInfo`（`comm_name`、rank_table、已注册内存句柄、换算成秒的 timeout 全部打包进 HCCL comm 参数）；L330-332 登记远端内存并入台账，成功后 dismiss 回滚 guard。

远端内存登记：[src/llm_datadist/link_mgr/link_msg_handler.cc:L255-L276](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/link_msg_handler.cc#L255-L276)。对端 `LLMExchangeInfo` 里的三块内存（device 上的 cache table + host 上的 req/resp 缓冲区）被逐一登记为 `CommMem`，之后单边传输就靠这三块「对端授权内存」完成——这与 u2-l3 的「先注册再传输」语义在 LLM-DataDist 层的体现。

断链协议：[src/llm_datadist/link_mgr/link_msg_handler.cc:L415-L456](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/link_msg_handler.cc#L415-L456)。`UnlinkCluster` 非 force 模式下连上对端发 `kDisconnect`、等 `kStatus`，两端各自 `DestroyEntity`；`force_flag=true` 时跳过控制面（对端可能已死），只销毁本地实体——这就是 `UnlinkLlmClusters` 的 `force_flag` 参数的去向。

#### 4.3.4 代码实践

**实践目标**：把上面的时序图与源码逐行对上，产出一份「消息字段流转表」。

**操作步骤**：

1. 对照 [LLMExchangeInfo 定义](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/link_msg_handler.h#L25-L37)（11 个字段），在 `LinkCluster`（client 侧，L381-393）与 `ProcessConnectRequest`（server 侧，L181-190）各找出每个字段的赋值来源。
2. 制作表格：列为「字段 / client 赋值来源 / server 赋值来源 / 消费方（谁读它）」。例如 `comm_res`：client 取自 `local_comm_res_`（初始化时配置或现算），server 相同；消费方是对端的 `GenerateRankInfo`。
3. 思考并在表中标注：`timeout` 字段为什么要在消息里传？（提示：server 侧 `ExchangeInfoProcess` L321-323 把毫秒换算成秒填进 HCCL comm 参数。）

**需要观察的现象**：两个字段的赋值在两端是否对称；`comm_name` 是唯一在传输途中动态拼出（不在 `LLMExchangeInfo` 结构里）的字段。

**预期结果**：得到一张 11 行的流转表，能回答「对端 cache table 地址从哪里来、到哪里去」。

**待本地验证**：无运行环节，纯源码阅读，结果可直接产出。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ProcessConnectRequest` 要在处理对端请求**之前**先把自己的 `LLMExchangeInfo` 发出去？

**答案**：client 的 `RecvMsg(kConnect)` 在 `ExchangeInfoProcess` 之前阻塞等待（[L396-L397](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/link_msg_handler.cc#L396-L397)）。若 server 先做耗时的 HCCL 建链再回包，client 的 TCP 读会一直阻塞且无法并行推进自己的建链准备。先回信息使两端的 `ExchangeInfoProcess` 得以并行执行，缩短建链总时延。

**练习 2**：重复对同一远端 `LinkLlmClusters` 两次会发生什么？与 u2-l4 HIXL 的 `ALREADY_CONNECTED` 幂等语义有何不同？

**答案**：第二次在 `ExchangeInfoProcess` L306-308 命中 `entity != nullptr` 检查，返回 `LLM_ALREADY_LINK` 失败（除非首次 `force_link` 语义触发先销毁）。HIXL 的重复 Connect 返回特殊码但视为幂等成功；LLM-DataDist 默认视为错误，需先 Unlink 或用 force 语义——两个库对「重复建链」的策略不同，迁移代码时要注意。

**练习 3**：`kStatus` 消息的 `error_code` 是 `ge::Status` 类型，而外层错误码是 `LLM_*` 段，它们会混淆吗？

**答案**：控制面内部全程使用 `ge::Status`（`ge::SUCCESS==0`），`kStatus` 只在两个 `LinkMsgHandler` 之间流转；回到 api 层时由 `LlmDataDist::Initialize`/`LinkLlmClusters` 外壳的 `TransRetToLlmCodes`（[llm_datadist_impl.cc:L452](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/api/llm_datadist_impl.cc#L452)）统一翻译，两套编码不会泄漏混淆。

### 4.4 RankTableGenerator：rank table 生成机制

#### 4.4.1 概念说明

HCCL 建通信域需要 rank table，而 LLM-DataDist 的用户只配了一个 `llm.LocalCommRes`（本机通信资源 JSON）甚至只配了一个监听 IP。rank table 从哪来？答案是**握手时交换**：两端的 `comm_res` 随 `kConnect` 消息互换，收到对端 `comm_res` 后由 `RankTableGenerator` 现场合并成一张双端 rank table。

类层次：

- `RankTableGenerator`（抽象基类）：`Generate(device_id, rank_table)` + `GetLocalRankId()/GetPeerRankId()` 三个纯虚接口。
- `RankTableGeneratorV1`：version "1.0" 格式（`server_list`/`device_list`，单 device 粒度）。
- `RankTableGeneratorV2`：version "1.2" 格式（新一代 SoC，`device_port` 等扩展字段）。
- `LocalCommResGenerator`：静态工具类，为本端**生成** `comm_res`（用户没配时按 SoC 型号选 V1/V2 格式自动生成）。
- `RankTableGeneratorFactory`：按两端 `comm_res` 的 version 协商决定用哪个生成器。

#### 4.4.2 核心流程

```text
GenerateRankInfo(peer_comm_res)
  └─ RankTableGeneratorFactory::Create(local_comm_res, peer_comm_res)
       ├─ 解析两端 JSON，提取 version 字段
       ├─ 版本合法性：两端都必须是 "1.0" 或 "1.2"
       └─ version = min(local, peer)          ← 就低协商
            "1.0" → RankTableGeneratorV1
            "1.2" → RankTableGeneratorV2
  └─ generator->Generate(device_id, rank_table)
       ├─ 解析两端 comm_res 为 RankTableInfo
       ├─ MergeRankTable：
       │    · 本端 server 的 device 必须与当前逻辑 device 对应的
       │      物理 device 一致（aclrtGetPhyDevIdByLogicDevId 校验）
       │    · 合并进 map<server_id, set<DeviceInfo>>（自动按 server_id 排序）
       │    · rank_id 从 0 起按 server_id 升序、设备序连续分配
       │    · 本端 device 标记 is_local=true
       └─ Dump 成 JSON 字符串 → 交给 HCCL
  └─ GetLocalRankId() / GetPeerRankId()：按 is_local 标记查 merged 表
```

rank id 分配的确定性：由于两端独立各自合并，必须保证**两端算出同一张表**。手段是 `std::map`/`std::set` 的有序性——server_id 排序确定遍历顺序，rank id 依次递增，与谁发起合并无关。这可以写成：

\[ \text{rank}(d) = \#\{d' \in \text{merged\_devices} : d' < d\} \]

其中全序由 `(server_id, device_id)` 的字典序给出。

#### 4.4.3 源码精读

版本协商工厂：[src/llm_datadist/common/rank_table_generator.cc:L30-L64](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/common/rank_table_generator.cc#L30-L64)。先做 JSON 大小与解析校验（L37-45），再要求两端 version 都属 {"1.0", "1.2"}（L51-57），最后 `std::min` 取低版本生成对应 generator（L59-63）——协议兼容的经典「就低」策略。

本端 comm_res 的自动生成：[src/llm_datadist/common/rank_table_generator.cc:L66-L81](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/common/rank_table_generator.cc#L66-L81)。`LocalCommResGenerator::Generate` 用 `aclrtGetSocName()` 查 SoC 型号，命中 `Ascend910_93xx` 系列（A5 类 SoC，L68-69）走 V2 格式，否则 V1。

V1 合并与 rank 分配：[src/llm_datadist/common/rank_table_generator_v1.cc:L65-L108](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/common/rank_table_generator_v1.cc#L65-L108)。`MergeRankTable` 校验每台 server 只声明一个 device 且本端声明的物理卡号与当前逻辑 device 一致（L71-79，`aclrtGetPhyDevIdByLogicDevId`）；对端 device 标记 `is_local=false`（L87）；L94-105 按 `map`（有序）遍历连续分配 `rank_id`。

本端 comm_res 的字段来源：[src/llm_datadist/common/rank_table_generator_v1.cc:L148-L174](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/common/rank_table_generator_v1.cc#L148-L174)。`GenerateLocalCommRes` 用物理卡号做 `device_id`，用 `hixl::GetDeviceIp` 查 device 侧网卡 IP（L159）——注意这里复用了 HIXL 引擎的工具函数，两个组件并非完全隔离。

rank 查询接口：[src/llm_datadist/common/rank_table_generator_v1.cc:L126-L146](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/common/rank_table_generator_v1.cc#L126-L146)。`GetLocalRankId`/`GetPeerRankId` 都是对 merged 表的线性扫描，按 `is_local` 标记取值，找不到返回 -1（由调用方 `GenerateRankInfo` 校验，[link_msg_handler.cc:L284-L287](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/link_msg_handler.cc#L284-L287)）。

#### 4.4.4 代码实践

**实践目标**：手工演算一次 rank table 合并，验证「两端独立合并结果一致」。

**操作步骤**：

1. 构造示例输入（示例数据，非项目文件）：本端 comm_res 声明 `server_id="192.168.1.10"`、device_id="0"；对端声明 `server_id="192.168.1.20"`、device_id="0"。
2. 按 [MergeRankTable](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/common/rank_table_generator_v1.cc#L65-L108) 的规则，分别以「本端视角」和「对端视角」推导 merged 表：server_id 排序 → rank_id 分配 → is_local 标记。
3. 核对两份结果的 `rank_table` JSON 是否逐字节相同、`GetLocalRankId` 是否各取到不同值。

**需要观察的现象**：字符串比较 `"192.168.1.10" < "192.168.1.20"` 决定 rank 0/1 的归属；`is_local` 是唯一随视角变化的字段，它不在序列化输出里。

**预期结果**：两端 rank table 完全一致；本端视角 local rank=0、peer rank=1，对端视角反之。HCCL 通信域因此能正确配对。

**待本地验证**：可在本地用任意 JSON 库重放此演算，无硬件依赖。

#### 4.4.5 小练习与答案

**练习 1**：如果本端 comm_res 里声明的 device_id 与实际运行的逻辑 device 不符，会在哪一步、以什么错误暴露？

**答案**：`MergeRankTable` L74-79：用 `aclrtGetPhyDevIdByLogicDevId` 把逻辑 device 转物理卡号，与 comm_res 声明的 `device_id` 字符串比对，不一致报 `LLM_PARAM_INVALID`，日志提示检查 `llm.LocalCommRes`。这发生在握手后、HCCL 建链前的生成阶段。

**练习 2**：本端用 V2 格式（"1.2"）、对端用 V1 格式（"1.0"），最终用哪个生成器？可能出什么问题？

**答案**：`std::min("1.0","1.2") == "1.0"`，走 `RankTableGeneratorV1`。V1 的合并逻辑对 `device_port` 等扩展字段的读写方式与 V2 不同（比较 [rank_table_generator_v1.cc:L26-L36](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/common/rank_table_generator_v1.cc#L26-L36) 的 `LoadOptionalDeviceNetworkFields`），跨代机器组网时应保证两端 CANN/配置格式一致，版本协商是防呆而非万能。

**练习 3**：`LocalCommResGenerator::Generate` 依据什么选 V1/V2？这个判断在什么时机执行？

**答案**：依据 `aclrtGetSocName()` 返回的 SoC 名是否在 `kV2Version` 集合（六种 `Ascend910_93xx` 型号）。执行时机有两个：`LinkMsgHandler::Initialize`（用户只配了监听 IP 时，[link_msg_handler.cc:L117-L120](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/link_msg_handler.cc#L117-L120)）和首次 `LinkCluster` 时的 `GenerateLocalCommRes`（L368）——后者覆盖「初始化时也没有 IP」的场景。

## 5. 综合实践

**任务：追踪 SetRole + LinkLlmClusters 的内部调用链，画出 Prompt 与 Decoder 两端的建链时序图。**

这是本讲规格指定的实践，纯源码阅读即可完成，无需昇腾硬件：

1. **准备场景**：设想 PD 分离最小部署——Prompt 集群（cluster_id=0，device 0，配置 `llm.ListenIpInfo="192.168.10.1:26000"`）与 Decoder 集群（cluster_id=1，device 0，不配监听）。双方均未配 `llm.LocalCommRes`。
2. **画初始化序列**：从两端的 `Initialize` 画起，标出：api 层补了哪些键、`TransferEngineFactory` 各自选了什么后端（两端都没配 `llm.TransferBackend` → 均为默认）、哪一端的 `LLMLinkManager::Initialize` 启动了 daemon（只有 Prompt 端）。
3. **补画 SetRole 分支**：假设 Decoder 端先 `SetRole(kPrompt, {ListenIpInfo=...})` 再建链。在图中标出 `LLMLinkManager::SwitchRole` 的 `GetLinkSize()==0` 检查点与 daemon 启停分支（[llm_link_manager.cc:L101-L138](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/llm_link_manager.cc#L101-L138)）。
4. **画建链时序**：Decoder 端调 `LinkLlmClusters([{remote_cluster_id=0, remote_ip_infos=[{192.168.10.1, 26000}]}], timeout=60000)`。按 4.3.2 的时序图补全两端细节，重点标注：`comm_res` 各自何时生成（两端都走 `LocalCommResGenerator::Generate` 现算）、rank table 两端独立合并但结果一致、`hcclCommName` 两端拼出相同字符串、最后 `kStatus` 双向确认。
5. **验证**：把你画的图与 `examples/cpp/decoder_pull_cache_and_blocks.cpp`（Decoder 侧）和 `examples/cpp/prompt_push_cache_and_blocks.cpp`（Prompt 侧）的 `Initialize`/`SetRole`/`LinkLlmClusters` 实参对照，检查选项键与角色假设是否与真实样例一致。样例的启动方式见 [examples/README.md](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/README.md)（在昇腾环境跑通双进程属于 u7-l3 的实践，本讲不要求）。

产出物：一张时序图 + 一份「选项键在两端的取值对照表」。这份图同时也是 u6-l4（Push/Pull 传输）与 u6-l6（FSM）的导航地图。

## 6. 本讲小结

- LLM-DataDist 初始化是**两层管线**：api 层 `LlmDataDistImpl` 做公开类型翻译与选项补齐（`llm.DeviceId` 必填、listen 信息拆键），实现层 `LLMDataDistV2` 装配五大组件并创建独立 ACL context，失败逆序回滚；`TransferEngineFactory` 是后端唯一分叉点（默认 HCCL / `llm.TransferBackend=hixl`）。
- `LinkLlmClusters` 的并发模型：`LLMLinkManager` 为每个远端集群提交一个任务到 16 线程池并发建链，`rets` 与 `clusters` 等长对齐；断链用 `unlink_flag`/`prepare_mem_flag` 双原子标志做协作式取消。
- 建链控制面是自定义 TCP 协议：`kConnect/kDisconnect/kStatus` 三种消息、`LLMExchangeInfo`（cache table 地址 + req/resp 缓冲区 + comm_res + cluster_id）做载荷；两端**对称地**各自创建 `CommEntity` 入 `CommEntityManager` 台账，`hcclCommName` 两端拼出相同字符串配对 HCCL 通信域。
- rank table 在握手中现算：两端交换 `comm_res`，`RankTableGeneratorFactory` 按 version 就低协商选 V1/V2，合并时用有序容器保证两端独立合并得到同一张表，rank id 按 server_id 字典序连续分配。
- 重复建链返回 `LLM_ALREADY_LINK`（与 HIXL 的幂等 `ALREADY_CONNECTED` 策略不同）；`SetRole` 要求先断链（`LLM_EXIST_LINK`），其本质只是监听 daemon 的启停。
- `CommEntityManager` 是「远端集群 → 通信实体」台账，查询时过滤 DESTROYED/INIT 态实体，FSM 线程 `ge_llm_fsm` 周期驱动实体状态迁移（u6-l6 展开）。

## 7. 下一步学习建议

链路建好后，下一步自然是「Cache 从哪来」。建议：

1. **下一讲 u6-l3（Cache 管理）**：精读 `cache_mgr/cache_manager.cc`、`data_cache_engine.cc`，理解建链时交换的 `cache_table`（本讲反复出现的 `GetCacheTableBufferAndSize`）如何在 Pull/Push 时被用作远端寻址表。
2. **u6-l6（FSM）**：本讲多次出现 `MarkEntityIdle`/`MarkEntityDestroyed`/`FSM_INIT_STATE`，实体状态机的完整迁移规则在 `fsm/` 目录。
3. **u6-l7（传输后端）**：若你对 `llm.TransferBackend=hixl` 分支好奇，`hixl_transfer_engine.cc` 的 `LinkCluster` 用 `HixlEntity` 替代了本讲的 HCCL 路径，可对比两种后端在建链语义上的差异。
4. 阅读顺序上建议先跑一遍第 5 节综合实践画出时序图，再进入 u6-l3——图中的 `CommEntity`/`cache table` 是下一讲的入口概念。

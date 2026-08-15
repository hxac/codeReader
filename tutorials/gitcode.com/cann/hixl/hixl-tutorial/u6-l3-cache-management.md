# Cache 管理：分配、注册与查找

## 1. 本讲目标

上一讲（u6-l2）我们走通了 LLM-DataDist 的初始化与集群建链，知道了 `LLMDataDistV2` 在 Initialize 时装配了 CacheManager、CommMemManager、DataCacheEngine 等组件。本讲下沉到 `src/llm_datadist/cache_mgr/` 目录，回答三个问题：

1. 一块 KV Cache 在 LLM-DataDist 内部是如何被描述和登记的（`CacheDesc` / `CacheEntry` / `CacheKey` 数据结构与 `CachePlacement` 语义）？
2. 用户获得一块 Cache 有哪两条路径——引擎代为分配（Allocate）与注册外部内存（Register）——两者行为有何差异？
3. 传输时如何凭 `cache_id` 或 `cache_key` 查到内存地址，以及这些地址如何被注册进传输后端使其可被远端单边访问？

学完本讲，你应当能够读懂 `cache_mgr` 模块的完整调用链，并能根据业务场景正确选择 Allocate 还是 Register。

## 2. 前置知识

- **KV Cache 与 PD 分离**：大模型推理分为 Prefill（Prompt 计算）与 Decode（逐 token 生成）两个阶段。PD 分离架构中两阶段跑在不同集群，Prefill 算出的 KV Cache 需要搬运到 Decode 侧。LLM-DataDist 管理的就是这些 KV Cache 的「内存本体 + 寻址索引」。
- **单边传输的注册前提**（回顾 u2-l3）：一块内存要被远端直接 READ/WRITE，必须先经过传输后端（HIXL Engine 或 HCCL）注册，换取对端可 DMA 访问的授权。LLM-DataDist 把这一步封装在 CommMemManager 里，用户无感。
- **内存池（Memory Pool）**：LLM-DataDist 可以在 Initialize 时通过 `llm.MemPoolConfig` 选项预分配一大块 device/host 内存，之后 Allocate 接口从池里切分，避免反复调用 `aclrtMalloc`。池的实现（scalable allocator + span）属于内存子系统，将在 u6-l8 详讲，本讲只把它当作「能 AllocShared 的黑盒」。
- **`ge::Status` 与 LLM 错误码**（回顾 u6-l1）：内部实现统一返回 `ge::Status`（`ge::SUCCESS` 为 0），API 层再翻译成 `LLM_XXX` 错误码。本讲引用的源码中大量出现 `LLM_CHK_BOOL_RET_STATUS` / `LLM_CHK_STATUS_RET` 宏，含义是「条件不满足/调用失败即打日志并返回错误码」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/llm_datadist/llm_datadist.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/llm_datadist/llm_datadist.h) | 公开数据结构：`CacheIndex`、`CachePlacement`、`CacheDesc`、`Cache`，以及 `LlmDataDist` 公开类 |
| [src/llm_datadist/common/llm_inner_types.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/common/llm_inner_types.h) | 内部扩展数据结构：`CacheKey`、`CacheMemType`、内部版 `CacheDesc`（比公开版多 `cache_mem_type`、`remote_accessible` 等字段） |
| [src/llm_datadist/common/common.h](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/common/common.h) | `CacheEntry`（cache 台账条目）与 `llm.MemPoolConfig` 等选项键 |
| [src/llm_datadist/cache_mgr/cache_manager.cc](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/cache_mgr/cache_manager.cc) | CacheManager：cache 台账、cache_key 索引、Allocate/Deallocate/RemoveCacheKey、本地 CopyCache |
| [src/llm_datadist/cache_mgr/data_cache_engine.cc](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/cache_mgr/data_cache_engine.cc) | DataCacheEngine：cache 引擎门面，编排注册/分配/传输，并负责内存池与 cache table 初始化 |
| [src/llm_datadist/cache_mgr/comm_mem_manager.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/cache_mgr/comm_mem_manager.cc) | CommMemManager / GlobalMemManager：把 cache 内存注册进传输后端 |
| [src/llm_datadist/api/llm_datadist_impl.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/api/llm_datadist_impl.cc) | API 层：`RegisterKvCache` 的参数翻译与门卫检查 |

> 说明：讲义规格中提到的 `include/llm_datadist/llm_engine_types.h` 实际只存放 `llm.ClusterInfo`、`llm.Role` 等选项键常量；Cache 相关公开结构真正定义在 `include/llm_datadist/llm_datadist.h` 中（见 [llm_engine_types.h:L15-L23](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/llm_datadist/llm_engine_types.h#L15-L23)）。

## 4. 核心概念与源码讲解

先给出本讲的模块关系图（文字版）：

```text
用户 (C++ RegisterKvCache / Python allocate_cache)
  │
  ▼
LlmDataDistImpl (api 层：参数翻译、门卫检查)
  │
  ▼
LLMDataDistV2 (实现层：恢复 ACL context、统计)
  │
  ▼
DataCacheEngine (cache 引擎门面：发号 cache_id、管内存池)
  ├──► CacheManager     (cache_id→CacheEntry 台账 + cache_key 索引 + 本地拷贝)
  └──► CommMemManager   (cache_id→传输后端 mem_handle 台账，按 (addr,size) 去重)
         │
         ▼
       TransferEngine (HCCL 或 HIXL 后端，u6-l7 详讲)
```

### 4.1 CacheManager：cache 台账与索引

#### 4.1.1 概念说明

CacheManager 是「账本」：每块被管理的 cache 在这里占一行（`CacheEntry`），同时维护两套寻址索引——按 `cache_id`（本端整数 ID）和按 `DataCacheKey`（业务语义键）。远端要 Pull 本端某条请求的 KV 时，本端凭 `cache_key` 反查出地址；本端自己传输时凭 `cache_id` 查。

理解账本的关键是分清三层 ID：

| 层 | 名称 | 产生方 | 作用域 |
| --- | --- | --- | --- |
| 公开 | `cache_id` | `DataCacheEngine` 的原子计数器（从 1 递增） | 本进程内唯一 |
| 公开 | `CacheIndex` = (cluster_id, cache_id, batch_index) | 用户构造 | 跨进程寻址远端 cache |
| 内部 | `DataCacheKey` = (req_id 或 prefix_id, model_id) | 由 `CacheKey` 归一化而来 | 集群内业务索引 |

#### 4.1.2 核心流程

一次 **Allocate**（引擎代分配）的流程：

1. 按 `placement` 选内存池（HOST→host_mem_pool_，否则 npu_mem_pool_）。
2. `CalcTensorMemSize` 由 shape×dtype 算出单个 tensor 字节数。
3. 循环 `num_tensors` 次从池里 `AllocShared`，收集地址。
4. 构造 `CacheEntry`（标记 `is_owned=true`、`ext_ref_count=1`），加锁登记进 `cache_id_to_entry_`。
5. 校验 cache_keys 不越界、不重复、不与已有绑定冲突（`CheckCacheKeys`）。
6. `AddCacheIndices` 建立 cache_key → cache_id 的索引，`UpdateCacheTable` 刷新远端可查的 cache 表。

一次 **Deallocate** 的流程：

1. 查不到 cache_id → 幂等返回 SUCCESS（打日志）。
2. `is_owned=false`（即 Register 来的外部内存）→ 拒绝释放，仅打日志返回 SUCCESS。
3. BLOCKS 型 → 立即摘除索引并删除条目。
4. CACHE 型 → 置 `ext_ref_count=0`；若还有 cache_key 引用则**延迟释放**（等最后一个 `RemoveCacheKey` 到来时才真正删条目）。

#### 4.1.3 源码精读

公开数据结构（注意公开 `CacheDesc` 只有 4 个业务字段，内部版会扩展）：

[include/llm_datadist/llm_datadist.h:L122-L147](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/llm_datadist/llm_datadist.h#L122-L147) —— 定义 `CacheIndex`（cluster_id + cache_id + batch_index 三级寻址）、`CachePlacement`（kHost=0 / kDevice=1）、`CacheDesc`（placement、num_tensors、data_type、shape）与 `Cache`（cache_id + tensor_addrs 回传给用户）。

内部扩展结构：

[src/llm_datadist/common/llm_inner_types.h:L52-L79](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/common/llm_inner_types.h#L52-L79) —— `CacheKey` 携带 prompt 侧定位信息（prompt_cluster_id/prompt_cache_id/req_id/prefix_id/model_id）；`prefix_id != UINT64_MAX` 时键落入前缀索引表（prefix 复用场景）；`CacheMemType` 区分 CACHE（按 batch 维度切 stride）/ BLOCKS（按 block 切）/ MIX；内部 `CacheDesc` 额外带 `seq_len_dim_index`、`cache_mem_type`、`remote_accessible`。

台账条目：

[src/llm_datadist/common/common.h:L73-L86](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/common/common.h#L73-L86) —— `CacheEntry` 是每块 cache 的完整描述：`num_blocks`（>0 即 blocks 语义）、`stride`（batch 步长或 block 大小）、`cache_addrs`（shared_ptr 持有的地址列表）、`id_to_batch_index_and_size`（req_id → batch_index 映射）、`is_owned`（是否由引擎分配）与 `ext_ref_count`（引用计数，决定延迟释放）。

五张索引表：

[src/llm_datadist/cache_mgr/cache_manager.h:L72-L84](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/cache_mgr/cache_manager.h#L72-L84) —— `cache_id_to_entry_`（主台账）、`cache_key_to_id_` 与 `prefix_key_to_id_`（业务键→cache_id，普通键与前缀键分表）、`cache_id_and_batch_id_to_cache_key_`（反向表，RemoveCacheKey 时反查）、`cache_id_to_tensor_indices_`（部分 tensor 释放的进度记录）。

Allocate 主流程：

[src/llm_datadist/cache_mgr/cache_manager.cc:L171-L211](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/cache_mgr/cache_manager.cc#L171-L211) —— 先按 placement 选池并校验池已启用（`LLM_FEATURE_NOT_ENABLED`），再逐 tensor `AllocShared`（失败打池状态日志并返回 `LLM_OUT_OF_MEMORY`），随后持锁做 `CheckCacheKeys` + cache_id 查重 + 索引登记。注意 `cache_entry.is_owned = true` 与 `ext_ref_count = 1` 这两行，它们决定了后面 Deallocate 的延迟释放行为。

CreateCacheEntry 的三分支：

[src/llm_datadist/cache_mgr/cache_manager.cc:L213-L238](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/cache_mgr/cache_manager.cc#L213-L238) —— 由 `cache_mem_type` 推导几何信息：BLOCKS 取 `shape.front()` 为 block 数、stride = tensor_size/num_blocks（即 block size）；CACHE 取 `shape.front()` 为 batch_size、stride 为 batch 步长；MIX 两者都填。Register 外部内存时地址用空删除器的 shared_ptr 包装（`&NoDelete`），即账本只记账不持有。

Deallocate 与延迟释放：

[src/llm_datadist/cache_mgr/cache_manager.cc:L323-L353](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/cache_mgr/cache_manager.cc#L323-L353) —— 三种路径：不存在→幂等成功；非 owned→打日志跳过（外部内存归用户管）；owned 且仍被 cache_key 引用→仅置 `ext_ref_count=0`，日志明确写着 "delayed for that it is still referenced by %zu cache_key(s)"。

RemoveCacheKey 的引用收敛：

[src/llm_datadist/cache_mgr/cache_manager.cc:L361-L409](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/cache_mgr/cache_manager.cc#L361-L409) —— 摘除一个 cache_key 绑定；当 `ext_ref_count==0` 且反向表为空时才真正删除 `CacheEntry`。第 373-377 行还处理了「键指向的 cache_id 已不存在」的陈旧映射清理。这套机制保证了 pull_cache 失败时 Prompt 侧也能通过 remove_cache_key 把 cache 释放掉（见 pull_cache_sample.py 中的注释）。

双入口查询：

[src/llm_datadist/cache_mgr/cache_manager.cc:L113-L136](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/cache_mgr/cache_manager.cc#L113-L136) —— `GetCacheEntry(cache_id)` 与 `GetCacheEntry(DataCacheKey, is_prefix)` 两个重载分别服务本端发起传输（PullCache 用 cache_id 查 dst）与远端寻址（Push 时对端凭 key 找源）。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：验证「重复绑定 cache_key 会被拒绝」与「延迟释放」两条规则。
2. **操作步骤**：
   - 阅读 [CheckCacheKeys](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/cache_mgr/cache_manager.cc#L240-L257)（cache_manager.cc:240-257），找出两条参数非法分支各自的错误码与日志内容。
   - 阅读 [Deallocate](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/cache_mgr/cache_manager.cc#L323-L353)，回答：对一块 Register 来的 cache 调用 deallocate，返回值是什么？内存会被释放吗？
3. **需要观察的现象**：日志中的 `cache_key (%lu, %lu) already bound to cache_id(%ld)` 与 `cannot deallocate registered cache` 字样。
4. **预期结果**：CheckCacheKeys 对「key 数超 batch_size」「key 已绑定其他 cache」「请求内 key 重复」分别返回 `LLM_PARAM_INVALID`；Deallocate 对非 owned cache 返回 SUCCESS 但不做任何清理。
5. 若需运行验证，可配合 4.3.4 的 Python 实践在测试机执行；无硬件环境则本实践为纯源码阅读。

#### 4.1.5 小练习与答案

**练习 1**：`stride` 字段在 CACHE 型和 BLOCKS 型 cache 中分别代表什么？

答案：CACHE 型中 stride = tensor_size / batch_size，是「一个 batch 条目的字节步长」，拷贝时地址偏移按 `stride * batch_index` 计算；BLOCKS 型中 stride = tensor_size / num_blocks，是单个 block 的字节大小（block size），偏移按 `stride * block_index` 计算。见 cache_manager.cc:221-232。

**练习 2**：为什么 `id_to_batch_index_and_size` 非空时 Deallocate 要延迟？

答案：因为远端可能还会凭这些 cache_key 来寻址（例如 Decoder 还没 pull 完）。提前删除条目会让后续按 key 的查询失败。置 `ext_ref_count=0` 表示「用户已请求释放」，待最后一个 RemoveCacheKey 收敛后（cache_manager.cc:403-406）条目才真正移除。

**练习 3**：公开 `CacheDesc`（llm_datadist.h）和内部 `CacheDesc`（llm_inner_types.h）为何要拆成两个版本？

答案：公开版只暴露稳定的业务字段（placement/num_tensors/data_type/shape），内部版多出的 `cache_mem_type`、`remote_accessible`、`seq_len_dim_index` 属于实现细节；API 层（llm_datadist_impl.cc:415-422）在翻译时用默认值补齐这些内部字段（如 RegisterKvCache 固定填 `MIX` + `remote_accessible=true`），避免内部演变破坏 ABI。

### 4.2 DataCacheEngine：引擎门面与生命周期

#### 4.2.1 概念说明

DataCacheEngine 是 cache 业务的「发动机 + 调度台」：它不自己记账（交给 CacheManager），也不直接碰传输后端（交给 CommMemManager），而是负责：

- 发号：`cache_id_gen_` 原子计数器为每块 cache 分配全局递增 ID（从 1 开始，0 与负数保留，SwapBlocks 用 -1 表示「裸地址模式」）；
- 装配：Initialize 时解析 device_id、创建内存池、注册 cache table、创建 req/transfer stream；
- 编排：Register/Unregister 先走 CommMemManager（通信注册）再走 CacheManager（记账），两步都必须成功；
- 传输入口：PullCache/SwapBlocks/TransferCache 都从这里查表后交给 data_transfer 模块（下一讲展开）。

#### 4.2.2 核心流程

**Register 流程**（外部内存注册）：

1. 校验 shape 维数 ∈ [1, 33)、地址数 == num_tensors。
2. `cache_id_gen_` 发号。
3. `CalcTensorMemSize` 算 tensor 大小。
4. `CommMemManager::RegisterCacheMem` 把地址注册进传输后端（见 4.3）。
5. `CacheManager::RegisterCacheEntry` 登记台账 + 建索引。
6. 把 cache_id 回填给用户的 `Cache` 出参。

**Initialize 装配流程**（由 u6-l2 讲过的 `LLMDataDistV2::Initialize` 调用）：

解析 device_id → 读 `llm.EnableRemoteCacheAccessible` 选项 → 初始化 CacheManager → 把 cache table 缓冲区注册进 GlobalMemManager → 解析同步等待超时 → `InitializeMemoryPool`（device 池 + host 池，均由 JSON 选项驱动）→ 创建 FAST_LAUNCH|FAST_SYNC 的 req stream。

**内存池配置**：选项 `llm.MemPoolConfig` / `llm.HostMemPoolConfig` 的值是 JSON 字符串，形如 `{"memory_size": 1073741824, "page_shift": 16}`；`memory_size` 必填，`page_shift` 取值 [10,31)（页大小 = 2^shift 字节，默认 64KB）；不配置该选项则对应池不启用，Allocate 会返回 `LLM_FEATURE_NOT_ENABLED`。

#### 4.2.3 源码精读

Register 的两步编排：

[src/llm_datadist/cache_mgr/data_cache_engine.cc:L91-L118](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/cache_mgr/data_cache_engine.cc#L91-L118) —— 第 101 行 `cache_id_gen_.fetch_add(1)` 发号；第 108-113 行先 `comm_mem_manager_->RegisterCacheMem`（通信注册）再 `cache_manager_->RegisterCacheEntry`（台账登记），顺序固定：通信注册失败则不会留下孤立台账。

Initialize 装配：

[src/llm_datadist/cache_mgr/data_cache_engine.cc:L203-L226](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/cache_mgr/data_cache_engine.cc#L203-L226) —— 依次解析 device id、`kLlmOptionEnableRemoteCacheAccessible`（选项常量定义在 [llm_inner_types.h:L26](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/common/llm_inner_types.h#L26)，即 `llm.EnableRemoteCacheAccessible`）、初始化 CacheManager、把 cache table 设备缓冲注册进 GlobalMemManager（远端可凭它直接查本端 cache 位置）、初始化内存池、创建 req stream。

device 内存池创建：

[src/llm_datadist/cache_mgr/data_cache_engine.cc:L275-L301](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/cache_mgr/data_cache_engine.cc#L275-L301) —— 无 `LLM_OPTION_MEM_POOL_CONFIG` 选项则直接返回（池不启用）；否则解析 JSON、`aclrtMalloc` 一大块 `ACL_MEM_TYPE_HIGH_BAND_WIDTH` 内存、用 `LlmMemPool` 包装初始化、再把整块内存注册进 GlobalMemManager，最后 `cache_manager_->SetNpuMemPool` 把池交给账本。选项键 `llm.MemPoolConfig` 定义于 [common.h:L19](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/common/common.h#L19)。host 池（L303-L328）逻辑对称，用 `aclrtMallocHost`。

Allocate 门面：

[src/llm_datadist/cache_mgr/data_cache_engine.cc:L336-L359](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/cache_mgr/data_cache_engine.cc#L336-L359) —— 三道前置检查：至少一个池启用（否则 `LLM_FEATURE_NOT_ENABLED`）、placement 必须与已启用的池匹配（DEVICE→npu 池、HOST→host 池，否则 `LLM_PARAM_INVALID`）、shape 合法；然后发号并转交 `CacheManager::Allocate`。注意 Allocate 的内存来自池，**不再需要** CommMemManager 注册——整块池内存已一次性注册（见 L295-296）。

API 层的 RegisterKvCache 翻译：

[src/llm_datadist/api/llm_datadist_impl.cc:L404-L433](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/api/llm_datadist_impl.cc#L404-L433) —— 门卫检查（已初始化、addrs 非空且长度等于 num_tensors、地址非零），把公开 `CacheDesc` 翻译为内部版：`cache_mem_type` 固定 `MIX`、`remote_accessible=true`、`seq_len_dim_index=0`。

一个重要事实——公开 C++ `AllocateCache` 目前是占位：

[llm_datadist_impl.cc:L495-L506](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/api/llm_datadist_impl.cc#L495-L506) —— `LlmDataDist::AllocateCache` / `DeallocateCache` 直接返回 `LLM_FEATURE_NOT_ENABLED`。整条分配链路（`LLMDataDistV2::AllocateCache` → `DataCacheEngine::Allocate`，见 [llm_datadist_v2.cc:L201-L216](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/llm_datadist_v2.cc#L201-L216)）实际由 **Python v2 接口 `allocate_cache_v2`** 驱动（[src/python/llm_datadist/llm_datadist/v2/cache_manager.py:L120](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/llm_datadist/llm_datadist/v2/cache_manager.py#L120)）。C++ 用户当前获得 Cache 的可用路径只有 RegisterKvCache。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：理清「两条获得 Cache 的路径」在源码中的真实入口与差异。
2. **操作步骤**：
   - 路径 A（Register）：从 [llm_datadist_impl.cc:L404](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/api/llm_datadist_impl.cc#L404) 出发，跟踪到 `DataCacheEngine::Register`，记录它经过 CommMemManager 的那一步。
   - 路径 B（Allocate）：从 [cache_manager.py:L120](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/llm_datadist/llm_datadist/v2/cache_manager.py#L120) 的 `allocate_cache_v2` 出发，跟踪 `LLMDataDistV2::AllocateCache` → `DataCacheEngine::Allocate` → `CacheManager::Allocate`，确认它**没有**逐块调用 CommMemManager，并解释原因（提示：池整块已注册，data_cache_engine.cc:295-296）。
3. **需要观察的现象**：两条路径在 `is_owned`、`ext_ref_count` 初值、是否调用 `transfer_engine_->RegisterMem` 上的差异。
4. **预期结果**：Register 路径 is_owned=false（用户持有内存）、每块地址单独注册；Allocate 路径 is_owned=true、ext_ref_count=1、地址由池的 shared_ptr 持有、释放依赖 Deallocate/RemoveCacheKey 收敛。
5. 待本地验证（若需实际断点跟踪）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 DataCacheEngine::Allocate 要求 placement 必须匹配已启用的池，而不是自动选择？

答案：池是进程级大块资源，host 池与 device 池彼此独立启用；若 placement=HOST 但只配了 device 池，后续 Pull/地址翻译都会踩空，所以在入口处用 `LLM_PARAM_INVALID` 快速失败（data_cache_engine.cc:340-343）。

**练习 2**：`llm.MemPoolConfig` 中 `page_shift` 设为 12 和 20 分别意味着什么？合法范围是多少？

答案：页大小分别为 4KB 和 1MB；合法范围 [10, 31)，即页 1KB ~ 1GB（不含）。此外 `memory_size >> page_shift`（页数）必须小于 uint32 上限。见 data_cache_engine.cc:43-52。

**练习 3**：Initialize 中 cache table 缓冲区为什么要注册进 GlobalMemManager？

答案：cache table 是放在 device 上的「cache 寻址表」，开启 `llm.EnableRemoteCacheAccessible` 后远端可单边读取这张表来定位 cache 地址，因此它本身也必须是一块远端可访问的注册内存（data_cache_engine.cc:211-215）；CacheManager 每次 AddCacheIndices/Remove 后都会调 `UpdateCacheTable` 刷新它（cache_manager.cc:573-580）。

### 4.3 CommMemManager 与 GlobalMemManager：通信内存注册

#### 4.3.1 概念说明

CommMemManager 解决的问题是：**一块普通内存如何变成「远端可单边访问的内存」**。它对每个 cache 把地址列表交给 `TransferEngine`（HCCL 或 HIXL 后端，u6-l7 详讲）注册，换回 `mem_handle`，并维护 cache_id → handles 的台账用于解注册。GlobalMemManager 则是进程级的补充账本，管理「不属于某个 cache 的大块注册内存」（内存池整块、cache table），保证 Finalize 时统一回收。

一个容易忽略的字段是 `remote_accessible`：公开 API 翻译时固定为 true，但内部路径若把它置 false，RegisterCacheMem 会直接跳过注册——这块 cache 就只能本地 Copy，不能参与 Push/Pull。

#### 4.3.2 核心流程

**RegisterCacheMem**：

1. `remote_accessible == false` → 直接返回（不注册）。
2. 逐地址判空；按 placement 推导 `CommMemType`（DEVICE/HOST）。
3. 以 `(地址, tensor_size)` 为键查 `registered_cache_mem_` 去重集合——同一块内存被多个 cache 引用时只注册一次。
4. 未注册过则 `transfer_engine_->RegisterMem` 换 handle，记入本 cache 的 `RegisterMems`。
5. `cache_id_to_mems_[cache_id]` 收拢该 cache 的全部 handle。

**UnregisterCacheMem**：查台账 → 逐 handle 调 `transfer_engine_->UnregisterMem` → 清去重集合 → 删台账项。查不到 cache_id 仅告警并返回 SUCCESS（幂等）。

#### 4.3.3 源码精读

注册与去重：

[src/llm_datadist/cache_mgr/comm_mem_manager.cc:L73-L104](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/cache_mgr/comm_mem_manager.cc#L73-L104) —— 第 75-78 行是 remote_accessible 短路；第 88-92 行以 `(mem_ptr, tensor_size)` 为键做已注册去重（`continue` 跳过）；第 94 行 `transfer_engine_->RegisterMem` 是真正落到传输后端的一步；第 98 行把键加入去重集合。

解注册：

[src/llm_datadist/cache_mgr/comm_mem_manager.cc:L106-L125](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/cache_mgr/comm_mem_manager.cc#L106-L125) —— 逐 handle 解注册、清 `registered_cache_mem_`、删 `cache_id_to_mems_` 条目；找不到 cache_id 时仅 `LLMLOGW` 告警并返回 SUCCESS。

Finalize 兜底：

[src/llm_datadist/cache_mgr/comm_mem_manager.cc:L55-L65](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/cache_mgr/comm_mem_manager.cc#L55-L65) —— 进程退出时遍历所有 cache 的 handle 统一解注册，防止用户忘记 Unregister 造成后端资源泄漏。

GlobalMemManager：

[src/llm_datadist/cache_mgr/comm_mem_manager.cc:L16-L53](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/cache_mgr/comm_mem_manager.cc#L16-L53) —— 单例（`GetInstance`）；`RegisterMem` 转发传输后端并把 handle 存入 `handles_` 集合；`UnregisterMem` 先在集合中查（查不到仅告警返回 SUCCESS），再转发后端。DataCacheEngine::Finalize（data_cache_engine.cc:228-261）按 npu 池 → host 池 → cache table 的顺序逐一注销。

#### 4.3.4 代码实践（可运行型）

本讲的指定实践：**用 Allocate 分配一个 Cache，再用 Register 注册一块外部内存，对比两种方式的接口行为**。Python 侧提供了全部素材：

1. **实践目标**：在同一进程中先后体验 allocate_cache（池分配）与 register_cache（外部内存注册），记录接口行为差异。
2. **操作步骤**（在有昇腾硬件、已安装 torch-npu 与 llm_datadist whl 的环境）：
   - 阅读 [examples/python/pull_cache_sample.py:L90-L93](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/python/pull_cache_sample.py#L90-L93)，确认 `enable_mem_pool` 打开时通过 `mem_pool_cfg` 传入 `{"memory_size": ...}`（对应 `llm.MemPoolConfig`）；Allocate 路径必须开启它，否则返回 `LLM_FEATURE_NOT_ENABLED`。
   - 仿照 [pull_cache_sample.py:L201-L227](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/python/pull_cache_sample.py#L201-L227) 的 decoder 分支：`cache_manager.allocate_cache(cache_desc)` 分配一块 cache，打印 `cache.cache_id` 与返回的 tensor 地址。
   - 仿照 [hixl_transfer_backend_sample.py:L143-L159](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/python/hixl_transfer_backend_sample.py#L143-L159) 的写法：用 torch 分配 tensor，取 `int(tensor.data_ptr())`，调 `register_cache(cache_desc, [addr, ...], cache_key)` 注册外部内存。
   - 分别调用 `deallocate_cache(allocate 的 cache)` 与 `unregister_cache(register 的 cache_id)`，观察日志。
   - 交换实验：对 register 来的 cache 调 `deallocate_cache`，观察是否生效（提示：会命中 4.1.3 中 "cannot deallocate registered cache" 分支）。
3. **需要观察的现象**：`[Allocate] success` 与 `[Register] success` 日志中的 cache_id 序号（同一个计数器递增）；`Register global mem[...] success` 日志只出现在 register 路径（allocate 路径池整块注册发生在 init 时）；交换实验中 deallocate 对注册内存被拒绝的日志。
4. **预期结果**：两种方式都能得到可用于 pull/push 的 cache_id；差异记录成表——内存所有权（引擎 vs 用户）、是否需要 mem_pool_cfg、释放接口（deallocate_cache+remove_cache_key vs unregister_cache）、is_owned 标记。
5. 无硬件环境时：改为在 [tests/cpp/llm_datadist](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/cpp/llm_datadist) 目录中找 register/allocate 相关单测，阅读断言完成行为对比（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：两块不同 cache 注册了同一块内存地址（大小相同），`transfer_engine_->RegisterMem` 会被调用几次？

答案：1 次。`registered_cache_mem_` 以 `(mem_ptr, tensor_size)` 为键去重（comm_mem_manager.cc:88-92），第二次注册直接 `continue`；但两个 cache_id 的台账里都会记到相关条目——注意此时只有实际注册的那个 cache 的 `mem_handles` 含 handle，Unregister 另一个 cache 不会误伤这块内存。

**练习 2**：为什么需要 GlobalMemManager 和 CommMemManager 两个类，而不是一个？

答案：两者管理粒度不同。CommMemManager 按 cache_id 管理用户/引擎 tensor 的注册与解注册；GlobalMemManager 是进程级单例，管理「基础设施内存」——内存池整块缓冲与 cache table，这些不属于任何 cache，生命周期与进程相同，由 DataCacheEngine::Finalize 统一注销。

**练习 3**：UnregisterKvCache 与 DeallocateCache 的语义差异是什么？

答案：Unregister 针对注册内存，走 CommMemManager::UnregisterCacheMem + CacheManager::UnregisterCacheEntry，立即摘除（RegisterCacheEntry 登记的条目没有延迟释放逻辑，cache_manager.cc:151-161）；Deallocate 针对池分配内存，只操作 CacheManager，且有 4.1 讲的延迟释放收敛机制。对调两者都不会真正生效（前者查不到 mem 台账只告警，后者被 is_owned 检查拒绝）。

## 5. 综合实践

**任务：绘制并验证一条「Cache 全生命周期」时序。**

以 `pull_cache_sample.py` 双进程样例为蓝本，完成三件事：

1. **画时序图**：Prompt 侧与 Decoder 侧各画一条生命线，标出以下事件及其落点函数——
   - 双方 `init`（内含 DataCacheEngine::Initialize：池创建 + cache table 注册）；
   - Prompt 侧 `allocate_cache(cache_desc, [key0, key1])`（DataCacheEngine::Allocate → CacheManager::Allocate，key 索引建立）；
   - Decoder 侧 `allocate_cache(cache_desc)`（无 key，纯分配）；
   - `link`（u6-l2 的建链，交换 cache table）；
   - Decoder `pull_cache(key, cache, batch_index)`（DataCacheEngine::PullCache 开头的 GetCacheEntry 查表，data_cache_engine.cc:132-139）；
   - 收尾：Prompt `remove_cache_key` ×2 → `deallocate_cache`；Decoder `deallocate_cache`。
2. **对照源码标注**：时序图中至少 6 个事件要标注对应源码行号链接（本文 4.x.3 节已给出全部锚点）。
3. **回答验证问题**：若 Decoder 侧 pull_cache 一直不执行，Prompt 侧的 deallocate 能否立即释放内存？依据是哪几行代码？
   （参考答案：不能立即释放。`CacheManager::Deallocate` 发现 `id_to_batch_index_and_size` 非空时只置 `ext_ref_count=0` 并打 "delayed" 日志，cache_manager.cc:343-352；这正是样例注释里「pull_cache 失败也要调 remove_cache_key」的原因，pull_cache_sample.py:250-253。）

无硬件环境时，第 1、2 项为纯源码阅读仍可完成，第 3 项通过阅读代码作答。

## 6. 本讲小结

- **三条数据结构主线**：公开 `CacheDesc/Cache/CacheIndex`（llm_datadist.h）描述用户视角；内部 `CacheKey/CacheMemType`（llm_inner_types.h）扩展业务语义；`CacheEntry`（common.h）是台账里的一行，`stride/num_blocks/is_owned/ext_ref_count` 决定后续一切行为。
- **两条获得 Cache 的路径**：Register（外部内存，is_owned=false，每块地址经 CommMemManager 注册进传输后端，C++ 公开可用）与 Allocate（内存池切分，is_owned=true，池整块已注册故无需逐块注册，当前由 Python `allocate_cache_v2` 驱动，C++ 公开接口暂返回 `LLM_FEATURE_NOT_ENABLED`）。
- **CacheManager 是账本**：五张 map 维护 cache_id、cache_key、batch_index 三类索引的双向映射；Deallocate 对 owned cache 有基于 cache_key 引用计数的延迟释放机制。
- **DataCacheEngine 是门面**：发号（原子 cache_id）、装配（内存池、cache table、stream）、编排（Register 必须「通信注册→台账登记」两步都成功）。
- **CommMemManager 管注册、GlobalMemManager 管基础设施**：前者按 cache_id 记 mem_handle 并按 (addr,size) 去重；后者单例管理池与 cache table 等进程级注册内存。
- **`remote_accessible` 是注册开关**：false 时 RegisterCacheMem 直接跳过，该 cache 只能本地 Copy 不能跨集群传输。

## 7. 下一步学习建议

本讲结束时，cache 已经「登记在册、远端可访问」，下一讲 **u6-l4（Push/Pull 接口）** 将从 `DataCacheEngine::PullCache` 的查表结果出发，跟踪 KV Cache 如何经 CacheIndex 寻址远端、Blocks 版本接口如何组织批量地址。之后再进入 u6-l5（DataTransfer Job 体系）看任务拆分、u6-l7（TransferEngine 后端）看本讲 CommMemManager 调用的 `RegisterMem` 在 HIXL 侧落到何处。若想先补内存池底层，可跳读 u6-l8（LLM-DataDist 内存子系统）。

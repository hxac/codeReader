# KV Cache 传输：Push/Pull 接口

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `PushKvCache`/`PushKvBlocks` 与 `PullKvCache`/`PullKvBlocks` 四个接口的语义差异与各自的参数约束。
2. 理解 `CacheIndex` 如何通过「cluster_id + cache_id + batch_index」三级寻址定位一个远端 Cache。
3. 理解 `KvCacheExtParam` 的层区间（layer range）语义，以及批量 Blocks 传输中 `src_blocks`/`dst_blocks` 的地址组织方式。
4. 能对照 `prompt_push_cache_and_blocks` 与 `decoder_pull_cache_and_blocks` 两个样例，写出 Push 与 Pull 两侧的接口调用对照表。

## 2. 前置知识

本讲建立在前几讲的基础之上，先简要回顾：

- **PD 分离与角色**（u6-l1）：LLM-DataDist 服务于 PD 分离推理，Prompt 侧负责预填充并产生 KV Cache，Decoder 侧负责解码，需要把 KV Cache 从 Prompt 搬到 Decoder。`LlmDataDist` 是公开类，`kPrompt`/`kDecoder` 只是业务标签，真正的通信由建链（u6-l2）决定。
- **Cache 的两条获得路径**（u6-l3）：`RegisterKvCache` 注册外部内存获得 `cache_id`（写入 CacheManager 台账并经 TransferEngine 注册为远端可单边访问），`AllocateCache` 从内存池切分（C++ 公开接口当前返回 `LLM_FEATURE_NOT_ENABLED`）。**本讲的四个传输接口全部以 `cache_id` 标识本地端。**
- **单边通信**（u1-l1）：底层 HIXL 允许一端直接 READ/WRITE 对端注册过的内存，对端 CPU 不参与。Push 对应「本地 WRITE 到远端」，Pull 对应「本地 READ 远端」——但注意在 LLM-DataDist 语义里，无论 Push 还是 Pull，**发起方都是调用接口的这一侧**，远端只被动提供已注册的内存。
- **错误码**（u6-l1）：判断统一用 `== LLM_SUCCESS`；本讲会遇到的常见错误有 `LLM_KV_CACHE_NOT_EXIST`（本地 cache_id 查不到）、`LLM_NOT_YET_LINK`（还没建链）、`LLM_PARAM_INVALID`（参数约束不满足）。

一个形象的比喻：`cache_id` 像「本地的房间号」，`CacheIndex` 像「对方小区 + 楼栋 + 房间号」的三段式地址。Push 是把本地家具送过去，Pull 是从对方地址把家具取回来——无论送还是取，搬家的车都由**发起方**开出。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/llm_datadist/llm_datadist.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/llm_datadist/llm_datadist.h) | 公开头文件：`CacheIndex`、`KvCacheExtParam` 结构体与 Push/Pull/Copy 接口声明 |
| [src/llm_datadist/api/llm_datadist_impl.cc](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/api/llm_datadist_impl.cc) | Pimpl 实现层：参数校验、`CacheIndex→CacheKey` 翻译、层区间展开、逐层循环下发 |
| [src/llm_datadist/common/llm_inner_types.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/common/llm_inner_types.h) | 内部数据结构：`CacheKey`、`PullCacheParam`、`TransferCacheConfig`、`TransferBlockConfig` |
| [src/llm_datadist/llm_datadist_v2.cc](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/llm_datadist_v2.cc) | V2 实现层：恢复 ACL context、统计埋点，转交 DataCacheEngine |
| [src/llm_datadist/cache_mgr/data_cache_engine.cc](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/cache_mgr/data_cache_engine.cc) | 查本地 cache 台账、按 cluster_id 找 CommEntity、选择传输客户端 |
| [examples/cpp/prompt_push_cache_and_blocks.cpp](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/prompt_push_cache_and_blocks.cpp) | Push 侧样例：注册 4 个 tensor 的 Cache，混合使用 PushKvBlocks 与 PushKvCache |
| [examples/cpp/decoder_pull_cache_and_blocks.cpp](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/decoder_pull_cache_and_blocks.cpp) | Pull 侧样例：注册本地目标 Cache，Pull 后逐 tensor 校验数据 |

## 4. 核心概念与源码讲解

### 4.1 Push/Pull 接口全景：四个接口与一组正交维度

#### 4.1.1 概念说明

LLM-DataDist 的 KV Cache 传输接口共 6 个，按「传输粒度 × 传输方向」排成一张矩阵：

| | 连续 Cache（按 batch/层） | 离散 Blocks（按块） |
| --- | --- | --- |
| **Push（推到远端）** | `PushKvCache` | `PushKvBlocks` |
| **Pull（从远端拉）** | `PullKvCache` | `PullKvBlocks` |
| 本地拷贝 | `CopyKvCache` | `CopyKvBlocks` |

其中 Copy 系列当前是占位实现，直接返回 `LLM_FEATURE_NOT_ENABLED`（见 [llm_datadist_impl.cc:542-563](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/api/llm_datadist_impl.cc#L542-L563)），真正可用的是 Push/Pull 四个。

「连续 Cache」与「离散 Blocks」对应推理引擎中两种 KV Cache 组织方式：PagedAttention 类引擎把 KV Cache 切成固定大小的 block（块号不连续），逐条传输就要用 Blocks 版本；整段连续布局则用 Cache 版本一次搬完。两种粒度可以混用，样例里就演示了先 Blocks 后 Cache。

#### 4.1.2 核心流程

一次跨进程 KV Cache 传输的调用合同：

```text
Prompt 侧（Push）                          Decoder 侧（Pull）
─────────────────────                      ─────────────────────
Initialize(options)                        Initialize(options)
RegisterKvCache(src_desc, src_addrs)       RegisterKvCache(dst_desc, dst_addrs)
        └─ 得到本地 cache_id                       └─ 得到本地 cache_id
        │                                          │ 等待/通知（样例用 sleep，业务可自选）
LinkLlmClusters(...)  ←──── 双向建链 ────→  LinkLlmClusters(...)
PushKvBlocks(src_cache, dst_index,         PullKvBlocks(src_index, dst_cache,
             src_blocks, dst_blocks)                    src_blocks, dst_blocks)
PushKvCache(src_cache, dst_index, ...)     PullKvCache(src_index, dst_cache, ...)
        │                                          │
UnlinkLlmClusters(...)                     UnregisterKvCache(cache_id)
UnregisterKvCache(cache_id)                Finalize()
Finalize()
```

关键合同条款（由源码校验保证，违反即返回 `LLM_PARAM_INVALID`）：

1. **两端都要注册 Cache**：Push 需要 Prompt 侧注册源、Decoder 侧注册目的地；Pull 需要 Decoder 侧注册目的地、Prompt 侧注册源。`cache_id` 只在各自进程内有意义。
2. **先建链后传输**：未建链时 Pull 会得到 `LLM_NOT_YET_LINK`。
3. **Push 只支持 device 内存**（`CachePlacement::kDevice`），且 `size` 目前只支持 -1（全部）。
4. **Blocks 模式下 CacheIndex 的 `batch_index` 只能为 0**。

#### 4.1.3 源码精读

四个接口在公开头文件中的声明：

- [llm_datadist.h:229-239](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/llm_datadist/llm_datadist.h#L229-L239) — `PullKvCache` 声明：入参为远端 `src_cache_index`、本地 `dst_cache`、目标 batch 槽位、大小（-1 表示全量）、扩展参数。
- [llm_datadist.h:242-252](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/llm_datadist/llm_datadist.h#L242-L252) — `PullKvBlocks` 声明：额外携带 `src_blocks`/`dst_blocks` 两个 block 编号列表。
- [llm_datadist.h:279-288](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/llm_datadist/llm_datadist.h#L279-L288) — `PushKvCache` 声明：入参顺序与 Pull 镜像——本地 `src_cache` 在前，远端 `dst_cache_index` 在后。
- [llm_datadist.h:291-301](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/llm_datadist/llm_datadist.h#L291-L301) — `PushKvBlocks` 声明：`src_cache` + `dst_cache_index` + 两个 block 列表。

注意参数顺序的「本地在前、远端在后」约定：Pull 是 `(src_cache_index, dst_cache, ...)`，Push 是 `(src_cache, dst_cache_index, ...)`——第一个参数永远是**源**，第二个永远是**目的地**。

公开类外壳只做日志与 `impl_` 判空，然后转发给 Pimpl 实现层，例如 [llm_datadist_impl.cc:580-597](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/api/llm_datadist_impl.cc#L580-L597) 是 `PushKvBlocks` 的外壳：打 start 日志 → 调 `impl_->PushKvBlocks` → 失败打详细错误（含 block 列表）→ 成功打 success 日志。

#### 4.1.4 代码实践

**实践目标**：不看实现，仅凭头文件注释整理四个接口的参数语义。

1. 打开 [include/llm_datadist/llm_datadist.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/llm_datadist/llm_datadist.h)，找到 L229-L301 区间。
2. 为每个接口列一张参数表：参数名、类型、方向（本地/远端）、默认值。
3. 特别注意两个默认值：`PullKvCache` 的 `size = -1` 与 `PushKvCache` 的 `size = -1`，注释都写明「-1 表示完整数据」。
4. **观察现象**：`PushKvCache` 与 `PullKvCache` 都有 `KvCacheExtParam ext_param = {}` 默认参数，而 Blocks 版本同样有——思考默认构造的 `KvCacheExtParam`（layer range 为 `{-1,-1}`）意味着什么（答案见 4.3）。
5. **预期结果**：得到一张四接口参数对照表，为后续阅读样例做准备。本实践为纯阅读型，无需运行环境。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `PushKvCache` 的参数顺序是 `(src_cache, dst_cache_index)` 而 `PullKvCache` 是 `(src_cache_index, dst_cache)`？

**答案**：两个接口都遵循「第一个参数是源、第二个参数是目的地」的约定。Push 的源在本地（`Cache`），目的地在远端（`CacheIndex`）；Pull 的源在远端（`CacheIndex`），目的地在本地（`Cache`）。参数类型本身就标注了该端点在哪一侧。

**练习 2**：`CopyKvCache`/`CopyKvBlocks`（本地拷贝）现在能用吗？

**答案**：不能。二者在实现层是占位实现，忽略全部入参并返回 `LLM_FEATURE_NOT_ENABLED`（[llm_datadist_impl.cc:542-563](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/api/llm_datadist_impl.cc#L542-L563)）。类似地 `AllocateCache`/`DeallocateCache` 公开接口也返回同一错误码（[llm_datadist_impl.cc:495-506](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/api/llm_datadist_impl.cc#L495-L506)）。

### 4.2 CacheIndex：远端 Cache 的三级寻址

#### 4.2.1 概念说明

`CacheIndex` 是回答「远端那块 Cache 在哪」的结构体，只有三个有效字段：

```cpp
struct CacheIndex {
  uint64_t cluster_id;    // 远端集群 id（建链时 ClusterInfo.remote_cluster_id）
  int64_t cache_id;       // 远端进程 RegisterKvCache 得到的 cache id
  uint32_t batch_index;   // cache 内的 batch 槽位
  uint8_t reserved[128];  // ABI 预留，不要读写
};
```

三级寻址分别解决三个问题：

| 字段 | 解决的问题 | 底层落到哪里 |
| --- | --- | --- |
| `cluster_id` | 走哪条链路？ | u6-l2 建链时建立的 `CommEntity`（按 remote_cluster_id 索引） |
| `cache_id` | 链路对端的哪块内存？ | 远端建链握手时同步过来的 cache table 中的条目 |
| `batch_index` | 该 Cache 内哪个 batch 槽位？ | cache table 条目内部的偏移计算 |

**跨进程 id 一致性的前提**：调用方写的 `cache_id` 必须与远端注册时拿到的 id 相同。样例两端各自只注册一个 Cache，且 id 都从 1 起编号，所以两端都写 `cache_id = 1` 恰好一致；实际业务中这依赖两侧约定或独立的通知通道。

#### 4.2.2 核心流程

`CacheIndex` 在实现层被翻译成内部 `CacheKey`，随传输请求一起下发：

```text
CacheIndex                       CacheKey（内部）
─────────                        ──────────────
cluster_id      ──────────→      prompt_cluster_id
cache_id        ──────────→      prompt_cache_id
batch_index     ──────────→      prompt_batch_index   （仅连续 Cache 模式携带）
```

Pull 路径上，`cluster_id` 先用来查建链实体：`GetEntityByRemoteClusterId(cache_key.prompt_cluster_id)` 拿不到就报 `LLM_NOT_YET_LINK`；`cache_key` 与本地 `cache_id` 一起交给传输客户端，由后者从 cache table 中解析出远端地址。Push 路径上，三个字段被平铺进 `TransferCacheConfig` 的 `cluster_id`/`dst_cache_id`/`dst_batch_index` 字段。

#### 4.2.3 源码精读

**结构体定义**：[llm_datadist.h:122-127](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/llm_datadist/llm_datadist.h#L122-L127) — 公开的 `CacheIndex` 定义，注意 `cache_id` 是 `int64_t`、`batch_index` 是 `uint32_t`。

**CacheIndex → CacheKey 翻译**：[llm_datadist_impl.cc:68-76](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/api/llm_datadist_impl.cc#L68-L76) — `ToCacheKey` 把三个字段搬进内部 `CacheKey`。第二个参数 `dst_is_cache` 决定是否携带 `batch_index`：连续 Cache 模式传 true；Blocks 模式传 `dst_blocks.empty()`——正常 Blocks 调用不为空，即 false，batch_index 不进 key（因为 Blocks 模式本来就限定它为 0）。

**内部 CacheKey 定义**：[llm_inner_types.h:52-60](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/common/llm_inner_types.h#L52-L60) — 内部键比公开 `CacheIndex` 多了 `req_id`、`prefix_id`、`model_id` 等字段，供更复杂的寻址场景使用；公开层只填前三项。

**cluster_id 落到建链实体**：[data_cache_engine.cc:141-147](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/cache_mgr/data_cache_engine.cc#L141-L147) — Pull 时用 `cache_key.prompt_cluster_id` 查 `CommEntity`，查不到报 `LLM_NOT_YET_LINK`；拿到后还要加该实体的 Pull 互斥锁并确认实体未被销毁（FSM 未进 DESTROYED 态）。

**自拉自被拒绝**：[llm_datadist_v2.cc:226-227](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/llm_datadist_v2.cc#L226-L227) — Pull 侧校验 `cluster_id_ != cache_key.prompt_cluster_id`，即不允许从自己集群拉数据（本地拷贝请走 Copy 系列）。

**本地 cache_id 查表**：[data_cache_engine.cc:135-138](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/cache_mgr/data_cache_engine.cc#L135-L138) — 用本地 `cache_id` 查 CacheManager 台账，查不到报 `LLM_KV_CACHE_NOT_EXIST`——这就是「本地端用 cache_id、远端端用 CacheIndex」两条寻址路径的分岔点。

#### 4.2.4 代码实践

**实践目标**：用一个故意写错的 `CacheIndex` 验证错误码路径。

1. 阅读型实践：在 [data_cache_engine.cc:132-179](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/cache_mgr/data_cache_engine.cc#L132-L179) 的 `PullCache` 中，按顺序列出所有会提前返回的错误码及触发条件。
2. 假设（示例代码，未运行）：把 decoder 样例中的 `cache_index.cluster_id = kPromptClusterId` 改成 `99`（未建链的集群），再调用 `PullKvBlocks`。
3. **需要观察的现象**：接口应返回 `LLM_NOT_YET_LINK`（0x5010B007），错误日志包含 `current cluster is not linked with remote cluster:99`。
4. **预期结果**：三种典型错误各对应一个入口——`LLM_KV_CACHE_NOT_EXIST`（本地 dst cache_id 无效）、`LLM_NOT_YET_LINK`（cluster_id 无链路）、`LLM_PARAM_INVALID`（自拉自等）。待本地验证（需要双端环境）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ToCacheKey` 需要第二个参数 `dst_is_cache`，而不是无脑把 `batch_index` 抄进去？

**答案**：Blocks 模式下 `batch_index` 被限定为 0（见 4.4 的校验），携带与否不影响寻址；而 V2 层 `PullBlocks` 又会再次校验 `cache_key.prompt_batch_index == 0`（[llm_datadist_v2.cc:244-246](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/llm_datadist_v2.cc#L244-L246)）。用 `dst_blocks.empty()` 作为开关，让同一份翻译函数同时服务 Cache 与 Blocks 两种语义。

**练习 2**：Prompt 与 Decoder 两端进程各自 `RegisterKvCache` 都得到 `cache_id = 1`，会不会冲突？

**答案**：不会冲突。`cache_id` 的作用域是各自进程：本端的 `cache_id` 用于查本地台账，`CacheIndex.cache_id` 才是远端的 id。两个「1」分别活在两个进程的 CacheManager 里，靠 `cluster_id` 区分方向。样例恰好两端第一个注册的 Cache id 相同，业务上应保证调用方写入的 `cache_id` 与对端注册结果一致（通常 id 从 1 递增，两端注册顺序一致即可对齐）。

### 4.3 Push 系列实现精读：逐层拆解与 TransferCache 下发

#### 4.3.1 概念说明

Push 的两个接口共享同一条下发管线：**参数校验 → 组装 `TransferCacheConfig`（+`TransferBlockConfig`）→ 按层循环调用 V2 层的 `TransferCache`**。理解 Push 的关键是「层（layer）」这个概念：

- 一个 Cache 由 `num_tensors` 个 tensor 组成，按 `KvCacheExtParam.tensor_num_per_layer`（默认 2，即 K 和 V）切成若干层。
- `src_layer_range`/`dst_layer_range` 指定只传哪些层，两端层号可以不同（如源第 0 层写到目的地第 3 层），但**宽度必须一致**。
- 层区间为 `{-1,-1}`（默认）表示「全部层」。

Blocks 的地址组织：`src_blocks` 与 `dst_blocks` 是两个等长的 block 编号列表，按**下标一一配对**——`src_blocks[i]` 的数据写到 `dst_blocks[i]`。这就是「批量 Blocks 传输的地址组织方式」：不要求 block 连续，也允许源与目的地的编号重排。

#### 4.3.2 核心流程

`PushKvCache`/`PushKvBlocks` 的共同流程：

```text
外壳（公开类）: 日志 + impl_ 判空 + 转发
  └─ Pimpl 实现:
       ① 校验: 已初始化 / placement==kDevice / (Cache版) size==-1 /
          blocks 非空且等长 / dst batch_index==0 /
          layer range 合法且 src/dst 宽度一致(check_range_same=true)
       ② 组装 TransferCacheConfig:
            src_cache_id   ← 本地 Cache.cache_id
            cluster_id / dst_cache_id / dst_batch_index ← CacheIndex 三字段
            type = 2 (kCacheKeyByIdType, 按 cache id 寻址)
            tensor_num_per_layer ← ext_param
       ③ (Blocks版) 组装 TransferBlockConfig: src_blocks / dst_blocks
       ④ PushData 按层循环:
            - 指定单层 (first==second 且 >=0): 只调一次 TransferCache
            - 全部层 (first==-1): 从层 0 循环到 GetCacheMaxLayer
            - 指定多层区间: 从 src.first 循环到 src.second
            每轮设置 layer_index / dst_layer_index 并调 V2 的 TransferCache
  └─ V2 层: 恢复 ACL context + transfer_mutex_ 串行化 → DataCacheEngine::TransferCache
```

最大层号的计算：当 `num_tensors` 能被 `tensor_num_per_layer` 整除时，`max_layer = num_tensors / tensor_num_per_layer - 1`（闭区间末尾）；不能整除时把余数部分当作一层。

#### 4.3.3 源码精读

**PushKvCache 实现**：[llm_datadist_impl.cc:338-364](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/api/llm_datadist_impl.cc#L338-L364) — 依次校验初始化状态、`placement == kDevice`（"Only support push to device cache"）、`size == -1`、层参数（`check_range_same=true` 强制 src/dst 宽度一致），然后把 `CacheIndex` 三字段平铺进 `TransferCacheConfig`，`type = 2` 表示按 cache id 寻址。

**PushKvBlocks 实现**：[llm_datadist_impl.cc:366-402](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/api/llm_datadist_impl.cc#L366-L402) — 在 PushKvCache 的校验之外，额外要求 `src_blocks` 非空、与 `dst_blocks` 等长、`dst_cache_index.batch_index == 0`；两个 block 列表装进 `TransferBlockConfig`。注意它**不设置** `batch_index`/`dst_batch_index`（保持默认 0）。

**按层循环的 PushData**：[llm_datadist_impl.cc:306-336](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/api/llm_datadist_impl.cc#L306-L336) — 三分支：指定单层一次调用；`-1` 起步则从 0 循环到 `GetCacheMaxLayer` 计算出的最大层；指定区间则循环到 `src_layer_range.second`。每轮同步递增 `layer_index` 与 `dst_layer_index`，实现源/目的地层号错位对齐。

**最大层号计算**：[llm_datadist_impl.cc:98-111](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/api/llm_datadist_impl.cc#L98-L111) — `GetCacheMaxLayer`：整除时 `num_tensors / tensor_num_per_layer - 1`，非整除时把尾巴当一层。

**V2 层 TransferCache**：[llm_datadist_v2.cc:302-323](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/llm_datadist_v2.cc#L302-L323) — 用 `TemporaryRtContext` 恢复初始化时保存的 ACL context（因为调用方线程可能没有绑定设备上下文），经 `transfer_mutex_` 串行化后转交 `DataCacheEngine::TransferCache`，并做耗时统计埋点。

**内部配置结构**：[llm_inner_types.h:93-111](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/common/llm_inner_types.h#L93-L111) — `TransferCacheConfig`（源/目的 cache id、batch、layer、cluster）与 `TransferBlockConfig`（block 编号列表），是 Push 管线的「集装箱」。

**样例中的 Push 用法**：[prompt_push_cache_and_blocks.cpp:165-204](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/prompt_push_cache_and_blocks.cpp#L165-L204) — `PushCache` 函数演示两种粒度：第一段对 4 个 tensor 逐层调用 `PushKvBlocks`（`tensor_num_per_layer=1`，即每层一个 tensor，`src_layer_range={i,i}` 单层推送，blocks `{5,6,7}→{5,6,7}` 编号一一对应）；第二段用 `PushKvCache` 一次推整个 batch 槽位（`batch_index=4`、`tensor_num_per_layer=4`、layer range `{0,0}` 且 size=-1 表示该层全部 4 个 tensor）。

#### 4.3.4 代码实践

**实践目标**：通过修改样例参数理解 Push 的层语义。

1. 阅读 [prompt_push_cache_and_blocks.cpp:175-185](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/prompt_push_cache_and_blocks.cpp#L175-L185)：循环变量 `i` 同时出现在 `src_layer_range` 与 `dst_layer_range`，且 `tensor_num_per_layer=1`。手算：`num_tensors=4` 时这个循环会触发几次 `TransferCache`？
2. 修改练习（示例代码）：把 `param.src_layer_range = {i, i}` 改为 `{i, i}` 保持不变、`param.dst_layer_range` 改为 `{i + 1, i + 1}`，即源层 i 推到目的地层 i+1。
3. **需要观察的现象**：Push 接口本身仍返回 `LLM_SUCCESS`（两端层号允许错位），但 decoder 侧校验逻辑（`CheckBuffers`）不改的话数据对位会变化；若把 `tensor_num_per_layer` 改成 0，应立即返回 `LLM_PARAM_INVALID`。
4. **预期结果**：第 1 步答案是 4 次（每层一次）。第 3 步的参数非法路径可直接从 [llm_datadist_impl.cc:87-89](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/api/llm_datadist_impl.cc#L87-L89) 的校验推出，无需硬件也可确认。运行验证待本地环境。

#### 4.3.5 小练习与答案

**练习 1**：`PushKvCache` 的 `size` 参数注释说「-1 表示推送源 cache 的完整数据」，那传 `size = 1024` 会怎样？

**答案**：返回 `LLM_PARAM_INVALID`。与 `PullKvCache`（`size == -1 || size > 0` 都合法）不同，Push 实现里显式校验 `size == -1`（[llm_datadist_impl.cc:344](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/api/llm_datadist_impl.cc#L344)，"size only support -1 currently"）。部分传输请改用层区间（`ext_param`）表达。

**练习 2**：`src_blocks = {5, 6, 7}`、`dst_blocks = {9, 3, 0}`，数据怎么流？

**答案**：按数组下标配对：源 block 5 → 目的地 block 9，源 6 → 目的地 3，源 7 → 目的地 0。唯一硬约束是两列表等长（[llm_datadist_impl.cc:376-378](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/api/llm_datadist_impl.cc#L376-L378)），编号无需有序也无需互相对应。

**练习 3**：`GetCacheMaxLayer` 对 `num_tensors=4, tensor_num_per_layer=3` 返回几？

**答案**：返回 1。因为 4 不能被 3 整除，`4 / 3 = 1`，把余下的 tensor 当作额外一层，即层 0（3 个 tensor）+ 层 1（1 个 tensor），最大层号是 1（[llm_datadist_impl.cc:98-111](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/api/llm_datadist_impl.cc#L98-L111)）。

### 4.4 Pull 系列实现精读：PullCacheParam 与查表下发

#### 4.4.1 概念说明

Pull 与 Push 管线对称但容器不同：Push 把配置装进 `TransferCacheConfig` 逐层下发，Pull 则装进 `PullCacheParam` **一次下发**（不做层循环）。`PullCacheParam` 的关键字段：

- `size`/`batch_index`：连续 Cache 模式的传输参数；
- `prompt_blocks`/`decoder_blocks`：Blocks 模式的两个编号列表（沿用 Prompt/Decoder 命名，因为 Pull 的源端永远是 Prompt 侧）；
- `src_tensor_indices`/`dst_tensor_indices`：层区间展开后的 tensor 下标序列，是实现「按层部分拉取」的真正载体；
- `tensor_num_per_layer`：层的切分粒度。

层区间在 Pull 侧的处理方式与 Push 不同：Push 是**循环多次调用**，Pull 是**一次调用、展开成 indices 列表**——把 `[a,b]` 区间乘以 `tensor_num_per_layer` 展开成一串连续 tensor 下标。

#### 4.4.2 核心流程

```text
PullKvCache / PullKvBlocks
  ① 校验: 已初始化 / (Cache版) size==-1 或 >0 / (Blocks版)
     src_cache_index.batch_index==0 / CheckKvCacheExtParam(不强制宽度一致)
  ② 组装 PullCacheParam:
       prompt_blocks ← src_blocks, decoder_blocks ← dst_blocks (Blocks版)
       batch_index / size ← 入参 (Cache版)
       tensor_num_per_layer ← ext_param
       CalcIndicesWithValidRanges: 层区间 → src/dst_tensor_indices
  ③ ToCacheKey(src_cache_index, ...) → CacheKey
  ④ V2 层 PullCache / PullBlocks:
       PullBlocks 额外校验 blocks 非空、等长、batch_index==0 后复用 PullCache
  ⑤ DataCacheEngine::PullCache:
       本地 cache_id 查台账 (失败→LLM_KV_CACHE_NOT_EXIST)
       cluster_id 查 CommEntity (失败→LLM_NOT_YET_LINK)
       按配置选择传输客户端 → 底层后端 (HCCL 或 HIXL) 执行远端读
```

#### 4.4.3 源码精读

**PullKvCache 实现**：[llm_datadist_impl.cc:264-278](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/api/llm_datadist_impl.cc#L264-L278) — 校验 `size == -1 || size > 0` 与层参数（注意这里 `CheckKvCacheExtParam` 不带 `check_range_same`，Pull 不强制 src/dst 层宽一致，因为展开逻辑天然按各自区间计算），组装 `PullCacheParam` 后调 `PullCache(dst_cache.cache_id, ToCacheKey(..., true), ...)`——`true` 表示携带 batch_index 的连续 Cache 语义。

**PullKvBlocks 实现**：[llm_datadist_impl.cc:244-262](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/api/llm_datadist_impl.cc#L244-L262) — 校验 `src_cache_index.batch_index == 0`，把 `src_blocks`→`prompt_blocks`、`dst_blocks`→`decoder_blocks`，最后 `ToCacheKey(src_cache_index, dst_blocks.empty())`：正常调用 blocks 非空故为 false，batch_index 不进 key。

**层区间展开**：[llm_datadist_impl.cc:114-132](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/api/llm_datadist_impl.cc#L114-L132) — `CalcIndicesWithValidRanges`：任一侧 layer range 首值 < 0 就直接返回（默认全量）；否则按下式展开：

\[ \text{indices\_beg} = \text{first} \times \text{tensor\_num\_per\_layer}, \quad \text{range} = (\text{second} - \text{first} + 1) \times \text{tensor\_num\_per\_layer} \]

源与目的地区间各自展开成 `src_tensor_indices`/`dst_tensor_indices`，二者长度由各自区间决定。

**层参数校验**：[llm_datadist_impl.cc:78-96](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/api/llm_datadist_impl.cc#L78-L96) — `CheckKvCacheExtParam`：两侧 range 都需 `>= -1` 且 `first <= second`，`tensor_num_per_layer > 0`；可选的 `check_range_same` 仅 Push 使用。

**V2 层的二级校验与转发**：[llm_datadist_v2.cc:237-248](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/llm_datadist_v2.cc#L237-L248) — `PullBlocks` 再校验一次 blocks 非空、等长、`prompt_batch_index == 0`，然后**直接复用 `PullCache`**（[llm_datadist_v2.cc:218-235](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/llm_datadist_v2.cc#L218-L235)）：后者校验初始化、`tensor_num_per_layer > 0`、禁止自拉自，做耗时统计后转交 DataCacheEngine。

**引擎层查表与客户端选择**：[data_cache_engine.cc:132-179](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/cache_mgr/data_cache_engine.cc#L132-L179) — PullCache 的完整决策：查本地台账 → 查建链实体并加 Pull 锁 → 恢复 ACL context → 按 `access_remote_cache_` 开关选择 `PullCacheByGet`（直接访问远端 cache table）路径，或按 `cache_entry.placement` 选 `D2HDataTransferClient`（HOST）与 `DataTransferClient`（DEVICE）。

**PullCacheParam 定义**：[llm_inner_types.h:81-91](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/common/llm_inner_types.h#L81-L91) — 上述所有字段的定义处。

**样例中的 Pull 用法**：[decoder_pull_cache_and_blocks.cpp:183-208](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/decoder_pull_cache_and_blocks.cpp#L183-L208) — `PullCache` 函数：先 `PullKvBlocks`（源端 Prompt cluster 0 的 cache 1，blocks `{1,2,3}→{1,2,3}`，未传 ext_param 即默认全量层），再 `PullKvCache`（batch 0 整槽拉取）。拉完后 [decoder_pull_cache_and_blocks.cpp:164-181](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/decoder_pull_cache_and_blocks.cpp#L164-L181) 的 `CheckBuffers` 把每个 tensor 拷回主机，逐元素对比 `iota` 填充的期望值。

#### 4.4.4 代码实践

**实践目标**：写出 Push 与 Pull 两侧的接口调用对照表（本讲综合实践的前半部分）。

1. 通读两个样例的主流程函数：[prompt_push_cache_and_blocks.cpp:230-302](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/prompt_push_cache_and_blocks.cpp#L230-L302)（`RunPromptSample`）与 [decoder_pull_cache_and_blocks.cpp:261-315](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/decoder_pull_cache_and_blocks.cpp#L261-L315)（`RunDecoderSample`）。
2. 逐行对齐两侧的编号步骤（初始化/注册/建链/传输/断链/清理），标出哪一步只在一侧存在。
3. **需要观察的现象**：两侧步骤几乎镜像，差异集中在三处——角色与监听端口（Prompt 26000 / Decoder 26001）、`sleep` 的位置（Prompt 等对端注册、Decoder 等对端写入）、传输调用（Push vs Pull）。另外注意 Prompt 端最后还要通过**自建 TCP 控制通道**（端口 26003）通知 Decoder「Unlink 完成」——LLM-DataDist 本身不提供跨进程完成通知，样例用裸 socket 补齐。
4. **预期结果**：得到一张类似下表的对照表（这是本实践的参考产出，请自行补全参数列）：

| 步骤 | Prompt 侧（Push） | Decoder 侧（Pull） |
| --- | --- | --- |
| 构造 | `LlmDataDist(0, kPrompt)` | `LlmDataDist(1, kDecoder)` |
| 初始化 | `Initialize`（监听 26000） | `Initialize`（监听 26001） |
| 注册 | `RegisterKvCache`（源 cache，填 iota 数据） | `RegisterKvCache`（目标 cache，未初始化数据） |
| 等待 | sleep 5s 等对端注册 | sleep 5s 等对端写入 |
| 建链 | `LinkLlmClusters`（remote=1） | `LinkLlmClusters`（remote=0） |
| 传输 | `PushKvBlocks`（层 0..3，blocks 5,6,7）+ `PushKvCache`（batch 4 全量） | `PullKvBlocks`（blocks 1,2,3）+ `PullKvCache`（batch 0） |
| 校验 | 无 | `CheckBuffers` 逐元素对比 |
| 断链/清理 | `Unlink` → socket 通知 → `UnregisterKvCache` → `Finalize` | `Unlink` → socket 通知 → `UnregisterKvCache` → `Finalize` |

5. 本实践为源码阅读型，无需昇腾环境即可完成；运行双进程样例属于 u7-l3 的实战内容。

#### 4.4.5 小练习与答案

**练习 1**：`PullKvCache` 传 `size = 0` 会怎样？传 `size = 512` 呢？

**答案**：`size = 0` 返回 `LLM_PARAM_INVALID`（校验要求 `size == -1 || size > 0`，[llm_datadist_impl.cc:267-268](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/api/llm_datadist_impl.cc#L267-L268)）；`size = 512` 合法，只拉前 512 字节。对比 Push 只允许 -1，Pull 是四个接口中唯一支持部分字节传输的。

**练习 2**：Pull 侧 `CheckKvCacheExtParam` 为什么不传 `check_range_same = true`？

**答案**：Pull 的层区间处理是把源与目的地区间**各自**展开成 tensor indices 列表（`CalcIndicesWithValidRanges`），两侧下标序列独立生成，天然允许源传 2 层写到目的地 3 层这类错位；而 Push 是按层号逐轮配对循环，源/目的层号必须同步递增，宽度不一致就无法配对，所以强制等宽（[llm_datadist_impl.cc:346](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/api/llm_datadist_impl.cc#L346)）。

**练习 3**：decoder 样例的 `PullKvBlocks` 没有传 `ext_param`（用默认值），此时拉哪些层？

**答案**：拉全部 tensor。默认 `KvCacheExtParam` 的 `src_layer_range = {-1,-1}`，`CalcIndicesWithValidRanges` 检测到 `first < 0` 直接返回，`src_tensor_indices`/`dst_tensor_indices` 保持为空，下游按「全量」处理（[llm_datadist_impl.cc:115-117](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/api/llm_datadist_impl.cc#L115-L117)）。

## 5. 综合实践

**任务：把「Push/Pull 对照表」升级为一份带约束注释的接口速查卡，并用它审查一段错误调用。**

1. **制卡**：综合 4.1–4.4 的内容，制作一张速查卡，每个接口一行，列包括：粒度（Cache/Blocks）、方向参数顺序、size 约束、batch_index 约束、层区间约束、典型错误码。
2. **审查**：阅读下面这段示例代码（非项目代码，为本实践虚构），找出全部会返回非 `LLM_SUCCESS` 的调用并说明理由：

```cpp
// 示例代码：供审查练习用，非项目源码
Cache src;                       // placement == CachePlacement::kHost
CacheIndex idx{1, 1, 3};         // cluster 1, cache 1, batch 3
llm_datadist.PushKvCache(src, idx, 0, 2048);                          // A
llm_datadist.PushKvBlocks(src, idx, {}, {});                          // B
KvCacheExtParam p;
p.src_layer_range = {2, 4};
p.dst_layer_range = {0, 1};
llm_datadist.PushKvCache(src, idx, 0, -1, p);                         // C
CacheIndex idx0{1, 1, 0};
std::vector<uint64_t> sb{1, 2}, db{3, 4, 5};
llm_datadist.PushKvBlocks(src, idx0, sb, db);                         // D
```

3. **参考答案**：
   - **A 错**，两处：placement 为 kHost（Push 只支持 kDevice）且 `size=2048`（Push 只支持 -1）。
   - **B 错**：`src_blocks` 为空（"push from non-block cache is not supported yet"）。
   - **C 错**：src 层宽 3（2..4）、dst 层宽 2（0..1），Push 要求两侧等宽（`check_range_same=true`）。
   - **D 错**：`src_blocks`(2) 与 `dst_blocks`(3) 长度不等。
   - 四个调用全失败，正好对应 Push 路径的四道参数闸门。
4. **延伸（可选，需环境）**：在有双端昇腾环境时，按 4.4.4 的对照表运行两个样例（`examples/run_example.sh` 或直接执行编译产物），用 `ASCEND_SLOG_PRINT_TO_STDOUT=1` 观察 `[PushKvBlocks] success` / `[PullKvBlocks] success` 日志中打印的 cluster_id、cache_id 与 block 列表，与速查卡互相印证。待本地验证。

## 6. 本讲小结

- LLM-DataDist 提供四个可用 KV Cache 传输接口，按「粒度（连续 Cache / 离散 Blocks）× 方向（Push / Pull）」排成矩阵，参数顺序统一为「源在前、目的地在后」，本地端用 `Cache`（cache_id 标识）、远端端用 `CacheIndex`。
- `CacheIndex` 三级寻址：`cluster_id` 选建链实体（CommEntity）、`cache_id` 查远端 cache table、`batch_index` 定位 batch 槽位；实现层翻译成内部 `CacheKey`，典型失败码为 `LLM_NOT_YET_LINK` 与 `LLM_KV_CACHE_NOT_EXIST`。
- Push 只支持 device 内存、`size` 只支持 -1、要求 src/dst 层区间等宽；实现上把配置装进 `TransferCacheConfig`，由 `PushData` 按层循环逐层下发 `TransferCache`。
- Pull 的 `size` 支持 -1 或正数（唯一支持部分字节传输的接口）；层区间在 Pull 侧不是循环而是一次性展开成 `src/dst_tensor_indices`；Blocks 与 Cache 两种模式最终在 V2 层汇合到同一个 `PullCache`。
- Blocks 的地址组织是「下标配对」：`src_blocks[i] → dst_blocks[i]`，两列表必须等长且非空，编号无需连续或有序；Blocks 模式下 `CacheIndex.batch_index` 恒为 0。
- 样例证实了完整的调用合同：两端各自 Initialize + RegisterKvCache → LinkLlmClusters → Push/Pull → Unlink → UnregisterKvCache → Finalize；跨进程的「注册完成/断链完成」通知需业务自行解决（样例用裸 TCP socket）。

## 7. 下一步学习建议

- **u6-l5（DataTransfer Job 体系）**：本讲到 `DataCacheEngine::PullCache`/`TransferCache` 为止，传输请求之后如何被拆成 D2D/D2H 等 DataTransferJob 并调度执行，是下一讲的主题。
- **u6-l6（FSM 状态机）**：本讲提到 Pull 前会检查实体 FSM 状态（`FSM_DESTROYED_STATE`），传输会话的完整状态迁移在 FSM 讲义中展开。
- **u7-l3（PD 分离端到端）**：如果想在真实双机环境跑通本讲的两个样例并做 blocks 粒度耗时对比，参考该讲的操作步骤。
- 继续阅读源码建议：从 [data_cache_engine.cc:132-179](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/cache_mgr/data_cache_engine.cc#L132-L179) 的 `DataTransferClient` 调用点往下追一层，看 `PullCacheByGet` 与 `PullCache` 两条客户端路径的差异。

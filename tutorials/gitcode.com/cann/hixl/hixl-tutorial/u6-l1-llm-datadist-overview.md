# u6-l1 LLM-DataDist 概述与角色模型

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 LLM-DataDist 在整个 HIXL 仓库中的定位：它是面向 **KV Cache 语义** 的上层传输接口，而 HIXL Engine 是面向 **内存语义** 的底层引擎。
2. 通读 `include/llm_datadist/llm_datadist.h`，把 `LlmDataDist` 类的公开接口按「初始化、建链、Cache、传输」四组归类，并说出每个接口的一句话职责。
3. 掌握 Prompt / Decoder / Mix 三种角色（`LlmRole`）的含义，以及 Server / Client 由谁决定。
4. 理解 LLM-DataDist 与 HIXL Engine 的分层关系：通过 `llm.TransferBackend` 选项把 HIXL 作为可配置的传输后端之一。

本讲是单元六（LLM-DataDist：KV Cache 传输）的第一课，只建立全景地图，不深入任何一个实现文件；实现细节由 u6-l2 至 u6-l8 逐层展开。

## 2. 前置知识

阅读本讲前，你需要具备以下背景（均在前面讲义中建立）：

- **KV Cache 是什么**：大模型推理中，Transformer 每一层的自注意力计算会产生 Key/Value 张量。增量解码时这些张量可以缓存复用，避免重复计算。这份缓存就是 KV Cache，它通常按 layer 组织（每层 2 个 tensor：一份 K、一份 V）、按 batch/block 组织地址，体积巨大（GB 级），因此「把 KV Cache 从一个集群搬到另一个集群」是推理系统里最重的数据搬运任务之一。
- **PD 分离（Prompt/Decoder 分离）**：把负责首 token 计算的 Prompt 集群与负责增量解码的 Decoder 集群物理分开，中间需要把 Prompt 侧算好的 KV Cache 传给 Decoder 侧——这正是 LLM-DataDist 的主战场（回顾 u1-l1）。
- **HIXL Engine 的能力模型**（回顾 u1-l3、u2 系列）：Initialize → RegisterMem → Connect → TransferSync/TransferAsync → Disconnect → Finalize。HIXL 的传输单元是裸的「本地地址 + 远端地址 + 长度」三元组，它不知道什么是 layer、什么是 batch。
- **ge::Status**：LLM-DataDist 的 `Status` 直接复用 CANN 图引擎（ge_common）的状态类型。与 HIXL 的 `hixl::Status`（uint32_t 别名、`hixl::SUCCESS == 0`）不同，LLM-DataDist 有自己的一套 `0x5010Bxxx` 错误码段（本讲 4.3 详述）。
- **Pimpl 惯用法**（回顾 u2-l1、u1-l4）：公开头文件只暴露前置声明 + `std::unique_ptr<Impl>`，实现细节全部藏在 `src/` 内部，保证公开头的 ABI 稳定。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `include/llm_datadist/llm_datadist.h` | LLM-DataDist 唯一公开类 `LlmDataDist`，含选项键、错误码常量、全部公开数据结构与接口声明。本讲主战场。 |
| `include/llm_datadist/llm_engine_types.h` | 更早期/引擎侧的选项键与角色字符串常量（`llm` 命名空间），体量很小但承载角色模型的历史信息。 |
| `include/llm_datadist/llm_error_codes.h` | 用 `GE_ERRORNO_DEFINE` 宏批量定义的 `ge::` 命名空间错误码枚举，比 `llm_datadist.h` 中的常量集更全。 |
| `docs/zh/api/cpp/LLM-DataDist-interface.md` | 官方 API 文档，逐接口给出参数表、返回值、约束说明，是本讲实践的校对基准。 |
| `src/llm_datadist/transfer_engine/`（目录） | 传输后端抽象与 HIXL 后端适配的实现所在，本讲只指出位置，u6-l7 精读。 |

头文件与实现的对应关系（回顾 u1-l4 的结论）：`llm_datadist.h` 声明的 `LlmDataDistImpl` 落在 `src/llm_datadist/api/llm_datadist_impl.cc`，本讲不展开。

## 4. 核心概念与源码讲解

### 4.1 LLM-DataDist 的定位：KV Cache 语义层

#### 4.1.1 概念说明

单元二至单元五学习的 HIXL Engine 提供的是**内存语义**：你给它「本地地址、远端地址、长度」，它把字节搬过去，至于搬的是什么、layer 怎么排布、batch 怎么索引，它一概不知。

而真实推理引擎（vLLM、SGLang 等）面对的是**业务语义**：「把 Prompt 集群上 cache_id=7 的第 0~15 层 KV Cache 推给 Decoder 集群」「按 block 列表只拉取命中的那些 block」。LLM-DataDist 就是补上这层语义的组件：

- 用 `CacheDesc` 描述一份 KV Cache 的形状（多少 tensor、什么数据类型、什么 shape）；
- 用 `CacheIndex` 跨集群定位一份远端 Cache；
- 用 `KvCacheExtParam` 表达 layer 级的部分传输；
- 用 `Push/PullKvBlocks` 表达 block 粒度的稀疏传输（对应 PagedAttention 类推理引擎的 block 表）。

于是整个仓库形成清晰的三层：

```text
推理引擎 (vLLM / SGLang ...)
    │  Cache/Layer/Block 语义
    ▼
LLM-DataDist (src/llm_datadist)          ← 本单元
    │  TransferBackend 适配
    ▼
HIXL Engine (src/hixl)                   ← 单元 2~5
    │
    ▼
HCCS / RDMA / FabricMem 硬件链路
```

#### 4.1.2 核心流程

一次典型的 PD 分离 KV Cache 搬运（两侧各一个 `LlmDataDist` 实例）：

1. 双方各自 `LlmDataDist(cluster_id, role)` 构造、`Initialize(options)`；Prompt 侧配置 `OPTION_LISTEN_IP_INFO` 成为 Server。
2. 双方 `RegisterKvCache` 把自己的 KV Cache 内存注册进来，拿到 `cache_id`（业务侧再把这个 id 告诉对端）。
3. Decoder 侧（Client）调用 `LinkLlmClusters` 与 Prompt 侧建链。
4. Prompt 侧 `PushKvCache` / Decoder 侧 `PullKvCache`（或 blocks 版本）完成传输。
5. `UnlinkLlmClusters` 断链，`UnregisterKvCache` 解注册，`Finalize` 收尾。

注意与 HIXL 原生 API 的两点风格差异：

- **角色不写在传输参数里**：`role` 只影响建链时谁是 Server/Client 的默认行为（文档明确「该参数只用于标识当前角色，对传输过程无影响」），真正的 Server/Client 由 `OPTION_LISTEN_IP_INFO` 是否配置决定。
- **Push/Pull 双向都有**：不像 HIXL 里 READ/WRITE 由「谁掌握两端地址谁发起」决定，LLM-DataDist 把方向封装成语义化的 Push（推出去）与 Pull（拉过来），两端都可主动。

#### 4.1.3 源码精读

分层关系的代码证据在选项键上。`llm_datadist.h` 定义了 LLM-DataDist 的初始化选项：

[include/llm_datadist/llm_datadist.h:L33-L42](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/llm_datadist/llm_datadist.h#L33-L42)

这段定义了 8 个选项键常量：`OPTION_LISTEN_IP_INFO`（配置即 Server）、`OPTION_DEVICE_ID`（必选，单进程单卡）、`OPTION_SYNC_CACHE_WAIT_TIME`、`OPTION_BUF_POOL_CFG`、`OPTION_ENABLE_SET_ROLE`、`OPTION_LOCAL_COMM_RES`（本地通信资源，JSON）、`OPTION_TRANSFER_BACKEND`（传输后端，当前支持 `"hixl"`）和 `OPTION_GLOBAL_RESOURCE_CONFIG`。

其中 `OPTION_TRANSFER_BACKEND` 与 `OPTION_GLOBAL_RESOURCE_CONFIG` 是分层关系的直接证据——官方文档对后者的描述是「LLM-DataDist 仅将配置透传至 HIXL，不新增校验或改变 HIXL 引擎选择逻辑」（见 [docs/zh/api/cpp/LLM-DataDist-interface.md:L105](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/api/cpp/LLM-DataDist-interface.md#L105)），说明 LLM-DataDist 在传输这件事上只是「翻译 + 透传」，真正的链路决策仍在 HIXL Engine。

透传的另一端在实现层：`src/llm_datadist/transfer_engine/` 目录下同时存在 `transfer_engine.h`（后端抽象）、`hixl_transfer_engine.cc`（HIXL 后端）与 `comm_transfer_engine.cc`（集合通信后端）。文件列表可自行 `ls` 验证；其内部逻辑由 u6-l7 精读，本讲只需要知道「后端是可插拔的，HIXL 是其中之一」。

#### 4.1.4 代码实践

**实践目标**：用「文档反查法」验证分层关系——从 LLM-DataDist 的官方文档出发，找到它引用 HIXL 文档的所有位置。

**操作步骤**：

1. 打开 [docs/zh/api/cpp/LLM-DataDist-interface.md](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/api/cpp/LLM-DataDist-interface.md)，全文搜索 `HIXL`（或 `hixl`）。
2. 记录每一处出现的章节：`OPTION_TRANSFER_BACKEND`、`OPTION_GLOBAL_RESOURCE_CONFIG`、`OPTION_LOCAL_COMM_RES` version 1.3 的说明（「使用 HixlCS 能力进行建链，没有链路上限限制」，L125）。
3. 对比同一目录下的 HIXL-interface.md 是否被引用为「通信资源配置字段说明」的唯一出处。

**需要观察的现象**：LLM-DataDist 文档中所有涉及链路细节（通信资源配置、建链方式、TLS）的内容都指向 HIXL 文档，而不是自己重新定义。

**预期结果**：你能在文档中找到至少 3 处对 HIXL 接口文档的显式引用，从而确认「语义在 LLM-DataDist、链路在 HIXL」的分层。

（本实践为纯文档阅读，无需硬件，可直接完成。）

#### 4.1.5 小练习与答案

**练习 1**：HIXL 的 `TransferOpDesc` 和 LLM-DataDist 的 `CacheIndex + Cache` 各自描述了什么？为什么后者不能直接替代前者？

**参考答案**：`TransferOpDesc` 是纯地址三元组（local_addr/remote_addr/len），描述「搬哪些字节」；`CacheIndex + Cache` 描述「哪份业务数据」（远端哪个集群哪个 cache_id 的哪个 batch）。后者不能替代前者，因为 KV Cache 语义必须落到具体地址才能传输，LLM-DataDist 内部仍要完成「Cache 描述 → 地址区间」的翻译（这正是 u6-l3/u6-l5 要讲的 CacheManager 与 DataTransferJob 的工作）。

**练习 2**：如果一台机器只链接了 `libllm_datadist.so` 而没有 HIXL 相关库，`OPTION_TRANSFER_BACKEND` 还能配 `"hixl"` 吗？

**参考答案**：不能正常工作。`"hixl"` 只是告诉 LLM-DataDist 选择 HIXL 后端，实际传输由 HIXL Engine 完成，运行期需要 HIXL 的引擎实现与底层通信库。这也是为什么文档把 HIXL 的建链约束（如 version 1.3 需要 HDK ≥ 25.5.0、toolkit ≥ 9.1.0）原样搬到 LLM-DataDist 文档里。

### 4.2 LlmDataDist 类接口全景

#### 4.2.1 概念说明

`LlmDataDist` 是 LLM-DataDist 的唯一公开类，与 HIXL 的 `hixl::Hixl` 一样采用 Pimpl 设计。它的接口面比 HIXL 更小、更贴业务：一个构造函数 + 15 个公开接口，可分四组：

| 分组 | 接口 |
| --- | --- |
| 生命周期 | `Initialize`、`Finalize`、`SetRole` |
| 链路 | `LinkLlmClusters`、`UnlinkLlmClusters` |
| Cache 管理 | `AllocateCache`、`DeallocateCache`、`RegisterKvCache`、`UnregisterKvCache` |
| 传输 | `PullKvCache`、`PullKvBlocks`、`PushKvCache`、`PushKvBlocks`、`CopyKvCache`、`CopyKvBlocks` |

与 HIXL 的接口对照（帮助记忆）：

| 语义 | HIXL (`hixl::Hixl`) | LLM-DataDist (`LlmDataDist`) |
| --- | --- | --- |
| 初始化 | `Initialize(options)` | `Initialize(options)`（选项键前缀 `llm.`） |
| 内存 | `RegisterMem/MemDesc` | `RegisterKvCache/CacheDesc`（带类型与 shape） |
| 建链 | `Connect(remote_engine)` | `LinkLlmClusters(ClusterInfo)`（按集群，可批量） |
| 传输 | `TransferSync(TransferOpDesc[])` | `Push/PullKvCache/Blocks`（带 layer/block 语义） |
| 通知 | `SendNotify/GetNotifies` | 无（语义层不暴露） |

#### 4.2.2 核心流程

接口间的顺序合同（违反会拿到对应错误码）：

```text
构造(cluster_id, role)
   └─> Initialize                        ← 一切接口的前提
         ├─> RegisterKvCache ──────────┐  ← 建链前必须完成（文档约束）
         ├─> AllocateCache（可选）      │
         ▼                             │
   LinkLlmClusters  <──────────────────┘
         ├─> PushKvCache / PushKvBlocks
         ├─> PullKvCache / PullKvBlocks
         ├─> CopyKvCache / CopyKvBlocks   ← 纯本地拷贝，不涉及远端
         ▼
   UnlinkLlmClusters
         ├─> UnregisterKvCache / DeallocateCache
         ▼
   Finalize
```

要点：

- **`SetRole` 可在运行中切换角色**（需要 `OPTION_ENABLE_SET_ROLE` 打开），但必须先断开全部链路，否则返回 `LLM_EXIST_LINK`；这是 u6-l6 状态机（switch roles）的入口。
- **`LinkLlmClusters` 是批量接口**：一次传多个 `ClusterInfo`，出参 `rets` 给出每个集群的结果，只有全部成功接口才返回 `LLM_SUCCESS`。
- **`DeallocateCache`/`UnregisterKvCache` 对不存在的 id 幂等返回成功**（文档明确），与 HIXL 的 DeregisterMem 风格一致。

#### 4.2.3 源码精读

类的整体骨架与 Pimpl：

[include/llm_datadist/llm_datadist.h:L159-L171](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/llm_datadist/llm_datadist.h#L159-L171)

这段声明了 `LlmDataDist` 类、构造函数（`cluster_id` 在所有参与建链范围内需唯一，`role` 仅标识不影响传输）与析构函数。`ASCEND_FUNC_VISIBILITY` 宏保证符号从动态库导出。

生命周期三接口：

[include/llm_datadist/llm_datadist.h:L173-L191](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/llm_datadist/llm_datadist.h#L173-L191)

`Initialize` 要求先于其他接口调用；`Finalize` 与之配对，文档强调「初始化成功后任何退出前都需要调用 Finalize，否则资源释放顺序不符合预期」。`SetRole` 的注释点明关键约束：切换为 Prompt（Server）时 options 必须包含 `OPTION_LISTEN_IP_INFO`。

链路两接口：

[include/llm_datadist/llm_datadist.h:L193-L212](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/llm_datadist/llm_datadist.h#L193-L212)

`LinkLlmClusters` 由 Client 端调用，`clusters` 里每个 `ClusterInfo` 描述一个远端集群（`remote_cluster_id` + 双方 ip 信息），默认超时 1000ms；`UnlinkLlmClusters` 多一个 `force_flag`——强制断链只拆本端，需两端都调；非强制由 Client 发起、正常时两端都拆。

Cache 管理四接口：

[include/llm_datadist/llm_datadist.h:L214-L227](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/llm_datadist/llm_datadist.h#L214-L227)

[include/llm_datadist/llm_datadist.h:L303-L319](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/llm_datadist/llm_datadist.h#L303-L319)

两条获得 Cache 的路径：`AllocateCache`（由 LLM-DataDist 的内存池分配，u6-l8 讲其 allocator）与 `RegisterKvCache`（注册用户已有内存，出参返回 `cache_id`，地址个数上限 240）。释放对应 `DeallocateCache` 与 `UnregisterKvCache`。

传输六接口：

[include/llm_datadist/llm_datadist.h:L229-L301](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/llm_datadist/llm_datadist.h#L229-L301)

六个传输接口两两成对：`PullKvCache`/`PullKvBlocks`（从远端拉）、`PushKvCache`/`PushKvBlocks`（推向远端）、`CopyKvCache`/`CopyKvBlocks`（本地拷贝，`CopyKvBlocks` 的 `dst_blocks_list` 是二维的，可一次拷到多个目标）。跨集群的四个接口都用 `CacheIndex` 定位远端、用 `KvCacheExtParam` 的 layer range 表达部分传输。

Pimpl 收尾：

[include/llm_datadist/llm_datadist.h:L321-L323](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/llm_datadist/llm_datadist.h#L321-L323)

前置声明 `class LlmDataDistImpl` + `std::unique_ptr<LlmDataDistImpl> impl_`，与 `hixl::Hixl` 的手法完全一致（对比 u2-l1），公开头因此零内部依赖。

#### 4.2.4 代码实践

**实践目标**：完成本讲规格指定的任务——把 `LlmDataDist` 的接口按「初始化、建链、Cache、传输」四组分类，并给每个接口写一句话说明。

**操作步骤**：

1. 打开 [include/llm_datadist/llm_datadist.h](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/llm_datadist/llm_datadist.h)，从 L159 的类声明开始逐个抄录接口签名。
2. 对每个接口，先读头文件里的 `@brief` 注释写出初稿说明。
3. 打开 [docs/zh/api/cpp/LLM-DataDist-interface.md](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/api/cpp/LLM-DataDist-interface.md)，用文档中的「函数功能」「约束说明」「返回值」三栏校对并补全你的说明（例如 `DeallocateCache` 要补上「Cache 不存在也返回 LLM_SUCCESS」这个幂等语义）。
4. 制作成如下格式的表（示例给出前两行，其余自己补全）：

| 分组 | 接口 | 一句话说明 |
| --- | --- | --- |
| 初始化 | Initialize | 解析 `llm.*` 选项完成初始化；配置了 `OPTION_LISTEN_IP_INFO` 的一侧成为 Server。 |
| 初始化 | SetRole | 运行中切换 Prompt/Decoder 角色，切 Server 需带监听 ip:port，且需先断开全部链路。 |
| … | … | … |

5. 标注每个接口文档中的「约束说明」里提到的**前置接口**（如「调用该接口前需要先 LinkLlmClusters」），检查它们能否连成 4.2.2 的顺序图；若有对不上的地方，以文档为准修正你的图。

**需要观察的现象**：头文件注释是「接口语义」的浓缩，文档「约束说明」是「调用顺序合同」的出处；两者拼起来才是一份完整的接口说明书。

**预期结果**：得到一张 15 行左右的四分组接口表，且每行的一句话说明里至少包含一个文档中的关键约束或幂等/批量语义。

（本实践为纯源码/文档阅读，无需硬件。）

#### 4.2.5 小练习与答案

**练习 1**：`LinkLlmClusters` 返回 `LLM_SUCCESS` 意味着什么？返回非 `LLM_SUCCESS` 又意味着什么？

**参考答案**：只有 `clusters` 中**所有**集群都建链成功才返回 `LLM_SUCCESS`；任一失败则接口整体返回失败，此时必须逐个查看出参 `rets` 才能知道哪些成功、哪些失败（成功的链路已经建立，不会被自动回滚）。

**练习 2**：`RegisterKvCache` 和 `AllocateCache` 都能得到可传输的 Cache，什么时候必须用前者？

**参考答案**：当 KV Cache 内存由推理引擎自己分配管理（例如框架的 PagedAttention 显存池）时必须用 `RegisterKvCache` 注册既有地址；只有愿意把内存分配交给 LLM-DataDist 托管时才用 `AllocateCache`。文档还提示 `RegisterKvCache` 的地址个数不超过 240，且传输类接口构造 `Cache` 时仅需指定 `RegisterKvCache` 返回的 `cache_id`。

### 4.3 llm_engine_types.h 与关键数据结构、错误码

#### 4.3.1 概念说明

本模块过一遍「传参必用」的数据结构与错误码。`llm_datadist.h` 中间的 L63-L158 一口气定义了所有公开数据结构；另一个小头文件 `llm_engine_types.h` 则保留了引擎侧的选项键与角色字符串，是理解角色模型演化的钥匙。

关键结构一览：

| 结构 | 角色 | 关键字段 |
| --- | --- | --- |
| `IpInfo` / `ClusterInfo` | 建链参数 | ip、port；远端集群 id、角色类型、双方 ip 列表 |
| `LlmRole` | 角色枚举 | kPrompt=1、kDecoder=2、kMix=3 |
| `CacheIndex` | 远端 Cache 的「门牌号」 | cluster_id + cache_id + batch_index |
| `CachePlacement` | Cache 放在哪 | kHost=0、kDevice=1 |
| `CacheDesc` | Cache 的形状 | placement、num_tensors、data_type、shape |
| `Cache` | 一份具体 Cache | cache_id、tensor_addrs、cache_desc |
| `KvCacheExtParam` | layer 级部分传输 | src/dst_layer_range、tensor_num_per_layer（默认 2） |
| `RegisterCfg` | 注册配置 | 全预留 |

#### 4.3.2 核心流程

**layer 寻址的数学**：`CacheDesc::num_tensors` 是这份 Cache 的 tensor 总数（例如 32 层 × 每层 2 个 = 64），`KvCacheExtParam::tensor_num_per_layer` 默认为 2（K 与 V）。文档给出的最大可用层索引公式为：

\[ \text{max\_layer\_index} = \frac{\text{num\_tensors}}{\text{tensor\_num\_per\_layer}} - 1 \]

layer range 的约束：src 与 dst 的跨度（second − first）必须相等（保证源层与目标层一一对应），取值范围 \([0, \text{max\_layer\_index}]\) 且 first ≤ second；默认 {-1, -1} 表示全部层。

**错误码的两套体系**（易混淆，务必分清）：

1. `llm_datadist.h` 中的 `constexpr Status` 常量（`LLM_SUCCESS`、`LLM_PARAM_INVALID` = 0x5010B005 等）——**用户代码判断返回值用这套**。
2. `llm_error_codes.h` 中 `GE_ERRORNO_DEFINE` 展开的 `ge::` 枚举（数量更多，含 `LLM_REPEAT_REQUEST`、`LLM_NO_FREE_BLOCK` 等内部错误）——主要供内部与 ge 生态使用。

两者数值上同段（0x5010Bxxx），但判断入口统一是 `Status == LLM_SUCCESS`（0）。

#### 4.3.3 源码精读

基础类型与选项键、错误码常量：

[include/llm_datadist/llm_datadist.h:L29-L61](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/llm_datadist/llm_datadist.h#L29-L61)

`Status`/`AscendString`/`DataType` 全部 `using` 自 `ge` 命名空间（L29-L31），错误码常量段从 `LLM_WAIT_PROC_TIMEOUT`(0x5010B001) 到 `LLM_OUT_OF_MEMORY`(0x5010B01C)，其中 `LLM_SUCCESS = 0`、`LLM_FAILED = 0xFFFFFFFF`。

建链与 Cache 相关结构体：

[include/llm_datadist/llm_datadist.h:L106-L158](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/llm_datadist/llm_datadist.h#L106-L158)

这段连续定义了 `IpInfo`、`ClusterInfo`、`LlmRole`、`CacheIndex`、`CachePlacement`、`CacheDesc`、`Cache`、`KvCacheExtParam`、`RegisterCfg`。注意每个结构体都有 `uint8_t reserved[128]`——ABI 预留字段，用户不应读写（回顾 u2-l2 的同款约定）。`kDefaultTensorNumPerLayer = 2U` 在 L27 定义，对应「每层 K+V 两个 tensor」。

`llm_engine_types.h` 全文（仅 27 行）：

[include/llm_datadist/llm_engine_types.h:L14-L24](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/llm_datadist/llm_engine_types.h#L14-L24)

`llm` 命名空间定义了角色字符串 `kPrompt`/`kDecoder`（供引擎侧把 `LlmRole` 序列化/反序列化为字符串）和 6 个 `LLM_OPTION_*` 选项键。对比 `llm_datadist.h` 的选项键可以发现对应与差异：`LLM_OPTION_SYNC_KV_CACHE_WAIT_TIME`↔`OPTION_SYNC_CACHE_WAIT_TIME`、`LLM_OPTION_ENABLE_SWITCH_ROLE`↔`OPTION_ENABLE_SET_ROLE`、`LLM_OPTION_BUF_POOL_CFG`↔`OPTION_BUF_POOL_CFG`；而 `LLM_OPTION_CLUSTER_INFO`、`LLM_OPTION_ROLE`、`LLM_OPTION_OUTPUT_MAX_SIZE` 只在此处出现，属于引擎侧（如 GE/推理引擎下发到 LLM-DataDist 的入口选项），不在 `LlmDataDist::Initialize` 的文档表中。

`llm_error_codes.h` 的宏定义批次：

[include/llm_datadist/llm_error_codes.h:L19-L47](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/llm_datadist/llm_error_codes.h#L19-L47)

28 个 `GE_ERRORNO_DEFINE` 按 8bit 子系统 + 11bit 模块的方式编码，比 `llm_datadist.h` 中的常量集多出 `LLM_REPEAT_REQUEST`、`LLM_PREFIX_ALREADY_EXIST`、`LLM_NO_FREE_BLOCK`、`LLM_CACHE_INCOMPATIBLE` 等——这些是内部流程（请求去重、block 分配、Cache 兼容性检查）会用到的细分错误，用户一般只会碰到它们经由文档列出的那几个入口错误。

#### 4.3.4 代码实践

**实践目标**：用「字段推演」验证 layer 寻址公式与 `reserved` 字段的存在。

**操作步骤**：

1. 构造一个假想的 CacheDesc：`num_tensors = 64`、`tensor_num_per_layer = 2`（默认），用公式算出最大可用层索引，应为 31。
2. 再令 `tensor_num_per_layer = 4`，重算（应为 15），并检查「需被 tensor 总数整除」的约束是否满足（64 ÷ 4 = 16 ✓）。
3. 写出三组合法的 `(src_layer_range, dst_layer_range)` 与两组非法组合（跨度不等、越界各一组）。
4. 用 `static_assert` 或打印 `sizeof(CacheIndex)`、`sizeof(CacheDesc)` 观察结构体尺寸（可选，需 CANN 头文件环境；若无环境则记录「待本地验证」）。

**需要观察的现象**：layer range 的合法性完全由 num_tensors 与 tensor_num_per_layer 两个数值决定，与 shape/data_type 无关。

**预期结果**：三组合法示例如 \({-1,-1}\)→全层、\({0,15}\)→\({0,15}\)、\({8,8}\)→\({0,0}\)（跨度都是 0 或相等）；非法示例如 \({0,31}\)→\({0,15}\)（跨度不等）、\({0,32}\)→\({0,32}\)（越界，32 > 31）。步骤 4 的 sizeof 数值待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`CacheIndex` 三个字段的含义分别是什么？为什么需要 `cluster_id`？

**参考答案**：`cluster_id` 定位哪个远端集群（一个 LlmDataDist 实例可和多条链路），`cache_id` 定位该集群上哪份 Cache，`batch_index` 定位 Cache 内哪个 batch 槽。缺 `cluster_id` 就无法在多链路场景下唯一寻址，这正对应 HIXL 层面 `ClientManager` 按 remote engine 管理多条链路的结构。

**练习 2**：`llm_datadist.h` 里的 `LLM_PARAM_INVALID` 和 `llm_error_codes.h` 里的 `ge::LLM_PARAM_INVALID` 是什么关系？

**参考答案**：数值同源（都由 0x5010B005 编码而来，前者是手写常量、后者是 `GE_ERRORNO_DEFINE` 宏展开的枚举），语义相同。用户侧判断统一用 `llm_datadist` 命名空间的常量并比较 `== LLM_SUCCESS` 即可，不必关心 ge 枚举。

### 4.4 Prompt/Decoder/Mix 角色模型

#### 4.4.1 概念说明

`LlmRole` 是一个三值枚举：

- **kPrompt（=1）**：本实例属于 Prompt 集群，负责预填充、产出 KV Cache，通常作为数据的**发送方**（Push）或被拉取方（被 Pull）。
- **kDecoder（=2）**：本实例属于 Decoder 集群，负责增量解码，通常作为 KV Cache 的**接收方**（Pull 或被 Push）。
- **kMix（=3）**：混合角色——同一集群既能当 Prompt 也能当 Decoder，常见于资源动态调度、增量/全量集群等场景。

必须澄清一个极易误解的点（文档在构造函数参数表里明确写了）：**`role` 只是标识，不决定传输行为，也不直接决定 Server/Client**。真正决定谁是 Server 的是 `Initialize` 时是否配置 `OPTION_LISTEN_IP_INFO`：配了的一侧监听（Server），没配的一侧主动连（Client）。角色与连接方向是可以自由组合的——Decoder 侧完全可以在自己 `Initialize` 时配监听地址，让 Prompt 侧来连它。

那 `role` 有什么用？它是给**业务协作流程**用的标签：推理引擎按角色编排「谁先算、谁 push/pull、何时切换」，`SetRole` 支持运行期在 Prompt/Decoder 间切换（即 PD 分离动态调度、switch roles 场景，样例 `prompt_switch_roles.cpp`/`decoder_switch_roles.cpp`，u6-l6 精读）。

#### 4.4.2 核心流程

角色 × 连接方向 × 传输方向的组合矩阵：

| 组合 | 谁是 Server（配 ListenIpInfo） | 传输发起 | 说明 |
| --- | --- | --- | --- |
| 常规 PD | Prompt | Decoder Pull（或 Prompt Push） | 文档示例的默认形态 |
| 反向 | Decoder | Prompt Push | 传输方向与连接方向无关 |
| HIXL 后端 | 任意一侧均可 | 任意 | 文档注明 hixl 后端下每个端「既可作为 client 也可以作为 server」 |

角色切换的状态骨架：

```text
LlmRole::kDecoder ──SetRole(kPrompt, {ListenIpInfo})──> kPrompt（变为 Server）
LlmRole::kPrompt  ──SetRole(kDecoder, {})─────────────> kDecoder（变为 Client）
        ▲ 前提：UnlinkLlmClusters 已清空全部链路，否则 LLM_EXIST_LINK
```

#### 4.4.3 源码精读

角色枚举定义：

[include/llm_datadist/llm_datadist.h:L120](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/llm_datadist/llm_datadist.h#L120)

`enum class LlmRole : int32_t { kPrompt = 1, kDecoder = 2, kMix = 3, kEnd }`——注意有 `kEnd` 哨兵值（迭代/校验用），且是强类型枚举，不会与 int 隐式转换。

角色进入接口的三个位置：

[include/llm_datadist/llm_datadist.h:L160-L166](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/llm_datadist/llm_datadist.h#L160-L166)

构造函数接收 `cluster_id` 与 `role`，注释明确「该参数只用于标识当前角色」（完整表述见文档构造函数一节的参数表）。

[include/llm_datadist/llm_datadist.h:L185-L191](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/llm_datadist/llm_datadist.h#L185-L191)

`SetRole(role, options)`——运行期角色切换入口，注释指出设置成 Prompt 时 options 需含 `OPTION_LISTEN_IP_INFO`。

[include/llm_datadist/llm_engine_types.h:L15-L16](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/llm_datadist/llm_engine_types.h#L15-L16)

`kPrompt`/`kDecoder` 字符串常量——角色在控制面消息里的序列化形态（注意没有 `kMix` 字符串，Mix 更多是业务侧组合 Prompt/Decoder 行为的标签）。

`ClusterInfo` 中的 `remote_role_type`：

[include/llm_datadist/llm_datadist.h:L112-L118](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/llm_datadist/llm_datadist.h#L112-L118)

建链参数里也带一个 `int32_t remote_role_type`，即建链时显式声明对端角色——这是角色标签参与控制面协商的证据。

#### 4.4.4 代码实践

**实践目标**：在样例里找到「角色 ≠ Server/Client」的直接证据。

**操作步骤**：

1. 打开 [examples/README.md](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/README.md) 与 `examples/cpp/` 下的 `prompt_push_cache_and_blocks.cpp`、`decoder_pull_cache_and_blocks.cpp`（存在性可用 `ls examples/cpp` 确认）。
2. 分别找出两个样例中：`LlmDataDist` 构造时传的 role、`Initialize` 时是否配置 `OPTION_LISTEN_IP_INFO`、谁调用 `LinkLlmClusters`、谁调用 Push/Pull。
3. 把结果填进 4.4.2 的组合矩阵，看这对样例落在哪一格。
4. 再看 `prompt_switch_roles.cpp` / `decoder_switch_roles.cpp` 中 `SetRole` 前后 `UnlinkLlmClusters` 的调用顺序，验证「先断链再切角色」的合同。

**需要观察的现象**：两个 push/pull 样例里，「配置 ListenIpInfo 的一侧」与「传 kPrompt 的一侧」是否重合；如果不重合，就说明角色确实只是标签。

**预期结果**：常规样例中 Prompt 侧同时是 Server（两者重合，这是惯例而非强制）；switch roles 样例中能看到 SetRole 与断链/重新建链的交替。具体每个样例落在哪一格，待本地阅读样例源码后确认（样例精读在 u6-l4、u6-l6 进行，此处只要求找到证据）。

#### 4.4.5 小练习与答案

**练习 1**：一个 Decoder 侧实例想在本地 `Initialize` 时配置 `OPTION_LISTEN_IP_INFO`，让 Prompt 主动连过来，合法吗？

**参考答案**：合法。`OPTION_LISTEN_IP_INFO` 只决定监听/主动连接方向，与 `LlmRole` 无关；文档对 `OPTION_TRANSFER_BACKEND=hixl` 还特别说明「每个传输端既可作为 client 也可以作为 server」。传输发起方向由 Push/Pull 接口选择决定。

**练习 2**：`SetRole(kPrompt, options)` 切换角色前忘了解链，会发生什么？`kMix` 角色能绕过这个限制吗？

**参考答案**：返回 `LLM_EXIST_LINK`（存在残留链路资源），必须先 `UnlinkLlmClusters`。`kMix` 也绕不过——它是角色标签，不改变接口的前置约束；且 `llm_engine_types.h` 的角色字符串里根本没有 Mix 的序列化形态。

## 5. 综合实践

**任务：制作你自己的《LLM-DataDist 接口速查卡》并用一个 PD 场景故事串起来。**

1. **接口面**：完成 4.2.4 的四分组接口表（初始化/建链/Cache/传输，含一句话说明与关键约束）。
2. **数据面**：为 4.3 表中的 8 个结构体各写一行「字段 → 用在哪类接口」的映射（例如 `CacheIndex` → 所有 Pull/Push 接口的入参）。
3. **角色面**：画出 4.4.2 的组合矩阵，并标注文档中支持 hixl 后端时的特殊行为。
4. **串故事**：用 200 字左右写出一个具体场景的调用序列：Prompt 集群（cluster_id=1，Server，192.168.1.1:26000）与 Decoder 集群（cluster_id=2，Client），Prompt 注册 64 tensor 的 Cache 并把 cache_id=100 告知 Decoder，Decoder Pull 第 0~15 层。在故事里标出每一步调用的接口、使用的结构体、可能返回的错误码（至少用到 `LLM_NOT_YET_LINK` 与 `LLM_TIMEOUT` 一次）。
5. **校验**：全篇对照 `docs/zh/api/cpp/LLM-DataDist-interface.md`，确保每个约束都能在文档中找到出处；把出处行号写在速查卡备注列。

完成后的速查卡就是你后续阅读 `src/llm_datadist/` 实现时的「护照」——每个实现文件都能在卡上找到它服务的那个接口。

## 6. 本讲小结

- LLM-DataDist 是 KV Cache **语义层**：用 Cache/Layer/Block 概念服务推理引擎（vLLM/SGLang），底层通过 `llm.TransferBackend` 等选项把真实传输交给 HIXL Engine 或集合通信后端，配置对 HIXL 只做透传。
- `LlmDataDist` 类采用与 `hixl::Hixl` 相同的 Pimpl 设计，15 个公开接口可分四组：生命周期（Initialize/Finalize/SetRole）、链路（Link/UnlinkLlmClusters，批量 + 每集群结果）、Cache（Allocate/Deallocate/Register/UnregisterKvCache，两种获得 Cache 的路径）、传输（Push/Pull 的 Cache/Blocks 版本 + 本地 Copy）。
- 关键数据结构：`CacheIndex`（cluster_id+cache_id+batch_index 三级寻址）、`CacheDesc`（placement/num_tensors/data_type/shape）、`KvCacheExtParam`（layer range，最大层索引 = num_tensors/tensor_num_per_layer − 1）；错误码是 0x5010Bxxx 段的 `ge::Status`，唯一正确判断是 `== LLM_SUCCESS`。
- `LlmRole`（Prompt/Decoder/Mix）只是角色标签，不影响传输、也不直接决定 Server/Client；Server/Client 由 `OPTION_LISTEN_IP_INFO` 是否配置决定，运行期可经 `SetRole` 切换但必须先断链。
- `llm_engine_types.h` 保留引擎侧的 `llm.*` 选项键与 `kPrompt`/`kDecoder` 字符串，`llm_error_codes.h` 用 `GE_ERRORNO_DEFINE` 定义了比用户可见常量更多的内部错误码——两套体系数值同段、判断入口统一。

## 7. 下一步学习建议

下一讲 **u6-l2「初始化与集群建链」** 将进入实现层：`src/llm_datadist/api/llm_datadist_impl.cc` 中 `Initialize` 如何解析这些 `llm.*` 选项、`link_mgr` 模块（`comm_link_manager.cc`、`rank_table_generator.cc`、`link_msg_handler.cc`）如何完成集群建链与 rank table 生成。建议预习时先浏览 `src/llm_datadist/` 的目录结构（api/cache_mgr/data_transfer/fsm/link_mgr/memory/transfer_engine），把本讲的接口表映射到这些目录上。

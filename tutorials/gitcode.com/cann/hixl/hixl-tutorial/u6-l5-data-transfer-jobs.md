# u6-l5 DataTransfer Job 体系

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `DataTransferJob` 抽象基类的接口约定，以及它在**服务端（被拉取方）**和**客户端（拉取方）**两侧分别被谁创建、谁驱动。
2. 区分 D2D / D2H / H2D 三类 job 的适用场景与实现差异：D2D 是单边批量传输，D2H/H2D 则要经过一块 device 中转 buffer 分阶段搬运。
3. 理解 `LayerWiseTransferJob` 的"按层传输"思路：每次只传一层（或一个 block 区间），供推理引擎边算边传。
4. 掌握 `DataTransferClient` 如何编排一次 PullKvCache：构造请求 → 发往远端 → 同步 stream → 读应答。
5. 能独立画出"一次 PullKvCache 从公开 API 到 job 执行"的完整调用序列图。

## 2. 前置知识

本讲假设你已掌握 u6-l3（Cache 管理）与 u6-l4（Push/Pull 接口）的内容，另外补充三个概念：

- **CacheEntry 与 CachePlacement**：`CacheEntry` 是 LLM-DataDist 内部的 cache 台账条目（地址列表 `cache_addrs`、行距 `stride`、块数 `num_blocks`、放置位置 `placement` 等）。`placement` 取 `DEVICE`（NPU 显存）或 `HOST`（主机内存），它决定了选哪条传输路径。
- **单边传输 op**：`HcclOneSideOpDesc` 是一条"本地地址 + 远端地址 + 字节数"的三元组（u6-l4 讲过的 `TransferOpDesc` 的内部形态）。一个 job 的本质工作就是把用户请求**展开成一串 `HcclOneSideOpDesc`**，再交给 `CommEntity` 的 `BatchTransfer` / `BatchPutAsync` 下发到传输后端（HCCL 或 HIXL）。
- **请求缓冲区即通信介质**：u6-l2 建链时两端交换了 `req/resp` 缓冲区地址。本讲中客户端把 `TransferCacheReq` 结构体**直接写进远端请求缓冲区**（这本身就是一次单边 PUT），服务端状态机轮询到请求后进入 SendState 处理——控制消息与数据一样走单边路径，不再有 socket 收发。

一个直觉性的总纲：**"job = 请求解析 + 任务展开 + 分批执行 + 完成上报"**。四类 job 的差别只在"任务展开"和"分批执行"两步。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/llm_datadist/data_transfer/data_transfer_job.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/data_transfer/data_transfer_job.h) | `DataTransferJob` 抽象基类，全部 job 的接口约定 |
| [src/llm_datadist/data_transfer/data_transfer_client.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/data_transfer/data_transfer_client.cc) | 客户端编排器：构造请求、发送、同步、读应答 |
| [src/llm_datadist/data_transfer/d2d_data_transfer_job.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/data_transfer/d2d_data_transfer_job.cc) | D2D job：两端都是显存，单边批量传输 |
| [src/llm_datadist/data_transfer/d2h_data_transfer_job.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/data_transfer/d2h_data_transfer_job.cc) | D2H job（服务端）+ `D2HDataTransferClient`（客户端）+ 任务切分器 `DataTransferTaskGenerator` |
| [src/llm_datadist/data_transfer/h2d_data_transfer_job.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/data_transfer/h2d_data_transfer_job.cc) | H2D job：源在主机内存，经中转 buffer 用状态机搬运 |
| [src/llm_datadist/data_transfer/layer_wise_transfer_job.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/data_transfer/layer_wise_transfer_job.cc) | 按层/block 粒度传输的 job（不继承 `DataTransferJob`） |
| [src/llm_datadist/data_transfer/data_transfer_utils.h](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/data_transfer/data_transfer_utils.h) | 公共工具：event 查询、批量 SendCache |
| [src/llm_datadist/fsm/send_state.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/fsm/send_state.cc) | 服务端状态机：解析请求、**选择并创建 job**、反复驱动 `Process` |
| [src/llm_datadist/cache_mgr/data_cache_engine.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/cache_mgr/data_cache_engine.cc) | 客户侧入口：`PullCache` 与 `TransferCache` 的分派 |

## 4. 核心概念与源码讲解

先给一张全景图，再逐模块精读。

```text
客户端（拉取方，本进程）                     服务端（被拉取方，对端进程）
────────────────────────────                ────────────────────────────
LlmDataDist::PullKvCache
  └─ DataCacheEngine::PullCache
       ├─ placement=DEVICE ──► DataTransferClient::PullCache
       |     ├─ ConstructTransferInfo  (填 TransferCacheReq)
       |     ├─ SendCacheInfoToRemote  ──单边PUT──►  远端 req 缓冲区
       |     └─ SynchronizeStreamTask / GetResponseInfo
       ├─ placement=HOST  ──► D2HDataTransferClient::PullCache（中转 buffer 流水线）
       └─ access_remote_cache ──► PullCacheByGet（本地直接 D2DDataTransferJob::PullCache）
                                                    │
                                              FSM 进入 SendState
                                                └─ ResolveTransferType
                                                     ├─ D2D ──► D2DDataTransferJob
                                                     ├─ D2H ──► D2HDataTransferJob
                                                     └─ H2D ──► H2DDataTransferJob
                                                job->Initialize / 反复 job->Process
                                                完成后 SendResponse，回到 IdleState
```

注意一个容易混淆的点：**`D2DDataTransferJob` 在两侧都会被实例化**。服务端由 SendState 创建并驱动 `Process`（被动发送方视角）；客户端在 `PullCacheByGet` 路径里也会栈上创建一个并调用它的 `PullCache`（主动拉取方视角）。而 `DataTransferClient` / `D2HDataTransferClient` 只存在于客户端。

### 4.1 DataTransferJob：任务抽象与 SendState 工厂

#### 4.1.1 概念说明

`DataTransferJob` 是**服务端**处理一次远端拉取请求的任务抽象。它把"处理一个 `TransferCacheReq`"这件事抽象成三个步骤：初始化（解析请求、生成任务列表）、处理（分批执行，可能需要多次调用才完成）、以及可选的主动拉取。为什么需要"多次调用才完成"？因为 job 自身不拥有线程——它由 FSM 的 SendState 在每次被调度时推进一小步（非阻塞），这样单线程的状态机循环可以同时看护多个 entity 的多个 job。

#### 4.1.2 核心流程

```text
SendState::Preprocess(entity)
  ├─ Prepare(entity)
  |    ├─ QueryCacheEntryAndOffset   # 按 cache_key / cache_id / blocks 三种方式查台账
  |    ├─ CheckParam                 # num_tensors、block 越界、pull_size 等校验
  |    ├─ ResolveTransferType        # src_placement × dst_placement → D2D/D2H/H2D/-1
  |    ├─ ValidateTransferRequest    # dst_addr_count/buffer_info_count 上限校验
  |    ├─ SetDataTransferJob(...)    # 按类型创建 job（工厂分支）
  |    └─ transfer_job->Initialize(cache_entry, entity, offset)
  └─ Process(entity)                 # FSM 周期性调用，直到 is_done=true
       └─ job->Process(is_done)
            └─ is_done → SendResponse + Postprocess（清 job，回 IdleState）
```

#### 4.1.3 源码精读

抽象基类只有三个接口，`PullCache` 默认返回"功能未开启"：

- [data_transfer_job.h:L20-L30](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/data_transfer/data_transfer_job.h#L20-L30) —— 定义 `Initialize`（读请求、解析参数）、`Process`（推进执行）、`PullCache`（客户端主动拉取路径，仅 D2D 实现）三个纯虚/虚接口。

传输类型的判定完全由两端内存放置位置决定，这是整个体系的**唯一选路依据**：

- [send_state.cc:L207-L220](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/fsm/send_state.cc#L207-L220) —— `ResolveTransferType`：源 DEVICE + 目的 DEVICE → D2D；源 DEVICE + 目的 HOST → D2H；源 HOST + 目的 DEVICE → H2D；其余（如 HOST→HOST）返回 -1，随后被拒绝为 `LLM_FEATURE_NOT_ENABLED`。

job 的创建点就是这三行 if-else，可以视为一个内联工厂：

- [send_state.cc:L102-L111](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/fsm/send_state.cc#L102-L111) —— 按 `transfer_type` 用 `MakeUnique` 创建 `D2HDataTransferJob` / `H2DDataTransferJob` / `D2DDataTransferJob`，存进 entity（`SetDataTransferJob`），然后立刻调用 `Initialize`。

FSM 对 job 的驱动是"每周期推一步"：

- [send_state.cc:L115-L132](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/fsm/send_state.cc#L115-L132) —— `SendState::Process`：先查超时，再调 `entity.GetDataTransferJob()->Process(is_done)`；`is_done` 为真时可能顺带移除一次性 cache_key（pull 后即焚），然后 `Postprocess` 清空 job 并回到 IdleState。

服务端如何根据请求找到 cache 与偏移（三种寻址方式的汇合点）：

- [send_state.cc:L139-L173](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/fsm/send_state.cc#L139-L173) —— `QueryCacheEntryAndOffset`：`is_pull_block=1` 走 blocks 查表（offset=0）；`cache_id >= 0` 先尝试映射到 CacheKey（req_id, model_id），找不到再按 cache_id 查；命中 CacheKey 时 offset 由 `batch_index × stride` 算出，且非 prefix、自有的 cache 会被标记"拉完即删"。

#### 4.1.4 代码实践

**实践目标**：确认"选路只看 placement"，并亲手列出三类 job 的创建条件。

1. 打开 `send_state.cc`，阅读 `ResolveTransferType` 与 `ResolveTransferType` 的调用点。
2. 构造下表（已给出参考答案，请对照源码逐行验证）：

| src_placement（服务端 cache） | dst_placement（请求中） | transfer_type | 创建的 job |
| --- | --- | --- | --- |
| DEVICE | DEVICE | 0 (D2D) | D2DDataTransferJob |
| DEVICE | HOST | 1 (D2H) | D2HDataTransferJob |
| HOST | DEVICE | 2 (H2D) | H2DDataTransferJob |
| HOST | HOST | -1 | 拒绝，LLM_FEATURE_NOT_ENABLED |

3. **需要观察的现象**：`request.dst_placement` 是客户端填的（见 4.2 节 `ConstructTransferInfo` 中 `request.dst_placement = 1`），`cache_entry.placement` 是服务端注册 cache 时定的——也就是说**客户端通过 placement 字段"点菜"，服务端据此"上菜"**。

4. **预期结果**：能不看书回答"为什么 HOST→HOST 不支持"。（提示：`DataTransferClient` 只有 req_stream 一条 ACL stream，D2H/H2D job 都依赖 device 内存池中转，双方都在 host 时没有可用的单边写路径。）

**待本地验证**：表中行为可在 tests/cpp 的 llm_datadist 单测中通过桩环境验证；如需真机验证，参考 `examples/cpp/decoder_pull_cache_and_blocks.cpp`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `DataTransferJob::Process` 的签名是 `Process(bool &is_done)` 而不是阻塞到完成？
**答案**：job 不拥有线程，由 FSM 的 SendState 周期性驱动。非阻塞推进让单个状态机循环能同时看护多个 entity；D2H job 等 sync flag、H2D job 等 copy future 时都直接返回，把 CPU 让给下一次调度。

**练习 2**：`ValidateTransferRequest` 为什么故意不校验 `req_size` 与期望值相等？
**答案**：见 [send_state.cc:L64-L69](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/fsm/send_state.cc#L64-L69) 的注释：旧版本 D2D/H2D 客户端不填 `req_size`，严格相等会拒绝合法的跨版本请求。内存安全不依赖它——所有 `transfer_infos[]` 访问都由 `dst_addr_count`/`transfer_info_count` 的上界校验兜住，服务端 D2H 的 recv-flag 基址也从这些已校验计数推导而非信任线上 `req_size`，伪造的 `req_size` 无法把远端写引出缓冲区。

### 4.2 DataTransferClient：客户端的编排器

#### 4.2.1 概念说明

`DataTransferClient` 是**拉取方**（本进程）的传输编排器，服务"目的 cache 在 device 显存"的普通路径。它不做任务展开，而是完成一次"远程过程调用"式的协作：把目的地址写进请求结构体 → 单边 PUT 到远端请求缓冲区 → 等待远端 SendState 处理完 → 从本端 resp 缓冲区读结果。真正搬数据的可能是远端的 D2D job（对端单边 PUT 过来），也可能在 `PullCacheByGet` 模式下由本端 D2D job 直接单边 GET。

#### 4.2.2 核心流程

```text
DataTransferClient::PullCache
  ├─ ConstructTransferInfo   # 计算连续块配对、填 TransferCacheReq 到本端 req 缓冲区
  └─ PullCacheFromRemote
       ├─ SendCacheInfoToRemote   # 校验 req_size 后 SendRequest（单边 PUT 请求）
       ├─ SynchronizeStreamTask   # aclrtSynchronizeStreamWithTimeout + 自旋等 local_resp_flag
       └─ GetResponseInfo         # 读远端写回的 ResponseInfo.ret_code
```

其中 `ConstructTransferInfo` 先解决 u6-l4 讲过的三种 Pull 形态：连续→连续（buffer_info_count=1）、连续→离散（按 decoder_blocks 逐块生成）、离散→离散（`FindContiguousBlockIndexPair` 把两侧 blocks 合并成连续段配对）。

#### 4.2.3 源码精读

- [data_transfer_client.h:L24-L44](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/data_transfer/data_transfer_client.h#L24-L44) —— 类定义：只持 `comm_entity_`、`req_stream_`、超时值三个成员；公开 `PullCache`（协作式）与 `PullCacheByGet`（主动 GET 式）两个入口。

- [data_transfer_client.cc:L215-L223](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/data_transfer/data_transfer_client.cc#L215-L223) —— `PullCache` 主流程：记录起始时间 → `ConstructTransferInfo` → `PullCacheFromRemote`，两步任一失败即整体失败。

- [data_transfer_client.cc:L22-L48](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/data_transfer/data_transfer_client.cc#L22-L48) —— `SetBufferInfoCount`：三种 Pull 形态的分派。注意第一条检查——"离散→连续不支持"直接报 `LLM_PARAM_INVALID`，这与 D2H 客户端 `Prepare` 里的检查（见 4.4 节）一致。

- [data_transfer_client.cc:L108-L151](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/data_transfer/data_transfer_client.cc#L108-L151) —— `ConstructTransferInfo`：按 `CalcMinRequestSize` 算请求尺寸，从 `comm_entity_->GetTransferCacheReq` 拿到本端 req 缓冲区里的指针，逐字段填 `cache_id / batch_index / req_id / prefix_id / model_id / dst_addr_count / buffer_info_count / is_pull_block / dst_placement(=1，即 DEVICE) / timeout_in_ms / pull_size / max_block_index` 等，再由 `SetDstAddr`（L50-L69，目的地址 = `cache_addrs[i+起始层] + batch_index*stride`）与 `SetBufferInfo`（L71-L104，连续段的 `buffer_len` 与 `block_start_index`）补齐地址信息。**这份字段清单就是客户端与服务端 SendState 之间的全部合同**。

- [data_transfer_client.cc:L183-L205](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/data_transfer/data_transfer_client.cc#L183-L205) —— `SynchronizeStreamTask`：先带超时同步 req stream（确保请求 PUT 已落地），然后**自旋读本端 device 上的 resp flag**（volatile 指针轮询，远端写 1 表示应答就绪），超过 `timeout_in_ms_` 报 `LLM_TIMEOUT`，成功后 `ClearResponseFlags` 复位。

- [data_transfer_client.cc:L225-L244](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/data_transfer/data_transfer_client.cc#L225-L244) —— `PullCacheByGet`（`access_remote_cache` 开关打开时的路径）：同样构造请求，但从本端 **cache access table**（建链时同步过来的远端 cache 表）直接 `FindCacheEntry` 拿到远端地址，校验两侧 `remote_accessible` 后，栈上创建 `D2DDataTransferJob` 并调用其 `PullCache`——由本端主动单边 GET，远端 FSM 完全不参与。

客户端的分派入口在 `DataCacheEngine::PullCache`：

- [data_cache_engine.cc:L132-L179](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/cache_mgr/data_cache_engine.cc#L132-L179) —— 按序判空 cache、判链路存活（`LLM_NOT_YET_LINK`）、加 entity 级 pull 互斥锁、恢复 ACL context，然后三分支：`access_remote_cache_` → `DataTransferClient::PullCacheByGet`；`placement == HOST` → `D2HDataTransferClient`；否则 → `DataTransferClient::PullCache`。每条路径都挂了 `LLM_DISMISSABLE_GUARD(abort_stream, ...)` 失败时中止 stream 防挂死。

#### 4.2.4 代码实践

**实践目标**：搞清 `TransferCacheReq` 每个字段的填写者与消费者。

1. 通读 [data_transfer_client.cc:L108-L151](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/data_transfer/data_transfer_client.cc#L108-L151)，列出每个字段。
2. 对每个字段，在 `send_state.cc` 中找到读取它的函数，填成对照表。参考答案（节选）：

| 字段 | 客户端填写处 | 服务端消费处 |
| --- | --- | --- |
| `cache_id/batch_index/req_id/prefix_id/model_id` | ConstructTransferInfo L125-L129 | `QueryCacheEntryAndOffset` L147-L173 |
| `dst_addr_count` / `transfer_infos[i].dst_addr` | L116-L130、SetDstAddr L50-L69 | `GetSendTask`（d2d L32、L54-L58） |
| `buffer_info_count` / `is_pull_block` | L27-L44 | `QueryBlocksCache` L175-L185、`CheckParam` L237 |
| `dst_placement` | L133（固定 1=DEVICE） | `ResolveTransferType` L207-L220 |
| `pull_size` | L136（-1 时取 stride） | `CheckParam` L246-L259 |
| `src_tensor_start_index / src_tensor_indices_size` | L141-L145 | H2D `Initialize` L83-L92、D2D `GetSendTask` L53 |

3. **预期结果**：一张完整的字段流转表；你会发现它是 4.1 节"合同"的具体化，也是排障时对照日志的基础。

#### 4.2.5 小练习与答案

**练习 1**：`SynchronizeStreamTask` 里为什么先 `aclrtSynchronizeStreamWithTimeout` 还要再自旋等 flag？只同步 stream 不够吗？
**答案**：同步 req stream 只保证"请求 PUT 已从本端发出并完成"，不保证"远端已经处理完并写回应答"。应答完成的标志是远端单边写本端 device 上的 `local_resp_flag`（值为 1），这个事件与本端 stream 无依赖关系，只能靠轮询 flag 加超时兜底。

**练习 2**：`PullCacheByGet` 与 `PullCache` 对远端的依赖有何本质区别？
**答案**：`PullCache` 需要远端 FSM 介入（SendState 创建 job、查台账、发数据、写应答）；`PullCacheByGet` 依赖建链时同步好的 cache access table，本端直接持远端地址发起单边 GET，远端零参与——这正是 u1-l1 讲的"单边零拷贝"理念在 LLM-DataDist 层的体现，代价是双方 cache 都必须 `remote_accessible`。

### 4.3 D2DDataTransferJob：两端显存的直通路径

#### 4.3.1 概念说明

D2D 是最简单也最快的路径：源和目的都在 device 显存，注册过的地址对双方都可直接单边读写。job 的工作退化成纯"任务展开"：把请求中的地址与块信息翻译成一串 `HcclOneSideOpDesc`，分批（`kMaxBatchPutNum = 64`）交给 `CommEntity::BatchTransfer`。它同时在两个视角下工作：

- **服务端视角**（SendState 创建）：`Initialize` + `Process`——被动地"把本地 cache 发出去"（单边 PUT 到请求里的 dst_addr）。
- **客户端视角**（`PullCacheByGet` 创建）：`PullCache`——主动地从远端地址 GET 回来。

#### 4.3.2 核心流程

```text
服务端视角：
Initialize → GenerateSendTask → GenerateCacheTask → GetSendTask   # 展开 op 列表
Process（可多次）:
  等待上一批 event 完成（轮询 QueryEventStatus + 超时检查）
  ├─ send_tasks_ 空 → SendResponse(SUCCESS), is_done=true, 记录耗时统计
  └─ 否则取一批（≤64 条）SendCache 下发，下次 Process 再等 event

客户端视角：
PullCache → comm_entity_->BatchTransfer(send_tasks_, is_put=false, reversed=true)
            # 直接批量 GET，同步等待
```

#### 4.3.3 源码精读

- [d2d_data_transfer_job.cc:L27-L74](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/data_transfer/d2d_data_transfer_job.cc#L27-L74) —— `GetSendTask`：任务展开的核心。双重循环（每个 tensor × 每个连续 buffer 段）生成 op：`localAddr = cache_addrs[i+起始层] + offset + src_block_index × block_size`（offset 来自 batch_index），`remoteAddr = dst_addr + dst_block_index × block_size`；末段有余数时 `count` 取 remainder；`dataType` 固定 INT8（按字节计）。开头一系列 `LLM_CHK_BOOL_RET_STATUS` 逐项校验源地址数、块索引、buffer_len 对等，任何不一致都是 `LLM_PARAM_INVALID`。

- [d2d_data_transfer_job.cc:L77-L83](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/data_transfer/d2d_data_transfer_job.cc#L77-L83) —— `Initialize`：记住 entity、展开任务、记超时起点。

- [d2d_data_transfer_job.cc:L85-L117](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/data_transfer/d2d_data_transfer_job.cc#L85-L117) —— `Process` 的"等-发-完"三态循环：先轮询上一批的 event（超时则 `SendResponse(LLM_TIMEOUT)` 并失败返回）；任务发空后 `SendResponse(ge::SUCCESS)`、`is_done = true`，并把本次耗时经 `CommStatisticManager::UpdateCost` 记入 send 统计（最小/最大/累计，供 u8-l3 的统计体系消费）；否则再发下一批 `SendCache`。

- [d2d_data_transfer_job.cc:L119-L136](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/data_transfer/d2d_data_transfer_job.cc#L119-L136) —— `GenerateCacheTask`：决定粒度——`block_size != 0`（blocks 模式）用请求里的 block_size，否则用 `cache_entry.stride`（连续模式按整层切）。

- [d2d_data_transfer_job.cc:L138-L142](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/data_transfer/d2d_data_transfer_job.cc#L138-L142) —— 客户端视角的 `PullCache`：一行 `comm_entity_->BatchTransfer(send_tasks_, /*is_put=*/false, /*reversed=*/true, timeout)`，即批量 GET。`BatchTransfer` 的声明见 [comm_entity.h:L173](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/comm_entity.h#L173)，最终落到 HCCL 或 HIXL 传输后端（u6-l7）。

#### 4.3.4 代码实践

**实践目标**：验证批量分批逻辑的边界。

阅读 [d2d_data_transfer_job.cc:L24-L25](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/data_transfer/d2d_data_transfer_job.cc#L24-L25) 的两个常量：`kMaxTaskNum = 1024`、`kMaxBatchPutNum = 64`。再看 [data_transfer_utils.h:L21-L24](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/data_transfer/data_transfer_utils.h#L21-L24) 的 `SendCache` 声明。回答：

1. 一次请求最多能展开多少 op？（每个 tensor × 每个 buffer 段，校验上限在 `send_tasks_.size() == total_num`）
2. 假设 32 层、每层 2 个 tensor、离散 blocks 配对出 10 个连续段，共生成多少 op？（32×2×10 = 640 条）
3. 这些 op 需要几轮 `Process` 才发完？（每轮 `SendCache` 一批 ≤64 条 → 至少 10 轮；每轮之间还要等上一轮 event 完成）

**预期结果**：能准确说出"任务展开是一次性的、执行是分批的"，以及 event 是批次间的同步点。

#### 4.3.5 小练习与答案

**练习 1**：`GetSendTask` 中 `hccl_one_side_op_desc.count` 在什么条件下不等于 `buffer_len`？
**答案**：见 [d2d_data_transfer_job.cc:L63-L64](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/data_transfer/d2d_data_transfer_job.cc#L63-L64)：当且仅当是最后一个 buffer 段且 `tensor_size % block_size != 0`（有余数）时，count 取 remainder，即尾段只传有效字节。

**练习 2**：服务端视角与客户端视角的 D2D job，生成的 op 有何不同？
**答案**：op 结构相同（local/remote/count），但语义相反：服务端是 PUT（把本地 cache 写到请求指定的远端 dst_addr，本地地址来自本地台账）；客户端（`PullCacheByGet`）是 GET（`BatchTransfer` 传 `is_put=false, reversed=true`，从远端台账地址拉回本地的 `send_tasks_` 中的目的地址）。

### 4.4 D2H 与 H2D：经中转 buffer 的两阶段路径

#### 4.4.1 概念说明

当一端在主机内存（HOST placement）时，直接跨节点单边传输往往不可达（host 内存对远端单边访问的支持受限、且 `remote_accessible` 未必打开），于是引入一块**device 中转 buffer**，把一次传输拆成两段：

- **D2H**（目的在 host）：服务端把 device 上的 cache 分块 PUT 到客户端的 device 中转 buffer；客户端再用 `aclrtMemcpy(D2H)` 拷到自己的 host 目的地址。两侧用 **device 上的 flag** 做流控：buffer 装满→置 flag→客户端拷走→回写 flag→buffer 可复用。多块 buffer 轮转形成流水线，拷贝用 8 线程池并发。
- **H2D**（源在 host）：服务端先把 host 源数据 `aclrtMemcpy(H2D)` 到自己的 device buffer，再 `BatchPutAsync` 单边写到客户端 dst_addr。每个 buffer 是一个 IDLE→COPY→TRANSFER→IDLE 的小状态机，两个 buffer 交替实现"拷贝与传输重叠"。

关键工程问题：**buffer 数量有限（D2H 默认 2 块 32MB，H2D 默认 2 块），而数据可能远大于 buffer**——所以两个 job 都有一个"任务切分器"，把整份数据切成 `Start/Transfer/End` 三类子任务序列，按 buffer 轮转编排。

#### 4.4.2 核心流程

D2H 客户端流水线（`D2HDataTransferClient`）：

```text
Prepare:  校验布局 → 从内存池 Alloc N 块 32MB device buffer → 发请求 → 等响应(拿远端flag地址)
          → GenerateTasks 切出 Start/Transfer/End 序列
RunTasks: 顺序消费任务序列（host 线程 + 8线程拷贝池）：
  Start   → 等本端 recv_flag（远端写完该 buffer 才放行）
  Transfer→ 提交线程池 CopyAsync（buffer→host 目的，ACL_MEMCPY_DEVICE_TO_HOST）
  End     → 等全部 future 完成 → 单边写远端 recv_flag "该 buffer 已腾空"
```

D2H 服务端（`D2HDataTransferJob`，由 SendState 驱动）是镜像：

```text
Initialize: 解析请求 → 算本端/远端 flag 基址 → GenerateTasks → SendResponse(带上本端flag地址)
Process:   Start → 等 sync_flag(客户端腾空信号) → Transfer → buffered_sender_.Put(单边写客户端buffer)
           End   → Flush + 写 dst_receive_flag(通知客户端) ；全部下发完等 event → is_done
```

H2D 服务端（`H2DDataTransferJob`）每个 buffer 走状态机：

```text
IDLE     → 取下一批 dst 切片；没有则 END
COPY     → 线程池并发 aclrtMemcpy(H2D) host→buffer；全部完成进 TRANSFER
TRANSFER → BatchPutAsync(buffer→远端dst) + 记 event；event 完成回 IDLE
```

#### 4.4.3 源码精读

**D2H 服务端**：

- [d2h_data_transfer_job.cc:L114-L170](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/data_transfer/d2h_data_transfer_job.cc#L114-L170) —— `Initialize`：算出本端/远端 flag 基址（注意 L124-L127：远端 flag 基址**从已校验的请求布局推导**而非信任线上 `req_size`，防伪造）；为每个 dst 准备 receive flag 并预置 1（首个 buffer 免等）；解析层区间得到 `data_addresses_`；`ResolveBlockSize` 定块大小后 `GenerateTasks` 切任务；最后 `SendResponse` 把本端 sync flag 地址与 block_size 告诉客户端——**应答在 Initialize 阶段就发出**，因为后续数据全走单边写，不再需要控制消息。

- [d2h_data_transfer_job.cc:L172-L221](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/data_transfer/d2h_data_transfer_job.cc#L172-L221) —— `Process`：`kTaskTypeStartBlock` 查该 buffer 的 sync flag（未就绪直接返回等下次调度）；`kTaskTypeTransferBlock` 用 `buffered_sender_.Put` 单边写客户端 buffer；`kTaskTypeEndBlock` Flush 后写 `dst_receive_flag` 通知客户端；序列消费完记录 event，event 完成则 `is_done = true`。

- [d2h_data_transfer_job.cc:L263-L287](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/data_transfer/d2h_data_transfer_job.cc#L263-L287) —— `ResolveBlockSize`：四种布局组合的块大小决策——目的为 blocks 用请求 block_size（blocks→blocks 时还要求与本地 stride 相等）；目的为连续时固定 `kBlockSizeForContMem = 512KB`；且明确拒绝"blocks→连续"。

**任务切分器**：

- [d2h_data_transfer_job.cc:L289-L324](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/data_transfer/d2h_data_transfer_job.cc#L289-L324) —— `DoGenerate`：按 `block_indices` 逐块生成任务；连续 block index 会合并进同一个 span（`AppendBlockTransferTask`，L61-L80：后一块 index = 前一块+1 且不超 max_block_size 就并入）；buffer 写满（块数或 64 个 transfer 任务到顶）就发 `EndBlock` 并轮转到下一块 buffer（`state.buffer_index = (state.buffer_index + 1) % num_buffers_`）。

- [d2h_data_transfer_job.cc:L371-L398](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/data_transfer/d2h_data_transfer_job.cc#L371-L398) —— 大块特例 `DoGenerateForLargeBlock`（block 比 buffer 还大时按 buffer 尺寸再切 chunk）与总入口 `GenerateTasks`（cont 时把 tensor_size 均分为 block_num 块）。

**D2H 客户端**：

- [d2h_data_transfer_job.cc:L437-L482](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/data_transfer/d2h_data_transfer_job.cc#L437-L482) —— `Prepare`：拒绝 blocks→连续；`ValidateD2HClientPullLayout`（L30-L51）校验块数上限（≤60K 且不超请求缓冲区）；从 **NPU 内存池** Alloc `num_buffers_` 块 32MB buffer 与发送 flag；发请求、等响应，从响应里取远端 flag 地址；最后用同一个任务切分器生成与对端**布局一致**的任务序列。

- [d2h_data_transfer_job.cc:L586-L628](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/data_transfer/d2h_data_transfer_job.cc#L586-L628) —— `RunTasks` + `CopyAsync`：临时建 8 线程 `LLMThreadPool`；Start 等本端 recv_flag（超时 `LLM_TIMEOUT`）；Transfer 提交 `CopyAsync`（device buffer → host 目的）；End 等 future 全部完成、单边写远端 flag 宣告 buffer 腾空。

**H2D 服务端**：

- [h2d_data_transfer_job.cc:L66-L95](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/data_transfer/h2d_data_transfer_job.cc#L66-L95) —— `Initialize`：要求源/目的布局一致（blocks 对 blocks、连续对连续，混合直接拒绝）；依赖 NPU 内存池开启；用两个 `TaskBatcher`（src 按 host 数据切、dst 按远端目的切，两者按 buffer 偏移对齐）。

- [h2d_data_transfer_job.cc:L124-L153](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/data_transfer/h2d_data_transfer_job.cc#L124-L153) —— `Process`：对每块 buffer 先懒分配内存（池满则本轮跳过下次再试），随后在内层 while 里反复 `UpdateState` 直到状态不再变化；全部 buffer 到 END 态即 `is_done` 并 `SendResponse`。四个状态类的定义在 [h2d_data_transfer_job.cc:L25-L63](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/data_transfer/h2d_data_transfer_job.cc#L25-L63)（IDLE/COPY/TRANSFER/END）。

- [h2d_data_transfer_job.cc:L243-L258](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/data_transfer/h2d_data_transfer_job.cc#L243-L258) —— `BufferStateTransfer::BatchPutAsync`：把 buffer 切片组装成 `HcclOneSideOpDesc` 批量下发（源 = 本端 buffer + 偏移，目的 = 请求 dst_addr + 偏移），完成后记 event，event 完成才允许 buffer 回到 IDLE 复用。

#### 4.4.4 代码实践

**实践目标**：亲手推演 D2H 流水线中 flag 的握手时序。

情境：2 块 32MB buffer，传输 128MB 连续 cache（block_size=512KB，即 256 块）。

1. 读 `DoGenerate`（[d2h_data_transfer_job.cc:L289-L324](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/data_transfer/d2h_data_transfer_job.cc#L289-L324)），算出每块 buffer 装多少块（32MB/512KB = 64 块）、共几轮（256/64 = 4 轮，buffer0 与 buffer1 各用 2 次）。
2. 画出时序：服务端填 buffer0 → 置 flag → 客户端拷 buffer0 的同时服务端已开始填 buffer1 → …… 体会"两块 buffer 让拷贝与传输重叠"。
3. 回答：如果只有 1 块 buffer，吞吐会怎样？（串行化：每轮必须等"客户端拷完并回写 flag"后服务端才能复用，拷贝与传输无法重叠。）

**预期结果**：一张 flag 握手时序图；结论 `buffer 数 ≈ 2` 是"流水线深度 2"的最小实现。

**待本地验证**：可用 `tests/run_test.sh -s llm_datadist` 中相关桩单测验证任务切分；真机流水线行为需在双 device 环境用 host placement 的 cache 观察。

#### 4.4.5 小练习与答案

**练习 1**：D2H 客户端与服务端的任务序列是各自独立生成的，为什么能对上？
**答案**：两端使用**同一个** `DataTransferTaskGenerator`、同样的输入（块大小、块索引、buffer 尺寸与数量）。blocks→blocks 场景下客户端还会把远端 block 序列传给 `DoGenerateForClientBlocks`（[d2h_data_transfer_job.cc:L326-L362](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/data_transfer/d2h_data_transfer_job.cc#L326-L362)），先按远端序列算出每个 buffer 的块数，再按本地序列生成任务，保证"第 i 个 buffer 恰好装对端第 i 批块"。

**练习 2**：H2D job 中 `Process` 为什么每块 buffer 内部还有一个 `while (state_changed)` 循环？
**答案**：见 [h2d_data_transfer_job.cc:L137-L142](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/data_transfer/h2d_data_transfer_job.cc#L137-L142)：一次 `Process` 调用内允许状态"原地连跳"（如 IDLE→COPY→TRANSFER 在 copy 已完成时一次通过），只有遇到真正的等待点（copy future 未就绪、event 未完成）才返回让出调度。这减少了无意义的跨周期空转。

### 4.5 LayerWiseTransferJob：按层粒度的可控传输

#### 4.5.1 概念说明

前面三类 job 服务 `PullKvCache` 系接口（一次传整个/部分 cache）。`LayerWiseTransferJob` 服务另一个维度：**每次只传一层**（`tensor_num_per_layer` 个 tensor），对应 `TransferCache`/`TransferBlocks` 这类内部接口。它解决的是 PD 分离里的"通信与计算重叠"问题：Decoder 端不需要等全部 KV 到齐，第 0 层到了就可以算第 0 层，同时第 1 层在传。它与 `DataTransferJob` 无继承关系（不入 SendState 流程，调用方同步等待），是**客户端主动发起**的 job。

#### 4.5.2 核心流程

```text
DataCacheEngine::TransferCache
  └─ LayerWiseTransferJob::TransferCache(cache_entry, config, block_config, timeout, access_remote_cache)
       ├─ access_remote_cache=true  → FillRemoteLayerAddrs   # 查远端 cache 表拿目的层地址
       ├─ Prepare                    # 校验层号整除性，按 dst_blocks 分三路生成 op
       │    ├─ dst_blocks 空          → GenerateCacheToCacheTask    (层→层)
       │    ├─ src_blocks 空          → GenerateCacheToBlocksTask   (层→离散块)
       │    └─ 其他                   → GenerateBlocksToBlocksTask  (块→块，连续段合并)
       └─ access_remote_cache=true  → BatchTransfer(is_put=true)   # 单边 PUT，不等 stream
          否则                        → SynchronizeTransferCacheWithRecord  # SendCache+等event+同步stream
```

#### 4.5.3 源码精读

- [layer_wise_transfer_job.cc:L241-L263](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/data_transfer/layer_wise_transfer_job.cc#L241-L263) —— `TransferCache` 总入口：挂 `LLM_DISMISSABLE_GUARD`（异常时 `aclrtStreamAbort` 防挂死）；`access_remote_cache` 为真时先校验本地 cache 可达并填远端层地址，再 `BatchTransfer(..., is_put=true, ...)` 异步 PUT；否则走 `SynchronizeTransferCacheWithRecord` 同步路径。

- [layer_wise_transfer_job.cc:L120-L150](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/data_transfer/layer_wise_transfer_job.cc#L120-L150) —— `Prepare`：核心校验是**层号整除性**——`cache_addrs.size()` 必须是 `tensor_num_per_layer` 的整数倍，然后切出第 `layer_index` 层的地址子区间 `[layer_index × per_layer, (layer_index+1) × per_layer)`，按 `dst_blocks`/`src_blocks` 是否为空三路分派。

- [layer_wise_transfer_job.cc:L27-L43](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/data_transfer/layer_wise_transfer_job.cc#L27-L43) —— `GenerateCacheToCacheTask`：每层每 tensor 一条 op，`count = stride`，batch 偏移由 `batch_index × stride` 算入 localAddr。

- [layer_wise_transfer_job.cc:L45-L82](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/data_transfer/layer_wise_transfer_job.cc#L45-L82) —— `GenerateCacheToBlocksTask`：连续层内存按 `block_mem_size` 切成 block_num 块，要求块数 ≥ 目的 blocks 数，逐块生成 op，尾块取余数。

- [layer_wise_transfer_job.cc:L84-L118](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/data_transfer/layer_wise_transfer_job.cc#L84-L118) —— `GenerateBlocksToBlocksTask`：与 D2D 的离散 Pull 同样的思路——`FindContiguousBlockIndexPair` 把 src/dst block 序列合并成连续段，每段一条 op（`count = stride × 段长`），块粒度下连续段合并能显著减少 op 条数。

- [layer_wise_transfer_job.cc:L185-L211](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/data_transfer/layer_wise_transfer_job.cc#L185-L211) —— `FillRemoteLayerAddrs`：按 `type`（blocks cache_key 或 cache_id）从本端 cache access table 查到远端 cache，校验后取远端第 `dst_layer_index` 层地址并加上 `dst_batch_index × stride` 偏移。**这就是"单边"的关键：目的地址完全来自建链时同步的表，不需要远端进程参与。**

- [layer_wise_transfer_job.cc:L152-L183](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/data_transfer/layer_wise_transfer_job.cc#L152-L183) —— `SynchronizeTransferCacheWithRecord`：循环"发一批 + 轮询 event + 销毁 event"，最后同步 stream 并把耗时记入 per-stream send 统计（与 D2D Process 末尾同一套统计入口）。

调用方与专用 stream：

- [data_cache_engine.cc:L452-L486](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/cache_mgr/data_cache_engine.cc#L452-L486) —— `DataCacheEngine::TransferCache`：查 cache、查 entity、加 pull 锁后，用 `std::call_once` 创建带 `ACL_STREAM_FAST_LAUNCH | ACL_STREAM_FAST_SYNC` 属性的专用 `transfer_stream_`（低时延下发/同步），栈上构造 `LayerWiseTransferJob` 执行。

#### 4.5.4 代码实践

**实践目标**：数清"按层传输"到底省了什么。

假设 32 层、每层 2 tensor、每 tensor stride = 2MB，目的为连续 cache：

1. 读 `GenerateCacheToCacheTask`，算一次整层传输的 op 数（2 条）与字节数（4MB）。
2. 对比 `DataTransferClient` 一次 Pull 全部 32 层（64 条 op、128MB，D2D 路径分 1 批即可发完）。
3. 回答：既然整传 op 更少，为什么还需要 layer-wise？
   **参考答案**：op 条数不是目的，**到达时间**才是。整传时 Decoder 必须等 128MB 全部到齐才能开始计算；layer-wise 让第 i 层到达即可参与计算，理想情况下端到端时延从"总传输时间"降为"首层传输时间 + (层数-1) × max(单层传输, 单层计算)"。当单层计算时间 ≥ 单层传输时间时，传输被计算完全掩盖。

**预期结果**：能用一句话向同伴解释 layer-wise 的收益来源（流水线化，而非减少开销）。

#### 4.5.5 小练习与答案

**练习 1**：`Prepare` 为什么强制 `cache_addrs.size() % tensor_num_per_layer == 0`？
**答案**：层的切分完全靠"起始 tensor 索引 = layer_index × per_layer"的等距假设（[layer_wise_transfer_job.cc:L133-L136](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/data_transfer/layer_wise_transfer_job.cc#L133-L136)）。不整除时尾部会切出残缺层，源/目的地址数对不上，直接判 `LLM_PARAM_INVALID` 拒绝。

**练习 2**：`TransferCache` 的 `access_remote_cache` 参数为 false 时走同步路径，为 true 时走 `BatchTransfer` PUT，两者分别对应什么使用方式？
**答案**：`access_remote_cache=true` 要求双方 cache 均 `remote_accessible`，本端从 cache access table 直接拿远端地址单边 PUT，远端不参与；false 时是本地/对端协作式路径（`SynchronizeTransferCacheWithRecord`：SendCache 下发 + event 轮询 + stream 同步 + 统计记录）。这与 4.2 节 `PullCache` / `PullCacheByGet` 的二分完全同构。

## 5. 综合实践：画出一次 PullKvCache 的完整调用序列图

这是本讲的贯穿任务。请以 device placement 的 `PullKvCache(cache_id, CacheIndex, ...)` 为入口，产出一张包含**双端**的序列图。参考骨架（请补全每一步的函数名与文件行号）：

```text
Decoder(客户端)                                    Prompt(服务端)
──────────────                                    ──────────────
LlmDataDist::PullKvCache
 └ LlmDataDistImpl / V2 层翻译 CacheIndex→CacheKey        (u6-l4)
 └ DataCacheEngine::PullCache                data_cache_engine.cc:132
    ├ GetCacheEntry(cache_id)                本地台账
    ├ GetEntityByRemoteClusterId             建链产物 (u6-l2)
    ├ GetPullMutex / CheckEntityInfo
    └ DataTransferClient::PullCache          data_transfer_client.cc:215
       ├ ConstructTransferInfo               :108  (填 TransferCacheReq 到本端 req 缓冲区)
       ├ SendCacheInfoToRemote ──单边PUT──►  远端 req 缓冲区
       ├ SynchronizeStreamTask               :183  (同步 stream + 自旋 resp flag)
       │                                      FSM 收到请求 → SendState::Preprocess  send_state.cc:74
       │                                       ├ QueryCacheEntryAndOffset      :139
       │                                       ├ ResolveTransferType → D2D     :207
       │                                       └ new D2DDataTransferJob + Initialize :102-111
       │                                      SendState::Process (周期驱动)     :115
       │                                       └ D2DDataTransferJob::Process   d2d_...cc:85
       │                                            ├ 等上批 event
       │                                            ├ SendCache 一批 op(≤64) ──► 单边PUT 数据
       │                                            └ 跗尾批 SendResponse ────► 写客户端 resp 缓冲区
       └ GetResponseInfo                      :153  (读 ret_code)
```

进阶（可选）：再画一张 `placement=HOST` 的版本（换成 `D2HDataTransferClient` + 对端 `D2HDataTransferJob`，多出中转 buffer 与 flag 握手），与上图对比标出新增的交互。

验收标准：图中每个箭头都能对应到本讲引用的一个具体源码位置；三个关键同步点（请求 PUT、数据 PUT、应答 flag 写）能被明确指出。

## 6. 本讲小结

- **job = 请求解析 + 任务展开 + 分批执行 + 完成上报**：`DataTransferJob` 基类只约定 `Initialize/Process/PullCache` 三个接口；服务端由 FSM 的 SendState 按 `src_placement × dst_placement` 三分派创建并周期性非阻塞驱动。
- **`TransferCacheReq` 是两端间的全部合同**：客户端 `DataTransferClient::ConstructTransferInfo` 填写、服务端 SendState 系列函数消费；控制消息本身也走单边 PUT（写远端 req 缓冲区），没有 socket。
- **D2D 是直通路径**：任务一次性展开为 `HcclOneSideOpDesc` 列表，按 ≤64 条分批执行，event 作为批次间同步点；`PullCacheByGet` 模式下客户端可凭 cache access table 直接单边 GET，远端零参与。
- **D2H/H2D 是中转路径**：一端在 host 时经 device buffer 两段搬运，`DataTransferTaskGenerator` 把数据切成 Start/Transfer/End 任务序列，多块 buffer 轮转 + device flag 握手实现拷贝与传输的流水线重叠。
- **LayerWiseTransferJob 提供按层粒度**：每次只传 `tensor_num_per_layer` 个 tensor，用专用 fast stream，收益来自"层到即算"的流水线化；同样具备远端可达表驱动的单边 PUT 与协作式同步两条路径。
- **健壮性设计贯穿始终**：每条客户端路径挂 stream abort guard、所有跨端输入（块索引、buffer_len、count 上限、flag 基址）都经显式校验，超时统一走 `LLM_TIMEOUT`。

## 7. 下一步学习建议

- 下一讲 **u6-l6 FSM：发送/接收状态机** 将从 job 的"驱动者"视角展开：SendState/ReceiveState/IdleState 如何轮转、`DataTransferJob` 存活于 entity 的哪个生命周期阶段，本讲 4.1 节是它的直接前置。
- 想追"op 最终如何落到链路"，进入 **u6-l7 传输后端：TransferEngine 与 HIXL 适配**，看 `CommEntity::BatchTransfer/BatchPutAsync` 背后的 `HixlTransferEngine` 与 `CommAdapter`。
- 建议继续阅读的源码：[src/llm_datadist/link_mgr/comm_entity.h](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/comm_entity.h)（job 的宿主与传输入口）、[src/llm_datadist/common/transfer_message_limits.h](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/common/transfer_message_limits.h)（请求缓冲区的上限常量体系）。

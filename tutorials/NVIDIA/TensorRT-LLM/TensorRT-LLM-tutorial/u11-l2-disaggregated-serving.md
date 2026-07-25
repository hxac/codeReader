# 分离式服务（prefill/decode disaggregation）

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚「聚合式服务」与「分离式服务」的区别，理解为什么要把 prefill（context）和 decode（generation）拆到不同的 GPU 池上。
- 复述一次请求在分离式架构里的完整旅程：编排器（disaggregated server）→ context 实例（算 KV cache + 第一个 token）→ KV cache 跨节点搬运 → generation 实例（接力 decode）。
- 读懂 `KvCacheTransceiverV2` 这条「KV 搬运」链路：发送/接收会话、共识式完成判定、多种通信后端、缓存布局变换。
- 理解「协调服务 + 工作进程舰队（coordinator + worker fleet）」如何把单进程编排器扩展成多进程；以及雪花算法（snowflake）如何在不跨进程协调的情况下生成全局唯一的请求 ID。
- 说清楚 router / disagg_utils 如何保证跨节点请求的「token identity」正确（以 GPT-OSS 的 Harmony 分词为典型场景），以及 KV cache 传输为何要「按 pool 解析 attention cache dtype」。

本讲是进阶到「部署与服务」专家层的第一站，承接 [u11-l1 trtllm-serve 与 OpenAI 兼容服务](u11-l1-trtllm-serve-openai-server.md)（聚合服务 `OpenAIServer`）与 [u7-l1 分页 KV Cache 与 KVCacheManager](u7-l1-paged-kv-cache-manager.md)（KV cache 的块结构）。

## 2. 前置知识

- **LLM 推理的两个阶段**：prefill（context）阶段一次性算出 prompt 所有 token 的 KV cache，是**算力密集**型；decode（generation）阶段逐个生成 token、复用已缓存的 KV，是**显存带宽密集**型。两者的资源画像差异很大。
- **TTFT 与 TPOT**：TTFT（Time To First Token，首 token 延迟）主要由 prefill 决定；TPOT（Time Per Output Token，token 间延迟）主要由 decode 决定。聚合服务里两者抢同一份 GPU 资源，常常顾此失彼。
- **分页 KV Cache**：见 u7-l1。KV cache 被切成等长 block（默认 32 token/块），用页表把逻辑 token 位置映射到物理 block。分离式服务跨节点搬的就是这些 block。
- **OpenAI 兼容协议**：见 u11-l1。`/v1/completions`、`/v1/chat/completions` 等端点。
- **PyExecutor 单步循环**：见 [u3-l2](u3-l2-pyexecutor-step-loop.md)。分离式服务的 KV 收发发生在单步循环里，由 `PyExecutor` 驱动。
- **RDMA / NVLink**：跨 GPU 的高速数据通路。NVLink 用于同节点 GPU 间通信，RDMA（InfiniBand + GPUDirect）用于跨节点 GPU 显存直接互传。KV cache 的底层传输走的正是这些通路。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [docs/source/features/disagg-serving.md](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/features/disagg-serving.md) | 官方设计文档，讲动机、KV 交换架构、多种后端、overlap 优化、雪花 ID、用法、环境变量、FAQ |
| [tensorrt_llm/serve/openai_disagg_server.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/openai_disagg_server.py) | 分离式编排器（orchestrator）的 FastAPI 服务器本体，持有 coordinator 与路由 |
| [tensorrt_llm/serve/openai_disagg_service.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/openai_disagg_service.py) | 编排逻辑：context_first / generation_first 两种编排顺序、标记请求类型、串联 ctx→gen |
| [tensorrt_llm/serve/disagg_coordinator.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/disagg_coordinator.py) | coordinator 抽象基类与两种实现：进程内 owner（`DisaggCoordinatorService`）与工作进程代理（`CoordinatorClient`） |
| [tensorrt_llm/llmapi/disagg_utils.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/llmapi/disagg_utils.py) | 配置 dataclass、`ServerRole` 枚举、MPI 通信子切分、雪花全局请求 ID |
| [tensorrt_llm/_torch/disaggregation/transceiver.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/disaggregation/transceiver.py) | `KvCacheTransceiverV2`：Python 侧的 KV 收发器，对接 `KvCacheManager` 与底层传输 worker |
| [tensorrt_llm/_torch/disaggregation/base/transfer.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/disaggregation/base/transfer.py) | KV 切片 `KVSlice`、会话状态机 `SessionStatus`/`WaitResult`、收发会话基类 |
| [tensorrt_llm/_torch/disaggregation/nixl/agent.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/disaggregation/nixl/agent.py) | NIXL 传输代理的两种实现选择（C++ bindings vs 纯 Python） |
| [tensorrt_llm/llmapi/llm_args.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/llmapi/llm_args.py) | `CacheTransceiverConfig`：通信后端、缓冲、超时等配置 |

> 提醒：本讲源码横跨「服务层（serve/）」「配置层（llmapi/）」「运行时（_torch/disaggregation/）」三层。读的时候注意区分：服务层负责 HTTP 编排与路由，运行时层负责 GPU 显存里 KV block 的实际收发。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 分离角色**（谁是谁、为什么拆）、**4.2 KV 搬运**（block 怎么跨 GPU 走）、**4.3 协调服务与 router**（编排器怎么扩成多进程、怎么保证请求身份正确）。

### 4.1 分离角色：聚合 vs 分离，context / generation / orchestrator

#### 4.1.1 概念说明

聚合式服务（aggregated serving，也就是 u11-l1 讲的普通 `OpenAIServer`）把 prefill 和 decode 放在**同一组 GPU** 上跑。它的代价是：prefill 是一坨大算力任务，跑起来会挤占 decode 的时间片，于是正在生成的请求 TPOT 变大、交互卡顿；而且两阶段被迫共用同一种 GPU 型号和同一种并行策略，无法分别优化 TTFT 和 TPOT。

分离式服务（disaggregated serving）把两阶段**解耦**到不同的 GPU 池：

- **context 实例（prefill worker）**：只负责算 prompt 的 KV cache 和第一个生成 token，算力密集，倾向用大 TP。
- **generation 实例（decode worker）**：只负责接力 decode，带宽密集，倾向用大 batch、可用 PP。
- **orchestrator（disaggregated server）**：一个 OpenAI 兼容的「门面」服务，接收客户端请求，把它们派发给 context 实例，再把 context 产出的「KV cache + 首 token + 元数据」转交给 generation 实例完成续写。

分离的收益是消除了两阶段的互相干扰，TTFT 和 TPOT 可以独立优化；代价是要把 context 算好的 KV cache **跨节点搬到** generation 节点。对「长输入、中等输出」的负载（KV 大、搬运一次划算、干扰最严重）收益最大。

> 关键认知：分离式服务**不是**一个新的推理引擎，context / generation 实例本身就是普通的 `trtllm-serve` 服务（u11-l1 的 `LLM` + `OpenAIServer`）。分离式新增的是「编排器 + KV 跨节点搬运」这一层。引擎层面唯一的要求是：KV cache 传输被启用（配 `cache_transceiver_config`）。

#### 4.1.2 核心流程

以 `context_first`（默认）编排顺序为例，一次请求的完整旅程：

```text
客户端 ──/v1/completions──▶ orchestrator
                             │ 1. 生成全局唯一 disagg_request_id（雪花）
                             │ 2. 标记 request_type="context_only"
                             ├──HTTP──▶ context 实例（prefill）
                             │            │ 算 KV cache + 首 token
                             │            │ respond_and_send_async：把 KV 块「挂上」发送会话
                             │ ◀─HTTP───── 返回 ctx_response（首 token + ctx_params 元数据）
                             │ 3. 标记 request_type="generation_only"，带上 ctx_params
                             ├──HTTP──▶ generation 实例（decode）
                             │            │ request_and_receive_async：按 disagg_request_id 收 KV
                             │            │ KV 到齐 → 接力 decode → 流式吐 token
                             │ ◀─stream── 返回生成结果
客户端 ◀──stream─────────────
```

两个标记非常重要：context 实例收到 `context_only` 就**跳过 generation**（只 prefill），generation 实例收到 `generation_only` 就**跳过 context**（不重算 prompt，直接用搬来的 KV）。这两件事由编排器在转发前注入到请求的 `disaggregated_params` 里。

`ctx_params` 是 context 回传给编排器、再由编排器塞给 generation 的一坨元数据，generation 靠它和 context 建立 KV 传输连接。文档里把这条链路画成了 Figure 6。

#### 4.1.3 源码精读

编排器本体是 `OpenAIDisaggServer`，一个 FastAPI 应用。它注册的对外端点和普通聚合服务几乎一样，但内部多了一个「协调者（coordinator）」对象：

[openai_disagg_server.py:213-221](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/openai_disagg_server.py#L213-L221) — `register_routes` 注册 `/v1/completions`、`/v1/chat/completions`、`/health`、`/cluster_info` 等端点。请求处理委托给 `self._service`（`OpenAIDisaggregatedService`），而**就绪状态与集群拓扑则直接挂在 coordinator 上**——因为这才是 coordinator 的职责。

编排器持有的 coordinator 有两种形态（同一段代码二选一）：

[openai_disagg_server.py:133-145](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/openai_disagg_server.py#L133-L145) — 若给了 `coordinator_url`，本进程是「工作进程」，持有一个代理 `CoordinatorClient`；否则本进程拥有真正的路由与集群状态，构造 `DisaggCoordinatorService`。无论哪种，服务都从 coordinator 上读 `ctx_router` / `gen_router` 来用。这是「服务编排」与「集群管理」解耦的关键。

请求标记与 ctx→gen 串联发生在 service 层：

[openai_disagg_service.py:116-179](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/openai_disagg_service.py#L116-L179) — `_send_disagg_request_ctx_first` 是 context_first 编排主干：先 `get_disagg_request_id()` 拿到雪花 ID，再 `_get_ctx_request` 标记 `context_only` 并经 `ctx_router` 派发；拿到 ctx 响应后 `_get_gen_request` 标记 `generation_only` 并带上 ctx 返回的 disaggregated_params，最后经 `gen_router` 派发到 generation 实例。注意 `disagg_request_id` 在 ctx 重试时可能被替换，所以预约 generation 的 key（`gen_reservation_id`）单独保留。

[openai_disagg_service.py:203-234](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/openai_disagg_service.py#L203-L234) — `_get_ctx_request` / `_get_gen_request` 构造 `DisaggregatedParams`，分别写 `request_type="context_only"` 与 `request_type="generation_only"`，并把 `schedule_style`、`conversation_id`、`ctx_usage` 等字段串好。

`ServerRole` 枚举刻画了所有可能的「角色」，context 与 generation 只是其中两种：

[disagg_utils.py:34-39](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/llmapi/disagg_utils.py#L34-L39) — `ServerRole`：`CONTEXT=0`、`GENERATION=1`，还有 `MM_ENCODER`、`VISUAL_GEN`、`EMBEDDING`。说明这套分离式架构是被设计成可扩展到多模态、VisualGen、Embedding 等多种角色的通用编排框架。

#### 4.1.4 代码实践

**实践目标**：用最小配置亲手起一组分离式服务，观察请求在三个进程间流转。

**操作步骤**（来自官方文档）：

1. 准备两个 context 配置和一个 generation 配置，关键是都带 `cache_transceiver_config`：

   ```yaml
   # context_config.yml —— context 侧要关掉 overlap scheduler
   disable_overlap_scheduler: True
   cache_transceiver_config:
     backend: UCX
     max_tokens_in_buffer: 2048
   ```
   ```yaml
   # gen_config.yml
   cache_transceiver_config:
     backend: UCX
     max_tokens_in_buffer: 2048
   ```

2. 分别启动 context 和 generation 实例（各自是普通的 `trtllm-serve`）：

   ```bash
   CUDA_VISIBLE_DEVICES=0 trtllm-serve TinyLlama/TinyLlama-1.1B-Chat-v1.0 \
       --host localhost --port 8001 --backend pytorch --config ./context_config.yml &
   CUDA_VISIBLE_DEVICES=1 trtllm-serve TinyLlama/TinyLlama-1.1B-Chat-v1.0 \
       --host localhost --port 8002 --backend pytorch --config ./context_config.yml &
   CUDA_VISIBLE_DEVICES=2 trtllm-serve TinyLlama/TinyLlama-1.1B-Chat-v1.0 \
       --host localhost --port 8003 --backend pytorch --config ./gen_config.yml &
   ```

3. 起编排器（注意子命令 `disaggregated`）：

   ```yaml
   # disagg_config.yaml
   hostname: localhost
   port: 8000
   backend: pytorch
   context_servers:
     num_instances: 2
     urls: ["localhost:8001", "localhost:8002"]
   generation_servers:
     num_instances: 1
     urls: ["localhost:8003"]
   ```
   ```bash
   trtllm-serve disaggregated -c disagg_config.yaml
   ```

4. 像调普通 OpenAI 服务一样请求 `localhost:8000`。

**需要观察的现象**：① 编排器日志里能看到请求被路由到某个 context server，再把 ctx_response 转给某个 generation server；② context 实例日志会打印「正在发送 KV cache」，generation 实例打印「正在接收 KV cache」；③ 对比同样请求打到**单个聚合服务**（u11-l1 的 `trtllm-serve`）的结果，最终生成文本应一致。

**预期结果**：分离式与聚合式产出相同文本（因为 KV 内容等价），但分离式下 context / generation 是不同的进程、不同的 GPU。

> 如果没有多卡环境无法实跑，改做「源码阅读型实践」：在 `_send_disagg_request_ctx_first` 里标注出「生成 id → 预约 gen → 发 ctx → 收 ctx_response → 发 gen」五个阶段各对应哪几行，画出时序图。**待本地验证**实际运行日志。

#### 4.1.5 小练习与答案

**练习 1**：为什么文档说「context 实例必须关掉 overlap scheduler」？

**参考答案**：因为 overlap scheduler 会把「上一步的响应处理」与「本步的前向」重叠，而分离式 context 实例在 KV 发送完成前不能释放缓存块、其响应处理（`respond_and_send_async`）与 KV 传输紧耦合。当前实现尚未支持 context 侧的 overlap，所以配置里显式 `disable_overlap_scheduler: True`。

**练习 2**：编排器本身跑模型吗？

**参考答案**：不跑。编排器（`OpenAIDisaggServer`）只做 HTTP 编排与路由，把请求转发给 context / generation 实例；真正加载模型、前向、采样的还是那两组普通的 `trtllm-serve` 进程。

---

### 4.2 KV 搬运：transceiver、会话、多后端与布局变换

#### 4.2.1 概念说明

分离式的核心难点是：context 节点算出的 KV cache 在 **generation 节点的显存里不存在**，必须搬过去。这件事要解决四个子问题：

1. **搬什么**：不是整个显存池，而是「这个请求对应的那些 KV block」。所以要按请求、按层组（layer group）枚举物理 block id。
2. **怎么搬**：底层用 RDMA / NVLink，上层用 MPI / UCX / NIXL / MOONCAKE 等通信库。TensorRT-LLM 把「KV 收发逻辑」与「底层通信库」**解耦**，用一套会话（session）抽象屏蔽差异。
3. **怎么对齐**：context 和 generation 可以用**不同的并行策略**（如 context 用 TP2、generation 用 PP2），于是「同一个 token 在 context 第 0 卡的 KV」与「在 generation 第 0 卡的 KV」对应的层/头并不一样，要做**缓存布局变换**。
4. **怎么不阻塞**：KV 搬运很慢，若一个请求搬 KV 时别的请求都干等，吞吐就崩了。于是把「KV 传输」与「其他请求的计算」**重叠**起来。

把这套逻辑封装起来的就是 **transceiver（收发器）**。PyTorch 后端当前主推 `KvCacheTransceiverV2`，它对接 u7-l1 的 `KVCacheManager`，向下委托给一个 `TransferWorker`（native 实现，内部再选 NIXL/UCX/...）。

会话（session）是 transceiver 的基本工作单元：一个请求的一次 KV 传输 = 一个发送会话（context 侧）配一个接收会话（generation 侧），用 `disagg_request_id` 配对。

#### 4.2.2 核心流程

**发送侧（context 实例，`respond_and_send_async`）**：

```text
prefill 算完
  └─ _create_kv_slice(req)
       │ 遍历每个 layer group，用 reuse_adapter 算出该请求占用的物理 block_ids
       │ 构造 KVSlice(token_range=[0,prompt_len), block_ids_per_layer_groups, ...)
  └─ session.send(slice)         # 把 block 描述交给 TransferWorker，触发 RDMA/NVLink 传输
  └─ _finalize_send              # 打包 aux（首 token / draft token），写 ContextPhaseParams
       └─ ctx 实例把「首 token + 元数据」作为 HTTP 响应回给编排器
```

**接收侧（generation 实例，`request_and_receive_async`）**：

```text
gen 实例收到 generation_only 请求（带 disagg_request_id）
  └─ _create_kv_slice(req)       # gen 侧预分配好的空 block_ids（写入目标）
  └─ session.receive(slice)      # 按 disagg_request_id 与 context 侧配对，收 KV
  └─ 单步循环里反复 check_gen_transfer_status(...)
       │ 轮询会话状态
       │ 完成 → req.state = DISAGG_GENERATION_TRANS_COMPLETE，可进入 decode
       │ 未完成 → 这一步先让位给别的请求（overlap）
```

**会话状态机**（`SessionStatus`）：`INIT → READY → TRANSFERRING → KV_TRANSFERRED → FULLY_TRANSFERRED`，异常时进入 `ERROR` 或 `CANCELLED`。transceiver 的 `check_*_transfer_status` 就是在扫描这些状态。

**多后端**：`cache_transceiver_config.backend` 可选 `DEFAULT`/`UCX`/`NIXL`/`MOONCAKE`/`MPI`（默认 NIXL）。其中 NIXL 自身又可通过环境变量 `TRTLLM_NIXL_KVCACHE_BACKEND` 选底层走 `UCX`（默认）或 `LIBFABRIC`（v0.16.0+）。也就是「NIXL 是上层的传输框架，UCX/LIBFABRIC 是它脚下的传输插件」。

#### 4.2.3 源码精读

`KvCacheTransceiverV2` 继承自 `KvCacheTransceiver`（u7-l2 提过的抽象），是 PyTorch 后端 KV 搬运的真正实现：

[transceiver.py:59-99](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/disaggregation/transceiver.py#L59-L99) — 构造函数：持有 `_dist`（分布式通信，用于 TP/PP 共识）、`_kv_cache_manager`、`_mapping`，并创建 `TransferWorker`（底层传输引擎）。注意 `max_concurrent_sessions` 被设成 `max_batch_size * 20000`，因为 context-only 请求发完 KV 就释放，可以同时在飞很多个；还创建了一个 `CacheReuseAdapter`（前缀复用适配器），搬运时会跳过已命中的 block。

**发送**（context 侧）的主干非常薄：

[transceiver.py:536-543](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/disaggregation/transceiver.py#L536-L543) — `respond_and_send_async`：标记开始时间 → 取/建发送会话 → 把状态置为 `DISAGG_CONTEXT_TRANS_IN_PROGRESS` → `session.send(self._create_kv_slice(req))` → `_finalize_send`（打包 aux、写 `ContextPhaseParams`）。真正的 RDMA/NVLink 传输发生在 `session.send` 内部，由 `TransferWorker` 异步推进。

`_create_kv_slice` 决定「搬哪些 block」：

[transceiver.py:173-244](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/disaggregation/transceiver.py#L173-L244) — 遍历每个 layer group，用 `reuse_adapter.get_block_ids` 取该请求占用的物理 block；按 `prompt_len` 截断（投机解码的 `num_extra_kv_tokens` 不搬）；处理滑动窗口（SWA，丢弃窗口外的过期 block）与 beam search 的打包布局；最后封装成 `KVSlice`。

**接收**（generation 侧）有同步/异步两个入口，单步循环里用的是异步版：

[transceiver.py:581-597](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/disaggregation/transceiver.py#L581-L597) — `request_and_receive_async`：建接收会话、`session.receive(slice)` 发起接收（**不阻塞**），把请求挂到 `_recv_reqs`，状态置为 `DISAGG_GENERATION_TRANS_IN_PROGRESS`。真正的「等结果」在下面这个轮询函数里：

[transceiver.py:664-747](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/disaggregation/transceiver.py#L664-L747) — `check_gen_transfer_status`：扫描所有接收会话的状态，收集 completed / failed / cancelled。**关键点是共识（consensus）**：所有 rank 必须**对同一个请求的结局达成一致**，否则不同 rank 的请求状态会分叉、导致死锁。完成后置 `DISAGG_GENERATION_TRANS_COMPLETE` 并关闭会话。

会话与切片的抽象定义在 base 层，与具体通信库无关：

[base/transfer.py:42-67](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/disaggregation/base/transfer.py#L42-L67) — `KVSlice` 描述「一段要搬的 KV」：`token_range`（搬哪些 token）、`block_ids_per_layer_groups`（每个层组的物理 block id 列表）、`is_last_slice`（多片传输时标记最后一片）。注释里说得分清楚：「每层 token 起点**不**编码在 token_range 里，而是由发送方根据 block 数量反推」。

[base/transfer.py:70-98](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/disaggregation/base/transfer.py#L70-L98) — `SessionStatus` 与 `WaitResult`：会话的七种状态与等待结果的三种取值，是 transceiver 轮询判定的依据。

NIXL 后端有 C++ 与纯 Python 两套实现，按是否「纯 Python 传输 agent」二选一：

[nixl/agent.py:32-57](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/disaggregation/nixl/agent.py#L32-L57) — `use_pure_python_transfer_agent()` 为真时加载 `_agent_py.NixlTransferAgent`（直接用 Python nixl 库，fallback），否则加载 `_agent_cpp.BindingsNixlTransferAgent`（独立的 C++ bindings 模块，支持释放 GIL）。注释特意说明：`nixl_bindings` 是**独立于主 trtllm bindings** 的模块，这样即使没装 NIXL，trtllm 其它功能也照常工作。（本讲规格里提到的 `nixl/__init__.py` 实际为空文件，真正的实现在 `agent.py`。）

#### 4.2.4 代码实践

**实践目标**：对比三种传输后端（NIXL / UCX / MPI），并理解它们与底层 RDMA/NVLink 的关系。

**操作步骤**：

1. 阅读文档的「KV Cache Exchange」「Environment Variables」两节，整理一张「后端选择」表。
2. 在 `CacheTransceiverConfig` 里定位 `backend` 字段，确认可选值：

   [llm_args.py:3961-3990](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/llmapi/llm_args.py#L3961-L3990) — `backend` 是 `Optional[Literal["DEFAULT","UCX","NIXL","MOONCAKE","MPI"]]`，默认 `None`（解析后落到 NIXL）；还有 `transceiver_runtime`（CPP/PYTHON/auto）、`max_tokens_in_buffer`、`kv_transfer_timeout_ms`（默认 60000ms，超时则取消传输）。

   [llm_args.py:3950-3958](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/llmapi/llm_args.py#L3950-L3958) — `_CACHE_TRANSCEIVER_BACKEND_ENV_VARS`：当 `backend="DEFAULT"` 时，按 `TRTLLM_USE_NIXL_KVCACHE` / `..._UCX_...` / `..._MOONCAKE_...` / `..._MPI_...` 这个优先级解析实际后端。

3. 画一张对比表：

   | 后端 | 性质 | 推荐度 | 说明 |
   |------|------|--------|------|
   | NIXL | 上层框架（底下可插 UCX/LIBFABRIC） | 推荐（默认） | 支持动态节点加入/退出、角色切换 |
   | UCX | 直接通信库 | 推荐 | 成熟，RDMA/NVLink 都能走 |
   | MPI | 直接通信库 | 可用 | 历史方案 |
   | MOONCAKE | 直接通信库 | 可用 | 月饼，KVCache 传输优化 |
   | DEFAULT | 占位 | —— | 按环境变量解析到上面四个 |

**需要观察的现象**：NIXL 与 UCX/MPI **不在同一个层次**——NIXL 是「KV 交换框架」，UCX/LIBFABRIC 是它的底层插件；而 UCX/MPI 也可作为**顶层后端**直接使用。

**预期结果**：能向别人解释「为什么 NIXL 默认却又推荐 UCX」——因为 NIXL 默认底下用的就是 UCX（`TRTLLM_NIXL_KVCACHE_BACKEND=UCX`），NIXL 在 UCX 之上多了一层动态扩展能力。

> **待本地验证**：若有多节点 + InfiniBand 环境，可对比 `backend: UCX` 与 `backend: NIXL` 下 KV 传输的带宽（文档 FAQ 提到首几个请求因建连开销带宽偏低，需 warm-up）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `check_gen_transfer_status` 要做「全 rank 共识」？

**参考答案**：因为一个请求横跨多个 TP/PP rank，KV 传输在每个 rank 上独立进行。若 rank 0 认为某请求完成、rank 1 认为失败，两者会把该请求的状态机推进到不同的分支，导致后续调度不一致甚至死锁。共识（「失败/取消任一 rank 发生即为全局；完成必须是全部 rank 同意」）保证所有 rank 对每个请求的结局看法一致。代码里 `_gen_consensus_outcome` 用一次 allgather 批量交换三份 id 列表来完成这件事。

**练习 2**：`max_tokens_in_buffer` 设成多少合适？

**参考答案**：文档建议「大于等于所有请求的最大 ISL（输入序列长）」，这样单个请求的 KV 能一次性塞进传输缓冲，避免分片开销；过小会导致一个请求的 KV 要分多片传输。

---

### 4.3 协调服务与 router：舰队、雪花 ID、token 身份

#### 4.3.1 概念说明

本模块讲三件事，它们都围绕「跨进程/跨节点的正确性」：

**(A) coordinator + worker fleet**：单进程的编排器是一个单线程 orchestrator，它要终止每条客户端连接、跑路由、代理 ctx→gen 这一跳，高并发下会成为吞吐瓶颈。解决办法是**把「服务请求」与「管理集群状态」拆开**：

- **coordinator（协调者）**：单进程，拥有全部集群状态（ctx/gen 路由表、worker 就绪状态、KV-cache-aware 路由的那一个 ZMQ 事件入口）。对外暴露内部 API：`/select`（选下一个 server）、`/finish`（释放）、`/cluster_info`、`/health`。
- **fleet workers（工作进程舰队）**：`num_workers` 个**无状态**编排器进程，靠 `SO_REUSEPORT` 共享同一个对外端口（内核按四元组哈希把连接分发到各 worker）。每个 worker 本地算路由 key（如 block hash），把**放置决策**委托给 coordinator；无状态路由（round_robin/load_balancing）则直接本地决策，不走 coordinator。

**(B) 全局唯一请求 ID（snowflake 雪花算法）**：context 和 generation **必须**用同一个 `disagg_request_id` 来配对 KV 传输——两个在飞请求撞 ID 会串台、损坏传输。编排器要给每个请求发一个**全局唯一**的 64 位正整数。雪花算法让每个 worker **本地自产** ID、无需跨进程协调。

**(C) token identity 与 GPT-OSS**：当用 `kv_cache_aware` 路由时，router 要对请求**分词**、算出 prompt 的 KV block 哈希，据此找「已经有相同前缀 KV」的 generation worker（前缀复用）。问题：GPT-OSS 系模型用 **Harmony 分词**，若 router 的分词方式与 worker 不一致，算出的 block 哈希就对不上，前缀复用失效、甚至把请求导到错的 worker。所以 router 必须知道「这个 checkpoint 是不是 Harmony 模型」、用一致的路径分词——这就是「token identity」问题。

#### 4.3.2 核心流程

**fleet 模式下的请求路由**（以有状态路由 `kv_cache_aware` 为例）：

```text
客户端 ──▶ (SO_REUSEPORT) ──▶ 某个 fleet worker
                                │ 本地算 routing_key（block hash）
                                ├──POST /select──▶ coordinator
                                │                    │ ctx_router.get_next_server_by_key()
                                │ ◀──{server, info}──
                                │ 经 OpenAIHttpClient 把请求发给选中的 ctx server
                                │ ...（ctx→gen 同理，gen 用 /select + /finish）
```

**雪花 ID 的位布局**（64 位，最高位保留 0 保证正数）：

\[ \text{id} = \text{timestamp\_ms}[39] \;\|\; \text{node\_id}[8] \;\|\; \text{process\_id}[6] \;\|\; \text{counter}[10] \]

- `node_id`（0–255）：节点号，默认取 MAC 地址哈希。
- `process_id`（0–63）：该节点上的 worker 进程号（由启动器经 `TRTLLM_DISAGG_WORKER_PROCESS_ID` 注入，每个 worker 不同）。
- `(node_id, process_id)` 这对组合保证「同一毫秒内，同节点的不同 worker 不会撞 ID」。
- 全局 ID 落在 \([2^{40}, 2^{63})\)；本地/预热 ID 落在不相交的 \([0, 2^{40})\)，永不冲突。

**token identity（GPT-OSS）**：router 的 `BlockHashMixin` 现在会从 checkpoint 的 `config.json` 解析出模型类型（`resolve_model_type_from_config`），判断是否需要 Harmony 分词；然后用与 worker 完全一致的 `tokenize_chat_request_for_serving` 路径分词。这样 router 算出的 block 哈希与 worker 实际缓存里的 block 哈希一致，前缀复用/路由才正确。

#### 4.3.3 源码精读

coordinator 的抽象基类定义了「路由 + 就绪 + 集群信息 + 生命周期」这个最小表面：

[disagg_coordinator.py:92-119](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/disagg_coordinator.py#L92-L119) — `DisaggCoordinator`（ABC）：暴露 `ctx_router` / `gen_router` 两个 property，以及 `is_ready` / `cluster_info` / `get_disagg_request_id` 等抽象方法。注释点明：放置（placement）与释放（finish）都通过 `router.get_next_server` / `router.finish_request` 驱动，所以这个表面**只**暴露路由，把「服务 completion」与「管理集群」彻底解耦。

两种实现共享这套表面。**owner 版**：

[disagg_coordinator.py:213-249](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/disagg_coordinator.py#L213-L249) — `DisaggCoordinatorService.select` / `finish` 就是 coordinator 的 `/select` / `/finish` 处理器。`select` 用 `router.get_next_server_by_key` 做放置，并起一个「预约超时任务」`_expire_reservation`——若 worker 选了 server 却迟迟不 `finish`，超时（默认 180s）后自动释放，避免路由状态泄漏。

**代理版**（每个 fleet worker 持有）：

[disagg_coordinator.py:437-498](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/disagg_coordinator.py#L437-L498) — `CoordinatorClient`：构造时用 `is_delegating_client=True` 建「无核路由」（本地算 key、不做放置），然后用 `_maybe_delegate` 包装：**有状态路由**（暴露 `get_next_server_by_key`）包成 `CoordinatorDelegatingRouter`（走 HTTP `/select`），**无状态路由**直接用（本地放置、不问 coordinator）。还有后台 `_sync_coordinator_state` 周期拉 `/cluster_info` 同步就绪状态与无状态路由的 server 列表。

[disagg_coordinator.py:582-586](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/disagg_coordinator.py#L582-L586) — 关键：`CoordinatorClient.get_disagg_request_id` **本地生成**雪花 ID（不经 HTTP），用本 worker 的 `(node_id, process_id)`。注释强调「雪花 ID 自包含、不是共享状态，所以无需协调也不会撞」。

**雪花算法本体**在 disagg_utils 里：

[disagg_utils.py:448-497](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/llmapi/disagg_utils.py#L448-L497) — 位宽常量 `DISAGG_TIMESTAMP_BITS=39`、`DISAGG_NODE_ID_BITS=8`、`DISAGG_PROCESS_ID_BITS=6`、`DISAGG_COUNTER_BITS=10`；`MIN_GLOBAL_ID = 1<<40` 把全局 ID 空间与本地 ID 空间分开；`get_global_disagg_request_id` 用一把锁保护计数器（前瞻 GIL 移除），按位拼装后再旋转到 `[MIN_GLOBAL_ID, 2^63)` 区间。

[disagg_utils.py:510-522](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/llmapi/disagg_utils.py#L510-L522) — `worker_local_process_id` 从环境变量 `TRTLLM_DISAGG_WORKER_PROCESS_ID` 读本 worker 的进程号（启动器按 worker 分配），独立服务默认 0。

**token identity（GPT-OSS）**这条修复横跨 disagg_utils 与 router_utils：

[disagg_utils.py:314-317](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/llmapi/disagg_utils.py#L314-L317) — `extract_router_config`：当 router 类型是 `kv_cache_aware` 且没显式给 `model_path` 时，自动把顶层 `model` 字段塞进去。这样 router 才有 checkpoint 路径可去解析模型类型。

router 侧的 `BlockHashMixin`（`serve/router_utils.py`）据此做 Harmony 感知分词（见提交 9d7ef31f67）：它新增 `_get_model_type()` 从 checkpoint 的 `config.json` 解析模型类型，再调用统一的 `tokenize_chat_request_for_serving(..., use_harmony=self._use_harmony, model_type_resolver=self._get_model_type, ...)`，保证 router 算 block 哈希用的分词路径**和 worker 一致**。GPT-OSS 之前正是因为 router 走了普通 `apply_chat_template`、而 worker 走 Harmony，两边分词不同，导致 KV block 哈希对不上、token identity 错乱。

**KV 传输按 pool 解析 attention cache dtype**（见提交 cf44a1ccee）：这是 C++ 侧的一个细节修复。KV cache 显存池可能分多种 pool（普通注意力 cache、线性注意力 recurrent state、block scales、indexer K cache…）。老代码粗暴地把「pool 0 的 dtype 当作线上传输的统一 dtype」，并留了 TODO；新代码引入 `getTransferDataType()`：

- `isCachePool`：不是 block scales、不是 indexer K cache 的 pool。
- `isAttentionCachePool`：是 cache pool 且**不是**线性注意力（recurrent state）cache。
- 遍历所有 pool，**优先返回 attention cache pool 的 dtype**；没有 attention pool 时回退到任意 cache pool 的 dtype；并检查所有 attention pool 的 dtype 必须一致（否则报错，因为逐 pool 调度尚未实现）。

它通过 `CacheTransBufferManager::getDataType()` 暴露给 formatter，`CacheFormatter::unformat` 改为调 `mCacheTransBufferManager->getDataType()` 而非 `getPrimaryPool(0)->getDataType()`。意义：当模型混用了不同 dtype 的 pool（如 MLA 的潜缓存、SWA 的窗口缓存）时，传输 buffer 用**注意力 cache** 的 dtype 来编解码，避免用错 dtype 解析线上字节。

> 这两个细节一个在 Python 服务层（保证 router 与 worker 的「逻辑身份」一致），一个在 C++ 运行时层（保证 KV 字节的「二进制身份」一致），合起来就是「跨节点请求身份正确」的两道闸。

#### 4.3.4 代码实践

**实践目标**：动手理解雪花 ID 的唯一性，并追踪 router 的 Harmony 感知分词。

**操作步骤**：

1. **雪花 ID 唯一性验证**（纯 Python，无需 GPU）：在仓库根目录跑一段脚本（**示例代码**，非项目原有）：

   ```python
   # 示例代码：验证同一 worker 同毫秒不撞、不同 worker 不撞
   from tensorrt_llm.llmapi.disagg_utils import get_global_disagg_request_id, MIN_GLOBAL_ID

   ids = [get_global_disagg_request_id(node_id=1, process_id=0) for _ in range(2000)]
   assert len(set(ids)) == len(ids), "同 worker 内撞 ID 了！"
   assert all(i >= MIN_GLOBAL_ID for i in ids), "全局 ID 落在了本地 ID 区间！"

   a = get_global_disagg_request_id(node_id=1, process_id=0)
   b = get_global_disagg_request_id(node_id=1, process_id=1)
   print("同节点不同 worker:", a, b, "不同即 OK" if a != b else "撞了！")
   ```
   > 注意：上面循环里同 worker 取 2000 个 id，counter 只有 10 位（1024），所以**预期会**跨毫秒；真正保证唯一的是 `(timestamp_ms, counter)` 组合。若在同一毫秒内取超过 1024 个才会撞——可观察这一边界。

2. **Harmony 分词追踪**：读 `serve/router_utils.py` 里 `BlockHashMixin._get_model_type` 与 `_tokenize`，对照本讲引用的提交 9d7ef31f67 的 diff，回答：为什么 GPT-OSS 之前 router 算的 block 哈希和 worker 对不上？

**需要观察的现象**：① 雪花 id 全部落入 `[2^40, 2^63)`；② 同一 `(node_id, process_id)` 连续生成的 id 大体单调（但不保证严格单调，见函数 docstring）；③ Harmony 模型经 `_get_model_type` 解析后走与 worker 一致的分词分支。

**预期结果**：能说清「token identity = router 与 worker 用同一套分词 → block 哈希一致 → 前缀复用/路由正确」。

> **待本地验证**：示例脚本的运行结果（需在装好 tensorrt_llm 的环境里跑）。

#### 4.3.5 小练习与答案

**练习 1**：fleet 模式下，无状态路由（round_robin）为什么「不走 coordinator」？

**参考答案**：无状态路由的放置决策不依赖任何跨请求的历史状态（轮询/负载均衡只看本地计数），每个 worker 本地就能算出下一个 server 且结果天然合理；只有**有状态**路由（kv_cache_aware 需要全局 block 哈希索引、conversation 需要会话亲和）才必须由单点 coordinator 统一决策以保证全局一致。所以代码里 `_maybe_delegate` 只对暴露 `get_next_server_by_key` 的路由做委托。

**练习 2**：为什么雪花 ID 要分「全局区间」和「本地区间」？

**参考答案**：全局 ID 用于跨节点 KV 传输配对（必须全局唯一，落在 `[2^40, 2^63)`）；本地 ID（如预热请求、worker 内部临时 id）不需要全局唯一，落在 `[0, 2^40)`。两个区间不相交，所以「客户端自带正 id」「服务端自产雪花 id」「本地预热 id」三者永不相撞，免去任何协调。

**练习 3**：`getTransferDataType` 为什么要**排除**线性注意力（recurrent state）pool？

**参考答案**：recurrent state（如 Mamba/SSM 类的线性注意力缓存）有**独立的传输 manager 和 formatter**，走单独的通道；只有普通注意力 cache pool 决定主 KV 传输 buffer 的 dtype。若把 recurrent state 的 dtype 误当作注意力 dtype，就会用错的精度去解析线上 KV 字节。所以遍历时用 `isAttentionCachePool` 把它排除掉。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「全链路追踪」任务：

**任务**：为一次 `context_first` 的分离式请求，画一张**完整的 KV cache 搬运时序图**，并在图上标注每一跳对应的源码位置。

要求覆盖：

1. **编排层**：客户端 → orchestrator（fleet worker）→ `/select` → coordinator → 选定 ctx server → HTTP 发送 `context_only` 请求。
2. **context 计算与发送**：ctx 实例 prefill → `KvCacheTransceiverV2.respond_and_send_async` → `_create_kv_slice` 枚举 block → `session.send` → `_finalize_send` 打包首 token。
3. **回传与转发**：ctx 把首 token + `ctx_params`（含 `disagg_request_id`、`ctx_info_endpoint`）经 HTTP 回给 orchestrator → orchestrator 用**同一个** `disagg_request_id` 构造 `generation_only` 请求 → 选 gen server → 转发。
4. **generation 接收与接力**：gen 实例 `request_and_receive_async` → 按 id 与 ctx 配对收 KV（底层 NIXL/UCX/RDMA）→ `check_gen_transfer_status` 共识判定完成 → `DISAGG_GENERATION_TRANS_COMPLETE` → 进入正常 decode → 流式回吐 token。
5. **身份正确性**：在图旁注明两道「身份闸」——router 的 Harmony 感知分词（token identity，提交 9d7ef31f67）与 C++ 侧按 pool 解析 attention cache dtype（字节 identity，提交 cf44a1ccee）。

**进阶**：把三种后端（NIXL / UCX / MPI）画成可替换的「传输插件」框，标出 NIXL 底下还能插 UCX/LIBFABRIC，说明「为何 NIXL 默认却仍推荐 UCX」。

> 这张图建议用 Mermaid 的 `sequenceDiagram` 画。画完后，你应当能用一句话向别人解释：「分离式服务 = 普通的 ctx/gen trtllm-serve + 一个会路由、会发雪花 id、能把 KV 跨节点搬过去的编排器」。

## 6. 本讲小结

- **分离式服务**把 prefill 与 decode 解耦到不同 GPU 池，消除两阶段互相干扰，独立优化 TTFT/TPOT，代价是要跨节点搬 KV cache；它**不是新引擎**，ctx/gen 实例就是普通的 `trtllm-serve`。
- **编排器**（`OpenAIDisaggServer`）是 OpenAI 兼容门面，内部委托给 **coordinator**（`DisaggCoordinatorService` owner 或 `CoordinatorClient` 代理）做路由与集群管理，把「服务请求」与「管理集群」解耦。
- **KV 搬运**由 `KvCacheTransceiverV2` 负责：发送侧 `respond_and_send_async` 把请求占用的物理 block 封装成 `KVSlice` 经会话异步发出；接收侧 `request_and_receive_async` + `check_gen_transfer_status` 轮询、**全 rank 共识**判定完成；底层 NIXL/UCX/MPI/MOONCAKE 可替换，NIXL 底下还能插 UCX/LIBFABRIC。
- **coordinator + worker fleet** 用 `SO_REUSEPORT` 把编排器水平扩展成多进程：有状态路由委托给单点 coordinator（`/select`/`/finish`），无状态路由本地决策。
- **雪花 ID**（`timestamp_ms|node_id|process_id|counter`，64 位正整数）让每个 worker 本地自产全局唯一 `disagg_request_id`，免去跨进程协调，是 ctx↔gen KV 配对的钥匙。
- **token identity** 两道闸：router 的 Harmony 感知分词（保证 block 哈希与 worker 一致）+ C++ 侧按 pool 解析 attention cache dtype（保证线上 KV 字节用对精度编解码）。

## 7. 下一步学习建议

- 想深入「KV cache 搬运的底层传输」：阅读 `_torch/disaggregation/native/transfer.py`（`TransferWorker`）与 `_torch/disaggregation/native/bounce/`（gather/scatter 缓冲），理解 RDMA/NVLink 之上的缓冲管理。
- 想深入「前缀复用与路由」：阅读 `serve/router.py`（`KvCacheAwareRouter`、`CoordinatorDelegatingRouter`）与 `serve/router_utils.py`（`BlockHashMixin` 的 block 哈希），它是本讲 token identity 的另一半。
- 想看「分离式的真实部署与基准」：阅读 `examples/disaggregated/README.md` 与 `examples/disaggregated/slurm`（SLURM 集群脚本），结合 [u11-l3 基准测试与配置数据库](u11-l3-benchmark-and-config-database.md) 学会用 `trtllm-bench` 量化分离式的 TTFT/TPOT 收益。
- 想看「Dynamo 数据中心级编排」：文档的 Dynamo 一节把分离式扩展到了 K8s + 智能路由 + 动态扩缩容，是本讲 coordinator/worker fleet 思路的工业化版本。
- 下一站可继续 [u11-l3 基准测试与配置数据库](u11-l3-benchmark-and-config-database.md)，或跳到 [u12 二次开发与扩展](../) 系列。

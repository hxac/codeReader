# 与 SGLang 集成：从 PD 分离到弹性 EP

Mooncake 与 SGLang 的多层次集成涵盖 Prefill-Decode（PD）分离、HiCache 分层 KV 缓存、弹性专家并行（Elastic EP）、多模态 Encode-Prefill-Decode 流水线解耦，以及 RDMA P2P 权重传输，共同构建高吞吐、低延迟的 MoE 推理服务体系。

---

## 最小模块 1：PD 分离——Prefill 与 Decode 实例间的 KV Cache 零拷贝传输

### 概念说明

在长文本和高并发场景下，将预填充（Prefill）阶段和解码（Decode）阶段分离到不同实例运行，可以独立扩展各自资源，提升整体吞吐。然而，跨实例 KV Cache 传输成为瓶颈：传统 NCCL 等方案拷贝开销大、延迟高，且难以与 PD 解耦调度协同。

Mooncake 通过 Transfer Engine 实现跨实例的 KV Cache 零拷块传输，利用 RDMA 直接在 GPU 显存间搬运 KV Cache 块，无需 CPU 中转，大幅降低 ITL（Inter-Token Latency）。在 SGLang 中，prefill 实例通过 Mooncake 将 KV Cache 块直接写入 decode 实例的 GPU 显存，decode 实例直接从本地显存读取。

### 伪代码或流程

```text
# Prefill 实例侧
for request in incoming_requests:
    kv_blocks = prefill_compute(request)  # 在 GPU 上计算 KV Cache
    for block in kv_blocks:
        transfer_engine.put_block(
            key=block.key,
            src_addr=block.gpu_ptr,
            dst_addr=decode_gpu_ptr_map[block.token_span],
            size=block.size_bytes,
            remote_rank=decode_rank,
        )  # RDMA 零拷贝写入 decode 实例的 GPU 显存
    send_metadata_to_decode(token_span, block_addrs)

# Decode 实例侧
for request in incoming_from_prefill:
    block_addrs = receive_metadata_from_prefill()
    for addr in block_addrs:
        kv_blocks.append(addr)  # 直接使用本地显存地址，无需拷贝
    decode_compute(kv_blocks)  # 从本地显存读取并计算
```

### 原理分析

- **零拷贝 RDMA**：Transfer Engine 在 prefill 和 decode 实例间建立 RDMA QP，注册 GPU 显存为内存区域，通过 RDMA WRITE 直接写入目标 GPU 地址，绕过 CPU 和中间缓冲区。
- **块粒度传输**：KV Cache 按 Token span 划分为块（page），每块携带全局唯一 key，prefill 实例在生成后立即发送对应块，decode 实例按需组装本地引用。
- **流水线并行**：prefill 实例边生成边发送，decode 实例边接收边解码，隐藏传输延迟。

### 代码实践

Prefill 实例启动参数：
```bash
python -m sglang.launch_server \
  --model-path Qwen/Qwen2.5-7B-Instruct-GPTQ-Int4 \
  --disaggregation-mode prefill \
  --disaggregation-ib-device "mlx5_1" \
  --port 30000 --tp-size 2
```
[sglang/launch_server.py 中 `--disaggregation-mode prefill` 分支启动 prefill 角色](https://github.com/sgl-project/sglang/blob/HEAD/python/sglang/launch_server.py#L400-L410)

Decode 实例启动参数：
```bash
python -m sglang.launch_server \
  --model-path Qwen/Qwen2.5-7B-Instruct-GPTQ-Int4 \
  --disaggregation-mode decode \
  --disaggregation-ib-device "mlx5_1" \
  --port 30001 --tp-size 2
```
[同文件中 decode 分支](https://github.com/sgl-project/sglang/blob/HEAD/python/sglang/launch_server.py#L420-L430)

Router 调度：
```bash
python -m sglang_router.launch_router \
  --pd-disaggregation \
  --prefill "http://192.168.0.137:30000" 8998 \
  --decode "http://192.168.0.140:30001" \
  --policy round_robin
```
[Router 通过 HTTP 端点调度 prefill 和 decode 实例](https://github.com/sgl-project/sglang/blob/HEAD/python/sglang_router/launch_router.py#L150-L165)

### 练习题

1. 在 PD 分离场景中，为什么零拷贝 RDMA 传输比传统 NCCL 更适合 KV Cache 传输？
2. 如何调整块大小（page_size）以平衡传输粒度和缓存命中率？
3. 在单机多 GPU 场景下，如何通过 `--base-gpu-id` 避免 prefill 和 decode 实例争抢同一 GPU？

### 答案

1. NCCL 通常依赖 CPU 中转且需同步等待，RDMA 零拷贝直接 GPU 写入，无 CPU 参与，且传输与计算可流水线并行。
2. 增大 `--page-size` 减少传输次数但增加冗余（未命中块携带未用 token）；减小则提高精确性但增加开销，通常 64~256 为折中。
3. prefill 使用 `--base-gpu-id 0`，decode 使用 `--base-gpu-id 2`，确保实例间 GPU 物理隔离。

---

## 最小模块 2：HiCache 集成——GPU/CPU/分布式三层缓存体系

### 概念说明

SGLang HiCache 扩展了 RadixAttention 为三层缓存：L1（GPU 显存）、L2（CPU 内存）、L3（Mooncake 分布式存储）。当 L1/L2 miss 时，HiCache 从 L3（Mooncake Store）预取 KV Cache 块，并通过 RDMA 并行写入 L2，再拷贝至 GPU 计算。该架构在多轮对话和长上下文场景中显著提升 TTFT（Time To First Token）。

### 伪代码或流程

```text
# 请求处理主流程
def handle_request(tokens):
    # L1/L2 本地匹配
    prefix_l1, prefix_l2 = hiradix_tree.match(tokens)
    miss_tokens = tokens[len(prefix_l1) + len(prefix_l2):]

    # 从 L3 预取（异步）
    if len(miss_tokens) > prefetch_threshold:
        fetch_tokens = l3_backend.query_prefix_key(miss_tokens)
        if fetch_tokens > 256:
            async_prefetch_from_mooncake(fetch_tokens)

    # 等待预取或超时
    prefetched = wait_prefetch(timeout=base + per_ki_token * len(fetch_tokens))
    combined_kv = prefix_l1 + prefix_l2 + prefetched

    # GPU 计算
    output = prefill_compute(combined_kv)

    # 写回 L2/L3（异步）
    async_writeback_to_l2(output)
    if writeback_policy == "write_through":
        async_writeback_to_l3(output)
```

### 原理分析

- **三层缓存一致性**：L1/L2 为实例私有，L3 为集群共享；HiRadixTree 记录每层 KV Cache 的存储位置，L3 元数据实时查询。
- **预取策略**：`best_effort` 立即终止、`wait_complete` 等待全部、`timeout` 等待动态超时（`base + per_ki_token * num_token`），平衡延迟与命中率。
- **异步并行**：预取和写回通过独立线程执行，RDMA 并行拉取多个存储节点数据；L2↔L3 零拷贝，GPU↔L2 通过 DMA 搬运。
- **多 Rank 同步**：在 TP 多 GPU 场景，通过 `all_reduce(min)` 确保 Rank 对 L3 命中长度判断一致，避免计算发散。

### 代码实践

Mooncake L3 后端配置：
```bash
export MOONCAKE_MASTER=127.0.0.1:50051
export MOONCAKE_TE_META_DATA_SERVER="http://127.0.0.1:8080/metadata"
export MOONCAKE_GLOBAL_SEGMENT_SIZE="8gb"
export MC_MMAP_ARENA_POOL_SIZE="56gb"
export MC_STORE_USE_HUGEPAGE="1"
export MC_STORE_HUGEPAGE_SIZE="2MB"

python -m sglang.launch_server \
  --enable-hierarchical-cache \
  --hicache-storage-backend mooncake \
  --hicache-storage-prefetch-policy timeout \
  --model-path [model_path]
```
[HiCache 启用 Mooncake 后端](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/getting_started/examples/sglang-integration/hicache-integration-v1.md#L161-L219)

预取超时参数：
```bash
--hicache-prefetch-timeout-base-ms 5 \
--hicache-prefetch-timeout-per-ki-token-ms 2
```
[HiCache 超时计算公式](https://github.com/sgl-project/sglang/blob/HEAD/python/sglang/hiradix_tree.py#L450-L460)  

### 练习题

1. 在多轮对话场景中，为什么 Mooncake L3 缓存能提升 TTFT？
2. `write_through` 与 `write_back` 写回策略在什么场景下各自更优？
3. 如何根据 GPU/CPU/网络配置调整 `MOONCAKE_GLOBAL_SEGMENT_SIZE` 和 `MC_MMAP_ARENA_POOL_SIZE`？

### 答案

1. L3 共享缓存跨实例保留历史轮次 KV Cache，后续请求直接命中预取，避免重复 prefill，降低 TTFT。
2. `write_through` 适合带宽充足且需强共享场景，实时备份至 L3；`write_back` 适合容量受限且需最大化 L1 利用率场景，仅在 L1 驱逐时写回。
3. `MOONCAKE_GLOBAL_SEGMENT_SIZE` 为每实例贡献的 L3 容量，总 L3 容量为所有实例之和；`MC_MMAP_ARENA_POOL_SIZE` 为 L2 预分配池，需匹配 `--hicache-size`，通常设置为 `hicache-size + global_segment_size` 的 1.2~1.5 倍。

---

## 最小模块 3：弹性 EP（Elastic Expert Parallelism）——MoE 推理的容错调度

### 概念说明

在 MoE（Mixture-of-Experts）模型推理中，不同节点持有不同专家权重，传统的 NCCL all-to-all 通信在节点故障时难以恢复。Mooncake EP（Expert Parallelism）结合 Mooncake PG（Process Group）提供弹性调度：通过 `active_ranks` 张量标记健康节点，dispatch/combine 阶段自动规避故障节点，并通过 RDMA P2P 传输激活值，在 prefill/decode 解耦场景下替换 NCCL all-to-all。

### 伪代码或流程

```text
# 弹性 EP 初始化
active_ranks = torch.ones(world_size, dtype=torch.int32, device="cuda")
dist.init_process_group(
    backend="mooncake",
    pg_options=pg.MooncakeBackendOptions(active_ranks, max_world_size=8),
)
buffer = Buffer(group, num_ep_buffer_bytes)

# Dispatch 阶段
def dispatch(x, topk_idx, active_ranks):
    recv_x, recv_count, handle = buffer.dispatch(
        x, topk_idx, active_ranks,
        num_experts=num_global_experts,
        timeout_us=-1,
        use_fp8=True,
    )
    # recv_x: 当前 rank 负责的专家输入（已从其他 rank RDMA 拉取）
    return recv_x, recv_count, handle

# Combine 阶段
def combine(expert_out, topk_idx, topk_weights, active_ranks, handle):
    combined = buffer.combine(
        expert_out, topk_idx, topk_weights, active_ranks,
        handle=handle, zero_copy=True,
    )
    # expert_out 通过 RDMA P2P 写回源 rank
    return combined

# 节点故障恢复
if rank_failed(3):
    active_ranks[3] = 0
    pg.recover_ranks(backend, join_ranks=[3])  # 新 rank 3 加入
    buffer.update_ep_member()  # 刷新 RDMA QP
```

### 原理分析

- **DeepEP 模型**：dispatch 将 token 按 top-k 专家分配到对应 rank，combine 将专家输出按路由权重加权归约至源 token。
- **Mooncake 传输加速**：dispatch/combine 通过 Mooncake EP Buffer 的 RDMA/IPC 传输激活值，替代 NCCL all-to-all，支持 intra-node P2P 和 inter-node RDMA。
- **弹性容错**：`active_ranks` 张量标记健康节点，dispatch/combine 内核自动跳过失活 rank；timeout 检测将超时源 rank 标记为 0。
- **PG 集成**：EP Buffer 通过 Mooncake PG 交换 RDMA 内存区域和 QP 元数据，并在 PG 恢复后调用 `update_ep_member()` 刷新传输层。

### 代码实践

SGLang EP Backend 启动参数：
```bash
# Prefill 实例
python -m sglang.launch_server \
  --model-path deepseek-ai/DeepSeek-V3-0324 \
  --disaggregation-mode prefill \
  --elastic-ep-backend mooncake \
  --moe-a2a-backend mooncake \
  --tp-size 8 --dp-size 8

# Decode 实例
python -m sglang.launch_server \
  --model-path deepseek-ai/DeepSeek-V3-0324 \
  --disaggregation-mode decode \
  --elastic-ep-backend mooncake \
  --moe-a2a-backend mooncake \
  --tp-size 8 --dp-size 8
```
[EP Backend 通过 `--moe-a2a-backend mooncake` 启用 Mooncake EP 传输](https://github.com/sgl-project/sglang/blob/HEAD/python/sglang/launch_server.py#L500-L520)

Mooncake EP API：
```python
from mooncake.mooncake_ep_buffer import Buffer

buffer = Buffer(group, num_ep_buffer_bytes)
recv_x, recv_count, handle, event, hook = buffer.dispatch(
    x, topk_idx, active_ranks,
    num_max_dispatch_tokens_per_rank=128,
    num_experts=288,
    use_fp8=True,
)
combined_x, event, hook = buffer.combine(
    expert_out, topk_idx, topk_weights, active_ranks,
    handle=handle, zero_copy=True,
)
```
[Mooncake EP dispatch/combine API](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/python-api-reference/ep-backend.md#L294-L397)

### 练习题

1. 在 MoE 推理中，为什么 EP 需要 `active_ranks` 而非依赖 PG 集合通信？
2. 如何通过 `timeout_us` 参数平衡检测延迟与故障恢复时间？
3. 在 PD 分离 + EP 场景下，如何避免 prefill 和 decode 实例的 EP Buffer 传输冲突？

### 答案

1. EP dispatch/combine 是 P2P 模式，不依赖集合通信；`active_ranks` 在内核级跳过故障节点，避免超时等待所有 rank。
2. 较小 `timeout_us`（如 500µs）快速检测故障但可能在网络抖动时误判；较大 `timeout_us`（如 -1 禁用）避免误判但延长恢复时间，典型值为 1000~5000µs。
3. prefill 和 decode 实例各自独立 EP Buffer，prefill→decode 通过 Transfer Engine 传输 KV Cache，EP 传输激活值仅在 prefill/decode 内部进行。

---

## 最小模块 4：多模态流水线（Encoder-Prefill-Decode）——视觉编码与文本生成的解耦

### 概念说明

多模态模型（如 LLaVA、InternVL）包含图像编码器（Vision Transformer）和语言模型。传统部署将两者耦合在同一实例，导致资源争用和扩展困难。Mooncake EPD（Encoder-Prefill-Decode）后端将编码器、prefill、decode 分离到三个节点角色，通过 Mooncake Transfer Engine 传输视觉嵌入（Encoder→Prefill）和 KV Cache（Prefill→Decode），实现流水线并行。

### 伪代码或流程

```text
# Encoder 节点
def encoder_node():
    for image in incoming_images:
        visual_embeddings = vit_encoder(image)  # ViT 编码
        transfer_engine.put_embeddings(
            key=image.id,
            src_addr=visual_embeddings.gpu_ptr,
            dst_addrs=prefill_gpu_ptr_map[image.id],
            size=visual_embeddings.size_bytes,
            remote_ranks=[prefill_rank],
        )  # RDMA 写入 prefill 节点

# Prefill 节点
def prefill_node():
    for request in incoming_requests:
        # 从 Encoder 节点接收视觉嵌入
        visual_embeddings = wait_encoder_embeddings(request.image_id)
        # 本地 prefill（文本 + 视觉 token）
        kv_blocks = prefill_compute(request.text_tokens + visual_embeddings)
        # 传输 KV Cache 至 Decode 节点（同 PD 分离）
        transfer_kv_to_decode(kv_blocks)

# Decode 节点
def decode_node():
    for request in incoming_from_prefill:
        kv_blocks = receive_kv_from_prefill()
        decode_compute(kv_blocks)
```

### 原理分析

- **三节点角色**：Encoder（`--encoder-only`）专注图像编码；Prefill（`--disaggregation-mode prefill --language-only`）接收视觉嵌入并进行文本 prefill；Decode（`--disaggregation-mode decode`）生成输出。
- **视觉嵌入传输**：Encoder→Prefill 通过 `--encoder-transfer-backend mooncake` 使用 RDMA 传输视觉嵌入，Prefill 通过 `--encoder-urls` 指定 encoder 地址。
- **KV Cache 传输**：Prefill→Decode 复用 PD 分离机制，通过 `--disaggregation-transfer-backend mooncake` 传输 KV Cache。

### 代码实践

Encoder 节点启动：
```bash
python -m sglang.launch_server \
  --model-path $MODEL \
  --encoder-only \
  --encoder-transfer-backend mooncake \
  --port 30002
```
[Encoder 节点通过 `--encoder-only` 启用纯编码模式](https://github.com/sgl-project/sglang/blob/HEAD/python/sglang/launch_server.py#L600-L610)

Prefill 节点启动：
```bash
python -m sglang.launch_server \
  --model-path $MODEL \
  --disaggregation-mode prefill \
  --disaggregation-transfer-backend mooncake \
  --encoder-transfer-backend mooncake \
  --language-only \
  --encoder-urls http://127.0.0.1:30002,http://127.0.0.1:30003 \
  --port 30000
```
[Prefill 节点接收 encoder 嵌入并启用 PD 分离](https://github.com/sgl-project/sglang/blob/HEAD/python/sglang/launch_server.py#L620-L630)

Decode 节点启动：
```bash
python -m sglang.launch_server \
  --model-path $MODEL \
  --disaggregation-mode decode \
  --disaggregation-transfer-backend mooncake \
  --port 30001
```
[Decode 节点接收 prefill 传输的 KV Cache](https://github.com/sgl-project/sglang/blob/HEAD/python/sglang/launch_server.py#L640-L650)

### 练习题

1. 为什么在多模态场景中，将 Encoder 独立部署能提升整体吞吐？
2. 如何调整 `--encoder-urls` 以实现多 encoder 节点负载均衡？
3. 在 EPD 场景下，视觉嵌入传输与 KV Cache 传输如何共享 Mooncake Transfer Engine 而不冲突？

### 答案

1. Encoder 和 Prefill 计算模式不同（图像编码 vs 文本 prefill），独立部署可分别扩展（如多 encoder 节点对应高图像并发），避免 GPU 资源争用。
2. 多 encoder 地址以逗号分隔，Prefill 节点按轮询或哈希策略选择 encoder 节点，`--encoder-urls http://host1:port1,http://host2:port2`。
3. Transfer Engine 支持多流并发，视觉嵌入和 KV Cache 使用不同 QP 和内存注册，通过 `key` 区分数据流，prefill 节点独立接收两种流。

---

## 最小模块 5：权重传输——RDMA P2P 的 MoE 专家权重分发

### 概念说明

在 MoE 模型的弹性 EP 场景中，专家权重可能在节点间动态迁移或复制。传统权重加载依赖本地存储和 NCCL 广播，延迟高且无法细粒度共享。Mooncake 通过 Transfer Engine 的 P2P 传输实现专家权重的 RDMA 直传，支持按需拉取和增量更新。

### 伪代码或流程

```text
# 权重服务节点
def weight_server():
    for expert_id in local_experts:
        weight_ptr = load_expert_weight(expert_id)
        transfer_engine.register_weight(
            key=expert_id,
            addr=weight_ptr,
            size=weight_size_bytes,
        )
    # 监听权重请求
    while True:
        req = receive_weight_request()
        transfer_engine.send_weight(
            key=req.expert_id,
            dst_addr=req.dst_gpu_ptr,
            remote_rank=req.request_rank,
        )  # RDMA 直接写入请求节点的 GPU 显存

# 推理节点
def inference_node():
    for expert_id in required_experts:
        if expert_id not in local_weights:
            dst_ptr = allocate_gpu_buffer(expert_size)
            transfer_engine.request_weight(
                expert_id=expert_id,
                dst_addr=dst_ptr,
                server_rank=weight_server_rank,
            )
            wait_weight_ready(expert_id)  # 等待 RDMA 写入完成
        expert_weights.append(local_weights[expert_id])
    run_moe_compute(expert_weights)
```

### 原理分析

- **按需拉取**：推理节点仅在需要某专家权重时请求权重服务器，避免全量广播。
- **RDMA P2P**：权重服务器通过 RDMA WRITE 直接写入请求节点的 GPU 显存，无需 CPU 中转。
- **增量更新**：权重更新时仅传输差异部分，减少带宽占用。
- **与 EP 协同**：EP dispatch/combine 传输激活值，权重传输并行进行，通过不同 QP 隔离流。

### 代码实践

Mooncake P2P 权重传输 API（概念示例）：
```python
from mooncake import TransferEngine

engine = TransferEngine(protocol="rdma", device_name="mlx5_1")

# 注册权重
engine.register_weight("expert_42", weight_gpu_ptr, weight_size)

# 请求权重
def request_weight(expert_id, dst_ptr, server_rank):
    engine.rdma_write(
        key=expert_id,
        src_addr=None,  # 由服务器查询注册地址
        dst_addr=dst_ptr,
        remote_rank=server_rank,
    )

# 等待完成
engine.wait_completion(expert_id)
```
[P2P 传输基于 Transfer Engine 的 RDMA WRITE](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/cpp/transfer_engine/include/transfer_engine.h#L200-L220)

### 练习题

1. 为什么 MoE 权重传输需要 P2P 模式而非集合通信广播？
2. 在推理节点缓存专家权重时，如何处理权重驱逐和缓存一致性？
3. 权重传输与 EP 激活值传输如何共享 RDMA 带宽而不相互饿死？

### 答案

1. P2P 按需拉取避免广播全部权重，节省带宽；集合通信广播需等待所有节点就绪且无法细粒度缓存。
2. 采用 LRU 驱逐策略，并通过版本号或元数据服务器同步更新；驱逐前确保无活跃计算引用该权重。
3. 通过 QO S 优先级或权重分配（如 70% 带宽给激活值，30% 给权重），或错峰传输（激活值传输优先，权重在后台补位）。

---

## 总结

本讲义覆盖了 Mooncake 与 SGLang 集成的五个最小模块：PD 分离实现 prefill/decode 实例间 KV Cache 零拷贝传输，HiCache 建立三层 KV 缓存体系，弹性 EP 提供 MoE 推理的容错调度，多模态流水线解耦编码器与语言模型，以及权重传输的 RDMA P2P 分发机制。这些技术共同支撑高吞吐、低延迟的 MoE 和多模态推理服务。

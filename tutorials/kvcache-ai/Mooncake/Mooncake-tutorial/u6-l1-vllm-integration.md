# 与 vLLM 集成：PD 分离与 KVCache 共享

本讲义深入讲解 Mooncake 如何与 vLLM 深度集成，实现 Prefill-Decode（PD）分离推理与跨实例 KVCache 共享。我们将剖析 vLLM connector 的工作原理、KVCache block 传输流程、HiCache 分层缓存集成，以及在 Kimi K2 等大规模生产环境中的部署实践。

## 最小模块 1：vLLM Connector 架构

### 概念说明

vLLM Connector 是 Mooncake 与 vLLM 集成的核心接口，负责在 vLLM 的推理流程中插入 KV cache 的传输和存储逻辑。Mooncake 提供两种主要的 Connector：

1. **MooncakeConnector**：用于 PD 分离场景，实现 prefill 节点到 decode 节点的直接 KV cache 传输
2. **MooncakeStoreConnector**：用于 KV cache 存储与共享，将 KV cache 卸载到分布式存储池

这两种 Connector 可以单独使用，也可以组合使用（通过 MultiConnector），实现既有 PD 分离又有 KV cache 共享的混合部署。

### 伪代码与流程

```python
# MooncakeConnector 工作流程（PD 分离场景）
def mooncake_connector_workflow():
    # Scheduler 侧逻辑
    if role == "kv_producer":
        # Prefill 节点
        for request in incoming_requests:
            compute_prefill(request)
            blocks = get_kv_blocks(request)
            mark_blocks_for_send(blocks)
            
    elif role == "kv_consumer":
        # Decode 节点
        for request in incoming_requests:
            remote_params = parse_kv_transfer_params(request)
            if remote_params.do_remote_prefill:
                # 从远程拉取 KV cache
                blocks_to_pull = identify_missing_blocks(request)
                trigger_kv_receive(blocks_to_pull, remote_params)
            compute_decode(request)
    
    # Worker 侧逻辑（异步传输）
    if role == "kv_producer":
        # 后台线程发送 KV cache
        send_kv_blocks_to_decode(nodes)
    elif role == "kv_consumer":
        # 后程线程接收 KV cache
        receive_kv_blocks_from_prefill(nodes)

# MooncakeStoreConnector 工作流程（KV cache 共享场景）
def mooncake_store_connector_workflow():
    # 初始化连接到 MooncakeStore
    store = connect_to_mooncake_store(config)
    
    if role == "kv_both":
        # 单节点模式：既是生产者也是消费者
        for request in incoming_requests:
            # 检查 L3 缓存（MooncakeStore）
            cached_blocks = store.get(request.hash)
            if cached_blocks:
                load_cached_kv(cached_blocks)
            else:
                compute_prefill(request)
                store.put(request.hash, get_kv_blocks())
            compute_decode(request)
            
    elif role == "kv_producer":
        # Prefill 节点：写入 L3
        compute_prefill(request)
        store.put(request.hash, get_kv_blocks())
        
    elif role == "kv_consumer":
        # Decode 节点：从 L3 读取
        cached_blocks = store.get(request.hash)
        load_cached_kv(cached_blocks)
        compute_decode(request)
```

### 原理分析

#### MooncakeConnector 的双端设计

MooncakeConnector 采用 Scheduler-Worker 分离设计：

- **Scheduler 侧（决策层）**：负责判断请求是否需要远程 KV cache，构建传输元数据
- **Worker 侧（执行层）**：负责实际的 KV cache 数据传输

关键决策点：

1. **远程 Prefill 判断**：当 decode 节点收到请求时，检查 `kv_transfer_params` 中的 `do_remote_prefill` 标志
2. **远程 Decode 触发**：当 prefill 节点完成请求后，检查是否需要将 KV cache 发送到 decode 节点
3. **前缀缓存命中处理**：如果 decode 节点完全命中 L3 缓存，无需 prefill，直接通知 prefill 节点释放资源

#### MooncakeStoreConnector 的 L3 存储抽象

MooncakeStoreConnector 将 MooncakeStore 抽象为 L3 层级存储：

- **Hash-based 去重**：通过 block hash 作为 key 存储 KV cache，相同 prompt 的不同请求共享同一份数据
- **透明缓存层次**：对 vLLM 而言，L3 只是另一个缓存层级，自动处理 miss 和 hit
- **零拷贝传输**：通过 RDMA 直接从远程内存读取到本地 GPU，无需 CPU 中转

### 代码实践

#### MooncakeConnector 的核心实现

[https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-wheel/mooncake/mooncake_connector_v1.py#L122-L143](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-wheel/mooncake/mooncake_connector_v1.py#L122-L143)

这段代码定义了 `MooncakeConnector` 类，根据角色创建 Scheduler 或 Worker 实例：

```python
class MooncakeConnector(KVConnectorBase_V1, SupportsHMA):
    def __init__(self, vllm_config: VllmConfig, role: KVConnectorRole):
        assert vllm_config.kv_transfer_config is not None
        assert vllm_config.kv_transfer_config.engine_id is not None
        super().__init__(vllm_config, role)
        self.engine_id: EngineId = vllm_config.kv_transfer_config.engine_id
        
        if role == KVConnectorRole.SCHEDULER:
            self.connector_scheduler: Optional[MooncakeConnectorScheduler] = \
                MooncakeConnectorScheduler(vllm_config, self.engine_id)
            self.connector_worker: Optional[MooncakeConnectorWorker] = None
        elif role == KVConnectorRole.WORKER:
            self.connector_scheduler = None
            self.connector_worker = MooncakeConnectorWorker(
                vllm_config, self.engine_id)
```

#### Scheduler 侧的远程 Prefill 判断

[https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-wheel/mooncake/mooncake_connector_v1.py#L252-L284](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-wheel/mooncake/mooncake_connector_v1.py#L252-L284)

这个方法判断是否需要从远程拉取 KV cache：

```python
def get_num_new_matched_tokens(
        self, request: "Request",
        num_computed_tokens: int) -> tuple[int, bool]:
    """
    对于远程 prefill，从引擎执行之间异步拉取所有 prompt blocks。
    
    返回:
    * 可以从外部 KV cache 加载的 token 数量
    * 外部 KV cache tokens 是否会被异步加载
    """
    params = request.kv_transfer_params
    
    if params is not None and params.get("do_remote_prefill"):
        # 远程 prefill：从远程获取所有 prompt blocks
        count = len(request.prompt_token_ids) - num_computed_tokens
        if count > 0:
            return count, True
    
    # 该请求无需远程 prefill
    return 0, False
```

#### Worker 侧的 KV Cache 传输

[https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-wheel/mooncake/mooncake_connector_v1.py#L587-L615](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-wheel/mooncake/mooncake_connector_v1.py#L587-L615)

这个方法负责实际的 KV cache 数据传输：

```python
async def send_kv_to_decode(self, meta: MooncakeAgentMetadata):
    send_reqs: list[tuple[ReqId, SendBlockMeta]] = []
    for req_id in meta.request_ids:
        send_meta = self.reqs_need_send.get(req_id)
        if send_meta is None:
            logger.warning("Request %s not found in reqs_need_send", req_id)
            return
        # 标记为未过期，立即发送
        send_meta.expire_time = float("inf")
        send_reqs.append((req_id, send_meta))

    src_ptrs, dst_ptrs, lengths = await self._build_transfer_params(send_reqs, meta)
    remote_session = f"{meta.remote_hostname}:{meta.remote_port}"
    ret_value = await self.sender_loop.run_in_executor(
        self._sender_executor,
        self._send_blocks,
        remote_session,
        src_ptrs,
        dst_ptrs,
        lengths,
    )

    if ret_value != 0:
        raise RuntimeError(f"Error in batch_transfer_sync_write: {ret_value}")

    for req_id in meta.request_ids:
        del self.reqs_need_send[req_id]

    self.finished_sending_reqs.update(meta.request_ids)
```

### 练习题

1. **设计题**：假设你有 4 个 prefill 节点和 8 个 decode 节点，如何设计负载均衡策略来最大化 KV cache 传输效率？

2. **故障排查题**：在 PD 分离场景中，如果 decode 节点无法从 prefill 节点拉取 KV cache，可能的原因有哪些（至少列举 3 个）？

3. **优化题**：在高并发场景下，MooncakeConnector 的 `send_kv_to_decode` 方法可能成为瓶颈，提出至少两种优化方案。

4. **场景题**：对于混合工作负载（既有短 prompt 又有长 prompt），如何动态调整 prefill 和 decode 节点的比例？

### 答案

1. **设计题答案**：
   - **基于请求队列长度的负载均衡**：实时监控各 prefill 节点的请求队列长度，将新请求路由到队列最短的节点
   - **基于 KV cache 大小的调度**：根据请求的预期 KV cache 大小（基于 prompt 长度估算）进行调度，避免单个节点过载
   - **亲和性调度**：相同或相似 prompt 的请求路由到同一 prefill 节点，提高 KV cache 重用率
   - **实现方式**：在代理服务器层面实现调度策略，通过 `--scheduling` 参数选择算法

2. **故障排查题答案**：
   - **网络连接问题**：prefill 和 decode 节点之间的 RDMA/TCP 连接未建立或中断
   - **元数据不匹配**：`kv_transfer_params` 中的 `remote_host` 或 `remote_port` 配置错误
   - **Mooncake Transfer Engine 初始化失败**：RDMA 设备未正确注册或 `mooncake_transfer_engine` 未安装
   - **请求超时**：`VLLM_MOONCAKE_ABORT_REQUEST_TIMEOUT` 设置过短，导致请求被提前释放
   - **Block ID 不匹配**：prefill 和 decode 节点的 block 分配策略不一致，导致本地和远程 block IDs 无法对应

3. **优化题答案**：
   - **批处理优化**：将多个小请求的 KV cache 合并为一个批次传输，减少 RDMA 操作次数
   - **流水线并行**：在 prefill 计算第 \(N\) 层的同时，开始传输第 \(N-1\) 层的 KV cache，隐藏传输延迟
   - **多线程并发**：增加 `VLLM_MOONCAKE_SENDER_WORKERS` 数量，使用更多线程并行发送 KV cache
   - **预取策略**：根据请求的模式（如多轮对话）提前预测可能需要的 KV cache，提前传输到 decode 节点

4. **场景题答案**：
   - **动态扩缩容**：使用 `MultiConnector` 模式，运行时通过 Admin API 动态添加或移除 prefill/decode 节点
   - **自适应调度**：监控工作负载的 prompt 长度分布，长 prompt 占比高时增加 prefill 节点，短 prompt 占比高时增加 decode 节点
   - **混合角色节点**：对于负载波动大的场景，使用 `kv_both` 角色，使节点既能 prefill 又能 decode，提高资源利用率
   - **预测性扩容**：基于历史数据和趋势预测，在流量高峰到来之前提前调整节点数量

## 最小模块 2：Prefill-Decode 分离架构

### 概念说明

Prefill-Decode（PD）分离是一种推理架构优化策略，将大语言模型推理的两个阶段——Prefill（处理输入 prompt）和 Decode（生成输出 tokens）——分离到不同的计算节点上执行。

这种分离的核心动机是：

1. **计算模式差异**：Prefill 阶段的计算是高度并行的（所有 tokens 可以同时处理），而 Decode 阶段是串行的（每个 token 依赖前一个 token）
2. **资源需求不同**：Prefill 需要更大的显存来存储完整的 KV cache，Decode 需要更低的延迟来快速生成每个 token
3. **负载不均衡**：在实际服务中，Prefill 和 Decode 的资源消耗比例动态变化，耦合部署难以优化

通过 PD 分离，我们可以：
- 独立扩展 Prefill 和 Decode 节点数量，适应不同阶段的负载需求
- 优化硬件配置：Prefill 节点使用更大显存的 GPU，Decode 节点使用更高频率的 GPU
- 实现 KV cache 的跨节点共享，减少重复计算

### 伪代码与流程

```python
# 传统耦合推理
def traditional_inference(model, prompt):
    # 阶段 1: Prefill
    kv_cache = {}
    for layer in model.layers:
        kv_cache[layer] = layer.compute_kv(prompt)
    
    # 阶段 2: Decode
    output_tokens = []
    for _ in range(max_tokens):
        next_token = model.generate_next_token(kv_cache, output_tokens)
        output_tokens.append(next_token)
        # 更新 KV cache
        for layer in model.layers:
            layer.update_kv(kv_cache, next_token)
    
    return output_tokens

# PD 分离推理
def pd_disaggregated_inference(prefill_nodes, decode_nodes, prompt):
    # 阶段 1: Prefill（在 prefill 节点）
    prefill_node = select_prefill_node(prefill_nodes)
    kv_cache = prefill_node.compute_prefill(prompt)
    
    # 关键：通过 Mooncake Transfer KV cache 到 decode 节点
    decode_node = select_decode_node(decode_nodes)
    transfer_kv_cache(kv_cache, prefill_node, decode_node)
    
    # 阶段 2: Decode（在 decode 节点）
    output_tokens = []
    for _ in range(max_tokens):
        next_token = decode_node.generate_next_token(kv_cache, output_tokens)
        output_tokens.append(next_token)
        # Decode 节点本地更新 KV cache
    
    return output_tokens

# Mooncake Transfer 实现
def transfer_kv_cache(kv_cache, src_node, dst_node):
    # 1. 注册 KV cache 内存到 RDMA
    src_ptrs = register_memory_for_rdma(kv_cache)
    
    # 2. 建立与目标节点的连接
    dst_session = connect_to(dst_node.hostname, dst_node.port)
    
    # 3. 批量传输 KV cache
    for layer_name, layer_kv in kv_cache.items():
        dst_ptr = dst_node.allocate_layer_buffer(layer_name)
        length = get_layer_size(layer_kv)
        rdma_write(dst_session, src_ptrs[layer_name], dst_ptr, length)
    
    # 4. 通知目标节点传输完成
    notify_transfer_complete(dst_node)
```

### 原理分析

#### PD 分离的性能模型

设总请求处理时间为 \(T_{total}\)，Prefill 时间为 \(T_{prefill}\)，Decode 时间为 \(T_{decode}\)，传输时间为 \(T_{transfer}\)：

\[ T_{total} = T_{prefill} + T_{transfer} + T_{decode} \]

在传统耦合部署中，\(T_{prefill}\) 和 \(T_{decode}\) 串行执行。在 PD 分离部署中：

- **Prefill 阶段可以流水线化**：多个请求的 Prefill 可以在不同节点并行执行
- **Decode 阶段可以独立扩展**：根据 decode 负载动态调整节点数量
- **传输时间可以通过 RDMA 优化**：使用多网卡聚合和零拷贝技术降低 \(T_{transfer}\)

Mooncake 在实际测试中达到 **142.25 GB/s** 的峰值传输带宽（8x RoCE 网卡的 71.1% 利用率），对于 32K tokens 的 prompt（4.50 GB KV 数据），传输时间仅为 **31.65 ms**，占总 TTFT 的 **4.2%**。

#### 请求路由与调度

PD 分离需要一个代理服务器（Router）来协调请求路由：

```
请求 → Router → Prefill 节点（处理 prompt）
                  ↓ (KV cache 通过 Mooncake 传输)
              → Decode 节点（生成 tokens）
                  ↓
              → Router → 返回给用户
```

Router 的职责：

1. **请求分发**：将新请求发送到负载最轻的 prefill 节点
2. **状态跟踪**：记录每个请求当前所处的阶段（prefilling / decoding）
3. **故障处理**：节点故障时重试或迁移请求
4. **负载均衡**：根据实时负载情况动态调整 prefill/decode 节点数量

### 代码实践

#### vLLM 中的 PD 分离配置

[https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/getting_started/examples/vllm-integration/disagg-prefill-decode.md#L51-L75](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/getting_started/examples/vllm-integration/disagg-prefill-decode.md#L51-L75)

这段代码展示了如何启动 prefill 和 decode 节点：

```bash
# Prefiller Node (192.168.0.2)
vllm serve Qwen/Qwen2.5-7B-Instruct \
  --port 8010 \
  --kv-transfer-config '{"kv_connector":"MooncakeConnector","kv_role":"kv_producer"}'

# Decoder Node (192.168.0.3)
vllm serve Qwen/Qwen2.5-7B-Instruct \
  --port 8020 \
  --kv-transfer-config '{"kv_connector":"MooncakeConnector","kv_role":"kv_consumer"}'

# Proxy Server
python tests/v1/kv_connector/nixl_integration/toy_proxy_server.py \
  --prefiller-host 192.168.0.2 --prefiller-port 8010 \
  --decoder-host 192.168.0.3 --decoder-port 8020
```

#### KV Transfer 参数传递

[https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-wheel/mooncake/mooncake_connector_v1.py#L345-L393](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-wheel/mooncake/mooncake_connector_v1.py#L345-L393)

这个方法展示了如何构建 KV transfer 元数据：

```python
def request_finished(
        self,
        request: "Request",
        block_ids: list[int],
    ) -> tuple[bool, Optional[dict[str, Any]]]:
    """
    请求完成后，确定请求块是应该现在释放还是将异步发送并稍后释放。
    """
    params = request.kv_transfer_params
    
    if params.get("do_remote_decode"):
        # 需要将 KV cache 发送到 decode 节点
        delay_free_blocks = len(block_ids) > 0
        
        if delay_free_blocks:
            self._reqs_need_send[request.request_id] = block_ids
        
        return delay_free_blocks, dict(
            do_remote_prefill=True,
            do_remote_decode=False,
            remote_host=self.side_channel_host,
            remote_port=self.side_channel_port,
            remote_request_id=request.request_id)
    
    return False, None
```

### 练习题

1. **设计题**：在一个有 2 个 prefill 节点和 4 个 decode 节点的集群中，如何设计请求路由策略以避免 prefill 节点成为瓶颈？

2. **性能分析题**：假设 prefill 时间 \(T_p = 100 ms\)，decode 时间 \(T_d = 500 ms\)（250 tokens @ 2 ms/token），传输时间 \(T_t = 30 ms\)。计算 PD 分离相比传统部署的理论加速比（假设 prefill 和 decode 可以完全重叠）。

3. **故障处理题**：如果 prefill 节点在计算完成后、传输 KV cache 之前崩溃，decode 节点应该如何检测并处理这种情况？

4. **优化题**：在多租户场景中，不同用户的请求有不同优先级，如何在 PD 分离架构中实现优先级调度？

### 答案

1. **设计题答案**：
   - **自适应路由**：Monitor prefill 节点的队列长度和完成率，动态调整请求分发权重
   - **Prompt 长度感知**：根据请求的 prompt 长度预估 prefill 时间，长 prompt 请求分散到不同节点
   - **KV cache 缓存**：对于相同或相似 prompt，复用已计算过的 KV cache，跳过 prefill 阶段
   - **混合模式**：在低负载时允许部分节点同时处理 prefill 和 decode（`kv_both` 模式）

2. **性能分析题答案**：
   
   传统部署：\(T_{traditional} = T_p + T_d = 100 + 500 = 600 ms\)
   
   PD 分离（完全重叠）：\(T_{pd} = \max(T_p + T_t, T_d) = \max(100 + 30, 500) = 500 ms\)
   
   加速比：\(S = \frac{T_{traditional}}{T_{pd}} = \frac{600}{500} = 1.2x\)
   
   注意：实际加速比取决于 prefill/decode 的负载比例。如果 decode 负载更重（多个请求同时 decode），加速比会更明显。

3. **故障处理题答案**：
   - **心跳检测**：Decode 节点定期向 prefill 节点发送心跳，检测节点存活状态
   - **超时机制**：设置 `VLLM_MOONCAKE_ABORT_REQUEST_TIMEOUT`，超时后释放资源并返回错误
   - **请求重试**：Router 检测到 prefill 节点故障后，将请求重新路由到健康的 prefill 节点
   - **Checkpoint 恢复**：对于长 prompt 请求，定期保存 prefill 中间状态，故障后从 checkpoint 恢复

4. **优化题答案**：
   - **请求队列隔离**：为不同优先级的请求维护独立的队列，高优先级队列优先调度
   - **资源预留**：为高优先级请求预留部分 prefill/decode 节点，确保其资源可用
   - **抢占式调度**：当高优先级请求到达时，可以抢占低优先级请求的资源（如果业务允许）
   - **传输优先级**：在 RDMA 层面，为高优先级请求的 KV cache 传输分配更高带宽或更低的延迟

## 最小模块 3：KVCache 跨实例共享

### 概念说明

KVCache 跨实例共享是指多个 vLLM 实例（可能分布在不同的机器上）共享同一份 KV cache 数据，避免重复计算相同的 prompt。这是通过将 KV cache 存储在分布式存储系统（MooncakeStore）中实现的。

核心价值：

1. **减少重复计算**：相同或相似的 prompt 只需计算一次，其他实例直接从缓存读取
2. **提高缓存命中率**：分布式存储池容量远大于单机 GPU 显存，可以缓存更多历史请求
3. **支持弹性扩缩容**：新加入的实例可以直接访问已有缓存，无需预热
4. **降低内存压力**：将不常用的 KV cache 卸载到 CPU 内存或 SSD，释放 GPU 显存

### 伪代码与流程

```python
# 传统单实例缓存
def traditional_inference_with_cache(model, prompt, local_cache):
    cache_key = hash(prompt)
    if cache_key in local_cache:
        kv_cache = local_cache[cache_key]
    else:
        kv_cache = compute_prefill(model, prompt)
        local_cache[cache_key] = kv_cache
    return decode(model, kv_cache)

# 分布式 KV cache 共享
def distributed_inference_with_cache(model, prompt, mooncake_store):
    cache_key = hash_blocks(prompt)  # 基于 block 级别计算 hash
    
    # 1. 检查 MooncakeStore 是否有缓存
    cached_blocks = mooncake_store.get(cache_key)
    
    if cached_blocks:
        # 2. 缓存命中：直接加载
        load_blocks_to_gpu(cached_blocks)
        return decode_with_cached_blocks(model, cached_blocks)
    else:
        # 3. 缓存未命中：计算并存储
        kv_blocks = compute_prefill_to_blocks(model, prompt)
        
        # 4. 将新计算的 blocks 写入 MooncakeStore
        mooncake_store.put(cache_key, kv_blocks)
        
        return decode_with_blocks(model, kv_blocks)

# Block 级别的去重
def hash_blocks(prompt_tokens):
    """
    将 prompt 分块，每个块独立计算 hash。
    这样即使 prompt 只有部分相同，也能共享部分 KV cache。
    """
    blocks = []
    for i in range(0, len(prompt_tokens), BLOCK_SIZE):
        block_tokens = prompt_tokens[i:i+BLOCK_SIZE]
        block_hash = hash(tuple(block_tokens))
        blocks.append(block_hash)
    return blocks

# MooncakeStore 的分布式存储
class MooncakeStore:
    def __init__(self, master_servers, local_memory_size):
        self.master = connect_to_master(master_servers)
        self.local_buffer = allocate_memory(local_memory_size)
        self.rdma_engine = TransferEngine()
    
    def get(self, block_hashes):
        # 1. 查询元数据：哪些 blocks 在分布式存储中
        metadata = self.master.query_metadata(block_hashes)
        
        # 2. 并行拉取可用的 blocks
        available_blocks = [b for b in metadata if b.exists]
        self.rdma_engine.batch_read(available_blocks, self.local_buffer)
        
        # 3. 返回本地缓冲区指针
        return self.local_buffer
    
    def put(self, block_hashes, blocks_data):
        # 1. 写入本地缓冲区
        self.local_buffer.write(blocks_data)
        
        # 2. 通过 RDMA 零拷贝传输到远程存储节点
        target_nodes = self.master.allocate_storage_nodes(len(blocks_data))
        self.rdma_engine.batch_write(self.local_buffer, target_nodes)
        
        # 3. 更新元数据
        self.master.update_metadata(block_hashes, target_nodes)
```

### 原理分析

#### 基于 Hash 的前缀缓存

MooncakeStore 使用 block-level hashing 来实现细粒度的 KV cache 共享：

1. **Block 分块**：将 KV cache 按固定大小（如 16 tokens）分块
2. **Hash 计算**：每个 block 根据其对应的 prompt tokens 计算 hash 值
3. **去重存储**：相同 hash 的 block 只存储一份，不同请求共享

这种方法的优势：

- **部分匹配**：即使 prompt 只有部分相同，也能共享这部分 KV cache
- **空间效率**：相同的 block 不会重复存储，节省分布式存储空间
- **快速查找**：通过 hash 直接定位 block，无需遍历

#### 分布式存储的一致性

MooncakeStore 使用中心化的 Master 服务来维护全局元数据：

- **Block 位置映射**：记录每个 block hash 存储在哪个存储节点
- **分配策略**：新 block 写入时，Master 负责分配存储节点（考虑负载均衡和副本因子）
- **驱逐策略**：当存储空间不足时，Master 驱动驱逐算法（如 LRU）释放空间

对于多租户和弹性部署：

- **租户隔离**：不同租户的 KV cache 可以存储在独立的命名空间中，避免互相干扰
- **动态扩缩容**：新节点加入时向 Master 注册，自动参与存储和查询；节点离开时，Master 负责迁移数据

### 代码实践

#### MooncakeStoreConnector 的初始化

[https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/getting_started/examples/vllm-integration/kv-cache-storage.md#L75-L108](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/getting_started/examples/vllm-integration/kv-cache-storage.md#L75-L108)

这段代码展示了如何在 vLLM 中启用 MooncakeStoreConnector：

```bash
# 单节点 KV cache 卸载
MOONCAKE_CONFIG_PATH=mooncake_config.json \
vllm serve meta-llama/Llama-3.1-8B-Instruct \
    --kv-transfer-config '{"kv_connector":"MooncakeStoreConnector","kv_role":"kv_both"}'

# XpYd PD 分离 + KV cache 共享
# Prefill Node
MOONCAKE_CONFIG_PATH=mooncake_config.json \
VLLM_MOONCAKE_BOOTSTRAP_PORT=50052 \
vllm serve meta-llama/Llama-3.1-8B-Instruct \
    --port 8100 \
    --kv-transfer-config '{
        "kv_connector": "MultiConnector",
        "kv_role": "kv_producer",
        "kv_connector_extra_config": {
            "connectors": [
                {"kv_connector": "MooncakeConnector", "kv_role": "kv_producer"},
                {"kv_connector": "MooncakeStoreConnector", "kv_role": "kv_producer"}
            ]
        }
    }'

# Decode Node
MOONCAKE_CONFIG_PATH=mooncake_config.json \
VLLM_MOONCAKE_BOOTSTRAP_PORT=50053 \
vllm serve meta-llama/Llama-3.1-8B-Instruct \
    --port 8200 \
    --kv-transfer-config '{
        "kv_connector": "MultiConnector",
        "kv_role": "kv_consumer",
        "kv_connector_extra_config": {
            "connectors": [
                {"kv_connector": "MooncakeConnector", "kv_role": "kv_consumer"},
                {"kv_connector": "MooncakeStoreConnector", "kv_role": "kv_consumer"}
            ]
        }
    }'
```

#### 配置文件示例

[https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/getting_started/examples/vllm-integration/kv-cache-storage.md#L55-L65](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/getting_started/examples/vllm-integration/kv-cache-storage.md#L55-L65)

Mooncake 配置文件定义了存储集群的连接信息：

```json
{
  "metadata_server": "http://127.0.0.1:8092/metadata",
  "master_server_address": "127.0.0.1:50063",
  "global_segment_size": "0",
  "local_buffer_size": "2147483648",
  "protocol": "rdma",
  "device_name": ""
}
```

关键参数：
- `global_segment_size`：每个实例贡献到分布式内存池的大小（0 表示不贡献，仅作为消费者）
- `local_buffer_size`：本地缓冲区大小，用于临时存储待传输的 KV cache
- `protocol`：传输协议（`rdma` 或 `tcp`）

### 练习题

1. **设计题**：在一个有多租户的部署中，如何设计 KV cache 的隔离策略，避免高优先级租户的缓存被低优先级租户的驱逐策略影响？

2. **性能分析题**：假设 MooncakeStore 的命中率为 40%，未命中时 prefill 时间为 100 ms，命中时加载时间为 10 ms。计算使用 KV cache 共享后的平均 prefill 时间。

3. **一致性问题**：在分布式存储中，如果两个实例同时计算相同的 prompt 并写入 KV cache，如何避免数据冲突和不一致？

4. **优化题**：对于工作负载有明显时间特征的场景（如白天查询多、晚上索引多），如何设计 KV cache 的预热和淘汰策略？

### 答案

1. **设计题答案**：
   - **命名空间隔离**：为不同租户或优先级分配独立的命名空间，缓存和驱逐策略各自独立
   - **QoS 保障**：为高优先级租户预留存储空间和带宽，确保其性能不受低优先级影响
   - **分层驱逐**：优先驱逐低优先级租户的冷数据，高优先级租户的数据即使较冷也保留
   - **配额管理**：为每个租户设置最大缓存配额，防止单个租户占用过多资源

2. **性能分析题答案**：
   
   平均 prefill 时间 = 命中率 × 命中时间 + (1 - 命中率) × 未命中时间
   
   \(T_{avg} = 0.4 \times 10 + 0.6 \times 100 = 4 + 60 = 64 ms\)
   
   加速比 = 未命中时间 / 平均时间 = \(100 / 64 = 1.56x\)

3. **一致性问题答案**：
   - **原子性检查**：写入前先检查 block 是否已存在（通过元数据查询），如果已存在则放弃写入
   - **乐观并发控制**：使用版本号或时间戳，当检测到冲突时重试或合并
   - **Master 仲裁**：所有写入操作通过 Master 协调，Master 负责去重和冲突解决
   - **幂等写入**：设计写入操作为幂等的，多次写入相同数据结果一致

4. **优化题答案**：
   - **预测性缓存**：基于历史数据预测即将到来的请求类型，提前加载相关 KV cache
   - **时间窗口驱逐**：在低流量时段（如夜间）进行大规模缓存清理和预热，为高流量时段做准备
   - **分层存储**：热数据存放在 GPU 显存，温数据存放在 CPU 内存，冷数据存放在 SSD，根据时间模式自动调整层级
   - **潮汐调度**：利用集群的潮汐特性（部分节点闲时），在闲时进行 prefill 计算并缓存结果

## 最小模块 4：HiCache 分层缓存集成

### 概念说明

HiCache 是 SGLang 引入的分层缓存系统，将传统仅限于 GPU 显存的 RadixAttention 扩展为三级缓存架构：

- **L1 Cache（GPU 显存）**：最快但容量最小，存放最热的数据
- **L2 Cache（CPU 内存）**：中等速度和容量，作为 L1 的扩展
- **L3 Cache（分布式存储）**：容量最大但访问延迟最高，由 Mooncake 提供

这种分层设计灵感来自现代 CPU 的多级缓存系统，旨在在容量和延迟之间取得最佳平衡。

### 伪代码与流程

```python
# HiCache 的三级缓存查询
def hicache_lookup(request_tokens):
    # 1. L1 查找（GPU 显存）
    l1_hit_tokens, l1_blocks = search_l1_cache(request_tokens)
    
    if len(l1_hit_tokens) == len(request_tokens):
        # 完全命中 L1
        return l1_blocks, "L1_FULL_HIT"
    
    remaining_tokens = request_tokens[len(l1_hit_tokens):]
    
    # 2. L2 查找（CPU 内存）
    l2_hit_tokens, l2_blocks = search_l2_cache(remaining_tokens)
    
    if len(l2_hit_tokens) == len(remaining_tokens):
        # 完全命中 L1 + L2
        combined_blocks = combine_blocks(l1_blocks, l2_blocks)
        return combined_blocks, "L1_L2_FULL_HIT"
    
    still_missing = remaining_tokens[len(l2_hit_tokens):]
    
    # 3. L3 查找（Mooncake 分布式存储）
    l3_hit_tokens, l3_blocks = search_l3_cache(still_missing)
    
    # 4. 决定预取策略
    if len(l3_hit_tokens) > PREFETCH_THRESHOLD:
        # 触发预取：异步将 L3 数据加载到 L2
        prefetch_to_l2(l3_blocks)
    
    # 5. 组合所有层级的缓存
    combined_blocks = combine_blocks(l1_blocks, l2_blocks, l3_blocks)
    
    return combined_blocks, "PARTIAL_HIT"

# 数据写回策略
def hicache_writeback(blocks, access_pattern):
    """
    根据访问频率决定写回策略
    """
    if access_pattern == "hot":
        # 热数据：立即写回下一层
        if current_level == "L1":
            write_to_l2(blocks)
        elif current_level == "L2":
            write_to_l3(blocks)
    
    elif access_pattern == "warm":
        # 温数据：写回 L2
        write_to_l2(blocks)
    
    elif current_level == "cold":
        # 冷数据：仅当被驱逐时才写回
        pass  # 等待驱逐时触发写回

# HiRadixTree：增强版的 RadixTree
class HiRadixNode:
    def __init__(self, tokens):
        self.tokens = tokens
        self.hash = compute_hash(tokens)
        
        # 记录数据在各个层级的存储位置
        self.storage_locations = {
            "L1": None,  # GPU 显存地址
            "L2": None,  # CPU 内存地址
            "L3": None   # Mooncake 存储节点地址
        }
    
    def locate_data(self, target_level):
        """定位数据在指定层级的存储位置"""
        if self.storage_locations[target_level]:
            return self.storage_locations[target_level]
        
        # 如果目标层级没有数据，从更高级层级拉取
        if target_level == "L2" and self.storage_locations["L3"]:
            return fetch_from_l3_to_l2(self.storage_locations["L3"])
        
        return None
```

### 原理分析

#### HiRadixTree：元数据组织

HiRadixTree 扩展了 RadixTree 的设计，每个节点不仅记录 KV cache 的内容，还记录其在各个缓存层级的存储位置：

- **本地精确元数据**：对于 L1 和 L2，节点存储确切的内存地址，实现快速访问
- **L3 延迟查询**：对于 L3，节点不直接存储位置信息，而是运行时查询 Mooncake 的元数据服务

这种设计的好处：

- **减少元数据开销**：L3 数据的元数据由 Mooncake 管理，HiRadixTree 只需存储引用
- **支持动态扩缩容**：L3 存储节点的变化不需要更新所有 HiRadixTree 节点
- **容错性**：L3 元数据集中管理，便于实现副本和故障恢复

#### 预取策略与终止条件

HiCache 提供三种预取终止策略：

1. **best_effort**：立即终止，不等待预取完成，适合延迟敏感场景
2. **wait_complete**：必须等待所有预取完成，适合高命中率要求场景
3. **timeout**：等待指定时间或完成，平衡延迟和命中率

超时计算公式：

\[ \text{timeout} = \text{prefetch\_timeout\_base} + \text{prefetch\_timeout\_per\_ki\_token} \times \frac{\text{num\_token\_to\_fetch}}{1024} \]

这种动态超时机制根据实际传输数据量调整等待时间，避免短请求等待过长或长请求等待不足。

#### 数据写回优化

HiCache 支持三种写回策略：

1. **write_through**：每次访问立即写回下一层，带宽充足时提供最强缓存效果
2. **write_through_selective**：访问频率超过阈值后才写回，只备份热数据，减少 I/O 开销
3. **write_back**：数据被驱逐时才写回下一层，适合存储容量受限但需最大化内存利用率的场景

写回过程使用异步并行：

- **L1 → L2**：通过 `write_backup` 函数异步传输，不阻塞主流程
- **L2 → L3**：通过 `backup_queue` 和专用 `backup_thread_func` 线程处理

### 代码实践

#### HiCache 的配置示例

[https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/getting_started/examples/sglang-integration/hicache-integration-v1.md#L177-L183](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/getting_started/examples/sglang-integration/hicache-integration-v1.md#L177-L183)

这段代码展示了如何在 SGLang 中启用 HiCache 和 Mooncake 后端：

```bash
python -m sglang.launch_server \
    --enable-hierarchical-cache \
    --hicache-storage-backend mooncake \
    --model-path [model_path] \
    --hicache-storage-backend-extra-config '{
        "master_server_address": "127.0.0.1:50051", 
        "local_hostname": "localhost", 
        "metadata_server": "http://127.0.0.1:8080/metadata", 
        "global_segment_size": "4gb", 
        "protocol": "rdma", 
        "device_name": ""
    }'
```

#### 预取配置参数

HiCache 提供多个参数来调优预取行为：

- `--hicache-storage-prefetch-policy`：预取策略（`best_effort` / `wait_complete` / `timeout`）
- `--hicache-storage-prefetch-timeout-base`：基础超时时间（毫秒）
- `--hicache-storage-prefetch-timeout-per-ki-token`：每千 tokens 的额外超时时间（毫秒）

例如：

```bash
python -m sglang.launch_server \
    --enable-hierarchical-cache \
    --hicache-storage-backend mooncake \
    --hicache-storage-prefetch-policy timeout \
    --hicache-storage-prefetch-timeout-base 50 \
    --hicache-storage-prefetch-timeout-per-ki-token 10 \
    ...
```

### 练习题

1. **设计题**：在一个有多租户的 SGLang 部署中，如何设计 HiCache 的 L2 大小分配策略，确保高优先级租户的性能不受低优先级租户影响？

2. **调优题**：假设你的工作负载中 80% 的请求是短 prompt（<1K tokens），20% 是长 prompt（>10K tokens）。如何配置 HiCache 的预取参数以优化整体性能？

3. **故障恢复题**：如果 L3 存储节点（Mooncake）发生故障，HiCache 应该如何降级服务，确保基本功能可用？

4. **监控题**：设计一个监控方案来跟踪 HiCache 的命中率（L1、L2、L3 分别的命中率），并基于这些指标动态调整缓存大小。

### 答案

1. **设计题答案**：
   - **L2 配额管理**：为不同租户分配独立的 L2 内存配额，确保高优先级租户有足够空间
   - **动态调整**：根据实时负载动态调整配额，高优先级租户流量增加时临时扩大其配额
   - **驱逐隔离**：每个租户的 L2 数据独立管理，驱逐策略不跨租户
   - **QoS 保障**：为高优先级租户设置 L2 命中率的 SLA，当低于阈值时触发扩容或调整

2. **调优题答案**：
   - **短 prompt 优化**：对于 80% 的短请求，使用 `best_effort` 预取策略，避免等待过长时间
   - **长 prompt 优化**：对于 20% 的长请求，使用 `timeout` 策略，设置较长的超时时间：
     - `prefetch_timeout_base = 100 ms`（基础调度开销）
     - `prefetch_timeout_per_ki_token = 20 ms`（每 1K tokens 额外 20 ms）
   - **分层超时**：根据请求的实际 token 数量动态计算超时，避免一刀切
   - **监控与反馈**：实时监控不同长度请求的命中率，动态调整参数

3. **故障恢复题答案**：
   - **降级模式**：当 L3 不可用时，HiCache 降级为二级缓存（L1 + L2），仅使用本地存储
   - **优雅降级**：已从 L3 预取到 L2 的数据继续可用，新请求直接跳过 L3 查询
   - **快速失败**：L3 查询超时时间设置为较短值（如 50 ms），避免长时间阻塞
   - **故障告警**：监控 L3 可用性，故障时触发告警并自动切换到降级模式
   - **恢复后预热**：L3 恢复后，逐步将 L2 热数据写回 L3，避免瞬时流量冲击

4. **监控题答案**：
   - **指标采集**：在每个层级记录命中/未命次数，计算命中率：
     - L1 命中率 = L1 命中次数 / 总查询次数
     - L2 命中率 = (L1 + L2 命中次数) / 总查询次数
     - L3 命中率 = (L1 + L2 + L3 命中次数) / 总查询次数
   - **动态调整**：基于命中率趋势调整缓存大小：
     - L1 命中率 < 目标：增加 L1 大小（调整 GPU 显存分配）
     - L2 命中率 < 目标：增加 L2 大小（调整 CPU 内存分配）
     - L3 命中率 < 目标：检查预取策略和网络配置
   - **告警机制**：设置命中率阈值告警，异常时自动触发扩容或配置调整

## 生产实践：Kimi K2 部署案例

### 概念说明

Kimi K2 是 Moonshot AI 的大规模生产部署案例，展示了 Mooncake 在实际场景中的强大能力。该部署在 **128 个 H200 GPU** 上实现了：

- **224k tokens/sec** 的 prefill 吞吐量
- **288k tokens/sec** 的 decode 吞吐量
- 支持 **PD 分离** 和 **大规模专家并行（EP）**

这个案例证明了 Mooncake 在超大规模集群中的可扩展性和性能优势。

### 伪代码与流程

```python
# Kimi K2 的部署架构
def kimi_k2_deployment():
    # 1. 硬件配置
    num_gpus = 128  # H200 GPUs
    num_prefill_nodes = 32
    num_decode_nodes = 96
    
    # 2. 启动 Mooncake 组件
    mooncake_master = start_master_service()
    mooncake_metadata = start_metadata_service()
    
    # 3. 启动 prefill 节点（带 Mooncake Connector 和 Store）
    for i in range(num_prefill_nodes):
        start_vllm_instance(
            role="kv_producer",
            enable_multi_connector=True,  # 同时使用 MooncakeConnector 和 MooncakeStoreConnector
            model="kimi-k2",
            tensor_parallel_size=4,
            mooncake_config=mooncake_config
        )
    
    # 4. 启动 decode 节点
    for i in range(num_decode_nodes):
        start_vllm_instance(
            role="kv_consumer",
            enable_multi_connector=True,
            model="kimi-k2",
            tensor_parallel_size=4,
            mooncake_config=mooncake_config
        )
    
    # 5. 启动代理服务器集群
    router_cluster = start_router_cluster(
        num_instances=8,
        scheduling_policy="adaptive_load_balance",
        prefill_nodes=prefill_nodes,
        decode_nodes=decode_nodes
    )
    
    # 6. 监控和自动扩缩容
    monitor_and_scale(
        target_prefill_tps=224000,
        target_decode_tps=288000,
        auto_scale_policy="qps_based"
    )

# 专家并行（EP）集成
def expert_parallel_with_mooncake(model, request):
    """
    在专家并行模型中，不同专家的 KV cache 分布在不同 GPU 上。
    Mooncake 负责跨节点的专家 KV cache 传输。
    """
    # 1. 确定 request 需要哪些专家
    active_experts = route_to_experts(request)
    
    # 2. 从 prefill 节点拉取各专家的 KV cache
    for expert_id in active_experts:
        expert_prefill_node = locate_expert_node(expert_id)
        expert_kv = mooncake_transfer(
            src=expert_prefill_node,
            dst=local_decode_node,
            expert_id=expert_id
        )
    
    # 3. 本地聚合所有专家的 KV cache
    aggregated_kv = aggregate_expert_kv(active_experts)
    
    # 4. 执行 decode
    return decode_with_experts(model, aggregated_kv, active_experts)
```

### 原理分析

#### 大规模集群的调度挑战

在 128 GPU 的集群中，调度器面临以下挑战：

1. **节点异构性**：不同节点的性能可能有差异（网络延迟、GPU 频率等）
2. **动态负载**：请求的 prefill/decode 比例实时变化，需要动态调整节点分配
3. **故障容错**：节点故障时需要快速迁移请求，避免服务中断
4. **资源碎片化**：KV cache 的碎片化可能导致内存利用率下降

Mooncake 的解决方案：

- **拓扑感知路由**：根据网络拓扑（NUMA 亲和性、RDMA 路径）选择最优传输路径
- **自适应负载均衡**：实时监控节点负载，动态调整请求分发权重
- **副本机制**：关键 KV cache 可以存储多个副本，提高可用性和读取吞吐
- **智能驱逐**：基于访问模式和历史数据，预测性地驱逐不活跃的 KV cache

#### 专家并行的 KV Cache 管理

在专家并行（EP）模型中，每个专家的 KV cache 分布在不同的 GPU 或节点上。Mooncake 负责：

- **跨节点专家传输**：当 request 需要多个专家时，从对应的 prefill 节点拉取 KV cache
- **零拷贝聚合**：通过 RDMA 直接将多个专家的 KV cache 聚合到 decode 节点，无需 CPU 中转
- **专家级缓存**：每个专家的 KV cache 可以独立缓存和复用，提高专家模型的效率

### 代码实践

#### 生产部署的配置要点

根据 Kimi K2 的部署经验，生产环境的关键配置：

```bash
# Mooncake Master 配置
mooncake_master \
  --port 50063 \
  --eviction_high_watermark_ratio=0.95 \
  --enable_http_metadata_server=true \
  --http_metadata_server_port=8080

# Prefill 节点配置
export MOONCAKE_CONFIG_PATH=/path/to/mooncake_config.json
export VLLM_MOONCAKE_BOOTSTRAP_PORT=50052  # 每个 TP rank 不同

vllm serve kimi-k2 \
  --port 8100 \
  --tensor-parallel-size 4 \
  --kv-transfer-config '{
    "kv_connector": "MultiConnector",
    "kv_role": "kv_producer",
    "kv_connector_extra_config": {
      "connectors": [
        {"kv_connector": "MooncakeConnector", "kv_role": "kv_producer"},
        {"kv_connector": "MooncakeStoreConnector", "kv_role": "kv_producer"}
      ]
    }
  }' \
  --max-model-len 32000 \
  --gpu-memory-utilization 0.9

# 关键环境变量
export VLLM_MOONCAKE_SENDER_WORKERS=20  # 增加发送线程数
export VLLM_MOONCAKE_ABORT_REQUEST_TIMEOUT=600  # 10 分钟超时
export PYTHONHASHSEED=0  # 确保数据并行下 block hash 一致性
```

### 练习题

1. **架构设计题**：在 Kimi K2 的部署中，如果有 10% 的 prefill 节点故障，如何重新分配请求以最小化性能影响？

2. **容量规划题**：假设要部署一个支持 10000 QPS、平均 prompt 长度 5K tokens、平均输出 1K tokens 的服务，需要多少个 prefill 和 decode 节点（参考 Kimi K2 的性能数据）？

3. **成本优化题**：在预算有限的情况下，如何平衡 GPU 的数量和配置（显存、频率）以达到最佳的性能成本比？

4. **监控题**：设计一个监控 dashboard 来实时跟踪 Kimi K2 部署的健康状态，包括哪些关键指标？

### 答案

1. **架构设计题答案**：
   - **健康检查与隔离**：定期检查节点健康状态，故障节点自动从路由表中移除
   - **请求重分配**：将原本路由到故障节点的请求重新分发到健康的 prefill 节点
   - **弹性扩容**：触发自动扩容机制，启动新的 prefill 节点补充容量
   - **降级服务**：如果扩容不及时，可以临时降低服务质量（如限制并发数、增加排队时间）
   - **KV cache 恢复**：故障节点的 KV cache 如果未写入 MooncakeStore，需要重新计算

2. **容量规划题答案**：
   
   参考 Kimi K2 的性能数据：
   - 32 prefill 节点 → 224k tokens/sec
   - 96 decode 节点 → 288k tokens/sec
   
   计算：
   - **Prefill 需求**：10000 QPS × 5K tokens = 50M tokens/sec
   - **Decode 需求**：10000 QPS × 1K tokens = 10M tokens/sec
   
   所需节点（线性扩展）：
   - **Prefill 节点**：50M / (224k / 32) ≈ 7143 / 7 = 1021 个 prefill 节点
   - **Decode 节点**：10M / (288k / 96) ≈ 10M / 3k = 3334 个 decode 节点
   
   注意：实际部署中需要考虑请求模式的不均匀性和安全余量，通常增加 20-30% 的冗余。

3. **成本优化题答案**：
   - **Prefill 节点**：选择大显存 GPU（如 A100 80GB），因为 prefill 阶段需要存储大量 KV cache
   - **Decode 节点**：选择高频率 GPU（如 H200），因为 decode 阶段对延迟敏感
   - **混合部署**：在低负载时，部分节点可以同时处理 prefill 和 decode（`kv_both` 模式），提高资源利用率
   - **spot 实例**：对于 decode 节点，可以使用 spot 实例降低成本（配合快速故障恢复机制）
   - **分层存储**：将冷数据的 KV cache 存储在更便宜的 SSD 甚至对象存储，释放昂贵的 GPU 显存

4. **监控题答案**：
   
   关键监控指标：
   
   **系统级别**：
   - **QPS**：每秒请求数，区分 prefill 和 decode QPS
   - **延迟**：TTFT（Time To First Token）、TBT（Time Between Tokens）
   - **吞吐**：tokens/sec（prefill 和 decode 分别统计）
   
   **资源级别**：
   - **GPU 利用率**：计算、显存、带宽利用率
   - **网络利用率**：RDMA 带宽、网络延迟
   - **CPU/内存利用率**：L2 缓存的使用情况
   
   **Mooncake 级别**：
   - **KV Cache 命中率**：L1、L2、L3 各层的命中率
   - **传输性能**：KV cache 传输带宽、传输延迟
   - **存储使用率**：MooncakeStore 的内存使用和碎片化情况
   
   **业务级别**：
   - **错误率**：请求失败率、超时率
   - **队列长度**：prefill 和 decode 节点的请求队列长度
   - **用户满意度**：基于 SLO 的合规率

## 总结

本讲义深入讲解了 Mooncake 与 vLLM 的深度集成，覆盖了四个核心模块：

1. **vLLM Connector**：MooncakeConnector 和 MooncakeStoreConnector 的设计与实现，支持 PD 分离和 KV cache 共享
2. **Prefill-Decode 分离**：通过 RDMA 实现高效的跨节点 KV cache 传输，达到 142.25 GB/s 的峰值带宽
3. **KVCache 跨实例共享**：基于 MooncakeStore 的分布式 KV cache 存储，实现 hash-based 去重和前缀缓存
4. **HiCache 分层缓存**：三级缓存架构（L1 GPU、L2 CPU、L3 分布式存储），通过智能预取和写回策略优化性能

这些技术共同构成了 Mooncake 在生产环境中的强大能力，如 Kimi K2 在 128 个 H200 GPU 上实现了 224k tokens/sec 的 prefill 吞吐量和 288k tokens/sec 的 decode 吞吐量。通过理解这些核心概念和实现细节，你可以在自己的部署中充分利用 Mooncake 的优势，构建高性能、可扩展的 LLM 推理系统。

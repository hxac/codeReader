# Mooncake 架构概览与应用场景

本讲义将系统讲解 Mooncake 的整体架构设计，帮助读者建立对项目全局视角的认知，理解其 KVCache 中心型解耦设计的核心思想以及在实际 LLM 推理场景中的应用价值。

---

## 最小模块 1：架构概览

### 1.1 概念说明

Mooncake 是一个以 KVCache（键值缓存）为中心的**解耦架构**，专门为大型语言模型（LLM）推理服务设计。在传统的 LLM 推理系统中，Prefill（预填充）和 Decode（解码）阶段通常在同一个 GPU 节点上执行，这导致了资源利用率不均和性能瓶颈。Mooncake 通过将这两个阶段分离到不同的集群，并利用未被充分利用的 CPU、DRAM 和 SSD 资源构建分布式 KVCache 池，实现了更高的系统吞吐和更好的资源利用率。

**核心设计思想：**
- **KVCache 中心型调度**：以 KVCache 的生命周期管理为中心，而非传统以任务为中心
- **解耦架构**：将计算密集型的 Prefill 阶段和内存密集型的 Decode 阶段分离
- **多级缓存池**：利用 DRAM/SSD 构建分层缓存，减少对慢速对象存储的访问

### 1.2 伪代码或流程

Mooncake 的整体架构可以抽象为以下流程：

```
# 用户请求到达
request = receive_user_request()

# 1. Prefill 阶段：计算密集型
prefill_cluster = select_prefill_node(request)
kv_cache = prefill_cluster.process_prefill(request)

# 2. KVCache 传输：通过 Transfer Engine
transfer_engine = TransferEngine()
decode_cluster = select_decode_node()
transfer_engine.transfer(
    source=prefill_cluster.vram, 
    destination=decode_cluster.dram,  # 或 VRAM
    data=kv_cache,
    protocol="RDMA"  # 零拷贝传输
)

# 3. Decode 阶段：内存密集型
response = decode_cluster.process_decode(kv_cache)

# 4. KVCache 管理：存储到 Mooncake Store
mooncake_store.put(
    key=request.hash,
    value=kv_cache,
    replication=2,  # 副本数
    tiers=["GPU", "DRAM", "SSD"]  # 多级缓存
)
```

### 1.3 原理分析

**数学模型：**

在传统耦合架构中，单个节点的处理时间可以表示为：

\[T_{\text{coupled}} = T_{\text{prefill}} + T_{\text{decode}}\]

而在 Mooncake 的解耦架构中，系统吞吐量受限于最慢的集群。假设有 \(N_p\) 个 Prefill 节点和 \(N_d\) 个 Decode 节点，系统的有效吞吐量为：

\[\text{Throughput} = \min\left(\frac{N_p}{T_{\text{prefill}}}, \frac{N_d}{T_{\text{decode}}}\right)\]

**设计优势：**

1. **资源独立扩展**：Prefill 和 Decode 集群可以独立扩展，根据实际负载动态调整节点数量
2. **缓存共享**：多个请求可以共享相同的 KVCache，特别是在多轮对话和 Agent 场景中
3. **预测性调度**：基于 SLO（Service Level Objectives）的请求调度，在高负载场景下提前拒绝不符合 SLO 的请求

**数据流分析：**

```
用户请求 → Prefill 集群（生成 KVCache）
    ↓ (Transfer Engine: RDMA 零拷贝传输)
Decode 集群（读取 KVCache 生成响应）
    ↓ (可选：缓存到 Mooncake Store)
Mooncake Store（多级存储：GPU → DRAM → SSD）
```

### 1.4 代码实践

Mooncake 的核心架构在 README 中有详细描述。让我们查看关键代码位置：

**架构定义和核心组件说明：**

[README.md](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/README.md#L75-L81)

这段代码描述了 Mooncake 的核心架构：KVCache 中心型的解耦设计，将 Prefill 和 Decode 集群分离，并利用 CPU、DRAM 和 SSD 资源构建分布式 KVCache 池。

**三大核心组件：**

1. **Transfer Engine (TE)**：[README.md#L90-L112](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/README.md#L90-L112)
   - 高性能数据传输框架
   - 支持 RDMA 零拷贝传输
   - 多 NIC 带宽聚合
   - 拓扑感知路径选择

2. **Mooncake Store**：[README.md#L114-L131](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/README.md#L114-L131)
   - 分布式 KVCache 存储引擎
   - 多级缓存层次（GPU → DRAM → SSD）
   - 对象级别的存储和复制
   - 动态资源管理

3. **Mooncake EP & PG**：[README.md#L133-L150](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/README.md#L133-L150)
   - 专家并行（Expert Parallelism）支持
   - 容错的分布式执行
   - PyTorch ProcessGroup 集成

### 1.5 练习题

**练习 1（基础）：** 在 Mooncake 架构中，为什么将 Prefill 和 Decode 分离可以提高整体系统吞吐？请从资源利用率和可扩展性两个角度分析。

**练习 2（进阶）：** 假设一个 LLM 推理系统有 4 个 Prefill 节点和 8 个 Decode 节点。如果 Prefill 阶段平均耗时 100ms，Decode 阶段平均耗时 200ms，计算系统的最大吞吐量（请求/秒）。如果增加 2 个 Prefill 节点，吞吐量如何变化？

**练习 3（应用）：** 在多轮对话场景中，用户的第一轮请求生成了 KVCache。在后续轮次中，如何利用 Mooncake Store 来减少重复计算？请描述完整的数据流。

**练习 4（分析）：** Mooncake 使用 RDMA 技术进行数据传输。相比传统的 TCP 传输，RDMA 在这个场景中有什么优势？为什么零拷贝对 KVCache 传输特别重要？

### 1.6 答案

**答案 1：**  
从资源利用率角度：Prefill 是计算密集型（主要消耗 GPU 计算能力），Decode 是内存密集型（主要消耗 GPU 显存带宽）。将它们分离后，可以为每个阶段配置专门的硬件资源，避免资源竞争。  
从可扩展性角度：可以根据实际负载独立扩展 Prefill 和 Decode 集群。例如，在长文本场景下 Prefill 压力大，可以增加 Prefill 节点；在高并发场景下 Decode 压力大，可以增加 Decode 节点。

**答案 2：**  
当前系统吞吐量受限于 Decode 集群：  
\(\text{Throughput} = \min\left(\frac{4}{0.1}, \frac{8}{0.2}\right) = \min(40, 40) = 40\) 请求/秒  
增加 2 个 Prefill 节点后（6 个 Prefill）：  
\(\text{Throughput} = \min\left(\frac{6}{0.1}, \frac{8}{0.2}\right) = \min(60, 40) = 40\) 请求/秒  
吞吐量不变，因为瓶颈在 Decode 集群。

**答案 3：**  
完整数据流：  
1. 第一轮：Prefill 节点处理用户请求 → 生成 KVCache → 存储到 Mooncake Store（以对话 ID 为 key）  
2. 后续轮次：  
   - 系统首先检查 Mooncake Store 中是否存在该对话的 KVCache  
   - 如果存在，直接从 Store 获取 KVCache（可能从 DRAM 或 SSD 层）  
   - 将 KVCache 传输到 Decode 节点  
   - Decode 节点基于现有 KVCache 继续生成，避免重复 Prefill 计算

**答案 4：**  
RDMA 优势：  
1. **零拷贝**：数据直接从源节点的内存传输到目标节点的内存，无需经过操作系统的缓冲区，大幅降低 CPU 开销和延迟  
2. **低延迟**：绕过内核网络协议栈，直接由网卡硬件处理传输  
3. **高带宽**：可以充分利用多网卡带宽，在 8×400 Gbps RoCE 网络中达到 190 GB/s  
对 KVCache 特别重要的原因：KVCache 通常很大（对于 128K token 的 LLaMA3-70B 模型约 40GB），零拷贝传输可以避免多次内存复制，显著减少传输时间和 CPU 开销。

---

## 最小模块 2：KVCache 分离

### 2.1 概念说明

KVCache 分离是 Mooncake 架构的核心创新。传统 LLM 推理中，KVCache 作为计算的中间结果，与计算节点紧密耦合。Mooncake 将 KVCache 视为**一等公民**，将其生命周期管理与计算节点解耦，实现跨节点、跨实例的共享与复用。

**解决的核心问题：**
- **计算冗余**：相同前缀的请求重复计算 KVCache
- **资源浪费**：KVCache 占用大量显存，限制了批次大小
- **扩展性受限**：缓存与计算耦合，难以独立扩展缓存容量

### 2.2 伪代码或流程

KVCache 分离的抽象流程：

```
# 传统耦合模式
def traditional_inference(request):
    h2o_cache = compute_prefill(request)  # 每次都重新计算
    response = compute_decode(h2o_cache)
    return response
    # h2o_cache 随请求结束而释放

# Mooncake 解耦模式
def mooncake_inference(request):
    # 1. 检查缓存
    cache_key = hash(request.prefix)
    h2o_cache = mooncake_store.get(cache_key)
    
    if h2o_cache is None:
        # 2. 缓存未命中，计算并存储
        h2o_cache = compute_prefill(request)
        mooncake_store.put(
            key=cache_key,
            value=h2o_cache,
            tiers=["GPU", "DRAM", "SSD"],
            replication=2
        )
    
    # 3. 跨节点传输
    target_node = select_decode_node()
    transfer_engine.transfer(
        source=h2o_cache.location,
        destination=target_node,
        data=h2o_cache,
        protocol="RDMA"
    )
    
    # 4. 解码
    response = target_node.compute_decode(h2o_cache)
    return response
    # h2o_cache 保留在 Store 中，可供后续请求复用
```

### 2.3 原理分析

**KVCache 结构分析：**

在 Transformer 模型中，KVCache 存储每个 token 的 Key 和 Value 矩阵。对于层数为 \(L\)、隐藏层维度为 \(d_{model}\)、注意力头数为 \(n_{heads}\) 的模型，单个 token 的 KVCache 大小约为：

\[\text{Size}_{\text{per token}} = 2 \times L \times n_{heads} \times d_{head} \times \text{sizeof(float32)}\]

其中 \(d_{head} = d_{model} / n_{heads}\)。

**缓存命中率模型：**

假设请求的前缀长度分布为 \(P(l)\)，缓存大小为 \(C\)，缓存命中率可以近似为：

\[\text{Hit Rate} = \sum_{l} P(l) \times \mathbb{1}[\text{prefix of length } l \text{ in cache}]\]

**多级缓存层次：**

Mooncake Store 实现三级缓存：

1. **L1 (GPU VRAM)**：最快，容量最小（通常几十 GB）
2. **L2 (CPU DRAM)**：中等速度，容量中等（通常几百 GB 到几 TB）
3. **L3 (SSD/NVMe)**：最慢，容量最大（通常几十 TB）

访问延迟近似：
\[T_{\text{GPU}} \ll T_{\text{DRAM}} \ll T_{\text{SSD}} \ll T_{\text{Network Storage}}\]

### 2.4 代码实践

**Mooncake Store 的核心接口定义：**

Mooncake Store 提供对象级别的存储操作。让我们查看架构文档中的描述：

[docs/source/design/architecture.md#L13-L20](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/design/architecture.md#L13-L20)

这段代码说明了 Mooncake Store 的核心特性：
- 提供 Get/Put/List/Del 等对象级操作
- 支持动态配置复制策略
- 支持 VRAM/DRAM/NVMe SSD 之间的零拷贝传输
- Master 节点集中管理对象到缓冲区的映射

**多级缓存实现的关键点：**

[docs/source/design/architecture.md#L5-L12](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/design/architecture.md#L5-L12)

这段描述了 Mooncake Store 的设计目标：
- 多级缓存池（高速互连的 DRAM/SSD 资源）
- RDMA 技术实现零拷贝传输
- 切片级别的放置保证和尽力而为分配
- 大对象的条带化和并行 I/O

### 2.5 练习题

**练习 1（基础）：** 对于 LLaMA3-70B 模型（70B 参数，80 层，64 头，每头 128 维），计算 10,000 个 token 的 KVCache 大小（使用 float32）。

**练习 2（进阶）：** 假设一个场景：1000 个请求，其中 30% 的请求共享相同的前 1000 个 token，20% 共享另一个前缀，其余 50% 前缀完全不同。如果 Mooncake Store 的缓存命中率为 50%，计算节省的计算量。

**练习 3（设计）：** 设计一个 KVCache 淘汰策略，考虑以下因素：访问频率、前缀长度、缓存层级。说明你的策略如何在 Mooncake Store 的三级缓存中工作。

**练习 4（分析）：** 在多租户环境中，如何保证不同租户的 KVCache 隔离？Mooncake Store 的对象级存储设计如何帮助实现这种隔离？

### 2.6 答案

**答案 1：**  
每头维度 \(d_{head} = 128\)  
单个 token 每层的大小：\(2 \times 64 \times 128 \times 4 = 65,536\) 字节  
80 层：\(65,536 \times 80 = 5,242,880\) 字节 ≈ 5 MB/token  
10,000 个 token：\(5 \text{ MB} \times 10,000 = 50 \text{ GB}\)

**答案 2：**  
总请求数：1000  
缓存命中请求：50%（500 个请求）  
对于共享前缀的请求（30% + 20% = 50% = 500 个请求），假设缓存策略完美，这 500 个请求都可以复用 KVCache  
节省的计算量：500 个请求的 Prefill 计算  
如果每个请求的 Prefill 需要 10 秒，则节省：\(500 \times 10 = 5,000\) 秒

**答案 3：**  
设计的淘汰策略（分层 LRU + 优先级）：  
1. **L1 (GPU)**：使用 LRU，但优先保留"热"前缀（高频访问 + 长前缀）  
2. **L2 (DRAM)**：使用 LRU，考虑访问频率和前缀长度  
3. **L3 (SSD)**：主要存储冷数据，使用基于空间的全局策略  
跨层级策略：L1 淘汰到 L2，L2 淘汰到 L3，除非数据被标记为"软钉住"（soft pin）  
优先级计算：\(\text{Priority} = \alpha \times \text{Frequency} + \beta \times \text{PrefixLength} - \gamma \times \text{Age}\)

**答案 4：**  
租户隔离方案：  
1. **命名空间隔离**：每个租户使用独立的命名空间，key 设计为 `tenant_id:cache_key`  
2. **资源配额**：为每个租户设置缓存空间配额，防止单个租户占用过多资源  
3. **访问控制**：在 Get/Put 操作前验证租户权限  
4. **对象级存储优势**：Mooncake Store 的对象级存储天然支持隔离，每个对象（KVCache）都有独立的 key 和元数据，易于实现租户级别的策略和监控

---

## 最小模块 3：PD 分离推理

### 3.1 概念说明

PD（Prefill-Decode）分离推理是 Mooncake 架构的核心应用场景。它将 LLM 推理的两个阶段——Prefill（处理输入文本生成 KVCache）和 Decode（基于 KVCache 生成输出文本）——分离到不同的计算集群中执行。

**核心价值：**
- **资源优化**：Prefill 是计算密集型，Decode 是内存密集型，分离后可以为每个阶段配置专门的硬件
- **弹性扩展**：根据负载特征独立扩展 Prefill 和 Decode 集群
- **性能提升**：在高并发场景下，PD 分离可以实现更高的吞吐和更低的尾部延迟

### 3.2 伪代码或流程

PD 分离推理的完整流程：

```
# 1. 请求路由
def route_request(request):
    if is_prefill_phase(request):
        return dispatch_to_prefill_cluster(request)
    else:
        return dispatch_to_decode_cluster(request)

# 2. Prefill 阶段
def prefill_cluster_processing(request):
    # 计算 KVCache
    kv_cache = model.forward_prefill(request.input_tokens)
    
    # 存储到 Mooncake Store
    cache_key = hash(request.prefix)
    mooncake_store.put(
        key=cache_key,
        value=kv_cache,
        location="prefill_node",
        tiers=["GPU", "DRAM"]
    )
    
    # 传输到 Decode 集群
    decode_node = select_decode_node()
    transfer_engine.transfer(
        source=kv_cache.location,
        destination=decode_node,
        data=kv_cache,
        protocol="RDMA",
        zero_copy=True
    )
    
    return {"cache_key": cache_key, "decode_node": decode_node}

# 3. Decode 阶段
def decode_cluster_processing(cache_key, decode_node):
    # 从本地或远程获取 KVCache
    kv_cache = decode_node.get_local_cache(cache_key)
    if kv_cache is None:
        kv_cache = mooncake_store.get(cache_key)
    
    # 生成响应
    response = model.forward_decode(kv_cache, max_tokens=request.max_tokens)
    
    return response

# 4. 完整的 PD 分离推理
def pd_disaggregated_inference(request):
    # Step 1: Prefill
    prefill_result = prefill_cluster_processing(request)
    
    # Step 2: 等待 Decode 节点就绪
    wait_for_decode_ready(prefill_result["decode_node"])
    
    # Step 3: Decode
    response = decode_cluster_processing(
        prefill_result["cache_key"],
        prefill_result["decode_node"]
    )
    
    return response
```

### 3.3 原理分析

**PD 分离的性能模型：**

在耦合架构中，单个节点的服务时间：
\[T_{\text{coupled}} = T_{\text{prefill}} + T_{\text{decode}}\]

在 PD 分离架构中，系统的有效吞吐量：
\[\text{Throughput}_{\text{PD}} = \min\left(\frac{N_p}{\overline{T}_{\text{prefill}}}, \frac{N_d}{\overline{T}_{\text{decode}}}\right)\]

其中 \(\overline{T}_{\text{prefill}}\) 和 \(\overline{T}_{\text{decode}}\) 分别是 Prefill 和 Decode 的平均服务时间，\(N_p\) 和 \(N_d\) 是节点数。

**传输开销分析：**

KVCache 传输时间：
\[T_{\text{transfer}} = \frac{\text{KVCache}_{\text{size}}}{\text{Bandwidth} \times \text{Utilization}}\]

在 8×400 Gbps RoCE 网络中，Mooncake 可以达到 190 GB/s 的带宽。对于 32K token 的 LLaMA3-70B 模型（约 4.5 GB KVCache），传输时间约为：
\[T_{\text{transfer}} = \frac{4.5 \text{ GB}}{190 \text{ GB/s}} \approx 23.7 \text{ ms}\]

**调度策略：**

Mooncake 使用基于预测的调度器，在高负载场景下提前拒绝不符合 SLO 的请求。预测模型考虑：
- 当前队列长度
- 预计的 Prefill 和 Decode 时间
- 传输时间
- SLO 要求（TTFT、ITL 等）

### 3.4 代码实践

**SGLang 集成的 PD 分离实现：**

SGLang 与 Mooncake 的集成展示了 PD 分离的实际应用。

[docs/source/getting_started/examples/sglang-integration/index.md#L7-L18](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/getting_started/examples/sglang-integration/index.md#L7-L18)

这段代码展示了 SGLang 使用 Mooncake Transfer Engine 进行跨实例 KVCache 传输：
- Prefill 和 Decode 实例之间通过 RDMA 直接传输 KVCache
- 支持 EP（Expert Parallel）和 EPD（Encode-Prefill-Decode）后端
- 在基准测试中，PD 分离实现了约 30% 更低的 ITL（Inter-Token Latency）

**vLLM 集成的 PD 分离实现：**

[docs/source/getting_started/examples/vllm-integration/index.md#L24-L28](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/getting_started/examples/vllm-integration/index.md#L24-L28)

这段描述了 vLLM 的 MooncakeConnector：
- 通过 `MooncakeConnector` 实现跨节点 KVCache 传输
- 使用 RDMA 实现高达 142.25 GB/s 的峰值带宽
- 对于 32K token 的提示词（4.5 GB KVCache），传输仅需 31.65 ms

### 3.5 练习题

**练习 1（基础）：** 在 PD 分离架构中，如果有 4 个 Prefill 节点（每个 Prefill 耗时 50ms）和 6 个 Decode 节点（每个 Decode 耗时 100ms），系统的最大吞吐量是多少？哪个集群是瓶颈？

**练习 2（进阶）：** 假设一个请求的 Prefill 生成 5 GB 的 KVCache。在 Mooncake 的 8×400 Gbps RoCE 网络中（带宽 190 GB/s），传输时间是多少？如果使用传统的 TCP 传输（假设带宽 40 GB/s），传输时间是多少？Mooncake 的优势在哪里？

**练习 3（设计）：** 设计一个 PD 分离的调度策略，考虑以下场景：80% 的请求是短文本（Prefill 20ms，Decode 50ms），20% 是长文本（Prefill 200ms，Decode 100ms）。如何分配 Prefill 和 Decode 资源？

**练习 4（分析）：** 在 PD 分离架构中，KVCache 的传输可能成为瓶颈。分析 Mooncake 如何通过以下技术缓解这个问题：(1) RDMA 零拷贝，(2) 多 NIC 聚合，(3) 拓扑感知路由。

### 3.6 答案

**答案 1：**  
Prefill 集群吞吐：\(4 / 0.05 = 80\) 请求/秒  
Decode 集群吞吐：\(6 / 0.1 = 60\) 请求/秒  
系统最大吞吐：\(\min(80, 60) = 60\) 请求/秒  
瓶颈是 Decode 集群。

**答案 2：**  
Mooncake（190 GB/s）：\(T = 5 / 190 \approx 0.0263\) 秒 = 26.3 ms  
TCP（40 GB/s）：\(T = 5 / 40 = 0.125\) 秒 = 125 ms  
优势：Mooncake 比 TCP 快约 4.75 倍，节省了约 98.7 ms 的传输时间。这在高频交易、实时对话等延迟敏感场景中非常关键。

**答案 3：**  
资源分配策略：  
1. **负载分析**：短文本占 80%，长文本占 20%  
2. **资源分配**：  
   - Prefill 资源：短文本 Prefill 20ms，长文本 200ms。假设 100 请求/秒，需要：  
     \(0.8 \times 100 \times 0.02 + 0.2 \times 100 \times 0.2 = 1.6 + 4 = 5.6\) Prefill 节点  
   - Decode 资源：短文本 Decode 50ms，长文本 100ms。需要：  
     \(0.8 \times 100 \times 0.05 + 0.2 \times 100 \times 0.1 = 4 + 2 = 6\) Decode 节点  
3. **调度策略**：  
   - 为短文本请求分配专用的 Prefill 节点（快速处理）  
   - 长文本请求使用单独的 Prefill 节点池（避免阻塞短请求）  
   - Decode 节点共享，但优先调度短请求（保证尾部延迟）

**答案 4：**  
Mooncake 缓解传输瓶颈的技术：  
1. **RDMA 零拷贝**：  
   - 数据直接从源内存传输到目标内存，绕过内核协议栈  
   - 避免多次内存复制（传统 TCP：网卡 → 内核缓冲区 → 用户缓冲区 → 内核缓冲区 → 网卡）  
   - CPU 开销大幅降低，延迟减少  
2. **多 NIC 聚合**：  
   - 同时使用多个网卡（如 8×400 Gbps）传输单个大对象  
   - 将 KVCache 分片并行传输，聚合带宽可达 190 GB/s  
   - 充分利用网络带宽，减少传输时间  
3. **拓扑感知路由**：  
   - 根据 NUMA 亲和性选择最优网卡和路径  
   - 减少跨 NUMA/跨 PCIe 传输的延迟  
   - 在异构网络中自动选择最快路径  
综合效果：这些技术使 Mooncake 能够在毫秒级完成大 KVCache 的传输，使 PD 分离的传输开销可以忽略不计（仅占 TTFT 的 4.2%）。

---

## 最小模块 4：专家并行

### 4.1 概念说明

专家并行（Expert Parallelism，EP）是 Mooncake 支持的另一个重要应用场景，专门针对混合专家模型（Mixture of Experts，MoE）的大规模推理。MoE 模型通过将计算分散到多个"专家"子模型来实现高效的参数扩展，但也带来了分布式执行的复杂性。

**Mooncake EP 的核心特性：**
- **容错专家并行**：支持在部分 rank 失败时继续服务
- **DeepEP 兼容**：与 DeepEP 的低延迟模式 API 保持一致
- **弹性扩展**：支持动态添加和移除专家节点
- **PyTorch 集成**：可以作为 PyTorch ProcessGroup 后端使用

### 4.2 伪代码或流程

专家并行的抽象流程：

```
# 传统 MoE 推理（无容错）
def traditional_moe_inference(input, routing):
    expert_outputs = []
    for expert_id in routing.selected_experts:
        expert_node = expert_mapping[expert_id]
        try:
            output = expert_node.compute(input)
            expert_outputs.append(output)
        except Exception as e:
            # 整个推理失败
            raise MoEInferenceError(f"Expert {expert_id} failed: {e}")
    
    return combine_outputs(expert_outputs)

# Mooncake EP（容错）
def mooncake_moe_inference(input, routing):
    expert_outputs = []
    failed_experts = []
    
    for expert_id in routing.selected_experts:
        expert_node = expert_mapping[expert_id]
        
        # 检查专家状态
        if not mooncake_pg.is_rank_active(expert_node.rank):
            failed_experts.append(expert_id)
            continue
        
        try:
            # 使用容错的 dispatch API
            output = mooncake_ep.dispatch_to_expert(
                input=input,
                expert_id=expert_id,
                timeout=100ms,  # 超时保护
                retry_on_failure=False  # 立即切换到备用专家
            )
            expert_outputs.append(output)
        except Exception as e:
            # 记录失败，但不阻塞整体推理
            failed_experts.append(expert_id)
            mooncake_pg.report_failure(expert_node.rank)
    
    # 使用备用专家或降级策略
    if failed_experts:
        expert_outputs = handle_failed_experts(
            failed_experts, 
            expert_outputs, 
            input
        )
    
    return combine_outputs(expert_outputs)

# 弹性 Rank 恢复
def recover_failed_rank(rank_id):
    # 1. 启动替换进程
    new_rank = spawn_replacement_process(rank_id)
    
    # 2. 加载专家模型
    new_rank.load_expert_model(expert_id=rank_id)
    
    # 3. 重新加入 ProcessGroup
    mooncake_pg.recover_rank(
        failed_rank=rank_id,
        new_rank=new_rank
    )
    
    # 4. 恢复服务
    expert_mapping[rank_id] = new_rank
    active_ranks.add(rank_id)
```

### 4.3 原理分析

**MoE 模型的路由机制：**

在 MoE 模型中，每个 token 被路由到 Top-K 个专家：

\[\text{Expert Selection} = \text{TopK}(\text{Router}(x_t), k)\]

其中 \(x_t\) 是时刻 \(t\) 的输入，Router 是一个学习到的路由函数。

**容错机制分析：**

传统 EP 的挑战：
- 单点故障：一个 rank 失败导致整个推理失败
- 静态映射：专家到 rank 的映射是固定的，难以处理动态故障
- 全局重启：恢复需要重启整个服务，成本高

Mooncake EP 的解决方案：
1. **Active Ranks 感知**：在 dispatch/combine 时跳过失败的 rank
2. **故障报告**：向 ProcessGroup 报告失败，触发恢复流程
3. **弹性恢复**：支持替换进程重新加入，无需全局重启

**通信优化：**

在专家并行中，通信模式包括：
1. **All-to-All**：将 token 分发到对应的专家节点
2. **AllGather**：收集所有专家的输出

Mooncake 使用专门的集体通信原语来优化这些操作。

### 4.4 代码实践

**Mooncake EP 的核心特性：**

[README.md#L133-L150](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/README.md#L133-L150)

这段描述了 Mooncake EP 和 PG 的核心功能：
- 容错的专家并行（active_ranks 感知）
- DeepEP 兼容的编程模型
- PyTorch ProcessGroup 集成
- 弹性 rank 恢复

**SGLang 中的 Elastic EP 集成：**

SGLang 集成了 Mooncake 的容错专家并行，支持大规模 MoE 模型的弹性推理。关键特性包括：
- 部分故障容忍
- 自动故障恢复
- 专家动态调度

### 4.5 练习题

**练习 1（基础）：** 在一个 8 专家的 MoE 模型中，每个 token 被路由到 2 个专家（Top-2）。如果有 2 个专家节点（rank）失败，Mooncake EP 如何保证推理继续进行？

**练习 2（进阶）：** 对比传统 EP 和 Mooncake EP 在处理 rank 失败时的行为。假设一个 16 节点的 MoE 推理系统，其中节点 5 发生硬件故障。描述两种系统的恢复过程和 downtime。

**练习 3（设计）：** 设计一个专家调度策略，考虑以下因素：专家负载、网络延迟、故障历史。如何在 Mooncake EP 的框架中实现这个策略？

**练习 4（分析）：** 在大规模 MoE 推理中（如 Kimi-K2 的 1T 参数模型），通信开销可能是瓶颈。分析 Mooncake 如何通过 Transfer Engine 和集体通信优化来缓解这个问题。

### 4.6 答案

**答案 1：**  
容错机制：  
1. **路由时检测**：在 dispatch 阶段，Mooncake EP 检查目标 rank 是否 active  
2. **跳过失败专家**：对于失败的 rank，使用备用专家或降级策略  
   - 如果有该专家的副本（replica），使用副本  
   - 如果没有，使用次优专家（router 的第二选择）或零输出  
3. **Combine 时过滤**：在 combine 阶段，只处理成功返回的专家输出  
4. **故障报告**：通过 `mooncake_pg.report_failure()` 向系统报告，触发恢复流程

**答案 2：**  
传统 EP：  
- 检测到 rank 5 失败 → 整个推理崩溃 → 需要重启所有 16 个节点 → 加载模型 → 恢复服务  
- Downtime：几分钟到几十分钟  
Mooncake EP：  
- 检测到 rank 5 失败 → 标记 rank 5 为 inactive → 其他 rank 继续服务（使用副本或降级） → 启动 rank 5 的替换进程 → 新 rank 重新加入 ProcessGroup → 恢复完整服务  
- Downtime：几秒到几十秒（取决于模型加载时间）  
- 关键区别：Mooncake EP 不中断正在进行的推理，只是暂时降级服务质量

**答案 3：**  
专家调度策略设计：  
1. **负载监控**：  
   - 跟踪每个专家的请求数、队列长度、处理时间  
   - 计算负载分数：\(\text{Load} = \alpha \times \text{QueueLength} + \beta \times \text{ProcessingTime}\)  
2. **网络感知**：  
   - 测量到每个专家节点的网络延迟  
   - 优先选择延迟低的专家（在负载相近时）  
3. **故障历史**：  
   - 记录每个专家的故障率和平均恢复时间  
   - 降低不稳定专家的优先级  
4. **调度算法**：  
   ```python
   def select_expert(token, router_output, active_ranks):
       # Router 输出：专家 ID 和分数
       candidates = router_output.top_k(k=4)
       
       # 过滤掉不活跃的 rank
       active_candidates = [
           e for e in candidates 
           if expert_mapping[e.id].rank in active_ranks
       ]
       
       # 根据负载、延迟、稳定性排序
       scored_experts = []
       for expert in active_candidates:
           score = (
               expert.confidence -
               expert_mapping[expert.id].load * 0.3 -
               latency_to(expert.id) * 0.2 -
               expert_mapping[expert.id].failure_rate * 0.5
           )
           scored_experts.append((expert, score))
       
       # 选择 Top-2
       return sorted(scored_experts, key=lambda x: -x[1])[:2]
   ```

**答案 4：**  
大规模 MoE 推理中的通信优化：  
1. **通信瓶颈分析**：  
   - 在 1T 参数的 Kimi-K2 模型中，单个 batch 的 All-to-All 通信可能涉及几百 GB 的数据  
   - 传统集体通信（如 NCCL）在多跳网络中延迟高  
2. **Mooncake 的优化**：  
   - **Transfer Engine RDMA**：零拷贝传输，绕过内核，降低延迟  
   - **拓扑感知路由**：选择最优网络路径，减少跳数  
   - **多 NIC 聚合**：同时使用多个网卡，聚合带宽  
   - **集体通信优化**：针对 MoE 模式的 All-to-All 和 AllGather 优化  
3. **实际效果**：  
   - 在 Kimi-K2 的部署中（128 H200 GPU），Mooncake 支持 224k tokens/sec 的 prefill 吞吐和 288k tokens/sec 的 decode 吞吐  
   - 在大规模 RL 训练中，RDMA P2P 权重传输将 1T 参数模型的更新时间从 53s 降低到 7.2s（7.4x 加速）

---

## 总结

本讲义系统讲解了 Mooncake 的架构概览和四个核心应用场景：

1. **架构概览**：理解 Mooncake 的 KVCache 中心型解耦设计和三大核心组件（Transfer Engine、Mooncake Store、EP & PG）
2. **KVCache 分离**：掌握 KVCache 作为一等公民的生命周期管理和多级缓存策略
3. **PD 分离推理**：理解 Prefill 和 Decode 分离的性能模型和调度策略
4. **专家并行**：了解容错专家并行的机制和弹性恢复流程

这些内容为读者建立了 Mooncake 的全局认知，为深入学习和实践奠定了基础。

**关键要点：**
- Mooncake 通过 KVCache 中心型设计实现了 LLM 推理的解耦架构
- Transfer Engine 提供高性能 RDMA 传输，支持零拷贝和多 NIC 聚合
- Mooncake Store 实现分布式 KVCache 存储，支持多级缓存和对象管理
- PD 分离和专家并行是 Mooncake 的两大核心应用场景，显著提升系统吞吐和资源利用率
- 容错和弹性是 Mooncake 在大规模部署中的关键优势

**后续学习建议：**
- 实践：尝试部署一个简单的 PD 分离推理系统
- 深入：研究 Transfer Engine 的 API 和性能调优
- 扩展：了解 Mooncake 与 SGLang/vLLM 的集成细节

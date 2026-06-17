# u8-l3 吞吐量优化模式

> 本讲义分析 Megakernels 中的吞吐量优化场景设计，对比延迟优化模式，讲解批量处理、内存分配和调度策略。

## 最小模块 1：吞吐量调度策略

### 概念说明

在深度学习推理中，有两种主要的优化目标：
- **延迟优化（Latency Optimization）**：最小化单个请求的处理时间，关注"何时完成"
- **吞吐量优化（Throughput Optimization）**：最大化单位时间内处理的请求数量，关注"完成了多少"

吞吐量调度策略通过批量处理（batching）和细粒度分块来提高 GPU 利用率，在牺牲单个请求响应时间的前提下，实现整体吞吐量的最大化。

### 伪代码或流程

```
# 吞吐量调度策略伪代码
def throughput_schedule(model, batch_size):
    # 1. 预分配批量缓冲区
    buffers = allocate_batch_buffers(batch_size)
    
    # 2. 为每个操作类型生成批量指令
    instructions = []
    for layer in model.layers:
        # Attention 阶段
        for batch_idx in range(batch_size):
            instructions.append(PreAttnLayerNorm(layer, batch_idx))
        
        for batch_block in range(batch_size // batch_block_size):
            for qkv_block in range(qkv_dim // output_block_size):
                instructions.append(QKV_MatMulRopeAppend(layer, batch_block, qkv_block))
        
        for batch_idx in range(batch_size):
            for kv_head in range(num_kv_heads):
                instructions.append(AttentionDecode(layer, batch_idx, kv_head))
        
        # MLP 阶段
        ...
    
    # 3. 构建 DAG 依赖关系
    dag = build_dependency_graph(instructions)
    
    # 4. 按指令类型分配到不同执行池
    schedule = pool_assign(dag, memory_fraction=0.3)
    
    return schedule
```

### 原理分析

吞吐量调度的核心原理是**批量并行化**和**资源分池**：

1. **批量并行化**：将多个样本的计算合并成批量操作，充分利用 GPU 的并行计算能力。对于矩阵乘法操作，批量处理可以显著提高计算密度。

2. **指令分池**：将指令分为计算密集型（compute）和内存密集型（memory）两类，分别调度到不同的 SM（Streaming Multiprocessor）池：
   - 计算池：处理 MatMul 等计算密集操作
   - 内存池：处理 LayerNorm 等内存密集操作
   
3. **依赖跟踪**：使用 barrier 张量跟踪批量中每个元素的依赖状态，确保只有在所有依赖满足后才执行后续操作。

设 batch_size 为 \(B\)，单个样本处理时间为 \(t_1\)，批量处理时间为 \(t_B\)，则吞吐量为：
\[
\text{Throughput} = \frac{B}{t_B}
\]

理想情况下，\(t_B \approx t_1\)，因此吞吐量可以接近 \(B\) 倍提升。

### 代码实践

吞吐量调度的核心实现在 `ThroughputScheduleBuilder` 中：

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/throughput/scheduler.py#L401-L411

```python
class ThroughputScheduleBuilder(ScheduleBuilder):
    @classmethod
    def make_globals(cls, model):
        return make_globals(model)

    @classmethod
    def make_dag(
        cls, globs, stop_after_op: str | None = None, layer_limit: int | None = None
    ):
        return make_dag(globs, stop_after_op, layer_limit)
```

批量缓冲区的分配策略：

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/throughput/scheduler.py#L25-L43

这段代码定义了全局状态和缓冲区的分配，包括预分配批量激活缓冲区，设置分块大小参数，并创建 barrier 同步张量用于跟踪依赖关系。

批量调度示例——Attention 解码阶段：

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/throughput/scheduler.py#L140-L156

这段代码为 Attention 解码生成批量指令，每个 batch 元素和每个 KV head 都生成独立的指令，由 barrier 机制保证依赖顺序。

指令池的分类：

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/throughput/instructions.py#L43-L55

这里定义了 `ComputeInstruction` 和 `MemoryInstruction` 两个基类，为后续的池化调度提供类型标记。

### 练习题

1. **基础题**：吞吐量调度和延迟调度的主要区别是什么？各自的应用场景是什么？

2. **进阶题**：在 `make_globals` 中，为什么 barrier 张量的第四个维度要取 `num_attention_heads + num_key_value_heads * 2` 和 `intermediate_size / matmul_output_block_size` 的最大值？

3. **实现题**：假设 batch_size = 256，matmul_batch_block_size = 128，计算 `PreAttnLayerNorm` 和 `QKV_MatMulRopeAppend` 分别会生成多少条指令？

4. **设计题**：如果要将 batch_size 从 256 增加到 512，需要对哪些缓冲区和 barrier 张量进行调整？

### 答案

1. **基础题**：
   - 区别：吞吐量调度关注批量处理效率，延迟调度关注单请求响应时间
   - 应用场景：吞吐量调度适用于离线批处理、高并发服务；延迟调度适用于实时交互、单请求优化

2. **进阶题**：因为 barrier 的第四个维度需要能够索引所有可能的并行单元。Attention 阶段的最大并行度是 `num_attention_heads + 2 * num_key_value_heads`（Q、K、V），MLP 阶段的最大并行度是 `intermediate_size / matmul_output_block_size`。取最大值确保 barrier 张量足够大以覆盖所有情况。

3. **实现题**：
   - `PreAttnLayerNorm`：batch_size = 256 条
   - `QKV_MatMulRopeAppend`：(256 / 128) × (qkv_dim / 128) 条，假设 qkv_dim = 4096，则 2 × 32 = 64 条

4. **设计题**：
   - 所有 `[batch_size, ...]` 形状的缓冲区需要调整为 `[512, ...]`
   - barrier 张量的第三维需要从 256 扩展到 512
   - 检查 `max_batch_size` 配置是否支持 512

---

## 最小模块 2：批量处理优化

### 概念说明

批量处理优化是指将多个独立的计算任务合并成一个批量操作，从而提高计算和内存访问的效率。在 GPU 上，批量处理可以：
1. **提高计算密度**：更多的并行线程充分利用 GPU 的计算单元
2. **减少内存开销**：合并内存访问，减少 kernel 启动开销
3. **简化同步**：通过 barrier 机制统一管理批量依赖关系

### 伪代码或流程

```
# 批量处理优化伪代码
def batch_operation(activations, weights, batch_size, block_size):
    # 1. 分块处理批量数据
    num_blocks = batch_size // block_size
    results = []
    
    for batch_block in range(num_blocks):
        start = batch_block * block_size
        end = start + block_size
        batch_activations = activations[start:end]
        
        # 2. 对每个输出块进行分块计算
        for output_block in range(output_dim // output_block_size):
            out_start = output_block * output_block_size
            out_end = out_start + output_block_size
            
            # 3. 执行批量矩阵乘法
            block_weights = weights[out_start:out_end]
            block_result = matmul(batch_activations, block_weights)
            results.append((batch_block, output_block, block_result))
    
    # 4. 合并结果
    final_result = combine_results(results)
    return final_result
```

### 原理分析

批量处理优化的数学原理基于**矩阵分块乘法**。给定批量激活矩阵 \(X \in \mathbb{R}^{B \times d}\) 和权重矩阵 \(W \in \mathbb{R}^{d \times h}\)，我们将其分块：

\[
X = \begin{bmatrix} X_{1,1} & X_{1,2} \\ X_{2,1} & X_{2,2} \end{bmatrix}, \quad
W = \begin{bmatrix} W_{1,1} & W_{1,2} \\ W_{2,1} & W_{2,2} \end{bmatrix}
\]

其中：
- \(X_{i,j} \in \mathbb{R}^{B_b \times d_b}\)：批量块 \(i\)，特征块 \(j\)
- \(W_{j,k} \in \mathbb{R}^{d_b \times h_b}\)：输入块 \(j\)，输出块 \(k\)

批量矩阵乘法的输出块为：

\[
Y_{i,k} = \sum_{j} X_{i,j} W_{j,k}
\]

分块处理的优势：
1. **缓存友好**：每个块可以独立处理，充分利用 GPU 的共享内存
2. **并行度高**：不同的 \((i,k)\) 块对可以并行计算
3. **负载均衡**：通过合理分配块大小，平衡各 SM 的计算负载

### 代码实践

批量处理的关键在于**分块策略**和**barrier 同步**：

QKV MatMul 的批量分块处理：

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/throughput/scheduler.py#L115-L138

这段代码实现了 QKV 矩阵乘法的批量分块调度，按照批量块和输出块的双重循环生成指令，每个指令处理一个 `(batch_block, qkv_block)` 组合。

barrier 同步检查：

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/throughput/python_vm.py#L85-L95

这段代码在执行 QKV MatMul 前检查 barrier，确保前序操作（PreAttnLayerNorm）在当前批量块上已经完成。

批量 Attention 解码：

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/throughput/python_vm.py#L171-L191

这段代码实现了单个 batch 元素的 Attention 解码，通过 barrier 检查确保该 batch 元素的所有 QKV 块都已完成。

### 练习题

1. **基础题**：为什么批量处理需要分块？直接处理整个 batch 有什么问题？

2. **进阶题**：在 `QKV_MatMulRopeAppend` 的 barrier 检查中，为什么要求 `barriers[...] == matmul_batch_block_size`？

3. **实现题**：假设 batch_size = 128，matmul_batch_block_size = 32，hidden_size = 4096，matmul_output_block_size = 128，计算 `O_ProjResidual` 会生成多少条指令？

4. **设计题**：如果想要支持变长 batch（即不同请求的 batch size 不同），需要对调度策略做什么修改？

### 答案

1. **基础题**：
   - GPU 共享内存和寄存器有限，大 batch 无法一次性加载
   - 分块可以提高缓存命中率，减少全局内存访问
   - 分块便于负载均衡和并行调度

2. **进阶题**：因为 `PreAttnLayerNorm` 为每个 batch 元素生成一条指令，一个批量块包含 `matmul_batch_block_size` 个 batch 元素。barrier 计数达到 `matmul_batch_block_size` 表示该批量块中所有元素的 Norm 操作都已完成。

3. **实现题**：
   - 批量块数：128 / 32 = 4
   - 输出块数：4096 / 128 = 32
   - 总指令数：4 × 32 = 128 条

4. **设计题**：
   - 需要动态调整 barrier 张量的大小
   - 调度时需要根据实际 batch size 生成指令，而非固定的 `batch_size`
   - 可以使用 mask 或动态形状来处理变长情况

---

## 最小模块 3：与延迟模式对比

### 概念说明

Megakernels 提供了两种调度模式：
- **延迟模式（Latency Mode）**：位于 `megakernels/demos/latency/`，优化单请求响应时间
- **吞吐量模式（Throughput Mode）**：位于 `megakernels/demos/throughput/`，优化批量处理效率

这两种模式在设计目标、调度策略、资源分配等方面都有显著差异。

### 伪代码或流程

```
# 延迟模式 vs 吞吐量模式对比
def latency_scheduling(model, single_input):
    # 延迟模式：单请求深度并行
    instructions = []
    
    # 1. 按层顺序调度
    for layer in model.layers:
        # 2. 在单请求内按 SM 并行分块
        for sm_idx in range(num_sms):
            for block_idx in range(blocks_per_sm):
                instructions.append(
                    LayerNorm_QKV_MatVecRopeAppend(
                        layer=layer,
                        sm_idx=sm_idx,
                        block_range=(start, end)
                    )
                )
    
    # 3. 使用 SM 级别的并行
    schedule = assign_to_sms(instructions, mode='wave')
    return schedule


def throughput_scheduling(model, batch_input):
    # 吞吐量模式：批量处理 + 池化分配
    instructions = []
    
    # 1. 按批量索引调度
    for batch_idx in range(batch_size):
        for layer in model.layers:
            # 2. 为每个 batch 元素生成独立指令
            instructions.append(
                PreAttnLayerNorm(layer=layer, batch_idx=batch_idx)
            )
    
    # 3. 按指令类型分池
    schedule = pool_assign(instructions, memory_fraction=0.3)
    return schedule
```

### 原理分析

#### 对比维度分析

| 维度 | 延迟模式 | 吞吐量模式 |
|---|---|---|
| **优化目标** | 最小化单请求延迟 | 最大化批量吞吐量 |
| **并行粒度** | SM 级并行（单请求内分块） | Batch 级并行（多请求独立） |
| **调度策略** | Wave 调度（按波次分配） | Pool 调度（按类型分池） |
| **内存分配** | 单样本激活缓冲区 | 批量激活缓冲区 |
| **barrier 维度** | `[layer, opcode, head_dim]` | `[layer, opcode, batch_size, block_dim]` |
| **指令粒度** | 粗粒度（一个 SM 处理多个块） | 细粒度（每个块独立指令） |
| **适用场景** | 实时推理、单请求优化 | 批处理、高并发服务 |

#### 性能特征

延迟模式的性能模型：
\[
T_{\text{latency}} = \sum_{\text{layer}} \frac{\text{work}_{\text{layer}}}{\text{parallelism}_{\text{SM}}}
\]

吞吐量模式的性能模型：
\[
T_{\text{throughput}} = \sum_{\text{layer}} \frac{\text{work}_{\text{layer}} \times B}{\text{parallelism}_{\text{pool}}}
\]

其中 \(B\) 是 batch size。当 \(\text{parallelism}_{\text{pool}} \approx B \times \text{parallelism}_{\text{SM}}\) 时，吞吐量模式的时间接近延迟模式，但处理的请求数是 \(B\) 倍。

### 代码实践

#### 延迟模式的调度策略

延迟模式使用 Wave 调度，按波次分配指令到 SM：

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L194-L218

这段代码实现了 Wave 调度策略，将指令按 opcode 分组成波次，然后按成本贪心分配到各 SM，确保负载均衡。

延迟模式的指令粒度较粗：

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L118-L143

这段代码为延迟模式的 QKV 操作生成指令，每个 SM 处理多个输出块，减少指令数量和同步开销。

#### 吞吐量模式的调度策略

吞吐量模式使用 Pool 调度，按指令类型分池：

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L220-L243

这段代码实现了 Pool 调度策略，将指令按标签分为 compute 和 memory 两类，分配到不同的 SM 池中执行。

吞吐量模式的指令粒度较细：

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/throughput/scheduler.py#L103-L112

这段代码为吞吐量模式的 PreAttnLayerNorm 生成指令，每个 batch 元素生成一条独立指令，便于细粒度并行。

#### 全局状态对比

延迟模式的全局状态（单样本）：

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L38-L115

这段代码创建延迟模式的全局状态，所有激活缓冲区都是单样本形状（如 `[hidden_size]`）。

吞吐量模式的全局状态（批量）：

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/throughput/scheduler.py#L25-L100

这段代码创建吞吐量模式的全局状态，激活缓冲区是批量形状（如 `[batch_size, hidden_size]`），并包含 barrier 张量用于批量同步。

### 练习题

1. **基础题**：延迟模式和吞吐量模式分别适合哪些应用场景？给出具体例子。

2. **进阶题**：为什么延迟模式使用 Wave 调度，而吞吐量模式使用 Pool 调度？交换使用会有什么问题？

3. **实现题**：对比两种模式的 barrier 张量维度，解释为什么吞吐量模式需要额外的 `batch_size` 维度？

4. **设计题**：如果要设计一个混合模式，在低并发时使用延迟调度，高并发时自动切换到吞吐量调度，需要考虑哪些因素？

### 答案

1. **基础题**：
   - 延迟模式：实时对话系统、在线推理服务等对单请求延迟敏感的场景
   - 吞吐量模式：批处理任务、离线推理、高并发 API 服务等对整体吞吐量敏感的场景

2. **进阶题**：
   - 延迟模式的指令是顺序依赖的，Wave 调度可以最大化并行度同时保证依赖
   - 吞吐量模式的指令可以按类型并行，Pool 调度可以将不同类型的指令隔离到不同 SM 池
   - 交换使用会导致：延迟模式下 Pool 调度浪费并行度；吞吐量模式下 Wave 调度无法充分利用类型并行

3. **实现题**：
   - 延迟模式 barrier：`[num_layers, 10, num_heads + 2 * num_kv_heads]`，只需跟踪层内不同头的依赖
   - 吞吐量模式 barrier：`[num_layers, 10, batch_size, max_dim]`，需要跟踪每个 batch 元素的依赖
   - 吞吐量模式额外的 `batch_size` 维度是因为每个 batch 元素有独立的执行进度，需要单独跟踪

4. **设计题**：
   - 并发度阈值：定义多少并发请求时切换模式
   - 切换成本：模式切换的预热时间和资源开销
   - 资源分配：如何在两种模式之间动态分配 SM 资源
   - 监控指标：需要监控队列长度、平均延迟、吞吐量等指标
   - 降级策略：高负载下如何处理模式切换失败的情况

---

## 最小模块 4：内存分配策略

### 概念说明

在吞吐量优化中，内存分配策略对性能有重要影响。Megakernels 采用**预分配 + 复用**的策略：
1. **预分配**：在初始化时分配所有需要的缓冲区，避免运行时动态分配
2. **批量缓冲区**：为批量处理分配 `[batch_size, feature_dim]` 形状的缓冲区
3. **barrier 缓冲区**：预分配 barrier 张量用于同步跟踪
4. **分块对齐**：缓冲区大小按照块大小对齐，便于分块处理

### 伪代码或流程

```
# 内存分配策略伪代码
def allocate_throughput_buffers(model, batch_size, block_sizes):
    buffers = {}
    
    # 1. 预分配激活缓冲区（批量形状）
    for name, shape in model.activation_shapes.items():
        batch_shape = (batch_size,) + shape
        buffers[name] = torch.zeros(batch_shape, dtype=model.dtype, device=model.device)
    
    # 2. 分配 barrier 缓冲区（高层维度 + 批量维度）
    max_parallel_units = compute_max_parallel(model, block_sizes)
    barriers = torch.zeros(
        [model.num_layers, 
         model.num_opcodes,
         batch_size,
         max_parallel_units],
        dtype=torch.int32,
        device=model.device
    )
    
    # 3. 预分配 KV cache（多层 + 批量）
    kv_cache = torch.zeros(
        [model.num_layers,
         batch_size,
         model.max_seq_len,
         model.num_kv_heads,
         model.head_dim],
        dtype=model.dtype,
        device=model.device
    )
    
    return buffers, barriers, kv_cache


def compute_max_parallel(model, block_sizes):
    # 计算最大并行单元数
    # Attention 阶段：Q、K、V 的头数
    attn_parallel = model.num_attention_heads + 2 * model.num_kv_heads
    
    # MLP 阶段：输出块数
    mlp_parallel = model.intermediate_size // block_sizes.matmul_output
    
    return max(attn_parallel, mlp_parallel)
```

### 原理分析

#### 内存分配优化原理

1. **预分配消除动态开销**：
   - GPU 内存分配（`cudaMalloc`）开销大，可能需要数百微秒
   - 预分配将所有内存一次性分配，避免推理时的分配延迟

2. **批量缓冲区内存效率**：
   设单个样本激活大小为 \(S\)，batch size 为 \(B\)：
   - 分离分配：\(B \times S + \text{overheads}\)
   - 批量分配：\(B \times S\)（连续内存，元数据更少）
   
   批量分配可以减少内存碎片和提高缓存命中率。

3. **barrier 缓冲区的数学模型**：

   barrier 张量的形状为 \([L, O, B, P]\)，其中：
   - \(L\)：层数
   - \(O\)：操作码数
   - \(B\)：batch size
   - \(P\)：最大并行单元数

   内存占用为：
   \[
   \text{Barrier Memory} = L \times O \times B \times P \times \text{sizeof(int32)}
   \]

   对于 32 层、10 个操作、batch 256、并行单元 128 的情况：
   \[
   32 \times 10 \times 256 \times 128 \times 4 \text{ bytes} \approx 42 \text{ MB}
   \]

4. **分块对齐的访存优化**：

   分块对齐确保内存访问按照块边界对齐，提高内存带宽利用率。对齐的访存模式可以让 GPU 的内存控制器更好地预取和合并访问。

### 代码实践

#### 批量缓冲区分配

批量缓冲区的分配函数：

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/throughput/scheduler.py#L32-L33

这段代码定义了批量缓冲区的分配函数，创建形状为 `(batch_size, feature_dim)` 的张量。

批量缓冲区的实际分配：

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/throughput/scheduler.py#L73-L81

这段代码为各种中间激活分配批量缓冲区，包括 hidden states、RMS 中间结果、attention 输出、SiLU 输出和 logits。

#### barrier 张量分配

barrier 张量的分配策略：

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/throughput/scheduler.py#L44-L56

这段代码分配了 barrier 张量，其第四维大小为 Attention 头数和 MLP 输出块数的最大值，确保能够覆盖所有并行单元。

#### KV Cache 分配

KV Cache 的分配（在模型初始化时）：

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/throughput/scheduler.py#L69-L70

这段代码将模型的 KV cache 传递给全局状态，KV cache 在模型初始化时已经分配为多层多批量的形状。

#### 分块大小的配置

分块大小的全局配置：

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/throughput/scheduler.py#L40-L42

这段代码设置了三种分块大小：
- `matmul_batch_block_size = 128`：批量维度的块大小
- `matmul_output_block_size = 128`：输出维度的块大小  
- `norm_block_size = 16`：归一化操作的块大小

#### 辅助函数

块数量计算函数：

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/throughput/instructions.py#L30-L40

这些辅助函数计算各个维度的块数量，确保整除（`assert_div`），便于分块调度。

### 练习题

1. **基础题**：为什么要在推理前预分配所有缓冲区？直接在推理时动态分配有什么问题？

2. **进阶题**：计算一个 32 层 Llama-7B 模型在 batch_size = 256 时的 barrier 张量内存占用。假设 num_attention_heads = 32，num_kv_heads = 4，head_dim = 128，intermediate_size = 11008，matmul_output_block_size = 128。

3. **实现题**：如果要将 matmul_batch_block_size 从 128 调整到 64，会对内存分配和调度产生什么影响？

4. **设计题**：设计一个内存预算分配器，给定固定的 GPU 内存预算（如 8GB），如何合理分配给激活缓冲区、KV cache 和 barrier 张量？

### 答案

1. **基础题**：
   - 动态分配开销大：每次 cudaMalloc 可能需要数百微秒
   - 内存碎片：频繁分配释放导致内存碎片化
   - 同步开销：动态分配可能需要 GPU 同步，破坏流水线
   - 失败风险：运行时分配可能因内存不足失败

2. **进阶题**：
   - Attention 并行单元：32 + 2 × 4 = 40
   - MLP 并行单元：11008 / 128 = 86.125 ≈ 87
   - 最大并行单元：max(40, 87) = 87
   - barrier 张量形状：[32, 10, 256, 87]
   - 内存占用：32 × 10 × 256 × 87 × 4 bytes = 28,599,936 bytes ≈ 27.3 MB

3. **实现题**：
   - 批量块数增加：256 / 128 → 256 / 64，指令数量增加
   - barrier 张量第三维不变，仍是 batch_size = 256
   - 每个批量块处理的数据量减少，可能影响计算密度
   - 需要重新调整 `num_batch_blocks()` 等辅助函数

4. **设计题**：
   - 激活缓冲区（必需）：batch_size × hidden_size × dtype_size × num_buffers
   - KV cache（必需）：num_layers × batch_size × max_seq_len × kv_size × dtype_size × 2
   - barrier 张量（必需）：按上述公式计算
   - 权重（必需）：模型参数大小
   - 剩余可用于工作区内存
   - 分配策略：优先满足必需项，剩余分配给更大的 batch_size 或更长的序列

---

## 总结

本讲义介绍了 Megakernels 的吞吐量优化模式，涵盖四个核心最小模块：

1. **吞吐量调度策略**：通过批量处理和指令分池提高 GPU 利用率
2. **批量处理优化**：利用分块和 barrier 机制实现高效的批量并行计算
3. **与延迟模式对比**：分析两种模式的设计目标、调度策略和适用场景差异
4. **内存分配策略**：通过预分配、批量缓冲区和 barrier 张量优化内存使用

吞吐量优化模式适用于批处理和高并发场景，通过牺牲单请求延迟来换取整体吞吐量的提升，是离线推理和高并发服务的重要优化手段。

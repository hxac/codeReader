# 延迟优化调度策略

在延迟优化场景中，推理的批处理大小通常为 1（单 token 或少量 token）。此时，GPU 上有大量流式多处理器（Streaming Multiprocessor, SM）处于空闲状态。为了最大化利用硬件并行能力，我们需要将单个层的计算任务拆分到多个 SM 上并行执行。

本讲义将深入讲解 Megakernels 在延迟优化场景下的调度策略，涵盖 QKV 投影、注意力计算、MLP 层和语言模型头（LM Head）的并行化方法。

## 1. QKV 并行调度

### 1.1 概念说明

Transformer 的自注意力机制需要为每个输入 token 计算查询（Query, Q）、键（Key, K）和值（Value, V）三个投影。对于分组查询注意力（Grouped Query Attention, GQA）或多头注意力（Multi-Head Attention, MHA），这些投影的总输出维度为：

\[ \text{qkv\_outdim} = (\text{num\_attention\_heads} + 2 \times \text{num\_kv\_heads}) \times \text{head\_dim} \]

在延迟优化场景中，我们需要将这个大的矩阵-向量乘法任务分配给多个 SM 并行执行，以减少单个 SM 的工作量，从而降低延迟。

### 1.2 伪代码流程

```python
def schedule_qkv_parallel(num_attention_heads, num_kv_heads, head_dim,
                          hidden_size, sm_count, block_size):
    # 计算输出维度
    qkv_outdim = (num_attention_heads + 2 * num_kv_heads) * head_dim
    
    # 将输出划分为多个 block
    num_blocks = qkv_outdim // block_size
    
    # 计算每个 SM 应处理的 block 数量
    blocks_per_sm = num_blocks / sm_count
    
    # 为每个 SM 分配连续的 block 区间
    for sm_idx in range(sm_count):
        start = round(sm_idx * blocks_per_sm)
        end = round((sm_idx + 1) * blocks_per_sm)
        yield (sm_idx, start, end)
```

### 1.3 原理分析

QKV 投影的计算本质是：\( \text{output} = \text{input} \times W^T \)，其中 input 的形状为 \([1, \text{hidden\_size}]\)，权重 \(W\) 的形状为 \([\text{qkv\_outdim}, \text{hidden\_size}]\)。

通过按输出维度切分，每个 SM 计算输出张量的一部分：

\[ \text{output}[start \times \text{block\_size} : end \times \text{block\_size}] = \text{input} \times W[start \times \text{block\_size} : end \times \text{block\_size}, :]^T \]

这种切分方式有以下优点：
1. **负载均衡**：每个 SM 处理的计算量相近（block 数量相近）
2. **内存访问局部性**：每个 SM 访问连续的输出区间，有利于合并内存访问
3. **独立性**：各 SM 之间无需同步，可并行执行

### 1.4 代码实践

Megakernels 的 QKV 并行调度实现在 `schedule_qkv` 函数中：

[https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L118-L142](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L118-L142)

这段代码实现了上述调度策略，具体说明如下：

- **第 123 行**：计算 QKV 总输出维度，包含所有注意力头和 KV 头的维度
- **第 125 行**：将输出维度按 `qkv_block_size`（默认 16）切分为多个 block
- **第 128 行**：计算每个 SM 应处理的 block 数量
- **第 131-140 行**：为每个 SM 分配连续的 block 区间，创建 `LayerNorm_QKV_MatVecRopeAppend` 指令

每个 SM 执行的指令包含层归一化、矩阵-向量乘法、旋转位置编码（RoPE）以及将 K 和 V 追加到 KV cache。

### 1.5 练习题

1. 假设模型有 32 个注意力头、4 个 KV 头、每个头维度为 128，`qkv_block_size` 为 16，GPU 有 80 个 SM。请计算每个 SM 处理多少个 block？

2. 为什么 QKV 调度选择为每个 SM 分配**连续**的 block 区间，而不是采用循环分配（如第 0、16、32... block 给 SM0）？

3. 在延迟优化场景下，假设批处理大小为 1、序列长度为 1，QKV 计算的时间复杂度是多少？如果我们将计算分配给 \(N\) 个 SM 并行执行，理论上最大加速比是多少？

### 1.6 答案

1. **解答**：
   - QKV 总输出维度 = \((32 + 2 \times 4) \times 128 = 40 \times 128 = 5120\)
   - Block 数量 = \(5120 / 16 = 320\)
   - 每个 SM 的 block 数 = \(320 / 80 = 4\)

2. **解答**：连续分配的优势在于：
   - **内存合并**：每个 SM 写入连续的内存区间，GPU 内存控制器可以合并写入请求
   - **缓存友好**：权重矩阵的读取也是连续的，有利于利用 L2 缓存
   - **简化逻辑**：每个 SM 只需维护一个 `[start, end)` 区间，指令参数更简单

   循环分配会导致写入分散，降低内存带宽利用率。

3. **解答**：
   - 时间复杂度：\(O(\text{hidden\_size} \times \text{qkv\_outdim})\)
   - 理论最大加速比：在理想情况下（无同步开销、负载完全均衡），加速比为 \(\min(N, \text{num\_blocks})\)。但实际上受限于指令启动开销、内存带宽等因素，实际加速比会小于理论值。

---

## 2. 注意力分区

### 2.1 概念说明

在自注意力计算中，每个查询需要与所有历史键值对计算注意力分数。当序列长度增长时，注意力计算的复杂度会以 \(O(L^2)\) 增长，其中 \(L\) 是序列长度。

对于延迟优化场景，我们可以将序列长度划分为多个分区，每个 SM 并行计算一个分区内的注意力分数。这种方法称为**注意力分区**（Attention Partitioning）。

### 2.2 伪代码流程

```python
def pick_attention_partitions(prompt_len, ntok, min_chunk_size=256):
    full_len = prompt_len + ntok
    num_divisions = ceil(full_len / min_chunk_size)
    num_partitions = min(num_divisions, 24)  # 受限于归约树结构
    return num_partitions

def schedule_partial_attention(num_kv_heads, num_partitions):
    for kv_head_idx in range(num_kv_heads):
        for partial_idx in range(num_partitions):
            # 每个 partial 处理 1/num_partitions 的序列
            yield PartialAttention(
                kv_head_idx=kv_head_idx,
                num_partials=num_partitions,
                partial_idx=partial_idx
            )
```

### 2.3 原理分析

标准注意力计算公式为：

\[ \text{Attention}(Q, K, V) = \text{softmax}\left(\frac{QK^T}{\sqrt{d_k}}\right)V \]

在分区场景下，我们将 \(K\) 和 \(V\) 按序列维度划分为 \(P\) 个分区：

\[ K = [K^{(0)}, K^{(1)}, ..., K^{(P-1)}], \quad V = [V^{(0)}, V^{(1)}, ..., V^{(P-1)}] \]

每个分区计算部分注意力：

\[ \text{Partial}^{(p)} = \text{softmax}\left(\frac{Q (K^{(p)})^T}{\sqrt{d_k}}\right) V^{(p)} \]

最终的注意力输出需要将所有部分结果归约（reduction）：

\[ \text{Attention}(Q, K, V) \approx \sum_{p=0}^{P-1} \text{Partial}^{(p)} \]

**注意**：Megakernels 的延迟优化实现中，`skip_attn_reduction=True` 意味着跳过了归约步骤，这在某些特殊场景下使用（如调试或特定优化）。

### 2.4 代码实践

注意力分区数量的选择实现在 `pick_num_attention_partitions` 函数中：

[https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L21-L35](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L21-L35)

这段代码根据序列长度动态确定分区数量：
- **第 22 行**：设置最小 chunk 大小为 256
- **第 25 行**：计算需要的分区数量（向上取整）
- **第 28 行**：限制最大分区数为 24（受限于归约树实现）

在 `make_dag_layer` 函数中，部分注意力指令的创建如下：

[https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L299-L341](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L299-L341)

这段代码为每个 KV 头和每个分区创建 `PartialAttention` 指令：
- **第 302-309 行**：遍历所有 KV 头和分区索引
- **第 313-328 行**：计算当前分区依赖的 K 和 V 的 block 索引
- **第 330-334 行**：从 QKV 阶段的输出中收集依赖的节点
- **第 336 行**：创建部分注意力节点，加入 DAG

### 2.5 练习题

1. 假设 prompt 长度为 512，生成长度为 1，最小 chunk 大小为 256。请计算需要多少个注意力分区？

2. 为什么注意力分区数量有上限（24）？这个限制可能来自哪里？

3. 在延迟优化场景下，注意力分区如何影响内存访问模式？

### 2.6 答案

1. **解答**：
   - 总序列长度 = \(512 + 1 = 513\)
   - 分区数量 = \(\lceil 513 / 256 \rceil = \lceil 2.0039 \rceil = 3\)

2. **解答**：
   - 这个限制来自归约树的实现。当有多个分区时，需要对各分区的结果进行归约求和。
   - 24 可能是当前实现的归约树能处理的最大分区数（可能与 SM 数量、barrier 同步机制有关）。
   - 代码注释中也提到这是"待改进的限制"（TODO limitation）。

3. **解答**：
   - **优点**：每个 SM 只需加载和计算部分序列的 K/V，减少单 SM 的内存压力
   - **缺点**：需要额外的归约步骤来合并各分区结果，增加同步开销
   - 在延迟优化场景下，由于批处理大小为 1，内存带宽通常不是瓶颈，分区主要帮助减少计算延迟

---

## 3. UpGate 并行化

### 3.1 概念说明

Transformer 的 MLP 层（也称为 Feed-Forward Network, FFN）包含两个线性变换：
1. **Up 投影和 Gate 投影**：\( \text{gate} = \text{SiLU}(x W_g), \quad \text{up} = x W_u \)
2. **Down 投影**：\( \text{output} = (\text{gate} \odot \text{up}) W_d \)

其中 \(\odot\) 表示逐元素乘法。前两个投影（Up 和 Gate）可以并行执行，因为它们共享相同的输入。

### 3.2 伪代码流程

```python
def schedule_upgate_parallel(intermediate_size, hidden_size,
                            sm_count, block_size):
    num_blocks = intermediate_size // block_size
    blocks_per_sm = num_blocks / sm_count
    
    for sm_idx in range(sm_count):
        # 循环分配：每个 SM 处理间隔的 blocks
        block_idxs = list(range(sm_idx, num_blocks, sm_count))
        yield UpGateInstruction(block_idxs=block_idxs)
```

### 3.3 原理分析

Up 和 Gate 投影的计算公式为：

\[ \text{output}_{\text{gate}} = \text{SiLU}(x W_g), \quad \text{output}_{\text{up}} = x W_u \]

其中 \(x \in \mathbb{R}^{1 \times \text{hidden\_size}}\)，\(W_g, W_u \in \mathbb{R}^{\text{intermediate\_size} \times \text{hidden\_size}}\)。

通过按输出维度切分，每个 SM 计算输出的一部分：

\[ \text{output}_{\text{gate}}[i] = \text{SiLU}(x W_g[i, :]), \quad \text{output}_{\text{up}}[i] = x W_u[i, :] \]

其中 \(i\) 是 block 索引。

**关键设计**：Megakernels 采用**循环分配**策略，即 SM0 处理 block 0, \(N, 2N, ...\)，SM1 处理 block 1, \(N+1, 2N+1, ...\)，其中 \(N\) 是 SM 数量。这种策略与 QKV 的连续分配不同。

### 3.4 代码实践

UpGate 并行调度实现在 `schedule_upgate` 函数中：

[https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L145-L164](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L145-L164)

这段代码实现了循环分配策略：
- **第 147-149 行**：计算 Up/Gate 输出的 block 数量
- **第 151 行**：获取 SM 数量
- **第 156-162 行**：为每个 SM 创建 `LayerNormDoubleMatVecSiLU` 指令，使用 `block_idxs` 参数指定循环分配的 block 索引

`LayerNormDoubleMatVecSiLU` 指令同时执行层归一化、两个矩阵-向量乘法和 SiLU 激活函数，对应 Up 和 Gate 投影。

### 3.5 练习题

1. 假设 `intermediate_size` 为 13760，`up_gate_proj_block_size` 为 16，SM 数量为 80。请计算 SM0 和 SM1 分别处理哪些 block 索引？

2. 为什么 UpGate 采用循环分配，而 QKV 采用连续分配？

3. 在 `LayerNormDoubleMatVecSiLU` 指令中，为什么要同时执行两个矩阵-向量乘法，而不是分别执行？

### 3.6 答案

1. **解答**：
   - Block 数量 = \(13760 / 16 = 860\)
   - SM0 处理的 block 索引：0, 80, 160, 240, 320, 400, 480, 560, 640, 720, 800（共 11 个）
   - SM1 处理的 block 索引：1, 81, 161, 241, 321, 401, 481, 561, 641, 721, 801（共 11 个）

2. **解答**：
   - **连续分配（QKV）**：适合写入连续内存的情况，有利于内存合并
   - **循环分配（UpGate）**：可能是为了负载均衡。如果 Up 和 Gate 的权重在内存中的布局有特殊模式（如交错存储），循环分配可以减少缓存冲突

   具体原因可能与硬件架构和内存布局有关，需要根据实际实现分析。

3. **解答**：
   - **减少启动开销**：两个乘法打包在一个指令中，只需一次 kernel 启动
   - **共享输入**：两个乘法都使用相同的输入 \(x\)，可以复用加载的数据
   - **流水线优化**：GPU 可以并行执行两个独立的矩阵-向量乘法，隐藏延迟

---

## 4. DownProj 调度

### 4.1 概念说明

Down 投影（Down Projection）是 MLP 层的最后一步，将 Up 和 Gate 输出的逐元素乘积投影回隐藏维度：

\[ \text{output} = (\text{gate} \odot \text{up}) W_d \]

其中 \(W_d \in \mathbb{R}^{\text{hidden\_size} \times \text{intermediate\_size}}\)。

在延迟优化场景中，Down 投影的调度策略更加复杂，因为输出维度通常小于输入维度，需要处理**维度不匹配**的问题。

### 4.2 伪代码流程

```python
def schedule_downproj(hidden_size, intermediate_size, sm_count, block_size):
    num_down_blocks = hidden_size // block_size
    num_col_splits = intermediate_size // hidden_size  # 切分份数
    
    # 生成所有任务：(列索引, 输出block索引)
    jobs = []
    for col_idx in range(num_col_splits):
        for down_block_idx in range(num_down_blocks):
            jobs.append((col_idx, down_block_idx))
    
    # 将任务分配给 SM，确保每个 SM 的任务属于同一列
    num_assigned = 0
    for sm_idx in range(sm_count):
        jobs_left = len(jobs) - num_assigned
        sms_left = sm_count - sm_idx
        jobs_per_sm = jobs_left / sms_left
        
        sliced_jobs = jobs[num_assigned : num_assigned + round(jobs_per_sm)]
        col_idx = sliced_jobs[0][0]  # 确保所有任务属于同一列
        
        # 提取该列的输出 block 范围
        output_blocks = [job[1] for job in sliced_jobs if job[0] == col_idx]
        
        yield DownProjInstruction(
            start_block_idx=output_blocks[0],
            end_block_idx=output_blocks[-1] + 1,
            reduction_block_idx=col_idx
        )
        
        num_assigned += len(output_blocks)
```

### 4.3 原理分析

Down 投影的计算涉及一个维度变换：
- 输入：\([1, \text{intermediate\_size}]\)
- 输出：\([1, \text{hidden\_size}]\)

通常 \(\text{intermediate\_size} > \text{hidden\_size}\)（例如，Llama-2-7B 的 intermediate_size 是 hidden_size 的 4 倍）。

为了并行化，我们可以将输入维度按列切分：

\[ \text{input} = [\text{input}^{(0)}, \text{input}^{(1)}, ..., \text{input}^{(C-1)}] \]

其中每列的宽度为 \(\text{hidden\_size}\)，\(C = \text{intermediate\_size} / \text{hidden\_size}\)。

每个部分计算为：

\[ \text{partial\_output}^{(c)} = \text{input}^{(c)} (W_d^{(c)})^T \]

最终的输出需要对各部分求和：

\[ \text{output} = \sum_{c=0}^{C-1} \text{partial\_output}^{(c)} \]

Megakernels 的策略是为每个 SM 分配同一列内的多个输出 block，这样可以：
1. **减少归约开销**：每个 SM 只需对单个列的部分结果求和
2. **负载均衡**：将所有任务尽可能均匀分配给各 SM

### 4.4 代码实践

Down 投影调度实现在 `schedule_downproj` 函数中：

[https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L167-L213](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L167-L213)

这段代码实现了复杂的二维任务分配：
- **第 170-171 行**：计算输出 block 数量和列切分数量
- **第 174-177 行**：生成所有任务，每个任务由 (列索引, 输出block索引) 组成
- **第 180-211 行**：将任务分配给 SM，确保每个 SM 的任务属于同一列
  - **第 189-190 行**：获取当前任务的列索引
  - **第 190-200 行**：筛选出该列的所有任务
  - **第 202-209 行**：创建 `DownProjResidual` 指令，指定输出 block 范围和归约 block 索引

### 4.5 练习题

1. 假设 `hidden_size` 为 4096，`intermediate_size` 为 16384，`down_proj_block_size` 为 16。请计算 `num_col_splits` 和总任务数量。

2. 为什么 DownProj 调度要确保每个 SM 的任务属于同一列？如果一个 SM 处理多个列的任务会有什么问题？

3. 在代码中，为什么第 189-190 行要先获取 `col_idx`，然后在第 190 行只保留该列的任务？

### 4.6 答案

1. **解答**：
   - Block 数量 = \(4096 / 16 = 256\)
   - 列切分数量 = \(16384 / 4096 = 4\)
   - 总任务数量 = \(4 \times 256 = 1024\)

2. **解答**：
   - **减少同步**：如果每个 SM 只处理一列，则无需在 SM 内部进行跨列的归约
   - **简化逻辑**：`DownProjResidual` 指令只需指定一个 `reduction_block_idx`（列索引）
   - **内存访问**：同一列的任务访问权重矩阵的连续区域，有利于缓存

   如果一个 SM 处理多列，则需要在 SM 内部对多列的部分结果求和，增加复杂度和延迟。

3. **解答**：
   - **确保任务连续性**：第 189 行获取的是当前批次的第一个任务的列索引
   - **过滤任务**：第 190 行只保留属于该列的任务，丢弃其他列的任务
   - **保证 correctness**：这样确保每个 `DownProjResidual` 指令只处理一个列的部分结果，归约逻辑正确

   剩下的其他列任务会在后续 SM 分配中被处理。

---

## 5. LM Head 调度

### 5.1 概念说明

语言模型头（LM Head）是 Transformer 模型的最后一层，将最终的隐藏状态映射到词表大小的 logits：

\[ \text{logits} = x W_{\text{lm\_head}} \]

其中 \(x \in \mathbb{R}^{1 \times \text{hidden\_size}}\)，\(W_{\text{lm\_head}} \in \mathbb{R}^{\text{vocab\_size} \times \text{hidden\_size}}\)。

在延迟优化场景下，LM Head 的计算特点与 QKV 类似：输出维度很大（词表大小通常为 32k 到 256k），需要并行执行。

### 5.2 伪代码流程

```python
def schedule_lm_head(vocab_size, hidden_size, sm_count, block_size):
    num_blocks = vocab_size // block_size
    blocks_per_sm = num_blocks / sm_count
    
    for sm_idx in range(sm_count):
        start = round(sm_idx * blocks_per_sm)
        end = round((sm_idx + 1) * blocks_per_sm)
        yield LMHeadInstruction(start, end)
```

### 5.3 原理分析

LM Head 的计算是标准的矩阵-向量乘法：

\[ \text{logits}[i] = x W_{\text{lm\_head}}[i, :]^T \]

通过按输出维度（词表维度）切分，每个 SM 计算部分 logits：

\[ \text{logits}[start \times \text{block\_size} : end \times \text{block\_size}] = x W_{\text{lm\_head}}[start \times \text{block\_size} : end \times \text{block\_size}, :]^T \]

这种切分方式与 QKV 完全一致，都是：
1. **连续分配**：每个 SM 处理连续的输出区间
2. **负载均衡**：每个 SM 的计算量相近

### 5.4 代码实践

LM Head 调度实现在 `schedule_lm_head` 函数中：

[https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L216-L232](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L216-L232)

这段代码与 QKV 调度几乎一致：
- **第 219 行**：计算词表大小的 block 数量
- **第 222 行**：获取 SM 数量
- **第 225-230 行**：为每个 SM 分配连续的 block 区间，创建 `RMS_LM_Head` 指令

`RMS_LM_Head` 指令执行 RMS 归一化和 LM Head 矩阵-向量乘法，输出最终的 logits。

### 5.5 练习题

1. 假设词表大小为 32000，`lm_head_block_size` 为 16，SM 数量为 80。请计算每个 SM 处理多少个 block？

2. 为什么 LM Head 的调度策略与 QKV 完全一致？有什么共同特点？

3. 在延迟优化场景下，LM Head 的计算通常不会成为瓶颈，为什么？

### 5.6 答案

1. **解答**：
   - Block 数量 = \(32000 / 16 = 2000\)
   - 每个 SM 的 block 数 = \(2000 / 80 = 25\)

2. **解答**：
   - **共同点**：
     - 都是矩阵-向量乘法
     - 输出维度远大于输入维度
     - 计算模式完全相同：\( \text{output} = \text{input} \times W^T \)
   - 因此，调度策略可以复用，都采用连续分配的方式。

3. **解答**：
   - **只执行一次**：LM Head 只在最后一层执行一次，不像其他层需要堆叠多次
   - **相对较小**：虽然词表维度大，但相对于多层注意力/MLP 的累积计算量，LM Head 的占比较小
   - **内存带宽友好**：权重矩阵可以缓存在 GPU 内存中，访问模式相对简单

   因此，在延迟优化场景下，优化注意力层和 MLP 层通常比优化 LM Head 更关键。

---

## 总结

本讲义介绍了 Megakernels 在延迟优化场景下的调度策略，涵盖五个关键模块：

1. **QKV 并行调度**：按输出维度连续分配，每个 SM 处理 QKV 投影的一部分
2. **注意力分区**：将序列长度划分为多个分区，每个 SM 并行计算分区内的注意力
3. **UpGate 并行化**：采用循环分配策略，每个 SM 间隔处理 Up/Gate 投影的 block
4. **DownProj 调度**：二维任务分配，确保每个 SM 处理同一列内的输出 block
5. **LM Head 调度**：与 QKV 类似，按词表维度连续分配

这些调度策略的核心思想是：
- **任务拆分**：将大任务拆分为多个小任务，分配给多个 SM 并行执行
- **负载均衡**：确保每个 SM 的计算量相近
- **内存优化**：选择合适的分配策略（连续 vs 循环），优化内存访问模式

通过精心设计的调度策略，Megakernels 能够充分利用 GPU 的并行计算能力，显著降低单 token 推理的延迟。

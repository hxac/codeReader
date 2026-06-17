# U6-L1: 注意力计算 CUDA 实现

## 概述

本讲义深入分析 Megakernels 项目中注意力计算的 CUDA 实现。注意力机制是 Transformer 模型的核心组件，在推理过程中计算开销最大。Megakernels 通过将注意力计算分解为 **Partial** 和 **Reduction** 两个阶段，充分利用 GPU 的并行计算能力，实现了低延迟的 LLM 推理。

我们将依次分析：
1. Partial Attention 阶段：计算部分注意力结果
2. Attention Reduction 阶段：合并多个 partial 结果
3. GQA（Grouped Query Attention）支持
4. 共享内存优化策略
5. Warp 级协作机制

---

## 模块 1: Partial Attention 实现

### 1.1 概念说明

Partial Attention 是注意力计算的第一阶段，负责计算查询（Q）与键值对（K、V）的注意力分数，并生成部分结果。为了高效利用 GPU 的并行计算能力，Megakernels 将序列分成多个 **block**（每个 block 包含 16 个 token），每个 SM（Streaming Multiprocessor）负责处理一部分 block。

**为什么需要 Partial 阶段？**
- **并行度提升**：多个 SM 可以同时处理不同的序列 block，充分利用 GPU 的 132 个 SM（H100）
- **内存局部性**：分块处理可以更好地利用共享内存和片上缓存
- **流水线重叠**：计算和数据传输可以重叠执行

### 1.2 伪代码流程

```
输入: Q (查询向量), K_cache (键缓存), V_cache (值缓存)
输出: O_partial (部分输出), LSE (对数空间指数)

# 初始化
max_vec = -∞
norm_vec = 0
O = 0

# 遍历所有 KV block
for block_idx in assigned_blocks:
    # 1. 加载 K 和 V 到共享内存
    K = load_from_cache(K_cache, block_idx)
    V = load_from_cache(V_cache, block_idx)

    # 2. 计算 Q @ K^T（注意力分数）
    attn = Q @ K.T

    # 3. 位置掩码（处理最后一个 block 的填充）
    if is_last_block(block_idx):
        attn = mask_padding(attn, seq_len)

    # 4. 计算 softmax（online softmax）
    max_vec_new = max(max_vec, max(attn))
    attn = exp(attn / scale - max_vec_new)
    norm_vec = norm_vec * exp(max_vec - max_vec_new) + sum(attn)

    # 5. 计算 O = O + attn @ V
    O = O * exp(max_vec - max_vec_new) + attn @ V
    max_vec = max_vec_new

# 6. 归一化输出
O = O / norm_vec
LSE = log(norm_vec) + max_vec

返回 O, LSE
```

### 1.3 原理分析

#### Online Softmax 算法

标准的 softmax 需要两次遍历：第一次计算最大值，第二次计算指数和归一化。为了高效处理流式数据，Partial Attention 使用 **Online Softmax**（也称为递归 softmax），可以单次遍历完成计算。

给定注意力分数 \(a_i\) 和当前的累积最大值 \(m_{i-1}\)、归一化因子 \(d_{i-1}\)，新 block 的更新规则为：

\[
\begin{aligned}
m_i &= \max(m_{i-1}, \max(a_i)) \\
d_i &= d_{i-1} \cdot 2^{m_{i-1} - m_i} + \sum_j 2^{a_{i,j} / \tau - m_i} \\
O_i &= O_{i-1} \cdot 2^{m_{i-1} - m_i} + \text{softmax}(a_i) \cdot V_i
\end{aligned}
\]

其中 \(\tau = \sqrt{d_k}\) 是缩放因子，\(d_k\) 是 head 维度（64）。

**关键观察**：
- 指数运算使用 \(2^x\) 而非 \(e^x\)，因为 GPU 的 `exp2` 指令比 `exp` 更快
- 使用对数空间（Log-Sum-Exp）避免数值溢出
- 每次更新只需要 \(O(1)\) 状态（\(m\) 和 \(d\)）

#### 数据流和控制流

```
Launcher Warp (lane 0)
    ↓
  分配任务（计算哪些 blocks）
    ↓
  发起 TMA 加载 K、V（异步）
    ↓
Consumer Warp (warp 0)
    ↓
  等待 Q 就绪
    ↓
  流水线处理每个 block:
    - 等待 K 到达
    - 计算 Q @ K^T
    - 等待 V 到达
    - 计算 softmax
    - 计算 O = attn @ V
    ↓
  存储 O、LSE 到全局内存
```

### 1.4 代码实践

#### 核心数据结构定义

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L17-L37

这段代码定义了寄存器纹理（`rt`）和共享内存（`st`）的类型别名，用于高效存储和计算 Q、K、V、O 和注意力分数。

- `q_rt` / `q_st`：查询向量（16×64，但只用 4 行）
- `k_rt` / `kv_st`：键向量（16×64）
- `v_rt`：值向量（16×64，列主序布局）
- `attn_fl_rt`：注意力分数（16×16，浮点型）
- `max_vec_rv`：每行的最大值（用于 online softmax）
- `l_sv`：对数空间指数（Log-Sum-Exp）

#### Consumer 核心计算循环

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L463-L520

这是 Partial Attention 的核心计算循环，实现了完整的 online softmax 和矩阵乘法。

**关键步骤**：

1. **等待 K 到达并计算 Q @ K^T**（第 470-476 行）：
   ```cpp
   kittens::warp::wait(K_arrived(s, stage), (i / NUM_STAGES) % 2);
   kittens::warp::load(K_reg, K_smem);
   kittens::warp::mma_ABt(attn_fl_reg, Q_reg, K_reg, attn_fl_reg);
   ```

2. **位置掩码**（第 478-482 行）：处理最后一个 block 的填充位置

3. **计算最大值**（第 485-486 行）：
   ```cpp
   kittens::warp::row_max(max_vec_reg, attn_fl_reg, max_vec_reg);
   ```

4. **Online Softmax 更新**（第 488-516 行）：
   - 缩放注意力分数和最大值（第 489-490 行）
   - 计算 \(2^{\text{attn} - \text{max}}\)（第 493-494 行）
   - 更新归一化因子（第 497-500 行）
   - 更新输出 O（第 503-511 行）

5. **归一化最终输出**（第 527-531 行）：
   ```cpp
   kittens::warp::div_row(O_reg, O_reg, norm_vec_reg);
   kittens::warp::log2(L_reg, norm_vec_reg);
   kittens::warp::add(L_reg, L_reg, last_scaled_max_vec_reg);
   ```

#### TMA 异步加载 K 和 V

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L373-L382

使用 Tensor Memory Accelerator（TMA）异步加载 K 和 V 到共享内存，实现计算和数据传输的重叠。

### 1.5 练习题

**练习 1**：为什么 Online Softmax 使用 \(2^x\) 而不是 \(e^x\)？

**练习 2**：假设序列长度为 100，KV block size 为 16，有多少个 block？最后一个 block 有多少个有效位置？

**练习 3**：在第 489-490 行的代码中，为什么要同时缩放 `attn_fl_reg` 和 `max_vec_reg`？

**练习 4**：如果 `norm_vec_reg` 在某个迭代中变成了 0，会发生什么？如何避免这种情况？

### 1.6 答案

**答案 1**：GPU 提供硬件指令 `exp2`（计算 \(2^x\)），其延迟比 `exp`（计算 \(e^x\)）更低。两者可以通过换底公式互相转换：\(e^x = 2^{x / \ln(2)}\)，其中 \(\ln(2) \approx 0.693\)。

**答案 2**：
- 总 block 数 = \(\lceil 100 / 16 \rceil = 7\)
- 最后一个 block 的有效位置 = \(100 \mod 16 = 4\)

**答案 3**：
- `attn_fl_reg` 需要缩放以应用 softmax 的温度系数
- `max_vec_reg` 需要同步缩放，因为它参与后续的指数运算（\(2^{\text{attn} - \text{max}}\)）
- 缩放因子 \(1.4427 \approx 1 / \ln(2)\) 将 \(\sqrt{d_k}\) 缩放转换为 \(\log_2\) 基底

**答案 4**：
- 如果 `norm_vec_reg` 为 0，说明所有注意力分数都是 \(-\infty\)（可能由掩码导致），最终除法会产生 NaN
- 代码通过 `right_fill` 将无效位置填充为一个非常负的数（`-999999999999.f`），而非真正的 \(-\infty\)，避免了这种情况
- 在实际应用中，序列长度至少为 1，所以至少有一个有效位置

---

## 模块 2: Attention Reduction

### 2.1 概念说明

Attention Reduction 是注意力计算的第二阶段，负责合并多个 SM 生成的 **Partial 结果**。每个 SM 计算了部分序列的注意力输出，现在需要将这些部分结果合并成最终的完整输出。

**为什么需要 Reduction 阶段？**
- **结果完整性**：每个 SM 只计算了部分序列，需要合并所有 partial 结果
- **数值稳定性**：直接合并 softmax 结果可能导致数值溢出，需要特殊处理
- **GQA 支持**：多个 Q heads 共享相同的 KV heads，需要聚合它们的 partial 结果

### 2.2 伪代码流程

```
输入: O_partial[i], LSE_partial[i] (i = 0..num_partials-1)
输出: O_final (最终输出)

# 初始化
O_accumulated = 0
LSE_accumulated = -∞

# 递归合并所有 partial 结果
for i in 0..num_partials-1:
    # 1. 加载当前 partial 结果
    O_current = O_partial[i]
    LSE_current = LSE_partial[i]

    # 2. 计算 Log-Sum-Exp 合并
    max_lse = max(LSE_accumulated, LSE_current)

    # 3. 计算权重
    accumulated_exp = 2^(LSE_accumulated - max_lse)
    current_exp = 2^(LSE_current - max_lse)
    new_denom = accumulated_exp + current_exp

    accumulated_scale = accumulated_exp / new_denom
    current_scale = current_exp / new_denom

    # 4. 加权合并输出
    O_accumulated = O_accumulated * accumulated_scale + O_current * current_scale

    # 5. 更新 LSE
    LSE_accumulated = max_lse + log2(new_denom)

返回 O_accumulated
```

### 2.3 原理分析

#### Log-Sum-Exp 合并

Partial Attention 的输出是已经归一化的结果：
\[
O_i = \frac{\sum_j \exp(a_{i,j} / \tau) \cdot V_j}{\sum_j \exp(a_{i,j} / \tau)}
\]

对应的 LSE（对数空间指数）为：
\[
\text{LSE}_i = \log\left(\sum_j \exp(a_{i,j} / \tau)\right)
\]

合并两个 partial 结果时，需要保持 softmax 的归一化性质。给定两个 partial 结果 \(O_1, O_2\) 和对应的 LSE \(l_1, l_2\)：

\[
\begin{aligned}
m &= \max(l_1, l_2) \\
w_1 &= \frac{2^{l_1 - m}}{2^{l_1 - m} + 2^{l_2 - m}} \\
w_2 &= \frac{2^{l_2 - m}}{2^{l_1 - m} + 2^{l_2 - m}} \\
O_{\text{final}} &= w_1 \cdot O_1 + w_2 \cdot O_2 \\
l_{\text{final}} &= m + \log_2(2^{l_1 - m} + 2^{l_2 - m})
\end{aligned}
\]

**关键观察**：
- 合并结果是两个 partial 结果的**凸组合**（权重和为 1）
- LSE 的更新保证了对数空间的归一化
- 使用 \(2^x\) 而非 \(e^x\) 以利用 GPU 的 `exp2` 指令

#### Reduction Pipeline

```
Launcher Warp
    ↓
  等待所有 Partial Attention 完成
    ↓
  发起 TMA 加载所有 O_partial 和 LSE_partial
    ↓
Consumer Warps (4 warps, 每个 head 一个)
    ↓
  每个 warp 独立处理一个 head:
    for partial in partials:
      加载 O_partial[head], LSE_partial[head]
      合并到 accumulated_out, accumulated_lse
    ↓
  存储最终 O_final 到全局内存
```

### 2.4 代码实践

#### Reduction 数据结构

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_reduction.cu#L13-L16

定义了用于 Reduction 的向量类型：
- `l_partial_sv`：存储所有 partial 的 LSE（长度为 `MAX_ATTN_PARTIALS`，即 SM 数量）
- `o_sv`：单个 partial 的输出向量（64 维）
- `o_rv`：寄存器版本的输出向量
- `o_final_sv`：最终的合并输出（BF16 格式）

#### Consumer Reduction 核心逻辑

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_reduction.cu#L253-L290

这是 Reduction 阶段的核心计算循环，实现了 Log-Sum-Exp 合并算法。

**关键步骤**：

1. **初始化累加器**（第 239-245 行）：
   ```cpp
   o_rv accumulated_out;
   float accumulated_lse = -INFINITY;
   o_rv current_out;
   float current_lse;
   kittens::warp::zero(accumulated_out);
   ```

2. **遍历所有 partial 结果**（第 253-290 行）：
   - 等待 O_partial 到达（第 255-256 行）
   - 从共享内存加载 LSE（第 263-266 行）
   - 加载 O_partial（第 268 行）

3. **Log-Sum-Exp 合并**（第 270-286 行）：
   ```cpp
   float max_lse = max(accumulated_lse, current_lse);

   float accumulated_exp = exp2f(accumulated_lse - max_lse);
   float current_exp = exp2f(current_lse - max_lse);

   float new_denom = accumulated_exp + current_exp;

   float accumulated_scale = accumulated_exp / new_denom;
   float current_scale = current_exp / new_denom;

   kittens::warp::mul(accumulated_out, accumulated_out, accumulated_scale);
   kittens::warp::mul(current_out, current_out, current_scale);
   kittens::warp::add(accumulated_out, accumulated_out, current_out);

   accumulated_lse = max_lse + log2f(new_denom);
   ```

4. **存储最终结果**（第 293-298 行）：
   ```cpp
   o_final_sv &O_final_smem = get_O_final_smem(s, q_head_local_idx);
   kittens::warp::store(O_final_smem, accumulated_out);
   kittens::warp::sync();
   kittens::warp::arrive(final_O_ready(s, q_head_local_idx));
   ```

#### Launcher 等待和加载逻辑

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_reduction.cu#L183-L227

Launcher 负责等待所有 Partial Attention 完成，然后异步加载所有 partial 结果。

**关键逻辑**：
- 等待 4 个 Q heads 的所有 partial 完成（第 184-197 行）
- 使用 TMA 异步加载 LSE（第 201-207 行）
- 使用流水线加载 O_partial（第 209-226 行）

### 2.5 练习题

**练习 1**：假设有 3 个 partial 结果，它们的 LSE 分别是 10.0、12.0、11.0，对应的 O 分别是 [1, 2]、[3, 4]、[5, 6]，请计算合并后的 LSE 和 O。

**练习 2**：为什么 Reduction 阶段使用 `exp2f` 和 `log2f` 而不是 `expf` 和 `logf`？

**练习 3**：在第 270-286 行的代码中，如果 `accumulated_lse` 和 `current_lse` 都是 `-INFINITY`，会发生什么？

**练习 4**：代码中使用了双阶段流水线（`NUM_STAGES = 2`），这样做的优势是什么？

### 2.6 答案

**答案 1**：
- 合并前两个 partial：
  - \(m = \max(10.0, 12.0) = 12.0\)
  - \(w_1 = 2^{10-12} / (2^{10-12} + 2^{12-12}) = 0.2\)
  - \(w_2 = 2^{12-12} / (2^{10-12} + 2^{12-12}) = 0.8\)
  - \(O_{12} = 0.2 \cdot [1, 2] + 0.8 \cdot [3, 4] = [2.6, 3.6]\)
  - \(l_{12} = 12.0 + \log_2(2^{10-12} + 2^{12-12}) = 12.0 + \log_2(1.25) \approx 12.322\)

- 合并第三个 partial：
  - \(m = \max(12.322, 11.0) = 12.322\)
  - \(w_{12} = 2^{12.322-12.322} / (2^{12.322-12.322} + 2^{11-12.322}) \approx 0.712\)
  - \(w_3 = 2^{11-12.322} / (2^{12.322-12.322} + 2^{11-12.322}) \approx 0.288\)
  - \(O_{\text{final}} = 0.712 \cdot [2.6, 3.6] + 0.288 \cdot [5, 6] \approx [3.38, 4.28]\)
  - \(l_{\text{final}} = 12.322 + \log_2(1 + 2^{11-12.322}) \approx 12.48\)

**答案 2**：
- GPU 的 `exp2f` 和 `log2f` 指令比 `expf` 和 `logf` 更快
- 与 Partial Attention 保持一致，统一使用 \(\log_2\) 基底

**答案 3**：
- 如果两者都是 `-INFINITY`，则 `max_lse = -INFINITY`
- `exp2f(-INFINITY - (-INFINITY))` 返回 `exp2f(NaN)` = NaN
- 这会导致后续计算全部出错
- 实际上这种情况不会发生，因为每个 partial 至少处理一个 token，LSE 不会是 `-INFINITY`
- 代码在 Partial Attention 阶段通过 `right_fill` 确保至少有一个有效位置

**答案 4**：
- 双阶段流水线允许在处理当前 partial 的同时，预加载下一个 partial 的数据
- 这样可以隐藏内存延迟，提高吞吐量
- 代码第 215-218 行的 `if (i >= NUM_STAGES)` 等待逻辑确保了流水线正确性

---

## 模块 3: GQA 支持

### 3.1 概念说明

**Grouped Query Attention（GQA）** 是一种注意力机制优化，多个查询头（Q heads）共享相同的键值头（KV heads）。在 Megakernels 的 Llama-1B 配置中：
- Q heads：32 个
- KV heads：8 个
- GQA ratio：32 / 8 = 4（即每 4 个 Q heads 共享一组 KV heads）

**为什么使用 GQA？**
- **减少内存占用**：KV cache 的大小减少了 4 倍（从 32 个 head 降到 8 个）
- **提高缓存利用率**：更少的 KV heads 意味着更好的 L2 缓存命中率
- **平衡性能和精度**：相比 Multi-Query Attention（MQA，所有 Q heads 共享 1 个 KV head），GQA 在精度和效率之间取得了更好的平衡

### 3.2 伪代码流程

```
输入: Q_heads[32], KV_heads[8]
输出: attn_out[32]

# 每个 KV head 服务 4 个 Q heads
for kv_head_idx in 0..7:
    # 1. 确定共享这组 KV 的 Q head 范围
    q_head_start = kv_head_idx * 4
    q_heads = Q_heads[q_head_start : q_head_start + 4]

    # 2. 并行计算 4 个 Q heads 的注意力
    for head_offset in 0..3:
        q_head_idx = q_head_start + head_offset
        Q = q_heads[head_offset]
        K, V = KV_heads[kv_head_idx]

        # 计算 partial attention
        O_partial[head_offset], LSE_partial[head_offset] =
            partial_attention(Q, K, V)

    # 3. 将 4 个 Q heads 的结果存储到全局内存
    for head_offset in 0..3:
        attn_out[q_head_start + head_offset] = O_partial[head_offset]
        LSE_intermediates[q_head_start + head_offset] = LSE_partial[head_offset]
```

### 3.3 原理分析

#### GQA 的内存布局

在 GQA 配置下，KV cache 的形状是 `[num_layers, seq_len, num_kv_heads, head_dim]`，而 Q cache 的形状是 `[num_layers, seq_len, num_attention_heads, head_dim]`。

对于 Llama-1B：
- KV cache：`[16, seq_len, 8, 64]`
- Q cache：`[16, seq_len, 32, 64]`

当计算注意力时，每 4 个 Q heads（索引 0-3, 4-7, ..., 28-31）共享同一组 KV heads。

#### Partial Attention 中的 GQA 处理

在 Partial Attention 阶段：
1. 每个指令处理 **1 个 KV head** 和对应的 **4 个 Q heads**
2. 单个 warp 加载 4 个 Q heads 到寄存器（每个 Q head 是 4×64，共 16×64 = 1024 个元素）
3. 流水线处理 KV blocks，每个 block 的 K、V 被所有 4 个 Q heads 共享

在 Reduction 阶段：
1. 每个指令处理 **4 个 Q heads**（同一个 KV head 组）
2. 4 个 warps 并行处理，每个 warp 负责一个 Q head
3. 每个 warp 独立合并所有 partial 结果

#### 数据流图

```
KV Cache (8 heads)
    ↓
  Partial Attention (launcher)
    ↓
  SM 0: processes KV[0], generates Q[0-3]'s partials
  SM 1: processes KV[1], generates Q[4-7]'s partials
  ...
  SM 7: processes KV[7], generates Q[28-31]'s partials
    ↓
  Intermediates (32 heads × num_partials × 64)
    ↓
  Reduction (4 warps per instruction)
    ↓
  Warp 0: processes Q[0], merges all partials
  Warp 1: processes Q[1], merges all partials
  ...
  Warp 3: processes Q[3], merges all partials
    ↓
  Final Output (32 heads × 64)
```

### 3.4 代码实践

#### GQA Ratio 定义和使用

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L9-L14

定义 GQA ratio 为 4，并使用静态断言确保配置正确。

#### Consumer 中加载 4 个 Q heads

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L393-L407

Consumer warp 等待 4 个 Q heads 的准备完成（每个 Q head 对应 `OPCODE_RMS_QKV_MatVecRopeAppend` 操作的 4 个输出）。

#### Storer 中存储 4 个 heads 的结果

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L562-L580

将 4 个 Q heads 的结果转换为 BF16 格式并使用 TMA 存储到全局内存。

#### Reduction 中的 4-warp 并行

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_reduction.cu#L234-L237

每个指令启动 4 个 consumer warps，每个 warp 处理一个 Q head（在同一个 KV head 组内）。

### 3.5 练习题

**练习 1**：假设有 32 个 Q heads 和 8 个 KV heads，索引为 15 的 Q head 使用哪个 KV head？

**练习 2**：在 Partial Attention 的 consumer 中，为什么只使用 1 个 warp（warp 0）来处理 4 个 Q heads？

**练习 3**：代码中 `store_4_rows` 函数的作用是什么？为什么需要这个函数？

**练习 4**：如果把 GQA ratio 改成 8（即 4 个 KV heads），代码的哪些部分需要修改？

### 3.6 答案

**答案 1**：
- Q head 15 属于第 \(15 / 4 = 3\) 组（索引从 0 开始）
- 对应的 KV head 索引是 3

**答案 2**：
- 4 个 Q heads 共享同一组 KV heads，它们的计算流程完全相同
- 使用单个 warp 可以充分利用寄存器和共享内存
- 单个 warp 的 32 个线程足够处理 4×64 = 256 个元素（每个线程处理 8 个元素）
- 使用 1 个 warp 而非 4 个 warps 可以减少同步开销和寄存器压力

**答案 3**：
- `store_4_rows` 函数将寄存器中的 16×64 数据存储到共享内存中的 4 个 64 维向量
- 寄存器布局和共享内存布局不同：
  - 寄存器：16 行 × 64 列（每个线程存储 4 个连续元素）
  - 共享内存：4 个独立的 64 维向量（banked 布局，避免 bank conflicts）
- 需要重新排列数据以优化后续的 TMA 存储操作

**答案 4**：
- 修改 `GQA_RATIO` 定义（第 9-10 行）
- 调整 `store_4_rows` 函数，处理 8 行而非 4 行
- 修改 Reduction 的 `Q_HEADS_PER_INSTRUCTION`（第 9 行）
- 调整信号量数量和共享内存布局
- 修改 `load_Q_async` 函数，处理 8 个 Q heads 的加载

---

## 模块 4: 共享内存优化

### 4.1 概念说明

共享内存（Shared Memory）是 GPU 片上的高速内存， latency 约为 30 个时钟周期，比全局内存（~400+ 时钟周期）低一个数量级。Megakernels 大量使用共享内存来缓存频繁访问的数据，减少全局内存访问。

**共享内存的关键作用**：
- **缓存 Q、K、V**：减少重复的全局内存访问
- **Partial 结果缓冲**：存储中间计算结果
- **Warp 间通信**：通过共享内存在不同 warp 间传递数据

### 4.2 伪代码流程

```
# 共享内存布局（Partial Attention）
Shared Memory = [
    Q_smem: 16×64 (bf16),           # 4 个 Q heads
    O_smem[4]: 4×64 (float),        # 4 个 heads 的输出
    L_smem: 16 (float),             # 4 个 heads 的 LSE
]

# 共享内存布局（Reduction）
Shared Memory = [
    对于每个 Q head (0..3):
      L_partial_smem: MAX_PARTIALS (float)  # 所有 partial 的 LSE
      O_partial_smem[2]: 2×64 (float)       # 2 个阶段的 O partial
      O_final_smem: 64 (bf16)                # 最终输出
]

# 使用流程
1. Launcher 发起 TMA 加载到共享内存
2. Consumer 等待数据到达（通过信号量）
3. Consumer 从共享内存加载数据到寄存器
4. Consumer 计算完成后，将结果写回共享内存
5. Storer 从共享内存读取并使用 TMA 存储到全局内存
```

### 4.3 原理分析

#### 共享内存分页（Memory Paging）

Megakernels 使用 **分页机制** 管理共享内存，每个"页"（page）是一个独立的共享内存块，由不同的角色访问。

**Partial Attention 的页布局**：
- **QOL Page**（Query-Output-LSE 页）：存储 Q、O、L
- **KV Page**（Key-Value 页）：存储流水线的 K 和 V（多个 stage）

**Reduction 的页布局**：
- **Shared Data Page**：存储 L_partial、O_partial、O_final

#### 数据布局和 Bank Conflict 避免

共享内存被分成 32 个 bank（每个 bank 4 字节宽），如果多个线程访问同一 bank 的不同地址，会发生 **bank conflict**，导致串行化访问。

**Megakernels 的布局策略**：
- 使用 `kittens` 库的 `sv`（Shared Vector）和 `st`（Shared Tile）抽象，自动优化布局
- 对于 `sv_fl<64>`，64 个浮点数分布在 32 个 bank 上，避免 32-way conflict
- 使用 padding 和重排列来打破访问模式

#### 信号量同步

使用信号量（semaphore）来同步生产者（launcher）和消费者（consumer）：

```cpp
// Launcher: 发起异步加载
kittens::tma::expect(K_arrived(s, stage), K_smem);
kittens::tma::load_async(K_smem, g.k_cache, ..., K_arrived(s, stage));

// Consumer: 等待数据到达
kittens::warp::wait(K_arrived(s, stage), phase);

// Consumer: 完成后通知
kittens::warp::arrive(K_finished(s, stage));
```

### 4.4 代码实践

#### Partial Attention 的共享内存访问函数

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L94-L120

定义了访问 QOL page 和 KV page 中各个数据结构的辅助函数。

**关键函数**：
- `get_Q_smem`：获取 Q 的共享内存引用
- `get_O_smem`：获取 4 个 heads 的输出数组
- `get_L_smem`：获取 LSE 的共享内存引用
- `get_K_smem` / `get_V_smem`：获取流水线中各 stage 的 K、V

#### Reduction 的共享内存布局计算

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_reduction.cu#L105-L134

计算每个 head 的共享内存大小，并定义访问函数。

**布局**：
- 每个 head 占用 `sizeof(l_partial_sv) + NUM_STAGES * sizeof(o_sv) + sizeof(o_final_sv)`
- 4 个 heads 总共占用 `Q_HEADS_PER_INSTRUCTION * size_per_head`

#### 信号量管理

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L56-L78

定义了访问信号量的辅助函数，用于同步 TMA 加载完成事件。

**信号量类型**：
- `Q_arrived` / `O_arrived` / `L_arrived`：Q、O、L 数据到达信号
- `K_arrived` / `V_arrived`：K、V 数据到达信号（每个 stage 一个）
- `K_finished` / `V_finished`：K、V 处理完成信号（每个 stage 一个）

### 4.5 练习题

**练习 1**：假设共享内存大小为 128 KB，Partial Attention 的 KV page 需要多少空间？（假设 NUM_STAGES = 3）

**练习 2**：为什么 Reduction 阶段只需要 2 个 stage 的 O_partial 缓冲？

**练习 3**：代码中使用 `static_cast<uint32_t>(__cvta_generic_to_shared(...))` 将共享内存指针转换为整数，为什么需要这样做？

**练习 4**：如果 32 个线程同时访问共享内存的不同地址，如何避免 bank conflict？

### 4.6 答案

**答案 1**：
- 每个 K 或 V tile：`16 × 64 × sizeof(bf16) = 16 × 64 × 2 = 2048` 字节
- 3 个 stages × 2（K 和 V）= 6 个 tiles
- 总空间：`6 × 2048 = 12,288` 字节 = 12 KB

**答案 2**：
- Reduction 使用双缓冲（2 个 stages）来实现流水线
- 当一个 stage 的 O_partial 正在被合并时，下一个 stage 的数据可以被异步加载
- 这样可以隐藏内存延迟，不需要为所有 partial 分配缓冲（只需 2 个）

**答案 3**：
- CUDA 的内联汇编（`asm volatile`）需要立即数或寄存器操作数
- `__cvta_generic_to_shared` 将 64 位虚拟地址转换为 32 位共享内存物理地址
- 转换后的整数可以直接用于 `cp.async` 或 `ld.shared` 指令

**答案 4**：
- 确保访问地址跨步为 32 个 bank 的倍数 + 偏移
- 例如，对于 32 个线程，线程 \(i\) 访问地址 \(i \times 8\)（每线程 8 字节）会产生 32-way conflict
- 使用 `kittens` 的 `sv` 类型，它自动添加 padding 来打破冲突模式
- 或者在编译时使用 `__shared__` 内存的对齐属性

---

## 模块 5: Warp 协作

### 5.1 概念说明

**Warp** 是 CUDA 执行的基本单位，由 32 个线程组成，所有线程执行相同的指令（SIMT：单指令多线程）。Warp 内的线程可以通过 **warp-level 原语** 进行高效通信和同步。

**Warp 协作的关键优势**：
- **寄存器共享**：warp 内的所有线程可以访问同一个寄存器文件的不同部分
- **快速同步**：`__syncwarp()` 或隐式同步比全局 `__syncthreads()` 快得多
- **warp shuffle**：线程间可以直接交换数据，无需经过共享内存

### 5.2 伪代码流程

```
# Warp 内的数据分配（16×64 矩阵）
每个线程负责一个 4×4 的子块：
  Thread 0:  行 0-3,  列 0-3
  Thread 1:  行 0-3,  列 4-7
  ...
  Thread 31: 行 12-15, 列 60-63

# 矩阵乘法 C = A @ B^T（warp-level MMA）
1. 加载 A 的行向量（16×64）到寄存器
2. 加载 B 的列向量（16×64）到寄存器
3. 执行 warp-level MMA：每个线程计算一个 4×4 的结果块
4. 使用 warp shuffle 累加部分和

# Online Softmax 的 warp 协作
1. 每个线程计算自己负责元素的最大值
2. 使用 warp::row_max 在 16 个线程间归约最大值
3. 广播最大值到所有线程
4. 每个线程独立计算指数和归一化
```

### 5.3 原理分析

#### Warp-Level Matrix Multiply-Accumulate (MMA)

Megakernels 使用 `kittens::warp::mma_ABt` 来计算 \(Q \times K^T\)，这是一个 16×64 和 16×64 的矩阵乘法。

**寄存器布局**：
- `q_rt`：16×64 寄存器矩阵，每个线程存储 4×4 的子块
- `k_rt`：16×64 寄存器矩阵，每个线程存储 4×4 的子块
- `attn_fl_rt`：16×16 输出矩阵，每个线程存储 1×1 的标量（实际上是 4×4 的子块，通过累加）

**MMA 执行流程**：
1. 每个线程从 `Q_reg` 加载自己的 4×4 子块
2. 每个线程从 `K_reg` 加载对应的 4×4 子块（转置访问）
3. 执行 Tensor Core MMA（如果是 Ampere 或更新架构）
4. 累加到 `attn_reg` 中

#### Warp-Level 归约（Reduction）

对于 Online Softmax 的最大值计算和求和，需要在线程间进行归约操作。

**最大值归约**（`warp::row_max`）：
```
输入: 16 个线程，每个线程有一个浮点数
输出: 所有线程得到最大值

步骤:
1. 每 16 个线程一组（warp 内的行）
2. 使用 warp shuffle 比较：线程 i 和线程 i+16 比较
3. 通过多轮比较（类似 butterfly network）得到最大值
4. 广播到所有 16 个线程
```

**求和归约**（`warp::row_sum`）：
```
输入: 16 个线程，每个线程有一个浮点数
输出: 所有线程得到总和

步骤:
1. 类似最大值归约，但使用加法而非比较
2. 通过多轮累加得到总和
3. 广播到所有 16 个线程
```

#### Warp 同步和信号量

Warp 同步使用 `kittens::warp::sync()`，它比全局 `__syncthreads()` 更轻量级。

**信号量的相位**（phase）：
```cpp
kittens::warp::wait(K_arrived(s, stage), (i / NUM_STAGES) % 2);
kittens::warp::arrive(K_finished(s, stage));
```
- `(i / NUM_STAGES) % 2`：在两个相位间交替，避免自死锁
- 每个信号量有两个槽位（phase 0 和 phase 1），可以交替使用

### 5.4 代码实践

#### Warp-Level 矩阵乘法

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L472-L473

计算 \(Q \times K^T\)，生成注意力分数矩阵。

```cpp
kittens::warp::load(K_reg, K_smem);
kittens::warp::mma_ABt(attn_fl_reg, Q_reg, K_reg, attn_fl_reg);
```

#### Warp-Level 归约

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L485-L486

计算每行的最大值，用于 Online Softmax。

```cpp
kittens::warp::row_max(max_vec_reg, attn_fl_reg, max_vec_reg);
```

#### Warp 同步

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L523-L527

在计算完成后，使用 warp 同步确保所有线程完成计算。

```cpp
kittens::warp::sync();
if (start_blk_idx < end_blk_idx) {
    finish_KV_page(s);
    kittens::warp::div_row(O_reg, O_reg, norm_vec_reg);
    ...
}
```

#### Warp-Level 数据存储

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L539-L540

将寄存器中的数据存储到共享内存，需要 warp 同步确保所有线程完成写入。

```cpp
store_4_rows(O_smem, O_reg, q_head_local_idx);
kittens::warp::sync();
```

### 5.5 练习题

**练习 1**：为什么 `warp::row_max` 只需要 16 个线程参与，而不是 32 个？

**练习 2**：在 warp 同步中，为什么使用 `(i / NUM_STAGES) % 2` 作为相位？

**练习 3**：假设 warp 内的 32 个线程要计算 32 个数的总和，最少需要几轮 warp shuffle 操作？

**练习 4**：代码中 `kittens::warp::sync()` 和 `kittens::warp::arrive()` 的区别是什么？

### 5.6 答案

**答案 1**：
- `max_vec_reg` 是一个列向量（`col_vec`），只有 16 行
- 每 16 个线程（`laneid % 16`）负责一行，进行行内归约
- 另外的 16 个线程（`laneid >= 16`）在行最大值计算中不参与（因为寄存器布局）

**答案 2**：
- 两个相位交替使用，避免同一个信号量被重复等待而导致死锁
- 当 phase 为 0 时，等待信号量的槽位 0；当 phase 为 1 时，等待槽位 1
- 这样 producer 可以在下一个相位写入数据，而 consumer 在当前相位读取，实现流水线

**答案 3**：
- 使用 butterfly network（类似并行归约），需要 \(\log_2(32) = 5\) 轮
- 每轮将线程数量减半：32 → 16 → 8 → 4 → 2 → 1
- `kittens` 库的 `warp::row_sum` 使用硬件加速的 warp shuffle 指令，可以在单个指令内完成

**答案 4**：
- `warp::sync()`：同步 warp 内的所有线程，确保之前的内存操作完成
- `warp::arrive(sem)`：通知信号量，当前 warp 已经完成某个操作（如数据加载或计算完成）
- `sync()` 用于内存一致性，`arrive()` 用于跨 warp 的生产者-消费者同步

---

## 总结

本讲义深入分析了 Megakernels 中注意力计算的 CUDA 实现，涵盖了从 Partial 到 Reduction 的完整流程。关键要点包括：

1. **Partial Attention**：使用 Online Softmax 单次遍历计算部分注意力结果，支持流式处理
2. **Attention Reduction**：使用 Log-Sum-Exp 合并多个 partial 结果，保持数值稳定性
3. **GQA 支持**：32 个 Q heads 共享 8 个 KV heads，通过 warp 内并行和 4-warp 并行实现高效计算
4. **共享内存优化**：使用分页和双缓冲机制，最大化数据复用和流水线效率
5. **Warp 协作**：通过 warp-level 原语（MMA、归约、同步）实现高性能计算

这些技术共同构成了 Megakernels 低延迟 LLM 推理的基础，充分利用了 H100 GPU 的 132 个 SM 和 Tensor Core。

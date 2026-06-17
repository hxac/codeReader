# U6-L2: 矩阵向量乘法操作

## 最小模块 1: MatVec 基础操作

### 概念说明

矩阵向量乘法（Matrix-Vector Multiplication, MatVec）是神经网络中最基础的计算操作。对于 Transformer 模型而言，大量的线性变换都归结为矩阵向量乘法：\(y = Wx + b\)，其中 \(W\) 是权重矩阵，\(x\) 是输入向量，\(b\) 是偏置项。

在 GPU 上执行 MatVec 操作时，我们需要考虑：
- **内存访问模式**：权重矩阵和输入向量的加载效率
- **计算密度**：充分利用 GPU 的张量核心（Tensor Core）
- **数据复用**：减少全局内存访问，利用共享内存

ThunderKittens 框架通过专门的抽象（如 `st_bf`、`rv_fl`、`sv_fl`）来优化这些操作。

### 伪代码或流程

```python
# 伪代码：矩阵向量乘法的 CUDA 实现流程
def matvec(weights, activations):
    # weights: (16, 512) 的 bf16 矩阵片段
    # activations: (512,) 的 float32 向量
    
    # 1. 将激活向量广播到寄存器瓦片
    broadcast_activations = broadcast_to_tile(activations)
    
    # 2. 加载权重到寄存器瓦片
    weights_tile = load_weights_to_registers(weights)
    
    # 3. 执行矩阵乘法（使用 Tensor Core 或普通乘法）
    if has_tensor_core:
        result = mma(weights_tile, broadcast_activations)
    else:
        result = multiply_and_sum(weights_tile, broadcast_activations)
    
    # 4. 沿列方向求和，得到最终结果向量
    output = row_sum(result)
    
    return output  # (16,) 的 float32 向量
```

### 原理分析

在 CUDA 编程中，MatVec 操作的核心原理是**寄存器瓦片化（Register Tiling）**。我们将输入向量广播到一个寄存器瓦片中，然后与权重矩阵进行逐元素乘法，最后沿列方向求和。

数学上，对于权重矩阵 \(W \in \mathbb{R}^{m \times n}\) 和输入向量 \(x \in \mathbb{R}^n\)，输出向量 \(y \in \mathbb{R}^m\) 的每个元素为：

\[ y_i = \sum_{j=1}^{n} W_{i,j} \cdot x_j \]

在 GPU 实现中，我们：
1. 使用 `st_bf<16, 512>` 存储 16×512 的权重片段（bf16 精度）
2. 使用 `rv_fl<512>` 存储激活向量（float32 精度）
3. 使用 `rt_fl<16, 512>` 作为寄存器瓦片，用于广播激活向量
4. 最终输出使用 `sv_fl<16>` 存储 16 个结果

对于 H100 架构，我们可以直接使用 Tensor Core 的 MMA 指令：
\[ \text{out} = W \cdot x^\top \]

对于非 Tensor Core 架构，我们使用逐元素乘法后求和：
\[ \text{out} = \sum_{\text{cols}} (W \odot \text{broadcast}(x)) \]

### 代码实践

以下代码展示了 MatVec 的核心实现（H100 版本）：

[matvec 函数实现](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/utils.cuh#L40-L71)

```cpp
template <kittens::ducks::st::all st_t>
__device__ static inline void matvec(kittens::sv_fl<st_t::rows> &out_smem,
                                     st_t &weights_smem,
                                     kittens::rv_fl<st_t::cols> &activations) {
    using rt_t = kittens::rt_bf<st_t::rows, st_t::cols>;
    using rrv_t = typename rt_t::row_vec;
    using rcv_t = typename kittens::rt_fl<16, 16>::col_vec;
    using rv_t = kittens::rv_fl<st_t::rows>;
    using sv_t = kittens::sv_bf<st_t::rows>;

    // 1. 将激活向量复制到行向量
    rrv_t row_activations;
    kittens::warp::copy(row_activations, activations);

    // 2. 创建寄存器瓦片并广播激活向量
    rt_t broadcast_activations, weights;
    kittens::warp::broadcast_col(broadcast_activations, row_activations);
    
    // 3. 加载权重到寄存器瓦片
    kittens::warp::load(weights, weights_smem);
    
    // 4. 初始化输出瓦片
    kittens::rt_fl<16, 16> out_activations;
    kittens::warp::zero(out_activations);
    
    // 5. 使用 Tensor Core 执行矩阵乘法
    kittens::warp::mma_ABt(out_activations, weights, broadcast_activations,
                  out_activations);
    
    // 6. 沿列方向求和
    rcv_t sum_col_vec;
    kittens::warp::row_max(sum_col_vec, out_activations);

    rv_t sum_vec;
    kittens::warp::copy(sum_vec, sum_col_vec);

    // 7. 将结果写入共享内存（只有前 16 个线程写入）
    if (kittens::laneid() < 16) {
        out_smem[kittens::laneid()] = sum_vec[0][0];
    }
    kittens::warp::sync();
}
```

**关键点解析**：
- **第 51-52 行**：将输入的 `rv_fl` 寄存器向量复制到 `rt_t` 的行向量中
- **第 55 行**：将激活向量广播到寄存器瓦片的每一列，形成 `broadcast_activations`
- **第 56 行**：从共享内存加载权重到寄存器
- **第 59 行**：使用 Tensor Core 执行 `weights @ broadcast_activations^T`，这是 H100 特有的优化
- **第 62 行**：`row_max` 实际上是在做行求和（这是 ThunderKittens 的命名惯例）
- **第 67-69 行**：只有前 16 个线程写入结果，避免写冲突

对于非 Tensor Core 架构，实现略有不同：

[非 Tensor Core 版本的 matvec](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/utils.cuh#L73-L101)

```cpp
// 非 Tensor Core 版本（如 V100/AMD）
kittens::warp::mul(broadcast_activations, broadcast_activations, weights);
rcv_t sum_col_vec;
kittens::warp::row_sum(sum_col_vec, broadcast_activations);
```

这里使用逐元素乘法 `mul` 加上 `row_sum` 来替代 Tensor Core 的 MMA 指令。

### 练习题

1. **基础理解**：为什么在 MatVec 操作中需要将激活向量"广播"到寄存器瓦片？直接点乘会有什么问题？

2. **架构差异**：对比 H100 的 Tensor Core 实现和非 Tensor Core 实现，它们的计算逻辑有何本质区别？性能差异大概有多大？

3. **数据类型**：代码中为什么权重使用 `bf16` 而激活使用 `float32`？如果全部使用 `float32` 会怎样？

4. **并行效率**：为什么最后只有前 16 个线程（`laneid() < 16`）写入结果？这是如何避免写冲突的？

### 答案

1. **广播的原因**：矩阵向量乘法需要将同一个向量与矩阵的每一行进行乘加运算。广播将向量复制到寄存器瓦片的每一列，使得每个元素都能与对应的权重元素并行相乘，充分利用 GPU 的 SIMD 架构。直接点乘会导致串行执行，效率极低。

2. **架构差异**：H100 的 Tensor Core 实现使用专用的 MMA 指令，一次能完成 16×16 的矩阵乘法，硬件加速比约 8-16 倍。非 Tensor Core 版本使用通用乘法单元，需要逐元素相乘后再求和，效率较低。性能差异取决于具体硬件，通常 Tensor Core 版本快 2-4 倍。

3. **数据类型选择**：`bf16`（Brain Float 16）占用内存少，适合存储大量权重，且在推理时精度损失可接受。激活使用 `float32` 是因为在计算过程中需要更高精度来累积结果，避免数值误差累积。全部使用 `float32` 会翻倍内存带宽需求，降低性能。

4. **并行写入策略**：输出向量只有 16 个元素，而一个 warp 有 32 个线程。如果所有线程都写入，会导致写冲突和不可预测的结果。只让前 16 个线程写入，每个线程负责一个元素，避免了冲突，是高效的并行策略。

---

## 最小模块 2: RMSNorm 融合

### 概念说明

RMSNorm（Root Mean Square Normalization）是 Transformer 模型中常用的归一化层，用于稳定训练。对于输入向量 \(x \in \mathbb{R}^d\)，RMSNorm 的计算公式为：

\[ \text{RMSNorm}(x) = \frac{x}{\sqrt{\text{mean}(x^2) + \epsilon}} \odot \gamma \]

其中 \(\gamma\) 是可学习的缩放参数，\(\epsilon\) 是防止除零的小常数。

**融合（Fusion）**是指将多个连续的操作合并到一个 GPU kernel 中执行，避免中间结果写入全局内存。在 Megakernels 中，我们实现了 RMSNorm 与 MatVec 的融合：先对输入进行 RMSNorm，然后直接用归一化后的结果执行矩阵向量乘法。

### 伪代码或流程

```python
# 伪代码：RMSNorm + MatVec 融合操作
def rmsnorm_matvec_fusion(input_activations, weights, rms_scale_weights, eps):
    # 第一阶段：RMSNorm
    
    # 1. 计算平方和（跨 warp 并行）
    partial_sums = parallel_map_reduce(input_activations, lambda x: x**2)
    
    # 2. 计算 RMS 缩放因子
    mean_square = sum(partial_sums) / input_dim
    rms_factor = 1 / sqrt(mean_square + eps)
    
    # 3. 应用 RMSNorm 和可学习缩放
    normalized = input_activations * rms_factor * rms_scale_weights
    
    # 第二阶段：MatVec（无需写入全局内存）
    output = matvec(weights, normalized)
    
    return output
```

### 原理分析

RMSNorm 的计算分为三步：

1. **计算平方和**：对于向量 \(x\)，计算 \(S = \sum_{i=1}^{d} x_i^2\)
2. **计算缩放因子**：\(\alpha = \frac{1}{\sqrt{S/d + \epsilon}}\)
3. **应用归一化**：\(y_i = x_i \cdot \alpha \cdot \gamma_i\)

在 GPU 上实现时，我们：
- 使用多个 warp 并行计算部分平方和
- 通过共享内存汇总部分结果
- 在寄存器中直接应用归一化，无需中间结果落盘

融合的关键优势：
- **减少内存访问**：归一化后的激活向量不需要写入全局内存，直接在寄存器中传递给 MatVec
- **降低延迟**：避免了 kernel 启动和同步开销
- **提高吞吐**：连续的计算提高了 GPU 利用率

### 代码实践

以下是 RMSNorm 的实现：

[rms_norm 函数实现](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/utils.cuh#L5-L37)

```cpp
template <typename Config, kittens::ducks::sv::all sv_t>
__device__ static inline auto
rms_norm(const sv_t &rms_scale_smem, const sv_t &activations_smem,
         float rms_norm_eps, void *scratch_memory) {
    using rv_t = kittens::rv_fl<sv_t::length>;
    rv_t activations_vec, sq_activations_vec, rms_scale_vec;

    // 1. 加载激活向量到寄存器
    kittens::warp::load(activations_vec, activations_smem);
    
    // 2. 计算平方
    kittens::warp::copy(sq_activations_vec, activations_vec);
    kittens::warp::mul(sq_activations_vec, sq_activations_vec, sq_activations_vec);
    
    // 3. 计算 warp 级别的部分和
    float partial_sum = kittens::warp::sum(sq_activations_vec);

    // 4. 将部分和写入共享内存
    float *smem_rms_partial_sums = (float *)scratch_memory;
    if (kittens::laneid() == 0) {
        smem_rms_partial_sums[kittens::warpid()] = partial_sum;
    }
    kittens::group<Config::NUM_CONSUMER_WARPS>::sync(0);

    // 5. 汇总所有 warp 的部分和
    float full_sum = 0;
#pragma unroll
    for (int i = 0; i < Config::NUM_CONSUMER_WARPS; i++) {
        full_sum += smem_rms_partial_sums[i];
    }

    // 6. 计算 RMS 缩放因子
    float variance = full_sum / 2048.0f;
    float rms_scale = rsqrtf(variance + rms_norm_eps);

    // 7. 应用归一化和可学习缩放
    kittens::warp::mul(activations_vec, activations_vec, rms_scale);
    kittens::warp::load(rms_scale_vec, rms_scale_smem);
    kittens::warp::mul(activations_vec, activations_vec, rms_scale_vec);

    return activations_vec;
}
```

**关键点解析**：
- **第 12-14 行**：从共享内存加载激活向量，然后计算平方
- **第 15 行**：`warp::sum` 在 warp 内部进行归约求和
- **第 18-20 行**：每个 warp 的第一个线程将部分和写入共享内存
- **第 21 行**：同步所有 warp，确保部分和写入完成
- **第 24-27 行**：汇总所有 warp 的部分和，得到完整的平方和
- **第 29-30 行**：计算方差和 RMS 缩放因子（`rsqrtf` 是快速倒数平方根函数）
- **第 32-34 行**：应用 RMS 缩放和可学习的权重缩放

融合 MatVec 的调用示例：

[RMSNorm + MatVec 融合调用](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L338-L347)

```cpp
// 在 consumer_loop 中调用 rms_norm，然后直接传递结果给 matvec
auto activations_vec = rms_norm<Config>(
    rms_scale_smem, activations_smem, g.rms_norm_eps,
    pipeline::get_output_start(s, pipeline::OUTPUT_PIPELINE_STAGES));

kittens::warp::sync();
s.warp_finish_page(activation_page, 1);

// 直接使用归一化后的向量进行 MatVec，无需写入全局内存
pipeline::consumer_loop(s, g, activations_vec);
```

### 练习题

1. **并行归约**：为什么 RMSNorm 计算平方和时需要先在每个 warp 内部求和，然后再汇总？直接让所有线程并行求和会有什么问题？

2. **内存效率**：融合操作相比分离操作（先 RMSNorm kernel，再 MatVec kernel），减少了多少次全局内存访问？假设 hidden_dim = 2048。

3. **数值稳定性**：为什么在计算倒数平方根时需要加上 epsilon（`rsqrtf(variance + rms_norm_eps)`）？如果不加会有什么风险？

4. **融合限制**：什么样的操作适合融合？什么样的操作不适合融合？给出一个不适合融合的例子。

### 答案

1. **并行归约的原因**：直接让所有线程并行求和会导致写冲突，因为多个线程可能同时更新同一个内存位置。先在 warp 内部求和利用了 warp 级别的归约指令（高效），然后将每个 warp 的结果写入共享内存的不同位置（避免冲突），最后汇总。这种分层归约策略既高效又正确。

2. **内存访问节省**：分离操作需要将归一化后的 2048 个元素（每个 4 字节，float32）写入全局内存，然后再次读取，共 16KB 的额外内存访问。融合操作在寄存器中直接传递结果，完全省去了这 16KB 的读写。对于 batch 推理，节省的内存访问会成倍增加。

3. **数值稳定性**：当输入向量全零或接近全零时，方差会非常小或为零，倒数平方根会趋向无穷大，导致 NaN。加上 epsilon（通常约 1e-5）避免了除零，保证了数值稳定性。这是深度学习中常见的技巧。

4. **融合的限制**：适合融合的操作通常是：计算密集、数据局部性好、中间结果不需要跨 kernel 访问。不适合融合的例子：需要全局同步的操作（如 AllReduce）、中间结果需要被多个后续操作使用（如残差连接写入后多处读取）、计算复杂度过高导致 register spill 的操作。

---

## 最小模块 3: RoPE 融合

### 概念说明

RoPE（Rotary Positional Encoding，旋转位置编码）是 Transformer 模型中用于注入位置信息的技术。与传统的绝对位置编码不同，RoPE 通过旋转查询和键向量来编码相对位置关系。

对于二维向量 \((x_{2i}, x_{2i+1})\)，RoPE 的变换公式为：

\[
\begin{aligned}
x'_{2i} &= x_{2i} \cos(\theta_i) - x_{2i+1} \sin(\theta_i) \\
x'_{2i+1} &= x_{2i} \sin(\theta_i) + x_{2i+1} \cos(\theta_i)
\end{aligned}
\]

其中 \(\theta_i = \frac{pos}{10000^{2i/d}}\)，\(pos\) 是位置，\(d\) 是维度。

在 Megakernels 中，我们实现了 **RMSNorm + MatVec + RoPE** 的三重融合，进一步减少了内存访问和延迟。

### 伪代码或流程

```python
# 伪代码：RMSNorm + MatVec + RoPE 三重融合
def rmsnorm_matvec_rope_fusion(input_activations, weights, rms_weights, 
                                rope_cos, rope_sin, position, eps):
    # 阶段 1：RMSNorm
    normalized = rmsnorm(input_activations, rms_weights, eps)
    
    # 阶段 2：MatVec（计算 QKV 投影）
    qkv_projection = matvec(weights, normalized)
    
    # 阶段 3：RoPE（只对 Q 和 K 应用，V 不需要）
    for i in range(0, dim, 2):
        # 提取相邻元素对
        x_even = qkv_projection[i]
        x_odd = qkv_projection[i+1]
        
        # 应用旋转（仅对 Q 和 K）
        if is_q_or_kv:
            cos_val = rope_cos[i//2]
            sin_val = rope_sin[i//2]
            
            qkv_projection[i] = x_even * cos_val - x_odd * sin_val
            qkv_projection[i+1] = x_even * sin_val + x_odd * cos_val
    
    return qkv_projection
```

### 原理分析

RoPE 的核心思想是将位置信息编码到向量对的旋转角度中。从几何角度看，RoPE 将二维向量 \((x, y)\) 旋转 \(\theta\) 角度：

\[
\begin{bmatrix} x' \\ y' \end{bmatrix} = 
\begin{bmatrix} \cos\theta & -\sin\theta \\ \sin\theta & \cos\theta \end{bmatrix}
\begin{bmatrix} x \\ y \end{bmatrix}
\]

这个旋转矩阵是正交矩阵，保持了向量的模长，只改变方向。在多头注意力中，每个头的维度是 64，我们将 64 维向量视为 32 个二维向量，每对独立旋转。

**融合实现的关键挑战**：
1. **相邻元素访问**：RoPE 需要访问相邻的元素对（第 2i 和 2i+1 个元素），这在 GPU 的 SIMD 执行模式下需要使用 `__shfl_sync` 指令进行线程间通信
2. **条件分支**：只有 Q 和 K 需要 RoPE，V 不需要，引入了条件分支
3. **内存访问**：RoPE 的 cos/sin 表需要预加载，增加内存带宽压力

### 代码实践

以下是 RoPE 融合的核心实现：

[RoPE 融合实现](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_matvec_rope_append.cu#L106-L121)

```cpp
// 第 106-121 行：应用 RoPE
if (block_idx < V_BLK_START) { // 只对 Q 和 K 应用 RoPE

    // 1. 获取相邻线程的值
    int mod = (kittens::laneid() & 0b1) ? -1 : 1; // 偶数线程 +1，奇数线程 -1
    kittens::warp::sync();
    float pair_val = __shfl_sync(MASK_ALL, qkv_proj[0][0], kittens::laneid() + mod);

    // 2. 应用旋转
    if (kittens::laneid() < 16) {
        qkv_proj[0][0] = float(qkv_proj[0][0]) * rope_cos[0][0] +
                        float(-1 * mod) * float(pair_val) * rope_sin[0][0];
    }
}
```

**关键点解析**：
- **第 109 行**：`mod` 决定了当前线程是偶数（+1）还是奇数（-1），用于确定配对关系
- **第 112 行**：`__shfl_sync` 在 warp 内部交换数据，获取相邻线程的值
  - 对于偶数线程（laneid=0, 2, 4...），获取 laneid+1 的值
  - 对于奇数线程（laneid=1, 3, 5...），获取 laneid-1 的值
- **第 115-119 行**：应用 RoPE 公式
  - 对于偶数位置（对应公式中的 \(x_{2i}\)）：\(x' = x \cdot \cos - y \cdot \sin\)
  - 对于奇数位置（对应公式中的 \(x_{2i+1}\)）：\(x' = x \cdot \sin + y \cdot \cos\)
  - `mod` 的符号决定了是加还是减

完整的 RoPE 融合流程：

[完整的 RoPE 融合 pipeline](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_matvec_rope_append.cu#L72-L167)

```cpp
static __device__ inline void store(..., int output_idx, int output_stage) {
    int block_idx = inst.start_block_idx + output_idx;

    // 1. 获取输出缓冲区
    uint8_t *output_scratch_start = pipeline::get_output_start(s, output_stage);
    kittens::sv_bf<16> &qkv_proj_smem_bf = 
        *reinterpret_cast<kittens::sv_bf<16> *>(output_scratch_start);

    // 2. MatVec 的归约
    kittens::rv_fl<16> qkv_proj, rope_cos, rope_sin;
    matvec_reduce<Config, kittens::sv_fl<16>, kittens::rv_fl<16>,
                  pipeline::SCRATCH_BYTES_PER_WARP>(output_scratch_start, qkv_proj);

    // 3. 等待 RoPE 的 cos/sin 表加载完成
    kittens::wait(rope_arrived(s), 0);

    // 4. 加载对应头块的 cos/sin 值
    auto head_chunk = block_idx % 4;
    kittens::sv_fl<16> &rope_cos_sv = *reinterpret_cast<kittens::sv_fl<16> *>(
        get_rope_cos_ptr(s) + head_chunk * 64);
    kittens::sv_fl<16> &rope_sin_sv = *reinterpret_cast<kittens::sv_fl<16> *>(
        get_rope_sin_ptr(s) + head_chunk * 64);

    kittens::warp::load(rope_cos, rope_cos_sv);
    kittens::warp::load(rope_sin, rope_sin_sv);

    // 5. 应用 RoPE（如上所示）
    if (block_idx < V_BLK_START) { /* ... RoPE 代码 ... */ }

    // 6. 将结果写回共享内存
    kittens::warp::sync();
    kittens::warp::store(qkv_proj_smem_bf, qkv_proj);
    kittens::warp::sync();

    // 7. 将 QKV 写入全局内存（通过 TMA）
    if (kittens::laneid() == 0) {
        if (block_idx < K_BLK_START) { // Q
            kittens::tma::store_async<cache_policy::EVICT_LAST>(
                g.q_post_rope, qkv_proj_smem_bf, {0, 0, 0, block_idx});
        } else if (block_idx < V_BLK_START) { // K
            // 写入 KV cache
            int base_index = (block_idx - K_BLK_START) * Globals::matvec_block_size;
            int head_idx = base_index / Globals::head_dim;
            int dim_idx = (base_index % Globals::head_dim) / Globals::matvec_block_size;
            kittens::tma::store_async<cache_policy::EVICT_LAST>(
                g.k_cache, qkv_proj_smem_bf,
                {inst.layer_idx, static_cast<int>(g.pos_id), head_idx, dim_idx});
        } else { // V（无 RoPE）
            // 写入 V cache
            // ...
        }
    }
}
```

### 练习题

1. **线程间通信**：为什么 RoPE 实现中需要使用 `__shfl_sync`？如果直接从共享内存读取相邻元素的值会怎样？

2. **性能优化**：RoPE 的 cos/sin 表是按位置预计算的。在推理时，为什么可以使用预计算的表而不是实时计算 `cos(pos/10000^(2i/d))`？

3. **分支效率**：代码中有两个条件分支：`if (block_idx < V_BLK_START)` 和 `if (kittens::laneid() < 16)`。这些分支会导致 warp 分歧吗？为什么？

4. **融合边界**：为什么没有将 Attention 计算也融合进来（RMSNorm + MatVec + RoPE + Attention）？这样做的利弊是什么？

### 答案

1. **__shfl_sync 的必要性**：RoPE 需要访问相邻元素的值，而相邻元素位于不同线程的寄存器中。`__shfl_sync` 允许线程间直接交换寄存器值，无需经过共享内存，延迟更低（约 20 个周期 vs 共享内存的 80+ 周期）。如果从共享内存读取，需要先将所有值写回共享内存再读取，增加了内存访问和同步开销。

2. **预计算 cos/sin 表**：推理时位置通常是连续或可预测的（0, 1, 2, ...），可以预先计算最大序列长度的 cos/sin 表，存储在全局内存中。实时计算三角函数开销巨大（每个元素需要几十个周期），而预计算后只需一次内存加载（约 200 周期，但可流水线化）。对于 128K 的 vocab 和 64 的 head_dim，表大小约 16KB，完全可以接受。

3. **分支分歧分析**：
   - `if (block_idx < V_BLK_START)`：这个判断在 warp 级别是统一的，因为一个 warp 处理同一个 block_idx，不会分歧。
   - `if (kittens::laneid() < 16)`：这个会导致分歧，但只有 16 个线程执行分支，开销可控。这是必要的，因为输出向量只有 16 个元素。

4. **融合边界**：Attention 计算涉及大量的中间结果（注意力矩阵、softmax 等），融合会导致：
   - **寄存器压力**：中间状态过多会导致 register spill，反而降低性能
   - **灵活性降低**：Attention 需要访问 KV cache 的所有历史位置，融合后难以支持不同长度的序列
   - **调度复杂度**：Attention 的计算模式与 MatVec 截然不同，融合会增加调度器复杂度
   - **收益递减**：MatVec-RoPE 融合已经节省了主要的内存访问（归一化后的激活向量），进一步融合 Attention 的边际收益较小

---

## 最小模块 4: 残差连接优化

### 概念说明

残差连接（Residual Connection）是深度学习中的重要技术，通过将输入直接加到输出上，缓解了梯度消失问题。在 Transformer 中，每个子层（Attention、FFN）后都有残差连接：

\[ \text{output} = \text{LayerNorm}(\text{input} + \text{SubLayer}(\text{input})) \]

在 GPU 实现中，残差连接通常需要：
1. 从全局内存读取输入
2. 计算子层输出
3. 将输入和输出相加
4. 将结果写回全局内存

Megakernels 通过 **原子加法（Atomic Add）** 优化残差连接，多个操作可以并行写入同一个位置，GPU 硬件自动处理累加。

### 伪代码或流程

```python
# 伪代码：原子加法优化的残差连接
def matvec_with_residual(input_activations, weights, residual_buffer):
    # 传统方法（需要同步）
    # residual = load_from_global(residual_buffer)
    # output = matvec(input_activations, weights)
    # final_output = output + residual
    # store_to_global(final_output, residual_buffer)
    
    # 优化方法（使用原子加法）
    output = matvec(input_activations, weights)
    atomic_add(residual_buffer, output)  # GPU 硬件自动累加
    # 无需读取原始值，直接累加
```

### 原理分析

原子加法的核心思想是：**多个写入者可以并行累加到同一个位置，无需显式同步**。

GPU 的原子加法单元保证了：
- **原子性**：多个并行的加法操作不会相互干扰，最终结果是所有加法的总和
- **并行性**：不同 SM 的加法操作可以并行进行，只在写入全局内存时串行化
- **正确性**：即使写入顺序不确定，最终结果仍是正确的

对于残差连接，我们将输出写入 `hidden_states` 缓冲区时使用原子加法：
\[ \text{hidden\_states} \leftarrow \text{hidden\_states} + \text{output} \]

这样，多个 MatVec 操作（如 QKV 投影、O 投影、Up 投影、Down 投影）可以并行写入同一个缓冲区，无需等待前一个操作完成。

### 代码实践

以下是带残差连接的 MatVec 实现：

[带残差连接的 MatVec 实现](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_adds.cu#L55-L84)

```cpp
static __device__ inline void store(megakernel::state<Config> &s, const globals &g,
                                    parsed_instruction &inst,
                                    int output_idx, int output_stage) {

    int block_idx = inst.start_block_idx + output_idx;

    // 1. 获取输出缓冲区
    uint8_t *output_scratch_start = pipeline::get_output_start(s, output_stage);
    kittens::sv_bf<16> &output_smem_bf =
        *reinterpret_cast<kittens::sv_bf<16> *>(output_scratch_start);

    // 2. MatVec 归约
    kittens::rv_fl<16> output_rv;
    matvec_reduce<Config, kittens::sv_fl<16>, kittens::rv_fl<16>,
                  pipeline::SCRATCH_BYTES_PER_WARP>(
        output_scratch_start, output_rv);

    // 3. 将结果写回共享内存
    kittens::warp::sync();
    kittens::warp::store(output_smem_bf, output_rv);
    kittens::warp::sync();

    // 4. 使用原子加法写入全局内存（残差连接）
    if (kittens::warp::laneid() == 0) {
        auto &OutputActivations = g.*OutputActivationsPtr;
        kittens::tma::store_add_async<cache_policy::EVICT_LAST>(
            OutputActivations, output_smem_bf, {block_idx});
        kittens::tma::store_async_read_wait();
    }

    kittens::warp::sync();
}
```

**关键点解析**：
- **第 78-80 行**：`tma::store_add_async` 是 ThunderKittens 提供的原子加法接口
  - 与普通 `store_async` 不同，它会将值加到目标位置而不是覆盖
  - 对于残差连接，这意味着 `output += hidden_states` 而不是 `hidden_states = output`
- **第 81 行**：`store_async_read_wait` 确保写入完成

残差连接的屏障同步：

[残差连接的屏障同步](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_adds.cu#L129-L134)

```cpp
// 在 consumer 中等待前一操作的残差连接完成
s.record(megakernel::TEVENT_AT_GMEM_WAIT);
while (*(volatile int *)&g.Bar[{inst.layer, prev_opcode - 1,
                                inst.reduction_block_idx}] <
       EXPECTED_ARRIVAL_COUNT) {
    __nanosleep(Config::GMEM_SPIN_LOOP_SLEEP_NANOS);
}
s.record(megakernel::TEVENT_DONE_GMEM_WAIT);
```

**关键点解析**：
- **第 131 行**：`prev_opcode - 1` 是前一个操作的 opcode，我们需要等待它的残差连接完成
- **EXPECTED_ARRIVAL_COUNT**：期望的写入数量（如 512，表示 512 个 block 都要写入）
- **自旋等待**：使用 `__nanosleep` 降低自旋等待的功耗

完成残差连接后更新屏障：

[更新屏障计数](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_adds.cu#L166-L176)

```cpp
if (kittens::laneid() == 0) {
    s.record(megakernel::TEVENT_AT_GMEM_STORE);

    kittens::tma::store_async_wait(); // 等待所有写入完成

    atomicAdd(&g.Bar[{inst.layer, opcode - 1, 0}], inst.iters);
    s.record(megakernel::TEVENT_DONE_GMEM_STORE);
}
```

**关键点解析**：
- **第 170 行**：`atomicAdd` 增加屏障计数，表示当前操作的残差连接已完成
- **inst.iters**：增加的计数等于迭代次数（每个迭代写入一个 block）

### 练习题

1. **原子操作开销**：原子加法比普通写入慢多少？在什么情况下使用原子加法是值得的？

2. **内存一致性**：原子加法保证了加法的原子性，但没有保证写入顺序。对于残差连接，写入顺序重要吗？为什么？

3. **性能权衡**：原子加法 vs 显式同步（先读后加再写），哪种方法更高效？在什么场景下显式同步更好？

4. **扩展性**：如果有 3 个或更多操作需要残差连接（如 `output = input + f(input) + g(input) + h(input)`），原子加法还能正确工作吗？需要注意什么？

### 答案

1. **原子操作开销**：原子加法比普通写入慢约 2-4 倍（普通写入约 200 周期，原子加法约 400-800 周期），因为它需要锁住内存地址直到操作完成。但在残差连接场景下是值得的：
   - 避免了读取原始值的内存访问（节省 16KB × 2 次访问）
   - 避免了显式同步的开销（内核间同步需要数百到数千周期）
   - 多个操作可以并行执行，提高了整体吞吐

2. **内存一致性**：对于残差连接，写入顺序不重要。数学上，加法是可交换和可结合的：
   \[ a + b + c = a + c + b = b + a + c \]
   无论写入顺序如何，最终结果都是所有值的总和。只要原子加法保证每个加法操作都生效，结果就是正确的。这得益于浮点加法在相同精度下的可交换性。

3. **性能权衡**：
   - **原子加法更高效**：当多个操作并行写入同一位置时，原子加法避免了显式同步和额外的内存访问。典型场景：Transformer 的多个投影层（QKV、Up、Gate、Down）并行计算残差连接。
   - **显式同步更好**：当写入顺序很重要，或者需要中间结果进行其他计算时。例如：需要先计算 Attention 输出，再用于后续计算，此时显式同步更清晰。
   - 指导原则：如果只是简单的累加且写入顺序无关，优先使用原子加法；否则使用显式同步。

4. **多操作残差连接**：原子加法可以正确处理 3 个或更多操作的残差连接，但需要注意：
   - **数值精度**：浮点加法虽然可交换，但不可结合（`(a + b) + c` 可能不等于 `a + (b + c)`，由于舍入误差）。操作越多，累积误差越大。
   - **性能下降**：多个原子操作串行化，总延迟增加。如果有 10 个操作，每个 800 周期，总延迟 8000 周期。
   - **解决方案**：将多个操作的残差连接分阶段执行，或使用局部缓冲区累积后再原子加到全局。Megakernels 采用分阶段策略（每 2-3 个操作一组）。

---

## 总结

本讲义覆盖了 Megakernels 中矩阵向量乘法相关的四个关键优化：

1. **MatVec 基础操作**：利用寄存器瓦片化和 Tensor Core 加速矩阵向量乘法
2. **RMSNorm 融合**：将归一化和 MatVec 合并，减少中间结果的内存访问
3. **RoPE 融合**：三重融合 RMSNorm + MatVec + RoPE，通过 `__shfl_sync` 实现高效的线程间通信
4. **残差连接优化**：使用原子加法实现无锁的残差连接，提高并行度

这些优化的核心思想是：
- **减少内存访问**：融合操作让数据留在寄存器中
- **提高并行度**：原子加法让多个操作并行执行
- **硬件加速**：充分利用 Tensor Core 和 TMA（Tensor Memory Accelerator）

通过这些优化，Megakernels 实现了极低延迟的 LLaMA 推理，相比传统方法提升了数倍性能。

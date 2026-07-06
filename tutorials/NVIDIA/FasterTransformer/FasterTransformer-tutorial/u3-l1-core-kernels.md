# 核心 CUDA kernel 总览：layernorm / activation / add_residual

## 1. 本讲目标

学完本讲，你应该能够：

- 看懂 FasterTransformer（下称 FT）`kernels/` 目录下统一的 **`invokeXxx` 函数风格**——每个算子都由「一个 `__global__` 设备 kernel + 一个 host 启动函数」组成，layer/model 永远只调 `invokeXxx`，绝不直接写 `<<<>>>`。
- 读懂 **LayerNorm kernel** 的两遍 reduction 实现，理解 `warpReduceSum` / `blockReduceSum` 为什么能把 \(O(n)\) 的求和压成 \(O(\log n)\) 步。
- 读懂 **激活函数 kernel**（GELU / SiLU）如何用「模板策略模式」在编译期生成多种特化、零运行期开销，并做到原地（in-place）计算。
- 读懂 **add_bias_residual kernel** 如何把「加偏置 + 加残差」融进同一个 elementwise kernel，并用 `RESIDUAL_NUM` 模板参数兼容单残差与双残差结构。

本讲是进入「层（layer）」之前的最后一道关卡：它讲解的是 layer 内部真正在 GPU 上跑的「单件事」。

## 2. 前置知识

在读本讲前，建议你已经具备：

- **CUDA 编程直觉**（来自 u1-l5、u2-l1）：知道 `grid` / `block` / `thread`、`<<<grid, block, smem, stream>>>` 启动语法、`__global__` 与 `__device__` 的区别、共享内存（shared memory）与 `__syncthreads()`。
- **Tensor 与显存抽象**（u2-l1、u2-l2）：知道 FT 的 `Tensor` 只是个非拥有的描述符，真正的显存由 `IAllocator` 管；本讲里的 kernel 接收的都是裸指针 `T*`，而不是 `Tensor`。

下面补充三个本讲要用到的 CUDA 小概念，初学者不熟悉的话先看这里：

| 概念 | 一句话解释 |
| --- | --- |
| **warp（线程束）** | GPU 调度的最小单位，连续 32 个线程组成一个 warp，它们锁步执行同一条指令。 |
| **warp shuffle（`__shfl_xor_sync`）** | 一个 warp 内线程之间直接交换寄存器值的硬件指令，不走任何内存，极快。 |
| **reduction（归约）** | 把一串数「累加 / 取最大」成一个数。本讲里 LayerNorm 要对 hidden 维度做两次求和归约。 |
| **elementwise（逐元素）** | 每个输出元素只依赖同位置的输入元素（如 `c[i]=a[i]+b[i]`），天然可大规模并行，瓶颈通常在显存带宽而非计算。 |

还有一点工程认知很关键（承接 u2-l3）：transformer block 里既有 **GEMM**（矩阵乘，计算密集），也有大量 **elementwise / reduction**（如 LayerNorm、激活、加残差，**显存带宽密集**）。后者看起来「简单」，但每一层、每一个 token 都要跑。如果朴素地写成「读一遍→算→写一遍」的多个独立 kernel，显存来回搬运的开销会吃掉 GEMM 省下的时间。所以 FT 的核心思路之一就是**把这些小算子融合（fuse）**，本讲的三个 kernel 就是融合思想的代表作。

## 3. 本讲源码地图

本讲涉及的关键文件都位于 `src/fastertransformer/kernels/`：

| 文件 | 作用 |
| --- | --- |
| [layernorm_kernels.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/layernorm_kernels.h) | 声明 `LayerNormType` 枚举、`LayerNormWeight<T>` 与一堆 `invokeXxx` 模板（host 接口）。 |
| [layernorm_kernels.cu](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/layernorm_kernels.cu) | LayerNorm 的 `__global__` kernel 实现 + `invokeGeneralLayerNorm` 等启动函数（本讲的「重头戏」）。 |
| [reduce_kernel_utils.cuh](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/reduce_kernel_utils.cuh) | 全库共用的 `warpReduceSum` / `blockReduceSum` 等归约工具，LayerNorm 加速的关键。 |
| [activation_kernels.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/activation_kernels.h) | 声明 `invokeGenericActivation`、`invokeAddBiasGeluV2`、`invokeAddBiasTanh` 等。 |
| [activation_kernels.cu](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/activation_kernels.cu) | `GeluActivation` / `SiluActivation` / `ReluActivation` / `IdentityActivation` 结构体与 `generic_activation` kernel。 |
| [add_residual_kernels.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/add_residual_kernels.h) | 声明 `invokeAddBiasResidual` 的多个重载（1/2 残差、是否带 scale）。 |
| [add_residual_kernels.cu](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/add_residual_kernels.cu) | `addBiasResidual` kernel 实现与启动函数。 |

> 提示：`reduce_kernel_utils.cuh` 不在本讲的「关键源码」清单里，但理解 LayerNorm 加速绕不开它，所以本讲会一并精读。

## 4. 核心概念与源码讲解

### 4.1 invokeXxx 调用约定：kernel 与 host 启动函数的分层

#### 4.1.1 概念说明

FT 的 `kernels/` 目录有一个贯穿全库的约定：**每一个算子都由两层组成**。

1. **设备 kernel**：一个 `__global__` 函数，是真正跑在 GPU 上的代码，负责「一个 block 干什么」。
2. **host 启动函数**：一个普通 C++ 函数，名字以 `invoke` 开头（如 `invokeGeneralLayerNorm`、`invokeGenericActivation`、`invokeAddBiasResidual`），负责决定 `grid` / `block` 大小、挑选哪个模板特化、配置共享内存，最后发出 `<<<grid, block, smem, stream>>>` 启动。

**layer / model 代码永远只调 `invokeXxx`，从不直接写 `<<<>>>`。** 这样做的好处是：

- 把 CUDA 启动细节（block 多大、要不要共享内存、走哪个特化）集中在一处，layer 代码保持干净。
- 把「该跑哪个变体」的判断（如 FP16 走 half2 路径、INT8 走带 scale 路径）封装在 `invokeXxx` 里，对外接口统一。
- `invokeXxx` 看起来就像普通函数调用：`invokeGeneralLayerNorm(out, input, gamma, beta, eps, m, n, ...);`。

#### 4.1.2 核心流程

一个典型 `invokeXxx` 的内部流程：

```
invokeXxx(参数指针、形状 m/n、stream):
  1. 根据 m/n/数据类型，计算 dim3 grid, dim3 block
  2. 根据 nullptr 判断（bias 是否存在？residual2 是否存在？是否 INT8？）挑选模板特化
  3. （可选）配置共享内存大小、调用 cudaFuncSetAttribute
  4. kernel<<<grid, block, smem, stream>>>(...) 启动
  5. （通常不在此处做 cudaStreamSynchronize，保持异步）
```

注意第 5 点：FT 的 kernel 启动是**异步**的，函数立刻返回，真正的 GPU 计算在 stream 上排队。`invokeXxx` 不阻塞——这是推理流水线高吞吐的基础。

#### 4.1.3 源码精读

以 `invokeAddBiasResidual` 的重载链为例，可以看到「简化重载 → 完整实现」的层层委托。最简的三参数版本（残差默认只有一段、`input=output` 原地）在头文件里内联：

```cpp
// 三参数版：残差只有 1 段，input 默认就写在 output 上（原地）
template<typename T>
void invokeAddBiasResidual(T* output, const T* residual1, const T* bias, const int m, const int n, cudaStream_t stream)
{
    invokeAddBiasResidual(output, residual1, (const T*)nullptr, bias, m, n, stream);
}
```
见 [add_residual_kernels.h:L43-L47](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/add_residual_kernels.h#L43-L47)——它把 `residual2` 补成 `nullptr`，转发给两残差版本。

两残差版本再转发到「带 scale 的完整十参数版」——真正的启动函数（[add_residual_kernels.cu:L109-L113](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/add_residual_kernels.cu#L109-L113)）：

```cpp
template<typename T>
void invokeAddBiasResidual(
    T* output, const T* residual1, const T* residual2, const T* bias, const int m, const int n, cudaStream_t stream)
{
    invokeAddBiasResidual(output, output, residual1, residual2, bias, nullptr, nullptr, m, n, stream);
}
```

这里第二个参数 `output` 同时充当「输入」与「输出」，即**原地计算**；`scale_inter/scale_out` 传 `nullptr` 表示不走 INT8 反量化路径。最终落到 4.4 节要精读的真正启动函数。

> 这种「多个重载 + 一个完整实现」的写法是 FT 全库风格：调用方按需传最少的参数，`nullptr` 默认值由重载补齐，模板特化在最终的 `invoke` 里统一决定。

#### 4.1.4 代码实践

- **目标**：建立对 `invokeXxx` 分层约定的肌肉记忆。
- **步骤**：
  1. 打开 [add_residual_kernels.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/add_residual_kernels.h)，数一下 `invokeAddBiasResidual` 有几个重载、各自参数个数。
  2. 用 `Grep` 在 `src/fastertransformer/` 下搜 `invokeAddBiasResidual(` 的调用点，看看 layer 代码到底用的是哪个重载。
- **观察**：你会发现绝大多数调用点用的是「最简重载」，完整十参数版只出现在 `invoke` 实现内部。
- **预期结果**：能复述「layer 调简版 → 简版补 nullptr → 完整版决定特化并启动」这条委托链。

#### 4.1.5 小练习与答案

**练习 1**：为什么 FT 要把「决定 block 大小」「挑选模板特化」这些逻辑放在 `invokeXxx` 里，而不是直接让调用方写 `<<<>>>`？

> **参考答案**：因为这些决策依赖数据类型、形状、是否带 bias/INT8 等运行期参数，集中放在 `invokeXxx` 才能保证：调用方接口统一、决策逻辑只有一处（便于维护与调优）、layer 代码不被 CUDA 细节污染。

**练习 2**：`invokeXxx` 内部启动 kernel 后通常**不**调用 `cudaStreamSynchronize`，这意味着什么？

> **参考答案**：kernel 启动是异步的，`invokeXxx` 立刻返回，GPU 计算在 stream 上排队。这样 CPU 可以继续往同一条 stream 上提交后续 kernel，形成流水线；只有调试模式（u1-l5 讲过的 `FT_DEBUG_LEVEL=DEBUG`）才会在每个 kernel 后插同步。

---

### 4.2 LayerNorm 与 block reduction 加速

#### 4.2.1 概念说明

**LayerNorm（层归一化）** 对一个样本的隐藏向量做归一化。给定长度为 \(n\) 的一行 \(x\)（在 transformer 里 \(n\) 就是 `hidden_units`），LayerNorm 计算：

\[
\mu = \frac{1}{n}\sum_{i=1}^{n} x_i
\]

\[
\sigma^2 = \frac{1}{n}\sum_{i=1}^{n}(x_i - \mu)^2
\]

\[
y_i = \gamma_i \cdot \frac{x_i - \mu}{\sqrt{\sigma^2 + \epsilon}} + \beta_i
\]

其中 \(\epsilon\) 是防止除零的小常数（FT 里由 `layernorm_eps` 传入），\(\gamma\) / \(\beta\) 是可训练的缩放与偏移（对应 `LayerNormWeight` 的 `gamma` / `beta` 两个指针，见 [layernorm_kernels.h:L47-L51](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/layernorm_kernels.h#L47-L51)）。

注意它与 BatchNorm 的区别：**LayerNorm 沿「特征维」归约，每个样本独立**。这正好天然适配 GPU——**一行（一个 token）交给一个 block**，行与行之间完全独立、无需通信。

难点在于：求 \(\mu\) 和 \(\sigma^2\) 各需要一次「跨整行的求和归约」。朴素做法是让一个线程串行累加 \(n\) 个元素，太慢。FT 用 **block reduction** 把它加速。

#### 4.2.2 核心流程

`generalLayerNorm` kernel 的执行模型：**`grid(m)`，一个 block 处理一行（一个 token）**。其中 `m` 是总行数（如 `batch × seq_len`，或在去 padding 后就是有效 token 数）。

每个 block 内部三步走：

```
step 1（求均值）:
  每个 thread 先串行累加自己负责的若干元素 → 得 local_sum
  blockReduceSum(local_sum) → 全 block 的总和（结果落在 thread 0）
  thread 0 写 s_mean = sum / n，__syncthreads 广播给所有线程

step 2（求方差）:
  每个 thread 用 s_mean 算自己那段元素的 (x_i - mean)^2 → 得 local_var_sum
  blockReduceSum(local_var_sum) → 全 block 的平方差总和
  thread 0 写 s_variance = rsqrtf(var/n + eps)，__syncthreads 广播

step 3（归一化输出）:
  每个 thread 并行写 y_i = (x_i - mean) * variance * gamma_i + beta_i
```

`blockReduceSum` 的内部是「**warp 归约 + 共享内存 + 首 warp 再归约**」两段式（见 4.2.3），它把 \(n\) 个数的求和从 \(O(n)\) 步压到 \(O(\log n)\) 步，且 warp 内用硬件 shuffle 指令、不走内存。

#### 4.2.3 源码精读

先看归约工具（理解加速的关键）。`warpReduceSum` 用 `__shfl_xor_sync` 做**蝴蝶归约**：每轮把相邻 \(16,8,4,2,1\) 距离的线程寄存器值相加，5 轮（\(\log_2 32\)）内让整个 warp 拿到总和，全程不碰内存：

```cpp
template<typename T>
__inline__ __device__ T warpReduceSum(T val)
{
#pragma unroll
    for (int mask = 16; mask > 0; mask >>= 1)
        val = add(val, __shfl_xor_sync(FINAL_MASK, val, mask, 32));
    return val;
}
```
见 [reduce_kernel_utils.cuh:L73-L80](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/reduce_kernel_utils.cuh#L73-L80)。

`blockReduceSum` 在其上搭出「整个 block」的归约：

```cpp
template<typename T>
__inline__ __device__ T blockReduceSum(T val)
{
    static __shared__ T shared[32];
    int lane = threadIdx.x & 0x1f;   // warp 内编号 0..31
    int wid  = threadIdx.x >> 5;     // block 内第几个 warp

    val = warpReduceSum<T>(val);     // ① 每个 warp 各自归约出本 warp 的部分和

    if (lane == 0)
        shared[wid] = val;           // ② 每个 warp 的 0 号线程把部分和写到 shared
    __syncthreads();

    // ③ 只让前 ceil(blockDim/32) 个线程（即第一个 warp 范围内）读 shared 再做一次 warp 归约
    val = (threadIdx.x < (blockDim.x / 32.f)) ? shared[lane] : (T)(0.0f);
    val = warpReduceSum<T>(val);
    return val;
}
```
见 [reduce_kernel_utils.cuh:L82-L103](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/reduce_kernel_utils.cuh#L82-L103)。

> **关键约定**：`blockReduceSum` 只保证 **thread 0** 拿到正确的总和（其他线程的返回值是中间垃圾值）。所以调用方必须由 thread 0 写入一个 `__shared__` 变量、再用 `__syncthreads()` 广播给全 block。下面 kernel 里就是这么用的。

现在看 `generalLayerNorm` kernel 主体。**均值**这一遍：

```cpp
float local_sum = 0.0f;
for (int i = tid; i < n; i += blockDim.x) {              // 每 thread 跨步累加自己负责的元素
    local_sum += (float)(ldg(&input[blockIdx.x * n + i]));
}
mean = blockReduceSum(local_sum);                         // 全 block 归约
if (threadIdx.x == 0) {
    s_mean = mean / n;                                    // 只有 thread 0 写
}
__syncthreads();                                          // 广播给所有线程
```
见 [layernorm_kernels.cu:L1594-L1604](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/layernorm_kernels.cu#L1594-L1604)。注意 `blockIdx.x * n`——block `i` 负责第 `i` 行，这正是「一行一 block」的体现。

**方差**这一遍用刚算出的 `s_mean`：

```cpp
float local_var_sum = 0.0f;
for (int i = tid; i < n; i += blockDim.x) {
    float diff = (float)(ldg(&input[blockIdx.x * n + i])) - s_mean;
    local_var_sum += diff * diff;
}
variance = blockReduceSum(local_var_sum);
if (threadIdx.x == 0) {
    s_variance = rsqrtf(variance / n + layernorm_eps);    // rsqrtf = 1/sqrt，硬件快速近似
}
__syncthreads();
```
见 [layernorm_kernels.cu:L1606-L1616](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/layernorm_kernels.cu#L1606-L1616)。注意 `rsqrtf` 直接算倒数平方根，比 `1.0f/sqrtf(...)` 快得多。

**输出**这一步是纯 elementwise、无需归约，所有线程并行写：

```cpp
for (int i = tid; i < n; i += blockDim.x) {
    const int index    = blockIdx.x * n + i;
    float     beta_val = (beta == nullptr) ? 0.0f : (float)ldg(&beta[i]);
    T         val      = (T)((((float)input[index] - s_mean) * s_variance)
                             * (float)(ldg(&gamma[i])) + beta_val);
    // ...（INT8 / 动态量化分支略）
    normed_output[index] = val;
}
```
见 [layernorm_kernels.cu:L1620-L1636](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/layernorm_kernels.cu#L1620-L1636)。

最后看启动函数 `invokeGeneralLayerNorm`，它体现「**按数据类型与形状挑特化**」的设计：

```cpp
dim3 grid(m);                          // 一个 block 处理一行
// ... FP16/BF16 且 n 为偶数时走 half2 打包路径（每线程处理 2 个元素）
if (n % 2 == 0 && (std::is_same<T, half>::value ...) && opt_version > 0) {
    int half_n = n / 2;
    dim3 block(min((half_n + 31)/32*32, 512));   // 向上对齐到 32 的倍数，上限 512
    // 复用融合版 kernel generalAddBiasResidualLayerNormOpt（is_output=false）
    dispatch_generalAddBiasResidualLayerNormOpt_unroll_factor(...);
}
else {
    dim3 block(min(n, 1024));           // 通用标量路径
    if (n % 32 != 0) block.x = 1024;    // warp shuffle 要求 block.x 是 32 的倍数
    generalLayerNorm<T, false><<<grid, block, 0, stream>>>(input, gamma, beta, out, ...);
}
```
见 [layernorm_kernels.cu:L1652-L1735](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/layernorm_kernels.cu#L1652-L1735)。

两个值得品味的细节：

1. **`n % 32 != 0` 时强制 `block.x = 1024`**（[L1716-L1718](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/layernorm_kernels.cu#L1716-L1718)）。因为 `blockReduceSum` 内部用了 warp shuffle，`blockDim.x` 必须是 32（一个 warp 宽度）的整数倍，否则归约会算错。
2. **FP16 复用融合版 kernel**。common 的 FP16 情况并不直接调标量 `generalLayerNorm`，而是调「加偏置+残差+layernorm」三合一的融合 kernel（4.1 里见过的 `generalAddBiasResidualLayerNormOpt2`），通过 `is_output=false` 把「加偏置/残差」关掉、只保留 layernorm——一份代码两种用途，这是 FT 大量「融合 kernel」复用的典型手法。

> 旁注：还有一个更激进的 `...Opt2` 变体（[L195-L246](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/layernorm_kernels.cu#L195-L246)）用恒等式 \(\sigma^2 = E[x^2] - (E[x])^2\) 把「求和」与「求平方和」合并成**一遍**归约（`blockReduceSumV2` 同时归约两个值），省掉一遍读数据。代价是该公式在数值上不如两遍稳定，FT 用它换速度。

#### 4.2.4 代码实践

这是本讲的主实践——一个**源码阅读型实践**（这些 kernel 是 CUDA 代码，难以脱离模型单独运行；若想看它真实跑起来，可在学完 u4-l1 后用 `bert_example` 端到端触发）。

- **目标**：弄清 `invokeGeneralLayerNorm` 的输入/输出张量形状、一个 block 处理多少元素，并解释 block reduction 为何加速。
- **操作步骤**：
  1. 打开 [layernorm_kernels.cu 的 invokeGeneralLayerNorm](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/layernorm_kernels.cu#L1652-L1735)。
  2. 假设一组典型 BERT 形状：`m = batch_size * seq_len = 8 * 128 = 1024` 行，`n = hidden_units = 768`，数据类型 `half`。
  3. 回答下表（**待本地验证**：若你在本机用 `cudaGetDeviceProperties` 查到 SM 数，可估算实际并发 block 数）：

  | 量 | 你的答案 |
  | --- | --- |
  | `grid` 大小 | ? |
  | 一个 block 负责多少个输入元素？ | ? |
  | 本例走哪条路径（half2 还是标量）？为什么？ | ? |
  | `block.x` 等于多少？ | ? |
  | 每个 thread 实际处理多少个元素（n / blockDim.x，或 half2 路径下 half_n / blockDim.x）？ | ? |

  4. 在 kernel 源码里定位：求均值在第几行？求方差在第几行？为什么两次都要 `__syncthreads()`？
- **需要观察的现象**（阅读理解，非运行）：`blockReduceSum` 的返回值只对 thread 0 有效，所以 kernel 必须用 `s_mean` / `s_variance` 这两个 `__shared__` 变量 + `__syncthreads()` 把结果广播给全 block。
- **预期结果**：能填出「grid=1024；一个 block 处理 768 个元素；half 走 half2 路径因为 `n%2==0`；标量路径 block.x=min(768,1024)=768，但 half2 路径 block.x=min((384+31)/32\*32,512)=512；每 thread 处理约 768/768≈1 个（标量）或 384/512 后跨步处理（half2）」。能口述「warp shuffle 让求和从 O(n) 步变 O(log n) 步、且不读内存」。
- **关于加速的要点解释**：朴素串行求和 768 个数要 768 步加法；用 block reduction 后，warp 内 5 步 shuffle + 跨 warp 一轮共享内存 + 首 warp 再 5 步，约十几次硬件操作即得全 block 之和；而且 warp shuffle 走寄存器、不占显存带宽，把宝贵的带宽留给真正必须的 `input[]` 读取。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `invokeGeneralLayerNorm` 里 `grid` 直接设成 `dim3(m)`，而不是 `dim3(m, something)`？

> **参考答案**：LayerNorm 每行独立、行间无需通信，所以一行交一个 block 最自然；行内的归约全部在单个 block 内用 `blockReduceSum` 完成，不需要第二个 grid 维度。

**练习 2**：如果把 `blockReduceSum` 换成「让 thread 0 串行 for 循环累加 n 个元素」，会慢在哪儿？

> **参考答案**：① 只有一个线程在算，其余 1023 个线程闲置，并行度极低；② 串行需 O(n) 步；③ 完全没用上 warp shuffle 这个零内存的硬件归约原语。结果该 kernel 从「计算/带宽都不是瓶颈」退化成严重的串行瓶颈。

**练习 3**：`s_variance = rsqrtf(variance / n + layernorm_eps)` 里的 `layernorm_eps` 去掉会怎样？

> **参考答案**：当某一行所有元素相等时，方差为 0，`rsqrtf(0)` 得 inf，输出全 NaN。`eps` 把分母托住、保证数值稳定。

---

### 4.3 激活函数 kernel：模板策略与原地计算

#### 4.3.1 概念说明

激活函数给线性变换引入非线性。transformer 里最常用的是：

- **GELU**（BERT、GPT 标准 FFN）：近似公式（tanh 版）
  \[
  \text{GELU}(x) = 0.5\,x\,\left(1 + \tanh\left(\sqrt{2/\pi}\,(x + 0.044715\,x^3)\right)\right)
  \]
  其中 \(\sqrt{2/\pi} \approx 0.7978845608\)。
- **SiLU / Swish**（GPT-J、Llama 系 FFN）：
  \[
  \text{SiLU}(x) = x \cdot \sigma(x) = \frac{x}{1 + e^{-x}}
  \]
- **ReLU**：\(\max(0, x)\)，部分老模型用。

激活是**纯 elementwise** 操作（每个输出只依赖同位置输入），瓶颈在显存带宽。FT 的设计有两个亮点：

1. **策略模式（strategy pattern）用模板实现**：把激活函数抽成 `GeluActivation` / `SiluActivation` / `ReluActivation` / `IdentityActivation` 四个结构体，各自提供 `static __device__ apply(val)`；kernel 用**模板的模板参数（template template parameter）** `template<template<typename T> class Activation>` 在编译期把激活「插」进 kernel。换激活 = 换模板参数 = 编译期生成不同的 kernel，**零运行期开销**。
2. **原地（in-place）计算**：kernel 直接读 `out[id]`、加 bias、施加激活、再写回 `out[id]`，不额外分配中间 buffer。

#### 4.3.2 核心流程

```
invokeGenericActivation<Activation, T, BT>(out, bias, gated_weights, gated_bias, ..., m, n, stream):
  1. 把 T 视为「打包类型」PT（half→half2），算出每元素打包了几路 packed_elems
  2. 配置 grid/block：目标是「每线程约 4 个元素」
       if n/4/packed_elems <= 1024:  block.x = n/4/packed_elems, grid.x = m
       else:                         block.x = 1024, grid.x = ceil(m*n/1024)
  3. generic_activation<Activation><<<grid, block, 0, stream>>>(...)
```

kernel 内部每个线程循环跨步处理多个元素：

```
for id in [线程负责的范围):
    val = out[id]                         # 原地读
    if with_bias: val += bias[id % n]     # 偏置按列广播（每行复用同一组 bias）
    if with_gate: gated_val = gated_weights[id]; gated_val += gated_bias[id%n]
    val = with_gate ? Activation::apply(val) * gated_val   # SiLU/GELU 门控（GLU 变体）
                   : Activation::apply(val)
    out[id] = val                         # 原地写
```

`with_bias` / `with_gate` 是**用 `nullptr` 在运行期判定的开关**——同一个 kernel 同时服务「带 bias」「不带 bias」「门控 FFN」「普通 FFN」四种情形，靠指针是否为空来分支。

#### 4.3.3 源码精读

先看激活「策略」结构体。`GeluActivation`（标量 float 版）把上述 GELU 公式直接翻译成代码：

```cpp
template<typename T>
struct GeluActivation {
    using return_type = T;
    static __device__ __forceinline__ T apply(const T& val)
    {
        const float cdf = 0.5f * (1.0f + tanh_opt((0.7978845608028654f * (val + 0.044715f * val * val * val))));
        return val * cdf;
    }
};
```
见 [activation_kernels.cu:L49-L57](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/activation_kernels.cu#L49-L57)。其中 `tanh_opt`（[L37-L47](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/activation_kernels.cu#L37-L47)）在 sm≥75 且 CUDA≥11 时直接用 PTX 内联汇编 `tanh.approx.f32`，比 `tanhf` 快。

`SiluActivation` 同样简洁：

```cpp
template<typename T>
struct SiluActivation {
    using return_type = T;
    static __device__ __forceinline__ T apply(const T& val)
    {
        return (T)((float)val / (1.0f + __expf((float)-val)));
    }
};
```
见 [activation_kernels.cu:L126-L133](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/activation_kernels.cu#L126-L133)，`__expf` 是 `expf` 的快速硬件近似。

> 还有特化的 `GeluActivation<half2>` / `SiluActivation<half2>` / `...<__nv_bfloat162>`（[L59-L89](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/activation_kernels.cu#L59-L89)、[L135-L153](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/activation_kernels.cu#L135-L153)），一次处理两个半精度元素——和 LayerNorm 的 half2 打包同理。

再看 kernel 主体如何「调用」策略。关键就是 `Activation<T>::apply(val)` 这一行：

```cpp
template<template<typename T> class Activation, typename T, typename BT>
__global__ void generic_activation(T* out, const BT* __restrict bias, ..., int m, int n)
{
    // ...
    for (int id = blockIdx.x * blockDim.x + threadIdx.x; id < m * n; id += blockDim.x * gridDim.x) {
        T val = out[id];                              // 原地读
        // ... 加 bias、加 gated_bias（略）
        if (with_gate) {
            val = cuda_cast<T>(Activation<T>::apply(val) * cuda_cast<Act_T>(gated_val));  // GLU 门控
        }
        else {
            val = cuda_cast<T>(Activation<T>::apply(val));                               // 普通激活
        }
        // ... ia3、INT8 分支略
        out[id] = val;                                // 原地写
    }
}
```
见 [activation_kernels.cu:L167-L239](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/activation_kernels.cu#L167-L239)。`Activation` 是「模板的模板参数」——调用方传 `GeluActivation` 或 `SiluActivation` 这个**类模板的名字**，编译器就把这里的 `Activation<T>::apply` 替换成对应实现，生成一个专用 kernel。

启动函数 `invokeGenericActivation` 负责 grid/block 决策：

```cpp
dim3 block, grid;
if (n / 4 / packed_elems <= 1024) {
    block.x = n / 4 / packed_elems;     // 让每线程处理约 4 个（打包）元素
    grid.x  = m;
}
else {
    block.x = 1024;
    grid.x  = ceil(m * n / 1024.);
}
generic_activation<Activation><<<grid, block, 0, stream>>>(...);
```
见 [activation_kernels.cu:L242-L284](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/activation_kernels.cu#L242-L284)。

最后是一个巧妙复用：`invokeAddBias`（只加偏置、不激活）就是 `invokeGenericActivation<IdentityActivation, ...>`：

```cpp
template<typename T>
void invokeAddBias(T* out, T const* bias, const int m, const int n, cudaStream_t stream)
{
    invokeGenericActivation<IdentityActivation, T, T>(
        out, bias, nullptr, nullptr, nullptr, nullptr, m, n, 0, nullptr, nullptr, stream);
}
```
见 [activation_kernels.h:L90-L95](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/activation_kernels.h#L90-L95)。`IdentityActivation::apply` 原样返回（[L157-L164](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/activation_kernels.cu#L157-L164)），于是「加偏置」复用了「激活」的整套代码——一份 kernel 服务多种语义。

#### 4.3.4 代码实践

- **目标**：体会「模板策略 + nullptr 运行期开关」如何让一个 kernel 覆盖多种激活语义。
- **步骤**：
  1. 在 [activation_kernels.cu](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/activation_kernels.cu) 里数 `INSTANTIATE_GENERIC_ACTIVATION` 宏展开了多少种组合（[L302-L326](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/activation_kernels.cu#L302-L326)）。
  2. 追踪 `invokeAddBias` → `invokeGenericActivation<IdentityActivation,...>` → `generic_activation` 这条调用链，确认「只加偏置」走的是同一个 kernel。
- **观察**：编译期会为 `{Gelu,Relu,Silu,Identity} × {float,half,bf16}` 各生成一份 kernel 代码——这是模板的代价（二进制变大）也是收益（运行期零分支开销）。
- **预期结果**：能说清「换激活函数不改 kernel 主体，只换模板实参；带不带 bias / 门控由 nullptr 在运行期判定」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 FT 用「模板的模板参数 `template<template<typename T> class Activation>`」而不是「运行期传一个 enum 来 switch 激活类型」？

> **参考答案**：模板在编译期就把激活函数内联进 kernel，没有分支、没有函数调用开销，GPU 上每个线程都跑最优的直线代码。若用 enum + switch，每个线程都要判一次分支，且无法把 `apply` 内联，性能下降。

**练习 2**：`generic_activation` 里 `val = out[id]` 读、`out[id] = val` 写，为什么敢原地写？

> **参考答案**：激活是逐元素操作，`out[id]` 的新值只依赖 `out[id]` 的旧值（及同位置的 bias/gate），不依赖任何「邻居」元素。因此原地覆盖不会破坏其它元素的输入。这省掉了一整块中间 buffer 的分配与一次额外的显存读写。

**练习 3**：`with_gate` 分支（`Activation::apply(val) * gated_val`）对应哪种网络结构？

> **参考答案**：对应 GLU（Gated Linear Unit）风格的 FFN，如 SwiGLU/GeGLU——FFN 第一段 GEMM 输出两路，一路做激活后与另一路相乘。FT 把这个「乘门」也融进了激活 kernel，避免再开一个 elementwise kernel。

---

### 4.4 add_bias_residual：elementwise 融合与残差复用

#### 4.4.1 概念说明

残差连接（residual connection）是深度 transformer 能训深的关键：把子层输入直接加到子层输出上，公式形如：

\[
y = x + \text{Sublayer}(x)
\]

在代码层面，「Sublayer 的输出 GEMM 结果」要加上「子层输入 \(x\)」再加上「输出偏置 bias」。这三步都是 elementwise 加法，朴素实现会写三个 kernel、读三遍写一遍。FT 把它们融进**一个** kernel `addBiasResidual`：

\[
\text{output}[i] = \text{input}[i] + \text{residual}[i] \;(+ \text{residual2}[i])\; + \text{bias}[i]
\]

它还用模板参数 `RESIDUAL_NUM`（取 1 或 2）兼容两种残差结构：

- **`RESIDUAL_NUM == 1`**（标准 transformer，如 BERT/GPT）：一段残差。
- **`RESIDUAL_NUM == 2`**（并行残差结构，如 GPT-J / GPT-NeoX）：attention 和 FFN 共用一条残差，需要同时加两段子层输出。

此外，`T2` 这个模板参数（默认等于 `T`）让同一 kernel 还能服务 **INT8 反量化** 场景：GEMM 输出 `int32_t`（INT8×INT8 的累加结果），kernel 把它乘上 `scale_inter * scale_out` 反量化回浮点再相加——又一次把「反量化 + 加残差 + 加偏置」三合一。

#### 4.4.2 核心流程

```
invokeAddBiasResidual(output, input, residual1, residual2, bias, scale_inter, scale_out, m, n, stream):
  1. blocks_per_row = ceil(n / 1024)      # n 太大时一行拆给多个 block
  2. grid(m, blocks_per_row), block(min(n,1024))
  3. 按 (residual2 是否为 nullptr) 选 RESIDUAL_NUM ∈ {1,2}
  4. 按 (scale_inter 是否为 nullptr) 选是否走 INT8 反量化（T2=int32_t）
  5. addBiasResidual<T, RESIDUAL_NUM><<<grid, block, 0, stream>>>(...)
```

kernel 内部：

```
col_index = blockIdx.y * blockDim.x + threadIdx.x
if col_index < n:
    bias_val = bias ? bias[col_index] : 0          # 偏置按列广播
    in = (T==T2) ? (T)input[...]
                 : (float)input[...] * scale_inter * scale_out   # INT8 反量化
    if RESIDUAL_NUM == 1:
        output[...] = in + residual1[...] + bias_val
    else:  # == 2
        output[...] = in + residual1[...] + residual2[...] + bias_val
```

#### 4.4.3 源码精读

kernel 主体很短，核心就是几行加法：

```cpp
template<typename T, int RESIDUAL_NUM, typename T2 = T>
__global__ void addBiasResidual(T* output, const T2* input, const T* residual1, const T* residual2,
                                const T* bias, const float* scale_inter, const float* scale_out,
                                const int m, const int n)
{
    const int col_index = blockIdx.y * blockDim.x + threadIdx.x;
    if (col_index < n) {
        T bias_val = (bias == nullptr) ? (T)(0.0f) : bias[col_index];
        T in;
        if (std::is_same<T, T2>::value) {
            in = cuda_cast<T>(input[blockIdx.x * n + col_index]);           // 普通浮点
        } else {
            in = cuda_cast<float>(input[blockIdx.x * n + col_index])
                 * (*scale_inter) * (*scale_out);                            // INT8 反量化
        }
        if (RESIDUAL_NUM == 1) {
            output[blockIdx.x * n + col_index] = in + residual1[...] + bias_val;
        } else if (RESIDUAL_NUM == 2) {
            output[blockIdx.x * n + col_index] = in + residual1[...] + residual2[...] + bias_val;
        }
    }
}
```
见 [add_residual_kernels.cu:L22-L52](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/add_residual_kernels.cu#L22-L52)。注意 `bias[col_index]`（不是 `bias[blockIdx.x*n+col_index]`）——**bias 是按列共享的**，每行复用同一组长度为 `n` 的偏置。

启动函数体现「运行期判 nullptr → 选模板特化」：

```cpp
int  blocks_per_row = ceil(float(n) / 1024);
dim3 grid(m, blocks_per_row);
dim3 block(min(n, 1024));
if (residual2 == nullptr) {
    if (should_scale_input) {                       // INT8
        addBiasResidual<T, 1><<<grid, block, 0, stream>>>(output, (const int32_t*)input, ...);
    } else {
        addBiasResidual<T, 1><<<grid, block, 0, stream>>>(output, input, residual1, residual2, bias, nullptr, nullptr, m, n);
    }
} else {                                            // 双残差
    if (should_scale_input) { addBiasResidual<T, 2><<<...>>>((const int32_t*)input, ...); }
    else                    { addBiasResidual<T, 2><<<...>>>(input, ...); }
}
```
见 [add_residual_kernels.cu:L54-L106](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/add_residual_kernels.cu#L54-L106)。`grid(m, blocks_per_row)` 是因为当 `n > 1024` 时单 block 放不下一整行，于是第二个 grid 维度把一行切成多段并行处理。

> **融合的收益**：朴素写法是「kernel A: tmp = input + residual；kernel B: out = tmp + bias」，要读 input、residual、tmp 各一遍、写 tmp 和 out。融合后只读 input、residual、bias、只写 output，**显存读写减半**。对这种带宽受限的算子，这直接近似翻倍吞吐。

#### 4.4.4 代码实践

- **目标**：理解 `RESIDUAL_NUM` 与 `grid(m, blocks_per_row)` 的含义。
- **步骤**：
  1. 打开 [add_residual_kernels.cu 的 invokeAddBiasResidual](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/add_residual_kernels.cu#L54-L106)。
  2. 假设 `m = 1024`，`n = 4096`（典型大模型 `hidden_units`），回答：`blocks_per_row` 是多少？`grid`、`block` 各是几维、每维多大？一个 thread 处理几个元素？
  3. 用 `Grep` 在 `src/fastertransformer/layers/` 下搜 `invokeAddBiasResidual`，看哪种 layer 用了双残差版本（传了非空的 `residual2`）。
- **预期结果**：能算出「blocks_per_row = ceil(4096/1024)=4；grid=(1024,4)；block=(1024)；每 thread 处理 1 个元素」。能指出双残差版本出现在并行残差结构（如 GPT-NeoX/J 相关 layer）。
- **待本地验证**：若本地有编译好的 FT，可在调用前后用 `cudaMemcpy` 取回 output 与手算的 `input+residual+bias` 比对，验证融合结果正确。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `bias` 用 `bias[col_index]` 而不是 `bias[blockIdx.x * n + col_index]`？

> **参考答案**：偏置是长度为 `n`（hidden 维度）的向量，对每一行（每一个 token）都相同，按列广播复用即可，不需要为每行存一份。

**练习 2**：`RESIDUAL_NUM == 2` 的双残差版本对应什么网络结构？为什么需要它？

> **参考答案**：对应 GPT-J / GPT-NeoX 等「并行」结构——attention 与 FFN 共用一条主残差，两路子层输出要同时累加到这条残差上。于是 kernel 要支持「input + residual1 + residual2 + bias」三路相加。

**练习 3**：把「加残差」和「加偏置」拆成两个独立 kernel，会比融合版慢在哪里？

> **参考答案**：拆开后中间结果要写一遍 global memory 再读回来一遍，而该算子是带宽受限的；融合版只读输入、只写输出，省掉一次中间读写，显存带宽占用近乎减半，吞吐显著提升。

---

## 5. 综合实践

**任务：把三个 kernel 串成一个「类 transformer 子层后处理」的故事。**

一个 transformer 子层（如 FFN）的尾部典型流程是：

```
GEMM 出 out → 加 bias → 激活(GELU/SiLU) → （下一层 GEMM）→ 加 bias + 残差 → LayerNorm
```

请完成：

1. 在 `src/fastertransformer/layers/` 下找一个真实 layer（例如 `FfnLayer.cc` 或注意力层），用 `Grep` 找出它在 forward 里依次调用了哪些本讲讲过的 `invokeXxx`（`invokeGenericActivation` / `invokeAddBiasResidual` / `invokeGeneralLayerNorm` 或其融合变体 `invokeGeneralAddBiasResidualPreLayerNorm`）。
2. 画出这个 layer 尾部的「invoke 调用序列图」，标注每一步输入/输出张量的形状（`m = 有效 token 数`，`n = hidden_units`）。
3. 对照本讲的源码，说明哪些步骤被**融合**成了一个 kernel（例如 `invokeGeneralAddBiasResidualPreLayerNorm` 把「加偏置 + 加残差 + pre-LayerNorm」三合一），哪些是分开调用的。指出融合发生在哪几个 `invokeXxx` 里。
4. **思考题**：如果让你把「激活」和「加残差」也融进同一个 kernel，从本讲 4.3/4.4 的设计里能借鉴哪些手法？（提示：模板策略、nullptr 运行期开关、原地读写。）

> **说明**：本实践为源码阅读型，不需要 GPU 运行。若想端到端看到这些 kernel 的输出，可在学完 u4-l1（BERT 模型）后运行 `bert_example`，并用 u1-l5 讲过的 `FT_LOG_LEVEL=DEBUG` + NVTX 在 Nsight Systems 时间轴上观察这些 kernel 的真实执行。

## 6. 本讲小结

- FT 的 `kernels/` 全库遵循 **`invokeXxx` 约定**：`__global__` 设备 kernel 负责「一个 block 干什么」，host 启动函数 `invokeXxx` 负责 grid/block 决策、模板特化选择与 kernel 启动；layer 只调 `invokeXxx`，从不直接 `<<<>>>`。
- **LayerNorm** 用「一行一 block」模型，靠 `warpReduceSum`（warp shuffle 蝴蝶归约）+ `blockReduceSum`（warp 归约 + 共享内存 + 首 warp 再归约）把求均值/方差从 \(O(n)\) 压到 \(O(\log n)\)，且 shuffle 不占显存带宽。
- **激活 kernel** 用「模板的模板参数」实现编译期策略模式，`GeluActivation`/`SiluActivation`/`ReluActivation`/`IdentityActivation` 各提供 `apply`，换激活零运行期开销；并通过原地读写与 nullptr 开关让一份 kernel 覆盖 bias/gate/INT8 多种语义。
- **add_bias_residual** 把「加偏置 + 加残差（一段或两段）+ 可选 INT8 反量化」融进单个 elementwise kernel，`RESIDUAL_NUM` 与 `T2` 两个模板参数分别控制残差段数与输入是否为 int32，显存读写近乎减半。
- 三者共同体现 FT 的核心优化哲学：**对带宽受限的小算子，能用融合与模板特化省下的每一次显存读写、每一次分支，都直接转化为推理吞吐。**
- 贯穿全讲的「数据类型分派」套路（FP32 标量路径、FP16/BF16 走 half2 打包路径、INT8 走带 scale 路径）与 u2-l1 的「DataType 枚举」、u1-l4 的「枚举→模板 dispatch」一脉相承。

## 7. 下一步学习建议

- **紧接着读 u3-l2（注意力 kernel）**：注意力是 `kernels/` 里最复杂的一类融合 kernel，但它同样遵循本讲的 `invokeXxx` 约定与 half2 打包 / 模板特化套路，本讲是它的直接前置。
- **回头看 layer**：学完 u3-l1～u3-l4 后，建议读 `src/fastertransformer/layers/FfnLayer.cc`，亲眼看一个 layer 如何把本讲的 `invokeGenericActivation`、`invokeAddBiasResidual` 与 u2-l3 的 GEMM 串起来——那是「kernel → layer」抽象闭合的瞬间。
- **想深挖 reduction**：可精读 [reduce_kernel_utils.cuh](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/reduce_kernel_utils.cuh) 全文，里面还有 `blockReduceMean`、`blockAllReduceMax`（LayerNorm 动态量化路径用到）等变体。
- **性能验证**：本机若有 GPU，可在 `tests/unittests/` 下参考 `test_attention_kernels.cu`、`test_gpt_kernels.cu` 的写法，仿写一个调用 `invokeGeneralLayerNorm` 的小用例，与 PyTorch `nn.LayerNorm` 的输出比对，验证你对形状与归约的理解。

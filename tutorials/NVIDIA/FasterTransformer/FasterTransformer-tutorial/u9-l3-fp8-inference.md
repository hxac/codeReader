# FP8 推理（实验性）

> 本讲承接 [u9-l1 INT8 量化推理](./u9-l1-int8-quantization.md) 与 [u6-l1 ParallelGpt 架构](./u6-l1-parallel-gpt.md)。在前一讲里我们看到了 INT8 的「w8a8 / SmoothQuant」路线，本讲把精度推到更激进、也更接近浮点本质的 **FP8**。本讲涉及的 FP8 能力在 FasterTransformer（以下简称 FT）中标记为 **Experimental**，对应 `models/gpt_fp8/` 与 `models/bert_fp8/` 两个模型目录。

---

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 **FP8 E4M3** 浮点格式的位宽分配、动态范围（`FP8_E4M3_MAX = 480`）以及它为何比 INT8 更「抗离群点」。
2. 写出启用 FP8 推理所需的三个硬性条件：**CUDA ≥ 11.8**、**Hopper 架构（sm_90）**、**CMake 选项 `-DENABLE_FP8=ON`**，并理解 `ENABLE_FP8` / `FUSE_GEMM_ACT` / `USE_QGMMA` / `FP8_MHA` 这一串条件编译宏的层层关系。
3. 读懂 `cuda_fp8_utils` 中的量化/反量化与缩放因子计算，理解「低精度存储 + 高精度计算」在 FP8 下的具体形态。
4. 读懂 `cublasFP8MMWrapper` 如何用 cuBLASLt 的 `CUDA_R_8F_E4M3` 输入 + `CUDA_R_16BF` 输出 + scale 指针 + `FAST_ACCUM` 完成 FP8 GEMM。
5. 看懂 `GptFP8` 与 `BertFP8` 的前向骨架：哪些算子已经 FP8 化（GEMM、layernorm、激活、加残差），哪些暂时不是（如 logits 投影）。
6. 解释 FP8 相比 INT8 w8a8 的精度优势来源：浮点表示的动态范围与相对精度，以及「无需校准即可用 identity scale」的鲁棒性。

---

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 FP8 是「浮点」而不是「定点」

INT8 是**定点**整数：8 个比特均匀地表达 `[-128, 127]` 这 256 个格子，相邻格子的「步长」处处相等。为了让神经网络里大小悬殊的数值塞进这 256 个格子，必须先用一个标量 `scale` 把张量整体缩放，于是离群点（outlier）和密集的小值难以兼顾——这正是 u9-l1 里 SmoothQuant 要把激活离群点「搬」到权重的原因。

FP8 则是**浮点**：同样 8 比特，但分成「符号位 + 指数位 + 尾数位」。FT 选用的是 **E4M3**（1 位符号 + 4 位指数 + 3 位尾数）。浮点的相邻格子步长**随数值大小变化**（值越大格子越宽），因而：

- 动态范围大：E4M3 的最大可表示值是 **480**（见下文 `FP8_E4M3_MAX`），而 INT8 只有 127；
- 相对精度稳定：尾数位保证「有效数字位数」大致恒定，无论数值大小。

一句话：FP8 用「指数负责范围、尾数负责精度」的浮点结构，天然比 INT8 更能容忍激活里的离群点。

### 2.2 FP8 GEMM 仍是 FP32 累加

和 INT8 一样，FT 的 FP8 GEMM 遵循**低精度存储 + 高精度计算**：两个 FP8 矩阵相乘时，**乘加用 FP32**（cuBLASLt 的 `CUBLAS_COMPUTE_32F`），只是输入输出在显存里以 FP8 存。两侧的 scale 指针负责把 FP8 还原成真实数值。这点和 u9-l1 的 `CUBLAS_COMPUTE_32I` 不同——INT8 用整数累加，FP8 用浮点累加。

### 2.3 两种模板参数 T1 / T2

FT 的 FP8 模型都用 `template<typename T1, typename T2>`，实例化为 `GptFP8<__nv_fp8_e4m3, __nv_bfloat16>` / `BertFP8<__nv_fp8_e4m3, __nv_bfloat16>`：

- `T1 = __nv_fp8_e4m3`：**FP8 计算/存储类型**（GEMM 的 FP8 输入输出、cache 的低精度形态）。
- `T2 = __nv_bfloat16`：**高精度中间类型**（BF16，用于残差流、logits、KV cache 默认形态等）。

理解了「T1 是低精度、T2 是高精度」这层关系，源码里 `T1*` 与 `T2*` 的交错就不再迷惑。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/fastertransformer/utils/cuda_fp8_utils.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cuda_fp8_utils.h) | FP8 使能宏、`FP8_E4M3_MAX`、`QUANTIZE_MODE` 枚举、打包类型与量化函数声明 |
| [src/fastertransformer/utils/cuda_fp8_utils.cu](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cuda_fp8_utils.cu) | `quantizeMatrix` / `computeFP8QuantizeScale` 等量化 kernel 实现 |
| [src/fastertransformer/utils/cublasFP8MMWrapper.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasFP8MMWrapper.h) | 继承自 `cublasMMWrapper` 的 FP8 GEMM 封装接口 |
| [src/fastertransformer/utils/cublasFP8MMWrapper.cu](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasFP8MMWrapper.cu) | cuBLASLt FP8 GEMM 的具体调用、scale 指针、`FAST_ACCUM`、算法查表 |
| [src/fastertransformer/kernels/activation_fp8_kernels.cu](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/activation_fp8_kernels.cu) | FP8「加偏置 + 激活 + 再量化」融合 kernel |
| [src/fastertransformer/kernels/layernorm_fp8_kernels.cu](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/layernorm_fp8_kernels.cu) | FP8 LayerNorm、`AddBiasResidualPostLayerNorm` 等融合 kernel |
| [src/fastertransformer/models/gpt_fp8/GptFP8.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/gpt_fp8/GptFP8.h) / [GptFP8.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/gpt_fp8/GptFP8.cc) | GPT FP8 端到端生成模型（context + decoder 两阶段） |
| [src/fastertransformer/models/gpt_fp8/GptFP8Decoder.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/gpt_fp8/GptFP8Decoder.cc) | GPT FP8 单步 decoder（FP8 LayerNorm + FP8 注意力 + FP8 FFN） |
| [src/fastertransformer/models/bert_fp8/BertFP8.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert_fp8/BertFP8.h) / [BertFP8.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert_fp8/BertFP8.cc) | BERT FP8 编码器（含 `fp8_mode_` 与去 padding 融合） |
| [src/fastertransformer/models/gpt_fp8/gpt_fp8_gemm.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/gpt_fp8/gpt_fp8_gemm.cc) | FP8 GEMM 离线调优工具（`data_type=4` 即 FP8） |
| [CMakeLists.txt](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt) | `ENABLE_FP8` 条件编译入口与 FP8 目标链接 |
| [docs/gpt_guide.md](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/gpt_guide.md) | GPT FP8 的构建与运行演示 |

---

## 4. 核心概念与源码讲解

### 4.1 FP8 数据类型与使能条件

#### 4.1.1 概念说明

要跑 FP8，必须同时满足「软件使能」与「硬件使能」两层条件。软件层由 CMake 的 `ENABLE_FP8` 开关驱动，它注入 `-DENABLE_FP8` 宏，让整个 FP8 相关源码（被 `#ifdef ENABLE_FP8` 包裹）参与编译；硬件层要求 GPU 计算 capability 为 **9.0（Hopper / H100）**，因为 E4M3 的硬件加速指令（包括 QGMMa 矩阵乘加速）只在这一代架构上提供。

此外，FT 还用一组**派生宏**描述 FP8 内部不同的实现路径：

- `FUSE_GEMM_ACT`：当目标架构是 sm_90 时定义，表示「GEMM + 激活」可以融合成一条 QGMMa 指令。
- `USE_QGMMA`：由 `FUSE_GEMM_ACT` 派生，开启 Hopper 专用的 QGMMa 1×1 融合 GEMM。
- `FP8_MHA`：默认**注释关闭**，开启后会把 KV cache 也存成 FP8（需要更高版本 cuBLAS）。
- `FP8_GEMM_OUTPUT_QUANT_DISABLE`：默认定义，表示「GEMM 输出先反量化成 BF16 再做激活」，而非直接产出 FP8。

#### 4.1.2 核心流程

使能 FP8 的判定链（自上而下逐层门槛）：

```
CUDA Toolkit 版本 ≥ 11.8  ──┐
                              ├─► CMake 注入 -DENABLE_FP8（编译期宏）
用户传 -DENABLE_FP8=ON  ─────┘
        │
        ▼
链接 BertFP8 / GptFP8 / cublasFP8MMWrapper / *_fp8_kernels 等目标（CMake）
        │
        ▼
运行期 GPU sm == 90 (Hopper)
        │
        ├─► __CUDA_ARCH__ == 900 ─► 定义 FUSE_GEMM_ACT ─► USE_QGMMA
        └─► 走 Hopper FP8 GEMM / QGMMa 路径
```

注意「CUDA ≥ 11.8」只是**声明** `ENABLE_FP8` 这个 option 并注入宏；真正要编出可用代码，仍需用户显式传 `-DENABLE_FP8=ON`。

#### 4.1.3 源码精读

CMake 中的条件声明（[CMakeLists.txt:24-30](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt#L24-L30)）：

```cmake
if((${CUDA_VERSION_MAJOR} VERSION_GREATER_EQUAL "11" AND ${CUDA_VERSION_MINOR} VERSION_GREATER_EQUAL "8")
   OR (${CUDA_VERSION_MAJOR} VERSION_GREATER_EQUAL "12"))
  add_definitions("-DENABLE_FP8")
  option(ENABLE_FP8 "ENABLE_FP8" OFF)
  if(ENABLE_FP8)
    message("CUDA_VERSION ... is greater or equal than 11.8, enable -DENABLE_FP8 flag")
  endif()
endif()
```

这段说明：`add_definitions("-DENABLE_FP8")` 会无条件注入宏（只要 CUDA 够新），但 `option(ENABLE_FP8 ... OFF)` 默认 **OFF**，只有用户传 `-DENABLE_FP8=ON` 时，下面的 `target_link_libraries` 才会把 FP8 目标链接进 `transformer-shared`（[CMakeLists.txt:432-456](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt#L432-L456)）。

派生宏在 `cuda_fp8_utils.h` 中按目标架构定义（[cuda_fp8_utils.h:24-32](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cuda_fp8_utils.h#L24-L32)）：

```cpp
// #define FP8_MHA
#if defined(__CUDA_ARCH__) && __CUDA_ARCH__ == 900
#define FUSE_GEMM_ACT
#endif
#define FP8_GEMM_OUTPUT_QUANT_DISABLE

#ifdef FUSE_GEMM_ACT
#define USE_QGMMA
#endif
```

即：编译目标是 Hopper（`__CUDA_ARCH__ == 900`）才定义 `FUSE_GEMM_ACT` 与 `USE_QGMMA`，从而启用 QGMMa 融合路径；`FP8_MHA` 默认被注释，因此 KV cache 默认仍存 BF16（见 4.4.3）。

`FP8_E4M3_MAX` 这个常量是 E4M3 浮点格式的最大可表示值（[cuda_fp8_utils.h:36](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cuda_fp8_utils.h#L36)）：

```cpp
const float FP8_E4M3_MAX = 480.0f;
```

这个 480 正是 E4M3 的「4 位指数 + 3 位尾数」所能表达的有限最大值，远大于 INT8 的 127——它是 FP8 动态范围的直接体现，也是 4.2 量化缩放因子的分母来源。

`docs/gpt_guide.md` 给出的启用 FP8 的最小命令（[docs/gpt_guide.md:940-947](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/gpt_guide.md#L940-L947)）：

```bash
cmake -DSM=90 -DCMAKE_BUILD_TYPE=Release -DBUILD_PYT=ON -DBUILD_MULTI_GPU=ON -DENABLE_FP8=ON ..
```

注意 `-DSM=90` 显式指定 Hopper，配合 `-DENABLE_FP8=ON`。文档开头还明确「FP8 is supported since Hopper and CUDA 11.8」。

#### 4.1.4 代码实践

**实践目标**：确认你所在机器能否启用 FP8，并理解每个编译开关。

**操作步骤**：

1. 查看 GPU 架构：`nvidia-smi --query-gpu=compute_cap --format=csv`，确认 `9.0`。
2. 查看 CUDA 版本：`nvcc --version`，确认 ≥ 11.8。
3. 对照 [CMakeLists.txt:432-456](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt#L432-L456)，列出 `ENABLE_FP8=ON` 时会被链接进 `transformer-shared` 的目标名（至少 6 个）。
4. 写出一条「单卡 + PyTorch + FP8」的 cmake 命令。

**需要观察的现象**：若把 `-DSM=80`（Ampere）但仍传 `-DENABLE_FP8=ON`，编译可以过（因为 `add_definitions("-DENABLE_FP8")` 只看 CUDA 版本），但 `FUSE_GEMM_ACT`/`USE_QGMMA` 不会被定义，运行期 FP8 GEMM 不会走 QGMMa 路径——属于「能编但跑不出应有性能 / 可能报错」的状态。

**预期结果**：得到一条形如 `cmake -DSM=90 -DBUILD_PYT=ON -DENABLE_FP8=ON ..` 的命令，并能口述「CUDA≥11.8 给宏、Hopper 给硬件、`-DENABLE_FP8=ON` 给链接」三道门槛。

> 待本地验证：步骤 4 的实际编译产物与运行结果需在真实 Hopper 机器上确认。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `add_definitions("-DENABLE_FP8")` 已注入宏，却还要单独设一个默认 OFF 的 `option(ENABLE_FP8)`？

**参考答案**：注入宏只让 FP8 源码「能被编译」（满足 `#ifdef ENABLE_FP8`），但 FP8 目标文件是否**链接进最终库**由 `if(ENABLE_FP8) target_link_libraries(...)` 控制。默认 OFF 是因为 FP8 是实验特性、且依赖 Hopper，多数用户无需付出额外的编译时间与链接体积，故设为 opt-in。

**练习 2**：`FUSE_GEMM_ACT` 与 `USE_QGMMA` 的关系是什么？

**参考答案**：`FUSE_GEMM_ACT` 是「能否融合 GEMM 与激活」的能力标志，仅在 sm_90 下定义；`USE_QGMMA` 由 `FUSE_GEMM_ACT` 直接派生（`#ifdef FUSE_GEMM_ACT #define USE_QGMMA`），用于在源码里选择 Hopper 专属的 QGMMa 1×1 融合实现路径。

---

### 4.2 量化、反量化与缩放因子：cuda_fp8_utils

#### 4.2.1 概念说明

FP8 与真实浮点数值之间的桥梁是**缩放因子（scale）**。FT 用一个 `QUANTIZE_MODE` 枚举区分两种粒度（[cuda_fp8_utils.h:38-42](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cuda_fp8_utils.h#L38-L42)）：

- `PER_CHANNEL`：每个输出通道一个 scale（权重常用，精度更高）。
- `PER_TENSOR`：整个张量一个 scale（激活常用，开销小）。
- `PER_CHANNEL_WEIGHT_PER_TENSOR_ACT`：权重按通道、激活按张量（INT8 里也见过这种组合）。

量化的本质是「找张量里绝对值最大的元素 `amax`，除以 `FP8_E4M3_MAX` 得到 scale，再把每个元素除以 scale 映射到 E4M3 表示范围」。反量化则是乘回 scale。FT 的量化 kernel 把这一步写成一个简单的 elementwise 乘法 + 类型转换。

#### 4.2.2 核心流程

量化缩放因子的计算（per-channel，按列求 amax）：

\[
\text{scale}_j = \max\!\left(\frac{\max_i |W_{i,j}|}{\text{FP8\_E4M3\_MAX}},\ \frac{1}{32}\right)
\]

下限 `1/32` 是为了避免 scale 过小导致数值全部映射到 0。量化时：

\[
\text{FP8}(x) = \text{cast}_{\text{E4M3}}(x \cdot \text{scale})
\]

反量化时：

\[
x = \text{float}(\text{FP8}(x)) \cdot \text{scale}_{\text{inv}}
\]

其中 `scale_inv = 1/scale`（FT 权重里常直接存 `input_scale_inv`）。注意 FT 的 GEMM 用的是「输入侧 scale」约定（见 4.3），即 `真实值 = FP8值 × scale`。

#### 4.2.3 源码精读

`quantizeMatrix` kernel 是量化的核心（[cuda_fp8_utils.cu:22-33](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cuda_fp8_utils.cu#L22-L33)）：

```cpp
template<typename T_OUT, typename T_IN, QUANTIZE_MODE quantize_mode>
__global__ void quantizeMatrix(T_OUT* output, float const* input_scale,
                               T_IN const* input, uint32_t size, uint32_t n)
{
    for (uint32_t i = threadIdx.x + blockIdx.x * blockDim.x; i < size; i += blockDim.x * gridDim.x) {
        if (quantize_mode == QUANTIZE_MODE::PER_CHANNEL) {
            output[i] = T_OUT((float)(input[i]) * __ldg(input_scale + (i % n)));
        } else {
            output[i] = T_OUT((float)(input[i]) * __ldg(input_scale));
        }
    }
}
```

逐元素地把 `input` 乘以 scale（per-channel 时按 `i % n` 取每列各自的 scale），再构造函数 `T_OUT(...)` 借助 CUDA 内置的 `__nv_fp8_e4m3` 转换完成 E4M3 取整。

`computeFP8QuantizeScale` 负责离线计算 scale（[cuda_fp8_utils.cu:91-105](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cuda_fp8_utils.cu#L91-L105)）：

```cpp
template<typename T_W>
__global__ void computeFP8QuantizeScale(float* quant_ptr, const T_W* weights, const int k, const int n)
{
    float max = -10000.f;
    for (int i = 0; i < k; i++) {
        float val = fabs((float)weights[i * n + blockIdx.x * blockDim.x + threadIdx.x]);
        max = max > val ? max : val;
    }
    quant_ptr[blockIdx.x * blockDim.x + threadIdx.x] = std::max(max / FP8_E4M3_MAX, 1.0f / 32.f);
}
```

每个线程负责权重矩阵的一列，遍历该列所有行求绝对值最大 `max`，再按上式得到 scale。这与 INT8 的对称量化 `scale = 127/amax`（u9-l1）结构完全一致，只是把分母换成了 FP8 的 480。

声明部分还提供了一组「打包类型」与转换辅助函数（[cuda_fp8_utils.h:83-107](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cuda_fp8_utils.h#L83-L107)），例如把 4 个 E4M3 一次性转成两个 BF16 pair（[cuda_fp8_utils.h:149-156](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cuda_fp8_utils.h#L149-L156)）。这些是 4.3、4.4 融合 kernel 用来提升显存带宽的「向量化读写」积木。

#### 4.2.4 代码实践

**实践目标**：理解量化/反量化的可逆性与 scale 的作用。

**操作步骤**：

1. 阅读 [cuda_fp8_utils.cu:22-42](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cuda_fp8_utils.cu#L22-L42)，确认 `invokeQuantizeMatrix` 的 grid/block 配置（`dim3 grid(32); dim3 block(256);`）。
2. 用伪代码写一段「把一个 BF16 张量量化成 FP8 再反量化回 BF16」的过程：先调用 `invokeComputeFP8QuantizeScale` 算 scale，再 `invokeQuantizeMatrix<__nv_fp8_e4m3, __nv_bfloat16, PER_TENSOR>` 量化，最后用一个反向乘 scale 反量化。
3. 思考：当某列 `max` 非常小（接近 0）时，为什么需要 `std::max(..., 1.0f/32.f)` 这个下限？

**需要观察的现象**：量化-反量化是**有损**的，因为 E4M3 只有 3 位尾数（约 8 个有效等级/区间）。对幅值接近 `FP8_E4M3_MAX=480` 的大值，相对误差较小；对接近 0 的小值，量化台阶更密但绝对表示能力有限。

**预期结果**：能说出「scale 把张量动态范围压到 E4M3 的 [-480, 480]，反量化乘回 scale 还原量纲」。

> 待本地验证：量化误差的实际数值需在 GPU 上运行 `tests/` 下的 FP8 kernel 测试（如 `layernorm_fp8_kernels_test.cc`）观察。

#### 4.2.5 小练习与答案

**练习 1**：`FP8_E4M3_MAX` 为什么是 480 而不是 512？

**参考答案**：E4M3 的编码并非「全指数 + 全尾数」都用于有限值，它为了与 E5M2 互换并保留 NaN 表示，最大有限值落在 480（二进制指数 1111、尾数 110 对应 1.75 × 2⁸ = 448，但具体编码约定下达到 480）。这是 IEEE/OCP FP8 规范的取值，FT 直接以常量 480.0f 体现。

**练习 2**：PER_CHANNEL 与 PER_TENSOR 在 kernel 里只是 `i % n` 之差，但对精度影响不同，为什么？

**参考答案**：PER_TENSOR 用全张量一个 scale，离群点会撑大 scale、把其余小值压到很少的有效位；PER_CHANNEL 给每列独立 scale，离群点只影响它所在通道，其它通道仍保留细粒度。权重一般按通道（列）量化以保精度，激活因需在线计算多用 per-tensor。

---

### 4.3 FP8 矩阵乘骨干：cublasFP8MMWrapper

#### 4.3.1 概念说明

`cublasFP8MMWrapper` 继承自 u2-l3 讲过的 `cublasMMWrapper`，复用其 handle、workspace、互斥锁与算法表，额外封装了 cuBLASLt 的 **FP8 矩阵乘**。关键差异在于：FP8 GEMM 的输入是 `__nv_fp8_e4m3`，输出可以是 BF16 或 FP8，并且必须通过 **scale 指针**（A_SCALE / B_SCALE / D_SCALE）告诉 cuBLASLt 如何在 FP8 与真实数值间换算。

它还提供两条融合接口：

- `Gemm_Bias_Act<RELU, GELU>`：GEMM + 加偏置 + 激活 融合（模板参数编译期选定激活）。
- `Conv1x1Gemm<RELU, GELU>`：Hopper 专属的 QGMMa 1×1 融合（GEMM + bias + 激活压进单条指令），名字虽叫 Conv1x1，实际就是 1×1 卷积等价的矩阵乘，被 FFN 第一段 GEMM 复用。

#### 4.3.2 核心流程

一次 FP8 GEMM（输出 BF16）在 cuBLASLt 层面的配置：

```
A (input)  : CUDA_R_8F_E4M3, OP_T (转置)
B (kernel) : CUDA_R_8F_E4M3, OP_N (不转置)
D (output) : CUDA_R_16BF
compute    : CUBLAS_COMPUTE_32F            ← FP32 累加
scaleType  : CUDA_R_32F
A_SCALE_POINTER → kernel_scale   (注意 FT 里 A 是权重 kernel)
B_SCALE_POINTER → input_scale    (B 是激活 input)
FAST_ACCUM = 1 (可选，cublas≥11.11.1，快速但不精确的累加)
```

注意 FT 这里把 cuBLASLt 的 A/B 与语义上的 kernel/input 做了交换（见 `devAscalePtr = kernel_scale`、`devBscalePtr = input_scale`），以适配其「A 转置、B 不转置」的列主序约定。

算法选择仍走 u2-l4 的 `cublasAlgoMap`，键里的 `data_type` 用 `FP8_DATATYPE = 4`（[cuda_utils.h:54](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cuda_utils.h#L54)），由 `gpt_fp8_gemm` 工具离线调优生成。

#### 4.3.3 源码精读

`Gemm`（BF16 输出）的核心配置（[cublasFP8MMWrapper.cu:145-178](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasFP8MMWrapper.cu#L145-L178)）：

```cpp
const void* devAscalePtr = (const void*)kernel_scale;
const void* devBscalePtr = (const void*)input_scale;

const auto aType       = CUDA_R_8F_E4M3;
const auto bType       = CUDA_R_8F_E4M3;
const auto dType       = CUDA_R_16BF;
const auto computeType = CUBLAS_COMPUTE_32F;
const auto scaleType   = CUDA_R_32F;
const cublasOperation_t tA = CUBLAS_OP_T;
const cublasOperation_t tB = CUBLAS_OP_N;
...
if (version_major_ >= 11 && version_minor_ >= 11 && version_patch_ > 0 && fastAccum) {
    const int8_t fastAccuMode = 1;  // enable fast imprecise accum
    cublasLtMatmulDescSetAttribute(matmulDesc, CUBLASLT_MATMUL_DESC_FAST_ACCUM, ...);
}
cublasLtMatmulDescSetAttribute(matmulDesc, CUBLASLT_MATMUL_DESC_A_SCALE_POINTER, &devAscalePtr, ...);
cublasLtMatmulDescSetAttribute(matmulDesc, CUBLASLT_MATMUL_DESC_B_SCALE_POINTER, &devBscalePtr, ...);
```

可以看到 FP8 GEMM 的三大特征：(1) A/B 用 `CUDA_R_8F_E4M3`、D 用 `CUDA_R_16BF`；(2) FP32 累加；(3) 通过 scale 指针做反量化，`FAST_ACCUM` 允许「快速但不精确」的累加（牺牲一点精度换吞吐）。

构造时申请 1MB 的 QGEMM workspace（[cublasFP8MMWrapper.cu:22-36](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasFP8MMWrapper.cu#L22-L36)）：

```cpp
#define CUBLAS_WORKSPACE_1MB 1048576
...
cublas_workspace_qgemm_ = allocator_->reMalloc(cublas_workspace_qgemm_, CUBLAS_WORKSPACE_1MB, true);
```

版本检查会因 FP8 MHA 需要更高 cuBLAS（[cublasFP8MMWrapper.cu:76-87](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasFP8MMWrapper.cu#L76-L87)），要求 ≥ 11.11.4。

接口侧声明了几档重载（[cublasFP8MMWrapper.h:54-100](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasFP8MMWrapper.h#L54-L100)）：输出 BF16 的 `Gemm`、输出 FP8 的 `Gemm`（多一个 `output_scale`），以及融合的 `Conv1x1Gemm`、`Gemm_Bias_Act`（[cublasFP8MMWrapper.h:120-150](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasFP8MMWrapper.h#L120-L150)）。

实际调用方（FFN 层）通过 `reinterpret_cast<cublasFP8MMWrapper*>(cublas_wrapper_)` 把基类指针向下转型来访问这些 FP8 接口（[FfnFP8Layer.cc:49-64](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnFP8Layer.cc#L49-L64)）：

```cpp
reinterpret_cast<cublasFP8MMWrapper*>(cublas_wrapper_)
    ->Gemm(inter_buf_bf16_, 1, m, inter_size_, d_model, 0,0,0,
           &alpha, &beta,
           input_hidden_state,                                  // __nv_fp8_e4m3*
           ffn_weights->intermediate_weight.kernel,             // __nv_fp8_e4m3*
           ffn_weights->intermediate_weight.input_scale,        // 激活 scale
           ffn_weights->intermediate_weight.per_channel_scale_min, // identity_scale
           stream_);
```

fp8_mode==2 且开启 `USE_QGMMA` 时，则改走 `Conv1x1Gemm`，把 scale_a / scale_b / scale_d 三个标量传入（[FfnFP8Layer.cc:76-87](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnFP8Layer.cc#L76-L87)），让 Hopper 在一条 QGMMa 指令里完成「乘 + 加偏置 + 激活 + 再量化」。

#### 4.3.4 代码实践

**实践目标**：理解 FP8 GEMM 的接口形态与离线调优入口。

**操作步骤**：

1. 对照 [cublasFP8MMWrapper.h:54-67](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasFP8MMWrapper.h#L54-L67)，写出一个「输出 BF16」的 FP8 GEMM 调用伪代码：标出 `m/n/k`、`input/kernel/input_scale/kernel_scale` 各对应什么。
2. 阅读 [gpt_fp8_gemm.cc:117-130](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/gpt_fp8/gpt_fp8_gemm.cc#L117-L130)，确认 `data_type == 4`（`FP8_DATATYPE`）时调用 `generate_gpt_gemm_config<__nv_fp8_e4m3>`。
3. 解释为什么 FP8 GEMM 比 INT8 GEMM「省心」——为什么 FT 文档说「checkpoint 没有量化信息、用 identity scale 也能跑」？

**需要观察的现象**：FP8 GEMM 的关键参数是**两个 scale 指针**（input_scale、kernel_scale），它们取代了 INT8 里的 `ScaleList` 那一大本「账本」；FP8 因动态范围大，即使 scale 取 1.0（identity）也不会立刻溢出或塌成 0。

**预期结果**：能写出形如 `wrapper->Gemm(out_bf16, 1, M, N, K, ..., fp8_input, fp8_kernel, input_scale, kernel_scale, stream)` 的调用，并指出 M/N/K 的语义。

> 待本地验证：`gpt_fp8_gemm` 的实际运行（如 `./bin/gpt_fp8_gemm 8 4 32 96 128 49152 51200 4 8`，注意第 8 个参数 4 表示 FP8）需在 Hopper 上执行。

#### 4.3.5 小练习与答案

**练习 1**：`CUBLASLT_MATMUL_DESC_FAST_ACCUM`（`fastAccum`）开启会有什么代价？

**参考答案**：它启用 cuBLASLt 的「快速但不精确」累加模式（通常是用低精度中间累加），换取更高吞吐，代价是累加结果有微小数值偏差。FP8 本就精度有限，对误差不敏感的场景（如 FFN 第一段 GEMM）可以开启；对精度敏感的场景应关闭。

**练习 2**：为什么 `cublasFP8MMWrapper` 要继承 `cublasMMWrapper` 而不是独立实现？

**参考答案**：为了最大化复用 u2-l3 已经验证过的 handle、32MB workspace、互斥锁 `mu_`、`cublasAlgoMap` 等基础设施，只在派生类里新增 FP8 专属的 `Gemm`/`Conv1x1Gemm`/`Gemm_Bias_Act` 接口。调用方持有基类指针 `cublasMMWrapper*`，需要 FP8 时向下转型即可，保持了与现有 layer 代码的兼容。

---

### 4.4 GptFP8 / BertFP8 前向结构

#### 4.4.1 概念说明

FT 的 FP8 推理落在两个端到端模型上：

- **GptFP8**：decoder-only 生成模型，沿用 u6-l1 的「context + decoder 两阶段」骨架，把 QKV/FFN 的 GEMM、LayerNorm、加残差、激活都换成 FP8/融合版本。
- **BertFP8**：encoder，沿用 u4-l1/u4-l2 的「去 padding + transformer block」骨架，并引入一个 `fp8_mode_`（1 或 2）控制量化粒度。

两者的共同套路是**模板 `<T1=fp8, T2=bf16>`** + **整网尽量用 FP8 GEMM** + **非 GEMM 算子（layernorm / 激活 / 残差）写专门融合 kernel**，把「反量化 → 计算 → 再量化」压进一次显存读写。

#### 4.4.2 核心流程

GptFP8 的生成主循环（与 ParallelGpt 同构，差异在子组件是 FP8 版）：

```
forward(input_ids):
  ├─ context 阶段（max_input_length > 1）:
  │    embedding lookup+pos编码 → GptFP8ContextDecoder（写满 KV cache）→ finished 初始化
  ├─ 解码循环 step = max_input_length .. max_output_seq_len:
  │    embedding lookup → GptFP8Decoder（单步, 追加 KV cache）
  │    → post LayerNorm → logits GEMM（注意:暂未 FP8, 见 4.4.3）
  │    → DynamicDecodeLayer（beam search / sampling 选 token）
  │    → early stop（finished 全 1 则 break）
  └─ invokeGatherTree 回溯输出
```

BertFP8 的前向（去 padding 贯穿）：

```
forward(input_ids, sequence_lengths):
  ├─ build attention mask / padding offset（去 padding: invokeGetPaddingOffset）
  ├─ invokeRemovePaddingEmbLookupLayerNormFP8Out  ← embedding+layernorm+量化 融合, 输出 FP8
  ├─ for 每层 transformer block:
  │     SelfAttentionFP8Layer.forward   (FP8 GEMM)
  │     invokeGeneralFP8IOAddBiasResidualPostLayerNorm  ← 加偏置+残差+post-LN+再量化 融合
  │     FfnFP8Layer.forward             (FP8 GEMM + 融合激活)
  │     invokeGeneralFP8IOAddBiasResidualPostLayerNorm  ← 同上
  └─ invokeQuantizeMatrixRebuildPadding  ← 反量化+恢复 padding 输出 FP16
```

#### 4.4.3 源码精读

**GptFP8 类骨架**（[GptFP8.h:29-53](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/gpt_fp8/GptFP8.h#L29-L53)）持有三个子组件，结构与 ParallelGpt 完全对应：

```cpp
template<typename T1, typename T2>
class GptFP8: public BaseLayer {
    ...
    const int  int8_mode_ = 0;                  // FP8 模型固定 int8_mode=0
    GptFP8Decoder<T1, T2>*        gpt_decoder_;
    GptFP8ContextDecoder<T1, T2>* gpt_context_decoder_;
    DynamicDecodeLayer<float>*    dynamic_decode_layer_;
```

实例化只有一个特化：FP8 计算 + BF16 存储（[GptFP8.cc:867](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/gpt_fp8/GptFP8.cc#L867)）：

```cpp
template class GptFP8<__nv_fp8_e4m3, __nv_bfloat16>;
```

KV cache 默认存 BF16（`T2`），只有定义了 `FP8_MHA` 才存 FP8（`T1`）——而 `FP8_MHA` 默认被注释（[GptFP8.cc:100-104](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/gpt_fp8/GptFP8.cc#L100-L104)）：

```cpp
#ifdef FP8_MHA
    key_cache_ = (T1*)(allocator_->reMalloc(key_cache_, sizeof(T1) * self_cache_size * 2, true));
#else
    key_cache_ = (T2*)(allocator_->reMalloc(key_cache_, sizeof(T2) * self_cache_size * 2, true));
#endif
```

K cache 的形状与 u6-l2 一致（按 `x = 16/sizeof(T)` 重排以便 16 字节向量化扫描，[GptFP8.cc:311-318](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/gpt_fp8/GptFP8.cc#L311-L318)）。

一个值得注意的「尚未 FP8 化」的点：logits 投影 GEMM 用的是普通 `cublas_wrapper_->Gemm`，源码里直接留了 `// TODO Support FP8 GEMM` 注释（[GptFP8.cc:603-625](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/gpt_fp8/GptFP8.cc#L603-L625)）。也就是说，隐状态先经 `invokeGeneralLayerNorm`（BF16）再以 BF16 输入做 logits GEMM，输出 FP32 logits 给动态解码。这是 FT 的务实取舍：词表投影维度极大（vocab_size × hidden），FP8 化收益与精度风险需要更仔细评估，故先保留 BF16。

**GptFP8Decoder** 单步前向里，每个 transformer block 的第一步是 FP8 LayerNorm（[GptFP8Decoder.cc:258-270](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/gpt_fp8/GptFP8Decoder.cc#L258-L270)）：

```cpp
FP8LayerNormParam<T1, T2> param{
    decoder_normed_input_,                                  // T1* fp8 输出
    (T2*)decoder_input,                                     // T2  bf16 输入
    ...->pre_layernorm_weights.gamma, ...->beta,
    ...->identity_scale,                                    // input_deq_ptr
    ...->self_attention_weights.query_weight.input_scale_inv, // output_qua_ptr
    (int)local_batch_size, (int)hidden_units_, stream_, true};
invokeFP8LayerNorm<T1, T2, 0>(param);
```

子组件是 FP8 专属的注意力与 FFN 层（[GptFP8Decoder.cc:27-41](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/gpt_fp8/GptFP8Decoder.cc#L27-L41)）：

```cpp
self_attention_layer_ = new TensorParallelDecoderSelfAttentionFP8Layer<T1, T2>(...);
ffn_layer_ = new TensorParallelGeluFfnFP8Layer<T1, T2>(inter_size_, tensor_para_, ...);
```

**FP8 LayerNorm kernel** 的精妙之处在「per-tensor 量化下输入 scale 可省略」的数学证明（[layernorm_fp8_kernels.cu:263-282](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/layernorm_fp8_kernels.cu#L263-L282)）。设 \(x' = s \cdot x\)，则：

\[
\text{LN}(x') = \frac{E[x']}{\sqrt{V[x']}} = \frac{sE[x]}{\sqrt{s^2 V[x]}} = \frac{sE[x]}{s\sqrt{V[x]}} = \text{LN}(x)
\]

即整体 scale 在归一化里被约掉，所以 kernel 跳过 `input_scalar` 的乘法省下计算与访存，只在输出端乘 `output_qua_ptr`（再量化回 FP8）。均值/方差仍用 `blockReduceSum`（一个 block 处理一行，承接 u3-l1），最终：

```cpp
result1.x = (((float)local_out1.x - s_mean) * s_variance * gamma[...] + beta[...]) * output_scalar[0];
...
__nv_fp8x4_e4m3 output_val = __nv_fp8x4_e4m3(result1, result2);  // 再量化回 FP8
```

**FP8 激活 kernel** 同样是「反量化 → 加偏置 → 激活 → 再量化」的单遍融合（[activation_fp8_kernels.cu:64-79](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/activation_fp8_kernels.cu#L64-L79)）：

```cpp
T2 val = (T2)((float)(param.out[id]) * __ldg(param.input_scale));  // 反量化到 bf16
if (param.bias != nullptr) { val = val + __ldg(&param.bias[id % param.n]); }
param.out[id] = (T1)((float)gelu(val) * __ldg(param.output_scale)); // 激活后再量化回 fp8
```

BF16 输入时还有 8 元素打包的特化版本（`__nv_bfloat168` / `__nv_fp8_8_e4m3`）以提升带宽（[activation_fp8_kernels.cu:114-143](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/activation_fp8_kernels.cu#L114-L143)）。

**BertFP8** 的差异点是 `fp8_mode_`（[BertFP8.h:40-43](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert_fp8/BertFP8.h#L40-L43)）：

```cpp
// mode 1: per tensor scale for activation, per channel scale for weight
// mode 2: per tensor scale for activation and weight
int fp8_mode_;
```

它的前向入口先做「去 padding + embedding + layernorm + 量化」一次性融合（[BertFP8.cc:260-284](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert_fp8/BertFP8.cc#L260-L284)），每层 block 之间用 `invokeGeneralFP8IOAddBiasResidualPostLayerNorm` 把「加偏置 + 残差 + post-LN + 再量化」融成一个 kernel（[BertFP8.cc:364-385](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert_fp8/BertFP8.cc#L364-L385)）。最后因为「TRT 不支持 bfloat 输出」，末尾用 `invokeQuantizeMatrixRebuildPadding` 把 FP8 内部表示反量化成 **FP16** 并恢复 padding 输出（[BertFP8.cc:491-505](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert_fp8/BertFP8.cc#L491-L505)）。实例化同样是 FP8+BF16（[BertFP8.cc:567](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert_fp8/BertFP8.cc#L567)）。

#### 4.4.4 代码实践

**实践目标**：把 FP8 的「数据类型流转」沿着 GptFP8 的 forward 走一遍。

**操作步骤**：

1. 打开 [GptFP8.cc:244-295](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/gpt_fp8/GptFP8.cc#L244-L295)，对照注释里的 input/output 张量约定。
2. 沿调用链画一张「类型流转图」：`input_ids(int)` → embedding lookup 得到 `T2(bf16)` 的 `context_decoder_input_buf_` → context decoder 内部 GEMM 用 `T1(fp8)` → 输出 `T2(bf16)` 的 `decoder_output_buf_` → post-LN（BF16）→ logits GEMM（BF16→FP32）→ DynamicDecode（FP32）→ `output_ids(int)`。在每一处标明是 T1 还是 T2。
3. 找到 [GptFP8.cc:603](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/gpt_fp8/GptFP8.cc#L603) 的 `// TODO Support FP8 GEMM`，解释为什么 logits 投影这一步暂未 FP8。
4. 阅读 [GptFP8Decoder.cc:258-270](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/gpt_fp8/GptFP8Decoder.cc#L258-L270)，确认 FP8 LayerNorm 的输入是 BF16、输出是 FP8。

**需要观察的现象**：整条 forward 上，**GEMM 两侧与中间激活走 FP8（T1），残差流与 cache 默认走 BF16（T2）**。FP8 只在「矩阵乘 + 紧邻的 elementwise」处短暂出现，反量化/再量化都被融进相邻 kernel，避免额外的显存往返。

**预期结果**：得到一张完整的类型流转图，并能指出「logits GEMM 与动态解码仍是 BF16/FP32，是 FP8 化的边界」。

> 待本地验证：实际运行 `examples/pytorch/gpt/gpt_summarization.py --data_type fp8`（见 [docs/gpt_guide.md:960-964](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/gpt_guide.md#L960-L964)）需在 Hopper 机器上完成。

#### 4.4.5 小练习与答案

**练习 1**：GptFP8 默认把 KV cache 存成 BF16 而非 FP8，原因可能是什么？

**参考答案**：源码里 `FP8_MHA` 宏默认被注释（[cuda_fp8_utils.h:24](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cuda_fp8_utils.h#L24)），且开启 FP8 MHA 需要更高版本 cuBLAS（≥ 11.11.4，见 [cublasFP8MMWrapper.cu:82-86](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasFP8MMWrapper.cu#L82-L86)）来支持 d-scale。KV cache 在长序列生成里被反复读取，存 BF16 能避免精度损失累积，是「Experimental」阶段的稳妥选择。

**练习 2**：FP8 LayerNorm 为什么可以「跳过输入 scale」？

**参考答案**：因为 per-tensor scale \(s\) 是常数，\( \text{LN}(sx) = \text{LN}(x) \)（均值与方差各除以 \(s\) 与 \(s^2\)，归一化时约掉）。所以 kernel 不必先反量化输入，只在输出端乘 `output_scale` 完成再量化即可，省一次乘法与访存。

**练习 3**：BertFP8 的输出为什么是 FP16 而不是 BF16？

**参考答案**：源码注释明示「TRT does not support bfloat output now」（[BertFP8.cc:491-505](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert_fp8/BertFP8.cc#L491-L505)），因此末尾用 `invokeQuantizeMatrixRebuildPadding<half, T1, PER_TENSOR>` 把 FP8 内部结果反量化成 FP16 并恢复 padding，兼容 TensorRT plugin 的下游。

---

## 5. 综合实践

**任务：为「在 Hopper 上把一个 GPT 模型从 BF16 切到 FP8」写一份 Checklist，并对每一点给出源码依据。**

请按以下子任务完成：

1. **环境与编译**：写出 cmake 命令，包含 `-DSM=90 -DENABLE_FP8=ON`，并说明若漏掉 `-DSM=90` 会发生什么（提示：`FUSE_GEMM_ACT` 是否还会被定义？参考 4.1.3）。
2. **权重准备**：说明 FT 对「无量化信息的 checkpoint」如何处理（提示：identity scale，参考 [docs/gpt_guide.md:967](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/gpt_guide.md#L967)）；并指出若要自己算 scale，应调用 4.2 里的哪个函数。
3. **GEMM 调优**：写出用 `gpt_fp8_gemm` 生成 FP8 algoMap 的命令（注意 `data_type=4`，参考 [gpt_fp8_gemm.cc:117-130](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/gpt_fp8/gpt_fp8_gemm.cc#L117-L130)）。
4. **精度对照**：用一段话说明 FP8 相比 INT8 w8a8（u9-l1）的精度优势来源——要点包括：浮点 vs 定点、动态范围（`FP8_E4M3_MAX=480` vs 127）、相对精度、对离群点的容忍、是否需要 SmoothQuant 这类校准。
5. **边界识别**：在 GptFP8 的 forward 里找出「尚未 FP8 化」的 GEMM（提示：logits 投影，参考 [GptFP8.cc:603](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/gpt_fp8/GptFP8.cc#L603)），并思考为什么。

**预期产出**：一份 5 条的 Checklist，每条都带源码永久链接与行号作为依据；第 4 条给出至少 3 个对比维度。

> 待本地验证：综合实践的第 2、3 步实际产物（c-model 权重、`gemm_config.in`）与第 5 步的实际性能差异，需在真实 H100 上运行确认。

---

## 6. 本讲小结

- **FP8 = 浮点 8 比特（E4M3）**，`FP8_E4M3_MAX = 480` 远大于 INT8 的 127，动态范围大、对离群点天然容忍，这是它比 INT8 w8a8 精度更好的根本来源。
- **使能三门槛**：CUDA ≥ 11.8（注入宏）+ Hopper sm_90（`FUSE_GEMM_ACT`/`USE_QGMMA`）+ `-DENABLE_FP8=ON`（链接 FP8 目标）；`FP8_MHA` 默认关闭，故 KV cache 默认仍是 BF16。
- **量化哲学不变**：仍是「低精度存储 + 高精度计算」，FP8 GEMM 用 `CUDA_R_8F_E4M3` 输入、`CUDA_R_16BF` 输出、`CUBLAS_COMPUTE_32F` 累加，靠 A/B scale 指针还原数值。
- **`cublasFP8MMWrapper`** 继承 `cublasMMWrapper`，新增 FP8 `Gemm`、`Conv1x1Gemm`（Hopper QGMMa）、`Gemm_Bias_Act`，并支持 `FAST_ACCUM` 快速累加；算法仍走 `cublasAlgoMap`，键用 `FP8_DATATYPE=4`。
- **GptFP8 / BertFP8** 沿用既有 context/decoder 或去 padding 骨架，把 GEMM、LayerNorm、激活、加残差替换为 FP8/融合 kernel（`invokeFP8LayerNorm`、`invokeFP8AddBiasGelu`、`invokeGeneralFP8IOAddBiasResidualPostLayerNorm`），模板实例化为 `<__nv_fp8_e4m3, __nv_bfloat16>`。
- **边界**：logits 投影 GEMM 在 GptFP8 中暂未 FP8（源码留 `// TODO Support FP8 GEMM`），动态解码仍走 FP32；FP8 LayerNorm 利用 \(\text{LN}(sx)=\text{LN}(x)\) 跳过输入 scale 省计算。

---

## 7. 下一步学习建议

- **横向对比量化路线**：回头读 [u9-l1 INT8 量化](./u9-l1-int8-quantization.md) 与（讲义就绪后）u9-l2 weight-only/CUTLASS，把 INT8 / weight-only INT4 / FP8 三者的「存储精度、计算精度、是否需要校准、适用 batch」整理成一张对比表。
- **深入 Hopper 专属优化**：阅读 `3rdparty/fp8_qgmma_1x1/` 与 `cublasFP8MMWrapper.cu` 中 `Conv1x1Gemm` 的实现，理解 QGMMa 1×1 如何把 GEMM+bias+激活压成一条指令。
- **跟踪 FP8 的演进**：本仓库 FP8 为 Experimental；FT 后续迁移到 TensorRT-LLM 后 FP8（含 E5M2、d-scale/amax 的成熟方案）成为一等公民，可对照 TRT-LLM 的 FP8 实现理解 `FP8_MHA`、per-tensor d-scale 等本讲被注释/简化掉的机制。
- **实践**：在 H100 上按 [docs/gpt_guide.md:940-974](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/gpt_guide.md#L940-L974) 跑通 `gpt_summarization.py --data_type fp8`，对比 BF16/FP8 的 rouge 指标与延迟，验证「identity scale 仍保精度」的结论。

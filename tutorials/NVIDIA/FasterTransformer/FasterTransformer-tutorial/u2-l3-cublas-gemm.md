# 矩阵乘骨干：cublasMMWrapper 与 GEMM

## 1. 本讲目标

学完本讲后，你应该能够：

- 理解 FasterTransformer（后文简称 FT）为什么要把 cuBLAS / cuBLASLt 封装成 `cublasMMWrapper`，以及它内部持有哪些「重量级资源」。
- 看懂 `Gemm` 一族重载函数的入参（`transa/transb/m/n/k/lda/ldb/ldc`），并知道同一个 `Gemm` 名字背后对应着几条不同的执行路径。
- 说出 FP16 默认走 cuBLASLt、FP32 默认走 cuBLAS 的原因，以及 `cublasAlgoMap` 在其中扮演的角色。
- 掌握行主序（row-major）与列主序（column-major）之间的转换技巧，理解为什么 `Gemm` 类内部要「交换 A 和 B、交换 m 和 n」。
- 区分普通 `Gemm`、`batchedGemm`、`stridedBatchedGemm` 三类矩阵乘的使用场景。

## 2. 前置知识

### 2.1 什么是 GEMM

GEMM（GEneral Matrix Multiply）即通用矩阵乘，计算：

\[
C = \alpha \cdot \mathrm{op}(A) \cdot \mathrm{op}(B) + \beta \cdot C
\]

其中 \(\mathrm{op}(X)\) 表示对矩阵 \(X\) 「转置」或「不转置」，\(\alpha\)、\(\beta\) 是标量。Transformer 模型里几乎所有的「大计算量」操作（QKV 投影、attention 输出投影、FFN 的两层全连接、最终 logits 投影）本质上都是 GEMM。所以 GEMM 跑得快不快，直接决定了整个推理速度。

### 2.2 cuBLAS 与 cuBLASLt 是什么

它们都是 NVIDIA 提供的 GPU 线性代数库：

- **cuBLAS**（`cublas_v2.h`）：经典接口，函数如 `cublasGemmEx`、`cublasGemmBatchedEx`。稳定、覆盖广。
- **cuBLASLt**（`cublasLt.h`）：cuBLAS 的「轻量/高级」版本，函数如 `cublasLtMatmul`。它允许你**手动指定算法**（tile 大小、split-K、stages 等），还支持 epilogue（在矩阵乘之后顺手加 bias、做激活），在 FP16/混合精度下往往能比经典 cuBLAS 更快。

两者都需要先创建一个 **handle**（`cublasHandle_t` / `cublasLtHandle_t`），handle 内部缓存了 GPU 上下文、工作区等信息，创建代价较高，因此通常**全程只创建一次、反复使用**。

### 2.3 列主序 vs 行主序

这是本讲最容易踩坑的点。cuBLAS 沿袭 Fortran 习惯，**默认矩阵按列存放（column-major）**；而 C/C++ 数组按行存放（row-major）。一个行主序的矩阵在内存里的排布，恰好等于它的转置按列主序的排布。利用这一点，可以用列主序的 cuBLAS 去算行主序的矩阵乘，但**需要交换一些参数**——本讲 4.3 会用真实代码讲清楚这个技巧。

> 前置讲义回顾：本讲承接 [u2-l1] 的 `DataType`/`getTensorType` 与 [u2-l2] 的 `IAllocator`/`reMalloc`。`cublasMMWrapper` 正是用 `IAllocator` 来分配自己的工作区显存的。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/fastertransformer/utils/cublasMMWrapper.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasMMWrapper.h) | `cublasMMWrapper` 类声明：持有 handle/stream/mutex/工作区，声明 `Gemm`/`batchedGemm`/`stridedBatchedGemm` 等接口。**这是 FT 真正大量使用的 GEMM 入口。** |
| [src/fastertransformer/utils/cublasMMWrapper.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasMMWrapper.cc) | 上述接口的实现，包含 cuBLASLt vs cuBLAS 的路径选择与算法装配。 |
| [src/fastertransformer/utils/gemm.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/gemm.h) | 一个**更「行主序友好」的并行抽象** `Gemm`（及稀疏版 `SpGemm`），自带行/列主序转换、工厂函数 `createGemm` 和类型映射工具。 |
| [src/fastertransformer/utils/gemm.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/gemm.cc) | `Gemm` 的实现，其中能看到最清晰的「交换 A/B、交换 m/n」的列主序技巧。 |
| [src/fastertransformer/utils/cuda_utils.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cuda_utils.h) | 定义工作区大小常量 `CUBLAS_WORKSPACE_SIZE` 与 `CublasDataType` 枚举。 |

> 阅读提示：FT 里**真正被各 layer/model 大量调用的是 `cublasMMWrapper`**；`gemm.h/.cc` 里的 `Gemm` 类是一套更「干净」的并行设计（注释里仍有不少 TODO，部分基于 `Tensor` 的接口被注释掉，属于演进中的抽象）。本讲以 `cublasMMWrapper` 为主线，用 `Gemm` 类来帮助讲清「行列主序转换」这个关键概念。

## 4. 核心概念与源码讲解

### 4.1 整体封装：handle、工作区与互斥锁

#### 4.1.1 概念说明

直接调用 `cublasLtMatmul` 需要写很长一段样板代码：创建描述符（descriptor）、设置转置属性、装配算法（algo）、传入工作区（workspace）、最后销毁描述符。如果每个 layer 都自己写一遍，代码会极其冗余且容易出错。

`cublasMMWrapper` 的核心价值就是把这些样板代码**收拢成一个可复用的对象**，它持有三类「贵重资源」：

1. **cuBLAS / cuBLASLt 的 handle**：创建昂贵，全程共享。
2. **工作区显存** `cublas_workspace_`：cuBLASLt 的很多高速算法需要一块临时显存（用于 split-K 归约等），固定 32MB。
3. **互斥锁** `mu_`：cuBLAS handle 不是线程安全的，多线程复用同一个 handle 必须加锁。

#### 4.1.2 核心流程

构造一个 `cublasMMWrapper` 的流程：

1. 外部先创建好 `cublasHandle_t`、`cublasLtHandle_t`、`cudaStream_t`、`cublasAlgoMap*`、`std::mutex*`、`IAllocator*`。
2. 把它们传给构造函数，wrapper 仅保存指针，**不负责创建 handle**（handle 的生命周期由外部管理，这样多张卡、多个 wrapper 可以共享同一套 handle）。
3. 构造函数用 `IAllocator::reMalloc` 申请 32MB 工作区。
4. 析构时只释放工作区，把 `allocator_` 置空，但**不销毁 handle**。

#### 4.1.3 源码精读

类成员，注意它持有的全部是指针/句柄，没有大块数据：

[cublasMMWrapper.h:30-51](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasMMWrapper.h#L30-L51) — `cublas_handle_`、`cublaslt_handle_`、`stream_`、`cublas_algo_map_`、`mu_`、`allocator_`、`cublas_workspace_`，以及四个 `cudaDataType_t` 成员（`Atype_/Btype_/Ctype_/computeType_`）记录「当前 GEMM 的数据类型配置」。

构造函数：保存外部传入的指针，并用 allocator 申请 32MB 工作区：

[cublasMMWrapper.cc:25-42](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasMMWrapper.cc#L25-L42) — 关键行 `cublas_workspace_ = allocator_->reMalloc(cublas_workspace_, CUBLAS_WORKSPACE_SIZE, false);`，其中 `CUBLAS_WORKSPACE_SIZE` 在 [cuda_utils.h:39](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cuda_utils.h#L39) 定义为 `33554432`（即 32MB）。第二个参数 `false` 表示这块 buffer **不是「带预设值的」**（参见 [u2-l2] 的 `reMalloc` 语义）。

析构函数只回收工作区，不动 handle：

[cublasMMWrapper.cc:67-75](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasMMWrapper.cc#L67-L75) — `allocator_->free((void**)(&cublas_workspace_))`，并把 `mu_`、`allocator_` 置空。

#### 4.1.4 代码实践

**实践目标**：理解「handle 外部创建、工作区内部申请」的分工，以及工作区大小从哪里来。

**操作步骤**：

1. 打开 [cublasMMWrapper.cc:25-42](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasMMWrapper.cc#L25-L42)，确认构造函数里**没有任何** `cublasCreate` / `cublasLtCreate` 调用——handle 是外部传进来的。
2. 对比 `Gemm` 类的构造函数 [gemm.cc:23-36](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/gemm.cc#L23-L36)，它**自己**调用了 `cublasCreate(&cublas_handle_)` 和 `cublasLtCreate(&cublaslt_handle_)`。

**需要观察的现象**：两套封装对 handle 生命周期的管理策略不同——`cublasMMWrapper` 是「借用者」，`Gemm` 是「拥有者」。

**预期结果**：能用自己的话回答「为什么 FT 选让 `cublasMMWrapper` 借用而非拥有 handle」——因为多张 GPU、多个 layer 希望共享同一套 handle，由更上层（如 `Allocator` 或模型对象）统一创建销毁更可控。

#### 4.1.5 小练习与答案

**练习 1**：`cublasMMWrapper` 的拷贝构造函数（[cublasMMWrapper.cc:77-92](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasMMWrapper.cc#L77-L92)）为什么在拷贝后还要再 `reMalloc` 一次工作区，而不是直接共享 `cublas_workspace_` 指针？

**答案**：因为 cuBLASLt 在执行时会向工作区里写中间结果，多个 wrapper 若共享同一块工作区，并发调用就会互相覆盖。拷贝构造让每个 wrapper 拥有**独立的工作区显存**，但**共享 handle/algo_map/mutex**，从而既省去重建 handle 的开销，又保证工作区互不干扰。

---

### 4.2 Gemm 接口族与 cuBLASLt / cuBLAS 路径选择

#### 4.2.1 概念说明

`cublasMMWrapper` 用**函数重载**提供了好几个同名 `Gemm`，参数越少越「省心」（用默认配置），参数越多越「可控」（显式指定数据类型、算法）。它们最终都会落到 cuBLASLt 的 `cublasLtMatmul` 或经典 cuBLAS 的 `cublasGemmEx` 上。

关键设计决策：

- **FP16 默认走 cuBLASLt，FP32 默认走经典 cuBLAS**。因为 cuBLASLt 在 FP16 下能选到更快的算法（且能装配 split-K、stages 等）。
- 是否真的走 cuBLASLt，还会参考 `cublasAlgoMap` 里**有没有针对当前 (batch, m, n, k, 数据类型) 离线调优过的算法**。这部分细节属于下一讲 [u2-l4]，本讲只需知道「查得到算法 → 用 cuBLASLt 并装配该算法；查不到 → 让库自己挑」。

#### 4.2.2 核心流程

以最常用的「13 参数版」`Gemm(..., f_alpha, f_beta)` 为例，流程是：

1. 把 `f_alpha/f_beta` 转成 `half` 备用（FP16 计算时用 half 指针）。
2. 判断 `is_fp16_computeType`（计算精度是否 FP16）和 `using_cublasLt`（输入是否 FP16）。
3. 用 `(batch=1, m, n, k, 数据类型)` 去 `cublas_algo_map_` 查算法。
4. 若查到算法且 `stages != -1` → 走 cuBLASLt 分支：建描述符、装配算法属性、调用 `cublasLtMatmul`。
5. 否则走 cuBLAS 分支：直接调用 `cublasGemmEx`，并用 `info.algoId` 作为算法枚举。
6. 全程被 `mu_->lock()` / `mu_->unlock()` 包裹。

#### 4.2.3 源码精读

三个重载的声明，从「最灵活」到「最省心」：

[cublasMMWrapper.h:131-174](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasMMWrapper.h#L131-L174) — 分别是：
- 18 参数版：显式给出每个矩阵的 `cudaDataType_t` 和 `computeType`、`algo`，**直接调 `cublasGemmEx`**；
- 11 参数版：省略 alpha/beta（默认 1.0/0.0）；
- 13 参数版：带 `f_alpha/f_beta`，是 FP16/FP32 的主路径。

11 参数版只是简单转发：

[cublasMMWrapper.cc:138-152](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasMMWrapper.cc#L138-L152) — `Gemm(..., C, ldc)` 内部直接调用 `Gemm(..., C, ldc, 1.0f, 0.0f)`。

18 参数版直接打 cuBLAS，最简单（注意全程加锁）：

[cublasMMWrapper.cc:94-136](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasMMWrapper.cc#L94-L136) — `mu_->lock()` → `cublasGemmEx(...)` → `sync_check_cuda_error()` → `mu_->unlock()`。

13 参数版是本讲重点，先看路径选择：

[cublasMMWrapper.cc:172-192](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasMMWrapper.cc#L172-L192) — `using_cublasLt = (Atype_ == CUDA_R_16F) ? true : false;`（FP16 输入默认走 Lt），随后用 `cublas_algo_map_->isExist(...)` 与 `getAlgo(...)` 查离线调优结果；若查到且 `info.stages != -1` 则确认走 Lt，否则退回经典 cuBLAS。

cuBLASLt 分支（建描述符、装配算法、调用）：

[cublasMMWrapper.cc:194-303](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasMMWrapper.cc#L194-L303) — 其中 [L223-L225](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasMMWrapper.cc#L223-L225) 根据转置标志决定每个矩阵的逻辑行列数；[L243-L261](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasMMWrapper.cc#L243-L261) 把 `cublasAlgoMap` 里记录的 `algoId/tile/splitK/swizzle/stages` 等属性逐个写进 `cublasLtMatmulAlgo_t`；最后 [L281-L296](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasMMWrapper.cc#L281-L296) 调用 `cublasLtMatmul`，并把 `cublas_workspace_` 作为工作区传入。

cuBLAS 回退分支：

[cublasMMWrapper.cc:304-327](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasMMWrapper.cc#L304-L327) — 直接 `cublasGemmEx`，用 `info.algoId` 作为 `cublasGemmAlgo_t`。

数据类型配置由这几个 setter 控制，注意 FP16/BF16 的**存储**与**累加**精度不同：

[cublasMMWrapper.cc:330-354](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasMMWrapper.cc#L330-L354) — `setFP16GemmConfig` 把 `Atype_/Btype_/Ctype_` 设成 `CUDA_R_16F`，但 `computeType_` 设成 `CUDA_R_32F`，即 **FP16 存储但用 FP32 累加**（更稳的数值范围）；`setBF16GemmConfig` 同理，且受 `#ifdef ENABLE_BF16` 守护。

#### 4.2.4 代码实践

**实践目标**：写出一个 FP16 矩阵乘的调用伪代码，理解 M/N/K 与 leading dimension 的含义，并解释 cuBLAS handle 为何在多线程下要小心共享。

**操作步骤**：假设要计算 `C(M×N) = A(M×K) · B(K×N)`，A、B、C 都是 FP16 行主序，存放在 GPU 显存。对照 [cublasMMWrapper.h:162-174](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasMMWrapper.h#L162-L174) 的 13 参数 `Gemm` 写伪代码。

**示例代码**（非项目原码，仅为说明）：

```cpp
// 目标：C[M][N] = A[M][K] * B[K][N]   （行主序视角，FP16）
const int M = 32, K = 64, N = 128;

// 1. 先告诉 wrapper：本次是 FP16 配置（FP16 存储 + FP32 累加）
cublasMMWrapper* cublas_wrapper = ...;          // 已构造好
cublas_wrapper->setFP16GemmConfig();            // 见 cublasMMWrapper.cc:338-344

// 2. 计算 leading dimension（行主序下 lda = 一行有几个元素）
//    注意：cuBLAS 是列主序，FT 的 layer 在调用 cublasMMWrapper 时
//    已按 cuBLAS 约定传参，这里给出最常见的 “输入不转置、权重转置” 写法。
cublasOperation_t op_N = CUBLAS_OP_N;
cublasOperation_t op_T = CUBLAS_OP_T;

cublas_wrapper->Gemm(op_T,   // transa：A(权重) 转置
                     op_N,   // transb：B(激活) 不转置
                     M,      // m
                     N,      // n
                     K,      // k
                     weight, // A 指针
                     K,      // lda
                     input,  // B 指针
                     K,      // ldb
                     output, // C 指针
                     N,      // ldc
                     1.0f,   // alpha
                     0.0f);  // beta
```

**需要观察的现象**：

- `setFP16GemmConfig()` 之后，wrapper 内部的 `Atype_/Btype_/Ctype_/computeType_` 就固定了，后续 `Gemm` 不必再传数据类型。
- 上述 `Gemm` 进入 [cublasMMWrapper.cc:154-328](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasMMWrapper.cc#L154-L328) 的 13 参数实现，因为 `Atype_ == CUDA_R_16F`，`using_cublasLt` 初始为 `true`，于是走 cuBLASLt 分支。

**预期结果 / 待本地验证**：能否在真实 GPU 上跑通取决于你是否正确传入了 `cublas_handle`、`cublaslt_handle`、stream 与一块 ≥32MB 的 allocator 工作区。若无 GPU 环境，可只做「源码阅读型实践」：跟踪一次 `Gemm` 调用，确认它最终进入 `cublasLtMatmul` 还是 `cublasGemmEx`，并记下依据的分支条件。

**关于 handle 多线程共享**：cuBLAS / cuBLASLt 的 handle **不是线程安全**的——同一个 handle 被两个线程同时用来发 GEMM，会因内部状态被并发改写而出错甚至崩溃。`cublasMMWrapper` 用一把 `std::mutex mu_`（[cublasMMWrapper.h:48](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasMMWrapper.h#L48)）保护，每次 `Gemm` 都 `mu_->lock()` ... `mu_->unlock()`（例如 [cublasMMWrapper.cc:114-135](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasMMWrapper.cc#L114-L135)）。代价是：多线程复用同一 wrapper 时 GEMM 实际上是**串行**的。这也是为什么 FT 在多流/异步场景（如 [u6-l3] 的 async 生成）里会给每个流**单独建一个 wrapper**（甚至单独的 handle），从而绕开这把锁、真正并发。

#### 4.2.5 小练习与答案

**练习 1**：`setFP16GemmConfig()` 把 `computeType_` 设成了 `CUDA_R_32F`（[cublasMMWrapper.cc:338-344](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasMMWrapper.cc#L338-L344)），而 `is_fp16_computeType` 的判断是 `computeType_ == CUDA_R_16F`（[cublasMMWrapper.cc:174](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasMMWrapper.cc#L174)）。那么默认 FP16 配置下 `is_fp16_computeType` 是 true 还是 false？这意味着 alpha/beta 用的是 float 还是 half？

**答案**：是 `false`（因为 `computeType_` 是 `CUDA_R_32F` 而非 `CUDA_R_16F`）。因此 alpha/beta 走 `reinterpret_cast<void*>(&f_alpha)` 那一支，用的是 **float** 指针。这正是「FP16 存储、FP32 累加」配置下的预期行为：缩放因子用更高精度。

**练习 2**：如果 `cublas_algo_map_->isExist(...)` 返回 false（没找到离线调优算法），`Gemm` 会走哪条路径？

**答案**：`findAlgo` 为假，`using_cublasLt` 仍由 `(Atype_ == CUDA_R_16F)` 决定。若输入是 FP16，仍会走 cuBLASLt 分支，但在 [cublasMMWrapper.cc:293](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasMMWrapper.cc#L293) 把算法指针传成 `NULL`，即让 cuBLASLt **自己用启发式挑算法**，而不是装配离线调优的那个。

---

### 4.3 矩阵布局转换：行主序与列主序的 trick

#### 4.3.1 概念说明

4.2 里的 `cublasMMWrapper::Gemm` **没有**做行列主序转换——它的调用方（各个 layer）早已按 cuBLAS 的列主序约定来传参了。而 `gemm.h/.cc` 里的 `Gemm` 类走的是另一条路线：它对外承诺「A、B、C 都是行主序，调用者不用关心 cuBLAS 的列主序约定」，**由它自己在内部完成转换**。

这个转换的数学依据是：

\[
C = A \cdot B \quad\Longleftrightarrow\quad C^{\mathsf{T}} = B^{\mathsf{T}} \cdot A^{\mathsf{T}}
\]

因为「行主序的矩阵在内存里的排布」=「它的转置按列主序的排布」。所以：要把行主序的 \(A\cdot B\) 喂给列主序的 cuBLAS，只需让 cuBLAS 去算 \(B^{\mathsf{T}}\cdot A^{\mathsf{T}}\)——也就是**把 A 和 B 的角色对调、把两个转置标志对调、把 m 和 n 对调、把 lda 和 ldb 对调**。

#### 4.3.2 核心流程

`Gemm::gemm`（最完整版本，[gemm.cc:208-370](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/gemm.cc#L208-L370)）：

1. **交换数据指针**：`a_data_ptr = B; b_data_ptr = A;`
2. **交换转置标志**：`a_op = getCublasOperation(transb); b_op = getCublasOperation(transa);`
3. **交换尺寸**：`_m = n; _n = m;`
4. **交换 leading dimension**：`_lda = ldb; _ldb = lda;`
5. 之后的逻辑与 `cublasMMWrapper` 类似：查 algoMap、走 cuBLASLt 或 cuBLAS。

#### 4.3.3 源码精读

`Gemm` 类在头文件里明确标注了它的「行主序友好」承诺：

[gemm.h:61-68](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/gemm.h#L61-L68) — 注释写道：「A, B, C are assumed to have a row major layout... a family of Gemm has already handled such discrepancy internally. Please use naively without a trick like switching inputs A and B」。换句话说：**别自己手动交换 A/B，类已经帮你做了**。

转换 trick 的真实代码：

[gemm.cc:228-246](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/gemm.cc#L228-L246) — 这就是上面四步对应的实现，配着注释「Switch A and B since both cublas and cublasLt assume a column major layout, while A and B are both row major layout.」

`gemm()` 完整接口的文档（讲清 m/n/k/lda/ldb/ldc 的含义）：

[gemm.h:213-251](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/gemm.h#L213-L251) — 其中 `m` 是 op(A) 与 C 的行数，`n` 是 op(B) 与 C 的列数，`k` 是 op(A) 的列数 / op(B) 的行数；`lda/ldb/ldc` 是各矩阵的 leading dimension（一行的元素个数，行主序下）。

#### 4.3.4 代码实践

**实践目标**：用一张表把「行主序视角」和「列主序 cuBLAS 实际执行」对应起来，彻底理解这个 trick。

**操作步骤**：阅读 [gemm.cc:228-246](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/gemm.cc#L228-L246)，填写下表（假设用户想算 `C = op(A)·op(B)`，A、B、C 行主序）：

| 调用者视角（行主序） | cuBLASLt 实际收到的（列主序） |
| --- | --- |
| 矩阵 A 的数据指针 | `B` 的指针 |
| 矩阵 B 的数据指针 | `A` 的指针 |
| m（A 的行数） | `n` |
| n（B 的列数） | `m` |
| transa | `getCublasOperation(transb)` |
| transb | `getCublasOperation(transa)` |
| lda | `ldb` |
| ldb | `lda` |

**需要观察的现象**：四个「交换」是**成对**出现的——指针、转置标志、尺寸、leading dimension 必须同时交换，少一个都会算出错误结果。

**预期结果**：能口述出「为什么交换后等价」——因为 \(C = A B \iff C^{\mathsf{T}} = B^{\mathsf{T}} A^{\mathsf{T}}\)，而行主序矩阵在内存里就是它转置的列主序形式。

#### 4.3.5 小练习与答案

**练习 1**：`cublasMMWrapper::Gemm`（4.2）**没有**做这个交换，而 `Gemm::gemm`（本节）做了。这对调用者意味着什么？

**答案**：调用 `cublasMMWrapper` 时，调用者必须**自己**按 cuBLAS 列主序约定来组织参数（FT 的 layer 都是这样写的，例如权重矩阵预先以「转置」形式存放，调用时传 `CUBLAS_OP_T`）。调用 `Gemm` 类时，调用者可以**按行主序直观地**传 A、B、C，类内部自动转换。前者性能与控制力更强、但心智负担重；后者接口更友好、是更「现代」的封装方向。

**练习 2**：`gemm.cc` 里有个工具函数 `getCublasOperation(GemmOp op)`（[gemm.cc:1062-1072](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/gemm.cc#L1062-L1072)），它把 FT 自己的 `GemmOp`（`GEMM_OP_N/GEMM_OP_T`，定义在 [gemm.h:50-53](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/gemm.h#L50-L53)）映射到 cuBLAS 的 `CUBLAS_OP_N/CUBLAS_OP_T`。为什么不直接用 cuBLAS 的枚举？

**答案**：为了让 `Gemm`/`SpGemm` 这套抽象**与后端解耦**——同一套 `GemmOp` 既能映射到 cuBLAS（`getCublasOperation`），也能映射到 cuSPARSELt（`getCusparseOperation`，[gemm.cc:1086-1096](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/gemm.cc#L1086-L1096)）。这样上层代码不必关心底层用的是稠密还是稀疏库。

---

### 4.4 数据类型映射与 batched / stridedBatched GEMM

#### 4.4.1 概念说明

除了「单次」矩阵乘，Transformer 里还常遇到**一批同样形状的矩阵乘**：

- **batchedGemm**：每个矩阵有**独立的指针**（`A[i]`、`B[i]`、`C[i]` 是指针数组），适合各矩阵分散存放。
- **stridedBatchedGemm**：矩阵在内存里**等距排布**，用「步长 stride」描述第 i 个和第 i+1 个矩阵的偏移，适合连续存放（如 multi-head attention 里把 head 维当作 batch）。

此外，FT 内部有两套「类型标识」需要互转：

- `DataType`（FT 自己的枚举，如 `TYPE_FP16/TYPE_FP32`，见 [u2-l1]）。
- `cudaDataType_t`（cuBLAS 的枚举，如 `CUDA_R_16F/CUDA_R_32F`）。
- `CublasDataType`（FT 在 [cuda_utils.h:49-52](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cuda_utils.h#L49-L52) 定义的「简化版」枚举：`FLOAT_DATATYPE=0, HALF_DATATYPE=1, BFLOAT16_DATATYPE=2`），用作 `cublasAlgoMap` 查算法时的 key。

#### 4.4.2 核心流程

- `stridedBatchedGemm`：用 `cublasGemmStridedBatchedEx`，参数里多了 `strideA/strideB/strideC` 与 `batch_count`，算法同样从 `cublas_algo_map_` 查。
- `batchedGemm`：用 `cublasGemmBatchedEx`，参数里 A/B/C 是指针的指针。
- 类型映射：`getCublasDataType`（两处实现：`cublasMMWrapper` 的成员版 [cublasMMWrapper.cc:367-381](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasMMWrapper.cc#L367-L381)，以及 `gemm.cc` 的自由函数版 [gemm.cc:1024-1034](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/gemm.cc#L1024-L1034)）把 `cudaDataType_t` 翻译成 `CublasDataType`，是查 algoMap 的关键桥梁。

#### 4.4.3 源码精读

`stridedBatchedGemm`（简版，11 个核心参数）：

[cublasMMWrapper.cc:463-516](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasMMWrapper.cc#L463-L516) — 调用 `cublasGemmStridedBatchedEx`，其中 [L489](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasMMWrapper.cc#L489) 用 `getCublasDataType(Atype_)` 查到 batch 维的算法 `info`，再传 `info.algoId`。注意它**只支持经典 cuBLAS**（`cublasGemmStridedBatchedEx`），不走 cuBLASLt。

`batchedGemm`（指针数组版）：

[cublasMMWrapper.cc:577-623](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasMMWrapper.cc#L577-L623) — A/B/C 是 `const void* const*` / `void* const*`（指针数组），调用 `cublasGemmBatchedEx`。

类型映射（成员函数版）：

[cublasMMWrapper.cc:367-381](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasMMWrapper.cc#L367-L381) — `CUDA_R_16F → HALF_DATATYPE`、`CUDA_R_32F → FLOAT_DATATYPE`、`CUDA_R_16BF → BFLOAT16_DATATYPE`（后者受 `ENABLE_BF16` 守护）。

还有一个实用判断 `isFuseBatchGemm`，用来决定「把 batch 维融进一次大 GEMM」是否更划算：

[cublasMMWrapper.cc:625-637](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasMMWrapper.cc#L625-L637) — 比较 `getAlgo(batch_count,...)` 与 3 倍的 `getAlgo(1,...)` 的执行时间，若 batched 版本更快则返回 true。

#### 4.4.4 代码实践

**实践目标**：理解 stride 的含义，能写出一次 strided batched GEMM 的参数。

**操作步骤**：假设有 `batch=8` 个矩阵乘 `C[i](M×N) = A[i](M×K) · B[i](K×N)`，所有 A 连续存放在一块显存里（`A[0]` 之后紧跟 `A[1]`……），B、C 同理。阅读 [cublasMMWrapper.cc:463-516](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasMMWrapper.cc#L463-L516) 后填空。

**示例代码**（非项目原码，仅为说明）：

```cpp
const int batch = 8, M = 16, K = 16, N = 16;
// 行主序下，A[i] 与 A[i+1] 之间相隔 M*K 个元素
int64_t strideA = (int64_t)M * K;
int64_t strideB = (int64_t)K * N;
int64_t strideC = (int64_t)M * N;

cublas_wrapper->setFP16GemmConfig();
cublas_wrapper->stridedBatchedGemm(
    /*transa=*/CUBLAS_OP_N, /*transb=*/CUBLAS_OP_N,
    M, N, K,
    A, /*lda=*/K, strideA,
    B, /*ldb=*/N, strideB,
    C, /*ldc=*/N, strideC,
    /*batchCount=*/batch,
    1.0f, 0.0f);
```

**需要观察的现象**：`stride` 的单位是**元素个数**而非字节（参见 [gemm.h:382-390](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/gemm.h#L382-L390) 的文档说明 "An offset in number of elements"）。

**预期结果**：能解释为什么 `strideA = M*K`——因为行主序下第 i 个 A 矩阵占用 `M*K` 个元素，下一个矩阵紧跟其后。

#### 4.4.5 小练习与答案

**练习 1**：`stridedBatchedGemm` 走的是 `cublasGemmStridedBatchedEx`（经典 cuBLAS），而单个 `Gemm` 在 FP16 下默认走 `cublasLtMatmul`（cuBLASLt）。为什么 batched 版本不用 cuBLASLt？

**答案**：这是历史与覆盖度原因——经典 cuBLAS 的 `cublasGemmStridedBatchedEx` 接口成熟稳定、对 batched 场景支持充分；cuBLASLt 的 batched 能力与算法调优在早期版本覆盖不如经典版全。FT 选择在 batched 场景用经典 cuBLAS，在单次 FP16 GEMM 用 cuBLASLt 以拿到更快算法，是一种务实的取舍。

**练习 2**：`getCublasDataType` 把 `CUDA_R_16F` 映射成 `HALF_DATATYPE`（值=1）。这个 `HALF_DATATYPE` 接下来会被用在哪？

**答案**：作为 `cublasAlgoMap->isExist / getAlgo(batch_count, m, n, k, data_type)` 的入参之一（见 [cublasMMWrapper.cc:182](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasMMWrapper.cc#L182)），即「数据类型」是离线调优算法表的一个 key 维度——同一个 (m,n,k)，FP16 与 FP32 的最优算法通常不同。

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「读源码 + 画调用链」的任务：

**任务**：假设你想在 FT 里加一个新的 GEMM 调用，计算 `Y = X · W`（X 是 `[batch, hidden]` 的 FP16 激活，W 是 `[hidden, inter]` 的 FP16 权重，Y 是 `[batch, inter]`）。请：

1. **选择入口**：你会用 `cublasMMWrapper::Gemm` 还是 `Gemm::gemm`？分别说明理由（提示：哪个被 layer 广泛使用、哪个是行主序友好的实验性抽象）。
2. **配置类型**：写出调用前需要先调用的 setter（参考 [cublasMMWrapper.cc:330-354](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasMMWrapper.cc#L330-L354)）。
3. **确定参数**：写出 m/n/k 与 lda/ldb/ldc 的值（假设你用 `cublasMMWrapper`，并按 cuBLAS 列主序约定传 `OP_T/OP_N`）。
4. **跟踪路径**：根据 [cublasMMWrapper.cc:172-192](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasMMWrapper.cc#L172-L192)，判断这次调用会进入 cuBLASLt 分支还是 cuBLAS 分支，并说明依据。
5. **线程安全**：如果在两个 CPU 线程里同时通过**同一个** `cublasMMWrapper` 发起这次 GEMM，会发生什么？为什么？如何避免？

**参考要点**：

1. 生产代码用 `cublasMMWrapper::Gemm`（FT 所有 layer 都用它，且与 `cublasAlgoMap` 调优体系打通）；`Gemm::gemm` 适合「我希望按行主序直观写、不想管列主序约定」的场景。
2. `cublas_wrapper->setFP16GemmConfig();`。
3. 取 `m=batch, n=inter, k=hidden`；权重 W 以转置形式存放时，常见写法是 `Gemm(OP_T, OP_N, m, n, k, W, /*lda=*/hidden, X, /*ldb=*/hidden, Y, /*ldc=*/inter, 1.0f, 0.0f)`（具体 lda/ldb 取决于权重的物理布局，此处仅示意，**待本地按实际权重布局确认**）。
4. 因为 `Atype_ == CUDA_R_16F`，`using_cublasLt` 初值为 true；若 algoMap 里查到该 shape 的算法且 `stages != -1`，则进入 cuBLASLt 分支装配算法；否则可能在 cuBLASLt 下传 `NULL` algo 让库自选，或退回经典 cuBLAS。
5. 同一 wrapper 的 `mu_` 会把两次调用串行化（不会出错但失去并发）；要真正并发，应为每个线程/流**单独构造一个 wrapper**（甚至独立 handle），从而避免共享同一把锁。

## 6. 本讲小结

- `cublasMMWrapper` 把 cuBLAS/cuBLASLt 的 handle、32MB 工作区、互斥锁、算法表收拢成一个可复用对象，是 FT 真正大量使用的 GEMM 入口。
- 它通过**函数重载**提供从「省心」到「全可控」的多档 `Gemm`；FP16 输入默认走 cuBLASLt，FP32 默认走经典 cuBLAS，最终走哪条还取决于 `cublasAlgoMap` 是否查到离线调优算法。
- FP16/BF16 配置采用「低精度存储 + FP32 累加」（`setFP16GemmConfig` 把 `computeType_` 设为 `CUDA_R_32F`），兼顾速度与数值稳定。
- 行主序与列主序的鸿沟有两种填法：`cublasMMWrapper` 让调用方自己按 cuBLAS 约定传参；`Gemm` 类则在内部用「交换 A/B、交换 m/n、交换 lda/ldb」的 trick 自动转换。
- `batchedGemm`（指针数组）与 `stridedBatchedGemm`（等距步长）覆盖一批同形矩阵乘；stride 单位是「元素个数」。
- cuBLAS handle 非线程安全，`cublasMMWrapper` 用一把 `mu_` 串行化，多线程高并发场景应给每流单独建 wrapper。

## 7. 下一步学习建议

- 下一讲 **[u2-l4] GEMM 算法自动调优：cublasAlgoMap 与 gemm_test** 会讲清本讲反复出现的 `cublas_algo_map_->isExist/getAlgo` 背后的算法表是怎么生成、怎么加载的，建议接着读。
- 想看 `cublasMMWrapper` 真实被怎么调用，可跳到 **[u3-l3] 注意力层** 或 **[u3-l4] FFN 层**，观察 layer 如何在 `forward` 里组织 m/n/k 与 transpose 标志。
- 对稀疏 GEMM（`SpGemm`、2:4 稀疏、`cusparseLt`）感兴趣的话，可直接阅读 [gemm.cc:713-989](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/gemm.cc#L713-L989) 与 [cublasMMWrapper.cc:639-786](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasMMWrapper.cc#L639-L786)（受 `SPARSITY_ENABLED` 守卫），这部分会在后续量化/高性能 GEMM 单元再展开。

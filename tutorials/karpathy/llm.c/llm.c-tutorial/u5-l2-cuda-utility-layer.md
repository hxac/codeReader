# CUDA 工具层：streams、kernel 启动与错误检查

## 1. 本讲目标

本讲聚焦 `train_gpt2.cu` CUDA 主线背后那层「不直接算模型、但没有它整个工程跑不起来」的基础设施代码：`llmc/cuda_common.h`、`llmc/cuda_utils.cuh`、`llmc/cublas_common.h`。

学完本讲你应该能够：

- 说出 `cudaCheck` / `cudaFreeCheck` / `cublasCheck` 这套错误检查宏的作用，并理解它们为什么一律用 `exit` 而非返回错误码。
- 看懂 llm.c 启动一个 CUDA kernel 的「惯用模板」——原生 `<<<grid, block, smem, stream>>>` 三尖括号语法配合 `CEIL_DIV` 便利宏，并能解释 grid/block 是如何由张量大小算出来的。
- 理解 `main_stream` 这条主计算流，以及多卡场景下 `nccl_stream` 这条「通信流」为什么要单独存在、两条流如何用 event 桥接。
- 掌握 cuBLASLt 的 handle / workspace / 计算类型（`cublas_compute`）等全局对象在何处创建、何处销毁、何处被复用。

本讲是 u5-l1（CUDA 主线架构与 `floatX`/`TensorSpec`）的直接后续：u5-l1 讲「模型骨架怎么装配」，本讲讲「装配时反复用到的螺丝刀和扳手」。本讲不展开任何具体模型层的 kernel（layernorm、attention、matmul 各有专讲），只讲这些层共享的工具层。

## 2. 前置知识

阅读本讲前，建议你已经掌握：

- u5-l1 中介绍的 `floatX` 编译期类型别名（随 `PRECISION=BF16/FP16/FP32` 切换为 `__nv_bfloat16`/`half`/`float`），以及「一次性 `cudaMalloc` + 指针排布」的工程技巧。
- CUDA 的基本执行模型：一个 kernel 由若干 **grid** 启动，每个 grid 含若干 **block**，每个 block 含若干 **thread**。三者构成层次结构，硬件上 block 映射到 SM（Streaming Multiprocessor），thread 映射到 SM 内的 CUDA 核心。
- **stream（流）** 的概念：CUDA 把一连串异步操作（kernel 启动、`cudaMemcpyAsync`）排进同一条 stream 后按提交顺序依次执行；**不同 stream 之间可以并发**，这是 GPU 上做「计算-通信重叠」的基础。
- 同步与异步：默认 stream 之外，stream 上的 API 大多是「立即返回、异步执行」的，要等结果必须显式同步（如 `cudaStreamSynchronize`）。

几个本讲会反复出现的术语：

- **handle**：很多 CUDA 库（cuBLAS、cuBLASLt、cuDNN）采用「创建一个 handle、长期复用」的模式。handle 内部缓存了工作区、计划等，反复创建销毁会很慢。
- **workspace**：某些库函数（如 cuBLASLt 的 GEMM）需要一块临时显存来挑选更快的算法，这块显存由调用方提供，称为 workspace。
- **TF32**：Ampere（sm_80）及以上 GPU 支持的一种「fp32 输入、内部用 19 位尾数做矩阵乘」的格式，牺牲少量精度换取大幅加速，相当于 PyTorch 的 `set_float32_matmul_precision('high')`。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
|------|------|-----------|
| [llmc/cuda_common.h](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cuda_common.h) | 所有 CUDA 代码的「公共头」，被几乎所有 `.cuh` 间接包含 | `CEIL_DIV`、`WARP_SIZE`、`cudaCheck`/`cudaFreeCheck`、`deviceProp` 全局声明、`device_to_file`/`file_to_device` 双缓冲 I/O、NVTX profiler 工具 |
| [llmc/cuda_utils.cuh](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cuda_utils.cuh) | `__device__` 端的小工具集合 | `Packed128`（128 位宽存取）、`DType`/`dtype_of`、`warpReduceSum`/`warpReduceMax`/`blockReduce`、`global_sum_deterministic`、`cudaMallocConditionallyManaged`、随机数与随机舍入 |
| [llmc/cublas_common.h](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cublas_common.h) | cuBLAS / cuBLASLt 相关公共定义 | `CUBLAS_LOWP`、`cublaslt_workspace`、`cublas_compute`、`cublaslt_handle`、`cublasCheck` |
| [train_gpt2.cu](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu) | CUDA 主线，工具层的「使用者」 | `deviceProp`/`main_stream` 定义、`common_start`/`common_free`（创建与销毁流和 handle） |
| [llmc/zero.cuh](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/zero.cuh) | 多 GPU（ZeRO + NCCL）支持 | 第二条流 `nccl_stream`、`compute_nccl_sync` event |

## 4. 核心概念与源码讲解

### 4.1 错误检查与 kernel 启动

#### 4.1.1 概念说明

CUDA 的运行时 API 几乎都返回一个 `cudaError_t` 状态码，cuBLAS 函数返回 `cublasStatus_t`。这两类函数「不抛异常、失败只体现在返回值里」，如果调用方不检查，程序会带着错误的状态继续跑下去，产生难以定位的连锁崩溃。

llm.c 的工程取舍是：**只要出错就立刻打印并 `exit(EXIT_FAILURE)`**，绝不吞掉错误继续跑。这符合教学项目的定位——相比「带病运行」，快速失败能让你第一时间看到是哪一行、什么错误。

至于「启动 kernel」，llm.c 并没有像某些框架那样自定义一个形如 `LAUNCH(kernel, grid, block, stream, args...)` 的包装宏。它的做法很直白：用原生 CUDA 的三尖括号启动语法 `kernel<<<grid, block, shared_mem, stream>>>(args...)`，再用 `cuda_common.h` 里的 `CEIL_DIV(M, N)` 便利宏来计算 grid 有多少个 block。换句话说，llm.c 的「kernel 启动辅助宏」实际是 `CEIL_DIV` + 一段几行长的固定模板，而不是一个把启动藏起来的宏。

> ⚠️ 诚实提示：讲义规格里提到「在 `cuda_utils.cuh` 中找到 kernel 启动辅助宏」。实际情况是 `cuda_utils.cuh` 里并没有专门包装 kernel 启动的宏；真正与启动相关的便利工具是定义在 `cuda_common.h`（被 `cuda_utils.cuh` `#include`）里的 `CEIL_DIV`。本讲据此实事求是地讲解，不虚构一个不存在的宏。

#### 4.1.2 核心流程

一个典型的「错误检查 + kernel 启动」流程如下（伪代码）：

```text
1. const int block_size = 256;                 // 每个 block 的线程数，经验值
2. const int grid_size  = CEIL_DIV(N, block_size * pack);  // 由张量元素数 N 反推 grid
3. kernel<<<grid_size, block_size, 0, stream>>>(...args);  // 原生三尖括号启动（异步）
4. cudaCheck(cudaGetLastError());              // 捕获「启动时」错误（参数非法、grid 过大等）
```

要点拆解：

- **`CEIL_DIV(M, N)`** 即向上取整除法 \(\lceil M/N \rceil\)，展开为 \((M + N - 1) / N\)。用「总元素数 ÷ 每个 block 处理的元素数」得到需要多少个 block 才能覆盖全部元素。
- **三尖括号四个槽位**：`<<<grid, block, shared_mem, stream>>>`。llm.c 里 `shared_mem` 一般写 `0`（需要动态共享内存的 kernel 如 `layernorm_backward_kernel10` 才填计算出的字节数）。
- **为什么启动后还要 `cudaGetLastError()`**：`<<<>>>` 本身返回 `void`，而且 kernel 是**异步**启动的——真正「跑完」的错误要等到同步才会暴露。但「启动这一刻」的错误（如 grid 维度超限、显存参数非法）会立即设置一个错误码，必须用 `cudaGetLastError()` 主动取回，否则就被丢了。注意：`cudaGetLastError()` **不**能检测 kernel 内部的运行时错误，那要等同步。

错误检查宏的工作方式：

```text
cudaCheck(expr)
  └─ 展开为 cudaCheck_(expr, __FILE__, __LINE__)
       └─ 若 err != cudaSuccess：打印 [文件:行] + 错误描述，然后 exit
```

把 `__FILE__` / `__LINE__` 通过宏「烘焙」进调用点，错误信息就能直接定位到出问题的那一行源码，无需栈回溯。

#### 4.1.3 源码精读

**错误检查宏**定义在 `cuda_common.h`，是「内联函数 + 一层带 `__FILE__`/`__LINE__` 的包装宏」的两段式结构：

[llmc/cuda_common.h:52-58](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cuda_common.h#L52-L58) —— `cudaCheck_` 真正的实现 + `cudaCheck` 宏。函数名加下划线（`cudaCheck_`）是为了让它既能被宏调用、也能在需要时直接调用（比如 `cudaMallocConditionallyManaged` 里就调用了 `cudaCheck_`）。

```c
inline void cudaCheck_(cudaError_t error, const char *file, int line) {
  if (error != cudaSuccess) {
    printf("[CUDA ERROR] at file %s:%d:\n%s\n", file, line, cudaGetErrorString(error));
    exit(EXIT_FAILURE);
  }
};
#define cudaCheck(err) (cudaCheck_(err, __FILE__, __LINE__))
```

[llmc/cuda_common.h:61-70](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cuda_common.h#L61-L70) —— `cudaFreeCheck` 是「`cudaFree` + 错误检查 + 把指针置空」三合一，模板化以兼容任意指针类型，置空避免悬垂指针。

启动相关的便利宏只有 `CEIL_DIV` 与两个调度常量：

[llmc/cuda_common.h:31-42](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cuda_common.h#L31-L42) —— `WARP_SIZE=32`、`MAX_1024_THREADS_BLOCKS`（Ampere/Hopper 上为 2，其它为 1，用于「让 2 个 block 驻留以掩盖延迟」）、`CEIL_DIV`。

```c
#define WARP_SIZE 32U
#if __CUDA_ARCH__ == 800 || __CUDA_ARCH__ >= 900
#define MAX_1024_THREADS_BLOCKS 2
#else
#define MAX_1024_THREADS_BLOCKS 1
#endif
#define CEIL_DIV(M, N) (((M) + (N)-1) / (N))
```

真实的 kernel 启动模板，看一个最简单的元素级 kernel——`gelu_forward`（u2-l5 讲过 GELU 的数学）：

[llmc/gelu.cuh:52-55](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/gelu.cuh#L52-L55) —— 标准四步：定 `block_size` → 用 `CEIL_DIV` 算 `grid_size` → 三尖括号启动 → （此处未显式 `cudaGetLastError`，因为元素级 kernel 启动参数极简）。

```c
const int block_size = 512;
assert(N % (block_size * x128::size) == 0);
const int grid_size = CEIL_DIV(N, block_size * x128::size);
gelu_forward_kernel2<<<grid_size, block_size, 0, stream>>>(out, inp);
```

这里 `x128::size` 是「一个 `Packed128` 里装几个 `floatX`」（见 4.1 中 `Packed128`），每个线程一次处理 `x128::size` 个元素，所以分母是 `block_size * x128::size`。`assert(N % (...) == 0)` 保证元素总数能被整除，省去 kernel 内部的越界判断。

需要动态共享内存时，第四个槽位才不写 `0`，例如：

[llmc/layernorm.cuh:500-503](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/layernorm.cuh#L500-L503) —— 先算出 `shared_mem_size` 再填进第三槽位启动 `layernorm_backward_kernel10`。

启动后用 `cudaGetLastError()` 检查的范例：

[llmc/cuda_utils.cuh:202-206](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cuda_utils.cuh#L202-L206) —— `global_sum_deterministic` 启动单 block kernel 后立即检查启动错误。

```c
template<class Float>
void global_sum_deterministic(float* result, const Float* values, int count, cudaStream_t stream) {
    global_sum_single_block_kernel<<<1, 1024, 0, stream>>>(result, values, count);
    cudaCheck(cudaGetLastError());
}
```

#### 4.1.4 代码实践

**实践目标**：亲手把「`CEIL_DIV` + 三尖括号」这套启动模板读懂，并验证 grid 计算的正确性。

**操作步骤**：

1. 打开 [llmc/encoder.cuh:161-164](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/encoder.cuh#L161-L164)，找到 `encoder_forward` 的启动段：

   ```c
   const int block_size = 256;
   const int grid_size = CEIL_DIV(N, (int)(block_size * x128::size));
   encoder_forward_kernel3<<<grid_size, block_size, 0, stream>>>(out, inp, wte, wpe, B, T, C);
   ```

2. 回忆 u2-l1：encoder 的输出是 `(B, T, C)`，所以这里的张量元素数 \(N = B \times T \times C\)。在 BF16 精度下 `floatX = __nv_bfloat16`，`sizeof(int4)=16`，所以 `x128::size = 16/2 = 8`，每个线程处理 8 个元素。

3. 代入 GPT-2 124M 训练的常见取值（例如 `B=4, T=1024, C=768`）手算：
   - \(N = 4 \times 1024 \times 768 = 3{,}145{,}728\)
   - 每个 block 处理 \(256 \times 8 = 2048\) 个元素
   - \(grid\_size = \lceil 3{,}145{,}728 / 2048 \rceil = 1536\) 个 block

**需要观察的现象**：

- grid 大小会随 `B*T*C`（即 batch 和序列长度）线性变化；这是 GPU 自适应并行的关键——同一份 kernel 代码能铺满小模型，也能铺满大模型。
- `block_size` 是人为选定的（这里 256），并不依赖张量大小；它通常选成 warp（32）的整数倍，且不超过 1024。

**预期结果**：你能用 `CEIL_DIV` 在纸上把任意 `(B,T,C)` 下的 `grid_size` 算出来，并与 kernel 内部 `idx = blockIdx.x*blockDim.x + threadIdx.x` 的寻址自洽（每个线程拿到的元素下标互不重叠且覆盖全部 \(N\)）。

**待本地验证**：上述 1536 是基于 `B=4,T=1024,C=768` 的算术结果；若你实际跑的 batch/序列不同，请用你的真实参数重算。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `cudaCheck` 要写成「内联函数 + 宏」两段，而不是直接一个宏 `#define cudaCheck(err) if(err!=cudaSuccess){...}`？

**参考答案**：把 `__FILE__`/`__LINE__` 烘焙进调用点必须靠宏（只有宏能在调用处展开这两个内置符号）；但宏做类型检查和作用域控制很弱，容易出副作用（如 `err` 被求值两次）。折中是「宏只负责传递文件名行号，真正的检查逻辑放在类型安全的内联函数 `cudaCheck_` 里」，兼顾定位精度与类型安全。

**练习 2**：`<<<>>>` 启动后调用 `cudaGetLastError()` 能不能捕获「kernel 内部访问越界」这类运行时错误？为什么？

**参考答案**：不能。`cudaGetLastError()` 在启动后立即调用，只能抓到**启动这一刻**的错误（grid/block 非法、参数无效等）。kernel 在设备上真正执行是异步的，内部的越界、段错误要等后续的同步操作（如 `cudaDeviceSynchronize`、`cudaStreamSynchronize`、或下一次阻塞式拷贝）才会以错误码的形式冒出来。

---

### 4.2 多 stream 调度

#### 4.2.1 概念说明

CUDA 的 stream 是一条「异步命令队列」。同一个 stream 里的操作按提交顺序串行执行；**不同 stream 之间可以并发**。如果所有工作都塞进同一条 stream，GPU 虽然仍在并行执行一个 kernel 内部的成千上万个线程，但「kernel 与 kernel 之间」「kernel 与拷贝/通信之间」就失去了并发机会。

llm.c 的做法分两种规模：

- **单卡训练**：所有 kernel、所有 `cudaMemcpyAsync` 都排在唯一的 `main_stream` 上。注释直言 `// atm everything is on the single main stream`（atm = at the moment）。这把事情简化到极致，正确性最容易推理。
- **多卡训练**：除了 `main_stream`（计算流），额外引入 `nccl_stream`（通信流）。梯度同步的 `ncclAllReduce` 在 `nccl_stream` 上发起，从而让「跨卡通信」有机会与「主计算」重叠或解耦。

为什么要把通信单独放一条流？因为 NCCL 集合通信（all-reduce）本身就是异步的，且通常占满通信带宽、几乎不消耗 SM 算力；把它和计算分开排队，能让 GPU 的「计算单元」和「NVLink/NIC 通信单元」这两个相对独立的硬件资源并行工作，而不是让计算流阻塞着干等通信完成。

#### 4.2.2 核心流程

多卡场景下，一个训练步的尾部（反向传播算完梯度之后）大致是：

```text
main_stream：   ... backward 各层 ... → 梯度就绪 → record(compute_nccl_sync)
                                                    │
nccl_stream：                                       wait(compute_nccl_sync) → ncclAllReduce(梯度) → ...
```

关键机制是 **event（事件）**：

- **`cudaEventRecord(event, streamA)`**：在 streamA 上「打一个点」，表示「当 streamA 执行到这里时，把 event 标记为完成」。
- **`cudaStreamWaitEvent(streamB, event)`**：让 streamB **后续**的操作等待 event 完成，再开始执行。

于是用一条 event 就能建立「streamB 等 streamA」的跨流依赖，而不需要全局同步。在 llm.c 里，`compute_nccl_sync` 这条 event 记录在 `main_stream`（计算流）上，`nccl_stream`（通信流）在发起 all-reduce 前先 `wait` 它，保证「梯度算完了才开始规约」，同时两者仍可按需并发排队。

#### 4.2.3 源码精读

`main_stream` 是主线唯一定义的计算流：

[train_gpt2.cu:79-80](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L79-L80)（仓库根目录文件，非 `llmc/` 下）—— 全局 `deviceProp` 与 `main_stream` 的定义处：

```c
cudaDeviceProp deviceProp; // fills in common_start()
cudaStream_t main_stream;
```

创建与命名在 `common_start` 中，旁边一句注释说明了它的「独占」地位：

[train_gpt2.cu:1182-1184](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1182-L1184) —— 创建 `main_stream` 并用 NVTX 给它打标签（profiler 里能看到「main stream」这个名字）：

```c
// set up the cuda streams. atm everything is on the single main stream
cudaCheck(cudaStreamCreate(&main_stream));
nvtxNameCudaStreamA(main_stream, "main stream");
```

`main_stream` 被几乎每个模型层函数当作最后一个参数透传，例如 [train_gpt2.cu:680](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L680) 的 encoder、`730` 行的 attention、`753` 行的 logits 投影，全都以 `main_stream` 收尾。这就是「atm everything is on the single main stream」的具体含义。

第二条流 `nccl_stream` 只在多卡路径里出现，定义在 ZeRO/多 GPU 头文件中：

[llmc/zero.cuh:75](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/zero.cuh#L75) —— `MultiGpuConfig` 结构体里的成员声明：

```c
cudaStream_t nccl_stream;   // CUDA Stream to perform NCCL operations.
```

[llmc/zero.cuh:449-453](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/zero.cuh#L449-L453) —— 多 GPU 初始化时创建 NCCL 通信子并开辟独立的 `nccl_stream`：

```c
ncclCheck(ncclCommInitRank(&result.nccl_comm, result.num_processes, nccl_id, result.process_rank));
cudaCheck(cudaStreamCreate(&result.nccl_stream));
// ...
nvtxNameCudaStreamA(result.nccl_stream, "nccl stream");
```

两条流的桥接——用 event 让通信流等计算流：

[llmc/zero.cuh:509-529](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/zero.cuh#L509-L529) —— `multi_gpu_async_reduce_gradient`：先在 `compute_stream`（即调用方传入的 `main_stream`）上 `record` 一条 `compute_nccl_sync` event，再让 `nccl_stream` `wait` 它，最后才在 `nccl_stream` 上发起 all-reduce（节选关键行）：

```c
// Block NCCL stream until computations on compute_stream are done, ...
cudaCheck(cudaEventRecord(config->compute_nccl_sync, compute_stream));
cudaCheck(cudaStreamWaitEvent(config->nccl_stream, config->compute_nccl_sync));
// ... ncclAllReduce(... config->nccl_stream) ...
```

所有 all-reduce 都被绑到这条通信流，例如 [llmc/zero.cuh:483](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/zero.cuh#L483) 与 `588` 行的 `ncclAllReduce(..., config->nccl_stream)`。

补充：即便单卡，`cuda_common.h` 里也提供了「在指定 stream 上做异步双缓冲 I/O」的工具 `device_to_file` / `file_to_device`，它们接收一个 stream 参数。在 llm.c 中它们目前都用 `main_stream` 调用（如 [train_gpt2.cu:1233](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1233) 保存 checkpoint），但接口上预留了「换一条 I/O 流做后台搬运」的能力。

[llmc/cuda_common.h:130-141](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cuda_common.h#L130-L141) —— `device_to_file` 用 pinned memory + 两个缓冲区交替「从 GPU 拷贝」与「写盘」，整段跑在传入的 stream 上（节选头部）：

```c
inline void device_to_file(FILE* dest, void* src, size_t num_bytes, size_t buffer_size, cudaStream_t stream) {
    char* buffer_space;
    cudaCheck(cudaMallocHost(&buffer_space, 2*buffer_size));   // 双缓冲 + pinned
    // ... 交替 cudaMemcpyAsync(D2H) 与 fwrite，靠 cudaStreamSynchronize(stream) 对齐 ...
```

#### 4.2.4 代码实践

**实践目标**：解释「为什么 `main_stream` 之外还存在其他 stream」，并用 grep 自行核验结论。

**操作步骤**：

1. 在仓库根目录运行只读搜索（**示例命令**，自行执行）：

   ```bash
   # 看看工程里到底创建了几条 stream
   git grep -n 'cudaStreamCreate'
   # 看看 event 是怎么桥接两条流的
   git grep -n 'compute_nccl_sync'
   ```

2. 阅读 [llmc/zero.cuh:509-529](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/zero.cuh#L509-L529) 的注释与代码。

**需要观察的现象 / 结论**：

- 全仓 `cudaStreamCreate` 只出现在两处：`train_gpt2.cu` 创建 `main_stream`、`zero.cuh` 创建 `nccl_stream`。也就是说，**单卡时只有 `main_stream` 一条流**；`nccl_stream` 是多卡才出现的「第二条流」。
- `main_stream` 之外存在 `nccl_stream` 的根本原因：**把梯度规约（通信）与计算解耦排队**。NCCL 的 all-reduce 走 NVLink/IB 带宽、几乎不占 SM；让它单独排一条流，配合 `compute_nccl_sync` event 表达「算完梯度再规约」这一最小依赖，既保证正确，又把通信尽可能推到与计算并发的位置，避免主计算流被通信阻塞。

**预期结果**：你能用一句话回答——「`nccl_stream` 是为了让跨卡梯度规约与主计算流并发排队，而不是塞在计算流里串行等待；它通过 `compute_nccl_sync` event 与 `main_stream` 建立最小依赖。」

**待本地验证**：单卡 `make train_gpt2cu` 运行时只会用到 `main_stream`；要观察 `nccl_stream`，需用 `scripts/multi_node/` 下的脚本以多进程方式启动（见 u6-l4/u6-l5）。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `ncclAllReduce` 直接放到 `main_stream` 上执行，省掉 `nccl_stream` 和 `compute_nccl_sync` event，结果还正确吗？会有什么代价？

**参考答案**：结果仍然正确——all-reduce 本身仍会等前面反向传播的梯度写完（同一条 stream 天然保序）。代价是失去「计算-通信」的排队弹性：all-reduce 会与同流上的后续 kernel 串行，无法让通信与可能的独立计算并发；而且 NCCL 在独立流上往往能更充分地利用通信硬件。event 机制只是显式表达「只等梯度这一件事」，比全流串行更细粒度。

**练习 2**：`cudaStreamWaitEvent(nccl_stream, event)` 让 `nccl_stream` 等待 `event`。如果 `event` 根本没被任何 `cudaEventRecord` 触发过，会发生什么？

**参考答案**：新创建的 event 默认是「未完成」状态；若从不 record 就 wait，wait 会一直阻塞该流后续操作（实际上 NVIDIA 文档里未初始化 event 的行为不可依赖）。这正是为什么代码总是「先 record 再 wait」成对使用，确保 event 有明确的完成点。

---

### 4.3 cuBLAS handle 管理

#### 4.3.1 概念说明

llm.c 的矩阵乘（GPT-2 里最耗时、最频繁的算子，见 u2-l3、u5-l3）没有手写 GEMM kernel，而是调用 NVIDIA 的 **cuBLASLt** 库（cuBLAS 的「轻量/高级」变体，支持 epilogue 融合、workspace 自适应算法）。使用这类库要遵循「创建一次、长期复用」的模式：

1. 启动时创建一个 **handle**（`cublasLtHandle_t`），分配一块 **workspace**（让库在里面挑更快的算法）。
2. 整个训练过程所有 matmul 调用都共享同一个 handle 和 workspace。
3. 程序退出前销毁 handle、释放 workspace。

反复创建/销毁 handle 会很慢，且每次都要重新做算法启发式搜索；复用单一 handle 是性能与简洁的折中。

cuBLASLt 里有两类「类型」需要区分（这是新手最容易混淆的点）：

- **数据类型（`CUBLAS_LOWP`）**：矩阵元素的存放类型，随精度宏变化——BF16→`CUDA_R_16BF`，FP16→`CUDA_R_16F`，FP32→`CUDA_R_32F`。它和 `floatX` 是同一套精度的「cuBLAS 侧别名」。
- **计算类型（`cublas_compute`）**：内部累加用的类型。llm.c 默认用 `CUBLAS_COMPUTE_32F`（低精度输入、fp32 累加、再截回低精度，保证数值稳定）；在 FP32 模式且 Ampere+ GPU 上，会切到 `CUBLAS_COMPUTE_32F_FAST_TF32` 启用 TF32。

#### 4.3.2 核心流程

cuBLASLt 全局对象的生命周期由主线的 `common_start` / `common_free` 配对管理：

```text
common_start()                         common_free()
  ├─ cublasLtCreate(&cublaslt_handle)    ├─ cudaFree(cublaslt_workspace)
  ├─ cudaMalloc(workspace, 32 MiB)       └─ cublasLtDestroy(cublaslt_handle)
  └─ (FP32 + Ampere+ 时) cublas_compute = ..._FAST_TF32
            │
            ▼
  每次 matmul_forward_cublaslt：直接读全局 cublaslt_handle / cublaslt_workspace / cublas_compute
```

workspace 固定 32 MiB（注释指出只有 Hopper 真正需要 32，其它卡 4 就够，但写死 32 图省事）；TF32 开关由「精度模式 + GPU 主版本号」共同决定，等价于 PyTorch 的 `torch.backends.cuda.matmul.allow_tf32`。

#### 4.3.3 源码精读

所有 cuBLAS 相关全局量集中在 `cublas_common.h`：

[llmc/cublas_common.h:16-22](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cublas_common.h#L16-L22) —— `CUBLAS_LOWP`：cuBLAS 侧的「低精度数据类型」别名，与 `floatX` 同源（节选）：

```c
#if defined(ENABLE_FP32)
#define CUBLAS_LOWP CUDA_R_32F
#elif defined(ENABLE_FP16)
#define CUBLAS_LOWP CUDA_R_16F
#else // default to bfloat16
#define CUBLAS_LOWP CUDA_R_16BF
#endif
```

[llmc/cublas_common.h:27-31](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cublas_common.h#L27-L31) —— workspace 大小、workspace 指针、计算类型、handle 四个全局量：

```c
// Hardcoding workspace to 32MiB but only Hopper needs 32 (for others 4 is OK)
const size_t cublaslt_workspace_size = 32 * 1024 * 1024;
void* cublaslt_workspace = NULL;
cublasComputeType_t cublas_compute = CUBLAS_COMPUTE_32F;
cublasLtHandle_t cublaslt_handle;
```

[llmc/cublas_common.h:37-44](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cublas_common.h#L37-L44) —— `cublasCheck`，与 `cudaCheck` 同构的错误检查（打印状态码 + 文件行 + `exit`）：

```c
void cublasCheck(cublasStatus_t status, const char *file, int line) {
    if (status != CUBLAS_STATUS_SUCCESS) {
        printf("[cuBLAS ERROR]: %d %s %d\n", status, file, line);
        exit(EXIT_FAILURE);
    }
}
#define cublasCheck(status) { cublasCheck((status), __FILE__, __LINE__); }
```

handle / workspace 的实际创建与销毁在主线：

[train_gpt2.cu:1186-1192](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1186-L1192) —— `common_start` 里创建 cuBLASLt、分配 workspace，并按精度+架构决定是否启用 TF32：

```c
// set up cuBLAS and cuBLASLt
cublasCheck(cublasLtCreate(&cublaslt_handle));
cudaCheck(cudaMalloc(&cublaslt_workspace, cublaslt_workspace_size));

// TF32 precision is equivalent to torch.set_float32_matmul_precision('high')
bool enable_tf32 = PRECISION_MODE == PRECISION_FP32 && deviceProp.major >= 8 && override_enable_tf32;
cublas_compute = enable_tf32 ? CUBLAS_COMPUTE_32F_FAST_TF32 : CUBLAS_COMPUTE_32F;
```

[train_gpt2.cu:1199-1206](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1199-L1206) —— `common_free` 里对称地释放，顺序与创建相反（先释 workspace 再销 handle）：

```c
void common_free(GPT2 &model) {
    cudaCheck(cudaStreamDestroy(main_stream));
    cudaCheck(cudaFree(cublaslt_workspace));
    cublasCheck(cublasLtDestroy(cublaslt_handle));
    ...
}
```

这些全局量在每次 matmul 调用时被「隐式共享」——matmul 封装函数直接引用全局的 `cublaslt_handle` 与 `cublaslt_workspace`，而不必把它们层层当参数传。例如 [llmc/matmul.cuh:205-216](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/matmul.cuh#L205-L216) 直接以全局 `cublaslt_handle` 调用 `cublasLtMatmul`（matmul 的 epilogue 融合细节留待 u5-l3 详讲）。

> 补充：`cublas_common.h` 头部注释和 `train_gpt2.cu:43` 的包含说明里都提到 `cublas_handle`（旧版 cuBLAS v2 的 handle），但当前文件实际定义的是 `cublaslt_handle`；旧版 `cublas_handle` 只在 fp32 legacy 版 `train_gpt2_fp32.cu` 相关代码里出现（见 u4-l3）。读者若 grep 到 `cublas_handle` 请注意区分新旧两条线。

#### 4.3.4 代码实践

**实践目标**：理清 cuBLASLt 全局对象「在哪里创建、被谁共享、在哪里销毁」，并核实 TF32 的触发条件。

**操作步骤**：

1. 执行只读搜索（**示例命令**）：

   ```bash
   # 谁在用全局 cublaslt_handle / workspace？
   git grep -n 'cublaslt_handle\|cublaslt_workspace'
   # 看看 TF32 判定依赖了哪些条件
   git grep -n 'FAST_TF32\|override_enable_tf32'
   ```

2. 阅读 [train_gpt2.cu:1186-1192](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1186-L1192)，确认创建顺序。

**需要观察的现象**：

- `cublaslt_handle` 在 `common_start` 创建一次后，被 `matmul.cuh` 里的封装函数反复引用，整个训练复用同一个 handle——这就是「handle 复用」模式。
- TF32 启用是三个条件的与：`PRECISION_MODE==PRECISION_FP32` **且** `deviceProp.major>=8`（Ampere 及以上）**且** `override_enable_tf32` 为真。也就是说 BF16/FP16 模式根本不会触发 TF32（TF32 只对 fp32 输入有意义）。

**预期结果**：你能画出「`common_start` 建表 → 各层 matmul 读全局 handle → `common_free` 销毁」的生命周期图，并说清 TF32 只在 fp32+Ampere+ 才可能开启。

**待本地验证**：你的卡是否 `major>=8`，用 `nvidia-smi --query-gpu=compute_cap` 或在程序输出的 `Device N: <name>` 里确认。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `cublaslt_workspace` 要「一次分配 32 MiB、全局共享」，而不是每次 matmul 调用前临时 `cudaMalloc`？

**参考答案**：两方面的收益。其一，`cudaMalloc` 是较重的同步操作，训练每步要调几十次 matmul，临时分配会拖慢训练；其二，cuBLASLt 会根据可用 workspace 大小选择更优算法（workspace 越大，可选算法越多），固定的全局 workspace 让库能稳定挑到快算法，并避免碎片化。

**练习 2**：BF16 训练时，`CUBLAS_LOWP` 是什么、`cublas_compute` 默认又是什么？为什么这样组合？

**参考答案**：BF16 模式下 `CUBLAS_LOWP = CUDA_R_16BF`（矩阵以 bf16 存取），而 `cublas_compute` 默认仍是 `CUBLAS_COMPUTE_32F`（内部用 fp32 累加）。这样组合兼顾「省显存/带宽（bf16 存储）」与「数值稳定（fp32 累加）」，是混合精度训练的典型做法。注意 BF16 模式下 TF32 分支不会被触发，因为 `PRECISION_MODE != PRECISION_FP32`。

---

## 5. 综合实践

把本讲三个模块串起来，做一个「**追踪一个 cuBLASLt matmul 调用全程所依赖的工具层**」的源码阅读任务：

1. **起点——模型层调用**：打开 [train_gpt2.cu:720](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L720)，这是 `gpt2_forward` 里第一处 matmul（QKV 投影），形如 `matmul_forward_cublaslt(l_qkvr, l_ln1, l_qkvw, l_qkvb, B, T, C, 3*C, main_stream)`。
2. **进工具层——handle/workspace**：跳到 [llmc/matmul.cuh:205-216](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/matmul.cuh#L205-L216)，确认它用的是 `cublas_common.h` 里的全局 `cublaslt_handle`/`cublaslt_workspace`/`cublas_compute`，并用 `cublasCheck` 包裹（→ 模块 4.3）。
3. **回溯生命周期**：这些全局量在 [train_gpt2.cu:1186-1192](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1186-L1192) 创建、在 `1199-1206` 销毁（→ 模块 4.3）。
4. **stream 归属**：这次 matmul 跑在 `main_stream` 上（调用实参就是 `main_stream`），而 `main_stream` 在 [train_gpt2.cu:1182-1184](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1182-L1184) 创建、被所有层共享（→ 模块 4.2）。
5. **（可选）对比手写 kernel**：再看一个非 cuBLAS 的元素级 kernel 启动，如 [llmc/gelu.cuh:52-55](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/gelu.cuh#L52-L55)，确认它用 `CEIL_DIV` 算 grid、用三尖括号启动、同样以 `stream` 收尾（→ 模块 4.1）。

**最终产出**：画一张时序图，标出 `common_start`（建 handle/stream/workspace）→ 训练步内多次复用（matmul 吃 handle、kernel 吃 `main_stream`）→ `common_free`（销毁）的全过程，并在图上标注每一步用到了本讲的哪个工具。这张图能帮你建立「工具层如何支撑整个 CUDA 主线」的整体认知，是进入 u5-l3（matmul/cuBLASLt 详解）和 u6（混合精度、多卡）前的关键脚手架。

## 6. 本讲小结

- llm.c 用「**内联函数 + 带 `__FILE__`/`__LINE__` 的包装宏**」实现 `cudaCheck`/`cudaFreeCheck`/`cublasCheck`，出错即打印定位信息并 `exit`，绝不带病运行。
- 工程里**没有**自定义的 kernel 启动包装宏；启动靠原生 `<<<grid, block, shared_mem, stream>>>` 三尖括号 + `CEIL_DIV(M,N)` 便利宏算 grid，必要时启动后调 `cudaGetLastError()` 抓启动期错误。
- 单卡训练只有一条 `main_stream`，所有 kernel 与异步拷贝都排在它上面；**多卡**才出现第二条流 `nccl_stream`，专门跑 NCCL all-reduce，靠 `compute_nccl_sync` event 与主计算流建立「算完梯度再规约」的最小依赖，争取计算-通信并发。
- cuBLASLt 遵循「创建一次、全局复用」：`cublaslt_handle` 与 32 MiB `cublaslt_workspace` 在 `common_start` 创建、`common_free` 销毁，所有 matmul 共享；`CUBLAS_LOWP`（数据类型）随精度宏变、`cublas_compute`（计算类型）默认 fp32 累加，且仅在 fp32+Ampere+ 时切到 TF32。
- 工具层（`cuda_common.h` / `cuda_utils.cuh` / `cublas_common.h`）是各模型层 `.cuh` 共享的「螺丝刀」，理解它能让后续读 matmul、attention、global_norm 等层时不再被基础设施细节绊住。

## 7. 下一步学习建议

- **u5-l3（MatMul：cuBLASLt 的调用与封装）**：本讲只讲了 handle/workspace 的「管理」，下一讲正面拆解 `matmul_forward_cublaslt` 如何设置 transpose、bias/gelu epilogue 融合、TF32 与混合精度，是本讲 4.3 的自然延伸。
- **u5-l4（各层 CUDA kernel）**：会用到这里介绍的 `warpReduceSum`/`blockReduce`（`cuda_utils.cuh`）和 `Packed128`/`load128`/`store128cs` 宽存取工具，建议读完本讲后带着「这些 device 工具具体怎么被 kernel 用」的视角去读。
- **u6-l1（混合精度与 master weights）**：本讲埋了 `CUBLAS_LOWP`、`cublas_compute`、TF32 等精度相关的伏笔，混合精度那一讲会把 `floatX` 与 cuBLAS 数据类型的对应关系讲透。
- **u6-l4（多 GPU：ZeRO 与 NCCL）**：想深入理解 `nccl_stream` 和 `compute_nccl_sync` event 的全部用法，直接读 `llmc/zero.cuh` 配合那一讲。

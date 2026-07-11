# MatMul：cuBLASLt 的调用与封装

## 1. 本讲目标

在前面的学习中（u2-l3、u4-l3），我们已经见过 matmul 的两种形态：CPU 参考实现里的「朴素三重循环」和「cache blocking 分块版」，以及 GPU fp32 legacy 版里手写的 CUDA kernel。这些手写实现虽然清楚，但远不是 GPU 上最快的矩阵乘（GEMM）做法——真正的性能来自厂商库的高度调优 kernel。

本讲我们进入 CUDA 主线 `train_gpt2.cu` 里 matmul 的「最终形态」：用 **cuBLASLt** 替代手写 GEMM。学完后你应该能够：

1. 读懂 `matmul_cublaslt` 这个对 `cublasLtMatmul` 的封装，理解它的参数约定（谁转置、`m/n/k` 各是什么、`alpha/beta` 怎么用）。
2. 理解 cuBLASLt 的 **epilogue（尾声）** 机制：如何把 bias 加法和 GELU 激活「融合」进同一次 GEMM 调用，省掉一次显存往返。
3. 看懂反向 `matmul_backward` 如何复用同一个封装求出三路梯度（`dinp`、`dweight`、`dbias`），以及为什么 dbias 要单独用一个归约 kernel。
4. 理解 **TF32** 在 matmul 中的意义、它何时开启、与 BF16/FP16 精度的关系。
5. 能在 `gpt2_forward` 里数出所有 `matmul_forward_cublaslt` 调用点，并指出它们对应模型的哪些线性层。

## 2. 前置知识

读本讲前，你最好已经具备以下认知（来自前置讲义）：

- **matmul 的数学形状**（u2-l3）：一个线性层是 `out = inp @ weight^T + bias`，其中 `inp` 形状 `(B*T, C)`、`weight` 形状 `(OC, C)`、`out` 形状 `(B*T, OC)`，`OC` 是输出通道数；反向要分别对 `inp`、`weight`、`bias` 求三路梯度，且权重梯度用 `+=` 累加。
- **GELU 的前向反向**（u2-l5）：GELU 是 MLP 升维后的非线性；反向需要用到前向的输入（即「pre-gelu」张量）。
- **CUDA 主线骨架与精度宏**（u5-l1）：`floatX` 是由 Makefile 的 `PRECISION` 决定的编译期类型别名（默认 `__nv_bfloat16`）；`ParameterTensors`/`ActivationTensors` 用「一次 `cudaMalloc` + 指针排布」管理。
- **CUDA 工具层**（u5-l2）：`cublasCheck` 错误检查宏、`cublaslt_handle` 句柄与 32 MiB `cublaslt_workspace` 在 `common_start` 创建一次、全局复用。

本讲还会用到两个**关键术语**，先建立直觉：

- **GEMM**（General Matrix Multiply，通用矩阵乘）：即 `C = α·op(A)·op(B) + β·C`，`op` 可以是转置或不转置。cuBLAS / cuBLASLt 都是算这个。
- **cuBLASLt**：cuBLAS 的「轻量/可调度」版本（Lt = Lightweight），相比老 `cublasGemm*` 接口，它最大的特点是支持 **epilogue 融合**——在一次 GEMM 之后顺手做 bias、GELU、ReLU 等后处理，不必再写回显存再读出来。

> 列主序小提示：cuBLAS 系列是 **列主序（column-major / Fortran 风格）** 库，而 C/CUDA 里我们存的是**行主序（row-major）**。一个巧妙的恒等式是：「行主序的 `(R, K)` 矩阵」与「列主序的 `(K, R)` 矩阵」在内存里逐字节相同。本讲封装里大量出现的 `transA=true`，正是用这个恒等式把行主序的权重喂给列主序的 cuBLAS。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [llmc/matmul.cuh](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/matmul.cuh) | 本讲主角：`matmul_cublaslt` 封装、`matmul_forward_cublaslt` 前向、`matmul_backward` 反向，以及反向求 dbias 的自定义归约 kernel。 |
| [llmc/cublas_common.h](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cublas_common.h) | cuBLAS 精度宏 `CUBLAS_LOWP`、全局 `cublas_compute` / `cublaslt_handle` / workspace、`cublasCheck` 宏。 |
| [llmc/cuda_common.h](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cuda_common.h) | `floatX` 类型别名定义、`CEIL_DIV`、错误检查宏。 |
| [llmc/gelu.cuh](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/gelu.cuh) | 当 gelu 未融合进 matmul 时的回退实现 `gelu_forward` / `gelu_backward_inplace`。 |
| [train_gpt2.cu](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu) | 调用方：`gpt2_forward` 里的各线性层、`gpt2_backward`、`common_start` 里的 TF32 与句柄初始化。 |

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：先看封装本体 `matmul_cublaslt`，再看它最值得讲的 epilogue 融合能力，然后看反向如何复用同一封装，最后收束于 TF32 与精度。

### 4.1 cuBLASLt GEMM 封装：matmul_cublaslt

#### 4.1.1 概念说明

`matmul_cublaslt` 是 llm.c 对 `cublasLtMatmul` 的一层薄封装，目标是「一个函数支持 llm.c 里所有需要的矩阵乘场景」。它的数学语义就是标准 GEMM，外加可选的 bias / gelu 尾声：

\[
D = \alpha \cdot \mathrm{op}(A)\,\mathrm{op}(B) + \beta \cdot C \quad (+ \text{bias})(\to \text{gelu})
\]

其中 \(\mathrm{op}\) 表示转置或不转置。封装把「算什么形状、要不要转置、要不要累加、要不要融合 gelu」都参数化了，于是前向、反向、attention 的 batched matmul 都能复用同一个函数。

理解这个封装的关键是它的参数约定。先看签名（[llmc/matmul.cuh:109-112](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/matmul.cuh#L109-L112)）：

```c
void matmul_cublaslt(floatX* d, const floatX* a, const floatX* b, const floatX* bias,
                     int m, int n, int k, cudaStream_t stream=0, bool transA=true, bool transB=false,
                     int batch_count=0, size_t strideA=0, size_t strideB=0, size_t strideOut=0,
                     bool accumulate=false, floatX* pre_gelu=NULL, bool backward=false)
```

约定如下（结合 cuBLAS 列主序约定）：

- `d` 是输出，逻辑形状在列主序下是 `m × n`。
- `a`、`b` 是两个输入矩阵，经 `transA`/`transB` 转置后参与相乘，`op(A)` 为 `m × k`、`op(B)` 为 `k × n`。
- `m, n, k` 即 GEMM 的三个维度；内层求和沿 `k`。
- `accumulate`（对应 `beta`）：`false` 时 `beta=0`（**覆盖写**），`true` 时 `beta=1`（**累加** `D += ...`）。
- `pre_gelu` 非空时启用 GELU 融合；`bias` 非空时启用 bias 融合。
- `batch_count` 非零时做 **strided batched GEMM**（给非 flash-attention 的批量矩阵乘用）。

#### 4.1.2 核心流程

`matmul_cublaslt` 的执行流程可以拆成「准备描述符 → 设布局 → 设尾声 → 选算法 → 执行 → 清理」六步：

```text
1. 对齐检查：a/b/d/bias 必须 16 字节对齐，否则 exit
2. 创建 operationDesc：计算类型用 cublas_compute，scale 类型固定 FP32
3. 设 TRANSA / TRANSB（默认 transA=true, transB=false）
4. 创建 A/B/C/D 四个矩阵布局（列主序，含 leading dimension）
   - 若 batch_count != 0：给四个布局都附加 batch_count 与 stride
5. 设 epilogue（DEFAULT / BIAS / GELU_AUX / BGRADB / DGELU …）并挂上 bias、pre_gelu 指针
6. 用 heuristic 在 32 MiB workspace 约束下挑一个算法；alpha=1, beta=accumulate?1:0
7. 调 cublasLtMatmul 执行
8. 销毁所有描述符/布局/偏好
```

注意第 2 步里的两个「类型」要区分清楚（这是上一讲 u5-l2 已埋下的、本讲正式用上的伏笔）：

- **数据类型 `CUBLAS_LOWP`**：矩阵元素本身是什么精度，随 `PRECISION` 切换为 `CUDA_R_32F` / `CUDA_R_16F` / `CUDA_R_16BF`（[llmc/cublas_common.h:16-22](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cublas_common.h#L16-L22)）。
- **计算类型 `cublas_compute`**：乘加累加用什么精度，默认 `CUBLAS_COMPUTE_32F`，FP32 + Ampere+ 时可切到 `CUBLAS_COMPUTE_32F_FAST_TF32`（见 4.4 节）。在 [llmc/matmul.cuh:126](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/matmul.cuh#L126) 创建描述符时同时指定二者。

#### 4.1.3 源码精读

**对齐检查**——cuBLASLt 对未对齐指针虽能跑但性能差，这里直接强制要求 16 字节对齐（[llmc/matmul.cuh:119-122](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/matmul.cuh#L119-L122)）：

```c
if(((uintptr_t)a % 16) != 0 || ((uintptr_t)b % 16) != 0 || ((uintptr_t)d % 16) != 0 || ((uintptr_t)bias % 16) != 0) {
    printf("All cuBLASLt pointers must be aligned!\n");
    exit(EXIT_FAILURE);
}
```

**转置与布局**——这是封装里最绕、也最值得理解的一段。`transA` 默认为 `true`，源于「行主序权重 → 列主序 cuBLAS」的恒等式（[llmc/matmul.cuh:134-154](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/matmul.cuh#L134-L154)）：

```c
cublasCheck(cublasLtMatmulDescSetAttribute(operationDesc, CUBLASLT_MATMUL_DESC_TRANSA,
               (transA) ? &opTranspose : &opNoTranspose, sizeof(opTranspose)));
// ...
if (transA) {
    cublasCheck(cublasLtMatrixLayoutCreate(&ALayout, CUBLAS_LOWP, k, m, k)); // rows=k, cols=m, ld=k
} else {
    cublasCheck(cublasLtMatrixLayoutCreate(&ALayout, CUBLAS_LOWP, m, k, m));
}
```

以前向 `matmul_forward_cublaslt` 的调用 `matmul_cublaslt(out, weight, inp, bias, OC, B*T, C, stream, true, false, ...)` 为例：`a=weight, b=inp, m=OC, n=B*T, k=C, transA=true`。权重在 llm.c 里是行主序 `(OC, C)`，它在内存里逐字节等于列主序 `(C, OC)`，于是把 `ALayout` 声明成 `rows=k=C, cols=m=OC`（即列主序 `C×OC`）正好对上存储，再用 `TRANSA=OP_T` 让 cuBLAS 把它当成 `OC×C` 来乘。最终 `D` 的列主序 `OC×B*T` 又逐字节等于行主序 `(B*T, OC)` 的 `out`。一句话：**`transA=true` 是把行主序权重喂给列主序 cuBLAS 的标准技巧**，计算结果等价于 CPU 版的 `out = inp @ weight^T + bias`。

**算法选择与执行**——cuBLASLt 不让你手写循环顺序，而是用一个「启发式（heuristic）」在给定形状、精度、workspace 上限下自动挑一个最优算法（[llmc/matmul.cuh:204-218](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/matmul.cuh#L204-L218)）：

```c
cublasLtMatmulAlgoGetHeuristic(cublaslt_handle, operationDesc, ALayout, BLayout,
                               CLayout, DLayout, preference, 1, &heuristic, &returnedResults);
if (returnedResults == 0) { printf("No cuBLASLt algorithm: ...\n"); exit(EXIT_FAILURE); }

const float alpha = 1.0f, beta = accumulate ? 1.0f : 0.0f;
cublasCheck(cublasLtMatmul(cublaslt_handle, operationDesc,
               &alpha, a, ALayout, b, BLayout, &beta, d, CLayout, d, DLayout,
               &heuristic.algo, cublaslt_workspace, cublaslt_workspace_size, stream));
```

两个要点：其一，`beta` 编码了覆盖还是累加——这正是「激活梯度用 `=`、参数梯度用 `+=`」约定在 GEMM 层面的落点（与 u4-l3 里 cuBLAS 老接口的 `beta` 语义一致）。其二，`cublaslt_workspace` 是那块全局 32 MiB scratch（[llmc/cublas_common.h:28](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cublas_common.h#L28)），算法可以拿它做分块暂存；注释提到「只有 Hopper 需要 32 MiB，其它 4 MiB 就够」。

#### 4.1.4 代码实践

**实践目标**：验证封装的维度约定，把「调用参数」翻译回「熟悉的数学」。

**操作步骤**：

1. 打开 [llmc/matmul.cuh:231-242](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/matmul.cuh#L231-L242) 的 `matmul_forward_cublaslt`，它把前向参数按「历史顺序」转给 `matmul_cublaslt`。
2. 取 GPT-2 124M 的 fc 层（升维）：`C=768`、`OC=4*C=3072`、`B*T` 设为某批大小（如 `B=4, T=1024` → `B*T=4096`）。
3. 写出对应的 `matmul_cublaslt` 调用：`m=OC=3072, n=B*T=4096, k=C=768, transA=true, transB=false`。
4. 在纸上画出 `op(A)=weight^T` 形状 `m×k = 3072×768`、`op(B)=inp` 形状 `k×n = 768×4096`、输出 `D` 形状 `m×n = 3072×4096`（列主序），确认内层维度 `k=768` 能对上。

**需要观察的现象**：`op(A)·op(B)` 的内层维度 `k` 必须等于 `C`；输出在列主序下是 `OC×B*T`，转回行主序正好是 `(B*T, OC)` 的 `out`。

**预期结果**：你能在不改数学的前提下，把任意一个 llm.c 线性层的 `(B,T,C,OC)` 翻译成 `(m,n,k,transA,transB)`；本实践无需运行，属于源码阅读型实践（待本地验证：若你想跑，可参考 dev/cuda 的 benchmark 写法）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `matmul_cublaslt` 默认 `transA=true` 而不是 `transA=false`？

**参考答案**：因为 llm.c 的权重矩阵是行主序 `(OC, C)` 存储的，而 cuBLAS 是列主序。行主序 `(OC, C)` 与列主序 `(C, OC)` 内存逐字节相同，把 `ALayout` 声明成列主序 `C×OC` 再用 `OP_T` 转置回 `OC×C` 参与相乘，正好等价于数学上的 `weight^T`，无需任何数据搬运。

**练习 2**：封装里 `accumulate` 参数最终影响的是 `alpha` 还是 `beta`？为什么反向求 `dweight` 时要传 `accumulate=true`？

**参考答案**：影响 `beta`（`accumulate ? 1.0f : 0.0f`）。反向求 `dweight` 用 `+=` 累加（因为多个 `B*T` 位置对同一权重元素贡献梯度，且权重梯度可能被多次累加），所以需要 `beta=1` 让 GEMM 执行 `D += ...`，这正是 u2-l3 反向里「参数梯度沿 (b,t) 求和、用 `+=`」约定在 GEMM 层的实现。

---

### 4.2 bias / gelu 的 epilogue 融合

#### 4.2.1 概念说明

**epilogue（尾声）** 是 cuBLASLt 区别于老 cuBLAS 的杀手锏：一次 GEMM 算完后，硬件在把结果写回显存之前，可以「顺手」对每个输出元素再做一点后处理——加 bias、套个激活函数（GELU/ReLU）、或二者兼有。好处是**省一次显存写+读**：不融合时，GEMM 要把 `(B*T, OC)` 的中间结果写回显存，再启动一个新 kernel 把它读出来加 bias / 算 GELU；融合后这些都在寄存器/片上完成，只写回最终结果。

llm.c 里用到的 epilogue 主要有：

| epilogue 常量 | 含义 | 用在哪 |
| --- | --- | --- |
| `CUBLASLT_EPILOGUE_DEFAULT` | 纯 GEMM，无尾声 | 大多数前向/反向 |
| `CUBLASLT_EPILOGUE_BIAS` | 加 bias 向量 | 带 bias 的前向 |
| `CUBLASLT_EPILOGUE_GELU_AUX` | 套 GELU，并把 **pre-gelu**（GELU 的输入）存到辅助指针 | fc 层前向（融合 GELU 时） |
| `CUBLASLT_EPILOGUE_GELU_AUX_BIAS` | 加 bias + 套 GELU + 存 pre-gelu | fc 层前向（同时有 bias 与 GELU） |
| `CUBLASLT_EPILOGUE_BGRADB` | 反向求 bias 梯度 | 反向（注：llm.c 实际改用了自定义 kernel，见 4.3） |
| `CUBLASLT_EPILOGUE_DGELU` | 反向穿过 GELU | 反向 dinp（融合 GELU 反向时） |

注意 `GELU_AUX` 里的 **AUX**：它不仅算 GELU，还把 GELU 的**输入**（pre-gelu）写到一个辅助缓冲。这一点很关键，因为 GELU 反向需要用到前向输入（见 u2-l5），所以前向必须把它存下来留给反向——融合非但没有丢信息，反而顺手把反向要用的东西也存好了。

#### 4.2.2 核心流程

封装根据「有没有 bias」「有没有 pre_gelu」「是不是反向」三个布尔，选出一个 epilogue（[llmc/matmul.cuh:174-198](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/matmul.cuh#L174-L198)）：

```text
if has_gelu:
    设 EPILOGUE_AUX_LD 与 AUX_POINTER = pre_gelu   # 告诉 cuBLAS 把 pre-gelu 存哪
    if backward: epilogue = DGELU          (且断言此时不应有 bias)
    else:        epilogue = has_bias ? GELU_AUX_BIAS : GELU_AUX
elif has_bias:
    epilogue = backward ? BGRADB : BIAS
else:
    epilogue = DEFAULT
设 EPILOGUE = epilogue
if has_bias: 设 BIAS_POINTER = bias
```

#### 4.2.3 源码精读

前向封装 `matmul_forward_cublaslt` 决定「融合还是不融合 GELU」（[llmc/matmul.cuh:231-242](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/matmul.cuh#L231-L242)）：

```c
void matmul_forward_cublaslt(floatX* out, floatX* inp, floatX* weight, floatX* bias,
                     int B, int T, int C, int OC, cudaStream_t stream,
                     floatX* pre_gelu=NULL, int gelu_fusion=1) {
    // By default only fuse GELU for H100+ as cuBLAS seems to be inefficient for fused GELU on Ada/Ampere (?)
    if (gelu_fusion < 1 && pre_gelu) {
        matmul_cublaslt(pre_gelu, weight, inp, bias, OC, B*T, C, stream, true, false, 0,0,0,0, false, NULL, false);
        gelu_forward(out, pre_gelu, B*T*OC, stream);
    } else {
        matmul_cublaslt(out, weight, inp, bias, OC, B*T, C, stream, true, false, 0,0,0,0, false, pre_gelu, false);
    }
}
```

两种路径对比：

- **不融合**（`gelu_fusion < 1`）：先 GEMM 把 `weight^T @ inp + bias` 写进 `pre_gelu`，再单独启动 `gelu_forward` kernel 读 `pre_gelu` 写 `out`。两次显存往返。
- **融合**（默认分支）：一次 GEMM 直接写 `out`，epilogue 在片上完成 GELU，并把 pre-gelu 存到辅助指针。一次显存往返。

注释点出一个工程现实：cuBLAS 的融合 GELU 在 Ada/Ampere（非 Hopper）上可能反而更慢，所以默认是否融合要由调用方按 GPU 代际决定——这就是 `train_gpt2.cu` 里 `model->gelu_fusion` 字段（[train_gpt2.cu:317](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L317)）的由来，它的取值 `0=none, 1=forward, 2=forward+backward`，默认值是 `0`（[train_gpt2.cu:1516](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1516)），可通过命令行 `-ge` 改（[train_gpt2.cu:1482](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1482)）。

epilogue 的实际设置代码（[llmc/matmul.cuh:176-191](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/matmul.cuh#L176-L191)）：

```c
if (has_gelu) {
    int64_t gelu_ld = m;
    cublasCheck(cublasLtMatmulDescSetAttribute(operationDesc, CUBLASLT_MATMUL_DESC_EPILOGUE_AUX_LD, &gelu_ld, sizeof(gelu_ld)));
    cublasCheck(cublasLtMatmulDescSetAttribute(operationDesc, CUBLASLT_MATMUL_DESC_EPILOGUE_AUX_POINTER, &pre_gelu, sizeof(pre_gelu)));
    if (backward) {
        assert(!has_bias);
        epilogue = CUBLASLT_EPILOGUE_DGELU;
    } else {
        epilogue = has_bias ? CUBLASLT_EPILOGUE_GELU_AUX_BIAS : CUBLASLT_EPILOGUE_GELU_AUX;
    }
} else if(has_bias){
    epilogue = backward ? CUBLASLT_EPILOGUE_BGRADB : CUBLASLT_EPILOGUE_BIAS;
} else {
    epilogue = CUBLASLT_EPILOGUE_DEFAULT;
}
```

> 一个细节：fc 层是「带 bias + 带 GELU」的，所以前向走 `GELU_AUX_BIAS`；而最终的 logits 投影（output 层）传 `bias=NULL`（权重绑定，见 u2-l7），于是既无 bias 也无 gelu，走 `DEFAULT`——可在 [train_gpt2.cu:753](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L753) 验证。

#### 4.2.4 代码实践

**实践目标**：搞清 GPT-2 哪些线性层启用了 GELU 融合、哪些只有 bias、哪些两者皆无。

**操作步骤**：

1. 打开 `gpt2_forward` 的循环体 [train_gpt2.cu:720-753](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L720-L753)。
2. 对每个 `matmul_forward_cublaslt` 调用，记录它传的 `bias`（第 4 参数）和 `pre_gelu`（倒数第 2 参数）是否为 `NULL`。
3. 列一张表：层名 | bias | pre_gelu | 对应 epilogue。

**需要观察的现象**：只有 fc 升维层（`l_fch_gelu = ... l_fcw, l_fcb ..., l_fch, model->gelu_fusion`，[train_gpt2.cu:735](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L735)）同时带 bias 且传了 `pre_gelu`；output 层 bias 为 `NULL`（[train_gpt2.cu:753](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L753)）。

**预期结果**：你会得到一张形如「qkv: bias+无gelu → BIAS；attproj: bias+无gelu → BIAS；fc: bias+gelu → GELU_AUX_BIAS；fcproj: bias+无gelu → BIAS；output: 无bias+无gelu → DEFAULT」的表。本实践为源码阅读型，无需运行（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `GELU_AUX` 要把 pre-gelu 额外存一份到辅助指针，而不是只存 GELU 的输出？

**参考答案**：因为 GELU 的反向 \(\mathrm{d}x = \mathrm{GELU}'(x)\cdot \mathrm{d}y\) 依赖前向输入 \(x\)（即 pre-gelu），而不依赖前向输出 \(\mathrm{GELU}(x)\)。融合 GELU 时 cuBLAS 内部已经算了输出并写回，但反向需要的输入若不单独存下来就丢了，所以 epilogue 用 AUX 指针把 pre-gelu 一并存下，供反向复用。

**练习 2**：注释说「默认只为 H100+ 融合 GELU」，但代码默认 `gelu_fusion=0`。这两者矛盾吗？

**参考答案**：不矛盾。注释描述的是「**若**要融合，只在 H100+ 才划算」的设计意图；而 `gelu_fusion=0`（[train_gpt2.cu:1516](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1516)）是当前更保守的默认——连 H100 上也默认关闭，把融合作为可选优化留给用户用 `-ge` 开启。这反映了「融合收益随硬件/驱动版本波动，保守默认更安全」的工程权衡。

---

### 4.3 反向 matmul_backward：三路梯度与一个自定义归约

#### 4.3.1 概念说明

前向 `out = inp @ weight^T + bias` 的反向要分别求 `dinp`、`dweight`、`dbias` 三路梯度（见 u2-l3）。在 cuBLASLt 主线里，`matmul_backward` 把这三路都用 GEMM 表达：

\[
\begin{aligned}
\mathrm{dinp} &= \mathrm{dout} \cdot \mathrm{weight}            &\quad(\text{沿 } OC \text{ 求和})\\
\mathrm{dweight} &= \mathrm{dout}^\top \cdot \mathrm{inp}     &\quad(\text{沿 } B*T \text{ 求和})\\
\mathrm{dbias} &= \sum_{b,t} \mathrm{dout}[b,t,:]             &\quad(\text{沿 } B*T \text{ 归约})
\end{aligned}
\]

前两路天然是 GEMM（一个矩阵乘），可以复用 `matmul_cublaslt`。第三路 `dbias` 是「沿 batch/seq 维归约成一个向量」，**不是** GEMM——于是 llm.c 没有用 cuBLASLt 的 `BGRADB` 尾声去算它，而是写了一个专门的高效归约 kernel `matmul_backward_bias_kernel9`。

这里有一个贯穿全讲的「`=` vs `+=`」约定，在反向三路里表现得最清楚：

- `dinp`：每个位置独立计算，用 `beta=0` **覆盖写**。
- `dweight`：多个 `B*T` 位置累加到同一权重，用 `beta=1`（`accumulate=true`）**累加**。
- `dbias`：也是累加，但因为是归约，由 kernel 内部用 `+=` 完成，且依赖每步开头的梯度清零。

#### 4.3.2 核心流程

`matmul_backward` 的流程（[llmc/matmul.cuh:244-290](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/matmul.cuh#L244-L290)）：

```text
1. 若 dbias != NULL：
   - 启动 matmul_backward_bias_kernel9 对 (B*T) 维归约 dout，得到 dbias（必要时经中间 buffer 再归约）
   - 把 dbias 置 NULL，防止后面又融合一遍
2. dinp = matmul_cublaslt(dinp, weight, dout, NULL, C, B*T, OC, ..., accumulate=false, pre_gelu?, backward=true)
   - 用 beta=0 覆盖写
   - 若 gelu_fusion >= 2 且有 pre_gelu：用 DGELU 尾声一并穿过 GELU 反向
3. 若 gelu 未融合（gelu_fusion < 2 且有 pre_gelu）：额外调 gelu_backward_inplace
4. dweight = matmul_cublaslt(dweight, inp, dout, NULL, C, OC, B*T, ..., transB=true, accumulate=true, backward=true)
   - 用 beta=1 累加
```

#### 4.3.3 源码精读

**dbias 的自定义归约**——为什么不用 cuBLASLt 的 `BGRADB` 尾声？因为 dbias 是「把 `(B*T, OC)` 的 dout 沿第 0 维加成 `(OC,)`」，这是一个**纯归约**，用手写的 warp shuffle 归约（`__shfl_down_sync`）可以非常高效，且能根据 OC 大小决定要不要走中间 buffer（[llmc/matmul.cuh:252-276](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/matmul.cuh#L252-L276)）：

```c
if (dbias != NULL) {
    const int block_size = deviceProp.maxThreadsPerMultiProcessor == 1536 ? 768 : 1024;
    dim3 block_dim = {4, 8, (unsigned)block_size/WARP_SIZE};
    const int OC_per_warp = block_dim.y * x128::size; // 64 at BF16
    const int grid_size_x = CEIL_DIV(OC, OC_per_warp);
    const int grid_size_y = max(1, deviceProp.maxThreadsPerMultiProcessor * deviceProp.multiProcessorCount / (block_size * grid_size_x));
    if(grid_size_y == 1) {
        matmul_backward_bias_kernel9<<<dim3(grid_size_x, grid_size_y), block_dim, 0, stream>>>(dbias, dout, B, T, OC, False);
    } else {
        // OC 太少 → grid_size_y>1 → 各 block 写临时 buffer，再用 reduce_add_sum_kernel 汇总
        matmul_backward_bias_kernel9<<<...>>>(dbias_buffer, dout, B, T, OC, True);
        reduce_add_sum_kernel<<<CEIL_DIV(OC, 256 * f128::size), 256, 0, stream>>>(dbias, dbias_buffer, OC, grid_size_y);
    }
    dbias = NULL; // 防止下面又融合算一遍
}
```

设计很巧妙：当 OC 足够大（`grid_size_y==1`）时，一个 block 就能把某个 OC 列的全部 `B*T` 归约完，直接 `+=` 写进 `dbias`；当 OC 太小、单 block 装不下整张卡的并行度时（`grid_size_y>1`），多个 block 各自把部分和写进 `dbias_buffer`，再用第二个 kernel `reduce_add_sum_kernel` 汇总。注意单 block 路径里 kernel 内部用的是 `+=`（[llmc/matmul.cuh:74-75](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/matmul.cuh#L74-L75)），所以仍依赖每步开头的梯度清零。

**dinp（覆盖写）与可选 GELU 反向融合**（[llmc/matmul.cuh:279-285](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/matmul.cuh#L279-L285)）：

```c
// backward to input, uses = in the backward pass (set the gradient)
matmul_cublaslt(dinp, weight, dout, NULL, C, B*T, OC, stream, false, false, 0,0,0,0, false,
                gelu_fusion >= 2 ? pre_gelu : NULL, true);

// backward GELU (if it wasn't fused into the matmul above)
if (gelu_fusion < 2 && pre_gelu) {
    gelu_backward_inplace(dinp, pre_gelu, B*T*C, stream);
}
```

注意这里 `m=C, n=B*T, k=OC, transA=false`（因为这次 `a=weight` 要当 `C×OC` 直接用、不转置，算 `dinp = dout @ weight`）；`backward=true` 配合 `pre_gelu` 时走 `DGELU` 尾声，一步把「穿 GELU」也算进去。否则退回单独的 `gelu_backward_inplace`（[llmc/gelu.cuh:59-66](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/gelu.cuh#L59-L66)）。

**dweight（累加）**（[llmc/matmul.cuh:287-289](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/matmul.cuh#L287-L289)）：

```c
// backward to weight, uses += in the backward pass (accumulate the gradient) by setting alpha=one
matmul_cublaslt(dweight, inp, dout, NULL /*dbias*/, C, OC, B*T, stream, false, true, 0,0,0,0,
                true /* accumulate */, NULL, true);
```

这里 `m=C, n=OC, k=B*T, transB=true`，`accumulate=true` 即 `beta=1`，于是 cuBLASLt 执行 `dweight += inp^T @ dout`，实现沿 `B*T` 求和。注释特意写了 `by setting alpha=one`——其实代码注释略有口误，真正起作用的是 `beta=1`（accumulate），但表达的是「权重梯度累加」这件事。

#### 4.3.4 代码实践

**实践目标**：在 `gpt2_backward` 里确认反向三路的调用顺序与覆盖/累加约定。

**操作步骤**：

1. 打开 [train_gpt2.cu:900-924](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L900-L924)，这是 fcproj 层的反向。
2. 找到那行 `matmul_backward(dl_bt4c, dl_fcprojw, dl_fcprojb, dresidual, l_fch_gelu, l_fcprojw, scratchF, B, T, 4*C, C, ...)`（[train_gpt2.cu:900](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L900)）。
3. 对照 `matmul_backward` 的参数顺序 `(dinp, dweight, dbias, dout, inp, weight, dbias_buffer, ...)`，确认哪个输出是覆盖写、哪个是累加。

**需要观察的现象**：`dl_bt4c`（dinp）被覆盖写、`dl_fcprojw`（dweight）被累加、`dl_fcprojb`（dbias）由 kernel 内 `+=` 归约；三者都依赖该步早先的梯度清零。

**预期结果**：你能指出 fcproj 反向先归约 dbias、再算 dinp（覆盖）、最后算 dweight（累加）的顺序，并解释为什么 dweight 必须用 `+=`（多个 token 共享同一权重）。源码阅读型实践，无需运行（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`dinp` 为什么用 `beta=0`（覆盖）而 `dweight` 用 `beta=1`（累加）？

**参考答案**：`dinp` 的每个 `(b,t)` 位置是独立计算 \(\sum_{oc}\mathrm{dout}[b,t,oc]\cdot \mathrm{weight}[oc,c]\)，没有「多个源累加到同一目标」的情况，所以直接覆盖写即可。而 `dweight[c,oc]` 需要把所有 \(B*T\) 个位置的贡献 \(\mathrm{dout}[b,t,oc]\cdot \mathrm{inp}[b,t,c]\) 加起来，是沿 `B*T` 的归约，必须用累加；这与 u2-l3 CPU 版「dweight 沿 (b,t) 求和、用 `+=`」完全一致。

**练习 2**：为什么 llm.c 用自定义 kernel 算 `dbias`，而不是用 cuBLASLt 的 `BGRADB` 尾声？

**参考答案**：`dbias` 是沿 `B*T` 维的纯归约（不是矩阵乘），手写的 warp shuffle 归约 kernel（`matmul_backward_bias_kernel9`）针对这个形状更高效，还能按 OC 大小自适应决定是否走中间 buffer 二次归约。代码里算完 dbias 后立即把 `dbias` 置 `NULL`，正是为了**避免** dinp 的 GEMM 又顺手用 `BGRADB` 算一遍——把归约交给更擅长的自定义 kernel。

---

### 4.4 TF32 与精度

#### 4.4.1 概念说明

到这里要厘清三个常被混淆的精度概念：

- **数据精度（`floatX` / `CUBLAS_LOWP`）**：矩阵元素存成什么。由 `PRECISION` 决定：FP32（`float`）、FP16（`half`）、BF16（`__nv_bfloat16`），默认 BF16。详见 u5-l1 与 [llmc/cuda_common.h:82-92](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cuda_common.h#L82-L92)。
- **计算精度（`cublas_compute`）**：乘加（MAC）用什么累加。llm.c 默认 `CUBLAS_COMPUTE_32F`（FP32 累加），仅在 FP32 模式 + Ampere+ 时切到 `CUBLAS_COMPUTE_32F_FAST_TF32`。
- **TF32（TensorFloat-32）**：这是本节主角。它是 NVIDIA Ampere（SM 8.0）起 tensor core 支持的一种**中间格式**：输入是 FP32，但 tensor core 在做乘加时把每个 FP32 数的尾数截短到约 10 位、指数保留 8 位（即用一个「FP19」表示）来加速。换句话说，**TF32 不是一种存储类型，而是 FP32 数据走 tensor core 的一种更快、略低保真的计算模式**。

llm.c 注释里明确给出对照（[train_gpt2.cu:1190](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1190)）：

> TF32 precision is equivalent to `torch.set_float32_matmul_precision('high')`

即：开启 TF32 等同于告诉 PyTorch「FP32 的 matmul 可以用 high 精度（TF32 tensor core）而非最高精度（FP32 CUDA core）」。

一个**非常重要的约束**：TF32 只在 `PRECISION=FP32` 时才有意义。如果你用 BF16 或 FP16 训练，矩阵本身就是 BF16/FP16，直接走对应的 tensor core，根本不经过 TF32 这条路——所以代码里 `enable_tf32` 的条件第一个就是 `PRECISION_MODE == PRECISION_FP32`。

#### 4.4.2 核心流程

TF32 的开关逻辑在 `common_start`（[train_gpt2.cu:1190-1192](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1190-L1192)）：

```text
enable_tf32 = (PRECISION == FP32) AND (deviceProp.major >= 8) AND override_enable_tf32
cublas_compute = enable_tf32 ? CUBLAS_COMPUTE_32F_FAST_TF32 : CUBLAS_COMPUTE_32F
```

三个条件：FP32 模式、Ampere 及更新（major≥8）、用户没在命令行用 `-f 0` 关掉。三者都满足才用 TF32 计算类型。

#### 4.4.3 源码精读

全局变量与默认值（[llmc/cublas_common.h:28-31](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cublas_common.h#L28-L31)）：

```c
const size_t cublaslt_workspace_size = 32 * 1024 * 1024;
void* cublaslt_workspace = NULL;
cublasComputeType_t cublas_compute = CUBLAS_COMPUTE_32F;   // 默认 FP32 累加
cublasLtHandle_t cublaslt_handle;
```

TF32 的判定与赋值（[train_gpt2.cu:1186-1192](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1186-L1192)）：

```c
// set up cuBLAS and cuBLASLt
cublasCheck(cublasLtCreate(&cublaslt_handle));
cudaCheck(cudaMalloc(&cublaslt_workspace, cublaslt_workspace_size));

// TF32 precision is equivalent to torch.set_float32_matmul_precision('high')
bool enable_tf32 = PRECISION_MODE == PRECISION_FP32 && deviceProp.major >= 8 && override_enable_tf32;
cublas_compute = enable_tf32 ? CUBLAS_COMPUTE_32F_FAST_TF32 : CUBLAS_COMPUTE_32F;
```

`cublas_compute` 这个全局随后在 [llmc/matmul.cuh:126](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/matmul.cuh#L126) 被用来创建 operationDesc，于是整条 matmul 链路就都按这个计算精度跑了。

用户侧的开关：命令行 `-f`（[train_gpt2.cu:1446](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1446) 默认 1，[train_gpt2.cu:1485](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1485) 解析），最终传给 `common_start(override_enable_tf32, false)`（[train_gpt2.cu:1505](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1505)）。打印时还会把 `cublas_compute` 翻译成 `TF32` 或 `FP32` 字样（[train_gpt2.cu:1552](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1552)）。

一句话总结精度关系：

| `PRECISION` | `floatX` / `CUBLAS_LOWP` | `cublas_compute` | 走的硬件路径 |
| --- | --- | --- | --- |
| FP32（+Ampere+，默认开 `-f`） | `float` / `CUDA_R_32F` | `CUBLAS_COMPUTE_32F_FAST_TF32` | FP32 输入，TF32 tensor core |
| FP32（关闭 TF32） | `float` / `CUDA_R_32F` | `CUBLAS_COMPUTE_32F` | FP32 CUDA core（最慢最精） |
| BF16（默认） | `__nv_bfloat16` / `CUDA_R_16BF` | `CUBLAS_COMPUTE_32F` | BF16 tensor core，FP32 累加 |
| FP16 | `half` / `CUDA_R_16F` | `CUBLAS_COMPUTE_32F` | FP16 tensor core，FP32 累加 |

注意 BF16/FP16 模式下 `cublas_compute` 始终是 `CUBLAS_COMPUTE_32F`（用 FP32 累加中间结果以保证数值范围），TF32 在它们身上不出现。

#### 4.4.4 代码实践

**实践目标**：确认「TF32 只在 FP32 模式下生效」，并看清命令行开关如何传递。

**操作步骤**：

1. 读 [train_gpt2.cu:1191](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1191) 的 `enable_tf32` 表达式，列出它的三个条件。
2. 追 `-f` 参数从 [train_gpt2.cu:1485](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1485) 到 [train_gpt2.cu:1505](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1505) `common_start(override_enable_tf32, false)` 的传递路径。
3. 思考：若用 `make train_gpt2cu`（默认 BF16）编译运行，`cublas_compute` 会是哪个值？打印行 [train_gpt2.cu:1552](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1552) 会显示 `TF32` 还是 `FP32`？

**需要观察的现象**：BF16 模式下 `PRECISION_MODE != PRECISION_FP32`，所以 `enable_tf32=false`，`cublas_compute=CUBLAS_COMPUTE_32F`，打印显示 `FP32`（指累加精度，而非数据精度）。

**预期结果**：你能解释「为什么 BF16 训练时 TF32 开关完全不起作用」——因为 TF32 是 FP32 数据的加速通道，BF16 数据有自己的 tensor core 通道。待本地验证：在 BF16 下运行打印的精度行。

#### 4.4.5 小练习与答案

**练习 1**：有人说「BF16 训练时应该开 TF32 提速」，对吗？

**参考答案**：不对。TF32 是**FP32 输入**走 tensor core 的加速模式；BF16 训练时数据本身就是 BF16，已经走 BF16 tensor core 了，不经过 TF32 这条路。代码里 `enable_tf32` 的首个条件 `PRECISION_MODE == PRECISION_FP32` 正是把这个排除掉。

**练习 2**：`CUBLAS_COMPUTE_32F` 在 BF16 模式下意味着「全程 FP32」吗？

**参考答案**：不是。它指的是**乘加累加**用 FP32，而矩阵元素仍是 BF16（`CUBLAS_LOWP=CUDA_R_16BF`）。即每个 BF16 元素相乘后，乘积用 FP32 累加（避免 BF16 的小动态范围导致溢出/截断），最后再写回 BF16 输出。这是混合精度的常见做法，也呼应了 u5-l1 提到的「mean/rstd 等统计量恒为 float」。

---

## 5. 综合实践

把本讲四个模块串起来，做一个「全链路追踪」任务：跟踪 GPT-2 一个 Transformer block 里 matmul 的前向→反向全过程，把每一处 GELM 的「封装参数、epilogue、精度、覆盖/累加」都对上号。

具体步骤：

1. **数前向调用点**：在 [train_gpt2.cu:718-753](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L718-L753) 的循环体里，统计 `matmul_forward_cublaslt` 的调用。注意第 720 行和第 729 行被 `#ifdef ENABLE_CUDNN` 分成两条互斥分支（一条走 cuDNN attention、一条走手写 attention），所以**单次前向实际执行 5 处**：qkv、attproj、fc(升维)、fcproj(降维)、output(logits)。

2. **建对照表**：为这 5 处各填一行——
   | 层 | 对应权重 | `OC` | `bias` | `pre_gelu` | 启用的 epilogue |
   |---|---|---|---|---|---|
   | qkv | `l_qkvw` | `3C` | `l_qkvb` | NULL | `BIAS` |
   | attproj | `l_attprojw` | `C` | `l_attprojb` | NULL | `BIAS` |
   | fc（升维） | `l_fcw` | `4C` | `l_fcb` | `l_fch`（受 `gelu_fusion`） | `GELU_AUX_BIAS` 或 `BIAS` |
   | fcproj（降维） | `l_fcprojw` | `C` | `l_fcprojb` | NULL | `BIAS` |
   | output（logits） | `wte`（权重绑定） | `Vp` | **NULL** | NULL | `DEFAULT` |

3. **核对反向**：到 [train_gpt2.cu:900-924](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L900-L924) 找到对应的 `matmul_backward` 调用，确认每个线性层的反向都复用了同一个 `matmul_backward`，且 dbias 走自定义归约、dinp 覆盖、dweight 累加。

4. **精度核查**：回答——若以默认 BF16 编译运行，这 5 处 matmul 的数据类型是 `CUDA_R_16BF`、计算类型是 `CUBLAS_COMPUTE_32F`、TF32 开关不起作用；若以 FP32 编译且 `-f 1`，则计算类型切到 `CUBLAS_COMPUTE_32F_FAST_TF32`。

**预期产出**：一张完整的前向调用对照表 + 一段说明「同一个 `matmul_cublaslt` 封装如何通过参数差异覆盖 5 种不同线性层、3 种 epilogue、2 种累加模式」的文字。这是本讲最重要的综合练习，做完它你就真正读懂了 cuBLASLt matmul 在 llm.c 里的角色。

（本综合实践为源码阅读型，无需 GPU；如需运行验证，可在有 CUDA 环境的机器上 `make train_gpt2cu` 后用 `-ge 2 -f 1` 等参数对比 ncu 剖析里的 GEMM kernel 占比，待本地验证。）

## 6. 本讲小结

- llm.c 用 **cuBLASLt** 替代手写 GEMM，核心封装是 `matmul_cublaslt`，它把转置、`m/n/k` 维度、`alpha/beta`、bias、gelu 全参数化，前向反向乃至 batched attention 都复用它。
- **`transA=true`** 是「行主序权重喂给列主序 cuBLAS」的标准技巧：行主序 `(OC,C)` 与列主序 `(C,OC)` 内存逐字节相同，于是声明成 `C×OC` 布局再用 `OP_T` 转置参与相乘，无需搬运数据。
- **epilogue 融合**是 cuBLASLt 的杀手锏：bias、GELU 可在一次 GEMM 内完成，省一次显存往返；`GELU_AUX` 还会顺手把 pre-gelu 存到辅助指针供反向使用。是否融合 GELU 由 `model->gelu_fusion`（默认 0）控制。
- 反向 `matmul_backward` 求三路梯度：`dinp` 用 `beta=0` 覆盖、`dweight` 用 `beta=1` 累加、`dbias` 不走 GEMM 而用手写归约 kernel `matmul_backward_bias_kernel9`（按 OC 大小决定是否二次归约）。
- **TF32** 是 FP32 数据走 tensor core 的加速计算模式，等价于 `torch.set_float32_matmul_precision('high')`；它**仅在 `PRECISION=FP32` + Ampere+ + 未被 `-f 0` 关闭**时启用，BF16/FP16 模式下完全不出现。
- 数据精度 `CUBLAS_LOWP` 与计算精度 `cublas_compute` 是两件事：前者随 `PRECISION` 切 `floatX`，后者默认 `CUBLAS_COMPUTE_32F`（含 BF16/FP16 的 FP32 累加），仅 FP32 模式才可能升到 TF32。

## 7. 下一步学习建议

本讲把 matmul 这一层的 CUDA 主线实现讲透了，建议接下来：

- **u5-l4（各层 CUDA kernel）**：看 layernorm、encoder、gelu、adamw、global_norm 等「不适合交给 cuBLAS」的算子是如何手写 CUDA kernel 的，尤其是 layernorm 的 warp reduce，与本讲 dbias 的归约 kernel 思路相通。
- **u5-l5（Attention CUDA）**：attention 也大量用到 matmul（QK^T、att@V），其中非 flash-attention 路径正是用本讲的 `batch_count` strided batched GEMM；学完后可回头体会 `matmul_cublaslt` 的 batch 参数为何而设。
- **u6-l1（混合精度与 master weights）**：本讲的 BF16/FP32/TF32 是基础，u6-l1 会讲 master weights 如何在 BF16 训练时用 FP32 备份权重、再把更新写回 BF16，把「数据精度 vs 计算精度」的故事补完。
- 继续阅读 `llmc/matmul.cuh` 里 `matmul_backward_bias_kernel9` 和 `reduce_add_sum_kernel` 的 warp shuffle 归约细节，以及 NVIDIA 官方的 [cuBLASLt 文档](https://docs.nvidia.com/cuda/cublas/#cublasltmatmul) 对 epilogue 的完整列表。

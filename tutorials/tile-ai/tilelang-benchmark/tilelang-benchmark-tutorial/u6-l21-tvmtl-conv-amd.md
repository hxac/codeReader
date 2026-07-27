# tvm.tl 变体与卷积(AMD/HIP)

## 1. 本讲目标

学完本讲，你应该能够：

1. 区分同一套 TileLang DSL 的两种「宿主」——TVM 内置的 `tvm.tl` 变体与独立的 `tilelang` 包，并说出它们在 `import`、`target`、`profiler`、`autotune` 返回值上的差异。
2. 理解一个 2D 卷积是如何被「拍扁」成一个带边界判断的 GEMM（即 im2col 思路），从而复用块级 GEMM 内核。
3. 掌握 `T.Buffer` 视图重排（`kernel_flat` / `out_flat`）与 `in_bound` 掩码如何实现 padding 与 stride。
4. 看懂 CDNA（AMD MI300x）目录下卷积基准的「内核 → 驱动 shell → 日志提取」完整链路。

## 2. 前置知识

- **NHWC vs NCHW 布局**：深度学习里一张特征图通常记为 4 维。NCHW 是「批次-通道-高-宽」，NHWC 是「批次-高-宽-通道」。本讲的卷积内核与 torch 参考都用 **NHWC**，因为通道 C 放在最后一维时，相邻元素在内存里连续，利于合并访存。
- **im2col（image to column）**：卷积本来是「滑窗 + 逐元素乘加」，但每个输出像素的计算其实就是一次向量点积。如果把每个滑窗展开成一列，整个卷积就变成了一个大矩阵乘（GEMM）。本讲内核并不真的在内存里展开图像，而是**在加载到 shared memory 时即时换算地址**，等价于 im2col 却省下了展开的显存。
- **HIP / ROCm**：HIP 是 AMD 的 GPU 编程模型，地位相当于 NVIDIA 的 CUDA。`target="hip"` 表示把 TileLang DSL 编译成跑在 AMD GPU 上的 HIP 代码；你之前在 hopper/ampere 上看到的 `target="auto"` 或 CUDA 路径，对应的是 NVIDIA。
- **块级 GEMM 五要素**（依赖 u3-l9）：`T.Kernel` 网格、`alloc_shared`/`alloc_fragment`、`T.copy`、`T.gemm`、`T.Pipelined`。本讲的卷积内核**完全复用**这套骨架，只是把「取数据」这一步从普通的 `T.copy` 换成了带边界判断的 im2col 加载。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [benchmark_tilelang_conv.py](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/benchmark_tilelang_conv.py) | 本讲主角：用 `tvm.tl` 变体写的 fp16 卷积内核 + `argparse` 驱动。 |
| [benchmark_torch_conv.py](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/benchmark_torch_conv.py) | torch 参考基线：一批 NHWC 卷积 shape，用 `torch.conv2d` 计时。 |
| [benchmark_tilelang.sh](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/benchmark_tilelang.sh) | 驱动脚本：按 shape 列表循环调用上面的 `.py`，逐 shape tee 出日志。 |
| [conv_tlops_extract.py](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/conv_tlops_extract.py) | 日志→数据：用正则从日志抽 `Best TFlops`，对应 u2-l7 的可视化管线。 |

> 同目录还有 `benchmark_ladder_conv.py` / `benchmark_ladder.sh`（Ladder 基线，AMD 专用），属于 u7-l23 的基线生态，本讲不展开。

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：**tvm.tl 变体的 API 差异**、**卷积→GEMM 的 im2col 映射**、**Buffer 重排与 in_bound 边界处理**、**调优旋钮与驱动链路**。

### 4.1 tvm.tl 变体 vs 独立 tilelang 包

#### 4.1.1 概念说明

你在前几讲看到的 TileLang 内核（hopper 的 dense matmul、FlashAttention 等）都来自**独立的 `tilelang` Python 包**：`import tilelang as tl`、`import tilelang.language as T`。而本讲的卷积内核来自**TVM 内置的 `tvm.tl` 变体**：`from tvm import tl`、`import tvm.tl.language as T`。

为什么要分两套？历史上 TileLang 最早就长在 TVM 项目内部（`tvm.tl` 子模块），后来才被抽出来做成独立的 `tilelang` 包。本仓库里两种写法并存：

- 独立 `tilelang` 包：hopper / ampere 几乎全部，以及 cdna 的大部分（gemm、mha、mla、dequant）。
- `tvm.tl` 变体：本讲的 **conv**，以及 `cdna_benchmark/mha_benchmark/test_tilelang_mha.py`。

两者共享几乎相同的 DSL 语法（`T.prim_func`、`T.Kernel`、`T.copy`、`T.gemm`、`@autotune`、`@jit`），但在四个工程细节上有可验证的差异。

#### 4.1.2 核心流程

下表把两种变体的差异列清楚（**全部基于代码实测**）：

| 维度 | `tvm.tl` 变体（本讲 conv） | 独立 `tilelang` 包（如 cdna gemm、hopper dense） |
|---|---|---|
| `import` | `from tvm import tl` + `import tvm.tl.language as T` + `from tvm.tl.autotuner import *` | `import tilelang as tl` + `import tilelang.language as T` + `from tilelang.autotuner import autotune, jit` |
| `target` | 显式 `target="hip"`（AMD） | NVIDIA 上常 `target="auto"`；**但 cdna 上同样写 `target="hip"`** |
| `profiler` | 显式 `profiler="tvm"` | 省略（用默认 profiler） |
| `autotune` 返回值 | **3 元组** `(best_latency, best_config, ref_latency)` | **对象** `best_result`，含 `.latency/.config/.ref_latency/.kernel` |

> ⚠️ **以代码为准（重要纠正）**：直觉上容易把 `target="hip"` 当成 `tvm.tl` 的特征，但这是错的。`target` 取决于**目标 GPU**，不是 API 变体——独立 `tilelang` 包在 CDNA 上同样写 `target="hip"`（见 cdna gemm 第 37 行）。真正区分两种变体的是 **import 路径、`profiler="tvm"`、返回值是元组还是对象**。

#### 4.1.3 源码精读

**import（tvm.tl 变体）**——注意三行都来自 `tvm` 命名空间：

[benchmark_tilelang_conv.py:2-4](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/benchmark_tilelang_conv.py#L2-L4) —— `from tvm import tl` 取出 `tl`（提供 `TensorSupplyType` 等）；`import tvm.tl.language as T` 取出 DSL 原语；`from tvm.tl.autotuner import *` 取出 `@autotune`/`@jit`。

对照独立包的写法（同样三行，命名空间换成 `tilelang`）：

[hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py:8-10](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L8-L10) —— `import tilelang as tl` + `import tilelang.language as T` + `from tilelang.autotuner import autotune, jit`。

**装饰器与 target/profiler**：

[benchmark_tilelang_conv.py:46-47](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/benchmark_tilelang_conv.py#L46-L47) —— `@autotune(configs=..., keys=..., warmup=10, rep=10)` 套 `@jit(out_idx=[2], supply_type=..., ref_prog=..., skip_check=True, profiler="tvm", target="hip")`。这里同时显式给出 `profiler="tvm"` 与 `target="hip"`。

对照独立包（cdna gemm，同样在 AMD 上）：

[cdna_benchmark/gemm_benchmark/1.tilelang_benchmark/benchmark_tilelang_matmul.py:37](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/gemm_benchmark/1.tilelang_benchmark/benchmark_tilelang_matmul.py#L37) —— `@jit(..., skip_check=True, target="hip")`，**没有** `profiler` 参数，但 `target` 同样是 `"hip"`。这就是上表「target 取决于 GPU」的直接证据。

**返回值差异（最硬的证据）**——看驱动代码怎么消费 `kernel()` 的返回值：

[benchmark_tilelang_conv.py:110](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/benchmark_tilelang_conv.py#L110) —— `best_latency, best_config, ref_latency = convolution(...)`，**直接解包成 3 个变量**，说明 `tvm.tl` 的 `@autotune` 返回一个三元组。

对照独立包（hopper dense matmul）：

[hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py:271-275](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L271-L275) —— `best_result = matmul(...)`，再用 `best_result.latency` / `.config` / `.ref_latency` / `.kernel.get_kernel_source()` 逐字段取值，说明独立包返回**对象**。

> 关于 `profiler="tvm"` 与独立包默认 profiler 的**内部行为差异**（计时方式、输出格式是否完全一致）：本仓库源码无法确认，标注 **待确认**。能确认的只有「tvm.tl 变体显式传 `profiler="tvm"`，独立包省略该参数」。

#### 4.1.4 代码实践

**实践目标**：用「返回值怎么被消费」反推两种变体的 API 差异。

**操作步骤**：

1. 打开 [benchmark_tilelang_conv.py:110](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/benchmark_tilelang_conv.py#L110)，确认它把返回值解包成 3 个变量。
2. 打开 [hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py:271-275](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L271-L275)，确认它用 `.latency` 等属性访问。
3. 想一想：如果把 conv 内核的 `from tvm import tl` 改成 `import tilelang as tl`、把第 110 行改成 `best_result = convolution(...)` 再取 `.latency`，代码逻辑是否还能跑通？（取决于两套 API 的对象是否一致——这是独立包与 tvm.tl 能否互换的关键。）

**预期结果**：你能不靠注释、只靠「驱动代码如何消费返回值」就判断一个文件用的是哪种变体。

**待本地验证**：步骤 3 的互换是否真的可行，需要在装有两套依赖的 AMD 机器上实测。

#### 4.1.5 小练习与答案

**练习 1**：项目里 `cdna_benchmark/mha_benchmark/test_tilelang_mha.py` 用的是哪种变体？依据是什么？

**答案**：`tvm.tl` 变体。依据是其 import 写的是 `from tvm import tl` / `import tvm.tl.language as T` / `from tvm.tl.autotuner import *`（与本讲 conv 相同），而同目录正式版 `benchmark_tilelang_mha.py` 用的是独立 `tilelang` 包。

**练习 2**：为什么不能单凭 `target="hip"` 判断一个内核是 `tvm.tl` 变体？

**答案**：因为独立 `tilelang` 包在 CDNA 上同样用 `target="hip"`（见 cdna gemm 第 37 行）。`target` 描述目标硬件（AMD→hip、NVIDIA→cuda/auto），与 API 变体无关。

### 4.2 卷积→GEMM 的 im2col 映射

#### 4.2.1 概念说明

卷积的标准定义（NHWC 输入 `data[N,H,W,C]`、权重 `kernel[F,KH,KW,C]`、输出 `out[N,OH,OW,F]`）：

\[
\text{out}[n,oh,ow,f] = \sum_{kh,kw,c} \text{data}\big[n,\; oh\cdot S + kh\cdot D - P,\; ow\cdot S + kw\cdot D - P,\; c\big] \cdot \text{kernel}[f,kh,kw,c]
\]

其中 \(S\) 是 stride、\(D\) 是 dilation、\(P\) 是 padding。仔细看这个式子：右边是一次对 \((kh,kw,c)\) 的**点积**。如果把每个输出像素看成一「行」、把 \((kh,kw,c)\) 展平成一「列」，那么整个卷积就是一次矩阵乘：

- 矩阵 \(A\) 的行 = 输出位置 \((n,oh,ow)\)，共 \(M = N\cdot OH\cdot OW\) 行；
- 矩阵 \(B\) 的列（或说行，转置后）= 卷积核参数 \((kh,kw,c)\)，共 \(K = KH\cdot KW\cdot C\) 个；
- 矩阵 \(C\) 的列 = 输出通道 \(f\)，共 \(N_{gemm}=F\) 列。

输出尺寸由标准公式给出：

\[
OH = \left\lfloor\frac{H + 2P - D(KH-1) - 1}{S}\right\rfloor + 1,\qquad OW = \left\lfloor\frac{W + 2P - D(KW-1) - 1}{S}\right\rfloor + 1
\]

这就是 [benchmark_tilelang_conv.py:26-27](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/benchmark_tilelang_conv.py#L26-L27) 计算的 `OH`/`OW`。算力则是把卷积当 GEMM 后的标准 \(2MNK\)：

\[
\text{FLOPS} = 2 \cdot (N\cdot OH\cdot OW) \cdot F \cdot (C\cdot KH\cdot KW)
\]

对应 [benchmark_tilelang_conv.py:109](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/benchmark_tilelang_conv.py#L109) 的 `total_flops = 2 * N * C * OH * OW * F * K * K`（这里 `K` 是方核边长，`KH=KW=K`）。

#### 4.2.2 核心流程

本讲内核并不真的在显存里展开 im2col 矩阵（那会爆显存），而是**把展开逻辑折叠进 shared memory 的加载步骤**，整体仍是一个标准块级 GEMM：

```
1. 把输出 out 看成 (N*OH*OW, F) 的矩阵 out_flat      ← 行=M, 列=F
2. 把权重 kernel 看成 (F, KH*KW*C) 的矩阵 kernel_flat ← 行=F, 列=K
3. 网格：bx 遍历 F 的块(block_N)，by 遍历 N*OH*OW 的块(block_M)
4. K 维循环：把 KH*KW*C 按 block_K 分块，每块：
     a. data_shared  ← 用 im2col 公式即时换算地址，从全局 data 取数(带边界判断)
     b. kernel_shared ← 从 kernel_flat 直接拷贝(规整布局，无需换算)
     c. out_local += data_shared @ kernel_shared^T   ← T.gemm(transpose_B=True)
5. 回写：out_local → out_shared → out_flat
```

关键点：`data` 的内存布局是 4 维 `(N,H,W,C)`，但 GEMM 需要的「行」是 \((n,oh,ow)\)、「列」是 \((kh,kw,c)\)——两者对不齐，所以**取 data 时必须算地址**；而 `kernel` 虽然逻辑上是 `(F,KH,KW,C)`，但它天然就是「行=F、列连续走 (kh,kw,c)」，所以可以用一个 `T.Buffer` 视图直接当 GEMM 的 \(B\) 用，无需换算。

#### 4.2.3 源码精读

**精度三件套**——fp16 输入、fp32 累加、fp16 输出（与 u4-l12 一致的「窄输入、宽累」约定）：

[benchmark_tilelang_conv.py:31-33](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/benchmark_tilelang_conv.py#L31-L33) —— `dtype="float16"`、`accum_dtype="float"`、`out_dtype="float16"`。

**网格映射**——`bx` 绑输出通道块、`by` 绑空间块：

[benchmark_tilelang_conv.py:56-59](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/benchmark_tilelang_conv.py#L56-L59) —— `T.Kernel(T.ceildiv(F, block_N), T.ceildiv(N*OH*OW, block_M), threads=thread_num) as (bx, by)`。这与 u3-l9 的 GEMM 网格同构，只是「M 维」换成了 `N*OH*OW`。

**Buffer 视图重排**——把 4 维张量看成 2 维 GEMM 矩阵：

[benchmark_tilelang_conv.py:65-66](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/benchmark_tilelang_conv.py#L65-L66) —— `kernel_flat = T.Buffer((F, KH*KW*C), dtype, kernel.data)` 用**同一个底层指针 `kernel.data`** 重新解释成 `(F, KH*KW*C)`；`out_flat = T.Buffer((N*OH*OW, F), out_dtype, out.data)` 同理把输出看成 `(M, F)`。这是零拷贝的「布局视图」，是 im2col 能挂上 GEMM 的接口。

> 注意：这里**只有** `kernel` 和 `out` 做了视图重排，`data` 没有——因为 data 需要的 im2col 重排是非线性的（要算 stride/padding），无法用一个简单 `T.Buffer` 形状表达，只能在加载时逐元素换算（见 4.3）。

**K 维分块流水线 + gemm**：

[benchmark_tilelang_conv.py:70-86](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/benchmark_tilelang_conv.py#L70-L86) —— 外层 `for k_iter in T.Pipelined(T.ceildiv(KH*KW*C, block_K), num_stages=...)` 把规约维 \(K=KH\cdot KW\cdot C\) 分块并软件流水；每个 `k_iter` 先填 `data_shared`（带边界判断的 im2col 加载）、再 `T.copy(kernel_flat[...], kernel_shared)`、最后 `T.gemm(data_shared, kernel_shared, out_local, transpose_B=True, k_pack=k_pack)`。`transpose_B=True` 是因为 `kernel_shared` 存成 `(block_N, block_K)`，而 gemm 要算 \(C += A \cdot B^{\top}\)。

#### 4.2.4 代码实践

**实践目标**：确认「卷积 = GEMM」这一映射在代码里处处自洽。

**操作步骤**：

1. 在 [benchmark_tilelang_conv.py:51-55](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/benchmark_tilelang_conv.py#L51-L55) 读出三个 buffer 的形状：`data(N,H,W,C)`、`kernel(F,KH,KW,C)`、`out(N,OH,OW,F)`。
2. 对照 [行 65-66](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/benchmark_tilelang_conv.py#L65-L66) 的 `kernel_flat(F, KH*KW*C)` 与 `out_flat(N*OH*OW, F)`，验证：GEMM 的 \(M=N\cdot OH\cdot OW\)、\(N_{gemm}=F\)、\(K=KH\cdot KW\cdot C\)。
3. 对照 [行 109](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/benchmark_tilelang_conv.py#L109) 的 `total_flops`，验证它等于 \(2 \cdot M \cdot N_{gemm} \cdot K\)。

**预期结果**：三处尺寸与 FLOPS 公式完全对齐，说明内核在数学上就是一次 GEMM。

**待本地验证**：无（纯源码阅读）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `kernel` 能用 `T.Buffer((F, KH*KW*C), ...)` 直接重排，而 `data` 不能？

**答案**：`kernel` 在内存里就是 `(F, KH, KW, C)` 行主序，把中间两维 `KH,KW` 和最后一维 `C` 合并成 `KH*KW*C` 是连续内存的「合并相邻轴」，等价于换个 shape 看，零代价。`data` 需要的 im2col 重排要把「输出位置」映射到「输入位置」（含 stride/dilation/padding），是非线性地址变换，不能靠改 shape 完成。

**练习 2**：若把 `transpose_B=True` 改成 `False`，计算结果会怎样？

**答案**：会出错。`kernel_shared` 形状是 `(block_N, block_K)`（行=输出通道、列=规约维），即 GEMM 的 \(B^{\top}\)。要算 \(C += A\cdot B^{\top}\) 必须 `transpose_B=True`；若改 `False`，相当于算 \(A\cdot B\)，维度与语义都不对。

### 4.3 边界判断、Buffer 重排与 in_bound 掩码

#### 4.3.1 概念说明

im2col 加载的核心难题是：**给定 GEMM 里的行号 `m` 和列号 `k`，反推出该去 `data` 的哪个地址取数，并判断这个地址是否落在 padding 区**。

- `m` 编码输出位置 \((n,oh,ow)\)（输出按 `N,OH,OW,F` 行主序展开成 `N*OH*OW` 行）；
- `k` 编码规约维 \((kh,kw,c)\)（按 `KH,KW,C` 行主序展开成 `KH*KW*C` 列）。

把 `m`、`k` 拆回各分量，再代入卷积的坐标映射，就得到要访问的输入地址。若地址落在 `[0,H)×[0,W)` 之外，就是 padding 区，填 0——这就是 `in_bound` 掩码的作用。它让同一个 GEMM 内核既能处理图像内部的有效像素，也能处理边缘/角落的 padding，无需为不同像素写不同代码。

#### 4.3.2 核心流程

把 `m`、`k` 拆解与地址换算整理成公式（与代码逐字符对应）：

**拆 `m`（输出位置）**：\(m = n\cdot(OH\cdot OW) + oh\cdot OW + ow\)

\[
n = m // (OH\cdot OW),\qquad oh = (m \bmod (OH\cdot OW)) // OW,\qquad ow = m \bmod OW
\]

**拆 `k`（规约维）**：\(k = kh\cdot(KW\cdot C) + kw\cdot C + c\)

\[
kh = k // (KW\cdot C),\qquad kw = (k // C) \bmod KW,\qquad c = k \bmod C
\]

**输入地址（代入 stride \(S\)、dilation \(D\)、padding \(P\)）**：

\[
\text{access\_h} = oh\cdot S + kh\cdot D - P
\]

\[
\text{access\_w} = ow\cdot S + kw\cdot D - P
\]

**边界掩码**：

\[
\text{in\_bound} = (0 \le \text{access\_h} < H) \wedge (0 \le \text{access\_w} < W)
\]

\[
\text{data\_shared}[i,j] = \begin{cases} \text{data}[n, \text{access\_h}, \text{access\_w}, c] & \text{in\_bound 为真} \\ 0 & \text{否则（padding 区）} \end{cases}
\]

这套公式同时实现了三件事：**stride**（\(oh\cdot S\)）、**dilation**（\(kh\cdot D\)）、**padding**（\(-P\) 与 `in_bound` 的 0 回退）。

#### 4.3.3 源码精读

**im2col 加载主体**——本讲最关键的一段：

[benchmark_tilelang_conv.py:71-84](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/benchmark_tilelang_conv.py#L71-L84) —— 双重 `T.Parallel(block_M, block_K)` 循环里：

- `k = k_iter * block_K + j`（列号 → 规约维索引）；
- `m = by * block_M + i`（行号 → 输出位置索引）；
- `access_h = m % (OH*OW)//OW * S + k//(KW*C) * D - P`（即 \(oh\cdot S + kh\cdot D - P\)）；
- `access_w = m % OW * S + k//C % KW * D - P`（即 \(ow\cdot S + kw\cdot D - P\)）；
- `in_bound = (access_h>=0) and (access_w>=0) and (access_h<H) and (access_w<W)`；
- `data_shared[i,j] = T.if_then_else(in_bound, data[m//(OH*OW), access_h, access_w, k%C], 0)` —— 命中取真值，padding 填 0。

**权重的规整拷贝（对照）**——无需换算：

[benchmark_tilelang_conv.py:85](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/benchmark_tilelang_conv.py#L85) —— `T.copy(kernel_flat[bx*block_N, k_iter*block_K], kernel_shared, coalesced_width=coalesced_width)`，直接按 `(block_N, block_K)` 子块拷贝。对比 `data` 的逐元素换算，能直观体会「布局对齐」与「im2col」的差别。

**回写两步走**——与 u3-l9 一致：

[benchmark_tilelang_conv.py:87-88](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/benchmark_tilelang_conv.py#L87-L88) —— `T.copy(out_local, out_shared)` 再 `T.copy(out_shared, out_flat[by*block_M, bx*block_N])`。寄存器（fragment）的分布布局先经 shared memory 重排成可合并写的规整布局，再写回全局的 `out_flat` 视图。

#### 4.3.4 代码实践（本讲指定实践）

**实践目标**：把 `data_shared` 加载时的 `access_h` / `access_w` / `in_bound` 计算整理成公式，并用一个手算例子验证它如何实现 padding 与 stride。

**操作步骤**：

1. 先把 4.3.2 的公式抄一遍，对照 [行 74-81](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/benchmark_tilelang_conv.py#L74-L81) 逐项确认符号对应（`m % (OH*OW)//OW` ↔ \(oh\)、`k//(KW*C)` ↔ \(kh\)，等等）。
2. 取一个**手算友好**的小配置：`N=1, C=4, H=3, W=3, F=1, K=3, S=1, D=1, P=1`（方核 `KH=KW=3`）。先算 `OH=OW=3`、`KW*C=12`、`OH*OW=9`。
3. **例子 A（输出中心像素）**：取 \(oh=1, ow=1, n=0\)，则 \(m = 0\cdot9 + 1\cdot3 + 1 = 4\)。
   - 中心 tap \(kh=1,kw=1,c=0\)：\(k = 1\cdot12 + 1\cdot4 + 0 = 16\)。
     - `access_h = 1·1 + 1·1 − 1 = 1`，`access_w = 1·1 + 1·1 − 1 = 1`，`in_bound = (0≤1<3) ∧ (0≤1<3) = 真` → 取 `data[0,1,1,0]`（输入中心）。
   - 角落 tap \(kh=0,kw=0,c=0\)：\(k=0\)。
     - `access_h = 1 + 0 − 1 = 0`，`access_w = 1 + 0 − 1 = 0`，`in_bound = 真` → 取 `data[0,0,0,0]`（因 `P=1` 偏移，角落 tap 命中输入左上角）。
4. **例子 B（输出左上像素，命中 padding）**：取 \(oh=0, ow=0\)，则 \(m=0\)。
   - 角落 tap \(kh=0,kw=0,c=0\)：\(k=0\)。
     - `access_h = 0·1 + 0·1 − 1 = −1`，`in_bound = (−1≥0)? 假` → 填 `0`（左上 padding 区）。
   - 中心 tap \(kh=1,kw=1,c=0\)：\(k=16\)。
     - `access_h = 0 + 1 − 1 = 0`，`access_w = 0 + 1 − 1 = 0`，`in_bound = 真` → 取 `data[0,0,0,0]`（中心 tap 仍能命中输入左上角）。

**需要观察的现象**：

- 同一个输出像素（同一 `m`）的不同 tap（不同 `k`），有的命中输入、有的落在 padding——全部由 `in_bound` 自动区分，内核代码只有一份。
- `P=1` 的偏移让角落 tap 在「输出中心像素」处落到输入内部、在「输出边缘像素」处落到输入外（padding），这正是 padding 该有的行为。
- `S=1` 时相邻输出像素对应的 `access_h`/`access_w` 相差 1；若把 `S` 改大，相邻输出像素的访问地址会拉开 `S`，体现 stride。

**预期结果**：你能用纸笔算出任意 `(m, k)` 的访问地址与是否 padding，且与代码公式逐项对齐。

**待本地验证**：若想看真实地址流，可在 [行 82-84](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/benchmark_tilelang_conv.py#L82-L84) 的 `data_shared[i,j] = ...` 之前加一行调试打印（具体打印 API 以 tvm.tl 文档为准），在 AMD 机器上跑小 shape 观察——属于可选验证。

#### 4.3.5 小练习与答案

**练习 1**：若设 `P=0`（无 padding）、`K=3`，输出中心像素的角落 tap `access_h` 会是多少？说明什么？

**答案**：中心像素 \(oh=1\)、角落 tap \(kh=0\)、`S=1,D=1`：`access_h = 1·1 + 0·1 − 0 = 1`，仍合法。但输出**边缘**像素（\(oh=0\)）的角落 tap `access_h = 0 + 0 − 0 = 0` 合法、而要算 \(kh\) 更小的负向时无 padding 兜底，输出尺寸会缩小（`OH = H − K + 1`）。说明 `P` 的作用正是补齐边缘，让卷积后尺寸可控。

**练习 2**：`k // C % KW` 这一段先除后模，为什么能正确取出 \(kw\)？

**答案**：`k` 按 `(kh, kw, c)` 行主序展开，`c` 最内层（跨度为 1）、`kw` 中间层（跨度为 `C`）。`k // C` 去掉最内层 `c`，得到 `kh*KW + kw`；再 `% KW` 即得 `kw`。等价于把 `k` 沿 `C` 轴「下采样」到 `(kh, kw)` 平面再取列号。

### 4.4 调优旋钮与驱动链路

#### 4.4.1 概念说明

卷积内核的 `@autotune` 搜索空间由 `get_configs()` 用 `itertools.product` 暴力枚举，**没有**走 u3-l10 讲的 Roller 自动调优（tvm.tl 变体里也没有 Roller 分支）。此外内核里还有两个「半固定」旋钮 `k_pack` 与 `coalesced_width`，以及驱动 shell 的 shape 循环。

#### 4.4.2 核心流程

- **搜索空间**：`block_M ∈ {32,64,128,256}` × `block_N ∈ {32,64,128,256}` × `block_K ∈ {32,64}` × `num_stages ∈ {0,1,2}` × `thread_num ∈ {128,256}`，共 \(4·4·2·3·2 = 192\) 个配置。
- **`k_pack=2`**：传给 `T.gemm`，提示把 2 个 fp16 元素打包进一个 32 位寄存器元素，匹配 TensorCore/MFMA 指令的数据排版。
- **`coalesced_width=None`**：传给 `T.Parallel` 与 `T.copy`，控制合并访存宽度；`None` 表示交给编译器自动选择。
- **驱动 shell**：`benchmark_tilelang.sh` 把一组 shape（`n c h w f k s d p`）循环喂给 `python benchmark_tilelang_conv.py --n ...`，每 shape 一个日志文件。

#### 4.4.3 源码精读

**搜索空间枚举**：

[benchmark_tilelang_conv.py:9-22](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/benchmark_tilelang_conv.py#L9-L22) —— `itertools.product` 生成 192 个 dict，与 u3-l8 讲的 `with_roller=False` 暴搜同构（注意 tvm.tl 变体**没有** Roller 分支，与独立包 dense matmul 双分支不同）。

**两个旋钮**：

[benchmark_tilelang_conv.py:44-45](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/benchmark_tilelang_conv.py#L44-L45) —— `k_pack = 2`、`coalesced_width = None`，写死在函数里、不进搜索空间。

**驱动 shell 的 shape 循环**：

[benchmark_tilelang.sh:112-126](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/benchmark_tilelang.sh#L112-L126) —— `for shape in "${shapes[@]}"` 读出 9 个参数，调用 `python benchmark_tilelang_conv.py --n ... --p ...`，`tee` 到 `logs/${id}.conv_${n}_${c}_${h}_${w}_${f}_${k}_${s}_${d}_${p}.log`，并 `id=$((id+1))` 自增编号。

**日志提取**：

[conv_tlops_extract.py:153-166](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/conv_tlops_extract.py#L153-L166) —— 用正则 `Best TFlops:\s*([\d\.]+)` 从每个日志抽 TFlops（对应 [驱动打印行 112](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/benchmark_tilelang_conv.py#L112) 的 `print(f"Best TFlops: ...")`）。注意该脚本**还尝试抽 `Ref TFlops`**，但 conv 驱动只打印了 `Best TFlops`，故 `ref_tflops` 对本内核恒为 `None`。

> 历史遗留提醒：`benchmark_tilelang.sh` 默认只解注释了一行 shape（`"32 3 224 224 64 7 2 1 3"`），其余全被注释；`conv_tlops_extract.py` 里的 shape 列表与 shell 也不完全一致。复现时须以实际启用的 shape 为准，别盲信脚本字面量——这是本项目一贯的「以代码为准」原则。

#### 4.4.4 代码实践

**实践目标**：理解 `k_pack` / `coalesced_width` 两个旋钮的去向，以及 shell 如何逐 shape 产日志。

**操作步骤**：

1. 在 [行 86](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/benchmark_tilelang_conv.py#L86) 找到 `T.gemm(..., k_pack=k_pack)`，确认 `k_pack` 只影响 gemm。
2. 在 [行 71](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/benchmark_tilelang_conv.py#L71) 与 [行 85](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/benchmark_tilelang_conv.py#L85) 找到两处 `coalesced_width=coalesced_width`，确认它同时作用于并行加载与拷贝。
3. 跟踪 [benchmark_tilelang.sh:112-126](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/benchmark_tilelang.sh#L112-L126)：一条 shape 产生哪个日志文件名（注意文件名里带 `${id}` 编号）。

**预期结果**：能说出两个旋钮各影响哪条语句、一条 shape 跑完会在 `logs/` 下生成哪个文件。

**待本地验证**：在 AMD 机器上把 `k_pack` 从 2 改成 1 观察性能变化——属于可选验证。

#### 4.4.5 小练习与答案

**练习 1**：本内核的搜索空间大小是多少？为什么说它「没有 Roller」？

**答案**：\(4·4·2·3·2 = 192\)。没有 Roller 是因为 `get_configs` 只有 `itertools.product` 暴搜一条路，没有独立包 dense matmul 里那种 `with_roller=True` 的 `MatmulTemplate.recommend_hints` 分支（见 u3-l10）。

**练习 2**：`conv_tlops_extract.py` 抽取 `Ref TFlops` 对本内核会得到什么？为什么？

**答案**：`None`。因为 conv 驱动 [行 110-113](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/benchmark_tilelang_conv.py#L110-L113) 只 `print` 了 `Best latency` 与 `Best TFlops`，没有打印 `Ref TFlops` 这一行，正则匹配不到就返回 `None`。

## 5. 综合实践

把本讲四个模块串起来，完成一次「读图—对账—手算」的综合任务。

**任务**：给定 shape `(N=1, C=4, H=3, W=3, F=4, K=3, S=1, D=1, P=1)`（NHWC、方核），回答下列问题，全部基于源码与公式，不靠运行：

1. **API 识别**：这个内核用的是 `tvm.tl` 变体还是独立 `tilelang` 包？给出两条代码证据（提示：import、返回值解包）。
2. **GEMM 对账**：算出 `OH`、`OW`、GEMM 的 \(M/N_{gemm}/K\)、以及 `total_flops`。对照 [行 109](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/benchmark_tilelang_conv.py#L109) 验证。
3. **im2col 手算**：取输出像素 \(oh=0,ow=2\)（右上角），写出它的 `m`；再取 tap \(kh=2,kw=0,c=1\)，写出 `k`、`access_h`、`access_w`、`in_bound`，并判断该取 `data[?,?,?,?]` 还是 0。
4. **跨架构迁移**：若要把这个卷积内核搬到 NVIDIA H100 上，至少要改哪两处？（提示：`target`、`import`/profiler 的取舍。）

**参考答案要点**：

1. `tvm.tl` 变体。证据：import 是 `from tvm import tl`（[行 2](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/benchmark_tilelang_conv.py#L2)）；返回值被解包成 3 元组（[行 110](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/benchmark_tilelang_conv.py#L110)）。
2. `OH=OW=3`；\(M=1·3·3=9\)；\(N_{gemm}=F=4\)；\(K=3·3·4=36\)；`total_flops = 2·9·4·36 = 2592`（即 \(2·N·C·OH·OW·F·K·K = 2·1·4·3·3·4·3·3\)）。
3. \(m = 0·9 + 0·3 + 2 = 2\)。`KW*C = 3·4 = 12`；\(k = kh·12 + kw·4 + c = 2·12 + 0·4 + 1 = 25\)。`access_h = oh·S + kh·D − P = 0 + 2 − 1 = 1`；`access_w = ow·S + kw·D − P = 2 + 0 − 1 = 1`；`in_bound = (0≤1<3) ∧ (0≤1<3) = 真`；取 `data[0, 1, 1, c=1]`。
4. 至少把 [行 47](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/benchmark_tilelang_conv.py#L47) 的 `target="hip"` 改成 `target="cuda"`（或 `"auto"`）；import 是否要从 `tvm.tl` 换成独立 `tilelang` 包、以及 `profiler="tvm"` 是否保留，取决于目标机器装的是哪套依赖——**待本地验证**。

## 6. 本讲小结

- 同一套 TileLang DSL 有两种宿主：TVM 内置的 `tvm.tl` 变体（`from tvm import tl`）与独立 `tilelang` 包（`import tilelang`）；区分它们看 **import 路径、`profiler="tvm"`、返回值是 3 元组还是对象**，而**不是**看 `target`。
- `target="hip"` 描述目标硬件是 AMD（HIP≈AMD 版 CUDA），与 API 变体无关——独立 `tilelang` 包在 CDNA 上同样用 `"hip"`。
- 卷积被「拍扁」成 GEMM：输出 `(N,OH,OW,F)` 与权重 `(F,KH,KW,C)` 用 `T.Buffer` 视图零拷贝重排成 `(N*OH*OW, F)` 与 `(F, KH*KW*C)`；`data` 因 im2col 非线性，只能加载时逐元素换算。
- im2col 地址公式 `access_h = oh·S + kh·D − P`、`access_w = ow·S + kw·D − P` 一行同时编码 stride、dilation、padding，`in_bound` 掩码用 `T.if_then_else` 把越界访问回退为 0。
- 搜索空间是 `itertools.product` 暴搜 192 个配置，**无 Roller 分支**；`k_pack`、`coalesced_width` 是两个写死的半固定旋钮。
- 驱动链路：`benchmark_tilelang.sh` 逐 shape 调 `python` 脚本产日志，`conv_tlops_extract.py` 用正则抽 `Best TFlops`；注意 shell 默认只解注释一行 shape、与 extract 脚本的 shape 列表不一致，复现以实际启用为准。

## 7. 下一步学习建议

- **基线生态**：本讲的 torch 参考与同目录的 Ladder 基线（`benchmark_ladder_conv.py`，AMD 专用）属于第 7 单元的对比基线生态，建议接着读 u7-l23，把 cuBLAS/Triton/BitBLAS/Ladder/CK 等基线的定位与安装方式系统过一遍。
- **跨架构适配**：综合实践第 4 题引出的「同一内核在 NVIDIA / AMD 间迁移要改什么」，正是 u7-l24 的主题，建议结合本讲的 `target` 差异去读。
- **更复杂的 tvm.tl 内核**：`cdna_benchmark/mha_benchmark/test_tilelang_mha.py` 是另一个 `tvm.tl` 变体例子（FlashAttention），可用来巩固「靠 import 与返回值识别变体」的技能，并与 u5-l16 的独立包版本对照。
- **调度机制深挖**：若想理解 `T.Pipelined`、`T.use_swizzle`、warp 策略在这些 CDNA 内核里的具体表现，可回看 u3-l9 / u3-l11 的块级 GEMM 五要素与调优旋钮。

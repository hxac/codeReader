# int8 与多精度 GEMM

## 1. 本讲目标

本讲承接 [u3-l9（块级 GEMM 内核解剖）](u3-l9-block-gemm-anatomy.md)。在上一讲里，我们已经把 TileLang 块级 GEMM 内核的「五要素」（`T.Kernel` / `alloc_shared` / `alloc_fragment` / `T.copy` / `T.gemm` / `T.Pipelined`）逐行拆开。当时反复出现一个提醒：那个内核**注释写的是 fp16、代码跑的却是 int8**。本讲就来兑现那个提醒——专门讲清楚「精度（dtype）」这一维度，回答三个问题：

- TileLang 内核里 `dtype` / `accum_dtype` / `out_dtype` 这三种类型分别是什么、为什么不能只用一种？
- 为什么要把 fp16/fp32 的 GEMM 换成 int8？int8 走 `dp4a` 还是 Tensor Core，收益从何而来？
- 同样是「矩阵乘」，为什么项目要分 **GEMM**（M 较大）和 **GEMV**（M=1）两套 shape、两个数据文件？

学完本讲，你应当能够：

- 说出 `dtype`（输入精度）、`accum_dtype`（累加精度）、`out_dtype`（输出精度）三者的分工，并能解释**累加精度必须比输入精度更宽**的溢出原因。
- 理解 int8 在 GPU 上走 **INT8 Tensor Core（IMMA）** 而非标量 `dp4a`，能定性说出「位宽减半、吞吐近翻倍」的收益来源。
- 区分 GEMM 与 GEMV：前者**计算密集**（适合 Tensor Core 压榨算力），后者**访存密集**（瓶颈在带宽），并能从 `data_int8_gemm.py` 与 `data_int8_gemv.py` 的 shape 表里读出这一差异。

本讲是第 4 单元（多精度与量化矩阵乘）的开篇，后续 [u4-l13（反量化 GEMV）](u4-l13-dequant-gemv-thread-reduction.md)、[u4-l14（量化与 lop3 快速解码）](u4-l14-quantize-fast-decoding.md) 都建立在本讲的精度概念之上。

## 2. 前置知识

### 2.1 什么是「精度」（dtype）

在 GPU 计算里，**精度（dtype = data type）**指一个数用多少比特、什么格式来表示。常见的几种：

| dtype | 位宽 | 典型用途 | 表示范围（粗略） |
|---|---|---|---|
| `float` / `float32`（fp32） | 32 位 | 「高精度」累加、参考实现 | ±3.4e38，约 7 位有效十进制 |
| `float16` / `half`（fp16） | 16 位 | 训练/推理的主输入 | ±65504，约 3 位有效十进制 |
| `int8` | 8 位有符号整数 | 低精度推理、量化 | -128 ~ 127 |
| `int32` | 32 位有符号整数 | int8 乘加的累加结果 | -2.1e9 ~ 2.1e9 |

关键直觉：**位宽越小，一个数占的存储和带宽越少，硬件单位时间能算的个数越多**——这正是「低精度」追求的性能。代价是表示范围和精度变窄，必须小心处理溢出与累加误差。

### 2.2 为什么用低精度算 GEMM

GEMM 是深度学习里最耗算力的算子。神经网络推理时，权重量化到 int8（甚至 int4）通常对最终精度影响很小，却能让吞吐成倍提升。同一代 GPU 的 Tensor Core 对不同精度的**峰值吞吐（TOPS / TFlops）随位宽近似反比缩放**：粗略地说，int8 的 TOPS 约为 fp16 的 2 倍、fp32 的数倍。因此把一个 GEMM 从 fp16 换成 int8，理论上算力上限直接翻倍——前提是数据本身能被合理地量化到 int8。

### 2.3 int8 运算为什么会「溢出」

int8 只能表示 -128 ~ 127。两个 int8 相乘，单个乘积最大是 \(127 \times 127 = 16129\)，已经超出 int8、甚至接近 int16（±32767）的上限。而 GEMM 在 K 维上要把多达成千上万个这样的乘积**累加**起来：

\[
C[m,n] = \sum_{k=0}^{K-1} A[m,k] \cdot B[n,k]
\]

若 \(K = 16384\)，最坏情况下累加和可达 \(127 \times 127 \times 16384 \approx 2.6 \times 10^{8}\)，远超 int16、但仍在 int32（\(2.1 \times 10^{9}\)）范围内。**所以 int8 GEMM 的输入用 int8，但累加器和输出必须用 int32**——这就是 `accum_dtype` 存在的根本原因。fp16 同理：fp16 的尾数只有 10 位（约 3 位有效数字），直接累加上万次会严重丢精度，所以要累加在 fp32（`float`）里。

> 这两段直觉（输入窄、累加宽）是本讲全部内容的基石，下面的源码都是在落地这个直觉。

## 3. 本讲源码地图

本讲涉及三个主文件，分别覆盖「TileLang int8 内核」「int8 GEMM 数据提取」「cuBLAS 低精度测试床」：

| 文件 | 作用 |
|---|---|
| [`benchmark_tilelang_matmul.py`](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py) | hopper 下 dense matmul 的 TileLang 内核。本讲聚焦 [L186-L195](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L186-L195)：`dtype="int8"`、`accum_dtype="int32"` 以及 A/B/C 三参的 dtype 标注。 |
| [`data_int8_gemm.py`](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/data/data_int8_gemm.py) | int8 **GEMM**（M 较大）的「日志→数据」提取脚本，定义了 M0–M12 共 13 组 shape（[L25-L39](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/data/data_int8_gemm.py#L25-L39)）。 |
| [`cublas_benchmark.cu`](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/ada_benchmark/lowprecision_matmul/0.cublas-benchmark/cublas_benchmark.cu) | ada 下 **lowprecision matmul** 的 cuBLAS C++ 测试床。用模板 `time_gemm<T1,T2>` 一个函数编译期分派 fp16/int8/fp8 多条路径（[L115-L148](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/ada_benchmark/lowprecision_matmul/0.cublas-benchmark/cublas_benchmark.cu#L115-L148)），是 cuBLAS 多精度 GEMM 的标准写法。 |

辅助参考（本讲会引用，但不逐行展开）：

| 文件 | 作用 |
|---|---|
| [`data_int8_gemv.py`](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/data/data_int8_gemv.py) | int8 **GEMV**（M=1）的数据提取脚本，定义 V0–V12 共 13 组 shape（[L25-L39](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/data/data_int8_gemv.py#L25-L39)），与 GEMM 版一一对照。 |
| [`benchmark_tilelang_matmul.sh`](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.sh) | 驱动脚本，dtype 组合里**注释掉**了 fp16、只跑 int8（[L25-L28](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.sh#L25-L28)）。本讲综合实践要用到它。 |
| [`cublas_benchmark.cu`](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu)（hopper dense_matmul） | `data_int8_gemm.py` 实际读取的 cuBLAS 日志由它产生。输出列顺序为 `fp32, fp16, int8, fp16-TC, int8-TC`（[L203-L206](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L203-L206)）。 |

> **以代码为准**（u3-l8/u3-l9 已建立此意识）：`benchmark_tilelang_matmul.py` 的 [L186-L187](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L186-L187) 注释写着「Use half-precision ... accumulate in float」，但紧接的 [L188-L189](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L188-L189) 代码是 `dtype = "int8"`、`accum_dtype = "int32"`。**实际跑的是 int8 路径**，注释是历史遗留。本讲一律以代码为准。

## 4. 核心概念与源码讲解

### 4.1 模块一：dtype / accum_dtype / out_dtype——三种类型各司其职

#### 4.1.1 概念说明

一个低精度 GEMM 内核，其实涉及**三种类型**，不能混为一谈：

- **输入精度 `dtype`（in_dtype）**：矩阵 A、B 在显存里的存储格式，决定 Tensor Core 用哪条指令、决定访存带宽。本内核是 `int8`。
- **累加精度 `accum_dtype`**：在 K 维上把成千上万个乘积累加起来时用的格式。**必须比 `dtype` 宽**，否则溢出或丢精度。本内核是 `int32`。
- **输出精度 `out_dtype`**：最终写回 C 的格式。在很多实现里 `out_dtype` 与 `accum_dtype` 相同（本内核 C 就用 `accum_dtype`），但也可不同——例如 int8 输入、fp32 累加、再反量化回 fp16 输出（这正是 u4-l13 反量化 GEMV 的模式）。

一句话：**「窄输入、宽累加、（可选）指定输出」**是低精度 GEMM 的通用三件套。

#### 4.1.2 核心流程

在 TileLang 内核里，这三类精度通过两处变量落地：

1. 在 `kernel()` 函数体顶部声明两个 dtype 变量（输入与累加）。
2. 在 `@T.prim_func` 的三个张量参数上，分别用它们标注 A、B、C 的类型。

数据流上，A、B 以 `dtype`（int8）进入 → Tensor Core 在 `accum_dtype`（int32）下做乘加、结果落进 `C_local`（int32 fragment）→ 最终以 `accum_dtype`（int32）写回 C。三者关系可画成：

```
A(int8) ─┐
         ├──▶ T.gemm ──▶ C_local(int32 累加器) ──▶ C(int32 输出)
B(int8) ─┘     （IMMA 指令，累加在 int32）
```

#### 4.1.3 源码精读

精度声明在 [benchmark_tilelang_matmul.py:186-189](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L186-L189)：

```python
# Use half-precision for input data to reduce memory bandwidth,
# accumulate in float for better numerical accuracy   ← 注释，与代码不符
dtype = "int8"
accum_dtype = "int32"
```

注意：注释描述的是 fp16 输入 + float 累加，但代码实际是 **int8 输入 + int32 累加**——这是本文件多处「注释 vs 代码」不一致之一，务必以代码为准。

三个张量参数用这两个变量标注类型，见 [L191-L196](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L191-L196)：

```python
@T.prim_func
def main(
        A: T.Tensor((M, K), dtype),            # 输入 A：int8
        B: T.Tensor((N, K), dtype),            # 输入 B：int8
        C: T.Tensor((M, N), accum_dtype),      # 输出 C：int32（= out_dtype）
):
```

说明：

- A、B 用 `dtype`（int8）：低精度输入，省带宽、走 INT8 Tensor Core。
- C 用 `accum_dtype`（int32）：此处输出精度等于累加精度，所以 `out_dtype` 就是 int32。若要输出 fp16，需另引入 `out_dtype` 变量并在 C 上使用（本内核未这么做）。
- 片上分配同样遵循「输入窄、累加宽」：`A_shared`/`B_shared` 是 `dtype`（int8，见 [L213-L215](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L213-L215)），`C_local`/`C_shared` 是 `accum_dtype`（int32，见 [L217-L219](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L217-L219)）。这在 u3-l9 的存储分配表里已列出。

> 还有一处隐藏的「以代码为准」细节：Roller 分支在构造调优模板时用的是 fp16 语义（[L43-L46](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L43-L46)：`in_dtype="float16"`、`accum_dtype="float"`），而真正编译执行的内核却是 int8。也就是说 **Roller 推导 hint 时假设的是 fp16，内核却按 int8 跑**。块大小/流水级等 hint 主要由形状与架构决定，因此仍可用；但严格的 int8 指令形状（如 IMMA 的 `16×16×32`）约束并未被 Roller 在此处精确捕捉。这正是综合实践里「把 dtype 改回 fp16」能让两者重新一致的切入点。

#### 4.1.4 代码实践

**实践目标**：核对三种精度在内核里的落点，建立「窄输入、宽累加」的对应关系。

**操作步骤**：

1. 打开 [benchmark_tilelang_matmul.py:186-196](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L186-L196)。
2. 列一张表：`dtype` 用在哪些变量/参数上？`accum_dtype` 用在哪些上？
3. 回答：本内核的 `out_dtype` 是什么？（提示：没有单独的 `out_dtype` 变量。）

**需要观察的现象**：输入路径（A、B、A_shared、B_shared）全是 int8；累加/输出路径（C_local、C_shared、C）全是 int32。

**预期结果**：`dtype(int8)` → A、B、A_shared、B_shared；`accum_dtype(int32)` → C_local、C_shared、C。`out_dtype` 未单独声明，等于 `accum_dtype` = int32。

#### 4.1.5 小练习与答案

**练习 1**：如果硬把 `accum_dtype` 也设成 `int8`，会发生什么？
**答案**：int8 的 K 维累加几乎必然溢出（见 2.3，最坏情况累加和约 \(2.6\times10^8\)，远超 127）。结果会回绕（wrap-around）成错误值，GEMM 数值全错。所以累加器**必须**用更宽的 int32。

**练习 2**：本内核的 `out_dtype` 与 `accum_dtype` 相同（都是 int32）。什么场景下会希望二者不同？
**答案**：当上游/下游期望的输出格式与累加格式不一致时。例如量化推理里常见的「int8 输入、fp32 累加、fp16 输出」——累加用 fp32 保精度，但最终只需 fp16 送入下一层。这时就要单独引入 `out_dtype="float16"`。本内核没有这种需求，故二者合一。

---

### 4.2 模块二：int8 路径——dp4a 与 INT8 Tensor Core（IMMA）

#### 4.2.1 概念说明

把 GEMM 从 fp16 换成 int8，性能收益来自硬件。NVIDIA GPU 上 int8 的乘加有两条路径：

- **`dp4a`（CUDA Core 标量路径）**：一条 CUDA Core 指令，把**两个 int8×4 的向量**做 4 元素点积、得到一个 int32 结果。即 4 个 \(a_i \cdot b_i\) 求和。这是「标量」单元上的指令，吞吐受 CUDA Core 数量限制。
- **INT8 Tensor Core（IMMA，矩阵路径）**：Tensor Core 上的整数矩阵乘加指令，一条指令完成一个固定尺寸（如 `16×16×32`）的 int8 小矩阵乘加，结果累加到 int32。吞吐远高于 `dp4a`。

在 TileLang 的块级 `T.gemm` 里，输入来自共享内存、输出是 fragment 累加器——这正是 Tensor Core 的数据通路，因此 **`T.gemm` + `dtype="int8"` 会编译成 IMMA（INT8 Tensor Core），而不是 `dp4a`**。`dp4a` 更多出现在不支持 Tensor Core 的旧架构，或在线程级手写点积时（u4-l13 的反量化 GEMV 就会用到线程级点积思路）。

#### 4.2.2 核心流程

收益的数学直觉：Tensor Core 的峰值吞吐（TOPS）随数据位宽近似**反比缩放**。同一代 GPU 上，粗略地有：

\[
\text{TOPS}_{\text{int8}} \;\approx\; 2 \times \text{TFlops}_{\text{fp16}} \;\approx\; \text{数倍} \times \text{TFlops}_{\text{fp32}}
\]

因此，只要数据能合理量化到 int8，单位时间能完成的乘加数近似翻倍。对应的代价是：

- **数值范围收窄**（-128~127），需要量化（scale/zero-point）把 fp16 权重映射进 int8，这部分是 u4-l13/u4-l14 的主题。
- **对齐约束**：int8 Tensor Core（IMMA）通常要求 M 维对齐到某个倍数（cuBLAS 里是 4），否则要 padding。
- **累加精度**：如 4.1 所述，必须累加在 int32。

cuBLAS 侧的对应物：把 compute type 设成 `CUBLAS_COMPUTE_32I`、数据类型设成 `CUDA_R_8I`，cuBLAS 就会走 INT8 Tensor Core（见 4.4）。

#### 4.2.3 源码精读

TileLang 这边，决定走 int8 IMMA 的就是 [L188](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L188) 的 `dtype = "int8"`。有了它，K 循环里的 [L235-L241](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L235-L241)：

```python
T.gemm(
    A_shared,    # int8
    B_shared,    # int8
    C_local,     # int32 累加器
    transpose_B=True,
    policy=policy,
)
```

会被编译成 INT8 Tensor Core（IMMA）指令：`A_shared`/`B_shared`（int8）作输入、`C_local`（int32）作累加器，单条指令完成一个 `16×16×32`（或架构相关的）int8 小矩阵乘加。把 `dtype` 改成 `"float16"`，同一行 `T.gemm` 就会改走 fp16 Tensor Core（HMMA/BF16 MMA）——**内核结构完全不变，变的只是 dtype 与底层指令**。这正是 TileLang「结构与精度解耦」的好处。

cuBLAS 这边，hopper dense_matmul 的 int8 Tensor Core 调用在 [cublas_benchmark.cu:269-301](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L269-L301)：

```cpp
status = cublasSetMathMode(cublas_handle, CUBLAS_TENSOR_OP_MATH);  // L269 开启 Tensor Core
...
// int8 tensor core benchmark
auto a = rand<uint8_t>({...});   // int8 输入
auto b = rand<uint8_t>({...});
auto c = zeros<int>({pad_m, n}); // int32 输出/累加
time_us = time_gemm<uint8_t, int>(a, b, c, a_t, b_t, cublas_handle, true);  // L299
```

注意它的类型搭配与 TileLang 完全一致：**`uint8_t` 输入 × 2、`int`（int32）输出/累加**，且开启 `CUBLAS_TENSOR_OP_MATH`。这印证了「int8 输入 + int32 累加」是跨框架的通用约定。

#### 4.2.4 代码实践

**实践目标**：把 TileLang 内核与 cuBLAS 在 int8 路径上的类型搭配对照起来（源码阅读型）。

**操作步骤**：

1. 阅读 [benchmark_tilelang_matmul.py:235-241](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L235-L241)（TileLang `T.gemm`）与 [cublas_benchmark.cu:295-300](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L295-L300)（cuBLAS int8 调用）。
2. 各自列出「输入 dtype / 累加 dtype / 输出 dtype / 是否走 Tensor Core」。

**需要观察的现象**：两边都是 int8 输入、int32 累加与输出，且都走 Tensor Core。

**预期结果**：

| 维度 | TileLang `T.gemm` | cuBLAS `time_gemm<uint8_t,int>` |
|---|---|---|
| 输入 dtype | int8（`A_shared`/`B_shared`） | `uint8_t`（`CUDA_R_8I`） |
| 累加 dtype | int32（`C_local`） | `int` / `CUBLAS_COMPUTE_32I` |
| 输出 dtype | int32（`C`） | `int` |
| 走 Tensor Core | 是（IMMA） | 是（`CUBLAS_TENSOR_OP_MATH`） |

#### 4.2.5 小练习与答案

**练习 1**：为什么 TileLang 的 `T.gemm` 走 IMMA 而不是 `dp4a`？
**答案**：`T.gemm` 的输入在共享内存、输出在 fragment 累加器，是 Tensor Core 的数据通路，且块级矩阵乘用 IMMA 一条指令算一个 `16×16×32` 小块，吞吐远高于 CUDA Core 上的标量 `dp4a`。`dp4a` 适合无线程级、无 Tensor Core 的点积场景。

**练习 2**：把 `dtype` 从 `int8` 改成 `float16`，`T.gemm` 这一行需要改吗？
**答案**：不需要改 `T.gemm` 本身。它的行为由传入张量的 dtype 决定：A/B/C_local 的 dtype 改了，`T.gemm` 自动走对应的 fp16 Tensor Core 指令。这正是本讲综合实践的要点。

---

### 4.3 模块三：GEMM vs GEMV——同一种算子，两种性能瓶颈

#### 4.3.1 概念说明

矩阵乘 \(C = A \times B^{\top}\)，按 M 的大小分两类：

- **GEMM（Matrix-Matrix）**：M 较大（如 8192、16384），A 是「胖」矩阵，算的是矩阵乘矩阵。**计算量大**，瓶颈在 GPU 的**算力（compute-bound）**，最受益于 Tensor Core。
- **GEMV（Matrix-Vector）**：M=1（或很小），A 退化为一个向量，算的是矩阵乘向量。**计算量小但要把整张 B 从显存搬进来**，瓶颈在**显存带宽（memory-bound）**，Tensor Core 往往喂不饱。

在项目里，这两类 shape 被分别命名（u1-l1 引入过）：**`M` 族**（GEMM，M 大）与 **`V` 族**（GEMV，M=1）。它们共享同一批 N、K，只让 M 变化，从而把「算力极限」与「带宽极限」两种场景都覆盖到。

#### 4.3.2 核心流程

用「算术强度（arithmetic intensity）」刻画二者差异——即每搬一字节能做多少次运算：

\[
\text{计算量} = 2MNK, \qquad \text{访存量} \approx MK + NK + MN
\]

- **GEMM**（\(M\) 大）：算术强度 \(\approx \frac{2MNK}{MK+NK} \to O(K)\)（随 K 增大），数值大，落在算力瓶颈区，Tensor Core 满载。
- **GEMV**（\(M=1\)）：算术强度 \(\approx \frac{2NK}{K+NK} = \frac{2N}{1+N} \approx 2\)，几乎与 K 无关、是个很小的常数。即每读一字节 B 只能做约 2 次运算——典型的带宽瓶颈，再强的 Tensor Core 也使不上劲。

这就是为什么 cuBLAS/TileLang/Triton 在 GEMV 上往往拉不开差距（都被带宽卡住），而在 GEMM 上 Tensor Core 的优势才能充分体现。

项目用两个数据文件分别承载这两套 shape：

| 数据文件 | shape 族 | M 取值 | 性质 |
|---|---|---|---|
| `data_int8_gemm.py` | M0–M12 | 16384 / 8192（大） | GEMM，算力瓶颈 |
| `data_int8_gemv.py` | V0–V12 | 1（恒为 1） | GEMV，带宽瓶颈 |

两者的 N、K 列表完全相同，只有 M 不同——这是刻意设计的对照实验。

#### 4.3.3 源码精读

GEMM 的 shape 表在 [data_int8_gemm.py:25-39](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/data/data_int8_gemm.py#L25-L39)，M 都是 16384 或 8192：

```python
for i, (m, n, k) in enumerate([
        [16384, 16384, 16384],
        [8192, 43008, 14336],
        [8192, 14336, 14336],
        ...
]):
```

GEMV 的 shape 表在 [data_int8_gemv.py:25-39](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/data/data_int8_gemv.py#L25-L39)，M 恒为 1，N、K 与 GEMM 版逐一对应：

```python
for i, (m, n, k) in enumerate([
        [1, 16384, 16384],
        [1, 43008, 14336],
        [1, 14336, 14336],
        ...
]):
```

对比可见：第 0 组 GEMM 是 `16384×16384×16384`，对应 GEMV 是 `1×16384×16384`——**N、K 完全相同，只把 M 从 16384 降到 1**。文件顶部的 provider 标签也印证了族的划分：GEMM 是 `["M0",...,"M12"]`（[L5](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/data/data_int8_gemm.py#L5)），GEMV 是 `["V0",...,"V12"]`（[L5](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/data/data_int8_gemv.py#L5)）。

两个文件的 BitBLAS 日志名都带 4 段 dtype `int8_int8_int32_int32`（[data_int8_gemm.py:104](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/data/data_int8_gemm.py#L104)），即 `A_dtype_W_dtype_out_dtype_accum_dtype`——又一次印证 int8/int8/int32/int32 的搭配。

> **以代码为准**（数据提取的下标细节）：两个文件都从同一份 cuBLAS 日志读 int8 列，但 cuBLAS 的浮点下标不一致——GEMM 版 [data_int8_gemm.py:21](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/data/data_int8_gemm.py#L21) 取 `[-1]`（最后一列 = int8 Tensor Core），GEMV 版 [data_int8_gemv.py:21](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/data/data_int8_gemv.py#L21) 却取 `[-2]`（倒数第二列 = fp16 Tensor Core）。在 hopper dense_matmul 的 cuBLAS 列顺序 `fp32, fp16, int8, fp16-TC, int8-TC`（[cublas_benchmark.cu:203-206](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L203-L206)）下，`[-1]` 才是 int8 TC。因此 GEMV 版取 `[-2]` 很可能是历史遗留的取列偏差（待确认是否 intentional）。复现时务必以实际日志每列含义为准。

#### 4.3.4 代码实践

**实践目标**：用算术强度区分 GEMM 与 GEMV 的瓶颈归属。

**操作步骤**：

1. 取第 0 组 shape：GEMM `M=N=K=16384`，GEMV `M=1, N=K=16384`。
2. 分别算两者的计算量 \(2MNK\) 与访存量 \(MK+NK+MN\)（粗算，字节量按 int8 输入、int32 输出）。
3. 算出算术强度（运算次数 / 字节），判断谁算力瓶颈、谁带宽瓶颈。

**需要观察的现象**：GEMM 的算术强度大（上万），GEMV 的算术强度小（个位数）。

**预期结果**：

- GEMM（\(M=16384\)）：计算量 \(2 \times 16384^3 \approx 8.8 \times 10^{12}\) FLOP；访存量 \(\approx 2 \times 16384^2 \times 1\text{B} \approx 5.4 \times 10^{8}\) 字节（输入 int8）；算术强度 \(\approx 1.6 \times 10^{4}\) FLOP/字节 → 算力瓶颈。
- GEMV（\(M=1\)）：计算量 \(2 \times 16384^2 \approx 5.4 \times 10^{8}\) FLOP；访存量主要是 B 矩阵 \(\approx 16384^2 \times 1\text{B} \approx 2.7 \times 10^{8}\) 字节；算术强度 \(\approx 2\) FLOP/字节 → 带宽瓶颈。

（以上为数量级估算，**待本地验证**精确数值。）

#### 4.3.5 小练习与答案

**练习 1**：为什么 GEMV 上各框架（cuBLAS/Triton/TileLang）的性能差距通常比 GEMM 小？
**答案**：GEMV 是带宽瓶颈，谁都得把整张 B 从显存搬进来，搬运时间由显存带宽这一硬件常数决定，框架间的调度差异被掩盖。GEMM 是算力瓶颈，Tensor Core 利用率、流水深度、栅格化等调度差异直接体现在吞吐上，差距才被拉开。

**练习 2**：`data_int8_gemm.py` 和 `data_int8_gemv.py` 为什么只让 M 变化、固定 N 和 K？
**答案**：为了**控制变量**。固定 N、K，只把 M 从大变到 1，就构造出「同一个算子、同一组权重大小、从算力瓶颈平滑滑向带宽瓶颈」的对照实验，便于观察 Tensor Core 收益随 M 的变化曲线。这也是 README 里 M 族与 V 族并存的用意。

---

### 4.4 模块四：lowprecision——cuBLAS 低精度测试床与多精度分派

#### 4.4.1 概念说明

ada 下的 `lowprecision_matmul/0.cublas-benchmark` 是一个**专门对比低精度**的 cuBLAS 测试床：它在同一个程序里、用同一组 shape，依次跑 fp16、int8、fp8（e5m2/e4m3）四条精度路径，把它们的延迟并排打印成 CSV。它的核心技巧是**C++ 模板 + 类型分派**：写一个模板函数 `time_gemm<T1, T2>`，让 `T1`（输入类型）和 `T2`（输出类型）在编译期决定走哪条 cuBLAS 路径——这与 TileLang「改 dtype 即换指令」的思路异曲同工，只是 cuBLAS 用 C++ 模板实现分派。

#### 4.4.2 核心流程

`time_gemm<T1, T2>` 内部根据 `T1` 的类型，设置 cuBLAS 的数据类型与 compute type：

| 模板实参 `T1` | 数据类型（A/B/C） | compute type | 对应路径 |
|---|---|---|---|
| `uint16_t` / `half` | `CUDA_R_16F` | `CUBLAS_COMPUTE_16F` | fp16 Tensor Core |
| `uint8_t`（纯 int8） | `CUDA_R_8I` / `CUDA_R_32I` | `CUBLAS_COMPUTE_32I` | **int8 Tensor Core（IMMA）** |
| `uint8_t` + `is_fp_e5m2` | `CUDA_R_8F_E5M2/E4M3` | `CUBLAS_COMPUTE_32F` | fp8 e5m2 |
| `uint8_t` + `is_fp_e4m3` | `CUDA_R_8F_E4M3` | `CUBLAS_COMPUTE_32F` | fp8 e4m3 |

关键：int8 路径的 compute type 是 `CUBLAS_COMPUTE_32I`——即「**用 int32 做累加**」，这正是 TileLang `accum_dtype="int32"` 在 cuBLAS 侧的对应物。二者的「int8 输入 + int32 累加」约定完全一致。

此外，int8 Tensor Core 对 M 维有对齐要求（IMMA 通常要 M 是 4 的倍数），所以测试床在调 int8 前会 `pad_dim(pad_m, 4)` 把 M 补齐到 4 的倍数。

#### 4.4.3 源码精读

模板与类型分派在 [cublas_benchmark.cu:115-148](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/ada_benchmark/lowprecision_matmul/0.cublas-benchmark/cublas_benchmark.cu#L115-L148)：

```cpp
if (std::is_same<T1, uint16_t>::value || std::is_same<T1, half>::value) {
    A_type = CUDA_R_16F; ... compute_type = CUDA_R_16F;
    gemm_compute_type = CUBLAS_COMPUTE_16F;            // fp16 路径
}
else if (std::is_same<T1, uint8_t>::value) {
    A_type = CUDA_R_8I;  B_type = CUDA_R_8I;
    C_type = CUDA_R_32I; compute_type = CUDA_R_32I;
    gemm_compute_type = CUBLAS_COMPUTE_32I;            // int8 路径：int32 累加
    ...                                                 // fp8 子分支（is_fp_e5m2/e4m3）
}
```

说明：

- 这是**编译期分派**（`std::is_same` 在模板实例化时求值），没有运行时开销。一个函数模板实例化出 fp16、int8、fp8 等多个版本。
- int8 分支里 `C_type = CUDA_R_32I`、`gemm_compute_type = CUBLAS_COMPUTE_32I`，即「int8 输入、int32 输出与累加」——与 TileLang 的 int8/int8/int32/int32 一一对应。

int8 的调用点（带 M 对齐 padding）在 [cublas_benchmark.cu:329-345](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/ada_benchmark/lowprecision_matmul/0.cublas-benchmark/cublas_benchmark.cu#L329-L345)：

```cpp
// int8 tensor core benchmark
int pad_m = m;
if (pad_m % 4) { pad_dim(pad_m, 4); }                  // M 补齐到 4 的倍数
auto a = rand<uint8_t>({...});                          // int8 输入
auto b = rand<uint8_t>({...});
auto c = zeros<int>({pad_m, n});                        // int32 输出
time_us = time_gemm<uint8_t, int>(b, a, c, b_t, a_t, cublaslt_handle, true);  // 走 TC
```

说明：

- 模板实参 `<uint8_t, int>`：`T1=uint8_t`（int8 输入）、`T2=int`（int32 输出），正是上表第二行。
- 最后一个参数 `true` 表示 `use_tensor_core=true`，强制走 Tensor Core（IMMA）。
- `pad_dim(pad_m, 4)`：int8 Tensor Core 要求 M 对齐到 4，不足则补零。这也是为什么 cuBLAS 的 int8 shape 集合（[L35-L53](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/ada_benchmark/lowprecision_matmul/0.cublas-benchmark/cublas_benchmark.cu#L35-L53) 的 `inference_server_set`）里 M 多取 4096/8192 这种 4 的倍数。

把 fp16 段（[L319-L327](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/ada_benchmark/lowprecision_matmul/0.cublas-benchmark/cublas_benchmark.cu#L319-L327)）与 int8 段并排看，就能直观体会「改 dtype 即换路径」：两段代码结构几乎相同，差别只在模板实参（`<uint16_t,uint16_t>` vs `<uint8_t,int>`）和是否 padding。

#### 4.4.4 代码实践

**实践目标**：理解 cuBLAS 用模板做「一个函数、多精度分派」的写法，并对照 TileLang。

**操作步骤**：

1. 阅读 [cublas_benchmark.cu:115-148](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/ada_benchmark/lowprecision_matmul/0.cublas-benchmark/cublas_benchmark.cu#L115-L148) 的类型分派。
2. 回答：要新增一条 bf16 路径，需要在哪几个地方改动？（只描述思路）
3. 对照 TileLang：TileLang 切 int8/fp16 只改一个 `dtype` 变量，cuBLAS 为何要这么繁琐？

**需要观察的现象**：cuBLAS 的精度切换散落在多处（类型实参、`is_*` 模板标志、compute type 赋值、padding），而 TileLang 集中在一个 `dtype` 字符串。

**预期结果**：新增 bf16 需在 [L115-L148](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/ada_benchmark/lowprecision_matmul/0.cublas-benchmark/cublas_benchmark.cu#L115-L148) 加一个 `else if (is bf16)` 分支设 `CUDA_R_16BF`/`CUBLAS_COMPUTE_32F`，再在 `main` 里加一段调用 `time_gemm<...>`。cuBLAS 繁琐是因为它是底层库、必须显式告诉它每种类型对应的硬件路径；TileLang 把这些映射藏进 `T.gemm` 的 dtype 推断里，故而简洁——这是 DSL 的价值。

#### 4.4.5 小练习与答案

**练习 1**：为什么 int8 段要 `pad_dim(pad_m, 4)`，而 fp16 段不需要？
**答案**：INT8 Tensor Core（IMMA）指令对 M 维有对齐要求（通常是 8 或 16 的相关倍数，这里补到 4 的倍数以满足约束），不对齐会触发 padding 或非法访问。fp16 Tensor Core 的指令形状（如 `16×16×16`）对 M 的约束不同，且 cuBLAS 内部会处理对齐，因此这段代码对 fp16 不显式 pad。

**练习 2**：cuBLAS int8 路径的 `CUBLAS_COMPUTE_32I` 与 TileLang 的 `accum_dtype="int32"` 表达的是同一件事吗？
**答案**：是。两者都指「乘加在 int32 里累加」。`CUBLAS_COMPUTE_32I` 是 cuBLAS 的 compute type 枚举，`accum_dtype` 是 TileLang 的累加类型字符串，语义一致——都保证了 4.1 讲的「窄输入、宽累加」。

---

## 5. 综合实践

本讲综合实践是规格指定的核心任务：**把 dense matmul 的 TileLang 内核从 int8 改读为 fp16 的等价设置（只描述改动点，不真的改源码），并解释 `accum_dtype` 为什么必须保持高精度**。它把本讲四个模块（dtype 三件套、int8 路径、cuBLAS 对照、多精度切换）串到一起。

### 5.1 任务

通读 [benchmark_tilelang_matmul.py:186-196](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L186-L196) 与 [benchmark_tilelang_matmul.sh:25-28](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.sh#L25-L28)，完成：

1. 列出把内核从 int8 切到 fp16 需要改的**所有**点位（内核内 + 驱动脚本）。
2. 解释为什么切到 fp16 后 `accum_dtype` 仍要用 `float`（fp32）而不是 fp16。

### 5.2 参考答案

**第一问：改动点位**

切到 fp16 只动「类型」、不动「结构」，共三处：

1. **内核的 dtype 变量**（[benchmark_tilelang_matmul.py:188-189](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L188-L189)）：

   ```python
   # 原（int8）            →    改（fp16）
   dtype = "int8"          →    dtype = "float16"
   accum_dtype = "int32"   →    accum_dtype = "float"
   ```

   改完后，A/B 变 fp16、C_local/C_shared/C 变 fp32，`T.gemm`（[L235-L241](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L235-L241)）自动走 fp16 Tensor Core。**内核主体（网格、copy、Pipelined）一字不改**——这就是「结构与精度解耦」。

2. **Roller 模板重归一致**（可选，但推荐）：[L43-L46](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L43-L46) 的 `MatmulTemplate` 本来就写的是 `in_dtype="float16"`、`accum_dtype="float"`——也就是说**改成 fp16 后，Roller 推导与内核执行终于自洽**（int8 时二者不一致，见 4.1.3 的提醒）。这一处无需改，改了内核它就「对了」。

3. **驱动脚本的 dtype 组合**（[benchmark_tilelang_matmul.sh:25-28](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.sh#L25-L28)）：

   ```bash
   dtypes=(
       # "float16 float16 float16 float32"   ← 取消注释这行
       "int8 int8 int32 int32"               ← 注释掉这行
   )
   ```

   注意脚本里**早就预留了 fp16 那一行**（被注释），四段含义是 `A_dtype W_dtype out_dtype accum_dtype` = `float16 float16 float16 float32`。这与第 1 点的改动完全吻合。

> 补充观察：`@jit` 的 `supply_type=tl.TensorSupplyType.Integer`（[L149](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L149)）是为 int8 准备的（让 profiler 生成整数测试输入）。切到 fp16 后它仍能用（Integer 输入在 fp16 范围内合法），但若追求更贴近真实的 fp16 分布，可改为 `Normal`/`Uniform`。这不是必须改动。

**第二问：为什么 `accum_dtype` 仍要保持高精度（fp32）**

即便输入降到 fp16，累加器也必须用 fp32（`float`），原因有二：

1. **精度损失**：fp16 的尾数只有 10 位（约 3 位有效十进制），而 K 维要把成千上万个 fp16 乘积相加。若直接在 fp16 里累加，每次相加的舍入误差会被反复放大，最终累加结果的有效数字会严重流失（大数吃小数）。fp32 的 23 位尾数提供了足够的「累加裕度」，把舍入误差压到可忽略。

2. **范围不足**：fp16 的最大值仅 65504。即便每个乘积不大，\(K=16384\) 个乘积相加也很容易超过 65504，导致 inf 溢出。fp32 的范围（±3.4e38）则绰绰有余。

数学上，把累加器从 fp16 换成 fp32，等价于把求和 \(\sum_{k=0}^{K-1} A[m,k]B[n,k]\) 放到更高精度的数轴上执行，避免中途的舍入与溢出——这就是「**混合精度（mixed precision）**」的标准做法：**低精度算、高精度累**。对 int8 而言，这个「高精度累」是 int32（防整数溢出）；对 fp16 而言，是 fp32（防浮点舍入与溢出）。形式不同，原理一致。

### 5.3 进阶（可选）

把本讲的多精度切换与 cuBLAS 测试床（4.4）对应：TileLang 改一个 `dtype` 字符串，cuBLAS 要改模板实参与 compute type。试着在 [cublas_benchmark.cu:115-148](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/ada_benchmark/lowprecision_matmul/0.cublas-benchmark/cublas_benchmark.cu#L115-L148) 里找出「fp16 → int8」对应的那几行类型赋值，体会 DSL 与底层库在抽象层级上的差距。

## 6. 本讲小结

- 低精度 GEMM 用**三种类型**：输入 `dtype`（本内核 int8）、累加 `accum_dtype`（int32）、输出 `out_dtype`（本内核 = accum_dtype = int32）；「窄输入、宽累加」是通用约定。
- `accum_dtype` 必须比 `dtype` 宽：int8 乘积在 K 维累加会溢出 int16（需 int32），fp16 累加会丢精度/溢出（需 fp32）——这就是混合精度「低精度算、高精度累」的本质。
- int8 在 GPU 上走 **INT8 Tensor Core（IMMA）**，而非标量 `dp4a`：TileLang 的 `T.gemm` + `dtype="int8"`、cuBLAS 的 `CUDA_R_8I` + `CUBLAS_COMPUTE_32I` 都路由到 IMMA；峰值吞吐随位宽近似反比缩放，是 int8 的收益来源。
- **GEMM（M 大，算力瓶颈）vs GEMV（M=1，带宽瓶颈）**：项目用 M 族/V 族两套 shape、`data_int8_gemm.py`/`data_int8_gemv.py` 两个文件控制变量；GEMV 上各框架差距小，因为都被带宽卡住。
- cuBLAS 的 `lowprecision` 测试床用 C++ 模板 `time_gemm<T1,T2>` 做编译期多精度分派（fp16/int8/fp8），并 `pad_dim` 满足 int8 Tensor Core 的 M 对齐——与 TileLang「改 dtype 即换路径」对照，体现 DSL 的简洁。
- 本文件多处「注释 vs 代码」不一致：注释说 fp16/float，代码跑 int8/int32；Roller 模板用 fp16 而内核跑 int8。**始终以代码为准**。

## 7. 下一步学习建议

- 本讲的 int8 是「**对称、无反量化**」的纯整数 GEMM。真实量化推理里，权重常以 int4 打包存储、需要现场反量化——下一步推荐 [u4-l13（反量化 GEMV 内核：线程级外积规约）](u4-l13-dequant-gemv-thread-reduction.md)：看 TileLang 如何用线程级 `alloc_local` + `tvm_thread_allreduce` 做反量化 GEMV，与本章的块级 Tensor Core GEMM 形成对照。
- 想了解 int4 的比特打包与 `lop3` 快速解码，接着读 [u4-l14（量化与 lop3 快速解码）](u4-l14-quantize-fast-decoding.md)。
- 想纵览 int4/fp4 与 BitBLAS/Marlin/CUTLASS/bitsandbytes 等量化基线的定位，读 [u4-l15（fp4/int4 反量化 matmul 与量化基线生态）](u4-l15-fp4-int4-and-quant-baselines.md)。
- 也可回顾 [u2-l5（cuBLAS 参考基准）](u2-l5-cublas-reference-harness.md)，把本讲的 int8 路径放回 cuBLAS 测试床的整体计时框架里理解。

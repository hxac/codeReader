# INT8 量化推理：BertINT8 与 SmoothQuant

> 本讲属于「量化与高性能 GEMM」单元（u9）的第一篇。前置讲义：[u2-l3 矩阵乘骨干](u2-l3-cublas-gemm.md)（cuBLAS/cuBLASLt、GEMM、leading dimension）、[u4-l1 BERT 模型与 forward 主流程](u4-l1-bert-model.md)（一个 transformer block 的 8 GEMM + 6 kernel）。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 **INT8 推理为什么快**：从 Tensor Core 的 INT8 吞吐与显存带宽两个角度建立直觉。
- 区分 FasterTransformer（FT）BERT 的 **`int8_mode` 分级**：mode 1（per-channel 权重量化 + int32 累加 + 残差不量化）与 mode 2/3（per-tensor 量化 + int8 全链路 IO + 残差量化，即 w8a8），并理解 SmoothQuant 在 w8a8 中的「激活平滑」思路。
- 看懂 **`ScaleList`** 这张缩放因子账本如何把「权重 amax / 激活 amax / deQFactor / output scale」组织成一段连续显存。
- 读懂 **`cublasINT8MMWrapper`** 如何用 `CUBLASLT_ORDER_COL32` 等特殊布局喂给 INT8 Tensor Core。
- 读懂 **量化/反量化 kernel** 与 **融合 `add_bias_residual_layernorm` kernel** 如何在「低精度存储 + 高精度计算」之间架桥。
- 跟踪 **`BertLayerINT8::forward`** 的两种 mode 分支，理解 mode 1 与 mode 2/3 在「输入/输出/GEMM 累加类型」上的差异。

## 2. 前置知识

在进入源码前，先用通俗语言建立三个直觉。

### 2.1 为什么要 INT8

Transformer 推理的算力瓶颈是 **GEMM（矩阵乘）**。NVIDIA GPU 的 Tensor Core 在 INT8 下的吞吐通常是 FP16 的 2 倍、FP32 的数倍（具体倍率随架构：Turing/Ampere 的 INT8 Tensor Core 是 FP16 的 2 倍）。此外 INT8 元素只占 1 字节，**显存带宽与权重显存占用都降到 FP16 的一半**。当推理是访存密集（小 batch、大权重）或算力密集（大 batch）时，INT8 都能换到可观加速。

代价是 **精度损失**：INT8 只能用 `[-127, 127]` 共 255 个离散值表示原本连续的浮点数。量化就是把浮点张量压进这 255 个格子的过程。

### 2.2 对称量化与缩放因子

FT 的 INT8 采用 **对称量化**（symmetric quantization）：对一段浮点张量，先统计其绝对值最大值 `amax`，再算缩放因子，把每个浮点值乘以缩放因子后四舍五入到 `[-127,127]`：

\[
\text{scale} = \frac{127.0}{\text{amax}},\qquad
x_{\text{int8}} = \mathrm{round}(x_{\text{fp}}\cdot \text{scale})
\]

反量化（dequantize）则是乘回 `amax/127.0`。`amax` 就是贯穿本讲的关键量。一个 INT8 GEMM 的正确性，取决于「输入 A 的 amax、权重 B 的 amax、输出 C 的 amax」三者都被准确记录并在反量化时乘回。

> 注意：本讲看到代码里出现 `0.000062f` 这类「魔法常数」时，它就是 \(\frac{1}{127\times127}\) 的近似，用来在反量化时一次性消去两次 `amax/127`。

### 2.3 per-tensor vs per-channel 与 SmoothQuant

**per-tensor（逐张量）**：整张权重只用一个 amax，简单、与 cuBLASLt 的 INT8 Tensor Core 接口契合（额外还能用 `int8` 作为输出），但精度损失大。

**per-channel（逐通道）**：权重的每个输出通道各用一个 amax，精度高（每个通道都能用满 255 格），但 cuBLASLt 难以直接产出 int8 输出，只能产出 **int32** 累加结果，再由自定义 kernel 反量化。

**SmoothQuant（w8a8 的校准思路）**：当「权重+激活」都要量化（weight-8-bit, activation-8-bit，即 **w8a8**）时，激活往往含少量极大离群点，导致 per-tensor 的 amax 被拉大、大部分正常值被挤进很小范围、精度崩坏。SmoothQuant 的做法是 **把激活的离群点「迁移」到权重上**：给激活乘一个介于 0~1 的平滑因子 \(s\)，给权重除以同一个 \(s\)，数学上 GEMM 结果不变，但激活分布变平、权重分布略尖，两者都更容易被 INT8 准确表达。本讲关注 FT 推理侧的 w8a8 路径（mode 2/3），SmoothQuant 的离线校准细节属量化工具范畴。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/fastertransformer/models/bert_int8/BertINT8.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert_int8/BertINT8.h) / [BertINT8.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert_int8/BertINT8.cc) | INT8 版 BERT 的编排者：校验 `int8_mode`、管理层循环与去 padding/恢复 padding。 |
| [src/fastertransformer/models/bert_int8/BertLayerINT8.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert_int8/BertLayerINT8.cc) | 单个 transformer 层的 INT8 前向，按 `int8_mode` 走两条不同分支。 |
| [src/fastertransformer/models/bert_int8/BertLayerINT8Weight.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert_int8/BertLayerINT8Weight.h) | 每层权重，额外携带一个 `ScaleList`。 |
| [src/fastertransformer/utils/ScaleList.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/ScaleList.h) | 把一层的所有 amax/deQFactor 打包成一段连续显存的结构体。 |
| [src/fastertransformer/utils/cublasINT8MMWrapper.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasINT8MMWrapper.cc) | 封装 cuBLASLt 的 INT8 IGEMM（COL32 布局、algoMap 查表）。 |
| [src/fastertransformer/kernels/int8_utils.cuh](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/int8_utils.cuh) | 设备函数 `float_to_int8_rn` / `float4_to_char4`（PTX 饱和取整）。 |
| [src/fastertransformer/kernels/quantization_int8_kernels.cu](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/quantization_int8_kernels.cu) | `invokeQuantization`：把 FP 张量按 scale 压成 INT8。 |
| [src/fastertransformer/kernels/layernorm_int8_kernels.cu](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/layernorm_int8_kernels.cu) | 融合 kernel：`add_bias + residual + layernorm + 量化/反量化` 一体。 |
| [docs/bert_guide.md](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/bert_guide.md) | 官方 BERT 文档，含 INT8 两套流程说明与运行命令。 |

---

## 4. 核心概念与源码讲解

本讲按 5 个最小模块展开：先讲量化直觉与 `int8_mode` 分级，再讲缩放因子账本 `ScaleList`，接着是 INT8 GEMM 封装、量化/反量化与融合 kernel，最后落到一层的完整前向编排。

### 4.1 int8_mode 分级：两条计算路径

#### 4.1.1 概念说明

FT 的 BERT INT8 用一个整数 `int8_mode` 选择量化「深度」。根据官方文档与源码，关键差异如下表（与 [docs/bert_guide.md:109-120](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/bert_guide.md#L109-L120) 的描述一致）：

| 特性 | `int8_mode == 1`（int8v1） | `int8_mode == 2`（int8v2） |
| --- | --- | --- |
| 量化残差连接 | **否** | 是 |
| INT8 GEMM 的输出类型 | **int32** | **int8** |
| 权重量化粒度 | **per-channel**（每输出通道一个 amax） | per-tensor（整张一个 amax） |
| 精度 | 更高 | 略低 |
| 性能 | 较低 | 更高 |

一句话总结：**mode 1 用更高精度的 per-channel + int32 累加换取精度，mode 2 用 per-tensor + int8 全链路换取性能。** `int8_mode == 3` 在源码中与 mode 2 走同一条「int8 全链路 IO」分支（见 [BertLayerINT8.cc:272](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert_int8/BertLayerINT8.cc#L272)），并在量化工具里同样使用 per-tensor（见 [examples/.../ckpt_quantization.py:270-271](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/tensorflow/bert/tensorflow_bert/ckpt_quantization.py#L270-L271)），可视为 w8a8（含 SmoothQuant 风格校准）的变体路径；其与 mode 2 的精确差异由离线校准工具决定，本讲标注为「待确认」。

> ⚠️ 注意 BERT 与 GPT 的 `int8_mode` 语义不同：GPT 里 mode 1 表示 **weight-only PTQ**（权重 INT8、激活仍 FP16/BF16）。本讲只讲 BERT 语义。weight-only 量化的 CUTLASS 实现见下一讲 [u9-l2 权重仅量化与 CUTLASS 混合 GEMM](u9-l2-weight-only-cutlass-gemm.md)。

#### 4.1.2 核心流程

```text
BertINT8 构造:  校验 int8_mode ∈ {1,2,3}
                校验 sparse 与 mode 1 互斥
                创建 BertLayerINT8
                       │
                       ▼
每层 BertLayerINT8::forward
   ┌─ mode 1 分支 ─┐    ┌─ mode 2/3 分支 ─┐
   │ 输入 T → 量化  │    │ layer0: T→量化   │
   │ GEMM 输出 int32│    │ GEMM 输出 int8    │
   │ 残差保留 T     │    │ 残差也是 int8     │
   │ per-channel amax│   │ per-tensor scale  │
   └────────────────┘    └───────────────────┘
```

两个分支的根本差别是 **数据在层与层之间流动的类型**：mode 1 在层间传递的是 `T`（FP16/FP32），每层入口都要重新量化；mode 2/3 在层间传递 int8，省掉了重复量化，但代价是残差也被压成 int8、精度下降。

#### 4.1.3 源码精读

`int8_mode` 的合法性校验在 `BertINT8` 构造函数里：

[BertINT8.cc:54-65](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert_int8/BertINT8.cc#L54-L65) — 校验 `int8_mode` 必须是 1/2/3；sparsity 只能与 mode 2/3 搭配（mode 1 的 per-channel 与稀疏结构不兼容）；mode 1 在 unfused MHA 下要求 `max_seq_len` 是 32 的倍数。

```cpp
if (int8_mode_ != 1 && int8_mode_ != 2 && int8_mode_ != 3) {
    throw std::runtime_error(std::string("[FT][ERROR] int8_mode_ not support \n"));
}
if (sparse_ && int8_mode_ == 1) {
    throw std::runtime_error(std::string("[FT][ERROR] int8_mode 1 does not support sparsity \n"));
}
```

而 `BertLayerINT8::forward` 顶部的注释精准概括了两条路径的输入约定：

[BertLayerINT8.cc:162-165](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert_int8/BertLayerINT8.cc#L162-L165) — `layer_idx == 0` 时输入是 `T` 需量化、行主序需转 COL32；`layer_idx != 0` 时 mode 2/3 输入已是 int8、mode 1 输入仍是 `T` 需量化。

#### 4.1.4 代码实践

1. **实践目标**：用静态分析确认「mode 1 与 mode 2/3 在 buffer 类型上的差异」。
2. **操作步骤**：打开 [BertLayerINT8.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert_int8/BertLayerINT8.cc)，定位 `allocateBuffer()`（[L128-L148](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert_int8/BertLayerINT8.cc#L128-L148)），观察 `attn_out_buf_` 被声明为 `int32_t*`。
3. **需要观察的现象**：在 mode 1 分支（[L204-L271](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert_int8/BertLayerINT8.cc#L204-L271)）里，attention/FFN 的输出都写进这个 `int32_t*` 缓冲；而在 mode 2/3 分支（[L272-L419](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert_int8/BertLayerINT8.cc#L272-L419)）里，同样的缓冲被强转为 `int8_t*` 使用。
4. **预期结果**：你会看到 mode 1 用 `int32` 累加 + 后续 kernel 反量化；mode 2/3 全程 `int8` IO。这正是两套路径精度/性能权衡的根源。
5. 运行结果待本地验证（本实践为源码阅读型，无需 GPU）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `sparse_ && int8_mode_ == 1` 会报错？
**答案**：sparsity（结构化稀疏，2:4）要求权重按特定 8/16 对齐的块存储，而 mode 1 的 per-channel 量化给每个输出通道独立 amax，两者的权重布局假设冲突，故 FT 不支持组合。

**练习 2**：mode 1 在 unfused MHA 下为何要求 `max_seq_len % 32 == 0`？
**答案**：INT8 Tensor Core 与 COL32 布局按 32 元素对齐，unfused 路径里注意力分数矩阵的序列维必须能被 32 整除才能正确喂给 INT8 kernel（参考 [BertINT8.cc:60-65](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert_int8/BertINT8.cc#L60-L65)）。这也是文档建议 INT8 配合 Effective FasterTransformer（去 padding）的原因。

---

### 4.2 ScaleList：缩放因子的统一账本

#### 4.2.1 概念说明

INT8 推理要在「量化、GEMM、反量化」之间反复搬运 amax。如果每个 kernel 都各自去查 amax，既啰嗦又难以对齐。FT 的做法是把 **一整层所需的全部缩放因子** 打包成一段连续显存 `ScaleList`，所有 INT8 kernel 都从同一个 `d_scale_list_` 指针按固定下标取值。它是一层的「量化参数账本」。

#### 4.2.2 核心流程

`ScaleList` 由 5 段拼接（命名见 [ScaleList.h:27-49](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/ScaleList.h#L27-L49)）：

| 段 | 长度 | 内容 |
| --- | --- | --- |
| Part 1 | 72 | 激活 amax（每组 4 值：amax, amax/127, amax/127², 127/amax），覆盖 Q/K/V/bmm1/bmm2/proj/FC1/FC2 等 |
| Part 2 | 9×hidden_dim | 权重 per-channel amax（Q/K/V/proj/FC1/FC2 等，每段 hidden 个通道） |
| Part 3 | 8 | INT8 GEMM 的 deQFactor（Q/K/V/bmm1/bmm2/FC0/FC1/FC2） |
| Part 4 | 3 | 融合 MHA kernel 专用 amax（QKVbias / softmax / bmm2） |
| Part 5 | 21 | 预留 |

每组 amax 存 4 个派生值，是为了让不同 kernel 按需取「正向 scale」或「反向 deQFactor」而无需现场除法。

#### 4.2.3 源码精读

[ScaleList.h:27-49](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/ScaleList.h#L27-L49) — 结构体本体只有两个指针 `d_scale_list_`（device）、`h_scale_list_`（host）和几个偏移量常量：

```cpp
struct ScaleList {
    // Part 1 -- 72: 激活 amax ...（每组 4 值）
    // Part 2 -- 9*hidden_dim: 权重 per-channel amax
    // Part 3 -- 8: INT8 GEMM deQFactor
    // Part 4 -- 3: 融合 MHA amax
    // Part 5 -- 21: 预留
    const float* d_scale_list_ = nullptr;
    const float* h_scale_list_ = nullptr;
    size_t       size_         = ACTIVATION_AMAX_NUM + 9 * 768 + INT8O_GEMM_NUM + TRT_AMAX_NUM;
    size_t       p2_offset_    = ACTIVATION_AMAX_NUM;          // Part 2 起点
    size_t       p3_offset_    = ACTIVATION_AMAX_NUM + 9 * 768; // Part 3 起点
    ...
};
```

注意 `size_` 里硬编码了 `9 * 768`——这是 BERT-base 的 hidden_dim 假设。`ScaleList` 由 `BertLayerINT8Weight` 持有（[BertLayerINT8Weight.h:39](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert_int8/BertLayerINT8Weight.h#L39)），在权重加载时由量化工具生成的 scale 数据填充，运行期只读。

#### 4.2.4 代码实践

1. **实践目标**：把 `BertLayerINT8::forward` 里对 `scale_list->d_scale_list_[下标]` 的访问与 `ScaleList` 的分段对上号。
2. **操作步骤**：在 mode 1 分支里找到 [L229-L230](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert_int8/BertLayerINT8.cc#L229-L230) 的调用：`&scale_list->d_scale_list_[scale_list->p2_offset_ + 3 * hidden_units_]` 与 `&scale_list->d_scale_list_[36]`。
3. **需要观察的现象**：`p2_offset_ + 3*hidden_units_` 落在 Part 2（权重 per-channel amax）里，对应注意力输出投影权重；`[36]` 落在 Part 1 的 `bmm2_amax` 区。
4. **预期结果**：你能说出每个下标取的是「哪类缩放因子」——这正是综合实践题的雏形。
5. 运行结果待本地验证（源码阅读型）。

#### 4.2.5 小练习与答案

**练习 1**：`ScaleList` 里每个 amax 为什么存 4 个派生值？
**答案**：不同 kernel 需要不同的表达：`amax/127` 是正向量化 scale，`127/amax` 是反量化 deQFactor，`amax/127²` 用于一次消去两次量化的 GEMM 输出（即 `0.000062` 那类常数）。预先算好省去 kernel 内除法。

**练习 2**：`ScaleList` 的 `size_` 写死 `9 * 768`，这对非 BERT-base 的模型会有什么问题？
**答案**：hidden_dim 不是 768 时 Part 2 长度算错，下标会错位。实际工程中需要按真实 hidden_dim 重新生成 scale 文件并相应调整（这是 FT 早期 INT8 耦合 BERT-base 假设的局限）。

---

### 4.3 cublasINT8MMWrapper：COL32 布局与 INT8 IGEMM

#### 4.3.1 概念说明

INT8 Tensor Core 不能接受任意的行/列主序矩阵。cuBLASLt 要求 INT8 GEMM（IGEMM）使用特殊布局：激活用 **`CUBLASLT_ORDER_COL32`**（每 32 行为一组、组内列主序），权重用 **`CUBLASLT_ORDER_COL4_4R2_8C`** 或更新的 **`CUBLASLT_ORDER_COL32_2R_4R4`**（Ampere）。此外，IGEMM 的「计算类型」是 `CUBLAS_COMPUTE_32I`（int32 累加），输出可以是 int32 或 int8。`cublasINT8MMWrapper` 就是把这些约束收拢、并提供两个重载 `Gemm`：一个产出 int32、一个产出 int8。

#### 4.3.2 核心流程

```text
Gemm(int32 输出):  ATransform[m,k] COL32  +  kernel[n,k] COL4_4R2_8C
                   → res[m,n] COL32,  compute=INT32,  alpha=1 beta=0
Gemm(int8 输出):   同样布局,  compute=INT32,  但 scaleType=FP32
                   → res[m,n] COL32 INT8,  alpha 是 FP32 缩放(融合反量化)
```

注意 int8 输出版本的 `alpha` 是一个 **FP32 标量**——cuBLASLt 会在累加成 int32 后乘以 `alpha` 再饱和到 int8，相当于把「反量化 + 再量化」融进 GEMM，省一次显存往返。这正是 mode 2/3 比 mode 1 快的原因之一。

#### 4.3.3 源码精读

**布局与 leading dimension**（int32 输出版本）：

[cublasINT8MMWrapper.cc:120-128](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasINT8MMWrapper.cc#L120-L128) — COL32 布局下，leading dimension 不是普通的「列数」而是 `32 * m`；权重侧按是否启用 `COL32_2R_4R4` 走不同对齐：

```cpp
int ldaTransform = 32 * m;                 // 激活：每 32 行打包，ld = 32*m
int ldbTransform;
if (use_ORDER_COL32_2R_4R4_) {
    ldbTransform = 32 * ((n + 32 - 1) / 32) * 32;   // Ampere 布局
} else {
    ldbTransform = 32 * ((n + 8 - 1) / 8) * 8;       // Turing 布局
}
int ldcTransform = 32 * m;                 // 输出也是 COL32
```

**Layout 描述与计算类型**：

[cublasINT8MMWrapper.cc:131-143](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasINT8MMWrapper.cc#L131-L143) — 创建 matmulDesc（`CUBLAS_COMPUTE_32I`，CUDA ≥11 用三元 create），设置 `TRANSB`（权重转置），三个 layout 都显式指定 ORDER。A/B 输入 `CUDA_R_8I`，输出 `CUDA_R_32I`。

**algoMap 查表**（承接 [u2-l4](u2-l4-gemm-autotuning.md)）：

[cublasINT8MMWrapper.cc:165-228](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasINT8MMWrapper.cc#L165-L228) — 先用 `cublas_algo_map_->isExist(batchCount, m, n, k, INT8_DATATYPE)` 查离线调优算法，命中则按 `cublasLtMatmulAlgo_info` 配置 tile/splitK/swizzle/stages；未命中则用默认 algo（`COL32_2R_4R4` 选 algoId=7、stages=15，否则 algoId=6、stages=13）。

**int8 输出版本的差异**：

[cublasINT8MMWrapper.cc:275-327](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasINT8MMWrapper.cc#L275-L327) — `scaleType = CUDA_R_32F`、输出 layout 是 `CUDA_R_8I`，`alpha` 是 FP32。调用方（mode 2/3 的层）会把 ScaleList 里的 deQFactor×output_scale 算好作为 `alpha` 传入，实现「GEMM 内融合反量化」。

#### 4.3.4 代码实践

1. **实践目标**：理解为何 INT8 GEMM 必须转 COL32。
2. **操作步骤**：在 [BertLayerINT8.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert_int8/BertLayerINT8.cc) 搜索 `invokeTransposeMatrixColMajorToCOL32`（如 [L207](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert_int8/BertLayerINT8.cc#L207)）。
3. **需要观察的现象**：`layer_idx == 0` 时，输入是行主序的 `T`，必须先转成 COL32 才能喂 IGEMM；而 `layer_idx != 0`（mode 2/3）时输入已是 COL32 的 int8，可直接用。
4. **预期结果**：你会确认「布局转换」是 INT8 路径在首层和末层各发生一次的开销（末层用 `invokeTransposeMatrixCOL32ToColMajor` 转回行主序供下游）。
5. 运行结果待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 int8 输出版 IGEMM 的 `alpha` 是 FP32 而非 int32？
**答案**：累加在 int32 寄存器完成，但要在写回 int8 前「反量化到 FP 再量化回 int8」，这一步乘以一个 FP32 缩放（`alpha`）由 cuBLASLt 在 GEMM 内部完成，避免额外 kernel 与显存读写。

**练习 2**：`ldaTransform = 32 * m` 中，`m` 是 token 数（变长）。这对 batch 变化意味着什么？
**答案**：COL32 的 leading dimension 依赖 `m`，所以 batch/序列长度变化会改变布局参数，需要重新查 algoMap（这也是为何 INT8 要为不同 batch 离线调优 `igemm_config.in`）。

---

### 4.4 量化/反量化与融合 LayerNorm kernel

#### 4.4.1 概念说明

INT8 路径里有两类「桥接 kernel」：一是把 FP 压成 INT8 的 `invokeQuantization`，二是把「加偏置 + 残差 + LayerNorm + 量化/反量化」融成一体的 `invokeAddBiasResidualLayerNormCol32`。后者是 INT8 推理省访存的关键——朴素实现要把 int32 GEMM 输出反量化成 FP、加残差、算 LN、再量化回 int8，四次显存往返；融合后全部在寄存器/共享内存里完成，只读写一次。

#### 4.4.2 核心流程

```text
invokeQuantization(dst_int8, src_FP, size, scale):
    对每 4 元素: dst = round(src * scale)   (char4 向量化)

add_bias_input_layernorm_COL32_int32I_DataTypeO (mode 1 用):
    input1 = GEMM 的 int32 输出 (COL32)
    反量化:  tmp = int32(input1) * weight_amax * input1_amax * (1/127²)
    加残差+偏置:  out = tmp + input2 + bias
    blockReduce 求 mean/variance → LayerNorm → 写回 DataType O (FP)

add_bias_input_layernorm_COL32_int8IO (mode 2/3 用):
    input1/input2 都是 int8 (COL32)
    用 deQFactor 反量化 → 加偏置残差 → LayerNorm → 再用 output_scale 量化回 int8
```

关键：mode 1 的反量化用「per-channel weight_amax × per-tensor input1_amax」，这正是 per-channel 量化的体现；mode 2/3 用两个标量 deQFactor，对应 per-tensor。

#### 4.4.3 源码精读

**饱和取整设备函数**（贯穿所有量化 kernel）：

[int8_utils.cuh:22-27](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/int8_utils.cuh#L22-L27) — `float_to_int8_rn` 用一条 PTX `cvt.rni.sat.s8.f32` 完成「四舍五入 + 饱和到 [-128,127]」：

```cpp
static inline __device__ int8_t float_to_int8_rn(float x) {
    uint32_t dst;
    asm volatile("cvt.rni.sat.s8.f32 %0, %1;" : "=r"(dst) : "f"(x));
    return reinterpret_cast<const int8_t&>(dst);
}
```

[int8_utils.cuh:29-51](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/int8_utils.cuh#L29-L51) — `float4_to_char4` 在 sm≥720（Turing+）上用 `cvt.pack.sat.s8.s32.b32` 把 4 个 float 打包成一个 `char4`，是融合 kernel 末尾量化输出的利器。

**量化 kernel**：

[quantization_int8_kernels.cu:22-35](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/quantization_int8_kernels.cu#L22-L35) — `quantized_kernel` 以 `char4`/`float4` 向量化，每个线程处理 4 元素，`scale` 取自 ScaleList。`invokeQuantization`（[L56-L71](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/quantization_int8_kernels.cu#L56-L71)）按 FP32/FP16 分发。

**融合 LayerNorm（int32 输入，mode 1 用）**：

[layernorm_int8_kernels.cu:25-71](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/layernorm_int8_kernels.cu#L25-L71) — 注意 [L47-L48](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/layernorm_int8_kernels.cu#L47-L48) 的反量化：

```cpp
float tmp = static_cast<float>(__ldg(input1 + outIdx)) * __ldg(weight_amax + col_start) * input1_amax
            * 0.000062f;  //(1/127/127);
```

`weight_amax[col_start]` 是 per-channel（按列取），`input1_amax` 是标量，`0.000062f ≈ 1/127²` 一次消去两次量化。之后用 `blockReduceSum` 求 mean/variance（承接 [u3-l1](u3-l1-core-kernels.md) 的归约套路）。

**融合 LayerNorm（int8 IO，mode 2/3 用）**：

[layernorm_int8_kernels.cu:207-293](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/layernorm_int8_kernels.cu#L207-L293) — 输入输出都是 int8（`char4` 向量化），用 `input1_deQFactor`/`input2_deQFactor` 反量化、`output_scale` 量化回 int8（[L275](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/layernorm_int8_kernels.cu#L275)）。host 入口 `invokeAddBiasResidualLayerNormCol32` 见 [L385-L428](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/layernorm_int8_kernels.cu#L385-L428)，按 `sizeof(T)` 选 half2/float 模板。

> 注意命名重载：`invokeAddBiasResidualLayerNormCol32` 有两个签名——一个吃 `int32_t* input1`（mode 1）、一个吃 `int8_t* input1`（mode 2/3），编译器按实参类型选择，调用方不必关心。

#### 4.4.4 代码实践

1. **实践目标**：验证融合 kernel 确实「一次读写」完成四件事。
2. **操作步骤**：读 [layernorm_int8_kernels.cu:230-292](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/layernorm_int8_kernels.cu#L230-L292)。
3. **需要观察的现象**：kernel 开头读 `input1Tmp`/`input2Tmp`（两次 char4 读），中间全部在 `local_out[4]` 寄存器数组里做反量化→加偏置→归约 mean/var→LN，末尾才用 `float_to_int8_rn` 写回 `outTmpPtr[outIdx]`。
4. **预期结果**：确认全程只有「2 次读 + 1 次写」全局显存，相比朴素实现的 4 次以上往返显著省带宽。
5. 运行结果待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`0.000062f` 这个常数从哪来？为什么是 `1/127²`？
**答案**：int32 GEMM 输出 = (int8 输入 × 127/amax_in) × (int8 权重 × 127/amax_w) × …… 反量化时要乘回 `amax_in/127 × amax_w/127`。当 weight_amax 已是「amax/127」、input1_amax 也是「amax/127」时，还需再除一次 127²，即乘 `1/127²≈0.000062`。

**练习 2**：融合 LN kernel 里 `grid=m, block=n`（或 `n/4`），一个 block 处理什么？
**答案**：一个 block 处理一个 token（一行），block 内所有线程协作对该行的 hidden 维做 `blockReduceSum` 求 mean/variance。这与 [u3-l1 layernorm](u3-l1-core-kernels.md) 的「一行一 block」模型一致。

---

### 4.5 BertLayerINT8::forward：一层的完整量化前向

#### 4.5.1 概念说明

前 4 个模块是零件，本模块把它们装成一层。`BertLayerINT8::forward` 是 INT8 路径的「总指挥」：取 `ScaleList`、按 `int8_mode` 选分支、在 attention 子层与 FFN 子层后各跑一次融合 `AddBiasResidualLayerNorm`，并处理首层（量化+转 COL32）与末层（转回行主序）的特殊情况。它复用 u4-l1 的 BERT 结构（LN→attn→残差→LN→FFN→残差），只是把每个算子换成 INT8 版本。

#### 4.5.2 核心流程（mode 2/3，即 w8a8，最常用）

```text
forward(input=T 当 layer0, 否则 int8 COL32):
  scale_list = weight.scale_list_
  if layer_idx == 0:
     T → invokeTransposeMatrixColMajorToCOL32Quantize → int8 COL32
  attention_layer_->forward(int8 输入, attn_out=int8/int32)
  # post layernorm 1: 残差=int8, bias, γ/β, deQFactor, output_scale → int8
  invokeAddBiasResidualLayerNormCol32(int8 IO)
  ffn_layer_->forward(int8) → attn_out(int8)
  # post layernorm 2:
  if 非末层:  invokeAddBiasResidualLayerNormCol32 → int8  (传给下一层)
  if 末层:    先输出 T, 再 invokeTransposeMatrixCOL32ToColMajor 转回行主序
```

mode 1 的区别：attention/FFN 输出写进 `int32_t* attn_out_buf_`，融合 LN 是 `int32I→DataTypeO`（残差保留 `T`），每层入口都要把 `T` 重新 `invokeQuantization`。

#### 4.5.3 源码精读

**取 ScaleList 与基本参数**：

[BertLayerINT8.cc:172-198](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert_int8/BertLayerINT8.cc#L172-L198) — 从权重取出 `scale_list`，读 `m`(token 数)、`layer_idx`、`num_layer`。注意 attention 输出张量用 `getTensorType<int>()` 即 int32 缓冲（mode 1）。

**mode 1 分支**（per-channel + int32）：

[BertLayerINT8.cc:204-271](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert_int8/BertLayerINT8.cc#L204-L271) — 首层 [L206-L210](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert_int8/BertLayerINT8.cc#L206-L210) 先转 COL32 再 `invokeQuantization`；attention 后 [L220-L231](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert_int8/BertLayerINT8.cc#L220-L231) 调 int32 输入版的融合 LN（传 per-channel `weight_amax`）；FFN 后 [L237-L270](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert_int8/BertLayerINT8.cc#L237-L270) 同理，末层多一步转回行主序。

**mode 2/3 分支**（per-tensor + int8 IO）：

[BertLayerINT8.cc:272-419](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert_int8/BertLayerINT8.cc#L272-L419) — 首层 [L274-L292](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert_int8/BertLayerINT8.cc#L274-L292) 用 `invokeTransposeMatrixColMajorToCOL32Quantize`（转置+量化一步融合，sparse 时改走 `invokeQuantization`，见 `#ifdef SPARSITY_ENABLED`）；attention 后 [L300-L335](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert_int8/BertLayerINT8.cc#L300-L335) 调 int8 IO 版融合 LN（传 `d_scale_list_[40+1]`/`[0+1]`/`[44+3]` 三个 deQFactor/scale）；FFN 后 [L341-L418](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert_int8/BertLayerINT8.cc#L341-L418) 同理，末层输出 `T` 再转回行主序。注意 sparse 分支用 `invokeAddBiasResidualLayerNormRow`（行主序），非 sparse 用 Col32 版。

**BertINT8 的层循环与去 padding**：

[BertINT8.cc:262-285](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert_int8/BertINT8.cc#L262-L285) — 与 u4-l1/u4-l2 同构：先按 `attention_type_` 做 `invokeRemovePadding`（INT8 因要求 seq%32==0 尤其受益于去 padding，见 [docs/bert_guide.md:43](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/bert_guide.md#L43)），再用两块 buffer 流式覆写跑 N 层，最后 `invokeRebuildPadding` 恢复。

#### 4.5.4 代码实践

1. **实践目标**：对照官方命令亲手跑一遍 INT8 推理，体会 mode 1 与 mode 2 的速度差。
2. **操作步骤**：按 [docs/bert_guide.md:310-324](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/bert_guide.md#L310-L324) 的指引，在 Turing+（如 T4）上：
   ```bash
   ./bin/bert_gemm 32 32 12 64 1 1     # 生成 INT8 的 igemm 配置
   ./bin/bert_int8_example 32 12 32 12 64 1 0 1   # mode 1
   ./bin/bert_gemm 32 32 12 64 1 2
   ./bin/bert_int8_example 32 12 32 12 64 1 0 2   # mode 2
   ```
3. **需要观察的现象**：对比 mode 1 与 mode 2 的 `FT-CPP-time`；文档示例中 mode 1 ≈ 7.49ms、mode 2 ≈ 4.79ms（T4, batch32, seq32, 12 层）。
4. **预期结果**：mode 2（int8 IO + 融合反量化）明显快于 mode 1（int32 + per-channel），印证本讲「mode 2 性能更好、mode 1 精度更好」的结论。
5. 运行结果待本地验证（需 T4/A100 等 Turing+ GPU 与编译产物 `bert_int8_example`）。

#### 4.5.5 小练习与答案

**练习 1**：为什么首层和末层都要做布局转换，而中间层不需要？
**答案**：首层输入来自外部（行主序 `T`），要转成 IGEMM 要求的 COL32 int8；末层输出要交回 FP 主流程（行主序 `T`），故从 COL32 转回。中间层全程在 COL32 int8 域流动，无需转换。

**练习 2**：mode 2/3 分支里 attention 与 FFN 后的融合 LN 分别传了 3 个 scale 指针，它们大致各是什么角色？
**答案**：以 attention 后的 `invokeAddBiasResidualLayerNormCol32(int8 IO)` 为例（[L320-L331](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert_int8/BertLayerINT8.cc#L320-L331)）：`input1_deQFactor`（GEMM 输出反量化）、`input2_deQFactor`（残差反量化）、`output_scale`（LN 后再量化回 int8），三者都取自 ScaleList。

---

## 5. 综合实践

把 5 个模块串起来，完成下面这个「读源码 + 跑推理」的综合任务。

### 任务：为 INT8 mode 1 与 mode 2 各画一张「数据类型与 scale 流转图」

1. **绘制 mode 1 图**：以一个 transformer 层为框，标注：
   - 输入类型（首层 `T` / 中间层 `T`）、attention GEMM 输出（**int32**）、融合 LN 的输入（int32I）/输出（`T`）、FFN GEMM 输出（int32）、残差类型（**`T`，未量化**）、末层输出（行主序 `T`）。
   - 在每个箭头上标注它从 `ScaleList` 取哪类因子（per-channel `weight_amax` 还是 per-tensor `input1_amax`）。

2. **绘制 mode 2/3 图**：同样一层，但标注：
   - 输入类型（首层量化为 int8 COL32 / 中间层 int8 COL32）、GEMM 输出（**int8**，融合了 FP32 alpha 反量化）、融合 LN 的 int8 IO、残差类型（**int8，已量化**）、末层先出 `T` 再转行主序。
   - 标注融合 LN 用到的 3 个标量 scale（两个 deQFactor + 一个 output_scale）。

3. **跑实验验证差异**：按 [4.5.4](#454-代码实践) 的命令分别跑 mode 1 与 mode 2，记录两者的延迟与显存占用，填入下表（待本地验证）：

   | 指标 | mode 1 | mode 2 |
   | --- | --- | --- |
   | FT-CPP-time (ms) |  |  |
   | 显存占用 (GB) |  |  |

4. **回答关键问题**（即本讲的 practice_task）：
   - **weight-only INT8 与 w8a8（SmoothQuant 风格）在精度收益和适用 batch 上的差异**？
     - 参考答案：weight-only（GPT 的 mode 1 路线）只压权重、激活仍 FP16，精度高、且在小 batch（访存密集、权重读取是瓶颈）时收益最大；w8a8（BERT 的 mode 2/3 路线，可配 SmoothQuant 校准）把激活也压成 int8，能吃满 INT8 Tensor Core 的算力，在大 batch（算力密集）时收益最大，但精度损失更明显、需要 SmoothQuant 这类激活平滑来保精度。
   - **`ScaleList` 中保存的是哪类缩放因子**？
     - 参考答案：它保存一层的全部量化参数账本——激活 amax（Part 1，含 amax/amax÷127/amax÷127²/127÷amax 四种派生）、权重 per-channel amax（Part 2）、INT8 GEMM 的 deQFactor（Part 3）、融合 MHA 专用 amax（Part 4）。它是推理期只读的「反量化/再量化查表」。

## 6. 本讲小结

- FT BERT INT8 用 `int8_mode` 分级：**mode 1 = per-channel 权重 + int32 GEMM 输出 + 残差不量化（精度优先）**；**mode 2/3 = per-tensor + int8 全链路 IO + 残差量化，即 w8a8（性能优先）**，其中 mode 3 在源码与量化工具中与 mode 2 同走 per-tensor/int8-IO 路径，可视为含 SmoothQuant 风格校准的 w8a8 变体（与 mode 2 的精确差异「待确认」）。
- `ScaleList` 把一层所有 amax/deQFactor/output_scale 打包成连续显存，是 INT8 kernel 共享的「缩放因子账本」。
- `cublasINT8MMWrapper` 强制 COL32/COL4_4R2_8C 布局、`CUBLAS_COMPUTE_32I` 累加；int8 输出版用 FP32 `alpha` 在 GEMM 内融合反量化，是 mode 2/3 更快的关键。
- 设备函数 `float_to_int8_rn`/`float4_to_char4` 用 PTX 饱和取整；`invokeQuantization` 负责 FP→int8；融合 `invokeAddBiasResidualLayerNormCol32` 把「反量化+加偏置+残差+LN+再量化」压成一次显存读写。
- `BertLayerINT8::forward` 是总指挥：按 mode 选分支，attention/FFN 后各跑一次融合 LN，首层量化+转 COL32、末层转回行主序；`BertINT8` 在外层做去 padding（INT8 因 seq%32 约束尤其受益）与层循环。
- INT8 推理的哲学仍是 FT 一贯的「低精度存储 + 高精度计算」：存储与 GEMM 走 int8，归约与累加走 FP32/int32，靠 scale 在两侧架桥。

## 7. 下一步学习建议

- **下一讲 [u9-l2 权重仅量化与 CUTLASS 混合 GEMM](u9-l2-weight-only-cutlass-gemm.md)**：本讲讲的是 cuBLASLt 的「权重+激活都 int8」路径；u9-l2 讲 GPT 的 **weight-only** 路线（fpA_intB），权重 int8/int4、激活仍 FP16，靠 CUTLASS 混合精度 GEMM 实现，适合小 batch 大权重场景——与本讲形成完整对照。
- **[u9-l3 FP8 推理](u9-l3-fp8-inference.md)**：把本讲 INT8 的思路推到 FP8（Hopper），看 `cublasFP8MMWrapper` 与 FP8 kernel 如何延续「低精度存储+高精度计算」。
- **继续阅读源码**：想深入可读 [FusedAttentionLayerINT8](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers_int8/) 与 [GeluFfnLayerINT8](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/) 看 attention/FFN 子层如何在内部串接 IGEMM 与量化 kernel；以及 `examples/tensorflow/bert/bert-quantization` 看离线校准（PTQ/QAT/SmoothQuant）如何生成 ScaleList。

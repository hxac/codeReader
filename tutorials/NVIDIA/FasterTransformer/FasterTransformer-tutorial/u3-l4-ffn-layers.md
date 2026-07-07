# FFN 层与张量并行 FFN 变体

## 1. 本讲目标

Transformer 的每个 block 除了注意力子层，还有另一个核心子层——**前馈网络（Feed-Forward Network，FFN）**。本讲聚焦 FasterTransformer（以下简称 FT）里 FFN 的实现，学完后你应当能够：

- 说出 FFN 的「两段 GEMM + 激活」结构，并能在 `FfnLayer.cc` 里精确定位这两段矩阵乘。
- 看懂 FT 用「虚函数 + 子类」让同一份 `FfnLayer` 代码同时支持 Gelu / Relu / Silu（以及 GeGLU / ReGLU / SiGLU 门控变体）的多态设计。
- 解释张量并行 FFN 为什么采用「第一段 GEMM 列切分、第二段 GEMM 行切分、末尾一次 all-reduce」，并能用数学证明它与单卡计算完全等价。
- 读懂 `TensorParallelGeluFfnLayer` 等三个变体如何复用基类 `forward`、只在末尾插一次 `ftNcclAllReduceSum`。

本讲是 u3-l3（注意力层）的姊妹篇：注意力层和 FFN 层共同构成一个 transformer block，二者都遵循「单卡层 + TensorParallel 子类 + 一次 all-reduce」的同款套路。

## 2. 前置知识

### 2.1 什么是 FFN

标准 Transformer 的 FFN 是一个两层的多层感知机（MLP）。设输入向量为 \( x \in \mathbb{R}^{d} \)（\(d\) 是 hidden_units），则：

\[
\text{FFN}(x) = W_2 \cdot \sigma(W_1 \cdot x + b_1) + b_2
\]

其中：

- \( W_1 \in \mathbb{R}^{d_{\text{inter}} \times d} \)：第一段（intermediate）权重，把维度从 \(d\) 升到 \(d_{\text{inter}}\)（通常 \(d_{\text{inter}} = 4d\)）。
- \( \sigma \)：逐元素（elementwise）激活函数，常见有 Gelu、Relu、Silu。
- \( W_2 \in \mathbb{R}^{d \times d_{\text{inter}}} \)：第二段（output）权重，把维度降回 \(d\)。
- \( b_1, b_2 \)：偏置。

> **关键词「逐元素」** 是本讲后半段张量并行能成立的数学前提，请先记住它。

### 2.2 cuBLAS GEMM 的列主序约定

FT 的矩阵乘走 cuBLAS（见 u2-l3）。cuBLAS 是**列主序（column-major）**的，`cublasGemm` 的参数顺序是 `Gemm(opA, opB, m, n, k, A, lda, B, ldb, C, ldc)`，计算 \( C_{m \times n} = \text{op}(A)_{m \times k} \cdot \text{op}(B)_{k \times n} \)。

因为 FT 里的张量在概念上是**行主序**（一行是一个 token），所以源码里你会看到一种「错位」写法：把行主序的 \([ \text{token}, \text{feature} ]\) 当成列主序的 \([ \text{feature}, \text{token} ]\) 来传，于是 cuBLAS 参数里的 `m` 其实是 feature 维、`n` 其实是 token 维。这一点在精读 GEMM 时会反复出现，后面会给一个具体例子。

### 2.3 张量并行（Tensor Parallel）的两类切分

承接 u3-l3 的结论，FT 把一层权重的切分归纳为两种：

| 切分方式 | 切哪个维度 | 输出是否完整 | 是否需要通信 |
|---|---|---|---|
| **列并行（Column-parallel）** | 沿输出的 feature 维切 | 每卡只有一部分 feature | 不需要 |
| **行并行（Row-parallel）** | 沿输入的 feature 维切 | 每卡只是部分和 | 需要 all-reduce 求和 |

FFN 的巧妙之处在于：第一段 GEMM 用列并行、第二段 GEMM 用行并行，中间用一次激活衔接，**整层只需要在末尾做一次 all-reduce**。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/fastertransformer/layers/FfnWeight.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnWeight.h) | 定义 `FfnWeight`，把 FFN 用到的几组权重（intermediate / output / 门控 / ia3）打包。 |
| [src/fastertransformer/layers/FfnLayer.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnLayer.h) | 声明基类 `FfnLayer` 和三个激活子类 `GeluFfnLayer` / `ReluFfnLayer` / `SiluFfnLayer`。 |
| [src/fastertransformer/layers/FfnLayer.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnLayer.cc) | 基类 `forward` 的完整实现：两段 GEMM + 激活，以及 MoE / INT8 / 稀疏等多条旁路。 |
| [src/fastertransformer/layers/TensorParallelGeluFfnLayer.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/TensorParallelGeluFfnLayer.h) / [.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/TensorParallelGeluFfnLayer.cc) | 张量并行 Gelu 变体：继承 `GeluFfnLayer`，构造时把 inter_size 除以 world_size，末尾插 all-reduce。 |
| [src/fastertransformer/layers/TensorParallelSiluFfnLayer.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/TensorParallelSiluFfnLayer.cc) / [TensorParallelReluFfnLayer.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/TensorParallelReluFfnLayer.cc) | Silu / Relu 的张量并行变体，结构与 Gelu 版几乎逐行相同。 |
| [src/fastertransformer/utils/activation_types.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/activation_types.h) | `ActivationType` 枚举与字符串解析。 |

一条贯穿全讲的继承链（自底向上）：

```
BaseLayer
  └─ FfnLayer<T>            # 通用 forward，getActivationType() 默认返回 InvalidType
       ├─ GeluFfnLayer<T>   # override getActivationType() → Gelu
       │    └─ TensorParallelGeluFfnLayer<T>   # + tensor_para + all-reduce
       ├─ ReluFfnLayer<T>   # override getActivationType() → Relu
       │    └─ TensorParallelReluFfnLayer<T>
       └─ SiluFfnLayer<T>   # override getActivationType() → Silu
            └─ TensorParallelSiluFfnLayer<T>
```

这张图就是本讲的「骨架」：**激活的差异靠虚函数多态解决，并行的差异靠子类包一层 `forward` 解决**。

---

## 4. 核心概念与源码讲解

### 4.1 FFN 的结构：两段 GEMM + 激活

#### 4.1.1 概念说明

FFN 在数学上就是「升维 → 激活 → 降维」三步。由于激活是逐元素的，它夹在两段矩阵乘之间，因此一个 FFN 层在 GPU 上落地为：

1. **GEMM 1（升维）**：`[token, hidden] → [token, inter]`
2. **加偏置 + 激活**：逐元素 `σ(· + b1)`
3. **GEMM 2（降维）**：`[token, inter] → [token, hidden]`，再加 `b2`

注意 FT 的 FFN 把**第二段的偏置 `b2` 与残差相加融合到了下游的 `add_residual` kernel 里**（见 u3-l1），所以你在 `FfnLayer.cc` 的 GEMM 2 之后看不到显式加 `b2` 的步骤——这是 FT 一贯的「融合」风格。

此外，FT 的 FFN 还支持三条旁路（本讲作为了解，不展开）：

- **门控激活（Gated Activation）**：GeGLU/ReGLU/SiGLU，形式为 \( \sigma(W_1 x) * (W_{\text{gate}} x) \)，多一组 `intermediate_weight2`。
- **MoE（混合专家）**：`use_moe=true` 时改走 `CutlassMoeFCRunner`，一次算多个专家。
- **INT8 / 稀疏（Sparsity）**：`int8_mode` 和 `sparse_` 控制改走 CUTLASS 混合精度 GEMM 或 cuBLAS 稀疏 GEMM。

本讲的主线只看 `int8_mode==0`、`sparse==false`、`use_moe==false` 的标准 FP16/FP32 路径。

#### 4.1.2 核心流程

```text
输入 ffn_input [token_num, hidden_units]
        │
        ▼
┌─────────────────────────────────────┐
│ GEMM 1: inter_buf = W1 · x          │  ← 列并行的切分点（输出维 = inter_size）
│   m=inter_size, n=token_num, k=hidden │
└─────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────┐
│ genericActivation                    │  ← 加 b1 + σ（Gelu/Relu/Silu）
│   对 inter_buf 逐元素                │
└─────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────┐
│ GEMM 2: output = W2 · inter_buf      │  ← 行并行的切分点（输入维 = inter_size）
│   m=hidden, n=token_num, k=inter_size│     （b2 的加法被融合到下游残差 kernel）
└─────────────────────────────────────┘
        │
        ▼
输出 ffn_output [token_num, hidden_units]
```

#### 4.1.3 源码精读

**入口与张量约定**。`FfnLayer::forward(TensorMap*, TensorMap*, const FfnWeight<T>*)` 的输入输出张量名字与形状在注释里写得很清楚，是阅读该函数的「地图」：

- 输入 `ffn_input`：`[token_num, hidden_units]`
- 输出 `ffn_output`：`[token_num, hidden_units]`

参见 [src/fastertransformer/layers/FfnLayer.cc:36-48](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnLayer.cc#L36-L48)，注释同时列出了 MoE 与 ia3 的可选输入输出。

**GEMM 1（升维）**。在标准路径下（非 MoE、非稀疏、`int8_mode==0`），第一段矩阵乘是这几行——它把输入从 `hidden_units` 维升到 `inter_size` 维：

[src/fastertransformer/layers/FfnLayer.cc:265-275](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnLayer.cc#L265-L275) — 调用 `cublas_wrapper_->Gemm`，参数为 `(OP_N, OP_N, inter_size_, m, hidden_units_, intermediate_weight.kernel, inter_size_, input_tensor, hidden_units_, inter_buf_, inter_size_)`。

按 2.2 节的列主序约定解读：cuBLAS 的 `m=inter_size_`、`n=token_num(m)`、`k=hidden_units_`，输出 `inter_buf_` 在列主序下是 `[inter_size_, token_num]`，也就是行主序的 `[token_num, inter_size_]`。**输出的 feature 维是 `inter_size_`**，这正是后面张量并行要切分的维度。

**激活**。两段 GEMM 之间，调用 `genericActivation(...)` 完成加偏置与激活（门控时还会乘以第二路）。注意外层有一个条件：只有「不是 weight-only INT8 融合路径」时才单独跑激活，否则 CUTLASS 会把 GEMM+bias+act 融成一步：

[src/fastertransformer/layers/FfnLayer.cc:294-309](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnLayer.cc#L294-L309) — `PUSH_RANGE("add bias act")` 后调用 `genericActivation`。

**GEMM 2（降维）**。第二段把 `inter_size` 维降回 `hidden_units` 维：

[src/fastertransformer/layers/FfnLayer.cc:360-370](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnLayer.cc#L360-L370) — `Gemm(OP_N, OP_N, hidden_units_, m, inter_size_, output_weight.kernel, hidden_units_, inter_buf_, inter_size_, output_tensor, hidden_units_)`。

这里 cuBLAS 的 `k=inter_size_`，即**规约（reduction）维度是 `inter_size_`**。张量并行下，每张卡只有 `inter_size_ / world_size` 的规约段，于是每张卡算出的是「部分和」——这正是行并行。

**临时显存**。两段 GEMM 之间的中间结果 `inter_buf_`（`[token_num, inter_size_]`）由 `allocateBuffer` 在 `IAllocator` 上申请、`freeBuffer` 释放，复用规则见 u2-l2 的 `reMalloc`：

[src/fastertransformer/layers/FfnLayer.cc:480-485](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnLayer.cc#L480-L485) — 用 `max_inter_size_` 申请 `inter_buf_`，门控激活时多申请一块 `inter_buf_2_`。

#### 4.1.4 代码实践

**实践目标**：把「两段 GEMM + 激活」的抽象结构与源码里的具体调用一一对应起来。

**操作步骤**：

1. 打开 [src/fastertransformer/layers/FfnLayer.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnLayer.cc)，定位到 `forward(TensorMap*, ...)`（第 34 行起）。
2. 忽略 `use_moe`（第 92-170 行）这一大段 MoE 旁路，直接跳到第 172 行的 `PUSH_RANGE("FFN gemm 1")`。
3. 在标准路径（非稀疏、`int8_mode==0`）下，跟踪三步：
   - GEMM 1（第 265-275 行）
   - `genericActivation`（第 299 行）
   - GEMM 2（第 360-370 行）
4. 画一张表，记录每段 GEMM 的 `(m, n, k)` 与对应的 feature 维含义。

**需要观察的现象 / 预期结果**：你会得到如下映射（cuBLAS 列主序视角）：

| 阶段 | cuBLAS `(m,n,k)` | 行主序下输出形状 | feature 维 |
|---|---|---|---|
| GEMM 1 | `(inter_size, token_num, hidden_units)` | `[token_num, inter_size]` | inter_size（升维） |
| GEMM 2 | `(hidden_units, token_num, inter_size)` | `[token_num, hidden_units]` | hidden_units（降维） |

注意 GEMM 2 的 `k=inter_size` 是规约维，这正是下一节「行并行 = 部分和」的根源。

#### 4.1.5 小练习与答案

**练习 1**：FT 的 FFN 在 GEMM 2 之后并没有显式「加 `b2`」的步骤，`b2` 去哪了？

> **答案**：`b2` 的加法被融合到了下游的 `add_residual` kernel（见 u3-l1 的 `add_bias_residual_kernels`）。FT 的设计哲学是尽可能把「加偏置 + 残差 + 反量化」融进单个 elementwise kernel，减少显存往返。

**练习 2**：为什么 `inter_buf_` 用 `max_inter_size_` 而不是运行期的 `inter_size_` 来申请？

> **答案**：见 [FfnLayer.h:74-79](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnLayer.h#L74-L79) 的注释：同一个 `FfnLayer` 对象可能被多个不同 `inter_size` 的 FFN 复用，按最大值申请一次、之后用 `reMalloc` 的 REUSE 语义复用，避免反复 `cudaMalloc`。

---

### 4.2 激活函数多态：Gelu / Relu / Silu 子类

#### 4.2.1 概念说明

同一个 FFN 结构，GPT 用 Gelu，部分模型用 Relu，LLaMA 系用 Silu。FT 没有为每种激活复制三份 `forward`，而是用了经典的**模板方法模式（Template Method）**：

- 基类 `FfnLayer` 写完整的 `forward` 流程，但**把「用哪种激活」抽成一个虚函数 `getActivationType()`**，默认返回 `InvalidType`。
- 子类 `GeluFfnLayer` / `ReluFfnLayer` / `SiluFfnLayer` **只重写这一个虚函数**，分别返回 `Gelu` / `Relu` / `Silu`。
- 运行期 `forward` 里调用 `genericActivation`，它在一个 `switch` 上按返回值分发到不同的激活 kernel。

这样「换激活」变成了「换一个枚举返回值」，零重复代码、零运行期开销（虚函数解析在每个 forward 里只发生一次）。

#### 4.2.2 核心流程

```text
forward() 内部：
  ...
  auto activation_type = getActivationType();   // 虚函数调用，多态发生在这里
  ...
  genericActivation(...) {
      switch (getActivationType()) {            // 再次按类型分发
          case Gelu / GeGLU: → invokeAddBiasGeluV2 或 invokeGenericActivation<GeluActivation>
          case Relu / ReGLU: → invokeGenericActivation<ReluActivation>
          case Silu / SiGLU: → invokeGenericActivation<SiluActivation>
          case Identity    : → invokeGenericActivation<IdentityActivation>
      }
  }
```

注意 `Gelu` 和 `GeGLU` 共用同一个 case、`Relu` 和 `ReGLU` 共用、`Silu` 和 `SiGLU` 共用——因为门控只是多乘了一路输出，激活函数本身相同。这一点在 [activation_types.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/activation_types.h) 的 `isGatedActivation` 里也有体现。

#### 4.2.3 源码精读

**虚函数声明**。基类里 `getActivationType()` 是 `protected virtual`，默认返回 `InvalidType`：

[src/fastertransformer/layers/FfnLayer.h:86-89](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnLayer.h#L86-L89) — 基类 `getActivationType()` 返回 `ActivationType::InvalidType`。

**子类只重写这一个函数**。以 `GeluFfnLayer` 为例：

[src/fastertransformer/layers/FfnLayer.h:152-157](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnLayer.h#L152-L157) — `GeluFfnLayer::getActivationType()` override 返回 `ActivationType::Gelu`。

`ReluFfnLayer`（[FfnLayer.h:186-191](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnLayer.h#L186-L191)）和 `SiluFfnLayer`（[FfnLayer.h:220-224](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnLayer.h#L220-L224)）同理，只是返回值不同。三个子类的构造函数体都是空的，全部工作交给基类（见 [FfnLayer.cc:593-621](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnLayer.cc#L593-L621) 的 `GeluFfnLayer` 构造函数）。

**分发 switch**。`genericActivation` 把枚举映射到具体 kernel：

[src/fastertransformer/layers/FfnLayer.cc:559-582](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnLayer.cc#L559-L582) — `switch (getActivationType())` 把 `Gelu/GeGLU`、`Relu/ReGLU`、`Silu/SiGLU`、`Identity` 分别派发到 `invokeGenericActivation<GeluActivation>` 等。

这里用到 u3-l1 讲过的「模板的模板参数」技巧：`GeluActivation` / `ReluActivation` / `SiluActivation` 是策略类，`invokeGenericActivation<ACT>` 在编译期把激活函数内联进 kernel，换激活零开销。注意 Gelu 分支有一个小优化：非门控、非 INT8 时直接走更快的 `invokeAddBiasGeluV2`（[FfnLayer.cc:563-566](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnLayer.cc#L563-L566)）。

**门控激活的判定**。是否走门控由两件事同时决定：构造时传入的 `use_gated_activation_` 标志，**且** `intermediate_weight2.kernel != nullptr`：

[src/fastertransformer/layers/FfnLayer.cc:83-84](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnLayer.cc#L83-L84) — `const bool use_gated_activation = use_gated_activation_ && ffn_weights->intermediate_weight2.kernel != nullptr;`

对应的权重字段在 `FfnWeight` 里：

[src/fastertransformer/layers/FfnWeight.h:23-30](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnWeight.h#L23-L30) — `FfnWeight` 含 `gating_weight`（MoE 用）、`intermediate_weight`（GEMM1）、`intermediate_weight2`（门控用）、`output_weight`（GEMM2）、`ia3_weight`。

#### 4.2.4 代码实践

**实践目标**：体会「加一种新激活只需新增一个子类」的可扩展性。

**操作步骤**：

1. 在 [FfnLayer.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnLayer.h) 里并排对比 `GeluFfnLayer`（132-163 行）、`ReluFfnLayer`（165-197 行）、`SiluFfnLayer`（199-230 行）三个类的声明。
2. 数一下每个子类**真正新增**了多少代码（提示：除了构造函数，只有一个 4 行的 `getActivationType()`）。
3. 思考：如果要新增一个 `TanhFfnLayer`，按这个模式需要改哪几处？

**需要观察的现象 / 预期结果**：你会发现三个子类几乎是「复制粘贴 + 改一个枚举值」。新增 `TanhFfnLayer` 大致需要：(a) 在 `ActivationType` 枚举加 `Tanh`；(b) 在 `genericActivation` 的 switch 加一个 case；(c) 写一个 `TanhActivation` 策略类（或复用 `invokeGenericActivation`）；(d) 新增 `TanhFfnLayer` 子类只重写 `getActivationType()`。**不需要碰基类的 `forward`**——这就是模板方法模式的价值。

> 待本地验证：实际新增类还需要在 [activation_types.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/activation_types.h) 的 `getActivationType(string)` 里加字符串解析，并在对应 kernel 文件里实例化 `invokeGenericActivation<TanhActivation>` 的模板。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Gelu` 和 `GeGLU` 共用同一个 `case`？

> **答案**：GeGLU 的「门控」只是在激活后再乘以另一路 GEMM 的输出（`σ(W1·x) * (W_gate·x)`），激活函数本身仍是 Gelu。门控与否由 4.2.3 末尾的 `use_gated_activation` 标志控制（决定是否多算一路 GEMM、是否传 `bias2`），与激活枚举无关，所以两者共用同一个 kernel 分支。

**练习 2**：`SiluFfnLayer` 的构造函数没有 `int8_mode` 参数（见 [FfnLayer.h:202-213](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnLayer.h#L202-L213)），而 `GeluFfnLayer` 有，为什么？

> **答案**：`SiluFfnLayer` 在构造时把 `int8_mode` 硬编码为 `0` 传给基类（见 [FfnLayer.cc:687-700](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnLayer.cc#L687-L700)）。也就是说当前 Silu 变体不与 INT8 量化组合使用；Gelu 变体则保留了 INT8 能力（因为 BERT/GPT 的 INT8 路径用 Gelu）。

---

### 4.3 张量并行 FFN：列切分 + 行切分 + all-reduce

#### 4.3.1 概念说明

单卡 FFN 是 \( y = W_2 \cdot \sigma(W_1 \cdot x) \)。要把这一层摊到 \(N\) 张卡（tensor_para_size = \(N\)）上，且希望结果与单卡**完全一致**，FT 用的是 Megatron-LM 论文里的经典切分：

1. **第一段 GEMM 列并行**：把 \(W_1\) 沿输出维（inter_size 维）切成 \(N\) 份，每张卡持有 \(W_1^{(i)} \in \mathbb{R}^{d_{\text{inter}}/N \times d}\)。每张卡独立算出自己的 intermediate 切片 \( h^{(i)} = \sigma(W_1^{(i)} x) \in \mathbb{R}^{d_{\text{inter}}/N} \)。**这一步无需通信。**
2. **激活逐元素生效**：因为 \(\sigma\) 是逐元素的，对切片分别激活等于对整体激活的对应切片——这是切分正确的数学根基。
3. **第二段 GEMM 行并行**：把 \(W_2\) 沿输入维（同样是 inter_size 维）切成 \(N\) 份，每张卡持有 \(W_2^{(i)} \in \mathbb{R}^{d \times d_{\text{inter}}/N}\)。每张卡算出输出的一个**部分和** \( y^{(i)} = W_2^{(i)} h^{(i)} \)。
4. **一次 all-reduce 求和**：\( y = \sum_{i=0}^{N-1} y^{(i)} \)，得到与单卡一致的结果。

#### 4.3.2 核心流程

用数学证明它等价于单卡。记完整 intermediate \( h = \sigma(W_1 x) \)，把它按 inter_size 维分块为 \( h = [h^{(0)}; h^{(1)}; \dots; h^{(N-1)}] \)，对应 \( W_2 = [W_2^{(0)} \; W_2^{(1)} \dots W_2^{(N-1)}] \)（按列分块）。则：

\[
y = W_2\, h = \sum_{i=0}^{N-1} W_2^{(i)}\, h^{(i)}
\]

而每张卡算出的正是 \( y^{(i)} = W_2^{(i)} h^{(i)} \)，因此：

\[
y = \sum_{i=0}^{N-1} y^{(i)}
\]

关键前提只有一条：**激活 \(\sigma\) 必须逐元素**。Gelu、Relu、Silu 都满足；门控激活（GeGLU 等）的乘法也是逐元素，同样满足。这就是为什么 FT 敢在中间不做任何通信——「激活逐元素」让列并行的切片各自激活后，仍能被行并行的部分和正确地拼回去。

```text
                x [token, hidden]  (每张卡都持完整副本)
                │
   ┌────────────┴────────────┐
   ▼ rank0        ▼ rank1   ▼ ...
列并行 GEMM1   列并行 GEMM1
W1⁽⁰·x → h⁽⁰⁾   W1⁽¹·x → h⁽¹⁾       ← 各算 inter_size/N 维，不通信
   │σ(逐元素)      │σ
   ▼              ▼
行并行 GEMM2   行并行 GEMM2
W2⁽⁰·h⁽⁰⁾=y⁽⁰⁾  W2⁽¹·h⁽¹⁾=y⁽¹⁾       ← 各算 [token, hidden] 的部分和
   │              │
   └──────┬───────┘
          ▼
   ftNcclAllReduceSum  (整层唯一一次通信)
          ▼
      y = Σ y⁽ⁱ⁾  == 单卡结果
```

对比注意力层（u3-l3）：两者都是「中间无通信、末尾一次 all-reduce」，区别在于注意力把通信点放在输出投影之后、FFN 也放在第二段 GEMM 之后，**两个子层各一次 all-reduce**，构成了一个 transformer block 在张量并行下的通信开销下界。

#### 4.3.3 源码精读

**继承关系**。`TensorParallelGeluFfnLayer` 公开继承 `GeluFfnLayer`（也就是 4.1、4.2 那套完整的两段 GEMM + Gelu 激活逻辑），只额外持有四个成员：

[src/fastertransformer/layers/TensorParallelGeluFfnLayer.h:25-31](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/TensorParallelGeluFfnLayer.h#L25-L31) — 成员 `tensor_para_`、`custom_all_reduce_comm_`、`enable_custom_all_reduce_`、`do_all_reduce_`。

**列切分发生在构造函数**。构造时把传进来的完整 `inter_size` 除以 `tensor_para.world_size_`，再交给基类——这样基类 `forward` 里所有 GEMM 的 inter 维度都自动变成「本卡的那一份」：

[src/fastertransformer/layers/TensorParallelGeluFfnLayer.cc:86-98](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/TensorParallelGeluFfnLayer.cc#L86-L98) — 基类构造参数写作 `inter_size / tensor_para.world_size_`，于是本卡的 `inter_size_` 就是 `inter_size / N`。

紧接着的断言保证切得开：

[src/fastertransformer/layers/TensorParallelGeluFfnLayer.cc:105](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/TensorParallelGeluFfnLayer.cc#L105) — `FT_CHECK(inter_size % tensor_para_.world_size_ == 0);`

这正是 u1-l4 提到的「`inter_size` 必须能被 tensor_para_size 整除」的来源（与 `head_num % TP == 0` 同性质）。

> **谁来切权重？** 构造函数只切了「维度」，真正按 rank 取出对应那一列/行权重的工作在 `ParallelGptWeight` / `BertWeight` 的权重加载阶段完成（u2-l5 讲过：列并行权重存成 `.<rank>.bin`，沿输出维切）。`FfnLayer` 本身只管「按本卡的 `inter_size_` 算」，不感知切分。

**forward = 基类 forward + 一次 all-reduce**。`TensorParallelGeluFfnLayer::forward` 的核心只有三步：(1) 可选地交换自定义 all-reduce 缓冲；(2) 调基类 `GeluFfnLayer<T>::forward`（也就是 4.1 的两段 GEMM + 激活，此时每卡只算自己的部分和）；(3) 末尾 all-reduce 求和：

[src/fastertransformer/layers/TensorParallelGeluFfnLayer.cc:51-65](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/TensorParallelGeluFfnLayer.cc#L51-L65) — 先 `GeluFfnLayer<T>::forward(...)`，随后 `ftNcclAllReduceSum(ffn_out, ffn_out, token_num * hidden_units, tensor_para_, stream_)`。

注意 `ftNcclAllReduceSum` 的签名（[nccl_utils.h:90](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/nccl_utils.h#L90)）：`send_buf` 和 `recv_buf` 这里是同一个指针 `ffn_out`，即**原地（in-place）all-reduce**——把每张卡的部分和就地累加成完整结果。

**两个开关**。

- `do_all_reduce_ && tensor_para_.world_size_ > 1`（[第 55 行](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/TensorParallelGeluFfnLayer.cc#L55)）：只有多卡且开关打开才真正通信。`do_all_reduce_` 的作用是**延迟通信**——某些上层结构会把连续两层的 all-reduce 合并，或者把 FFN 的 all-reduce 和注意力层的 all-reduce 合并到一起，于是构造时传 `do_all_reduce=false`，由调用方自己在合适时机统一 reduce。
- `enable_custom_all_reduce_`：开启时走 FT 自带的低延迟 all-reduce kernel（`customAllReduce`，见 u7-l3），否则走 NCCL（`ftNcclAllReduceSum`）。两条路径在第 56-62 行的二选一里体现。

**三个变体逐行同构**。`TensorParallelSiluFfnLayer`（[TensorParallelSiluFfnLayer.cc:32-63](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/TensorParallelSiluFfnLayer.cc#L32-L63)）和 `TensorParallelReluFfnLayer`（[TensorParallelReluFfnLayer.cc:33-66](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/TensorParallelReluFfnLayer.cc#L33-L66)）与 Gelu 版几乎逐字相同，差别仅在：继承的基类（`SiluFfnLayer` / `ReluFfnLayer`）、构造时是否传 `int8_mode`（Silu 版不传，呼应 4.2.5 练习 2）。这再次印证 4.2 的多态设计：**并行的逻辑与激活的逻辑是正交的，互不干扰**。

#### 4.3.4 代码实践

**实践目标**：画出 TP=4 时 FFN 的数据流图，并口头证明它与单卡等价。

**操作步骤**：

1. 在纸上画 4 列（代表 rank0~rank3），顶部是公共输入 `x [token, hidden]`（每卡完整副本）。
2. 在每列画两个方框：上面是「GEMM1（列并行，inter_size/4）」，下面是「GEMM2（行并行，输出 hidden）」，中间夹一个 σ。
3. 在最下方画一个连接 4 列的「all-reduce 求和」节点，输出 `y [token, hidden]`。
4. 对照源码核对：列切分对应 [TensorParallelGeluFfnLayer.cc:91](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/TensorParallelGeluFfnLayer.cc#L91) 的 `inter_size / tensor_para.world_size_`；all-reduce 对应第 57 行的 `ftNcclAllReduceSum`。
5. 写下等价性证明的两行关键式（见 4.3.2 的求和分解）。

**需要观察的现象 / 预期结果**：

- 整层只有**一次**跨卡通信（末尾 all-reduce），中间两段 GEMM 与激活完全本地。
- 第一段 GEMM 每卡计算量是单卡的 \(1/N\)（inter_size 小了）；第二段 GEMM 每卡也是 \(1/N\)（规约维 inter_size 小了）。所以张量并行把 FFN 的计算与显存都近似线性摊薄到 \(N\) 张卡。
- 数学等价的关键前提：**激活逐元素**。如果哪天有人把激活换成「跨维度的函数」（比如对 intermediate 做一次 softmax），这个切分就不再正确——这是一个很好的反向检验。

**为什么与单卡等价（一句话）**：行并行 GEMM2 的输出是按规约维切片后的部分和，而 all-reduce 正好把这些部分和按 \( y = \sum_i W_2^{(i)} \sigma(W_1^{(i)} x) \) 拼回完整结果；又因 σ 逐元素，分别激活再拼接等于整体激活。

> 待本地验证：若有 4 卡环境，可对照 [examples/cpp/multi_gpu_gpt](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_example.cc) 跑 TP=4 与 TP=1 各一次，比较输出 logits 是否一致（排除数值精度误差）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 FFN 的 all-reduce 放在「第二段 GEMM 之后」而不是「第一段 GEMM 之后」？

> **答案**：第一段 GEMM 是列并行，输出是 intermediate 的**不同切片**，各卡拼接起来才是完整 intermediate，不能求和（求和会把不同 feature 加在一起，无意义）。第二段 GEMM 是行并行，输出是 hidden 维的**部分和**，求和才有意义。所以唯一合法的 all-reduce 点就是第二段之后。

**练习 2**：`do_all_reduce_` 设为 `false` 时，all-reduce 不发生，那 partial output 被谁、在什么时候 reduce？

> **答案**：由**调用方**（如 `ParallelGptDecoder` 等上层模型）在更合适的时机统一 reduce，例如把注意力层和 FFN 层的两次 all-reduce 合并、或把多层的 reduce 延迟到流水线边界。这样能减少通信次数、重叠计算与通信。构造函数里的 `do_all_reduce` 参数就是为此预留的「延迟通信」开关。

**练习 3**：`inter_size = 4096 * 4 = 16384`、`tensor_para_size = 8`，能否切成 8 份？每卡 inter_size 是多少？

> **答案**：`16384 % 8 == 0`，可以切；每卡 `inter_size_ = 2048`。若 `inter_size` 不能被 `world_size` 整除，[第 105 行](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/TensorParallelGeluFfnLayer.cc#L105) 的 `FT_CHECK` 会让程序直接 abort。

---

## 5. 综合实践

把 4.1 ~ 4.3 串起来，完成下面这个「读码 + 推理」小任务：

**任务**：假设你要为一个 GPT 模型（`hidden_units=4096`、`inter_size=16384`、激活=Silu、TP=4）配置 FFN 层，请：

1. **选类**：应该实例化 `TensorParallelSiluFfnLayer` 还是 `SiluFfnLayer`？为什么？
   > 参考真实用法：[src/fastertransformer/models/bert/Bert.cc:53-87](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/Bert.cc#L53-L87) 展示了模型如何按 `activation_type_` 选择对应的 TensorParallel 变体（Gelu 走 `TensorParallelGeluFfnLayer`、Relu 走 `TensorParallelReluFfnLayer`）。
2. **算维度**：写出每张卡上 GEMM1 的 `(m, n, k)` 与 GEMM2 的 `(m, n, k)`（cuBLAS 列主序视角，假设 `token_num = 32`）。
3. **画图**：画出 4 卡数据流图，标出唯一的 all-reduce 点。
4. **验证**：用 4.3.2 的求和分解式，写出「4 卡部分和相加 == 单卡完整 FFN」的一行证明。

**参考答案要点**：

1. 选 `TensorParallelSiluFfnLayer`：因为 TP=4>1，需要张量并行；激活是 Silu。注意它的构造函数不收 `int8_mode`（默认 0）。
2. 每卡 `inter_size_ = 16384 / 4 = 4096`。
   - GEMM1：`(m=4096, n=32, k=4096)`（行主序输出 `[32, 4096]`）。
   - GEMM2：`(m=4096, n=32, k=4096)`（行主序输出 `[32, 4096]`，注意此时输出 feature 维是 hidden_units=4096，规约维是 inter_size/4=4096）。
3. 数据流图同 4.3.2，all-reduce 在 GEMM2 之后、由 `ftNcclAllReduceSum` 对 `[32, 4096]` 的输出原地求和。
4. 证明：\( y = W_2 \sigma(W_1 x) = \sum_{i=0}^{3} W_2^{(i)} \sigma(W_1^{(i)} x) = \sum_{i=0}^{3} y^{(i)} \)，最后一步等于 all-reduce 的结果。

---

## 6. 本讲小结

- FFN 在 GPU 上落地为「**两段 GEMM + 一段激活**」：GEMM1 把 hidden 升到 inter、GEMM2 把 inter 降回 hidden；第二段的加偏置被融合进下游残差 kernel。
- 激活的差异用**模板方法模式**解决：基类 `FfnLayer` 写完整 `forward`，子类 `Gelu/Relu/SiluFfnLayer` 只重写 `getActivationType()`，运行期由 `genericActivation` 的 switch 分发到对应 kernel。
- 张量并行 FFN 采用「**GEMM1 列切分 + GEMM2 行切分 + 末尾一次 all-reduce**」：构造时 `inter_size /= world_size_`，整层只在最后做一次 `ftNcclAllReduceSum`。
- 等价性的数学根基是「**激活逐元素**」：行并行的部分和求和恰好还原完整结果，前提是中间没有跨维度的非线性。
- `TensorParallelGelu/Silu/ReluFfnLayer` 三个变体逐行同构，证明「并行逻辑」与「激活逻辑」**正交解耦**；`do_all_reduce_` 与 `enable_custom_all_reduce_` 两个开关分别支持「延迟通信」与「NCCL/自定义 kernel 二选一」。

## 7. 下一步学习建议

- **横向对比注意力层**：回到 u3-l3，把 `TensorParallelGeluFfnLayer` 与 `TensorParallelUnfusedAttentionLayer` 并排看，体会两者「列切分输入投影 + 行切分输出投影 + 末尾 all-reduce」的同构性——一个 transformer block 在 TP 下恰好两次 all-reduce。
- **纵向看权重切分**：结合 u2-l5 的 `ParallelGptWeight` / `BertWeight`，搞清楚 `intermediate_weight`（列并行，存 `.<rank>.bin`）和 `output_weight`（行并行，bias 完整）在磁盘上的实际布局，把「维度切分」与「权重文件切分」对应起来。
- **进入端到端模型**：本讲的 FFN 是「层（layer）级」组件，下一站可读 u4-l1（BERT model）或 u6-l1（ParallelGpt），看一个完整的 transformer block 如何把注意力层、FFN 层、layernorm、残差串成 `forward`，以及 TP 下的两次 all-reduce 在模型主循环里出现在哪里。
- **进阶**：若关心 INT8/FP8，可对比 [FfnLayer.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnLayer.cc) 里 `int8_mode==1`（weight-only，CUTLASS `gemm_bias_act` 融合）与 `int8_mode==2`（SmoothQuant w8a8，`Int8Gemm`）两条旁路，它们是 u9 量化主题的预热。

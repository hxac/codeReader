# 权重容器与权重加载：DenseWeight / BaseWeight / 各模型 Weight

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 `DenseWeight` 这个「最小积木」封装了什么字段，为什么它只持有 `const T*` 指针而不拥有内存。
- 看懂 `AttentionWeight` / `FfnWeight` / `LayerNormWeight` 是如何用 `DenseWeight` 拼装出一层的全部权重。
- 画出 BERT 权重的三级组织树：模型级 `BertWeight` → 每层 `BertLayerWeight` → 各模块 `DenseWeight`，并理解「分配-登记-绑定」三步走。
- 解释权重文件名中的 `.rank.bin` 后缀，以及 self-attention 的 QKV 权重在 `tensor_para_size=2` 时如何按列切分（即按 head 切分）。
- 对比 BERT 与 GPT（`ParallelGptWeight` / `ParallelGptDecoderLayerWeight`）在权重组织上的差异。

## 2. 前置知识

本讲建立在前面几讲的概念之上，先做最简回顾：

- **推理库不训练、不更新权重**：FasterTransformer（下称 FT）是推理库，权重在模型构造时一次性加载到 GPU，之后只读不变。因此权重管理走的是「冷路径」的一次性分配（u2-l2 的 `deviceMalloc`/`deviceFree`），而不是 forward 热路径上的 `IAllocator::reMalloc`。
- **Tensor 是描述符、不拥有内存**（u2-l1）：`Tensor` 字段全为 const、不分配不释放。本讲你会看到同样的「非拥有指针」思想在权重里再次出现——`DenseWeight` 的 `kernel`/`bias` 都是借来的 `const T*`。
- **张量并行（Tensor Parallel, TP）**：把一个大矩阵乘切成若干份分到多张卡上算，关键约束是 `head_num % tensor_para_size == 0`（u1-l4）。本讲会落到具体权重上看这份切分是怎么体现在「文件名 + shape」里的。
- **数据类型 `T`**：FT 全库用模板参数 `T`（`float` / `half` / `__nv_bfloat16`）抽象精度，权重类几乎都是 `template<typename T> struct XxxWeight`。

> 一句话直觉：**权重在 FT 里被组织成一棵「指针树」**——根节点拥有真正的显存，叶子节点（`DenseWeight`）只是指向这些显存的 `const T*` 视图，模型 forward 时拿到的就是这些视图。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/fastertransformer/layers/DenseWeight.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/DenseWeight.h) | 权重的最小积木：一个线性层（GEMM）所需的 kernel、bias 及各类量化 scale。 |
| [src/fastertransformer/models/BaseWeight.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/BaseWeight.h) | `FtWeight<T>`：带名字、shape、指针的单个权重描述单元，是 `unordered_map` 的 value 类型。 |
| [src/fastertransformer/layers/attention_layers/AttentionWeight.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/AttentionWeight.h) | 一层注意力的权重聚合：Q/K/V/输出 四个 `DenseWeight`（外加 ia3）。 |
| [src/fastertransformer/layers/FfnWeight.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnWeight.h) | FFN 的权重聚合：intermediate、output 两个 `DenseWeight`（外加门控/ia3）。 |
| [src/fastertransformer/kernels/layernorm_kernels.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/layernorm_kernels.h) | `LayerNormWeight<T>`：仅 gamma/beta 两个指针。 |
| [src/fastertransformer/models/bert/BertWeight.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/BertWeight.h) / [.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/BertWeight.cc) | BERT 模型级权重：N 个 `BertLayerWeight` + 末层 LayerNorm。 |
| [src/fastertransformer/models/bert/BertLayerWeight.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/BertLayerWeight.h) / [.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/BertLayerWeight.cc) | 单个 transformer 层权重：attention + ffn + 两个 LayerNorm，含加载与切分逻辑。 |
| [src/fastertransformer/models/multi_gpu_gpt/ParallelGptWeight.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGptWeight.h) | GPT 模型级权重：含 embedding 表、位置编码、prompt learning 表等。 |
| [src/fastertransformer/models/multi_gpu_gpt/ParallelGptDecoderLayerWeight.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGptDecoderLayerWeight.h) | GPT 单层权重，结构与 BERT 层同源但增加了 GPT 变体字段。 |

## 4. 核心概念与源码讲解

### 4.1 DenseWeight：权重的最小积木

#### 4.1.1 概念说明

Transformer 里几乎每个子模块（Q/K/V 投影、attention 输出投影、FFN 的两段线性层）本质上都是一个**带 bias 的矩阵乘**：`y = xW + b`。FT 把「一个这样的线性层所需的全部权重」抽象成一个结构体 `DenseWeight`，它是整个权重体系的**最小积木**——再往下就只是裸指针了。

为什么不让每个层各自定义 kernel/bias 字段？因为同一个 GEMM 在不同精度/量化模式下需要伴随不同的 scale。把这些字段集中到一个结构体里，layer 代码就能用统一的 `dense.kernel` / `dense.bias` / `dense.scale` 写法，不必为 FP16/INT8/FP8 各写一套。

关键设计：**`DenseWeight` 只持有 `const T*` 指针，默认全是 `nullptr`，它自己不分配也不释放内存**。它是「视图」而非「主人」。真正的显存由上一级（`BertLayerWeight` 等）拥有，构造时把指针「绑定」进来。这和 u2-l1 的 `Tensor` 非拥有思想完全一致。

#### 4.1.2 核心流程

一个 `DenseWeight` 在其生命周期里经历三个阶段：

1. **诞生**：被包含进某个 `AttentionWeight`/`FfnWeight` 时自动构造，所有指针为 `nullptr`。
2. **绑定**：上层 `setWeightPtr()` 把已分配好显存的地址赋给 `kernel`/`bias`（以及需要的 scale 指针）。
3. **使用**：forward 时被 layer 拿去喂给 `cublasMMWrapper` 的 GEMM（u2-l3）。

死亡时不自己 free——由拥有显存的那一级统一释放。

`DenseWeight` 字段可分为三组：

| 分组 | 字段（节选） | 用途 |
| --- | --- | --- |
| 基础（全精度通用） | `kernel`, `bias` | 标准 FP16/FP32/BF16 的 GEMM 权重与偏置 |
| 稀疏 | `sp_kernel` | 结构化稀疏（Sparsity，需 Ampere，u1-l2 的 `SPARSITY_SUPPORT`）压缩后的权重 |
| INT8 量化 | `int8_kernel`, `scale`, `scale_inter`, `scale_out`, `weight_only_quant_scale` | w8a8 / weight-only INT8 的量化权重与缩放因子（u9-l1/u9-l2） |
| FP8 量化 | `input_scale`, `weight_scale`, `output_scale` 及其 `_inv`、`_h_`（host）版本 | FP8（Hopper，u9-l3）的逐张量缩放 |

#### 4.1.3 源码精读

`DenseWeight` 是一个双模板参数结构体（`T1` 为权重/输入类型，`T2` 默认与 `T1` 相同，用于 bias 在某些配置下取不同精度）：

[src/fastertransformer/layers/DenseWeight.h:L29-L64](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/DenseWeight.h#L29-L64) 定义了上述全部字段。最核心的两行：

```cpp
const T1* kernel = nullptr;
const T2* bias   = nullptr;
```

注意每个字段都带 `= nullptr` 默认值——这意味着你可以廉价地构造一个「空」`DenseWeight`，再按需填指针；用不到 INT8/FP8 的模型，那些 scale 指针就一直是 `nullptr`，layer 代码通过判断是否为空来决定走哪条量化路径。

文件顶部的注释（[DenseWeight.h:L23-L27](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/DenseWeight.h#L23-L27)）还点明了 INT8 各 scale 的语义：`scale` 量化输入、`scale_inter` 是「输出 scale / (输入 scale × 权重 scale)」、`scale_out` 把激活反量化回浮点。这些会在 u9-l1 详细展开，本讲只需记住「scale 也住在这个结构体里」。

#### 4.1.4 代码实践

**实践目标**：用源码阅读确认 `DenseWeight` 的「非拥有」本性。

**操作步骤**：

1. 打开 `DenseWeight.h`，确认结构体里**没有任何 `malloc`/`cudaMalloc`/`new`，也没有析构函数**——只有一堆指针。
2. 再打开 `BertLayerWeight.cc` 的析构函数（下文 4.3 会读到），观察 free 的动作发生在 `BertLayerWeight` 这一级，而**不是** `DenseWeight` 这一级。

**需要观察的现象**：`DenseWeight` 全是 `const T*`（注意是 `const`！），意味着拿到它的人没有权限改写权重内容——这正符合「推理阶段权重只读」的约束。

**预期结果**：你能用一句话总结——「`DenseWeight` 是一组 `const` 裸指针的集合，它描述权重但不拥有权重」。

#### 4.1.5 小练习与答案

**练习 1**：既然 `DenseWeight` 不拥有内存，那它的 `kernel` 指针最终指向的那块显存，是由谁分配、由谁释放？

> **参考答案**：由包含它的层权重类（如 `BertLayerWeight`）在构造时用 `deviceMalloc` 分配、在析构时用 `deviceFree` 释放；`DenseWeight` 只通过 `setWeightPtr()` 拿到指向该显存的 `const T*` 视图。

**练习 2**：为什么 `kernel`/`bias` 都声明成 `const T*` 而不是 `T*`？

> **参考答案**：推理阶段权重是只读的，`const` 在编译期就阻止了任何误写，同时也允许同一份权重被多个并发的 forward 请求共享读取而不引入数据竞争。

---

### 4.2 复合层权重：用 DenseWeight 拼装 Attention / FFN / LayerNorm

#### 4.2.1 概念说明

有了最小积木，下一步是把它拼成「一个子模块的权重」。Transformer 一层里有四类子模块：self-attention（含 Q/K/V/输出 四个线性层）、FFN（含两段线性层）、以及若干 LayerNorm。FT 为每类子模块定义一个聚合结构体：

- `AttentionWeight`：注意力
- `FfnWeight`：前馈网络
- `LayerNormWeight`：层归一化

它们都只是 `DenseWeight`（或裸指针）的简单组合，同样不拥有内存——是「指针树」的中间层。

#### 4.2.2 核心流程

- **AttentionWeight** 由 4 个 `DenseWeight` 拼成：`query_weight`、`key_weight`、`value_weight`、`attention_output_weight`，对应 Q/K/V 投影和注意力输出投影；外加 `ia3_key_weight`/`ia3_value_weight` 用于 IA3 微调（可忽略）。
- **FfnWeight** 由 2 个核心 `DenseWeight` 拼成：`intermediate_weight`（第一段，升维）、`output_weight`（第二段，降维）；外加门控用的 `gating_weight`/`intermediate_weight2`（Gated activation，如 GLU/SwiGLU）和 `ia3_weight`。
- **LayerNormWeight** 只有 `gamma` 和 `beta` 两个 `const T*`（不需要 `DenseWeight`，因为 LayerNorm 没有 GEMM）。

#### 4.2.3 源码精读

注意力聚合体——4 个 `DenseWeight`（[AttentionWeight.h:L23-L31](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/AttentionWeight.h#L23-L31)）：

```cpp
struct AttentionWeight {
    DenseWeight<T1, T2> query_weight;
    DenseWeight<T1, T2> key_weight;
    DenseWeight<T1, T2> value_weight;
    DenseWeight<T1, T2> attention_output_weight;
    DenseWeight<T1, T2> ia3_key_weight;
    DenseWeight<T1, T2> ia3_value_weight;
};
```

FFN 聚合体（[FfnWeight.h:L23-L30](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnWeight.h#L23-L30)）：注意 `intermediate_weight`（升维到 inter_size）和 `output_weight`（降维回 hidden_units）这两个是 FFN 的主干。

LayerNorm 聚合体（[layernorm_kernels.h:L48-L51](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/layernorm_kernels.h#L48-L51)），极简：

```cpp
struct LayerNormWeight {
    const T* gamma = nullptr;
    const T* beta  = nullptr;
};
```

> 设计要点：从 `DenseWeight` → `AttentionWeight`/`FfnWeight` → 层权重 → 模型权重，每一层都是「用结构体把下一层组合起来」，没有任何虚函数、没有继承，纯粹是**值语义的组合**。这让权重对象可以廉价地整体拷贝（FT 也确实实现了拷贝构造与 `operator=`，见 4.3）。

#### 4.2.4 代码实践

**实践目标**：用一张表理清「一层 transformer 需要几个 `DenseWeight`」。

**操作步骤**：数一数，self-attention 有 4 个 `DenseWeight`（Q/K/V/输出），FFN 有 2 个主干 `DenseWeight`（intermediate/output），加上两个 LayerNorm 各有 gamma/beta。

**预期结果（计数表）**：

| 子模块 | DenseWeight 个数 | 额外裸指针 |
| --- | --- | --- |
| self-attention | 4（Q/K/V/输出） | — |
| FFN | 2（intermediate/output） | — |
| 2× LayerNorm | 0 | 4（gamma/beta × 2） |

这正好对应 BERT 一层里有 6 个 GEMM（4 attention + 2 FFN）——与 u4-l1 将讲到的「BERT 一个 block 的 6 个 custom kernel + GEMM」一一对上。

#### 4.2.5 小练习与答案

**练习**：`LayerNormWeight` 为什么不用 `DenseWeight`，而只用两个裸 `const T*`？

> **参考答案**：LayerNorm 没有矩阵乘、也就没有 kernel 矩阵，只有逐通道的缩放 `gamma` 和偏移 `beta` 两个一维向量，用 `DenseWeight` 会带一堆无意义的 nullptr 字段，所以直接用两个裸指针最简洁。

---

### 4.3 BERT 权重层级：BertWeight 与 BertLayerWeight 的三级树

#### 4.3.1 概念说明

现在把镜头拉到模型级。BERT 的全部权重被组织成一棵三级树：

```
BertWeight（模型级，拥有显存）
├── bert_layer_weights[0..N-1]   ← 每层一个 BertLayerWeight
│   ├── attention_weights (AttentionWeight)
│   │   ├── query_weight (DenseWeight)  → kernel, bias
│   │   ├── key_weight   (DenseWeight)  → kernel, bias
│   │   ├── value_weight (DenseWeight)  → kernel, bias
│   │   └── attention_output_weight     → kernel, bias
│   ├── attn_layernorm_weights (gamma, beta)
│   ├── ffn_weights (FfnWeight)
│   │   ├── intermediate_weight         → kernel, bias
│   │   └── output_weight               → kernel, bias
│   └── ffn_layernorm_weights (gamma, beta)
└── post_transformer_layernorm_weights (gamma, beta)   ← 最后一层后的 LayerNorm
```

根节点 `BertWeight` **真正拥有显存**（通过私有成员 `weights_ptr` 和 `is_maintain_buffer` 标志），中间层和叶子都是借来的指针。

#### 4.3.2 核心流程：分配-登记-绑定 三步走

`BertLayerWeight` 的构造用了 FT 权重加载最经典的「**分配-登记-绑定**」三步，这是本讲最重要的模式：

1. **登记（Register）**：把每个权重的**名字**和 **shape** 登记进一个 `unordered_map<string, FtWeight<T>>`，名字里带上 `tensor_para_rank`（这就是 TP 切分的体现，见 4.4）。此时 `ptr_` 还是 `nullptr`。
2. **分配（Malloc）**：遍历 map，对每个 `FtWeight` 调 `deviceMalloc(&ptr_, size_)` 真正申请显存。
3. **绑定（setWeightPtr）**：把 map 里的 `ptr_` 一一赋给 `attention_weights.query_weight.kernel` 等叶子指针，让结构体树与显存挂上钩。

加载真实权重时（`loadModel`）只是把磁盘上的 `.bin` 文件按名字读进已经分配好的 `ptr_` 里。

模型级 `BertWeight` 的逻辑同理但更简单：它只为「末层 LayerNorm」分配 2 块显存（`weights_ptr[0/1]`），然后把 N 层的构造下放给 `BertLayerWeight`。

#### 4.3.3 源码精读

**模型级 `BertWeight`**（[BertWeight.h:L23-L59](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/BertWeight.h#L23-L59)）。两个核心成员：

```cpp
std::vector<BertLayerWeight<T>> bert_layer_weights;          // 每层
LayerNormWeight<T>              post_transformer_layernorm_weights;  // 末层 LN
```

构造函数（[BertWeight.cc:L22-L49](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/BertWeight.cc#L22-L49)）：先为末层 LayerNorm 分配两块 `hidden_units_` 大小的显存（gamma、beta），再循环 push N 个 `BertLayerWeight`：

```cpp
deviceMalloc(&weights_ptr[0], hidden_units_);   // gamma
deviceMalloc(&weights_ptr[1], hidden_units_);   // beta
setWeightPtr();
for (int i = 0; i < num_layer_; i++) {
    bert_layer_weights.push_back(
        BertLayerWeight<T>(hidden_units_, inter_size_, tensor_para_size_, tensor_para_rank_));
}
```

绑定末层 LN（[BertWeight.cc:L141-L148](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/BertWeight.cc#L141-L148)）：

```cpp
post_transformer_layernorm_weights.gamma = weights_ptr[0];
post_transformer_layernorm_weights.beta  = weights_ptr[1];
```

**每层 `BertLayerWeight`**（[BertLayerWeight.h:L29-L65](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/BertLayerWeight.h#L29-L65)）。注意私有成员里那个 map：

```cpp
std::unordered_map<std::string, FtWeight<T>> weights_ptr;  // 名字 → 显存描述
```

map 的 value 类型 `FtWeight<T>` 定义在 [BaseWeight.h:L24-L47](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/BaseWeight.h#L24-L47)，它持有一个名字、shape、size 和裸指针 `ptr_`，并在构造时由 shape 连乘算出 `size_`：

```cpp
FtWeight(const std::string name, const std::vector<size_t> shape, T* ptr)
    : name_(name), shape_(shape), ptr_(ptr) {
    size_ = 1;
    for (uint i = 0; i < shape_.size(); i++) size_ *= shape_[i];
}
```

「登记」环节——把每个权重的名字与 shape 写进 map（节选自 [BertLayerWeight.cc:L32-L66](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/BertLayerWeight.cc#L32-L66)）：

```cpp
name = "attention.self.query.weight." + std::to_string(tensor_para_rank_) + ".bin";
weights_ptr.insert({name, FtWeight<T>(name, {hidden_units_, hidden_units_ / tensor_para_size_}, nullptr)});
name = "attention.self.query.bias." + std::to_string(tensor_para_rank_) + ".bin";
weights_ptr.insert({name, FtWeight<T>(name, {hidden_units_ / tensor_para_size_}, nullptr)});
...
```

「分配」环节（[BertLayerWeight.cc:L67-L70](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/BertLayerWeight.cc#L67-L70)）：

```cpp
for (auto it = weights_ptr.begin(); it != weights_ptr.end(); ++it) {
    deviceMalloc(&it->second.ptr_, it->second.size_);
}
```

「绑定」环节 `setWeightPtr()`（[BertLayerWeight.cc:L185-L218](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/BertLayerWeight.cc#L185-L218)）把 map 里的指针赋给 `attention_weights.query_weight.kernel` 等叶子字段。

析构（[BertLayerWeight.cc:L74-L113](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/BertLayerWeight.cc#L74-L113)）由这一级统一 `deviceFree` 并把叶子指针清零——印证了 4.1 所说「`DenseWeight` 自己不释放」。

#### 4.3.4 代码实践

**实践目标**：把 BERT 权重组织树画出来，并定位每个 `DenseWeight` 的 kernel/bias 来自哪个 map 条目。

**操作步骤**：

1. 对照 [BertLayerWeight.h:L47-L50](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/BertLayerWeight.h#L47-L50) 的四个成员（`attention_weights` / `attn_layernorm_weights` / `ffn_weights` / `ffn_layernorm_weights`），画出 4.3.1 那棵树。
2. 打开 `setWeightPtr()`（[BertLayerWeight.cc:L185-L218](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/BertLayerWeight.cc#L185-L218)），为每个叶子指针标注它绑定自哪个 `.bin` 文件名。

**预期结果**：你会得到一张「叶子指针 ↔ .bin 文件名」对照表，例如 `attention_weights.query_weight.kernel ↔ attention.self.query.weight.<rank>.bin`。这张表就是后续 4.4 讨论 TP 切分的基础。

#### 4.3.5 小练习与答案

**练习 1**：`BertLayerWeight` 为什么用 `unordered_map<string, FtWeight>` 而不是直接用一堆成员变量当 buffer？

> **参考答案**：用 map 可以用「文件名」作 key 统一处理「登记、分配、加载、绑定、释放」五件事——循环遍历 map 即可，不必为每个权重重复写一遍样板代码；加载时直接用 key 拼出磁盘路径。

**练习 2**：`BertLayerWeight` 的拷贝构造（[L115-L124](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/BertLayerWeight.cc#L115-L124)）是怎么保证深拷贝的？

> **参考答案**：它先委托给非拷贝构造重新分配一遍自己的显存，再用 `cudaD2Dcpy` 把 `other` 的每块显存逐元素拷过来，因此两份 `BertLayerWeight` 各自独立持有显存，互不影响。

---

### 4.4 权重加载与张量并行切分

#### 4.4.1 概念说明

FT 的权重以**预切分好的 `.bin` 文件**形式存在磁盘上。也就是说，用户在离线阶段（用 FT 提供的转换脚本，如 `examples/pytorch/.../checkpoint converter`）就把权重按 TP 切好，每张卡只读自己那一份。切分信息**编码在文件名里**：带 `.rank.bin` 后缀的就是按 rank 切分过的，不带后缀的就是各 rank 共享（replicated）的。

`loadModel` 的工作就是：读 `config.ini` 确定数据类型，然后对每层把对应的 `.bin` 读进 GPU。

#### 4.4.2 核心流程：TP 切分的两种模式

观察 [BertLayerWeight.cc:L32-L66](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/BertLayerWeight.cc#L32-L66) 里每个权重的 shape 与文件名后缀，可以归纳出 TP 切分的两种模式（即 Megatron 张量并行）：

| 权重 | shape（hidden=H, inter=I, TP=N） | 文件名后缀 | bias shape | 模式 |
| --- | --- | --- | --- | --- |
| Q/K/V kernel | `{H, H/N}` | `.<rank>.bin` | `{H/N}` | **列并行**（按输出维切，= 按 head 切） |
| Q/K/V bias | `{H/N}` | `.<rank>.bin` | — | 列并行（切分） |
| attention output kernel | `{H, H/N}` | `.<rank>.bin` | `{H}`（**完整**） | **行并行**（输出维完整，需 all-reduce） |
| attention output bias | `{H}` | `.bin`（**无 rank**） | — | 复制（replicated） |
| FFN intermediate kernel | `{H, I/N}` | `.<rank>.bin` | `{I/N}` | 列并行（升维维切） |
| FFN output kernel | `{I/N, H}` | `.<rank>.bin` | `{H}`（**完整**） | 行并行（需 all-reduce） |
| LayerNorm gamma/beta | `{H}` | `.bin`（无 rank） | — | 复制 |

直觉解释：

- **列并行（Q/K/V、FFN intermediate）**：把权重矩阵沿「输出特征」维切成 N 份，每卡算一份输出。对 Q/K/V 而言，输出维 = `head_num × size_per_head`，切输出维就等价于**切 head**——这正是为什么必须 `head_num % TP == 0`。列并行每卡输出互不重叠，**不需要通信**。
- **行并行（attention output、FFN output）**：把权重沿「输入特征」维切，每卡输入一份、输出完整，但每卡算出的只是「部分和」，必须 **all-reduce** 求和才是最终结果。这部分的 bias 是完整且复制的，在 all-reduce 之后（或概念上）加一次即可。
- **复制（LayerNorm、输出 bias）**：每个 rank 持有完全相同的一份。

为什么 QKV 用列并行而输出投影用行并行？因为列并行和行并行**配对**使用，可以在一个子模块（attention 或 FFN）的入口和出口之间避免通信：列并行把激活切成 N 份分别处理，行并行再把 N 份部分和 all-reduce 回完整激活。这是 Megatron 论文的核心观察，在 FT 的权重 shape 里直接可见。

#### 4.4.3 源码精读

加载主循环（[BertWeight.cc:L119-L131](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/BertWeight.cc#L119-L131)）：

```cpp
FtCudaDataType model_file_type = getModelFileType(dir_path + "/config.ini", "bert");
for (uint l = 0; l < num_layer_; l++) {
    if (isValidLayerParallelId(l)) {
        bert_layer_weights[l].loadModel(dir_path + "model.encoder.layer." + std::to_string(l) + ".",
                                        model_file_type);
    }
}
```

`isValidLayerParallelId`（[BertWeight.cc:L133-L139](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/BertWeight.cc#L133-L139)）实现的是**流水并行（Pipeline Parallel, PP）**的切层：把 `num_layer_` 按 `pipeline_para_size` 均分，每个 `pipeline_para_rank` 只加载并持有属于自己的那几层。这是「层间切分」，与 TP 的「层内切分」正交。

每层的 `loadModel`（[BertLayerWeight.cc:L175-L183](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/BertLayerWeight.cc#L175-L183)）就是遍历 map、按名字把 `.bin` 读进 `ptr_`：

```cpp
for (auto it = weights_ptr.begin(); it != weights_ptr.end(); ++it) {
    loadWeightFromBin<T>(it->second.ptr_, it->second.shape_, dir_path + it->first, model_file_type);
}
```

注意 `dir_path + it->first`：`it->first` 就是带 `.rank.bin` 后缀的文件名，所以每张卡天然只读自己那一份切分文件。

#### 4.4.4 代码实践：TP=2 时 QKV 如何切分（本讲核心实践任务）

**实践目标**：用具体数字说清 self-attention 的 QKV 权重在 `tensor_para_size=2` 时的切分。

**操作步骤**：

1. 假设 BERT-base：`hidden_units = 768`，`head_num = 12`，`size_per_head = 64`，`TP = 2`。
2. 对照 [BertLayerWeight.cc:L34-L45](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/BertLayerWeight.cc#L34-L45)，写出 rank 0 和 rank 1 各自的 Q 权重文件名与 shape。
3. 验证 `head_num % TP == 0`（12 % 2 == 0 ✓）。

**预期结果**：

- rank 0：文件 `attention.self.query.weight.0.bin`，shape `{768, 768/2} = {768, 384}`，对应 `768/64 = 12` 个 head 中的前 6 个。
- rank 1：文件 `attention.self.query.weight.1.bin`，shape `{768, 384}`，对应后 6 个 head。
- Q bias 同理：rank 0 是 `attention.self.query.bias.0.bin`，shape `{384}`。
- K、V 与 Q 完全对称。

用数学表达列并行的切分（`H = hidden_units`，`N = tensor_para_size`）：

\[
W^{(r)}_{\text{QKV}} \in \mathbb{R}^{H \times (H/N)}, \quad b^{(r)}_{\text{QKV}} \in \mathbb{R}^{H/N}, \quad r \in \{0,\dots,N-1\}
\]

每卡持有 `head_num/N = 6` 个 head 的 Q/K/V，attention 计算在卡内自洽完成、无需通信；直到 attention 输出投影（行并行）才 all-reduce。

**待本地验证**：如果你本地有编译好的 FT 与转换后的 BERT 权重，可在两卡上以 `tensor_para_size=2` 运行 `bert_example`，对比单卡与双卡输出是否一致（行并行 all-reduce 保证了数值等价）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 attention 输出投影的 bias 文件名**没有** `.rank` 后缀，而 QKV 的 bias **有**？

> **参考答案**：QKV 是列并行，bias 随输出维一起切分，所以每个 rank 持有不同的 `.<rank>.bin`；attention 输出投影是行并行，输出维完整、bias 在所有 rank 上相同，只需复制一份，因此文件名无 rank 后缀、shape 是完整的 `{H}`。

**练习 2**：`isValidLayerParallelId` 实现的是 TP 还是 PP？它和 `tensor_para_rank_` 有关系吗？

> **参考答案**：它实现的是**流水并行（PP）**——按 `pipeline_para_rank` 把层均分；它与 `tensor_para_rank_` 无关。TP 切层内、PP 切层间，两者正交，约束是 `TP × PP == world_size`（u1-l4）。

---

### 4.5 GPT 权重层级：ParallelGptWeight 与 ParallelGptDecoderLayerWeight

#### 4.5.1 概念说明

GPT 的权重树与 BERT 同源（都是 模型级 → 每层 → DenseWeight），但多了三样东西：

1. **embedding 表**：word embedding（`pre_decoder_embedding_table`）、位置编码（`position_encoding_table`）、输出投影（`post_decoder_embedding`，常与 word embedding 共享权重）。
2. **首尾 LayerNorm**：`pre_decoder_layernorm` / `post_decoder_layernorm`（是否启用取决于 GPT 变体）。
3. **prompt learning 表**：用于 p-tuning / prefix-tuning 等可控生成（`prompt_learning_table`）。

每层权重 `ParallelGptDecoderLayerWeight` 与 BERT 的层结构基本一致（pre-LN + self-attention + FFN），但增加了 `gpt_variant_params`（控制激活类型、是否用 RoPE、是否有 adapter 等，详见 u6-l4）和对应的 adapter 权重。

#### 4.5.2 核心流程：两种 buffer 组织风格

对比 BERT 与 GPT 的 buffer 组织，能看出 FT 内部有两种风格并存：

- **BERT 风格**（`unordered_map<string, FtWeight>`）：用文件名作 key，灵活、可读，适合层数/权重固定且数量适中的模型。
- **GPT 风格**（固定大小的 `std::vector<T*>`）：用一个固定容量的指针数组（如 `weights_ptr = std::vector<T*>(20, nullptr)`），按下标访问，适合需要支持 int8/weight-only/稀疏**多套并列权重**的场景——每种量化形态各占一组下标。

两者本质相同（都是「分配-绑定-释放」），只是 key 从「字符串」换成了「下标」，换取更紧凑的内存和更快的查找。

#### 4.5.3 源码精读

**模型级 `ParallelGptWeight`**（[ParallelGptWeight.h:L28-L117](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGptWeight.h#L28-L117)）。核心成员：

```cpp
std::vector<ParallelGptDecoderLayerWeight<T>*> decoder_layer_weights;  // 每层（注意是指针向量）
const T*                                       position_encoding_table     = nullptr;
const T*                                       pre_decoder_embedding_table = nullptr;
LayerNormWeight<T>                             pre_decoder_layernorm;
LayerNormWeight<T>                             post_decoder_layernorm;
DenseWeight<T>                                 post_decoder_embedding;     // 输出投影
std::vector<std::pair<const T*, int>>          prompt_learning_table = {}; // prompt learning
```

注意 GPT 的每层是**指针**向量 `vector<ParallelGptDecoderLayerWeight<T>*>`（[L54](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGptWeight.h#L54)），而 BERT 是**值**向量 `vector<BertLayerWeight<T>>`——GPT 用指针是为了支持运行期 `resizeLayer`（[L49](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGptWeight.h#L49)）动态调整层数。

文件里 [L107-L116](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGptWeight.h#L107-L116) 的注释还列出了 GPT「基础权重」的数量与可选项（位置编码、首尾 LayerNorm 是否存在因变体而异），`num_base_weights = 7` 是默认上限。

**每层 `ParallelGptDecoderLayerWeight`**（[ParallelGptDecoderLayerWeight.h:L53-L111](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGptDecoderLayerWeight.h#L53-L111)）。成员结构是 BERT 层的「超集」：

```cpp
LayerNormWeight<T> pre_layernorm_weights;          // pre-LN（GPT 默认前置）
AttentionWeight<T> self_attention_weights;         // 与 BERT 同构
LayerNormWeight<T> self_attn_layernorm_weights;
FfnWeight<T>       ffn_weights;                    // 与 BERT 同构
FfnWeight<T>       after_attention_adapter_weights; // detoxification 等 adapter
FfnWeight<T>       after_ffn_adapter_weights;
```

它持有的多套并列 buffer 体现了 GPT 风格（[L97-L104](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGptDecoderLayerWeight.h#L97-L104)）：

```cpp
std::vector<T*> weights_ptr        = std::vector<T*>(20, nullptr);  // FP16/BF16 主权重
std::vector<int8_t*> int8_weights_ptr      = std::vector<int8_t*>(8, nullptr);  // INT8
std::vector<T*>      weight_only_scale_ptr = std::vector<T*>(8, nullptr);       // weight-only scale
std::vector<float*>  scale_ptr / scale_out_ptr / scale_inter_ptr;               // INT8 scales
```

> 结论：从 `DenseWeight` 到 `ParallelGptDecoderLayerWeight`，抽象层次与 BERT 完全一致；GPT 多出来的是 embedding/位置编码/prompt learning（模型级）和 adapter/变体参数（每层），以及为多精度并列存储而改用的「下标数组」buffer 风格。详细的 GPT 推理流程见 u6-l1。

#### 4.5.4 代码实践

**实践目标**：对比 BERT 与 GPT 的权重树，找出「同构部分」与「GPT 独有部分」。

**操作步骤**：

1. 列出 `BertLayerWeight` 与 `ParallelGptDecoderLayerWeight` 各自的成员，圈出同构的 `AttentionWeight` + `FfnWeight` + `LayerNormWeight`。
2. 标出 GPT 独有项：pre-layernorm 位置差异、adapter 权重、`gptVariantParams`。

**预期结果**：

| 维度 | BERT 层 | GPT 层 |
| --- | --- | --- |
| attention | self-attn（同构） | self-attn（同构） |
| FFN | intermediate+output（同构） | intermediate+output（同构） |
| LayerNorm 位置 | post-LN（默认） | pre-LN（默认，由 `layernorm_type` 控制） |
| adapter | 无 | `after_attention_adapter` / `after_ffn_adapter` |
| 多精度并列 buffer | map 风格 | 下标数组风格（含 int8 / weight-only / scales） |

#### 4.5.5 小练习与答案

**练习 1**：GPT 的 `post_decoder_embedding` 是什么？它和 `pre_decoder_embedding_table` 通常是什么关系？

> **参考答案**：`post_decoder_embedding` 是输出投影（把最后一层隐状态映射回词表 logits）的 `DenseWeight`；在很多 GPT 模型里它与输入 `pre_decoder_embedding_table` **权重共享（tied embedding）**，由 `shared_embed_` 标志控制（[ParallelGptWeight.h:L93](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGptWeight.h#L93)）。

**练习 2**：为什么 GPT 的每层用 `vector<ParallelGptDecoderLayerWeight<T>*>`（指针），而 BERT 用 `vector<BertLayerWeight<T>>`（值）？

> **参考答案**：GPT 提供了 `resizeLayer`（[L49](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGptWeight.h#L49)）以支持运行期调整层数，用指针可以单独 new/delete 某一层而不搬动整个 vector；BERT 层数固定，用值语义更简单、缓存更友好。

---

## 5. 综合实践

把本讲知识串起来，完成一个**「权重侦探」**任务：

**场景**：你拿到一个转换好的 BERT-base 模型权重目录，需要在 `tensor_para_size=2`、`pipeline_para_size=1` 下部署。

**任务**：

1. **画树**：画出 `BertWeight` → 2 路（`bert_layer_weights[12]` + `post_transformer_layernorm_weights`）→ 每层 4 路（attention/attn-LN/ffn/ffn-LN）→ 每个 `DenseWeight` 的 kernel/bias 的完整组织树。标注哪些节点「拥有显存」（根与每层）、哪些是「借来的视图」（叶子）。
2. **列文件清单**：对 rank 0，列出它需要读取的某一层（如 layer 0）的全部 `.bin` 文件名，并标注每个文件的 shape 与切分模式（列并行/行并行/复制）。提示：直接对照 [BertLayerWeight.cc:L32-L66](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/BertLayerWeight.cc#L32-L66)。
3. **追踪一次 forward 的权重取用**：假设 forward 走到 attention 的 Q 投影 GEMM，追踪 `attention_weights.query_weight.kernel` 这个 `const T*` 是怎么从 `weights_ptr.at("attention.self.query.weight.0.bin").ptr_` 一路绑定过来的（参考 `setWeightPtr`，[BertLayerWeight.cc:L185-L218](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/BertLayerWeight.cc#L185-L218)）。
4. **解释等价性**：用一句话说明，为什么 2 卡 TP 下 rank 0 持有前 6 个 head、rank 1 持有后 6 个 head，最终 attention 输出却和单卡 12 head 数值等价（提示：行并行输出投影 + all-reduce）。

**验收标准**：你能指着组织树上任何一个叶子指针，说出它绑定自哪个 `.bin` 文件、该文件是切分还是复制、以及切分沿哪个维度。

## 6. 本讲小结

- **`DenseWeight` 是最小积木**：一组 `const T*`（kernel、bias 及 INT8/FP8 各类 scale），非拥有、默认 nullptr，是所有线性层权重的统一容器。
- **复合层权重是 `DenseWeight` 的组合**：`AttentionWeight`（4 个）、`FfnWeight`（2 个主干）、`LayerNormWeight`（gamma/beta），全部值语义、无继承。
- **三级指针树**：模型级 Weight 拥有显存 → 每层 LayerWeight 持有 `map`/数组 buffer → 叶子 `DenseWeight` 借指针；遵循「分配-登记-绑定」三步走。
- **TP 切分编码在文件名与 shape 里**：QKV/FFN-intermediate 列并行（`.{rank}.bin`，输出维切 = 切 head，约束 `head_num%TP==0`），attention-output/FFN-output 行并行（bias 完整、需 all-reduce），LayerNorm 复制。
- **PP 切层与 TP 切层内正交**：`isValidLayerParallelId` 按 `pipeline_para_rank` 均分层，`TP×PP==world_size`。
- **GPT 与 BERT 同构**：每层 attention+FFN+LN 结构一致，GPT 多出 embedding/位置编码/prompt learning（模型级）与 adapter/变体参数（每层），并用「下标数组」buffer 支持多精度并列存储。

## 7. 下一步学习建议

- 权重加载好之后，下一步就是 forward 真正使用这些权重做 GEMM——建议进入 **u2-l3（cublasMMWrapper 与 GEMM）** 复习 GEMM 接口，再读 **u3-l3（注意力层）** 和 **u3-l4（FFN 层）** 看 `DenseWeight.kernel` 是怎么被喂给 `cublasMMWrapper` 的。
- 想看 TP 切分在通信层如何落地（列并行不通信、行并行 all-reduce），直接读 **u7-l1（张量并行：NCCL 切分与 all-reduce）**。
- 想了解 INT8/FP8 那些 scale 字段的真实用法，进入 **u9-l1（INT8 量化）** 与 **u9-l3（FP8 推理）**。
- 想看 GPT 模型级权重如何驱动 context/decoder 两阶段 forward，进入 **u6-l1（ParallelGpt 架构）**。

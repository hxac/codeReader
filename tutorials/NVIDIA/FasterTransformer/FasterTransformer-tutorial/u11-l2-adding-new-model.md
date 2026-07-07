# 新增模型指南：从 templates 出发

## 1. 本讲目标

本讲是全册的「输出端」：前面十几讲我们一直在「读懂」FasterTransformer（FT），本讲回答「如何把一个 FT 还不支持的新 transformer 模型加进去」。

学完本讲你应当能够：

- 复述 FT 官方在 `templates/adding_a_new_model/README.md` 中给出的新增模型 **7 步流程**，并理解每一步要交付什么产物。
- 牢记 FT 最核心的贡献原则：**复用现有 layer/kernel 拼出新模型，绝不改动既有模型去迁就新模型**。
- 以 `longformer` 为完整范例，区分「可复用组件」（FFN、layernorm、add_residual）与「必须新建的组件」（专属 attention layer）。
- 知道新模型要落在哪些目录、如何接入 CMake 构建、example 与 guide 放在哪里、PR 要满足哪些编码规范。

本讲对应高级（advanced）阶段，承接 u1-l3（目录结构）、u3-l3（注意力层）、u3-l4（FFN 层）、u4-l1（BERT 模型编排）。建议先确认你已经理解「kernel / layer / model 三层抽象」与「一个 transformer block = 注意力 + FFN + layernorm + 残差」这两件事。

## 2. 前置知识

在动手前，先用通俗语言对齐三个概念：

- **transformer 变体**：绝大多数 NLP/CV 模型（BERT、GPT、Longformer、ViT……）都共享同一个「block 骨架」——注意力 → 残差 → LayerNorm → FFN → 残差 → LayerNorm。所谓「变体」往往只改其中一两个零件（换一种注意力、换一种位置编码、换一种激活）。
- **复用（reuse）vs 新建（new）**：如果新模型的某个零件与现有实现**完全相同**，就直接 `new` 一个现成的类来用；只有**真正不同**的零件才需要新写一个类/一个 kernel。FT 把这条线划得很清楚：注意力层经常不同（要新建），FFN/layernorm/残差几乎都相同（可复用）。
- **三层抽象的位置**：新零件如果是「GPU 上一件并行的事」就放进 `kernels/`（写 `invokeXxx`）；如果是「把 kernel 与 GEMM 组合成一个语义模块」就放进 `layers/`；如果是「把多个 layer 串成完整前向」就放进 `models/`。（这三层抽象详见 u1-l3、u3-l5。）

一句话：新增模型 = 在 `models/<new_model>/` 里，用现成的 FFN/layernorm 拼装，只在必要时去 `layers/` 或 `kernels/` 补一个新零件。

## 3. 本讲源码地图

| 文件 / 目录 | 作用 |
| --- | --- |
| [templates/adding_a_new_model/README.md](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/templates/adding_a_new_model/README.md) | 官方新增模型指南：7 步流程 + 「不要改老模型」原则 + 编码规范 |
| `src/fastertransformer/models/longformer/` | 范例的「模型层」：`LongformerEncoder.h/.cc` + 独立 `CMakeLists.txt` |
| [src/fastertransformer/models/longformer/LongformerEncoder.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/longformer/LongformerEncoder.h) | 模型类与 `LongformerLayerWeight` 权重结构，声明了**复用**的 `GeluFfnLayer` 与**新建**的 `LongformerAttentionLayer` 两个成员 |
| [src/fastertransformer/models/longformer/LongformerEncoder.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/longformer/LongformerEncoder.cc) | 完整前向：6 个 QKV 投影 GEMM → 专属注意力 → 复用的 layernorm → 复用的 FFN |
| [src/fastertransformer/layers/attention_layers/LongformerAttentionLayer.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/LongformerAttentionLayer.h) | 范例的「必须新建」专属注意力层声明 |
| [src/fastertransformer/layers/attention_layers/LongformerAttentionLayer.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/LongformerAttentionLayer.cc) | 滑窗 + 全局注意力的 GEMM 编排与融合 softmax kernel 调用 |
| [src/fastertransformer/kernels/longformer_kernels.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/longformer_kernels.h) | 范例的「专属 kernel」入口（mask shift、索引初始化、融合 softmax） |
| [src/fastertransformer/models/longformer/CMakeLists.txt](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/longformer/CMakeLists.txt) | 模型如何接入构建：链接现成的 `FfnLayer`/`layernorm_kernels` 等 |

> 说明：README 在第 4 步说模型文件「can be `Longformer`」，而真实仓库里它叫 `LongformerEncoder`（因为只实现了 encoder 部分）。这种「文档示例名」与「实际类名」的差异是正常的，本讲一律以真实源码为准。

## 4. 核心概念与源码讲解

### 4.1 templates 的 7 步流程：新增模型的贡献流水线

#### 4.1.1 概念说明

`templates/adding_a_new_model/README.md` 是 FT 给贡献者的「施工图」。它把「加一个新 transformer 模型」拆成 7 个有明确交付物的步骤，并反复用一个真实范例（Longformer）演示。掌握这 7 步，你就掌握了 FT 所有现存模型（ViT、Swin、GPT-J……）当初被加进来时遵循的同一套套路。

#### 4.1.2 核心流程

7 步可以归纳为「先比对、再新建零件、再组装、再验证、再交付」：

```
1. 对比架构   → 找出与已有模型相同的零件（FFN/layernorm）和不同的零件（通常是 attention）
2. 建目录     → src/fastertransformer/models/<new_model>/
3. 写新零件   → 不同的组件放进 layers/ 或 kernels/（如专属 attention layer）
4. 组装模型   → 在 models/<new_model>/ 里把零件串成完整 forward
5. 写 example → examples/{cpp,pytorch,tensorflow}/<new_model>/，让别人能验证正确性
6. 写 guide   → docs/<new_model>_guide.md，讲用法 + 给 benchmark
7. 提 PR      → 发起 review
```

第 3 步内嵌一条最重要的告诫（README 用 BERT vs Encoder 举例）：**相似但不同的模型，要新建一个类去复用零件，而不是改老模型去迁就新模型。**

#### 4.1.3 源码精读

7 步原文（含那条关键告诫）在：

[templates/adding_a_new_model/README.md:L10-L17](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/templates/adding_a_new_model/README.md#L10-L17) —— 完整 7 步流程，第 13 行的子条目就是「不要改 `Bert` 去适配 `Encoder`，而应复用 attention/FFN/layernorm 新建 `Encoder` 类」。

第 5 步对 example 的位置与形式有明确要求：

[templates/adding_a_new_model/README.md:L15-L15](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/templates/adding_a_new_model/README.md#L15) —— example 可放 `examples/cpp/<model>`、`examples/pytorch/<model>`、`examples/tensorflow/<model>`，且「其他用户能用它检查正确性」是硬要求。Longformer 实际落在了 `examples/pytorch/longformer/`（`longformer_qa.py`、`model.py`）。

> 提示：这份 README 还包含「How to optimize some kernels?」（[L23-L25](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/templates/adding_a_new_model/README.md#L23-L25)）和「Coding style」两节，后者会在 4.5 节展开。

#### 4.1.4 代码实践

**实践目标**：把 7 步流程与真实文件一一对应，建立「步骤 → 产物」的肌肉记忆。

**操作步骤**：

1. 打开本讲的「源码地图」表格，对照 Longformer 的真实文件。
2. 填写下面这张映射表（在心里或笔记上完成）：

| 7 步 | Longformer 的真实产物 |
| --- | --- |
| 1. 对比架构 | （自行判断：哪些与 BERT 相同？） |
| 2. 建目录 | `src/fastertransformer/models/longformer/` |
| 3. 写新零件 | `layers/attention_layers/LongformerAttentionLayer.{h,cc}` + `kernels/longformer_kernels.{h,cu}` |
| 4. 组装模型 | （自行填写） |
| 5. 写 example | （自行填写：见源码地图上方的提示） |
| 6. 写 guide | `docs/longformer_guide.md` |
| 7. 提 PR | （历史 PR，不在仓库内） |

**需要观察的现象**：你会发现 Longformer **没有**独立的 `examples/cpp/longformer/`，只提供了 PyTorch example——这与 README「cpp/tensorflow/pytorch 三选一即可」一致，说明 example 不必三种语言都写。

**预期结果**：能说出第 1 步结论是「FFN、layernorm、add_residual 与 BERT 相同可复用；attention 是滑窗+全局结构，必须新建」；第 4 步产物是 `LongformerEncoder.cc`。无法本地编译时标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：README 第 6 步要求 guide 里包含什么？
**答案**：包含「如何使用代码」的说明，以及 benchmark 性能数据（Longformer 的 `docs/longformer_guide.md` 就有 FP32/FP16 两节 Performance 表格）。

**练习 2**：为什么第 3 步强调「不要改老模型」？
**答案**：改动既有模型会破坏已有用户的正确性与性能回归基线；新建一个类、复用底层零件，既共享优化成果又不污染已验证路径（详见 4.2）。

---

### 4.2 复用 vs 新建：FT 新增模型的核心原则

#### 4.2.1 概念说明

7 步流程里最容易被新手做错的是第 1、3 步的判断：**到底哪些零件复用、哪些新建？** FT 的回答非常工程化——用「是否数学等价」来划线。一个 block 里，FFN、LayerNorm、加偏置+残差这些 elementwise/规约运算，几乎所有 transformer 变体都长得一样，于是 FT 把它们做成可复用的 layer/kernel；而注意力因 mask 结构、窗口划分、位置编码不同，几乎每个长序列模型都要单独写。

#### 4.2.2 核心流程

判断一个新模型零件归属的决策树：

```
该零件的数学定义与 FT 现有实现是否完全一致？
├─ 是 → 复用：在 model 构造里 new 一个现成类（如 GeluFfnLayer），直接 forward
└─ 否 → 该零件能否用「现有 kernel + GEMM」拼出来？
        ├─ 能 → 新建一个 layer 类，内部调用现成 kernel 与 cublas GEMM 编排
        └─ 不能 → 还要新写专属 kernel（放进 kernels/，命名 invokeXxx）
```

注意：新建 layer **不等于**重写所有 kernel。Longformer 的专属注意力层（4.3 节）大量调用现成的 `cublas_wrapper_->stridedBatchedGemm`，只把「滑窗 + 全局 softmax」这一步无可替代的部分写成专属 kernel。

#### 4.2.3 源码精读

这条「复用」原则在模型层的直接体现，是 `LongformerEncoder` 同时持有两个指针——一个新建、一个复用：

[src/fastertransformer/models/longformer/LongformerEncoder.h:L70-L73](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/longformer/LongformerEncoder.h#L70-L73) —— `GeluFfnLayer<T>*` 是**复用**（u3-l4 讲过的现成 FFN），`LongformerAttentionLayer<T>*` 是**新建**，`weights_` 用 `std::vector<LongformerLayerWeight<T>>` 承载每层权重（权重组织承接 u2-l5）。

构造函数里两者的创建方式完全对称，凸显「复用就是直接 new 现成类」：

[src/fastertransformer/models/longformer/LongformerEncoder.cc:L59-L81](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/longformer/LongformerEncoder.cc#L59-L81) —— 先 `new LongformerAttentionLayer<T>(...)`，再 `new GeluFfnLayer<T>(...)`，后者参数里 `0, // expert_num` 表明它就是 u3-l4 那个支持 MoE 的 FFN 的非 MoE 用法。

而 README 里用 BERT vs Encoder 举的反例（LayerNorm 位置不同），正是这条原则的「反面教材」：

[templates/adding_a_new_model/README.md:L13-L13](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/templates/adding_a_new_model/README.md#L13) —— 应「reuse the attention layer, feed forward network layer and layer normalization kernel to create a new class」，而不是改 `Bert` 去适配 `Encoder`。

#### 4.2.4 代码实践

**实践目标**：在真实前向里数清楚「哪几行是复用、哪几行是新建」。

**操作步骤**：

1. 打开 `LongformerEncoder.cc` 的 `forwardLayer`（[L184-L368](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/longformer/LongformerEncoder.cc#L184-L368)）。
2. 给下面每行标注「复用 / 新建」：

   - L202-L279：6 个 QKV 投影 GEMM →（用现成 `cublas_wrapper_->Gemm`，算复用 GEMM，但 6 投影结构是新模型的）
   - L318：`longformer_attn_layer_->forward(...)` →（新建专属注意力层）
   - L337-L345：`invokeAddBiasResidualLayerNorm(...)` →（复用通用 kernel，与 BERT 同款）
   - L349-L354：`inter_gelu_out_ffn_->forward(...)` →（复用 `GeluFfnLayer`）
   - L358-L366：第二次 `invokeAddBiasResidualLayerNorm(...)` →（复用）

**需要观察的现象**：一个 Longformer block 里，「注意力相关」只占少数几行（因为复杂度被封装进专属层），而「残差+LN」与「FFN」用的是与 BERT 完全相同的代码路径。

**预期结果**：得到结论——Longformer 相对 BERT 的「增量」几乎全在 `LongformerAttentionLayer` 与 `longformer_kernels`，其余都是零成本复用。这正是 FT 设计的目标。若无法运行，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：假如新模型的 FFN 把 GeLU 换成了 SiLU，需要新建 FFN 类吗？
**答案**：不需要。按 u3-l4，FT 的 FFN 用模板方法模式，激活由 `getActivationType()` 决定，已有 `TensorParallelSiluFfnLayer` 等现成子类；直接复用对应激活的 FFN 子类即可。

**练习 2**：`LongformerEncoder` 为何不自己实现一个 FFN，而要 `new GeluFfnLayer`？
**答案**：因为 FFN 的数学定义与 BERT 相同（两段 GEMM + GeLU），复用既能享受已有的 buffer 复用与模板特化优化，又能避免重复代码带来的维护与回归风险。

---

### 4.3 Longformer 范例（一）：必须新建的专属 attention layer

#### 4.3.1 概念说明

Longformer 处理超长文档，标准 self-attention 的 \(O(L^2)\) 复杂度无法承受。它的注意力是「**滑窗局部注意力 + 少量全局注意力**」的混合：大部分 token 只看周围一个窗口内的 token，少数「全局 token」看整句。这与 BERT 的「每个 token 看全场」完全不同，因此 BERT 的 `UnfusedAttentionLayer`/`FusedAttentionLayer`（u3-l3）都不能直接用——必须新建 `LongformerAttentionLayer`。

#### 4.3.2 核心流程

专属注意力层内部仍遵循「\(QK^\top\) → softmax → \(PV\)」三段式，但 \(QK^\top\) 被拆成「窗口内」与「全局」两类 GEMM，softmax 被融进一个专属 kernel：

\[
\text{score}_{ij}=\frac{Q_i K_j^\top}{\sqrt{d}},\quad
\alpha_{ij}=\frac{\exp(\text{score}_{ij}+m_{ij})}{\sum_k \exp(\text{score}_{ik}+m_{ik})},\quad
O_i=\sum_j \alpha_{ij} V_j
\]

其中 \(m_{ij}\) 是掩码偏置（local/global mask 决定哪些 \(j\) 参与求和）。层的主流程：

```
allocateBuffer()
# ---- 计算 QK^T（多段 stridedBatchedGemm）----
local  attention: head 段 / middle 段 / tail 段（按窗口切片）
global attention: 局部 token 看全局 token / 全局 token 看全场
# ---- 融合 softmax（专属 kernel）----
invokeLongformerMHASoftmax(...)   ← 唯一无可替代的专属 kernel
# ---- 计算 PV（多段 stridedBatchedGemm）----
local/global 的反向 GEMM，写回 output
freeBuffer()（若 is_free_buffer_after_forward_）
```

#### 4.3.3 源码精读

新建层首先要继承 `BaseLayer`（u3-l5 讲过的 buffer 生命周期基类），并遵守它的 `forward(std::vector<Tensor>*, ...)` 接口约定：

[src/fastertransformer/layers/attention_layers/LongformerAttentionLayer.h:L24-L59](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/LongformerAttentionLayer.h#L24-L59) —— 类继承 `BaseLayer`，私有成员 `head_num_/local_attn_window_size_/max_global_token_num_` 等是 Longformer 专属参数；`allocateBuffer/freeBuffer` override 基类纯虚函数；`forward` 用旧式 `std::vector<Tensor>` 接口（与 u3-l3 提到的 BERT 同代接口一致）。

构造函数里有 FT 常见的「参数合法性校验」模式（`FT_CHECK`），这是新建层必须做的：

[src/fastertransformer/layers/attention_layers/LongformerAttentionLayer.cc:L50-L51](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/LongformerAttentionLayer.cc#L50-L51) —— `FT_CHECK(max_global_token_num_ <= local_attn_window_size_)`，把 `docs/longformer_guide.md` 的「Notes」约束直接编进代码（guide 第 16 行的 `max_global_token_num < local_attn_window_size`）。

forward 里 local attention 的第一段 QK GEMM（窗口内 \(QK^\top\)）：

[src/fastertransformer/layers/attention_layers/LongformerAttentionLayer.cc:L142-L156](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/LongformerAttentionLayer.cc#L142-L156) —— 注意它**复用** `cublas_wrapper_->stridedBatchedGemm`，并没有自创矩阵乘 kernel；新建层 = 新建编排，而非重造轮子。

无可替代的专属 softmax kernel 调用：

[src/fastertransformer/layers/attention_layers/LongformerAttentionLayer.cc:L259-L270](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/LongformerAttentionLayer.cc#L259-L270) —— `invokeLongformerMHASoftmax` 把 local mask、global mask、global 索引全部揉进一次 softmax，这是普通注意力层做不到的，所以必须放进 `kernels/longformer_kernels.h`（见 [L37-L48](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/longformer_kernels.h#L37-L48)）。

文件末尾的模板显式实例化是 FT 全库统一的「枚举→模板」dispatch 落点（承接 u1-l4、u2-l1）：

[src/fastertransformer/layers/attention_layers/LongformerAttentionLayer.cc:L427-L431](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/LongformerAttentionLayer.cc#L427-L431) —— 实例化 `float`/`half`，BF16 受 `#ifdef ENABLE_BF16` 守卫（CUDA 版本驱动条件编译，见 u1-l2）。

#### 4.3.4 代码实践

**实践目标**：通过阅读测试断言理解专属注意力层的输入约定。

**操作步骤**：

1. 打开 `tests/longformer/py_longformer_unit_test.py`（仓库内存在该文件）。
2. 找到它构造 `local_attn_mask`、`global_attn_mask` 与 `global_tokens` 的部分，记录每个张量的形状与取值含义（local mask：0/1；global mask：-10000.0/0.0，见 `LongformerEncoder.cc` forward 注释 [L143-L147](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/longformer/LongformerEncoder.cc#L143-L147)）。
3. 对照 [LongformerAttentionLayer.cc:L78-L89](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/LongformerAttentionLayer.cc#L78-L89)，确认 input_tensors 的 10 个槽位（q/k/v/qg/kg/vg/local_mask/global_mask/global_idx/global_token_nums）与测试构造的张量一一对应。

**需要观察的现象**：该测试把 FT 的输出与 HuggingFace Longformer 的输出逐元素比较（guide 第 8 行声明「aligned with Huggingface Longformer」），这是 README 第 5 步「验证正确性」的标准做法。

**预期结果**：能说出第 0~5 号输入是 6 个投影后的 QKV 张量、第 6~7 号是两种 mask、第 8~9 号是全局索引表。该测试依赖 `BUILD_PYT` 编译出的 `.so`，本地若未编译标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `LongformerAttentionLayer` 继承 `BaseLayer` 而不是某个现成的注意力层？
**答案**：因为它的注意力语义（滑窗+全局）与现成 `UnfusedAttentionLayer`/`FusedAttentionLayer` 不兼容，无法继承复用；但 buffer 生命周期（allocate/free）约定是通用的，所以继承 `BaseLayer` 拿到这套基础设施（u3-l5）。

**练习 2**：专属层里 `cudaMemcpyAsync(... global_token_nums ...)` 把一个数组拷回 CPU（[L95-L96](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/LongformerAttentionLayer.cc#L95-L96)）用来做什么？
**答案**：每个 batch 的全局 token 数运行期才知道，CPU 循环里据此决定 global attention GEMM 的 M/K 维（[L210-L257](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/LongformerAttentionLayer.cc#L210-L257)），用 `cudaEventSynchronize` 等这次拷贝完成——这是该层唯一的 CPU↔GPU 同步点。

---

### 4.4 Longformer 范例（二）：用复用组件编排出完整模型

#### 4.4.1 概念说明

写好专属注意力层后，第 4 步「组装模型」其实最简单：在 `models/longformer/` 里写一个 `LongformerEncoder` 类，把「QKV 投影 GEMM → 专属注意力 → layernorm → FFN → layernorm」按 block 顺序串起来，外层套一个 `for (layer)` 循环。这一步的精髓在于：**模型类本身几乎不写 GPU 逻辑，它只是个「编排者」**，真正的计算都委托给复用的 layer/kernel。

#### 4.4.2 核心流程

一个标准 transformer block 在 `forwardLayer` 里的编排（与 u4-l1 的 BERT、u3-l3/u3-l4 高度同构）：

```
forwardLayer(input, output, masks, idx, weight, ...):
  1. 6 个 QKV 投影 GEMM（q/k/v/kg/vg 各一个 + qg 一个 batched）      # cublas
  2. invokeAddBiasTransposeToMultiHead                                 # 复用 kernel
  3. longformer_attn_layer_->forward(...)                              # 专属层（4.3）
  4. invokeTransposeMultiHeadToSingle                                  # 复用 kernel
  5. attention 输出投影 GEMM                                            # cublas
  6. invokeAddBiasResidualLayerNorm                                    # 复用 kernel
  7. inter_gelu_out_ffn_->forward(...)                                 # 复用 FFN（u3-l4）
  8. invokeAddBiasResidualLayerNorm                                    # 复用 kernel

forward(output_tensors, input_tensors):
  allocateBuffer()
  invokeInitLongformerIdx(...)         # 专属 kernel：算全局索引
  invokeLocalAttnMaskShift(...)        # 专属 kernel：mask 预处理
  for i in layers_num_: forwardLayer(...)
  freeBuffer()
```

#### 4.4.3 源码精读

模型级 forward 的「预处理 + 逐层循环」骨架：

[src/fastertransformer/models/longformer/LongformerEncoder.cc:L150-L176](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/longformer/LongformerEncoder.cc#L150-L176) —— 先调两个专属 kernel 准备索引与 mask，再 `for (i = 0; i < layers_num_; i++)` 反复调用 `forwardLayer`；两块 buffer 原地流式覆写（`i==0` 吃原始输入、之后吃上一层的 `output`），与 u4-l1 BERT 的主循环同构。

`forwardLayer` 里「复用 FFN」的调用，是整段最能体现编排者角色的代码——它把 attention 输出包成 `TensorMap` 直接交给现成 FFN：

[src/fastertransformer/models/longformer/LongformerEncoder.cc:L349-L354](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/longformer/LongformerEncoder.cc#L349-L354) —— 构造 `TensorMap({"ffn_input", ...})` 与 `{"ffn_output", ...}`，调 `inter_gelu_out_ffn_->forward(&output_tensors, &attn_out_tensors, &(weight->ffn_weights))`。注意这里用的是新式 `TensorMap` 接口（u2-l1），而同文件的 attention 仍用旧式 `std::vector<Tensor>`——同一个模型里两种接口并存是 FT 演进期的正常现象。

两次残差+LayerNorm 都复用同一个通用 kernel（与 BERT/Decoder 完全同款）：

[src/fastertransformer/models/longformer/LongformerEncoder.cc:L337-L345](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/longformer/LongformerEncoder.cc#L337-L345) —— `invokeAddBiasResidualLayerNorm` 把「加偏置 + 残差 + LayerNorm」三合一融进一个 kernel（u3-l1 讲过的融合哲学）。

模型类也遵守 `is_allocate_buffer_` 守卫的幂等 buffer 约定（u3-l5）：

[src/fastertransformer/models/longformer/LongformerEncoder.cc:L92-L113](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/longformer/LongformerEncoder.cc#L92-L113) —— `allocateBuffer` 用 `if (!is_allocate_buffer_)` 守卫，每块 buffer 走 `allocator_->reMalloc(ptr, bytes, false)`（REUSE 语义，承接 u2-l2）。

#### 4.4.4 代码实践

**实践目标**：把 `LongformerEncoder` 与 `Bert`（u4-l1）的 block 编排做差异对比，亲眼看「复用」占多大比重。

**操作步骤**：

1. 并排打开 [LongformerEncoder.cc:L184-L368](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/longformer/LongformerEncoder.cc#L184-L368) 与 `src/fastertransformer/models/bert/Bert.cc` 的单层 forward。
2. 列出两者**相同**的调用：`invokeAddBiasResidualLayerNorm`、`GeluFfnLayer::forward`、attention 输出投影 GEMM。
3. 列出两者**不同**的调用：Longformer 用 `LongformerAttentionLayer` + 6 投影 GEMM；BERT 用 `FusedAttentionLayer`/`UnfusedAttentionLayer` + batched QKV。

**需要观察的现象**：除「注意力层」与「QKV 投影方式」外，两个模型的 block 几乎逐行相同。

**预期结果**：写出一句结论——「新增 Longformer 的模型层代码量，约等于『写一个新的 forwardLayer 编排』+『复用 FFN/LN』，真正新增的 GPU 逻辑只在专属注意力层里」。若没有 GPU 环境跑 BERT 对照，标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`forwardLayer` 里 attention 用 `std::vector<Tensor>`，FFN 用 `TensorMap`，为什么没统一？
**答案**：这是 FT 接口演进的历史痕迹（u2-l1、u3-l3）。新代码倾向 `TensorMap`（按名字索引、可插可选参数），旧代码保留 `std::vector<Tensor>`。新增模型时新零件优先用 `TensorMap`，但与旧 layer 交互时仍要适配其原接口。

**练习 2**：`LongformerEncoder` 的 `forward` 在层循环前后各做了一次什么专属预处理？
**答案**：循环前调 `invokeInitLongformerIdx`（算 global_idx、global_token_nums）与 `invokeLocalAttnMaskShift`（local mask 移位）；循环本身对 mask/索引无感知，每个 block 直接复用预处理结果（[L154-L163](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/longformer/LongformerEncoder.cc#L154-L163)）。

---

### 4.5 编码风格、构建接入与 PR 要求

#### 4.5.1 概念说明

代码写对还不够，FT 的 review 还要求「命名规范 + 接入构建 + example/guide 齐全」。命名规范在 README 的 Coding style 一节；构建接入靠每个目录的 `CMakeLists.txt`；这些都做好了才能进入第 7 步提 PR。本模块把这些「工程纪律」集中讲清。

#### 4.5.2 核心流程

新增一个模型零件的工程闭环：

```
1. 命名：单类文件用大驼峰（LongformerAttentionLayer.cc）；
        工具/多类文件用小写下划线（longformer_kernels.cu）；
        函数小驼峰（invokeLongformerMHASoftmax）；变量小写下划线（local_attn_window_size_）。
2. 接入构建：在 kernels/ 或 layers/attention_layers/ 的 CMakeLists.txt 里 add_library；
            在 models/<model>/CMakeLists.txt 里 target_link_libraries 把复用库链进来。
3. （可选）框架外壳：th_op/<model>/ 下加 Op，把模型包成 torch 自定义 op。
4. example + guide + 测试齐全后提 PR。
```

#### 4.5.3 源码精读

命名规范原文：

[templates/adding_a_new_model/README.md:L46-L56](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/templates/adding_a_new_model/README.md#L46-L56) —— 大驼峰只用于「仅含一个类」的文件（如 `BertLayer.cc`），其余小写下划线；函数小驼峰；变量小写下划线。可以对照：`LongformerAttentionLayer.cc`（单类→大驼峰）、`longformer_kernels.cu`（多函数→小写下划线）、`invokeLongformerMHASoftmax`（函数→小驼峰）、`local_attn_window_size_`（成员变量→小写下划线+尾下划线）。

新建零件接入构建——专属 kernel 库：

[src/fastertransformer/kernels/CMakeLists.txt:L179-L181](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/CMakeLists.txt#L179-L181) —— `add_library(longformer_kernels STATIC longformer_kernels.cu)`，设 `POSITION_INDEPENDENT_CODE` 与 `CUDA_RESOLVE_DEVICE_SYMBOLS`（FT 全库 STATIC 库的统一套路）。

专属注意力层库：

[src/fastertransformer/layers/attention_layers/CMakeLists.txt:L32-L35](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/CMakeLists.txt#L32-L35) —— `add_library(LongformerAttentionLayer STATIC ...)`，`target_link_libraries` 链 `cublasMMWrapper` 与 `longformer_kernels`（即「层依赖它专用的 kernel 库」）。

模型库把复用库链进来——这是「复用」在构建层面的落点：

[src/fastertransformer/models/longformer/CMakeLists.txt:L20-L22](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/longformer/CMakeLists.txt#L20-L22) —— `target_link_libraries(LongformerEncoder PUBLIC ... cublasMMWrapper LongformerAttentionLayer longformer_kernels add_bias_transpose_kernels activation_kernels layernorm_kernels FfnLayer ...)`。注意 `FfnLayer`、`layernorm_kernels`、`activation_kernels` 都是**现成库**——模型层在 CMake 里把复用关系显式声明出来。

（可选）PyTorch 外壳：FT 还为 Longformer 配了 `th_op/longformer/LongformerEncoderOp`，把 C++ 模型包成 torch 自定义 op（u10-l1 的三段式封装）：

[src/fastertransformer/th_op/longformer/LongformerEncoderOp.h:L24-L60](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/longformer/LongformerEncoderOp.h#L24-L60) —— `FasterTransformerLongformerEncoder` 继承 `torch::jit::CustomClassHolder`，`forward` 接收 `th::Tensor` 并在内部把权重指针装配进 `ft::LongformerLayerWeight`（`setWeight` 模板函数 [L62-L129](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/longformer/LongformerEncoderOp.h#L62-L129) 用 `offside` 偏移逐层切指针，承接 u2-l5 的「权重即指针树」）。外壳不是新增模型必须的，但若想让 example 用 PyTorch 跑，就需要它。

#### 4.5.4 代码实践

**实践目标**：走一遍「命名 → 构建 → 外壳」的完整接入检查。

**操作步骤**：

1. 对照 [README 编码规范 L46-L56](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/templates/adding_a_new_model/README.md#L46-L56)，给下列 Longformer 符号分类（文件名/函数名/变量名）：
   `LongformerEncoder.h`、`longformer_kernels.h`、`invokeInitLongformerIdx`、`global_token_nums_`、`invokeAddBiasResidualLayerNorm`。
2. 在仓库里找到三处 `CMakeLists.txt`（kernels、attention_layers、models/longformer），确认依赖链：`LongformerEncoder → LongformerAttentionLayer + FfnLayer + ...`，`LongformerAttentionLayer → longformer_kernels`。
3. （可选）确认 `examples/pytorch/longformer/longformer_qa.py` 通过 `torch.classes.load_library` 加载 `LongformerEncoderOp` 来调用模型。

**需要观察的现象**：依赖链是严格自底向上的（kernel → layer → model → th_op），与 u1-l3 的三层抽象完全吻合。

**预期结果**：能画出依赖图 `longformer_kernels → LongformerAttentionLayer → LongformerEncoder → LongformerEncoderOp(.so)`，并说出每一层落在哪个目录。本地未编译时标注「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `LongformerAttentionLayer.cc` 是大驼峰，而 `longformer_kernels.cu` 是小写下划线？
**答案**：前者只含一个类（`LongformerAttentionLayer`），按规范用大驼峰；后者含多个自由函数（`invokeXxx`），不是单类文件，用小写下划线。

**练习 2**：新增模型时，模型库的 `target_link_libraries` 为什么要链 `FfnLayer`、`layernorm_kernels`？
**答案**：因为模型层复用了这些现成组件（4.4 节），CMake 必须把这些库作为依赖链进来，链接期才能解析到 `GeluFfnLayer`、`invokeAddBiasResidualLayerNorm` 等符号。这是「复用」在构建系统里的直接体现。

---

## 5. 综合实践

**任务**：以 Longformer 为范例，按 templates 的 7 步，为「**Reformer**」（另一个长序列 transformer 变体，使用 LSH 注意力与可逆残差）写一份新增模型计划。目标是把本讲 5 个模块串成一份可执行的施工方案。

**要求产出一张完整的 7 步计划表**，每一步都要落到 FT 的真实目录/类名，并明确标注「复用 / 新建」。建议按下面的模板填写：

| 步骤 | 决策与产物 | 复用 / 新建 |
| --- | --- | --- |
| 1. 对比架构 | 列出 Reformer 与 BERT 的相同零件与不同零件 | — |
| 2. 建目录 | `src/fastertransformer/models/reformer/` | 新建 |
| 3. 写新零件 | LSH 注意力层（无标准 \(QK^\top\)）→ 放 `layers/attention_layers/ReformerAttentionLayer.{h,cc}`；LSH 哈希 kernel → 放 `kernels/reformer_kernels.{h,cu}` | 新建 |
| 4. 组装模型 | `Reformer.cc`：QKV 投影（若有）→ 专属注意力 → 复用 layernorm → 复用 FFN；可逆残差需另写专属 kernel | 部分新建 |
| 5. example | `examples/pytorch/reformer/reformer_xxx.py`，对照 HuggingFace Reformer 验正确性 | 新建 |
| 6. guide | `docs/reformer_guide.md`，含用法 + FP16 benchmark | 新建 |
| 7. PR | 提交 review | — |

**关键判断（必答）**：

1. **哪些组件可复用**？答：FFN（`GeluFfnLayer`/`SiluFfnLayer`）、LayerNorm（`invokeAddBiasResidualLayerNorm` 或 `invokeLayerNorm`）、add_residual 等 elementwise kernel——这些与 BERT 数学等价。
2. **哪些必须新建**？答：LSH 注意力层（哈希分桶代替 \(QK^\top\)，无法用现成注意力层）、可逆残差的反向重建逻辑（如有）。参考 Longformer 的做法，新建一个继承 `BaseLayer` 的 `ReformerAttentionLayer`，内部尽量用 `cublas_wrapper_->Gemm`，只把无可替代的「哈希分桶 + softmax」写成专属 `reformer_kernels`。
3. **example 与 guide 放在哪**？答：example 放 `examples/pytorch/reformer/`（或 cpp/tensorflow 任选其一），guide 放 `docs/reformer_guide.md`，并在仓库根 `README.md` 的支持矩阵里登记新模型。

**自检方法**：把你的计划与 Longformer 的真实产物（4.3、4.4 节）逐项对比——如果某个零件你在 Longformer 里找不到对应物，多半就是 Reformer 特有、需要新建的部分。完成后，你就拥有了一份「7 步法 + 复用/新建判断 + 目录/构建/example/guide 落点」齐全的新增模型方案。

> 若想进一步把计划变成代码，建议先从第 3 步的最小子集开始：只实现 `ReformerAttentionLayer` 的 forward 骨架（先复用 GEMM、用占位 softmax），跑通第 5 步 example 的正确性闭环，再回头优化专属 kernel。无法本地编译时，所有「运行」环节标注「待本地验证」。

## 6. 本讲小结

- FT 用 `templates/adding_a_new_model/README.md` 的 **7 步流程**统一所有新模型的贡献方式：对比架构 → 建目录 → 写新零件 → 组装模型 → example → guide → PR。
- 核心原则是**复用而非改动**：相似但不同的模型要新建类复用底层零件，绝不修改既有模型（BERT vs Encoder 反例）。
- 判断线是「数学是否等价」：FFN/LayerNorm/残差几乎都等价→复用；注意力因 mask/窗口/位置编码不同→新建专属层。
- Longformer 完整示范了这条线：新建 `LongformerAttentionLayer` + `longformer_kernels`（滑窗+全局注意力），复用 `GeluFfnLayer`、`invokeAddBiasResidualLayerNorm` 等组件编排出 `LongformerEncoder`。
- 工程纪律：单类文件大驼峰、工具文件小写下划线、函数小驼峰、变量小写下划线；新零件要在对应目录的 `CMakeLists.txt` 里 `add_library`，模型库用 `target_link_libraries` 把复用库链进来。
- 模型类是「编排者」而非「计算者」：`forwardLayer` 用现成 GEMM + 复用 layer/kernel 串成 block，真正新增的 GPU 逻辑集中在专属注意力层。

## 7. 下一步学习建议

- **动手验证**：照综合实践的计划，挑一个真实 transformer 变体（Reformer / BigBird / Linear Transformer）走一遍 7 步，先写 `forward` 骨架跑通 example 正确性，再优化专属 kernel。
- **横向阅读其它「新增模型」范例**：`src/fastertransformer/models/vit/`（ViT，复用 BERT block + 新增 patch embed）、`models/swin/`（新增 window/shift kernel）、`models/gptj/`（新增 rotary kernel），它们都遵循本讲的 7 步与复用原则，对照阅读能加深理解。
- **补齐框架外壳**：若想让新模型在 PyTorch/Triton 下可用，继续读 u10-l1（th_op 三段式封装）与 u10-l3（Triton backend 的 Model/ModelInstance 分层）。
- **回归基础**：如果对模型层复用的 FFN/layernorm/注意力组件还有疑问，回看 u3-l1（核心 kernel）、u3-l3（注意力层）、u3-l4（FFN 层）、u3-l5（BaseLayer buffer 生命周期）。

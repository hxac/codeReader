# u4-l1 FlashAttentionScoreEnhance 前向：变长场景与算子总览

## 1. 本讲目标

本讲是 Attention 算子族的第一讲。学完后你应该能够：

1. 说出 `flash_attention_score_enhance` 前向算子的完整输入输出清单：必选的 query/key/value、可选的 rope 对、多种 mask、sink、varlen 元数据，以及 4 个输出。
2. 对照公式理解 online softmax 为什么必须输出 `softmax_max` 与 `softmax_sum` 两个中间量，以及它们在反向计算中的用途。
3. 通过 `_def.cpp` 与反向算子 `_def.cpp` 的对照，说清前向与反向之间的数据依赖。
4. 理解 `flash_attention_score_enhance_tiling_common.h` 的真实角色——Host 侧共享的平台编译信息结构体，以及它在 tiling 注册链路中的填充与消费方式。

本讲只做「总览」：公式、接口规模、数据依赖。Tiling 切分细节留给 u4-l2，Kernel 模板留给 u4-l3，反向算法留给 u4-l4。

## 2. 前置知识

本讲默认你已学过 u2-l5（aclnn 两段式接口）和 u1-l2（op_def/op_api/op_host/op_kernel 四层模型）。在此之上，补充几个 Attention 领域的通俗概念：

- **Self-Attention（自注意力）**：序列中每个 token 用 query 去和所有 token 的 key 做打分，分数经 softmax 变成权重，再加权求和所有 token 的 value。公式上就是三个矩阵乘加一次 softmax。
- **缩放系数 scale**：\( q \cdot k^T \) 的数值随维度 D 增大而变大，除以 \( \sqrt{D} \)（或等价地乘一个系数）可以防止 softmax 进入饱和区。本算子把它做成可传的 `scale` 属性，默认 1.0。
- **多头与 GQA/MQA**：把 hidden 维切成 N 个 head 各自做注意力。query 的头数 Nq 可以是 key/value 头数 Nkv 的整数倍（Nq/Nkv > 1 即 GQA，分组查询注意力；Nkv=1 即 MQA），多个 query 头共享一组 KV，能省显存。
- **RoPE（旋转位置编码）**：把位置信息「旋转」进向量。在 MLA（Multi-head Latent Attention）这类压缩结构里，rope 部分与主体部分分开放置，所以算子单独提供了 `query_rope`/`key_rope` 两个输入，打分时两部分各算各的再相加。
- **varlen（变长）与 TND 排布**：训练时一个 batch 里各样本序列长度不同。TND 排布把 B 和 S 合轴成 T——所有 batch 的 token 在第 0 维紧密排成一条，再用 `actual_seq_qlen`/`actual_seq_kvlen`（每个 batch 的累加长度）描述边界。例如真实长度 `[2, 2, 2, 2, 2]` 传入的是 `[2, 4, 6, 8, 10]`。
- **稀疏模式 sparse_mode**：不计算完整 \( S_q \times S_{kv} \) 打分矩阵，只算因果下三角（causal）、带状（band）等局部模式，靠 `atten_mask` + `pre_tokens`/`next_tokens` 组合表达。
- **Sink（注意力汇）**：让一个可学习的「sink token」吸收多余的注意力分数，避免 softmax 权重被迫摊到无效位置上。本算子有两种互斥的 sink 机制：`sink` 输入（gptoss 风格，逐 head 标量）与 `sink_num` 属性（Param Sink，在 KV 尾部追加 \( sinkNum \times 64 \) 个 token）。
- **online softmax（在线 softmax）**：FlashAttention 的核心技巧，详见 4.3.1。直觉是：softmax 的分母是整行指数和，朴素做法必须先算完整行；online softmax 把 KV 切块，滚动维护「当前最大值」和「当前指数和」，每来一块就修正一次旧结果，等价于先算完了整行。

## 3. 本讲源码地图

本讲涉及的关键文件（均位于 `ascendc/src/ops-transformer/attention/` 下）：

| 文件 | 作用 |
| --- | --- |
| `flash_attention_score_enhance/docs/npu_flash_attention_score_enhance.md` | torch 侧（`torch.ops.custom.npu_flash_attention_score_enhance`）接口文档：公式、参数、sparse_mode 全表、调用示例 |
| `flash_attention_score_enhance/docs/aclnnFlashAttentionVarLenScoreEnhanceV5.md` | aclnn 侧（`aclnnFlashAttentionVarLenScoreEnhanceV5`）接口文档：两段式原型、逐参数表格、约束、C++ 调用示例 |
| `flash_attention_score_enhance/op_host/flash_attention_score_enhance_def.cpp` | 算子原型注册：18 个输入、4 个输出、14 个属性、双芯片 AICore 配置 |
| `flash_attention_score_enhance/op_host/flash_attention_score_enhance_tiling_common.h` | Host 侧共享的编译期平台信息结构体 `FlashAttentionScoreEnhanceCompileInfo` |
| `flash_attention_score_enhance/op_host/flash_attention_score_enhance_tiling.cpp` | tiling 入口与 `TilingParse` 注册（本讲只看它如何消费 tiling_common.h，切分细节在 u4-l2） |
| `flash_attention_score_enhance/op_api/aclnn_flash_attention_score_enhance.h` | aclnn 两段式接口的 C 声明（对外契约的最终依据） |
| `flash_attention_score_grad_enhance/op_host/flash_attention_score_grad_enhance_def.cpp` | 反向算子原型（只看它的输入清单，用来确证前向→反向数据依赖） |

推荐阅读顺序：两份 docs → `_def.cpp` → 反向 `_def.cpp` 的输入清单 → `tiling_common.h` 与 tiling.cpp 的注册段。

## 4. 核心概念与源码讲解

### 4.1 从两份文档读懂算子：公式与输入输出全貌

#### 4.1.1 概念说明

读一个复杂算子，最快的入口永远是 docs。这个算子有两份文档，分别对应两层接口：

- `npu_flash_attention_score_enhance.md` 面向 PyTorch 用户，描述 `torch.ops.custom.npu_flash_attention_score_enhance`；
- `aclnnFlashAttentionVarLenScoreEnhanceV5.md` 面向 CANN 原生开发者，描述 aclnn 两段式接口。

两份文档给出同一个计算公式：

\[
\text{Attention}=\text{Dropout}(\text{Softmax}(\text{Mask}(scale\cdot(query\cdot key^{T}+queryRope\cdot keyRope^{T})+pse),\ atten\_mask),\ keep\_prob)\cdot value
\]

把它拆成五步，就是一条流水线：

1. **打分**：\( s = scale\cdot(q\cdot k^T + q_{rope}\cdot k_{rope}^T) + pse \)。rope 部分是 MLA 结构专用，不传 rope 时退化为普通的 \( scale\cdot q\cdot k^T + pse \)。
2. **掩码**：`atten_mask`（1 表示该位不参与计算）叠加 sparse_mode 描述的稀疏模式。
3. **online softmax**：分块滚动计算，同时落盘两个中间量 max 与 sum。
4. **dropout**：按 `keep_prob` 随机置零（训练专属，`keep_prob=1` 时跳过）。
5. **加权求和**：乘 value 得到 `attention_out`。

「Enhance（增强）」与「VarLen（变长）」两个词点出了它与标准 FA 算子的差异：本仓库这一版接口只支持 TND 变长排布，并额外融合了 rope、sink、prefix、外切（q_start_idx/kv_start_idx）等盘古 2.0 训练需要的能力。

#### 4.1.2 核心流程

把两份文档的参数表合并，输入可以分成六组：

| 分组 | 参数 | 类型/排布 | 说明 |
| --- | --- | --- | --- |
| 必选三件套 | `query`、`key`、`value` | FP16/BF16/FP32，TND | T = B*S 紧凑合轴 |
| MLA rope 对 | `queryRope`、`keyRope` | BF16，TND | 可选；打分时与主体部分相加 |
| 偏置与掩码 | `realShiftOptional`(pse)、`attenMaskOptional`、`dropMaskOptional`、`paddingMaskOptional` | pse 同 QKV；mask 为 BOOL/UINT8 | pse 是位置编码偏置；drop_mask 是外部传入的 dropout 掩码 |
| varlen 元数据 | `prefixOptional`、`actualSeqQLenOptional`、`actualSeqKvLenOptional`、`qStartIdxOptional`、`kvStartIdxOptional` | INT64 数组 | 描述每个 batch 的长度边界与外切起始索引 |
| 量化缩放 | `d_scale_q`、`d_scale_k`、`d_scale_v` | FLOAT | 配合量化训练的 scale 输入 |
| sink | `sinkOptional` | FLOAT32，[headNum] | 可学习 sink token；与 `sinkNum` 属性互斥 |

标量属性则包括：`headNum`、`inputLayout`（必选）；`scaleValue`(1.0)、`keepProb`(1.0)、`preTokens`/`nextTokens`(INT_MAX)、`innerPrecise`(0)、`sparseMode`(0)、`pseType`(1)、`softmaxOutLayout`("")、`sinkNum`(0) 等（可选，带默认值）。

输出有 4 个（这也是训练 FA 与推理 FA 最大的不同——为反向预留了中间量）：

| 输出 | 含义 | shape（TND 场景） |
| --- | --- | --- |
| `softmaxMaxOut` | online softmax 的行最大值中间结果，FP32 | [N,T,8] 或 [T,N,8]（由 `softmaxOutLayout` 决定） |
| `softmaxSumOut` | online softmax 的行指数和中间结果，FP32 | 同上 |
| `softmaxOutOut` | softmax 概率矩阵 P 本身 | [T,N,Skv] 语义 |
| `attentionOutOut` | 最终输出，与 query 同 dtype/shape | [T,N,D] |

注意末维的「8」：softmax 的 max/sum 本来每行每 head 只有一个标量，这里固定扩成 8 份，是与硬件向量通道对齐的排布约定（具体如何在 kernel 里使用，留到 u4-l3 观察）。

#### 4.1.3 源码精读

公式与产品支持（A2/A3 支持，950PR/950DT 等不支持）：

- [npu_flash_attention_score_enhance.md:L3-L16](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/docs/npu_flash_attention_score_enhance.md#L3-L16)：产品支持表 + 计算公式，这就是 4.1.1 那条公式的出处。
- [aclnnFlashAttentionVarLenScoreEnhanceV5.md:L17-L25](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/docs/aclnnFlashAttentionVarLenScoreEnhanceV5.md#L17-L25)：aclnn 侧的同一公式，并注明「训练场景下，使用 FlashAttention 算法实现 self-attention 的计算」。

两份原型（注意 aclnn 侧是两段式）：

- [npu_flash_attention_score_enhance.md:L20-L23](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/docs/npu_flash_attention_score_enhance.md#L20-L23)：torch 侧原型，返回值标注为 3 个 Tensor。
- [aclnnFlashAttentionVarLenScoreEnhanceV5.md:L30-L73](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/docs/aclnnFlashAttentionVarLenScoreEnhanceV5.md#L30-L73)：两段式原型全文。第一段 `...GetWorkspaceSize` 收 4 个输出张量指针（softmaxMaxOut/softmaxSumOut/softmaxOutOut/attentionOutOut）并产出 executor；第二段只有 workspace/executor/stream 四个参数——这正是 u2-l5 讲过的契约。

关键参数的文档定义：

- [npu_flash_attention_score_enhance.md:L35-L38](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/docs/npu_flash_attention_score_enhance.md#L35-L38)：`atten_mask`（1=不参与计算）与 `sink_tensor`、`query_rope`/`key_rope`（MLA 场景）的定义。
- [aclnnFlashAttentionVarLenScoreEnhanceV5.md:L153-L191](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/docs/aclnnFlashAttentionVarLenScoreEnhanceV5.md#L153-L191)：pse（含 alibi 压缩 shape [B,N,1024,Skv]）、drop_mask、atten_mask、sink（FLOAT32，[headNum]）的表格行。
- [aclnnFlashAttentionVarLenScoreEnhanceV5.md:L203-L241](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/docs/aclnnFlashAttentionVarLenScoreEnhanceV5.md#L203-L241)：`actualSeqQLenOptional`/`actualSeqKvLenOptional`（每个 batch 的 sequence length）与外切索引 `qStartIdxOptional`/`kvStartIdxOptional`。
- [aclnnFlashAttentionVarLenScoreEnhanceV5.md:L343-L381](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/docs/aclnnFlashAttentionVarLenScoreEnhanceV5.md#L343-L381)：`softmaxOutLayout`（"same_as_input" → TND 排布输出；空串 → 默认 NTD）与三个输出张量的 shape/类型表。注意：**表格里没有 `softmaxOutOut` 的行**，但它在 L62 的签名中存在——这是文档滞后于接口的实例，读接口必须回到头文件（见 4.2.3）。

约束速查（详细约束建议直接读文档原文）：

- [aclnnFlashAttentionVarLenScoreEnhanceV5.md:L490-L536](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/docs/aclnnFlashAttentionVarLenScoreEnhanceV5.md#L490-L536)：确定性计算默认开启；T∈[1,1M]、B∈[1,20000]（带 prefix 时 ≤1K）、N∈[1,256]、S∈[1,1M]、D∈[1,768]；GQA/MQA 的 Nq/Nkv 比例约束；sinkNum 的全部互斥条件。
- [npu_flash_attention_score_enhance.md:L58-L135](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/docs/npu_flash_attention_score_enhance.md#L58-L135)：sparse_mode 0~8 全表（0 defaultMask、1 allMask、2 leftUpCausal、3 rightDownCausal、4 band、5/6 prefix 非压缩/压缩、7/8 varlen 外切），及各模式下 `atten_mask` 应传什么形状的矩阵图示（参见文末「参考资源」章节）。

调用示例：

- [npu_flash_attention_score_enhance.md:L189-L291](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/docs/npu_flash_attention_score_enhance.md#L189-L291)：torch_npu 单算子测试：L206-216 是 CPU 参考实现（`softmax(qk + mask*(-10000))` 再乘 value，正是 4.1.1 公式的朴素版），L218-234 是 NPU 调用，L272-287 演示了 5 组 sparse_mode 参数组合的精度对拍。
- [aclnnFlashAttentionVarLenScoreEnhanceV5.md:L539-L780](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/docs/aclnnFlashAttentionVarLenScoreEnhanceV5.md#L539-L780)：C++ 两段式完整示例：L623-633 构造 [T,N,D] 的 shape，L719-736 依次调用第一段、申请 workspace、调用第二段。

#### 4.1.4 代码实践：文档对账

**实践目标**：体会「文档可能滞后，读接口要以头文件为准」，并建立本算子的参数全景表。

**操作步骤**：

1. 打开两份文档，分别统计参数个数：torch 原型（L21）有多少个参数？aclnn 第一段原型（L33-66）有多少个参数和几个输出张量？
2. 做一张三列对账表：`参数名（torch）| 参数名（aclnn）| 在 def.cpp 中的落点（输入还是属性，行号）`。
3. 找茬：对照 [npu_flash_attention_score_enhance.md:L142-L148](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/docs/npu_flash_attention_score_enhance.md#L142-L148)（「输出说明」声称的输出数量）与 aclnn 签名中的 4 个输出张量，指出不一致处。

**需要观察的现象**：

- torch 原型把 aclnn 的多个 INT64 数组参数（actual_seq_qlen 等）收进 python list，把标量参数收进关键字参数；两边参数一一对应但形态不同。
- npu 文档「输出说明」写「共7个输出」，但下方只列出 3 条，且 torch 原型返回值也是 3 个 Tensor；而 aclnn 签名与 `_def.cpp`（4.2 节）都是 **4** 个输出。

**预期结果**：对账表能覆盖全部参数；发现 npu 文档的输出计数与实际不符（疑为笔误），`softmaxOutOut` 在 aclnn 参数表中缺行。结论：**接口的真实契约以 `_def.cpp` 和 aclnn 头文件为准，文档只做导读**。这与 u2-l5 的结论一致。

#### 4.1.5 小练习与答案

**练习 1**：`input_layout="TND"` 时，真实序列长度为 `[4, 6, 0, 8]`（第 3 个 batch 为空），`actual_seq_qlen` 应传什么？

**答案**：传累加和 `[4, 10, 10, 18]`——第 3 个 batch 长度为 0 时累加值不增长，直接复用前一个累加值。依据：[npu_flash_attention_score_enhance.md:L178-L179](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/docs/npu_flash_attention_score_enhance.md#L178-L179)（真实长度 `[2,2,0,2,2]` 传 `[2,4,4,6,8]` 的例子），且此场景不支持 pse 输入。

**练习 2**：`sparse_mode=2` 和 `sparse_mode=3` 有什么区别？各自的参数起点在哪里？

**答案**：两者都是下三角 causal 掩码，区别在三角的参照顶点：2 是 leftUpCausal（以左上顶点划分），3 是 rightDownCausal（以右下顶点划分），适用于 Sq 与 Skv 对齐方式不同的场景。两者都忽略 `pre_tokens`/`next_tokens`。依据：[npu_flash_attention_score_enhance.md:L344-L354](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/docs/npu_flash_attention_score_enhance.md#L344-L354)。

**练习 3**：`sinkNum=3` 时实际传入了多少个 sink token？此时 `sink` 输入还能传吗？

**答案**：\( 3 \times 64 = 192 \) 个。不能——`sinkNum>0` 与 gptoss 风格的 `sinkOptional` 互斥，后者必须传空指针。依据：[aclnnFlashAttentionVarLenScoreEnhanceV5.md:L530-L536](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/docs/aclnnFlashAttentionVarLenScoreEnhanceV5.md#L530-L536)。

### 4.2 `_def.cpp`：18 输入 / 4 输出 / 14 属性的算子骨架

#### 4.2.1 概念说明

u2-l2 已经讲过 `_def.cpp` 的骨架（OpDef 类、Input/Output/Attr 链式声明、OpAICoreConfig、OP_ADD 注册）。本讲换一个角度：把 `_def.cpp` 当作**接口契约的最终事实源**，用它来给 4.1 节的文档对账兜底。

这个文件很长（1138 行），但结构极其规整：前 400 行是 OpDef 级的 18 个输入 + 4 个输出 + 14 个属性声明；中间约 700 行是一份按芯片收窄类型的 `OpAICoreConfig`；最后 20 行是能力开关与注册。**声明的顺序就是运行期的索引**——这一点在 4.4 节会看到实锤（`TilingInputsDataDependency({7, 8, 9, 10, 11})` 直接按下标引用输入）。

#### 4.2.2 核心流程

OpDef 构造函数的声明流水线：

```text
Input("query")  → REQUIRED → DataType(...) → Format(...) → AutoContiguous()
   ↓ 依次声明 18 个输入（3 必选 + 15 可选）
Output("softmax_max" / "softmax_sum" / "softmax_out" / "attention_out") → 全部 REQUIRED
   ↓
Attr(...) × 14（head_num/input_layout 必选，其余带默认值）
   ↓
OpAICoreConfig aicore_config → 逐输入再声明一遍（类型矩阵收窄）
   ↓
能力 Flag × 6 + ExtendCfgInfo × 3
   ↓
AICore().AddConfig("ascend910b") / AddConfig("ascend910_93")
   ↓
OP_ADD(FlashAttentionScoreEnhance)  ← 启动期登记进注册表
```

两个值得注意的类型矩阵细节：

- **OpDef 层的 dtype 列表比 AICore 层宽**：OpDef 层的 query 声明了 22 种类型组合（含 `DT_HIFLOAT8`、`DT_FLOAT8_E5M2`、`DT_FLOAT8_E4M3FN` 三类 FP8），而注册到 ascend910b/ascend910_93 的 `aicore_config` 里 query 只保留 10 种 FP16/BF16/FLOAT 组合。运行时真正可用的是两者的交集——本仓库注册的两款芯片上，文档写的 FLOAT16/BFLOAT16/FLOAT32 就是全集。
- **ValueDepend 的五个输入**：`prefix`、`actual_seq_qlen`、`actual_seq_kvlen`、`q_start_idx`、`kv_start_idx` 这五个 INT64 数组输入额外标了 `.ValueDepend(OPTIONAL)`，意思是**框架要把这些输入的值（而不只是 shape）交给算子**——因为 varlen 场景下 tiling 必须读到每个 batch 的真实长度才能切分。

#### 4.2.3 源码精读

类骨架与必选输入：

- [flash_attention_score_enhance_def.cpp:L20-L41](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_def.cpp#L20-L41)：`class FlashAttentionScoreEnhance : public OpDef` 构造开始；`query` 输入 REQUIRED，22 组 DataType（可数出 FP8 类型出现在 L28-31），FORMAT_ND，`AutoContiguous()` 要求框架保证内存连续。`key`（L42-59）、`value`（L60-77）与 query 完全同构。

可选输入中的关键几组：

- [flash_attention_score_enhance_def.cpp:L126-L141](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_def.cpp#L126-L141)：`atten_mask` OPTIONAL，类型矩阵是 UINT8 与 BOOL 的混合排列（不同类型组合下允许不同的 mask 类型）。
- [flash_attention_score_enhance_def.cpp:L142-L189](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_def.cpp#L142-L189)：`prefix`（L152 出现 `.ValueDepend(OPTIONAL)`）、`actual_seq_qlen`（L168）、`actual_seq_kvlen`（L184）——varlen 元数据三件套，全部 INT64。
- [flash_attention_score_enhance_def.cpp:L270-L321](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_def.cpp#L270-L321)：`query_rope`（L270-287）、`key_rope`（L288-305）与 `sink`（L306-321，纯 FLOAT）三个可选输入——对应公式里的 rope 项和 gptoss sink。

四个输出（全部 REQUIRED）：

- [flash_attention_score_enhance_def.cpp:L322-L381](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_def.cpp#L322-L381)：`softmax_max`（L322-336，FP32）、`softmax_sum`（L337-351，FP32）、`softmax_out`（L352-366，FP16/BF16/FLOAT）、`attention_out`（L367-381，与 query 同构）。这就是 4.1 节「4 个输出」的源码实锤。

14 个属性：

- [flash_attention_score_enhance_def.cpp:L382-L395](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_def.cpp#L382-L395)：`head_num`（L386）与 `input_layout`（L387）是仅有的两个 REQUIRED 属性；`pre_tockens`/`next_tockens` 默认 2147483647（即 INT_MAX，语义是「不设边界」）；`softmax_out_layout` 默认空串（L394）；`sink_num` 默认 0（L395）。注意拼写沿用 CANN 传统的 `tockens`（aclnn 层参数则叫 `preTokens`，两侧命名有历史差异）。

芯片配置与注册：

- [flash_attention_score_enhance_def.cpp:L397-L430](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_def.cpp#L397-L430)：`OpAICoreConfig aicore_config` 开始，`query` 在芯片层收窄为 10 种 FP16/BF16/FLOAT 组合（对照 OpDef 层的 22 种）。
- [flash_attention_score_enhance_def.cpp:L1121-L1129](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_def.cpp#L1121-L1129)：能力开关——动态编译/动态格式/动态维数/动态 shape 全开，`NeedCheckSupportFlag(false)`，`PrecisionReduceFlag(true)`；ExtendCfgInfo 指定 `coreType=AiCore` 与 `jitCompile=static_false,dynamic_false`（走预编译产物，不在运行期 JIT）。
- [flash_attention_score_enhance_def.cpp:L1131-L1136](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_def.cpp#L1131-L1136)：`AddConfig("ascend910b")` 与 `AddConfig("ascend910_93")` 双芯片注册（对应 A2/A3，与文档产品表互证），最后 `OP_ADD(FlashAttentionScoreEnhance)` 完成登记。

aclnn 头文件（接口契约最终依据）：

- [aclnn_flash_attention_score_enhance.h:L24-L56](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_api/aclnn_flash_attention_score_enhance.h#L24-L56)：第一段接口完整签名——10 个张量输入 + 5 个 INT64 数组 + 9 个标量 + 4 个输出张量 + workspaceSize/executor 出参。文档参数表里缺失的 `softmaxOutOut` 在这里 L53 明确存在。
- [aclnn_flash_attention_score_enhance.h:L61-L65](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_api/aclnn_flash_attention_score_enhance.h#L61-L65)：第二段接口只有 4 个参数，标准的 aclnn 两段式。

#### 4.2.4 代码实践：给输入分组标注行号

**实践目标**：不借助文档，仅凭 `_def.cpp` 重建 4.1.2 的输入分组表，并验证「声明顺序即索引」。

**操作步骤**：

1. 在 `_def.cpp` 中 grep `this->Input("` 与 `this->Output("` 与 `this->Attr("`，按出现顺序编号（0 起）。
2. 按编号回答：下标 7、8、9、10、11 分别是哪五个输入？（4.4 节会用到这里）
3. 数一数 `AutoContiguous()` 出现的次数，找出**没有**加 `AutoContiguous()` 的输入，对照 4.2.2 的 ValueDepend 说明解释原因。

**需要观察的现象**：

- 18 个输入按 0 起编号：0=query, 1=key, 2=value, 3=real_shift, 4=drop_mask, 5=padding_mask, 6=atten_mask, 7=prefix, 8=actual_seq_qlen, 9=actual_seq_kvlen, 10=q_start_idx, 11=kv_start_idx, 12=d_scale_q, 13=d_scale_k, 14=d_scale_v, 15=query_rope, 16=key_rope, 17=sink。
- 带 `AutoContiguous()` 的是张量类输入；prefix/actual_seq_qlen/actual_seq_kvlen/q_start_idx/kv_start_idx 这五个 INT64 数组没有 AutoContiguous，但有 `ValueDepend`。

**预期结果**：下标 7~11 恰好是五个 varlen 元数据输入（prefix + 两组长度 + 两组外切索引），与 4.4 节将看到的 `TilingInputsDataDependency({7, 8, 9, 10, 11})` 完全吻合——这不是巧合，是「Input 声明顺序即运行期索引」这条规则的直接体现（u2-l2 讲过，这里看到消费方）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `softmax_max`/`softmax_sum` 的 DataType 是纯 FLOAT（FP32），而 `attention_out` 是 FP16/BF16/FLOAT？

**答案**：max/sum 是给反向算子用的中间量。反向要重建概率矩阵 \( p_{ij} = e^{s_{ij}-m_i}/l_i \)，若 m/l 本身有较大舍入误差，误差会通过指数放大并污染全部梯度，所以必须用高精度的 FP32 保存；而 attention_out 是送给下一层的激活值，与输入 query 同 dtype 即可。

**练习 2**：如果删掉 L1136 的 `OP_ADD(FlashAttentionScoreEnhance);` 会发生什么？

**答案**：构造函数永远不会被调用，算子原型不会登记进 CANN 注册表——op_build 无法为它生成 aclnn 接口，tiling 侧的 `IMPL_OP` 也找不到同名原型，属于 u2-l2 说过的「静默失败」。

**练习 3**：OpDef 层声明了 FP8 类型（DT_HIFLOAT8 等），是否意味着 ascend910b 上能用 FP8 调这个算子？

**答案**：不能。OpDef 层与 OpAICoreConfig 层的类型矩阵是「全集与注册子集」的关系：本仓库为 ascend910b/ascend910_93 注册的 config 只含 FP16/BF16/FLOAT（L397-430），FP8 组合虽在 OpDef 层出现，但没有随任何 `AddConfig` 落到这两款芯片上。可用类型以注册的 config 与文档（FLOAT16/BFLOAT16/FLOAT32）为准。

### 4.3 前向 → 反向的数据依赖：max/sum/out 四件套的去向

#### 4.3.1 概念说明

为什么前向要多输出两个「中间量」？答案藏在 online softmax 里。

朴素 softmax 对一行得分 \( s_{i1},\dots,s_{in} \) 的计算是：

\[
p_{ij} = \frac{e^{s_{ij}}}{\sum_{k} e^{s_{ik}}}
\]

它要求先看到整行。FlashAttention 为了省显存，把 KV 序列切成小块逐块处理，滚动维护两个量：当前行最大值 \( m_i \) 与当前指数和 \( l_i \)。处理第 t 块 \( B_t \) 时：

\[
m_i^{(t)} = \max\left(m_i^{(t-1)},\ \max_{j\in B_t} s_{ij}\right)
\]

\[
l_i^{(t)} = e^{\,m_i^{(t-1)}-m_i^{(t)}}\, l_i^{(t-1)} + \sum_{j\in B_t} e^{\,s_{ij}-m_i^{(t)}}
\]

\[
o_i^{(t)} = e^{\,m_i^{(t-1)}-m_i^{(t)}}\, o_i^{(t-1)} + \sum_{j\in B_t} e^{\,s_{ij}-m_i^{(t)}} v_j
\]

最终输出 \( \text{out}_i = o_i^{(T)} / l_i^{(T)} \)。数学上它与朴素 softmax 完全等价，但任何时刻内存里只需要一个 KV 块。

代价是：**算完之后 \( s_{ij} \) 被丢掉了**，只留下 \( m_i \) 和 \( l_i \)。而反向传播需要概率矩阵 \( P \)（对 V 的加权系数）和对 \( s \) 的梯度链。幸好 P 可以由 \( m_i \)、\( l_i \) 反解：

\[
p_{ij} = \frac{e^{\,s_{ij}-m_i}}{l_i}
\]

所以前向必须把 \( m_i \)（→ `softmax_max`）和 \( l_i \)（→ `softmax_sum`）作为输出保存下来，训练才有反向可算。这就是「训练 FA 算子比推理 FA 算子多输出」的根本原因。

#### 4.3.2 核心流程

前向 4 个输出在反向中的角色：

```text
前向输出                反向输入（flash_attention_score_grad_enhance）   用途
─────────────────────  ────────────────────────────────────────  ─────────────────────────
softmax_max   (m_i) →  softmax_max        反解概率矩阵 P、重建 softmax 链
softmax_sum   (l_i) →  softmax_sum        同上（P 的分母）
softmax_out   (P)   →  softmax_in         直接复用 P，避免重算 exp
attention_out (O)   →  attention_in       参与 dy → dP 的权重复用（等价形式）
```

反向还需要重新拿到前向的输入 query/key/value（以及相同的 mask/pse/varlen 元数据），配合上游传来的损失梯度 `dy`，才能算出对 query/key/value 的梯度。

#### 4.3.3 源码精读

反向算子的输入清单（证据链）：

- [flash_attention_score_grad_enhance_def.cpp:L24-L78](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_host/flash_attention_score_grad_enhance_def.cpp#L24-L78)：反向算子同样以 `query`（L24）、`key`（L42）、`value`（L60）开头，第 4 个输入是 `dy`（L78，上游损失梯度）。
- [flash_attention_score_grad_enhance_def.cpp:L158-L206](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_host/flash_attention_score_grad_enhance_def.cpp#L158-L206)：依次声明 `softmax_max`（L158）、`softmax_sum`（L174）、`softmax_in`（L190）、`attention_in`（L206）——前向的四个输出在这里全部以**输入**身份出现，名字上 `_out` 后缀换成了 `_in` 前缀，一一对应。
- [flash_attention_score_grad_enhance_def.cpp:L222-L418](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_host/flash_attention_score_grad_enhance_def.cpp#L222-L418)：`prefix`/`actual_seq_qlen`/`actual_seq_kvlen`/`q_start_idx`/`kv_start_idx`（L222-286）以及 `query_rope`/`key_rope`/`sink`（L382-418）也与前向对齐——反向必须在与前向完全相同的 varlen/rope/sink 语境下重算打分，mask 语义稍有偏差梯度就是错的。

前向输出声明的对照（回看 4.2.3 的 L322-381）：`softmax_max`/`softmax_sum` 为 FP32，反向侧同名输入同样要求 FP32；`softmax_out`/`attention_out` 的 dtype 组合与反向的 `softmax_in`/`attention_in` 对齐。**dtype 与 shape 的前反向一致性由两侧 def 的类型矩阵互相锁死**。

#### 4.3.4 代码实践：用 torch 复现 online softmax 并验证中间量语义

**实践目标**：在 CPU 上用 torch 实现朴素 softmax 与分块 online softmax 两版 FA 前向，验证三者：(a) 两版输出数值一致；(b) 滚动维护的 m 就是 `softmax_max` 的语义（行最大值）；(c) 滚动维护的 l 就是 `softmax_sum` 的语义（行指数和）。以下是**示例代码**（无需 NPU，纯 CPU 可运行）：

```python
# fa_online_softmax_golden.py  —— 示例代码，仅 CPU 依赖 torch
import torch

torch.manual_seed(0)
Nq, Nkv, D = 6, 6, 16                     # 单 head 小规模
q = torch.randn(1, Nq, D, dtype=torch.float64)
k = torch.randn(1, Nkv, D, dtype=torch.float64)
v = torch.randn(1, Nkv, D, dtype=torch.float64)
scale = 0.25
mask = torch.tril(torch.ones(Nq, Nkv, dtype=torch.bool))    # causal：True=保留

# 1) 朴素参考（对应 npu 文档 L206-216 的 supported_op_exec）
s = scale * (q @ k.transpose(-1, -2))
s = s.masked_fill(~mask, float("-inf"))
p = torch.softmax(s, dim=-1)
o = p @ v

# 2) online softmax：把 KV 切成两块，滚动维护 m / l / o_acc
BLOCK = 3
m     = torch.full((1, Nq), float("-inf"))     # softmax_max 的语义
l     = torch.zeros(1, Nq)                      # softmax_sum 的语义
o_acc = torch.zeros(1, Nq, D)
for st in range(0, Nkv, BLOCK):
    sb = scale * (q @ k[:, st:st+BLOCK].transpose(-1, -2))
    sb = sb.masked_fill(~mask[:, st:st+BLOCK], float("-inf"))
    m_new = torch.maximum(m, sb.max(dim=-1).values)
    alpha = torch.exp(m - m_new)                # 旧结果的折减系数
    pb = torch.exp(sb - m_new.unsqueeze(-1))
    l     = l * alpha + pb.sum(dim=-1)
    o_acc = o_acc * alpha.unsqueeze(-1) + pb @ v[:, st:st+BLOCK]
    m = m_new
o_online = o_acc / l.unsqueeze(-1)

print("max |o - o_online| =", (o - o_online).abs().max().item())
print("m == row-max(s)   :", torch.allclose(m, s.max(dim=-1).values))
print("l == sum(exp(s-m)):", torch.allclose(l, torch.exp(s - m.unsqueeze(-1)).sum(-1)))
```

**操作步骤**：

1. 把脚本存为 `fa_online_softmax_golden.py`，`python fa_online_softmax_golden.py` 运行。
2. 把 BLOCK 从 3 改成 2、1、6（整行一次算完），重复运行。
3. 在第 2 步循环末尾打印每块的 `m`/`l`，观察它们的演化。

**需要观察的现象**：

- 三行校验输出：两版输出差值的最大值（应接近机器精度 0）、`m == row-max(s)` 与 `l == sum(exp(s-m))` 两个 allclose 均为 True。
- 无论 BLOCK 取多少，最终 m/l 不变；块越细，`m` 被修正（增大）的次数越多——这正是「滚动维护」的可视化。

**预期结果**：差值数量级在 1e-15 上下（float64），两个 allclose 为 True。这证明 `softmax_max`/`softmax_sum` 不是凭空设计，而是 online softmax 算法天然要落盘的两个状态量；反向算子拿到它们即可重建 \( p_{ij} \)。具体数值**待本地验证**（不同 torch 版本的浮点细节可能让差值在 1e-15~1e-16 间波动）。

#### 4.3.5 小练习与答案

**练习 1**：如果不保存 `softmax_max`/`softmax_sum`，反向还能算吗？代价是什么？

**答案**：能——反向算子可以重新算一遍前向打分得到完整 \( s_{ij} \)，再整体 softmax。代价是反向阶段额外多一遍 \( q\cdot k^T \) 的全量矩阵乘（显存与计算都翻倍），而保存 m/l 只占 \( O(N \times T) \) 的 FP32 空间，代价远小。这是典型的「用小内存中间量换掉大计算重算」。

**练习 2**：`softmax_out`（P 矩阵）既然可以由 m/l 反解，为什么前向还要输出它、反向还要接收它？

**答案**：反解 P 需要重算 \( s_{ij} \)（还要乘 value 得 dV/dK 中的矩阵乘），直接把 P 落盘能让反向省掉一次 exp 与部分重建步骤，属于实现路线上的「复用优先」。从 grad def L190 的 `softmax_in` 看本仓库选择了复用路线；具体在 kernel 里怎么用，u4-l4 展开。

**练习 3**：为什么反向算子要把 `prefix`/`actual_seq_qlen`/`sink` 这些元数据再收一遍，而不是从前向的输出里「带过来」？

**答案**：aclnn 算子之间没有隐式状态传递，每个算子的输入必须在 def 中显式声明。反向需要在与前向完全相同的 varlen/prefix/sink 语境下重建打分掩码，框架层（如 torch 的 autograd Function）负责把前向的输入与中间量一起转交给反向算子。u6-l2 会看到 csrc 适配层如何手工完成这次转交。

### 4.4 `tiling_common.h`：Host 侧共享的平台编译信息

#### 4.4.1 概念说明

先澄清一个容易望文生义的点：`flash_attention_score_enhance_tiling_common.h` 这个文件名里的 common，指的不是「多种 layout 的公共抽象」，而是**同一算子多个 tiling 实现文件之间共享的一份数据结构**。全文件只有 34 行，核心是一个结构体 `FlashAttentionScoreEnhanceCompileInfo`，字段是清一色的平台参数：AIV/AIC 核数、UB/L1/L0C/L2 缓存大小、socVersion。

它解决的问题是：FA 的 tiling 分散在多个文件里——主入口 `flash_attention_score_enhance_tiling.cpp`、arch32 通用切分 `arch32/flash_attention_score_enhance_tiling_general.cpp`——这些文件都需要读平台信息（核数决定切多少块，UB 大小决定单块上限）。与其每个文件各自探测一遍，不如定义一个共享结构体，由框架在**编译准备期**（TilingParse 阶段）探测一次并缓存，tiling 执行期直接取用。

这正对应 u2-l3 讲过的 TilingContext 平台信息获取，只是 FA 把「探测结果」物化成了显式结构体（经 `TilingParse<FlashAttentionScoreEnhanceCompileInfo>` 注册进框架）。至于多 layout（TND/NTD 输出排布）的 shape 处理，分布在 `flash_attention_score_enhance_tiling.cpp` 与 `flash_attention_score_enhance_infershape.cpp` 中，是 u4-l2 的主题。

#### 4.4.2 核心流程

`FlashAttentionScoreEnhanceCompileInfo` 的生命周期：

```text
编译/加载准备期（每个编译上下文执行一次）
  TilingPrepareForFlashAttentionScoreEnhance(TilingParseContext)
    ├─ GetCoreNumAiv()  → compileInfo->aivNum
    ├─ GetCoreNumAic()  → compileInfo->aicNum
    ├─ GetSocVersion()  → compileInfo->socVersion
    ├─ GetCoreMemSize(UB/L1/L0_C/L2) → ubSize/l1Size/l0cSize/l2CacheSize
    └─ 框架缓存这份 CompileInfo
tiling 执行期（每次算子调用）
  TilingFlashAttentionScoreEnhance(TilingContext)
    ├─ context->GetCompileInfo() → 取回 const FlashAttentionScoreEnhanceCompileInfo*
    ├─ 空输入检查（消费 aivNum 等）
    └─ TilingRegistryNew 责任链 → 各实现按 compileInfo->ubSize/aivNum 决定切分
```

#### 4.4.3 源码精读

结构体定义：

- [flash_attention_score_enhance_tiling_common.h:L24-L32](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_tiling_common.h#L24-L32)：`struct FlashAttentionScoreEnhanceCompileInfo` 的全部 7 个字段——`aivNum`/`aicNum`（向量核/矩阵核数量）、`ubSize`/`l1Size`/`l0cSize`/`l2CacheSize`（四级存储容量）、`socVersion`。加上头部的 include 保护（L16-17）与命名空间 `optiling`（L22），这就是全部内容。

填充与注册（在主 tiling 文件里）：

- [flash_attention_score_enhance_tiling.cpp:L308-L325](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_tiling.cpp#L308-L325)：`TilingPrepareForFlashAttentionScoreEnhance` 用 `context->GetCompiledInfo<FlashAttentionScoreEnhanceCompileInfo>()`（L312）拿到结构体，再用 `PlatformAscendC` 的 `GetCoreNumAiv/GetCoreNumAic/GetSocVersion/GetCoreMemSize`（L316-322）逐字段填充——对应 4.4.2 流程图的上半部分。
- [flash_attention_score_enhance_tiling.cpp:L327-L331](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_tiling.cpp#L327-L331)：注册四连——`IMPL_OP(FlashAttentionScoreEnhance)` 绑定算子名；`.Tiling(TilingFlashAttentionScoreEnhance)` 挂执行入口；`.TilingInputsDataDependency({7, 8, 9, 10, 11})` 声明 tiling 依赖第 7~11 号**输入的值**（对照 4.2.4 的编号，正是 prefix/actual_seq_qlen/actual_seq_kvlen/q_start_idx/kv_start_idx 五个 varlen 元数据——varlen 切分必须知道每个 batch 的真实长度）；`.TilingParse<FlashAttentionScoreEnhanceCompileInfo>(...)` 把平台信息结构体交给框架托管。

执行期的消费：

- [flash_attention_score_enhance_tiling.cpp:L287-L306](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_tiling.cpp#L287-L306)：tiling 入口 `TilingFlashAttentionScoreEnhance`：先 `CheckParams`，再做空输入早退（L300），最后交给 `TilingRegistryNew::GetInstance().DoTilingImpl(context)`（L304）——这正是 u3-l3 讲过的**tiling 责任链**：多个候选实现按优先级依次尝试，`GRAPH_PARAM_INVALID` 让位给下一个。
- [flash_attention_score_enhance_tiling.cpp:L252-L282](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_tiling.cpp#L252-L282)：空输入路径对 CompileInfo 的具体消费——L255-256 取回 `compileInfoPtr`，L259 用 `compileInfoPtr->aivNum` 计算空输出的多核切分，L274 设 `FA_EMPTY_TILING_KEY`（空输入走专属 tilingKey），L278 用 `CalcTschBlockDim` 结合 aivNum 设 blockDim。文件中段 L240-250 可见的注释还详细写死了主核/尾核的分配规则（blocks 与 coreNum 整除/非整除的分支），是难得的中文算法注释。
- arch32 通用实现同样消费它：[arch32/flash_attention_score_enhance_tiling_general.cpp:L598](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L598) 同样 `reinterpret_cast` 取回 CompileInfo 使用（该文件 L23 include 了同一个 common 头）。这正是「common」的含义：**跨 tiling 实现共享**。

#### 4.4.4 代码实践：统计 CompileInfo 在 UT 中的使用

**实践目标**：确认这份「编译期平台信息」在单元测试里如何被伪造，体会 UT 无硬件可测的关键一环（承接 u3-l4 的 stub 思想）。

**操作步骤**：

1. 执行 `grep -c "FlashAttentionScoreEnhanceCompileInfo compileInfo" ascendc/src/ops-transformer/attention/flash_attention_score_enhance/tests/ut/op_host/test_flash_attention_score_enhance_tiling.cpp`。
2. 打开该 UT 文件，跳到第一个命中处（第 42 行附近），读一读 `compileInfo = {...}` 里填的 aivNum/ubSize 等数值。
3. 对照 u8 单元将讲的 TilingContextPara 用例法，思考：UT 里填一个假的 `aivNum = 50`，tiling 会按 50 个核切分吗？

**需要观察的现象**：当前 HEAD 下 grep 计数应为 51——即该 UT 文件里有 51 个用例各自构造了一份 CompileInfo（数值是伪造的平台参数，与真实芯片无关）。

**预期结果**：是的，tiling 完全按伪造值切分。CompileInfo 是 tiling 的「世界观输入」，UT 通过伪造它来模拟任意型号的芯片（不同核数/UB 大小），再断言 TilingData/tilingKey 输出——这正是 u3-l4「桩使 UT 无硬件可测」的又一实例。具体用例写法在 u8-l2 展开。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `TilingParse`（探测平台）和 `Tiling`（执行切分）要分成两个回调，而不是在一次调用里现场探测？

**答案**：平台信息对同一编译上下文是恒定的，探测一次、缓存复用即可；分开后 tiling 执行期只做一次指针取回（`GetCompileInfo`），把昂贵的平台查询从每次算子调用的热路径上挪走。这也是 CompileInfo 结构体存在的意义——它是两阶段之间的缓存载体。

**练习 2**：`TilingInputsDataDependency({7, 8, 9, 10, 11})` 与 def 里的 `ValueDepend(OPTIONAL)` 是什么关系？

**答案**：同一件事的两端声明。def 侧的 `.ValueDepend(OPTIONAL)`（prefix L152 等）声明「这五个输入的值会被算子消费」；tiling 侧的 `TilingInputsDataDependency` 进一步声明「具体是 **tiling 阶段**就要消费它们的值」。两端合起来框架才知道：必须在 tiling 前把这五个 INT64 数组从 Device 拷回 Host（或保证其在 Host 可见），否则 varlen 切分无从下手。

**练习 3**：如果把 `tiling_common.h` 里的 `ubSize` 字段删掉，哪些文件会编译失败？

**答案**：直接 include 它的三个文件会受影响：`flash_attention_score_enhance_tiling.cpp`（L23 include，L319 填充 ubSize）、`arch32/flash_attention_score_enhance_tiling_general.cpp`（L25 include，消费字段）、以及 UT `test_flash_attention_score_enhance_tiling.cpp`（L12 include，51 处构造里大多初始化了 ubSize 字段——删除字段后带初始化的构造会直接编译报错）。这个依赖关系用 `grep -l "flash_attention_score_enhance_tiling_common.h"` 即可自行验证。

## 5. 综合实践

**任务：整理 FA 前反向数据流图 + 小规模 golden 对拍**（本讲的主实践）。

**第一步：绘制前反向数据流图。** 以本讲源码为依据，画出训练一步中 FA 前后的张量流转：

```text
                ┌────────────────────────── 前向 flash_attention_score_enhance ─────────────────────────┐
 query/key/value ├─→                                                                      ├─→ softmax_max  (FP32) ─┐
 pse/atten_mask  │    scale*(q·kᵀ + qRope·kRopeᵀ) → mask → online softmax → dropout → ·value          ├─→ softmax_sum  (FP32) ─┤
 rope/sink/varlen│                                                                        ├─→ softmax_out  (P)    ─┤
 元数据(7~11号)  └────────────────────────────────────────────────────────────────────────────────┘   └→ attention_out (O) ──┤
                                                                                                (下一层 + 损失)        │
                ┌────────────────────────── 反向 flash_attention_score_grad_enhance ──────────────────┐ │
 dy(上游梯度)   ├─→                                                                                      │←┘
 同一份         │    重建打分/mask → 由 softmax_max+softmax_sum 反解 P（或直接用 softmax_in）            │
 q/k/value/     │    → dV=Pᵀ·dy、dK=dPᵀ·q、dq=dP·k … （具体矩阵乘次序见 u4-l4）                        ├─→ dq/dk/dv/dpse…
 元数据         └────────────────────────────────────────────────────────────────────────────────────┘
```

要求每个箭头都标注源码依据行号：前向四输出出自 [flash_attention_score_enhance_def.cpp:L322-L381](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_def.cpp#L322-L381)，反向四输入出自 [flash_attention_score_grad_enhance_def.cpp:L158-L206](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_grad_enhance/op_host/flash_attention_score_grad_enhance_def.cpp#L158-L206)。结论应落在一点上：**前向的 4 个输出全部被反向消费，一个不多、一个不少**。

**第二步：扩充 4.3.4 的 golden 脚本，加入 sink 处理的示意实现。** 在朴素参考实现中给每行额外拼一个「虚拟 sink 打分」（每 head 一个标量，按注意力汇的常见语义并入 softmax 分母与分子），如下（**示例代码**；注意：仓库两份文档均未给出 sink 的精确数学形式，此处仅演示「sink 并入 softmax」的一般语义，不保证与 kernel 逐比特一致）：

```python
# 在 1) 朴素参考中追加（示例代码，示意性 sink 处理）
sink = torch.randn(1)                                  # 每 head 一个可学习标量，模拟 sinkOptional=[headNum]
s_full = torch.cat([s, sink.expand(1, Nq, 1)], dim=-1) # 把 sink 当作一个虚拟 key 拼在打分行尾
p_full = torch.softmax(s_full, dim=-1)
o_with_sink = p_full[..., :-1] @ v                     # sink 无对应 value（被吸收），仅影响归一化
print("with-sink vs no-sink max diff =", (o_with_sink - o).abs().max().item())
```

**第三步：观察与验证。**

1. 运行扩充后的脚本：online softmax 三项校验仍应全部通过；追加 sink 后输出与不加 sink 的版本出现可度量差异——说明 sink 通过改变 softmax 归一化分母影响了注意力分配。
2. 回到文档核对：`sinkOptional` 的 shape 是 `[headNum]`（FLOAT32）、`sinkNum>0` 时与之互斥（[aclnnFlashAttentionVarLenScoreEnhanceV5.md:L183-L191](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/docs/aclnnFlashAttentionVarLenScoreEnhanceV5.md#L183-L191) 与 L530-536）。你的示意实现与哪种机制对应？另一种（Param Sink，`sinkNum*64` 个 token 追加在 KV 尾部）在数据流图上应如何表示？
3. **待本地验证**：若你手头有装好算子包的 NPU 环境，可把 4.3.4 脚本的输入搬到 `torch.ops.custom.npu_flash_attention_score_enhance`（参考 [npu_flash_attention_score_enhance.md:L272-L287](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/docs/npu_flash_attention_score_enhance.md#L272-L287) 的对拍写法），把 CPU 的 m/l 与算子返回的 softmax_max/softmax_sum 对比（注意 NPU 侧是 bf16 输入 + FP32 中间量，容差需放宽；且 max/sum 的末维为 8，需先理解其排布再对拍，建议留到 u4-l3 之后再做）。

## 6. 本讲小结

- FA 前向公式可拆为「打分（含 rope 相加项与 pse）→ mask → online softmax → dropout → 乘 value」五步；两份文档（torch 侧 / aclnn 侧）描述同一算子的两层接口，参数可一一对账。
- `_def.cpp` 是接口契约的事实源：3 必选 + 15 可选共 18 个输入、4 个 REQUIRED 输出、14 个属性；五个 varlen 元数据输入带 `ValueDepend`，其声明顺序（下标 7~11）与 tiling 侧 `TilingInputsDataDependency({7,8,9,10,11})` 精确互锁。
- online softmax 用滚动的 \( m_i \)（行最大）与 \( l_i \)（指数和）把整行 softmax 化为分块计算，代价是丢掉 \( s_{ij} \)；因此前向必须落盘 `softmax_max`/`softmax_sum`，反向凭 \( p_{ij}=e^{s_{ij}-m_i}/l_i \) 重建概率矩阵。
- 反向算子 def 的输入里出现了 `softmax_max`/`softmax_sum`/`softmax_in`/`attention_in`——前向 4 个输出全部被反向消费，这就是训练 FA 与推理 FA 的接口差异根源。
- `tiling_common.h` 的真实角色是跨 tiling 实现共享的平台编译信息结构体（核数/四级缓存/socVersion），由 `TilingParse` 一次性探测填充、tiling 执行期取回消费；多 layout 的 shape 处理在 tiling.cpp 与 infershape.cpp，留待下一讲。
- 文档存在滞后实例（npu 文档输出计数笔误、aclnn 参数表缺 `softmaxOutOut` 行），印证 u2-l5 的方法论：**读接口以 `_def.cpp` 与 aclnn 头文件为准**。

## 7. 下一步学习建议

- **u4-l2（FA 前向 Tiling 与 InferShape 细节）**：本讲只看了 tiling 入口与 CompileInfo；下一讲进入 `flash_attention_score_enhance_tiling.cpp` 的责任链实现、`arch32/tiling_general.cpp` 的按 B/N2/G/S1/S2 切分，以及 `infershape.cpp` 如何根据 `actualSeqQLen` 推导 [T,N,8] 输出。
- **u4-l3（FA 前向 Kernel）**：看 `op_kernel` 主入口如何按 tilingKey 分发到各 layout 模板，`softmax_max/sum` 末维的 8 在 kernel 里如何使用，dropout 掩码与空输入的边界处理。
- **u4-l4（FA 反向算子）**：把本讲的数据依赖图落到反向的五次矩阵乘上，理解 `softmax_in`/`attention_in` 的复用路线。
- 若想先补齐责任链背景，可回读 u3-l3（TilingBase 三态返回值与模板注册）；想看这份 def 如何被包装成 torch 算子，可跳读 u6-l1/u6-l2。

# 视觉编码器：ViT 与 Swin

## 1. 本讲目标

本讲把 FasterTransformer（后文简称 FT）的「编码器」从自然语言推进到计算机视觉。读完本讲你应该能够：

- 说清 **ViT** 如何把一张图像切成 patch、当成 token 序列，再套用与 BERT 几乎相同的 transformer block；
- 说清 **Swin** 的两层创新——**窗口注意力（window attention）** 与 **移窗（shifted window）**——以及它为何带来对图像尺寸的**线性复杂度**；
- 看懂 `SwinTransformer → SwinTransformerBasicLayer → SwinTransformerBlock` 的三层调用层级，以及层级式下采样（patch merge）如何同时缩小分辨率、加倍通道；
- 指出 Swin 相比 ViT 多出的两个专属 kernel：`image_shift_partition` 与 `image_merge`，并理解它们各自解决什么问题；
- 对比 `vit_gemm`、`swin_gemm` 与 BERT 的 `bert_gemm` 在 GEMM 调优上的差异。

本讲承接 [u4-l1 BERT 模型与 forward 主流程](./u4-l1-bert-model.md)（transformer block 的 GEMM + kernel 流水）与 [u3-l2 注意力 kernel](./u3-l2-attention-kernels.md)（融合多头注意力），是把同一套编码器骨架迁移到视觉模态的实践。

## 2. 前置知识

### 2.1 从「词向量」到「图像 token」

NLP 里 transformer 的输入是一串词向量 \([B, L, H]\)（batch、序列长度、隐层维度）。视觉里没有天然的「词」，ViT 的核心想法是**人为造出 token**：

- 把 \(H_{\text{img}}\times W_{\text{img}}\) 的图像按 \(P\times P\) 的方块切分，得到 \(N=(H_{\text{img}}/P)\cdot(W_{\text{img}}/P)\) 个 patch；
- 每个 patch 是 \(C\times P\times P\) 的小块，用一个卷积（kernel 与 stride 都为 \(P\)）把它压成一个 \(H\) 维向量，等价于一次「词嵌入」；
- 于是图像变成 \([B, N, H]\) 的 token 序列，后续可以**原样复用**语言 transformer 的 block。

这样，ViT 的 transformer block 与 BERT 的 block 在数学上完全相同，差别只在「输入是怎么构造出来的」。

### 2.2 注意力的复杂度瓶颈

标准自注意力对 \(N\) 个 token 的复杂度是 \(O(N^2)\)（要算 \(N\times N\) 的相似度矩阵）。对 NLP 来说 \(N\) 通常几百；但视觉里，一张 \(384\times384\)、patch \(16\) 的图就有 \(24\times24=576\) 个 token，更高分辨率会迅速爆炸。

**Swin** 的回答是：不在全图上做注意力，而是把图切成若干不重叠的 \(W\times W\) **窗口**，只在窗口内做注意力；再通过**移窗（shift）**让相邻窗口交换信息。这样每张图的注意力成本变成 \(O(N\cdot W^2)\)（\(N\) 个 token，每个只在一个 \(W^2\) 大小的窗口里参与注意力），对图像尺寸近似线性。

### 2.3 与本册已建立认知的衔接

- FT 全库的 `invokeXxx` kernel 约定、`BaseLayer` 的 buffer 生命周期、`cublasMMWrapper` 的 GEMM、`FusedAttentionLayer`/`UnfusedAttentionLayer` 都已在 u2/u3 讲清，本讲直接复用，不重复；
- 本讲唯一新增的「框架依赖」是 **cuDNN**：ViT/Swin 的 patch embed 用 cuDNN 卷积实现，这是 FT 极少数依赖 cuDNN 的地方（见构造函数里的 `cudnnHandle_t` 参数）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/fastertransformer/models/vit/ViT.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/vit/ViT.cc) | ViT 模型主体：`patchEmbed` + `forward` 主循环 |
| [src/fastertransformer/models/vit/ViT.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/vit/ViT.h) | `ViTTransformer` 类声明与成员 buffer |
| [src/fastertransformer/models/vit/vit_gemm.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/vit/vit_gemm.cc) | ViT 的 GEMM 离线调优工具入口 |
| [src/fastertransformer/models/swin/Swin.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/swin/Swin.cc) | Swin 顶层：patch embed + 多级 basic layer + avg pool |
| [src/fastertransformer/models/swin/SwinBasicLayer.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/swin/SwinBasicLayer.cc) | 一个 stage：若干 block + 可选 patch merge |
| [src/fastertransformer/models/swin/SwinBlock.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/swin/SwinBlock.cc) | 单个 transformer block：shift-partition + window attention + MLP |
| [src/fastertransformer/models/swin/swin_gemm.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/swin/swin_gemm.cc) | Swin 的 GEMM 离线调优工具入口 |
| [src/fastertransformer/kernels/image_shift_partition_kernels.cu](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/image_shift_partition_kernels.cu) | **Swin 专属 kernel ①**：循环移位 + 窗口重排 |
| [src/fastertransformer/kernels/image_merge_kernels.cu](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/image_merge_kernels.cu) | **Swin 专属 kernel ②**：patch merge 的空间合并 |
| [docs/vit_guide.md](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/vit_guide.md) | ViT 计算流图、运行命令与性能数据 |
| [docs/swin_guide.md](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/swin_guide.md) | Swin 计算流图、运行命令与性能数据 |

---

## 4. 核心概念与源码讲解

### 4.1 ViT：把图像切成 patch 后复用标准 transformer block

#### 4.1.1 概念说明

ViT（Vision Transformer）只回答一个问题：**怎么把图像喂给一个原本处理文本的 transformer**。它的做法是：

1. **Patch Embedding**：用一个 stride = kernel = \(P\) 的卷积，把 \([B, C, H_{\text{img}}, W_{\text{img}}]\) 的图像压成 \([B, N, H]\) 的 token 序列，其中 \(N=(H_{\text{img}}/P)\cdot(W_{\text{img}}/P)\)、\(H\) 是 `embed_dim`。
2. **Class token 与位置编码**（可选）：在序列开头拼接一个可学习的 `cls_token`，并给每个位置加上可学习的位置编码 `pos_embed`。
3. **N 层标准 transformer block**：与 BERT 一模一样的「LayerNorm → 注意力 → 残差 → LayerNorm → FFN → 残差」。
4. **末层 LayerNorm**：输出 \([B, N, H]\) 的特征（`cls_token` 位置常用于分类）。

关键洞察：**第 3 步与 BERT 完全相同**，所以 ViT 复用了 FT 已经为 BERT 写好的 `FusedAttentionLayer`/`UnfusedAttentionLayer` 和 `GeluFfnLayer`，不需要新写任何注意力或 FFN 代码。

#### 4.1.2 核心流程

```text
输入图像 [B, C, H_img, W_img]
   │  patchEmbed: conv2d(stride=P,k=P) → [B, N, H]
   │  + bias, (+ cls_token), + pos_embed
   ▼
encoder_input [B, N, H]   （N = (H_img/P)² + (cls?1:0)）
   │  for i in 0..num_layer-1:
   │      LayerNorm → Attention(attn_layernorm) → +残差
   │      LayerNorm → FFN(gelu)                → +残差
   ▼
final LayerNorm
   ▼
输出特征 [B, N, H]
```

其中序列长度 \(N\) 在构造期就定死：

\[
N = \left(\frac{H_{\text{img}}}{P}\right)^2 + \mathbb{1}[\text{cls\_token}]
\]

例如 ViT-B/16、\(384\times384\)、带 cls token：\(N=(384/16)^2+1=24^2+1=577\)。

#### 4.1.3 源码精读

**① 序列长度在构造期算好**

`ViTTransformer` 的构造函数把 `request_seq_len_` 直接写成上面的公式：

[ViT.cc:138](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/vit/ViT.cc#L138)（构造期计算序列长度）

```cpp
request_seq_len_(img_size * img_size / patch_size / patch_size + (with_cls_token ? 1 : 0)),
```

随后 `initialize()` 会校验两个硬约束：图像边长必须能被 patch 整除、`head_num*head_dim == embed_dim`：

[ViT.cc:50-62](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/vit/ViT.cc#L50-L62)（校验 img/patch 整除与 head/dim 匹配）

**② 注意力层与 BERT 共用同一套**

`initialize()` 按 `AttentionType` 选择 `FusedAttentionLayer`（FP16 + 合法形状时走 TensorRT 融合 kernel）或 `UnfusedAttentionLayer`（cuBLAS GEMM 展开），这两个类就是 u3-l3 里讲过的同一对：

[ViT.cc:65-97](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/vit/ViT.cc#L65-L97)（按 attention_type 创建 Fused/Unfused 注意力层）

FFN 同理，直接 `new GeluFfnLayer<T>(...)`：

[ViT.cc:102-112](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/vit/ViT.cc#L102-L112)（创建与 BERT 同款的 GeluFFN）

> 这正印证了 4.1.1 的论断：ViT 的「模型主体」没有为视觉新写任何注意力/FFN，只是把 BERT 的零件装配起来。

**③ forward：patch embed → 主循环 → 末层 LN**

forward 的输入是 BCHW 图像、输出是 `[B, seq_len, embed_dim]` 特征：

[ViT.cc:264-283](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/vit/ViT.cc#L264-L283)（输入输出约定与形状校验）

第一步 `patchEmbed` 把图像变成 token 序列：

[ViT.cc:290-301](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/vit/ViT.cc#L290-L301)（调用 patchEmbed 完成卷积+位置编码）

`patchEmbed` 内部就是一个 cuDNN 卷积加上「加偏置 / 拼 cls token / 加位置编码」的融合 elementwise kernel：

[ViT.cc:472-483](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/vit/ViT.cc#L472-L483)（conv2d + invokeAddBiasConcatClsTokenAddPosEmbed）

```cpp
conv2d(tmp_buf, input, kernel, batch, img_size, img_size,
       in_chans, embed_dim, patch_size, patch_size, cudnn_handle_);
// ...
if (with_cls_token_) {
    invokeAddBiasConcatClsTokenAddPosEmbed(tmp_buf, output, bias, cls_embed, pos_embed, m, n, s, stream_);
} else {
    invokeAddBiasAddPosEmbed(tmp_buf, bias, pos_embed, m, n, s * n, stream_);
}
```

随后是 transformer block 主循环。仔细看每一层做了什么——它与 BERT 的 block 逐字对应：

[ViT.cc:329-396](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/vit/ViT.cc#L329-L396)（每层：LN→Attention→残差→LN→FFN→残差）

```cpp
for (uint i = 0; i < num_layer_; i++) {
    invokeGeneralLayerNorm(norm_out_buf, from_buf, /*attn LN weights*/...);
    // Attention
    attention_layer_->forward(&attn_output_tensors, &attn_input_tensors,
                              &weights->vit_layer_weights[i].attention_weights);
    invokeGeneralAddBiasResidualPreLayerNorm(from_buf, norm_out_buf, from_buf, attn_out_buf,
                                             /*ffn LN weights*/...);
    // FFN
    ffn_layer_->forward(&ffn_output_tensors, &ffn_input_tensors, &weights->vit_layer_weights[i].ffn_weights);
    invokeAddBiasResidual(from_buf, attn_out_buf, /*ffn output bias*/...);
}
```

三块 buffer（`embed_buf_1_/2_/3_`）在层间原地流式覆写，正是 u4-l1 讲过的「两块 buffer 交替」手法。最后跑一次 `invokeGeneralLayerNorm` 得到输出：

[ViT.cc:398-411](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/vit/ViT.cc#L398-L411)（末层 LayerNorm，必要时 remove padding 恢复）

#### 4.1.4 代码实践

**实践目标**：亲手验证「ViT block == BERT block，差别只在输入构造」。

**操作步骤**：

1. 打开 [src/fastertransformer/models/vit/ViT.cc:329-396](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/vit/ViT.cc#L329-L396)，把每层循环体里的 5 个调用列出来。
2. 打开 [src/fastertransformer/models/bert/Bert.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/Bert.cc)（u4-l1 已读过）找到对应的主循环，逐行比对两者的 LayerNorm/Attention/FFN 调用顺序。
3. 回到 [ViT.cc:472-483](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/vit/ViT.cc#L472-L483)，确认 ViT 多出来的只有 `conv2d` + `invokeAddBiasConcatClsTokenAddPosEmbed` 这一步「输入构造」。

**需要观察的现象**：

- 两者主循环的算子序列完全同构：都是 `LayerNorm → Attention → AddBiasResidual(+PreLN) → FFN → AddBiasResidual`；
- 唯一不同的「头部」是：BERT 用 token embedding 查表，ViT 用 cuDNN 卷积把图像切块。

**预期结果**：你能用一张两列的表把 ViT 与 BERT 每一层的算子一一对应，唯一无法对应的行是「输入构造」。

> 命令行可运行验证（需 GPU 与已编译产物）：按 [docs/vit_guide.md:111-117](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/vit_guide.md#L111-L117) 先跑 `./bin/vit_gemm 32 384 16 768 12 1 1 0` 调优，再跑 `./bin/vit_example 32 384 16 768 12 12 1 1`。若本地无 GPU，则以上为「源码阅读型实践」，结论不变。**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：ViT-B/16、输入 \(224\times224\)、不带 cls token，序列长度是多少？带 cls token 又是多少？

**参考答案**：\(N=(224/16)^2=14^2=196\)；带 cls token 时 \(N=196+1=197\)。这与构造函数 [ViT.cc:138](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/vit/ViT.cc#L138) 的公式一致。

**练习 2**：为什么 ViT 可以直接复用 BERT 的 `UnfusedAttentionLayer`，而不需要新写一个「视觉注意力」类？

**参考答案**：因为 patch embedding 之后，图像已经被表示成 \([B, N, H]\) 的 token 序列，与文本 token 序列在数据布局上完全一致；自注意力本身与「token 是词还是 patch」无关，只依赖序列长度与隐层维度。所以同一份注意力实现可以无修改复用。

---

### 4.2 Swin：层级式窗口注意力

#### 4.2.1 概念说明

Swin Transformer 用两个机制把视觉注意力的成本压到近似线性：

- **窗口注意力（Window-based MHA）**：把 \(H\times W\) 的 token 图切成 \((H/w)\cdot(W/w)\) 个互不重叠的 \(w\times w\) 窗口，注意力只在每个 \(w^2\) 个 token 的窗口内计算。一张图的复杂度从 \(O((HW)^2)\) 降到 \(O(HW\cdot w^2)\)。
- **移窗（Shifted Window）**：若每个窗口都固定不变，窗口之间永远不交换信息，等价于一组孤立的小模型。Swin 在相邻 block 之间把整张图**循环移位** \(w/2\)，让原本跨窗口相邻的 token 落进同一窗口，从而建立跨窗口连接。

此外 Swin 是**层级式（hierarchical）**的：每经过一个 stage，做一次 **patch merge**——把 \(2\times2\) 邻域的 token 拼接后用一次线性投影，使**空间分辨率减半、通道数加倍**（类似 CNN 的 stage 下采样）。这使得 Swin 既能输出高分辨率特征（适合检测/分割），又控制了整体计算量。

#### 4.2.2 核心流程

Swin 的调用链是清晰的三层：

```text
SwinTransformer.forward()                 # 顶层：1 个模型
  ├── patchEmbed (conv2d + LN)            # 图像 → token 图 [B, R, R, C]
  ├── for i in 0..layer_num-1:            # 每个 stage
  │     SwinTransformerBasicLayer.forward()
  │       ├── for d in 0..depth-1:        # stage 内若干 block
  │       │     SwinTransformerBlock.forward()
  │       │       ├── shift + partition   # 移窗 + 窗口重排（专属 kernel）
  │       │       ├── WindowAttention     # 窗口内注意力
  │       │       └── MLP (2×GEMM + GeLU)
  │       └── patchMerge (最后才做)        # 分辨率减半、通道加倍
  ├── final LayerNorm
  └── avg pool (stridedBatchedGemm)       # 得到 [B, C_final] 全局特征
```

注意三个「每往下一级就改变」的维度：

- `SwinTransformer` 层面：每过一个 stage，`dim *= 2`、`input_resolution /= 2`；
- `BasicLayer` 层面：每个 block 交替 `shift_size = 0` 与 `shift_size = window_size/2`；
- `Block` 层面：把整张 token 图重新排列成「窗口维 × 窗口内 token 维」，再交给 `WindowAttention`。

#### 4.2.3 源码精读

**① 顶层 `SwinTransformer::forward`：patch embed + 多级 stage + avg pool**

输入是 `[B, C_in, R, R]` 图像，输出是 `[B, final_dim]` 的池化特征：

[Swin.cc:166-180](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/swin/Swin.cc#L166-L180)（输入输出约定与 patch embed 调用）

每个 stage 之间，分辨率与通道数此消彼长，这是层级式结构的核心：

[Swin.cc:201-228](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/swin/Swin.cc#L201-L228)（stage 循环：dim 加倍、分辨率减半）

```cpp
for (int i = 0; i < layer_num_; i++) {
    if (i == layer_num_ - 1) do_patch_merge = false;        // 最后一级不下采样
    // ... 把 [B, R, R, C] 喂给 basic_layer_->forward，输出 [B, R', R', C']
    if (i != layer_num_ - 1) {
        basic_layer_dim *= 2;                               // 通道加倍
        basic_layer_input_resolution /= 2;                  // 分辨率减半
    }
}
```

末尾先做一次 LayerNorm，再用一次 `stridedBatchedGemm` 把每个样本的所有 token 与「全 1 向量」相乘，等价于全局平均池化：

[Swin.cc:229-257](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/swin/Swin.cc#L229-L257)（末层 LN + avg pool）

其中 `avg_pool_ones_` 在 `allocateBuffer` 里用 `deviceFill(..., T(1.0f))` 填成全 1：

[Swin.cc:96-102](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/swin/Swin.cc#L96-L102)（构造全 1 的池化核）

**② `SwinTransformerBasicLayer::forward`：stage 内的 block 循环与移窗交替**

一个 stage 包含 `depth` 个 block。关键细节是 `shift_size` 随 block 奇偶交替：

[SwinBasicLayer.cc:187-214](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/swin/SwinBasicLayer.cc#L187-L214)（stage 循环，shift_size 在 0 与 window_size/2 间交替）

```cpp
for (int i = 0; i < depth; i++) {
    shift_size = (i % 2 == 0) ? 0 : (window_size_ / 2);   // 偶数 block 不移位，奇数 block 移位
    int additional_parameters[3] = {num_head, shift_size, sm};
    block_->forward(&tmp_output_tensors, &tmp_input_tensors,
                    swin_basic_layer_weights.block_weight_list[i]);
}
```

`do_patch_merge` 为真时，stage 末尾再调用 `patchMerge` 做下采样：

[SwinBasicLayer.cc:216-224](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/swin/SwinBasicLayer.cc#L216-L224)（调用 patchMerge）

`patchMerge` 内部分 V1/V2 两条路径：V1 用 `invokeMergeLayernorm` 做「层归一化 + 4 邻域拼接」、V2 用 `invokeImageMerge`（即 4.3 的专属 kernel）做空间合并，随后都用一次 `Gemm` 把 \(4C\) 投影回 \(2C\)：

[SwinBasicLayer.cc:102-151](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/swin/SwinBasicLayer.cc#L102-L151)（patchMerge 的 V1/V2 分支）

**③ `SwinTransformerBlock::forward`：移窗分区 + 窗口注意力 + MLP**

这是单个 transformer block。它先按版本调用不同的「移窗 + 分区」kernel（V1 把 LayerNorm 也融进去，V2 只做分区、LayerNorm 推迟到残差里）：

[SwinBlock.cc:130-154](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/swin/SwinBlock.cc#L130-L154)（V2 用 invokeShiftPartition，V1 用 invokeLayernormShiftPartition）

```cpp
if (version_ == 2) {
    invokeShiftPartition(normed_shifted_input_, input, batch,
                          input_resolution, input_resolution, dim,
                          shift_size, window_size_in_use, stream_);
} else if (version_ == 1) {
    invokeLayernormShiftPartition(normed_shifted_input_, input, /*LN weights*/...,
                                  shift_size, window_size_in_use, stream_);
}
```

随后把重排好的 token 交给 `WindowAttention`（窗口内注意力，自带相对位置偏置 `attention_relative_position_bias`）：

[SwinBlock.cc:160-189](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/swin/SwinBlock.cc#L160-L189)（组装张量并调用 atten_->forward）

最后是标准 MLP：两段 `Gemm`（升维到 `mlp_dim = mlp_ratio*dim`、再降回 `dim`）夹一个 `invokeAddBiasGeluV2`，结构上与 BERT/FFN 完全一致：

[SwinBlock.cc:222-252](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/swin/SwinBlock.cc#L222-L252)（MLP 两段 GEMM + GeLU）

#### 4.2.4 代码实践

**实践目标**：跟踪 `shift_size` 在一个 stage 内如何交替，并理解 patch merge 前后形状的变化。

**操作步骤**：

1. 在 [SwinBasicLayer.cc:194](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/swin/SwinBasicLayer.cc#L194) 与 [SwinBasicLayer.cc:234](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/swin/SwinBasicLayer.cc#L234) 处确认：`i` 为偶数时 `shift_size=0`，`i` 为奇数时 `shift_size=window_size_/2`。
2. 在 [Swin.cc:216-219](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/swin/Swin.cc#L216-L219) 处确认：除最后一级外，每过一级 `dim *= 2`、`input_resolution /= 2`。
3. 假设初始 `R=64`、`C=96`、`layer_num=4`、`window_size=8`，手工推出第 4 级的 `R` 与 `C`。

**需要观察的现象**：

- `shift_size` 的交替正是「移窗」机制的体现——奇偶 block 用不同的窗口划分，从而让窗口边界穿越；
- 形状演变满足 \(R_i = R_0 / 2^i\)、\(C_i = C_0 \cdot 2^i\)（最后一级不再下采样）。

**预期结果**：初始 \(R=64, C=96\)，则 4 个 stage 后 \(R=64/2^3=8\)、\(C=96\cdot 2^3=768\)（最后一级不除 2、不乘 2）。**待本地验证**（可在 `forward` 里临时加 `FT_LOG_DEBUG` 打印每级的 `basic_layer_input_resolution` 与 `basic_layer_dim` 对照）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `shift_size` 取 `window_size/2` 而不是任意值？

**参考答案**：移 \(w/2\) 后，原本位于窗口边界两侧、相距 \(w/2\) 的 token 会落进同一个新窗口；移位量再大没必要（窗口内最多容纳 \(w\times w\)），移位量太小则跨窗口连接不充分。同时循环移位要保证移完后仍是整数个窗口，\(w/2\) 在 \(H,W\) 是 \(w\) 倍数时天然满足。

**练习 2**：Swin 的 patch merge 与 CNN 里哪种操作最像？

**参考答案**：最像 stride=2 的 \(2\times2\) 卷积或「像素拆拼（pixel unshuffle）」+ \(1\times1\) 卷积——都是把 \(2\times2\) 空间邻域的信息合并到通道维、使空间分辨率减半而通道加倍，从而形成层级式特征金字塔。

---

### 4.3 Swin 专属 kernel：image_shift_partition 与 image_merge

#### 4.3.1 概念说明

这两个 kernel 文件是 **Swin 相比 ViT（以及 BERT）真正多出来的东西**。ViT 的 token 序列在整段前向里顺序固定，无需重排；而 Swin 为了实现「窗口注意力 + 移窗」和「patch merge」，必须在每个 block 开头把 token 图按窗口重新排列、在 patch merge 时把 \(2\times2\) 邻域合并。这两件事都涉及**非平凡的下标重映射**，所以各有专门 kernel：

- `image_shift_partition_kernels.cu`：先对整张 token 图做**循环移位**，再把每个窗口内的 token **连续排列**，使后续 `WindowAttention` 可以把一个窗口当成一个独立的小序列处理。
- `image_merge_kernels.cu`：在 patch merge 阶段把 \([B, 2H, 2W, C/4]\) 的输入重排成 \([B, H, W, C]\)，即「\(2\times2\) 空间 → 通道」。

#### 4.3.2 核心流程

**移窗分区的下标计算**：对 token 图中位置 \((h,w)\) 的 token，先做循环移位得到 \((h',w')\)：

\[
h' = (h - \text{shift\_size} + H)\bmod H,\quad w' = (w - \text{shift\_size} + W)\bmod W
\]

再把它归入哪个窗口、落在窗口内第几位：

\[
\text{window\_idx} = \lfloor h'/w \rfloor \cdot (W/w) + \lfloor w'/w \rfloor,\quad
\text{idx\_in\_window} = (h'\bmod w)\cdot w + (w'\bmod w)
\]

输出把同一窗口的 token 连续存放，便于后续注意力以「窗口为单位」批量处理。

**image merge 的下标计算**：输出通道按 \(4\) 等分，每一份对应 \(2\times2\) 邻域中的一个位置：

\[
\text{part\_id} = \lfloor c_{\text{out}} / (C/4) \rfloor,\quad
\text{offset\_in\_W} = \lfloor \text{part\_id}/2 \rfloor,\quad
\text{offset\_in\_H} = \text{part\_id}\bmod 2
\]

于是输出 \([B,H,W,C]\) 中位置 \((h,w,c)\) 来自输入 \([B,2H,2W,C/4]\) 中位置 \((2h+\text{offset\_in\_H},\;2w+\text{offset\_in\_W},\;c\bmod(C/4))\)。

#### 4.3.3 源码精读

**① 移窗分区 kernel**

`shift_partition` 是面向 `half2`/`bfloat162` 的主版本。注意它的 grid 是 `(W, H, batch)`——一个 block 负责一个空间位置的所有通道，并直接在 kernel 里算出移位后的目标下标：

[image_shift_partition_kernels.cu:26-45](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/image_shift_partition_kernels.cu#L26-L45)（移窗 + 窗口重排的下标重映射）

```cpp
const int shifted_H_idx = (shift_size != 0) ? ((blockIdx.y - shift_size + gridDim.y) % gridDim.y) : blockIdx.y;
const int shifted_W_idx = (shift_size != 0) ? ((blockIdx.x - shift_size + gridDim.x) % gridDim.x) : blockIdx.x;
const int window_H_idx  = shifted_H_idx / window_size;
const int window_W_idx  = shifted_W_idx / window_size;
const int window_idx    = window_H_idx * stride_of_window_H + window_W_idx;
const int idx_in_window = (shifted_H_idx % window_size) * window_size + (shifted_W_idx % window_size);
const int output_bid    = batch_offset + window_idx * window_size * window_size + idx_in_window;
// out_ptr[output_bid * n + tid] = input_ptr[bid * n + tid];
```

host 端 `invokeShiftPartition` 按 `blockSize` 在普通版与「每线程处理 4 个元素」的 `shift_partition_v2` 间选择，并对 FP32 单独特化：

[image_shift_partition_kernels.cu:127-145](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/image_shift_partition_kernels.cu#L127-L145)（按数据类型与 block 大小选择 kernel 版本）

同文件还提供 `invokeShiftPartitionCol32`（[L355-L378](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/image_shift_partition_kernels.cu#L355-L378)），在重排的同时把 FP16 量化成 INT32- COL32 布局，供 INT8 Swin 使用——这是 FT「低精度存储 + 高精度计算」套路的又一次体现。

**② image merge kernel**

`image_merge_kernel` 把 \([B,2H,2W,C/4]\) 重排成 \([B,H,W,C]\)。每个 block 负责输出图的一个 \((h,w)\) 位置，循环处理 \(C\) 个通道，按 `part_id` 决定从输入的哪个 \(2\times2\) 子位置取：

[image_merge_kernels.cu:30-51](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/image_merge_kernels.cu#L30-L51)（patch merge 的 \(2\times2\) 空间 → 通道重排）

```cpp
for (int col_id = tid; col_id < n; col_id += bdim) {
    int part_id      = col_id / n_4;        // n_4 = n/4，4 个邻域位置
    int offset_in_W  = part_id / 2;
    int offset_in_H  = part_id % 2;
    size_t input_id  = batch_offset
                     + (2*H_idx + offset_in_H) * input_H_stride
                     + (2*W_idx + offset_in_W) * n_4 + (col_id % n_4);
    size_t output_idx = batch_offset + H_idx*output_H_stride + W_idx*n + col_id;
    out[output_idx]   = ldg(input + input_id);
}
```

host 端 `invokeImageMerge` 把 grid 设成 `(W/2, H/2, batch)`：

[image_merge_kernels.cu:55-64](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/image_merge_kernels.cu#L55-L64)（host 启动配置）

它在 [SwinBasicLayer.cc:127](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/swin/SwinBasicLayer.cc#L127) 的 V2 patchMerge 中被调用，重排完再接一次 `Gemm` 完成 \(4C\to 2C\) 投影。

#### 4.3.4 代码实践

**实践目标**：用一个 \(4\times4\) 的小例子，手工跑一遍移窗分区的下标重映射，确认 kernel 行为。

**操作步骤**：

1. 设 \(H=W=4\)、`window_size=2`、`shift_size=1`（奇数 block）。
2. 对源位置 \((h,w)=(0,0)\)，套用 4.3.2 的公式算 `shifted_H_idx`、`shifted_W_idx`、`window_idx`、`idx_in_window`、`output_bid`。
3. 再算 \((0,1)\)、\((2,2)\) 两个位置，对照 [image_shift_partition_kernels.cu:31-38](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/image_shift_partition_kernels.cu#L31-L38) 验证。
4. 把同一个例子改成 `shift_size=0`（偶数 block），重算一遍，观察哪些 token 的目标位置变了。

**需要观察的现象**：

- `shift_size=0` 时，`output_bid` 退化为「按窗口连续编号」，等价于纯窗口划分；
- `shift_size=1` 时，原本位于 \((0,0)\) 的 token 会跳到网格另一侧（循环移位），窗口边界整体错位。

**预期结果**（以 \((0,0)\)、`shift=1`、`window=2`、`H=W=4` 为例）：

- `shifted_H_idx = (0-1+4)%4 = 3`，`shifted_W_idx = 3`；
- `window_H_idx = 3/2 = 1`，`window_W_idx = 1`，`window_idx = 1*2+1 = 3`；
- `idx_in_window = (3%2)*2 + (3%2) = 1*2+1 = 3`；
- `output_bid = batch_offset + 3*4 + 3 = batch_offset + 15`（落在最后一个窗口的最后一位）。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `shift_partition` 要在 kernel 内部直接算出 `output_bid`，而不是分两步「先移位、再分窗口」？

**参考答案**：分两步需要一块与输入等大的中间 buffer 来存「移位后的图」，再读一遍写一遍，多两次全局显存往返。把移位与分窗口融合进一个 kernel、用算出的目标下标直接「读 A 写 B」，只需一次读写，省掉中间 buffer 与一次访存。这正是 FT「融合 kernel」哲学的又一次体现。

**练习 2**：`image_merge` 的输入为什么通道维是 \(C/4\)、输出是 \(C\)？

**参考答案**：patch merge 要把 \(2\times2\) 个空间邻域的 token 合并。每个邻域位置贡献 \(C/4\) 个通道（输入通道是输出通道的 \(1/4\)），4 个位置正好拼出完整的 \(C\) 个通道；同时空间分辨率从 \(2H\times2W\) 降到 \(H\times W\)。这就是「空间换通道」的下采样。

---

### 4.4 GEMM 调优差异：vit_gemm 复用 BERT，swin_gemm 独立

#### 4.4.1 概念说明

承接 [u2-l4 GEMM 自动调优](./u2-l4-gemm-autotuning.md)：每个 (M,N,K) 形状需要单独离线调优出最优 cuBLAS 算法，写入 `gemm_config.in` 供运行期查表。ViT 和 Swin 的调优工具分别是 `vit_gemm` 与 `swin_gemm`，它们的关键差异源于 **M（token 数）与序列长度的取值完全不同**：

- ViT 的注意力是**全序列**的，M = 序列长度（整张图的 patch 数），与 BERT 完全同构；
- Swin 的注意力是**窗口内**的，每个窗口只有 `window_size²` 个 token，但「批数」被窗口个数放大。

#### 4.4.2 核心流程

两者的入口 `main` 都很薄，差别只在「如何把命令行参数翻译成 (batch, seq_len, head, size_per_head)」。

#### 4.4.3 源码精读

**① `vit_gemm`：直接复用 BERT 的 encoder 调优函数**

`vit_gemm` 把 `seq_len` 算成 patch 数（与 ViT.cc 公式一致），然后把活儿交给 BERT 那套 `generate_encoder_gemm_config`：

[vit_gemm.cc:60-67](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/vit/vit_gemm.cc#L60-L67)（seq_len 与 inter_size 计算）

[vit_gemm.cc:88-100](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/vit/vit_gemm.cc#L88-L100)（INT8 走 igemm，FP32/FP16 走 BERT 同款 encoder_gemm）

```cpp
if (int8_mode != 0) {
    ft::generate_encoder_igemm_config(batch_size, seq_len, head_num, size_per_head, gemm_test_buf, false);
} else if (data_type == ft::FLOAT_DATATYPE) {
    ft::generate_encoder_gemm_config<float>(batch_size, seq_len, head_num, size_per_head, gemm_test_buf, false);
} else if (data_type == ft::HALF_DATATYPE) {
    ft::generate_encoder_gemm_config<half>(batch_size, seq_len, head_num, size_per_head, gemm_test_buf, false);
}
```

`#include "src/fastertransformer/utils/gemm_test/encoder_gemm_func.h"` 正是 BERT 用的同一份头文件——这从工程上再次证明 **ViT 与 BERT 共享同一套 GEMM 调优产物结构**。

**② `swin_gemm`：独立的 swin 调优函数，且要枚举 4 个 stage**

Swin 的 (batch, seq_len) 取值与 ViT 截然不同：`seq_len = window_width*window_width`（窗口大小，不是全图），而 `batch_size` 被窗口个数放大：

[swin_gemm.cc:50-54](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/swin/swin_gemm.cc#L50-L54)（patch_width=4、batch 被窗口放大、seq_len=window²）

```cpp
const int patch_width = 4;
const int batch_size  = batch_img * (image_width/(patch_width*window_width))
                                   * (image_width/(patch_width*window_width));
const int seq_len     = window_width * window_width;
const int inter_size  = 4 * head_num * size_per_head;
```

而且因为 Swin 每过一级就「通道加倍、token 数减半」，调优工具必须**枚举 4 个 stage 的形状**取最大 buffer，并对每一级单独调优：

[swin_gemm.cc:59-69](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/swin/swin_gemm.cc#L59-L69)（枚举 4 级：batch/4、head*2 逐级变化）

```cpp
int batch_tmp = batch_size, head_num_tmp = head_num;
for (int i = 1; i < 4; i++) {
    batch_tmp /= 4;          // 分辨率减半→窗口数减为 1/4
    head_num_tmp *= 2;       // 通道（头数）加倍
    size_t buf_size_tmp = ft::calGemmTestBufSizeInByte(
        batch_tmp, seq_len, head_num_tmp, size_per_head, 4*head_num_tmp*size_per_head, 0, is_int8, data_type);
    if (buf_size_tmp > buf_size_in_byte) buf_size_in_byte = buf_size_tmp;
}
```

最终调用的是 Swin 自己的 `generate_swin_gemm_config`（而非 BERT 的 encoder 版本），见 [swin_gemm.cc:85-103](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/swin/swin_gemm.cc#L85-L103)。

> 一句话总结差异：`vit_gemm` 的 (M,N,K) 与 BERT 同构，于是复用 `encoder_gemm_func`；`swin_gemm` 的 seq_len 退化成窗口大小、batch 被窗口数放大、且要枚举 4 级形状，于是有独立的 `swin_gemm_func`。

#### 4.4.4 代码实践

**实践目标**：用 docs 里给出的真实命令，对照两个工具的参数语义。

**操作步骤**：

1. 阅读 [docs/vit_guide.md:105-108](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/vit_guide.md#L105-L108) 的 `vit_gemm` 用法与 [docs/swin_guide.md:131-134](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/swin_guide.md#L131-L134) 的 `swin_gemm` 用法。
2. 回到 [vit_gemm.cc:25-39](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/vit/vit_gemm.cc#L25-L39) 与 [swin_gemm.cc:25-38](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/swin/swin_gemm.cc#L25-L38)，逐个参数对齐：vit 的 8 个参数 vs swin 的 7 个参数。
3. 解释为什么 `swin_gemm` 不需要 `embed_dim`、`with_cls_token` 这两个参数。

**需要观察的现象**：

- `vit_gemm` 接收 `img_size/patch_size/embed_dim`，因为 ViT 的序列长度依赖这些；
- `swin_gemm` 接收 `window_width/head_num(of first block)/size_per_head`，因为 Swin 的序列长度只由窗口决定，通道维由「头数 × size_per_head」表达，且头数会逐级翻倍（工具内部枚举），所以只需第一级的头数。

**预期结果**：你能写出一张两列对照表，说明两者参数语义不同的根因是「ViT 在全序列上做注意力，Swin 在窗口上做注意力」。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`swin_gemm` 的 `seq_len` 与 `vit_gemm` 的 `seq_len` 在数量级上典型差多少？

**参考答案**：ViT 的 seq_len 是全图 patch 数，典型 196～577；Swin 的 seq_len 是窗口大小平方，典型 \(7^2=49\) 或 \(8^2=64\)。差约一个数量级。但 Swin 的「batch」被窗口个数放大，总 token 数仍与图像尺寸成正比。

**练习 2**：为什么 `swin_gemm` 要在循环里把 `head_num` 逐级 `*2`、`batch` 逐级 `/4`？

**参考答案**：因为 Swin 是层级式结构，每过一级 patch merge，空间分辨率减半（窗口数变为原来的 1/4，故 batch/4）、通道加倍（实现上由 head_num 翻倍表达）。调优工具必须为每一级的 (M,N,K) 都选出最优 cuBLAS 算法，所以要在循环里枚举这 4 级形状。

---

## 5. 综合实践

**任务**：为「同一个图像分类任务，在 ViT 和 Swin 之间做选型」写一份**基于源码的技术备忘录**，要求覆盖以下三点，并全部标注永久链接。

1. **block 同构性**：用 [ViT.cc:329-396](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/vit/ViT.cc#L329-L396) 与 [SwinBlock.cc:130-252](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/swin/SwinBlock.cc#L130-L252) 说明：剥去 patch embed 与 shift/partition 后，两者的「注意力 + MLP」主干是否同构？各自额外多了哪一步？
2. **Swin 的两个专属 kernel**：用 [image_shift_partition_kernels.cu:26-45](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/image_shift_partition_kernels.cu#L26-L45) 与 [image_merge_kernels.cu:30-51](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/image_merge_kernels.cu#L30-L51) 说明它们分别服务于「移窗」和「patch merge」中的哪一步，并指出调用点（[SwinBlock.cc:130-140](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/swin/SwinBlock.cc#L130-L140) 与 [SwinBasicLayer.cc:127](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/swin/SwinBasicLayer.cc#L127)）。
3. **GEMM 调优策略**：用 [vit_gemm.cc:88-100](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/vit/vit_gemm.cc#L88-L100) 与 [swin_gemm.cc:50-69](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/swin/swin_gemm.cc#L50-L69) 说明为什么 ViT 能复用 BERT 的调优函数而 Swin 不能。

**交付物**：一份不超过一页的备忘录，给出「高分辨率输入 + 密集预测（检测/分割）」与「中等分辨率 + 图像分类」两种场景下分别推荐 ViT 还是 Swin，并各用一条源码依据支撑结论。

**提示**：密集预测需要高分辨率多尺度特征 → Swin 的层级式 patch merge（[Swin.cc:216-219](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/swin/Swin.cc#L216-L219)）更有利；纯分类且分辨率不高时，ViT 的全序列注意力（[ViT.cc:344-357](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/vit/ViT.cc#L344-L357)）实现更简单、与 BERT 共享调优产物。

> 本实践为「源码阅读 + 设计论证」型，无需 GPU；若要补充实测延迟，可按 docs/vit_guide.md 与 docs/swin_guide.md 的命令在本地跑 `vit_example`/`swin_example` 取数。**待本地验证**。

## 6. 本讲小结

- ViT 的本质是「**用卷积把图像切成 patch 当 token，再原样复用 BERT 的 transformer block**」；它的注意力/FFN 与 BERT 完全同款，唯一新增的是 `patchEmbed` 里的 cuDNN 卷积与「加偏置/拼 cls/加位置编码」的融合 elementwise kernel。
- Swin 用「**窗口注意力 + 移窗 + 层级式 patch merge**」把视觉注意力压到对图像尺寸近似线性；调用链是清晰的三层 `SwinTransformer → SwinTransformerBasicLayer → SwinTransformerBlock`。
- Swin 相比 ViT（及 BERT）多出**两个专属 kernel**：`image_shift_partition_kernels` 负责「循环移位 + 窗口重排」，`image_merge_kernels` 负责 patch merge 的「\(2\times2\) 空间 → 通道」重排；两者都靠在 kernel 内部算出目标下标实现单遍融合。
- `shift_size` 在一个 stage 内随 block 奇偶在 `0` 与 `window_size/2` 间交替，正是「移窗」机制；patch merge 则使每过一级 `dim *= 2`、`input_resolution /= 2`。
- GEMM 调优上，`vit_gemm` 直接复用 BERT 的 `generate_encoder_gemm_config`（序列同构），`swin_gemm` 则需独立的 `generate_swin_gemm_config`（seq_len 退化为窗口大小、batch 被窗口数放大、并要枚举 4 级形状）。
- 视觉模型同样受益于 FT 的统一抽象：`TensorMap` 接口、`BaseLayer` 的 buffer 复用、`invokeXxx` kernel 风格、Fused/Unfused 注意力分发——这些在 u2/u3 建立的基础设施被 ViT/Swin 直接继承。

## 7. 下一步学习建议

- **横向扩展到其它编码器变体**：阅读 [src/fastertransformer/models/swin/SwinBasicLayer.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/swin/SwinBasicLayer.cc) 中的 V1/V2 分支，理解 Swin V2 相对 V1 的改动（pre-LN 位置、`invokeImageMerge` vs `invokeMergeLayernorm`）；可对照 docs/swin_guide.md 的精度表。
- **纵向深入到 INT8 量化视觉模型**：本讲只覆盖 FP32/FP16 路径。建议进入 u9 量化单元，看 [vit_guide.md:265-280](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/vit_guide.md#L265-L280) 的 INT8 性能，以及 `image_shift_partition_kernels.cu` 里 `invokeShiftPartitionCol32` 如何把移窗与量化融合（本讲已点到，留作后续深读）。
- **回到调用链上下游**：若你想知道这些 C++ 模型如何被 PyTorch/TensorRT 调用，可跳到 u10（框架集成），看 ViT/Swin 的 `th_op` 与 `tensorrt_plugin` 封装；若想看「新增一个视觉变体」的完整流程，参考 u11-l2 的 templates 指引。
- **建议继续精读的源码**：`src/fastertransformer/layers/attention_layers/WindowAttention.{h,cu}`（窗口注意力的 QKV + 相对位置偏置实现）、`src/fastertransformer/utils/conv2d.{h,cc}`（patch embed 用的 cuDNN 卷积封装）。

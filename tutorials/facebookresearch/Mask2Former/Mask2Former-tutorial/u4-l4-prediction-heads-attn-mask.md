# u4-l4 预测头与 Attention Mask 生成

## 1. 本讲目标

上一讲（u4-l3）我们读完了 `MultiScaleMaskedTransformerDecoder.forward` 的 masked attention 主循环，但把每层末尾调用的 `forward_prediction_heads` 当黑盒留到了本讲。学完本讲，你应该能够：

1. 说清 `forward_prediction_heads` 这个「一进三出」函数的三份输出分别是什么、被谁消费。
2. 掌握 `einsum("bqc,bchw->bqhw")` 生成 per-mask 预测的方式，理解 mask 嵌入就是每个 query 专属的「动态 1×1 卷积核」。
3. 逐步推导 attention mask 的形状变换链：下采样 → sigmoid → 阈值 0.5 → bool → 复制到多头 → detach。
4. 解释 `.detach()` 在这里的真实作用（防御性 + 明确设计意图），而不是背一个似是而非的理由。
5. 理解 `_set_aux_loss` 如何把「初始 query 预测 + 每层预测」打包成深监督需要的 `aux_outputs`。

## 2. 前置知识

本讲建立在前几讲已建立的概念之上，先用两分钟复习：

- **mask classification 范式**（u1-l1、u3-l1）：模型不再逐像素分类，而是输出 Q 个 query，每个 query 同时给出「类别 logits（K+1 维，含 no-object）」和「一张 mask」。COCO 全景分割下 K=133、Q=100，所以类别 logits 是 134 维。
- **(L, B, C) 序列格式**（u4-l2）：Transformer 积木内部用 `(序列长度, 批大小, 通道)`；query 序列长度是 Q=100，图像 token 序列长度是 H·W。
- **masked attention 主循环**（u4-l3）：每层顺序为 cross→self→FFN；cross-attention 的 `memory_mask` 中 **True 表示禁止注意**；层与层之间在 1/32、1/16、1/8 三个尺度间轮转，所以 mask 的目标尺寸也在轮转。
- **像素解码器的输出契约**（u3-l2、u3-l5）：`mask_features` 是 1/4 分辨率的逐像素嵌入（通道 256），`multi_scale_features` 依次为 1/32、1/16、1/8。本讲的预测头吃前者生成 mask，attention mask 的尺寸则要跟随后者。
- **深监督**（u3-l1 已预告，u5-l3 将展开）：每一组预测都参与损失，`weight_dict` 由此从 3 个键展开成 30 个键。

一个术语先行澄清：本讲的「预测头（prediction head）」指 decoder 输出之后的小网络（`class_embed` 线性层 + `mask_embed` MLP），与 u4-l2 讲过的解码器内部积木 `FFNLayer` 不是一回事——后者是 Transformer 层的组成部分，前者是「把 query 向量翻译成预测」的出口。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py` | 本讲主角：`MLP`、`forward_prediction_heads`、`_set_aux_loss` 都在这个文件里 |
| `configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml` | 实践所用超参的事实来源：`NUM_CLASSES=133`、`NUM_OBJECT_QUERIES=100`、`HIDDEN_DIM=256`、`MASK_DIM=256`、`NHEADS=8`、`DEC_LAYERS=10` |
| `mask2former/config.py` | 上述配置项的默认值注册处（本讲取值与默认一致或被 yaml 显式覆盖） |
| `mask2former/modeling/transformer_decoder/maskformer_transformer_decoder.py` | v1 解码器，作为对照：它把预测头内联在 `forward` 里，本讲末尾会对比 |

## 4. 核心概念与源码讲解

### 4.1 forward_prediction_heads：一个函数、三份输出

#### 4.1.1 概念说明

`forward_prediction_heads` 是 masked attention 闭环的「合拢点」。上一讲我们看到主循环每走完一层就调用它一次，它做的事可以概括为**一进三出**：

- **一进**：当前 query 序列 `output`，形状 (Q, B, C)，加上像素解码器给的 `mask_features`，形状 (B, C, H, W)。
- **三出**：
  1. `outputs_class`——每个 query 的类别 logits，(B, Q, K+1)；
  2. `outputs_mask`——每个 query 的 mask **logits**（注意：没有过 sigmoid），(B, Q, H, W)；
  3. `attn_mask`——给**下一层** cross-attention 用的二值禁止掩码，(B·h, Q, h'·w')。

为什么要把三件事放在一个函数里？因为 masked attention 的核心机制是「**用自己刚预测的 mask 限制自己下一步能看哪里**」：query 在第 i 层末尾预测出一张 mask，第 i+1 层的 cross-attention 就只允许它注意这张 mask 覆盖的区域。预测和掩码必须同源同时刻生成，天然属于一个函数。这也解释了为什么主循环一共调用它 **DEC_LAYERS 次**（配置值 10 = 1 次初始 query 预测 + 9 层各一次）。

#### 4.1.2 核心流程

```text
输入: output (Q,B,C), mask_features (B,C,H,W), attn_mask_target_size (h',w')
1. decoder_norm(output) 并 transpose(0,1)      → decoder_output (B,Q,C)
2. outputs_class = class_embed(decoder_output)  → (B,Q,K+1)
3. mask_embed    = MLP(decoder_output)          → (B,Q,mask_dim)
4. outputs_mask  = einsum("bqc,bchw->bqhw")     → (B,Q,H,W)  logits
5. attn_mask     = interpolate(outputs_mask, (h',w'))   → (B,Q,h',w')
6. attn_mask     = (sigmoid(attn_mask) < 0.5) 展平并复制到 B·h 个头 → (B·h,Q,h'·w') bool
7. attn_mask     = attn_mask.detach()
返回: (outputs_class, outputs_mask, attn_mask)
```

#### 4.1.3 源码精读

先看两个调用点，理解「谁在什么时候喂参数」。初始调用发生在任何 decoder 层之前，直接吃可学习的 `query_feat`，目标尺寸取 `size_list[0]`（1/32），因为第 0 层 cross-attention 读的是 1/32 尺度：

[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:391-394](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L391-L394) —— 在 learnable query features 上先做一次预测，产生第 0 组输出和第 0 层要用的 attn_mask。

每层末尾再调用一次，目标尺寸取 `size_list[(i + 1) % 3]`——对齐**下一层**要读的尺度：

[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:418-420](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L418-L420) —— 第 i 层 cross→self→FFN 结束后立刻再预测，供深监督收集，并产出第 i+1 层的 attn_mask。

函数本体只有 15 行：

[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:433-448](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L433-L448) —— 预测头主体：LayerNorm + 转置后分别过分类线性层与 mask 嵌入 MLP，用 einsum 生成 mask，再把 mask 下采样阈值化成下一层的 attention mask。

其中第 434-435 行是序列格式的「最后一跳」：

```python
decoder_output = self.decoder_norm(output)        # (Q,B,C) → (Q,B,C)，LayerNorm
decoder_output = decoder_output.transpose(0, 1)   # (Q,B,C) → (B,Q,C)
```

[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:434-435](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L434-L435) —— 先过 `decoder_norm`（构造于 [L312](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L312) 的 LayerNorm），再从 (Q,B,C) 转回 (B,Q,C)。对第 0 步尤其必要：`query_feat` 是未经任何层处理的裸嵌入，归一化后预测才稳定。

后面三行的分工（分类、嵌入、点积）在 4.2 精读；attn_mask 三行在 4.3 精读。

#### 4.1.4 代码实践

这是本讲的主实践，直接单独调用 `forward_prediction_heads`，验证形状契约与 True 比例。

**实践目标**：给定 (100,2,256) 的 decoder 输出和 (2,256,128,128) 的 mask_features，验证三份输出的形状，并统计 attn_mask 中 True（禁止注意）像素的比例。

**操作步骤**（前置条件：按 u1-l2 装好环境，`import mask2former` 可用；CPU 即可）：

```python
# practice_u4l4.py（示例代码，非项目原有文件）
import torch
from mask2former.modeling.transformer_decoder.mask2former_transformer_decoder import (
    MultiScaleMaskedTransformerDecoder,
)

torch.manual_seed(0)

# 超参加自 configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml
decoder = MultiScaleMaskedTransformerDecoder(
    in_channels=256, mask_classification=True,
    num_classes=133, hidden_dim=256, num_queries=100,
    nheads=8, dim_feedforward=2048,
    dec_layers=9,              # 注意：DEC_LAYERS=10，实际构建 10-1=9 层
    pre_norm=False, mask_dim=256, enforce_input_project=False,
)

output = torch.randn(100, 2, 256)              # (Q, B, C)，主循环里的 output
mask_features = torch.randn(2, 256, 128, 128)  # (B, C, H, W)，1/4 分辨率嵌入

cls, mask, attn_mask = decoder.forward_prediction_heads(
    output, mask_features, attn_mask_target_size=(32, 32),  # 下一层读 1/32 尺度
)
print(cls.shape)        # 期望 (2, 100, 134)
print(mask.shape)       # 期望 (2, 100, 128, 128)
print(attn_mask.shape)  # 期望 (16, 100, 1024)，16 = B·h = 2×8
print("True（禁止注意）比例:", attn_mask.float().mean().item())
```

说明：`MultiScaleMaskedTransformerDecoder` 的构造函数被 detectron2 的 `@configurable` 装饰，直接用全量关键字参数调用即走原生 `__init__`，不必组装 cfg；`in_channels == hidden_dim` 且 `enforce_input_project=False` 时 `input_proj` 是空 `nn.Sequential`（恒等映射），不影响本实践。

**需要观察的现象**：

1. 三份输出的形状是否与期望一致；
2. `attn_mask` 最后一维 1024 = 32×32，恰好是 1/32 尺度的 token 数；
3. True 比例的数值。

**预期结果**：形状分别为 `(2,100,134)`、`(2,100,128,128)`、`(16,100,1024)`。True 比例应在 **0.5 附近浮动**：随机初始化下 `outputs_mask` 的 logits 近似零均值对称分布，`sigmoid(logit) < 0.5` 等价于 `logit < 0`，概率约为一半；换随机种子数值会漂移（例如 0.3~0.7 都正常）。精确数值**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：配置 `DEC_LAYERS=10` 时，一次完整 forward 会调用 `forward_prediction_heads` 几次？

答案：10 次——进入 decoder 层之前 1 次（吃 `query_feat`），9 个已构建层各 1 次。这也是「DEC_LAYERS 减 1」的由来：初始 query 预测顶替了一层。

**练习 2**：若把 `attn_mask_target_size` 传成 `(64, 64)`，返回的 `attn_mask` 形状是什么？

答案：`(16, 100, 4096)`。最后一维 = 64×64 = 4096（1/16 尺度的 token 数），第一维 = B·num_heads = 2×8 = 16，与目标尺度无关。

**练习 3**：`outputs_class` 最后一维为什么是 134 而不是 133？

答案：`class_embed` 输出 `num_classes + 1` 维，多出的一维是 **no-object 通道**——query 没匹配到任何真实实例时，监督它把概率质量放在这一类（见 u5-l2 的 `empty_weight` 降权）。

### 4.2 预测头双通道：class_embed、mask_embed（MLP）与 einsum

#### 4.2.1 概念说明

预测头是一对「双生子」，从同一份 (B,Q,256) 的 decoder 输出出发，兵分两路：

- **分类通道**：`class_embed = nn.Linear(256, K+1)`，一个线性层直接输出每个 query 在 K+1 个类别上的 logits。
- **mask 通道**：`mask_embed = MLP(256, 256, mask_dim, 3)`，把每个 query 向量嵌入到与 `mask_features` 同维（256）的空间，再与逐像素嵌入做**点积**，得到该 query 的 mask。

关键直觉：mask 嵌入向量 \(\mathbf{e}_q\)（256 维）作用上 `mask_features` \(\mathbf{f}_{h,w}\)（每像素 256 维），数学上等价于**每个 query 拥有一枚专属的 1×1 卷积核，在共享的高分辨率特征图上滑动一次**。这就是 mask classification 范式「Q 个 query → Q 张 mask」的实现本质。用公式写：

\[
m_q^{(b)}(h,w) \;=\; \mathbf{e}_q^{(b)\top}\,\mathbf{f}^{(b)}(h,w)
\]

即 einsum `"bqc,bchw->bqhw"` 中固定 q 后的那一步。相比真正输出 Q 张全分辨率 mask 的卷积分支，这种「嵌入 + 点积」参数共享让参数量与 Q 无关（MLP 只有一组权重，所有 query 共用）。

#### 4.2.2 核心流程

```text
decoder_output (B,Q,256)
  ├─ class_embed: Linear(256 → K+1)      → outputs_class (B,Q,134)
  └─ mask_embed:  MLP(256 → 256 → 256)   → mask_embed (B,Q,256)
                      │
                      └─ einsum("bqc,bchw->bqhw") with mask_features
                                        → outputs_mask (B,Q,H,W)
```

#### 4.2.3 源码精读

两个头在构造函数中定义：

[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:331-334](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L331-L334) —— 定义输出头：`class_embed` 是输出 K+1 维的线性层；`mask_embed` 是 3 层 MLP，输出维度为 `mask_dim`（=256，与 mask_features 通道一致，这是 einsum 能成立的前提）。

`MLP` 类从 DETR 移植而来，总共 13 行：

[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:192-204](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L192-L204) —— 通用多层感知机：`num_layers` 个 `nn.Linear` 堆叠，前 `num_layers-1` 层后接 ReLU，最后一层线性输出。

```python
h = [hidden_dim] * (num_layers - 1)
self.layers = nn.ModuleList(nn.Linear(n, k) for n, k in zip([input_dim] + h, h + [output_dim]))
...
x = F.relu(layer(x)) if i < self.num_layers - 1 else layer(x)
```

[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:198-203](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L198-L203) —— 用 `zip([input_dim]+h, h+[output_dim])` 生成 (256→256, 256→256, 256→256) 三层；最后一层不加 ReLU。

为什么最后一层不加 ReLU？因为 mask 嵌入随后要去做点积产出 **logits**，logits 需要正负两侧取值；若把嵌入截断在非负象限，点积结果的表达能力会被严重限制。

预测头的三行核心：

[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:436-438](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L436-L438) —— 分类线性层产出 logits；MLP 产出 mask 嵌入；einsum 把「每 query 一个 256 维嵌入」与「每像素一个 256 维嵌入」批量做内积，直接得到 (B,Q,H,W) 的 mask logits。

注意 `outputs_mask` 是 logits，不是概率。sigmoid 留给两个下游：训练时的 BCE/dice 损失（u5-l2）和推理时的语义/实例融合（u6）。

顺带一提 v1 对照：MaskFormer 一代的 `TransformerDecoder` 把同样的逻辑写死在 forward 里，一次性处理所有层（`"lbqc,bchw->lbqhw"` 带 batch 前面的层数维）：

[mask2former/modeling/transformer_decoder/maskformer_transformer_decoder.py:146-147](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/maskformer_transformer_decoder.py#L146-L147) —— v1 对整个 (层数×批×query×通道) 张量做 einsum；v2 把它拆成逐层调用的 `forward_prediction_heads`，因为每层预测还要即时变成下一层的 attn_mask——这正是 masked attention 带来的结构性变化。

#### 4.2.4 代码实践

**实践目标**：单独验证 `MLP` 的形状与参数量，建立「嵌入点积 = 动态 1×1 卷积」的手感。

**操作步骤**（示例代码）：

```python
import torch
from mask2former.modeling.transformer_decoder.mask2former_transformer_decoder import MLP

mlp = MLP(input_dim=256, hidden_dim=256, output_dim=256, num_layers=3)
x = torch.randn(2, 100, 256)          # (B, Q, C)
e = mlp(x)
print(e.shape)                        # (2, 100, 256)
print(sum(p.numel() for p in mlp.parameters()))
# 3 × (256×256 + 256) = 197,376

# 「动态 1×1 卷积」等价性验证：逐 query 手工点积 vs einsum
f = torch.randn(2, 256, 8, 8)          # (B, C, H, W)
m_einsum = torch.einsum("bqc,bchw->bqhw", e, f)
m_manual = torch.einsum("qc,chw->qhw", e[0, 7], f[0])   # 第 0 张图、第 7 个 query
print(torch.allclose(m_einsum[0, 7], m_manual, atol=1e-5))  # True
```

**需要观察的现象**：einsum 与手工逐 query 点积结果一致；MLP 参数量 197,376 与手算 3×65,792 相符。

**预期结果**：两处均打印 True / 197376。该验证只依赖 PyTorch 随机张量与 `MLP` 类本身，结果是确定性的（浮点误差内 allclose 成立）；具体数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`mask_embed = MLP(hidden_dim, hidden_dim, mask_dim, 3)` 一共几层 Linear？若 `hidden_dim=mask_dim=256`，参数量是多少？

答案：3 层。每层 256×256 权重 + 256 偏置 = 65,792，共 197,376。

**练习 2**：如果把 `mask_dim` 配成 512 而 `mask_features` 仍是 256 通道，会发生什么？

答案：einsum 的 `bqc` 与 `bchw` 在 c 维上不匹配（512 ≠ 256），运行时直接报错。这就是 `MASK_DIM` 必须等于像素解码器输出的 `mask_features` 通道数的原因。

**练习 3**：用一句话向同学解释 `mask_embed` 嵌入的作用。

答案：每个 query 的 256 维嵌入相当于一枚专属的 1×1 卷积核，作用在共享的 1/4 分辨率逐像素特征上，「滑」出这个 query 的 mask logits。

### 4.3 从预测 mask 到 attention mask：下采样、阈值化、多头展开、detach

#### 4.3.1 概念说明

这是本讲最精妙的五行程。它回答的问题：**第 i 层刚预测出的 (B,100,128,128) mask logits，怎么变成第 i+1 层 cross-attention 可用的 memory_mask？**

三件事必须发生：

1. **尺寸对齐**：cross-attention 的 key 是下一层尺度的图像 token（1/32 时只有 32×32=1024 个），掩码的最后一维必须等于 key 数。所以先把 1/4 分辨率的预测 mask 双线性下采样到目标尺度——顺带把数据量从 100×128×128 压到 100×32×32。
2. **离散化**：attention mask 是 bool 语义（True=禁止）。对下采样后的 logits 取 sigmoid 再与 0.5 比较，得到「预测概率低于 0.5 的位置一律禁止注意」的二值掩码。
3. **多头展开**：`nn.MultiheadAttention` 的 3D attn_mask 期望形状是 (N·num_heads, L, S)——同一张空间掩码要在每个注意力头上各放一份（此处所有头共享同一掩码）。

写成公式（\(\tilde m\) 为下采样后的 mask logits，\(j\) 为下一层尺度的 token 下标）：

\[
A[b, q, j] \;=\; \mathbb{1}\!\left[\,\sigma\!\big(\tilde m_q^{(b)}(j)\big) < 0.5\,\right], \qquad j = 1, \dots, h'\!\cdot\!w'
\]

至于 `.detach()`：严格说，`< 0.5` 的比较已经产生不可导的 bool 张量，梯度在比较这一步就断了。因此 detach 在数值梯度上**没有实际改变**，它的价值在于把设计意图写进代码——「掩码只是路由提示，不是损失通路」，并防御未来修改（比如有人把阈值化换成可导的软掩码）时意外接通梯度回路。这正是本讲实践要求你解释的问题，4.3.4 会让你亲手验证。

#### 4.3.2 核心流程

源码注释里自带形状账本，补上具体数字（B=2, Q=100, h=8, 目标尺度 32×32, mask_features 128×128）：

```text
outputs_mask (2, 100, 128, 128)
  │ F.interpolate → (32,32)          # [B, Q, H, W] → [B, Q, 32, 32]
  │ .sigmoid().flatten(2)            # → [B, Q, 1024]
  │ .unsqueeze(1)                    # → [B, 1, Q, 1024]
  │ .repeat(1, num_heads, 1, 1)      # → [B, 8, Q, 1024]
  │ .flatten(0, 1)                   # → [16, Q, 1024]  即 [B·h, Q, hw]
  │ < 0.5 → .bool()                  # True = 概率低于 0.5 = 禁止注意
  └ .detach()                        # 切断与预测头的关联（防御性声明）
```

#### 4.3.3 源码精读

五行程原文：

[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:440-446](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L440-L446) —— 先把高分辨率预测 mask 双线性插值到下一层尺度，再 sigmoid 阈值化为 bool 掩码、按注意力头复制展开，最后 detach。

逐行看：

```python
attn_mask = F.interpolate(outputs_mask, size=attn_mask_target_size, mode="bilinear", align_corners=False)
```

[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:442](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L442) —— 对 **logits**（而非概率）做双线性下采样。bool 没法双线性插值，先插 logits 再阈值化能保留平滑的亚像素信息；`align_corners=False` 与 u4-l1 可变形采样的坐标约定一致。

```python
# [B, Q, H, W] -> [B, Q, H*W] -> [B, h, Q, H*W] -> [B*h, Q, HW]
attn_mask = (attn_mask.sigmoid().flatten(2).unsqueeze(1).repeat(1, self.num_heads, 1, 1).flatten(0, 1) < 0.5).bool()
```

[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:441-445](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L441-L445) —— 注释即形状账本；`num_heads` 取自构造时的 `nheads`（COCO 配置为 8）。阈值语义与源码注释一致：BoolTensor 中 True 的位置**不允许注意**。

```python
attn_mask = attn_mask.detach()
```

[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:446](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L446) —— detach 返回共享存储的视图、不复制数据，开销可忽略；如上所述，它的作用是声明式与防御性的。

最后别忘了消费侧的兜底（u4-l3 已讲，这里闭环）：若某个 query 的 mask 全图概率都低于 0.5，它在下一层将被整行禁止注意，softmax 分母为零产生 NaN。主循环在用掩码之前把全 True 的行整体置 False：

[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:397-398](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L397-L398) —— `attn_mask[torch.where(attn_mask.sum(-1) == attn_mask.shape[-1])] = False`：sum 为全长（全 True）的行被放开。

#### 4.3.4 代码实践

**实践目标**：复现这五行程并回答两个问题——True 比例是多少？`.detach()` 到底切断了什么？

**操作步骤**（示例代码，可接在 4.1.4 的脚本后）：

```python
import torch.nn.functional as F

with torch.no_grad():
    cls, mask, attn_mask = decoder.forward_prediction_heads(
        output, mask_features, attn_mask_target_size=(32, 32))

# 1) True（禁止注意）比例
print("True 比例:", attn_mask.float().mean().item())

# 2) 手工复现形状链与阈值，核对一致
m = F.interpolate(mask, size=(32, 32), mode="bilinear", align_corners=False)
manual = (m.sigmoid().flatten(2).unsqueeze(1)
           .repeat(1, 8, 1, 1).flatten(0, 1) < 0.5).bool()
print(torch.equal(attn_mask, manual))   # True

# 3) detach 切断了什么？给输入开梯度再看
output_g = torch.randn(100, 2, 256, requires_grad=True)
cls_g, mask_g, am_g = decoder.forward_prediction_heads(
    output_g, mask_features, attn_mask_target_size=(32, 32))
print(am_g.requires_grad)               # False：bool 比较 + detach 双重切断
mask_g.sum().backward()
print(output_g.grad is not None)         # True：梯度只经 outputs_mask/outputs_class 回流
```

**需要观察的现象**：手工复现与函数返回值完全相等；`attn_mask.requires_grad` 为 False；对 `outputs_mask` 反传时梯度正常流回 `output_g`——说明 detach 只针对掩码通路。

**预期结果**：`torch.equal` 为 True；`am_g.requires_grad` 为 False；`output_g.grad` 非 None。True 比例约 0.5（理由见 4.1.4），具体数值随种子浮动，**待本地验证**。

**关于 detach 的参考答案**（实践要求解释的问题）：`< 0.5` 的比较本身不可导，梯度在此已经断了，所以去掉 `.detach()` 不会改变训练数值——它的作用是（1）把「掩码只是给下一层注意力的路由提示、绝不参与损失回传」这一设计意图显式写进代码；（2）防御性编程：若未来有人把阈值化替换成可导的软掩码，detach 能阻止梯度从第 i+1 层的注意力经掩码意外回流到第 i 层的预测头，避免优化目标被悄悄改变。

#### 4.3.5 小练习与答案

**练习 1**：attn_mask 的 True 在 `nn.MultiheadAttention` 里意味着什么？与 u4-l2 的 `memory_mask` 是什么关系？

答案：True 表示对应 key 位置被禁止注意（softmax 前加 -inf）。它就是 `CrossAttentionLayer` 的 `memory_mask` 实参——`forward` 主循环 L402 传入的 `memory_mask=attn_mask`。

**练习 2**：为什么先 interpolate 再阈值化，而不是先阈值化再 interpolate？

答案：bool 张量没有「双线性插值」的定义；对 logits 插值保留了跨像素的平滑信息，再阈值化得到空间上连贯的掩码。反过来只能做最近邻投票，边界更粗糙。（工程上还有动机：掩码必须在低分辨率上工作，因为其最后一维必须等于 key 序列长度。）

**练习 3**：`repeat(1, self.num_heads, 1, 1)` 复制出的 8 份掩码，不同头的掩码内容一样吗？这是缺陷吗？

答案：一样，所有头共享同一张空间掩码。这是设计选择而非缺陷——掩码语义是「这个 query 该看哪些位置」，与头无关；多头差异交给注意力权重本身表达。

### 4.4 _set_aux_loss：深监督的输出打包

#### 4.4.1 概念说明

主循环把每次预测都收集进 `predictions_class` / `predictions_mask` 两个列表（DEC_LAYERS=10 时各 10 项），最后组装成输出 dict。**深监督（deep supervision）**要求每一组中间预测都参与损失计算——这是 DETR 系列训练收敛的关键技巧：只监督最后一层，浅层 query 得不到有效梯度信号。

`_set_aux_loss` 负责把「除最后一组之外」的所有中间预测打包成 `aux_outputs` 列表，每组是 `{"pred_logits": ..., "pred_masks": ...}` 的小 dict。为什么最后一组不在里面？因为它已经作为 `pred_logits` / `pred_masks` 放在输出 dict 顶层了，放进去会重复计算。

回忆 u4-l3 的伏笔：构建层数取 `DEC_LAYERS - 1`，正是因为可学习 `query_feat` 自带第 0 组受监督的预测——源码注释写明「辅助损失数量 = decoder 层数」的不变式要靠减一来维持。

#### 4.4.2 核心流程

```text
predictions_class（10 项）, predictions_mask（10 项）
  │ 最后一项 → out["pred_logits"] / out["pred_masks"]
  └ 其余 9 项 zip 成对 → aux_outputs = [
        {"pred_logits": c0, "pred_masks": m0},   # 第 0 组：query_feat 的预测
        {"pred_logits": c1, "pred_masks": m1},   # 第 1 层输出
        ... 共 9 组
      ]
```

下游（u5-l1/u5-l3）：`SetCriterion` 对每组 aux 重新做匈牙利匹配再算损失，`weight_dict` 用 `_0.._8` 后缀键加权——3 个基础键 × 10 组 = 30 个损失键，对应 u2-l1 观察到的 30 个 loss 日志项。

#### 4.4.3 源码精读

输出 dict 的组装：

[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:424-431](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L424-L431) —— 顶层键 `pred_logits`/`pred_masks` 取两个列表的**最后一项**（最终层预测），其余交给 `_set_aux_loss`；断言保证列表长度 = 层数 + 1（[L422](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L422)）。

[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:450-461](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L450-L461) —— `_set_aux_loss`：切掉两个列表的最后一个元素，zip 成小 dict 列表；`mask_classification=False` 分支只打包 masks（本项目 meta arch 已断言必须为 True，此分支实际不可达，保留是为兼容 per-pixel 基线等用法）。

两个值得注意的细节：

- 装饰器 `@torch.jit.unused` 与源码注释说明：TorchScript 不支持「值类型异构的 dict」（顶层 dict 里既有 Tensor 又有 list），脚本化时跳过此方法、走 Python 回退。
- `zip(outputs_class[:-1], outputs_seg_masks[:-1])` 的 `[:-1]` 就是「去掉最后一组」的实现，和顶层键正好互补、不多不少。

#### 4.4.4 代码实践

**实践目标**：跑通整个 decoder forward，核对输出 dict 的结构与 `aux_outputs` 的数量关系。

**操作步骤**（示例代码，构造三尺度随机特征 + 1/4 分辨率 mask_features）：

```python
import torch
from mask2former.modeling.transformer_decoder.mask2former_transformer_decoder import (
    MultiScaleMaskedTransformerDecoder,
)

decoder = MultiScaleMaskedTransformerDecoder(
    in_channels=256, mask_classification=True, num_classes=133, hidden_dim=256,
    num_queries=100, nheads=8, dim_feedforward=2048, dec_layers=9,
    pre_norm=False, mask_dim=256, enforce_input_project=False,
)

# 三尺度：1/32、1/16、1/8（顺序契约见 u3-l2/u3-l5）；mask_features 为 1/4
src = [torch.randn(2, 256, 32, 32), torch.randn(2, 256, 64, 64), torch.randn(2, 256, 128, 128)]
mask_features = torch.randn(2, 256, 128, 128)

with torch.no_grad():
    out = decoder(src, mask_features)

print(out["pred_logits"].shape)      # (2, 100, 134)
print(out["pred_masks"].shape)       # (2, 100, 128, 128)
print(len(out["aux_outputs"]))       # 9 = 构建层数（DEC_LAYERS=10 减 1）
print(out["aux_outputs"][0].keys())  # dict_keys(['pred_logits', 'pred_masks'])
```

**需要观察的现象**：输出 dict 恰有三个顶层键；`aux_outputs` 长度等于构建层数 9；每个元素是只含两个键的小 dict。

**预期结果**：如注释所示。`forward` 对 `src` 的尺度顺序没有硬校验（只用形状记账），但语义上必须按 1/32、1/16、1/8 传入才能与预训练权重对齐。CPU 上几秒内可完成，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：DEC_LAYERS=10 时 `aux_outputs` 有几组？加上顶层共几组预测参与监督？

答案：9 组 aux + 1 组顶层 = 10 组，恰好等于配置的 DEC_LAYERS——「每个配置层一组监督」的不变式由此保持。

**练习 2**：`aux_outputs[-1]["pred_logits"]` 与 `out["pred_logits"]` 各来自哪次预测？

答案：前者是 `predictions_class[-2]`，即第 8 个 decoder 层（倒数第二组）的输出；后者是 `predictions_class[-1]`，最终层输出。

**练习 3**：`@torch.jit.unused` 装饰器解决什么问题？

答案：TorchScript 无法处理同一 dict 中既有 Tensor 又有 list 的异构值类型，该装饰器让脚本化时跳过此方法、回退到 Python 执行。

## 5. 综合实践

**任务：给预测头装上「行车记录仪」，观察 masked attention 的尺度轮转。**

把 4.4.4 的完整 forward 稍加改造，用猴子补丁（monkey-patch）记录每次 `forward_prediction_heads` 的 `attn_mask_target_size` 与 True 比例，验证你在 u4-l3 学到的轮转规律在本讲函数上的落点：

```python
#（示例代码）
orig = decoder.forward_prediction_heads
log = []
def spy(output, mask_features, attn_mask_target_size):
    cls, mask, am = orig(output, mask_features, attn_mask_target_size)
    log.append((tuple(attn_mask_target_size), am.float().mean().item()))
    return cls, mask, am
decoder.forward_prediction_heads = spy

with torch.no_grad():
    out = decoder(src, mask_features)

for i, (size, ratio) in enumerate(log):
    print(f"第 {i} 次调用: 目标尺度 {size}, True 比例 {ratio:.3f}")
```

要做的事：

1. 解释打印出的目标尺度序列为什么是 32→64→128→32→…（对照 [L392](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L392) 的 `size_list[0]` 与 [L418](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L418) 的 `size_list[(i + 1) % 3]`）。
2. 核对调用总次数 = 10、`aux_outputs` = 9，把三个数字（DEC_LAYERS、调用次数、aux 组数）的关系写成一句话。
3. （可选进阶）把随机初始化的 `decoder.mask_embed.layers[-1].weight` 乘以 10 放大 mask logits，重复实验，观察 True 比例向 0 或 1 两极分化——这解释了为什么训练初期（预测接近均匀 0.5）与收敛后（预测自信）的掩码稀疏度完全不同。

预期：尺度序列为 (32,32),(64,64),(128,128),(32,32),… 共 10 项；第 3 项观察到的现象方向（两极化）是确定的，具体数值待本地验证。

## 6. 本讲小结

- `forward_prediction_heads` 是「一进三出」的合拢点：同一份 decoder 输出同时产出类别 logits（`class_embed` 线性层 → (B,Q,K+1)）、mask logits（`mask_embed` MLP + `einsum("bqc,bchw->bqhw")` → (B,Q,H,W)）和下一层的 attention mask。
- mask 嵌入 = 每个 query 专属的动态 1×1 卷积核，与 `mask_features` 逐像素点积生成 per-mask 预测；`MASK_DIM` 必须等于 mask_features 通道数，否则 einsum 报错。
- attention mask 的生成链：logits 双线性下采样到下一层尺度 → sigmoid → `< 0.5` 转 bool（True=禁止注意）→ 复制到 B·num_heads 份；全 True 的行由主循环在使用前置 False 兜底防 NaN。
- `.detach()` 在数值梯度上无实际作用（bool 比较已断梯度），它是「掩码不参与损失回传」的显式声明与软掩码防御。
- `_set_aux_loss` 把除最后一组外的所有中间预测打包成 `aux_outputs`，使监督组数恰等于 DEC_LAYERS，支撑深监督与 30 个损失键的 weight_dict 展开。

## 7. 下一步学习建议

预测头产出的 `pred_logits` / `pred_masks` / `aux_outputs` 是**训练监督**的直接输入，下一讲 u5-l1（匈牙利匹配器 `HungarianMatcher`）正好接住它们：代价矩阵如何用分类代价 + 采样点上的 mask/dice 代价把 100 个 query 与 GT 一一配对。建议阅读顺序：

1. `mask2former/modeling/matcher.py` 的 `memory_efficient_forward`，留意它消费的正是本讲的 `pred_logits` 与 `pred_masks`。
2. 对照 `mask2former/modeling/criterion.py` 中 `SetCriterion.forward` 的 aux 循环（每组重新匹配），把本讲 4.4 的打包逻辑与损失侧的消费逻辑对上。
3. 想看「每 query 一张 mask」在推理端如何融合回 per-class 概率图，可提前浏览 `mask2former/maskformer_model.py` 的 `semantic_inference`（u6-l1 会精读）。

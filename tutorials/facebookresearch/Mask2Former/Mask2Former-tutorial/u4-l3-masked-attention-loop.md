# Masked Attention 主循环：MultiScaleMaskedTransformerDecoder

## 1. 本讲目标

上一讲（u4-l2）我们认识了 Transformer 解码器的三类积木：SelfAttentionLayer、CrossAttentionLayer、FFNLayer 与位置编码。本讲把这些积木装回主机，精读 `MultiScaleMaskedTransformerDecoder.forward`——Mask2Former 命名中的 "Masked" 正是发生在这里。

学完本讲你应该能：

1. 说清 `query_feat`（内容嵌入）与 `query_embed`（位置嵌入）双嵌入各自的角色，以及为什么第 0 组预测直接作用在 learnable query 上。
2. 解释 `level_index = i % 3` 的三尺度轮转 cross-attention：每层看哪个尺度、attn_mask 尺寸如何跟着轮转对齐。
3. 逐行讲出主循环的层顺序 cross → self → FFN，以及每层结束后的再预测。
4. 说明 `attn_mask` 中全 True 行被置 False 的兜底逻辑：它防的是什么、为什么必须防。
5. 通过独立脚本实测「开启 / 关闭 masked attention」对 `pred_logits` 的影响。

## 2. 前置知识

本讲承接 u4-l2 与 u3-l5 的认知，只补充三个新术语：

- **query（查询向量）**：Mask2Former 用 \( Q=100 \) 个向量代表「100 个候选分割实例」。每个 query 最终产出一份类别 logits 和一份 mask 嵌入。query 不是一个具体物体，训练时由匈牙利匹配（u5-l1）把它与某个真实物体绑定。
- **内容嵌入 vs 位置嵌入**：Transformer 里一个 token 的信息可以拆成「我是什么」（内容）与「我在哪」（位置）。本项目把两者分开存：`query_feat` 是内容，`query_embed` 是位置；注意力计算时位置只加进 Q/K 参与打分，不进 V（不污染传递的内容），这是 u4-l2 讲过的 `with_pos_embed` 机制。
- **attention mask 语义**：PyTorch `nn.MultiheadAttention` 的 `attn_mask` 是布尔张量，**True 表示禁止注意**，False 表示正常注意。masked attention 的"mask"就是用它把 query 的视野限制在自己预测的前景区域内。

还需记住上游契约（u3-l5 已建立）：解码器收到的 `x` 是长度为 3 的多尺度特征列表，依次为 1/32、1/16、1/8 分辨率、各 256 通道；`mask_features` 是 1/4 分辨率的逐像素嵌入，最终 mask 由它点积产生。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py) | 本讲主角：SelfAttentionLayer / CrossAttentionLayer 定义与 `MultiScaleMaskedTransformerDecoder` 全部实现 |
| [mask2former/modeling/transformer_decoder/maskformer_transformer_decoder.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/maskformer_transformer_decoder.py) | 项目自建 `TRANSFORMER_DECODER_REGISTRY` 与 `build_transformer_decoder`（u3-l2 已讲），本讲只引用 |
| [mask2former/modeling/transformer_decoder/position_encoding.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/position_encoding.py) | `PositionEmbeddingSine` 正弦位置编码（u4-l2 已精读），本讲看它如何按尺度生成 `pos` |
| [mask2former/modeling/meta_arch/mask_former_head.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/mask_former_head.py) | 调用方：`MaskFormerHead.layers` 把 pixel decoder 的三元组喂给本讲的 forward |
| [configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml) | 默认配置：DEC_LAYERS=10、100 query、8 heads 等本讲参数的取值来源 |

## 4. 核心概念与源码讲解

本讲拆四个最小模块：构造器与双嵌入、多尺度输入准备、尺度轮转主循环、attn_mask 兜底逻辑。

### 4.1 构造器：双查询嵌入、level_embed 与「减一层」的由来

#### 4.1.1 概念说明

`MultiScaleMaskedTransformerDecoder` 注册在项目自建的 `TRANSFORMER_DECODER_REGISTRY`（注册与构建机制见 u3-l2 与 u8-l3）。构造器做了四件事：

1. **双查询嵌入**：`query_feat` 与 `query_embed` 是两个形状完全相同的 `nn.Embedding(num_queries, hidden_dim)`，前者是 query 的初始**内容**，后者是 query 的**位置编码**。这一拆分是本模块的核心设计——内容会被预测头消费并产生第 0 组预测（因此可学习 query 本身受监督），位置只参与注意力打分。
2. **level_embed**：3 个可学习向量（默认配置下 3×256），作为「尺度身份标签」加到对应尺度的特征上。因为正弦位置编码是按归一化坐标计算的（u4-l2），同一个相对位置在 1/32 与 1/8 尺度上编码结果相同，注意力层无从分辨当前看到的是哪个尺度，需要显式注入身份。
3. **三个平行的层列表**：每层解码器由一个 SelfAttentionLayer + 一个 CrossAttentionLayer + 一个 FFNLayer 组成，各层实例独立、不共享权重。
4. **预测头**：`class_embed` 线性层输出 \( K+1 \) 类 logits（多的 1 是 no-object，u3-l1 已讲），`mask_embed` 三层 MLP 把 query 映射成与 `mask_features` 点积的 mask 嵌入（u4-l4 专门精读）。

#### 4.1.2 核心流程

```text
from_config(cfg) 读配置
  ├─ hidden_dim=256, num_queries=100, nheads=8, dim_feedforward=2048
  ├─ dec_layers = DEC_LAYERS − 1        # 关键：减 1
  └─ pre_norm=False → 所有层走 post-norm 路径（u4-l2）
__init__(...)
  ├─ pe_layer = PositionEmbeddingSine(128, normalize=True)
  ├─ 3 个平行 ModuleList，各 dec_layers 个层实例
  ├─ query_feat / query_embed = Embedding(100, 256)   # 双嵌入
  ├─ level_embed = Embedding(3, 256)
  ├─ input_proj: 3 个 1×1 Conv2d(256→256)（enforce_input_project=False 时若通道相同仍会建，见下方坑 2）
  └─ class_embed = Linear(256, K+1)；mask_embed = MLP(256,256,256,3)
```

为什么减 1？配置注释写得很直白：learnable query 特征本身需要监督，于是把「对 query_feat 直接预测」算作第 0 组预测，为保持「辅助损失数量 = 解码器层数」的损失实现不变，实际构建的解码层数要少一层。COCO 配置 `DEC_LAYERS: 10`（注释 "9 decoder layers, add one for the loss on learnable query"）→ 构建 9 层 + 1 组 query 预测 = 10 组预测，即训练日志里 30 个 loss 键的来源（3 种损失 × 10 组，承接 u2-l1）。

#### 4.1.3 源码精读

注册入口（装饰器把它登记进项目级注册表）：

- [mask2former_transformer_decoder.py:L207-L208](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L207-L208) —— `@TRANSFORMER_DECODER_REGISTRY.register()` 注册 `MultiScaleMaskedTransformerDecoder`。

双嵌入定义，两行代码是本模块的灵魂：

- [mask2former_transformer_decoder.py:L314-L318](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L314-L318) —— `query_feat = nn.Embedding(num_queries, hidden_dim)`（learnable query features，内容）与 `query_embed = nn.Embedding(num_queries, hidden_dim)`（learnable query p.e.，位置）。两者形状相同但用途完全不同。

level_embed 与 input_proj：

- [mask2former_transformer_decoder.py:L320-L329](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L320-L329) —— 尺度身份硬编码为 3（`self.num_feature_levels = 3`）；`input_proj` 每尺度一个 1×1 卷积把特征投到 hidden_dim（通道相同时若 `enforce_input_project=False` 本可不建，但这里 R50 配置 in_channels=256=hidden_dim，走的是哪个分支？注意条件是 `in_channels != hidden_dim or enforce_input_project`，默认配置两者都为 False/相等时插入空 `nn.Sequential()`——**默认 R50 下 input_proj 是恒等映射**，这是一个容易被忽略的事实）。
- [mask2former_transformer_decoder.py:L331-L334](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L331-L334) —— 输出头：`class_embed` 输出 `num_classes + 1` 维；`mask_embed` 是 3 层 MLP。

三个平行层列表的组装循环（每层一个独立实例，dropout 硬编码 0.0）：

- [mask2former_transformer_decoder.py:L280-L310](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L280-L310) —— 循环 `dec_layers` 次，每次往三个 ModuleList 各 append 一个 SelfAttentionLayer / CrossAttentionLayer / FFNLayer。

from_config 中著名的「减一层」：

- [mask2former_transformer_decoder.py:L349-L355](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L349-L355) —— 注释原文解释了 `dec_layers = DEC_LAYERS - 1`：加了 learnable query features 的监督后，辅助损失数 = 解码器层数 + 1，减 1 是为了让损失实现保持「辅助损失数 = DEC_LAYERS」。

配置取值对照（默认 COCO 全景分割）：

- [configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml:L19-L39](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml#L19-L39) —— `HIDDEN_DIM: 256`、`NUM_OBJECT_QUERIES: 100`、`NHEADS: 8`、`DIM_FEEDFORWARD: 2048`、`PRE_NORM: False`、`DEC_LAYERS: 10`。config.py 中的全局默认值则是 `DEC_LAYERS = 6`（[mask2former/config.py:L44](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L44)），yaml 覆盖为 10。

两个值得记录的坑：

- **坑 1：`DROPOUT` 是本解码器的死配置。** v2 解码器的 from_config 不读 `cfg.MODEL.MASK_FORMER.DROPOUT`（config.py 默认 0.1、yaml 写 0.0 都不生效），层构造里硬编码 `dropout=0.0`（L289/L298/L306）。读这个键的是 v1 的 `StandardTransformerDecoder`：[maskformer_transformer_decoder.py:L118](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/maskformer_transformer_decoder.py#L118)。
- **坑 2：默认 R50 下 `input_proj` 是空的 `nn.Sequential()`。** 条件 `in_channels != hidden_dim or enforce_input_project`（L325）在 256==256 且 `ENFORCE_INPUT_PROJ: False` 时为 False。state_dict 里因此没有 input_proj 权重，属正常现象。

#### 4.1.4 代码实践

1. **实践目标**：用肉眼验证双嵌入的形状、层数「减 1」关系与 dropout 死配置。
2. **操作步骤**（示例代码，保存为 `inspect_decoder.py` 运行；需先按 u1-l2 装好环境，`import mask2former.modeling...` 会触发包级 import，需要已编译的 MSDeformAttn 扩展）：

   ```python
   # 示例代码
   import torch
   from mask2former.modeling.transformer_decoder.mask2former_transformer_decoder import (
       MultiScaleMaskedTransformerDecoder,
   )

   dec = MultiScaleMaskedTransformerDecoder(
       in_channels=256, mask_classification=True,
       num_classes=133, hidden_dim=256, num_queries=100,
       nheads=8, dim_feedforward=2048, dec_layers=9,   # DEC_LAYERS(10) - 1
       pre_norm=False, mask_dim=256, enforce_input_project=False,
   )
   print(type(dec.query_feat.weight), dec.query_feat.weight.shape)   # (100, 256)
   print(dec.query_embed.weight.shape)                              # (100, 256)
   print(dec.level_embed.weight.shape)                              # (3, 256)
   print(len(dec.transformer_cross_attention_layers))               # 9
   print(dec.input_proj[0])                                         # Sequential()（空，恒等映射）
   print(dec.class_embed)                                           # Linear(256, 134)
   ```

3. **需要观察的现象**：`@configurable` 允许绕过 cfg 直接以全关键字参数实例化；两个嵌入形状相同但属于不同参数张量。
4. **预期结果**：输出 `(100, 256)` 两次、`(3, 256)`、`9`、空的 `Sequential()` 与 `Linear(in_features=256, out_features=134)`。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：如果两个嵌入形状完全相同，能否合并成一个 `nn.Embedding` 省参数？
**答案**：不能。合并后内容与位置被绑定为同一向量，注意力里 `q = k = tgt + query_pos` 的拆分就失去意义；更重要的是只有内容嵌入（`query_feat`）过预测头产生第 0 组预测并受监督，位置嵌入不产生预测。二者梯度来源与语义都不同。

**练习 2**：把 `DEC_LAYERS` 从 10 改成 11，实际构建几层解码器、输出几组预测？
**答案**：构建 10 层，产出 11 组预测（1 组 learnable query 预测 + 10 组逐层预测），训练日志将出现 33 个 loss 键。

### 4.2 输入准备：多尺度特征逐尺度展平与 level_embed 注入

#### 4.2.1 概念说明

forward 的第一段把三个尺度的 2D 特征图变成注意力可用的 `(L, B, C)` token 序列（u4-l2 建立的序列格式）。与 u3-l5 的 MSDeformAttn encoder 有一个关键区别：**encoder 把三个尺度拼接成一条扁平序列**（用 `level_start_index` 记账），而这里**保持三条独立序列**，每层解码器只取其中一条——这正是「轮转」的前提。

每条序列上叠加两种信息：

- `pos`：该尺度的正弦位置编码（`pe_layer` 生成，normalize=True 即归一化坐标）；
- `level_embed`：该尺度的可学习身份向量，广播加到每个 token 上。

另外注意 forward 第三参数 `mask`（本应是 padding mask）被直接 `del`——调用方实际从不传它。

#### 4.2.2 核心流程

```text
forward(x, mask_features, mask=None)
  assert len(x) == 3
  del mask                              # 注释：disable mask, it does not affect performance
  for i in 0..2:
      size_list[i] = x[i] 的空间形状          # 记账，后续 attn_mask 对齐用
      pos[i]   = sine_PE(x[i]).flatten.permute  # (HW_i, B, C)
      src[i]   = (input_proj[i](x[i]) + level_embed[i]).flatten.permute
  query_embed → (Q, B, C)；output ← query_feat → (Q, B, C)
```

维度变换（B=2、C=256、输入 1024×1024 为例）：

\[ \text{NCHW} \xrightarrow{\text{flatten}(2)} (B, C, HW) \xrightarrow{\text{permute}(2,0,1)} (HW, B, C) \]

#### 4.2.3 源码精读

- [mask2former_transformer_decoder.py:L363-L371](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L363-L371) —— forward 签名（`x` 是多尺度特征列表）与 `del mask`：注释明说「不影响性能，禁用」。上游 [mask_former_head.py:L118-L121](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/mask_former_head.py#L118-L121) 调用 `self.predictor(multi_scale_features, mask_features, mask)` 时 `mask` 就是默认 None。
- [mask2former_transformer_decoder.py:L373-L380](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L373-L380) —— 展平循环本体，四行一句：记尺寸、生成位置编码并展平、`input_proj` 后**广播加上 `level_embed.weight[i][None, :, None]`**（形状 `(1, C, 1)`，对 `(B, C, HW)` 广播）再展平，最后都 permute 成 `(HW, B, C)`。
- [mask2former_transformer_decoder.py:L382-L386](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L382-L386) —— 从 `src[0]` 取出 batch size；把 `(Q, C)` 的两个嵌入 `unsqueeze(1).repeat(1, bs, 1)` 扩成 `(Q, B, C)`，query 侧所有 batch 共享同一组初始内容与位置。

#### 4.2.4 代码实践

1. **实践目标**：亲手跑一遍展平循环，确认三个尺度各自的序列长度与 level_embed 生效。
2. **操作步骤**（示例代码，接 4.1.4 的 `dec`）：

   ```python
   # 示例代码
   torch.manual_seed(0)
   B = 2
   x = [torch.randn(B, 256, s, s) for s in (32, 64, 128)]  # 1/32, 1/16, 1/8
   src, pos, size_list = [], [], []
   for i in range(3):
       size_list.append(x[i].shape[-2:])
       pos.append(dec.pe_layer(x[i], None).flatten(2).permute(2, 0, 1))
       src.append((dec.input_proj[i](x[i]).flatten(2)
                   + dec.level_embed.weight[i][None, :, None]).permute(2, 0, 1))
       print(i, size_list[i], src[i].shape, pos[i].shape)
   ```

3. **需要观察的现象**：序列长度依次为 1024、4096、16384，通道都是 256。
4. **预期结果**：`0 torch.Size([32, 32]) torch.Size([1024, 2, 256]) torch.Size([1024, 2, 256])`，随后 4096 与 16384 两个尺度同理。注意 1/32 尺度只有 1024 个 token——这就是轮转从最粗尺度开始的原因之一：第一层 cross-attention 代价最小。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么这里不把三个尺度拼成一条序列（像 encoder 那样）？
**答案**：拼接后 cross-attention 的 key 长度是三尺度之和（1024+4096+16384=21504），Q×K 注意力矩阵过大；轮转方案每层 key 只有当前尺度的长度，把多尺度信息分摊到不同层。此外 masked attention 的 attn_mask 需与 key 长度一致，单尺度序列让 mask 的生成与对齐更简单。

**练习 2**：`level_embed` 加在 `src` 上而不是 `pos` 上，有区别吗？
**答案**：在 CrossAttentionLayer 里二者最终都进 K（`with_pos_embed(memory, pos)`），效果近似；但 `level_embed` 是可学习参数（随梯度更新），而 sine `pos` 是无参数的确定函数。把它加进 src 也意味着它进入了 value 的世界（value 用的是原始 memory，不含 pos——实际上 value=memory 不加 pos，所以 level_embed 经 src 进 value。准确说：key 加了 pos 与 level_embed，value 只含 input_proj 后的特征加 level_embed）。核心动机是让层能区分尺度身份。

### 4.3 主循环：`level_index = i % 3` 尺度轮转与 cross→self→FFN

#### 4.3.1 概念说明

主循环是整篇论文的核心：**每个 query 先用上一层的 mask 预测把视野圈定在自己前景区域内，再从当前尺度取证据（masked cross-attention），接着 query 之间互通分工（self-attention），最后逐 token 精炼（FFN）；每层结束立刻重新预测，产出新 mask 又约束下一层**——一个「预测 → 约束 → 再预测」的自环。

两个签名式的设计：

- **尺度轮转**：9 层解码器依次看 1/32 → 1/16 → 1/8 → 1/32 → …，`level_index = i % 3`。9 层恰好每个尺度被看 3 次，从粗到细循环往复。attn_mask 的空间尺寸必须与当前层 key 序列（当前尺度的 HW）一致，所以预测头生成 mask 时目标尺寸也按 `(i+1) % 3` 轮转——**特征尺度与 mask 尺寸同步轮转**是初读时最容易看漏的一点。
- **层顺序 cross 在前**：经典 DETR 解码器是 self → cross → FFN；Mask2Former 反过来 cross → self → FFN。直觉：query 应先带着自己（受限）的视野从图像取证据，再在 query 之间交换信息完成去重与分工，最后由 FFN 逐 token 变换。

#### 4.3.2 核心流程

```text
第 0 步（进循环前）：
  预测头(query_feat) → pred[0]（learnable query 的直接监督）
                     + attn_mask（下采样对齐 size_list[0] = 1/32 尺寸）

for i in 0 .. num_layers-1:            # 默认 9 层
    level = i % 3                        # 0:1/32 → 1:1/16 → 2:1/8 → 循环
    兜底：attn_mask 中全 True 的行整体置 False      # 见 4.4
    output = CrossAttn[i](output, src[level],
                          memory_mask=attn_mask,      # masked attention 本体
                          memory_key_padding_mask=None,
                          pos=pe[level], query_pos=query_embed)
    output = SelfAttn[i](output, tgt_mask=None, query_pos=query_embed)
    output = FFN[i](output)
    预测头(output, 尺寸对齐 size_list[(i+1)%3]) → pred[i+1] + 下一层 attn_mask

return {'pred_logits': pred[-1],        # 最后一层为主体预测
        'pred_masks':  mask[-1],
        'aux_outputs': pred[0..-2]}     # 深监督（u5-l3）
```

注意力内部的形状（B=2、h=8、Q=100、当前尺度 1/32 即 HW=1024）：

\[ \text{attn\_logits} \in \mathbb{R}^{(B \cdot h) \times Q \times HW} \]

masked attention 的数学含义：对第 \( q \) 个 query，注意力分布只在集合 \( \{ j : M_{qj} = 0 \} \)（预测前景）上做 softmax，等价于把 \( M_{qj}=1 \) 处的 logit 加上 \( -\infty \)。

#### 4.3.3 源码精读

第 0 次预测——learnable query 直接受监督的落点：

- [mask2former_transformer_decoder.py:L388-L394](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L388-L394) —— 在进入任何解码层之前，`forward_prediction_heads(output, mask_features, attn_mask_target_size=size_list[0])` 直接作用于 `query_feat` 展开的 output，产出第一组类别/mask 预测与第一份 attn_mask（对齐 1/32 尺度）。

主循环本体（逐行）：

- [mask2former_transformer_decoder.py:L396-L398](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L396-L398) —— `level_index = i % self.num_feature_levels` 选定本层尺度；下一行是 4.4 要精读的兜底语句（先进循环就执行，因此第 0 组预测产生的 attn_mask 同样被兜底处理）。
- [mask2former_transformer_decoder.py:L399-L405](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L399-L405) —— masked cross-attention：`tgt=output`（query 序列），`memory=src[level_index]`（当前尺度特征），`memory_mask=attn_mask`，注释明确 **`memory_key_padding_mask=None`：不对 padding 区域做屏蔽**（与 DETR 不同，作者选择信任模型自己学会忽略 padding）；`pos=pos[level_index]` 是图像侧位置编码，`query_pos=query_embed` 是 query 侧位置编码。
- [mask2former_transformer_decoder.py:L407-L411](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L407-L411) —— self-attention：query 之间互通，`tgt_mask=None`（query 间不互相屏蔽），同样带 `query_pos`。
- [mask2former_transformer_decoder.py:L413-L416](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L413-L416) —— FFN 逐 token 精炼（两层 MLP + 残差 + norm，u4-l2）。
- [mask2former_transformer_decoder.py:L418-L420](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L418-L420) —— 每层结束立刻再过预测头，**`attn_mask_target_size=size_list[(i + 1) % self.num_feature_levels]`**：mask 尺寸对齐下一层将使用的尺度——本模块最精妙的一行，把「预测」与「轮转」锁在一起。

输出打包与断言：

- [mask2former_transformer_decoder.py:L422-L431](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L422-L431) —— 断言预测组数 = 层数+1；返回 dict 三件套 `pred_logits / pred_masks / aux_outputs`（`_set_aux_loss` 把前 num_layers 组打包成深监督列表，细节见 u5-l3）。顺带一提：最后一层（i=8）结束时生成的 attn_mask 对齐 `size_list[0]`，但循环随即结束，这份 mask 生成了并未被使用。

两个积木的调用视角速查（完整实现 u4-l2 已精读，此处只列与主循环对接的部分）：

- [mask2former_transformer_decoder.py:L98-L110](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L98-L110) —— `CrossAttentionLayer.forward_post`：`query = tgt + query_pos`、`key = memory + pos`、`value = memory`（位置不进 value），`attn_mask=memory_mask` 就是 masked attention 的载体。
- [mask2former_transformer_decoder.py:L40-L50](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L40-L50) —— `SelfAttentionLayer.forward_post`：`q = k = tgt + query_pos`、`value = tgt`，query 间的全连接通信。
- [mask2former_transformer_decoder.py:L64-L72](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L64-L72) 与 [L126-L135](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L126-L135) —— 两层各自的 `forward` 按 `normalize_before` 分派 pre/post 路径，默认 `PRE_NORM: False` 走 post-norm。

| 调用点（层 i） | query 来源 | key/value 来源 | 位置参数 | mask 参数 |
| --- | --- | --- | --- | --- |
| cross | `output` + query_embed | `src[i%3]` + `pe[i%3]` / value 不加 pos | `pos=pe[level]`, `query_pos=query_embed` | `memory_mask=attn_mask` |
| self | `output` + query_embed | 同 query 侧 | `query_pos=query_embed` | 全 None |
| FFN | `output` | — | — | — |

#### 4.3.4 代码实践（本讲核心实践）

1. **实践目标**：把 forward 复制为可开关 masked attention 的独立函数，实测 `memory_mask=None` 前后 `pred_logits` 的差异；并以注释逐行复述主循环。
2. **操作步骤**（示例代码，保存为 `masked_vs_unmasked.py`）：

   ```python
   # 示例代码：复制自 MultiScaleMaskedTransformerDecoder.forward，仅两处改动
   #   (1) 抽成函数、显式接收 dec 与 use_mask 开关
   #   (2) memory_mask=attn_mask if use_mask else None
   import torch
   from mask2former.modeling.transformer_decoder.mask2former_transformer_decoder import (
       MultiScaleMaskedTransformerDecoder,
   )

   def forward_with_switch(dec, x, mask_features, use_mask=True):
       assert len(x) == dec.num_feature_levels
       src, pos, size_list = [], [], []
       for i in range(dec.num_feature_levels):
           size_list.append(x[i].shape[-2:])
           pos.append(dec.pe_layer(x[i], None).flatten(2))
           src.append(dec.input_proj[i](x[i]).flatten(2)
                      + dec.level_embed.weight[i][None, :, None])
           pos[-1] = pos[-1].permute(2, 0, 1)
           src[-1] = src[-1].permute(2, 0, 1)
       _, bs, _ = src[0].shape
       query_embed = dec.query_embed.weight.unsqueeze(1).repeat(1, bs, 1)
       output = dec.query_feat.weight.unsqueeze(1).repeat(1, bs, 1)   # ← query_feat 预测的起点

       predictions_class, predictions_mask = [], []
       # ---- 第 0 组预测：learnable query 直接过预测头（尺寸对齐 size_list[0]，即 1/32）----
       outputs_class, outputs_mask, attn_mask = dec.forward_prediction_heads(
           output, mask_features, attn_mask_target_size=size_list[0])
       predictions_class.append(outputs_class)
       predictions_mask.append(outputs_mask)

       for i in range(dec.num_layers):
           level_index = i % dec.num_feature_levels            # ← 三尺度轮转：0,1,2,0,1,2,...
           attn_mask[torch.where(attn_mask.sum(-1) == attn_mask.shape[-1])] = False  # 兜底，见 4.4
           # ---- ① masked cross-attention：query 只在自己的预测前景内看当前尺度 ----
           output = dec.transformer_cross_attention_layers[i](
               output, src[level_index],
               memory_mask=attn_mask if use_mask else None,     # ← 本实践唯一开关
               memory_key_padding_mask=None,
               pos=pos[level_index], query_pos=query_embed)
           # ---- ② self-attention：query 间互通（cross 在前，与 DETR 相反）----
           output = dec.transformer_self_attention_layers[i](
               output, tgt_mask=None, tgt_key_padding_mask=None, query_pos=query_embed)
           # ---- ③ FFN：逐 token 精炼 ----
           output = dec.transformer_ffn_layers[i](output)
           # ---- ④ 层末再预测：mask 尺寸对齐“下一层”的尺度 (i+1)%3 ----
           outputs_class, outputs_mask, attn_mask = dec.forward_prediction_heads(
               output, mask_features,
               attn_mask_target_size=size_list[(i + 1) % dec.num_feature_levels])
           predictions_class.append(outputs_class)
           predictions_mask.append(outputs_mask)

       return {
           'pred_logits': predictions_class[-1],
           'pred_masks': predictions_mask[-1],
           'aux_outputs': [
               {"pred_logits": a, "pred_masks": b}
               for a, b in zip(predictions_class[:-1], predictions_mask[:-1])
           ],
       }

   torch.manual_seed(0)
   dec = MultiScaleMaskedTransformerDecoder(
       in_channels=256, mask_classification=True, num_classes=133, hidden_dim=256,
       num_queries=100, nheads=8, dim_feedforward=2048, dec_layers=9,
       pre_norm=False, mask_dim=256, enforce_input_project=False).eval()

   B = 2
   x = [torch.randn(B, 256, 32, 32), torch.randn(B, 256, 64, 64), torch.randn(B, 256, 128, 128)]
   mask_features = torch.randn(B, 256, 256, 256)     # 1/4 分辨率

   with torch.no_grad():
       a = forward_with_switch(dec, x, mask_features, use_mask=True)   # masked attention 开
       b = forward_with_switch(dec, x, mask_features, use_mask=False)  # memory_mask=None 关
   d = (a['pred_logits'] - b['pred_logits']).abs()
   print('pred_logits 形状', tuple(d.shape))
   print('平均绝对差', d.mean().item(), '最大绝对差', d.max().item())
   ```

   先跑通 `use_mask=True`，再与 `dec(x, mask_features)` 原版 forward 的输出比对，确认复制无误差（`torch.allclose`）。
3. **需要观察的现象**：两种模式的 `pred_logits`（形状 `(B, 100, 134)`）数值不同；随机初始化下预测 mask 的 sigmoid 在 0.5 附近，attn_mask 约一半位置为 True，所以开关会实质性改变注意力分布。
4. **预期结果**：`平均绝对差 > 0`（量级取决于随机种子，典型在 0.01~0.1 之间），`torch.allclose(复制版, 原版) == True`。具体数值待本地验证。
5. 若机器显存/内存吃紧，把三个尺度改成 `(8, 16, 32)`、`mask_features` 改成 `(B, 256, 64, 64)`，结论不变。

#### 4.3.5 小练习与答案

**练习 1**：为什么第 i 层结束时的预测头用 `size_list[(i+1) % 3]` 而不是 `size_list[i % 3]` 生成 attn_mask？
**答案**：这份 attn_mask 的消费者是第 i+1 层的 cross-attention，其 key 长度等于第 i+1 层尺度的 HW；attn_mask 的最后一维必须与之相等，否则形状不匹配直接报错。若写成 `i % 3`，只在相邻两层恰好同尺度时才不崩。

**练习 2**：9 层轮转下每个尺度被看几次？若 `DEC_LAYERS=11`（构建 10 层），轮转是否失衡？
**答案**：9 层 = 每尺度 3 次，均衡；10 层时尺度 0（1/32）被看 4 次、尺度 1/2 各 3 次，粗尺度略多。这说明 DEC_LAYERS 取 3 的倍数+1（即层数为 3 的倍数）时轮转最均衡。

**练习 3**：`memory_key_padding_mask=None` 意味着 query 可以注意 padding 区域，为什么不屏蔽？
**答案**：作者实验发现屏蔽 padding 不影响性能（forward 里 `del mask` 的注释同源），省去维护 padding mask 的开销；且 query 的视野已被 attn_mask 限制在预测前景内，padding 大概率落在前景之外，天然被双重过滤。

### 4.4 兜底逻辑：attn_mask 全 True 行置 False

#### 4.4.1 概念说明

attn_mask 由上一组预测 mask 下采样后 `sigmoid < 0.5` 得到（生成细节 u4-l4 精读）：**sigmoid < 0.5 的位置被判为"该 query 的背景"，置 True 禁止注意**。极端情形：某个 query 的预测 mask 在当前尺度的**所有**位置都低于 0.5——整行全 True，这个 query 什么都不能看。

这为什么是灾难？`nn.MultiheadAttention` 内部把 attn_mask 的 True 处 logits 加 \( -\infty \)。某行全 \( -\infty \) 时该行 softmax 分母为 0，输出 NaN；NaN 随该 query 进入 self-attention，污染所有 query，训练即刻崩溃。兜底语句把**全 True 的行整体放开**（整行置 False）：该 query 退化为普通的全局 cross-attention——失去聚焦，但保住数值健康。论文附录称此操作对性能无影响。

注意语义细节：兜底是「整行放开」，而不是「至少放开一个位置」；判定与修复都发生在**每一层、恰在使用前**。

#### 4.4.2 核心流程

```text
attn_mask: (B·h, Q, HW) 的布尔张量       # True = 禁止注意
mask_sum = attn_mask.sum(-1)              # (B·h, Q)，True 计 1
all_true_rows = (mask_sum == HW)          # 某行全 True
attn_mask[all_true_rows] = False          # 整行放开 → 全局注意力
```

触发概率的直觉：随机初始化时单个位置 sigmoid<0.5 的概率约 0.5，一行全 True 的概率约 \( 0.5^{HW} \)（HW=1024 时趋于 0）；训练中某个 query 若坚定预测「全图皆背景」（如已学会当 no-object 候选），整行 sigmoid 都可能压到 0.5 以下，兜底便真实生效。

#### 4.4.3 源码精读

- [mask2former_transformer_decoder.py:L398](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L398) —— 一行完成判定与修复：`attn_mask[torch.where(attn_mask.sum(-1) == attn_mask.shape[-1])] = False`。布尔张量 `sum(-1)` 把 True 按 1 累加；等于最后一维 HW 即全 True；`torch.where` 返回 `(行索引, 列索引)`（attn_mask 是 3 维，`sum(-1)` 后是 2 维 `(B·h, Q)`），成对索引选中的是形状 `(n, HW)` 的整段，赋 False 一次性放开。
- attn_mask 的来源与形状（生成细节留给 u4-l4，此处只看契约）：[mask2former_transformer_decoder.py:L433-L448](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L433-L448) —— 预测 mask 双线性下采样到目标尺度，`sigmoid().flatten(2).unsqueeze(1).repeat(1, num_heads, 1, 1).flatten(0, 1)` 得 `(B·h, Q, HW)`，再 `< 0.5` 转 bool 并 `detach()`（掩码是硬阈值产物，阻断梯度流回预测头）。

#### 4.4.4 代码实践

1. **实践目标**：亲眼验证「全 True 行 → NaN」与兜底语句的修复效果。
2. **操作步骤**（示例代码）：

   ```python
   # 示例代码
   import torch
   mha = torch.nn.MultiheadAttention(256, 8).eval()
   q = torch.randn(5, 2, 256); k = v = torch.randn(64, 2, 256)

   # (a) 不兜底：第 0 个 query 的 mask 整行全 True
   bad = torch.zeros(2 * 8, 5, 64, dtype=torch.bool)
   bad[:, 0, :] = True                      # 所有头下第 0 个 query 全图禁看
   with torch.no_grad():
       out = mha(q, k, v, attn_mask=bad)[0]
   print('不兜底时含 NaN：', out.isnan().any().item())

   # (b) 套用源码的兜底行
   bad[torch.where(bad.sum(-1) == bad.shape[-1])] = False
   with torch.no_grad():
       out2 = mha(q, k, v, attn_mask=bad)[0]
   print('兜底后含 NaN：', out2.isnan().any().item())
   ```

   再把兜底行插进 4.3.4 的复制版 forward，统计每层被放开的行数：

   ```python
   # 示例代码（插在 level_index 一行之后）
   rescued = (attn_mask.sum(-1) == attn_mask.shape[-1]).sum().item()
   if rescued:
       print(f'layer {i}: 放开 {rescued} 个全 True 行')
   ```
3. **需要观察的现象**：(a) 输出含 NaN（第 0 个 query 的 NaN 经输出张量可见）；(b) NaN 消失；插桩版在随机初始化下几乎不打印。
4. **预期结果**：`True / False / （无输出）`。第 (a) 步在个别 PyTorch 版本上可能表现为极大值而非标准 NaN，以实际输出为准，待本地验证；插桩无打印是因为随机权重下全 True 行概率约 \( 0.5^{HW} \)，该兜底主要在真实训练中触发。

#### 4.4.5 小练习与答案

**练习 1**：兜底为什么「整行放开」而不是「放开该行概率最大的一个位置」？
**答案**：整行放开只需一次布尔索引赋值、无需求 top-k，实现极简且数值安全；论文实验表明对性能无影响。放开单个位置虽保留聚焦意图，但引入额外计算与实现复杂度，收益不成比例。

**练习 2**：把兜底行从 `attn_mask.sum(-1) == attn_mask.shape[-1]` 改成 `>= attn_mask.shape[-1] - 1`（允许只剩一个可见位置）会怎样？
**答案**：行为上通常也能避免 NaN（softmax 至少有一个有限 logit），但语义改变：只剩一个位置可见时注意力会全部压在那一点上，输出近似单点采样；且 `sum == HW-1` 的行在训练早期频繁出现，等于大面积削弱 masked attention 的约束。这是示例性讨论，仓库并未这样实现。

## 5. 综合实践

把本讲四个模块串成一个「Mini 解码器实验台」脚本（示例代码，命名 `u4l3_lab.py`）：

1. 构建 `MultiScaleMaskedTransformerDecoder`（参数同 4.1.4），打印 `query_feat/query_embed/level_embed` 形状与层数，验证 `DEC_LAYERS-1` 关系（4.1）。
2. 生成 `(32, 64, 128)` 三尺度随机特征与 `256×256` 的 mask_features，手工执行展平循环并打印三条序列的 `(L, B, C)` 形状（4.2）。
3. 使用 4.3.4 的 `forward_with_switch` 分别跑 masked / unmasked 前向：
   - 断言复制版与原版 `dec(x, mask_features)` 输出 `allclose`；
   - 打印 `pred_logits` 与 `pred_masks` 的平均/最大绝对差；
   - 遍历 `aux_outputs`（9 组），打印每组 `pred_logits` 的差异均值，观察 masked attention 的影响是逐层放大还是收敛。
4. 在复制版中插桩统计每层兜底放开的行数（4.4），并手工构造一个「第 7 个 query 的预测 mask 全图置 -10」的极端输入（把 `dec.forward_prediction_heads` 的输出改写后继续跑），观察兜底行被触发。

预期：步骤 3 中差异非零且各组 aux 差异量级相近；步骤 4 的极端构造下应看到至少一行被放开、前向不产生 NaN。全部数值待本地验证。完成后，你应能不看源码复述主循环的每一步。

## 6. 本讲小结

- `query_feat`（内容）与 `query_embed`（位置）是两个同形的可学习嵌入：前者作为初始 query 直接过预测头产生第 0 组预测（可学习 query 受监督的落点），后者只作为 `query_pos` 进 Q/K 打分；这正是 `DEC_LAYERS` 要减 1 的原因。
- forward 把三个尺度各自展平成 `(HW, B, C)` 序列并注入可学习 `level_embed` 作尺度身份，不拼接成单序列——这是轮转的前提。
- 主循环层序为 **cross → self → FFN**（与 DETR 的 self → cross 相反），cross 由 `level_index = i % 3` 轮换尺度（1/32→1/16→1/8），每层结束立刻再预测，且 attn_mask 的目标尺寸按 `(i+1) % 3` 对齐下一层尺度。
- masked attention = 把上一组预测 mask 下采样、`sigmoid < 0.5` 处置 True 禁看、detach 后作为 `memory_mask`；padding 区域不做屏蔽。
- `attn_mask[torch.where(attn_mask.sum(-1) == attn_mask.shape[-1])] = False` 在每层使用前兜底：全 True 行会让 softmax 产生 NaN 并污染全部 query，整行放开让其退化为全局注意力。
- 两个工程细节：v2 解码器不读 `DROPOUT` 配置（硬编码 0.0）；默认 R50 下 `input_proj` 是空 Sequential（恒等映射）。

## 7. 下一步学习建议

- 下一讲 u4-l4「预测头与 Attention Mask 生成」精读 `forward_prediction_heads`：`class_embed`/`mask_embed` MLP、`einsum("bqc,bchw->bqhw")` 生成 mask、attention mask 的下采样细节与 `.detach()` 的必要性——本讲把它当黑盒，正好接续。
- 之后进入单元五：u5-l1 匈牙利匹配器消费本讲输出的 `pred_logits/pred_masks`，u5-l3 讲 `aux_outputs` 与 weight_dict 如何驱动深监督。
- 建议顺带重读 [mask2former_transformer_decoder.py:L363-L431](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L363-L431)，对照本讲 4.3.2 的伪代码逐行默写一遍主循环；有余力可对比视频版 `VideoMultiScaleMaskedTransformerDecoder`（u9-l2）看帧维如何并入。

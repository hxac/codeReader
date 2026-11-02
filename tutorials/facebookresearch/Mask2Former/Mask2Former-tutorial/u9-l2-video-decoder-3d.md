# u9-l2 视频解码器与 3D 位置编码

## 1. 本讲目标

上一讲（u9-l1）我们看到 `VideoMaskFormer` 把 B 段视频 × T 帧折叠成 BT 的"图像批次"，零改动复用 backbone 与像素解码器，并把"帧间交互"推迟到 Transformer 解码器。本讲就精读这个"被推迟"的部分：`VideoMultiScaleMaskedTransformerDecoder` 与它的 `PositionEmbeddingSine3D`。

学完本讲你应该能够：

1. 解释**帧维度折叠进注意力序列**的设计：为什么把 T 帧的特征拼进同一条 (T×HW, B, C) 序列，注意力就"天然"具备了时空建模能力，而不需要任何显式时序模块（3D 卷积、光流、逐帧传播）。
2. 逐行说出**视频版与图像版解码器的全部差异点**——它们集中在约 10 行形状变换代码上，注意力层本身一行未改。
3. 掌握 `forward_prediction_heads` 的视频版改造：mask 预测从 (B,Q,H,W) 变为 (B,Q,T,H,W)（每个 query 对应一个**时空管道**），attention mask 形状从 (B·h, Q, HW) 变为 (B·h, Q, T·HW)。
4. 理解 `PositionEmbeddingSine3D` 的时间-空间编码方式：时间维不是与空间维拼接，而是**同通道数相加**，且时间方向的频率密度是空间单方向的 2 倍。

## 2. 前置知识

本讲建立在以下已学内容之上（不再重复展开，不熟悉的读者请先回看）：

- **u9-l1**：`VideoMaskFormer.forward` 把嵌套的 B×T 帧摊平成帧列表，backbone 与 `sem_seg_head` 都按 BT 批次运行（见 [video_maskformer_model.py:L180-L188](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/video_maskformer_model.py#L180-L188)，逐 video 逐 frame 摊平后一次前向）。`INPUT.SAMPLING_FRAME_NUM`（默认 2）贯穿 mapper 采帧、模型 `num_frames` 与本讲解码器三个消费点。
- **u4-l2 / u4-l3**：解码器序列统一为 (L, B, C) 格式（L 是序列长度、B 批、C 通道）；层序为 cross → self → FFN；`query_feat`（内容嵌入）与 `query_embed`（位置嵌入）是两个同形可学习嵌入；三尺度特征各自成序列并注入可学习 `level_embed`；cross-attention 按层号 `i % 3` 在三个尺度间轮转。
- **u4-l4**：图像版 `forward_prediction_heads` 用 `einsum("bqc,bchw->bqhw")` 把 query 嵌入与 1/4 分辨率 mask_features 点积出 mask；attention mask 由 mask 下采样 + sigmoid < 0.5 + 复制到 B·num_heads 份生成。

本讲新增的基础概念：

- **注意力对序列长度没有先验假设**。`nn.MultiheadAttention` 只把输入当作一串 token，既不关心它们来自一张图还是一段视频。只要把"位置身份"（位置编码）和"可见性"（attention mask）正确地随 token 一起搬过去，把哪些 token 拼进序列是自由的——这是视频版改造的理论基础。
- **正弦位置编码回顾**。DETR 式位置编码把归一化坐标 \( \hat{x}\in[0,2\pi] \) 除以一组几何级数的"波长"，再交错取 sin/cos：

  \[ \text{PE}(2k)=\sin\Big(\frac{\hat{x}}{\tau^{\,2k/N}}\Big),\qquad \text{PE}(2k+1)=\cos\Big(\frac{\hat{x}}{\tau^{\,2k/N}}\Big),\qquad \tau=10000 \]

  坐标由 `cumsum` 生成、再除以最后一行/列的值完成归一化，因此编码的是**相对位置**，与绝对分辨率无关。
- **einsum 记法**：`"bqc,btchw->bqthw"` 表示把 (b,q,c) 与 (b,t,c,h,w) 两个张量在 c 维上做内积，输出 (b,q,t,h,w)；字母只是维度名字，比 `torch.matmul` 直观。
- **masked attention 的 mask 约定**：`nn.MultiheadAttention` 的 3D `attn_mask` 形状为 (N·num_heads, L, S)，`True` 表示禁止注意；L 是 query 数，S 是 key 数。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [mask2former_video/modeling/transformer_decoder/video_mask2former_transformer_decoder.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/video_mask2former_transformer_decoder.py) | 本讲主角：视频版解码器。开头 L18-L205 是从图像包**复制**（而非 import）来的 SelfAttentionLayer/CrossAttentionLayer/FFNLayer/MLP 四种积木；L208 起是 `VideoMultiScaleMaskedTransformerDecoder`。 |
| [mask2former_video/modeling/transformer_decoder/position_encoding.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/position_encoding.py) | `PositionEmbeddingSine3D`：全文件仅一个 57 行的类，只依赖 torch，无 detectron2 依赖。 |
| [mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py) | 图像版对照：`MultiScaleMaskedTransformerDecoder`。视频版的每一处差异都以它为基准。 |
| [mask2former/modeling/transformer_decoder/position_encoding.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/position_encoding.py) | 图像版 `PositionEmbeddingSine`，2D 对照。 |
| [mask2former_video/video_maskformer_model.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/video_maskformer_model.py) | 上游：BT 折叠发生的地方，决定解码器入口张量的形状。 |

值得先指出一个与 u9-l1"薄层复用"印象相反的事实：视频解码器文件**跨包 import 的只有注册表**（[video_mask2former_transformer_decoder.py:L13](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/video_mask2former_transformer_decoder.py#L13)，从图像包的 v1 文件引入 `TRANSFORMER_DECODER_REGISTRY`），四种注意力积木则是整段复制。所以它是"厚拷贝 + 少量改造"：改造点恰好就是本讲要精读的形状变换。

## 4. 核心概念与源码讲解

### 4.1 VideoMultiScaleMaskedTransformerDecoder.forward：(T×HW) 展平与主循环

#### 4.1.1 概念说明

视频实例分割（VIS）要求一个实例在所有帧里保持同一身份——理想的做法是让一个 query 直接"负责"一段时空管道（tube），而不是逐帧分割再做跨帧关联。`VideoMultiScaleMaskedTransformerDecoder` 的解法极为直接：

- **不引入任何时序模块**。它把 T 帧在同一尺度下的特征图全部展平，拼成一条长度为 T×HW 的 token 序列，作为 cross-attention 的 key/value。由于注意力对序列来源不敏感，query 一步就能看到所有帧的所有位置——时序建模完全交给了注意力本身。
- **时间身份由 3D 位置编码提供**（4.3 节）：序列里的 token 需要知道自己属于第几帧、在帧内何处，否则"跨帧相同位置"与"同帧相邻位置"无法区分。
- **可见性由时空 attention mask 提供**（4.2 节）：masked attention 的掩码从 (Q, HW) 变为 (Q, T·HW)，即"第 t 帧的位置 (h,w) 是否可被注意"逐帧判定。

改造的代价是 cross-attention 的计算量随 T 线性增长：\( O(Q \cdot T \cdot HW \cdot C) \)。这正是 YTVIS 配置把 `SAMPLING_FRAME_NUM` 设为 2（训练时每 clip 只采 2 帧）的原因之一。

#### 4.1.2 核心流程

```text
输入: x = [res5(1/32), res4(1/16), res3(1/8)]，每个形状 (BT, C, h_i, w_i)
      mask_features (BT, C_m, H, W)   # 1/4 分辨率逐像素嵌入，仍是 BT 折叠形态

1. 反折叠: bt = BT;  bs = bt // T (训练) 或 1 (推理);  t = bt // bs
   mask_features ← view(bs, t, C_m, H, W)
2. 对 3 个尺度 i = 0..2:
   pos[i]  ← PE3D(x[i] 的 5D 视图)          → (bs, t, C, h_i*w_i) → (t*h_i*w_i, bs, C)
   src[i]  ← input_proj[i](x[i]).flatten(2) + level_embed[i]
                                            → (BT, C, h_i*w_i) → view(bs,t,C,h_i*w_i)
                                            → (t*h_i*w_i, bs, C)
3. query_feat / query_embed: (Q, bs, C)     # query 在批维广播，跨帧共享
4. 用 query_feat 先出第 0 组预测（attn_mask 对齐 size_list[0]，即 1/32 尺度）
5. for i in 0 .. DEC_LAYERS-1-1:            # dec_layers = DEC_LAYERS - 1
     level = i % 3
     attn_mask 全 True 行置 False（防 softmax 全 -inf 出 NaN）
     output ← CrossAttn(output, src[level], memory_mask=attn_mask,
                        pos=pos[level], query_pos=query_embed)
     output ← SelfAttn(output, query_pos=query_embed)
     output ← FFN(output)
     预测（attn_mask 对齐 size_list[(i+1) % 3]）
6. 返回 pred_logits (bs, Q, K+1)、pred_masks (bs, Q, t, H, W)、aux_outputs
```

关于第 1 步的两种模式（训练 / 推理）：

| 模式 | 上游输入 | `bs` 推断 | `t` | 含义 |
| --- | --- | --- | --- | --- |
| 训练 | B 个 clip × 每_clip T 帧 → BT = B×T | `bt // num_frames` = B | T | 批内 B 段视频并行训练 |
| 推理 | 整段视频一次前向 → BT = 总帧数 | 硬编码 1 | 总帧数 | 一段视频 = 一个"批" |

#### 4.1.3 源码精读

**（a）入口：BT 反折叠与 bs/t 推断**

[video_mask2former_transformer_decoder.py:L370-L374](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/video_mask2former_transformer_decoder.py#L370-L374)：解码器入口先从 `mask_features` 的批维"猜回" B 与 T——训练时 `bs = bt // self.num_frames`，推理时直接令 `bs = 1`（u9-l1 讲过：视频推理是整段视频一次前向，此时 `num_frames` 只是训练采样参数，与视频长度无关），再把 4D 的 (BT, C, H, W) `view` 回 5D 的 (bs, t, C, H, W)。注意此处没有整除断言：若训练时 bt 不能被 `num_frames` 整除，`view` 会直接抛运行时错误。

**（b）多尺度序列化：视频版与图像版唯二的形状差异之一**

视频版 [video_mask2former_transformer_decoder.py:L385-L393](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/video_mask2former_transformer_decoder.py#L385-L393)：

- `pos` 一行：把 `x[i]`（仍是 4D 的 BT 批）`view` 成 (bs, t, C, h, w) 的 5D 张量喂给 `pe_layer`（3D 位置编码只用形状、不用数值，见 4.3），得到 (bs, t, 256, h, w)，`flatten(3)` 折成 (bs, t, 256, h·w)。
- `src` 一行：`input_proj[i]` 是普通 `Conv2d`（1×1 卷积），在 4D 的 BT 批上逐帧运行——**像素解码器输出到这一步仍是逐帧处理的**；`flatten(2)` 后加 `level_embed`（尺度身份，与图像版相同）。
- 随后两行（注释 `# NTxCxHW => NxTxCxHW => (TxHW)xNxC`）把 pos 与 src 统一重排：先 `view(bs, t, c, hw)`（对 pos 是恒等变形、对 src 是真正的 BT→B×T 折回），再 `permute(1, 3, 0, 2)` 变 (t, hw, bs, c)，最后 `flatten(0, 1)` 得 **(t·hw, bs, c)**。

对照图像版 [mask2former_transformer_decoder.py:L373-L380](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L373-L380)：同样的循环里，`pe_layer` 是 2D 的、只 `flatten(2)` + `permute(2, 0, 1)` 得 **(hw, bs, c)**。差异总结：

| | 图像版 | 视频版 |
| --- | --- | --- |
| 序列长度 | hw | t·hw（T 外层、HW 内层） |
| 位置编码输入 | (B,C,H,W) | (B,T,C,H,W) 的 5D 视图 |
| 重排链 | `flatten(2).permute(2,0,1)` | `view(bs,t,c,hw).permute(1,3,0,2).flatten(0,1)` |
| `input_proj` 运行形态 | B 批 | BT 批（逐帧），折回发生在其后 |

序列内部的排列顺序（先第 0 帧全部 HW、再第 1 帧全部 HW……）对注意力本身不重要——注意力对序列置换等变；它唯一的硬约束是 **attn_mask 最后一维的 T·HW 必须与 src 的展平顺序逐位对齐**，4.2 节会验证两侧确实都是"T 外、HW 内"。

**（c）query 广播与构造器差异**

[video_mask2former_transformer_decoder.py:L395-L397](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/video_mask2former_transformer_decoder.py#L395-L397)：`query_feat`/`query_embed` 仍是 (Q, bs, C)——**同一组 Q 个 query 服务于所有帧**，这正是"一个 query 管一段时空管道"的落点。与图像版的差异只剩批维大小（bs 而非 B）以及：构造器多接收一个 `num_frames`（[L252](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/video_mask2former_transformer_decoder.py#L252)、[L276](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/video_mask2former_transformer_decoder.py#L276)），来源是 `from_config` 里新增的一行 [L366](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/video_mask2former_transformer_decoder.py#L366)：`ret["num_frames"] = cfg.INPUT.SAMPLING_FRAME_NUM`；位置编码换成 3D（[L279-L280](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/video_mask2former_transformer_decoder.py#L279-L280)）。其余构造项（query_feat/query_embed 双嵌入 [L319-L323](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/video_mask2former_transformer_decoder.py#L319-L323)、`level_embed` [L326-L327](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/video_mask2former_transformer_decoder.py#L326-L327)、`dec_layers = DEC_LAYERS - 1` [L360](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/video_mask2former_transformer_decoder.py#L360)）与图像版逐字相同。

**（d）主循环：与图像版逐字相同**

[video_mask2former_transformer_decoder.py:L399-L431](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/video_mask2former_transformer_decoder.py#L399-L431)：第 0 组预测在 query_feat 上先做（[L402-L405](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/video_mask2former_transformer_decoder.py#L402-L405)，`attn_mask_target_size=size_list[0]`）；主循环内 `level_index = i % 3` 尺度轮转（[L408](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/video_mask2former_transformer_decoder.py#L408)）、attn_mask 全 True 行兜底（[L409](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/video_mask2former_transformer_decoder.py#L409)）、cross → self → FFN 层序（[L411-L427](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/video_mask2former_transformer_decoder.py#L411-L427)）、每层末预测并对齐 `size_list[(i+1) % 3]`（[L429](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/video_mask2former_transformer_decoder.py#L429)）——与图像版 [L396-L420](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L396-L420) 完全一致。唯一的"变化"是 `src[level_index]` 与 `attn_mask` 更长了：memory 从 HW 个 token 变成 T×HW 个，masked attention 因此成为**时空掩码**。另外 [L382-L383](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/video_mask2former_transformer_decoder.py#L382-L383) 的 `del mask`（注释"disable mask, it does not affect performance"）与图像版相同——padding 掩码被丢弃，这解释了 3D 位置编码的 mask 参数为何永远是 None。

最终输出 dict（[L435-L442](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/video_mask2former_transformer_decoder.py#L435-L442)）：`pred_logits` (bs, Q, K+1)、`pred_masks` **(bs, Q, t, H, W)**、`aux_outputs` 由 `_set_aux_loss` 打包（[L463-L474](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/video_mask2former_transformer_decoder.py#L463-L474)，与图像版相同）。

#### 4.1.4 代码实践

用纯 PyTorch 复现 forward 入口的形状变换链，观察训练/推理两种模式下的 bs/t 推断（无需安装 detectron2）。

1. **实践目标**：亲手验证 BT 折叠批如何被反折叠、(BT,C,HW) 如何变成 (T×HW,B,C)，并确认它与图像版 (HW,B,C) 的序列长度差异。
2. **操作步骤**：把下面的脚本保存为独立文件并运行（只需 PyTorch）。

   ```python
   # practice_4_1_shapes.py —— 复现 video_mask2former_transformer_decoder.py
   # L371-L393 的形状变换（纯 PyTorch，无 detectron2 依赖）
   import torch

   def fold_like_decoder(feat_bt, num_frames, training):
       bt, c, h, w = feat_bt.shape
       bs = bt // num_frames if training else 1   # ← 源码 L372
       t = bt // bs                               # ← 源码 L373
       return feat_bt.view(bs, t, c, h, w), bs, t # ← 源码 L374

   x = torch.randn(4, 256, 8, 8)      # 模拟像素解码器输出：(BT=4, C, H, W)

   # 训练模式：B=2 段视频 × T=2 帧
   x5, bs_tr, t_tr = fold_like_decoder(x, num_frames=2, training=True)
   print("train:", bs_tr, t_tr, tuple(x5.shape))  # 期望 2 2 (2, 2, 256, 8, 8)

   # 推理模式：整段 4 帧视频一次前向
   x5, bs_ev, t_ev = fold_like_decoder(x, num_frames=2, training=False)
   print("eval: ", bs_ev, t_ev, tuple(x5.shape))  # 期望 1 4 (1, 4, 256, 8, 8)

   # 多尺度序列化：复现 L388 的 flatten(2) 与 L391-393 的 view/permute/flatten
   src = x.flatten(2)                             # (BT, C, HW)
   src = src.view(bs_tr, t_tr, 256, -1)           # (B, T, C, HW)
   src = src.permute(1, 3, 0, 2).flatten(0, 1)    # (T*HW, B, C)
   print("video seq (train):", tuple(src.shape))  # 期望 (128, 2, 256)，T*HW=2*64

   # 对照图像版 L378-380：flatten(2) 后直接 permute(2,0,1)
   src_img = x.flatten(2).permute(2, 0, 1)        # (HW, B, C)
   print("image seq:", tuple(src_img.shape))      # 期望 (64, 4, 256)
   ```

3. **需要观察的现象**：同一个 (4,256,8,8) 输入，训练模式被解释为 B=2、T=2，推理模式被解释为 B=1、T=4；训练模式下视频序列长度 (128) 是图像版 (64) 的 T=2 倍，批维则从 4 缩小为 2。
4. **预期结果**：依次打印 `train: 2 2 (2, 2, 256, 8, 8)`、`eval:  1 4 (1, 4, 256, 8, 8)`、`video seq (train): (128, 2, 256)`、`image seq: (64, 4, 256)`（形状由 view/permute 语义决定；具体数值待本地验证）。
5. 若把训练模式的输入改为 `torch.randn(5, 256, 8, 8)`（bt=5 不能被 num_frames=2 整除），`view` 将抛出运行时错误——这验证了 4.1.3（a）中"靠 view 报错兜底"的说法。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `input_proj`（1×1 卷积）可以在 BT 折叠批上运行，而 3D 位置编码必须先 view 成 5D？

**答案**：`Conv2d` 把批维当作独立样本逐个处理，BT 帧与 B 张图在它眼里没有区别，所以折叠形态反而最方便；而 `PositionEmbeddingSine3D` 需要沿时间维做 `cumsum`（[position_encoding.py:L35](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/position_encoding.py#L35)），时间必须是一个显式维度，因此要先 view 成 (bs, t, C, H, W)——它只借用这个形状，不用特征数值。

**练习 2**：训练时批内 2 段视频各采 2 帧，cross-attention 在 1/32 尺度（输入 1024×1024）下 key 序列有多长？与图像版相比呢？

**答案**：1/32 尺度特征图为 32×32=1024 个位置/帧；序列长度 = T×HW = 2×1024 = 2048，批维为 bs=2。图像版同样输入下序列长度只有 1024（批维 4）。即注意力矩阵的 key 维扩大为 2 倍、批维缩小为一半，总计算量不变，但**每个 query 单次 attention 能看到的 token 数翻倍**——这正是时空交互的来源。

**练习 3**：推理模式 `bs=1` 时，若一段视频有 36 帧，`t` 是多少？这依赖 `INPUT.SAMPLING_FRAME_NUM` 吗？

**答案**：`t = bt // bs = 36`。不依赖——推理时视频解码器按整段视频一次前向（u9-l1），`SAMPLING_FRAME_NUM` 只是训练侧的采样帧数；源码里 `bs = bt // self.num_frames if self.training else 1`（[L372](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/video_mask2former_transformer_decoder.py#L372)）用 `self.training` 显式区分了这两种语义。

### 4.2 forward_prediction_heads：(B,Q,T,H,W) mask 与 (B·h, Q, T·HW) 掩码

#### 4.2.1 概念说明

图像版的预测头（u4-l4）"一进三出"：类别 logits、mask logits、下一层的 masked attention 掩码。视频版保留这一结构，三样输出全部长出时间维：

- **mask 从"一张图"变"一段管道"**：`einsum` 从 `"bqc,bchw->bqhw"` 变为 `"bqc,btchw->bqthw"`，每个 query 的 mask 嵌入与**每一帧**的 mask_features 点积，输出 (B,Q,T,H,W)。一个 query 因此描述一个跨帧一致的实例管道——这是 VIS"同一实例跨帧同 ID"的模型层保障。
- **attention mask 最后一维从 HW 变 T·HW**：`nn.MultiheadAttention` 的 3D 掩码形状是 (N·num_heads, L, S)；query 数 L=Q 不变，key 数 S 从 HW 变为 T·HW，于是掩码成为 (B·h, Q, T·HW)——每个"批 × 头"一份 Q×(T·HW) 的布尔表，逐帧逐位置地声明可见性。
- **下采样仍是逐帧 2D**：对 5D 张量做双线性下采样必须先 `flatten(0,1)` 成 (B·Q, T, H, W) 的 4D——把 T 临时当作通道、B·Q 当作批——`F.interpolate` 才能处理；模式仍是 `bilinear` 而非 3D 插值。

#### 4.2.2 核心流程

视频版 [video_mask2former_transformer_decoder.py:L444-L461](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/video_mask2former_transformer_decoder.py#L444-L461) 的形状变换链（attn_mask 部分）：

```text
outputs_mask (B, Q, T, H, W)                        # einsum("bqc,btchw->bqthw")
  └─ flatten(0,1) → (B·Q, T, H, W)                  # 5D → 4D 才能 interpolate
  └─ F.interpolate(bilinear) → (B·Q, T, h, w)       # 逐帧 2D 下采样到下一层尺度
  └─ view(b, q, t, h, w)                            # 折回 5D（用 L450 取出的 b,q,t）
  └─ sigmoid → flatten(2) → (B, Q, T·h·w)           # T 外、HW 内 —— 与 src 展平顺序对齐
  └─ unsqueeze(1) → (B, 1, Q, T·hw)
  └─ repeat(1, h, 1, 1) → (B, h, Q, T·hw)           # 复制到每个注意力头
  └─ flatten(0, 1) → (B·h, Q, T·hw)                 # 满足 attn_mask 的 3D 约定
  └─ < 0.5 → bool → detach                          # True = 禁止注意；bool 已断梯度
```

#### 4.2.3 源码精读

**（a）分类与 mask 嵌入：与图像版逐字相同**

[video_mask2former_transformer_decoder.py:L445-L448](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/video_mask2former_transformer_decoder.py#L445-L448)：decoder 输出 (Q, B, C) 先 LayerNorm 再 `transpose(0, 1)` 成 (B, Q, C)；`class_embed` 线性层出 (B, Q, K+1)（类别 logits 跨帧共享，一个 query 全程一个类别）；`mask_embed` 三层 MLP 出 (B, Q, mask_dim)。这与图像版 [L434-L437](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L434-L437) 完全一致。

**（b）mask 预测：einsum 多一个 t**

[video_mask2former_transformer_decoder.py:L449](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/video_mask2former_transformer_decoder.py#L449)：`torch.einsum("bqc,btchw->bqthw", mask_embed, mask_features)`——注意进入本函数的 `mask_features` 已经是 forward 开头 view 好的 5D (B,T,C,H,W)（见 4.1.3（a）），在 c 维点积后输出 (B,Q,T,H,W)。图像版对应行是 [L438](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L438) 的 `"bqc,bchw->bqhw"`。语义仍是"每个 query 专属的动态 1×1 卷积核"，只是同一组核在每帧上各卷一次。

**（c）attention mask：三处差异**

对照图像版 [L440-L446](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L440-L446)，视频版 [L450-L459](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/video_mask2former_transformer_decoder.py#L450-L459) 的差异共三处：

| 步骤 | 图像版 | 视频版差异 |
| --- | --- | --- |
| 取形状 | 不需要 | **新增** `b, q, t, _, _ = outputs_mask.shape`（[L450](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/video_mask2former_transformer_decoder.py#L450)），供后面 view 回 5D |
| 下采样 | 直接 `F.interpolate(outputs_mask, ...)`（4D） | 先 `flatten(0, 1)` 变 (B·Q,T,H,W) 再插值，再 `view(b, q, t, h, w)` 折回（[L454-L455](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/video_mask2former_transformer_decoder.py#L454-L455)） |
| 掩码展平 | `sigmoid().flatten(2)` → (B,Q,HW) → 复制头 → (B·h,Q,HW) | 同一条链，但 flatten(2) 压的是 (T,h,w) 三维 → (B,Q,T·hw) → (B·h,Q,T·HW)（[L458](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/video_mask2former_transformer_decoder.py#L458)） |

源码注释（[L452-L453](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/video_mask2former_transformer_decoder.py#L452-L453)）把这条链写作 `[B, Q, T, H, W] -> [B, Q, T*H*W] -> [B, h, Q, T*H*W] -> [B*h, Q, T*HW]`，正是 (B·h, Q, T·HW) 的来历：**T 进入最后一维（key 维）**，因此 masked attention 是"逐帧逐位置"的时空掩码；而 (B,Q,T,h,w).flatten(2) 的 T 外、HW 内顺序与 4.1.3（b）中 src 的展平顺序严格一致，二者逐位对齐。末尾 `< 0.5` 的 bool 化与 `detach`（[L458-L459](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/video_mask2former_transformer_decoder.py#L458-L459)）与图像版相同：True = 禁止注意，bool 比较已断梯度，detach 属设计声明。

#### 4.2.4 代码实践

构造"已知答案"的玩具 mask，验证 attn_mask 的展平顺序确实是 T 外、HW 内。

1. **实践目标**：证明 attn_mask 最后一维的前 h·w 个元素对应第 0 帧、后 h·w 个对应第 1 帧，从而确认掩码与 src 序列逐位对齐。
2. **操作步骤**：

   ```python
   # practice_4_2_attn_mask.py —— 复现 L454-L459 的掩码展平（纯 PyTorch）
   import torch
   import torch.nn.functional as F

   B, Q, T, H, W, h, w, num_heads = 1, 1, 2, 4, 4, 2, 2, 8
   # 构造已知 logits：第 0 帧全 -10（sigmoid≈0 → 禁止注意），第 1 帧全 +10（允许）
   outputs_mask = torch.full((B, Q, T, H, W), 10.0)
   outputs_mask[:, :, 0] = -10.0

   attn = F.interpolate(                        # ← 源码 L454-455
       outputs_mask.flatten(0, 1), size=(h, w),
       mode="bilinear", align_corners=False).view(B, Q, T, h, w)
   attn = (attn.sigmoid().flatten(2).unsqueeze(1)   # ← 源码 L458
           .repeat(1, num_heads, 1, 1).flatten(0, 1) < 0.5)
   print("attn_mask:", tuple(attn.shape))       # 期望 (8, 1, 8) = (B*h, Q, T*h*w)

   frame0 = attn[0, 0, : h * w]                 # 前 T*h*w 的一半
   frame1 = attn[0, 0, h * w:]
   print("frame0 all-True:", bool(frame0.all()))
   print("frame1 any-True:", bool(frame1.any()))
   ```

3. **需要观察的现象**：`attn_mask` 形状为 (8, 1, 8)；第 0 帧（logits=-10，sigmoid≈0<0.5）对应的 4 个位置全为 True（禁止注意），第 1 帧全为 False（允许）。
4. **预期结果**：打印 `attn_mask: (8, 1, 8)`、`frame0 all-True: True`、`frame1 any-True: False`。这直接验证了 T 外 HW 内的展平顺序（数值待本地验证）。
5. 把 `outputs_mask[:, :, 0] = -10.0` 改成只在第 0 帧左半平面（`outputs_mask[:, :, 0, :, :2] = -10.0`）置负，重跑后 frame0 内 True/False 各占一半且按空间排列——掩码在同一帧内仍保持空间结构。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `F.interpolate` 之前必须 `flatten(0,1)`？直接对 5D 张量调用会怎样？

**答案**：`F.interpolate` 只接受 4D（N, C, H_d, W_d）或 3D 输入，5D 会抛异常。`flatten(0,1)` 把 (B,Q,T,H,W) 压成 (B·Q,T,H,W)，把 T 临时放进"通道"位，插值对每个 (b,q) 的每帧独立做 2D 双线性下采样；插完再 `view(b, q, t, h, w)` 折回（b,q,t 由 [L450](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/video_mask2former_transformer_decoder.py#L450) 提前取出）。

**练习 2**：视频版 attn_mask 形状 (B·h, Q, T·HW) 中，T 为什么在最后一维而不是第二维？

**答案**：`nn.MultiheadAttention` 的 3D `attn_mask` 约定形状是 (N·num_heads, L_query, S_key)。query 是 Q 个（无时间维，query 跨帧共享），key 是 T×HW 个时空 token，所以 T 只能进入 key 维。这也要求 (B,Q,T,h,w) 的 flatten(2)（T 外、HW 内）与 forward 里 src 的 `permute(1,3,0,2).flatten(0,1)` 展平顺序一致，否则掩码会错位到别的帧。

**练习 3**：一个 query 的 mask 若在第 0 帧的下一层尺度上整体 sigmoid < 0.5（全 True 行），会发生什么？

**答案**：主循环 [L409](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/video_mask2former_transformer_decoder.py#L409) 的兜底逻辑 `attn_mask[torch.where(attn_mask.sum(-1) == attn_mask.shape[-1])] = False` 会把**整行**置为 False。注意"整行"的判定横跨全部 T×HW 个 key：只有当该 query 在**每一帧的全部位置**都被屏蔽时才触发，此时置 False 使其退化为无掩码注意力，防 softmax 分母为零出 NaN；若只是某一帧被完全屏蔽而其他帧仍有可看位置，行内存在 False、兜底不触发，该 query 只是无法从那一帧取证据。

### 4.3 PositionEmbeddingSine3D：时间维的加法式编码

#### 4.3.1 概念说明

token 被拼成一条序列后，注意力本身无法区分"第 0 帧的像素"与"第 1 帧的像素"——位置编码是唯一的时空身份来源。`PositionEmbeddingSine3D` 在 2D 版基础上做了两处关键设计：

- **时间与空间是"相加"而非"拼接"**。2D 版把 y、x 两组编码拼接（`cat(pos_y, pos_x)`，各占 hidden_dim/2 通道）。3D 版若把 z 也拼接，通道数会涨到 1.5×hidden_dim，下游序列通道就必须改。它的做法是：z 编码单独生成 **hidden_dim 通道**（与 cat(y,x) 同宽），然后逐元素相加——通道数不变，时间信息叠加在空间编码之上。
- **时间方向的频率密度是空间单方向的 2 倍**。z 使用 `dim_t_z`（长度 2·num_pos_feats，[position_encoding.py:L47-L48](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/position_encoding.py#L47-L48)），即 128 个频率对；x、y 各用 `dim_t`（长度 num_pos_feats）即 64 个频率对。直觉：帧数 T（如 2~36）远小于空间分辨率（HW 成千上万），坐标取值范围小，需要更密的低频段才能在少数帧之间拉开编码距离。
- **归一化让编码与 clip 长度无关**：z 坐标除以最后一帧的值后乘 2π，编码的是"在 clip 里的相对时间"，任意帧数的 clip 共享同一套编码语义（空间维同理，与 u4-l2 讲过的 2D 版一致）。

#### 4.3.2 核心流程

设 \( N=\text{num\_pos\_feats}=\text{hidden\_dim}/2=128 \)（decoder 侧由 [L279-L280](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/video_mask2former_transformer_decoder.py#L279-L280) 传入），\( \tau=10000 \)。归一化坐标：

\[ \hat{z}(t)=\frac{2\pi\,t}{T},\qquad \hat{y}=\frac{2\pi\,y}{H},\qquad \hat{x}=\frac{2\pi\,x}{W} \]

（由全 1 掩码的 `cumsum` 再除以末帧/末行/末列实现，[L35-L42](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/position_encoding.py#L35-L42)。）

空间编码（x 与 y 各 \( N \) 通道，\( k=0,\dots,N/2-1 \)）：

\[ \text{PE}_x(\cdot,\,2k)=\sin\frac{\hat{x}}{\tau^{2k/N}},\qquad \text{PE}_x(\cdot,\,2k+1)=\cos\frac{\hat{x}}{\tau^{2k/N}} \]

时间编码（\( N\cdot 2=256 \) 通道，\( k=0,\dots,N-1 \)，频率指数 \( k/N \)）：

\[ \text{PE}_z(t,\,2k)=\sin\frac{\hat{z}(t)}{\tau^{k/N}},\qquad \text{PE}_z(t,\,2k+1)=\cos\frac{\hat{z}(t)}{\tau^{k/N}} \]

合成（**拼接 + 相加**，总通道 \( N+N=2N=\text{hidden\_dim} \)）：

\[ \text{PE} = \big[\,\text{PE}_y\,\|\,\text{PE}_x\,\big] \oplus \text{PE}_z \]

其中 \( \|\) 表通道拼接、\( \oplus \) 表逐元素相加（对应源码 [L56](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/position_encoding.py#L56) 的 `cat(...) + pos_z`）。

一个有用的退化性质：当 \( T=1 \) 时 \( \hat{z}\equiv 2\pi \) 处处相同，\( \text{PE}_z \) 退化为一个与空间位置无关的常数向量，于是 **3D 编码 = 2D 编码 + 常数偏置**——4.3.4 的实践将验证这一点。

#### 4.3.3 源码精读

**（a）输入契约与默认掩码**

[position_encoding.py:L29-L33](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/position_encoding.py#L29-L33)：断言输入必须是 5D (b, t, c, h, w)；`mask=None` 时构造全 0 的 (b,t,h,w) 掩码。decoder 调用处永远传 None（[video_mask2former_transformer_decoder.py:L387](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/video_mask2former_transformer_decoder.py#L387)），配合 forward 里的 `del mask`——掩码机制存在但从未使用，`not_mask` 全 1 使 `cumsum` 退化为坐标计数。

**（b）三轴坐标与归一化**

[position_encoding.py:L34-L42](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/position_encoding.py#L34-L42)：`z_embed/y_embed/x_embed` 分别沿 dim1/2/3 累加；归一化时 z 除以**最后一帧**的值、y 除以最后一行、x 除以最后一列，均乘 `scale=2π`。注意 PE 只消费这些坐标，**完全不用输入特征的数值**——这就是 decoder 可以随便 view 形状喂进来的原因。

**（c）频率表：dim_t 与 dim_t_z 的区别**

[position_encoding.py:L44-L48](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/position_encoding.py#L44-L48)：`dim_t` 长度 `num_pos_feats`（服务 x/y），`dim_t_z` 长度 `num_pos_feats * 2`（服务 z）。指数公式 \( \tau^{2\lfloor j/2\rfloor / L} \)（\( L \) 为表长）保证相邻的 sin/cos 对共享同一频率——z 表更长意味着 z 在 \([0,1)\) 的指数区间里取了 128 个频率点，而 x/y 各只有 64 个。

**（d）sin/cos 交错与合成**

[position_encoding.py:L50-L56](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/position_encoding.py#L50-L56)：三个轴分别按偶数下标取 sin、奇数下标取 cos、`stack(..., dim=5).flatten(4)` 交错排列（与 2D 版 [L43-L50](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/position_encoding.py#L43-L50) 同手法）；最后 `torch.cat((pos_y, pos_x), dim=4) + pos_z` 完成拼接加相加，`permute(0, 1, 4, 2, 3)` 输出 (b, t, 2N, h, w)。对照 2D 版 [L51](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/position_encoding.py#L51)：只有 `cat((pos_y, pos_x))` 没有 `+ pos_z`，输出 (b, 2N, h, w)。

#### 4.3.4 代码实践

两个 PE 文件都只依赖 torch，可以用 `importlib` 按文件路径直接加载（绕过各包 `__init__.py`——它们会连带 import detectron2），验证"3D = 2D + 时间常数"的退化性质。

1. **实践目标**：确认 T=1 时 3D 位置编码与 2D 版只差一个空间常数向量，从而理解"时间维是加法叠加"。
2. **操作步骤**：

   ```python
   # practice_4_3_pe3d.py —— 按文件路径加载两个 PE，避免触发包级 __init__
   import importlib.util, torch

   def load(path, name):
       spec = importlib.util.spec_from_file_location(name, path)
       mod = importlib.util.module_from_spec(spec)
       spec.loader.exec_module(mod)
       return mod

   pe2d = load("mask2former/modeling/transformer_decoder/position_encoding.py", "pe2d")
   pe3d = load("mask2former_video/modeling/transformer_decoder/position_encoding.py", "pe3d")

   f2 = pe2d.PositionEmbeddingSine(128, normalize=True)      # N_steps = hidden_dim//2
   f3 = pe3d.PositionEmbeddingSine3D(128, normalize=True)

   x = torch.randn(1, 3, 32, 32)        # (B, C, H, W)，数值无关紧要
   p2 = f2(x, None)                     # → (1, 256, 32, 32)
   p3 = f3(x.unsqueeze(1), None)        # T=1 → (1, 1, 256, 32, 32)
   print("2D:", tuple(p2.shape), " 3D:", tuple(p3.shape))

   diff = p3.squeeze(1) - p2            # 理论上 = PE_z 常数向量在空间上广播
   const = diff[..., :1, :1]
   print("diff is spatially constant:", bool(torch.allclose(diff, const.expand_as(diff))))
   print("t=2 differs from t=1:", not bool(torch.allclose(
       f3(torch.randn(1, 2, 3, 32, 32), None)[0, 0], p3[0, 0])))
   ```

   （在仓库根目录运行；`f3(torch.randn(1,2,3,32,32))` 的输入形状为 (B,T,C,H,W)。）
3. **需要观察的现象**：两个输出形状分别为 (1,256,32,32) 与 (1,1,256,32,32)；`diff` 逐空间位置相同（即时间编码是一个加性常数）；T=2 时第 0 帧的编码与 T=1 时不同（归一化坐标 \( \hat z \) 依赖 T）。
4. **预期结果**：打印 `2D: (1, 256, 32, 32)  3D: (1, 1, 256, 32, 32)`、`diff is spatially constant: True`、`t=2 differs from t=1: True`（待本地验证）。
5. 再把 `PositionEmbeddingSine3D(128)` 的 `normalize` 改为 `False`（或另建实例），会得到 ValueError 之外的另一现象：z 坐标不再除以 T，`f3(T=2)` 的第 0 帧编码将与 `f3(T=1)` 相同——归一化正是"相对时间"语义的实现点。

#### 4.3.5 小练习与答案

**练习 1**：如果把时间编码从"相加"改成"拼接"（cat(pos_y, pos_x, pos_z)），需要连带修改哪些地方？

**答案**：pos 通道数将从 \( 2N \) 变为 \( 3N \)，而 `src` 经 `input_proj` 投影后通道是 hidden_dim=\( 2N \)——cross-attention 里 `with_pos_embed(memory, pos)` 要求二者同形相加（[video_mask2former_transformer_decoder.py:L104-L107](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/video_mask2former_transformer_decoder.py#L104-L107)），所以要么把 hidden_dim 全局扩大 1.5 倍（牵动所有注意力层与 FFN），要么给 pos 单独加投影层。相加方案以零参数、零结构改动换取通道不变，代价是时间与空间信息在同一组通道上叠加而非正交分离。

**练习 2**：`PositionEmbeddingSine3D` 的 `mask` 参数为什么在本项目中永远是 None？如果启用它（对 padding 区域置 True），哪些量会变？

**答案**：因为 decoder 的 forward 显式 `del mask`（[video_mask2former_transformer_decoder.py:L382-L383](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/video_mask2former_transformer_decoder.py#L382-L383)，注释说明不影响性能），PE 调用处固定传 None。若启用，`not_mask` 在 padding 处为 0，`cumsum` 不再推进——有效区域的归一化坐标与 clip 实际长度对齐（不受 padding 帧影响），归一化分母 `z_embed[:, -1:, :, :]` 取到的也变成最后一个有效帧的坐标。

**练习 3**：时间编码用 128 个频率对、空间每轴只用 64 个，这是浪费还是合理？

**答案**：合理。帧坐标的取值域是 \( \hat z \in [0, 2\pi] \) 上仅 T 个离散点（T 通常 2~36），而空间坐标在成千上万个位置上连续取值、天然拉开距离。要让少量帧也能彼此区分，需要在低频段取更密的频率采样，`dim_t_z` 的 \( \tau^{k/N} \)（步长比空间轴密一倍）正是为此；空间轴点数多，64 个频率对已够分辨。

## 5. 综合实践

**任务：写一个对比脚本，把图像版与视频版 `forward_prediction_heads` 并排复现，验证 `einsum("bqc,btchw->bqthw")` 的输出形状与 attn_mask 的展平逻辑，并用注释逐行标出两处实现中"维度差异"的每一行。**

规格：bs=2、t=2、Q=100、K+1=41、C=hidden_dim=256、mask_dim=256、num_heads=8；视频版 `mask_features` 用折叠前的 (BT=4, 256, H, W)，图像版用 (2, 256, H, W)，H=W=32；下一层尺度 `attn_mask_target_size=(16, 16)`。

1. **实践目标**：
   - 在同一份代码里并排看到两个版本预测头的全部差异行（共 4 处：入口 `view` 5D、einsum 记法、interpolate 的 flatten/view、掩码最后一维含 T）；
   - 验证视频版 `outputs_mask` 形状 (2,100,2,32,32)、attn_mask 形状 (2×8, 100, 2×16×16)=(16,100,512)，图像版对应 (2,100,32,32)、(16,100,256)。
2. **操作步骤**：保存以下脚本并在仓库根目录运行（仅需 PyTorch；`MLP`/线性层用轻量替身，因为我们只关心形状与掩码逻辑，不关心数值）：

   ```python
   # practice_compare_heads.py —— 图像版 vs 视频版 forward_prediction_heads 对比
   # 对应源码：
   #   图像版 mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py L433-L448
   #   视频版 mask2former_video/modeling/transformer_decoder/video_mask2former_transformer_decoder.py L444-L461
   import torch
   import torch.nn as nn
   import torch.nn.functional as F

   torch.manual_seed(0)
   B, T, Q, K1, C, MASK_DIM, HEADS = 2, 2, 100, 41, 256, 256, 8
   H = W = 32
   target_size = (16, 16)          # attn_mask_target_size：下一层 cross-attention 的尺度

   decoder_output = torch.randn(Q, B, C)          # (L=Q, N=B, C)，u4-l2 的序列格式
   class_embed = nn.Linear(C, K1)

   class MLP3(nn.Module):                          # mask_embed 的 3 层替身（结构同源码 L193-L205）
       def __init__(s, i, h, o):
           super().__init__()
           s.f = nn.Sequential(nn.Linear(i, h), nn.ReLU(), nn.Linear(h, h),
                               nn.ReLU(), nn.Linear(h, o))
       def forward(s, x): return s.f(x)
   mask_embed = MLP3(C, C, MASK_DIM)

   def head_image(output, mask_features, tgt):
       """图像版逐行复现。差异行用 [IMG] 标注。"""
       dec = decoder_norm(output).transpose(0, 1)         # (B, Q, C)
       cls = class_embed(dec)                             # (B, Q, K+1)
       emb = mask_embed(dec)                              # (B, Q, mask_dim)
       mask = torch.einsum("bqc,bchw->bqhw", emb, mask_features)   # [IMG] 4D 特征
       attn = F.interpolate(mask, size=tgt, mode="bilinear",
                            align_corners=False)          # [IMG] 4D 可直接插值
       attn = (attn.sigmoid().flatten(2).unsqueeze(1)
               .repeat(1, HEADS, 1, 1).flatten(0, 1) < 0.5).bool()  # (B*h, Q, HW)
       return cls, mask, attn.detach()

   def head_video(output, mask_features_bt, tgt):
       """视频版逐行复现。相对图像版的维度差异行用 [VID] 标注。"""
       bt, c_m, h_m, w_m = mask_features_bt.shape
       bs = bt // T                                      # [VID] 训练模式反折叠（源码 L372 的 bs 推断）
       t = bt // bs                                      # [VID]（源码 L373）
       mask_features = mask_features_bt.view(bs, t, c_m, h_m, w_m)  # [VID] 折成 5D（源码 L374）
       dec = decoder_norm(output).transpose(0, 1)        # 与图像版相同
       cls = class_embed(dec)                            # 与图像版相同（类别跨帧共享）
       emb = mask_embed(dec)                             # 与图像版相同
       mask = torch.einsum("bqc,btchw->bqthw", emb, mask_features)  # [VID] einsum 多 t 维
       b, q, t, _, _ = mask.shape                        # [VID] 取出 b,q,t 供 view（源码 L450）
       attn = F.interpolate(mask.flatten(0, 1), size=tgt,       # [VID] 5D→(B*Q,T,H,W) 才能插值
                            mode="bilinear", align_corners=False
                            ).view(b, q, t, tgt[0], tgt[1])     # [VID] 折回 5D
       attn = (attn.sigmoid().flatten(2).unsqueeze(1)    # flatten(2) 压 (t,h,w) → T*hw
               .repeat(1, HEADS, 1, 1).flatten(0, 1) < 0.5).bool()  # (B*h, Q, T*HW)
       return cls, mask, attn.detach()

   decoder_norm = nn.LayerNorm(C)

   mask_features_img = torch.randn(B, C, H, W)        # 图像版：B 批
   mask_features_bt  = torch.randn(B * T, C, H, W)    # 视频版：BT 折叠批（u9-l1）

   ci, mi, ai = head_image(decoder_output, mask_features_img, target_size)
   cv, mv, av = head_video(decoder_output, mask_features_bt, target_size)

   print("image: cls", tuple(ci.shape), " mask", tuple(mi.shape),
         " attn", tuple(ai.shape))
   print("video: cls", tuple(cv.shape), " mask", tuple(mv.shape),
         " attn", tuple(av.shape))

   assert cv.shape == (B, Q, K1) and ci.shape == (B, Q, K1)
   assert mv.shape == (B, Q, T, H, W)                  # ← einsum("bqc,btchw->bqthw") 的验证点
   assert mi.shape == (B, Q, H, W)
   assert av.shape == (B * HEADS, Q, T * target_size[0] * target_size[1])  # (B*h, Q, T*HW)
   assert ai.shape == (B * HEADS, Q, target_size[0] * target_size[1])
   assert av.dtype == torch.bool
   # 掩码顺序验证：T 外、HW 内（与 forward 里 src 的展平顺序对齐）
   assert av.numel() == B * HEADS * Q * T * tgt[0] * tgt[1]
   print("all assertions passed")
   ```

3. **需要观察的现象**：
   - 视频版 `mask` 比图像版多出第 3 维 T，即每个 query 的 mask 从一张图变成 T 帧的管道；
   - 视频版 `attn` 最后一维 = T×h×w = 512，恰为图像版（256）的 T 倍；
   - 脚本中 7 处 `[VID]` 标注就是两个实现全部的维度差异行：反折叠 3 行、einsum 1 行、取形状 1 行、interpolate 的 flatten/view 2 行（掩码展平行写法与图像版相同，只是作用对象多了 t 维）；
4. **预期结果**：打印 `image: cls (2, 100, 41)  mask (2, 100, 32, 32)  attn (16, 100, 256)` 与 `video: cls (2, 100, 41)  mask (2, 100, 2, 32, 32)  attn (16, 100, 512)`，最后输出 `all assertions passed`（形状由代码语义决定；具体运行待本地验证）。
5. **可选加强**（需要完整环境：detectron2 + 编译好的 ops）：直接实例化仓库里的真实类并调用其 `forward_prediction_heads`，与本脚本的形状断言互相印证：

   ```python
   import torch, mask2former_video  # import 触发注册
   from mask2former_video.modeling.transformer_decoder.video_mask2former_transformer_decoder import (
       VideoMultiScaleMaskedTransformerDecoder)

   dec = VideoMultiScaleMaskedTransformerDecoder(   # @configurable 支持直接传参构造
       in_channels=256, mask_classification=True, num_classes=40, hidden_dim=256,
       num_queries=100, nheads=8, dim_feedforward=2048, dec_layers=9,
       pre_norm=False, mask_dim=256, enforce_input_project=False, num_frames=2).eval()

   output = dec.query_feat.weight.unsqueeze(1).repeat(1, 2, 1)   # (Q, B, C)
   mask_features = torch.randn(2, 2, 256, 32, 32)                # 5D：(B, T, C, H, W)
   cls, mask, attn = dec.forward_prediction_heads(output, mask_features,
                                                  attn_mask_target_size=(16, 16))
   print(cls.shape, mask.shape, attn.shape)   # 期望 (2,100,41) (2,100,2,32,32) (16,100,512)
   ```

   注意真实函数入口要求 5D 的 `mask_features`（forward 已在上游完成 view），这与脚本体形成呼应；结果待本地验证。

## 6. 本讲小结

- **帧折叠进序列 = 免费的时空注意力**：视频版把每尺度特征展平成 (T×HW, B, C) 序列（`view(bs,t,c,hw).permute(1,3,0,2).flatten(0,1)`），注意力层一行未改，query 一次即可看到所有帧的所有位置；时序建模完全交给注意力 + 位置编码。
- **视频版与图像版的差异集中在约 10 行形状代码**：入口 BT 反折叠（训练 `bs=bt//T`、推理 `bs=1`）、序列化重排、einsum 多 t 维、interpolate 的 flatten(0,1)/view、掩码最后一维含 T；主循环（尺度轮转、全 True 兜底、cross→self→FFN）与图像版逐字相同。
- **mask 预测从 (B,Q,H,W) 变 (B,Q,T,H,W)**：`einsum("bqc,btchw->bqthw")` 让一个 query 的动态 1×1 核在每帧各卷一次，跨帧同核保证实例身份一致——VIS 的模型层根基。
- **attention mask 形状 (B·h, Q, T·HW) 的来源**：`nn.MultiheadAttention` 的 3D 掩码约定 (N·num_heads, L_query, S_key)，query 跨帧共享故 L=Q，key 是 T×HW 个时空 token 故 T 进入最后一维；且 flatten(2) 的 T 外 HW 内顺序必须与 src 的展平顺序逐位对齐。
- **3D 位置编码是"加法式"的**：`PE = cat(pos_y, pos_x) + pos_z`，z 单独占满 hidden_dim 通道（128 个频率对，密度是空间单轴 2 倍）、归一化到 clip 相对时间；通道数不变使下游零改动，T=1 时退化为 2D 编码加常数。

## 7. 下一步学习建议

本讲打通了视频模型的"中间层"。接下来两讲分别补齐它的两端：

1. **u9-l3 视频数据管线**：`YTVISDatasetMapper` 如何采样 T 帧、`ytvis_annotations_to_instances` 如何用 `gt_ids` 表达跨帧实例关联——本讲的 (B,T) 折叠与 (B,Q,T,H,W) mask 在数据侧的对应物。
2. **u9-l4 视频损失与评估**：`VideoSetCriterion` / `VideoHungarianMatcher`（注意其 mask 代价不再走采样点）如何消费本讲输出的 `pred_masks` (B,Q,T,H,W)，以及 `YTVISEvaluator` 的指标输出。
3. 若想横向对照"把时间折叠进序列"的另一种做法，可回看图像包的 `msdeformattn.py`（u3-l5）：那里的多尺度也是先各自展平再靠 `level_embed` 区分身份——本讲的 T 维与那里的尺度维扮演同一角色。

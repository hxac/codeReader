# MSDeformAttnPixelDecoder：默认像素解码器

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `MSDeformAttnPixelDecoder` 的完整数据流：res3/res4/res5 经 `input_proj` 投影后送入多尺度可变形注意力 encoder，再用 extra FPN 层补足高分辨率，最终输出 `mask_features` 与三个尺度的 `multi_scale_features`。
2. 解释 `MSDeformAttnTransformerEncoderOnly.forward` 中的多尺度展平、`level_embed`、`spatial_shapes` 与 `level_start_index` 的作用。
3. 手算 `num_fpn_levels = log2(min_stride) - log2(common_stride)`，并说明在默认配置下 extra FPN 只补 **一个** 层（res2）而不是两个。
4. 准确复述 `forward_features` 返回三元组的契约，以及它与 `MaskFormerHead.layers` 的对接方式。

本讲是单元三（三大组件拆解）的收尾：上一讲我们看了两个 FPN 式像素解码器，本讲进入 Mask2Former 的**默认**像素解码器，也是论文的核心组件之一。

## 2. 前置知识

### 2.1 为什么需要「多尺度可变形注意力」

上一讲（u3-l4）的 `TransformerEncoderPixelDecoder` 把全局自注意力放在 1/32 尺度上。全局注意力的代价是序列长度的平方：若在 1/8 尺度（1024 输入下 128×128 = 16384 个 token）做全局注意力，注意力矩阵有 \(16384^2 \approx 2.7 \times 10^8\) 个元素，显存和计算都不可接受。

多尺度可变形注意力（MSDeformAttn，来自 Deformable-DETR）换了一种思路：**每个 query token 不和所有 token 做注意力，而是只在其参考点附近、跨所有尺度采样 K 个点**，对采样值做加权求和。复杂度从 \(O(N^2)\) 降到 \(O(N \cdot L \cdot K)\)，其中 \(L\) 是尺度数、\(K\) 是每尺度采样点数（本项目中 K=4、L=3）。这让它可以**同时吃进 1/8、1/16、1/32 三个尺度**——这正是 Mask2Former 比 MaskFormer(v1) 涨点的重要来源。

算子本身的数学形式与 CUDA 实现是下一单元 u4-l1 的主题；本讲只把它当「一个可调用的模块」用，关注像素解码器如何组织数据流。

### 2.2 前置认知回顾（来自已学讲义）

- u3-l2：`MaskFormerHead.layers` 调用 `self.pixel_decoder.forward_features(features)`，要求返回三元组 `(mask_features, transformer_encoder_features, multi_scale_features)`；`TRANSFORMER_IN_FEATURE = "multi_scale_pixel_decoder"`（v2 默认）时 head 只消费第一、三项。
- u3-l4：`build_pixel_decoder` 从 `SEM_SEG_HEADS_REGISTRY` 查表构建，并校验 `forward_features` 可调用；`BasePixelDecoder` 输出的 `multi_scale_features` 依次为 1/32、1/16、1/8。
- u1-l2：MSDeformAttn 有 CUDA 与 PyTorch 双实现，CPU 张量会走 `ms_deform_attn_core_pytorch` 回退，因此本讲的实践在 CPU 上也能跑。
- u1-l5：配置由 `add_maskformer2_config` 注册，`MODEL.SEM_SEG_HEAD.*` 控制像素解码器。

### 2.3 一个容易混淆的点：extra FPN 到底补几层？

先给出结论（本讲 4.3 会用源码与公式证明）：**默认配置下（`DEFORMABLE_TRANSFORMER_ENCODER_IN_FEATURES = ["res3","res4","res5"]`、`COMMON_STRIDE = 4`），extra FPN 只补一层：res2（1/4）**。transformer 自己处理 1/8、1/16、1/32，从 1/8 到 1/4 只差一个「层级」，所以 FPN 只需补一层。若 transformer 只吃 res4、res5（1/16 起），则需要补 res3、res2 两层。很多资料想当然地写成「补两层」，读源码时请以公式为准。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [mask2former/modeling/pixel_decoder/msdeformattn.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/msdeformattn.py) | 本讲主角：`MSDeformAttnPixelDecoder` 与 `MSDeformAttnTransformerEncoderOnly` 全部在此文件 |
| [mask2former/config.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py) | 注册 `DEFORMABLE_TRANSFORMER_ENCODER_IN_FEATURES` 等配置键 |
| [configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml) | 把 `PIXEL_DECODER_NAME` 指向本类的配置文件 |
| [mask2former/modeling/pixel_decoder/ops/modules/ms_deform_attn.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/ops/modules/ms_deform_attn.py) | MSDeformAttn 算子模块（本讲当黑盒，u4-l1 精读） |
| [mask2former/modeling/transformer_decoder/position_encoding.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/position_encoding.py) | `PositionEmbeddingSine`，为每个尺度生成正弦位置编码 |
| [mask2former/modeling/meta_arch/mask_former_head.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/mask_former_head.py) | 三元组的消费方 `MaskFormerHead.layers`（u3-l2 已学） |

## 4. 核心概念与源码讲解

本讲拆三个最小模块：

- 4.1 构造期：`from_config` 与 `__init__` 的组装决策（input_proj、mask_features 卷积、num_fpn_levels 公式）
- 4.2 `MSDeformAttnTransformerEncoderOnly.forward`：多尺度展平与 level_embed
- 4.3 `MSDeformAttnPixelDecoder.forward_features`：split 还原、extra FPN 与三元组契约

### 4.1 构造期：from_config 与模块组装

#### 4.1.1 概念说明

`MSDeformAttnPixelDecoder` 注册在 `SEM_SEG_HEADS_REGISTRY` 中，通过 u3-l4 学过的 `build_pixel_decoder` 查表构建。它的构造期要做四个决策：

1. **哪些特征进 transformer**：由 `DEFORMABLE_TRANSFORMER_ENCODER_IN_FEATURES` 决定（默认 res3/res4/res5），其余特征（res2）留给 extra FPN。
2. **通道对齐**：backbone 各尺度通道不同（R50 为 512/1024/2048），transformer 要求统一 256 维，用 `input_proj`（1×1 卷积 + GroupNorm）拉齐。
3. **mask_features 生成方式**：与 `BasePixelDecoder` 用 3×3 卷积不同，这里只用一个 **1×1 卷积**（`conv_dim → mask_dim`），因为卷积精修的职责已经交给 extra FPN 的 `layer_*`。
4. **extra FPN 需要几层**：由 `num_fpn_levels` 公式计算，层数在构造期就确定（决定建多少对 lateral/output 卷积）。

#### 4.1.2 核心流程

```text
from_config(cfg, input_shape)
    ├─ 过滤 IN_FEATURES → input_shape
    ├─ conv_dim / mask_dim / norm ← SEM_SEG_HEAD.*
    ├─ transformer_dropout / nheads ← MASK_FORMER.*（注意来源！）
    ├─ transformer_dim_feedforward = 1024（硬编码）
    ├─ transformer_enc_layers ← SEM_SEG_HEAD.TRANSFORMER_ENC_LAYERS
    ├─ transformer_in_features ← DEFORMABLE_TRANSFORMER_ENCODER_IN_FEATURES
    └─ common_stride ← SEM_SEG_HEAD.COMMON_STRIDE

__init__
    ├─ input_shape 按 stride 排序 → in_features = [res2..res5]
    ├─ transformer 子集按 stride 排序 → transformer_in_features = [res3,res4,res5]
    ├─ input_proj：按「低分辨率→高分辨率」(res5→res3) 建一组 1x1conv+GN
    ├─ transformer = MSDeformAttnTransformerEncoderOnly(6 层, 3 尺度)
    ├─ pe_layer = PositionEmbeddingSine(128, normalize=True)
    ├─ mask_features = 1x1 Conv2d(conv_dim → mask_dim)
    └─ num_fpn_levels = log2(min(strides)) - log2(common_stride)
        → 建对应数量的 adapter_i (1x1) 与 layer_i (3x3)
```

#### 4.1.3 源码精读

**注册与类定义**——与 `BasePixelDecoder` 同一个注册表，因此配置里改一个 `PIXEL_DECODER_NAME` 字符串即可互换：

[msdeformattn.py:164-165](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/msdeformattn.py#L164-L165) 把类注册进 `SEM_SEG_HEADS_REGISTRY`。

**两个特征清单的分离**——构造期第一件事是把「pixel decoder 的全部输入」与「transformer 的输入」分成两个清单：

[msdeformattn.py:195-211](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/msdeformattn.py#L195-L211) 先按 stride 排序得到 `self.in_features`（res2→res5，给 extra FPN 用），再用 `transformer_in_features` 过滤出子集并同样排序，得到 `transformer_feature_strides`（默认 `[8, 16, 32]`）。`transformer_num_feature_levels` 就是这个子集的长度（默认 3）。

**input_proj 的顺序陷阱**——投影组按「低分辨率→高分辨率」创建：

[msdeformattn.py:212-230](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/msdeformattn.py#L212-L230) 对 `transformer_in_channels[::-1]` 逐个建 `Conv2d(in_channels, conv_dim, 1) + GroupNorm(32)`。因此 **`input_proj[0]` 对应 res5（2048→256）、`input_proj[1]` 对应 res4（1024→256）、`input_proj[2]` 对应 res3（512→256）**。前向时特征也按同样顺序送入（见 4.3），两处的 `[::-1]` 互为镜像。只建一层时走 else 分支（单尺度退化情形）。

**transformer 与位置编码**：

[msdeformattn.py:232-241](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/msdeformattn.py#L232-L241) 构建 `MSDeformAttnTransformerEncoderOnly`，`d_model=conv_dim=256`、`num_encoder_layers=TRANSFORMER_ENC_LAYERS`（配置为 6）、`num_feature_levels=3`；`N_steps = conv_dim // 2 = 128`，`PositionEmbeddingSine(128, normalize=True)` 输出通道为 2×128=256，与 d_model 对齐。

**mask_features 用 1×1 卷积**：

[msdeformattn.py:243-252](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/msdeformattn.py#L243-L252) 与 `BasePixelDecoder` 的 3×3 卷积（u3-l4）相比这里只有 1×1，注释 `# use 1x1 conv instead` 点明了差异——高分辨率特征的空间精修由 extra FPN 的 3×3 `layer_*` 完成，最后只做通道变换。

**num_fpn_levels 公式**——本讲最重要的一行：

[msdeformattn.py:254-259](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/msdeformattn.py#L254-L259) 中

```python
self.maskformer_num_feature_levels = 3  # always use 3 scales
self.common_stride = common_stride
stride = min(self.transformer_feature_strides)
self.num_fpn_levels = int(np.log2(stride) - np.log2(self.common_stride))
```

含义：特征金字塔每个「层级」对应分辨率 ×2（stride ÷2）。transformer 处理的最细尺度是 `min_stride`，而 `mask_features` 必须落在 `common_stride` 上，两者相差的层数就是 extra FPN 要补的层数：

\[ \text{num\_fpn\_levels} = \log_2(\text{min\_stride}) - \log_2(\text{common\_stride}) \]

默认配置代入：\(\log_2 8 - \log_2 4 = 3 - 2 = 1\)，即只补 res2 一层。（若 transformer 只吃 res4/res5，则 \(\log_2 16 - \log_2 4 = 2\)，补 res3、res2 两层。）

**extra FPN 卷积对**：

[msdeformattn.py:261-292](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/msdeformattn.py#L261-L292) 对 `self.feature_channels[:self.num_fpn_levels]`（默认即只有 res2 的 256 通道）建一对卷积：`adapter_i`（1×1 lateral，拉齐通道）与 `layer_i`（3×3 output，带 GN 与 ReLU），并以 `self.add_module("adapter_1", ...)`、`self.add_module("layer_1", ...)` 命名注册。最后 `[::-1]` 反转成「自顶向下」顺序，让前向代码从低分辨率往高分辨率读。

**from_config 的键来源与三个「坑」**：

[msdeformattn.py:294-312](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/msdeformattn.py#L294-L312) 中有三个值得注意的细节：

1. `transformer_dim_feedforward` 被硬编码为 1024（第 306 行），读取 `MASK_FORMER.DIM_FEEDFORWARD`（配置值 2048）的那行被注释掉了——encoder 的 FFN 维度与 decoder **不同**，改配置里的 `DIM_FEEDFORWARD` 不会影响这里。
2. `transformer_nheads` 来自 `cfg.MODEL.MASK_FORMER.NHEADS`（第 304 行），而**不是** `SEM_SEG_HEAD.DFORMABLE_TRANSFORMER_ENCODER_N_HEADS`。
3. 结合 grep 全仓库可以确认：[config.py:103-104](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L103-L104) 注册的 `DEFORMABLE_TRANSFORMER_ENCODER_N_POINTS = 4` 与 `DEFORMABLE_TRANSFORMER_ENCODER_N_HEADS = 8` 两个键**没有任何代码读取**，是「死配置」——实际生效的是 `MSDeformAttnTransformerEncoderOnly` 签名里的默认值 `enc_n_points=4` 和从 `MASK_FORMER.NHEADS` 传来的 8。两者数值恰好一致，所以平时看不出差异；想改采样点数，改配置是无效的。

配置文件侧的取值见 [maskformer2_R50_bs16_50ep.yaml:14-18](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml#L14-L18)：`PIXEL_DECODER_NAME: "MSDeformAttnPixelDecoder"`、`DEFORMABLE_TRANSFORMER_ENCODER_IN_FEATURES: ["res3","res4","res5"]`、`COMMON_STRIDE: 4`、`TRANSFORMER_ENC_LAYERS: 6`。

#### 4.1.4 代码实践

**实践目标**：不动前向，只构建模块，验证构造期的四个决策（input_proj 顺序、1×1 mask_features、num_fpn_levels、死配置）。

**操作步骤**（示例代码，独立脚本 `inspect_decoder.py`，在仓库根目录运行）：

```python
# 示例代码：检验 MSDeformAttnPixelDecoder 的构造期决策
import torch
from detectron2.config import get_cfg
from detectron2.layers import ShapeSpec
from detectron2.projects.deeplab import add_deeplab_config
from mask2former import add_maskformer2_config
from mask2former.modeling.pixel_decoder.msdeformattn import MSDeformAttnPixelDecoder

cfg = get_cfg()
add_deeplab_config(cfg)
add_maskformer2_config(cfg)
# 与 maskformer2_R50_bs16_50ep.yaml 等价的取值
cfg.MODEL.SEM_SEG_HEAD.IN_FEATURES = ["res2", "res3", "res4", "res5"]
cfg.MODEL.SEM_SEG_HEAD.DEFORMABLE_TRANSFORMER_ENCODER_IN_FEATURES = ["res3", "res4", "res5"]
cfg.MODEL.SEM_SEG_HEAD.TRANSFORMER_ENC_LAYERS = 6
cfg.MODEL.SEM_SEG_HEAD.COMMON_STRIDE = 4
cfg.MODEL.SEM_SEG_HEAD.CONVS_DIM = 256
cfg.MODEL.SEM_SEG_HEAD.MASK_DIM = 256
cfg.MODEL.SEM_SEG_HEAD.NORM = "GN"
cfg.MODEL.MASK_FORMER.NHEADS = 8
cfg.MODEL.MASK_FORMER.DROPOUT = 0.0

# R50 四个 stage 的 ShapeSpec（通道数、stride）
input_shape = {
    "res2": ShapeSpec(channels=256, stride=4),
    "res3": ShapeSpec(channels=512, stride=8),
    "res4": ShapeSpec(channels=1024, stride=16),
    "res5": ShapeSpec(channels=2048, stride=32),
}
decoder = MSDeformAttnPixelDecoder(cfg, input_shape)  # @configurable：传 cfg 走 from_config

print("in_features:", decoder.in_features)                  # 预期 ['res2','res3','res4','res5']
print("transformer_in_features:", decoder.transformer_in_features)  # 预期 ['res3','res4','res5']
print("num_fpn_levels:", decoder.num_fpn_levels)            # 预期 1
print("input_proj[0]:", decoder.input_proj[0][0])           # 预期 Conv2d(2048, 256, k=1) → res5
print("input_proj[2]:", decoder.input_proj[2][0])           # 预期 Conv2d(512, 256, k=1) → res3
print("mask_features:", decoder.mask_features)              # 预期 Conv2d(256, 256, k=1)
print("FFN dim:", decoder.transformer.encoder.layers[0].linear1)  # 预期 Linear(256, 1024)，非 2048
n_params = sum(p.numel() for p in decoder.parameters())
print(f"params: {n_params/1e6:.1f}M")
```

**需要观察的现象**（依据源码推导，运行结果待本地验证）：

1. `num_fpn_levels` 打印 `1`，且模块树里只有 `adapter_1`、`layer_1` 一对 extra FPN 卷积。
2. `input_proj[0]` 的 `in_channels` 是 2048（res5），顺序与直觉相反。
3. `mask_features` 是 `kernel_size=1`。
4. encoder 层的 FFN 是 `Linear(256, 1024)`，证明 `DIM_FEEDFORWARD=2048` 配置未生效。

**预期结果**：以上四条全部与源码推导一致，则构造期理解正确。若想看「补两层」的情形，把 `DEFORMABLE_TRANSFORMER_ENCODER_IN_FEATURES` 改成 `["res4", "res5"]` 再构建一次，`num_fpn_levels` 应变为 2，模块树出现 `adapter_1/layer_1` 与 `adapter_2/layer_2` 两对。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `input_proj` 要建成 1×1 卷积 + GroupNorm，而不是纯线性层？

**答案**：输入是 `(B, C, H, W)` 的卷积特征图，1×1 卷积等价于逐像素共享的线性变换，但能直接吃四维张量、且便于后接 GN 做通道归一化稳定训练；纯 `nn.Linear` 需要先 flatten/transpose。GroupNorm(32) 不依赖 batch size，兼容小 batch 多卡的分割训练设置。

**练习 2**：把 `COMMON_STRIDE` 从 4 改成 8，`num_fpn_levels` 变成多少？模块结构会如何变化？

**答案**：\(\log_2 8 - \log_2 8 = 0\)，extra FPN 完全消失（`adapter_*`/`layer_*` 不再创建），`mask_features` 直接作用在 transformer 输出的 1/8 特征上。代价是 mask 嵌入分辨率减半，最终 mask 需上采样 8 倍而非 4 倍，边界更粗糙——这正是 `COMMON_STRIDE` 的意义：**mask_features 的分辨率与质量的权衡旋钮**。

**练习 3**：修改配置键 `MODEL.SEM_SEG_HEAD.DEFORMABLE_TRANSFORMER_ENCODER_N_POINTS` 能否改变每个头的采样点数？

**答案**：不能。该键在 [config.py:103](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L103) 注册后没有任何消费方；`from_config` 不传 `enc_n_points`，生效的是 [msdeformattn.py:27](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/msdeformattn.py#L27) 的默认值 4。要真正修改需改代码传参。

### 4.2 MSDeformAttnTransformerEncoderOnly.forward：多尺度展平与 level_embed

#### 4.2.1 概念说明

MSDeformAttn 算子吃的是**一条拼接后的扁平序列**，而不是多个独立的特征图。`MSDeformAttnTransformerEncoderOnly.forward` 的职责就是把三个尺度「展平并拼接」，同时记好三本账：

- `spatial_shapes`：每个尺度的 `(H, W)`，告知算子如何切分序列；
- `level_start_index`：每个尺度在拼接序列中的起始下标；
- `level_embed`：一个可学习的 `(num_feature_levels, d_model)` 参数，**加到位置编码上**，让算子知道每个 token 属于哪个尺度——因为拼接后尺度信息在序列维度上「消失」了，必须以加性偏置的形式重新注入。

#### 4.2.2 核心流程

```text
forward(srcs=[res5, res4, res3], pos_embeds=[...])      # 低分辨率在前
    ├─ masks = 全 False 的占位掩码（本类不接收 padding mask）
    ├─ 对每个尺度 lvl：
    │    src:      (B,C,H,W) --flatten(2).transpose--> (B, HW, C)
    │    pos:      (B,C,H,W) --------------------> (B, HW, C)
    │    lvl_pos = pos + level_embed[lvl]                 # 尺度身份注入
    ├─ 拼接: src_flatten (B, ΣHW, C)，记录 spatial_shapes 与 level_start_index
    ├─ valid_ratios = 每尺度有效区域比例（占位掩码下恒为 1.0）
    └─ memory = encoder(src_flatten, ...)                 # 6 层 MSDeformAttn encoder
         └─ 每层: MSDeformAttn(自注意力+残差+LN) → FFN(残差+LN)   # post-norm
    返回 (memory, spatial_shapes, level_start_index)
```

#### 4.2.3 源码精读

**类与 level_embed 的定义**：

[msdeformattn.py:23-41](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/msdeformattn.py#L23-L41) 创建一个 `nn.Parameter(torch.Tensor(num_feature_levels, d_model))`（默认 3×256），并用 `normal_` 初始化（第 50 行）。注释标明整个结构来自 Deformable-DETR 的 encoder。

**多尺度展平与拼接**——本模块的核心循环：

[msdeformattn.py:61-84](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/msdeformattn.py#L61-L84) 逐尺度执行 `src.flatten(2).transpose(1, 2)` 把 `(B,C,H,W)` 变 `(B,HW,C)`；第 75 行 `lvl_pos_embed = pos_embed + self.level_embed[lvl].view(1, 1, -1)` 把尺度偏置加进位置编码。随后第 79-83 行沿序列维拼接，并计算：

```python
level_start_index = torch.cat((spatial_shapes.new_zeros((1,)),
                               spatial_shapes.prod(1).cumsum(0)[:-1]))
```

对 1024×1024 输入（R50）：三个尺度 token 数为 \(32^2 + 64^2 + 128^2 = 1024 + 4096 + 16384 = 21504\)，`level_start_index = [0, 1024, 5120]`。第 62 行为每个尺度造**全 False 的占位掩码**——注意 `forward_features` 根本不接收 `ImageList` 的 padding mask，所以 Deformable-DETR 时代的 padding 处理在这里是「在场的惰性代码」：`get_valid_ratio`（[msdeformattn.py:52-59](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/msdeformattn.py#L52-L59)）算出的 `valid_ratios` 恒为 1.0。

**encoder 本体与参考点**：

[msdeformattn.py:134-161](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/msdeformattn.py#L134-L161) 中 `get_reference_points` 用 `torch.linspace(0.5, H_-0.5, ...)` 生成每个 token 的中心坐标并归一化到 `[0,1]`，作为可变形注意力的采样基准点；`forward` 先算参考点，再把同一条扁平序列喂给 6 层 encoder。每层的结构见 [msdeformattn.py:92-131](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/msdeformattn.py#L92-L131)：`self_attn = MSDeformAttn(...)`（第 100 行）+ FFN，**post-norm**（先残差相加再 LayerNorm，与 decoder 的 pre/post-norm 可切换不同，这里写死）。第 124 行的调用方式值得注意：`query` 是加了位置编码的 `src + pos`，而 `value` 输入是不加位置编码的 `src`。

**算子黑盒**：MSDeformAttn 内部（[ms_deform_attn.py:82-125](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/ops/modules/ms_deform_attn.py#L82-L125)）依据 `spatial_shapes`/`level_start_index` 切分序列，对每个 query 在 3 个尺度各采样 4 个点做加权求和；第 116-121 行的 `try/except` 实现 CUDA→PyTorch 参考实现的回退（u1-l2 已讲，u4-l1 精读）。

**位置编码从哪来**：每个尺度的 `pos_embed` 由 `PositionEmbeddingSine` 现场计算，见 [position_encoding.py:29-52](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/position_encoding.py#L29-L52)：对非掩码区域做累和并归一化到 `[0, 2π]`，再除以温度系数后取 sin/cos——即「归一化坐标的正弦编码」，与尺度无关，所以三个尺度可以共用同一个 `pe_layer`。

#### 4.2.4 代码实践

**实践目标**：不跑模型，手工推导再核对 1024×1024 输入下 encoder 的序列账本。

**操作步骤**：

1. 写一个纯 Python 小脚本（示例代码，不需要 torch）：

```python
# 示例代码：核算多尺度序列账本
import math
H = 1024
shapes = [(H // 32, H // 32), (H // 16, H // 16), (H // 8, H // 8)]  # res5, res4, res3
tokens = [h * w for h, w in shapes]
start = [0]
for t in tokens[:-1]:
    start.append(start[-1] + t)
print("spatial_shapes:", shapes)
print("tokens per level:", tokens)
print("level_start_index:", start)
print("total tokens:", sum(tokens))
print("对比单尺度 1/8 全局注意力:", tokens[2] ** 2, "vs MSDeformAttn:", sum(tokens) * 3 * 4)
```

2. 打开 [msdeformattn.py:83](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/msdeformattn.py#L83)，对照 `cumsum` 逻辑核对你手算的 `level_start_index`。
3. 再写 3 行注释贴在第 75 行旁，解释 `level_embed[lvl]` 为什么必须加在**位置编码**上而不是 `src` 上。

**需要观察的现象**：脚本输出 `tokens per level: [1024, 4096, 16384]`、`level_start_index: [0, 1024, 5120]`、`total tokens: 21504`；全局注意力配对数约 \(1.63 \times 10^8\) 对 16384 个 query，而 MSDeformAttn 只需 21504×3×4 ≈ \(2.6 \times 10^5\) 次采样。

**预期结果**：账本与源码 `cumsum` 推导一致（脚本为纯算术，可直接运行；与真实张量值的对应关系待本地验证）。关于第 3 步：加在 `src` 上会污染内容特征本身（同一 token 在进入 FFN、残差路径时都被改动），而加在位置编码上只影响注意力的 query 侧，让算子「知道尺度身份」却不改写内容——与 Transformer 把位置信息加在 Q/K 的惯例一致。

#### 4.2.5 小练习与答案

**练习 1**：如果没有 `level_embed`，模型会损失什么信息？

**答案**：拼接后序列里每个 token 只剩内容与自身坐标的位置编码，两个空间位置相同但尺度不同的 token（例如 res3 的 (0,0) 与 res5 的 (0,0)，归一化坐标都接近 (0,0)）位置编码几乎一样、内容感受野差异巨大却无法区分，采样点预测会混淆不同尺度的语义。`level_embed` 以可学习偏置显式标记尺度身份。

**练习 2**：`get_valid_ratio` 在本项目中恒返回 1.0，为什么代码还保留？

**答案**：这是 Deformable-DETR 的遗产：其原始用途是处理变长输入的 padding，让参考点先映射到有效区域再映射回 padded 坐标系。Mask2Former 的 `forward_features` 不接收 padding mask（第 62 行造全 False 掩码），逻辑退化为恒等，但保留它与算子接口兼容——MSDeformAttn 的签名仍接受 padding mask。

**练习 3**：encoder 层是 pre-norm 还是 post-norm？依据是什么？

**答案**：post-norm。[msdeformattn.py:124-129](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/msdeformattn.py#L124-L129) 中先 `src = src + dropout(src2)` 再 `src = norm1(src)`，LayerNorm 在残差相加**之后**；而 Transformer 解码器层（u4-l2）提供 `normalize_before` 开关。encoder 只有 6 层，post-norm 的训练稳定性问题不突出。

### 4.3 forward_features：split 还原、extra FPN 与三元组契约

#### 4.3.1 概念说明

`forward_features` 是像素解码器对外唯一入口（u3-l4 讲过 `build_pixel_decoder` 会校验它可调用）。它把 4.1 的组件按「**transformer 主干 + extra FPN 补层级**」组织成一条流水线，最终兑现与 `MaskFormerHead.layers` 的三元组契约：

```python
mask_features, transformer_encoder_features, multi_scale_features = \
    self.pixel_decoder.forward_features(features)
```

关键认知：**transformer 输出的扁平序列要先还原成 2D 特征图**（`torch.split` + `view`），extra FPN 才能做上采样相加；且 `multi_scale_features` 的取法是「按 out 列表顺序取前 3 个」——顺序恰好是 1/32、1/16、1/8（从低分辨率到高分辨率），与 `BasePixelDecoder` 的约定一致。

#### 4.3.2 核心流程

```text
forward_features(features = {res2..res5})          # @autocast(enabled=False)
    ├─ 对 transformer_in_features[::-1]（res5→res3）：
    │    x = features[f].float()                    # 禁 half，deformable 不支持 fp16
    │    srcs.append(input_proj[idx](x))            # 通道 → 256
    │    pos.append(pe_layer(x))                    # 归一化正弦编码
    ├─ (y, spatial_shapes, level_start_index) = transformer(srcs, pos)
    │    # y: (B, 21504, 256) 的扁平序列（1024 输入）
    ├─ y = torch.split(y, 每尺度 token 数, dim=1)   # 按 level_start_index 切回 3 段
    ├─ 逐段 view 回 (B, 256, H_l, W_l) → out = [1/32, 1/16, 1/8]
    ├─ extra FPN（对 in_features[:num_fpn_levels][::-1]，默认只有 res2）：
    │    cur = adapter_1(res2)                      # 1x1 拉通道
    │    y = cur + up(out[-1])                      # 上采样相加（bilinear）
    │    y = layer_1(y)                             # 3x3 + GN + ReLU
    │    out.append(y)                              # out = [1/32, 1/16, 1/8, 1/4]
    ├─ multi_scale_features = out 的前 3 个          # [1/32, 1/16, 1/8]
    └─ return (mask_features(out[-1]), out[0], multi_scale_features)
              #      1/4 的 mask 嵌入   1/32 恒返回   3 个尺度
```

1024×1024、batch=1 下的形状账单（R50，通道统一 256 后）：

| 张量 | 形状 | 说明 |
| --- | --- | --- |
| `srcs[0..2]` | (1,256,32,32) / (1,256,64,64) / (1,256,128,128) | res5→res3 投影后 |
| `y`（拼接序列） | (1,21504,256) | 1024+4096+16384 |
| `out[0..3]` | 32² / 64² / 128² / 256² | 1/32→1/4 |
| 第二返回值 `out[0]` | (1,256,32,32) | 1/32 |
| `multi_scale_features` | [(1,256,32,32), (1,256,64,64), (1,256,128,128)] | 1/32、1/16、1/8 |
| `mask_features` | (1,256,256,256) | 1/4 |

#### 4.3.3 源码精读

**fp16 防护**：

[msdeformattn.py:314-322](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/msdeformattn.py#L314-L322) 整个函数被 `@autocast(enabled=False)` 包住，且每个输入 `.float()`——注释写明 `deformable detr does not support half precision`。COCO 基线配置开了 AMP（`SOLVER.AMP.ENABLED: True`），这层防护保证混合精度训练时 CUDA 算子拿到的是 fp32。循环同样按 `[::-1]` 从 res5 走到 res3，与 `input_proj` 的构建顺序严格对齐。

**序列切分与还原**：

[msdeformattn.py:324-339](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/msdeformattn.py#L324-L339) 拿到 encoder 返回的 `y` 后，用 `level_start_index` 相邻差值算出每段长度（最后一段用总长减起点），`torch.split` 切成 3 段，再 `transpose(1,2).view(bs, -1, H_l, W_l)` 还原成 2D。**还原顺序 = 拼接顺序 = res5, res4, res3**，所以 `out` 从低分辨率排到高分辨率。

**extra FPN 补层级**：

[msdeformattn.py:341-351](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/msdeformattn.py#L341-L351) 对 `self.in_features[:self.num_fpn_levels][::-1]`（默认即 `[res2]`）执行 FPN 单元：`lateral_conv`（即 `adapter_1`）先 1×1 拉齐通道，加上 `F.interpolate(out[-1], size=..., mode="bilinear", align_corners=False)` 的上采样（第 349 行），再过 `output_conv`（`layer_1`，3×3+GN+ReLU）。**注意第 348 行注释写着 "we use nearest upsampling here"，但代码实际用的是 bilinear**——注释与代码不一致，以代码为准（这种注释漂移在读开源项目时很常见，值得警觉）。若有多个 FPN 层级，每轮 `out[-1]` 滚动更新为最新高层级输出，形成逐级自顶向下的传递。

**三元组契约的兑现**：

[msdeformattn.py:353-358](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/msdeformattn.py#L353-L358) 先从 `out` 顺序取前 `maskformer_num_feature_levels = 3` 个作为 `multi_scale_features`（1/32、1/16、1/8），再返回：

```python
return self.mask_features(out[-1]), out[0], multi_scale_features
```

- 第一项：`mask_features` 作用在 `out[-1]`（最高分辨率、默认 1/4）上，产出 mask 嵌入；
- 第二项：`out[0]`（1/32 的 encoder 输出）。对比 u3-l4：`BasePixelDecoder` 恒返回 `None`，`TransformerEncoderPixelDecoder` 返回 encoder 特征供 v1 的 `TRANSFORMER_IN_FEATURE="transformer_encoder"` 使用；**v2 默认的 `multi_scale_pixel_decoder` 模式下这一项不被消费**——消费方见 [mask_former_head.py:118-132](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/mask_former_head.py#L118-L132)，`multi_scale_pixel_decoder` 分支只把 `multi_scale_features` 和 `mask_features` 传给 predictor；
- 第三项：长度恒为 3 的尺度列表，供 `MultiScaleMaskedTransformerDecoder` 做逐尺度轮转的 masked cross-attention（u4-l3）。

这就解释了契约「为什么是三元组」：它是 v1（单尺度 `transformer_encoder`）与 v2（多尺度）两种用法共用的接口形状，`None`/`out[0]` 的差异由 head 的 `TRANSFORMER_IN_FEATURE` 分支消化。

#### 4.3.4 代码实践

**实践目标**：对 1024×1024 输入完整追踪一次 `forward_features`，验证 4.3.2 的形状账单，并亲眼确认「extra FPN 只补一层」。

**操作步骤**（示例代码，脚本 `trace_pixel_decoder.py`，在仓库根目录运行；需要 u1-l2 搭好的环境，CPU 即可——MSDeformAttn 会走 PyTorch 回退实现）：

```python
# 示例代码：追踪 MSDeformAttnPixelDecoder.forward_features 的形状
import torch
from detectron2.config import get_cfg
from detectron2.layers import ShapeSpec
from detectron2.projects.deeplab import add_deeplab_config
from mask2former import add_maskformer2_config
from mask2former.modeling.pixel_decoder.msdeformattn import MSDeformAttnPixelDecoder

cfg = get_cfg(); add_deeplab_config(cfg); add_maskformer2_config(cfg)
cfg.MODEL.SEM_SEG_HEAD.IN_FEATURES = ["res2", "res3", "res4", "res5"]
cfg.MODEL.SEM_SEG_HEAD.DEFORMABLE_TRANSFORMER_ENCODER_IN_FEATURES = ["res3", "res4", "res5"]
cfg.MODEL.SEM_SEG_HEAD.TRANSFORMER_ENC_LAYERS = 6
cfg.MODEL.SEM_SEG_HEAD.COMMON_STRIDE = 4
cfg.MODEL.SEM_SEG_HEAD.CONVS_DIM = 256
cfg.MODEL.SEM_SEG_HEAD.MASK_DIM = 256
cfg.MODEL.SEM_SEG_HEAD.NORM = "GN"
cfg.MODEL.MASK_FORMER.NHEADS = 8
cfg.MODEL.MASK_FORMER.DROPOUT = 0.0

input_shape = {k: ShapeSpec(channels=c, stride=s) for k, c, s in [
    ("res2", 256, 4), ("res3", 512, 8), ("res4", 1024, 16), ("res5", 2048, 32)]}
decoder = MSDeformAttnPixelDecoder(cfg, input_shape).eval()

H = 1024; B = 1
features = {
    "res2": torch.randn(B, 256,  H // 4,  H // 4),
    "res3": torch.randn(B, 512,  H // 8,  H // 8),
    "res4": torch.randn(B, 1024, H // 16, H // 16),
    "res5": torch.randn(B, 2048, H // 32, H // 32),
}

# 用钩子偷看 transformer 的输入输出与 extra FPN 的中间量
seen = {}
def mk_hook(name):
    def hook(mod, inp, out):
        seen[name] = (inp[0].shape if torch.is_tensor(inp[0]) else None,
                      out[0].shape if isinstance(out, tuple) else out.shape)
    return hook
decoder.transformer.register_forward_hook(mk_hook("transformer"))
decoder.adapter_1.register_forward_hook(mk_hook("adapter_1"))
decoder.layer_1.register_forward_hook(mk_hook("layer_1"))

with torch.no_grad():
    mask_features, y0, multi_scale_features = decoder.forward_features(features)

for k, v in seen.items():
    print(k, "in/out:", v)
print("第二返回值 out[0]:", y0.shape)
print("multi_scale_features:", [tuple(t.shape) for t in multi_scale_features])
print("mask_features:", tuple(mask_features.shape))
# 期望：
# transformer  in (B,21504,256) 拼接序列（钩子看到的是 encoder 展平前的 srcs 列表，视实现而定）
# adapter_1    in (1,256,256,256)（res2 原始特征）
# multi_scale_features [(1,256,32,32),(1,256,64,64),(1,256,128,128)]  # 1/32、1/16、1/8
# mask_features (1,256,256,256)                                       # 1/4
```

**需要观察的现象**（依据源码推导，运行结果待本地验证）：

1. `multi_scale_features` 三个张量的边长为 32/64/128（即 1/32、1/16、1/8），通道均为 256；
2. `mask_features` 边长 256（1/4），通道 256（`MASK_DIM`）；
3. 模块树中只有 `adapter_1`/`layer_1` 一对 extra FPN 卷积，钩子只有 res2 进出——证明 extra FPN 只补了一个层级；
4. 把 `H` 改成 512 再跑，所有边长应精确减半（形状关系与输入尺寸线性成比例）。

**预期结果**：观察值与本讲 4.3.2 的形状账单逐项吻合。若不吻合，优先检查 `IN_FEATURES` 与 backbone 实际输出名是否一致（u3-l2 的对接约束）。

#### 4.3.5 小练习与答案

**练习 1**：`multi_scale_features` 为什么按「1/32、1/16、1/8」从低分辨率到高分辨率排列，而不是反过来？

**答案**：因为 `out` 列表的构建顺序天然如此：transformer 的输入按 `transformer_in_features[::-1]`（res5→res3）组织，encoder 输出还原后 `out[0]` 就是 1/32；extra FPN 再往尾部追加更高分辨率。`MultiScaleMaskedTransformerDecoder` 的解码层用 `level_index = i % 3` 在该列表上轮转（u4-l3），顺序是这个约定的另一半——两边共同约定「索引 0 是最粗尺度」。

**练习 2**：第二返回值 `out[0]`（1/32）在默认配置下有人消费吗？为什么还要返回？

**答案**：没有。`MaskFormerHead.layers` 在 `TRANSFORMER_IN_FEATURE == "multi_scale_pixel_decoder"` 时只把 `multi_scale_features` 与 `mask_features` 传给 predictor；`out[0]` 只在 `transformer_encoder` 分支（v1 用法，且那要求 `TransformerEncoderPixelDecoder`）被读取。返回它是为了维持 `forward_features` 三元组的统一接口形状，让三类像素解码器可互换。

**练习 3**：若把 `TRANSFORMER_ENC_LAYERS` 设为 0会发生什么？配置里默认值是多少、COCO 基线设为多少？

**答案**：`_get_clones(encoder_layer, 0)` 会生成空的层列表，encoder 的 forward 循环零次、`memory` 原样返回 `src_flatten`——多尺度特征退化为「投影+拼接+拆开」的无注意力版本，模型仍能跑但性能大幅下降。[config.py:69](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L69) 默认值为 0（因为默认 `PIXEL_DECODER_NAME` 是 `BasePixelDecoder`），COCO 基线在 [maskformer2_R50_bs16_50ep.yaml:18](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml#L18) 显式设为 6。

## 5. 综合实践

**任务：用「两组配置对照」完整验证 num_fpn_levels 公式与三元组契约。**

在 4.3.4 脚本的基础上完成：

1. **对照组 A（默认）**：`DEFORMABLE_TRANSFORMER_ENCODER_IN_FEATURES = ["res3","res4","res5"]`、`COMMON_STRIDE = 4`。跑通 `forward_features`，记录：transformer 各输入尺度、extra FPN 层级数、`multi_scale_features` 形状、`mask_features` 形状。
2. **对照组 B（缩范围）**：改为 `["res4","res5"]`，其余不变，重新构建 decoder 并前向。
3. **对照分析**：
   - 手算两组的 \(\log_2(\min\text{stride}) - \log_2(4)\)：A 组 = 3−2 = 1，B 组 = 4−2 = 2；
   - 验证 B 组模块树出现 `adapter_1/layer_1`（res3）与 `adapter_2/layer_2`（res2）两对卷积，且 extra FPN 链为 1/16→1/8→1/4 逐级上采样；
   - 验证两组的**三元组契约不变**：`mask_features` 恒为 1/4、`multi_scale_features` 恒为 [1/32, 1/16, 1/8] 长度 3——变的只是「这些特征由谁算出来」；
   - 打印两组参数量，解释为什么 B 组参数更多（多了两对卷积、input_proj 少一个）却少了 res3 上的可变形注意力开销。
4. **写一段结论注释**：用一句话回答「`num_fpn_levels` 公式衡量的是什么？」——衡量 transformer 覆盖的最细尺度与 `common_stride` 之间相差几个二倍层级，即 FPN 需要补的高分辨率层数。

预期（依据源码推导，运行结果待本地验证）：A、B 两组的返回三元组形状完全一致；B 组 transformer 的 token 总数从 21504 降到 5120（64²+32²），`adapter`/`layer` 从 1 对变 2 对。

## 6. 本讲小结

- `MSDeformAttnPixelDecoder` 是 v2 默认像素解码器：res3/res4/res5 经 `input_proj`（1×1+GN，**按 res5→res3 顺序构建**）拉齐到 256 维，送入 6 层多尺度可变形注意力 encoder，再由 extra FPN 把分辨率补到 1/4。
- `MSDeformAttnTransformerEncoderOnly.forward` 把三个尺度展平拼成一条 `(B, ΣHW, 256)` 序列，用 `level_embed` 注入尺度身份，用 `spatial_shapes`/`level_start_index` 记账；padding 掩码是恒为空的占位逻辑。
- `num_fpn_levels = log2(min_stride) − log2(common_stride)`：默认配置下等于 **1**，extra FPN 只补 res2 一层（1/8→1/4 只差一个层级），不是两层。
- `forward_features` 全程禁 autocast 并 `.float()`（可变形注意力不支持 fp16）；encoder 输出用 `torch.split` + `view` 还原成 2D 后，extra FPN 做「1×1 拉通道 + bilinear 上采样相加 + 3×3 精修」（注释写 nearest，代码是 bilinear）。
- 三元组契约：`(1/4 的 mask_features, 1/32 的 out[0], [1/32,1/16,1/8] 的 multi_scale_features)`；默认 `multi_scale_pixel_decoder` 模式下第二项无人消费，返回它只为统一三类像素解码器的接口。
- 三个「坑」：encoder FFN 维度硬编码 1024（`DIM_FEEDFORWARD` 配置不影响它）；`DEFORMABLE_TRANSFORMER_ENCODER_N_POINTS/N_HEADS` 是无人读取的死配置；nheads 实际取自 `MASK_FORMER.NHEADS`。

## 7. 下一步学习建议

本讲结束后，像素解码器（三段式的第二段）就完整了。下一步按依赖顺序：

1. **u4-l1（MSDeformAttn 算子：从 PyTorch 到 CUDA）**：把本讲当黑盒的算子拆开——采样点预测、双线性插值加权求和的数学形式，以及 `ms_deform_attn_core_pytorch` 参考实现与 CUDA 扩展的关系。
2. **u4-l2 / u4-l3（Transformer 解码器）**：看 `multi_scale_features` 的三个尺度如何被解码层以 `i % 3` 轮转消费，以及 `mask_features` 如何与 query 嵌入 einsum 成 mask 预测——那才是 mask classification 范式的正片。
3. 想先动手的读者可以直接跳到 **u8-l2（自定义 Pixel Decoder）**：本讲的「三元组契约 + 尺度约定」正是自定义实现必须遵守的全部规则。

建议顺带重读 [msdeformattn.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/msdeformattn.py) 一遍：整文件不足 360 行，现在你应该能不查资料讲清每一行的意图。

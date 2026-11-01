# BasePixelDecoder 与 TransformerEncoderPixelDecoder

## 1. 本讲目标

上一篇（u3-l2）我们看清了 `MaskFormerHead` 这个组装器：它自己不含计算层，只把 `pixel_decoder` 与 `predictor` 两个子模块接线，并固化了一条「`forward_features` 三元组」契约。本讲就钻进契约的提供方——像素解码器中最基础的两个实现，学完你应该能够：

1. 读懂 `build_pixel_decoder` 的两步逻辑：查注册表 + 校验 `forward_features` 可调用，并说出这条校验与 `ADVANCED_USAGE.md` 契约文档的对应关系。
2. 逐行解释 `BasePixelDecoder`：它是一个改造版 FPN——lateral 1×1 conv 拉齐通道、自顶向下 nearest 上采样相加、3×3 output conv 精修，最后用一层卷积在 1/4 分辨率产出 `mask_features`，同时顺手收集 3 个尺度的多尺度特征。
3. 解释 `TransformerEncoderPixelDecoder` 如何在 FPN 的最粗尺度（1/32）顶端插入一个全局自注意力 Transformer encoder，以及它为什么能把三元组的第二项从 `None` 变成真实的 encoder 特征。
4. 独立完成对比实验：同一份配置下分别构建两种像素解码器，验证三元组形状契约与参数量差异。

本讲不涉及 CUDA 算子：这两个解码器只用了普通卷积和 `nn.MultiheadAttention`，因此**无需编译 MSDeformAttn 扩展即可跑通全部实践**（对比：默认的 `MSDeformAttnPixelDecoder` 是下一讲 u3-l5 的内容）。

## 2. 前置知识

### 2.1 像素解码器在干什么

backbone（R50 或 Swin）输出的是一座特征金字塔：`res2`(1/4) → `res5`(1/32)，分辨率递减、语义递增、通道数各异（R50 为 256/512/1024/2048）。Transformer 解码器需要的是**通道统一、数量固定（3 个）**的多尺度特征，mask 预测需要的是**高分辨率**的逐像素嵌入。像素解码器就是这两者之间的「格式转换器」：

```text
backbone 金字塔（通道不一，4 个尺度）
        │
        ▼
pixel decoder（统一到 CONVS_DIM=256 通道）
        │
        ├── mask_features      1/4 分辨率、MASK_DIM 通道 ──► 与 query 点积"印"出 mask
        ├── 第二项              Base 版恒为 None / TransformerEncoder 版为 1/32 encoder 特征
        └── multi_scale_features  [1/32, 1/16, 1/8] 共 3 个 ──► Transformer 解码器交叉注意力
```

### 2.2 FPN 一分钟回顾

FPN（Feature Pyramid Network）的经典三件套，本讲的 `BasePixelDecoder` 几乎原样照搬：

- **lateral conv（1×1）**：把各层通道数拉齐到统一宽度（`CONVS_DIM=256`），不改分辨率；
- **自顶向下路径 + 最近邻上采样**：把最粗、语义最强的特征逐级放大，与细分辨率特征逐元素相加；
- **output conv（3×3）**：相加后做一次卷积精修，消除上采样带来的混叠。

用公式写出自顶向下的递推（\( x_i \) 为 backbone 特征，\( i \) 从最粗的 res5 走到最细的 res2）：

\[
y_{\text{res5}} = \mathrm{Out}_{\text{res5}}(x_{\text{res5}}), \qquad
y_i = \mathrm{Out}_i\big(\,\mathrm{Lat}_i(x_i) + \mathrm{Up}_{\text{nearest}}(y_{i-1})\,\big)
\]

### 2.3 全局自注意力的代价：为什么只放在 1/32

标准 Transformer encoder 把特征图展平成 token 序列做全局注意力：

\[
\mathrm{Attn}(Q,K,V)=\mathrm{softmax}\!\left(\frac{QK^{\top}}{\sqrt{d_k}}\right)V
\]

注意力矩阵大小是 \( N \times N \)（\(N\) 为 token 数）。对 1024×1024 输入：

| 特征图 | 分辨率 | token 数 \(N\) | \(N^2\) |
| --- | --- | --- | --- |
| res5（1/32） | 32×32 | 1024 | ≈ 1.0×10⁶（可行） |
| res4（1/16） | 64×64 | 4096 | ≈ 1.6×10⁷（勉强） |
| res2（1/4） | 256×256 | 65536 | ≈ 4.3×10⁹（不可行） |

这就是 `TransformerEncoderPixelDecoder` 只把 Transformer 放在最粗的 1/32 尺度上的根本原因；也解释了 Mask2Former 为什么要发明多尺度**可变形**注意力（采样少量点而非全局两两配对，u3-l5/u4-l1 展开）来替换它。

### 2.4 仓库里的三种像素解码器

| 类名 | 出处 | 三元组第二项 | 是否需要 CUDA 扩展 |
| --- | --- | --- | --- |
| `BasePixelDecoder` | 本讲，源自 MaskFormer(v1) | 恒为 `None` | 否 |
| `TransformerEncoderPixelDecoder` | 本讲，v1 默认像素解码器 | 1/32 encoder 特征 | 否 |
| `MSDeformAttnPixelDecoder` | u3-l5，Mask2Former 默认 | 恒为 `None` | 是（CPU 走 PyTorch 回退） |

配置默认值在 `add_maskformer2_config` 中注册为 `BasePixelDecoder`，而仓库里所有 Mask2Former 训练配置都显式覆盖为 `MSDeformAttnPixelDecoder`（见 [configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml:L13-L18](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml#L13-L18)，该文件同时把 `TRANSFORMER_ENC_LAYERS` 设为 6）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [mask2former/modeling/pixel_decoder/fpn.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py) | 本讲主角：`build_pixel_decoder`、`BasePixelDecoder`、`TransformerEncoderOnly`、`TransformerEncoderPixelDecoder` |
| [ADVANCED_USAGE.md](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/ADVANCED_USAGE.md) | 官方对「自定义 pixel decoder 契约」的唯一文档说明，含 `TransformerEncoderPixelDecoder` 的示例配置 |
| [mask2former/modeling/meta_arch/mask_former_head.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/mask_former_head.py) | 上游消费者：`MaskFormerHead.layers` 按三元组契约调用 `forward_features`（u3-l2 已精读，本讲引用其分派与断言） |
| [mask2former/modeling/transformer_decoder/position_encoding.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/position_encoding.py) | `PositionEmbeddingSine`：给 Transformer 提供 2D 正弦位置编码 |
| [mask2former/modeling/transformer_decoder/transformer.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/transformer.py) | DETR 风格的 `TransformerEncoder` / `TransformerEncoderLayer`（本讲只用 encoder 部分） |
| [mask2former/config.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py) | `PIXEL_DECODER_NAME`、`TRANSFORMER_ENC_LAYERS`、`CONVS_DIM` 等键的默认值 |
| [configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml) | 综合实践用的真实配置（R50 + COCO 全景） |

## 4. 核心概念与源码讲解

### 4.1 `build_pixel_decoder`：注册表查询与接口校验

#### 4.1.1 概念说明

u3-l2 讲过 `MaskFormerHead.from_config` 里的一行 `build_pixel_decoder(cfg, input_shape)`。这个函数只做两件事：

1. **查表**：从 `SEM_SEG_HEADS_REGISTRY` 里按 `cfg.MODEL.SEM_SEG_HEAD.PIXEL_DECODER_NAME` 取出类，以 `(cfg, input_shape)` 调用（`@configurable` 惯例下先走 `from_config` 再 `__init__`）；
2. **验货**：检查产物身上有没有**可调用的** `forward_features` 方法，没有就抛 `ValueError`。

注意一个容易踩的坑：pixel decoder 与 `MaskFormerHead` 共用同一个注册表 `SEM_SEG_HEADS_REGISTRY`——它并不是一个「pixel decoder 专用」的注册表，而是靠 `build_pixel_decoder` 的验货逻辑把「能当像素解码器用」这一子集筛出来。

#### 4.1.2 核心流程

```text
cfg.MODEL.SEM_SEG_HEAD.PIXEL_DECODER_NAME
        │
        ▼
SEM_SEG_HEADS_REGISTRY.get(name) ──► 类
        │  (cfg, input_shape)
        ▼
@configurable: from_config(cfg, input_shape) → kwargs → __init__(**kwargs)
        │
        ▼
getattr(model, "forward_features", None)
        │
   callable? ──否──► raise ValueError（契约不满足，立即失败）
        │是
        ▼
返回 model（挂到 MaskFormerHead.pixel_decoder）
```

这条「构造期就校验」的设计让配置错误在**构建模型的瞬间**暴露，而不是等到第一次前向才炸——与 `MaskFormerHead.layers` 里运行期 `assert transformer_encoder_features is not None`（见 4.3）形成两道防线。

#### 4.1.3 源码精读

[mask2former/modeling/pixel_decoder/fpn.py:L21-L33](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L21-L33) —— `build_pixel_decoder` 全文：从 `cfg.MODEL.SEM_SEG_HEAD.PIXEL_DECODER_NAME` 取名字查表构建，然后用 `getattr` + `callable` 校验 `forward_features`，不满足即抛出带类名的 `ValueError`。

两个阅读提醒：

- **文档字符串陷阱**：[fpn.py:L23](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L23) 的 docstring 写的是 `cfg.MODEL.MASK_FORMER.PIXEL_DECODER_NAME`，但 [fpn.py:L25](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L25) 实际读取的是 `cfg.MODEL.SEM_SEG_HEAD.PIXEL_DECODER_NAME`。以代码为准（默认值注册在 [config.py:L71](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L71)）。
- **无用 import**：[fpn.py:L11](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L11) 导入的 `autocast` 在本文件从未使用（真正用的是下一讲的 msdeformattn.py），不要因此误以为本文件有混合精度逻辑。

这条校验对应的官方契约写在 [ADVANCED_USAGE.md:L18-L22](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/ADVANCED_USAGE.md#L18-L22)：`forward_features` 必须返回三个值——(1) 1/4 分辨率的 `mask_features`；(2) 第二项可以直接返回 `None`；(3) 长度为 3 的 `multi_scale_features`（官方默认 1/32、1/16、1/8，但允许任意分辨率）。文档紧跟着给出的示例配置（[ADVANCED_USAGE.md:L24-L33](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/ADVANCED_USAGE.md#L24-L33)）正是把 `PIXEL_DECODER_NAME` 换成本讲的 `TransformerEncoderPixelDecoder` 并设 `TRANSFORMER_ENC_LAYERS: 6`——这也是第 5 节综合实践的蓝本。

#### 4.1.4 代码实践：亲眼看到注册表与校验

**实践目标**：确认 `import mask2former` 后注册表里有哪些像素解码器，并复现 `build_pixel_decoder` 的 `ValueError` 分支。

**操作步骤**（示例代码，保存为 `Mask2Former-tutorial/practice_u3l4_registry.py`，在仓库根目录运行 `python Mask2Former-tutorial/practice_u3l4_registry.py`）：

```python
# 示例代码：需要已安装 detectron2 与本仓库（pip install -e . 或源码在 PYTHONPATH 上）
import torch
from torch import nn
from detectron2.modeling import SEM_SEG_HEADS_REGISTRY
from detectron2.config import get_cfg

import mask2former  # import 即注册（u1-l3 讲过的机制）
from mask2former import add_maskformer2_config
from mask2former.modeling.pixel_decoder.fpn import build_pixel_decoder

# 1) 看注册表里有哪些名字
names = SEM_SEG_HEADS_REGISTRY.get_registered_names()  # 旧版 fvcore 可改用 list(SEM_SEG_HEADS_REGISTRY._obj_map)
for n in sorted(names):
    print(n)

# 2) 注册一个没有 forward_features 的假头，验证契约校验
@SEM_SEG_HEADS_REGISTRY.register()
class DummyNoContractHead(nn.Module):
    def forward(self, features):
        return features

cfg = get_cfg()
add_maskformer2_config(cfg)
cfg.MODEL.SEM_SEG_HEAD.PIXEL_DECODER_NAME = "DummyNoContractHead"
try:
    build_pixel_decoder(cfg, {})
except ValueError as e:
    print("ValueError as expected:\n", e)
```

**需要观察的现象**：第 1 步打印中应能看到 `BasePixelDecoder`、`TransformerEncoderPixelDecoder`、`MSDeformAttnPixelDecoder` 与 `MaskFormerHead` 等名字同处一个注册表；第 2 步应抛出 `ValueError: Only SEM_SEG_HEADS with forward_features method can be used as pixel decoder. ...`。

**预期结果**：注册表共置两类角色（head 与 pixel decoder），契约校验对没有 `forward_features` 的类立即报错。待本地验证（本讲未代跑；无 GPU 也能运行，只需 CPU 版 PyTorch + detectron2）。

#### 4.1.5 小练习与答案

**练习 1**：`build_pixel_decoder` 为什么校验 `forward_features` 而不校验 `forward`？

**答案**：因为上游 `MaskFormerHead.layers` 直接调用 `self.pixel_decoder.forward_features(features)`（[mask2former/modeling/meta_arch/mask_former_head.py:L119](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/mask_former_head.py#L119)），这才是真正被消费的接口；`forward` 只是一个带警告的转发（见 4.2.3 末尾）。校验「实际被调用的方法」才能在构造期拦住契约违规。

**练习 2**：如果两个不同的类用同一个名字注册到 `SEM_SEG_HEADS_REGISTRY`，会发生什么？

**答案**：fvcore 的 `Registry` 在重复注册同名对象时会抛出 "Cannot register duplicate" 的 `AssertionError`。这也是上面实践脚本只能运行一次、重复执行需换名字或重启进程的原因。

**练习 3**：把 `PIXEL_DECODER_NAME` 写成一个不存在的名字，错误是在 `build_pixel_decoder` 里抛的吗？

**答案**：不是。名字查表失败发生在更早的 `SEM_SEG_HEADS_REGISTRY.get(name)`（内部抛 `AssertionError: Object 'XXX' not found`），根本走不到 `forward_features` 校验那一步；`build_pixel_decoder` 只在「类存在但契约不符」时抛 `ValueError`。

### 4.2 `BasePixelDecoder`：一个改造过的 FPN

#### 4.2.1 概念说明

`BasePixelDecoder` 是仓库里最朴素的可换插件（[fpn.py:L36-L38](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L36-L38) 的注释就写着 "This is a modified FPN decoder"）。它解决的问题：**把通道各异、4 个尺度的 backbone 金字塔，转换成一份 1/4 高分辨率嵌入 + 3 个等通道多尺度特征**。

它对应 MaskFormer(v1) 时代「无 Transformer 的像素解码器」这条技术线：多尺度融合完全靠卷积。类名里的 "Base" 指它是 `TransformerEncoderPixelDecoder`（以及语义上的其他变体）的公共基类，也是 `add_maskformer2_config` 里的配置默认值。

#### 4.2.2 核心流程

`forward_features` 按**自顶向下**（top-down，从最粗的 res5 到最细的 res2）顺序执行：

```text
输入 features = {res2:1/4, res3:1/8, res4:1/16, res5:1/32}（R50 通道 256/512/1024/2048）

第 0 步（res5，最粗）：  y = OutConv_3×3(res5)                 # 无 lateral：没有更粗的层可加
第 1 步（res4）：        y = OutConv_3×3( Lateral_1×1(res4) + Up_nearest(y) )
第 2 步（res3）：        y = OutConv_3×3( Lateral_1×1(res3) + Up_nearest(y) )
第 3 步（res2，最细）：  y = OutConv_3×3( Lateral_1×1(res2) + Up_nearest(y) )

收集：multi_scale_features = [第0步y(1/32), 第1步y(1/16), 第2步y(1/8)]   # 只取前 3 个尺度
返回：( mask_features_3×3(y@1/4), None, multi_scale_features )
```

两个关键取舍：

- **`res2` 的 1/4 输出只进 `mask_features`，不进 `multi_scale_features`**。多尺度特征收集有硬上限 `maskformer_num_feature_levels = 3`（[fpn.py:L123](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L123) 注释 "always use 3 scales"），于是 1/4 层的全部信息只通过 `mask_features` 这一条通道流向 mask 预测。
- **上采样用 nearest 而非双线性**：沿用 FPN 官方实现的选型，省计算且不引入平滑模糊（[fpn.py:L148-L149](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L148-L149) 的注释 "Following FPN implementation, we use nearest upsampling here"）。

#### 4.2.3 源码精读

**(a) 构造：按 stride 排序、逐层搭卷积、再倒序成自顶向下**

[mask2former/modeling/pixel_decoder/fpn.py:L58-L60](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L58-L60) —— 把 `input_shape`（`Dict[str, ShapeSpec]`）按 stride 升序排序，得到 `self.in_features = ["res2","res3","res4","res5"]` 与对应通道数列表。**排序**而非假定顺序，保证即使 backbone 的 output_shape 顺序变化也能正确工作。

[mask2former/modeling/pixel_decoder/fpn.py:L62-L107](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L62-L107) —— 卷积搭建循环。对每个特征层：

- 最后一层（idx 最大 = res5，[L67-L83](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L67-L83)）：只建一个 **3×3 output conv**（`in_channels → conv_dim`，带 norm+ReLU），lateral 位置放 `None`——它是最粗层，没有更粗的特征可以相加；
- 其余层（[L84-L107](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L84-L107)）：建 **1×1 lateral conv**（`in_channels → conv_dim`，只拉通道）+ **3×3 output conv**（`conv_dim → conv_dim`）。

命名规则是 `adapter_{idx+1}`（lateral）与 `layer_{idx+1}`（output），即 `adapter_1/2/3` 对应 res2/3/4，`layer_1/2/3/4` 对应 res2/3/4/5——这些名字会出现在 checkpoint 的参数 key 里（v1 权重迁移时 u3-l2 见过 `sem_seg_head.adapter_1.weight` 这类前缀）。

[mask2former/modeling/pixel_decoder/fpn.py:L108-L111](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L108-L111) —— 把两个 Python 列表**倒序**存放（注释：Place convs into top-down order），于是 `self.lateral_convs[0]` 是 res5 的 `None`、`self.output_convs[0]` 是 `layer_4`。构造时按 stride 升序建、使用时按分辨率从粗到细取——这两个顺序相反，是读这段代码时最容易晕的点。

[mask2former/modeling/pixel_decoder/fpn.py:L113-L123](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L113-L123) —— 最后一层 `mask_features` 卷积：3×3、`conv_dim → mask_dim`（默认 256→256），作用在最细的 1/4 特征上；同时写下 `self.maskformer_num_feature_levels = 3`。

**(b) from_config：四个配置键**

[mask2former/modeling/pixel_decoder/fpn.py:L125-L134](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L125-L134) —— 从 `cfg` 读四个值：用 `IN_FEATURES` 过滤 `input_shape`（backbone 多给的层直接丢弃）、`CONVS_DIM`（中间通道）、`MASK_DIM`（mask 嵌入通道）、`NORM`（卷积归一化，COCO 配置里是 `"GN"`；`norm == ""` 时卷积才带 bias，[L65](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L65)）。这些键的默认值见 [config.py:L67-L71](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L67-L71)。

**(c) forward_features：自顶向下主循环**

[mask2former/modeling/pixel_decoder/fpn.py:L136-L154](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L136-L154) —— 全文只有 18 行：

- `for idx, f in enumerate(self.in_features[::-1])`：从 res5 走到 res2；
- `lateral_conv is None` 分支（[L144-L145](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L144-L145)）：首层直接过 output conv；
- 其余分支（[L146-L150](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L146-L150)）：`cur_fpn = lateral_conv(x)` 拉通道 → `y = cur_fpn + F.interpolate(y, size=cur_fpn.shape[-2:], mode="nearest")` 上采样相加 → `y = output_conv(y)` 精修；
- [L151-L153](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L151-L153)：只把**前 3 个**尺度的输出收进 `multi_scale_features`（即 1/32、1/16、1/8，顺序从粗到细）；
- [L154](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L154)：返回三元组 `(self.mask_features(y), None, multi_scale_features)`——此时循环结束后的 `y` 恰好是 res2 层（1/4）输出，第二项恒为 `None`。

**(d) forward：带警告的转发**

[mask2former/modeling/pixel_decoder/fpn.py:L156-L159](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L156-L159) —— 普通 `forward` 只打一条 warning（"Calling forward() may cause unpredicted behavior"）后转发给 `forward_features`。存在的意义是兼容 detectron2「head 都有 forward」的通用习惯，真正的契约入口是 `forward_features`。

#### 4.2.4 代码实践：手工喂数据，验证三元组契约

**实践目标**：不构建整个模型，单独构建 `BasePixelDecoder`，用一份假特征验证三元组的形状与顺序。

**操作步骤**（示例代码，保存为 `Mask2Former-tutorial/practice_u3l4_base.py`）：

```python
# 示例代码
import torch
from detectron2.layers import ShapeSpec
from detectron2.config import get_cfg

from mask2former import add_maskformer2_config
from mask2former.modeling.pixel_decoder.fpn import build_pixel_decoder

cfg = get_cfg()
add_maskformer2_config(cfg)
cfg.MODEL.SEM_SEG_HEAD.PIXEL_DECODER_NAME = "BasePixelDecoder"
cfg.MODEL.SEM_SEG_HEAD.IN_FEATURES = ["res2", "res3", "res4", "res5"]
cfg.MODEL.SEM_SEG_HEAD.CONVS_DIM = 256
cfg.MODEL.SEM_SEG_HEAD.MASK_DIM = 256
cfg.MODEL.SEM_SEG_HEAD.NORM = "GN"

input_shape = {  # 模拟 R50 的 output_shape()
    "res2": ShapeSpec(channels=256, stride=4),
    "res3": ShapeSpec(channels=512, stride=8),
    "res4": ShapeSpec(channels=1024, stride=16),
    "res5": ShapeSpec(channels=2048, stride=32),
}
pd = build_pixel_decoder(cfg, input_shape)
print("children:", [n for n, _ in pd.named_children()])
print("in_features:", pd.in_features)
print("lateral_convs:", [type(m).__name__ if m is not None else None for m in pd.lateral_convs])

H = 1024
feats = {
    "res2": torch.rand(1, 256, H // 4, H // 4),
    "res3": torch.rand(1, 512, H // 8, H // 8),
    "res4": torch.rand(1, 1024, H // 16, H // 16),
    "res5": torch.rand(1, 2048, H // 32, H // 32),
}
mask_features, enc_feat, msf = pd.forward_features(feats)
print("mask_features:", tuple(mask_features.shape))
print("second value :", enc_feat)
print("multi_scale  :", [tuple(f.shape) for f in msf])
print("params(M)    :", sum(p.numel() for p in pd.parameters()) / 1e6)
```

**需要观察的现象**：`children` 里应出现 `adapter_1..3`、`layer_1..4`、`mask_features`；`lateral_convs` 列表应为 `[None, Conv2d, Conv2d, Conv2d]`（自顶向下序，res5 在最前）；`enc_feat` 为 `None`。

**预期结果**（由源码推得，待本地验证）：`mask_features` 形状 `(1, 256, 256, 256)`（1/4 分辨率、MASK_DIM 通道）；`multi_scale` 依次为 `(1,256,32,32)`、`(1,256,64,64)`、`(1,256,128,128)`——即 1/32、1/16、1/8，从粗到细。CPU 即可运行。

#### 4.2.5 小练习与答案

**练习 1**：`multi_scale_features` 里三个元素的顺序是什么？为什么 res2（1/4）不在其中？

**答案**：顺序是 [1/32, 1/16, 1/8]（从粗到细），因为主循环按 `self.in_features[::-1]` 自顶向下执行、每层 append 进列表；res2 是第 4 个算出的尺度，而收集上限 `maskformer_num_feature_levels = 3`（[fpn.py:L151-L153](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L151-L153)），它的信息只通过 `mask_features` 流出。

**练习 2**：`self.output_convs[0]` 对应哪个子模块？它是 `layer_1` 还是 `layer_4`？

**答案**：是 `layer_4`。构造循环按 stride 升序生成 `output_convs = [layer_1, layer_2, layer_3, layer_4]`，随后 [L110-L111](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L110-L111) 倒序，列表下标 0 对应最粗层 res5 的 `layer_4`——`forward_features` 第一次迭代取的正是它。

**练习 3**：如果把 `cfg.MODEL.SEM_SEG_HEAD.IN_FEATURES` 改成 `["res3","res4","res5"]`（少给 res2），`forward_features` 还能跑吗？输出会变成什么？

**答案**：能跑但语义改变（这是源码推演，待本地验证）：`from_config` 过滤后 `in_features = [res3,res4,res5]`，最细层变成 res3（1/8），于是 `mask_features` 分辨率变为 1/8 而非 1/4，破坏了与 `MaskFormerHead.common_stride = 4` 的默认约定——这解释了为什么换 backbone 时 `OUT_FEATURES` 必须与 `IN_FEATURES` 对齐（u8-l1 的主题）。

### 4.3 `TransformerEncoderPixelDecoder`：在 FPN 顶端加全局自注意力

#### 4.3.1 概念说明

`TransformerEncoderPixelDecoder` 直接继承 `BasePixelDecoder`（[fpn.py:L203-L205](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L203-L205)，注释 "a modified FPN decoder with extra Transformer encoder that processes the lowest-resolution feature map"）。改动只有一处：**把 res5 这条最粗尺度的入口从「单个 3×3 卷积」升级为「1×1 投影 + 6 层全局自注意力 encoder + 3×3 卷积」**。

动机：纯卷积 FPN 的感受野增长慢，res5 上每个位置只能「看到」有限邻域；而 DETR 式 Transformer encoder 一次注意力就让 1/32 图上的所有位置互相交换信息，得到更强的全局语境，这正是 MaskFormer(v1) 的默认像素解码器。它也顺手让三元组的第二项变成非 `None` 的 `transformer_encoder_features`，供 v1 的 `StandardTransformerDecoder` 通过 `TRANSFORMER_IN_FEATURE: "transformer_encoder"` 消费（u3-l2 讲过的四路分派）。

#### 4.3.2 核心流程

```text
res5(1/32, 2048ch) ──input_proj 1×1──► 256ch ──► TransformerEncoderOnly(6 层全局自注意力)
                                                        │ pos = PositionEmbeddingSine(128, normalize=True)
                                                        ▼
                                          transformer_encoder_features ──► 存为三元组第二项
                                                        │
                                                        ▼ layer_4 (3×3, 256→256)
     res4/res3/res2 分支与 BasePixelDecoder 完全一致（lateral + nearest 上采样 + output conv）
                                                        │
                                                        ▼
                              ( mask_features@1/4, transformer_encoder_features, [1/32,1/16,1/8] )
```

每个 encoder 层就是标准结构（post-norm 为例）：

\[
\mathrm{src} \leftarrow \mathrm{LN}\big(\mathrm{src} + \mathrm{MHA}(\mathrm{src}+pos,\ \mathrm{src}+pos,\ \mathrm{src})\big),\qquad
\mathrm{src} \leftarrow \mathrm{LN}\big(\mathrm{src} + \mathrm{FFN}(\mathrm{src})\big)
\]

位置编码把每个像素的归一化坐标 \((x/W\cdot 2\pi,\ y/H\cdot 2\pi)\) 编成 256 维正弦/余弦向量，与 token 嵌入**相加**后作为 attention 的 Q/K（`with_pos_embed`），V 仍用原始嵌入。

#### 4.3.3 源码精读

**(a) 构造：先搭好 FPN，再替换 res5 的入口**

[mask2former/modeling/pixel_decoder/fpn.py:L233](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L233) —— 先调用 `super().__init__(...)` 完整搭好基类的 FPN（adapter/layer/mask_features 全在），再做三处增量。

[mask2former/modeling/pixel_decoder/fpn.py:L240-L252](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L240-L252) —— 三件新东西：

- `input_proj`（1×1，`res5 通道(2048) → conv_dim(256)`）：Transformer 要求统一宽度；
- `transformer = TransformerEncoderOnly(...)`：d_model=conv_dim、nhead/dropout/dim_feedforward/normalize_before 全部继承自 `MODEL.MASK_FORMER` 的对应键（见 (b)），层数是独立键 `TRANSFORMER_ENC_LAYERS`；
- `pe_layer = PositionEmbeddingSine(N_steps=conv_dim//2, normalize=True)`：128 维给 x、128 维给 y，拼起来正好 256 = conv_dim。

[mask2former/modeling/pixel_decoder/fpn.py:L254-L270](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L254-L270) —— 微妙的一步：`delattr(self, "layer_4")` 后重建同名模块并更新 `self.output_convs[0]`。原因：基类的 `layer_4` 输入通道是 res5 的 2048，而现在 res5 先经 `input_proj` 变成 256，`layer_4` 必须是 256→256 的 3×3——**同名换实现**，既保住参数命名（`layer_4.*`）又适配新输入。

**(b) from_config：跨两个配置节点取值**

[mask2former/modeling/pixel_decoder/fpn.py:L272-L282](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L272-L282) —— 在基类四个键之外追加五个：`MASK_FORMER.DROPOUT / NHEADS / DIM_FEEDFORWARD / PRE_NORM`（注意注释特意标明 `TRANSFORMER_ENC_LAYERS  # a separate config`——它属于 `SEM_SEG_HEAD` 而非 `MASK_FORMER`，默认值 0 注册在 [config.py:L69](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L69)，不设为 6 就会建一个 0 层的 encoder）。

**(c) TransformerEncoderOnly 与 PositionEmbeddingSine**

[mask2former/modeling/pixel_decoder/fpn.py:L162-L189](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L162-L189) —— `TransformerEncoderOnly`：用 `_get_clones` 复制 `num_encoder_layers` 份 `TransformerEncoderLayer`，`_reset_parameters` 对所有二维以上参数做 xavier 初始化。它是 DETR `transformer.py` 的 encoder-only 裁剪版。

[mask2former/modeling/pixel_decoder/fpn.py:L191-L200](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L191-L200) —— 其 `forward`：`(B,C,H,W)` 展平转置成 `(HW, B, C)`（nn.MultiheadAttention 的序列在前的格式），过 encoder 后再变回 `(B,C,H,W)`。

[mask2former/modeling/transformer_decoder/transformer.py:L78-L102](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/transformer.py#L78-L102) —— `TransformerEncoder`：逐层调用 `TransformerEncoderLayer`，pre-norm 时结尾补一次 LayerNorm。单层结构（自注意力 + FFN + 残差 + norm，pre/post 两种执行序）在 [transformer.py:L154-L227](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/transformer.py#L154-L227)，u4-l2 会逐层细读，本讲知道它是标准 DETR 层即可。

[mask2former/modeling/transformer_decoder/position_encoding.py:L29-L52](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/position_encoding.py#L29-L52) —— `PositionEmbeddingSine.forward`：对全 0 的 mask 取反累加得到坐标，`normalize=True` 时除以宽/高再乘 `scale=2π`（[L35-L38](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/position_encoding.py#L35-L38)），随后按不同温度 \( \text{temperature}^{2i/d} \) 生成 sin/cos 并拼接 y、x 两半（[L40-L51](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/position_encoding.py#L40-L51)）。归一化让编码与输入分辨率无关——推理图变大小时位置语义不变。

**(d) forward_features：只有首层分支不同**

[mask2former/modeling/pixel_decoder/fpn.py:L284-L307](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L284-L307) —— 与基类逐行同构，唯一差别在 `lateral_conv is None` 的首层分支（[L292-L298](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L292-L298)）：`transformer = self.input_proj(x)` → `pos = self.pe_layer(x)` → `transformer = self.transformer(transformer, None, pos)` → `y = output_conv(transformer)`；并把 **encoder 输出（output_conv 之前）** 存进 `transformer_encoder_features`。返回行（[L307](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L307)）把三元组第二项从基类的 `None` 换成它。

**(e) 上游如何消费第二项**

[mask2former/modeling/meta_arch/mask_former_head.py:L120-L127](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/mask_former_head.py#L120-L127) —— `MaskFormerHead.layers` 的分派：v2 默认 `multi_scale_pixel_decoder` 走 `multi_scale_features`（此时 encoder 特征被**原样丢弃**，第二项形同虚设）；`transformer_encoder` 分支则断言 `transformer_encoder_features is not None`——若你配了 `BasePixelDecoder`（第二项恒 `None`）却选 `transformer_encoder`，前向立刻 `AssertionError`。这就是 u3-l2 说的「组合约束」在本讲的落点。

#### 4.3.4 代码实践：两种解码器同台对比

**实践目标**：同一份 `input_shape` 分别构建两种解码器，对比三元组（重点是第二项）与参数量。

**操作步骤**（示例代码，在 4.2.4 脚本基础上追加）：

```python
# 示例代码（紧接 practice_u3l4_base.py 的 input_shape / feats 定义；pd 为 Base 版实例）
cfg.MODEL.SEM_SEG_HEAD.PIXEL_DECODER_NAME = "TransformerEncoderPixelDecoder"
cfg.MODEL.SEM_SEG_HEAD.TRANSFORMER_ENC_LAYERS = 6
pd_t = build_pixel_decoder(cfg, input_shape)
print("children:", [n for n, _ in pd_t.named_children()])

mask_features_t, enc_feat_t, msf_t = pd_t.forward_features(feats)
print("second value:", None if enc_feat_t is None else tuple(enc_feat_t.shape))
print("mask/multi_scale 与 Base 版一致:",
      tuple(mask_features_t.shape), [tuple(f.shape) for f in msf_t])

for tag, m in [("Base", pd), ("TransformerEncoder", pd_t)]:
    print(tag, "params(M):", sum(p.numel() for p in m.parameters()) / 1e6)
```

**需要观察的现象**：`children` 中应出现 `input_proj`、`transformer`、`pe_layer`，且仍有 `layer_4`（被替换过的那个）；TransformerEncoder 版的第二项不再是 `None`。

**预期结果**（源码推得，待本地验证）：

- `enc_feat_t` 形状 `(1, 256, 32, 32)`——1/32 分辨率、conv_dim 通道，且等于 `layer_4` 卷积**之前**的 encoder 输出；
- 两版的 `mask_features` 与 `multi_scale` 形状完全一致（契约不变，只是内部实现换了）；
- 参数量：TransformerEncoder 版更多。可按公式核对差值（CONVS_DIM=256、DIM_FEEDFORWARD=2048、6 层）：

\[
\Delta \approx \underbrace{(1{\times}1{\times}2048{\times}256)}_{\text{input\_proj}} + 6\times\underbrace{(4d^2 + 2\times d\times2048)}_{d=256} + \underbrace{(3{\times}3{\times}256{\times}256)}_{\text{新 layer\_4}} - \underbrace{(3{\times}3{\times}2048{\times}256)}_{\text{旧 layer\_4}}
\]

代入即约 \(0.52\text{M} + 7.87\text{M} + 0.59\text{M} - 4.72\text{M} \approx +4.3\text{M}\)（估算值，以实际打印为准）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `input_proj` 是 1×1 而不是 3×3？

**答案**：它只负责把 res5 的 2048 通道压到 conv_dim=256、不改变分辨率——纯粹的通道对齐（projection）。空间信息随后交给全局自注意力（它本身就等价于一种可学习的全局「卷积」），再用 3×3 的 `layer_4` 做局部精修，分工明确且省参数（1×1 比 3×3 省 9 倍乘法）。

**练习 2**：`transformer_encoder_features` 存的是 output_conv 之前还是之后的张量？依据是哪一行？

**答案**：之前。[fpn.py:L295-L298](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L295-L298) 中 `transformer = self.transformer(...)` 之后、`y = output_conv(transformer)` 之前，就把 `transformer` 赋给了 `transformer_encoder_features`——下游 v1 解码器拿到的是「纯 encoder 输出」，与进入 FPN 融合链的 `y` 分道扬镳。

**练习 3**：若 `TRANSFORMER_ENC_LAYERS` 保持默认 0 会发生什么？

**答案**：`TransformerEncoder` 会用 0 份层构建（`_get_clones(layer, 0)` 得空 ModuleList），forward 循环不执行、仅在 pre-norm 时做一次 LayerNorm——模型仍能跑但「Transformer 增强」名存实亡。这就是真实配置里总要显式写 `TRANSFORMER_ENC_LAYERS: 6` 的原因（见 [maskformer2_R50_bs16_50ep.yaml:L18](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml#L18)）。（推演结论，待本地验证。）

## 5. 综合实践

**任务**：从真实配置出发（而非手造 `input_shape`），完整复现规格中的对比实验——把 `PIXEL_DECODER_NAME` 分别设为 `BasePixelDecoder` 与 `TransformerEncoderPixelDecoder`（`TRANSFORMER_ENC_LAYERS=6`），各构建一次**完整模型**，对比二者 `forward_features` 三元组输出与参数量差异。

**操作步骤**（示例代码，保存为 `Mask2Former-tutorial/practice_u3l4_full.py`，CPU 可跑）：

```python
# 示例代码
import torch
from detectron2.config import get_cfg
from detectron2.modeling import build_model
from detectron2.projects.deeplab import add_deeplab_config

import mask2former
from mask2former import add_maskformer2_config

CONFIG = "configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml"

rows = []
for name in ["BasePixelDecoder", "TransformerEncoderPixelDecoder"]:
    cfg = get_cfg()
    add_deeplab_config(cfg)
    add_maskformer2_config(cfg)
    cfg.merge_from_file(CONFIG)
    cfg.merge_from_list([
        "MODEL.SEM_SEG_HEAD.PIXEL_DECODER_NAME", name,
        "MODEL.SEM_SEG_HEAD.TRANSFORMER_ENC_LAYERS", 6,
        "MODEL.DEVICE", "cpu",
    ])
    model = build_model(cfg)          # 不设 MODEL.WEIGHTS，随机初始化，只为看结构
    pd = model.sem_seg_head.pixel_decoder

    feats = model.backbone(torch.rand(1, 3, 1024, 1024))  # LSJ 训练尺寸
    mf, enc, msf = pd.forward_features(feats)
    rows.append({
        "name": name,
        "mask_features": tuple(mf.shape),
        "second": None if enc is None else tuple(enc.shape),
        "multi_scale": [tuple(f.shape) for f in msf],
        "params(M)": round(sum(p.numel() for p in pd.parameters()) / 1e6, 2),
        "backbone_strides": {k: v.stride for k, v in model.backbone.output_shape().items()},
    })

for r in rows:
    print(r)
```

**需要观察的现象与预期结果**：

1. 两版 `mask_features` 均为 `(1, 256, 256, 256)`、`multi_scale` 均为 `[(1,256,32,32), (1,256,64,64), (1,256,128,128)]`——**契约与解码器实现无关**，这正是 `MaskFormerHead` 能随意换插件的原因；
2. 仅第二项不同：`None` vs `(1, 256, 32, 32)`；
3. 参数量差约 +4M（4.3.4 的估算式），TransformerEncoder 版更大；
4. `TRANSFORMER_IN_FEATURE` 沿用配置中的 `multi_scale_pixel_decoder`，因此两版模型都能正常构建且 v2 解码器照常消费 3 尺度特征——encoder 特征在这条配置路径下只是被返回、未被使用。

进一步实验（可选）：把 `MODEL.MASK_FORMER.TRANSFORMER_IN_FEATURE` 改成 `"transformer_encoder"` 再配 `BasePixelDecoder`，观察断言 `"Please use the TransformerEncoderPixelDecoder."` 触发（注意 v2 的 `MultiScaleMaskedTransformerDecoder` 与该分支并不配套，此实验只验证断言路径）。以上运行结果均待本地验证。

## 6. 本讲小结

- `build_pixel_decoder` = 查 `SEM_SEG_HEADS_REGISTRY`（与 head 共用同一注册表）+ 校验 `forward_features` 可调用；契约三要素（1/4 `mask_features`、可为 `None` 的第二项、长度 3 的 `multi_scale_features`）由 `ADVANCED_USAGE.md` 官方背书。
- `BasePixelDecoder` 是改造版 FPN：lateral 1×1 拉通道 → nearest 上采样逐层相加 → 3×3 output conv 精修；构造按 stride 升序、使用按自顶向下（列表倒序），`res2` 的 1/4 输出只进 `mask_features`，多尺度特征只收 3 个（1/32、1/16、1/8）。
- `TransformerEncoderPixelDecoder` 继承 Base 并只换掉 res5 入口：`input_proj`(1×1) → 6 层全局自注意力（DETR 式 encoder + 归一化正弦位置编码）→ 重建的 `layer_4`；Transformer 只放 1/32 是因为全局注意力 \(O(N^2)\) 的 token 数只有 1024。
- 两种解码器输出契约完全一致，可即插即换；区别只在三元组第二项（`None` vs encoder 特征），且仅当 `TRANSFORMER_IN_FEATURE="transformer_encoder"`（v1 用法）时它才被消费，否则断言或丢弃。
- 这两个解码器无需 CUDA 扩展即可运行；Mask2Former 默认的 `MSDeformAttnPixelDecoder` 用可变形注意力换取「多尺度 + 高分辨率」上更优的精度/开销平衡，是下一讲主角。

## 7. 下一步学习建议

- **u3-l5（MSDeformAttnPixelDecoder）**：看 Mask2Former 如何把「只在最粗尺度做全局注意力」升级为「res3/res4/res5 三尺度进 deformable encoder + extra FPN 层补高分辨率」，并继续遵守同一个三元组契约——对比本讲的 `TransformerEncoderPixelDecoder`，你会看到同一契约下的两种实现取舍。
- **u4-l2（解码器基础件与位置编码）**：本讲一笔带过的 `TransformerEncoderLayer`（pre/post-norm）与 `PositionEmbeddingSine` 会在 Transformer 解码器里反复出现，值得逐行精读。
- **u8-l2（自定义 Pixel Decoder 扩展实践）**：学完本讲的契约后，动手写一个自己的最小像素解码器并注册替换，检验「契约优先」的设计是否真的做到即插即用。
- 延伸阅读：`mask2former/modeling/transformer_decoder/maskformer_transformer_decoder.py` 中的 v1 `StandardTransformerDecoder`，看 `transformer_encoder` 分支的 encoder 特征在 v1 时代如何被用作解码器 memory。

# MaskFormerHead：三大组件的组装器

## 1. 本讲目标

上一篇（u3-l1）我们读完了 `MaskFormer.forward` 这条"一进两出"的总调度线，其中有一行 `outputs = self.sem_seg_head(features)` 把 backbone 特征整个交给了"头"。本讲就钻进这个头——`MaskFormerHead`，学完你应该能够：

1. 说清 `MaskFormerHead` 在「backbone → pixel decoder → Transformer decoder」三段式结构中扮演的**组装器**角色：它自己几乎不做计算，只负责把两个子模块接线起来。
2. 读懂 `from_config` 如何根据 `TRANSFORMER_IN_FEATURE` 的四种取值推导 Transformer 解码器的输入通道数，并把 `build_pixel_decoder` / `build_transformer_decoder` 的产物注入进来。
3. 解释 `layers` 中按 `transformer_in_feature` 四路分派的行为差异，以及 `forward_features` 三元组这一"契约"。
4. 知道 `_version = 2` 的旧权重自动迁移逻辑：为什么加载 MaskFormer(v1) 的 checkpoint 时 key 会被自动改写。

## 2. 前置知识

### 2.1 meta arch 与 sem_seg head 的分工

detectron2 把模型分成两层：

- **meta architecture**：注册在 `META_ARCH_REGISTRY` 的顶层模型（本项目的 `MaskFormer`），负责输入预处理（归一化、padding）、损失组织、推理后处理。
- **sem_seg head**：注册在 `SEM_SEG_HEADS_REGISTRY` 的预测头（本讲的 `MaskFormerHead`），负责从 backbone 特征到分割预测的核心网络。

`MaskFormer.from_config` 里的这两行就是两者的交接点（u3-l1 已读过）：

[mask2former/maskformer_model.py:L98-L99](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L98-L99) —— 用 detectron2 的 `build_backbone` 与 `build_sem_seg_head` 分别构建 backbone 和头，`backbone.output_shape()`（各特征的通道数和步长）作为 `input_shape` 传给头。

### 2.2 @configurable 与 from_config 惯例

detectron2 的 `@configurable` 装饰器约定：当类被以 `(cfg, ...)` 形式调用时，先执行类方法 `from_config(cfg, ...)` 得到一个 kwargs 字典，再用它调用 `__init__`。所以注册表里的 `SEM_SEG_HEADS_REGISTRY.get(name)(cfg, input_shape)` 实际走的是 `from_config → __init__` 两步。这个模式在 u1-l5（配置系统）和 u3-l1（`MaskFormer.from_config`）都见过，本讲是同一惯例在"头"这一层的重现。

### 2.3 ShapeSpec：特征的"名片"

`backbone.output_shape()` 返回 `Dict[str, ShapeSpec]`，例如 R50 的：

```text
{"res2": ShapeSpec(channels=256,  stride=4),
 "res3": ShapeSpec(channels=512,  stride=8),
 "res4": ShapeSpec(channels=1024, stride=16),
 "res5": ShapeSpec(channels=2048, stride=32)}
```

`stride`（步长）表示该特征图分辨率是输入图像的几分之一：`res2` 是 \( 1/4 \)，`res5` 是 \( 1/32 \)。这个"名片"决定了各模块的卷积通道设计和下游的分辨率约定。

### 2.4 三段式结构与分辨率约定

Mask2Former 的网络主体是三段（详见 u3-l1 第 4 节）：

```text
图像 → backbone(res2~res5) → pixel decoder → Transformer decoder → 预测
```

pixel decoder 对外承诺一个**三元组契约**（本讲 4.2 展开）：

- `mask_features`：分辨率 \( H/4 \times W/4 \)、通道 `MASK_DIM`（256）的逐像素嵌入，最终用来"印"出每个 query 的 mask；
- 第二项：仅部分解码器非 `None` 的 encoder 中间特征；
- `multi_scale_features`：长度为 3 的 list，分辨率依次为 \( 1/32 \)、\( 1/16 \)、\( 1/8 \)，供多尺度 Transformer 解码器做交叉注意力。

### 2.5 v1 → v2 的结构演变（权重迁移的背景）

Mask2Former 的代码是从 MaskFormer(v1) 仓库演进而来的：v1 中 pixel decoder 的卷积**直接挂在** `sem_seg_head` 下（参数名形如 `sem_seg_head.adapter_1.weight`）；v2 把它们收进子模块 `sem_seg_head.pixel_decoder`，同时把可学习 query 从 `static_query` 改名 `query_feat`。旧 checkpoint 的 key 与新结构对不上，于是有了本讲 4.3 的自动迁移钩子。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [mask2former/modeling/meta_arch/mask_former_head.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/mask_former_head.py) | 本讲主角：`MaskFormerHead` 的注册、构造、`layers` 分派与 `_load_from_state_dict` |
| [mask2former/modeling/pixel_decoder/fpn.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py) | `build_pixel_decoder` 构建函数与 `BasePixelDecoder` / `TransformerEncoderPixelDecoder`（细节在 u3-l4） |
| [mask2former/modeling/transformer_decoder/maskformer_transformer_decoder.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/maskformer_transformer_decoder.py) | `TRANSFORMER_DECODER_REGISTRY` 与 `build_transformer_decoder`（v1 解码器所在文件） |
| [mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py) | 默认解码器 `MultiScaleMaskedTransformerDecoder`（细节在 u4-l2/u4-l3），本讲只看它的接口断言与版本迁移 |
| [mask2former/config.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py) | `TRANSFORMER_IN_FEATURE`、`PIXEL_DECODER_NAME`、`TRANSFORMER_DECODER_NAME` 的默认值 |
| [configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml) | 实际配置对这些键的取值（实践脚本用它构建模型） |

## 4. 核心概念与源码讲解

### 4.1 组装器的构造：`__init__` 与 `from_config`

#### 4.1.1 概念说明

`MaskFormerHead` 是一个**组装器（assembler）**：它不含任何实质的计算层，只做两件事——

1. 持有两个子模块的引用：`self.pixel_decoder`（像素解码器）与 `self.predictor`（Transformer 解码器，注意属性名叫 predictor）；
2. 记住派发规则 `self.transformer_in_feature`，供 `layers` 决定把什么喂给 predictor。

"组装器"的价值在于**解耦**：meta arch 只认 `sem_seg_head` 这一个插槽，而像素解码器、Transformer 解码器都可以独立替换（u8-l2、u8-l3 的自定义扩展正是利用这一点），替换的黏合剂就在 `from_config` 里。

#### 4.1.2 核心流程

```text
build_sem_seg_head(cfg, backbone.output_shape())          # detectron2 宿主函数
  → SEM_SEG_HEADS_REGISTRY.get("MaskFormerHead")(cfg, input_shape)
      → @configurable 拦截 → MaskFormerHead.from_config(cfg, input_shape)
          ① 按 TRANSFORMER_IN_FEATURE 推导 transformer_predictor_in_channels
          ② build_pixel_decoder(cfg, input_shape)         → pixel_decoder
          ③ build_transformer_decoder(cfg, in_channels, mask_classification=True)
                                                          → predictor
      → 用返回的 kwargs 调用 __init__
          排序 in_features（按 stride 升序）、保存 common_stride=4 等标量
```

注意一个容易忽略的不对称：传给 `__init__` 的 `input_shape` 在 `from_config` 里被 `IN_FEATURES` 过滤过（只剩 `res2~res5`），而传给 `build_pixel_decoder` 的是**完整**的 `input_shape`——过滤的权力留给了 pixel decoder 自己的 `from_config`（见 [mask2former/modeling/pixel_decoder/fpn.py:L128-L134](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L128-L134)，它做同样的过滤）。

#### 4.1.3 源码精读

**注册**。装饰器一行把类登记进 `SEM_SEG_HEADS_REGISTRY`，`_version = 2` 是给权重迁移用的版本号（4.3 详述）：

[mask2former/modeling/meta_arch/mask_former_head.py:L18-L21](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/mask_former_head.py#L18-L21) —— `MaskFormerHead` 注册进 `SEM_SEG_HEADS_REGISTRY`，并声明类属性 `_version = 2`。

**构造函数**。`@configurable` 修饰的 `__init__` 只做"接收 + 记账"：

[mask2former/modeling/meta_arch/mask_former_head.py:L47-L85](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/mask_former_head.py#L47-L85) —— 参数包括 `input_shape`（特征名 → ShapeSpec）、`pixel_decoder`、`transformer_predictor`、`transformer_in_feature` 等；第 72 行把 `input_shape` 按 stride 升序排序，随后拆出 `self.in_features`（`["res2","res3","res4","res5"]`）与各特征的 stride / channels 列表；第 78 行写死 `self.common_stride = 4`（mask_features 的目标分辨率 \( 1/4 \)）；第 81-83 行把两个子模块和派发键存为属性。

**from_config 的四分支**。这段是本模块的"大脑"：

[mask2former/modeling/meta_arch/mask_former_head.py:L87-L113](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/mask_former_head.py#L87-L113) —— 第 90-97 行按 `cfg.MODEL.MASK_FORMER.TRANSFORMER_IN_FEATURE` 推导 predictor 的输入通道数（详见 4.2 的表格）；第 105 行调用 `build_pixel_decoder(cfg, input_shape)`（传完整字典）；第 108-112 行调用 `build_transformer_decoder(cfg, transformer_predictor_in_channels, mask_classification=True)`。`mask_classification=True` 表示 predictor 需要带分类头 `class_embed`；对照 u8-l4 将讲的 per-pixel 基线：[mask2former/modeling/meta_arch/per_pixel_baseline.py:L200-L204](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/per_pixel_baseline.py#L200-L204) 以 `mask_classification=False` 构建 `StandardTransformerDecoder`，即不需要"query → 类别"的分类头。

两个构建函数本身都极薄：

[mask2former/modeling/pixel_decoder/fpn.py:L21-L33](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L21-L33) —— `build_pixel_decoder` 从 `cfg.MODEL.SEM_SEG_HEAD.PIXEL_DECODER_NAME` 查 `SEM_SEG_HEADS_REGISTRY`，并**校验**产物必须有可调用的 `forward_features` 方法，否则抛 `ValueError`。这就是"pixel decoder 契约"的强制执行点。

[mask2former/modeling/transformer_decoder/maskformer_transformer_decoder.py:L16-L27](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/maskformer_transformer_decoder.py#L16-L27) —— `build_transformer_decoder` 查的是**项目自建**的 `TRANSFORMER_DECODER_REGISTRY`（不是 detectron2 的注册表），按 `cfg.MODEL.MASK_FORMER.TRANSFORMER_DECODER_NAME` 取类并以 `(cfg, in_channels, mask_classification)` 三参调用。这个签名约定是 u8-l3 自定义解码器必须遵守的。

配置默认值与实际取值的对照（承接 u1-l5）：

[mask2former/config.py:L50](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L50) —— `TRANSFORMER_IN_FEATURE` 默认 `"res5"`（v1 遗留习惯）。

[mask2former/config.py:L71](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L71) / [mask2former/config.py:L94](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L94) —— `PIXEL_DECODER_NAME` 默认 `BasePixelDecoder`；`TRANSFORMER_DECODER_NAME` 默认 `MultiScaleMaskedTransformerDecoder`（maskformer2 新增键）。

[configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml:L13-L21](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml#L13-L21) —— 实际配置覆盖为 `MSDeformAttnPixelDecoder` + `MultiScaleMaskedTransformerDecoder` + `multi_scale_pixel_decoder`。仓库里全部 12 个 maskformer2 yaml 都用同一组合（`configs/` 下 grep 可验证）。

#### 4.1.4 代码实践

**实践目标**：验证"组装器"确实只是接线——打印 head 的关键属性与两个子模块的类型，确认它们来自配置的哪个键。

操作步骤（示例代码，保存为独立脚本运行，需已按 u1-l2 装好环境）：

```python
# 示例代码：inspect_head.py
import mask2former  # noqa: F401 —— import 即注册（u1-l3）

from detectron2.config import get_cfg
from detectron2.projects.deeplab import add_deeplab_config
from mask2former import add_maskformer2_config
from detectron2.modeling import build_model

cfg = get_cfg()
add_deeplab_config(cfg)
add_maskformer2_config(cfg)
cfg.merge_from_file("configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml")
cfg.MODEL.DEVICE = "cpu"   # 无 GPU 时兜底
# 若未编译 MSDeformAttn CUDA 扩展，取消下一行注释（形状契约完全一致，见 u1-l2）：
# cfg.MODEL.SEM_SEG_HEAD.PIXEL_DECODER_NAME = "BasePixelDecoder"
cfg.freeze()

model = build_model(cfg)
head = model.sem_seg_head

print(type(head).__name__)            # MaskFormerHead
print(head.in_features)               # ['res2', 'res3', 'res4', 'res5']
print(head.transformer_in_feature)    # multi_scale_pixel_decoder
print(head.common_stride)             # 4
print(type(head.pixel_decoder).__name__)   # MSDeformAttnPixelDecoder（或 BasePixelDecoder）
print(type(head.predictor).__name__)       # MultiScaleMaskedTransformerDecoder
```

需要观察的现象与预期结果（依据源码推得，待本地验证）：

- `head.in_features` 恰好是按 stride 升序的 `["res2","res3","res4","res5"]`，因为 `from_config` 用 `IN_FEATURES` 过滤、`__init__` 又做了排序；
- 两个子模块的类型分别由 `MODEL.SEM_SEG_HEAD.PIXEL_DECODER_NAME` 与 `MODEL.MASK_FORMER.TRANSFORMER_DECODER_NAME` 决定——改这两个键（如换成 `BasePixelDecoder`）后重新构建，打印随之改变，这就是 u8 扩展实践的入口。

#### 4.1.5 小练习与答案

**练习 1**：把 `cfg.MODEL.SEM_SEG_HEAD.IN_FEATURES` 改成 `["res3","res4","res5"]`（去掉 res2），构建后 `head.in_features` 是什么？`mask_features` 的分辨率会变成多少？

**答案**：`in_features` 为 `["res3","res4","res5"]`。分辨率跟着"最高分辨率的输入特征"走：`BasePixelDecoder` 的自顶向下循环最终停在 res3 级（见 [fpn.py:L140-L154](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L140-L154) 的最后一轮），`mask_features` 变为 \( 1/8 \)；`MSDeformAttnPixelDecoder` 下 extra FPN 层数按 \( \log_2 8 - \log_2 4 = 1 \) 只补一层（见 [msdeformattn.py:L257-L259](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/msdeformattn.py#L257-L259)），`mask_features` 同样落在 \( 1/8 \)。这解释了为什么 `common_stride` 属性写死 4 只是"意图声明"，实际分辨率由输入特征决定。

**练习 2**：`TRANSFORMER_IN_FEATURE="transformer_encoder"` 但 `PIXEL_DECODER_NAME="BasePixelDecoder"`，构建时会报错吗？什么时候报？

**答案**：构建时不报错——`from_config` 只是无条件取 `CONVS_DIM` 当通道数（[mask_former_head.py:L90-L91](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/mask_former_head.py#L90-L91)）；错误延迟到前向：`layers` 里的 `assert transformer_encoder_features is not None` 失败（`BasePixelDecoder.forward_features` 第二项恒为 `None`，见 [fpn.py:L154](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L154)），提示 `"Please use the TransformerEncoderPixelDecoder."`。这是"契约靠运行期断言兜底"的典型例子。

### 4.2 `layers` 主链路：`forward_features` 三元组与四路分派

#### 4.2.1 概念说明

`layers` 只有十几行，但它固化了整个项目的两条**接口契约**：

- **pixel decoder 契约**：必须实现 `forward_features(features)`，返回三元组 `(mask_features, transformer_encoder_features, multi_scale_features)`。`build_pixel_decoder` 在构建期用 `callable` 检查强制这一点（4.1.3 已读）。
- **predictor 契约**：调用形如 `predictor(x, mask_features, mask)`，第二个参数永远是 `mask_features`，差异全在第一参数 `x`——由 `transformer_in_feature` 决定。

`transformer_in_feature` 的四种取值对应四种"喂法"，本质上是这个代码库三代架构（v1 单尺度 / v1 多尺度 / v2 多尺度）在同一分派函数里的共存。

#### 4.2.2 核心流程

```text
layers(features, mask=None):
    (mask_features, enc_feat, msf) = pixel_decoder.forward_features(features)
    # mask_features: (B, MASK_DIM, H/4, W/4)   永远传给 predictor 作第二参数
    # enc_feat:      仅 TransformerEncoderPixelDecoder 非 None（res5 级）
    # msf:           [1/32, 1/16, 1/8] 三个尺度的 list

    分派 transformer_in_feature:
      "multi_scale_pixel_decoder" → predictor(msf,     mask_features, mask)   # v2 默认
      "transformer_encoder"       → 断言 enc_feat 非 None
                                    predictor(enc_feat, mask_features, mask)  # v1 默认
      "pixel_embedding"           → predictor(mask_features, mask_features, mask)
      其他（如 "res5"，config.py 的默认值）
                                  → predictor(features[name], mask_features, mask)
    return predictions  # dict: pred_logits / pred_masks / aux_outputs
```

四种取值的完整对照：

| `TRANSFORMER_IN_FEATURE` 取值 | 送入 predictor 的第一参数 | `in_channels` 推导（from_config） | 配套 pixel decoder | 配套 predictor | 语义 |
| --- | --- | --- | --- | --- | --- |
| `"multi_scale_pixel_decoder"` | `multi_scale_features`（3 尺度 list） | `CONVS_DIM`（256） | 任意（默认 `MSDeformAttnPixelDecoder`） | `MultiScaleMaskedTransformerDecoder` | **v2 / Mask2Former 默认**，仓库 12 个 yaml 全用它 |
| `"transformer_encoder"` | `transformer_encoder_features` | `CONVS_DIM`（256） | 必须是 `TransformerEncoderPixelDecoder`（否则 assert） | `StandardTransformerDecoder`（v1） | v1：FPN 顶上再叠 6 层 encoder，用其最低分辨率输出 |
| `"pixel_embedding"` | `mask_features` 本身 | `MASK_DIM`（256） | 任意 | `StandardTransformerDecoder`（v1） | v1 可选：直接把 \( 1/4 \) 像素嵌入当 decoder 输入 |
| 其他，如 `"res5"`（config.py 默认） | `features[name]`，原始 backbone 特征 | `input_shape[name].channels`（R50 的 res5 = 2048） | 任意 | `StandardTransformerDecoder`（v1） | 最原始形态：单尺度 backbone 特征直接进 decoder |

注意配套关系不是随意的：`MultiScaleMaskedTransformerDecoder.forward` 第一行就 `assert len(x) == self.num_feature_levels`（[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:L363-L365](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L363-L365)），要求 `x` 是长度 3 的 list；其余三种取值给的是单个张量，只能配 v1 的 `StandardTransformerDecoder`。改配置时 `TRANSFORMER_IN_FEATURE` 与 `TRANSFORMER_DECODER_NAME` 必须一起换。

#### 4.2.3 源码精读

[mask2former/modeling/meta_arch/mask_former_head.py:L115-L132](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/mask_former_head.py#L115-L132) —— `forward` 直接转发到 `layers`（保留 `layers` 这个名字是 detectron2 语义分割头的传统，便于子类复用 `forward` 骨架）；`layers` 第 119 行调用 `pixel_decoder.forward_features(features)` 拿到三元组；第 120-131 行按上面表格四路分派；第 120 行的 `multi_scale_pixel_decoder` 分支**最先**被判断，所以它是唯一与"第二项是否为 None"无关的分支。

三元组两个代表实现（细节分别在 u3-l4、u3-l5 展开，这里只看返回值）：

[mask2former/modeling/pixel_decoder/fpn.py:L136-L154](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L136-L154) —— `BasePixelDecoder.forward_features`：自顶向下逐级融合（lateral 1×1 卷积 + nearest 上采样 + 3×3 output 卷积），收集前 3 个尺度进 `multi_scale_features`，返回 `(mask_features(y), None, multi_scale_features)`——**第二项恒为 None**，`y` 停在 res2 级，故 `mask_features` 为 \( 1/4 \) 分辨率。

[mask2former/modeling/pixel_decoder/fpn.py:L284-L307](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L284-L307) —— `TransformerEncoderPixelDecoder.forward_features`：唯一区别是最底层（res5）先经 `input_proj` + 6 层标准 Transformer encoder，把结果同时作为 `transformer_encoder_features` 返回（第 297-298 行），这就是 `"transformer_encoder"` 分支要消费的东西。

对照默认解码器的返回：

[mask2former/modeling/pixel_decoder/msdeformattn.py:L314-L358](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/msdeformattn.py#L314-L358) —— `MSDeformAttnPixelDecoder.forward_features`：res3-res5 经多尺度可变形注意力 encoder，再用 extra FPN 层补足高分辨率，最后返回 `(self.mask_features(out[-1]), out[0], multi_scale_features)`。注意它第二项返回 `out[0]`（\( 1/32 \) 的 encoder 输出）而非 None——但默认配置走 `multi_scale_pixel_decoder` 分支，这一项**根本不会被消费**（u3-l5 详述）。

`mask` 参数在图像模型里实际恒为 `None`：u3-l1 读过的 `MaskFormer.forward` 只调用 `self.sem_seg_head(features)`，不传 mask；该参数是为视频版（帧 padding mask）预留的接口。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：给 `layers` 做一个"带打印的副本"，跑一次前向，记录 `mask_features`、`transformer_encoder_features`、`multi_scale_features` 的形状，验证分辨率契约：`mask_features` 为 \( 1/4 \)，多尺度特征依次为 \( 1/32 \)、\( 1/16 \)、\( 1/8 \)。

操作步骤（示例代码，不改任何源码，用运行期实例属性覆盖 `layers`——实例属性查找优先于类方法）：

```python
# 示例代码：inspect_head_shapes.py
import torch
import mask2former  # noqa: F401

from detectron2.config import get_cfg
from detectron2.projects.deeplab import add_deeplab_config
from mask2former import add_maskformer2_config
from detectron2.modeling import build_model

# 1) 组装配置（u1-l5 的五层叠加）
cfg = get_cfg()
add_deeplab_config(cfg)
add_maskformer2_config(cfg)
cfg.merge_from_file("configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml")
cfg.MODEL.DEVICE = "cpu"
# 未编译 MSDeformAttn 扩展时（构建该解码器需要它，见 u1-l2），换成：
# cfg.MODEL.SEM_SEG_HEAD.PIXEL_DECODER_NAME = "BasePixelDecoder"
cfg.freeze()

# 2) 构建模型，取 head
model = build_model(cfg)
head = model.sem_seg_head

# 3) layers 的副本：拦截三元组并打印（等价于把 layers 复制出来插打印）
orig_layers = head.layers                       # 先留住原方法（已绑定实例）
def debug_layers(features, mask=None):
    mask_features, enc_feat, msf = head.pixel_decoder.forward_features(features)
    print("mask_features:                ", tuple(mask_features.shape))
    print("transformer_encoder_features: ",
          None if enc_feat is None else tuple(enc_feat.shape))
    for i, f in enumerate(msf):
        print(f"multi_scale_features[{i}]:      ", tuple(f.shape))
    return orig_layers(features, mask)          # 再走原流程，保证输出不受影响
head.layers = debug_layers                      # 实例属性遮蔽类方法

# 4) 前向一次（1024 与 SIZE_DIVISIBILITY=32 对齐；随机数即可，只看形状）
model.eval()
with torch.no_grad():
    features = model.backbone(torch.randn(1, 3, 1024, 1024))
    out = head(features)
print("pred_logits:", tuple(out["pred_logits"].shape))
print("pred_masks: ", tuple(out["pred_masks"].shape))
```

需要观察的现象与预期结果（形状依据源码推得，待本地验证；`CONVS_DIM = MASK_DIM = 256`、`NUM_CLASSES = 133`、query 数 100）：

```text
mask_features:                 (1, 256, 256, 256)   # 1024/4，验证 1/4 契约
transformer_encoder_features:  (1, 256, 32, 32)     # MSDeformAttn 时为 out[0]；BasePixelDecoder 时为 None
multi_scale_features[0]:       (1, 256, 32, 32)     # 1024/32
multi_scale_features[1]:       (1, 256, 64, 64)     # 1024/16
multi_scale_features[2]:       (1, 256, 128, 128)   # 1024/8
pred_logits: (1, 100, 134)                          # 133 类 + 1 个 no-object 通道（u3-l1）
pred_masks:  (1, 100, 256, 256)                     # 100 个 query × 1/4 分辨率 mask
```

可再加一步自动校验（示例代码）：

```python
H = W = 1024
assert mask_features.shape[-2:] == (H // 4, W // 4)
assert [f.shape[-1] for f in msf] == [H // 32, H // 16, H // 8]
```

若把输入换成 `torch.randn(2, 3, 1024, 768)` 之外的任意尺寸，注意先按 `SIZE_DIVISIBILITY=32` pad（图像模型里这由 meta arch 的 `ImageList.from_tensors` 完成，u3-l1），否则 backbone（Swin）可能报错；`torch.randn(2, 3, 1024, 1024)` 这类对齐尺寸可直接用，所有形状的第一维都会变成 2。

#### 4.2.5 小练习与答案

**练习 1**：默认配置下 `MSDeformAttnPixelDecoder` 返回的第二项（`out[0]`，\( 1/32 \)）被消费了吗？为什么它还是契约的一部分？

**答案**：没被消费——默认 `TRANSFORMER_IN_FEATURE="multi_scale_pixel_decoder"` 走第一个分支，只用到三元组的第 1、3 项。契约保持三元组是为了让 `MaskFormerHead.layers` 对所有 pixel decoder 一视同仁：第二项在 `"transformer_encoder"` 分支（配 `TransformerEncoderPixelDecoder`，v1 用法）是必需输入，`BasePixelDecoder` 用 `None` 占位表达"我不提供"。

**练习 2**：四种取值里哪些能和 `MultiScaleMaskedTransformerDecoder` 搭配？为什么？

**答案**：只有 `multi_scale_pixel_decoder`。该解码器的 `forward` 开头断言 `len(x) == self.num_feature_levels`（[mask2former_transformer_decoder.py:L363-L365](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L363-L365)），随后按下标 `x[i]` 逐尺度取特征，要求 `x` 是 3 个张量的 list；其余三种取值送入的是单个张量，`len()` 会沿 batch 维数出错或直接断言失败。它们面向 v1 的 `StandardTransformerDecoder`（其 `forward(x, mask_features, mask)` 接受单张量，见 [maskformer_transformer_decoder.py:L130-L136](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/maskformer_transformer_decoder.py#L130-L136)）。

**练习 3**：不写代码回答：输入 `2×3×1024×1024` 时 `mask_features` 与 `multi_scale_features[0]` 的形状各是多少？

**答案**：`mask_features` 为 `(2, 256, 256, 256)`；`multi_scale_features[0]` 为 `(2, 256, 32, 32)`。批量维原样传递，空间维按 \( 1/4 \) 与 \( 1/32 \) 缩小。

### 4.3 `_version = 2`：旧权重自动迁移

#### 4.3.1 概念说明

PyTorch 的 `nn.Module` 提供了一个加载钩子 `_load_from_state_dict`：`load_state_dict` 会**逐模块**调用它，把该模块前缀（如 `"sem_seg_head."`）下的 key 与版本元数据交给模块自己处理。Mask2Former 利用这个钩子做**前向兼容**：

- 旧（v1）权重里 pixel decoder 的参数直接叫 `sem_seg_head.adapter_1.*`、`sem_seg_head.layer_1.*`……
- 新（v2）结构里它们必须叫 `sem_seg_head.pixel_decoder.adapter_1.*`……

加载时钩子检测到 checkpoint 的版本号缺失或小于 2，就把旧 key 现场改写成新 key，于是旧权重无需重新训练就能加载进新结构。与之配合，`MultiScaleMaskedTransformerDecoder` 的同名钩子把 `static_query` 改名为 `query_feat`。

#### 4.3.2 核心流程

```text
加载 v1 checkpoint
→ PyTorch 逐模块调用 _load_from_state_dict(prefix="sem_seg_head.",
                                        local_metadata={"version": None})   # v1 保存时无 _version
→ version is None or version < 2：
     遍历 state_dict 的所有 key：
       若 key 含 "sem_seg_head" 且不以 "sem_seg_head.predictor" 开头：
           "sem_seg_head.X" → "sem_seg_head.pixel_decoder.X"     # 挪进子模块
→ 若至少改写过一个 key（scratch 变 False）：打印 warning
→ 之后才真正执行参数拷贝（拷贝时新旧 key 已经对齐）
```

两个设计细节：

1. **排除 predictor 前缀**：v1 的 `sem_seg_head.predictor.*` 在 v2 里路径没变，不能被一起挪进 `pixel_decoder`，否则它们会变成 unexpected keys 而整个丢失。
2. **scratch 标志**：从零训练（不加载 checkpoint）时 state_dict 里没有可改写的 key，`scratch` 保持 True，**不打印** warning，避免噪音——"Do not warn if train from scratch"。

#### 4.3.3 源码精读

[mask2former/modeling/meta_arch/mask_former_head.py:L21-L45](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/mask_former_head.py#L21-L45) —— 完整钩子：第 26-27 行读版本号判断是否迁移；第 31-39 行是改写循环，`k.replace(prefix, prefix + "pixel_decoder.")` 把 `sem_seg_head.` 整体替换为 `sem_seg_head.pixel_decoder.`，改写后删旧 key、写新 key；第 41-45 行在确有改写时打印 "Weight format of MaskFormerHead have changed! ... Applying automatic conversion now ..."。

姊妹钩子（对照阅读，完整分析留给 u10-l4）：

[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:L210-L233](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L210-L233) —— `MultiScaleMaskedTransformerDecoder` 同样声明 `_version = 2`，加载时把含 `static_query` 的 key 改名为 `query_feat`（第 222-223 行）。v1 里可学习 query 叫 static query，v2 改叫 learnable query features 并参与监督（u4-l3 详述），参数名随之改变。

版本号的来源：PyTorch 保存 `state_dict` 时会把每个模块的 `_version` 写进随行的元数据（`state_dict._metadata`）；detectron2 的 checkpointer 加载时把这份元数据作为 `local_metadata` 传入。v1 的 `MaskFormerHead` 还没有 `_version` 属性，因此旧 checkpoint 的元数据里版本为 None，正好命中第 27 行的迁移条件。

#### 4.3.4 代码实践

**实践目标**：不用下载任何旧权重，用一个假 state_dict 直接驱动钩子，亲眼看到 key 的自动改写与 warning。

操作步骤（示例代码，可直接接在 4.2.4 的脚本之后，`head` 已就绪）：

```python
# 示例代码：接在 inspect_head_shapes.py 之后
import logging
logging.basicConfig(level=logging.WARNING)   # 让 logger.warning 可见

sd = {
    "sem_seg_head.adapter_1.weight": torch.zeros(1),          # v1 风格 pixel decoder 卷积
    "sem_seg_head.layer_1.weight": torch.zeros(1),            # v1 风格 output 卷积
    "sem_seg_head.predictor.query_embed.weight": torch.zeros(1),  # predictor，不应被改写
}
# 手动模拟 load_state_dict 对 head 这一模块的调用（8 个参数一一对应）
head._load_from_state_dict(sd, "sem_seg_head.", {"version": None}, False, [], [], [])
print(sorted(sd.keys()))
```

需要观察的现象与预期结果（依据源码推得，待本地验证）：

- 终端出现 warning：`Weight format of MaskFormerHead have changed! Please upgrade your models. Applying automatic conversion now ...`；
- 打印的 key 变为：

```text
['sem_seg_head.layer_1.weight',
 'sem_seg_head.pixel_decoder.adapter_1.weight',   # ← 被挪进 pixel_decoder 子模块
 'sem_seg_head.pixel_decoder.layer_1.weight',     # ← 被挪进 pixel_decoder 子模块
 'sem_seg_head.predictor.query_embed.weight']     # ← 原样保留
```

再做一个对照实验（示例代码）：把 `{"version": None}` 换成 `{"version": 2}` 重新构造 `sd` 调用一次——预期**没有 warning、没有任何 key 改写**，因为版本号已达标；这验证了钩子只对旧权重生效。

#### 4.3.5 小练习与答案

**练习 1**：为什么改写条件要排除 `prefix + "predictor"` 开头的 key？

**答案**：v1 → v2 的结构变化只发生在 pixel decoder 一侧（卷积被收进 `pixel_decoder` 子模块）；predictor 的参数路径在两个版本里都是 `sem_seg_head.predictor.*`，本来就对得上。若不排除，predictor 的权重会被错误挪到 `sem_seg_head.pixel_decoder.predictor.*`，与模型真实参数名不匹配，变成 unexpected keys 而被丢弃。

**练习 2**：用 `--eval-only` 加载一个 **v2 自己训练**的 checkpoint 时，会看到这条 warning 吗？

**答案**：不会。v2 checkpoint 的元数据里 `MaskFormerHead` 版本号为 2，第 27 行的 `version is None or version < 2` 不成立，直接跳过整个改写块（也就没有 warning）。warning 只在加载 v1（或无版本号）权重时出现。

**练习 3**：`MultiScaleMaskedTransformerDecoder` 的对应钩子改写什么 key？为什么需要它？

**答案**：把含 `static_query` 的 key 改成 `query_feat`（[mask2former_transformer_decoder.py:L222-L223](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L222-L223)）。因为 v2 把可学习 query 从"静态"语义升级为参与损失监督的 learnable query features（`nn.Embedding` 的属性名从 `static_query` 改成 `query_feat`），旧名字的参数需要映射到新属性才能加载。

## 5. 综合实践

把本讲三个模块串成一个「head 体检脚本」`head_inspector.py`，对**任意一份 yaml** 输出一份结构报告：

1. **构建**：按 4.2.4 的步骤从 `configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml` 构建 `model`（CPU 即可）。
2. **报告组装关系**：打印 `head.in_features`、`head.transformer_in_feature`、`type(head.pixel_decoder).__name__`、`type(head.predictor).__name__`，并对照 `cfg` 打印 `MODEL.SEM_SEG_HEAD.PIXEL_DECODER_NAME` 与 `MODEL.MASK_FORMER.TRANSFORMER_DECODER_NAME`，确认属性与配置一一对应。
3. **报告分辨率契约**：用 4.2.4 的 `debug_layers` 跑一次 \( 1024 \times 1024 \) 前向，打印三元组形状，并用断言验证 \( 1/4 \)、\( 1/32 \)、\( 1/16 \)、\( 1/8 \)。
4. **报告迁移行为**：用 4.3.4 的假 state_dict 分别以 `{"version": None}` 和 `{"version": 2}` 各调用一次 `_load_from_state_dict`，把两次的 key 集合差异打印成对照表。
5. **换配置再跑**：把 `PIXEL_DECODER_NAME` 换成 `BasePixelDecoder` 重复第 3 步，观察 `transformer_encoder_features` 从 `(1,256,32,32)` 变为 `None`、其余形状不变——这正说明三元组契约的稳定性，也是 u8-l2 自定义 pixel decoder 时必须遵守的规范。

预期成果：一份能对任意 Mask2Former 配置快速回答"用了什么解码器、喂了什么特征、旧权重能不能加载"的脚本；后续学习 u3-l3（backbone）、u3-l4/u3-l5（两种 pixel decoder）、u4-x（解码器内部）时可以反复复用它做对照实验。

## 6. 本讲小结

- `MaskFormerHead` 是三段式结构的**组装器**：自己不做计算，只持有 `pixel_decoder` 与 `predictor` 两个子模块并记录派发键 `transformer_in_feature`；`from_config` 负责推导通道数、调 `build_pixel_decoder` / `build_transformer_decoder` 注入两者。
- pixel decoder 的契约是 `forward_features(features) → (mask_features, encoder_feature, multi_scale_features)`：`mask_features` 为 \( 1/4 \) 分辨率，`multi_scale_features` 依次为 \( 1/32 \)、\( 1/16 \)、\( 1/8 \)；`build_pixel_decoder` 在构建期校验 `forward_features` 存在。
- `TRANSFORMER_IN_FEATURE` 四种取值 = 四种喂法：`multi_scale_pixel_decoder`（v2 默认，3 尺度 list）、`transformer_encoder`（v1，需 `TransformerEncoderPixelDecoder`，否则前向 assert 失败）、`pixel_embedding`（直接用 mask_features）、特征名如 `"res5"`（原始 backbone 特征，也是 config.py 的默认值）；取值必须与 `TRANSFORMER_DECODER_NAME` 配套。
- `_version = 2` 的 `_load_from_state_dict` 钩子把 v1 的 `sem_seg_head.X` 自动改写为 `sem_seg_head.pixel_decoder.X`（predictor 前缀除外），配合解码器的 `static_query → query_feat` 改名，实现旧权重免重训加载；全新训练不会触发 warning。
- 本讲的两条契约（pixel decoder 三元组、predictor 的 `(x, mask_features, mask)` 签名）正是单元八三大扩展点（换 backbone、换 pixel decoder、换 transformer decoder）要遵守的接口规范。

## 7. 下一步学习建议

`MaskFormerHead` 把三段式结构接好了线，接下来沿数据流向上游与下游各走一讲：

- **u3-l3（Swin Backbone）**：看 `input_shape` 这张"名片"是怎么来的——`backbone.output_shape()` 与 Swin 的四个 stage、`res2~res5` 命名；
- **u3-l4 / u3-l5（两种 pixel decoder）**：分别精读 `BasePixelDecoder`（含 `TransformerEncoderPixelDecoder` 变体）与默认的 `MSDeformAttnPixelDecoder`，弄清 `forward_features` 三元组是如何一步步算出来的（extra FPN 层数公式 \( \log_2(\text{min\_stride}) - \log_2(\text{common\_stride}) \)）；
- 之后进入单元四（u4-l2 / u4-l3）：拆开 `self.predictor`，看 `MultiScaleMaskedTransformerDecoder` 如何消费这 3 个尺度和 `mask_features`。

建议把第 5 节的 `head_inspector.py` 保留下来，作为后续每一讲的"对照实验"工具。

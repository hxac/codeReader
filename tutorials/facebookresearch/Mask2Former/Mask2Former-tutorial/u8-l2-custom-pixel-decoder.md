# 扩展实践二：自定义 Pixel Decoder

## 1. 本讲目标

本讲是「扩展实践」系列的第二讲（第一讲 u8-l1 替换了 Backbone）。学完后你应该能够：

1. 说出 `build_pixel_decoder` 如何从配置查表构建像素解码器，以及它做了（和没做）哪些接口校验。
2. 准确复述 `forward_features` 的三元组返回契约：`(mask_features, 第二项, multi_scale_features)`，其中 `mask_features` 是 1/4 分辨率逐像素嵌入、`multi_scale_features` 是长度为 3 的多尺度特征列表。
3. 理解 `common_stride=4` 与 `1/32、1/16、1/8` 两个尺度约定的来源——哪些是硬约束（有断言兜底）、哪些是软约定（仓库习惯）。
4. 亲手实现并注册一个 `MiniPixelDecoder`，通过改一行 `PIXEL_DECODER_NAME` 配置接入完整 MaskFormer 模型并跑通前向。

## 2. 前置知识

本讲默认你已完成 u3-l4（BasePixelDecoder 与 TransformerEncoderPixelDecoder）和 u3-l5（MSDeformAttnPixelDecoder），这里只做简要回顾，不重复展开。

- **三段式架构**：Mask2Former 模型 = backbone（提特征金字塔）+ pixel decoder（重组金字塔）+ Transformer decoder（query 在特征上做 masked attention 出预测）。u3-l2 讲过 `MaskFormerHead` 是组装器，像素解码器是它的一个子模块。
- **`ShapeSpec` 与 stride**：detectron2 用 `ShapeSpec(channels=..., stride=...)` 描述每个输入特征的通道数和相对原图的缩小倍数。R50 的四路特征 `res2/res3/res4/res5` 分别是 1/4、1/8、1/16、1/32 分辨率，通道数 256/512/1024/2048。
- **`SEM_SEG_HEADS_REGISTRY`**：detectron2 的 head 注册表。像素解码器与 `MaskFormerHead` 共用这一个注册表（注意：Transformer 解码器用的是项目自建注册表，见 u8-l3）。「import 即注册」：装饰器在模块被 import 时执行，`mask2former/modeling/__init__.py` 已经 import 了官方的两个像素解码器文件。
- **`@configurable` + `from_config` 模式**：detectron2 的构造模式——外部用 `Class(cfg, input_shape)` 构建时，实际走 `from_config(cfg, input_shape)` 把配置翻译成关键字参数，再调真正的 `__init__`。自定义模块沿用这个模式即可被 `build_pixel_decoder` 以统一签名构建。
- **环境前提**：`import mask2former` 会连带 import `msdeformattn.py`，而它顶层 import 已编译的 MSDeformAttn 扩展（u1-l2）。本讲所有实践都要求 u1-l2 的环境安装（含算子编译）已完成。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `ADVANCED_USAGE.md` | 官方扩展指南，明确写了像素解码器的替换契约 |
| `mask2former/modeling/pixel_decoder/fpn.py` | `build_pixel_decoder` 工厂函数 + `BasePixelDecoder` / `TransformerEncoderPixelDecoder` 两个实现 |
| `mask2former/modeling/pixel_decoder/msdeformattn.py` | 默认像素解码器 `MSDeformAttnPixelDecoder`，契约的第三个参照实现 |
| `mask2former/modeling/meta_arch/mask_former_head.py` | 消费方：`MaskFormerHead.layers` 解包三元组并分发给 predictor |
| `mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py` | 下游：对 `multi_scale_features` 的长度断言与逐尺度消费 |
| `mask2former/modeling/__init__.py` | import 链，决定哪些类会被自动注册 |
| `mask2former/config.py` | `PIXEL_DECODER_NAME` 等配置键的注册 |
| `configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml` | R50 基线配置，展示 `PIXEL_DECODER_NAME` / `COMMON_STRIDE` 的实际取值 |

## 4. 核心概念与源码讲解

### 4.1 替换入口：build_pixel_decoder 与它的「弱校验」

#### 4.1.1 概念说明

官方扩展指南 [ADVANCED_USAGE.md:L13-L22](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/ADVANCED_USAGE.md#L13-L22) 对「换像素解码器」只提了三条要求：把类定义（并注册）在 `mask2former/modeling/pixel_decoder` 下、改配置、以及——最关键的——**必须实现 `self.forward_features(features)` 方法并返回三个值**：

1. `mask_features`：1/4 分辨率的逐像素嵌入，用于生成二值 mask；
2. 第二项：直接返回 `None` 即可；
3. `multi_scale_features`：送入 Transformer 解码器的多尺度输入，**必须是长度为 3 的列表**（仓库用 1/32、1/16、1/8，文档说明也可用其他分辨率）。

这三条就是本讲的全部「契约」。但要注意一个容易被忽视的事实：**工厂函数只校验契约的第一半（有没有 `forward_features` 方法），不校验返回值结构**。返回三元组、长度 3、通道数这些要求是靠下游运行时断言或直接报错兜底的——这是一种典型的「弱校验 + 消费端兜底」设计。理解这一点能帮你快速定位自定义解码器的报错位置：报错通常不在构建期，而在第一次前向时。

#### 4.1.2 核心流程

`build_pixel_decoder` 的构建流程只有四步：

```text
1. 读 cfg.MODEL.SEM_SEG_HEAD.PIXEL_DECODER_NAME 得到类名
2. SEM_SEG_HEADS_REGISTRY.get(name) 查注册表
3. 以 (cfg, input_shape) 为参数实例化（走 from_config 模式）
4. getattr(model, "forward_features", None) 检查可调用性
   └─ 不可调用 → 抛 ValueError
```

配合 u1-l5 讲过的配置系统，完整的「换解码器」调用链是：

```text
yaml 里 PIXEL_DECODER_NAME: "XXX"
   ↓ (五层配置合并)
MaskFormerHead.from_config
   ↓ 调 build_pixel_decoder(cfg, input_shape)     ← mask_former_head.py:L105
SEM_SEG_HEADS_REGISTRY.get("XXX")(cfg, input_shape)
   ↓
返回的实例被注入 self.pixel_decoder
```

#### 4.1.3 源码精读

工厂函数本体在 fpn.py 里（而不是 `pixel_decoder/__init__.py`——后者只有一行版权注释）：

- [mask2former/modeling/pixel_decoder/fpn.py:L21-L33](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L21-L33)：`build_pixel_decoder` 完整实现。第 25 行读配置键，第 26 行查 `SEM_SEG_HEADS_REGISTRY` 并立刻以 `(cfg, input_shape)` 调用——这就是自定义类必须兼容 `from_config(cfg, input_shape)` 签名的原因。

弱校验的关键三行在 [mask2former/modeling/pixel_decoder/fpn.py:L27-L32](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L27-L32)：`getattr` 取 `forward_features`，只要不可调用就抛 `ValueError`，错误信息明确说「Only SEM_SEG_HEADS with forward_features method can be used as pixel decoder」。注意它**不检查**返回值是不是三元组、也不检查 `multi_scale_features` 长度。

调用点在组装器里：

- [mask2former/modeling/meta_arch/mask_former_head.py:L105](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/mask_former_head.py#L105)：`MaskFormerHead.from_config` 中 `"pixel_decoder": build_pixel_decoder(cfg, input_shape)`，构建出的实例作为关键字参数注入 `__init__`，最终挂在 `self.pixel_decoder` 上（[mask2former/modeling/meta_arch/mask_former_head.py:L81](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/mask_former_head.py#L81)）。

注册的触发链：

- [mask2former/modeling/__init__.py:L3-L4](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/__init__.py#L3-L4)：`from .pixel_decoder.fpn import BasePixelDecoder` 与 `from .pixel_decoder.msdeformattn import MSDeformAttnPixelDecoder`——import 模块即执行文件顶部的 `@SEM_SEG_HEADS_REGISTRY.register()` 装饰器（分别在 [fpn.py:L37](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L37) 与 [msdeformattn.py:L164](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/msdeformattn.py#L164)）。这与 u8-l1 的结论一致：**你自己新增的文件不会被自动 import**，要么改 `__init__.py`（研究项目里的常见做法），要么在使用前手动 `import` 你的模块（本讲实践采用后者，避免改动源码）。

配置键的注册点：

- [mask2former/config.py:L71](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L71)：`cfg.MODEL.SEM_SEG_HEAD.PIXEL_DECODER_NAME = "BasePixelDecoder"` 是该键的默认值。R50 基线 yaml 把它覆盖为 `MSDeformAttnPixelDecoder`（[configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml:L14](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml#L14)）。切换解码器 = 改这一个键。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「弱校验」的两个面——合法类能构建，缺 `forward_features` 的类在构建期就被拦截。

**操作步骤**（示例代码，保存为仓库根目录下的 `check_build.py` 并运行 `python check_build.py`）：

```python
# 示例代码
from detectron2.config import get_cfg
from detectron2.layers import ShapeSpec
from detectron2.modeling import SEM_SEG_HEADS_REGISTRY
from detectron2.projects.deeplab import add_deeplab_config

from mask2former import add_maskformer2_config
from mask2former.modeling.pixel_decoder.fpn import build_pixel_decoder

cfg = get_cfg()
add_deeplab_config(cfg)
add_maskformer2_config(cfg)

# R50 四路特征的 ShapeSpec（通道数/stride 与 ResNet-50 对应）
input_shape = {
    "res2": ShapeSpec(channels=256, stride=4),
    "res3": ShapeSpec(channels=512, stride=8),
    "res4": ShapeSpec(channels=1024, stride=16),
    "res5": ShapeSpec(channels=2048, stride=32),
}

# 1) 正常路径：默认键值 BasePixelDecoder 可以直接构建
dec = build_pixel_decoder(cfg, input_shape)
print(type(dec).__name__)

# 2) 注册一个没有 forward_features 的类，观察 ValueError
@SEM_SEG_HEADS_REGISTRY.register()
class BrokenDecoder:
    def __init__(self, cfg, input_shape):
        pass

cfg.MODEL.SEM_SEG_HEAD.PIXEL_DECODER_NAME = "BrokenDecoder"
try:
    build_pixel_decoder(cfg, input_shape)
except ValueError as e:
    print("捕获预期错误：", e)
```

**需要观察的现象**：第一次构建打印 `BasePixelDecoder`；第二次构建进入 `except` 分支，打印的错误信息以 "Only SEM_SEG_HEADS with forward_features method..." 开头。

**预期结果**：证明构建期校验只覆盖「方法存在性」。若给 `BrokenDecoder` 加一个返回错误结构（比如只返回一个张量）的 `forward_features`，构建期不会报错，错误会推迟到前向时才爆出——这正是 4.2 要讲的契约必须靠你自己遵守的原因。（本实践完整输出待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：`build_pixel_decoder` 从哪个配置键读类名？这个键在哪个文件注册、默认值是什么？

答案：从 `cfg.MODEL.SEM_SEG_HEAD.PIXEL_DECODER_NAME` 读取；在 `mask2former/config.py` 的 `add_maskformer2_config` 中注册，默认值 `"BasePixelDecoder"`；R50 基线配置将它覆盖为 `"MSDeformAttnPixelDecoder"`。

**练习 2**：如果你把自定义解码器文件放在 `mask2former/modeling/pixel_decoder/my_decoder.py` 但没有改任何 `__init__.py`，`build_pixel_decoder` 会发生什么？如何在不改 detectron2 的前提下修复？

答案：注册表查不到该名字，`SEM_SEG_HEADS_REGISTRY.get(name)` 抛出 "Not a registered name" 类错误（注册表 miss）。修复方式有两种：在 `mask2former/modeling/__init__.py`（或 `pixel_decoder` 包的入口）加一行 import；或在使用脚本里先 `import my_decoder` 手动触发注册——注册只依赖「模块被 import 过」，不依赖文件放哪。

**练习 3**：为什么说接口校验是「弱校验」？举一个能通过构建期检查但前向必挂的反例。

答案：校验只 `getattr` 检查 `forward_features` 可调用，不检查返回值结构。反例：`forward_features` 只 `return None`——构建成功，但 `MaskFormerHead.layers` 第 119 行的三元组解包直接抛 `TypeError: cannot unpack non-iterable NoneType`。

### 4.2 forward_features 的三元组契约与尺度约定

#### 4.2.1 概念说明

契约的权威定义是消费方。`MaskFormerHead.layers` 用一行解包消费三元组，随后按 `TRANSFORMER_IN_FEATURE` 分发。逐项拆解三个返回值：

**第一项 `mask_features`——1/4 分辨率的逐像素嵌入**。

- 通道数应为 `cfg.MODEL.SEM_SEG_HEAD.MASK_DIM`（默认 256，注册于 [mask2former/config.py:L67](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L67)）。
- 分辨率约定为原图的 1/4，即形状 \((B, C_{mask}, H/4, W/4)\)。它的唯一下游是预测头：`einsum("bqc,bchw->bqhw")` 与每个 query 的 mask 嵌入做点积生成 mask logits（u4-l4）。训练时损失用归一化坐标的 `point_sample` 对采（u5-l2），因此 1/4 分辨率是「质量与显存的折中」而非硬性数值要求——但整条推理链（如 attention mask 下采样目标尺寸）都按 1/4 假设设计，遵守约定最安全。
- `common_stride=4` 这个名字的语义来源就是它：mask 特征相对原图的公共下采样倍数。注意一个细节——`COMMON_STRIDE` **配置键只有 MSDeformAttnPixelDecoder 读取**（[msdeformattn.py:L311](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/msdeformattn.py#L311)），`BasePixelDecoder.from_config`（[fpn.py:L125-L134](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L125-L134)）根本不读它，它的 1/4 是 FPN 结构「融合到 res2」自然产生的；`MaskFormerHead` 里另有一个硬编码的 `self.common_stride = 4`（[mask_former_head.py:L78](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/mask_former_head.py#L78)）。

**第二项——`None` 就够用**。

这是三个官方实现的对照表（重点：默认配置下这项根本无人消费）：

| 实现 | 第二个返回值 | 何时被消费 |
| --- | --- | --- |
| `BasePixelDecoder` | 恒为 `None` | 从不 |
| `TransformerEncoderPixelDecoder` | 1/32 的 encoder 输出特征 | 仅 `TRANSFORMER_IN_FEATURE="transformer_encoder"`（v1 用法） |
| `MSDeformAttnPixelDecoder` | `out[0]`（1/32） | 默认模式下不被消费 |

消费逻辑在 `layers` 的 else 分支：只有 `transformer_in_feature == "transformer_encoder"` 时才解引用第二项，且带断言「Please use the TransformerEncoderPixelDecoder」。Mask2Former（v2）默认 `multi_scale_pixel_decoder` 走 if 分支，第二项被丢弃——所以自定义解码器返回 `None` 完全合规。

**第三项 `multi_scale_features`——长度必须为 3 的列表**。

- **长度 3 是硬约束**：下游 Transformer 解码器硬编码 `self.num_feature_levels = 3` 并在 forward 开头断言（见下方源码精读），长度不符在第一次前向就报错。
- **`1/32、1/16、1/8` 是软约定**：ADVANCED_USAGE.md 明说「We use resolution 1/32, 1/16, and 1/8 but you can use arbitrary resolutions here」。但**顺序（低分辨率 → 高分辨率）应当遵守**：解码器按下标 `x[i]` 消费、`level_embed[i]` 注入尺度身份、`level_index = i % 3` 轮转选尺度，顺序颠倒不会报错，但 level_embed 学到的「尺度身份」与实际尺度错位，属静默的逻辑错误。
- 通道数应与解码器 `hidden_dim` 一致（`CONVS_DIM=256` 恰好等于 `HIDDEN_DIM=256`，此时解码器内的 `input_proj` 为恒等映射，u4-l3）。不一致也能跑——解码器会自建 1×1 投影——但多一层无谓开销。

尺度约定的换算（以 LSJ 的 1024×1024 输入为例）：`mask_features` 为 \(1024/4 \times 1024/4 = 256 \times 256\)；三个多尺度特征依次为 \(32\times32\)、\(64\times64\)、\(128\times128\)。

MSDeformAttnPixelDecoder 里还有一个与尺度相关的公式值得回顾（u3-l5 讲过）：它需要用 extra FPN 层从 transformer 处理的最粗尺度补回高分辨率，层数为

\[
\text{num\_fpn\_levels} = \log_2(\text{min\_stride}) - \log_2(\text{common\_stride})
\]

默认 `min_stride=8`（res3..res5 中最小 stride 是 res3 的 8）、`common_stride=4`，故 \(3-2=1\)，只补 res2 一层。这个公式正是「`common_stride` 决定 mask_features 分辨率」在默认解码器里的量化体现。

#### 4.2.2 核心流程

一次前向中，像素解码器输入输出的完整数据流：

```text
backbone: {"res2": (B,C2,H/4,W/4), "res3": (B,C3,H/8,W/8),
           "res4": (B,C4,H/16,W/16), "res5": (B,C5,H/32,W/32)}
        │
        ▼
pixel_decoder.forward_features(features)
        │
        ├──► 返回值 1: mask_features (B, MASK_DIM, H/4, W/4)
        │        └─► predictor 内部: einsum("bqc,bchw->bqhw") 生成每个 query 的 mask
        ├──► 返回值 2: None（默认 multi_scale_pixel_decoder 模式下无人消费）
        └──► 返回值 3: multi_scale_features = [1/32, 1/16, 1/8]（长度 3，通道 CONVS_DIM）
                 └─► predictor: 逐尺度 flatten + level_embed → 轮转 masked cross-attention

MaskFormerHead.layers 的分发逻辑：
if transformer_in_feature == "multi_scale_pixel_decoder":   # v2 默认
    predictor(multi_scale_features, mask_features, mask)
elif transformer_in_feature == "transformer_encoder":       # v1 用法
    assert 第二项 is not None
    predictor(第二项, mask_features, mask)
elif transformer_in_feature == "pixel_embedding":
    predictor(mask_features, mask_features, mask)
else:                                                        # 直接用某个 backbone 特征
    predictor(features[name], mask_features, mask)
```

#### 4.2.3 源码精读

**消费端——三元组的权威定义处**：

- [mask2former/modeling/meta_arch/mask_former_head.py:L118-L132](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/mask_former_head.py#L118-L132)：`MaskFormerHead.layers` 全文。第 119 行 `mask_features, transformer_encoder_features, multi_scale_features = self.pixel_decoder.forward_features(features)` 一行定死三元组结构；第 120-121 行是 v2 默认分支，把 `multi_scale_features` 作第一参数、`mask_features` 作第二参数传给 predictor；第 123-126 行是 v1 分支的断言——第二项为 `None` 时给出「Please use the TransformerEncoderPixelDecoder」的明确提示。

**参照实现一——`BasePixelDecoder`（返回 `None` 的标准写法）**：

- [mask2former/modeling/pixel_decoder/fpn.py:L136-L154](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L136-L154)：`forward_features` 主体。循环按 `self.in_features[::-1]`（res5→res2，低分辨率到高分辨率）自顶向下融合；每级结果 `y` 依次 append 进 `multi_scale_features`，因此列表天然按 **1/32、1/16、1/8** 排列（先 append 的是 res5 级）。循环里最粗一级没有 lateral（`lateral_conv is None`），直接过 `output_conv`。
- [mask2former/modeling/pixel_decoder/fpn.py:L154](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L154)：`return self.mask_features(y), None, multi_scale_features`——`y` 此时是融合到 res2 级（1/4）的特征，经 `self.mask_features` 3×3 卷积映射到 `mask_dim` 通道；第二项字面量 `None`。
- [mask2former/modeling/pixel_decoder/fpn.py:L113-L123](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L113-L123)：`self.mask_features` 卷积定义与 `self.maskformer_num_feature_levels = 3`——「长度 3」在像素解码器侧的对应物。
- [mask2former/modeling/pixel_decoder/fpn.py:L156-L159](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L156-L159)：普通 `forward` 只是转发并打 warning「Calling forward() may cause unpredicted behavior」——再次强调契约入口是 `forward_features` 而非 `forward`。

**参照实现二——`TransformerEncoderPixelDecoder`（第二项有值的场景）**：

- [mask2former/modeling/pixel_decoder/fpn.py:L284-L307](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L284-L307)：同构的自顶向下循环，区别在最粗一级：res5 先 `input_proj` 进全局自注意力 encoder，输出既过 `output_conv` 进 FPN 主链，也被存为 `transformer_encoder_features`。
- [mask2former/modeling/pixel_decoder/fpn.py:L307](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L307)：`return self.mask_features(y), transformer_encoder_features, multi_scale_features`——第二项返回 1/32 的 encoder 特征，供 v1 的 `transformer_encoder` 模式消费。

**参照实现三——`MSDeformAttnPixelDecoder`（默认解码器）**：

- [mask2former/modeling/pixel_decoder/msdeformattn.py:L314-L358](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/msdeformattn.py#L314-L358)：res5→res3 逐级 `input_proj` 送多尺度可变形注意力 encoder，`torch.split` 按 `level_start_index` 还原成 2D 特征后，用 extra FPN 层（[L343-L351](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/msdeformattn.py#L343-L351)，注意这里上采样用 `bilinear` 而 BasePixelDecoder 用 `nearest`）补出 res2 级。
- [mask2former/modeling/pixel_decoder/msdeformattn.py:L353-L358](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/msdeformattn.py#L353-L358)：先从 `out` 收满 3 个 `multi_scale_features`，最后 `return self.mask_features(out[-1]), out[0], multi_scale_features`——`out[-1]` 是 res2 级（1/4），`out[0]` 是 res5 级（1/32）。
- [mask2former/modeling/pixel_decoder/msdeformattn.py:L258-L259](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/msdeformattn.py#L258-L259)：`num_fpn_levels` 的对数公式，量化了「transformer 最粗尺度 ↔ common_stride」之间的差距。

**下游断言——长度 3 的兜底**：

- [mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:L321](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L321)：`self.num_feature_levels = 3` 硬编码。
- [mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:L365](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L365)：`assert len(x) == self.num_feature_levels`——`multi_scale_features` 长度不是 3 时，第一次前向在这里立刻失败。
- [mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:L373-L376](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L373-L376)：消费循环——`size_list`、位置编码、`input_proj` + `level_embed` 全部按 `x[i]` 的**下标顺序**建立，这就是「顺序 low→high 必须遵守」的直接证据。

#### 4.2.4 代码实践

**实践目标**：不跑完整模型，单独实例化 `BasePixelDecoder` 并喂入假特征，亲手核对三元组的形状、顺序与第二项的 `None`。

**操作步骤**（示例代码，保存为 `check_contract.py`）：

```python
# 示例代码
import torch
from detectron2.layers import ShapeSpec
from mask2former.modeling.pixel_decoder.fpn import BasePixelDecoder

input_shape = {
    "res2": ShapeSpec(channels=256, stride=4),
    "res3": ShapeSpec(channels=512, stride=8),
    "res4": ShapeSpec(channels=1024, stride=16),
    "res5": ShapeSpec(channels=2048, stride=32),
}
# @configurable 类支持绕过 from_config 直接传参实例化
dec = BasePixelDecoder(input_shape, conv_dim=256, mask_dim=256, norm="GN")

B = 2
features = {
    "res2": torch.rand(B, 256, 128, 128),    # 512/4
    "res3": torch.rand(B, 512, 64, 64),      # 512/8
    "res4": torch.rand(B, 1024, 32, 32),     # 512/16
    "res5": torch.rand(B, 2048, 16, 16),     # 512/32
}
mask_features, second, multi = dec.forward_features(features)

print("mask_features:", mask_features.shape)
print("second:", second)
for i, t in enumerate(multi):
    print(f"multi[{i}]: {tuple(t.shape)}")
```

**需要观察的现象**：三组打印分别给出形状、`None` 与三个尺度。

**预期结果**（按源码推演，待本地验证）：

```text
mask_features: torch.Size([2, 256, 128, 128])   # 1/4 分辨率，MASK_DIM 通道
second: None
multi[0]: (2, 256, 16, 16)                      # 1/32
multi[1]: (2, 256, 32, 32)                      # 1/16
multi[2]: (2, 256, 64, 64)                      # 1/8
```

若把 `multi` 列表手动 reverse 后再喂给解码器，断言 `len(x) == 3` 依然通过——这验证了「顺序是软约定、无兜底」的论断。

#### 4.2.5 小练习与答案

**练习 1**：`mask_features` 的通道数由哪个配置键决定？分辨率由什么决定？

答案：通道数由 `MODEL.SEM_SEG_HEAD.MASK_DIM`（默认 256）决定；分辨率按契约取原图 1/4。在 `BasePixelDecoder` 中这是「FPN 融合到 res2 + 3×3 卷积」的结构结果；在 `MSDeformAttnPixelDecoder` 中由 `COMMON_STRIDE`（默认 4）经 `num_fpn_levels` 公式显式控制。

**练习 2**：第二项返回值在什么配置组合下才不能是 `None`？

答案：仅当 `MODEL.MASK_FORMER.TRANSFORMER_IN_FEATURE == "transformer_encoder"`（v1 用法，需搭配 `TransformerEncoderPixelDecoder`）时被消费，为 `None` 会触发断言；v2 默认的 `multi_scale_pixel_decoder` 模式下永远可以为 `None`。

**练习 3**：如果把 `multi_scale_features` 改成长度 4（多补一个 1/4 尺度），会在哪里、以什么方式失败？

答案：在 Transformer 解码器 `forward` 开头的 `assert len(x) == self.num_feature_levels`（mask2former_transformer_decoder.py 第 365 行）抛 `AssertionError`，且因为 `self.num_feature_levels = 3` 是硬编码，改配置无法绕过——长度 3 是硬契约。

### 4.3 动手实现 MiniPixelDecoder

#### 4.3.1 概念说明

理解契约之后，实现一个「最小可行」像素解码器出奇地简单：**不需要任何注意力**，四个 1×1 投影 + 自顶向下相加 + 一个 3×3 卷积即可满足全部契约。这就是 `BasePixelDecoder` 的骨架去掉 GN/ReLU 精修层后的形态，我们叫它 `MiniPixelDecoder`。

设计要点对照契约逐条落实：

| 契约条款 | MiniPixelDecoder 的落实方式 |
| --- | --- |
| 可被 `build_pixel_decoder` 构建 | `@SEM_SEG_HEADS_REGISTRY.register()` + `from_config(cfg, input_shape)` 签名 |
| `forward_features` 存在 | 直接实现该方法（不实现 `forward` 也行，但保留一个转发更稳妥） |
| 第一项：1/4、`mask_dim` 通道 | res2 横向连接 + 上采样融合后过 3×3 卷积到 `mask_dim` |
| 第二项：可为 `None` | 直接返回 `None` |
| 第三项：长度 3、1/32→1/16→1/8 | 自顶向下融合的中间结果按低→高顺序收集 |
| 通道与解码器 hidden_dim 一致 | 中间通道一律取 `CONVS_DIM`（默认 256） |

一个工程上的选择：不放 `mask2former/modeling/pixel_decoder/` 目录下。ADVANCED_USAGE 官方建议放那里并改 `__init__.py`，但学习实践里我们把它写成独立文件（如仓库根目录的 `my_mini_pixel_decoder.py`），在使用脚本里显式 `import` 触发注册——零源码改动，且恰好复习了 4.1 的注册机制。

#### 4.3.2 核心流程

MiniPixelDecoder 的 `forward_features` 伪代码：

```text
输入 features = {res2(1/4), res3(1/8), res4(1/16), res5(1/32)}

y = proj[res5](res5)                        # (B, conv_dim, H/32, W/32)
outs = [y]                                  # 收集 1/32
for name in [res4, res3]:                   # 自顶向下
    y = proj[name](name) + upsample_nearest(y, 对齐 name 的尺寸)
    outs.append(y)                          # 依次收集 1/16、1/8
y = proj[res2](res2) + upsample_nearest(y, 对齐 res2 的尺寸)   # 融合到 1/4
mask_features = mask_conv(y)                # (B, mask_dim, H/4, W/4)

return mask_features, None, outs            # 三元组契约
```

自顶向下融合的直觉：低分辨率特征语义强但定位粗，逐级上采样加到高分辨率特征上，让每级都同时拥有「语义 + 定位」。这与 `BasePixelDecoder.forward_features` 的循环结构同构，只是省掉了 3×3 精修卷积与归一化。

#### 4.3.3 源码精读

以下为示例代码（**非项目原有文件**），结构刻意贴近 [fpn.py:L136-L154](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L136-L154) 以便对照阅读。保存为仓库根目录 `my_mini_pixel_decoder.py`：

```python
# 示例代码：my_mini_pixel_decoder.py —— 最小可行像素解码器
import torch
from torch import nn
from torch.nn import functional as F

import fvcore.nn.weight_init as weight_init
from detectron2.config import configurable
from detectron2.layers import Conv2d, ShapeSpec
from detectron2.modeling import SEM_SEG_HEADS_REGISTRY


@SEM_SEG_HEADS_REGISTRY.register()
class MiniPixelDecoder(nn.Module):
    @configurable
    def __init__(self, input_shape: dict, *, conv_dim: int, mask_dim: int):
        super().__init__()
        # 按 stride 升序排序，得到 [res2, res3, res4, res5]
        input_shape = sorted(input_shape.items(), key=lambda x: x[1].stride)
        self.in_features = [k for k, v in input_shape]
        channels = {k: v.channels for k, v in input_shape}

        # 每路特征一个 1x1 投影，拉齐到 conv_dim 通道
        self.proj = nn.ModuleDict(
            {name: Conv2d(channels[name], conv_dim, kernel_size=1)
             for name in self.in_features}
        )
        # 3x3 卷积把 1/4 融合特征映射到 mask_dim 通道 -> mask_features
        self.mask_features = Conv2d(conv_dim, mask_dim, kernel_size=3, padding=1)
        for m in self.proj.values():
            weight_init.c2_xavier_fill(m)
        weight_init.c2_xavier_fill(self.mask_features)

    @classmethod
    def from_config(cls, cfg, input_shape):
        # 只消费三个最基础的配置键，其他键一概不读
        return {
            "input_shape": {
                k: v for k, v in input_shape.items()
                if k in cfg.MODEL.SEM_SEG_HEAD.IN_FEATURES
            },
            "conv_dim": cfg.MODEL.SEM_SEG_HEAD.CONVS_DIM,
            "mask_dim": cfg.MODEL.SEM_SEG_HEAD.MASK_DIM,
        }

    def forward_features(self, features):
        outs = []
        # 自顶向下：res5 -> res4 -> res3，outs 按 1/32、1/16、1/8 顺序收集
        y = self.proj["res5"](features["res5"])
        outs.append(y)
        for name in ["res4", "res3"]:
            y = self.proj[name](features[name]) + F.interpolate(
                y, size=features[name].shape[-2:], mode="nearest"
            )
            outs.append(y)
        # 融合 res2，得到 1/4 分辨率的 mask_features
        y = self.proj["res2"](features["res2"]) + F.interpolate(
            y, size=features["res2"].shape[-2:], mode="nearest"
        )
        return self.mask_features(y), None, outs

    def forward(self, features, targets=None):
        return self.forward_features(features)
```

逐点对照官方实现：

- `from_config` 的键过滤写法与 [fpn.py:L125-L134](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L125-L134) 一致——只保留 `IN_FEATURES` 声明过的特征，通道数从 `ShapeSpec` 读取，因此换 backbone（如 Swin，通道 96/192/384/768）无需改动本类。
- `nearest` 上采样与 [fpn.py:L148-L149](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L148-L149) 的选择一致（FPPN 传统）；MSDeformAttn 的 extra FPN 层则用 `bilinear`（[msdeformattn.py:L349](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/msdeformattn.py#L349)），两者都合规。
- 返回语句 `return self.mask_features(y), None, outs` 与 [fpn.py:L154](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L154) 逐字同构。
- 参数量对比参考：R50 配置下 MiniPixelDecoder 约 \(256{\times}(256{+}512{+}1024{+}2048) + 9{\times}256{\times}256 \approx 1.03\text{M}\)，而 `MSDeformAttnPixelDecoder` 含六层可变形注意力 encoder，参数量约为其一个数量级——「最小可行」的代价是放弃了跨尺度长程建模，精度必然下降，但足以验证接口。

#### 4.3.4 代码实践

**实践目标**：单独验证 MiniPixelDecoder 满足三元组契约（形状、顺序、第二项）。

**操作步骤**（示例代码）：

```python
# 示例代码：check_mini.py
import torch
from detectron2.layers import ShapeSpec
import my_mini_pixel_decoder  # 必须 import 触发注册
from my_mini_pixel_decoder import MiniPixelDecoder

input_shape = {
    "res2": ShapeSpec(channels=256, stride=4),
    "res3": ShapeSpec(channels=512, stride=8),
    "res4": ShapeSpec(channels=1024, stride=16),
    "res5": ShapeSpec(channels=2048, stride=32),
}
dec = MiniPixelDecoder(input_shape, conv_dim=256, mask_dim=256)
features = {
    "res2": torch.rand(2, 256, 128, 128),
    "res3": torch.rand(2, 512, 64, 64),
    "res4": torch.rand(2, 1024, 32, 32),
    "res5": torch.rand(2, 2048, 16, 16),
}
mask_features, second, multi = dec.forward_features(features)
print("mask_features:", mask_features.shape)   # (2, 256, 128, 128)
print("second:", second)                        # None
print([tuple(t.shape) for t in multi])
# [(2,256,16,16), (2,256,32,32), (2,256,64,64)] 即 1/32、1/16、1/8
```

**需要观察的现象**：三行打印与 4.2.4 中 `BasePixelDecoder` 的输出完全同构。

**预期结果**：形状逐项匹配契约（待本地验证）。若 `second` 不是 `None` 或 `multi` 长度不为 3，说明实现偏离契约。

#### 4.3.5 小练习与答案

**练习 1**：MiniPixelDecoder 为什么必须写 `from_config`？直接写普通 `__init__(self, conv_dim, ...)` 行不行？

答案：不行（对 `build_pixel_decoder` 而言）。工厂函数以 `SEM_SEG_HEADS_REGISTRY.get(name)(cfg, input_shape)` 的固定签名调用，detectron2 的 `@configurable` 装饰器会把这种调用路由到 `from_config(cfg, input_shape)` 再喂给真 `__init__`。没有 `from_config` 就无法从 `(cfg, input_shape)` 翻译出构造参数。（绕过工厂、在测试脚本里直接传参实例化则不需要，如 4.3.4 的写法。）

**练习 2**：把 MiniPixelDecoder 中间通道从 `conv_dim`（256）改成 128，模型还能跑通吗？为什么？

答案：能跑通但多一层开销。`multi_scale_features` 通道变为 128，与解码器 `HIDDEN_DIM=256` 不一致，解码器 `from_config` 检测到 in_channels ≠ hidden_dim 后会自建 1×1 投影对齐（`ENFORCE_INPUT_PROJ=False` 时的自动行为）；`mask_features` 通道变为 128 也不影响预测头的 einsum（点积维度自动对齐）。接口上合规，只是白付一次投影开销。

**练习 3**：MiniPixelDecoder 与 BasePixelDecoder 相比省掉了什么？这些省略分别影响契约的哪一条？

答案：省掉了 3×3 output 卷积精修、GN 归一化、ReLU 激活（`BasePixelDecoder` 每级融合后的 `output_conv`）。这些都不在契约条款里——契约只约束返回值结构与尺度，因此省略不影响合规性，只影响特征质量与最终精度。

## 5. 综合实践

**任务**：把 MiniPixelDecoder 接入完整 MaskFormer 模型，在 CPU 上跑通一次端到端前向，确认「改一行配置即换解码器」。

**前置**：u1-l2 的环境已装好（`import mask2former` 需要 MSDeformAttn 扩展已编译）；`my_mini_pixel_decoder.py` 已放在仓库根目录。

**步骤**（示例代码，保存为 `run_mini_decoder.py`）：

```python
# 示例代码：run_mini_decoder.py
import torch
import my_mini_pixel_decoder  # 触发 MiniPixelDecoder 注册

from detectron2.config import get_cfg
from detectron2.modeling import build_model
from detectron2.projects.deeplab import add_deeplab_config
from mask2former import add_maskformer2_config  # import mask2former 亦在此触发

cfg = get_cfg()
add_deeplab_config(cfg)
add_maskformer2_config(cfg)
cfg.merge_from_file(
    "configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml"
)
# 核心一步：换解码器只需改这一个键
cfg.merge_from_list(["MODEL.SEM_SEG_HEAD.PIXEL_DECODER_NAME", "MiniPixelDecoder"])
cfg.MODEL.DEVICE = "cpu"
cfg.freeze()

model = build_model(cfg)
print(type(model.sem_seg_head.pixel_decoder).__name__)  # MiniPixelDecoder

model.eval()
with torch.no_grad():
    outputs = model([{"image": torch.rand(3, 384, 512)}])

print(outputs[0].keys())          # COCO 全景配置三开关全开
print(outputs[0]["sem_seg"].shape)
```

**需要观察的现象与预期结果**（待本地验证）：

1. 第一行打印 `MiniPixelDecoder`，证明注册表查到的已是自定义类。
2. `outputs[0]` 的键为 `dict_keys(['sem_seg', 'instances', 'panoptic_seg'])`——COCO 基线配置 `SEMANTIC_ON/INSTANCE_ON/PANOPTIC_ON` 三开关全开（[maskformer2_R50_bs16_50ep.yaml:L40-L43](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml#L40-L43)），且 eval 分支的三个推理函数都成功消费了 MiniPixelDecoder 的输出。
3. `sem_seg` 形状为 `(133, 384, 512)`——133 类（含背景语义类全集）、还原到输入图尺寸（u6-l1）。
4. 全程无 `AssertionError`——即 `len(x) == 3` 断言、`forward_features` 解包、预测头 einsum 全部通过。

**进一步实验**（可选）：

- 把 `cfg.MODEL.SEM_SEG_HEAD.PIXEL_DECODER_NAME` 改回 `"MSDeformAttnPixelDecoder"` 再跑一次，对比两者 `sem_seg` 输出的差异像素占比——随机初始化权重下两者都是噪声，但能验证「即插即换、互不干扰」。
- 在 `forward_features` 的返回前加一行 `print`，观察每级特征的形状随输入尺寸（如 384×512 → 96×128 的 mask_features，1/32 级为 12×16）变化。
- 若想走官方推荐路径：把 `my_mini_pixel_decoder.py` 移入 `mask2former/modeling/pixel_decoder/` 并在 `mask2former/modeling/__init__.py` 加一行 import——本教程不这么做是为了保持源码零改动。

## 6. 本讲小结

- `build_pixel_decoder`（fpn.py）按 `MODEL.SEM_SEG_HEAD.PIXEL_DECODER_NAME` 从 `SEM_SEG_HEADS_REGISTRY` 查表构建，接口校验是**弱校验**：只查 `forward_features` 可调用，不查返回值结构。
- `forward_features` 的三元组契约：`(1/4 分辨率、mask_dim 通道的 mask_features, 第二项, 长度为 3 的 multi_scale_features)`；第二项在 v2 默认的 `multi_scale_pixel_decoder` 模式下无人消费，返回 `None` 即可。
- 尺度约定分两级：**长度 3 是硬约束**（解码器 `assert len(x) == 3` 兜底），**1/32→1/16→1/8 的顺序是软约定**（level_embed 按下标注入尺度身份，颠倒不报错但语义错位）；`common_stride=4` 对应 mask_features 的 1/4 分辨率，该配置键只有 `MSDeformAttnPixelDecoder` 读取。
- 三个官方实现对照：`BasePixelDecoder` 第二项恒 `None`；`TransformerEncoderPixelDecoder` 返回 1/32 encoder 特征（供 v1 `transformer_encoder` 模式）；`MSDeformAttnPixelDecoder` 返回 `out[0]`（默认模式下同样无人消费）。
- 换解码器 = 「注册 + 改一行配置」：注册靠 import 触发装饰器，配置只动 `PIXEL_DECODER_NAME` 一个键；`MiniPixelDecoder` 用四个 1×1 投影加自顶向下融合即满足全部契约。

## 7. 下一步学习建议

本讲完成了三段式架构中第二段（像素解码器）的替换。下一讲 **u8-l3（自定义 Transformer Decoder 与 Registry 机制）** 处理第三段：项目自建的 `TRANSFORMER_DECODER_REGISTRY`、`from_config(cfg, in_channels, mask_classification)` 的另一种签名约定，以及输出 dict 必须含 `pred_logits/pred_masks/aux_outputs` 的契约——注意它的构建签名与像素解码器不同（传的是通道数而非完整 `input_shape`），对照本讲的 `build_pixel_decoder` 可以看到两种契约设计风格的取舍。如果你更关心「不换组件、只换用法」，可以先读 **u8-l4（PerPixelBaselineHead 对照实验）**，看同一像素解码器如何接逐像素分类头。建议同时重读 [mask_former_head.py:L118-L132](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/mask_former_head.py#L118-L132)——它是三大组件契约的交汇点，本讲的 `forward_features` 契约与下一讲的 predictor 契约都在这里对表。

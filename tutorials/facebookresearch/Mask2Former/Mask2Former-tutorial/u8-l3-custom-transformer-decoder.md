# 扩展实践三：自定义 Transformer Decoder 与 Registry 机制

## 1. 本讲目标

前两讲我们已经把 Backbone（u8-l1）与 Pixel Decoder（u8-l2）替换成了自己的实现，本讲完成三段式架构（backbone → pixel decoder → Transformer decoder）扩展的最后一块拼图，也是最复杂的一块：**自定义 Transformer 解码器**。

学完本讲，你应该能够：

1. 说清 `TRANSFORMER_DECODER_REGISTRY` 为什么是「项目自建注册表」而非 detectron2 官方注册表，以及它如何借用了 detectron2 的 `Registry` 基础设施。
2. 跟踪 `build_transformer_decoder(cfg, in_channels, mask_classification)` 的完整调用链：从 `MaskFormerHead.from_config` 到 `TRANSFORMER_DECODER_REGISTRY.get(name)(...)` 的三参调用。
3. 掌握自定义解码器必须遵守的两条硬约定：
   - `from_config(cls, cfg, in_channels, mask_classification)` 签名约定（配合 `@configurable`）；
   - `forward(x, mask_features, mask=None)` 的输入契约与输出 dict 必须含 `pred_logits` / `pred_masks` / `aux_outputs`。
4. 动手实现一个仅 2 层、单尺度的 `MiniMaskedTransformerDecoder`，注册后改一行配置接入完整模型，跑通前向并算出损失。

## 2. 前置知识

本讲是「动手写」的讲义，默认你已读过以下内容（没读过也不影响，但建议先补）：

- **u3-l2（MaskFormerHead 组装器）**：知道 `MaskFormerHead` 自己不算东西，只持有 `pixel_decoder` 与 `predictor` 两个子模块，`predictor` 就是本讲要替换的 Transformer 解码器。
- **u4-l3（Masked Attention 主循环）**：知道官方 `MultiScaleMaskedTransformerDecoder.forward` 的层序是 cross → self → FFN，query 有 `query_feat`（内容）与 `query_embed`（位置）两个可学习嵌入，`query_feat` 在进解码层之前就先产出第 0 组受监督的预测——这决定了「DEC_LAYERS 配置减 1」的约定。
- **u4-l2（解码器基础件）**：知道 `SelfAttentionLayer` / `CrossAttentionLayer` / `FFNLayer` 三个积木的 `(L, B, C)` 序列格式，本讲的迷你解码器直接复用它们。
- **u5-l3（深监督与权重流转）**：知道 `weight_dict` 的 aux 键带 `_i` 后缀、`MaskFormer.forward` 会丢弃白名单外的损失键——本讲自定义解码器输出的 `aux_outputs` 条数必须与这套后缀体系对齐。

本讲需要的两个机制性概念：

- **注册表（Registry）**：一个「字符串名字 → 类（或工厂函数）」的字典。配置文件里只写名字，构建时查表取类。它是 detectron2 生态「配置驱动组装」的核心：配置决定用哪个类，import 决定哪些类可用。
- **`@configurable`**：detectron2 提供的装饰器。被装饰的 `__init__` 若发现第一个位置参数是 `CfgNode`（配置对象），会先调用同类classmethod `from_config` 把配置展开成关键字参数，再执行真正的 `__init__`。它让「从配置构建」与「手动传参构建」共用同一个构造函数。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [mask2former/modeling/transformer_decoder/maskformer_transformer_decoder.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/maskformer_transformer_decoder.py) | 定义 `TRANSFORMER_DECODER_REGISTRY` 与 `build_transformer_decoder`；v1 解码器 `StandardTransformerDecoder` 也在此 |
| [mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py) | v2 默认解码器 `MultiScaleMaskedTransformerDecoder` 及三种注意力积木，是自定义解码器的最佳模板 |
| [mask2former/modeling/transformer_decoder/__init__.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/__init__.py) | 包出口，import 即触发两个解码器的注册 |
| [mask2former/modeling/meta_arch/mask_former_head.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/mask_former_head.py) | `MaskFormerHead.from_config` 调用 `build_transformer_decoder`；`layers` 按 `transformer_in_feature` 把特征喂给解码器 |
| [mask2former/modeling/criterion.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py) | `SetCriterion` 消费 `pred_logits` / `pred_masks` / `aux_outputs` 的地方 |
| [mask2former/maskformer_model.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py) | 顶层 `MaskFormer`：训练分支按 `weight_dict` 加权过滤损失，推理分支读取输出 dict 两键 |
| [mask2former/config.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py) | `TRANSFORMER_DECODER_NAME` 等配置键的默认值注册处 |
| [ADVANCED_USAGE.md](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/ADVANCED_USAGE.md) | 官方对三大组件扩展的说明（Transformer decoder 一节只有两句话，本讲是它的完整展开） |

## 4. 核心概念与源码讲解

### 4.1 TRANSFORMER_DECODER_REGISTRY：项目为什么要自建注册表

#### 4.1.1 概念说明

detectron2 官方为常见组件类型各维护了一个注册表：`META_ARCH_REGISTRY`（顶层模型）、`SEM_SEG_HEADS_REGISTRY`（分割头）、`BACKBONE_REGISTRY`（骨干网络）等。但 detectron2 并没有「Transformer 解码器」这个概念——它是 MaskFormer 论文引入的组件，宿主框架自然没有为它预留注册表。

于是项目**借用 detectron2 的 `Registry` 类（一个通用的名字→对象字典实现），自建了一个项目级注册表**。这带来两个特点：

1. **基础设施复用，命名空间独立**：注册、查表、报错的逻辑全部来自 detectron2 的 `Registry` 类，但 `TRANSFORMER_DECODER_REGISTRY` 与 `SEM_SEG_HEADS_REGISTRY` 互不相通——`build_sem_seg_head` 查不到解码器，`build_transformer_decoder` 也查不到像素解码器。
2. **配置键也自建**：查表用的键 `cfg.MODEL.MASK_FORMER.TRANSFORMER_DECODER_NAME` 由 `add_maskformer2_config` 注册，而不是 detectron2 基础配置。

一个小历史包袱：注册表的内部名字是 `"TRANSFORMER_MODULE"`（v1 MaskFormer 时代的叫法），与变量名 `TRANSFORMER_DECODER_REGISTRY` 不一致，阅读日志或报错信息时不要被迷惑。

#### 4.1.2 核心流程

「import 即注册」的完整链条（承接 u1-l3）：

```
import mask2former
  └─ mask2former/modeling/__init__.py 逐个 import 子模块
       └─ meta_arch.mask_former_head  （注册 MaskFormerHead 到 SEM_SEG_HEADS_REGISTRY）
            └─ import build_transformer_decoder
                 └─ maskformer_transformer_decoder.py 模块顶层执行
                      ├─ TRANSFORMER_DECODER_REGISTRY = Registry("TRANSFORMER_MODULE")   ← 创建注册表
                      └─ @TRANSFORMER_DECODER_REGISTRY.register()                        ← StandardTransformerDecoder 入表
                 └─ mask2former_transformer_decoder.py 模块顶层执行
                      └─ @TRANSFORMER_DECODER_REGISTRY.register()                        ← MultiScaleMaskedTransformerDecoder 入表
```

只要 `mask2former` 被 import 过一次，注册表里就有两个成员：

| 注册名 | 版本 | 文件 |
| --- | --- | --- |
| `StandardTransformerDecoder` | v1（MaskFormer） | maskformer_transformer_decoder.py |
| `MultiScaleMaskedTransformerDecoder` | v2（Mask2Former，默认） | mask2former_transformer_decoder.py |

`configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml` 的 `TRANSFORMER_DECODER_NAME: "MultiScaleMaskedTransformerDecoder"`（[L20](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml#L20)）查的就是这张表。

#### 4.1.3 源码精读

[mask2former/modeling/transformer_decoder/maskformer_transformer_decoder.py:L10](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/maskformer_transformer_decoder.py#L10) —— 从 detectron2 借来的 `Registry` 基础设施，注册表的全部行为（register/get/报错）都由它实现。

[mask2former/modeling/transformer_decoder/maskformer_transformer_decoder.py:L16-L19](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/maskformer_transformer_decoder.py#L16-L19) —— 创建项目自建注册表，注意内部名字是历史遗留的 `"TRANSFORMER_MODULE"`。

[mask2former/modeling/transformer_decoder/maskformer_transformer_decoder.py:L30-L31](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/maskformer_transformer_decoder.py#L30-L31) —— v1 解码器以装饰器语法入表：`@TRANSFORMER_DECODER_REGISTRY.register()`，注册名默认取类名字符串。

[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:L14](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L14) —— v2 文件**跨文件复用同一张表**：直接 import v1 文件里创建的注册表对象，而不是再建一张。这是关键设计——注册表全局唯一，任何模块 import 它并调用 `.register()` 即可入表。

[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:L207-L208](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L207-L208) —— v2 默认解码器以同样方式入表。

[mask2former/modeling/transformer_decoder/__init__.py:L2-L3](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/__init__.py#L2-L3) —— 包出口只 import 这两个解码器。**你在该目录下新建的解码器文件不会自动被 import**，必须（a）在此处加一行 import，或（b）在使用方的脚本里显式 `import mask2former.modeling.transformer_decoder.你的文件`——两种方式效果等价，都会触发模块顶层执行从而完成注册。

[mask2former/config.py:L94](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L94) —— 配置键默认值：不写 yaml 时默认查 `MultiScaleMaskedTransformerDecoder`。

[ADVANCED_USAGE.md:L35-L37](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/ADVANCED_USAGE.md#L35-L37) —— 官方对「换 Transformer 解码器」的全部说明只有两句话：解码器定义在 `mask2former/modeling/transformer_decoder` 下、改配置即可。具体要遵守什么接口约定没有写，这正是本讲要补齐的内容。

#### 4.1.4 代码实践

**实践目标**：亲眼看到这张自建注册表里有什么，并验证「不 import 就查不到」。

**操作步骤**（示例代码，在仓库根目录运行）：

```python
# peek_registry.py —— 示例代码
from mask2former.modeling.transformer_decoder.maskformer_transformer_decoder import (
    TRANSFORMER_DECODER_REGISTRY,
)
print(TRANSFORMER_DECODER_REGISTRY)
```

`detectron2` 的 `Registry` 实现了友好的 `__repr__`，会打印表名与全部已注册名字。

**需要观察的现象**：输出中应包含 `StandardTransformerDecoder` 与 `MultiScaleMaskedTransformerDecoder` 两个名字。

**预期结果**：确认两个官方解码器都在表内。注意：**运行本脚本需要先完成 u1-l2 的环境安装**——`import mask2former` 会传递性地 import 像素解码器目录下的 MSDeformAttn 算子模块，后者在 import 期强依赖已编译的 `MultiScaleDeformableAttention` 扩展（见 [mask2former/modeling/pixel_decoder/ops/functions/ms_deform_attn_func.py:L21-L29](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/ops/functions/ms_deform_attn_func.py#L21-L29)，未编译时直接抛出带编译指引的 `ModuleNotFoundError`）。即使你打算用 BasePixelDecoder 绕开 MSDeformAttn，import 这一步也绕不开。

**运行结果：待本地验证。**

#### 4.1.5 小练习与答案

**练习 1**：`TRANSFORMER_DECODER_REGISTRY` 与 `SEM_SEG_HEADS_REGISTRY` 是同一张表吗？`build_sem_seg_head` 能不能查到 `MultiScaleMaskedTransformerDecoder`？

**答案**：不是同一张表。前者是本项目用 detectron2 的 `Registry` 类自建的（内部名 `"TRANSFORMER_MODULE"`），后者是 detectron2 官方的分割头注册表。两者命名空间完全独立，`build_sem_seg_head` 查的是 `SEM_SEG_HEADS_REGISTRY`，永远查不到任何 Transformer 解码器；反过来 `MaskFormerHead` 倒是注册在 `SEM_SEG_HEADS_REGISTRY` 里。

**练习 2**：为什么 `mask2former_transformer_decoder.py` 要 `from .maskformer_transformer_decoder import TRANSFORMER_DECODER_REGISTRY`，而不是自己再 `Registry(...)` 一张新表？

**答案**：注册表必须全局唯一才能成为「名字 → 类」的唯一权威映射。若各建一张表，`build_transformer_decoder` 只认 v1 文件里的那张，v2 解码器注册到别的表就永远查不到，配置里的名字会失效。Python 模块是单例的，import 同一个对象保证所有解码头共用一张表。

---

### 4.2 build_transformer_decoder：三参工厂与调用链

#### 4.2.1 概念说明

`build_transformer_decoder` 是一个极短的工厂函数：从配置读名字，查注册表，把类当函数调用。它的价值不在代码量，而在**它定死的调用签名**：

```python
TRANSFORMER_DECODER_REGISTRY.get(name)(cfg, in_channels, mask_classification)
```

三个参数各有来头：

| 参数 | 来源 | 含义 |
| --- | --- | --- |
| `cfg` | 配置对象 | 全部超参的载体，由解码器自己的 `from_config` 展开消费 |
| `in_channels` | `MaskFormerHead.from_config` 依据 `TRANSFORMER_IN_FEATURE` 推导 | 解码器输入特征的通道数，决定是否需要 1×1 投影 |
| `mask_classification` | 调用点硬编码 `True` | 是否启用 mask classification 范式（分类头 + mask 头）；v2 解码器直接 `assert` 只支持 `True` |

`in_channels` 为什么不能由解码器自己从 cfg 读？因为像素解码器/骨干的选择决定了送进解码器的特征通道，这个信息在 `MaskFormerHead.from_config` 手里（它同时构建两个子模块，必须保证对齐）。这正是三参签名的由来。

#### 4.2.2 核心流程

完整调用链（自底向上）：

```
detectron2: build_model(cfg)
  └─ META_ARCH_REGISTRY.get("MaskFormer")(cfg)                ← MaskFormer.from_config
       └─ build_sem_seg_head(cfg, backbone.output_shape())    ← detectron2 官方工厂
            └─ SEM_SEG_HEADS_REGISTRY.get("MaskFormerHead")(cfg, input_shape)
                 └─ MaskFormerHead.from_config:
                      ① 按 TRANSFORMER_IN_FEATURE 四路分派推导 in_channels
                      ② build_pixel_decoder(cfg, input_shape)
                      ③ build_transformer_decoder(cfg, in_channels, mask_classification=True)
                           └─ TRANSFORMER_DECODER_REGISTRY.get(name)(cfg, in_channels, True)
                                └─ @configurable 检测到 CfgNode → 调解码器自己的 from_config
                                     → 展开成 kwargs → 执行真正 __init__
```

`in_channels` 的四路分派（v2 默认走第三条）：

| `TRANSFORMER_IN_FEATURE` | in_channels 取值 | 语义 |
| --- | --- | --- |
| `"transformer_encoder"` | `SEM_SEG_HEAD.CONVS_DIM` | 用 TransformerEncoderPixelDecoder 的 encoder 输出（v1 用法） |
| `"pixel_embedding"` | `SEM_SEG_HEAD.MASK_DIM` | 直接用 1/4 mask_features（v1 用法） |
| `"multi_scale_pixel_decoder"` | `SEM_SEG_HEAD.CONVS_DIM` | 用多尺度特征 list（v2 默认） |
| 其他（如 `"res5"`） | 该 backbone 特征的 channels | 直接吃骨干某一层（v1 用法） |

#### 4.2.3 源码精读

[mask2former/modeling/transformer_decoder/maskformer_transformer_decoder.py:L22-L27](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/maskformer_transformer_decoder.py#L22-L27) —— 工厂函数本体：读 `cfg.MODEL.MASK_FORMER.TRANSFORMER_DECODER_NAME`，`get(name)` 查表，紧接着 `(cfg, in_channels, mask_classification)` 三参调用。整个函数就这么多——所有复杂度都被推给了注册表与 `@configurable`。

[mask2former/modeling/meta_arch/mask_former_head.py:L88-L97](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/mask_former_head.py#L88-L97) —— `MaskFormerHead.from_config` 推导 `transformer_predictor_in_channels` 的四路 if-elif（表见上）。v2 默认的 `multi_scale_pixel_decoder` 取 `CONVS_DIM`（默认 256）。

[mask2former/modeling/meta_arch/mask_former_head.py:L108-L112](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/mask_former_head.py#L108-L112) —— 唯一调用点：`build_transformer_decoder(cfg, transformer_predictor_in_channels, mask_classification=True)`，产物作为 `transformer_predictor` 注入。注意 `mask_classification=True` 在这里**硬编码**——想跑 per-pixel 对照基线要走 u8-l4 的 `PerPixelBaselineHead`，而不是把这个参数改成 False。

[mask2former/modeling/meta_arch/mask_former_head.py:L118-L121](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/mask_former_head.py#L118-L121) —— 前向时的交接：`multi_scale_pixel_decoder` 模式下，`self.predictor(multi_scale_features, mask_features, mask)` 三个实参原样传给解码器的 `forward`。这定死了 forward 的输入契约（见 4.3）。

[mask2former/config.py:L50](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L50) —— `TRANSFORMER_IN_FEATURE` 默认 `"res5"`；v2 配置文件里显式覆盖为 `"multi_scale_pixel_decoder"`。

#### 4.2.4 代码实践

**实践目标**：验证「改错名字立刻炸、错误发生在查表时」，从而理解名字的解析时机。

**操作步骤**（示例代码）：

```python
# bad_name.py —— 示例代码
from detectron2.config import get_cfg
from detectron2.projects.deeplab import add_deeplab_config
from mask2former import add_maskformer2_config
from mask2former.modeling.transformer_decoder.maskformer_transformer_decoder import (
    build_transformer_decoder,
)

cfg = get_cfg()
add_deeplab_config(cfg)
add_maskformer2_config(cfg)
cfg.MODEL.MASK_FORMER.TRANSFORMER_DECODER_NAME = "MyDecoder"   # 故意写一个没注册的名字
cfg.MODEL.MASK_FORMER.TRANSFORMER_IN_FEATURE = "multi_scale_pixel_decoder"
try:
    build_transformer_decoder(cfg, in_channels=256)
except Exception as e:
    print(type(e).__name__, ":", e)
```

**需要观察的现象**：抛出 `KeyError`（detectron2 的 `Registry.get` 查不到名字时的行为，错误信息会列出当前注册表里可用的名字——具体文案以本地 detectron2 版本为准）。

**预期结果**：确认报错发生在 `build_transformer_decoder` 内部的查表一步，而不是模型前向时——名字解析是构建期行为，配置写错名字不会潜伏到运行期。**运行结果：待本地验证。**

#### 4.2.5 小练习与答案

**练习 1**：`build_transformer_decoder` 的注释写着「from `cfg.MODEL.INS_EMBED_HEAD.NAME`」（v1 时代的笔误）。实际读的是哪个键？这个「注释与代码不一致」说明什么？

**答案**：实际读 `cfg.MODEL.MASK_FORMER.TRANSFORMER_DECODER_NAME`（[maskformer_transformer_decoder.py:L26](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/maskformer_transformer_decoder.py#L26)）。说明这段代码是从 MaskFormer 前身（instance embedding 分支）演化来的，docstring 没跟上重构——读源码时以代码为准、注释为线索，是常态。

**练习 2**：如果我想让解码器在 `in_channels != hidden_dim` 时自动加 1×1 投影，这个判断应该写在解码器里的什么位置？为什么不能写在 `build_transformer_decoder` 里？

**答案**：写在解码器自己的 `__init__` 里（参照 [mask2former_transformer_decoder.py:L325-L329](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L325-L329)：`if in_channels != hidden_dim or enforce_input_project: Conv2d(...) else: nn.Sequential()`）。工厂函数是所有解码器共用的公共入口，不该掺入某个解码器的私有超参逻辑；`hidden_dim` 是解码器自己的配置项，只有解码器知道两者的关系。

---

### 4.3 from_config 签名约定与 forward 的输入输出契约

#### 4.3.1 概念说明

自定义解码器要遵守**三条契约**，缺一条都会在构建期或训练期炸掉：

**契约一：`from_config(cls, cfg, in_channels, mask_classification)` 签名。**
`@configurable` 装饰的 `__init__` 收到 `(cfg, in_channels, mask_classification)` 时，会把 `cfg` 之外的多余位置参数**原样转发**给 `from_config`。也就是说调用点传了两个额外位置参数，`from_config` 的形参表就必须恰好接得住它们。返回值是一个 dict，其键必须是 `__init__` 形参名的子集（`@configurable` 会把 dict 解包成 `cls(**kwargs)`）。

**契约二：`forward(x, mask_features, mask=None)` 输入。**
v2 默认模式下 `x` 是长度为 3 的多尺度特征 list（1/32、1/16、1/8，通道 `CONVS_DIM`），`mask_features` 是 1/4 分辨率逐像素嵌入（通道 `MASK_DIM`），`mask` 是 padding 掩码（官方 v2 直接 `del mask` 忽略）。你的解码器可以只用其中一部分尺度（本讲的迷你版只用 x[0]），但**形参必须摆够三个**，因为调用方按位置传参。

**契约三：输出 dict 含 `pred_logits`、`pred_masks`，深监督时含 `aux_outputs`。**
- `pred_logits`：`(B, Q, K+1)`，最后一维是带 no-object 通道的类别 logits；
- `pred_masks`：`(B, Q, H/4, W/4)`，与 `mask_features` 同分辨率的 mask logits；
- `aux_outputs`：list，每项是 `{"pred_logits": ..., "pred_masks": ...}`。

三条契约分别对应三个消费方（见 4.4）。

#### 4.3.2 核心流程

`@configurable` 的展开过程（以自定义解码器为例）：

```
调用: MiniMaskedTransformerDecoder(cfg, 256, True)
  ① @configurable 检测到第 1 个位置参数是 CfgNode
  ② 调 cls.from_config(cfg, 256, True)          ← 签名约定在此生效
  ③ 得 dict: {"in_channels": 256, "mask_classification": True,
              "num_classes": 133, "hidden_dim": 256, ...}
  ④ 真正 __init__(**kwargs) 执行
```

深监督的数量对齐关系（`DEC_LAYERS = N` 时）：

\[
N_{\text{预测组}} = N_{\text{实建层}} + 1 = N, \qquad N_{\text{aux}} = N - 1
\]

即：`from_config` 里 `dec_layers = DEC_LAYERS - 1`（可学习 `query_feat` 在进第 1 层之前先产出第 0 组预测，见 u4-l3），forward 收集 `num_layers + 1` 组预测，最后一组进主键、前面 `num_layers` 组进 `aux_outputs`（长度 `num_layers`，恰为 `DEC_LAYERS - 1`），与 `MaskFormer.from_config` 生成的 `_0.._{N-2}` 后缀 weight_dict 一一对应。

`from_config` 能读的全部常用 cfg 键：

| cfg 键 | 用途 |
| --- | --- |
| `MODEL.SEM_SEG_HEAD.NUM_CLASSES` | 分类头输出 `K+1` 的 K |
| `MODEL.SEM_SEG_HEAD.MASK_DIM` | mask 嵌入输出维度（与 mask_features 通道对齐） |
| `MODEL.MASK_FORMER.HIDDEN_DIM` | 解码器隐层维度 |
| `MODEL.MASK_FORMER.NUM_OBJECT_QUERIES` | query 数 Q |
| `MODEL.MASK_FORMER.NHEADS` | 注意力头数 |
| `MODEL.MASK_FORMER.DIM_FEEDFORWARD` | FFN 中间维度 |
| `MODEL.MASK_FORMER.DEC_LAYERS` | 解码层数（from_config 内减 1） |
| `MODEL.MASK_FORMER.PRE_NORM` | pre/post-norm 开关 |
| `MODEL.MASK_FORMER.ENFORCE_INPUT_PROJ` | 强制加输入投影 |

#### 4.3.3 源码精读

[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:L336-L361](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L336-L361) —— `MultiScaleMaskedTransformerDecoder.from_config`，自定义解码器的标准模板：前两行把 `in_channels` 与 `mask_classification` 原样塞回返回 dict（它们不是 cfg 里的键，而是调用点传来的位置参数）；随后逐键从 cfg 取值；`mask_dim` 来自 `SEM_SEG_HEAD` 而非 `MASK_FORMER`，容易拿错节点。

[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:L349-L355](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L349-L355) —— `DEC_LAYERS - 1` 的权威注释：因为可学习 query features 本身需要一组监督，辅助损失组数要恒等于解码层数，所以配置值减 1。**自定义解码器强烈建议沿用同一约定**，否则与 `MaskFormer.from_config` 生成的 weight_dict 后缀对不上。

[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:L235-L250](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L235-L250) —— `@configurable` 装饰的 `__init__` 形参表：`in_channels` 与 `mask_classification` 是普通位置参数，`*` 之后全部关键字参数。from_config 返回的 dict 键与这些名字一一对应。

[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:L363-L371](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L363-L371) —— forward 开头：`assert len(x) == self.num_feature_levels`（官方实现硬要求 3 尺度；迷你版可以不 assert、只取 `x[0]`），随后 `del mask` 显式忽略 padding 掩码。

[mask2former/modeling/transformer_decoder/maskformer_transformer_decoder.py:L107-L128](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/maskformer_transformer_decoder.py#L107-L128) —— v1 `StandardTransformerDecoder.from_config`：签名同样是 `(cls, cfg, in_channels, mask_classification)`，证明该签名是跨版本稳定的约定，不是 v2 的巧合。

[mask2former/modeling/transformer_decoder/maskformer_transformer_decoder.py:L130-L132](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/maskformer_transformer_decoder.py#L130-L132) —— v1 forward 对 `mask` 的用法与 v2 相反：v1 真的把 padding 掩码插值后用于位置编码。说明 `mask` 参数是「可用可不用」的自由度，但**形参必须保留**。

[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:L140-L142](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/maskformer_transformer_decoder.py#L140-L142) —— v1 的 `mask_classification=False` 分支：输出 dict 可以没有 `pred_logits`（对应 per-pixel 式用法）。但 v2 直接 `assert mask_classification`（[mask2former_transformer_decoder.py:L270](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L270)），本讲的迷你版跟随 v2。

#### 4.3.4 代码实践

**实践目标**：在不实例化模型的情况下，手动调用 `from_config`，看它把一份真实配置展开成什么。

**操作步骤**（示例代码）：

```python
# peek_from_config.py —— 示例代码
from detectron2.config import get_cfg
from detectron2.projects.deeplab import add_deeplab_config
from mask2former import add_maskformer2_config
from mask2former.modeling.transformer_decoder.mask2former_transformer_decoder import (
    MultiScaleMaskedTransformerDecoder,
)

cfg = get_cfg()
add_deeplab_config(cfg)
add_maskformer2_config(cfg)
cfg.merge_from_file("configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml")
cfg.MODEL.MASK_FORMER.DEC_LAYERS = 3   # 覆盖成小值，观察 dec_layers 的展开

params = MultiScaleMaskedTransformerDecoder.from_config(cfg, in_channels=256, mask_classification=True)
for k, v in params.items():
    print(f"{k:>22} = {v}")
```

**需要观察的现象**：`dec_layers` 打印 `2`（= 3 - 1）、`num_classes` 打印 `133`、`mask_dim` 打印 `256`、`enforce_input_project` 打印 `False`。

**预期结果**：确认 `DEC_LAYERS - 1` 的减法发生在 `from_config` 内部；确认 `num_classes`/`mask_dim` 取自 `SEM_SEG_HEAD` 节点而非 `MASK_FORMER` 节点。**运行结果：待本地验证。**

#### 4.3.5 小练习与答案

**练习 1**：如果把自定义解码器的 `from_config` 写成 `(cls, cfg)`（丢掉后两个参数），会在哪一步、报什么错？

**答案**：在构建期报错。`@configurable` 把调用点的 `(cfg, in_channels, mask_classification)` 转发给 `from_config` 时，多出的两个位置参数没有形参接收，抛 `TypeError`（参数个数不匹配）。这正是「签名约定」的强制力所在——它不是文档约定，而是会被 Python 参数绑定机制硬性检查的接口。

**练习 2**：迷你解码器只想用一个尺度，能不能把 `forward` 写成 `def forward(self, x0, mask_features)`？

**答案**：不能。调用方 [mask_former_head.py:L121](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/mask_former_head.py#L121) 是按位置传三参 `self.predictor(multi_scale_features, mask_features, mask)`——`multi_scale_features` 这个 list 会绑到 `x0`，`mask_features` 绑到 `mask_features`，而 `mask` 没有形参接收，直接 `TypeError`。正确做法是保留三参签名 `forward(self, x, mask_features, mask=None)`，在函数体内只用 `x[0]`。

**练习 3**：为什么 `num_classes` 要从 `SEM_SEG_HEAD.NUM_CLASSES` 取，而 `hidden_dim` 从 `MASK_FORMER.HIDDEN_DIM` 取？两个节点各自管什么？

**答案**：沿用 u1-l5 的分工：`MODEL.SEM_SEG_HEAD` 管「头」的结构参数（类别数、mask 维度、像素解码器），`MODEL.MASK_FORMER` 管 meta arch 与 Transformer 解码器行为（隐层维度、query 数、损失权重、任务开关）。类别数属于数据集/头层面的属性，所以放 `SEM_SEG_HEAD`；`hidden_dim` 是解码器内部宽度，所以放 `MASK_FORMER`。分类头输出维度是 `num_classes + 1`（no-object 通道）。

---

### 4.4 输出 dict 契约：pred_logits / pred_masks / aux_outputs 的三个消费方

#### 4.4.1 概念说明

解码器返回的 dict 会被三方按不同方式消费，这决定了哪些键「必须有」：

| 消费方 | 位置 | 用到的键 | 缺键后果 |
| --- | --- | --- | --- |
| `SetCriterion`（训练） | criterion.py `forward` | `pred_logits`、`pred_masks`、`aux_outputs`（可选） | 前两者有 `assert`，构建后第一次前向直接断言失败 |
| `MaskFormer.forward` 推理分支 | maskformer_model.py | `pred_logits`、`pred_masks` | `KeyError` |
| `MaskFormer.forward` 加权过滤 | maskformer_model.py | 损失 dict 键 ∩ `weight_dict` | 不在 `weight_dict` 的损失键被丢弃 |

两个容易踩的坑：

1. **`aux_outputs` 是可选的但强烈建议有**。criterion 只在 `"aux_outputs" in outputs` 时才算辅助损失；深监督关闭（`DEEP_SUPERVISION: False`）时 `weight_dict` 没有 aux 键，解码器多产出的 aux 损失会在加权阶段被整体丢弃（算力浪费但不出错）。
2. **aux 条数与 `_i` 后缀必须对齐才能吃到权重**。`MaskFormer.from_config` 为 `range(DEC_LAYERS - 1)` 生成 `_0.._{DEC_LAYERS-2}` 后缀键；解码器 `aux_outputs` 的第 i 项在 criterion 里自动加 `_i` 后缀。条数一致时全部损失有权重；解码器少给则部分权重键永远空置，多给则多出的损失算完被丢。

#### 4.4.2 核心流程

训练期一次损失的完整流转：

```
decoder 返回 out = {pred_logits, pred_masks, aux_outputs: [ {…}, {…}, … ]}
  │
  ├─ criterion(out, targets)
  │    ① 剥离 aux_outputs → outputs_without_aux
  │    ② matcher 对主输出做匈牙利匹配
  │    ③ loss_labels / loss_masks 产出 loss_ce / loss_mask / loss_dice
  │    ④ 对每组 aux_outputs：重新匹配、重算损失、键加 "_i" 后缀
  │    返回未加权损失 dict（3 + 3×N_aux 个键）
  │
  └─ MaskFormer.forward 训练分支：
       for k in losses: 在 weight_dict 则乘权重，否则 pop 丢弃
       返回最终损失 dict（由 detectron2 求和反传）
```

#### 4.4.3 源码精读

[mask2former/modeling/criterion.py:L126-L127](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py#L126-L127) 与 [mask2former/modeling/criterion.py:L144](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py#L144) —— 两个硬断言：`assert "pred_logits" in outputs`、`assert "pred_masks" in outputs`。自定义解码器缺任何一个键，第一次训练前向就断言失败，错误信息直指键名。

[mask2former/modeling/criterion.py:L219-L222](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py#L219-L222) —— 主输出的处理：先把 `aux_outputs` 从 dict 里剥离再送匹配器（匹配器只看主输出）。

[mask2former/modeling/criterion.py:L239-L245](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py#L239-L245) —— aux 循环：`if "aux_outputs" in outputs` 说明该键可选；每组 aux 独立重新匹配（`indices = self.matcher(aux_outputs, targets)`），损失键加 `_i` 后缀。**后缀由 criterion 自动生成，解码器不要自己加**。

[mask2former/maskformer_model.py:L198-L217](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L198-L217) —— 训练分支：`outputs = self.sem_seg_head(features)` 拿到解码器 dict，`self.criterion(outputs, targets)` 算损失，然后逐键与 `weight_dict` 求交集——在则乘权重、不在则 `pop` 丢弃。

[mask2former/maskformer_model.py:L219-L220](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L219-L220) —— 推理分支只取两键：`outputs["pred_logits"]` 与 `outputs["pred_masks"]`，随后把 mask 上采样到 padding 尺寸再进三条推理后处理（u6 系列）。

[mask2former/maskformer_model.py:L118-L125](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L118-L125) —— `weight_dict` 的生成处：3 个基础键，`DEEP_SUPERVISION` 为真时为 `range(dec_layers - 1)` 展开 `_0.._{N-2}` 后缀（DEC_LAYERS=10 时共 30 键，即 u2-l1 观察到的 30 个 loss 键的来源）。

[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:L424-L431](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L424-L431) —— 官方输出 dict 的组装：最后一组预测进 `pred_logits`/`pred_masks`，前面的组经 `_set_aux_loss` 打包进 `aux_outputs`。

[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:L450-L461](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L450-L461) —— `_set_aux_loss`：把 `[:-1]` 的中间组打包成 `[{"pred_logits": a, "pred_masks": b}, ...]`；`@torch.jit.unused` 只是 TorchScript 兼容标记，自定义解码器可以不写、直接列表推导。

#### 4.4.4 代码实践

**实践目标**：绕开整个模型，直接用手工构造的输出 dict 喂 `SetCriterion`，验证键名与后缀体系。

**操作步骤**（示例代码）：

```python
# peek_criterion.py —— 示例代码
import torch
from mask2former.modeling.criterion import SetCriterion
from mask2former.modeling.matcher import HungarianMatcher

K, Q, B = 133, 100, 2
matcher = HungarianMatcher(cost_class=2.0, cost_mask=5.0, cost_dice=5.0, num_points=12544)
weight_dict = {"loss_ce": 2.0, "loss_mask": 5.0, "loss_dice": 5.0}
for i in range(2):  # 模拟 DEC_LAYERS=3 → 2 组 aux 后缀
    weight_dict.update({k + f"_{i}": v for k, v in list(weight_dict.items())[:3]})
criterion = SetCriterion(K, matcher=matcher, weight_dict=weight_dict,
                         eos_coef=0.1, losses=["labels", "masks"],
                         num_points=12544, oversample_ratio=3.0, importance_sample_ratio=0.75)

H = W = 128  # GT mask 分辨率（点采样用归一化坐标，与 1/4 预测直接对采）
outputs = {
    "pred_logits": torch.randn(B, Q, K + 1),
    "pred_masks": torch.randn(B, Q, H // 4, W // 4),
    "aux_outputs": [
        {"pred_logits": torch.randn(B, Q, K + 1), "pred_masks": torch.randn(B, Q, H // 4, W // 4)}
        for _ in range(2)
    ],
}
targets = [
    {"labels": torch.tensor([0, 5]), "masks": torch.rand(2, H, W) > 0.7},
    {"labels": torch.tensor([3]), "masks": torch.rand(1, H, W) > 0.8},
]
losses = criterion(outputs, targets)
print(sorted(losses.keys()))
```

**需要观察的现象**：打印出的键应为 9 个：`loss_ce`、`loss_dice`、`loss_mask` 及其 `_0`、`_1` 后缀版本。

**预期结果**：确认（a）criterion 不关心 dict 里有没有别的键，只 assert 这三个；（b）后缀是 criterion 加的、与 weight_dict 的 `_i` 天然对齐；（c）targets 只需要 `labels`（连续类别 id）与 `masks`（全分辨率二值图）两个键——这正是 `MaskFormer.prepare_targets` 的产出格式（[mask2former/maskformer_model.py:L264-L278](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L264-L278)）。**运行结果：待本地验证。**

#### 4.4.5 小练习与答案

**练习 1**：自定义解码器返回了 5 组 aux_outputs，但配置 `DEC_LAYERS=3`（weight_dict 只有 `_0`、`_1`）。会发生什么？

**答案**：不报错。criterion 会算出 3 + 3×5 = 18 个损失键，但 `MaskFormer.forward` 加权时只保留 `weight_dict` 里的 9 个键，多出的 `_2`、`_3`、`_4` 组被 `pop` 丢弃。代价是白白多算 3 次匹配与损失——功能正确、算力浪费，务必让条数对齐。

**练习 2**：深监督配置 `DEEP_SUPERVISION: False` 时，解码器还要不要产出 `aux_outputs`？

**答案**：产出也不出错：criterion 照算 aux 损失，加权阶段因 weight_dict 无后缀键而全部丢弃（对应 u5-l3 的结论「深监督关闭时辅助损失照算、只在加权阶段被丢弃」）。但最干净的做法是像 v1 `StandardTransformerDecoder` 那样用 `self.aux_loss` 开关控制是否打包 aux——既省算力又让输出 dict 语义与配置一致。

**练习 3**：`pred_masks` 的空间分辨率必须是多少？由谁决定？

**答案**：与 `mask_features` 一致（默认 1/4 输入分辨率、`MASK_DIM` 通道的逐像素嵌入做点积对象）。官方实现经 `einsum("bqc,bchw->bqhw", mask_embed, mask_features)` 得到（[mask2former_transformer_decoder.py:L438](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L438)），分辨率跟随 `mask_features` 自动确定，无需解码器操心；训练时 criterion 用归一化坐标点采样，1/4 预测与全分辨率 GT 直接对齐（u5-l2），推理时由 `MaskFormer` 统一上采样（u6-l1）。

## 5. 综合实践

把三个契约串起来：实现 **`MiniMaskedTransformerDecoder`（2 层解码层、单尺度、无 masked attention 的精简版）**，注册进项目注册表，改一行配置接入完整模型，跑通「假数据 → 前向 → 损失」全链路。

### 5.1 第一步：写解码器文件

在 `mask2former/modeling/transformer_decoder/` 下新建 `mini_masked_transformer_decoder.py`（示例代码，非仓库原有文件）：

```python
# mask2former/modeling/transformer_decoder/mini_masked_transformer_decoder.py
# 示例代码：本讲综合实践，精简版解码器（2 层、单尺度、无 masked attention）
import torch
from torch import nn

from detectron2.config import configurable
from detectron2.layers import Conv2d

from .maskformer_transformer_decoder import TRANSFORMER_DECODER_REGISTRY
from .mask2former_transformer_decoder import (
    SelfAttentionLayer, CrossAttentionLayer, FFNLayer, MLP,
)
from .position_encoding import PositionEmbeddingSine


@TRANSFORMER_DECODER_REGISTRY.register()
class MiniMaskedTransformerDecoder(nn.Module):

    @configurable
    def __init__(
        self,
        in_channels,                 # ← 契约一：调用点传入，来自 head 的推导
        mask_classification=True,    # ← 契约一：调用点硬编码 True
        *,
        num_classes: int,
        hidden_dim: int,
        num_queries: int,
        nheads: int,
        dim_feedforward: int,
        dec_layers: int,
        pre_norm: bool,
        mask_dim: int,
        enforce_input_project: bool,
    ):
        super().__init__()
        assert mask_classification, "Only support mask classification model"
        self.mask_classification = mask_classification
        self.num_heads = nheads
        self.num_layers = dec_layers          # from_config 已做过 -1，这就是"实建层数"
        self.num_queries = num_queries

        N_steps = hidden_dim // 2
        self.pe_layer = PositionEmbeddingSine(N_steps, normalize=True)

        # 每层三件套：cross → self → FFN（直接复用官方积木，见 u4-l2）
        self.cross_attn_layers = nn.ModuleList()
        self.self_attn_layers = nn.ModuleList()
        self.ffn_layers = nn.ModuleList()
        for _ in range(self.num_layers):
            self.cross_attn_layers.append(CrossAttentionLayer(
                d_model=hidden_dim, nhead=nheads, dropout=0.0, normalize_before=pre_norm))
            self.self_attn_layers.append(SelfAttentionLayer(
                d_model=hidden_dim, nhead=nheads, dropout=0.0, normalize_before=pre_norm))
            self.ffn_layers.append(FFNLayer(
                d_model=hidden_dim, dim_feedforward=dim_feedforward,
                dropout=0.0, normalize_before=pre_norm))

        self.decoder_norm = nn.LayerNorm(hidden_dim)

        # 双查询嵌入：query_feat=内容，query_embed=位置（沿用官方设计，见 u4-l3）
        self.query_feat = nn.Embedding(num_queries, hidden_dim)
        self.query_embed = nn.Embedding(num_queries, hidden_dim)

        # 单尺度输入投影；默认配置 in_channels == hidden_dim 时为空 Sequential
        if in_channels != hidden_dim or enforce_input_project:
            self.input_proj = Conv2d(in_channels, hidden_dim, kernel_size=1)
        else:
            self.input_proj = nn.Sequential()

        # 契约三：分类头（K+1 含 no-object）+ mask 嵌入头
        self.class_embed = nn.Linear(hidden_dim, num_classes + 1)
        self.mask_embed = MLP(hidden_dim, hidden_dim, mask_dim, 3)

    @classmethod
    def from_config(cls, cfg, in_channels, mask_classification):
        # 契约一：签名必须与官方完全一致
        ret = {
            "in_channels": in_channels,
            "mask_classification": mask_classification,
            "num_classes": cfg.MODEL.SEM_SEG_HEAD.NUM_CLASSES,
            "hidden_dim": cfg.MODEL.MASK_FORMER.HIDDEN_DIM,
            "num_queries": cfg.MODEL.MASK_FORMER.NUM_OBJECT_QUERIES,
            "nheads": cfg.MODEL.MASK_FORMER.NHEADS,
            "dim_feedforward": cfg.MODEL.MASK_FORMER.DIM_FEEDFORWARD,
            "pre_norm": cfg.MODEL.MASK_FORMER.PRE_NORM,
            "enforce_input_project": cfg.MODEL.MASK_FORMER.ENFORCE_INPUT_PROJ,
            "mask_dim": cfg.MODEL.SEM_SEG_HEAD.MASK_DIM,
        }
        assert cfg.MODEL.MASK_FORMER.DEC_LAYERS >= 1
        ret["dec_layers"] = cfg.MODEL.MASK_FORMER.DEC_LAYERS - 1  # 沿用官方约定
        return ret

    def forward(self, x, mask_features, mask=None):
        # 契约二：三参形参摆够；x 是长度 3 的 list（1/32, 1/16, 1/8）
        del mask                                # 与官方一致：忽略 padding 掩码
        x0 = x[0]                               # 单尺度：只取最粗的 1/32

        src = self.input_proj(x0).flatten(2).permute(2, 0, 1)      # (HW, B, C)
        pos = self.pe_layer(x0, None).flatten(2).permute(2, 0, 1)  # (HW, B, C)

        _, bs, _ = src.shape
        query_pos = self.query_embed.weight.unsqueeze(1).repeat(1, bs, 1)  # (Q, B, C)
        output = self.query_feat.weight.unsqueeze(1).repeat(1, bs, 1)      # (Q, B, C)

        predictions_class, predictions_mask = [], []

        # 第 0 组预测：可学习 query_feat 直接过预测头（深监督要求，见 u5-l3）
        cls, msk = self.forward_prediction_heads(output, mask_features)
        predictions_class.append(cls)
        predictions_mask.append(msk)

        # 主循环：cross → self → FFN，每层末尾立刻预测（层序与官方一致）
        for i in range(self.num_layers):
            output = self.cross_attn_layers[i](
                output, src,
                memory_mask=None,               # 简化点：不做 masked attention
                memory_key_padding_mask=None,
                pos=pos, query_pos=query_pos)
            output = self.self_attn_layers[i](
                output, tgt_mask=None, tgt_key_padding_mask=None, query_pos=query_pos)
            output = self.ffn_layers[i](output)
            cls, msk = self.forward_prediction_heads(output, mask_features)
            predictions_class.append(cls)
            predictions_mask.append(msk)

        # 契约三：输出 dict 三键
        return {
            "pred_logits": predictions_class[-1],                  # (B, Q, K+1)
            "pred_masks": predictions_mask[-1],                    # (B, Q, H/4, W/4)
            "aux_outputs": [                                       # 前面 num_layers 组
                {"pred_logits": a, "pred_masks": b}
                for a, b in zip(predictions_class[:-1], predictions_mask[:-1])
            ],
        }

    def forward_prediction_heads(self, output, mask_features):
        decoder_output = self.decoder_norm(output)
        decoder_output = decoder_output.transpose(0, 1)        # (Q, B, C) → (B, Q, C)
        outputs_class = self.class_embed(decoder_output)       # (B, Q, K+1)
        mask_embed = self.mask_embed(decoder_output)           # (B, Q, mask_dim)
        outputs_mask = torch.einsum("bqc,bchw->bqhw", mask_embed, mask_features)
        return outputs_class, outputs_mask
```

与官方 `MultiScaleMaskedTransformerDecoder` 相比刻意去掉了三样东西，各自对应前面某讲的内容：

| 删掉的 | 原因与影响 |
| --- | --- |
| masked attention（`memory_mask=None`） | u4-l3/u4-l4 的核心创新，但纯可选——注意力掩码传 None 就退化成普通 cross-attention |
| `level_embed` 与三尺度轮转 | 只用 1/32 单尺度，无需尺度身份注入 |
| attention mask 的下采样与 `attn_mask` 维护 | 没有掩码就不需要为下一层准备掩码 |

### 5.2 第二步：注册（两种方式任选）

- **方式 A（免改源码，推荐练习用）**：不动仓库任何文件，在测试脚本里显式 import 触发注册：

  ```python
  import mask2former.modeling.transformer_decoder.mini_masked_transformer_decoder  # noqa: F401
  ```

- **方式 B（长期使用）**：在 [mask2former/modeling/transformer_decoder/__init__.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/__init__.py) 末尾加一行同款 import（与 L2-L3 两行官方 import 并列），之后任何 `import mask2former` 都会带上你的解码器。

### 5.3 第三步：改配置 + 跑前向 + 算损失

在仓库根目录写测试脚本（示例代码）：

```python
# mini_decoder_demo.py —— 示例代码
import torch
from detectron2.config import get_cfg
from detectron2.modeling import build_model
from detectron2.projects.deeplab import add_deeplab_config
from detectron2.structures import Instances, BitMasks

from mask2former import add_maskformer2_config
import mask2former.modeling.transformer_decoder.mini_masked_transformer_decoder  # noqa: F401  ← 方式 A 注册

cfg = get_cfg()
add_deeplab_config(cfg)
add_maskformer2_config(cfg)
cfg.merge_from_file("configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml")
cfg.merge_from_list([
    "MODEL.MASK_FORMER.TRANSFORMER_DECODER_NAME", "MiniMaskedTransformerDecoder",  # ← 唯一必改项
    "MODEL.MASK_FORMER.DEC_LAYERS", 3,        # 3 组预测 = 2 层解码器 + 1 组 query_feat 预测
    "MODEL.SEM_SEG_HEAD.PIXEL_DECODER_NAME", "BasePixelDecoder",  # 可选：纯 CPU 也无需跑 MSDeformAttn
    "MODEL.DEVICE", "cpu",
])
cfg.freeze()

model = build_model(cfg)
model.train()
print(type(model.sem_seg_head.predictor).__name__)   # 应打印 MiniMaskedTransformerDecoder

# 假数据：1 张 128×128 图、2 个实例（128 是 SIZE_DIVISIBILITY=32 的倍数）
H = W = 128
instances = Instances((H, W))
instances.gt_classes = torch.tensor([0, 5])                  # 连续类别 id，须 < NUM_CLASSES=133
instances.gt_masks = BitMasks(torch.rand(2, H, W) > 0.7)
batched_inputs = [{
    "image": torch.randint(0, 255, (3, H, W), dtype=torch.uint8),
    "instances": instances,
}]

losses = model(batched_inputs)                              # 前向 + 匹配 + 损失一次完成
for k in sorted(losses.keys()):
    print(f"{k:>16} = {losses[k].item():.4f}")
```

**需要观察的现象**：

1. `build_model` 成功，predictor 类型打印 `MiniMaskedTransformerDecoder`——证明注册 + 查表 + `@configurable` 三关全过。
2. 损失 dict 恰好 9 个键：`loss_ce` / `loss_mask` / `loss_dice` 与 `loss_ce_0` / `loss_mask_0` / `loss_dice_0` / `loss_ce_1` / `loss_mask_1` / `loss_dice_1`——DEC_LAYERS=3 时主输出 3 键 + 2 组 aux 各 3 键，且全部落在 weight_dict 内（yaml 中 `DEEP_SUPERVISION: True`）。
3. 所有损失值都是有限标量（无 NaN）。
4. 把 `DEC_LAYERS` 改成 2 再跑：实建层数变为 1，损失键变成 6 个。

**预期结果**：如上 4 条。第 0 组预测来自未训练的 `query_feat`，损失数值本身无意义，本实践只验证「接口与流程跑通」。**运行结果：待本地验证。**

**环境提示**：

- `import mask2former` 传递性地加载 MSDeformAttn 算子模块，**即使配置里换成 BasePixelDecoder**，u1-l2 的算子编译（含无 GPU 机器的 `FORCE_CUDA=1` 构建）也是前置条件；换 BasePixelDecoder 只是让前向不执行可变形注意力，CPU 上更快更稳。
- `build_model` 会经 `MetadataCatalog.get(cfg.DATASETS.TRAIN[0])` 取 COCO 元数据（[mask2former/maskformer_model.py:L147](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L147)）。注册在 import 时完成、不碰磁盘（u2-l2），因此**不需要下载 COCO 数据集**即可跑通本实践。

## 6. 本讲小结

- `TRANSFORMER_DECODER_REGISTRY` 是项目用 detectron2 的 `Registry` 类**自建**的注册表（内部名历史遗留为 `"TRANSFORMER_MODULE"`），与官方 `SEM_SEG_HEADS_REGISTRY` 命名空间互相独立；v1/v2 两个解码器跨文件共用这同一张表。
- `build_transformer_decoder(cfg, in_channels, mask_classification)` 是唯一公共入口：`in_channels` 由 `MaskFormerHead.from_config` 按 `TRANSFORMER_IN_FEATURE` 四路分派推导，`mask_classification` 在调用点硬编码 `True`。
- 自定义解码器的三条硬契约：`from_config(cls, cfg, in_channels, mask_classification)` 签名（配合 `@configurable`，多余位置参数被原样转发）；`forward(x, mask_features, mask=None)` 三参输入（x 为长度 3 的多尺度 list，可只用子集）；输出 dict 必含 `pred_logits` / `pred_masks`、建议含 `aux_outputs`。
- 数量对齐公式 \(N_{\text{预测组}} = N_{\text{实建层}} + 1 = \text{DEC\_LAYERS}\)：`from_config` 内减 1、`aux_outputs` 给 `DEC_LAYERS - 1` 条，才能与 weight_dict 的 `_0.._{N-2}` 后缀一一对应；条数不齐不会报错，但会浪费算力或空置权重。
- `aux_outputs` 的 `_i` 后缀由 criterion 自动添加并逐组重新匹配；`MaskFormer.forward` 按 `weight_dict` 加权并丢弃白名单外的键——这三方（解码器、criterion、meta arch）通过键名约定完成解耦协作。
- 新解码器文件**不会自动注册**：要么在包 `__init__.py` 加 import，要么在使用方显式 import，二者等价。

## 7. 下一步学习建议

- **u8-l4（PerPixelBaselineHead 对照基线）**：单元八的收尾讲，看同一像素解码器上「逐像素 1×1 分类器」的对照设计如何用 `mask_classification=False` 路径实现，理解 Mask2Former 论文如何隔离 mask classification 范式本身的收益。
- **重读 u10-l4（权重转换与版本兼容）**：`MultiScaleMaskedTransformerDecoder._load_from_state_dict`（[mask2former_transformer_decoder.py:L210-L233](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L210-L233)）会自动迁移 `static_query → query_feat`；若你的迷你解码器想复用官方预训练权重的 `class_embed` / `mask_embed`，需要理解这套 key 改写机制。
- **挑战方向**：给 `MiniMaskedTransformerDecoder` 加回 masked attention（对照 u4-l3/u4-l4 逐步补齐 `attn_mask` 的生成与轮转），或在真实小数据集（如 100 张 COCO 子集）上短训对比有无 masked attention 的收敛差异。
- **视频版对照**：u9-l2 的 `VideoMultiScaleMaskedTransformerDecoder` 是「继承同一套契约、把帧维度折进注意力序列」的完整范例，适合作为自定义解码器写完后的进阶阅读。

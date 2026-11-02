# u10-l4 权重转换与 Checkpoint 版本兼容

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 PyTorch 的 `_load_from_state_dict` 钩子在参数装载流水线中的位置，以及 `_version` 与 checkpoint metadata 的关系。
2. 逐行解释 Mask2Former 两处权重自动迁移逻辑：`MaskFormerHead` 的 `pixel_decoder.` 前缀迁移、`MultiScaleMaskedTransformerDecoder` 的 `static_query → query_feat` 改名——也就是 `_version = 2` 背后的完整故事。
3. 会使用 `tools/convert-torchvision-to-d2.py` 与 `tools/convert-pretrained-swin-model-to-d2.py`，并说出两者的本质区别：R50 脚本**改写 key**，Swin 脚本**零改写**、完全依赖 detectron2 的形状启发式匹配。
4. 理解 `DetectionCheckpointer` 的部分加载行为：为什么「只有 backbone 的第三方权重」能直接加载进完整模型而不报错。

本讲是单元十（工程化主题）的最后一篇，讨论一个纯工程但极常踩坑的问题：**代码在演进，旧权重怎么办**。

## 2. 前置知识

### 2.1 state_dict：一张「模块路径 → 张量」的字典

PyTorch 把模型所有可学习参数（和 BN 的 running stats）拍平成一个 `OrderedDict`，key 是参数在模块树中的完整路径。例如 Mask2Former 完整模型里：

```text
backbone.patch_embed.proj.weight                     # Swin 的 patch embedding
sem_seg_head.pixel_decoder.input_proj.0.0.weight     # 像素解码器的 1x1 投影
sem_seg_head.predictor.query_feat.weight             # 100 个可学习 query 的内容嵌入
```

**key 就是模块属性的嵌套路径**。这一点是本讲一切问题的根源：只要代码把某一层挪进（或挪出）一个子模块，key 就变了，旧 checkpoint 里按旧路径存放的张量就「找不到家」了。

### 2.2 模块重构如何弄坏权重兼容

MaskFormer（v1）→ Mask2Former（v2）做过两次影响 key 的重构：

| 重构 | v1 权重里的 key | v2 模型里的 key |
|---|---|---|
| FPN 各层从 `MaskFormerHead` 内联属性重构为 `pixel_decoder` 子模块 | `sem_seg_head.lateral_convs.0.*` | `sem_seg_head.pixel_decoder.lateral_convs.0.*` |
| 解码器可学习 query 的内容嵌入更名 | `sem_seg_head.predictor.static_query.*` | `sem_seg_head.predictor.query_feat.*` |

（v2 模型侧的 `query_feat` 定义见 [mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:L314-L318](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L314-L318)，这里同时定义了 `query_feat`（内容嵌入）与 `query_embed`（位置嵌入）两个可学习嵌入。）

mask classification 范式的训练成本很高（COCO 全景 50 epoch），作者显然不希望用户因为一次重构就重训模型——于是写了两段「加载时自动改写 key」的迁移代码，也就是本讲的主角。

### 2.3 三种权重来源，三种加载姿态

| 权重来源 | 文件内容特征 | 加载姿态 |
|---|---|---|
| 本项目自训 checkpoint（`.pth`） | 带 `model` 键，state_dict 自带 `_metadata`（记录各模块 version） | 正常加载；若版本旧于代码则触发钩子迁移 |
| torchvision ResNet（`.pth`） | 裸 state_dict，key 命名与 detectron2 不同 | 先跑转换脚本改写 key，再靠 `matching_heuristics` 对齐 |
| Swin 官方分类权重（`.pth`） | 包在 `"model"` 键下，参数名与本项目 swin.py 一致 | 零改写，直接包一层 pickle 元数据靠启发式对齐 |

「部分加载」指：checkpoint 里有而模型里没有的 key（多余 key）只记录不报错，模型里有而 checkpoint 里没有的参数（缺失 key）保持随机初始化。这正是「只用 backbone 预训练权重训练整个 Mask2Former」可行的原因。仓库内可见的加载入口例如 [train_net.py:L301-L305](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L301-L305)（`--eval-only` 分支用 `DetectionCheckpointer(...).resume_or_load(...)` 只载权重）与 [tools/analyze_model.py:L54-L62](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/analyze_model.py#L54-L62)（`DetectionCheckpointer(model).load(...)`）。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [mask2former/modeling/meta_arch/mask_former_head.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/mask_former_head.py) | 三段式组装器 `MaskFormerHead`；本讲关注其 `_version = 2` 与 `_load_from_state_dict`（前缀迁移） |
| [mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py) | v2 解码器 `MultiScaleMaskedTransformerDecoder`；本讲关注其同名钩子（`static_query` 改名）与 `query_feat` 定义 |
| [mask2former/modeling/transformer_decoder/maskformer_transformer_decoder.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/maskformer_transformer_decoder.py) | v1 解码器 `MaskedTransformerDecoder`，仅作对照：其中只有 `query_embed`，没有 `static_query` |
| [tools/convert-torchvision-to-d2.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/convert-torchvision-to-d2.py) | 把 torchvision ResNet 权重转成 detectron2 可加载的 `.pkl`，核心是一张 key 映射表 |
| [tools/convert-pretrained-swin-model-to-d2.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/convert-pretrained-swin-model-to-d2.py) | 把 Swin 官方分类权重包成 detectron2 `.pkl`，不改任何 key |
| [mask2former/modeling/backbone/swin.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/backbone/swin.py) | Swin 实现；用于解释「为什么 Swin 权重可以零改写对齐」 |

## 4. 核心概念与源码讲解

### 4.1 PyTorch 装载钩子：_load_from_state_dict 与 _version 机制

#### 4.1.1 概念说明

`nn.Module.load_state_dict()` 并不是简单地「逐 key 赋值」，而是先按模块树**递归调用每个子模块的 `_load_from_state_dict` 钩子，再由默认实现完成真正的参数拷贝**。这给框架代码留了一个正规扩展点：在参数装载发生**之前**，对传入的 state_dict 做原地修正——比如改写 key。

与之配套的是 `nn.Module` 的内置属性 `_version`（基类默认为 0）：

- **保存时**：`model.state_dict()` 会把每个子模块当前的 `_version` 记录到返回值的 `_metadata` 里（以模块前缀为 key）。
- **加载时**：`load_state_dict()` 从 checkpoint 的 `_metadata` 中取出该模块前缀对应的 version，装进 `local_metadata` 传给钩子。

于是钩子可以回答一个问题：「这份 checkpoint 是新版代码存的，还是旧版代码存的？」旧 checkpoint 里记录的 version 小于当前代码声明的 `_version`，说明权重格式过期，需要迁移；反之不动。若 checkpoint 根本没有 `_metadata`（比如手工构造的 state_dict），`local_metadata` 里取不到 version，同样视为「需要尝试迁移」。

#### 4.1.2 核心流程

一次 `model.load_state_dict(sd, strict=False)` 的完整流程（简化伪代码）：

1. 从 `sd._metadata` 取出全部模块前缀的 version 表（可能为空）。
2. 从根模块开始深度优先递归，对每个模块调用：
   `module._load_from_state_dict(sd, prefix, local_metadata, strict, missing_keys, unexpected_keys, error_msgs)`
   - `prefix` 是该模块在整棵树中的路径（如 `"sem_seg_head."`）；
   - `sd` 始终是**整份** state_dict，钩子自己按 `prefix` 过滤；
   - 钩子默认实现负责把 `sd` 中本模块的参数拷贝进 `module._parameters`。
3. 用户覆写的钩子在调用默认逻辑（通过 `super()` 或直接不调用）之前改写 `sd` 的 key。
4. 递归完成后，汇总 missing/unexpected key；`strict=False` 时只警告不抛错（形状不匹配仍会进 `error_msgs` 并抛 `RuntimeError`）。

关键性质：钩子改写的是**内存中的 state_dict**，影响且仅影响本次加载，不回写 checkpoint 文件。

#### 4.1.3 源码精读

Mask2Former 的两处迁移都采用同一个模板。先看两处 `_version = 2` 的声明：

- [mask2former/modeling/meta_arch/mask_former_head.py:L18-L21](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/mask_former_head.py#L18-L21)：`MaskFormerHead` 注册进 `SEM_SEG_HEADS_REGISTRY` 后立即声明 `_version = 2`——「当前代码的权重格式是第 2 版」。
- [mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:L207-L210](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L207-L210)：`MultiScaleMaskedTransformerDecoder` 同样声明 `_version = 2`。

两个钩子共享同一个骨架（以 head 为例，decoder 版逐字对应）：

- [mask2former/modeling/meta_arch/mask_former_head.py:L26-L29](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/mask_former_head.py#L26-L29)：取 `local_metadata` 中的 version；`version is None or version < 2` 才进入迁移分支；同时立一个 `scratch = True` 旗标，注释写明意图——「从零训练时不告警」。

再看消费端：`DetectionCheckpointer` 加载 `.pth` 时最终也是走 `load_state_dict` 这条递归路径，所以无论是 `train_net.py --eval-only` 还是 `tools/analyze_model.py`，只要 checkpoint 里是 v1 权重，两个钩子都会自动触发。

#### 4.1.4 代码实践

目标：亲眼看见 `_version` 被写进 state_dict 的 metadata，验证 4.1.2 描述的机制。

操作步骤（示例代码，独立脚本，可放在任意临时目录，不改动仓库）：

```python
# practice_metadata.py —— 零模型依赖，只需 PyTorch
import torch
from torch import nn

class MyHead(nn.Module):
    _version = 2                      # 模拟 MaskFormerHead 的版本声明
    def __init__(self):
        super().__init__()
        self.linear = nn.Linear(4, 4)

class MyModel(nn.Module):
    def __init__(self):
        super().__init__()
        self.backbone = nn.Identity()
        self.sem_seg_head = MyHead()

m = MyModel()
sd = m.state_dict()
print(sd.keys())                       # odict_keys(['sem_seg_head.linear.weight', ...])
for prefix, meta in sd._metadata.items():
    print(repr(prefix), meta)          # 关注 '' 与 'sem_seg_head' 两项的 version
```

需要观察的现象：

1. `sd._metadata` 中根模块（`''`）与 `sem_seg_head` 前缀都记录了 `'version': 2`。
2. 若删掉 `_version = 2` 再跑，`sem_seg_head` 对应的 version 变回 0——这正是「v1 时代保存的 checkpoint」在 metadata 里的样子。
3. 把 `sd` 保存再加载（`torch.save` / `torch.load`），metadata 随之持久化；而 `OrderedDict({...})` 手工构造的 dict 没有 `_metadata` 属性，加载时 `local_metadata` 为空 → version 取到 `None`。

预期结果：确认「version 存在于 checkpoint 的 metadata，而不是权重张量里」。若你本地输出与此不符，请以实际 PyTorch 版本行为为准（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么两个钩子的触发条件写成 `version is None or version < 2`，而不是只写 `version < 2`？

**答案**：当 state_dict 没有携带 `_metadata`（第三方转换出的 `.pkl`、手工构造的 dict）时，`local_metadata.get("version", None)` 返回 `None`，`None < 2` 在 Python 3 里会抛 `TypeError`；先判 `is None` 既避免崩溃，也顺带让「无版本信息」的权重走一遍迁移尝试（若确实没有旧 key，`scratch` 保持 True，不会误告警）。

**练习 2**：钩子改写 key 之后，为什么不担心影响同一 state_dict 中的 backbone 权重？

**答案**：改写有精确的条件过滤（head 钩子要求 key 含 `"sem_seg_head"` 且不以 `prefix + "predictor"` 开头；decoder 钩子要求 key 含 `"static_query"`），不满足条件的 key 原样保留；且改写只发生在本次加载的内存字典里。

### 4.2 MaskFormerHead._load_from_state_dict：pixel_decoder 前缀迁移

#### 4.2.1 概念说明

v1（MaskFormer）时代，FPN 式像素解码器的 lateral/output 卷积直接作为 `MaskFormerHead` 的内联属性存在，参数路径形如 `sem_seg_head.lateral_convs.0.*`；v2 重构把这些层整体搬进了 `pixel_decoder` 子模块（u3-l4 讲过的 `BasePixelDecoder` / `MSDeformAttnPixelDecoder`），参数路径多出一级 `pixel_decoder.`。结构变了，数值没变——所以迁移规则非常机械：**给属于 head、但不属于 predictor 的 key，插入一段 `pixel_decoder.` 前缀**。

注意 predictor（Transformer 解码器）在 v1、v2 中都挂在 `sem_seg_head.predictor` 下、属性名没变，必须显式排除，否则会把解码器的 key 也错误改写。

#### 4.2.2 核心流程

```
for k in state_dict 的所有 key（先快照成 list）:
    if "sem_seg_head" 出现在 k 中 且 k 不以 "sem_seg_head.predictor" 开头:
        newk = k 把 "sem_seg_head." 替换为 "sem_seg_head.pixel_decoder."
        state_dict[newk] = state_dict[k];  删除 state_dict[k]
        scratch = False          # 证明这不是从零训练
if not scratch:
    打印 warning："Weight format of MaskFormerHead have changed! ..."
```

替换用 `prefix` 变量而非硬编码 `"sem_seg_head."`，`prefix` 由 PyTorch 递归装载机制传入，始终等于该模块的真实路径，因此即使整个 head 被嵌到别的容器模块下，规则依然成立（当然条件里的 `"sem_seg_head" in k` 仍是硬编码，依赖 meta arch 中属性名恰好叫 `sem_seg_head`）。

#### 4.2.3 源码精读

- [mask2former/modeling/meta_arch/mask_former_head.py:L23-L34](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/mask_former_head.py#L23-L34)：钩子完整签名与改写核心。第 33 行是过滤条件：key 属于 `sem_seg_head` 且**不**以 `prefix + "predictor"` 开头；第 34 行执行前缀插入：`k.replace(prefix, prefix + "pixel_decoder.")`。
- [mask2former/modeling/meta_arch/mask_former_head.py:L36-L45](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/mask_former_head.py#L36-L45)：只要发生过任何改写（`scratch=False`），打一条 warning 提示用户「权重格式变了，已自动转换」。从零训练或加载新格式权重时一个 key 都不会命中，静默通过。

对照模型侧结构：[mask2former/modeling/meta_arch/mask_former_head.py:L81-L83](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/mask_former_head.py#L81-L83) 中 `self.pixel_decoder` 与 `self.predictor` 两个子模块就是迁移的目标路径来源。

#### 4.2.4 代码实践

目标：用一份「v1 风格」的假 state_dict 驱动这个钩子，看到 key 被原地改写、warning 被打印，并验证 predictor key 与无关 key 不受影响。

前置条件：已完成 u1-l2 的环境安装与 CUDA 扩展编译（`import mask2former` 的注册副作用需要它；本实践跑在 CPU 上即可）。

操作步骤（示例代码）：

```python
# practice_head_hook.py
import logging, torch
from torch import nn
logging.basicConfig(level=logging.WARNING)   # 让 logger.warning 显示出来

from detectron2.layers import ShapeSpec
from mask2former.modeling.meta_arch.mask_former_head import MaskFormerHead

# @configurable 支持直接传关键字参数构建（不经过 cfg）
# 用 nn.Identity() 占位两个子模块：本实践只考察钩子，不需要真实解码器
head = MaskFormerHead(
    {"res2": ShapeSpec(channels=8, stride=4)},
    num_classes=1,
    pixel_decoder=nn.Identity(),
    transformer_predictor=nn.Identity(),
    transformer_in_feature="multi_scale_pixel_decoder",
)

fake_sd = {
    "sem_seg_head.lateral_convs.0.conv.weight": torch.randn(8, 8, 1, 1),   # v1 风格：FPN 内联在 head 下
    "sem_seg_head.predictor.transformer_self_attention_layers.0.norm.weight": torch.randn(8),  # predictor key
    "backbone.res2.0.conv1.weight": torch.randn(8, 3, 1, 1),               # 无关 key
}

# 手动调用钩子，模拟 load_state_dict 递归时发生的事
head._load_from_state_dict(
    fake_sd, prefix="sem_seg_head.", local_metadata={},   # 空 metadata → version=None → 触发
    strict=False, missing_keys=[], unexpected_keys=[], error_msgs=[],
)
print("== 改写后的 key ==")
for k in sorted(fake_sd):
    print(" ", k)
```

需要观察的现象：

1. `sem_seg_head.lateral_convs.0.conv.weight` → `sem_seg_head.pixel_decoder.lateral_convs.0.conv.weight`。
2. `sem_seg_head.predictor.*` 与 `backbone.*` 两类 key 原样保留。
3. stderr 打印一条 `Weight format of MaskFormerHead have changed! ... Applying automatic conversion now ...`，logger 名为 `mask2former.modeling.meta_arch.mask_former_head`。
4. 对照组：把 `local_metadata={}` 换成 `{"version": 2}` 再跑一次——key 不变、无 warning。

预期结果：与上述四点完全一致（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：如果 v1 checkpoint 里同时有 `sem_seg_head.lateral_convs.0.conv.weight` 与 `sem_seg_head.predictor.class_embed.weight`，加载后两者分别落在模型的哪个参数上？

**答案**：前者被改写为 `sem_seg_head.pixel_decoder.lateral_convs.0.conv.weight`，装进像素解码器；后者路径未变（predictor 被条件排除），仍装到 `sem_seg_head.predictor.class_embed`——v2 解码器的分类头沿用同一命名。

**练习 2**：把 `pixel_decoder` 换成自定义名字（如 `pd`）需要同步修改哪些地方？

**答案**：至少三处——`MaskFormerHead.__init__` 中的属性名（`mask_former_head.py:L81`）、u3-l4/u8-l2 讲过的 `build_pixel_decoder` 工厂注入点，以及本钩子第 34 行的替换字符串 `"pixel_decoder."`。漏改第三处的话，v1 权重将静默装不上（进入 unexpected keys 列表）。这也是「迁移逻辑与模块命名强耦合」的典型案例。

### 4.3 MultiScaleMaskedTransformerDecoder._load_from_state_dict：static_query 改名 query_feat

#### 4.3.1 概念说明

v2 解码器用两个同形可学习嵌入承载 query（u4-l3 讲过）：`query_feat` 是内容嵌入，`query_embed` 是位置嵌入。而 MaskFormer(v1) 发布的旧 checkpoint 里，内容嵌入的参数名是 `static_query`——名字变了、张量含义与形状没变，所以迁移规则是**子串改名**：`static_query → query_feat`。

一个值得注意的事实：本仓库当前的 v1 解码器文件（`maskformer_transformer_decoder.py`）里只有 `query_embed`（[maskformer_transformer_decoder.py:L89-L99](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/maskformer_transformer_decoder.py#L89-L99) 中可见 `self.query_embed = nn.Embedding(...)`），并没有 `static_query`。也就是说这个钩子针对的不是「本仓库某个现存类的旧命名」，而是**历史上发布出去的 v1 checkpoint 里的参数名**——这正是版本迁移代码的典型处境：要兼容的旧格式只存在于用户手里的旧文件中。

#### 4.3.2 核心流程

与 4.2.2 同一模板，仅过滤条件和替换规则不同：

```
for k in state_dict 的所有 key:
    if "static_query" 出现在 k 中（任意位置）:
        newk = k 把 "static_query" 替换为 "query_feat"
        搬家 + scratch = False
if not scratch: 打 warning
```

注意它是**全局子串替换、不看 prefix**——因为 `static_query` 这个词只会出现在解码器自己的参数名里，无需更精确的过滤。

#### 4.3.3 源码精读

- [mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:L212-L227](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L212-L227)：钩子全文。第 215 行取 version，第 216 行判断版本，第 222-223 行做 `k.replace("static_query", "query_feat")`，第 224-227 行搬家并把 `scratch` 置 False。
- [mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:L229-L233](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L229-L233)：与 head 钩子逐字相同的 warning。
- 改名的目标参数在模型侧的定义：[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:L314-L318](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L314-L318)——`query_feat`（L316）与 `query_embed`（L318）两个 `nn.Embedding(num_queries, hidden_dim)`。

两个钩子的调用时机由 PyTorch 递归顺序决定：`load_state_dict` 从根模块深度优先，`sem_seg_head` 的钩子先执行（处理 FPN 前缀），随后递归到子模块 `sem_seg_head.predictor` 时 decoder 的钩子再执行（处理 static_query 改名）。两者遍历的是同一份完整 state_dict，各改各的 key，互不干扰。

#### 4.3.4 代码实践

目标：驱动 decoder 钩子完成改名，并用「数值真的装进了 `query_feat`」做硬验证；再对比版本开关的行为。

操作步骤（示例代码）：

```python
# practice_decoder_hook.py
import logging, torch
logging.basicConfig(level=logging.WARNING)

from mask2former.modeling.transformer_decoder.mask2former_transformer_decoder import (
    MultiScaleMaskedTransformerDecoder,
)

dec = MultiScaleMaskedTransformerDecoder(
    8, True,                                   # in_channels, mask_classification
    num_classes=1, hidden_dim=8, num_queries=3, nheads=2,
    dim_feedforward=16, dec_layers=2, pre_norm=False, mask_dim=8,
    enforce_input_project=False,
)

w = torch.randn(3, 8)                          # 与 query_feat.weight 同形
fake_sd = {
    "sem_seg_head.predictor.static_query.weight": w.clone(),   # v1 旧名
    "sem_seg_head.predictor.query_embed.weight": torch.randn(3, 8),  # 旧名未变
}

dec._load_from_state_dict(
    fake_sd, prefix="sem_seg_head.predictor.", local_metadata={},
    strict=False, missing_keys=[], unexpected_keys=[], error_msgs=[],
)
print("改写后:", sorted(fake_sd.keys()))
print("数值已装入 query_feat:", torch.equal(dec.query_feat.weight, w))   # 钩子只改名，这里仍需装载验证
```

注意最后一步：钩子本身只改 key 不拷贝数值（拷贝是 `load_state_dict` 默认逻辑做的）。要看到 `torch.equal` 为 True，需要走完整装载路径，请进入综合实践（第 5 节）用 `model.load_state_dict` 验证。

需要观察的现象：`static_query.weight` 变成 `query_feat.weight`；`query_embed.weight` 不变；打印一条 warning（logger 名 `mask2former.modeling.transformer_decoder.mask2former_transformer_decoder`）。对照组 `local_metadata={"version": 2}` 时一切不动。

预期结果：如上（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 decoder 钩子敢做全局子串替换，而 head 钩子要做前缀判断？

**答案**：`static_query` 是一个独特到不会误伤的 token——整个模型的参数名里只有解码器内容嵌入叫这个名字；而 head 钩子要匹配的是「一段前缀」，`sem_seg_head.` 下还有 predictor 等不该动的子树，必须精确排除。迁移代码的过滤条件粒度取决于名字的独特性。

**练习 2**：假设 v3 想把 `query_feat` 再改名为 `query_content`，迁移代码应怎么写？

**答案**：把 `_version` 提到 3，在钩子里追加一段 `if version < 3: k.replace("query_feat", "query_content")`，并保留 version<2 的 `static_query → query_feat` 分支——这样 v1、v2、v3 的 checkpoint 都能加载（链式迁移）。

### 4.4 两个转换脚本：R50 的 key 映射与 Swin 的零改写策略

#### 4.4.1 概念说明

这一节从「自家版本迁移」转向「外部权重引进」。detectron2 生态约定 checkpoint 是一个 pickle 字典，至少含 `model` 键；两个脚本做的事就是把第三方 `.pth` 包装成这个格式，并按需附带两个元数据：

- `"__author__": "torchvision" / "third_party"`——记录权重来源的署名元数据。
- `"matching_heuristics": True`——告诉 `DetectionCheckpointer` 启用**启发式匹配**：装载时不要求 key 完全相等，而是允许「checkpoint key 作为模型 key 的后缀 + 张量形状一致」的对齐。这解释了为什么转换脚本不需要给权重加 `backbone.` 前缀：`res2.0.conv1.weight` 能对上模型里的 `backbone.res2.0.conv1.weight`。

两脚本策略截然不同：

| | R50 脚本 | Swin 脚本 |
|---|---|---|
| 命名差异 | torchvision 与 detectron2 的 ResNet 命名完全不同 | 本仓库 swin.py 从官方分类实现移植，参数名一致 |
| 策略 | 逐条改写 key | 零改写，直接包元数据 |
| 依赖 | 无（纯字符串处理） | 无 |

**一个必要的考据说明**：本讲规格中提到的最小模块 `convert_to_detectron2_registry`，经对仓库全文检索（含 `git grep`）确认**并不存在**这个函数（待确认：可能来自 detectron2 生态其他项目或大纲规划时的笔误）。它在规格中的实际意图，对应的就是两个脚本末尾构造 detectron2 checkpoint 字典的代码段（`{"model": ..., "__author__": ..., "matching_heuristics": True}`）以及消费端 `DetectionCheckpointer` 的对齐逻辑，本节按此意图讲解。

#### 4.4.2 核心流程

R50 脚本主循环对每个 key 依次应用四条规则（顺序重要）：

1. **stem 前缀**：key 中不含 `layer`（即 `conv1` / `bn1` / `fc` 这些顶层参数）→ 加 `stem.` 前缀。
2. **stage 改名**：`layer{1..4}` → `res{2..5}`。
3. **BN 归位**：`bn{1,2,3}` → `conv{1,2,3}.norm`（detectron2 的 `Conv2d` 把 norm 收纳为 conv 的子模块）。
4. **shortcut 改名**：`downsample.0` → `shortcut`，`downsample.1` → `shortcut.norm`。

Swin 脚本只有三步：`torch.load(...)["model"]` → 包上元数据 dict → `pickle.dump`。

#### 4.4.3 源码精读

**R50 脚本** [tools/convert-torchvision-to-d2.py:L27-L51](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/convert-torchvision-to-d2.py#L27-L51)：

- L30：`torch.load(input, map_location="cpu")`——torchvision 的 ResNet `.pth` 就是裸 state_dict。
- [L33-L44](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/convert-torchvision-to-d2.py#L33-L44)：四条规则的实现。注意 L35-36 先判断 `layer` 再加 `stem.`，随后 L39-40 的 `bn` 替换循环对**所有** key 生效——所以 stem 的 `bn1.weight` 先变成 `stem.bn1.weight`，再被改成 `stem.conv1.norm.weight`，恰好对上 detectron2 `BasicStem` 的真实参数路径 `stem.conv1.norm.*`。
- L43：逐条打印 `旧 key -> 新 key`，转换过程完全透明。
- [L46-L49](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/convert-torchvision-to-d2.py#L46-L49)：包装成 `{"model": newmodel, "__author__": "torchvision", "matching_heuristics": True}` 并 pickle 落盘。
- L50-51：`if obj: print("Unconverted keys", ...)`——由于 L44 对每个 key 都执行了 `obj.pop(old_k)`，`obj` 最终恒为空，这两行实际永远不会打印非空内容，属于防御性残留代码。
- 脚本头部的使用说明 [L9-L25](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/convert-torchvision-to-d2.py#L9-L25) 不只是注释：它列出**key 兼容之外的配套配置**——`PIXEL_MEAN: [123.675, 116.280, 103.530]`、`PIXEL_STD: [58.395, 57.120, 57.375]`（ImageNet 的 RGB 顺序统计量）、`RESNETS.STRIDE_IN_1X1: False`（torchvision 是 stride-in-3x3）、`INPUT.FORMAT: "RGB"`（detectron2 默认 BGR）。少改任何一项，权重能装上但数值口径全错——**权重兼容 = key 兼容 + 预处理兼容**。

**Swin 脚本** [tools/convert-pretrained-swin-model-to-d2.py:L22-L30](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/convert-pretrained-swin-model-to-d2.py#L22-L30)：

- L25：`torch.load(input, map_location="cpu")["model"]`——Swin 官方权重包在 `"model"` 键下。
- L27：`{"model": obj, "__author__": "third_party", "matching_heuristics": True}`——一个 key 都不改。

零改写能成立的证据在 swin.py：本仓库实现从 Swin 官方分类代码移植，内部参数名原样保留——[swin.py:L559-L560](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/backbone/swin.py#L559-L560) 的 `self.patch_embed`、[swin.py:L588-L605](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/backbone/swin.py#L588-L605) 的 `self.layers = nn.ModuleList()`。官方 checkpoint 里的 `patch_embed.proj.weight`、`layers.0.blocks...` 与模型里的 `backbone.patch_embed.proj.weight` 只差一个 `backbone.` 前缀，由启发式后缀匹配补齐；分类头 `head.*` 在模型中不存在，作为多余 key 被丢弃；detectron2 适配层新增的 `norm0..norm3` 没有来源，保持随机初始化（u3-l3 已分析过）。适配器 `D2SwinTransformer` 的注册见 [swin.py:L686-L688](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/backbone/swin.py#L686-L688)。

#### 4.4.4 代码实践

目标：离线跑通 R50 转换（不需要下载真权重），整理出完整映射表；对照 detectron2 真实 R50 的 key 验证对齐情况。

操作步骤（示例代码）：

```python
# practice_make_fake_r50.py —— 用随机权重的 torchvision R50 代替下载
import torch, torchvision
m = torchvision.models.resnet50(weights=None)      # 只为拿 key 结构与形状
torch.save(m.state_dict(), "/tmp/r50_fake.pth")
# 然后执行仓库脚本（在仓库根目录）：
#   python tools/convert-torchvision-to-d2.py /tmp/r50_fake.pth /tmp/r50_fake.pkl
```

随后检查产物：

```python
# practice_check_pkl.py
import pickle
d = pickle.load(open("/tmp/r50_fake.pkl", "rb"))
print(d["__author__"], d["matching_heuristics"])   # torchvision True
for k in list(d["model"])[:8]:
    print(k, tuple(d["model"][k].shape))
```

需要观察的现象（对照 4.4.2 的规则表）：

| torchvision key | 转换后 key |
|---|---|
| `conv1.weight` | `stem.conv1.weight` |
| `bn1.weight` | `stem.conv1.norm.weight` |
| `layer1.0.conv1.weight` | `res2.0.conv1.weight` |
| `layer1.0.bn2.weight` | `res2.0.conv2.norm.weight` |
| `layer1.0.downsample.0.weight` | `res2.0.shortcut.weight` |
| `layer1.0.downsample.1.weight` | `res2.0.shortcut.norm.weight` |
| `fc.weight` | `stem.fc.weight`（模型中无此参数，装载时被丢弃） |

预期结果：打印的映射与上表一致（待本地验证）。进阶：用 detectron2 的 `build_resnet_backbone` 构建真实 R50（配置 `RESNETS.DEPTH: 50`、`STRIDE_IN_1X1: False`），对比 `model.state_dict()` 与转换产物，找出能对上与对不上的 key——特别留意 `stem.fc.*` 与分类头相关 key 的去向。

Swin 侧的阅读型实践：读 [convert-pretrained-swin-model-to-d2.py:L22-L30](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/convert-pretrained-swin-model-to-d2.py#L22-L30)，再打印 `D2SwinTransformer` 的前 20 个参数名（可用 `MODEL.SWIN.*` 最小配置构建，或直接阅读 swin.py 的 `__init__`），确认它们与官方 Swin 分类权重的 key（去掉 `backbone.` 前缀后）同名。

#### 4.4.5 小练习与答案

**练习 1**：R50 脚本把 `bn1 → conv1.norm` 的替换循环放在 `stem.` 前缀之后、且对所有 key 生效，若把它挪到最前面（`stem.` 前缀判断之前），结果会变吗？

**答案**：不变。替换是幂等的字符串操作，`bn1.weight` 无论先变成 `conv1.norm.weight` 再加前缀（`stem.conv1.norm.weight`），还是先加前缀再替换（`stem.bn1.weight` → `stem.conv1.norm.weight`），最终结果相同。真正不能换序的是 `layer` 判断——它必须基于**原始** key 判断（`layer` 不在 `k` 中才加 `stem.`），若在 `layer→res` 替换之后判断也无影响，但若某个规则产生了新的 `layer` 子串则会误判（本脚本不存在这种情况）。

**练习 2**：为什么 Swin 脚本不需要写任何映射规则，而 R50 脚本写了四条？

**答案**：因为本仓库的 swin.py 是从官方 Swin 分类实现**逐名移植**的（`patch_embed`、`layers.N.blocks...` 全部同名），第三方权重与模型参数名只差 `backbone.` 前缀，后缀启发式即可对齐；而 detectron2 的 ResNet 是 caffe2 风格命名（`res2`、`conv1.norm`、`shortcut`），与 torchvision 风格差异是结构性的，必须显式改写。启示：**想让外部权重零成本接入，最有效的办法是移植代码时保留上游参数命名**。

**练习 3**：`matching_heuristics: True` 与 4.2/4.3 的钩子迁移，各自解决什么问题？

**答案**：钩子解决**纵向**兼容——同一项目新旧版本之间的 key 改写，规则由作者显式编码；启发式解决**横向**对齐——第三方权重与本模型之间「差一个前缀」的模糊匹配，靠「后缀 + 形状」自动完成。两者可以叠加：R50 权重经启发式对齐装进 backbone 后，sem_seg_head 的钩子迁移依旧按需工作。

## 5. 综合实践

**任务：伪造一份 MaskFormer(v1) 风格的 checkpoint，端到端走完「两个钩子协同迁移 + 真实装载 + 硬验证」。**

这个实践把本讲三条主线（钩子机制、前缀迁移、改名迁移）串成一次真实加载，且全程 CPU 可跑。

操作步骤（示例代码）：

```python
# practice_u10l4_full.py
import logging, torch
from torch import nn
logging.basicConfig(level=logging.WARNING)

from detectron2.layers import ShapeSpec
from mask2former.modeling.meta_arch.mask_former_head import MaskFormerHead
from mask2former.modeling.transformer_decoder.mask2former_transformer_decoder import (
    MultiScaleMaskedTransformerDecoder,
)

# 1) 构建一个"最小但结构真实"的 head：predictor 用真解码器，pixel_decoder 用占位
predictor = MultiScaleMaskedTransformerDecoder(
    8, True, num_classes=1, hidden_dim=8, num_queries=3, nheads=2,
    dim_feedforward=16, dec_layers=2, pre_norm=False, mask_dim=8,
    enforce_input_project=False,
)
head = MaskFormerHead(
    {"res2": ShapeSpec(channels=8, stride=4)},
    num_classes=1,
    pixel_decoder=nn.Identity(),               # 占位：FPN key 改写后无处可装，进入 unexpected keys
    transformer_predictor=predictor,
    transformer_in_feature="multi_scale_pixel_decoder",
)

# 2) 仿照 MaskFormer meta arch 的属性名搭一个假顶层（钩子条件依赖 "sem_seg_head" 字样）
class FakeMetaArch(nn.Module):
    def __init__(self, head):
        super().__init__()
        self.backbone = nn.Identity()
        self.sem_seg_head = head

model = FakeMetaArch(head)

# 3) 伪造 v1 checkpoint：只有三类关键 key，形状必须与目标参数一致
q_w = torch.randn(3, 8); e_w = torch.randn(3, 8)
v1_sd = {
    "sem_seg_head.lateral_convs.0.conv.weight": torch.randn(8, 8, 1, 1),  # v1 内联 FPN key
    "sem_seg_head.predictor.static_query.weight": q_w.clone(),            # v1 旧 query 名
    "sem_seg_head.predictor.query_embed.weight": e_w.clone(),
}

# 4) 真实装载路径：load_state_dict 内部会先后调用 head 与 predictor 的钩子
result = model.load_state_dict(v1_sd, strict=False)
print("missing keys:", len(result.missing_keys), "unexpected keys:", len(result.unexpected_keys))
print("unexpected:", result.unexpected_keys)

# 5) 硬验证：static_query 的数值应已装入 query_feat
print("query_feat 装载成功:", torch.equal(model.sem_seg_head.predictor.query_feat.weight, q_w))
print("query_embed 装载成功:", torch.equal(model.sem_seg_head.predictor.query_embed.weight, e_w))
```

需要观察的现象与预期结果：

1. stderr 依次出现两条 warning（先 head 后 predictor——递归装载先父后子）。
2. `unexpected_keys` 中出现 `sem_seg_head.pixel_decoder.lateral_convs.0.conv.weight`——证明 head 钩子已完成前缀迁移，只是我们的占位 `pixel_decoder` 没有对应参数（真实模型中它会正常装上）。
3. 两个 `torch.equal` 均为 `True`——证明 `static_query → query_feat` 改名后数值真实进入模型，而非只停留在 key 层面。
4. `missing_keys` 非空：模型其余参数（注意力层、FFN、class_embed 等）没有来源，保持随机初始化——这正是「部分加载」的语义。

若第 3 步为 `False`，优先检查张量形状是否与 `query_feat.weight`（`num_queries × hidden_dim`，本例 3×8）严格一致。全部现象待本地验证。

## 6. 本讲小结

- PyTorch 在 `load_state_dict` 时先递归调用各子模块的 `_load_from_state_dict` 钩子再装参数；钩子可在装载前原地改写 state_dict 的 key，`_version` 与 checkpoint `_metadata` 提供「新代码还是旧权重」的判断依据。
- `MaskFormerHead._load_from_state_dict`（`_version=2`）把 v1 内联在 head 下的 FPN key 插入 `pixel_decoder.` 前缀，并用 `not k.startswith(prefix + "predictor")` 保护解码器子树；`MultiScaleMaskedTransformerDecoder` 的同名钩子把 v1 权重的 `static_query` 全局改名为 `query_feat`。两者都带 `scratch` 旗标：从零训练时不告警。
- 两个钩子针对的旧 key（`sem_seg_head.lateral_convs.*`、`static_query`）在当前仓库代码中已不存在——迁移代码的使命正是兼容那些「只活在用户旧 checkpoint 里」的格式。
- `tools/convert-torchvision-to-d2.py` 用四条有序规则改写 key（stem 前缀、`layer→res`、`bn→conv.norm`、`downsample→shortcut`），并落盘成 `{"model", "__author__", "matching_heuristics"}` 的 detectron2 pickle；使用 torchvision R50 还必须同步改 `PIXEL_MEAN/PIXEL_STD`、`STRIDE_IN_1X1: False`、`INPUT.FORMAT: "RGB"`——key 兼容之外还有预处理兼容。
- `tools/convert-pretrained-swin-model-to-d2.py` 零改写，全靠 swin.py 移植时保留上游参数命名 + `matching_heuristics` 的后缀/形状启发式对齐；规格中提到的 `convert_to_detectron2_registry` 在本仓库不存在（已检索确认），其意图对应脚本构造 checkpoint 元数据字典的代码段。
- `DetectionCheckpointer` 的部分加载（多余 key 丢弃、缺失 key 随机初始化）是「backbone 预训练 + 整模型从头训」工作流的基础，仓库内入口见 `train_net.py` 的 `--eval-only` 分支与 `tools/analyze_model.py`。

## 7. 下一步学习建议

至此单元十（也是整本手册的最后一个单元）的机制篇完结。建议：

1. **横向对照 detectron2 的同类机制**：detectron2 的 ResNet 也有 `_version = 2` 迁移（处理 caffe2 时代的 `bn` 命名），阅读 `detectron2/modeling/backbone/resnet.py` 中的 `_load_from_state_dict`，与本讲两个钩子对比过滤条件的写法。
2. **追踪加载全链路**：从 `train_net.py` 的 `resume_or_load` 进入 fvcore `Checkpointer.load` → `_load_file`（按扩展名分发 pickle/torch.load）→ `_load_model`（`matching_heuristics` 分支与 `align_and_update_state_dicts`），把本讲的「行为描述」落实到宿主框架源码。
3. **动手实验（承接 u8 系列）**：如果你在 u8-l1/u8-l2 写过自定义 backbone 或 pixel decoder，给它加上自己的 `_load_from_state_dict` 钩子，演练一次「v2 → v3」的链式迁移设计（参考 4.3.5 练习 2 的答案）。
4. **收官回顾**：以 `MODEL_ZOO.md` 中任一权重为对象，从 `--eval-only` 命令出发，口述这份 checkpoint 从磁盘文件到模型参数的完整旅程（扩展名分发 → 元数据 → 启发式对齐 → 钩子迁移 → 部分装载）——能不假思索地讲完，说明你已经把 Mask2Former 的工程面打通了。

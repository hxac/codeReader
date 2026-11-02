# 权重转换与 Checkpoint 版本兼容

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 PyTorch `load_state_dict` 的逐模块分发机制，以及 `_load_from_state_dict` 钩子、`_version` 元数据分别在什么时机起作用。
2. 逐行解释 `MaskFormerHead._load_from_state_dict`（插入 `pixel_decoder.` 前缀）与 `MultiScaleMaskedTransformerDecoder._load_from_state_dict`（`static_query` → `query_feat` 改名）这两条自动迁移规则，理解它们如何「两级接力」把 MaskFormer（v1）旧 checkpoint 的 key 改写成 Mask2Former（v2）布局。
3. 会用 `tools/convert-torchvision-to-d2.py` 与 `tools/convert-pretrained-swin-model-to-d2.py` 准备 ImageNet 预训练权重，并说出 `matching_heuristics` 与 `strict=False` 部分加载的含义。
4. 动手构造带旧 key 的假 state_dict，亲眼看到迁移 warning 与改名结果，而不是停留在纸面理解。

本讲是单元十（工程化主题）的第四讲，也是全手册对「三段式架构」的最后一块补充：u3-l2 从组装器视角初识过 `_version = 2`，本讲从 **checkpoint 生命周期**的视角把它讲透。

## 2. 前置知识

### 2.1 state_dict：模块树的「扁平化快照」

PyTorch 模型是嵌套的 `nn.Module` 树。调用 `model.state_dict()` 得到的是一个扁平字典：

- **key = 从根到参数的属性路径**。例如 `sem_seg_head.pixel_decoder.mask_features.weight` 表示根模型 → 子模块 `sem_seg_head` → 子模块 `pixel_decoder` → 子模块 `mask_features`（一个卷积）→ 它的 `weight`。
- **value = 张量本体**。

因此，**代码重构一旦改变了模块树的挂载位置或属性名，旧 checkpoint 里的 key 就全部失配**——权重数值完好，但名字对不上，`load_state_dict` 会把它们当作「unexpected keys」丢弃。本讲的核心就是 Mask2Former 为此准备的两套补救机制。

### 2.2 load_state_dict 的两条通道：strict 与钩子

- `model.load_state_dict(sd, strict=True)`：key 必须与模型完全一致，缺一个、多一个都抛异常。
- `strict=False`：**部分加载**——对得上的 key 覆盖权重，模型有而 checkpoint 没有的 key（missing）保持随机初始化，checkpoint 有而模型没有的 key（unexpected）被忽略。三条清单会装进返回的 `NamedTuple(missing_keys, unexpected_keys)`。
- 加载时 PyTorch 会**递归地、逐模块**调用 `module._load_from_state_dict(state_dict, prefix, local_metadata, ...)`：每个模块拿到的是**整棵树共享的 state_dict 副本**、自己的 `prefix`（如 `"sem_seg_head."`）和版本元数据，先让模块自己「改造」key，再由基类实现完成真正的拷贝。子类覆写这个钩子即可实现**加载时的自动迁移**。
- **版本元数据**：`state_dict()` 保存时，会把每个模块当前的类属性 `_version` 连同前缀记进该 `OrderedDict` 的 `_metadata` 属性；`load_state_dict` 时按前缀取出来作为 `local_metadata["version"]` 传给钩子。若传入的字典没有 `_metadata`（手拼的字典、被格式转换工具重建过的字典），钩子拿到的 version 是 `None`。
- 这一机制的「祖师爷」是 PyTorch 自家的 `_BatchNorm`：它把 `_version` 升到 2，用 `if version is None or version < 2` 迁移缺少 `num_batches_tracked` 的旧权重。Mask2Former 的钩子连 warning 文案都照搬了这套写法。

### 2.3 detectron2 的 checkpoint 约定

本仓库（及所有 detectron2 插件项目）不直接用 `torch.save`/`torch.load`，而是经由 `DetectionCheckpointer`（detectron2 提供）：

- checkpoint 通常是 **pkl 文件**，内容是一个字典：`"model"`（state_dict，值允许是 numpy 数组）、可选的 `"__author__"`（来源标记）与 `"matching_heuristics"`（宽松对齐开关）。本讲要读的两个转换脚本产出的正是这种格式。
- `matching_heuristics=True` 时，checkpointer 会做**名字后缀 + 形状一致**的对齐（detectron2 的 `c2_model_loading.align_and_update_state_dicts`），让没有 `backbone.` 前缀的 ImageNet 权重对齐到 `backbone.*` 上（u3-l3 已用过这一结论）；最终仍以 `strict=False` 落盘加载，missing/unexpected 都会记日志。
- 具体实现随 detectron2 版本略有差异，本讲涉及细节处会标注「以本地安装的 detectron2 源码为准」。

### 2.4 为什么会有「旧权重」：仓库的演化史

Mask2Former 仓库由 MaskFormer（v1）仓库演化而来：v1 的 `MaskFormerHead` 把像素解码器的卷积层**直接**挂在 `sem_seg_head` 下；v2 重构出独立的 `pixel_decoder` 子模块（u3-l4、u3-l5 讲过的 `build_pixel_decoder` 工厂），key 布局随之改变。同时 v1 解码器里可学习 query 叫 `static_query`，v2 拆成 `query_feat`（内容）+ `query_embed`（位置）双嵌入（u4-l3）。大量 v1 训练好的权重仍散落在论文发布页与集群里——「权重还在旧布局，代码已是新布局」，迁移钩子因此而生。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [mask2former/modeling/meta_arch/mask_former_head.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/mask_former_head.py) | 迁移钩子之一：给旧 key 插入 `pixel_decoder.` 前缀；`_version = 2` 声明 |
| [mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py) | 迁移钩子之二：`static_query` → `query_feat` 改名；v2 的双 query 嵌入定义处 |
| [tools/convert-torchvision-to-d2.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/convert-torchvision-to-d2.py) | torchvision ResNet 分类权重 → detectron2 pkl（key 逐条改名） |
| [tools/convert-pretrained-swin-model-to-d2.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/convert-pretrained-swin-model-to-d2.py) | 官方 Swin 分类权重 → detectron2 pkl（不改 key，只加元信息） |
| [mask2former/modeling/meta_arch/per_pixel_baseline.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/per_pixel_baseline.py) | 对照参考：两个 per-pixel 基线头里的同款钩子（u8-l4 已讲模型本身） |
| [MODEL_ZOO.md](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/MODEL_ZOO.md) | 第三方 ImageNet 预训练模型的获取指引（指向 MaskFormer 仓库 tools） |

> 说明：本讲规划中出现的函数名 `convert_to_detectron2_registry` 在当前仓库中**不存在**（全仓检索确认），它的实际对应物就是上表两个 `tools/` 脚本——都是无函数定义的 `__main__` 直排脚本，本文按真实文件讲解。

## 4. 核心概念与源码讲解

### 4.1 版本迁移问题的全景：key 布局差异与两级接力

#### 4.1.1 概念说明

把「加载旧权重」理解成一次**命名空间翻译**：checkpoint 的 key 空间是 v1 代码的模块树，模型的 key 空间是 v2 代码的模块树。差异集中在两处：

| v1（MaskFormer 旧布局，示意名） | v2（当前代码布局） | 谁来修 |
| --- | --- | --- |
| `sem_seg_head.<像素解码器层>.weight`（卷积层直接挂在 head 下） | `sem_seg_head.pixel_decoder.<像素解码器层>.weight` | `MaskFormerHead` 的钩子：插入 `pixel_decoder.` |
| `sem_seg_head.predictor.static_query.weight` | `sem_seg_head.predictor.query_feat.weight` | 解码器的钩子：改名 |

两点需要立即澄清，避免误解：

- **「两级接力」的分工**：head 的钩子在 prefix 为 `"sem_seg_head."` 时运行，它负责搬运除 predictor 以外的一切；解码器的钩子在 prefix 为 `"sem_seg_head.predictor."` 时运行，只管 `static_query` 一族。两级钩子操作的是**同一个共享 state_dict 副本**（PyTorch 递归分发时传同一个字典对象），先跑完的改名结果对后跑的立即可见，且两者改写的 key 集合不相交。
- **钩子只救「名字」，不救「结构」**：若 v1 与 v2 的层数、通道数不同（例如 v2 因给 `query_feat` 增加第 0 组监督而实建 `DEC_LAYERS−1` 层，见 4.3.1），多出来的层是 missing（随机初始化）、多出来的旧层是 unexpected（被忽略）——钩子不做也不该做这件事，这正是 `strict=False` 部分加载的用武之地。

#### 4.1.2 核心流程

一次 `model.load_state_dict(old_ckpt, strict=False)` 的完整时序：

```text
load_state_dict(sd, strict=False)
 ├─ metadata = sd._metadata（可能为 None）
 ├─ sd = sd.copy()                      # 浅拷贝：钩子改的是这份副本，调用者的字典不动
 └─ load(root, prefix="")
     ├─ root._load_from_state_dict(sd, "", metadata.get("", {}), ...)   # MaskFormer 本体：无自定义钩子
     ├─ load(backbone, "backbone.")                                      # Swin/ResNet：无自定义钩子
     │    └─ ...
     └─ load(sem_seg_head, "sem_seg_head.")
          ├─ sem_seg_head._load_from_state_dict(sd, "sem_seg_head.",     # ← 第一级：搬 pixel_decoder 前缀
          │        metadata.get("sem_seg_head", {}), ...)
          ├─ load(pixel_decoder, "sem_seg_head.pixel_decoder.")          # MSDeformAttnPixelDecoder：无钩子
          └─ load(predictor, "sem_seg_head.predictor.")
               └─ predictor._load_from_state_dict(sd,                    # ← 第二级：static_query→query_feat
                        "sem_seg_head.predictor.",
                        metadata.get("sem_seg_head.predictor", {}), ...)
```

每级钩子的判定逻辑都是同一个小状态机：

```text
version = local_metadata.get("version", None)
if version is None 或 version < 2:
    遍历整份 state_dict 的 key（注意用 list(keys()) 快照，边遍历边删改）
        命中改写规则 → 写入新 key、删除旧 key，标记 scratch = False
    若 scratch 仍为 True（一个 key 都没改）→ 视为「从零训练」，不打 warning
    否则 → logger.warning("Weight format of ... have changed! ...")
```

#### 4.1.3 源码精读

v2 布局的「立法者」是构造函数——key 布局由这两行属性赋值决定：

[mask2former/modeling/meta_arch/mask_former_head.py:L81-L83](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/mask_former_head.py#L81-L83) —— `self.pixel_decoder = pixel_decoder` 与 `self.predictor = transformer_predictor`：v2 把像素解码器包进 `pixel_decoder` 属性，这就是旧 key 缺的那个前缀的来源。

[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:L314-L318](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L314-L318) —— `self.query_feat = nn.Embedding(num_queries, hidden_dim)`（内容嵌入）与 `self.query_embed = nn.Embedding(...)`（位置嵌入）：v1 的 `static_query` 对应这里的 `query_feat`；`query_embed` 是 v2 新增的，v1 权重里没有它（加载时 missing、随机初始化）。

而「版本号」的声明只有一行：

[mask2former/modeling/meta_arch/mask_former_head.py:L21](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/mask_former_head.py#L21) —— `_version = 2`：类属性。由 v2 代码保存的 checkpoint，其 `_metadata` 里该模块记录的 version 就是 2，加载时钩子一看版本相同便直接跳过迁移。

#### 4.1.4 代码实践

**实践目标**：亲眼确认 `_metadata` 确实随 `state_dict()` 流动、`version` 从哪里来——这是理解后面两个钩子的地基。本实践不依赖本仓库代码，任何装了 PyTorch 的机器可做。

**操作步骤**（示例代码）：

```python
import collections
import torch
from torch import nn

class MyMod(nn.Module):
    _version = 2                      # 模拟 MaskFormerHead 的版本声明
    def __init__(self):
        super().__init__()
        self.fc = nn.Linear(4, 4)     # Linear 自身 _version 默认为 1

m = MyMod()
sd = m.state_dict()
print("metadata:", getattr(sd, "_metadata", None))

sd2 = collections.OrderedDict(sd)     # 用内容重建一个新 OrderedDict
print("rebuilt has _metadata:", hasattr(sd2, "_metadata"))
```

**需要观察的现象**：

1. 第一行打印应出现 `{'': {'version': 2}, 'fc': {'version': 1}}` 一类的结构——根模块记在空串键下，子模块记在去尾点的路径键下，各自记录保存时的 `_version`。
2. 第二行应为 `False`：`OrderedDict(sd)` 拷贝条目但不拷贝实例属性，`_metadata` 丢失——这正是「手拼/重建字典会让钩子看到 version=None」的原因。

**预期结果**：确认 version 信息寄生在 `_metadata` 属性上、且非常脆弱。具体打印格式随 PyTorch 版本可能略有差异，核心结论不变（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么两个钩子都用 `for k in list(state_dict.keys())` 而不是直接 `for k in state_dict.keys()`？
**答案**：循环体内会 `state_dict[newk] = ...` 与 `del state_dict[k]`，边遍历边增删键会抛 `RuntimeError: dictionary changed size during iteration`；先 `list()` 取键快照即可安全改写。

**练习 2**：如果调用者自己 `torch.save` 了 `model.state_dict()`，之后用 `model.load_state_dict(torch.load(...))` 加载，钩子会触发吗？
**答案**：不会。这份 checkpoint 的 `_metadata` 记录的是 v2 模块自己的 `version=2`，钩子判断 `version < 2` 不成立，直接跳过迁移——这正是「保存新权重→加载」不误伤的原因。

**练习 3**：钩子改写 state_dict 后，调用者手里传入的那个字典会跟着变吗？
**答案**：不会。`load_state_dict` 一进来就 `state_dict = state_dict.copy()`（浅拷贝），钩子改的是副本；想直接观察改名结果，必须自己显式调用 `_load_from_state_dict`（见 4.2.4）。

### 4.2 MaskFormerHead._load_from_state_dict：pixel_decoder 前缀搬运

#### 4.2.1 概念说明

这是第一级钩子，解决「v1 像素解码器层直接挂在 `sem_seg_head` 下、v2 包进了 `pixel_decoder` 子模块」的失配。它的迁移策略极其朴素：**凡 key 里含 `sem_seg_head` 且不是 predictor 前缀的，统一在 head 前缀后面插一层 `pixel_decoder.`**。

注意它对 key 内容的假设很少——不枚举 v1 的具体层名（`input_proj`、lateral 卷积……一概不关心），只认 `sem_seg_head` 这个字符串和 prefix 位置。代价是它是「字符串规则」而非「语义规则」：任何恰好含该子串的 key 都会被搬运。

#### 4.2.2 核心流程

```text
条件：version 为 None 或 < 2
对每个 key k：
    命中 ⇔ ("sem_seg_head" in k) 且 NOT k.startswith("sem_seg_head." + "predictor")
    改写 ⇔ k.replace("sem_seg_head.", "sem_seg_head.pixel_decoder.")
    例：sem_seg_head.input_proj.0.0.weight
        → sem_seg_head.pixel_decoder.input_proj.0.0.weight
```

三个设计要点：

1. **排除 predictor**：`sem_seg_head.predictor.*`（Transformer 解码器）在 v1 里就叫这个名字、位置没变，不需要搬；且它们的修复归第二级钩子管，两级互不越界。
2. **`replace(prefix, prefix + "pixel_decoder.")`**：`prefix` 作为子串只在 key 开头出现一次，等价于「在模块路径中插入一层」。
3. **scratch 标志**：一个 key 都没改就不打 warning——训练新模型时经常加载只含 backbone 的初始化权重，此时 head 子树没有任何 key，属正常情况，不该吓唬用户。

#### 4.2.3 源码精读

[mask2former/modeling/meta_arch/mask_former_head.py:L21-L45](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/mask_former_head.py#L21-L45) —— 完整钩子：L21 声明 `_version = 2`；L26-L27 取版本并判断 `version is None or version < 2`（与 PyTorch `_BatchNorm` 迁移 `num_batches_tracked` 的写法同源）；L31-L39 遍历并改写 key；L41-L45 无 key 被改时不告警，否则打出 `Weight format of MaskFormerHead have changed! ... Applying automatic conversion now ...`。

[mask2former/modeling/meta_arch/mask_former_head.py:L33-L34](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/mask_former_head.py#L33-L34) —— 核心两行：命中条件 `if "sem_seg_head" in k and not k.startswith(prefix + "predictor")` 与改写 `newk = k.replace(prefix, prefix + "pixel_decoder.")`。

对照参考（同一份代码的复制粘贴变体）：

[mask2former/modeling/meta_arch/per_pixel_baseline.py:L20-L39](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/per_pixel_baseline.py#L20-L39) 与 [mask2former/modeling/meta_arch/per_pixel_baseline.py:L128-L144](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/per_pixel_baseline.py#L128-L144) —— `PerPixelBaselineHead` 与 `PerPixelBaselinePlusHead`（u8-l4 讲过的对照基线）各带一个逐字相同的钩子（Plus 版在 L140 多一行 `logger.debug` 打印新旧 key 对，仅在 DEBUG 级别可见）。可见「同一迁移规则被复制到所有受重构影响的 head」是项目通用做法，不是 `MaskFormerHead` 独有。

#### 4.2.4 代码实践

**实践目标**：构造一份带旧 key 的假 state_dict，直接调用 head 的钩子，观察「改写了哪些 key、warning 说了什么、version=2 时如何静默跳过」。

**前置**：按 u1-l2 完成环境安装并编译 MSDeformAttn 扩展（`import mask2former` 强依赖编译产物，见 [mask2former/modeling/pixel_decoder/ops/functions/ms_deform_attn_func.py:L21-L29](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/ops/functions/ms_deform_attn_func.py#L21-L29) 的硬报错）；CPU 即可，无需权重与数据集。

**操作步骤**（示例代码）：

```python
import logging, torch
logging.basicConfig(level=logging.WARNING)          # 让迁移 warning 打到终端

from detectron2.config import get_cfg
from detectron2.projects.deeplab import add_deeplab_config
from detectron2.modeling import build_model
from mask2former import add_maskformer2_config      # import 即注册（u1-l3）

cfg = get_cfg()
add_deeplab_config(cfg)
add_maskformer2_config(cfg)
cfg.merge_from_file("configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml")
cfg.freeze()
model = build_model(cfg)                            # CPU 上构建 R50 全模型
head = model.sem_seg_head

# 1) 伪造 v1 布局：把 v2 key 逆向还原成旧写法
fake = {}
for k, v in model.state_dict().items():
    if k.startswith("sem_seg_head.pixel_decoder."):
        fake[k.replace("sem_seg_head.pixel_decoder.", "sem_seg_head.")] = torch.zeros_like(v)
    elif k.startswith("sem_seg_head.predictor.query_feat."):
        fake[k.replace("query_feat", "static_query")] = torch.zeros_like(v)
    else:
        fake[k] = torch.zeros_like(v)               # backbone 等其余键原样保留

# 2) 直接调用 head 的钩子（dict 按引用传入，改写直接可见）
head._load_from_state_dict(fake, "sem_seg_head.", {"version": None}, False, [], [], [])

old_style = [k for k in fake if k.startswith("sem_seg_head.")]
print(old_style[:3])   # 还剩哪些 sem_seg_head 直挂键？
```

**需要观察的现象**：

1. 终端出现 `Weight format of MaskFormerHead have changed! Please upgrade your models. Applying automatic conversion now ...`。
2. 步骤 2 之后，`fake` 中**不再有** `sem_seg_head.input_proj...` 这类直挂键——它们都变成了 `sem_seg_head.pixel_decoder.*`；而 `sem_seg_head.predictor.static_query.weight` 原封不动（被排除条件保护，留给 4.3 的钩子）。
3. 再跑一遍，但把 `{"version": None}` 换成 `{"version": 2}`：无 warning、无任何 key 变化。

**预期结果**：version 元数据单方面决定钩子是否动手；两条改写规则精确命中各自的 key 集合。具体键名清单依配置而异（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：若把一份 **v2 布局**的 state_dict 丢掉 `_metadata` 后传入（version 变 None），这个钩子会发生什么？
**答案**：`sem_seg_head.pixel_decoder.xxx` 含 `sem_seg_head` 且不以 `sem_seg_head.predictor` 开头，会被改写成 `sem_seg_head.pixel_decoder.pixel_decoder.xxx`——双重前缀的错 key，原键被删，这批权重随 strict=False 的 unexpected 通道被丢弃（同时打出 warning）。教训：**随手重建 state_dict 字典会破坏 `_metadata`，进而触发错误迁移**；保留 `state_dict()` 原对象或正确传递 `_metadata` 才安全。

**练习 2**：为什么钩子扫描的是整份 state_dict（含 backbone 的 key），而不是只扫 `sem_seg_head.` 开头的？
**答案**：PyTorch 的分发机制使然——每个模块的钩子收到的都是全模型共享的那份 state_dict 副本，模块靠自己的 `prefix` 与字符串条件从中筛出自己的 key；这也解释了为什么必须显式排除 `predictor` 前缀（别人的 key 也在同一份字典里）。

**练习 3**：`scratch` 标志设为 True 的初始含义是什么？
**答案**：「至今没有任何 key 被改写」。若循环结束它仍为 True，说明这次加载根本没涉及旧布局的 key（典型场景：从零训练、只加载 backbone 预训练权重），于是跳过 warning，避免误报。

### 4.3 MultiScaleMaskedTransformerDecoder._load_from_state_dict：static_query → query_feat

#### 4.3.1 概念说明

第二级钩子解决的是**改名**而非挪位：v1 的可学习 query 嵌入叫 `static_query`（一个 `nn.Embedding`），v2 在架构上把它升级为「内容 + 位置」双嵌入 `query_feat` + `query_embed`（u4-l3 讲过：`query_feat` 在进解码层前先过预测头、产出受监督的第 0 组预测，因此实建层数为 `DEC_LAYERS−1`，见 [mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:L349-L355](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L349-L355) 的注释）。

迁移只搬内容嵌入：`static_query.weight` → `query_feat.weight`。`query_embed` 是 v2 新增参数，旧权重里没有，加载后保持随机初始化——这类「结构性新增」不属于 key 改名能解决的问题。

#### 4.3.2 核心流程

```text
条件：version 为 None 或 < 2（与第一级相同）
对每个 key k：
    命中 ⇔ "static_query" in k
    改写 ⇔ k.replace("static_query", "query_feat")
    例：sem_seg_head.predictor.static_query.weight
        → sem_seg_head.predictor.query_feat.weight
其余同 4.2.2（scratch 标志 + warning 文案，类名不同）
```

与前一级的关键差别：改写条件**完全不看 prefix**，只认 `static_query` 子串。在全模型里只有 predictor 拥有这个属性名，所以安全；但作为通用习惯这比 4.2 的写法更「激进」——如果你的某个新模块恰好叫 `static_query_xxx`，加载 v1 权重（或无 metadata 的字典）时会被误改（本项目当前不存在此冲突）。

#### 4.3.3 源码精读

[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:L210-L233](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L210-L233) —— 完整钩子：L210 `_version = 2`；L215-L216 版本判定；L222-L223 两行核心改写 `if "static_query" in k: newk = k.replace("static_query", "query_feat")`；L229-L233 warning（类名为 `MultiScaleMaskedTransformerDecoder`）。

[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:L384-L386](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L384-L386) —— `forward` 开头的用法证据：`query_embed = self.query_embed.weight...`（位置嵌入，传给 `query_pos`）与 `output = self.query_feat.weight...`（内容嵌入，作为初始 token 并在 L392 立即过 `forward_prediction_heads` 产出第 0 组预测）。v2 双嵌入的语义在此落地，也解释了为何迁移目标是 `query_feat` 而非 `query_embed`。

#### 4.3.4 代码实践

**实践目标**：在 4.2.4 脚本基础上补完第二级：先验证解码器钩子独立完成改名，再对**完整模型**做一次 `load_state_dict(strict=False)`，把两级接力、warning 与 incompatible 报告一次看全。

**操作步骤**（示例代码，接 4.2.4 的 `model` 与 `fake`）：

```python
# 1) 只调解码器钩子，验证 static_query 改名
fake2 = {"sem_seg_head.predictor.static_query.weight": torch.zeros(100, 256)}
model.sem_seg_head.predictor._load_from_state_dict(
    fake2, "sem_seg_head.predictor.", {"version": None}, False, [], [], [])
print(fake2.keys())        # 期望只剩 query_feat 键

# 2) 整模型级验证：fake（4.2.4 已造好的 v1 布局）直接交给 load_state_dict
fake_all = dict(fake); fake_all.update(fake2)          # 汇总一份完整 v1 假 checkpoint
result = model.load_state_dict(fake_all, strict=False)
print("missing:", len(result.missing_keys), result.missing_keys[:3])
print("unexpected:", len(result.unexpected_keys), result.unexpected_keys[:3])
```

**需要观察的现象**：

1. 步骤 1 打印只剩 `sem_seg_head.predictor.query_feat.weight`，并出现类名为 `MultiScaleMaskedTransformerDecoder` 的 warning。
2. 步骤 2 同时出现**两条** warning（head 与解码器各一条）；`missing_keys` 基本只剩 `sem_seg_head.predictor.query_embed.weight` 这类 v2 新增参数（若 fake 未覆盖它）；`unexpected_keys` 应接近 0——两级钩子已把旧 key 全部翻译成模型能认的写法。

**预期结果**：两级钩子在真实加载路径中自动接力完成迁移；剩下的 missing/unexpected 全部来自「结构性新增参数」，属 strict=False 的正常处理范围。具体清单长度依配置（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：head 的钩子为什么必须排除 `predictor` 前缀，而解码器的钩子不需要排除任何前缀？
**答案**：head 的规则是「给含 `sem_seg_head` 的 key 插 `pixel_decoder.` 前缀」，若不排除，predictor 的 key 会被错搬成 `sem_seg_head.pixel_decoder.predictor.*`；解码器的规则是「改 `static_query` 子串」，该子串只出现在 predictor 自己的 key 里，天然无冲突。

**练习 2**：加载 v1 权重到 v2 代码后，`query_embed` 的值是什么？这对微调意味着什么？
**答案**：随机初始化（missing key 走 strict=False 的保留通道）。意味着 v1→v2 的迁移加载适合作为**热启动**（backbone、像素解码器、解码层、query_feat 都已就位），但需要重新训练让 `query_embed`（以及任何层数差异带来的新参数）收敛，不能当作无损恢复。

**练习 3**：迁移后 `sem_seg_head.predictor.query_feat.weight` 的形状是 `(num_queries, hidden_dim)`。若 v1 模型用了 100 个 query、v2 配置改成 200，钩子能救吗？
**答案**：不能。钩子只改 key 名，不做形状检查与裁剪；形状不一致的张量在 `load_state_dict` 的拷贝环节报 shape mismatch（进入 `error_msgs`/incompatible 报告）。换 query 数量必须重新训练或手工调整张量。

### 4.4 官方转换脚本与 DetectionCheckpointer 的部分加载

#### 4.4.1 概念说明

迁移钩子解决「自家新旧版本」的兼容；另一类需求是**引入外部的 ImageNet 分类权重**（torchvision 的 ResNet、微软官方的 Swin）做 backbone 初始化。两者是不同的工具：

- **`tools/convert-torchvision-to-d2.py`（改 key）**：torchvision ResNet 的命名（`layer1`、`bn1`、`downsample.0`）与 detectron2 的命名（`res2`、`conv1.norm`、`shortcut`）完全两套，脚本逐条机械翻译。
- **`tools/convert-pretrained-swin-model-to-d2.py`（不改 key）**：因为 `D2SwinTransformer`（u3-l3）移植时**原样保留了上游实现的参数命名**，key 本身已对齐，脚本只做「换壳」——把官方 `.pth` 里的 `"model"` 抠出来，包上 detectron2 pkl 的三个字段。

两个脚本产出的 pkl 都带 `matching_heuristics: True`，把「无 `backbone.` 前缀的分类权重如何对上 `backbone.*` 的模型 key」交给 `DetectionCheckpointer` 的名字后缀 + 形状一致对齐去完成，最终以 `strict=False` 落盘：分类头（`fc`、Swin 的分类相关层）与 detectron2 侧没有的层成为 unexpected/missing，被丢弃或保持初始化。

#### 4.4.2 核心流程

`convert-torchvision-to-d2.py` 的 key 映射规则（对每个 key 顺序应用）：

```text
1. 若 key 不含 "layer"        → 加前缀 "stem."        （conv1/bn1 属于 stem）
2. layer{t} → res{t+1}        （t = 1..4；layer1→res2 ... layer4→res5）
3. bn{t} → conv{t}.norm       （t = 1..3；detectron2 把 norm 挂在 conv 属性下）
4. downsample.0 → shortcut    （ResBlock 的 1×1 降采样卷积）
5. downsample.1 → shortcut.norm
随后：张量 .detach().numpy()，连同
      {"model": ..., "__author__": "torchvision", "matching_heuristics": True}
      一并 pickle 落盘
```

Swin 脚本全程只有三步：`torch.load(...)["model"]` → 包 `{"model": obj, "__author__": "third_party", "matching_heuristics": True}` → pickle 落盘。

`DetectionCheckpointer` 一侧（detectron2 提供，行为概述）：

```text
load(path)
 ├─ _load_file：pkl → unpickle → "model"（numpy 数组转回 tensor）
 └─ _load_model：
     ├─ 若 matching_heuristics=True → 按名字（精确→后缀）+ 形状一致对齐 checkpoint key 与模型 key
     └─ model.load_state_dict(..., strict=False) → 部分加载，missing/unexpected 记日志
```

（对齐算法在 detectron2 的 `checkpoint/c2_model_loading.py`（`align_and_update_state_dicts`），加载入口在 `checkpoint/detection_checkpoint.py`；本仓库未 vendor 这两个文件，细节以你本地安装的 detectron2 源码为准，标注：待确认的实现细节。）

#### 4.4.3 源码精读

[mask2former/modeling/../../tools/convert-torchvision-to-d2.py:L33-L44](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/convert-torchvision-to-d2.py#L33-L44) —— 即 [tools/convert-torchvision-to-d2.py:L33-L44](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/convert-torchvision-to-d2.py#L33-L44)：映射主循环。L35-L36 不含 `layer` 的键加 `stem.`；L37-L38 `layer{t}→res{t+1}`；L39-L40 `bn{t}→conv{t}.norm`；L41-L42 两行 downsample → shortcut；L43 逐条打印 `旧 key -> 新 key`（脚本自带审计输出）；L44 `obj.pop(old_k).detach().numpy()`——边迁移边清空原字典，最后剩下的就是「未转换 key」。

[tools/convert-torchvision-to-d2.py:L46-L51](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/convert-torchvision-to-d2.py#L46-L51) —— 产出 detectron2 pkl 的三字段（`model`/`__author__`/`matching_heuristics`）并落盘；若 `obj` 非空则打印 `Unconverted keys` 兜底提示。

[tools/convert-torchvision-to-d2.py:L9-L25](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/convert-torchvision-to-d2.py#L9-L25) —— 文档字符串里的使用说明：除 `MODEL.WEIGHTS` 指向转换产物外，还要求同步修改 `PIXEL_MEAN`/`PIXEL_STD`、`RESNETS.DEPTH`、`RESNETS.STRIDE_IN_1X1: False` 与 `INPUT.FORMAT: "RGB"`。**权重转换不只是改 key**：torchvision 的预处理约定（RGB、ImageNet 均方差、下采样放在 3×3 卷积的 stride）与 detectron2 默认（BGR、MSRA 均值、stride 在第一个 1×1 卷积）不同，配置不对齐时权重再准也会掉点。

[tools/convert-pretrained-swin-model-to-d2.py:L25-L30](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/convert-pretrained-swin-model-to-d2.py#L25-L30) —— 全部三行逻辑：`torch.load(input, map_location="cpu")["model"]` 取官方权重的模型部分（官方 `.pth` 顶层还带 optimizer/epoch 等），包上 `"__author__": "third_party"` 与 `matching_heuristics` 后落盘。与 torchvision 脚本对照着读，「改 key」与「不改 key」两种策略一目了然——能不改的前提是移植代码时冻结了参数命名（u3-l3 的 `D2SwinTransformer` 适配器）。

[MODEL_ZOO.md:L28-L30](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/MODEL_ZOO.md#L28-L30) —— 「第三方 ImageNet 预训练模型」一节：论文所用非 detectron2 自带的预训练权重，指引读者到 MaskFormer 仓库的 tools 目录获取——即本节两个脚本服务的对象。

#### 4.4.4 代码实践

**实践目标**：离线跑通 torchvision 转换脚本（无需下载真权重、无需 GPU），核对手写推导的 key 映射表与脚本实际输出是否一致。

**操作步骤**（示例代码）：

```python
# 第一步：伪造一份 torchvision 风格的 ResNet checkpoint（只造名字，数值无意义）
import torch
from collections import OrderedDict
fake_tv = OrderedDict([
    ("conv1.weight", torch.zeros(64, 3, 7, 7)),
    ("bn1.weight", torch.ones(64)), ("bn1.running_var", torch.ones(64)),
    ("layer1.0.conv1.weight", torch.zeros(64, 64, 1, 1)),
    ("layer1.0.bn1.weight", torch.ones(64)),
    ("layer1.0.downsample.0.weight", torch.zeros(64, 64, 1, 1)),
    ("layer1.0.downsample.1.weight", torch.ones(64)),
    ("fc.weight", torch.zeros(1000, 2048)),
])
torch.save(fake_tv, "/tmp/fake_r50.pth")
```

```bash
# 第二步：跑仓库自带脚本（只会打印映射与生成 pkl，不碰网络）
python tools/convert-torchvision-to-d2.py /tmp/fake_r50.pth /tmp/fake_r50.pkl
```

```python
# 第三步：检查产物结构
import pickle
res = pickle.load(open("/tmp/fake_r50.pkl", "rb"))
print(res["__author__"], res["matching_heuristics"])
print(sorted(res["model"].keys()))
```

**需要观察的现象**（脚本第二步逐行打印 `旧 -> 新`）：

| 旧 key（torchvision） | 新 key（脚本输出） |
| --- | --- |
| `conv1.weight` | `stem.conv1.weight` |
| `bn1.weight` | `stem.conv1.norm.weight` |
| `layer1.0.conv1.weight` | `res2.0.conv1.weight` |
| `layer1.0.bn1.weight` | `res2.0.conv1.norm.weight` |
| `layer1.0.downsample.0.weight` | `res2.0.shortcut.weight` |
| `layer1.0.downsample.1.weight` | `res2.0.shortcut.norm.weight` |
| `fc.weight` | `stem.fc.weight`（见练习 1） |

**预期结果**：第三步打印 `torchvision True`，key 集合与上表一致；注意所有新 key 都**没有** `backbone.` 前缀——这一步是留给 `DetectionCheckpointer` 的 matching_heuristics 完成的（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：`fc.weight`（分类全连接层）会被映射成什么？最终能加载进模型吗？
**答案**：它不含 `layer`，会被加上 `stem.` 前缀变成 `stem.fc.weight`——这是规则 1 的「误伤」。它加载不进任何模型 key，最终作为 unexpected key 被 `strict=False` 忽略；脚本作者显然知道并接受这一点（分类头本来就不该迁移）。

**练习 2**：为什么 Swin 脚本不需要（也没有）任何 key 映射逻辑，而 torchvision 脚本需要 5 条规则？
**答案**：`D2SwinTransformer` 是「适配器式移植」，内部 `SwinTransformer` 的参数命名与上游官方实现逐字一致，所以只需要换壳（取 `"model"`、加元信息）；而 detectron2 的 ResNet 命名体系与 torchvision 是两套独立演化（`res2` vs `layer1`、norm 挂卷积下 vs 独立 `bn`），必须显式翻译。**冻结命名以换取零成本权重迁移**，是 u3-l3「接口命名的兼容性价值高于实现本身」的又一例证。

**练习 3**：两个脚本都写 `"matching_heuristics": True`，这个开关大概解决了什么问题？关掉会怎样（推理即可）？
**答案**：解决「checkpoint key 无 `backbone.` 前缀 / 前缀体系不一致」与模型 key 的对齐问题——按名字后缀与形状一致把分类权重挂到模型的 `backbone.*` 上。若关掉（或不具备该机制），无前缀 key 将全部 unmatched，backbone 只能随机初始化，转换脚本也就白做了。

## 5. 综合实践：写一个「checkpoint 体检器」

把本讲三块知识串成一个脚本 `ckpt_doctor.py`（示例代码，放在仓库外任意目录运行），给定一份 state_dict，输出体检报告：

1. **构建标尺模型**：按 4.2.4 的方式从 `configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml` 构建模型（需 u1-l2 环境，CPU 即可）。
2. **判定年代**：检查输入字典有无 `_metadata`、`sem_seg_head` 直挂 key、`static_query` key，输出判定（v1 布局 / v2 布局 / 命名未知）。
3. **执行迁移**：依序直接调用 `model.sem_seg_head._load_from_state_dict(sd, "sem_seg_head.", {"version": None}, ...)` 与 predictor 的同名钩子（模拟一次 version<2 的加载），打印被改写的前 10 个 `旧 -> 新` 对照。
4. **对账**：`model.load_state_dict(sd, strict=False)`，统计并分类 missing（预期只剩 v2 新增参数如 `query_embed`、`pixel_decoder.encoder.*` 的层数差）/ unexpected（分类头、无法识别的键）数量。
5. **自检**：用 `model.state_dict()` 自己喂给自己（应零迁移、零 missing/unexpected），再用 `OrderedDict(model.state_dict())` 重建后喂入（应触发 4.2.5 练习 1 的双重前缀事故）——把「`_metadata` 脆弱性」变成可复现的实验。

预期产出：一份能区分「名字失配（可自动修）」与「结构失配（只能部分加载）」的报告。这正是排查「权重加载了但指标不对」类问题的第一手工具。

## 6. 本讲小结

- PyTorch 的 `load_state_dict` 会**逐模块**分发 `_load_from_state_dict` 钩子并传递共享的 state_dict 副本；模块可借钩子在加载瞬间改写 key，`_version` 元数据（寄生在 `_metadata` 属性、随保存动作记录）决定迁移是否触发。
- `MaskFormerHead`（version 2）把 v1 直挂 `sem_seg_head` 下的像素解码器 key 统一插入 `pixel_decoder.` 前缀，排除 `predictor` 前缀留给下一级；`MultiScaleMaskedTransformerDecoder`（同为 version 2）把 `static_query` 改名 `query_feat`——两级接力修复 v1→v2 的全部命名失配，均带 scratch 标志避免「从零训练」误报警告。
- 钩子只修**名字**不修**结构**：v2 新增参数（`query_embed` 等）与层数差异走 `strict=False` 的 missing/unexpected 通道，保持随机初始化或被忽略——迁移加载是热启动，不是无损恢复。
- `_metadata` 极易丢失（重建字典即失），丢了对 v2 权重反而会触发**双重前缀**的错误迁移；搬运 state_dict 时要保留原对象或显式带上 `_metadata`。
- 两个官方脚本代表两种策略：torchvision ResNet 需要 5 条规则改 key（`layer→res`、`bn→conv.norm`、`downsample→shortcut`、stem 前缀）；Swin 因移植时冻结了上游命名而**零改名**。两者都以 `{"model", "__author__", "matching_heuristics"}` 格式落盘，把无前缀对齐交给 `DetectionCheckpointer`，最终一律 `strict=False` 部分加载；torchvision 权重还要求同步改 `PIXEL_MEAN/STD`、`STRIDE_IN_1X1`、`INPUT.FORMAT` 等预处理配置。

## 7. 下一步学习建议

至此单元十（也是整册手册的最后一个单元）的机制类主题已讲完。建议收尾路径：

1. **横向对照**：阅读你本地 detectron2 安装的 `checkpoint/detection_checkpoint.py`（`_load_file`/`_load_model`）与 `checkpoint/c2_model_loading.py`（`align_and_update_state_dicts`），把本讲 4.4.2 流程图中标注「待确认」的两处补成实证——特别是 metadata 在 checkpointer 路径下如何流转、迁移钩子与后缀对齐的先后次序。
2. **回看迁移现场**： MaskFormer（v1）仓库的 `MODEL_ZOO` 与模型代码，找出真实的 v1 key（`sem_seg_head` 直挂层名、`static_query`），验证本讲由 v2 排除条件反推的 v1 布局。
3. **串读全册**：沿「u3-l2 组装器 → u3-l3 Swin 命名冻结 → u4-l3 双 query 嵌入 → 本讲版本迁移」重读一遍，体会「架构重构」与「权重兼容」这对矛盾如何在 key 命名上达成妥协；若要继续实践，可把第 5 节的体检器扩展成支持视频包（`mask2former_video`）checkpoint 的版本。

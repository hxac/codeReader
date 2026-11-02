# u1-l5 配置系统：add_maskformer2_config 与 yaml 继承

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 Mask2Former 一份最终生效配置的「五层叠加」顺序：`get_cfg()` 默认值 → `add_deeplab_config` → `add_maskformer2_config` → yaml 文件（含 `_BASE_` 继承链）→ 命令行 `merge_from_list`，并理解「后合并的覆盖先合并的」。
2. 逐组说出 `add_maskformer2_config` 注册了哪些 Mask2Former 专属配置项，它们分别控制模型、数据还是训练超参。
3. 记住两个最关键的配置节点：`MODEL.MASK_FORMER`（meta arch 行为）与 `MODEL.SEM_SEG_HEAD`（头部/像素解码器），知道它们分别被哪些代码读取。
4. 会写一个继承现有 yaml 的 mini 配置，用脚本打印并 diff 出「最终配置 vs 基线配置」的差异项。

本讲不涉及任何模型计算，只讲「配置如何变成 `cfg` 这棵树」，但它是后续所有单元（改 backbone、改解码器、改损失权重）的地基。

## 2. 前置知识

- **配置树 CfgNode**：detectron2 的配置对象 `cfg` 本质是一棵嵌套字典树（Python 的 `CfgNode`，衍生自 yacs 库）。访问键用点号链，例如 `cfg.MODEL.MASK_FORMER.NUM_OBJECT_QUERIES`。你可以把它想象成「一个能用点号访问的深层 dict」。
- **默认值**：`get_cfg()` 返回的 `cfg` 已经装好了 detectron2 全部内置默认键（如 `MODEL.META_ARCHITECTURE` 默认是 `"GeneralizedRCNN"`）。所以任何键在覆盖前都有值。
- **注册扩展键**：Mask2Former 是 detectron2 的插件项目，detectron2 默认配置里当然没有 `MODEL.MASK_FORMER` 这种键。插件项目通过 `add_maskformer2_config(cfg)` 这类函数，往 `cfg` 树上「补种」自己的键和默认值。这就是 [u1-l3](u1-l3-repo-structure.md) 讲过的「import 即注册」的配置侧对应物。
- **yaml**：一种层级缩进的数据格式。Mask2Former 用 yaml 描述每个实验配置，缩进对应 `cfg` 树的层级。
- **`_BASE_` 继承**：一个 yaml 顶部写 `_BASE_: 某yaml`，表示「先加载那个 yaml，再把本文件的内容覆盖上去」。公共配置抽到 Base 文件，实验差异留在子文件。
- **冻结**：`cfg.freeze()` 之后配置只读。detectron2 的入口脚本在合并完全部来源后才冻结，保证训练过程中配置不被意外修改。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [mask2former/config.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py) | 定义 `add_maskformer2_config`，向 `cfg` 注册全部 Mask2Former 专属键（本讲主角） |
| [configs/coco/panoptic-segmentation/Base-COCO-PanopticSegmentation.yaml](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/Base-COCO-PanopticSegmentation.yaml) | COCO 全景分割任务的公共 Base 配置：backbone、数据集、solver、输入尺寸 |
| [configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml) | R50 基线实验配置，`_BASE_` 继承上一个文件，补充 `MODEL.META_ARCHITECTURE` 与两大节点 |
| [train_net.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py) / [demo/demo.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/demo/demo.py) | 真实调用五层合并的两个入口（训练与推理） |
| [mask2former_video/config.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/config.py) | 视频版对照：`add_maskformer2_video_config` 再往同一棵 `cfg` 上追加视频专属键 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. CfgNode 体系与五层合并顺序
2. `add_maskformer2_config` 逐段精读
3. yaml 的 `_BASE_` 继承
4. 两个关键节点与 `merge_from_list` 命令行覆盖

### 4.1 CfgNode 体系与五层合并顺序

#### 4.1.1 概念说明

[u1-l4](u1-l4-first-inference-demo.md) 已经提到配置是「五层叠加」，本讲把它落到源码。为什么要分层？因为三个来源各司其职：

- detectron2 默认值提供「通用骨架」（1000+ 个键，训练循环全靠它们）；
- 插件的 `add_*_config` 提供「本项目新增的键」（detectron2 不认识 `MODEL.MASK_FORMER`）；
- yaml 提供「单个实验的取值」；命令行提供「本次运行的一次性微调」。

关键规则只有一条：**后合并的覆盖先合并的**。所以扩展函数必须先于 yaml 调用——如果 yaml 里出现了 `cfg` 树上不存在的键，合并就会失败（没有「先注册后覆盖」就没有合法的覆盖目标）。

#### 4.1.2 核心流程

```text
cfg = get_cfg()                    # 第 1 层：detectron2 全部默认键
add_deeplab_config(cfg)            # 第 2 层：DeepLab 项目键（poly 学习率、SEM_SEG_HEAD 部分键等）
add_maskformer2_config(cfg)        # 第 3 层：Mask2Former 专属键（本讲 4.2）
cfg.merge_from_file(yaml)          # 第 4 层：先解析 _BASE_ 链，Base 先进、子文件后进
cfg.merge_from_list(opts)          # 第 5 层：命令行键值对，优先级最高
cfg.freeze()                       # 冻结，之后只读
```

训练入口与 demo 入口的代码几乎一字不差，说明这套顺序是项目的固定惯例。

#### 4.1.3 源码精读

训练入口 `setup()` 中的五层合并与冻结：

- [train_net.py:281-295](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L281-L295)：`get_cfg()` 后依次 `add_deeplab_config` → `add_maskformer2_config` → `merge_from_file(args.config_file)` → `merge_from_list(args.opts)`，最后 `cfg.freeze()`。注意第 286 行注释「for poly lr schedule」——引入 deeplab 配置层是为了用它的 poly 学习率调度器（详见 u2-l4）。

demo 入口 `setup_cfg()` 的同款五层：

- [demo/demo.py:34-42](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/demo/demo.py#L34-L42)：与训练入口逐行对应，只是没有 `default_setup`（不初始化分布式日志）。u1-l4 里你用过的 `--opts MODEL.DEVICE cpu` 正是在第 5 层生效。

`add_maskformer2_config` 是从包根导出的公开 API：

- [mask2former/__init__.py:6](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/__init__.py#L6)：`from .config import add_maskformer2_config`，所以外部统一 `from mask2former import add_maskformer2_config`。

视频版是在同一段代码后面再叠一层：

- [train_net_video.py:247-251](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net_video.py#L247-L251)：`add_deeplab_config` → `add_maskformer2_config` → `add_maskformer2_video_config` → yaml → 命令行。插件一层层往上「贴补丁」，互不冲突。

#### 4.1.4 代码实践

1. **实践目标**：亲眼看到五层中每一层对 `cfg` 的改变。
2. **操作步骤**（示例代码，可在仓库根目录外的任意目录保存运行，需已安装 detectron2 与本仓库源码可 import）：

```python
# 示例代码：five_layers.py
from detectron2.config import get_cfg
from detectron2.projects.deeplab import add_deeplab_config
from mask2former import add_maskformer2_config

cfg = get_cfg()
print("第1层后 META_ARCHITECTURE =", cfg.MODEL.META_ARCHITECTURE)
print("第1层后有 MASK_FORMER 吗 =", hasattr(cfg.MODEL, "MASK_FORMER"))

add_deeplab_config(cfg)
print("第2层后 SOLVER 键数 =", len(cfg.SOLVER.keys()))

add_maskformer2_config(cfg)
print("第3层后 MASK_FORMER.NUM_OBJECT_QUERIES =", cfg.MODEL.MASK_FORMER.NUM_OBJECT_QUERIES)

cfg.merge_from_file(
    "configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml")
print("第4层后 META_ARCHITECTURE =", cfg.MODEL.META_ARCHITECTURE)

cfg.merge_from_list(["MODEL.MASK_FORMER.NUM_OBJECT_QUERIES", "200"])
print("第5层后 NUM_OBJECT_QUERIES =", cfg.MODEL.MASK_FORMER.NUM_OBJECT_QUERIES)
```

3. **需要观察的现象**：`META_ARCHITECTURE` 从默认的 `"GeneralizedRCNN"`（detectron2 的检测模型）在第 4 层后变成 `"MaskFormer"`；`MASK_FORMER` 节点在第 3 层之前不存在；第 5 层把 100 改成 200。
4. **预期结果**：以上三个断言都成立。具体打印文案与 detectron2 版本有关，待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `cfg.merge_from_file(...)` 放到 `add_maskformer2_config(cfg)` 之前会发生什么？
**答案**：yaml 里出现 `MODEL.MASK_FORMER.*` 等键时，`cfg` 树上还没有这些键。合并时 detectron2 会因未知键报错（或至少无法按预期覆盖），因此扩展函数必须先注册、yaml 后合并。

**练习 2**：为什么 `add_deeplab_config` 和 `add_maskformer2_config` 都存在，而不是合并成一个？
**答案**：前者属于 detectron2 的 deeplab 项目（提供 poly 学习率调度与部分头部键），后者属于 Mask2Former 插件。分层意味着职责清晰：detectron2 升级不会破坏插件键，其他插件项目也可以只用其中一层。

**练习 3**：`cfg.freeze()` 去掉行不行？
**答案**：功能上大概率仍能跑，但失去了「配置在训练中被意外篡改」的保护。detectron2 的惯例是合并完立刻冻结，保证训练循环、dataloader、模型构建读到的是同一份不可变配置。

### 4.2 add_maskformer2_config 逐段精读

#### 4.2.1 概念说明

[mask2former/config.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py) 全文只有一个函数：向 `cfg` 树补种 Mask2Former 专属键。读懂这个文件，你就掌握了「改任何超参应该动哪个键」。函数体内按功能分成六段，注释里还标注了历史：`NOTE: configs from original maskformer`（继承自一代 MaskFormer）与 `NOTE: maskformer2 extra configs`（二代新增）。

#### 4.2.2 核心流程

函数按顺序注册六个功能域的键：

```text
INPUT（数据采样与增强）
  └─ SOLVER（优化器相关）
      └─ MODEL.MASK_FORMER（meta arch 行为：损失权重/Transformer/推理开关/采样点）
          └─ MODEL.SEM_SEG_HEAD（头部：mask 维度、像素解码器名、可变形注意力）
              └─ MODEL.SWIN（Swin backbone 专属）
                  └─ LSJ 增强 + MSDeformAttn encoder + point loss（二代新增）
```

其中 `cfg.MODEL.MASK_FORMER = CN()` 与 `cfg.MODEL.MASK_FORMER.TEST = CN()` 会创建**新的子树**；`cfg.MODEL.SEM_SEG_HEAD.MASK_DIM = 256` 则是往 detectron2 默认已存在的 `SEM_SEG_HEAD` 节点上**追加键**（该节点的基础键如 `NAME`、`IN_FEATURES`、`NUM_CLASSES` 来自 detectron2 默认配置与 `add_deeplab_config`，可用 `cfg.MODEL.SEM_SEG_HEAD.keys()` 自行打印验证）。

#### 4.2.3 源码精读

数据与优化器段（一代遗留）：

- [mask2former/config.py:11-20](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L11-L20)：注册 `INPUT.DATASET_MAPPER_NAME`（选择用哪个 dataset mapper，u2-l3 的开关）、`COLOR_AUG_SSD`、`CROP.SINGLE_CATEGORY_MAX_AREA`、`SIZE_DIVISIBILITY`。
- [mask2former/config.py:22-27](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L22-L27)：`SOLVER.WEIGHT_DECAY_EMBED`、`OPTIMIZER = "ADAMW"`、`BACKBONE_MULTIPLIER = 0.1`（backbone 学习率乘 0.1，u2-l4 展开）。

`MODEL.MASK_FORMER` 主节点：

- [mask2former/config.py:29-37](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L29-L37)：创建节点并注册 5 个损失权重键（`DEEP_SUPERVISION`、`NO_OBJECT_WEIGHT`、`CLASS_WEIGHT`、`DICE_WEIGHT`、`MASK_WEIGHT`），它们在 u5 单元被 `SetCriterion` 与 `weight_dict` 消费。
- [mask2former/config.py:39-51](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L39-L51)：Transformer 结构键（`NHEADS`、`DEC_LAYERS`、`HIDDEN_DIM`、`NUM_OBJECT_QUERIES`、`TRANSFORMER_IN_FEATURE` 等），被 Transformer 解码器的 `from_config` 消费（u4-l3）。
- [mask2former/config.py:53-64](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L53-L64)：推理子节点 `MODEL.MASK_FORMER.TEST`：三个任务开关 `SEMANTIC_ON` / `INSTANCE_ON` / `PANOPTIC_ON`（u1-l4 已用）、两个全景阈值（u6-l3）以及 `SEM_SEG_POSTPROCESSING_BEFORE_INFERENCE`（u6 单元）；还有兜底的 `SIZE_DIVISIBILITY = 32`（backbone 自身 `size_divisibility` 为 0 时用它 padding 图像）。

`MODEL.SEM_SEG_HEAD` 追加键与 Swin 节点：

- [mask2former/config.py:66-71](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L66-L71)：`MASK_DIM`（mask 特征通道数）、`TRANSFORMER_ENC_LAYERS`、`PIXEL_DECODER_NAME`（像素解码器注册名，u3-l4/u3-l5、u8-l2 扩展点）。
- [mask2former/config.py:73-90](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L73-L90)：整棵 `MODEL.SWIN` 子树（patch 大小、各 stage 深度/头数、窗口大小等），仅当 `MODEL.BACKBONE.NAME` 指向 Swin 时被读取（u3-l3）。

二代（maskformer2）新增段：

- [mask2former/config.py:92-104](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L92-L104)：`TRANSFORMER_DECODER_NAME`（Transformer 解码器注册名，u8-l3 扩展点）、LSJ 增强的 `INPUT.IMAGE_SIZE/MIN_SCALE/MAX_SCALE`、MSDeformAttn encoder 的三个键（输入特征层、采样点数、头数）。
- [mask2former/config.py:106-114](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L106-L114)：point loss 三参数：`TRAIN_NUM_POINTS = 112 * 112`（训练时按点采样算 mask 损失）、`OVERSAMPLE_RATIO = 3.0`（PointRend 论文的 \(k\)）、`IMPORTANCE_SAMPLE_RATIO = 0.75`（重要性采样比例 \(\beta\)）。u5-l2 会精读这三个键如何进入采样器。

#### 4.2.4 代码实践

1. **实践目标**：用「前后差集」把 `add_maskformer2_config` 注册的键完整列出来，验证 4.2.3 的分组。
2. **操作步骤**（示例代码）：

```python
# 示例代码：diff_keys.py
from detectron2.config import get_cfg
from detectron2.projects.deeplab import add_deeplab_config
from mask2former import add_maskformer2_config

def flatten(node, prefix=""):
    keys = set()
    for k, v in node.items():
        full = f"{prefix}{k}"
        keys.add(full)
        if hasattr(v, "keys"):          # 子树递归
            keys |= flatten(v, full + ".")
    return keys

cfg = get_cfg()
add_deeplab_config(cfg)
before = flatten(cfg)
add_maskformer2_config(cfg)
after = flatten(cfg)

new_keys = sorted(after - before)
print(f"新增键共 {len(new_keys)} 个：")
for k in new_keys:
    print(" ", k)
```

3. **需要观察的现象**：输出应包含 `MODEL.MASK_FORMER.*`（含 `TEST` 子树）、`MODEL.SEM_SEG_HEAD.*` 追加键、`MODEL.SWIN.*`、`INPUT.*` 与 `SOLVER.*` 的新键。
4. **预期结果**：新增键总数在 40 个左右（含 `MODEL.SWIN` 约 17 个）；`flatten` 无法区分「新增节点」与「追加键」，但逐条都能在 [mask2former/config.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py) 里找到对应行。精确数量待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：想把训练时的采样点从 12544 降到 4900，应该改哪个键？
**答案**：`MODEL.MASK_FORMER.TRAIN_NUM_POINTS`（[mask2former/config.py:108](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L108)）。注意它注册时默认是 `112 * 112`，yaml 里通常直接写数值 12544。

**练习 2**：`MODEL.SEM_SEG_HEAD.PIXEL_DECODER_NAME` 与 `MODEL.MASK_FORMER.TRANSFORMER_DECODER_NAME` 分别控制什么？
**答案**：前者从 `SEM_SEG_HEADS_REGISTRY` 选像素解码器（如 `MSDeformAttnPixelDecoder`）；后者从项目自建的 `TRANSFORMER_DECODER_REGISTRY` 选 Transformer 解码器（如 `MultiScaleMaskedTransformerDecoder`）。两者是 u8 单元两个最主要的扩展点。

**练习 3**：`cfg.MODEL.MASK_FORMER = CN()` 这行为什么是必须的？
**答案**：detectron2 默认配置里没有 `MASK_FORMER` 节点，必须先创建空子树，后面 `cfg.MODEL.MASK_FORMER.DEEP_SUPERVISION = True` 这类行才有地方挂键；否则会抛 `AttributeError`（CfgNode 不允许对未知属性赋值）。

### 4.3 yaml 的 `_BASE_` 继承

#### 4.3.1 概念说明

一份实验配置通常只写「与公共配置不同的部分」，公共部分抽到 Base 文件。`_BASE_` 的语义是「先加载被继承文件的完整内容，再用本文件按键覆盖」。两个要点：

- **覆盖是按键的，不是整棵子树替换**：子 yaml 写了 `MODEL.MASK_FORMER.TEST.SEMANTIC_ON`，只会覆盖这一个叶子，Base 里 `MODEL.BACKBONE.*` 等兄弟分支原样保留。
- **`_BASE_` 路径相对于当前 yaml 所在目录解析**，且可以链式继承（本例只有一层）。detectron2 加载时会先把整条继承链展开成一个 dict（Base 先进、子文件后进），再整体并入 `cfg`。

COCO 全景分割的继承链：`maskformer2_R50_bs16_50ep.yaml` → `Base-COCO-PanopticSegmentation.yaml`（Base 自身没有 `_BASE_`，链条到此为止）。

#### 4.3.2 核心流程

```text
merge_from_file("maskformer2_R50_bs16_50ep.yaml")
  ├─ 读到 _BASE_: Base-COCO-PanopticSegmentation.yaml
  │    └─ 展开成 dict_base（backbone/数据集/solver/input/...）
  ├─ 把本文件内容按键覆盖到 dict_base 上（META_ARCHITECTURE、SEM_SEG_HEAD、MASK_FORMER）
  └─ 将合并结果并入 cfg（第 4 层），命令行 opts 仍可再覆盖（第 5 层）
```

#### 4.3.3 源码精读

Base 文件承载「与具体模型无关的实验公共部分」：

- [Base-COCO-PanopticSegmentation.yaml:1-15](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/Base-COCO-PanopticSegmentation.yaml#L1-L15)：`MODEL.BACKBONE` 选 `build_resnet_backbone`（R50，`FREEZE_AT: 0` 不冻结）、torchvision R50 预训练权重、RGB 归一化均值方差、res2–res5 输出。第 10、15 行注释了两个「not used」的死键（`STEM_TYPE`、`RES5_MULTI_GRID`）——配置里允许存在无效键，读配置时要以代码实际读取为准。
- [Base-COCO-PanopticSegmentation.yaml:16-18](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/Base-COCO-PanopticSegmentation.yaml#L16-L18)：训练/测试数据集名。这些名字必须在 DatasetCatalog 里注册过（u2-l2）。
- [Base-COCO-PanopticSegmentation.yaml:19-35](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/Base-COCO-PanopticSegmentation.yaml#L19-L35)：solver 段——`ADAMW`、`BASE_LR 0.0001`、poly 步点、`WEIGHT_DECAY 0.05`、full-model 梯度裁剪（`CLIP_GRADIENTS`）、混合精度（`AMP`）。
- [Base-COCO-PanopticSegmentation.yaml:36-47](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/Base-COCO-PanopticSegmentation.yaml#L36-L47)：输入段——LSJ 目标尺寸 1024、缩放范围 0.1–2.0、mapper 名 `coco_panoptic_lsj`（u2-l3）；`VERSION: 2` 是配置版本号，与旧 checkpoint 兼容有关（u10-l4 展开）。

子文件只写「R50 实验相对 Base 的差异」：

- [maskformer2_R50_bs16_50ep.yaml:1](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml#L1)：`_BASE_: Base-COCO-PanopticSegmentation.yaml`，相对路径同目录解析。
- [maskformer2_R50_bs16_50ep.yaml:2-18](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml#L2-L18)：`META_ARCHITECTURE: "MaskFormer"`（告诉 registry 建哪个 meta arch）与 `SEM_SEG_HEAD` 段：头名 `MaskFormerHead`、类别数 133（COCO 全景 133 类）、`PIXEL_DECODER_NAME: "MSDeformAttnPixelDecoder"`、`TRANSFORMER_ENC_LAYERS: 6` 等。细心的读者会发现第 6 行与第 15 行重复写了 `IN_FEATURES`（同值）——yaml 里后写的覆盖先写的，此处无影响，是个「配置可以有冗余」的真实例子。
- [maskformer2_R50_bs16_50ep.yaml:19-39](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml#L19-L39)：`MODEL.MASK_FORMER` 段，把 4.2 注册的键逐一取值：解码器名、`TRANSFORMER_IN_FEATURE: "multi_scale_pixel_decoder"`、损失权重（CLASS/MASK/DICE = 2/5/5，覆盖了 config.py 里的 1/20/1 默认值——**yaml 覆盖扩展函数默认值**的直接证据）、`DEC_LAYERS: 10`。第 36 行注释「9 decoder layers, add one for the loss on learnable query」解释了为何写 10（u5-l3 深监督会呼应）。
- [maskformer2_R50_bs16_50ep.yaml:40-45](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml#L40-L45)：`TEST` 子树：三个任务开关全开（覆盖 config.py 默认的 `SEMANTIC_ON=True, 其他 False`），并给出两个全景阈值 0.8。

#### 4.3.4 代码实践（本讲主实践）

1. **实践目标**：写一个 mini yaml 继承 `Base-COCO-PanopticSegmentation.yaml`，修改 `NUM_OBJECT_QUERIES`、`DEC_LAYERS` 与 `TEST.SEMANTIC_ON`，再与基线配置做 diff，直观看到「继承 + 覆盖」的最终效果。
2. **操作步骤**：

第一步，在仓库根目录新建 `mini_panoptic.yaml`（示例配置；若放在别处，`_BASE_` 需写成相对该文件目录的路径或绝对路径）：

```yaml
# 示例配置：mini_panoptic.yaml
_BASE_: "configs/coco/panoptic-segmentation/Base-COCO-PanopticSegmentation.yaml"
MODEL:
  MASK_FORMER:
    NUM_OBJECT_QUERIES: 200     # 覆盖 config.py 默认值 100
    DEC_LAYERS: 6               # 覆盖默认值 6（基线 yaml 里是 10）
    TEST:
      SEMANTIC_ON: False        # 关闭语义分支输出
```

第二步，写 diff 脚本（示例代码）：

```python
# 示例代码：diff_configs.py
from detectron2.config import get_cfg
from detectron2.projects.deeplab import add_deeplab_config
from mask2former import add_maskformer2_config

def build(yaml_path):
    cfg = get_cfg()
    add_deeplab_config(cfg)
    add_maskformer2_config(cfg)
    cfg.merge_from_file(yaml_path)
    cfg.freeze()
    return cfg

def flatten(node, prefix=""):
    out = {}
    for k, v in node.items():
        full = f"{prefix}{k}"
        if hasattr(v, "keys"):
            out.update(flatten(v, full + "."))
        else:
            out[full] = v
    return out

mini = flatten(build("mini_panoptic.yaml"))
base = flatten(build(
    "configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml"))

print("=== mini 相对基线不同的键 ===")
for k in sorted(set(mini) | set(base)):
    if mini.get(k, "<缺省>") != base.get(k, "<缺省>"):
        print(f"{k}:  mini={mini.get(k, '<缺省>')}  基线={base.get(k, '<缺省>')}")
```

3. **需要观察的现象**：
   - 你改的三个键出现在 diff 中：`NUM_OBJECT_QUERIES 200 vs 100`、`DEC_LAYERS 6 vs 10`、`TEST.SEMANTIC_ON False vs True`。
   - diff 还会列出**一长串**你没写的键（`META_ARCHITECTURE`、整个 `SEM_SEG_HEAD` 段、`CLASS_WEIGHT` 等）——因为 mini 只继承了 Base，没有继承 R50 子文件的取值，这些键退回到 `add_maskformer2_config` 的默认值。
4. **预期结果**：看到「3 个有意修改 + 一批继承层级差异」。随后把 mini yaml 的 `_BASE_` 改成 `configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml`（继承链变成 mini → R50 → Base）再跑一次，diff 应**恰好只剩 3 个键**。这一步是理解 `_BASE_` 链式展开的最佳实验。具体差异条数待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Base` 文件里不写 `META_ARCHITECTURE`？
**答案**：Base 只沉淀「任务级公共项」（数据集、输入尺寸、solver）；meta arch 与头部属于「具体模型的选择」，放在子文件。同一 Base 可以被 R50、Swin 等多个子配置复用。

**练习 2**：`DEC_LAYERS: 10` 上的注释说「9 decoder layers, add one for the loss on learnable query」，如果照注释理解，真正的解码器层数是多少？
**答案**：9 层。第 10 层的输出对应「对可学习 query 本身的监督」，深监督会为每一层（含 query 层）计算辅助损失（u5-l3 展开）。

**练习 3**：mini yaml 里只写了 `MODEL.MASK_FORMER` 的 3 个键，Base 里也没有 `MASK_FORMER` 段，为什么合并不报错、其余 `MASK_FORMER` 键还有值？
**答案**：因为第 3 层 `add_maskformer2_config` 已经注册了整棵 `MASK_FORMER` 子树并给了默认值；yaml 只负责覆盖个别叶子。这正是「扩展函数注册、yaml 覆盖」的分工。

### 4.4 两个关键节点与 merge_from_list 命令行覆盖

#### 4.4.1 概念说明

`cfg` 树很大（detectron2 默认键 + 插件键上千个），但日常调参几乎都落在两个节点上：

| 节点 | 控制什么 | 主要消费代码 |
| --- | --- | --- |
| `MODEL.MASK_FORMER` | meta arch 行为：损失权重、Transformer 结构、query 数、推理任务开关、采样点 | `mask2former/maskformer_model.py` 的 `from_config`、`SetCriterion`、Transformer 解码器 |
| `MODEL.SEM_SEG_HEAD` | 头部组装：类别数、通道数、像素解码器名、可变形注意力参数 | `mask2former/modeling/meta_arch/mask_former_head.py` 的 `from_config` |

`merge_from_list` 接受「扁平键值对列表」，是命令行覆盖的标准通道：`--opts MODEL.DEVICE cpu MODEL.MASK_FORMER.TEST.PANOPTIC_ON False`。因为它是最后一层，**优先级最高**，可以不改任何 yaml 就完成一次性实验。键名必须与 `cfg` 树上的完整点号路径一致，值的类型要能被解析（数字、布尔、字符串、列表均可）。

#### 4.4.2 核心流程

```text
demo.py --config-file X.yaml --opts K1 V1 K2 V2 ...
  └─ setup_cfg: 前四层完成后
       cfg.merge_from_list(["K1", "V1", "K2", "V2"])   # 成对解析，逐个写入叶子
       cfg.freeze()
```

#### 4.4.3 源码精读

- [demo/demo.py:39-41](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/demo/demo.py#L39-L41)：`merge_from_file(args.config_file)` 之后再 `merge_from_list(args.opts)` 并冻结——命令行永远赢过 yaml。
- [train_net.py:289-291](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L289-L291)：训练入口同款。你在 u1-l4 用过的 `MODEL.WEIGHTS`、`MODEL.DEVICE` 都是走这条路径写进 `cfg` 的。
- 两个节点的消费端（下一单元精读，这里先建立「配置→代码」映射的直觉）：`MODEL.MASK_FORMER.*` 进入 [mask2former/maskformer_model.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py) 的 `from_config`；`MODEL.SEM_SEG_HEAD.*` 进入 [mask2former/modeling/meta_arch/mask_former_head.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/mask_former_head.py) 的 `from_config`。

#### 4.4.4 代码实践

1. **实践目标**：验证命令行覆盖优先级，并观察覆盖「未注册键」的后果。
2. **操作步骤**（示例代码）：

```python
# 示例代码：list_override.py
from detectron2.config import get_cfg
from detectron2.projects.deeplab import add_deeplab_config
from mask2former import add_maskformer2_config

cfg = get_cfg(); add_deeplab_config(cfg); add_maskformer2_config(cfg)
cfg.merge_from_file(
    "configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml")

# 实验 A：命令行覆盖 yaml（yaml 里是 100）
cfg.merge_from_list(["MODEL.MASK_FORMER.NUM_OBJECT_QUERIES", "50",
                     "MODEL.MASK_FORMER.TEST.PANOPTIC_ON", "False"])
print(cfg.MODEL.MASK_FORMER.NUM_OBJECT_QUERIES,
      cfg.MODEL.MASK_FORMER.TEST.PANOPTIC_ON)

# 实验 B：覆盖一个从未注册过的键
cfg2 = get_cfg(); add_deeplab_config(cfg2); add_maskformer2_config(cfg2)
try:
    cfg2.merge_from_list(["MODEL.MASK_FORMER.NOT_A_KEY", "1"])
except Exception as e:
    print(type(e).__name__, ":", str(e)[:120])
```

3. **需要观察的现象**：实验 A 打印 `50 False`（命令行赢过 yaml）；实验 B 抛出异常（预期是关于「不存在键」的错误，具体异常类型与文案随 detectron2 版本而异）。
4. **预期结果**：A 的结果确定；B 的报错文案待本地验证。这也解释了为什么想用新键必须先在 `add_maskformer2_config`（或自己的 `add_*_config`）里注册。

#### 4.4.5 小练习与答案

**练习 1**：想让 demo 只输出实例分割结果，`--opts` 应该怎么写？
**答案**：`--opts MODEL.MASK_FORMER.TEST.SEMANTIC_ON False MODEL.MASK_FORMER.TEST.PANOPTIC_ON False MODEL.MASK_FORMER.TEST.INSTANCE_ON True`。注意三个开关都在 `MODEL.MASK_FORMER.TEST` 下，而不是顶层 `TEST`（顶层 `TEST` 是 detectron2 的评估段，如 `EVAL_PERIOD`）。

**练习 2**：`MODEL.SEM_SEG_HEAD.NUM_CLASSES` 和 `MODEL.MASK_FORMER.HIDDEN_DIM` 分别在哪个节点、由谁读取？
**答案**：前者在 `SEM_SEG_HEAD` 节点（本例 133 = COCO 全景 80 thing + 53 stuff），由头部的 `from_config` 读取用于分类头；后者在 `MASK_FORMER` 节点，由 Transformer 解码器读取决定 query/特征通道宽度（256）。

**练习 3**：为什么 u1-l4 的 demo 必须用 `--opts MODEL.WEIGHTS ...` 而不能指望 yaml 里的 WEIGHTS？
**答案**：Base yaml 里的 `MODEL.WEIGHTS` 指向 ImageNet 预训练 backbone（`detectron2://ImageNetPretrained/torchvision/R-50.pkl`），那是训练起点；推理需要的是 MODEL_ZOO 的最终 checkpoint，所以用优先级最高的命令行层覆盖。

## 5. 综合实践

**任务：做一个「优先级验证 + 快速评估配置」的小工具。**

结合本讲全部内容，完成以下三步：

1. **写一个 `fast_eval.yaml`**：`_BASE_` 指向 `configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml`（链式继承），内容只有三行：`TEST.EVAL_PERIOD: 0`、`MODEL.MASK_FORMER.TEST.SEMANTIC_ON: False`、`INPUT.MIN_SIZE_TEST: 640`（缩小测试图提速）。
2. **故意制造一次三层冲突**：在 `fast_eval.yaml` 里写 `MODEL.MASK_FORMER.NUM_OBJECT_QUERIES: 80`，然后用第 5 章的 `build()` 函数加载它，并在 `merge_from_file` 之后追加 `cfg.merge_from_list(["MODEL.MASK_FORMER.NUM_OBJECT_QUERIES", "120"])`。打印最终值，验证取值是 120（命令行 > 子 yaml > 扩展函数默认值 100）。
3. **用 flatten diff 三份配置**：分别对 `Base`（配合 `add_*` 后直接 merge Base 文件）、`maskformer2_R50_bs16_50ep.yaml`、`fast_eval.yaml` 两两做 4.3.4 的键级 diff，把「每层各贡献/覆盖了什么」整理成一张表。

验收标准：你能不查资料说出——任意一个键的最终取值由「五层中最后写到它的那层」决定；`NUM_OBJECT_QUERIES` 在本实验中的取值链条是 config.py 默认 100 → R50 yaml 100（未改）→ fast_eval 80 → 命令行 120。全程不需要 GPU；仅当想跑真实推理时才回到 u1-l4 的 demo 流程（待本地验证）。

## 6. 本讲小结

- 最终配置由五层叠加：`get_cfg()` 默认值 → `add_deeplab_config` → `add_maskformer2_config` → yaml（`_BASE_` 链，Base 先、子文件后）→ 命令行 `merge_from_list`；后写的覆盖先写的，全部合并完才 `freeze()`。
- [mask2former/config.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py) 是全部 Mask2Former 专属键的唯一注册点，分六段：INPUT、SOLVER、`MODEL.MASK_FORMER`、`MODEL.SEM_SEG_HEAD` 追加键、`MODEL.SWIN`、二代新增（解码器名/LSJ/MSDeformAttn/point loss）。
- `_BASE_` 继承是**按键覆盖**而非子树替换，路径相对当前 yaml 目录解析，可链式展开；Base 沉淀任务公共项，子文件写模型差异。
- 日常调参的两个关键节点：`MODEL.MASK_FORMER`（meta arch 行为与推理开关）和 `MODEL.SEM_SEG_HEAD`（头部与像素解码器）；yaml 覆盖扩展函数的默认值（如损失权重 1/20/1 → 2/5/5），命令行又能覆盖 yaml。
- 覆盖未注册的键会失败——想引入新键必须先在某个 `add_*_config` 里注册，这就是 u8 单元自定义组件时「改代码 + 改注册 + 改配置」三件套的配置环节。

## 7. 下一步学习建议

- 下一讲 u2-l1 将进入 [train_net.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py) 的训练主流程：本讲的 `setup()` 只是它的第一站，接下来看 `Trainer` 如何用 `cfg` 构建模型、数据与优化器。
- 建议顺手通读 [configs/](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs) 目录下其他 Base 文件（如 `Base-ADE20K-PanopticSegmentation.yaml`、`Base-COCO-InstanceSegmentation.yaml`），对比它们与本讲两个 yaml 的差异，体会「任务级 Base + 模型级子配置」的组织方式。
- 想提前看配置如何变成模型，可先浏览 [mask2former/maskformer_model.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py) 的 `from_config`，找一找本讲表格里那两个节点的键分别出现在哪里（u3-l1 精读）。

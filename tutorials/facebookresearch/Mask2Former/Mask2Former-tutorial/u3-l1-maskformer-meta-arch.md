# u3-l1｜MaskFormer 顶层 Meta Architecture

> 依赖讲义：u1-l5（配置系统）、u2-l1（train_net.py 主流程）。本讲正式进入模型内部，从最顶层的 `MaskFormer` 类开始。

## 1. 本讲目标

学完本讲，你应该能够：

1. 画出 `MaskFormer.forward` 的完整流程图：图像归一化 → ImageList padding → backbone → sem_seg_head → 训练/推理两条分支。
2. 说出 `from_config` 里构建了哪几个组件、`weight_dict` 是怎么从 3 个损失键膨胀成 30 个键的。
3. 逐行解释 `prepare_targets` 如何把 detectron2 的 `Instances` 变成 `{"labels": ..., "masks": ...}` 字典，并解释为什么 GT mask 是「左上角对齐 zero-pad」而不是 resize。
4. 独立完成一个实践：不加载权重构建 COCO 全景分割配置的 `MaskFormer`，分别在 train/eval 模式下对随机输入跑前向，打印两种模式返回的键。

## 2. 前置知识

### 2.1 meta architecture：detectron2 对「一个完整模型」的抽象

detectron2 把「一个可以直接 `model(inputs)` 调用的完整模型」叫做 **meta architecture**。u1-l3 讲过「import 即注册」：`import mask2former` 时，装饰器 `@META_ARCH_REGISTRY.register()` 把 `MaskFormer` 类登记进 detectron2 的注册表；之后无论是 `train_net.py` 的 Trainer 还是 demo 里的 `DefaultPredictor`，都通过 `build_model(cfg)` 按 `cfg.MODEL.META_ARCHITECTURE`（这里取值 `"MaskFormer"`）从注册表取类并实例化。

### 2.2 mask classification 范式一分钟回顾

传统分割对每个像素问「你是哪一类」（per-pixel classification）；Mask2Former 换成对每个像素问「你属于哪个对象」，即 **mask classification**：模型输出 \(Q\) 个 query（COCO 配置里 \(Q=100\)），每个 query 负责预测

- 一个类别分布 `pred_logits`，形状 `(B, Q, K+1)`，最后一维多出的 1 是 **no-object 通道**（这个 query 没对上任何真实对象的出口）；
- 一个 mask `pred_masks`，形状 `(B, Q, H/4, W/4)`（mask 特征图是 1/4 分辨率）。

本讲的 `MaskFormer` 类不关心这些预测怎么算出来（那是 sem_seg_head 的事，u3-l2 起细讲），它只负责「调度」：喂数据、分损失或分推理。

### 2.3 forward 的输入契约：batched_inputs

`forward(batched_inputs)` 接收的是 **list[dict]**，每个 dict 对应一张图。这是 detectron2 全链路的通用格式——u2-l3 讲过 DatasetMapper 的输出就是它：

| 键 | 出现场合 | 含义 |
|---|---|---|
| `"image"` | 总是存在 | `(C, H, W)` 张量，已缩放到网络输入尺寸、已转 RGB |
| `"height"` / `"width"` | 总是存在 | **原图**的高宽（mapper 透传），推理后处理时用 |
| `"instances"` | 训练时 | `Instances` 对象，含 `gt_classes`、`gt_masks`（tensor，`(N, H, W)`） |

同一份契约同时服务训练（dataloader）和推理（`DefaultPredictor`），这是 `forward` 内部用 `self.training` 分流的根本前提。

### 2.4 train()/eval() 与 self.training

`nn.Module` 自带布尔标志 `self.training`：`model.train()` 置 True，`model.eval()` 置 False（还会联动把 BN/Dropout 切到推理行为）。detectron2 的引擎在训练循环里保证模型处于 train 模式、评估时切 eval 模式，所以 `MaskFormer.forward` 里只需要读 `self.training` 一个布尔值就能分流。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| [mask2former/maskformer_model.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py) | `MaskFormer` 顶层模型，本讲主角 | `from_config`、`forward`、`prepare_targets` |
| [mask2former/modeling/meta_arch/mask_former_head.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/mask_former_head.py) | sem_seg_head：组装像素解码器与 Transformer 解码器 | 只看 `layers` 的输出契约（u3-l2 再拆） |
| [mask2former/modeling/criterion.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py) | `SetCriterion` 损失计算 | 只看 `forward` 的返回键结构（u5-l2 再拆） |
| [mask2former/modeling/matcher.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/matcher.py) | 匈牙利匹配器 | 本讲只在 `from_config` 里看它被构造（u5-l1 再拆） |
| [configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml) | COCO 全景分割 R50 配置 | 实践用的配置，含损失权重与三开关 |
| [configs/coco/panoptic-segmentation/Base-COCO-PanopticSegmentation.yaml](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/Base-COCO-PanopticSegmentation.yaml) | 上面配置的 `_BASE_` | `PIXEL_MEAN/PIXEL_STD` 与数据集名 |

## 4. 核心概念与源码讲解

本讲拆三个最小模块：**4.1 from_config**（模型怎么被造出来）、**4.2 MaskFormer.forward**（模型怎么跑）、**4.3 prepare_targets**（训练分支里最关键的一步格式转换）。

---

### 4.1 from_config：从一份 cfg 到一个可训练的模型

#### 4.1.1 概念说明

u1-l5 讲过配置五层叠加，u2-l1 讲过 `build_model` 交给注册表。中间还有一环：detectron2 的 `@configurable` 装饰器约定，`build_model(cfg)` 实际执行的是 `cls.from_config(cfg)`，它返回一个 **参数字典**，再用这个字典调用 `__init__`。

这样设计的好处：`__init__` 保留纯 PyTorch 风格（显式参数、可类型标注、可脱离 detectron2 单测），而「读哪些配置键、怎么组装子模块」全部集中在 `from_config`。你以后自定义组件时也要遵守这个模式。

#### 4.1.2 核心流程

```text
build_model(cfg)
  └─ META_ARCH_REGISTRY["MaskFormer"].from_config(cfg)
       1. build_backbone(cfg)                      → R50（或 Swin）
       2. build_sem_seg_head(cfg, backbone.output_shape())
                                                   → MaskFormerHead（u3-l2）
       3. HungarianMatcher(cost_class=2.0, cost_mask=5.0, cost_dice=5.0,
                           num_points=12544)        → 匹配器
       4. weight_dict = {loss_ce:2.0, loss_mask:5.0, loss_dice:5.0}
          + DEEP_SUPERVISION 时按层展开 _0.._8 后缀  → 3 + 9×3 = 30 个键
       5. SetCriterion(num_classes=133, matcher, weight_dict,
                       eos_coef=0.1, losses=["labels","masks"], ...)
       6. return {...全部参数...} → __init__(**kwargs)
```

#### 4.1.3 源码精读

**① 构建两大可学习组件：**

[mask2former/maskformer_model.py:98-99](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L98-L99)

```python
backbone = build_backbone(cfg)
sem_seg_head = build_sem_seg_head(cfg, backbone.output_shape())
```

先建 backbone，再把 `backbone.output_shape()`（各层通道数/步长信息）喂给 sem_seg_head 的构建函数——head 据此知道输入特征长什么样，通道自动对齐，这是「换 backbone 不改 head 代码」的关键（u8-l1 扩展实践的基础）。

**② 构造匹配器与损失权重：**

[mask2former/maskformer_model.py:106-125](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L106-L125)

```python
class_weight = cfg.MODEL.MASK_FORMER.CLASS_WEIGHT   # 2.0
dice_weight  = cfg.MODEL.MASK_FORMER.DICE_WEIGHT    # 5.0
mask_weight  = cfg.MODEL.MASK_FORMER.MASK_WEIGHT    # 5.0

matcher = HungarianMatcher(
    cost_class=class_weight, cost_mask=mask_weight, cost_dice=dice_weight,
    num_points=cfg.MODEL.MASK_FORMER.TRAIN_NUM_POINTS,   # 12544
)

weight_dict = {"loss_ce": class_weight, "loss_mask": mask_weight, "loss_dice": dice_weight}

if deep_supervision:
    dec_layers = cfg.MODEL.MASK_FORMER.DEC_LAYERS        # 10
    aux_weight_dict = {}
    for i in range(dec_layers - 1):                      # i = 0..8
        aux_weight_dict.update({k + f"_{i}": v for k, v in weight_dict.items()})
    weight_dict.update(aux_weight_dict)
```

注意三个权重**同时**扮演两个角色：构造匹配器的代价系数、构造损失的加权系数——论文里匹配代价与损失权重共享数值，源码里就是这同一个变量传两处。

深监督展开：`DEC_LAYERS: 10` 的注释写着 "9 decoder layers, add one for the loss on learnable query"（见 [maskformer2_R50_bs16_50ep.yaml:36](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml#L36)），所以辅助键是 `_0` 到 `_8` 共 9 组。最终

\[ |\text{weight\_dict}| = 3 \times (1 + (\text{DEC\_LAYERS}-1)) = 3 \times 10 = 30 \]

这正是 u2-l1 里「训练日志有 30 个 loss 键」的出处。

**③ 组装 SetCriterion：**

[mask2former/maskformer_model.py:129-138](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L129-L138)

`num_classes` 直接取 `sem_seg_head.num_classes`（COCO 全景为 133 = 80 thing + 53 stuff），保证头和损失永远用同一个类别数；`eos_coef=NO_OBJECT_WEIGHT=0.1` 给 no-object 类降权；`losses=["labels", "masks"]` 声明只用这两种损失。

**④ 返回参数字典（含推理开关的联动逻辑）：**

[mask2former/maskformer_model.py:140-161](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L140-L161)

```python
return {
    "backbone": backbone,
    "sem_seg_head": sem_seg_head,
    "criterion": criterion,
    "num_queries": cfg.MODEL.MASK_FORMER.NUM_OBJECT_QUERIES,     # 100
    "object_mask_threshold": cfg.MODEL.MASK_FORMER.TEST.OBJECT_MASK_THRESHOLD,
    "overlap_threshold": cfg.MODEL.MASK_FORMER.TEST.OVERLAP_THRESHOLD,
    "metadata": MetadataCatalog.get(cfg.DATASETS.TRAIN[0]),
    "size_divisibility": cfg.MODEL.MASK_FORMER.SIZE_DIVISIBILITY,
    "sem_seg_postprocess_before_inference": (
        cfg.MODEL.MASK_FORMER.TEST.SEM_SEG_POSTPROCESSING_BEFORE_INFERENCE
        or cfg.MODEL.MASK_FORMER.TEST.PANOPTIC_ON
        or cfg.MODEL.MASK_FORMER.TEST.INSTANCE_ON
    ),
    ...
    "semantic_on":  cfg.MODEL.MASK_FORMER.TEST.SEMANTIC_ON,
    "instance_on":  cfg.MODEL.MASK_FORMER.TEST.INSTANCE_ON,
    "panoptic_on":  cfg.MODEL.MASK_FORMER.TEST.PANOPTIC_ON,
    "test_topk_per_image": cfg.TEST.DETECTIONS_PER_IMAGE,
}
```

两处值得注意：

- `metadata` 取**训练集**的元数据（类别名、thing/stuff 划分），推理时全景/实例分支靠它判断「这个类是不是 thing」。COCO 配置里训练集是 `coco_2017_train_panoptic`（见 [Base-COCO-PanopticSegmentation.yaml:16-18](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/Base-COCO-PanopticSegmentation.yaml#L16-L18)）。
- `sem_seg_postprocess_before_inference` 是「显式配置 or 全景开 or 实例开」：只要做全景/实例推理，就必须先把 mask 还原到原图尺寸再推理（这两个分支要逐像素硬分配），这个三值 or 就是强制开关的落点。

**⑤ `__init__` 只做记账，不建任何可学习子模块：**

[mask2former/maskformer_model.py:71-94](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L71-L94)

把传进来的组件挂到 `self.`，注册 `pixel_mean/pixel_std` 为 buffer（`persistent=False`，不进 checkpoint），处理 `size_divisibility < 0` 时回退到 backbone 自带的值，最后一条断言：

```python
if not self.semantic_on:
    assert self.sem_seg_postprocess_before_inference
```

语义关（只做全景/实例）时**必须**先还原尺寸——呼应上面第 ④ 点的 or 逻辑。

另外 [mask2former_model.py:163-165](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L163-L165) 的 `device` property 从 `pixel_mean` 所在设备取设备号，forward 里 `x["image"].to(self.device)` 就靠它。

#### 4.1.4 代码实践

**实践目标**：不构建模型，纯用 Python 复现 `from_config` 中 `weight_dict` 的展开逻辑，验证键数与键名。

**操作步骤**（零依赖，任何有 Python 的机器都能跑）：

```python
# 示例代码：复现 from_config 中 weight_dict 的展开（对应 maskformer_model.py L118-L125）
class_weight, mask_weight, dice_weight = 2.0, 5.0, 5.0
dec_layers, deep_supervision = 10, True

weight_dict = {"loss_ce": class_weight, "loss_mask": mask_weight, "loss_dice": dice_weight}
if deep_supervision:
    aux = {}
    for i in range(dec_layers - 1):
        aux.update({k + f"_{i}": v for k, v in weight_dict.items()})
    weight_dict.update(aux)

print(len(weight_dict))                                # 期望 30
print(sorted(k for k in weight_dict if k.startswith("loss_ce")))
print(sorted(weight_dict.items())[:3])
```

**需要观察的现象**：`len(weight_dict)` 输出 30；`loss_ce` 系列是 `loss_ce, loss_ce_0 ... loss_ce_8` 共 10 个。

**预期结果**：30 个键，恰好等于 u2-l1 训练日志里看到的 loss 键数。把 `dec_layers` 改成 11 再跑一遍，应得到 33 个键。

#### 4.1.5 小练习与答案

**练习 1**：`from_config` 里 `num_classes` 为什么取 `sem_seg_head.num_classes` 而不是再读一次 `cfg.MODEL.SEM_SEG_HEAD.NUM_CLASSES`？

**答案**：单一事实来源。`MaskFormerHead.__init__` 已把该配置存为 `self.num_classes`（[mask_former_head.py:85](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/mask_former_head.py#L85)），从 head 取值保证头与损失永远一致；若两处各读一次配置，将来一处改了另一处漏改就会出现类别数不匹配。

**练习 2**：如果某配置 `TEST.PANOPTIC_ON: False`、`TEST.INSTANCE_ON: False`、`TEST.SEM_SEG_POSTPROCESSING_BEFORE_INFERENCE` 未设置（默认 False），`sem_seg_postprocess_before_inference` 最终是 True 还是 False？这带来什么行为？

**答案**：False（三个条件全 False）。此时语义推理走「低分辨率融合、后置还原尺寸」路径，避免先物化 Q 张高清 mask 导致的显存暴涨——这正是 u6-l1 讲过的 OOM 规避取舍。

**练习 3**：`HungarianMatcher` 的 `num_points=12544` 从哪个配置键来？它在匹配器里干什么？

**答案**：来自 `cfg.MODEL.MASK_FORMER.TRAIN_NUM_POINTS`。匹配器在计算 mask 代价时不逐像素比 mask，而是在随机+重要性采样的 12544 个点上近似（u5-l1 精讲），这是 memory-efficient 的关键设计。

---

### 4.2 MaskFormer.forward：两条路径的总调度

#### 4.2.1 概念说明

`forward` 是整个模型唯一的对外入口，长约 95 行，却浓缩了「一进两出」的结构：**公共前奏**（归一化、padding、backbone、head）之后按 `self.training` 二分——训练路径返回损失字典，推理路径返回逐图结果列表。理解它就拿到了整本手册的「总目录」：backbone 在 u3-l3、head 在 u3-l2、损失在单元五、三个推理分支在单元六逐一展开。

#### 4.2.2 核心流程

```text
forward(batched_inputs)
│
├─ 公共前奏
│   1. 每张图 .to(device)
│   2. (x - pixel_mean) / pixel_std          # 逐通道归一化
│   3. ImageList.from_tensors(images, size_divisibility=32)
│      → 把 batch 内所有图 pad 到同一 H_pad×W_pad（32 的倍数）
│      → image_sizes 记录各自 pad 前的 (H, W)
│   4. features = backbone(images.tensor)    # {"res2":…, "res3":…, "res4":…, "res5":…}
│   5. outputs = sem_seg_head(features)      # {"pred_logits":(B,Q,134),
│                                             #  "pred_masks":(B,Q,H/4,W/4),
│                                             #  "aux_outputs":[…9 个…]}
│
├─ if self.training:                    ──── 训练路径
│   6. 取出每图 "instances" → prepare_targets → targets
│   7. losses = criterion(outputs, targets)   # 30 个键
│   8. 按 criterion.weight_dict 加权；不在 weight_dict 里的键直接删除
│   9. return losses                          # dict[str, Tensor]
│
└─ else:                                ──── 推理路径
   10. mask_pred 上采样：1/4 分辨率 → padding 尺寸（双线性）
   11. del outputs 释放中间结果
   12. 逐图循环：
       ├─ sem_seg_postprocess_before_inference?
       │    是 → 先把 mask 还原到原图 (height, width)
       ├─ SEMANTIC_ON  → semantic_inference  → result["sem_seg"]
       ├─ PANOPTIC_ON  → panoptic_inference  → result["panoptic_seg"]
       └─ INSTANCE_ON  → instance_inference  → result["instances"]
   13. return processed_results               # list[dict]
```

三种尺寸必须分清（u6-l1 也强调过）：

| 名称 | 来源 | 典型值（demo，最短边 800） |
|---|---|---|
| 原图尺寸 | `batched_inputs[i]["height"/"width"]` | 例如 1332×800 |
| 网络输入尺寸 | mapper/预测器缩放后、padding 前 | 例如 800×1200 |
| padding 尺寸 | `images.tensor.shape[-2:]`（32 的倍数） | 例如 800×1216 |

#### 4.2.3 源码精读

**① 归一化与 ImageList：**

[mask2former/maskformer_model.py:193-195](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L193-L195)

```python
images = [x["image"].to(self.device) for x in batched_inputs]
images = [(x - self.pixel_mean) / self.pixel_std for x in images]
images = ImageList.from_tensors(images, self.size_divisibility)
```

归一化常数来自 `_BASE_` 配置的 `PIXEL_MEAN [123.675, 116.280, 103.530]`、`PIXEL_STD [58.395, 57.120, 57.375]`（[Base-COCO-PanopticSegmentation.yaml:6-7](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/Base-COCO-PanopticSegmentation.yaml#L6-L7)），RGB 顺序。`ImageList.from_tensors` 是 detectron2 提供的工具：把 batch 内尺寸不一的图全部 pad 到「最大高、最大宽、且被 `size_divisibility` 整除」的统一画布上，右侧/下侧补零，并把各自 pad 前的 `(H, W)` 存进 `images.image_sizes`。注意 LSJ 训练时输入恒为 1024×1024（u2-l3），本来就是 32 的倍数，padding 实际是零开销；推理时图片尺寸任意，padding 才真正发挥作用。

**② 前向计算：**

[mask2former/maskformer_model.py:197-198](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L197-L198)

```python
features = self.backbone(images.tensor)     # dict: res2..res5
outputs = self.sem_seg_head(features)       # dict: pred_logits / pred_masks / aux_outputs
```

两行就是「三大组件」的前两个。`MaskFormerHead.layers` 的返回值直接透传 Transformer 解码器的输出字典（契约见 [mask_former_head.py:118-132](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/mask_former_head.py#L118-L132)），所以 `outputs` 的键由解码器决定：`pred_logits`、`pred_masks`、`aux_outputs`。

**③ 训练分支：**

[mask2former/maskformer_model.py:200-217](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L200-L217)

```python
if self.training:
    if "instances" in batched_inputs[0]:
        gt_instances = [x["instances"].to(self.device) for x in batched_inputs]
        targets = self.prepare_targets(gt_instances, images)
    else:
        targets = None

    losses = self.criterion(outputs, targets)

    for k in list(losses.keys()):
        if k in self.criterion.weight_dict:
            losses[k] *= self.criterion.weight_dict[k]
        else:
            losses.pop(k)
    return losses
```

要点：

- `criterion(outputs, targets)` 内部先做匈牙利匹配，再逐损失计算，最后对 `aux_outputs` 里 9 个中间层逐层重匹配重计算，返回的字典键正是 4.1 里那 30 个（见 [criterion.py:212-247](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py#L212-L247)）。
- 末尾的循环是**加权 + 过滤**一体：在 `weight_dict` 里的键乘上权重；不在的（例如未来给 criterion 加的新损失忘了配权重）直接从字典删除——防止未加权损失混进日志误导调参。
- detectron2 的引擎把返回 dict 的各项求和当作总 loss 反传，日志逐键打印。

**④ 推理分支：上采样 + 三开关分发：**

[mask2former/maskformer_model.py:218-234](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L218-L234)

```python
mask_cls_results = outputs["pred_logits"]     # (B, Q, K+1)
mask_pred_results = outputs["pred_masks"]     # (B, Q, H/4, W/4)
mask_pred_results = F.interpolate(
    mask_pred_results, size=(images.tensor.shape[-2], images.tensor.shape[-1]),
    mode="bilinear", align_corners=False)     # → (B, Q, H_pad, W_pad)

del outputs

for mask_cls_result, mask_pred_result, input_per_image, image_size in zip(
        mask_cls_results, mask_pred_results, batched_inputs, images.image_sizes):
    height = input_per_image.get("height", image_size[0])
    width = input_per_image.get("width", image_size[1])
```

[mask2former_model.py:239-260](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L239-L260)

```python
if self.sem_seg_postprocess_before_inference:
    mask_pred_result = retry_if_cuda_oom(sem_seg_postprocess)(
        mask_pred_result, image_size, height, width)
    mask_cls_result = mask_cls_result.to(mask_pred_result)

if self.semantic_on:
    r = retry_if_cuda_oom(self.semantic_inference)(mask_cls_result, mask_pred_result)
    if not self.sem_seg_postprocess_before_inference:
        r = retry_if_cuda_oom(sem_seg_postprocess)(r, image_size, height, width)
    processed_results[-1]["sem_seg"] = r

if self.panoptic_on:
    panoptic_r = retry_if_cuda_oom(self.panoptic_inference)(mask_cls_result, mask_pred_result)
    processed_results[-1]["panoptic_seg"] = panoptic_r

if self.instance_on:
    instance_r = retry_if_cuda_oom(self.instance_inference)(mask_cls_result, mask_pred_result)
    processed_results[-1]["instances"] = instance_r
```

关键行为：

- `F.interpolate` 一次性把整个 batch 的 Q 张 mask 从 1/4 分辨率拉到 padding 尺寸；`del outputs` 主动释放 `aux_outputs` 等已无用的中间张量，为后面的高清后处理腾显存。
- `sem_seg_postprocess(x, img_size, output_h, output_w)`（detectron2 提供）负责「padding 画布 → 网络输入尺寸 → 原图尺寸」的裁剪+插值；`height/width` 缺省时退回 `image_size`。
- `pred_logits` 末维是 \(K+1=134\)（含 no-object），各推理分支自行决定怎么处理最后一维。
- 三个 `if` 互相独立：COCO 配置三个开关全开（[maskformer2_R50_bs16_50ep.yaml:40-43](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml#L40-L43)），所以 demo 输出同时含三个键——呼应 u1-l4 的观察。
- 每个分支都包了 `retry_if_cuda_oom`：高分辨率下任何一步 OOM 都有三段降级（直接算 → 清缓存重试 → 搬 CPU），细节在 u10-l2。

三个 `*_inference` 方法的内部逻辑（einsum 融合、topk 重打分、段合并）分别由 u6-l1/l2/l3 精讲，本讲不展开。

#### 4.2.4 代码实践

**实践目标**：以「源码阅读 + 形状推演」方式验证对 forward 两条路径的理解，不用跑模型。

**操作步骤**：

1. 打开 [mask2former/maskformer_model.py:167-262](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L167-L262)，对照 4.2.2 的流程图逐行标注「训练路径走哪几行、推理路径走哪几行」。
2. 在纸上推演：两张图，网络输入尺寸分别为 `(3, 800, 1120)` 和 `(3, 800, 1333)`，`size_divisibility=32`，COCO 配置 `NUM_OBJECT_QUERIES=100`、`NUM_CLASSES=133`。计算 `images.tensor` 形状、`pred_masks` 插值前后的形状。
3. 复盘 u1-l4 的 demo：`DefaultPredictor` 构造的输入是 ` [{"image": ..., "height": ..., "width": ...}]`（无 `"instances"`），确认它在 eval 分支不会触碰 `prepare_targets`。

**需要观察/推演的现象**：

- `images.tensor` 形状为 `(2, 3, 800, 1344)`：高取最大 800，宽取最大 1333 向上补到 32 的倍数 1344。
- `pred_masks` 插值前 `(2, 100, 200, 336)`，插值后 `(2, 100, 800, 1344)`。

**预期结果**：与上面给出的数值一致（待本地验证：可在综合实践中打印真实形状核对）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `del outputs` 只在推理分支出现，训练分支不需要？

**答案**：训练时 `outputs` 整个（含 `aux_outputs`）都要喂给 criterion 计算深监督损失，不能删；推理时只有 `pred_logits`/`pred_masks` 有用，立即删除能释放 9 组辅助预测占用的显存，给后面高清 mask 后处理让路。

**练习 2**：推理分支的逐图循环里，`height/width` 用 `input_per_image.get(...)` 取而不是直接取 `image_size`，两者语义差别是什么？

**答案**：`image_size`（来自 `images.image_sizes`）是「网络输入尺寸」，即模型实际看到的缩放后尺寸；`batched_inputs` 里的 `height/width` 是 mapper 透传的「原图尺寸」，推理输出应对齐的最终目标。`get` 的缺省值处理的是输入 dict 没带原图尺寸的边缘情况。

**练习 3**：训练分支若 `batched_inputs[0]` 没有 `"instances"` 键会怎样？

**答案**：`targets = None`，随后 `self.criterion(outputs, None)` 在匹配器里解包 `targets` 时会抛异常。也就是说 train 模式下必须提供 GT（本讲综合实践中要记得给随机输入配随机 instances，否则前向直接报错）。

---

### 4.3 prepare_targets：把 Instances 变成 labels + masks

#### 4.3.1 概念说明

criterion 的输入不是 detectron2 的 `Instances`，而是朴素的 `list[dict]`，每图一个 dict，只含两个键：`"labels"`（该图 N 个 GT 的连续类别 id，形状 `(N,)`）和 `"masks"`（二值 mask 张量，形状 `(N, H_pad, W_pad)`）。`prepare_targets` 就是这最后一跳的「格式转换器」。

最关键的设计决策：GT mask 的尺寸适配用的是 **左上角对齐 zero-pad**，而不是 resize。原因：u2-l3 讲过 mapper 输出的 `gt_masks` 已经是网络输入尺寸（LSJ 下 1024×1024）的 tensor，与图像经受的几何变换完全一致；到 padding 尺寸只差右侧/下侧补零，与 `ImageList` 对图像的处理方式完全对称——**图像怎么 pad，GT 就怎么 pad**。如果用插值 resize，GT mask 的二值边界会被破坏，且与图像网格错位。

#### 4.3.2 核心流程

```text
prepare_targets(targets=list[Instances], images=ImageList)
  h_pad, w_pad = images.tensor.shape[-2:]        # padding 尺寸
  for 每张图的 Instances:
      gt_masks = instances.gt_masks               # (N, H, W) tensor
      padded = zeros(N, h_pad, w_pad)             # 全 0 画布
      padded[:, :H, :W] = gt_masks                # 左上角原样贴入
      new_targets.append({
          "labels": instances.gt_classes,         # (N,)
          "masks":  padded,                       # (N, H_pad, W_pad)
      })
  return new_targets
```

注意 batch 内每张图的 N 可以不同（图片里对象数不同），`labels`/`masks` 是按图成对的列表，匹配与损失都按图处理后汇总（criterion 里 `num_masks = sum(len(t["labels"]) for t in targets)`，见 [criterion.py:225](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py#L225)）。

#### 4.3.3 源码精读

[mask2former/maskformer_model.py:264-278](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L264-L278)

```python
def prepare_targets(self, targets, images):
    h_pad, w_pad = images.tensor.shape[-2:]          # ① padding 尺寸
    new_targets = []
    for targets_per_image in targets:
        # pad gt
        gt_masks = targets_per_image.gt_masks        # ② (N, H, W) tensor（来自 mapper）
        padded_masks = torch.zeros(
            (gt_masks.shape[0], h_pad, w_pad),
            dtype=gt_masks.dtype, device=gt_masks.device)   # ③ 新画布，类型/设备对齐
        padded_masks[:, : gt_masks.shape[1], : gt_masks.shape[2]] = gt_masks  # ④ 左上角贴入
        new_targets.append(
            {
                "labels": targets_per_image.gt_classes,     # ⑤ 连续类别 id
                "masks": padded_masks,
            }
        )
    return new_targets
```

- ① 尺寸取自 `images.tensor` 而不是原图——GT mask 必须与 `pred_masks` 上采样后共享同一画布，损失才能逐像素对齐。
- ② `gt_masks` 是**tensor**。LSJ mapper 里 `instances.gt_masks = masks.tensor`（`BitMasks` 的裸 tensor，见 [coco_panoptic_new_baseline_dataset_mapper.py:157-160](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/dataset_mappers/coco_panoptic_new_baseline_dataset_mapper.py#L157-L160)）；如果喂进来的是 `PolygonMasks`（多边形列表，无 `.shape[1]`/`.shape[2]`），第 ④ 行会直接 `AttributeError`/索引错误——这是自己写 mapper 时最常见的坑。
- ③ `torch.zeros` 保证 pad 区是 0（非对象），`dtype`/`device` 与 GT 一致。
- ④ 切片赋值：`[:, :H, :W]` 把整张 GT mask 不做任何缩放地放到画布左上角，右侧/下侧留 0。
- ⑤ `gt_classes` 是 mapper 阶段已映射好的**连续 id**（0..132），与 `pred_logits` 的前 K 维直接对应；全景标注原始的 `category_id` 到连续 id 的映射发生在数据注册/mapper 侧（u2-l2、u2-l3），到这里已经是干净的张量。

#### 4.3.4 代码实践

**实践目标**：构造玩具 `Instances`，调用 `prepare_targets`，亲眼验证 pad 行为与输出结构。

**操作步骤**（示例代码，需要已安装 detectron2；不需要编译 MSDeformAttn 算子，因为我们不构建完整模型，只借 `Instances` 和 `ImageList`）：

```python
# 示例代码：独立验证 prepare_targets
import torch
from detectron2.structures import Instances, ImageList

# 1) 仿照模型属性拼两个最小依赖
h_pad, w_pad = 64, 96   # 假装 padding 尺寸是 64x96（size_divisibility=32 的倍数）

class FakeModel:
    def prepare_targets(self, targets, images):  # 复制 maskformer_model.py L264-L278 的实现
        h_pad, w_pad = images.tensor.shape[-2:]
        new_targets = []
        for t in targets:
            gt_masks = t.gt_masks
            padded = torch.zeros((gt_masks.shape[0], h_pad, w_pad),
                                 dtype=gt_masks.dtype, device=gt_masks.device)
            padded[:, : gt_masks.shape[1], : gt_masks.shape[2]] = gt_masks
            new_targets.append({"labels": t.gt_classes, "masks": padded})
        return new_targets

# 2) 造一张 50x70 的"图"，2 个 GT 对象
inst = Instances((50, 70))
inst.gt_classes = torch.tensor([3, 128])
inst.gt_masks = torch.zeros(2, 50, 70)
inst.gt_masks[0, :20, :30] = 1.0   # 对象 0：左上角一块
inst.gt_masks[1, 40:, 60:] = 1.0   # 对象 1：右下角一块

images = ImageList(torch.zeros(1, 3, h_pad, w_pad), image_sizes=[(50, 70)])
out = FakeModel().prepare_targets([inst], images)[0]

print(out["labels"], out["labels"].shape)         # 期望 tensor([  3, 128]) / (2,)
print(out["masks"].shape)                          # 期望 (2, 64, 96)
print(int(out["masks"][0].sum()))                  # 期望 600 = 20*30
print(int(out["masks"][1].sum()))                  # 期望 100 = 10*10（第 40..49 行 × 第 60..69 列）
```

**需要观察的现象**：`masks` 形状从 `(2, 50, 70)` 变成 `(2, 64, 96)`；两个对象的前景像素数在 pad 前后**完全不变**（pad 只加 0）。

**预期结果**：`labels` 形状 `(2,)`；`masks` 形状 `(2, 64, 96)`；`masks[0].sum() == 600`；`masks[1]` 的和等于 `gt_masks[1].sum()`（对象 1 是切片 `[40:, 60:]`，即 10 行 × 10 列 = 100 个前景像素，pad 后仍是 100）。待本地验证。

**延伸观察**：把 `inst.gt_masks` 换成 `PolygonMasks([[...]])` 再跑一次，确认会报错——记住「gt_masks 必须是 tensor」这条契约。

#### 4.3.5 小练习与答案

**练习 1**：如果某图的 GT mask 尺寸大于 padding 尺寸（例如手工喂数据时忘了先做 LSJ resize），会发生什么？

**答案**：第 ④ 行切片赋值中 `gt_masks.shape[1] > h_pad` 时，`padded_masks[:, :H, :W]` 的目标区域比源小，PyTorch 会因形状不匹配抛 `RuntimeError`。这天然起到了「GT 必须与图像同尺寸」的断言作用。

**练习 2**：为什么 pad 出来的全 0 背景对损失是「正确的」监督信号，而不是「噪声」？

**答案**：`ImageList` 对图像 pad 的区域同样是 0，模型在这些像素上的预测没有任何图像依据；GT mask 在此处为 0 表示「不属于任何对象」，恰好与「预测的所有 mask 在此处都应接近 0」对应。更重要的是，匈牙利匹配在 mask 代价里会避开纯背景差异的歧义（u5-l1 详解）。反过来说，batch 内不同图的 padding 量不同，若用全图逐像素损失而不加处理，短图会被更多 pad 区稀释——这就是 criterion 里按「mask 数量」而非「像素数」归一化的动机之一（u5-l2 详解）。

**练习 3**：`prepare_targets` 为什么需要 `images` 参数？只用 `batched_inputs` 里的信息不够吗？

**答案**：`batched_inputs[i]["image"]` 的形状是 padding **前**的网络输入尺寸，而 GT mask 必须落在 padding **后**的画布上（与 `images.tensor`、上采样后的 `pred_masks` 对齐）。`H_pad/W_pad` 只有 `ImageList` 知道，所以必须把 `images` 传进来。

---

## 5. 综合实践

**任务**：按本讲规格，用 COCO 全景分割配置构建 `MaskFormer`（不加载预训练权重），对 batch=2 的随机输入分别跑 train/eval 两种模式的前向，打印两种模式返回的键。这是把 4.1（构建）、4.2（分流）、4.3（targets）串起来的总演练。

**前置**：按 u1-l2 完成环境安装（detectron2 + `sh make.sh` 编译 MSDeformAttn；CPU 即可，CPU 前向会走 [ms_deform_attn.py:116-121](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/ops/modules/ms_deform_attn.py#L116-L121) 的 PyTorch 回退）。

**操作步骤**：在仓库根目录新建 `explore_meta_arch.py`（放在仓库外的自己目录也可以），内容如下：

```python
# 示例代码：探索 MaskFormer 两种模式的前向输出
import torch
from detectron2.config import get_cfg
from detectron2.projects.deeplab import add_deeplab_config
from detectron2.modeling import build_model
from detectron2.structures import Instances

from mask2former import add_maskformer2_config   # import 即注册（u1-l3）

# 1) 组装配置：五层叠加的最终两层（u1-l5）
cfg = get_cfg()
add_deeplab_config(cfg)
add_maskformer2_config(cfg)
cfg.merge_from_file("configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml")
cfg.MODEL.DEVICE = "cpu"   # 无 GPU 时必须显式指定，否则默认 cuda 会失败
cfg.freeze()

# 2) 构建模型（随机初始化，不加载权重）
model = build_model(cfg)
model.eval()   # build_model 返回的模型默认就是 eval 状态

def rand_batch(with_instances=True):
    batch = []
    for _ in range(2):
        # 故意用 300x470（非 32 倍数）：padding 后画布为 320x480，可以观察 pad 效果
        d = {
            "image": torch.randint(0, 255, (3, 300, 470), dtype=torch.uint8).float(),
            "height": 400, "width": 600,
        }
        if with_instances:
            inst = Instances((300, 470))
            inst.gt_classes = torch.tensor([0, 5, 80])
            inst.gt_masks = (torch.rand(3, 300, 470) > 0.7).float()
            d["instances"] = inst
        batch.append(d)
    return batch

# 3) 训练模式：应返回 30 个键的 loss dict
model.train()
losses = model(rand_batch(with_instances=True))
print("== train mode ==")
print(len(losses), "keys")
print(sorted(losses.keys()))
print({k: round(v.item(), 4) for k, v in list(losses.items())[:3]})

# 4) 推理模式：应返回 list[dict]，每图含 sem_seg / panoptic_seg / instances 三个键
model.eval()
with torch.no_grad():
    results = model(rand_batch(with_instances=False))
print("== eval mode ==")
print(len(results), "images")
for i, r in enumerate(results):
    print(f"image {i} keys:", sorted(r.keys()))
    print("  sem_seg:", tuple(r["sem_seg"].shape))
    print("  panoptic_seg: id map", tuple(r["panoptic_seg"][0].shape),
          ", segments:", len(r["panoptic_seg"][1]))
    inst = r["instances"]
    print("  instances:", len(inst), "fields:", sorted(inst.get_fields().keys()))
```

**需要观察的现象**：

1. **train 模式**：返回 `dict`，共 **30 个键**：`loss_ce`、`loss_mask`、`loss_dice` 加后缀 `_0`…`_8`（与 4.1.4 的手工展开完全一致）；每个值是标量张量。数值本身无意义（随机权重），但键集合应当一字不差。
2. **eval 模式**：返回长度 2 的 `list`；每个 dict 恰好三个键 `instances`、`panoptic_seg`、`sem_seg`（COCO 配置三开关全开）；`sem_seg` 形状 `(133, 400, 600)`（对齐到原图 height/width）；`panoptic_seg` 是 `(id 图 tensor(400,600), segments_info list)` 二元组；`instances` 是 detectron2 `Instances` 对象，字段含 `pred_masks / pred_boxes / scores / pred_classes`。
3. 若把第 3 步改成 `with_instances=False`，前向应当在 criterion/matcher 内部报错（印证练习 4.2-3）。
4. 随机权重下 `segments_info` 可能为空（分数达不到阈值 0.8）——这正常，说明 `panoptic_inference` 的过滤逻辑在工作。

**预期结果**：以上四点均成立。本环境未安装运行时，以上输出为**待本地验证**（跑通后可把实际打印贴回本节注释里，形成自己的笔记）。

**进阶变化**（可选）：把 `cfg.MODEL.MASK_FORMER.TEST.PANOPTIC_ON` 改成 False 再 `build_model` 一次，观察 eval 输出少一个键、且 `sem_seg_postprocess_before_inference` 联动变化——亲手复现 4.1 讲的 or 逻辑。

## 6. 本讲小结

- `build_model(cfg)` → `from_config(cfg)` → `__init__(**dict)`：`from_config` 构建 backbone、sem_seg_head、`HungarianMatcher` 与 `SetCriterion`，并把三个损失权重同时用作匹配代价与损失加权；深监督把 `weight_dict` 从 3 个键展开成 30 个。
- `forward` = 公共前奏（归一化 → `ImageList` padding → backbone → sem_seg_head）+ 按 `self.training` 二分流：训练返回加权过滤后的 loss dict，推理返回逐图 `list[dict]`。
- 训练分支把 mapper 产出的 `Instances`（`gt_classes` + tensor 型 `gt_masks`）经 `prepare_targets` 转成 `{"labels", "masks"}`；GT mask 用**左上角 zero-pad** 对齐到 padding 画布，与图像的处理方式严格对称，绝不 resize。
- 推理分支先把 `pred_masks` 双线性上采样到 padding 尺寸并 `del outputs` 省显存，再按 `SEMANTIC_ON / PANOPTIC_ON / INSTANCE_ON` 三个独立开关把结果分发给三个 `*_inference`（细节在单元六）。
- 三种尺寸贯穿始终：原图（`height/width`）、网络输入（`image_sizes`）、padding（`images.tensor`），后处理就是在这三者之间换算。

## 7. 下一步学习建议

- **u3-l2 MaskFormerHead**：本讲把 `sem_seg_head` 当黑盒，下一讲打开它，看 `build_pixel_decoder` + `build_transformer_decoder` 如何组装，以及 `TRANSFORMER_IN_FEATURE` 的四种取值。
- **u3-l3 Swin Backbone / u3-l4-l5 像素解码器**：沿 `forward` 的调用链继续向下：R50/Swin 怎么产出 `res2..res5`，`MSDeformAttnPixelDecoder` 怎么产出 `mask_features` 与三个尺度。
- **单元五**：`criterion(outputs, targets)` 这一行内部的匈牙利匹配与逐点损失。
- **单元六**：`semantic_inference / panoptic_inference / instance_inference` 三个分支的逐行精读（u6-l1 已可先行阅读）。
- 建议阅读顺序：先做本讲综合实践再往下走——后面每一讲都可以在同一个探索脚本里把黑盒逐层替换成白盒。

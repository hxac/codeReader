# u2-l3 DatasetMapper：从 dataset dict 到训练样本

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出仓库里 5 种 dataset mapper 各自适用哪种分割任务，并解释 `INPUT.DATASET_MAPPER_NAME` 如何在 `train_net.py` 里被分发成具体的 mapper 类。
2. 理解新一代 LSJ（Large-Scale Jitter）增强策略：`RandomFlip → ResizeScale → FixedSizeCrop` 三步如何把任意尺寸的图片变成固定的 `1024×1024` 训练样本。
3. 精读 `COCOPanopticNewBaselineDatasetMapper.__call__`：panoptic PNG 如何经 `rgb2id` 变成 id 图，再如何逐 segment 转成 `Instances`（`gt_classes` + `gt_masks`）。
4. 对比老一代 `MaskFormerSemanticDatasetMapper`：`ResizeShortestEdge + 约束裁剪 + mapper 内 padding` 的增强链，以及「逐像素语义图 → 每类一张二值 mask」的转换。
5. 手写脚本构造假数据，独立跑通一次 mapper 变换，检查输出 tensor 形状与 `instances` 字段。

一个需要先澄清的点：学习目标里提到的 `annotations_to_panopticfacemask` 在**当前仓库中并不存在**（全仓库 grep 无此函数名）。它描述的是「标注 → Instances」这一转换步骤的概念；在 Mask2Former 里这段逻辑被**内联**写在了各 mapper 的 `__call__` 方法中，本讲按真实源码逐行讲解。

## 2. 前置知识

### 2.1 DatasetMapper 是什么、为什么需要它

上一讲（u2-l2）我们看到：`DatasetCatalog` 的加载函数返回的是 `list[dict]`，每个 dict 只含「元数据」——`file_name`、`height`、`width`、`annotations`（COCO json 里的多边形/RLE）或 `pan_seg_file_name`（panoptic PNG 路径）。这些信息：

- 图片还没读进内存，只是路径；
- 标注还是「矢量格式」（多边形顶点坐标）或「调色板格式」（RGB 编码的 panoptic 图），不是 tensor；
- 没做任何数据增强。

而模型的 `forward` 需要的是：一个归一化好的 `float tensor` 图片 + 一个可直接计算损失的 `Instances` 对象。**DatasetMapper 就是填补这两者之间鸿沟的「样本工厂」**：它是一个可调用对象（实现了 `__call__`），dataloader 的每个 worker 进程取到 dataset dict 后先调用 mapper，再把 mapper 的输出送进 batch collator。

### 2.2 mask classification 范式对 GT 的特殊要求

Mask2Former 是 mask classification 范式（u1-l1 已建立）：模型输出 Q 个「mask + 类别」对，用匈牙利匹配与 GT 一一配对后计算损失。所以 GT 必须组织成：

- `gt_classes`：形状 `(N,)` 的 int64 tensor，N 是这张图里的目标（实例或 stuff 类别段）个数；
- `gt_masks`：形状 `(N, H, W)` 的二值 tensor，每个目标一张 mask。

注意这与「逐像素语义分割」的 GT（一张 `(H, W)` 的类别 id 图）完全不同——**所有 mapper 的最终使命，都是把各自任务的原始标注统一转换成上述 `Instances` 格式**。这也解释了为什么三类任务可以共用同一个 `MaskFormer` 模型：损失端看到的 GT 结构完全一致。

### 2.3 detectron2 的增强抽象：Augmentation / AugInput / TransformGen

detectron2 把数据增强拆成三层：

- `Augmentation`：随机策略（如 `RandomFlip`），每次调用采样出一个确定性的 `Transform`；
- `Transform`：确定性的几何变换，知道如何变换图片（`apply_image`）、分割图（`apply_segmentation`）、多边形标注（`apply_polygons`）；
- `T.apply_transform_gens(gens, input)`：依次运行一组 `Augmentation`，返回变换后的输入和累积的 `TransformList`，之后可用 `transforms.apply_segmentation(...)` 把**同一组几何变换**套到 GT 上——保证图片和标注永远同步。

新基线 mapper 直接对 `np.ndarray` 图片调用（返回 `TransformList`）；老一代 mapper 则构造 `T.AugInput(image, sem_seg=...)` 让增强链同时处理图片和语义图。

### 2.4 BitMasks 与 Instances

`Instances(image_size)` 是 detectron2 的动态字段容器，像 dict 但字段是 tensor 且第一维必须一致（N 个目标）。`BitMasks` 是 `(N, H, W)` 布尔 mask 的包装类，提供 `get_bounding_boxes()` 从 mask 反推外接框——mapper 里 `gt_boxes` 不是从标注读的，而是从 mask 算出来的。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [train_net.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py) | `Trainer.build_train_loader`（L149-173）按 `INPUT.DATASET_MAPPER_NAME` 分发 5 种 mapper |
| [mask2former/config.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py) | 注册 `DATASET_MAPPER_NAME`（L13）与 LSJ 三键 `IMAGE_SIZE/MIN_SCALE/MAX_SCALE`（L96-99） |
| [mask2former/data/dataset_mappers/coco_panoptic_new_baseline_dataset_mapper.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/dataset_mappers/coco_panoptic_new_baseline_dataset_mapper.py) | 新基线：COCO 全景分割 LSJ mapper（本讲主线） |
| [mask2former/data/dataset_mappers/coco_instance_new_baseline_dataset_mapper.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/dataset_mappers/coco_instance_new_baseline_dataset_mapper.py) | 新基线：COCO 实例分割 LSJ mapper（多边形 → mask） |
| [mask2former/data/dataset_mappers/mask_former_semantic_dataset_mapper.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/dataset_mappers/mask_former_semantic_dataset_mapper.py) | 老一代：语义分割 mapper（ADE20K 系列配置在用） |
| [mask2former/data/dataset_mappers/mask_former_panoptic_dataset_mapper.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/dataset_mappers/mask_former_panoptic_dataset_mapper.py) | 老一代：全景分割 mapper，继承语义版 |
| [mask2former/data/dataset_mappers/mask_former_instance_dataset_mapper.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/dataset_mappers/mask_former_instance_dataset_mapper.py) | 老一代：实例分割 mapper |
| [mask2former/maskformer_model.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py) | `prepare_targets`（L264-278）消费 mapper 产出的 `Instances` |
| [configs/coco/panoptic-segmentation/Base-COCO-PanopticSegmentation.yaml](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/Base-COCO-PanopticSegmentation.yaml) | COCO 全景基线：LSJ 三键 + `DATASET_MAPPER_NAME: coco_panoptic_lsj` |
| [configs/ade20k/panoptic-segmentation/Base-ADE20K-PanopticSegmentation.yaml](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/ade20k/panoptic-segmentation/Base-ADE20K-PanopticSegmentation.yaml) | ADE20K 基线：老一代 INPUT 键 + `mask_former_panoptic` |

## 4. 核心概念与源码讲解

### 4.1 五种 Mapper 与选择逻辑：INPUT.DATASET_MAPPER_NAME

#### 4.1.1 概念说明

mapper 不走 detectron2 的 registry 机制，而是**一个配置字符串 + 一串 if-elif 分发**。`INPUT.DATASET_MAPPER_NAME` 默认值是 `"mask_former_semantic"`，由 `add_maskformer2_config` 注册；每个 Base yaml 用不同的值选择任务对应的 mapper。理解这张「配置值 → 类 → 适用任务」对照表，是改数据管线的第一步。

#### 4.1.2 核心流程

```
Trainer 构建训练 dataloader
  └─ Trainer.build_train_loader(cfg)          # train_net.py L149
       ├─ 读 cfg.INPUT.DATASET_MAPPER_NAME
       ├─ if-elif 匹配 5 个字符串之一 → 实例化对应 mapper(cfg, True)
       └─ build_detection_train_loader(cfg, mapper=mapper)   # detectron2 通用接口
```

5 个分支的对照表：

| `DATASET_MAPPER_NAME` | mapper 类 | 增强风格 | 适用任务/配置 |
|---|---|---|---|
| `"mask_former_semantic"`（默认） | `MaskFormerSemanticDatasetMapper` | 老一代 | ADE20K 语义 |
| `"mask_former_panoptic"` | `MaskFormerPanopticDatasetMapper` | 老一代 | ADE20K / Mapillary 全景 |
| `"mask_former_instance"` | `MaskFormerInstanceDatasetMapper` | 老一代 | ADE20K 实例 |
| `"coco_instance_lsj"` | `COCOInstanceNewBaselineDatasetMapper` | LSJ 新基线 | COCO 实例 |
| `"coco_panoptic_lsj"` | `COCOPanopticNewBaselineDatasetMapper` | LSJ 新基线 | COCO 全景（论文默认） |

#### 4.1.3 源码精读

配置键的注册点——[mask2former/config.py:13](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L13) 注册 `DATASET_MAPPER_NAME` 且默认值为 `"mask_former_semantic"`：这是「没写就是老一代语义 mapper」的由来。

分发逻辑在 [train_net.py:149-173](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L149-L173)：`build_train_loader` 依次比较 `cfg.INPUT.DATASET_MAPPER_NAME` 与 5 个字符串，各自 `new` 一个 mapper 并传给 `build_detection_train_loader(cfg, mapper=mapper)`；其中 L168-170 就是本讲主线的 `coco_panoptic_lsj` 分支；若都不匹配则 mapper 为 `None`（L171-173），退回 detectron2 默认 `DatasetMapper`。

mapper 类的可导入性来自 [mask2former/__init__.py:9-19](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/__init__.py#L9-L19)：包顶层显式 import 了 5 个 mapper 类，`train_net.py` L50-59 才能一行批量导入——这也是「import 即接线」的又一处体现。

COCO 配置侧的样例：[configs/coco/panoptic-segmentation/Base-COCO-PanopticSegmentation.yaml:36-41](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/Base-COCO-PanopticSegmentation.yaml#L36-L41) 的 INPUT 块同时给出 LSJ 三键（`IMAGE_SIZE: 1024`、`MIN_SCALE: 0.1`、`MAX_SCALE: 2.0`）与 `DATASET_MAPPER_NAME: "coco_panoptic_lsj"`；ADE20K 侧则完全不同，[Base-ADE20K-PanopticSegmentation.yaml:36-50](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/ade20k/panoptic-segmentation/Base-ADE20K-PanopticSegmentation.yaml#L36-L50) 用的是 `MIN_SIZE_TRAIN / CROP / COLOR_AUG_SSD / SIZE_DIVISIBILITY` 这套老键 + `mask_former_panoptic`。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：建立「任务 → 配置 → mapper」的检索能力。
2. **操作步骤**：
   - 在仓库根目录执行 `grep -rn "DATASET_MAPPER_NAME" configs/ mask2former/ train_net.py`；
   - 对每个 hit，记下：哪个 Base yaml、取值是什么、对应上表哪一行。
3. **需要观察的现象**：所有 COCO 配置都继承 `coco_*_lsj`，所有 ADE20K/Mapillary 配置都用 `mask_former_*` 系列；同一 Base 下不同模型（R50/Swin）共享同一个 mapper。
4. **预期结果**：得到一张与 4.1.2 表格一致的完整清单；COCO 实例与全景的 INPUT 块除 mapper 名外逐行相同。

#### 4.1.5 小练习与答案

**练习 1**：把 `DATASET_MAPPER_NAME` 改成一个不存在的字符串（如 `"my_mapper"`），训练启动时会发生什么？
**答案**：`build_train_loader` 落入 L171-173 的 `else` 分支，`mapper=None`，`build_detection_train_loader` 使用 detectron2 默认 `DatasetMapper`。它产出的 `instances` 里 `gt_masks` 是 `PolygonMasks` 而非二值 tensor，`MaskFormer.prepare_targets` 取 `gt_masks` 后无法按 `(N,H,W)` tensor 填充，训练会在很早处报形状/类型错误——不会在配置检查阶段报错。

**练习 2**：为什么 mapper 分发不放进 `DatasetCatalog` 那套 registry？
**答案**：数据集注册（u2-l2）解决「数据从哪来」，与任务无关；mapper 选择取决于「模型以什么任务训练」，且同一数据集（如 COCO 全景）可同时供全景/实例/语义三种训练复用。把它做成 `INPUT.*` 配置项 + 构建期 if-elif，允许同一份注册数据配不同 mapper。

### 4.2 build_transform_gen：LSJ 新基线增强策略

#### 4.2.1 概念说明

LSJ（Large-Scale Jitter）源自 DETR 系列的实测经验：先在 `[MIN_SCALE, MAX_SCALE]`（0.1~2.0）之间随机**大幅缩放**原图，再从结果中**固定尺寸裁剪** `1024×1024`（不够大就补零 padding）。缩放范围越激进，模型越能学到尺度不变性——Mask2Former 论文的 50ep 训练全部依赖这套增强。与之配套的关键事实是：**输出永远是固定正方形**，因此整批图片无需在 collator 里做尺寸对齐。

#### 4.2.2 核心流程

```
读入原图 (H, W, 3)
  ① RandomFlip            按 cfg.INPUT.RANDOM_FLIP 水平/垂直翻转
  ② ResizeScale           在 [min_scale, max_scale] 均匀采样 s，
                           缩放至能覆盖 target (1024×1024) 的随机尺寸
  ③ FixedSizeCrop         随机裁出（不足处补 0）确切的 1024×1024
→ 图片 tensor (3, 1024, 1024)；同一 TransformList 再作用于 GT
```

注意顺序：翻转在最前、裁剪在最后；而老一代语义 mapper 的顺序是「缩放 → 约束裁剪 → 颜色增强 → 翻转」（见 4.5）。

#### 4.2.3 源码精读

[build_transform_gen（panoptic 版 L18-47）](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/dataset_mappers/coco_panoptic_new_baseline_dataset_mapper.py#L18-L47)：L25 断言 `is_train`（该增强只用于训练）；L26-28 从 `cfg.INPUT` 读三键；L32-38 按字符串决定 `RandomFlip` 方向；L40-45 依次 append `ResizeScale` 与 `FixedSizeCrop`——这就是 LSJ 的全部。

同样的函数在实例版里**原样复制了一份**：[coco_instance_new_baseline_dataset_mapper.py:37-66](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/dataset_mappers/coco_instance_new_baseline_dataset_mapper.py#L37-L66)。两份代码逐行相同，属于刻意复制而非共享工具函数——阅读时不要怀疑自己看错了。

三个键由 [mask2former/config.py:96-99](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L96-L99) 注册（`IMAGE_SIZE=1024`、`MIN_SCALE=0.1`、`MAX_SCALE=2.0`），注释明确写着 `# LSJ aug`。`ResizeScale` 与 `FixedSizeCrop` 都是 detectron2 提供的现成 `Augmentation`（为 DETR 引入），不是本仓库实现。

#### 4.2.4 代码实践

1. **实践目标**：脱离 mapper 单独观察 LSJ 三步的几何效果，直观理解「缩放 0.1 倍 + 补零」长什么样。
2. **操作步骤**（示例代码，非项目原有文件）：

```python
# practice_lsj_aug.py —— 只依赖 detectron2，无需编译 CUDA 算子
import numpy as np
from detectron2.data import transforms as T

img = np.zeros((480, 640, 3), dtype=np.uint8)
img[:, :320] = 255  # 左半白右半黑，便于观察翻转与裁剪

gens = [
    T.RandomFlip(horizontal=True),
    T.ResizeScale(min_scale=0.1, max_scale=2.0, target_height=1024, target_width=1024),
    T.FixedSizeCrop(crop_size=(1024, 1024)),
]
for i in range(3):
    out, tfms = T.apply_transform_gens(gens, img)
    print(i, out.shape, tfms)  # 打印尺寸与每步 Transform
```

   再把 `min_scale=max_scale=1.0` 跑一次作对照。
3. **需要观察的现象**：三次输出形状恒为 `(1024, 1024, 3)`；`tfms` 里能看到缩放比例在 0.1~2.0 之间波动；当采样比例 < 1（短边小于 1024）时裁剪前发生了 padding。
4. **预期结果**：输出尺寸固定，仅内容随随机数变化；`min_scale=max_scale=1.0` 时每次输出几何上仅差裁剪偏移与翻转。
5. 本实践逻辑简单可直接推断，但打印 `TransformList` 的细节建议本地实际运行确认（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 LSJ 输出固定 1024×1024 后，模型侧就不再需要 `size_divisibility` padding？
**答案**：1024 是任何 2 的幂次 stride（4/8/16/32）的整数倍，backbone 多次下采样不会遇到除不尽的问题；又因所有样本同尺寸，`ImageList.from_tensor_list` 的批内 padding 实际是零开销的恒等操作。

**练习 2**：把 `MIN_SCALE/MAX_SCALE` 收窄到 `[0.8, 1.2]` 会带来什么训练上的变化？
**答案**：尺度抖动减弱，小目标出现频率降低（原本 0.1 倍缩放会把物体变成极小样本），模型对极端尺度的泛化会变差，通常对应论文里「去掉 LSJ 后 AP 掉点」的消融方向。

### 4.3 COCOPanopticNewBaselineDatasetMapper.__call__：panoptic 标注 → Instances

#### 4.3.1 概念说明

这是本讲主线：COCO 全景分割训练样本的完整生产线。上一讲（u2-l2）已知 COCO 全景数据的两个关键字段：`pan_seg_file_name`（一张 RGB 调色板 PNG）和 `segments_info`（`[{id, category_id, iscrowd}, ...]`）。panoptic PNG 用「颜色 → 实例 id」编码每个像素属于哪个 segment：`id = R + G×256 + B×256²`。mapper 的任务是把这张 PNG 解码成 N 张二值 mask，并且**与图片经受完全相同的几何变换**。

#### 4.3.2 核心流程

```
__call__(dataset_dict)
  ① deepcopy（dataset dict 会被就地修改，多 worker 下必须防串改）
  ② read_image(file_name) 读图，check_image_size 校验 height/width
  ③ apply_transform_gens(LSJ, image) → 新图 + TransformList
  ④ 新图 HWC→CHW 转 torch tensor 存入 dataset_dict["image"]
  ⑤ 若 eval：pop 掉 annotations 直接返回（不含 GT）
  ⑥ 读 panoptic PNG 为 RGB → apply_segmentation 同步变换 → rgb2id 得 id 图
  ⑦ 遍历 segments_info：跳过 iscrowd 段
       classes.append(category_id); masks.append(id图 == segment_id)
  ⑧ 组装 Instances：gt_classes (N,) int64、gt_masks (N,H,W) bool、
     gt_boxes 由 BitMasks.get_bounding_boxes() 反推
  ⑨ 空标注兜底：gt_masks 形状 (0,H,W)、gt_boxes (0,4)
```

#### 4.3.3 源码精读

[__call__ 主体（L105-165）](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/dataset_mappers/coco_panoptic_new_baseline_dataset_mapper.py#L105-L165)：

- [L113-115](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/dataset_mappers/coco_panoptic_new_baseline_dataset_mapper.py#L113-L115)：deepcopy 后读图并校验 `dataset_dict` 里的 `height/width` 与真实图片一致（所以伪造 dataset dict 时这两个键必须写对）。
- [L117-123](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/dataset_mappers/coco_panoptic_new_baseline_dataset_mapper.py#L117-L123)：应用 LSJ 增强；`transpose(2,0,1)` 把 HWC 转 CHW 并存为 tensor——注释解释了为什么必须尽早转 tensor（dataloader 的共享内存机制对 tensor 高效、对大对象走 pickle 低效）。
- [L125-128](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/dataset_mappers/coco_panoptic_new_baseline_dataset_mapper.py#L125-L128)：eval 分支只保留图片、丢掉标注。注意 `build_transform_gen` L25 的断言使得这个分支实际不会被 `build_train_loader` 触发——评估走 detectron2 默认 mapper。
- [L130-139](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/dataset_mappers/coco_panoptic_new_baseline_dataset_mapper.py#L130-L139)：核心三连——读 panoptic PNG 为 RGB；`transforms.apply_segmentation(pan_seg_gt)` 把与图片相同的几何变换（含 LSJ 的补零 padding）施加到 GT 上；`panopticapi.utils.rgb2id` 把 RGB 调色板解码为 id 图。GT 与图片的对齐精度完全依赖这两行。
- [L141-151](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/dataset_mappers/coco_panoptic_new_baseline_dataset_mapper.py#L141-L151)：**「标注 → Instances」的内联转换**（即概念上对应 `annotations_to_panopticfacemask` 的那段）：遍历 `segments_info`，`iscrowd` 的段直接跳过（人群标注不参与损失）；对每个保留段做 `pan_seg_gt == segment_info["id"]` 的布尔比较得到一张 mask。注意这里没有过滤「被裁剪裁没了的段」——全零 mask 会保留，交给损失端与 no-object 匹配。
- [L152-163](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/dataset_mappers/coco_panoptic_new_baseline_dataset_mapper.py#L152-L163)：无标注时兜底 `(0,H,W)` 零 mask；否则 `BitMasks(torch.stack(...))` 打包，`gt_masks` 取 `.tensor`（裸 bool tensor），`gt_boxes` 由 `get_bounding_boxes()` 从 mask 反推——`gt_boxes` 并不会被 `MaskFormer` 消费（`prepare_targets` 只取 `gt_classes/gt_masks`），留作兼容。

消费端对照：[maskformer_model.py:264-278](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L264-L278) 的 `prepare_targets` 把每个样本的 `gt_masks` pad 到批内最大尺寸、`gt_classes` 原样改名为 `labels`——mapper 产出的两个字段在此被精确消费。

**与实例版 `COCOInstanceNewBaselineDatasetMapper` 的差异**（同一文件族，入口 [__call__ L121-189](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/dataset_mappers/coco_instance_new_baseline_dataset_mapper.py#L121-L189)）：

| 维度 | panoptic 版 | instance 版 |
|---|---|---|
| GT 来源 | `pan_seg_file_name` PNG | `annotations` 列表（COCO json 多边形/RLE） |
| GT 变换方式 | `transforms.apply_segmentation`（栅格图整体变换） | [L164-168](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/dataset_mappers/coco_instance_new_baseline_dataset_mapper.py#L164-L168) `transform_instance_annotations` 变换矢量多边形 |
| 栅格化时机 | 读 PNG 时已是栅格 | 最后一步 [L181-186](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/dataset_mappers/coco_instance_new_baseline_dataset_mapper.py#L181-L186) 才用 `convert_coco_poly_to_mask` 转二值 |
| 空实例处理 | 无（全零 mask 保留） | [L179](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/dataset_mappers/coco_instance_new_baseline_dataset_mapper.py#L179) `filter_empty_instances` 丢弃被裁剪裁没的实例 |
| 额外产物 | 无 | [L148](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/dataset_mappers/coco_instance_new_baseline_dataset_mapper.py#L148) `padding_mask`（DETR 遗留，本仓库模型不消费，见 4.3.5 练习 2） |

#### 4.3.4 代码实践（本讲主实践）

1. **实践目标**：不下载 COCO，用假图片 + 假 panoptic PNG 跑通 `COCOPanopticNewBaselineDatasetMapper`，逐字段检查输出。
2. **操作步骤**（示例代码，非项目原有文件；前置条件：u1-l2 的环境已装好、CUDA 算子已编译、`panopticapi` 已安装）：

```python
# practice_panoptic_mapper.py
import os
import numpy as np
import torch
from PIL import Image
from detectron2.config import get_cfg
from mask2former.data.dataset_mappers.coco_panoptic_new_baseline_dataset_mapper import (
    COCOPanopticNewBaselineDatasetMapper,
)

root = "/tmp/fake_coco"
os.makedirs(f"{root}/images", exist_ok=True)
os.makedirs(f"{root}/panoptic", exist_ok=True)

H, W = 200, 320
# ① 假图片：渐变图
img = np.tile(np.arange(W, dtype=np.uint8), (H, 1))[..., None].repeat(3, 2)
Image.fromarray(img).save(f"{root}/images/fake.jpg")

# ② 假 panoptic PNG：id = R + G*256 + B*256^2
#    背景全 0（VOID）；segment 1=红色通道 1；segment 2=红色通道 2
pan = np.zeros((H, W, 3), dtype=np.uint8)
pan[:100, :160] = (1, 0, 0)   # id 1
pan[:100, 160:] = (2, 0, 0)   # id 2
Image.fromarray(pan).save(f"{root}/panoptic/fake.png")

# ③ 构造 dataset_dict（height/width 必须与图片一致，check_image_size 会校验）
dataset_dict = {
    "file_name": f"{root}/images/fake.jpg",
    "image_id": 1,
    "height": H, "width": W,
    "pan_seg_file_name": f"{root}/panoptic/fake.png",
    "segments_info": [
        {"id": 1, "category_id": 10, "iscrowd": 0},
        {"id": 2, "category_id": 25, "iscrowd": 0},
        {"id": 3, "category_id": 33, "iscrowd": 1},  # crowd，应被跳过
    ],
}

# ④ 最小配置：只需 mapper 用到的 INPUT 键（detectron2 默认 cfg + 直接赋新键）
cfg = get_cfg()
cfg.INPUT.FORMAT = "RGB"
cfg.INPUT.RANDOM_FLIP = "none"     # 关闭翻转，输出可复现
cfg.INPUT.IMAGE_SIZE = 128         # 用小尺寸，跑得快
cfg.INPUT.MIN_SCALE = 1.0
cfg.INPUT.MAX_SCALE = 1.0          # 固定缩放，去除随机性

mapper = COCOPanopticNewBaselineDatasetMapper(cfg, True)  # @configurable 走 from_config
out = mapper(dataset_dict)

print("image:", out["image"].shape, out["image"].dtype)
inst = out["instances"]
print("gt_classes:", inst.gt_classes)
print("gt_masks:", inst.gt_masks.shape, inst.gt_masks.dtype)
print("mask 0 前景像素数:", int(inst.gt_masks[0].sum()))
print("gt_boxes:", inst.gt_boxes.tensor)
print("残留键:", [k for k in out if k not in ("image", "instances")])
```

3. **需要观察的现象**：
   - `out["image"]` 形状 `(3, 128, 128)`（LSJ 固定裁剪的结果，与原图 200×320 无关）；
   - `inst.gt_classes` 为 `tensor([10, 25])`——`iscrowd=1` 的第三个段没有出现；
   - `inst.gt_masks` 形状 `(2, 128, 128)`、dtype 为 bool；由于 `MIN_SCALE=1.0`，两个 mask 各自的前景像素数接近原图 160×100、160×100 经随机裁剪后的保留量；
   - `pan_seg_file_name`、`segments_info` 已从输出 dict 中被 pop 掉。
4. **预期结果**：上述四条全部成立。把 `MIN_SCALE/MAX_SCALE` 改回 `0.1/2.0` 再跑几次，可看到 mask 前景像素数大幅波动（极端缩放 + 裁剪的作用）。
5. 形状与字段可由源码逻辑直接推断，但随机裁剪下的具体像素数请实际运行确认（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 mapper 里必须 `copy.deepcopy(dataset_dict)`？
**答案**：detectron2 的 `MapDataset` 与多 worker dataloader 会复用/缓存 dataset dict；`__call__` 里大量 `pop`（`pan_seg_file_name`、`segments_info`）会破坏原 dict。若不 deepcopy，第二次访问该样本时关键字段已丢失，且不同 worker 的缓存行为会让错误难以复现。

**练习 2**：实例版 mapper L148 写入的 `dataset_dict["padding_mask"]` 最终被谁消费？
**答案**：没有人。全仓库搜索可见 `MaskFormer.forward` 与解码器都不读 `padding_mask`，[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:403](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L403) 处 `memory_key_padding_mask=None` 并注释「不对 padding 区域做 masking」。它是从 DETR 数据管线沿袭的遗留产物（源文件顶部也注明 modified from DETR 的 mapper）。

**练习 3**：panoptic 版为什么不 `filter_empty_instances` 而实例版要？
**答案**：实例版的多边形经裁剪后可能整个落在裁剪框外，产生空实例会让 `BitMasks`/框计算退化，故先过滤；panoptic 版的 mask 是整图栅格比较，最多为全零（LSJ 裁剪裁没了或本就是空段），全零 mask 在集合预测里对应「与所有 query 匹配都是 no-object」，是合法监督信号，无需过滤。

### 4.4 MaskFormerSemanticDatasetMapper：老一代语义 mapper

#### 4.4.1 概念说明

老一代 mapper（文件名以 `mask_former_` 开头）是 MaskFormer 一代的产物，ADE20K/Mapillary 配置仍在使用。它解决的任务不同：语义分割的原始 GT 是**一张 `(H,W)` 的逐像素类别 id 图**（`sem_seg_file_name`，uint16 PNG，255=ignore），既无实例概念也无 segment 列表。mapper 需要做两件新基线不做的事：把「逐像素图」重组成「每类一张二值 mask」（以适配 mask classification 损失），以及在 **mapper 内部**把图片和 GT padding 到 `SIZE_DIVISIBILITY`（老一代不做固定尺寸裁剪，批内尺寸不一，padding 被提前到了数据侧）。

#### 4.4.2 核心流程

```
__call__(dataset_dict)
  ① 读图 + 读 sem_seg GT（astype double 以便变换）
  ② AugInput(image, sem_seg=gt) → 增强链同时作用于图与 GT：
       ResizeShortestEdge → （可选）RandomCrop_CategoryAreaConstraint
       → （可选）ColorAugSSDTransform → RandomFlip
  ③ 转tensor；若 SIZE_DIVISIBILITY>0：图 pad 128、GT pad ignore_label
  ④ dataset_dict["image"/"sem_seg"] 存入（注意：语义 GT 也随样本下发）
  ⑤ 有 "annotations" 则报错（语义数据不应有实例标注）
  ⑥ np.unique(GT) 去掉 ignore_label → 每个**出现过的类别**一张
     (pan_seg_gt == class_id) mask → Instances(gt_classes, gt_masks)
```

注意第 ⑥ 步与 panoptic 版的语义差异：这里的 N 是「图中出现过的类别数」（同一类的所有像素合成一张 mask，可能不连通），不是实例数。

#### 4.4.3 源码精读

增强链构建在 [mask_former_semantic_dataset_mapper.py:61-96](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/dataset_mappers/mask_former_semantic_dataset_mapper.py#L61-L96)：L64-70 `ResizeShortestEdge`（按 `MIN_SIZE_TRAIN` 列表采样短边）；L71-79 `RandomCrop_CategoryAreaConstraint`——带约束的随机裁剪，重试直到没有单个类别占据超过 `SINGLE_CATEGORY_MAX_AREA`，防止某类（如大面积天空）吞掉整个 crop；L80-81 `ColorAugSSDTransform`（来自 detectron2 point_rend 项目的 SSD 色彩扰动）；L82 翻转。特别地，L84-87 从 `MetadataCatalog.get(DATASETS.TRAIN[0])` 读 `ignore_label`——**ignore 值来自数据集元数据而非配置**，这是 u2-l2 元数据机制的直接消费点。

[__call__ 主体 L98-184](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/dataset_mappers/mask_former_semantic_dataset_mapper.py#L98-L184)：

- [L112-116](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/dataset_mappers/mask_former_semantic_dataset_mapper.py#L112-L116)：读语义 GT；`astype("double")` 的注释说明原因是 PyTorch 变换不支持 uint16。
- [L125-128](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/dataset_mappers/mask_former_semantic_dataset_mapper.py#L125-L128)：`T.AugInput(image, sem_seg=sem_seg_gt)` 让增强链一次处理图与 GT（与新基线「图片先行、GT 用 TransformList 补做」是两种等价写法）。
- [L135-145](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/dataset_mappers/mask_former_semantic_dataset_mapper.py#L135-L145)：mapper 内 padding——图 pad 值 128（中性灰）、GT pad 值 `ignore_label`（255，即 padding 区域不参与损失）。对照 ADE20K 配置的 `SIZE_DIVISIBILITY: 640`（Base-ADE20K yaml L48）。
- [L154-158](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/dataset_mappers/mask_former_semantic_dataset_mapper.py#L154-L158)：语义 GT 以 `sem_seg` 键随样本下发（评估/可视化用）；若发现实例标注则直接 `raise ValueError`。
- [L160-182](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/dataset_mappers/mask_former_semantic_dataset_mapper.py#L160-L182)：**逐像素图 → per-category Instances**：`np.unique` 得到出现过的类别、剔除 ignore 类别，逐类 `sem_seg_gt == class_id` 生成 mask；空图兜底 `(0,H,W)`。这一段与 panoptic 版 L141-163 的循环结构几乎对称——一个按 `segments_info` 列表、一个按 unique 类别。

老一代的另外两个变体：`MaskFormerPanopticDatasetMapper`（[mask_former_panoptic_dataset_mapper.py:19](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/dataset_mappers/mask_former_panoptic_dataset_mapper.py#L19) 直接**继承语义版**）只是重写 `__call__` 增加对 `pan_seg_file_name` 的处理——[L95-106](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/dataset_mappers/mask_former_panoptic_dataset_mapper.py#L95-L106) 用 `AugInput` 变换图/语义图、再用 `transforms.apply_segmentation` + `rgb2id` 变换全景图，[L125-127](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/dataset_mappers/mask_former_panoptic_dataset_mapper.py#L125-L127) 把全景 id 图 pad 0（VOID），[L146-150](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/dataset_mappers/mask_former_panoptic_dataset_mapper.py#L146-L150) 复用与新基线相同的 segment 循环。`MaskFormerInstanceDatasetMapper` 的差异在标注转换：[mask_former_instance_dataset_mapper.py:118-139](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/dataset_mappers/mask_former_instance_dataset_mapper.py#L118-L139) 支持 polygon/RLE/ndarray 三种 segmentation 格式手工转 bitmask（新基线实例版则依赖 detectron2 的 `annotations_to_instances`）。

#### 4.4.4 代码实践（源码阅读 + 最小复现）

1. **实践目标**：理解「逐像素类别图 → per-category mask」这一步的语义，不依赖完整配置独立复现它。
2. **操作步骤**（示例代码，非项目原有文件；完整跑 `MaskFormerSemanticDatasetMapper` 本身需要 `MetadataCatalog` 中已注册带 `ignore_label` 的数据集及 `point_rend` 依赖，此处复现核心逻辑）：

```python
# practice_semantic_to_masks.py —— 复现 L160-182 的核心转换
import numpy as np, torch

ignore_label = 255
H = W = 8
sem_seg = np.full((H, W), ignore_label, dtype=np.int64)
sem_seg[0:4, 0:4] = 7    # 类别 7：左上块
sem_seg[4:8, 0:8] = 12   # 类别 12：下半整行（不连通也无所谓，此处连通）

classes = np.unique(sem_seg)
classes = classes[classes != ignore_label]          # 剔除 ignore（对应 L166）
gt_classes = torch.tensor(classes, dtype=torch.int64)
masks = torch.stack([torch.from_numpy(sem_seg == c) for c in classes])  # 对应 L169-171
print(gt_classes)          # tensor([ 7, 12])
print(masks.shape)         # (2, 8, 8)
```

   然后打开真实源文件，对照 [L160-182](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/dataset_mappers/mask_former_semantic_dataset_mapper.py#L160-L182) 给每一行写一句注释。
3. **需要观察的现象**：`gt_classes` 只含「出现过的类别」（7 和 12），255 被剔除；mask 数量等于类别数而非实例数。
4. **预期结果**：如代码注释所示 `(2, 8, 8)`；若把某个类别像素改成 255，对应 mask 消失。
5. numpy/torch 逻辑可直接推断，但建议本地运行核对（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：老一代语义 mapper 的 `SIZE_DIVISIBILITY=640` 与新基线「固定 1024 裁剪」分别把批内对齐问题放在哪里解决？
**答案**：老一代在 **mapper 内** pad 到 640 的整数倍（图 pad 128、GT pad 255），批内样本仍是「640 的倍数但彼此可以不同」，后续 `ImageList` 还可能再做一次批内 padding；新基线直接把每个样本裁成同一个 1024×1024，批内天然对齐，`SIZE_DIVISIBILITY` 形同虚设。

**练习 2**：`RandomCrop_CategoryAreaConstraint` 约束的是什么风险？
**答案**：普通随机裁剪可能裁出一个被单一 dominant 类（天空、道路）占满的 patch，语义/全景训练会严重偏向该类。该增强会重试采样，直到任何单个类别在 crop 中的面积占比不超过 `SINGLE_CATEGORY_MAX_AREA`，保证 crop 的类别多样性。

**练习 3**：同一张 ADE20K 图里某类别的像素被 LSJ 裁剪裁没了（mask 全零），语义 mapper 与新基线 panoptic mapper 分别怎么处理？
**答案**：语义 mapper 的类别集合来自 `np.unique(变换后的 GT)`，该类根本不会出现在 `gt_classes` 里（等于该类此样本不监督）；新基线 panoptic mapper 的类别集合来自 `segments_info`（变换前的标注），即使 mask 被裁成全零也保留该条目，作为「空 GT」参与匹配。

## 5. 综合实践

**任务：模拟「mapper 输出 → prepare_targets 输入」的完整接力，验证 LSJ 固定尺寸的批处理优势。**

在 4.3.4 脚本的基础上扩展（示例代码）：

1. 生成**两张长宽比不同**的假图（如 200×320 与 300×300），各配一份假 panoptic PNG 与 `segments_info`；
2. 用同一个 `COCOPanopticNewBaselineDatasetMapper`（`IMAGE_SIZE=128`）分别变换，收集 `out["image"]` 与 `out["instances"]`；
3. 用 `torch.stack([o["image"] for o in outs])` 直接组 batch，再手写一个 5 行版的 `prepare_targets`（对照 [maskformer_model.py:264-278](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L264-L278)）：把每张 `gt_masks` pad 成 `(N_i, 128, 128)`（因输入固定尺寸，此处 pad 实际是恒等），拼出 `[{labels, masks}, ...]`；
4. 可视化：把 `out["image"]` 与 `gt_masks.max(0).values` 各存一张 PNG，肉眼核对 mask 与图片内容对齐（同一 TransformList 的效果）；
5. 观察点：
   - 两张原图尺寸不同，batch tensor 仍是 `(2, 3, 128, 128)`，无需任何 padding；
   - 每个样本的 `labels`/`masks` 第一维可以不同（实例数不同），这正是 `prepare_targets` 要逐样本处理的原因；
   - 叠加图里 mask 边缘与假图色块边界重合，证明 GT 与图片经受了同一几何变换。

预期结果：batch 无缝堆叠；若你把 4.4 的老一代路径拿来对比（不同尺寸输出），`torch.stack` 会直接报错——这就是新基线用固定尺寸裁剪换来的工程简化。完整运行结果待本地验证。

## 6. 本讲小结

- mapper 分发不走 registry：`INPUT.DATASET_MAPPER_NAME`（默认 `mask_former_semantic`，[config.py:13](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L13)）在 [train_net.py:149-173](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L149-L173) 被 if-elif 分发成 5 个 mapper 类：COCO 系列用 `coco_*_lsj` 新基线，ADE20K/Mapillary 用 `mask_former_*` 老一代。
- 新基线 LSJ = `RandomFlip → ResizeScale(0.1~2.0) → FixedSizeCrop(1024)`，输出恒为固定正方形；`build_transform_gen` 在两个新基线文件里逐行重复。
- `COCOPanopticNewBaselineDatasetMapper.__call__` 的核心链路：图片先增强得 TransformList → panoptic PNG 用 `apply_segmentation` 同步变换 → `rgb2id` 解码 → 按 `segments_info`（跳过 iscrowd）做 `id图 == segment_id` 得二值 mask → 组装 `Instances(gt_classes, gt_masks, gt_boxes)`；不存在独立的 `annotations_to_panopticfacemask` 函数，转换全部内联。
- 老、新两代的关键差异：GT 变换方式（栅格 `apply_segmentation` vs 矢量多边形后置栅格化）、空实例处理（panoptic 保留全零 mask vs instance `filter_empty_instances`）、尺寸对齐（mapper 内 pad `SIZE_DIVISIBILITY` vs 固定裁剪天然对齐）。
- 所有 mapper 的统一出口是 mask classification 需要的 `Instances`：`gt_classes (N,) + gt_masks (N,H,W)`，被 `MaskFormer.prepare_targets` 精确消费为 `{labels, masks}`——这是三种任务共用一个模型的数据侧根基。

## 7. 下一步学习建议

数据侧的样本格式（`image` tensor + `instances`）已经就绪，下一讲 **u3-l1（MaskFormer 顶层 Meta Architecture）** 将从模型的 `forward` 接过这一棒：看 `prepare_targets` 如何把它变成损失输入、批内 padding 在 `ImageList` 里如何发生。如果你更关心训练闭环，可以先读 **u2-l4（优化器构建与学习率策略）**。想动手改数据的读者，建议把 4.3.4 的脚本改造成自定义数据集的调试工具：任何新数据接入，先让它能通过 mapper 产出形状正确的 `instances`，再谈训练。

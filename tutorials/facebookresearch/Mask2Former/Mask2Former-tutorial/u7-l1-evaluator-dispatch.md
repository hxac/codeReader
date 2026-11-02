# 评估器分发：build_evaluator

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `Trainer.build_evaluator` 是被谁、在什么时机调用的（评估周期到点、`--eval-only`、TTA 三条路径）。
2. 记住每种 `evaluator_type` 对应的评估器组合表：哪个是无条件添加的，哪个受 `MODEL.MASK_FORMER.TEST.PANOPTIC_ON / INSTANCE_ON / SEMANTIC_ON` 三个开关控制。
3. 理解「同一份配置键同时控制模型输出与评估器消费」的联动设计，并能解释开关改错会发生什么。
4. 掌握 `DatasetEvaluators` 组合器的三个出口（报错、裸返回单个、组合返回）以及结果合并方式。

## 2. 前置知识

本讲不再重复模型内部结构，但依赖前面几讲建立的三个认知：

- **评估器（Evaluator）是什么**：detectron2 中一个实现了「`reset()` → 多次 `process(inputs, outputs)` → `evaluate()`」协议的对象。推理阶段它逐批收到「输入数据 + 模型输出」，最后把算好的指标（mIoU、AP、PQ 等）以字典形式交回训练引擎打印。协议与各具体评估器（`COCOEvaluator`、`SemSegEvaluator` 等）都定义在 detectron2 中，属于**本仓库之外的源码**，本讲引用时不再给出永久链接。
- **`MetadataCatalog` 与 `evaluator_type`**（u2-l2）：每个注册进 catalog 的数据集都挂有一份静态元数据，其中 `evaluator_type` 这个字段就是「这份标注适合用什么方式评估」的说明。注册不碰磁盘，因此本讲的干跑实践**不需要下载任何数据集**。
- **三个 TEST 开关**（u6-l1/l2/l3）：`MODEL.MASK_FORMER.TEST.SEMANTIC_ON / INSTANCE_ON / PANOPTIC_ON` 决定 `MaskFormer` 推理时产出 `sem_seg`、`instances`、`panoptic_seg` 三个输出键中的哪几个。本讲你会看到同一组开关的**第二重身份**。

几个指标术语的通俗解释：

| 指标 | 全称 | 一句话含义 | 典型评估器 |
|---|---|---|---|
| mIoU | mean Intersection over Union | 每类「预测与真值交集/并集」的平均值，语义分割主流指标 | `SemSegEvaluator` |
| AP | Average Precision | 按 IoU 阈值扫描得到的精度均值，实例检测/分割主流指标 | `COCOEvaluator` / `InstanceSegEvaluator` |
| PQ | Panoptic Quality | 同时考核分割质量（SQ）与识别质量（RQ）的全景指标 | `COCOPanopticEvaluator` |

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [train_net.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py) | 本讲主战场：`Trainer.build_evaluator`（L67-L147）、`test_with_TTA`（L264-L278）、`main` 的评估路径（L298-L315） |
| [mask2former/__init__.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/__init__.py) | 导出本仓库唯一的自定义评估器 `InstanceSegEvaluator`（L26） |
| [mask2former/evaluation/instance_evaluation.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/evaluation/instance_evaluation.py) | `InstanceSegEvaluator`：继承 detectron2 `COCOEvaluator` 的定制实例评估器（u7-l2 精读） |
| [mask2former/data/datasets/register_ade20k_panoptic.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/datasets/register_ade20k_panoptic.py) | ADE20K 全景数据集注册，写入 `evaluator_type="ade20k_panoptic_seg"` |
| [mask2former/data/datasets/register_coco_panoptic_annos_semseg.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/datasets/register_coco_panoptic_annos_semseg.py) | 注册 `coco_2017_val_panoptic_with_sem_seg`，一份注册同时挂三套评估所需路径 |
| [mask2former/data/datasets/register_mapillary_vistas_panoptic.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/datasets/register_mapillary_vistas_panoptic.py) | Mapillary 全景注册，`evaluator_type="mapillary_vistas_panoptic_seg"`（L422） |
| [mask2former/config.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py) | 三个 TEST 开关的默认值（L54-L60） |
| [mask2former/maskformer_model.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py) | 模型侧消费同一组开关（L149-L159），证明开关联动 |
| [configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml) | COCO 全景配置：三开关全开（L40-L45） |

## 4. 核心概念与源码讲解

### 4.1 评估的触发时机：谁在调用 build_evaluator

#### 4.1.1 概念说明

`build_evaluator` 是 `Trainer` 的一个类方法，但**你在这份代码里找不到任何一行调用它的语句**——它是一个回调（callback），由 detectron2 的 `DefaultTrainer` 引擎在评估时刻回调。这正是 u2-l1 讲过的「子类覆写 build_* 方法、由引擎回调」模式在评估侧的体现：引擎知道「什么时候该评估」，插件知道「该用哪些评估器」。

#### 4.1.2 核心流程

三条路径会间接调用到 `build_evaluator`：

```text
路径 A：周期评估
  trainer.train() 内部
    → 每隔 TEST.EVAL_PERIOD 个迭代
    → DefaultTrainer.test(cfg, model)          （detectron2 引擎）
    → 对 cfg.DATASETS.TEST 中的每个数据集名
       逐个调用 cls.build_evaluator(cfg, name)

路径 B：--eval-only
  main(args)
    → args.eval_only 为真
    → Trainer.test(cfg, model)                 （不传 evaluators，引擎内部自建）
    → 同上，逐数据集回调 build_evaluator

路径 C：TTA
  main 中若 cfg.TEST.AUG.ENABLED
    → Trainer.test_with_TTA(cfg, model)
    → 显式为每个测试数据集调用 cls.build_evaluator(..., output_folder=inference_TTA)
```

注意路径 B 与路径 A 走的是同一个 `Trainer.test` 类方法；路径 C 则是本仓库自己写的，显式调用了 `build_evaluator`，且把输出目录换成 `inference_TTA` 以免覆盖普通评估的中间文件。

#### 4.1.3 源码精读

`main` 中评估分支只做三件事：建模型、加载权重、调 `Trainer.test`（[train_net.py:298-311](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L298-L311)）。注意 `Trainer.test(cfg, model)` 没有传 evaluators 参数，评估器在引擎内部按数据集逐个构建：

```python
if args.eval_only:
    model = Trainer.build_model(cfg)
    DetectionCheckpointer(model, save_dir=cfg.OUTPUT_DIR).resume_or_load(
        cfg.MODEL.WEIGHTS, resume=args.resume
    )
    res = Trainer.test(cfg, model)          # 引擎内部回调 build_evaluator
    if cfg.TEST.AUG.ENABLED:
        res.update(Trainer.test_with_TTA(cfg, model))
```

TTA 路径是本仓库唯一显式调用 `build_evaluator` 的地方（[train_net.py:264-278](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L264-L278)）：为 `cfg.DATASETS.TEST` 里每个数据集建一组评估器，输出目录统一改到 `inference_TTA`，最后给所有指标键加 `_TTA` 后缀，与普通评估结果并排对比。

`DefaultTrainer.test` 如何逐数据集回调、指标如何按数据集名分组打印，属于 detectron2 引擎源码（非本仓库文件，行号待确认）。

#### 4.1.4 代码实践

1. **实践目标**：在真实评估日志中确认 `build_evaluator` 的调用时机与产物目录。
2. **操作步骤**：按 [GETTING_STARTED.md:54-59](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/GETTING_STARTED.md#L54-L59) 的命令运行 COCO 全景评估：

   ```bash
   python train_net.py \
     --config-file configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml \
     --eval-only MODEL.WEIGHTS /path/to/checkpoint_file
   ```

3. **需要观察的现象**：日志中先出现模型构建与权重加载，随后在评估开始前依次出现各评估器自己的准备日志（如 `COCOEvaluator` 加载真值 json 的输出）；评估结束后在 `OUTPUT_DIR/inference/` 下出现各评估器落盘的中间文件。
4. **预期结果**：`output/inference/` 目录下能看到全景评估写入的预测文件与实例评估写入的 `coco_instances_results.json`（该文件名的写入位置见 [instance_evaluation.py:72-76](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/evaluation/instance_evaluation.py#L72-L76)）。本讲写作环境无数据与 GPU，**待本地验证**。
5. 若无数据集，可跳过运行，改做 4.3.4 的干跑实践。

#### 4.1.5 小练习与答案

**练习 1**：`--eval-only` 时为什么不传 evaluators 给 `Trainer.test`？
**答案**：detectron2 的 `DefaultTrainer.test` 在未显式传入评估器时，会对 `cfg.DATASETS.TEST` 中每个数据集回调 `cls.build_evaluator` 自行构建；这正是插件式设计——引擎提供流程，插件提供组件。

**练习 2**：TTA 评估为什么要把 `output_folder` 换成 `inference_TTA`？
**答案**：避免与同一输出目录下普通评估的中间产物（如 COCO 格式的预测 json、panoptic 预测图）互相覆盖；同时最终指标键加了 `_TTA` 后缀，两套结果可以并排对比。

### 4.2 evaluator_type：数据集自带的「评估方式说明书」

#### 4.2.1 概念说明

`build_evaluator` 的第一把钥匙是数据集元数据里的 `evaluator_type`。它回答的问题是：**这份标注文件能支持评什么指标**。COCO 全景标注（png id 图 + json）既能算 PQ，又能导出实例 AP 和语义 mIoU；而一份纯语义分割标注只能算 mIoU。这个信息在数据集注册时就被写进了 `MetadataCatalog`，评估侧只需查表。

#### 4.2.2 核心流程

```text
数据集注册（u2-l2）
  → MetadataCatalog.set(evaluator_type=..., panoptic_root=..., json_file=..., sem_seg_root=...)
评估时
  → build_evaluator(cfg, dataset_name)
  → evaluator_type = MetadataCatalog.get(dataset_name).evaluator_type   # 查表
  → 进入对应 if-else 分支
```

#### 4.2.3 源码精读

本仓库注册的三个全景数据集都在元数据里写明了类型。ADE20K 全景（[register_ade20k_panoptic.py:289-305](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/datasets/register_ade20k_panoptic.py#L289-L305)）：这段代码把惰性加载函数登记进 `DatasetCatalog`，再往 `MetadataCatalog` 写入 `panoptic_root`（PQ 评估用）、`json_file`（实例 AP 评估用）、`ignore_label`（语义评估用）与 `evaluator_type="ade20k_panoptic_seg"`——一份注册同时备好了三种评估器各自需要的路径。

COCO 全景的 `_with_sem_seg` 变体（[register_coco_panoptic_annos_semseg.py:129-157](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/datasets/register_coco_panoptic_annos_semseg.py#L129-L157)）：在 detectron2 内置的 `coco_2017_val_panoptic` 之上追加 `sem_seg_root` 与实例 `json_file`，并设 `evaluator_type="coco_panoptic_seg"`。为什么要专门造这个变体？答案写在基础配置的注释里（[Base-COCO-PanopticSegmentation.yaml:16-18](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/Base-COCO-PanopticSegmentation.yaml#L16-L18)）——`to evaluate instance and semantic performance as well`，一次评估同时报 PQ / AP / mIoU 三张表。

其余类型（`coco`、`coco_panoptic_seg` 的原始名、`cityscapes_instance`、`cityscapes_sem_seg`、`cityscapes_panoptic_seg`、`lvis`）对应的数据集名（如 `cityscapes_fine_instance_seg_val`）由 detectron2 内置注册（非本仓库文件）；Mapillary 全景则由 [register_mapillary_vistas_panoptic.py:422](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/datasets/register_mapillary_vistas_panoptic.py#L422) 注册为 `mapillary_vistas_panoptic_seg`。

#### 4.2.4 代码实践

1. **实践目标**：不下载任何数据，枚举 catalog 中所有数据集的 `evaluator_type`。
2. **操作步骤**：保存以下脚本并运行（示例代码，放在仓库外的任意目录均可，**不要**写进仓库）：

   ```python
   # list_evaluator_types.py（示例代码）
   import mask2former  # noqa: F401  import 即注册：本项目 7 个模块 + detectron2 内置数据集全部入 catalog
   from detectron2.data import MetadataCatalog

   for name, meta in sorted(MetadataCatalog.list().items()):
       if meta.has("evaluator_type"):
           print(f"{meta.get('evaluator_type'):<35} {name}")
   ```

3. **需要观察的现象**：输出形如 `coco_panoptic_seg   coco_2017_val_panoptic_with_sem_seg` 的两列清单。
4. **预期结果**：能看到本仓库注册的 `ade20k_panoptic_*`、`mapillary_vistas_panoptic_*`、`coco_*_panoptic_with_sem_seg`，以及 detectron2 内置的 `coco_2017_val_*`、`cityscapes_fine_*`、`lvis_*` 系列。**待本地验证**（需要已装好 detectron2 与本仓库依赖）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `evaluator_type` 放在数据集元数据里，而不是配置文件里？
**答案**：评估方式由标注格式决定，而标注格式是数据集的固有属性；放进元数据后，换一张测试集（改 `DATASETS.TEST`）就能自动换上正确的评估器，无需改模型配置。

**练习 2**：`coco_2017_val_panoptic_with_sem_seg` 相比 detectron2 内置的 `coco_2017_val_panoptic` 多挂了哪些元数据、分别服务谁？
**答案**：多挂了 `sem_seg_root`（服务 `SemSegEvaluator` 读语义真值图）与实例标注 `json_file`（服务 `COCOEvaluator` 读实例真值），配合原有的 `panoptic_root`/`panoptic_json`（服务 `COCOPanopticEvaluator`），实现一个测试集同时评三种指标。

### 4.3 build_evaluator 分发表精读

#### 4.3.1 概念说明

`build_evaluator` 是两把钥匙共同驱动的分发表：

- **第一把：`evaluator_type`（数据说了算）**——决定候选评估器集合；
- **第二把：三个 TEST 开关（模型说了算）**——在候选集合里再做筛选，保证「模型没输出的任务，就不为它建评估器」。

因为模型侧与评估侧读的是**同一组配置键**（见 4.3.3 最后一处链接），两边天然同步。

#### 4.3.2 核心流程

```text
build_evaluator(cfg, dataset_name):
    output_folder = OUTPUT_DIR/inference        # 默认落盘目录
    evaluator_type = MetadataCatalog[dataset_name].evaluator_type
    list = []
    ① evaluator_type ∈ {sem_seg, ade20k_panoptic_seg}        → 加 SemSegEvaluator（无条件）
    ② evaluator_type == coco                                  → 加 COCOEvaluator
    ③ evaluator_type ∈ {coco/ade20k/cityscapes/mapillary}_panoptic_seg 且 PANOPTIC_ON
                                                              → 加 COCOPanopticEvaluator
    ④ evaluator_type == coco_panoptic_seg 且 INSTANCE_ON      → 加 COCOEvaluator
    ⑤ evaluator_type == coco_panoptic_seg 且 SEMANTIC_ON      → 加 SemSegEvaluator
    ⑥ mapillary_vistas_panoptic_seg 且 INSTANCE_ON/SEMANTIC_ON → 加 InstanceSegEvaluator/SemSegEvaluator
    ⑦ cityscapes_instance / cityscapes_sem_seg                → 提前 return 单个评估器
    ⑧ cityscapes_panoptic_seg 且 SEMANTIC_ON/INSTANCE_ON      → 加 Cityscapes 对应评估器
    ⑨ ade20k_panoptic_seg 且 INSTANCE_ON                      → 加 InstanceSegEvaluator
    ⑩ evaluator_type == lvis                                  → 提前 return LVISEvaluator
    出口：空 → NotImplementedError；单 → 裸返回；多 → DatasetEvaluators(list)
```

汇总成对照表（「—」表示该类型不检查此开关）：

| evaluator_type | 无条件 | PANOPTIC_ON | INSTANCE_ON | SEMANTIC_ON |
|---|---|---|---|---|
| `sem_seg` | SemSegEvaluator | — | — | — |
| `coco` | COCOEvaluator | — | — | — |
| `coco_panoptic_seg` | — | COCOPanopticEvaluator | COCOEvaluator | SemSegEvaluator |
| `ade20k_panoptic_seg` | SemSegEvaluator | COCOPanopticEvaluator | InstanceSegEvaluator（本项目） | — |
| `mapillary_vistas_panoptic_seg` | — | COCOPanopticEvaluator | InstanceSegEvaluator（本项目） | SemSegEvaluator |
| `cityscapes_instance` | 提前 return CityscapesInstanceEvaluator | — | — | — |
| `cityscapes_sem_seg` | 提前 return CityscapesSemSegEvaluator | — | — | — |
| `cityscapes_panoptic_seg` | — | COCOPanopticEvaluator | CityscapesInstanceEvaluator | CityscapesSemSegEvaluator |
| `lvis` | 提前 return LVISEvaluator | — | — | — |

#### 4.3.3 源码精读

函数开头取钥匙并准备空列表（[train_net.py:76-79](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L76-L79)）：`output_folder` 默认取 `OUTPUT_DIR/inference`，随后从 `MetadataCatalog` 查出 `evaluator_type`——这一行就是 4.2 讲的「数据说了算」。

第一段是无条件语义评估（[train_net.py:80-88](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L80-L88)）：`sem_seg` 与 `ade20k_panoptic_seg` 两种类型直接加 `SemSegEvaluator`，**不检查 SEMANTIC_ON**——这与后面 COCO 分支的条件式追加形成对照，是一个易踩的坑（见本节末尾「陷阱」）。

COCO 实例类型加原生 `COCOEvaluator`（[train_net.py:89-91](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L89-L91)）。

全景四兄弟统一走同一个 `COCOPanopticEvaluator`（[train_net.py:92-100](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L92-L100)）：四种全景类型都在名单里，但受 `PANOPTIC_ON` 把关。能统一的前提是这四个数据集的全景真值都被整理成 COCO panoptic 格式（id 图 + json）。

接着是 COCO 与 Mapillary 的条件追加（[train_net.py:101-110](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L101-L110)）：同一个数据集上按 `INSTANCE_ON`/`SEMANTIC_ON` 追加实例与语义评估器。注意 COCO 用的是 detectron2 原生 `COCOEvaluator`，而 Mapillary 用的是本仓库定制的 `InstanceSegEvaluator`（差异留给 u7-l2）。

Cityscapes 两个单任务类型提前返回（[train_net.py:111-121](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L111-L121)）：注意两点——先断言每张卡上都有 GPU 进程可用（Cityscapes 评估器不支持多机）；且 `return CityscapesInstanceEvaluator(dataset_name)` **没有传 output_folder**，与其它分支不同。

Cityscapes 全景、ADE20K 实例、LVIS 三段（[train_net.py:122-135](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L122-L135) 与 [train_net.py:136-138](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L136-L138)）：Cityscapes 全景按开关追加 Cityscapes 版评估器（同样带 GPU 断言）；ADE20K 全景数据集在 `INSTANCE_ON` 时用本项目 `InstanceSegEvaluator`；LVIS 直接返回。

为什么模型侧与评估侧能对上？看 [mask2former/maskformer_model.py:149-159](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L149-L159)：模型在 `from_config` 里读的正是同三个键 `cfg.MODEL.MASK_FORMER.TEST.SEMANTIC_ON / INSTANCE_ON / PANOPTIC_ON`（存为 `semantic_on`/`instance_on`/`panoptic_on`），决定 `forward` 推理分支产出哪些输出键。**一个配置键，生产端与消费端同时生效**——这是本讲最重要的设计。

**两个陷阱**：

1. ADE20K 全景的 `SemSegEvaluator` 是**无条件**添加的（L81 不看开关）。若你把 ADE20K 全景配置的 `SEMANTIC_ON` 设为 False，模型不再产出 `sem_seg` 键，但评估器照样构建，`process` 阶段读不到该键将报错（该读取行为在 detectron2 侧实现）。因此 ADE20K 全景配置事实上要求 `SEMANTIC_ON: True`（[configs/ade20k/panoptic-segmentation/maskformer2_R50_bs16_160k.yaml:39-45](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/ade20k/panoptic-segmentation/maskformer2_R50_bs16_160k.yaml#L39-L45) 也确实三开全开）。
2. Cityscapes 三个分支都有 CUDA 数量断言，纯 CPU 环境无法评估 Cityscapes。

#### 4.3.4 代码实践

1. **实践目标**：不动源码、不下载数据，干跑分发表，打印三种 `evaluator_type` 各自构建出的评估器组合。
2. **操作步骤**：保存以下脚本（示例代码；放在仓库外或 `Mask2Former-tutorial/` 下均可）。它在副本中把「实例化」换成「记录类名」，完整保留 if-else 与开关判断；Cityscapes 分支的 GPU 断言在干跑中跳过并注记。

   ```python
   # dispatch_dryrun.py（示例代码：build_evaluator 的干跑副本）
   from detectron2.config import get_cfg
   from detectron2.data import MetadataCatalog
   from detectron2.projects.deeplab import add_deeplab_config
   import mask2former  # noqa: F401  import 即注册
   from mask2former import add_maskformer2_config

   def make_cfg(config_file):
       cfg = get_cfg()
       add_deeplab_config(cfg)
       add_maskformer2_config(cfg)
       cfg.merge_from_file(config_file)
       return cfg

   def dispatch(cfg, dataset_name):
       """train_net.py L67-L147 的副本：只记录类名，不实例化。"""
       plan = []
       t = MetadataCatalog.get(dataset_name).evaluator_type
       S = cfg.MODEL.MASK_FORMER.TEST  # 三个开关
       if t in ["sem_seg", "ade20k_panoptic_seg"]:            # L80-88
           plan.append("SemSegEvaluator")
       if t == "coco":                                          # L89-91
           plan.append("COCOEvaluator")
       if t in ["coco_panoptic_seg", "ade20k_panoptic_seg",    # L92-100
                "cityscapes_panoptic_seg", "mapillary_vistas_panoptic_seg"] and S.PANOPTIC_ON:
           plan.append("COCOPanopticEvaluator")
       if t == "coco_panoptic_seg" and S.INSTANCE_ON:          # L101-103
           plan.append("COCOEvaluator")
       if t == "coco_panoptic_seg" and S.SEMANTIC_ON:          # L104-105
           plan.append("SemSegEvaluator")
       if t == "mapillary_vistas_panoptic_seg" and S.INSTANCE_ON:   # L106-108
           plan.append("InstanceSegEvaluator(本项目)")
       if t == "mapillary_vistas_panoptic_seg" and S.SEMANTIC_ON:   # L109-110
           plan.append("SemSegEvaluator")
       if t == "cityscapes_instance":                          # L111-116 提前 return（原函数此处还有 GPU 断言）
           return t, ["CityscapesInstanceEvaluator（提前 return，不进组合）"]
       if t == "cityscapes_sem_seg":                           # L117-121 提前 return
           return t, ["CityscapesSemSegEvaluator（提前 return，不进组合）"]
       if t == "cityscapes_panoptic_seg":                      # L122-132
           if S.SEMANTIC_ON:
               plan.append("CityscapesSemSegEvaluator")
           if S.INSTANCE_ON:
               plan.append("CityscapesInstanceEvaluator")
       if t == "ade20k_panoptic_seg" and S.INSTANCE_ON:        # L133-135
           plan.append("InstanceSegEvaluator(本项目)")
       if t == "lvis":                                         # L136-138 提前 return
           return t, ["LVISEvaluator（提前 return，不进组合）"]
       if not plan:                                            # L139-147 三个出口
           return t, ["NotImplementedError!"]
       if len(plan) == 1:
           return t, plan + ["→ 裸返回单个"]
       return t, plan + [f"→ DatasetEvaluators({len(plan)})"]

   CASES = [
       ("configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml",
        "coco_2017_val_panoptic_with_sem_seg"),
       ("configs/ade20k/panoptic-segmentation/maskformer2_R50_bs16_160k.yaml",
        "ade20k_panoptic_val"),
       ("configs/cityscapes/instance-segmentation/maskformer2_R50_bs16_90k.yaml",
        "cityscapes_fine_instance_seg_val"),
   ]
   for config_file, name in CASES:
       t, plan = dispatch(make_cfg(config_file), name)
       print(f"{name}  (evaluator_type={t})")
       for item in plan:
           print(f"   - {item}")
   ```

3. **需要观察的现象**：三个数据集各自打印出的评估器列表与出口形态。
4. **预期结果**（示意，**待本地验证**）：

   ```text
   coco_2017_val_panoptic_with_sem_seg  (evaluator_type=coco_panoptic_seg)
      - COCOPanopticEvaluator
      - COCOEvaluator
      - SemSegEvaluator
      - → DatasetEvaluators(3)
   ade20k_panoptic_val  (evaluator_type=ade20k_panoptic_seg)
      - SemSegEvaluator
      - COCOPanopticEvaluator
      - InstanceSegEvaluator(本项目)
      - → DatasetEvaluators(3)
   cityscapes_fine_instance_seg_val  (evaluator_type=cityscapes_instance)
      - CityscapesInstanceEvaluator（提前 return，不进组合）
   ```

5. 注意干跑版故意不真正 `import`/实例化评估器：`COCOEvaluator` 等的构造函数会去读磁盘上的真值标注（如 COCO 的 json），无数据时必然失败；分发逻辑本身不依赖磁盘，所以可以完整干跑。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ade20k_panoptic_seg` 的 `InstanceSegEvaluator` 分支要检查 `INSTANCE_ON`，而 `SemSegEvaluator` 分支不用检查 `SEMANTIC_ON`？
**答案**：这只是源码当前的写法差异（L81 与 L134），并非设计必然。效果上 ADE20K 全景配置把三开关全开（模型输出齐全），所以两种写法等价；但如果你手动关掉 `SEMANTIC_ON`，无条件分支就会构建一个读不到 `sem_seg` 输出的评估器，运行期报错——这正是需要记住的陷阱。

**练习 2**：`coco_panoptic_seg` + `INSTANCE_ON` 用的是 detectron2 原生 `COCOEvaluator`，而 `ade20k_panoptic_seg` + `INSTANCE_ON` 用的是本项目 `InstanceSegEvaluator`，为什么？
**答案**：两者真值格式不同——COCO 有现成的实例标注 json 可直接喂给原生 COCOEvaluator；ADE20K 的实例真值由全景标注整理而来，本项目定制的 `InstanceSegEvaluator` 放宽/调整了原生实现里若干面向检测的假设（细节是 u7-l2 的主题）。

**练习 3**：若某配置 `evaluator_type == "coco_panoptic_seg"` 且三个开关全 False，会发生什么？
**答案**：列表为空，函数走到 [train_net.py:139-144](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L139-L144) 抛出 `NotImplementedError: no Evaluator for the dataset ... with the type ...`。

### 4.4 DatasetEvaluators 组合器与三个出口

#### 4.4.1 概念说明

一个数据集可能同时要算 PQ、AP、mIoU 三张表，`build_evaluator` 的解法是**组合模式（Composite pattern）**：把多个评估器塞进 detectron2 的 `DatasetEvaluators` 容器。容器对外仍是一个普通评估器（实现同一套 reset/process/evaluate 协议），对内把每个阶段广播给所有子评估器，最后把各自的指标字典合并成一个大字典返回。这样训练引擎完全不需要关心「这一组里有几个评估器」。

#### 4.4.2 核心流程

```text
build_evaluator 的出口（三分支 + 三个提前 return）
  A. cityscapes_instance / cityscapes_sem_seg / lvis → 提前 return 单个评估器
  B. 列表为空   → NotImplementedError（防止静默地「什么都不评」）
  C. 列表恰一个 → 裸返回该评估器（省一层包装）
  D. 列表多个   → return DatasetEvaluators(list)

评估期（detectron2 引擎驱动，对引擎透明）
  reset()    → 广播给每个子评估器
  process()  → 同一批 (inputs, outputs) 喂给每个子评估器，各取所需键
  evaluate() → 各子评估器返回指标字典 → 合并为一个 OrderedDict
              （若两个子评估器产出同名顶层键会冲突，detectron2 侧做断言检查，行号待确认）
```

#### 4.4.3 源码精读

三个出口的原文（[train_net.py:139-147](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L139-L147)）：空列表显式报错比静默返回更有价值——评估白跑是训练期最难发现的浪费；单元素时直接返回，避免多余包装：

```python
if len(evaluator_list) == 0:
    raise NotImplementedError(...)
elif len(evaluator_list) == 1:
    return evaluator_list[0]
return DatasetEvaluators(evaluator_list)
```

容器类从 detectron2 导入（[train_net.py:35-44](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L35-L44)）：同一个 import 块还集中引入了 `CityscapesInstanceEvaluator`、`CityscapesSemSegEvaluator`、`COCOEvaluator`、`COCOPanopticEvaluator`、`LVISEvaluator`、`SemSegEvaluator` 与 `verify_results`——分发表里用到的全部兵种；而本仓库唯一的自研评估器 `InstanceSegEvaluator` 从 `mask2former` 包导入（[train_net.py:50-59](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L50-L59)，导出位置 [mask2former/__init__.py:26](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/__init__.py#L26)）。

三个开关的默认值注册在 [mask2former/config.py:54-60](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L54-L60)：默认 `SEMANTIC_ON=True`、`INSTANCE_ON=False`、`PANOPTIC_ON=False`（语义分割的默认世界观），COCO 全景配置再全部打开（[maskformer2_R50_bs16_50ep.yaml:40-45](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml#L40-L45)）。

TTA 路径复用同一分发逻辑（[train_net.py:270-275](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L270-L275)）：对 `cfg.DATASETS.TEST` 里每个数据集各建一组 `DatasetEvaluators`，说明「一个数据集一组组合」的粒度是稳定的。

#### 4.4.4 代码实践

1. **实践目标**：验证「关闭 `PANOPTIC_ON` 后组合如何变化」，并把现象与模型侧行为对应起来。
2. **操作步骤**：
   - 在 4.3.4 的脚本里给 `make_cfg` 增加命令行覆盖，然后以 COCO 全景配置 + `MODEL.MASK_FORMER.TEST.PANOPTIC_ON False` 再跑一次 dispatch：

     ```python
     cfg = make_cfg("configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml")
     cfg.merge_from_list(["MODEL.MASK_FORMER.TEST.PANOPTIC_ON", "False"])
     t, plan = dispatch(cfg, "coco_2017_val_panoptic_with_sem_seg")
     ```

   - 有数据与权重时，用真实评估对照（**待本地验证**）：

     ```bash
     python train_net.py \
       --config-file configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml \
       --eval-only \
       MODEL.WEIGHTS /path/to/checkpoint_file \
       MODEL.MASK_FORMER.TEST.PANOPTIC_ON False
     ```

3. **需要观察的现象**：干跑列表里 `COCOPanopticEvaluator` 消失，出口从 `DatasetEvaluators(3)` 变为 `DatasetEvaluators(2)`；真实评估日志里不再出现 PQ 指标表，AP 与 mIoU 仍在。
4. **预期结果**（示意，**待本地验证**）：

   ```text
   coco_2017_val_panoptic_with_sem_seg  (PANOPTIC_ON=False)
      - COCOEvaluator
      - SemSegEvaluator
      - → DatasetEvaluators(2)
   ```

5. **解释**：`PANOPTIC_ON=False` 同时改了生产端与消费端——模型侧 `forward` 不再产出 `panoptic_seg` 键（[maskformer_model.py:157-159](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L157-L159)），评估侧不再构建 `COCOPanopticEvaluator`（[train_net.py:99-100](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L99-L100)）。若只关一头（例如评估器仍在、模型不产出），`COCOPanopticEvaluator.process` 取不到 `panoptic_seg` 键会直接崩溃。这正是「同一配置键驱动两端」的意义：改一个键，两端同步。另可注意：因 `INSTANCE_ON` 仍为 True，模型侧 `sem_seg_postprocess_before_inference` 依然被强制开启（[maskformer_model.py:149-152](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L149-L152)）——u6-l1 讲过的规则在此处显形。

#### 4.4.5 小练习与答案

**练习 1**：为什么单元素时不也包一层 `DatasetEvaluators`？
**答案**：功能上等价，但裸返回少一层间接、日志更干净；detectron2 引擎对「单个评估器」与「组合评估器」一视同仁（都实现同一协议），所以两种返回类型对上层完全透明。

**练习 2**：`COCOPanopticEvaluator`、`COCOEvaluator`、`SemSegEvaluator` 合并结果时为什么不会键冲突？
**答案**：三者产出的顶层指标键各不相同（全景评估的指标挂在全景分割键下、实例评估挂在 segm/bbox 键下、语义评估挂在 sem_seg 键下，具体键名以 detectron2 各评估器的 `evaluate()` 实现为准），天然错开；`DatasetEvaluators` 合并时仍会做同名键检查兜底。

**练习 3**：`output_folder` 不传时的默认值是什么？谁会用到它？
**答案**：`os.path.join(cfg.OUTPUT_DIR, "inference")`（[train_net.py:76-77](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L76-L77)）；各评估器把中间产物落在这里（如实例预测 json、panoptic 预测目录），TTA 路径则显式传入 `inference_TTA` 以隔离。

## 5. 综合实践

把 4.2/4.3/4.4 的碎片串成一个「分发表全景图」小工具：

1. **任务**：扩展 4.3.4 的脚本——
   - 第一步：用 4.2.4 的方法枚举 catalog 中所有带 `evaluator_type` 的数据集；
   - 第二步：对三份配置（COCO 全景、ADE20K 全景、Cityscapes 实例各一份 yaml）分别干跑 dispatch，并额外对 COCO 配置各生成「三开关全开」「仅关 PANOPTIC_ON」「仅开 SEMANTIC_ON」三种开关组合的结果；
   - 第三步：把全部结果整理成一张 Markdown 表（行 = 数据集 × 开关组合，列 = 评估器组合与出口形态）。
2. **需要观察/解释的三个问题**：
   - `PANOPTIC_ON=False` 时组合从 3 变 2，且模型侧同时停止产出 `panoptic_seg`——为什么两端必须同步？
   - ADE20K 全景即使 `SEMANTIC_ON=False`，干跑列表里仍会有 `SemSegEvaluator`——这说明源码里藏着什么隐含约束？
   - `cityscapes_fine_instance_seg_val` 永远是提前 return 的单评估器——它与 `coco_panoptic_seg` 走列表组合的方式有何本质区别？
3. **预期产出**：一张约 6-8 行的对照表，能回答上面三个问题。全部为干跑，无需数据与 GPU；若某行报 `NotImplementedError`，把它也记进表里（这正是出口 B 的样例）。

## 6. 本讲小结

- `build_evaluator` 是被 detectron2 引擎回调的类方法：周期评估、`--eval-only`、TTA 三条路径都会（直接或间接）按 `cfg.DATASETS.TEST` 逐数据集调用它。
- 分发由两把钥匙决定：数据集元数据的 `evaluator_type`（标注能评什么）+ `MODEL.MASK_FORMER.TEST` 三开关（模型产出什么），同一组配置键同时驱动模型侧 `forward` 与评估侧组合。
- `coco_panoptic_seg` + 三开关全开是典型组合：`COCOPanopticEvaluator` + `COCOEvaluator` + `SemSegEvaluator` 三合一，对应 `_with_sem_seg` 变体数据集「一份注册、三套真值路径」的设计。
- 三个出口：空列表抛 `NotImplementedError`、单元素裸返回、多元素包成 `DatasetEvaluators` 组合器；Cityscapes 单任务与 LVIS 另有提前 return。
- 两个陷阱：ADE20K 全景的 `SemSegEvaluator` 无条件追加（隐含要求 `SEMANTIC_ON=True`）；Cityscapes 全系评估器要求 GPU。

## 7. 下一步学习建议

- 下一讲 u7-l2 精读本讲反复出现的 `InstanceSegEvaluator`（[instance_evaluation.py:30-107](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/evaluation/instance_evaluation.py#L30-L107)）：它相对原生 `COCOEvaluator` 改了什么、Mask2Former 没有真实框的占位 `pred_boxes` 如何通过 COCO api 得到 mask AP。
- 之后 u7-l3 走出 `train_net.py`，看仓库自带的离线指标工具（语义分割输出算 PQ 的脚本与 boundary AP），理解「内置评估器没有覆盖的指标如何补」。
- 想加深对「三开关联动」的理解，可回头对照 [maskformer_model.py:149-159](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L149-L159) 与本讲的 [train_net.py:99-110](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L99-L110)，把 u6 三讲的三条推理分支与三个评估器一一配对。

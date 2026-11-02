# InstanceSegEvaluator：面向 mask 分类的 AP 评估

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `InstanceSegEvaluator` 在整个评估链路中的位置：它由 `Trainer.build_evaluator` 在什么条件下创建（u7-l1），消费的是 `MaskFormer.instance_inference` 产出的什么字段（u6-l2）。
2. 讲清它相对 detectron2 原生 `COCOEvaluator` **真正改了什么**：只覆写了一个方法 `_eval_predictions`，而且核心改动是**类别 id 反映射的宽松校验**——这背后是 ADE20K / Mapillary 全景数据集「thing 连续 id 不从 0 开始、不密集」的事实。
3. 读懂一张 COCO AP 表：AP / AP50 / AP75、APs / APm / APl、AR 的含义、匹配规则与 101 点插值的计算方式，并知道 Model Zoo 里的单列 AP 就是其中哪个数。
4. 解释「Mask2Former 没有真实框」如何传导到评估侧：占位的全零 `pred_boxes` 照样被写进 COCO json 的 `bbox` 字段，但实例评估只看 `segm` 任务（mask IoU）。
5. 独立跑通一个不依赖本仓库环境的 pycocotools 玩具实验，亲手制造一条「IoU 偏低的预测」并观察 AP50 / AP75 如何分化。

## 2. 前置知识

本讲是评估主题的第二讲，默认你已学完 u6-l2（实例推理分支）与 u7-l1（评估器分发）。再用三段话补齐评估学的几个基础概念。

**IoU 与 mask IoU。** 交并比（Intersection over Union）衡量两个二值区域的重合程度：

\[ \mathrm{IoU}(p, g) = \frac{|p \cap g|}{|p \cup g|} \]

检测任务里 \(p\)、\(g\) 是两个矩形框；实例分割里它们是两张二值 mask。mask IoU 的分子分母都是像素计数，工程上用 RLE（Run-Length Encoding，行程长度编码）把 mask 压成「连续同值段」的一维序列后按位统计，比逐像素比较高效得多——pycocotools 的所有 mask 运算都基于 RLE。

**precision / recall / AP。** 对一个类别，把模型的所有预测按置信度从高到低排序，逐条判定「命中某个尚未被占用的同类别 GT（IoU ≥ 阈值 t）」或「误检」。遍历过程中每一步都能算出一对

\[ \text{precision} = \frac{\mathrm{TP}}{\mathrm{TP}+\mathrm{FP}}, \qquad \text{recall} = \frac{\mathrm{TP}}{\mathrm{TP}+\mathrm{FN}} = \frac{\mathrm{TP}}{N_{gt}} \]

得到一条 PR 曲线。AP（Average Precision）就是这条曲线下的面积。COCO 的约定是：在 recall 轴上取 101 个等距点做插值求平均，再对 10 个 IoU 阈值 \( \mathcal{T} = \{0.50, 0.55, \ldots, 0.95\} \) 再平均：

\[ \mathrm{AP} = \frac{1}{|\mathcal{T}|}\sum_{t \in \mathcal{T}} \left( \frac{1}{101}\sum_{i=0}^{100} p_{\text{interp}}\!\left(\tfrac{i}{100}\right) \right) \]

- **AP50 / AP75**：只看单个 IoU 阈值 0.5 / 0.75 的 AP，分别对应「宽松命中」与「严格命中」。
- **APs / APm / APl**：按 GT 实例的像素面积分桶（small：\( < 32^2 \)；medium：\( 32^2 \sim 96^2 \)；large：\( > 96^2 \)），小物体 mask 边缘误差占比大，通常 APs 明显低于 APl。
- **AR**（Average Recall）：在每图最多取 maxDets（100）条预测的约束下，对 IoU 阈值平均的召回率。
- 表中数值均为百分制（0~100），-1 或 NaN 表示该桶内没有 GT、无法计算。

**「实例评估」在评什么。** 一句话：给定「每图若干个 (mask, 类别, 分数) 的列表」与 GT 的「每图若干个 (mask, 类别)」，用上述协议打分。它不关心 mask 是怎么来的——可以来自框回归（如 Mask R-CNN），也可以来自 Mask2Former 的 query 点积（u4-l4）。评估器是纯粹的**下游消费者**，这正是本讲的主角能只写 108 行的原因：消费侧的一切脏活累活都由 detectron2 父类与 pycocotools 包揽。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [mask2former/evaluation/instance_evaluation.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/evaluation/instance_evaluation.py) | **本讲主角**，全仓库唯一的自研评估器，108 行，仅覆写 `_eval_predictions` |
| [train_net.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py) | 消费方：`build_evaluator` 在两个分支创建它 |
| [mask2former/maskformer_model.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py) | 生产方：`instance_inference` 产出被评估的 `Instances` 四字段 |
| [mask2former/data/datasets/register_ade20k_instance.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/datasets/register_ade20k_instance.py) | 对照样本：ADE20K 实例数据集的**密集** id 映射 |
| [mask2former/data/datasets/register_ade20k_panoptic.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/datasets/register_ade20k_panoptic.py) | 证据：全景注册里 thing 连续 id 取自 150 类**全集枚举**（不密集） |
| [mask2former/data/datasets/register_mapillary_vistas_panoptic.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/datasets/register_mapillary_vistas_panoptic.py) | 同款构造：Mapillary 的 thing 连续 id 取自 133 类全集枚举 |
| [datasets/prepare_ade20k_ins_seg.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/datasets/prepare_ade20k_ins_seg.py) | GT json 的生产脚本：实例与全景标注共享一套类别 id |
| [MODEL_ZOO.md](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/MODEL_ZOO.md) | 指标出口：ADE20K 实例表的单列 AP 从哪来 |

注意：`COCOEvaluator`、`_evaluate_predictions_on_coco`、`instances_to_coco_json`、`COCOeval` 等来自 **detectron2 / pycocotools 依赖**，不在本仓库内，本讲对它们只做职责层面的描述（无永久链接、无行号），请你结合本地安装的版本对照阅读。

## 4. 核心概念与源码讲解

### 4.1 模块一：InstanceSegEvaluator 的定位与生命周期

#### 4.1.1 概念说明

u7-l1 已经画过分发图：`build_evaluator` 按「数据集元数据的 `evaluator_type` + 模型三开关」组装评估器。本仓库共两个分支会挂上 `InstanceSegEvaluator`——Mapillary 全景（`INSTANCE_ON`）与 ADE20K 全景（`INSTANCE_ON`）。而同样开了 `INSTANCE_ON` 的 COCO 全景用的却是 detectron2 **原生** `COCOEvaluator`。

这就抛出本讲的核心问题：**为什么 ADE20K / Mapillary 需要一个修改版，COCO 却不需要？**

答案有两层，分别对应 Mask2Former 范式的两个特点：

1. **模型没有真实框**。mask classification 范式下（u3-l1、u6-l2），每个实例只有一张 mask，`instance_inference` 给 `pred_boxes` 填的是全零占位张量。原生 `COCOEvaluator` 是为「框来自回归、可同时评 bbox 与 segm」的检测器设计的；在 Mask2Former 场景里 bbox 指标失去意义，评估重心完全落在 mask 上。（后面会看到：这一层其实**不需要**覆写就能工作，只是产出一个无意义的 bbox 表。）
2. **类别连续 id 不密集**。这是**必须**覆写的硬理由：原生实现假设 `thing_dataset_id_to_contiguous_id` 的值域恰好是 `[0, N)` 的密集区间并据此做严格断言，而 ADE20K / Mapillary 全景注册构造出的值域不满足该假设，原生实现会直接断言失败。推导过程放在 4.3 精读。

先记住结论：**`InstanceSegEvaluator` 不是重写，而是「最小手术」——继承 `COCOEvaluator` 的全部基础设施，只替换 `_eval_predictions` 中的类别 id 反映射校验。**

#### 4.1.2 核心流程

评估器由 detectron2 引擎按固定生命周期驱动（父类实现）：

```
构造 InstanceSegEvaluator(dataset_name, output_dir=...)
 └─ 读取元数据、定位 GT json（如 ade20k_instance_val.json）
 └─ self._tasks 为空（未显式指定任务）
reset()            # 清空累积的预测
  ↓ 引擎逐批推理
process(inputs, outputs)   # 每批调用
 └─ 取 outputs["instances"]（detectron2 Instances 对象）
 └─ 转成 COCO json 条目（image_id / category_id / bbox / score / segmentation-RLE）
 └─ 追加进 self._predictions
  ↓ 推理结束
evaluate()
 └─ 跨进程收集预测（分布式）
 └─ 调 self._eval_predictions(...)   ← 本仓库唯一覆写的方法
     ├─ 展平 + 推断任务集合
     ├─ 类别 id 反映射（模型连续 id → 数据集原始 id）★ 改动点
     ├─ 落盘 coco_instances_results.json
     ├─ 逐任务调 _evaluate_predictions_on_coco（COCOeval）
     └─ _derive_coco_results 生成 Markdown 风格 AP 表
```

覆写只发生在最后一格；`reset / process / evaluate` 的骨架、GT 加载、json 转换全部复用父类。

#### 4.1.3 源码精读

类的声明与模块注释：

- [mask2former/evaluation/instance_evaluation.py:L29-L30](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/evaluation/instance_evaluation.py#L29-L30)：注释 `modified from COCOEvaluator for instance segmetnat`（原文拼写如此）+ 类定义 `class InstanceSegEvaluator(COCOEvaluator)`。整个文件没有任何 `def __init__` / `def process`——全部继承。
- [mask2former/evaluation/instance_evaluation.py:L22-L23](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/evaluation/instance_evaluation.py#L22-L23)：从 detectron2 导入父类 `COCOEvaluator`、模块级函数 `_evaluate_predictions_on_coco`（覆写版直接复用它，不必自己 `super()` 绕行），以及加速版评估器 `COCOeval_opt`（L23 导入后交由 `_evaluate_predictions_on_coco` 内部按需选用，本文件内不直接使用）。
- [mask2former/evaluation/instance_evaluation.py:L30-L41](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/evaluation/instance_evaluation.py#L30-L41)：类的 docstring 基本照搬父类（还提到 keypoint 评估，实际本仓库场景只可能出现 bbox / segm 两个任务），并给出 COCO 官方评估协议的链接与「指标 0~100、-1/NaN 表示不可计算」的约定。

两个创建点（u7-l1 分发图的局部放大）：

- [train_net.py:L107-L108](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L107-L108)：`evaluator_type == "mapillary_vistas_panoptic_seg"` 且 `INSTANCE_ON` 时追加 `InstanceSegEvaluator`。
- [train_net.py:L134-L135](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L134-L135)：`evaluator_type == "ade20k_panoptic_seg"` 且 `INSTANCE_ON` 时追加。
- 对照 [train_net.py:L102-L103](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L102-L103)：COCO 全景 + `INSTANCE_ON` 用的是**原生** `COCOEvaluator`——作者精确地只在「需要」的数据集上换用修改版。

它在包入口被 re-export，`import mask2former` 即可用：

- [mask2former/__init__.py:L26](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/__init__.py#L26)：`from .evaluation.instance_evaluation import InstanceSegEvaluator`。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：不看本讲正文，自己从源码推出「哪些数据集 × 哪些开关会用到 `InstanceSegEvaluator`」，并解释 COCO 为什么例外。
2. **操作步骤**：
   - 在 [train_net.py:L67-L147](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L67-L147) 中 `grep InstanceSegEvaluator`，记下每个命中行的上下文条件（`evaluator_type` 与开关）；
   - 再 `grep COCOEvaluator`，对比 L91（`evaluator_type == "coco"`，ADE20K 纯实例配置的数据集 `ade20k_instance_val` 由 detectron2 的 `register_coco_instances` 注册、默认就是这个类型）与 L102-L103 两处的原生用法；
   - 写成一张三列表：数据集 / `INSTANCE_ON` 时用哪个评估器 / 类别 id 映射来自哪个 register 文件（提示：分别是 `register_ade20k_panoptic.py`、`register_mapillary_vistas_panoptic.py`、`register_coco_panoptic_annos_semseg.py`、`register_ade20k_instance.py`）。
3. **需要观察的现象**：换用修改版的两个数据集，其 thing id 映射都由「全集枚举」构造（4.3 精读）；用原生评估器的两个，要么映射密集（COCO / ADE20K 纯实例），要么数据集本身是 COCO 格式标准 id。
4. **预期结果**：得出「修改版 ⇔ 映射值域不密集」的对应关系，为 4.3 的断言分析做好铺垫。

#### 4.1.5 小练习与答案

**练习 1**：`InstanceSegEvaluator` 覆写了几个方法？这说明了什么工程习惯？
**答案**：只覆写 `_eval_predictions` 一个（[instance_evaluation.py:L43](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/evaluation/instance_evaluation.py#L43)）。它说明该类是「最小改动式定制」：尽量复用父类的生命周期与转换逻辑，只动必须动的分支，降低与上游 detectron2 的维护偏差。

**练习 2**：`--eval-only` 会不会走到 `InstanceSegEvaluator`？
**答案**：会。u7-l1 讲过 `--eval-only` 与周期评估最终都调用 `build_evaluator`；只要 `DATASETS.TEST` 里数据集的 `evaluator_type` 命中 L107 或 L134 且 `INSTANCE_ON=True`，实例评估就会执行。

**练习 3**：为什么文件要 `from detectron2.evaluation.fast_eval_api import COCOeval_opt`（L23），却没在本文件里出现第二次？
**答案**：`COCOeval_opt` 由同模块导入的 `_evaluate_predictions_on_coco` 在内部按 `use_fast_impl` 开关选用；本文件导入它只是（显式或隐式地）确保加速实现可用。阅读依赖代码时，「导入了但没直接用」往往是指向真实调用点的路标。

### 4.2 模块二：输入契约——从 Instances 四字段到 COCO json

#### 4.2.1 概念说明

评估器的输入不是模型内部张量，而是 `MaskFormer.forward` 推理分支返回的 `processed_results`：

- [mask2former/maskformer_model.py:L258-L260](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L258-L260)：`if self.instance_on:` 时把 `instance_inference` 的返回写进 `processed_results[-1]["instances"]`——这正是 4.1.2 生命周期里 `process()` 取的 `outputs["instances"]` 键（[instance_evaluation.py:L48](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/evaluation/instance_evaluation.py#L48) 的 `x["instances"]` 与之首尾相接）。

`Instances` 是 detectron2 的动态字段容器。父类的 `process` 会把它逐字段翻译成 COCO json 条目（detectron2 侧函数 `instances_to_coco_json` 的契约）：

| Instances 字段 | COCO json 键 | 说明 |
| --- | --- | --- |
| `pred_boxes` | `bbox` | 转 XYWH_ABS 格式（左上角 + 宽高），4 个数 |
| `scores` | `score` | 置信度，用于排序 |
| `pred_classes` | `category_id` | 模型连续 id，评估前会被 4.3 的反映射转回数据集原始 id |
| `pred_masks` | `segmentation` | 二值 mask，RLE 编码（`counts` 解码为 utf-8 字符串以便 json 序列化） |

对照 u6-l2 精读过的 `instance_inference` 尾段，四个字段逐一就位：

- [mask2former/maskformer_model.py:L369-L379](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L369-L379)：构造 `Instances(image_size)` 并挂载 `pred_masks / pred_boxes / scores / pred_classes`。
- [mask2former/maskformer_model.py:L371](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L371)：`pred_masks = (mask_pred > 0).float()`——logits 过 0 阈值硬二值化，`segm` 任务的 IoU 全部基于这张 0/1 mask。
- [mask2former/maskformer_model.py:L372-L374](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L372-L374)：`pred_boxes = Boxes(torch.zeros(mask_pred.size(0), 4))` 全零占位；紧接着**被注释掉的**替代方案 `BitMasks(mask_pred > 0).get_bounding_boxes()`（作者注明 it is slow）。这就是「无真实框」的源头。
- [mask2former/maskformer_model.py:L377-L378](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L377-L378)：重打分 `score = 分类分数 × mask 区域内平均 sigmoid 概率`（u6-l2），进入 json 的 `score` 字段——它决定 AP 排序，直接同时影响 precision 与 recall。

#### 4.2.2 核心流程

```
instance_inference（每图）
  → Instances{pred_masks(0/1), pred_boxes(全零), scores(重打分), pred_classes(模型连续 id)}
    → process()：instances_to_coco_json（detectron2 父类）
      → [{image_id, category_id, bbox=[0,0,0,0], score, segmentation=RLE}, ...]
        → _eval_predictions 的 unmap（4.3）把 category_id 转回数据集原始 id
          → COCOeval(iouType="segm")：只看 segmentation，bbox 无人消费
```

两个易错点提前点破：

1. **占位框不会让评估崩溃，只是产出无意义的 bbox 指标**。全零 `[0,0,0,0]` 的框与任何 GT 框 IoU 为 0，bbox 任务的 AP 趋近 0（若无任何命中则为 -1/NaN）。所以读日志时要**只看 `segm` 表**——这也是 Model Zoo 实例表只有一列 AP 的原因。
2. **mask 是硬二值化后的**，不是 sigmoid 概率图。评估用的是离散 IoU，阈值 0 之前已在模型侧定死。

#### 4.2.3 源码精读

- [mask2former/evaluation/instance_evaluation.py:L47-L48](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/evaluation/instance_evaluation.py#L47-L48)：`Preparing results for COCO format ...` 之后 `itertools.chain(*[x["instances"] for x in predictions])` 把「每图一个 list」展平成全数据集一维列表 `coco_results`——每个元素就是上表的一行 json 条目。注意 `x` 里**只取** `"instances"` 键：u6-l1 的 `"sem_seg"`、u6-l3 的 `"panoptic_seg"` 与本评估器无关（它们归 `SemSegEvaluator` / `COCOPanopticEvaluator` 管）。
- [mask2former/maskformer_model.py:L344-L357](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L344-L357)：复习 u6-l2 的选例逻辑——`softmax` 去掉 no-object 通道（L349）、query×class 展开取 topk（L352-L353）、`topk_indices // num_classes` 还原 query 索引取 mask（L355-L357）。条目数默认 `TEST.DETECTIONS_PER_IMAGE = 100`，恰好与 COCO 协议的 maxDets=100 上限对齐。
- [mask2former/maskformer_model.py:L360-L367](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L360-L367)：全景模式下按 `thing_dataset_id_to_contiguous_id.values()` 过滤——这段代码同时透露了一个关键事实：**模型连续 id 空间与该映射的值域是同一个空间**（否则过滤会把所有预测滤空）。4.3 将沿这条线索展开。

#### 4.2.4 代码实践（源码阅读 + 手工推导）

1. **实践目标**：不运行任何代码，为一张假想的推理输出手工写出它的 COCO json 条目，检验对输入契约的理解。
2. **操作步骤**：
   - 设某图原始尺寸 512×384，`instance_inference` 选出 2 个实例：`(query#7, 类别 12 person, 分类分 0.9, mask 均值概率 0.8)` 与 `(query#3, 类别 15 table, 分类分 0.6, mask 均值概率 0.5)`；
   - 按 L369-L379 逐字段写出两个 `Instances` 字段的值（`scores` 要用重打分公式先算出来）；
   - 再按本节的字段对照表翻译成两条 COCO json（`segmentation` 用 `<RLE of 512×384 mask>` 占位即可）；
   - 最后对照原生 `COCOEvaluator` 的场景（Mask R-CNN 类模型）：它的 `bbox` 字段会是什么？由此归纳两者的「输入字段要求」差异。
3. **需要观察的现象**：`scores` 分别为 `0.9×0.8=0.72` 与 `0.6×0.5=0.30`；两条 json 的 `bbox` 都是 `[0.0, 0.0, 0.0, 0.0]`。
4. **预期结果**：两条形如 `{"image_id": 42, "category_id": 12, "bbox": [0,0,0,0], "score": 0.72, "segmentation": {...}}` 的条目；归纳出的差异是——**字段集合两者完全一致**（原生同样接受 `pred_masks` 可选），差别在于 Mask2Former 场景下 `bbox` 语义失效、评估只能依赖 `segmentation`，以及 4.3 将看到的类别 id 校验差异。

#### 4.2.5 小练习与答案

**练习 1**：如果把 L372 的占位框换成被注释的 `BitMasks(...).get_bounding_boxes()`（需要 mask 已在原图尺寸），`InstanceSegEvaluator` 的结果会怎么变？
**答案**：`segm` 任务的 AP 完全不变（它不读 `bbox`）；`bbox` 任务的 AP 从 ≈0 变成由 mask 外接框算出的真实框 AP。代价是逐实例求外接框的计算开销（作者注释的 slow）。

**练习 2**：`pred_masks` 若不经过 L371 的 `> 0` 二值化、直接把 float mask 塞进 `process`，会发生什么？
**答案**：RLE 编码要求 uint8 输入，float 概率图要么编码失败要么语义错误；即便强行编码，COCO 的 mask IoU 定义在二值集合上（\( \frac{|p\cap g|}{|p\cup g|} \) 的像素计数），连续值无定义。硬二值化是评估侧的硬性前置。

**练习 3**：为什么 `scores` 用「分类分 × mask 均值概率」而不是纯分类分，对 AP 是有影响的？
**答案**：AP 对预测的**排序**敏感（PR 曲线按 score 降序遍历）。重打分改变了排序：mask 质量差的实例被压低，更晚进入遍历，从而在同等召回下减少前置的误检、抬升 precision。u6-l2 的「挑选与定序分离」在评估侧的效果就体现在这里。

### 4.3 模块三：`_eval_predictions` 精读——id 反映射与那处被注释掉的断言

#### 4.3.1 概念说明：三种类别 id 空间

要读懂覆写版与原生版的差异，必须先分清同一条 `category_id` 在不同阶段属于哪个空间：

| 空间 | 例（ADE20K） | 出现位置 |
| --- | --- | --- |
| ① 数据集原始 id（dataset id） | person = 12，flag = 149，散布于 0~149 | GT json、COCO api、最终指标 |
| ② 全集枚举 id（semantic 顺序） | 同①（prepare 脚本保证两套共享） | `ade20k_panoptic_*` 的映射值 |
| ③ 模型连续 id（contiguous） | thing 密集 0~99 或全景全集 0~149 | `pred_classes`、分类头输出维度 |

评估前必须把 ③ 反映射回 ①（`reverse_id_mapping`），因为 GT json 里存的是 ①。**问题在于：映射「③ → ①」的值域长什么样，取决于数据集的注册方式**，而这正是原生断言崩溃的地方。

看两个决定性的构造：

- 密集版（[register_ade20k_instance.py:L29-L32](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/datasets/register_ade20k_instance.py#L29-L32)）：`thing_dataset_id_to_contiguous_id = {k: i for i, k in enumerate(thing_ids)}`，注释直言 "Mapping from the incontiguous ADE category id to an id in [0, 99]"。值域是**密集的** `[0, 99]`——满足原生断言。
- 稀疏版（[register_ade20k_panoptic.py:L355-L357](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/datasets/register_ade20k_panoptic.py#L355-L357) 与 [register_mapillary_vistas_panoptic.py:L474-L476](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/datasets/register_mapillary_vistas_panoptic.py#L474-L476)）：`for i, cat in enumerate(全部分类): if cat["isthing"]: 映射[cat["id"]] = i`。**值域取自全类集的枚举位置**——ADE20K 的 150 类里 thing 与 stuff 交错（wall=id0 是 stuff、bed=id7 是 thing、…、flag=id149 是 thing），100 个 thing 的值域是 `{7, 8, 10, ..., 149}`：`min=7 ≠ 0`、`max=149 ≠ 99`。Mapillary 同理（133 类全集枚举）。

原生 `COCOEvaluator` 在 unmap 段断言「值域 `min == 0` 且 `max == N-1`」并检查 `category_id < N`。对稀疏版数据集，第一个断言直接失败——**这就是 ADE20K / Mapillary 全景必须换用 `InstanceSegEvaluator` 的硬理由**，也是 4.1.4 实践表格里「修改版 ⇔ 值域不密集」对应关系的代码根源。

顺带补一块拼图：为什么 GT json 里可以存 ① 空间？因为 [datasets/prepare_ade20k_ins_seg.py:L36-L50](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/datasets/prepare_ade20k_ins_seg.py#L36-L50) 在生成实例 json 时，通过 `ade20k_instance_catid_mapping.txt` 把 Instance100 的类别号搬到 SceneParse150 的 id 上（`map_id[ins_id] = sem_id - 1`），注释强调 "it is important to share category id for both instance and panoptic annotations"——实例与全景标注共用同一套原始 id，评估端只需一份反映射。

#### 4.3.2 核心流程

覆写版 `_eval_predictions` 的五段式：

```
输入: predictions = 每图 {"instances": [json条目…]} 的列表
① 展平为 coco_results（L48）
② tasks = self._tasks 或从预测字段推断（L49）
③ 类别 id 反映射（L52-L70）★ 与原生的唯一实质差异
    if 元数据含 thing_dataset_id_to_contiguous_id:
        reverse = {连续id: 原始id}
        对每个条目: assert category_id in reverse; 改写回原始 id
④ 落盘 coco_instances_results.json（L72-L76）；_do_evaluation=False 则到此为止（L79-L81）
⑤ for task in sorted(tasks):
        coco_eval = _evaluate_predictions_on_coco(GT api, coco_results, task, …)
        res = _derive_coco_results(coco_eval, task, class_names=thing_classes)
```

#### 4.3.3 源码精读

逐段对照：

- [instance_evaluation.py:L47-L49](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/evaluation/instance_evaluation.py#L47-L49)：展平与任务推断。构造时未传 `tasks`，于是走 `_tasks_from_predictions`（detectron2 侧）：按预测条目携带的字段推断要评估的任务集合——Mask2Former 的条目总是同时带 `bbox` 与 `segmentation` 字段，因此典型会得到 `bbox` 与 `segm` 两个任务，日志里会看到两张表。
- [instance_evaluation.py:L52-L58](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/evaluation/instance_evaluation.py#L52-L58)：**被注释掉的原生断言**就躺在 L54-L56（`all_contiguous_ids` / `min(...) == 0 and max(...) == num_classes - 1`）——这是作者留给我们的「修改现场」，无声胜有声地指出原生实现的问题所在。L58 构造反向表 `reverse_id_mapping = {v: k for k, v in ...}`。
- [instance_evaluation.py:L59-L70](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/evaluation/instance_evaluation.py#L59-L70)：逐条改写 `category_id`。新的校验是**宽松的成员检查** `assert category_id in reverse_id_mapping`（L66-L69，报错信息列出合法 id 集合），替代原生的两道「密集区间」断言（其一等价物在 L61-L65 也被注释掉）。语义从「值域必须是 [0,N)」放宽为「预测 id 必须是已知 thing id」。
- [instance_evaluation.py:L72-L76](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/evaluation/instance_evaluation.py#L72-L76)：把反映射后的结果写进 `output_dir/coco_instances_results.json`——想离线复算指标、或用第三方工具分析预测，直接读这个文件。
- [instance_evaluation.py:L79-L81](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/evaluation/instance_evaluation.py#L79-L81)：`_do_evaluation=False`（比如拿评估器当「推理落盘器」）时到此返回。
- [instance_evaluation.py:L88-L102](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/evaluation/instance_evaluation.py#L88-L102)：逐任务评估。注意两点：任务名断言在 `{"bbox", "segm", "keypoints"}` 内（L89）；**空预测直接给 `None`**（L100-L101 的条件表达式）——COCO api 对空结果不友好，这是防御分支。L96 还向 `_evaluate_predictions_on_coco` 显式传了 `use_fast_impl`（这一传参在旧版 detectron2 的函数签名中不存在，是本仓库对较新 detectron2 的适配痕迹之一）。
- [instance_evaluation.py:L104-L107](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/evaluation/instance_evaluation.py#L104-L107)：`_derive_coco_results`（父类方法）把 COCOeval 的统计整理成百分制小表格，`class_names=self._metadata.get("thing_classes")` 让它额外输出 per-category AP 表。

指标出口（为什么 Model Zoo 实例表只有一列 AP）：

- [MODEL_ZOO.md:L463-L481](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/MODEL_ZOO.md#L463-L481)：ADE20K Instance Segmentation 表，R50 行的 `AP = 26.4`——即 `segm` 任务的 `AP`（IoU 0.5:0.95 平均）， bbox 任务的结果被弃用。
- 对照 [MODEL_ZOO.md:L427-L443](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/MODEL_ZOO.md#L427-L443)：全景表的 `AP = 26.5`（PQ 39.7 / mIoU 46.1 旁边）正是 `INSTANCE_ON` 分支下 `InstanceSegEvaluator` 报出的同一个指标——u7-l1 的「三评估器各报各的」在数字上的落点。

#### 4.3.4 代码实践（干跑，不依赖 detectron2 / 数据集）

1. **实践目标**：亲手复现「原生断言为何失败、宽松断言为何通过」，把 4.3.1 的论断变成可运行的证据。
2. **操作步骤**：写一个独立小脚本（示例代码，仅需 Python 3）：

   ```python
   # 示例代码：复现两种 id 映射构造与原生断言的碰撞
   # thing ids 取自 register_ade20k_instance.py 的 ADE_CATEGORIES（此处摘前 5 个示意，请自行抄全 100 个）
   thing_ids = [7, 8, 10, 12, 14]  # ... 一直到 149，共 100 个

   # 构造 A：register_ade20k_instance.py L32 的密集映射
   dense = {k: i for i, k in enumerate(thing_ids)}
   vals = list(dense.values())
   print("dense  :", min(vals), max(vals), len(vals))   # 期望 0 99 100

   # 构造 B：register_ade20k_panoptic.py L355-L357 的全集枚举（此处以恒等示意：
   # cat["id"] 恰好等于枚举位置 i，因此值域 = thing_ids 本身）
   sparse_vals = thing_ids
   print("sparse :", min(sparse_vals), max(sparse_vals), len(sparse_vals))  # 期望 7 149 100

   # 原生 COCOEvaluator 的断言（其源码不在本仓库）：
   num_classes = len(sparse_vals)
   ok = (min(sparse_vals) == 0) and (max(sparse_vals) == num_classes - 1)
   print("native assert passes on sparse?", ok)         # 期望 False

   # InstanceSegEvaluator 的宽松断言（L66）：预测 id 只需是已知 thing id
   reverse = {v: k for k, v in zip(sparse_vals, sparse_vals)}  # 稀疏情形下近似恒等
   print("loose assert on pred id 12?", 12 in reverse)  # 期望 True
   ```

   把 `thing_ids` 补全为 [register_ade20k_instance.py:L12](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/datasets/register_ade20k_instance.py#L12) 中 `ADE_CATEGORIES` 的全部 100 个 id（按文件顺序抄写）。
3. **需要观察的现象**：`dense` 行输出 `0 99 100`；`sparse` 行输出 `7 149 100`；原生断言在稀疏值域上为 `False`。
4. **预期结果**：`min==7` 且 `max==149≠99`，两条原生断言条件同时被违反——若不覆写，ADE20K 全景数据集的实例评估会在 unmap 段 `AssertionError` 中止；覆写后的成员检查只拒绝「不在 thing 集合里」的预测。此实践为纯本地干跑，预期结果可复现；若抄全列表后 `len` 不是 100，说明漏抄了（可用文件里 `assert len(thing_ids) == 100` 的写法自查，见 [register_ade20k_instance.py:L30](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/datasets/register_ade20k_instance.py#L30)）。

#### 4.3.5 小练习与答案

**练习 1**：同一个 ADE20K，为什么 `ade20k_instance_val`（纯实例配置 `DATASETS.TEST`）可以用**原生** `COCOEvaluator`，而 `ade20k_panoptic_val` + `INSTANCE_ON` 必须用修改版？
**答案**：两者的 `thing_dataset_id_to_contiguous_id` 来自不同注册脚本——`register_ade20k_instance.py` 显式构造密集 `[0,99]` 映射（满足原生断言）；`register_ade20k_panoptic.py` 的映射值取自 150 类全集枚举（稀疏，原生断言失败）。分发表上前者 `evaluator_type="coco"`（detectron2 的 `register_coco_instances` 默认），后者 `"ade20k_panoptic_seg"`。

**练习 2**：宽松断言 `assert category_id in reverse_id_mapping` 失败时，最可能的模型侧原因是什么？
**答案**：模型输出的 `pred_classes` 落在了映射值域之外——典型如全景数据集上模型对某个 stuff-only 通道给出了高分成实例、但 `panoptic_on=False` 时 [maskformer_model.py:L360-L367](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L360-L367) 的 thing 过滤被跳过。报错信息会把合法 id 集合整个打印出来（L68），正好用于排查。

**练习 3**：`coco_instances_results.json` 里存的 `category_id` 是哪个空间的？为什么？
**答案**：数据集原始 id（① 空间）。写盘发生在反映射**之后**（L70 改写、L72-L76 落盘），因此该文件与 GT json 同空间，可直接喂给 COCO api 复算。

### 4.4 模块四：`_evaluate_predictions_on_coco` 与 AP 表解读

#### 4.4.1 概念说明

`_evaluate_predictions_on_coco` 是 detectron2 的模块级函数（本仓库 L22 导入）：把 GT 的 COCO api 对象与预测列表交给 pycocotools 的 `COCOeval`，`iouType="segm"` 时用 mask IoU 做 TP 判定，随后 `evaluate → accumulate → summarize` 产出 12 项统计（AP 4 项 + 按面积 3 项 + AR 5 项，均为 0~1，展示时再换算成百分制）。COCO 协议的匹配规则可概括为：

1. 每个类别**独立**计算，最后对类别平均（AP 是 mean over categories）；
2. 预测按 `score` 降序逐条尝试匹配：与同类 GT 的 mask IoU ≥ 阈值 t 且该 GT 未被更高分的预测占用，才算 TP（一对一贪心）；
3. 每图最多取 maxDets 条预测（L98 传入的 `max_dets_per_image`，默认 100）；
4. 对 10 个 IoU 阈值平均即 AP；单一阈值即 AP50/AP75；按 GT 面积分桶即 APs/APm/APl。

为什么这一套对 Mask2Former 尤其重要：mask classification 范式没有「框回归质量」这个概念，实例层面的优劣完全由 mask 质量（在多阈值下的 IoU 稳定性）与打分质量（排序）决定——AP@[.5:.95] 恰好同时惩罚这两者。

#### 4.4.2 核心流程

```
_evaluate_predictions_on_coco(coco_gt_api, coco_results, task="segm")
  ├─ coco_gt.loadRes(coco_results)      # 预测注入 COCO api
  ├─ COCOeval(gt, dt, iouType="segm")   # 或加速版 COCOeval_opt
  ├─ params: iouThrs=0.5:0.05:0.95, recThrs=0:0.01:1, maxDets=100,
  │         areaRng=[all, <32², 32²~96², >96²]
  ├─ evaluate(): 逐图逐类计算预测×GT 的 mask IoU 矩阵（RLE 按位运算）
  ├─ accumulate(): 逐 (类, IoU阈值, 召回点, 面积桶, maxDets) 累积 TP/FP 计数
  └─ summarize(): 汇总为 12 项统计
        ↓
_derive_coco_results(coco_eval, "segm", class_names)
  ├─ 总表：AP / AP50 / AP75 / APs / APm / APl / AR@1 / AR@10 / AR@100 / …
  └─ 类别表：per-category AP（thing_classes 提供行名）
```

#### 4.4.3 源码精读

- [instance_evaluation.py:L90-L99](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/evaluation/instance_evaluation.py#L90-L99)：调用点全景——传入 GT api、反映射后的 `coco_results`、任务名、`kpt_oks_sigmas`（keypoint 专用，本场景为 None）、`use_fast_impl`、`img_ids`（子集评估用）与 `max_dets_per_image`。
- [instance_evaluation.py:L100-L101](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/evaluation/instance_evaluation.py#L100-L101)：`if len(coco_results) > 0 else None`——空预测短路，`_derive_coco_results(None, ...)` 会给出 -1 占位的空表而不是崩溃。
- [instance_evaluation.py:L104-L107](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/evaluation/instance_evaluation.py#L104-L107)：结果挂到 `self._results[task]`；`class_names` 来自元数据 `thing_classes`（ADE20K 全景注册里它是 100 个 thing 名，见 [register_ade20k_panoptic.py:L334-L339](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/datasets/register_ade20k_panoptic.py#L334-L339)），per-category 表的行名即由此而来。
- 任务循环 `for task in sorted(tasks)`（L88）意味着日志按字母序先 `bbox` 后 `segm` 出表；结合 4.2 的结论，**读 ADE20K/Mapillary 实例指标时直接跳到 `[segm]` 表**，`[bbox]` 表只是占位框的牺牲品。

#### 4.4.4 代码实践（可本地运行的 pycocotools 玩具实验）

1. **实践目标**：在 100 行以内亲手复现 COCO 协议，制造一条「IoU 偏低的预测」，观察 AP50 与 AP75 如何分化——从此读任何 AP 表都有画面感。
2. **操作步骤**：安装 `pycocotools` 与 `numpy` 后运行（示例代码，不依赖本仓库）：

   ```python
   # 示例代码：3 个 GT、3 条预测的迷你 COCO 评估
   import json, copy
   import numpy as np
   from pycocotools import mask as mask_util
   from pycocotools.coco import COCO
   from pycocotools.cocoeval import COCOeval

   H = W = 100
   def rle(m): return mask_util.encode(np.asfortranarray(m.astype(np.uint8)))

   gt_masks = [np.zeros((H, W)) for _ in range(3)]
   gt_masks[0][10:40, 10:40] = 1   # 大物体
   gt_masks[1][50:90, 20:60] = 1   # 大物体
   gt_masks[2][20:30, 70:95] = 1   # 小物体（面积 250 < 32²=1024 → APs 桶）

   gt = {"images": [{"id": 1, "width": W, "height": H}],
         "annotations": [
             {"id": i + 1, "image_id": 1, "category_id": 1, "iscrowd": 0,
              "segmentation": rle(m), "area": int(m.sum())}
             for i, m in enumerate(gt_masks)],
         "categories": [{"id": 1, "name": "toy"}]}

   SHIFT = 25   # 第 2 步把这里改成 5 再跑一遍
   preds = []
   for i, m in enumerate(gt_masks):
       pm = np.roll(m, SHIFT, axis=1) if i == 2 else m.copy()
       preds.append({"image_id": 1, "category_id": 1,
                     "segmentation": rle(pm), "score": round(0.9 - 0.2 * i, 2)})

   coco_gt = COCO()
   coco_gt.dataset = gt; coco_gt.createIndex()
   coco_dt = coco_gt.loadRes(copy.deepcopy(preds))
   E = COCOeval(coco_gt, coco_dt, iouType="segm")
   E.evaluate(); E.accumulate(); E.summarize()
   ```

3. **需要观察的现象**：
   - `SHIFT=25` 时第三条预测与原位置完全错开（循环移位把 25 列宽的小物体挪出了原区间），IoU = 0；前两条 IoU = 1.0。观察 `summarize()` 的 AP / AP50 / AP75 三行是否**彼此几乎相同**（≈ 0.89，见第 4 点的推算）——IoU=0 在**所有**阈值下都是漏检，多阈值平均无从分化；
   - 把 `SHIFT` 改为 5（列交叠 20/25，IoU ≈ 0.67）：AP50 应回到 1.0（0.67 ≥ 0.5 命中），AP75 回落到 ≈ 0.89（0.67 < 0.75 漏检），总 AP 介于两者之间——**单一低阈值指标会掩盖 mask 边缘质量**，这正是 COCO 用多阈值平均的原因；
   - 同时观察 APs 行（小物体桶）：本例只有第 3 个 GT 属于 small，`SHIFT=25` 时 APs 应为 0，`SHIFT=5` 时约为 0.4（10 个阈值中只有 0.5~0.65 共 4 个命中）——一个实例的成败单独决定一个面积桶。
4. **预期结果**：能口头回答「AP50 不变（或升满）而 AP 下降时，模型的问题出在哪儿」——答：mask 边界不够贴合（中等 IoU 段塌陷），而非实例选择错误。理论推算：召回上限 2/3 时，101 点插值 + PR 包络给出的单阈值 AP ≈ (67×1.0 + 34×(2/3)) / 101 ≈ 0.89（不是朴素的 2/3——超出召回上限的部分由包络末端的 precision 填充），据此可得两组 SHIFT 下的全部预期值；具体打印值待本地验证（不同 pycocotools 版本的 `summarize` 格式一致，小数位数可能不同）。

#### 4.4.5 小练习与答案

**练习 1**：某模型 AP50 = 70、AP = 50，最可能的短板是定位精度还是分类/检索？
**答案**：定位（mask 质量）。AP50 高说明「有没有找到这个实例」基本对；AP（0.5~0.95 平均）低说明在高阈值下 IoU 不达标——mask 边缘不准。若是分类/检索问题（漏检、错类、排序差），AP50 会先掉。

**练习 2**：为什么 ADE20K 实例评估里 APs 往往远低于 APl？
**答案**：小物体（面积 < 32²）的 mask 像素基数小，同样的边界误差对 IoU 的侵蚀比例更大（分母小）；且下采样到 1/4 分辨率的 mask_features（u3-l5）对小物体的表达更粗。这是范式共性，不是 InstanceSegEvaluator 的行为。

**练习 3**：`max_dets_per_image` 与 `TEST.DETECTIONS_PER_IMAGE` 是什么关系？
**答案**：后者（u6-l2，默认 100）决定模型每图产出多少条实例进 json；前者（L98，默认 100）是 COCO api 评估时每图最多计入多少条。两者相等时无感；若把模型侧调大到 200，第 101~200 条会被评估端截掉——对照阅读可加深「生产者-消费者两端契约」的印象。

## 5. 综合实践

把四个模块串成一次真实评估的「读表」任务（对应本讲规格的实践要求）：

1. **任务**：取得一张 ADE20K 上的实例评估输出并完整解读。
   - **路线 A（有数据与 GPU，推荐）**：按 [MODEL_ZOO.md:L463-L481](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/MODEL_ZOO.md#L463-L481) 下载 ADE20K 全景 R50 权重，执行

     ```bash
     python train_net.py --num-gpus 1 \
       --config-file configs/ade20k/panoptic-segmentation/maskformer2_R50_bs16_160k.yaml \
       --eval-only MODEL.WEIGHTS <model_final_5c90d4.pkl 的路径>
     ```

     在输出目录的日志中定位 `InstanceSegEvaluator` 相关段落（`Preparing results for COCO format` / `Saving results to .../coco_instances_results.json`），抄下 `[bbox]` 与 `[segm]` 两张表及 per-category 表。
   - **路线 B（无数据）**：用 4.4.4 的玩具脚本代替，并打开 4.3.4 落盘逻辑对应的 `coco_instances_results.json`（若做过 --eval-only 则为真实文件；否则用玩具预测手写一个），检查其中 `category_id` 的取值空间。
2. **需要回答的四个问题**：
   - `[bbox]` 表与 `[segm]` 表的 AP 差距有多大？用 4.2 的「全零占位框」解释这个差距的来源；
   - `segm` 表中 AP / AP50 / AP75 / APs / APm / APl 分别是多少？哪个面积桶最差？对应第 2 节的哪个定义？
   - per-category 表里挑出 AP 最高与最低的两个类，对照 [register_ade20k_panoptic.py:L334-L339](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/datasets/register_ade20k_panoptic.py#L334-L339) 的 `thing_classes` 找出类名，猜测原因（出现频率？物体尺度？）；
   - 与原生 `COCOEvaluator` 的输入要求逐项对比：字段集合是否不同？`pred_boxes` 的语义差异？类别 id 校验差异（引用 4.3 的两处断言）？
3. **预期产出**：一份两栏笔记——左栏是抄录的指标表，右栏是逐行注解；以及一张「InstanceSegEvaluator vs 原生 COCOEvaluator」的三行对比表（字段要求 / bbox 可用性 / id 校验）。路线 A 的具体数值与 Model Zoo 全景表 AP=26.5 的接近程度，待本地验证（数据子集或单卡会带来偏差）。

## 6. 本讲小结

- `InstanceSegEvaluator` 是本仓库唯一的自研评估器：继承 `COCOEvaluator`，**只覆写 `_eval_predictions`**，生命周期（reset/process/evaluate）、Instances→COCO json 转换、GT 加载全部复用父类。
- 它存在的硬理由是**类别 id 反映射的校验**：ADE20K / Mapillary 全景注册用「全集枚举」构造 `thing_dataset_id_to_contiguous_id`，值域不从 0 开始、不密集（ADE20K 为 {7,…,149} 共 100 个），原生实现的两道密集区间断言会直接失败；覆写版换成宽松的成员检查（`assert category_id in reverse_id_mapping`），并把原生断言留在注释里作「修改现场」。
- 「没有真实框」是范式传导：`instance_inference` 的 `pred_boxes` 是全零占位（maskformer_model.py L372），json 的 `bbox` 字段照写但无意义——读指标只看 `[segm]` 表，Model Zoo 实例表的单列 AP 即 `segm` 任务的 AP@[.5:.95]。
- 输入契约四字段：`pred_masks`（硬二值化后 RLE 进 `segmentation`）、`pred_boxes`（占位进 `bbox`）、`scores`（重打分，决定 AP 排序）、`pred_classes`（模型连续 id，评估前反映射回数据集原始 id）。
- COCO 协议三要点：同类一对一贪心匹配（mask IoU ≥ t）、101 点插值 PR 曲线、IoU 阈值 0.5:0.05:0.95 平均 + 面积分桶——AP50/AP75/APs 的分化各有诊断意义（4.4.4 实验已亲手验证机制）。

## 7. 下一步学习建议

- 下一讲 **u7-l3** 走出 `train_net.py`，学习仓库自带的两个离线指标工具：把语义分割输出转成 panoptic 格式算 PQ 的脚本与 COCO boundary AP 脚本——它们回答「内置评估器没覆盖的指标怎么补」，与本讲的「继承改造」是两种互补的扩展姿势。
- 想继续深挖 COCO 协议，推荐通读 pycocotools 的 `cocoeval.py`（`evaluate/accumulate/summarize` 三段对应本讲 4.4.2 的流程图），再用 4.4.4 的玩具环境打断点观察 `evalImgs` 的结构。
- 想回看「生产者」一侧，重读 [maskformer_model.py:L344-L380](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L344-L380)（u6-l2）与本讲 4.2 的字段对照表逐行配对，体会「模型范式 → 输出契约 → 评估器定制」的完整传导链。

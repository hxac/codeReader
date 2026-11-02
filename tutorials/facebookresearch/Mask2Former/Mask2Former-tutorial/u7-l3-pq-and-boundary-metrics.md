# PQ 评估与 Boundary AP 工具

## 1. 本讲目标

前两讲我们看的是「内置评估器」：u7-l1 讲了 `build_evaluator` 如何按 `evaluator_type` 与三个模型开关组合出评估器，u7-l2 精读了仓库唯一的自研评估器 `InstanceSegEvaluator`。但内置组合有覆盖不到的指标：

- 语义分割分支的内置评估器（detectron2 的 `SemSegEvaluator`）只输出 mIoU 与逐类 IoU/acc，**不会算 PQ**；而 Mask2Former 论文在 ADE20K 语义分割上同时报告 mIoU 与 PQ。
- 内置 `COCOEvaluator` 算的是常规 mask AP，**不衡量 mask 边界质量**；而 MODEL_ZOO 的 COCO 实例分割表里专门有一列 Boundary AP。

本讲走出 `train_net.py`，学习 `tools/` 下补齐这两个缺口 的离线工具脚本：

1. `tools/evaluate_pq_for_semantic_segmentation.py`——把语义分割预测（per-class RLE json）重建成类别图，**当全景格式算 PQ**，并与 mIoU 并排输出。
2. `tools/evaluate_coco_boundary_ap.py`——一个只有 50 行的薄封装，调用外 部 boundary-iou-api 计算 **Boundary AP**。

读完本讲，你应该能够：

- 独立读懂并运行这两个脚本，说清它们的输入契约与输出含义。
- 手推一张玩具图上的 PQ（TP/FP/FN、SQ、RQ）与 mIoU，并解释两者口径差异。
- 准确指出「mask 的边界处理（膨胀/腐蚀）发生在哪里」——这是一个很容易望文生义搞错的点。

## 2. 前置知识

### 2.1 PQ：全景分割质量指标

PQ（Panoptic Quality）定义在「id 图 + segments_info」的全景格式上（u6-l3 见过这个输出契约）：每个分割段（segment）是一个带类别的连通或不连通区域。把预测段与 GT 段做**同类间一一匹配**，IoU 大于 0.5 判为 TP（true positive），随后：

\[
\mathrm{PQ}
= \frac{\sum_{(p,g)\in TP} \mathrm{IoU}(p,g)}{|TP|}
= \underbrace{\frac{\sum \mathrm{IoU}}{|TP|}}_{\text{SQ（分割质量）}}
\times
\underbrace{\frac{|TP|}{|TP|+\frac{1}{2}|FP|+\frac{1}{2}|FN|}}_{\text{RQ（识别质量，即 F1）}}
\]

- **SQ**：匹配上的段平均 IoU 有多高（边界准不准）。
- **RQ**：匹配对得齐不齐，就是检测里的 F1 分数。
- 未匹配的预测段是 FP，未匹配的 GT 段是 FN。

与 mIoU 的本质区别：**mIoU 按像素面积加权平均，PQ 按「段」计数**。漏掉一个小类别只让 mIoU 掉一点点（面积小），却让 PQ 损失一个完整的 FN。

### 2.2 语义分割怎么算 PQ？——「每类一段」技巧

PQ 本来需要全景格式（每个实例一个 id），而语义分割输出的是 per-class mask：同类所有像素属于一个整体，没有实例区分。本仓库脚本的解法是：**直接令段 id = 类别 id，把每个类别整体当作一个段**（tools/README.md 称之为 PQ-stuff）。这样：

- GT 里出现过的每个类别 = 1 个 GT 段；
- 预测里出现过的每个类别 = 1 个预测段；
- 某类预测与该类 GT 的 IoU > 0.5 → 该类记一次 TP。

### 2.3 mIoU 与混淆矩阵的 bincount 技巧

语义分割的标准 mIoU 来自 (K+1)×(K+1) 混淆矩阵（多出的一行/列放 ignore 像素）。逐类：

\[
\mathrm{IoU}_c = \frac{tp_c}{pos\_gt_c + pos\_pred_c - tp_c},
\qquad
\mathrm{mIoU} = \frac{\sum_{c\in\text{GT 出现的类}} \mathrm{IoU}_c}{|\{c:\ pos\_gt_c + pos\_pred_c > 0\}|}
\]

逐像素填 (K+1)² 的矩阵太慢，惯用技巧是把 (pred, gt) 坐标对编码成一维索引 `idx = (K+1) * pred + gt`，用 `np.bincount` 一次统计再 reshape——u5-l2 的损失、u7-l2 的评估里反复出现过同类思路。

### 2.4 RLE 与预测 json 格式

回顾 u7-l2：COCO 系工具用 pycocotools 的 RLE（游程编码）存二值 mask，`maskUtils.decode(rle)` 还原成 H×W 的 0/1 数组。detectron2 的 `SemSegEvaluator` 在评估语义分割时会把每个 per-class 预测 mask 落盘成一条记录，字段为 `file_name` / `category_id` / `segmentation`（RLE），存放于 `OUTPUT_DIR/inference/sem_seg_predictions.json`（见 tools/README.md 的用法行）。这正是 PQ 脚本的输入。

### 2.5 Boundary AP：mask AP 看不见轮廓误差

Boundary IoU 论文（Cheng et al., CVPR 2021）指出：常规 mask IoU/AP 被区域**内部**的大面积正确像素主导，mask 边界（轮廓）上的系统性误差在指标上几乎不可见。解法是把每个 mask 替换成它的**形态学边界带**（以 `dilation_ratio` 控制带宽，膨胀与腐蚀之差），再在边界带上算 IoU/AP。Mask2Former 的 MODEL_ZOO 在 COCO 实例分割表中并列报告两项，例如 R50 模型 AP 43.7 / Boundary AP 30.6。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [tools/evaluate_pq_for_semantic_segmentation.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/evaluate_pq_for_semantic_segmentation.py) | 本讲主角一：语义分割预测算 PQ（PQ-stuff）+ mIoU 并排输出 |
| [tools/evaluate_coco_boundary_ap.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/evaluate_coco_boundary_ap.py) | 本讲主角二：Boundary AP 薄封装（50 行） |
| [tools/README.md](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/README.md) | 两个工具的用法与外部库安装说明 |
| [datasets/prepare_ade20k_sem_seg.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/datasets/prepare_ade20k_sem_seg.py) | 生成 PQ 脚本要读的 `annotations_detectron2` GT 目录（呼应 u2-l2） |
| [MODEL_ZOO.md](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/MODEL_ZOO.md) | 实例分割表中并列 AP 与 Boundary AP，是这两个工具存在的动机 |
| [INSTALL.md](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/INSTALL.md) | `panopticapi` 的安装（PQ 脚本顶层 import 依赖它） |

## 4. 核心概念与源码讲解

### 4.1 evaluate_pq_for_semantic_segmentation 主流程

#### 4.1.1 概念说明

这是一个**离线后处理脚本**：不碰模型、不碰训练循环，输入是「某次 `--eval-only` 语义分割评估落盘的预测 json」+「数据集 GT」，输出是 PQ/SQ/RQ 表和 mIoU。它解决的问题是：u7-l1 的评估器组合里，语义分支只有 `SemSegEvaluator`（mIoU），没有 PQ；而 `COCOPanopticEvaluator` 的 PQ 只吃全景格式输出（u6-l3 的 id 图 + segments_info）。本脚本在内部**把 per-class RLE 重建回类别图，再现场拼装全景格式**，从而复用官方 PQ 统计器 `PQStat`（来自 `panopticapi`，需按 INSTALL.md 安装）。

#### 4.1.2 核心流程

```text
python tools/evaluate_pq_for_semantic_segmentation.py \
        --dataset-name ade20k_sem_seg_val \
        --json-file OUTPUT_DIR/inference/sem_seg_predictions.json
│
├── 1. 读预测 json → 按 file_name 去扩展名得到 image_id，分组成 imgToAnns
├── 2. 从 MetadataCatalog 取 stuff_classes / ignore_label，构造 categories 表（全部 isthing=0）
├── 3. 逐图循环：
│     ├── 3a. 读 GT 类别图（annotations_detectron2/{split}/{image_id}.png 或 .tif）
│     ├── 3b. 零图初始化 segm_dt，逐条 ann：类别 id 反映射 → RLE decode → 涂入类别
│     ├── 3c. mIoU 侧：GT 的 ignore 像素挪到第 K 类 → bincount 累加混淆矩阵
│     └── 3d. PQ 侧：pq_compute_single_image（4.2 精读）→ 累加 PQStat
├── 4. PQStat.pq_average → 打印 PQ / SQ / RQ / N 表（All 与 Stuff 两行）
└── 5. 由混淆矩阵算逐类 IoU → 打印 mIoU
```

#### 4.1.3 源码精读

**（1）参数与输入契约**。[tools/evaluate_pq_for_semantic_segmentation.py:L21-L37](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/evaluate_pq_for_semantic_segmentation.py#L21-L37)：`--dataset-name` 只有三个取值（`ade20k_sem_seg_val`、`coco_2017_test_stuff_10k_sem_seg`、`ade20k_full_sem_seg_val`），文件内注释明确**暂不支持 Cityscapes**（其预测格式不同，需先转换）；`--json-file` 指向上节说的 `sem_seg_predictions.json`。

**（2）数据根目录**。[tools/evaluate_pq_for_semantic_segmentation.py:L143-L143](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/evaluate_pq_for_semantic_segmentation.py#L143)：`_root = os.getenv("DETECTRON2_DATASETS", "datasets")`——与 u2-l2 数据集注册用的是同一个环境变量，GT 目录由此拼接。

**（3）预测分组**。[tools/evaluate_pq_for_semantic_segmentation.py:L146-L154](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/evaluate_pq_for_semantic_segmentation.py#L146-L154)：`json.load` 后把每条预测按 `basename(file_name).split(".")[0]` 提取 image_id（如 `ADE_val_00000001.jpg → ADE_val_00000001`），只保留 `category_id` 与 `segmentation` 两个字段挂到 `imgToAnns`。注意：**预测条目本身是 per-class 粒度**，一张图通常有几十条（每个预测出的类别一条 RLE）。

**（4）类别表**。[tools/evaluate_pq_for_semantic_segmentation.py:L158-L166](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/evaluate_pq_for_semantic_segmentation.py#L158-L166)：从 `MetadataCatalog` 取 `stuff_classes`（如 ADE20K 150 类）与 `ignore_label`（ADE20K 为 255），并构造 `categories = {i: {"id": i, "name": ..., "isthing": 0}}`——**全部标记为 stuff**，这决定了后面 PQ 表里 All 与 Stuff 两行完全相同。

**（5）GT 路径三分支**。[tools/evaluate_pq_for_semantic_segmentation.py:L170-L181](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/evaluate_pq_for_semantic_segmentation.py#L170-L181)：三个数据集分别读 `ADEChallengeData2016/annotations_detectron2/validation/*.png`、`coco/coco_stuff_10k/annotations_detectron2/test/*.png`、`ADE20K_2021_17_01/annotations_detectron2/validation/*.tif`（full 版用 tif，可存 8 万+ 类 id）。这些 `annotations_detectron2` 目录由 [datasets/prepare_ade20k_sem_seg.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/datasets/prepare_ade20k_sem_seg.py) 等准备脚本生成（u2-l2 讲过），特点是像素值已经是**连续类别 id**而非原始调色板。

**（6）重建预测类别图**。[tools/evaluate_pq_for_semantic_segmentation.py:L184-L194](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/evaluate_pq_for_semantic_segmentation.py#L184-L194)：三个行为细节值得注意：

- `segm_dt = np.zeros_like(segm_gt)`：零初始化，**未被任何预测 RLE 覆盖的像素落在类别 0**（ADE20K 里 0 号是 wall）——语义预测「默认墙」；
- `stuff_dataset_id_to_contiguous_id` 反映射：预测 json 里的 `category_id` 是数据集原始 id（u7-l2 讲过 SemSegEvaluator 落盘前的反映射），这里换回连续 id；
- `segm_dt[mask > 0] = category_id`：多个类别 RLE **互相重叠时后者直接覆盖前者**，胜负由 json 里的条目顺序决定，没有任何优先级控制。

**（7）双路统计与汇总输出**。[tools/evaluate_pq_for_semantic_segmentation.py:L197-L207](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/evaluate_pq_for_semantic_segmentation.py#L197-L207)：mIoU 侧累加混淆矩阵（4.3 精读），PQ 侧逐图调用 `pq_compute_single_image` 并 `pq_stat += ...` 累加（`PQStat` 重载了 `+=`）。汇总打印在 [L209-L225](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/evaluate_pq_for_semantic_segmentation.py#L209-L225)：`metrics = [("All", None), ("Stuff", False)]` 两行 PQ/SQ/RQ/N 表（如前述，两行数值恒相同），mIoU 在 [L240-L241](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/evaluate_pq_for_semantic_segmentation.py#L240-L241) 打印。

#### 4.1.4 代码实践

**实践目标**：亲手构造一个 mini 版 `sem_seg_predictions.json`，并复现 L146-L154 的分组与 L184-L194 的重建逻辑，验证 4.1.3 第（6）条的两个行为（未覆盖像素回落类别 0、重叠区后写覆盖先写）。

**操作步骤**（示例代码，非项目原有文件；只需 `numpy` 与 `pycocotools`）：

```python
import json
from collections import defaultdict
import numpy as np
from pycocotools import mask as maskUtils

H = W = 32
def to_rle(mask):
    rle = maskUtils.encode(np.asfortranarray(mask.astype(np.uint8)))
    rle["counts"] = rle["counts"].decode("utf-8")   # counts 是 bytes，须解码才能进 json
    return rle

# 类别 0 覆盖第 0~14 列；类别 1 覆盖第 14~31 列（第 14 列故意重叠）
m0 = np.zeros((H, W)); m0[:, :15] = 1
m1 = np.zeros((H, W)); m1[:, 14:] = 1
preds = [
    {"file_name": "toy/A00000001.jpg", "category_id": 0, "segmentation": to_rle(m0)},
    {"file_name": "toy/A00000001.jpg", "category_id": 1, "segmentation": to_rle(m1)},
]
json.dump(preds, open("toy_predictions.json", "w"))

# ---- 复现 tools/evaluate_pq_for_semantic_segmentation.py L149-L154 与 L184-L194 ----
imgToAnns = defaultdict(list)
for pred in preds:
    image_id = pred["file_name"].split("/")[-1].split(".")[0]
    imgToAnns[image_id].append(
        {"category_id": pred["category_id"], "segmentation": pred["segmentation"]}
    )
# 再故意挖一个洞：把类别 1 的预测改为只覆盖第 14~28 列（第 29~31 列不覆盖）
m1b = np.zeros((H, W)); m1b[:, 14:29] = 1
imgToAnns["A00000001"][1]["segmentation"] = to_rle(m1b)

segm_dt = np.zeros((H, W), dtype=np.int64)
for ann in imgToAnns["A00000001"]:
    mask = maskUtils.decode(ann["segmentation"])
    segm_dt[mask > 0] = ann["category_id"]        # 无 stuff 映射表时原样使用
labels, cnts = np.unique(segm_dt, return_counts=True)
print(dict(zip(labels.tolist(), cnts.tolist())))
```

**需要观察的现象**：

1. 第 14 列重叠区最终值为 1（json 中类别 1 排在后面，覆盖了类别 0）；
2. 若调换两条预测的顺序，重叠列会变成 0；
3. 把预测换成「挖洞」版本后，第 29~31 列未被覆盖，`np.unique` 里类别 0 的计数上升——这些像素被当成了「预测为类别 0」。

**预期结果**（按列数 × 32 行手推，可直接核对）：

- 完整覆盖版：`{0: 448, 1: 576}`——类别 0 覆盖第 0~13 列（14 列），第 14 列被后处理的类别 1 夺走，类别 1 共 18 列；
- 调换 `preds` 里两条记录的顺序后变为 `{0: 480, 1: 544}`——重叠的第 14 列改判类别 0；
- 挖洞版：`{0: 544, 1: 480}`——类别 1 只剩第 14~28 列（15 列），第 29~31 列共 96 像素未被覆盖、回落为类别 0（14×32 + 96 = 544）。

若环境无 `pycocotools`，可仅阅读源码完成三个观察点的推理，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么这个脚本不直接复用 detectron2 的 `COCOPanopticEvaluator`？
**答案**：`COCOPanopticEvaluator` 的输入是模型前向直接产出的全景格式（`panoptic_seg` id 图 + `segments_info`，见 u6-l3），依赖 `PANOPTIC_ON` 分支；而本脚本面对的是**语义分割评估落盘的 per-class RLE json**（`SEMANTIC_ON` 分支的产物），必须先在脚本内重建类别图、再现场拼装「每类一段」的全景表示，两者的输入格式与任务分支都不同。

**练习 2**：`--dataset-name` 为什么不支持 Cityscapes？
**答案**：源码注释（L28-L29）写明 Cityscapes 的预测格式与 detectron2 预测格式不同，需要先转换；同时本脚本的 GT 读取分支只硬编码了三个数据集的目录（L170-L181），新数据集要加 elif 分支并准备 `annotations_detectron2` 格式的 GT。

---

### 4.2 pq_compute_single_image：把语义图当「每类一段」的全景图

#### 4.2.1 概念说明

这个函数是脚本的核心算法（约 95 行），改写自官方 panopticapi 的 `pq_compute`（文件头注释 L40 标明出处）。它接收 GT 类别图与预测类别图，返回一张图的 `PQStat`（每类的 TP/FN/FP 计数与 IoU 累计）。核心 trick 是 2.2 节的「每类一段」：**GT 与预测都不再需要外部 segments_info，函数内部用 `np.unique` 现场统计**——类别图中出现过的每个非 ignore 类别天然就是一个段，段 id = 类别 id。

#### 4.2.2 核心流程

```text
输入：segm_gt（H×W 类别图，含 255 ignore）、segm_dt（H×W 类别图）、categories、ignore_label
│
├── 1. 构造 segments_info
│     GT：np.unique(segm_gt) 逐类别 {"id":c, "category_id":c, "area":像素数, "iscrowd":0}，跳过 VOID
│     预测：np.unique(segm_dt) 逐类别 {"id":c, "category_id":c}
├── 2. 一趟 np.unique 统计所有 (gt, pred) 组合对的交集像素数
│     pan_gt_pred = gt.astype(uint64) * 2^24 + pred   → gt_pred_map[(g,p)] = 交集
├── 3. 匹配：遍历组合对，类别相同且 IoU > 0.5 → TP（计数 + IoU 累加）
│     注意 union 要减去预测段与 VOID 的交集
├── 4. 未匹配的 GT 段 → FN（每类一段，即每类 +1）
└── 5. 未匹配的预测段 → 与 VOID/crowd 交集超过自身面积一半则忽略，否则 FP
```

关键数学是 OFFSET 编码：`OFFSET = 256³ = 2²⁴`，类别 id 远小于 2²⁴，因此 \( \text{combined} = gt \times 2^{24} + pred \) 与 (gt, pred) 二元组一一对应，`np.unique` 一次扫描即得全部组合对及其像素数——等价于稀疏混淆矩阵，比双重循环或稠密 (K+1)² 矩阵更快更省内存（u5-l1 的 `einsum("nc,mc->nm")`、u7-l2 的成对矩阵是同一思想的别的写法）。

匹配判据沿用全景 PQ 的 IoU > 0.5（此时一一匹配与最优匈牙利匹配等价，u5-l1 讨论过为什么 0.5 阈值下不会出现多对多竞争）。有效并集为：

\[
|p \cup g|_{\text{eff}} = |p| + |g| - |p \cap g| - |p \cap \mathrm{VOID}|
\]

即预测段落在 ignore 区域的部分不计入并集（不惩罚预测器「看见」了标注为 ignore 的区域）。

#### 4.2.3 源码精读

**（1）VOID 与 OFFSET**。[tools/evaluate_pq_for_semantic_segmentation.py:L41-L47](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/evaluate_pq_for_semantic_segmentation.py#L41-L47)：`VOID = ignore_label`（ADE20K 为 255），`OFFSET = 256*256*256`；`pan_gt = segm_gt; pan_pred = segm_dt`——语义图直接冒充全景图，这就是「每类一段」的全部实现。

**（2）segments_info 现场统计**。[tools/evaluate_pq_for_semantic_segmentation.py:L49-L63](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/evaluate_pq_for_semantic_segmentation.py#L49-L63)：GT 段带 `area`（`np.unique(..., return_counts=True)` 顺手得到）且 `iscrowd` 恒 0；预测段暂无 area，稍后补。

**（3）一致性检查（防御性死代码）**。[tools/evaluate_pq_for_semantic_segmentation.py:L65-L78](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/evaluate_pq_for_semantic_segmentation.py#L65-L78)：检查「PNG 里出现但 JSON 里没有的段 id」并抛 `KeyError`。注意两处阅读陷阱：其一，报错信息引用了本函数作用域里**并不存在的变量 `image_id`**（L72/L76/L78），真触发会先 `NameError`——这是从官方 `pq_compute` 拷贝时的残留；其二，本函数的 segments_info 本来就从同一张图统计而来，这些检查**永远不会触发**，属于官方实现的防御逻辑被原样保留。

**（4）组合对统计**。[tools/evaluate_pq_for_semantic_segmentation.py:L81-L87](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/evaluate_pq_for_semantic_segmentation.py#L81-L87)：`pan_gt_pred = pan_gt.astype(np.uint64) * OFFSET + pan_pred.astype(np.uint64)`，unique 后用 `label // OFFSET`、`label % OFFSET` 还原 (gt_id, pred_id)，值即交集面积——一趟扫描得到稀疏混淆表 `gt_pred_map`。

**（5）TP 匹配循环**。[tools/evaluate_pq_for_semantic_segmentation.py:L90-L109](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/evaluate_pq_for_semantic_segmentation.py#L90-L109)：逐组合对过滤——gt 是 VOID 跳过、预测段不在表内跳过、iscrowd 跳过（本场景恒不触发）、**类别不同跳过**；随后 [L103-L105](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/evaluate_pq_for_semantic_segmentation.py#L103-L105) 按 4.2.2 的公式算 union 与 IoU，`iou > 0.5` 则给该类别的 `pq_stat` 记 `tp += 1`、`iou += iou`，并把两侧 id 加入 matched 集合。

**（6）FN 与 FP**。[tools/evaluate_pq_for_semantic_segmentation.py:L111-L120](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/evaluate_pq_for_semantic_segmentation.py#L111-L120)：未匹配 GT 段按类别记 `fn += 1`（注意 L111 的注释写的是 "count false positives"，实际统计的是 FN——官方源码原样拷贝的笔误，阅读时别被带偏）。[L122-L134](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/evaluate_pq_for_semantic_segmentation.py#L122-L134)：未匹配预测段先算它与 VOID（及同类 crowd 段）的交集，若超过自身面积一半则**忽略不计**（大面积落在 ignore 区的预测不算错），否则 `fp += 1`。

**（7）汇总**。[tools/evaluate_pq_for_semantic_segmentation.py:L206-L225](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/evaluate_pq_for_semantic_segmentation.py#L206-L225)：逐图 `pq_stat += pq_compute_single_image(...)`，最后 `pq_stat.pq_average(categories, isthing=...)` 按类平均出 PQ/SQ/RQ/N（`PQStat` 与 `pq_average` 来自 [panopticapi.evaluation，按 INSTALL.md:L39](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/INSTALL.md#L39) 安装）。

#### 4.2.4 代码实践

**实践目标**：在一张 64×64 玩具图上同时算 PQ 与 mIoU，验证「段计数 vs 面积加权」的口径差异。这正是本讲规格里的核心实践。

**操作步骤**（示例代码，非项目原有文件；前置条件：u1-l2 的环境，`panopticapi` 已安装）：

```python
"""玩具图上的 PQ + mIoU 对比（非项目原有文件）"""
import importlib.util
import numpy as np

# tools/ 不是包，用文件路径加载方式导入
spec = importlib.util.spec_from_file_location(
    "pq_tool", "tools/evaluate_pq_for_semantic_segmentation.py")
pq_tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pq_tool)          # 顶层 import 需要 detectron2 / panopticapi

# ---- 1. GT：类别 1 占右半、类别 2 是左下 16x16 小块、其余为类别 0，左上 8x8 ignore ----
H = W = 64
gt = np.zeros((H, W), dtype=np.int64)
gt[:, W // 2:] = 1        # 右半 32 列：类别 1（2048 px）
gt[48:, :16] = 2          # 左下 16x16：类别 2（256 px，刻意做小）
gt[:8, :8] = 255          # 左上 8x8：ignore（64 px）；其余为类别 0（1728 px）

# ---- 2. 预测：类别 1 边界右移 4 列且被幻觉类别 3 咬掉一列；
#          类别 2 整类漏报（其区域因零初始化回落为类别 0）----
dt = np.zeros((H, W), dtype=np.int64)
dt[:, W // 2 + 4:] = 1    # 第 36~63 列
dt[:, 50] = 3             # 幻觉类别 3：整条第 50 列

# ---- 3. PQ（每类一段）----
cats = {i: {"id": i, "name": f"c{i}", "isthing": 0} for i in range(4)}
pq = pq_tool.pq_compute_single_image(gt, dt, cats, ignore_label=255)
res, per_class = pq.pq_average(cats, isthing=None)
print("PQ stats per class:", dict(per_class))
print({k: round(v, 4) for k, v in res.items()})       # pq / sq / rq / n

# ---- 4. mIoU（照抄 main 的 L197-L203 与 L227-L238）----
K = 4
cm = np.zeros((K + 1, K + 1), dtype=np.int64)
g = gt.copy(); g[g == 255] = K
cm += np.bincount((K + 1) * dt.reshape(-1) + g.reshape(-1),
                  minlength=cm.size).reshape(cm.shape)
tp = cm.diagonal()[:-1].astype(np.float64)
pos_gt = cm[:-1, :-1].sum(0); pos_pred = cm[:-1, :-1].sum(1)
iou = np.full(K, np.nan); union = pos_gt + pos_pred - tp
acc_valid = pos_gt > 0
iou[acc_valid] = tp[acc_valid] / union[acc_valid]
iou_valid = (pos_gt + pos_pred) > 0
print("per-class IoU:", iou[acc_valid])
print("mIoU:", np.sum(iou[acc_valid]) / np.sum(iou_valid))
```

**需要观察的现象**：

1. PQ 侧：类别 1 只错位 4 列（IoU ≈ 0.84），类别 0 吞下了漏报的类别 2 区域但仍 IoU ≈ 0.87，两者都 > 0.5，各记一次 TP；类别 2 整类漏报 → FN；类别 3 幻觉（与 VOID 无交集，必计 FP）→ FP。
2. mIoU 侧：类别 0/1 的 IoU 同样只有轻微下降，但**类别 2 的 IoU 恰为 0** 且计入分子，类别 3 又因 `iou_valid` 计入分母，mIoU 被双重压低。
3. 把 `dt[:, 50] = 3` 这一行删掉再跑一次，观察 FP 与 mIoU 分母同时减一的变化。

**预期结果**（手推，具体数值待本地验证）：TP=2、FN=1、FP=1，故 \( RQ = 2/(2+0.5+0.5) \approx 0.667 \)，\( SQ \approx (0.87+0.84)/2 \approx 0.86 \)，\( PQ \approx 0.57 \)；mIoU ≈ (0.87 + 0.84 + 0) / 4 ≈ 0.43。对比结论：PQ 只被「段找没找对」惩罚（RQ 项），mIoU 则被小类别漏报（IoU=0 进分子）与幻觉类别（进分母）双重稀释——同一个错误在两个口径下的「价格」完全不同。

#### 4.2.5 小练习与答案

**练习 1**：若把 `iou > 0.5` 改成 `iou > 0.3`，会不会出现一个 GT 段同时匹配多个预测段？
**答案**：会改变语义。0.5 阈值下，同类内每个 GT 段与预测段的交集互斥且并集恒定，两个预测段不可能都与 GT 的 IoU > 0.5（否则两者交集超过各自面积的一半，矛盾）；降到 0.3 后该保证不再成立，可能出现一个 GT 段记两次 TP——这正是官方 PQ 选 0.5 的原因（匹配退化为「唯一可能的一一匹配」，无需匈牙利算法）。

**练习 2**：预测段与 VOID 的交集为什么从 union 里减掉，而 GT 段与 VOID 的关系不用处理？
**答案**：GT 里根本没有 VOID 段参与匹配（构造 segments_info 时显式跳过 VOID，见 [L49-L56](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/evaluate_pq_for_semantic_segmentation.py#L49-L56)）；而预测段可能覆盖到标注为 ignore 的像素（如 ADE20K 的未标注区域），这些像素没有真值，计入并集会不公平地压低 IoU，因此在 [L103](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/evaluate_pq_for_semantic_segmentation.py#L103) 用 `gt_pred_map.get((VOID, pred_label), 0)` 从 union 中扣除。

**练习 3**：一个类别在 GT 中出现了两个不连通区域（比如两扇窗），会被记几个 GT 段？
**答案**：一个。segments_info 由 `np.unique(segm_gt)` 生成，只看类别值不看连通性——同一类别的所有像素（无论几块）合成一个段。这就是「每类一段」的语义版 PQ（PQ-stuff）与真全景 PQ（每个实例一段）的本质区别。

---

### 4.3 mIoU 对照计算：同一份数据上的两种口径

#### 4.3.1 概念说明

脚本在同一循环里顺手算了 mIoU（L197-L203 累加、L227-L238 汇总），把两个指标并排打印。这不是重复劳动，而是刻意对照：**mIoU 是像素级面积加权的区域指标，PQ 是段级计数指标**。对 mask classification 范式的语义分割模型（u6-l1：Q 个 per-mask 预测软融合成 per-class 概率图），论文习惯同时报告两项，以同时覆盖「区域覆盖好不好」与「类别集合找得全不全」两个维度。

#### 4.3.2 核心流程

```text
逐图：
  gt 副本中 ignore(255) 改写为 K（第 K 类）
  idx = (K+1) * pred + gt → np.bincount → reshape 成 (K+1)×(K+1) 累加到 conf_matrix
汇总：
  tp      = 对角线前 K 个
  pos_gt  = conf[:-1,:-1].sum(axis=0)   # 每个真值类的像素数（不含 ignore 列）
  pos_pred= conf[:-1,:-1].sum(axis=1)   # 每个预测类的像素数（落在 ignore 上的预测不计入）
  IoU_c   = tp_c / (pos_gt_c + pos_pred_c − tp_c)     （仅对 pos_gt_c > 0 的类赋值）
  mIoU    = Σ IoU_c(有 GT 的类) / |{c: pos_gt_c + pos_pred_c > 0}|
```

#### 4.3.3 源码精读

**（1）混淆矩阵累加**。[tools/evaluate_pq_for_semantic_segmentation.py:L197-L203](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/evaluate_pq_for_semantic_segmentation.py#L197-L203)：`gt[gt == ignore_label] = num_classes` 把 ignore 挪到矩阵最后一列；`(num_classes + 1) * pred.reshape(-1) + gt.reshape(-1)` 把二维坐标压成一维索引，`np.bincount(..., minlength=conf_matrix.size).reshape(...)` 一次统计整图——行是预测、列是真值。落在 ignore 上的预测像素被收进最后一列，随后被 `[:-1, :-1]` 切片排除，既不算对也不算错。

**（2）mIoU 公式与两个掩码的细微差别**。[tools/evaluate_pq_for_semantic_segmentation.py:L227-L238](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/evaluate_pq_for_semantic_segmentation.py#L227-L238)：`acc_valid = pos_gt > 0`（真值出现过的类）用来筛选**分子求和**的类；`iou_valid = (pos_gt + pos_pred) > 0`（真值或预测任一出现过的类）用作**分母计数**。两者不一致意味着：**幻觉类别（有预测无真值）会稀释 mIoU**——它计入分母却没有可加的 IoU。这是本脚本的实现口径，与某些「只在有 GT 的类上平均」的实现不同，做跨论文对比时要留意。

#### 4.3.4 代码实践

**实践目标**：用 4.2.4 的玩具脚本验证「面积加权 vs 段计数」的差异方向。

**操作步骤**：在 4.2.4 的脚本里做一组对照实验——把类别 2 的预测逐步缩小：

1. `dt[48:, :16] = 2`（完整覆盖，IoU = 1）；`dt[48:, :11] = 2`（覆盖 11 列，IoU = 176/256 = 0.6875）；`dt[48:, :6] = 2`（覆盖 6 列，IoU = 96/256 = 0.375）；最后删掉这行（整类漏报）。
2. 每一步同时打印 PQ 侧的 TP/FN/FP 与 mIoU 侧的逐类 IoU。

**需要观察的现象**：覆盖比例从 100% → 69% → 37% → 0% 的过程中：mIoU 里类别 2 的 IoU 从 1 **连续**降到 0.69、0.38、0；PQ 侧前两步完全不变（IoU 仍 > 0.5，一直是 TP），第三步**阶跃**为「FN + FP」双输（漏掉的 10 列回落为类别 0，同时类别 2 的预测成为无匹配段），最后一步只剩 FN。

**预期结果**：PQ 对「类内覆盖比例」在 0.5 IoU 阈值两侧突变，mIoU 呈连续反应；而对「整类漏报/幻觉」，PQ 每次固定 ±1 个 FN/FP，mIoU 的变化幅度则取决于该类面积占比。具体数值待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `pos_pred` 要用 `conf[:-1, :-1]` 而不是整行求和？
**答案**：整行求和会把「预测为类 c 但落在 ignore 区域」的像素计入 `pos_pred`，导致并集偏大、IoU 被系统性压低。切片 `[:-1, :-1]` 把 ignore 行列整个剔除，等价于「评估时假装 ignore 像素不存在」。

**练习 2**：如果一个模型从不预测某个 GT 中存在的稀有类，mIoU 和 PQ 分别怎么罚它？
**答案**：mIoU：该类 IoU 为 0 计入分子、计入分母，损失 = 1/|有效类数|，与该类面积无关；PQ：该类记一个 FN，RQ 分母加 0.5。两者对「类别缺失」的惩罚都是常数级，但 mIoU 同时对**大面积类的边界误差**格外敏感（像素加权），PQ 则对**多个中小类别的系统性缺失**更敏感（每个都是独立 FN）。

---

### 4.4 evaluate_coco_boundary_ap 主流程：薄封装与外部库

#### 4.4.1 概念说明

[tools/evaluate_coco_boundary_ap.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/evaluate_coco_boundary_ap.py) 只有 50 行，改写自 boundary-iou-api 的示例脚本（文件头 L3 标明）。它计算 COCO 实例分割的 **Boundary AP**：把 GT 与预测的 mask 都替换成边界带后再跑标准 COCO PR 流程（u7-l2 讲过 AP 的 101 点插值），使指标聚焦 mask 轮廓质量。

**最重要的认知纠偏**：本仓库这个脚本本身**没有一行膨胀/腐蚀代码**。对 mask 的边界处理全部发生在外部依赖 [boundary-iou-api](https://github.com/bowenc0221/boundary-iou-api) 内部（按 [tools/README.md:L54-L58](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/README.md#L54-L58) 的说明 `pip install git+...` 安装）。脚本自己的贡献只有三件事：解析参数、以 `get_boundary=True` 构造 COCO、剔除预测里的 bbox。

#### 4.4.2 核心流程

```text
python tools/evaluate_coco_boundary_ap.py \
        --gt-json-file COCO_GT_JSON --dt-json-file COCO_DT_JSON \
        [--iou-type boundary] [--dilation-ratio 0.020]
│
├── 1. iou_type == "boundary" → get_boundary = True（否则 False，退回普通 mask 评估）
├── 2. COCO(gt_json, get_boundary=True, dilation_ratio)   ← 边界替换发生在外部库的加载阶段
│      （外部库行为：对每个 GT ann 的 mask 求「膨胀 − 腐蚀」边界带，替换 segmentation；
│        具体实现以安装的 boundary_iou 源码为准——本仓库不含该代码）
├── 3. 读预测 json，逐条 c.pop("bbox", None)               ← 剔除占位框（呼应 u6-l2）
├── 4. cocoDt = cocoGt.loadRes(res)；COCOeval(cocoGt, cocoDt, iouType="boundary", dilation_ratio)
└── 5. evaluate() → accumulate() → summarize()             ← 标准 COCO 三段式（u7-l2）
```

按 Boundary IoU 论文的定义（外部概念，本讲只转述），mask \( M \) 的边界带为：

\[
B(M, r) = D(M, r) \setminus E(M, r)
\]

其中 \(D\)、\(E\) 是以半径受 `dilation_ratio` 控制的结构元做的形态学膨胀/腐蚀。`dilation_ratio` 越小，边界带越窄，指标对轮廓误差越敏感、对远离边界的内部错误越无感。精确实现（结构元形状、半径如何随目标尺寸缩放）以安装版本源码为准，待本地确认。

#### 4.4.3 源码精读

**（1）外部依赖 import**。[tools/evaluate_coco_boundary_ap.py:L14-L15](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/evaluate_coco_boundary_ap.py#L14-L15)：`from boundary_iou.coco_instance_api.coco import COCO` 与 `...cocoeval import COCOeval`——注意这不是 pycocotools 的 COCO，而是 boundary-iou-api 里同名类的**魔改版**（构造函数多了 `get_boundary`、`dilation_ratio` 参数）。未安装该包时脚本直接 ImportError，这是它与 PQ 脚本（依赖 panopticapi）在环境上的差别。

**（2）参数**。[tools/evaluate_coco_boundary_ap.py:L20-L24](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/evaluate_coco_boundary_ap.py#L20-L24)：`--gt-json-file` 是 COCO 标注 json（如 `instances_val2017.json`），`--dt-json-file` 是预测结果 json（COCO 格式结果，含 `segmentation`/`score`/`category_id`），`--iou-type` 默认 `boundary`，`--dilation-ratio` 默认 `0.020`。

**（3）get_boundary 开关与 COCO 构造**。[tools/evaluate_coco_boundary_ap.py:L30-L34](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/evaluate_coco_boundary_ap.py#L30-L34)：`iou_type` 非 boundary 时 `get_boundary=False`，库退回普通 mask IoU 路径——同一个脚本可对照跑出普通 mask AP 与 Boundary AP。GT 的边界替换就发生在这一次构造里（外部库内部）。

**（4）剔除 bbox——本仓库唯一的关键适配**。[tools/evaluate_coco_boundary_ap.py:L36-L39](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/evaluate_coco_boundary_ap.py#L36-L39)：`for c in resFile: c.pop("bbox", None)`。呼应 u6-l2：mask classification 范式的实例输出没有真实框，`pred_boxes` 是全零占位。u7-l2 的做法是「忽略 bbox 表、只读 segm 表」；这里更直接——把 bbox 字段从每条预测里删掉，防止外部库按 bbox 初始化或校验时出问题。

**（5）标准三段式**。[tools/evaluate_coco_boundary_ap.py:L41-L45](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/evaluate_coco_boundary_ap.py#L41-L45)：`loadRes` 加载预测、`COCOeval(..., iouType=args.iou_type, dilation_ratio=...)` 构造评估器，`evaluate / accumulate / summarize` 输出 AP 表——与 u7-l2 精读的 COCOeval 完全同构，只是 IoU 的计算对象从整个 mask 换成了边界带。

#### 4.4.4 代码实践

**实践目标**：完成规格中的注释任务——找出「对预测 mask 做边界处理的具体步骤」，并诚实地标注哪些步骤不在本仓库里。

**操作步骤**：

1. **注释本仓库脚本**：对 50 行的 [tools/evaluate_coco_boundary_ap.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/evaluate_coco_boundary_ap.py) 逐段写注释，标注每一步做什么、发生在哪一层（本仓库 / 外部库）。你的注释里应明确写出：「膨胀/腐蚀不在本文件中，由 `boundary_iou` 在 COCO 构造与 IoU 计算时完成」。
2. **安装外部库并查看边界处理源码**（需网络，待本地验证）：

   ```bash
   pip install git+https://github.com/bowenc0221/boundary-iou-api.git
   python -c "import boundary_iou.utils as u, inspect; print(inspect.getsource(u.mask_to_boundary))"
   ```

   对打印出的 `mask_to_boundary` 源码逐步注释：它如何用 `dilation_ratio` 计算结构元尺寸、先膨胀后腐蚀（或相减）得到边界带、再编码回 RLE。
3. **无网络替代方案**（示例代码，非项目原有文件）：用 scipy 自己实现边界带，直观感受 `dilation_ratio` 的作用：

   ```python
   import numpy as np
   from scipy import ndimage
   mask = np.zeros((64, 64)); mask[16:48, 16:48] = 1     # 一个 32x32 的方块 mask
   for ratio in (0.05, 0.02, 0.005):
       r = max(1, int(ratio * mask.shape[0]))             # 简化：半径 ∝ 图像边长
       band = ndimage.binary_dilation(mask, iterations=r) ^ \
              ndimage.binary_erosion(mask, iterations=r)
       print(ratio, "band pixels:", int(band.sum()))
   ```

**需要观察的现象**：

1. 本仓库脚本中搜索不到 `dilate`/`erode`/`cv2` 等任何形态学调用——边界处理 100% 在外部库。
2. 步骤 3 中 `ratio` 越小，边界带像素数越少；当带宽小于典型轮廓误差时，边界 IoU 才会「放过」该误差。
3. 把 `--iou-type` 换成非 boundary 值时 `get_boundary=False`（[L30-L33](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/evaluate_coco_boundary_ap.py#L30-L33)），GT 不再替换为边界带，输出退化为普通 mask AP。

**预期结果**：三步注释合起来构成对「Boundary AP 从哪来」的完整回答；步骤 2 打印的外部源码细节（结构元形状、半径缩放方式）待本地验证后补进注释。

#### 4.4.5 小练习与答案

**练习 1**：为什么这个脚本要 `pop("bbox")`，而 u7-l2 的 `InstanceSegEvaluator` 不用？
**答案**：u7-l2 走的是 detectron2 体系内的 COCOEvaluator，评估时显式选择 `segm`（mask）指标表，bbox 结果即使无意义也只是并存不干扰；本脚本调用的是外部魔改 COCO 类，其 `loadRes`/`createIndex` 对结果条目的处理不被本仓库控制，占位 bbox（全零）留着有引发错误初始化或校验的风险，干脆物理删除，这也是「跨体系传数据」时的通用防御。

**练习 2**：MODEL_ZOO 里 R50 实例分割 AP 43.7、Boundary AP 30.6，两者差 13 个点说明了什么？
**答案**：Boundary AP 的绝对值天然低于普通 AP（匹配对象从整 mask 换成窄边界带，IoU 分布整体下移），不能横向比较绝对值；真正有信息量的是**不同模型间两项指标的变化是否同步**——若模型 A 的 AP 高但 Boundary AP 增益小，说明它的优势来自区域内部（如更好的类别识别）而非更准的轮廓。这也是 MODEL_ZOO 并列两列的用意。

---

## 5. 综合实践

把本讲两个工具接回真实评估流程，完成一次「内置指标 + 论文指标」的完整对照（需要已按 u2-l2 准备好数据集、按 u2-l1 跑通过评估；全流程待本地验证）：

1. **语义分割侧**：

   ```bash
   python train_net.py --num-gpus 1 \
     --config-file configs/ade20k/semantic-segmentation/maskformer2_R50_bs16_160k.yaml \
     --eval-only MODEL.WEIGHTS <MODEL_ZOO 权重> MODEL.DEVICE cpu OUTPUT_DIR out/ade_sem
   python tools/evaluate_pq_for_semantic_segmentation.py \
     --dataset-name ade20k_sem_seg_val \
     --json-file out/ade_sem/inference/sem_seg_predictions.json
   ```

   记录输出的 PQ/SQ/RQ/N 与 mIoU，并与 `SemSegEvaluator` 在同次评估日志里打印的 mIoU 对照——两者应当一致（同一混淆矩阵口径），这可以反过来验证你对脚本的理解。
2. **实例分割侧**：用 COCO 实例配置跑 `--eval-only`，取评估器落盘的预测 json（文件名以输出目录与日志为准），安装 boundary-iou-api 后运行：

   ```bash
   python tools/evaluate_coco_boundary_ap.py \
     --gt-json-file $DETECTRON2_DATASETS/coco/annotations/instances_val2017.json \
     --dt-json-file <预测 json>
   ```

   把得到的 AP 与 Boundary AP 和 [MODEL_ZOO.md:L131-L147](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/MODEL_ZOO.md#L131-L147) 中 R50 行（43.7 / 30.6）对照，验证复现是否到位（允许小数点后细微差异）。
3. **分析**：写一段结论，回答——同一模型在 mIoU、PQ、AP、Boundary AP 四个指标上各自强在哪、弱在哪；如果你要改进 mask 边界质量，哪个指标最能反映进步？

## 6. 本讲小结

- 内置评估器组合（u7-l1）覆盖不了「语义分割的 PQ」与「mask 边界质量」，`tools/` 下两个离线脚本分别补这两个缺口；它们不碰模型，只消费 `--eval-only` 落盘的预测 json。
- PQ 脚本的核心 trick 是「每类一段」：令段 id = 类别 id，语义类别图直接冒充全景图；`gt * 2²⁴ + pred` 的一趟 `np.unique` 等价于稀疏混淆矩阵，IoU > 0.5 匹配后按段统计 TP/FN/FP，PQ = SQ × RQ。
- 同一循环顺手算的 mIoU 与 PQ 口径互补：mIoU 按像素面积加权（大类主导、误差按比例稀释，幻觉类稀释分母），PQ 按段计数（整类漏报 = 完整 FN，0.5 IoU 阈值两侧阶跃）。
- Boundary AP 脚本是 50 行薄封装：本仓库代码只做 `get_boundary` 开关、`dilation_ratio` 透传、剔除占位 bbox 三件事；对 mask 的膨胀/腐蚀边界处理**全部在外部 boundary-iou-api 中**，读代码时不要在本仓库里找它。
- 两个脚本分别依赖 `panopticapi` 与 `boundary-iou-api`，都从 GitHub 源码安装（INSTALL.md 与 tools/README.md 给出命令），这是本仓库「评估库按需补装」策略的延续（u1-l2）。

## 7. 下一步学习建议

评估体系单元到此完结。下一步有两条路：

1. **横向对照**：读 detectron2 源码中的 `COCOPanopticEvaluator` 与 panopticapi 的 `pq_compute`，对照本讲 4.2 的单图版实现，看「真全景 PQ（每实例一段）」与「PQ-stuff（每类一段）」在代码上的全部差异点；有余力可读 Boundary IoU 论文（Cheng et al., CVPR 2021）理解边界带宽度参数的选取。
2. **纵向前进**：进入单元八「扩展点与二次开发」，从 u8-l1 替换 backbone 开始动手——前三单元拆解的 backbone、pixel decoder、transformer decoder 三大组件将以 registry 注册的方式被逐个替换，本单元积累的「评估怎么验证改进」的经验正好用来检验你的改动是否真的有效。

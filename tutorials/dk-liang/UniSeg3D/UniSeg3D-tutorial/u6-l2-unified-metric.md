# 六任务统一评估：PromptSupportedUnifiedSegMetric

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `process()` 如何把一次推理产生的五份结果（常规预测 + 点提示预测 + 点提示置信度 + 文本提示预测 + 评估标注）收集成统一的五元组。
2. 理解 `compute_metrics()` 如何把同一批结果**拆成三份**，分别喂给 `seg_eval`（语义 mIoU）、`instance_seg_eval`（实例 mAP，ScanNet 协议）、`panoptic_seg_eval`（全景 PQ）。
3. 掌握 `map_inst_markup()` 的类别映射：如何把「全景评估 id 体系」的 GT 翻译成「实例任务 id 体系」。
4. 手工推导交互式/指代两个提示任务的自定义指标：对齐 IoU → `cal_ap_thr` 的 PR 曲线数值积分 → AP@25/50/AP，以及文本分支的 precision。
5. 拿到一份训练/评估日志时，能反查出每个指标键（`miou`/`all_ap`/`pq`/`Point Prompt AP`/`Text Prompt mIoU`）分别由哪段代码算出。

本讲是第 6 单元的第二讲，承接 [u6-l1 推理链路](u6-l1-inference-pipeline.md)：模型 `predict()` 写进 `data_sample` 的那些字段，本讲看评估器如何消费它们。

## 2. 前置知识

### 2.1 mmengine 的 Metric 生命周期

mmengine 中评估器由两类对象组成：

- **Metric（指标器）**：一个继承 `BaseMetric` 的类，只需实现两个方法——`process()` 在每个验证 batch 后被调用、把中间结果攒进 `self.results` 列表；`compute_metrics()` 在整个数据集跑完后被调用一次、对 `self.results` 做汇总，返回 `dict[str, float]`。
- **Evaluator（评估器）**：持有若干 Metric，由 Runner 的 val/test 循环驱动。Runner 还会把 `dataset.metainfo` 注入 `metric.dataset_meta`——本项目正是从这里读到 `seg_valid_class_ids`。

所以读评估代码的口诀是：**process 攒数据，compute 出指标，dataset_meta 供类别表**。

### 2.2 三大标准指标在算什么

| 指标 | 任务 | 通俗解释 |
| --- | --- | --- |
| mIoU | 语义分割 | 每类算「预测点集与 GT 点集的交并比」，再对类别取平均。\( \mathrm{IoU} = \frac{\lvert G \cap P\rvert}{\lvert G \cup P\rvert} \) |
| mAP | 实例分割 | 把每个预测实例按置信度排序，在不同 IoU 阈值下画 PR 曲线、曲线下面积即 AP，对类别取平均。ScanNet 协议惯用 AP@25 / AP@50 |
| PQ | 全景分割 | 先把 stuff 区域与 thing 实例都当作「全景段落」，按 IoU>0.5 做**一对一**匹配；\( \mathrm{PQ} = \frac{\sum_{匹配} \mathrm{IoU}}{\lvert TP\rvert + \frac{1}{2}\lvert FP\rvert + \frac{1}{2}\lvert FN\rvert} \)，可分解为 SQ（匹配段平均 IoU）× RQ（类似 F1） |

### 2.3 两套 GT id 体系（承自 u3-l3）

`AddSuperPointAnnotations` 在测试旁路 `eval_ann_info` 里写的是**全景评估 markup**：

- `pts_semantic_mask`：0..19 为 20 个类别索引（wall=0、floor=1、cabinet=2……），20 为 unlabeled；
- `pts_instance_mask`：stuff 点写 0/1，things 点写从 2 起的连续实例号，未标注点写 -1。

而 ScanNet 官方实例评估协议要求另一种编码：`1000 × 语义类id + 实例号`。本讲的 `map_inst_markup()` 与 `rename_gt()` 就是这两套体系之间的两座翻译桥。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [uniseg3d/unified_metric.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_metric.py) | 本讲主角：`PromptSupportedSegMetric`（收集）与 `PromptSupportedUnifiedSegMetric`（汇总计算），以及 `get_iou`/`cal_ap`/`cal_ap_thr`/`map_inst_markup` 四个工具函数 |
| [uniseg3d/instance_seg_eval.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_seg_eval.py) | 从 mmdet3d 拷来并改造的实例评估入口：`aggregate_predictions`/`rename_gt`/`instance_seg_eval` |
| [uniseg3d/evaluate_semantic_instance.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/evaluate_semantic_instance.py) | ScanNet 官方实例评估脚本的核心实现：贪心匹配、PR 曲线、AP 积分 |
| [uniseg3d/uniseg3d.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py) | `predict()` 把结果写进 `data_sample` 的那几行——评估器的输入源头 |
| [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py) | `val_evaluator` 的全部构造参数 |
| [uniseg3d/transforms_3d.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py) | `AddSuperPointAnnotations` 生成全景评估 markup 的旁路（u3-l3 已精读） |
| [uniseg3d/loading.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/loading.py) | `eval_ann_info` 里 `sp_pts_mask`/`lidar_idx` 的注入点 |

另外两个被调用但不属于本仓库的函数：`seg_eval` 与 `panoptic_seg_eval` 来自 pip 安装的 mmdet3d 包（`from mmdet3d.evaluation import ...`，见 [uniseg3d/unified_metric.py:L9](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_metric.py#L9)），本讲只从调用方式层面理解它们。

## 4. 核心概念与源码讲解

本讲的三个最小模块：

1. `process()` 结果收集与分支；
2. `compute_metrics()` 总装与 `map_inst_markup` 类别映射 + 三任务指标调用；
3. 提示任务 AP@25/50 与 precision 的自定义计算。

### 4.1 process()：从 data_sample 到五元组

#### 4.1.1 概念说明

模型 `predict()` 会在每个 `data_sample`（一个场景）上写 1～4 个字段：`pred_pts_seg`（常规三任务预测）、`point_prompt_pred_pts_seg` + `point_prob`（交互式）、`text_prompt_pred_pts_seg`（指代），而 GT 标注则藏在 `eval_ann_info` 里。`process()` 是这些字段与评估器之间的**唯一搬运工**：把 torch 张量搬到 CPU 转 numpy、把不同任务的预测组装成**定长五元组**追加进 `self.results`。

为什么是五元组而不是 dict？因为下游 `compute_metrics()` 用固定位置解包，缺哪路预测就用 `None` 占位，位置即语义。

#### 4.1.2 核心流程

```text
对 batch 中每个 data_sample：
  1. 若含点提示预测 → 逐键搬 CPU/numpy → cpu_point_prompt_pred_3d
                     → point_prob 也转 numpy（必须是张量）
  2. 若含文本提示预测 → 同样搬运 → cpu_text_prompt_pred_3d
  3. pred_pts_seg、eval_ann_info 逐键搬运 → cpu_pred_3d
  4. 按「有哪些提示」四选一，追加五元组：
     (eval_ann_info, pred_3d, point_prompt, point_prob, text_prompt)
     两提示齐 → 全填；只有点 → 文本位 None；只有文本 → 点两位 None；
     都没有 → 后三位全 None
```

#### 4.1.3 源码精读

类注册与继承：`PromptSupportedSegMetric` 继承 mmdet3d 的 `SegMetric`，自己只重写了 `process`（`compute_metrics` 被子类完全覆盖，父类那份只适配纯语义任务，本项目不直接用它）：

- [uniseg3d/unified_metric.py:L24-L31](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_metric.py#L24-L31) 注册到 `METRICS` 注册表，`__init__` 原样调用父类——这个类的全部价值都在 `process` 的搬运逻辑里。

点提示分支的搬运（有 `.to` 方法的当张量处理，没有的当 numpy 直接透传）：

- [uniseg3d/unified_metric.py:L41-L50](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_metric.py#L41-L50) 读 `point_prompt_pred_pts_seg` 与 `point_prob`。注意 `point_prob` 那行**没有** `hasattr` 判断——只要场景带点提示，`point_prob` 键必须存在，否则直接 `KeyError`。模型侧保证两者成对写入（见下）。

文本提示分支与常规预测的搬运同构：

- [uniseg3d/unified_metric.py:L52-L68](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_metric.py#L52-L68) `pred_pts_seg` 与 `eval_ann_info` 同样逐键搬 CPU。

五元组的四分支落盘：

- [uniseg3d/unified_metric.py:L69-L76](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_metric.py#L69-L76) 按「两提示齐 / 只点 / 只文本 / 无提示」四种情况追加元组，`None` 作哨兵。

搬运的源头——模型 `predict()` 末尾把四路结果写进 `data_sample`：

- [uniseg3d/uniseg3d.py:L877-L887](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L877-L887) `pred_pts_seg` 永远写；带点提示时同时写 `point_prompt_pred_pts_seg` 和 `point_prob`；带文本提示时写 `text_prompt_pred_pts_seg`——与 `process()` 的四分支一一对应。
- [uniseg3d/uniseg3d.py:L882-L884](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L882-L884) `point_prob` 的来历：对点提示查询的类别 logits 做 softmax、去掉背景列，再用 `gather` 挑出 **GT 类别那一列**的概率。它不是模型自报的置信度，而是「模型认为该提示物体是它真实类别的概率」——正是 AP 排序需要的置信度。

点/文本提示预测的内容由两个极简函数产生：

- [uniseg3d/uniseg3d.py:L20-L31](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L20-L31) `point_prompt_predict_by_feat`：预测掩码 = 提示查询的掩码 logits 经 `superpoints` 花式索引映射回点级后，按 `sp_score_thr`（0.4）二值化；GT = 提示实例的超点掩码同样映射回点级。两者形状同为 `(n_prompts, n_points)` 且**行对齐**——第 i 行 GT 对第 i 行预测。
- [uniseg3d/uniseg3d.py:L41-L52](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L41-L52) 文本版完全同构。

#### 4.1.4 代码实践

**实践目标**：不动模型，直接用手造的 `data_sample` 字典调用 `process()`，观察 `self.results` 里五元组的形状与哨兵行为。

操作步骤（示例代码，在仓库根目录、按 u1-l2 装好环境后运行；保存为独立脚本，不要放进源码目录）：

```python
# 示例代码
import numpy as np, torch
from uniseg3d.unified_metric import PromptSupportedSegMetric

m = PromptSupportedSegMetric()
n = 100
base_pred = dict(
    pts_semantic_mask=[np.zeros(n, np.int64), np.zeros(n, np.int64)],
    pts_instance_mask=[np.zeros((n, 0), bool), np.zeros(n, np.int64)],
    instance_labels=np.array([]), instance_scores=np.array([]))
ann = dict(pts_semantic_mask=np.zeros(n, np.int64),
           pts_instance_mask=np.zeros(n, np.int64))

ds_full = dict(ann | {'eval_ann_info': ann, 'pred_pts_seg': base_pred,
    'point_prompt_pred_pts_seg': dict(
        point_prompt_object_gt_masks=np.ones((2, n), bool),
        point_prompt_object_pred_masks=np.ones((2, n), bool)),
    'point_prob': torch.tensor([0.9, 0.8]),                       # 必须是张量
    'text_prompt_pred_pts_seg': dict(
        text_prompt_object_gt_masks=np.ones((1, n), bool),
        text_prompt_object_pred_masks=np.ones((1, n), bool))})
ds_none = dict(eval_ann_info=ann, pred_pts_seg=base_pred)

m.process({}, [ds_none]);        m.process({}, [ds_none])
print(len(m.results[0]), m.results[0][2:])   # 5 → (None, None, None)
m.results.clear()
m.process({}, [ds_full]);        m.process({}, [ds_none])
for r in m.results: print(type(r[2]), type(r[3]), type(r[4]))
```

需要观察的现象与预期结果：

1. `self.results[0]` 长度为 5，后三位为 `None`（无提示场景）。
2. 第二组实验中，第一个样本五元组全满（numpy.ndarray × 3），第二个样本后三位为 `None`——**同一个列表里两种元组混存**，`compute_metrics` 靠逐元素判 `is not None` 区分。
3. 把 `point_prob` 换成 numpy 数组会直接 `AttributeError`（`.to` 不存在）——体会「张量键必须真是张量」。（待本地验证）

#### 4.1.5 小练习与答案

**练习 1**：为什么五元组里缺哪路预测就用 `None`，而不是干脆少存几个字段？
**答案**：`compute_metrics()` 用 `for a, b, c, d, e in results:` 按位置解包（见 [L156](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_metric.py#L156)）。定长结构让「是否有某路预测」的信息用 `None` 哨兵表达，代码无需处理变长结构，也不会把「点提示结果」误读成「文本提示结果」。

**练习 2**：如果某场景的 `data_sample` 里有 `point_prompt_pred_pts_seg` 却没有 `point_prob`，会发生什么？
**答案**：[L50](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_metric.py#L50) 对 `point_prob` 无条件调用 `.to('cpu')`，缺失时抛 `KeyError`。模型侧 [uniseg3d.py:L879-L884](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L879-L884) 总是成对写入，因此正常运行不会触发。

**练习 3**：`point_prob` 为什么取「GT 类别那一列」的 softmax 概率，而不是最大类概率？
**答案**：交互式分割的「检索问题」是：给定一组带置信度的物体预测，按置信度从高到低验收 IoU 是否达标。用真实类别列概率作置信度，衡量的是模型对该提示物体的**类别确信度**——若模型把物体认成别的类，即使掩码尚可也应在排序中靠后。

### 4.2 compute_metrics() 总装：三任务数据准备与 map_inst_markup 类别映射

#### 4.2.1 概念说明

`compute_metrics()` 是一次「总装」：把 `self.results` 中每个场景的五元组，按三个标准任务 + 两个提示任务**分拣进不同容器**，再调用三份标准评估代码。核心难点是：GT 只有一份（全景 markup），而三个任务各要自己的格式——桥梁就是 `map_inst_markup()`。

配置侧先交代参数。评估器在配置中的全部构造参数：

- [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L268-L293](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L268-L293) 20 个类名 + unlabeled；`sem_mapping` 是 20 个 nyu40 有效类别 id；`inst_mapping = sem_mapping[2:]` 去掉 wall/floor 两个 stuff id；`stuff_class_inds=[0,1]`、`thing_class_inds=range(2,20)`、`min_num_points=1`（全景段落最小点数）、`id_offset=2**16`（全景预测中 thing 实例号的隔离位宽，供 mmdet3d 的 `panoptic_seg_eval` 使用）。

其中 `seg_valid_class_ids` 并不在构造参数里，而是运行时从 `dataset_meta` 注入（数据集 metainfo 里的 20 个 nyu id，取值与 `sem_mapping` 一致）。

#### 4.2.2 核心流程

```text
compute_metrics(results):
  读 dataset_meta['seg_valid_class_ids'] → 前 2 个是 stuff，后 18 个是 things 的 nyu id
  遍历 results 中每个场景：
    ① 点提示 → get_iou(gt, pred) 逐行算 IoU → 追加进 val_set_prompt_IoUs_point / point_prob
    ② 文本提示 → 同上 → val_set_prompt_IoUs_text
    ③ 全景容器：gt = eval_ann 原样；pred = 两路全景 markup（semantic[1] / instance[1]）
    ④ 语义容器：gt = eval_ann['pts_semantic_mask']；pred = pts_semantic_mask[0]
    ⑤ 实例容器：gt 经 map_inst_markup 翻译；pred = 布尔掩码 + 标签 + 分数
  调 panoptic_seg_eval → pq
  调 seg_eval → miou
  调 instance_seg_eval → all_ap / all_ap_50% / all_ap_25%
  按 logger_keys 过滤出 5 个键
  若（最后一个样本）带点提示 → 追加 Point Prompt mIoU / AP@50 / AP@25 / AP
  若（最后一个样本）带文本提示 → 追加 Text Prompt mIoU / Precision_half / quarter
```

`map_inst_markup` 的翻译规则（全景 markup → 实例任务 markup）：

\[
\begin{aligned}
\mathrm{inst}' &= \mathrm{clip}(\mathrm{inst} - 2,\ \min=-1) \\
\mathrm{sem}' &= \mathrm{mapping}\!\left[\mathrm{sem} - 2\right],\quad \text{且 } \mathrm{inst}'=-1 \text{ 处强制 } \mathrm{sem}'=-1
\end{aligned}
\]

其中 `mapping = 18 个 things 的 nyu id + [-1]`。效果：things 实例号变成从 0 起的连续 id，语义变成 nyu id；wall/floor/未标注点全部变 `(-1, -1)`。

#### 4.2.3 源码精读

初始化：全部参数原样存成员，`sem_mapping`/`inst_mapping` 转 numpy 数组（注意：转完在本文件里其实未被使用，真正起作用的是 `dataset_meta` 里的 `seg_valid_class_ids`）：

- [uniseg3d/unified_metric.py:L99-L119](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_metric.py#L99-L119) 构造参数与默认 `logger_keys=[('miou',), ('all_ap','all_ap_50%','all_ap_25%'), ('pq',)]`——这决定了最终日志里出现哪 5 个键。

类别表准备：

- [uniseg3d/unified_metric.py:L133-L139](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_metric.py#L133-L139) 从 `dataset_meta` 取有效类别 id，按 `stuff_class_inds`/`thing_class_inds` 把 21 个类名切成 stuff 2 个 + things 18 个。

九个累计容器的声明：

- [uniseg3d/unified_metric.py:L141-L155](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_metric.py#L141-L155) 三个任务各一对 gt/pred 容器 + 实例任务多出标签与分数两个列表 + 提示任务三个一维数组。

提示 IoU 的累计（详见 4.4）：

- [uniseg3d/unified_metric.py:L156-L175](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_metric.py#L156-L175) 逐场景逐提示任务地调 `get_iou` 追加；空提示场景追加空数组（相当于跳过）。

全景与语义容器的填充——注意 `[0]`/`[1]` 下标的含义：

- [uniseg3d/unified_metric.py:L176-L187](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_metric.py#L176-L187) 模型侧的 `predict_by_feat` 打包时故意把两套预测塞进同名的二元素列表（见 [uniseg3d/uniseg3d.py:L82-L84](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L82-L84)）：`pts_semantic_mask[0]` 是纯语义任务的 argmax（0..20 含 unlabeled），`[1]` 与 `pts_instance_mask[1]` 是全景 markup；`pts_instance_mask[0]` 才是实例任务的 `(n_points, n_insts)` 布尔掩码。评估器按下标各取所需。

实例任务 GT 的翻译与预测容器：

- [uniseg3d/unified_metric.py:L189-L202](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_metric.py#L189-L202) 调 `map_inst_markup`（传入 `valid_class_ids[num_stuff_cls:]`，即 18 个 things 的 nyu id）；预测侧三个键原样转 tensor。

三个标准评估的调用点：

- [uniseg3d/unified_metric.py:L205-L226](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_metric.py#L205-L226) 依次调 `panoptic_seg_eval`（PQ）、`seg_eval`（mIoU，`ignore_index[0]`=20 忽略 unlabeled）、`instance_seg_eval`（mAP，`class_labels=classes[num_stuff_cls:-1]` 即 18 个 things 类名，`[:-1]` 去掉 unlabeled）。

按 `logger_keys` 过滤输出键：

- [uniseg3d/unified_metric.py:L228-L231](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_metric.py#L228-L231) 三份返回字典只挑 5 个键，其余（逐类 IoU、prec/rec 等）留在终端表格里、不进日志字典。

`map_inst_markup` 本体：

- [uniseg3d/unified_metric.py:L277-L303](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_metric.py#L277-L303) 四步：实例号减 2 并把负值压成 -1（L295-L296）；语义号减 2（L297）；凡实例号为 -1 的点语义强制 -1（L298）——这一步保证后面 `rename_gt` 的「一个实例内语义唯一」断言成立；最后用 `nyu id 表 + [-1]` 做查表映射（L300-L301），numpy 的 `-1` 索引恰好取到表尾的 -1。

一个工程细节（读源码时容易踩）：提示指标的两个 `if` 判断用的是**循环结束后的残留变量**：

- [uniseg3d/unified_metric.py:L232](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_metric.py#L232) / [L240](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_metric.py#L240) `single_point_prompt_pred_results` / `single_text_prompt_pred_results` 在 for 循环结束后保存的是**最后一个场景**的取值。若最后一个场景恰好没有提示（该位为 `None`），即使前面场景全是提示，提示指标也会整组消失。验证集每个场景都带提示，因此正常评估不触发——但自定义数据集或子集评估时要小心。

#### 4.2.4 代码实践

**实践目标**：用 10 个点的小数组手动验证 `map_inst_markup` 的翻译规则，确认输出与手工推导一致。

操作步骤（示例代码）：

```python
# 示例代码
import numpy as np
from uniseg3d.unified_metric import PromptSupportedUnifiedSegMetric

metric = PromptSupportedUnifiedSegMetric(
    thing_class_inds=list(range(2, 20)), stuff_class_inds=[0, 1],
    min_num_points=1, id_offset=2**16,
    sem_mapping=[1,2,3,4,5,6,7,8,9,10,11,12,14,16,24,28,33,34,36,39],
    inst_mapping=[3,4,5,6,7,8,9,10,11,12,14,16,24,28,33,34,36,39],
    metric_meta=dict(label2cat={}, ignore_index=[20], classes=[]))

sem  = np.array([0,0,1,1,2,2,4,4,20,20])   # wall wall floor floor cab cab chair chair unlabeled×2
inst = np.array([0,0,1,1,2,2,3,3,-1,-1])   # 全景 markup：stuff 0/1、things 从 2 起
valid_thing_ids = metric.sem_mapping and np.array(
    [3,4,5,6,7,8,9,10,11,12,14,16,24,28,33,34,36,39])
new_sem, new_inst = metric.map_inst_markup(sem, inst, valid_thing_ids, 2)
print(new_sem); print(new_inst)
```

需要观察的现象与预期结果（可直接手工核对）：

- `new_inst = [-1,-1,-1,-1, 0,0, 1,1, -1,-1]`——两个 thing 实例号 2、3 减 2 变 0、1；wall/floor/unlabeled 全部 -1。
- `new_sem  = [-1,-1,-1,-1, 3,3, 5,5, -1,-1]`——cabinet（类索引 2）→ nyu id 3，chair（类索引 4）→ nyu id 5；非实例点语义一律 -1。
- 若把 `inst` 中某实例点改成 `1`（floor），该点语义会随 `pts_instance_mask == -1` 的判定结果变化——体会「语义跟随实例号」的联动。（待本地验证）

#### 4.2.5 小练习与答案

**练习 1**：`map_inst_markup` 里为什么需要 `pts_semantic_mask[pts_instance_mask == -1] = -1` 这一步强制覆盖？
**答案**：不覆盖的话，wall/floor 点的语义值减 2 后是 -2/-1，未标注点是 18，会以奇怪的下标进入查表甚至越界。更关键的是下游 `rename_gt`（[instance_seg_eval.py:L66](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_seg_eval.py#L66)）断言每个实例内部的语义唯一：把所有非 thing 点统一成 -1 后，按实例号分组时才不会混进多个语义值。

**练习 2**：`valid_class_ids` 为什么取 `[num_stuff_cls:]` 切片？`classes` 又为什么取 `[num_stuff_cls:-1]`？
**答案**：`seg_valid_class_ids` 的前两个是 wall/floor 的 nyu id（1、2），实例任务只评估 18 个 things，所以两端都要切；`[:-1]` 是去掉末尾的 unlabeled 类名。两个列表长度必须一致（[instance_seg_eval.py:L103](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_seg_eval.py#L103) 有断言）。

**练习 3**：语义任务与全景任务用的预测都叫 `pts_semantic_mask`，评估器如何区分？
**答案**：靠下标。模型打包时 `[0]` 存纯语义任务的点级 argmax（含 20=unlabeled），`[1]` 存全景 markup 的语义通道（things 点被实例语义覆盖、非 thing 点只在 stuff 两类中取值，见 [uniseg3d/uniseg3d.py:L193-L224](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L193-L224)）；评估器在 [L182](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_metric.py#L182)/[L187](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_metric.py#L187) 分别取 `[1]` 与 `[0]`。

### 4.3 三大标准指标调用链：seg_eval / instance_seg_eval / panoptic_seg_eval

#### 4.3.1 概念说明

- `seg_eval` 与 `panoptic_seg_eval` 是 mmdet3d 包内函数，本讲从调用契约层面理解：前者吃「gt/pred 点级语义标签 + label2cat + ignore_index」返回含 `miou` 的字典；后者吃「gt/pred 的 {pts_semantic_mask, pts_instance_mask} + things/stuff 类名 + min_num_points + id_offset」返回含 `pq`（以及 sq/rq）的字典。
- `instance_seg_eval` 是本仓库从 mmdet3d **拷贝改造**的版本（文件头两行注释写明改动），再往下接 `evaluate_semantic_instance.py`——后者改自 ScanNet 官方 BenchmarkScripts。这条链是本讲唯一需要逐层精读的标准指标。

为什么要拷贝改造？mmdet3d 原版假设预测实例掩码是「每个实例一个整数掩码文件」，而 UniSeg3D 的预测是 `(n_points, n_insts)` 的布尔矩阵。改造点有二（文件头注释原文）：

- [uniseg3d/instance_seg_eval.py:L1-L14](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_seg_eval.py#L1-L14) ① `info[file_name]['mask'] = mask[i]` 直接按行取第 i 个实例的布尔掩码；② 原来用 `mask.max()+1` 推实例数（布尔阵恒为 2），改成 `mask.shape[0]` 遍历所有实例。

#### 4.3.2 核心流程

ScanNet 实例评估协议四步（对全部场景一次性算）：

```text
instance_seg_eval:
  1) aggregate_predictions: 每场景的 (掩码矩阵, 标签, 分数)
     → {'场景号_实例号': {mask: bool(n,), label_id: nyu id, conf: 分数}}
  2) rename_gt: 每场景 GT 实例号 i → 1000×语义nyu id + i
  3) scannet_eval:
     a. assign_instances_for_scan: 逐场景、逐类别，统计每个预测与每个 GT 的交集
     b. evaluate_matches: 逐 IoU 阈值 × 逐类别，贪心匹配 → PR 曲线 → AP
     c. compute_averages: 对类别 nanmean → all_ap / all_ap_50% / all_ap_25%
  4) AsciiTable 打印逐类表格并返回 metrics 字典
```

关键编码：\(\text{gt\_id} = 1000 \times \text{nyu\_id} + \text{local\_inst\_id}\)。评估侧用 `gt_ids // 1000` 取类别、按整个 id 匹配实例——这也是 `rename_gt` 名字的由来（给每个 GT 实例「改名」）。

#### 4.3.3 源码精读

预测侧格式化：

- [uniseg3d/instance_seg_eval.py:L15-L41](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_seg_eval.py#L15-L41) `aggregate_predictions`：`file_name = f'{id}_{i}'`（场景号_实例号）作全局唯一键；`label_id` 用 `valid_class_ids[label[i]]` 把模型输出的 0..17 thing 索引翻译成 nyu id——所以预测标签与 GT 语义在同一 id 空间对齐。

GT 侧重命名：

- [uniseg3d/instance_seg_eval.py:L46-L71](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_seg_eval.py#L46-L71) `rename_gt`：逐实例断言语义唯一；语义不在 18 个有效 id 里的实例不改名（保持 -1 等，天然被后续 `1000` 进制解码排除）；符合条件的整体赋值 `1000*语义+实例号`。注意 `instance_mask == i` 循环里 `i=-1`（非实例点）语义恒为 -1（`map_inst_markup` 保证），不会触发断言。

入口与表格打印：

- [uniseg3d/instance_seg_eval.py:L73-L131](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_seg_eval.py#L73-L131) `instance_seg_eval`：建 `id_to_label`、串起前三步、用 `AsciiTable` 打印逐类 `AP_0.25 / AP_0.50 / AP / Prec_0.50 / Rec_0.50`——训练日志里那张实例评估大表就是它打的。

逐场景关联：

- [uniseg3d/evaluate_semantic_instance.py:L242-L315](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/evaluate_semantic_instance.py#L242-L315) `assign_instances_for_scan`：先取 void 掩码（GT 类别不在有效 id 内的点，[L271](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/evaluate_semantic_instance.py#L271) `np.in1d(gt_ids // 1000, valid_class_ids)`）；逐预测掩码：点数 < 100 直接丢弃（[L286-L287](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/evaluate_semantic_instance.py#L286-L287)），否则统计与同类每个 GT 的交集（[L299-L311](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/evaluate_semantic_instance.py#L299-L311)），双向挂在 `gt2pred`/`pred2gt` 上。

贪心匹配与 PR 曲线：

- [uniseg3d/evaluate_semantic_instance.py:L34-L40](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/evaluate_semantic_instance.py#L34-L40) 每个 (IoU 阈值, 类别) 组合开始前，把所有预测标记为「未占用」——`pred_visited` 是一对一匹配的执行者。
- [uniseg3d/evaluate_semantic_instance.py:L62-L96](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/evaluate_semantic_instance.py#L62-L96) 逐 GT 找重叠超阈值的预测：已被其它 GT 占用的跳过；首个命中记 TP 并占用该预测；同一 GT 的第二个命中预测自动降级为 FP（保留较小分数）。无任何命中的 GT 计入 `hard_false_negatives`。
- [uniseg3d/evaluate_semantic_instance.py:L99-L126](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/evaluate_semantic_instance.py#L99-L126) 未匹配预测默认 FP，但如果它与「void 区域 / 小 GT 实例」的重叠占比超过阈值则被豁免——避免把压在不可评估区域上的预测白白算成 FP。
- [uniseg3d/evaluate_semantic_instance.py:L132-L187](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/evaluate_semantic_instance.py#L132-L187) AP 数值积分：按分数升序排、`cumsum` 得 TP 数、只在**去重阈值**处取 PR 点；曲线首点补人工点 \((recall, precision)=(0,1)\)（[L169-L170](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/evaluate_semantic_instance.py#L169-L170)）；最后用 `np.convolve(recall, [-0.5, 0, 0.5])` 生成相邻 recall 的步宽，与 precision 点积即 AP（[L184-L187](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/evaluate_semantic_instance.py#L184-L187)）：\[ \mathrm{AP} = \sum_k p_k \cdot (r_k - r_{k-1}) \]
- [uniseg3d/evaluate_semantic_instance.py:L172-L176](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/evaluate_semantic_instance.py#L172-L176) 顺带用 F1 最大点给出 `Prec_0.50/Rec_0.50` 表格列。

类别平均与阈值集：

- [uniseg3d/evaluate_semantic_instance.py:L204-L239](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/evaluate_semantic_instance.py#L204-L239) `compute_averages`：`all_ap` 在**剔除 0.25 阈值**后的 9 个阈值上 nanmean（ScanNet 惯例），`all_ap_50%`/`all_ap_25%` 单独取；无 GT 的类别是 nan，不参与平均。
- [uniseg3d/evaluate_semantic_instance.py:L351-L368](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/evaluate_semantic_instance.py#L351-L368) `get_options`：`overlaps = [0.5, 0.55, …, 0.9] + [0.25]`，`min_region_sizes=[100]`——GT 与预测两侧的 100 点下限都源于此。

至此可以把日志指标与代码对上：`all_ap` 来自 9 阈值 nanmean，`all_ap_25%`/`all_ap_50%` 来自 0.25/0.5 单阈值，`pq` 来自 mmdet3d `panoptic_seg_eval`，`miou` 来自 mmdet3d `seg_eval`。

#### 4.3.4 代码实践

**实践目标**：源码阅读型实践——对一个只有 2 个实例的迷你场景，手工执行 `aggregate_predictions` 与 `rename_gt` 的编码规则，并核对 `get_options` 的阈值集合。

操作步骤（示例代码）：

```python
# 示例代码
import numpy as np, torch
from uniseg3d.instance_seg_eval import aggregate_predictions, rename_gt
from uniseg3d.evaluate_semantic_instance import get_options

# 场景 0：cabinet(nyu 3) 实例号 0、chair(nyu 5) 实例号 1（map_inst_markup 产物）
masks = torch.tensor([[1,1,0,0],[0,0,1,1]])          # (2 实例, 4 点) 布尔
labels = torch.tensor([0, 2])                        # thing 索引：0=cabinet, 2=chair
scores = torch.tensor([0.9, 0.8])
valid = (3,4,5,6,7,8,9,10,11,12,14,16,24,28,33,34,36,39)
print(aggregate_predictions([masks],[labels],[scores],valid))
# 期望: {'0_0': label_id=3, '0_1': label_id=5}，conf 与 mask 对应

sem  = np.array([3,3,5,5])                            # nyu id
inst = np.array([0,0,1,1])
print(rename_gt([sem.copy()],[inst.copy()],valid))    # 期望: [[3000,3000,5001,5001]]
print(get_options())
# 期望: overlaps=[0.5 0.55 ... 0.9 0.25], min_region_sizes=[100]
```

需要观察的现象与预期结果：

1. `aggregate_predictions` 输出的键是 `'0_0'`/`'0_1'`，`label_id` 已是 nyu id 3/5——预测与 GT 在同一 id 空间。
2. `rename_gt` 输出 `[3000, 3000, 5001, 5001]`——`1000×3+0`、`1000×5+1`，与上文公式一致。
3. `get_options()` 证实 AP 的 10 个 IoU 阈值与 100 点下限。（待本地验证）

#### 4.3.5 小练习与答案

**练习 1**：`min_region_sizes=[100]` 分别作用在 GT 和预测的哪一侧、哪一行代码？
**答案**：GT 侧在 `evaluate_matches` 的过滤列表（[L51-L56](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/evaluate_semantic_instance.py#L51-L56)，小于 100 点的 GT 不参与匹配）；预测侧在 `assign_instances_for_scan`（[L286-L287](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/evaluate_semantic_instance.py#L286-L287)，直接 `continue` 丢弃）。

**练习 2**：为什么 GT 实例要用 `1000×语义+实例号` 编码，而预测不用？
**答案**：GT 的掩码信息本来就分散在「语义掩码 + 实例掩码」两个数组里，编码成一个 int 便于复用 ScanNet 官方按单一 `gt_ids` 数组写的匹配逻辑（`//1000` 解码类别）。预测的掩码本来就是逐实例的布尔向量，只需把标签翻译成 nyu id 即可。

**练习 3**：日志里 `all_ap` 与 `all_ap_50%` 的关系是什么？
**答案**：`all_ap_50%` 是 IoU 阈值 0.5 单独一个阈值下的类别平均 AP；`all_ap` 是 0.5~0.9 共 9 个阈值（显式排除 0.25）AP 的类别平均（nanmean），更严格，数值上通常 `all_ap ≤ all_ap_50%`。

### 4.4 提示任务自定义指标：对齐 IoU、cal_ap/cal_ap_thr 与 precision

#### 4.4.1 概念说明

交互式（点提示）与指代（文本提示）分割没有现成的多场景评估协议——每个提示对应一个「指定物体」，预测与 GT 天然一一成对。于是项目自己实现了轻量指标：

- **对齐 IoU**：第 i 条提示的 IoU 只在第 i 行 GT 与第 i 行预测之间算，不做任何匹配。
- **点提示有置信度**（`point_prob`），可以做完整的 AP：把所有场景的所有提示汇成一个大列表，按置信度排序后在不同 IoU 阈值下积分 PR 曲线。
- **文本提示没有全局置信度**（`TextPromptTest` 把每个物体的每条描述全量展开，见 u3-l5），无法排序画 PR 曲线，于是只报 **IoU 均值**与 **precision@0.5/0.25**（IoU 超过阈值的预测占比）。

注意一个命名陷阱：日志键 `Point Prompt mIoU` / `Text Prompt mIoU` 里的 "mIoU" 其实是**全体提示 IoU 的算术平均**，与语义分割按类平均的 mIoU 不是一回事。

#### 4.4.2 核心流程

`get_iou` 的对齐计算：

\[
\mathrm{IoU}_i = \frac{\sum_j g_{ij} \wedge p_{ij}}{\sum_j g_{ij} \vee p_{ij} + \epsilon}
\]

`cal_ap_thr` 的 VOC 风格 AP（带包络）：

1. 判定 TP：\( t_i = \mathbb{1}[\mathrm{IoU}_i > \mathrm{thr}] \)（严格大于）；
2. 按置信度降序排：排序后第 k 个位置 \( p_k = \frac{\mathrm{TP}_k}{k} \)、\( r_k = \frac{\mathrm{TP}_k}{N_{pos}} \)；
3. 从右往左取包络 \( \tilde p_k = \max_{j \ge k} p_j \)（把 PR 曲线压成单调不增）；
4. 积分：\[ \mathrm{AP} = \sum_k (r_k - r_{k-1})\, \tilde p_k \]

`cal_ap` 再对 10 个阈值取平均：

\[
\mathrm{AP} = \frac{1}{10} \sum_{i=0}^{9} \mathrm{AP}_{\,0.5 + 0.05 i}
\]

#### 4.4.3 源码精读

- [uniseg3d/unified_metric.py:L15-L21](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_metric.py#L15-L21) `get_iou`：`&`/`|` 逐元素布尔运算后按行求和，交并比相除，分母加 1e-8 防零。行与行之间互不干扰——这正是「提示即指定候选」的评估哲学（对照 u5-l5：提示损失同样无需匹配器）。

- [uniseg3d/unified_metric.py:L248-L267](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_metric.py#L248-L267) `cal_ap_thr`：`np.argsort(-confidences)` 降序；`cumsum` 同时累计 TP 与 FP；`mrec`/`mpre` 两端补 0、1 哨兵点（注意 `mpre` 尾部补的是 0）；L263-L265 的逆序循环就是包络；L266 的点积即积分。与 4.3 的 ScanNet 版本相比，这里没有去重阈值与 convolve 技巧，但数学上同为「precision 包络 × recall 步长」。

- [uniseg3d/unified_metric.py:L269-L275](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_metric.py#L269-L275) `cal_ap`：阈值 0.50, 0.55, …, 0.95（`0.5 + 0.05*i`，i=0..9）逐一调 `cal_ap_thr` 后平均——与 COCO 风格 mAP 的阈值集合一致。

- [uniseg3d/unified_metric.py:L232-L239](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_metric.py#L232-L239) 点提示四指标：`Point Prompt mIoU`（IoU 均值）、`AP@50`、`AP@25`、`AP`（10 阈值平均）。IoU 与置信度数组是**跨场景拼接**后一起排序的——AP 是全验证集级指标。

- [uniseg3d/unified_metric.py:L240-L245](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_metric.py#L240-L245) 文本提示三指标：`Text Prompt mIoU`、`Text_Precision_half`（IoU>0.5 占比）、`Text_Precision_quarter`（IoU>0.25 占比）。分母是全部文本提示数——没有排序、没有积分。

#### 4.4.4 代码实践

**实践目标**：用 4 条手造提示验证 `cal_ap_thr`/`cal_ap` 与手工推导一致。

设 4 条提示的 (IoU, 置信度) 为：(1.0, 0.3)、(0.6, 0.9)、(0.5, 0.8)、(0.25, 0.95)。

手工推导（thr=0.5，注意 0.5 不满足「严格大于」）：

- TP 判定：1.0→T，0.6→T，0.5→F，0.25→F，共 2 个正样本；
- 按置信度降序：(0.95,F)、(0.9,T)、(0.8,F)、(0.3,T)；
- 累计 precision = (0, 1/2, 1/3, 1/2)，recall = (1/2, 1/2, 1/2, 1)；
- 包络后 precision = (1/2, 1/2, 1/2, 1/2)，AP = (1/2−0)×1/2 + (1−1/2)×1/2 = **0.5**。

操作步骤（示例代码）：

```python
# 示例代码
import numpy as np
from uniseg3d.unified_metric import PromptSupportedUnifiedSegMetric as M

ious = np.array([1.0, 0.6, 0.5, 0.25])
conf = np.array([0.3, 0.9, 0.8, 0.95])
m = M(thing_class_inds=[], stuff_class_inds=[], min_num_points=1,
      id_offset=2**16, sem_mapping=[], inst_mapping=[],
      metric_meta=dict(label2cat={}, ignore_index=[], classes=[]))
print(m.cal_ap_thr(ious, conf, 0.5))   # 期望 0.5   （上面手工推导）
print(m.cal_ap_thr(ious, conf, 0.25))  # 期望 0.75  （3 个 TP，包络 0.75）
print(m.cal_ap(ious, conf))            # 期望 0.5   （0.5→0.5；0.55..0.95 每个 0.5）
print(ious.mean())                     # Point Prompt mIoU 期望 0.5875
```

0.75 的推导（thr=0.25）：TP = 1.0、0.6、0.5（0.25 仍不严格大于 0.25），降序 (0.95,F)、(0.9,T)、(0.8,T)、(0.3,T)，precision = (0, 1/2, 2/3, 3/4)，recall = (1/3, 1/3, 2/3, 1)，包络全为 3/4，AP = 1×3/4 = 0.75。thr∈[0.55, 0.95] 时只剩 IoU=1.0 一个 TP，降序 (0.95,F)、(0.9,T)、(0.8,F)、(0.3,F)，AP 同样算得 0.5，故 `cal_ap` = 0.5。

需要观察的现象与预期结果：三个打印依次为 0.5、0.75、0.5；`ious.mean()` = 0.5875。若与手工值不符，优先检查 `ious > thr` 的严格大于语义。（待本地验证）

#### 4.4.5 小练习与答案

**练习 1**：为什么点提示能算 AP 而文本提示只有 precision？
**答案**：AP 需要按置信度对预测做全局排序。点提示有 `point_prob`（GT 类别列的 softmax 概率，[uniseg3d.py:L882-L884](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L882-L884)）可排序；文本提示按「每物体 × 每条描述」全量展开成平等的一组预测，没有跨描述可比的分数，只能数「IoU 超阈值占比」。

**练习 2**：某条提示 IoU 恰好等于 0.5，AP@50 里它是 TP 吗？
**答案**：不是。[L249](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_metric.py#L249) 用 `ious > thr` 严格大于，0.5 被归为 FP。

**练习 3**：`cal_ap_thr` 里那个从右往左的 `for` 循环（L263-L265）删掉会怎样？
**答案**：得到「原始 PR 折线」而非包络曲线，AP 会偏小且不再等价于 VOC 定义。包络把后续可能出现的低 precision 抬高，保证 AP 是在最优操作点序列上的积分。

## 5. 综合实践

**任务**：构造 2 个场景的假 GT/预测（含点提示与文本提示结果），端到端调用 `process()` + `compute_metrics()`，把输出键与手工计算逐一核对。设计目标：

- 常规三任务预测 = 完美复刻 GT → `miou`/`all_ap*`/`pq` 应达到满分；
- 点提示 4 条，(IoU, conf) = (1.0, 0.3)、(0.6, 0.9)、(0.5, 0.8)、(0.25, 0.95) → 4.4.4 已手工算出全部指标；
- 文本提示 2 条，IoU = 0.8、0.4 → mIoU = 0.6、precision_half = 0.5、precision_quarter = 1.0。

（以下为示例代码，在仓库根目录、u1-l2 环境下运行；保存为独立脚本，勿放源码目录。）

```python
# 示例代码：eval_metric_demo.py
import numpy as np, torch
from mmengine.logging import MMLogger
from uniseg3d.unified_metric import PromptSupportedUnifiedSegMetric

MMLogger.get_instance('mmengine', log_level='INFO')   # 模拟 Runner 建好日志器

class_names = ['wall','floor','cabinet','bed','chair','sofa','table','door',
    'window','bookshelf','picture','counter','desk','curtain','refrigerator',
    'showercurtrain','toilet','sink','bathtub','otherfurniture','unlabeled']
sem_mapping = [1,2,3,4,5,6,7,8,9,10,11,12,14,16,24,28,33,34,36,39]

metric = PromptSupportedUnifiedSegMetric(
    thing_class_inds=list(range(2, 20)), stuff_class_inds=[0, 1],
    min_num_points=1, id_offset=2**16, sem_mapping=sem_mapping,
    inst_mapping=sem_mapping[2:],
    metric_meta=dict(label2cat={i: n for i, n in enumerate(class_names)},
                     ignore_index=[20], classes=class_names,
                     dataset_name='ScanNet'))
metric.dataset_meta = dict(seg_valid_class_ids=tuple(sem_mapping))  # Runner 本会注入

def build_scene(with_point, with_text):
    """wall/floor 各 100 点 + cabinet、chair 两实例各 100 点 + 提示块。"""
    n = 400
    sem  = np.concatenate([np.zeros(100,int), np.ones(100,int),
                           np.full(100,2,int), np.full(100,4,int)])   # 0,1,2=cab,4=chair
    inst = np.concatenate([np.zeros(100,int), np.ones(100,int),
                           np.full(100,2,int), np.full(100,3,int)])   # 全景 markup
    sem_pred  = np.concatenate([sem.copy(), np.full(20,20,int)])      # 尾部 20 点未标注
    sem       = sem_pred.copy()
    inst_full = np.concatenate([inst.copy(), np.full(20,-1,int)])
    n_all = n + 20

    inst_bool = np.zeros((n_all, 2), bool)      # 实例任务预测：两个完美掩码
    inst_bool[200:300, 0] = True                # cabinet → thing 索引 0
    inst_bool[300:400, 1] = True                # chair   → thing 索引 2
    pred = dict(
        pts_semantic_mask=[sem_pred.copy(),          # [0] 语义任务：argmax 含 20
                           sem_pred.copy()],         # [1] 全景语义（完美时同值）
        pts_instance_mask=[inst_bool,                # [0] 实例任务布尔掩码
                           inst_full.copy()],        # [1] 全景实例 markup
        instance_labels=np.array([0, 2]), instance_scores=np.array([.9, .8]))

    ds = dict(eval_ann_info=dict(pts_semantic_mask=sem,
                                 pts_instance_mask=inst_full),
              pred_pts_seg=pred)

    def block(gt_n, pred_n):                       # 造一对 IoU=pred_n/gt_n 的提示行
        g = np.zeros((1, n_all), bool); p = np.zeros((1, n_all), bool)
        g[0, n:n+gt_n] = True; p[0, n:n+pred_n] = True
        return g, p
    if with_point:
        g1,p1 = block(10,10); g2,p2 = block(10,6); g3,p3 = block(10,5); g4,p4 = block(4,1)
        ds['point_prompt_pred_pts_seg'] = dict(
            point_prompt_object_gt_masks=np.vstack([g1,g2]),
            point_prompt_object_pred_masks=np.vstack([p1,p2]))
        ds['point_prob'] = torch.tensor([0.3, 0.9])
        scene2_point = dict(                          # scene2 的两条点提示
            point_prompt_object_gt_masks=np.vstack([g3,g4]),
            point_prompt_object_pred_masks=np.vstack([p3,p4]))
    if with_text:
        g5,p5 = block(10,8); g6,p6 = block(10,4)
        ds['text_prompt_pred_pts_seg'] = dict(
            text_prompt_object_gt_masks=np.vstack([g5,g6]),
            text_prompt_object_pred_masks=np.vstack([p5,p6]))
    return ds, scene2_point if with_point else None

# ---- 场景 1：点提示 2 条（IoU 1.0/0.6）----
ds1, s2p = build_scene(with_point=True, with_text=False)
# ---- 场景 2：点提示 2 条（0.5/0.25）+ 文本提示 2 条（0.8/0.4）----
ds2, _    = build_scene(with_point=True, with_text=True)
ds2['point_prompt_pred_pts_seg'] = s2p
ds2['point_prob'] = torch.tensor([0.8, 0.95])

metric.process({}, [ds1]); metric.process({}, [ds2])   # 注意 ds2 必须最后（见下）
metrics = metric.compute_metrics(metric.results)
for k, v in metrics.items(): print(f'{k:24s} {v}')
```

预期结果（提示指标可精确手工核对，三任务指标为满分预期）：

| 键 | 预期值 | 依据 |
| --- | --- | --- |
| `miou` | 1.0（待本地验证） | 语义预测 = GT，unlabeled 被 ignore |
| `all_ap` / `all_ap_50%` / `all_ap_25%` | 1.0（待本地验证） | 两实例掩码完美、各 ≥100 点、类别正确 |
| `pq` | 1.0（待本地验证） | 全景 markup 与 GT 一致；未标注 20 点被 ignore |
| `Point Prompt mIoU` | **0.5875** | (1.0+0.6+0.5+0.25)/4 |
| `Point Prompt AP@50` | **0.5** | 4.4.4 手工推导 |
| `Point Prompt AP@25` | **0.75** | 4.4.4 手工推导 |
| `Point Prompt AP` | **0.5** | 4.4.4 手工推导 |
| `Text Prompt mIoU` | **0.6** | (0.8+0.4)/2 |
| `Text_Precision_half` | **0.5** | 0.8>0.5 计 1 条 / 2 |
| `Text_Precision_quarter` | **1.0** | 两条都 >0.25 |

进阶实验（强烈建议做）：

1. **验证「最后一个样本」陷阱**：把 `process` 的两行对调（ds2 先、ds1 后），使最后一个场景没有文本提示——预期 `Text Prompt *` 三键整组消失，其余不变。这正是 4.2.3 指出的循环残留变量行为。
2. **验证 AP 的置信度敏感性**：把 ds1 的 `point_prob` 改成 `[0.95, 0.9]`（两个高分），观察 `Point Prompt AP@50` 如何变化，并用 4.4.4 的方法手工复算。
3. **验证 100 点下限**：把 cabinet 实例截成 60 点（GT 与预测同步），预期该类从实例评估中被剔除（GT 侧过滤），`all_ap` 退化为只剩 chair 一类。

若运行报 `seg_valid_class_ids` 缺失，说明忘了 `metric.dataset_meta = ...`——真实运行时由 Runner 从数据集 metainfo 注入。

## 6. 本讲小结

- 评估链路是「**process 攒五元组 → compute 拆三路标准任务 + 两路提示任务**」：`(eval_ann_info, pred_3d, point_prompt, point_prob, text_prompt)`，`None` 是「无该任务」的哨兵。
- 模型侧的 `pts_semantic_mask`/`pts_instance_mask` 都是**二元素列表**：`[0]` 给语义/实例任务，`[1]` 给全景任务；评估器按下标各取所需。
- GT 只有一份全景 markup（stuff 0/1、things 从 2 起、未标注 -1），`map_inst_markup` 用「减 2 + 查表 + 非实例点置 -1」把它翻译成实例任务格式，`rename_gt` 再编码成 `1000×nyu+inst` 进入 ScanNet 官方评估。
- 实例 mAP 走 `aggregate_predictions → rename_gt → scannet_eval`：贪心一对一匹配（`pred_visited`）、void/小实例豁免、10 个 IoU 阈值（0.25 单列，0.5~0.9 九个 nanmean 成 `all_ap`）、100 点下限双侧生效。
- 提示任务指标是纯 numpy 自定义实现：对齐 IoU + `cal_ap_thr`（严格大于、降序 cumsum、precision 包络、Δrecall 积分）+ `cal_ap`（10 阈值平均）；文本无置信度故只报 precision。
- 两个工程陷阱：`point_prob` 必须与点提示成对出现且为张量；提示指标是否输出取决于**最后一个**样本是否带提示。

## 7. 下一步学习建议

- 下一讲 [u6-l3 开放词汇分割实战](u6-l3-open-vocabulary.md)：评估链路之外的第二条推理出口——类无关掩码保存与 Open3DIS 对接，会用到本讲 `preds_iou` 相关的过滤逻辑（承 u5-l4）。
- 想加深 AP 数值积分的直觉，可对照读 [uniseg3d/evaluate_semantic_instance.py:L179-L187](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/evaluate_semantic_instance.py#L179-L187)（convolve 步长积分）与 [uniseg3d/unified_metric.py:L261-L266](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_metric.py#L261-L266)（包络点积积分）——同一个定义的两种实现。
- 若要接入自定义数据集，回头对照 [uniseg3d/transforms_3d.py:L434-L441](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L434-L441) 检查你的 eval markup 是否满足「stuff 0/1、things 从 2 起、未标注 -1」，否则 `map_inst_markup`/`rename_gt` 的断言会拦住你。
- 最后一讲 [u6-l4 二次开发指南](u6-l4-extensions.md) 会把「新增提示类型」时评估器需要同步改动的位置（process 搬运、compute 分支、新指标键）整理成清单。

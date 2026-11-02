# 全景分割推理分支

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂 `MaskFormer.panoptic_inference` 的完整后处理流程：query 过滤 → 像素分配 → 段级过滤 → stuff 合并 → 输出 id 图与 `segments_info`。
2. 说清 `OBJECT_MASK_THRESHOLD`（query 级置信度过滤）与 `OVERLAP_THRESHOLD`（段级重叠剔除）这两个阈值各在哪一步起作用、方向如何。
3. 掌握 `stuff_memory_list` 的按类合并逻辑：同一 stuff 类的多个 query 共享一个 segment id。
4. 理解输出契约——`panoptic_seg`（int32 id 图）与 `segments_info`（`id`/`isthing`/`category_id` 列表）的对应关系，以及 id=0 的空洞含义。

本讲承接 u6-l1（语义分支与 eval 总流程）与 u6-l2（实例分支），是推理三分支的最后一讲。

## 2. 前置知识

- **全景分割（panoptic segmentation）**：把语义分割（stuff 类，如草地、天空，无个体区分）与实例分割（thing 类，如人、车，每个个体一条）拼成一张统一标注图。要求**每个像素最多属于一个段**，thing 与 stuff 互斥。
- **PQ 指标直觉**：评估时一张预测段要和 GT 段做 IoU 匹配（IoU > 0.5 记 TP）。这解释了后处理为何宁可留空洞（id=0）也不让两个段互相重叠。
- **mask classification 范式回顾**（u3-l1）：模型输出 `mask_cls (Q, K+1)` 与 `mask_pred (Q, H, W)`。Q=100 个 query，K 个真实类 + 1 个 no-object 通道（下标 `K = sem_seg_head.num_classes`）。
- **三分支对比**（u6-l1/u6-l2 已讲）：
  - 语义分支：`softmax` 去掉 no-object 列后与 sigmoid mask 做 `einsum` **软融合**，输出每类概率图；
  - 实例分支：query×class 展开后 **topk 挑选**，不改变像素归属；
  - 全景分支（本讲）：**硬分配**——每个像素通过 argmax 只归一个 query，再把 query 映射为段。
- **输入尺寸**：`panoptic_on` 开启时 `sem_seg_postprocess_before_inference` 被强制置 True（见 4.1.3），因此进入本函数的 `mask_pred` 已是**原图尺寸**。
- **thing 判定**：`metadata.thing_dataset_id_to_contiguous_id` 是「数据集类别 id → 连续训练 id」的映射，其 `values()` 就是所有 thing 类的连续 id 集合（u2-l2 注册数据集时写入）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [mask2former/maskformer_model.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py) | 本讲主角：`panoptic_inference`（L286-L342）、eval 分支调用点（L252-L255）、阈值注入（L145-L146）、实例分支 thing 过滤对照（L359-L367） |
| [mask2former/config.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py) | 两个阈值的**默认值**注册处（0.0） |
| [configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml) | COCO 全景配置把两个阈值**覆写为 0.8** |
| [demo/predictor.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/demo/predictor.py) | demo 中 `panoptic_seg` + `segments_info` 的标准消费方式（可视化） |
| [train_net.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py) | `build_evaluator` 里 `COCOPanopticEvaluator` 消费本函数输出（下一讲 u7-l1 主题） |

## 4. 核心概念与源码讲解

### 4.1 入口与 query 级过滤：OBJECT_MASK_THRESHOLD

#### 4.1.1 概念说明

全景推理的第一步不是分配像素，而是**先淘汰不配参赛的 query**：

- argmax 类别是 no-object 的 query（模型认为"这里没有任何物体"）直接出局；
- 分类置信度（softmax 后的最大概率）不超过 `OBJECT_MASK_THRESHOLD` 的 query 也出局。

注意这是**严格的 `>`**：分数恰好等于阈值也会被淘汰。被淘汰的 query 不参与后续 argmax 竞争，它们的领地会变成空洞（id=0，见 4.2）。

与实例分支的区别：实例分支用 topk「选 N 个最好的」，全景分支用阈值「选所有够格的」——全景天然允许一张图里有几十个 stuff 段，固定 topk 并不合适。

#### 4.1.2 核心流程

```text
mask_cls (Q, K+1)
   │ softmax(dim=-1).max(-1)        # 注意：softmax 包含 no-object 通道
   ▼
scores (Q,), labels (Q,)            # 每个 query 的最大概率及其类别
keep = labels.ne(K) 且 scores > OBJECT_MASK_THRESHOLD
   ▼
cur_scores / cur_classes / cur_masks   # 仅保留的 N 个 query
```

关键点：`softmax` 在**全部 K+1 个通道**上做，no-object 参与归一化。若某 query 的 argmax 正好落在 no-object（labels == K），`labels.ne(num_classes)` 将其排除——这是与语义分支（先 softmax 再切掉最后一列）完全不同的处理方式。

#### 4.1.3 源码精读

eval 分支的调用点（承接 u6-l1，外层包 `retry_if_cuda_oom` 防高分辨率 OOM）：

[mask2former/maskformer_model.py:L252-L255](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L252-L255) —— `panoptic_on` 开关打开时，用 `retry_if_cuda_oom` 包裹调用 `panoptic_inference(mask_cls_result, mask_pred_result)`，结果元组存入 `processed_results[-1]["panoptic_seg"]`。

[mask2former/maskformer_model.py:L145-L153](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L145-L153) —— `from_config` 从 `cfg.MODEL.MASK_FORMER.TEST.OBJECT_MASK_THRESHOLD / OVERLAP_THRESHOLD` 读入两个阈值；同时只要 `PANOPTIC_ON` 或 `INSTANCE_ON` 打开就强制 `sem_seg_postprocess_before_inference=True`，保证 mask 先还原到原图尺寸再做推理。

函数头部与过滤逻辑：

```python
def panoptic_inference(self, mask_cls, mask_pred):
    scores, labels = F.softmax(mask_cls, dim=-1).max(-1)
    mask_pred = mask_pred.sigmoid()

    keep = labels.ne(self.sem_seg_head.num_classes) & (scores > self.object_mask_threshold)
    cur_scores = scores[keep]
    cur_classes = labels[keep]
    cur_masks = mask_pred[keep]
```

[mask2former/maskformer_model.py:L286-L295](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L286-L295) —— 第一行在 K+1 个通道上 softmax 后取每个 query 的最大概率 `scores` 与对应类别 `labels`；第三行用「类别不是 no-object **且** 分数严格大于 `object_mask_threshold`」的布尔掩码一次性筛出保留的 query。**布尔掩码索引保持原 query 顺序**（不按分数排序），后面循环按原顺序进行。

阈值来源的两处对照：

- [mask2former/config.py:L53-L60](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L53-L60) —— 默认注册值都是 **0.0**（纯语义任务用不到全景后处理，等于不过滤）。
- [configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml:L40-L45](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml#L40-L45) —— COCO 全景配置把 `OVERLAP_THRESHOLD` 与 `OBJECT_MASK_THRESHOLD` 都设为 **0.8**，并打开 `PANOPTIC_ON`。

**源码阅读小发现**：L294-L295 的 `cur_mask_cls = mask_cls[keep]; cur_mask_cls = cur_mask_cls[:, :-1]` 赋值后**从未被使用**，是当前实现里的死代码（疑似 MaskFormer v1 遗留），阅读时可跳过。

#### 4.1.4 代码实践

1. **实践目标**：亲手验证 keep 过滤行为。
2. **操作步骤**（示例代码，需已按 u1-l2 装好 detectron2 环境）：

```python
# 示例代码：验证 query 级过滤
import math, torch
import torch.nn.functional as F

K = 4                                     # 4 个真实类别
def row(target, score):                   # softmax 后 target 类概率 ≈ score
    r = torch.full((K + 1,), -20.0); r[target] = math.log(score); r[K] = math.log(1 - score)
    return r

mask_cls = torch.stack([row(0, 0.95), row(1, 0.70), row(2, 0.90), row(K, 0.90)])
scores, labels = F.softmax(mask_cls, dim=-1).max(-1)
print(scores)   # 预期 ≈ [0.95, 0.70, 0.90, 0.90]
print(labels)   # 预期 = [0, 1, 2, 4]，最后一个 query 的 argmax 是 no-object
for omt in (0.8, 0.5):
    keep = labels.ne(K) & (scores > omt)
    print(omt, keep.tolist())
```

3. **需要观察的现象**：`labels` 的最后一项等于 K（no-object 的下标）；`omt=0.8` 时只有 query 0、2 通过；`omt=0.5` 时 query 1 也通过；query 3 在两种阈值下都被 `labels.ne(K)` 排除。
4. **预期结果**：keep 分别为 `[True, False, True, False]` 与 `[True, True, True, False]`。精确浮点值待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `softmax` 不先切掉 no-object 通道再取 max？
**答案**：全景分支需要判断"模型最相信这个 query 是什么"；若先切掉 no-object，一个整体置信度极低的 query 也会被迫在真实类里挑一个 argmax，从而以虚假的高条件概率进入后续流程。保留 no-object 参与归一化，让"什么都不像"的 query 自然落在 K 上被 `labels.ne(K)` 剔除。

**练习 2**：把 `OBJECT_MASK_THRESHOLD` 从 0.8 提高到 0.95，段数量一定减少吗？
**答案**：会减少或不变。该阈值只做淘汰、不做新增；提高它只会淘汰更多 query（严格大于比较），保留下来的 query 子集是原来的子集，产生的段不会更多。

### 4.2 像素分配：cur_prob_masks 与 argmax

#### 4.2.1 概念说明

过滤后剩下 N 个 query，每个 query 有一个分数 \( s_k \) 和一张 sigmoid 概率图 \( \sigma(m_k) \in (0,1)^{H\times W} \)。像素分配规则是把二者相乘后逐像素 argmax：

\[ p_{k,h,w} = s_k \cdot \sigma(m_{k,h,w}), \qquad \text{owner}(h,w) = \arg\max_k\; p_{k,h,w} \]

直觉：分数高的 query 在争议像素上更有话语权。每个像素恰好有一个获胜 query，因此 `cur_mask_ids` 中各 query 的领地**天然互斥**——这正是全景分割"像素不重不漏（漏的部分为空洞）"的来源。

#### 4.2.2 核心流程

```text
cur_prob_masks = cur_scores.view(-1,1,1) * cur_masks     # (N, H, W)
cur_mask_ids = cur_prob_masks.argmax(0)                  # (H, W)，值域 [0, N)
for k in 0..N-1:
    mask = (cur_mask_ids == k) & (cur_masks[k] >= 0.5)   # 落笔前还要过 0.5 二值化这道闸
```

注意两道关卡的区别：argmax 决定"这像素归谁"，`>= 0.5` 决定"赢了之后是否真的画上去"。若某像素的获胜 query 自身概率不足 0.5，该像素保持 id=0（空洞）。

#### 4.2.3 源码精读

[mask2former/maskformer_model.py:L297-L310](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L297-L310) —— `cur_scores.view(-1, 1, 1)` 把分数广播成 (N,1,1) 与 (N,H,W) 的 mask 概率逐元素相乘得到 `cur_prob_masks`；初始化全零 int32 的 `panoptic_seg` 与空的 `segments_info`；若 N=0（一个 query 都没过筛）直接返回空结果；否则 `cur_prob_masks.argmax(0)` 得到每个像素的获胜 query 下标 `cur_mask_ids`。

[mask2former/maskformer_model.py:L312-L317](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L312-L317) —— 循环体开头为每个 k 统计三个量：`mask_area`（argmax 赢下的像素数）、`original_area`（自身概率 ≥0.5 的"原始地盘"像素数）、`mask`（二者交集，即最终落笔区域）。

**为什么 `mask` 要再交一次 `& (cur_masks[k] >= 0.5)`？** 考虑一个所有 query 概率都接近 0 的像素（如被淘汰 query 的旧领地）：argmax 仍会返回某个下标（比如 0），若不检查 0.5，这个"全场无人认领"的像素会被错误画进 query 0 的段。交集操作把这类像素留在 id=0。

#### 4.2.4 代码实践

1. **实践目标**：直观看到"分数加权 argmax"如何裁决争议像素。
2. **操作步骤**（示例代码）：

```python
# 示例代码：两个 query 争夺同一像素
masks = torch.tensor([[[1.0, 1.0], [0.0, 0.0]],
                      [[0.0, 1.0], [0.0, 0.0]]])   # query1 只额外覆盖 (0,1)
scores = torch.tensor([0.9, 0.7])
prob = scores.view(-1, 1, 1) * masks
print(prob.argmax(0))    # 像素 (0,1) 争议：0.9 > 0.7 → 归 query 0
```

3. **需要观察的现象**：像素 (0,1) 两个 query 概率都是 1.0，但 query 0 分数更高而获胜；整张图所有像素都有唯一归属。
4. **预期结果**：输出 `[[0, 0], [0, 0]]`（query 1 一无所获，随后会因 `mask_area=0` 被丢弃，见 4.3）。

#### 4.2.5 小练习与答案

**练习 1**：`cur_mask_ids` 里不同 k 的区域会重叠吗？后处理的循环会互相覆盖 `panoptic_seg` 吗？
**答案**：不会重叠。argmax 保证每个像素只有一个获胜下标，`(cur_mask_ids == k)` 的集合彼此互斥，再与各自的 `>=0.5` 区域取交集仍然互斥，所以循环按任意顺序落笔结果都一样。

**练习 2**：一个像素的获胜 query 后来在 4.3 的重叠过滤中被丢弃，这个像素会重新分配给别人吗？
**答案**：不会。argmax 只做一次；被丢弃段赢下的像素既不会被重新分配，也不满足其他 query 的落笔条件，最终保持 id=0（空洞）。

### 4.3 段级过滤：mask_area / original_area 与 OVERLAP_THRESHOLD

#### 4.3.1 概念说明

argmax 之后每个段统计两个面积：

- `original_area`：query 自身概率 ≥0.5 的像素数——它"声称"的地盘；
- `mask_area`：argmax 实际赢下的像素数——它"守住"的地盘。

保留条件（比例检验）：

\[ \frac{\text{mask\_area}}{\text{original\_area}} \geq \text{OVERLAP\_THRESHOLD} \]

若一个 query 声称 100 个像素却只守住 10 个（比例 0.1），说明它的地盘几乎全被别的 query 抢走——这是一个"边界模糊、与他物高度重叠"的劣质预测，整段丢弃（`continue`），其像素变空洞。这与论文中 "remove the overlapped regions" 的后处理一致，也是 MaskFormer 一系（继承自 DeeperLab）的经典策略：**先到先得 + 整段淘汰**，而不是把重叠像素拆开重分。

三重前置守卫 `mask_area > 0 and original_area > 0 and mask.sum() > 0` 防止除零与空段。

#### 4.3.2 核心流程

```text
for k in 保留的 query（按原 query 顺序）:
    mask_area    = (cur_mask_ids == k).sum()
    original_area = (cur_masks[k] >= 0.5).sum()
    mask = (cur_mask_ids == k) & (cur_masks[k] >= 0.5)
    if 三者均 > 0 且 mask_area / original_area < OVERLAP_THRESHOLD:
        continue                     # 整段丢弃，像素留空洞
    # 通过 → 进入 4.4 的 stuff 合并与落笔
```

两个阈值的分工一张表看清：

| 阈值 | 作用层级 | 判据 | 淘汰对象 | COCO 全景取值 |
| --- | --- | --- | --- | --- |
| `OBJECT_MASK_THRESHOLD` | query 级（argmax 之前） | 分类置信度 | 低置信 / no-object query | 0.8 |
| `OVERLAP_THRESHOLD` | 段级（argmax 之后） | mask_area / original_area | 被大量抢占的重叠段 | 0.8 |

#### 4.3.3 源码精读

[mask2former/maskformer_model.py:L312-L321](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L312-L321) —— 每轮循环先取出类别 `pred_class`、用 `self.metadata.thing_dataset_id_to_contiguous_id.values()` 判定 `isthing`，再计算 `mask_area`、`original_area` 与交集 `mask`；守卫通过后若 `mask_area / original_area < self.overlap_threshold` 则 `continue` 跳过该段（注意：分母是 `original_area`，比较方向是"低于阈值才丢弃"，因此**调高阈值会丢弃更多段**）。

对照实例分支的 thing 过滤（u6-l2 已讲）：

[mask2former/maskformer_model.py:L359-L367](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L359-L367) —— 实例分支在 `panoptic_on` 打开时同样用 `thing_dataset_id_to_contiguous_id.values()` 把 stuff 类剔除，保证全景模式下实例输出与全景输出互不越界。两个分支共用同一份 thing 判定元数据。

#### 4.3.4 代码实践

1. **实践目标**：构造一个比例恰在 0.8 与 0.95 之间的段，验证阈值方向。
2. **操作步骤**（示例代码，续 4.1.4 的 `row` 辅助函数）：设计一个 mask 有 10 个 ≥0.5 的像素，其中 1 个被分数更高的 query（0.95 vs 0.88）抢走，则 `mask_area=9, original_area=10`，比例 0.9：

```python
# 示例代码：比例 0.9 的段在不同 OVERLAP_THRESHOLD 下的命运
area_ratio = 9 / 10
for ot in (0.8, 0.95):
    print(ot, "丢弃" if area_ratio < ot else "保留")
```

3. **需要观察的现象**：`ot=0.8` 时保留、`ot=0.95` 时丢弃。
4. **预期结果**：输出 `0.8 保留`、`0.95 丢弃`。完整端到端验证见第 5 节综合实践。

#### 4.3.5 小练习与答案

**练习 1**：为什么分母用 `original_area`（≥0.5 的像素数）而不是整图面积？
**答案**：整图面积与预测质量无关。`original_area` 度量 query 自己声称的地盘，比值刻画"声称的地盘守住了几成"，是被抢占程度的归一化；用整图面积会让大物体天然吃亏。

**练习 2**：某 query 的 `mask_area=0`（一像素未赢）时会发生什么？
**答案**：`mask_area > 0` 守卫失败，直接跳过——这实际上也是比例检验的极限情形（0/original_area = 0 < 任何正阈值），守卫只是把除零防御显式化了。

### 4.4 stuff 合并与输出契约：stuff_memory_list、panoptic_seg 与 segments_info

#### 4.4.1 概念说明

- **stuff 合并**：同一 stuff 类（如"草地"）可能被多个 query 分别预测在图像不同位置。全景分割中同一 stuff 类**只需要一个类别标签**，不需要区分"第几块草地"。`stuff_memory_list` 是"类别 → 已分配 segment id"的备忘录：该类第二次出现时不再新建段，直接复用旧 id 落笔。thing 类永不合并——每个人都是独立实例。
- **输出契约**（与 COCO panoptic 标注格式对齐，供 `COCOPanopticEvaluator` 与可视化器消费）：
  - `panoptic_seg`：(H, W) 的 **int32** 张量，像素值是 segment id，**从 1 开始编号**，0 表示空洞/未分配；
  - `segments_info`：`list[dict]`，每个 dict 含 `"id"`（对应 id 图中的值）、`"category_id"`（训练用的连续类别 id）、`"isthing"`。
  - 一个 id 可以对应**多块不连通区域**（合并后的 stuff），但 `segments_info` 里只有一条记录——这是"按类合并"的直接推论。

#### 4.4.2 核心流程

```text
for k in 保留且未被重叠过滤的 query（原 query 顺序）:
    if 不是 thing:
        if pred_class in stuff_memory_list:
            panoptic_seg[mask] = stuff_memory_list[pred_class]; continue   # 复用旧 id，不新增记录
        else:
            stuff_memory_list[pred_class] = current_segment_id + 1         # 预登记下一个 id
    current_segment_id += 1
    panoptic_seg[mask] = current_segment_id
    segments_info.append({id, isthing, category_id})
```

三个易错点：

1. **比例检验在合并查表之前**：一个同类但被严重抢占的 stuff query 会先被 4.3 丢弃，而不是被"合并"掉。
2. **循环按原 query 顺序而非分数顺序**，segment id 的编号顺序 = query 下标顺序，与置信度无关（实例分支才是按分数排序的 topk）。
3. stuff 首次出现时在 `stuff_memory_list` 预登记的 id 恰是"将要分配"的 `current_segment_id + 1`，随后 `+=1` 与落笔使用同一值，两条路径编号一致。

#### 4.4.3 源码精读

[mask2former/maskformer_model.py:L323-L332](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L323-L332) —— `if not isthing:` 分支实现合并备忘录：类别已在 `stuff_memory_list` 中则把当前 `mask` 直接画成旧 id 并 `continue`（不新增段）；否则登记 `current_segment_id + 1` 后正常走新增流程（`+=1`、落笔）。

[mask2former/maskformer_model.py:L334-L342](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L334-L342) —— 每个新增段追加一条 `{"id", "isthing", "category_id"}` 记录（`isthing` 转 Python bool、`category_id` 转 int，方便序列化），函数返回 `(panoptic_seg, segments_info)` 元组。

消费端示例——demo 的可视化正是按输出契约解包：

[demo/predictor.py:L53-L57](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/demo/predictor.py#L53-L57) —— `panoptic_seg, segments_info = predictions["panoptic_seg"]` 后调用 detectron2 `Visualizer.draw_panoptic_seg_predictions(panoptic_seg, segments_info)`，按 id 上色、按 `segments_info` 标类别。

评估端（下一讲详细展开）：

[train_net.py:L93-L100](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L93-L100) —— `evaluator_type` 为 `coco_panoptic_seg` 等且 `PANOPTIC_ON` 打开时追加 `COCOPanopticEvaluator`，它把 id 图 + `segments_info` 写成 COCO panoptic JSON 算 PQ。

#### 4.4.4 代码实践

1. **实践目标**：观察同类 stuff 的两个 query 合并成一条 `segments_info` 记录。
2. **操作步骤**（示例代码）：构造两个同类（类别 2）、区域不重叠、分数 0.90 / 0.85 的 query：

```python
# 示例代码：两个 grass query + 一个 person query（完整脚本见第 5 节）
# 预期 segments_info 只有一条 category_id=2 的记录，
# 但 panoptic_seg 中该 id 覆盖两个不连通区域
```

3. **需要观察的现象**：`segments_info` 中类别 2 只出现一次；`torch.bincount` 统计该 id 的像素数 = 两个区域面积之和。
4. **预期结果**：一条 grass 记录、像素数为两块区域之和；端到端复现见第 5 节（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：如果两个同类 stuff query 中第二个被 `OVERLAP_THRESHOLD` 过滤了，还会发生合并吗？
**答案**：不会。比例检验（L320-L321）在合并查表（L325）之前执行，被过滤的 query 直接 `continue`，既不落笔也不触发合并；它声称的区域中 argmax 赢下的像素变成空洞。

**练习 2**：为什么 thing 类不用同样的备忘录合并？
**答案**：thing 的语义是"可数个体"——两个人是两个实例，必须各占一个 id；stuff 的语义是"物质类别"——两块草地共享同一类别身份，合并正是全景标注（COCO panoptic 对 stuff 每类一个 segment）的要求。

**练习 3**：`panoptic_seg` 中 id=0 的像素有哪些来源？
**答案**：三类：(1) 被淘汰 query（no-object、低置信）的旧领地；(2) 获胜 query 自身概率 <0.5 的像素；(3) 被 `OVERLAP_THRESHOLD` 丢弃的段的像素。

## 5. 综合实践

**任务**：用玩具输入直接调用真实的 `MaskFormer.panoptic_inference`，渲染 id 图并打印 `segments_info`；再扫描两组阈值，统计段数变化并解释 stuff 合并行为。

**设计图**（16×16，类别：0=person/thing、1=bike/thing、2=grass/stuff、3=sky/stuff，K=4）：

| query | 类别 | 分数 | mask 区域 | 测试点 |
| --- | --- | --- | --- | --- |
| q0 | 0 person | 0.95 | 左上 8×8（64px） | 高分基准段 |
| q1 | 1 bike | 0.70 | 右上 8×8（64px） | 分数在 0.5~0.8 之间，OMT 测试点 |
| q2 | 2 grass | 0.90 | 左下 8×8（64px） | stuff 合并的"第一块草地" |
| q3 | 2 grass | 0.85 | 右下 8×8（64px） | 同类 stuff 合并测试点 |
| q4 | 3 sky | 0.88 | 10px：9px 独占（右上角内）+1px 与 q0 重叠 | 比例 9/10=0.9，OT 测试点 |
| q5 | — | — | 空 | argmax 为 no-object，应被剔除 |

q4 的 1 个像素 `(0,0)` 落在 q0 地盘内且 q0 分数更高，会被抢走，故 `mask_area=9, original_area=10`，比例恰为 0.9。

**操作步骤**——把下面脚本存为 `toy_panoptic.py`（示例代码，非项目原有文件）运行：

```python
# 示例代码：玩具输入 + 阈值扫描
import math
from types import SimpleNamespace
import torch
from mask2former.maskformer_model import MaskFormer   # 需 detectron2 环境（u1-l2）

H = W = 16; K = 4; NO_OBJ = K

def row(target, score):
    r = torch.full((K + 1,), -20.0)
    r[target] = math.log(score); r[NO_OBJ] = math.log(1 - score)
    return r

def box(r0, r1, c0, c1):                      # 闭区间像素集合
    return [(r, c) for r in range(r0, r1 + 1) for c in range(c0, c1 + 1)]

def mask_from(pixels):
    m = torch.full((H, W), -10.0)
    for r, c in pixels: m[r, c] = 10.0        # sigmoid(-10)≈0，sigmoid(10)≈1
    return m

q4_px = box(0, 3, 14, 15) + [(4, 15), (0, 0)]  # 9 独占 + 1 被 q0 抢走
mask_cls = torch.stack([row(0, .95), row(1, .70), row(2, .90),
                        row(2, .85), row(3, .88), row(NO_OBJ, .90)])
mask_pred = torch.stack([mask_from(box(0, 7, 0, 7)), mask_from(box(0, 7, 8, 15)),
                         mask_from(box(8, 15, 0, 7)), mask_from(box(8, 15, 8, 15)),
                         mask_from(q4_px), mask_from([])])

def stub(omt, ot):                            # 只喂方法真正用到的 4 个属性
    return SimpleNamespace(object_mask_threshold=omt, overlap_threshold=ot,
        sem_seg_head=SimpleNamespace(num_classes=K),
        metadata=SimpleNamespace(thing_dataset_id_to_contiguous_id={1: 0, 2: 1}))  # 0、1 是 thing

for omt, ot, name in [(0.8, 0.8, "A"), (0.5, 0.8, "B"), (0.8, 0.95, "C")]:
    seg, info = MaskFormer.panoptic_inference(stub(omt, ot), mask_cls, mask_pred)
    counts = torch.bincount(seg.flatten(), minlength=6)
    print(f"[{name}] OMT={omt} OT={ot} 段数={len(info)} 各id像素={counts.tolist()}")
    for d in info: print("   ", d)
    # 渲染：id=0 黑色，其余每 id 一色（示例代码）
    import matplotlib.pyplot as plt
    pal = torch.tensor([[0,0,0],[230,30,100],[30,200,30],[30,30,230],[230,200,30]], dtype=torch.uint8)
    plt.imsave(f"panoptic_{name}.png", pal[seg.long()].numpy())
```

**预期结果**（逻辑推演，精确像素数待本地验证）：

| 配置 | 淘汰情况 | 段数 | segments_info（按序） | id 像素分布 |
| --- | --- | --- | --- | --- |
| A：OMT=0.8, OT=0.8 | q1（0.70≤0.8）、q5（no-object）出局；q4 比例 0.9≥0.8 保留 | 3 | id1 person、id2 grass、id3 sky | 64 / 128 / 9，空洞 55 |
| B：OMT=0.8→0.5 | q1 回归（比例 55/64≈0.86≥0.8 保留） | 4 | id1 person、id2 bike、id3 grass、id4 sky | 64 / 55 / 128 / 9，无空洞 |
| C：OT=0.8→0.95 | q4 比例 0.9<0.95 被整段丢弃 | 2 | id1 person、id2 grass | 64 / 128，空洞 64 |

**需要观察与解释的三个现象**：

1. **OMT 0.8→0.5（A→B）**：bike 段出现。q1 的领地被 q4 抢走 9px 后守住 55/64≈0.86，仍过 OT=0.8 这一关。注意 A 中 q1 的 55 个像素是空洞——证明被淘汰 query 的领地不会被重新分配（4.2 练习 2）。
2. **OT 0.8→0.95（A→C）**：sky 段整段消失，其 9 个像素并入空洞。方向确认：**OT 是"守住比例的下限"，调高只会丢更多段**。
3. **stuff 合并**：三种配置里 grass 都只有一条 `segments_info` 记录，但该 id 的像素数是 128（两块 8×8）——q3 复用 q2 的 id 落笔、不新增记录（4.4.3）。同时注意：合并前 q3 也要先通过比例检验（本例比例 1.0）。

**思考题**：把 q3 的分数降到 0.70 且让它的地盘与 q2 完全重叠，会发生什么？
**参考答案**：q3 在 argmax 中一像素都赢不了（q2 分数更高且覆盖相同区域），`mask_area=0` 触守卫被丢弃——**不触发合并**；合并只发生在"守住了自己地盘的同类 stuff query"之间。

## 6. 本讲小结

- 全景后处理是四级流水线：**query 级过滤**（`labels.ne(K)` + `scores > OBJECT_MASK_THRESHOLD`，严格大于）→ **像素分配**（\( s_k \cdot \sigma(m_k) \) 逐像素 argmax，领地互斥）→ **段级过滤**（`mask_area / original_area < OVERLAP_THRESHOLD` 整段丢弃、留空洞）→ **stuff 合并与落笔**（`stuff_memory_list` 同类复用 id）。
- 两个阈值方向不同：OMT 调高淘汰更多低置信 query；OT 是"守住地盘比例"的下限，调高淘汰更多重叠段；默认值 0.0（[config.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L58-L59)），COCO 全景配置统一用 0.8。
- 输出契约：`(panoptic_seg, segments_info)`——int32 id 图从 1 编号、0 为空洞（被淘汰 query 领地 / 获胜者概率 <0.5 / 被段级过滤丢弃三类来源）；`segments_info` 每条含 `id/isthing/category_id`，同一 stuff 类多块区域共享一条记录。
- 与另两个分支对比：语义分支软融合不产生段，实例分支 topk 不改像素归属，全景分支是唯一做像素硬分配的分支；实例分支在 `panoptic_on` 时复用同一份 thing 元数据剔除 stuff 类。
- 阅读彩蛋：L294-L295 的 `cur_mask_cls` 是死代码；循环按原 query 顺序编号，与分数无关。

## 7. 下一步学习建议

- **u7-l1 评估器分发**：本讲输出的 `(panoptic_seg, segments_info)` 会被 [train_net.py:L93-L100](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L93-L100) 的 `COCOPanopticEvaluator` 消费——它如何把 id 图写成 PNG、`segments_info` 写成 JSON、`category_id` 如何从连续 id 映射回数据集 id 并计算 PQ，是下一讲的主线。
- **对比阅读**：detectron2 中 MaskFormer（v1）的 `panoptic_inference`（本仓库 `mask2former/modeling/meta_arch/` 未包含，可对照官方 MaskFormer 仓库）以及 panopticapi 的 `pq_compute`，理解"id 图 + segments_info"这一格式为何能成为全景分割的通用中间表示。
- **动手延伸**：在第 5 节脚本基础上，把玩具 logits 换成真实模型对一张图的输出（参考 u1-l4 的 demo 流程），比较真实输出与玩具输出的 `segments_info` 规模差异。

# 开放词汇实例损失 OVInstanceCriterion：base/novel 双路监督

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 `is_novel` 标记从数据侧（`AddSuperPointAnnotations`）到损失侧（`OVInstanceCriterion`）的完整传递链路。
2. 独立复述「先 base 匹配、剩余查询再 novel 匹配」的两阶段流程，并解释为什么 novel 匹配必须在 base 匹配的**剩余查询**上进行。
3. 逐行读懂 `cls_target` 的构造：背景列 `n_classes`、base 标签 `0..17`、以及被 novel 匹配占用的查询为何被置为 `-1`（`ignore_index`）。
4. 理解 `iter_matcher`、`fix_dice_loss_weight`、`fix_mean_loss` 三个工程开关的真实作用（其中一个的实际效果可能和它的名字给你的直觉不一样）。
5. 理解 7 份解码器预测（1 主 + 6 aux）如何在 `__call__` 里被逐层累加进最终的 `inst_loss` / `ov_inst_loss` / `iou_inst_loss`。

## 2. 前置知识

### 2.1 开放词汇分割与「伪掩码」

**开放词汇分割（Open-Vocabulary Segmentation）** 指模型能识别训练标注词表**之外**的类别。UniSeg3D 的训练策略是：

- **base 类**：ScanNet 人工标注的 18 个实例类（cabinet、chair……），标注可靠、有类别标签。
- **novel 类**：训练时没有人工标注的类别。仓库用 **SAM3D** 在训练场景上离线生成了「类无关伪掩码」`sam3d_pseudo_sp_masks`（见 u1-l4），它们**只有形状、没有可信类别**——哪是哪个物体的大致轮廓有，但「它是什么」不可信。

所以监督方式天然分成两路：base 走「分类 + 掩码」完整监督，novel 只走「掩码」监督。本讲的主角 `OVInstanceCriterion`（OV = Open-Vocabulary）就是实现这一双路监督的损失类。

### 2.2 你需要回忆的三个前置结论

| 前置讲义 | 需要回忆的结论 |
| --- | --- |
| u3-l3 | `gt_sp_masks` 的行布局 = K 个 base 实例行 + M 个 SAM3D 伪掩码行 + 21 个语义行；`is_novel` 与之逐行同长，伪掩码行标 `True` |
| u5-l1 | 损失总调度按 `[:-n-1]` / `[-n-1:]` 把 `gt_sp_masks` 切成实例部分与语义部分，`is_novel` 也随实例部分一起切片传入 |
| u5-l2 | `SparseMatcher` 不做全局匈牙利匹配，而是用 `query_masks`（允许矩阵）约束下每个 GT 独立 top-k 选查询；`inf` 掩码禁用不允许的组合 |

### 2.3 三个损失的数学直觉

- **交叉熵（分类）**：`cls_preds` 形状为 `(n_queries, 19)`（18 类 + 1 背景列）。`ignore_index=-1` 表示某些下标的目标是「不监督」，不进入损失。
- **BCE with logits（掩码）**：对匹配上的 `(查询, GT)` 对，逐超点做二元交叉熵，输入是未过 sigmoid 的 logits。
- **Dice（掩码重叠度）**：

\[ L_{\text{dice}} = 1 - \frac{2\sum_i p_i t_i + 1}{\sum_i p_i + \sum_i t_i + 1} \]

完美重叠时趋于 0，完全不重叠时趋于 1，对掩码大小不敏感，与 BCE 互补。

## 3. 本讲源码地图

| 文件 | 在本讲中的角色 |
| --- | --- |
| [uniseg3d/instance_criterion.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py) | 主角：`OVInstanceCriterion`（L85-L643）+ 三种代价函数 + `SparseMatcher` |
| [uniseg3d/unified_criterion.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py) | 损失总调度：切出实例 GT 并把 `is_novel` 传递给本讲主角 |
| [uniseg3d/transforms_3d.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py) | `is_novel` 与伪掩码行的诞生地（`AddSuperPointAnnotations`） |
| [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py) | 双匹配器、`loss_weight` 六元组与三个开关的实参来源 |
| [uniseg3d/structures.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/structures.py) | `InstanceData_`：放宽等长断言的 GT 容器（u3-l2 已讲） |

## 4. 核心概念与源码讲解

本讲三个最小模块：

1. base/novel 拆分与两阶段匹配
2. cls/mask 损失计算与权重
3. aux_outputs 逐层损失累加

---

### 4.1 base/novel 拆分与两阶段匹配

#### 4.1.1 概念说明

一个批里有若干场景，每个场景的实例 GT 行混着两种来源：人工标注的 base 行、SAM3D 伪掩码的 novel 行，靠 `is_novel` 布尔向量区分。`OVInstanceCriterion` 的做法是**两阶段匹配**：

- **第一阶段（base）**：用完整代价（分类 + BCE + Dice）把 base GT 匹配到查询，走 DETR 式标准流程。
- **第二阶段（novel）**：把第一阶段**已被匹配占用的查询移除**，在「剩余查询」构成的新坐标系里，用只含掩码代价的 `ov_matcher` 给 novel 伪掩码二次匹配。

为什么 novel 必须在剩余查询上匹配？因为**一个查询只能输出一个掩码**。若同一查询既要逼近 base GT（还带分类监督）又要逼近 novel 伪掩码，两条梯度会互相拉扯；而且伪掩码噪声大于人工标注，理应让 base 优先挑走最好的查询。源码用 `assert` 明确锁死了这一不变量：base 与 novel 命中的查询集合必须不相交。

这里还有一个贯穿本讲的**坐标系**问题：novel 匹配发生在「剩余查询空间」（下标 0..n_remaining-1），而分类目标、IoU 分支都工作在「原始查询空间」（下标 0..n_queries-1），中间需要一个 `mask_map` 映射表来回翻译。

#### 4.1.2 核心流程

以单个场景（批内循环一次）为例：

```text
输入: pred(cls_preds[Nq,19], masks[Nq,Nsp]), gt(sp_masks[Ng,Nsp], labels_3d[Ng], is_novel[Ng], query_masks[Ng,Nq])

1. base 拆分:  base_gt = gt 中 is_novel==False 的行（labels/sp_masks/query_masks 同步行过滤）
2. base 匹配:  indices = matcher(pred, base_gt)            # 允许矩阵 = base query_masks
3. 剩余查询:  mask_[q] = q ∈ indices.query ? False : True
              mask_map = nonzero(mask_)                    # 剩余查询的原始下标表
4. novel 拆分: novel_gt = gt 中 is_novel==True 的行
              novel_gt.query_masks = query_masks[is_novel][:, mask_]   # 行过滤 + 列过滤
5. novel 匹配: ov_indices = ov_matcher(masks[mask_map], novel_gt)      # 在剩余空间里
6. 坐标还原:  ori_ov_query = mask_map[ov_indices.query]    # 剩余下标 → 原始下标

不变量: indices.query ∩ ori_ov_query = ∅                    # 源码 assert 三处锁死
```

用集合语言概括两阶段的输入输出关系：

\[ Q_{\text{base}} \subseteq Q, \qquad Q_{\text{novel}} \subseteq Q \setminus Q_{\text{base}}, \qquad Q_{\text{base}} \cap Q_{\text{novel}} = \varnothing \]

#### 4.1.3 源码精读

**(1) `is_novel` 的诞生（数据侧）**

[uniseg3d/transforms_3d.py:L385-L390](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L385-L390) 在 `merge_ov=True`（主配置 L176 就是这么开的）时：读入 `sam3d_pseudo_sp_masks`，造一个与 `gt_sp_masks` 总行数等长的全 False 向量，把伪掩码对应区段置 `True`，再把伪掩码行拼到实例掩码下面。

注意 [L396-L403](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L396-L403)：`gt_labels` 先用 0 初始化，随后只有 base 行在 [L405-L407](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L405-L407) 被写入真实标签——**novel 行的 `labels_3d` 是占位用的 0，不是真标签**。这一点是理解后文「novel 无分类损失」的钥匙。

**(2) 总调度的传递**

[uniseg3d/unified_criterion.py:L35-L41](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L35-L41) 把实例 GT 从 `gt_sp_masks` 中切出（`[:-n-1]`，n=20），`is_novel` 与 `sp_masks`、`labels_3d`、`query_masks` 用同一个切片，保证逐行对齐；随后在 [L65](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L65) 把 `pred` 与 `inst_gts` 一起交给本讲主角。

**(3) 双匹配器的组装**

[uniseg3d/instance_criterion.py:L90-L91](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L90-L91) 用 `TASK_UTILS.build` 同时构建 `matcher` 与 `ov_matcher` 两个匹配器。对照主配置 [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L76-L88](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L76-L88)：

- `matcher`（base 用）：`QueryClassificationCost(0.5) + MaskBCECost(1.0) + MaskDiceCost(1.0)`，topk=1；
- `ov_matcher`（novel 用）：**只有** `MaskBCECost + MaskDiceCost`，topk=1——没有分类代价，因为伪掩码的标签不可信。

**(4) base 拆分与第一阶段匹配**

[uniseg3d/instance_criterion.py:L372-L395](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L372-L395)（`__call__` 主层版本）：先按 `~is_novel` 行过滤出 `base_insts`（`labels_3d`、`sp_masks`、`query_masks` 三者同步过滤），再把预测包成 `InstanceData_(scores=cls_preds, masks=pred_masks)`、GT 包成 `InstanceData_(labels=..., masks=..., query_masks=...)` 喂给 `self.matcher`。注意字段名的细节：GT 侧叫 `labels`（匹配器约定），而数据管线里叫 `labels_3d`，所以这里要手工换名。

**(5) 剩余查询与 novel 二次匹配**

[uniseg3d/instance_criterion.py:L431-L449](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L431-L449)：

- `mask_` 初始全 True，把 `indices[j][0]`（base 已占用的查询下标）置 False；
- `mask_map = torch.nonzero(mask_)[:, 0]` 得到**剩余查询的原始下标表**；
- `ov_inst.query_masks = query_masks[is_novel][:, mask_]` 做了**二维切片**：行过滤到 novel GT，列过滤到剩余查询——允许矩阵被换到剩余坐标系；
- `ov_pred_mask = pred_mask[mask_]` 把预测掩码也换到剩余坐标系。

随后 [L451-L464](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L451-L464) 调 `ov_matcher` 得到剩余空间里的 `ov_indices`，再用 `mask_maps[j][ov_indice[0]]` 一步还原出原始查询下标 `ori_ov_query_indice`。

**(6) 不变量锁死**

[uniseg3d/instance_criterion.py:L474](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L474)：`assert torch.isin(idx_q, ori_ov_query_indice).sum() == 0`——base 命中查询与 novel 命中查询必须不相交。同一断言还出现在逐层版本 [L209](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L209) 和 IoU 分支 [L601](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L601)。由于 novel 匹配天然只发生在剩余查询上，这个断言按构造本不可能触发，它是防止后续改动破坏两阶段逻辑的保险丝。

#### 4.1.4 代码实践

> 本实践是规格中指定的任务：构造 6 个 GT（4 base + 2 novel）、20 个查询的假数据，复刻 `get_layer_loss` 内部的匹配步骤（等效于插桩打印，**无需改动仓库源码**），验证同一查询不会同时命中两路匹配，并核对 `cls_target` 中 `-1` 的位置。

**实践目标**：亲眼看到两阶段匹配的输出与不变量。

**操作步骤**（以下为示例代码，保存为仓库外任意脚本，如 `/tmp/ov_probe.py`）：

```python
import torch
from mmengine.registry import init_default_scope
init_default_scope('mmdet3d')                      # 让 TASK_UTILS.build 能解析注册名

from uniseg3d.instance_criterion import OVInstanceCriterion
from uniseg3d.structures import InstanceData_

torch.manual_seed(0)
NQ, NSP, NB, NN, NCLS = 20, 50, 4, 2, 18           # 20 查询 / 50 超点 / 4 base + 2 novel / 18 类

# --- GT: base 行每列至多一个 True（模拟超点标注），novel 行随机伪掩码 ---
sp_masks = torch.zeros(NB + NN, NSP, dtype=torch.bool)
cols = torch.randperm(NSP)
for i in range(NB):
    sp_masks[i, cols[i*5:(i+1)*5]] = True          # 4 个 base 实例各占 5 个互斥超点
sp_masks[NB:] = torch.rand(NN, NSP) > 0.7          # novel 伪掩码，允许重叠
labels = torch.cat([torch.arange(NB) % NCLS, torch.zeros(NN, dtype=torch.long)])  # novel 标签占位 0
is_novel = torch.zeros(NB + NN, dtype=torch.bool); is_novel[NB:] = True

# --- 模拟 _select_queries: 20 个查询来自随机 20 个超点 → 允许矩阵 ---
query_pts = torch.randperm(NSP)[:NQ]
query_masks = sp_masks[:, query_pts]               # (6, 20)

cls_preds  = [torch.randn(NQ, NCLS + 1)]           # (20, 19)
pred_masks = [torch.randn(NQ, NSP)]
pred_scores = [torch.randn(NQ, 1)]

criterion = OVInstanceCriterion(
    matcher=dict(type='SparseMatcher', costs=[
        dict(type='QueryClassificationCost', weight=0.5),
        dict(type='MaskBCECost', weight=1.0),
        dict(type='MaskDiceCost', weight=1.0)], topk=1),
    ov_matcher=dict(type='SparseMatcher', costs=[
        dict(type='MaskBCECost', weight=1.0),
        dict(type='MaskDiceCost', weight=1.0)], topk=1),
    loss_weight=[0.5, 1.0, 1.0, 0.5, 0.1, 0.1],
    non_object_weight=0.1, num_classes=NCLS,
    fix_dice_loss_weight=True, IoU_loss_weight=0.1,
    IntrinsicPointAndQuery_loss_weight=0.1,
    iter_matcher=True, ratio=0.1, iou_thr=0.,
    pred_iou=False, fix_mean_loss=True)

insts = [InstanceData_(sp_masks=sp_masks, labels_3d=labels,
                       is_novel=is_novel, query_masks=query_masks)]
aux = dict(cls_preds=cls_preds, scores=pred_scores, masks=pred_masks)
print('get_layer_loss 返回:', criterion.get_layer_loss(aux, insts))

# --- 复刻源码 L143-L199 的匹配步骤（等效插桩） ---
base = InstanceData_(labels_3d=labels[~is_novel], sp_masks=sp_masks[~is_novel],
                     query_masks=query_masks[~is_novel])
idx_q, idx_gt = criterion.matcher(
    InstanceData_(scores=cls_preds[0], masks=pred_masks[0]),
    InstanceData_(labels=base.labels_3d, masks=base.sp_masks, query_masks=base.query_masks))
print('阶段1 base 匹配: 查询', idx_q.tolist(), '← GT 行', idx_gt.tolist())

mask_ = torch.ones(NQ, dtype=torch.bool); mask_[idx_q] = False
mask_map = torch.nonzero(mask_)[:, 0]
ov_q, ov_gt = criterion.ov_matcher(
    InstanceData_(masks=pred_masks[0][mask_map]),
    InstanceData_(masks=sp_masks[is_novel], labels=labels[is_novel],
                  query_masks=query_masks[is_novel][:, mask_]))
ori_ov = mask_map[ov_q]
print('阶段2 novel 匹配: 剩余空间', ov_q.tolist(), '← GT 行', ov_gt.tolist(),
      '→ 原始下标', ori_ov.tolist())
assert torch.isin(idx_q, ori_ov).sum() == 0        # 与源码 L209/L474 同一不变量

cls_target = cls_preds[0].new_full((NQ,), NCLS, dtype=torch.long)
cls_target[idx_q]  = base.labels_3d[idx_gt]        # 对应源码 L207
cls_target[ori_ov] = -1                           # 对应源码 L208
print('cls_target =', cls_target.tolist())
```

**运行方式**：在 u1-l2 搭好的 conda 环境里、仓库根目录下执行 `python /tmp/ov_probe.py`（`uniseg3d` 与 `open_clip` 依赖 `PYTHONPATH` 指向仓库根目录，在根目录运行即可）。

**需要观察的现象**：

1. 阶段 1 输出的查询下标个数 ≤ 4（每个 base GT 最多 topk=1 个查询；若某 GT 的超点里恰好一个查询都没采到，允许数为 0，它就匹配不上——这正是 u5-l2 讲过的稀疏匹配退化行为）。
2. 阶段 2 的「剩余空间」下标与「原始下标」不同，且 `ori_ov` 中每个元素都不在 `idx_q` 里（断言通过）。
3. `cls_target` 中 `-1` 恰好出现在 `ori_ov` 的位置，`0..17` 出现在 `idx_q` 的位置，其余全是 18（背景列）。

**预期结果**：断言全部通过，打印的两个查询集合不相交。具体的下标数值依赖随机种子与张量内容，**待本地验证**（脚本固定了 `torch.manual_seed(0)`，重复运行结果应可复现）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ov_matcher` 的代价列表里没有 `QueryClassificationCost`？

**答案**：novel 伪掩码的 `labels_3d` 只是占位 0（数据侧 [transforms_3d.py:L396-L403](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L396-L403) 从未给 novel 行写真实标签），分类代价 `scores[:, labels]` 对它没有意义；所以 novel 路只按掩码亲和度（BCE + Dice）匹配。

**练习 2**：如果删掉「先移除 base 已匹配查询」这一步，直接在全部 20 个查询上做 novel 匹配，会发生什么？

**答案**：同一个查询可能同时匹配一个 base GT 和一个 novel 伪掩码。轻则 [instance_criterion.py:L474](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L474) 的断言立刻失败；即使绕过断言，一个查询只有一个掩码输出，却要同时逼近两个人工/伪真值，梯度互相冲突，`cls_target` 也会在同一位置被 `-1` 覆盖掉 base 标签。

**练习 3**：数据侧 `is_novel` 的长度为什么是 `len(insts) + len(novel_sp_masks) + num_classes + 1`，而不是只有实例行？

**答案**：它与 `gt_sp_masks` 的**总行数**对齐（base 实例行 + 伪掩码行 + 语义行 + unlabeled 行）。这样总调度在 [unified_criterion.py:L36-L40](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L36-L40) 才能用同一个 `[:-n-1]` 切片把 `sp_masks`、`labels_3d`、`is_novel`、`query_masks` 四者同步裁到实例行，无需各自维护独立下标。

---

### 4.2 cls/mask 损失计算与权重

#### 4.2.1 概念说明

两路匹配完成后，损失分三块：

1. **分类损失（只有 base 参与）**：所有查询都要给出分类目标——base 命中的写真实标签，novel 命中的写 `-1`（忽略），其余写背景列。这相当于把「novel 查询是什么类别」这个问题从分类头里**摘出去**，只留给掩码头。
2. **base 掩码损失**：BCE + Dice，只算匹配上的 `(查询, GT)` 对。
3. **novel 掩码损失**：同样 BCE + Dice，但只算 ov 匹配对，且权重被压到 0.1（base 路是 1.0）。

最后按 `loss_weight` 六元组线性组合。三个开关 `fix_dice_loss_weight` / `fix_mean_loss` / （下一模块的）`iter_matcher` 都作用在「逐场景损失如何聚合成一个标量」这一步。

#### 4.2.2 核心流程

```text
cls_target 初始化为全 n_classes (=18, 背景列)
  ├─ cls_target[idx_q]        = base GT 标签        (0..17)
  ├─ cls_target[ori_ov_query] = -1                  (novel 命中, 忽略)
  └─ cross_entropy(cls_pred, cls_target, weight=class_weight, ignore_index=-1)
       class_weight = [1]*18 + [0.1]                (背景降权)

base 掩码:  逐场景 BCE(logits[idx_q], sp_masks[idx_gt]) + dice(...)
novel 掩码:  逐场景 BCE(ov_logits[ov_q], ov_sp_masks[ov_gt]) + dice(...)

聚合:  sum(逐场景) / len(pred_masks)   ← 分母是批内场景数, 不是有匹配的场景数
       fix_mean_loss:  × len(pred_masks)/len(有匹配列表)  → 修正为「只在有匹配的场景上平均」
       fix_dice_loss_weight:  dice 额外 / len(pred_masks) × 4

inst_loss    = 0.5·cls + 1.0·bce + 1.0·dice + 0.5·score   (score 恒为 0)
ov_inst_loss = 0.1·ov_bce + 0.1·ov_dice
```

`fix_mean_loss` 的修正在数学上就是：

\[ L_{\text{mean}} = \frac{\sum_{i} l_i}{B} \cdot \frac{B}{|\{i : l_i \text{存在}\}|} = \frac{\sum_{i} l_i}{|\{i : l_i \text{存在}\}|} \]

即把「被空场景稀释的平均」还原成「有监督场景上的平均」。

#### 4.2.3 源码精读

**(1) 分类目标三步构造**

[uniseg3d/instance_criterion.py:L466-L476](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L466-L476)：

```python
cls_target = cls_pred.new_full((len(cls_pred),), n_classes, dtype=torch.long)  # 全背景
cls_target[idx_q] = inst.labels_3d[idx_gt]        # base 命中 → 真实标签
cls_target[ori_ov_query_indice] = -1              # novel 命中 → 忽略
assert torch.isin(idx_q, ori_ov_query_indice).sum() == 0
cls_losses.append(F.cross_entropy(
    cls_pred, cls_target, cls_pred.new_tensor(self.class_weight), ignore_index=-1))
```

三个关键点：

- `n_classes = cls_pred.shape[1] - 1`（[L469](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L469)）取的是**背景列下标** 18——对照 u4-l4，这一列正是球面分类里全零 `back_token` 原型对应的「无对象」列，两边的语义严格一致。
- `-1` 在 `F.cross_entropy(..., ignore_index=-1)` 下完全不产生梯度：novel 命中的查询在分类头上是「旁观者」，但它们的掩码仍被 ov 路监督。
- `class_weight = [1]*num_classes + [non_object_weight]`（[L92-L93](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L92-L93)），配置里 `non_object_weight=0.1`：绝大多数查询是背景，不降权会让模型学成「什么都说不是物体」。

代码里 [L466](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L466) 上方的注释 `# class loss, note:ov has no cls loss` 是作者自己强调的设计要点。

**(2) base 掩码损失与聚合开关**

[uniseg3d/instance_criterion.py:L479-L506](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L479-L506)：逐场景取 `mask[idx_q]` 与 `inst.sp_masks[idx_gt]` 做 BCE-with-logits 和 Dice；无匹配的场景直接 `continue` 跳过，因此 `mask_bce_losses` 列表可能短于批大小。聚合时 `sum() / len(pred_masks)`（分母是批大小），再由 `fix_mean_loss` 按上面的公式补偿。`score_loss` 在 [L490](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L490) 被硬编码为 0——解码器的 `scores` 输出在实例分支不受监督，物体置信度完全来自球面分类（u4-l4 的 `objectness_flag=False` 结论），所以 `loss_weight[3]=0.5` 实际乘的是零。

**(3) novel 掩码损失与权重压制**

[uniseg3d/instance_criterion.py:L508-L533](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L508-L533) 与 base 路结构完全对称，只是输入换成 `ov_pred_masks`（剩余坐标系下的预测）与 `ov_insts.sp_masks`（伪掩码行）。最终组合在 [L535-L543](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L535-L543)：

- `inst_loss = 0.5·cls + 1.0·bce + 1.0·dice + 0.5·0`
- `ov_inst_loss = 0.1·ov_bce + 0.1·ov_dice`

权重来自配置 [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L92](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L92)：前四项乘 `inst_weight`（=1.0），**后两项是写死的 0.1**——伪掩码噪声大于人工标注，作者的工程选择是把它压到一个低权重，避免噪声监督主导训练。

**(4) 一处值得注意的「双拷贝不一致」**

`get_layer_loss`（aux 层用）与 `__call__`（主层用）是两份近乎复制的代码，但 Dice 聚合有一处细微差别：

- 主层 [L493-L497](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L493-L497)：`bce = Σ/B`，`dice = Σ`，开关后 `dice = 4Σ/B`；
- aux 层 [L229-L233](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L229-L233)：`bce = Σ/B`，`dice = Σ/B`，开关后 `dice = 4Σ/B²`。

批大小 B=1 时两者等价；B>1 时 aux 层的 Dice 聚合系数比主层小 B 倍。阅读源码、复现数值时务必区分这两份拷贝，不要拿主层公式去验 aux 层的数。

#### 4.2.4 代码实践

**实践目标**：直观感受 `cls_target` 三类取值的分布与 `non_object_weight` 的作用。

**操作步骤**（示例代码，接着 4.1.4 的脚本运行）：

```python
# 1) 统计 cls_target 三类取值
vals, cnts = cls_target.unique(return_counts=True)
print(dict(zip(vals.tolist(), cnts.tolist())))    # {18: 背景, 0..17: base, -1: novel}

# 2) 手工复算分类损失，与 criterion 内部对比
import torch.nn.functional as F
w = cls_preds[0].new_tensor([1.0]*NCLS + [0.1])
manual = F.cross_entropy(cls_preds[0], cls_target, w, ignore_index=-1)
print('manual cls_loss =', manual.item())

# 3) 把 non_object_weight 从 0.1 调到 1.0, 重新构建 criterion 再跑第 2) 步
```

**需要观察的现象**：背景列 18 的计数远大于其它取值（20 个查询里通常只有个位数被匹配）；第 2) 步手工值与 4.1.4 中复刻流程算出的分类损失同源；第 3) 步背景权重升高后，交叉熵数值明显变化。

**预期结果**：手工复算与源码路径一致（因为公式与权重完全相同）；权重变化的幅度与背景占比相关。具体数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`cls_target` 里 `-1`、`18`、`0..17` 各自代表什么？被标 `-1` 的查询还有梯度吗？

**答案**：`-1` = 被 novel 匹配占用、分类上被 `ignore_index` 忽略；`18` = 背景列（球面分类的 back_token 列）；`0..17` = base 类别。被标 `-1` 的查询**分类头无梯度，但掩码头仍有梯度**——它的掩码正在 ov 路里被伪掩码监督。

**练习 2**：`fix_mean_loss=True` 解决什么问题？举一个会触发它的场景。

**答案**：`sum()/len(pred_masks)` 的分母是批大小，但无匹配的场景根本没进列表，损失被「空场景」稀释。`fix_mean_loss` 用 `× B/len(列表)` 把平均修正回「只在有匹配的场景上平均」。触发场景：某场景没有任何 base 实例（如空房间），或其超点上恰好没采到查询导致匹配数为 0——该场景 `continue` 跳过。

**练习 3**：为什么 `loss_weight` 的后两项（novel 掩码）是写死的 0.1，而不像前四项那样乘 `inst_weight`？

**答案**：novel 监督来自 SAM3D 伪掩码，可靠性低于人工标注；固定低权重是一种对噪声监督降权的工程手段，也意味着即使调大 `inst_weight` 加强实例学习，novel 路的相对强度也保持不变。

---

### 4.3 aux_outputs 逐层损失累加

#### 4.3.1 概念说明

u4-l3 讲过：解码器迭代预测模式下一次前向产出 7 份预测（6 个解码层 + 1 个最终输出），前 6 份打包在 `pred['aux_outputs']` 里做**深监督**。`OVInstanceCriterion.__call__` 先算主层损失，再对每个 aux 层调用 `get_layer_loss`，把三路损失分别 `+=` 累加——也就是说，最终 loss 字典里的 `inst_loss` 一个键里其实叠了 7 层的量。

深监督的意义：让浅层解码层的预测也直接对齐 GT，梯度不必穿过整个解码器才能回传到浅层，训练更稳、收敛更快。

#### 4.3.2 核心流程

```text
__call__(pred, insts, ...):
  主层三路损失计算 (4.1 + 4.2 的流程)
  IntrinsicPointAndQuery 损失 (依赖主层 base 匹配 indices, 详见 u5-l6)
  if 'aux_outputs' in pred:
      if iter_matcher: indices = None            # ← 注意: 实际上是空操作, 见 4.3.3
      for aux in pred['aux_outputs']:            # 6 层
          loss = get_layer_loss(aux, insts, indices)
          inst_loss += loss[0]; ov_inst_loss += loss[1]; (iou_inst_loss += loss[2])
  return {'inst_loss', 'ov_inst_loss', 'iou_inst_loss'} (+ 可选 'IntrinsicPointAndQuery_loss')
```

#### 4.3.3 源码精读

**(1) 逐层累加循环**

[uniseg3d/instance_criterion.py:L616-L624](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L616-L624)（`pred_iou=True` 路径，主配置开启）遍历 6 个 aux 层，`get_layer_loss` 返回的三元组分别累加；非 `pred_iou` 路径在 [L632-L638](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L632-L638) 只累加两路。累加是裸 `+=`，没有额外的层间权重。

**(2) `iter_matcher` 的真实效果：一处「名字与行为不符」**

配置里 `iter_matcher=True`（[configs/...unified.py:L99](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L99)）。直觉上它的含义是「每个 aux 层重新匹配」，与之相对的用法是把主层 `indices` 传给各层复用。但看代码：

- [L618-L619](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L618-L619)（及非 pred_iou 版 [L633-L634](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L633-L634)）在 `iter_matcher=True` 时把 `indices` 置 `None` 再传入；
- 而 `get_layer_loss` 的函数体在 [L153](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L153) **第一行就执行 `indices = []`**——形参从头到尾没有被读取，每层无论如何都会重新匹配。

结论：在这个 criterion 里，`indices=None` 的赋值是装饰性的；逐层独立重匹配是无条件行为。`iter_matcher` 这个开关在此处改不改都不影响结果。阅读这类从 DETR 系代码继承来的参数时，要以函数体实际读写的变量为准。

**(3) `get_layer_loss` 的返回约定与 IoU 分支的合流**

[uniseg3d/instance_criterion.py:L354-L357](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L354-L357)：`pred_iou=True` 时返回 `(inst_loss, ov_inst_loss, iou_inst_loss)` 三元组，否则两元组。IoU 分支（[L545-L614](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L545-L614)）把 base 与 ov 的匹配对在行维 `torch.cat` 拼起来统一算 `get_iou` 目标分数再做 MSE——这里能看到 base/novel 两路在 IoU 监督上被**平等对待**（第三处不相交断言在 [L601](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L601)）。该分支的完整拆解是 u5-l4 的主题，本讲只记它的存在与合流点。

**(4) 最终返回字典**

[uniseg3d/instance_criterion.py:L626-L630](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L626-L630)：返回 `{'inst_loss', 'ov_inst_loss', 'iou_inst_loss'}`，当传入了点提示预测且 `IntrinsicPointAndQuery_losses` 非空时追加 `'IntrinsicPointAndQuery_loss'`（权重 `IntrinsicPointAndQuery_loss_weight=0.1`）。对照 u5-l1 的结论：实例类 key 共 3 个（外加可选 1 个），每个内部已含 1+6 层累加，这与「主配置一次前向 10 个 loss key」的总账吻合。

#### 4.3.4 代码实践

**实践目标**：验证「`__call__` 返回的 `inst_loss` = 主层 + 6 个 aux 层之和」这一累加语义。

**操作步骤**（示例代码，可接着 4.1.4 的变量写）：

```python
# 1) 构造 7 份输出: 1 主 + 6 aux, 让每层张量可微且数值不同
def make_layer(k):
    g = torch.Generator().manual_seed(100 + k)
    return dict(cls_preds=[torch.randn(NQ, NCLS + 1, generator=g)],
                scores=[torch.randn(NQ, 1, generator=g)],
                masks=[torch.randn(NQ, NSP, generator=g)])

pred = make_layer(0)
for t in pred['cls_preds'] + pred['scores'] + pred['masks']:
    t.requires_grad_(True)
pred['aux_outputs'] = []
for k in range(1, 7):
    layer = make_layer(k)
    for t in layer['cls_preds'] + layer['masks']:
        t.requires_grad_(True)
    pred['aux_outputs'].append(layer)

# 2) 整体前向
out = criterion(pred, insts)                       # pred_iou=False → 两元组路径
print('loss keys:', list(out.keys()))

# 3) 手工分层求和对比
main = criterion.get_layer_loss(pred, insts)       # 主层: pred 本身就是主层 dict
total = [l.detach() for l in main]
for aux in pred['aux_outputs']:
    l = criterion.get_layer_loss(aux, insts)
    total = [a + b.detach() for a, b in zip(total, l)]
print('inst_loss  整体:', out['inst_loss'].item(),  ' 手工累加:', total[0].item())
print('ov_inst_loss 整体:', out['ov_inst_loss'].item(), ' 手工累加:', total[1].item())

# 4) 反向传播, 确认主层与 aux 层张量都拿到梯度
out['inst_loss'] + out['ov_inst_loss']
(out['inst_loss'] + out['ov_inst_loss']).backward()
print('主层 masks.grad 非零:', bool(pred['masks'][0].grad.abs().sum() > 0))
print('aux 层 masks.grad 非零:', bool(pred['aux_outputs'][0]['masks'][0].grad.abs().sum() > 0))
```

**需要观察的现象**：`loss keys` 为 `['inst_loss', 'ov_inst_loss']`（未传点提示，所以没有 `IntrinsicPointAndQuery_loss`）；第 3) 步两个数值在浮点误差内相等；第 4) 步两个梯度检查均为 True。

**预期结果**：手工累加与整体前向严格一致（所有运算确定性，无随机性介入）；aux 层张量梯度非零证明深监督确实把梯度送进了浅层。具体数值**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`iter_matcher=True` 时传给 `get_layer_loss` 的 `indices=None` 有实际效果吗？

**答案**：没有。`get_layer_loss` 函数体第一行 [L153](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L153) 就把 `indices` 重置为空列表，形参从未被读取；逐层重匹配是无条件行为。判断这类问题永远以函数体内实际使用的变量为准。

**练习 2**：最终 loss 字典里 `inst_loss` 这一个键包含了几层损失？为什么看不出层数？

**答案**：7 层（主层 + 6 个 aux 层）。因为 `__call__` 在 [L620-L624](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L620-L624) 用 `+=` 把逐层损失累加进同一个标量后才返回，层信息在 criterion 内部就被折叠掉了。

**练习 3**：`IntrinsicPointAndQuery_loss` 什么时候会出现在返回字典里？

**答案**：仅当调用方传入了 `point_prompt_pred`（即本批次含点提示样本，模型侧调用了 `__call__` 的点提示参数）且至少一个场景算出了有效损失时（[L628-L629](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L628-L629)、[L641-L642](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L641-L642)）。它消费的正是主层 base 匹配的 `indices`——机制细节在 u5-l6 展开。

---

## 5. 综合实践

**任务：把三个模块串起来——一次「消融实验」式的假数据演练。**

沿用 4.1.4 的数据与 criterion，完成下面三组对照，并把结果整理进一张表：

1. **基线**：4 base + 2 novel，跑 `criterion(pred, insts)`，记录 `inst_loss`、`ov_inst_loss`。
2. **去掉 novel**：把 `is_novel` 全部置 `False`（等价于 `merge_ov=False` 的数据），重新组 `insts` 再跑。预期 `ov_inst_loss` 变为 0——因为 `is_novel` 过滤后 novel GT 数为 0，`ov_matcher` 对空 GT 直接返回空匹配（[instance_criterion.py:L761-L762](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L761-L762)），ov 损失落到 `torch.tensor(0.)` 分支（[L268-L270](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L268-L270)）；同时原本被 `-1` 忽略的查询现在可以自由参与 base 匹配。
3. **只留 novel**：把 4 个 base 行也标成 `is_novel=True`。预期 `cls_loss` 对全 `-1`/全背景目标仍可计算，`inst_loss` 只剩掩码项贡献（`idx_q` 为空时 base 掩码损失也走 0 分支），主要损失来自 ov 路。

每组再各做一次 `loss.backward()`，用 `pred['cls_preds'][0].grad` 是否为全零验证「novel 行不产生分类梯度」：第 3 组里分类头梯度应当接近全零（只剩背景类目标产生的少量梯度），而掩码头梯度非零。

**预期结果**：三组的损失数值与梯度模式差异清晰可解释；把「哪一路损失、哪些查询、哪个头有梯度」整理成对照表，你就完整复现了 base/novel 双路监督的行为。具体数值**待本地验证**。

## 6. 本讲小结

- **两阶段匹配**：base GT 用完整代价（分类+BCE+Dice）先匹配；novel 伪掩码在被移除 base 已占查询后的「剩余查询坐标系」里用纯掩码代价的 `ov_matcher` 二次匹配，`mask_map` 负责坐标还原，三处 `assert` 锁死两路查询不相交。
- **分类目标三色**：`cls_target` 中 base 命中写真实标签、novel 命中写 `-1`（`ignore_index` 忽略）、其余写背景列 `n_classes=18`——背景列与球面分类的 back_token 列语义严格对齐。
- **权重不对称**：base 路权重 `[0.5, 1.0, 1.0, 0.5]`（乘 `inst_weight`），novel 路固定 `[0.1, 0.1]`，是对伪掩码噪声的降权；`score_loss` 恒为 0，物体置信度完全来自球面分类。
- **聚合开关**：`fix_mean_loss` 把「被空场景稀释的平均」修正为「有匹配场景上的平均」；`fix_dice_loss_weight` 把 Dice 额外放大 4 倍，且主层与 aux 层两份拷贝的聚合公式存在细微不一致（`4Σ/B` vs `4Σ/B²`）。
- **深监督累加**：7 份预测的损失在 `__call__` 内部裸 `+=` 折叠成单个 key；`iter_matcher=True` 的 `indices=None` 赋值是空操作——`get_layer_loss` 从不读取传入的 `indices`，逐层重匹配无条件发生。
- **IoU 分支合流**：base 与 ov 匹配对被 `cat` 后统一做 IoU 目标的 MSE 监督，是两路结果在损失侧的会合点，细节留待 u5-l4。

## 7. 下一步学习建议

本讲搞定了「实例 GT 的双路监督」，下一讲 **u5-l4（IoU 预测分支）** 将深入本讲只点到为止的 `pred_iou` 分支：`get_iou` 目标分数如何构造、`iou_thr` 如何过滤低质量匹配、以及推理时 `preds_iou` 如何乘进实例分数改变 topk 排序。建议先重读 [uniseg3d/instance_criterion.py:L545-L614](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L545-L614)，再对照 [uniseg3d/uniseg3d.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py) 中 `pred_iou_proj` 的定义。若你对本讲 `IntrinsicPointAndQuery_losses` 那段被跳过的代码（[L397-L429](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L397-L429)）好奇，它属于 u5-l6 的跨任务知识共享主题，届时会结合 `ContrastiveCriterion` 一并展开。

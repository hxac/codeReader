# u5-l5 提示实例损失：点提示与文本提示的一对一监督

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚为什么 `PointPromptInstanceCriterion` 与 `TextPromptInstanceCriterion` 完全不需要匹配器（matcher），匹配如何退化为「恒等映射」。
2. 逐行读懂这两个 criterion 的 `__call__` 与 `get_layer_loss`：cls 损失、mask BCE 损失、Dice 损失如何计算与聚合。
3. 理解 `use_cls_supervise` 开关（`cls_weight`）控制什么、为什么文本分支默认开启监督分类。
4. 理解 aux 层损失的逐层累加方式与 `total_weight=0.1` 的整体缩放作用。
5. 把这两个 criterion 与 `OVInstanceCriterion`（u5-l3）做系统对比，说出异同各自的根源。

## 2. 前置知识

本讲是第 5 单元「统一损失体系」的一环，默认你已读完 u5-l1（损失总调度）、u5-l2（SparseMatcher）、u5-l3（OVInstanceCriterion）与 u4-l6（提示查询注入与结果拆分）。下面把用到的概念快速串一遍。

### 2.1 DETR 范式为什么需要匹配器

在 DETR 类实例分割中，模型输出 \( N \) 条「候选查询」，每条查询预测一个掩码和一组类别分数；但训练集只标注了 \( K \) 个真值实例（GT），且 \( N \gg K \)。于是产生一个分配问题：**哪条查询负责哪个 GT？** 剩下 \( N-K \) 条查询被当作「背景」。u5-l2 讲过，UniSeg3D 的常规实例分支用 `SparseMatcher` 在 `query_masks` 允许的组合里做稀疏 top-k 匹配来完成这个分配。

**提示分支没有这个问题。** 一条点提示（「分割这个点所在的物体」）或一条文本提示（「分割那个红色椅子」）天生就指定了一个 GT 物体：提示查询是按 GT 逐条构造出来的，第 \( j \) 条提示查询对应第 \( j \) 行 GT，不需要任何分配算法。

### 2.2 三种基础损失回顾

- **加权交叉熵（cls loss）**：`F.cross_entropy(cls_pred, cls_target, class_weight)`，逐行（逐查询）做分类监督，目标是一个类别下标向量。
- **带 logits 的 BCE（mask loss）**：`F.binary_cross_entropy_with_logits(pred_mask, tgt_mask)`，对每条查询的超点级掩码逐元素做二分类。
- **Dice loss**：衡量两枚掩码的整体重叠度：

\[ \mathcal{L}_{dice} = 1 - \frac{2\sum_i p_i g_i + 1}{\sum_i p_i + \sum_i g_i + 1} \]

分母加 1 是平滑项；\( p_i \) 是 sigmoid 后的预测概率，\( g_i \in \{0,1\} \) 是真值。这三件套在 u5-l3 已反复出现，本讲的提示分支沿用同样的配方。

### 2.3 提示查询与其 GT 从哪里来

回顾 u4-l6 的链路（[uniseg3d/uniseg3d.py:465-468](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L465-L468)）：

- 批次里存在 `point_prompts` 键时，`add_point_prompt_queries` 把每个提示点所在超点的特征（加类型嵌入）拼到查询序列**尾部**；
- 存在 `text_token` 键时，`add_text_prompt_queries` 把 LangModule 编码的文本嵌入（加类型嵌入）也拼到尾部。

关键在于：这两个函数在拼查询的**同一个循环里**同步拼好了 GT——这正是「一一对应」的源头，4.1.3 会精读。解码后，[uniseg3d/uniseg3d.py:488-491](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L488-L491) 按提示数量把解码输出从尾部拆成「原始输出」与「提示输出」两份 dict（含 `cls_preds`/`masks`/`scores` 与逐层 `aux_outputs`），随后在 [uniseg3d/uniseg3d.py:496](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L496) 一并交给总调度 `ScanNetUnifiedCriterion`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [uniseg3d/point_prompt_instance_criterion.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/point_prompt_instance_criterion.py) | 点提示实例损失：恒等 indices + cls/BCE/Dice，输出 `point_prompt_inst_loss` |
| [uniseg3d/text_prompt_instance_criterion.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/text_prompt_instance_criterion.py) | 文本提示实例损失：与点提示几乎逐行相同，多一个 `use_cls_supervise` 开关，输出 `text_prompt_inst_loss` |
| [uniseg3d/unified_criterion.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py) | 损失总调度：组装提示 GT、按顺序调用五路子损失并合并字典 |
| [uniseg3d/uniseg3d.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py) | 主模型：注入提示查询、拼装提示 GT、拆分提示输出（本讲只看 GT/拆分侧） |
| [uniseg3d/instance_criterion.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py) | `OVInstanceCriterion`，仅用于 4.4 的对比 |
| [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py) | 两个提示 criterion 的实参（权重、开关） |

## 4. 核心概念与源码讲解

### 4.1 恒等 indices：提示查询与 GT 的一一对应

#### 4.1.1 概念说明

「匹配」在这两个 criterion 里的形式是：对批次中第 \( i \) 个场景，构造

\[ indices_i = (\mathbf{q}, \mathbf{q}), \quad \mathbf{q} = (0, 1, \dots, K_i - 1) \]

即查询下标与 GT 下标都是同一个等差序列——第 \( j \) 条提示查询配第 \( j \) 行 GT。这就是**恒等映射（identity mapping）**。它成立的唯一前提是：提示查询、提示标签、提示掩码三者按同一个顺序拼装。这个前提由模型侧的 `add_point_prompt_queries` / `add_text_prompt_queries` 保证（4.1.3 第二段），不是 criterion 自己校验的。

一个直观的比喻：常规分支里查询是「考场上自由作答的考生」，阅卷（损失）前要先决定谁答哪道题（匹配）；提示分支里每条查询是「点名进场、自带题目的考生」，阅卷只需按座位号一一对应。

#### 4.1.2 核心流程

```
__call__(pred, insts):
  1. 逐场景构造恒等 indices：(arange(K_i), arange(K_i))
  2. cls 损失：cls_target 初值全为背景列 n_classes，
     再用 indices 把每一行覆盖成真实标签
     （因 indices 覆盖全部行，背景初值实际从不生效）
  3. mask 损失：pred_mask = masks[arange]，tgt_mask = sp_masks[arange]
     —— 等价于「逐条提示各自算 BCE 与 Dice」
  4. 按 loss_weight 加权求和，累加全部 aux 层，乘 total_weight 返回
```

对比 u5-l3 的 `OVInstanceCriterion`：同样的代码骨架里，第 1 步是「matcher + ov_matcher 两阶段匹配」，第 2 步的 `cls_target` 是「真实标签 / -1 / 背景」三色取值。提示分支把这两处都简化到了极致。

#### 4.1.3 源码精读

**第一处：恒等 indices 的构造**（点提示，[uniseg3d/point_prompt_instance_criterion.py:146-151](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/point_prompt_instance_criterion.py#L146-L151)）：

```python
# match
indices = []
for i in range(len(insts)):
    query_index=torch.tensor([m for m in range(len(insts[i].labels_3d))]).to(insts[i].labels_3d.device)
    labels_index=query_index.clone()
    indices.append((query_index,labels_index))
```

`query_index` 就是 `arange(len(labels_3d))`；`labels_index` 是它的克隆。两行代码替代了整个 `SparseMatcher`。文本分支完全相同，见 [uniseg3d/text_prompt_instance_criterion.py:151-156](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/text_prompt_instance_criterion.py#L151-L156)。

**第二处：一一对应的源头在模型侧**。点提示的查询与 GT 在同一个 `for j` 循环里同步拼装（[uniseg3d/uniseg3d.py:702-716](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L702-L716)）：

```python
for j in range(len(cur_point_prompts)):
    cur_point_prompt_sp_ids = batch_point_prompt_sp_ids[i][j]
    cur_point_prompt_instance_ids = batch_point_prompt_instance_ids[i][j]

    cur_point_prompt_feat = batch_sp_feat[i][cur_point_prompt_sp_ids:cur_point_prompt_sp_ids+1]
    ...
    cur_point_prompt_label_3d = sp_gt_instances[i].labels_3d[cur_point_prompt_instance_ids:cur_point_prompt_instance_ids+1].clone()
    cur_point_prompt_labels_3d_list.append(cur_point_prompt_label_3d)
    ...
    cur_point_prompt_sp_mask = sp_gt_instances[i].sp_masks[cur_point_prompt_instance_ids:cur_point_prompt_instance_ids+1].clone()
    cur_point_prompt_sp_masks_list.append(cur_point_prompt_sp_mask)
```

这段代码为第 \( j \) 条提示同时取三样东西：超点特征（作查询）、`labels_3d` 的一行切片（作标签）、`sp_masks` 的一行切片（作掩码真值）。随后 [uniseg3d/uniseg3d.py:718-730](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L718-L730) 用三个列表的 `torch.cat` 一次性拼出查询、标签、掩码，并把跨任务对齐用的 `point_object_id` 也挂上。文本侧同理：[uniseg3d/uniseg3d.py:670-674](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L670-L674) 把 `label_text`、`gt_text_prompt`、`text_object_id` 按描述逐条对齐——这与 u3-l5 讲过的「text_info 按物体×描述展开」的顺序完全一致。

**第三处：总调度的转接**（[uniseg3d/unified_criterion.py:43-63](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L43-L63)）把 `point_prompt_insts` / `text_prompt_insts` 重新包成干净的 `InstanceData_`（透传 `labels_3d`、`sp_masks`、`point_object_id` / `text_object_id`，条件透传 `point_prompt_distance_norms`），然后在 [uniseg3d/unified_criterion.py:65-74](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L65-L74) 按固定顺序调用五路子损失：

```python
loss = self.inst_criterion(pred, inst_gts, point_prompt_pred=point_prompt_pred, point_prompt_insts=point_prompt_insts)
loss.update(self.sem_criterion(pred, sem_gts))

if point_prompt_pred is not None:
    loss.update(self.point_prompt_inst_criterion(point_prompt_pred, point_prompt_gts))

if text_prompt_pred is not None:
    loss.update(self.text_prompt_inst_criterion(text_prompt_pred, text_prompt_gts))
```

注意两点：① 提示 criterion 只在对应提示存在（`point_prompt_pred is not None`）时被调用，纯几何任务的数据集上它们整体静默；② 第一行把 `point_prompt_pred` 同时喂给了 `inst_criterion`——OV 分支要拿点提示输出做 `IntrinsicPointAndQuery` 蒸馏，这是 u5-l6 的主题。

**第四处：值得注意的「死代码」**。两个文件的 `get_layer_loss` 开头都保留了一段当 `indices is None` 时调用 `self.matcher` 的分支（[uniseg3d/point_prompt_instance_criterion.py:72-83](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/point_prompt_instance_criterion.py#L72-L83)），但 `__init__`（[uniseg3d/point_prompt_instance_criterion.py:50-58](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/point_prompt_instance_criterion.py#L50-L58)）根本没有创建 `self.matcher`——真调用会直接 `AttributeError`。文件顶部的 `linear_sum_assignment` 导入、辅助函数 `_get_src_permutation_idx` 与 `get_iou` 同样从未被走到。这是从 `OVInstanceCriterion` 复制代码后删掉匹配器留下的痕迹，读源码时应能识别出来，而不是误以为这里藏着匈牙利匹配。

#### 4.1.4 代码实践

**实践目标**：验证「恒等 indices ⇒ 提示查询全部是正样本，`cls_target` 就是真实标签」。

**操作步骤**：

1. 在按 u1-l2 搭好的环境（需能 `import torch`、`mmdet3d`）中，于仓库根目录新建脚本 `identity_check.py`（示例代码，非项目原有文件）：

```python
# 示例代码
import torch
import torch.nn.functional as F
from uniseg3d.structures import InstanceData_

torch.manual_seed(0)
N_CLS, N_PROMPT, N_SP = 18, 3, 50

inst = InstanceData_()
inst.labels_3d = torch.randint(0, N_CLS, (N_PROMPT,))
print('labels_3d =', inst.labels_3d.tolist())

cls_pred = torch.randn(N_PROMPT, N_CLS + 1)

# --- 手工复现 criterion 内部的 cls 损失（见 4.2.3 引用的 L154-164）---
indices = (torch.arange(N_PROMPT), torch.arange(N_PROMPT))   # 恒等映射
idx_q, idx_gt = indices
cls_target = cls_pred.new_full((len(cls_pred),), N_CLS, dtype=torch.long)
cls_target[idx_q] = inst.labels_3d[idx_gt]
loss_identity = F.cross_entropy(cls_pred, cls_target, cls_pred.new_tensor([1.] * N_CLS + [0.1]))

# --- 对照组：不用恒等映射，cls_target 全留背景 ---
loss_all_bg = F.cross_entropy(cls_pred, cls_target.new_full((N_PROMPT,), N_CLS),
                              cls_pred.new_tensor([1.] * N_CLS + [0.1]))

print('cls_target =', cls_target.tolist())
print('identity cls loss =', loss_identity.item())
print('all-bg cls loss   =', loss_all_bg.item())
```

2. 运行 `python identity_check.py`。

**需要观察的现象**：打印出的 `cls_target` 与 `labels_3d` 完全相同（没有任何一行等于 18/背景）；两种 `cls_target` 算出的损失值不同。

**预期结果**：因为恒等 indices 恰好覆盖全部 3 行提示查询，`cls_target` 的背景初值被整体覆盖，`class_weight` 里的 `non_object_weight=0.1`（背景类权重）实际乘不到任何样本上。这是「提示查询全是正样本」的直接数值证据。（结论由源码逻辑推出，具体数值待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：若把 [uniseg3d/point_prompt_instance_criterion.py:149](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/point_prompt_instance_criterion.py#L149) 的 `query_index` 改成逆序（`range(len-1, -1, -1)`），训练会发生什么？

**答案**：第 \( j \) 条提示查询会配上第 \( K-1-j \) 行 GT：cls 损失用错标签、BCE/Dice 用错掩码，掩码损失急剧增大，提示分支学到「张冠李戴」的分割。这说明恒等映射的正确性完全依赖模型侧「查询、标签、掩码同序拼装」的约定，criterion 自身不做任何校验。

**练习 2**：为什么这两个 criterion 的 `__init__` 里没有 `TASK_UTILS.build(matcher)`，而 `OVInstanceCriterion` 必须有（[uniseg3d/instance_criterion.py:90-91](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L90-L91)）？

**答案**：匹配器解决的是「候选查询 → GT」的分配问题。提示查询按 GT 逐条构造、一一对应，分配问题不存在，因此无需匹配器；OV 分支的超点查询是「自由候选」，必须先匹配（且 novel 伪掩码还要二次匹配），所以要有 `matcher` 与 `ov_matcher` 两个。

**练习 3**：一个场景没有任何提示（`len(labels_3d)==0`）时，这两个 criterion 返回什么？

**答案**：模型侧拆分函数会让该场景的 `cls_preds[i]` 保持 `None`，cls 循环里被 `continue` 跳过；若整个批次都没有提示，`cls_losses` 为空，走 [uniseg3d/point_prompt_instance_criterion.py:163](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/point_prompt_instance_criterion.py#L163)（文本分支为 [L168](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/text_prompt_instance_criterion.py#L168)）提前返回 `tensor(0.)`——一个**无梯度**的标量，mmengine 聚合总损失时等价于该分支缺位。

### 4.2 cls/mask 损失与 use_cls_supervise 开关

#### 4.2.1 概念说明

恒等映射之后，损失只剩三件事：

1. **cls 损失**：每条提示查询在 19 列（18 个前景类 + 1 个背景列）logits 上做加权交叉熵，目标是该提示所指物体的真实类别；
2. **mask BCE 损失**：每条提示查询预测的超点掩码与 GT 掩码逐元素二分类；
3. **mask Dice 损失**：每条查询预测掩码与 GT 的整体重叠度。

第四项 `score_loss`（objectness 分数监督）在代码里**恒为 0**：`score_losses` 列表从头到尾没有被 append 过任何元素（见 4.2.3 第三段），`loss_weight[3]=0.5` 乘的是 0。提示分支不监督置信度分数，置信度信息由推理阶段的其他通道给出。

两个 criterion 的唯一实质差别是文本分支的 **`use_cls_supervise`** 开关：它把 `cls_weight` 置 1 或 0，直接乘进 cls 项。置 0 时文本提示分支只学「分割出被描述的物体」，不学「报出物体类别」。主配置将其设为 `True`（[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:116](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L116)），即文本查询也要对齐到 CLIP 词表的分类空间——这为 u5-l6 的伪分布蒸馏（用点提示的分类分布监督文本分支）提供了可对齐的 logits。

#### 4.2.2 核心流程

逐场景（batch 内第 \( i \) 个样本）：

```
cls 项:  CE(cls_preds[i], labels_3d, class_weight=[1]*18+[0.1])
mask 项: BCE(masks[i], sp_masks)  与  Dice(masks[i], sp_masks)
聚合:    cls_loss   = batch 内场景均值
         bce_loss   = stack(各场景 BCE).sum() / B
         dice_loss  = stack(各场景 Dice).sum()        ← 注意未除 B，见 4.3
         (fix_dice: dice /= B * 4;  fix_mean: 空场景补偿)
单层总和: w0·cls_weight·cls + w1·bce + w2·dice + w3·score(=0)
```

权重来自配置 [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:101-117](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L101-L117)：两个分支同为 `loss_weight=[0.5, 1.0, 1.0, 0.5]`、`num_classes=18`（`num_instance_classes`，见 [L10](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L10)）、`non_object_weight=0.1`、`fix_dice_loss_weight=True`、`fix_mean_loss=True`、`total_weight=0.1`，文本分支额外 `use_cls_supervise=True`。

#### 4.2.3 源码精读

**cls 损失**（点提示，[uniseg3d/point_prompt_instance_criterion.py:154-164](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/point_prompt_instance_criterion.py#L154-L164)）：

```python
cls_losses = []
for cls_pred, inst, (idx_q, idx_gt) in zip(cls_preds, insts, indices):
    if cls_pred is None: continue
    n_classes = cls_pred.shape[1] - 1
    cls_target = cls_pred.new_full(
        (len(cls_pred),), n_classes, dtype=torch.long)
    cls_target[idx_q] = inst.labels_3d[idx_gt]
    cls_losses.append(F.cross_entropy(
        cls_pred, cls_target, cls_pred.new_tensor(self.class_weight)))
if not cls_losses: return {'point_prompt_inst_loss': torch.tensor(0.).to(insts[0].sp_masks.device)}
cls_loss = torch.mean(torch.stack(cls_losses))
```

先填背景、再覆盖：因为 `idx_q` 覆盖全部行，最终 `cls_target` 就是 `labels_3d` 本身。逐场景算 CE 再取批内均值。

**mask 损失**（[uniseg3d/point_prompt_instance_criterion.py:166-176](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/point_prompt_instance_criterion.py#L166-L176)）：

```python
for mask, score, inst, (idx_q, idx_gt) in zip(pred_masks, pred_scores, insts, indices):
    if mask is None: continue
    if len(inst) == 0:
        continue
    pred_mask = mask[idx_q]
    tgt_mask = inst.sp_masks[idx_gt]
    mask_bce_losses.append(F.binary_cross_entropy_with_logits(
    pred_mask, tgt_mask.float()))
    mask_dice_losses.append(dice_loss(pred_mask, tgt_mask.float()))
```

恒等 indices 下 `mask[idx_q]` 与 `inst.sp_masks[idx_gt]` 行序完全一致——每条提示的预测掩码对着自己的 GT 算 BCE 与 Dice。循环里的 `score` 被 zip 解包却从未使用；同样，`score_losses` 在整个文件里没有任何 append，因此 [L179-182](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/point_prompt_instance_criterion.py#L179-L182) 里 `len(score_losses)` 恒为 0，`score_loss` 恒为 0。

**`use_cls_supervise` 开关**（[uniseg3d/text_prompt_instance_criterion.py:60-63](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/text_prompt_instance_criterion.py#L60-L63)）：

```python
if use_cls_supervise:
    self.cls_weight = 1.
else:
    self.cls_weight = 0.
```

它只在两处生效——主输出求和（[uniseg3d/text_prompt_instance_criterion.py:205-209](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/text_prompt_instance_criterion.py#L205-L209)）与每层 aux 求和（[L137-143](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/text_prompt_instance_criterion.py#L137-L143)）：

```python
loss = (
    self.loss_weight[0] * cls_loss * self.cls_weight +
    self.loss_weight[1] * mask_bce_loss +
    self.loss_weight[2] * mask_dice_loss +
    self.loss_weight[3] * score_loss)
```

点提示分支没有这个开关——它的分类监督恒开启。这符合两个任务的语义：点提示查询的特征直接来自超点（几何侧），其分类头输出可与 GT 稳定对齐；文本分支要不要对齐词表分类空间，则是一个可以开关的设计变量。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：构造 3 条点提示与 3 条文本提示的假 `pred`/`gt`，分别调用两个 criterion，打印两个损失键；再把 `use_cls_supervise` 置 `False`，观察数值与梯度的变化并解释。

**操作步骤**：

1. 在按 u1-l2 搭好的环境中，于仓库根目录新建 `prompt_loss_playground.py`（示例代码，非项目原有文件）：

```python
# 示例代码
import copy
import torch
import torch.nn.functional as F
from uniseg3d.structures import InstanceData_
from uniseg3d.point_prompt_instance_criterion import PointPromptInstanceCriterion
from uniseg3d.text_prompt_instance_criterion import TextPromptInstanceCriterion

torch.manual_seed(0)
N_CLS, N_PROMPT, N_SP = 18, 3, 50   # 与主配置一致的类别数

def make_insts():
    """3 条提示的假 GT：标签 + 每条提示随机圈 15 个超点。"""
    inst = InstanceData_()
    inst.labels_3d = torch.randint(0, N_CLS, (N_PROMPT,))
    sp_masks = torch.zeros(N_PROMPT, N_SP).bool()
    for j in range(N_PROMPT):
        sp_masks[j, torch.randperm(N_SP)[:15]] = True
    inst.sp_masks = sp_masks
    return [inst]                    # batch_size = 1

def make_pred():
    """假预测：主输出 + 2 层 aux（真实解码器有 6 层 aux）。"""
    def one_layer():
        return dict(
            cls_preds=[torch.randn(N_PROMPT, N_CLS + 1, requires_grad=True)],
            scores=[torch.zeros(N_PROMPT)],   # 占位：损失中恒不使用
            masks=[torch.randn(N_PROMPT, N_SP, requires_grad=True)])
    pred = one_layer()
    pred['aux_outputs'] = [one_layer() for _ in range(2)]
    return pred

kw = dict(loss_weight=[0.5, 1.0, 1.0, 0.5], non_object_weight=0.1,
          num_classes=N_CLS, fix_dice_loss_weight=True,
          fix_mean_loss=True, total_weight=0.1)

insts = make_insts()

# ① 点提示与文本提示（use_cls_supervise=True）喂同一份 pred：
#    两者代码路径逐行相同，数值应完全一致
pred = make_pred()
loss_pt  = PointPromptInstanceCriterion(**kw)(pred, insts)['point_prompt_inst_loss']
loss_txt = TextPromptInstanceCriterion(**kw, use_cls_supervise=True)(pred, insts)['text_prompt_inst_loss']
print('point_prompt_inst_loss        =', loss_pt.item())
print('text_prompt_inst_loss (cls=1) =', loss_txt.item())

# ② use_cls_supervise=False：用同一份 pred 的深拷贝，保证可比
pred_off = copy.deepcopy(pred)
loss_off = TextPromptInstanceCriterion(**kw, use_cls_supervise=False)(pred_off, insts)['text_prompt_inst_loss']
print('text_prompt_inst_loss (cls=0) =', loss_off.item())
print('差值 loss(cls=1) - loss(cls=0) =', (loss_txt - loss_off).item())

# ③ 手工复算主层 cls 项：恒等 indices 下 cls_target 就是 labels_3d
manual_cls = F.cross_entropy(pred['cls_preds'][0], insts[0].labels_3d,
                             torch.tensor([1.] * N_CLS + [0.1]))
print('manual main-layer cls loss    =', manual_cls.item(),
      '（差值应等于 total_weight*w0*(主层cls+2层aux cls) = 0.05*三层cls之和）')

# ④ 梯度检查：cls_weight=0 时分类 logits 不应收到任何梯度
loss_off.backward()
print('grad on cls_preds (cls=0):', pred_off['cls_preds'][0].grad)
```

2. 运行 `python prompt_loss_playground.py`。

**需要观察的现象**：

- ① 处两个打印值**完全相等**；
- ② 处 `cls=0` 的损失更小，且差值恰好等于 `total_weight × loss_weight[0] × (主层 cls + 2 层 aux cls)`；
- ③ 处手工交叉熵与 criterion 内部 cls 项同口径；
- ④ 处 `cls_preds` 的梯度为 `None`，而 `masks` 的梯度存在（可用 `pred_off['masks'][0].grad is not None` 补充验证）。

**预期结果**：①②③④ 全部成立。解释：点/文本两个 criterion 除返回键名与 `cls_weight` 外逐行相同（`cls_weight=1` 时数值必然一致）；`use_cls_supervise=False` 把 cls 项从总损失中整项拿掉，数值上少了一份、梯度上切断「文本查询特征 → 分类 logits」这条反传路径，掩码分支与对比分支不受影响。（以上为源码逻辑推得的预期，具体数值待本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：`non_object_weight=0.1` 在提示分支里实际起作用吗？

**答案**：不起作用。它只进入 `class_weight` 的最后一项（背景列权重）；而恒等 indices 覆盖全部提示行、`cls_target` 全部被真实标签覆盖，背景列从不出现在目标里，0.1 永远乘不到任何样本上。它是与 OV 分支共享超参形状时的「陪跑」参数。（若某场景提示数为 0，该场景 `cls_pred` 为 `None` 直接跳过，同样轮不到背景列。）

**练习 2**：把 `use_cls_supervise` 从 `True` 改成 `False`，文本分支的哪些参数不再从 `text_prompt_inst_loss` 收到梯度？

**答案**：文本提示查询特征 → 解码器 `cls_proj`/球面分类原型相似度 → `cls_preds` 这条链路上的所有可训练参数（含 `logit_scale` 侧的通路）都不再从该损失获得梯度；掩码预测链路（`masks`）、以及对比蒸馏分支照常。注意 cls 头仍可能从其他损失（如 u5-l6 的伪分布蒸馏）收到梯度，开关只切断 `text_prompt_inst_loss` 这一路。

**练习 3**：为什么点提示分支不需要、也没设计这个开关？

**答案**：文本提示的输入是自然语言，模型本可只学「定位被描述物体」而不学分类；要不要额外对齐词表分类空间是个开放设计变量，故留了开关做消融。点提示查询的特征直接取自被提示的超点，几何与类别信息天然绑定，分类监督没有歧义，恒开即可——这也体现在配置注释 `use_cls_supervise=True, # Try` 上（[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:116](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L116)）：这是一个实验性选择。

### 4.3 aux 层损失与 total_weight

#### 4.3.1 概念说明

u4-l3 讲过：迭代预测模式下解码器共输出 7 份预测（1 份主输出 + 6 层 `aux_outputs`），实现深监督。提示分支沿用这一机制：主损失算完后，把**同一组恒等 indices** 逐层传给 `get_layer_loss`，各层损失**直接相加**（不取平均、不再加权），最后整体乘 `total_weight=0.1` 一次性缩放：

\[ \mathcal{L} = w_{total}\Big( \mathcal{L}_{main} + \sum_{l=1}^{6} \mathcal{L}_{aux}^{(l)} \Big), \qquad \mathcal{L}^{(l)} = \sum_{k=0}^{3} w_k\, s_{cls}\, \mathcal{L}_k^{(l)} \]

其中 \( s_{cls} \) 即 4.2 的 `cls_weight`（点提示恒为 1），\( w_k = [0.5, 1.0, 1.0, 0.5] \)。

`total_weight` 的语义是「该分支在五路总损失里的整体地位」：0.1 表示提示任务是被常规实例/语义任务「顺带」监督的辅助任务，防止其梯度主导共享骨干与解码器。它与把 `loss_weight` 四项全部乘 0.1 在数值上等价，但把「分支级权重」与「项级权重」分离，调参时互不干扰。注意它**不随** `inst_weight`（[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:15](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L15)）缩放——OV 分支的 `loss_weight` 里乘了 `inst_weight`，提示分支没有。

#### 4.3.2 核心流程

```
__call__ 主输出:
  loss = Σ w_k · L_k(main)
  for aux in pred['aux_outputs']:          # 6 层
      loss += get_layer_loss(aux, insts, indices)   # 复用同一恒等 indices
  return {'xxx_prompt_inst_loss': total_weight * loss}
get_layer_loss 与 __call__ 的差别:
  · indices 由外部传入 ⇒ 跳过死代码 matcher 分支
  · dice 聚合多除一次 B（见下）
  · cls_losses 为空时返回整数 0（tensor + 0 合法）
```

两个聚合开关（继承自 OV 分支的工程细节）：

- `fix_dice_loss_weight=True`：Dice 再除以批内场景数 \( B \)、乘 4——把 Dice 的量级对齐到 BCE 附近；
- `fix_mean_loss=True`：当批次里混入「无提示场景」（其 `cls_pred`/`mask` 为 `None` 被跳过）时，`len(mask_bce_losses) < B`，除法会把损失稀释；该开关乘回 \( B / \text{len}(\cdot) \)，恢复「按场景数平均」的口径。全场景都有提示时它是 1 倍的空操作。

#### 4.3.3 源码精读

**主层与 aux 层的聚合差异**（点提示主层 [uniseg3d/point_prompt_instance_criterion.py:184-195](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/point_prompt_instance_criterion.py#L184-L195)）：

```python
if len(mask_bce_losses):
    mask_bce_loss = torch.stack(mask_bce_losses).sum() / len(pred_masks)
    mask_dice_loss = torch.stack(mask_dice_losses).sum()      # ← 主层：只 sum

    if self.fix_dice_loss_weight:
        mask_dice_loss = mask_dice_loss / len(pred_masks) * 4

    if self.fix_mean_loss:
        mask_bce_loss  = mask_bce_loss * len(pred_masks) \
            / len(mask_bce_losses)
        mask_dice_loss  = mask_dice_loss * len(pred_masks) \
            / len(mask_dice_losses)
```

对照 aux 层（[uniseg3d/point_prompt_instance_criterion.py:116-127](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/point_prompt_instance_criterion.py#L116-L127)）：

```python
mask_dice_loss = torch.stack(mask_dice_losses).sum() / len(pred_masks)   # ← aux 层：先 /B
...
if self.fix_dice_loss_weight:
    mask_dice_loss = mask_dice_loss / len(pred_masks) * 4                # 再 /B*4
```

主层 Dice 最终是 \( 4\sum_i d_i / B \)，每层 aux 是 \( 4\sum_i d_i / B^2 \)——**主层 Dice 恰好是每层 aux 的 \( B \) 倍**。这与 u5-l3 在 `OVInstanceCriterion` 里指出的「主/aux 层 Dice 聚合公式不一致」是同一处历史遗留：文本分支同样如此（主层 [uniseg3d/text_prompt_instance_criterion.py:189-194](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/text_prompt_instance_criterion.py#L189-L194)、aux 层 [L121-126](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/text_prompt_instance_criterion.py#L121-L126)）。batch_size=1 时两者恰好相等，所以小实验里看不出来；batch_size=4 的真实训练里主层 Dice 权重隐含放大了 4 倍。读源码要能发现这类「无声的不一致」，复现实验时才不会困惑。

**aux 累加与 total_weight**（[uniseg3d/point_prompt_instance_criterion.py:200-210](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/point_prompt_instance_criterion.py#L200-L210)）：

```python
loss = (
    self.loss_weight[0] * cls_loss +
    self.loss_weight[1] * mask_bce_loss +
    self.loss_weight[2] * mask_dice_loss +
    self.loss_weight[3] * score_loss)

if 'aux_outputs' in pred:
    for i, aux_outputs in enumerate(pred['aux_outputs']):
        loss += self.get_layer_loss(aux_outputs, insts, indices)

return {'point_prompt_inst_loss': self.total_weight * loss}
```

三个要点：① aux 层**裸累加**（无 `1/num_layers` 平均），7 层叠加使提示分支的有效监督量是单层的 7 倍，`total_weight=0.1` 正好把它压回辅助任务量级；② `get_layer_loss` 复用主层的 `indices`，因此 aux 层同样不会触碰到 4.1.3 提到的死代码分支；③ 某层 `get_layer_loss` 返回整数 0 时（该层无有效 cls），`tensor + 0` 合法，不影响其余层。

**配置侧的 total_weight**（[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:101-117](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L101-L117)）：

```python
point_prompt_inst_criterion=dict(
    type='PointPromptInstanceCriterion',
    loss_weight=[0.5, 1.0, 1.0, 0.5],
    num_classes=num_instance_classes,
    non_object_weight=0.1,
    fix_dice_loss_weight=True,
    fix_mean_loss=True,
    total_weight = 0.1),
text_prompt_inst_criterion=dict(
    type='TextPromptInstanceCriterion',
    loss_weight=[0.5, 1.0, 1.0, 0.5],
    ...
    use_cls_supervise=True, # Try
    total_weight = 0.1),
```

两个分支 `total_weight` 同为 0.1，保持交互式与指代两个提示任务地位对等。

#### 4.3.4 代码实践

**实践目标**：亲手验证「aux 层裸累加」与「主/aux 层 Dice 聚合差异随 batch size 显形」。

**操作步骤**（示例代码，接 4.2.4 的脚本环境）：

```python
# 示例代码
import torch
from uniseg3d.structures import InstanceData_
from uniseg3d.point_prompt_instance_criterion import PointPromptInstanceCriterion

def run(B):
    torch.manual_seed(0)
    insts = []
    for _ in range(B):
        inst = InstanceData_()
        inst.labels_3d = torch.randint(0, 18, (3,))
        sp = torch.zeros(3, 50).bool()
        for j in range(3):
            sp[j, torch.randperm(50)[:15]] = True
        inst.sp_masks = sp
        insts.append(inst)

    def one_layer():
        return dict(cls_preds=[torch.randn(3, 19) for _ in range(B)],
                    scores=[torch.zeros(3) for _ in range(B)],
                    masks=[torch.randn(3, 50) for _ in range(B)])
    pred = one_layer()
    pred['aux_outputs'] = [one_layer() for _ in range(2)]

    crit = PointPromptInstanceCriterion(loss_weight=[0.5, 1.0, 1.0, 0.5],
                                        non_object_weight=0.1, num_classes=18,
                                        fix_dice_loss_weight=True,
                                        fix_mean_loss=True, total_weight=1.0)  # 关掉缩放便于观察
    total = crit(pred, insts)['point_prompt_inst_loss']
    only_main = crit({'cls_preds': pred['cls_preds'], 'scores': pred['scores'],
                      'masks': pred['masks']}, insts)['point_prompt_inst_loss']  # 去掉 aux
    print(f'B={B}: total={total.item():.4f}  main={only_main.item():.4f}  '
          f'aux合计={total.item()-only_main.item():.4f}')

run(1)
run(4)
```

2. 运行并对比两组输出。

**需要观察的现象**：`total > main`（aux 是加项）；`B=4` 时 `aux合计/main` 的比值明显大于 `B=1` 时——因为主层 Dice 比每层 aux 大 \( B \) 倍，\( B \) 越大主层占比越高。

**预期结果**：`B=1` 时主层与每层 aux 结构等价；`B=4` 时主层 Dice 项隐含放大 4 倍，主层在总损失中的权重上升。此差异完全来自 [L186](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/point_prompt_instance_criterion.py#L186)（主层 `.sum()` 不除 B）与 [L123](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/point_prompt_instance_criterion.py#L123)（aux 层先除 B）的一字之差。（具体数值待本地验证。）

#### 4.3.5 小练习与答案

**练习 1**：把 `total_weight` 从 0.1 改成 0，模型还能训练吗？会发生什么？

**答案**：能训练。损失值变为 0 且无有效梯度贡献（`0 * loss` 的反传梯度为 0），提示分支等价于被整体摘除——交互式/指代任务的性能会崩掉，但常规全景/语义/实例分支不受影响，因为五路子损失彼此独立合并进总字典。这也是做「去掉提示监督」消融实验的最小改动点。

**练习 2**：7 层裸累加 + `total_weight=0.1`，与「7 层取平均 + `total_weight=0.7`」是否等价？

**答案**：对最终损失值与梯度方向等价（都是各层的线性组合，系数同为 0.1/层）。但两者语义不同：前者把「深监督强度」与「分支权重」耦合在 total_weight 一个旋钮里；后者拆开表达。复现论文或调参时应意识到有效单层系数是 \( 0.1 \times w_k \)。

**练习 3**：`get_layer_loss` 在什么情况下返回整数 0 而不是 tensor？

**答案**：当传入该层的所有场景 `cls_pred` 均为 `None`（[uniseg3d/point_prompt_instance_criterion.py:94](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/point_prompt_instance_criterion.py#L94)）或 `cls_losses` 为空（[L99](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/point_prompt_instance_criterion.py#L99)）时返回 Python 整数 0。调用处 `loss += ...` 对 `tensor + int` 合法，但若整批都为空，主入口早已在 [L163](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/point_prompt_instance_criterion.py#L163) 提前返回，不会走到 aux 循环。

### 4.4 两个提示 criterion 与 OVInstanceCriterion 对比

#### 4.4.1 概念说明

把 u5-l3 的 `OVInstanceCriterion` 与本讲两个 criterion 并排看，能提炼出 DETR 类损失设计的一个通用骨架：**「匹配 → 构造 cls_target → 逐对算 cls/BCE/Dice → aux 累加」**。三个 criterion 共享这个骨架，差异全部集中在「匹配」与「cls_target 构造」两步，而这两步的差异根源只有一个：**查询的来源不同**。

- 超点查询：从超点特征采样而来，是「自由候选」，不知道自己该属于谁 → 需要匹配器 + 三色 cls_target；
- 提示查询：按 GT 逐条构造，「自带答案」 → 恒等映射 + 单色 cls_target。

#### 4.4.2 核心流程（对照表）

| 维度 | OVInstanceCriterion（u5-l3） | Point/TextPromptInstanceCriterion（本讲） |
| --- | --- | --- |
| 查询来源 | 超点特征采样（自由候选） | 点提示超点特征 / 文本嵌入（按 GT 构造） |
| 匹配器 | `matcher` + `ov_matcher` 两阶段（[instance_criterion.py:90-91](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L90-L91)） | 无；恒等 indices（4.1.3） |
| cls_target 取值 | 三色：base 命中→真实标签；novel 命中→ -1（ignore）；未命中→背景 18 | 单色：每条提示→真实标签；背景列从不出现 |
| ignore_index | 需要（novel 命中写 -1，[instance_criterion.py:472-476](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L472-L476)） | 不需要 |
| 监督项 | cls + BCE + Dice + score(恒0) + IoU + IntrinsicPointAndQuery（6 元 loss_weight） | cls + BCE + Dice + score(恒0)（4 元 loss_weight） |
| base/novel 双路 | 有（`is_novel` 拆分，novel 掩码权重 0.1） | 无（提示只指向真实标注物体） |
| aux 累加 | 逐层裸累加，折叠进 `inst_loss`/`ov_inst_loss`/`iou_inst_loss` | 逐层裸累加，单独成键 |
| 输出 loss key | `inst_loss`、`ov_inst_loss`、`iou_inst_loss`（多键） | `point_prompt_inst_loss` / `text_prompt_inst_loss`（各一键） |
| 分支级权重 | 烧进 `loss_weight`（乘 `inst_weight`） | `total_weight=0.1` 统一缩放，不随 `inst_weight` |
| 特有开关 | `iter_matcher`、`iou_thr`、`ratio` 等 | 文本分支 `use_cls_supervise` |

#### 4.4.3 源码精读

**OV 侧的三色 cls_target**（[uniseg3d/instance_criterion.py:472-476](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L472-L476)）：

```python
cls_target[idx_q] = inst.labels_3d[idx_gt]
cls_target[ori_ov_query_indice] = -1
cls_losses.append(F.cross_entropy(
    cls_pred, cls_target, cls_pred.new_tensor(self.class_weight), ignore_index=-1))
```

对照 4.2.3 的提示版本：同样的 `cls_target[idx_q] = ...` 首行，OV 多出「novel 命中写 -1、交叉熵忽略」两步——因为 novel 伪掩码只有形状没有可信类别；提示分支没有 novel 概念，也就没有 -1。**同为三行代码的差别，背后是「自由候选需要宽容错配」与「指定候选必然正配」的本质不同。**

**GT 预处理差异**：OV 消费的是总调度切出的 `inst_gts`（`sp_masks[:-n-1]` 等，见 [uniseg3d/unified_criterion.py:35-41](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L35-L41)，含 SAM3D 伪掩码行与 `is_novel`）；提示 criterion 消费的是模型侧直接拼好的整行 GT（[uniseg3d/unified_criterion.py:43-63](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L43-L63)），不需要任何切片——提示 GT 本来就只含「被指向的实例行」。

**跨分支耦合点**：[uniseg3d/unified_criterion.py:65](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L65) 把 `point_prompt_pred` 也传给了 `inst_criterion`——提示输出不只喂自己的 criterion，还被常规分支用作蒸馏的老师（`IntrinsicPointAndQuery`，权重 0.1）；[L74](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L74) 的对比损失同时消费两路提示的 `contras_embeds` 与 `object_id`。本讲的「一对一损失」只是提示查询参与的损失中的第一路。

#### 4.4.4 代码实践

**实践目标**：从配置文件出发，核对三个 criterion 的实参差异表。

**操作步骤**：

1. 打开 [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:74-117](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L74-L117)，把 `inst_criterion`、`point_prompt_inst_criterion`、`text_prompt_inst_criterion` 三段的每个键抄成三列。
2. 用 `--cfg-options` 思想做思想实验（不必真跑训练）：若把 `model.criterion.point_prompt_inst_criterion.total_weight` 改为 1.0，训练日志里哪个 loss key 的数值会变大？哪个任务受影响最大？
3. （可选，源码阅读型）在 [uniseg3d/unified_criterion.py:65-74](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L65-L74) 上为五路子损失标注它们各自消费 `pred`（原始输出）还是 `point_prompt_pred` / `text_prompt_pred`（提示输出）。

**需要观察的现象**：配置中 OV 分支多了 `matcher`/`ov_matcher`/`pred_iou`/`ratio`/`iou_thr`/`iter_matcher` 等键；两个提示分支的键集合完全一致（文本多 `use_cls_supervise`）。

**预期结果**：`total_weight=1.0` 时 `point_prompt_inst_loss` 数值放大 10 倍，交互式分割（Point Prompt AP）受影响最大；由于提示查询与原始查询共享骨干与解码器，过强的提示梯度也可能拖累常规任务——这正是辅助任务权重的权衡。第 3 步的标注结果：`inst_criterion` 同时消费原始输出与点提示输出；`sem_criterion` 只消费原始输出；两个提示 criterion 各消费自己的提示输出；`contrastive_criterion` 消费全部三路。（思想实验结论，无需 GPU，待用真实日志验证。）

#### 4.4.5 小练习与答案

**练习 1**：为什么 OV 需要 `ignore_index=-1` 而提示分支不需要？

**答案**：OV 的查询里有「匹配到 novel 伪掩码」的行——它们该受掩码监督但类别不可信，只能写 -1 让交叉熵跳过。提示分支每条查询都有确定的真实类别，不存在「要掩码不要类别」的行，自然不需要 ignore 机制。

**练习 2**：如果想让提示分支也输出 IoU 预测（u5-l4 的机制），应该改哪里？

**答案**：提示输出的 dict 里已经有逐层 `contras_embeds`（拆分函数透传，见 [uniseg3d/uniseg3d.py:594-599](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L594-L599) 的键清单），可以仿照 `get_pred_iou` 给提示输出加 `preds_iou`，再在两个 criterion 里仿照 `get_iou`（恒等配对让「预测掩码 vs GT 掩码」的 IoU 一一可算）加一项 MSE。改动点集中在 `uniseg3d.py` 的拆分/前向与两个 criterion 的 `__call__`，不需要动匹配逻辑——因为根本没有匹配。

**练习 3**：五路子损失中，哪几路的「匹配」是显式算法、哪几路是退化的？

**答案**：`inst_criterion`（含 base 的 `SparseMatcher` 与 novel 的 `ov_matcher`）是显式匹配；两个提示 criterion 是退化的恒等映射；`sem_criterion`（逐超点交叉熵）与 `contrastive_criterion`（按 `object_id` 对齐）根本不存在查询-GT 分配问题。一个批次里三种范式同时出现，正是 UniSeg3D「统一但异构」的损失设计的缩影。

## 5. 综合实践

**任务：用假数据复现提示分支的完整损失链路，并做一次三组消融的思想设计。**

1. **复现链路**：把 4.2.4 的脚本扩展成 batch_size=2（其中一个场景放 3 条点提示、另一个放 2 条点提示 + 1 条文本提示），按 [uniseg3d/unified_criterion.py:43-63](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L43-L63) 的方式手工组装 `point_prompt_gts` / `text_prompt_gts`，然后分别调用两个 criterion，确认批次内提示数不同的场景能正确聚合（这里 `fix_mean_loss` 会开始起作用，思考它补偿了什么）。
2. **对照日志**：在有真实训练环境的机器上，用 `./tools/dist_train.sh configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py 1` 跑几十个 iter，在日志里找到 `point_prompt_inst_loss` 与 `text_prompt_inst_loss`（u5-l1 列出的 10 个 loss key 中的两个），记录初值量级，与第 1 步假数据的量级对照。
3. **消融设计**：写出三组实验的预期并说明理由（待本地验证）：
   - A 组：`total_weight` 0.1 → 0；
   - B 组：`use_cls_supervise` True → False；
   - C 组：把主层 Dice 聚合改成与 aux 一致（`.sum() / len(pred_masks)`）。
   分别预测：交互式 AP、指代分割指标、其余四任务指标各自会升/降/不变，并指出判断依据来自本讲哪一段源码。
4. **边界检验**：把一个场景的提示数设为 0（模拟无提示场景混入批次），验证 4.1.5 练习 3 与 4.3.2 `fix_mean_loss` 的论述：该场景被跳过、损失不报错、量级被补偿回「按场景平均」口径。

## 6. 本讲小结

- 提示查询按 GT 逐条构造，查询、标签、掩码在模型侧同一个循环里同序拼装，因此两个提示 criterion 用**恒等 indices**（`arange` 对 `arange`）取代了整个匹配器，`self.matcher` 分支是从 `OVInstanceCriterion` 复制来的死代码。
- 恒等配对使 `cls_target` 被真实标签**全覆盖**：背景列与 `non_object_weight` 从不生效，cls 损失就是「逐条提示的普通分类交叉熵」；mask 损失就是「逐条提示的 BCE + Dice」；`score_loss` 因 `score_losses` 永远为空而恒为 0。
- 两个 criterion 代码逐行相同，唯一实质差别是文本分支的 `use_cls_supervise`（`cls_weight` 0/1 开关，配置默认 True）：关掉即切断文本查询到分类 logits 的监督路径，掩码与对比分支不受影响。
- 7 份预测（主 + 6 层 aux）**裸累加**后乘 `total_weight=0.1` 统一缩放；`fix_dice_loss_weight` 与 `fix_mean_loss` 是从 OV 分支继承的聚合修正；主层与 aux 层的 Dice 聚合相差一个因子 \( B \)，是与 u5-l3 同款的历史遗留。
- 与 `OVInstanceCriterion` 的对比揭示了通用骨架「匹配 → cls_target → cls/BCE/Dice → aux 累加」：差异全部源自查询来源（自由候选 vs 指定候选），体现在匹配器有无与 cls_target 单色/三色上。
- 提示输出不只喂自己的 criterion：`unified_criterion.py` 还把 `point_prompt_pred` 喂给 `inst_criterion` 做蒸馏、把两路提示嵌入喂给对比损失——这是 u5-l6 的入口。

## 7. 下一步学习建议

下一讲 **u5-l6 跨任务知识共享：对比学习、排序与伪分布蒸馏** 将顺着本讲结尾的耦合点展开：`ContrastiveCriterion` 如何用 `point_object_id` / `text_object_id`（本讲 GT 里一直透传的那把「钥匙」）构造文本-点提示正样本对做对称 InfoNCE 与 MarginRanking 排序，`OVInstanceCriterion` 里的 `IntrinsicPointAndQuery` 如何把点提示掩码当伪标签蒸馏给常规查询，以及伪分布损失如何用点提示的分类分布监督文本分支（届时回看 `use_cls_supervise=True` 的动机会更完整）。建议先重读 [uniseg3d/unified_criterion.py:65-74](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L65-L74) 的五路分发顺序，再进入 [uniseg3d/contrastive_criterion.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/contrastive_criterion.py)。

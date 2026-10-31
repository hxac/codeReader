# 损失总调度 ScanNetUnifiedCriterion 与语义损失

## 1. 本讲目标

学完本讲，你应该能够：

1. **看懂 `gt_sp_masks` 的行布局如何在损失侧被一刀切开**：前段（实例 + 伪掩码）喂给实例类损失，后 `n+1` 行（语义 one-hot，含 stuff 类与 unlabeled 兜底行）喂给语义损失，`labels_3d` 与 `is_novel` 同步行切。
2. **说清 `query_masks` 的来龙去脉**：它为什么是 `sp_masks` 的「列视图」，查询随机采样后为什么要同步切列，以及它在五个子损失之间如何传递。
3. **列出一次训练前向产生的全部 loss key**：共 10 个（`inst_loss`、`ov_inst_loss`、`iou_inst_loss`、`IntrinsicPointAndQuery_loss`、`seg_loss`、`point_prompt_inst_loss`、`text_prompt_inst_loss`、`contrastive_loss`、`rank_loss`、`pseudo_ditribute_loss`），并知道每个 key 由哪个子损失产出、带什么权重。
4. **逐行读懂 `ScanNetSemanticCriterion`** 的超点级交叉熵，特别是「切掉预测最后一列 + `ignore_index` 忽略 unlabeled」这一对配合动作。

本讲是第 5 单元（训练机制）的第一讲：我们只站在**总调度**的视角看「一份 GT 如何分发、一份 loss 字典如何合并」，各子损失内部的匹配与蒸馏机制留给后续讲义（u5-l2 ~ u5-l6）逐个拆解。

## 2. 前置知识

### 2.1 交叉熵损失回顾

多分类交叉熵衡量预测分布与真实分布的差异。对单个样本，设预测 logits 经 softmax 得到概率 \( p = (p_0, p_1, \dots, p_{C-1}) \)，真实类别为 \( y \)，则：

\[
\mathcal{L}_{ce} = -\log p_y
\]

PyTorch 的 `F.cross_entropy(input, target)` 中，`input` 是形状 `(N, C)` 的 logits，`target` 是形状 `(N,)` 的类别下标。若某些位置的真实标签是「不可信/不参与训练」的（比如未标注点），可用 `ignore_index` 把它们排除在平均之外——这是本讲语义损失的关键机制。

### 2.2 本讲要继承的前几讲结论

| 来自 | 结论 | 本讲如何使用 |
|---|---|---|
| u3-l3 | `AddSuperPointAnnotations` 产出 `gt_sp_masks`：行 = [K 个 base 实例行 \| M 个 SAM3D 伪掩码行 \| 21 个语义行]，每列至多一个 True；`gt_labels_3d`、`is_novel` 与总行数同长 | 本讲的核心输入，损失侧按 `[:-n-1]` / `[-n-1:]` 切开 |
| u3-l2 / u4-l6 | `InstanceData_` 是放宽字段等长断言的实例容器 | 总调度用它重组各路 GT |
| u4-l1 | `loss()` 前向中 `_select_queries` 按 `query_thr=0.5` 随机保留 50%~100% 的超点查询（上限 3500） | 被丢弃的查询列需要从 GT 中同步裁掉，这就是 `query_masks` |
| u4-l3 / u4-l4 | 解码器迭代预测：主输出 + 6 层 `aux_outputs` 深监督；`sem_preds` 只在最后一层计算一次 | 解释为什么 `inst_loss` 是 7 份累加、`seg_loss` 只有 1 份 |
| u2-l1 / u2-l2 | 配置字符串经 `MODELS.build` 递归实例化；criterion 也是 model 配置的一个子树 | 五个子损失由总调度的 `__init__` 逐一 build |

### 2.3 「损失调度器」是什么

在 mmengine 体系里，损失不必是模型 `forward` 里的几行代码，也可以像搭积木一样做成**可调用对象**（callable）：`__init__` 里构建子损失，`__call__(pred, gt)` 里计算并返回一个 `{loss_name: tensor}` 字典。Runner 的训练循环拿到这个字典后，把所有 value 求和当作总损失反传，key 则原样打进训练日志。**所以「一次前向有哪些 loss key」是直接可以从日志里核对的事实**——这也是本讲实践任务的验收标准。

## 3. 本讲源码地图

| 文件 | 行数 | 在本讲中的角色 |
|---|---|---|
| [uniseg3d/unified_criterion.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L1-L75) | 75 | **主角**：`ScanNetUnifiedCriterion`，GT 拆分 + 五路损失调度 |
| [uniseg3d/semantic_criterion.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/semantic_criterion.py#L1-L44) | 44 | **主角**：`ScanNetSemanticCriterion`，超点级交叉熵 |
| [uniseg3d/transforms_3d.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L336-L432) | — | 行布局的**生产端**（u3-l3 已精读，本讲只回看关键行） |
| [uniseg3d/uniseg3d.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L226-L256) | — | `query_masks` 的**生产端**（`_select_queries`）与总调度的**调用点**（`loss()` 末尾） |
| [uniseg3d/instance_criterion.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L361-L395) | — | 子损失之一 `OVInstanceCriterion`：本讲只看它的签名、base/novel 拆分和返回 key（内部机制在 u5-l2 / u5-l3） |
| [uniseg3d/point_prompt_instance_criterion.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/point_prompt_instance_criterion.py#L141-L210) | — | 子损失之一：点提示实例损失（u5-l5 精读） |
| [uniseg3d/text_prompt_instance_criterion.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/text_prompt_instance_criterion.py#L146-L215) | — | 子损失之一：文本提示实例损失（u5-l5 精读） |
| [uniseg3d/contrastive_criterion.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/contrastive_criterion.py#L40-L115) | — | 子损失之一：跨任务对比损失（u5-l6 精读） |
| [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L67-L124) | — | 五个子损失的全部权重参数 |

## 4. 核心概念与源码讲解

### 4.1 模块一：`__call__` 中的 GT 拆分逻辑

#### 4.1.1 概念说明

UniSeg3D 的六类任务共用一次前向，但不同任务需要**不同形态的监督信号**：

- 实例类任务（全景/实例/开放词汇）需要「每个 GT 实例一条掩码行 + 类别标签」；
- 语义任务需要「每个超点属于哪个语义类」的逐列类别标签；
- 交互式/指代任务需要与提示一一对应的小份 GT。

而数据管线（u3-l3）只产出**一张**大布尔矩阵 `gt_sp_masks`（连同同长的 `gt_labels_3d`、`is_novel`）。于是总调度 `ScanNetUnifiedCriterion` 的第一职责就是**拆分发料**：把这张矩阵按行切开，重组出三份（或五份）GT 视图，分别交给对应子损失。

为什么不干脆在数据管线里直接产出多份 GT？因为行布局天然共享同一套超点列（`scatter_mean` 多数投票只算一次），拆分放到损失侧做，数据侧只需存一份，批处理与提示注入（u4-l6）也能统一操作。

#### 4.1.2 核心流程

先回顾行布局（u3-l3 的结论，本讲直接使用）。设一个场景有 \( K \) 个 base 实例、\( M \) 个 SAM3D 伪掩码、\( n = 20 \) 个语义类、超点数为 \( S \)，则：

\[
\underbrace{\text{gt\_sp\_masks}}_{(K+M+n+1)\ \times\ S}
= \left[
\underbrace{I_1,\dots,I_K}_{\text{base 实例行}}\ ;\ 
\underbrace{P_1,\dots,P_M}_{\text{SAM3D 伪掩码行}}\ ;\ 
\underbrace{C_0,C_1,\dots,C_{19},U}_{\text{语义 one-hot 行（末行 } U \text{ 为 unlabeled 兜底）}}
\right]
\]

其中语义 21 行 = wall（id 0）+ floor（id 1）+ 18 个 things 类（id 2~19）+ unlabeled 兜底行。注意**实例部分不含 stuff 类**——u3-l3 里 wall/floor 与未标注点在进入 one-hot 前就被置 -1 剔除了。

总调度的拆分只依赖一个数字 \( n+1 = 21 \)（拆分点固定在倒数第 21 行）：

```text
n = num_semantic_classes = 20

对批次里每个场景 i：
    # 语义视图：后 21 行
    sem_gt.sp_masks  = query_masks[-21:, :]   （有 query_masks 时）
                     或 sp_masks[-21:, :]     （无 query_masks 时）

    # 实例视图：前 K+M 行（= 去掉后 21 行）
    inst_gt.sp_masks    = sp_masks[:-21, :]
    inst_gt.labels_3d   = labels_3d[:-21]
    inst_gt.is_novel    = is_novel[:-21]
    inst_gt.query_masks = query_masks[:-21, :]  （可选）

    # 点提示视图 / 文本提示视图：从提示专属 GT 中透传字段
```

一句话：**行方向一刀两断（`[:-n-1]` 与 `[-n-1:]`），列方向按存活查询裁剪（`query_masks`）**。`labels_3d` 与 `is_novel` 是「逐行」属性，所以与 `sp_masks` 用同一个行切片保持对齐——实例视图里的第 r 行掩码，正好对应第 r 个标签、第 r 个 novel 标记。

#### 4.1.3 源码精读

**总调度的构造：五个子损失全部经注册表构建。**

[uniseg3d/unified_criterion.py:6-16](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L6-L16) 中，`ScanNetUnifiedCriterion` 自身注册进 `MODELS`，`__init__` 接收 5 个子损失的配置 dict 并逐一 `MODELS.build`——这就是 u2-l2 精读过的 `criterion=dict(...)` 配置子树落地成对象图的地方。注意 `num_semantic_classes` 是唯一被总调度自己持有的数字，它就是拆分点。

**入口签名：五路输入。**

[uniseg3d/unified_criterion.py:18-20](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L18-L20) 中 `__call__` 的参数正好对应 u4-l6 讲过的「解码输出拆分」：`pred` 是原始查询的输出字典，`point_prompt_pred` / `text_prompt_pred` 是两路提示查询的输出（无对应提示键时为 `None`），`insts` / `point_prompt_insts` / `text_prompt_insts` 是三路 GT。

**语义视图的切取：优先 `query_masks`。**

[uniseg3d/unified_criterion.py:27-33](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L27-L33) 对批次内每个场景构造 `sem_gt`：若 GT 里带了 `query_masks`（即发生过查询采样），就取 `query_masks[-n - 1:, :]` 的后 21 行；否则退回 `sp_masks[-n - 1:, :]`。语义视图**只需要掩码**，不需要标签——语义标签将由语义损失在运行时对 one-hot 做 `argmax` 现算（见 4.4）。

**实例视图的切取：三个逐行属性同步切片。**

[uniseg3d/unified_criterion.py:35-41](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L35-L41) 把 `sp_masks`、`labels_3d`、`is_novel` 都切 `[:-n-1]`，得到「K 个 base 实例行 + M 个伪掩码行」的完整实例视图。`is_novel` 顺带传下去，供 `OVInstanceCriterion` 在内部再按 novel 标记二次拆分 base/novel（见 4.3 与 u5-l3）。

**两路提示视图：字段透传 + 重组。**

[uniseg3d/unified_criterion.py:43-53](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L43-L53) 重组点提示 GT：`sp_masks`、`labels_3d`、`point_object_id` 三个主字段必备；`point_prompt_distance_norms`（u3-l4 讲过的点离质心距离，供对比损失加权）与 `query_masks` 是可选字段，存在才透传。[uniseg3d/unified_criterion.py:55-63](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L55-L63) 同样地重组文本提示 GT，关键字段换成 `text_object_id`——这两个 `*_object_id` 是 u3-l4/u3-l5 讲过的「物体 id 对齐钥匙」，将在对比损失里配对（u5-l6）。

**行布局的生产端回顾（三处关键行）。**

- [uniseg3d/transforms_3d.py:351-353](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L351-L353)：语义为 unlabeled（`== num_classes`）或 stuff 类的点，实例 id 置 -1——这就是实例部分不含 stuff、语义部分才含 stuff 的原因。
- [uniseg3d/transforms_3d.py:384-390](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L384-L390)：`merge_ov=True` 时把 SAM3D 伪掩码行拼在 base 实例行之后，并把 `is_novel` 对应位置标 1——所以「实例段 = base + novel」是**连续两段**，总调度无需知道 K 和 M 各是多少，一刀 `[:-21]` 全拿走。
- [uniseg3d/transforms_3d.py:411-432](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L411-L432)：语义 one-hot 21 行经 `scatter_mean > 0.5` 多数投票生成，[第 421 行](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L421) 把「没有任何语义行胜出」的列兜底成 unlabeled，保证每列至少一个 True；[第 430-432 行](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L430-L432) `vstack` 拼出完整 `gt_sp_masks`。注意 [第 425-428 行](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L425-L428) 的 `merge_non_stuff_cls=True` 分支会把语义压成 3 行——训练配置显式设了 `merge_non_stuff_cls=False`（见 [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:172-176](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L172-L176)），语义段才恰好是 \( n+1 = 21 \) 行，与总调度的 `[-n-1:]` 切分公式**严格配套**。

#### 4.1.4 代码实践

**实践目标**：不跑模型，单独验证「行布局 + 拆分公式」的几何关系，直观看到三个视图的行列形状。

**操作步骤**：

1. 把下面的脚本保存为 `uniseg3d-tutorial/playground_u5_l1_split.py`（示例代码，非项目原有文件）：

```python
# 示例代码：验证 gt_sp_masks 的行布局与总调度的拆分公式
import torch

K, M, n, S = 3, 2, 20, 50          # base 实例 / 伪掩码 / 语义类 / 超点数
rows = K + M + n + 1               # 26 行

gt_sp_masks = torch.zeros(rows, S, dtype=torch.bool)
# 实例段 + 伪掩码段：前 5 行各圈 5 个互斥超点
for r in range(K + M):
    gt_sp_masks[r, 5 * r:5 * r + 5] = True
# 语义段：前 30 个超点随机给 0..19 类，后 20 个超点给 unlabeled
sem_ids = torch.randint(0, n, (30,))
gt_sp_masks[K + M + sem_ids, torch.arange(30)] = True
gt_sp_masks[-1, 30:] = True

gt_labels_3d = torch.zeros(rows, dtype=torch.long)   # 实例行填类别，其余占位 0
gt_labels_3d[:K] = torch.tensor([3, 7, 15])
is_novel = torch.zeros(rows, dtype=torch.bool)
is_novel[K:K + M] = True                             # 2 行伪掩码 = novel

# ---- 复现 unified_criterion.py L30/L36-L40 的切片 ----
sem_part = gt_sp_masks[-n - 1:, :]
inst_part = gt_sp_masks[:-n - 1, :]
labels_part = gt_labels_3d[:-n - 1]
novel_part = is_novel[:-n - 1]

print('总行数:', gt_sp_masks.shape[0])          # 期望 26
print('sem_part:', tuple(sem_part.shape))       # 期望 (21, 50)
print('inst_part:', tuple(inst_part.shape))     # 期望 (5, 50)
print('labels_part:', tuple(labels_part.shape)) # 期望 (5,)
print('novel_part:', tuple(novel_part.shape), novel_part.tolist())  # [F,F,T,T]
print('语义段每列至多一个 True:',
      bool((sem_part.sum(0) <= 1).all()))
print('unlabeled 兜底行覆盖列数:', int(gt_sp_masks[-1].sum()))  # 期望 20
```

2. 在仓库根目录运行 `python uniseg3d-tutorial/playground_u5_l1_split.py`（只需 PyTorch，无需 mmdet3d）。

**需要观察的现象**：`sem_part` 恒为 `(21, 50)`，`inst_part` 行数恒等于 `K+M`；`novel_part` 中 True 的位置正好是伪掩码行；语义段每列至多一个 True（互斥 one-hot）。

**预期结果**：输出与注释中的期望值一致。本脚本只依赖 torch 张量切片语义，结果可静态推得；仍请以本地运行为准（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：某场景 `gt_sp_masks` 形状为 `(29, 4000)`，`num_semantic_classes=20`。实例视图有多少行？如果 `is_novel` 中恰有 3 个 True，base 实例有几个？

**答案**：实例视图 = `[:-21]` = 前 8 行（29−21）；这 8 行里 3 行是 SAM3D 伪掩码（novel），所以 base 实例有 5 个，伪掩码 3 个，8 = 5+3。

**练习 2**：如果把训练配置里的 `merge_non_stuff_cls` 改成 `True`（其余不变），总调度的 `[-n-1:]` 切分会切到什么？这说明什么约束？

**答案**：`merge_non_stuff_cls=True` 时语义段被压成 3 行（wall、floor、所有非 stuff 合并行，见 [transforms_3d.py:425-428](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L425-L428)），总行数变成 \( K+M+3 \)，而切分公式仍取固定 21 行，行数对不上，语义视图会混入实例行、实例视图丢失若干行。这说明「数据管线的 `merge_non_stuff_cls=False`」与「损失侧的 `[-n-1:]`」是**强耦合约定**，改动任何一侧都必须同步另一侧。

**练习 3**：为什么 `sem_gt` 不需要（也没有）`labels_3d` 字段？

**答案**：语义段本身是 one-hot 布尔矩阵，类别信息就编码在「哪一行为 True」里；`ScanNetSemanticCriterion` 在运行时用 `argmax(0)` 现算类别序列（见 4.4.3），无需数据侧再存一份整数标签。

---

### 4.2 模块二：`query_masks`——查询采样后的 GT 同步裁剪

#### 4.2.1 概念说明

`sp_masks` 的列坐标是「场景内全部超点」。但 u4-l1 讲过：训练前向里 `_select_queries` 会按 `query_thr=0.5` 随机丢弃一部分超点查询（省算力 + 增广效果），只有存活查询才进入解码器。于是产生一个对齐问题：**预测 `sem_preds`/`masks` 的第 j 列对应的是「存活查询 j」，而 GT 的第 c 列对应的是「超点 c」——两套列坐标不一致了**。

解法：采样时把被选中的超点下标 `ids` 记下来，用 `sp_masks[:, ids]` 把 GT 的列同步裁剪到存活查询上，结果存成 `query_masks`。所以：

- `sp_masks` = 完整视图：行 = GT 条目，列 = **全部超点**；
- `query_masks` = 训练视图：行 = GT 条目（**行不变**），列 = **存活的查询**。

`query_masks` 在总调度里被切片后随各路 GT 一起传给子损失；匹配器（u5-l2）据此只在存活查询上做匹配，损失计算也在存活列上进行——被丢弃的查询从训练中完全消失，等价于「这个超点今天不参与监督」。

#### 4.2.2 核心流程

```text
_select_queries（每个场景）：
    若 query_thr < 1：
        n ∼ Uniform(query_thr, 1) × 超点数，再截断到 ≤ 3500
        ids = 随机排列取前 n 个            # 存活超点的原始下标
        queries = 超点特征[ids]            # 预测侧：只保留这些查询
        query_masks = sp_masks[:, ids]     # GT 侧：列同步裁剪
    否则：
        queries = 全部超点特征
        query_masks = sp_masks             # 退化为完整视图
```

随后在总调度里：`sem_gt` 优先用 `query_masks[-n-1:, :]`，`inst_gt` 附带 `query_masks[:-n-1, :]`（见 4.1.3）；提示 GT 的 `query_masks` 原样透传（提示查询是从特定超点生成的，天然在存活集合内，无需再裁）。

#### 4.2.3 源码精读

[uniseg3d/uniseg3d.py:226-256](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L226-L256) 是 `query_masks` 的出生地：[第 245-248 行](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L245-L248) 在 `[query_thr, 1)` 区间随机采样保留比例并截断到 3500；[第 249-251 行](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L249-L251) 用 `randperm` 选出存活下标 `ids`，查询取 `x[i][ids]`、GT 同步取 `sp_masks[:, ids]`；[第 252-254 行](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L252-L254) 是不采样的退化分支，`query_masks` 直接等于 `sp_masks`（保证下游「恒有 query_masks 可用」的约定）。

调用链上，[uniseg3d/uniseg3d.py:458](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L458) 在提特征之后立即做查询选择；[uniseg3d/uniseg3d.py:486-497](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L486-L497) 则是损失调度的完整上下文：解码（L486）→ 拆分提示输出（L488-491）→ IoU 预测（L493-494）→ **L496 把三路 pred 与三路 GT 一起交给 `self.criterion`**。

消费端再看一眼 [uniseg3d/unified_criterion.py:29-32](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L29-L32) 与 [L39-40](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L39-L40)：语义路和实例路都遵循「有 `query_masks` 就优先用」——语义路用 `query_masks[-n-1:, :]` 而不是 `sp_masks[-n-1:, :]`，就是为了与 `sem_preds` 的存活查询列对齐。

#### 4.2.4 代码实践

**实践目标**：亲手复现「行不变、列裁剪」的同步关系，并验证一个容易踩的坑——若忘记同步裁剪，形状会对不上。

**操作步骤**（示例代码，接 4.1.4 的脚本继续写即可）：

```python
# 示例代码：复现 _select_queries 的列裁剪
n_q = 40
keep = torch.randperm(S)[:n_q]                 # 模拟存活的 40 个超点
query_masks = gt_sp_masks[:, keep]             # 行 26 不变，列 50 -> 40

print('sp_masks:', tuple(gt_sp_masks.shape))     # (26, 50)
print('query_masks:', tuple(query_masks.shape))  # (26, 40)
# 模拟预测：sem_preds 的列数必须等于存活查询数
sem_preds = torch.randn(n_q, n + 1)
sem_gt_view = query_masks[-n - 1:, :]
target = sem_gt_view.float().argmax(0)
print('target:', tuple(target.shape))            # (40,)，与 sem_preds 行数一致
```

**需要观察的现象**：`query_masks` 与 `sp_masks` 行数相同、列数不同；`target` 长度等于存活查询数 `n_q`，与 `sem_preds` 的行数严格相等——这正是 `F.cross_entropy(sem_preds[:, :-1], target)` 要求的形状契约。

**预期结果**：形状全部吻合；若把 `sem_gt_view` 误换成 `sp_masks[-n-1:, :]`（50 列），`argmax(0)` 得到长度 50 的 `target`，与 40 行的 `sem_preds` 相减时 `cross_entropy` 会直接报形状错误——这个「故意做错一次」的对照实验值得做（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：`query_thr=0.5` 时一个有 2000 个超点的场景，最少/最多各保留多少查询？

**答案**：保留比例 ∼ Uniform(0.5, 1)，最少约 1000、最多 2000（均再受 3500 上限约束，本例不触及上限）。比例是随机的，每个 iteration 都不同。

**练习 2**：`query_masks` 与 `sp_masks` 的关系，为什么说前者是后者的「列子集视图」而不是副本？

**答案**：`query_masks = sp_masks[:, ids]`，只是按列下标重排选取，行（GT 条目）与每行的布尔取值语义完全不变；它不引入新标注，只是把监督信号对齐到存活查询坐标上。

**练习 3**：推理（`predict`）路径也走 `_select_queries` 的采样分支吗？提示查询需要裁剪吗？

**答案**：不需要。采样只发生在 `loss()` 训练路径（[uniseg3d.py:458](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L458) 位于 `loss()` 内）；推理用全量超点查询。提示查询本身由点/文本提示指定的超点生成（u4-l6），天然是存活查询的子集，所以总调度里提示 GT 的 `query_masks` 只做透传、不做裁剪。

---

### 4.3 模块三：五个子损失的调用顺序与 loss 字典合并

#### 4.3.1 概念说明

拆分完 GT，总调度开始「发货」。五个子损失的调用次序、合并方式决定了最终 loss 字典的 key 顺序（Python dict 保持插入序）。这一节回答三个问题：

1. **谁先谁后？** 实例 → 语义 → 点提示 → 文本提示 → 对比。顺序本身不影响数值（都是加法），但影响日志里 key 的排列，也影响条件分支的写法。
2. **每一路拿到什么？** 关键差异是：`inst_criterion` 拿到的点提示参数是**原始的** `point_prompt_insts`（供其内部的蒸馏项使用），而不是重组过的 `point_prompt_gts`。
3. **最终字典长什么样？** 主配置下一次前向共 **10 个 loss key**，其中 3 个实例类 key 各自是「1 份主输出 + 6 层 aux 深监督」的累加，而不是像 DETR 那样每层一个带编号的 key。

#### 4.3.2 核心流程

```text
loss = inst_criterion(pred, inst_gts,
                      point_prompt_pred, point_prompt_insts)   # 原始提示 GT！
loss.update(sem_criterion(pred, sem_gts))                      # + seg_loss
if point_prompt_pred is not None:
    loss.update(point_prompt_inst_criterion(...))              # + point_prompt_inst_loss
if text_prompt_pred is not None:
    loss.update(text_prompt_inst_criterion(...))               # + text_prompt_inst_loss
loss.update(contrastive_criterion(pred, inst_gts,
              point_prompt_pred, point_prompt_gts,
              text_prompt_pred, text_prompt_gts))              # 恒调用
return loss
```

注意两个不对称的设计：

- **两个提示 criterion 有 `is not None` 保护**（[L68](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L68)、[L71](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L71)），**对比损失没有**——[contrastive_criterion.py:50](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/contrastive_criterion.py#L50) 直接对 `point_prompt_pred['contras_embeds']` 下标，传 `None` 会抛 `TypeError`。它敢这么写，是因为训练管线里 `PointPromptGeneration` 与 `TextPromptGeneration` 恒执行（[config train_pipeline](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L172-L180)），两个提示键在训练批次中总是存在。**自己写假数据做实验时必须同时构造两路提示，否则走到 L74 就会崩**——这是本讲实践脚本设计的依据。
- **对比损失的内部保护靠「跳过」而非「判空」**：[contrastive_criterion.py:57-59](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/contrastive_criterion.py#L57-L59) 在某场景 `text_object_id` 为空时 `continue`；全部为空时返回 `contrastive_loss=0`（[L107-110](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/contrastive_criterion.py#L107-L110)）。

#### 4.3.3 源码精读

**调用与合并的主干。**

[uniseg3d/unified_criterion.py:65-75](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L65-L75) 就是上面伪代码的原文：L65 先调 `inst_criterion`（注意第 4 个参数是 `point_prompt_insts` 而非 `point_prompt_gts`，这个原始 GT 携带 `point_prompt_instance_ids`，供蒸馏项把点提示掩码当伪标签教常规查询，细节在 u5-l3/u5-l6）；L66 语义；L68-72 两路条件提示；L74 对比；L75 返回合并后的字典。

**`inst_criterion` 的返回 key。**

[uniseg3d/instance_criterion.py:616-630](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L616-L630) 是 `pred_iou=True` 时的返回：`{'inst_loss', 'ov_inst_loss', 'iou_inst_loss'}` 三个 key，`IntrinsicPointAndQuery_loss` 视点提示存在与否补挂。注意 [L617-624 行](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L617-L624)：6 层 `aux_outputs` 的损失**直接 `+=` 累加**进主输出的同名项——所以 `inst_loss` 是 7 份之和，日志里看不到 `inst_loss_0`、`inst_loss_1` 这类带层号的 key。`sem_preds` 只在解码器最后一层计算（u4-l4 的 `last_flag` 结论），所以 `seg_loss` 天然只有 1 份。

**提示 criterion 的返回 key。**

[uniseg3d/point_prompt_instance_criterion.py:206-210](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/point_prompt_instance_criterion.py#L206-L210) 返回 `point_prompt_inst_loss`（`total_weight=0.1` 缩放，同样把 6 层 aux 累加进来）；[uniseg3d/text_prompt_instance_criterion.py:211-215](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/text_prompt_instance_criterion.py#L211-L215) 同构地返回 `text_prompt_inst_loss`。它们不需要匹配器——[point_prompt_instance_criterion.py:147-151](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/point_prompt_instance_criterion.py#L147-L151) 用 `range` 直接构造恒等映射（u5-l5 展开）。

**对比 criterion 的返回 key。**

[uniseg3d/contrastive_criterion.py:107-115](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/contrastive_criterion.py#L107-L115) 返回 `contrastive_loss`，并按开关补挂 `rank_loss` 与 `pseudo_ditribute_loss`——注意 `pseudo_ditribute_loss` 是源码里的原始拼写（少一个 s），对照日志/grep 时请原样使用。

**权重配置一页看全。**

[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:67-124](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L67-L124) 定义了全部五路损失，其中 `num_semantic_classes=20`（[L69](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L69)）即拆分点；语义损失 `loss_weight=0.2*sem_weight=0.2`（[L70-73](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L70-L73)）；实例与提示、对比的权重见 [L74-100](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L74-L100)、[L101-117](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L101-L117)、[L118-124](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L118-L124)。

**一次前向的完整 loss 字典**（主配置：`pred_iou=True`、`use_ranking=True`、`use_pseudo_cls_supervise=True`、两类提示恒在）：

| 顺序 | key | 产出者 | 份数 | 缩放权重（1xb4 配置） |
|---|---|---|---|---|
| 1 | `inst_loss` | OVInstanceCriterion | 1+6 累加 | 内部 `[0.5, 1.0, 1.0, 0.5]`（cls/BCE/dice/score） |
| 2 | `ov_inst_loss` | OVInstanceCriterion（novel 路） | 1+6 累加 | `loss_weight[4]=0.1, [5]=0.1` |
| 3 | `iou_inst_loss` | OVInstanceCriterion（IoU 分支） | 1+6 累加 | `IoU_loss_weight=0.1` |
| 4 | `IntrinsicPointAndQuery_loss` | OVInstanceCriterion（蒸馏项） | — | `0.1*hype_lambda=0.1` |
| 5 | `seg_loss` | ScanNetSemanticCriterion | 仅 1（末层） | `0.2*sem_weight=0.2` |
| 6 | `point_prompt_inst_loss` | PointPromptInstanceCriterion | 1+6 累加 | `total_weight=0.1` |
| 7 | `text_prompt_inst_loss` | TextPromptInstanceCriterion | 1+6 累加 | `total_weight=0.1` |
| 8 | `contrastive_loss` | ContrastiveCriterion | — | `0.1*contra_hype_lambda=0.1` |
| 9 | `rank_loss` | ContrastiveCriterion | — | `ranking_loss_weight=0.1` |
| 10 | `pseudo_ditribute_loss` | ContrastiveCriterion | — | `cls_logit_loss_weight=0.1`（源码拼写如此） |

Runner 会把这 10 个 value 求和得到总损失（mmengine 默认行为），每个 key 单独打进日志的 `loss:` 行。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：用假数据完整驱动一次 `ScanNetUnifiedCriterion.__call__`，打印全部 loss key 与量级，验证本讲的 key 清单；对照真实训练日志核对键名。

**操作步骤**：

1. 确认已按 u1-l2 搭好环境（需要 mmdet3d、mmengine、torch）。
2. 把下面脚本保存为 `uniseg3d-tutorial/playground_u5_l1_criterion.py`（示例代码，非项目原有文件）：

```python
# 示例代码：假数据驱动 ScanNetUnifiedCriterion，打印全部 loss key
import torch
import uniseg3d                                   # 触发全部注册（u2-l1）
from mmengine import Config
from mmdet3d.registry import MODELS
from uniseg3d.structures import InstanceData_

torch.manual_seed(0)
B, n_sp, n_q = 1, 50, 40          # 批次 / 超点数 / 存活查询数
K, M, n = 3, 2, 20                # base 实例 / 伪掩码 / 语义类数
n_inst_cls = 18

# ---- 1) 按配置构建五合一 criterion ----
cfg = Config.fromfile(
    'configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py')
criterion = MODELS.build(cfg.model.criterion)

# ---- 2) 原始查询的 GT：26 行 gt_sp_masks + query_masks 列裁剪 ----
rows = K + M + n + 1
sp_masks = torch.zeros(rows, n_sp, dtype=torch.bool)
for r in range(K + M):
    sp_masks[r, 5 * r:5 * r + 5] = True
sem_ids = torch.randint(0, n, (30,))
sp_masks[K + M + sem_ids, torch.arange(30)] = True
sp_masks[-1, 30:] = True
labels = torch.zeros(rows, dtype=torch.long)
labels[:K] = torch.arange(K)
is_novel = torch.zeros(rows, dtype=torch.bool)
is_novel[K:K + M] = True
keep = torch.randperm(n_sp)[:n_q]
insts = [InstanceData_(sp_masks=sp_masks, labels_3d=labels,
                       is_novel=is_novel, query_masks=sp_masks[:, keep])]

# ---- 3) 原始查询的 pred：主输出 + 6 层 aux ----
def head(nq, ncol=n_q):
    return dict(cls_preds=[torch.randn(nq, n_inst_cls + 1)],
                masks=[torch.randn(nq, ncol)],
                scores=[torch.randn(nq)],
                preds_iou=[torch.randn(nq)])

pred = dict(head(n_q), sem_preds=[torch.randn(n_q, n + 1)],
            contras_embeds=[torch.randn(n_q, 32)])
pred['aux_outputs'] = [head(n_q) for _ in range(6)]

# ---- 4) 点提示：3 条；文本提示：2 条；object_id 互相对齐 ----
pp_pred = dict(head(3), contras_embeds=[torch.randn(3, 32)])
pp_pred['aux_outputs'] = [dict(cls_preds=[torch.randn(3, 19)],
                               masks=[torch.randn(3, n_q)],
                               scores=[torch.randn(3)]) for _ in range(6)]
pp_insts = [InstanceData_(sp_masks=sp_masks[:3][:, keep],
                          labels_3d=torch.tensor([0, 1, 2]),
                          point_object_id=torch.tensor([0, 1, 2]))]

tp_pred = dict(head(2), contras_embeds=[torch.randn(2, 32)])
tp_pred['aux_outputs'] = [dict(cls_preds=[torch.randn(2, 19)],
                               masks=[torch.randn(2, n_q)],
                               scores=[torch.randn(2)]) for _ in range(6)]
tp_insts = [InstanceData_(sp_masks=sp_masks[:2][:, keep],
                          labels_3d=torch.tensor([0, 1]),
                          text_object_id=torch.tensor([0, 1]))]

# ---- 5) 前向：对照 4.3.3 的 key 清单 ----
loss = criterion(pred, insts, pp_pred, pp_insts, tp_pred, tp_insts)
total = 0.
for k, v in loss.items():
    print(f'{k:32s} {v.item():10.4f}')
    total += v.item()
print(f'{"(sum)":32s} {total:10.4f}')
```

3. 在仓库根目录运行 `python uniseg3d-tutorial/playground_u5_l1_criterion.py`。

**需要观察的现象**：

- 打印出的 key 依次是 `inst_loss → ov_inst_loss → iou_inst_loss → IntrinsicPointAndQuery_loss → seg_loss → point_prompt_inst_loss → text_prompt_inst_loss → contrastive_loss → rank_loss → pseudo_ditribute_loss`，与 4.3.3 表格完全一致（共 10 个）。
- 随机 logits 下，分类项量级约在「交叉熵 × 权重」附近：如 `seg_loss ≈ 0.2 × ln(20) ≈ 0.6`，提示两项 `≈ 0.1 × (0.5 × ln(19) + …)`。
- 若第 4 步的两路提示传 `None`：程序会在对比损失处抛 `TypeError`（见 4.3.2 的分析）——这是「训练管线恒产提示」这一隐含约定的直接证据。

**预期结果**：key 名与顺序与表格一致、数值为有限正数。具体数值取决于随机种子与各损失内部实现（matcher、蒸馏分支等在后续讲义才拆解），本讲以核对 key 与量级为主；以上数值均待本地验证。

4. **对照官方日志**：若你已按 u1-l2 跑过训练，在日志中 grep `loss:` 行，逐个核对 10 个 key 的名字与相对大小是否与本实践一致；也可以只跑 1 个 iteration（`--cfg-options train_cfg.max_iters=1` 之类，视环境而定）观察首个日志行（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `inst_criterion`（L65）拿到的是 `point_prompt_insts` 而不是重组过的 `point_prompt_gts`？

**答案**：`OVInstanceCriterion` 内部的 `IntrinsicPointAndQuery` 蒸馏项需要 `point_prompt_instance_ids`（提示与匹配结果的行对应关系）来把点提示掩码对齐到常规查询（[instance_criterion.py:402-415](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L402-L415) 里没有该键就 `continue`）；而重组的 `point_prompt_gts` 只保留 `sp_masks/labels_3d/point_object_id` 等字段，是给点提示损失和对比损失用的。两个视角各取所需。

**练习 2**：把 `pred_iou` 改为 `False`（[config L13](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L13)），loss 字典会少哪些 key？

**答案**：少 `iou_inst_loss`——[instance_criterion.py:616-643](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L616-L643) 里 `pred_iou=False` 走第二个分支，返回字典只有 `inst_loss` 与 `ov_inst_loss`。同时假数据里的 `preds_iou` 键也不再需要（u5-l4 详述）。

**练习 3**：日志里为什么看不到 `aux_outputs` 相关的独立 loss key？

**答案**：实例与两路提示 criterion 都把 6 层 aux 损失直接 `+=` 进主 key（如 [instance_criterion.py:620-624](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L620-L624)），深监督被折叠进同名项；这与 DETR 系「每层一个 `loss_ce_N`」的风格不同，读日志时要意识到 `inst_loss` 是 7 份之和。

---

### 4.4 模块四：`seg_loss`——超点级交叉熵的三个细节

#### 4.4.1 概念说明

语义分割的监督目标：给每个**超点**（而非每个原始点）一个 20 类之一的标签，unlabeled 超点不参与。`ScanNetSemanticCriterion` 只用了 10 行核心代码，但有三个容易忽略的细节：

1. **逐样本计算再平均**：交叉熵按场景各自算，再对批次求均值——不同场景超点数不同，若先拼接再算均值，大会被大场景主导（这里选择每个场景等权）。
2. **切掉预测的最后一列**：`sem_preds` 有 21 列（20 类 + unlabeled/背景列，来自球面分类的 back_token，u4-l4），训练时 `[:, :-1]` 丢掉第 21 列。
3. **`ignore_index=20` 忽略 unlabeled 超点**：GT 的 `argmax` 取值范围是 0~20，值为 20（unlabeled 兜底行胜出）的超点在交叉熵里被整体忽略——**与细节 2 是一对配合动作**。

#### 4.4.2 核心流程

```text
对批次中每个场景 i：
    pred_mask = sem_preds[i]                # (n_queries, 21)
    若 ignore_index >= 0:
        pred_mask = pred_mask[:, :-1]       # (n_queries, 20)：丢 unlabeled 列
    target = sem_gt.sp_masks.float().argmax(0)   # (n_queries,)，取值 0..20
    loss_i = F.cross_entropy(pred_mask, target, ignore_index=20)
                  # target==20 的位置（unlabeled 超点）被忽略
seg_loss = loss_weight × mean(loss_1..loss_B)
```

为什么「切列 + ignore」要成对出现？交叉熵要求 `target` 的每个值要么在 `[0, C)` 内、要么等于 `ignore_index`。GT 里 unlabeled 超点的 `argmax` 是 20，而切掉最后一列后预测只有 20 类（下标 0~19）——如果没有 `ignore_index=20`，`F.cross_entropy` 会因越界标签直接报错；反过来，如果不切列而保留第 21 列，unlabeled 超点就会作为一个「真实类别」被学习，与「unlabeled 不参与监督」的意图相悖。两者配合的效果是：**unlabeled 超点对梯度零贡献，模型永远不会因为背景无标注而被惩罚**。

顺带一提维度：`sem_preds` 的行是存活查询（`query_masks` 的列，见 4.2），所以这里的「超点级」确切说是「存活超点查询级」；推理时（u6-l1）语义预测再经 `superpoints` 索引映射回原始点。

#### 4.4.3 源码精读

[uniseg3d/semantic_criterion.py:7-18](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/semantic_criterion.py#L7-L18)：注意它**不是** `nn.Module`，只是一个注册进 `MODELS` 的普通可调用对象，`__init__` 只存 `ignore_index` 与 `loss_weight` 两个数——没有可学习参数的损失完全不需要模块的开销。配置侧对应 [configs L70-73](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L70-L73)：`ignore_index=num_semantic_classes=20`、`loss_weight=0.2*sem_weight=0.2`。

[uniseg3d/semantic_criterion.py:34-43](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/semantic_criterion.py#L34-L43) 是全部计算：L35 逐场景 zip `pred['sem_preds']` 与 GT；L36-37 当 `ignore_index >= 0` 时切掉预测最后一列；L38-41 用 `gt_mask.sp_masks.float().argmax(0)` 把 one-hot 语义行转回类别下标序列并做忽略式交叉熵；L42 对批次 `stack + mean` 后乘权重；L43 以 `dict(seg_loss=loss)` 返回——**返回 dict 是 mmengine 损失协议的硬性要求**，总调度靠 `update` 合并。

它的输入 `sem_gts` 正是 4.1 里切出的后 21 行视图；`pred['sem_preds']` 来自解码器输出字典（[query_decoder.py:525-528](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/query_decoder.py#L525-L528) 的键之一），且只在最后一层解码器计算（u4-l4），所以语义路没有深监督。

#### 4.4.4 代码实践

**实践目标**：单独驱动 `ScanNetSemanticCriterion`，验证「unlabeled 超点零梯度」这一论断。

**操作步骤**（示例代码，可存为 `uniseg3d-tutorial/playground_u5_l1_sem.py`）：

```python
# 示例代码：验证 seg_loss 对 unlabeled 超点的忽略
import torch
import uniseg3d
from uniseg3d.semantic_criterion import ScanNetSemanticCriterion
from uniseg3d.structures import InstanceData_

torch.manual_seed(0)
crit = ScanNetSemanticCriterion(ignore_index=20, loss_weight=0.2)

n_q, n = 40, 20
sem_preds = [torch.randn(n_q, n + 1, requires_grad=True)]

# 语义 GT：前 30 个查询有类别，后 10 个为 unlabeled（最后一行胜出）
gt = torch.zeros(n + 1, n_q, dtype=torch.bool)
ids = torch.randint(0, n, (30,))
gt[ids, torch.arange(30)] = True
gt[-1, 30:] = True
sem_gts = [InstanceData_(sp_masks=gt)]

loss1 = crit(sem_preds, sem_gts)['seg_loss']

# 把 unlabeled 查询（31..40 行）的预测 logits 改成完全不同的值
with torch.no_grad():
    sem_preds[0][30:] = torch.randn(10, n + 1) * 100
loss2 = crit(sem_preds, sem_gts)['seg_loss']

print(f'loss1={loss1.item():.6f}  loss2={loss2.item():.6f}')
print('差值:', abs(loss1.item() - loss2.item()))
```

**需要观察的现象**：尽管后 10 个查询（unlabeled）的预测被换成大相径庭的值，两次损失**完全相等**；再对 `loss1.backward()` 后检查 `sem_preds.grad`，后 10 行的梯度应全为 0。

**预期结果**：差值为 0（浮点意义上相等），unlabeled 行梯度为 0。此行为可直接从 `ignore_index` 语义推出，具体数值待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`sem_preds` 有 21 列，为什么不直接用 21 列做交叉熵、把 unlabeled 当第 21 类学习？

**答案**：unlabeled 不是语义概念上稳定的类别（ScanNet 的 `unlabeled` 是「无标注」的兜底，可能是任何东西），学它会逼模型给无标注区域编造一致性；两阶段做法（丢列 + 忽略）让这些超点完全不产生监督信号，背景列仅保留给推理时的「无对象」机制（u4-l4 的 back_token）。

**练习 2**：`loss_weight=0.2` 相对实例类损失的内部权重（cls 0.5、BCE 1.0、dice 1.0）更小，这暗示什么？

**答案**：这是多任务平衡的手段——语义任务每个超点都产生一份监督（监督密度高），实例任务只有 K+M 行掩码监督（密度低）；给语义损失更小的总权重，避免语义梯度在总损失中淹没实例梯度。具体比例是实验调参结果，读者可在 4.3.4 的脚本里改 `sem_weight` 观察各 key 相对量级（改配置需重新 build criterion）。

**练习 3**：若某场景**所有**超点都是 unlabeled（`sp_masks` 只有最后一行为 True），`seg_loss` 会是多少？

**答案**：`F.cross_entropy` 在所有位置都被忽略时的行为取决于 PyTorch 版本（对全忽略输入可能返回 0 或 `nan`）。真实 ScanNet 训练场景不会出现全 unlabeled 的情况；但自己构造极端假数据实验时若遇到 `nan`，应先检查 GT 是否全空（待本地验证）。

---

## 5. 综合实践

**任务：做一次「损失字典体检」，把本讲四个模块串起来。**

在 4.3.4 脚本的基础上完成下面三步：

1. **体检一（拆分几何）**：把 `insts[0]` 构造里的 `K` 从 3 改成 6（总行数 29），不改其他任何东西，重新运行。观察 `inst_loss` 系列仍在（实例视图自动变成 8 行），`seg_loss` 不受影响——验证 4.1 的「一刀切」公式对任意 K、M 成立。
2. **体检二（query_masks）**：把 `keep = torch.randperm(n_sp)[:n_q]` 的 `n_q` 分别改成 30 和 40 各跑一次，观察 `seg_loss` 与 `inst_loss` 数值变化而所有 key 名不变——验证 4.2 的「列裁剪」只影响数值不影响结构；同时确认 `sem_preds` 等张量的形状要同步改（形状契约）。
3. **体检三（权重敏感性）**：用 `--cfg-options` 思路或直接改内存中的配置 dict（`cfg.model.criterion.sem_criterion.loss_weight = 1.0` 后再 `MODELS.build`），把 `seg_loss` 权重从 0.2 提到 1.0，观察打印中 `seg_loss` 数值精确变为原来的 5 倍、其余 key 不变——验证 4.4 的权重只在自己的分支内生效。

最后把三次运行的关键输出（key 列表 + 每个数值）整理成一张对照表。如果本地已能跑真实训练（u1-l2），再抽一条真实日志的 `loss:` 行做第四列对照，键名应完全一致（数值不同是正常的——真实模型是训练中的网络而非随机数）。

以上观察点均可从本讲源码分析直接推出，具体数值待本地验证。

## 6. 本讲小结

- `ScanNetUnifiedCriterion` 是**总调度**：`__init__` 用 `MODELS.build` 装配五个子损失，`__call__` 先把每份 GT 按 `[:-n-1]` / `[-n-1:]` 一刀切成实例视图（base 实例 + SAM3D 伪掩码）与语义视图（21 行 one-hot，末行 unlabeled 兜底），`labels_3d`、`is_novel` 与行切片同步。
- `query_masks` 是 `sp_masks` 的**列子集视图**：`_select_queries` 按 `query_thr=0.5` 随机丢查询时同步裁列，行（GT 条目）不变；语义路优先消费 `query_masks[-n-1:, :]`，实例路附带 `query_masks[:-n-1, :]`，保证 GT 与预测在同一套「存活查询」坐标上。
- 调用顺序固定为**实例 → 语义 → 点提示 → 文本提示 → 对比**，靠 `dict.update` 合并；`inst_criterion` 拿原始 `point_prompt_insts`（供蒸馏），两个提示 criterion 有判空保护而对比损失没有——它依赖训练管线恒产出两类提示这一约定。
- 主配置下一次前向共 **10 个 loss key**：`inst_loss`/`ov_inst_loss`/`iou_inst_loss`（各为 1+6 层深监督累加）、`IntrinsicPointAndQuery_loss`、`seg_loss`（仅末层 1 份）、`point_prompt_inst_loss`、`text_prompt_inst_loss`、`contrastive_loss`、`rank_loss`、`pseudo_ditribute_loss`（源码拼写）。
- `seg_loss` 是超点级交叉熵，三个细节：逐样本计算再批次平均；`[:, :-1]` 丢掉预测的 unlabeled 列；`ignore_index=20` 让 unlabeled 超点零梯度——切列与忽略必须成对，否则越界报错或学到伪背景类。
- `ScanNetSemanticCriterion` 不是 `nn.Module` 而是注册进 `MODELS` 的可调用对象，「损失=配置构建的可调用体、返回 dict」是 mmengine 的损失协议。

## 7. 下一步学习建议

本讲只看了各子损失的**接口与返回**，下一讲 u5-l2《匹配器 SparseMatcher 与代价函数》将钻进 `inst_criterion` 的第一站：`QueryClassificationCost`/`MaskBCECost`/`MaskDiceCost` 三种代价如何算、`SparseMatcher` 如何利用本讲传递的 `query_masks` 只在允许的 query-GT 组合里做 topk 稀疏匹配。建议在阅读前先自己重看 [unified_criterion.py:35-41](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L35-L41)（`query_masks` 如何随 `inst_gt` 下发）与 [instance_criterion.py:383-395](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L383-L395)（匹配器被调用的现场），并思考：超点查询天然携带归属信息时，为什么还需要一个匹配器？（答案在 u5-l2 揭晓：`is_novel` 拆出的 base 路仍需在「多个候选超点都部分覆盖同一实例」时选出最佳配对。）

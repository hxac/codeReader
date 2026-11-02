# 任务模块：BBox Coder、3D Assigner 与 Sampler

## 1. 本讲目标

u5-l4 读 `Anchor3DHead` 时我们留下了几个黑盒：`DeltaXYZWLHRBBoxCoder` 只讲了公式、`PseudoSampler` 一笔带过、「正样本」到底是谁定的没有展开。本讲把这些黑盒全部打开——它们全部住在 `mmdet3d/models/task_modules/` 目录里。读完本讲，你应该能够：

1. 说出「训练目标生产流水线」的四个环节及调用现场：IoU 计算器 → Assigner（分配）→ Sampler（采样）→ BBox Coder（编码）。
2. 理解 `PartialBinBasedBBoxCoder`（VoteNet 系编码器）为什么把「预测一个 7 维框」拆成「分类 + 残差」，并能独立完成 `encode → decode` 往返并统计解码误差。
3. 理解 `Max3DIoUAssigner` 的四步分配准则与三个阈值（`pos_iou_thr` / `neg_iou_thr` / `min_pos_iou`）各自挡住了谁。
4. 准确说出 `AssignResult` 与 `SamplingResult` 两个字段容器的语义（尤其是 `gt_inds` 的取值约定），并能按库内的标准姿势直接调用它们做一次小规模分配实验。

## 2. 前置知识

- **7 自由度框与锚点（origin）**：u2-l2 讲过框张量为 `(x, y, z, w, l, h, yaw)`，内部 `z` 是**底面中心**高度（构造默认 `origin=(0.5, 0.5, 0)`，见 [base_box3d.py:L48](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/base_box3d.py#L48)）；而 `gravity_center` 属性给出**几何中心** \(z + h/2\)（[base_box3d.py:L142-L148](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/base_box3d.py#L142-L148)）。本讲的两个 bbox coder 对「center 用哪个锚点」约定不同，是最容易踩的坑。
- **nearest_bev 与 3D IoU**：u2-l2 讲过 3D IoU = BEV 旋转 IoU × 高度交集，`nearest_bev` 把旋转框投影成轴对齐 BEV 外接矩形。本讲的 `BboxOverlapsNearest3D` 就建立在这个属性上。
- **Anchor3DHead 的损失结构**：u5-l4 讲过 `FocalLoss`（隐式背景类）配 `PseudoSampler`、回归/方向损失只算正样本。本讲回答「正样本是谁定的」——答案是 assigner。
- **InstanceData**：u2-l4 讲过的实例容器。3D 分配链路读写的字段名是 `bboxes_3d` / `labels_3d` / `priors`（priors 即 anchor，或上一阶段预测框）。

一个直觉比喻：训练一个检测头像改卷子——**IoU 计算器是扫描仪**（算每个 anchor 与每个 gt 的重叠度），**Assigner 是判卷老师**（判每道题算对、算错还是不算），**Sampler 是抽订员**（决定多少份对题、错题进损失），**BBox Coder 是答案书写格式**（把 gt 翻译成回归分支能拟合的数字）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [mmdet3d/models/task_modules/__init__.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/__init__.py) | task_modules 总出口：1 个 assigner、10 个 coder、9 个 sampler 的转出口 |
| [mmdet3d/models/task_modules/coders/partial_bin_based_bbox_coder.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/partial_bin_based_bbox_coder.py) | VoteNet 系编码器：尺寸按类回归残差、角度按 bin 分类 + 残差 |
| [mmdet3d/models/task_modules/coders/delta_xyzwhlr_bbox_coder.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/delta_xyzwhlr_bbox_coder.py) | SECOND/PointPillars 的连续 delta 编码（u5-l4 已精读，本讲作对比基准） |
| [mmdet3d/structures/ops/iou3d_calculator.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/ops/iou3d_calculator.py) | 三种 3D 重叠度计算器：Nearest3D / 3D / 轴对齐 |
| [mmdet3d/models/task_modules/assigners/max_3d_iou_assigner.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/assigners/max_3d_iou_assigner.py) | anchor ↔ gt 的正负样本分配器 |
| [mmdet3d/models/task_modules/samplers/pseudosample.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/samplers/pseudosample.py) | 不采样的「伪采样器」，直接返回全部正负下标 |
| [mmdet3d/models/task_modules/samplers/iou_neg_piecewise_sampler.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/samplers/iou_neg_piecewise_sampler.py) | 负样本按 IoU 分段配额抽取（PartA2/PV-RCNN 两阶段用） |
| [mmdet3d/models/dense_heads/train_mixins.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/train_mixins.py) | `AnchorTrainMixin`：assigner + sampler + coder 的真实调用现场 |
| [mmdet3d/models/dense_heads/anchor3d_head.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/anchor3d_head.py) | 头里 `_init_assigner_sampler` 把配置实例化成对象 |
| [mmdet3d/models/dense_heads/vote_head.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/vote_head.py) | `PartialBinBasedBBoxCoder` 的头号消费者 |
| [tests/test_models/test_task_modules/test_coders/test_partial_bin_based_box_coder.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_models/test_task_modules/test_coders/test_partial_bin_based_box_coder.py) | 官方单测，本讲 coder 实践的取材来源 |

## 4. 核心概念与源码讲解

### 4.1 task_modules 全景：训练目标的生产流水线

#### 4.1.1 概念说明

`mmdet3d/models/task_modules/` 下有五个子目录：`coders/`（10 个编码器）、`assigners/`（1 个分配器）、`samplers/`（本地 2 个 + 从 mmdet 借 6 个，见 [samplers/\_\_init\_\_.py:L2-L15](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/samplers/__init__.py#L2-L15)）、`anchor/`（u5-l4 的锚框生成器）、`voxel/`（u4-l3 的体素生成器）。它们注册在 `TASK_UTILS` 节点，共同身份是：**不带可学习参数、不进前向传播图，只在「算损失」那一刻被调用**。

为什么要单独成目录？因为这些组件与具体检测头解耦：同一个 `Max3DIoUAssigner` 既能配 `Anchor3DHead`（SECOND 的 RPN），也能配两阶段第二段的 `PartA2BBoxHead`——换分配策略只改 config，不动头代码。

#### 4.1.2 核心流程

anchor-based 头每步训练的目标生产流程：

```
anchors（前向时由 prior_generator 现算, u5-l4）
   │
   ├─ ① IoU 计算器: overlaps = iou_calculator(gt_bboxes, priors) → (num_gt, num_anchor)
   ├─ ② Assigner.assign: 四步准则给每个 anchor 定身份 → AssignResult(gt_inds, max_overlaps, labels)
   ├─ ③ Sampler.sample: 从 AssignResult 取出 pos_inds / neg_inds → SamplingResult
   └─ ④ BBox Coder: bbox_targets[pos] = encode(pos_bboxes, pos_gt_bboxes)
                     labels[pos] = gt_labels_3d[pos_assigned_gt_inds]
   → 交给 u5-l4 讲过的分类/回归/方向三类损失
```

#### 4.1.3 源码精读

头初始化时把 config 里的 dict 变成对象。注意两个细节：是否建真采样器由**损失函数类型**决定；assigner 允许是 **list**（每类一个，见 4.4）。

[anchor3d_head.py:L107-L110](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/anchor3d_head.py#L107-L110) —— `self.sampling` 的取值规则：损失类型是 `mmdet.FocalLoss` / `mmdet.GHMC` 时为 `False`。这正是 u5-l4「FocalLoss 配 PseudoSampler」结论的实现位置。

[anchor3d_head.py:L128-L142](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/anchor3d_head.py#L128-L142) —— `_init_assigner_sampler`：`sampling=True` 才按 `train_cfg.sampler` 建真采样器，否则一律 `PseudoSampler()`；`train_cfg.assigner` 是 dict 建单个、是 list 就逐个 `TASK_UTILS.build`。

真实调用现场在 `AnchorTrainMixin.anchor_target_single_assigner`：

[train_mixins.py:L270-L282](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/train_mixins.py#L270-L282) —— 先在 271-273 行把 `bboxes_3d` 从框对象**降级成裸 tensor**（原因见 4.4.3 的「标准姿势」），274 行包一个 `InstanceData(priors=anchors)` 当「预测实例」，然后一行 assign（275-277）、一行 sample（278-280），取出 `pos_inds / neg_inds`。283-289 行是「本帧无 gt」时的空分支——全部按负样本处理。

[train_mixins.py:L291-L320](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/train_mixins.py#L291-L320) —— 第 ④ 步与收尾：292 行 `labels += num_classes` 把背景标签定为 `num_classes`（u5-l4 讲过的 FocalLoss 隐式背景）；294-295 行 `bbox_coder.encode(pos_bboxes, pos_gt_bboxes)` 得到正样本回归目标，302-305 行按 `pos_inds` 散射进全量 `bbox_targets / bbox_weights / dir_targets` 并置权重 1；310-311 行用 `gt_labels_3d[sampling_result.pos_assigned_gt_inds]` 取回类别标签；317-318 行负样本只给 `label_weights=1`；319-320 行返回 8 元组。

#### 4.1.4 代码实践

**实践目标**：动手前先看清流水线的输入输出规模与「打表式」目标结构。

**操作步骤**（源码阅读型实践，不需要数据集）：

1. 通读 [train_mixins.py:L240-L320](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/train_mixins.py#L240-L320)，写出 `anchor_target_single_assigner` 返回的 8 元组各自的名字与形状（都与 anchor 数等长）。
2. 对照 u5-l4 读过的 [second_hv_secfpn_kitti.py:L60-L86](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/second_hv_secfpn_kitti.py#L60-L86)：`train_cfg.assigner` 是含 3 个 dict 的 list（Pedestrian / Cyclist / Car 各一个），且 Car 的三个阈值（0.6/0.45/0.45）明显高于 Pedestrian/Cyclist（0.35/0.2/0.2）。
3. 回答：SECOND KITTI 下 anchor 约 84 万个（u5-l4 的结论），`labels / bbox_targets / bbox_weights / dir_targets` 都是与 anchor 等长的稠密张量——为什么用打表而不是稀疏下标列表？（提示：`bbox_targets[pos_inds, :] = ...` 一行完成散射，GPU 友好；且 FocalLoss 需要对全量 anchor 算背景分类损失。）

**需要观察的现象 / 预期结果**：8 元组为 `(labels, label_weights, bbox_targets, bbox_weights, dir_targets, dir_weights, pos_inds, neg_inds)`；负样本行 `label_weights=1` 但 `bbox_weights` 全零——负样本只进分类损失、不进回归损失，与 u5-l4 闭环。待本地验证（可在本地实验环境的该函数内临时加 print 观察）。

#### 4.1.5 小练习与答案

**练习 1**：task_modules 下的组件为什么注册在 `TASK_UTILS` 而不是 `MODELS`？

**答案**：`MODELS` 下的组件由 `MODELS.build` 递归构建、参与前向传播；coder/assigner/sampler 是训练期工具，无参数、由头在 `__init__` 阶段单独经 `TASK_UTILS.build` 创建（[anchor3d_head.py:L128-L142](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/anchor3d_head.py#L128-L142)）。分开注册让两套角色在配置里类型不同（前者嵌在 model 树里，后者嵌在 `train_cfg` 里），查找语义也更清晰。

**练习 2**：`gt_instance_3d.bboxes_3d` 在 assign 前被换成 `.tensor`，这意味着传进 `SamplingResult` 的 `gt_bboxes` 是什么类型？后续 `bbox_coder.encode` 收到的又是什么？

**答案**：都是裸 `torch.Tensor`（[train_mixins.py:L271-L273](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/train_mixins.py#L271-L273)）。IoU 计算与编码只需要数值矩阵，框对象的几何方法在此阶段用不到，早拆壳可避免每个组件重复取 `.tensor`。

---

### 4.2 PartialBinBasedBBoxCoder：分类 + 残差的编码哲学

#### 4.2.1 概念说明

u5-l4 的 `DeltaXYZWLHRBBoxCoder` 把回归目标定义为「相对 anchor 的连续偏移」，它依赖一个前提：**anchor 本身已足够接近 gt**（SECOND 每类有专属尺寸的 anchor 阵列）。

室内检测（VoteNet）没有这个前提：种子点/投票点在物体表面附近，**没有现成 anchor**；同类物体尺寸方差大（ScanNet 18 类家具）；朝向任意。于是 VoteNet 把「一次回归 7 个连续量」拆成几个低难度子问题：

- **中心** `center`：仍回归连续坐标（相对投票点的偏移）；
- **尺寸**：先**分类**属于哪个尺寸簇（size_class，本质就是物体类别），再回归**相对该簇均值的残差**（size_res）；
- **朝向**：把 \([0, 2\pi)\) 均匀切成 `num_dir_bins` 个 bin，先**分类**落在哪个 bin（dir_class），再回归**相对 bin 中心的偏移**（dir_res，弧度）。

这就是 "partial bin-based"（部分基于 bin）名字的由来：**尺寸和朝向是 bin-based（分类 + 残差），中心是连续回归**。每个子问题的解空间都很小——分类只需挑对簇/桶，残差只在小范围内微调，对噪声大的点云输入更稳。

#### 4.2.2 核心流程

**encode：gt 框 → 5 组监督目标**

```
输入: gt_bboxes_3d（框对象）, gt_labels_3d (N,)
输出:
  center_target     = gravity_center        (N, 3)   ← 注意是几何中心 z+h/2!
  size_class_target = gt_labels_3d          (N,)     ← 尺寸簇 = 类别
  size_res_target   = dims - mean_sizes[labels]  (N, 3)  ← 米制残差
  (dir_class_target, dir_res_target) = angle2class(yaw)  (N,), (N,)
```

角度离散化的数学：设 bin 宽 \(\Delta = \frac{2\pi}{N}\)，对任意角 \(\theta\) 先折叠到 \([0, 2\pi)\)，再「半格偏移取整」：

\[
\theta' = (\theta + \Delta/2) \bmod 2\pi,\qquad
k = \left\lfloor \theta' / \Delta \right\rfloor,\qquad
r = \theta' - \left(k\Delta + \Delta/2\right)
\]

这样 bin 中心角恰为 \(k\Delta\)，残差 \(r \in [-\Delta/2, \Delta/2)\)。

**decode：「预测 dict」→ 7 维框**

```
center (B, M, 3) ─────────────────────────── 原样输出
dir_class --argmax--> k，dir_res --gather--> yaw = kΔ + r（折叠回 [-π, π]）
size_class --argmax--> c，size_res --gather--> dims = mean_sizes[c] + res
bbox3d = concat(center, dims, yaw) → (B, M, 7)
```

#### 4.2.3 源码精读

[partial_bin_based_bbox_coder.py:L24-L34](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/partial_bin_based_bbox_coder.py#L24-L34) —— 构造参数：`num_dir_bins`（角度 bin 数）、`num_sizes`（尺寸簇数 = 类别数）、`mean_sizes`（每类平均尺寸表，按训练集统计，与 `dims` 同列序逐维相减）、`with_rot`。ScanNet 配置里是 `num_sizes=18, num_dir_bins=1, with_rot=False` 加 18×3 的均值表（[votenet_8xb8_scannet-3d.py:L8-L32](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/votenet/votenet_8xb8_scannet-3d.py#L8-L32)）——呼应 u3-l4「ScanNet 是 6 维轴对齐框」。

[partial_bin_based_bbox_coder.py:L36-L66](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/partial_bin_based_bbox_coder.py#L36-L66) —— `encode`：第 49 行中心目标取 `gravity_center`；第 52-54 行 `size_class_target = gt_labels_3d`、`size_res_target = dims - mean_sizes[labels]`（高级索引一次取出每类均值）；`with_rot=False` 时方向目标退化为全零（第 58-63 行）。

[partial_bin_based_bbox_coder.py:L215-L234](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/partial_bin_based_bbox_coder.py#L215-L234) —— `angle2class`：上一节公式的一比一实现（228 行 `% 2π`、230 行加半格再折叠、231-233 行取整与作差）。

[partial_bin_based_bbox_coder.py:L236-L255](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/partial_bin_based_bbox_coder.py#L236-L255) —— `class2angle`：逆变换；253-254 行把大于 π 的角减 2π 折回 \([-\pi, \pi]\)（`limit_period` 默认开）。

[partial_bin_based_bbox_coder.py:L68-L109](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/partial_bin_based_bbox_coder.py#L68-L109) —— `decode`：方向分支 89-92 行 `argmax` 选 bin、`torch.gather` 抽出对应残差（网络对**每个** bin 各输出一个残差，只取被选中的）；尺寸分支 99-106 行同理，残差是 `(M, num_sizes, 3)` 三通道，`repeat(1,1,1,3)` 对齐后 gather，再 `index_select(mean_sizes)` 取基准；第 108 行 `cat` 拼出 7 维框——**decode 原样输出 center 的 z，不做任何 ±h/2 修正**。

[partial_bin_based_bbox_coder.py:L150-L213](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/partial_bin_based_bbox_coder.py#L150-L213) —— `split_pred`：把网络原始回归通道按 `3 + 2·num_dir_bins + num_sizes + 3·num_sizes` 的顺序切片成 dict；171-172 行 `center = base_xyz + 偏移`（回归的是相对投票点的坐标）；185 行与 201-202 行把归一化残差乘回物理量纲（bin 宽 / 类均值尺寸）。

消费现场一（训练侧，VoteHead）：

[vote_head.py:L678-L714](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/vote_head.py#L678-L714) —— 第 680 行 `bbox_coder.encode` 得 5 组目标；683-688 行用 **chamfer distance** 把「投票聚合点」与「gt 中心」做最近邻匹配——VoteNet 用距离匹配替代 IoU assigner（所以 VoteNet 的 train_cfg 里没有 assigner/sampler）；700-704 行把目标按匹配下标散射到每个 proposal；702 行 `dir_res_targets /= (np.pi / num_dir_bins)`、714 行 `size_res_targets /= pos_mean_sizes`——**损失在归一化域里计算**，与 `split_pred` 的两次乘法互为逆操作。

消费现场二（推理侧）：

[vote_head.py:L750-L767](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/vote_head.py#L750-L767) —— 第 750 行 `bbox_coder.decode` 出 7 维框；764-767 行用 `box_type_3d(bbox_selected, box_dim=..., with_yaw=self.bbox_coder.with_rot)` 包装——**不传 `origin`**，即按默认 `origin=(0.5, 0.5, 0)` 把 z 解释为**底面中心**（[base_box3d.py:L48](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/base_box3d.py#L48)），而 encode 端的中心目标是**几何中心**。两端的 z 语义相差 \(h/2\)，详见 4.2.5 练习 3。

与 `DeltaXYZWLHRBBoxCoder` 的对比（后者 u5-l4 已精读，这里只列差异）：

| 维度 | DeltaXYZWLHRBBoxCoder | PartialBinBasedBBoxCoder |
| --- | --- | --- |
| 使用者 | SECOND / PointPillars / PartA2 RPN | VoteNet / H3DNet / GroupFree3D |
| 前提 | 有 anchor（回归相对量） | 无 anchor（投票点附近） |
| center 锚点 | 底面中心；encode 时 `za + ha/2` 先换几何中心再作差、decode 后 `zg - hg/2` 换回（[delta_xyzwhlr_bbox_coder.py:L46-L47](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/delta_xyzwhlr_bbox_coder.py#L46-L47)、[L90](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/delta_xyzwhlr_bbox_coder.py#L90)） | encode 用 `gravity_center`，decode 原样输出（不修正） |
| 尺寸 | log 比 `log(g/a)`（[L52-L54](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/delta_xyzwhlr_bbox_coder.py#L52-L54)） | 分类 + 米制残差 |
| 角度 | 直接作差 + 头里 sin 变换（u5-l4） | bin 分类 + 弧度残差 |
| 配套分配 | IoU assigner（本讲 4.4） | chamfer 距离匹配（无 assigner） |

#### 4.2.4 代码实践

**实践目标**：走通 `encode →（把目标当完美预测）→ decode` 的往返并统计解码误差，亲眼确认「z 列恒差 h/2」这个锚点陷阱。

**操作步骤**（以下为示例代码，保存为独立脚本在装好 mmdet3d 的环境运行；配置与官方单测 [test_partial_bin_based_box_coder.py:L8-L48](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_models/test_task_modules/test_coders/test_partial_bin_based_box_coder.py#L8-L48) 同源）：

```python
# 示例代码（非项目原有文件）
import torch
from mmdet3d.registry import TASK_UTILS
from mmdet3d.structures import DepthInstance3DBoxes

coder = TASK_UTILS.build(dict(
    type='PartialBinBasedBBoxCoder', num_sizes=10, num_dir_bins=12,
    with_rot=True,
    mean_sizes=[[2.114256, 1.620300, 0.927272],
                [0.791118, 1.279516, 0.718182],
                [0.923508, 1.867419, 0.845495],
                [0.591958, 0.552978, 0.827272],
                [0.699104, 0.454178, 0.75625],
                [0.69519, 1.346299, 0.736364],
                [0.528526, 1.002642, 1.172878],
                [0.500618, 0.632163, 0.683424],
                [0.404671, 1.071108, 1.688889],
                [0.76584, 1.398258, 0.472728]]))

gt = DepthInstance3DBoxes([   # (x, y, z_bottom, w, l, h, yaw)
    [0.8308, 4.1168, -1.2035, 2.2493, 1.8444, 1.9245, 1.6486],
    [2.3002, 4.8149, -1.2442, 0.5718, 0.8629, 0.9510, 1.6030],
    [-1.1477, 1.8090, -1.1725, 0.6965, 1.5273, 2.0563, 0.0552]])
labels = torch.tensor([0, 1, 2])

center_t, size_cls_t, size_res_t, dir_cls_t, dir_res_t = coder.encode(gt, labels)

# 把 encode 的目标伪装成网络的“完美预测”（one-hot 乘 10 保证 argmax 正确）
bbox_out = dict(
    center=center_t.unsqueeze(0),
    size_class=torch.nn.functional.one_hot(size_cls_t, 10).float().unsqueeze(0) * 10,
    size_res=size_res_t.unsqueeze(0),        # (1, N, 10, 3)
    dir_class=torch.nn.functional.one_hot(dir_cls_t, 12).float().unsqueeze(0) * 10,
    dir_res=dir_res_t.unsqueeze(0))          # (1, N, 12)
decoded = coder.decode(bbox_out)[0]          # (N, 7)

err = (decoded - gt.tensor).abs()
print('逐列绝对误差:\n', err)
print('z 列误差 / h:', (err[:, 2] / gt.tensor[:, 5]).tolist())

# 换算锚点后再看：decode 的 z 是几何中心，减 h/2 回到底面中心语义
fixed = decoded.clone(); fixed[:, 2] -= decoded[:, 5] * 0.5
print('锚点修正后最大误差:', (fixed - gt.tensor).abs().max().item())
```

**需要观察的现象**：

1. x / y / w / l / h / yaw 六列误差在 \(10^{-7}\) 量级（尺寸残差与 bin 残差被完整还原；官方单测的期望值 `[3, 3, 0]` 与 `[0.0778, 0.0322, 0.0552]` 可手工按 4.2.2 公式复核）。
2. 唯独 **z 列系统性偏大**，且 `z 误差 / h` 每项恒为 `0.5`——因为 encode 存的是几何中心 \(z_{bottom} + h/2\)，而 `gt.tensor` 的 z 是底面中心。
3. 锚点修正后最大误差回落到 \(10^{-6}\) 量级。

**预期结果**：如上三条。若第 2 条不等于 0.5，说明环境里的代码与本仓库版本不一致。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：ScanNet 配置里 `num_dir_bins=1`（[votenet_8xb8_scannet-3d.py:L13](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/votenet/votenet_8xb8_scannet-3d.py#L13)），且 `with_rot=False`，角度编码退化成什么？

**答案**：`with_rot=False` 时 `encode` 直接给全零方向目标、`decode` 返回零角度（[partial_bin_based_bbox_coder.py:L58-L63](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/partial_bin_based_bbox_coder.py#L58-L63) 与 [L95-L96](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/partial_bin_based_bbox_coder.py#L95-L96)），`num_dir_bins` 实际不再参与编码；它仍决定 `split_pred` 切出多少通道。对应 ScanNet 轴对齐框（u3-l4）没有可学的朝向。

**练习 2**：为什么 `size_class_target` 直接复用 `gt_labels_3d`，而不是让网络自己发现尺寸簇？

**答案**：`mean_sizes` 本来就是**按类别**统计的平均尺寸（每类一行），「尺寸簇」与「语义类别」天然一一对应；复用类别标签使尺寸分类分支可与语义分类分支共享监督，且推理时 `argmax(size_class)` 同时指出「用哪一行均值做基准」（[partial_bin_based_bbox_coder.py:L99-L104](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/partial_bin_based_bbox_coder.py#L99-L104) 的 `index_select`）。

**练习 3**：对比两端源码事实——encode 的中心目标是 `gravity_center`（[L49](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/partial_bin_based_bbox_coder.py#L49)），而推理端 `predict_by_feat` 包装预测框时不传 `origin`（[vote_head.py:L764-L767](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/vote_head.py#L764-L767)）。按默认 `origin=(0.5, 0.5, 0)`（[base_box3d.py:L48](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/base_box3d.py#L48)），这两端对 z 的语义解释一致吗？

**答案**：不一致。训练目标与 decode 输出的 z 都是**几何中心**语义；而 `box_type_3d` 默认把传入 z 当**底面中心**存储（`origin != (0.5,0.5,0)` 时才会平移，见 [base_box3d.py:L77-L80](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/base_box3d.py#L77-L80)，这里恰好不触发）。也就是说按源码字面语义，推理写出的框 z 比训练定义的几何中心低 \(h/2\)。这是阅读源码时应留意的一个语义差；实际影响建议在本地用 demo 可视化验证（待本地验证），二次开发若沿用该头，需自行决定是否补 `z += h/2`。

---

### 4.3 3D IoU 计算器：assigner 的度量尺

#### 4.3.1 概念说明

Assigner 判正负的依据是重叠度，而「3D 框怎么算重叠度」本身有三档精度/速度取舍，全部实现在 `iou3d_calculator.py`，以 `iou_calculator=dict(type='...')` 挂在 assigner 配置里：

| 计算器 | 算法 | 典型用户 |
| --- | --- | --- |
| `BboxOverlapsNearest3D` | 框投影成 `nearest_bev` 轴对齐矩形后算 **2D** IoU（忽略高度与旋转） | SECOND / PointPillars / MVXNet 等 KITTI 系 anchor 分配 |
| `BboxOverlaps3D` | 调用框对象的 `overlaps`，真 3D 体积 IoU | nuScenes 系配置 / 两阶段 RoI 分配 |
| `AxisAlignedBboxOverlaps3D` | 6 维 `(x1,y1,z1,x2,y2,z2)` 框的纯张量 IoU/GIoU，支持 batch 维 | 3DSSD、室内无 yaw 场景 |

为什么室外 anchor 分配敢用「最粗」的 nearest 版本？u5-l4 讲过 anchor 阵列按 `rotations=[0, 1.57]` 双朝向铺满 BEV，IoU 只用来**粗筛正负**，精修交给 coder；而 nearest 版本全程纯 torch 张量运算（注释明说 CUDA 上比 numpy 快约 10 倍），84 万 anchor × 若干 gt 的矩阵算得动。

#### 4.3.2 核心流程

`bbox_overlaps_nearest_3d(bboxes1, bboxes2)` 的执行过程：

```
输入两个 (N, 7+C) / (M, 7+C) 裸张量（第一行就断言 .size(-1) 相等且 ≥ 7）
  → get_box_type(coordinate) 解析坐标系 → LiDARInstance3DBoxes 等
  → 包装成框对象，取 .nearest_bev       (N, 4) / (M, 4) 的 [x1, y1, x2, y2]
  → 复用 mmdet 的 bbox_overlaps 算 2D IoU / IoF
  → 返回 (N, M)
```

注意方向约定：assign 里的调用是 `iou_calculator(gt_bboxes, priors)`，得到 **(num_gt, num_anchor)** 矩阵——行是 gt、列是 anchor。后续按行取 max 是「每个 gt 找最佳 anchor」，按列取 max 是「每个 anchor 找最近 gt」。

#### 4.3.3 源码精读

[iou3d_calculator.py:L10-L48](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/ops/iou3d_calculator.py#L10-L48) —— `BboxOverlapsNearest3D`：构造参数只有 `coordinate`（默认 `'lidar'`，断言必须是 camera/lidar/depth 三者之一），`__call__` 直接转发到同名自由函数。

[iou3d_calculator.py:L130-L145](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/ops/iou3d_calculator.py#L130-L145) —— 实现体：132 行 `get_box_type` 按坐标系解析框类；134-135 行把裸张量包成框对象；140-141 行取 `nearest_bev`（u2-l2 讲过的 BEV 外接矩形属性）；143-144 行复用 mmdet 的 `bbox_overlaps` 完成 2D 计算。139 行注释点明 torch 版在 CUDA 上比 numpy 快约 10 倍。

[iou3d_calculator.py:L148-L176](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/ops/iou3d_calculator.py#L148-L176) —— `bbox_overlaps_3d`：同样先包框对象，但 176 行改调 `bboxes1.overlaps(...)` 走框类自带的真 3D IoU——即 u2-l2 讲过的「BEV 旋转 IoU × 高度交集」实现（[base_box3d.py:L529-L590](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/base_box3d.py#L529-L590)：561 行高度交集、570 行 `box_iou_rotated`、578 行两者相乘），docstring 注明暂不支持 aligned 模式。

[iou3d_calculator.py:L210-L329](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/ops/iou3d_calculator.py#L210-L329) —— `axis_aligned_bbox_overlaps_3d`：不依赖任何框类，直接对 `(…, 6)` 张量算 `overlap / union`（281-321 行的交集体积与并集），支持 `giou` 模式（324-329 行用最小外接长方体体积作惩罚项）；267-268 行的 `shape[:-2]` 断言允许 batch 维广播，3DSSD 的候选点分配直接受益。

#### 4.3.4 代码实践

**实践目标**：对同一对框对比「nearest_bev 近似」与「真 3D」两种 IoU 的数值差，建立精度直觉。

**操作步骤**（示例代码）：

```python
# 示例代码（非项目原有文件）
import torch
from mmdet3d.registry import TASK_UTILS

near = TASK_UTILS.build(dict(type='BboxOverlapsNearest3D', coordinate='lidar'))
full = TASK_UTILS.build(dict(type='BboxOverlaps3D', coordinate='lidar'))

# A 与 B 底面部分重叠，但高度完全错开
A = torch.tensor([[0., 0., -1., 4.0, 2.0, 1.0, 0.]])
B = torch.tensor([[1., 0.,  5., 4.0, 2.0, 1.0, 0.]])   # z 抬高 6m

print('nearest(近似):', near(A, B))   # 只看 BEV → 非零
print('真 3D     :', full(A, B))     # 高度不相交 → 0
```

**需要观察的现象**：nearest 版本给出非零值（BEV 投影重叠）；真 3D 版本给出 0（高度区间 \([-1,0]\) 与 \([5,6]\) 不相交）。

**预期结果**：如上。可再传 `mode='iof'` 观察分母从并集换成前景面积。注意 `BboxOverlaps3D` 走 `box_iou_rotated` 算子，建议在 GPU 环境验证；无 GPU 时可只跑 nearest 部分（纯 torch，CPU 可用）。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`bbox_overlaps_nearest_3d` 为什么先转 `nearest_bev` 而不是 `bev`？

**答案**：`bev` 仍带旋转角（u2-l2：返回 `(N, 5)` 旋转 BEV 框），后续还得做旋转 IoU；`nearest_bev` 直接给轴对齐 `(N, 4)`，可喂给 mmdet 的通用 2D `bbox_overlaps`，把问题彻底降维成轴对齐 2D IoU（[iou3d_calculator.py:L140-L144](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/ops/iou3d_calculator.py#L140-L144)），代价是高估斜置框的重叠。

**练习 2**：SECOND 的 assigner 配置里 `iou_calculator=dict(type='BboxOverlapsNearest3D')` 没写 `coordinate`，默认是什么？为什么恰好正确？

**答案**：默认 `'lidar'`（[iou3d_calculator.py:L21](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/ops/iou3d_calculator.py#L21)）。KITTI 的 anchor 与 gt 在进入 assign 前都已转到 LiDAR 系（u3-l3 讲过 KITTI 标注由相机系逆变换到雷达系），所以默认值正确；若给相机系数据集（如 mono3d）写配置就必须显式指定。

---

### 4.4 Max3DIoUAssigner：四步分配准则与 AssignResult

#### 4.4.1 概念说明

有了 IoU 矩阵，分配问题是：**84 万个 anchor，每个算正样本、负样本还是忽略？** `Max3DIoUAssigner` 是经典 MaxIoU 准则的 3D 版。它的身份很特别——只继承 mmdet 的 `MaxIoUAssigner`、重写「准备阶段」（取数、设备回退、ignore 处理），四步准则本体 `assign_wrt_overlaps` 继承自 mmdet（[max_3d_iou_assigner.py:L4](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/assigners/max_3d_iou_assigner.py#L4) 导入、[L154](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/assigners/max_3d_iou_assigner.py#L154) 调用，函数体在安装的 mmdet 依赖里，本仓库看不到）。

三个阈值各挡一件事：

- `pos_iou_thr`：anchor 与**最近 gt** 的 IoU ≥ 它 → 正样本候选；
- `neg_iou_thr`：与**所有** gt 的 IoU 都 < 它 → 负样本（背景）；
- 两者之间（`neg_iou_thr ≤ IoU < pos_iou_thr`）→ **忽略**：既不算正也不算负；
- `min_pos_iou`：兜底下限——第 4 步「每个 gt 把自己最好的 anchor 拉成正样本」时，若该 anchor 的 IoU 连这个下限都不到就不拉，防止硬凑低质正样本。

为什么允许「忽略带」？落在中间的 anchor 猜对了一半：强行标负会教网络「这里没车」，强行标正又监督错误，忽略最稳妥。KITTI 配置给 Car 开窄带（0.45~0.6）、给 Pedestrian 开宽带（0.2~0.35），因为行人框小、IoU 天然难高（[second_hv_secfpn_kitti.py:L60-L83](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/second_hv_secfpn_kitti.py#L60-L83)）。

#### 4.4.2 核心流程

docstring 明文给出的四步准则（顺序有讲究，[max_3d_iou_assigner.py:L77-L84](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/assigners/max_3d_iou_assigner.py#L77-L84)）：

```
1. 每个 anchor 先赋背景（gt_inds 置 0）
2. 与所有 gt 的 IoU < neg_iou_thr 的 anchor → 负样本（gt_inds = 0）
3. 对每个 anchor：与最近 gt 的 IoU ≥ pos_iou_thr → 正样本，记该 gt 下标
4. 对每个 gt：把与它 IoU 最大的 anchor(s) 也拉成正样本（低质量匹配）
   （受 min_pos_iou 下限约束；gt_max_assign_all 决定并列时全拉还是拉一个）
```

第 4 步（`match_low_quality=True`）的意义：极端情况下某 gt 与所有 anchor 的 IoU 都不到 `pos_iou_thr`，若无第 4 步它永远没有正样本、永远学不会。单阶段/RPN 需要它，两阶段精修阶段常关掉。

产出 `AssignResult` 三字段（这是 mmdet 定义、本仓库透传使用的容器，[task_modules/\_\_init\_\_.py:L2](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/__init__.py#L2)）：

| 字段 | 形状 | 语义 |
| --- | --- | --- |
| `gt_inds` | (num_anchor,) | **0 = 负样本；正整数 k = 正样本、指向第 k−1 个 gt（1-based 存储）；−1 = 忽略/未定** |
| `max_overlaps` | (num_anchor,) | 每个 anchor 与所有 gt 的最大 IoU |
| `labels` | (num_anchor,) | 按 `gt_labels` 查表填充的类别标签 |

> **读源码要以消费方为准**：本文件 docstring 的散文部分写的是「-1: negative sample、semi-positive integer: positive（0-based）」（[L15-L19](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/assigners/max_3d_iou_assigner.py#L15-L19)），与它自己内嵌的用法示例冲突——示例断言高重叠框得 `gt_inds=1`、低重叠框得 `0`（[L109-L116](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/assigners/max_3d_iou_assigner.py#L109-L116)）；下游 `PseudoSampler` 也按 `>0` 为正、`==0` 为负消费（4.5.3）。以示例与消费代码为准：0 是负、k>0 是正且 gt 下标要减一。

#### 4.4.3 源码精读

[max_3d_iou_assigner.py:L45-L65](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/assigners/max_3d_iou_assigner.py#L45-L65) —— 重写的 `__init__`：签名与 mmdet 版相同，唯一实质差别在第 65 行 `iou_calculator` 经 **mmdet3d 的** `TASK_UTILS.build` 构建——这样才能路由到 4.3 的三个 3D 计算器（默认值 `BboxOverlaps2D` 只是占位，实际配置都会显式覆盖）。

[max_3d_iou_assigner.py:L118-L127](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/assigners/max_3d_iou_assigner.py#L118-L127) —— `assign` 的取数约定：gt 从 `gt_instances.bboxes_3d` 取；anchor 优先 `pred_instances.priors`，没有则退回 `pred_instances.bboxes_3d.tensor`（两阶段第二段把上一层预测框当 priors）；ignore 框走 `gt_instances_ignore.bboxes_3d`。

**调用标准姿势（重要）**：直接调用 `assign` 时，`gt_instances.bboxes_3d` 应传**裸 tensor**——这正是 [train_mixins.py:L271-L273](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/train_mixins.py#L271-L273) 在 assign 前先把框对象降级为 `.tensor` 的原因：IoU 计算器第一行就要调 `bboxes1.size(-1)`（[iou3d_calculator.py:L130](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/ops/iou3d_calculator.py#L130)），而 `BaseInstance3DBoxes` 没有代理 `size` 方法，传框对象会直接 `AttributeError`（待本地验证）。

[max_3d_iou_assigner.py:L129-L139](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/assigners/max_3d_iou_assigner.py#L129-L139) —— `gpu_assign_thr` 的 CPU 回退：gt 数量超过阈值时把 priors/gt/labels 整体搬 CPU 再分配。分割数据一帧可能有上千 gt，`(num_gt, num_anchor)` 的 IoU 矩阵会撑爆显存，这个开关为此准备。

[max_3d_iou_assigner.py:L140-L154](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/assigners/max_3d_iou_assigner.py#L140-L154) —— 主线三连：140 行算 `overlaps`；142-152 行可选的 ignore 处理（与 `gt_bboxes_ignore` 算 IoF，超过 `ignore_iof_thr` 的 anchor 所在列整列置 −1，等效逐出竞争）；154 行把 overlaps 交给 `assign_wrt_overlaps`——四步准则就在那里执行。

[max_3d_iou_assigner.py:L155-L160](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/assigners/max_3d_iou_assigner.py#L155-L160) —— 若在 CPU 上分配，把 `gt_inds / max_overlaps / labels` 搬回原设备——这行同时是 `AssignResult` 三字段的直接证据。

多 assigner 支持：SECOND KITTI 每类一个 assigner（`assign_per_class=True` 时逐类筛选 gt、各自分配再拼表，[train_mixins.py:L140-L146 与 L194-L203](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/train_mixins.py#L140-L146)），前提是 anchor 生成时 `reshape_out=False` 让每类 anchor 可整体切片（u5-l4）。

#### 4.4.4 代码实践

**实践目标**：构造可控的小场景，直接调用 `Max3DIoUAssigner.assign`，验证四步准则与阈值效果（本讲规格指定的核心实践）。

**操作步骤**（示例代码，按 4.4.3 的标准姿势传裸 tensor）：

```python
# 示例代码（非项目原有文件）
import torch
from mmengine.structures import InstanceData
from mmdet3d.registry import TASK_UTILS

assigner = TASK_UTILS.build(dict(
    type='Max3DIoUAssigner',
    iou_calculator=dict(type='BboxOverlapsNearest3D'),
    pos_iou_thr=0.5, neg_iou_thr=0.3, min_pos_iou=0.3))

# gt：一辆车 (w=4, l=2, h=1.5)；注意 bboxes_3d 传裸 tensor（标准姿势）
gt_bboxes = torch.tensor([[10., 0., -1., 4., 2., 1.5, 0.]])
gt_inst = InstanceData(bboxes_3d=gt_bboxes, labels_3d=torch.tensor([0]))

# 4 个 anchor，与 gt 的 BEV 重叠刻意摆成 高/中/低/无（y 轴全部完全重合）
priors = torch.tensor([
    [10., 0., -1., 4., 2., 1.5, 0.],   # 完全重合 → IoU 1.0
    [12., 0., -1., 4., 2., 1.5, 0.],   # 部分重叠 → 1/3（忽略带）
    [13., 0., -1., 4., 2., 1.5, 0.],   # 少量重叠 → 1/7
    [40., 0., -1., 4., 2., 1.5, 0.]])  # 无重叠 → 0
pred_inst = InstanceData(priors=priors)

res = assigner.assign(pred_inst, gt_inst)
print('gt_inds      :', res.gt_inds.tolist())      # 预期 [1, 0 或 -1, 0, 0]
print('max_overlaps:', res.max_overlaps.tolist())
pos = int((res.gt_inds > 0).sum())
neg = int((res.gt_inds == 0).sum())
ign = int((res.gt_inds < 0).sum())
print(f'正 {pos} | 负 {neg} | 忽略 {ign}')
```

**需要观察的现象**：

1. 第 1 个 anchor `gt_inds=1`（正样本，指向 gt#0）；第 3、4 个 `gt_inds=0`（IoU < 0.3，负样本）。
2. IoU 手算（y 轴区间完全重合，故 IoU 退化为 x 轴交并比；两框 w 均为 4）：第 2 个 anchor 的 x 区间 \([8,12]\) 与 \([10,14]\) 交集 2、并集 6，IoU = 1/3 ≈ 0.333，落在 \([0.3, 0.5)\) 忽略带；第 3 个的 x 区间 \([8,12]\) 与 \([11,15]\) 交集 1、并集 7，IoU = 1/7 ≈ 0.143。
3. `max_overlaps` 与手算一致：`[1.0, 0.333, 0.143, 0.0]`。

**预期结果**：`gt_inds = [1, -1, 0, 0]`。可再做两个变体实验：(a) 把 `neg_iou_thr` 降到 `0.05`，忽略数变 0、负样本 +1；(b) 把 `pos_iou_thr` 抬到 `1.5`（不可达），第 3、4 步后第 1 个 anchor 仍为正——第 4 步低质量匹配把「该 gt 的最佳 anchor」（IoU=1.0 ≥ `min_pos_iou=0.3`）拉成正样本，这正是第 4 步存在的意义；第 2 个 anchor 则留在忽略带。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：SECOND KITTI 为什么给 Car 配 `pos_iou_thr=0.6`、给 Pedestrian 配 `0.35`？

**答案**：见 [second_hv_secfpn_kitti.py:L60-L83](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/second_hv_secfpn_kitti.py#L60-L83)。BEV 下行人框约 0.6m×0.8m，位置偏 0.3m IoU 就掉一半，若沿用 0.6 阈值行人几乎没有正样本；Car 框大、IoU 天然高，抬高阈值只留高质量正样本、提高定位监督精度。

**练习 2**：`min_pos_iou=0` 且某 gt 与所有 anchor 的 IoU 都低于 `pos_iou_thr`，这个 gt 还有正样本吗？

**答案**：有——第 4 步（`match_low_quality=True` 默认开）会把与该 gt IoU 最大的 anchor 拉成正样本，`min_pos_iou=0` 即不设下限；若 `min_pos_iou>0` 且最大 IoU 低于它，该 gt 才真正拿不到正样本（此类 anchor 留在忽略带）。

**练习 3**：`gpu_assign_thr` 触发 CPU 回退后，为什么还要把结果搬回原设备（155-159 行）？

**答案**：`gt_inds / max_overlaps / labels` 马上要在原设备上与 anchor、特征张量一起做散射与损失计算（4.1.3 第 ④ 步），留在 CPU 会引入跨设备同步与搬运开销；回退只为省下大 IoU 矩阵的显存，算完应立即回原设备。

---

### 4.5 Sampler 家族与 SamplingResult：从「身份」到「下标」

#### 4.5.1 概念说明

`AssignResult` 只回答「每个 anchor 是什么身份」；损失实际吃的是**下标集合**——哪些进正样本损失、哪些进负样本损失、要不要控制正负比例。这一步由 Sampler 完成。mmdet3d 本地实现两个采样器，另从 mmdet 借六个（`RandomSampler / OHEMSampler / InstanceBalancedPosSampler / IoUBalancedNegSampler / CombinedSampler / BaseSampler`，见 [samplers/\_\_init\_\_.py:L2-L15](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/samplers/__init__.py#L2-L15)）：

- **`PseudoSampler`**：不抽样——正样本全要、负样本全要。u5-l4 讲过 SECOND/PointPillars 的分类损失是 `FocalLoss`（自带难例降权），海量负样本不怕，所以不需要真采样器；头里甚至由损失类型自动决定用它（[anchor3d_head.py:L107-L110](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/anchor3d_head.py#L107-L110)）。
- **`IoUNegPiecewiseSampler`**：继承 mmdet 的 `RandomSampler`，把负样本按 IoU 区间**分段配额**抽取；真实用户是两阶段 RoI 头（PartA2 / PV-RCNN / PointRCNN），例如 PartA2 配 `num=128, pos_fraction=0.55, neg_piece_fractions=[0.8, 0.2], neg_iou_piece_thrs=[0.55, 0.1], return_iou=True`（[parta2.py:L184-L192](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/parta2.py#L184-L192)）。

`SamplingResult`（mmdet 定义、本地转出口）是采样产物容器。

#### 4.5.2 核心流程

```
SamplingResult = sampler.sample(assign_result, pred_instances, gt_instances)
  pos_inds              正样本 anchor 下标          (P,)
  neg_inds              负样本 anchor 下标          (Q,)
  ── 派生属性（按需计算） ──
  pos_bboxes            = priors[pos_inds]           (P, 7)   回归的“源框”
  pos_gt_bboxes         = gt_bboxes[gt_inds[pos]-1]  (P, 7)   回归的“目标框”
  pos_assigned_gt_inds  = gt_inds[pos_inds] - 1      (P,)     查类别标签用
  pos_gt_labels         = gt_labels[pos_assigned_gt_inds]
```

关键**减一**：`gt_inds` 里的正整数是 gt 下标 + 1（0 被背景占用），派生属性都要 −1 才能回去索引 gt。这正是 [train_mixins.py:L310-L311](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/train_mixins.py#L310-L311) 写 `gt_labels_3d[sampling_result.pos_assigned_gt_inds]` 的原因。注意「忽略」anchor（gt_inds=−1）既不进 pos 也不进 neg，被彻底排除在损失之外。

#### 4.5.3 源码精读

[pseudosample.py:L27-L61](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/samplers/pseudosample.py#L27-L61) —— `PseudoSampler.sample`：44-45 行取 `gt_bboxes / priors`；47-50 行两条 `torch.nonzero` 就是全部逻辑——`gt_inds > 0` 为 `pos_inds`、`== 0` 为 `neg_inds`（再次印证 4.4.2 的语义表）；52 行 `gt_flags` 全零；53-60 行直接构造 `SamplingResult`（多传一个 `avg_factor_with_neg=False`，即损失归一化只按正样本数）。它的 `_sample_pos / _sample_neg` 故意抛 `NotImplementedError`（[L19-L25](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/samplers/pseudosample.py#L19-L25)）——因为这个采样器根本不采样。

[iou_neg_piecewise_sampler.py:L35-L51](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/samplers/iou_neg_piecewise_sampler.py#L35-L51) —— 构造：断言 `neg_piece_fractions` 与 `neg_iou_piece_thrs` 等长。以 PartA2 的 `[0.8, 0.2]` 与 `[0.55, 0.1]` 为例：分段阈值**降序**排列，第 0 段覆盖 IoU ∈ [0.1, 0.55)（占 80% 配额的「较难负例」），最后一段覆盖 [0, 0.1)（占剩余配额的「简单负例」）。

[iou_neg_piecewise_sampler.py:L64-L133](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/samplers/iou_neg_piecewise_sampler.py#L64-L133) —— `_sample_neg`：75 行读 `assign_result.max_overlaps[neg_inds]`——这是 `AssignResult.max_overlaps` 字段的真实消费点；77-95 行的循环里，92-95 行按 `min ≤ IoU < max` 的带状条件筛出每段候选；97-131 行处理配额不足/溢出（前段不足则把缺额 `extend_num` 补给后段，最后一段直接拿「剩余全部配额」，132 行断言总数精确凑满）。

[iou_neg_piecewise_sampler.py:L135-L193](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/samplers/iou_neg_piecewise_sampler.py#L135-L193) —— `sample`：160-167 行可选 `add_gt_as_proposals`（把 gt 本身追加进 priors，并调 `assign_result.add_gt_` 同步改 gt_inds）；169-183 行按 `num × pos_fraction` 算正样本配额、`neg_pos_ub` 限制负样本上限；185-186 行构造 `SamplingResult`；187-191 行 `return_iou=True` 时把 `max_overlaps` 存成 `sampling_result.iou`——注释明说「PartA2 needs iou score to regression」（IoU 分数本身作为回归目标，u6-l4 会见到用途）。

#### 4.5.4 代码实践

**实践目标**：把 4.4.4 的 `assign_result` 用 `PseudoSampler` 打包，逐字段验证 `SamplingResult` 的派生关系。

**操作步骤**（示例代码，接 4.4.4 的变量）：

```python
# 示例代码（非项目原有文件）
from mmdet3d.models.task_modules import PseudoSampler

sampler = PseudoSampler()
sr = sampler.sample(res, pred_inst, gt_inst)

print('pos_inds            :', sr.pos_inds.tolist())
print('neg_inds            :', sr.neg_inds.tolist())
print('pos_bboxes          :', sr.pos_bboxes)
print('pos_gt_bboxes       :', sr.pos_gt_bboxes)
print('pos_assigned_gt_inds:', sr.pos_assigned_gt_inds.tolist())

# 一致性断言（应全部通过）
assert torch.equal(sr.pos_bboxes, priors[sr.pos_inds])
assert torch.equal(sr.pos_gt_bboxes, gt_bboxes[res.gt_inds[sr.pos_inds] - 1])
```

**需要观察的现象**：`pos_inds=[0]`；`pos_assigned_gt_inds=[0]`（只有一个 gt，减一后仍是 0）；`pos_gt_bboxes` 每行等于 gt 第 0 行；三个断言全部通过。

**预期结果**：如上。变体实验：把 4.4.4 的 `pos_iou_thr` 抬到 `1.1` 使正样本数为 0，则 `pos_inds` 为空张量、`sr.pos_bboxes.shape=(0, 7)`——[train_mixins.py:L293](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/train_mixins.py#L293) 的 `if len(pos_inds) > 0` 守卫就是为这种情况准备的。待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：为什么 SECOND/PointPillars 敢用「不采样」的 `PseudoSampler`，而 Faster R-CNN 一类要 `RandomSampler` 控制正负比？

**答案**：SECOND 的分类损失是 `FocalLoss`（u5-l4），对海量易负例自动降权，负样本全量进损失也不失衡，头里甚至按损失类型自动切换（[anchor3d_head.py:L107-L110](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/anchor3d_head.py#L107-L110)）；Faster R-CNN 用普通交叉熵，必须靠采样把正负比压到约 1:1。选 sampler 要看损失函数的自平衡能力。

**练习 2**：`gt_flags` 是干什么用的？`PseudoSampler` 里为什么恒为零？

**答案**：`add_gt_as_proposals=True` 的采样器会把 gt 追加进 priors（保证每个 gt 至少有一个完美正样本），`gt_flags` 标记哪些 priors 是追加的 gt（[iou_neg_piecewise_sampler.py:L160-L167](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/samplers/iou_neg_piecewise_sampler.py#L160-L167)）；`PseudoSampler` 从不追加，故恒置全零（[pseudosample.py:L52](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/samplers/pseudosample.py#L52)）。

**练习 3**：`IoUNegPiecewiseSampler._sample_neg` 为什么最后一段要单独处理配额？

**答案**：前几段按 `ceil(num_expected × fraction)` 取整会有损耗；最后一段直接拿「剩余全部配额」（`num_expected − len(neg_inds_choice)`，[iou_neg_piecewise_sampler.py:L78-L79](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/samplers/iou_neg_piecewise_sampler.py#L78-L79)），若该段候选不足还要从已选集合随机回补（107-125 行的两个分支），最后断言总数精确（132 行）。

---

## 5. 综合实践

**任务：用随机 anchors 和 2 个 gt，把本讲四个组件串成一条迷你版「目标生产线」；一半 anchor 走 IoU 分配链路，gt 框同时用 `PartialBinBasedBBoxCoder` 做 encode→decode 误差统计。**

```python
# 示例代码（非项目原有文件），保存为 mini_pipeline.py 运行
import torch
from mmengine.structures import InstanceData
from mmdet3d.registry import TASK_UTILS
from mmdet3d.structures import DepthInstance3DBoxes

torch.manual_seed(0)

# ── 场景：2 个 gt（LiDAR 系，裸 tensor） ──────────────
gt_bboxes = torch.tensor([
    [12., 2., -1., 4.0, 1.8, 1.5, 0.3],
    [20., -4., -1., 1.0, 0.7, 1.7, -1.2]])
gt_inst = InstanceData(bboxes_3d=gt_bboxes, labels_3d=torch.tensor([2, 0]))

# anchors：每个 gt 配 1 个近邻 anchor + 18 个随机远 anchor
near = gt_bboxes + torch.cat([torch.rand(2, 3) * 0.4 - 0.2,
                              torch.zeros(2, 4)])          # 仅位置微扰
far = torch.rand(18, 7) * torch.tensor([40., 40., 0., 0., 0., 0., 0.]) \
    + torch.tensor([5., -20., -1., 4., 1.8, 1.5, 0.])
priors = torch.cat([near, far])
pred_inst = InstanceData(priors=priors)

# ── ① IoU → ② assign → ③ sample ────────────────────
assigner = TASK_UTILS.build(dict(
    type='Max3DIoUAssigner',
    iou_calculator=dict(type='BboxOverlapsNearest3D'),
    pos_iou_thr=0.5, neg_iou_thr=0.3, min_pos_iou=0.3))
res = assigner.assign(pred_inst, gt_inst)

pos = int((res.gt_inds > 0).sum())
neg = int((res.gt_inds == 0).sum())
ign = int((res.gt_inds < 0).sum())
print(f'20 个 anchor → 正 {pos} | 负 {neg} | 忽略 {ign}')
print('assigned_gt_inds 分布:',
      torch.bincount(res.gt_inds.clamp(min=-1) + 1, minlength=4).tolist())
      # 下标 0/1/2/3 分别对应 gt_inds = -1/0/1/2

from mmdet3d.models.task_modules import PseudoSampler
sr = PseudoSampler().sample(res, pred_inst, gt_inst)
print('pos_assigned_gt_inds:', sr.pos_assigned_gt_inds.tolist())

# ── ④ coder A：DeltaXYZWLHRBBoxCoder 严格互逆自检（u5-l4 契约） ──
if len(sr.pos_inds) > 0:
    coder = TASK_UTILS.build(dict(type='DeltaXYZWLHRBBoxCoder'))
    deltas = coder.encode(sr.pos_bboxes, sr.pos_gt_bboxes)
    back = coder.decode(sr.pos_bboxes, deltas)
    print('delta 编码往返最大误差:',
          (back - sr.pos_gt_bboxes).abs().max().item())   # 预期 ~1e-7

# ── ④ coder B：PartialBinBasedBBoxCoder bin 编码误差统计 ──
mean_sizes = [[4.0, 1.8, 1.5], [1.0, 0.7, 1.7]]           # 每类一行，与 dims 同列序
bin_coder = TASK_UTILS.build(dict(
    type='PartialBinBasedBBoxCoder', num_sizes=2, num_dir_bins=12,
    with_rot=True, mean_sizes=mean_sizes))
gt_obj = DepthInstance3DBoxes(gt_bboxes.clone())          # encode 吃框对象
center_t, size_cls_t, size_res_t, dir_cls_t, dir_res_t = \
    bin_coder.encode(gt_obj, gt_inst.labels_3d)
bbox_out = dict(                                           # 目标伪装成完美预测
    center=center_t.unsqueeze(0),
    size_class=torch.nn.functional.one_hot(size_cls_t, 2).float().unsqueeze(0) * 10,
    size_res=size_res_t.unsqueeze(0),
    dir_class=torch.nn.functional.one_hot(dir_cls_t, 12).float().unsqueeze(0) * 10,
    dir_res=dir_res_t.unsqueeze(0))
decoded = bin_coder.decode(bbox_out)[0]
err = (decoded - gt_obj.tensor).abs()
print('bin 编码逐列平均误差 :', err.mean(dim=0).tolist())  # 仅 z 列 ≈ h/2
print('z 列误差 / h        :', (err[:, 2] / gt_obj.tensor[:, 5]).tolist())
```

**验收标准**：

1. 两个近邻 anchor 大概率为正样本（`pos_assigned_gt_inds` 含 0 和 1）；18 个随机远 anchor 绝大多数为负或忽略；三类计数之和恒为 20（分配只改身份、不增删 anchor）。
2. `delta 编码往返最大误差` 在 \(10^{-6}\) 量级——u5-l4「严格互逆」契约从 sampling_result 视角再验证一次。
3. `bin 编码逐列平均误差` 中除 z 列外均近 0；`z 列误差 / h` 恒为 `0.5`（gravity_center 锚点）。注意本例把 LiDAR 系张量直接喂给 `DepthInstance3DBoxes` 只为复用其属性方法——正式使用时应按数据集选对框子类。

整体运行结果：待本地验证。

## 6. 本讲小结

- **task_modules 是「训练目标生产线」**：IoU 计算器（算重叠）→ Assigner（定身份）→ Sampler（挑下标）→ BBox Coder（编码回归目标）；组件全部无参数、注册在 `TASK_UTILS`，标准调用现场是 [train_mixins.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/train_mixins.py) 的 `anchor_target_single_assigner`。
- **两种编码哲学**：`DeltaXYZWLHRBBoxCoder` 回归「相对 anchor 的连续量」，encode/decode 中显式做底面中心 ↔ 几何中心换算；`PartialBinBasedBBoxCoder` 把尺寸/朝向拆成「分类 + 残差」，为无 anchor 的 VoteNet 设计，中心目标直接取 `gravity_center` 且 decode 不修正——往返误差集中且仅集中在 z 列（恰为 h/2）。
- **`Max3DIoUAssigner` 只重写准备阶段**（取数约定、CPU 回退、ignore 处理、用 3D IoU 计算器），四步准则 `assign_wrt_overlaps` 继承自 mmdet 父类；双阈值之间是「忽略带」，第 4 步低质量匹配保证每个 gt 有机会获得正样本。
- **`AssignResult.gt_inds` 的真实语义以消费方为准**：0 = 负样本、正整数 k = 正样本指向第 k−1 个 gt、−1 = 忽略；文件 docstring 的散文描述与内置示例相互矛盾，是一个「以代码为准」的典型案例。
- **`SamplingResult` 是下标容器**：`pos_inds / neg_inds` 为源，`pos_bboxes / pos_gt_bboxes / pos_assigned_gt_inds` 为派生属性（查 gt 要减一）；anchor 训练路径里 `pos_gt_bboxes` 已是裸张量，`IoUNegPiecewiseSampler` 还会额外消费 `max_overlaps` 做分段采样并给 PartA2 回传 IoU 分数。
- **IoU 计算器三档精度**：nearest_bev 2D 近似（KITTI 系 anchor 分配，纯 torch）、真 3D 体积 IoU（`box_iou_rotated` CUDA 算子 × 高度交集）、轴对齐 6 维（室内 / 3DSSD，支持 GIoU 与 batch 维）。

## 7. 下一步学习建议

本讲补齐了 u5-l4 留下的最后一块拼图，anchor-based 主线（u5-l1 ~ u5-l4 + 本讲）至此完整。两条后续路径：

1. **推荐先读 u5-l7（3D 损失函数与 NMS）**：本讲产出的 `labels / bbox_targets / dir_targets` 马上被 `FocalLoss`、`SmoothL1Loss`、方向交叉熵消费，且 rotated IoU 损失会复用本讲 4.3 的 3D 重叠度思想，衔接最紧。
2. **或跳到 u6-l2（VoteNet）**：本讲 4.2 的 `PartialBinBasedBBoxCoder` 与 chamfer 距离匹配正是 VoteNet 的训练核心，读完本讲再读 `VoteHead` 会非常顺畅。

源码阅读补充：想看「多 assigner 逐类分配」的完整拼表逻辑，精读 [train_mixins.py:L140-L238](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/train_mixins.py#L140-L238)；想看两阶段第二段如何把预测框当 priors 再分配、以及 `return_iou` 的 IoU 回归用途，可预习 u6-l4 的 [parta2.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/parta2.py) 与 [part_aggregation_roi_head.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/part_aggregation_roi_head.py)。

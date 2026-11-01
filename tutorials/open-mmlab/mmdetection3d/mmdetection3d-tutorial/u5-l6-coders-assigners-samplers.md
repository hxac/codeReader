# 任务模块：BBox Coder、3D Assigner 与 Sampler

## 1. 本讲目标

上一讲（u5-l4）我们读完了 `Anchor3DHead` 的三个卷积分支，也第一次见到了 `DeltaXYZWLHRBBoxCoder` 和 `PseudoSampler` 这两个名字，但当时把它们当黑盒跳过了。本讲把这个黑盒彻底打开。读完本讲，你应该能够：

1. 说清「训练目标（target）是怎么生产出来的」这条完整流水线：IoU 计算器 → Assigner（分配）→ Sampler（采样）→ BBox Coder（编码）。
2. 理解 `PartialBinBasedBBoxCoder`（VoteNet 用的编码器）为什么用「分类 + 残差」而不是连续 delta 回归，并能手工走通它的 `encode → decode` 往返。
3. 理解 `Max3DIoUAssigner` 的四步分配准则，以及 `pos_iou_thr` / `neg_iou_thr` / `min_pos_iou` 各自挡住了谁。
4. 准确说出 `AssignResult` 与 `SamplingResult` 两个字段容器的含义，并能独立调用它们完成一次小规模分配实验。

## 2. 前置知识

- **7 自由度 3D 框与锚点（origin）**：u2-l2 讲过，`BaseInstance3DBoxes` 内部张量存的是 `(x, y, z, w, l, h, yaw)`，其中 `z` 是**底面中心**的高度；而 `gravity_center` 属性给出**几何中心**（底面 `z + h/2`）。本讲的两个 bbox coder 对「center 用哪个锚点」约定不同，这是最容易踩的坑。
- **nearest_bev 与 3D IoU**：u2-l2 讲过 3D IoU = BEV 旋转 IoU × 高度交集占比，以及 `nearest_bev` 把任意旋转框投影成轴对齐 BEV 外接矩形。本讲的 `BboxOverlapsNearest3D` 正是建立在这个属性上的「近似 IoU」。
- **Anchor3DHead 的损失结构**：u5-l4 讲过 `FocalLoss`（隐式背景类）+ `PseudoSampler` + 回归/方向损失只算正样本。本讲解释「正样本是谁定的」——答案就是 assigner。
- **InstanceData**：u2-l4 讲过的实例数据容器。3D 场景下分配器读写的字段是 `bboxes_3d` / `labels_3d` / `priors`（priors 即 anchor 或前一层预测框）。

一个直觉性的比喻：如果把训练一个检测头比作改卷子，那么 **IoU 计算器是「答题卡扫描仪」**（算出每个 anchor 与每个 gt 的重叠度），**Assigner 是「判卷老师」**（决定每道题算对、算错还是不算），**Sampler 是「抽订员」**（挑出多少份对题、错题进损失函数），**BBox Coder 是「标准答案的书写格式」**（把 gt 框翻译成回归分支能拟合的数值）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [mmdet3d/models/task_modules/coders/partial_bin_based_bbox_coder.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/partial_bin_based_bbox_coder.py) | VoteNet 系编码器：尺寸按类回归残差、角度按 bin 分类 + 残差 |
| [mmdet3d/models/task_modules/coders/delta_xyzwhlr_bbox_coder.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/delta_xyzwhlr_bbox_coder.py) | SECOND/PointPillars 的连续 delta 编码（u5-l4 已精读，本讲只做对比） |
| [mmdet3d/structures/ops/iou3d_calculator.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/ops/iou3d_calculator.py) | 三种 3D 重叠度计算器：Nearest3D / 3D / 轴对齐 |
| [mmdet3d/models/task_modules/assigners/max_3d_iou_assigner.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/assigners/max_3d_iou_assigner.py) | anchor ↔ gt 的正负样本分配器 |
| [mmdet3d/models/task_modules/samplers/pseudosample.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/samplers/pseudosample.py) | 不做采样的「伪采样器」，直接返回全部正负下标 |
| [mmdet3d/models/task_modules/samplers/iou_neg_piecewise_sampler.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/samplers/iou_neg_piecewise_sampler.py) | SSN 用的分段负样本采样器 |
| [mmdet3d/models/dense_heads/train_mixins.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/train_mixins.py) | `AnchorTrainMixin`：assigner + sampler + coder 的真实调用现场 |
| [mmdet3d/models/dense_heads/anchor3d_head.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/anchor3d_head.py) | 头里 `_init_assigner_sampler` 把配置实例化成对象 |
| [mmdet3d/models/dense_heads/vote_head.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/vote_head.py) | `PartialBinBasedBBoxCoder` 的头号消费者 |
| [tests/test_models/test_task_modules/test_coders/test_partial_bin_based_box_coder.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_models/test_task_modules/test_coders/test_partial_bin_based_box_coder.py) | 官方单测，本讲实践的取材来源 |

## 4. 核心概念与源码讲解

### 4.1 task_modules 全景：训练目标的生产流水线

#### 4.1.1 概念说明

`mmdet3d/models/task_modules/` 目录下放着五个子目录：`coders/`（10 个编码器）、`assigners/`（1 个分配器）、`samplers/`（本库 2 个 + 从 mmdet 借 6 个）、`anchor/`（u5-l4 讲过的锚框生成器）、`voxel/`（u4-l3 讲过的体素生成器）。它们共同的注册节点是 `TASK_UTILS`，共同的身份是：**本身不带可学习参数、不参与前向传播图，只在「算损失」那一刻被调用**。

为什么要单独成目录？因为这些组件与具体检测头解耦：同一个 `Max3DIoUAssigner` 能配 `Anchor3DHead`（KITTI）也能配 `PartA2BBoxHead`（两阶段第二段），换分配策略不用改头代码，只改 config。

#### 4.1.2 核心流程

anchor-based 头每一步训练的目标生产流程：

```
anchors (前向时生成, u5-l4)
   │
   ├─ ① IoU 计算器: overlaps = iou_calculator(gt_bboxes, priors)   → (num_gt, num_anchor)
   │
   ├─ ② Assigner.assign: 按四步准则打标签 → AssignResult(gt_inds, max_overlaps, labels)
   │
   ├─ ③ Sampler.sample: 从 AssignResult 挑出 pos_inds / neg_inds → SamplingResult
   │
   └─ ④ BBox Coder: bbox_targets[pos] = coder.encode(pos_bboxes, pos_gt_bboxes)
                     labels[pos] = gt_labels_3d[pos_assigned_gt_inds]
   → 交给 u5-l4 讲过的三类损失
```

#### 4.1.3 源码精读

头的初始化入口，把 config 里的 dict 变成对象，注意 assigner 允许是 **list**（每类一个分配器，见 4.4）：

[anchor3d_head.py:L128-L142](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/anchor3d_head.py#L128-L142) —— `_init_assigner_sampler`：`train_cfg.sampler` 存在且 `self.sampling=True` 才建真采样器，否则一律 `PseudoSampler()`；`assigner` 是 dict 就建单个、是 list 就逐个 build。

真实调用现场在 `AnchorTrainMixin`：

[train_mixins.py:L270-L282](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/train_mixins.py#L270-L282) —— 先把 `gt_instance_3d.bboxes_3d` 从框对象**降级成裸 tensor**（271-273 行），再包一个 `InstanceData(priors=anchors)` 当作「预测实例」，然后一行 assign、一行 sample，取出 `pos_inds / neg_inds`。这是全库调用 assigner 的标准姿势，本讲实践将完全复刻这两行。

[train_mixins.py:L293-L311](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/train_mixins.py#L293-L311) —— 第 ④ 步：`self.bbox_coder.encode(sampling_result.pos_bboxes, sampling_result.pos_gt_bboxes)` 得到正样本回归目标，按 `pos_inds` 散射回全量 `bbox_targets` 张量；类别标签则用 `gt_labels_3d[sampling_result.pos_assigned_gt_inds]` 取回——这两个 `sampling_result` 字段的含义在 4.5 展开。

#### 4.1.4 代码实践

**实践目标**：在动手读组件之前，先用打点的方式亲眼看到这条流水线的输入输出规模。

**操作步骤**（源码阅读型实践，不依赖数据集）：

1. 阅读 [train_mixins.py:L240-L320](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/train_mixins.py#L240-L320)，找出 `anchor_target_single_assigner` 返回的 8 元组分别是什么。
2. 对照 u5-l4 读过的 [second_hv_secfpn_kitti.py:L60-L86](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/second_hv_secfpn_kitti.py#L60-L86)：注意 `train_cfg.assigner` 是一个含 3 个 dict 的 list，与 `num_classes=3` 一一对应，且 Car 的三个阈值（0.6/0.45/0.45）明显高于 Pedestrian/Cyclist（0.35/0.2/0.2）。
3. 回答： SECOND KITTI 下 anchor 总数约 84 万（u5-l4 的结论），而 `labels / bbox_targets / bbox_weights / dir_targets` 都是与 anchor 等长的「打表」张量——想想为什么不用稀疏的下标列表而要打表？（提示：`bbox_targets[pos_inds, :] = ...` 一行散射，GPU 友好。）

**需要观察的现象 / 预期结果**：8 元组为 `(labels, label_weights, bbox_targets, bbox_weights, dir_targets, dir_weights, pos_inds, neg_inds)`；负样本只有 `label_weights=1` 而全零的 `bbox_weights`——即负样本只进分类损失、不进回归损失，与 u5-l4 的结论闭环。待本地验证（如需打点确认，可在本地实验环境的该函数内临时加 print）。

#### 4.1.5 小练习与答案

**练习 1**：`task_modules` 下的组件为什么注册在 `TASK_UTILS` 而不是 `MODELS`？

**答案**：`MODELS` 里的组件参与 `MODELS.build` 构建检测器对象树、进入前向传播；而 coder/assigner/sampler 是「训练期工具」，无参数、由头在 `__init__` 或 `loss_by_feat` 阶段单独经 `TASK_UTILS.build` 创建（如 [anchor3d_head.py:L134-L138](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/anchor3d_head.py#L134-L138)），分开注册便于同一套注册查找语义下隔离两类角色。

**练习 2**：`gt_instance_3d.bboxes_3d` 在进入 assigner 前被换成了 `.tensor`，这意味着传给 `SamplingResult` 的 `gt_bboxes` 是什么类型？

**答案**：是裸 `torch.Tensor` 而不是 `BaseInstance3DBoxes` 对象（[train_mixins.py:L271-L273](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/train_mixins.py#L271-L273)）。因为 IoU 计算与编码只需要数值矩阵，框对象的几何方法（corners/bev 等）在此阶段用不到，早拆壳可以避免每次访问再取 `.tensor`。

---

### 4.2 PartialBinBasedBBoxCoder：分类 + 残差的编码哲学

#### 4.2.1 概念说明

u5-l4 的 `DeltaXYZWLHRBBoxCoder` 把回归目标定义成「相对 anchor 的连续偏移」：位移按对角线归一、尺寸取 log 比、角度直接作差。这依赖一个前提——**anchor 本身已经足够接近 gt**（SECOND 每个类别有专属尺寸的 anchor 阵列）。

室内检测（VoteNet）没有这个前提：种子点/投票点在物体表面附近，**没有现成的 anchor**；同类物体的尺寸方差大（ScanNet 有 18 类家具）；朝向角在深度坐标系里任意。于是 VoteNet 采用另一种编码：把「预测一个 7 维连续量」拆成「**几个低难度子问题**」：

- **中心** `center`：仍然回归连续坐标（相对投票点的偏移）；
- **尺寸**：先**分类**属于哪个尺寸簇（size_class，本质上就是物体类别），再回归**相对该簇均值的残差**（size_res）；
- **朝向**：把 \([0, 2\pi)\) 均匀切成 `num_dir_bins` 个 bin，先**分类**落在哪个 bin（dir_class），再回归**相对 bin 中心的偏移**（dir_res，单位是弧度）。

这就是 "partial bin-based"（部分基于 bin）名字的由来：**尺寸和朝向是 bin-based（分类 + 残差），中心是连续回归**。好处是每个子问题的解空间都很小——分类只需挑对簇/桶，残差只需在小范围内微调，这对点云这种输入噪声大的任务更稳。

#### 4.2.2 核心流程

**encode：gt 框 → 5 组监督目标**

```
输入: gt_bboxes_3d (N, 7 框对象), gt_labels_3d (N,)
输出:
  center_target    = gravity_center        (N, 3)   ← 注意是几何中心!
  size_class_target = gt_labels_3d          (N,)     ← 尺寸簇 = 类别
  size_res_target   = dims - mean_sizes[labels]  (N, 3)  ← 米制残差
  dir_class_target, dir_res_target = angle2class(yaw)    (N,), (N,)
```

角度离散化的数学：设 bin 宽 \(\Delta = \frac{2\pi}{N}\)，对任意角 \(\theta\)（先折叠到 \([0, 2\pi)\)）：

\[
\theta' = (\theta + \Delta/2) \bmod 2\pi,\qquad
k = \lfloor \theta' / \Delta \rfloor,\qquad
r = \theta' - (k\Delta + \Delta/2)
\]

即先把角度「半格偏移」再取整，使得残差 \(r \in [-\Delta/2, \Delta/2)\)，bin 中心角恰为 \(k\Delta\)。

**decode：「完美预测」→ 7 维框**

```
center (B, M, 3) ──────────────────────────┐
dir_class --argmax--> k, dir_res --gather--> yaw = kΔ + r  (折叠回 [-π, π])
size_class --argmax--> c, size_res --gather--> dims = mean_sizes[c] + res
bbox3d = concat(center, dims, yaw)  → (B, M, 7)
```

#### 4.2.3 源码精读

[partial_bin_based_bbox_coder.py:L24-L34](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/partial_bin_based_bbox_coder.py#L24-L34) —— 构造函数四个参数：`num_dir_bins`（角度 bin 数）、`num_sizes`（尺寸簇数 = 类别数）、`mean_sizes`（每类的平均尺寸，来自数据集统计，ScanNet 是 18×3 的表）、`with_rot`（ScanNet 无 yaw 框为 False，SUN RGB-D 为 True——呼应 u3-l4 的「6 维轴对齐框」）。

[partial_bin_based_bbox_coder.py:L36-L66](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/partial_bin_based_bbox_coder.py#L36-L66) —— `encode`：中心目标取 `gravity_center`（几何中心，u2-l2 的属性在此消费）；尺寸目标第 53-54 行 `dims - mean_sizes[labels]` 用**高级索引**一次取出每类的均值再作差；`with_rot=False` 时方向目标退化为全零。

[partial_bin_based_bbox_coder.py:L215-L234](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/partial_bin_based_bbox_coder.py#L215-L234) —— `angle2class`：上面公式的一比一实现。第 228 行先 `% 2π` 折叠，第 230 行加半格再折叠，第 231-233 行分别得到类下标与残差。

[partial_bin_based_bbox_coder.py:L236-L255](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/partial_bin_based_bbox_coder.py#L236-L255) —— `class2angle`：逆变换，第 253-254 行把大于 π 的角减 2π 折回 \([-\pi, \pi]\)（`limit_period` 默认开）。

[partial_bin_based_bbox_coder.py:L68-L109](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/partial_bin_based_bbox_coder.py#L68-L109) —— `decode`：方向分支第 89-92 行 `argmax` 取 bin、`torch.gather` 按选中 bin 抽出对应残差（因为网络对每个 bin 各输出一个残差）；尺寸分支第 99-106 行同理，且残差是 `(M, num_sizes, 3)` 三通道，`repeat(1,1,1,3)` 对齐后 gather；最后第 108 行 `cat([center, bbox_size, dir_angle])` 拼出 7 维框——**decode 不改写 center 的 z，原样输出**。

消费现场一（训练侧）：

[vote_head.py:L678-L704](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/vote_head.py#L678-L704) —— `VoteHead._get_targets_single` 第 680 行调用 `bbox_coder.encode` 得到 5 组目标，随后第 683-686 行用 chamfer distance 把「投票聚合点」与「gt 中心」做最近邻匹配（这就是 VoteNet 的分配方式——**用距离替代 IoU assigner**，所以 VoteNet 的 config 里没有 assigner），第 700-704 行把目标按匹配结果下标散射到每个 proposal 上。注意第 702 行 `dir_res_targets /= (np.pi / num_dir_bins)`——损失里用的是**归一化残差**（除以 bin 宽），与 4.2.4 实践的观察呼应。

消费现场二（推理侧）：

[vote_head.py:L750-L767](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/vote_head.py#L750-L767) —— `get_bboxes` 第 750 行 `bbox_coder.decode(bbox_preds_dict)` 出 7 维框，第 764-767 行用 `box_type_3d(...)` 包装时**不传 origin**，即默认 `(0.5, 0.5, 0.5)` 几何中心锚点——与 encode 端 `gravity_center` 恰好闭环（这也解释了为什么 decode 不需要 ±h/2 修正，而 `DeltaXYZWLHRBBoxCoder` 需要，见对比表）。

split_pred 是网络原始输出到 dict 的「切分器」：

[partial_bin_based_bbox_coder.py:L150-L213](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/partial_bin_based_bbox_coder.py#L150-L213) —— 回归通道按 `3 + 2*num_dir_bins + num_sizes + 3*num_sizes` 的顺序切片；第 171-172 行 center = `base_xyz + 偏移`（回归的是相对投票点的相对坐标）；第 185、201-202 行把归一化残差乘回物理量纲（bin 宽 / mean_sizes）。

与 `DeltaXYZWLHRBBoxCoder` 的对比总结：

| 维度 | DeltaXYZWLHRBBoxCoder | PartialBinBasedBBoxCoder |
| --- | --- | --- |
| 使用者 | SECOND / PointPillars / PartA2 | VoteNet / H3DNet / GroupFree3D |
| 前提 | 有 anchor（回归相对量） | 无 anchor（投票点附近） |
| center 锚点 | 底面中心，编码时 `z + h/2` 换几何中心再作差（[delta_xyzwhlr_bbox_coder.py:L46-L51](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/delta_xyzwhlr_bbox_coder.py#L46-L51)） | 直接用 `gravity_center` |
| 尺寸 | log 比 `log(g/a)`（[L52-L54](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/delta_xyzwhlr_bbox_coder.py#L52-L54)） | 分类 + 米制残差 |
| 角度 | 直接作差 + 头里 sin 变换（u5-l4） | bin 分类 + 弧度残差 |
| 分配方式 | IoU assigner（本讲 4.4） | chamfer 距离匹配（无 assigner） |

#### 4.2.4 代码实践

**实践目标**：走通 `encode →（把目标当完美预测）→ decode` 的往返，量化误差，并亲眼确认「z 列的 h/2 偏差」这个锚点陷阱。

**操作步骤**（以下为示例代码，请保存为独立脚本在装好 mmdet3d 的环境运行）：

```python
# 示例代码（非项目原有文件）
import torch
from mmdet3d.registry import TASK_UTILS
from mmdet3d.structures import DepthInstance3DBoxes

# 配置与单测同源: tests/.../test_partial_bin_based_box_coder.py
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

gt = DepthInstance3DBoxes([
    [0.8308, 4.1168, -1.2035, 2.2493, 1.8444, 1.9245, 1.6486],
    [2.3002, 4.8149, -1.2442, 0.5718, 0.8629, 0.9510, 1.6030],
    [-1.1477, 1.8090, -1.1725, 0.6965, 1.5273, 2.0563, 0.0552]])
labels = torch.tensor([0, 1, 2])

center_t, size_cls_t, size_res_t, dir_cls_t, dir_res_t = coder.encode(gt, labels)

# 把 encode 的目标伪装成网络的“完美预测”
bbox_out = dict(
    center=center_t.unsqueeze(0),          # (1, N, 3)
    size_class=torch.nn.functional.one_hot(size_cls_t, 10).float().unsqueeze(0) * 10,
    size_res=size_res_t.unsqueeze(0),      # (1, N, 10, 3)
    dir_class=torch.nn.functional.one_hot(dir_cls_t, 12).float().unsqueeze(0) * 10,
    dir_res=dir_res_t.unsqueeze(0))        # (1, N, 12)
decoded = coder.decode(bbox_out)[0]        # (N, 7)

err_raw = (decoded - gt.tensor).abs()
print('原始逐列绝对误差:\n', err_raw)
print('z 列误差 / 每框高度 h:',
      (err_raw[:, 2] / gt.tensor[:, 5]).tolist())   # 预期恒等于 0.5

# 修正锚点后 re-check: decode 输出的 z 是几何中心, 换回底面中心
z_fixed = decoded.clone(); z_fixed[:, 2] -= decoded[:, 5] * 0.5
print('锚点修正后最大误差:', (z_fixed - gt.tensor).abs().max().item())
```

**需要观察的现象**：

1. `err_raw` 的 x/y/w/l/h/yaw 六列误差近似为 0（尺寸残差、bin 残差被完整还原）；唯独 **z 列系统性偏大**。
2. `z 列误差 / h` 每项都是 `0.5`——因为 `encode` 存的是几何中心（`gravity_center = z_bottom + h/2`），而 `gt.tensor` 的 z 是底面中心。
3. 锚点修正后最大误差应回落到 \(10^{-6}\) 量级（float 精度）。

**预期结果**：如上三条。若第 2 条不是 0.5，说明环境内代码与本仓库版本不一致。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`num_dir_bins=1` 时（ScanNet 配置，[votenet_8xb8_scannet-3d.py:L10-L14](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/votenet/votenet_8xb8_scannet-3d.py#L10-L14)），角度编码退化成什么？

**答案**：bin 宽 \(\Delta = 2\pi\)，任何角折叠后都落在第 0 个 bin，`dir_class` 恒为 0、`dir_res` 等于原角度；加上 `with_rot=False` 时 encode 干脆给全零目标、decode 直接返回 0（[partial_bin_based_bbox_coder.py:L58-L63](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/partial_bin_based_bbox_coder.py#L58-L63) 与 [L95-L96](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/partial_bin_based_bbox_coder.py#L95-L96)），对应 ScanNet 轴对齐框没有可学的朝向。

**练习 2**：为什么 `size_class_target` 直接复用了 `gt_labels_3d`，而不是让网络自己学尺寸簇？

**答案**：`mean_sizes` 本来就是**按类别**在训练集上统计的平均尺寸（每类一行），「尺寸簇」与「语义类别」天然一一对应；复用类别标签使尺寸分类分支可以与语义分类分支共享监督，且推理时 `argmax(size_class)` 同时给出了「用哪一行的均值做基准」（[partial_bin_based_bbox_coder.py:L99-L106](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/partial_bin_based_bbox_coder.py#L99-L106) 的 `index_select`）。

**练习 3**：`split_pred` 中 `dir_res = dir_res_norm * (π / num_dir_bins)`，`size_res = size_res_norm * mean_sizes`，为什么要做这两次乘法？

**答案**：网络最后一层输出的数值量纲统一（无界回归），乘上 bin 宽 / 类均值尺寸后，才把归一化预测换算回弧度与米；这也与训练侧 `encode` 产出的米制残差、以及 [vote_head.py:L702](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/vote_head.py#L702) 把目标除以 bin 宽（换到归一化域算 SmoothL1）互为逆操作。

---

### 4.3 3D IoU 计算器：assigner 的「眼睛」

#### 4.3.1 概念说明

Assigner 判正负的依据是重叠度，而「3D 框怎么算重叠度」本身有三种精度/速度取舍，全部实现在 `iou3d_calculator.py`，都以 `iou_calculator=dict(type='...')` 的形式挂在 assigner 配置里：

| 计算器 | 算法 | 典型用户 |
| --- | --- | --- |
| `BboxOverlapsNearest3D` | 框投影成 `nearest_bev` 轴对齐矩形后算 **2D** IoU（忽略高度与旋转） | SECOND / PointPillars / PartA2 / MVXNet（KITTI 系） |
| `BboxOverlaps3D` | 调用框对象的 `overlaps`，真 3D 体积 IoU（BEV 旋转 IoU × 高度交集） | nuScenes 系配置 / 两阶段 RoI 分配 |
| `AxisAlignedBboxOverlaps3D` | 6 维 `(x1,y1,z1,x2,y2,z2)` 框的纯张量 IoU/GIoU，可带 batch 维 | 3DSSD、室内无 yaw 场景 |

为什么室外 anchor 分配敢用「最粗」的 nearest_bev 版本？因为 u5-l4 讲过 anchor 阵列是 `rotations=[0, 1.57]` 双朝向铺的，IoU 只用来**粗筛正负**，编码精修交给 coder；而 nearest 版本全程是纯 torch 张量运算，84 万 anchor × 若干 gt 的矩阵算得动。

#### 4.3.2 核心流程

`bbox_overlaps_nearest_3d(bboxes1, bboxes2)` 的执行过程：

```
输入两个 (N, 7+C) / (M, 7+C) 裸张量
  → get_box_type(coordinate) 解析坐标系 → LiDARInstance3DBoxes 等
  → 包装成框对象, 取 .nearest_bev   (N, 4) / (M, 4)   [x1, y1, x2, y2]
  → 复用 mmdet 的 bbox_overlaps 算 2D IoU / IoF
  → 返回 (N, M)
```

注意输入输出方向：`overlaps = iou_calculator(gt_bboxes, priors)` 得到 **(num_gt, num_anchor)** 矩阵（4.4 的 assign 会这样调用）。

#### 4.3.3 源码精读

[iou3d_calculator.py:L10-L48](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/ops/iou3d_calculator.py#L10-L48) —— `BboxOverlapsNearest3D`：构造参数只有一个 `coordinate`（默认 `'lidar'`），`__call__` 直接转发到同名自由函数；docstring 明说「先在 BEV 找最近 2D 框再算 2D IoU」。

[iou3d_calculator.py:L130-L145](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/ops/iou3d_calculator.py#L130-L145) —— 实现体：第 132 行 `get_box_type` 按（左闭右开）版本约束解析坐标系到框类；第 134-135 行把裸张量包成框对象；第 140-141 行取 `nearest_bev`（u2-l2 讲过的属性：旋转框的 BEV 外接矩形）；第 143 行复用 mmdet 的 `bbox_overlaps` 完成 2D 计算。注释里点明 torch 版比 numpy 版在 CUDA 上快约 10 倍。

[iou3d_calculator.py:L148-L176](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/ops/iou3d_calculator.py#L148-L176) —— `bbox_overlaps_3d`：同样先包框对象，但第 176 行调用 `bboxes1.overlaps(bboxes1, bboxes2, mode=mode)` 走框类自带的真 3D IoU（即 u2-l2 讲过的「BEV 旋转 IoU × 高度交集」CUDA 实现），docstring 注明暂不支持 aligned 模式。

[iou3d_calculator.py:L210-L329](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/ops/iou3d_calculator.py#L210-L329) —— `axis_aligned_bbox_overlaps_3d`：不依赖任何框类，直接对 `(…, 6)` 张量算 `overlap / union`（第 281-321 行），并支持 `giou`（第 324-329 行用最小外接长方体体积惩罚）。batch 维广播（第 267-268 行的 `shape[:-2]` 断言）让它能被 3DSSD 的候选点分配直接调用。

#### 4.3.4 代码实践

**实践目标**：对同一对框，对比「nearest_bev 近似 IoU」与「真 3D IoU」的数值差，建立对精度的直觉。

**操作步骤**（示例代码）：

```python
# 示例代码（非项目原有文件）
import torch
from mmdet3d.registry import TASK_UTILS
from mmdet3d.structures import LiDARInstance3DBoxes

near = TASK_UTILS.build(dict(type='BboxOverlapsNearest3D', coordinate='lidar'))
full = TASK_UTILS.build(dict(type='BboxOverlaps3D', coordinate='lidar'))

# 框 A: 大而扁的底面 + 矮高度; 框 B: 与 A 底面部分重叠但高度完全错开
A = LiDARInstance3DBoxes([[0, 0, -1, 4.0, 2.0, 1.0, 0.0]])
B = LiDARInstance3DBoxes([[1, 0, 5, 4.0, 2.0, 1.0, 0.0]])   # z 抬高 6m

print('nearest(近似):', near(A.tensor, B.tensor))   # 只看 BEV → 仍有重叠
print('真 3D     :', full(A, B.tensor))             # 高度不相交 → 0
```

**需要观察的现象**：nearest 版本给出非零 IoU（两框 BEV 投影重叠），真 3D 版本给出 0（高度区间 \([-1, 0]\) 与 \([5, 6]\) 不相交）。

**预期结果**：如上；再交换 `mode='iof'` 观察分母切换。`BboxOverlaps3D` 的调用要求两边都是可被 `box_type` 包装的张量（内部会各自重新包框）。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`bbox_overlaps_nearest_3d` 里为什么先把框转成 `nearest_bev` 而不是 `bev`？

**答案**：`bev` 仍带旋转角（u2-l2：返回 `(N, 5)` 的旋转 BEV 框），后续还要做旋转 IoU；`nearest_bev` 直接给轴对齐 `(N, 4)`，可以直接喂 mmdet 的通用 2D `bbox_overlaps`，把问题彻底降维成轴对齐 2D IoU（[iou3d_calculator.py:L140-L144](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/ops/iou3d_calculator.py#L140-L144)），代价是高估斜置框的重叠。

**练习 2**：assign 的调用是 `iou_calculator(gt_bboxes, priors)`，得到的矩阵形状是什么？行列分别是谁？

**答案**：`(num_gt, num_anchor)`——行是 gt、列是 anchor（[max_3d_iou_assigner.py:L140](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/assigners/max_3d_iou_assigner.py#L140)），后续按行求 max 是「每个 gt 找最佳 anchor」、按列求 max 是「每个 anchor 找最近 gt」。

---

### 4.4 Max3DIoUAssigner：四步分配准则与 AssignResult

#### 4.4.1 概念说明

有了 IoU 矩阵，分配问题就是：**84 万个 anchor，每个该算正样本、负样本还是不管？** `Max3DIoUAssigner` 的思路是「最大 IoU 匹配 + 双阈值」，也就是 RCNN 时代的经典 MaxIoU 准则的 3D 版。它的类定义只有一行身价说明——继承 mmdet 的 `MaxIoUAssigner`，只重写「准备阶段」：

[max_3d_iou_assigner.py:L10-L19](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/assigners/max_3d_iou_assigner.py#L10-L19) —— `class Max3DIoUAssigner(MaxIoUAssigner)`；docstring 约定每个 anchor（这里叫 proposal/bbox）将被赋值为 `-1` 或非负整数。

三个阈值各挡一件事：

- `pos_iou_thr`：IoU ≥ 它 → 正样本候选；
- `neg_iou_thr`：与**所有** gt 的 IoU 都 < 它 → 负样本（背景）；
- 两者之间（`neg_iou_thr ≤ IoU < pos_iou_thr`）→ **忽略**，既不算正也不算负；
- `min_pos_iou`：兜底阈值——第 4 步「每个 gt 把自己最好的 anchor 拉成正样本」时，若该 anchor IoU 连 `min_pos_iou` 都不到，就不拉，防止「整帧没有够格 anchor 时硬凑低质正样本」。

为什么允许「忽略带」？落在中间的 anchor 位置猜对了一半，强行标负会教网络「这里没车」，强行标正又监督错误，忽略最稳妥。KITTI 配置把 Car 的带子开得窄（0.45~0.6）、行人开得宽（0.2~0.35），因为行人框小、IoU 天然难高。

#### 4.4.2 核心流程

docstring 给出的四步准则（顺序有讲究）：

```
1. 全部 anchor 先置为"背景/未定" (gt_inds 全 0 或 -1)
2. 与所有 gt 的 IoU < neg_iou_thr 的 anchor → 负样本 (gt_inds = 0)
3. 对每个 anchor: 若与最近 gt 的 IoU ≥ pos_iou_thr → 正样本, 记 gt 下标
4. 对每个 gt: 把与它 IoU 最大的 anchor(s) 也拉成正样本   ← match_low_quality
   (受 min_pos_iou 下限约束; gt_max_assign_all 决定并列时全拉还是拉一个)
```

第 4 步是「低质量匹配」：极端情况下某个 gt 的所有 anchor IoU 都不到 `pos_iou_thr`，若不做第 4 步它就永远没有正样本、永远学不会。单阶段检测器（RPN/SECOND）需要它，某些两阶段精修阶段会关掉（`match_low_quality=False`）。

产出 `AssignResult` 三字段：

| 字段 | 形状 | 含义 |
| --- | --- | --- |
| `gt_inds` | (num_anchor,) | 0=负样本；正整数 k=正样本、对应第 k-1 个 gt；-1=忽略/未定 |
| `max_overlaps` | (num_anchor,) | 每个 anchor 与所有 gt 的最大 IoU |
| `labels` | (num_anchor,) | 每个 anchor 的类别标签（负样本对应背景），由 `gt_labels` 查表填充 |

（gt_inds 的取值语义由 [max_3d_iou_assigner.py:L15-L19](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/assigners/max_3d_iou_assigner.py#L15-L19) 与四步 docstring 的第 2 步「< neg_iou_thr 置 0」共同约定；4.1.3 的调用方 `PseudoSampler` 也按 `>0` / `==0` 二分消费它。）

#### 4.4.3 源码精读

[max_3d_iou_assigner.py:L45-L65](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/assigners/max_3d_iou_assigner.py#L45-L65) —— 重写的 `__init__`：签名与 mmdet 版相同，唯一实质差别在第 65 行 `iou_calculator` 经 **mmdet3d 的** `TASK_UTILS.build` 构建——这样才能路由到 4.3 的三个 3D 计算器（默认值 `BboxOverlaps2D` 只是占位，实际配置都会显式覆盖）。

[max_3d_iou_assigner.py:L118-L127](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/assigners/max_3d_iou_assigner.py#L118-L127) —— `assign` 的取数约定：gt 从 `gt_instances.bboxes_3d`（框对象）取；anchor 优先 `pred_instances.priors`，没有则用 `pred_instances.bboxes_3d.tensor`（两阶段第二段把上一层预测框当 priors）；ignore 框走 `gt_instances_ignore.bboxes_3d`。

[max_3d_iou_assigner.py:L129-L139](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/assigners/max_3d_iou_assigner.py#L129-L139) —— `gpu_assign_thr` 的 CPU 回退：gt 数量超过阈值时把 priors/gt 整体搬到 CPU 再分配。语义分割数据一帧可能有上千 gt，`(num_gt, num_anchor)` 的 IoU 矩阵会撑爆显存，这个开关就是为此准备的。

[max_3d_iou_assigner.py:L140-L154](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/assigners/max_3d_iou_assigner.py#L140-L154) —— 主线三连：第 140 行算 `overlaps`；第 142-152 行可选的 ignore 处理（与 `gt_bboxes_ignore` 算 IoF，超过 `ignore_iof_thr` 的 anchor 所在列的 overlaps 整列置 -1，等效逐出所有竞争）；第 154 行把 overlaps 交给 `assign_wrt_overlaps`——**四步准则的实现体继承自 mmdet 父类**，本仓库源码里看不到它的函数体，但其语义就是上面 docstring 承诺的四步。

[max_3d_iou_assigner.py:L155-L160](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/assigners/max_3d_iou_assigner.py#L155-L160) —— 若在 CPU 上分配，把 `gt_inds / max_overlaps / labels` 三个字段搬回原设备——这一行同时是 `AssignResult` 三字段的直接证据。

多 assigner 支持：`Anchor3DHead` 的 `assign_per_class=True` 时，每类一个 assigner、各自跑完再拼表（[train_mixins.py:L222-L238](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/train_mixins.py#L222-L238)，SECOND KITTI 三个 assigner 的用法）。前提是 anchor 生成时 `reshape_out=False`，让每类的 anchor 可以整体切片（u5-l4 讲过）。

#### 4.4.4 代码实践

**实践目标**：手工构造一个可控的小场景，直接调用 `Max3DIoUAssigner.assign`，验证四步准则与三个阈值的实际效果（本讲规格指定的核心实践）。

**操作步骤**（示例代码）：

```python
# 示例代码（非项目原有文件）
import torch
from mmengine.structures import InstanceData
from mmdet3d.registry import TASK_UTILS
from mmdet3d.structures import LiDARInstance3DBoxes

assigner = TASK_UTILS.build(dict(
    type='Max3DIoUAssigner',
    iou_calculator=dict(type='BboxOverlapsNearest3D'),
    pos_iou_thr=0.5, neg_iou_thr=0.3, min_pos_iou=0.3))

# gt: 一辆车 (w=4, l=2, h=1.5, BEV 投影 4x2)
gt = LiDARInstance3DBoxes([[10., 0., -1., 4., 2., 1.5, 0.]])
gt_inst = InstanceData(bboxes_3d=gt, labels_3d=torch.tensor([0]))

# anchors: 4 个, 与 gt 的 BEV 重叠度被刻意摆成 高/中/低/无
anchors = torch.tensor([
    [10., 0., -1., 4., 2., 1.5, 0.],    # 完全重合 → IoU 1.0
    [12., 0., -1., 4., 2., 1.5, 0.],    # 部分重叠 → 0.3x (忽略带?)
    [14., 0., -1., 4., 2., 1.5, 0.],    # 少量重叠 → 0.1x
    [40., 0., -1., 4., 2., 1.5, 0.]])   # 完全不重叠 → 0
pred_inst = InstanceData(priors=anchors)

res = assigner.assign(pred_inst, gt_inst)
print('gt_inds       :', res.gt_inds)        # 预期 [1, ?, 0, 0]
print('max_overlaps  :', res.max_overlaps)
print('labels        :', res.labels)
print('正样本数:', int((res.gt_inds > 0).sum()),
      '| 负样本数:', int((res.gt_inds == 0).sum()),
      '| 忽略数 :', int((res.gt_inds < 0).sum()))
```

**需要观察的现象**：

1. 第 1 个 anchor `gt_inds=1`（正样本，指向 gt#0）。
2. 第 3、4 个 anchor `gt_inds=0`（负样本：IoU < neg_iou_thr）。
3. 第 2 个 anchor 的 IoU 若落在 \([0.3, 0.5)\) 则为忽略（-1）；若因 `nearest_bev` 外接矩形高估跨过 0.5 则也会变正——打印 `max_overlaps[1]` 判断。
4. `labels` 中正样本位置为 0（该 gt 的类别），负样本位置为背景值。

**预期结果**：正/负/忽略三类计数与手算 IoU 一致；把 `pos_iou_thr` 改成 `0.6` 重跑，第 2 个 anchor 一定落入忽略带。待本地验证（`nearest_bev` 对正放框无高估，IoU 可手工按矩形交并比核算）。

#### 4.4.5 小练习与答案

**练习 1**：SECOND KITTI 为什么给 Car 配 `pos_iou_thr=0.6`、给 Pedestrian 配 `0.35`？

**答案**：见 [second_hv_secfpn_kitti.py:L60-L83](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/second_hv_secfpn_kitti.py#L60-L83)。BEV 下行人框约 0.6m×0.8m，位置偏 0.3m IoU 就掉一半，若沿用 0.6 阈值行人几乎没有正样本；Car 框大、IoU 天然高，抬高阈值可以只留高质量正样本、提高定位监督精度。

**练习 2**：`min_pos_iou=0` 且某 gt 与所有 anchor 的 IoU 都低于 `pos_iou_thr`，这个 gt 还有正样本吗？

**答案**：有——第 4 步（`match_low_quality=True` 时）会把与该 gt IoU 最大的 anchor 拉成正样本；`min_pos_iou=0` 表示不设下限。若 `min_pos_iou>0` 且最大 IoU 低于它，该 gt 才真正拿不到任何正样本。

**练习 3**：`gpu_assign_thr` 触发 CPU 回退后，为什么还要把结果搬回 GPU（第 155-159 行），而不是直接返回？

**答案**：分配结果 `gt_inds/max_overlaps/labels` 马上要在 GPU 上与 anchor、特征张量一起做散射与损失计算（4.1.3 第 ④ 步），留在 CPU 会引入隐式同步与搬运开销；回退只是为省下大矩阵的显存，计算完应回到原设备。

---

### 4.5 Sampler 与 SamplingResult：从「分配」到「样本」

#### 4.5.1 概念说明

`AssignResult` 只回答「每个 anchor 是什么身份」，而损失函数实际吃的是**下标集合**：哪些进正样本损失、哪些进负样本损失、要不要控制正负比例。从分配结果到下标集合的这一步由 Sampler 完成。mmdet3d 本地实现了两个：

- **`PseudoSampler`**：不做任何抽样——正样本全要、负样本全要。u5-l4 说过 SECOND/PointPillars 的分类损失是 `FocalLoss`（自带难例降权），负样本再多也不怕，所以不需要真采样器。
- **`IoUNegPiecewiseSampler`**：继承 mmdet 的 `RandomSampler`，把负样本按 IoU 区间**分段配额抽取**（SSN 用）：接近阈值的「难负例」多抽、远处的「简单负例」少抽，兼顾难例挖掘与分布覆盖。

`SamplingResult`（从 mmdet 借用、在 [samplers/\_\_init\_\_.py:L2-L15](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/samplers/__init__.py#L2-L15) 统一转出口）是两者的产物容器。

#### 4.5.2 核心流程

```
SamplingResult(sample(assign_result, pred_instances, gt_instances))
  pos_inds            正样本 anchor 下标        (P,)
  neg_inds            负样本 anchor 下标        (Q,)
  ── 派生属性(惰性) ──
  pos_bboxes          = priors[pos_inds]        (P, 7)   回归的"源框"
  pos_gt_bboxes       = gt_bboxes[gt_inds[pos]-1] (P, 7)  回归的"目标框"
  pos_assigned_gt_inds= gt_inds[pos_inds] - 1    (P,)     查类别标签用
  pos_gt_labels       = gt_labels[pos_assigned_gt_inds]
```

关键减一：`gt_inds` 里的正整数是 **gt 下标 + 1**（0 被背景占用），所以派生属性里都要 `-1` 才能回去索引 gt。这也是 `train_mixins.py:311` 写成 `gt_labels_3d[sampling_result.pos_assigned_gt_inds]` 的原因。

#### 4.5.3 源码精读

[pseudosample.py:L27-L61](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/samplers/pseudosample.py#L27-L61) —— `PseudoSampler.sample`：第 47-50 行两条 `torch.nonzero` 就是全部逻辑——`gt_inds > 0` 的下标为 `pos_inds`、`== 0` 的为 `neg_inds`（再次印证 gt_inds 的取值语义）；第 52 行 `gt_flags` 全零（没有 add_gt_as_proposals 时用）；第 53-60 行直接以 `priors`、`gt_bboxes`、`assign_result` 构造 `SamplingResult`。它的 `_sample_pos/_sample_neg` 故意抛 `NotImplementedError`（第 19-25 行）——因为这个采样器根本不采样。

[iou_neg_piecewise_sampler.py:L15-L51](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/samplers/iou_neg_piecewise_sampler.py#L15-L51) —— `IoUNegPiecewiseSampler` 构造：`neg_piece_fractions` 与 `neg_iou_piece_thrs` 等长（如 SSN 的 `[0.5, 0.5]` 与 `[0.3, 0.6]`：一半配额给 IoU<0.3 的易负例、一半给 0.3~0.6 的难负例，具体值以 SSN config 为准）。

[iou_neg_piecewise_sampler.py:L53-L80](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/samplers/iou_neg_piecewise_sampler.py#L53-L80) —— `_sample_pos` 与 `_sample_neg` 的开头：正样本直接随机抽（59-62 行）；负样本第 75 行读 `assign_result.max_overlaps[neg_inds]`——这就是 `AssignResult.max_overlaps` 字段的真实消费点，按 IoU 分段（77-80 行的循环）逐段凑配额。

[train_mixins.py:L293-L311](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/train_mixins.py#L293-L311) —— `SamplingResult` 三大派生字段的消费现场：`pos_bboxes → pos_gt_bboxes` 喂给 coder（第 294-295 行）、`pos_assigned_gt_inds` 查回类别标签（第 310-311 行）。注意 `pos_gt_bboxes` 是裸张量（4.1.3 讲过的降级）。

#### 4.5.4 代码实践

**实践目标**：把 4.4.4 得到的 `assign_result` 用 `PseudoSampler` 打包，逐字段打印 `SamplingResult`，亲手验证派生关系。

**操作步骤**（示例代码，接 4.4.4 的变量）：

```python
# 示例代码（非项目原有文件）
from mmdet3d.models.task_modules import PseudoSampler

sampler = PseudoSampler()
sr = sampler.sample(res, pred_inst, gt_inst)

print('pos_inds            :', sr.pos_inds)
print('neg_inds            :', sr.neg_inds)
print('pos_bboxes          :', sr.pos_bboxes)            # == anchors[pos_inds]
print('pos_gt_bboxes       :', sr.pos_gt_bboxes)          # == gt.tensor[gt_inds[pos]-1]
print('pos_assigned_gt_inds:', sr.pos_assigned_gt_inds)   # == gt_inds[pos_inds] - 1

# 一致性断言（应全部通过）
assert torch.equal(sr.pos_bboxes, anchors[sr.pos_inds])
assert torch.equal(sr.pos_gt_bboxes,
                   gt.tensor[res.gt_inds[sr.pos_inds] - 1])
```

**需要观察的现象**：`pos_assigned_gt_inds` 全为 0（只有一个 gt）；`pos_gt_bboxes` 每行都等于 gt 的第 0 行；三个断言全部通过。

**预期结果**：如上；若把 4.4.4 的 `pos_iou_thr` 调高使正样本数为 0，`pos_inds` 为空张量、`sr.pos_bboxes.shape=(0, 7)`——`anchor_target_single_assigner` 里 `if len(pos_inds) > 0` 的守卫（[train_mixins.py:L293](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/train_mixins.py#L293)）就是为这种情况准备的。待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：为什么 SECOND/PointPillars 敢用「不采样」的 `PseudoSampler`，而 Faster R-CNN 一类要 `RandomSampler`？

**答案**：SECOND 的分类损失是 `FocalLoss`（u5-l4），对海量易负例自动降权，负样本全量进损失也不失衡；Faster R-CNN 用普通交叉熵，必须靠采样把正负比例压到 1:1 左右。选 sampler 要看损失函数的自平衡能力。

**练习 2**：`gt_flags` 参数是干什么用的？

**答案**：`add_gt_as_proposals=True` 的采样器会把 gt 本身追加进 priors 当 proposal（两阶段常用，保证每个 gt 至少有一个完美正样本），`gt_flags` 标记哪些 priors 是追加的 gt。`PseudoSampler` 恒置全零（[pseudosample.py:L52](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/samplers/pseudosample.py#L52)），因为它从不追加。

**练习 3**：`IoUNegPiecewiseSampler._sample_neg` 为什么最后一段（`piece_inds == neg_piece_num - 1`）要单独处理配额？

**答案**：见 [iou_neg_piecewise_sampler.py:L77-L80](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/samplers/iou_neg_piecewise_sampler.py#L77-L80)：前几段按 `num_expected * fraction` 取整配额，会有取整损耗；最后一段直接拿「剩余全部配额」（`num_expected - len(neg_inds_choice)`），保证总数精确凑满，同时它的 IoF 上界天然是 `neg_iou_thr`（分段阈值最后一个）。

---

## 5. 综合实践

**任务：用 20 个随机 anchor 和 2 个 gt，把本讲四个组件串成一条迷你版「目标生产线」，并用 `DeltaXYZWLHRBBoxCoder` 的编码-解码闭环自检。**

```python
# 示例代码（非项目原有文件），保存为 mini_pipeline.py 运行
import torch
from mmengine.structures import InstanceData
from mmdet3d.registry import TASK_UTILS
from mmdet3d.structures import LiDARInstance3DBoxes

torch.manual_seed(0)

# ── 场景 ─────────────────────────────────────────────
gt = LiDARInstance3DBoxes([
    [12., 2., -1., 4.0, 1.8, 1.5, 0.3],
    [20., -4., -1., 1.0, 0.7, 1.7, -1.2]])
gt_inst = InstanceData(bboxes_3d=gt, labels_3d=torch.tensor([2, 0]))  # Car/Ped

anchors = torch.cat([
    gt.tensor[:1] + torch.rand(1, 7) * 0.4 - 0.2,          # 贴着 gt#0 的好 anchor
    torch.rand(19, 7) * torch.tensor([40., 40., 0., 0., 0., 0., 0.])
    + torch.tensor([5., -20., -1., 4., 1.8, 1.5, 0.])])    # 随机远 anchor
pred_inst = InstanceData(priors=anchors)

# ── ① IoU → ② assign → ③ sample ─────────────────────
assigner = TASK_UTILS.build(dict(
    type='Max3DIoUAssigner',
    iou_calculator=dict(type='BboxOverlapsNearest3D'),
    pos_iou_thr=0.5, neg_iou_thr=0.3, min_pos_iou=0.3))
res = assigner.assign(pred_inst, gt_inst)

from mmdet3d.models.task_modules import PseudoSampler
sr = PseudoSampler().sample(res, pred_inst, gt_inst)
print(f'正 {len(sr.pos_inds)} | 负 {len(sr.neg_inds)} | '
      f'忽略 {len(anchors) - len(sr.pos_inds) - len(sr.neg_inds)}')

# ── ④ coder encode→decode 闭环自检（回归目标可完美还原） ──
coder = TASK_UTILS.build(dict(type='DeltaXYZWLHRBBoxCoder'))
if len(sr.pos_inds) > 0:
    deltas = coder.encode(sr.pos_bboxes, sr.pos_gt_bboxes)   # 正样本回归目标
    back = coder.decode(sr.pos_bboxes, deltas)               # 用同一 anchor 解回
    print('encode→decode 最大误差:', (back - sr.pos_gt_bboxes).abs().max().item())
    print('正样本类别:', gt_inst.labels_3d[sr.pos_assigned_gt_inds].tolist())
```

**验收标准**：

1. 第一个 anchor（gt#0 扰动 ±0.2 生成）大概率是正样本；19 个随机 anchor 绝大多数为负或忽略。
2. `encode→decode` 最大误差在 \(10^{-6}\) 量级——这是 u5-l4 讲过的「严格互逆」契约，本讲从 sampling_result 的角度再次验证。
3. 把 `neg_iou_thr` 从 0.3 降到 0.05 重跑，「忽略」数量应显著减少、「负」显著增多；总样本数不变（分配不增删 anchor，只改身份）。

整体运行结果：待本地验证。

## 6. 本讲小结

- **task_modules 是「训练目标生产线」**：IoU 计算器（重叠度）→ Assigner（定身份）→ Sampler（挑下标）→ BBox Coder（编码回归目标），全部无参数、注册在 `TASK_UTILS`，调用现场在 [train_mixins.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/train_mixins.py) 的 `anchor_target_single_assigner`。
- **两种编码哲学**：`DeltaXYZWLHRBBoxCoder` 回归「相对 anchor 的连续量」，前提是有好 anchor；`PartialBinBasedBBoxCoder` 把尺寸/朝向拆成「分类 + 残差」，为无 anchor 的 VoteNet 设计，中心用 `gravity_center`（几何中心）锚点，decode 端由 `box_type_3d` 默认 origin 闭环。
- **`Max3DIoUAssigner` 只重写准备阶段**（取 `bboxes_3d/priors`、CPU 回退、ignore 处理），四步准则与 `assign_wrt_overlaps` 继承自 mmdet 的 `MaxIoUAssigner`；双阈值之间是「忽略带」，第 4 步低质量匹配保证每个 gt 有机会获得正样本。
- **`AssignResult.gt_inds` 语义**：0 负样本、正整数 k 指向第 k-1 个 gt（消费时要减一）、-1 忽略；`max_overlaps` 被 `IoUNegPiecewiseSampler` 消费做分段采样。
- **`SamplingResult` 是下标容器**：`pos_inds/neg_inds` 为源，`pos_bboxes/pos_gt_bboxes/pos_assigned_gt_inds` 为派生属性；在 anchor 训练路径中 `pos_gt_bboxes` 已是裸张量。
- **IoU 计算器三档精度**：nearest_bev 2D 近似（KITTI anchor 分配）、真 3D 体积 IoU（`overlaps` CUDA 算子）、轴对齐 6 维（室内/3DSSD），精度与速度按场景取舍。

## 7. 下一步学习建议

本讲补齐了 u5-l4 留下的最后一块拼图，至此 anchor-based 主线（u5-l1 ~ u5-l4 + 本讲）已经完整。接下来两条路：

1. **推荐先读 u5-l7（3D 损失函数与 NMS）**：本讲的 `AssignResult`/`bbox_targets` 产出马上要被 `FocalLoss`、`SmoothL1Loss` 消费，且 rotated IoU 损失会反过来复用本讲 4.3 的 3D IoU 思想，衔接最紧。
2. **或跳到 u6-l2（VoteNet）**：本讲 4.2 的 `PartialBinBasedBBoxCoder` + chamfer 距离匹配正是 VoteNet 的核心训练机制，读完本讲再读 VoteHead 会非常顺畅。

源码阅读补充：想看「多 assigner 逐类分配」的完整拼表逻辑，可精读 [train_mixins.py:L150-L238](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/train_mixins.py#L150-L238)；想看两阶段第二段如何把预测框当 priors 再分配，可关注 u6-l4 将讲的 `PartA2BBoxHead` 与 [parta2.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/parta2.py)。

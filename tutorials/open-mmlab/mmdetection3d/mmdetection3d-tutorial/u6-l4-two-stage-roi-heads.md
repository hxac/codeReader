# u6-l4 两阶段检测器与 RoI Heads：Part-A2 与 PV-RCNN

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `TwoStage3DDetector` 如何把「RPN 出提案」和「RoI head 精修」两段串成完整的 `loss` / `predict` 链路，以及 `train_cfg` 的 `rpn` / `rpn_proposal` / `rcnn` 三段配置分别注入到哪里。
2. 理解 `roi_heads` 子系统的三层组织：`Base3DRoIHead` 基类、`roi_extractors` 特征提取器、`bbox_heads` 打分回归头。
3. 剖析 Part-A2：part 分支（逐体素的前景分割 + 部位回归）与 RoI 分支（框内特征池化 + 稀疏卷积精修）如何通过 `feats_dict` 这个字典耦合在一起。
4. 理解 `Single3DRoIAwareExtractor` 封装的 `RoIAwarePool3d`：如何把每个 RoI 内部的点级特征重新栅格化（point → voxel）成 \(14^3\) 的规则网格。
5. 剖析 PV-RCNN：`VoxelSetAbstraction` 关键点采样分支、`Batch3DRoIGridExtractor` 网格点池化，以及它与 Part-A2 在 RoI 特征来源上的本质差异。

## 2. 前置知识

### 2.1 什么是两阶段检测

单元 5 讲的 SECOND、PointPillars、CenterPoint 都是**单阶段**检测器：骨干网络输出特征，检测头一次性在特征图上打分、回归出最终框。**两阶段**检测器则把任务拆成两步：

- **第一阶段（RPN，Region Proposal Network）**：一个粗糙的检测头，只负责产出少量「可能有物体」的候选框，称为 **proposal（提案）** 或 **RoI（Region of Interest，感兴趣区域）**。
- **第二阶段（RoI head）**：对每个 proposal **单独**提取局部特征（RoI 特征提取），再用更重的网络对它做二分类（是不是物体）和框精修（回归残差）。

直觉类比：单阶段是「一眼报答案」，两阶段是「先圈出可疑区域，再逐个仔细看」。两阶段换来了更精的框，代价是逐 RoI 计算带来的复杂度。

### 2.2 本讲要用的既有知识

| 来自哪一讲 | 需要记住的内容 |
| --- | --- |
| u4-l3 | `Det3DDataPreprocessor(voxel=True)` 产出 `voxels / coors / num_points / voxel_centers` 四件套，`coors` 第 0 列是帧号 |
| u5-l2 | 稀疏卷积与 `SparseConvTensor`（indices + features + spatial_shape），`SubMConv3d` 只在已有格点间卷积 |
| u5-l4 | `Anchor3DHead` 的 cls / reg / dir 三分支；`DeltaXYZWLHRBBoxCoder` 的 7 维编码 |
| u5-l6 | assigner（分配正负样本）与 sampler（采样）的分工；`SamplingResult` 的字段 |
| u6-l1 | SA 模块三步（采样—分组—聚合）、FPS 最远点采样、`StackedSAModuleMSG` |

另外补充一个术语：**canonical（规范坐标系）**。两阶段回归时，通常不以全局坐标为基准，而是先把 gt 框平移旋转到「以 RoI 中心为原点、RoI 朝向为 x 轴」的局部坐标系里再编码残差，这样回归目标的数值范围小、且与 RoI 位置无关。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [mmdet3d/models/detectors/two_stage.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/two_stage.py) | 两阶段检测器基类，编排 RPN 与 RoI head |
| [mmdet3d/models/detectors/parta2.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/parta2.py) | Part-A2 检测器壳：体素编码 → SparseUNet → SECOND → FPN |
| [mmdet3d/models/roi_heads/base_3droi_head.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/base_3droi_head.py) | RoI head 基类，继承自 mmdet 的 `BaseRoIHead` |
| [mmdet3d/models/roi_heads/part_aggregation_roi_head.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/part_aggregation_roi_head.py) | Part-A2 的 RoI head：part 聚合 |
| [mmdet3d/models/roi_heads/mask_heads/pointwise_semantic_head.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/mask_heads/pointwise_semantic_head.py) | Part-A2 的逐点语义头：前景分割 + 部位回归 |
| [mmdet3d/models/roi_heads/roi_extractors/single_roiaware_extractor.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/roi_extractors/single_roiaware_extractor.py) | RoI 池化提取器（封装 mmcv 的 `RoIAwarePool3d`） |
| [mmdet3d/models/roi_heads/bbox_heads/parta2_bbox_head.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/bbox_heads/parta2_bbox_head.py) | Part-A2 的 bbox head：稀疏卷积精修 |
| [mmdet3d/models/dense_heads/parta2_rpn_head.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/parta2_rpn_head.py) | Part-A2 / PV-RCNN 共用的 RPN 头（继承 `Anchor3DHead`） |
| [mmdet3d/models/detectors/pv_rcnn.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/pv_rcnn.py) | PV-RCNN 检测器（注册名 `PointVoxelRCNN`） |
| [mmdet3d/models/middle_encoders/voxel_set_abstraction.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/middle_encoders/voxel_set_abstraction.py) | PV-RCNN 的关键点特征聚合模块 |
| [mmdet3d/models/roi_heads/pv_rcnn_roi_head.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/pv_rcnn_roi_head.py) | PV-RCNN 的 RoI head |
| [mmdet3d/models/roi_heads/roi_extractors/batch_roigridpoint_extractor.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/roi_extractors/batch_roigridpoint_extractor.py) | PV-RCNN 的网格点 RoI 提取器 |
| [mmdet3d/models/roi_heads/bbox_heads/pv_rcnn_bbox_head.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/bbox_heads/pv_rcnn_bbox_head.py) | PV-RCNN 的 bbox head |
| [configs/_base_/models/parta2.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/parta2.py) / [configs/pv_rcnn/pv_rcnn_8xb2-80e_kitti-3d-3class.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/pv_rcnn/pv_rcnn_8xb2-80e_kitti-3d-3class.py) | 两个模型的完整配置 |

`roi_heads` 目录的组织正是本讲主线：`base_3droi_head.py` 定骨架，`roi_extractors/` 负责「从哪里取 RoI 特征」，`bbox_heads/` 负责「拿特征做什么」，`mask_heads/` 存放逐点语义类辅助头（名字带 mask 是沿袭 mmdet 的目录习惯，并不是真的做实例分割）。

## 4. 核心概念与源码讲解

### 4.1 两阶段骨架：TwoStage3DDetector 与 Base3DRoIHead

#### 4.1.1 概念说明

所有两阶段 3D 检测器（Part-A2、PV-RCNN、PointRCNN、H3DNet）都站在两个基类之上：

- `TwoStage3DDetector`：管「编排」。它把 `rpn_head` 与 `roi_head` 作为可插拔组件组装起来，规定 `loss` / `predict` 两个入口里两阶段的执行顺序，并把 `train_cfg` 的三段配置切开后分别塞给对应组件。
- `Base3DRoIHead`：管「第二阶段的内部结构」。它继承 mmdet 的 `BaseRoIHead`，约定一个 RoI head = `bbox_roi_extractor`（RoI 特征提取器）+ `bbox_head`（打分回归头），并可再挂 `mask_head` 等辅助分支。

理解这两个类的关键是：**它们自己几乎不做计算，只做装配与转发**。真正的算法差异全部在子类和 extractor / head 里。

#### 4.1.2 核心流程

**训练链路（`TwoStage3DDetector.loss`）：**

```text
batch_inputs_dict ──> extract_feat ──> feats_dict
        │
        ├─(1) rpn_head.loss_and_predict(feats_dict, gt, proposal_cfg)
        │        ├── RPN 损失（键名加 rpn_ 前缀）
        │        └── rpn_results_list：每帧一份 InstanceData
        │              {bboxes_3d(框对象), scores_3d, labels_3d, cls_preds}
        │
        └─(2) roi_head.loss(feats_dict, rpn_results_list, batch_data_samples)
                 ├── assigner 把 proposal 分给 gt，sampler 抽 128 个
                 ├── RoI 特征提取 + bbox_head 前向
                 └── RoI 损失（loss_cls / loss_bbox / loss_corner…）
```

**推理链路（`predict`）** 与之平行：`extract_feat` → `rpn_head.predict` → `roi_head.predict` → `add_pred_to_datasample` 写回 `Det3DDataSample.pred_instances_3d`。

**配置注入**：`train_cfg` 在 `__init__` 里被切成三份——

- `train_cfg.rpn` → 合入 `rpn_head` 的构建参数；
- `test_cfg.rpn` → 同上；
- `train_cfg.rcnn` 与 `test_cfg.rcnn` → 写入 `roi_head` 的构建参数（`rcnn` 即第二阶段的别名）。

#### 4.1.3 源码精读

先看组件装配。`TwoStage3DDetector.__init__` 构建 backbone / neck / rpn_head / roi_head 四件，注意 train_cfg 的切分注入：

- [mmdet3d/models/detectors/two_stage.py:L37-L44](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/two_stage.py#L37-L44)：若未显式给 `num_classes`，RPN 头默认单类——因为 RPN 只需要「是不是物体」，不需要区分类别。
- [mmdet3d/models/detectors/two_stage.py:L46-L51](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/two_stage.py#L46-L51)：`train_cfg.rcnn` / `test_cfg.rcnn` 被 update 进 roi_head 配置。

再看训练编排：

- [mmdet3d/models/detectors/two_stage.py:L88-L105](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/two_stage.py#L88-L105)：训练期 RPN 用 `rpn_proposal`（比测试更宽松：`nms_pre=9000, nms_post=512`）产生提案；`loss_and_predict` 一次前向同时拿损失与提案，避免重复计算。注意键名改写循环：RPN 的 `loss_cls` 会被改名为 `loss_rpn_cls`，防止与 RoI head 的同类键冲突。
- [mmdet3d/models/detectors/two_stage.py:L115-L117](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/two_stage.py#L115-L117)：`feats_dict` 与 `rpn_results_list` 一起交给 `roi_head.loss`——第一阶段的所有中间特征对第二阶段可见，这是两阶段共享特征的关键接口。

RoI head 基类的三个钩子：

- [mmdet3d/models/roi_heads/base_3droi_head.py:L27-L37](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/base_3droi_head.py#L27-L37)：`init_bbox_head` 同时构建 `bbox_roi_extractor` 与 `bbox_head`。
- [mmdet3d/models/roi_heads/base_3droi_head.py:L39-L50](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/base_3droi_head.py#L39-L50)：`init_assigner_sampler` 支持 assigner 为单个 dict 或 **dict 列表**（逐类 assigner，Part-A2 用到）。
- [mmdet3d/models/roi_heads/base_3droi_head.py:L52-L55](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/base_3droi_head.py#L52-L55)：`init_mask_head` 被显式置空——注释说明 `PartAggregationROIHead` 没有传统意义下的 mask head。

两个补充点：

1. RPN 结果契约由 `PartA2RPNHead.class_agnostic_nms` 固定：[mmdet3d/models/dense_heads/parta2_rpn_head.py:L347-L354](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/parta2_rpn_head.py#L347-L354) 把 NMS 后的框包装成 `InstanceData{bboxes_3d, scores_3d, labels_3d, cls_preds}`，其中 `cls_preds` 是逐类原始 sigmoid 分数——Part-A2 第二阶段 NMS 时还要用它。损失键改名见 [mmdet3d/models/dense_heads/parta2_rpn_head.py:L263-L271](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/parta2_rpn_head.py#L263-L271)。
2. 批内 RoI 的统一表示由 `bbox3d2roi` 完成：[mmdet3d/structures/ops/transforms.py:L27-L47](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/ops/transforms.py#L27-L47) 给每帧的框前面拼一列帧号，得到 `(总 RoI 数, 1+7)` 的张量，后续所有 extractor 都按「第 0 列是 batch id」的约定工作。

#### 4.1.4 代码实践

**实践目标**：验证 `train_cfg` 三段配置与代码注入点的对应关系。

**操作步骤**：

1. 打开 [configs/_base_/models/parta2.py:L122-L207](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/parta2.py#L122-L207)，找到 `train_cfg.rpn`（assigner 列表）、`train_cfg.rpn_proposal`（nms_pre=9000/nms_post=512）、`train_cfg.rcnn`（assigner + sampler + 阈值）和 `test_cfg.rpn` / `test_cfg.rcnn`。
2. 逐段回答：「这段配置会被 `two_stage.py` 的哪一行消费？」
3. 可选：在有完整依赖的环境里运行

   ```bash
   python tools/misc/print_config.py configs/parta2/parta2_hv_secfpn_8xb2-cyclic-80e_kitti-3d-3class.py
   ```

   在展开后的配置里搜 `rpn_proposal` 与 `rcnn`，确认继承链没有覆盖掉这些键。

**需要观察的现象**：`train_cfg.rpn` 与 `test_cfg.rpn` 出现在构建后的 `model.rpn_head` 侧（train_cfg / test_cfg 属性），而 `rcnn` 两段出现在 `model.roi_head` 侧。

**预期结果**：`model.roi_head.train_cfg` 含 `assigner`（3 个 Max3DIoUAssigner）、`sampler`（IoUNegPiecewiseSampler，num=128）和 `cls_pos_thr=0.75 / cls_neg_thr=0.25`；`model.rpn_head.train_cfg` 只有 assigner 相关字段。（配置文件本身已给出这些值；注入路径以源码为准。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 RPN 头默认 `num_classes=1` 也能工作，而第二阶段必须区分类别？

**答案**：RPN 的职责只是「这里有没有物体」，它的 FocalLoss 是类不可分（class-agnostic）的前景/背景判断，逐类分数留给第二阶段；`TwoStage3DDetector.__init__` 里 `rpn_head_num_classes is None` 时补 `num_classes=1` 正是这个约定。

**练习 2**：训练循环里同时出现 `loss_rpn_cls` 和 `loss_cls`，它们如何避免重名？

**答案**：`two_stage.py` L100-105 对 RPN 损失键做了改写——含 `loss` 且不含 `rpn` 的键统一加 `rpn_` 前缀；`PartA2RPNHead.loss_by_feat`（parta2_rpn_head.py L263-271）也在源头把 `loss_cls` 等改名成 `loss_rpn_cls`，双保险。

**练习 3**：`bbox3d2roi` 输出张量的第 0 列是什么？为什么必须由它而非 RoI 顺序来区分帧？

**答案**：第 0 列是帧号（batch index）。因为 RoI 提取器（如 `Single3DRoIAwareExtractor`）需要在「同一帧的点云里找属于该帧 RoI 的点」，帧内点与 RoI 的对应关系必须显式携带；且某些帧可能贡献 0 个 RoI，此时不能靠顺序推断。

### 4.2 PartA2 与 PartAggregationROIHead：part 分支与 RoI 分支的耦合

#### 4.2.1 概念说明

Part-A2（Part-Aware & Part-Aggregation）的核心洞察是：第一阶段的体素骨干已经为**每个非空体素**算出了特征，这些特征不仅可用于出框，还可以顺带回答两个更细的问题：

- **这个体素是不是落在某个物体内部（前景）？**——逐体素的前景分割（seg）。
- **这个体素在物体内部的相对位置在哪里？**——部位（part）回归：预测体素中心相对所属 gt 框中心的归一化偏移。

第二阶段再把这些「免费」的逐体素信息聚合到每个 RoI 内部，与骨干语义特征拼接后精修框。于是 `PartAggregationROIHead` 天生有**两条分支**：

- `semantic_head`（`PointwiseSemanticHead`）：吃骨干的逐体素特征，产出 `seg_preds`、`part_preds` 和 part 特征；
- bbox 分支：`seg_roi_extractor` + `bbox_roi_extractor` 两个池化器 + `PartA2BboxHead`。

#### 4.2.2 核心流程

```text
feats_dict（来自 PartA2.extract_feat）
 ├─ spatial_features ──> SECOND backbone ──> SECONDFPN ──> neck_feats ──> RPN
 ├─ seg_features（SparseUNet 解码路径的逐体素 16 维特征）
 │      └─> semantic_head ──> {seg_preds(N,1), part_preds(N,3), part_feats(N,4)}
 └─ voxels_dict（voxels/coors/num_points/voxel_centers）

roi_head.loss：
 (1) _semantic_forward_train：seg/part 两项损失（与 RoI 无关，作用于全部体素）
 (2) _assign_and_sample：proposal ↔ gt 分配采样，得 sampling_results
 (3) _bbox_forward_train：
      rois = bbox3d2roi(pos+neg 框)
      pooled_seg  = seg_roi_extractor(seg_features, voxel_centers, batch_inds, rois)  # max 池化
      pooled_part = bbox_roi_extractor(part_feats, voxel_centers, batch_inds, rois)   # avg 池化
      cls_score, bbox_pred = bbox_head(pooled_seg, pooled_part)
```

part 回归目标的数学定义：设体素中心为 \(p\)、所属 gt 框的底面中心为 \(c\)、朝向角为 \(\theta\)、尺寸为 \(d=(d_x,d_y,d_z)\)，先把 \(p\) 平移旋转到框局部坐标，再按尺寸归一化并平移到 \([0,1]\)：

\[
t \;=\; \frac{R(-\theta)\,(p - c)}{d} + \left(0.5,\,0.5,\,0\right)
\]

其中 \(z\) 分量用 0 而不是 0.5，是因为 \(c\) 取的是底面中心（体素在框内的相对高度天然从 0 开始）。`part_feats` 的 4 维 = 3 维偏移 + 1 维前景分数。

#### 4.2.3 源码精读

**检测器壳**。[mmdet3d/models/detectors/parta2.py:L35-L36](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/parta2.py#L35-L36) 在两阶段基类之外多构建 `voxel_encoder`（HardSimpleVFE）与 `middle_encoder`（SparseUNet）。[parta2.py:L38-L66](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/parta2.py#L38-L66) 的 `extract_feat` 依次跑 VFE → SparseUNet → SECOND → FPN，最后把 `voxels_dict` 塞进 feats_dict 带给第二阶段（L65）。注意 SparseUNet 的返回只有两个键：[mmdet3d/models/middle_encoders/sparse_unet.py:L161-L165](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/middle_encoders/sparse_unet.py#L161-L165) 产出 `spatial_features`（dense BEV，给 RPN）与 `seg_features`（解码路径的稀疏特征，给语义头）——**`part_feats` 并非骨干产物**，而是语义头在现场算出来的。

**语义头**。[mmdet3d/models/roi_heads/mask_heads/pointwise_semantic_head.py:L52-L53](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/mask_heads/pointwise_semantic_head.py#L52-L53) 只有两个 `nn.Linear`：1 维的前景打分与 3 维的部位回归。[L71-L82](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/mask_heads/pointwise_semantic_head.py#L71-L82) 的 forward 里，`part_feats = cat(sigmoid 后的前 3 维偏移, 前景分数)` 共 4 维，且偏移在前景分数低于 `seg_score_thr`（配置 0.3）处被置零——背景体素不携带部位信息。

**part 目标生成**。[pointwise_semantic_head.py:L102-L133](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/mask_heads/pointwise_semantic_head.py#L102-L133)：`points_in_boxes_part` 找到体素落入的 gt 框；分割标签里「框外但在外扩 0.2m 框内」的点标 -1（忽略带，L114-116）；框内体素按上面的公式生成 3 维 part 目标（L118-130），最后 `clamp(min=0)`（L132）。

**RoI head 的耦合点**。[mmdet3d/models/roi_heads/part_aggregation_roi_head.py:L316-L357](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/part_aggregation_roi_head.py#L316-L357) 的 `loss` 揭示了双分支的执行顺序：先 `_semantic_forward_train`（L343-346，损失从结果 dict 中 pop 出来），再 `feats_dict.update(semantic_results)`（L352）——**这一行把 `part_feats` 注入 feats_dict**，随后 `_bbox_forward_train` 才能取到它。推理路径同理：[predict_bbox L271-L272](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/part_aggregation_roi_head.py#L271-L272) 先跑语义头并 update。

**双提取器**。[part_aggregation_roi_head.py:L299-L307](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/part_aggregation_roi_head.py#L299-L307)：`_bbox_forward` 分别用 `seg_roi_extractor`（对 16 维语义特征，mode='max'）与 `bbox_roi_extractor`（对 4 维 part 特征，mode='avg'）池化，两个提取器类相同、仅池化模式不同——max 保显著语义、avg 保平均部位分布。配置对照见 [configs/_base_/models/parta2.py:L81-L94](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/parta2.py#L81-L94)。

**逐类分配**。[part_aggregation_roi_head.py:L131-L161](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/part_aggregation_roi_head.py#L131-L161)：当 `bbox_assigner` 是列表（配置 `assign_per_class=True`）时，按类别把 proposal 与该类 gt 分开 assign，再用 `F.pad` 技巧把各类 `AssignResult.gt_inds`（0=背景、k=第 k 个 gt、-1=忽略）重新编号合并成一个跨类的 `AssignResult`。注意 L118-119 先把 gt 的框对象 `.tensor` 化——mmdet 的 assigner 期望裸张量。

#### 4.2.4 代码实践

**实践目标**：跑通仓库自带的 Part-A2 测试，观察两阶段损失键的完整集合。

**操作步骤**：

1. 准备能 import mmdet3d 的 GPU 环境（RoIAwarePool3d 等算子仅 CUDA 实现）。
2. 运行：

   ```bash
   pytest tests/test_models/test_detectors/test_parta2.py -v
   ```

3. 阅读测试源码 [tests/test_models/test_detectors/test_parta2.py:L41-L61](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_models/test_detectors/test_parta2.py#L41-L61)：它用 `create_detector_inputs` 造伪输入，先 `mode='predict'` 再 `mode='loss'`。
4. 若想看全部损失键，可在本地临时加一行 `print(sorted(losses.keys()))`（改测试文件属于本地实验，勿提交）。

**需要观察的现象**：损失字典同时含 RPN 段（`loss_rpn_cls`、`loss_rpn_bbox`、`loss_rpn_dir`）与 RoI 段（`loss_seg`、`loss_part`、`loss_cls`、`loss_bbox`、`loss_corner`）。

**预期结果**：测试断言 `loss_rpn_cls[0] > 0`、`loss_seg > 0`、`loss_part > 0`、`loss_cls > 0` 全部通过；`bboxes_3d / scores_3d / labels_3d` 出现在 `pred_instances_3d`。本机无 GPU 时无法运行，属**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`feats_dict['part_feats']` 是谁生产的？为什么它不可能是 SparseUNet 的输出？

**答案**：由 `PointwiseSemanticHead.forward` 生产，经 `feats_dict.update(semantic_results)` 注入。SparseUNet 的返回 dict 只有 `spatial_features` 与 `seg_features` 两个键（sparse_unet.py L161-165）；part 特征依赖监督信号（前景分数门控），必须由带损失的语义头现场计算。

**练习 2**：为什么两个 RoI 提取器一个用 `mode='max'`、一个用 `mode='avg'`？

**答案**：语义特征（16 维）希望突出「RoI 内最像物体的证据」，max 池化保留每个网格里最显著的响应；part 特征（4 维偏移 + 分数）描述的是体素的空间分布统计，取平均才能反映部位的整体走向，max 只会挑一个极端值。

**练习 3**：part 回归目标公式里 z 分量的偏移是 0 而非 0.5，原因是什么？

**答案**：`get_targets_single` 用 `gt_bboxes_3d.bottom_center` 作为参考中心（L124），框的锚点在底面，体素相对底面的高度天然落在 \([0,1]\)（除以 \(d_z\) 后），所以不再加 0.5；x、y 的偏移则因中心在框中部而需要 0.5。

### 4.3 Single3DRoIAwareExtractor：RoI 内的 point→voxel 池化

#### 4.3.1 概念说明

有了 proposal，第二阶段的第一个问题是：**如何把形状不规则、点数不定的 RoI 内部特征，变成一个固定形状的张量喂给网络？**

mmdet 的 2D 世界里答案是 RoIAlign（在特征图上双线性采样固定网格）。3D 世界里 `Single3DRoIAwareExtractor` 的答案分两步：

1. **找点**：RoI 是一个旋转 3D 框，凡落在框内的点/体素都属于它；
2. **point→voxel**：把每个 RoI 内部的点按 RoI 自身的局部坐标系重新栅格化成 \(14\times14\times14\) 的规则网格，再在每个小格里做 max 或 avg 池化——这一步就是把「无序点集」变成「规则体素网格」，是本讲学习目标里 point2voxel 的具体实现。

#### 4.3.2 核心流程

```text
输入：feats (全部体素的特征, (N_all, C))
     coordinate (全部体素中心, (N_all, 3))
     batch_inds (每个体素的帧号, (N_all,))
     rois ((总RoI数, 8)：帧号 + 7 维框)

for 每一帧 batch_idx:
    roi_inds   = rois[:, 0] == batch_idx 的 RoI
    coors_inds = batch_inds == batch_idx 的体素
    该帧池化结果 = RoIAwarePool3d(rois[..., 1:], coordinate[该帧], feats[该帧])

输出：cat 各帧结果 → (总RoI数, 14, 14, 14, C)
```

每个 RoI 内部的池化等价于求解：对网格点 \(g\)（共 \(14^3\) 个），

\[
\text{feat}(g) \;=\; \underset{p \,\in\, \text{voxel}(g) \cap \text{RoI}}{\max / \avg}\; \text{feat}(p)
\]

其中 \(\text{voxel}(g)\) 是把 RoI 的 \(d_x \times d_y \times d_z\) 空间等分后的第 \(g\) 个小格；`max_pts_per_voxel=128` 限制每个小格最多参与 128 个点，防止极端密集处拖慢算子。

#### 4.3.3 源码精读

- [mmdet3d/models/roi_heads/roi_extractors/single_roiaware_extractor.py:L29-L36](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/roi_extractors/single_roiaware_extractor.py#L29-L36)：`build_roi_layers` 直接用 `getattr(mmcv.ops, layer_type)` 取算子类——`type='RoIAwarePool3d'` 路由到 mmcv 的 CUDA 实现，`out_size=14, max_pts_per_voxel=128, mode` 从配置透传。
- [L38-L61](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/roi_extractors/single_roiaware_extractor.py#L38-L61)：forward 的 per-batch 循环。L54-55 按帧号分别筛 RoI 与点，L56-58 对单帧调用算子（`rois[..., 1:]` 剥掉帧号列），L60 沿第 0 维拼接。输出形状由消费方 [parta2_bbox_head.py:L259-L264](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/bbox_heads/parta2_bbox_head.py#L259-L264) 反推：`part_feats.shape[1:4]` 当作 \(14^3\) 空间形状、最后一维当通道，即输出为 5 维 `(num_rois, 14, 14, 14, C)`。

配置侧的对应（Part-A2 用同一个类构建两次）见 [configs/_base_/models/parta2.py:L81-L94](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/parta2.py#L81-L94)。

顺带对比目录里的另外两个提取器，理解「提取器 = 一个可替换的策略类」：

- `Single3DRoIPointExtractor`：不做栅格化，直接按点索引取 RoI 内点特征（H3DNet 等使用）；
- `Batch3DRoIGridExtractor`（下一节详解）：把 RoI 拆成 6³ 网格**点**再做球查询聚合，PV-RCNN 使用。

#### 4.3.4 代码实践

**实践目标**：用最小伪数据亲手驱动一次 RoI 感知池化，观察输出形状。

**操作步骤**（示例代码，非项目原有代码）：

```python
# 示例代码：最小 RoIAwarePool3d 实验（需要 GPU，RoIAwarePool3d 仅 CUDA 实现）
import torch
from mmdet3d.registry import MODELS

extractor = MODELS.build(dict(
    type='Single3DRoIAwareExtractor',
    roi_layer=dict(type='RoIAwarePool3d', out_size=14,
                   max_pts_per_voxel=128, mode='max'))).cuda()

num_pts, C = 20000, 16
feats = torch.rand(num_pts, C).cuda()                 # 假装是 seg_features
coordinate = torch.rand(num_pts, 3).cuda() * 40 - 20  # [-20, 20) 的伪体素中心
batch_inds = torch.zeros(num_pts, dtype=torch.long).cuda()  # 单帧
rois = torch.tensor([[0, 0.0, 0.0, 0.0, 4.0, 2.0, 1.6, 0.0]]).cuda()

out = extractor(feats, coordinate, batch_inds, rois)
print(out.shape)   # 期望 torch.Size([1, 14, 14, 14, 16])
print(out.min(), out.max())
```

**需要观察的现象**：输出形状为 `(1, 14, 14, 14, 16)`；由于 `feats ∈ [0,1)` 均匀分布且 mode='max'，每个格子的值应落在 `[0, 1)` 内、空格子为 0。

**预期结果**：形状断言成立；把 `mode` 换成 `'avg'` 后同一格子数值变小（max ≥ avg）。无 GPU 环境时**待本地验证**；纯 CPU 机器上 `RoIAwarePool3d` 会因 CUDA 算子缺失而报错，这本身就是值得记录的现象。

#### 4.3.5 小练习与答案

**练习 1**：为什么 forward 要按帧循环，而不是把整个 batch 一次丢给算子？

**答案**：`RoIAwarePool3d` 的输入契约是「一组 RoI + 一片点云」，要求点与 RoI 同处一个坐标系批次；批内不同帧的点云各自独立，帧号编码在 rois 第 0 列与 `batch_inds` 里，循环筛出「该帧的 RoI + 该帧的点」是让算子契约成立的最直接做法。

**练习 2**：`out_size=14` 会传导到哪些下游超参？

**答案**：`PartA2BboxHead.roi_feat_size=14`（配置 L109）必须与之一致；head 内 `pool_size = roi_feat_size // 2 = 7`（经 2×2 稀疏最大池化后网格减半），shared_fc 输入维度 `shared_fc_channels[0] * 7³`。

**练习 3**：`max_pts_per_voxel=128` 起什么作用？调大会怎样？

**答案**：限制每个输出小格最多聚合 128 个输入点，是速度与统计质量的折中。调大更贴近真实 max/avg 但显存与耗时上升；调小则可能漏掉点，使池化结果有偏。

### 4.4 PartA2BboxHead：稀疏卷积精修、canonical 编码与 corner loss

#### 4.4.1 概念说明

`PartA2BboxHead` 拿到两个 \(14^3\) 网格特征（16 维语义 + 4 维 part）后，要做三件事：

1. **再加工**：RoI 内大部分格子是空的（物体表面才有回波点），用**稀疏卷积**只在非空格点上做 3D 卷积，比 dense 卷积省算力；
2. **打分与回归**：cls 分支输出 1 维（类不可分前景置信度），reg 分支输出 7 维残差；
3. **训练目标构造**：canonical 变换 + IoU 软标签 + corner loss 三件套。

它与 PV-RCNN 的 `PVRCNNBBoxHead` 是「兄弟」：目标构造逻辑几乎逐行相同，差别在前半段特征加工方式（稀疏 3D 卷积 vs 纯 FC）。

#### 4.4.2 核心流程

```text
forward(seg_feats, part_feats)：各 (M, 14, 14, 14, C)
 (1) sum(-1).nonzero() 找非空格 → 转 SparseConvTensor
 (2) part_conv / seg_conv：各自 2 层 SubMConv3d(3×3×3)
 (3) cat 两侧 features → merge_conv(2 层) → SparseMaxPool3d(2×2×2) → down_conv
 (4) x.dense() 展平 → shared_fc(1×1 conv) → cls(1 维) / reg(7 维)

_get_target_single（对每个正样本 RoI）：
 IoU 软标签：label = 1 (iou>0.75) / iou*2-0.5 (0.25~0.75) / 0 (iou<0.25)
 canonical：gt 平移到 RoI 中心、旋转 -roi_ry、朝向折到 (-π/2, π/2)
 encode：DeltaXYZWLHRBBoxCoder.encode(零位 anchor, canonical gt)

loss：CrossEntropy(软标签) + SmoothL1(残差) + corner loss
```

corner loss 的动机：SmoothL1 对 7 个自由度逐项独立惩罚，但一个框的 8 个角点是 7 个参数的**联合**函数——参数误差小而角点偏差大的情况（细长框的角度抖动）逐项损失察觉不到。corner loss 直接对齐 8 个角点：

\[
\mathcal{L}_{corner} \;=\; \frac{1}{8}\sum_{i=1}^{8} \text{Huber}\bigl(\min_{\delta\in\{0,\pi\}} \lVert c_i^{pred} - c_i^{gt}(\theta+\delta) \rVert\bigr)
\]

对 gt 朝向翻转 \(\pi\) 取 min，是为了容忍「框朝向定义头尾不分」的等价解。

#### 4.4.3 源码精读

**结构搭建**。[mmdet3d/models/roi_heads/bbox_heads/parta2_bbox_head.py:L96-L160](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/bbox_heads/parta2_bbox_head.py#L96-L160)：part/seg 各两层 `SubMConv3d`（L100-127），两侧末层通道拼接后走 `merge_conv`（131-142）、`SparseMaxPool3d(2,2)`（158-159）、`down_conv`（144-160）——`indice_key` 复用（`rcnn_part0/1`、`rcnn_seg0/1`）是稀疏卷积共享索引的计算优化（u5-l2 已讲）。[L162-L180](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/bbox_heads/parta2_bbox_head.py#L162-L180) 的 shared_fc 输入维度是 `shared_fc_channels[0] * (14//2)**3 = 256×343`；cls/reg 是两串 1×1 Conv1d（[L182-L236](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/bbox_heads/parta2_bbox_head.py#L182-L236)），输出分别为 1 与 `bbox_coder.code_size=7`。

**前向**。[L248-L296](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/bbox_heads/parta2_bbox_head.py#L248-L296)：L264 用 `part_feats.sum(dim=-1).nonzero()` 把 dense 5 维张量转稀疏索引（part 特征被 sigmoid 压到非负，sum 为 0 即全空格）；L285 走 `conv_down`；L287 `x.dense()` 收回 dense；L289-294 出 cls/reg。

**canonical 变换与目标**。[L416-L480](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/bbox_heads/parta2_bbox_head.py#L416-L480) 的 `_get_target_single`：IoU 软标签在 L434-440（区间内线性 `iou*2-0.5`）；canonical 在 L449-468——先减 RoI 中心、减 RoI 朝向（L454-455），再绕 z 轴旋转 \(-\theta_{roi}\)（L456-458），随后把 gt 朝向折叠进 \((-\pi/2, \pi/2)\)（L461-468），让「车头朝 +x 与朝 −x」不再混淆；最后把位置清零的 anchor 与 canonical gt 一起 `encode`（L470-474）。注意 L445-446：`reg_mask` 只把**前 M 个**（每个 gt 恰好配到一个 proposal）置 1。

**损失**。[L298-L371](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/bbox_heads/parta2_bbox_head.py#L298-L371)：分类用 `CrossEntropyLoss(use_sigmoid=True)` 吃软标签；回归仅对 `reg_mask>0` 的正样本（L332-345）；corner 损失分支先在 L348-364 把预测残差解码回全局坐标（零位 anchor decode → 转回 `roi_ry` → 加回 `roi_xyz`），再调 `get_corner_loss_lidar`。

**corner loss 实现**。[L482-L519](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/bbox_heads/parta2_bbox_head.py#L482-L519)：L500-507 分别取预测框与「gt 框朝向 +π」翻转版的 8 角点，L509-512 取逐角点最小欧氏距离，L514-517 手写 Huber（二次段+线性段）。

**推理解码**。[L521-L590](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/bbox_heads/parta2_bbox_head.py#L521-L590) 的 `get_results` 与训练 corner 分支的解码完全同构（L557-565）；随后 [L592-L658](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/bbox_heads/parta2_bbox_head.py#L592-L658) 的 `multi_class_nms` 用 **RPN 传下来的 `cls_preds`（逐类原始分数）** 选类，而最终 `scores_3d` 用 RoI 头的置信度——两阶段分数分工。

#### 4.4.4 代码实践

**实践目标**：验证 canonical 编码的可逆性，并体会零位 anchor 的含义。

**操作步骤**（示例代码，CPU 可跑）：

```python
# 示例代码：canonical encode/decode 往返实验
import torch
from mmdet3d.registry import TASK_UTILS

coder = TASK_UTILS.build(dict(type='DeltaXYZWLHRBBoxCoder'))

# 一个 RoI（底面中心锚点）与一个 canonical 化之后的 gt
roi_anchor = torch.tensor([[0., 0., 0., 4., 2., 1.6, 0.]])   # 位置/朝向清零
gt_canonical = torch.tensor([[1.2, -0.4, 0.3, 4.1, 2.0, 1.6, 0.2]])

code = coder.encode(roi_anchor, gt_canonical)
recon = coder.decode(roi_anchor, code)
print('code:', code)                      # 7 维残差
print('往返误差:', (recon - gt_canonical).abs().max())
```

然后回到源码：对照 [parta2_bbox_head.py:L557-L565](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/bbox_heads/parta2_bbox_head.py#L557-L565)，说明为什么 decode 后还必须「先绕 z 转 `roi_ry`、再加 `roi_xyz`」。

**需要观察的现象**：`code` 中位置分量是 RoI 与 gt 的中心差（z 分量因底面/几何中心换算差 h/2，具体符号以 coder 实现为准），尺寸分量为 log 比，往返误差接近 0（浮点误差量级）。

**预期结果**：往返误差小于 \(10^{-6}\)；若把 `gt_canonical` 的朝向改成 \(\pi\)（未折叠），decode 无法还原——这正是 L461-468 朝向折叠存在的意义。数值以本地运行为准（**待本地验证**）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `forward` 里用 `sum(dim=-1).nonzero()` 而不是 `feats.nonzero()` 找非空格？

**答案**：按最后一维求和后得到每个格子的标量能量，`nonzero()` 给出 4 维索引 \((roi, x, y, z)\)，正好是 SparseConvTensor 需要的 indices（再补 batch 维）；直接对 5 维张量 nonzero 会返回逐元素索引，维度不对。

**练习 2**：IoU 软标签 `label[interval] = iou*2 - 0.5` 为什么是线性拉伸？

**答案**：把 \((0.25, 0.75)\) 的 IoU 区间线性映射到 \((0, 1)\) 的连续标签，使 BCE 损失对「中等质量 RoI」给出介于前景与背景之间的监督，比硬 0/1 标签更平滑，两个端点在区间边界处恰好连续。

**练习 3**：corner loss 为什么要与翻转 \(\pi\) 的 gt 取 min？

**答案**：朝向角 \(\theta\) 与 \(\theta+\pi\) 描述的是同一个矩形框（只是「车头」方向相反），3D 框回归本身不区分头尾；若不取 min，网络会被迫在两个等价解之间二选一，损失面上出现人为的坑洼。取 min 允许预测收敛到任意一个等价朝向。

### 4.5 PV-RCNN：关键点分支、Batch3DRoIGridExtractor 与 PVRCNNRoiHead

#### 4.5.1 概念说明

Part-A2 的第二阶段特征全部来自**体素侧**（稀疏骨干 + 逐体素语义）。PV-RCNN（注册名 `PointVoxelRCNN`）则主张 **Point–Voxel 双路融合**：在体素 RPN 之外，另设一个**关键点分支**——

1. 用 FPS 从原始点云采 2048 个关键点；
2. 每个关键点从三个来源收集特征：BEV 特征图双线性插值、原始点邻域 SA 聚合、多尺度稀疏体素特征 SA 聚合（正是 u6-l1 `StackedSAModuleMSG` 的用武之地）；
3. 拼接成 640 维「keypoint_features」，再 1×1 卷积压成 128 维融合特征。

第二阶段不再对体素池化，而是对每个 RoI 生成 \(6^3=216\) 个**网格点**，用 SA 球查询从关键点特征里聚合出 RoI 特征（`Batch3DRoIGridExtractor`），随后 `PVRCNNBBoxHead`（纯 FC）打分回归。语义侧只保留一个**前景二分类头**（`ForegroundSegmentationHead`），其输出用于给关键点特征做前景加权。

#### 4.5.2 核心流程

```text
PointVoxelRCNN.loss：
 (1) extract_feat（体素路）：
     VFE → SparseEncoder(return_middle_feats=True)
       ├─ spatial_feats（BEV）→ SECOND → SECONDFPN → rpn_head
       └─ multi_scale_3d_feats（4 级稀疏张量，留给关键点分支）
 (2) rpn_head.loss_and_predict → rpn_results_list
 (3) extract_points_feat（点路）＝ VoxelSetAbstraction：
     FPS 采 2048 关键点
       ├─ BEV 双线性插值            → 256 维
       ├─ 原始点 SA(0.4/0.8)        → 32 维
       └─ 4 级体素 SA(×1/×2/×4/×8) → 352 维
     cat → keypoint_features(640) ──1×1 conv──> fusion_keypoint_features(128)
 (4) roi_head.loss：
     语义头对 keypoint_features 出前景分 → loss_semantic
     前景分数 sigmoid 加权 fusion 特征
     Batch3DRoIGridExtractor：RoI → 6³ 网格点 → SA 聚合 → (B*N, 6,6,6,128)
     PVRCNNBBoxHead → cls(1) / reg(7)，目标构造与 Part-A2 同款
```

#### 4.5.3 源码精读

**检测器**。[mmdet3d/models/detectors/pv_rcnn.py:L59-L61](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/pv_rcnn.py#L59-L61)：比 PartA2 多构建 `points_encoder`（VoxelSetAbstraction）。[L116-L149](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/pv_rcnn.py#L116-L149) 的 `extract_feat` 同时产出 `spatial_feats` 与 `multi_scale_3d_feats`（SparseEncoder 开 `return_middle_feats=True` 的原因）。[L176-L232](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/pv_rcnn.py#L176-L232) 的 `loss` 里，关键点提取发生在 RPN 之后（L224-226）——`VoxelSetAbstraction` 需要 `rpn_results_list` 参数（接口预留，当前实现未消费 RoI 内容）。

**VoxelSetAbstraction**。[mmdet3d/models/middle_encoders/voxel_set_abstraction.py:L192-L231](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/middle_encoders/voxel_set_abstraction.py#L192-L231)：逐帧 FPS 采样；点数不足时重复填充（L222-225）。[L131-L168](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/middle_encoders/voxel_set_abstraction.py#L131-L168)：把关键点坐标换算成 BEV 格坐标（除以 voxel_size 再除以 scale_factor）后双线性插值。[L170-L190](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/middle_encoders/voxel_set_abstraction.py#L170-L190)：体素中心反算 \((c+0.5)\cdot v\cdot s + p_{min}\)，注意 coors 的 (z,y,x) 倒序换回 (x,y,z)。[L265-L324](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/middle_encoders/voxel_set_abstraction.py#L265-L324)：三源特征逐路 append 后 cat，过 1×1 `point_feature_fusion_layer`；[L326-L335](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/middle_encoders/voxel_set_abstraction.py#L326-L335) 返回时给 keypoints 拼上帧号列——与 `bbox3d2roi` 的约定对称。配置侧三路通道数见 [configs/pv_rcnn/pv_rcnn_8xb2-80e_kitti-3d-3class.py:L95-L143](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/pv_rcnn/pv_rcnn_8xb2-80e_kitti-3d-3class.py#L95-L143)（256+32+352=640，融合成 128）。

**RoI head**。[mmdet3d/models/roi_heads/pv_rcnn_roi_head.py:L60-L104](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/pv_rcnn_roi_head.py#L60-L104) 的 `loss`：语义头吃 640 维 `keypoint_features`（L90-93，对应 `ForegroundSegmentationHead` 的 `in_channels=640`，配置 [L188-L199](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/pv_rcnn/pv_rcnn_8xb2-80e_kitti-3d-3class.py#L188-L199)）；前景加权发生在 [L174-L176](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/pv_rcnn_roi_head.py#L174-L176)：`keypoint_features * seg_preds.sigmoid().max(...)`，即用前景概率逐点调制 128 维融合特征（推理侧对应 [L139-L143](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/pv_rcnn_roi_head.py#L139-L143)）。前景分割的目标生成在 [foreground_segmentation_head.py:L88-L118](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/mask_heads/foreground_segmentation_head.py#L88-L118)：与 Part-A2 的语义目标同构（外扩框忽略带），但只有「前景/背景」一档，且 `activated=True` 的 FocalLoss 要求输入已过 sigmoid（[L172-L173](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/mask_heads/foreground_segmentation_head.py#L172-L173) 在头内部先 sigmoid）。`_bbox_forward` 见 [pv_rcnn_roi_head.py:L188-L210](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/pv_rcnn_roi_head.py#L188-L210)：`keypoints[..., 1:]` 是坐标、`keypoints[..., 0].int()` 是帧号。

**网格点提取器**。[mmdet3d/models/roi_heads/roi_extractors/batch_roigridpoint_extractor.py:L72-L97](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/roi_extractors/batch_roigridpoint_extractor.py#L72-L97) 的 `get_dense_grid_points`：先生成 \(((i+0.5)/6 - 0.5)\) 的标准网格偏移（L83-88，范围 \((-0.4, 0.4)\)），乘以 RoI 尺寸、绕 z 转 RoI 朝向、加 RoI 中心——**注意 L82 先给 z 加了 `h/2`**，因为 RoI 的 z 是底面中心而网格要以几何中心对称分布。[L43-L68](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/roi_extractors/batch_roigridpoint_extractor.py#L43-L68)：把网格点作为 SA 模块的 `new_xyz`（分组中心），从关键点里球查询聚合（L59-64），再 reshape 成 `(B*N, 6, 6, 6, 128)`。

**bbox head**。[mmdet3d/models/roi_heads/bbox_heads/pv_rcnn_bbox_head.py:L82-L111](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/bbox_heads/pv_rcnn_bbox_head.py#L82-L111)：`class_agnostic=True` 时 cls 输出 1 维、reg 输出 7 维；L93 `in_channels *= grid_size**3` 把 \(6^3\) 摊平进输入维度。[L150-L169](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/bbox_heads/pv_rcnn_bbox_head.py#L150-L169) 的 forward 纯 2D 1×1 卷积栈。`_get_target_single`（[L290-L355](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/bbox_heads/pv_rcnn_bbox_head.py#L290-L355)）与 Part-A2 版逐行近似，是两兄弟共享模板的直接证据；差别之一是 corner loss 的 Huber 写法（[L389-L392](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/bbox_heads/pv_rcnn_bbox_head.py#L389-L392) 用 `torch.where`，数学等价）。推理走 **class-agnostic NMS**（[L467-L509](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/bbox_heads/pv_rcnn_bbox_head.py#L467-L509)，topk 截到 4096 再 NMS），标签沿用 RPN 的 `labels_3d`，而 Part-A2 是逐类 NMS——两阶段分数的分工方式不同。

**Part-A2 vs PV-RCNN 速览表**：

| 维度 | Part-A2 | PV-RCNN |
| --- | --- | --- |
| 检测器类 | `PartA2` | `PointVoxelRCNN` |
| 体素骨干 | `HardSimpleVFE` + `SparseUNet`（双输出） | `HardSimpleVFE` + `SparseEncoder`（`return_middle_feats=True`） |
| RPN 头 | `PartA2RPNHead` | `PartA2RPNHead`（同一个类） |
| 二阶段特征来源 | 逐**体素**：seg_features(16) + part_feats(4) | 逐**关键点**：三源融合 keypoint_features(640)→fusion(128) |
| 语义辅助头 | `PointwiseSemanticHead`（前景 + 部位回归） | `ForegroundSegmentationHead`（仅前景二分类） |
| RoI 提取器 | `Single3DRoIAwareExtractor`（两个实例，max/avg） | `Batch3DRoIGridExtractor`（SA 球查询） |
| RoI 特征形 | \(14^3\) 网格（RoI 内点重新体素化） | \(6^3\) 网格点（从关键点聚合） |
| bbox head | `PartA2BboxHead`：稀疏 3D 卷积 + FC | `PVRCNNBBoxHead`：纯 FC |
| 推理 NMS | 逐类（用 RPN `cls_preds`） | class-agnostic（topk 4096 截断） |

#### 4.5.4 代码实践

**实践目标**：验证 `get_dense_grid_points` 的网格点生成公式，理解 \(6^3\) 网格的几何分布。

**操作步骤**：

1. 阅读 [batch_roigridpoint_extractor.py:L72-L97](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/roi_extractors/batch_roigridpoint_extractor.py#L72-L97)。
2. 手工推导：RoI = `(x,y,z,dx,dy,dz,ry) = (10, 0, 0, 4, 2, 1.6, 0)`，grid_size=6 时：
   - 标准偏移 \((i+0.5)/6 - 0.5\) 对 \(i=0\) 为 \(-5.5/6\)——请先算出全部 6 个偏移值（应为 \(\pm5.5/6,\pm3.5/6,\pm1.5/6\)）；
   - 乘尺寸、加中心后，写出第一个网格点 \((i,j,k)=(0,0,0)\) 的坐标。
3. 用示例代码核对（CPU 可跑，只依赖 `rotation_3d_in_axis`）：

   ```python
   # 示例代码：单 RoI 网格点核对
   import torch
   from mmdet3d.registry import MODELS

   ext = MODELS.build(dict(type='Batch3DRoIGridExtractor', grid_size=6,
                           roi_layer=dict(type='StackedSAModuleMSG',
                                          in_channels=128,
                                          radius=(0.8, 1.6),
                                          sample_nums=(16, 16),
                                          mlp_channels=((64, 64), (64, 64)),
                                          use_xyz=True, pool_mod='max')))
   rois = torch.tensor([[0, 10., 0., 0., 4., 2., 1.6, 0.]])
   grid = ext.get_dense_grid_points(rois[:, 1:])   # (1, 216, 3)
   print(grid.shape, grid[0, 0], grid[0, -1])
   ```

**需要观察的现象**：`grid.shape == (1, 216, 3)`；`ry=0` 时第一个网格点三个分量都接近各自维度的最小端 \(-5.5/6 \times d\)，最后一个点接近最大端；`grid[0].mean(0)` 应近似 \((10, 0, 0.8)\)——0.8 即 `z + dz/2`（几何中心，因为 L82 先加了半高）。

**预期结果**：均值校验 `\approx (10, 0, 0.8)` 成立；把 `ry` 改成 1.57 后网格绕 z 旋转，均值不变。数值以本地运行为准（**待本地验证**）。

#### 4.5.5 小练习与答案

**练习 1**：PV-RCNN 的 `keypoint_features` 是 640 维、`fusion_keypoint_features` 是 128 维，配置里哪些数字相加得到 640？

**答案**：BEV 插值 `bev_feat_channel=256`；原始点 SA 的 `mlp_channels=((16,16),(16,16))` 末层和 \(16+16=32\)；四层体素 SA 分别为 32、64、128、128（`((16,16),(16,16))`、`((32,32),(32,32))`、`((64,64),(64,64))`、`((64,64),(64,64))`），共 352；\(256+32+352=640\)，随后 1×1 卷积压到 `fused_out_channel=128`。

**练习 2**：为什么 `get_dense_grid_points` 要先执行 `rois_bbox[:, 2] += rois_bbox[:, 5] / 2`？

**答案**：RoI 的 z 语义是**底面中心**（u5-l4 讲过 anchor 与采样框的锚点约定），而对称分布的网格点要以**几何中心**为原点；不加半高，网格会整体贴着框底面，RoI 上半部分没有网格点覆盖。

**练习 3**：PV-RCNN 推理用 class-agnostic NMS 而不是逐类 NMS，标签从哪里来？

**答案**：`PVRCNNBBoxHead.get_results`（L441-456）把 RoI 头置信度 sigmoid 后取 max 得到 objectness 分数用于 NMS 排序，类别标签直接沿用 RPN 传入的 `labels_3d`；Part-A2 则相反——用 RPN 的逐类 `cls_preds` 挑类、RoI 头分数作最终 scores。

## 5. 综合实践

**任务**：画出 Part-A2 与 PV-RCNN 的两阶段结构对比图，并写一张「RoI 特征提取器对比表」。这是把本讲五个模块串起来的检验。

**要求**：

1. 对每个模型各画一条从输入到输出的数据流，必须标出三个锚点：**RPN 的特征输入**、**RoI 特征的来源与提取器类名**、**bbox head 的输出**；
2. 用不同颜色/线型区分「体素路」与「点路」（PV-RCNN 特有）；
3. 在图旁给出对比表，至少包含：RoI 提取器类名、池化算子、RoI 特征网格、池化模式、语义辅助头、NMS 方式。

**参考答案骨架**（可直接核对）：

```text
Part-A2:
  points → Det3DDataPreprocessor(voxel) → HardSimpleVFE → SparseUNet
      ├─ spatial_features(BEV) → SECOND → SECONDFPN ──┐
      └─ seg_features(逐体素16维) → PointwiseSemanticHead├→ PartA2RPNHead → proposals
                                    └─ part_feats(4维) │
  proposals + (seg_features, part_feats, voxel_centers)
      → Single3DRoIAwareExtractor ×2 (RoIAwarePool3d, 14³, max/avg)
      → PartA2BboxHead(稀疏卷积) → cls/reg → 逐类 NMS(用 RPN cls_preds)

PV-RCNN:
  points ─┬─ voxel 路: VFE → SparseEncoder(middle_feats=True)
          │     ├─ spatial_feats → SECOND → SECONDFPN → PartA2RPNHead → proposals
          │     └─ multi_scale_3d_feats(4级) ┐
          └─ 点路: 原始点 ──────────────────┼→ VoxelSetAbstraction(FPS 2048)
                BEV 插值(256) ──────────────┘     ├ keypoint_features(640) → ForegroundSegmentationHead
                                                    └ 前景加权 fusion(128)
  proposals + (keypoints, fusion 特征)
      → Batch3DRoIGridExtractor(6³ 网格点 + StackedSAModuleMSG 球查询)
      → PVRCNNBBoxHead(纯FC) → cls/reg → class-agnostic NMS
```

| 对比项 | Part-A2 | PV-RCNN |
| --- | --- | --- |
| RoI 提取器类名 | `Single3DRoIAwareExtractor`（两个实例） | `Batch3DRoIGridExtractor` |
| 底层算子 | mmcv `RoIAwarePool3d`（RoI 内点重新体素化，point→voxel） | `StackedSAModuleMSG`（网格点为中心的球查询聚合） |
| 网格形状 | \(14\times14\times14\) | \(6\times6\times6\) |
| 池化模式 | seg 用 max、part 用 avg | max（`pool_mod='max'`） |
| 输入特征 | 体素级 seg(16) + part(4) | 关键点级 fusion(128) |
| 关键差异 | 特征在「RoI 内部的点」上直接栅格化 | 特征先离线聚合到固定关键点，RoI 只决定查哪里 |

**检验标准**：如果你能不看讲义填出上表，并解释「Part-A2 池化的是 RoI 内部点、PV-RCNN 池化的是全局关键点」这一根本差异，本讲目标即达成。

## 6. 本讲小结

- 两阶段 = RPN 出 proposal + RoI head 精修；`TwoStage3DDetector` 只做编排，`train_cfg` 被切成 `rpn / rpn_proposal / rcnn` 三段分别注入，RPN 损失键统一加 `rpn_` 前缀防冲突。
- `roi_heads` 子系统三层组织：`Base3DRoIHead` 骨架 + `roi_extractors` 特征策略 + `bbox_heads` 打分回归；RoI 的批内统一表示是 `bbox3d2roi` 的「帧号 + 7 维框」。
- Part-A2 的耦合枢纽是 `feats_dict`：SparseUNet 只产 `spatial_features / seg_features`，`part_feats` 由 `PointwiseSemanticHead` 现场产出并注入，RoI 分支用 max/avg 两个 `Single3DRoIAwareExtractor` 分别池化语义与部位特征。
- `Single3DRoIAwareExtractor` 封装 mmcv 的 `RoIAwarePool3d`：把每个 RoI 内部的点按局部坐标系重新栅格化为 \(14^3\) 网格（point→voxel），per-batch 循环调用。
- `PartA2BboxHead` 用稀疏 3D 卷积加工网格特征；训练目标三件套 = IoU 软标签 + canonical 变换编码 + 角点级 Huber（对 gt 朝向翻转取 min）。
- PV-RCNN 的第二阶段特征来自 `VoxelSetAbstraction` 的 2048 个 FPS 关键点（BEV 插值 + 原始点 SA + 多尺度体素 SA 三源 640 维融合成 128 维），`Batch3DRoIGridExtractor` 以 \(6^3\) 网格点球查询聚合，前景分数逐点调制特征；其目标构造与 Part-A2 共享同一模板。

## 7. 下一步学习建议

- **横向扩展**：阅读 `mmdet3d/models/roi_heads/point_rcnn_roi_head.py` 与 `h3d_roi_head.py`，看它们如何复用本讲的 `_assign_and_sample` 模板与 `Single3DRoIPointExtractor`（不栅格化、直接取点）。
- **回到 RPN 侧**：本讲两次遇到 `PartA2RPNHead`，它继承 u5-l4 的 `Anchor3DHead`，建议精读其 `predict_by_feat` 中 `limit_period` 修正朝向的段落，补全两阶段的第一阶段细节。
- **前往 u6-l5**：下一讲转入单目 3D 检测（FCOS3D、SMOKE），那里的「RoI」概念退场，取而代之的是图像平面上的 anchor-free 分支，正好与本讲的 anchor + RoI 范式形成对照。
- **工程视角**：结合 u7-l1 的训练循环，跑一次 Part-A2 的短程训练（可缩小数据集），在日志中逐一找到本讲列出的 8 个损失键，验证你对两阶段损失组成的心智模型。

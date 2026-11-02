# VoteNet：基于投票的室内检测端到端解析

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 VoteNet 的核心思想——**投票（vote）**：为什么室内点云检测要让「表面点」投票指向「物体中心」，而不是像 SECOND 那样在 BEV 网格上密铺 anchor。
2. 区分 **object coordinate（物体表面点所在坐标）** 与 **center coordinate（物体中心坐标）**，理解 VoteModule 学的正是二者之差（偏移向量）。
3. 沿着 `VoteHead.forward` 的四步注释（生成投票 → 聚合投票 → 预测打分 → 解码）走通整条前向链路，并能用 `configs/_base_/models/votenet.py` 核对每一层的输入输出通道数。
4. 追踪 `get_targets` / `_get_targets_single`：VoteNet 没有 anchor 与 assigner/sampler，proposal 与真值的匹配靠 **Chamfer 最近邻**完成，正负样本由距离阈值 `pos_distance_thr` / `neg_distance_thr` 划定。
5. 说出 `predict_by_feat`（即 get_bboxes 角色）中「投票点 → 提案 → NMS → 最终框」的顺序，以及空框过滤、`per_class_proposal` 等后处理细节。

## 2. 前置知识

### 2.1 投票的思想直觉

室内场景（ScanNet、SUN RGB-D）的点云来自深度相机或扫描仪，有三个与室外 LiDAR 不同的特点：

- **物体只有表面**：一把椅子的点全落在椅面、椅背上，物体**几何中心附近往往一个点都没有**（中心悬空）。
- **点数极不均衡**：大桌子几万个点，小杯子几十个点。
- **远小近大、稀疏断裂**：同一物体不同扫描视角点密度差很大。

如果直接让网络「看一个点，回归出物体中心的绝对坐标」，网络必须同时记住「这个点在世界系的哪里」和「物体中心在哪」，两个量都随场景平移旋转而变，学习难度大。

VoteNet（ICCV 2019）借用了传统 Hough Voting 的思想：**让落在物体表面的每一个点，投票（回归一个偏移向量）指向它所属物体的中心**。单独一票可能有噪声，但同一物体的几十上百个表面点的票会**在物体中心附近聚成一簇**；在票的聚类中心处再做「这是不是物体、是什么类别、框多大」的判断，就稳定得多。

用公式表达，对第 \(i\) 个表面点 \(p_i\)（object coordinate），网络学习偏移：

\[
\Delta_i = c^{gt} - p_i
\]

其中 \(c^{gt}\) 是该物体中心的 center coordinate。投票点即：

\[
v_i = p_i + \Delta_i^{pred}
\]

两个坐标系的差别一句话总结：**object coordinate 是「点自己站在哪」，center coordinate 是「物体的重心在哪」；投票学的不是绝对位置，而是二者的差**。这让监督信号对场景的整体平移旋转天然不变——场景动，偏移不动。

### 2.2 本讲要用的既有知识

| 概念 | 来自哪一讲 | 本讲怎么用 |
|---|---|---|
| Depth 坐标系、DepthInstance3DBoxes、室内 6 维轴对齐框（with_yaw=False） | u3-l4 | ScanNet 的 gt 框是 6 维无朝向框 |
| PointNet2SASSG 骨干、SA 模块「采样-分组-聚合」、fp_xyz/fp_features 输出 dict | u6-l1 | VoteNet 的骨干与「投票聚合」层都是 SA 模块 |
| PartialBinBasedBBoxCoder：尺寸/朝向拆成「bin 分类 + 残差回归」 | u5-l6 | VoteHead 的 bbox_coder，负责 encode 真值与 decode 预测 |
| aligned_3d_nms：类别感知的室内轴对齐 NMS | u5-l7 | 推理后处理去重 |
| Det3DDataSample 的 gt_instances_3d / gt_pts_seg | u2-l4 | 损失函数读取真值的入口 |

还有一个本讲新用到、需要先认识的工具——**Chamfer 距离**。它衡量两个点集 \(S\)（大小 \(N\)）与 \(T\)（大小 \(M\)）的匹配程度：

\[
d_{CD}(S, T) = \sum_{s \in S}\min_{t \in T}\|s - t\|^2 + \sum_{t \in T}\min_{s \in S}\|s - t\|^2
\]

直觉：左边保证「我的每个点附近都有你的点」，右边保证「你的每个点附近也都有我的点」，两个方向缺一不可。mmdet3d 的实现见 [mmdet3d/models/losses/chamfer_distance.py:L57-L78](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/losses/chamfer_distance.py#L57-L78)：算出 \(N \times M\) 的两两距离矩阵后，`min(dim=2)` 得到 source→destination 的最近距离 `loss_src` 及下标 `indices1`，`min(dim=1)` 得到 `loss_dst` 与 `indices2`。VoteNet 用它做两件事：投票损失（4.2 节）和 proposal↔gt 的匹配分配（4.4 节）——`indices1` 就是「每个 proposal 最近的 gt 是哪一个」。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
|---|---|---|
| [mmdet3d/models/detectors/votenet.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/votenet.py) | 检测器薄壳 | 为什么重写 loss/predict（要把原始 points 传给头） |
| [mmdet3d/models/dense_heads/vote_head.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/vote_head.py) | 核心，全部算法在这 | forward 四步、get_targets、loss_by_feat、predict_by_feat |
| [mmdet3d/models/layers/vote_module.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/vote_module.py) | 投票层 | 偏移回归、残差特征、投票损失 |
| [configs/_base_/models/votenet.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/votenet.py) | 模型四件套之一 | 骨干/头/损失的全部超参 |
| [configs/votenet/votenet_8xb8_scannet-3d.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/votenet/votenet_8xb8_scannet-3d.py) | ScanNet 实验配置 | num_classes=18、bbox_coder 的 mean_sizes |
| [mmdet3d/models/dense_heads/base_conv_bbox_head.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/base_conv_bbox_head.py) | 打分头 | shared conv + cls/reg 双分支 |
| [mmdet3d/models/layers/pointnet_modules/point_sa_module.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/pointnet_modules/point_sa_module.py) | SA 模块（u6-l1 已讲） | 投票聚合层的实现底座 |
| [mmdet3d/models/task_modules/coders/partial_bin_based_bbox_coder.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/partial_bin_based_bbox_coder.py) | bbox 编解码器（u5-l6 已讲） | split_pred 的切片顺序、encode/decode |

整条数据流先给一张总览（ScanNet 18 类、batch_size=B）：

```
原始点云 (B, 40000, 4)                    # x,y,z + shift_height 高度列
        │  PointNet2SASSG backbone (u6-l1)
        ▼
seed_points (B, 1024, 3) + seed_features (B, 256, 1024) + seed_indices
        │  ① VoteModule：回归偏移 offset，seed + offset
        ▼
vote_points (B, 1024, 3) + vote_features (B, 256, 1024)
        │  ② vote_aggregation (PointSAModule)：FPS 选 256 个中心
        │     + 球查询聚合邻域投票 → 提案特征
        ▼
aggregated_points (B, 256, 3) + features (B, 128, 256)
        │  ③ BaseConvBboxHead：shared conv + cls/reg 双 1×1 卷积
        ▼
cls_predictions (B, 20, 256)   reg_predictions (B, 77, 256)
        │  ④ bbox_coder.split_pred 切片解码
        ▼
obj_scores(B,256,2) sem_scores(B,256,18) center(B,256,3)
dir_class(B,256,1) dir_res(B,256,1) size_class(B,256,18) size_res(B,256,18,3)
        │  推理：predict_by_feat → 空框过滤 → aligned_3d_nms → per_class_proposal
        ▼
pred_instances_3d: bboxes_3d (N,6) + scores_3d + labels_3d
```

下面按模块逐一展开。

## 4. 核心概念与源码讲解

### 4.1 VoteNet 检测器骨架：为什么要重写 loss/predict

#### 4.1.1 概念说明

`VoteNet` 继承自 `SingleStage3DDetector`（单阶段检测器基类，u4-l2 讲过它的 loss/predict/_forward 三模式）。有趣的是，它的 `__init__` 只是把参数原样透传给父类，**一行自己的逻辑都没有**；真正需要重写的是 `loss` 和 `predict` 两个方法。

原因在于一个签名差异：基类的 `loss` 只把 `extract_feat` 的产物传给 bbox_head（见 [mmdet3d/models/detectors/single_stage.py:L74-L76](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/single_stage.py#L74-L76)：`losses = self.bbox_head.loss(x, batch_data_samples, ...)`），而 VoteHead 生成投票监督时需要**原始输入点云本身**（要判断哪些点落在 gt 框内）。所以 VoteNet 重写后多传了一个 `points` 参数。

#### 4.1.2 核心流程

```
训练 loss 模式:
  batch_inputs_dict['points'] ──┬─> extract_feat ─> feat_dict ─┐
                                │                              ├─> bbox_head.loss(points, feat_dict, data_samples)
                                └────────── points ────────────┘

推理 predict 模式:
  同上，bbox_head.predict(...) ─> results_list
  ─> add_pred_to_datasample 写入 pred_instances_3d
```

#### 4.1.3 源码精读

VoteNet 类声明与构造函数只是透传，[mmdet3d/models/detectors/votenet.py:L13-L46](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/votenet.py#L13-L46)：注册为 `VoteNet`，把 backbone/bbox_head/train_cfg/test_cfg 全交给 `SingleStage3DDetector`。

重写的 loss 方法在 [mmdet3d/models/detectors/votenet.py:L48-L69](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/votenet.py#L48-L69)：

```python
feat_dict = self.extract_feat(batch_inputs_dict)
points = batch_inputs_dict['points']
losses = self.bbox_head.loss(points, feat_dict, batch_data_samples, **kwargs)
```

——注意 `points`（每个样本一个 `(N, C)` 张量的 list）被一并传入；对比基类版本只有 `x`。predict 同理，[mmdet3d/models/detectors/votenet.py:L98-L104](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/votenet.py#L98-L104)，最后经 `add_pred_to_datasample` 把 `InstanceData` 结果写进每个 `Det3DDataSample.pred_instances_3d`（u2-l4 的契约）。

`extract_feat` 沿用基类实现，[mmdet3d/models/detectors/single_stage.py:L140-L163](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/single_stage.py#L140-L163)：点云 list `torch.stack` 成 `(B, N, C)` 后喂给 backbone（VoteNet 配置里没有 neck），返回的就是 PointNet2SASSG 输出的 dict（u6-l1 讲过：`fp_xyz`/`fp_features`/`fp_indices` 列表 + 各级 SA 特征）。

另外文件尾部还有一个 `aug_test`（[mmdet3d/models/detectors/votenet.py:L106-L148](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/votenet.py#L106-L148)），用于测试时增强：多种增强各自 predict 后经 `merge_aug_bboxes_3d` 合并。主线不经过它，了解即可。

#### 4.1.4 代码实践

1. **实践目标**：确认「配置 → 类」的路由，并亲眼看到 VoteNet 配置的完整展开形态。
2. **操作步骤**：
   ```bash
   # 展开继承链，查看完整配置
   python tools/misc/print_config.py configs/votenet/votenet_8xb8_scannet-3d.py | head -60
   ```
   展开后定位三个区块：`model.backbone`（PointNet2SASSG）、`model.bbox_head`（VoteHead）、`model.train_cfg / test_cfg`。再对照单测 [tests/test_models/test_detectors/test_votenet.py:L19-L23](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_models/test_detectors/test_votenet.py#L19-L23)：`get_detector_cfg(...)` 读配置 → `MODELS.build(cfg)` 一行构建整个模型 → `create_detector_inputs` 造伪输入。
3. **需要观察的现象**：展开配置中 `model.type='VoteNet'`、`bbox_head.type='VoteHead'`，且 `train_cfg` 里没有 `assigner`/`sampler` 字段——这与 SECOND（u5-l4）形成鲜明对比，为 4.4 节「用 chamfer 匹配替代 assigner」埋下伏笔。
4. **预期结果**：能口头回答「VoteNet 配置里谁负责 anchor？」——没有人，它没有 anchor。
5. 以上命令涉及真实环境运行，若本地未装 mmdet3d 则为「待本地验证」；仅阅读展开逻辑也可用 `mmengine.Config.fromfile('configs/votenet/votenet_8xb8_scannet-3d.py')` 等价完成。

#### 4.1.5 小练习与答案

**练习 1**：VoteNet 的 `__init__` 没有自己的逻辑，为什么不直接用 `SingleStage3DDetector` 配 `type='SingleStage3DDetector'`？
**答案**：因为基类的 `loss`/`predict` 不把原始 `points` 传给 bbox_head，而 VoteHead 的监督生成（4.4 节）必须知道每个原始点是否在 gt 框内；重写这两个方法才能把 `points` 送到头里。此外单独的 `VoteNet` 注册名也让配置可读、可被 imvotenet 等子类复用。

**练习 2**：`extract_feat` 返回 dict 而不是 tuple，这对 VoteHead 意味着什么？
**答案**：`SingleStage3DDetector.extract_feat` 的文档注明室内检测通常返回 dict（u6-l1：PointNet2SASSG 输出 `fp_xyz`/`fp_features`/`fp_indices` 等）；VoteHead 用 `_extract_input` 按键名取 `fp_xyz[-1]` 等，dict 是室内骨干与头之间的契约。

---

### 4.2 VoteModule：从种子点到投票点

#### 4.2.1 概念说明

`VoteModule` 是「投票」思想的落地层：输入骨干产出的 **seed points（种子点）** 及其特征，输出每个种子点**投票后的新位置** vote_points 和增强后的特征 vote_features。

三个关键超参：

- `vote_per_seed=1`：每个种子生成几张票。原文默认 1；若是 K，则每个种子裂成 K 个投票点。
- `gt_per_seed=3`：监督时每个种子最多登记几条真值投票——一个点可能同时落在多个物体重叠的框内，最多记 3 个目标中心。
- `in_channels=256`：种子特征通道数，也是残差特征的通道数。

两个设计要点：

1. **回归偏移而非绝对坐标**：网络输出 \(\Delta\)，投票点 \(= p_{seed} + \Delta\)。这就是 2.1 节说的 object coordinate → center coordinate 的偏移，对场景级平移旋转不变。
2. **残差特征 + L2 归一化**（`with_res_feat=True, norm_feats=True`）：投票点不仅要「搬家」，还要「带上行李」——新特征 = 旧特征 + 回归出的残差，再做 L2 归一化让不同种子迁移后的特征尺度一致，便于下一层聚合影比较。

#### 4.2.2 核心流程

```
seed_points (B, N, 3)、seed_feats (B, C, N)        # 注意两个布局不同：xyz 是 N 在末维，特征是 C 在前
   │ vote_conv: Conv1d 256→256→256（BN1d+ReLU）
   │ conv_out: Conv1d 256→(3+C)*vote_per_seed
   ▼
votes (B, (3+C)*K, N) ──transpose/view──> (B, N, K, 3+C)
   │ 前 3 列 = offset Δ
   │ vote_points = seed_points.unsqueeze(2) + Δ      # (B, N, K, 3) → view (B, N*K, 3)
   │ 后 C 列 = res_feats
   │ vote_feats = seed_feats + res_feats → L2 归一化  # (B, C, N*K)
   ▼
返回 (vote_points, vote_feats, offset)
```

投票损失（监督侧）用 Chamfer 距离的单向形式：

\[
L_{vote} = \sum_{i \in \text{前景种子}} \min_{j \in \{1..K_{gt}\}} \| v_i - c^{gt}_{ij} \|
\]

即每个前景种子的 3 条真值投票中，**取离预测最近的一条**来计损——网络只需要投准「其中一个」目标中心即可（点在重叠区域时是合理的要求）。

#### 4.2.3 源码精读

构造函数里的两层网络，[mmdet3d/models/layers/vote_module.py:L69-L91](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/vote_module.py#L69-L91)：`vote_conv` 是一串 1×1 `ConvModule`（配置 `conv_channels=(256, 256)`）；`conv_out` 的输出通道是 `(3 + in_channels) * vote_per_seed`——前 3 列给坐标偏移、后 256 列给特征残差，ScanNet 配置下即 259 通道。

前向主体，[mmdet3d/models/layers/vote_module.py:L119-L141](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/vote_module.py#L119-L141)：

```python
x = self.vote_conv(seed_feats)
votes = self.conv_out(x)                                   # (B, (3+C)*K, N)
votes = votes.transpose(2, 1).view(batch_size, num_seed, self.vote_per_seed, -1)
offset = votes[:, :, :, 0:3]
...
vote_points = (seed_points.unsqueeze(2) + offset).contiguous()
vote_points = vote_points.view(batch_size, num_vote, 3)
```

这段就是 \(v_i = p_i + \Delta_i\) 的向量化实现。可选的 `vote_xyz_range` 会把每轴偏移 `clamp` 在限定范围内（[L129-L137](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/vote_module.py#L129-L137)），防止网络学出「一步跨整个房间」的离谱票；默认配置未启用。

残差特征与归一化，[mmdet3d/models/layers/vote_module.py:L143-L153](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/vote_module.py#L143-L153)：

```python
res_feats = votes[:, :, :, 3:]
vote_feats = (seed_feats.transpose(2, 1).unsqueeze(2) + res_feats).contiguous()
...
features_norm = torch.norm(vote_feats, p=2, dim=1)
vote_feats = vote_feats.div(features_norm.unsqueeze(1))
```

投票损失 `get_loss`，[mmdet3d/models/layers/vote_module.py:L158-L190](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/vote_module.py#L158-L190)，分三步读：

1. **把监督从原始点搬回种子**：`vote_targets` 是按**原始输入点**（40000 个）生成的偏移监督（4.4 节），而预测只有 1024 个种子。`seed_indices` 记录每个种子在原始点中的下标，`torch.gather` 按下标取出该种子的真值投票掩码与偏移，再加回 `seed_points` 得到**绝对坐标**形式的真值投票点（[L175-L181](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/vote_module.py#L175-L181)：`seed_gt_votes += seed_points.repeat(...)`）。
2. **算 Chamfer 并取 `[1]`**：[L184-L187](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/vote_module.py#L184-L187) 调用 `self.vote_loss`（配置为 `ChamferDistance, mode='l1'`），返回四元组 `(loss_src, loss_dst, indices1, indices2)`，取 `[1]` 即 `loss_dst`——**每条真值投票到最近预测投票点的 L1 距离**，形状 `(B*N, gt_per_seed)`。
3. **min 聚合再求和**：[L188](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/vote_module.py#L188) `torch.min(distance, dim=1)[0]` 在 3 条真值投票里取最近者，`sum` 得总损失。前景掩码权重 `weight` 已在步骤里按 `dst_weight` 传入，只让落在物体内的种子贡献损失。

#### 4.2.4 代码实践

1. **实践目标**：亲手跑一次 VoteModule 前向，验证「vote_points = seed + offset」与形状约定。
2. **操作步骤**（示例代码，非项目原有）：

   ```python
   import torch
   from mmdet3d.models.layers import VoteModule

   B, N, C = 2, 1024, 256
   seed_points = torch.rand(B, N, 3)          # (B, N, 3)
   seed_feats = torch.rand(B, C, N)           # (B, C, N)

   m = VoteModule(in_channels=C, vote_per_seed=1, gt_per_seed=3,
                  conv_channels=(256, 256),
                  norm_cfg=dict(type='BN1d'))
   m.eval()
   with torch.no_grad():
       vote_points, vote_feats, offset = m(seed_points, seed_feats)
   print(vote_points.shape, vote_feats.shape, offset.shape)
   # 手工复算：直接用返回的 offset 验证加法关系
   print(torch.allclose(vote_points, (seed_points + offset.transpose(2, 1))))
   ```

3. **需要观察的现象**：三个输出形状分别为 `(2, 1024, 3)`、`(2, 256, 1024)`、`(2, 3, 1024)`；`allclose` 为 True；`vote_feats` 每个点的 L2 范数为 1（归一化生效）。
4. **预期结果**：形状与 4.2.2 的流程图逐项对上；若把 `vote_per_seed` 改为 2，`vote_points` 变 `(2, 2048, 3)`、`conv_out` 输出通道变 518。
5. 本实践需本地安装 mmdet3d 后运行，结果标注为「待本地验证」；BN1d 在 `eval()` 下用运行均值，单次前向不会因 batch 小而报错。

#### 4.2.5 小练习与答案

**练习 1**：`seed_points` 布局是 `(B, N, 3)` 而 `seed_feats` 是 `(B, C, N)`，为什么不同？
**答案**：这是 PointNet 系的约定（u6-l1）：坐标张量以点维在末位方便球查询与 broadcasting（如 `seed_points.unsqueeze(2) + offset`）；特征张量通道维在前是 `Conv1d`（kernel=1 逐点卷积）的自然布局。两套布局在代码中靠 `transpose(2, 1)` 互转。

**练习 2**：为什么投票损失取 `loss_dst`（真值→预测方向）并在 3 条真值里取 `min`，而不是对称 Chamfer？
**答案**：监督语义是「每条真值投票附近应有一个预测」。若取 `loss_src`（预测→真值），一个远处的错误投票只要碰巧靠近任一真值就不受惩罚，无法约束所有票；而 `loss_dst + min` 允许多张真值票共享同一个好预测，只要求每个目标中心至少被投中一次，与「聚簇」的目标一致。

---

### 4.3 VoteHead.forward：投票聚合 → 提案 → 解码

#### 4.3.1 概念说明

投票点生成后还只是一盘散沙——1024 个点的票可能聚成若干簇。`VoteHead.forward` 的后半段要**从票堆里选出提案（proposal）并给它打分**。源码注释把整个 forward 明确分成 4 步：① 从种子生成投票；② 聚合投票；③ 预测框与分数；④ 解码预测（[mmdet3d/models/dense_heads/vote_head.py:L300-L307](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/vote_head.py#L300-L307)），本模块讲 ②③④。

「聚合」复用了 u6-l1 的 **PointSAModule**（采样-分组-聚合）：在投票点上 FPS 选 256 个中心，每个中心球查询（半径 0.3m）取 16 张邻域票，MLP 编码后池化成该提案的 128 维特征。**这正对应 Hough Voting 中「票簇」的概念——球查询半径就是簇的尺度**。

`sample_mode` 决定 FPS 在哪个点集上采样，训练与测试不同（属性方法 [vote_head.py:L109-L116](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/vote_head.py#L109-L116)：训练取 `train_cfg.sample_mode='vote'`，测试取 `test_cfg.sample_mode='seed'`）。

#### 4.3.2 核心流程

```
① seed → vote（4.2 已讲）
② 聚合（sample_mode 四选一）：
   'vote'  : FPS 直接在 vote_points 上选 256 中心            ← 训练配置
   'seed'  : FPS 在 seed_points 上选 256 中心，再取这些位置的票 ← 测试配置
   'random': 随机抽 256 个
   'spec'  : 不采样，中心直接用 seed，票作为 target_xyz（H3DNet 用）
   PointSAModule(num_point=256, radius=0.3, num_sample=16, mlp [256,128,128,128])
   → aggregated_points (B,256,3), features (B,128,256)
③ BaseConvBboxHead：shared conv 128→128→128，分叉两个 1×1 卷积
   cls 通道 = num_classes + 2（objectness 2 + 语义 C）
   reg 通道 = 3 + num_dir_bins*2 + num_sizes*4
④ bbox_coder.split_pred 把 reg 向量按固定顺序切片，center = aggregated_points + 残差
```

#### 4.3.3 源码精读

**入口提取**：[_extract_input, vote_head.py:L130-L157](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/vote_head.py#L130-L157) 先区分两种输入：若 feat_dict 已含 `seed_points/seed_features/seed_indices`（imvotenet 多模态变体提前算好）直接取；否则取 PointNet2SASSG 的 `fp_xyz[-1]`、`fp_features[-1]`、`fp_indices[-1]`——FP 塔末级即 1024 个种子（u6-l1 的下采样塔 2048→1024→512→256 加两级 FP 上采样回到 1024 点、256 通道）。

**第①步 + 第②步**，[vote_head.py:L315-L366](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/vote_head.py#L315-L366)：调用 VoteModule 拿到三元组后，按 `sample_mode` 组装 `aggregation_inputs`。`'vote'` 模式只传 `points_xyz=vote_points, features=vote_features`，FPS 由 SA 模块内部完成；`'seed'` 模式先 `furthest_point_sample(seed_points, self.num_proposal)` 拿到 256 个下标（[L335-L336](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/vote_head.py#L335-L336)），作为 `indices` 传入——**下标是种子坐标系下的，取到的恰是这些位置的投票点**。随后 [L361-L366](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/vote_head.py#L361-L366) 一次调用返回 `(aggregated_points, features, aggregated_indices)`（SA 模块的返回契约见 [point_sa_module.py:L193-L229](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/pointnet_modules/point_sa_module.py#L193-L229)：new_xyz、拼接后的新特征、采样下标）。

**第③步打分头**：[vote_head.py:L368-L369](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/vote_head.py#L368-L369) 调 `self.conv_pred(features)`。`conv_pred` 是 `BaseConvBboxHead`（[base_conv_bbox_head.py:L11-L131](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/base_conv_bbox_head.py#L11-L131)）：先过 shared conv 分支（配置 `shared_conv_channels=(128, 128)`），再分叉 cls/reg 两个 1×1 卷积。输出通道由两个私有方法决定：

- [_get_cls_out_channels, vote_head.py:L118-L121](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/vote_head.py#L118-L121)：`num_classes + 2`。ScanNet 18 类 → **20** = objectness 2 + 语义 18。
- [_get_reg_out_channels, vote_head.py:L123-L128](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/vote_head.py#L123-L128)：`3 + num_dir_bins*2 + num_sizes*4`。ScanNet（`num_dir_bins=1, num_sizes=18`）→ \(3 + 1\times2 + 18\times4 = 77\) = center 3 + dir_class 1 + dir_res 1 + size_class 18 + size_res 54。

**第④步解码**：[vote_head.py:L371-L375](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/vote_head.py#L371-L375) 把 `(cls_predictions, reg_predictions, aggregated_points)` 交给 `bbox_coder.split_pred`。切片逻辑在 [partial_bin_based_bbox_coder.py:L150-L213](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/partial_bin_based_bbox_coder.py#L150-L213)，按 `center(3) → dir_class → dir_res → size_class → size_res → obj_scores(2) → sem_scores` 的固定顺序切开，其中值得注意的两处：

- `center = base_xyz + reg_preds_trans[..., 0:3]`（[L171-L172](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/partial_bin_based_bbox_coder.py#L171-L172)）——中心又是「采样点 + 残差」：提案中心在聚合点基础上微调，而非凭空回归。这是投票范式的第二次「相对化」。
- `size_res` 是 `(B, P, num_sizes, 3)`：**每个尺寸类别都回归一套残差**，推理时用 `argmax` 的类别那套（u5-l6 讲过的 bin-based 哲学）。注意 ScanNet 配置 `with_rot=False`，但 `num_dir_bins=1` 的 dir 分支依然存在，只是解码时朝向恒为 0（[L88-L96](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/partial_bin_based_bbox_coder.py#L88-L96)：`with_rot=False` 时 `dir_angle` 直接置零）。

#### 4.3.4 代码实践

1. **实践目标**：纸面推导 + 配置核对每层通道数，画出数据流图（本讲综合实践的一半）。
2. **操作步骤**：
   - 打开 [configs/_base_/models/votenet.py:L4-L18](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/votenet.py#L4-L18)，从 `in_channels=4`（ScanNet 管线 `LoadPointsFromFile` 取 `use_dim=[0,1,2]` 且 `shift_height=True`，即 x,y,z + 高度列，见 [configs/_base_/datasets/scannet-3d.py:L27-L33](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/scannet-3d.py#L27-L33)）出发，逐层抄下通道数。
   - 再打开 [configs/votenet/votenet_8xb8_scannet-3d.py:L7-L32](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/votenet/votenet_8xb8_scannet-3d.py#L7-L32) 拿到 `num_classes=18 / num_sizes=18 / num_dir_bins=1 / with_rot=False`，代入 4.3.3 的两个通道公式。
3. **需要观察的现象**：你的推导应与下表一致——

   | 阶段 | 张量 | 形状 |
   |---|---|---|
   | 骨干输出 | seed_points / seed_features | (B, 1024, 3) / (B, 256, 1024) |
   | VoteModule conv_out | votes | (B, 259, 1024) |
   | VoteModule 输出 | vote_points / vote_features | (B, 1024, 3) / (B, 256, 1024) |
   | 聚合输出 | aggregated_points / features | (B, 256, 3) / (B, 128, 256) |
   | 打分头输出 | cls_predictions / reg_predictions | (B, 20, 256) / (B, 77, 256) |

4. **预期结果**：通道公式与配置数值吻合；如不吻合，先检查是否忘了 `use_xyz=True` 会给 SA 模块第一层 MLP 拼上 3 维坐标。
5. 纯源码阅读 + 算术推导，无需运行环境，可立即完成。

#### 4.3.5 小练习与答案

**练习 1**：训练用 `sample_mode='vote'`、测试用 `'seed'`，差异会导致训练与测试的提案分布不一致，为什么原文仍这样设计？
**答案**：训练时在投票点上 FPS，提案更贴近真值中心簇，正样本多、回归信号好；测试时预测投票尚不可靠（正是要评估的对象），在稳定的种子点上 FPS、取对应位置的票，能保证提案位置的可重复性与覆盖度。这是「训练用教师信号引导、测试用稳定锚点」的常见折中。

**练习 2**：把 ScanNet 配置换成 SUN RGB-D（`with_rot=True, num_dir_bins=12`，见 [configs/votenet/votenet_8xb16_sunrgbd-3d.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/votenet/votenet_8xb16_sunrgbd-3d.py)），reg 输出通道是多少？
**答案**：SUN RGB-D 10 类，\(3 + 12\times2 + 10\times4 = 71\)；cls 为 \(10+2=12\)。方向不再恒 0，解码时 `argmax(dir_class)` 选 bin 后取对应残差。

**练习 3**：`vote_aggregation` 的 `radius=0.3` 调大会怎样？
**答案**：球查询半径是「票簇尺度」的先验。调大则一个提案聚合进更多邻域票，感受野变大、对小物体不利（不同物体的票被混进同一提案）；调小则簇内票可能采不满 16 个，特征变稀。它与 `train_cfg.pos_distance_thr=0.3` 同为 0.3 并非巧合——提案中心偏离真值 0.3m 以内算正样本，恰是「簇内」的尺度。

---

### 4.4 训练目标生成：get_targets 与 Chamfer 匹配分配

#### 4.4.1 概念说明

VoteNet 没有 anchor，因此没有 u5-l6 那套「assigner 定身份 → sampler 挑下标」的生产线。它要回答的监督问题有三个：

1. **投票监督**：每个原始点应该投给谁？（哪些点是前景、偏移多少）
2. **proposal 匹配**：256 个提案里，谁对应哪个 gt 框？——用 **Chamfer 最近邻**：每个提案中心找最近的 gt 中心，`indices1` 就是分配结果。
3. **正负样本**：提案到最近 gt 中心的欧氏距离 < 0.3m 为正（objectness=1），> 0.6m 为负，0.3~0.6m 之间是**忽略带**（不算正也不算负，objectness 损失不计入）——因为边界距离上的提案「算对算错都勉强」，强行监督会引入噪声。

监督信号分两层生成：`get_targets` 做批处理对齐与权重归一化，`_get_targets_single` 逐样本算全部目标。

#### 4.4.2 核心流程

```
_get_targets_single（单样本）:
  A. 投票目标
     if with_rot（SUN RGB-D）:
        points_in_boxes_all 找出每个 gt 框内的点
        对第 i 框: votes_i = gravity_center_i - 框内点坐标   # 偏移形式存表
        一个点落入多框时, 按先后最多填 3 个目标槽（vote_target_idx clamp max=2）
     elif 有逐点 mask（ScanNet, with_rot=False）:
        pts_instance_mask 按实例找点, 中心 = 点集 min/max 包围盒中心
        vote_targets = 中心 - 实例内点, 再 repeat 成 3 份
  B. 框参数编码: bbox_coder.encode(gt) → center/size_class/size_res/dir_class/dir_res 目标
  C. Chamfer 分配: assignment = chamfer_distance(aggregated_points, center_targets) 的 indices1
     距离 < 0.3 → 正; > 0.6 → 计负; 中间忽略
     dir_res 目标 /= (π/num_dir_bins); size_res 目标 /= 该类 mean_sizes   # 归一化
     size_class/dir_class/.../mask_targets 全部按 assignment 索引到每个提案

get_targets（批层）:
  空真值样本造假框占位（valid_gt_masks=0）
  multi_apply 并行逐样本 → stack + pad 到最大 gt 数
  三套权重归一化: objectness_weights / box_loss_weights / valid_gt_weights
```

#### 4.4.3 源码精读

**投票目标（旋转框路线，SUN RGB-D）**：[vote_head.py:L631-L660](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/vote_head.py#L631-L660)。核心三行：

```python
box_indices_all = gt_bboxes_3d.points_in_boxes_all(points)     # (N, num_gt) 框内判定
...
votes = gt_bboxes_3d.gravity_center[i].unsqueeze(0) - selected_points[:, :3]
```

`points_in_boxes_all` 是框对象的方法（u2-l2 讲过），一次算出所有点与所有框的包含关系；真值偏移指向 **gravity_center（几何中心）**——这与室内 Depth 框「锚点在底面中心」的默认约定不同，VoteNet 全程用几何中心。`vote_target_masks[indices] = 1` 标记前景点；[L659-L660](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/vote_head.py#L659-L660) 的 `torch.clamp(vote_target_idx[indices] + 1, max=2)` 实现「一个点最多登记 3 个目标槽」：落在第 1、2、3 个重叠框内时依次填第 0、1、2 槽。

**投票目标（实例 mask 路线，ScanNet）**：[vote_head.py:L661-L674](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/vote_head.py#L661-L674)。因为 ScanNet 的 gt 框是 6 维轴对齐框、且数据集天然带逐点 instance mask（u3-l4），直接按 `pts_instance_mask` 分实例更准：中心取**实例点集 min/max 包围盒的中心**（`0.5 * (min + max)`），最后 `vote_targets.repeat((1, gt_per_seed))` 铺成 3 份。注意入口断言 [L625](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/vote_head.py#L625)：`with_rot` 与 `pts_semantic_mask` 至少得有一个，否则无法生成投票目标。

**框参数编码与 Chamfer 分配**：[vote_head.py:L678-L688](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/vote_head.py#L678-L688)：

```python
(center_targets, size_class_targets, size_res_targets,
 dir_class_targets, dir_res_targets) = self.bbox_coder.encode(gt_bboxes_3d, gt_labels_3d)
...
distance1, _, assignment, _ = chamfer_distance(
    aggregated_points.unsqueeze(0), center_targets.unsqueeze(0), reduction='none')
euclidean_distance1 = torch.sqrt(distance1.squeeze(0) + 1e-6)
```

`distance1` 是 L2 模式下每个提案到最近 gt 的**距离平方**，开方得欧氏距离；`assignment`（即 `indices1`）长度 256，第 k 个元素是提案 k 匹配到的 gt 下标。这就是「assigner」的全部——一行 Chamfer。

**正负判定与目标索引**：[vote_head.py:L690-L717](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/vote_head.py#L690-L717)。`objectness_targets` 在距离 < `pos_distance_thr`(0.3) 处置 1；`objectness_masks` 在 <0.3 或 >`neg_distance_thr`(0.6) 处置 1.0（中间带为 0，不参与 objectness 损失）。随后 `dir_class_targets[assignment]`、`size_res_targets[assignment]`、`mask_targets = gt_labels_3d[assignment]`、`assigned_center_targets = center_targets[assignment]`——**把 gt 级目标按下标查表展开成提案级目标**。两处归一化让回归目标落到 O(1) 量级：`dir_res_targets /= (np.pi / num_dir_bins)`（按 bin 角宽归一）与 `size_res_targets /= pos_mean_sizes`（按该类别平均尺寸归一，`mean_sizes` 来自 ScanNet 配置的 18 行统计值），它们恰好是 coder 解码 `dir_res = dir_res_norm * (π/N)`、`size_res = size_res_norm * mean_sizes`（[partial_bin_based_bbox_coder.py:L184-L202](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/partial_bin_based_bbox_coder.py#L184-L202)）的逆过程。

**批处理层**：[get_targets, vote_head.py:L529-L599](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/vote_head.py#L529-L599) 先给空真值样本造假框占位（`valid_gt_masks=0`，防止下游 stack 形状不齐），`multi_apply` 逐样本调用后 pad 到批内最大 gt 数，最后产出三套权重：`objectness_weights`（哪些提案计 objectness 损失）、`box_loss_weights`（正样本归一化权重，喂给 center/dir/size/sem 回归类损失）、`valid_gt_weights`（真 gt 的归一化权重，喂给 center 损失的反方向）。14 个目标张量按固定顺序打包返回，`loss_by_feat` 解包（[L410-L414](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/vote_head.py#L410-L414)）。

#### 4.4.4 代码实践

1. **实践目标**：用一张伪代码表追踪「一条 gt 框 → 它监督了哪些张量」。
2. **操作步骤**（源码阅读型）：
   - 假设单样本有 1 张椅子框，提案 256 个。读 [vote_head.py:L682-L717](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/vote_head.py#L682-L717)，写出 `assignment` 的值域（全 0，因为只有 1 个 gt）。
   - 接着回答：`objectness_targets` 里最多几个 1？（≤ 256，取决于距椅心的提案数）；`mask_targets` 的形状与取值？（`(256,)`，全为椅子类 id）。
   - 对照 VoteModule.get_loss（4.2.3）确认 `seed_indices` 的来源链：`fp_indices[-1]`（骨干 FP 塔）→ `_extract_input` → `results['seed_indices']` → gather。
3. **需要观察的现象**：你应能发现「监督定义在原始点（40000），预测定义在种子（1024）」这座桥完全靠 `seed_indices` 搭起；若 FP 模块不返回索引，投票损失无法计算。
4. **预期结果**：得到一张「目标张量 → 形状 → 由哪段代码产生 → 被哪个损失消费」的四列表（综合实践会用到）。
5. 纯阅读推导，无需运行。

#### 4.4.5 小练习与答案

**练习 1**：为什么 0.3~0.6m 之间的提案不计 objectness 损失，而 u5-l4 的 Anchor3DHead 没有「忽略带」？
**答案**：anchor 范式里 anchor 位置固定、IoU 分档天然有 0~1 的连续过渡，mmdet 的 MaxIoUAssigner 同样有忽略带（u5-l6）；VoteNet 的提案位置是学出来的，落在 0.3~0.6m 的提案「正在学往哪去」，此刻罚它是错还是对说不清，忽略带给了它「学徒期」。若去掉忽略带，负样本会把边界提案过早地推向 0 分，抑制中心回归的学习。

**练习 2**：ScanNet 路线为什么用「实例点集 min/max 包围盒中心」而不是 gt 框的 gravity_center 作为投票目标？
**答案**：ScanNet 的 gt 框是 axis-aligned 的 6 维框且个别标注与点云略有出入；逐点 instance mask 是更原始、更可靠的监督源，由点集直接算中心不依赖框标注质量。而 SUN RGB-D 框带朝向、且其数据管线不总提供逐点 mask，故走 `points_in_boxes_all` + gravity_center 路线。

**练习 3**：`dir_res_targets /= (np.pi / num_dir_bins)` 在 ScanNet（num_dir_bins=1）下除以多少？这意味着什么？
**答案**：除以 π。num_dir_bins=1 时只有 1 个朝向 bin，bin 角宽 2π/1 = 2π，但代码归一化用 π/1 = π（与解码端 `dir_res_norm * (π/N)` 严格互逆），朝向恒 0、该分支实际不起作用，仅保持张量结构一致。

---

### 4.5 loss_by_feat 与 predict_by_feat：八项损失与 NMS 出框

#### 4.5.1 概念说明

训练侧，`loss_by_feat` 把 4.4 的目标与 4.3 的预测对齐成 **8 项损失**：vote（投票回归）、objectness（是否物体）、semantic（是哪类）、center（中心回归，双向 Chamfer）、dir_class/dir_res（朝向，ScanNet 下退化）、size_class/size_res（尺寸分类 + 残差）。可选项 `iou_loss` 只有 `votenet_head-iouloss` 变体启用。

推理侧，`predict_by_feat` 承担 `get_bboxes` 的角色：解码 256 个提案 → 逐样本 NMS → 产出 `InstanceData(bboxes_3d, scores_3d, labels_3d)`。VoteNet 的 NMS 有两个室内特色：先用「框内点数 > 5」滤掉空框，再用轴对齐的 `aligned_3d_nms`（u5-l7）按类去重；`per_class_proposal=True` 时每类各自打分，一个框可重复出现在多个类别里。

#### 4.5.2 核心流程

```
loss_by_feat:
  get_targets → 14 元组
  vote_loss       : VoteModule.get_loss（前景种子, chamfer 单向 min）
  objectness_loss : CE(obj_scores, objectness_targets), class_weight=[0.2,0.8], weight=objectness_weights
  center_loss     : ChamferDistance(l2)(center ↔ center_targets) 双向求和
                    src 方向按 box_loss_weights, dst 方向按 valid_gt_weights
  dir_class_loss  : CE(dir_class, dir_class_targets)
  dir_res_loss    : SmoothL1( argmax-bin 残差 ↔ dir_res_targets )   # one-hot 取对应 bin
  size_class_loss : CE(size_class, size_class_targets)
  size_res_loss   : SmoothL1( 对应类残差 ↔ size_res_targets )
  semantic_loss   : CE(sem_scores, mask_targets)

predict_by_feat:
  obj_scores = softmax(obj_scores)[..., -1]        # 取「是物体」概率
  sem_scores = softmax(sem_scores)
  bbox3d = bbox_coder.decode(...)                  # (B, 256, 6/7)
  逐样本 multiclass_nms_single:
    ① 框内点数 > 5 过滤空框
    ② corners → minmax 6 维对角框
    ③ aligned_3d_nms(minmax, obj_scores, 类别, nms_thr)
    ④ score_thr 过滤
    ⑤ per_class_proposal: 每类 score = obj * sem[:,k]
  → box_type_3d 包装 → InstanceData
```

#### 4.5.3 源码精读

**八项损失**：[vote_head.py:L416-L487](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/vote_head.py#L416-L487)。几个值得停留的点：

- 投票损失入口在 [L417-L420](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/vote_head.py#L417-L420)，把 `seed_points/vote_points/seed_indices` 三个中间结果从 `bbox_preds_dict` 里取出交给 4.2 讲过的 `VoteModule.get_loss`——**forward 的中间张量通过 results dict 直达损失函数**，这是 VoteHead「前向产 dict、损失查 dict」的组织方式。
- objectness 的类别权重 `[0.2, 0.8]`（配置 [configs/_base_/models/votenet.py:L44-L48](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/votenet.py#L44-L48)）：256 个提案里正样本稀少，给「负类」降权、正类升权，再乘 `loss_weight=5.0` 抬高整体地位。
- center 损失是**双向 Chamfer**（[L429-L434](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/vote_head.py#L429-L434)）：`src_weight=box_loss_weights` 让每个**正提案**逼近最近 gt 中心；`dst_weight=valid_gt_weights` 让每个 **gt 中心**至少被一个提案覆盖——两个方向分别对应「别乱预测」和「别漏预测」。
- dir_res / size_res 损失前都有一段 one-hot 技巧（[L443-L450](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/vote_head.py#L443-L450) 与 [L459-L471](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/vote_head.py#L459-L471)）：回归分支给**每个 bin 都输出残差**，但训练时用**真值 bin 的 one-hot** 点乘出那一条参与损失——「全 bin 输出、真值 bin 监督」，推理时换成 argmax bin（解码端 [partial_bin_based_bbox_coder.py:L88-L106](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/partial_bin_based_bbox_coder.py#L88-L106) 同款操作）。
- 可选 iou_loss 在 [L489-L498](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/vote_head.py#L489-L498)：用 `decode_corners` 把预测与目标的 6 维对角框直接算 IoU 损失，`assigned_center_targets` 在此登场（这就是 get_targets 里它存在的意义）。

**推理出框**：[predict_by_feat, vote_head.py:L746-L776](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/vote_head.py#L746-L776)。`obj_scores` 先 softmax 再取 `[..., -1]`——objectness 两列里第二列才是「是物体」的概率；`sem_scores` 整表 softmax；`bbox_coder.decode` 把 4.3 的切片结果拼成 `(B, 256, 6)`（ScanNet）。逐样本进 NMS：

[multiclass_nms_single, vote_head.py:L793-L837](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/vote_head.py#L793-L837) 顺序非常讲究：

1. 用原始点云做「空框过滤」：`bbox.points_in_boxes_all(points)` 后转置求和，**框内点数 > 5** 才保留（[L798-L805](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/vote_head.py#L798-L805)）——预测了框却罩不住任何点的提案直接淘汰，比任何打分都硬核。
2. `bbox.corners` 取 8 顶点后压成 **min/max 对角 6 维框**（[L800-L803](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/vote_head.py#L800-L803)），交给 `aligned_3d_nms`，按 `argmax(sem_scores)` 的类别标签做类别感知去重（[L807-L811](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/vote_head.py#L807-L811)）。
3. NMS 幸存者再过 `score_thr=0.05`（[L814-L819](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/vote_head.py#L814-L819)）。
4. `per_class_proposal=True`（[L821-L831](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/vote_head.py#L821-L831)）：把幸存框复制 18 份，第 k 份的分数 = `obj_scores * sem_scores[:, k]`、标签 = k——**最终分数是「是物体」与「是这类」的乘积**，评估时每类各自排序。

最后 `input_meta['box_type_3d']`（u3-l1 讲过的元信息注入）按数据集的框类型打包成 `DepthInstance3DBoxes`，注意此处 `origin=(0.5, 0.5, 0.5)`（[L793-L797](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/vote_head.py#L793-L797)）——解码出的中心是几何中心，与 4.4 的 gravity_center 监督闭环。

#### 4.5.4 代码实践

1. **实践目标**：理解 NMS 三道闸门的作用，并学会用单测验证前向。
2. **操作步骤**：
   - 阅读 [tests/test_models/test_detectors/test_votenet.py:L39-L69](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_models/test_detectors/test_votenet.py#L39-L69)：`model.forward(**data, mode='predict')` 断言 `pred_instances_3d` 含三个键；`mode='loss'` 断言 8 项损失全部 > 0（CUDA 环境下）。本地可运行：
     ```bash
     pytest tests/test_models/test_detectors/test_votenet.py -v
     ```
   - 思考实验（可本地验证）：把 `test_cfg.nms_thr` 从 0.25 调到 0.8、`score_thr` 从 0.05 调到 0.5，重跑推理，观察保留框数量与 mAP 的变化。
3. **需要观察的现象**：`nms_thr` 变大 → 去重更松 → 保留框更多（召回升、精确降）；`score_thr` 变大 → 低分框被砍 → 框少而准。单测中 8 项损失在随机初始化下均应严格大于 0。
4. **预期结果**：能回答「空框过滤放在 NMS 之前还是之后？为什么？」（之前——先用免费的几何事实裁掉明显垃圾，减少 NMS 的候选量与误保留）。
5. 单测与调参均需本地 GPU 环境与 mmdet3d 安装，标注「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：`per_class_proposal=True` 时最终分数为什么用乘积 `obj * sem[:,k]` 而不是只用 `sem[:,k]`？
**答案**：sem_scores 是语义 softmax，即使框里没东西，18 类概率也总和为 1、必有一类接近 1；obj_scores 才是「这里有没有物体」的判据。乘积保证「物体性低」的框在所有类别上都拿不到高分。18 份复制让每个类别都能按自己的分数排序，配合类别感知的评估（IndoorMetric mAP@0.25/0.5）。

**练习 2**：`semantic_loss` 监督的是逐点语义分割吗？
**答案**：不是。它名为 semantic，实际监督 `sem_scores`（每个**提案**的类别分布），目标是 `mask_targets = gt_labels_3d[assignment]`——提案级分类损失。逐点的 `pts_semantic_mask` 在训练中只用于 4.4 的投票目标生成（ScanNet 路线）。

**练习 3**：为什么推理包装框时 `origin=(0.5, 0.5, 0.5)`，而 u3-l4 说 DepthInstance3DBoxes 默认锚点是底面中心？
**答案**：VoteNet 的 bbox_coder 编码/解码全程使用**几何中心**（encode 目标来自 `gravity_center`，decode 输出的 center 也是几何中心），所以构造框对象时必须显式传 `origin=(0.5,0.5,0.5)` 告诉容器「这批坐标是几何中心」，容器内部会换算回它默认的底面中心存储（u2-l2 的 origin 换算），否则框会整体下沉半个高度。

## 5. 综合实践

把全讲串起来：**画出 VoteNet 端到端数据流图并做通道核对**（对应大纲实践任务）。

1. **实践目标**：不看讲义，独立完成「seed 点 → vote 偏移 → 分组聚类 → bbox head 打分」的完整数据流图，并用配置核对每层通道数。
2. **操作步骤**：
   - 准备一张大纸或文本文件，从 `(B, 40000, 4)` 输入开始，依次画出：PointNet2SASSG（SA 塔 + FP 塔）→ VoteModule（vote_conv/conv_out、offset 相加、残差特征归一化）→ vote_aggregation（四种 sample_mode 画成四条支路，标注训练/测试各走哪条）→ BaseConvBboxHead（shared + cls/reg）→ split_pred 切片 → get_targets（Chamfer assignment、两档阈值、两处归一化）→ 8 项损失 / multiclass_nms_single 三道闸门。
   - 每条边上标张量形状，每个方框上标 `type` 字符串与源码文件；形状推导依据 4.3.4 的表格，配置依据 [configs/_base_/models/votenet.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/votenet.py) 与 [configs/votenet/votenet_8xb8_scannet-3d.py:L7-L32](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/votenet/votenet_8xb8_scannet-3d.py#L7-L32)。
   - 自查三个闭环：① 投票监督定义在 40000 原始点上、预测在 1024 种子上，`seed_indices` 是桥；② `dir_res/size_res` 的目标归一化与 coder 解码严格互逆；③ center 监督用 gravity_center、出框用 `origin=(0.5,0.5,0.5)`，两端口径一致。
3. **需要观察的现象**：画完后你会发现整张图只有两处「learnable 采样点」——骨干 SA 塔与 vote_aggregation 的 FPS；其余全是逐点卷积与查表。这是点云原生检测器与体素检测器（单元 5）在结构上的本质差异。
4. **预期结果**：一张可对照源码逐行验证的数据流图 + 一张通道核对表（20/77 等数字全部对上）。
5. 纸面实践无需环境；若想抽查某一环，可运行 4.2.4 的 VoteModule 最小示例（待本地验证）。

## 6. 本讲小结

- VoteNet 的核心是**投票**：表面点（object coordinate）回归指向物体中心（center coordinate）的偏移，票在中心处聚簇，把「稀疏表面的检测」转化为「稠密簇中心的分类回归」；全程使用几何中心（gravity_center）口径。
- `VoteNet` 检测器是薄壳，重写 `loss/predict` 的唯一目的是把**原始点云**随特征一起传给 VoteHead——投票监督需要判断「点在不在 gt 框内」。
- `VoteModule` 用两层 1×1 卷积输出 `(3+C)*vote_per_seed` 通道：前 3 列坐标偏移（vote = seed + offset），后 C 列特征残差（L2 归一化）；投票损失是单向上 Chamfer，每个前景种子在 3 条真值投票中取最近者。
- `VoteHead.forward` 四步：生成投票 → **PointSAModule 聚合**（训练 `sample_mode='vote'` 在投票点上 FPS，测试 `'seed'` 在种子点上 FPS）→ BaseConvBboxHead 双分支（cls = C+2、reg = 3+num_dir_bins×2+num_sizes×4）→ `split_pred` 切片，center = aggregated_points + 残差。
- 无 anchor 也就无 assigner：proposal↔gt 用 **Chamfer 最近邻**（`indices1`）匹配，距离 <0.3 正、>0.6 负、中间忽略；`dir_res`、`size_res` 目标分别按 bin 角宽与类别 mean_sizes 归一化，与解码严格互逆。
- 出框三道闸门：框内点数 >5 的空框过滤 → 轴对齐 `aligned_3d_nms`（类别感知）→ `score_thr`；`per_class_proposal` 让最终分数 = objectness × 各类语义概率，一个框可按多类别参评。

## 7. 下一步学习建议

- **下一讲 u6-l3** 将横向对比 GroupFree3D（transformer 解码器替代投票聚合）、3DSSD（F-FPS 前景采样 + 辅助分支）、H3DNet（面/边/角基元回归）——它们都站在 VoteNet 的提案框架上，建议先记住本讲的「提案 = 采样点 + 分组特征」抽象，届时只需对比「提案从哪来、怎么精修」。
- 想巩固投票思想可回读原论文 *Deep Hough Voting for 3D Object Detection in Point Clouds*（arXiv:1904.09664），对照 [vote_head.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/vote_head.py) 的实现差异。
- 多模态扩展可读 [mmdet3d/models/detectors/imvotenet.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/imvotenet.py)（ImVoteNet）：它在图像上跑 2D 检测、把 2D 票反投影拼进 3D 种子特征——`_extract_input` 里那个 `if 'seed_points' in feat_dict` 分支正是为它留的入口。
- 两阶段路线（u6-l4 Part-A2 / PV-RCNN）会复用本讲的 `BaseConvBboxHead` 与投票式关键点思想，可提前浏览 [mmdet3d/models/roi_heads/](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/) 目录。

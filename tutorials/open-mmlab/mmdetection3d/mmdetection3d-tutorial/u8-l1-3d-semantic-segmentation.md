# 3D 语义分割：Cylinder3D 与 MinkUNet

## 1. 本讲目标

前面的单元里，我们一直在和「3D 框」打交道：模型输出的是若干物体的中心、尺寸、朝向。本讲切换到另一大类任务——**3D 语义分割**：模型对点云中的**每一个点**输出一个类别标签（道路、植被、汽车……），不再有框、anchor、NMS。

学完本讲你应该能够：

1. 说出 `Seg3DDataset` 与 `Det3DDataset` 在数据契约上的核心差异（逐点标签 vs 实例框标签、两级标签映射、`dataset` 注入）。
2. 讲清 `segmentors`（分割器）子系统的组织方式：`Base3DSegmentor` → `EncoderDecoder3D` → `Cylinder3D` / `MinkUNet` 的继承层次，以及 `postprocess_result` 如何把 logits 变成 `pred_pts_seg`。
3. 理解 Cylinder3D 的两大设计：**柱体分区**（cylindrical voxelization，把点云转到柱坐标再栅格化）与**不对称稀疏卷积**（`(1,3,3)` / `(3,1,3)` 交替的卷积核）。
4. 理解 MinkUNet 的**立方体量化 + 每体素代表点**路线，以及它对三种稀疏卷积后端（torchsparse / spconv / Minkowski Engine）的适配方式。
5. 对比两条路线的 voxel/点表示差异，并解释 decode head 中「逐点（逐体素）分类分支」的结构。

## 2. 前置知识

**语义分割 vs 检测**。检测回答「哪里有什么物体」（输出实例框 + 类别 + 置信度）；语义分割回答「这个点属于哪一类」（输出与点数等长的标签数组）。3D 语义分割的常用指标是 mIoU（mean Intersection over Union）：

\[
\text{mIoU} = \frac{1}{C}\sum_{c=1}^{C}\frac{|P_c \cap G_c|}{|P_c \cup G_c|}
\]

其中 \(P_c\)、\(G_c\) 分别是预测和真值中类别 \(c\) 的点集合。mIoU 是**按点数算的集合交并比**，这解释了后面 Cylinder3DHead 为什么用 Lovasz 损失——它是 Lovasz 扩展下对 IoU 的直接凸替代。

**逐点标签与 `gt_pts_seg`**。在 [u2-l4](./u2-l4-det3d-data-sample.md) 中我们讲过 `Det3DDataSample` 的点级字段：`gt_pts_seg` / `pred_pts_seg` 装的是 `PointData`，要求所有字段一维且长度等于点数。分割任务的正线就是沿着这个契约展开的。本讲还会遇到两个派生字段：`voxel_semantic_mask`（体素级标签，长度 = 体素数）与 `point2voxel_map`（每个点属于哪个体素的下标，长度 = 点数）——它们不是数据管线产出的，而是模型前处理（体素化）时**动态写进** data_sample 的。

**稀疏卷积**。承接 [u5-l2](./u5-l2-second-backbone-sparse-encoder.md)：点云栅格化后 99% 以上的格子是空的，稀疏卷积只对非空格（active site）做计算。`SubMConv3d` 的输出位置 = 输入位置（矩阵卷积），`SparseConv3d` 会随 stride 改变分辨率，`SparseInverseConv3d` 借助记录的索引表做「逆卷积」上采样。这些算子来自 mmcv 封装的 spconv 库。

**忽略标签 ignore_index**。SemanticKITTI 原始标注有 20 多个大类（官方 id 0~259），但统一基准只评 19 类，其余统统映射为「无标签」。mmdet3d 的约定：19 个有效类是 0~18，无效类统一压成 `ignore_index = 19`（= 类别数），损失计算时跳过。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `mmdet3d/datasets/seg3d_dataset.py` | 分割数据集基类：标签映射、场景采样、`dataset` 注入 |
| `mmdet3d/datasets/semantickitti_dataset.py` | SemanticKITTI 数据集类，重写标签映射构建 |
| `mmdet3d/datasets/s3dis_dataset.py` | S3DIS 室内分割数据集类（`S3DISSegDataset`） |
| `mmdet3d/models/segmentors/base.py` | `Base3DSegmentor`：三模式入口 + `postprocess_result` |
| `mmdet3d/models/segmentors/encoder_decoder.py` | `EncoderDecoder3D`：编解码分割器模板 |
| `mmdet3d/models/segmentors/cylinder3d.py` | Cylinder3D 分割器（薄壳 + `voxel_encoder` 槽位） |
| `mmdet3d/models/segmentors/minkunet.py` | MinkUNet 分割器 |
| `mmdet3d/models/backbones/cylinder3d.py` | `Asymm3DSpconv` 骨干：不对称块 + U 形塔 + DDCM |
| `mmdet3d/models/backbones/minkunet_backbone.py` | `MinkUNetBackbone`：三后端稀疏 UNet |
| `mmdet3d/models/backbones/spvcnn_backone.py` | `SPVCNNBackbone` / `MinkUNetBackboneV2`：体素-点双分支 |
| `mmdet3d/models/voxel_encoders/voxel_encoder.py` | `SegVFE`：分割体素编码器（逐点 MLP + max 聚合） |
| `mmdet3d/models/decode_heads/decode_head.py` | `Base3DDecodeHead`：loss 模板 |
| `mmdet3d/models/decode_heads/cylinder3d_head.py` | `Cylinder3DHead`：稀疏卷积分类 + CE/Lovasz |
| `mmdet3d/models/decode_heads/minkunet_head.py` | `MinkUNetHead`：Linear 分类 |
| `mmdet3d/models/data_preprocessors/data_preprocessor.py` | `cylindrical` / `minkunet` 两种体素化分支 |
| `configs/_base_/models/cylinder3d.py`、`configs/_base_/models/minkunet.py` | 两个模型的基座配置 |
| `configs/_base_/datasets/semantickitti.py` | SemanticKITTI 数据管线基座 |
| `demo/pcd_seg_demo.py` | 点云分割 demo 入口 |

## 4. 核心概念与源码讲解

本讲的四个最小模块：**Seg3DDataset**（数据侧）、**segmentors 组织**（模型骨架）、**Cylinder3D 全链路**（柱体路线）、**MinkUNet/SPVCNN**（立方体路线）。

### 4.1 Seg3DDataset：分割数据集基类

#### 4.1.1 概念说明

[u3-l1](./u3-l1-det3d-dataset-base.md) 讲过 `Det3DDataset` 的职责：读 info pkl、`parse_data_info` 拼路径、`parse_ann_info` 把实例标注包装成 3D 框。分割数据集没有「实例」这个概念，标注就是**与点云逐点对齐的标签数组**（存成 `.bin` 或 `.npy`，每个点一个 int）。所以 `Seg3DDataset` 要解决的问题变成了：

- **标签空间归一**：不同数据集的原始类别 id 五花八门（SemanticKITTI 里 car 是 10、moving-car 是 252），要统一映射到连续的 0~18 加一个忽略位 19。
- **子集训练**：允许用户在 `metainfo['classes']` 里只选一部分类，其余全部进忽略位。
- **场景级采样**：一帧大场景十万点、一帧小场景一千点，如果按帧等概率采样，小场景被「看」的次数相对太少；`scene_idxs` 允许大场景重复出现。
- **混合数据增强**：LaserMix / PolarMix 这类增强需要**从数据集里再抽别的帧**来混合，因此数据集对象本身要传进管线。

#### 4.1.2 核心流程

```text
info pkl (v2 格式)
  └─ Seg3DDataset.__init__
       ├─ ignore_index = len(classes)        # 默认 19
       ├─ get_label_mapping(new_classes)     # dict: 原始类别名 → 新标签
       ├─ get_seg_label_mapping(metainfo)    # np 数组: 原始 seg id → 0..18 / 19
       └─ BaseDataset.__init__               # 读 pkl、逐条回调 parse_data_info
            └─ get_scene_idxs → 重排 data_list（大场景重复出现）

每条样本 parse_data_info:
  lidar_points.lidar_path       → 拼绝对路径
  pts_semantic_mask_path        → 拼绝对路径
  seg_label_mapping (np 数组)   → 注入 info，交给管线的 PointSegClassMapping
```

两级映射的分工是本模块的关键：

- `label_mapping` / `label2cat`：**字典**，作用于「类别 id 集合」层面，决定哪些原始 id 是有效类、哪些进忽略位；主要给配置和评估用。
- `seg_label_mapping`：**numpy 查表数组**，下标是数据文件里的原始 seg id，值是归一后的标签；由管线里的 `PointSegClassMapping` 消费，做 `label_mapping[pts_semantic_mask]` 一次花式索引。

#### 4.1.3 源码精读

类的注册与 METAINFO：`Seg3DDataset` 直接继承 mmengine 的 `BaseDataset`（注意：与 `Det3DDataset` 是平级兄弟，不是继承关系），METAINFO 比检测版多了 `seg_valid_class_ids`（参与训练的原始 id）与 `seg_all_class_ids`（标签文件里可能出现的全部 id）：

- [mmdet3d/datasets/seg3d_dataset.py:L12-L16](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/seg3d_dataset.py#L12-L16)：注册 `Seg3DDataset` 并声明它是 ScanNet / S3DIS / SemanticKIT 的公共基类。
- [mmdet3d/datasets/seg3d_dataset.py:L55-L60](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/seg3d_dataset.py#L55-L60)：`METAINFO` 四件套，`seg_valid_class_ids` / `seg_all_class_ids` 是分割特有。
- [mmdet3d/datasets/seg3d_dataset.py:L84-L98](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/seg3d_dataset.py#L84-L98)：构造函数里先定 `ignore_index`（缺省即类别数），再做两级标签映射并塞进 `metainfo`——这样映射表会随数据集元信息一起传给管线与可视化。
- [mmdet3d/datasets/seg3d_dataset.py:L188-L209](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/seg3d_dataset.py#L188-L209)：`get_seg_label_mapping` 的实现：先建一个全填 `len(seg_valid_class_ids)`（即忽略位）的数组，再把有效 id 逐个写成 0、1、2……这就是「无效类全部塌缩到 ignore_index」的机制。

`parse_data_info` 与检测版的差异——没有 `ann_info`/`instances` 处理，只有路径拼接与映射表注入：

- [mmdet3d/datasets/seg3d_dataset.py:L240-L287](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/seg3d_dataset.py#L240-L287)：把 `lidar_points.lidar_path`、`pts_instance_mask_path`、`pts_semantic_mask_path` 拼成绝对路径，并把 `seg_label_mapping` 数组写进 info；测试模式下准备空的 `eval_ann_info`（分割的真值由管线加载、随 data_sample 流转）。

两个分割特有的行为：

- [mmdet3d/datasets/seg3d_dataset.py:L289-L305](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/seg3d_dataset.py#L289-L305)：训练态的 `prepare_data` 把 `data_info['dataset'] = self` 注入——这是给 LaserMix / PolarMix 等「跨帧混合」增强开的口子，增强类可以拿 dataset 去取别的场景。`Det3DDataset` 没有这一步。
- [mmdet3d/datasets/seg3d_dataset.py:L307-L329](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/seg3d_dataset.py#L307-L329)：`get_scene_idxs`，支持从 `.npy` 读预计算的场景索引（「点多的场景多采几次」）。

SemanticKITTI 子类几乎只是换 METAINFO，但重写了 `get_seg_label_mapping`：

- [mmdet3d/datasets/semantickitti_dataset.py:L46-L61](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/semantickitti_dataset.py#L46-L61)：19 个类名、调色板、`seg_valid_class_ids = tuple(range(19))`。
- [mmdet3d/datasets/semantickitti_dataset.py:L91-L95](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/semantickitti_dataset.py#L91-L95)：映射数组直接由配置里的 `seg_label_mapping`（即 `labels_map` 字典）与 `max_label=259` 构建，覆盖基类默认逻辑——因为 SemanticKITTI 的原始 id 不连续（10、25、252……），必须显式给出整张表。

这张表在配置基座里维护，值得对照看：[configs/_base_/datasets/semantickitti.py:L12-L50](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/semantickitti.py#L12-L50) 把 0（unlabeled）、1（outlier）、52（other-structure）、99（other-object）等统统映到 19，252（moving-car）映到 0（car）——「动/静同类」是 SemanticKITTI 的官方约定。

管线的最后一层加工是 `PointSegClassMapping`：

- [mmdet3d/datasets/transforms/loading.py:L477-L504](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L477-L504)：一次花式索引 `label_mapping[pts_semantic_mask]` 完成全部原始 id → 训练 id 的转换，并同步更新 `eval_ann_info`，保证评估与训练看到同一套标签。

#### 4.1.4 代码实践

**实践目标**：不下载任何数据，手工复现 `get_seg_label_mapping`，验证 SemanticKITTI 的标签映射行为。

1. 操作步骤（示例代码，非项目原有代码）：

   ```python
   import numpy as np
   from mmengine import Config

   cfg = Config.fromfile('configs/_base_/datasets/semantickitti.py')
   metainfo = cfg.metainfo

   # 复刻 SemanticKittiDataset.get_seg_label_mapping 的逻辑
   table = np.zeros(metainfo['max_label'] + 1, dtype=np.int64)
   for idx in metainfo['seg_label_mapping']:
       table[idx] = metainfo['seg_label_mapping'][idx]

   print('table[10] =', table[10], '  # car -> 0')
   print('table[252] =', table[252], '  # moving-car -> 0')
   print('table[0] =', table[0], '  # unlabeled -> 19')
   print('table[1] =', table[1], '  # outlier -> 19')

   # 模拟一帧 8 个点的原始标签，走一遍 PointSegClassMapping 的变换
   raw = np.array([10, 252, 30, 0, 99, 40, 70, 81], dtype=np.int32)
   print('mapped:', table[raw])
   ```

2. 需要观察的现象：`table[10]` 与 `table[252]` 相同（动/静合并）；`table[0]`、`table[1]` 都是 19。
3. 预期结果：`mapped` 为 `[0 0 5 19 19 8 14 18]`。
4. 说明：本实践只依赖 mmengine 与仓库配置文件，可在 CPU 环境运行；若你的环境中 `Config.fromfile` 路径不同，请先 `cd` 到仓库根目录。

#### 4.1.5 小练习与答案

**练习 1**：`Seg3DDataset` 与 `Det3DDataset` 都继承 mmengine 的 `BaseDataset`，二者是兄弟关系。说出它们在 `parse_data_info` 阶段的两个关键差异。

答案：(1) 检测版要处理 `instances` / `ann_info`（把实例标注包装成 3D 框对象并可能做坐标系转换），分割版完全不做实例处理，只拼接点云与逐点 mask 的路径；(2) 分割版额外注入 `seg_label_mapping` 查表数组（供 `PointSegClassMapping` 使用），并在测试模式准备空的 `eval_ann_info`。

**练习 2**：为什么 `ignore_index` 默认取 `len(classes)`（19 类时即 19）？

答案：这样无效标签恰好落在有效标签区间 [0, 18] 之外且紧邻其右，`CrossEntropyLoss(ignore_index=19)` 与 Lovasz 损失可以直接跳过这些点；同时 `get_seg_label_mapping` 里「全填 `len(seg_valid_class_ids)`」的初值与它天然对齐。

**练习 3**：`prepare_data` 里 `data_info['dataset'] = self` 这一行为什么只在训练态注入？

答案：它是给 LaserMix / PolarMix 等混合增强准备的——这些增强需要从数据集中再取其他场景来拼贴，只有训练需要；测试/评估管线逐帧推理，注入 dataset 反而会让 info 带着整个数据集引用到处流动（也无法被序列化调试）。

### 4.2 segmentors 的组织：Base3DSegmentor 与 EncoderDecoder3D

#### 4.2.1 概念说明

检测模型在 `mmdet3d/models/detectors/` 下自成体系（[u4-l2](./u4-l2-detector-class-hierarchy.md) 的 `Base3DDetector` 分层），分割模型则平行的放在 `mmdet3d/models/segmentors/` 下，一共五个注册类：`Base3DSegmentor`、`EncoderDecoder3D`、`Cylinder3D`、`MinkUNet`、`Seg3DTTAModel`（见 [mmdet3d/models/segmentors/__init__.py:L1-L11](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/segmentors/__init__.py#L1-L11)）。

`EncoderDecoder3D` 是「编码器-解码器」模板：backbone 提特征、decode_head 出逐点 logits。它同时兼容两类实现——

- **逐点 MLP 路线**（PointNet++ 风格，如室内分割）：输入是 `(B, N, 3+C)` 的稠密点张量，基类的 `loss`/`predict` 可以直接用；
- **体素化路线**（Cylinder3D / MinkUNet）：输入是 `batch_inputs_dict['voxels']`（体素字典），必须重写 `extract_feat` / `loss` / `predict`。

这个「模板 + 按需重写」的结构是本模块要建立的认知。

#### 4.2.2 核心流程

训练与推理的两条主线：

```text
loss:      extract_feat(voxels) ──► decode_head.loss ──► loss_by_feat
                                      （读 gt_pts_seg.voxel_semantic_mask）

predict:   extract_feat(voxels) ──► decode_head.predict
                                      （借 point2voxel_map 把体素 logits 查回每个点）
           ──► postprocess_result
                  seg_pred = seg_logits.argmax(dim=0)
                  写入 pred_pts_seg.pts_semantic_mask / pts_seg_logits
```

注意一个与检测不同的推理约束：**分割逐场景推理、batch 恒为 1**（测试 dataloader 的 `batch_size=1`）。因为每个场景点数不同，逐点预测没法像图像那样 pad 后堆叠。

#### 4.2.3 源码精读

- [mmdet3d/models/segmentors/base.py:L66-L109](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/segmentors/base.py#L66-L109)：`Base3DSegmentor.forward` 的三模式分发（loss / predict / tensor），与检测器 `Base3DDetector` 的约定完全一致——MMEngine 的 `train_step`/`test_step` 就是按这三个模式名调用的。
- [mmdet3d/models/segmentors/base.py:L135-L165](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/segmentors/base.py#L135-L165)：`postprocess_result`——对每帧的 `(num_classes, N)` logits 做 `argmax(dim=0)` 得逐点类别，然后把 logits 与预测分别以 `pts_seg_logits`、`pred_pts_seg`（均为 `PointData`）写回 data_sample。这就是「分割版 `add_pred_to_datasample`」。
- [mmdet3d/models/segmentors/encoder_decoder.py:L80-L103](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/segmentors/encoder_decoder.py#L80-L103)：`EncoderDecoder3D.__init__` 组装 backbone / neck（可选）/ decode_head / auxiliary_head / loss_regularization，并断言必须有 decode_head。文件头注释（L29-L53）给出了完整的调用链图。
- [mmdet3d/models/segmentors/encoder_decoder.py:L226-L262](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/segmentors/encoder_decoder.py#L226-L262)：基类 `loss` 在 L245-246 直接 `torch.stack(batch_inputs_dict['points'])`——这句话只对「等点数堆叠」的逐点 MLP 模型成立，体素模型必须绕开它（下一模块的 Cylinder3D / MinkUNet 都重写了 `loss`）。
- [mmdet3d/models/segmentors/encoder_decoder.py:L481-L523](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/segmentors/encoder_decoder.py#L481-L523)：基类 `predict`：逐场景调 `inference`（slide / whole 两模式，见 [L456-L479](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/segmentors/encoder_decoder.py#L456-L479)），`slide` 模式按滑窗切块推理后用 `scatter_add_` 把重叠 patch 的 logits 求平均（[L403-L447](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/segmentors/encoder_decoder.py#L403-L447)），这是室内大场景的常用做法；Cylinder3D/MinkUNet 走 `whole` 模式并重写了 predict。
- [mmdet3d/models/decode_heads/decode_head.py:L118-L158](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/decode_heads/decode_head.py#L118-L158)：`Base3DDecodeHead.loss` 的两步模板：`forward` 出 logits → `loss_by_feat` 算损失；默认 `_stack_batch_gt` 读的是 `gt_pts_seg.pts_semantic_mask`（点级），体素头会重写为读 `voxel_semantic_mask`。

#### 4.2.4 代码实践

**实践目标**：源码阅读型实践——画出「logits → Det3DDataSample」的最后一步，并用伪输入手工验证 `postprocess_result` 的形状语义。

1. 操作步骤（示例代码）：

   ```python
   import torch
   from mmdet3d.structures import Det3DDataSample, PointData

   # 伪造一帧 5 个点、3 个类别的 logits，形状 (num_classes, N)
   seg_logits = torch.tensor([[0.1, 0.9, 0.2, 0.8, 0.3],
                              [0.5, 0.1, 0.4, 0.2, 0.6],
                              [0.4, 0.0, 0.4, 0.0, 0.1]])
   sample = Det3DDataSample()
   sample.set_data({'gt_pts_seg': PointData(pts_semantic_mask=torch.zeros(5, dtype=torch.long))})

   from mmdet3d.models.segmentors.base import Base3DSegmentor
   # postprocess_result 是普通实例方法但函数体未用到 self，
   # 因此可传 None 以未绑定方式调用（Base3DSegmentor 是抽象类无法实例化）
   results = Base3DSegmentor.postprocess_result(None, [seg_logits], [sample])

   pred = results[0].pred_pts_seg.pts_semantic_mask
   print('逐点类别:', pred.tolist())
   print('logits 形状:', results[0].pts_seg_logits.pts_seg_logits.shape)
   ```

2. 需要观察的现象：`argmax(dim=0)` 沿类别维取最大，逐点类别应与每列最大值的位置一致。
3. 预期结果：逐点类别为 `[1, 0, 1, 0, 1]`（逐列比较三行 logits，并列时取先出现者）；logits 形状 `torch.Size([3, 5])`。
4. 说明：需要安装好 mmdet3d 依赖后才可运行（`import mmdet3d` 成功）；如暂无环境，可对照源码 [base.py:L156-L164](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/segmentors/base.py#L156-L164) 手工推演，结论相同。

#### 4.2.5 小练习与答案

**练习 1**：为什么 Cylinder3D 和 MinkUNet 都要重写 `EncoderDecoder3D` 的 `loss` / `predict`，而不是直接复用？

答案：基类 `loss` 假设输入是可 `torch.stack` 的等长点张量（逐点 MLP 路线），而体素路线的输入是 `batch_inputs_dict['voxels']` 字典且体素数各帧不同；基类 `predict` 走 `inference`→`encode_decode` 链路且不处理体素→点的回查。因此两个体素分割器都重写这三方法，绕过滑窗与堆叠逻辑。

**练习 2**：`postprocess_result` 里写入的 `pred_pts_seg` 与 `pts_seg_logits` 分别给谁消费？

答案：`pred_pts_seg.pts_semantic_mask`（整数类别）供 `SegMetric` 计算 mIoU、供可视化上色；`pts_seg_logits`（softmax 前的浮点 logits）保留原始置信度信息，供 TTA（`Seg3DTTAModel` 对多增强视图的 logits 取平均）等需要软输出的场合。

**练习 3**：分割测试为什么 `batch_size=1`？

答案：逐点预测要求输出长度与输入点数一致，不同场景点数不同，无法像图像那样 pad 到同一尺寸再堆叠；`Cylinder3D.predict` 的 docstring 明确写了「only support testing one scene every time」。

### 4.3 Cylinder3D 全链路：柱体分区与不对称稀疏卷积

#### 4.3.1 概念说明

Cylinder3D（论文 *Cylindrical and Asymmetrical 3D Convolution Networks for LiDAR Segmentation*）的两个名字来源就是它的两个设计：

**柱体分区（cylindrical partition）**。把点云从笛卡尔坐标 \((x,y,z)\) 转到柱坐标 \((\rho,\varphi,z)\) 再栅格化：

\[
\rho = \sqrt{x^2 + y^2}, \qquad \varphi = \arctan2(y, x)
\]

为什么？LiDAR 的点密度随距离平方衰减：笛卡尔网格下，近处格子挤爆、远处格子常年空置，格间点数极度失衡。柱形网格的每个格子是「固定角度 × 固定径向长度」的楔形，角分辨率与传感器扫描线天然对齐，米制尺寸随 \(\rho\) 增大而增大，恰好补偿密度衰减，使各格子点数更均衡。配置里 `grid_shape = [480, 360, 32]` 即径向 480 格 × 角向 360 格 × 高度 32 格。

**不对称卷积（asymmetrical convolution）**。普通 3D 卷积用 \((3,3,3)\) 核，对 LiDAR 点云有两个不合适：一是计算量大，二是点云的三个轴统计特性不同（BEV 平面内各向同性、高度轴分布很薄）。Cylinder3D 把核拆成 \((1,3,3)\) 与 \((3,1,3)\) 两支互补使用，再辅以高度方向错开的下采样策略。

#### 4.3.2 核心流程

一帧点云在 Cylinder3D 中的完整旅程：

```text
(N, 4) 点云 (x, y, z, intensity)
  │  Det3DDataPreprocessor.voxelize (voxel_type='cylindrical')
  │    1) 转柱坐标 (rho, phi, z)，clamp 到 [0,50]×[-π,π]×[-4,2]
  │    2) floor((p - min) / voxel_size) 得每点柱格坐标 coors (N, 3)
  │    3) get_voxel_seg: 训练时 one-hot 标签按格 mean 聚合 + argmax
  │       → voxel_semantic_mask（体素标签）、point2voxel_map（点→体素下标）
  │    4) voxels 特征 = (rho, phi, z, x, y, intensity) 拼成 6 维
  ▼
SegVFE（voxel_encoder）
  │    逐点 Linear+BN+ReLU（64→128→256→256），点到中心偏移 3 维拼接
  │    DynamicScatter max 把点特征聚合为体素特征，压缩 256→16
  ▼
Asymm3DSpconv（backbone）
  │    SparseConvTensor(features, coors, grid_size, batch)
  │    down_context → 4×Down（stride=2 / 2 / (2,2,1) / (2,2,1)）
  │    4×Up（SparseInverseConv3d 逆卷积 + skip 相加）→ DDCM
  ▼
Cylinder3DHead（decode_head）
  │    SubMConv3d(128→20)：逐体素分类 logits
  │    训练: CE + Lovasz（体素标签）
  │    推理: coors 拆 batch → point2voxel_map 查回每个点
  ▼
postprocess_result → pred_pts_seg.pts_semantic_mask (N,)
```

体素尺寸由 grid_shape 反推（注意分母是 `grid_shape - 1`，端点包含）：

\[
\text{voxel\_size} = \frac{\text{range}_{\max} - \text{range}_{\min}}{\text{grid\_shape} - 1}
\]

#### 4.3.3 源码精读

**入口配置**。模型基座把四个部件拼在一起：

- [configs/_base_/models/cylinder3d.py:L1-L21](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/cylinder3d.py#L1-L21)：`grid_shape=[480,360,32]`、柱坐标范围 `[0, -π, -4, 50, π, 2]`、`voxel_type='cylindrical'`；`SegVFE` 的 `in_channels=6`（后面解释为什么是 6）、`feat_compression=16`；backbone `input_channels=16` 正好接上压缩后的通道数。
- [configs/_base_/models/cylinder3d.py:L28-L40](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/cylinder3d.py#L28-L40)：decode head 20 类（19 有效 + 1 忽略），损失 CE + Lovasz，`test_cfg=dict(mode='whole')` 全场景单次前向。

**分割器薄壳**。`Cylinder3D` 相对 `EncoderDecoder3D` 只加了 `voxel_encoder` 槽位并重写三个方法：

- [mmdet3d/models/segmentors/cylinder3d.py:L57-L68](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/segmentors/cylinder3d.py#L57-L68)：先走基类构造（backbone/decode_head 等），再额外 `MODELS.build(voxel_encoder)` 构建 `SegVFE`。
- [mmdet3d/models/segmentors/cylinder3d.py:L70-L79](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/segmentors/cylinder3d.py#L70-L79)：`extract_feat` 三步：`voxel_encoder(voxels, coors)` 出体素特征 → 把返回的 `voxel_coors` 写回 `batch_inputs['voxels']`（decode head 推理时要用）→ `backbone(feats, coors, batch_size)`。注意 `len(batch_inputs['points'])` 就被用作 batch_size——每个样本的原始点列表还在。
- [mmdet3d/models/segmentors/cylinder3d.py:L81-L105](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/segmentors/cylinder3d.py#L81-L105)：`loss`：提特征 → `_decode_head_forward_train`（内部调 `decode_head.loss`，损失名加 `decode.` 前缀），没有辅助头。
- [mmdet3d/models/segmentors/cylinder3d.py:L107-L144](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/segmentors/cylinder3d.py#L107-L144)：`predict`：注意它把 `batch_inputs_dict` 一并传给 `decode_head.predict`（head 需要里面的 `voxels['voxel_coors']`），随后把 head 输出 `(N, C)` 转置为 `(C, N)` 再进 `postprocess_result`——这个转置就是与基类约定的 `(num_classes, N)` 对齐。

**柱体体素化**。全部发生在 data_preprocessor 的 `cylindrical` 分支：

- [mmdet3d/models/data_preprocessors/data_preprocessor.py:L394-L426](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/data_preprocessors/data_preprocessor.py#L394-L426)：逐帧处理：L397-399 三行完成笛卡尔→柱坐标转换（`rho=sqrt(x²+y²)`、`phi=atan2(y,x)`、堆叠 z）；L404-415 把柱坐标 clamp 到范围（兼容 PyTorch 版本的两种写法）；L416-418 `floor((p - min)/voxel_size)` 量化出整数格坐标；L421-422 拼接特征：`cat((polar_res, res[:, :2], res[:, 3:]))` = `(rho, phi, z) + (x, y) + (intensity)` 共 **6 维**——这就是配置里 `in_channels=6` 的来历（保留原始 x、y 是为了让网络仍能感知绝对位置）。
- [mmdet3d/models/data_preprocessors/data_preprocessor.py:L470-L492](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/data_preprocessors/data_preprocessor.py#L470-L492)：`get_voxel_seg`——训练分支把每点标签 one-hot 后按格 `dynamic_scatter_3d(..., 'mean')` 聚合再 `argmax`，即**体素标签 = 格内标签的多数投票**；同时产出的 `point2voxel_map` 无论训练测试都会写入 `data_sample.point2voxel_map`。测试分支用全 1 的伪张量走同样的 scatter，只为拿 `point2voxel_map`。

**SegVFE：点级 MLP + max 聚合**。

- [mmdet3d/models/voxel_encoders/voxel_encoder.py:L598-L640](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/voxel_encoders/voxel_encoder.py#L598-L640)：`forward` 里 L614-622 计算每个点到所在柱格中心的偏移（`f_center`，注意用的是 `coors` 三列格坐标 × `vx/vy/vz`——此时 vx/vy/vz 是柱坐标三个方向的步长），拼到特征上（6+3=9 维）；L629-633 逐点过 `vfe_layers`（Linear+BN+ReLU 堆叠，最后一层纯 Linear），然后 **一次** `vfe_scatter`（`DynamicScatter`，默认 max）把点特征聚合成体素特征；L635-636 可选压缩层把 256 维压到 16 维。与 PointPillars 的 `PillarFeatureNet`（[u5-l1](./u5-l1-voxelnet-pointpillars.md)）相比，这里是「多层 MLP 后一次 max」而非「每层 PFN 后 max」。

**不对称骨干 Asymm3DSpconv**。

- [mmdet3d/models/backbones/cylinder3d.py:L43-L59](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/backbones/cylinder3d.py#L43-L59)：`AsymmResBlock` 内两支卷积的核——`conv0_0` 是 `(1,3,3)`、`conv1_0` 是 `(3,1,3)`：一支不含高度核、一支含高度核，shortcut 支与 residual 支互补，最后相加（[L83-L104](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/backbones/cylinder3d.py#L83-L104) 的 forward）。由于两支的第二层互为转置形状（`(3,1,3)` 接 `(1,3,3)`），整个块仍能覆盖完整 3D 邻域。
- [mmdet3d/models/backbones/cylinder3d.py:L175-L193](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/backbones/cylinder3d.py#L175-L193)：`AsymmeDownBlock` 的池化层——`height_pooling` 决定 stride：`height_pooling=True` 用 `stride=2`（三轴同缩，高度减半），否则用 `stride=(2,2,1)`（只缩 BEV、高度保持）。默认 `height_pooing=[True, True, False, False]`（[mmdet3d/models/backbones/cylinder3d.py:L416](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/backbones/cylinder3d.py#L416)，注意源码参数名就是这个拼写）：**前两级缩高度（32→16→8），后两级高度保持 8 格**——LiDAR 点云高度分布薄、早期快速降低高度分辨率省算力，深层保留高度线索供细粒度区分。
- [mmdet3d/models/backbones/cylinder3d.py:L286-L317](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/backbones/cylinder3d.py#L286-L317)：`AsymmeUpBlock` 用 `SparseInverseConv3d`（`indice_key=up_key` 指回对应 down 块记录的索引表）做逆卷积上采样，上采后与 down 阶段保存的 skip 特征相加，再过三个不对称卷积精修——这是稀疏版 U-Net 解码器。
- [mmdet3d/models/backbones/cylinder3d.py:L320-L389](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/backbones/cylinder3d.py#L320-L389)：`DDCMBlock`（Dimension-Decomposition based Context Modeling）：三个方向的分解核 `(3,1,1)` / `(1,3,1)` / `(1,1,3)` 各出一路，Sigmoid 门控后相加，再与原特征逐元素相乘（注意力式上下文建模），输出与输入通道一致。
- [mmdet3d/models/backbones/cylinder3d.py:L460-L481](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/backbones/cylinder3d.py#L460-L481)：`Asymm3DSpconv.forward`：把 `(features, coors, grid_size, batch_size)` 包成 `SparseConvTensor`（稀疏卷积的标准容器）→ down_context → 4 次 down（每次返回 `(pooled, skip)`，skip 进列表）→ 倒序 4 次 up 吃 skip → DDCM → 与 up 特征通道维 concat，输出仍是 `SparseConvTensor`。

**decode head**。

- [mmdet3d/models/decode_heads/cylinder3d_head.py:L78-L92](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/decode_heads/cylinder3d_head.py#L78-L92)：`build_conv_seg` 重写基类的 1D 卷积为 **`SubMConv3d(channels, num_classes, kernel_size=3)`**——分类器本身就是一个稀疏 3×3×3 卷积，在每个活跃体素位置输出 20 维 logits（「逐点分类分支」的体素版）。
- [mmdet3d/models/decode_heads/cylinder3d_head.py:L94-L122](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/decode_heads/cylinder3d_head.py#L94-L122)：`loss_by_feat` 把各帧的 `gt_pts_seg.voxel_semantic_mask` 拼接成体素标签序列，与 `seg_logit.features`（`(M, 20)`，M 为全部体素数）对齐后算 CE + Lovasz 双损失，`ignore_index=19`。Lovasz 直接优化 IoU 型目标，与 mIoU 指标同向。
- [mmdet3d/models/decode_heads/cylinder3d_head.py:L124-L158](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/decode_heads/cylinder3d_head.py#L124-L158)：`predict` 的回查两步：先按 `coors[:, 0] == batch_idx` 把拼接的体素 logits 按帧切开，再用该帧的 `point2voxel_map` 做一次花式索引 `seg_logits_sample[point2voxel_map]`，把 `(M_i, 20)` 的体素 logits 还原成 `(N_i, 20)` 的**逐点 logits**——同一体素内所有点共享该体素的预测。

#### 4.3.4 代码实践

**实践目标**：运行仓库自带的点云分割 demo，对一帧 KITTI 点云得到逐点语义（本讲的主实践，综合实践一节会继续用它）。

1. 操作步骤：

   ```bash
   cd <仓库根目录>
   # 1) 下载 Cylinder3D SemanticKITTI 预训练权重（链接来自 configs/cylinder3d/README.md）
   wget https://download.openmmlab.com/mmdetection3d/v1.1.0_models/cylinder3d/cylinder3d_4xb4_3x_semantickitti/cylinder3d_4xb4_3x_semantickitti_20230318_191107-822a8c31.pth

   # 2) 对 demo 自带的 KITTI 000008 帧做逐点分割
   python demo/pcd_seg_demo.py \
       demo/data/kitti/000008.bin \
       configs/cylinder3d/cylinder3d_4xb4-3x_semantickitti.py \
       cylinder3d_4xb4_3x_semantickitti_20230318_191107-822a8c31.pth \
       --out-dir seg_demo_out
   ```

2. 依据：[demo/pcd_seg_demo.py:L70-L75](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/demo/pcd_seg_demo.py#L70-L75) 的 main 只是 `LidarSeg3DInferencer(**init_args)` 加一次调用；权重链接来自 [configs/cylinder3d/README.md:L23](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/cylinder3d/README.md#L23)。注意：README 该行的配置超链接文本写的是 `cylinder3d_8xb2-laser-polar-mix-3x_semantickitti.py`，但 model URL 是 `4xb4` 权重——本实践用 4xb4 配置配 4xb4 权重保持自洽；若想跑 LaserMix/PolarMix 增强版，请改用 `cylinder3d_8xb2-laser-polar-mix-3x_semantickitti.py` 搭配 README 第 24 行的 AMP 权重（两个配置文件都在 `configs/cylinder3d/` 下）。
3. 需要观察的现象：`seg_demo_out` 目录下生成可视化图片与预测文件；可视化中同一物体的点被染成同一颜色（颜色查表来自 `SemanticKittiDataset.METAINFO['palette']`）。
4. 预期结果：得到一张按语义着色的点云渲染图与逐点预测结果文件；预测标签取值范围 0~18（19 为忽略类，正常不会出现在预测中）。**具体输出文件名与格式待本地验证**（取决于 `Base3DInferencer` 版本行为）。
5. 原理提示：[mmdet3d/apis/inferencers/lidar_seg3d_inferencer.py:L99-L124](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inferencers/lidar_seg3d_inferencer.py#L99-L124)——分割 Inferencer 在初始化时对测试管线做「外科手术」：删掉 `LoadAnnotations3D` 与 `PointSegClassMapping`（推理没有真值），把 `LoadPointsFromFile` 替换成可吃裸路径的 `LidarDet3DInferencerLoader`，其余 transform 原样保留。

#### 4.3.5 小练习与答案

**练习 1**：配置里 `in_channels=6`，输入点云是 4 维 `(x, y, z, intensity)`，多出来的 2 维从哪来？

答案：`voxelize` 的 cylindrical 分支把特征拼成 `(rho, phi, z, x, y, intensity)`：柱坐标 3 维 + 原始 `x, y` 2 维 + 强度 1 维，共 6 维（z 在柱坐标里已出现，不再重复取）。`SegVFE` 再因 `with_voxel_center=True` 追加 3 维中心偏移，进入第一层 Linear 的实际是 9 维。

**练习 2**：`point2voxel_map` 与 `voxel_semantic_mask` 分别是什么形状、在什么阶段产生？

答案：都在 `Det3DDataPreprocessor` 体素化时产生（`get_voxel_seg`）。`point2voxel_map` 长度等于该帧点数 N（每个点指向其体素在该帧体素列表中的下标）；`voxel_semantic_mask` 长度等于体素数 M（格内点标签 one-hot 均值后 argmax 的多数投票），训练时才有语义内容。推理时只生成 `point2voxel_map` 用于把体素预测查回每个点。

**练习 3**：如果把 `height_pooing` 改成 `[False, False, False, False]`，骨干的高度分辨率会怎样变化？对照默认值说明两者的取舍。

答案：改成全 False 后，高度方向全程保持 32 格不缩减（四个 Down 块全部 `stride=(2,2,1)`，只缩 BEV 两轴）；默认 `[True, True, False, False]` 则在前两级把高度 32→16→8、后两级保持 8 格。取舍在于：柱坐标下高度本就只有 32 格且 LiDAR 点的 z 分布很薄，早期降低高度分辨率可以显著省算力；而深层（物体级语义区分）保留 8 格高度，地面、低矮物体与高处植被等仍可分。想直观感受差异，可对比两种设置下 `get_flops.py` 的输出（见 [u7-l5](./u7-l5-analysis-tools.md)）。

### 4.4 MinkUNet 与 SPVCNN：立方体量化与多后端稀疏卷积

#### 4.4.1 概念说明

MinkUNet 出自 *4D Spatio-Temporal ConvNets*（Minkowski Engine 论文），是语义分割的另一个经典基线。它与 Cylinder3D 的根本差异在**体素化方式和体素特征来源**：

| 维度 | Cylinder3D | MinkUNet |
| --- | --- | --- |
| 体素形状 | 柱坐标楔形格（`voxel_type='cylindrical'`） | 5cm 立方体（`voxel_type='minkunet'`） |
| 量化公式 | 柱坐标 `floor((p-min)/voxel)` | 笛卡尔 `round(p/voxel)` 后减 min |
| 体素特征 | 格内全部点过 MLP 后 max 聚合 | 每格取**一个代表点**，特征即该点特征 |
| 体素标签 | 格内标签多数投票 | 代表点的标签 |
| batch 维位置 | coors 第 0 列（`batch_first=True`） | coors 最后一列（`batch_first=False`） |
| 稀疏卷积库 | mmcv/spconv（`SubMConv3d` 等） | torchsparse / spconv2 / Minkowski Engine 三选一 |
| 分类器 | `SubMConv3d(128→20)` | `nn.Linear(96→19)` |

另一个工程亮点是**后端可切换**：`MinkUNetBackbone` 在构造时根据 `sparseconv_backend` 参数选择 torchsparse、spconv（1.x/2.x）或 Minkowski Engine 的算子集合，同一套 UNet 结构可以在三种稀疏库之间迁移——因为 MinkUNet 的 encoder/decoder 只用到「下采样卷积 / 上采样转置卷积 / 残差块」三种原语，每个库都有对应实现。

SPVCNN（*Searching Efficient 3D Architectures with Sparse Point-Voxel Convolution*）则是在 MinkUNet 骨架里插入**体素分支与点分支的双向转换**：编码早期点特征更细、深层体素更省，两支信息来回穿插。

#### 4.4.2 核心流程

```text
(N, 4) 点云 (x, y, z, intensity)
  │  Det3DDataPreprocessor.voxelize (voxel_type='minkunet')
  │    coors = round(xyz / 0.05) - min        # 每点一个整数格坐标
  │    sparse_quantize(coors)                 # ravel 哈希去重
  │      ├─ inds            (M,)   代表点下标
  │      └─ point2voxel_map (N,)   点→体素
  │    voxels = points[inds]                  # (M, 4) 每体素一个代表点
  │    gt: voxel_semantic_mask = pts_semantic_mask[inds]
  ▼
MinkUNetBackbone（稀疏 UNet）
  │    conv_input (SubM 3×3×3 ×2)
  │    encoder: 4 级 SparseConv(k=2, s=2) 下采样 + 残差块
  │    decoder: 4 级 转置卷积上采样 + skip 拼接/相加 + 残差块
  ▼
MinkUNetHead
  │    nn.Linear(96 → 19)：每个体素直接线性分类
  │    推理: batch_idx 拆帧 → point2voxel_map 查回每个点
  ▼
postprocess_result → pred_pts_seg
```

#### 4.4.3 源码精读

**minkunet 体素化**：

- [mmdet3d/models/data_preprocessors/data_preprocessor.py:L427-L460](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/data_preprocessors/data_preprocessor.py#L427-L460)：逐帧：L431 `torch.round(res[:, :3] / voxel_size)` 量化（注意是 round 不是 floor，正负对称更贴近 Minkowski Engine 的惯例）再减每维最小值平移到非负；L435-436 `sparse_quantize` 返回代表点下标 `inds` 与 `point2voxel_map`；L443-445 若有真值则 `voxel_semantic_mask = pts_semantic_mask[inds]`（**代表点的标签即体素标签**，无投票）；L448-456 因为 `batch_first=False`，batch 号 pad 在 coors **末列**，同时把每帧代表点的 batch 号写进 `data_sample.batch_idx`，并把 `point2voxel_map` 写入 data_sample。
- [mmdet3d/models/data_preprocessors/data_preprocessor.py:L494-L528](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/data_preprocessors/data_preprocessor.py#L494-L528)：`ravel_hash` 把三维整数坐标压成一维哈希（逐维乘累积极大值），供 `np.unique` 去重——这就是 `sparse_quantize` 的实现思路（文件继续往下是 `sparse_quantize` 本体，返回 `unique` 的 `index`/`inverse`）。

**MinkUNet 分割器与基座配置**：

- [mmdet3d/models/segmentors/minkunet.py:L24-L42](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/segmentors/minkunet.py#L24-L42)：`loss` 与 Cylinder3D 同构但更薄——直接 `decode_head.loss(x, data_samples, self.train_cfg)`；[L95-L112](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/segmentors/minkunet.py#L95-L112)：`extract_feat` 只有两行——`backbone(voxels, coors)`（可选 neck）。注意它**没有** `voxel_encoder` 槽位：MinkUNet 的「体素编码」就是取代表点本身，无需网络。
- [configs/_base_/models/minkunet.py:L1-L13](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/minkunet.py#L1-L13)：`voxel_type='minkunet'`、`batch_first=False`、`voxel_size=[0.05,0.05,0.05]`、`max_voxels=80000`（训练时随机丢弃多余体素）。
- [configs/_base_/models/minkunet.py:L14-L31](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/minkunet.py#L14-L31)：backbone `encoder_channels=[32,64,128,256]` / `decoder_channels=[256,128,96,96]` 的对称 UNet；head 是 96→19 的 Linear，`avg_non_ignore=True` 的 CE（按非忽略点数平均）。

**MinkUNetBackbone 的三后端**：

- [mmdet3d/models/backbones/minkunet_backbone.py:L62-L77](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/backbones/minkunet_backbone.py#L62-L77)：构造参数与后端白名单断言（torchsparse / spconv / minkowski）。
- [mmdet3d/models/backbones/minkunet_backbone.py:L80-L116](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/backbones/minkunet_backbone.py#L80-L116)：后端选择分支——每个分支把 `input_conv` / `encoder_conv` / `decoder_conv` / `residual_block` 四个「原语工厂」绑到对应库的实现（如 spconv 分支用 `make_sparse_convmodule` 的 partial，minkowski 分支的 decoder 用 `MinkowskiConvNdTranspose`）。后面的网络组装代码对这些工厂一视同仁，从而实现一套结构、三种后端。
- [mmdet3d/models/backbones/minkunet_backbone.py:L118-L144](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/backbones/minkunet_backbone.py#L118-L144)：输入两个 SubMConv3d；encoder 每级以 `kernel_size=2, stride=2` 的稀疏卷积下采样（正好对齐格子）。

**MinkUNetHead 的逐点分类分支**：

- [mmdet3d/models/decode_heads/minkunet_head.py:L27-L30](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/decode_heads/minkunet_head.py#L27-L30)：`build_conv_seg` 返回 `nn.Linear(channels, num_classes)`——与 Cylinder3DHead 的 3×3×3 稀疏卷积分类器相比，这里**没有邻域交互**，纯逐体素线性映射（邻域建模已全部由 backbone 完成）。
- [mmdet3d/models/decode_heads/minkunet_head.py:L32-L38](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/decode_heads/minkunet_head.py#L32-L38)：`_stack_batch_gt` 读的是 `voxel_semantic_mask`（重写基类的点级版本）。
- [mmdet3d/models/decode_heads/minkunet_head.py:L40-L62](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/decode_heads/minkunet_head.py#L40-L62)：`predict`：`batch_idx` 拆帧 → `point2voxel_map` 查回逐点 logits，与 Cylinder3DHead 的回查思路一致，只是拆帧依据从 `voxels['voxel_coors']` 换成了预写的 `data_sample.batch_idx`。

**SPVCNN 变体**：

- [mmdet3d/models/backbones/spvcnn_backone.py:L20-L76](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/backbones/spvcnn_backone.py#L20-L76)：`SPVCNNBackbone` 继承 `MinkUNetBackbone`（仅允许 torchsparse），额外加了三个 `point_transforms`（Linear+BN+ReLU）与 dropout。
- [mmdet3d/models/backbones/spvcnn_backone.py:L78-L119](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/backbones/spvcnn_backone.py#L78-L119)：forward 里 `initial_voxelize` / `voxel_to_point` / `point_to_voxel` 在体素态与点态之间往返切换（编码器末端、解码器中部、末端各一次），`PointTensor` 携带逐点特征一路跟到输出——骨干直接输出**逐点特征**而非体素特征，head 因此可完全按点处理。
- [mmdet3d/models/backbones/spvcnn_backone.py:L122-L138](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/backbones/spvcnn_backone.py#L122-L138)：`MinkUNetBackboneV2` 进一步限定 torchsparse，是 SPVCNN 在本仓库的注册名。

**分割专用增强**。MinkUNet 的 SemanticKITTI 配置用了 LaserMix / PolarMix 两种混合增强（这正是 4.1 中 `dataset` 注入的消费者）：

- [configs/minkunet/minkunet34_w32_torchsparse_8xb2-laser-polar-mix-3x_semantickitti.py:L21-L72](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/minkunet/minkunet34_w32_torchsparse_8xb2-laser-polar-mix-3x_semantickitti.py#L21-L72)：`RandomChoice` 在 LaserMix（[transforms_3d.py:L2541](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L2541) 起）与 PolarMix（[transforms_3d.py:L2373](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L2373) 起）之间各 50% 抽选；LaserMix 按俯仰角把多帧点云「切片拼贴」，PolarMix 按类别做物体级旋转粘贴。

#### 4.4.4 代码实践

**实践目标**：源码阅读型实践——写 20 行脚本手工复现 `minkunet` 体素化的「量化 + 去重 + 代表点」三步，弄清 `voxels`、`coors`、`point2voxel_map` 的形状关系。

1. 操作步骤（示例代码，非项目原有代码，仅需 numpy）：

   ```python
   import numpy as np

   rng = np.random.RandomState(0)
   points = rng.rand(1000, 4).astype(np.float32)   # 模拟 (x,y,z,intensity)
   points[:, :3] *= 0.5                            # 让落点重复，模拟同格多点

   voxel_size = 0.05
   coors = np.round(points[:, :3] / voxel_size).astype(np.int32)
   coors -= coors.min(0)                           # 平移到非负，与源码 L431-432 一致

   # 复现 sparse_quantize：先 ravel 哈希再 unique
   xmax = coors.max(0) + 1
   h = coors[:, 0] * (xmax[1] * xmax[2]) + coors[:, 1] * xmax[2] + coors[:, 2]
   uniq, inds, point2voxel_map = np.unique(h, return_index=True, return_inverse=True)

   print('点数 N =', len(points))
   print('体素数 M =', len(uniq), '        # voxels 形状 = (M, 4)')
   print('point2voxel_map 形状 =', point2voxel_map.shape, '  # (N,)')
   print('重复率 = %.1f%%' % (100 * (1 - len(uniq) / len(points))))
   ```

2. 需要观察的现象：因为点了 0.5m 范围内撒 1000 个点、格边只有 0.05m，大量点落入同格，`M` 明显小于 `N`。
3. 预期结果：`M < N`（本例约几百），`point2voxel_map` 长度恒为 1000；把 `voxel_size` 改成 `0.01` 后 `M` 会显著增大（格子更细、去重更少）。**具体数值待本地验证**（与随机种子有关，现象方向是确定的）。
4. 对照源码：真实实现在 [data_preprocessor.py:L429-L436](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/data_preprocessors/data_preprocessor.py#L429-L436)（量化+去重）与 [L494-L514](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/data_preprocessors/data_preprocessor.py#L494-L514)（`ravel_hash`，注意其哈希按列累乘的顺序与上面伪码等价）。

#### 4.4.5 小练习与答案

**练习 1**：Cylinder3D 与 MinkUNet 的体素标签（`voxel_semantic_mask`）生成方式有何不同？

答案：Cylinder3D 把格内所有点的 one-hot 标签做 mean 聚合后 argmax（多数投票，见 `get_voxel_seg`）；MinkUNet 直接取代表点（`inds` 中的第一个落入该格的点）的标签，即 `pts_semantic_mask[inds]`，无任何聚合。

**练习 2**：为什么 MinkUNet 配置要设 `batch_first=False`，而 Cylinder3D 用默认 `True`？

答案：`batch_first` 决定 batch 号 pad 在 coors 的末列还是首列，本质是配合骨干所依赖的稀疏库的坐标约定：torchsparse 的 `SparseTensor` 约定坐标为 `(x, y, z, batch)`（batch 在最后），而 spconv 的 `SparseConvTensor` 约定 `(batch, z, y, x)`（batch 在最前）。MinkUNetBackbone 主用 torchsparse 所以 `batch_first=False`；Cylinder3D 的 `Asymm3DSpconv` 用 mmcv/spconv 所以默认 True。

**练习 3**：`MinkUNetHead` 的分类器是一个普通 `nn.Linear`，没有邻域卷积，为什么这样就够？

答案：MinkUNet 的 backbone 是完整的稀疏 UNet，decoder 末端每个体素的特征已经聚合了大感受野的邻域信息，head 只需做通道到类别的线性映射；而 Cylinder3DHead 用 `SubMConv3d` 做分类器，相当于在 head 里又补一次 3×3×3 的局部上下文（配合 DDCM 的设计），分工不同。

## 5. 综合实践

把本讲三个模块串起来的任务：**跑通 Cylinder3D 推理，并用一张对比表消化两条体素路线**。

**任务 A：跑通 demo（依赖 GPU 环境；无 GPU 可只做任务 B）**

1. 按 4.3.4 的步骤下载权重并运行 `demo/pcd_seg_demo.py`，对 `demo/data/kitti/000008.bin` 得到逐点语义预测。
2. 打开输出的预测文件（pkl/json），用 `np.bincount(pred, minlength=20)` 统计每个语义类别的点数，对照 [configs/_base_/datasets/semantickitti.py:L7-L11](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/semantickitti.py#L7-L11) 的类名表回答：这帧里最多的三类是什么？（KITTI 街景中通常是 vegetation、building、road 一类地形。**待本地验证**。）

**任务 B：填写两条路线的对比表（纯源码阅读，随时可做）**

对照 [configs/_base_/models/cylinder3d.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/cylinder3d.py) 与 [configs/_base_/models/minkunet.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/minkunet.py)，逐项填写并注明依据源码行号：

| 对比项 | Cylinder3D | MinkUNet |
| --- | --- | --- |
| `voxel_type` | ？ | ？ |
| 体素几何形状 / 分辨率 | ？ | ？ |
| 点→体素特征（聚合 or 代表点） | ？ | ？ |
| 体素标签生成 | ？ | ？ |
| decode head 分类器类型与形状 | ？ | ？ |
| head 损失函数 | CE + ? | ? |
| 依赖的稀疏卷积库 | ？ | ？（三选一） |

参考答案（自查）：`cylindrical` vs `minkunet`；柱形格 `[480,360,32]` vs 5cm 立方体；SegVFE max 聚合 vs `points[inds]` 代表点；one-hot mean+argmax 投票 vs 代表点标签直取；`SubMConv3d(128→20)` vs `Linear(96→19)`；CE+Lovasz vs `avg_non_ignore` CE；spconv（mmcv 封装） vs torchsparse/spconv/minkowski 三选一。

**任务 C（进阶，可选）：tiny SemanticKITTI 训练**

若已下载 SemanticKITTI，可仿照 [u7-l1](./u7-l1-training-loop.md) 的方法用 `--cfg-options` 把 `train_dataloader.dataset.ann_file` 指向一个只含几帧的自制 info pkl、`train_cfg.max_epochs=1`，跑一次最小训练，观察日志中的 `decode.loss_ce` 与 `decode.loss_lovasz` 两项损失是否都在下降。

## 6. 本讲小结

- `Seg3DDataset` 与 `Det3DDataset` 平级：没有实例标注与 `parse_ann_info` 链，取而代之的是 `seg_valid_class_ids`/`seg_all_class_ids` 两级标签映射（dict + numpy 查表数组）、`scene_idxs` 场景级重复采样，以及训练态把 `dataset` 对象注入 data_info 以支持 LaserMix/PolarMix 混合增强。
- 分割器组织是「模板 + 重写」：`Base3DSegmentor` 提供三模式入口与 `postprocess_result`（argmax + 写 `pred_pts_seg`/`pts_seg_logits`）；`EncoderDecoder3D` 是逐点 MLP 与体素两条路线的公共模板，体素路线（Cylinder3D/MinkUNet）重写 `extract_feat/loss/predict` 以处理 `voxels` 字典。
- Cylinder3D 的两大数据结构创新：**柱体分区**（先转柱坐标 \((\rho,\varphi,z)\) 再栅格化，缓解点密度随距离衰减导致的格子失衡）与**不对称卷积**（`(1,3,3)`/`(3,1,3)` 互补 + 高度方向延迟下采样 + DDCM 维度分解注意力）；其 head 用 `SubMConv3d` 做逐体素分类，CE + Lovasz 双损失。
- MinkUNet 走**立方体量化 + 每体素代表点**路线：`round(p/voxel)` 量化、哈希去重得 `inds` 与 `point2voxel_map`，体素特征/标签都来自代表点；骨干通过「原语工厂」在 torchsparse/spconv/Minkowski Engine 三后端间切换，head 只是一个 `nn.Linear`。
- 两条路线共有的关键机制是**点↔体素双向映射**：训练时把逐点标签聚合成体素标签算损失，推理时用 `point2voxel_map` 把体素 logits 查回每个点——这是所有「体素化分割器」的通用范式。

## 7. 下一步学习建议

本讲完成后，你已经见过了 mmdet3d 的第三类顶层任务（检测 → 分割）。建议按以下顺序继续：

1. **u8-l2（接入自定义数据集）**：本讲的 `Seg3DDataset` 标签映射体系是自定义分割数据集的模板，先想清楚你的数据 `seg_all_class_ids` 是什么再动手。
2. **u8-l3（自定义模型与 example_project）**：试着把 `MinkUNetHead` 的 `build_conv_seg` 换成带一次邻域卷积的版本，走一遍注册器 + custom_imports 流程。
3. **延伸阅读**：
   - [mmdet3d/evaluation/metrics/seg_metric.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/seg_metric.py)（`SegMetric`，L16 起）：看 mIoU 的 per-dataset 计算；
   - [mmdet3d/models/segmentors/seg3d_tta.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/segmentors/seg3d_tta.py)：分割版 TTA 如何对多视图 logits 取平均（呼应 [u7-l4](./u7-l4-inferencers-and-tta.md)）；
   - [mmdet3d/datasets/transforms/transforms_3d.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py) 中的 `LaserMix`（L2541）与 `PolarMix`（L2373）：体会「按俯仰角切片」与「按类别粘贴」两种混合增强如何同步变换点与逐点标签。

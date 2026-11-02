# 3D 语义分割：Cylinder3D 与 MinkUNet

## 1. 本讲目标

本讲是单元 8 的第一讲，从「检测」切换到 mmdet3d 的另一条主线——**3D 语义分割**：对点云中的**每一个点**预测一个类别标签。学完本讲你应该能够：

1. 说出 `Seg3DDataset` 与 `Det3DDataset` 在数据契约上的本质差异（逐点标签、双套标签映射、`scene_idxs` 采样）。
2. 画出分割器（segmentor）的类层次：`Base3DSegmentor → EncoderDecoder3D → Cylinder3D / MinkUNet`，并解释为什么后两者要重写 `loss/predict`。
3. 讲清 Cylinder3D 的两大设计：**柱体分区**（柱坐标体素化）与**不对称 3D 稀疏卷积**，并追踪 `voxels` 字典从预处理器到 decode head 的完整数据流。
4. 讲清 MinkUNet 的**多后端稀疏卷积**（torchsparse / spconv / MinkowskiEngine 可切换）与 `minkunet` 风实体素化的差异。
5. 对比两种模型的 voxel / 点表示差异，解释 decode head 中「体素级分类 + `point2voxel_map` 回投影」的逐点分类结构。

本讲默认你已掌握前置内容：数据集基类与管线（u3-l1、u3-l4）、`Det3DDataSample` 与 `PointData`（u2-l4）、检测器分层（u4-l2）、体素化预处理（u4-l3）。

## 2. 前置知识

**语义分割 vs 目标检测**。检测的监督信号是「每个目标一个 7 自由度框 + 类别」，分割的监督信号是「每个点一个整数类别标签」（`pts_semantic_mask`，长度恒等于点数）。这个差异会传导到全链路：数据集要多提供逐点掩码、损失从框回归变成逐点交叉熵、输出从 `pred_instances_3d` 变成 `pred_pts_seg`。

**体素化（voxelization）回顾（u4-l3）**。`Det3DDataPreprocessor.voxelize()` 把 `points` 列表变成 `voxels` 字典。检测线用过的 `hard` / `dynamic` 两种类型本讲不再重复，本讲聚焦分割专用的 `cylindrical` 与 `minkunet` 两种新类型。

**稀疏卷积回顾（u5-l2）**。稀疏卷积网络不存储稠密网格，只存「有值的格点」：特征矩阵 `(M, C)` + 坐标矩阵 `(M, 3~4)`。`SubMConv3d`（submanifold）不改变活跃格点集合，`SparseConv3d`（stride=2 时）会扩散/下采样，`SparseInverseConv3d` 借助正向卷积的索引表（indice table）做可逆上采样。`indice_key` 让同尺寸卷积共享 rule book 以省内存。

**mIoU**。语义分割的标准指标是各类别交并比的平均值：

\[
\text{mIoU} = \frac{1}{C}\sum_{c=1}^{C}\frac{TP_c}{TP_c + FP_c + FN_c}
\]

其中被标注为 `ignore_index` 的点不参与统计。

**逐点标签的两难**。体素网络在体素（格子）上做计算，但监督和评估都在点上。mmdet3d 的解法是：训练时把逐点标签**降采样**成体素级标签（`voxel_semantic_mask`），推理时把体素级 logits **回投影**到每个点（`point2voxel_map`）。这两个张量都由预处理器在体素化时现场生成，是本讲最重要的数据流线索。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `mmdet3d/datasets/seg3d_dataset.py` | 3D 分割数据集基类：双套标签映射、`scene_idxs`、向管线注入 dataset 自身 |
| `mmdet3d/datasets/semantickitti_dataset.py` | SemanticKITTI 数据集类，极薄，仅重写 `get_seg_label_mapping` |
| `configs/_base_/datasets/semantickitti.py` | SemanticKITTI 数据配置：`labels_map`、分割管线、`SegMetric` |
| `mmdet3d/models/segmentors/base.py` | `Base3DSegmentor`：forward 三模式 + `postprocess_result` |
| `mmdet3d/models/segmentors/encoder_decoder.py` | `EncoderDecoder3D`：backbone + decode_head 的组装与训练/推理骨架 |
| `mmdet3d/models/segmentors/cylinder3d.py` | Cylinder3D 分割器：串起 voxel_encoder → backbone → decode_head |
| `mmdet3d/models/segmentors/minkunet.py` | MinkUNet 分割器：薄壳，重写 loss/predict/extract_feat |
| `mmdet3d/models/data_preprocessors/data_preprocessor.py` | `voxelize()` 的 `cylindrical` / `minkunet` 分支与 `get_voxel_seg` |
| `mmdet3d/models/voxel_encoders/voxel_encoder.py` | `SegVFE`：分割专用体素特征编码器 |
| `mmdet3d/models/backbones/cylinder3d.py` | `Asymm3DSpconv`：不对称稀疏卷积 U-Net 骨干 |
| `mmdet3d/models/decode_heads/cylinder3d_head.py` | `Cylinder3DHead`：体素级 CE + Lovasz 损失、回投影预测 |
| `mmdet3d/models/decode_heads/decode_head.py` | `Base3DDecodeHead`：分割头基类 |
| `mmdet3d/models/backbones/minkunet_backbone.py` | `MinkUNetBackbone`：三后端可切换的稀疏 U-Net |
| `mmdet3d/models/decode_heads/minkunet_head.py` | `MinkUNetHead`：线性分类层 + `point2voxel_map` 回投影 |
| `mmdet3d/evaluation/metrics/seg_metric.py` | `SegMetric`：调 `seg_eval` 算 mIoU |
| `configs/_base_/models/cylinder3d.py` / `configs/_base_/models/minkunet.py` | 两个模型的基座配置 |
| `configs/cylinder3d/cylinder3d_4xb4-3x_semantickitti.py` | Cylinder3D 训练配置（36 epoch） |
| `demo/pcd_seg_demo.py` | 分割推理 demo 入口（`LidarSeg3DInferencer`） |

## 4. 核心概念与源码讲解

### 4.1 Seg3DDataset：3D 分割的数据契约

#### 4.1.1 概念说明

`Seg3DDataset` 是 SemanticKITTI、ScanNet seg、S3DIS 的共同基类。与 `Det3DDataset` 相比，它解决三个分割特有问题：

1. **逐点语义标签的映射**：原始数据集的标签 id 往往不连续（SemanticKITTI 官方 id 是 0/1/10/11/13…），训练前必须重映射成连续的 `0~C-1`，未参与的类统一映射到 `ignore_index`。
2. **子集训练**：metainfo 里传子集 `classes` 时，需要把「没选中的类」也打成 `ignore_index`。
3. **大场景重采样**：一帧室外点云可达十万点，一个室内场景也可以被采样多次训练，`scene_idxs` 允许同一帧在数据列表里出现多次。

#### 4.1.2 核心流程

`Seg3DDataset` 初始化时的关键加工：

```text
读 METAINFO（classes / palette / seg_valid_class_ids / seg_all_class_ids）
    │
    ├─ get_label_mapping(new_classes)     → label_mapping(dict) / label2cat / valid_class_ids
    ├─ get_seg_label_mapping(metainfo)    → seg_label_mapping(np.ndarray, 逐点标签查表)
    └─ super().__init__()（mmengine BaseDataset：读 info pkl → 逐条 parse_data_info → build pipeline）
           │
           └─ get_scene_idxs(scene_idxs)  → data_list 按 scene_idxs 重排（可重复）
```

两套映射的分工要分清：

- `label_mapping`（dict）：给**检测式**的类别子集选择用，描述「原始类 id → 新类 id」。
- `seg_label_mapping`（ndarray）：给**逐点标签**用，被管线中的 `PointSegClassMapping` 消费——`pts_semantic_mask` 直接用它做数组下标查表。

`seg_label_mapping` 的构造逻辑：长度为 `len(seg_all_class_ids)+1` 的全 `neg_label` 数组，再把有效类 id 的位置写成连续序号：

```text
seg_max_cat_id = len(seg_all_class_ids)     # 例如 19
neg_label      = len(seg_valid_class_ids)   # 未选中/无效类 → 映到 ignore 一侧
seg_label_mapping = np.ones(seg_max_cat_id + 1) * neg_label
for cls_idx, cat_id in enumerate(seg_valid_class_ids):
    seg_label_mapping[cat_id] = cls_idx
```

#### 4.1.3 源码精读

**METAINFO 的四个键**：分割数据集比检测多声明了 `seg_valid_class_ids`（参与训练的原始 id）与 `seg_all_class_ids`（标签文件里可能出现的全部 id），这是逐点标签映射的依据。

[seg3d_dataset.py:L55-L60](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/seg3d_dataset.py#L55-L60)：`Seg3DDataset` 的 `METAINFO` 声明 `classes / palette / seg_valid_class_ids / seg_all_class_ids` 四个键，子类用真实数据覆盖。

[seg3d_dataset.py:L86-L98](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/seg3d_dataset.py#L86-L98)：构造函数中先定 `ignore_index`（缺省为 `len(classes)`，SemanticKITTI 配置里显式传 19），再算 `label_mapping / label2cat / seg_valid_class_ids` 并全部塞进 metainfo——这些元信息随后会随 `dataset_meta` 流向 `SegMetric` 与可视化器。

[seg3d_dataset.py:L188-L209](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/seg3d_dataset.py#L188-L209)：`get_seg_label_mapping` 生成前述的查表数组；注意 `neg_label = len(seg_valid_class_ids)`，即「无效类 → 序号等于有效类数」，配合 `ignore_index=19` 使用。

**SemanticKITTI 的特殊之处**：它的原始标签 id 稀疏且含动态目标合并（如 252 moving-car → car），所以配置文件里手工维护了一张 `labels_map` 字典，子类重写 `get_seg_label_mapping` 把这张 dict 物化成数组。

[semantickitti_dataset.py:L91-L95](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/semantickitti_dataset.py#L91-L95)：`SemanticKittiDataset.get_seg_label_mapping` 用 metainfo 里的 `seg_label_mapping`（即配置中的 `labels_map`）与 `max_label` 逐 id 填表，生成长度 260 的映射数组。

[semantickitti.py:L12-L50](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/semantickitti.py#L12-L50)：数据配置里的 `labels_map` 把 30 个原始 id 映射到 19 类或 19（unlabeled），`metainfo` 里带着 `seg_label_mapping=labels_map` 和 `max_label=259`。注意 19 既是不存在的第 20 类，也是 `ignore_index`——unlabeled 点在损失里被忽略。

**parse_data_info 的「轻」**：分割不需要框标注加工，基类的 `parse_data_info` 只做路径拼接与 `seg_label_mapping` 注入，没有 `parse_ann_info` 一环。

[seg3d_dataset.py:L240-L287](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/seg3d_dataset.py#L240-L287)：拼接 `lidar_points`、`pts_instance_mask_path`、`pts_semantic_mask_path` 为绝对路径，并把 `seg_label_mapping` 写入 info dict 供管线使用（L279-L281）。

**prepare_data 的「重」**：与 `Det3DDataset.prepare_data` 的深拷贝 + 过滤不同，分割基类在训练态把 **dataset 自身**塞进 data dict——这是混合增强（PolarMix / LaserMix 需要跨样本拼点云）的接口钩子。

[seg3d_dataset.py:L289-L305](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/seg3d_dataset.py#L289-L305)：训练分支中 `data_info['dataset'] = self` 后再过管线；测试分支走 mmengine 默认 `prepare_data`。

[seg3d_dataset.py:L307-L329](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/seg3d_dataset.py#L307-L329)：`get_scene_idxs` 支持传 npy 文件或数组重排 `data_list`（同一帧可出现多次）；测试态恒为 `arange`，一次加载完整场景。L122-L124 在 `__init__` 尾部用 `scene_idxs` 重排数据列表。

**分割管线与评估器**：分割的 train/test 管线比检测短得多——没有 3D 框增强，只有加载、标签映射与轻量几何增强。

[semantickitti.py:L69-L98](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/semantickitti.py#L69-L98)：`train_pipeline` = `LoadPointsFromFile`(4 维：xyz+强度) → `LoadAnnotations3D`(with_seg_3d=True) → `PointSegClassMapping` → `RandomFlip3D` → `GlobalRotScaleTrans` → `Pack3DDetInputs(keys=['points', 'pts_semantic_mask'])`。`pts_semantic_mask` 被打包进 `gt_pts_seg`（见 u3-l8 的路由表）。

[loading.py:L461-L475](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L461-L475)：`PointSegClassMapping` 的职责就是 `pts_semantic_mask = seg_label_mapping[pts_semantic_mask]`，把原始 id 换成连续训练 id。

[seg_metric.py:L46-L67](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/seg_metric.py#L46-L67) 与 [seg_metric.py:L103-L137](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/seg_metric.py#L103-L137)：`SegMetric.process` 逐帧攒 `(eval_ann_info, pred_pts_seg)` 到 CPU，`compute_metrics` 最后统一调 `seg_eval`（实现在 `mmdet3d/evaluation/functional/seg_eval.py:L70`）算逐类 IoU 与 mIoU——AP 类指标需要全量排序，而 IoU 类指标逐帧可算，但这里仍按 BaseMetric 两段式契约组织。GT 来自 `eval_ann_info['pts_semantic_mask']`（由 `LoadAnnotations3D` 在测试管线里写入，见 u3-l8）。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `seg_label_mapping` 数组的形状与内容，理解「原始 id → 训练 id」的查表过程。

**操作步骤**（示例代码，非项目原有）：

```python
import numpy as np
from mmdet3d.datasets import SemanticKittiDataset  # import 顺带完成注册

# 复刻 semantickitti_dataset.py L91-95 的逻辑
labels_map = {0: 19, 1: 19, 10: 0, 11: 1, 30: 5, 252: 0, 259: 4}  # 节选
max_label = 259
mapping = np.zeros(max_label + 1, dtype=np.int64)
for idx in labels_map:
    mapping[idx] = labels_map[idx]

raw_mask = np.array([10, 10, 30, 0, 252, 99], dtype=np.int32)  # 模拟 6 个点的原始标签
print(mapping[raw_mask])   # 逐点查表
```

**需要观察的现象**：输出应为 `[0 0 5 19 0 19]`——car(10) 与 moving-car(252) 都变成 0，person(30) 变成 5，unlabeled(0) 与 other-object(99) 都变成 19（ignore）。

**预期结果**：确认「`PointSegClassMapping` 就是一次数组下标查表」，且动态/静态同类目标被合并、无效类统一为 19。

**待本地验证**：以上输出需在装好 mmdet3d 的环境中运行确认（`mapping` 本身是纯 numpy 逻辑，不依赖权重）。

#### 4.1.5 小练习与答案

**练习 1**：`Seg3DDataset` 里 `label_mapping`（dict）和 `seg_label_mapping`（ndarray）各自被谁消费？
**答案**：`label_mapping` 面向类别子集选择，结果进 metainfo（供子集训练与评估用）；`seg_label_mapping` 是逐点标签的查表数组，由管线中的 `PointSegClassMapping` 对 `pts_semantic_mask` 做下标映射。

**练习 2**：为什么 `SemanticKittiDataset` 要重写 `get_seg_label_mapping`，而 ScanNet seg 类基本不用？
**答案**：SemanticKITTI 的原始标签 id 稀疏（0/1/10/…/259）且含动态目标合并规则，需要在配置里手工维护 `labels_map` dict（含 `max_label`）再物化成数组；基类默认实现假设 `seg_all_class_ids` 恰是 `0..C-1` 的连续小集合，不适配 SemanticKITTI。

**练习 3**：`serialize_data` 在 `Seg3DDataset` 里默认 False，而 mmengine 默认 True，为什么？
**答案**：见 [seg3d_dataset.py:L45-L48](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/seg3d_dataset.py#L45-L48) 的 docstring：分割场景单帧点云巨大，序列化共享内存的开销/收益不划算，故默认关闭，由 dataloader worker 各自持有一份。

### 4.2 segmentors 家族：Base3DSegmentor 与 EncoderDecoder3D

#### 4.2.1 概念说明

检测侧的组织单元是 `detector`，分割侧是 `segmentor`。`mmdet3d/models/segmentors/__init__.py` 只导出 5 个类（见 [segmentors/__init__.py:L8-L11](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/segmentors/__init__.py#L8-L11)）：`Base3DSegmentor`、`EncoderDecoder3D`、`Cylinder3D`、`MinkUNet`、`Seg3DTTAModel`（TTA 已在 u7-l4 讲过）。

- `Base3DSegmentor`：继承 mmengine `BaseModel`，约定 forward 三模式与「输出写进 `pred_pts_seg`」的后处理。
- `EncoderDecoder3D`：经典「编码器-解码器」分割骨架，组装 `backbone + decode_head (+neck / auxiliary_head / loss_regularization)`，并提供滑窗推理。
- `Cylinder3D` / `MinkUNet`：体素基分割器，重写 `loss/predict` 以适配 `voxels` 输入。

#### 4.2.2 核心流程

`Base3DSegmentor.forward` 的三模式与检测器完全同构（u4-l2）：

```text
mode='loss'    → self.loss(inputs, data_samples)      返回 dict[str, Tensor]
mode='predict' → self.predict(inputs, data_samples)   返回 List[Det3DDataSample]
mode='tensor'  → self._forward(inputs, data_samples)  返回裸张量
```

`EncoderDecoder3D` 的默认训练/推理主线：

```text
loss():    torch.stack(points) → extract_feat → decode_head.loss（+aux_head/正则项）
predict(): 逐场景 inference()
              ├─ test_cfg.mode == 'slide' → slide_inference()（滑窗 + scatter_add 聚合）
              └─ test_cfg.mode == 'whole' → whole_inference()（整场景一次前向）
           → postprocess_result()（argmax → 写 pred_pts_seg / pts_seg_logits）
```

关键认知：`EncoderDecoder3D` 的默认实现假设输入是**等长点堆叠**的 `(B, N, 3+C)` 张量、decode head 输出 `(B, num_classes, N)` 的点级 logits——这适合室内点基模型。Cylinder3D/MinkUNet 是**体素基**模型，输入是 `voxels` 字典、head 输出体素级 logits，因此二者都绕开默认 `loss/predict` 各自重写，只复用 `EncoderDecoder3D` 的组装逻辑与 `postprocess_result`。

#### 4.2.3 源码精读

[base.py:L66-L109](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/segmentors/base.py#L66-L109)：`Base3DSegmentor.forward` 按 mode 分发到 `loss/predict/_forward`；`extract_feat`、`loss`、`predict`、`_forward` 均为抽象方法，强制子类实现。

[base.py:L135-L165](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/segmentors/base.py#L135-L165)：`postprocess_result` 是所有分割器共享的出口——对每场景 logits 沿类别维 `argmax` 得 `seg_pred`，然后以两个 `PointData` 写回 data_sample：`pts_seg_logits.pts_seg_logits`（原始 logits）与 `pred_pts_seg.pts_semantic_mask`（预测标签）。这正是 u2-l4 讲过的「点级数据装 PointData」契约在分割输出的落点。

[encoder_decoder.py:L80-L103](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/segmentors/encoder_decoder.py#L80-L103)：`EncoderDecoder3D.__init__` 用 `MODELS.build` 依次构建 backbone / neck / decode_head / auxiliary_head / loss_regularization，并断言必须有 decode_head（L102-L103）。`num_classes` 取自 decode_head（L108）。

[encoder_decoder.py:L226-L262](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/segmentors/encoder_decoder.py#L226-L262)：默认 `loss` 在 L245-L246 直接 `torch.stack(batch_inputs_dict['points'])`——这行就是「点基假设」的出处；`_decode_head_forward_train` 把 head 的损失字典加 `decode` 前缀（L176）。

[encoder_decoder.py:L403-L447](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/segmentors/encoder_decoder.py#L403-L447)：`slide_inference` 先用 `_sliding_patch_generation` 把场景切成带 50% 重叠的 patch 并把每 patch 补/重采样到固定点数，再分批前向，最后用 `scatter_add_` + `bincount` 对重叠点的 logits 做**计数平均**（L438-L443）——这是滑窗融合的实现位置。

[encoder_decoder.py:L481-L523](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/segmentors/encoder_decoder.py#L481-L523)：默认 `predict` 逐场景调用 `inference`（slide/whole 二选一，L470 断言）；注释（L509-L511）点明分割必须逐点输出、无法靠下采样对齐 batch，所以**一次只测一个场景**。Cylinder3D 的 `test_cfg=dict(mode='whole')`（整场景一次前向），MinkUNet 的 `test_cfg=dict()`。

#### 4.2.4 代码实践

**实践目标**：用配置直接构建 Cylinder3D segmentor，确认「EncoderDecoder3D 组装 + 子类扩展 voxel_encoder」的层次关系。

**操作步骤**（示例代码）：

```python
from mmengine.config import Config
from mmdet3d.registry import MODELS

cfg = Config.fromfile('configs/_base_/models/cylinder3d.py')
model = MODELS.build(cfg.model)

print(type(model).__mro__)            # Cylinder3D → EncoderDecoder3D → Base3DSegmentor → BaseModel
print(hasattr(model, 'voxel_encoder')) # True：Cylinder3D 独有
print(model.num_classes)               # 20（含 ignore 类槽位，来自 decode_head）
```

**需要观察的现象**：MRO 列表里 `Cylinder3D` 在 `EncoderDecoder3D` 之前；`voxel_encoder` 属性存在而 `EncoderDecoder3D` 基类不认识它。

**预期结果**：验证 `MODELS.build` 能从 dict 滚出整棵分割器对象树（注册器机制见 u4-l1），且 Cylinder3D 的扩展点只有一个 `voxel_encoder`。

**待本地验证**：需在安装了 mmcv（含 spconv 算子）的环境中运行；若缺 spconv，`Asymm3DSpconv` 中的 `SubMConv3d` 导入会失败。

#### 4.2.5 小练习与答案

**练习 1**：`EncoderDecoder3D` 的 `auxiliary_head` 与 `loss_regularization` 各自的作用是什么？
**答案**：`auxiliary_head` 挂在骨干中间特征上做深监督（训练期额外损失，推理时弃用，见类 docstring L21-L22）；`loss_regularization` 对模型权重施正则损失（`_loss_regularization_forward_train` L211-L224，直接传入 `self.modules()`）。Cylinder3D/MinkUNet 的配置里两者均为 None。

**练习 2**：为什么 `Cylinder3D.predict` 里要 `seg_logits_list[i].transpose(0, 1)`？
**答案**：head 的 `predict` 返回每场景 `(num_classes, N)` 的点级 logits（沿类别维 argmax 方便），而 `postprocess_result` 里 `seg_logits.argmax(dim=0)` 约定类别在第 0 维——两个子类统一 transpose 对齐（[cylinder3d.py:L141-L142](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/segmentors/cylinder3d.py#L141-L142)）。

### 4.3 Cylinder3D 全链路：柱体分区与不对称稀疏卷积

#### 4.3.1 概念说明

Cylinder3D（CVPR 2021）针对室外 LiDAR 点云的两大特性设计：

1. **密度随距离急剧衰减**。笛卡尔体素下，远处格子大多为空、近处格子点数爆炸，格间点数极不均衡。**柱体分区（cylindrical partition）** 改用柱坐标 \((\rho, \phi, z)\) 划格：方位角 \(\phi\) 天然等分圆周，使远近处的格子在角度上均匀、每格点数更平衡。网格为 `480 × 360 × 32`，范围 \([0, -\pi, -4, 50, \pi, 2]\)。
2. **z 轴与 BEV 两平面语义不同**。论文发现对 z 维无差别使用 3×3×3 卷积并无收益，于是用**不对称卷积**：BEV 两个方向用 (3,1,3)/(1,3,3) 交错，z 方向保持 3；下采样也优先压 BEV（stride (2,2,1)），z 维只在特定层下采样。

坐标换算：

\[
\rho = \sqrt{x^2 + y^2}, \quad \phi = \mathrm{atan2}(y, x), \quad z = z
\]

#### 4.3.2 核心流程

Cylinder3D 一次 `loss` 前向的完整数据流：

```text
points (List[Tensor (N,4)])                         # xyz + 强度
  │ Det3DDataPreprocessor.voxelize(voxel_type='cylindrical')
  │   ├─ 直角坐标 → 柱坐标 (rho, phi, z)，clamp 到范围
  │   ├─ floor((polar - min) / voxel_size) → res_coors (N,3)
  │   ├─ get_voxel_seg(res_coors, data_sample)
  │   │     训练: one-hot 逐点标签 → dynamic_scatter_3d(mean) → argmax
  │   │           ⇒ voxel_semantic_mask (体素级标签, 多数投票) + point2voxel_map
  │   │     测试: 只生成 point2voxel_map
  │   └─ res_voxels = cat(rho, phi, z, x, y, intensity) → (N, 6)；coors 补 batch 列
  ▼
voxels dict {voxels(N,6), coors(N,4)}
  │ SegVFE：逐点 [6 + 3(到格心偏移)] → Linear 64/128/256/256 → max 池化到体素 → 压缩到 16
  ▼
Asymm3DSpconv：SparseConvTensor(16ch) → down_context → 4×DownBlock(2×通道) → 4×UpBlock(逆卷积上采样+skip) → DDCM → concat → 128ch
  │
  ▼ Cylinder3DHead：SubMConv3d(128→20, 3³) ⇒ 每体素 20 类 logits
  │ 训练: loss_by_feat → CE + Lovasz（体素级，ignore_index=19）
  │ 推理: 按 coors[:,0] 过滤本场景体素 → seg_logits[point2voxel_map] 回投影到每点
  ▼ postprocess_result → pred_pts_seg.pts_semantic_mask (N,)
```

#### 4.3.3 源码精读

**（a）柱体体素化**。配置入口在模型基座里：

[models/cylinder3d.py:L1-L14](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/cylinder3d.py#L1-L14)：`grid_shape=[480,360,32]`、`point_cloud_range=[0,-π,-4,50,π,2]`、`max_num_points=-1, max_voxels=-1`（cylindrical 分支本来就不吃这两个上限，只依赖 grid/range）、`voxel_type='cylindrical'`。`VoxelizationByGridShape` 由 grid_shape 反推 `voxel_size = (range)/(grid-1)`（u4-l3 已讲），这里得到 \((50/479,\ 2\pi/359,\ 6/31)\)。

[data_preprocessor.py:L394-L426](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/data_preprocessors/data_preprocessor.py#L394-L426)：`cylindrical` 分支逐样本做坐标换算（L397-L399）、按范围 clamp（L404-L415）、`floor` 得整数格坐标（L416-L418）、调用 `get_voxel_seg`（L419）、补 batch 列（L420），并把 `res_voxels` 拼成 **(rho, phi, z, x, y, intensity) 共 6 列**（L421-L422）——注意特征里同时保留柱坐标与原始 x,y，这就是 `SegVFE` `in_channels=6` 的来源。

[data_preprocessor.py:L470-L492](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/data_preprocessors/data_preprocessor.py#L470-L492)：`get_voxel_seg` 训练分支把 `pts_semantic_mask` one-hot 后按格 mean 再 argmax——即**每格取出现次数最多的标签**（多数投票）作为 `voxel_semantic_mask`，同时拿到 `point2voxel_map`；测试分支用全 1 伪张量走同一 scatter，只为拿 `point2voxel_map`。这两个张量都直接写进 `data_sample`（随 `gt_pts_seg` / 根级字段流转）。

**（b）SegVFE：分割专用体素编码器**。

[voxel_encoder.py:L523-L596](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/voxel_encoders/voxel_encoder.py#L523-L596)：构造时 `with_voxel_center=True` 会使 `in_channels` 加 3（L541-L542，6→9）；`feat_channels=[64,128,256,256]` 组成逐点 `nn.Linear` 栈；`vfe_scatter` 是 mmcv 的 `DynamicScatter`（max 模式，L590-L592）；`feat_compression=16` 再加一个 Linear+ReLU 把 256 压到 16（L594-L596），正好等于骨干 `input_channels=16`。

[voxel_encoder.py:L598-L640](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/voxel_encoders/voxel_encoder.py#L598-L640)：`forward` 里 L613-L622 计算**每个点到所在格中心**的偏移 `f_center`（注意：在柱坐标语义下，`vx/vy/vz` 分别是 rho/phi/z 方向的格宽，`coors[:,1..3]` 是 rho/phi/z 的格索引）；逐点过 Linear 栈后在 L633 用 `vfe_scatter` 把变长点特征聚合成定长体素特征，返回 `(voxel_feats, voxel_coors)`。

**（c）Asymm3DSpconv 骨干**。

[backbones/cylinder3d.py:L460-L481](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/backbones/cylinder3d.py#L460-L481)：`forward` 把 `(voxel_features, coors, batch_size)` 包成 `SparseConvTensor`（L463-L465），先 `down_context`，再 4 个 DownBlock（每个同时返回池化结果与 skip，L470-L472），反向 4 个 UpBlock 消费 skip（L475-L476），最后 DDCM 并把其输出与 up 的特征 concat（L478-L479）→ 输出 128 通道体素特征。通道账本：16 → down_context 32 → 64/128/256/512 → up 512/256/128/64 → DDCM 64 → concat 64+64=128（与 decode head `channels=128` 严格咬合）。

[backbones/cylinder3d.py:L43-L59](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/backbones/cylinder3d.py#L43-L59)：`AsymmResBlock` 内两条支路的卷积核分别是 `(1,3,3)` 与 `(3,1,3)`——**不对称体现在 BEV 的 rho/phi 两个方向**，z 方向恒为 3；两支路结果相加构成残差（[L83-L104](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/backbones/cylinder3d.py#L83-L104)）。所有 SubMConv3d 共享 `indice_key + 'bef'`（L49），复用同一张 rule book。

[backbones/cylinder3d.py:L175-L193](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/backbones/cylinder3d.py#L175-L193)：DownBlock 的池化层有**命名陷阱**：`height_pooling=True` 时用全三维 `stride=2` 的 SparseConv3d（**对 z 也下采样**），`False` 时用 `stride=(2,2,1)`（保 z）。默认 `height_pooing=[True, True, False, False]`（[L416](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/backbones/cylinder3d.py#L416)，注意源码拼写就是双 o）——前两个 DownBlock 把 z 从 32 压到 8，后两个只压 BEV。`forward` 返回 `(pooled_res, res)` 二元组（[L195-L219](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/backbones/cylinder3d.py#L195-L219)），`res` 就是 U-Net 的 skip。

[backbones/cylinder3d.py:L286-L317](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/backbones/cylinder3d.py#L286-L317)：UpBlock 用 `SparseInverseConv3d(indice_key=up_key)` 做**精确可逆上采样**（`up_key` 指回对应 DownBlock 的池化卷积，见组装处 [L437-L452](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/backbones/cylinder3d.py#L437-L452)），上采样后与 skip 的特征直接相加（L303），再过三个不对称卷积。

[backbones/cylinder3d.py:L341-L369](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/backbones/cylinder3d.py#L341-L389)：`DDCMBlock`（维度分解上下文建模）用三支核 `(3,1,1)/(1,3,1)/(1,1,3)` 分别只在单一方向看上下文，Sigmoid 激活后求和，再**与输入特征逐元素相乘**充当注意力门控（L387）。

**（d）Cylinder3DHead：体素级分类与回投影**。

[cylinder3d_head.py:L78-L87](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/decode_heads/cylinder3d_head.py#L78-L87)：重写 `build_conv_seg`，分类器是一个 `SubMConv3d(channels→num_classes, kernel=3)`——**分类发生在体素域**，且仍是不对称骨干里的 submanifold 卷积（共享 `indice_key='logit'`）。对比基类默认的 `nn.Conv1d`（[decode_head.py:L106-L116](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/decode_heads/decode_head.py#L106-L116) 的 `cls_seg`），这是体素基 head 的标志。

[cylinder3d_head.py:L94-L122](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/decode_heads/cylinder3d_head.py#L94-L122)：`loss_by_feat` 直接 cat 各样本的 `gt_pts_seg.voxel_semantic_mask`（预处理阶段多数投票的产物）与 `seg_logit.features`（体素级 logits）对齐计算 **CE + Lovasz** 双损失，`ignore_index=19`。Lovasz loss 直接优化 IoU 上界，是分割任务常用的互补损失（配置见 [configs/_base_/models/cylinder3d.py:L32-L38](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/cylinder3d.py#L32-L38)）。

[cylinder3d_head.py:L124-L158](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/decode_heads/cylinder3d_head.py#L124-L158)：`predict` 是**回投影**的实现：用 `voxels['voxel_coors']` 的 batch 列过滤出本场景的体素（L150-L152），再 `seg_logits_sample[point2voxel_map]` 把体素 logits 广播回每个点（L153-L156）——同一格内所有点共享该格的预测。

**（e）segmentor 串线**。

[cylinder3d.py:L68-L79](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/segmentors/cylinder3d.py#L68-L79)：`Cylinder3D` 在 `EncoderDecoder3D` 之上只加一个 `voxel_encoder`；`extract_feat` 依次调用 `voxel_encoder(voxels, coors)` 与 `backbone(feats, coors, batch_size)`，并把编码器去重后的 `voxel_coors` 写回 `voxels` 字典（L74）——供 head 的 `predict` 过滤 batch 用。`loss`（[L81-L105](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/segmentors/cylinder3d.py#L81-L105)）与 `predict`（[L107-L144](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/segmentors/cylinder3d.py#L107-L144)）均绕开基类的 `torch.stack(points)` 默认实现，predict 直接把 head 的 `(num_classes, N)` 输出交给 `postprocess_result`。

**（f）训练配置**。

[cylinder3d_4xb4-3x_semantickitti.py:L1-L38](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/cylinder3d/cylinder3d_4xb4-3x_semantickitti.py#L1-L38)：继承 semantickitti 数据基座 + cylinder3d 模型基座 + default_runtime；AdamW lr=1e-3、36 epoch、milestone 30 衰减 0.1、batch_size=4。README 报告该配置 mIoU 63.1±0.5；加 Laser-Polar Mix 增强的变体可达 67.0（见 [configs/cylinder3d/README.md](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/cylinder3d/README.md)）。

#### 4.3.4 代码实践

**实践目标**：用 20 行代码手工复现柱体坐标换算与格坐标计算，验证你对 `cylindrical` 分支的理解。

**操作步骤**（示例代码）：

```python
import torch

# 与 configs/_base_/models/cylinder3d.py 一致
grid_shape = torch.tensor([480, 360, 32], dtype=torch.float32)
pc_range = torch.tensor([0, -3.14159265359, -4, 50, 3.14159265359, 2])
voxel_size = (pc_range[3:] - pc_range[:3]) / (grid_shape - 1)
print('voxel_size =', voxel_size)   # rho≈0.1044, phi≈0.01752, z≈0.1935

pts = torch.tensor([[10.0, 0.0, 1.0, 0.5],   # x, y, z, intensity
                    [40.0, 40.0, -3.0, 0.2]])
rho = torch.sqrt(pts[:, 0]**2 + pts[:, 1]**2)
phi = torch.atan2(pts[:, 1], pts[:, 0])
polar = torch.stack((rho, phi, pts[:, 2]), dim=-1)
polar = torch.clamp(polar, pc_range[:3], pc_range[3:])
coors = torch.floor((polar - pc_range[:3]) / voxel_size).int()
print('coors =\n', coors)
print('特征 6 列 =', torch.cat((polar, pts[:, :2], pts[:, 3:]), dim=-1).shape)
```

**需要观察的现象**：`voxel_size` 的三个分量；点 (10,0,1) 落在 rho≈95 格、phi≈180 格；点 (40,40,-3) 的 rho≈540 超出 480 格上限，被 clamp 到 479（体会 clamp 的作用）。

**预期结果**：`coors` 两行都在 `[0,479]×[0,359]×[0,31]` 内；特征张量形状 `(2, 6)`。与 `data_preprocessor.py` L397-L422 逐行对应。

**待本地验证**：数值输出请在本地运行确认。

#### 4.3.5 小练习与答案

**练习 1**：柱体化后每个体素的 6 维输入特征是什么？为什么不直接用 (rho, phi, z)？
**答案**：`(rho, phi, z, x, y, intensity)`（L421-L422）。保留原始 x, y 与柱坐标并存，让网络既能利用柱坐标的均衡划分，又能接触原始几何量；`SegVFE` 还会再拼 3 维「点到格中心的偏移」。

**练习 2**：`height_pooling=True` 到底池化不池化 z 维？
**答案**：池化。`True` → `SparseConv3d(stride=2)` 三维齐步下采样；`False` → `stride=(2,2,1)` 只压 BEV（L175-L193）。默认序列 `[True, True, False, False]` 使 z 只在前两层从 32 降到 8。

**练习 3**：训练时的 `voxel_semantic_mask` 是怎么从逐点标签得到的？为什么说它是「有损」的？
**答案**：逐点标签 one-hot 后按格 mean 再 argmax（[data_preprocessor.py:L480-L487](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/data_preprocessors/data_preprocessor.py#L480-L487)），即多数投票。同格内不同类别的点被迫共用一个标签，监督被「压扁」，论文中的 point-wise refinement 模块就是为缓解这一点（README 注明本实现未含该模块）。

### 4.4 MinkUNet：多后端稀疏 UNet 与 minkunet 体素化

#### 4.4.1 概念说明

MinkUNet 出自《4D Spatio-Temporal ConvNets》（Minkowski Engine 一文），是最经典的稀疏卷积 U-Net 分割基线：**在笛卡尔坐标下等距量化**（voxel 0.05m），编码器 kernel=2/stride=2 下采样，解码器转置卷积上采样，skip 连接沿通道 concat。它与 Cylinder3D 的差异不在「是否稀疏」，而在**分区方式**（等距立方体 vs 柱体）与**稀疏库后端**。

mmdet3d 的实现把三种稀疏卷积库统一封装成可选后端：`torchsparse`（默认）、`spconv`、`minkowski`（MinkowskiEngine）。三者都是**可选依赖**，需要按官方安装文档单独安装；这就是本讲标题所说「MinkUNet/SPVCNN 依赖的稀疏库」。配套的 `spvcnn_backone.py`（SPVCNN，稀疏体素 + 点分支混合）复用同一套后端封装。

#### 4.4.2 核心流程

MinkUNet 的前向链路（`voxel_type='minkunet'`）：

```text
points (N,4)
  │ voxelize() minkunet 分支：
  │   coors = round(xyz / 0.05) - min          # 逐点格坐标
  │   sparse_quantize：ravel_hash + np.unique  # 去重得 M 个体素
  │     inds(M,)         每个不重复格的首个点下标
  │     point2voxel_map(N,)  每个点 → 体素行号（inverse）
  │   训练: voxel_semantic_mask = pts_semantic_mask[inds]  # 首现点标签
  │   voxels = points[inds] (M,4)；coors 补 batch 列（batch_first=False → 末列）
  ▼
MinkUNetBackbone：SparseTensor → conv_input → 4×encoder(2×通道) → 4×decoder(转置卷积+skip cat)
  ▼ (M, 96)
MinkUNetHead：nn.Linear(96→19) ⇒ 每体素 logits
  │ predict：按 data_sample.batch_idx 过滤 → seg_logits[point2voxel_map] 回投影
  ▼ postprocess_result → pred_pts_seg
```

#### 4.4.3 源码精读

**（a）minkunet 体素化：量化 + 哈希去重**。

[data_preprocessor.py:L427-L460](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/data_preprocessors/data_preprocessor.py#L427-L460)：逐样本 `round(xyz/voxel_size)` 后减全局最小值（L431-L432，让格坐标从 0 起），然后 `sparse_quantize` 得 `inds`（去重后每个格的**首个点**下标）与 `point2voxel_map`（L434-L437）。训练态若体素数超过 `max_voxels=80000` 则随机抽体素（L438-L441）。L443-L445：`voxel_semantic_mask = pts_semantic_mask[inds]`——**直接取每格首现点的标签**，不做 Cylinder3D 那样的多数投票。L448-L456：`batch_first=False` 时 batch 列补在**末列**，且把 `batch_idx` 与 `point2voxel_map` 写进 data_sample 供 head 使用。

[data_preprocessor.py:L494-L542](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/data_preprocessors/data_preprocessor.py#L494-L542)：`ravel_hash` 把 3 列格坐标按列宽进位混成一个 uint64 哈希（L505-L513），`sparse_quantize` 用 `np.unique` 一次性拿到去重下标与逆映射——这是纯 numpy 的 O(N log N) 去重方案，不依赖 CUDA 算子。

**（b）MinkUNetBackbone：一套结构，三种后端**。

[minkunet_backbone.py:L74-L116](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/backbones/minkunet_backbone.py#L74-L116)：构造函数按 `sparseconv_backend` 从 `mmdet3d/models/layers/` 下的三套封装（`torchsparse_block` / `sparse_block`(spconv) / `minkowski_engine_block`）里选模块与残差块，并断言对应库已安装。转置卷积只在上采样层使用（minkowski 分支用 `MinkowskiConvNdTranspose`，L111-L113）。

[minkunet_backbone.py:L132-L197](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/backbones/minkunet_backbone.py#L132-L197)：编码器每级 = 一个 kernel=2/stride=2 的下采样卷积 + 若干残差块；解码器每级 = 转置卷积上采样 + 残差块，首个残差块输入通道要加上对应编码器 lateral 的通道数（L179），即 **concat 式 skip**（与 Cylinder3D 的相加式 skip 不同）。通道账本：4(xyz+i) → 32 → 64 → 128 → 256 → 解码 256 → 128 → 96 → 96。

[minkunet_backbone.py:L199-L245](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/backbones/minkunet_backbone.py#L199-L245)：`forward` 按后端把 `(voxel_features, coors)` 包成对应的 SparseTensor（torchsparse 直接收原始 coors；spconv 分支需自己推 `spatial_shape` 与 batch 数，L213-L216），走完 U-Net 后返回裸特征矩阵 `(M, 96)`（`decoder_outs[-1].F` 或 `.features`）。注意 spconv 分支要求 coors 的 batch 列在第 0 列，torchsparse/minkowski 在末列——这与预处理器 `batch_first` 的取值配套。

**（c）MinkUNetHead：最简 head**。

[minkunet_head.py:L27-L38](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/decode_heads/minkunet_head.py#L27-L38)：`build_conv_seg` 就是 `nn.Linear(channels, num_classes)`——因为骨干输出已是裸 `(M, C)` 矩阵，分类无需任何卷积；`_stack_batch_gt` cat 的是 `voxel_semantic_mask`（体素级 GT）。

[minkunet_head.py:L40-L62](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/decode_heads/minkunet_head.py#L40-L62)：`predict` 用预处理器写在 data_sample 上的 `batch_idx`（每个体素的 batch 号）过滤本场景体素，再用 `point2voxel_map` 回投影到点——与 `Cylinder3DHead.predict` 同构，只是「本场景体素的行号」来源不同（MinkUNet 用 `batch_idx` 掩码，Cylinder3D 用 `voxels['voxel_coors'][:,0]`）。

**（d）segmentor 与配置**。

[minkunet.py:L95-L112](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/segmentors/minkunet.py#L95-L112)：`MinkUNet` 是**纯薄壳**：`extract_feat` 直接 `backbone(voxels, coors)`（它连 voxel_encoder 都不需要，因为量化后的原始 4 维点特征就够用），`loss/predict/_forward` 三个重写都只是绕开基类的 stack-points 假设。

[configs/_base_/models/minkunet.py:L1-L33](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/minkunet.py#L1-L33)：`voxel_type='minkunet'`、`batch_first=False`、`max_voxels=80000`、`voxel_size=[0.05,0.05,0.05]`；head 用普通 CE（`avg_non_ignore=True`，无 Lovasz）、`ignore_index=19`。

[minkunet18_w32_torchsparse_8xb2-amp-15e_semantickitti.py:L1-L54](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/minkunet/minkunet18_w32_torchsparse_8xb2-amp-15e_semantickitti.py#L1-L54)：训练配置覆盖了 train_pipeline（只做全局旋转缩放、不翻转）与优化器（SGD lr=0.24、`AmpOptimWrapper` 混合精度、cosine 退火 15 epoch）——与 Cylinder3D 的 AdamW 低学习率路线形成对照。

**（e）两种 voxel/点表示的对比总表**：

| 维度 | Cylinder3D（`cylindrical`） | MinkUNet（`minkunet`） |
| --- | --- | --- |
| 分区坐标 | 柱坐标 (rho, phi, z)，网格 480×360×32 | 笛卡尔 xyz，0.05m 等距立方体 |
| 格坐标计算 | `floor((clamp(polar)-min)/voxel_size)` | `round(xyz/0.05) - min` |
| 去重方式 | `SegVFE` 内 mmcv `DynamicScatter`（max） | numpy `ravel_hash` + `np.unique` |
| 体素输入特征 | (rho,phi,z,x,y,i) 6 列 + 3 维格心偏移，SegVFE 编码 | 原始点 4 列 (x,y,z,i)，无 encoder |
| 体素级 GT | one-hot mean + argmax（多数投票） | 首现点标签 `mask[inds]` |
| batch 列位置 | coors 第 0 列（`batch_first=True`） | coors 末列（`batch_first=False`，torchsparse 约定） |
| head 分类器 | `SubMConv3d(128→20, k=3)`（体素域卷积） | `nn.Linear(96→19)`（裸特征线性层） |
| 损失 | CE + Lovasz | CE（avg_non_ignore） |
| head 回投影依据 | `voxels['voxel_coors'][:,0]` + `point2voxel_map` | `data_sample.batch_idx` + `point2voxel_map` |
| 稀疏库 | mmcv spconv 1.x | torchsparse / spconv2 / MinkowskiEngine 三选一 |

#### 4.4.4 代码实践

**实践目标**：以源码阅读方式完成上表最难的一格——亲手推演 `ravel_hash` 与 `np.unique` 的输出，确认 `point2voxel_map` 的语义。

**操作步骤**：

1. 阅读 [data_preprocessor.py:L494-L542](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/data_preprocessors/data_preprocessor.py#L494-L542)，注意 `np.unique(..., return_index=True, return_inverse=True)` 三个返回值。
2. 用如下示例代码（非项目原有）验证：

```python
import numpy as np

coords = np.array([[0, 0, 0],
                   [1, 0, 0],
                   [0, 0, 0],   # 与第 0 行重复
                   [1, 0, 0]])  # 与第 1 行重复

# 复刻 ravel_hash（data_preprocessor.py L494-L514）
x = coords - coords.min(axis=0)
xmax = x.max(axis=0) + 1
h = np.zeros(x.shape[0], dtype=np.uint64)
for k in range(x.shape[1] - 1):
    h += x[:, k]
    h *= xmax[k + 1]
h += x[:, -1]

_, indices, inverse = np.unique(h, return_index=True, return_inverse=True)
print('inds =', indices)          # 每个不重复格的首现点下标
print('point2voxel_map =', inverse)  # 每个点属于第几个格
```

3. 对照 L443-L445 与 L456：`voxel_semantic_mask = pts_semantic_mask[inds]`、`data_sample.point2voxel_map = inverse`。

**需要观察的现象**：`inds = [0 1]`（格 [0,0,0] 与 [1,0,0] 各自的首现点），`point2voxel_map = [0 1 0 1]`（4 个点分别属于 0 号、1 号格）。

**预期结果**：体素数 M=2 < 点数 N=4；head 推理时 `(M, num_classes)` 的 logits 经 `point2voxel_map` 变成 `(N, num_classes)`，同格点拿到相同 logits。

**待本地验证**：输出请本地运行确认。

#### 4.4.5 小练习与答案

**练习 1**：MinkUNet 为什么不需要 Cylinder3D 那样的 `SegVFE`？
**答案**：MinkUNet 的体素特征就是量化后点的原始 4 维坐标+强度（`voxels = points[inds]`），骨干 `conv_input` 直接从 4 通道卷到 `base_channels`；Cylinder3D 的输入是变长的 6+3 维逐点特征，需要 Linear 栈 + max 池化 + 通道压缩才能变成定长体素特征。

**练习 2**：`batch_first` 在两个模型里为什么取值不同？
**答案**：spconv 1.x（mmcv）的 `SparseConvTensor` 约定 batch 列在第 0 列（与检测线 `hard/dynamic` 一致，`batch_first=True`）；torchsparse 的 `SparseTensor` 约定 batch 列在最后一列（`batch_first=False`）。预处理器据此决定 `F.pad` 补在哪一端（L448-L455）。

**练习 3**：如果训练时某帧点云量化后有 9 万个体素（超过 `max_voxels=80000`），会发生什么？被丢掉的「体素」对应的点怎么办？
**答案**：L438-L441 会随机抽 8 万个体素保留。`point2voxel_map` 仍按**未抽样**的 `np.unique` 逆映射生成（L434-L437 在抽样之前），因此被丢体素的点会指向 head 输出中不存在的行——这是阅读时值得注意的边界情形；实践中 0.05m 量化下单帧体素数通常低于该上限（待本地验证具体行为）。

## 5. 综合实践

**任务：跑通一次 Cylinder3D 分割推理，并用源码知识解释输出的每一步。**

**实践目标**：把本讲四个模块串起来——从 demo 入口出发，依次核对「管线改写 → 柱体体素化 → 骨干前向 → 回投影 → 可视化」。

**操作步骤**：

1. 下载 Cylinder3D 预训练权重（模型库见 [configs/cylinder3d/README.md](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/cylinder3d/README.md)，本配置对应 checkpoint `cylinder3d_4xb4_3x_semantickitti_20230318_191107-822a8c31.pth`）。
2. 用仓库自带的 demo 点云运行分割 demo（入口见 [demo/pcd_seg_demo.py:L70-L82](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/demo/pcd_seg_demo.py#L70-L82)，它把参数转交给 `LidarSeg3DInferencer`）：

   ```bash
   python demo/pcd_seg_demo.py \
       demo/data/kitti/000008.bin \
       configs/cylinder3d/cylinder3d_4xb4-3x_semantickitti.py \
       <checkpoint路径>/cylinder3d_4xb4_3x_semantickitti_20230318_191107-822a8c31.pth \
       --out-dir outputs/seg_demo
   ```

3. 阅读推理管线的「外科手术」：[lidar_seg3d_inferencer.py:L99-L124](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inferencers/lidar_seg3d_inferencer.py#L99-L124) 会从 test pipeline 中**删掉** `LoadAnnotations3D` 与 `PointSegClassMapping`（L103-L109，推理没有真值），并把 `LoadPointsFromFile` 替换成可吃裸路径的 `LidarDet3DInferencerLoader`（L123）——对照 u1-l3 讲过的检测版改写，注意分割版多删了一个标签映射 transform。
4. 在 `Cylinder3D.predict`（[cylinder3d.py:L138-L144](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/segmentors/cylinder3d.py#L138-L144)）处下断点或加日志，打印 `batch_inputs_dict['voxels']['voxels'].shape`、`coors.shape`、`point2voxel_map.shape` 与最终 `seg_logits_list[0].shape`。
5. 检查输出目录：`outputs/seg_demo/` 下应有可视化图片（`vis_lidar/`，Open3D 渲染、按数据集 palette 上色，见 u7-l3）与 preds 文件。

**需要观察的现象**：

- `voxels['voxels']` 形状为 `(N, 6)`（柱坐标 + x,y + 强度），`coors` 为 `(N, 4)`；
- `point2voxel_map` 长度等于点数 N，`seg_logits_list[0]` 形状为 `(20, N)`——类别数 × 点数，证明「体素级 logits 已被广播回每个点」；
- 可视化图中道路/车辆/植被等大类颜色与 SemanticKITTI 官方 palette 一致。

**预期结果**：一张逐点上色的 3D 分割结果图 + 一份含 `pts_semantic_mask`（长度 N 的类别数组）的预测文件。

**待本地验证**：以上命令与形状需要 GPU 环境（mmcv spconv 算子）与权重文件，请在本地运行确认；无 GPU 时可退而完成 4.3.4 / 4.4.4 两个纯 CPU 实践。

**进阶替代方案（tiny 训练）**：若本地已用 `tools/create_data.py` 生成 SemanticKITTI info pkl（现成 pkl 可从 [docs/en/user_guides/dataset_prepare.md:L269](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/user_guides/dataset_prepare.md#L269) 的表格下载），可截取少量样本做一档短训练，观察日志中的 `decode.loss_ce / decode.loss_lovasz` 与验证 mIoU：

```bash
python tools/train.py configs/cylinder3d/cylinder3d_4xb4-3x_semantickitti.py \
    --cfg-options train_cfg.max_epochs=1 train_dataloader.dataset.ann_file=semantickitti_infos_train.pkl
```

（`--cfg-options` 的合并语义见 u1-l5；此命令仅为示意，样本裁剪方式请自行组织，待本地验证。）

## 6. 本讲小结

- `Seg3DDataset` 与 `Det3DDataset` 的核心差异在数据契约：逐点标签 `pts_semantic_mask` + 双套映射（dict 型 `label_mapping`、数组型 `seg_label_mapping` 供 `PointSegClassMapping` 查表）+ `scene_idxs` 大场景重采样 + 训练态向管线注入 dataset 自身（服务 PolarMix/LaserMix）。
- 分割器层次为 `Base3DSegmentor → EncoderDecoder3D → Cylinder3D/MinkUNet`：基类定 forward 三模式与 `postprocess_result`（写 `pred_pts_seg`/`pts_seg_logits` 两个 `PointData`）；`EncoderDecoder3D` 的默认 loss/predict 基于「等长点堆叠」假设，体素基子类必须重写。
- Cylinder3D 两大设计：**柱体分区**（`voxelize` 的 cylindrical 分支把点换到 (rho,phi,z) 再划 480×360×32 格，特征含柱坐标与原始 x,y 共 6 列）与**不对称稀疏卷积**（(3,1,3)/(1,3,3) 交错、`SparseInverseConv3d` 精确上采样、DDCM 三方向门控）。
- 逐点分类的统一解法是「体素级分类 + 回投影」：训练时逐点标签被降采样成 `voxel_semantic_mask`（Cylinder3D 多数投票 / MinkUNet 首现点），推理时体素 logits 经 `point2voxel_map` 广播回每个点。
- MinkUNet 是多后端稀疏 U-Net：`minkunet` 体素化用 `ravel_hash + np.unique` 在 numpy 里去重，骨干用统一封装在 torchsparse/spconv/MinkowskiEngine 间切换，head 退化为一个 `nn.Linear(96→19)`。
- 评估走 `SegMetric`：`process` 攒 `(eval_ann_info, pred_pts_seg)`，`compute_metrics` 调 `seg_eval` 输出逐类 IoU 与 mIoU。

## 7. 下一步学习建议

本讲覆盖了分割主线的数据集、segmentor 与两大模型。建议接下来：

1. **u8-l2（接入自定义数据集）**：把 `Seg3DDataset` 的子类化与 converter 实践一遍，检验你对 `seg_label_mapping`、`ignore_index` 契约的掌握。
2. **阅读 `configs/spvcnn/` 与 `mmdet3d/models/backbones/spvcnn_backone.py`**：SPVCNN 在 MinkUNet 的体素分支之外加了一条点分支，观察它如何复用本讲的三后端封装，作为「对比阅读」练习。
3. **阅读 `mmdet3d/models/segmentors/seg3d_tta.py`（`Seg3DTTAModel`）**：结合 u7-l4 的 TTA 讲义，看分割 TTA 为何是「softmax 平均」而非 NMS 合并；配套 tta 配置在 [configs/_base_/datasets/semantickitti.py:L129-L181](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/semantickitti.py#L129-L181)。
4. **顺带一览 `mmdet3d/evaluation/metrics/panoptic_seg_metric.py` 与 `instance_seg_metric.py`**：它们与本讲 `SegMetric` 同构，可自行检验「两段式契约」的普适性。

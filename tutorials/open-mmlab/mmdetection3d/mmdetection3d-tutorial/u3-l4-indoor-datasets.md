# 室内数据集：ScanNet 与 SUN RGB-D

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `ScanNetDataset` 与 `SUNRGBDDataset` 各自 `parse_data_info` / `parse_ann_info` 的输出结构，以及两者与基类 `Det3DDataset` 的分工。
2. 解释 Depth（深度）坐标系为什么是室内数据的默认坐标系，以及「轴对齐 6 维框（ScanNet）」与「带朝向角 7 维框（SUN RGB-D）」两种标注形态的差异。
3. 理解 `pts_instance_mask`（逐点实例标签）与 `pts_semantic_mask`（逐点语义标签）在数据管线中如何被加载、映射、并和点云保持同步。
4. 会用 `tools/misc/browse_dataset.py` 浏览室内数据集，观察一条样本经过管线前后的字段差异。

本讲承接 u3-l1（`Det3DDataset` 基类剖析）与 u3-l3（室外数据集对比）：基类讲「公共骨架」，室外讲「LiDAR 坐标系 + sweeps 聚合」这条线，本讲讲「Depth 坐标系 + 逐点 mask」这条室内线。三讲合起来，你就见过了 mmdet3d 数据集层的全部三种坐标系分支。

## 2. 前置知识

### 2.1 室内场景和室外自动驾驶场景差在哪里

| 维度 | 室外（KITTI / nuScenes） | 室内（ScanNet / SUN RGB-D） |
| --- | --- | --- |
| 传感器 | 旋转 LiDAR，视距可达 70m+ | 深度相机 / 扫描仪重建，单房间几米范围 |
| 默认坐标系 | LIDAR（x 前 y 左 z 上） | DEPTH（y 前 x 右 z 上） |
| 目标朝向 | 车、行人有明确朝向，yaw 重要 | 家具常常贴墙、正交摆放，很多任务不回归 yaw |
| 点数规模 | 单帧 ~10 万点 | ScanNet 单场景常十几万点，训练时采样到 4 万点 |
| 额外标注 | 速度向量、多帧 sweeps | 逐点实例 mask、逐点语义 mask |
| 评估指标 | mAP（IoU 0.5/0.7）、NDS | mAP@0.25 / mAP@0.5 |

「逐点 mask」是室内数据最独特的资产：它给点云中**每个点**一个标签，长度恒等于点数 \(N\)。`pts_instance_mask[i]` 表示第 i 个点属于哪个物体实例（同一把椅子的点共享同一个实例 id），`pts_semantic_mask[i]` 表示第 i 个点的语义类别（如 chair、wall）。VoteNet 这类投票式检测器，以及后来的 3D 语义/实例分割模型，都依赖这两份标注。

### 2.2 Depth 坐标系与两种框形态回顾

回顾 u2-l1/u2-l2：DEPTH 坐标系 y 轴向前、z 轴向上、x 轴向右，yaw 绕 z 轴，yaw=0 时物体朝 x 正方向。`DepthInstance3DBoxes` 的默认张量是 7 维 `(x, y, z, dx, dy, dz, yaw)`，默认锚点是**底面中心** `(0.5, 0.5, 0)`。

室内标注有两种形态：

- **轴对齐框（axis-aligned）**：只用 6 维 `(x, y, z, dx, dy, dz)`，yaw 恒为 0，即物体的边永远和坐标轴平行。ScanNet 的家具框就是这种。
- **带朝向框（with yaw）**：完整 7 维，物体可以在水平面内旋转任意角度。SUN RGB-D 的标注带朝向角。

`BaseInstance3DBoxes.__init__` 对 6 维输入有自动处理：补一列假 yaw 并强制 `with_yaw=False`（见 [mmdet3d/structures/bbox_3d/base_box3d.py:L69-L74](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/base_box3d.py#L69-L74)）；`origin` 参数则负责锚点换算（L77-L80）。

### 2.3 axis_align_matrix：ScanNet 的房间对齐矩阵

ScanNet 原始扫描的房间朝向是任意的。官方为每个场景提供一个 4×4 矩阵 `axis_align_matrix`，作用是把原始坐标系「掰正」到与重力方向和房间主轴对齐。点 p 的对齐操作是刚体变换：

\[ p_{\text{aligned}} = R\,p + t, \qquad A = \left[\,R \mid t\,\right] \in \mathbb{R}^{4\times 4} \]

关键事实：ScanNet 检测标注里的框**已经是变换后的「对齐框」**（converter 同时保存 aligned 和 unaligned 两套，见下文），但点云文件存的是原始坐标，所以管线里需要一步 `GlobalAlignment` 把点也搬到对齐坐标系，点和框才能对上。这一点是读 ScanNet 管线时最容易困惑的地方，记住口诀：**框天生对齐，点需要手动掰**。

### 2.4 与 u3-l1 的衔接

u3-l1 已建立的数据流心智模型在本讲完全适用：

```text
info pkl 单条记录（v2 三段式：lidar_points / images / instances）
        │  parse_data_info（拼绝对路径、注入元信息）
        ▼
data dict（含 ann_info：裸 numpy + 实例列表）
        │  prepare_data：深拷贝 + 注入 box_type_3d/box_mode_3d + 管线
        ▼
dict(inputs=..., data_samples=Det3DDataSample)
```

室外三讲里「裸数组 → 3D 框对象」的包装发生在数据集类的 `parse_ann_info`（KITTI 转雷达系、nuScenes 拼速度），本讲的室内两个类同样如此，只是包装目标换成 `DepthInstance3DBoxes`。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [mmdet3d/datasets/scannet_dataset.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/scannet_dataset.py) | ScanNet 检测数据集类（+ 分割/实例分割两个兄弟类） |
| [mmdet3d/datasets/sunrgbd_dataset.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/sunrgbd_dataset.py) | SUN RGB-D 检测数据集类 |
| [mmdet3d/datasets/det3d_dataset.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/det3d_dataset.py) | 基类（u3-l1 已精读，本讲只复用其结论） |
| [configs/_base_/datasets/scannet-3d.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/scannet-3d.py) | ScanNet 检测的数据集四件套（管线是本讲主线） |
| [configs/_base_/datasets/sunrgbd-3d.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/sunrgbd-3d.py) | SUN RGB-D 数据集四件套（用于对照） |
| [mmdet3d/datasets/transforms/loading.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py) | `LoadAnnotations3D`（双 mask 加载）、`PointSegClassMapping` |
| [mmdet3d/datasets/transforms/transforms_3d.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py) | `GlobalAlignment`、`PointSample`（mask 同步） |
| [tools/dataset_converters/scannet_data_utils.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/scannet_data_utils.py) / [update_infos_to_v2.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/update_infos_to_v2.py) | info pkl 的来源（v1 生成 + v2 标准化） |
| [tools/dataset_converters/sunrgbd_data_utils.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/sunrgbd_data_utils.py) | SUN RGB-D 原始标注 → 7 维深度框 |
| [mmdet3d/evaluation/metrics/indoor_metric.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/indoor_metric.py) | 室内 mAP 评估 |
| [tools/misc/browse_dataset.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/misc/browse_dataset.py) | 综合实践用浏览工具 |
| [demo/data/sunrgbd/sunrgbd_000017_infos.pkl](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/demo/data/sunrgbd/sunrgbd_000017_infos.pkl) | 仓库自带的真实 SUN RGB-D 单帧 info（零下载实践素材） |

## 4. 核心概念与源码讲解

### 4.1 ScanNetDataset：轴对齐深度框与双 mask 标注

#### 4.1.1 概念说明

`ScanNetDataset` 是 ScanNet **检测任务**的数据集类，1201 个重建室内场景、18 个家具类别。它解决三件事：

1. **约定元信息**：18 个检测类别，以及「分割标注里的合法 nyu 类别 id」——这是把逐点语义标签从原始 40 类 nyu id 压缩到 18 类的依据。
2. **注入室内特有字段**：`axis_align_matrix`、两个 mask 的相对路径、`seg_label_mapping`。
3. **把 6 维轴对齐框包装成 `DepthInstance3DBoxes`**。

它不做的事同样重要：读 pkl、遍历数据、跑管线、过滤空真值，全部继承自 `Det3DDataset`/mmengine `BaseDataset`（u3-l1 已讲）。

#### 4.1.2 核心流程

info pkl 中一条 ScanNet 记录（v2 格式）的字段：

```text
{
  'lidar_points': {'lidar_path': 'scene0000_00.bin', 'num_pts_feats': 6},
  'pts_instance_mask_path': 'scene0000_00.bin',   # instance_mask/ 目录下
  'pts_semantic_mask_path': 'scene0000_00.bin',   # semantic_mask/ 目录下
  'axis_align_matrix': [[...4x4...]],
  'instances': [ {'bbox_3d': [x, y, z, dx, dy, dz],   # 6 维，几何中心为锚点，已对齐
                  'bbox_label_3d': 2}, ... ]
}
```

`parse_data_info` 的加工步骤：

1. 从 info 中取（或补单位阵）`axis_align_matrix`；
2. 把两个 mask 相对路径拼上 `data_prefix`（`instance_mask/`、`semantic_mask/`）；
3. 调 `super().parse_data_info()`：基类负责拼 `lidar_points.lidar_path`、生成 `ann_info`/`eval_ann_info`；
4. 把 `seg_label_mapping` 塞进 info，供管线里的 `PointSegClassMapping` 使用。

`parse_ann_info` 的加工步骤：

1. 调基类版本：把 `instances` 列表按字段拼成 numpy 数组、应用 `label_mapping`（子集训练时未选类别 → -1）；
2. 空 GT 时补 `(0, 6)` 的占位数组；
3. 用 `origin=(0.5, 0.5, 0.5)`（几何中心）构造 `DepthInstance3DBoxes(with_yaw=False)`，构造器内部换算到底面中心锚点，6 维输入自动补假 yaw。

#### 4.1.3 源码精读

**元信息与类别映射表。** [mmdet3d/datasets/scannet_dataset.py:L51-L67](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/scannet_dataset.py#L51-L67) 定义 18 个检测类和 `seg_valid_class_ids`（原始 nyu id 中对应这 18 类的 id 列表，注意它不含 1=wall、2=floor）。构造函数里据此生成 `seg_label_mapping`：

[mmdet3d/datasets/scannet_dataset.py:L84-L92](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/scannet_dataset.py#L84-L92)

```python
seg_max_cat_id = len(self.METAINFO['seg_all_class_ids'])   # 40
seg_valid_cat_ids = self.METAINFO['seg_valid_class_ids']   # 18 个合法 id
neg_label = len(seg_valid_cat_ids)                         # 18 作为 ignore
seg_label_mapping = np.ones(seg_max_cat_id + 1, dtype=np.int64) * neg_label
for cls_idx, cat_id in enumerate(seg_valid_cat_ids):
    seg_label_mapping[cat_id] = cls_idx
```

这段代码构造了一个长度 41 的查找表：合法 id 映射到 `0~17`，其余（含 wall/floor/未标注 0）全部映射到 `18`（ignore）。它最终被放进 `self.metainfo['seg_label_mapping']`（L106），随 `parse_data_info` 进入每条样本。

**轴对齐矩阵的容错取值。** [mmdet3d/datasets/scannet_dataset.py:L111-L127](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/scannet_dataset.py#L111-L127) 的 `_get_axis_align_matrix`：info 里有就取出来，没有就警告并返回单位阵——这保证了用旧脚本生成的 info 依然能跑（只是没有对齐效果）。

**parse_data_info。** [mmdet3d/datasets/scannet_dataset.py:L129-L154](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/scannet_dataset.py#L129-L154)：先注入 `axis_align_matrix`（L142）、拼两个 mask 路径（L143-L148），再调 `super().parse_data_info(info)`（L150）把「拼 lidar 路径 + 生成 ann_info」交回基类，最后注入 `seg_label_mapping`（L153）。注意这个「先做自己的事、再调 super」的顺序和 SUN RGB-D 的写法不同（见 4.2）。

**parse_ann_info：6 维框的包装。** [mmdet3d/datasets/scannet_dataset.py:L156-L179](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/scannet_dataset.py#L156-L179)：

```python
ann_info = super().parse_ann_info(info)
if ann_info is None:                       # 空 GT
    ann_info['gt_bboxes_3d'] = np.zeros((0, 6), dtype=np.float32)
    ann_info['gt_labels_3d'] = np.zeros((0, ), dtype=np.int64)
ann_info['gt_bboxes_3d'] = DepthInstance3DBoxes(
    ann_info['gt_bboxes_3d'],
    box_dim=ann_info['gt_bboxes_3d'].shape[-1],   # 6
    with_yaw=False,                                # 轴对齐
    origin=(0.5, 0.5, 0.5)).convert_to(self.box_mode_3d)
```

`box_dim` 用 `shape[-1]` 动态取，是因为历史版本的 info 可能存 7 维；`with_yaw=False` 明确声明 ScanNet 框无朝向。`origin=(0.5, 0.5, 0.5)` 表示 info 中存的是**几何中心**，而 `DepthInstance3DBoxes` 内部锚点是底面中心 `(0.5, 0.5, 0)`，构造器按

\[ c_{\text{bottom}} = c_{\text{center}} + (d_x, d_y, d_z) \cdot \big((0.5,0.5,0) - (0.5,0.5,0.5)\big) \]

完成换算（实现见 [mmdet3d/structures/bbox_3d/base_box3d.py:L77-L80](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/base_box3d.py#L77-L80)），即 z 减去半个高度。`convert_to(self.box_mode_3d)` 把框转换到构造数据集时 `box_type_3d='Depth'` 对应的模式（ScanNet 场景下就是 DEPTH→DEPTH 的恒等转换，但这行保证了把 `box_type_3d` 改成 `'LiDAR'` 时整条链路依然正确）。

**info 从哪来（可选深读）。** converter 侧 [tools/dataset_converters/scannet_data_utils.py:L105-L194](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/scannet_data_utils.py#L105-L194) 的 `process_single_scene` 把 `*_vert.npy` 转成 6 维 `.bin` 点云（L110-L116），把实例/语义标签转成 int64 的 `.bin` mask（L132-L160），并**同时保存 aligned 与 unaligned 两套框**（L165-L184）和 `axis_align_matrix`（L191-L192）。随后 [tools/dataset_converters/update_infos_to_v2.py:L604-L678](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/update_infos_to_v2.py#L604-L678) 把 v1 升级为 v2 时，**只保留 aligned 框**作为 `bbox_3d`（L647-L655），mask 路径与 `axis_align_matrix` 分别在 L626-L639 落位——这正是 2.3 节「框天生对齐」的代码出处。

**下游呼应。** ScanNet 框无 yaw 的约定一直贯通到模型配置：[configs/votenet/votenet_8xb8_scannet-3d.py:L10-L14](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/votenet/votenet_8xb8_scannet-3d.py#L10-L14) 中 VoteNet 的 bbox coder 配了 `num_dir_bins=1, with_rot=False`——方向离散化为 1 个 bin，即不回归朝向。

#### 4.1.4 代码实践

**实践目标**：不下载任何数据，用伪数据亲手复现 `parse_ann_info` 中「6 维数组 → DepthInstance3DBoxes」的包装过程，观察假 yaw 与锚点换算。

**操作步骤**（需要已按 u1-l2 装好 mmdet3d 环境）：

```python
# 示例代码：scannet_box_wrap.py（自己创建，非仓库文件）
import numpy as np
import torch
from mmdet3d.structures import DepthInstance3DBoxes

# 仿照 info pkl 中的 instances：几何中心 + 尺寸，6 维，无 yaw
raw = np.array([[1.0, 2.0, 0.8, 0.7, 0.7, 1.4],    # 一把"椅子"
                [3.5, 1.2, 0.5, 1.8, 0.9, 2.0]], dtype=np.float32)

box = DepthInstance3DBoxes(raw, box_dim=raw.shape[-1],
                           with_yaw=False, origin=(0.5, 0.5, 0.5))
print(box.tensor)          # (2, 7)
print('bottom_center:', box.bottom_center)
print('gravity_center:', box.gravity_center)
print('with_yaw:', box.with_yaw)
```

**需要观察的现象**：

1. `box.tensor` 形状为 `(2, 7)`，第 7 列（yaw）被补成 0；
2. `bottom_center` 的 z 比输入中心的 z 小 `dz/2`（第一行：\(0.8 - 0.7 = 0.1\)），x、y 不变；
3. `with_yaw` 为 `False`。

**预期结果**：第一条框的 `bottom_center` 约为 `[1.0, 2.0, 0.1]`（浮点误差内）。以上推断基于 [mmdet3d/structures/bbox_3d/base_box3d.py:L69-L80](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/base_box3d.py#L69-L80) 的实现逻辑，具体打印数值待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `ScanNetDataset` 构造参数 `box_type_3d` 改成 `'LiDAR'`，`parse_ann_info` 里会发生什么？训练还能正常进行吗？

**答案**：`super().__init__` 中 `get_box_type('LiDAR')` 会把 `self.box_type_3d/box_mode_3d` 设为 `LiDARInstance3DBoxes` 与 `Box3DMode.LIDAR`，于是 `parse_ann_info` 里的 `.convert_to(self.box_mode_3d)` 会把深度框真正转换成 LiDAR 系框（坐标与尺寸按 u2-l1 的规则换轴、yaw 变号）。数据集层面可以运行，但点云仍以 `coord_type='DEPTH'` 加载，点和框坐标系不一致，需要同步修改 `LoadPointsFromFile` 的 `coord_type`，否则训练/评估都会错位。

**练习 2**：`seg_label_mapping[1]`（nyu id 1，wall）等于几？为什么 wall 不在检测的 18 类里还要专门处理？

**答案**：等于 18（`neg_label`，即 ignore）。`seg_label_mapping` 是为「逐点语义标签」服务的：ScanNet 原始语义标注含 40 个 nyu 类，而检测只关心 18 个家具类。点级 mask 中大量 wall/floor 点必须映射到一个统一的 ignore 类，后续 `PointSegClassMapping`、分割损失和评估才能跳过它们（u8-l1 的 `ScanNetSegDataset` 则是另一套 20 类的 `seg_valid_class_ids`，含 wall/floor，见 [mmdet3d/datasets/scannet_dataset.py:L215-L247](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/scannet_dataset.py#L215-L247)）。

**练习 3**：`ScanNetDataset.parse_data_info` 为什么在调 `super().parse_data_info()` **之前**先把 mask 路径拼好？

**答案**：因为基类的 `parse_data_info` 末尾会调 `parse_ann_info` 生成 `ann_info`/`eval_ann_info`，而室内样本的加工链中，路径拼接必须在数据真正被管线消费前全部完成；这里更直接的原因是保持 info dict 的完整性——mask 路径属于「帧级字段」，与 `axis_align_matrix` 一样是 ScanNet 特有的、基类不认识的字段，子类负责在交给基类前注入，避免基类逻辑感知室内细节。（对比：SUN RGB-D 干脆完全重写了 `parse_data_info`，见 4.2。）

### 4.2 SUNRGBDDataset：带朝向角的深度框与图像元信息

#### 4.2.1 概念说明

SUN RGB-D 是 335 张室内 RGB-D 图像组成的小型数据集（单帧深度图反投影成点云，而非 ScanNet 的整房间重建），10 个家具类别。它与 ScanNet 的三点本质差异：

1. **框带朝向**：7 维 `(x, y, z, dx, dy, dz, yaw)`；
2. **天然有图像**：每帧有对应 RGB 图像和 `depth2img` 投影矩阵，是多模态室内算法（如 ImVoxelNet、ImVoteNet）的数据基础；
3. **没有逐点 mask、没有 axis_align_matrix**：单帧深度图不存在房间级对齐问题，管线因此干净很多。

#### 4.2.2 核心流程

info 单条记录（v2 格式，可直接看仓库自带的 `demo/data/sunrgbd/sunrgbd_000017_infos.pkl`）：

```text
{
  'lidar_points': {'lidar_path': '000017.bin', 'num_pts_feats': 6},
  'images': {'CAM0': {'img_path': ..., 'height': ..., 'width': ...,
                      'depth2img': 3x3 矩阵}},
  'instances': [ {'bbox_3d': [x, y, z, dx, dy, dz, yaw],  # 7 维，几何中心锚点
                  'bbox': [x1, y1, x2, y2],                # 2D 框
                  'bbox_label_3d': 0, 'bbox_label': 0}, ... ]
}
```

`parse_data_info` 的加工步骤：

1. 若 `use_lidar`：拼 `lidar_points.lidar_path`；
2. 若 `use_camera`：为每个相机拼 `img_path`；若有 `default_cam_key`（默认 `'CAM0'`），把该相机的 `img_path` 提升为 `info['img_path']`，把 `depth2img` 提升为 `info['depth2img']`；
3. 训练态生成 `ann_info`，测试态按 `load_eval_anns` 生成 `eval_ann_info`。

`parse_ann_info`：基类拼数组 → 空 GT 补 `(0, 6)` → `DepthInstance3DBoxes(gt_bboxes_3d, origin=(0.5, 0.5, 0.5))`，**不传 `box_dim` / `with_yaw`**，即用默认值 7 维、`with_yaw=True`。

#### 4.2.3 源码精读

**类别元信息。** [mmdet3d/datasets/sunrgbd_dataset.py:L48-L55](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/sunrgbd_dataset.py#L48-L55)：10 类，bed/table/sofa/…。构造参数默认 `modality=dict(use_camera=True, use_lidar=True)`、`default_cam_key='CAM0'`（L57-L69），比 ScanNet 更「视觉友好」。

**parse_data_info：完整重写而非增量覆盖。** [mmdet3d/datasets/sunrgbd_dataset.py:L86-L125](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/sunrgbd_dataset.py#L86-L125)。LiDAR 分支（L101-L105）与基类 [mmdet3d/datasets/det3d_dataset.py:L278-L295](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/det3d_dataset.py#L278-L295) 相同但去掉了 sweeps 处理；相机分支的关键在 L112-L117：

```python
if self.default_cam_key is not None:
    info['img_path'] = info['images'][self.default_cam_key]['img_path']
    info['depth2img'] = np.array(
        info['images'][self.default_cam_key]['depth2img'], dtype=np.float32)
```

`depth2img` 是把深度坐标系 3D 点投影到图像平面的 3×3 矩阵。它的来历在 converter：[tools/dataset_converters/update_infos_to_v2.py:L701-L707](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/update_infos_to_v2.py#L701-L707) 按 `Coord3DMode.convert_point` 的约定计算 `depth2img = K @ (M @ Rt^T)`（K 为内参，M 为 DEPTH↔CAMERA 的固定方向修正矩阵）。可视化器画「3D 框投影回图像」时用的就是它。

**parse_ann_info：7 维带 yaw 框。** [mmdet3d/datasets/sunrgbd_dataset.py:L127-L147](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/sunrgbd_dataset.py#L127-L147)，包装行为与 ScanNet 唯一不同的一行：

```python
ann_info['gt_bboxes_3d'] = DepthInstance3DBoxes(
    ann_info['gt_bboxes_3d'], origin=(0.5, 0.5, 0.5)).convert_to(self.box_mode_3d)
```

不传 `box_dim`/`with_yaw`，默认 7 维、带 yaw。7 维的出处是 converter 的 [tools/dataset_converters/sunrgbd_data_utils.py:L46-L60](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/sunrgbd_data_utils.py#L46-L60)：`box3d = concat(centroid(3), size(3) /* 注意 ×2 换算 */, heading_angle(1))`，其中朝向角由标注中的方向向量算 `arctan2` 得到（L57-L58）。

**下游呼应。** SUN RGB-D 版 VoteNet 的 bbox coder 是 `num_dir_bins=12, with_rot=True`（[configs/votenet/votenet_8xb16_sunrgbd-3d.py:L11-L14](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/votenet/votenet_8xb16_sunrgbd-3d.py#L11-L14)），把朝向离散成 12 个 30° 的 bin 分类 + bin 内回归——与数据集的带 yaw 标注一一对应。

**两个数据集对照表**（把 4.1、4.2 收拢）：

| 维度 | ScanNetDataset | SUNRGBDDataset |
| --- | --- | --- |
| 类别数 | 18 | 10 |
| `bbox_3d` 维度 | 6（无 yaw） | 7（含 yaw） |
| `with_yaw` | 显式 `False` | 默认 `True` |
| 逐点 mask | 有（instance + semantic） | 无 |
| `axis_align_matrix` | 有，管线需 `GlobalAlignment` | 无 |
| `default_cam_key` | 无相机分支 | `'CAM0'`，提升 `depth2img` |
| 默认采样点数 | 40000 | 20000 |
| 评估 | `IndoorMetric` | `IndoorMetric` |

#### 4.2.4 代码实践

**实践目标**：仓库自带的 `demo/data/sunrgbd/sunrgbd_000017_infos.pkl` 是一条**真实**的 SUN RGB-D v2 info，零下载直接剖析它的结构。

**操作步骤**：

```python
# 示例代码：inspect_demo_info.py（自己创建，非仓库文件）
import pickle

with open('demo/data/sunrgbd/sunrgbd_000017_infos.pkl', 'rb') as f:
    data = pickle.load(f)

info = data['data_list'][0]           # v2 顶层：metainfo + data_list
print(info.keys())
print('lidar_points:', info['lidar_points'])
print('CAM0 keys:', info['images']['CAM0'].keys())
print('depth2img:\n', info['images']['CAM0']['depth2img'])
print('num instances:', len(info['instances']))
for ins in info['instances']:
    print(len(ins['bbox_3d']), ins['bbox_label_3d'], ins['bbox_3d'])
```

**需要观察的现象**：

1. 单条 info 顶层就是 4.2.2 节画的三段式：`lidar_points` / `images` / `instances`；
2. 每个 `bbox_3d` 长度为 **7**（对比 ScanNet 的 6）；
3. `instances` 里同时保留了 2D 框 `bbox` 与 3D 框 `bbox_3d`。

**预期结果**：打印出若干长度为 7 的框和 `depth2img` 的 3×3 数值矩阵；`data_list` 长度为 1。消费这份 pkl 的真实代码可对照 [mmdet3d/apis/inference.py:L233-L267](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inference.py#L233-L267)（多模态 demo 正是取 `data_list[index]['images'][cam_type]['depth2img']` 喂给管线）。具体类别与框数值待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `SUNRGBDDataset.parse_data_info` 里没有 `axis_align_matrix` 相关代码，而 ScanNet 有？

**答案**：ScanNet 是整房间重建，原始扫描坐标系的房间主轴方向任意，需要 4×4 对齐矩阵把点云掰到和「对齐标注框」同一坐标系；SUN RGB-D 每个样本是单帧深度图反投影的点云，标注框和点云天然在同一相机深度坐标系下，不存在房间级对齐问题。

**练习 2**：SUN RGB-D 的 `parse_ann_info` 用了默认 `box_dim=7`。如果某份旧版 info 里 `bbox_3d` 只有 6 维，会发生什么？

**答案**：`BaseInstance3DBoxes.__init__`（[mmdet3d/structures/bbox_3d/base_box3d.py:L69-L74](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/base_box3d.py#L69-L74)）检测到 `box_dim + 1 == 7` 时会自动补一列 0 假 yaw，并**强制 `with_yaw=False`**——即 6 维输入会静默退化为轴对齐框。这也解释了为什么 ScanNet 要显式传 `with_yaw=False`：让语义更清楚。

**练习 3**：`depth2img` 和 KITTI 的 `lidar2img` 在职责上的共同点是什么？为什么室内不用 `lidar2cam` 那套链路？

**答案**：两者都是「3D 点 → 图像像素」的投影矩阵，用于多模态可视化与图像特征反投影。区别在于出发点坐标系：KITTI 是 LiDAR 坐标系，需要外参 `Tr_velo_to_cam` 和 `R0_rect`；室内深度点云本就直接由深度图反投影而来，和图像之间只差一个内参与方向修正（u2-l1 讲过 DEPTH 与 CAMERA 坐标系的固定换轴矩阵），所以一个 `K @ rt_mat` 就够，不需要相机外参链。

### 4.3 室内数据管线：双 mask 加载、GlobalAlignment 与采样同步

#### 4.3.1 概念说明

室内检测管线（以 [configs/_base_/datasets/scannet-3d.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/scannet-3d.py) 为准）比室外多了三个专属角色：

- **`LoadAnnotations3D(with_mask_3d=True, with_seg_3d=True)`**：把两份逐点 mask 从磁盘读进 data dict；
- **`GlobalAlignment`**：用 `axis_align_matrix` 对齐点云（框已对齐）；
- **`PointSegClassMapping`**：把语义 mask 的原始 nyu id 重映射为 0~18。

此外有个贯穿全程的不变量：**`pts_instance_mask` / `pts_semantic_mask` 的长度必须始终等于点数 N**。凡是「重排或筛选点」的 transform 都必须同步索引 mask；凡是「原地坐标变换」（flip、rotate、scale）则不需要动 mask，因为点的顺序没变。

#### 4.3.2 核心流程

`scannet-3d.py` 的 train_pipeline（共 8 个 transform）：

| 序 | transform | 作用 | 是否动 mask |
| --- | --- | --- | --- |
| 1 | `LoadPointsFromFile(coord_type='DEPTH', shift_height=True, use_dim=[0,1,2])` | 读 6 维 `.bin`，取 xyz，加「离地高度」列 → `DepthPoints (N,4)` | 产生 points（尚无 mask） |
| 2 | `LoadAnnotations3D(with_bbox_3d, with_label_3d, with_mask_3d, with_seg_3d)` | 把 `ann_info` 提升为 `gt_*` 键，并读两份 mask | **加载** mask |
| 3 | `GlobalAlignment(rotation_axis=2)` | 用 `axis_align_matrix` 旋转+平移点云 | 不动（顺序未变） |
| 4 | `PointSegClassMapping` | nyu id → 0~18 | **改写** mask 值 |
| 5 | `PointSample(num_points=40000)` | 随机采样/补采到 4 万点 | **同步索引** mask |
| 6 | `RandomFlip3D` | 水平/垂直翻转（各 50%） | 不动 |
| 7 | `GlobalRotScaleTrans`（±5°、无缩放） | 小幅旋转平移 | 不动 |
| 8 | `Pack3DDetInputs` | 打包成 Det3DDataSample | 收集 mask 进 data_samples |

test_pipeline 则没有 `LoadAnnotations3D`/mask/增强，只有「加载 → 对齐 → MultiScaleFlipAug3D 包装的恒等变换 + 采样 → Pack」。

伪代码总结 mask 的生命周期：

```text
磁盘 int64 bin ──LoadAnnotations3D──▶ results['pts_instance_mask'] (N0,)
                    │                   results['pts_semantic_mask'] (N0,)
                    │  PointSegClassMapping: sem = mapping[sem]
PointSample(choices) ──▶ points = points[choices]
                        ins  = ins[choices]      # (40000,)
                        sem  = sem[choices]      # (40000,)
（之后 flip/rotate 原地变换，mask 不变）
Pack3DDetInputs ──▶ data_samples.gt_pts_seg / 或作为 keys 显式打包
```

#### 4.3.3 源码精读

**管线配置本体。** [configs/_base_/datasets/scannet-3d.py:L26-L60](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/scannet-3d.py#L26-L60) 是 train_pipeline；[L91-L108](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/scannet-3d.py#L91-L108) 的 train_dataloader 用 `RepeatDataset(times=5)` 包装、`filter_empty_gt=False`、`box_type_3d='Depth'`（L107 注释明确写着：kitti/nuscenes 用 LiDAR，sunrgbd/scannet 用 Depth）；[L136](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/scannet-3d.py#L136) 挂 `IndoorMetric`。

**shift_height：VoteNet 系的「第 4 维」。** [mmdet3d/datasets/transforms/loading.py:L659-L665](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L659-L665)：

```python
floor_height = np.percentile(points[:, 2], 0.99)
height = points[:, 2] - floor_height
points = np.concatenate([points[:, :3], np.expand_dims(height, 1), ...], 1)
```

即 \(h_i = z_i - \text{percentile}_{99\%}(z)\)，把绝对高程换成「离地高度」并作为第 4 列（`attribute_dims=dict(height=3)`）。所以 ScanNet/SUN RGB-D 训练时点云是 `(N, 4)`，这就是 VoteNet 输入 `in_channels=4` 的来源。

**双 mask 的加载。** [mmdet3d/datasets/transforms/loading.py:L1046-L1071](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L1046-L1071) 的 `LoadAnnotations3D.transform` 按开关分派，mask 两个分支在 [L920-L944](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L920-L944)（`_load_masks_3d`：按 `pts_instance_mask_path` 读 int64 一维数组）和 [L946-L978](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L946-L978)（`_load_semantic_seg_3d`）。两处都有一行值得注意：如果 results 里有 `eval_ann_info`，mask 会被**同时写入 eval 分支**——测试态的 mask 会随 `eval_ann_info` 交给评估器。

**语义重映射。** [mmdet3d/datasets/transforms/loading.py:L477-L504](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L477-L504) 的 `PointSegClassMapping.transform` 核心只有一行 `label_mapping[pts_semantic_mask]`（numpy 花式索引完成整表映射），同样同步 `eval_ann_info`。

**GlobalAlignment：只转点，不转框。** [mmdet3d/datasets/transforms/transforms_3d.py:L598-L621](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L598-L621)：

```python
axis_align_matrix = results['axis_align_matrix']
rot_mat = axis_align_matrix[:3, :3]
trans_vec = axis_align_matrix[:3, -1]
self._check_rot_mat(rot_mat)      # 校验是绕 rotation_axis 的正交阵
self._rot_points(results, rot_mat)     # points.rotate(rot_mat.T)
self._trans_points(results, trans_vec)
```

注意 L549-L554 的类注释明确说明：这一步**不记录逆向变换**（不像 `GlobalRotScaleTrans` 会存 rot 角度供反变换），因为 ScanNet 评估就在对齐坐标系下进行。`_check_rot_mat`（[L585-L596](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L585-L596)）断言该矩阵行列式为 1 且不混合重力轴（z 轴分量保持 e_z），是对「矩阵只绕 z 转」的防御。

**PointSample：mask 同步索引。** [mmdet3d/datasets/transforms/transforms_3d.py:L1097-L1127](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L1097-L1127)：采样得到 `choices` 后，`points[choices]` 与 `pts_instance_mask[choices]`、`pts_semantic_mask[choices]`（L1116-L1125）用**同一组索引**切片，保证第 i 个点的标签仍是它自己的标签。点数不足 `num_points` 时 `_points_random_sampling` 会自动放回采样（L1073-L1074）。

**对照组：SUN RGB-D 管线。** [configs/_base_/datasets/sunrgbd-3d.py:L23-L46](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/sunrgbd-3d.py#L23-L46)：没有 `LoadAnnotations3D` 的 mask 开关、没有 `GlobalAlignment`、没有 `PointSegClassMapping`；`GlobalRotScaleTrans` 的旋转范围是 ±30°（对比 ScanNet 的 ±5°），`PointSample(20000)` 排在增强**之后**。两份 config 的差异就是两个数据集差异的直接投影。

**轴对齐框旋转时的特殊行为（进阶）。** ScanNet 框 `with_yaw=False`，增强中旋转 5° 后怎么办？看 [mmdet3d/structures/bbox_3d/depth_box3d.py:L121-L133](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/depth_box3d.py#L121-L133)：`with_yaw=False` 分支先算 8 个角点旋转后的外接矩形，再把 `dx/dy` 替换成外接矩形边长——轴对齐框旋转后**变大一点**而不是带角度。这正是 ScanNet 只敢用 ±5° 小角度旋转的原因之一：角度越大，框被外接矩形撑得越肥，标注质量下降。

#### 4.3.4 代码实践

**实践目标**：不依赖数据集，手工复算 `seg_label_mapping` 并验证 `PointSegClassMapping` 的映射语义；再用同样的规则检查一个「假 mask」。

**操作步骤**：

```python
# 示例代码：seg_mapping_check.py（自己创建，非仓库文件）
import numpy as np

# 1) 照抄 ScanNetDataset.__init__ 的三行核心逻辑
seg_all = tuple(range(1, 41))                       # nyu id 1~40
seg_valid = (3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 14,
             16, 24, 28, 33, 34, 36, 39)            # 18 个合法 id
neg = len(seg_valid)
mapping = np.ones(len(seg_all) + 1, dtype=np.int64) * neg
for cls_idx, cat_id in enumerate(seg_valid):
    mapping[cat_id] = cls_idx

# 2) 伪造一帧语义 mask：墙(1)、地板(2)、椅子(5)、未知(40)
sem = np.array([1, 2, 5, 5, 40, 39])
print(mapping[sem])          # 期望 [18 18  2  2 18 17]
```

**需要观察的现象**：`mapping[sem]` 中 nyu id 5（chair）映射为 2（与 [mmdet3d/datasets/scannet_dataset.py:L51-L58](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/scannet_dataset.py#L51-L58) 中 classes 顺序一致），id 1/2/40 全部落到 18。

**预期结果**：输出 `[18 18 2 2 18 17]`。此结果可直接由映射表逻辑推出（确定性 numpy 索引），但仍建议本地跑一遍确认。

**延伸观察（源码阅读型）**：对照 `scannet-3d.py` 与 `sunrgbd-3d.py` 两份 config，回答——SUN RGB-D 为什么可以把 `PointSample` 放在增强之后而 ScanNet 放在增强之前？（提示：与点数规模、`shift_height=True` 下旋转对高度列的影响无关对错，主要是两个数据集点云规模与训练预算的工程取舍；重点是把「顺序不同」这个事实和 mask 同步机制联系起来。）

#### 4.3.5 小练习与答案

**练习 1**：如果自己在管线里新增一个「随机删除 10% 点」的 transform，需要同步处理哪些字段？

**答案**：`points`、`pts_instance_mask`、`pts_semantic_mask` 三者必须用同一个布尔/索引掩码切片，维持「mask 长度 == 点数」的不变量。参考同类实现 [mmdet3d/datasets/transforms/transforms_3d.py:L925-L948](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L925-L948)（`PointsRangeFilter` 的 mask 同步写法）。若该 transform 出现在 `MultiScaleFlipAug3D` 的测试增强里，还要考虑与 `PointSample` 的顺序关系。

**练习 2**：`GlobalAlignment` 为什么不把 3D 框也转一遍？

**答案**：因为 ScanNet info 里的 `bbox_3d` 是 converter 用 `axis_align_matrix` 预先对齐过的（[tools/dataset_converters/scannet_data_utils.py:L165-L184](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/scannet_data_utils.py#L165-L184) 同时保存 aligned/unaligned，v2 转换只保留 aligned），框已经在目标坐标系；若再转一次框就「转过头」了。反过来说，如果未来要用 unaligned 框，就必须同时让框和点都过对齐矩阵。

**练习 3**：ScanNet 的 `GlobalRotScaleTrans` 配置里 `scale_ratio_range=[1.0, 1.0]`，为什么不开放缩放？

**答案**：开放缩放会让物体物理尺寸改变，而 ScanNet 框是轴对齐的，旋转/缩放后取外接矩形只会让框变大（见 4.3.3 最后一段），导致框与点云、框与真实尺寸都不一致，直接影响 mAP@0.25/0.5 的标注质量。室外 KITTI 配置里常见 `[0.95, 1.05]` 的小幅缩放，是因为室外框带 yaw，旋转缩放后仍是「紧」的框。（这是一个基于代码行为的合理推断，配置文件本身未写注释说明动机。）

### 4.4 IndoorMetric：室内评估约定

#### 4.4.1 概念说明

两个室内数据集共用一个评估器 `IndoorMetric`（scannet-3d.py / sunrgbd-3d.py 中 `val_evaluator = dict(type='IndoorMetric')`）。它报告 IoU 阈值 0.25 和 0.5 两个档位下的 mAP。为什么比室外（0.5/0.7）低？室内框完全轴对齐或小角度，尺寸稍偏就同时影响 x/y 两个方向的交集，且家具类别多、样本不均衡，0.25 档是社区沿用的宽松基准（ScanNet 官方基准即 mAP@0.25/0.5）。

#### 4.4.2 核心流程

```text
模型 predict 输出 pred_instances_3d，add_pred_to_datasample 写入 Det3DDataSample
        │  （u2-l4 的数据契约）
测试态 parse_data_info 生成 eval_ann_info（含 gt 框 + 逐点 mask）
        ▼
IndoorMetric.process：收集 (eval_ann_info, pred_3d) 二元组到 self.results
        ▼
IndoorMetric.compute_metrics：调 mmdet3d.evaluation.indoor_eval
        ▼
每类 AP → mAP@0.25 / mAP@0.5
```

#### 4.4.3 源码精读

[mmdet3d/evaluation/metrics/indoor_metric.py:L15-L37](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/indoor_metric.py#L15-L37) 声明默认 `iou_thr=[0.25, 0.5]`。

[mmdet3d/evaluation/metrics/indoor_metric.py:L39-L58](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/indoor_metric.py#L39-L58) `process`：每个 data_sample 取 `pred_instances_3d`（搬到 CPU）与 `eval_ann_info` 配对存储。注意 GT 不像室外那样由 metric 自己读 ann_file，而是**跟着 data_sample 走**——这就是 u3-l1 强调的 `eval_ann_info` 字段的意义。

[mmdet3d/evaluation/metrics/indoor_metric.py:L60-L82](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/indoor_metric.py#L60-L82) `compute_metrics`：从 `dataset_meta` 取 `box_type_3d`（默认 `'depth'`，经 `get_box_type` 解析为 DepthInstance3DBoxes——即 [mmdet3d/structures/bbox_3d/utils.py:L150-L168](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/utils.py#L150-L168) 的查表），再调 `indoor_eval` 计算逐类 AP。IoU 用的是 u2-l2 讲过的 3D IoU 分解（BEV 旋转 IoU × 高度交集）。

#### 4.4.4 代码实践

**实践目标**（源码阅读型）：完整追踪一次「GT 从 info pkl 到 IndoorMetric」的路径，写出每一站的字段名。

**操作步骤**：

1. 从 [mmdet3d/datasets/scannet_dataset.py:L150](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/scannet_dataset.py#L150) 的 `super().parse_data_info(info)` 进入基类，找到 [det3d_dataset.py:L321-L325](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/det3d_dataset.py#L321-L325)：训练态生成 `ann_info`、测试态按 `load_eval_anns` 生成 `eval_ann_info`；
2. 查 `Pack3DDetInputs` 如何把 `eval_ann_info` 塞进 Det3DDataSample（[mmdet3d/datasets/transforms/formating.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/formating.py) 中搜索 `eval_ann_info`）；
3. 查 mask 类 transform（4.3.3 已列）在测试态如何同步改写 `eval_ann_info` 里的 mask；
4. 最后对照 [mmdet3d/evaluation/metrics/indoor_metric.py:L49-L58](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/indoor_metric.py#L49-L58) 确认消费点。

**需要观察的现象**：`eval_ann_info` 是 Det3DDataSample 中唯一一个「裸 dict」字段（u2-l4 结论），它绕过模型的 loss/predict 逻辑直接到达评估器。

**预期结果**：能画出 `instances → ann_info/eval_ann_info → data_samples.eval_ann_info → IndoorMetric.results` 的四级跳转图。此为纯阅读任务，无需运行；结论可与 u3-l3 中「评估不在数据集类内」的结论互相印证（室外 metric 另有 ann_file 通道，室内走 data_sample 通道）。

#### 4.4.5 小练习与答案

**练习 1**：`IndoorMetric` 怎么知道该用 `DepthInstance3DBoxes` 而不是 `LiDARInstance3DBoxes` 来算 IoU？

**答案**：`compute_metrics` 从 `self.dataset_meta.get('box_type_3d', 'depth')` 读字符串再经 `get_box_type` 解析。`dataset_meta` 来自数据集的 `metainfo`（Runner 组装 evaluator 时注入），而 `box_type_3d` 正是我们在 config 里传给数据集的 `box_type_3d='Depth'`——config 的一个字段同时决定了数据集包装、模型输出与评估器三处的框类型。

**练习 2**：为什么室内评估不需要像 KITTI 那样处理 difficulty（easy/moderate/hard）？

**答案**：KITTI 的难度按目标 2D 框像素高度/遮挡/截断分级，源于行车视角的成像特性；室内点云是稠密重建，不存在「远处小目标像素少」的问题，官方基准只按 IoU 阈值分档（0.25/0.5），`IndoorMetric` 因此只有 `iou_thr` 一个关键参数。

## 5. 综合实践

**任务**：用 `tools/misc/browse_dataset.py` 浏览 ScanNet（或 SUN RGB-D）数据集，观察一条样本经过管线前后的字段差异，并回答「`DepthInstance3DBoxes` 是在管线哪一步生成的？」

**步骤**：

1. **准备数据**（二选一）：
   - 完整流程：按 [docs/en/user_guides/dataset_prepare.md](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/user_guides/dataset_prepare.md) 与 [data/scannet/README.md](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/data/scannet/README.md) 下载 ScanNet 并执行 `python tools/create_data.py scannet --root-path ./data/scannet --out-dir ./data/scannet --extra-tag scannet`；
   - 轻量流程：只保留少数几个场景的 info（自己用 pickle 截取 `data_list` 前几条另存一个小 pkl，配合 `--cfg-options` 把 `train_dataloader.dataset.ann_file` 指过去），保证对应 `.bin` 文件存在即可。
2. **浏览**（以 VoteNet 的 ScanNet 配置为例，它继承本讲的 scannet-3d.py）：

   ```bash
   python tools/misc/browse_dataset.py configs/votenet/votenet_8xb8_scannet-3d.py \
       --task lidar_det --output-dir browse_scannet --not-show
   ```

   加 `--aug` 可看训练增强后的效果（[tools/misc/browse_dataset.py:L75-L88](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/misc/browse_dataset.py#L75-L88)：非 `--aug` 模式借 test_pipeline，再把 train_pipeline 里的 `LoadAnnotations3D` 与 `Pack3DDetInputs` 插进来，所以 GT 框也能画）。
3. **对比管线前后字段**（在 Python 里做，比看图更能回答问题）：

   ```python
   # 示例代码：before_after.py（自己创建，非仓库文件）
   from mmengine.config import Config
   from mmengine.registry import init_default_scope
   from mmdet3d.registry import DATASETS
   import torch

   cfg = Config.fromfile('configs/votenet/votenet_8xb8_scannet-3d.py')
   init_default_scope(cfg.default_scope)
   ds_cfg = cfg.train_dataloader.dataset.dataset       # 剥掉 RepeatDataset
   ds_cfg['test_mode'] = False
   ds = DATASETS.build(ds_cfg, default_args=dict(filter_empty_gt=False))

   before = ds.get_data_info(0)                        # 管线前（parse_data_info 产物）
   print('before keys:', sorted(before.keys()))
   print('ann gt_bboxes_3d type:', type(before['ann_info']['gt_bboxes_3d']))

   after = ds[0]                                       # 管线后（__getitem__ → prepare_data → pipeline）
   print('after keys:', sorted(after.keys()))
   print('inputs points:', after['inputs'].shape)      # 期待 torch.Tensor (40000, 4)
   print('gt bboxes:', after['data_samples'].gt_instances_3d.bboxes_3d)
   ```

4. **记录答案**：`before['ann_info']['gt_bboxes_3d']` 已经是 `DepthInstance3DBoxes`——它在 `ScanNetDataset.parse_ann_info`（数据集侧，[mmdet3d/datasets/scannet_dataset.py:L173-L177](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/scannet_dataset.py#L173-L177)）就已生成，**先于任何管线 transform**；管线里的 `GlobalAlignment`/`RandomFlip3D`/`GlobalRotScaleTrans` 只是继续对这个已存在的框对象做同步变换。

**预期结果**（关键观察点）：

- `before`：含 `lidar_path`、两个 mask 绝对路径、`axis_align_matrix`、`seg_label_mapping`、`ann_info`（其中 `gt_bboxes_3d` 是 DepthInstance3DBoxes、`gt_labels_3d` 是 int64 数组）；
- `after`：只有 `inputs`（(40000, 4) 的 torch 张量）与 `data_samples`（Det3DDataSample，`gt_instances_3d.bboxes_3d` 仍是 DepthInstance3DBoxes，另含逐点 mask 字段）；
- 图片输出在 `browse_scannet/` 下逐帧保存。

若本地暂无数据，可退化为第 3 步的「demo 版」：用 4.2.4 的 demo pkl + `demo/data/sunrgbd/000017.bin` 手工拼一个最小 data dict 过 `LoadPointsFromFile`/`Pack3DDetInputs`，同样能观察到 `DepthInstance3DBoxes` 的生成位置。完整跑通依赖本地数据，具体输出待本地验证。

## 6. 本讲小结

- ScanNet 与 SUN RGB-D 是 mmdet3d 两大室内检测数据集：**同一 Depth 坐标系、两种框形态**——ScanNet 6 维轴对齐框（`with_yaw=False`），SUN RGB-D 7 维带朝向框（默认 `with_yaw=True`），差异一路传导到 VoteNet bbox coder 的 `with_rot` 配置。
- 两个类都极薄：`parse_data_info` 只注入室内特有字段（`axis_align_matrix`、mask 路径 / `depth2img`），`parse_ann_info` 只做「裸数组 → `DepthInstance3DBoxes(origin=(0.5,0.5,0.5))`」的包装，其余全部复用 `Det3DDataset`。
- ScanNet 的关键约定是「**框天生对齐、点需要手动掰**」：converter 存 aligned 框，管线用 `GlobalAlignment` 对齐点云。
- 室内数据独有的逐点 mask（instance/semantic）遵循「**长度恒等于点数**」的不变量：加载靠 `LoadAnnotations3D`，重映射靠 `seg_label_mapping` + `PointSegClassMapping`，重排/筛选类 transform（如 `PointSample`）必须同步索引 mask。
- 评估统一走 `IndoorMetric`（mAP@0.25/0.5），GT 通过 `eval_ann_info` 随 Det3DDataSample 流转，而非像室外那样由 metric 读 ann_file。

## 7. 下一步学习建议

- **u3-l5（数据加载 Transforms）**：本讲只精读了 `LoadAnnotations3D` 与 mask 相关行为，`LoadPointsFromFile` 的 `load_dim/use_dim` 语义、多帧聚合等留给下一讲系统展开。
- **u3-l7（GT 数据库采样）**：室外专属的 `ObjectSample` 是「往场景里粘贴目标」，与本讲的「整场景刚体增强」互补，可对比阅读。
- **u6-l2（VoteNet）**：本讲末尾反复出现的 `shift_height` 第 4 维、`with_rot=False`、逐点 mask，将在 VoteNet 的 seed/vote/分组中变成真实的消费者，届时回看 `scannet-3d.py` 管线会有「原来是给这里用的」之感。
- **u8-l1（3D 语义分割）**：`ScanNetSegDataset`/`Seg3DDataset` 与 `scene_idxs` 重采样机制，是 `seg_label_mapping` 的另一条主线。

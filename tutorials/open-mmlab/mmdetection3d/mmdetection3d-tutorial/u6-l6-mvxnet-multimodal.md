# 多模态融合检测：MVXNet 与 mmdet 组件复用

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `MVXTwoStageDetector` 的「积木式」构造方式：点云分支（`pts_*`）与图像分支（`img_*`）各由哪些可选组件拼成，以及它们如何被 `MODELS.build` 逐个实例化。
2. 解释 `DynamicMVXFasterRCNN`（MVX-Net 的实际注册类）与基类在 `extract_pts_feat` 上的接口差异，理解动态体素化如何改变数据流。
3. **核心中的核心**：追踪图像特征如何被「映射回 voxel」——点级融合发生在 `DynamicVFE` 的最后一层 VFE 与 `PointFusion` 之间，中间要经历「逆 3D 增强 → lidar2img 投影 → 正向 2D 增强」三重坐标变换。
4. 对照 MVXNet 配置，列出哪些模块实际来自 MMDetection（`mmdet.` 前缀），并画出点云分支与图像分支在 `extract_feat` 阶段的唯一汇合点。
5. 理解为什么 MVXNet 配置里没有 `img_rpn_head` / `img_roi_head`——图像分支在这个配置中是纯粹的「特征捐赠者」。

## 2. 前置知识

本讲默认你已读过以下讲义（依赖：u5-l2 SECOND 稀疏编码器、u6-l5 单目 3D 检测），这里只做最小回顾：

- **配置驱动与注册器路由**（u4-l1）：配置里 `dict(type='XXX')` 中的 `type` 字符串经注册器路由到真实类；带 `mmdet.` 前缀表示从 MMDetection 跨库借用（scope 切换），带 `mmdet3d.` 前缀或无前缀表示本项目模块。
- **体素化两条路线**（u4-l3）：`Det3DDataPreprocessor` 可以在 batch 预处理阶段做体素化。**hard 体素化**产出定长三件套 `voxels/coors/num_points`；**dynamic 体素化**只算每个点落在哪个格子（`voxels` 即拼接后的原始点、`coors` 是逐点格坐标），把「聚合」推迟到网络内部。
- **SECOND 系骨干链路**（u5-l1、u5-l2）：体素特征 → `SparseEncoder`（稀疏 UNet）→ BEV 伪图像 → `SECOND` backbone → `SECONDFPN` neck → `Anchor3DHead`。MVXNet 的点云分支完全复用这条链。
- **相机投影与内参**（u6-l5）：单目 3D 检测用 `cam2img`（内参矩阵）把相机系点投影到像素平面；本讲用的是它的推广形式 `lidar2img`——外参与内参相乘得到的 4×4 矩阵，一步把雷达系点投到像素。
- **3D 增强的审计线索**（u3-l6）：`GlobalRotScaleTrans` / `RandomFlip3D` 会把 `pcd_rotation`、`pcd_scale_factor`、`transformation_3d_flow` 等键写进 data dict 的元信息。本讲会看到这些键的**消费者**——融合层靠它们把增强后的点云逆变换回原始坐标系，才能与未增强标定下的图像对齐。
- **MVX-Net 论文背景**：MVX-Net（ICRA 2019）提出两种早期融合方案 PointFusion 与 VoxelFusion，把 RGB 信息注入 VoxelNet 式点云检测器。mmdet3d 实现的是 PointFusion 路线：在 VFE 的**点级特征**上逐点采样图像特征做加法融合，见 [configs/mvxnet/README.md](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/mvxnet/README.md)。

一个术语先行澄清：**点级融合（point-wise fusion）** 指融合发生在「每个点还有独立身份」的阶段（VFE 内部），而不是 BEV 特征图或最终检测框层面。这是 MVXNet 与 BEVFusion 等 BEV 级融合（u8-l4）的本质区别。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [mmdet3d/models/detectors/mvx_two_stage.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/mvx_two_stage.py) | 多模态 VoxelNet 基类：双分支组件装配、`extract_feat`/`loss`/`predict` 主流程、图像分支的 RPN+RoIHead 损失通路 |
| [mmdet3d/models/detectors/mvx_faster_rcnn.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/mvx_faster_rcnn.py) | `MVXFasterRCNN`（薄壳）与 `DynamicMVXFasterRCNN`（重写 `extract_pts_feat` 适配动态体素化） |
| [mmdet3d/models/voxel_encoders/voxel_encoder.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/voxel_encoders/voxel_encoder.py) | `DynamicVFE`：逐点装饰特征、VFE 层堆叠，并在**最后一层**调用融合层注入图像特征 |
| [mmdet3d/models/layers/fusion_layers/point_fusion.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/fusion_layers/point_fusion.py) | `PointFusion` 融合层与 `point_sample` 投影采样函数（本讲坐标变换的核心） |
| [mmdet3d/models/layers/fusion_layers/coord_transform.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/fusion_layers/coord_transform.py) | `apply_3d_transformation`：按 `transformation_3d_flow` 正/逆向重放 3D 增强 |
| [mmdet3d/structures/bbox_3d/utils.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/utils.py) | `points_cam2img`（透视投影）与 `get_proj_mat_by_coord_type`（按坐标系取 lidar2img/depth2img/cam2img） |
| [configs/mvxnet/mvxnet_fpn_dv_second_secfpn_8xb2-80e_kitti-3d-3class.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/mvxnet/mvxnet_fpn_dv_second_secfpn_8xb2-80e_kitti-3d-3class.py) | MVXNet 在 KITTI 上的完整实验描述：动态体素化 + mmdet 图像组件 + PointFusion |

## 4. 核心概念与源码讲解

### 4.1 多模态检测器骨架：MVXTwoStageDetector 的积木式构造

#### 4.1.1 概念说明

`MVXTwoStageDetector` 是 CenterPoint、MVX-Net、TransFusion 等「点云 + 图像」检测器的共同基类。它的设计哲学是**积木化**：不预设固定的网络结构，而是开出一组长前缀的可选组件槽位——`pts_` 前缀属于点云分支，`img_` 前缀属于图像分支——每个槽位传了配置就 build，没传就跳过。检测器本身只负责「接线」：定义数据的流转顺序。

这带来两个好处：其一，配置文件就是结构图，删掉 `img_backbone` 就退化为纯点云检测器；其二，图像分支的组件槽位（backbone/neck/rpn_head/roi_head）与 MMDetection 的 Faster R-CNN 一一对应，因此可以直接填 `mmdet.ResNet`、`mmdet.FPN` 等跨库组件——「TwoStage」与「FasterRCNN」的名字正来自这条图像分支的形状。

#### 4.1.2 核心流程

构造阶段（`__init__`）：

```text
pts_voxel_encoder  ─┐
pts_middle_encoder ─┤        ┌─ img_backbone ─┐
pts_backbone       ─┼─ build ├─ img_neck      │  每个 if 对应一个可选槽位，
pts_fusion_layer   ─┤  MODELS └─ img_rpn_head │  传了 dict 就实例化，
pts_neck           ─┤           img_roi_head  │  不传则该属性根本不存在
pts_bbox_head      ─┘           （注入 train_cfg.pts / test_cfg.pts）
```

前向阶段（`loss` 模式）：

```text
batch_inputs_dict{voxels, imgs, points}
        │
        ├─→ extract_img_feat ─→ img_feats（mmdet.ResNet → mmdet.FPN）
        │                              │
        │                              ↓（作为融合原料传给点云分支）
        └─→ extract_pts_feat ─→ pts_feats（VFE[融合发生处] → SparseEncoder
                                        → SECOND → SECONDFPN）
                                       │
        loss = pts_bbox_head.loss(pts_feats)  +  loss_imgs(img_feats)
                                              （RPN loss + RoIHead loss，可选）
```

#### 4.1.3 源码精读

构造函数按 `if 槽位:` 逐个 build，点云分支与图像分支泾渭分明：

- [mvx_two_stage.py:L49-95](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/mvx_two_stage.py#L49-L95)：`__init__` 的全部构造逻辑。注意 L78-83：`pts_bbox_head` 在 build 前会被注入 `train_cfg.pts` / `test_cfg.pts`——这就是配置里 `train_cfg = dict(pts=dict(...))` 二级结构的消费位置；L85-92 build 四个图像分支组件。
- [mvx_two_stage.py:L166-184](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/mvx_two_stage.py#L166-L184)：`extract_img_feat`。先把真实的网络输入尺寸（pad 后）写进每条 `img_meta['input_shape']`（L171-172），再把可能的五维张量 `(B,N,C,H,W)` 折叠成 `(B*N,C,H,W)`（L174-178）以兼容多视角相机输入——单目 MVXNet 走 `(B,C,H,W)`，这个折叠是为多相机模型预留的。随后 `img_backbone` 提特征、可选 `img_neck` 整理成多尺度。
- [mvx_two_stage.py:L221-247](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/mvx_two_stage.py#L221-L247)：`extract_feat`——**两分支的汇合调度点**。注意执行顺序：L241 先算 `img_feats`，L242-246 再把它作为参数喂给 `extract_pts_feat`。图像特征进入点云分支的唯一通道就是这个函数参数（最终被 VFE 的融合层消费），两分支再无其他交叉。
- [mvx_two_stage.py:L249-280](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/mvx_two_stage.py#L249-L280)：`loss`。`pts_feats` 走 3D 头，`img_feats` 走 `loss_imgs`，两个损失字典直接 `update` 合并——监督信号层面两分支互不干扰（融合已经发生在特征层面）。
- [mvx_two_stage.py:L282-331](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/mvx_two_stage.py#L282-L331)：`loss_imgs`，几乎是 MMDetection Faster R-CNN 训练逻辑的翻版：L300-314 跑 `img_rpn_head.loss_and_predict`（RPN 阶段把所有 gt 类别抹成 0，L304-306），并给损失键加 `rpn_` 前缀防撞名；L327-330 跑 `img_roi_head.loss`。**关键阅读点**：这两段都被 `if self.with_img_rpn` / `if self.with_img_bbox` 守卫——MVXNet 配置两个都没配，所以这段代码对 MVXNet 而言是「备而不用」的能力。
- [mvx_two_stage.py:L361-407](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/mvx_two_stage.py#L361-L407)：`predict`。3D 结果来自 `pts_bbox_head.predict`（L391-393）；2D 结果仅当 `with_img_bbox` 时由 `predict_imgs` 产出（L397-401，内部 L351-358 先 RPN 出 proposal 再 RoIHead 精修）。最后 L404-406 调 `add_pred_to_datasample` 同时写回 `pred_instances_3d` 与 `pred_instances`（见 [detectors/base.py:L150-151](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/base.py#L150-L151)），这正是 u2-l4 讲过的 Det3DDataSample 双字段契约。

一个阅读注记（诚实提醒）：[mvx_two_stage.py:L126-129](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/mvx_two_stage.py#L126-L129) 的 `with_fusion` 属性写的是 `hasattr(self, 'pts_fusion_layer') and self.fusion_layer is not None`，后半句引用了不存在的 `self.fusion_layer`（应为 `self.pts_fusion_layer`）。若在配置了 `pts_fusion_layer` 的模型上调用该属性会抛 `AttributeError`；不过全库没有任何调用点使用 `with_fusion`（融合层的调用在 VFE 内部判断），所以这是一个无害的休眠缺陷。读源码时要以「实际调用链」为准，而非属性名臆测。

#### 4.1.4 代码实践

**实践目标**：验证「积木式构造」——用配置构建出的模型，其属性集合与配置槽位一一对应，并确认 MVXNet 配置下图像分支只有 backbone + neck。

**操作步骤**（示例代码，在安装好 mmdet3d 的环境中运行；`SparseEncoder` 依赖 `spconv`，需 GPU 环境，若本机无 spconv 则此步**待本地验证**）：

```python
# practice_slots.py（示例代码）
from mmengine.config import Config
from mmdet3d.registry import MODELS
import mmdet3d  # 触发注册器 locations 懒导入

cfg = Config.fromfile(
    'configs/mvxnet/mvxnet_fpn_dv_second_secfpn_8xb2-80e_kitti-3d-3class.py')
model = MODELS.build(cfg.model)

for name in ['img_backbone', 'img_neck', 'img_rpn_head', 'img_roi_head',
             'pts_voxel_encoder', 'pts_middle_encoder', 'pts_backbone',
             'pts_neck', 'pts_bbox_head', 'pts_fusion_layer']:
    print(f'{name:20s}',
          type(getattr(model, name, None)).__name__ if hasattr(model, name) else '<未配置>')
```

**需要观察的现象**：`img_rpn_head` 与 `img_roi_head` 两行打印 `<未配置>`；`img_backbone` 打印 `ResNet`、`img_neck` 打印 `FPN`（来自 mmdet）；`pts_voxel_encoder` 打印 `DynamicVFE`。

**预期结果**：MVXNet 的图像分支确实只有「backbone + neck」两块积木，没有 2D 检测头——图像特征唯一的去处是融合层。这解释了为什么 `loss_imgs` 里那套 RPN+RoIHead 逻辑在 MVXNet 中不会执行。

#### 4.1.5 小练习与答案

**练习 1**：如果把 MVXNet 配置里的 `img_backbone` 与 `img_neck` 整段删掉，模型还能训练吗？输入数据需要怎么改？

**答案**：能。`__init__` 里 `if img_backbone:` 不成立，属性不创建；`extract_img_feat` 返回 `None`，`extract_pts_feat` 收到 `img_feats=None`，`DynamicVFE.forward` 中 `img_feats is not None` 的融合条件（L272）不成立，跳过融合退化为 DV-SECOND。但数据侧必须同步改：`modality` 里的 `use_camera` 要改为 `False`，管线中 `LoadImageFromFile` 与 `Pack3DDetInputs` 的 `img` 键也要删掉，否则预处理器仍会等待 `imgs` 输入。

**练习 2**：`loss` 方法里 `if pts_feats:` 与 `if img_feats:` 两个分支会同时触发吗？对 MVXNet 配置，`loss_imgs` 返回什么？

**答案**：会同时触发——两分支输入独立，`extract_feat` 同时返回二者。但对 MVXNet，`loss_imgs` 内 `with_img_rpn` 与 `with_img_bbox` 均为 `False`，L316-325 走 else（且无预置 proposals），L327 跳过，最终返回空字典 `dict()`。即 MVXNet 的训练损失全部来自 `pts_bbox_head`，图像分支只通过融合特征间接影响 3D 损失。

**练习 3**：为什么 `img_rpn_head` 的配置注入用的是 `test_cfg.rpn`（L301）而不是 `test_cfg.img`？

**答案**：因为 `img_rpn_head` / `img_roi_head` 是 MMDetection 的组件，它们的接口约定遵循 mmdet 的 Faster R-CNN（RPN 的 proposal 生成参数叫 `proposal_cfg`，来自 `test_cfg.rpn`）；而 `pts_bbox_head` 遵循 mmdet3d 约定，用 `test_cfg.pts`。一个检测器内部同时服务两套组件生态的配置习惯，这正是「跨库复用」的代价与直观体现。

### 4.2 MVXFasterRCNN 与 DynamicMVXFasterRCNN：薄壳子类与动态体素化

#### 4.2.1 概念说明

[mvx_faster_rcnn.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/mvx_faster_rcnn.py) 定义了两个类，都很短：`MVXFasterRCNN` 是纯命名壳（不添加任何逻辑），`DynamicMVXFasterRCNN` 只重写一个方法 `extract_pts_feat`。为什么值得重写？因为基类的 `extract_pts_feat` 假设的是 **hard 体素化**接口，而 MVXNet 用的是 **dynamic 体素化**（配置 L12：`voxel_type='dynamic'`），两者传给 VFE 的东西和 VFE 返回的东西都不同。

回顾 u4-l3：dynamic 体素化时，预处理器产出的 `voxel_dict` 中 `voxels` 就是拼接后的全部原始点 `(总点数, 4)`，`coors` 是每个点所在格子的坐标 `(总点数, 4)`——**没有** `num_points`，因为「每个体素里有几个点」此时还没有定义，聚合被推迟到 `DynamicVFE` 内部用 `DynamicScatter` 完成。

#### 4.2.2 核心流程

两条路线在 `extract_pts_feat` 的接口对比：

| | 基类（hard 体素化） | DynamicMVXFasterRCNN（dynamic 体素化） |
| --- | --- | --- |
| 调用 VFE 的参数 | `voxels, num_points, coors, img_feats, metas` | `voxels(=原始点), coors(=逐点格坐标), points, img_feats, metas` |
| VFE 的返回 | 单个 `voxel_features` | 二元组 `(voxel_feats, voxel_coors)` |
| 聚合发生在 | 预处理器（进网络前已定长） | VFE 内部最后一层之后（`DynamicScatter` max 池化） |
| middle_encoder 收到 | `(voxel_features, coors, batch_size)` | `(voxel_feats, feature_coors, batch_size)` |

**为什么要动态化？** hard 体素化受 `max_num_points` 与 `max_voxels` 双上限约束，超限静默丢点；MVXNet 的融合发生在点级，任何被丢弃的点都意味着丢失一份图像信息。动态体素化保留全部点参与融合，只在融合完成后才聚合，这正配合了「先融合、后体素化」的 MVX-Net 设计。

#### 4.2.3 源码精读

- [mvx_faster_rcnn.py:L10-15](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/mvx_faster_rcnn.py#L10-L15)：`MVXFasterRCNN` 全文——只调 `super().__init__(**kwargs)`，说明「Faster R-CNN 形状」完全由配置槽位（`img_rpn_head` + `img_roi_head`）表达，类名只是语义标签。
- [mvx_faster_rcnn.py:L25-56](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/mvx_faster_rcnn.py#L25-L56)：`DynamicMVXFasterRCNN.extract_pts_feat`。L48-50 按 `voxels, coors, points, img_feats, batch_input_metas` 的顺序调 VFE 并解包二元组；L51 用 `voxel_dict['coors'][-1, 0] + 1` 从逐点坐标的最后一行恢复 batch_size（coors 按帧号排序，末行帧号即最大帧号）；L52 把 VFE 产出的 `(voxel_feats, feature_coors)` 交给 `SparseEncoder`。
- 对照 [mvx_two_stage.py:L186-219](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/mvx_two_stage.py#L186-L219)：基类版本 L209-212 传的是 `voxel_dict['num_points']`（hard 三件套之一），L213 同样以末行首列加一恢复 batch_size。注意基类 L209-212 虽然也传了 `img_feats` 与 `batch_input_metas`（给 `HardVFE` 的融合接口留位），但 `points` 原始点列表没有传——`HardVFE` 需要用体素内的点坐标做投影（详见其 forward，本文不展开）。
- 配置侧对应：[mvxnet config:L9-21](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/mvxnet/mvxnet_fpn_dv_second_secfpn_8xb2-80e_kitti-3d-3class.py#L9-L21)，`voxel=True, voxel_type='dynamic'`，`max_num_points=-1`、`max_voxels=(-1, -1)` 表示不设上限；同一块里还混着图像专用的 `mean/std/bgr_to_rgb/pad_size_divisor=32`——一个预处理器同时服务两种模态（u4-l3 讲过它的双职责）。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：用逐行对照的方式吃透 hard 与 dynamic 两条 `extract_pts_feat` 的接口差异。

**操作步骤**：

1. 打开 `mvx_two_stage.py` L186-219 与 `mvx_faster_rcnn.py` L25-56，左右并排。
2. 对每次方法调用，写下「实参 → 形参」的对应关系，特别注意两个版本调用 `self.pts_voxel_encoder(...)` 时参数个数与含义的差别。
3. 追问自己三个问题：动态版为什么必须多传 `points`？（融合层投影需要**原始**点的雷达系坐标，而 `voxels` 在动态模式下恰好就是原始点——此处传参实为同一数据的两个视图）；`feature_coors` 与 `voxel_dict['coors']` 有何不同？（前者是聚合后「非空体素」的坐标，行数=体素数；后者是逐点坐标，行数=点数）；两版恢复 batch_size 的写法为何相同？

**需要观察的现象**：两个版本对 `pts_middle_encoder` 的调用形态一致（特征、坐标、batch_size 三参数），说明 SparseEncoder 对上游是 hard 还是 dynamic 无感知——差异被 VFE 的返回契约完全吸收。

**预期结果**：完成一张与 4.2.2 表格类似的对照表（可补充「voxels 语义」一行：hard 版是定长 `(体素数, 每格点数上限, C)` 的 padded 张量，dynamic 版是 `(总点数, 4)`）。

#### 4.2.5 小练习与答案

**练习 1**：`DynamicMVXFasterRCNN` 为什么必须重写 `extract_pts_feat`，而不能像 `MVXFasterRCNN` 一样只做命名壳？

**答案**：因为基类方法把 VFE 的返回值当单个张量用（L214 直接传给 middle_encoder 的第二个位置参数是 `voxel_dict['coors']`），而 `DynamicVFE` 返回 `(voxel_feats, voxel_coors)` 二元组，且聚合后的 `voxel_coors` 行数与 `voxel_dict['coors']` 不同。接口契约变了，必须重写以解包并改用 `feature_coors`。

**练习 2**：`voxel_dict['coors'][-1, 0] + 1` 恢复 batch_size 的技巧依赖什么隐含前提？

**答案**：依赖「coors 按第 0 列（帧号）升序排列，因此最后一行的帧号是最大帧号」。这一前提由动态体素化算子（u4-l3 讲过的逐点格坐标计算）保证。若上游打乱顺序，该技巧失效——这也是为什么它是脆弱的隐式契约而非显式参数。

**练习 3**：配置里 `data_preprocessor` 同时出现 `voxel_layer`（体素化）与 `mean/std/pad_size_divisor`（图像预处理），这两组参数分别在数据流的哪一站生效？

**答案**：都在 `Det3DDataPreprocessor.forward` 里生效，但作用对象不同：mean/std/bgr_to_rgb/pad_size_divisor 作用于 `imgs`（先归一化再 pad 到 32 的倍数并堆叠成 `(B,C,H,W)`，`input_shape` 随之写入 metainfo）；`voxel_layer` 作用于 `points` 生成 `voxels` 字典。产物一起进入 `batch_inputs_dict`，供 `extract_feat` 的两个分支取用。

### 4.3 图像特征如何映射回 voxel：DynamicVFE + PointFusion 逐点融合

#### 4.3.1 概念说明

这是本讲的核心问题。MVXNet 的答案分两步：

1. **在哪融合**：在 `DynamicVFE` 的**最后一个 VFE 层**之后、聚合进体素之前。此时每个点仍携带独立的 64 维特征，融合以「每个点采一次图像特征、与点特征相加」的方式完成。
2. **怎么融合**：`PointFusion` 把点投影到多尺度图像特征图上，用 `grid_sample` 双线性插值取出该点处的图像特征，经线性变换后与点特征变换结果**逐元素相加**，再过 ReLU。

融合完成后，`DynamicScatter` 立即把点级特征 max 池化成体素特征——所以「图像特征映射回 voxel」的准确说法是：**图像特征先映射到每个点，再随点聚合成体素特征**。此后 SparseEncoder、SECOND、Anchor3DHead 对图像信息一无所知，它们只是消费已经「染色」的体素特征。

通道账本（对照配置 L38-61 可逐项核对）：

```text
每点输入:        4 (x,y,z,intensity)
  + with_cluster_center  → +3  (点到所在体素均值的偏移)
  + with_voxel_center    → +3  (点到所在格中心的偏移)
= 10 维输入到 VFE 第 1 层:  10 → 64（逐点 Linear+BN+ReLU）
  拼接体素 max 池化结果      64+64 = 128
VFE 第 2 层:                128 → 64（逐点）
【融合】PointFusion:        64 (点) + 256×5 (图) → 128   ← 就发生在这里
DynamicScatter max:         每点 128 → 每体素 128
SparseEncoder:              in_channels=128（与配置 L59 一致）
```

#### 4.3.2 核心流程

`DynamicVFE.forward` 的骨架（省略装饰特征细节）：

```text
输入: features(点, 4), coors(点, 4), points, img_feats, img_metas
1. 装饰: 拼 cluster-center 偏移(3) + voxel-center 偏移(3) → 10 维
2. for 每层 VFE:
     point_feats = Linear+BN+ReLU(features)          # 逐点
     if 是最后一层 且 融合层存在 且 img_feats 不为 None:
         point_feats = fusion_layer(img_feats, points, point_feats, img_metas)
     voxel_feats, voxel_coors = DynamicScatter(point_feats, coors)  # max 聚合
     if 不是最后一层:
         把 voxel_feats 映射回每个点(map_voxel_center_to_point)，
         与 point_feats 拼接 → 下一层输入（VoxelNet 式交互）
3. 返回 (voxel_feats, voxel_coors)
```

`PointFusion.forward` 的数学形式：设某点的点级特征为 \( p \in \mathbb{R}^{64} \)，其在 5 层图像特征图上采样并拼得的图像特征为 \( g \in \mathbb{R}^{640} \)，则

\[
f = \mathrm{ReLU}\big( W_g \,\mathrm{BN}(W_l \cdot g) + W_p \,\mathrm{BN}(p) \big) \in \mathbb{R}^{128}
\]

其中 \( W_l \) 是 5 个 3×3 侧向卷积（每层 256→128，输出拼接成 640 维）之后接的 `Linear(640→128)`，\( W_g \) 与 \( W_p \) 分别是图像/点两路的 `Linear`，两路各自带 `BatchNorm1d`。配置中 `activate_out=True`（输出过 ReLU）、`fuse_out=False`（不再追加卷积）。

#### 4.3.3 源码精读

- [voxel_encoder.py:L218-284](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/voxel_encoders/voxel_encoder.py#L218-L284)：`DynamicVFE.forward` 全流程。L242-268 做三类点级装饰（cluster 中心偏移、格中心偏移、可选距离）；**L269-274 是融合的精确插入点**——条件 `i == len(self.vfe_layers) - 1 and self.fusion_layer is not None and img_feats is not None` 三者缺一不可；L275 `DynamicScatter` 完成点→体素聚合；L276-280 非末层时用 `map_voxel_center_to_point` 把体素级 max 特征广播回每个点再拼接，实现 VoxelNet 论文的「点-体素特征交互」。
- [voxel_encoder.py:L175-216](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/voxel_encoders/voxel_encoder.py#L175-L216)：`map_voxel_center_to_point`。用一个一维 `canvas` 查找表把「体素坐标 → 体素行号」编码成 O(1) 映射：先 scatter 非空体素行号到 canvas（L199-207），再按每点的体素坐标查表取回所属体素的特征（L209-215）。
- [voxel_encoder.py:L171-173](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/voxel_encoders/voxel_encoder.py#L171-L173)：`fusion_layer` 槽位的构建——`DynamicVFE` 自己 build 融合层，配置里它是 `pts_voxel_encoder` 的**嵌套子配置**（config L47-56），而不是检测器的顶层槽位。这也解释了 4.1.3 阅读注记中检测器顶层 `pts_fusion_layer` 槽位在 MVXNet 中未用的原因：融合层的宿主是 VFE。
- [point_fusion.py:L151-229](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/fusion_layers/point_fusion.py#L151-L229)：`PointFusion.__init__`。L189-206 构建侧向卷积组与图像路变换：5 个 `ConvModule(256→128)` + `Linear(5×128→128)` + BN；L212-215 点路 `Linear(64→128)` + BN；L217-223 仅当 `fuse_out=True` 才有输出卷积（MVXNet 未开）。
- [point_fusion.py:L231-257](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/fusion_layers/point_fusion.py#L231-L257)：`forward`。L245 取多尺度采样特征，L246-248 图像路过变换（训练期可选 dropout 防过拟合），L249 点路过变换，**L251 逐元素相加**完成融合，L252-255 按配置过 ReLU/输出卷积。
- [point_fusion.py:L259-291](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/fusion_layers/point_fusion.py#L259-L291)：`obtain_mlvl_feats`。L272-278 先对 `img_levels` 指定的各层特征过侧向卷积统一到 128 通道；L281-288 **逐样本、逐层**调用 `sample_single` 采样（注意 L285 传 `pts[i][:, :3]`——只取坐标前三列）；L287 层间拼接、L290 样本间拼接，得到全 batch 的 `(总点数, 5×128)`。
- [point_fusion.py:L293-329](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/fusion_layers/point_fusion.py#L293-L329)：`sample_single`。L306-312 从 `img_meta` 里**宽容地**提取 2D 增强信息（键不存在就取默认值：scale=1、flip=False、crop=0）；L313 按 `coord_type='LIDAR'` 取 `lidar2img`；L323-324 注意两个形状的分工——`input_shape`（pad 后）用于归一化采样网格，`img_shape`（pad 前）用于翻转坐标。
- 配置对照：[mvxnet config:L38-56](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/mvxnet/mvxnet_fpn_dv_second_secfpn_8xb2-80e_kitti-3d-3class.py#L38-L56)，`img_levels=[0,1,2,3,4]` 采样 FPN 全部 5 层输出（config L37 `num_outs=5`），`align_corners=False` 与 FPN 的 BN（L36 注释「避免 loss nan」）都是数值稳定性取舍。

#### 4.3.4 代码实践

**实践目标**：脱离整个检测器，单独在 CPU 上跑通 `PointFusion` 的前向，直观验证「N 个点进、N×128 融合特征出」。

**操作步骤**（示例代码，保存为 `practice_fusion.py`，只依赖 torch 与 mmdet3d 安装，无需 GPU/spconv）：

```python
# practice_fusion.py（示例代码）
import torch
from mmdet3d.registry import MODELS
import mmdet3d  # 触发注册

fusion = MODELS.build(dict(
    type='PointFusion', img_channels=256, pts_channels=64,
    mid_channels=128, out_channels=128, img_levels=[0, 1, 2, 3, 4],
    align_corners=False, activate_out=True, fuse_out=False))

N = 200
# 伪点云：x 向前 5~30 米，y 左右 ±10 米，z 高度 -1~3 米
pts = [torch.cat([torch.rand(N, 1) * 25 + 5,
                  torch.rand(N, 1) * 20 - 10,
                  torch.rand(N, 1) * 4 - 1], dim=1)]
pts_feats = torch.rand(N, 64)                       # 伪 VFE 末层点特征
img_feats = [torch.rand(1, 256, 48, 160) for _ in range(5)]  # FPN 五层

# 伪 lidar2img：R_velo2cam 把雷达系转到相机系（x 前 → z 前），
# 再拼内参（fu=fv=700，主点 (80, 24)），示例中忽略平移
K = torch.tensor([[700., 0, 80, 0], [0, 700., 24, 0], [0, 0, 1, 0], [0, 0, 0, 1]])
R = torch.tensor([[0., -1, 0, 0], [0, 0, -1, 0], [1, 0, 0, 0], [0, 0, 0, 1.]])
lidar2img = (K @ R).numpy()

img_metas = [dict(lidar2img=lidar2img,
                  input_shape=(384, 1280),   # pad 后输入尺寸（特征图的 8 倍）
                  img_shape=(384, 1280))]    # 未提供 scale_factor/flip 等键 → 取默认值

out = fusion(img_feats, pts, pts_feats, img_metas)
print('融合输出形状:', out.shape)   # 预期 torch.Size([200, 128])
print('是否全零:', bool((out == 0).all().item()))
```

**需要观察的现象**：

1. 输出形状为 `(200, 128)`——逐点融合不改变点数，只改变通道。
2. 把若干点的坐标改到视野外（例如 `x` 设为负值，深度为负），其对应输出行会因 `padding_mode='zeros'` 而失去图像信息（采样值为 0），但形状不变。
3. 删掉 `img_metas` 里的 `lidar2img` 键会直接报 `KeyError`——它是唯一必需的元信息键，其余 2D 增强键都有默认值。

**预期结果**：打印 `torch.Size([200, 128])` 且非全零（视野内的点采到了图像特征）。若你的伪点大多落到图像外导致全零，缩小 `y` 的取值范围或增大焦距即可。（本实践未在当前环境运行，待本地验证。）

#### 4.3.5 小练习与答案

**练习 1**：融合为什么插在「最后一个 VFE 层」而不是第一层之前或 SparseEncoder 之后？

**答案**：插在最后一级点级特征之后，点特征已经过两层 VFE 加工（含与体素 max 特征的交互），语义更强，融合收益更大；同时下一站 `DynamicScatter` 立即聚合，图像信息以「每点一票」的方式公平进入 max 池化。若放第一层之前，融合的是原始坐标装饰特征，噪声大；若放 SparseEncoder 之后，特征已是稀疏体素张量，与图像像素网格没有直接对应关系，需要另建投影机制（那正是 BEVFusion 等 BEV 级融合的做法）。

**练习 2**：`obtain_mlvl_feats` 里 L285 用 `pts[i][:, :3]` 而不是整个点张量，为什么？

**答案**：因为投影只需要三维坐标。MVXNet 的输入点是 4 维（含强度），第 4 列是反射率不是坐标；`point_sample` 里的 `points_cam2img` 按三维齐次投影计算，多传列会破坏矩阵乘法形状。

**练习 3**：`DynamicScatter` 的聚合模式由 `mode` 参数决定（`DynamicVFE.__init__` L167-168 传入 `average_points=(mode != 'max')`），MVXNet 用 max。如果把 `pts_voxel_encoder` 配置里加上 `mode='avg'`，通道账本会变吗？检测行为预期会怎么变？

**答案**：通道数不变（max/avg 只改聚合的规约方式，不改维度），配置无需改 `in_channels`。行为上，max 池化保留每通道最显著响应（对稀疏的前景点更鲁棒），avg 会平滑掉孤立强响应、也更易受体素内点数多少的影响；对 KITTI 小目标（行人/骑行者），换成 avg 通常会使 AP 下降——可作为消融实验验证（待本地验证）。

### 4.4 跨模态对齐的三重坐标变换：apply_3d_transformation 与 point_sample

#### 4.4.1 概念说明

融合要成立，前提是「点投影到的像素」确实是那个点在**当前输入图像**上的像。难点在于：训练管线对点云和图像做了**不同**的增强——`GlobalRotScaleTrans`、`RandomFlip3D` 只作用于点云（u3-l6），`RandomResize` 只作用于图像，而且 `lidar2img` 标定矩阵是对着**原始**坐标系算的。于是 `point_sample` 在采样前必须完成一次「时间旅行」：

1. **逆 3D 增强**：把增强后的点云变换回原始雷达系（这样才配得上原始标定）；
2. **投影**：用 `lidar2img` 把原始雷达系点投到**原始**图像平面；
3. **正向 2D 增强**：把原始图像坐标映射到**当前**（缩放/裁剪/翻转后）输入图像上，再归一化到 `grid_sample` 要求的 \([-1,1]\) 网格。

第 1 步的依据是 u3-l6 讲过的审计线索：每个 3D 增强 transform 把自己做过什么登记进 `transformation_3d_flow`（如 `['S', 'R', 'HF']`），并把参数（`pcd_rotation`、`pcd_scale_factor`、`pcd_trans`、`pcd_horizontal_flip`）写进 metainfo。`apply_3d_transformation(reverse=True)` 按登记顺序的**倒序**、用各操作的**逆**重放一遍。

#### 4.4.2 核心流程

```text
增强后点 p_aug（雷达系）
  │ ① apply_3d_transformation(reverse=True)
  │    按 flow 倒序执行: 逆翻转 → 逆旋转 R⁻¹ → 逆缩放 1/s → 逆平移 -t
  ↓
原始点 p（雷达系）
  │ ② points_cam2img: [u, v, 1]ᵀ ∝ P · [p, 1]ᵀ，P = lidar2img（4×4）
  ↓
原始像坐标 (u, v)（原图，未缩放未翻转）
  │ ③ 正向 2D 增强: u' = u·w_scale − w_crop；若翻转: u' = w_ori − u'
  ↓
当前输入图像坐标 (u', v')
  │ ④ 归一化: x = u'/W_pad·2 − 1, y = v'/H_pad·2 − 1
  ↓
F.grid_sample（双线性，padding_mode='zeros'）→ 该点的图像特征 (C,)
```

其中透视投影对一般点 \( p_c = (x_c, y_c, z_c) \)（相机系）为：

\[
u = f_u \frac{x_c}{z_c} + c_u, \qquad v = f_v \frac{y_c}{z_c} + c_v
\]

`lidar2img` 已把「雷达系 → 相机系（外参）→ 像素（内参）」合成一个 4×4 矩阵，所以代码里一步完成。

#### 4.4.3 源码精读

- [point_fusion.py:L18-112](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/fusion_layers/point_fusion.py#L18-L112)：`point_sample` 全函数，三重变换的现场。L63-64 第①步逆 3D 增强；L67-72 第②步投影（`with_depth=True` 时顺带保留深度，供 `valid_flag` 过滤深度为负的点）；L74-77 第③步正向 2D 增强（先乘 scale、再减 crop offset）；L82-86 翻转修正（用 **pad 前**的 `img_shape` 宽度，因为翻转发生在 pad 之前）；L88-92 第④步归一化拼网格；L96-101 `grid_sample` 双线性采样；L103-110 可选的有效性过滤——视野外或深度非正的点特征置零并返回掩码（MVXNet 未启用 `valid_flag`，越界点靠 `padding_mode='zeros'` 自然拿到零特征）。
- [coord_transform.py:L11-98](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/fusion_layers/coord_transform.py#L11-L98)：`apply_3d_transformation`。L39-61 从 metainfo 容错地取增强参数（缺键即恒等）；L63-64 先克隆再包装成 `BasePoints` 子类对象（复用 u2-l3 讲的 `rotate/scale/flip/translate` 原地变换）；**L70-79 逆向模式**：缩放取倒数、平移取反、旋转取矩阵逆（注释说明不用 `R @ R⁻¹ ≈ I` 的数值风险），并 `flow[::-1]` 倒序；L85-96 用 `flow_mapping` 把字符串指令翻译成函数依次执行。这是 u3-l6 留下的悬念（「审计线索被谁消费」）的正面回答。
- [bbox_3d/utils.py:L309-323](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/utils.py#L309-L323)：`get_proj_mat_by_coord_type`，一行映射表 `LIDAR→lidar2img / DEPTH→depth2img / CAMERA→cam2img`——融合层不关心标定怎么来的（KITTI 数据集类在 `parse_data_info` 阶段就算好放进 info，u3-l3 讲过），只按键取用。
- [bbox_3d/utils.py:L184-233](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/utils.py#L184-L233)：`points_cam2img` 的齐次投影实现（升维、矩阵乘、按深度除回去）。
- 配置对照：[mvxnet config:L150-176](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/mvxnet/mvxnet_fpn_dv_second_secfpn_8xb2-80e_kitti-3d-3class.py#L150-L176)，训练管线同时含 `RandomResize`（只动图，L160）与 `GlobalRotScaleTrans`/`RandomFlip3D`（只动点云，L162-166）——这正是三重变换必须存在的配置层证据。另注意 L160 的 `scale=[(640,192),(2560,768)]` 是**随机多尺度**，即同一 epoch 内不同样本的 `scale_factor` 不同，逆变换必须逐样本从 metainfo 读取。

#### 4.4.4 代码实践（源码阅读型 + 可选动手）

**实践目标**：验证你对三重变换顺序的理解，并亲手观察「跳过逆变换会怎样」。

**操作步骤**：

1. 阅读 `point_sample` L62-101，给每一行标注它属于第①/②/③/④步中的哪一步。
2. 动手实验（CPU 可跑）：在 4.3.4 的脚本基础上，给 `img_metas` 逐步添加 3D 增强键，观察投影点位置的变化：

```python
# （示例代码）接 practice_fusion.py，替换 img_metas 后重新调用 fusion
import math
theta = 0.3  # 假设训练时对点云整体旋转了 0.3 rad
img_metas = [dict(
    lidar2img=lidar2img, input_shape=(384, 1280), img_shape=(384, 1280),
    pcd_rotation=torch.tensor(
        [[math.cos(theta), -math.sin(theta), 0],
         [math.sin(theta), math.cos(theta), 0], [0, 0, 1.]]).numpy(),
    pcd_scale_factor=1.1,
    pcd_trans=[0., 0., 0.],
    pcd_horizontal_flip=True,
    transformation_3d_flow=['S', 'R', 'HF'])]
```

3. 想象（或通过在 `point_sample` 里临时打印验证）：若把 L63-64 的 `reverse=True` 改成 `False`，投影用的是什么坐标系下的点？结果会怎样错位？

**需要观察的现象**：加入增强键后，只要 `transformation_3d_flow` 与各参数键**配套**，融合依然输出 `(N, 128)` 且数值合理（逆变换抵消了增强）；若只给 `pcd_rotation` 键而漏掉 `transformation_3d_flow`（空列表），逆变换不会执行——`flow` 为空时循环体不跑，`pcd_rotation` 被静默忽略，点仍按增强后坐标投影，产生系统性错位。

**预期结果**：理解「参数键与 flow 键必须成对登记」这一隐含契约；错位不会报错，只会让融合特征采错位置——这是多模态训练中典型的**静默 bug**，也是 u3-l6 强调审计线索完整性的原因。

#### 4.4.5 小练习与答案

**练习 1**：`RandomResize` 作用于图像、`GlobalRotScaleTrans` 作用于点云，为什么点云分支的其他增强（如 `PointsRangeFilter`）不需要进 `transformation_3d_flow`？

**答案**：`transformation_3d_flow` 只登记**改变点云几何且需要可逆**的刚体/相似变换，因为只有这类变换会破坏「点 ↔ 原始标定」的对应关系。`PointsRangeFilter` 是删除点（不可逆，也无需逆——删掉的点不参与投影）；`PointShuffle` 只改顺序不改坐标。登记的是「对齐所需的最小集合」。

**练习 2**：逆变换中旋转用 `pcd_rotate_mat.inverse()`，代码注释说不用角度构造逆矩阵，也不信任 `R @ R⁻¹ == I`。结合 u2-l3 的知识说明这里的数值考量。

**答案**：`BasePoints.rotate` 内部经 `rotation_3d_in_axis` 做矩阵乘，浮点矩阵乘有累积误差；若先用角度 −θ 构造「逆矩阵」，两次独立构造的矩阵乘积可能偏离恒等更远。直接取已用矩阵的代数逆（`inverse()`）在数值上与原变换配对得更紧，残差更小。注释强调即使如此乘积也「不严格是单位阵」，提醒读者这是近似恒等。

**练习 3**：`point_sample` 的 `valid_flag` 参数 MVXNet 没开，越界点靠 `padding_mode='zeros'` 拿零特征。这两种方案的监督语义有什么差别？

**答案**：零特征方案下，视野外的点的图像路输出是 `BN(0·W)` 近似常数，融合结果退化为「只有点特征」——点仍然参与体素聚合，等价于对图像信息做软屏蔽；`valid_flag=True` 则显式返回布尔掩码，允许调用方把这些点从融合中剔除或单独处理（例如 VoteFusion 用于多模态投票的场景）。前者实现简单、形状恒定，后者语义干净但要处理不规则掩码。

### 4.5 mmdet 组件复用全景：从配置看跨库借用的边界

#### 4.5.1 概念说明

MVXNet 配置是观察「MMDetection 组件如何在 3D 项目中被直接复用」的最佳标本。逐段扫描配置中的 `type` 字符串，凡是 `mmdet.` 前缀的都是跨库借用。本配置共 5 处：

| 配置位置 | type | 来源 | 在 3D 项目中的角色 |
| --- | --- | --- | --- |
| L23 `img_backbone` | `mmdet.ResNet` | mmdet | 图像分支骨干（caffe 风格、frozen_stages=1） |
| L32 `img_neck` | `mmdet.FPN` | mmdet | 图像分支多尺度特征金字塔 |
| L94 `loss_cls` | `mmdet.FocalLoss` | mmdet | Anchor3DHead 的分类损失（u5-l4 讲过） |
| L100 `loss_bbox` | `mmdet.SmoothL1Loss` | mmdet | 框回归损失 |
| L102 `loss_dir` | `mmdet.CrossEntropyLoss` | mmdet | 方向分类损失 |

值得划重点的是**没有出现的名字**：`img_rpn_head` 与 `img_roi_head` 在本配置中完全缺席。也就是说，虽然类名叫 `DynamicMVXFasterRCNN`、基类也内置了完整的 2D 两阶段损失通路（`loss_imgs`/`predict_imgs`），但 MVXNet 实例化的图像分支只有「backbone + neck」——**纯特征提取器**，图像特征的唯一消费者是 `DynamicVFE` 里的 `PointFusion`。「Faster R-CNN」在这里是基类提供的**能力上限**，不是本配置的**实际形状**。这不是缺陷，而是 3D 检测任务的理性选择：KITTI 3D 评估只看 3D 框，2D 检测头只会白白增加损失项与显存。

#### 4.5.2 核心流程

把 4.1-4.4 的结论拼成一张完整的数据流图（标 ★ 处为跨库组件，标 ☆ 处为两分支汇合点）：

```text
batch_inputs_dict
 ├─ imgs (B,C,H,W)
 │    └→ ★ResNet(caffe) → ★FPN(5 层) ──────────────┐
 │                                                   │ img_feats
 └─ points → Det3DDataPreprocessor(dynamic voxel)    │
      → voxel_dict{voxels=原始点, coors=逐点格坐标}   │
           └→ DynamicVFE                              │
                ├ 装饰(10 维) → VFE₁(64) → 散射回点(128)
                ├ VFE₂(64)                            │
                ├ ☆PointFusion ←──────────────────────┘
                │    ①逆3D增强 ②lidar2img ③正2D增强 ④grid_sample
                │    ReLU(BN(W_l·g) + BN(W_p·p)) → 128 维/点
                └ DynamicScatter(max) → 体素特征(128)
                     → SparseEncoder → SECOND → SECONDFPN
                     → Anchor3DHead（3 个 mmdet 损失★）→ 3D 框
```

汇合点只有一处：`extract_feat` 把 `img_feats` 传给 `extract_pts_feat`（u 见 4.1.3），最终在 `DynamicVFE.forward` 的最后一层被 `PointFusion` 消费。`roi_head` 阶段没有汇合点——因为本配置根本没有 RoIHead。

#### 4.5.3 源码精读

- [mvxnet config:L22-37](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/mvxnet/mvxnet_fpn_dv_second_secfpn_8xb2-80e_kitti-3d-3class.py#L22-L37)：图像分支全部配置——注意 L28-29 的 `norm_cfg=dict(type='BN', requires_grad=False)` 与 `norm_eval=True`：图像分支整体冻结 BN 统计，这是配合 L273 `load_from` 的 Detectron2/COCO 预训练权重（Caffe 风格 BGR、均值减法见 L18）使用的典型配置；L36 给 FPN 加不需要梯度的 BN 并注释「避免 loss nan」——冻结的 BN 在这里兼当数值稳定器。
- [mvxnet config:L57-72](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/mvxnet/mvxnet_fpn_dv_second_secfpn_8xb2-80e_kitti-3d-3class.py#L57-L72)：点云分支骨干链全部为 mmdet3d 自产（`SparseEncoder`/`SECOND`/`SECONDFPN`，即 u5-l2、u5-l3 的主角），`in_channels=128` 正是 4.3.1 通道账本的落点。
- [mvxnet config:L73-103](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/mvxnet/mvxnet_fpn_dv_second_secfpn_8xb2-80e_kitti-3d-3class.py#L73-L103)：`Anchor3DHead` 及三个 `mmdet.` 损失——head 是 3D 的（u5-l4），损失是 2D 库借的，说明「复用」不限于结构模块，无参数的损失函数跨库更常见。
- [mvxnet config:L260-266](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/mvxnet/mvxnet_fpn_dv_second_secfpn_8xb2-80e_kitti-3d-3class.py#L260-L266)：`clip_grad`（梯度裁剪）与 `load_from`（L273 的 COCO 预训练 URL）——多模态训练从 2D 预训练起步是标配流程，图像分支能借 mmdet 组件的好处在此兑现：预训练权重与组件实现同源。

#### 4.5.4 代码实践（对应本讲指定实践任务）

**实践目标**：程序化地枚举配置里「哪些模块来自 mmdet、哪些来自 mmdet3d」，并画出两分支在 `extract_feat` 与 roi_head 阶段的汇合点。

**操作步骤**（示例代码，CPU 可跑，无需构建模型）：

```python
# practice_origin.py（示例代码）
from mmengine.config import Config

cfg = Config.fromfile(
    'configs/mvxnet/mvxnet_fpn_dv_second_secfpn_8xb2-80e_kitti-3d-3class.py')

def walk(d, path=''):
    if isinstance(d, dict):
        if 'type' in d:
            t = str(d['type'])
            tag = 'mmdet 借用' if t.startswith('mmdet.') else 'mmdet3d 自产'
            print(f'{tag} | {path or "root":55s} | {t}')
        for k, v in d.items():
            if k != 'type':
                walk(v, f'{path}.{k}' if path else k)
    elif isinstance(d, (list, tuple)):
        for i, v in enumerate(d):
            walk(v, f'{path}[{i}]')

walk(cfg.model)
```

**需要观察的现象**：输出中 `mmdet 借用` 共 5 行（ResNet、FPN、FocalLoss、SmoothL1Loss、CrossEntropyLoss），其余（DynamicMVXFasterRCNN、Det3DDataPreprocessor、DynamicVFE、PointFusion、SparseEncoder、SECOND、SECONDFPN、Anchor3DHead、Anchor3DRangeGenerator、DeltaXYZWLHRBBoxCoder、Max3DIoUAssigner、BboxOverlapsNearest3D）均为 `mmdet3d 自产`；`img_rpn_head`、`img_roi_head` 的路径**不会出现**。

**预期结果**：结合 4.5.2 的数据流图，结论是——MVXNet 的跨库复用集中在「图像特征提取 + 无参数损失」，两分支汇合点仅有一处（VFE 末层的 PointFusion），roi_head 阶段无汇合点（未配置 2D 检测头）。把该图与 `loss`/`predict` 的源码对照，即可回答「图像信息从哪里进来、到哪里为止」。

#### 4.5.5 小练习与答案

**练习 1**：想让 MVXNet 同时输出 2D 检测框，配置要加什么？加了之后训练会发生什么变化？

**答案**：给 `model` 加 `img_rpn_head`（`mmdet.RPNHead` 或其 3D 项目别名）与 `img_roi_head`（如 `mmdet.StandardRoIHead`，含 `mmdet.Shared2FCBBoxHead` 与 `mmdet.MaxIoUAssigner`），并在 `train_cfg`/`test_cfg` 顶层补 `img` 段（与 `pts` 平级）。此后 `loss_imgs` 被激活，RPN 损失（类别统一为 0）+ RoIHead 损失并入总损失，`predict` 会经 `predict_imgs` 产出 `pred_instances` 写回 Det3DDataSample。同时 `Pack3DDetInputs` 的 keys 需含 `gt_bboxes`/`gt_labels`（本配置 L173-174 已经包含，说明数据侧早已备好 2D 监督）。

**练习 2**：`mmdet.FocalLoss` 与 `mmdet3d` 里注册的同名类（若有）会不会冲突？注册器如何区分？

**答案**：不会冲突。`mmdet.` 前缀触发注册器的 scope 切换（u4-l1）：先在 mmdet 的 `MODELS` 注册表里查 `FocalLoss`，查到即用 mmdet 的实现；无前缀则先查 mmdet3d 本级注册表，再沿 parent 链上溯。所以前缀本质上是在多个同名注册之间显式选边。

**练习 3**：`load_from` 指向的 COCO 预训练权重里显然没有 `pts_*` 分支的参数，加载时会发生什么？

**答案**：MMEngine 的 checkpoint 加载默认按前缀匹配、宽松处理缺失键：`img_backbone`/`img_neck`（或其 Detectron2 命名映射）的权重被加载，`pts_*` 与头部参数保持初始化（`init_cfg` 控制）。这正是多模态训练的标准起手式——图像分支白嫖 2D 预训练，点云分支从头学。日志中会出现 missing/unexpected keys 的提示，可作为加载是否生效的检查点。

## 5. 综合实践

**任务：给 MVXNet 画一张「模块来源 + 数据流」双标注结构图，并用代码验证它。**

1. **枚举来源**：运行 4.5.4 的 `practice_origin.py`，得到模型树中每个节点的来源标签（mmdet 借用 / mmdet3d 自产）。
2. **标注形状**：对照 4.3.1 的通道账本，在结构图上给每段连线标上张量形状——从 `(总点数, 4)` 的动态体素输入，到 VFE 装饰后的 10 维、两级 VFE 的 64/128 维、融合后的 128 维/点、聚合后的每体素 128 维，直到 `SparseEncoder` 的 BEV 输出（`sparse_shape=[41,1600,1408]`，config L60）。图像支路同样标注：`(B,3,H,W)` → ResNet 四阶段 → FPN 五层 `(B,256,H/8..H/128,W/..)`。
3. **标出唯一汇合点**：在图上高亮 `DynamicVFE.forward` L271-274 的融合调用，并在旁边注明三重坐标变换（①逆 3D 增强 → ②lidar2img → ③正 2D 增强 → ④归一化 grid_sample）。
4. **（可选，需 GPU/spconv，待本地验证）单步验证**：构建完整模型，喂一个 batch=1 的伪数据（点云可取 `demo/data/kitti_000008.bin` 前若干点 + 随机图像），在 `DynamicVFE.forward` 的融合调用前后各打一处断点或 hook，打印 `point_feats` 形状从 `(N,64)` 变为 `(N,128)` 的瞬间。
5. **回答收口问题**：图像信息进入模型后，最远到达哪个模块？（答：`DynamicScatter` 聚合之前的每一次点级/体素级运算都携带图像信息，最终体现在 `Anchor3DHead` 的输入特征里；但从数据流上看，显式消费图像张量的代码止步于 `PointFusion`。）

产出：一张双标注结构图（手绘或 mermaid 均可）+ 一份形状对照表。完成后，你应该能不查源码地说出 MVXNet 前向的每一跳。

## 6. 本讲小结

- `MVXTwoStageDetector` 用 `pts_*`/`img_*` 两组可选槽位实现积木式构造，`extract_feat` 先算图像特征、再把它作为参数传入点云分支——这是两分支唯一的显式汇合通道。
- `DynamicMVXFasterRCNN` 仅重写 `extract_pts_feat` 以适配动态体素化接口：VFE 收到「原始点 + 逐点格坐标」，返回「(体素特征, 体素坐标)」二元组，聚合被推迟到网络内部。
- 「图像特征映射回 voxel」实为两跳：`PointFusion` 先把图像特征**逐点**采样并与点特征相加（ReLU(BN(W·g)+BN(p))），再由 `DynamicScatter` max 池化随点聚合进体素；融合精确插在最后一个 VFE 层。
- 跨模态对齐靠三重坐标变换：逆 3D 增强（`apply_3d_transformation` 按 `transformation_3d_flow` 倒序重放）→ `lidar2img` 透视投影 → 正向 2D 增强（scale/crop/flip）→ 归一化 `grid_sample`；参数键与 flow 键必须配套登记，否则静默错位。
- MVXNet 配置的跨库借用共 5 处（`mmdet.ResNet/FPN/FocalLoss/SmoothL1Loss/CrossEntropyLoss`），且**没有**配置 2D 检测头——图像分支是纯特征捐赠者，「Faster R-CNN」只是基类能力上限；3D 损失全部来自 `Anchor3DHead`。
- 阅读多模态代码的要诀：先找 `extract_feat` 里的参数传递确定汇合点，再沿 VFE/融合层追坐标变换；属性名（如休眠的 `with_fusion` 缺陷）不可尽信，以实际调用链为准。

## 7. 下一步学习建议

- **u7-l1（train.py 与 Runner 训练全流程）**：本讲的 `load_from` COCO 预训练、`clip_grad` 梯度裁剪如何被 Runner 消费，将在那里展开；建议顺手用 MVXNet 配置（需 GPU）跑通 1 个 epoch 观察双模态 batch 的显存构成。
- **u8-l4（projects 生态：BEVFusion、DETR3D）**：本讲的 PointFusion 是「点级早期融合」，BEVFusion 展示「BEV 特征级融合」的另一条路线——两分支各自成图后在鸟瞰图上相加/拼接；对比两者能建立多模态融合方式的完整谱系。BEVFusion 同样继承 `MVXTwoStageDetector`，本讲的槽位知识直接可用。
- **源码延伸阅读**：`mmdet3d/models/layers/fusion_layers/vote_fusion.py` 的 `VoteFusion`（点级融合的另一个消费者，配合 6.2 的 VoteNet）与 `point_fusion.py` 末尾的 `voxel_sample`（L332-418，论文中 VoxelFusion 路线的锥体采样实现，把图像锥体栅格反投影回体素网格），可作为「同一融合思想的两种几何」对照读物。

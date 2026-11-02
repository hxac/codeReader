# CenterPoint：Anchor-free 的中心点范式

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 anchor-free 与 anchor-based 两种范式在「输出什么、怎么监督、怎么解码」三方面的本质差异。
2. 读懂 `CenterPoint` 检测器类的「薄壳」设计，以及它从 `MVXTwoStageDetector` 继承来的完整特征链路。
3. 剖析 `CenterHead` 的「按类别分组 + SeparateHead 多分支」结构，说出 center heatmap 与 reg / height / dim / rot / vel 各子分支的含义。
4. 手工推演 `get_targets_single`：给定一个 gt 3D 框，算出它落在热图的哪个格子、半径多少、10 维回归目标是什么。
5. 讲解 `CenterPointBBoxCoder.decode` 的两阶段 topK 峰值提取顺序，以及 CircleNMS 与 Rotate NMS 的区别。

## 2. 前置知识

### 2.1 回顾：anchor-based 范式在做什么

在上一单元（u5-l4）里，SECOND / PointPillars 用的 `Anchor3DHead` 是 anchor-based 范式：

- 先在 BEV 网格上**预先铺满几十万个 anchor**（SECOND KITTI 配置下约 84 万个），每个 anchor 有固定尺寸和两个朝向；
- 网络对每个 anchor 输出「是物体的置信度 + 相对 anchor 的 7 维偏移量」；
- 训练时用 `Max3DIoUAssigner` 先把 anchor 和 gt 框配对（正负样本分配），再只对正样本算回归损失；
- 推理时解码全部 anchor，再做 NMS 去重。

这套流程的两个「重」：anchor 数量巨大，且正负样本分配逻辑复杂。

### 2.2 anchor-free 的中心点思想

CenterPoint 把问题换了一个问法：**不再问「每个预设框里有没有物体」，而是问「每个 BEV 格子是不是某个物体的中心」**。

- 每个 BEV 格子只需回答：这里是某类物体中心的概率多大（center heatmap）；
- 如果是中心，再补充回归几个量：中心在格子内的亚格子偏移（reg）、离地高度（height）、物体尺寸（dim）、朝向的正弦/余弦（rot）、速度（vel）。

这样输出从「每 anchor 一组预测」变成「每格子一组预测」，监督也变成「在 gt 中心格子上画高斯核」，天然没有正负样本分配问题——这也是它能沿用上一讲 PointPillars / SECOND 的体素化骨干而只换头的原因。

### 2.3 高斯热图是什么

把「gt 中心是一个点」这一稀疏监督，换成「gt 中心附近一片区域都有渐弱的响应」：以中心格为圆心画一个二维高斯核，中心值 1、向外按

\[ g(x, y) = \exp\left(-\frac{(x - x_c)^2 + (y - y_c)^2}{2\sigma^2}\right), \qquad \sigma = \frac{2r + 1}{6} \]

衰减，\(r\) 是由物体尺寸算出的半径。配合 GaussianFocalLoss（对热图值高的位置给更高正样本权重），可以缓解「相邻物体中心太近互相干扰」的问题。

### 2.4 本讲的坐标与张量约定

- 全程 LiDAR 坐标系（nuScenes），点云范围 `[-51.2, -51.2, -5.0, 51.2, 51.2, 3.0]`，柱体尺寸 `[0.2, 0.2, 8]`，网格 `512 × 512 × 1`；
- 总降采样因子 `out_size_factor = 4`，所以 head 看到的特征图是 `128 × 128`：每个格子对应真实空间 `0.2 × 4 = 0.8` 米；
- nuScenes 的 gt 框是 9 维张量 `(x, y, z, w, l, h, yaw, vx, vy)`，注意 `LiDARInstance3DBoxes` 的 `z` 是**底面中心**高度，而 CenterPoint 内部监督用的是**重心**高度（本讲 4.3 会看到两者的换算）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `mmdet3d/models/detectors/centerpoint.py` | CenterPoint 检测器类，仅 60 余行的「薄壳」，全部逻辑继承自 MVXTwoStageDetector |
| `mmdet3d/models/detectors/mvx_two_stage.py` | 提供体素编码 → 伪图像 → backbone → neck → head 的完整链路与 loss/predict 分发 |
| `mmdet3d/models/dense_heads/centerpoint_head.py` | 本讲主角：SeparateHead / DCNSeparateHead / CenterHead（前向、目标编码、损失、解码后处理） |
| `mmdet3d/models/task_modules/coders/centerpoint_bbox_coders.py` | CenterPointBBoxCoder：topK 峰值提取 + 从格子坐标解码回米制 3D 框 |
| `mmdet3d/models/utils/gaussian.py` | `gaussian_2d` / `draw_heatmap_gaussian` / `gaussian_radius`，热图编码的数学工具 |
| `mmdet3d/models/utils/clip_sigmoid.py` | 数值稳定的 sigmoid，热图损失前使用 |
| `mmdet3d/models/layers/box3d_nms.py` | `circle_nms` 与 `nms_bev` 两种后处理 |
| `configs/_base_/models/centerpoint_pillar02_second_secfpn_nus.py` | head / train_cfg / test_cfg 的参数来源 |
| `configs/centerpoint/centerpoint_pillar02_second_secfpn_8xb4-cyclic-20e_nus-3d.py` | 完整实验配置（数据管线 + 模型增量覆盖） |
| `tests/test_models/test_task_modules/test_coders/test_centerpoint_bbox_coder.py` | coder 的最小可用性测试，本讲实践之一 |

## 4. 核心概念与源码讲解

### 4.1 CenterPoint 检测器：一个「薄壳」类

#### 4.1.1 概念说明

打开 `centerpoint.py` 你会发现它几乎什么都没做：注册 + 把十来个参数原样转发给父类。这是 MM 系配置驱动架构的典型做法——**检测器类只负责「组装哪些组件」，前向逻辑全部复用基类**。CenterPoint 与上一讲的 VoxelNet（PointPillars）共享完全相同的特征链路，差别只在头（CenterHead vs Anchor3DHead），因此类本身没有重写任何方法的必要。

值得注意的是，它的父类叫 `MVXTwoStageDetector`（多模态两阶段），而本讲配置里所有 `img_*` 组件都是 None、也没有 RoI head——「两阶段/多模态」的插槽空着不用，实际跑的是单阶段纯点云流程。这是「继承树上名字 ≠ 实际行为」的一个好例子，阅读源码时应以**配置里实例化了哪些组件**为准。

#### 4.1.2 核心流程

CenterPoint 一次 `loss` 前向的完整链路（沿用了 u5-l1 讲过的柱体链路）：

```text
点云列表 (B 个 (N,5) 张量)
  │  Det3DDataPreprocessor.voxel_layer        # 0.2m 柱体化，max 20 点/柱
  ▼
voxel_dict {voxels, coors, num_points}
  │  pts_voxel_encoder  = PillarFeatureNet    # 每柱 64 维特征
  │  pts_middle_encoder = PointPillarsScatter # 散回 512×512 伪图像
  │  pts_backbone       = SECOND              # 3 个 stage，输出 3 级 BEV 特征
  │  pts_neck           = SECONDFPN           # 对齐到 128×128 后 concat → (B,384,128,128)
  ▼
pts_bbox_head = CenterHead                    # 本讲主角
```

#### 4.1.3 源码精读

CenterPoint 类的全部内容——注册并把参数透传给父类：

- [mmdet3d/models/detectors/centerpoint.py:L8-L9](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/centerpoint.py#L8-L9) 用 `@MODELS.register_module()` 注册 `CenterPoint`，继承 `MVXTwoStageDetector`。docstring 写着 "Base class of Multi-modality VoxelNet"，这是从父类复制来的遗留文案，不必当真。
- [mmdet3d/models/detectors/centerpoint.py:L43-L65](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/centerpoint.py#L43-L65) `__init__` 只做一件事：把 `pts_voxel_encoder`、`pts_bbox_head` 等参数原样传给 `super().__init__`。

父类构建 head 时有一个关键细节——把 `model.train_cfg.pts` / `model.test_cfg.pts` 注入 head 配置：

- [mmdet3d/models/detectors/mvx_two_stage.py:L78-L83](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/mvx_two_stage.py#L78-L83) 在 `MODELS.build(pts_bbox_head)` 之前先 `pts_bbox_head.update(train_cfg=pts_train_cfg, test_cfg=...)`。**这就是为什么 CenterHead 内部能读到 `grid_size`、`gaussian_overlap` 等训练参数**——它们写在模型配置的 `train_cfg.pts` 里，而不是 head 自己的参数里。单独构建 head 做实验时必须手动完成同样的注入（本讲 4.3.4 实践会用到）。

特征提取与损失分发：

- [mmdet3d/models/detectors/mvx_two_stage.py:L186-L219](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/mvx_two_stage.py#L186-L219) `extract_pts_feat` 串起「柱体编码 → scatter → SECOND backbone → SECONDFPN neck」，返回特征列表（SECONDFPN 输出单级 `(B, 384, 128, 128)`）。
- [mmdet3d/models/detectors/mvx_two_stage.py:L249-L280](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/mvx_two_stage.py#L249-L280) `loss`：提取特征后把 `pts_feats` 与 `batch_data_samples` 一起交给 `self.pts_bbox_head.loss(...)`；图像分支因 `img_feats` 为空被跳过。

对照配置确认链路（与 u5-l1 的 PointPillars 骨架完全同构，仅 head 不同）：

- [configs/_base_/models/centerpoint_pillar02_second_secfpn_nus.py:L1-L36](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/centerpoint_pillar02_second_secfpn_nus.py#L1-L36) `voxel_size = [0.2, 0.2, 8]`、PillarFeatureNet、PointPillarsScatter(512×512)、SECOND、SECONDFPN。
- [configs/centerpoint/centerpoint_pillar02_second_secfpn_8xb4-cyclic-20e_nus-3d.py:L9-L26](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/centerpoint/centerpoint_pillar02_second_secfpn_8xb4-cyclic-20e_nus-3d.py#L9-L26) 顶层把 `point_cloud_range = [-51.2, -51.2, -5.0, 51.2, 51.2, 3.0]` 同步覆盖到 voxel_layer、encoder、coder、train_cfg、test_cfg——**改范围必须五处联动**，这是官方注释里也强调的约束。

#### 4.1.4 代码实践

**实践目标**：确认 CenterPoint 检测器是「配置组装出来的」，同一个类换个 head 配置就变成别的模型。

1. 打开 `configs/_base_/models/centerpoint_pillar02_second_secfpn_nus.py`，与 `configs/_base_/models/pointpillars_hv_secfpn_kitti.py`（u5-l1 讲过）逐项对比。
2. 在仓库根目录运行：

```bash
python -c "
import torch
from mmengine import Config
from mmdet3d.registry import MODELS
from mmdet3d.utils import register_all_modules
register_all_modules()
cfg = Config.fromfile('configs/centerpoint/centerpoint_pillar02_second_secfpn_8xb4-cyclic-20e_nus-3d.py')
model = MODELS.build(cfg.model)
print(type(model).__name__)                    # CenterPoint
print(type(model.pts_bbox_head).__name__)      # CenterHead
print(len(model.pts_bbox_head.task_heads))     # 6 个任务子头
print(model.img_backbone if hasattr(model, 'img_backbone') else '无图像分支')
"
```

3. 需要观察：类名 `CenterPoint`；`task_heads` 数量为 6；模型上**没有** `img_backbone` 属性（MVXTwoStageDetector 的多模态插槽未被占用）。
4. 预期结果：以上四个打印依次为 `CenterPoint`、`CenterHead`、`6`、`无图像分支`。构建纯 CPU 即可完成，无需权重。若报注册类缺失，先执行 `register_all_modules()`（示例中已包含）。

#### 4.1.5 小练习与答案

**练习 1**：`CenterPoint` 类为什么可以一行前向逻辑都不写？
答案：因为它的组件组合（柱体链路 + CenterHead）的前向顺序由父类 `MVXTwoStageDetector.extract_feat / loss / predict` 固定，子类只需在配置中选择「装哪些零件」；这正是注册器 + 配置驱动架构想要达到的效果。

**练习 2**：配置中 `train_cfg` 写在 `model` 块内，CenterHead 是怎么拿到 `grid_size` 的？
答案：见 `mvx_two_stage.py:L78-L83`，父类在 build head 前执行 `pts_bbox_head.update(train_cfg=train_cfg.pts)`，把训练参数注入 head 配置。

**练习 3**：如果想让 CenterPoint 真正变成两阶段/多模态，需要改什么？
答案：在配置中补上 `img_backbone`、`img_neck`、`img_roi_head` / `img_rpn_head` 等组件即可启用 `MVXTwoStageDetector` 的对应分支；代码无需改动（参考 u6-l6 的 MVXNet 讲义）。

### 4.2 CenterHead 与 SeparateHead：按类别分组的并行小头

#### 4.2.1 概念说明

`CenterHead` 由三部分组成：

1. **一个共享卷积** `shared_conv`：3×3 卷积把 neck 输出的 384 通道压到 64 通道；
2. **6 个并行的任务子头（task_heads）**：nuScenes 的 10 个类别被分成 6 组——`[car]`、`[truck, construction_vehicle]`、`[bus, trailer]`、`[barrier]`、`[motorcycle, bicycle]`、`[pedestrian, traffic_cone]`。每组一个子头，各自输出一张「组内类别数 × H × W」的热图；
3. **每个子头内部（SeparateHead）再分 6 个分支**：heatmap、reg、height、dim、rot、vel，各自是独立的小卷积栈。

为什么要按类别分组而不是 10 类一张热图？这是 CenterPoint 原论文的做法：**把尺寸差异大的类别（如 car 与 traffic_cone）拆到不同子头，可以给每组单独调 `min_radius`（circle NMS 用）等后处理参数**，同时也让每个子头的热图只包含形状相近的类别，降低学习难度。

「Separate」的含义是：分类（heatmap）和各回归量用**互相独立**的卷积分支预测，彼此不共享最后几层权重。

#### 4.2.2 核心流程

以本讲配置为例，各分支的输出形状（B 为 batch，H=W=128）：

| 分支 | 输出通道 | 每格含义 |
| --- | --- | --- |
| heatmap | 组内类别数（1 或 2） | 该格子是某类物体中心的概率（logit） |
| reg | 2 | 中心在格内的亚格子偏移 (Δx, Δy)，目标范围 [0,1) |
| height | 1 | 物体重心高度 z（米） |
| dim | 3 | 物体三尺寸，`norm_bbox=True` 时是 log 后的 (w, l, h) |
| rot | 2 | 朝向角的 (sin, cos) |
| vel | 2 | 雷达系下速度 (vx, vy)（米/秒） |

前向：`feats`（单级特征列表）→ `multi_apply(forward_single)` → 每级特征过 `shared_conv` → 6 个子头各自输出一个 dict → 最终返回「外层=特征级，内层=任务」的嵌套结构。

#### 4.2.3 源码精读

CenterHead 的组装过程：

- [mmdet3d/models/dense_heads/centerpoint_head.py:L308-L309](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/centerpoint_head.py#L308-L309) 从 `tasks` 里抽出每个任务的类别名列表 `self.class_names`（如 `[['car'], ['truck','construction_vehicle'], ...]`），这是后面按任务分组 gt 的依据。
- [mmdet3d/models/dense_heads/centerpoint_head.py:L316-L319](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/centerpoint_head.py#L316-L319) 构建两个损失（GaussianFocalLoss / L1）与 bbox_coder；`self.num_anchor_per_locs = [n for n in num_classes]` 这个变量名是 anchor 时代的遗物，实际上只是「每格的类别数」。
- [mmdet3d/models/dense_heads/centerpoint_head.py:L322-L338](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/centerpoint_head.py#L322-L338) 先建 3×3 共享卷积（384→64）；然后对每个任务，把 `common_heads` 深拷贝后**追加** `heatmap=(组内类别数, num_heatmap_convs)`，构建一个 SeparateHead。
- [mmdet3d/models/dense_heads/centerpoint_head.py:L340-L369](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/centerpoint_head.py#L340-L369) `forward_single`：特征过共享卷积后依次喂给 6 个子头；`forward` 用 `multi_apply` 支持多级特征（本配置 neck 只输出一级）。

SeparateHead 如何从 `heads` 字典生成卷积分支：

- [mmdet3d/models/dense_heads/centerpoint_head.py:L54-L82](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/centerpoint_head.py#L54-L82) 遍历 `heads` 的每个键，`(out_channels, num_conv)` 二元组指定输出通道数与卷积层数：前 `num_conv-1` 层是 64 通道的 ConvModule（含 BN+ReLU），最后一层 1×1/3×3 卷积输出到目标通道数。分支以 `self.__setattr__(head, conv_layers)` 动态挂到模块上。
- [mmdet3d/models/dense_heads/centerpoint_head.py:L87-L92](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/centerpoint_head.py#L87-L92) `init_weights` 只对 heatmap 分支的最后一层做特殊处理：bias 填 `-2.19`，因为 \(\mathrm{sigmoid}(-2.19) \approx 0.1\)——让训练初期热图输出接近全背景但仍留一点梯度，这是 CenterNet 系的经典初始化技巧。
- [mmdet3d/models/dense_heads/centerpoint_head.py:L94-L121](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/centerpoint_head.py#L94-L121) `forward` 逐分支前向，返回 `dict[str, Tensor]`。

配置里对应的写法：

- [configs/_base_/models/centerpoint_pillar02_second_secfpn_nus.py:L37-L49](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/centerpoint_pillar02_second_secfpn_nus.py#L37-L49) `tasks` 的 6 个分组；`common_heads=dict(reg=(2, 2), height=(1, 2), dim=(3, 2), rot=(2, 2), vel=(2, 2))`——每个元组就是 (输出通道, 卷积层数)。
- [configs/_base_/models/centerpoint_pillar02_second_secfpn_nus.py:L59-L63](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/centerpoint_pillar02_second_secfpn_nus.py#L59-L63) `separate_head=dict(type='SeparateHead', init_bias=-2.19, final_kernel=3)`、GaussianFocalLoss、L1Loss（weight 0.25）、`norm_bbox=True`。

进阶变体 DCNSeparateHead（`head-dcn` 系列配置使用）：

- [mmdet3d/models/dense_heads/centerpoint_head.py:L166-L201](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/centerpoint_head.py#L166-L201) 先把 `heatmap` 从回归分支字典中弹出；用两个可变形卷积 `feature_adapt_cls` / `feature_adapt_reg` 分别做「特征自适应」，分类热图走 `cls_head`，其余回归量走普通 SeparateHead——分类与回归看到的是**两份不同的变形特征**，缓解两任务对特征需求的冲突。
- [mmdet3d/models/dense_heads/centerpoint_head.py:L233-L240](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/centerpoint_head.py#L233-L240) `forward`：两路 DCN 适配 → 分类分支出 heatmap，回归分支出其余 5 个键。
- [configs/centerpoint/centerpoint_pillar02_second_secfpn_head-dcn-circlenms_8xb4-cyclic-20e_nus-3d.py:L3-L16](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/centerpoint/centerpoint_pillar02_second_secfpn_head-dcn-circlenms_8xb4-cyclic-20e_nus-3d.py#L3-L16) 覆盖 `separate_head` 为 DCNSeparateHead（DCN groups=4），同时把 `nms_type` 切到 `circle`——DCN 头与 CircleNMS 通常配套出现。

#### 4.2.4 代码实践

**实践目标**：数清 CenterHead 一共有多少个输出张量、多少个卷积分支。

1. 运行以下脚本（示例代码，CPU 即可）：

```python
import torch
from mmengine import Config
from mmdet3d.registry import MODELS
from mmdet3d.utils import register_all_modules
register_all_modules()

cfg = Config.fromfile(
    'configs/centerpoint/centerpoint_pillar02_second_secfpn_8xb4-cyclic-20e_nus-3d.py')
head = MODELS.build(cfg.model.pts_bbox_head)

x = torch.randn(1, 384, 128, 128)
outs = head([x])          # (num_levels,) 每级是 6 个 dict
print(len(outs))                          # 1 级特征
for t, task_out in enumerate(outs[0]):    # 6 个任务
    print(t, {k: tuple(v.shape) for k, v in task_out.items()})
```

2. 需要观察：每个任务的 dict 都有 6 个键（heatmap/reg/height/dim/rot/vel）；任务 0（car）的 heatmap 是 `(1, 1, 128, 128)`，任务 1 是 `(1, 2, 128, 128)`；reg 恒为 `(1, 2, 128, 128)`、dim 为 `(1, 3, 128, 128)`。
3. 预期结果：6 个任务 × 6 个分支 = 36 个输出张量，形状与 4.2.2 表格逐项吻合。
4. 若本机未装 mmcv/mmdet3d，本实践「待本地验证」，但可以对照 [configs/_base_/models/centerpoint_pillar02_second_secfpn_nus.py:L40-L49](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/centerpoint_pillar02_second_secfpn_nus.py#L40-L49) 手工推算出同样的结论。

#### 4.2.5 小练习与答案

**练习 1**：`common_heads=dict(reg=(2, 2), ...)` 中两个 2 各是什么意思？
答案：前者是该分支的输出通道数（reg 输出 Δx、Δy 两通道），后者是该分支的卷积层数（1 层 64 通道中间卷积 + 1 层输出卷积）。

**练习 2**：为什么 heatmap 分支最后一层要特意把 bias 初始化成 -2.19？
答案：\(\mathrm{sigmoid}(-2.19) \approx 0.1\)。让未训练的热图在所有格子上输出约 0.1 的低概率，避免初始阶段全图高响应导致的梯度爆炸/坍缩，是 CenterNet 系论文的标准技巧。

**练习 3**：10 个类别的热图被拆成 6 组有什么好处？
答案：(1) 尺寸/形态相近的类别放在同一张热图里学习，减少「大物体高斯核淹没小物体」的冲突；(2) 后处理可以按组使用不同的 NMS 半径（见 4.5.3 的 `min_radius=[4, 12, 10, 1, 0.85, 0.175]`）。

### 4.3 get_targets：从 gt 3D 框到高斯热图与 10 维回归目标

#### 4.3.1 概念说明

这是 CenterPoint 训练的核心：**把一组 gt 框「画」到 128×128 的特征网格上**，产生四样东西：

1. `heatmap`：每任务一张 `(类数, 128, 128)` 的浮点热图，gt 中心格为 1、周围按高斯衰减；
2. `anno_box`：`(500, 10)` 的回归目标矩阵，每行是一个 gt 的 10 维编码；
3. `ind`：`(500,)` 的展平索引，第 k 个 gt 中心在热图中的格子编号 \(y \times 128 + x\)；
4. `mask`：`(500,)` 的 0/1 有效位，标记前多少行是真实 gt。

10 维编码的布局（关键！）：

\[ (\Delta x, \ \Delta y, \ z, \ \log w, \ \log l, \ \log h, \ \sin\theta, \ \cos\theta, \ v_x, \ v_y) \]

其中 \((\Delta x, \Delta y) = (c_x, c_y) - (\lfloor c_x \rfloor, \lfloor c_y \rfloor)\) 是亚格子偏移；\(z\) 是**重心**高度；尺寸取对数（`norm_bbox=True`）让回归目标量级一致；角度用 sin/cos 双通道避免 ±π 的周期跳变（对比 u5-l4 anchor 头用「sin 差 + 方向二分类」的两段式方案）。

为什么训练时需要 `ind`/`mask`：CenterPoint 的回归损失**只在 gt 中心格上计算**（而不是全图 16384 个格子都算），所以要用 `ind` 把预测图上对应格子的回归值「捞」出来，用 `mask` 区分有效行与填充行。

#### 4.3.2 核心流程

单个样本（`get_targets_single`）的编码流程：

```text
gt_bboxes_3d (N, 9) + gt_labels_3d (N,)
  │ ① 取重心：cat(gravity_center, tensor[:, 3:])
  │    → (x, y, z+h/2, w, l, h, yaw, vx, vy)
  │ ② 按任务分组：task_masks / task_boxes / task_classes
  │    类别标签改为「组内 1 起」编号（0 留作背景）
  ▼ 对每个任务：
  │ ③ 物体两水平尺寸换算成格子数：l/(0.2×4), w/(0.2×4)
  │ ④ gaussian_radius((w,l), min_overlap=0.1)，下限 min_radius=2
  │ ⑤ 中心米制坐标 → 格子坐标：
  │      cx = (x + 51.2) / 0.2 / 4 = (x + 51.2) / 0.8
  │ ⑥ 越界检查（center_int 必须落在 [0,128)×[0,128)），越界跳过
  │ ⑦ draw_heatmap_gaussian：在 heatmap[cls_id] 上以 (cx,cy) 为圆心
  │    画高斯核（逐元素取 max，不叠加）
  │ ⑧ ind[k] = y*128 + x；mask[k] = 1
  │ ⑨ anno_box[k] = (Δx, Δy, z, log w, log l, log h, sin, cos, vx, vy)
  ▼
返回 6 组 (heatmap, anno_box, ind, mask)
```

注意 ⑥ 用的是**向零截断**（`center.to(torch.int32)`）而不是四舍五入，亚格子偏移恰好落在 [0,1) 内，与 reg 分支的 sigmoid-free 回归目标自洽。

`get_targets` 再用 `multi_apply` 扩展到 batch，并把嵌套列表「转置」：从「样本 × 任务」变成「任务 × batch 堆叠张量」，供 loss 按任务索引。

#### 4.3.3 源码精读

- [mmdet3d/models/dense_heads/centerpoint_head.py:L466-L477](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/centerpoint_head.py#L466-L477) 取出标签与框后立刻做关键换算：`torch.cat((gt_bboxes_3d.gravity_center, gt_bboxes_3d.tensor[:, 3:]), dim=1)`——前 3 列换成**重心坐标**（z 变为底面中心 + h/2），第 3 列之后原样保留；随后从 `train_cfg` 取出 `grid_size`、`point_cloud_range`、`voxel_size`，算出 `feature_map_size = 512 // 4 = 128`。
- [mmdet3d/models/dense_heads/centerpoint_head.py:L479-L501](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/centerpoint_head.py#L479-L501) 按任务分组：`task_masks` 用 `class_name.index(i) + flag` 把全局类别 id 映射到本任务；`task_classes` 给标签 `+1 - flag2`，让每个任务内部的类别编号从 1 开始（0 是背景），后面取热图通道时再 `-1`。
- [mmdet3d/models/dense_heads/centerpoint_head.py:L505-L516](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/centerpoint_head.py#L505-L516) 为每个任务分配四块缓冲：`heatmap (类数, 128, 128)`、`anno_box (500, 10)`、`ind (500,)`、`mask (500,)`。`max_objs = 500 × dense_reg(=1)`。
- [mmdet3d/models/dense_heads/centerpoint_head.py:L518-L531](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/centerpoint_head.py#L518-L531) 尺寸换算成格子单位后调用 `gaussian_radius((width, length), min_overlap=0.1)` 求半径，并用 `max(min_radius=2, ...)` 兜底。`min_overlap` 的含义是：高斯边缘响应处的两个框若都偏移到此处，IoU 仍不低于 0.1。
- [mmdet3d/models/dense_heads/centerpoint_head.py:L536-L557](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/centerpoint_head.py#L536-L557) 米制转格子坐标、截断取整、**越界即跳过**（`continue`，既不画热图也不占用 ind 槽位），最后 `draw_heatmap_gaussian(heatmap[cls_id], center_int, radius)`。
- [mmdet3d/models/dense_heads/centerpoint_head.py:L559-L580](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/centerpoint_head.py#L559-L580) 写入 `ind = y * feature_map_size[0] + x`（行主序展平）与 `mask=1`；然后拼 10 维目标：`center - [x, y]`（亚格子偏移）、`z`、`box_dim`（`log` 后）、`sin(rot)`、`cos(rot)`、`vx, vy`。速度直接取自 `task_boxes[idx][k][7:]`，即要求 gt 框是 9 维（nuScenes）；KITTI 7 维框不适用此 head 配置。
- [mmdet3d/models/dense_heads/centerpoint_head.py:L396-L444](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/centerpoint_head.py#L396-L444) `get_targets`：`multi_apply` 到 batch，再四次「转置 + stack」，把结果整理成按任务的 batch 张量。

高斯核的三件工具：

- [mmdet3d/models/utils/gaussian.py:L9-L25](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/utils/gaussian.py#L9-L25) `gaussian_2d` 按 \(\sigma = \mathrm{diameter}/6 = (2r+1)/6\) 生成奇数尺寸的核，小于 eps 的值截为 0。
- [mmdet3d/models/utils/gaussian.py:L28-L60](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/utils/gaussian.py#L28-L60) `draw_heatmap_gaussian`：按边界裁剪核与热图窗口后，用 `torch.max` 覆盖——**两个物体的高斯核重叠时取逐元素最大值而非相加**，保证热图峰值仍恰好为 1。
- [mmdet3d/models/utils/gaussian.py:L63-L93](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/utils/gaussian.py#L63-L93) `gaussian_radius`：CenterNet 的经典三候选半径（内切/外接/两角点三种偏移情形）取最小，这里是向量化 torch 实现，返回的半径是「格」为单位。

#### 4.3.4 代码实践

**实践目标**：给定一个手工构造的 car 框，逐项验证 4.3.2 流程图的每一步数值。

1. 运行以下脚本（示例代码，需要已安装 mmdet3d；CPU 即可）：

```python
import torch
from mmengine import Config
from mmengine.structures import InstanceData
from mmdet3d.registry import MODELS
from mmdet3d.structures import LiDARInstance3DBoxes
from mmdet3d.utils import register_all_modules
register_all_modules()

cfg = Config.fromfile(
    'configs/centerpoint/centerpoint_pillar02_second_secfpn_8xb4-cyclic-20e_nus-3d.py')
head_cfg = cfg.model.pts_bbox_head
head_cfg.train_cfg = cfg.model.train_cfg.pts   # 复刻 mvx_two_stage 的注入动作
head = MODELS.build(head_cfg)

# 一个 car：底面中心 (10, 5, -0.9)，(w,l,h)=(1.9,4.6,1.7)，yaw=0.3，v=(1,0)
gt = InstanceData()
gt.bboxes_3d = LiDARInstance3DBoxes(
    torch.tensor([[10.0, 5.0, -0.9, 1.9, 4.6, 1.7, 0.3, 1.0, 0.0]]), box_dim=9)
gt.labels_3d = torch.tensor([0])               # car

heatmaps, anno_boxes, inds, masks = head.get_targets_single(gt)
hm = heatmaps[0]                               # car 任务
print(hm.shape, hm.max().item())               # (1,128,128)  1.0
iy, ix = (hm[0] == 1).nonzero()[0]
print('中心格 =', (int(ix), int(iy)), 'ind =', inds[0][0].item())
print('anno_box =', anno_boxes[0][0])
print('热图 >0.5 的格子数 =', (hm[0] > 0.5).sum().item())
```

2. 需要观察并手工核对：
   - 米制→格子：\(c_x = (10+51.2)/0.8 = 76.5\)，\(c_y = (5+51.2)/0.8 = 70.25\)，截断后中心格应为 **(76, 70)**，`ind = 70×128 + 76 = 9036`；
   - 亚格子偏移：`anno_box[0][:2] ≈ (0.5, 0.25)`；
   - 重心高度：`anno_box[0][2] = -0.9 + 1.7/2 = -0.05`（验证「监督用重心而非底面」）；
   - 尺寸：`anno_box[0][3:6] ≈ (log 1.9, log 4.6, log 1.7) = (0.642, 1.526, 0.531)`；
   - 角度：`anno_box[0][6:8] ≈ (sin 0.3, cos 0.3) = (0.296, 0.955)`；速度 `(1.0, 0.0)` 原样进目标。
3. 预期结果：以上数值全部吻合；热图恰好一格为 1，其余按高斯衰减；`masks[0][:1] = 1`，其后为 0。
4. 高斯半径与 `>0.5` 格子数的精确值依赖 `gaussian_radius` 的浮点结果，**待本地验证**；但半径下限为 2（`min_radius=2`）可以先确定。

#### 4.3.5 小练习与答案

**练习 1**：gt 框的 z 是底面中心，为什么 get_targets 要先换算成重心？
答案：检测/跟踪更关心物体本体而非其贴地位置；且推理解码后 `predict_by_feat` 会再做 `z - h/2` 换回底面中心（见 4.5.3），训练与推理必须对称。

**练习 2**：两个 gt 的高斯核互相重叠，热图会变成两者之和吗？
答案：不会。`draw_heatmap_gaussian` 用 `torch.max` 覆盖，逐元素取最大（`gaussian.py:L59`），保证峰值恒为 1、监督信号不被叠加放大。

**练习 3**：一个中心格坐标算出来是 (128.3, 70.0) 的 gt 会发生什么？
答案：`center_int[0] = 128` 不满足 `0 <= 128 < 128`，直接 `continue`（`centerpoint_head.py:L553-L555`）——不画热图、不占 ind 槽位，等效于该目标对本帧无监督（正常情况下 PointsRangeFilter/ObjectRangeFilter 已在管线里把它过滤掉）。

### 4.4 loss_by_feat：Gaussian Focal 加「只算中心点」的 L1

#### 4.4.1 概念说明

CenterHead 的损失只有两项，且**按任务各算一份**：

1. **热图损失**：GaussianFocalLoss。它与普通 FocalLoss 的区别是把「0/1 硬标签」换成「0~1 的软标签（高斯值）」——把热图值为 \(y\) 的位置当作「以概率 y 是正样本」处理，从而让中心附近的格子也获得「接近但不等于中心」的监督。背景权重按 \( (1-y)^2 \) 衰减，远处背景几乎不产生损失。
2. **回归损失**：L1。**只在 gt 中心格上计算**：用 `ind` 把每个分支的预测图在 gt 格子处的值 gather 出来，与 `anno_box` 的 10 维目标做 L1，按 `code_weights` 加权（速度两维权重 0.2，其余 1.0）。

对比 anchor 头：没有 assigner、没有 sampler、没有 PseudoSampler——正样本由「是不是 gt 中心格」天然决定，这正是 anchor-free 的简化所在。

#### 4.4.2 核心流程

```text
outs = head(feats)                    # 6 个任务 × 6 分支
targets = get_targets(batch_gt)       # 4.3 的产物
对每个任务 task_id：
  ① heatmap 过 clip_sigmoid（数值稳定，避免 log(0)）
  ② loss_cls = GaussianFocalLoss(pred_hm, target_hm, avg_factor=max(正样本格数,1))
     其中 num_pos = (target_hm == 1) 的格子数
  ③ 把 reg/height/dim/rot/vel 六张预测图在通道维拼成 (B,10,H,W)
  ④ 展平后用 ind gather 出 (B,500,10)，与 anno_box 对齐
  ⑤ mask × code_weights 作为逐维权重，L1Loss，avg_factor=有效目标数
  ⑥ 记入 loss_dict['task{task_id}.loss_heatmap' / '.loss_bbox']
```

#### 4.4.3 源码精读

- [mmdet3d/models/dense_heads/centerpoint_head.py:L588-L607](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/centerpoint_head.py#L588-L607) `loss`：先 `self(pts_feats)` 前向拿 outs，从每个 `data_sample.gt_instances_3d` 收集真值，交给 `loss_by_feat`。
- [mmdet3d/models/dense_heads/centerpoint_head.py:L630-L637](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/centerpoint_head.py#L630-L637) 热图损失：`clip_sigmoid` 把预测压到 `[1e-4, 1-1e-4]`；`num_pos = heatmaps[task_id].eq(1)` 统计峰值格数作 `avg_factor`，最少为 1。
- [mmdet3d/models/utils/clip_sigmoid.py:L6-L18](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/utils/clip_sigmoid.py#L6-L18) `clip_sigmoid` 的实现：原地 `sigmoid_` 后 `clamp` 到 `[eps, 1-eps]`。
- [mmdet3d/models/dense_heads/centerpoint_head.py:L640-L659](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/centerpoint_head.py#L640-L659) 回归损失：六个分支在通道维 cat 成 `anno_box` 预测 → `(B,H,W,10)` 展平 → `_gather_feat(pred, ind)` 只取 gt 格子处的值 → `mask`（有效目标 + 非 NaN）乘 `code_weights`（train_cfg 里 `[1.0×8, 0.2, 0.2]`，最后两维正是速度）作为逐维权重 → L1。训练日志里会看到 `task0.loss_heatmap`、`task0.loss_bbox`、`task1...` 共 12 项。
- [configs/_base_/models/centerpoint_pillar02_second_secfpn_nus.py:L66-L75](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/centerpoint_pillar02_second_secfpn_nus.py#L66-L75) `train_cfg.pts` 里能看到全部编码参数：`gaussian_overlap=0.1`、`max_objs=500`、`min_radius=2`、`code_weights` 长度恰为 10。

#### 4.4.4 代码实践

**实践目标**：跑通一次真实损失计算，观察 12 项损失名。

1. 仓库自带整模型测试可直接复用（需 GPU 与编译好的 mmcv 算子）：

```bash
pytest tests/test_models/test_detectors/test_center_point.py -v
```

2. 该测试用 `create_detector_inputs` 伪造输入并断言 `task0..task5` 的两类损失均 ≥ 0，见 [tests/test_models/test_detectors/test_center_point.py:L40-L55](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_models/test_detectors/test_center_point.py#L40-L55)。
3. 无 GPU 时，可把 4.3.4 实践得到的 `heatmaps/anno_boxes/inds/masks` 与 4.2.4 实践的随机前向 `outs` 手工代入 `head.loss_by_feat(outs, [gt])`（示例代码），观察返回 dict 的 12 个键。
4. 需要观察：损失键名形如 `task0.loss_heatmap`；随机权重下热图损失量级明显大于回归损失（GaussianFocal 对全图 16384 格求和后按正样本数归一）。具体数值「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么速度两维的 `code_weights` 是 0.2 而不是 1.0？
答案：nuScenes 的速度标注噪声较大，且对 mAP 影响小（主要影响 NDS 中的速度误差项），降权防止它主导回归损失。

**练习 2**：CenterHead 里为什么找不到 assigner/sampler？
答案：anchor-free 的正样本定义就是「gt 中心所在格子」，由 `ind/mask` 直接给出，无需 IoU 分配；对比 u5-l4 中 Anchor3DHead 必须配 Max3DIoUAssigner + PseudoSampler。

**练习 3**：`clip_sigmoid` 为什么不在推理时也用？
答案：推理 `predict_by_feat` 用的是普通 `sigmoid`（L725），只关心相对排序和阈值；训练时进入 log 损失才需要防 0/1 截断带来的数值爆炸。

### 4.5 解码与后处理：CenterPointBBoxCoder、topK 与两种 NMS

#### 4.5.1 概念说明

推理时网络输出的是「6 任务 × 6 分支」的密集预测图，要变回「至多 500 个 3D 框」，分四步：

1. **热图取 sigmoid** 得到 0~1 的中心概率图；dim 分支若 `norm_bbox=True` 要过 `exp` 还原尺寸；
2. **解码（decode）**：对每个任务，从 `(类数, 128, 128)` 的热图里选出概率最大的 `max_num=500` 个峰值格，再从各回归图对应格子取出偏移/高度/尺寸/角度/速度，把格子坐标乘 `0.8`（out_size_factor × voxel_size）加回 `pc_range` 下限，还原成米制 9 维框 `(x, y, z, w, l, h, yaw, vx, vy)`，同时按 `score_threshold` 与 `post_center_range` 过滤；
3. **NMS**：`nms_type='rotate'` 走 BEV 旋转框 IoU NMS（`nms_bev`）；`'circle'` 走中心距离 NMS（`circle_nms`），每个任务有自己的距离阈值 `min_radius`；
4. **合并 6 个任务**：拼接框/分数，类别标签加回全局偏移，最后把 z 从重心换回底面中心（`z - h/2`），包装成 `LiDARInstance3DBoxes` 装进 `InstanceData`。

topK 是「两级」的：先在**每个类别内**各选 K 个峰值，再在这 `类数×K` 个候选中**跨类别**选最终的 K 个。这样保证小类别（如 traffic_cone）不会被大类别的低分峰值挤掉。

#### 4.5.2 核心流程

decode 中格子坐标 → 米制坐标的换算：

\[ x_{\text{metric}} = (i + \Delta x) \cdot f \cdot v_x + x_{\min}, \qquad f = 4,\ v_x = 0.2,\ x_{\min} = -51.2 \]

角度还原：\(\theta = \mathrm{atan2}(\sin\theta, \cos\theta)\)，天然落回 \((-\pi, \pi]\)，无需 anchor 头的 dir_offset 修正与方向分类分支。

整体后处理顺序（pillars02 默认 `rotate` NMS 配置）：

```text
sigmoid 热图 ─► 两级 topK(500) ─► gather 各回归分支 ─► atan2 还原 yaw
  ─► 格子→米制 ─► cat 成 9 维框 ─► score>0.1 且中心在 post_center_range 内
  ─► （rotate: score 阈值过滤 → BEV IoU NMS, thr=0.2, pre 1000/post 83
    │ circle: 中心距离平方 ≤ min_radius[task], post 83）
  └► 六任务合并：labels 加全局偏移；z ← z - h/2；包成 LiDARInstance3DBoxes
```

#### 4.5.3 源码精读

`CenterPointBBoxCoder.decode` 的主干：

- [mmdet3d/models/task_modules/coders/centerpoint_bbox_coders.py:L69-L102](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/centerpoint_bbox_coders.py#L69-L102) `_topk` 两级选择：第一次 `topk` 在每个类别的 `H×W` 上取 K 个（`topk_inds % (H*W)` 折回格号，再除/模 width 反解出 y、x）；第二次在 `类数×K` 上再取 K 个，并算出 `topk_clses`（候选属于哪个类别）。
- [mmdet3d/models/task_modules/coders/centerpoint_bbox_coders.py:L104-L118](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/centerpoint_bbox_coders.py#L104-L118) `_transpose_and_gather_feat`：把 `(B,C,H,W)` 的预测图变成 `(B,H·W,C)` 后按 `inds` 捞行，是所有回归分支共用的取样工具。
- [mmdet3d/models/task_modules/coders/centerpoint_bbox_coders.py:L152-L171](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/centerpoint_bbox_coders.py#L152-L171) decode 主体开头：`_topk` 拿分数/索引/类别/格坐标；有 reg 时 `xs += reg[...,0]`、`ys += reg[...,1]`（亚格子修正，无 reg 则加 0.5 取格心）；角度用 `atan2(sin, cos)` 还原。
- [mmdet3d/models/task_modules/coders/centerpoint_bbox_coders.py:L185-L197](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/centerpoint_bbox_coders.py#L185-L197) 格子→米制换算后按有没有速度分两种布局：KITTI 7 维 `[x, y, hei, dim(3), rot]`、nuScenes 9 维再拼 `vel`。
- [mmdet3d/models/task_modules/coders/centerpoint_bbox_coders.py:L203-L235](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/centerpoint_bbox_coders.py#L203-L235) 过滤：`score_threshold` 与 `post_center_range`（中心必须落在 `[-61.2, -61.2, -10, 61.2, 61.2, 10]` 内）双重过滤后，按样本组织成 `{'bboxes', 'scores', 'labels'}` 列表。注意 L120-L121：`encode()` 是空的——**训练目标由 head 的 get_targets 生成，coder 只负责解码**，这与 u5-l4 的 DeltaXYZWLHRBBoxCoder「encode/decode 成对」不同。

head 侧的调用与 NMS 分岔：

- [mmdet3d/models/dense_heads/centerpoint_head.py:L721-L750](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/centerpoint_head.py#L721-L750) `predict_by_feat` 对每个任务：热图 `sigmoid`、`norm_bbox` 时 `dim` 过 `exp`（与训练时的 `log` 对称）、拆出 rot 的 sin/cos 两通道，然后调用 `bbox_coder.decode`。
- [mmdet3d/models/dense_heads/centerpoint_head.py:L751-L782](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/centerpoint_head.py#L751-L782) NMS 二选一：`circle` 分支把 `(中心x, 中心y, score)` 送 `circle_nms`，阈值用 `min_radius[task_id]`；`rotate` 分支走 `get_task_detections`。
- [mmdet3d/models/dense_heads/centerpoint_head.py:L860-L881](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/centerpoint_head.py#L860-L881) rotate 路径：先 `score_threshold` 过滤，再把预测包成框对象取 `.bev`、经 `xywhr2xyxyr` 转成 NMS 需要的 5 维格式，最后 `nms_bev(boxes, scores, thresh=0.2, pre_max_size=1000, post_max_size=83)`。
- [mmdet3d/models/layers/box3d_nms.py:L234-L259](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/box3d_nms.py#L234-L259) `nms_bev`：按分数降序、先截 `pre_max_size`，底层用 mmcv 的旋转框 NMS 算子算 BEV IoU。
- [mmdet3d/models/layers/box3d_nms.py:L185-L228](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/box3d_nms.py#L185-L228) `circle_nms`：numba 加速的贪心法，按分数从高到低保留，抑制**中心距离平方** ≤ 阈值的候选（L218-L223 比较的是 \(dx^2 + dy^2\)）。注意：配置 `min_radius=[4, 12, 10, 1, 0.85, 0.175]` 传的是**平方距离**，4 对应实际 2 m、0.175 对应约 0.42 m——读源码才能发现的单位陷阱。

任务合并与 z 换算：

- [mmdet3d/models/dense_heads/centerpoint_head.py:L784-L807](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/centerpoint_head.py#L784-L807) 六任务结果拼接：`bboxes[:, 2] = bboxes[:, 2] - bboxes[:, 5] * 0.5` 把**重心 z 换回底面中心 z**（第 5 列是高度 h）——与 4.3 训练时的 gravity_center 换算严格对称；随后用 `batch_input_metas[i]['box_type_3d']`（即 LiDARInstance3DBoxes）包装；labels 逐任务累加 `flag`（前序任务的类别数）恢复全局 0 起类别编号，最终装进 `InstanceData` 的 `bboxes_3d / scores_3d / labels_3d`——正是 u1-l3 推理 demo 里见过的 `pred_instances_3d` 三件套。
- [configs/_base_/models/centerpoint_pillar02_second_secfpn_nus.py:L76-L89](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/centerpoint_pillar02_second_secfpn_nus.py#L76-L89) 默认 `nms_type='rotate'`、`nms_thr=0.2`、`pre/post_max_size=1000/83`、`min_radius` 六元组。顺带一提：`max_pool_nms=False` 这个键在当前代码里**没有任何地方消费**（全库 grep 仅出现在两个 config 里），属于遗留字段，读配置时不要被它迷惑。

#### 4.5.4 代码实践

**实践目标**：手工造一个「只有一个峰值」的热图，验证 decode 能精确还原出你编码时设定的框（encode-decode 往返）。

1. 先运行仓库自带的 coder 测试确认环境（CPU 即可，只依赖 torch）：

```bash
pytest tests/test_models/test_task_modules/test_coders/test_centerpoint_bbox_coder.py -v
```

该测试即 [tests/test_models/test_task_modules/test_coders/test_centerpoint_bbox_coder.py:L7-L33](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_models/test_task_modules/test_coders/test_centerpoint_bbox_coder.py#L7-L33)：随机 6 张图 decode 后断言每个样本恰好得到 500 个 `(500, 9)` 框、500 个分数与标签。

2. 再做往返实验（示例代码）：

```python
import torch
from mmdet3d.registry import TASK_UTILS

coder = TASK_UTILS.build(dict(
    type='CenterPointBBoxCoder',
    post_center_range=[-61.2, -61.2, -10.0, 61.2, 61.2, 10.0],
    max_num=500, score_threshold=0.1,
    pc_range=[-51.2, -51.2], out_size_factor=4, voxel_size=[0.2, 0.2]))

B, C, H, W = 1, 1, 128, 128
heat = torch.full((B, C, H, W), -10.0)
reg  = torch.zeros(B, 2, H, W)
heat[0, 0, 70, 76] = 5.0        # sigmoid 后 ≈0.993，(x格=76, y格=70)
reg[0, 0, 70, 76] = 0.5          # Δx，对应 4.3.4 里 cx=76.5
reg[0, 1, 70, 76] = 0.25         # Δy，对应 cy=70.25
hei = torch.zeros(B, 1, H, W);  hei[0, 0, 70, 76] = -0.05   # 重心 z
dim = torch.zeros(B, 3, H, W);  dim[0, :, 70, 76] = torch.tensor([0.642, 1.526, 0.531])  # log 尺寸
rots = torch.zeros(B, 1, H, W); rots[0, 0, 70, 76] = torch.sin(torch.tensor(0.3))
rotc = torch.zeros(B, 1, H, W); rotc[0, 0, 70, 76] = torch.cos(torch.tensor(0.3))
vel  = torch.zeros(B, 2, H, W); vel[0, 0, 70, 76] = 1.0

out = coder.decode(heat, rots, rotc, hei, dim, vel, reg=reg)[0]
box = out['bboxes'][0]
print('x =', box[0].item(), '  y =', box[1].item())   # 期望 10.0 / 5.0
print('z =', box[2].item(), ' 尺寸 =', torch.exp(box[3:6]).tolist())
print('yaw =', box[6].item(), ' 速度 =', box[7:].tolist())
```

3. 需要观察：`x = (76 + 0.5) × 0.8 − 51.2 = 10.0`，`y = (70 + 0.25) × 0.8 − 51.2 = 5.0`——与 4.3.4 编码时的米制中心完全一致；`exp` 后尺寸回到 `(1.9, 4.6, 1.7)`。
4. 预期结果：分数 ≈ 0.993 的一个候选框通过 score/post_center 双过滤被保留；把 `reg` 全置零重跑，`y` 会变成 `(70+0.5)×0.8−51.2 = 5.2`，直观展示亚格子偏移的量化误差（0.2 m）。

#### 4.5.5 小练习与答案

**练习 1**：两级 topK 为什么不能换成「把热图展平后直接全局取 500 个」？
答案：全局 topK 会被大类别（car 往往分数普遍更高）垄断，小类别可能一个峰值都进不了前 500；先类内取 K 再跨类取 K，保证每个类别都有代表，等价于按类别做了一次平衡采样。

**练习 2**：CircleNMS 与 Rotate NMS 各自的判据是什么？CenterPoint 为什么常用 Circle？
答案：Rotate 用 BEV 旋转框 IoU > `nms_thr`（0.2）抑制；Circle 用**中心距离平方** ≤ `min_radius[task]` 抑制。后者只需每任务一个标量参数（按类别尺寸给半径）、无需算旋转 IoU，速度更快，且对「中心近但朝向不同」的密集目标（如并排停车）更稳健；`-circlenms` 后缀配置即启用它。

**练习 3**：解码输出的 z 与最终 `LiDARInstance3DBoxes` 的 z 相差多少？为什么？
答案：相差 `h/2`（`centerpoint_head.py:L793` 做 `z - h/2`）。因为训练目标里的 z 是重心高度（4.3.3 的 gravity_center 换算），而 LiDARInstance3DBoxes 约定 z 为底面中心，两边必须换算对称，否则所有框会整体「悬空」半个高度。

## 5. 综合实践

**任务：画出 CenterPoint「gt 框 → 训练目标 → 推理解码」的双向数据流图，并用一个脚本两端对账。**

这是本讲规格中要求的核心实践，把 4.3（编码）与 4.5（解码）串成闭环：

1. **阅读与画图**：精读 [mmdet3d/models/dense_heads/centerpoint_head.py:L446-L586](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/centerpoint_head.py#L446-L586)（`get_targets_single`）与 [mmdet3d/models/task_modules/coders/centerpoint_bbox_coders.py:L123-L235](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/centerpoint_bbox_coders.py#L123-L235)（`decode`），画两张流程图（手绘或 mermaid）：
   - **编码图**：gt 9 维框 → 重心换算 → 任务分组 → 米制/格子坐标（含 0.8 m/格换算）→ 高斯半径 → 热图 + (ind, mask, 10 维 anno_box)；
   - **解码图**：6 任务热图 sigmoid → 两级 topK（先类内 K=500、再跨类 500）→ `_transpose_and_gather_feat` 取各分支值 → reg 亚格子修正 → atan2 还原 yaw → 格子→米制 → score 与 post_center_range 过滤 → circle/rotate NMS → 六任务合并（labels 加偏移、z − h/2）→ `pred_instances_3d`。
2. **脚本对账**：在 4.3.4 实践脚本末尾追加：把 `get_targets_single` 产出的 `heatmaps[0]`、`anno_boxes[0]` 中该目标的 10 维值，手工填进 4.5.4 的 decode 输入张量（heatmap 用 `(hm == 1).float() * 15 - 10` 造 logit——峰值处 +5、背景 −10——reg 用 `anno_box[:2]`，height/dim/rot/vel 用 `anno_box[2:]`），调用 `coder.decode` 后比较解码框与原始输入框 `(10, 5, -0.9+0.85, 1.9, 4.6, 1.7, 0.3, 1, 0)`。
3. **需要观察的现象**：除了 z 需要 `-h/2` 换回底面外，其余 8 维应完全复原（浮点误差 < 1e-4）；若把 reg 置零，x/y 各出现至多 0.8 m 的量化误差。
4. **预期结果**：两张图能覆盖本讲全部关键函数名与张量形状；脚本往返误差近零。至此你可以回答规格中的两问：编码过程即图一；topK 后处理顺序是「类内 topK → 跨类 topK → gather 回归值 → 阈值/范围过滤 → NMS → 跨任务合并」。若本机无环境，图仍可凭源码完成，脚本部分标注「待本地验证」。

## 6. 本讲小结

- `CenterPoint` 检测器是纯「薄壳」：特征链路（柱体化 → PillarFeatureNet → Scatter → SECOND → SECONDFPN）与 PointPillars 完全同构，全部行为由父类 `MVXTwoStageDetector` 与配置决定，`train_cfg.pts` 由父类注入 head。
- `CenterHead` 用「6 个按类别分组的任务子头 + 每头 6 个独立分支（heatmap/reg/height/dim/rot/vel）」替代 anchor 头的单一大分支；heatmap 末层 bias 初始化 -2.19（sigmoid≈0.1）是关键训练技巧。
- 训练目标由 `get_targets_single` 现场编码：gt 中心截断到 128×128 格子、按尺寸画高斯核（逐元素取 max），10 维回归目标 = (Δx, Δy, 重心 z, log 尺寸×3, sin, cos, vx, vy)，回归损失只在 gt 中心格上算——没有 assigner/sampler。
- `CenterPointBBoxCoder` 只解码不编码：两级 topK（类内→跨类）取 500 峰值，`(格子 + Δ) × 0.8 + pc_range` 还原米制坐标，atan2(sin, cos) 还原 yaw，天然规避角度周期与方向歧义问题。
- 后处理二选一：rotate NMS（BEV IoU，thr=0.2）或 circle NMS（中心距离平方 ≤ 每任务 `min_radius`，注意阈值是平方米单位）；六任务合并时 labels 加全局偏移、z 由重心换回底面（−h/2）。
- 与 anchor-based 对比一句话：SECOND 用 84 万预设框 + IoU 分配，CenterPoint 用 128×128×10 个中心格 + 高斯热图，参数量与后处理复杂度都显著下降。

## 7. 下一步学习建议

- **本单元内**：下一讲 u5-l6 进入 task_modules 的通用机制（各类 BBox Coder、Max3DIoUAssigner 与 Sampler），把 u5-l4 与本讲出现过的两种 coder 放到统一框架下对比。
- **横向阅读**：`mmdet3d/models/dense_heads/centerpoint_head.py` 中 `get_task_detections` 的 BEV NMS 与 u5-l7 将讲的 `box3d_nms.py` 全家（rotated/aligned/circle）互为表里，建议先把该文件的函数清单浏览一遍。
- **跟踪一条线**：从 `configs/centerpoint/` 目录里挑 `head-dcn-circlenms` 变体，对照本讲 4.2/4.5 找出它与默认配置的全部差异（DCNSeparateHead + circle NMS），体会「配置 = 论文变体」的组织方式。
- **向前衔接**：u6-l1 将离开 BEV 范式进入点云原生骨干（PointNet++），届时可回看本讲开头的范式对比表，把「体素/柱体 → BEV 热图」与「点 → 投票」两条技术路线并列理解。

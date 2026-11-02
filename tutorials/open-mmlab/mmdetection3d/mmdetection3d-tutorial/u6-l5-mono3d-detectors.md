# 单目 3D 检测：FCOS3D、SMOKE 与相机坐标系

## 1. 本讲目标

前面几个单元里，我们看到的检测器要么吃点云（VoteNet、PointNet++ 系），要么吃「点云 + 图像」（MVXNet）。本讲进入第三条输入路线：**只有一张 2D 图像，却要输出 3D 框**——单目 3D 检测（monocular 3D detection）。

读完本讲，你应该能够：

1. 说出 `SingleStageMono3DDetector` 如何跨库继承 MMDetection 的 `SingleStageDetector`，以及单目检测器为什么可以这样「薄」。
2. 读懂 `FCOSMono3DHead` 的逐像素分组回归设计：9 维回归向量如何拆成 offset/depth/size/rot/velo 五组，训练目标如何从 `centers_2d` 与 `depths` 编码出来。
3. 读懂 `FCOS3DBBoxCoder` 的两个关键设计：深度/尺寸的指数解码与先验，以及「局部朝向 → 全局朝向」的视线角修正。
4. 对比 `SMOKEMono3DHead` 的另一种范式：关键点热图 + 8 维回归，以及 `SMOKECoder` 的解析反投影。
5. 独立完成一个实践：实例化 FCOS3D head、用随机特征跑 forward、打印每个分支的张量形状，并把 bbox_coder 的解码结果反投影回图像平面验证 2D 框合理性。

## 2. 前置知识

### 2.1 单目 3D 检测的本质困难：深度不确定

一张图像是 3D 世界沿光线的投影。成像过程把三维压成二维，同一条光线上的所有点落在同一像素上——**单张图像在原理上无法唯一确定深度**。因此单目 3D 检测只能借助统计先验（类别平均尺寸、深度分布）与几何约束（相机内参）来「猜」出最合理的 3D 框。本讲会反复看到这一点的工程化身：

- 深度用 `exp()` 解码保证为正，或用「均值 + 标准差 × 偏移」围绕数据集先验回归；
- 尺寸围绕每类平均尺寸回归乘性偏移；
- 朝向被拆成「视线角（可由已知量算出）+ 局部朝向（网络回归）」。

### 2.2 相机坐标系与 cam2img 回顾

承接 [u2-l1](u2-l1-coordinate-systems.md)：CAM 坐标系 \(z\) 朝前、\(x\) 朝右、\(y\) 朝下，yaw 绕 \(y\) 轴。相机系 3D 框每行是

\[(x,\ y,\ z,\ x\_size,\ y\_size,\ z\_size,\ yaw)\]

其中 \(x\_size\) 是沿右方向的宽、\(y\_size\) 是沿下方向的高、\(z\_size\) 是沿前方向的长（见 `CameraInstance3DBoxes` 的类注释）。

相机内参 `cam2img`（KITTI 里常叫 P2/P3）把相机系点映到像素：

\[ u = \frac{f_x X}{Z} + c_x,\qquad v = \frac{f_y Y}{Z} + c_y \]

两个方向都常用到（均在 [mmdet3d/structures/bbox_3d/utils.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/utils.py#L184-L261)）：

- `points_cam2img`：3D → 像素（齐次坐标乘投影矩阵后除以深度）；
- `points_img2cam`：像素 + 深度（2.5D 点 \((u, v, d)\)）→ 相机系 3D 点，即「乘上深度再乘内参逆」。

单目 3D head 的解码核心就是 `points_img2cam`：网络预测像素偏移 + 深度，先拼出投影中心的 2.5D 坐标，再反投影回相机系。

### 2.3 两种 2D 检测范式：逐像素 vs 关键点热图

本讲的两个 head 正好是 2D 检测两大 anchor-free 范式的 3D 化：

| 范式 | 2D 代表 | 3D 化代表 | 检出方式 | 后处理 |
|---|---|---|---|---|
| 逐像素回归 | FCOS | FCOS3D | 每个特征像素都输出一组预测 | 逐层 topK + 旋转 NMS |
| 关键点热图 | CenterNet | SMOKE | 类别热图上取局部极大值 | topK + 阈值过滤（无 NMS） |

回忆 [u5-l5](u5-l5-centerpoint.md) 的 CenterPoint：它也是热图范式，只是画在 BEV 网格上；SMOKE 把同一思想搬到了图像平面。

### 2.4 单目 3D 的数据契约

承接 [u3-l5](u3-l5-loading-transforms.md) 与 [u3-l8](u3-l8-pack3d-det-inputs.md)。单目管线的两个特殊约定：

- `LoadImageFromFileMono3D` 加载图像并从 info 中取出 `cam2img` 等标定；
- `LoadAnnotations3D(..., with_bbox_depth=True)` 会额外产出 `centers_2d`（3D 框几何中心在图像上的投影，单位像素）和 `depths`（该中心的深度，单位米）。

FCOS3D 的 `Pack3DDetInputs` keys 里明确包含这两个键（见配置引用），它们最终出现在 `gt_instances_3d` 里，成为训练目标的原料。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| [mmdet3d/models/detectors/single_stage_mono3d.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/single_stage_mono3d.py#L13-L19) | 单目一阶段检测器基类，跨库继承 mmdet 的 `SingleStageDetector` |
| [mmdet3d/models/detectors/fcos_mono3d.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/fcos_mono3d.py#L13) | FCOS3D 检测器（注册名 `FCOSMono3D`），重写 `predict` |
| [mmdet3d/models/detectors/smoke_mono3d.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/smoke_mono3d.py#L8) | SMOKE 检测器（注册名 `SMOKEMono3D`），只有构造函数 |
| [mmdet3d/models/dense_heads/base_mono3d_dense_head.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/base_mono3d_dense_head.py#L13) | 单目 dense head 抽象基类：`loss → loss_by_feat`、`predict → predict_by_feat` 两段式 |
| [mmdet3d/models/dense_heads/anchor_free_mono3d_head.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/anchor_free_mono3d_head.py#L18) | 公共骨架：cls/reg/dir/attr 各分支的卷积塔与输出层 |
| [mmdet3d/models/dense_heads/fcos_mono3d_head.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/fcos_mono3d_head.py#L26) | FCOS3D head：逐像素分组回归、目标编码、分组损失、多级 NMS 推理 |
| [mmdet3d/models/dense_heads/smoke_mono3d_head.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/smoke_mono3d_head.py#L21) | SMOKE head：热图目标、激活函数内嵌、topK 解码、三组 corner 损失 |
| [mmdet3d/models/task_modules/coders/fcos3d_bbox_coder.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/fcos3d_bbox_coder.py#L13-L14) | FCOS3D 的 bbox coder：深度/尺寸解码 + 局部→全局朝向 |
| [mmdet3d/models/task_modules/coders/smoke_bbox_coder.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/smoke_bbox_coder.py#L13-L14) | SMOKE 的 bbox coder：解析反投影求 3D 中心 |
| [configs/_base_/models/fcos3d.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/fcos3d.py#L30-L73) | FCOS3D 模型四件套（ResNet-101-caffe + FPN + head） |
| [configs/_base_/models/smoke.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/smoke.py#L25-L61) | SMOKE 模型（DLANet + DLANeck + head） |
| [configs/fcos3d/fcos3d_r101-caffe-dcn_fpn_head-gn_8xb2-1x_nus-mono3d.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/fcos3d/fcos3d_r101-caffe-dcn_fpn_head-gn_8xb2-1x_nus-mono3d.py#L1-L53) | FCOS3D 完整实验配置（DCN 覆盖、pipeline、优化器） |

## 4. 核心概念与源码讲解

### 4.1 SingleStageMono3DDetector：跨库继承的单目检测器骨架

#### 4.1.1 概念说明

单目 3D 检测的输入是图像、主干是 2D CNN、特征金字塔是 FPN——这与 MMDetection 的 2D 检测完全同构。既然如此，mmdet3d 没有重造一遍「backbone → neck → dense head」的训练循环，而是**直接跨库继承 mmdet 的一阶段检测器**，只补上 3D 特有的两件事：

1. 从 mmdet3d 的 `batch_inputs_dict`（含 `imgs` 键）中取图；
2. 把 head 的输出同时写成 `Det3DDataSample` 的 `pred_instances_3d` 与 `pred_instances` 双契约（承接 [u2-l4](u2-l4-det3d-data-sample.md)）。

回忆 [u4-l2](u4-l2-detector-class-hierarchy.md) 讲过的 mmdet3d 自有 `SingleStage3DDetector`（面向点云）：单目分支**不走**那条线，而是挂在 mmdet 的 2D 体系上。这是「配置中 `mmdet.` 前缀跨库借用」在类继承层面的对应物——代码层面用 `from mmdet.models.detectors.single_stage import SingleStageDetector` 直接 import。

#### 4.1.2 核心流程

以 FCOS3D 为例的一次完整前向：

```text
Det3DDataPreprocessor（mean/std、bgr_to_rgb、pad 32）   ← u4-l3 已讲
        │  batch_inputs_dict['imgs']: (B, 3, H, W)
        ▼
SingleStageMono3DDetector.extract_feat
        │  mmdet.ResNet → mmdet.FPN（5 层，strides 8/16/32/64/128）
        ▼
FCOSMono3DHead.loss / .predict（由 mmdet SingleStageDetector 的
        │   train_step/test_step 模板调用，BaseMono3DDenseHead 拆 data_samples）
        ▼
add_pred_to_datasample → Det3DDataSample.pred_instances_3d / pred_instances
```

训练（`loss`）流程完全继承 mmdet，不需要任何重写；推理（`predict`）由 `FCOSMono3D` 重写，因为 head 返回的是 `(3D 结果, 2D 结果)` 二元组，而 mmdet 模板只预期单列表。

#### 4.1.3 源码精读

基类本体只有约 100 行。类定义与跨库继承：

[mmdet3d/models/detectors/single_stage_mono3d.py:L4-L14](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/single_stage_mono3d.py#L4-L14) —— 第 4 行从 mmdet 导入 `SingleStageDetector`，第 13–14 行注册并继承它。整个类的增量只有三个方法。

第一件事：改写特征提取入口。mmdet 的 `extract_feat` 接收裸 tensor，mmdet3d 的数据通路传的是字典：

[mmdet3d/models/detectors/single_stage_mono3d.py:L79-L94](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/single_stage_mono3d.py#L79-L94) —— 从 `batch_inputs_dict['imgs']` 取图，过 backbone 与可选 neck，返回多尺度特征元组。注意键名是 `imgs`（复数），正是 u4-l3 讲过的 `Det3DDataPreprocessor` 把 `img` 堆叠后的产物。

第二件事：双契约写回。

[mmdet3d/models/detectors/single_stage_mono3d.py:L61-L77](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/single_stage_mono3d.py#L61-L77) —— `add_pred_to_datasample` 先断言至少传入 3D/2D 一种结果（L61-63），缺失的一侧补空 `InstanceData`（L65-72），再逐样本写入 `pred_instances_3d` 与 `pred_instances`（L74-77）。这样评估器与可视化器永远能读到结构一致的字段。

第三件事（留给未来）：[mmdet3d/models/detectors/single_stage_mono3d.py:L96-L99](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/single_stage_mono3d.py#L96-L99) 的 `aug_test` 是 `pass`，源码注释 `TODO: Support test time augmentation`——单目 3D 目前不支持 TTA（对照 u7-l4）。

两个具体检测器都是薄壳：

- [mmdet3d/models/detectors/smoke_mono3d.py:L28-L43](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/smoke_mono3d.py#L28-L43) —— `SMOKEMono3D` 整个类只有一个透传构造参数的 `__init__`，训练与推理流程全部沿用父类模板。
- [mmdet3d/models/detectors/fcos_mono3d.py:L52-L101](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/fcos_mono3d.py#L52-L101) —— `FCOSMono3D` 额外重写 `predict`：L95 取特征，L96-97 调 `self.bbox_head.predict` 得到 `results_list, results_list_2d` 二元组，L98-100 用 `add_pred_to_datasample` 写回双字段。

head 侧的流程模板在 [mmdet3d/models/dense_heads/base_mono3d_dense_head.py:L16-L44](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/base_mono3d_dense_head.py#L16-L44)：`loss()` 先 forward 再 `loss_by_feat()`；`predict()` 先 forward 再 `predict_by_feat()`。其中 [L84-L97](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/base_mono3d_dense_head.py#L84-L97) 负责把 `batch_data_samples` 拆成 `batch_gt_instances_3d`（含 `bboxes_3d`、`centers_2d`、`depths` 等）、`batch_gt_instances`（2D 框）与 `batch_img_metas`（含 `cam2img`）三组输入——这正是 head 各实现方法的标准签名来源。

#### 4.1.4 代码实践

**实践目标**：用「grep 反查 + 类继承」确认单目检测器家族的挂载方式。

**操作步骤**（源码阅读型实践）：

1. 在仓库根目录执行 `grep -rn "SingleStageMono3DDetector" mmdet3d/models/detectors/`，列出所有直接子类；
2. 对每个子类再 `grep -n "def " ` 看它重写了哪些方法；
3. 画出继承链：`mmengine.BaseModel → mmdet.SingleStageDetector → SingleStageMono3DDetector → FCOSMono3D / SMOKEMono3D`（前两级在 mmdet/mmengine 包内，仓库里看不到源码，属跨库依赖）。

**需要观察的现象**：子类重写的方法数极少（SMOKE 为 0 个、FCOS3D 为 1 个 `predict`），说明「检测器 = 模板 + head」的分工。

**预期结果**：你会得出结论——单目 3D 的全部算法含量都在 `bbox_head` 与其 `bbox_coder` 里，本讲后续两节正是解剖它们。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `SingleStageMono3DDetector` 继承 mmdet 的 `SingleStageDetector`，而不是 mmdet3d 自己的 `SingleStage3DDetector`？

**答案**：`SingleStage3DDetector`（u4-l2）面向点云输入，其 `extract_feat`/数据通路围绕 `batch_inputs_dict['points']` 设计；单目输入是图像张量 `imgs`，与 2D 检测同构，直接复用 mmdet 的一阶段模板（backbone/neck/loss 循环）改造成本最低。这也呼应 u1-l4 的方法：带 `mmdet.` 前缀或直接 import mmdet 的组件，都是跨库借用的 2D 资产。

**练习 2**：`add_pred_to_datasample` 为什么要把缺失的一侧补成空 `InstanceData`，而不是留空不写？

**答案**：`Det3DDataSample` 是全流程统一契约（u2-l4）。下游（Evaluator、Visualizer、Inferencer）按固定字段名读取 `pred_instances_3d` / `pred_instances`；只输出 3D 的模型若不写 `pred_instances`，下游读取时行为不一致。补空对象保证字段总是存在且类型可预期。

**练习 3**：`aug_test` 为什么是 `pass`？这对用户意味着什么？

**答案**：源码标注 TODO，单目 3D 的 TTA 尚未实现。用户若在配置里给单目模型开 TTA（多增强合并，见 u7-l4 的 `MultiViewWrapper`）将得不到结果，需自行实现或改用单尺度测试。

### 4.2 AnchorFreeMono3DHead 公共骨架与 FCOSMono3DHead 的分组回归头

#### 4.2.1 概念说明

两个单目 head 都继承 `AnchorFreeMono3DHead`。这个公共骨架做的事很纯粹：**搭塔**。它在每个 FPN 层上搭若干卷积塔（tower），分别输出：

- 分类分支：`num_classes` 通道；
- 回归分支：按 `group_reg_dims` **分组**输出——这是 3D 与 2D FCOS 最大的差别，2D 只回归 4 个量，3D 要回归一族语义不同的量，各组的激活函数、尺度、损失都不同，所以物理上分成多个 `1×1` 卷积；
- 方向分支（可选）：2 通道；
- 属性分支（可选，nuScenes 专用）：`num_attrs` 通道；
- centerness 分支（仅 FCOS3D）：1 通道。

FCOS3D 在此之上把「每个像素预测什么」定义为一支 **9 维分组回归向量**，对应 nuScenes 相机系 9 维框 `(x, y, z, w, h, l, yaw, vx, vy)` 的训练参数化：

| 组 | 维数 | 物理含义 | 单位/空间 |
|---|---|---|---|
| offset | 2 | 格点到投影中心的像素偏移（归一化） | 像素 / stride |
| depth | 1 | 投影中心的深度 | 米（log 空间） |
| size | 3 | 宽、高、长 | 米（log 空间） |
| rot | 1 | 局部朝向 | 弧度 |
| velo | 2 | 速度 | 米/秒 |

#### 4.2.2 核心流程

训练一条样本时 head 内部发生的事：

```text
FPN 五层特征 (B, 256, H/8, W/8) ... (B, 256, H/128, W/128)
  │ forward_single（每层）
  │   cls塔 → cls_score (B,10,H,W)          reg塔×5组 → cat 成 bbox_pred (B,9,H,W)
  │   dir塔 → dir_cls_pred (B,2,H,W)        attr塔 → attr_pred (B,9,H,W)
  │   centerness塔 → (B,1,H,W)
  │   ★ bbox_coder.decode(bbox_pred, scale, stride, training, cls_score)  ← 训练也解码！
  ▼
get_targets：把 gt（含 centers_2d、depths、局部化 yaw）编码成逐像素目标
  │   正样本 = 落在中心采样框内 ∧ 满足该层回归范围 ∧ 距中心最近
  │   centerness 目标 = exp(-α · 相对距离)
  ▼
loss_by_feat：FocalLoss(cls) + SmoothL1(offset/depth/size/rotsin/velo 各组)
              + CE(dir) + CE(attr) + BCE(centerness)
```

推理路径见 4.3.3。**特别注意** `forward_single` 里的 `bbox_coder.decode`：与「推理才解码」的常见设计不同，FCOS3D 在训练前向内就做了解码（把原始输出变换到有物理量纲的空间），损失定义在解码后的空间上——深度因此可以直接用米制 SmoothL1 监督。

#### 4.2.3 源码精读

先看配置怎么描述这个 head：

[configs/_base_/models/fcos3d.py:L30-L73](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/fcos3d.py#L30-L73) —— `group_reg_dims=(2, 1, 3, 1, 2)`（L43 注释即 offset/depth/size/rot/velo），`reg_branch` 给每组单独的塔深（velo 组为空元组 `()`，直接一个 `1×1` 卷积），`dir_offset=0.7854`（π/4，方向分类的相位偏移），`pred_attrs=True`、`pred_velo=True`（nuScenes 专属），`bbox_coder=dict(type='FCOS3DBBoxCoder', code_size=9)`（L68）。`train_cfg.code_weight`（L76）是逐维损失权重，长度恰为 9。

公共骨架的输出层搭建：

[mmdet3d/models/dense_heads/anchor_free_mono3d_head.py:L263-L295](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/anchor_free_mono3d_head.py#L263-L295) —— `_init_predictor` 先搭分类塔与 `conv_cls`（L265-269）；再**按组**搭回归塔：`group_reg_dims` 有几组就建几个 `conv_regs`，每组输出维数等于该组维数（L270-285）；方向分支输出恒为 2 通道（L286-290）；属性分支输出 `num_attrs` 通道（L291-295）。

单层前向（塔与逐组卷积）：

[mmdet3d/models/dense_heads/anchor_free_mono3d_head.py:L372-L390](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/anchor_free_mono3d_head.py#L372-L390) —— 分类塔输出的 `cls_feat` 克隆后接 `conv_cls` 得 `cls_score`；回归塔输出的 `reg_feat` 对每一组克隆一次、过该组塔、`1×1` 卷积出该组预测，最后 `torch.cat` 拼回 (B, 9, H, W)。「克隆再分叉」保证各组共享塔特征但互不干扰。

FCOS3D 的增量层与 forward：

[mmdet3d/models/dense_heads/fcos_mono3d_head.py:L114-L125](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/fcos_mono3d_head.py#L114-L125) —— 新增 centerness 塔（L117-120），并给每层建 3 个可学习 `Scale`（L121-125，注释 `only for offset, depth and size regression`）：rot/velo 不需要尺度缩放，因为它们已经是无量纲量。

[mmdet3d/models/dense_heads/fcos_mono3d_head.py:L190-L202](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/fcos_mono3d_head.py#L190-L202) —— centerness 挂在回归特征上（`centerness_on_reg=True`，L190-194）；随后 L201-202 调 `self.bbox_coder.decode(bbox_pred, scale, stride, self.training, cls_score)`，训练与推理都执行，仅解码内容随 `training` 标志微调（见 4.3）。

训练目标编码（`_get_target_single`）三步走。第一步：真值准备与**朝向局部化**：

[mmdet3d/models/dense_heads/fcos_mono3d_head.py:L851-L874](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/fcos_mono3d_head.py#L851-L874) —— 从 `gt_instances_3d` 取出 `bboxes_3d/labels_3d/centers_2d/depths/attr_labels`（L854-858），L873-874 把全局 yaw 改写为局部 yaw：

\[ yaw_{local} = yaw_{global} - \arctan\frac{X}{Z} \]

其中 \(\arctan(X/Z)\) 是目标相对相机光轴的视线角（CAM 系 \(z\) 前 \(x\) 右）。第二步：拼 9 维回归目标：

[mmdet3d/models/dense_heads/fcos_mono3d_head.py:L890-L893](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/fcos_mono3d_head.py#L890-L893) —— `bbox_targets_3d = cat((Δx, Δy, depth, gt_bboxes_3d[..., 3:]))`，即 (格点−中心的像素差, 深度, w, h, l, 局部 yaw, vx, vy)。第三步：正负样本三条件与 centerness：

[mmdet3d/models/dense_heads/fcos_mono3d_head.py:L901-L955](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/fcos_mono3d_head.py#L901-L955) —— ① 中心采样：正样本须落在以投影中心为心、半径 \(r \times stride\) 的「中心框」内（L901-928）；② 回归范围：该点到 2D 框四边的最大距离须落在本层 `regress_ranges` 区间（L930-934，近处小物体归浅层、远处归深层）；③ 歧义消解：多个真值重叠时取 `bbox_targets_3d[..., :2]` 距离最小者，其余置背景（L936-947）。centerness 目标（L951-955）：

\[ c^{*} = \exp\left(-\alpha \cdot \frac{\sqrt{\Delta_x^2 + \Delta_y^2}}{\sqrt{2}\; s\, r}\right),\qquad \alpha = 2.5 \]

离中心越远衰减越快——推理时它与分类分数相乘压制偏离中心的低质量预测。最后注意 [L838-L840](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/fcos_mono3d_head.py#L838-L840)：`norm_on_bbox=True` 时目标的 offset 除以该层 stride——这与预测侧「训练时不乘 stride」严格配对（4.3.3）。

分组损失：

[mmdet3d/models/dense_heads/fcos_mono3d_head.py:L398-L424](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/fcos_mono3d_head.py#L398-L424) —— 对 offset、depth、size、rotsin、velo 五组分别调 `loss_bbox`（SmoothL1），各自可被 `code_weight` 加权；rotsin 一组先经 `add_sin_difference`（[L207-L230](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/fcos_mono3d_head.py#L207-L230)）把预测与目标的角度差改写成 \(\sin\theta_p\cos\theta_t\) 形式——这正是 u5-l4 在 Anchor3DHead 里见过的「正弦差回归」，用于消除角度 \(2\pi\) 周期带来的损失突变。方向二分类（`get_direction_target`，[L232-L267](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/fcos_mono3d_head.py#L232-L267)）把 yaw 按 \(\pi\) 折叠成 0/1 两类，解决「头尾不分」，同样是 u5-l4 的老朋友。

#### 4.2.4 代码实践

**实践目标**：数清 FCOS3D head 每个 FPN 层的输出通道构成。

**操作步骤**（源码阅读型实践）：

1. 读 [configs/_base_/models/fcos3d.py:L30-L73](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/fcos3d.py#L30-L73)，抄下 `num_classes`、`group_reg_dims`、`use_direction_classifier`、`pred_attrs`；
2. 读 [anchor_free_mono3d_head.py:L286-L295](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/anchor_free_mono3d_head.py#L286-L295) 与 [fcos_mono3d_head.py:L114-L125](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/fcos_mono3d_head.py#L114-L125)；
3. 手算每层总输出通道数（不含特征塔）。

**需要观察的现象**：cls 10 + reg 9 + dir 2 + attr 9 + centerness 1 = 31 通道/像素/层；FPN 共 5 层，即每像素 155 个输出量。

**预期结果**：写出一个 5 行 × 5 列的表格（行=层 stride，列=分支通道数）。通道数与层无关，只有空间尺寸随 stride 变化。本实践纯纸面推导即可完成；用随机张量实际验证的方式放在第 5 节综合实践。

#### 4.2.5 小练习与答案

**练习 1**：`group_reg_dims` 为什么不直接设成一个数（如 9），而要拆成 `(2, 1, 3, 1, 2)`？

**答案**：分组让每组拥有独立卷积塔、独立 `Scale`、独立损失与 `code_weight`，还可按需关闭某组的塔（velo 组 `()`）。深度、尺寸、朝向、速度的量纲与最优学习动态完全不同，物理分组是自然选择。SMOKE 则反过来用单组 8 维（见 4.4），说明这不是硬性约束而是设计权衡。

**练习 2**：FCOS3D 的正负样本分配与 u5-l6 的 `Max3DIoUAssigner` 有何本质区别？

**答案**：FCOS3D 无 anchor、无 IoU——每个像素按「中心采样框 ∧ 回归范围 ∧ 最近中心」三条规则直接定身份，是逐像素的几何判定；`Max3DIoUAssigner` 是 anchor 与 gt 之间按重叠阈值分配。二者都需要处理歧义（一个点/anchor 对多个 gt），FCOS3D 用最近距离，IoU 系用最大重叠。

**练习 3**：为什么 centerness 要「挂」在回归特征上（`centerness_on_reg=True`）而不是分类特征上？

**答案**：centerness 度量的是回归质量（离中心多远），与回归分支共享特征更合理；且源码注释引用了 FCOS 官方 issue #89 的讨论——放在回归分支上训练更稳定。这是从 2D FCOS 继承下来的实证选择。

### 4.3 FCOS3DBBoxCoder：深度/尺寸的指数解码与局部→全局朝向

#### 4.3.1 概念说明

`FCOS3DBBoxCoder` 解决单目 3D 的两个核心难题：

1. **深度与尺寸必须为正且有合理量级**。网络裸输出是无界实数，直接当米数用会出负深度。解法：在 log 空间回归、解码时取指数——

\[ d = e^{o_d},\qquad (w, h, l) = e^{o_s} \]

log 空间中均匀的预测误差对应物理空间的**相对**误差，这对跨度大（几米到几十米）的量比线性回归更友好。若数据集统计了深度/尺寸先验（`base_depths`/`base_dims`），则改为围绕先验的线性回归 \(d = \mu + \sigma \cdot o_d\)，进一步缩小网络需要覆盖的输出范围。

2. **朝向与视角耦合**。相机系全局 yaw 同时依赖物体自身朝向和它出现在视野哪个方位（同一辆车在画面左侧和正前方，全局 yaw 差别很大）。解法：把全局 yaw 分解为

\[ yaw_{global} = \underbrace{\arctan\frac{u - c_x}{f_x}}_{\text{视线角，由内参与像素位置算出}} + \; yaw_{local} \]

网络只回归与位置无关的局部 yaw，视线角在解码时精确补上（由 \(u - c_x = f_x X / Z\) 可证 \(\arctan\frac{u-c_x}{f_x} = \arctan\frac{X}{Z}\)，与 4.2.3 的目标编码严格互逆）。

#### 4.3.2 核心流程

`decode`（前向内调用，逐层）：

```text
raw (B,9,H,W)
  ├─ offset × scale₀、depth × scale₁、size × scale₂     # 可学习尺度（3 组）
  ├─ depth: exp(o)                       # 无先验时
  │        或 μ + σ·o                     # base_depths 单先验
  │        或 μ_c + σ_c·o                 # 多类先验，按 cls_score argmax 选类
  ├─ size: exp(o)（可再乘每类 base_dims）
  └─ 训练时 offset 不乘 stride；推理时 offset × stride    # 与 target 的 /stride 配对
```

`decode_yaw`（仅推理、合层后调用）：

```text
local yaw（标量）+ dir 二分类
  → limit_period 折叠到 (-π/2, π/2] 并按 dir 补 π        # 解决头尾歧义
  → + atan2(u - c_x, f_x)                                # 补视线角成全局 yaw
```

#### 4.3.3 源码精读

coder 本体：

[mmdet3d/models/task_modules/coders/fcos3d_bbox_coder.py:L42-L72](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/fcos3d_bbox_coder.py#L42-L72) —— `decode` 签名带着 `training` 与 `cls_score`：L67-72 只对 offset/depth/size 三组乘可学习 scale；L74-89 处理深度——无先验走 `exp()`（L74-75），单先验走 `mean + offset * std`（L76-79），多类先验按 `cls_score.max(dim=1)[1]` 选出每像素所属类的 \((\mu_c, \sigma_c)\)（L80-89）。

[mmdet3d/models/task_modules/coders/fcos3d_bbox_coder.py:L91-L108](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/fcos3d_bbox_coder.py#L91-L108) —— 尺寸走 `exp()`（L91），有 `base_dims` 时再乘每类平均尺寸（L92-99）；L101-107 是 `norm_on_bbox` 的关键配对：**只有推理时**才把 offset 乘 stride——因为训练目标在 [fcos_mono3d_head.py:L838-L840](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/fcos_mono3d_head.py#L838-L840) 已除以 stride，两侧必须在同一空间比较。

顺带一提 [L38-L40](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/fcos3d_bbox_coder.py#L38-L40)：`encode` 是 `pass` 加 TODO——FCOS3D 的目标编码没有走 coder，而是内联在 head 的 `_get_target_single` 里（4.2.3）。这与 u5-l6 见过的「coder 负责编解码双向」的 VoteNet 用法不同，是该 coder 的现状事实。

[mmdet3d/models/task_modules/coders/fcos3d_bbox_coder.py:L110-L136](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/fcos3d_bbox_coder.py#L110-L136) —— `decode_yaw`：L128-131 先把局部 yaw 减 `dir_offset` 折叠进 \((-\frac{\pi}{2}, \frac{\pi}{2}]\)，再加回 `dir_offset` 并按方向分类结果补 \(\pi\)（继承自 u5-l4 的方向二分类）；L133-134 补视线角 \(\arctan\frac{u - c_x}{f_x}\) 得全局 yaw。

推理链 `_predict_by_feat_single` 把 coder、反投影与 NMS 串起来：

[mmdet3d/models/dense_heads/fcos_mono3d_head.py:L617-L655](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/fcos_mono3d_head.py#L617-L655) —— L617 从 `img_meta` 取 `cam2img`；L645-655 每层先按 `nms_pre=1000` 用「分类分 × centerness」选 topK；L657 做关键一步**偏移换中心**：`bbox_pred[:, :2] = points - bbox_pred[:, :2]`（网络学的是「格点 − 中心」，故中心 = 格点 − 预测），随后 L661 把前三列 \((u, v, d)\) 经 `points_img2cam` 反投影成相机系 \((X, Y, Z)\)——此刻 9 维向量已是一支完整的相机系框参数 \((X, Y, Z, w, h, l, yaw_{local}, v_x, v_y)\)。格点坐标由 [L717-L739](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/fcos_mono3d_head.py#L717-L739) 的 `_get_points_single` 给出：每格中心 \( (j \cdot s + \lfloor s/2 \rfloor,\ i \cdot s + \lfloor s/2 \rfloor) \)。

[mmdet3d/models/dense_heads/fcos_mono3d_head.py:L673-L699](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/fcos_mono3d_head.py#L673-L699) —— 合层后 L674-679 补齐 4×4 内参并调 `decode_yaw` 把局部 yaw 换全局；L681-683 用 `xywhr2xyxyr(box.bev)` 把相机系 3D 框的 BEV 投影（x-z 平面旋转框）转成旋转 2D 框，供旋转 NMS 使用；L694-698 调 `box3d_multiclass_nms`（u5-l7 讲过的多类去重入口），分数是「分类分 × centerness」（L694）。

[mmdet3d/models/dense_heads/fcos_mono3d_head.py:L701-L713](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/fcos_mono3d_head.py#L701-L713) —— 最终用 `img_meta['box_type_3d']`（即 `CameraInstance3DBoxes`）以 `origin=(0.5, 0.5, 0.5)` 包装成框对象。注意注释：**投影中心 `centers_2d` 对应的是几何中心**，所以这里用几何中心 origin 而非底面中心——与 u5 系列点云框默认底面中心的约定不同。

#### 4.3.4 代码实践

**实践目标**：手推一遍「像素 + 深度 → 相机系」的反投影几何。

**操作步骤**（纸面推导）：

1. 设内参 \(f_x = f_y = 700,\ c_x = 600,\ c_y = 180\)（量级取自 KITTI P2）；
2. 某格点预测 center2d \(=(620, 200)\) 像素、depth \(= 35\) 米；
3. 用 `points_img2cam` 的算法（[mmdet3d/structures/bbox_3d/utils.py:L248-L261](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/utils.py#L248-L261)）手工计算：先拼未归一化坐标 \((u \cdot d,\ v \cdot d,\ d)\)，再左乘 3×3 内参的逆；
4. 把结果代回针孔公式验算。

**需要观察的现象**：3×3 内参逆乘 \((u d, v d, d)^{\mathsf T}\) 应恰好消掉 \(f_x, c_x\)，得到 \((X, Y, Z) = \left(\frac{(u-c_x)d}{f_x}, \frac{(v-c_y)d}{f_y}, d\right)\)。

**预期结果**：\((X, Y, Z) = (2.0,\ 2.0,\ 35)\) 米。验算 \(u = 700 \times 2 / 35 + 600 = 640\)？——注意上面代入 \(u=620\) 时 \(X = (620-600) \times 35/700 = 1.0\)，\(Y = (200-180) \times 35/700 = 1.0\)，即 \((1, 1, 35)\)；再投影回去 \(u = 700 \times 1/35 + 600 = 620\)，\(v = 700 \times 1/35 + 180 = 200\)，往返一致。这一恒等式就是 `points_img2cam`/`points_cam2img` 互为逆的几何内核。

#### 4.3.5 小练习与答案

**练习 1**：为什么深度用 `exp` 解码而不是直接线性回归米数？

**答案**：保证严格为正；且 log 空间的常数误差对应物理空间的固定**相对**误差（\(d = e^o \Rightarrow \delta d / d = \delta o\)），对「近处 5 米、远处 60 米」跨两个数量级的回归目标，比线性空间的绝对误差更合理。有可靠先验（`base_depths`）时退化为先验附近的线性回归，二者不矛盾——先验负责定标，网络负责修正。

**练习 2**：`norm_on_bbox` 在 target 侧（÷stride）与 decode 侧（仅推理 ×stride）为什么必须这样错开？

**答案**：训练时预测与目标要同空间比较：目标除了 stride，预测就不乘 stride，二者都在「归一化像素」空间算损失；推理时没有 target，解码必须把归一化 offset 还原成真像素偏移才能与格点坐标相加减。若两侧同时乘/除，损失空间与推理空间会错位一个 stride 因子。

**练习 3**：视线角公式为什么用 \(\arctan\frac{u-c_x}{f_x}\) 而不直接用 \(\arctan\frac{X}{Z}\)？

**答案**：推理时只有像素坐标与内参可用，\((X, Z)\) 尚未求出。由针孔模型 \(u - c_x = f_x X / Z\) 知两者恒等，故用像素域可直接计算的版本；训练编码（[fcos_mono3d_head.py:L873-L874](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/fcos_mono3d_head.py#L873-L874) 用 \(\arctan(X/Z)\)、解码用像素版）恰好构成互逆变换。

### 4.4 SMOKEMono3DHead：热图范式与 SMOKECoder

#### 4.4.1 概念说明

SMOKE 走 CenterNet 路线：不再让每个像素都出完整预测，而是

- **分类即定位**：网络输出 \((B, num\_classes, H/4, W/4)\) 的中心热图，物体的投影 3D 中心在其类别通道上是一个经高斯溅射的亮斑；
- **回归只在中心学**：一张 8 通道回归图，只在热图峰值位置被读取。

其 8 维回归布局与 FCOS3D 的 9 维不同，且深度放在第 0 维：

| 通道 | 维数 | 物理含义 |
|---|---|---|
| 0 | 1 | 深度偏移（围绕先验） |
| 1–2 | 2 | 投影中心的像素偏移 |
| 3–5 | 3 | 尺寸偏移（log 空间，围绕类别均值） |
| 6–7 | 2 | 局部朝向的单位向量（sin, cos 二维） |

与 FCOS3D 的对照：朝向从「标量 + 方向二分类」变成「二维单位向量」；无 centerness、无方向分类、无属性；特征只用 DLA neck 的一层 4 倍下采样图（无 FPN 多层）。

#### 4.4.2 核心流程

训练：

```text
DLANet → DLANeck → 单层 64 通道特征 (B, 64, H/4, W/4)
  │ forward_single：cls热图分支 / 8通道回归分支
  │   cls: sigmoid + clamp(1e-4, 1-1e-4)          ← 激活内嵌在前向里
  │   dim 通道: sigmoid() - 0.5                   ← 尺寸偏移对称化
  │   ori 通道: F.normalize                        ← 单位向量
  ▼
get_targets：gt 投影中心 ×(1/4) 溅射到热图（半径由 2D 框缩放后的高斯半径决定）
get_predictions：在 gt 中心位置 gather 回归 → SMOKECoder.decode → 三组「半真半预测」框
loss：GaussianFocalLoss(热图) + L1(三组框的 8 个角点)
```

推理：

```text
热图 → 3×3 局部极大值 → topK=100 峰值
  → 峰值处 gather 8 维回归 → SMOKECoder.decode（反 trans_mat + 乘深度 + 反内参）
  → score > 0.25 过滤 → CameraInstance3DBoxes(origin=(0.5,0.5,0.5))
（无 NMS：局部极大值 + 每峰一框天然去重）
```

#### 4.4.3 源码精读

配置先行：

[configs/_base_/models/smoke.py:L25-L61](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/smoke.py#L25-L61) —— `dim_channel=[3,4,5]`、`ori_channel=[6,7]` 声明 8 维回归中尺寸/朝向占据的通道（L29-30）；`group_reg_dims=(8,)` 单组（L39）；`bbox_code_size=7`（KITTI 无速度，L43）；`bbox_coder=dict(type='SMOKECoder', base_depth=(28.01, 16.32), base_dims=...)`（L46-51）——先验直接写在配置里：28.01/16.32 是 KITTI 训练集深度的均值/标准差，`base_dims` 是车/行人/骑车人三类的平均 \((l, h, w)\)。`train_cfg=None`（L60）、`test_cfg` 只有 `topK=100` 与 `local_maximum_kernel=3`（L61）——没有 NMS 参数。

前向内嵌激活：

[mmdet3d/models/dense_heads/smoke_mono3d_head.py:L102-L121](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/smoke_mono3d_head.py#L102-L121) —— 调父类 `forward_single` 拿裸输出后：cls 热图先 sigmoid 再 clamp 到 \([10^{-4}, 1-10^{-4}]\)（L113-114，防 log(0) 与梯度饱和）；`dim_channel` 三通道过 `sigmoid() - 0.5`（L116-117，把乘性尺寸偏移的中心移到 0，对应解码时的 `exp`）；`ori_channel` 两通道做 `F.normalize`（L119-120，约束成单位向量——方向只关心指向不关心幅度）。对比 FCOS3D：这些激活全部推迟到推理/损失里，SMOKE 则直接焊在 forward 上，因此它训练与推理读到的回归图永远是「已激活」的。

热图目标：

[mmdet3d/models/dense_heads/smoke_mono3d_head.py:L394-L421](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/smoke_mono3d_head.py#L394-L421) —— 特征图是原图的 1/4（L394-395），gt 投影中心乘 `width_ratio` 投到特征图；对每个 gt 取其 2D 框缩放后的高斯半径（`min_overlap=0.7`，L414-415），在对应类别通道上溅射高斯（L418-419）；`avg_factor = max(1, 热图上值为 1 的像素数)`（L421）作为 GaussianFocalLoss 的归一。这两个高斯工具（`gaussian_radius`/`gen_gaussian_target`）直接从 mmdet 借用——与 u5-l5 CenterPoint 在 BEV 上做热图是同一套代码资产。

推理解码：

[mmdet3d/models/dense_heads/smoke_mono3d_head.py:L149-L180](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/smoke_mono3d_head.py#L149-L180) —— L149 断言只有单层输出；L150-157 从 metainfo 堆叠 `cam2img` 与 `trans_mat`；L158-165 调 `_decode_heatmap(topk=100, kernel=3)`；L174-177 只保留 `score > 0.25` 的框；L179-180 用 `box_type_3d` 以几何中心 origin 包装。`_decode_heatmap` 内部（[L235-L251](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/smoke_mono3d_head.py#L235-L251)）：3×3 局部极大值（L235）→ topK（L237-239）→ 峰值处 gather 8 维回归（L241-242）→ `bbox_coder.decode`（L247-248）→ 拼成 (B, 100, 7)。

`trans_mat` 从哪来？SMOKE 专属管线里有 [RandomShiftScale 与 AffineResize](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/smoke/smoke_dla34_dlaneck_gn-all_4xb8-6x_kitti-mono3d.py#L18-L20)（随机平移缩放 + 仿射 resize 到 1280×384），这两个 transform 会把 `trans_mat`（原图→特征图的仿射矩阵）与 `affine_aug` 标志写进结果字典（[transforms_3d.py:L1662](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L1662) 起）——2D 仿射会破坏 3D 标定关系，必须记录矩阵以便解码时还原。

SMOKECoder 的解码几何：

[mmdet3d/models/task_modules/coders/smoke_bbox_coder.py:L96-L112](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/smoke_bbox_coder.py#L96-L112) —— `decode` 把 8 维切成 depth/offset/dim/ori 四段，依次得到深度、投影中心、尺寸、朝向。

[mmdet3d/models/task_modules/coders/smoke_bbox_coder.py:L114-L119](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/smoke_bbox_coder.py#L114-L119) —— 深度线性反归一：\(d = o_d \times 16.32 + 28.01\)，即配置里那对先验。

[mmdet3d/models/task_modules/coders/smoke_bbox_coder.py:L148-L161](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/smoke_bbox_coder.py#L148-L161) —— 求中心的解析反投影：特征图点加偏移 → 齐次坐标左乘 `trans_mat` 的逆回到原图像素 → **乘上深度**（把归一化坐标变成 \( (u d, v d, d) \)）→ 左乘 `cam2img` 的逆 → 相机系 3D 中心。这与 4.3.4 手推的几何完全同构，只是多了「特征图 ↔ 原图」的仿射一层。

[mmdet3d/models/task_modules/coders/smoke_bbox_coder.py:L163-L177](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/smoke_bbox_coder.py#L163-L177) —— 尺寸：\(dims = e^{o} \cdot \bar{dims}_{label}\)，乘性围绕类别均值。[L179-L217](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/smoke_bbox_coder.py#L179-L217) —— 朝向：由单位向量算夹角 \(\alpha = \arctan(\sin/(\cos+\epsilon))\)，按 cos 分量符号做 ±π/2 象限修正，再加视线角 \(ray = \arctan(X/Z)\)（用刚解出的中心位置）得全局 yaw，最后折叠回 \([-\pi, \pi]\)——与 FCOS3D 的 `decode_yaw` 目的相同，但用二维向量替代「标量 + 二分类」。

训练侧的三组 corner 损失：

[mmdet3d/models/dense_heads/smoke_mono3d_head.py:L302-L323](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/smoke_mono3d_head.py#L302-L323) —— `get_predictions` 在 **gt 投影中心**处 gather 回归预测并解码；L309 `locations[:, 1] += dimensions[:, 1] / 2` 把几何中心换到底面中心（配合 `encode` 用 `box_type_3d` 默认底面 origin 包装）；L316-321 构造三组「替换一个成分、其余用真值」的框——预测朝向的 `bbox3d_yaws`、预测尺寸的 `bbox3d_dims`、预测位置的 `bbox3d_locs`。

[mmdet3d/models/dense_heads/smoke_mono3d_head.py:L533-L552](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/smoke_mono3d_head.py#L533-L552) —— 损失 = GaussianFocalLoss（热图）+ 三组框各自 `.corners`（每框 8 个三维角点）对真值角点的 L1 之和。用角点而非裸参数算损失，是把「位置、尺寸、朝向」三个异质量纲的误差统一到同一几何空间的手法（u6-l4 Part-A2 的 corner loss 同源）。另注意 [L385-L389](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/smoke_mono3d_head.py#L385-L389)：经历过 `RandomShiftScale` 仿射增强（`affine_aug=True`）的样本不计回归损失——2D 仿射后 3D 真值不再可靠，只保留热图监督。

#### 4.4.4 代码实践

**实践目标**：理解 SMOKE 先验的量级意义。

**操作步骤**（纸面 + 阅读型实践）：

1. 读 [configs/_base_/models/smoke.py:L46-L51](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/smoke.py#L46-L51)，记下 `base_depth=(28.01, 16.32)` 与三组 `base_dims`；
2. 假设网络对某峰值点输出深度偏移 \(o_d = 0.5\)、尺寸偏移 \((0.1, -0.1, 0)\)，类别为 Car（`base_dims` 第 3 组 \((3.88, 1.63, 1.53)\)）；
3. 用 `_decode_depth`（L114-119）与 `_decode_dimension`（L163-177）的公式手算深度与尺寸。

**需要观察的现象**：深度 \(= 0.5 \times 16.32 + 28.01 = 36.17\) 米；尺寸 \(= (e^{0.1} \cdot 3.88,\ e^{-0.1} \cdot 1.63,\ 1.53) \approx (4.29,\ 1.48,\ 1.53)\)。

**预期结果**：偏移量在 ±0.5 量级即可覆盖 20~36 米的常见深度区间——先验把网络输出范围压缩到小数值区间，训练更稳定。若把 `base_depth` 改成远不适配数据集的值（如 `(5, 1)`），多数目标都需要网络输出很大的偏移才能纠偏，收敛会显著变慢（待本地验证：可在小数据集上对比训练前几百个 iter 的 loss 曲线）。

#### 4.4.5 小练习与答案

**练习 1**：SMOKE 推理为什么可以不做 NMS？

**答案**：检出方式是热图上的 3×3 局部极大值 + 每峰只取一个框：同一物体的邻域像素在局部极大值筛选后只剩峰值本身，不同物体峰不重合，天然互斥。FCOS3D 每个像素都出框，邻域像素对同一物体的预测高度冗余，必须用旋转 NMS 去重。

**练习 2**：SMOKE 的朝向为什么用二维单位向量，而不是 FCOS3D 的「标量 + 方向二分类」？

**答案**：二维 \((\sin, \cos)\) 表示天然消除了 \(2\pi\) 周期与 ±π 不连续问题（角度回绕在单位圆上连续），不需要额外的方向分类分支；代价是解码时要处理象限（`_decode_orientation` 的 ±π/2 修正）。两种方案解决同一个问题，是同一权衡的不同取舍——u5-l7 的 MultiBin、u5-l4 的 sin 差回归也都是这个家族的成员。

**练习 3**：`trans_mat` 在 SMOKE 里必不可少，在 FCOS3D 里却没有，为什么？

**答案**：SMOKE 的管线含 `RandomShiftScale`/`AffineResize` 仿射增强，特征图坐标与原图像素之间隔着一张仿射矩阵，解码时必须先乘 `trans_mat` 逆还原；FCOS3D 的测试管线只有 `mmdet.Resize`（等比缩放，比例信息在 `scale_factor` 里，见 [fcos_mono3d_head.py:L658-L659](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/fcos_mono3d_head.py#L658-L659) 的处理），不需要完整仿射矩阵。解码器与数据管线是配套设计的。

## 5. 综合实践

**任务**：按本讲规格完成——「用 FCOS3D config 实例化 head，随机图像特征 forward，打印每个输出分支张量形状，并把 bbox_coder 的解码输出投影回图像平面验证 2D 框合理性」。

**实践目标**：把 4.2 的分支结构、4.3 的解码几何在本机跑通，亲手验证「解码 → 反投影 → 再投影」的往返一致性。

**操作步骤**：

1. 准备脚本（示例代码，保存为 `fcos3d_head_probe.py`，在仓库根目录运行）：

```python
# 示例代码：探针 FCOSMono3DHead 的分支形状与解码几何
import torch
from mmengine import Config, init_default_scope

init_default_scope('mmdet3d')          # 让 type='FCOSMono3DHead' 能路由（u4-l1）
from mmdet3d.registry import MODELS    # locations 懒导入会自动加载 mmdet3d.models
from mmdet3d.structures import points_cam2img, points_img2cam

cfg = Config.fromfile(
    'configs/fcos3d/fcos3d_r101-caffe-dcn_fpn_head-gn_8xb2-1x_nus-mono3d.py')

head = MODELS.build(cfg.model.bbox_head)
head.eval()

# 模拟 1600x900 输入（pad 到 32 的倍数）经 FPN 后的 5 层特征
sizes = (113, 57, 29, 15, 8)   # strides = [8, 16, 32, 64, 128]
feats = tuple(torch.randn(1, 256, h, w) for h, w in
              zip(sizes, sizes))  # 方形仅为演示

with torch.no_grad():
    outs = head(feats)
cls_scores, bbox_preds, dir_preds, attr_preds, centernesses = outs[:5]

print('分支形状（每层）:')
for name, o in zip(['cls(10)', 'bbox(9)', 'dir(2)', 'attr(9)', 'centerness(1)'],
                   [cls_scores, bbox_preds, dir_preds, attr_preds, centernesses]):
    print(f'  {name:>14}: {[tuple(t.shape) for t in o]}')

# ---- 解码几何：取第 0 层（stride=8）某格点 ----
level, stride = 0, head.strides[level]
bbox = bbox_preds[level][0]        # (9, H, W)，forward 内已做过 decode
i, j = sizes[level] // 2, sizes[level] // 2   # 中心格点

offset = bbox[:2, i, j]            # 已乘 stride 的像素偏移（eval 模式）
depth  = bbox[2, i, j]             # exp 后的米制深度
w, h, l = bbox[3:6, i, j]          # exp 后的米制尺寸（CAM 系: 宽/高/长）

# 格点中心像素坐标（对照 _get_points_single 的公式）
pt = torch.tensor([j * stride + stride // 2, i * stride + stride // 2],
                  dtype=torch.float32)
center2d = pt - offset             # 投影 3D 中心的像素坐标 (u, v)
p25d = torch.cat([center2d, depth[None]])       # (u, v, d)

# 2.5D → 相机系 → 再投影回像素（往返验证）
cam_pt = points_img2cam(p25d[None, :])[0]       # (X, Y, Z)
reproj = points_cam2img(cam_pt[None, :])[0]     # (u', v')

# 内参（量级取自 nuScenes CAM_FRONT0）
view = torch.tensor([[1266.4, 0, 816.2], [0, 870.2, 493.9], [0, 0, 1]])

print(f'\ndepth={depth:.2f} m, dims(w,h,l)=({w:.2f},{h:.2f},{l:.2f}) m')
print(f'center2d={center2d.tolist()}, cam={cam_pt.tolist()}')
print(f'往返误差: {float((reproj - center2d).abs().max()):.2e} px')

# 2D 框合理性：像素宽 ≈ fx*w/Z，像素高 ≈ fy*h/Z
fx, fy = view[0, 0], view[1, 1]
print(f'投影 2D 框尺寸估计: 宽 {float(fx*w/cam_pt[2]):.1f} px, '
      f'高 {float(fy*h/cam_pt[2]):.1f} px')
```

2. 确认环境里已按 [u1-l2](u1-l2-installation-and-verification.md) 装好 mmengine/mmcv/mmdet/mmdet3d，然后运行 `python fcos3d_head_probe.py`。

**需要观察的现象**：

1. 五个分支每层形状分别为 `(1, 10, H, W)`、`(1, 9, H, W)`、`(1, 2, H, W)`、`(1, 9, H, W)`、`(1, 1, H, W)`——与 4.2.4 的纸面表格逐一对上；`bbox_pred` 已是**解码后**的量（深度为正的米制值）。
2. 往返误差 `reproj - center2d` 应在浮点精度量级（约 1e-3 ~ 1e-5 像素），验证 `points_img2cam`/`points_cam2img` 互逆。
3. 随机权重下深度/尺寸是无意义的正数，投影 2D 框尺寸可能偏离常识量级（正常训练后的模型应落在几十~几百像素）。

**预期结果**：形状表与往返误差两条可稳定复现（待本地验证：本讲义未实际执行该脚本，且 `sizes` 需与真实 FPN 输出一致——真实输入若为 1600×900，各层为 113/57/29/15/8 附近，取决于 pad；若报形状断言错误，请先用 `head.backbone` 之外的真实模型 `extract_feat` 打印实际尺寸后替换）。若想看有意义的解码值，可下载 FCOS3D 权重（见 `configs/fcos3d/README.md`）用 `init_model` 加载后再取 head 输出。

**延伸（可选）**：把脚本中 `cfg.model.bbox_head` 换成 `Config.fromfile('configs/_base_/models/smoke.py').model.bbox_head`，feats 换成单个 `(1, 64, 96, 320)` 张量（1280×384 的 1/4），观察 SMOKE head 的 `forward` 只返回 `(cls, bbox)` 两个列表——两套 head 的分支差异一目了然（待本地验证）。

## 6. 本讲小结

- **检测器极薄，算法在 head**：`SingleStageMono3DDetector` 跨库继承 mmdet 的 `SingleStageDetector`，只补「从 `imgs` 取特征」与「写 `pred_instances_3d`/`pred_instances` 双契约」两件事；`SMOKEMono3D` 连 `predict` 都不重写，`FCOSMono3D` 重写它是为了装 3D/2D 双结果。
- **FCOS3D = 逐像素 + 分组回归**：9 维回归按 `group_reg_dims=(2,1,3,1,2)` 拆成 offset/depth/size/rot/velo 五组，各有独立卷积塔、Scale 与损失；正负样本由「中心采样 ∧ 回归范围 ∧ 最近中心」三条件逐像素判定，无 anchor、无 IoU 分配。
- **解码内嵌前向**：`FCOS3DBBoxCoder.decode` 在训练时也执行，深度/尺寸用 `exp`（或先验线性）保证正量纲，`norm_on_bbox` 让 offset 在「训练÷stride / 推理×stride」的配对中保持损失与推理空间一致。
- **朝向分解是单目的通用技巧**：全局 yaw = 视线角（由 `atan2(u-c_x, f_x)` 精确算出）+ 局部朝向（网络回归）；FCOS3D 用「标量 + 方向二分类」，SMOKE 用「二维单位向量 + 象限修正」。
- **SMOKE = 热图 + 解析反投影**：类别热图局部极大值 + topK 检出、无需 NMS；`SMOKECoder` 用 `trans_mat` 逆与内参逆把「像素 + 深度」解析地反投影成相机系中心，训练用三组「半真半预测」框的角点 L1。
- **数据管线与解码器配套**：FCOS3D 靠 `centers_2d`/`depths`（`with_bbox_depth=True`）编码目标，SMOKE 靠 `AffineResize` 写入的 `trans_mat` 还原仿射——换 head 必须同步换管线。

## 7. 下一步学习建议

本讲补完了「点云 / 单目 / 多模态」三条输入路线中的第二条。建议下一步：

1. **学 [u6-l6](u6-l6-mvxnet-multimodal.md)（MVXNet 与多模态融合）**：看图像特征如何被映射回体素与点云特征融合，本讲的 `transformation_3d_flow` 逆变换知识（u3-l6）与 mmdet 组件复用（本讲 4.1）都会在那里再次出现。
2. **横向对比热图范式**：回看 [u5-l5](u5-l5-centerpoint.md) 的 CenterPoint，与本讲 SMOKE 对照——同一套高斯溅射/局部极大值代码资产分别在 BEV 网格与图像平面上的实例化。
3. **源码延伸阅读**：`mmdet3d/models/dense_heads/` 下还有 `pgd_head.py`（PGD，把深度改成概率分布预测，是 FCOS3D 的直接后继）与 `monoflex_head.py`（MonoFlex），可作为检验本讲知识迁移能力的自测材料；配套配置在 `configs/pgd/` 与 `configs/monoflex/`。
4. **动手验证**：完成第 5 节综合实践后，尝试把探针脚本改成加载真实权重的端到端推理（参照 [u1-l3](u1-l3-first-inference-demo.md) 的 `mono_det_demo.py` 与 `mmdet3d/apis/inference.py`），观察真实模型的深度/尺寸分布与随机权重的差别。

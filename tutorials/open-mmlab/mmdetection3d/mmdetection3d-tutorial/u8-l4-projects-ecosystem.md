# u8-l4 projects 生态导览：BEVFusion、DETR3D 与自定义模块组织

## 1. 本讲目标

前几讲我们一直在 `mmdet3d/` 主包与 `configs/` 里学习。本讲走出主包，进入仓库的「前沿孵化区」——`projects/` 目录。学完本讲，你应该能够：

1. 说出 `projects/` 目录的定位与组织规律：每个子项目如何「寄生」在主库注册器上、如何用 `custom_imports` 接入、以及 README 中三段式 milestone 清单的含义。
2. 走通 DETR3D 的 query-based 前向主链路：900 个 3D 查询如何通过相机投影「回采」多视角 2D 特征（3D-to-2D query）、解码器如何迭代精修参考点、训练时如何用 3D 匈牙利算法做一对一匹配。
3. 走通 BEVFusion 的 BEV 级融合前向主链路：图像分支如何用 LSS 范式「抬升-散射」成 BEV 特征、点云分支的体素特征如何与图像 BEV 特征对齐拼接、两条分支的分辨率为何恰好一致。
4. 掌握一套「config 考古」方法：拿到一个 project 的 config，能逐个 `type` 判断它来自主库、来自 project 自身、还是借自 MMDetection（`mmdet.` 前缀）。

本讲是单元 8 的第 4 讲，属于导览性质，目标不是逐行精读两个项目，而是建立「主库 ↔ 子项目」的接口认知，让你有信心去读任何一个新 project。

## 2. 前置知识

本讲默认你已掌握以下内容（均有对应前置讲义）：

- **注册器与 custom_imports**（u4-l1、u8-l3）：`dict(type='Xxx')` 经注册器路由到真实类；`@register_module()` 装饰器在模块被 `import` 时以副作用完成注册，所以 config 里要写 `custom_imports` 让 Runner 启动时执行这次 import；`default_scope='mmdet3d'` 决定无前缀 `type` 的查找起点。
- **MVXTwoStageDetector**（u6-l6）：主库中用 `pts_*` / `img_*` 可选槽位组装双分支检测器的基类，DETR3D 直接继承它。
- **Det3DDataSample 与 Pack3DDetInputs**（u2-l4、u3-l8）：模型输入是 `dict(inputs, data_samples)` 契约；`batch_input_metas` 来自 `data_samples[i].metainfo`。
- **数据增强的审计线索**（u3-l6）：增强 transform 会把 `pcd_rotation`、`transformation_3d_flow`、`img_aug_matrix` 等决策写进 data dict，供模型侧逆变换还原坐标。BEVFusion 的视角变换大量消费这些矩阵。
- **CenterPoint 热图范式**（u5-l5）：BEV 格子上预测中心点热图 + 回归分支，BEVFusion 的 TransFusionHead 用它来产生初始 proposal。
- **匈牙利匹配的直觉**：经典指派问题——有 \( n \) 个预测、\( m \) 个真值，代价矩阵 \( C \in \mathbb{R}^{n \times m} \)，求一一配对使总代价最小。DETR 系检测器用它替代 IoU anchor 分配，从而省掉 NMS。可以用如下公式表述：

\[
\min_{\pi} \sum_{i} C_{i,\pi(i)} \quad \text{s.t. } \pi \text{ 是单射}
\]

- **LSS（Lift-Splat-Shoot）直觉**：把每个图像像素位置「抬升」成 \( D \) 个离散深度上的 3D 点（带概率），再把所有 3D 点「散射」进 BEV 网格累积。BEVFusion 的视角变换是它的深度增强版。

不需要复杂数学，但建议对「齐次坐标投影 + 透视除法」有印象（u7-l3 图像投影一节讲过）：

\[
\begin{bmatrix} u \\ v \\ z \end{bmatrix} = \underbrace{\text{lidar2img}}_{4\times 4} \begin{bmatrix} x \\ y \\ z \\ 1 \end{bmatrix}, \qquad (u', v') = \left( \frac{u}{z}, \frac{v}{z} \right)
\]

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `mmdet3d/registry.py` | 主库注册节点定义；projects 的类全部注册到这里的 `MODELS` / `TASK_UTILS` 上 |
| `projects/DETR3D/detr3d/detr3d.py` | DETR3D 检测器：继承 MVXTwoStageDetector，组织图像特征提取与 head 调用 |
| `projects/DETR3D/detr3d/detr3d_head.py` | DETR3DHead：继承 mmdet 的 DETRHead，query 分类回归、逐层损失、解码输出 |
| `projects/DETR3D/detr3d/detr3d_transformer.py` | Detr3DTransformer / Decoder / CrossAtten：3D-to-2D 采样注意力的核心 |
| `projects/DETR3D/detr3d/hungarian_assigner_3d.py` | HungarianAssigner3D：3D 匈牙利一对一匹配 |
| `projects/DETR3D/detr3d/nms_free_coder.py` | NMSFreeCoder：充当 bbox coder 的「无 NMS 解码器」 |
| `projects/DETR3D/configs/detr3d_r101_gridmask.py` | DETR3D 主配置：custom_imports、model 树、管线 |
| `projects/BEVFusion/bevfusion/bevfusion.py` | BEVFusion 检测器：继承 Base3DDetector，双分支 extract_feat 与融合 |
| `projects/BEVFusion/bevfusion/depth_lss.py` | DepthLSSTransform：LSS 视角变换（frustum、几何反投影、BEV 池化） |
| `projects/BEVFusion/bevfusion/transfusion_head.py` | TransFusionHead 与 ConvFuser：热图 proposal + transformer 精修、BEV 特征融合层 |
| `projects/BEVFusion/configs/bevfusion_lidar_voxel0075_second_secfpn_8xb4-cyclic-20e_nus-3d.py` | 纯 LiDAR 基座配置（点云分支全量定义） |
| `projects/BEVFusion/configs/bevfusion_lidar-cam_voxel0075_second_secfpn_8xb4-cyclic-20e_nus-3d.py` | LiDAR-相机融合配置（继承基座，增量定义图像分支与融合层） |
| 两个项目的 `README.md` | 训练/测试命令、预训练权重、三段式 milestone 清单 |

阅读建议：先读 4.1 建立生态全景，再按 DETR3D → BEVFusion 的顺序走前向链路，最后用 4.4 的对比表收束。

## 4. 核心概念与源码讲解

### 4.1 projects 生态的组织方式：寄生在主库注册器上的插件

#### 4.1.1 概念说明

`projects/` 目录存放**尚未（或不必）进入主包的前沿算法实现**。本仓库 HEAD 上共有 10 个子目录：

```
BEVFusion   CENet   CenterFormer   DETR3D   DSVT
NeRF-Det    PETR    TPVFormer      TR3D     example_project
```

它们覆盖多模态融合（BEVFusion）、纯视觉 query 系检测（DETR3D、PETR）、占据网格分割（TPVFormer）、室内实例分割（TR3D、CENet）、Transformer 体素检测（DSVT、CenterFormer）等方向——基本是当时最新的论文复现。

关键认知是：**projects 不是独立的代码库，而是主库的「外挂插件」**。它们没有自己的注册器体系，而是把类直接注册到主库的注册节点上；也没有自己的训练循环，直接复用 `tools/train.py` 与 `tools/test.py`。这使得「新算法接入」的成本被压缩到：写几个类 + 注册 + 一份 config。

这与 u8-l3 讲过的 example_project 是同一机制的真实规模化应用——example_project 是官方提供的最小模板，其余 9 个是按同样约定写成的完整项目。

#### 4.1.2 核心流程

一个 project 接入主库的完整链路（四步）：

1. **定义并注册**：子项目内的类用主库注册节点装饰，如 `@MODELS.register_module()`、`@TASK_UTILS.register_module()`。
2. **包入口全量导出**：`<project>/<pkg>/__init__.py` import 所有公开类——这一步是「import 即注册」的触发器。
3. **config 声明接入**：config 顶部写 `custom_imports = dict(imports=['projects.Xxx.xxx'])`，Runner 启动时执行 `import_modules_from_strings`，触发第 2 步的注册副作用；`default_scope='mmdet3d'`（显式写或从 `_base_/default_runtime.py` 继承）保证无前缀 `type` 路由到 mmdet3d 注册器。
4. **正常训练测试**：`python tools/train.py projects/Xxx/configs/yyy.py`，与主库 config 无任何区别。

另外，每个 README 末尾有一个**三段式 milestone 清单**，是理解项目成熟度的快捷方式：

- Milestone 1：PR-ready，代码完成、文档齐全、推理结果正确——达到即可进入 `projects/`；
- Milestone 2：训练正确性（从零训练能复现论文指标）；
- Milestone 3：类型注解、单测、代码打磨齐备——达到才能**移入主包**。

DETR3D 与 BEVFusion 的清单都停在 Milestone 2（第三段全部未勾选），所以它们的代码在 `projects/` 而不是 `mmdet3d/models/` 下。

#### 4.1.3 源码精读

**注册节点来自主库**。projects 里所有 `@MODELS.register_module()` 用的都是主库 `mmdet3d/registry.py` 定义的节点：

- [mmdet3d/registry.py:L70-L72](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/registry.py#L70-L72)：`MODELS` 注册节点，parent 指向 mmengine 根注册器，locations 是 `mmdet3d.models`。注意 locations 只覆盖主包目录，**不包含 projects**——这正是每个 project 的 config 必须写 `custom_imports` 的原因：注册器懒导入找不到它们，必须显式 import。
- [mmdet3d/registry.py:L115-L117](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/registry.py#L115-L117)：`TASK_UTILS` 节点，DETR3D 的匈牙利分配器、代价函数、NMSFreeCoder 都注册在这里。

**包入口全量导出**（注册触发器）：

- [projects/DETR3D/detr3d/__init__.py:L1-L14](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/DETR3D/detr3d/__init__.py#L1-L14)：`from .detr3d import DETR3D` 等八行 import + `__all__`。config 里 `custom_imports` 指向的正是这个包——import 它的瞬间，八个类全部完成注册。
- [projects/BEVFusion/bevfusion/__init__.py:L1-L20](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/BEVFusion/bevfusion/__init__.py#L1-L20)：BEVFusion 导出的 17 个类，横跨检测器（BEVFusion）、head（TransFusionHead）、视角变换（DepthLSSTransform）、数据变换（BEVFusionRandomFlip3D）与匹配组件（HungarianAssigner3D）——注意一个 project 不只注册模型，还注册 transforms 与 task_utils，全方位借用主库的配置体系。

**config 侧的接入声明**：

- [projects/DETR3D/configs/detr3d_r101_gridmask.py:L26-L28](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/DETR3D/configs/detr3d_r101_gridmask.py#L26-L28)：`default_scope = 'mmdet3d'`，源码注释直白地解释了效果——`type='DETR3D'` 会被当作 `mmdet3d.DETR3D` 处理（DETR3D 类虽定义在 projects 里，但注册进了 mmdet3d 的 MODELS 节点，所以能被找到）。
- [projects/PETR/configs/petr_vovnet_gridmask_p4_800x320.py:L7-L7](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/PETR/configs/petr_vovnet_gridmask_p4_800x320.py#L7-L7) 与 [projects/TPVFormer/configs/tpvformer_8xb1-2x_nus-seg.py:L3-L4](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/TPVFormer/configs/tpvformer_8xb1-2x_nus-seg.py#L3-L4)：其他项目如法炮制，一律 `custom_imports = dict(imports=['projects.<项目名>.<包名>'])`。这是全目录统一约定。

**里程碑清单与成熟度**：

- [projects/DETR3D/README.md:L94-L148](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/DETR3D/README.md#L94-L148)：DETR3D 的 checklist。Milestone 1、2 已勾选，Milestone 3（类型注解、单测、Metafile、移入核心包）全部未勾选。注释还写明了对 PR 的最低要求：「每个模型组件都应注册进 `mmdet3d.registry.MODELS` 并可通过 config 配置」——这就是 projects 的接口纪律。
- [projects/DETR3D/README.md:L34-L36](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/DETR3D/README.md#L34-L36)：一个重要的**版本约束警示**——README 明说要求 `mmdet <= V3.0.0rc5`，因为更高版本 mmdet 重构了 DETR 系 head，而 DETR3D 的代码还没跟上。DETR3DHead 跨库继承 mmdet 的 DETRHead，因此对 mmdet 版本敏感。这是「projects 依赖主库 + 姊妹库三方协同」带来的现实代价。

**BEVFusion 的特殊之处：自带 CUDA 算子**。

- [projects/BEVFusion/README.md:L24-L31](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/BEVFusion/README.md#L24-L31)：README 指出 BEVFusion 原版的 voxelization 算子与 mmcv 内置实现不同，若要用原版预训练权重，需要先执行 `python projects/BEVFusion/setup.py develop` 编译 `bevfusion/ops/` 下的 CUDA 扩展（bev_pool 与 voxelization 两组算子）。这是 10 个项目中唯一需要编译步骤的——大多数项目纯 Python，靠主库 + mmcv 的现成算子即可。

#### 4.1.4 代码实践

**实践：扫描全部 projects 的接入方式**

1. **实践目标**：用两条命令验证「所有 project 都用同一套 custom_imports 约定接入主库」，把生态全景装进脑子里。
2. **操作步骤**：
   - 在仓库根目录执行 `ls projects/`，记录 10 个子目录；
   - 执行 `grep -rn "custom_imports" projects/*/configs/*.py`，观察每个 config 顶部的 imports 指向；
   - 挑一个没读过的项目（如 DSVT 或 TR3D），打开它的 `<pkg>/__init__.py`，数一数导出了哪些类。
3. **需要观察的现象**：每个算法 config 的第一或第二行都是 `custom_imports = dict(imports=['projects.<项目名>.<包名>'])`；对比 `allow_failed_imports` 是否设置（BEVFusion、TPVFormer、DSVT 显式设为 False，DETR3D、PETR 未设置）。
4. **预期结果**：9 个算法项目 + example_project 全部命中同一约定；同时你会发现 DETR-family 的「家族相似性」——PETR 包里也有 `hungarian_assigner_3d.py`、`nms_free_coder.py`、`petr_head.py`，文件名与 DETR3d 几乎平行，说明新项目常常是从旧项目复制改造而来。

（本实践只需 grep 与读文件，无需 GPU 与数据，可直接完成。）

#### 4.1.5 小练习与答案

**练习 1**：为什么注册器的 `locations=['mmdet3d.models']` 找不到 projects 里的类，但 config 依然能 `MODELS.build` 出 DETR3D？

**答案**：locations 懒导入只扫描主包目录；但 DETR3D 在定义处用 `@MODELS.register_module()` 把类**直接写进了** mmdet3d 的 MODELS 注册表。`custom_imports` 保证 Runner 启动时执行了那次定义，于是查找链路第一段「本级注册表命中」就成功了，根本轮不到 locations。

**练习 2**：如果把 `projects/DETR3D/configs/detr3d_r101_gridmask.py` 里的 `custom_imports` 一行删掉，训练命令会发生什么？

**答案**：Runner 构建模型时，`MODELS.build(dict(type='DETR3D', ...))` 在 mmdet3d → mmengine 的注册链上找不到 `DETR3D`（主包没有这个名字，locations 也只扫主包目录），抛出类似 `DETR3D is not in the MODELS registry` 的 KeyError。这正是 u8-l3 强调的「import 是易错环节」。

**练习 3**：判断对错：projects 里的项目各自维护一套独立的注册器和训练脚本。

**答案**：错。它们复用主库 `mmdet3d/registry.py` 的节点与根目录的 `tools/train.py` / `tools/test.py`，区别只在类定义的位置与 `custom_imports` 声明。（唯一例外是 BEVFusion 需要额外编译自带 CUDA 算子。）

### 4.2 DETR3D：3D-to-2D query 与匈牙利一对一匹配

#### 4.2.1 概念说明

DETR3D 解决的是**纯视觉多相机 3D 检测**：输入 nuScenes 一帧的 6 张环视图像，输出 LiDAR 坐标系下的 3D 框。回忆 u6-l5 的单目 3D 检测——FCOS3D 逐像素预测深度，误差会逐级放大。DETR3D 的论文标题点名了它的替代方案：**3D-to-2D Queries**。

核心思想一句话：**不在 2D 图像上预测 3D，而是先在 3D 空间里放 900 个「对象查询」（object query），每个查询持有一个 3D 参考点，用相机投影矩阵把这个 3D 点投回 6 张图像上采样特征**。这样做有三个好处：

1. 检测框的参数化天然在 3D（LiDAR）坐标系，不需要逐像素深度估计；
2. 查询是稀疏的（900 个），不产生稠密 BEV 特征图；
3. 一对一匹配让每个真值只对应一个预测，**推理无需 NMS**。

它与 u6-l6 的 MVXNet 形成有趣对照：MVXNet 把图像特征「送进」点云（点级融合），DETR3D 让 3D 查询「去取」图像特征（特征采样式交互）。

#### 4.2.2 核心流程

DETR3D 推理/训练主链路（`predict` / `loss` 共享前半段）：

```text
batch (6 视图像 imgs + data_samples)
  │
  ├─ add_lidar2img:  cam2img × lidar2cam → 逐相机 lidar2img 矩阵，写回 metainfo
  │
  ├─ extract_feat → extract_img_feat
  │     (B,6,C,H,W) → view 成 (B*6,C,H,W) → GridMask(可选) → ResNet → FPN
  │     → 各级特征 reshape 回 (B,6,C,H/4,W/4) 多尺度列表
  │
  ├─ pts_bbox_head(img_feats, metas)               # DETR3DHead.forward
  │     ├─ query_embedding(900, 512) 拆成 query 与 query_pos
  │     ├─ reference_points = Linear(256→3)+sigmoid   # 归一化 3D 参考点
  │     └─ 6 层解码器循环：
  │           self-attn(query 之间) → Detr3DCrossAtten(3D 参考点投影采样图像特征)
  │           → reg_branch 出 10 维增量 → 更新参考点(with_box_refine)
  │
  ├─ 训练: loss_by_feat —— 每层解码器输出都做
  │     HungarianAssigner3D 一对一匹配 → FocalLoss + L1（深监督）
  │
  └─ 推理: predict_by_feat —— NMSFreeCoder.decode
        取最后一层输出 → topk(max_num=300) → 重心 z 换底面 z
        → box_type_3d 包装 → add_pred_to_datasample
```

角度处理与 u5-l4/u5-l5 一脉相承：回归输出用 sin/cos 对表示朝向，避免 2π 周期歧义；解码时用 `atan2` 还原（封装在 `denormalize_bbox` 中）。

#### 4.2.3 源码精读

**检测器：薄壳 + 现场合成 lidar2img**

- [projects/DETR3D/detr3d/detr3d.py:L13-L20](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/DETR3D/detr3d/detr3d.py#L13-L20)：`DETR3D(MVXTwoStageDetector)` 注册进主库 MODELS。它继承 u6-l6 精读过的 MVXTwoStageDetector——那套 `img_backbone` / `img_neck` / `pts_bbox_head` 的构建逻辑全部复用，尽管 DETR3D 实际上没有点云输入（名字里的 pts_* 槽位装的是「图像特征上的 3D head」）。
- [projects/DETR3D/detr3d/detr3d.py:L36-L54](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/DETR3D/detr3d/detr3d.py#L36-L54)：构造函数把组件配置全部转交父类，自己只额外建一个 `GridMask` 数据增强（图像网格遮挡，概率 0.7，由 config 的 `use_grid_mask=True` 启用）。
- [projects/DETR3D/detr3d/detr3d.py:L184-L201](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/DETR3D/detr3d/detr3d.py#L184-L201)：`add_lidar2img`——对每个样本、每个相机，从 metainfo 取 `cam2img` 与 `lidar2cam`，用主库工具函数 `get_lidar2img`（来自 `mmdet3d.structures.bbox_3d.utils`）现乘出 `lidar2img` 写回 metainfo。这呼应 u2-l1：跨传感器转换矩阵的存放位置就是 info pkl → dataset → metainfo 这条链。
- [projects/DETR3D/detr3d/detr3d.py:L56-L96](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/DETR3D/detr3d/detr3d.py#L56-L96)：`extract_img_feat`——多视图像 `(B,N,C,H,W)` 先 `view(B*N,C,H,W)` 摊平送 backbone + FPN（mmdet 的 ResNet/FPN 期望 4 维输入），再按 `BN/B` 还原相机维，返回多尺度 `(B,N,C,H,W)` 列表。这个「摊平-还原」手法在 BEVFusion 里会再次出现。
- [projects/DETR3D/detr3d/detr3d.py:L112-L139](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/DETR3D/detr3d/detr3d.py#L112-L139)：`loss`——取 metainfo → 补 lidar2img → 提特征 → `pts_bbox_head(img_feats, metas)` → 从 data_samples 收集 `gt_instances_3d` → `loss_by_feat`。注意模型不碰点云，输入契约里只有 `imgs`。
- [projects/DETR3D/detr3d/detr3d.py:L142-L181](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/DETR3D/detr3d/detr3d.py#L142-L181)：`predict`——同样链路，走 `predict_by_feat` 得 `results_list_3d`，最后由基类的 `add_pred_to_datasample` 写成 `pred_instances_3d`（u2-l4 的契约）。

**Head：跨库继承 mmdet 的 DETRHead**

- [projects/DETR3D/detr3d/detr3d_head.py:L19-L20](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/DETR3D/detr3d/detr3d_head.py#L19-L20)：`DETR3DHead(DETRHead)`——直接继承 MMDetection 的 DETRHead！损失函数构建、`sync_cls_avg_factor`、背景类权重等通用逻辑全部来自 mmdet（config 里的 `loss_cls=dict(type='mmdet.FocalLoss', ...)` 正是给父类用的）。这是「projects 同时寄生 mmdet3d 与 mmdet 两个库」的最佳例证。
- [projects/DETR3D/detr3d/detr3d_head.py:L37-L66](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/DETR3D/detr3d/detr3d_head.py#L37-L66)：构造函数里值得关注三点：`bbox_coder` 传的是 `NMSFreeCoder`（用「coder」之名行无 NMS 解码之实）；`code_weights` 是 10 维逐项损失权重（速度项 0.2，其余 1.0）；sampler 写死 `PseudoSampler`——一对一匹配之后无需再采样，这与 u5-l4 FocalLoss 配 PseudoSampler 的逻辑一致。`assigner` 则由父类从 `train_cfg.pts.assigner` 构建，`HungarianAssigner3D` 注册在 mmdet3d 的 TASK_UTILS 上，能被 mmdet 系父类找到，靠的是 u4-l1 讲过的 default_scope 跨库切换。
- [projects/DETR3D/detr3d/detr3d_head.py:L69-L105](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/DETR3D/detr3d/detr3d_head.py#L69-L105)：`_init_layers`——分类/回归分支都是 Linear+LayerNorm+ReLU 堆叠；`with_box_refine=True` 时每层解码器持有**独立副本**的分支（`_get_clones` 深拷贝），否则各层共享权重。`num_pred = decoder 层数`，逐层深监督的舞台在此搭好。
- [projects/DETR3D/detr3d/detr3d_head.py:L115-L181](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/DETR3D/detr3d/detr3d_head.py#L115-L181)：`forward` 主函数。`query_embedding` 是 `(900, 512)` 的可学习嵌入，劈成 query 与 query_pos 各 256 维；transformer 返回各层解码状态 `hs` 与参考点序列。随后是**DETR 式反归一化精修**（[L143-L166](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/DETR3D/detr3d/detr3d_head.py#L143-L166)）：回归增量先加到 `inverse_sigmoid(reference)` 上、过 sigmoid 压回 (0,1)，再仿射到真实点云范围：

\[
c_x = \sigma\!\left(\sigma^{-1}(r_x) + \Delta_x\right) \cdot (x_{max} - x_{min}) + x_{min}
\]

  其中 \( r_x \) 是参考点、\( \Delta_x \) 是回归分支输出。xy 用第 0/1 维、z 用第 4 维（对应 10 维码 `(cx,cy,l,w,cz,h,sinφ,cosφ,vx,vy)` 的布局），注意长宽高朝向速度不走 sigmoid。

**Transformer：3D-to-2D 采样的心脏**

- [projects/DETR3D/detr3d/detr3d_transformer.py:L170-L191](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/DETR3D/detr3d/detr3d_transformer.py#L170-L191)：解码器循环内的**迭代精修**——每层先用 `reg_branches[lid]` 预测增量，把它加到 `inverse_sigmoid(reference_points)` 再 sigmoid，得到新参考点；关键是 `reference_points = new_reference_points.detach()`，参考点更新**不回传梯度**，保证每层的监督信号稳定。
- [projects/DETR3D/detr3d/detr3d_transformer.py:L292-L347](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/DETR3D/detr3d/detr3d_transformer.py#L292-L347)：`Detr3DCrossAtten.forward`——本讲最核心的 20 行。`attention_weights` 分支为每个查询输出 `num_cams × num_points × num_levels` 的权重；`feature_sampling` 把参考点投影到各相机图像并双线性采样特征，返回采样值与合法掩码 mask（视锥外的投影点被遮蔽）；随后 `attention_weights.sigmoid() * mask` 加权求和，最后输出 = 注意力采样特征 + 残差 + 3D 位置编码（`position_encoder` 编码 `inverse_sigmoid(reference_points_3d)`）。这就是「3D 查询到 2D 特征上取信息」的全部实现。
- [projects/DETR3D/detr3d/detr3d_transformer.py:L350-L420](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/DETR3D/detr3d/detr3d_transformer.py#L350-L420)：`feature_sampling`——先把归一化参考点还原成米制坐标（乘 pc_range 加偏移），拼齐次坐标，与逐相机 `lidar2img` 矩阵相乘，做透视除法 \( (u/z, v/z) \)，除以 `pad_shape` 归一到 (0,1)；`z > eps` 且落图内的点保留在 mask 中，投出图外/背面的点特征被置 NaN 后由 `nan_to_num` 清零。u7-l3 图像投影那套齐次坐标数学在这里被原样复用。

**匈牙利一对一匹配**

- [projects/DETR3D/detr3d/hungarian_assigner_3d.py:L18-L19](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/DETR3D/detr3d/hungarian_assigner_3d.py#L18-L19)：`HungarianAssigner3D` 注册在 TASK_UTILS，继承 mmdet 的 `BaseAssigner`——与 u5-l6 的 Max3DIoUAssigner 同一抽象父类，但分配准则从「IoU 阈值带」换成了「代价矩阵全局最优一对一」。
- [projects/DETR3D/detr3d/hungarian_assigner_3d.py:L104-L115](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/DETR3D/detr3d/hungarian_assigner_3d.py#L104-L115)：代价构造——分类代价用 mmdet 的 `FocalLossCost`（dev1.x 接口：装进 InstanceData 传参），回归代价用 project 自己的 `BBox3DL1Cost`，只取**前 8 维**（不含两个速度维）。细读会发现一个事实：`__init__` 虽然构建了 `iou_cost`（[L48](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/DETR3D/detr3d/hungarian_assigner_3d.py#L48)），但 `assign` 里 `cost = cls_cost + reg_cost` **根本没有用到它**——config 里 `iou_cost=dict(type='mmdet.IoUCost', weight=0.0)` 的注释「Fake cost. This is just to get compatible with DETR head」也印证了这一点：纯粹是为了满足 mmdet DETRHead 的配置校验。
- [projects/DETR3D/detr3d/hungarian_assigner_3d.py:L117-L133](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/DETR3D/detr3d/hungarian_assigner_3d.py#L117-L133)：代价矩阵搬回 CPU，调 scipy 的 `linear_sum_assignment` 求解指派问题；匹配对 (`matched_row_inds`, `matched_col_inds`) 之外的查询全部标 0（背景），匹配上的查询标 `gt_index + 1`——与 u5-l6 讲过的 `AssignResult.gt_inds` 语义完全一致，所以下游 `PseudoSampler` 与损失代码无需任何改动。
- [projects/DETR3D/detr3d/detr3d_head.py:L183-L226](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/DETR3D/detr3d/detr3d_head.py#L183-L226)：`_get_target_single`——单帧目标生产：先把 gt 框的底面中心换成 `gravity_center`（几何中心口径，与 u6-l2 VoteNet 的约定相同）；assign → PseudoSampler 格式化 → 正样本查抄 gt 标签与 9 维框目标（`code_size - 1`：theta 仍是标量，sin/cos 展开发生在 `normalize_bbox`）。
- [projects/DETR3D/detr3d/detr3d_head.py:L344-L399](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/DETR3D/detr3d/detr3d_head.py#L344-L399)：`loss_by_feat`——**逐层深监督**：6 层解码器每层各算一次 `loss_cls`（FocalLoss，前景×1 + 背景×bg_cls_weight 加权平均）与 `loss_bbox`（L1，`code_weights` 逐维加权），中间层损失记作 `d0.loss_cls`、`d1.loss_cls`……只取最后一层作为 `loss_cls`/`loss_bbox` 汇总项。回归目标经 `normalize_bbox` 转成 10 维归一化码，NaN（负样本占位）用 `isnotnan` 掩码剔除。
- [projects/DETR3D/detr3d/nms_free_coder.py:L39-L93](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/DETR3D/detr3d/nms_free_coder.py#L39-L93)：`NMSFreeCoder.decode_single`——推理后处理：对 `(900, 10类)` 的 sigmoid 分数展平后直接 `topk(300)`，`labels = index % num_classes`、`bbox = index // num_classes` 反解；再做 `post_center_range` 范围过滤。全程没有 NMS——一对一匹配训练保证了同类相邻查询不会重复框住同一目标。
- [projects/DETR3D/detr3d/detr3d_head.py:L401-L447](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/DETR3D/detr3d/detr3d_head.py#L401-L447)：`predict_by_feat`——解码后 `bboxes[:, 2] -= bboxes[:, 5] * 0.5`，把 z 从**几何中心**换回 mmdet3d 通行的**底面中心**口径（u2-l2），再用 `img_metas[i]['box_type_3d']`（即 LiDARInstance3DBoxes）包装成 9 维框（含速度）。

#### 4.2.4 代码实践

**实践：跟踪 DETR3D.loss 调用链并核对 config**

1. **实践目标**：不看讲义，仅凭源码画出 `DETR3D.loss` 从 `batch_inputs_dict` 到 `losses_pts` 的完整调用图，并用 config 核对每个环节的 `type`。
2. **操作步骤**：
   - 读 [detr3d.py:L112-L139](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/DETR3D/detr3d/detr3d.py#L112-L139)，列出依次调用的 5 个方法；
   - 打开 [configs/detr3d_r101_gridmask.py:L28-L117](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/DETR3D/configs/detr3d_r101_gridmask.py#L28-L117)，对每个环节标注 `type` 的归属：`DETR3D`（project）、`mmdet.ResNet` / `mmdet.FPN`（mmdet）、`Detr3DTransformer`（project）、`mmdet.DetrTransformerDecoderLayer`（mmdet）、`MultiheadAttention`（mmcv，无前缀时按 scope 查找）、`HungarianAssigner3D`（project）；
   - 数一数 config 里 `pts_bbox_head.transformer.decoder.num_layers`（应为 6），再对照 `loss_by_feat` 的深监督：训练日志里应出现 `d0` 到 `d4` 共 5 组中间层损失加最后一层汇总。
3. **需要观察的现象**：模型树中「project 自有类」与「mmdet. 前缀类」交替出现；`train_cfg.pts.assigner` 与 head 内损失函数的权重成对出现（`FocalLossCost weight=2.0` ↔ `FocalLoss loss_weight=2.0`，`BBox3DL1Cost weight=0.25` ↔ `L1Loss loss_weight=0.25`）——DETR 系的标准做法：**匹配代价与损失同构**，匹配对了梯度方向才对。
4. **预期结果**：得到一张五段式调用图（add_lidar2img → extract_feat → head.forward → head.loss_by_feat → 返回 loss dict）。若本地有 8 卡与 nuScenes 数据，可按 [README 训练命令](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/DETR3D/README.md#L38-L47) 实跑一个 iter 验证日志键名；无法实跑则完成源码侧核对即可，日志键名**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：DETR3D 为什么能把 `iou_cost` 权重设为 0 甚至在 assign 里不用它，而 u5-l6 的 Max3DIoUAssigner 必须依赖 IoU？

**答案**：匈牙利分配对「代价」没有形式要求，只要能构造标量矩阵即可，分类 + L1 回归代价已足够区分好坏匹配；而 Max3DIoUAssigner 的分配准则本身定义为 IoU 与阈值的比较，IoU 是它的立身之本。这也解释了为什么 DETR3D 的 gt 框要先换 gravity_center 并归一化——为了让 L1 距离在所有维度上量纲一致。

**练习 2**：`with_box_refine=True` 时为什么要给每层解码器深拷贝独立的 cls/reg 分支？

**答案**：逐层精修意味着每层的回归任务难度不同（第 1 层参考点很粗糙，第 6 层已经很准），共享一套分支会迫使同组权重同时拟合多种分布；独立副本让每层有专属的细化策略。代码里对应 `_get_clones` 深拷贝（[detr3d_head.py:L86-L101](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/DETR3D/detr3d/detr3d_head.py#L86-L101)）。

**练习 3**：DETR3D 推理为什么可以完全不做 NMS？这个性质的来源是结构性的还是后处理技巧？

**答案**：来源是结构性的：训练时匈牙利匹配保证每个 gt 只分配给一个查询、其余查询被监督为背景，因此模型学到的是「互斥地」预测目标，同类相邻查询不会重复响应。NMSFreeCoder 只是受益者（topk 即可），不是原因。对比 u5-l7：anchor 系与热图系（CenterPoint 仍需 circle NMS 去除相邻峰）都做不到这一点。

### 4.3 BEVFusion：BEV 统一表征的多传感器融合

#### 4.3.1 概念说明

BEVFusion 解决**多传感器融合**：LiDAR 点云 + 6 相机图像同时输入。u6-l6 的 MVXNet 是**点级融合**——把图像特征投影给每个点再聚合进体素，论文摘要里批评这种方式「丢掉了图像特征的语义密度」（投影到稀疏点云上，语义信息被稀释）。

BEVFusion 的答案是把两条分支都转换到**统一的 BEV（鸟瞰图）特征平面**再融合：

- 图像分支：2D 特征 → **LSS 视角变换**（深度分布 + 视锥散射）→ 图像 BEV 特征；
- 点云分支：体素化 → 稀疏卷积 → 点云 BEV 特征（就是单元 5 的 SECOND 路线）；
- 融合：两个 BEV 张量沿通道 concat 后一个卷积（ConvFuser），之后完全是标准的 SECOND + SECONDFPN + head。

BEV 是自动驾驶感知的「通用语」：几何（来自 LiDAR）与语义（来自相机）在同一网格上对齐，融合天然空间对齐，且任务无关（检测、分割都能用）。

#### 4.3.2 核心流程

BEVFusion 前向主链路（`extract_feat` 是枢纽）：

```text
batch (imgs + points + data_samples)
  │
  ├─ 图像分支 extract_img_feat
  │    imgs (B,6,3,H,W) → 摊平 (B*6,...) → Swin → GeneralizedLSSFPN
  │    → DepthLSSTransform:
  │        ① 点云投影生成稀疏深度图 → dtransform 编码 64 通道
  │        ② 与图像特征 concat → depthnet 出 D+C 通道
  │        ③ 深度 softmax 加权 context → (B,6,80,D,32,88)
  │        ④ frustum 反投影成 LiDAR 系 3D 点 → bev_pool scatter
  │        ⑤ Z 维压平 + downsample → 图像 BEV 特征 (B,80,180,180)
  │
  ├─ 点云分支 extract_pts_feat
  │    points → CUDA Voxelization → 均值池化(voxelize_reduce)
  │    → HardSimpleVFE → BEVFusionSparseEncoder → (B,256,180,180)
  │
  ├─ ConvFuser: concat(80+256) → Conv3x3+BN+ReLU → (B,256,180,180)
  │
  ├─ SECOND backbone → SECONDFPN → 多尺度 BEV 特征
  │
  └─ TransFusionHead: 热图 topk 出 200 proposal
       → Transformer decoder 精修 → 匈牙利匹配训练 / 输出
```

注意图像分支的 ①：点云在这里就进场了——用 LiDAR 点投影出的**稀疏深度图**监督 depthnet 的深度预测，这是 DepthLSS 相对朴素 LSS 的增强，也是融合的第一处伏笔。

#### 4.3.3 源码精读

**检测器：单阶段自组装 + 从 data_preprocessor 里「偷」体素配置**

- [projects/BEVFusion/bevfusion/bevfusion.py:L19-L20](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/BEVFusion/bevfusion/bevfusion.py#L19-L20)：`BEVFusion(Base3DDetector)`——不走 MVXTwoStageDetector，直接继承检测器根基类，把所有组件当作平级槽位手动组装。与 DETR3D 的选择形成对照：复用基类的层次越浅，自由度越大、复用越少。
- [projects/BEVFusion/bevfusion/bevfusion.py:L36-L63](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/BEVFusion/bevfusion/bevfusion.py#L36-L63)：构造函数有个非常规操作——体素化配置藏在 `data_preprocessor` 字典里，先 `pop('voxelize_cfg')` 再交给父类，然后**不经注册器**直接实例化 `Voxelization(**voxelize_cfg)`（`from .ops import Voxelization`，即 project 自带的 CUDA 算子，对应 4.1 说的编译步骤）。其余九个组件（voxel_encoder / img_backbone / view_transform / fusion_layer / pts_backbone / pts_neck / bbox_head 等）全部 `MODELS.build`，可选槽位为 None 时跳过——所以「纯 LiDAR」配置把 img_* 留空即可单分支运行。对照 config：[bevfusion_lidar_voxel0075_second_secfpn_8xb4-cyclic-20e_nus-3d.py:L49-L54](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/BEVFusion/configs/bevfusion_lidar_voxel0075_second_secfpn_8xb4-cyclic-20e_nus-3d.py#L49-L54) 的 `voxelize_cfg`（max_num_points=10、max_voxels=[120000,160000]、voxelize_reduce=True）。
- [projects/BEVFusion/bevfusion/bevfusion.py:L175-L201](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/BEVFusion/bevfusion/bevfusion.py#L175-L201)：`voxelize`——逐帧调 CUDA 算子，`F.pad(c, (1,0), value=k)` 把帧号编进 coors 第 0 列（u4-l3 讲过的 batch 编码手法）；`voxelize_reduce=True` 时体素特征取**均值**（sum/sizes），与主库 VFE 内做的平均等效，这里提前在 CPU 侧算掉。
- [projects/BEVFusion/bevfusion/bevfusion.py:L130-L173](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/BEVFusion/bevfusion/bevfusion.py#L130-L173)：两个分支的特征提取。`extract_img_feat` 里视角变换包在 `torch.autocast(device_type='cuda', dtype=torch.float32)` 中（AMP 训练时强制 float32，防投影数值不稳），点云分支则 `autocast('cuda', enabled=False)`——README 提示可加 `--amp` 训练，两处精度策略就是支撑。`batch_size = coords[-1, 0] + 1` 又一次从 coors 末行反推 batch 数。
- [projects/BEVFusion/bevfusion/bevfusion.py:L240-L284](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/BEVFusion/bevfusion/bevfusion.py#L240-L284)：`extract_feat`——**双分支唯一汇合点**。图像在则从 metainfo 收集 `lidar2img`/`cam2img`/`cam2lidar` 及两个增强矩阵 `img_aug_matrix`/`lidar_aug_matrix`（缺省单位阵），成批转 tensor 送视角变换；点云分支无条件执行；`fusion_layer` 存在则融合，否则断言只有单分支特征。融合后走 `pts_backbone → pts_neck`。这两个 `*_aug_matrix` 正是 u3-l6 埋下的伏笔：BEVFusion 的管线（`ImageAug3D`、`BEVFusionRandomFlip3D` 等）把增强决策写进 meta_keys（见融合 config 的 [Pack3DDetInputs meta_keys 列表 L125-L131](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/BEVFusion/configs/bevfusion_lidar-cam_voxel0075_second_secfpn_8xb4-cyclic-20e_nus-3d.py#L125-L131)），模型侧才能把增强后的坐标还原回原始标定系。
- [projects/BEVFusion/bevfusion/bevfusion.py:L286-L298](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/BEVFusion/bevfusion/bevfusion.py#L286-L298)：`loss` 与 `predict` 都是「extract_feat → bbox_head.loss/predict → add_pred_to_datasample」三步薄壳。

**视角变换：LSS 的深度增强版**

- [projects/BEVFusion/bevfusion/depth_lss.py:L11-L17](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/BEVFusion/bevfusion/depth_lss.py#L11-L17)：`gen_dx_bx` 从 xbound/ybound/zbound 三个 `(min, max, step)` 三元组算出格距 `dx`、格心 `bx`、格数 `nx`。融合 config 的 `xbound=[-54,54,0.3]` 给出 \( n_x = (54-(-54))/0.3 = 360 \)。
- [projects/BEVFusion/bevfusion/depth_lss.py:L52-L69](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/BEVFusion/bevfusion/depth_lss.py#L52-L69)：`create_frustum`——预生成视锥格网格：深度按 `dbound=[1,60,0.5]` 离散化

\[
D = \frac{60 - 1}{0.5} = 118
\]

  再把图像宽高等距采样到 `feature_size=[32,88]`，堆出 `(118, 32, 88)` 的 frustum 参数（每个元素是 (u, v, d)）。这个张量是静态的，注册为 `requires_grad=False` 的 Parameter。
- [projects/BEVFusion/bevfusion/depth_lss.py:L71-L111](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/BEVFusion/bevfusion/depth_lss.py#L71-L111)：`get_geometry`——视锥点变 3D 的三步：先**撤销图像增强**（减 post_trans、乘 post_rots 逆，对应 resize/crop/翻转），再**内参逆投影**（前两维乘深度 d、乘 `intrinsics` 逆，把像素坐标变成相机系射线上的点），再经 `camera2lidar`（cam2lidar 旋转 × intrinsics 逆先组合再乘，省计算）转到 LiDAR 系，最后施加 `extra_rots/extra_trans`（LiDAR 增强的正变换）。每个 (d,u,v) 格由此获得一个 LiDAR 系 3D 坐标。
- [projects/BEVFusion/bevfusion/depth_lss.py:L256-L331](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/BEVFusion/bevfusion/depth_lss.py#L256-L331)：`BaseDepthTransform.forward` 的前半段——**用点云制造稀疏深度监督**：逐帧把点云按「逆 LiDAR 增强 → lidar2img 投影 → 透视除法 → 正向图像增强」变换到图像平面，落在图内的点把深度写进 `(B,6,1,H,W)` 的稀疏深度图。这段是 u6-l6 PointFusion 三重坐标变换的镜像——那边采特征、这边写深度。
- [projects/BEVFusion/bevfusion/depth_lss.py:L334-L426](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/BEVFusion/bevfusion/depth_lss.py#L334-L426)：`DepthLSSTransform`——`dtransform`（[L361-L371](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/BEVFusion/bevfusion/depth_lss.py#L361-L371)）把稀疏深度图编码成 64 通道，与 256 通道图像特征 concat 进 depthnet（[L372-L380](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/BEVFusion/bevfusion/depth_lss.py#L372-L380)），输出 D+C；`get_cam_feats`（[L406-L421](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/BEVFusion/bevfusion/depth_lss.py#L406-L421)）里深度通道 softmax 作为概率分布，逐深度加权 context 特征——「每个特征格在 118 个深度 bin 上摊开概率质量」，得到 `(B,6,80,118,32,88)`。
- [projects/BEVFusion/bevfusion/depth_lss.py:L116-L148](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/BEVFusion/bevfusion/depth_lss.py#L116-L148)：`bev_pool`——把每个视锥格的特征按其 3D 坐标栅格化（`(coord - 格心偏移)/格距` 取整）、越界的丢弃，调用 project 自带的 `bev_pool` CUDA 算子（[ops/bev_pool](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/BEVFusion/bevfusion/ops/bev_pool/bev_pool.py)）做 scatter 累加，最后 `torch.cat(x.unbind(dim=2), 1)` 把 Z 维拍进通道。`downsample=2` 再把 360×360 缩到 180×180。
- 融合 config 的视角变换参数见 [bevfusion_lidar-cam_voxel0075_second_secfpn_8xb4-cyclic-20e_nus-3d.py:L45-L55](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/BEVFusion/configs/bevfusion_lidar-cam_voxel0075_second_secfpn_8xb4-cyclic-20e_nus-3d.py#L45-L55)：`out_channels=80`、`image_size=[256,704]`、`feature_size=[32,88]`。

**融合层与 head：简单得近乎朴素**

- [projects/BEVFusion/bevfusion/transfusion_head.py:L29-L42](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/BEVFusion/bevfusion/transfusion_head.py#L29-L42)：`ConvFuser`——`nn.Sequential(Conv3x3 + BN + ReLU)`，forward 就是 `cat(inputs, dim=1)` 过卷积。80（图像）+ 256（点云）→ 256。「BEV 级融合」的全部本体不过这三层，重活全在前面的特征对齐。
- [projects/BEVFusion/configs/bevfusion_lidar_voxel0075_second_secfpn_8xb4-cyclic-20e_nus-3d.py:L82-L152](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/BEVFusion/configs/bevfusion_lidar_voxel0075_second_secfpn_8xb4-cyclic-20e_nus-3d.py#L82-L152)：`TransFusionHead` 配置——`num_proposals=200`、`nms_kernel_size=3`（热图上的 3×3 最大池化找局部峰值，呼应 u5-l5 CenterPoint 的 topK 峰值提取）、1 层 transformer decoder 精修；`common_heads` 声明 center/height/dim/rot/vel 五组卷积分支；损失是 GaussianFocalLoss（热图）+ L1（回归）；`assigner` 用的同样是 `HungarianAssigner3D`——不过是 BEVFusion 在自己 [utils.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/BEVFusion/bevfusion/utils.py) 里的一份变体实现（代价含 `BBoxBEVL1Cost` 与 `IoU3DCost`，带 IoU 项）；`test_cfg.nms_type=None`——同样无 NMS。可以看到 DETR 系思想（一对一匹配 + decoder 精修）与热图系思想（CenterPoint 式 proposal）在这个 head 里合流。
- 语义对齐的数学巧合并非巧合：点云分支 `sparse_shape=[1440,1440,41]`（\( 108/0.075=1440 \)），稀疏编码器总输出步长 8（train_cfg `out_size_factor=8`），得 \( 1440/8=180 \)；图像分支 360 网格 `downsample=2` 后也是 180。即 \( 0.075 \times 8 = 0.6 = 2 \times 0.3 \) 米/格——**两分支 BEV 分辨率必须由配置作者手工对齐**，这是 BEV 级融合最重要的隐性契约。

#### 4.3.4 代码实践

**实践：手算 DepthLSSTransform 的形状流**

1. **实践目标**：仅凭 config 参数推导视角变换每一站的张量形状，验证「两分支 180×180 对齐」这条隐性契约。
2. **操作步骤**：
   - 从融合 config 抄下 `image_size=[256,704]`、`feature_size=[32,88]`、`dbound=[1,60,0.5]`、`xbound=ybound=[-54,54,0.3]`、`out_channels=80`、`downsample=2`；
   - 手算：\( D=(60-1)/0.5=? \)；frustum 形状 = ?；xbound 格数 = ?；经 downsample 后 BEV 边长 = ?；
   - 再从纯 LiDAR config 抄 `voxel_size=[0.075,0.075,0.2]`、`point_cloud_range=[-54,-54,-5,54,54,3]`、`sparse_shape=[1440,1440,41]`、`out_size_factor=8`，算点云分支 BEV 边长；
   - 验证两者相等，并解释 `0.075 × 8 = 0.6 = 2 × 0.3` 的含义。
3. **需要观察的现象**：形状链 `frustum (118,32,88) → get_cam_feats 输出 (B,6,80,118,32,88) → bev_pool 输出 (B,80,360,360) → downsample 输出 (B,80,180,180)`；点云侧 `(B,256,180,180)`。
4. **预期结果**：两分支在 180×180 对齐，ConvFuser 的 `in_channels=[80,256]` 恰好等于两分支通道数（这就是 config 里 `[80, 256]` 的来历）。本实践纯纸面推导，无需运行环境；如想实际验证，可在本地 `MODELS.build` 一个 DepthLSSTransform 并喂随机张量打印形状，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：BEVFusion 图像分支里点云在几个地方被用到？

**答案**：两处。其一在 `BaseDepthTransform.forward` 前半段：点云投影成稀疏深度图，经 dtransform 编码后拼进 depthnet 输入（DepthLSS 的「Depth」来源）；其二在检测器 `extract_img_feat` 把原始 points 透传给视角变换（视角变换需要点云来造深度图，也用于 LSS 的几何参考）。但注意点云**主干特征**仍走独立的体素分支，两支只在 ConvFuser 汇合。

**练习 2**：为什么不直接把 360×360 的图像 BEV 特征与点云 BEV 特征融合，而要 downsample？

**答案**：对齐约束由点云分支决定：体素 0.075 m、稀疏编码器步长 8 → 0.6 m/格，即 180×180。图像分支 xbound 步长 0.3 m 得 360×360，必须 downsample=2 缩到 0.6 m/格才能 concat。若强行保持 360，通道拼接会因空间尺寸不匹配直接报错。

**练习 3**：`get_geometry` 里为什么先做 `post_rots/post_trans` 的逆变换，而不是最后做？

**答案**：frustum 的 (u,v) 是**增强后图像**上的网格坐标，而相机内参 `intrinsics` 描述的是**原始图像**的投影。所以必须先撤销图像增强回到原始像素系，才能用内参逆投影到相机系；顺序错了等于用错误的内参投影错误的坐标。同理 LiDAR 增强的 `extra_rots/extra_trans` 在最后施加——它作用在 LiDAR 系坐标上。这套「逆增强 → 几何变换 → 正增强」的顺序在 u6-l6 PointFusion 与本讲 depth_lss 中反复出现，是融合类模型的通用范式。

### 4.4 双子对比：两条前向主链路与 config 借用清单核对

#### 4.4.1 概念说明

DETR3D 与 BEVFusion 代表了 2022 年前后两条主流技术路线：**稀疏 query 路线**（图像特征被 3D 查询按需采样）与**稠密 BEV 路线**（图像特征整图抬升到 BEV）。它们同住 `projects/`、同用主库注册器、同样无 NMS，但几乎每个设计决策都相反。把它们并排读一遍，「融合范式」这个抽象概念就落地了。

#### 4.4.2 核心流程

| 维度 | DETR3D | BEVFusion |
| --- | --- | --- |
| 输入模态 | 仅 6 相机图像 | LiDAR 10 帧 sweep + 6 相机图像 |
| 检测器基类 | `MVXTwoStageDetector`（复用图像分支骨架） | `Base3DDetector`（单阶段，全手动组装） |
| 图像→3D | 3D 参考点投影**采样**特征（稀疏，900 查询×每相机 1 点） | LSS 视锥抬升 + BEV scatter（稠密，全图 118 深度） |
| 深度处理 | 隐式（查询在 3D 空间中优化，不预测深度） | 显式离散深度分布，且用点云稀疏深度图增强 |
| LiDAR 参与 | 无 | 独立体素分支 + 稀疏深度监督 |
| 融合点 | 无融合（纯视觉） | ConvFuser：BEV 通道 concat + 3×3 卷积 |
| head | DETR3DHead：900 query → 框 | TransFusionHead：热图 top 200 proposal + 1 层 decoder 精修 |
| 匹配 | HungarianAssigner3D（cls+L1，IoU 项弃用） | HungarianAssigner3D 变体（cls+BEV L1+IoU3D） |
| NMS | NMSFreeCoder top 300 | `nms_type=None` |
| 深监督 | 6 层逐层损失 | auxiliary 热图 + decoder |
| z 口径 | 训练 gravity_center，输出时减 h/2 回底面 | TransFusionBBoxCoder 输出底面中心 |

#### 4.4.3 源码精读

**借用 mmdet 组件的清单核对**（读 project config 的基本功）：

- DETR3D config（[detr3d_r101_gridmask.py:L28-L117](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/DETR3D/configs/detr3d_r101_gridmask.py#L28-L117)）借自 mmdet 的有：`mmdet.ResNet`（带 DCNv2 的 R101）、`mmdet.FPN`、`mmdet.DetrTransformerDecoderLayer`、`mmdet.SinePositionalEncoding`、`mmdet.FocalLoss`、`mmdet.L1Loss`、`mmdet.GIoULoss`（权重 0，占位）、`mmdet.FocalLossCost`、`mmdet.IoUCost`（权重 0，占位）；project 自有的有：`DETR3D`、`DETR3DHead`、`Detr3DTransformer`、`Detr3DTransformerDecoder`、`Detr3DCrossAtten`、`HungarianAssigner3D`、`BBox3DL1Cost`、`NMSFreeCoder`；无前缀的 `MultiheadAttention` 来自 mmcv（经 scope 链查找）。数一数：**纯视觉的 DETR3D 反而比 BEVFusion 借了更多 mmdet 组件**，因为 DETR 系基础设施都在 mmdet。
- BEVFusion 融合 config（[bevfusion_lidar-cam_voxel0075_second_secfpn_8xb4-cyclic-20e_nus-3d.py:L8-L57](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/BEVFusion/configs/bevfusion_lidar-cam_voxel0075_second_secfpn_8xb4-cyclic-20e_nus-3d.py#L8-L57)）借自 mmdet 的只有 `mmdet.SwinTransformer`（图像骨干）与三个损失（FocalLoss/GaussianFocalLoss/L1Loss，见[基座 config L142-L152](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/BEVFusion/configs/bevfusion_lidar_voxel0075_second_secfpn_8xb4-cyclic-20e_nus-3d.py#L142-L152)）；其余 `GeneralizedLSSFPN`、`DepthLSSTransform`、`ConvFuser`、`BEVFusionSparseEncoder`、`TransFusionHead`、`TransFusionBBoxCoder` 全是 project 自有。而点云分支的 `HardSimpleVFE`、`SECOND`、`SECONDFPN`、数据集 `NuScenesDataset`、评估 `NuScenesMetric`、`CBGSDataset` 等全部来自**主库 mmdet3d**——单元 5 学过的 SECOND 体系原样复用。

**数据管线的两种策略**：

- [detr3d_r101_gridmask.py:L132-L157](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/DETR3D/configs/detr3d_r101_gridmask.py#L132-L157)：DETR3D 的管线**完全借用主库**——`LoadMultiViewImageFromFiles`、`LoadAnnotations3D`、`MultiViewWrapper`（u7-l4 讲过的多相机决策复用器，把 PhotoMetricDistortion3D/RandomResize3D 逐相机应用）、`ObjectRangeFilter`、`Pack3DDetInputs`，一个自有 transform 都没有。模型侧需要的 `lidar2img` 由检测器在 `add_lidar2img` 里现场计算。
- [bevfusion_lidar-cam_voxel0075_second_secfpn_8xb4-cyclic-20e_nus-3d.py:L59-L131](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/BEVFusion/configs/bevfusion_lidar-cam_voxel0075_second_secfpn_8xb4-cyclic-20e_nus-3d.py#L59-L131)：BEVFusion 的管线是**混编**——点云侧的 `LoadPointsFromFile`/`LoadPointsFromMultiSweeps`/`PointsRangeFilter`/`PointShuffle` 与出口 `Pack3DDetInputs` 来自主库；图像侧因需要记录增强矩阵（`img_aug_matrix`/`lidar_aug_matrix`）供模型逆变换，自带 `BEVLoadMultiViewImageFromFiles`、`ImageAug3D`、`BEVFusionRandomFlip3D`、`BEVFusionGlobalRotScaleTrans`、`GridMask` 一整套。这印证了 4.1 的观察：project 不仅注册模型，也常自带数据变换——因为主库 transforms 的元信息输出满足不了它的坐标还原需求。

**训练入口完全一致**：两个项目都用根目录的 `tools/train.py`/`tools/test.py`，只是 config 路径指向 `projects/...`（见 [BEVFusion README:L40-L54](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/BEVFusion/README.md#L40-L54) 的训练命令——先训纯 LiDAR 基座，再加载它训融合模型，两阶段策略）。

#### 4.4.4 代码实践

**实践：制作「type 归属核对表」**

1. **实践目标**：对两个 config 的 model 块做彻底的 type 审计，训练「一眼判断 type 归属」的反射。
2. **操作步骤**：
   - 打开两个 config 的 model 块；
   - 逐个 `type` 判定归属：`mmdet.` 前缀 → MMDetection；`projects/<pkg>/__init__.py` 的 `__all__` 里能找到 → project 自有；其余 → 主库 mmdet3d（用 `grep -rn "class Xxx" mmdet3d/` 反查，方法见 u1-l4）；
   - 把结果填成三列表格（mmdet / mmdet3d 主库 / project 自有），统计各列数量。
3. **需要观察的现象**：DETR3D 的 mmdet 列明显更长；BEVFusion 的 project 自有列集中在视角变换与融合层；两边的 `data_preprocessor` 都是主库的 `Det3DDataPreprocessor`（BEVFusion 的只是多了 `voxelize_cfg` 键）。
4. **预期结果**：DETR3D 约 9 个 mmdet 组件、8 个自有组件；BEVFusion 约 4 个 mmdet 组件、约 8 个自有组件、约 10 个主库组件（含数据链上的）。数字有出入没关系，关键是每个判定都能说出依据。（纯静态分析，可直接完成。）

#### 4.4.5 小练习与答案

**练习 1**：两个项目都无 NMS，原因相同吗？

**答案**：机理相同（一对一匈牙利匹配使预测互斥），实现位置不同：DETR3D 靠 `NMSFreeCoder.decode` 里 topk 直接截断；BEVFusion 的 TransFusionHead 靠热图最大池化取峰值 + `test_cfg.nms_type=None`。另外 DETR3D 是「全 query 集合预测」，BEVFusion 是「热图 proposal + decoder」，后者即使匹配失误还有热图峰值兜底。

**练习 2**：如果让你给 BEVFusion 换一个更轻的图像骨干，config 要改哪里？为什么 DETR3D 不能简单这么做？

**答案**：BEVFusion 只需把 `model.img_backbone` 换成别的 `mmdet.` 骨干并保证 `img_neck.in_channels` 匹配新骨干输出通道（GeneralizedLSSFPN 的 `in_channels=[192,384,768]` 对应 Swin-T 三阶段）。DETR3D 的骨干嵌在 DETR 训练范式里，还受「mmdet <= V3.0.0rc5」的版本约束（README 明言），且 `load_from='ckpts/fcos3d.pth'` 的预训练权重与骨干绑定，换骨干要重新准备 FCOS3D 预训练，成本高得多。

**练习 3**：`projects/` 里还有 PETR、TPVFormer。根据本讲建立的框架，预测它们的 config 会怎么写接入声明、它们与 DETR3D 会有哪些同名文件？

**答案**：接入声明必然是 `custom_imports = dict(imports=['projects.PETR.petr'])` 与 `imports=['projects.TPVFormer.tpvformer']`（已验证属实）；PETR 包内确有 `hungarian_assigner_3d.py`、`nms_free_coder.py`、`petr_head.py`、`petr_transformer.py`、`grid_mask.py`——与 DETR3D 文件级平行，说明它是 DETR3D 的直系后代（PETR 把 3D 位置编码显式注入图像特征，替代 3D-to-2D 采样）。TPVFormer 则多了自己的 `nuscenes_dataset.py` 与 `data_preprocessor.py`——分割类项目连数据集都要自定义。

## 5. 综合实践

**综合任务：双检测器前向主链路对照图 + config 全量核对**（本讲规格要求的实践，纯源码阅读型，无需 GPU/数据）

1. **实践目标**：把 4.2、4.3、4.4 的内容串成一张自己的产出物：两张流程图 + 一张核对表，检验你是否真的能独立读懂任意 project。
2. **操作步骤**：
   - **第一步（DETR3D 链路）**：从 [detr3d.py 的 loss 方法](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/DETR3D/detr3d/detr3d.py#L112-L139) 入口出发，沿 `add_lidar2img → extract_feat → extract_img_feat → pts_bbox_head.forward → transformer → Detr3DCrossAtten → feature_sampling` 逐函数点击跳转，每站标注：所在文件、输入形状、输出形状、config 中对应的 `type`。在 `loss_by_feat` 处分叉画出匈牙利匹配支路。
   - **第二步（BEVFusion 链路）**：从 [bevfusion.py 的 extract_feat](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/BEVFusion/bevfusion/bevfusion.py#L240-L284) 入口，画图像分支（Swin → FPN → DepthLSSTransform 五步）与点云分支（Voxelization → VFE → SparseEncoder）双泳道，在 ConvFuser 汇合，标注每站的张量形状（用 4.3.4 的手算结果）。
   - **第三步（config 核对）**：对照 [detr3d_r101_gridmask.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/DETR3D/configs/detr3d_r101_gridmask.py) 与 [bevfusion_lidar-cam 配置](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/BEVFusion/configs/bevfusion_lidar-cam_voxel0075_second_secfpn_8xb4-cyclic-20e_nus-3d.py)，把流程图上每个模块的 `type` 标成三种颜色（mmdet / 主库 / project 自有）。
   - **第四步（反问自检）**：合上源码回答——DETR3D 的参考点在第 3 层解码器后为什么 detach？BEVFusion 的 frustum 有多少个格、为什么两分支 BEV 都是 180×180？
3. **需要观察的现象**：两张图在「多视图像摊平-还原」「匈牙利一对一」「无 NMS」「BEV 分辨率契约」四处出现交汇；config 里出现大量与流程图模块一一对应的 `type` 字符串。
4. **预期结果**：产出物可复述本讲全部三个最小模块；若日后仓库新增 project，你能用同样的三步（读 `__init__.py` → 读 detector 的 extract_feat/loss → config type 审计）在半小时内建立它的主链路认知。图中标注的运行时形状如需实测（例如真正 forward 一次打印 shape），需本地编译 BEVFusion CUDA 算子并准备 nuScenes 数据，**待本地验证**。

## 6. 本讲小结

- `projects/` 是寄生在主库注册器上的插件集合：10 个子项目统一用「类注册进 mmdet3d 节点 + `__init__.py` 全量导出 + config `custom_imports` 声明 + `default_scope='mmdet3d'` 路由」四步接入，训练测试复用根目录 tools 脚本；README 的三段式 milestone 清单标注项目成熟度（DETR3D/BEVFusion 均停在 Milestone 2）。
- DETR3D 是稀疏 query 路线：900 个 3D 查询持有归一化参考点，经 lidar2img 投影到 6 相机图像上采样特征（Detr3DCrossAtten + feature_sampling），6 层解码器迭代精修且参考点更新 detach；训练用 HungarianAssigner3D 做一对一匹配（cls + 前 8 维 L1 代价，IoU 项是兼容 DETRHead 的占位），逐层深监督，NMSFreeCoder topk 输出、全程无 NMS。
- BEVFusion 是稠密 BEV 路线：图像分支 DepthLSSTransform 用点云投影的稀疏深度图增强 LSS 深度预测（frustum (118,32,88) → 深度 softmax 加权 → bev_pool scatter → downsample），点云分支复用主库 SECOND 体系，两支在 ConvFuser（concat+Conv3×3）汇合；「两分支 BEV 分辨率必须手工对齐」（0.075×8 = 2×0.3 = 0.6 m/格 → 180×180）是最重要的隐性契约。
- 两个项目演示了 project 与主库/mmdet 的三种复用姿态：DETR3D 深度借 mmdet（继承 DETRHead，受 mmdet≤3.0.0rc5 约束）、BEVFusion 深度借主库（SECOND/SECONDFPN/NuScenesDataset/CBGSDataset）且自带 CUDA 算子、数据管线一边全借主库一边自带一整套记录增强矩阵的 transforms。
- 读任意 project 的通用方法三步走：读 `<pkg>/__init__.py` 看注册面 → 读 detector 的 `extract_feat`/`loss` 看主链路 → config `type` 逐个审计归属。

## 7. 下一步学习建议

- **下一讲（u8-l5）**：测试体系与贡献流程。你已经看到 projects 的 README 承诺了 Milestone 3 的「Unit tests」，下一讲讲 `tests/` 目录组织、mmdet3d/testing 伪数据工具与贡献指南——如果你打算把自己的 project 合入上游，那是必读内容。
- **横向扩展阅读**：用本讲的三步法自学 `projects/PETR`（与 DETR3D 文件级平行，对比 3D 位置编码 vs 3D-to-2D 采样两种 query 交互方式）与 `projects/TPVFormer`（BEV→三平面 TPV 的分割范式，观察它为何连 NuScenesDataset 都要自定义）。
- **纵深阅读**：若对 BEV 视角变换的效率优化感兴趣，读 [projects/BEVFusion/bevfusion/ops/bev_pool/bev_pool.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/BEVFusion/bevfusion/ops/bev_pool/bev_pool.py) 与其 CUDA 源码，理解论文所说的「BEV pooling 提速 40 倍」在算子层如何实现；若对 query 系感兴趣，追读 Detr3DCrossAtten 与 mmdet Deformable DETR 中 `MultiScaleDeformableAttention` 的异同。
- **动手验证**：在本地按 [BEVFusion README 的 demo 命令](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/BEVFusion/README.md#L32-L38) 跑通多模态 demo（需先编译 CUDA 算子、下载权重与 nuScenes mini 数据），把本讲的形状推导与真实前向对照。

# 3D 数据增强：RandomFlip3D 与 GlobalRotScaleTrans

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 KITTI 训练管线中各增强类的排列顺序，并解释「为什么范围过滤必须放在增强链的末尾」。
2. 追踪 RandomFlip3D 与 GlobalRotScaleTrans 内部「点云 + 3D 框（+ 2D 图像标注）同步变换」的完整调用链，理解同步的关键在于**框对象的方法顺带变换点对象**。
3. 理解 GlobalRotScaleTrans 的旋转/缩放/平移三步（R→S→T）如何同时作用于点和框，并会用 `pcd_rotation`、`pcd_scale_factor`、`pcd_trans` 手工推算任意一点变换后的位置。
4. 理解 ObjectNoise 借助 numba 加速的「逐目标扰动 + 碰撞测试」机制（`noise_per_object_v3_`），以及它为什么只变换落在框内的点。
5. 理解 ObjectRangeFilter / PointsRangeFilter 在增强末尾的「收尾」作用：把被增强推出感知范围的目标与点裁剪掉。

本讲的实践主线是：**写一个约 20 行的调试脚本，直接实例化 GlobalRotScaleTrans 并对一个随机场景（点 + 框）调用 transform，用手工矩阵运算验证框中心与点云刚体变换完全一致。**

## 2. 前置知识

### 2.1 一个 data dict 里到底有什么

经过加载类（上一讲 u3-l5）之后，管线中流动的 `input_dict` 至少包含：

- `points`：一个 `LiDARPoints`（或 Camera/DepthPoints）对象，内部是 `(N, C)` 的 float32 张量，前 3 列是坐标，后面的列是强度、时间差等属性。
- `gt_bboxes_3d`：一个 `LiDARInstance3DBoxes`（或对应坐标系子类）对象，内部是 `(M, 7)`（或 9 维含速度）的张量。
- `gt_labels_3d`：`(M,)` 的 numpy 整数数组。

增强类的工作就是：**对 points 与 gt_bboxes_3d 施加同一种几何变换，保证「点还在原来的框里」**。如果只变换其中一方，标注就废了。

### 2.2 刚体变换与「非刚体」的缩放

- **刚体变换**（平移、旋转、翻转）不改变物体的形状和尺寸，只改变位置与朝向。
- **缩放**严格来说不是刚体变换——它同时放大点的坐标间距与框的尺寸（`lwh`），所以「点在框内」的包含关系依然成立。mmdet3d 的做法是：坐标与尺寸同乘缩放因子，但**强度等属性列不缩放**、**yaw 不缩放**。

### 2.3 为什么 3D 检测特别依赖旋转增强

自动驾驶场景中，LiDAR 坐标系的 x 轴指向车头。如果训练集里所有车都「朝前开」，模型会学出「x 方向细长的框就是车」这类捷径特征。全局旋转增强把整个场景随机转一个角度，迫使模型学出旋转不变的几何特征；翻转增强则制造「靠左行驶/靠右行驶」的镜像世界。这两者合起来能大幅提升泛化能力。

### 2.4 numba 是什么

[numba](https://numba.pydata.org/) 是一个 Python JIT（即时编译）库，用装饰器 `@numba.njit` 把 Python 函数编译成机器码。`data_augment_utils.py` 里的逐点、逐框循环（点数上万、框数上百）如果用纯 Python 写会慢到无法用于训练，所以全部用 numba 编译。注意 `njit` 函数内部只能用 numpy 基本操作，不能随便调用 Python 对象方法——这解释了该文件「先转 numpy 裸数组再处理」的风格。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [mmdet3d/datasets/transforms/transforms_3d.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py) | 本讲主角：所有 3D 增强 Transforms。本讲覆盖 RandomFlip3D、ObjectNoise、GlobalRotScaleTrans、PointShuffle、ObjectRangeFilter、PointsRangeFilter |
| [mmdet3d/datasets/transforms/data_augment_utils.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/data_augment_utils.py) | ObjectNoise 的底层实现：numba 加速的逐目标噪声、碰撞测试、逐点变换 |
| [mmdet3d/structures/bbox_3d/lidar_box3d.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/lidar_box3d.py) | LiDARInstance3DBoxes 的 `rotate` / `flip`——「框带点一起转」的落点 |
| [mmdet3d/structures/points/base_points.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/base_points.py) | BasePoints 的 `rotate` / `translate` / `scale` / `in_range_3d` |
| [mmdet3d/structures/bbox_3d/base_box3d.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/base_box3d.py) | 框的 `translate` / `scale` / `in_range_bev` / `limit_yaw` 基础实现 |
| [configs/_base_/datasets/kitti-3d-3class.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/kitti-3d-3class.py) | KITTI 训练管线的真实配置，观察增强类的排序 |
| [mmdet3d/models/layers/fusion_layers/coord_transform.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/fusion_layers/coord_transform.py) | 消费增强「审计线索」的地方：多模态融合时按记录逆变换 |

## 4. 核心概念与源码讲解

### 4.1 增强全景：train_pipeline 里的增强家族与排序原则

#### 4.1.1 概念说明

3D 数据增强不是一个类，而是一组各司其职的 Transform，在 `train_pipeline` 列表里按固定顺序排成一条链。理解这条链的**排序原则**比记住每个类更重要：

1. **内容增改在前**（ObjectSample 粘贴新目标、ObjectNoise 扰动目标）——它们改变场景里「有哪些目标、目标在哪」。
2. **全局几何变换居中**（RandomFlip3D、GlobalRotScaleTrans）——它们把整个场景当作一个刚体来转/翻/缩放。
3. **过滤与洗牌在最后**（PointsRangeFilter、ObjectRangeFilter、PointShuffle）——增强可能把目标或点推出 `point_cloud_range`（模型只在这个范围内感知），所以必须先增强完再裁剪；顺序反了就会「先裁剪、后增强又出界」，把出界目标留给模型。

#### 4.1.2 核心流程

KITTI 训练管线的实际顺序（下节逐行精读）：

```text
LoadPointsFromFile / LoadAnnotations3D      # 加载（上一讲）
        │
        ▼
ObjectSample                                # 从 GT 数据库粘贴目标（下一讲 u3-l7 详讲）
        ▼
ObjectNoise                                 # 逐目标扰动（本讲 4.4）
        ▼
RandomFlip3D                                # 全局镜像（本讲 4.2）
        ▼
GlobalRotScaleTrans                         # 全局旋转/缩放/平移（本讲 4.3）
        ▼
PointsRangeFilter / ObjectRangeFilter       # 范围收尾（本讲 4.5）
        ▼
PointShuffle → Pack3DDetInputs              # 洗牌、打包（下一讲 u3-l8）
```

另一个贯穿全讲的机制是**变换审计线索**：每个几何增强都往 `input_dict['transformation_3d_flow']` 追加记号——`'HF'`（水平翻转）、`'VF'`（垂直翻转）、`'R'`/`'S'`/`'T'`（旋转/缩放/平移）。同时把变换参数存进 `pcd_rotation`、`pcd_scale_factor`、`pcd_trans` 等键。多模态模型（如 MVXNet）在融合图像特征时需要把「已增强的 3D 点」映射回「未增强的图像坐标系」，就要靠这条线索做逆变换。

#### 4.1.3 源码精读

先看真实配置。[kitti-3d-3class.py:L41-L67](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/kitti-3d-3class.py#L41-L67) 定义了 KITTI 的 `train_pipeline`：第 49 行 `ObjectSample`、第 50-55 行 `ObjectNoise`、第 56 行 `RandomFlip3D`（水平翻转概率 0.5）、第 57-60 行 `GlobalRotScaleTrans`（旋转 ±π/4、缩放 0.95~1.05）、第 61-62 行两个范围过滤器、第 63 行 `PointShuffle`，最后 `Pack3DDetInputs` 打包——这正是上面流程图的原型。

再看消费审计线索的一方。[coord_transform.py:L11-L98](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/fusion_layers/coord_transform.py#L11-L98) 的 `apply_3d_transformation`：它从 `img_meta` 里取出 `pcd_rotation` / `pcd_scale_factor` / `pcd_trans` / 翻转标志，把字母记号映射成逆操作；[L70-L79](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/fusion_layers/coord_transform.py#L70-L79) 中 `reverse=True` 分支把缩放换成 `1/s`、平移换成 `-t`、旋转矩阵取逆，并**把 flow 倒序**（`flow = flow[::-1]`），然后逐个执行——这正是「先增强后复原」的教科书式实现。

#### 4.1.4 代码实践

**实践目标**：从配置反查增强类的注册与顺序，建立本讲的「地图感」。

**操作步骤**：

1. 打开 [kitti-3d-3class.py:L41-L67](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/kitti-3d-3class.py#L41-L67)，把 `train_pipeline` 中每个 `type=` 抄下来。
2. 在 `mmdet3d/datasets/transforms/transforms_3d.py` 里用编辑器搜索 `class <type>`，确认它们全部注册在同一文件（`ObjectSample` 留到下一讲）。
3. 对比 [nus-3d.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/nus-3d.py) 中 nuScenes 的 train_pipeline（含 `LoadPointsFromMultiSweeps`），观察差异。

**需要观察的现象**：两个数据集管线的「内容增改 → 全局几何 → 过滤」三段式结构完全一致，只是加载阶段与参数（如旋转范围、是否用 `ObjectNoise`）不同。

**预期结果**：能画出 4.1.2 的流程图并为每个节点标出 `type` 与所在文件。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `ObjectRangeFilter` 移到 `RandomFlip3D` 之前，会出现什么问题？

**答案**：翻转/旋转/平移会把原本在范围内的目标中心推出 `point_cloud_range`（例如某车中心 x=69，被平移后 x=71 超出上界 70.4）。先过滤后增强，这些出界目标会带着「合法标注」的身份留在训练样本里，而模型只在范围内做预测， anchor 分配与损失计算都会被污染。这就是过滤必须收尾的原因。

**练习 2**：`transformation_3d_flow` 记录的 `['R', 'S', 'T']` 对应哪一个增强类？谁会消费它？

**答案**：对应 `GlobalRotScaleTrans`（它在 `transform` 末尾一次性追加三个记号）。消费方是多模态融合层的 `apply_3d_transformation`（coord_transform.py），用 `reverse=True` 按倒序撤销变换，把增强后的点映射回原始坐标系以匹配图像特征。

---

### 4.2 RandomFlip3D：BEV 镜像与 2D/3D 同步

#### 4.2.1 概念说明

`RandomFlip3D` 继承自 **MMDetection 的 `RandomFlip`**（2D 图像翻转），并扩展出 3D 语义：沿 BEV 平面把整个场景镜像。它要同时照顾四种情形——纯 LiDAR 检测、纯 LiDAR 分割、纯视觉检测、多模态检测——所以代码里充满了 `if 'points' in ... / if 'img' in ...` 的分支。

三个关键参数：

- `sync_2d`：有图像时，3D 翻转是否**跟随** 2D 图像的翻转决策。图像翻转了而点云没翻（或反之），点云与图像就不再对齐，多模态训练立即崩溃，所以默认 `True`。
- `flip_ratio_bev_horizontal` / `flip_ratio_bev_vertical`：无图像（或 `sync_2d=False`）时，BEV 水平/垂直翻转的独立采样概率。
- `flip_box3d`：是否翻转 3D 框。相机系 BEV 检测里 2D 图像翻转**不会改变**相机系下 3D 框的真值（框以相机坐标系定义），此时设为 `False`。

#### 4.2.2 核心流程

`transform()` 的决策链：

```text
input_dict 里有 img？
 ├─ 有 → 先调父类 RandomFlip.transform 翻转 2D 图像与 2D 标注
 │        sync_2d 且有 img？→ pcd_horizontal_flip = 2D 的翻转决策（复用）
 └─ 无 → 各自按 flip_ratio_bev_horizontal / vertical 独立掷骰子
        ↓
pcd_horizontal_flip == True？
 └─ random_flip_data_3d(input_dict, 'horizontal')
     ├─ flip_box3d 且有 gt_bboxes_3d 且有 points
     │    → gt_bboxes_3d.flip(direction, points=points)   # 框带点一起翻
     ├─ 只有框（纯视觉）→ 只翻框
     └─ 只有点（分割）  → 只翻点
 有 centers_2d（单目 3D）？→ 2D 投影中心与 cam2img 主点也要镜像
        ↓
transformation_3d_flow 追加 'HF' / 'VF'
```

LiDAR 坐标系下的翻转规则（horizontal = 左右镜像）：

\[ y \leftarrow -y, \qquad \text{yaw} \leftarrow -\text{yaw} \]

vertical（前后镜像）则为 \( x \leftarrow -x,\ \text{yaw} \leftarrow \pi - \text{yaw} \)（代码写作 `-yaw + π`）。

#### 4.2.3 源码精读

**构造与 2D 复用**：[transforms_3d.py:L115-L137](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L115-L137) 把 `flip_ratio_bev_horizontal` 直接传给父类作为 2D 图像的 `prob`——即 `sync_2d=True` 时「图像翻转概率」与「BEV 水平翻转概率」天然是同一个数；另两个 BEV 概率则做 0~1 合法性断言。

**3D 翻转的分派中心**：[transforms_3d.py:L139-L182](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L139-L182) 的 `random_flip_data_3d`。L160-169 处理三种组合，核心一行是 L163-164：`gt_bboxes_3d.flip(direction, points=points)`——**框的 flip 方法顺带把点翻了**，这就是「同步」的实现位置。L171-182 是单目 3D 的 2D 同步：投影中心 \(u\) 变为 \(w - u\)，内参矩阵的横轴主点 \(c_u\)（`cam2img[0][2]`）同样取 \(w - c_u\)——镜像图像后光心位置确实变了，内参必须跟着改。

**主入口**：[transforms_3d.py:L211-L249](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L211-L249) 的 `transform`。L223-225 先翻 2D；L227-238 决定 `pcd_horizontal_flip` / `pcd_vertical_flip`（注意：若上游已写入这两个键则不重采——`MultiScaleFlipAug3D` 测试时增强会预先注入）；L243-248 按方向调用翻转并记录 'HF'/'VF'。同文件 [L184-L209](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L184-L209) 的 `_flip_on_direction` 处理「外部已给定 flip 决策」的覆盖逻辑。

**框侧底层**：[lidar_box3d.py:L139-L178](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/lidar_box3d.py#L139-L178) 的 `LiDARInstance3DBoxes.flip`。L160-167 用切片 `tensor[:, 1::7]` / `tensor[:, 0::7]` 取负——步长 7 的用意：对 9 维框（含速度），列 1 和列 8（y 与 \(v_y\)）会**同时**被翻负，旋转速度矢量与翻转几何自动一致；yaw 按 \( -\text{yaw} \)（horizontal）或 \( \pi - \text{yaw} \)（vertical）修正，且仅在 `with_yaw` 时执行。L169-178 再把传入的点对象（或裸数组）按同规则翻转并返回。

**点侧底层**：[lidar_points.py:L38-L49](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/lidar_points.py#L38-L49) 的 `LiDARPoints.flip`——horizontal 翻 y 列、vertical 翻 x 列，重力轴 z 不动，属性列不动。

#### 4.2.4 代码实践

**实践目标**：直接实例化 `RandomFlip3D`，观察翻转前后点、框、yaw、审计线索的变化。

**操作步骤**（以下为示例代码，非项目自带脚本，在仓库根目录用 `python -I` 运行）：

```python
import numpy as np
from mmdet3d.datasets.transforms import RandomFlip3D
from mmdet3d.structures import LiDARInstance3DBoxes, LiDARPoints

np.random.seed(0)
pts = np.random.rand(100, 4).astype(np.float32) * [70, 80, 4, 1] - [0, 40, 3, 0]
gt = np.array([[10., 5., -1., 4., 2., 1.5, 0.7]], dtype=np.float32)

results = dict(
    points=LiDARPoints(pts.copy(), points_dim=4),
    gt_bboxes_3d=LiDARInstance3DBoxes(gt.copy()))

flip = RandomFlip3D(sync_2d=False,
                    flip_ratio_bev_horizontal=1.0,   # 概率置 1，必然翻转
                    flip_ratio_bev_vertical=0.0)
out = flip(results)

print('flip 标记:', out['pcd_horizontal_flip'], out['flip_direction'])
print('flow:', out['transformation_3d_flow'])
print('框翻转前  :', gt[0])
print('框翻转后  :', out['gt_bboxes_3d'].tensor[0].numpy())
print('点 y 均值 :', pts[:, 1].mean(), '->', out['points'].tensor[:, 1].mean().item())
```

**需要观察的现象**：框的 y 从 5 变为 -5、yaw 从 0.7 变为 -0.7、x/lwh/尺寸完全不变；点云整体 y 均值取反（点数对称时约为相反数）；`transformation_3d_flow == ['HF']`。

**预期结果**：y 与 yaw 同时取负，x、z、尺寸、强度保持不变；`pcd_horizontal_flip=True`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `RandomFlip3D` 继承 2D 的 `RandomFlip` 而不是直接继承 `BaseTransform`？

**答案**：为了复用 2D 翻转的全部能力（按 `prob` 掷骰子、翻转图像与 2D 框、`_choose_direction` 等）。`transform` 里检测到 `img` 键时直接调 `super().transform(input_dict)` 完成 2D 部分；同时构造函数把 `flip_ratio_bev_horizontal` 作为 `prob` 传给父类，使 `sync_2d=True` 时图像与 BEV 使用同一次随机决策。

**练习 2**：`flip_box3d=False` 用在什么场景？此时 `points` 还会被翻转吗？

**答案**：用于相机系 BEV 检测——2D 图像的镜像翻转不改变相机坐标系下 3D 框的真值几何，翻框反而制造错误监督。看 L160 的 `if self.flip_box3d:` 可知整个框/点翻转块被跳过，`points` 键（如果有）不会被翻转，只有 `centers_2d`、`cam2img` 等 2D 字段被更新。

**练习 3**：`tensor[:, 1::7]` 这个奇怪的切片为什么能顺带处理速度？

**答案**：7 维框布局为 (x, y, z, l, w, h, yaw)，列 1 是 y；9 维框在其后追加 (vx, vy) 占列 7、8。从列 1 开始以步长 7 取列得到 {1, 8}，恰好是 y 与 vy——镜像世界中 y 位置和 y 方向速度必须同时取负，一切片完成两件事。

---

### 4.3 GlobalRotScaleTrans：全局旋转、缩放、平移（本讲主实践）

#### 4.3.1 概念说明

`GlobalRotScaleTrans` 对整个场景施加三步复合变换，顺序固定为 **R（旋转）→ S（缩放）→ T（平移）**：

\[ \text{center}_{new} = (\text{center}_{old} \cdot R_T)\cdot s + t, \qquad \text{yaw}_{new} = \text{yaw}_{old} + \theta \]

其中 \(R_T\) 是代码里名为 `rot_mat_T` 的 3×3 旋转矩阵（绕重力轴 z、角度 θ，以「行向量右乘」的转置形式存放），\(s\) 是标量缩放因子，\(t\) 是平移向量。注意**旋转改变 yaw、缩放改变尺寸**：

- 点侧：坐标列（前 3 维）依次被旋转、缩放、平移；强度等属性列不动（见 `BasePoints.scale` 只乘 `[:, :3]`）。
- 框侧：中心与尺寸（列 0:6）同样处理，yaw 只加 θ 不缩放，9 维框的速度矢量还要跟着旋转。

四个「 Added Keys 」是本讲反复强调的审计线索：`pcd_rotation`（矩阵）、`pcd_rotation_angle`（θ 标量）、`pcd_scale_factor`（s）、`pcd_trans`（t）。它们会被 `Pack3DDetInputs` 装进 `data_samples` 的 metainfo，流向多模态融合与可视化。

#### 4.3.2 核心流程

```text
transform(input_dict)
 ├─ 初始化 transformation_3d_flow
 ├─ ① _rot_bbox_points：θ ~ Uniform(rot_range)
 │     有框 → gt_bboxes_3d.rotate(θ, points)   # 框转、点转、返回 rot_mat_T
 │     无框 → points.rotate(θ)                  # 分割任务只有点
 │     记录 pcd_rotation / pcd_rotation_angle
 ├─ ② 若上游未注入 pcd_scale_factor → _random_scale：s ~ Uniform(scale_ratio_range)
 │     _scale_bbox_points：points.scale(s)（可选 shift_height 连高度属性一起缩放）
 │                        gt_bboxes_3d.scale(s)（中心+尺寸+速度，yaw 除外）
 └─ ③ _trans_bbox_points：t ~ Normal(0, translation_std)
       points.translate(t) 与 gt_bboxes_3d.translate(t)，记录 pcd_trans
 追加 'R','S','T' 到 flow
```

两个容易忽略的细节：

- **顺序不可交换**。`R→S→T` 意味着缩放作用在「已旋转」的坐标上、平移加在最后。逆变换（coord_transform.py）之所以要倒序执行，正是因为这里固定了顺序。
- `pcd_scale_factor` 的「若不存在才采样」设计（L792-793）服务于测试时增强：`MultiScaleFlipAug3D` 的 `pts_scale_ratio` 会预先把这个键写进 input_dict，训练时的随机采样就会被接管。

#### 4.3.3 源码精读

**主入口与三步调度**：[transforms_3d.py:L775-L799](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L775-L799) 的 `transform`：L790 旋转、L792-794 缩放（含「已存在则跳过采样」）、L796 平移、L798 追加 `['R', 'S', 'T']`。

**旋转**：[transforms_3d.py:L712-L736](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L712-L736) 的 `_rot_bbox_points`。L723 从 `rot_range` 均匀采样 θ；L725-730 **有框时走 `gt_bboxes_3d.rotate(θ, points)`**——又一次「框的方法带点一起变」；L732-733 无框时只转点（分割管线）。

**缩放**：[transforms_3d.py:L738-L759](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L738-L759) 的 `_scale_bbox_points` 与 [L761-L773](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L761-L773) 的 `_random_scale`。L751-754 的 `shift_height=True` 分支把室内点云的「离地高度」属性列也乘上 s——高度随场景等比缩放才自洽。

**平移**：[transforms_3d.py:L694-L710](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L694-L710) 的 `_trans_bbox_points`，\(t\) 从各向异性的高斯 \(N(0, \text{translation\_std}^2)\) 采样（KITTI 配置默认 `[0,0,0]` 即不平移）。

**框侧旋转底层**：[lidar_box3d.py:L82-L137](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/lidar_box3d.py#L82-L137) 的 `LiDARInstance3DBoxes.rotate`。L108-113 调 `rotation_3d_in_axis` 把中心绕 `YAW_AXIS`（LiDAR 系为 z）旋转并拿回 `rot_mat_T`；L121 `self.tensor[:, 6] += angle` 补上 yaw；L123-125 对 9 维框用 `rot_mat_T[:2, :2]` 同步旋转速度矢量；L127-137 按参数类型（Tensor / ndarray / BasePoints）把传入的点用**同一个** `rot_mat_T` 旋转后返回——框与点严格共享一次旋转，这就是一致性保障的全部秘密。

**点侧与框侧的基础方法**：点旋转 [base_points.py:L167-L197](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/base_points.py#L167-L197)（接受角度或现成矩阵，`axis=None` 时用子类的 `rotation_axis`）；点缩放 [base_points.py:L302-L308](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/base_points.py#L302-L308)（只乘前 3 列）；点平移 [base_points.py:L209-L228](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/base_points.py#L209-L228)；框平移 [base_box3d.py:L249-L258](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/base_box3d.py#L249-L258)（只加中心 3 列）；框缩放 [base_box3d.py:L311-L318](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/base_box3d.py#L311-L318)（`[:, :6]` 中心+尺寸、`[:, 7:]` 速度，**第 6 列 yaw 被跳过**）。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：写约 20 行调试脚本，直接实例化 `GlobalRotScaleTrans`，验证「框中心与点云经历完全相同的刚体变换」——即手工用 \( (\text{old} \cdot R_T)\cdot s + t \) 推算的结果与增强后实际值逐位一致。

**操作步骤**（示例代码，在仓库根目录以 `python -I` 运行）：

```python
import numpy as np
from mmdet3d.datasets.transforms import GlobalRotScaleTrans
from mmdet3d.structures import LiDARInstance3DBoxes, LiDARPoints

np.random.seed(42)
pts = np.random.rand(1000, 4).astype(np.float32) * [70, 80, 4, 1] - [0, 40, 3, 0]
gt = np.array([[10., 0., -1., 4., 2., 1.5, 0.7]], dtype=np.float32)

aug = GlobalRotScaleTrans(rot_range=[-0.78539816, 0.78539816],
                          scale_ratio_range=[0.95, 1.05],
                          translation_std=[0.5, 0.5, 0.])
out = aug(dict(points=LiDARPoints(pts.copy(), points_dim=4),
               gt_bboxes_3d=LiDARInstance3DBoxes(gt.copy())))

R, s, t = out['pcd_rotation'].numpy(), out['pcd_scale_factor'], out['pcd_trans']
expect_c = gt[0, :3] @ R * s + t          # 手工推算：旋转 → 缩放 → 平移
expect_yaw = gt[0, 6] + out['pcd_rotation_angle']
expect_p = pts[0, :3] @ R * s + t         # 对第一个点做同样推算

print('θ = %.4f, s = %.4f, flow = %s' % (out['pcd_rotation_angle'], s,
                                         out['transformation_3d_flow']))
print('框中心  实际:', out['gt_bboxes_3d'].tensor[0, :3].numpy(), ' 推算:', expect_c)
print('框 yaw   实际: %.4f  推算: %.4f' %
      (out['gt_bboxes_3d'].tensor[0, 6].item(), expect_yaw))
print('首点    实际:', out['points'].tensor[0, :3].numpy(), ' 推算:', expect_p)
assert np.allclose(out['gt_bboxes_3d'].tensor[0, :3].numpy(), expect_c, atol=1e-4)
assert np.allclose(out['points'].tensor[0, :3].numpy(), expect_p, atol=1e-4)
print('框与点云刚体变换一致性验证通过 ✓')
```

**需要观察的现象**：

1. `θ`、`s` 每次运行（换种子）都不同，但三条断言始终通过。
2. 框中心、首点的「实际值」与「推算值」在小数点后 4 位内一致。
3. 框的尺寸 `(4, 2, 1.5)` 被放大/缩小为原值的 s 倍，yaw 恰好加上 θ。
4. 点的第 4 列（强度）**完全没有变化**。

**预期结果**：所有断言通过，证明框与点共享同一个 `rot_mat_T`、同一个 s、同一个 t——「同步变换」不是靠事后对齐，而是靠在同一个方法调用里传参完成。

#### 4.3.5 小练习与答案

**练习 1**：为什么逆变换必须按 T→S→R 的倒序执行？写出逆变换公式。

**答案**：正变换是复合 \( f(p) = (pR_T)s + t \)，其逆自然是 \( f^{-1}(q) = ((q - t)/s)\,R_T^{-1} \)——先撤销最后施加的平移，再撤销缩放，最后撤销旋转。coord_transform.py 的 `flow[::-1]` 与 `scale=1/s`、`translate=-t`、`rotate=R⁻¹` 正是这个公式的代码化。

**练习 2**：9 维 nuScenes 框经过 `GlobalRotScaleTrans` 后，速度 (vx, vy) 会变成什么？

**答案**：速度是平面矢量，旋转时按 `rot_mat_T[:2, :2]` 旋转（lidar_box3d.py L123-125）；缩放时按 `tensor[:, 7:] *= s` 同乘缩放因子（base_box3d.py L318，注释就写着 `# velocity`）；平移不影响速度。即速度经历了「旋转 + 缩放」而位置经历「旋转 + 缩放 + 平移」。

**练习 3**：如果把 `rot_range` 设为 `[0, 0]`、`scale_ratio_range` 设为 `[1., 1.]`、`translation_std` 设为 `[0, 0, 0]`，这个变换还有用吗？

**答案**：有。此时它是一个「恒等但会补齐元信息」的变换：仍会写入 `pcd_rotation`（单位阵）、`pcd_scale_factor=1`、`pcd_trans=0` 和 flow。KITTI 的 test_pipeline 里正是这样用的（[kitti-3d-3class.py:L75-L89](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/kitti-3d-3class.py#L75-L89)），保证测试阶段 metainfo 字段齐全、多模态反变换链路不缺键。

---

### 4.4 ObjectNoise 与 noise_per_object_v3_：逐目标扰动

#### 4.4.1 概念说明

`GlobalRotScaleTrans` 把场景当整体，`ObjectNoise` 则相反：**每个真值框独立地**被随机平移和旋转一小段，模拟「标注定位误差」。难点在于物理合理性——两辆车不能被扰动到重叠。mmdet3d 的解法是「**碰撞测试 + 多候选重试**」：给每个框预算 `num_try=100` 组候选噪声，逐个尝试，第一个不与其他框碰撞的候选被采纳。

与全局增强的另一个差异：**只有落在某框内部的点才跟随该框移动**（路面、墙等背景点不动），这由逐点的 `point_masks` 决定。

#### 4.4.2 核心流程

`noise_per_object_v3_`（全部在 numpy 裸数组 + numba 中完成）：

```text
① 预采样候选噪声
     loc_noises ~ N(0, translation_std²)  形状 [N, num_try, 3]
     rot_noises  ~ U(rot_range)            形状 [N, num_try]
     global_rot_noises（可选手臂：让框绕原点公转）
② 把每个框的 7 维参数转成 8 个角点（box_np_ops.center_to_corner_box3d）
③ noise_per_box：对每个框 i，依次尝试第 j 组候选
     把角点绕框中心旋转 → 平移 → 与其它框的当前角点做 box_collision_test
     无碰撞 → success_mask[i] = j，采纳并更新 box_corners
④ _select_transform 按采纳编号挑出每个框的实际噪声
⑤ points_in_convex_polygon_3d_jit：算出每个点落在哪个框内（point_masks）
⑥ points_transform_：框内点绕所属框中心旋转 + 平移（每点只跟随第一个命中框）
⑦ box3d_transform_：框中心 += loc，yaw += rot
```

#### 4.4.3 源码精读

**Transform 壳**：[transforms_3d.py:L503-L530](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L503-L530) 的 `ObjectNoise.transform`——把框与点**降级成 numpy 裸数组**（源码注释特别标注 `# TODO: this is inplace operation`），调 `noise_per_object_v3_` 原地修改后，再用 `new_box` / `new_point` 重新包装回对象。这个「降级—计算—升级」的往返是 numba 世界与对象世界之间的桥。

**总调度**：[data_augment_utils.py:L330-L411](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/data_augment_utils.py#L330-L411) 的 `noise_per_object_v3_`。L355-370 把标量参数规范成区间/三维标准差并默认 `valid_mask` 全真；L372-382 一次性预采样三组噪声候选；L384-390 计算角点；L393-399 按 `enable_grot` 分派到两个版本的逐框尝试函数；L401-411 选中候选、算点掩码、逐点变换、最后改框。

**碰撞测试**：[data_augment_utils.py:L31-L125](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/data_augment_utils.py#L31-L125) 的 `box_collision_test`：先用「站立矩形」（外接轴对齐矩形）粗筛（L54-60 的 iw/ih），再对 4×4 条边做线段相交判定（L62-85），最后处理完全包含的退化情形（L86-124）。返回 (N, K) 布尔碰撞矩阵。

**逐框尝试**：[data_augment_utils.py:L128-L166](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/data_augment_utils.py#L128-L166) 的 `noise_per_box`：对每个有效框依次取候选 j，把角点平到框中心、用 `_rotation_box2d_jit_`（[L13-L28](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/data_augment_utils.py#L13-L28)）旋转再加回中心与平移，与其它框碰撞测试（L158-160 顺手把自己那列置 False）；第一个无碰撞的候选即被采纳（L162-165）。带全局公转的版本 [L169-L233](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/data_augment_utils.py#L169-L233) 用极坐标重写目标位置，思想相同。

**逐点变换**：[data_augment_utils.py:L283-L310](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/data_augment_utils.py#L283-L310) 的 `points_transform_`：先为每个框用 `_rotation_matrix_3d_`（[L254-L280](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/data_augment_utils.py#L254-L280)）造旋转矩阵（axis=2，绕 z），再对每个点找它所属的第一个有效框，执行「减中心 → 右乘旋转 → 加中心 → 加平移」，随后 `break`——**每个点只跟随一个框**。框本身的更新在 [L313-L327](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/data_augment_utils.py#L313-L327) 的 `box3d_transform_`：中心加 loc、yaw 加 rot，一行一个。

#### 4.4.4 代码实践

**实践目标**：跟踪 `ObjectNoise.transform` → `noise_per_object_v3_` 的调用链，用源码回答「哪些点动了、哪些点没动」。

**操作步骤**：

1. 阅读 [transforms_3d.py:L503-L530](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L503-L530)，列出进入 numba 前后数据的类型变化（`LiDARInstance3DBoxes → ndarray → new_box`）。
2. 在 [data_augment_utils.py:L330-L411](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/data_augment_utils.py#L330-L411) 中找到：噪声在哪一行采样、碰撞测试在哪一行被调用、点的归属掩码在哪一行计算、框在哪一行被最终更新。
3. （可选，需安装 numba，示例代码）构造两个不相交的框验证碰撞机制：

```python
import numpy as np
from mmdet3d.datasets.transforms import ObjectNoise
from mmdet3d.structures import LiDARInstance3DBoxes, LiDARPoints

np.random.seed(7)
pts = np.random.rand(500, 4).astype(np.float32) * [60, 60, 3, 1] - [0, 30, 3, 0]
gt = np.array([[10., 0., -1., 4., 2., 1.5, 0.5],
               [30., 5., -1., 4., 2., 1.5, -0.5]], dtype=np.float32)
out = ObjectNoise(translation_std=[1., 1., 0.5],
                  rot_range=[-0.78, 0.78], num_try=100)(
    dict(points=LiDARPoints(pts.copy(), points_dim=4),
         gt_bboxes_3d=LiDARInstance3DBoxes(gt.copy())))
print('扰动前中心:\n', gt[:, :3])
print('扰动后中心:\n', out['gt_bboxes_3d'].tensor[:, :3].numpy())
print('中心位移量:', (out['gt_bboxes_3d'].tensor[:, :3].numpy() - gt[:, :3]))
```

**需要观察的现象**：两个框的中心都发生了厘米到米级的位移、yaw 有小变化；由于碰撞测试约束，两框不会重叠（可打印 `out['gt_bboxes_3d'].overlaps(out['gt_bboxes_3d'])` 验证非对角线接近 0）。

**预期结果**：位移量大致在 `translation_std` 量级内；背景点（不在任何框内的点）坐标不变。若 numba 不可用则本步骤标注「待本地验证」，仅完成步骤 1-2 的源码阅读。

#### 4.4.5 小练习与答案

**练习 1**：`points_transform_` 里每个点处理完第一个命中框就 `break`，如果一个点同时落在两个框内（粘贴目标重叠时可能出现），会发生什么？

**答案**：该点只跟随第一个框（框遍历顺序即 `gt_bboxes_3d` 的行序）移动，另一个框「以为」这个点属于自己但它没有动。这是有意的工程取舍——点只能有一个位移，否则几何被撕裂；这也是 `ObjectSample` 粘贴前要 `remove_points_in_boxes` 清理采样框区域点的原因之一。

**练习 2**：`ObjectNoise` 与 `RandomJitterPoints`（[transforms_3d.py:L259-L324](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L259-L324)）都是加噪声，本质区别是什么？

**答案**：`RandomJitterPoints` 给**每个点独立**加高斯噪声且**不**同步移动任何框（docstring 明确说它只用于分割任务，检测任务请用 `ObjectNoise`）；`ObjectNoise` 按**框**为单位成组移动「框内点 + 框」，保持点—框包含关系。前者破坏包含关系，后者保持。

**练习 3**：`noise_per_object_v3_` 为什么把所有噪声候选一次性预采样成 `[N, num_try, ...]` 的大数组，而不是失败一个再采一个？

**答案**：为了让整个「逐框尝试 + 碰撞测试」循环都在 numba 编译的代码里完成。`njit` 函数内不能调用 Python 的 `np.random` 高级封装之外的任意对象，且预采样让随机数生成与几何计算两个阶段分离，循环内只剩纯数值操作，JIT 收益最大。

---

### 4.5 范围过滤与收尾：ObjectRangeFilter、PointsRangeFilter 与 PointShuffle

#### 4.5.1 概念说明

经过整条增强链后，场景已经「变形」：目标可能被推出感知范围、点云边缘可能出现新的越界点、yaw 可能累计超出 \([-π, π)\)。三个收尾 Transform 各管一件事：

- **ObjectRangeFilter**：按 `point_cloud_range` 过滤**框**——只保留 BEV 中心仍在范围内的真值，防止模型被要求预测「感知范围之外」的目标。
- **PointsRangeFilter**：按同一范围过滤**点**——越界点对模型无用还会浪费体素化内存。
- **PointShuffle**：打乱点的顺序。点云文件的点序带有空间局部性（扫描线顺序），洗牌能消除数据加载顺序带来的隐性偏置，也便于某些按序采样的操作。

#### 4.5.2 核心流程

```text
ObjectRangeFilter:
  根据 gt_bboxes_3d 的坐标系子类选 BEV 平面
    LiDAR/Depth 系 → bev_range = range[[0,1,3,4]]   # x、y 的下/上界
    Camera 系      → bev_range = range[[0,2,3,5]]   # x、z 的下/上界
  mask = gt_bboxes_3d.in_range_bev(bev_range)        # 框中心是否在范围内
  用同一个 mask 同步索引框与 gt_labels_3d
  limit_yaw: yaw 归一到 [-π, π)

PointsRangeFilter:
  mask = points.in_range_3d(range)                   # 逐点六不等式（严格 < >）
  points = points[mask]；若存在逐点 mask（pts_instance/semantic_mask）也同步索引

PointShuffle:
  idx = points.shuffle()（torch.randperm）
  逐点 mask 用 idx 同步重排
```

#### 4.5.3 源码精读

**框过滤**：[transforms_3d.py:L863-L895](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L863-L895) 的 `ObjectRangeFilter.transform`。L874-878 按 `isinstance` 分派 BEV 平面——相机系 BEV 是 x-z 平面（y 是重力轴），所以要换列。L882 调 `in_range_bev`，其实现见 [base_box3d.py:L181-L203](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/base_box3d.py#L181-L203)（对框中心的 BEV 坐标做四个比较）。L884-888 的注释值得细读：mask 是 torch 张量而标签是 numpy 数组，直接索引在 `len==1` 时会把 `mask=1` 误解为「取第 1 个元素」导致越界，所以先 `.numpy().astype(bool)` 再索引——一个真实的类型系统坑。L891 调 `limit_yaw`（[base_box3d.py:L320-L327](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/base_box3d.py#L320-L327)，内部用 `limit_period` 归一）。

**点过滤**：[transforms_3d.py:L925-L950](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L925-L950) 的 `PointsRangeFilter.transform`：L936 调 `in_range_3d`（六个**严格**不等式，实现见 [base_points.py:L230-L254](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/base_points.py#L230-L254)）；L941-948 若存在室内逐点 mask 则用同一个布尔掩码同步过滤——延续了 u3-l4 讲过的「逐点 mask 长度恒等于点数」不变量。

**洗牌**：[transforms_3d.py:L811-L841](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L811-L841) 的 `PointShuffle`：L825 调 `shuffle()` 拿回索引（[base_points.py:L157-L165](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/base_points.py#L157-L165)），再把逐点 mask 按同一索引重排。

**配置位置**：[kitti-3d-3class.py:L61-L63](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/kitti-3d-3class.py#L61-L63)——两个过滤器紧挨着放在 `GlobalRotScaleTrans` 之后、`PointShuffle` 之前；注意测试管线里只保留 `PointsRangeFilter`（[L75-L89](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/kitti-3d-3class.py#L75-L89)），因为测试没有真值框可过滤。

#### 4.5.4 代码实践

**实践目标**：观察范围过滤器如何把「被增强推出范围」的框裁掉。

**操作步骤**（示例代码）：

```python
import numpy as np
from mmdet3d.datasets.transforms import ObjectRangeFilter
from mmdet3d.structures import LiDARInstance3DBoxes

rng = [0, -40, -3, 70.4, 40, 1]
gt = np.array([
    [10., 0., -1., 4., 2., 1.5, 0.7],      # 范围内，保留
    [69., 0., -1., 4., 2., 1.5, 0.7],      # 贴近 x 上界，保留
    [72., 0., -1., 4., 2., 1.5, 0.7],      # x 超出 70.4，应被过滤
    [10., 45., -1., 4., 2., 1.5, 0.7],     # y 超出 40，应被过滤
], dtype=np.float32)

out = ObjectRangeFilter(point_cloud_range=rng)(
    dict(gt_bboxes_3d=LiDARInstance3DBoxes(gt.copy()),
         gt_labels_3d=np.array([0, 1, 2, 0])))
print('过滤后框数:', len(out['gt_bboxes_3d'].tensor), '/ 4')
print('保留的中心:\n', out['gt_bboxes_3d'].tensor[:, :3].numpy())
print('保留的标签:', out['gt_labels_3d'])
```

**需要观察的现象**：只剩 2 个框；被保留的是中心 `x=10`、`x=69` 的两个；标签数组与框用同一 mask 同步缩减。

**预期结果**：`过滤后框数: 2 / 4`，保留标签 `[0 1]`。可再验证 `PointsRangeFilter`：构造若干越界点观察点数下降。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `ObjectRangeFilter` 对相机系框使用 `pcd_range[[0, 2, 3, 5]]`？

**答案**：相机坐标系（CAM）中 y 轴向下是重力轴，BEV 平面是 x-z 而不是 x-y。`pcd_range` 统一按 (x_min, y_min, z_min, x_max, y_max, z_max) 存储，所以取列 {0, 2, 3, 5} 即 x 与 z 的下/上界，组成相机系的 BEV 范围。

**练习 2**：`PointsRangeFilter` 的边界比较是严格不等式（`>` / `<`）。一个恰好在 x = 70.4 的点会被保留吗？

**答案**：不会。`in_range_3d` 用 `tensor[:, 0] < point_range[3]` 严格小于，等于边界的点被判为出界。同理框中心的 `in_range_bev` 也是严格比较。落在边界上的目标/点正好被切掉，属于实现约定。

**练习 3**：为什么测试管线（test_pipeline）里没有 `ObjectRangeFilter` 和 `PointShuffle`，只留了 `PointsRangeFilter`？

**答案**：测试阶段没有真值框（`gt_bboxes_3d` 不存在），`ObjectRangeFilter` 无从过滤；`PointShuffle` 是消除训练点序偏置的增强，测试加随机性只会让结果不可复现。而 `PointsRangeFilter` 保证输入点云与训练时同分布（同一感知范围），因此保留。

---

## 5. 综合实践

**任务：把本讲四个增强类串成迷你管线，并用 `apply_3d_transformation` 走一遍「正向增强 → 逆向还原」的完整闭环。**

背景：多模态模型（u6-l6 将讲到的 MVXNet）在融合图像特征时，靠 `pcd_*` 元信息把增强后的点变回原始坐标系。本实践让你亲手验证这条链路是可逆的。

步骤（示例代码）：

```python
import numpy as np
import torch
from mmengine.dataset import Compose
from mmdet3d.datasets.transforms import (GlobalRotScaleTrans, ObjectRangeFilter,
                                         PointsRangeFilter, RandomFlip3D)
from mmdet3d.models.layers.fusion_layers.coord_transform import apply_3d_transformation
from mmdet3d.structures import LiDARInstance3DBoxes, LiDARPoints

np.random.seed(3)
pts = np.random.rand(2000, 4).astype(np.float32) * [70, 80, 4, 1] - [0, 40, 3, 0]
gt = np.array([[10., 0., -1., 4., 2., 1.5, 0.7],
               [35., 10., -1., 3.5, 1.6, 1.6, -0.3]], dtype=np.float32)

pipeline = Compose([
    dict(type='RandomFlip3D', sync_2d=False,
         flip_ratio_bev_horizontal=0.5, flip_ratio_bev_vertical=0.),
    dict(type='GlobalRotScaleTrans', rot_range=[-0.78, 0.78],
         scale_ratio_range=[0.95, 1.05], translation_std=[0.5, 0.5, 0.]),
    dict(type='PointsRangeFilter', point_cloud_range=[0, -40, -3, 70.4, 40, 1]),
    dict(type='ObjectRangeFilter', point_cloud_range=[0, -40, -3, 70.4, 40, 1]),
])
out = pipeline(dict(points=LiDARPoints(pts.copy(), points_dim=4),
                    gt_bboxes_3d=LiDARInstance3DBoxes(gt.copy())))
print('flow =', out['transformation_3d_flow'])
print('增强后 点数 %d / 框数 %d' % (len(out['points']), len(out['gt_bboxes_3d'])))

# 用元信息把增强后的点「逆变换」回原始坐标系（多模态融合的真实做法）
aug_pts = out['points'].tensor.clone()
img_meta = {k: out[k] for k in ('pcd_rotation', 'pcd_scale_factor', 'pcd_trans',
                                'pcd_horizontal_flip', 'pcd_vertical_flip',
                                'transformation_3d_flow')}
restored = apply_3d_transformation(aug_pts, 'LIDAR', img_meta, reverse=True)
```

观察与思考：

1. `transformation_3d_flow` 可能是 `['HF', 'R', 'S', 'T']` 或 `['R', 'S', 'T']`（取决于翻转骰子），顺序与 4.1.2 的管线顺序一致。
2. 两个过滤器过后点数会明显下降（大量点被翻转/旋转推出范围）。
3. `restored` 中**未被过滤器裁掉**的那些点，应能近似回到原始坐标——但由于过滤删除了部分点，无法与原 `pts` 逐行对齐。请思考：这说明多模态融合「逆变换」为何必须发生在**过滤之前的信息**之上？（提示：逆变换只还原图形，不能复活被删掉的点；这也是 `pcd_*` 元信息要随样本一起打包进 metainfo 的原因。）
4. 若 numba 未安装，去掉 ObjectNoise（本实践未用到，无碍）并标注「待本地验证」。

## 6. 本讲小结

- **排序即语义**：KITTI 训练管线按「内容增改（ObjectSample/ObjectNoise）→ 全局几何（RandomFlip3D/GlobalRotScaleTrans）→ 范围收尾（两个 Filter + PointShuffle）」三段式组织；过滤必须在最后，否则增强会把目标推出感知范围而无人清理。
- **同步变换的实现模式**：不是分别变换点与框，而是「框对象的方法带点对象一起变」——`gt_bboxes_3d.flip(direction, points=...)`、`gt_bboxes_3d.rotate(angle, points=...)` 内部用同一矩阵/规则同时处理两者。
- **GlobalRotScaleTrans = R→S→T**：\( \text{new} = (\text{old} \cdot R_T)\cdot s + t \)，yaw 只加 θ、尺寸乘 s、速度矢量旋转且缩放、强度列不动；同时把 `pcd_rotation / pcd_rotation_angle / pcd_scale_factor / pcd_trans` 与 `transformation_3d_flow` 写入 data dict 作为审计线索。
- **RandomFlip3D 的同步三件套**：BEV 翻转与 2D 图像翻转共用决策（`sync_2d`）；单目 3D 还要镜像 `centers_2d` 与 `cam2img` 主点；`tensor[:, 1::7]` 切片让 y 与 vy 同时取负。
- **ObjectNoise 的碰撞预算制**：预采样 `[N, num_try, ...]` 候选噪声，numba 加速逐框尝试 + `box_collision_test` 碰撞检测，只有框内的点跟随各自框移动。
- **收尾三件事**：ObjectRangeFilter 按坐标系选 BEV 平面过滤框并 `limit_yaw`；PointsRangeFilter 用六个严格不等式裁点；二者都用同一 mask 同步索引标签/逐点 mask；PointShuffle 消除点序偏置。

## 7. 下一步学习建议

本讲把「几何与点级增强」讲完了，管线还剩两块拼图，建议按序继续：

1. **u3-l7（GT 数据库采样）**：本讲一笔带过的 `ObjectSample` 详解——`create_gt_database` 如何离线生成 dbinfos，`DataBaseSampler.sample_all` 如何按类采样目标点云粘贴进场景，以及粘贴后框与标签如何拼接。
2. **u3-l8（Pack3DDetInputs）**：本讲反复出现的 `pcd_rotation`、`transformation_3d_flow` 等键最终如何被装进 `Det3DDataSample` 的 metainfo，`dict(inputs, data_samples)` 的最终形态是什么。
3. 源码延伸阅读：[mmdet3d/structures/bbox_3d/utils.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/utils.py) 中的 `rotation_3d_in_axis` 与 `limit_period`——本讲的旋转矩阵与 yaw 归一都落在它们身上；以及 [mmdet3d/datasets/transforms/test_time_aug.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/test_time_aug.py) 的 `MultiScaleFlipAug3D`，看它如何复用本讲的增强类做测试时增强（u7-l4 会正式讲）。

# 三大坐标系与 Box3DMode/Coord3DMode

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 LIDAR、CAM、DEPTH 三个坐标系各自的方向约定（哪根轴朝前、哪根轴朝上）、旋转轴与典型适用场景。
2. 使用 `Box3DMode.convert` 与 `Coord3DMode.convert` 完成 3D 框和点云的坐标系转换，并解释转换过程中坐标、尺寸、yaw 三者分别发生了什么。
3. 知道转换矩阵（如 `lidar2cam`、标定矩阵 `Rt`）在 info pkl 中的存放位置，以及数据集类在何处取用它。

本讲是单元 2（核心数据结构）的第一讲。在 [u1-l4](u1-l4-repository-structure-map.md) 中我们看到 `mmdet3d/structures` 位于星形依赖的最底层——本讲就从这个最底层中最重要的「坐标系约定」讲起。后面所有的数据集类（单元 3）、检测器（单元 4-6）、评估指标（单元 7）都建立在这套约定之上。

## 2. 前置知识

**为什么 3D 检测需要不止一套坐标系？**

在 2D 图像检测中，坐标约定几乎只有一种：原点在左上角，x 向右，y 向下。但 3D 世界里的数据来自不同物理设备，每台设备自带一套「出厂坐标」：

- **LiDAR（激光雷达）** 通常以自身旋转轴为 z 轴（朝上），前进方向为 x 轴。
- **相机** 遵循计算机视觉惯例：光轴为 z 轴（朝前），图像右方向为 x 轴，下方向为 y 轴。
- **深度相机 / RGB-D 数据**（如室内场景的 Kinect）产生的点云常用另一种约定：z 朝上，y 朝前。

此外，不同数据集与经典论文也各自「站队」：SECOND 等室外 LiDAR 方法用 LiDAR 系，VoteNet 等室内方法用 Depth 系，KITTI 原始标注却是相机系。MMDetection3D 不强行统一成一套，而是把三种约定都显式建模出来，并提供两者之间互转的工具——这就是本讲的两个主角 `Box3DMode` 与 `Coord3DMode`。

**回顾：7 自由度 3D 框。** 在 [u1-l3](u1-l3-first-inference-demo.md) 中我们已经见过，一个 3D 框由 7 个数描述：

\[
(x,\ y,\ z,\ dx,\ dy,\ dz,\ r)
\]

- \((x, y, z)\)：框的底部中心（bottom center）坐标；
- \((dx, dy, dz)\)：框沿三个坐标轴方向的边长（尺寸）；
- \(r\)：yaw 角（偏航角），即框在水平面内的朝向。

注意：这 7 个数的「含义」依赖坐标系——同样是 \(dx\)，在 LiDAR 系里是沿「前方」的边长，在相机系里是沿「右方」的边长。所以坐标系转换必须同时处理坐标、尺寸、yaw 三个量，缺一不可。

**yaw 的统一定义。** MMDetection3D 中三个坐标系都是右手系。取重力轴（LiDAR/DEPTH 的 z 轴、CAM 的 y 轴），在其垂直平面 \(\Pi\) 上取 x 轴正方向为参考方向（yaw = 0），yaw 角随方向逆时针（从重力轴负方向望向原点时）递增。一个框 yaw = 0 意味着它的「朝向边 \(dx\)」与 x 轴平行。官方文档用一句口诀概括：**框的朝向永远与 \(dx\) 边平行**。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [mmdet3d/structures/bbox_3d/box_3d_mode.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/box_3d_mode.py) | 定义 `Box3DMode` 枚举（LIDAR/CAM/DEPTH），并提供框坐标系转换的核心静态方法 `convert` |
| [mmdet3d/structures/bbox_3d/coord_3d_mode.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/coord_3d_mode.py) | 定义 `Coord3DMode` 枚举，统一入口：按输入类型自动分发给 `convert_box`（转给 Box3DMode）或 `convert_point`（点云专用转换） |
| [mmdet3d/structures/points/base_points.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/base_points.py) | `BasePoints` 基类：点云张量 `(N, points_dim)` 布局与 `rotation_axis` 约定 |
| [mmdet3d/structures/points/lidar_points.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/lidar_points.py) | `LiDARPoints` 子类：`rotation_axis=2`，`convert_to` 委托 `Coord3DMode.convert_point` |
| [mmdet3d/structures/bbox_3d/utils.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/utils.py) | `limit_period` 等工具：把角度限制到一个周期内 |
| [mmdet3d/datasets/kitti_dataset.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/kitti_dataset.py) | `lidar2cam` 矩阵的消费现场：把相机系标注转回 LiDAR 系 |
| [docs/en/user_guides/coord_sys_tutorial.md](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/user_guides/coord_sys_tutorial.md) | 官方坐标系教程，本讲的权威参照 |

记忆钩子：`Box3DMode` 管「框」，`Coord3DMode` 管「框 + 点」的统一入口；`BasePoints` 家族是点云版的「三个坐标系子类」，与框的三个子类（`LiDARInstance3DBoxes` / `CameraInstance3DBoxes` / `DepthInstance3DBoxes`，下一讲 [u2-l2](u2-l2-base-instance-3d-boxes.md) 详述）一一对应。

## 4. 核心概念与源码讲解

### 4.1 三大坐标系的方向约定

#### 4.1.1 概念说明

三个坐标系的方向图直接写在 `Box3DMode` 的类文档字符串里，这是全库最权威的定义处：

- **LIDAR**：z 朝上，x 朝前，y 朝左。用于 KITTI/nuScenes/Waymo 等 LiDAR 数据与 SECOND、PointPillars、CenterPoint 等室外算法。
- **CAM**：x 朝右，y 朝下，z 朝前。用于单目 3D 检测（FCOS3D、SMOKE）与 KITTI 原始标注。
- **DEPTH**：z 朝上，y 朝前，x 朝右。用于 ScanNet、SUN RGB-D 等室内数据与 VoteNet、H3DNet 等室内算法。

#### 4.1.2 核心流程

把三个坐标系的关键差异整理成一张对照表：

| | LIDAR | CAM | DEPTH |
| --- | --- | --- | --- |
| 上 | z | —（y 朝下） | z |
| 前 | x | z | y |
| 右/左 | y 朝左 | x 朝右 | x 朝右 |
| 重力轴（yaw 旋转轴） | z（axis=2） | y（axis=1） | z（axis=2） |
| 框底部中心的相对坐标 | (0.5, 0.5, 0) | (0.5, 1.0, 0.5) | (0.5, 0.5, 0) |
| 典型场景 | 室外 LiDAR 检测 | 单目 3D / KITTI 原始标注 | 室内点云检测 |

「底部中心的相对坐标」指框内一点在框自身归一化坐标系里的位置：LiDAR/DEPTH 框的 \((x,y,z)\) 指底部面中心（z 在框的最低面），而 CAM 框因 y 朝下，\((x,y,z)\) 指的是几何中心。这解释了为什么 CAM 的相对坐标是 (0.5, 1.0, 0.5)——沿 y 方向覆盖整个高度。

#### 4.1.3 源码精读

三个坐标系的 ASCII 方向图与「底部中心相对坐标 + 旋转轴」说明，写在类文档字符串中：

[box_3d_mode.py:L16-L61](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/box_3d_mode.py#L16-L61) —— `Box3DMode` 的文档字符串，逐一定义 LIDAR/CAM/DEPTH 的轴向、底部中心相对坐标与 yaw 旋转轴。这段文档就是「单一事实来源」，`Coord3DMode` 的文档字符串（[coord_3d_mode.py:L16-L60](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/coord_3d_mode.py#L16-L60)）与其完全一致。

[box_3d_mode.py:L63-L65](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/box_3d_mode.py#L63-L65) —— 三个枚举值：`LIDAR = 0`、`CAM = 1`、`DEPTH = 2`。它们继承自 `IntEnum`，所以可以和整数 0/1/2 直接比较。

官方教程对应段落：[coord_sys_tutorial.md:L9-L49](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/user_guides/coord_sys_tutorial.md#L9-L49) 用文字再次表述三轴方向，并说明成因（设备差异 + 数据集惯例 + 论文传统）。

#### 4.1.4 代码实践

1. **实践目标**：把三个坐标系的轴向「背」下来，并能在 BEV（鸟瞰图）上徒手画出。
2. **操作步骤**：
   - 打开 [box_3d_mode.py:L16-L61](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/box_3d_mode.py#L16-L61) 的三张 ASCII 图。
   - 在纸上分别画出三个坐标系从「重力轴负方向」俯视的 BEV 图，标注 yaw=0 的参考方向（都是 x 轴正方向）。
3. **需要观察的现象**：LIDAR 与 DEPTH 的 BEV 图只差「前」是 x 还是 y；CAM 因为 y 朝下，俯视时它的 yaw 增长方向与另两个相反。
4. **预期结果**：你能不查资料说出「KITTI 的 LiDAR 点云中，一辆朝 +x 方向开的车 yaw 是多少」（答案：0，因为 yaw=0 即朝向与 x 轴平行）。

#### 4.1.5 小练习与答案

**练习 1**：在 LiDAR 坐标系中，一辆车头指向 +y 轴（正左方）的车，yaw 是多少？指向 -y（右方）呢？

**答案**：指向 +y 时 yaw \(= +\frac{\pi}{2}\)。LiDAR 系 z 朝上，从上方（z 轴负方向）俯视，x 前 y 左构成逆时针方向，yaw 逆时针为正；从 x 正方向逆时针转 90° 正好指向 y 正方向（左）。源码文档 [box_3d_mode.py:L24](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/box_3d_mode.py#L24) 的图里明确标注了 `(yaw=0.5*pi) left y`。指向 -y（右方）则为 \(+\frac{3\pi}{2}\)，经 `limit_period(2π)` 归一后是 \(-\frac{\pi}{2}\)。

**练习 2**：为什么 CAM 坐标系的 yaw 旋转轴是 axis=1（y 轴），而 LIDAR 是 axis=2（z 轴）？

**答案**：yaw 定义为绕「重力轴」的旋转。LiDAR 与 DEPTH 系中 z 轴朝上（重力轴），是第 3 个分量（索引 2）；CAM 系中 y 轴朝下指向地面，重力轴是第 2 个分量（索引 1）。见 [box_3d_mode.py:L30-L31](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/box_3d_mode.py#L30-L31) 与 [L46-L47](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/box_3d_mode.py#L46-L47)。

### 4.2 Box3DMode：框坐标系转换的引擎

#### 4.2.1 概念说明

`Box3DMode.convert` 是一个静态方法，负责把任意 `src` 模式的 3D 框转换到 `dst` 模式。它解决的核心问题是：**同一个物理框，在不同坐标系下，7 个参数要同时重写**。它接受四种输入（tuple/list、numpy 数组、Tensor、`BaseInstance3DBoxes` 实例），返回同类型的结果——如果你传入的是 `LiDARInstance3DBoxes`，它会返回 `CameraInstance3DBoxes` 等目标坐标系对应的子类。

#### 4.2.2 核心流程

转换分三步（以 LIDAR → CAM 为例）：

```text
输入 (x, y, z, dx, dy, dz, r)  @ LiDAR
  │
  ├─ ① 中心坐标：乘旋转矩阵 rt_mat（默认 [[0,-1,0],[0,0,-1],[1,0,0]]）
  │      x_cam = -y_lidar,  y_cam = -z_lidar,  z_cam = x_lidar
  │      等价写法：x_L = z_C, y_L = -x_C, z_L = -y_C
  │
  ├─ ② 尺寸重排：(dx, dy, dz) → (dx, dz, dy)     # y、z 互换
  │      因为 LiDAR 的 dz（高度）对应 CAM 的 dy（深度方向边长）
  │
  └─ ③ yaw 变换：r_cam = -r_lidar - π/2，再 limit_period 到 [-π, π)
  │
输出 (x', y', z', dx', dy', dz', r')  @ CAM
```

六个转换方向中 yaw 的变换公式汇总：

| 方向 | yaw 公式 | 尺寸重排 |
| --- | --- | --- |
| LIDAR → CAM | \(r' = -r - \frac{\pi}{2}\) | \((dx, dz, dy)\) |
| CAM → LIDAR | \(r' = -r - \frac{\pi}{2}\) | \((dx, dz, dy)\) |
| DEPTH → CAM | \(r' = -r\)（纯符号翻转） | \((dx, dz, dy)\) |
| CAM → DEPTH | \(r' = -r\)（纯符号翻转） | \((dx, dz, dy)\) |
| LIDAR → DEPTH | \(r' = r + \frac{\pi}{2}\) | 不变 \((dx, dy, dz)\) |
| DEPTH → LIDAR | \(r' = r - \frac{\pi}{2}\) | 不变 \((dx, dy, dz)\) |

`limit_period` 的数学定义：把值限制在 \([-offset \times period,\ (1-offset) \times period)\) 内，转换中用 `period=2π`，即把 yaw 归一化到 \([-π, π)\)。计算方式为：

\[
limited\_val = val - \lfloor val / period + offset \rfloor \times period
\]

之所以出现 \(-r - \frac{\pi}{2}\) 这样的公式，是因为两个坐标系的参考方向与旋转正方向都不同：先取负号修正「逆时针/顺时针」的差异，再平移 \(\frac{\pi}{2}\) 修正「x 前方 vs x 右方」的参考方向差异。

#### 4.2.3 源码精读

[box_3d_mode.py:L67-L99](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/box_3d_mode.py#L67-L99) —— `convert` 静态方法签名。参数 `rt_mat` 是可选的外部变换矩阵（真实传感器标定时必须提供），`correct_yaw=True` 表示 yaw 已随 `rt_mat` 一起被旋转、需要从向量重建。

[box_3d_mode.py:L103-L121](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/box_3d_mode.py#L103-L121) —— 输入类型识别与张量化。注意每一分支都做了 `clone()`（L114-L118），**避免修改调用方的原始框**；若传入 `BaseInstance3DBoxes` 实例，则强制采用它自身的 `with_yaw` 属性（L120-L121），忽略外部参数。

[box_3d_mode.py:L127-L141](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/box_3d_mode.py#L127-L141) —— LIDAR → CAM 分支：默认旋转矩阵（L128-L129）、尺寸交换 `torch.cat([x_size, z_size, y_size])`（L130）、yaw 变换 `yaw = -yaw - np.pi / 2` 加 `limit_period(yaw, period=np.pi * 2)`（L140-L141）。其余五个方向的分支结构完全对称（L142-L214）。

[box_3d_mode.py:L220-L227](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/box_3d_mode.py#L220-L227) —— 中心坐标变换。若 `rt_mat` 有 4 列（3×4 或 4×4，即含平移的齐次形式），先把坐标扩成齐次 \((x, y, z, 1)\) 再相乘；否则按纯 3×3 旋转处理。**这个设计让真实标定矩阵（旋转 + 平移）可以直接传入。**

[box_3d_mode.py:L229-L239](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/box_3d_mode.py#L229-L239) —— `correct_yaw=True` 时的通用路径：把 yaw 转成方向向量 \((\cos r, \sin r, 0)\)，让它跟着 `rt_mat` 一起旋转，再用 `atan2` 从旋转后的向量反解新 yaw。这适用于任意旋转的 `rt_mat`（此时固定公式 \(-r-\frac{\pi}{2}\) 不再成立）。

[box_3d_mode.py:L248-L265](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/box_3d_mode.py#L248-L265) —— 结果按目标模式装箱：`dst==CAM` 返回 `CameraInstance3DBoxes`，`dst==LIDAR` 返回 `LiDARInstance3DBoxes`，`dst==DEPTH` 返回 `DepthInstance3DBoxes`，并保留 `with_yaw` 与多余维度（速度等，见 L242-L246 的 `remains` 拼接）。

[utils.py:L12-L28](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/utils.py#L12-L28) —— `limit_period` 实现，一行核心公式在 L27。

#### 4.2.4 代码实践

见本讲 [5. 综合实践](#5-综合实践)的完整脚本，其中第一、二步就是本模块的实践：构造 `LiDARInstance3DBoxes` 后调用 `Box3DMode.convert` 转到 CAM 与 DEPTH，并核对 yaw 数值。

#### 4.2.5 小练习与答案

**练习 1**：一个 LiDAR 系中的框 yaw = 0.6，转换到 CAM 系后 yaw 是多少？再转回 LiDAR 系呢？

**答案**：CAM 系 yaw \(= -0.6 - \frac{\pi}{2} \approx -2.1708\)（在 \([-π, π)\) 内，`limit_period` 不再调整）。转回 LiDAR 时套 CAM→LIDAR 公式：\(-(-2.1708) - \frac{\pi}{2} = 2.1708 - 1.5708 = 0.6\)，往返还原——转换是可逆的。

**练习 2**：为什么 LIDAR → DEPTH 不需要交换尺寸，而 LIDAR → CAM 需要？

**答案**：LIDAR 与 DEPTH 的重力轴都是 z，水平面内两个方向都是「前/右」互换（x↔y），所以 \((dx, dy)\) 各自对应另一系里的「前向边/右向边」，语义连贯，无需交换（对比 [box_3d_mode.py:L188](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/box_3d_mode.py#L188) 的 `torch.cat([x_size, y_size, z_size])`）；而 CAM 的重力轴是 y，LiDAR 的高度 dz 在 CAM 里变成沿 y 的边长，所以必须交换 y、z 两个尺寸分量（[L130](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/box_3d_mode.py#L130)）。

**练习 3**：`rt_mat` 传 `None` 与传一个 3×4 的真实标定矩阵，`convert` 的行为有何不同？

**答案**：`rt_mat=None` 时使用「理想轴向对齐」的默认旋转矩阵，只做轴重排，不含平移（L128-L129 等六处）；传入 3×4 矩阵时走 [L222-L225](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/box_3d_mode.py#L222-L225) 的齐次坐标路径，坐标还会被平移（传感器不在同一点时必需），且此时通常应同时设 `correct_yaw=True` 走向量旋转路径重算 yaw。

### 4.3 Coord3DMode：框与点的统一转换入口

#### 4.3.1 概念说明

`Box3DMode` 只能转框；但 3D 场景里点云也要跟着一起换坐标系（比如把 LiDAR 点云投影到相机画 Fig）。`Coord3DMode` 在 `Box3DMode` 之上做了一层「类型分发」：看到 `BaseInstance3DBoxes` 就转框，看到 `BasePoints` 就转点，看到裸数组按 `is_point` 标志决定。**转框的逻辑它并不重写，而是原样委托给 `Box3DMode.convert`**——所以两个枚举的六个方向矩阵完全一致。

#### 4.3.2 核心流程

```text
Coord3DMode.convert(input, src, dst, ...)
  │
  ├─ input 是 BaseInstance3DBoxes ──► convert_box ──► Box3DMode.convert（4.2 已讲）
  ├─ input 是 BasePoints ──────────► convert_point（点云专用）
  └─ input 是裸 tensor/ndarray/list
        ├─ is_point=True（默认）──► convert_point
        └─ is_point=False ────────► convert_box
```

`convert_point` 比转框简单得多：点没有尺寸和 yaw，只需把每个点的前 3 维坐标乘 `rt_mat`，**其余维度（强度、颜色、时间戳等属性列）原样保留**。

#### 4.3.3 源码精读

[coord_3d_mode.py:L104-L127](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/coord_3d_mode.py#L104-L127) —— `convert` 的类型分发逻辑：L104-L111 处理框、L112-L113 处理点对象、L114-L125 处理裸数组（依 `is_point` 分流）、L126-L127 其余类型直接抛 `NotImplementedError`。

[coord_3d_mode.py:L129-L168](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/coord_3d_mode.py#L129-L168) —— `convert_box` 只是薄薄一层包装，函数体直接 `return Box3DMode.convert(...)`（L162-L168），验证了「不重复实现」的设计。

[coord_3d_mode.py:L216-L233](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/coord_3d_mode.py#L216-L233) —— `convert_point` 中六个方向的默认旋转矩阵，与 `Box3DMode.convert` 中的矩阵逐个相同（对照 [box_3d_mode.py:L127-L214](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/box_3d_mode.py#L127-L214)）。例如 LIDAR→CAM 同为 `[[0,-1,0],[0,0,-1],[1,0,0]]`。

[coord_3d_mode.py:L239-L249](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/coord_3d_mode.py#L239-L249) —— 点坐标变换核心：同样支持 4 列齐次矩阵（L241-L244）；L248-L249 把变换后的 \((x,y,z)\) 与原样保留的属性列 `arr[..., 3:]` 重新拼起来。**这就是多帧点云带强度、RGB 时属性不丢的原因。**

[coord_3d_mode.py:L251-L271](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/coord_3d_mode.py#L251-L271) —— 按目标模式返回 `CameraPoints` / `LiDARPoints` / `DepthPoints`，并把 `attribute_dims` 原样传给新对象（L268-L271），保证「第几列是强度/颜色」的元信息在转换后仍然有效。

#### 4.3.4 代码实践

同样见 [5. 综合实践](#5-综合实践)脚本第三步：用 `Coord3DMode.convert` 分别转换框（`is_point=False`）和点（默认 `is_point=True`），对比与 `Box3DMode.convert` 的结果是否一致。

#### 4.3.5 小练习与答案

**练习 1**：给 `Coord3DMode.convert` 传一个形状 (N, 7) 的裸 Tensor，不设 `is_point`，会发生什么？

**答案**：默认 `is_point=True`（[coord_3d_mode.py:L74](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/coord_3d_mode.py#L74)），它会走 `convert_point`：只把前 3 列当坐标旋转，后 4 列（尺寸、yaw）被当成「属性」原样保留——得到的是错误的框。**用裸数组转框必须显式 `is_point=False`**；更稳妥的做法是直接传 `BaseInstance3DBoxes` 实例，让类型分发自动生效。

**练习 2**：`LiDARPoints` 对象的 `convert_to` 方法（[lidar_points.py:L51-L72](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/lidar_points.py#L51-L72)）与 `Coord3DMode.convert_point` 是什么关系？

**答案**：`convert_to` 是实例方法语法糖，内部就是调用 `Coord3DMode.convert_point(point=self, src=Coord3DMode.LIDAR, dst=dst, rt_mat=rt_mat)`（L70-L72）。每个 Points 子类都有对应实现，源码里各数据集、可视化模块调 `points.convert_to(...)` 时走的正是这条链。

### 4.4 BasePoints 家族与 lidar2cam 矩阵的存放

#### 4.4.1 概念说明

`mmdet3d/structures/points/` 目录定义了点云的数据容器：`BasePoints` 基类加 `LiDARPoints` / `CameraPoints` / `DepthPoints` 三个坐标系子类——与框的三个子类完全平行。它回答两个问题：点云张量怎么存（`(N, points_dim)`，每行 `(x, y, z, ...)`），以及绕哪根轴旋转（`rotation_axis`）。

而「真实世界」的坐标系转换需要标定矩阵。以 KITTI 为例，`lidar2cam` 就是把 LiDAR 系坐标变到相机系 的 4×4 矩阵，它由两步复合而成：

\[
lidar2cam = R0\_rect \cdot Tr\_velo\_to\_cam
\]

其中 \(Tr\_velo\_to\_cam\) 是 Velodyne 到相机的平移旋转，\(R0\_rect\) 是相机内参修正旋转。这类矩阵在数据预处理阶段就被写进 info pkl，运行时由数据集类读取。

#### 4.4.2 核心流程

一个 GT 框从原始标注到模型可用格式的路径（以 KITTI LiDAR 训练为例）：

```text
KITTI 原始标注（CAM 系）
  │  tools/create_data.py 阶段：
  │    update_infos_to_v2.py 计算 lidar2cam = R0_rect @ Tr_velo_to_cam
  │    存入 info pkl: info['images']['CAM2']['lidar2cam']
  ▼
数据集加载阶段（mmdet3d/datasets/kitti_dataset.py）
  │  取出 lidar2cam，求逆 inv(lidar2cam)（即 cam2lidar）
  │  CameraInstance3DBoxes(gt).convert_to(LIDAR, rt_mat=inv(lidar2cam))
  ▼
LiDAR 系的 DepthInstance→LiDARInstance3DBoxes，喂给 PointPillars 等模型
```

#### 4.4.3 源码精读

[base_points.py:L33-L54](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/base_points.py#L33-L54) —— `BasePoints.__init__`：断言张量必须是二维且最后一维等于 `points_dim`（L46-L49），存 `tensor`、`points_dim`、`attribute_dims`，默认 `rotation_axis = 0`。`points_dim` 可以大于 3，多出的列由 `attribute_dims` 字典声明含义（如 `{'height': 3}`）。

[lidar_points.py:L30-L36](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/lidar_points.py#L30-L36) —— `LiDARPoints` 构造：调用父类后设 `self.rotation_axis = 2`（z 轴）。对比 `BasePoints` 的默认 0——每个坐标系子类的差异点之一就是这个旋转轴（CAM 子类为 1）。

[kitti_dataset.py:L166-L172](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/kitti_dataset.py#L166-L172) —— `lidar2cam` 的消费现场：L166 注释写明 `in kitti, lidar2cam = R0_rect @ Tr_velo_to_cam`；L167 从 `info['images']['CAM2']['lidar2cam']` 取出矩阵；L169-L171 把相机系 GT 框用**矩阵的逆**（cam→lidar）转换到 LiDAR 系。这正是 4.2 讲的 `rt_mat` 参数的真实用法——此时 `rt_mat` 含平移，走齐次坐标路径。

[update_infos_to_v2.py:L436-L437](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/update_infos_to_v2.py#L436-L437) —— `lidar2cam` 的生产现场（数据转换阶段）：`lidar2cam = rect @ Trv2c` 后存入 `temp_data_info['images']['CAM2']['lidar2cam']`。nuScenes 的对应逻辑在 L834、L917-L918（按相机键存入 `lidar2sensor`/`lidar2cam`）。Waymo 见 [waymo_converter.py:L462](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/waymo_converter.py#L462)。

[coord_3d_mode.py:L9-L10](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/coord_3d_mode.py#L9-L10) —— `coord_3d_mode.py` 顶部 import 四个 Points 类，体现「框模块」与「点模块」的协作：`Coord3DMode` 是二者共同的转换中枢。

#### 4.4.4 代码实践

在综合实践脚本的第四步，我们把点云也一起转坐标系，并验证框中心与点使用同一矩阵得到一致结果。

#### 4.4.5 小练习与答案

**练习 1**：`LiDARPoints` 的 `rotation_axis` 为什么是 2，`CameraPoints` 应该是多少？

**答案**：`rotation_axis` 指定刚体旋转（数据增强中的 `rotate`）绕哪根轴。LiDAR 系 z 朝上，重力轴索引为 2（[lidar_points.py:L36](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/lidar_points.py#L36)）；CameraPoints 在 `cam_points.py` 中设为 1（y 朝下为重力轴）。与 4.1 表格中「yaw 旋转轴」一致。

**练习 2**：为什么 `kitti_dataset.py` 里转 LiDAR 用的是 `np.linalg.inv(lidar2cam)` 而不是 `lidar2cam` 本身？

**答案**：矩阵名字表示的是变换方向：`lidar2cam` 把 LiDAR 坐标变到相机坐标。KITTI 原始标注本来就在相机系，要把它变到 LiDAR 系需要反方向的矩阵，即 `cam2lidar = inv(lidar2cam)`（[kitti_dataset.py:L169-L171](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/kitti_dataset.py#L169-L171)）。这是读 3D 代码的高频陷阱：看到 `xxx2yyy` 先默念「从 xxx 到 yyy」。

## 5. 综合实践

**任务**：构造一个 `LiDARInstance3DBoxes`，分别用 `Box3DMode.convert` 与 `Coord3DMode.convert` 把它转换到相机坐标系和深度坐标系，打印每次转换后的 tensor，验证 yaw 的符号翻转，并把一个点同步转换以核对一致性。

前置条件：按 [u1-l2](u1-l2-installation-and-verification.md) 完成安装，`import mmdet3d` 不报错。

**操作步骤**：把下面的脚本保存为 `coord_convert_demo.py`（示例代码，非仓库自带文件），运行 `python coord_convert_demo.py`：

```python
import math
import torch
from mmdet3d.structures.bbox_3d import (Box3DMode, Coord3DMode,
                                        LiDARInstance3DBoxes)

# 模拟一辆车：LiDAR 系下底部中心 (5, 2, -1)，尺寸 (dx=4, dy=2, dz=1.5)，yaw=0.6
box_tensor = torch.tensor([[5.0, 2.0, -1.0, 4.0, 2.0, 1.5, 0.6]])
lidar_box = LiDARInstance3DBoxes(box_tensor, box_dim=7)
print('LIDAR  :', lidar_box.tensor.numpy().round(4))

# 第 1 步：Box3DMode 转 CAM / DEPTH
cam_box = Box3DMode.convert(lidar_box, Box3DMode.LIDAR, Box3DMode.CAM)
depth_box = Box3DMode.convert(lidar_box, Box3DMode.LIDAR, Box3DMode.DEPTH)
print('CAM    :', cam_box.tensor.numpy().round(4), type(cam_box).__name__)
print('DEPTH  :', depth_box.tensor.numpy().round(4), type(depth_box).__name__)

# 第 2 步：手工验证 yaw 公式
print('yaw CAM   expect:', round(-0.6 - math.pi / 2, 4))
print('yaw DEPTH expect:', round(0.6 + math.pi / 2, 4))

# 第 3 步：Coord3DMode 做同样的框转换（应与第 1 步完全一致）
cam_box2 = Coord3DMode.convert(lidar_box, Coord3DMode.LIDAR,
                               Coord3DMode.CAM, is_point=False)
print('CAM(2) :', cam_box2.tensor.numpy().round(4))

# 第 4 步：Coord3DMode 转点，并与框中心对照
point = torch.tensor([[5.0, 2.0, -1.0]])  # 与框底部中心同位置的一个点
cam_point = Coord3DMode.convert(point, Coord3DMode.LIDAR, Coord3DMode.CAM)
print('point in CAM:', cam_point.numpy().round(4))
```

**需要观察的现象与预期结果**（可先笔算再对照输出）：

1. **中心坐标**：LIDAR→CAM 后应为 \((-2, 1, 5)\)，即 \(x_{cam}=-y_{lidar},\ y_{cam}=-z_{lidar},\ z_{cam}=x_{lidar}\)。
2. **尺寸**：CAM 下应为 \((4, 1.5, 2)\)——y、z 两个尺寸分量互换，dy 从 2 变成 1.5；DEPTH 下尺寸不变，仍为 \((4, 2, 1.5)\)。
3. **yaw 翻转**：CAM 下 \(r' = -0.6 - \frac{\pi}{2} \approx -2.1708\)；DEPTH 下 \(r' = 0.6 + \frac{\pi}{2} \approx +2.1708\)。两值恰好互为相反数——因为两条路径的差别正是 DEPTH→CAM 的纯翻转 \(r' = -r\)。
4. **返回类型**：`cam_box` 是 `CameraInstance3DBoxes`，`depth_box` 是 `DepthInstance3DBoxes`（[box_3d_mode.py:L254-L260](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/box_3d_mode.py#L254-L260) 的装箱逻辑）。
5. **第 3 步与第 1 步输出逐位相同**：证明 `Coord3DMode.convert_box` 只是委托 `Box3DMode.convert`。
6. **第 4 步**：点 \((5, 2, -1)\) 转到 CAM 为 \((-2, 1, 5)\)，与框中心完全一致——同一物理位置，框和点走各自的转换函数，结果必须吻合。

若输出与上述数值不符，先检查 `box_dim` 是否为 7、是否误用了 `is_point` 默认值。本实践在纯 CPU 上即可运行，无需 GPU 与数据集。（具体打印格式因 PyTorch 版本略有差异，数值以四舍五入到 4 位小数为准；待本地验证。）

## 6. 本讲小结

- MMDetection3D 显式建模三套右手坐标系：**LIDAR**（x 前 y 左 z 上，室外 LiDAR）、**CAM**（x 右 y 下 z 前，单目/KITTI 原始标注）、**DEPTH**（x 右 y 前 z 上，室内点云）；三者的重力轴与 yaw 旋转轴分别为 z/y/z。
- 一个 3D 框的 7 参数 \((x, y, z, dx, dy, dz, r)\) 的**语义依赖坐标系**，转换必须同时改写坐标（乘 `rt_mat`）、尺寸（重排分量）与 yaw（公式变换 + `limit_period` 归一到 \([-π, π)\)）。
- `Box3DMode.convert` 是框转换引擎，输入输出类型保持一致（传子类实例则返回目标坐标系子类）；`rt_mat` 支持 3×3 与含平移的 3×4/4×4，任意旋转时应配 `correct_yaw=True`。
- `Coord3DMode.convert` 是统一入口：按输入类型自动分发，框转换委托 `Box3DMode`，点转换只旋转前 3 维并原样保留属性列；裸数组转框必须显式 `is_point=False`。
- 点云容器 `BasePoints` 家族与框的三个子类平行，`rotation_axis`（2/1/2）记录各自的旋转轴；`LiDARPoints.convert_to` 是 `Coord3DMode.convert_point` 的语法糖。
- 真实跨传感器转换依赖标定矩阵：KITTI 中 `lidar2cam = R0_rect @ Tr_velo_to_cam`，由 `tools/dataset_converters/update_infos_to_v2.py` 写入 info pkl 的 `info['images']['CAM2']['lidar2cam']`，运行时由 `kitti_dataset.py` 取逆后把相机系标注转为 LiDAR 系。

## 7. 下一步学习建议

本讲只讲了「坐标怎么转」，还没有剖析承载 7 参数的容器本身。下一讲 [u2-l2-base-instance-3d-boxes.md](u2-l2-base-instance-3d-boxes.md) 将精读 `BaseInstance3DBoxes` 基类：`tensor` 属性、`bev` 投影、`corners` 顶点计算、`in_range_bev` 过滤以及三个子类的 yaw 约定差异。之后再进入 [u2-l3](u2-l3-point-cloud-structures.md) 的点云结构与 [u2-l4](u2-l4-det3d-data-sample.md) 的 `Det3DDataSample`，完成单元 2 的数据契约三部曲。想加深坐标直觉的读者，推荐对照阅读官方教程 [docs/en/user_guides/coord_sys_tutorial.md](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/user_guides/coord_sys_tutorial.md) 的「Relation with raw coordinate systems of supported datasets」一节，它逐数据集说明了原始标注与本库三坐标系的关系。

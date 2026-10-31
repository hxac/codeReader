# 点云结构:BasePoints 与三个坐标系子类

## 1. 本讲目标

上一讲(u2-l1)搞清了三套坐标系的**方向约定**,以及 `Box3DMode` / `Coord3DMode` 两个转换引擎;u2-l2 又剖析了 3D 框容器 `BaseInstance3DBoxes`。本讲把镜头对准与之完全平行的另一族数据结构——**点云容器**:LiDAR 或深度相机吐出来的几万到几十万个三维采样点,在 MMDetection3D 里是如何被表示、变换和跨坐标系转换的。

学完本讲,你应该能够:

1. 说出 `BasePoints` 的张量布局 \((N,\ \text{points\_dim})\):前 3 列固定是坐标,其余列通过 `attribute_dims` 登记语义(强度、高度、颜色等),并理解 `points_dim` 这个维度属性。
2. 独立使用 `LiDARPoints` 的 `translate` / `rotate` / `flip` / `scale` 四个刚体变换方法,并解释「为什么只动坐标列、属性列不动」。
3. 说明 `LiDARPoints` / `CameraPoints` / `DepthPoints` 三个子类各自的 `rotation_axis` 与 `flip` 方向差异,掌握 `convert_to` 与 `Coord3DMode.convert_point` 的关系,以及点侧 `new_point` 与框侧 `new_box` 的配套用法。
4. 在数据管线里指出「谁生产了 points 对象、谁在消费它」。

> **两个名词勘误**(以源码为准):
> ① 有些旧资料会把点云的维度属性写成 `points_catdim`——当前 1.x 源码中的属性名是 **`points_dim`**(`BasePoints.points_dim`),不存在 `points_catdim`;「按哪个维度拼接」的语义体现在类方法 `cat`(沿第 0 维拼接多个点云对象)。
> ② 本讲任务描述里的「shift(平移)」,在点云侧的真实方法名是 **`translate`**——`BasePoints` 没有 `shift` 方法,下文统一使用 `translate`。

## 2. 前置知识

### 2.1 点云在内存里就是一个二维数组

一帧点云就是 N 个采样点,每个点至少有 \((x, y, z)\) 三个坐标,常常还附带:

- **强度(intensity)**:激光打到物体表面反射回来的信号强度。KITTI 点云文件每点 4 个 float32,第 4 个就是强度;
- **高度(height)**:点相对地面的高度,室内算法(VoteNet 一族)常用;
- **颜色(color)**:RGB 三列,来自 RGB-D 相机或带彩色传感的设备。

所以点云的原始形态是一个 `(N, D)` 数组:每行一个点、前 3 列坐标、后面挂属性,**D 可以是 3、4、5、6、7……** 这正是 `points_dim` 要记录的东西。

### 2.2 刚体变换与「属性列不参与变换」

刚体变换 = 不改变物体形状的几何变换:

| 变换 | 数学形式(列向量约定) | 数据增强中的调用方(u3-l6 详解) |
|---|---|---|
| 平移 | \(p' = p + t\) | `GlobalRotScaleTrans` 的 `_trans_bbox_points` |
| 旋转 | \(p' = R\,p\) | `GlobalRotScaleTrans` 的 `_rot_bbox_points` |
| 缩放 | \(p' = s \cdot p\) | `GlobalRotScaleTrans` 的 `_scale_bbox_points` |
| 翻转(镜像) | 某一坐标分量取负 | `RandomFlip3D` |

关键点:**旋转、翻转、平移、缩放都只作用于坐标列;强度、颜色这类属性没有几何意义,不参与变换**(高度是几何量,但它是「属性列」,调用方需要时自行补缩放,见 4.2.3)。

### 2.3 承接 u2-l1 的坐标系结论

三套右手坐标系:LIDAR(x 前、y 左、z 上)、CAM(z 前、x 右、y 下)、DEPTH(y 前、x 右、z 上),重力轴分别为 z、y、z。`BasePoints` 的三个子类与之一一对应,**子类的全部差异都源于「重力轴是哪根、BEV 平面由哪两根轴构成」**。忘了方向约定请先回看 u2-l1。

### 2.4 torch.Tensor 的原地操作与 clone

`BasePoints` 内部存一个 `torch.float32` 张量,大量使用切片赋值(`self.tensor[:, :3] = ...`,**原地修改**)与 `clone()`(**深拷贝**)。这决定了哪些方法改变对象本身(`rotate` / `flip` / `translate` / `scale` 都是原地操作、返回 `None` 或矩阵),哪些方法返回新对象(`clone` / `to` / `convert_to` / 索引)。

## 3. 本讲源码地图

| 文件 | 作用 | 关注度 |
|---|---|---|
| `mmdet3d/structures/points/base_points.py` | `BasePoints` 基类:张量容器 + 通用变换 + 索引/拼接/搬运 | ⭐⭐⭐ 核心 |
| `mmdet3d/structures/points/lidar_points.py` | LiDAR 坐标系子类(全文仅 73 行) | ⭐⭐⭐ 核心 |
| `mmdet3d/structures/points/cam_points.py` | CAM 子类(重写 `bev`) | ⭐⭐⭐ 核心 |
| `mmdet3d/structures/points/depth_points.py` | DEPTH 子类 | ⭐⭐ |
| `mmdet3d/structures/points/__init__.py` | 导出四个类 + `get_points_type` 工厂 | ⭐⭐ |
| `mmdet3d/structures/bbox_3d/utils.py` | `rotation_3d_in_axis`:旋转的底层实现(与 3D 框共用) | ⭐⭐ |
| `mmdet3d/structures/bbox_3d/coord_3d_mode.py` | `Coord3DMode.convert_point`:跨坐标系转换引擎 | ⭐⭐⭐ |
| `mmdet3d/datasets/transforms/loading.py` | 管线中点云对象的**生产者**(`LoadPointsFromFile`) | ⭐⭐ |
| `mmdet3d/datasets/transforms/transforms_3d.py` | 变换方法的**消费者**(`RandomFlip3D` / `GlobalRotScaleTrans` / `PointsRangeFilter`) | ⭐⭐ |
| `tests/test_structures/test_points/test_base_points.py` | 官方单测,可当用法手册读 | ⭐ |

点云家族与上一讲的框家族(`BaseInstance3DBoxes` + 三子类)**结构完全平行**:基类管通用逻辑,子类只补坐标系差异;两者通过同一个 `rotation_3d_in_axis` 实现「点与框同步旋转」,这是数据增强几何一致性的根基。

## 4. 核心概念与源码讲解

### 4.1 BasePoints:点云张量的统一容器

#### 4.1.1 概念说明

为什么不让所有代码直接操作裸 `torch.Tensor`?因为裸张量**只有形状、没有语义**:

- 无法知道第 4 列是强度还是高度——增强时就不知道哪些列该跟着几何变换走;
- 无法知道这批点处于哪个坐标系——`flip`、`bev` 无法给出正确行为;
- 点数可能为 0(空场景)、数据可能在 CPU/GPU 间搬运,需要统一封装处理边界情况。

`BasePoints` 的方案是「**张量 + 元数据**」:

```text
BasePoints 对象
├── tensor          (N, points_dim) 的 float32 张量,前 3 列固定是坐标
├── points_dim      int,每个点的总维度数
├── attribute_dims  dict 或 None,登记属性列语义,如 {'height': 3} 或 {'color': [4,5,6], 'height': 7}
└── rotation_axis   int,默认旋转轴(重力轴),基类置 0,子类覆盖
```

一个细节:`flip` 与 `convert_to` 标了 `@abstractmethod`,但 `BasePoints` 没有继承 `ABCMeta`,所以它**仍可直接实例化**——官方单测里就有 `BasePoints(empty_boxes)` 的用法(见 4.1.3 末尾)。这里的 `@abstractmethod` 更多是「子类应当实现」的文档性约定,而非强制。

#### 4.1.2 核心流程

构造一个点云对象:

```text
输入(Tensor / ndarray / 嵌套列表)
    │ torch.as_tensor(..., dtype=float32)     ← 统一类型;沿用输入 Tensor 的设备
    ▼
空张量? → reshape 成 (0, points_dim)          ← 空场景是合法输入
    ▼
断言: dim()==2 且 size(-1)==points_dim        ← (N, D) 布局的硬约束
    ▼
self.tensor = tensor.clone()                  ← 存拷贝,与输入解耦
    ▼
记录 points_dim / attribute_dims / rotation_axis=0(子类再覆盖)
```

属性访问的设计:`coord` / `height` / `color` 都是 property。其中 `height` 与 `color` 的 setter 支持**动态生长**——若 `attribute_dims` 没登记该属性,setter 会用 `torch.cat` 把张量拓宽对应列数,并同步更新 `attribute_dims` 与 `points_dim`。

#### 4.1.3 源码精读

**构造函数**:[base_points.py:L33-L54](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/base_points.py#L33-L54) —— `__init__` 完整流程:类型与设备统一(L41)、空张量 reshape(L42-45,注释说明用 reshape 而非新建是为了不干扰 JIT)、形状断言(L46-49,报错信息同时给出期望维度与实际形状)、`clone` 存储(L51)、记录三个元数据(L52-54)。

**坐标属性 coord**:[base_points.py:L56-L75](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/base_points.py#L56-L75) —— getter 返回 `tensor[:, :3]` 的视图;setter 允许整体替换坐标列而**不动属性列**,reshape 失败抛出带实际形状的 `ValueError`。

**height 的「动态生长」**:[base_points.py:L77-L111](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/base_points.py#L77-L111) —— 已登记则直接读写对应列(L83 / L101-103);未登记时 setter 走 L104-111:取当前列数为新属性下标 → `torch.cat` 追加一列 → 更新 `attribute_dims` → `points_dim += 1`。

**color 同理**:[base_points.py:L113-L150](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/base_points.py#L113-L150) —— 一次追加 3 列并登记成列表 `[attr_dim, attr_dim+1, attr_dim+2]`(L147-149);setter 还检查取值超出 \([0,255]\) 时告警(L135-136)。注意 `color` 登记为**列表**、`height` 登记为**单个下标**,两种形态都合法。

**shape / bev 两个查询属性**:[base_points.py:L152-L155](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/base_points.py#L152-L155) 透传张量形状;[base_points.py:L256-L259](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/base_points.py#L256-L259) 基类 `bev` 默认取前两列(x-y 平面)——4.3 节会看到 `CameraPoints` 重写它取 x-z 平面,**同一个属性名,不同子类对应不同 BEV 平面**。

**官方单测即用法手册**:[test_base_points.py:L9-L14](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_structures/test_points/test_base_points.py#L9-L14) 演示空列表构造 `(0, 3)` 空点云;[test_base_points.py:L26-L46](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_structures/test_points/test_base_points.py#L26-L46) 构造 7 维点云并登记 `attribute_dims=dict(color=[3, 4, 5], height=6)`,随后逐属性断言切片正确性。

#### 4.1.4 代码实践

**实践目标**:亲手构造带属性的点云对象,验证张量布局与属性访问。

**操作步骤**(示例代码,非项目原有代码;可在装好 mmdet3d 的 Python 环境逐步执行):

```python
import numpy as np
from mmdet3d.structures.points import LiDARPoints

# 5 个点:xyz + 第 4 列(这里登记为 height 属性)
points_np = np.array([
    [1.0, 0.0, 0.0, 0.5],
    [0.0, 1.0, 0.0, 0.5],
    [0.0, 0.0, 1.0, 0.5],
    [1.0, 1.0, 1.0, 0.3],
    [-1.0, 2.0, 0.5, 0.2],
], dtype=np.float32)

pts = LiDARPoints(points_np, points_dim=4, attribute_dims=dict(height=3))
print(pts.shape)          # torch.Size([5, 4])
print(pts.tensor.dtype)   # torch.float32
print(pts.coord)          # (5, 3):只有 xyz
print(pts.height)         # (5,):第 3 列
print(pts.bev)            # (5, 2):x、y 两列
print(pts.rotation_axis)  # 2 —— LiDAR 子类设为 z 轴
```

**需要观察的现象**:`coord` 正是 `tensor[:, :3]`;`height` 取出第 4 列;`bev` 是前两列;`rotation_axis` 已被子类覆盖为 2。

**预期结果**:打印与注释一致。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**:传入形状 `(5, 4)` 的数组却声明 `points_dim=3`,会发生什么?

**答案**:构造函数中的断言(L46-49)失败,抛 `AssertionError`,信息同时给出期望的最后一维长度与实际形状——这是 \((N, \text{points\_dim})\) 布局约束的运行时防线。

**练习 2**:对一个 3 维、`attribute_dims=None` 的点云执行 `pts.height = 某向量`,对象的 `points_dim` 与 `attribute_dims` 如何变化?

**答案**:走 setter 的动态追加分支(L104-111):张量 `torch.cat` 拓宽一列,`points_dim` 从 3 变 4,`attribute_dims` 从 `None` 变 `{'height': 3}`。这让管线可以中途补挂属性(`LoadPointsFromFile` 的 `shift_height=True` 场景就是这么用的,见 4.4.3)。

### 4.2 刚体变换四件套:translate / rotate / flip / scale

#### 4.2.1 概念说明

数据增强(u3-l6 系统讲解)要对整帧点云做随机几何扰动,让模型见过更多视角与尺度。`BasePoints` 把四种变换都实现为**原地修改**,共同遵守一条铁律:

> **只改 `tensor[:, :3]` 坐标列,`tensor[:, 3:]` 属性列原样保留。**

四个方法的分工:

- `translate`:坐标列加平移向量,支持 3 维广播或 `(N, 3)` 逐点平移;
- `rotate`:两种输入——标量角度(绕 `rotation_axis` 即重力轴转)或直接给 3×3 矩阵;返回旋转矩阵供调用方同步变换 3D 框;
- `flip`:抽象方法,由子类实现——「水平翻转翻哪根轴」取决于坐标系;
- `scale`:坐标列整体乘系数(**不会**自动缩放 height 属性列,调用方自行处理,见 4.4.3)。

`rotate` 为什么要返回旋转矩阵?因为**增强必须让点云和 3D 框同步**:点转了 θ,框中心要转过去、yaw 还要加 θ。点侧返回的矩阵会被上游拿去处理框(以及 2D 图像标注),这正是 u2-l2 讲过的「点与框共用 `rotation_3d_in_axis`」的另一半。

#### 4.2.2 核心流程

`rotate(rotation, axis=None)` 的分派:

```text
输入 rotation
    ├─ numel == 1(标量角度)
    │       axis 为 None → 取 self.rotation_axis(重力轴,子类设定)
    │       rotation_3d_in_axis(tensor[:, :3], 角度, axis, return_mat=True)
    │       写回坐标列,返回 rot_mat_T
    └─ shape == (3, 3)(整矩阵)
            tensor[:, :3] = tensor[:, :3] @ rotation   ← 行向量右乘
            返回该矩阵本身
```

标量分支的数学:绕 z 轴(`axis=2`,LIDAR/DEPTH 的重力轴)转角 \(\theta\),令 \(c=\cos\theta,\ s=\sin\theta\),标准旋转矩阵为

\[
R_z(\theta)=\begin{bmatrix} c & -s & 0 \\ s & c & 0 \\ 0 & 0 & 1 \end{bmatrix}
\]

源码 `rotation_3d_in_axis` 实际 stack 出的是它的**转置**(变量名 `rot_mat_T` 的 `_T` 即此意),再用 `einsum('aij,jka->aik', points, rot_mat_T)` 做「行向量右乘」。行向量右乘 \(M\) 等价于列向量左乘 \(M^{\mathsf T}\),所以最终效果仍是 \(p' = R_z(\theta)\,p\):

\[
\begin{bmatrix} x' & y' & z' \end{bmatrix}
=
\begin{bmatrix} x & y & z \end{bmatrix}
\begin{bmatrix} c & s & 0 \\ -s & c & 0 \\ 0 & 0 & 1 \end{bmatrix}
\quad\Longleftrightarrow\quad
\begin{cases} x' = x\cos\theta - y\sin\theta \\ y' = x\sin\theta + y\cos\theta \\ z' = z \end{cases}
\]

当 \(\theta=\pi/2\) 时 \(c=0,\ s=1\),简化为 \((x,y,z)\mapsto(-y,\ x,\ z)\),非常适合手算验证(下面实践就用它)。

#### 4.2.3 源码精读

**rotate 双分支**:[base_points.py:L167-L197](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/base_points.py#L167-L197) —— 先断言输入是 3×3 矩阵或单元素(L181-182);`axis=None` 时回落到 `self.rotation_axis`(L184-185,子类设定的「默认绕重力轴」在此生效);标量分支(L187-191)只取 `tensor[:, :3]` 去转、属性列天然不动;矩阵分支(L193-195)直接右乘。

**底层旋转(与 3D 框共用)**:[utils.py:L74-L105](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/utils.py#L74-L105) —— 按 `axis` 查表 stack 出转置旋转矩阵:`axis==2`(z,L86-91)、`axis==1`(y,CAM 的重力轴,L80-85)、`axis==0`(x,L92-97),**矩阵形状随重力轴变化**;`clockwise=True` 时再转置一次(L107-108)。[utils.py:L110-L116](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/utils.py#L110-L116) —— `torch.einsum('aij,jka->aik', points, rot_mat_T)` 完成批量行向量右乘;`return_mat=True` 时把矩阵转回非转置形态返回(L118-119),即返回的是列向量约定下的 \(R\)。

**translate 与 scale**:[base_points.py:L209-L228](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/base_points.py#L209-L228) —— 平移只加到坐标列(L228);向量形状可为 3 或 `(N, 3)`,其余抛 `NotImplementedError`。[base_points.py:L302-L308](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/base_points.py#L302-L308) —— `scale` 只乘坐标列(L308)。

**flip 的三个子类实现**(抽象方法,每个子类只翻 BEV 两个分量之一):

- [lidar_points.py:L38-L49](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/lidar_points.py#L38-L49) —— LIDAR:horizontal 翻第 1 列(y,左向轴),vertical 翻第 0 列(x,前向轴);
- [cam_points.py:L38-L49](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/cam_points.py#L38-L49) —— CAM:horizontal 翻第 0 列(x,右向轴),vertical 翻第 2 列(z,前向轴);
- [depth_points.py:L38-L49](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/depth_points.py#L38-L49) —— DEPTH:horizontal 翻第 0 列(x,右向轴),vertical 翻第 1 列(y,前向轴)。

汇成一张规律表(建议记住,调试时极有用):

| 子类 | 重力轴(永不翻) | horizontal 翻 | vertical 翻 |
|---|---|---|---|
| LiDARPoints | z(列 2) | y(列 1,横向) | x(列 0,前向) |
| CameraPoints | y(列 1) | x(列 0,横向) | z(列 2,前向) |
| DepthPoints | z(列 2) | x(列 0,横向) | y(列 1,前向) |

**horizontal 永远翻「横向轴」,vertical 永远翻「前向轴」,重力轴永远不翻**——flip 的语义是相对 BEV 画面定义的,不是相对固定的第几列。

#### 4.2.4 代码实践

**实践目标**:验证四个变换方法的行为,重点是「属性列不参与变换」与 \(\theta=\pi/2\) 旋转公式。

**操作步骤**(示例代码,接 4.1.4 的构造):

```python
import numpy as np
from mmdet3d.structures.points import LiDARPoints

points_np = np.array([
    [1.0, 0.0, 0.0, 0.5],
    [0.0, 1.0, 0.0, 0.5],
    [0.0, 0.0, 1.0, 0.5],
    [1.0, 1.0, 1.0, 0.3],
    [-1.0, 2.0, 0.5, 0.2],
], dtype=np.float32)
pts = LiDARPoints(points_np, points_dim=4, attribute_dims=dict(height=3))

print('初始:\n', pts.tensor)

pts.translate(np.array([1.0, 2.0, 3.0], dtype=np.float32))   # 任务描述中的 "shift"
print('translate([1,2,3]) 后:\n', pts.tensor)

rot_mat = pts.rotate(np.pi / 2)                              # 绕 z 轴逆时针 90°
print('rotate(pi/2) 后:\n', pts.tensor)
print('返回的旋转矩阵:\n', rot_mat)

pts.flip(bev_direction='horizontal')                         # LiDAR: 翻 y 列
print('flip(horizontal) 后:\n', pts.tensor)

pts.scale(2.0)                                               # 只缩坐标列
print('scale(2.0) 后:\n', pts.tensor)
```

**需要观察的现象**:每步打印 5 个点的全部 4 列,盯住第 4 列(height)是否恒为 `[0.5, 0.5, 0.5, 0.3, 0.2]`;rotate 返回的矩阵是否为 \(\begin{bmatrix} 0 & -1 & 0 \\ 1 & 0 & 0 \\ 0 & 0 & 1 \end{bmatrix}\)。

**预期结果**(手算参考,float32 下可能有 1e-7 量级误差与 `-0.0`,待本地验证):

| 步骤 | 第 0 点 | 第 4 点 |
|---|---|---|
| 初始 | `[1, 0, 0, 0.5]` | `[-1, 2, 0.5, 0.2]` |
| translate([1,2,3]) | `[2, 2, 3, 0.5]` | `[0, 4, 3.5, 0.2]` |
| rotate(π/2),\((x,y)\mapsto(-y,x)\) | `[-2, 2, 3, 0.5]` | `[-4, 0, 3.5, 0.2]` |
| flip(horizontal),y 取负 | `[-2, -2, 3, 0.5]` | `[-4, 0, 3.5, 0.2]`(y=0 不变) |
| scale(2.0) 只缩坐标 | `[-4, -4, 6, 0.5]` | `[-8, 0, 7, 0.2]` |

第 4 列从头到尾不变——「属性列不参与刚体变换」的直接证据。

#### 4.2.5 小练习与答案

**练习 1**:同一句 `pts.rotate(np.pi / 4)`(不传 `axis`),分别作用在 `LiDARPoints` 与 `CameraPoints` 上,旋转轴有何不同?

**答案**:都回落到各自的 `self.rotation_axis`——`LiDARPoints` 是 2(绕 z 轴,在 x-y 平面内旋转),`CameraPoints` 是 1(绕 y 轴,在 x-z 平面内旋转)。两者都是绕重力轴的水平旋转,正是 BEV 检测「旋转增强只改朝向、不改俯仰」的需要。

**练习 2**:`GlobalRotScaleTrans` 缩放时为什么要手动把 height 属性列再乘一次系数(见 4.4.3 的引用),而 `flip` 之后不需要任何补偿?

**答案**:`scale` 改变场景物理尺度,「点离地高度」作为几何量必须同比例缩放,但基类 `scale` 只处理坐标列,所以要手动补;`flip` 是等距镜像,高度数值大小不变(属性列本就不参与 flip),无需补偿。

**练习 3**:手算点 \((3,1,2)\) 绕 z 轴转 \(\pi/2\) 后的坐标,并用代码验证。

**答案**:按 \((x,y)\mapsto(-y,x)\) 得 \((-1,3,2)\)。构造单点 `LiDARPoints(np.array([[3.,1.,2.]], dtype=np.float32))` 后 `rotate(np.pi/2)`,`tensor` 应为 `[[-1, 3, 2]]`。待本地验证。

### 4.3 三个坐标系子类与 convert_to 跨系转换

#### 4.3.1 概念说明

三个子类总共只做三件事,却撑起全部坐标系语义:

1. **设定 `rotation_axis`**:`LiDARPoints`、`DepthPoints` 设 2(z 轴),`CameraPoints` 设 1(y 轴)——重力轴不同,默认旋转平面不同;
2. **实现 `flip`**:按 4.2.3 的规律表翻转对应列;
3. **实现 `convert_to`**:它只是 `Coord3DMode.convert_point` 的**语法糖**——把自己的坐标系填进 `src` 后转发给统一转换引擎。`CameraPoints` 额外重写 `bev`(CAM 的 BEV 平面是 x-z 而非 x-y)。

`convert_to` 内部做三件事,与框侧 `Box3DMode.convert` 严格对应:克隆数据(不动原对象)→ 前 3 列乘转换矩阵(3×3 纯旋转或 3×4 含平移齐次形式)→ 用目标坐标系子类重新包装并保留 `points_dim`、`attribute_dims`。

**与框侧 `new_box` 的配套关系**:框的转换引擎内部用 `BaseInstance3DBoxes.new_box()` 以同配置创建新坐标系实例;点云侧的对应工厂是 `BasePoints.new_point()`。配套使用的正确姿势:**点云和 3D 框用同一个 `rt_mat` 分别转换,转换后二者仍然对齐**——多模态与可视化代码(lidar2img 投影等)都依赖这一性质。

#### 4.3.2 核心流程

`Coord3DMode.convert_point(point, src, dst, rt_mat)` 的流程:

```text
src == dst ? ──是──▶ 原样返回(同一引用,不克隆)
    │否
克隆输入(裸数组 / BasePoints.tensor 都要克隆,避免改到调用方数据)
    ▼
rt_mat 未提供?──是──▶ 查六组坐标系两两默认矩阵(纯轴互换/取负,不含真实标定)
    ▼
rt_mat 是 3×4?──是──▶ 坐标列拼齐次 1:xyz1 @ rt_matᵀ
    └──否(3×3)──▶ xyz @ rt_matᵀ
    ▼
属性列 arr[..., 3:] 原样拼回:cat([新xyz, remains])
    ▼
按 dst 选目标子类(CameraPoints / LiDARPoints / DepthPoints)重新包装
```

注意:六组默认矩阵只做**轴的重排与取负**,描述的是三套坐标系「方向约定」之间的差异;真实传感器间的转换(LiDAR 装在车顶、相机装在挡风玻璃,位置不同)必须由调用方传入实测 `rt_mat`(通常来自数据集 info pkl 中的标定矩阵,回顾 u2-l1)。

默认 LiDAR→CAM 矩阵可按方向约定手工推出:CAM 的 x 轴(右)= LiDAR 的 \(-y\),CAM 的 y 轴(下)= LiDAR 的 \(-z\),CAM 的 z 轴(前)= LiDAR 的 \(x\),即

\[
\begin{bmatrix} x_{cam} \\ y_{cam} \\ z_{cam} \end{bmatrix}
=
\begin{bmatrix} 0 & -1 & 0 \\ 0 & 0 & -1 \\ 1 & 0 & 0 \end{bmatrix}
\begin{bmatrix} x_{lidar} \\ y_{lidar} \\ z_{lidar} \end{bmatrix}
\]

与源码默认矩阵逐项一致。一句话记:**\((x,y,z)_{lidar}\mapsto(-y,\ -z,\ x)_{cam}\)**。

#### 4.3.3 源码精读

**子类之「薄」正说明基类抽象到位**:[lidar_points.py:L30-L36](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/lidar_points.py#L30-L36) —— LiDAR 子类构造只做一件事:调用父类后把 `rotation_axis` 设为 2。[lidar_points.py:L51-L72](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/lidar_points.py#L51-L72) —— `convert_to` 实现体:函数体内才 import `Coord3DMode`(L70,避免循环导入),然后 `Coord3DMode.convert_point(point=self, src=Coord3DMode.LIDAR, dst=dst, rt_mat=rt_mat)`。CAM/DEPTH 子类的 `convert_to` 结构完全相同,只是 `src` 常量不同:[cam_points.py:L56-L77](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/cam_points.py#L56-L77)、[depth_points.py:L51-L72](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/depth_points.py#L51-L72)。

**CameraPoints 对 bev 的重写**:[cam_points.py:L51-L54](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/cam_points.py#L51-L54) —— BEV 取 `[0, 2]`(x-z 平面):CAM 系里 y 是重力轴,俯视平面由「前向 z + 横向 x」构成。基类默认 `[:, [0, 1]]` 对 CAM 是错的,所以必须重写;基类的 `in_range_bev` 因为访问的是 `self.bev`([base_points.py:L274-L277](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/base_points.py#L274-L277)),子类重写后自动获得正确的 BEV 过滤行为——多态的小福利。

**转换引擎的核心四段**:

- [coord_3d_mode.py:L195-L196](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/coord_3d_mode.py#L195-L196) —— `src == dst` 直接返回原对象(同一引用,不克隆);
- [coord_3d_mode.py:L198-L213](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/coord_3d_mode.py#L198-L213) —— 入口类型识别与克隆:`BasePoints` 分支用 `point.tensor.clone()`(L211);
- [coord_3d_mode.py:L216-L237](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/coord_3d_mode.py#L216-L237) —— 六组默认 `rt_mat` 查表(L218 即 LiDAR→CAM 矩阵);不在表内的方向组合抛 `NotImplementedError`;
- [coord_3d_mode.py:L239-L249](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/coord_3d_mode.py#L239-L249) —— 乘法核心:`rt_mat.size(1) == 4` 时拼齐次 1(L241-244),否则 `arr[..., :3] @ rt_mat.t()`(L246);L248-249 把属性列拼回——「只转坐标、保留属性」的又一次体现;
- [coord_3d_mode.py:L251-L271](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/coord_3d_mode.py#L251-L271) —— 按 `dst` 选目标子类并带着 `attribute_dims` 重新包装,返回对象类型随目标坐标系变化。

**字符串到类的工厂**:[__init__.py:L10-L31](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/__init__.py#L10-L31) —— `get_points_type` 把配置里的 `coord_type='LIDAR'` 字符串(大小写不敏感)映射到 `LiDARPoints` 类,非法值抛 `ValueError`。它是「配置字符串 → 点云子类」的唯一入口。

**点侧工厂 new_point 与框侧 new_box 的对照**:[base_points.py:L501-L523](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/base_points.py#L501-L523) —— `new_point` 沿用自身 dtype/device/`points_dim`/`attribute_dims` 创建新对象;[base_box3d.py:L592-L612](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/base_box3d.py#L592-L612) —— 框侧 `new_box` 沿用 `box_dim`/`with_yaw` 做同一件事。u3-l7 的 `DataBaseSampler` 粘贴目标点云、u6 的点云检测器在投票/采样后重建点对象,都靠这对工厂方法。

#### 4.3.4 代码实践

**实践目标**:把 4.2.4 变换后的点转到相机坐标系,并与手工矩阵乘法逐元素对比,验证默认转换矩阵。

**操作步骤**(示例代码):

```python
import numpy as np
import torch
from mmdet3d.structures.points import LiDARPoints
from mmdet3d.structures.bbox_3d import Coord3DMode

# 与 4.2.4 结束时等价的状态:translate -> rotate(pi/2) -> flip 后的 5 个点
points_np = np.array([
    [1.0, 0.0, 0.0, 0.5],
    [0.0, 1.0, 0.0, 0.5],
    [0.0, 0.0, 1.0, 0.5],
    [1.0, 1.0, 1.0, 0.3],
    [-1.0, 2.0, 0.5, 0.2],
], dtype=np.float32)
pts = LiDARPoints(points_np, points_dim=4, attribute_dims=dict(height=3))
pts.translate(np.array([1., 2., 3.], dtype=np.float32))
pts.rotate(np.pi / 2)
pts.flip(bev_direction='horizontal')
print('LiDAR 系:\n', pts.tensor)

# 路线一:子类语法糖
cam_pts = pts.convert_to(Coord3DMode.CAM)
print(type(cam_pts).__name__)     # CameraPoints
print('CAM 系:\n', cam_pts.tensor)

# 路线二:手工矩阵(cam_x=-y, cam_y=-z, cam_z=x)
xyz = pts.tensor[:, :3].clone()
rt = torch.tensor([[0., -1., 0.],
                   [0.,  0., -1.],
                   [1.,  0.,  0.]])
manual = xyz @ rt.t()
print('手工矩阵:\n', manual)
print('两条路线一致:', torch.allclose(cam_pts.tensor[:, :3], manual, atol=1e-6))

print('height 属性保留:', cam_pts.height)      # 仍可访问
print('原对象未被修改的判据:类型仍是', type(pts).__name__)
```

**需要观察的现象**:两条路线坐标列逐元素一致;返回对象类型从 `LiDARPoints` 变为 `CameraPoints`;`height` 属性仍然可用;`cam_pts.rotation_axis` 变为 1。

**预期结果**(手算参考,\((x,y,z)\mapsto(-y,-z,x)\),待本地验证):

| 点 | LiDAR(flip 后) | CAM(转换后) |
|---|---|---|
| 0 | `[-2, -2, 3, 0.5]` | `[2, -3, -2, 0.5]` |
| 1 | `[-3, -1, 3, 0.5]` | `[1, -3, -3, 0.5]` |
| 2 | `[-2, -1, 4, 0.5]` | `[1, -4, -2, 0.5]` |
| 3 | `[-3, -2, 4, 0.3]` | `[2, -4, -3, 0.3]` |
| 4 | `[-4, 0, 3.5, 0.2]` | `[0, -3.5, -4, 0.2]` |

`allclose` 应为 `True`(float32 下建议用 `atol=1e-6` 容忍舍入;`0` 可能显示为 `-0.0`)。

#### 4.3.5 小练习与答案

**练习 1**:对 `LiDARPoints` 对象调用 `convert_to(Coord3DMode.LIDAR)` 会发生什么?

**答案**:`convert_point` 开头的 `src == dst` 判断([coord_3d_mode.py:L195-L196](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/coord_3d_mode.py#L195-L196))直接返回**原对象的引用**,不做克隆——与跨系转换「克隆后返回新对象」的行为不同,写代码时要注意别误以为拿到了独立副本。

**练习 2**:配置里 `LoadPointsFromFile(coord_type='DEPTH')` 加载 ScanNet 点云后做 `rotate(0.5)`,旋转发生在哪个平面?

**答案**:`get_points_type('DEPTH')` 得到 `DepthPoints`,其 `rotation_axis = 2`(z 轴),旋转发生在 x-y 平面——即 DEPTH 系的 BEV 平面(y 前、x 右、z 上),与 LiDAR 相同、与 CAM(绕 y 转、在 x-z 平面)不同。

**练习 3**:为什么 `convert_point` 要支持 3×4 的 `rt_mat`?

**答案**:真实传感器外参一般含平移(LiDAR 与相机安装位置不同)。3×4 矩阵即 \([R \mid t]\),把坐标拼上齐次 1 后一次矩阵乘法同时完成旋转与平移(\(p' = Rp + t\));若只支持 3×3,带平移的转换要拆两步,容易在「先转后移/先移后转」上出顺序错误。

### 4.4 管线视角:谁生产 points、谁消费 points

#### 4.4.1 概念说明

单独看容器方法容易迷失,把它们放回数据管线(u3 单元主战场)就能看清职责:

- **生产者**:`LoadPointsFromFile` 从 `.bin/.npy` 读原始字节 → `reshape(-1, load_dim)` → 按 `use_dim` 选列 → 可选地构造 `attribute_dims` → `get_points_type(coord_type)` 包装成子类对象放进 `results['points']`。**此后整条管线里流转的是这个对象,而不是裸数组**;
- **消费者**:`RandomFlip3D` 调 `flip`,`GlobalRotScaleTrans` 调 `translate`/`rotate`/`scale`,`PointsRangeFilter` 调 `in_range_3d` + 掩码索引,GT 采样(u3-l7)调 `cat` 拼接。

此外还有一组「容器基础设施」:`__getitem__` 切片、`cat` 拼接、`new_point` 工厂、`clone`/`to`/`cuda`/`detach` 设备与梯度管理——它们让点云对象用起来像张量一样顺手。

#### 4.4.2 核心流程

`__getitem__` 的分派(文档注释列了 5 种合法用法):

```text
points[3]             单点      → 重新包装成 (1, points_dim) 的新对象
points[2:10]          行切片    → 新对象(属性维度全保留)
points[bool 掩码]     掩码选点  → 新对象(in_range_3d 的结果喂进来)
points[3:11, 列选择]  行+列切片 → 新对象,并修正 attribute_dims 下标
```

`cat` 类方法:`torch.cat` 多个对象的 tensor(dim=0)后用**第一个对象**的元数据重新包装;注释明确说明不用会共享存储的 layers.cat,保证返回对象与输入解耦。

#### 4.4.3 源码精读

**生产者 LoadPointsFromFile**:[loading.py:L645-L648](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L645-L648) —— 从 `lidar_path` 读字节流、`reshape(-1, load_dim)`、按 `use_dim` 选列(`load_dim`/`use_dim` 语义 u3-l5 详解)。[loading.py:L659-L665](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L659-L665) —— `shift_height=True` 时计算离地高度插到第 3 列并登记 `attribute_dims = dict(height=3)`——正是 4.1 练习 2「动态补挂属性」的真实调用方。[loading.py:L678-L681](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L678-L681) —— `get_points_type(self.coord_type)` 拿子类,把 numpy 数组包装成点对象放进 `results['points']`。

**消费者一:RandomFlip3D**:[transforms_3d.py:L139-L169](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L139-L169) —— `random_flip_data_3d` 兼顾 LiDAR 检测/分割、纯视觉、多模态四种情形:有框有点时走 `gt_bboxes_3d.flip(direction, points=...)` 一站式同步翻转(L163-164,内部用同一个 `rotation_3d_in_axis` 家族工具);只有点时直接 `points.flip(direction)`(L169)。

**消费者二:GlobalRotScaleTrans**:[transforms_3d.py:L707-L710](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L707-L710) —— 平移分支:`points.translate(trans_factor)` 后用**同一个**向量平移 `gt_bboxes_3d`。[transforms_3d.py:L725-L735](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L725-L735) —— 旋转分支:有框时 `gt_bboxes_3d.rotate(角度, points)` 一站式同步;无框(纯分割)时只调 `points.rotate(noise_rotation)`(L733)——**rotate 返回矩阵的设计在这里兑现**。[transforms_3d.py:L748-L759](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L748-L759) —— 缩放分支:`points.scale(scale)` 之后 L751-754 **手动**把 height 属性列也乘上系数——印证「scale 不自动处理属性列」的设计。

**消费者三:PointsRangeFilter**:[transforms_3d.py:L935-L938](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L935-L938) —— `points_mask = points.in_range_3d(self.pcd_range)` 生成 (N,) bool 掩码,`points[points_mask]` 掩码索引得到感知范围内的点——`in_range_3d` 与 `__getitem__` 的标准连招;随后的 `pts_instance_mask`/`pts_semantic_mask` 也用同一掩码同步过滤(分割任务用)。

**容器基础设施**:[base_points.py:L310-L387](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/base_points.py#L310-L387) —— `__getitem__`:L360-375 在「行+列」切片时重新计算 `attribute_dims`——选出的列变了,`height` 原来在第 3 列、切片后可能落到别的列,登记必须同步修正甚至移除,否则属性访问就错位了。[base_points.py:L230-L254](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/base_points.py#L230-L254) —— `in_range_3d` 六个严格比较决定点是否落在 `(x_min, y_min, z_min, x_max, y_max, z_max)` 盒内。[base_points.py:L397-L418](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/base_points.py#L397-L418) —— `cat`:拼接前断言列表非空且所有对象同类型。[base_points.py:L424-L499](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/base_points.py#L424-L499) —— `numpy`/`to`/`cpu`/`cuda`/`clone`/`detach`/`device`/`__iter__` 一组搬运方法,全部遵循同一模式:操作 tensor 后用 `type(self)` 重新包装,**保证子类调用返回子类**。

#### 4.4.4 代码实践

**实践目标**:体验「掩码过滤 + 切片 + 拼接」组合拳——这正是 `PointsRangeFilter` 与 GT 采样在工作时做的事。

**操作步骤**(示例代码):

```python
import numpy as np
from mmdet3d.structures.points import LiDARPoints

rng = np.random.RandomState(0)
points_np = rng.rand(1000, 4).astype(np.float32) * 80 - 40   # 约 [-40, 40)
pts = LiDARPoints(points_np, points_dim=4, attribute_dims=dict(height=3))

mask = pts.in_range_3d([-30, -30, -30, 30, 30, 30])
print('盒内点数:', int(mask.sum()), '/ 1000')

inside = pts[mask]                       # 掩码索引 → 新 LiDARPoints
print(type(inside).__name__, inside.shape, 'height 仍可访问:', inside.height is not None)

merged = LiDARPoints.cat([inside, pts.clone()])
print('cat 后点数:', len(merged))         # 盒内 + 全部
```

**需要观察的现象**:掩码索引返回的仍是 `LiDARPoints` 且属性访问不失效;`cat` 后点数是两者之和。

**预期结果**:均匀撒点下,边长 60 的盒子体积占比 \((60/80)^3 = 42.2\%\),盒内约 420 个点上下(随机种子固定,数值确定);`inside.height` 非 `None`;`len(merged) = mask.sum() + 1000`。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**:`pts[2:10, [0, 1, 2]]` 返回对象的 `attribute_dims` 是什么?

**答案**:选出的三列全是坐标、不含原第 3 列(height),走 L373-375 的 `else: attribute_dims.pop(key)` 分支,`height` 键被移除。**属性语义跟着列走**——这是 `__getitem__` 比直接切 tensor 多出来的正确性保障。

**练习 2**:`PointsRangeFilter` 为什么必须放在增强管线靠后位置?

**答案**:旋转/翻转/平移/缩放都会改变点坐标,过滤太早的话后续增强又会把点移出或移入感知范围,与模型 `point_cloud_range` 的假设不一致;先做完所有几何增强、最后裁剪,才能保证进入模型的数据与体素化范围对齐。

**练习 3**:把本讲 5 个手工点喂给 `ObjectSample`(GT 采样)可行吗?它的输入要求是什么?

**答案**:不可行。`ObjectSample` 要求 results 中有 `ann_info`、`gt_bboxes_3d`、`gt_labels_3d` 等标注字段(见 [transforms_3d.py:L328-L340](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L328-L340) 的 Required Keys),单靠点云对象无法运行——这正是 u3-l7 要讲的完整数据流。

## 5. 综合实践

把本讲全部内容串成**一个可运行的验证脚本**:构造点云 → translate/rotate/flip 三步变换逐步打印 → convert_to 转相机系并与手工矩阵对比 → 掩码过滤。全程只用本讲讲过的 API。

**实践目标**:一个脚本同时验证「布局、变换、转换、过滤」四件事,输出一张逐步演化的坐标表。

**操作步骤**(示例代码,保存为 `verify_points.py` 或在交互环境逐步执行):

```python
import numpy as np
import torch
from mmdet3d.structures.points import LiDARPoints
from mmdet3d.structures.bbox_3d import Coord3DMode

# 1) 构造:xyz + height
points_np = np.array([
    [1.0, 0.0, 0.0, 0.5],
    [0.0, 1.0, 0.0, 0.5],
    [0.0, 0.0, 1.0, 0.5],
    [1.0, 1.0, 1.0, 0.3],
    [-1.0, 2.0, 0.5, 0.2],
], dtype=np.float32)
pts = LiDARPoints(points_np, points_dim=4, attribute_dims=dict(height=3))
print('初始\n', pts.tensor)

# 2) 平移 -> 旋转(pi/2) -> 水平翻转,每步打印全部 5 个点
pts.translate(np.array([1., 2., 3.], dtype=np.float32)); print('translate\n', pts.tensor)
rot_mat = pts.rotate(np.pi / 2);                        print('rotate\n', pts.tensor, '\nR=\n', rot_mat)
pts.flip(bev_direction='horizontal');                   print('flip\n', pts.tensor)

# 3) 转相机系,并与手工矩阵对比
cam_pts = pts.convert_to(Coord3DMode.CAM)
manual = pts.tensor[:, :3] @ torch.tensor(
    [[0., -1., 0.], [0., 0., -1.], [1., 0., 0.]]).t()
print('CameraPoints\n', cam_pts.tensor)
print('与手工矩阵一致:', torch.allclose(cam_pts.tensor[:, :3], manual, atol=1e-6))
print('类型:', type(cam_pts).__name__, ' rotation_axis:', cam_pts.rotation_axis)

# 4) 掩码过滤(演示 in_range_3d 用法)
mask = cam_pts.in_range_3d([-100, -100, -100, 100, 100, 100])
print('过滤后剩余点数:', int(mask.sum()))
```

**需要观察的现象**:每步打印中第 4 列(height)恒为 `[0.5, 0.5, 0.5, 0.3, 0.2]`;rotate 后坐标满足 \((x,y)\mapsto(-y,x)\);flip 后 y 列取负;convert 后类型变为 `CameraPoints` 且 `rotation_axis` 变 1;`allclose` 为 `True`。

**预期结果**(手算参考,待本地验证):

| 步骤 | 第 0 点 | 第 4 点 |
|---|---|---|
| 初始 | `[1, 0, 0, 0.5]` | `[-1, 2, 0.5, 0.2]` |
| translate([1,2,3]) | `[2, 2, 3, 0.5]` | `[0, 4, 3.5, 0.2]` |
| rotate(π/2) | `[-2, 2, 3, 0.5]` | `[-4, 0, 3.5, 0.2]` |
| flip(horizontal) | `[-2, -2, 3, 0.5]` | `[-4, 0, 3.5, 0.2]` |
| convert_to(CAM) | `[2, -3, -2, 0.5]` | `[0, -3.5, -4, 0.2]` |

rotate 返回的 `R` 应为 \(\begin{bmatrix} 0 & -1 & 0 \\ 1 & 0 & 0 \\ 0 & 0 & 1 \end{bmatrix}\);宽松范围过滤保留全部 5 点。

**扩展(可选)**:把第 1 步换成真实 KITTI 点云,其余不变:

```python
points_np = np.fromfile('demo/data/kitti/000008.bin',
                        dtype=np.float32).reshape(-1, 4)
pts = LiDARPoints(points_np, points_dim=4)   # 第 4 列是强度,此处不登记语义
```

观察约 1.2 万个真实点在三步变换下的坐标统计(如 `pts.coord.mean(0)`)变化,并与 4.4.4 的 `in_range_3d` 配合,按 KITTI 常用范围 `[0, -40, -3, 70.4, 40, 1]` 过滤。

## 6. 本讲小结

- 点云对象 = **一个 \((N, \text{points\_dim})\) 的 float32 张量 + 三个元数据**(`points_dim`、`attribute_dims`、`rotation_axis`);前 3 列固定是坐标,属性列语义靠 `attribute_dims` 登记,height/color 的 setter 还能动态拓宽张量补挂属性。
- 四个刚体变换(`translate`/`rotate`/`flip`/`scale`)全是**原地修改且只动坐标列**;`rotate` 返回旋转矩阵是为了调用方用同一矩阵同步变换 3D 框;底层 `rotation_3d_in_axis` 以行向量右乘转置矩阵实现 \(p' = R_z(\theta)p\)。
- 三个子类极薄,只决定三件事:重力轴(`rotation_axis`:LiDAR/Depth 为 2、CAM 为 1)、`flip` 方向(horizontal 翻横向轴、vertical 翻前向轴、重力轴永不翻)、`convert_to` 的 `src` 常量;`CameraPoints` 额外重写 `bev` 为 x-z 平面。
- `convert_to` 是 `Coord3DMode.convert_point` 的语法糖:克隆 → 前 3 列乘 `rt_mat`(3×3 或 3×4 齐次)→ 属性列拼回 → 目标子类重新包装;默认矩阵只描述方向约定,真实跨传感器转换必须传标定矩阵;点侧 `new_point` 与框侧 `new_box` 是配套工厂。
- 管线视角:`LoadPointsFromFile` 经 `get_points_type` 生产点对象放进 `results['points']`;`RandomFlip3D`/`GlobalRotScaleTrans` 消费 `flip`/`translate`/`rotate`/`scale`,`PointsRangeFilter` 消费 `in_range_3d` + 掩码索引,`cat` 拼接是 GT 采样(u3-l7)的基础设施。

## 7. 下一步学习建议

- **下一讲 u2-l4(Det3DDataSample)**:点云对象与 3D 框最终都会被打包进 `Det3DDataSample`,成为贯穿「数据管线 → 模型 → 评估」的统一数据契约——把本讲的「容器」放进更大的「信封」里理解。
- **u3-l5(数据加载 Transforms)**:精读 `LoadPointsFromFile` 的 `load_dim`/`use_dim` 语义与 `LoadPointsFromMultiSweeps`,看 nuScenes 多帧点云如何用 `cat` 拼成聚合点云。
- **u3-l6(3D 数据增强)**:本讲只看了 `GlobalRotScaleTrans` 的三个私有方法片段,下一层要完整跟踪「点、框、2D 标注」三者同步变换的全貌。
- **动手方向**:把第 5 节综合实践换成 `demo/data/kitti/000008.bin` 真实点云,配合 `tools/misc/browse_dataset.py` 可视化增强前后的点云,直观感受几何扰动效果。

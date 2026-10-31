# 点云结构:BasePoints 与三个坐标系子类

## 1. 本讲目标

上一讲(u2-l1)我们搞清了三大坐标系的约定,以及 `Box3DMode` / `Coord3DMode` 这两个转换引擎。本讲把镜头对准点云本身:LiDAR/深度相机吐出来的几十万个三维采样点,在 MMDetection3D 里是如何被表示、变换和转换的。

学完本讲,你应该能够:

1. 说出 `BasePoints` 的张量布局 \((N, \text{points\_dim})\):前 3 列固定是坐标,其余列通过 `attribute_dims` 登记语义(强度、高度、颜色等)。
2. 独立使用 `LiDARPoints` 的 `translate` / `rotate` / `flip` / `scale` 四个刚体变换方法,并解释「为什么只动前 3 列、属性列不动」。
3. 解释 `LiDARPoints` / `CameraPoints` / `DepthPoints` 三个子类各自的 `rotation_axis`、`flip` 方向差异,以及 `convert_to` 与 `Coord3DMode.convert_point`、框侧 `new_box` 的配套关系。
4. 在数据管线里指出「谁生产了 points 对象、谁在消费它」。

> 说明:如果你在旧版资料里见过「points_catdim」之类的说法,请注意当前 1.x 源码中点云对象的维度属性名是 `points_dim`,本讲以源码为准。另外,数据增强里的「平移」操作,点云侧的真实方法名是 `translate`(基类中没有 `shift` 方法),下文不再特别区分这两个词。

## 2. 前置知识

### 2.1 点云在内存里就是一个二维数组

一帧点云就是 N 个采样点,每个点至少有 \((x, y, z)\) 三个坐标,常常还附带:

- **强度(intensity)**:激光打到物体表面反射回来的信号强度,KITTI 点云文件每点 4 个 float32,第 4 个就是强度;
- **高度(height)**:点到地面的相对高度,室内算法(VoteNet 等)常用;
- **颜色(color)**:RGB 三列,来自 RGB-D 相机或带彩色传感器的 LiDAR。

所以点云的原始形态就是一个 `(N, D)` 的数组:每行一个点,前 3 列坐标,后面挂属性。**D 可以是 3、4、5、6、7……这正是 `points_dim` 要记录的东西。**

### 2.2 刚体变换是什么

刚体变换 = 不改变物体形状的几何变换,包括:

| 变换 | 数学形式(列向量约定) | 点云增强中的例子 |
|---|---|---|
| 平移 | \(p' = p + t\) | `RandomShift3D` 随机抖动 |
| 旋转 | \(p' = R\,p\) | `GlobalRotScaleTrans` 全局旋转 |
| 缩放 | \(p' = s \cdot p\) | `GlobalRotScaleTrans` 全局缩放 |
| 翻转(镜像) | 某一坐标分量取负 | `RandomFlip3D` 水平/垂直翻转 |

关键点:**旋转和翻转只作用于坐标列;强度、颜色这类属性没有几何意义,不参与变换**。这是本讲反复出现的设计原则。

### 2.3 承接上一讲的坐标系结论

u2-l1 讲过三套右手坐标系:LiDAR(x 前、y 左、z 上)、CAM(z 前、x 右、y 下)、DEPTH(y 前、x 右、z 上),重力轴分别是 z、y、z。`BasePoints` 的三个子类与之一一对应,子类的全部差异都源于「重力轴是哪根、BEV 平面是哪两根」这两个问题。忘了的话建议先回看 u2-l1 的方向约定图。

### 2.4 torch.Tensor 的索引与视图

`BasePoints` 内部存的是一个 `torch.float32` 张量,大量使用切片赋值(如 `self.tensor[:, :3] = ...`)和 `clone()`。你需要知道:切片赋值是**原地修改**,`clone()` 是**深拷贝**——这决定了哪些方法会改变点云对象本身(`rotate`、`flip` 都是原地操作),哪些方法返回新对象(`clone`、`to`、`convert_to`)。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注度 |
|---|---|---|
| `mmdet3d/structures/points/base_points.py` | `BasePoints` 基类:张量容器 + 通用变换 + 索引/拼接/搬运 | ⭐⭐⭐ 核心 |
| `mmdet3d/structures/points/lidar_points.py` | LiDAR 坐标系子类 | ⭐⭐⭐ 核心 |
| `mmdet3d/structures/points/cam_points.py` | CAM 坐标系子类(重写了 `bev`) | ⭐⭐⭐ 核心 |
| `mmdet3d/structures/points/depth_points.py` | DEPTH 坐标系子类 | ⭐⭐ |
| `mmdet3d/structures/points/__init__.py` | 导出四个类 + `get_points_type` 工厂函数 | ⭐⭐ |
| `mmdet3d/structures/bbox_3d/utils.py` | `rotation_3d_in_axis`:旋转的底层实现 | ⭐⭐ |
| `mmdet3d/structures/bbox_3d/coord_3d_mode.py` | `Coord3DMode.convert_point`:跨坐标系转换引擎 | ⭐⭐⭐ |
| `mmdet3d/datasets/transforms/loading.py` | 管线中点云对象的**生产者**(`LoadPointsFromFile`) | ⭐⭐ |
| `mmdet3d/datasets/transforms/transforms_3d.py` | 管线中变换方法的**消费者** | ⭐⭐ |
| `tests/test_structures/test_points/test_base_points.py` | 官方单测,可当用法示例读 | ⭐ |

点云家族与上一讲的框家族(`BaseInstance3DBoxes` + 三子类)完全平行:**基类管通用逻辑,子类只补坐标系相关差异**。两个家族通过同一个 `rotation_3d_in_axis` 工具函数实现「点与框同步旋转」,这是数据增强正确性的根基。

## 4. 核心概念与源码讲解

### 4.1 BasePoints:点云张量的统一容器

#### 4.1.1 概念说明

为什么不让所有代码直接操作裸的 `torch.Tensor`?因为裸张量**只有形状、没有语义**:

- 代码无法知道第 4 列是强度还是高度,增强时就不知道哪些列该跟着旋转/缩放;
- 代码无法知道这批点在哪个坐标系,`flip`、`bev` 就无法给出正确行为;
- 点数可能为 0(空场景)、设备可能在 CPU/GPU 之间搬运,需要统一的封装处理这些边界情况。

`BasePoints` 的解决方案是「**张量 + 元数据**」:

```text
BasePoints 对象
├── tensor          (N, points_dim) 的 float32 张量,前 3 列固定是坐标
├── points_dim      int,每个点的总维度
├── attribute_dims  dict 或 None,登记属性列的语义,如 {'height': 3} 或 {'color': [4,5,6], 'height': 7}
└── rotation_axis   int,默认旋转轴(重力轴的列下标),子类中覆盖
```

一个值得注意的细节:`flip` 和 `convert_to` 虽然标了 `@abstractmethod`,但 `BasePoints` 并没有继承 `ABCMeta`,所以它**仍可直接实例化**——官方单测里就有 `BasePoints(empty_boxes)` 的用法。这个 `@abstractmethod` 更多是「子类必须实现」的文档性约定。

#### 4.1.2 核心流程

构造一个点云对象的流程:

```text
输入(Tensor / ndarray / 嵌套列表)
    │ torch.as_tensor(..., dtype=float32)      ← 统一类型与设备
    ▼
若为空张量: reshape 成 (0, points_dim)          ← 空场景是合法输入
    ▼
断言: dim()==2 且最后一维 == points_dim          ← (N, D) 布局的硬约束
    ▼
self.tensor = tensor.clone()                    ← 存一份拷贝,与输入解耦
    ▼
rotation_axis = 0                               ← 基类默认 x 轴,子类会覆盖
```

#### 4.1.3 源码精读

**构造函数与布局断言**:把任意数组输入统一转成 float32 张量,并强制 `(N, points_dim)` 布局;空点云用 `reshape` 而不是新建张量,注释说明是为了不干扰 JIT 追踪。

[base_points.py:L33-L54](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/base_points.py#L33-L54) —— `__init__` 完整流程:类型统一(L41)、空张量处理(L42-45)、形状断言(L46-49)、`clone` 存储(L51)、记录三个元数据(L52-54)。

**坐标属性 `coord`**:只读视图返回前 3 列;对应的 setter 允许整体替换坐标列而不动属性列。

[base_points.py:L56-L75](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/base_points.py#L56-L75) —— `coord` property 与 setter,setter 里 `reshape` 失败会抛出清晰的 `ValueError`。

**`height` 与 `color`:带「动态生长」的属性访问**。以 `height` 为例:若 `attribute_dims` 里登记过,直接读写对应列;若没登记,**setter 会把张量拓宽一列**(`torch.cat` 追加),并更新 `attribute_dims` 和 `points_dim`。

[base_points.py:L77-L111](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/base_points.py#L77-L111) —— `height` 的 getter/setter;L108-111 就是「追加一列并登记语义」的逻辑。

[base_points.py:L113-L150](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/base_points.py#L113-L150) —— `color` 同理,一次追加 3 列,登记成列表 `[attr_dim, attr_dim+1, attr_dim+2]`(L147-149);setter 还会检查取值范围并告警(L135-136)。

**`shape` 与 `bev`**:两个最常被上层代码查询的属性。

[base_points.py:L152-L155](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/base_points.py#L152-L155) —— `shape` 直接透传张量形状。

[base_points.py:L256-L259](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/base_points.py#L256-L259) —— 基类 `bev` 默认取前两列(x-y 平面);下一节会看到 `CameraPoints` 重写了它取 x-z 平面——**同一个属性名,不同子类对应不同的 BEV 平面**。

官方单测展示了带属性点云的标准构造方式,值得一读:

[test_base_points.py:L26-L71](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_structures/test_points/test_base_points.py#L26-L71) —— 构造 7 维点云:`attribute_dims=dict(color=[3, 4, 5], height=6)`,然后断言 `beev/coord/color/height` 各属性切片的正确性。注意 `color` 登记为列表、`height` 登记为单个下标,两种形态都合法。

#### 4.1.4 代码实践

**实践目标**:亲手构造一个带属性的点云对象,验证张量布局与属性访问。

**操作步骤**(示例代码,非项目原有代码,可在装好环境的 Python 交互环境中运行):

```python
import numpy as np
import torch
from mmdet3d.structures.points import LiDARPoints

# 5 个点:xyz + 第 4 列当强度
points_np = np.array([
    [1.0, 0.0, 0.0, 0.5],
    [0.0, 1.0, 0.0, 0.5],
    [0.0, 0.0, 1.0, 0.5],
    [1.0, 1.0, 1.0, 0.3],
    [-1.0, 2.0, 0.5, 0.2],
], dtype=np.float32)

pts = LiDARPoints(points_np, points_dim=4, attribute_dims=dict(height=3))
print(pts.shape)            # torch.Size([5, 4])
print(pts.tensor.dtype)     # torch.float32
print(pts.coord)            # (5, 3),只有 xyz
print(pts.height)           # (5,),第 3 列
print(pts.bev)              # (5, 2),前两列
print(pts.rotation_axis)    # 2(LiDAR 子类设的,z 轴)
```

**需要观察的现象**:`coord` 返回的正是 `tensor[:, :3]`;`pts.height` 能取出第 4 列;`bev` 是 x-y 两列。

**预期结果**:打印如注释所示。第 4.2 节的变换实践会继续用这个对象。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**:如果传入 `points_dim=4` 的数组却声明 `points_dim=3`,会发生什么?

**答案**:`__init__` 中的断言(`tensor.size(-1) == points_dim`)失败,抛出 `AssertionError`,报错信息会同时给出期望维度和实际形状。这就是 `(N, points_dim)` 布局约束的运行时防线。

**练习 2**:对一个 3 维、没有 `attribute_dims` 的点云对象执行 `pts.height = some_values`,对象的 `points_dim` 会变成多少?`attribute_dims` 会怎么变?

**答案**:走 setter 的「动态追加」分支(L104-111):张量用 `torch.cat` 拓宽一列,`points_dim` 从 3 变 4,`attribute_dims` 从 `None` 变成 `{'height': 3}`。这个设计允许管线中途补挂属性(比如 `LoadPointsFromFile` 的 `shift_height=True` 场景)。

### 4.2 刚体变换四件套:translate / rotate / flip / scale

#### 4.2.1 概念说明

数据增强(u3-l6 将系统讲解)需要对整帧点云做随机几何扰动,让模型见过更多样的视角与尺度。`BasePoints` 把四种变换都实现为**原地修改**(`in-place`)方法,共同遵守一条铁律:

> **只变换 `tensor[:, :3]` 坐标列,`tensor[:, 3:]` 属性列原样保留。**

对照基类源码可以逐一验证:

- `translate`:坐标列加平移向量,支持 3 维广播或 (N, 3) 逐点平移;
- `scale`:坐标列整体乘缩放系数(注意 `height` 属性列**不会**被自动缩放,需要调用方自己处理,见下面的管线代码);
- `rotate`:两种输入形态——标量角度(绕 `rotation_axis` 即重力轴转)或直接给 3×3 矩阵;
- `flip`:抽象方法,由子类实现,因为「水平翻转翻哪根轴」取决于坐标系。

`rotate` 为什么返回旋转矩阵?因为**数据增强必须让点云和 3D 框同步变换**:点转了 θ 角,框除了中心转过去,yaw 也要加 θ。点侧返回的 `rot_mat_T` 会被上游拿去同步处理框(以及 2D 图像标注),这正是 u2-l2 讲过的「点与框用同一个 `rotation_3d_in_axis`」的另一半。

#### 4.2.2 核心流程

`rotate(rotation, axis=None)` 的执行过程:

```text
输入 rotation
    ├─ numel == 1(标量角度)
    │       axis 为 None 时取 self.rotation_axis(重力轴)
    │       调 rotation_3d_in_axis(tensor[:, :3], 角度, axis, return_mat=True)
    │       写回坐标列,返回 rot_mat_T
    └─ shape == (3, 3)(整矩阵)
            tensor[:, :3] = tensor[:, :3] @ rotation   ← 行向量右乘
            返回该矩阵本身
```

标量分支的数学:绕 z 轴(`axis=2`,LiDAR/DEPTH 的重力轴)旋转角 \(\theta\),令 \(c=\cos\theta,\ s=\sin\theta\),标准旋转矩阵为

\[
R_z(\theta)=\begin{bmatrix} c & -s & 0 \\ s & c & 0 \\ 0 & 0 & 1 \end{bmatrix}
\]

源码里 `rotation_3d_in_axis` 实际 stack 出来的是它的**转置** \(R_z(\theta)^{\mathsf T}\),再用 `einsum('aij,jka->aik', points, rot_mat_T)` 做「行向量右乘」。行向量右乘 \(M\) 等价于列向量左乘 \(M^{\mathsf T}\),所以最终效果仍是 \(p' = R_z(\theta)\,p\):

\[
\begin{bmatrix} x' & y' & z' \end{bmatrix}
=
\begin{bmatrix} x & y & z \end{bmatrix}
\begin{bmatrix} c & s & 0 \\ -s & c & 0 \\ 0 & 0 & 1 \end{bmatrix}
\quad\Longleftrightarrow\quad
\begin{cases} x' = x\cos\theta - y\sin\theta \\ y' = x\sin\theta + y\cos\theta \\ z' = z \end{cases}
\]

变量名 `rot_mat_T` 里的 `_T` 就是提醒你「存的是转置形态」。当 \(\theta=\pi/2\) 时简化为 \((x,y,z)\mapsto(-y,\,x,\,z)\),非常适合用来手算验证。

#### 4.2.3 源码精读

**`rotate` 双分支**:

[base_points.py:L167-L197](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/base_points.py#L167-L197) —— 标量分支(L187-191)只取 `self.tensor[:, :3]` 去转,属性列天然不动;矩阵分支(L193-195)直接右乘。注意 L184-185:`axis=None` 时回落到 `self.rotation_axis`,这就是子类设定的「默认绕重力轴」生效处。

**底层旋转实现 `rotation_3d_in_axis`**(与 3D 框共用,上一讲已见过它的调用方):

[utils.py:L86-L91](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/utils.py#L86-L91) —— `axis==2` 时 stack 出转置旋转矩阵;`axis==1`(CAM 的 y 轴)对应 [utils.py:L80-L85](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/utils.py#L80-L85),矩阵形状随重力轴变化。

[utils.py:L110-L113](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/utils.py#L110-L113) —— `torch.einsum('aij,jka->aik', points, rot_mat_T)` 完成批量「行向量右乘」。

**`translate` 与 `scale`**:

[base_points.py:L209-L228](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/base_points.py#L209-L228) —— 平移只加到坐标列(L228);支持形状为 3 或 (N, 3) 的向量,其余形状抛 `NotImplementedError`。

[base_points.py:L302-L308](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/base_points.py#L302-L308) —— `scale` 只乘坐标列(L308)。

**`flip` 的三个子类实现**(flip 是抽象方法,每个子类只翻两个 BEV 分量之一):

[lidar_points.py:L38-L49](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/lidar_points.py#L38-L49) —— LiDAR:horizontal 翻第 1 列(y,左向轴),vertical 翻第 0 列(x,前向轴)。

[cam_points.py:L38-L49](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/cam_points.py#L38-L49) —— CAM:horizontal 翻第 0 列(x,右向轴),vertical 翻第 2 列(z,前向轴)。

[depth_points.py:L38-L49](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/depth_points.py#L38-L49) —— DEPTH:horizontal 翻第 0 列(x,右向轴),vertical 翻第 1 列(y,前向轴)。

三个实现合起来是一条整齐的规律(建议记住,面试和调试都用得上):

| 子类 | 重力轴(不翻) | horizontal 翻 | vertical 翻 |
|---|---|---|---|
| LiDARPoints | z(列 2) | y(列 1,横向) | x(列 0,前向) |
| CameraPoints | y(列 1) | x(列 0,横向) | z(列 2,前向) |
| DepthPoints | z(列 2) | x(列 0,横向) | y(列 1,前向) |

**horizontal 永远翻「横向轴」,vertical 永远翻「前向轴」,重力轴永远不翻**——`flip` 的语义是相对 BEV 画面定义的,而不是相对固定的第几列。

**管线里的真实消费方**(`GlobalRotScaleTrans`,u3-l6 的主角,这里先认个脸):

[transforms_3d.py:L707-L710](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L707-L710) —— 平移分支:`points.translate(trans_factor)` 后,用**同一个**向量平移 `gt_bboxes_3d`,保证点与框一致。

[transforms_3d.py:L725-L735](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L725-L735) —— 旋转分支:有框时走 `gt_bboxes_3d.rotate(角度, points)` 一站式同步旋转;无框(纯分割任务)时只调 `points.rotate(noise_rotation)`(L733)。

[transforms_3d.py:L748-L754](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L748-L754) —— 缩放分支:`points.scale(scale)` 之后,L751-754 **手动**把 `height` 属性列也乘上缩放系数——这正好印证了「`scale` 不会自动处理属性列」的设计,高度属性的几何含义需要调用方自己维护。

[transforms_3d.py:L159-L169](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L159-L169) —— `RandomFlip3D` 的翻转分支:有框有点时通过 `gt_bboxes_3d.flip(direction, points=...)` 同步翻转(L163-164),只有点时直接 `points.flip(direction)`(L169)。

#### 4.2.4 代码实践

**实践目标**:验证四个变换方法的行为,特别是「属性列不参与变换」与 \(\theta=\pi/2\) 的旋转公式。

**操作步骤**(示例代码,接 4.1.4 的 `pts` 对象):

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

print('初始:\n', pts.tensor[:5])

pts.translate(np.array([1.0, 2.0, 3.0], dtype=np.float32))
print('translate([1,2,3]) 后:\n', pts.tensor[:5])

theta = np.pi / 2
rot_mat_T = pts.rotate(theta)
print('rotate(pi/2) 后:\n', pts.tensor[:5])
print('返回的 rot_mat_T:\n', rot_mat_T)

pts.flip(bev_direction='horizontal')
print('flip(horizontal) 后:\n', pts.tensor[:5])

pts.scale(2.0)
print('scale(2.0) 后:\n', pts.tensor[:5])
```

**需要观察的现象**:每一步打印前 5 个点的全部 4 列,重点盯住第 4 列(height)是否始终是 `[0.5, 0.5, 0.5, 0.3, 0.2]`。

**预期结果**(按手算给出,待本地验证):

| 步骤 | 第 0 个点的 4 列 |
|---|---|
| 初始 | `[1, 0, 0, 0.5]` |
| translate([1,2,3]) | `[2, 2, 3, 0.5]` |
| rotate(π/2) 即 (x,y,z)→(-y,x,z) | `[-2, 2, 3, 0.5]` |
| flip(horizontal) 即 y 取负 | `[-2, -2, 3, 0.5]` |
| scale(2.0) 只缩坐标 | `[-4, -4, 6, 0.5]` |

`rot_mat_T` 应打印出 \(\begin{bmatrix} 0 & 1 & 0 \\ -1 & 0 & 0 \\ 0 & 0 & 1 \end{bmatrix}\)(\(c=\cos\frac\pi2=0,\ s=1\),对应 4.2.2 公式中的转置矩阵)。第 4 列从头到尾不变——这就是「属性列不参与刚体变换」。

#### 4.2.5 小练习与答案

**练习 1**:同一行代码 `pts.rotate(np.pi / 4)`,分别作用在 `LiDARPoints` 和 `CameraPoints` 上,旋转轴有什么不同?

**答案**:都省略了 `axis` 参数,回落到各自的 `self.rotation_axis`:`LiDARPoints` 是 2(绕 z 轴,在 x-y BEV 平面内旋转),`CameraPoints` 是 1(绕 y 轴,在 x-z BEV 平面内旋转)。两者都是绕重力轴的水平旋转,这正是 BEV 检测任务「旋转增强只改朝向不改俯仰」的需要。

**练习 2**:`RandomFlip3D` 里为什么 `height` 属性列要单独再乘一次缩放系数(见 `transforms_3d.py` L751-754),而 `flip` 之后不需要任何补偿?

**答案**:`scale` 改变场景的物理尺度,「点到地面高度」作为几何量必须同比例缩放,但基类 `scale` 只处理前 3 列,所以要手动补;`flip` 是等距镜像变换,高度值大小不变(它挂在属性列里本来就不参与 flip),无需补偿。

**练习 3**:写出点 \((3, 1, 2)\) 绕 z 轴转 \(\pi/2\) 后的坐标,再用 `pts.rotate(np.pi/2)` 验证。

**答案**:按 \((x,y)\mapsto(-y,x)\) 得 \((-1, 3, 2)\)。构造单点 `LiDARPoints(np.array([[3.,1.,2.]]))` 调用 `rotate(np.pi/2)` 后 `tensor` 应为 `[[-1, 3, 2]]`。待本地验证。

### 4.3 三个坐标系子类与 convert_to 跨系转换

#### 4.3.1 概念说明

三个子类总共只做了三件事,却撑起了全部坐标系语义:

1. **设定 `rotation_axis`**:`LiDARPoints` 和 `DepthPoints` 设 2(z 轴),`CameraPoints` 设 1(y 轴)——重力轴不同,默认旋转平面就不同;
2. **实现 `flip`**:按 4.2.3 的规律翻转对应列;
3. **实现 `convert_to`**:它只是 `Coord3DMode.convert_point` 的语法糖——把自己的坐标系填进 `src`,转发给上一讲讲过的统一转换引擎。`CameraPoints` 额外重写了 `bev`(CAM 的 BEV 平面是 x-z 而不是 x-y)。

`convert_to` 转换时会做三件事,与框的转换(`Box3DMode.convert`)严格对应:

- **克隆数据**再改,不动原对象;
- **前 3 列乘转换矩阵**(支持 3×3 纯旋转,或 3×4 含平移的齐次形式),属性列原样拼接回去;
- **用目标坐标系的子类重新包装**,并保留 `points_dim` 与 `attribute_dims`。

**与框侧 `new_box` 的配套关系**:框的转换引擎内部用 `BaseInstance3DBoxes.new_box()`([base_box3d.py:L592](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/base_box3d.py#L592))以同配置创建新坐标系子类实例;点云侧的对应工厂是 `BasePoints.new_point()`([base_points.py:L501-L523](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/base_points.py#L501-L523))。而 `convert_point` 里则是直接 `target_type(arr, ...)` 构造目标子类。配套使用的正确姿势:**点云和 3D 框用同一个 `rt_mat` 分别转换,转换后二者仍然对齐**——多模态/可视化代码(MVXNet、lidar2img 投影)都依赖这一性质。

#### 4.3.2 核心流程

`Coord3DMode.convert_point(point, src, dst, rt_mat)` 的流程:

```text
src == dst ?  ──是──▶  原样返回
    │否
克隆输入(裸数组 / BasePoints.tensor 都要克隆,避免改到调用方数据)
    ▼
rt_mat 未提供? ──是──▶ 查六组坐标系两两默认矩阵(纯轴互换/取负,不含真实标定)
    ▼
rt_mat 3×4 ? ──是──▶ 坐标列拼 1 变齐次:xyz1 @ rt_matᵀ
    └──否(3×3)──▶ xyz @ rt_matᵀ
    ▼
属性列 arr[..., 3:] 原样拼回:cat([新xyz, remains])
    ▼
按 dst 选目标子类(CameraPoints / LiDARPoints / DepthPoints)重新包装
```

注意:六组默认矩阵只做**轴的重排与取负**,描述的是三套坐标系「方向约定」之间的差异;真实传感器之间的转换(LiDAR 装在车上哪个位置、相机外参如何)必须由调用方传入实测的 `rt_mat`(通常来自数据集 info pkl 里的标定矩阵,回顾 u2-l1)。

默认 LiDAR→CAM 矩阵可以按方向约定手工推出:CAM 的 x 轴(右)= LiDAR 的 \(-y\),CAM 的 y 轴(下)= LiDAR 的 \(-z\),CAM 的 z 轴(前)= LiDAR 的 \(x\),即

\[
\begin{bmatrix} x_{cam} \\ y_{cam} \\ z_{cam} \end{bmatrix}
=
\begin{bmatrix} 0 & -1 & 0 \\ 0 & 0 & -1 \\ 1 & 0 & 0 \end{bmatrix}
\begin{bmatrix} x_{lidar} \\ y_{lidar} \\ z_{lidar} \end{bmatrix}
\]

这与源码 L218 行的默认矩阵完全一致。

#### 4.3.3 源码精读

**LiDAR 子类全文不足 80 行,子类的「薄」正说明基类抽象到位**:

[lidar_points.py:L30-L36](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/lidar_points.py#L30-L36) —— 构造只做一件事:调用父类后把 `rotation_axis` 设为 2(z 轴)。

[lidar_points.py:L51-L72](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/lidar_points.py#L51-L72) —— `convert_to` 的实现体:函数体内 import `Coord3DMode`(L70,避免循环导入),然后 `Coord3DMode.convert_point(point=self, src=Coord3DMode.LIDAR, dst=dst, rt_mat=rt_mat)`。CAM/DEPTH 子类的 `convert_to` 结构完全相同,只是 `src` 常量不同([cam_points.py:L56-L77](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/cam_points.py#L56-L77)、[depth_points.py:L51-L72](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/depth_points.py#L51-L72))。

**CameraPoints 对 `bev` 的重写**:

[cam_points.py:L51-L54](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/cam_points.py#L51-L54) —— BEV 取 `[0, 2]`(x-z 平面):CAM 系里 y 是重力轴,俯视图平面由「前向 z + 横向 x」构成。基类默认 `[:, [0, 1]]` 对 CAM 是错的,所以必须重写。

**转换引擎的核心三段**:

[coord_3d_mode.py:L198-L213](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/coord_3d_mode.py#L198-L213) —— 入口类型识别与克隆:`is_InstancePoints` 分支用 `point.tensor.clone()`(L211),保证转换不污染原对象。

[coord_3d_mode.py:L216-L237](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/coord_3d_mode.py#L216-L237) —— 六组默认 `rt_mat` 的查表(如 L218 的 LiDAR→CAM 矩阵);不在表内的方向组合直接 `NotImplementedError`。

[coord_3d_mode.py:L239-L249](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/coord_3d_mode.py#L239-L249) —— 乘法核心:`rt_mat.size(1) == 4` 时拼齐次 1(L241-244),否则直接 `arr[..., :3] @ rt_mat.t()`(L246);L248-249 把属性列拼回——**只转坐标、保留属性的又一次体现**。

[coord_3d_mode.py:L257-L271](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/coord_3d_mode.py#L257-L271) —— 按 `dst` 选目标子类并带着 `attribute_dims` 重新包装——返回对象的类型随目标坐标系变化。

**字符串到类的工厂 `get_points_type`**:

[__init__.py:L10-L31](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/__init__.py#L10-L31) —— 配置文件里写的 `coord_type='LIDAR'` 字符串就是在这里映射到 `LiDARPoints` 类的,大小写不敏感,非法值抛 `ValueError`。

**管线中的生产者 `LoadPointsFromFile`**(点对象在数据流中的出生地):

[loading.py:L645-L648](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L645-L648) —— 从 `lidar_path` 读原始字节流,`reshape(-1, load_dim)` 后按 `use_dim` 选列(这就是 u3-l5 要讲的 `load_dim`/`use_dim` 语义)。

[loading.py:L678-L681](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L678-L681) —— `get_points_type(self.coord_type)` 拿到子类,把 numpy 数组包装成点对象放进 `results['points']`——此后整条管线里流转的都是这个对象,而不是裸数组。

#### 4.3.4 代码实践

**实践目标**:把 4.2.4 变换后的点转到相机坐标系,并与手工矩阵乘法逐元素对比,验证默认转换矩阵。

**操作步骤**(示例代码):

```python
import numpy as np
import torch
from mmdet3d.structures.points import LiDARPoints
from mmdet3d.structures.bbox_3d import Coord3DMode

# 重新构造 4.2.4 flip 后的状态(避免顺序影响,这里直接从初始点开始演示)
points_np = np.array([
    [1.0, 0.0, 0.0, 0.5],
    [0.0, 1.0, 0.0, 0.5],
    [0.0, 0.0, 1.0, 0.5],
], dtype=np.float32)
pts = LiDARPoints(points_np, points_dim=4, attribute_dims=dict(height=3))

# 路线一:子类语法糖
cam_pts = pts.convert_to(Coord3DMode.CAM)
print(type(cam_pts).__name__)       # CameraPoints
print(cam_pts.tensor)

# 路线二:手工矩阵(公式:cam_x=-y, cam_y=-z, cam_z=x)
xyz = torch.from_numpy(points_np[:, :3])
rt = torch.tensor([[0., -1., 0.],
                   [0.,  0., -1.],
                   [1.,  0.,  0.]])
manual = xyz @ rt.t()               # 等价于 rt @ p(列向量)
print(manual)
print(torch.allclose(cam_pts.tensor[:, :3], manual, atol=1e-6))  # 期望 True
print(cam_pts.height)               # 属性列被保留
print(cam_pts.rotation_axis)        # 1(CAM 子类的重力轴)
```

**需要观察的现象**:两条路线的坐标列是否逐元素一致;返回对象的类型从 `LiDARPoints` 变成了 `CameraPoints`;`height` 属性是否还在;原对象 `pts` 的 tensor 是否未被修改。

**预期结果**:`type` 打印 `CameraPoints`;三个点转换后依次为 `[-0, -0, 1, 0.5]`、`[-1, -0, 0, 0.5]`、`[0, -1, 0, 0.5]`(即 \((x,y,z)\mapsto(-y,-z,x)\));`allclose` 为 `True`;`cam_pts.height` 仍是 `[0.5, 0.5, 0.5]`;`cam_pts.rotation_axis == 1`;`pts.tensor` 保持原值(转换内部做了 clone)。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**:`pts.convert_to(Coord3DMode.LIDAR)` 作用在 `LiDARPoints` 对象上会发生什么?

**答案**:`convert_point` 开头的 `if src == dst: return point`([coord_3d_mode.py:L195-L196](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/coord_3d_mode.py#L195-L196))直接返回原对象——注意返回的是**同一个引用**,不是克隆,与跨系转换返回新对象的行为不同。

**练习 2**:配置文件里 `LoadPointsFromFile(coord_type='DEPTH', ...)` 加载 ScanNet 点云后,管线中做 `rotate(0.5)` 旋转,旋转平面是哪两根轴?

**答案**:`get_points_type('DEPTH')` 得到 `DepthPoints`,其 `rotation_axis = 2`(z 轴),所以旋转发生在 x-y 平面(DEPTH 系的 BEV 平面:y 前 x 右 z 上),与 LiDAR 情形相同、与 CAM(绕 y 转、在 x-z 平面)不同。

**练习 3**:为什么 `convert_point` 要支持 3×4 的 `rt_mat`?

**答案**:真实传感器之间的外参一般含平移(LiDAR 和相机的安装位置不同)。3×4 矩阵形如 \([R \mid t]\),把坐标拼上齐次 1 后一次矩阵乘法同时完成旋转与平移(\(p' = Rp + t\));若只支持 3×3,带平移的转换就得拆成两步,容易在「先转后移/先移后转」上出顺序错误。

### 4.4 切片、拼接与搬运:模型侧的基础设施

#### 4.4.1 概念说明

点云对象进入模型前后,还需要一组「容器操作」:`__getitem__` 切片(范围过滤后取子集)、`cat` 拼接(GT 采样把粘贴的目标点云并入当前场景,u3-l7 的核心操作)、`new_point` 工厂(算法内部按原配置新建点对象)、`clone`/`to`/`cuda`/`detach`(设备与梯度管理)。它们与 `numpy()`、`__len__`、`__iter__` 一起,让点云对象可以像张量一样顺手地使用。

#### 4.4.2 核心流程

`__getitem__` 的分派逻辑:

```text
points[3]              → 单点 → 重新包装成 (1, points_dim) 的新对象
points[2:10]           → 切片 → 新对象(属性维度全保留)
points[bool向量/数组]   → 掩码选点 → 新对象(in_range_3d 的结果喂进来)
points[3:11, 列选择]    → 行+列切片 → 新对象,并修正 attribute_dims 下标
```

`cat` 类方法:`torch.cat` 多个对象的 tensor 后用**第一个对象的元数据**重新包装,且注释明确说明不用会共享存储的 layers.cat,保证返回对象与输入解耦。

#### 4.4.3 源码精读

[base_points.py:L310-L387](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/base_points.py#L310-L387) —— `__getitem__`:文档注释列了 5 种合法用法;L360-375 在「行+列」切片时重新计算 `attribute_dims`——选出的列变了,`height` 原来在第 3 列、切片后可能落到第 0 列,登记必须同步修正,否则属性访问就错位了。

[base_points.py:L230-L254](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/base_points.py#L230-L254) —— `in_range_3d` 返回 (N,) 的 bool 掩码,六个比较决定点是否落在 `(x_min, y_min, z_min, x_max, y_max, z_max)` 盒内;它通常与 `__getitem__` 连用。

[transforms_3d.py:L936](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L936) —— `PointsRangeFilter` 的真实消费点:`points_mask = points.in_range_3d(self.pcd_range)`,随后用掩码索引点云,把感知范围外的点扔掉(配合模型 `point_cloud_range` 配置)。

[base_points.py:L397-L418](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/base_points.py#L397-L418) —— 类方法 `cat`:拼接前断言列表非空且所有对象同类型。

[base_points.py:L501-L523](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/base_points.py#L501-L523) —— `new_point`:沿用自身 dtype/device/points_dim/attribute_dims 创建新对象,是框侧 `new_box`([base_box3d.py:L592](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/base_box3d.py#L592))的点云版搭档。u3-l7 的 `DataBaseSampler` 粘贴目标点云、u6 的点云检测器在投票/采样后重建点对象,都靠这对工厂方法。

[base_points.py:L424-L486](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/base_points.py#L424-L486) —— `to` / `cpu` / `cuda` / `clone` / `detach` 一组设备与梯度方法,全部遵循同一模式:操作 tensor 后用 `original_type` 重新包装,`type(self)` 的写法保证子类调用返回子类。

#### 4.4.4 代码实践

**实践目标**:体验掩码过滤 + 切片 + 拼接的组合拳,这正是 `PointsRangeFilter` 与 GT 采样在工作时做的事。

**操作步骤**(示例代码):

```python
import numpy as np
from mmdet3d.structures.points import LiDARPoints

rng = np.random.RandomState(0)
points_np = rng.rand(1000, 4).astype(np.float32) * 80 - 40  # 范围约 [-40, 40)
pts = LiDARPoints(points_np, points_dim=4, attribute_dims=dict(height=3))

mask = pts.in_range_3d([-30, -30, -30, 30, 30, 30])
print('盒内点数:', mask.sum().item(), '/ 1000')

inside = pts[mask]                # 掩码索引 → 新的 LiDARPoints
print(type(inside).__name__, inside.shape, 'height 属性仍可用:',
      inside.height is not None)

merged = LiDARPoints.cat([inside, pts.clone()])
print('cat 后点数:', len(merged))  # 盒内 + 全部 = mask.sum() + 1000
```

**需要观察的现象**:`in_range_3d` 返回的掩码与 `__getitem__` 配合后得到的仍是 `LiDARPoints`,属性访问不失效;`cat` 后点数是两者之和。

**预期结果**:均匀撒点下,边长 60 的盒子占 60³/80³ ≈ 42%,盒内约 420 个点上下(随机种子固定,数值确定);`inside.height` 非 `None`;`len(merged)` 约为 1420。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**:`pts[2:10, [0, 1, 2]]` 返回的对象里 `attribute_dims` 是什么?

**答案**:选出的三列全是坐标、不含原第 3 列(height),所以 `attribute_dims` 中的 `height` 键会被移除(L373-375 的 `else: attribute_dims.pop(key)` 分支),得到 `attribute_dims=None` 或空语义。属性语义跟着列走,这是 `__getitem__` 比「直接切 tensor」多出来的正确性保障。

**练习 2**:`PointsRangeFilter` 为什么必须放在增强管线的靠后位置?

**答案**:旋转/翻转/平移/缩放都会改变点的坐标,过滤太早的话,增强又会把点移出或移入感知范围,浪费计算且与模型 `point_cloud_range` 的假设不符。先做完所有几何增强、最后裁剪,才能保证进入模型的数据与体素化范围一致。

## 5. 综合实践

把本讲全部内容串成一个可运行的验证脚本:构造点云 → 三步变换逐步打印 → 转相机系对比手工矩阵 → 掩码过滤。**全程只使用本讲讲过的 API**。

**实践目标**:一个脚本验证「布局、变换、转换、过滤」四件事,并输出一张逐步演化的坐标表。

**操作步骤**(示例代码,保存为 `verify_points.py` 或在交互环境逐步执行):

```python
import numpy as np
import torch
from mmdet3d.structures.points import LiDARPoints
from mmdet3d.structures.bbox_3d import Coord3DMode

# 1) 构造:xyz + height,KITTI 风格
points_np = np.array([
    [1.0, 0.0, 0.0, 0.5],
    [0.0, 1.0, 0.0, 0.5],
    [0.0, 0.0, 1.0, 0.5],
    [1.0, 1.0, 1.0, 0.3],
    [-1.0, 2.0, 0.5, 0.2],
], dtype=np.float32)
pts = LiDARPoints(points_np, points_dim=4, attribute_dims=dict(height=3))
print('初始\n', pts.tensor)

# 2) 平移 → 旋转(pi/2) → 水平翻转,每步打印前 5 个点
pts.translate(np.array([1., 2., 3.], dtype=np.float32)); print('translate\n', pts.tensor)
rot_mat_T = pts.rotate(np.pi / 2);                      print('rotate\n', pts.tensor, '\nrot_mat_T\n', rot_mat_T)
pts.flip(bev_direction='horizontal');                   print('flip\n', pts.tensor)

# 3) 转到相机系,并与手工矩阵对比
cam_pts = pts.convert_to(Coord3DMode.CAM)
manual = pts.tensor[:, :3] @ torch.tensor(
    [[0., -1., 0.], [0., 0., -1.], [1., 0., 0.]]).t()
print('CameraPoints\n', cam_pts.tensor)
print('与手工矩阵一致:', torch.allclose(cam_pts.tensor[:, :3], manual, atol=1e-6))

# 4) 掩码过滤:只留 z(cam 系为 x)大于 0 的点(演示 in_range_3d 用法即可)
mask = cam_pts.in_range_3d([-100, -100, -100, 100, 100, 100])
print('过滤后剩余点数:', int(mask.sum()))
```

**需要观察的现象**:每步打印中第 4 列(height)恒为 `[0.5, 0.5, 0.5, 0.3, 0.2]`;`rotate` 后坐标满足 \((x,y)\mapsto(-y,x)\);`flip` 后 y 列取负;`convert_to` 后类型变为 `CameraPoints` 且与手工矩阵一致。

**预期结果**(手算参考,待本地验证):

| 步骤 | 第 0 点 | 第 4 点 |
|---|---|---|
| 初始 | `[1, 0, 0, 0.5]` | `[-1, 2, 0.5, 0.2]` |
| translate | `[2, 2, 3, 0.5]` | `[0, 4, 3.5, 0.2]` |
| rotate(π/2) | `[-2, 2, 3, 0.5]` | `[-4, 0, 3.5, 0.2]` |
| flip(horizontal) | `[-2, -2, 3, 0.5]` | `[-4, 0, 3.5, 0.2]`(y=0 翻转不变) |
| convert_to(CAM) | `[3, 2, 2, 0.5]` | `[3.5, 4, 0, 0.2]` |

convert 行按 \((x,y,z)\mapsto(-y,-z,x)\) 计算:如 `[-2,-2,3] → (2, -3, 2)`……注意第 0 点应为 `[2, -3, 2, 0.5]`,上表 convert 行请以公式为准自行核对每个点——这也是练习的一部分。`allclose` 应为 `True`;第 4 步的宽松范围内所有点都保留(5 个)。

**扩展(可选)**:把第 1 步换成真实数据,其余不变:

```python
points_np = np.fromfile('demo/data/kitti/000008.bin', dtype=np.float32).reshape(-1, 4)
pts = LiDARPoints(points_np, points_dim=4)   # KITTI 第 4 列是强度,这里不登记语义
```

观察真实 KITTI 点云(约 1.2 万点)在同样四步操作下的形状与统计量(如 `pts.coord.mean(0)`)变化。

## 6. 本讲小结

- 点云对象 = **一个 \((N, \text{points\_dim})\) 的 float32 张量 + 三个元数据**(`points_dim`、`attribute_dims`、`rotation_axis`);前 3 列固定是坐标,属性列语义靠 `attribute_dims` 登记,setter 甚至能动态拓宽张量补挂属性。
- 四个刚体变换方法(`translate`/`rotate`/`flip`/`scale`)全是**原地修改,且只动坐标列**;`rotate` 返回 `rot_mat_T` 是为了调用方能用同一矩阵同步变换 3D 框——点的旋转底层是 `rotation_3d_in_axis`,行向量右乘转置矩阵,效果为 \(p' = R_z(\theta)p\)。
- 三个子类极薄,只决定三件事:重力轴(`rotation_axis`:LiDAR/Depth 为 2、CAM 为 1)、`flip` 方向(horizontal 翻横向轴、vertical 翻前向轴、重力轴永不翻)、`convert_to` 的 `src` 常量;`CameraPoints` 额外重写 `bev` 为 x-z 平面。
- `convert_to` 是 `Coord3DMode.convert_point` 的语法糖:克隆 → 前三维乘 `rt_mat`(3×3 或 3×4 齐次)→ 属性列拼回 → 目标子类重新包装;默认矩阵只描述坐标系方向约定,真实跨传感器转换必须传标定矩阵。点侧 `new_point` 与框侧 `new_box` 是配套工厂。
- 管线视角:`LoadPointsFromFile` 用 `get_points_type('LIDAR')` 生产点对象放进 `results['points']`;`RandomFlip3D`/`GlobalRotScaleTrans` 消费 `flip`/`translate`/`rotate`/`scale`,`PointsRangeFilter` 消费 `in_range_3d` + 掩码索引;`cat` 拼接是 GT 采样(u3-l7)的基础设施。

## 7. 下一步学习建议

- **下一讲 u2-l4(Det3DDataSample)**:点云对象和 3D 框最终都会被打包进 `Det3DDataSample` 的 `gt_pts_seg`/`inputs` 等字段,成为贯穿训练-推理的数据契约——把本讲的「容器」放进更大的「信封」里理解。
- **u3-l5(数据加载 Transforms)**:精读 `LoadPointsFromFile` 的 `load_dim`/`use_dim` 与 `LoadPointsFromMultiSweeps`,看多帧点云如何用 `cat` 拼接成聚合点云。
- **u3-l6(3D 数据增强)**:本讲只看了 `GlobalRotScaleTrans` 的三个私有方法片段,下一层要完整跟踪「点、框、2D 标注」三者同步变换的全貌。
- **动手方向**:把第 5 节综合实践换成 `demo/data/kitti/000008.bin` 真实点云,配合 `browse_dataset.py` 可视化增强前后的点云,直观感受 `GlobalRotScaleTrans` 的效果。

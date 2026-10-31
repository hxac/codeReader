# 弹性变换与增广管线

## 1. 本讲目标

读完本讲，你应该能够：

1. 说清楚弹性形变（elastic deformation）这个数据增广的数学过程：高斯白噪声 → 三方向均值滤波平滑 → 规则网格插值 → 位移场叠加。
2. 理解 `ElasticTransfrom` 的两个控制参数 `gran`（噪声粒度/形变频率）与 `mag`（位移幅度/形变强度）的含义，并能推导位移量级的粗略估算。
3. 理解一个关键设计：`ElasticTransfrom` **不修改 `points`**，只新增 `elastic_coords` 键；弹性形变最终只影响「点落入哪个体素」，而每个点的特征（RGB、中心化坐标）与全部标注保持原样。
4. 跟踪 `elastic_coords` 键在三站通道（transform 生成 → `Pack3DDetInputs_` 分拣 → `Det3DDataPreprocessor_` 透传）中的流转，并读懂 `collate()` 中坐标的两条路径分支。
5. 动手调整 `gran`/`mag` 参数并可视化形变强度。

## 2. 前置知识

- **数据增广（data augmentation）**：训练时对样本施加随机变换（旋转、缩放、翻转、扰动等），让模型见到更多样的数据，从而更稳健。本讲之前你已经见过 `GlobalRotScaleTrans`（全局旋转/缩放/平移）。
- **体素化（voxelization）**：把连续坐标的点映射到规则立方体网格（体素）上。UniSeg3D 用 `voxel_size=0.02`（2 厘米）的体素，稀疏卷积只在这些被占据的体素上计算。坐标除以 `voxel_size` 后就得到「体素单位」的坐标。
- **刚性 / 仿射 / 弹性形变**：刚性变换是整体旋转平移，仿射变换再加整体缩放剪切——它们对每个点施加**同一个**变换。弹性形变不同：每个点根据自己所在位置获得**不同**的位移，就像把点云放在一块软橡胶上随意揉捏，因此局部结构会被扭曲而不是整体搬运。
- **均值滤波（box blur）与高斯平滑**：对数组做「邻域平均」可以让高频变化变缓。一维 3 抽头均值核 \([1/3, 1/3, 1/3]\) 沿三个轴分别卷积，等价于一次 3D 可分离 box blur；连做两遍近似得到三角核，是逼近高斯平滑的经典技巧。本讲正是用它把纯白噪声「抹」成缓慢变化的随机场。
- **三线性插值（trilinear interpolation）**：在规则网格的 8 个最近格点值之间做加权平均，得到任意连续位置的插值值。`scipy.interpolate.RegularGridInterpolator` 就是做这件事。
- **随机种子**：固定 `np.random.seed(...)` 可以让「随机」噪声可复现，是做增广实验对照的基本功。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [uniseg3d/transforms_3d.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py) | `ElasticTransfrom` 类本体：`transform()` 概率控制与键输出、`elastic()` 噪声场生成与插值 |
| [uniseg3d/uniseg3d.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py) | 模型侧 `collate()`：消费 `elastic_coords` 的两条坐标路径；`loss()` / `predict()` 分别如何调用它 |
| [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py) | `ElasticTransfrom` 在 `train_pipeline` 中的挂载位置与参数（`gran=[6,20]`、`mag=[40,160]`、`p=0.5`） |
| [uniseg3d/formatting.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/formatting.py) | `Pack3DDetInputs_` 把 `elastic_coords` 分拣进 `inputs`（通道第二站） |
| [uniseg3d/data_preprocessor.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/data_preprocessor.py) | `Det3DDataPreprocessor_` 白名单透传 `elastic_coords`（通道第三站） |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**① `elastic()` 噪声生成与网格插值；② `transform()` 的概率控制与坐标输出；③ `collate()` 中 `elastic_points` 分支**。

### 4.1 `elastic()`：噪声场生成、平滑与网格插值

#### 4.1.1 概念说明

弹性形变增广的目标：给点云的坐标叠加一个**空间上平滑**的随机位移场——相邻点位移相近、远处的点位移可以不同，于是整个场景像被揉捏过一样发生局部扭曲。

为什么分割任务需要它？直觉上有两点（背景知识，源自 PointGroup 一脉 3D 实例分割工作的经典做法，UniSeg3D 原样继承了这段代码）：

1. 它等价于对「体素化 / 超点边界」施加噪声：轻微挪动坐标后，原本落在边界一侧的点会掉进另一侧的体素，模拟真实推理时分割边界不精确的情况，迫使模型对边界错位保持稳健；
2. 它增加了坐标分布的多样性，且**不改变任何标注**（标签跟着点走，点还是那些点），是「免费」的增广。

`gran` 与 `mag` 的直觉：

- `gran`（granularity，粒度）：噪声网格的尺寸，控制形变的**频率**。`gran` 小 → 噪声格细密 → 高频、局部的小扭曲；`gran` 大 → 噪声格粗 → 低频、大范围的缓慢弯曲。
- `mag`（magnitude，幅度）：位移的放大系数，控制形变的**强度**，单位是体素。

配置里两组参数串联使用：`(gran=6, mag=40)` 高频小幅抖动 + `(gran=20, mag=160)` 低频大幅扭曲。

#### 4.1.2 核心流程

`elastic(x, gran, mag)` 对一份体素单位坐标 `x`（N×3）执行四步：

```text
① 定网格尺寸：noise_dim = floor(max|x| / gran) + 3        # 每轴
② 生成噪声：3 张独立的 randn(noise_dim) 白噪声场（对应 x/y/z 三轴位移）
③ 平滑：每张噪声场依次做 [x 轴 blur, y 轴 blur, z 轴 blur] × 2 轮
        （3 抽头均值核，mode='constant'，边界补 0）
④ 插值取值：在步长 2·gran、以原点为中心的规则网格上构建
        RegularGridInterpolator，对每个点取三张场的三线性插值值
返回：x + 位移场(x) * mag
```

一点量级估算（忽略边界补零与插值的额外平滑，仅作直觉）：

- 白噪声标准差为 1；3 抽头均值核卷一次使该轴标准差降为 \(1/\sqrt{3}\)，两轮后降为 \(1/3\)。
- 因此单次 `elastic` 的每轴位移标准差约为 \( \mathrm{mag}/3 \) 个体素：

\[
\sigma_{\text{位移}} \approx \frac{\mathrm{mag}}{3} \cdot \text{voxel\_size}
\]

- 代入配置：`mag=40` → 约 13 体素 ≈ 0.26 m；`mag=160` → 约 53 体素 ≈ 1.07 m。插值平均与边界补零还会让实际值略小。这个量级听起来很大，但低频形变在局部近似刚性搬移，场景的局部结构仍然连贯——第 5 节的综合实践会请你用真实数据验证。

网格的构造值得注意：`linspace(-(b-1)·gran, (b-1)·gran, b)`，格点间距是 \(2 \cdot \mathrm{gran}\)，覆盖范围 \(\pm (b-1)\cdot\mathrm{gran}\)。由于 \(b = \lfloor \max|x|/\mathrm{gran}\rfloor + 3\)，有 \((b-1)\cdot\mathrm{gran} \ge \max|x|\)，即格网必然罩住整个点云（`np.abs` 保证负方向也被覆盖），`fill_value=0` 只在极端浮点边缘情况下兜底。

#### 4.1.3 源码精读

先是三组可分离滤波核与噪声网格尺寸（[uniseg3d/transforms_3d.py:L285-L293](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L285-L293)）：三个 `(3,1,1)`/`(1,3,1)`/`(1,1,3)` 的核各除以 3，分别只沿一个轴做 3 抽头均值；`noise_dim` 由点云包围盒除以 `gran` 得到，随后为 x/y/z 三个方向各生成一张标准正态噪声场。

接着是六次卷积（[uniseg3d/transforms_3d.py:L295-L299](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L295-L299)）：`[blur0, blur1, blur2, blur0, blur1, blur2]` 依次卷积，即 x/y/z 三轴各平滑两轮，把逐格独立的白噪声抹成空间相关的低频随机场——这是「弹性」（平滑）的来源；没有这一步，相邻点位移互不相关，形变就退化成普通坐标抖动。

最后是插值与位移叠加（[uniseg3d/transforms_3d.py:L301-L309](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L301-L309)）：`ax` 构建以 0 为中心、步长 \(2\cdot\mathrm{gran}\) 的三轴格点坐标；三张平滑后的噪声场各自包成 `RegularGridInterpolator`（越界填 0）；`i(x)` 对每个点取三线性插值值，`np.hstack` 拼成 N×3 的位移，乘 `mag` 后加回坐标——返回形变后的坐标。

一个环境兼容性提醒：`scipy.ndimage.filters.convolve` 这个写法依赖 `scipy.ndimage.filters` 命名空间，较新版本的 SciPy 已将其弃用（推荐 `scipy.ndimage.convolve`）。若运行本讲实践时报 `AttributeError`，请检查 SciPy 版本（具体删除版本待本地验证）。

#### 4.1.4 代码实践

**实践目标**：绕开整条数据管线，单独调用 `elastic()`，用数值验证「位移量级 ≈ mag/3 个体素」的估算。

**操作步骤**（示例代码，需先完成第 u1-l2 讲的环境搭建，因为 `uniseg3d/transforms_3d.py` 顶部 import 了 torch、torch_scatter、MinkowskiEngine、mmdet3d）：

```python
# 示例代码
import numpy as np
from uniseg3d.transforms_3d import ElasticTransfrom

t = ElasticTransfrom(gran=[6, 20], mag=[40, 160], voxel_size=0.02, p=0.5)
np.random.seed(0)
coords = np.random.rand(100000, 3) * 500   # 模拟约 10m 见方的场景（体素单位）

for gran, mag in [(6, 40), (20, 160)]:
    np.random.seed(1)
    disp = t.elastic(coords, gran, mag) - coords          # N×3 位移（体素单位）
    print(f'gran={gran:>2}, mag={mag:>3}: 每轴位移 std = {disp.std(0)}, '
          f'估算 mag/3 = {mag / 3:.1f}')
```

**需要观察的现象**：打印出的每轴位移标准差与 `mag/3`（13.3 与 53.3）同一量级且略小；位移均值的绝对值相对标准差很小（噪声近似零均值）。

**预期结果**：两组参数的实测 std 都落在估算值的 50%~100% 区间内。若你把 `mag` 换成 4 倍，std 应近似线性放大 4 倍。运行数值以本地为准（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：把 `gran` 从 6 改成 60，`noise_dim` 会怎么变？形变频率会怎么变？

答案：`noise_dim = floor(max|x|/gran) + 3`，`gran` 变大 10 倍则 `noise_dim` 缩小约 10 倍，噪声网格更粗、格点更稀；形变更低频——空间上更平缓、更接近大范围弯曲，高频细节抖动消失。

**练习 2**：如果把六次卷积删掉只留一次 `blur0`（x 轴均值滤波），形变性质会发生什么变化？

答案：y/z 两个轴的噪声仍是无平滑的白噪声，相邻点在这两个方向上的位移互不相关，形变不再是「弹性」的连续扭曲而退化为逐点抖动；只有 x 方向保留平滑位移。这说明六次卷积（三轴各两轮）是产生平滑位移场的关键。

**练习 3**：`mag=[40, 160]` 的单位是什么？换算成米是多少？

答案：单位是**体素**（因为 `transform()` 传入的坐标已除以 `voxel_size`）。按 `voxel_size=0.02` m 换算，位移放大系数分别约为 0.8 m 与 3.2 m——注意这是放大系数而非实际位移，实际位移标准差约为 `mag/3` 个体素，即约 0.26 m 与 1.07 m。

### 4.2 `transform()`：概率控制、单位换算与「只写新键、不改 points」

#### 4.2.1 概念说明

`ElasticTransfrom` 是一个标准的 mmcv `BaseTransform`，但它与一般增广有一个本质区别：

> **它不修改 `input_dict['points']`，只新增一个 `elastic_coords` 键。**

也就是说，点云的原始坐标、RGB、以及此前管线产出的所有标注（`gt_sp_masks`、`sp_pts_mask`、点提示下标等）全部原样保留；弹性形变的坐标**单独存放**，等模型侧 `collate()` 决定是否使用（见 4.3）。这个设计把「数据增广」和「坐标消费」解耦：特征与监督信号永远来自未形变的点，形变只影响体素化的归属。

三个细节：

1. **概率控制**：参数 `p` 是应用形变的概率，配置取 `p=0.5`（[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L190](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L190)）。注意**即使未命中概率，`elastic_coords` 键也照样写入**（只是存未形变的坐标）——所以训练时该键恒存在。
2. **单位换算**：第一步就把坐标除以 `voxel_size`，之后 `gran`/`mag` 都在体素单位下工作；这也解释了为什么 4.3 中 `collate()` 的弹性分支**不再**除以体素尺寸。
3. **注册名拼写**：类名是 `ElasticTransfrom`（`Trans-from`，作者笔误少了一个 `s`），配置字符串必须与之一字不差——这是「配置即代码」的代价，也是第 u2-l1 讲注册器机制的又一次体现。

#### 4.2.2 核心流程

```text
输入: input_dict（此时 points 已经过 GlobalRotScaleTrans、
      NormalizePointsColor_、AddSuperPointAnnotations、
      TextPromptGeneration、PointPromptGeneration）

coords = points.tensor[:, :3].numpy() / voxel_size     # 米 → 体素单位
if rand() < p:
    coords = elastic(coords, gran[0], mag[0])          # 高频小幅
    coords = elastic(coords, gran[1], mag[1])          # 低频大幅（串联叠加）
input_dict['elastic_coords'] = coords                  # 恒写入；points 不动
```

两次 `elastic` 是**串联**的：第二次在第一次形变后的坐标上再形变，位移近似叠加，总位移方差约为两者之和。

#### 4.2.3 源码精读

注册与构造参数（[uniseg3d/transforms_3d.py:L237-L253](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L237-L253)）：`@TRANSFORMS.register_module()` 把 `ElasticTransfrom` 登记进 TRANSFORMS 注册表，`__init__` 仅保存 `gran`、`mag`、`voxel_size`、`p` 四个参数，无任何其他状态。

核心的 `transform()`（[uniseg3d/transforms_3d.py:L255-L270](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L255-L270)）：L265 从 `points` 对象取出 xyz（此时是 mmdet3d 的点云对象，`.tensor[:, :3]` 即坐标，单位米）并除以 `voxel_size` 换成体素单位；L266-L268 以概率 `p` 串联调用两次 `self.elastic`；L269 把结果写入新键 `elastic_coords` 后返回——自始至终没有触碰 `input_dict['points']`。

配置中的挂载（[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L185-L190](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L185-L190)）：`gran=[6, 20]`、`mag=[40, 160]`、`voxel_size=0.02`、`p=0.5`。它在 `train_pipeline` 中位于 `TextPromptGeneration`/`PointPromptGeneration`（[L177-L184](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L177-L184)）之后、`Pack3DDetInputs_`（[L191-L199](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L191-L199)）之前。承接第 u3-l4 讲的结论：点提示选点用的是**弹性形变前**（但已过旋转缩放）的坐标，且只输出点**下标**，因此弹性化不影响选点结果；`elastic_coords` 在 `Pack3DDetInputs_` 的 keys 里位于 L195。

#### 4.2.4 代码实践

**实践目标**：验证三件事——① `p=0` 时 `elastic_coords` 等于原坐标除以体素尺寸；② `points` 本体从未被修改；③ `p=1` 时发生可复现的形变。

**操作步骤**（示例代码）：

```python
# 示例代码
import numpy as np, torch
from mmdet3d.structures.points import LiDARPoints
from uniseg3d.transforms_3d import ElasticTransfrom

xyzrgb = torch.rand(200, 6); xyzrgb[:, :3] *= 4.0      # 4m 见方的假场景
t0 = ElasticTransfrom(gran=[6, 20], mag=[40, 160], voxel_size=0.02, p=0.0)
t1 = ElasticTransfrom(gran=[6, 20], mag=[40, 160], voxel_size=0.02, p=1.0)

d = {'points': LiDARPoints(xyzrgb.clone())}
out0 = t0.transform(d)
print('p=0 时 elastic_coords == 原坐标/0.02 :',
      np.allclose(out0['elastic_coords'], xyzrgb[:, :3].numpy() / 0.02))
print('points 未被修改 :', torch.equal(d['points'].tensor, xyzrgb))

np.random.seed(42)
out1 = t1.transform({'points': LiDARPoints(xyzrgb.clone())})
disp_m = (out1['elastic_coords'] - xyzrgb[:, :3].numpy() / 0.02) * 0.02
print('p=1 时每轴位移 std（米）:', disp_m.std(0))
```

**需要观察的现象**：两个布尔打印均为 `True`；`p=1` 的位移 std 与 4.1 的量级估算（约 0.26 m / 1.07 m 的平方和开根，即约 1.1 m 上下）一致。

**预期结果**：`p=0` 分支证明「未命中概率也写键、坐标保持原值」，`torch.equal` 证明 points 不被修改。结果以本地运行为准（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `transform()` 要在第一步就除以 `voxel_size`，而不是保持米单位、在 `collate()` 里再统一处理？

答案：为了让 `gran`/`mag` 以体素为单位定义——形变的频率和幅度应当与体素网格的尺度挂钩（挪动一点就要跨过体素边界才有意义）。提前换算使 `elastic()` 与体素尺寸解耦，也让 `collate()` 的弹性分支拿到的是「已除过体素尺寸」的坐标，直接做平移对齐即可。

**练习 2**：`p=0.5` 时，训练中大约一半样本的 `elastic_coords` 其实没有形变。那 `collate()` 走弹性分支和走原始分支还有区别吗？

答案：基本没有。未形变时 `elastic_coords = p/voxel`，而原始分支算的是 `(p - min)/voxel`，两者只差一个常数平移（`min/voxel`），且弹性分支会自己做 `el_p - el_p.min(0)[0]` 的对齐，因此两种分支对同一份未形变坐标给出等价的体素坐标（仅浮点舍入差异）。弹性分支并非「额外的增广开关」，而是「坐标来源不同」。

**练习 3**：如果把配置里的 `p` 改成 `1.0`，训练行为会如何变化？这是个好主意吗？

答案：每个训练样本都会被两组 `gran/mag` 形变。形变幅度不小（低频一路位移 std 约 1 m），全部样本都形变可能让几何结构过度失真、拖慢收敛甚至伤害精度；`p=0.5` 是「一半干净、一半扰动」的折中。是否更优属于超参问题，需实验验证（待本地验证）。

### 4.3 三站通道与 `collate()` 的两条坐标路径

#### 4.3.1 概念说明

本模块回答两个问题：`elastic_coords` 如何从 transform 走到模型？以及 `collate()` 拿到它之后做了什么？

第一问在 u3-l2 已建立框架——**键的三站通道**：transform 生成键 → `Pack3DDetInputs_` 分拣键 → `Det3DDataPreprocessor_` 透传键，三站键名必须一字不差。`elastic_coords` 正是通道里最标准的一个样本。

第二问是本讲的核心结论：

> `collate()` 有两条坐标路径。无弹性路径用**原始坐标除以体素尺寸**做体素化；有弹性路径直接用**已形变的体素单位坐标**（只做最小值对齐，不再除体素尺寸）。但**两条路径的点特征完全相同**——都用原始点的 RGB 与均值中心化坐标构建。

因此弹性增广的净效果是：**只改变每个点「落进哪个体素」，不改变任何点的特征与标注**。它是对体素化过程本身的扰动，而不是对场景内容的扰动。训练时模型被迫适应「同一点可能被挪进邻近体素」的噪声，这正是它作为分割边界正则的机制。

#### 4.3.2 核心流程

键的流转与两条路径：

```text
train_pipeline:  ElasticTransfrom 生成 elastic_coords（N×3, float, 体素单位）
        ↓
Pack3DDetInputs_: elastic_coords ∈ INPUTS_KEYS → 塞进 data['inputs']
        ↓
dataloader 伪批组装（样本成 List）
        ↓
Det3DDataPreprocessor_: 'elastic_coords' ∈ inputs → batch_inputs['elastic_coords']
        ↓
UniSeg3D.loss():  collate(points, batch_inputs_dict.get('elastic_coords', None))
        ↓
collate() 二选一：
  分支 A（elastic_points is None，测试时恒走此路）:
      coords = (p[:, :3] - p[:, :3].min()) / voxel_size     # 原始坐标体素化
  分支 B（训练时恒走此路，无论是否真的形变）:
      coords = el_p - el_p.min()                            # 已除过 voxel、已形变
  两条分支的 features 相同:  hstack(p[:, 3:], p[:, :3] - p[:, :3].mean())
        ↓
ME.utils.batch_sparse_collate → TensorField.sparse() 体素量化为稀疏张量
```

#### 4.3.3 源码精读

**第一站·分拣**：`Pack3DDetInputs_` 的输入键白名单（[uniseg3d/formatting.py:L15-L18](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/formatting.py#L15-L18)）把 `elastic_coords` 与 `points`、各提示键并列放进 `INPUTS_KEYS`；打包时 `if key in self.INPUTS_KEYS`（[uniseg3d/formatting.py:L119](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/formatting.py#L119)）把它路由进 `inputs` 字典而非 `data_sample`。

**第二站·透传**：`Det3DDataPreprocessor_` 在透传 `points` 之后单独检查弹性键（[uniseg3d/data_preprocessor.py:L36-L37](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/data_preprocessor.py#L36-L37)）：存在则放进 `batch_inputs`，不存在则静默跳过——测试管线里键不存在，也不会报错。

**第三站·消费**：`loss()` 调用 `self.collate(batch_inputs_dict['points'], batch_inputs_dict.get('elastic_coords', None))`（[uniseg3d/uniseg3d.py:L449-L451](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L449-L451)）。注意用的是 `.get(..., None)`：键缺失时得到 `None`，`collate()` 自然落入无弹性分支。

**两条分支**（[uniseg3d/uniseg3d.py:L381-L413](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L381-L413)）：无弹性分支（L394-L398）对每个样本取 `p[:, :3]` 减最小值后除以 `self.voxel_size` 作坐标，特征是 `p[:, 3:]`（归一化后的 RGB）与 `p[:, :3] - p[:, :3].mean(0)`（均值中心化坐标）横向拼接；弹性分支（L399-L403）改用 `el_p - el_p.min(0)[0]` 作坐标——**没有再除以体素尺寸**，因为 `transform()` 已经除过；特征仍从原始 `p` 构建，与分支 A 一字不差。随后两条路径汇合：`ME.utils.batch_sparse_collate` 为每个样本的坐标加上批次索引列并合并，`spatial_shape` 按 `min_spatial_shape` 裁剪下界（L405-L406），`TensorField.sparse()`（L407-L411）把落在同一体素内的点量化归并成稀疏张量条目，并返回 `inverse_mapping` 供把体素特征映射回每个点。

**测试侧为什么永远走分支 A**：`test_pipeline` 里根本没有 `ElasticTransfrom`，且 `Pack3DDetInputs_` 的 keys 也不含 `elastic_coords`（[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L239-L243](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L239-L243)）；`predict()` 更是只传一个参数 `self.collate(batch_inputs_dict['points'])`（[uniseg3d/uniseg3d.py:L780-L781](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L780-L781)），`elastic_points` 取默认值 `None`。增广只属于训练，评估在干净的坐标上进行——这是所有数据增广的通例。

#### 4.3.4 代码实践（源码阅读 + 手算型）

**实践目标**：用三个点的小例子手算两条路径的体素归属差异，直观理解「弹性只改体素归属」。

**操作步骤**：

1. 设 `voxel_size=0.02`，三个点（米）：A=(0.000, 0, 0)、B=(0.019, 0, 0)、C=(0.021, 0, 0)。
2. 分支 A：`(x - 0)/0.02` 得 0.0、0.95、1.05 → 体素化后 A、B 落体素 0，C 落体素 1。
3. 分支 B：设弹性位移恰为每点 +0.5 体素，则坐标变 0.5、1.45、1.55 → A 落体素 0，B、C 落体素 1——**B 被挪过了体素边界**。
4. 对照源码确认：两条分支里 A/B/C 的特征（RGB + 中心化坐标）与任何 GT 标注都不随位移改变。

**需要观察的现象**：位移仅为半个体素，就足以让 B 的体素归属翻转；位移不到一个体素时，多数点的归属不变。

**预期结果**：你可以总结出规则——位移绝对值小于 0.5 体素的点大概率保持原体素，跨越 \(\pm 0.5\) 体素边界的点被重新分箱。整场景统计下来，位移 std 约 1 m（约 53 体素）意味着大量点被重新分箱，但**邻近点的位移高度相关**（低频场），所以物体的整体形状基本保留、局部边界被揉开。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `transform()` 里写入的键改名为 `elastic_coord`（少一个 s），会发生什么？

答案：三站通道断流且**不报错**——`Pack3DDetInputs_` 白名单里没有这个名字，键被丢弃；`Det3DDataPreprocessor_` 检查 `'elastic_coords' in inputs` 不成立，静默跳过；`loss()` 的 `.get(..., None)` 返回 `None`，`collate()` 落入分支 A。训练照常进行，只是弹性增广悄悄失效。这正是 u3-l2 强调「三站键名必须一字不差」、且缺失往往是静默的原因。

**练习 2**：分支 B 的坐标 `el_p - el_p.min(0)[0]` 为什么不再除以 `voxel_size`？如果除会发生什么？

答案：因为 `el_p` 在 `transform()` 里已经除过体素尺寸（体素单位）。若再除一次，坐标会被压缩 50 倍，整个场景被塞进几十个体素里，体素分辨率实际变成 0.0004 m，稀疏张量的形状与特征对应关系虽不崩溃（`sparse()` 仍会量化），但体素化完全失真、模型输入分布被破坏。

**练习 3**：为什么特征里要用「均值中心化的原始坐标」`p[:, :3] - p[:, :3].mean(0)`，而坐标路径却用「最小值对齐」？

答案：坐标要做体素化，体素索引必须是非负整数，最小值对齐把每个场景平移到第一卦限，方便构建稀疏网格；特征是喂给网络的数值，均值中心化去掉场景的全局位置偏移、让数值分布以 0 为中心更稳定。两者服务不同目的，所以对齐方式不同。

## 5. 综合实践

**任务**：对一个真实 ScanNet 场景点云，分别用原始参数 `[6, 20]/[40, 160]` 与放大 4 倍的 `mag` 运行弹性形变，画出形变前后的点云对比图，并用位移统计验证 4.1 的量级估算。

**操作步骤**（示例代码；点云 bin 文件的来历见 u1-l4，由 `create_data.py` 产出，`(N, 6)` float32，位于 `data/scannet/points/` 子目录，目录名由配置的 `data_prefix` 决定）：

```python
# 示例代码：elastic_visualize.py（放在仓库根目录运行）
import numpy as np
import matplotlib.pyplot as plt
from uniseg3d.transforms_3d import ElasticTransfrom

# ① 读一个真实场景：把文件名换成你 data/scannet/points/ 下实际存在的场景
pts = np.fromfile('data/scannet/points/scene0000_00.bin',
                  dtype=np.float32).reshape(-1, 6)
voxel = 0.02
coords = pts[:, :3] / voxel                     # 与 transform() 第一步一致

t = ElasticTransfrom(gran=[6, 20], mag=[40, 160], voxel_size=voxel, p=1.0)

def deform(c, mag_scale=1.0):                   # 复现 transform() 的两次串联
    for g, m in zip(t.gran, t.mag):
        c = t.elastic(c, g, m * mag_scale)
    return c

np.random.seed(2024)
c1 = deform(coords.copy(), 1.0)                 # 原始 [6,20] / [40,160]
np.random.seed(2024)
c4 = deform(coords.copy(), 4.0)                 # mag 放大 4 倍

for name, c in [('mag x1', c1), ('mag x4', c4)]:
    d = np.linalg.norm((c - coords) * voxel, axis=1)      # 位移（米）
    print(f'{name}: 位移 mean={d.mean():.3f} m, '
          f'std={d.std():.3f} m, max={d.max():.3f} m')

# ② 三联图对比（下采样 2 万点保证 matplotlib 速度）
idx = np.random.choice(len(coords), 20000, replace=False)
fig = plt.figure(figsize=(15, 5))
for k, (title, c) in enumerate(
        [('original', coords), ('elastic mag x1', c1), ('elastic mag x4', c4)]):
    ax = fig.add_subplot(1, 3, k + 1, projection='3d')
    ax.scatter(*c[idx].T, s=0.3, c=pts[idx, 3:6] / 255.0, marker='.')
    ax.set_title(title); ax.set_axis_off()
plt.savefig('elastic_compare.png', dpi=150)
print('已保存 elastic_compare.png')
```

**需要观察的现象**：

1. `mag x1`：房间结构仍清晰可辨，但墙面、家具的边缘明显「发糊」——高频小幅扰动揉开了边界；位移 mean 大约在零点几米量级。
2. `mag x4`：低频大幅形变占主导，整个房间出现肉眼可见的弯曲、折叠，局部区域被明显拉伸；位移 mean 接近 `mag x1` 的 4 倍（位移与 `mag` 近似线性）。
3. 两张形变图中颜色（原始 RGB）与原始图完全一致——再次印证「特征不变，只动坐标」。

**预期结果**：`elastic_compare.png` 呈现「原图 → 轻微揉捏 → 明显扭曲」的递进；位移统计与 4.1 的 `mag/3` 估算同一量级。若无真实数据，可退回 4.1 的合成点云（`np.random.rand(N,3)*500`）。图示效果待本地验证。

## 6. 本讲小结

- 弹性形变 = 平滑随机场位移：白噪声 → 三轴均值滤波各两轮 → 步长 \(2\cdot\mathrm{gran}\) 的规则网格三线性插值 → 乘 `mag` 叠加；`gran` 控制频率、`mag` 控制幅度（体素单位），位移 std 约为 \(\mathrm{mag}/3\) 个体素。
- `transform()` 以概率 `p=0.5` 串联两组 `(gran, mag)`，**不修改 `points`**，只把体素单位的形变坐标写入新键 `elastic_coords`；未命中概率时键也照写（存未形变坐标）。
- `elastic_coords` 走标准三站通道（`Pack3DDetInputs_` 的 `INPUTS_KEYS` → `Det3DDataPreprocessor_` 白名单透传 → `loss()` 的 `.get()`），任一站键名不符都会让增广**静默失效**。
- `collate()` 两条路径：无弹性分支用原始坐标 `/voxel_size`，弹性分支用已除过体素尺寸的形变坐标只做最小值对齐；**两条路径的点特征（RGB + 均值中心化坐标）完全相同**——弹性增广的净效果只是扰动「点落入哪个体素」。
- 测试侧（`test_pipeline` 无 `ElasticTransfrom`、`predict()` 只传 points）恒走无弹性分支：增广只属于训练，评估在干净坐标上进行。

## 7. 下一步学习建议

至此第三单元（数据流）收官。你已经完整掌握了一个训练样本从 bin 文件到 `batch_inputs_dict` 的全部旅程。接下来进入第四单元，建议从 **u4-l1「UniSeg3D 模型骨架与训练前向 loss()」** 开始：本讲看到的 `collate()` 产物（coordinates/features/inverse_mapping/spatial_shape）正是在那里被组装成 `spconv.SparseConvTensor` 并送入 `extract_feat()` 的超点聚合——`inverse_mapping` 如何把体素特征还给每个点、`batch_offsets` 如何拼接多场景超点，是下一讲的第一站。若想先巩固数据侧，可回读 u3-l2 的「键的三站通道」并与本讲 4.3 对照。

# BaseInstance3DBoxes：7 自由度 3D 框的抽象

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 `BaseInstance3DBoxes` 中 `(N, box_dim)` 张量每一列的物理含义，说出「7 自由度」分别是什么。
2. 会用 `center` / `bottom_center` / `gravity_center` / `dims` / `yaw` / `height` / `volume` 这组只读属性，以及 `bev` / `nearest_bev` 两种 BEV（鸟瞰图）投影。
3. 会用 `in_range_bev` / `in_range_3d` 做范围过滤，用 `rotate` / `flip` / `translate` / `scale` 做几何变换。
4. 说出 `LiDARInstance3DBoxes`、`CameraInstance3DBoxes`、`DepthInstance3DBoxes` 三个子类在 **参考原点（origin）、旋转轴（YAW_AXIS）、高度方向** 上的约定差异，并能解释为什么 `CameraInstance3DBoxes` 要重写 `height_overlaps` 和 `bev`。
5. 理解 `with_yaw=False`（轴对齐框）时 `rotate` 如何取旋转后的外接矩形——这正是 ScanNet 室内检测的用法。

## 2. 前置知识

本讲直接建立在 u2-l1（三大坐标系）之上，开始前请确认你理解以下概念：

- **7 自由度 3D 框**：一个长方体在三维空间中的位姿由「中心位置 3 个平移自由度 + 尺寸 3 个自由度 + 绕重力轴的朝向角 yaw 1 个自由度」共 7 个数描述。3D 检测中通常不做滚转（roll）和俯仰（pitch），所以不是 9 自由度。
- **三套右手坐标系**：LIDAR（室外，x 前、y 左、z 上）、CAM（单目，z 前、x 右、y **向下**）、DEPTH（室内，x 右、y 前、z 上）。重力轴（yaw 的旋转轴）分别是 z、y、z。
- **底部中心约定**：mmdet3d 中一个框的「锚点」默认取**底面中心**（LiDAR/Depth 下相对原点为 `(0.5, 0.5, 0)`），而不是几何中心。这是从 KITTI 等数据集沿袭下来的习惯。
- **张量（Tensor）**：PyTorch 的多维数组。本讲所有框数据最终都存在一个 `float32` 的 `(N, box_dim)` 张量里，N 是框数量。
- **右手法则**：右手四指从 +x 轴弯向 +y 轴时，拇指指向 +z 轴。绕 +z 轴正角度旋转时，+x 方向的向量会朝 +y 方向转动（从 +z 往下看是逆时针）。本讲会多次用它验证旋转方向。

一个容易混淆的点先说清楚：**「底部中心」在不同坐标系里落在框的哪个部位是不一样的**。LiDAR/Depth 中底部中心相对框是 `(0.5, 0.5, 0)`（x、y 方向居中，z 方向贴底）；CAM 中是 `(0.5, 1.0, 0.5)`（x、z 居中，y 贴"底"——而 CAM 的 y 轴向下，所以"底"是 y 最大的那个面）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [mmdet3d/structures/bbox_3d/base_box3d.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/base_box3d.py) | 基类 `BaseInstance3DBoxes`：张量布局、构造与 origin 换算、属性族、BEV 投影、范围判断、容器操作、3D IoU |
| [mmdet3d/structures/bbox_3d/lidar_box3d.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/lidar_box3d.py) | LiDAR 子类：`corners` 与 `rotate` 的"参考实现"，绕 z 轴旋转 |
| [mmdet3d/structures/bbox_3d/cam_box3d.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/cam_box3d.py) | 相机子类：y 轴向下带来的全部特例（origin、height、bev、IoU、local_yaw） |
| [mmdet3d/structures/bbox_3d/depth_box3d.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/depth_box3d.py) | 深度（室内）子类：`with_yaw=False` 的轴对齐框处理、`get_surface_line_center` |
| [mmdet3d/structures/bbox_3d/utils.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/utils.py) | 工具函数：`limit_period`、`rotation_3d_in_axis`（所有旋转的底层）、`yaw2local` |

这五个文件都在 `mmdet3d/structures` 最底层，不依赖 datasets 和 models，可以脱离数据集单独实验——本讲的实践全部只需要 `import torch` 和这几个类。

## 4. 核心概念与源码讲解

### 4.1 BaseInstance3DBoxes 基类：张量布局与构造

#### 4.1.1 概念说明

3D 检测里"一个框"要同时被四处使用：数据集存 GT、数据增强做几何变换、模型回归目标、评估算 IoU。如果各处用不同格式（有的存 8 个顶点、有的存 minmax、有的存中心 + 尺寸），转换代码会爆炸。`BaseInstance3DBoxes` 的做法是：**把 N 个框统一装进一个 `(N, box_dim)` 张量，再用一组 property 和方法在这个张量上提供所有常用视图与操作**。

它是一个"包装类"（wrapper）而非神经网络模块：没有 `nn.Module`，不做梯度，只是张量的高级视图 + 原地（in-place）几何操作。理解了这一点，你会明白为什么 `rotate`、`flip`、`translate` 直接改 `self.tensor`，而 `corners`、`bev` 每次都是现算。

#### 4.1.2 核心流程

构造一个框对象时发生三件事：

```
输入 tensor (N, box_dim)
   │
   ├─ 1. 转成 float32 的 torch.Tensor（自动选 device）
   │
   ├─ 2. 形状断言：必须是 2 维、最后一维 == box_dim
   │     特例：box_dim == 6（无朝向的 minmax 框）
   │       → 补一列 0 当"假 yaw"，with_yaw 强制为 False
   │
   ├─ 3. clone() 存入 self.tensor（与输入解耦）
   │
   └─ 4. origin 换算：若 origin != 默认值，
         把输入锚点表示的框平移到默认锚点表示
         new_center = old_center + dims * (dst_origin - src_origin)
```

origin 换算的直觉：同一个长方体，"用底面中心描述"和"用几何中心描述"中心坐标差半个高度。换算公式就是按相对比例 `(dst - src)` 乘上三个尺寸再平移，例如 LiDAR 下从几何中心 `(0.5, 0.5, 0.5)` 换到底部中心 `(0.5, 0.5, 0)`，z 坐标要减去 \( 0.5 \times z_{size} \)。

#### 4.1.3 源码精读

类的骨架与文档注释，明确说出"底部中心"约定和 7 列含义：

[base_box3d.py:L15-L39](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/base_box3d.py#L15-L39) —— 基类定义。docstring 写明：框默认底部中心（相对原点 `(0.5, 0.5, 0)`），每行为 `(x, y, z, x_size, y_size, z_size, yaw)`，`box_dim` 默认 7。第 41 行的类属性 `YAW_AXIS: int = 0` 是给子类覆盖用的占位。

构造函数主体：

[base_box3d.py:L43-L80](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/base_box3d.py#L43-L80) —— 完成上面流程图的全部四步：L54 把任意输入 `as_tensor` 成 float32；L59-L62 断言形状；L64-L71 处理 6 维特例（补假 yaw、置 `with_yaw=False`）；L75 `clone()` 防止外部修改输入张量影响框；L77-L80 做 origin 换算，注意换算只动前 3 列（中心），且是用 `self.tensor[:, 3:6]`（尺寸）逐列加权的。

属性族——最常用的只读视图：

[base_box3d.py:L87-L148](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/base_box3d.py#L87-L148) —— 一组 property：`volume = 三尺寸连乘`；`dims` 取第 3~5 列；`yaw` 取第 6 列；`height` 取第 5 列（注意基类假定 z 是高度，CAM 子类会重写）；`top_height = bottom_height + height`、`bottom_height = tensor[:, 2]` 同理会被 CAM 重写；`center` 在 L134 直接返回 `bottom_center`（基类约定"中心"即底部中心，并建议需要语义明确时用后两者）；`gravity_center` 在 L141-L148 把底部中心沿高度方向抬半个 `height` 得到几何中心。

范围判断：

[base_box3d.py:L181-L203](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/base_box3d.py#L181-L203) —— `in_range_bev` 只检查 **BEV 中心点**（`bev` 的第 0、1 列）是否落在 `(x_min, y_min, x_max, y_max)` 内，返回 `(N,)` 的 bool 向量。docstring 特别说明：SECOND 原版是用"点是否在凸多边形内"判断的，这里为了效率简化成了只查中心。

[base_box3d.py:L260-L285](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/base_box3d.py#L260-L285) —— `in_range_3d` 则用 `gravity_center` 检查三维范围 `(x_min, y_min, z_min, x_max, y_max, z_max)`。数据管线里 `ObjectRangeFilter` 过滤超界 GT 用的就是这类接口。

容器与设备操作：

[base_box3d.py:L350-L383](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/base_box3d.py#L350-L383) —— `__getitem__` 支持 `boxes[3]`、`boxes[2:10]`、`boxes[bool_vector]` 三种索引，关键在 L374 用 `type(self)` 重建对象，保证切出来的还是**同一个子类**（从 `DepthInstance3DBoxes` 切片不会退化成基类）。

[base_box3d.py:L393-L415](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/base_box3d.py#L393-L415) 与 [base_box3d.py:L592-L612](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/base_box3d.py#L592-L612) —— `cat` 类方法拼接多个同类型框对象（GT 采样后拼接就靠它）；`new_box` 用同样的 `box_dim/with_yaw` 在同 device 上造一个新框对象。注意 `cat` 有 L407 的类型断言：**不同坐标系子类不能互相 cat**。

3D IoU——高度重叠 × BEV 旋转 IoU：

[base_box3d.py:L495-L526](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/base_box3d.py#L495-L526) —— `height_overlaps` 计算 z 方向的重叠长度 \(\max(0,\ \min(z^{top}_1, z^{top}_2) - \max(z^{bottom}_1, z^{bottom}_2))\)，得到 `(N, M)` 矩阵。

[base_box3d.py:L528-L590](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/base_box3d.py#L528-L590) —— `overlaps` 把 3D IoU 分解为"BEV 平面旋转 IoU × 高度重叠体积"。两个值得咀嚼的细节：L566-L567 把 BEV 宽高 `clamp(min=1e-4)` 防止零尺寸框让 CUDA 算子 `box_iou_rotated` 崩掉；L575 的 `overlaps_bev = iou2d * (areas1 + areas2) / (1 + iou2d)` 是从 IoU **反解回交集面积**——由 \( \text{iou} = \frac{I}{a_1 + a_2 - I} \) 可得 \( I = \frac{\text{iou} \cdot (a_1 + a_2)}{1 + \text{iou}} \)。最终 \( \text{IoU}_{3d} = \frac{I_{bev} \cdot I_h}{V_1 + V_2 - I_{bev} \cdot I_h} \)。

#### 4.1.4 代码实践

**实践目标**：验证构造流程中的 origin 换算与 6 维框特例。

**操作步骤**（以下为示例代码，不是仓库自带脚本）：

```python
import torch
from mmdet3d.structures import LiDARInstance3DBoxes

# 1) origin 换算：传入"几何中心"表示的框，内部转成"底部中心"表示
geo_center = torch.tensor([[0., 0., 1., 2., 4., 2., 0.5]])  # z=1 是几何中心
box = LiDARInstance3DBoxes(geo_center, origin=(0.5, 0.5, 0.5))
print(box.bottom_center)   # 预期 z = 1 - 0.5*2 = 0
print(box.gravity_center)  # 预期 z = 0 + 0.5*2 = 1（回到原输入）

# 2) 6 维 minmax 框：自动补假 yaw
box6 = LiDARInstance3DBoxes(torch.rand(3, 6), box_dim=6)
print(box6.box_dim, box6.with_yaw, box6.yaw)  # 7 True tensor([0., 0., 0.])
```

**需要观察的现象**：`bottom_center` 的 z 从 1 变成 0；6 维输入后 `box_dim` 变成 7、`yaw` 全 0。

**预期结果**：与注释一致（由 [base_box3d.py:L77-L80](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/base_box3d.py#L77-L80) 和 [base_box3d.py:L64-L71](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/base_box3d.py#L64-L71) 的逻辑直接推出；具体打印值待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `BaseInstance3DBoxes.corners` 在基类里是 `pass`（L151-L153），而 `bev` 却有实现？

**答案**：`bev` 只是从张量里挑 5 列（见 4.2），不涉及坐标系差异，可以在基类统一实现；而 8 个顶点的生成依赖"哪个轴是重力轴、锚点在框的哪个面"，LiDAR/Depth 绕 z 转、CAM 绕 y 转，锚点偏移也不同，所以 `corners` 连同 `rotate`、`flip`、`convert_to` 一起声明为抽象方法，强制子类各自实现。

**练习 2**：`overlaps` 中为什么先算高度重叠再乘 BEV 重叠，而不是直接在 3D 空间算？

**答案**：旋转长方体的精确 3D 相交体积计算代价很高；而"BEV 平面上带旋转角的矩形 IoU"有现成 CUDA 算子 `box_iou_rotated`，高度方向的重叠只是简单的一维区间交集。两者相乘即得 3D 交集体积，这是速度与精度的折中。

**练习 3**：`boxes[bool_mask]` 返回的对象和原对象是什么关系？

**答案**：`__getitem__` 用 `type(self)` 重建一个同子类、同 `box_dim/with_yaw` 的新对象；其内部张量服从 PyTorch 索引语义（L367-L368 注释提醒：可能与原对象共享存储）。

### 4.2 BEV 视图：bev、nearest_bev 与 in_range_bev

#### 4.2.1 概念说明

BEV（Bird's Eye View，鸟瞰图）是 3D 检测的核心视角：自动驾驶感知的绝大多数模型（SECOND、PointPillars、CenterPoint）都在 BEV 平面上做检测。"把 3D 框投影到 BEV"有两种粒度：

- **带旋转角的 BEV**（`bev`）：`(x_center, y_center, w, h, yaw)` 共 5 个数，保留朝向信息，用于旋转 NMS、旋转 IoU。
- **无旋转的外接 BEV**（`nearest_bev`）：`(x1, y1, x2, y2)` 轴对齐矩形，用于粗过滤、画图、以及那些不关心朝向的算法（如早期 anchor 生成）。

#### 4.2.2 核心流程

`nearest_bev` 的取"近似最小外接矩形"策略：

```
bev = (x, y, w, h, r)
1. r 经 limit_period 折到 [-π/2, π/2) 后取绝对值 → 等价角 ∈ [0, π/2)
2. 若 |r| > π/4：说明框"斜着躺"，长边主要沿另一条轴 → 交换 w 和 h
3. 否则 w、h 保持不变
4. 输出 (x - w/2, y - h/2, x + w/2, y + h/2)
```

这在等价角 \( \le \pi/4 \) 时直接取 w×h（不旋转尺寸），给出的是轴对齐矩形中**较紧的一个**，因此叫 *nearest* bev；当 \( \pi/4 < |r| \le \pi/2 \) 时交换宽高后同理。它一般小于真正的旋转外接矩形，是一个偏紧的近似。

#### 4.2.3 源码精读

[base_box3d.py:L155-L159](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/base_box3d.py#L155-L159) —— `bev` 属性：直接取张量的 `[0, 1, 3, 4, 6]` 列，即 `(x, y, x_size, y_size, yaw)` 的 XYWHR 格式。整列索引返回的是新张量（拷贝），不会污染原框。

[base_box3d.py:L161-L179](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/base_box3d.py#L161-L179) —— `nearest_bev`：L168 先用 `limit_period(rotations, 0.5, np.pi)` 把 yaw 折到 \([-\pi/2, \pi/2)\) 再取绝对值得到 \([0, \pi/2)\) 的等价角；L171-L174 用 `torch.where` 按"角度是否超过 π/4"决定是否交换第 2、3 列（w/h）；L178 拼成 `(x1, y1, x2, y2)`。

支撑它的角度工具在 [utils.py:L12-L28](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/utils.py#L12-L28) —— `limit_period(val, offset, period)` 实现一行公式：

\[
\text{limited} = val - \left\lfloor \frac{val}{period} + offset \right\rfloor \cdot period
\]

把任意角折进 \([-offset \cdot period,\ (1-offset) \cdot period)\)。默认 `offset=0.5, period=π` 即折进 \([-\pi/2, \pi/2)\)。方向分类损失（区分 0° 和 180° 的车头）也依赖这个函数。

#### 4.2.4 代码实践

**实践目标**：观察同一组框从旋转 BEV 到外接 BEV 的转换。

**操作步骤**（示例代码）：

```python
import torch
from mmdet3d.structures import DepthInstance3DBoxes

b = DepthInstance3DBoxes(torch.tensor([
    [0., 0., 0., 2., 4., 1., 0.0],           # yaw = 0
    [0., 0., 0., 2., 4., 1., torch.pi / 2],  # yaw = 90°
]))
print(b.bev)          # 两行都是 [0, 0, 2, 4, r]
print(b.nearest_bev)  # yaw=0 → [-1,-2,1,2]；yaw=90° → [-2,-1,2,1]（交换宽高）
```

**需要观察的现象**：`bev` 只改了最后一列的角度；`nearest_bev` 中第二个框的宽高被交换了。

**预期结果**：第一行 `[-1, -2, 1, 2]`、第二行 `[-2, -1, 2, 1]`（由 L171-L178 的 `torch.where` 逻辑推出，待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：`in_range_bev((0, 0, 10, 10))` 对一个中心在 `(10.5, 5, 0)`、宽 4 的框返回什么？合理吗？

**答案**：返回 `False`，因为判断只看中心点坐标（[base_box3d.py:L199-L202](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/base_box3d.py#L199-L202)），中心 10.5 已超过 x_max=10。虽然框的左半部分还在范围内，但这种"中心出局即剔除"的近似在数据管线里是可接受且高效的。

**练习 2**：`limit_period(3π/2, 0.5, π)` 等于多少？

**答案**：\( \lfloor 1.5 + 0.5 \rfloor \cdot \pi = 2\pi \)，所以 \( 1.5\pi - 2\pi = -0.5\pi \)。即 270° 被折叠成 -90°，两者对"绕 z 轴的朝向"等价（差 π 的整数倍，在这个周期下等价）。

### 4.3 三个子类的 yaw 约定差异：LiDAR、CAM、Depth 对照

#### 4.3.1 概念说明

三个子类继承同一份基类，**差异全部来自坐标系的三个不同点**：

| | `LiDARInstance3DBoxes` | `CameraInstance3DBoxes` | `DepthInstance3DBoxes` |
| --- | --- | --- | --- |
| 重力轴（旋转轴）`YAW_AXIS` | 2（z） | 1（y） | 2（z） |
| 默认 origin（锚点相对位置） | `(0.5, 0.5, 0)` 底面中心 | `(0.5, 1.0, 0.5)` y 最大面中心 | `(0.5, 0.5, 0)` 底面中心 |
| `height` 取哪列 | 第 5 列 `z_size` | **第 4 列 `y_size`**（重写） | 第 5 列 `z_size` |
| `bottom_height` | `tensor[:, 2]` | **`tensor[:, 1]`**（重写） | `tensor[:, 2]` |
| `top_height` | `bottom + height` | **`bottom - height`**（y 向下，重写） | `bottom + height` |
| `bev` 取列 | `[0,1,3,4,6]` | **`[0,2,3,5,6]` 且 yaw 取反**（重写） | `[0,1,3,4,6]`（继承基类） |
| yaw=0 基准 / 增方向 | +x 起，转向 +y | +x 起，**转向 -z**（即"减少"） | +x 起，转向 +y |
| 典型场景 | KITTI/nuScenes/Waymo 训练 | 单目 3D（FCOS3D、SMOKE）、KITTI 标注 | ScanNet、SUN RGB-D |

一句话记忆：**CAM 子类的所有"特例"都源于 y 轴向下**——高度变 y、顶变低（数值上）、BEV 平面变成 x-z 平面且旋转方向反号。

#### 4.3.2 核心流程

`rotate`（LiDAR/Depth 版）的统一流程：

```
输入 angle（标量或 3×3 矩阵）与可选 points
1. 标量 angle：
     中心点 (N,3) 经 rotation_3d_in_axis(..., axis=YAW_AXIS) 绕重力轴旋转
     同时拿到旋转矩阵 rot_mat_T
   矩阵 angle：直接 tensor[:, :3] @ rot_mat_T，并反解出等效角
2. yaw += angle（朝向角同步累加）
3. 若 box_dim == 9（带速度）：速度 2 维向量也用同一个 2×2 子矩阵旋转
4. 若传入 points：用同一 rot_mat_T 旋转点（保证点云与框同步！）
```

`corners` 的统一流程（三个子类结构相同、参数不同）：

```
1. 生成 8 个单位的 0/1 角点，重排成顺时针约定顺序
   (x0y0z0, x0y0z1, x0y1z1, x0y1z0, x1y0z0, x1y0z1, x1y1z1, x1y1z0)
2. 减去锚点相对偏移：LiDAR/Depth 减 (0.5, 0.5, 0)，CAM 减 (0.5, 1, 0.5)
3. corners = dims * 偏移  → 得到框局部坐标下的 8 顶点
4. rotation_3d_in_axis 按 yaw 绕重力轴旋转
5. 加回中心 (tensor[:, :3]) → 世界坐标 8 顶点，形状 (N, 8, 3)
```

旋转矩阵本身（`rotation_3d_in_axis`，axis=2 时对行向量右乘）：

\[
R^T = \begin{bmatrix} \cos\theta & \sin\theta & 0 \\ -\sin\theta & \cos\theta & 0 \\ 0 & 0 & 1 \end{bmatrix}, \quad p' = p \, R^T
\]

对列向量这等价于标准逆时针旋转 \( R \)（+x 转向 +y），符合右手法则。

#### 4.3.3 源码精读

**LiDAR 子类**——坐标系图与 `YAW_AXIS = 2`：

[lidar_box3d.py:L13-L38](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/lidar_box3d.py#L13-L38) —— docstring 的 ASCII 图清楚标出：x 前（yaw=0）、y 左（yaw=0.5π）、z 上。L38 `YAW_AXIS = 2`。

`corners` 参考实现：

[lidar_box3d.py:L40-L80](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/lidar_box3d.py#L40-L80) —— L63-L64 处理空框；L67-L71 用 `np.unravel_index(np.arange(8), [2]*3)` 生成 8 个二进制角点并重排成顺时针序；L73 减去 `(0.5, 0.5, 0)` 把"锚点在底面中心"体现出来（z 方向不减，所以 8 顶点的 z 从 bottom 到 bottom+z_size）；L77-L79 绕 z 轴旋转后加回中心。

`rotate` 实现：

[lidar_box3d.py:L82-L137](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/lidar_box3d.py#L82-L137) —— L108-L113 标量角走 `rotation_3d_in_axis` 且 `return_mat=True`；L114-L119 传 3×3 矩阵时直接矩阵乘并从矩阵元素 `arctan2(rot_sin, rot_cos)` 反解角度；L121 `yaw += angle`；L123-L125 是速度向量（box_dim=9，第 7、8 列是 vx、vy）同步旋转；L127-L137 传入的 points 用同一个 `rot_mat_T` 旋转——**这就是数据增强里"点云和框保持刚体一致"的实现位置**。

**CAM 子类**——构造与 origin 特例：

[cam_box3d.py:L13-L91](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/cam_box3d.py#L13-L91) —— docstring 的坐标系图：z 前（yaw=-0.5π）、x 右（yaw=0）、y 下。L52 `YAW_AXIS = 1`。注意这个子类**重写了 `__init__`**：L59 默认 `origin=(0.5, 1.0, 0.5)`，L88-L91 的换算目标也是它——"底部"在 CAM 里是 y 最大的面。

高度三件套重写：

[cam_box3d.py:L93-L128](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/cam_box3d.py#L93-L128) —— `height` 取第 4 列（y_size）；`top_height = bottom_height - height`（L102 注释点明"正方向是向下而不是向上"，所以"顶"数值更小）；`bottom_height` 取第 1 列（y）；`gravity_center` 在 L127 沿 **负 y** 抬半个高度。若不重写这组属性，`in_range_3d`、`overlaps` 在相机系下会全部算错。

`local_yaw`（KITTI 的 alpha 角）：

[cam_box3d.py:L109-L119](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/cam_box3d.py#L109-L119) —— 只有相机框有此属性：\( \text{local\_yaw} = yaw - \mathrm{atan2}(x_c, z_c) \)，即观测角修正后的朝向，等价 KITTI 标注里的 alpha。实现在 [utils.py:L326-L345](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/utils.py#L326-L345)，并做了 \((-π, π]\) 的范围折叠。

BEV 与高度 IoU 的重写：

[cam_box3d.py:L173-L182](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/cam_box3d.py#L173-L182) —— `bev` 取 `[0, 2, 3, 5, 6]` 列（BEV 平面是 **x-z** 平面，尺寸取 x_size、z_size），并且 L181 把 yaw **取负**：因为 CAM 的重力轴 y 指向地心，从上往下看旋转方向与 LiDAR/Depth 相反。

[cam_box3d.py:L278-L308](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/cam_box3d.py#L278-L308) —— `height_overlaps` 重写：因为 y 向下，"更低"数值更大，所以 L304-L306 用 `min` 取两框"更下面的底"、`max` 取"更上面的顶"，再相减——与基类的 `max`/`min` 恰好互换。

**Depth 子类**：

[depth_box3d.py:L13-L38](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/depth_box3d.py#L13-L38) —— 坐标系与 LiDAR 同构（x 右、y 前、z 上，`YAW_AXIS = 2`、origin `(0.5, 0.5, 0)`），所以 `bev`、`height` 等全部直接继承基类，`corners`（[depth_box3d.py:L40-L80](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/depth_box3d.py#L40-L80)）与 LiDAR 版逐行相同。区别在 `rotate` 的 `with_yaw=False` 分支（见 4.4）和室内专用方法。

#### 4.3.4 代码实践

**实践目标**：用 `grep` 验证三个子类的 YAW_AXIS 差异，并跑通一次带点的旋转。

**操作步骤**：

1. 在仓库根目录执行（只读 git/grep，不改源码）：

```bash
grep -n "YAW_AXIS = " mmdet3d/structures/bbox_3d/*.py
grep -n "def height_overlaps" mmdet3d/structures/bbox_3d/*.py
```

2. 再跑一段最小旋转示例（示例代码）：

```python
import torch
from mmdet3d.structures import LiDARInstance3DBoxes

box = LiDARInstance3DBoxes(torch.tensor([[0., 0., 0., 2., 4., 1., 0.]]))
pts = torch.tensor([[1., 0., 0.]])  # 框前方 1m 处的一个点
box.rotate(torch.pi / 2, pts)       # 绕 z 轴逆时针转 90°
print(box.tensor)   # 预期 yaw = π/2
print(pts)          # 预期 (1,0,0) → (0,1,0)
```

**需要观察的现象**：`grep` 显示 base=0、lidar=2、cam=1、depth=2；`height_overlaps` 只在 base 和 cam 出现；旋转后点的 y 变 1、x 变 0，yaw 变 π/2。

**预期结果**：点从 +x 转到 +y（右手法则绕 +z 的正旋转），框 yaw 同步 +π/2——`rotate` 返回 `(points, rot_mat_T)` 二元组，二者使用同一矩阵，因此点与框永远刚体一致。具体打印值待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `CameraInstance3DBoxes.bev` 要把 yaw 取负，而另外两个子类不用？

**答案**：BEV 平面在 LiDAR/Depth 下是 x-y 平面、重力轴 +z 朝上；CAM 下 BEV 平面是 x-z 平面、重力轴 y **朝下**。同一个物理旋转，从"重力轴正方向"看是逆时针，从另一侧看就是顺时针，投影到各自 BEV 平面后角度符号相反，所以 CAM 的 bev 需要取负（[cam_box3d.py:L178-L181](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/cam_box3d.py#L178-L181) 的注释原话是 "the bev yaw angle needs to be reversed"）。

**练习 2**：如果不重写 `CameraInstance3DBoxes.height_overlaps`（沿用基类版本），计算 KITTI 相机系下两框的高度重叠会发生什么？

**答案**：基类假设"顶 = 底 + 高、数值越大越高"，而相机系 y 向下，"顶"数值更小。沿用基类会把 max/min 用反，重叠高度会算出负数或错误值（再 `clamp(min=0)` 后大多变 0），导致 3D IoU 失真、正负样本分配错乱。

**练习 3**：`rotate` 里 `if self.tensor.shape[1] == 9` 这一段（[lidar_box3d.py:L123-L125](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/lidar_box3d.py#L123-L125)）是在处理什么？

**答案**：nuScenes 等数据集的框带 2 个速度自由度（vx、vy，占第 7、8 列）。速度是 BEV 平面向量，旋转场景时必须用同一个 2×2 旋转子矩阵变换，否则框转了、速度方向还指着老方向。

### 4.4 DepthInstance3DBoxes 与 with_yaw=False：室内轴对齐框

#### 4.4.1 概念说明

室内数据集（ScanNet、SUN RGB-D）的家具标注通常**没有可靠朝向**——一张桌子"朝哪"没有唯一定义，ScanNet 的官方标注就是 6 维的（中心 + 尺寸，无 yaw）。这正是构造函数里 6 维特例的用武之地：补 0 当假 yaw、`with_yaw=False`。

但数据增强仍会旋转场景（`GlobalRotScaleTrans`）。一个没有 yaw 的轴对齐框旋转 30° 后**不再是轴对齐框**，而 6 维表示又存不下旋转角。mmdet3d 的解决方案：**旋转后取新的轴对齐外接矩形**（enclosing box），即只旋转 8 个顶点、再取 x/y 方向的 min-max 作为新的 x_size/y_size。框会"变大"一点点，这是表示能力换来的可接受的近似。

#### 4.4.2 核心流程

```
DepthInstance3DBoxes.rotate(angle)
├─ with_yaw=True ：与 LiDAR 完全相同（转中心、yaw += angle）
└─ with_yaw=False：
      1. corners @ rot_mat_T     # 旋转 8 顶点
      2. new_x_size = max(x) - min(x)   # 外接矩形
      3. new_y_size = max(y) - min(y)
      4. 写回 tensor[:, 3:5]，yaw 保持 0
```

#### 4.4.3 源码精读

[depth_box3d.py:L82-L133](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/depth_box3d.py#L82-L133) —— `rotate`。L108-L113 与 LiDAR 版一致的标量角分支（中心旋转、拿回 `rot_mat_T`）；L121-L122 `with_yaw=True` 时 yaw 累加；**L123-L133 是本类独有的分支**：L126 先算旋转后的 corners，L127-L133 用第 0、1 维的 max-min 覆盖 `tensor[:, 3:5]`（x_size、y_size），注释写明 "for axis-aligned boxes, we take the new enclosing axis-aligned boxes after rotation"。

[depth_box3d.py:L147-L186](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/depth_box3d.py#L147-L186) —— `flip` 也处处检查 `with_yaw`：水平翻转翻转 x 列（`0::7` 步长索引同时命中 x 坐标与 x_size！），yaw 按 `-yaw + π` 修正；无 yaw 时跳过角度修正。

[depth_box3d.py:L233-L280](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/depth_box3d.py#L233-L280) —— `get_surface_line_center`：返回每个框 6 个面中心与 12 条棱中心（绕自身 yaw 旋转后加到 gravity_center 上），是 H3DNet 回归"面/边/角基元"的支撑接口。顺带注意 L245-L250 手工构造 \( R^T \) 的写法与 `rotation_3d_in_axis` 一致。

谁在用 `with_yaw=False`？看真实调用点：

[scannet_dataset.py:L173-L177](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/scannet_dataset.py#L173-L177) —— ScanNet 数据集把 GT 标注包成 `DepthInstance3DBoxes(..., with_yaw=False, origin=(0.5, 0.5, 0.5)).convert_to(self.box_mode_3d)`：先以**几何中心** origin 构造（ScanNet 标注存的是几何中心），再按需转成 LiDAR 等其他坐标系。SUN RGB-D（[sunrgbd_dataset.py:L143](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/sunrgbd_dataset.py#L143)）与 S3DIS 同理。

#### 4.4.4 代码实践

**实践目标**：观察 `with_yaw=False` 时旋转导致的外接矩形膨胀。

**操作步骤**（示例代码）：

```python
import torch
from mmdet3d.structures import DepthInstance3DBoxes

box_a = DepthInstance3DBoxes(torch.tensor([[0., 0., 0., 2., 4., 1., 0.]]), with_yaw=True)
box_b = box_a.clone(); box_b.with_yaw = False   # 仅演示；正常应由 box_dim=6 构造
box_b.rotate(torch.pi / 4)                      # 转 45°，外接矩形最大
print(box_a.dims, box_a.yaw)   # 预期 (2,4,1)，yaw=π/4
print(box_b.dims, box_b.yaw)   # 预期 x_size=y_size=2√2≈2.83，yaw 仍 0
```

**需要观察的现象**：45° 时外接矩形达到最大（对角线长度 \( \sqrt{2^2+4^2} \) 在两轴上的投影），90° 时又缩回与原尺寸相等（只是宽高互换）。

**预期结果**：`box_b.dims` 的 x_size、y_size 约为 `(2.83, 2.83, 1)`（由 L126-L133 推出，待本地验证）。更规范的做法是直接传 6 维张量 `box_dim=6` 构造（由构造函数自动置 `with_yaw=False`），此处为对比才用赋值。

#### 4.4.5 小练习与答案

**练习 1**：`flip` 里 `self.tensor[:, 0::7]` 为什么用步长 7 的切片，而不是 `[:, 0]`？

**答案**：框张量可能不止 7 列（如带速度的 9 维框）。以 7 为周期，第 0 列（x）、第 7 列（vx）都要取负；`0::7` 一次命中所有"应该随 x 翻转"的列。同理 `1::7` 覆盖 y 与 vy。

**练习 2**：ScanNet 为什么用 `origin=(0.5, 0.5, 0.5)` 构造框？

**答案**：ScanNet 原始标注中的中心是**几何中心**（框的正中央）。mmdet3d 内部统一用底面中心存储，所以构造时声明输入 origin 是 `(0.5, 0.5, 0.5)`，构造函数按 [base_box3d.py:L77-L80](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/base_box3d.py#L77-L80) 自动把中心下移半个高度。

**练习 3**：如果把 `DepthInstance3DBoxes`（with_yaw=False）直接喂给需要朝向的 `Anchor3DHead`，会发生什么？

**答案**：`Anchor3DHead` 的 anchor 与 bbox coder（如 `DeltaXYZWLHRBBoxCoder`）都会读写第 6 列 yaw；无 yaw 框该列恒 0，方向回归目标无意义，训练会退化。这类框应配合 VoteNet 系（`PartialBinBasedBBoxCoder` 支持 `with_yaw=False`，直接回归尺寸而不回归角度）使用。

## 5. 综合实践

把本讲全部知识点串成一个可运行脚本（示例代码，保存为 `play_depth_boxes.py` 在任意目录运行，需已 `pip install -e .` 安装 mmdet3d）：

```python
import torch
from mmdet3d.structures import DepthInstance3DBoxes

# ---- 1. 构造 3 个框（DEPTH 坐标系：x 右、y 前、z 上）----
#       列含义: x, y, z, x_size, y_size, z_size, yaw
boxes = DepthInstance3DBoxes(torch.tensor([
    [0.0, 0.0, 0.0, 2.0, 4.0, 1.0, 0.0],
    [5.0, 5.0, 0.0, 1.0, 1.0, 2.0, torch.pi / 4],
    [12.0, 5.0, 0.0, 3.0, 3.0, 1.5, torch.pi / 2],   # 故意放在范围外
]))

# ---- 2. BEV 视图与范围过滤 ----
print('bev:\n', boxes.bev)               # (3, 5)：x, y, w, h, r
print('nearest_bev:\n', boxes.nearest_bev)  # (3, 4)：x1, y1, x2, y2
mask = boxes.in_range_bev((0., 0., 10., 10.))
print('in_range_bev:', mask)             # 预期 [True, True, False]

# ---- 3. 旋转前后顶点对比（验证右手系）----
before_corners = boxes.clone().corners   # (3, 8, 3)
before_yaw = boxes.yaw.clone()
angle = torch.pi / 2                     # 绕 +z 逆时针 90°
boxes.rotate(angle)
after_corners = boxes.corners
print('yaw before:', before_yaw, '\nyaw after :', boxes.yaw)  # 预期各 + π/2

# 手工验证第 0 个框：旋转前局部顶点 (-1,-2,0) 应转到 (2,-1,0)
# 逆时针 90°: (x, y) -> (-y, x)
print('corner[0, 0] before:', before_corners[0, 0])
print('corner[0, 0] after :', after_corners[0, 0], ' 期望约 (2, -1, 0)')

# ---- 4. 轴对齐变体：6 维输入自动补假 yaw ----
axis_box = DepthInstance3DBoxes(
    torch.tensor([[0., 0., 0., 2., 4., 1.]]), box_dim=6)
axis_box.rotate(torch.pi / 4)
print('axis_box dims:', axis_box.dims)   # 预期 x_size≈y_size≈2.83
```

**观察要点**：

1. `in_range_bev` 只看中心——第 3 个框中心 `(12, 5)` 超出 `x_max=10` 被剔除，尽管它尺寸只有 3m。
2. 旋转 90° 后：第 0 个框原本指向 +x 的长边（x_size=2）现在指向 +y；`corners[0, 0]` 应从 `(-1, -2, 0)` 变为 `(2, -1, 0)`，因为逆时针旋转 \((x,y) \mapsto (-y,\ x)\)（右手法则绕 +z）。
3. 第 1 个框 yaw=π/4、底面是 1×1 正方形：由于交换条件是**严格大于** π/4（[base_box3d.py:L171](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/base_box3d.py#L171)），`nearest_bev` 不交换宽高，输出 `[4.5, 4.5, 5.5, 5.5]`——这比真正的旋转外接矩形（边长 \(\sqrt{2}\)）更紧，正是 "nearest" 的含义；第 2 个框 yaw=π/2 会触发宽高交换（本例 3×3 底面，交换后看不出差别）。
4. `bev`/`nearest_bev`/`corners` 全部可微且不修改 `tensor`；`rotate` 是原地操作（无返回值时返回 None）。
5. 最后一段 6 维构造会自动补假 yaw——打印 `axis_box.box_dim` 应为 7。

具体数值输出待本地验证（以上预期均由源码逻辑推导）。

## 6. 本讲小结

- `BaseInstance3DBoxes` 是 `(N, box_dim)` 张量的包装类：每行 `(x, y, z, x_size, y_size, z_size, yaw, ...)`，默认锚点为底面中心，构造时按 `origin` 参数自动换算锚点，6 维输入自动补假 yaw 并置 `with_yaw=False`。
- 属性族（`center/bottom_center/gravity_center/dims/yaw/height/volume`）是对张量列的只读视图；`bev` 给 XYWHR 旋转框、`nearest_bev` 给轴对齐外接框（角度超过 π/4 时交换宽高）。
- `in_range_bev`/`in_range_3d` 分别按 BEV 中心点和几何中心做范围过滤，是数据管线 `ObjectRangeFilter` 等的基础。
- `overlaps` 把 3D IoU 分解为 `box_iou_rotated` 的 BEV IoU 与一维高度交集的乘积，其中用 \( I = \frac{\text{iou}(a_1+a_2)}{1+\text{iou}} \) 从 IoU 反解交集面积。
- 三个子类的差异全部源于坐标系：CAM（`YAW_AXIS=1`、origin `(0.5,1.0,0.5)`、y 向下）必须重写 `height/top/bottom_height`、`gravity_center`、`bev`（取 x-z 平面且 yaw 取负）和 `height_overlaps`（max/min 互换）；LiDAR 与 Depth 几乎同构（都绕 z 转）。
- `rotate`/`flip` 通过 `rotation_3d_in_axis` 的 `rot_mat_T` 同时变换框中心、yaw、速度列和外部传入的点云，这是数据增强"点与框同步"的实现位置；Depth 在 `with_yaw=False` 时改取旋转后顶点的轴对齐外接矩形。

## 7. 下一步学习建议

下一讲（u2-l3）将把同样的设计模式搬到点云上：`BasePoints` 及 `LiDARPoints/CameraPoints/DepthPoints` 三个子类——你会发现 `shift/rotate/flip` 与本讲的框方法一一对应（`rotate(rot_mat_T)` 正是框的 `rotate` 在 L134 调用的接口），建议对照阅读 [mmdet3d/structures/points/base_points.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/base_points.py)。

之后（u2-l4）`Det3DDataSample` 会把本讲的框对象（`gt_bboxes_3d`/`pred_instances_3d.bboxes_3d`）和点云一起打包成贯穿训练、推理、评估的统一数据契约。

如果想在真实链路里回看本讲的类，推荐两条 grep 线索：

- `grep -rn "in_range_bev" mmdet3d/datasets/` —— 找到 `ObjectRangeFilter` 与 `GTNoiseAug` 等使用者；
- `grep -rn "\.rotate(" mmdet3d/datasets/transforms/` —— 看 `GlobalRotScaleTrans` 如何把随机角同时喂给点云、3D 框和 2D 框。

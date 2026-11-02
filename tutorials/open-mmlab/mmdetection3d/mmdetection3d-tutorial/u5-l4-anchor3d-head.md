# Anchor3DHead 与 3D 锚框生成

## 1. 本讲目标

上一讲我们看到：SECONDFPN 把稀疏骨干的多尺度特征对齐、拼接，输出一张单级大通道 BEV 特征图（SECOND KITTI 配置下形状为 `(B, 512, 400, 352)`）。本讲回答的问题是：**检测头如何把这张特征图变成 3D 框？**

学完本讲你应该能够：

1. 手算一个配置下 anchor 的总数量，并说清 `Anchor3DRangeGenerator` 的 `ranges / sizes / rotations / scales` 各参数如何参与生成；
2. 写出 `DeltaXYZWLHRBBoxCoder` 的 encode/decode 公式，解释「底面中心换几何中心」「对角线归一化」「log 尺寸比」三个设计；
3. 说出 Anchor3DHead 三组 1×1 卷积分支（分类 / 回归 / 方向）的输出通道数公式与各自语义；
4. 解释两个角度处理技巧的动机：`diff_rad_by_sin` 的 sin 差回归（解决 \(2\pi\) 周期）与方向二分类（解决 \(\pi\) 歧义），以及推理阶段如何用 `dir_offset` 把二者拼回正确的 yaw。

## 2. 前置知识

- **7 自由度 3D 框**（承接 u2-l2）：mmdet3d 的 `LiDARInstance3DBoxes` 每行是 `(x, y, z, x_size, y_size, z_size, yaw)`，默认锚点是**底面中心**（见 [mmdet3d/structures/bbox_3d/lidar_box3d.py:L26-L34](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/lidar_box3d.py#L26-L34) 的说明），yaw 绕 z 轴、从 +x 方向朝 +y 方向增大。
- **anchor（锚框）范式**：在 2D 检测（Faster R-CNN、SSD、YOLO 系列）中，网络不直接回归框的绝对坐标，而是在每个特征图格点上预设若干「参考框」（anchor），网络只学**anchor 到真值的差值**。3D 检测沿用这一思路，只是参考框从 4 维 \((x, y, w, h)\) 变成 7 维。
- **特征图分辨率与物理尺度的换算**（承接 u5-l1/u5-l3）：SECOND KITTI 的 `point_cloud_range = [0, -40, -3, 70.4, 40, 1]`、`voxel_size = [0.05, 0.05, 0.1]`，稀疏形状为 \([41, 1600, 1408]\)（z, y, x 顺序）；`SparseEncoder` 下采样 4 倍得到 \(400 \times 352\) 的 BEV 特征，SECONDFPN 把两级特征都上采样对齐到 \(400 \times 352\)。特征图上 1 格 = 物理世界 \(0.05 \times 4 = 0.2\) 米。
- **一个术语澄清**：配置与文档里常说 head 输出「热图」，本讲的分类分支输出的是**逐 anchor 的类别得分图**（BEV 网格上每个位置、每个 anchor 一个分数）；CenterPoint 那种以高斯核构造的 center heatmap 是下一讲（u5-l5）的内容。Anchor3DHead 也没有独立的「高度分支」——高度信息 \((z, h)\) 就包含在 7 维回归量里。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [mmdet3d/models/task_modules/anchor/anchor_3d_generator.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/anchor/anchor_3d_generator.py#L11-L12) | 3D 锚框生成器：`Anchor3DRangeGenerator` 及两个变体（Aligned、PerCls） |
| [mmdet3d/models/task_modules/coders/delta_xyzwhlr_bbox_coder.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/delta_xyzwhlr_bbox_coder.py#L9-L10) | anchor 与真值框之间的差值编码/解码器 |
| [mmdet3d/models/dense_heads/anchor3d_head.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/anchor3d_head.py#L22-L23) | 本讲主角：anchor-based 3D 检测头，SECOND/PointPillars/MVXNet/PartA2 共用 |
| [mmdet3d/models/dense_heads/base_3d_dense_head.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/base_3d_dense_head.py#L19) | 父类：predict_by_feat 的推理后处理（NMS + 方向修正）在这里 |
| [mmdet3d/models/dense_heads/train_mixins.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/train_mixins.py#L10-L11) | 训练侧 mixin：anchor 目标分配（`anchor_target_3d`）与方向目标生成 |
| [mmdet3d/structures/bbox_3d/utils.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/utils.py#L13-L28) | `limit_period`：把角度折叠进一个周期区间的工具函数 |
| [configs/_base_/models/second_hv_secfpn_kitti.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/second_hv_secfpn_kitti.py#L30-L58) | 本讲的配置依据：SECOND KITTI 的 bbox_head / train_cfg / test_cfg |

三者关系一句话：**生成器负责「在哪里放多少个参考框」，编码器负责「参考框到真值差多少」，头负责「用卷积把特征翻译成类别分数和这些差值」**。

## 4. 核心概念与源码讲解

### 4.1 Anchor3DRangeGenerator：在 BEV 平面上铺锚框

#### 4.1.1 概念说明

anchor 的本质是一组**先验假设**：「目标大概率出现在感知范围内的某个格点附近，且尺寸接近某个统计值、朝向接近某个方向」。生成器把这三类先验做笛卡尔积：

- **位置先验**：`ranges` 给出 \((x_{\min}, y_{\min}, z_{\min}, x_{\max}, y_{\max}, z_{\max})\)，在 BEV 网格上逐格放中心；
- **尺寸先验**：`sizes` 每项是一个 \((x\_size, y\_size, z\_size)\)，通常取数据集各类目标的平均尺寸。KITTI 三类（Pedestrian / Cyclist / Car）的平均尺寸分别是 `[0.8, 0.6, 1.73]`、`[1.76, 0.6, 1.73]`、`[3.9, 1.6, 1.56]`——正是 SECOND 配置里的值；
- **朝向先验**：`rotations` 默认 `[0, 1.57]`，即每格放「朝 x」和「朝 y」两个朝向（1.57 ≈ \(\pi/2\)）。

为什么 3D 检测特别需要按尺寸放 anchor？因为行人（0.8m）和汽车（3.9m）尺寸差近 5 倍，单一尺寸的 anchor 会让回归量方差巨大、IoU 分配失真。类注释也点明了这一惯例（见 [anchor_3d_generator.py:L16-L19](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/anchor/anchor_3d_generator.py#L16-L19)）。

`scales` 与 2D FPN 的多尺度 anchor 对应，但 3D 场景几乎不用（默认 `[1]`，即单尺度、`num_levels == 1`）；`custom_values` 用于给 anchor 附加额外维度（如 nuScenes 的两个速度维，拼成 9 维框）。

#### 4.1.2 核心流程

生成链路是三层递归：

```text
grid_anchors(featmap_sizes)                 # 逐特征层
  └─> single_level_grid_anchors(size, scale)  # 逐 (range, size) 组
        └─> anchors_single_range(...)           # 真正干活的一个
```

`anchors_single_range` 的算法：

1. 二维特征尺寸补 z 维：`(H, W) -> (1, H, W)`；
2. 用 `torch.linspace` 在 range 内均匀生成 \(x\)（W 个）、\(y\)（H 个）、\(z\)（D 个，通常 1 个）中心坐标；
3. `torch.meshgrid(x, y, z, rotations)` 造出所有组合；
4. 把尺寸张量 broadcast 到同样的形状，插到第 4 个位置；
5. `torch.cat` 后 `permute([2, 1, 0, 3, 4, 5])` 把坐标顺序从 (x, y, z, …) 转成 (z, y, x, …)，即 `(D, H, W, num_sizes, num_rots, 7)`；
6. 若有 `custom_values`，再拼上对应数量的零向量（nuScenes 9 维框）。

关键计数公式：

\[ N_{anchor} = H \times W \times \underbrace{|\texttt{sizes}|}_{num\_size} \times \underbrace{|\texttt{rotations}|}_{num\_rot} \]

`num_base_anchors`（每格的 anchor 数）定义为 \(num\_size \times num\_rot\)，见 [anchor_3d_generator.py:L80-L85](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/anchor/anchor_3d_generator.py#L80-L85)。SECOND KITTI 下每格 \(3 \times 2 = 6\) 个，全图 \(400 \times 352 \times 6 = 844800\) 个。

#### 4.1.3 源码精读

**构造与参数校验**（[anchor_3d_generator.py:L42-L68](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/anchor/anchor_3d_generator.py#L42-L68)）：`size_per_range=True` 时每个 size 配一个专属 range——若只给 1 个 range 但有 N 个 size，range 会被复制 N 份。SECOND 配置显式写了 3 个 range：行人与骑行者共享 \(-0.6\)（离地 0.6 米）的底面 z，汽车是 \(-1.78\)（底面在地下 1.78 米处，加上 1.56 的车高正好覆盖地面上的车体）。注意 anchor 的 z 语义是**底面中心**，与 `LiDARInstance3DBoxes` 的锚点约定一致。

**网格生成的核心**（[anchor_3d_generator.py:L196-L223](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/anchor/anchor_3d_generator.py#L196-L223)）：

```python
z_centers = torch.linspace(anchor_range[2], anchor_range[5], feature_size[0], ...)
y_centers = torch.linspace(anchor_range[1], anchor_range[4], feature_size[1], ...)
x_centers = torch.linspace(anchor_range[0], anchor_range[3], feature_size[2], ...)
...
rets = torch.meshgrid(x_centers, y_centers, z_centers, rotations)
...
ret = torch.cat(rets, dim=-1).permute([2, 1, 0, 3, 4, 5])
# [1, 200, 176, N, 2, 7] for kitti after permute
```

源码注释里那份 benchmark 值得一读（[L139-L142](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/anchor/anchor_3d_generator.py#L139-L142)）：同一逻辑用 torch 实现比 numpy 快约 5 倍，因为生成器每次训练迭代都要在 GPU 上调用。

**一个隐蔽的错位问题与 Aligned 变体**：`linspace(min, max, W)` 的相邻中心间距是 \(\frac{x_{\max}-x_{\min}}{W-1}\)，而特征图每格的真实物理宽度是 \(\frac{x_{\max}-x_{\min}}{W}\)——分母差 1，导致 anchor 中心并不落在体素格中心，且末端 anchor 会越出 range。`AlignedAnchor3DRangeGenerator` 修复了这一点：用 `feature_size + 1` 个格点取格中心（`align_corner=False` 时再平移半格），见 [anchor_3d_generator.py:L296-L329](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/anchor/anchor_3d_generator.py#L296-L329)。类 docstring 对「为什么对齐」有完整说明（[L242-L254](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/anchor/anchor_3d_generator.py#L242-L254)）。KITTI 的 SECOND 配置仍用非对齐版（沿用原始 SECOND 实现），nuScenes 上的 anchor 类检测器（如 SSN）则多用 Aligned 版。

**reshape_out 与 PerCls 变体**：`grid_anchors` 在 `reshape_out=True` 时把输出压平成 `(N, 7)`；SECOND KITTI 特意设 `reshape_out=False`，保留 `(1, H, W, 3, 2, 7)` 结构——因为它的 train_cfg 给了**一个长度为 3 的 assigner 列表**，训练时需要沿 size 维 `anchors[..., i, :, :]` 切出第 i 类尺寸的 anchor 交给第 i 个 assigner（详见 4.3.3）。第三个变体 `AlignedAnchor3DRangeGeneratorPerCls`（[L355-L438](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/anchor/anchor_3d_generator.py#L355-L438)）更进一步：不同类别的特征图尺寸都可以不同。

**custom_values**（[anchor_3d_generator.py:L226-L232](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/anchor/anchor_3d_generator.py#L226-L232)）：nuScenes 的 9 维框在 anchor 侧直接拼零向量初始化（速度先验设为 0）。

#### 4.1.4 代码实践

**实践目标**：脱离配置文件，直接实例化生成器，亲手数一遍 anchor。

**操作步骤**（示例代码，非项目原有代码）：

```python
import torch
from mmdet3d.models.task_modules.anchor import Anchor3DRangeGenerator

gen = Anchor3DRangeGenerator(
    ranges=[
        [0, -40.0, -0.6, 70.4, 40.0, -0.6],
        [0, -40.0, -0.6, 70.4, 40.0, -0.6],
        [0, -40.0, -1.78, 70.4, 40.0, -1.78]],
    sizes=[[0.8, 0.6, 1.73], [1.76, 0.6, 1.73], [3.9, 1.6, 1.56]],
    rotations=[0, 1.57],
    reshape_out=False)          # 与 SECOND KITTI 配置一致

# 注意：device 默认是 'cuda'，CPU 环境必须显式传 'cpu'
anchors = gen.grid_anchors([(400, 352)], device='cpu')
print(len(anchors), anchors[0].shape)        # 1, torch.Size([1, 400, 352, 3, 2, 7])
print(anchors[0].numel() // 7)               # 844800
print(gen.num_base_anchors)                  # 6
print(anchors[0][0, 0, 0, 2, 0])             # 第 (0,0) 格、Car、rot=0 的 anchor 七个分量
```

**需要观察的现象**：

1. 输出形状 `[1, 400, 352, 3, 2, 7]`：D=1、H=400（y 方向）、W=352（x 方向）、3 个尺寸、2 个朝向、7 维框；
2. 打印出的单个 anchor 形如 `(0.0, -40.0, -1.78, 3.9, 1.6, 1.56, 0.0)`——中心在 range 左下角、z 是底面高度、yaw 为 0；
3. 第 (0,0) 格、rot=1 的那个 anchor 第 7 维应为 1.57。

**预期结果**：anchor 总数 = \(400 \times 352 \times 3 \times 2 = 844800\)，与 4.3 中卷积输出的空间元素数严格相等（每个 anchor 元素对应特征图上一个通道组）。linspace 间距为 \(70.4 / 351 \approx 0.2006\) 米（可用 `torch.diff` 验证），略偏离特征图格宽 0.2 米——这就是 4.1.3 说的错位。

**待本地验证**：以上输出基于源码推导，需在本机装好 mmdet3d 依赖链（承接 u1-l2）后实际运行确认。

#### 4.1.5 小练习与答案

**练习 1**：若把 `rotations` 改成 `[0]`（单朝向），anchor 总数和 `num_base_anchors` 各是多少？

答案：`num_base_anchors = 3 × 1 = 3`，总数 \(400 \times 352 \times 3 = 422400\)，减半。

**练习 2**：PointPillars KITTI 配置的感知范围是 `[0, -39.68, -1.78, 69.12, 39.68, -1.78]`、pillar 尺寸 0.16 米，单类 Car。它的 SECONDFPN 输出特征图是 \(248 \times 216\)（y × x），anchor 有多少个？

答案：pillar 化后伪图像 \(496 \times 432\)，FPN 对齐后单级 \(248 \times 216\)；单尺寸 × 双朝向 → 每格 2 个，共 \(248 \times 216 \times 2 = 107136\) 个。

**练习 3**：为什么 `Anchor3DHead.__init__` 里说「In 3D detection, the anchor stride is connected with anchor size」（[anchor3d_head.py:L100-L101](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/anchor3d_head.py#L100-L101)）？

答案：2D FPN 中不同层用不同 stride 配不同尺寸；3D 的 BEV 特征通常是单级的（SECONDFPN 已把多尺度拼成一级），尺寸差异改由「同一格内多个 size」表达，即 stride 固定、每格 anchor 数 = sizes × rotations，因此 head 用 `num_base_anchors`（而非层数）来推卷积通道数。

### 4.2 DeltaXYZWLHRBBoxCoder：anchor 到真值的差值编码

#### 4.2.1 概念说明

coder 回答「网络到底回归什么」。直接回归 7 个绝对量的问题在于量纲失衡：\(x\) 动辄几十米、yaw 只有 \([-3.14, 3.14]\)，L1/SmoothL1 损失会被大数值维度主导。差值编码让回归目标量纲统一、且对先验「修正」而非从头预测。

编码公式（下标 \(a\) 为 anchor、\(g\) 为真值；源码变量名 `wa/la/ha` 依次对应张量的 x_size/y_size/z_size 三列）：

\[ d_a = \sqrt{l_a^2 + w_a^2},\qquad
t_x = \frac{x_g - x_a}{d_a},\quad t_y = \frac{y_g - y_a}{d_a},\quad t_z = \frac{(z_g + \tfrac{h_g}{2}) - (z_a + \tfrac{h_a}{2})}{h_a} \]

\[ t_w = \log\frac{w_g}{w_a},\quad t_l = \log\frac{l_g}{l_a},\quad t_h = \log\frac{h_g}{h_a},\qquad t_r = r_g - r_a \]

三个设计动机：

1. **底面中心 → 几何中心**：\(z + h/2\) 把两个框都先换成几何中心再作差，消除「anchor 与真值锚点相同但高度不同」带来的伪偏差；解码后再减回去；
2. **对角线 / 高度归一化**：位移差除以 \(d_a\)（BEV 对角线）或 \(h_a\)，让大目标容忍更大的绝对误差——同等 \(t_x\) 对应同等的「相对偏差」；
3. **log 尺寸比**：尺寸回归变成比率，\(\pm 0.1\) 对应放大/缩小约 10%，正负对称。

角度 \(t_r = r_g - r_a\) 看似朴素，但它的周期性问题（差 \(2\pi\) 的角度物理等价却数值差大）正是 4.4 要解决的第一宗罪。若框维数大于 7（如 nuScenes 9 维），附加维（速度等）直接做差追加在末尾（[delta_xyzwhlr_bbox_coder.py:L37-L42](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/delta_xyzwhlr_bbox_coder.py#L37-L42)）。

#### 4.2.2 核心流程

- **训练**：对每个正样本 anchor，`encode(anchor, gt)` 得到 7 维回归目标；
- **推理**：网络输出 7 维预测 `delta`，`decode(anchor, delta)` 反解出框：

\[ x_g = t_x \cdot d_a + x_a,\quad z_g = t_z \cdot h_a + (z_a + \tfrac{h_a}{2}) - \tfrac{h_g}{2},\quad l_g = e^{t_l} \cdot l_a,\quad r_g = t_r + r_a,\ \ldots \]

encode 与 decode 严格互逆（恒等往返）。

#### 4.2.3 源码精读

**encode**（[delta_xyzwhlr_bbox_coder.py:L21-L56](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/delta_xyzwhlr_bbox_coder.py#L21-L56)）：

```python
za = za + ha / 2
zg = zg + hg / 2
diagonal = torch.sqrt(la**2 + wa**2)
xt = (xg - xa) / diagonal
zt = (zg - za) / ha
lt = torch.log(lg / la)
...
rt = rg - ra
```

注意两点：`box_ndim > 7` 时拆出附加维做差（`cts = [g - a for ...]`）；返回顺序是 `[xt, yt, zt, wt, lt, ht, rt, *cts]`。

**decode**（[delta_xyzwhlr_bbox_coder.py:L58-L92](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/delta_xyzwhlr_bbox_coder.py#L58-L92)）：与 encode 镜像，最后 `zg = zg - hg / 2` 把几何中心换回底面中心（[L90](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/delta_xyzwhlr_bbox_coder.py#L90)）——因为下游的 `LiDARInstance3DBoxes` 约定底面中心锚点。

**调用位置**：训练侧在 [train_mixins.py:L294-L295](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/train_mixins.py#L294-L295)（`self.bbox_coder.encode(pos_bboxes, pos_gt_bboxes)`），推理侧在 [base_3d_dense_head.py:L339](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/base_3d_dense_head.py#L339)（`self.bbox_coder.decode(priors, bbox_pred)`）。

#### 4.2.4 代码实践

**实践目标**：验证 encode/decode 恒等往返，并亲眼确认「z 是底面中心」。

**操作步骤**（示例代码）：

```python
import torch
from mmdet3d.models.task_modules.coders import DeltaXYZWLHRBBoxCoder

coder = DeltaXYZWLHRBBoxCoder()
anchor = torch.tensor([[10., -5., -1.78, 3.9, 1.6, 1.56, 0.]])
gt     = torch.tensor([[11.2, -5.4, -1.5, 4.2, 1.7, 1.6, 0.4]])

delta = coder.encode(anchor, gt)      # 7 维回归目标
back  = coder.decode(anchor, delta)   # 反解
print(delta)                          # 注意 t_z 用的是两框几何中心之差 / anchor 高
print(back - gt)                      # 应接近全 0（浮点误差 ~1e-6）
print(coder.decode(anchor, torch.zeros(1, 7)) - anchor)   # 全 0 —— 零偏移即 anchor 本身
```

**需要观察的现象**：`delta` 各分量都在个位数级别（如 \(t_x \approx 0.27\)）；`back - gt` 几乎为零向量。

**预期结果**：往返误差在 \(10^{-6}\) 量级以内；零 delta 解码回 anchor 原值——这正是「网络学的是修正量」的直接体现。

**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：anchor 与真值尺寸完全相同（\(l_g = l_a\) 等）时 \(t_l, t_w, t_h\) 是多少？尺寸差 10% 时呢？

答案：相同时全为 \(\log 1 = 0\)；差 10% 时为 \(\log 1.1 \approx 0.0953\) 或 \(\log 0.9 \approx -0.105\)，正负幅度近似对称——这就是 log 编码的意义。

**练习 2**：为什么 \(t_z\) 的分母是 \(h_a\) 而 \(t_x, t_y\) 的分母是 BEV 对角线 \(d_a\)？

答案：各自消除对应方向的尺度依赖：\(t_z\) 只关心垂直方向（除以高度），\(t_x, t_y\) 在水平面内可朝任意方向偏移，用对角线做各向同性的归一化（对旋转不变的量级估计）。

**练习 3**：`code_size` 这个属性在 head 里被用来做什么？

答案：见 [anchor3d_head.py:L104](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/anchor3d_head.py#L104) 与 [L148-L149](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/anchor3d_head.py#L148-L149)：回归分支的输出通道数 = `num_anchors × box_code_size`，且推理时按 `(-1, self.box_code_size)` reshape 预测；nuScenes 上 `code_size` 会是 9。

### 4.3 Anchor3DHead：三组 1×1 卷积分支与训练目标

#### 4.3.1 概念说明

Anchor3DHead 是 SECOND、PointPillars、MVXNet、Part-A2（RPN 部分）共用的检测头（类 docstring 见 [anchor3d_head.py:L22-L24](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/anchor3d_head.py#L22-L24)）。它的网络部分出奇地简单：**三个 1×1 卷积**，分别输出：

| 分支 | 卷积 | 输出通道 | 语义 |
| --- | --- | --- | --- |
| 分类 | `conv_cls` | \(num\_anchors \times num\_classes\) | 每个 anchor 对各类别的分数（sigmoid，逐 BEV 格的「得分图」） |
| 回归 | `conv_reg` | \(num\_anchors \times box\_code\_size\) | 每个 anchor 的 7（或 9）维差值预测 |
| 方向 | `conv_dir_cls` | \(num\_anchors \times 2\) | 每个 anchor 的朝向二分类 logit |

SECOND KITTI 下的具体数字：\(6 \times 3 = 18\)、\(6 \times 7 = 42\)、\(6 \times 2 = 12\)。**通道维是 anchor 在前、类别在后**（anchor-major），这与特征图空间维 (H, W) 及 anchor 的 (size, rot) 展平顺序严格对齐——reshape 后第 \(i\) 行恰好是第 \(i\) 个 anchor。

一个容易忽略的联动：分类损失是否用 sigmoid 决定了两件事（[anchor3d_head.py:L107-L115](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/anchor3d_head.py#L107-L115)）——`use_sigmoid=False` 时 `num_classes += 1`（softmax 需要显式背景通道）；损失是 FocalLoss/GHMC 时 `sampling=False`，改用 `PseudoSampler`（不做难例重采样，正负样本全保留，靠 Focal 本身压制简单负样本）。SECOND 配置用 `mmdet.FocalLoss` + sigmoid，所以走的是「无采样器 + 背景隐式」路线；训练目标里背景标签被写成 `num_classes`（见 [train_mixins.py:L292](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/train_mixins.py#L292) 的 `labels += num_classes`），由 mmdet 的 FocalLoss 按背景处理。

#### 4.3.2 核心流程

**训练**：

```text
neck 输出 [(B,512,400,352)]
  └─ forward: 三个 1×1 conv → cls (B,18,·,·) / reg (B,42,·,·) / dir (B,12,·,·)
  └─ loss_by_feat:
       ① get_anchors: 按 featmap_sizes 生成 anchors（batch 共享一份）
       ② anchor_target_3d (train_mixins)
            anchor_target_3d_single:   # 列表 assigner → 按 size 逐类分配
              anchors[..., i, :, :] → 第 i 个 Max3DIoUAssigner.assign
              → PseudoSampler.sample → pos/neg 索引
              encode(pos_anchor, pos_gt) → bbox_targets
              get_direction_target(...) → dir_targets (0/1)
       ③ _loss_by_feat_single × 每层: Focal(分类) + SmoothL1(回归,仅正样本,可 sin 差) + CE(方向,仅正样本)
```

**推理**（实现在父类 `Base3DDenseHead.predict_by_feat`，本头未重写）：

```text
三分支输出 → 逐层 (H,W,C) 展平对齐 anchor → sigmoid 分数
  → nms_pre 截断（按最大类分数 topK，SECOND 配置 = 100）
  → bbox_coder.decode(anchor, delta) → (N,7) 框
  → bev 投影转 (x1,y1,x2,y2,ry) → box3d_multiclass_nms
  → 方向修正（4.4）→ LiDARInstance3DBoxes 打包进 InstanceData
```

#### 4.3.3 源码精读

**`__init__` 的构建顺序**（[anchor3d_head.py:L98-L118](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/anchor3d_head.py#L98-L118)）：先经 `TASK_UTILS` 注册节点 build 出 anchor 生成器与 bbox coder（u4-l1 讲过的注册器路由），由生成器反推 `num_anchors`，再 build 三个损失、初始化层与 assigner/sampler。默认参数中的 `anchor_generator`（[L55-L62](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/anchor3d_head.py#L55-L62)）与 PointPillars KITTI 单类配置一致，可作快速复现的默认值。

**三个 1×1 卷积**（[anchor3d_head.py:L144-L152](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/anchor3d_head.py#L144-L152)）：

```python
self.cls_out_channels = self.num_anchors * self.num_classes
self.conv_cls = nn.Conv2d(self.feat_channels, self.cls_out_channels, 1)
self.conv_reg = nn.Conv2d(self.feat_channels,
                          self.num_anchors * self.box_code_size, 1)
if self.use_direction_classifier:
    self.conv_dir_cls = nn.Conv2d(self.feat_channels,
                                  self.num_anchors * 2, 1)
```

**forward 与 multi_apply**（[anchor3d_head.py:L154-L198](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/anchor3d_head.py#L154-L198)）：`forward` 用 `multi_apply(self.forward_single, x)` 对多级特征逐层前向（borrow 自 mmdet，u6-l5 的单目头同样用这一套）。SECOND 的 neck 只输出一级，所以每个输出列表长度为 1。

**get_anchors**（[anchor3d_head.py:L221-L242](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/anchor3d_head.py#L221-L242)）：所有样本共享同一份 anchor（BEV 特征与感知范围对整个 batch 相同），直接按帧复制引用。

**列表 assigner 的分组分配**（[train_mixins.py:L120-L146](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/train_mixins.py#L120-L146)）：进入该分支的条件是「assigner 是列表、anchors 不是列表」——正是 `reshape_out=False` 的产物。第 i 个 assigner 拿第 i 个尺寸的 anchor：

```python
current_anchors = anchors[..., i, :, :].reshape(-1, self.box_code_size)
```

SECOND 配置三个 assigner 的 IoU 阈值不同（[second_hv_secfpn_kitti.py:L60-L86](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/second_hv_secfpn_kitti.py#L60-L86)）：Pedestrian/Cyclist 用 0.35/0.2，Car 用 0.6/0.45——小目标 IoU 普遍偏低，阈值随之放松。注意 `assign_per_class=False`（默认）：每个 assigner 仍看全部真值，靠 anchor 尺寸差异天然对齐类别；设为 True 才会显式按 `labels_3d == i` 过滤真值。

**单 assigner 的目标生成**（[train_mixins.py:L240-L320](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/train_mixins.py#L240-L320)）：初始化全零/全背景标签 → assign → sample → 对正样本写 `encode` 目标、方向目标、类别标签与三组权重（[L293-L315](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/train_mixins.py#L293-L315)）。`bbox_weights[pos_inds, :] = 1.0` 意味着**回归与方向损失只对正样本计算**。

**三损失计算**（[anchor3d_head.py:L244-L330](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/anchor3d_head.py#L244-L330)）：先把 `(B,C,H,W)` permute 成 `(B,H,W,C)` 再展平——保证通道顺序与 anchor 展平顺序一致；`train_cfg.code_weight` 可对 7 维差值逐维加权（SECOND 未配置）；`diff_rad_by_sin=True` 时在算回归损失前调用 4.4 的 `add_sin_difference`（[L303-L315](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/anchor3d_head.py#L303-L315)）。整个损失在 `amp.autocast(enabled=False)` 中以 float32 计算（[L414-L419](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/anchor3d_head.py#L414-L419)），混合精度下避免损失数值不稳。

#### 4.3.4 代码实践

**实践目标**：实例化 Anchor3DHead，用伪特征跑 forward，核对三个分支的通道数与 anchor 总数的对应关系。

**操作步骤**（示例代码；思路与 [single_stage.py:L50-L52](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/single_stage.py#L50-L52) 中检测器把 model 级 train_cfg 注入 bbox_head 的做法一致）：

```python
import torch
from mmengine.config import Config
from mmdet3d.registry import MODELS

cfg = Config.fromfile(
    'configs/_base_/models/second_hv_secfpn_kitti.py')
head_cfg = cfg.model.bbox_head                 # 已是 ConfigDict
head_cfg.update(train_cfg=cfg.model.train_cfg) # 模拟 SingleStage3DDetector 的注入
head = MODELS.build(head_cfg)

# 1) 通道数验证：小特征图即可
feat = torch.randn(1, 512, 200, 176)
cls_scores, bbox_preds, dir_preds = head([feat])
print(cls_scores[0].shape)   # (1, 18, 200, 176)   6 anchors × 3 classes
print(bbox_preds[0].shape)   # (1, 42, 200, 176)   6 × 7
print(dir_preds[0].shape)    # (1, 12, 200, 176)   6 × 2

# 2) anchor 总数验证：真实 SECOND 特征图尺寸
anchors = head.prior_generator.grid_anchors([(400, 352)], device='cpu')
n = anchors[0].numel() // head.box_code_size
print(n, 400 * 352 * head.prior_generator.num_base_anchors)  # 844800 844800

# 3) 训练侧属性检查
print(len(head.bbox_assigner))            # 3 个 assigner（列表）
print(type(head.bbox_sampler).__name__)   # PseudoSampler（FocalLoss → sampling=False）
```

**需要观察的现象**：

1. 三个分支空间尺寸与输入特征一致，通道数 18/42/12；
2. anchor 数量、特征图元素数、通道组数三者自洽：\(844800 = 400 \times 352 \times 6\)，每格 6 个 anchor 各占一个通道组；
3. `head(feat)` 是 `forward` 而非 loss——要跑通 `loss` 还需构造带 `gt_instances_3d` 的 `Det3DDataSample` 列表（可回看 u2-l4/u3-l8 的手工构造法）。

**预期结果**：第 2 步两个数字完全相等；若把 `sizes` 减为 2 组（同时 ranges 减为 2 个），通道数应变为 12/28/8。

**待本地验证**：CPU 上可跑，但 `(1,512,400,352)` 的随机特征约占 280MB 内存，建议 forward 用小图、anchor 核算用大图。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `conv_reg` 的输出通道是 `num_anchors × box_code_size` 而不是 `num_classes × box_code_size`？

答案：anchor 范式里每个 anchor 只负责一个（组）尺寸先验，尺寸回归以 anchor 自身为基准；同一格内不同 anchor 共享中心先验但各有独立的回归通道。类别的区分交给分类分支按 anchor 尺寸先验对应类别学习，无需每类一套回归。

**练习 2**：把损失换成 `mmdet.CrossEntropyLoss`（use_sigmoid=False）后，构建 head 时哪两个行为会变？

答案：`num_classes += 1`（softmax 显式背景通道，见 [anchor3d_head.py:L111-L112](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/anchor3d_head.py#L111-L112)），且 `sampling=True` 要求 train_cfg 里必须提供 sampler（[L133-L134](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/anchor3d_head.py#L133-L134)），否则构建时报键错误。

**练习 3**：训练与推理阶段 anchor 是各生成一次还是共享？

答案：都是**每次前向按当前特征图尺寸现算**（训练在 [loss_by_feat:L390-L394](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/anchor3d_head.py#L390-L394)，推理在 [predict_by_feat:L231-L233](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/base_3d_dense_head.py#L231-L233)）；同一 batch 内各帧共享（BEV 尺寸一致）。这呼应 u3-l7 的结论：数据管线里不存在 anchor，粘贴新 GT 无需修复对应关系。

### 4.4 方向分类与 sin 差回归：角度的两大病

#### 4.4.1 概念说明

角度回归有两宗罪，各用一个技巧对症下药：

**第一宗罪：\(2\pi\) 周期。** SmoothL1 把 \(r\) 当普通数值惩罚。设 anchor yaw 为 0、真值 yaw 为 \(3.13\) 与 \(-3.15\)——物理上只差 0.06 弧度（几乎同向），数值差却有 6.28。直接回归会让损失面在 \(\pm\pi\) 处撕裂。

**解法（diff_rad_by_sin）**：让回归损失作用在**正弦差**上。利用恒等式 \(\sin(p - g) = \sin p \cos g - \cos p \sin g\)，把「预测角与真值角」改写成两个新标量后再做 SmoothL1：

\[ \hat{r}_p = \sin r_p \cos r_g,\qquad \hat{r}_g = \cos r_p \sin r_g \]

两者之差恰为 \(\sin(r_p - r_g)\)——天然有界 \([-1, 1]\)、对 \(2\pi\) 周期不变（\(p\) 与 \(p + 2\pi\) 的 \(\hat r_p\) 相同），彻底消除撕裂。

**第二宗罪：\(\pi\) 歧义（头尾不分）。** \(\sin(p - g)\) 对「差 \(\pi\)」的两个角度取值为零：一个矩形框旋转 \(\pi\) 且 \(x\_size, y\_size\) 互换后是**同一个矩形**，但对一辆车来说这意味着「车头调转」。回归分支经 sin 差只对「朝向哪条轴线」敏感，无法区分 0 与 \(\pi\)——同一几何框的两个朝向。

**解法（方向二分类）**：额外加一个 2 类分支，判断目标朝向落在 anchor 朝向的「正半周」还是「反半周」，推理时据此给 yaw 加 \(\pi\) 修正。

两个 `dir` 参数的语义（源码标注了将并入 coder 的历史包袱，见 [anchor3d_head.py:L92-L96](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/anchor3d_head.py#L92-L96)）：

- `dir_offset`（默认 \(-\pi/2\)）：二分类**分界面的角度**。`dir_offset=-π/2` 时分界在朝向 \(\pm\pi/2\)（y 方向）处，类别 0 = 朝向大致沿 +x，类别 1 = 大致沿 −x。KITTI 车辆几乎都沿 x 轴行驶，分界面被挪到朝向分布最稀疏的角度，避免大量真值落在边界上来回抖动；
- `dir_limit_offset`：`limit_period` 的区间偏移参数，默认 0。

#### 4.4.2 核心流程

**训练侧生成方向目标**（`get_direction_target`）：

```text
rot_gt = t_r + r_a                          # 编码差 + anchor 角 = 真值绝对 yaw
offset_rot = limit_period(rot_gt - dir_offset, dir_limit_offset, 2π)   # 折进 [0, 2π)
dir_target = floor(offset_rot / π)          # num_bins=2 → 0 或 1
```

其中 `limit_period(val, offset, period)` 的实现是 `val - floor(val/period + offset) * period`（[utils.py:L13-L28](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/utils.py#L13-L28)），把任意角度映射进一个周期长的半开区间。

**推理侧方向修正**（父类 `_predict_by_feat_single` 末尾）：

```text
dir_rot = limit_period(yaw - dir_offset, 0, π)     # π 周期折叠（sin 回归只保证 mod π 正确）
yaw = dir_rot + dir_offset + π × dir_score         # 方向分类决定是否翻转 π
```

即：回归给出「沿哪条轴」(\(\bmod\ \pi\) 意义下的角)，分类给出「正方向还是反方向」，两者相加复原完整 yaw。NMS 使用的 `dir_score` 会随框一同传入（`box3d_multiclass_nms` 的 `mlvl_dir_scores` 参数，u5-l7 详讲）。

#### 4.4.3 源码精读

**add_sin_difference**（[anchor3d_head.py:L332-L354](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/anchor3d_head.py#L332-L354)）：

```python
rad_pred_encoding = torch.sin(boxes1[..., 6:7]) * torch.cos(boxes2[..., 6:7])
rad_tg_encoding = torch.cos(boxes1[..., 6:7]) * torch.sin(boxes2[..., 6:7])
boxes1 = torch.cat([boxes1[..., :6], rad_pred_encoding, boxes1[..., 7:]], dim=-1)
boxes2 = torch.cat([boxes2[..., :6], rad_tg_encoding, boxes2[..., 7:]], dim=-1)
```

只替换第 7 维（yaw），前 6 维与其余附加维原样保留；作用对象是**正样本**的预测与目标（在 [L308-L310](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/anchor3d_head.py#L308-L310) 被调用）。注意它**不改变**网络回归的语义——解码仍用原始 \(t_r\)，sin 差只发生在损失计算内部。

**get_direction_target**（[train_mixins.py:L323-L353](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/train_mixins.py#L323-L353)）：`rot_gt = reg_targets[..., 6] + anchors[..., 6]` 表明方向目标基于**绝对 yaw**（anchor 角 + 编码差还原）；`one_hot=False` 的调用点在 [L296-L301](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/train_mixins.py#L296-L301)，因为 CE 损失接受整数类别标签。

**方向损失**：`loss_dir` 是 2 类 CrossEntropy，只对正样本计算（[anchor3d_head.py:L317-L324](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/anchor3d_head.py#L317-L324)），默认权重仅 0.2——它是「修正项」而非主损失。

**推理修正**（[base_3d_dense_head.py:L359-L365](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/base_3d_dense_head.py#L359-L365)）：

```python
dir_rot = limit_period(bboxes[..., 6] - self.dir_offset,
                       self.dir_limit_offset, np.pi)
bboxes[..., 6] = (dir_rot + self.dir_offset +
                  np.pi * dir_scores.to(bboxes.dtype))
```

注意这里 `limit_period` 的周期是 \(\pi\)（训练目标生成时是 \(2\pi\)）——先把解码出的 yaw 折叠到以 `dir_offset` 为起点的 \([0, \pi)\)，再按方向类别决定加不加 \(\pi\)。

#### 4.4.4 代码实践

**实践目标**：用数值实验验证「sin 差 = 角度差的正弦」与「π 周期折叠 + 方向修正可复原任意 yaw」。

**操作步骤**（示例代码）：

```python
import numpy as np, torch
from mmdet3d.models.dense_heads.anchor3d_head import Anchor3DHead
from mmdet3d.structures import limit_period

# 实验 1：sin 差编码等价于 sin(pred - gt)
p = torch.tensor([[0., 0., 0., 3.9, 1.6, 1.56, 0.5]])          # pred yaw=0.5
g = torch.tensor([[0., 0., 0., 3.9, 1.6, 1.56, 0.5 + 2*np.pi]]) # gt 差整整 2π
pe, ge = Anchor3DHead.add_sin_difference(p, g)
print((pe[:, 6] - ge[:, 6]).item(), torch.sin(p[:, 6] - g[:, 6]).item())
# 两者都 ≈ 0：物理同向的角度在 sin 差下零惩罚

# 实验 2：方向修正复原 yaw
dir_offset = -np.pi / 2
yaw = torch.tensor([0.3, 0.3 + np.pi, -2.9, 1.0])
dir_score = (torch.cos(yaw) >= 0).long()      # 假装分类器完美：朝 +x 为 0，朝 -x 为 1
dir_rot = limit_period(yaw - dir_offset, 0, np.pi)
restored = dir_rot + dir_offset + np.pi * dir_score
print(torch.cos(restored) - torch.cos(yaw))   # 全 ≈ 0
print(torch.sin(restored) - torch.sin(yaw))   # 全 ≈ 0 → 与原 yaw 模 2π 等价
```

**需要观察的现象**：实验 1 中两个数都接近 0（\(\sin(0.5 - 0.5 - 2\pi) = 0\)）；实验 2 中 `restored` 与 `yaw` 的正余弦完全一致——即复原出了**模 \(2\pi\) 等价**的角度（数值上可能差整数个 \(2\pi\)）。

**预期结果**：sin/cos 差在 \(10^{-7}\) 量级。可再故意把 `dir_score` 全设 0，观察朝 −x 的两个 yaw 复原后 cos 值符号翻转——这就是分类错向的后果。

**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：若 `dir_offset=0`（无偏移），方向二分类的分界在哪？对 KITTI 有什么问题？

答案：分界在 yaw = 0 与 \(\pi\) 处，类别 0 = 朝 +x、类别 1 = 朝 −x，边界恰好压在**最常见**的朝向（顺 x 行驶）上，真值稍有噪声就会在 0/1 之间跳变，标签不稳定。`-π/2` 把边界挪到罕见的 y 向。

**练习 2**：`add_sin_difference` 之后，SmoothL1 损失面对「yaw 差 \(\pi\)」的预测惩罚是多少（设其余维度全对）？

答案：\(|\sin \pi| = 0\)，惩罚为 0——sin 差对 \(\pi\) 周期也不敏感（\(\sin(p-g)\) 在差为 \(\pi\) 时为 0）。所以「头尾调转」必须交给方向分类分支兜底，这正是两个技巧必须成对出现的原因。

**练习 3**：为什么推理修正里 `limit_period` 用周期 \(\pi\) 而训练目标生成里用 \(2\pi\)？

答案：回归分支经 sin 差训练后，其 yaw 预测只在**模 \(\pi\)** 意义下可靠（差 \(\pi\) 不受罚），所以先以 \(\pi\) 为周期折叠到 \([0, \pi)\)；方向分支补出「加不加 \(\pi\)」这一比特，二者合成模 \(2\pi\) 完整角度。训练侧则以 \(2\pi\) 为周期直接把绝对 yaw 折进 \([0, 2\pi)\) 再对半分成两类。

## 5. 综合实践

**任务：从配置到张量，完整核对 SECOND 的 anchor 链路。**

把 4.1.4 / 4.2.4 / 4.3.4 三个实验合并成一个脚本，完成一条自检流水线：

1. **读配置**：`Config.fromfile('configs/_base_/models/second_hv_secfpn_kitti.py')`，取出 `model.bbox_head` 并 `update(train_cfg=..., test_cfg=...)`，`MODELS.build` 构造 head（复现 [single_stage.py:L50-L52](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/single_stage.py#L50-L52) 的注入逻辑）；
2. **链路换算**：从 `data_preprocessor.voxel_layer` 里读出 `point_cloud_range` 与 `voxel_size`，手算 \(x\) 方向格数 \(70.4/0.05=1408\)、\(y\) 方向 \(80/0.05=1600\)，再除以 SparseEncoder 的 4 倍下采样得 \(352 \times 400\)，与 anchor 生成所用 featmap 对上；
3. **anchor 核对**：`grid_anchors([(400, 352)], device='cpu')`，断言 `numel()//7 == 400*352*len(sizes)*len(rotations)`；
4. **分支核对**：小特征图 forward，断言通道数 \(=6\times3, 6\times7, 6\times2\)；
5. **编码闭环**：从生成的 anchor 里任取一个，`decode(anchor, zeros)` 应等于 anchor 自身；再随机造一个真值框做 encode→decode 往返，误差应接近 0；
6. **方向演练**：对该 anchor 与「调转 180° 且 x/y_size 互换」的框（几何上同一个矩形）调用 `add_sin_difference`，观察 yaw 维惩罚为 0，体会为什么还需要方向分支。

**通过标准**：所有断言通过，并在笔记里用一句话回答——「SECOND 的检测头每帧要评估多少个假设，每个假设输出几个数？」（答案：844800 个假设，每个 3 分数 + 7 差值 + 2 方向 logit。）

## 6. 本讲小结

- `Anchor3DRangeGenerator` 把位置（ranges/linspace 网格）、尺寸（sizes）、朝向（rotations）做笛卡尔积铺满 BEV；anchor 总数 = \(H \times W \times |sizes| \times |rotations|\)，SECOND KITTI 为 844800；`Aligned` 变体修复了 linspace 与体素格中心的 ±1 错位。
- anchor 的 z 语义是**底面中心**（与 `LiDARInstance3DBoxes` 一致），不同类别可配不同 z 范围；`reshape_out=False` 保留 (H, W, size, rot) 结构，供按尺寸分组的列表 assigner 切片。
- `DeltaXYZWLHRBBoxCoder` 编码三要素：z 先换几何中心再作差、位移按 BEV 对角线/高度归一、尺寸用 log 比；encode/decode 严格互逆，零差值解码回 anchor 本身。
- `Anchor3DHead` 网络部分就是三个 1×1 卷积：分类（\(A \times C\)）、回归（\(A \times 7\)）、方向（\(A \times 2\)）；FocalLoss 路线隐式背景 + `PseudoSampler`，回归与方向损失只作用于正样本。
- 角度两宗罪各有解药：`add_sin_difference` 让回归损失对 \(2\pi\)（以及 \(\pi\)）周期不敏感；方向二分类补上「头尾」这一比特，推理时经 \(\pi\) 周期 `limit_period` + `dir_offset` 修正合成完整 yaw，`dir_offset=-π/2` 把分类边界挪到朝向最稀疏的 y 向。

## 7. 下一步学习建议

- **u5-l5（CenterPoint）**：看 anchor-free 范式如何用 center heatmap + 宽高热图取代本讲的 anchor 网格与 `DeltaXYZWLHRBBoxCoder`，并对比 CircleNMS 与本讲 `box3d_multiclass_nms` 的差异；
- **u5-l6（coders/assigners/samplers）**：本讲只用了 `Max3DIoUAssigner` 的「调用侧」，下一讲深入 assigner 内部的正负样本准则与 `PartialBinBasedBBoxCoder` 等室内编码方案；
- **源码延伸阅读**：[mmdet3d/models/layers/box3d_nms.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/box3d_nms.py#L11-L20) 中 `box3d_multiclass_nms` 如何消费本讲传出的 `dir_scores`；以及 [configs/centerpoint/](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/centerpoint/) 下 nuScenes 配置里 9 维框（含速度 custom_values）的 anchor 生成方式。

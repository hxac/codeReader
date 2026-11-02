# u5-l7 3D 损失函数与 NMS

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 mmdet3d 原生三大损失（`RotatedIoU3DLoss`、`AxisAlignedIoULoss`、`MultiBinLoss`）各自解决什么问题、分别被哪类检测头使用。
2. 理解 `weighted_loss` 装饰器为所有损失提供的公共骨架（weight / reduction / avg_factor）。
3. 手算几组典型几何关系下的旋转 3D IoU，并用单元测试验证自己的理解。
4. 解释 MultiBin 角度损失的「bin 分类 + 残差回归」设计，并能读懂它的目标编码格式。
5. 区分 `box3d_nms.py` 中五种去重函数的输入格式、重叠度量、运行设备与适用场景，并能说出 `nms_bev` 与 `nms_normal_bev`（旋转 vs 轴对齐）的差异。

> **版本警示（必读）**：本讲大纲里提到的 `rotated_nms_3d` 与 `aligned_rotated_nms_3d` 是 mmdet3d **0.x 时代的 API 名**，在当前 1.x 代码（HEAD `fe25f7a5`）中已经不存在（在 `mmdet3d/`、`configs/`、`tests/` 全量 grep 无结果）。它们的功能在 1.x 中由 `nms_bev`（旋转）与 `nms_normal_bev`（轴对齐）承担。同理，旧版 `rotated_iou_loss.py` 内纯 PyTorch 可微 IoU 实现中的 `box_converter`、`permute` 组件也已下沉为 mmcv 的编译算子 `diff_iou_rotated_3d`，仓库内已无这两个名字。本讲全部按当前源码讲解，不引用已删除的接口。

## 2. 前置知识

- **7 自由度 3D 框**（u2-l2）：中心 \((x, y, z)\) + 尺寸 \((w, l, h)\) + 朝向角 \(\alpha\)。本讲出现的裸张量均按此顺序排列。
- **`with_yaw=False` 的轴对齐框**（u2-l2、u3-l4）：室内数据集（ScanNet 等）的框没有朝向角，用 6 维 \((x_1, y_1, z_1, x_2, y_2, z_2)\)（两个对角点）表示。
- **「一一对应」损失 vs IoU 矩阵**（u5-l6）：assigner 需要的是 \(N \times M\) 的 IoU 矩阵来给 anchor 分配身份；而损失函数面对的是已经配好对的 \(N\) 对预测/真值，计算的是长度 \(N\) 的对齐 IoU 向量。本讲三个损失全部属于后者。
- **`weighted_loss` 装饰器**：从 MMDetection 借来的装饰器。证据在 mmdet3d 的调用点里：裸函数 `rotated_iou_3d_loss(pred, target)` 的签名只有两个参数，但 [rotated_iou_loss.py:L83-L89](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/losses/rotated_iou_loss.py#L83-L89) 却向它传了 `weight`、`reduction`、`avg_factor`——这些参数由装饰器消费，负责「逐元素损失 → 加权 → 按 reduction 规约」的后处理，裸函数本身只负责算逐元素损失。
- **NMS（非极大值抑制）**：把候选框按分数从高到低排序，每取一个最高分框，就删掉与它重叠超过阈值的其余框，循环至空。3D 检测的「重叠」可以在 BEV 平面算、在 3D 体积上算，甚至用中心距离代替——这正是本讲 4.5 节的主线。
- **角度的周期性难题**（u5-l4 承接）：\(\alpha\) 与 \(\alpha + 2\pi\) 是同一个朝向，直接 L1 回归会在 \(\pm\pi\) 边界爆炸；MultiBin 用「离散 bin + 连续残差」绕开它。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [mmdet3d/models/losses/rotated_iou_loss.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/losses/rotated_iou_loss.py) | 可微旋转 3D IoU 损失，调 mmcv CUDA 算子 |
| [mmdet3d/models/losses/axis_aligned_iou_loss.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/losses/axis_aligned_iou_loss.py) | 轴对齐 6 维框的体积 IoU/GIoU 损失 |
| [mmdet3d/models/losses/multibin_loss.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/losses/multibin_loss.py) | 角度的 bin 分类 + 残差回归混合损失 |
| [mmdet3d/structures/ops/iou3d_calculator.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/ops/iou3d_calculator.py) | IoU 计算器家族；`AxisAlignedBboxOverlaps3D` 是轴对齐损失的后端 |
| [mmdet3d/models/layers/box3d_nms.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/box3d_nms.py) | 五种 3D/NMS 去重后处理函数 |
| [mmdet3d/models/losses/__init__.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/losses/__init__.py) | 3D 原生损失家族的导出清单 |
| [mmdet3d/models/task_modules/coders/monoflex_bbox_coder.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/monoflex_bbox_coder.py) | MultiBin 目标编码的「上游供应商」 |
| 调用方示例 | `base_3d_dense_head.py`、`centerpoint_head.py`、`monoflex_head.py`、`vote_head.py` |
| 测试 | [tests/test_models/test_losses/](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_models/test_losses/test_multibin_loss.py)、[tests/test_models/test_layers/test_box3d_nms.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_models/test_layers/test_box3d_nms.py) |

导出清单见 [mmdet3d/models/losses/__init__.py:L12-L17](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/losses/__init__.py#L12-L17)：除本讲三个损失外，还有 `ChamferDistance`、`LovaszLoss`（分割用）、`UncertainSmoothL1Loss`（单目深度用）等，它们注册于 `MODELS`，在 config 中以 `dict(type='RotatedIoU3DLoss')` 的方式被构建（注册器机制见 u4-l1）。

## 4. 核心概念与源码讲解

### 4.1 损失的公共骨架：nn.Module 壳 + weighted_loss 芯

#### 4.1.1 概念说明

mmdet3d 的每个损失都是「两层结构」：

- **外层 `nn.Module`**：无参数的薄壳，只存 `reduction` 与 `loss_weight` 两个配置，暴露统一签名 `forward(pred, target, ...)`，让检测头可以用同一套代码调不同损失。
- **内层裸函数**：被 `@weighted_loss` 装饰，只负责「逐元素损失」的数学计算。

这种结构让「数学」与「工程规约」解耦：换一种归约方式（mean/sum/none）、加权重、除以 `avg_factor`，都不用改数学公式。

#### 4.1.2 核心流程

外层 `forward` 的公共流程（三个损失完全一致）：

```text
forward(pred, target, weight, avg_factor, reduction_override):
1. 若 weight 全零 → 直接返回 0（无正样本时防止 NaN）
2. 断言 reduction_override ∈ {None, 'none', 'mean', 'sum'}
3. reduction = reduction_override 或 self.reduction
4. loss = loss_weight * 裸函数(pred, target, weight, reduction, avg_factor)
5. 返回 loss
```

`reduction` 的语义：`'none'` 返回长度 N 的逐框损失向量（供检测头逐框加权），`'mean'`/`'sum'` 返回标量。

#### 4.1.3 源码精读

`RotatedIoU3DLoss.forward` 的完整骨架（轴对齐版几乎逐行相同）：

- [mmdet3d/models/losses/rotated_iou_loss.py:L76-L77](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/losses/rotated_iou_loss.py#L76-L77) —— weight 不为空但全零时直接返回 0，注释写明 `# 0`。这是「该批样本没有正样本」的防御。
- [mmdet3d/models/losses/rotated_iou_loss.py:L81-L82](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/losses/rotated_iou_loss.py#L81-L82) —— 若 weight 是多维的（比如与框同维的 (N, 7)），先对最后一维取均值压成 (N,)。**注意这个细节只有 RotatedIoU3DLoss 有**，`AxisAlignedIoULoss` 没有。
- [mmdet3d/models/losses/rotated_iou_loss.py:L83-L89](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/losses/rotated_iou_loss.py#L83-L89) —— 把 weight/reduction/avg_factor 传给被装饰的裸函数 `rotated_iou_3d_loss`（其签名见 L14 只有 pred/target，证明这些参数由装饰器消费）。
- [mmdet3d/models/losses/axis_aligned_iou_loss.py:L77-L85](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/losses/axis_aligned_iou_loss.py#L77-L85) —— 轴对齐版同款骨架；短路条件多了 `reduction != 'none'` 的判断。

#### 4.1.4 代码实践

1. **实践目标**：用注册器构建损失并观察 reduction 对输出形状的影响。
2. **操作步骤**（示例代码，可在仓库根目录用 `python -c` 或脚本运行）：

   ```python
   from mmdet3d.registry import MODELS
   import torch

   loss = MODELS.build(dict(type='MultiBinLoss', reduction='none', loss_weight=1.0))
   # 观察构造期断言：把 reduction 换成 'l2' 会触发 AssertionError
   ```

3. **需要观察的现象**：`reduction='l2'` 时构造即报错（断言见 [multibin_loss.py:L80](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/losses/multibin_loss.py#L80)）。
4. **预期结果**：合法 reduction 通过，非法值抛 `AssertionError`。完整运行需本地已安装 mmdet3d 及依赖，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么损失函数里的 IoU 是「对齐」的（is_aligned=True），而 u5-l6 的 assigner 里是 \(N \times M\) 矩阵？

**答案**：assigner 的任务是给每个 anchor 找身份，必须让每个 anchor 与所有 gt 都算一遍 IoU（矩阵）；损失函数发生在配对完成之后，第 i 个预测只和第 i 个真值比较，所以是长度 N 的对齐向量，计算量也从 \(O(NM)\) 降到 \(O(N)\)。

**练习 2**：`weight` 参数全零时 `RotatedIoU3DLoss` 返回什么？为什么需要这个分支？

**答案**：返回 `pred.sum() * weight.sum()` 即 0。FocalLoss 类损失中背景样本 weight 为 0，若某批全为背景，`1 - IoU` 对无意义框对仍会算出非零值并参与归约，污染总损失，因此提前短路。

---

### 4.2 RotatedIoU3DLoss：可微的旋转 3D IoU 损失

#### 4.2.1 概念说明

L1/SmoothL1 损失对 7 个自由度逐项独立惩罚，但评估指标（AP）只看框的**整体重叠**。IoU 损失直接优化评估所用的度量：

\[
\text{IoU} = \frac{|B_{p} \cap B_{gt}|}{|B_{p} \cup B_{gt}|}, \qquad L = 1 - \text{IoU}
\]

它天然尺度不变（大框小框同等对待），且中心、尺寸、朝向的误差会耦合地反映到一个数里。难点在于**旋转 3D 框的交集体积没有解析简洁的公式**，且反向传播需要每一步可微——这由 mmcv 的 CUDA 算子 `diff_iou_rotated_3d`（diff = differentiable）完成。mmdet3d 这边只剩 10 行调用代码。

当前使用方：ImVoxelNet（[imvoxel_head.py:L57](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/imvoxel_head.py#L57) 默认配置）、FCAF3D（[configs/fcaf3d/fcaf3d_2xb8_sunrgbd-3d-10class.py:L12](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/fcaf3d/fcaf3d_2xb8_sunrgbd-3d-10class.py#L12)）、NeRF-Det；projects 里的 TR3D 还基于它扩展了 DIoU 模式。

#### 4.2.2 核心流程

```text
rotated_iou_3d_loss(pred[N,7], target[N,7]):
1. pred.unsqueeze(0)、target.unsqueeze(0) → [1, N, 7]
   （mmcv 算子要求带 batch 维）
2. iou = diff_iou_rotated_3d(pred[1,N,7], target[1,N,7]) → [1, N]
3. return (1 - iou)[0] → [N]  逐元素损失
```

`(x, y, z)` 是框的几何中心，旋转发生在 BEV 平面（绕 z 轴），高度维只做区间求交——这两个语义都可以从官方单元测试反推验证（见下）。

#### 4.2.3 源码精读

- [mmdet3d/models/losses/rotated_iou_loss.py:L5](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/losses/rotated_iou_loss.py#L5) —— 从 mmcv 导入编译好的可微旋转 IoU 算子；0.x 时代这里的纯 PyTorch 实现（含 `box_converter`、`permute` 等组件）已被替换。
- [mmdet3d/models/losses/rotated_iou_loss.py:L13-L30](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/losses/rotated_iou_loss.py#L13-L30) —— 裸函数本体：docstring 声明输入是 `(x, y, z, w, l, h, alpha)` 的 [N, 7] 张量、pred 与 target 一一对应；L28-L29 完成上述三步。
- 单元测试 [tests/test_models/test_losses/test_rotated_iou_loss.py:L14-L27](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_models/test_losses/test_rotated_iou_loss.py#L14-L27) —— 5 组单位立方体（中心 (0.5, 0.5, 0.5)，尺寸 1×1×1，yaw=0）对照各种扰动，期望 IoU 损失 `[0, 0.5, 1-0.7071, 1-1/15, 1]`。这张表是本讲最好的教学材料：

| 组 | target 特征 | 几何关系 | IoU |
| --- | --- | --- | --- |
| 1 | 完全相同的框 | 完全重合 | 1 |
| 2 | 尺寸 1×1×2，yaw=π/2 | BEV 都是单位正方形（**正方形转 90° 不变**），差异全来自高度 2 vs 1：交 1×1×1=1，并 1+2−1=2 | 0.5 |
| 3 | 尺寸 1×1×1，yaw=π/4 | BEV 45° 旋转的两个单位正方形：交 \(2(\sqrt{2}-1)\approx0.8284\)，并 \(\approx1.1716\)，IoU\(=0.7071=1/\sqrt{2}\)；高度相同所以 3D IoU = BEV IoU | 0.7071 |
| 4 | 中心 (1,1,1) | 各轴仅重叠 0.5：交 \(0.5^3=0.125\)，并 \(2-0.125=1.875\) | 1/15 |
| 5 | 中心 (−1.5,−1.5,−1.5) 尺寸 2.5 | 完全分离 | 0 |

- [tests/test_models/test_losses/test_rotated_iou_loss.py:L11-L12](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_models/test_losses/test_rotated_iou_loss.py#L11-L12) —— `if not torch.cuda.is_available(): return`：**这个损失只能在 CUDA 上运行**（mmcv 算子仅提供 CUDA 实现），CPU 环境会静默跳过测试。

#### 4.2.4 代码实践

1. **实践目标**：亲手复现上表的 5 个 IoU 值，验证自己对旋转 IoU 语义的理解。
2. **操作步骤**（示例代码，需 GPU）：

   ```python
   import torch
   from mmdet3d.models.losses import RotatedIoU3DLoss
   import numpy as np

   pred = torch.tensor([[.5, .5, .5, 1., 1., 1., .0]] * 5).cuda()
   target = torch.tensor([
       [.5, .5, .5, 1., 1., 1., .0],
       [.5, .5, .5, 1., 1., 2., np.pi / 2],
       [.5, .5, .5, 1., 1., 1., np.pi / 4],
       [1., 1., 1., 1., 1., 1., .0],
       [-1.5, -1.5, -1.5, 2.5, 2.5, 2.5, .0]]).cuda()
   print(RotatedIoU3DLoss(reduction='none')(pred, target))
   ```

3. **需要观察的现象**：输出应等于 `1 - [1, .5, .7071, 1/15, 0]`。
4. **预期结果**：`[0.0000, 0.5000, 0.2929, 0.9333, 1.0000]`（与测试断言一致）。无 GPU 环境无法运行，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：第 2 组里 target 的 yaw=π/2，为什么 IoU 不是 1 也不是「旋转造成的某个值」？

**答案**：BEV 投影是单位正方形，旋转 90° 后与自身完全重合，所以 yaw 不产生任何影响；IoU 从 1 掉到 0.5 完全因为高度维从 1 变成 2（交高 1、并高按体积算 1+2−1=2）。这说明 3D IoU = BEV 旋转 IoU × 高度交集比（呼应 u2-l2 讲过的分解）。

**练习 2**：把第 4 组 target 的 yaw 从 0 改成 π/4，IoU 会怎么变？

**答案**：会变小。旋转后 BEV 投影的外接范围变大，与原框在 x/y 上的对齐重叠进一步减少（原本每轴恰好重叠 0.5），交集体积下降而并集体积上升。具体数值**待本地验证**。

---

### 4.3 AxisAlignedIoULoss：室内轴对齐框的体积 IoU

#### 4.3.1 概念说明

室内场景（ScanNet、SUN RGB-D）的框没有朝向角（`with_yaw=False`，u2-l2），用两个对角点 \((x_1, y_1, z_1, x_2, y_2, z_2)\) 表示 6 维框。两个轴对齐框的交并就是三根轴上区间交叠的乘积，**纯 PyTorch 广播就能可微地算出来**，不需要 CUDA 算子：

\[
\text{IoU} = \frac{\prod_k \max(0,\ \min(p_k^{max}, g_k^{max}) - \max(p_k^{min}, g_k^{min}))}{|B_p| + |B_{gt}| - \text{inter}}
\]

该损失同时支持 GIoU 模式：

\[
\text{GIoU} = \text{IoU} - \frac{|C| - |B_p \cup B_{gt}|}{|C|}, \qquad C = \text{最小外接轴对齐框}
\]

GIoU 对「完全不相交」的框对仍能提供梯度方向（IoU 恒为 0 时 IoU 损失梯度为 0，训练会停滞）。

典型使用方：VoteNet 的 IoU-loss 变体（[configs/votenet/votenet_head-iouloss_8xb8_scannet-3d.py:L4-L8](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/votenet/votenet_head-iouloss_8xb8_scannet-3d.py#L4-L8)，注意它用了 `reduction='sum'`、`loss_weight=10/3`）和 FCAF3D（[configs/_base_/models/fcaf3d.py:L16](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/fcaf3d.py#L16)）。

#### 4.3.2 核心流程

```text
axis_aligned_iou_loss(pred[N,6], target[N,6]):
1. 调 AxisAlignedBboxOverlaps3D()(pred, target, is_aligned=True) → iou[N]
2. return 1 - iou
```

计算器内部（is_aligned=True 分支）：三轴分别取 max(左下) / min(右上) → clamp(min=0) 得交叠长 → 体积；并集 = 两体积和 − 交；除法前给分母夹一个 `eps=1e-6` 下限防零除。

#### 4.3.3 源码精读

- [mmdet3d/models/losses/axis_aligned_iou_loss.py:L26-L28](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/losses/axis_aligned_iou_loss.py#L26-L28) —— 裸函数直接 new 一个 `AxisAlignedBboxOverlaps3D` 实例做对齐计算，再取 `1 - iou`。注意这里是**硬 import 直接实例化**（L10），不是走注册器 build。
- [mmdet3d/structures/ops/iou3d_calculator.py:L179-L207](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/ops/iou3d_calculator.py#L179-L207) —— 计算器类本体，注册于 `TASK_UTILS`；[L200](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/ops/iou3d_calculator.py#L200) 断言最后一维**必须等于 6**。
- [mmdet3d/structures/ops/iou3d_calculator.py:L281-L301](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/ops/iou3d_calculator.py#L281-L301) —— 先算两框各自体积（L281-L286）；对齐分支取逐对 `lt = max(前3维)`、`rb = min(后3维)`，clamp 后乘出交叠体积（L288-L293）；`mode='giou'` 时额外算最小外接框两个角点（L299-L301）。
- [mmdet3d/structures/ops/iou3d_calculator.py:L319-L329](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/ops/iou3d_calculator.py#L319-L329) —— 分母 `max(union, eps)` 防零除，返回 iou 或 gious。
- **docstring 漂移警示**：损失文件 docstring 写 pred 形状是 `[..., 3]`（[axis_aligned_iou_loss.py:L19-L20](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/losses/axis_aligned_iou_loss.py#L19-L20)），但实现断言必须 6 维——以断言为准，这是「不能只读 docstring」的又一实例（u4-l1 讲过注册节点数量的注释漂移）。

#### 4.3.4 代码实践

1. **实践目标**：用可手算的框对验证体积 IoU 公式，并对比 iou/giou 两模式。
2. **操作步骤**（示例代码，CPU 即可）：

   ```python
   import torch
   from mmdet3d.models.losses import AxisAlignedIoULoss

   pred = torch.tensor([[0., 0., 0., 2., 2., 2.]])
   target = torch.tensor([[1., 1., 1., 3., 3., 3.]])   # 每轴交叠 1
   loss = AxisAlignedIoULoss(reduction='none')
   print(loss(pred, target))
   ```

3. **需要观察的现象**：交集体积 \(1^3=1\)，并集 \(8+8-1=15\)，IoU = 1/15，损失应为 `1 - 1/15 ≈ 0.9333`。
4. **预期结果**：`tensor([0.9333])`。纯 PyTorch 实现，CPU 可直接运行，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 VoteNet 的 IoU-loss 变体配置用 `reduction='sum'` 而不是默认的 `'mean'`？

**答案**：室内一个场景的正样本框数波动很大（有的场景 5 个物体、有的 50 个）。`sum` 让总损失随物体数线性增长，配合固定的 `loss_weight=10/3`，等价于把「逐框平均的权重」交由 `avg_factor` 场景另行控制；而 `mean` 会让单个物体在稀疏场景中被放大权重。这是配置层面的取舍，没有绝对对错。

**练习 2**：`AxisAlignedIoULoss` 与 `RotatedIoU3DLoss` 输入都叫 pred/target，它们对最后一维的要求分别是什么？

**答案**：`RotatedIoU3DLoss` 要求 7 维 `(x, y, z, w, l, h, alpha)`，且只能跑 CUDA；`AxisAlignedIoULoss` 要求 6 维 `(x1, y1, z1, x2, y2, z2)`（对角点），纯 PyTorch、CPU 可跑。

---

### 4.4 MultiBinLoss：bin 分类 + 残差回归的角度损失

#### 4.4.1 概念说明

角度 \(\alpha \in (-\pi, \pi]\) 有两个麻烦：\(2\pi\) 周期让 L1 在边界处产生假梯度；±π 附近的「头尾歧义」让回归目标不稳定。MultiBin（出自 KITTI 单目 3D 检测的经典设计）把角度拆成两问：

1. **落在哪个 bin？** 把圆周均分为 \(B\) 个 bin，做分类——周期性问题消失，因为分类不关心角度连续性。
2. **离 bin 中心偏多少？** 只在正 bin 上，把残差 \(\Delta\alpha\) 编码成 \((\sin\Delta\alpha, \cos\Delta\alpha)\) 回归——单位圆上的点，天然有界。

经典公式：

\[
L_\theta = \frac{1}{B}\sum_{i=1}^{B} \text{CE}\big(c_i,\ \hat{c}_i\big) + \sum_{i \in \text{pos}} \Big( |\hat{s}_i - \sin\Delta\alpha_i| + |\hat{c}'_i - \cos\Delta\alpha_i| \Big)
\]

当前唯一使用方是 MonoFlex（[monoflex_head.py:L98](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/monoflex_head.py#L98) 默认 `dict(type='MultiBinLoss', loss_weight=0.1)`）。它与 u5-l4 讲的「正弦差回归 + 方向二分类」是同一问题的两种工程解：SECOND 系直接回归 sin 差并用二分类辨头尾，MonoFlex 用 bin 结构同时解决两件事。

#### 4.4.2 核心流程

**目标编码**（由 MonoFlex 的 bbox coder 完成，[monoflex_bbox_coder.py:L78-L110](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/monoflex_bbox_coder.py#L78-L110)）：

```text
输入 local_yaw ∈ (-π, π]，输出 target[N, 2B]:
  前 B 列：bin one-hot（含 bin_margin 时可多热）
  后 B 列：该 bin 内的残差弧度（相对 bin 中心，wrap 到 [-π, π]）
```

**损失计算**（对每个 bin i）：

```text
1. cls：cross_entropy(pred[i 的 2 个 logits], target 第 i 列 one-hot)
2. reg：仅当 target 第 i 列存在 1（该框落在此 bin）时
   pred_sin_cos = F.normalize(pred 的 2 个残差输出)   # 归一化到单位圆
   loss += |pred_sin - sin(残差)| + |pred_cos - cos(残差)|
3. 总损失 = cls 部分均值 + reg 部分均值
```

#### 4.4.3 源码精读

- [mmdet3d/models/losses/multibin_loss.py:L20-L24](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/losses/multibin_loss.py#L20-L24) —— 输入契约：pred 是 `[axis_cls, head_cls, sin, cos] × B` 拼成的 `(N, 4B)`；target 是 `(N, 2B)`。
- [mmdet3d/models/losses/multibin_loss.py:L37-L40](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/losses/multibin_loss.py#L37-L40) —— bin 分类：`F.cross_entropy` 把每个 bin 的 2 个 logits 与 one-hot 标签做二分类。
- [mmdet3d/models/losses/multibin_loss.py:L42-L48](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/losses/multibin_loss.py#L42-L48) —— `valid_mask_i = (gt == 1)` 选出落在此 bin 的框；残差预测先 `F.normalize` 投影到单位圆。
- [mmdet3d/models/losses/multibin_loss.py:L49-L57](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/losses/multibin_loss.py#L49-L57) —— 真值残差取 `sin/cos`，与预测做 L1，两分量相加。
- **重要源码发现——return 在循环体内**：[mmdet3d/models/losses/multibin_loss.py:L62](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/losses/multibin_loss.py#L62) 的 `return` 缩进为 8 个空格，与 L36-L60 的循环体同级，**位于 `for i in range(num_dir_bins)` 之内**。因此循环只执行 i=0 一次就返回——只有第一个 bin 的分类损失与残差损失被累计，与上面经典公式「对所有 bin 求和」不同。用 `git show d93205ed`（本文件的首个提交）可见该缩进自引入起就是如此；且单元测试 [tests/test_models/test_losses/test_multibin_loss.py:L29-L31](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_models/test_losses/test_multibin_loss.py#L29-L31) 的期望值 `2.1120` 恰好等于「只累计第一个 bin」的手算结果（第一行框：CE≈0.969、第二行≈0.611，均值 0.790，除以 4 得 0.1976；残差项两行合计 ≈3.829、除以计数 2 得 1.914；总计 2.112）。测试锁定了这一行为，读源码时不能只信 docstring 里的「Multi-Bin Loss」名字。
- 顺带的边界提示：若第一个 bin 内没有任何正样本（`reg_cnt == 0`），`reg_losses / reg_cnt` 是 0/0（NaN）。MonoFlex 编码允许 bin 多热（[monoflex_bbox_coder.py:L102-L106](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/monoflex_bbox_coder.py#L102-L106)：凡 |offset| < bin_size/2 + margin 的 bin 都置 1），一定程度上降低了这种概率。
- [mmdet3d/models/losses/multibin_loss.py:L84-L107](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/losses/multibin_loss.py#L84-L107) —— 外层 forward；注意与另两个损失不同，它**不接受 weight**，且默认 `reduction='none'`。

#### 4.4.4 代码实践

1. **实践目标**：跑通官方测试数据，验证 2.1120，并亲手验证「早退 return」的影响。
2. **操作步骤**（示例代码，CPU 即可）：

   ```python
   import torch
   from mmdet3d.registry import MODELS

   pred = torch.tensor([[0.81, 0.32, 0.78, 0.52, 0.24, 0.12, 0.32, 0.11,
                         1.20, 1.30, 0.20, 0.11, 0.12, 0.11, 0.23, 0.31],
                        [0.02, 0.19, 0.78, 0.22, 0.31, 0.12, 0.22, 0.11,
                         1.20, 1.30, 0.45, 0.51, 0.12, 0.11, 0.13, 0.61]])
   target = torch.tensor([[1, 1, 0, 0, 2.14, 3.12, 0.68, -2.15],
                          [1, 1, 0, 0, 3.12, 3.12, 2.34, 1.23]])
   loss = MODELS.build(dict(type='MultiBinLoss', reduction='none', loss_weight=1.0))
   print(loss(pred, target, num_dir_bins=4))
   ```

   然后在**本地副本**中把 L62 的 `return` 退一格移出 for 循环（不要改仓库源码，复制一份文件实验），再跑同样的数据对比输出。

3. **需要观察的现象**：原版输出 `2.1120`（与测试断言一致）；移出 return 后，4 个 bin 的分类损失与第 2 个 bin（target 第 2 列为 1）的残差也被累计。
4. **预期结果**：移出后数值大于 2.1120（多累加了 3 个 bin 的 CE 与额外残差项），具体数值**待本地验证**。也可直接运行 `pytest tests/test_models/test_losses/test_multibin_loss.py` 对照。

#### 4.4.5 小练习与答案

**练习 1**：为什么残差要回归 `(sin, cos)` 而不是直接回归弧度？

**答案**：弧度差在 wrap 边界（±π）附近不连续，L1 会产生错误的大梯度；`(sin, cos)` 把角度映射到单位圆上的点，连续且有界，预测经 `F.normalize` 后天然落在单位圆附近，与真值点的 L1 距离单调反映角度差。

**练习 2**：pred 张量 `(N, 4B)` 中，第 i 个 bin 的「2 个分类 logits」和「2 个 sin/cos 输出」分别位于哪些列？

**答案**：分类 logits 在第 `2i : 2i+2` 列（[L37-L39](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/losses/multibin_loss.py#L37-L39)）；sin/cos 在第 `2B + 2i : 2B + 2i + 2` 列（[L45-L46](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/losses/multibin_loss.py#L45-L46) 的 `start = num_dir_bins * 2 + i * 2`）。

**练习 3**：target 的 `2B` 列里前 B 列为什么可能同时有多个 1？

**答案**：MonoFlex 的编码器带 `bin_margin`（[monoflex_bbox_coder.py:L92-L96](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/monoflex_bbox_coder.py#L92-L96)），落在 bin 交界附近的角会在相邻两个 bin 都置 1，让两个 bin 的分类器都能学到边界样本。

---

### 4.5 box3d_nms.py：五种去重后处理

#### 4.5.1 概念说明

检测头在推理阶段吐出几百上千个候选框，去重由 [mmdet3d/models/layers/box3d_nms.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/box3d_nms.py) 的五个函数承担，它们在三个维度上分化：**重叠怎么算**（旋转 BEV IoU / 轴对齐 IoU / 中心距离）、**在哪个设备算**（mmcv CUDA 算子 / 纯 torch / numba CPU）、**谁来调用**（anchor 系头 / 室内头 / CenterPoint 头 / 评估器）。

| 函数 | 输入框格式 | 重叠度量 | 设备 | 典型调用方 |
| --- | --- | --- | --- | --- |
| `box3d_multiclass_nms` | 3D 框 (N,M) + BEV 框 (N,5) | 委托下面两个 | GPU | SECOND/PointPillars（[base_3d_dense_head.py:L356](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/base_3d_dense_head.py#L356)）、FCOS3D/PGD 单目头、NuScenesMetric/WaymoMetric |
| `nms_bev` | (N,5) `(x1,y1,x2,y2,ry)` | **旋转** BEV IoU | CUDA | `box3d_multiclass_nms`、CenterHead rotate 分支 |
| `nms_normal_bev` | (N,5) | **轴对齐** BEV IoU（yaw 置 0） | CUDA | `box3d_multiclass_nms` |
| `aligned_3d_nms` | (N,6) 对角点 | 3D 体积 IoU（类别感知） | 纯 torch/CPU | VoteNet（[vote_head.py:L808](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/vote_head.py#L808)）、GroupFree3D、H3DNet |
| `circle_nms` | (N,3) `(x,y,score)` | **中心平方距离** | numba/CPU | CenterHead circle 分支（[centerpoint_head.py:L764](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/centerpoint_head.py#L764)）、DSVT、CenterFormer |

`nms_bev` 与 `nms_normal_bev` 正是 0.x 时代 `rotated_nms_3d`/`aligned_rotated_nms_3d` 在 1.x 的对应物（文件头注释 [box3d_nms.py:L231-L233](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/box3d_nms.py#L231-L233) 也说明它们在复刻 mmcv≤1.5 的 `nms_bev` 功能）。

#### 4.5.2 核心流程

**box3d_multiclass_nms（逐类调度器）**：

```text
for 每个前景类 i:
1. 取该类得分 > score_thr 的框
2. 按 cfg.use_rotate_nms 选择 nms_bev（旋转）或 nms_normal_bev（轴对齐）
3. 记录被保留的 3D 框、得分、类别标签，及可选的方向分/属性分/2D 框
拼接所有类 → 若超过 max_num 按得分截断
```

**aligned_3d_nms（类别感知的贪心循环）**：

```text
按分数升序排序 → while 非空:
1. 取当前最高分框 i 放入 pick
2. 与剩余框逐对算 3D 体积 IoU；类别不同的对 IoU 置 0（不跨类抑制）
3. 删除 IoU > thresh 的框
```

**circle_nms（距离替代 IoU）**：

```text
按分数降序 → 对每个未被抑制的最高分框 i:
    抑制所有满足 d² = (x_i−x_j)² + (y_i−y_j)² ≤ thresh 的框 j
```

注意 `thresh` 比较的是**平方距离**——等价抑制半径是 \(\sqrt{\tau}\)。

#### 4.5.3 源码精读

- [mmdet3d/models/layers/box3d_nms.py:L49-L70](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/box3d_nms.py#L49-L70) —— `num_classes = mlvl_scores.shape[1] - 1`（去掉背景列）；逐类过滤分数（L58）后按 `cfg.use_rotate_nms` 在 L65-L68 分派旋转/轴对齐 NMS。
- [mmdet3d/models/layers/box3d_nms.py:L99-L110](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/box3d_nms.py#L99-L110) —— 跨类拼接后超过 `max_num` 时按得分截断；方向分、属性分、2D 框同步索引。
- [mmdet3d/models/layers/box3d_nms.py:L257-L271](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/box3d_nms.py#L257-L271) —— `nms_bev`：断言 (N,5)；按分数降序并可选 `pre_max_size` 截断；L264-L269 把调用方传入的 `(x1,y1,x2,y2,ry)` **换算回** `(cx,cy,w,h,ry)`（注释自嘲「better skip this step in the future」），再调 mmcv 的 `nms_rotated` CUDA 算子；L272 把 keep 映射回原始索引。调用方（如 [base_3d_dense_head.py:L345-L346](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/base_3d_dense_head.py#L345-L346)）先用 `xywhr2xyxyr` 把 BEV 框转成 xyxyr 喂进来，函数内又转回去——一次「先拆后装」的历史包袱。
- [mmdet3d/models/layers/box3d_nms.py:L281-L295](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/box3d_nms.py#L281-L295) —— `nms_normal_bev`：一行核心 `nms(boxes[:, :-1], ...)` 直接**丢弃 yaw 列**，退化成轴对齐 2D NMS（mmcv 的 `nms`）。
- [mmdet3d/models/layers/box3d_nms.py:L156-L179](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/box3d_nms.py#L156-L179) —— `aligned_3d_nms` 的贪心循环：`argsort` 升序（L156），循环里每次取最后一个即最高分（L160）；L171-L175 手写三轴体积交；**L177 `iou * (classes1 == classes2)` 是类别感知的关键**——不同类的框 IoU 直接归零，永不互相抑制。
- [mmdet3d/models/layers/box3d_nms.py:L204-L223](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/box3d_nms.py#L204-L223) —— `circle_nms`：numba `nopython` 加速的 CPU 循环；L219 `dist = (x1[i]-x1[j])**2 + (y1[i]-y1[j])**2`，L222 `if dist <= thresh: suppressed[j] = 1`——**没有开方，thresh 与平方距离直接比较**。官方测试 [tests/test_models/test_layers/test_box3d_nms.py:L63-L76](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_models/test_layers/test_box3d_nms.py#L63-L76) 中 thresh=0.175 时第 0 框（与第 1 框中心相距 √0.0418≈0.204m）被抑制，证实比较对象是平方距离。
- [mmdet3d/models/dense_heads/centerpoint_head.py:L751-L782](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/centerpoint_head.py#L751-L782) —— CenterPoint 的选择开关：`test_cfg.nms_type` 只允许 `'circle'` 或 `'rotate'`；circle 分支取框中心 (x, y) 拼分数喂 `circle_nms`（L761-L769，注意 `.cpu().numpy()`——numba 只吃 numpy），rotate 分支走 `get_task_detections` → [L872-L881](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/centerpoint_head.py#L872-L881) 的 `nms_bev`。
- [configs/_base_/models/centerpoint_pillar02_second_secfpn_nus.py:L74-L81](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/centerpoint_pillar02_second_secfpn_nus.py#L74-L81) —— `min_radius=[4, 12, 10, 1, 0.85, 0.175]` 按任务（类别组）配置 circle NMS 阈值，车辆大、行人小；启用 circle NMS 只需一行覆盖 `test_cfg=dict(pts=dict(nms_type='circle'))`（[configs/centerpoint/centerpoint_pillar02_second_secfpn_head-circlenms_8xb4-cyclic-20e_nus-3d.py:L3](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/centerpoint/centerpoint_pillar02_second_secfpn_head-circlenms_8xb4-cyclic-20e_nus-3d.py#L3)）。

**为什么 CenterPoint 偏爱 circle NMS？** 中心点范式的候选来自热图峰值，同一目标周围的几个峰**中心几乎重合**而框尺寸估计各异——按中心距离去重又快（免求交）又准（不依赖朝向/尺寸的估计质量），这也是 nuScenes 榜单的通用做法。anchor 系候选则天然分散在各 anchor 形状上，旋转 IoU 更合适。

#### 4.5.4 代码实践

1. **实践目标**：对同一组 BEV 框分别用旋转与轴对齐 NMS 去重，观察保留框差异（即大纲任务中 `rotated_nms_3d` vs `aligned_rotated_nms_3d` 的 1.x 现行版）。
2. **操作步骤**（示例代码，复用官方测试数据，需 GPU；数据取自 [tests/test_models/test_layers/test_box3d_nms.py:L85-L92](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_models/test_layers/test_box3d_nms.py#L85-L92)）：

   ```python
   import torch
   from mmdet3d.models.layers import nms_bev, nms_normal_bev

   boxes = torch.tensor([[6.0, 3.0, 8.0, 7.0, 2.0],    # yaw=2rad，显著旋转
                         [3.0, 6.0, 9.0, 11.0, 1.0],
                         [3.0, 7.0, 10.0, 12.0, 1.0],
                         [1.0, 4.0, 13.0, 7.0, 3.0]], dtype=torch.float32).cuda()
   scores = torch.tensor([0.6, 0.9, 0.7, 0.2], dtype=torch.float32).cuda()
   print('rotated  :', nms_bev(boxes, scores, thresh=0.3))
   print('axis-orig:', nms_normal_bev(boxes, scores, thresh=0.3))
   for thr in (0.1, 0.05):
       print(f'rotated thr={thr} :', nms_bev(boxes, scores, thresh=thr))
       print(f'aligned thr={thr} :', nms_normal_bev(boxes, scores, thresh=thr))
   ```

3. **需要观察的现象**：thr=0.3 时两者都返回 `[1, 0, 3]`（官方测试断言）；逐步调低阈值后，旋转 NMS 因计入 yaw 的真实重叠会更早抑制候选。
4. **预期结果**：thr=0.3 输出 `[1, 0, 3]`（有测试背书）；低阈值下的差异**待本地验证**。无 GPU 时可改跑 CPU 可用的 `aligned_3d_nms`（[tests/test_models/test_layers/test_box3d_nms.py:L54-L60](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_models/test_layers/test_box3d_nms.py#L54-L60)）与 `circle_nms`（L74-L76）。

#### 4.5.5 小练习与答案

**练习 1**：`aligned_3d_nms` 为什么要接收 `classes` 参数而 BEV 系 NMS 不需要？

**答案**：`aligned_3d_nms` 一次处理所有类的候选（室内头把多类框一起送入），用 L177 的类别相等指示把跨类 IoU 置 0 实现类内抑制；BEV 系的 `box3d_multiclass_nms` 则在更外层逐类循环调用 NMS，函数本身无需感知类别。

**练习 2**：nuScenes 配置里行人的 `min_radius=0.175`，实际抑制半径是多少米？

**答案**：\(\sqrt{0.175}\approx0.418\) 米——因为代码比较的是中心**平方**距离。配置时务必记住这层平方关系。

**练习 3**：`nms_bev` 的输入框格式是 `(x1,y1,x2,y2,ry)`，但 mmcv 的 `nms_rotated` 需要 `(cx,cy,w,h,ry)`，这中间发生了什么？

**答案**：见 [box3d_nms.py:L264-L269](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/box3d_nms.py#L264-L269)：函数内把对角形式换算回中心+宽高再调用算子；而调用方传入前又刚用 `xywhr2xyxyr` 做过反方向转换，一来一回是历史接口演进留下的冗余。

## 5. 综合实践

**任务：给「损失 + 去重」写一个统一的实验脚本。**

把本讲四块内容串成一个可复用的调试脚本 `u5l7_playground.py`（放在仓库外或 tutorial 目录，不要放进 `mmdet3d/`）：

1. **构造交叠框组**：复用 4.2 的 5 对单位立方体（RotatedIoU3DLoss，需 GPU）与 4.3 的对角点框（AxisAlignedIoULoss，CPU），分别打印逐框损失向量。
2. **角度损失**：复用 4.4 的 MultiBin 测试数据（CPU），断言输出 ≈ 2.1120；再做消融——复制 `multibin_loss.py` 到本地、把 L62 的 `return` 移出 for 循环，对比损失变化。
3. **NMS 对比**：用 4.5 的 4 个 BEV 框，在阈值网格 `{0.05, 0.1, 0.2, 0.3}` 上同时跑 `nms_bev` 与 `nms_normal_bev`，打印「阈值 × 方法 → 保留索引」对照表；CPU 机器改用 `aligned_3d_nms` + `circle_nms` 跑通同样流程。
4. **收尾验证**：运行 `pytest tests/test_models/test_losses/test_multibin_loss.py tests/test_models/test_layers/test_box3d_nms.py`，确认环境内损失与 NMS 行为与官方断言一致——CUDA 用例自带 `skipif` 标记（无 GPU 时自动跳过），`test_rotated_iou_loss` 无标记但会在函数开头 return，具体执行情况**待本地验证**。

预期成果：一张「同一组框在不同重叠度量下去重结果」的表格，以及一句话结论——什么时候旋转 IoU、什么时候中心距离更合适。

## 6. 本讲小结

- mmdet3d 原生损失是「无参 `nn.Module` 壳 + `@weighted_loss` 裸函数芯」的双层结构，weight/reduction/avg_factor 由装饰器统一处理，检测头以统一接口调用。
- `RotatedIoU3DLoss` = `1 - diff_iou_rotated_3d`，输入 7 维中心+尺寸+yaw 框、一一对应，**仅 CUDA 可用**；0.x 的纯 torch 实现（box_converter/permute）已下沉到 mmcv。
- `AxisAlignedIoULoss` 服务室内 6 维对角点框，纯 PyTorch 广播实现，支持 iou/giou 两模式；docstring 写 `[..., 3]` 是漂移，断言要求 6 维。
- `MultiBinLoss` 用「bin 二分类 + 单位圆 sin/cos 残差」化解角度周期性；其 `return` 缩进在 for 循环体内，实际只累计第一个 bin，且被单元测试的期望值 2.1120 锁定——读源码不能只信 docstring 与论文名。
- `box3d_nms.py` 五把刀：`box3d_multiclass_nms` 逐类调度、`nms_bev`（旋转）/`nms_normal_bev`（轴对齐）对应 0.x 的 rotated/aligned NMS、`aligned_3d_nms` 类别感知的室内体积 NMS、`circle_nms` 以中心平方距离替代 IoU（thresh 需按平方理解）。
- 选型直觉：anchor 系用旋转 BEV NMS，室内点云用轴对齐 3D NMS，中心点范式（CenterPoint 系）偏爱 circle NMS。

## 7. 下一步学习建议

- 本讲是 u5 单元（体素/柱体室外检测主线）的收尾：损失与 NMS 直接决定 mAP，下一步建议进入 **u7-l2（test.py 与评估指标体系）**，观察 `nms_thr`、`max_num`、`min_radius` 等本讲参数如何影响 NuScenesMetric 的 mAP/NDS。
- 想看 NMS 在完整推理链路中的位置，可回读 [mmdet3d/models/dense_heads/base_3d_dense_head.py:L318-L365](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/base_3d_dense_head.py#L318-L365)（anchor 系 `get_boxes`）与 [centerpoint_head.py:L725-L808](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/centerpoint_head.py#L725-L808)（CenterPoint 解码 + 双 NMS 分支）。
- 扩展阅读：`projects/TR3D/tr3d/rotated_iou_loss.py` 展示了如何在 `rotated_iou_3d_loss` 之上叠加 DIoU 模式——是 u8-l3 自定义损失的最佳模板。

# 超点池化：collate、scatter_mean 与特征组织

## 1. 本讲目标

学完本讲，你应该能够：

1. 讲清楚 `collate()` 如何把一个 batch 的点云列表变成稀疏卷积能吃的四件套：`coordinates / features / inverse_mapping / spatial_shape`，以及 MinkowskiEngine（下称 ME）的 `TensorField` 量化与 `inverse_mapping` 各自扮演什么角色。
2. 手工推导 `extract_feat()` 中一行 `scatter_mean(...)` 如何把「逐体素特征」变成「逐超点特征」，并写出每一步的张量形状（最终是 \((M, d)\)）。
3. 说明训练时 `elastic_coords` 如何在不破坏「点—标签对齐」的前提下改变坐标量化（体素划分），以及它与推理路径的差异。
4. 解释超点质心 `point_positions` 的计算方式、它为什么用原始米制坐标，以及它在解码器里被谁消费（FPS 聚合器）。

本讲是单元 3 的收官：u3-l1 讲了 `Grounded_LMM_main` 的整体链路与 `batch_offsets` 机制，u3-l2 讲了稀疏主干 SpConvUNet，本讲补上中间最关键的一跳——**从「几十万个点」到「几千个超点 token」的压缩是在哪里、怎么发生的**。这一跳正是 LMM 能够吃下 3D 场景的前提。

## 2. 前置知识

- **体素与稀疏卷积**：把连续点云按边长 `voxel_size` 的立方格（体素）划分，每个非空体素是稀疏张量中的一个「活跃位置」。u3-l2 已详细讲过 `SparseConvTensor` 四件套（features、indices、spatial_shape、batch_size），本讲直接使用。
- **超点（superpoint）**：数据集预先用几何聚类（如 SSTNet）把点云切成若干局部平滑的小块，每块一个编号。它有两重身份：
  - 作为**几何压缩单位**：逐点特征按超点取平均，得到逐超点特征，token 数从几十万降到几千；
  - 作为**标注粒度**：GT mask、`<SEG>` 查询的监督信号都在超点级（见 u2-l4）。
  每个点带一个超点编号，在代码里就是 `sp_pts_mask`（长度等于点数 N 的整型数组）。
- **scatter 操作（分组聚合）**：给定每个元素的组编号 \(g_i\)，把同组元素聚合为一个输出。`torch_scatter.scatter_mean` 做的是「分组求平均」：

  \[ \bar{x}_j = \frac{1}{|S_j|}\sum_{i \in S_j} x_i, \qquad S_j = \{\, i \mid g_i = j \,\} \]

  输出第 \(j\) 行是第 \(j\) 组的均值，组数决定输出行数。它出现在本项目的三个地方：GT mask 的多数投票（u2-l4）、本讲的超点池化、本讲的质心计算。
- **MinkowskiEngine（ME）**：另一个稀疏张量库。本项目用它的两个工具函数做「量化去重」：`ME.utils.batch_sparse_collate`（拼 batch）与 `ME.TensorField.sparse()`（坐标去重），而去重后的稀疏卷积仍由 spconv 完成——两个库在此分工协作。
- **弹性形变增强（elastic augmentation）**：给点云坐标加上平滑的随机位移，模拟扫描形变。本项目把它实现为「只生成一份加噪坐标副本」，这是 4.1 节的重点之一。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `uniseg3d/uniseg3d.py` | 本讲主战场：`Grounded_LMM_main.collate()`（L1028）、`extract_feat()`（L1005）、`loss()` 中的 `point_positions` 计算（L1164）与 `predict()` 中的对应段（L1296） |
| `uniseg3d/transforms_3d.py` | `ElasticTransfrom`（L1052）：生成加噪体素坐标副本 `elastic_coords` |
| `configs/ground3dlmm_train.py` | 关键参数：`in_channels=6`、`voxel_size=0.02`、`min_spatial_shape=128`（L31-L35），管线中 `ElasticTransfrom` 的配置（L144-L149） |
| `uniseg3d/query_decoder.py` | `point_positions` 的消费方：`FPSPositionAggregator` / `FPSAggregator` 的定义（L952-L955）与使用（L1360-L1366） |

建议打开 `uniseg3d/uniseg3d.py` 的 L1005-L1135 区间对照阅读——本讲 90% 的内容都发生在这 130 行里。

## 4. 核心概念与源码讲解

### 4.1 collate：把 batch 点云组装成稀疏张量

#### 4.1.1 概念说明

`loss()` / `predict()` 收到的 `batch_inputs_dict['points']` 是一个列表：每个场景一个 \((N_i, 6)\) 张量（前 3 列 XYZ、后 3 列 RGB，RGB 已被 `NormalizePointsColor_` 归一化到 \([-1,1]\)，见 u2-l3）。而稀疏卷积需要的输入是**一个**统一的稀疏张量。`collate()` 就是这个「多场景 → 一个稀疏张量」的组装器。

它要解决三个问题：

1. **量化**：XYZ 是连续浮点数，必须映射成整型体素索引，才能定义「哪些位置非空」。
2. **拼 batch**：多个场景要拼进同一个稀疏张量，且坐标不能互相冲突——经典做法是在坐标第 0 列加一个样本编号。
3. **可回溯**：量化会把落在同一体素内的多个点合并成一行，但后续的超点池化需要知道**每个原始点**落在哪个体素上——这就是 `inverse_mapping` 存在的意义。

#### 4.1.2 核心流程

以两个场景（batch_size=2）为例：

```
输入: points = [P0(N0,6), P1(N1,6)]，可选 elastic_points = [E0(N0,3), E1(N1,3)]

第 1 步 逐场景构造 (坐标, 特征) 对
  坐标: (XYZ - 场景内最小值) / voxel_size      # 平移到正象限 + 缩放到体素单位
        （若提供 elastic_coords：加噪坐标已除过 voxel_size，只做平移）
  特征: [RGB(3) | XYZ - 场景质心(3)]            # 共 6 维 = in_channels

第 2 步 ME.utils.batch_sparse_collate
  把各场景坐标量化为整型、第 0 列拼上样本编号 0/1
  → coordinates (N0+N1, 4)，features (N0+N1, 6)

第 3 步 TensorField 量化去重
  field = ME.TensorField(features, coordinates)
  tensor = field.sparse()                       # 坐标去重 → (V,4)/(V,6)，V ≤ N0+N1
  inverse_mapping = field.inverse_mapping(...)  # (N0+N1,) 每个输入点 → 所在体素行号

第 4 步 空间形状
  spatial_shape = clip(max(coordinates[:,1:]) + 1, 下限 128)   # 跳过第 0 列 batch 编号

输出: (coordinates, features, inverse_mapping, spatial_shape)
```

随后在 `loss()` 里用这四件套构造 spconv 的 `SparseConvTensor`（u3-l2 讲过）。

一个需要特别留意的**不对称**：非弹性分支的坐标除了 `voxel_size`，弹性分支没除——因为 `ElasticTransfrom` 在数据管线阶段就已经除过了（见 4.1.3 的对照）。

#### 4.1.3 源码精读

先看 `collate()` 主体（`Grounded_LMM_main` 的版本）：

[uniseg3d/uniseg3d.py:L1028-L1060](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1028-L1060) —— `collate()` 全文：分支选择、`batch_sparse_collate`、`TensorField` 量化与 `inverse_mapping` 的返回。

其中非弹性分支（推理路径也走这里）：

[uniseg3d/uniseg3d.py:L1041-L1045](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1041-L1045) —— 对每个场景 `p`：坐标 = `(p[:, :3] - p[:, :3].min(0)[0]) / self.voxel_size`（先平移到正象限，再除以 0.02m 换算成体素单位）；特征 = `torch.hstack((p[:, 3:], p[:, :3] - p[:, :3].mean(0)))`，即「归一化 RGB + 场景中心化坐标」共 6 维——这正是配置里 `in_channels=6` 的来源。

弹性分支（仅训练，`elastic_points` 来自 `batch_inputs_dict.get('elastic_coords', None)`）：

[uniseg3d/uniseg3d.py:L1046-L1050](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1046-L1050) —— 坐标换成 `el_p - el_p.min(0)[0]`（**只平移、不再除 `voxel_size`**），特征仍然从原始点 `p` 计算。注意这里 zip 的是 `(elastic_points, points)`：加噪坐标只决定「点落在哪个体素」，点的特征与所有逐点标签（超点编号、GT mask）保持基于原始点序的对齐。

量化去重与回溯映射：

[uniseg3d/uniseg3d.py:L1052-L1058](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1052-L1058) —— `spatial_shape` 取坐标最大值 +1（跳过第 0 列 batch 编号后按维度取长度），并用 `torch.clip` 保证下限 `min_spatial_shape=128`（给卷积核留足空间、避免退化场景）；`field = ME.TensorField(features, coordinates)` 后 `field.sparse()` 把重复体素合并成单行得到 `(V,4)` 坐标与 `(V,6)` 特征；`field.inverse_mapping(tensor.coordinate_map_key)` 返回 \((N_{total},)\) 的整型数组——第 \(i\) 个元素是第 \(i\) 个输入点所在体素在去重后张量中的行号。

再看调用侧——`loss()` 中的调用与 `predict()` 的差异：

[uniseg3d/uniseg3d.py:L1127-L1129](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1127-L1129) 与 [uniseg3d/uniseg3d.py:L1286-L1287](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1286-L1287) —— 训练时传入 `elastic_coords`（数据管线 `Pack` 的 keys 里包含它），推理时不传、走非弹性分支。两条路径共用同一个 `collate()`。

配套配置：

[configs/ground3dlmm_train.py:L31-L35](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L31-L35) —— `in_channels=6`（特征维度）、`voxel_size=0.02`（2cm 体素）、`min_spatial_shape=128`，与 `collate()` 内 `self.voxel_size`、`self.min_spatial_shape` 对应。

[uniseg3d/transforms_3d.py:L1069-L1084](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L1069-L1084) —— `ElasticTransfrom.transform()`：把当前点云坐标除以 `voxel_size`（L1079），以概率 `p` 施加两次弹性形变（L1080-L1082），结果写入新键 `elastic_coords`（L1083）。**它完全不修改 `points` 本身**，也不碰任何标签。注意它位于 `GlobalRotScaleTrans` 之后（管线顺序见 [configs/ground3dlmm_train.py:L106-L158](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L106-L158)），所以加噪是在旋转缩放增强之后的坐标上进行的。

[uniseg3d/transforms_3d.py:L1086-L1123](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L1086-L1123) —— `elastic()` 私有方法：生成三通道随机噪声，用三个方向的 \(1\times3\) 均值核对噪声做 6 次卷积平滑，再通过 `RegularGridInterpolator` 把噪声场插值到每个点上，最后 `x + noise * mag`。`gran` 控制噪声网格粗细（配置为 `[6, 20]` 体素），`mag` 控制位移幅度（`[40, 160]` 体素，即 0.8m 与 3.2m 的网格尺度扰动量级——第二次是粗粒度大幅扰动）。

关于「同一体素内多个点合并成一行时特征如何取舍」：`collate()` 的 docstring 写着「训练用随机采样、推理用无权重平均」，但函数签名里实际没有 `quantization_mode` 参数（这段注释继承自上游 SPFormer 实现），本仓库调用 `field.sparse()` 时也未传参，因此具体的合并策略由所装 ME 版本的默认行为决定——**待本地验证**（验证方法见 4.1.4）。

#### 4.1.4 代码实践

**实践目标**：不用 ME、不用跑模型，用纯 PyTorch 复现 `collate()` 的核心语义（拼 batch 列、坐标量化去重、求 `inverse_mapping`），验证你对这套机制的理解。

**操作步骤**（以下为「示例代码」，可在任意装有 PyTorch 的环境运行；本讲义编写环境无法执行，运行结果标注为「待本地验证」）：

```python
# 示例代码：手工模拟 collate() 的量化去重与 inverse_mapping
import torch

voxel_size = 0.02  # 与 configs/ground3dlmm_train.py 一致

# 两个场景，各 4 个点：(x, y, z, r, g, b)
p0 = torch.tensor([
    [0.000, 0.000, 0.000, 0.1, 0.2, 0.3],
    [0.021, 0.000, 0.000, 0.4, 0.5, 0.6],   # 与第 0 个点同属一个体素(边界 0.02)
    [0.050, 0.000, 0.000, 0.7, 0.8, 0.9],
    [0.000, 0.030, 0.000, 0.2, 0.3, 0.4],
])
p1 = torch.tensor([
    [0.000, 0.000, 0.000, 0.0, 0.0, 0.0],
    [0.100, 0.100, 0.100, 1.0, 1.0, 1.0],
    [0.100, 0.100, 0.100, 0.5, 0.5, 0.5],   # 与上一行完全同体素
    [0.200, 0.000, 0.000, 0.3, 0.3, 0.3],
])
points = [p0, p1]

# 第 1 步：逐场景 (坐标, 特征)，完全照抄 collate() 的非弹性分支
pairs = []
for b, p in enumerate(points):
    coords = (p[:, :3] - p[:, :3].min(0)[0]) / voxel_size   # 平移 + 缩放到体素单位
    feats = torch.hstack((p[:, 3:], p[:, :3] - p[:, :3].mean(0)))
    pairs.append((coords, feats))

# 第 2 步：模拟 ME.utils.batch_sparse_collate —— 整数化 + 第 0 列拼样本编号
coordinates = torch.cat(
    [torch.cat([torch.full((c.shape[0], 1), float(b)), c], dim=1)
     for b, (c, f) in enumerate(pairs)], dim=0).int()        # (8, 4)
features = torch.cat([f for c, f in pairs], dim=0)           # (8, 6)

# 第 3 步：模拟 TensorField().sparse() 的坐标去重
uniq, inverse_mapping = torch.unique(
    coordinates, dim=0, return_inverse=True, sorted=True)    # uniq:(V,4) inverse:(8,)

# 第 4 步：模拟 spatial_shape（跳过 batch 列、+1 变长度、clip 下限 128）
spatial_shape = torch.clip(uniq[:, 1:].max(0)[0] + 1, 128)

print('coordinates:\n', coordinates)
print('去重后体素数 V =', uniq.shape[0])
print('inverse_mapping =', inverse_mapping.tolist())
print('spatial_shape =', spatial_shape.tolist())
# 聚合去重后的体素特征（这里用“取第一个点”代替 ME 的合并策略）
voxel_feats = torch.zeros_like(features)
voxel_feats[inverse_mapping] = features
print('体素特征 (V, 6):\n', voxel_feats[torch.unique(inverse_mapping)])
```

**需要观察的现象**：

1. `inverse_mapping` 中，p0 的第 0、1 两个点（坐标 0.000 与 0.021，除以 0.02 后取整都落在体素索引 1 上，取决于取整方向）与 p1 的第 5、6 两个点应当分别得到**相同的行号**——这就是「多点合一体素」。
2. `V < 8`：8 个点只产生少于 8 个非空体素。
3. `spatial_shape` 恒为 `[128, 128, 128]`：因为这个小场景远小于 128，被 clip 下限托底。

**预期结果**：`V = 6`（p0 贡献 3 个体素、p1 贡献 3 个体素）；`inverse_mapping` 形如 `[0, 0, 1, 2, 3, 4, 4, 5]`（p1 两点合一体素）。具体行号取决于取整方向，但「同体素同行号」这一性质不变。若你环境里装好了 MinkowskiEngine，可把第 2、3 步替换为 `ME.utils.batch_sparse_collate(pairs)` + `ME.TensorField(features=..., coordinates=...).sparse()` 对拍，顺带验证 4.1.3 末尾提出的「重复合并策略」问题（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么弹性分支的坐标不再除以 `voxel_size`？

**答案**：因为 `ElasticTransfrom.transform()` 在数据管线阶段已经执行了 `coords / self.voxel_size`（transforms_3d.py L1079），`elastic_coords` 本来就是体素单位的坐标；`collate()` 的弹性分支只需再减去每场景最小值做平移。若在 `collate()` 里再除一次，体素会放大 50 倍（等效 voxel_size 变为 1m），量化会完全失真。

**练习 2**：`spatial_shape` 的计算里为什么是 `[1:]` 而不是取全部列的最大值？为什么需要 `+1` 和下限 128？

**答案**：`coordinates` 第 0 列是 batch 编号，不是空间坐标，算形状时必须跳过；坐标是索引（从 0 开始），最大索引 +1 才是该维长度；`min_spatial_shape=128` 是防御性下限，避免点数极少的样本产生过小的张量，导致多层下采样（步幅 16 的 U-Net）后特征图退化甚至为负尺寸。

**练习 3**：如果删掉 `ElasticTransfrom`（或把 `p` 设为 0），训练还能跑吗？会发生什么变化？

**答案**：能跑。`loss()` 里 `batch_inputs_dict.get('elastic_coords', None)` 会返回 None，`collate()` 自动走非弹性分支（用原始坐标做量化）。损失的是一种数据增强：模型每个 epoch 看到同一场景的体素划分完全相同，可能加重对固定量化边界的过拟合。`p=0.5` 的含义就是每个样本有 50% 概率施加弹性扰动。

---

### 4.2 extract_feat：scatter_mean 超点池化

#### 4.2.1 概念说明

u3-l2 讲过：SpConvUNet 输出的是**逐体素**的 32 维特征（与输入活跃位置一致）。但 LMM 的 token 预算只有几千，几十万个体素显然塞不进去。超点池化就是把「体素级特征」压成「超点级特征」的那一步，物理上等价于：**对每个超点内部所有点的特征取平均，得到这个超点的单一表示**。

这一步是整个架构的「咽喉」：

- 上游是密集的几何计算（稀疏卷积，逐体素）；
- 下游是稀疏的语义计算（Qwen3-VL 与 Mask2Former 式解码器，逐超点 token）；
- 每个超点恰好对应语言模型里的一个 `<|point|>` token（u4-l1 将详述）。

#### 4.2.2 核心流程

`extract_feat()` 的四步：

```
输入: x = SparseConvTensor(features:(V,6) → UNet 前已被 input_conv 升到 (V,32))
      superpoints = 全局拼接的超点编号 (N_total,)   # u3-l1 讲过的 batch_offsets 全局化
      inverse_mapping = (N_total,)
      batch_offsets = [0, M_0, M_0+M_1, ...]        # 长度 B+1

第 1 步 input_conv:  SubMConv3d 6→32 维（不改变活跃位置）
第 2 步 unet:        稀疏 U-Net，输出 (V, 32) 逐体素特征
第 3 步 output_layer: BatchNorm1d + ReLU
第 4 步 超点池化（本讲核心）:
        x.features[inverse_mapping]   (V,32) → (N_total,32)   # 体素特征“广播”回每个点
        scatter_mean(..., superpoints) (N_total,32) → (M_total,32)
第 5 步 按 batch_offsets 切回逐场景列表: [(M_0,32), (M_1,32), ...]
```

形状演化的推导。设 batch 共 B 个场景，第 \(i\) 个场景有 \(N_i\) 个点、\(M_i\) 个超点，体素化后全 batch共 \(V\) 个非空体素，则：

\[ N_{total} = \sum_i N_i, \qquad M_{total} = \sum_i M_i \]

- `x.features`：\((V, 32)\)，\(V \le N_{total}\)（同体素的点被合并）；
- `x.features[inverse_mapping]`：\((N_{total}, 32)\)，第 \(k\) 行 = 第 \(k\) 个点所在体素的特征。这一步是**从体素级回到点级**的精确还原，不引入任何信息损失——同体素的点拿到相同向量；
- `scatter_mean(feat, superpoints, dim=0)`：按全局超点编号分组取均值，得 \((M_{total}, 32)\)。每个超点 \(j\) 的特征是

  \[ f_j = \frac{1}{|P_j|}\sum_{k \in P_j} f^{(point)}_k, \qquad P_j = \{\, k \mid \text{superpoints}[k] = j \,\} \]

- 按 `batch_offsets` 切片后，第 \(i\) 个场景得到 \((M_i, 32)\)——因为 u3-l1 讲过的偏移机制已把各场景超点编号平铺进 \([0, M_{total})\) 的全局区间，切片即还原场景归属。

注意两次「降采样」的性质不同：点 → 体素的合并发生在 ME 量化内部（隐式、由 `inverse_mapping` 记录），体素 → 超点的平均发生在这行 `scatter_mean`（显式、可导）。反向看，评估时 `mask[:, superpoints]`（超点级预测 → 点级 mask）用的正是同一份逐点编号做「升采样」，一降一升构成闭环（u6-l1 的指标计算依赖这一点）。

#### 4.2.3 源码精读

[uniseg3d/uniseg3d.py:L1005-L1026](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1005-L1026) —— `extract_feat()` 全文（`Grounded_LMM_main` 版本）。

[uniseg3d/uniseg3d.py:L1019-L1021](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1019-L1021) —— 三段网络依次执行：`input_conv`（SubMConv3d，把 6 维升到 `num_channels=32`，配置见 [configs/ground3dlmm_train.py:L9](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L9)）、`self.unet`（u3-l2 的稀疏 U-Net）、`output_layer`（BN + ReLU）。

[uniseg3d/uniseg3d.py:L1022](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1022) —— 本讲最重要的一行：

```python
x = scatter_mean(x.features[inverse_mapping], superpoints, dim=0)
```

先用 `inverse_mapping` 把 \((V,32)\) 的体素特征「广播」回 \((N_{total},32)\) 的点级，再按 `superpoints` 分组求均值得到 \((M_{total},32)\)。`scatter_mean` 来自 `torch_scatter`（见文件头部 [uniseg3d/uniseg3d.py:L6](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L6) 的 `from torch_scatter import scatter_mean`）。

[uniseg3d/uniseg3d.py:L1023-L1026](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1023-L1026) —— 按 `batch_offsets` 把 \((M_{total},32)\) 切回逐场景列表并返回。这个列表就是传给解码器的 `point_feats`/`mask_feats`（超点特征既当 LMM 的 point token，也当分割解码器的 K/V，u1-l1 讲过这个双路设计）。

调用侧的输入准备（`superpoints` 的全局化在 u3-l1 已详述，这里只给链接）：

[uniseg3d/uniseg3d.py:L1111-L1123](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1111-L1123) —— `loss()` 用 `superpoint_bias` 累计偏移生成 `batch_offsets` 并平铺各场景 `sp_pts_mask`。

[uniseg3d/uniseg3d.py:L1131-L1135](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1131-L1135) —— 用 `collate()` 的输出构造 `SparseConvTensor`，`torch.hstack(sp_pts_masks)` 得到 \((N_{total},)\) 的全局超点编号，随后调用 `extract_feat()`。

顺带一提，`predict()` 走的是同一段 `extract_feat`（[uniseg3d/uniseg3d.py:L1289-L1293](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1289-L1293)），唯一差异在 `collate()` 不传 `elastic_coords`——训练与推理的 3D 特征提取只差「坐标是否被弹性扰动」这一点。

#### 4.2.4 代码实践

**实践目标**：用一个小到可以手算的例子，验证 `scatter_mean` 的「分组平均」语义，并确认 `features[inverse_mapping]` 的形状还原逻辑。

**操作步骤**（「示例代码」，依赖 `torch` 与 `torch_scatter`；若没有 `torch_scatter`，注释里给了纯 PyTorch 等价实现。运行结果标注为「待本地验证」）：

```python
# 示例代码：验证 extract_feat 中 scatter_mean 一行的语义
import torch
from torch_scatter import scatter_mean
# 没有 torch_scatter 时的等价实现:
# def scatter_mean(src, index, dim=0):
#     out = torch.zeros(int(index.max()) + 1, src.shape[1])
#     out.index_add_(0, index, src)
#     out /= torch.bincount(index).clamp(min=1).unsqueeze(1).float()
#     return out

# 模拟：8 个点、6 个体素、2 个场景共 5 个超点（已全局化编号）
inverse_mapping = torch.tensor([0, 0, 1, 2, 3, 4, 4, 5])   # 4.1.4 的产物
superpoints     = torch.tensor([0, 0, 1, 1, 2, 3, 3, 4])   # 逐点超点编号(全局)
voxel_feats     = torch.arange(6 * 2, dtype=torch.float).view(6, 2)  # (V=6, d=2)

# 完全照抄 uniseg3d.py L1022
point_feats = voxel_feats[inverse_mapping]        # (8, 2)：同体素的点拿同一向量
sp_feats    = scatter_mean(point_feats, superpoints, dim=0)

print('体素特征:\n', voxel_feats)
print('点级特征 (8,2):\n', point_feats)
print('超点特征 (5,2):\n', sp_feats)

# 手工核对超点 0（点 0、1，体素都是 0）：均值应为 voxel_feats[0]
assert torch.allclose(sp_feats[0], voxel_feats[0])
# 手工核对超点 4（只有点 7，体素 5）：均值应为 voxel_feats[5]
assert torch.allclose(sp_feats[4], voxel_feats[5])
# 手工核对超点 1（点 2 在体素 1、点 3 在体素 2）：均值应为两体素均值
assert torch.allclose(sp_feats[1], (voxel_feats[1] + voxel_feats[2]) / 2)
print('全部断言通过')
```

**需要观察的现象**：

1. `point_feats` 中行号相同的行（如同为体素 0 的第 0、1 行）完全一样；
2. `sp_feats` 的形状是 \((5, 2)\)——行数等于超点编号的最大值 +1，与点数、体素数都无关；
3. 断言逐条通过，确认均值是「先取点所在体素的特征、再对组内求平均」。

**预期结果**：打印 `(5, 2)` 的超点特征与「全部断言通过」。请特别留意：若把 `superpoints` 改成非连续编号（如 `[0, 0, 5, 5, ...]`），`scatter_mean` 仍会为中间空缺的编号 1-4 生成**全零行**——这就是为什么 u3-l1 的偏移机制必须保证全局编号连续无空洞（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：`x.features[inverse_mapping]` 这一步能不能省略，直接对 `x.features` 做 `scatter_mean(x.features, superpoints_voxel, dim=0)`（按「体素的超点」分组）？

**答案**：理论上可以——需要先为每个体素求出所属超点，再按体素聚合，结果近似（权重从「每点一票」变成「每体素一票」）。但当前实现选择回到点级再平均，好处是**复用同一份逐点 `sp_pts_mask`**：它与数据管线里所有逐点标注（GT mask、评估时的上采样）共享同一索引基准，不需要额外推导「体素 → 超点」的映射，也不依赖 ME 合并策略的具体行为。

**练习 2**：一个场景有 15 万个点、3 万个体素、2500 个超点。`extract_feat` 输出的形状是多少？token 压缩比是多少？

**答案**：输出 \((2500, 32)\)（单场景时 `batch_offsets=[0, 2500]`，切片后就是整个张量）。相对逐体素的压缩比是 \(30000/2500 = 12\) 倍，相对逐点是 \(150000/2500 = 60\) 倍。若超点数超过 `max_point_tokens=2000`，训练时还会被 4.3 节的 FPS 聚合器进一步压到 2000。

**练习 3**：训练与推理的 `extract_feat` 有几处不同？

**答案**：就一处：`collate()` 传入的坐标不同——训练可能使用弹性扰动后的 `elastic_coords` 做量化（改变体素划分），推理用原始坐标。`input_conv → unet → output_layer → scatter_mean → 切片` 这条链路本身完全相同。

---

### 4.3 超点质心 point_positions 的计算与去向

#### 4.3.1 概念说明

超点池化后，每个超点有了一个 32 维语义特征，但**丢掉了显式的几何位置**——一个超点平均特征不含「它在哪里」。而语言分支在把超点特征转成 point token 前，可能需要做一次**基于位置**的降采样（FPS，最远点采样）：当场景超点数超过 `max_point_tokens=2000` 时，优先保留空间上分布均匀的超点子集，而不是随机丢。为此模型需要每个超点的三维坐标，即**超点质心**（该超点内所有点 XYZ 的均值）。

`point_positions` 就是这个逐超点质心列表。它在 `loss()` 与 `predict()` 中各计算一次，随后作为参数传入解码器。

#### 4.3.2 核心流程

```
输入: batch_inputs_dict['points'] = [P0(N0,6), P1(N1,6), ...]
      sp_pts_masks = 全局拼接的超点编号 (N_total,)
      batch_offsets = [0, M_0, M_0+M_1, ...]

第 1 步 raw_positions = vstack([p[:, :3] for p in points])   # (N_total, 3) 原始米制坐标
第 2 步 sp_centroids = scatter_mean(raw_positions, sp_pts_masks, dim=0)  # (M_total, 3)
第 3 步 point_positions = 按 batch_offsets 切片 → [(M_0,3), (M_1,3), ...]

下游（query_decoder.infer_reason_train）:
若 self.training 且 超点数 > max_point_tokens(2000):
    point_positions 可用且长度匹配 → FPSPositionAggregator（按几何距离 FPS）
    否则                          → FPSAggregator（按特征距离 FPS）
```

与 4.2 的特征池化完全同构——同一个 `scatter_mean`、同一份 `sp_pts_masks`，只是被平均的对象从 32 维特征换成了 3 维坐标。数学上质心为

\[ c_j = \frac{1}{|P_j|}\sum_{k \in P_j} \, p_k^{xyz} \]

一个容易忽略的细节：这里的 XYZ 是**原始米制坐标**（没有除 `voxel_size`、没有减最小值），因为 FPS 需要真实的欧氏距离来保证采样点在物理空间上均匀分布；而 4.1 里 `collate()` 的坐标是体素索引，两者用途不同、尺度不同。

#### 4.3.3 源码精读

[uniseg3d/uniseg3d.py:L1164-L1167](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1164-L1167) —— `loss()` 中的三行：`torch.vstack` 取各场景原始坐标前三列拼成 \((N_{total},3)\)（并 `.to(sp_pts_masks.device)` 对齐设备）；`scatter_mean` 按全局超点编号求质心得 \((M_{total},3)\)；按 `batch_offsets` 切成逐场景列表。注释写明用途是 "position-aware aggregation"。

[uniseg3d/uniseg3d.py:L1296-L1299](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1296-L1299) —— `predict()` 中同样三行（注意推理时虽然也计算质心，但见下面消费方的 `self.training` 条件，推理实际不会触发 FPS 聚合）。

[uniseg3d/uniseg3d.py:L1172](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1172) —— 质心列表随 `point_positions=point_positions` 传入解码器（`predict()` 的对应调用在 L1305）。

消费方的定义：

[uniseg3d/query_decoder.py:L952-L955](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L952-L955) —— 解码器初始化中定义两个聚合器：`point_aggregator = FPSAggregator(...)`（按特征距离采样）与 `pos_aggregator = FPSPositionAggregator(in_channels=d_model, out_tokens=self.max_point_tokens, pos_dim=3, temperature=0.5)`（按位置采样，`out_tokens` 即配置里的 `max_point_tokens=2000`，见 [configs/ground3dlmm_train.py:L76](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L76)）。

消费方的触发逻辑：

[uniseg3d/query_decoder.py:L1343-L1368](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1343-L1368) —— `infer_reason_train` 逐场景循环：L1348 取出该场景的 `i_point_pos`（`None if point_positions is None else point_positions[i]`）；L1360 的条件 `if self.training and n_points > self.max_point_tokens:` 决定是否聚合——**仅在训练且超点数超限时**；L1361-L1363 若质心可用且行数与超点数一致，调用 `pos_aggregator`（基于位置的 FPS）；L1364-L1366 否则退回 `point_aggregator`（基于特征的 FPS）。聚合后 L1370 的 `point2token` 把超点特征投影为语言模型的 point token——这是 u4-l2 的内容。

这里有一个值得体会的设计：**推理时不聚合**。评估希望每个超点都被 LMM 看到（哪怕 token 多一些），训练时才为控制显存与序列长度而降采样。

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：完整跟踪 `point_positions` 的生命周期——从哪里计算、以什么形状传给谁、在什么条件下被消费，把 4.3 的调用链亲手走一遍。

**操作步骤**：

1. 打开 [uniseg3d/uniseg3d.py:L1164-L1167](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1164-L1167)，确认三行代码输入（`points`、`sp_pts_masks`、`batch_offsets`）与输出（`point_positions` 列表）的形状。
2. 顺着 L1172 的 `self.decoder(x, scene_names=..., qa_data=..., point_positions=point_positions)` 跳到解码器的 `forward`（[uniseg3d/query_decoder.py:L1962-L1976](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1962-L1976)），再进入 `forward_iter_pred`（L1853）→ `infer_reason_train`（L1325）。
3. 在 `infer_reason_train` 里找到 L1348（取出本场景质心）与 L1360-L1366（触发条件与两条聚合路径），回答下面「需要观察的现象」中的问题。
4. 对比 `predict()` 侧（[uniseg3d/uniseg3d.py:L1296-L1299](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1296-L1299) 与解码器 `infer_reason_eval`，L1686 起）：确认推理路径虽然计算并传递了 `point_positions`，但不存在 `self.training` 分支下的 FPS 聚合调用。

**需要观察的现象 / 思考题**：

1. `i_point_pos.shape[0] == n_points` 这个防御性检查（L1361）防的是什么？——提示：质心行数由 `scatter_mean` 输出决定，理论上恒等于该场景超点数；若上游改动了超点编号（比如出现空洞），两者就会失配，此时退回特征 FPS 而不是崩溃。
2. `pos_aggregator` 与 `point_aggregator` 的区别是什么时候重要的？——超点数 > 2000 的大型场景（ScanNet++ 中不少场景超点数在两三千以上）。

**预期结果**：你应当能不看讲义复述这条链：`loss() 算质心 → decoder(point_positions=...) → forward_iter_pred → infer_reason_train 训练且超限 → FPSPositionAggregator 降到 2000 token`。

#### 4.3.5 小练习与答案

**练习 1**：为什么质心计算用 `p[:, :3]`（原始坐标），而不是 `collate()` 里已经算好的体素坐标？

**答案**：FPS 采样依据的是欧氏距离，需要真实的物理尺度（米）。体素坐标是「平移到正象限后除以 0.02」的整数索引，虽然等比例放大了 50 倍（距离关系不变），但逐场景平移量不同、且丢失了米制直觉；更关键的是 `collate()` 的坐标在训练时可能被弹性扰动过，用它算质心会把增强噪声泄漏进「位置先验」。直接用原始点坐标最简单也最稳妥。

**练习 2**：`sp_centroids` 的行数由什么决定？如果某个超点编号在 `sp_pts_masks` 中不存在（空洞），质心矩阵会怎样？

**答案**：行数 = `sp_pts_masks.max() + 1`。出现空洞时，`scatter_mean` 会为缺失编号生成全零行（分母计数为 0 时的行为）——对应 4.2.4 末尾提到的现象。这也是 u3-l1 的偏移机制必须让全局超点编号**连续无空洞**的又一个原因：否则 `point_positions[i]` 与该场景超点特征 `point_feats[i]` 的行数对不上，触发 L1361 的防御分支。

**练习 3**：假设把 `max_point_tokens` 从 2000 调到 4000，哪些地方会受影响？

**答案**：直接受影响的是两个 FPS 聚合器的 `out_tokens`（query_decoder.py L954-L955，越少触发聚合、越多超点直接进 LMM）；间接影响是语言序列变长——注意力与 KV 显存约按 token 数平方/线性增长，训练显存占用上升、吞吐下降；但 3D 侧（collate、UNet、scatter_mean 池化）完全不受影响，因为压缩发生在「池化之后、进 LMM 之前」。这个参数是 3D 侧与语言侧之间的预算旋钮。

---

## 5. 综合实践

**任务**：把本讲三个模块串成一个端到端的最小演示——用两个合成场景，走完「collate（含弹性分支）→ extract_feat 的 scatter_mean → point_positions」，在每个检查点打印形状，最终产出一个手工可核对的结果表。

以下为「示例代码」（纯 PyTorch，不含 ME/UNet——UNet 用恒等映射代替，因为我们验证的是池化与坐标组织，不是卷积本身）。运行结果标注为「待本地验证」。

```python
# 示例代码：超点池化端到端最小演示（batch=2）
import torch
from torch_scatter import scatter_mean

torch.manual_seed(0)
voxel_size, min_spatial_shape = 0.02, 128

# ---- 构造两个场景：场景 0 有 5 点 2 超点，场景 1 有 4 点 2 超点 ----
N0, N1, M0, M1 = 5, 4, 2, 2
points = [torch.rand(N0, 6), torch.rand(N1, 6)]            # (N,6): XYZ+RGB
sp_local = [torch.tensor([0, 0, 1, 1, 0]),                 # 场景 0 的逐点超点编号
            torch.tensor([0, 1, 1, 1])]                    # 场景 1 的逐点超点编号
elastic = [torch.rand(N0, 3) * 50, torch.rand(N1, 3) * 50] # 假装是 ElasticTransfrom 的产物(体素单位)

# ---- u3-l1 的偏移机制：全局化超点编号 + batch_offsets ----
batch_offsets, sp_all, bias = [0], [], 0
for sp in sp_local:
    sp_all.append(sp + bias)
    bias = (sp + bias).max().item() + 1
    batch_offsets.append(bias)
sp_pts_masks = torch.hstack(sp_all)                        # (N_total,)
M_total = bias
print(f'batch_offsets={batch_offsets}, M_total={M_total}')

# ---- 模块 4.1：collate（弹性分支 + 非弹性特征），手工版 ----
coords = torch.cat([torch.cat([torch.full((c.shape[0],1), float(b)), c - c.min(0)[0]], 1)
                    for b, c in enumerate(elastic)]).int() # (N_total,4)
feats = torch.cat([torch.hstack((p[:,3:], p[:,:3] - p[:,:3].mean(0))) for p in points])
uniq, inverse_mapping = torch.unique(coords, dim=0, return_inverse=True, sorted=True)
spatial_shape = torch.clip(uniq[:,1:].max(0)[0] + 1, min_spatial_shape)
print(f'量化: N_total={coords.shape[0]} → V={uniq.shape[0]}, '
      f'inverse_mapping={inverse_mapping.tolist()}, spatial_shape={spatial_shape.tolist()}')

# ---- 模块 4.2：extract_feat（UNet 用恒等映射代替，维度当作 6）----
voxel_feats = feats[torch.unique(inverse_mapping)]         # (V,6) “体素特征”
x = scatter_mean(voxel_feats[inverse_mapping], sp_pts_masks, dim=0)   # (M_total,6)
out = [x[batch_offsets[i]: batch_offsets[i+1]] for i in range(len(batch_offsets)-1)]
print(f'超点池化: (V,6)→(M_total,6)={tuple(x.shape)}, 切分后 '
      f'{[tuple(o.shape) for o in out]}')

# ---- 模块 4.3：point_positions 质心 ----
raw_pos = torch.vstack([p[:, :3] for p in points])
sp_centroids = scatter_mean(raw_pos, sp_pts_masks, dim=0)
point_positions = [sp_centroids[batch_offsets[i]: batch_offsets[i+1]]
                   for i in range(len(batch_offsets)-1)]
print(f'质心: (N_total,3)→(M_total,3)={tuple(sp_centroids.shape)}')

# ---- 手工核对：场景 0 超点 0 = 点 0,1,4 的特征均值 ----
ids0 = (sp_local[0] == 0).nonzero().squeeze(1)
manual = feats[:N0][ids0].mean(0)
assert torch.allclose(out[0][0], manual, atol=1e-5), '超点特征手算不一致!'
assert torch.allclose(point_positions[0][0], points[0][ids0][:, :3].mean(0), atol=1e-6)
print('场景0超点0 的特征与质心 手工核对通过')
```

**预期结果**（形状部分是确定的，数值部分待本地验证）：

| 检查点 | 预期形状 |
|---|---|
| `coords` | \((9, 4)\) |
| `inverse_mapping` | \((9,)\)，V ≤ 9 |
| `spatial_shape` | \([128, 128, 128]\) |
| 池化输出 `x` | \((4, 6)\)，即 \(M_{total}=4\) |
| 切分 `out` | \([(2,6), (2,6)]\) |
| `sp_centroids` / `point_positions` | \((4,3)\) / \([(2,3),(2,3)]\) |

两条断言通过即说明：超点特征确实是「组内点特征的平均」、质心确实是「组内点坐标的平均」。完成后，建议再回到真实源码对照：把 `voxel_feats` 那一行换成真的 `input_conv + unet + output_layer`（需要装好 u1-l2 的完整环境），其余代码不用改——这正是 `extract_feat` 的结构。

## 6. 本讲小结

- `collate()` 完成三件事：逐场景构造「体素坐标 + 6 维特征（RGB + 中心化 XYZ）」、用 `ME.utils.batch_sparse_collate` 拼 batch（第 0 列样本编号）、用 `ME.TensorField.sparse()` 去重得到 \((V,4)\) 坐标并返回 `inverse_mapping` 记录「每个点 → 所在体素」的回溯关系。
- 训练专用的 `elastic_coords` 只替换量化坐标（且在 `ElasticTransfrom` 中已除过体素长，`collate` 只做平移），点特征与全部逐点标签保持原始对齐——增强只改变「分组方式」，不改变「点的内容」。
- 超点池化一行 `scatter_mean(x.features[inverse_mapping], superpoints, dim=0)` 先把体素特征广播回点级、再按全局超点编号分组求均值，形状链为 \((V,32) → (N_{total},32) → (M_{total},32)\)，最后按 `batch_offsets` 切回逐场景。
- `point_positions` 用同一个 `scatter_mean` 对**原始米制坐标**求逐超点质心，仅当训练且超点数超过 `max_point_tokens=2000` 时被 `FPSPositionAggregator` 消费（按几何距离做 FPS 降采样）；推理路径计算但不使用。
- 点 → 体素的合并是 ME 量化的隐式行为（`inverse_mapping` 记录），体素/点 → 超点的平均是显式可导的 `scatter_mean`；评估时 `mask[:, superpoints]` 用同一份编号反向升采样，一降一升构成闭环。

## 7. 下一步学习建议

本讲之后，3D 侧（单元 3）就完整了：数据管线（单元 2）→ 主入口与 batch_offsets（u3-l1）→ 稀疏主干（u3-l2）→ 超点池化（本讲）。下一讲进入单元 4 的 LMM 侧，推荐顺序：

1. **u4-l1（点云 token 注入）**：本讲的输出 `out`（逐场景 \((M_i,32)\) 超点特征）在那里被 `point2token` 投影并 masked_scatter 到 Qwen3-VL 的 `<|point|>` token 位置——本讲 4.3.3 提到的 `max_point_tokens` 预算正是在那一步兑现。
2. **u4-l2（解码器初始化与 LoRA）**：看 `FPSAggregator`/`FPSPositionAggregator` 的完整实现与 `point2token`/`query_proj` 的维度变换。
3. 带着本讲的问题去读：超点特征同时作为 LMM 的输入 token 与分割解码器的 K/V（u1-l1 的双路设计），在 u5-l1 的解码器前向里观察这两条路如何汇合。

如果想在真实数据上验证本讲内容，推荐在跑通 u1-l4 的推理后，在 `extract_feat` 的 L1022 前后各加一行 `print` 打印 `x.features.shape` 与池化后形状（只加日志、不改逻辑），对照本讲第 5 节的形状表。

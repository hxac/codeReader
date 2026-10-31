# 稀疏卷积主干 SpConvUNet

## 1. 本讲目标

上一讲（u3-l1）我们把 `Grounded_LMM_main.loss()` / `predict()` 两条链路走通了，其中都出现了同样三行：

```python
x = self.input_conv(x)
x, _ = self.unet(x)
x = self.output_layer(x)
```

当时留了一个悬念：`x, _ = self.unet(x)` 里被 `_` 丢掉的第二个返回值到底是什么？本讲就钻进 `self.unet`（即 `SpConvUNet`）内部，把这个问题连同整个稀疏卷积主干讲透。学完本讲，你应该能够：

1. 解释 `SparseConvTensor` 的四个组成部分，以及为什么点云必须用"稀疏"方式做卷积。
2. 读懂 `ResidualBlock` 的 pre-norm 设计、恒等分支与 `indice_key` 复用机制。
3. 画出 `num_planes=[32,64,96,128,160]` 对应的五级 U-Net 结构图，说出每一层的通道数、分辨率与 `indice_key`。
4. 说明 `return_blocks=True` 时多尺度返回值的组织方式，并理解为什么本项目中它"必须为 True、但多尺度特征又被丢弃"。

## 2. 前置知识

### 2.1 从点云到体素

u2-l3/u3-l1 已经讲过：一个场景的点云是 \( N \times 6 \) 的张量（XYZ + RGB）。`collate()` 会把坐标除以 `voxel_size=0.02`（2 厘米）并取整，把连续点云切进一个个 2cm 的小立方体——**体素（voxel）**。一个室内场景动辄十几万个点，但落到 2cm 精度后，占用的体素只占整个包围盒的极小一部分（墙、地板、家具之间的大量空气体素是空的）。

### 2.2 稠密卷积为什么不可行

如果直接用 `nn.Conv3d`，就必须把整个包围盒存成稠密张量。以一个 5m×4m×3m 的房间、`voxel_size=0.02` 为例：网格是 250×200×150 ≈ 7500 万体素；32 通道 float32、batch=2 就要约 192 MB——而这只是一个场景，其中超过九成的体素永远是 0。稀疏卷积（sparse convolution）只对**非空体素**存储和计算：

\[ \text{稠密内存} = B \times X \times Y \times Z \times C \qquad \text{vs} \qquad \text{稀疏内存} = V \times (C + 4) \]

其中 \( V \) 是活跃（非空）体素数，通常只有十万量级；多出的 4 是每个体素要额外存 4 个整数坐标（batch 索引 + 3 个空间索引）。

### 2.3 本讲登场的三种稀疏算子（spconv 库）

| 算子 | 作用 | 活跃位置变化 |
|---|---|---|
| `SubMConv3d` | 子流形（submanifold）卷积：只在已有活跃体素上做卷积，输出位置=输入位置 | 不变 |
| `SparseConv3d`（可设 stride） | 普通稀疏卷积：kernel 覆盖到输入活跃体素的位置都会产生输出 | 变（下采样时合并、变稀） |
| `SparseInverseConv3d` | 逆卷积：把某个 `SparseConv3d` 的输出映射回它的输入位置 | 变（上采样，与配对的下采样严格互逆） |

### 2.4 U-Net 与 pre-norm

- **U-Net**：经典的"编码器—解码器"结构。编码器逐级下采样（分辨率降低、通道数增加、感受野扩大），解码器用逆卷积逐级上采样还原分辨率，并把编码器同分辨率的特征**跳连（skip connection）**拼接过来，兼顾语义与细节。
- **pre-norm / post-norm**：残差块里"归一化+激活"放在卷积**前**还是**后**。pre-norm（先 Norm 再 ReLU 再 Conv）让恒等路径不经过任何变换，训练更稳，是 Transformer 与 pre-activation ResNet 的常见选择。本项目默认 `normalize_before=True`，即 pre-norm。
- **indice_key**：spconv 的"规则缓存"钥匙。稀疏卷积每次前向都要生成"输入体素 ↔ 输出体素"的索引规则（rule）；相同 `indice_key` 且输入布局相同的卷积可以**复用同一份规则**，省去重复计算。细节在 4.2 讲。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| [uniseg3d/spconv_unet.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/spconv_unet.py) | 本讲主角，全文仅 237 行：`ResidualBlock`（稀疏残差块）+ `SpConvUNet`（递归 U-Net）。文件首行注明改编自 SPFormer |
| [uniseg3d/uniseg3d.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py) | 调用方：`Grounded_LMM_main` 的 `_init_layers`（input_conv/output_layer）、`extract_feat`（调用链）与 `collate`（构造稀疏张量） |
| [configs/ground3dlmm_train.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py) | `backbone` 配置（`num_planes`、`return_blocks`）与 `voxel_size`、`min_spatial_shape` 等超参，以及 `backbone` 的 lr 分层与 `load_from` 预训练权重 |

另外，所有 eval 配置（如 [configs/ground3dlmm_eval_ground3d_scannet.py:L40-L43](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet.py#L40-L43)）中的 backbone 配置与训练配置完全一致，本讲内容对训练/推理两条链路同样成立。

## 4. 核心概念与源码讲解

### 4.1 SparseConvTensor：稀疏世界的"张量"

#### 4.1.1 概念说明

`SparseConvTensor` 是 spconv 库对"稀疏 3D 张量"的封装，四个要素：

| 成员 | 形状 | 含义 |
|---|---|---|
| `features` | \( V \times C \) | \( V \) 个活跃体素的特征（本模型里 C 从 6 开始） |
| `indices` | \( V \times 4 \) | 每个活跃体素的坐标，**第 0 列是 batch 索引**，后 3 列是空间坐标 |
| `spatial_shape` | 3 个整数 | 网格边界声明（只用于界定范围，**不会**分配稠密内存） |
| `batch_size` | 标量 | 批内样本数，配合第 0 列区分体素属于哪个场景 |

关键直觉：`spatial_shape` 只是"栅栏"，防止跨步卷积把坐标算出界；真正占内存的只有 `features` 和 `indices`。多条场景点云拼进**同一个**稀疏张量（batch 维），卷积只在同 batch 索引内部聚合，这就是 u3-l1 讲过的"多场景拼接"在 3D 主干里的形态。

#### 4.1.2 核心流程

以 `loss()` 为例，主干输入的诞生过程：

1. `collate()` 把每个场景的点云坐标 `(XYZ - min) / voxel_size` 得到体素坐标，特征拼成 `[RGB, XYZ - mean]` 共 6 通道，交给 MinkowskiEngine 的 `batch_sparse_collate` 批量组装并去重。
2. `spatial_shape` 取坐标最大值 +1，并用 `min_spatial_shape=128` 托底。
3. 用 `features + coordinates + spatial_shape + batch_size` 四件套构造 `SparseConvTensor`，交给 `extract_feat`。

（ME 的量化、去重与 `inverse_mapping` 属于下一讲 u3-l3 的主题，这里只需知道"进主干的已经是一个稀疏张量"。）

#### 4.1.3 源码精读

- [uniseg3d/uniseg3d.py:L1041-L1053](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1041-L1053)：`collate()` 中坐标与特征的构造——坐标除以 `voxel_size` 变体素索引，特征是 `RGB` 拼上"减去均值的 XYZ"（RGB 已在数据管线里归一化到 [-1,1]，见 u2-l3）；`spatial_shape = clip(坐标最大值+1, 128)`。

```python
coordinates, features = ME.utils.batch_sparse_collate(
    [((p[:, :3] - p[:, :3].min(0)[0]) / self.voxel_size,
      torch.hstack((p[:, 3:], p[:, :3] - p[:, :3].mean(0))))
     for p in points])
...
spatial_shape = torch.clip(
    coordinates.max(0)[0][1:] + 1, self.min_spatial_shape)
```

- [uniseg3d/uniseg3d.py:L1131-L1132](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1131-L1132)：`loss()` 里四件套组装成 `SparseConvTensor`，`batch_size` 就是 `len(batch_data_samples)`：

```python
x = spconv.SparseConvTensor(
    features, coordinates, spatial_shape, len(batch_data_samples))
```

- [configs/ground3dlmm_train.py:L31-L35](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L31-L35)：三个与稀疏张量直接相关的超参——`in_channels=6`（RGB+XYZ）、`voxel_size=0.02`（2cm 体素）、`min_spatial_shape=128`。代码没有注释 `min_spatial_shape` 的动机，一个合理推断是：五级 U-Net 有 4 次 stride-2 下采样，128 托底保证最深层网格仍有 \( 128/16 = 8 \) 的边长，避免过深下采样后网格退化。

#### 4.1.4 代码实践

**实践目标**：手工构造一个最小 `SparseConvTensor`，把"四件套"摸一遍。

1. 操作步骤（示例代码，需 u1-l2 装好的环境；spconv 2.x 的这些基础操作支持 CPU，无 GPU 也可做）：

```python
# 示例代码：非项目原有文件
import torch
import spconv.pytorch as spconv

V = 5
# 第 0 列 batch 索引，后 3 列空间坐标（必须非负、在 spatial_shape 内）
indices = torch.tensor([
    [0, 0, 0, 0], [0, 5, 5, 5], [0, 9, 9, 9],
    [1, 0, 0, 0], [1, 3, 3, 3]], dtype=torch.int32)
features = torch.randn(V, 6)
x = spconv.SparseConvTensor(features, indices, [16, 16, 16], batch_size=2)
print(x.features.shape, x.indices.shape, x.spatial_shape, x.batch_size)
```

2. 需要观察的现象：`features` 是 `(5, 6)`、`indices` 是 `(5, 4)`，`spatial_shape=[16,16,16]` 只是个边界声明。
3. 预期结果：打印 `torch.Size([5, 6]) torch.Size([5, 4]) [16, 16, 16] 2`；对比稠密方式需要 `2×16×16×16×6` 个浮点数，稀疏方式只存了 5 行。
4. 若与你本地 spconv 版本的行为不一致（如要求 indices 为 int32 之外的 dtype），以报错信息为准，待本地验证。

#### 4.1.5 小练习与答案

1. **问**：把 `voxel_size` 从 0.02 改成 0.04，活跃体素数和显存会怎么变？有什么代价？
   **答**：每维体素数减半，体素总数约按 \( 1/8 \) 缩减，显存与计算量随之下降；代价是空间分辨率变粗，椅腿、把手等细小结构的几何细节会被合并掉。
2. **问**：`spatial_shape` 为什么必须显式给出，而不能从 `features` 推导？
   **答**：`features`/`indices` 只记录活跃位置，不含边界信息；而 stride-2 的 `SparseConv3d` 输出网格大小取决于输入网格大小，必须有边界声明才能计算。
3. **问**：`indices` 第 0 列为什么是 batch 索引？
   **答**：多个场景拼进同一个稀疏张量时，用它区分体素属于哪个样本，保证卷积只在场景内部聚合、不跨样本串味。

### 4.2 ResidualBlock：pre-norm 稀疏残差块

#### 4.2.1 概念说明

`ResidualBlock` 是 U-Net 的最小积木：两个 3×3×3 的 `SubMConv3d` 加一条恒等跳连。两个设计要点：

- **为什么敢直接逐行相加**：`SubMConv3d` 是子流形卷积，输出活跃位置与输入完全相同，所以主支（conv_branch）输出的第 i 行与恒等支的第 i 行对应**同一个体素**，`features` 可以直接按行相加。
- **pre-norm**：`normalize_before=True`（默认）时结构为 `Norm → ReLU → Conv3×3 → Norm → ReLU → Conv3×3`，末尾不再接 Norm/ReLU——恒等路径完全"干净"，与 Transformer 的 pre-norm 残差同思路。

**indice_key 复用**：块内两个 `SubMConv3d` 共用同一个 `indice_key`。spconv 首次执行时要为卷积生成"输入/输出索引规则"；同 key、同输入布局的卷积直接复用这份规则。因为 SubM 卷积不改变活跃位置，同一层里 `blocks`（2 个块 × 各 2 个卷积）、`blocks_tail`（2 个块）共 8 个 3×3 卷积全部共用 `subm{id}` 一份规则，只算一次、复用七次。更妙的是，模型级的 `input_conv` 也用了 `'subm1'`（见 4.4.3），与 level 0 的所有块共享规则。

#### 4.2.2 核心流程

```
输入 SparseConvTensor (V, C_in)
  ├─ identity = 浅拷贝(共享 features 引用)
  ├─ conv_branch: [Norm, ReLU, SubMConv3×3(C_in→C_out), Norm, ReLU, SubMConv3×3(C_out→C_out)]   ← pre-norm
  │     （两个卷积都带 indice_key）
  └─ i_branch: C_in == C_out ? Identity : SubMConv1×1(C_in→C_out)                               ← 通道不匹配时投影
输出 = replace_feature(conv_branch.features + i_branch(identity).features)      # (V, C_out)，位置不变
```

注意 `i_branch` 的 1×1 卷积**没有**传 `indice_key`：1×1 子流形卷积不需要邻居聚合，无需规则。

#### 4.2.3 源码精读

- [uniseg3d/spconv_unet.py:L13-L38](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/spconv_unet.py#L13-L38)：类定义与恒等分支——通道一致用 `Identity`，不一致用 1×1 `SubMConv3d` 投影：

```python
if in_channels == out_channels:
    self.i_branch = spconv.SparseSequential(nn.Identity())
else:
    self.i_branch = spconv.SparseSequential(
        spconv.SubMConv3d(
            in_channels, out_channels, kernel_size=1, bias=False))
```

- [uniseg3d/spconv_unet.py:L40-L56](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/spconv_unet.py#L40-L56)：pre-norm 主支，两个 3×3 卷积共用同一个 `indice_key`：

```python
if normalize_before:
    self.conv_branch = spconv.SparseSequential(
        norm_fn(in_channels), nn.ReLU(),
        spconv.SubMConv3d(
            in_channels, out_channels, kernel_size=3, padding=1,
            bias=False, indice_key=indice_key), norm_fn(out_channels), nn.ReLU(),
        spconv.SubMConv3d(
            out_channels, out_channels, kernel_size=3, padding=1,
            bias=False, indice_key=indice_key))
```

（[L57-L72](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/spconv_unet.py#L57-L72) 是 post-norm 对照分支，本项目配置未覆盖 `normalize_before`，走的是上面的 pre-norm。）

- [uniseg3d/spconv_unet.py:L74-L91](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/spconv_unet.py#L74-L91)：前向——先包一个共享 features 的 `identity` 张量，再相加；`replace_feature` 只换特征、保留索引等元数据，避免整块拷贝：

```python
identity = spconv.SparseConvTensor(input.features, input.indices,
                                   input.spatial_shape, input.batch_size)
output = self.conv_branch(input)
output = output.replace_feature(output.features +
                                self.i_branch(identity).features)
```

#### 4.2.4 代码实践

**实践目标**：验证"SubM 链不改变活跃位置"与参数量公式。

1. 操作步骤（示例代码）：

```python
# 示例代码：非项目原有文件
import torch, functools
import torch.nn as nn
import spconv.pytorch as spconv
from uniseg3d.spconv_unet import ResidualBlock   # 直接导入，不经注册表

torch.manual_seed(0)
indices = torch.randint(0, 16, (300, 4)); indices[:, 0] = 0
indices = torch.unique(indices, dim=0).int()      # 坐标必须唯一
x = spconv.SparseConvTensor(torch.randn(len(indices), 32),
                            indices, [16, 16, 16], 1)
blk = ResidualBlock(32, 64, functools.partial(nn.BatchNorm1d, eps=1e-4, momentum=0.1),
                    indice_key='subm1')
blk.eval()
y = blk(x)
print('in/out voxels:', x.features.shape[0], y.features.shape[0])
print('indices equal:', torch.equal(x.indices, y.indices))
print('params:', sum(p.numel() for p in blk.parameters()))
```

2. 需要观察的现象：输入输出体素数相同、`indices` 完全一致；输出通道是 64。
3. 预期结果：`indices equal: True`。参数量可手算验证（卷积均 `bias=False`，BN 每层 weight+bias 共 \( 2C \) 个）：
   \[ \underbrace{27\,C_{in}C_{out} + 27\,C_{out}^2}_{\text{两个 }3\times3\times3\text{ 卷积}} + \underbrace{C_{in}C_{out}}_{\text{i 分支 }1\times1} + \underbrace{2C_{in} + 2C_{out}}_{\text{两组 BN}} \]
   代入 32→64 得 \( 27\times32\times64 + 27\times64\times64 + 32\times64 + 96 + 128 = 168\,128 \)；同法可得 32→32 块（i 分支为 Identity）为 \( 2\times27\times32\times32 + 128 = 55\,424 \)。请以本地 `params` 打印为准核对。
4. 若想感受 indice_key 复用，可在块前向前后分别打印 `blk.conv_branch` 内卷积对象的信息，对比第二次前向的耗时（待本地验证）。

#### 4.2.5 小练习与答案

1. **问**：为什么主支与恒等支可以直接按行相加？
   **答**：两个卷积都是 `SubMConv3d`，子流形卷积输出活跃位置=输入活跃位置，两支的第 i 行对应同一体素。
2. **问**：pre-norm 与 post-norm 的结构差异是什么？本项目默认哪个？
   **答**：pre-norm 是 `Norm→ReLU→Conv→Norm→ReLU→Conv`（末尾无激活），post-norm 是 `Conv→Norm→ReLU→Conv→Norm→ReLU`。本项目默认 `normalize_before=True`，即 pre-norm，且配置未覆盖。
3. **问**：32→32 与 32→64 两种块的 `i_branch` 有何不同？
   **答**：前者是 `nn.Identity`（零参数）；后者是 1×1×1 `SubMConv3d(32→64, bias=False)` 投影，且不带 `indice_key`（1×1 无需邻居规则）。

### 4.3 递归 U-Net：下采样、上采样与跳连拼接

#### 4.3.1 概念说明

`SpConvUNet` 没有用"层列表"堆 U-Net，而是**用递归表达 U 形**：构造函数里 `self.u = SpConvUNet(num_planes[1:], ...)`，把通道列表砍掉第一个再建一个自己——`num_planes` 的长度就是递归深度，`indice_key_id` 逐层 +1。每个非最深层拥有五件套：

| 子模块 | 算子 | 作用 |
|---|---|---|
| `blocks` | `ResidualBlock×2`（subm 键） | 本分辨率精炼特征 |
| `conv` | `SparseConv3d` k=2, s=2（spconv 键） | 下采样 /2，通道 ×下一级 |
| `u` | 递归子 U-Net | 处理更粗一级 |
| `deconv` | `SparseInverseConv3d`（**同一个** spconv 键） | 上采样 ×2，逆映射回编码器位置 |
| `blocks_tail` | `ResidualBlock×2`（同 subm 键） | 融合跳连后的特征 |

最深层（`len(num_planes)==1`）只有 `blocks`，即 U 形的底部。

**deconv 与 conv 必须配对同一个 `indice_key`**：`SparseInverseConv3d` 不自己找上采样位置，而是复用配对的 `SparseConv3d`（同 key）在正向时生成的索引规则做精确逆映射。这保证上采样回来的体素位置与编码器该层**逐一对应**，随后的 `torch.cat` 跳连拼接才是对齐的。

#### 4.3.2 核心流程

带 `num_planes=[32,64,96,128,160]` 的完整结构图（★ = 写入 `previous_outputs` 的多尺度输出，见 4.4）：

```
输入 (V, 6)
  │ input_conv: SubMConv3d 6→32, k=3, indice_key='subm1'        ← uniseg3d.py:_init_layers
  ▼ (V, 32)
Level 0 (1×)   blocks×2 [32→32, subm1] ─ identity(32) ─────────┐ concat(64)  → blocks_tail [64→32→32]  ★ → output (V, 32)
    │ conv [32→64, spconv1, stride2]                           │ deconv [64→32, spconv1]
    ▼                                                          │
Level 1 (1/2)  blocks×2 [64→64, subm2] ─ identity(64) ─────────┐ concat(128) → blocks_tail [128→64→64] ★
    │ conv [64→96, spconv2]                                    │ deconv [96→64, spconv2]
    ▼                                                          │
Level 2 (1/4)  blocks×2 [96→96, subm3] ─ identity(96) ─────────┐ concat(192) → blocks_tail [192→96→96] ★
    │ conv [96→128, spconv3]                                   │ deconv [128→96, spconv3]
    ▼                                                          │
Level 3 (1/8)  blocks×2 [128→128, subm4] ─ identity(128) ──────┐ concat(256) → blocks_tail [256→128→128] ★
    │ conv [128→160, spconv4]                                  │ deconv [160→128, spconv4]
    ▼                                                          │
Level 4 (1/16) blocks×2 [160→160, subm5]   （最深层：只有 blocks）                                                    ★
```

几个可推导的量化结论：

- 4 次下采样，总步幅 \( 2^4 = 16 \)；最深层体素边长 \( 0.02 \times 16 = 0.32 \) 米，感受野覆盖整个房间级物体。
- `blocks_tail` 首块输入通道 = `num_planes[0] * 2`（identity 与 deconv 输出各占一半），代码写作 `num_planes[0] * (2 - i)`，`i=0` 时 2 倍、`i=1` 时 1 倍——这也**隐含假定了 `block_reps=2`**（默认值，配置未覆盖）。
- 上采样路径的通道是"对称的"：deconv 输出 = 本层 `num_planes[0]`，与 identity 拼接后由 `blocks_tail` 收回本层通道。

#### 4.3.3 源码精读

- [uniseg3d/spconv_unet.py:L94-L138](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/spconv_unet.py#L94-L138)：`SpConvUNet` 注册进 MODELS（配置里 `type='SpConvUNet'` 的解析目标，见 u1-l3）；`blocks` 是本层的 2 个残差块，全部用 `subm{indice_key_id}`：

```python
blocks = {
    f'block{i}': block(
        num_planes[0], num_planes[0], norm_fn,
        normalize_before=normalize_before,
        indice_key=f'subm{indice_key_id}')
    for i in range(block_reps)
}
```

- [uniseg3d/spconv_unet.py:L140-L169](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/spconv_unet.py#L140-L169)：下采样卷积（k=2、s=2，pre-norm 版本）与**递归构造**——通道列表砍头、key id +1，`return_blocks` 一路透传：

```python
self.conv = spconv.SparseSequential(
    norm_fn(num_planes[0]), nn.ReLU(),
    spconv.SparseConv3d(
        num_planes[0], num_planes[1],
        kernel_size=2, stride=2, bias=False,
        indice_key=f'spconv{indice_key_id}'))
...
self.u = SpConvUNet(
    num_planes[1:], norm_fn, block_reps, block,
    indice_key_id=indice_key_id + 1,
    normalize_before=normalize_before,
    return_blocks=return_blocks)
```

- [uniseg3d/spconv_unet.py:L171-L199](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/spconv_unet.py#L171-L199)：上采样用 `SparseInverseConv3d`，`indice_key` 与同层 `conv` 完全相同（`spconv{indice_key_id}`）——这是逆映射能对齐编码器位置的关键；`blocks_tail` 首块吃 `num_planes[0] * (2 - i)` 通道：

```python
self.deconv = spconv.SparseSequential(
    norm_fn(num_planes[1]), nn.ReLU(),
    spconv.SparseInverseConv3d(
        num_planes[1], num_planes[0],
        kernel_size=2, bias=False,
        indice_key=f'spconv{indice_key_id}'))
...
for i in range(block_reps):
    blocks_tail[f'block{i}'] = block(
        num_planes[0] * (2 - i),
        num_planes[0], norm_fn,
        indice_key=f'subm{indice_key_id}',
        normalize_before=normalize_before)
```

- [uniseg3d/spconv_unet.py:L201-L227](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/spconv_unet.py#L201-L227)：前向——先存 identity，再"下采样→递归→上采样"，最后拼接 + `blocks_tail`：

```python
output = self.blocks(input)
identity = spconv.SparseConvTensor(output.features, output.indices,
                                   output.spatial_shape, output.batch_size)
if len(self.num_planes) > 1:
    output_decoder = self.conv(output)
    if self.return_blocks:
        output_decoder, previous_outputs = self.u(output_decoder, previous_outputs)
    else:
        output_decoder = self.u(output_decoder)
    output_decoder = self.deconv(output_decoder)
    output = output.replace_feature(
        torch.cat((identity.features, output_decoder.features), dim=1))
    output = self.blocks_tail(output)
```

#### 4.3.4 代码实践

**实践目标**：不跑前向，仅靠递归结构打印出上面那张五级结构表，验证 4.3.2 的每个数字。

1. 操作步骤（示例代码）：

```python
# 示例代码：非项目原有文件
import uniseg3d  # noqa: F401  触发注册（见 u1-l3）
from mmdet3d.registry import MODELS

unet = MODELS.build(dict(type='SpConvUNet',
                         num_planes=[32 * (i + 1) for i in range(5)],
                         return_blocks=True))
lvl, depth = unet, 0
while True:
    children = [n for n, _ in lvl.named_children()]
    print(f'level {depth}: planes={lvl.num_planes} children={children}')
    if hasattr(lvl, 'u'):
        lvl, depth = lvl.u, depth + 1
    else:
        break
```

2. 需要观察的现象：5 行输出；level 0–3 的 children 依次是 `blocks, conv, u, deconv, blocks_tail`；level 4 只有 `blocks`。
3. 预期结果：`level 4: planes=[160] children=['blocks']`；level 0 的 `planes` 以 `[32, ...]` 开头。还可顺手打印 `sum(p.numel() for p in unet.parameters())` 得到主干参数量（具体数值待本地验证）。
4. 思考题式验证：对照输出确认 `blocks_tail` 首块输入通道（可再遍历 `lvl.blocks_tail` 的各块 `in_channels`，应分别为 64/32、128/64、192/96、256/128）。

#### 4.3.5 小练习与答案

1. **问**：`blocks_tail` 的首块输入通道为什么是 `2 × num_planes[0]`？
   **答**：跳连拼接了 identity（`num_planes[0]`）与 deconv 输出（`num_planes[0]`），通道翻倍；代码用 `num_planes[0] * (2 - i)` 表达（i=0 → 2 倍，i=1 → 1 倍），隐含 `block_reps=2`。
2. **问**：`deconv` 的 `indice_key` 为什么必须与同层 `conv` 相同？
   **答**：`SparseInverseConv3d` 复用配对 `SparseConv3d`（同 key）生成的索引规则做精确逆映射，从而上采样结果与编码器该层位置一一对应，跳连 `torch.cat` 才对齐。
3. **问**：`num_planes` 长度为 5，为什么只有 4 次下采样？
   **答**：构造函数里只有 `len(num_planes) > 1` 时才建 `conv/u/deconv/blocks_tail`；最深层 `[160]` 只剩 `blocks`，即 U 形底部。

### 4.4 return_blocks 多尺度返回与调用方式：那个被丢弃的返回值

#### 4.4.1 概念说明

`return_blocks=True` 时，`SpConvUNet.forward` 返回**二元组** `(output, previous_outputs)`：

- `output`：本层（level 0）的最终输出，**全分辨率**、32 通道、活跃位置与输入完全一致（整条 SubM 链不改位置）。
- `previous_outputs`：各级"解码侧"输出的列表。由于递归先在深层完成、`append` 在每层 `forward` 尾部执行，列表顺序是**从最深层到最浅层**：`[L4(160ch, 1/16), L3(128ch, 1/8), L2(96ch, 1/4), L1(64ch, 1/2), L0(32ch, 1×)]`，长度 5。

关键事实（本讲最重要的"务实认知"）：**`Grounded_LMM_main.extract_feat` 写的是 `x, _ = self.unet(x)`——多尺度列表被原样丢弃**，只消费全分辨率的 `output`（随后 `output_layer` 再 `scatter_mean` 池化成超点特征，见 u3-l3）。这一接口与写法继承自 UniSeg3D/SPFormer 一系（[uniseg3d/uniseg3d.py:L377](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L377) 的 `UniSeg3D` 与 [L1473](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1473) 的 `UniSeg3D_Reason` 同样如此）。

但注意：**配置里的 `return_blocks=True` 不能随手改成 False**。`extract_feat` 按二元组解包，若 `return_blocks=False`，`forward` 只返回单个 `SparseConvTensor`，`x, _ = ...` 的解包会直接失败（`SparseConvTensor` 不是可迭代二元组；具体报错信息待本地验证）。所以这个 True 在本项目里是**接口必需**，而非"多尺度特征被下游使用"。

#### 4.4.2 核心流程

主干的完整调用链（`Grounded_LMM_main.extract_feat`）：

```
SparseConvTensor (V, 6)
  ① input_conv   : SubMConv3d 6→32, k=3, indice_key='subm1'      # 与 level0 的块共享 subm1 规则
  ② self.unet    : 五级 U-Net → (output(V,32), previous_outputs×5)  # 后者被 _ 丢弃
  ③ output_layer : BatchNorm1d(32) + ReLU
  ④ scatter_mean : features[inverse_mapping] 按超点池化 → List[Tensor]  # 下一讲 u3-l3 的主题
```

配套的两个工程细节：

- **预训练初始化**：[configs/ground3dlmm_train.py:L222](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L222) 的 `load_from = 'work_dirs/pretrained/sstnet_scannet.pth'` 加载主干预训练权重（README 称之为 point-encoder initialization）。
- **差分学习率**：[configs/ground3dlmm_train.py:L200-L206](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L200-L206) 用 `paramwise_cfg.custom_keys` 按参数名分层：`backbone`→0.01、`reason`→0.1、`decoder`→1.0。留意一个细节：mmengine 按参数名（`re.search`）匹配，而本模型主干的参数名以 `unet.`、`input_conv.`、`output_layer.` 开头，并不含 `backbone` 字样——`'backbone'` 这个 key 很可能是从上游继承的"死键"，主干实际按基础 lr 训练。这一推断值得在训练时打印 optimizer 各 param group 验证（待本地验证）。

#### 4.4.3 源码精读

- [uniseg3d/spconv_unet.py:L229-L236](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/spconv_unet.py#L229-L236)：多尺度返回的落点。`NOTE: to avoid the residual bug` 指的是最深层首次进入时 `previous_outputs` 还是 `None`，必须先置空表再 append：

```python
if self.return_blocks:
    # NOTE: to avoid the residual bug
    if previous_outputs is None:
        previous_outputs = []
    previous_outputs.append(output)
    return output, previous_outputs
else:
    return output
```

- [uniseg3d/uniseg3d.py:L991-L1002](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L991-L1002)：`_init_layers` 的两个内联小层。注意 `input_conv` 的 `indice_key='subm1'` 与 level 0 的块同名——同分辨率、同布局，规则直接复用：

```python
self.input_conv = spconv.SparseSequential(
    spconv.SubMConv3d(
        in_channels, num_channels, kernel_size=3, padding=1,
        bias=False, indice_key='subm1'))
self.output_layer = spconv.SparseSequential(
    torch.nn.BatchNorm1d(num_channels, eps=1e-4, momentum=0.1),
    torch.nn.ReLU(inplace=True))
```

- [uniseg3d/uniseg3d.py:L1005-L1026](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1005-L1026)：`extract_feat` 全貌——本讲的 ①②③ 步加第 ④ 步池化：

```python
x = self.input_conv(x)
x, _ = self.unet(x)
x = self.output_layer(x)
x = scatter_mean(x.features[inverse_mapping], superpoints, dim=0)
out = []
for i in range(len(batch_offsets) - 1):
    out.append(x[batch_offsets[i]: batch_offsets[i + 1]])
return out
```

- [configs/ground3dlmm_train.py:L43-L46](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L43-L46)：backbone 配置，`num_planes` 由 [L9](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L9) 的 `num_channels = 32` 推导为 `[32, 64, 96, 128, 160]`：

```python
backbone=dict(
    type='SpConvUNet',
    num_planes=[num_channels * (i + 1) for i in range(5)],
    return_blocks=True),
```

#### 4.4.4 代码实践

**实践目标**：亲手接住那个"被丢弃的返回值"，验证多尺度列表的顺序、通道与位置不变性。

1. 操作步骤（示例代码）：

```python
# 示例代码：非项目原有文件
import torch
import spconv.pytorch as spconv
import uniseg3d  # noqa: F401
from mmdet3d.registry import MODELS

torch.manual_seed(0)
unet = MODELS.build(dict(type='SpConvUNet',
                         num_planes=[32 * (i + 1) for i in range(5)],
                         return_blocks=True)).eval()

indices = torch.randint(0, 64, (20000, 4)); indices[:, 0] = torch.randint(0, 2, (20000,))
indices = torch.unique(indices, dim=0).int()          # 去重，体素坐标必须唯一
x = spconv.SparseConvTensor(torch.randn(len(indices), 32), indices,
                            [64, 64, 64], batch_size=2)
with torch.no_grad():
    out, prev = unet(x)

print('final out:', tuple(out.features.shape))        # 期望 (V, 32)
print('positions preserved:', torch.equal(out.indices, x.indices))
print('prev channels:', [tuple(p.features.shape) for p in prev])
```

2. 需要观察的现象：`out.features` 形状为 `(V, 32)` 且 `out.indices` 与输入逐行相同；`prev` 长度 5。
3. 预期结果：`positions preserved: True`；`prev channels` 依次为 `(V4,160)→(V3,128)→(V2,96)→(V1,64)→(V0,32)`，其中 V4 < V3 < V2 < V1 ≤ V0（体素数逐级递减，具体数值待本地验证——稀疏下采样的合并程度取决于随机坐标分布）。
4. 把配置改成 `return_blocks=False` 再调用 `unet(x)`，观察返回值变成单个 `SparseConvTensor`——此时你就明白 `extract_feat` 里 `x, _ = ...` 为什么必然报错（具体报错信息待本地验证）。

#### 4.4.5 小练习与答案

1. **问**：配置里 `return_blocks=True` 但 `extract_feat` 写 `x, _ = self.unet(x)`，这矛盾吗？
   **答**：不矛盾但"名不副实"：True 使 `forward` 返回二元组，满足解包语法（改成 False 解包会直接失败）；多尺度列表本身被 `_` 丢弃，项目只消费全分辨率输出。该接口继承自 SPFormer/UniSeg3D。
2. **问**：`previous_outputs` 的顺序和各级通道数是什么？
   **答**：append 发生在每层 `forward` 尾部而递归先返回深层，所以顺序为最深层→最浅层，通道 `[160, 128, 96, 64, 32]`，长度 5。
3. **问**：若想真正利用多尺度特征，改动面在哪里？
   **答**：让 `extract_feat` 接住 `prev` 并逐级池化/对齐后与全分辨率特征融合，再交给下游；同时需要 decoder 接口配合，并注意 `sstnet_scannet.pth` 预训练权重的键名兼容。

## 5. 综合实践：打印一棵"稀疏金字塔"

把本讲四个模块串起来：从注册表构建与配置一致的 `SpConvUNet`，用 forward hook 记录每一级下采样/上采样后的活跃体素数，输出一张稀疏金字塔表，并验证 4.4 的全部结论。

（示例代码，非项目原有文件；建议存为 `spconv_tour.py` 在仓库根目录运行，勿提交。spconv 2.x 基础算子支持 CPU，无 GPU 亦可；若遇 CPU 不支持的算子则需 GPU 环境验证。）

```python
import torch
import spconv.pytorch as spconv
import uniseg3d  # noqa: F401  触发注册
from mmdet3d.registry import MODELS

torch.manual_seed(0)
unet = MODELS.build(dict(
    type='SpConvUNet',
    num_planes=[32 * (i + 1) for i in range(5)],
    return_blocks=True)).eval()

# 1) 造一个 2 样本、64^3 网格的稀疏输入（模拟 input_conv 之后的 32 通道张量）
idx = torch.randint(0, 64, (30000, 4)); idx[:, 0] = torch.randint(0, 2, (30000,))
idx = torch.unique(idx, dim=0).int()
x = spconv.SparseConvTensor(torch.randn(len(idx), 32), idx, [64, 64, 64], 2)

# 2) 给每级的 conv（下采样）与 deconv（上采样）挂 hook，记录体素数与通道数
rows = []
def mk_hook(name):
    def hook(mod, inp, out):
        rows.append((name, out.features.shape[0], out.features.shape[1]))
    return hook
for name, mod in unet.named_modules():
    if name.endswith(('conv', 'deconv')):
        mod.register_forward_hook(mk_hook(name))

with torch.no_grad():
    out, prev = unet(x)

print(f'{"module":<14}{"voxels":>10}{"channels":>10}')
for r in rows:
    print(f'{r[0]:<14}{r[1]:>10}{r[2]:>10}')
print('input voxels:', x.features.shape[0],
      '| final:', tuple(out.features.shape),
      '| preserved:', torch.equal(out.indices, x.indices))
print('multi-scale:', [(tuple(p.features.shape[1:]), ) for p in prev])
```

**实践目标**：

1. 观察下采样链 `conv → u.conv → u.u.conv → u.u.u.conv` 的体素数逐级减少、通道按 64/96/128/160 递增；
2. 观察上采样链 `u.u.u.deconv → u.u.deconv → u.deconv` 通道按 128/96/64（对应 level 3/2/0 的 `num_planes[0]`）递减、体素数逐级恢复；
3. 验证最终输出 `(V, 32)` 且 `preserved: True`（SubM 链位置不变，为下一讲按 `inverse_mapping` 池化铺路）；
4. 验证 `prev` 通道顺序为 `[160, 128, 96, 64, 32]`。

**预期结果**：一张清晰的"稀疏金字塔"表；所有结构数字与 4.3.2 的结构图一一对应。具体体素数取决于随机坐标，待本地验证。

**可选加练**（有预训练权重时）：`torch.load('work_dirs/pretrained/sstnet_scannet.pth', map_location='cpu')` 后检查其中的键名（如 `unet.blocks...`、`input_conv...`），确认 README 所说的 point-encoder initialization 到底覆盖了主干哪些子模块（权重文件的内部结构待本地验证）。

## 6. 本讲小结

- 点云经 `collate()` 体素化后成为 `SparseConvTensor` 四件套（features / indices / spatial_shape / batch_size），`spatial_shape` 只是边界声明，内存只花在活跃体素上。
- `ResidualBlock` = pre-norm 双 3×3 `SubMConv3d` + 恒等跳连；SubM 卷积不改活跃位置，所以两支可按行相加；同层所有 SubM 卷积（含 `input_conv` 的 `subm1`）共用一个 `indice_key` 复用索引规则。
- `SpConvUNet` 用递归表达 U 形：`self.u = SpConvUNet(num_planes[1:], ...)`，每级五件套 `blocks / conv / u / deconv / blocks_tail`；`deconv` 与同层 `conv` 必须共用 `spconv{id}` 键才能精确逆映射，跳连拼接由 `blocks_tail` 的 `(2 - i)` 通道设计接住。
- `num_planes=[32,64,96,128,160]`：4 次下采样、总步幅 16、最深层体素 0.32 米；`return_blocks=True` 时返回 `(全分辨率输出, 多尺度列表[最深→最浅])`。
- 本项目里 `extract_feat` 写 `x, _ = self.unet(x)`：多尺度列表被丢弃、只消费全分辨率 32 通道输出，但 True 仍是接口必需（False 会令解包失败）——这是继承自 SPFormer/UniSeg3D 的"名不副实"配置。
- 主干配套：`input_conv`（6→32）与 `output_layer`（BN+ReLU）内联在模型里；训练时 `load_from` 加载 `sstnet_scannet.pth` 主干预训练，`backbone` 键名享受 `lr_mult=0.01` 的差分学习率。

## 7. 下一步学习建议

主干输出的是**逐体素**特征（V, 32），但整个项目的语言模型与解码器工作在**超点**粒度上。下一讲 **u3-l3 超点池化：collate、scatter_mean 与特征组织** 将讲清两座桥：`collate()` 里 MinkowskiEngine 的 `TensorField` 量化、去重与 `inverse_mapping`（点→体素的反查表），以及 `extract_feat` 里那句 `scatter_mean(x.features[inverse_mapping], superpoints, dim=0)` 如何把逐体素特征压缩成 (M, 32) 的逐超点特征、再按 `batch_offsets` 切回每个场景。建议预习时先读 [uniseg3d/uniseg3d.py:L1028-L1060](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1028-L1060) 的 `collate()`，对照本讲 4.1 的体素化直觉。

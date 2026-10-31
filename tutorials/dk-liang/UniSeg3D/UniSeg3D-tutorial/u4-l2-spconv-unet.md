# 稀疏卷积骨干 SpConvUNet

## 1. 本讲目标

上一讲（u4-l1）我们看到 `UniSeg3D.loss()` 的十步前向里，点云先被 `collate()` 体素化为稀疏张量，再交给 `extract_feat()` 提取特征。本讲钻进这个「特征提取」的黑盒，读完你应该能：

1. 理解 spconv 的稀疏张量 `SparseConvTensor` 与 `indice_key` 机制——为什么共享同一个 key 能省计算、为什么上采样层必须和下采样层配对。
2. 掌握 `ResidualBlock` 的 pre-norm 残差结构（`SubMConv3d` + 归一化前置）。
3. 掌握 `SpConvUNet` 的**递归构建**方式：`num_planes=[32,64,96,128,160]` 五级 U-Net 是由同一个类自我嵌套出来的，并能手工推算每一层的通道数。
4. 理解 `input_conv` / `output_layer` 外壳如何把 6 通道原始特征接入 U-Net、又把 32 通道体素特征交还给 `extract_feat` 做超点聚合。

## 2. 前置知识

### 2.1 为什么需要「稀疏」卷积

3D 点云体素化之后几乎总是空的。以本项目的设置为例：`voxel_size=0.02`（2 厘米一个体素），一个 5m×5m×2.5m 的房间会被划成约 \( 250 \times 250 \times 125 \approx 7.8 \times 10^6 \) 个体素，而真实被点云占据的通常只有 \( 10^5 \) 量级：

\[
\text{占用率} = \frac{N_{\text{占据体素}}}{N_{\text{总体素}}} \approx 1\%
\]

如果直接用稠密 3D 卷积，99% 的乘加都花在空气上。**稀疏卷积**（sparse convolution）只对「有值的活跃位置」做计算：它预先算出卷积核窗口内哪些输入活跃点会影响哪些输出活跃点，生成一张「规则表」（rulebook，即索引对），然后只对这些配对执行稠密的小矩阵乘法。spconv 是 NVIDIA 开源的稀疏卷积库，本项目用它的 PyTorch 前端 `spconv.pytorch`。

### 2.2 SparseConvTensor：稀疏世界的「张量」

spconv 不用 `(B,C,D,H,W)` 的稠密形状，而是用四元组描述一个稀疏张量：

| 组成 | 形状 | 含义 |
| --- | --- | --- |
| `features` | `(N, C)` | N 个活跃体素的特征，按行排 |
| `indices` | `(N, 4)` | 每行是 `[batch_idx, x, y, z]`，指明该特征放在哪个体素格子里 |
| `spatial_shape` | `(3,)` | 网格逻辑大小 `[D, H, W]`（只用于越界检查，不代表真的开辟了这么大数组） |
| `batch_size` | 标量 | 批大小，配合 `batch_idx` 区分样本 |

在 `uniseg3d.py` 的 `loss()` 里能看到它的构造现场：[uniseg3d/uniseg3d.py:L453-L454](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L453-L454)——`features` 与 `coordinates` 来自 `collate()`（其中特征是「RGB + 质心中心化坐标」拼接出的 6 维，见 [uniseg3d/uniseg3d.py:L394-L403](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L394-L403)），这也是模型配置里 `in_channels=6` 的出处。

### 2.3 三种卷积层与 indice_key

本讲源码会用到 spconv 的三种层，行为差异是理解 U-Net 的关键：

| 层 | 作用 | 活跃点集合 | 在本讲中的角色 |
| --- | --- | --- | --- |
| `SubMConv3d`（submanifold，流形保持） | 同分辨率卷积 | **完全不变**：输出位置 = 输入位置 | 堆叠特征、不改变网格 |
| `SparseConv3d`（可带 stride） | 下采样卷积 | 变化：滑动窗口扫描产生新的输出活跃点 | 每级一次 k=2、s=2 的减半下采样 |
| `SparseInverseConv3d` | 上采样「逆卷积」 | **精确还原**配对下采样层的输入活跃点 | 每级一次上采样，回到原网格 |

**indice_key 机制**：spconv 为每个卷积层预计算输入→输出的索引对（rulebook）。如果两个层拥有相同的 `indice_key` 且输入活跃点集合一致，它们可以**复用同一张规则表**，省掉重复的索引计算。这就是为什么源码里同级的一摞 `SubMConv3d` 都标着 `subm{i}`。更重要的是：`SparseInverseConv3d` 自己不算卷积映射，它直接**转置复用**同 key 的 `SparseConv3d` 已经算好的索引对——所以它必须和下采样层共用同一个 `indice_key`（如 `spconv{i}`），且 `kernel_size` 必须一致（都是 2），输出活跃点也因此精确落在下采样前的体素集合上。这个「精确还原」性质是后文跳连拼接能逐行对齐的前提。

### 2.4 U-Net 与 pre-norm 残差

- **U-Net**：编码器逐级下采样（视野变大、体素变少、通道变多），解码器逐级上采样还原图分辨率，并把编码器同级的特征**跳连拼接**（skip connection）给解码器，兼顾「语义深」与「定位准」。
- **pre-norm 残差**：残差块写作 `y = x + F(Norm(x))`，即先归一化再进卷积分支（对应 `normalize_before=True`）；相对 post-norm（先卷积后归一化），它让梯度可以沿恒等通路无阻碍回传，是 Transformer 时代流行的做法。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [uniseg3d/spconv_unet.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/spconv_unet.py#L1-L237) | 稀疏卷积骨干（改自 SPFormer） | `ResidualBlock` 与 `SpConvUNet` 全部实现，共 237 行 |
| [uniseg3d/uniseg3d.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L311-L379) | 主模型 | 骨干的组装（L311）、`input_conv`/`output_layer` 外壳（L345-L356）、`extract_feat`（L358-L379） |
| [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L20-L38) | 主配置 | `num_channels=32`、`in_channels=6`、`backbone` 的 `num_planes` 与 `return_blocks` |

配置中的骨干设定（注意 `num_planes` 是一个推导式）：

[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L35-L38](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L35-L38)——`num_planes=[num_channels * (i + 1) for i in range(5)]`，配合 [L9](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L9) 的 `num_channels = 32`，即 `[32, 64, 96, 128, 160]`，并打开 `return_blocks=True`。

## 4. 核心概念与源码讲解

### 4.1 ResidualBlock：pre-norm 残差块

#### 4.1.1 概念说明

`ResidualBlock` 是 U-Net 每一级的基本积木，做的是「同分辨率特征精炼」：输入输出活跃点完全相同（全部用 `SubMConv3d`），通道数可以变。它是一条标准的残差结构：

\[
y = \mathrm{Branch}_I(x) + \mathrm{Branch}_{conv}(x)
\]

- **恒等分支 `i_branch`**：通道不变时就是 `nn.Identity`；通道变化时退化为 1×1×1 的 `SubMConv3d` 做逐点线性投影（因为 `SubMConv3d` kernel_size=1 时活跃点也不变）。
- **卷积分支 `conv_branch`**：两层 3×3×3 `SubMConv3d`，pre-norm 模式下每个卷积前先 `Norm → ReLU`。

#### 4.1.2 核心流程

```
输入 SparseConvTensor x (N, C_in)
├── identity = 只包一层的新 SparseConvTensor（共享 features/indices）
├── h = conv_branch(x)：  Norm → ReLU → SubMConv3d(3³) → Norm → ReLU → SubMConv3d(3³)
├── id = i_branch(identity)： Identity 或 SubMConv3d(1³)
└── 输出 = h.replace_feature(h.features + id.features)   # 逐体素相加，(N, C_out)
```

注意 spconv 的张量「不可原地改特征」：相加结果要用 `replace_feature` 装回一个新 `SparseConvTensor`。

#### 4.1.3 源码精读

**恒等分支的通道适配**：[uniseg3d/spconv_unet.py:L33-L38](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/spconv_unet.py#L33-L38)——`in_channels == out_channels` 时用 `Identity`，否则用 kernel_size=1、`bias=False` 的 `SubMConv3d` 把通道对齐。

**pre-norm 卷积分支**（默认 `normalize_before=True`）：[uniseg3d/spconv_unet.py:L40-L56](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/spconv_unet.py#L40-L56)——顺序是 `norm_fn(C_in) → ReLU → SubMConv3d(3³, C_in→C_out) → norm_fn(C_out) → ReLU → SubMConv3d(3³, C_out→C_out)`，两个卷积都标同一个 `indice_key`（复用规则表），`padding=1` 保持网格不变。`norm_fn` 默认是 `BatchNorm1d(eps=1e-4, momentum=0.1)`（[L27-L28](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/spconv_unet.py#L27-L28)），把 N 个体素当 batch 维归一化。post-norm 的 else 分支在 [L57-L72](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/spconv_unet.py#L57-L72)，本项目配置未改动默认值，走的是 pre-norm。

**前向与残差相加**：[uniseg3d/spconv_unet.py:L74-L91](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/spconv_unet.py#L74-L91)——先用 `input.features/indices/spatial_shape/batch_size` 重新包一个 `SparseConvTensor` 作为恒等通路（[L83-L85](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/spconv_unet.py#L83-L85)），再在 [L87-L89](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/spconv_unet.py#L87-L89) 用 `replace_feature(output.features + self.i_branch(identity).features)` 完成逐体素残差相加。

#### 4.1.4 代码实践

1. **实践目标**：亲眼确认 `ResidualBlock` 不改变活跃点集合，且恒等分支随通道是否变化而切换。
2. **操作步骤**（示例代码，非项目原有文件；需 u1-l2 搭好的环境）：

```python
# block_play.py —— 在仓库根目录运行：python block_play.py
import torch, spconv.pytorch as spconv
from uniseg3d.spconv_unet import ResidualBlock

torch.manual_seed(0)
N, C = 4000, 6
feats = torch.randn(N, C)
idx = torch.cat([torch.zeros(N, 1, dtype=torch.int32),
                 torch.randint(0, 50, (N, 3), dtype=torch.int32)], dim=1)
x = spconv.SparseConvTensor(feats, idx, [50, 50, 50], batch_size=1)

for cin, cout in [(32, 32), (32, 64)]:
    blk = ResidualBlock(cin, cout, indice_key='subm1')
    print(f'--- {cin} -> {cout} ---')
    print(blk.i_branch)          # 观察恒等分支是 Identity 还是 1³ SubMConv3d
    xin = spconv.SparseConvTensor(torch.randn(N, cin), idx, [50, 50, 50], 1)
    y = blk(xin)
    print('输出 features:', tuple(y.features.shape))
    print('indices 与输入一致:', torch.equal(y.indices, xin.indices))
```

3. **需要观察的现象**：`i_branch` 在 32→32 时是 `Identity`、在 32→64 时是 1×1×1 `SubMConv3d`；两次前向的 `indices` 都与输入逐行相同。
4. **预期结果**：输出特征形状分别为 `(4000, 32)` 与 `(4000, 64)`，`indices` 完全一致（SubM 流形保持）。**待本地验证**。

#### 4.1.5 小练习与答案

1. **练习**：为什么 `conv_branch` 里的两个 3³ 卷积可以共用一个 `indice_key`，而 `i_branch` 的 1³ 卷积没有传 `indice_key`？
   **答案**：两个 3³ 卷积（以及外层 `input_conv`）的输入活跃点集合相同，输出位置也相同，spconv 可复用同一张规则表；`i_branch` 只在通道变化时才出现，且 1×1×1 卷积的规则表计算开销几乎为零，源码索性不传（使用自动生成的默认 key），不影响正确性。
2. **练习**：把 `normalize_before=False` 后，分支结构变成什么？
   **答案**：变成 post-norm：`SubMConv3d(3³) → Norm → ReLU → SubMConv3d(3³) → Norm → ReLU`，即先卷积后归一化（对应 [L58-L72](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/spconv_unet.py#L58-L72)）。

### 4.2 SpConvUNet 的递归构建：`__init__` 与通道数推算

#### 4.2.1 概念说明

`SpConvUNet` 最特别的地方是**用自己构造自己**：它的 `__init__` 在 `len(num_planes) > 1` 时会 `new` 一个通道表更短的 `SpConvUNet` 存进 `self.u`，如此剥洋葱直到只剩一个平面（最深层）。也就是说，`num_planes=[32,64,96,128,160]` 的五级 U-Net 其实是五个嵌套的 `SpConvUNet` 实例，每一级处理一个分辨率。每级五件套：

| 部件 | 类型 | 通道 |
| --- | --- | --- |
| `blocks` | `block_reps`(=2) 个 `ResidualBlock` | `C → C` |
| `conv` | `SparseConv3d` k=2 s=2（pre-norm 前置 Norm+ReLU） | `C → C_next`（下采样） |
| `u` | 递归子 `SpConvUNet` | 处理 `num_planes[1:]` |
| `deconv` | `SparseInverseConv3d` k=2（与 conv 同 key） | `C_next → C`（上采样） |
| `blocks_tail` | 2 个 `ResidualBlock` | `2C → C`、`C → C`（精炼拼接特征） |

#### 4.2.2 核心流程

```
SpConvUNet(num_planes=[32,64,96,128,160], indice_key_id=1)
 ├─ blocks      : 2×ResidualBlock(32→32,  key='subm1')
 ├─ conv        : SparseConv3d(32→64,  k=2,s=2, key='spconv1')
 ├─ u = SpConvUNet([64,96,128,160], indice_key_id=2)
 │    ├─ blocks : 2×RB(64→64, 'subm2'); conv: 64→96 ('spconv2')
 │    ├─ u = SpConvUNet([96,128,160], id=3)
 │    │    └─ ... 继续递归 ...
 │    │         SpConvUNet([160], id=5)：只有 blocks(2×RB 160→160)，无 conv/deconv/tail
 │    └─ deconv : SparseInverseConv3d(96→64, key='spconv2'); tail: 128→64, 64→64
 └─ deconv      : SparseInverseConv3d(64→32, key='spconv1'); tail: 64→32, 32→32
```

通道数推算只需三条规则（设本级通道 \( C \)，级编号从 1 开始，stride 2 每级减半分辨率，第 \( \ell \) 级体素边长 \( = 0.02 \times 2^{\ell-1} \) 米）：

1. `blocks`：`C → C`，重复 `block_reps` 次；
2. `conv`/`deconv`：`C → C'` 与 `C' → C`，`indice_key` 分别为 `spconv{级号}`，deconv 与 conv 同 key；
3. `blocks_tail`：第 `i` 个块输入通道是 `num_planes[0] * (2 - i)`（`i=0,1`），即先吃「恒等 \( C \) + 上采样 \( C \) = \( 2C \)」的拼接，再 `C → C`。

五级完整通道表：

| 级 | subm key | blocks（×2） | conv 下采样 | deconv 上采样 | blocks_tail |
| --- | --- | --- | --- | --- | --- |
| 1 | subm1 | 32→32 | 32→64 (spconv1) | 64→32 (spconv1) | 64→32, 32→32 |
| 2 | subm2 | 64→64 | 64→96 (spconv2) | 96→64 | 128→64, 64→64 |
| 3 | subm3 | 96→96 | 96→128 (spconv3) | 128→96 | 192→96, 96→96 |
| 4 | subm4 | 128→128 | 128→160 (spconv4) | 160→128 | 256→128, 128→128 |
| 5 | subm5 | 160→160 | —（最深层） | — | — |

另外，第 1 级的 `subm1` 与主模型 `input_conv` 的 `indice_key='subm1'` 相同（见 4.4），两者输入活跃点一致，可共享规则表。

#### 4.2.3 源码精读

**注册与构造参数**：[uniseg3d/spconv_unet.py:L94-L116](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/spconv_unet.py#L94-L116)——`@MODELS.register_module()` 让配置字符串 `type='SpConvUNet'` 能被 `MODELS.build` 实例化（组装现场在 [uniseg3d/uniseg3d.py:L311](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L311)）。[L122-L126](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/spconv_unet.py#L122-L126) 允许 `block` 传字符串 `'residual'`（映射回 `ResidualBlock`）；注意 `'vgg'/'asym'` 虽能通过断言，但仓库没有给出对应实现，传了会在构造块时报错——这是从 SPFormer 迁移时留下的占位，不要使用。

**本级 blocks**：[uniseg3d/spconv_unet.py:L128-L138](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/spconv_unet.py#L128-L138)——用字典推导式生成 `block_reps` 个 `ResidualBlock(num_planes[0], num_planes[0], ..., indice_key=f'subm{indice_key_id}')`，装进 `OrderedDict` 再包成 `spconv.SparseSequential`。

**下采样 conv 与递归 u**：[uniseg3d/spconv_unet.py:L140-L169](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/spconv_unet.py#L140-L169)——pre-norm 分支为 `Norm → ReLU → SparseConv3d(k=2, s=2, key='spconv{id}')`；随后 `self.u = SpConvUNet(num_planes[1:], ..., indice_key_id=id+1, ...)` 完成递归，级号随深度递增，保证每级的 `subm{id}`/`spconv{id}` 互不冲突。

**上采样 deconv 与 blocks_tail**：[uniseg3d/spconv_unet.py:L171-L199](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/spconv_unet.py#L171-L199)——`SparseInverseConv3d` 与 `conv` 共用 `indice_key=f'spconv{id}'`（直接转置复用其规则表，精确还原下采样前的活跃点）；`blocks_tail` 按规则 3 构造，第一个块的输入通道是 `num_planes[0] * 2`，正好接住跳连拼接的 \( 2C \) 通道。

顺带一提：主配置 `min_spatial_shape=128`（[configs L27](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L27)，生效于 [uniseg3d/uniseg3d.py:L405-L406](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L405-L406)）保证网格至少 \( 128^3 \)：五级下采样后仍有 \( 128/2^4 = 8 \)，小场景不会在第 5 级塌缩成空网格。

#### 4.2.4 代码实践（本讲主实践）

1. **实践目标**：用随机稀疏张量跑通五级 `SpConvUNet`，验证通道表推算；再改成 3 级，验证递归结构的自相似性。
2. **操作步骤**（示例代码，保存为仓库根目录的 `spconv_unet_play.py`）：

```python
# python spconv_unet_play.py
import torch
import spconv.pytorch as spconv
from uniseg3d.spconv_unet import SpConvUNet

torch.manual_seed(0)
N = 5000                                     # 5000 个活跃体素
feats = torch.randn(N, 6)                    # 6 通道，与 in_channels=6 对齐
idx = torch.cat([torch.zeros(N, 1, dtype=torch.int32),
                 torch.randint(0, 100, (N, 3), dtype=torch.int32)], dim=1)
x = spconv.SparseConvTensor(feats, idx, [100, 100, 100], batch_size=1)

def run(num_planes, tag):
    unet = SpConvUNet(num_planes=num_planes, return_blocks=True)
    out, prev = unet(x)
    print(f'== {tag}: num_planes={num_planes}')
    print('   最细层输出 features:', tuple(out.features.shape))
    for i, p in enumerate(prev):
        print(f'   prev[{i}]: {tuple(p.features.shape)}')

run([32 * (i + 1) for i in range(5)], '五级(项目默认)')
run([32, 64, 96], '三级(改参数)')
```

3. **需要观察的现象**：五级时 `prev` 长度为 5、通道从 160 递减到 32；三级时长度为 3、通道 96→64→32；两种深度的最细层输出通道都是 32，且体素数都回到 5000。
4. **预期结果**（**待本地验证**）：

```
== 五级(项目默认): num_planes=[32, 64, 96, 128, 160]
   最细层输出 features: (5000, 32)
   prev[0]: (N5, 160)   # N5<=N4<=...<=5000，随机坐标下各级体素数依赖数据
   prev[1]: (N4, 128)
   prev[2]: (N3, 96)
   prev[3]: (N2, 64)
   prev[4]: (5000, 32)
== 三级(改参数): num_planes=[32, 64, 96]
   最细层输出 features: (5000, 32)
   prev[0]: (..., 96) / prev[1]: (..., 64) / prev[2]: (5000, 32)
```

#### 4.2.5 小练习与答案

1. **练习**：`num_planes=[32,64,96,128,160]`、`block_reps=2` 时，第 3 级 `blocks_tail` 两个残差块的输入输出通道分别是多少？第 3 级 `blocks_tail` 的第一个块里 `i_branch` 是 `Identity` 还是 1³ 卷积？
   **答案**：第 3 级 \( C=96 \)，拼接后 \( 2C=192 \)，所以是 192→96 与 96→96；第一个块 in≠out，`i_branch` 是 1×1×1 `SubMConv3d`（第二个块是 `Identity`）。
2. **练习**：整个 U-Net 一共有多少个 `ResidualBlock`？
   **答案**：前 4 级各 2+2=4 个（blocks + blocks_tail），第 5 级只有 blocks 的 2 个，共 \( 4 \times 4 + 2 = 18 \) 个。
3. **练习**：为什么 `deconv` 必须用 `indice_key='spconv{id}'` 而不能像 `blocks` 那样用 `subm{id}`？
   **答案**：`SparseInverseConv3d` 不自行计算映射，它转置复用**同 key 的 `SparseConv3d`** 的规则表，把该下采样层的输出映射回其输入活跃点。key 不同就拿不到这张表（运行时报错或语义错误）；`subm` 表只描述同分辨率卷积，无法表达跨分辨率的逆映射。

### 4.3 SpConvUNet 前向：下采样、跳连拼接与 return_blocks

#### 4.3.1 概念说明

`forward` 描述每一级「下去—上来—拼—精炼」的四步；`return_blocks` 决定是否把**每级的最终输出**收集成一个列表一起返回。本项目的配置打开了 `return_blocks=True`，所以 `extract_feat` 里写的是 `x, _ = self.unet(x)`——第二个返回值 `previous_outputs` 被丢弃，模型实际只消费最细层（32 通道）输出，超点查询全部从这一层聚合（呼应 u4-l1 的「超点即查询」）。这个开关源自 SPFormer 的多尺度用法，UniSeg3D 保留了它但只用单尺度。

#### 4.3.2 核心流程

```
forward(x, previous_outputs=None):
 1. output = blocks(x)                      # 本级精炼（活跃点不变）
 2. identity = output 的浅拷贝包装          # 跳连备用
 3. 若还有更深层：
    a. output_decoder = conv(output)        # 下采样，体素数减少
    b. output_decoder, previous_outputs = u(output_decoder, previous_outputs)  # 递归
    c. output_decoder = deconv(output_decoder)  # 上采样，精确还原到本级活跃点
    d. output = replace_feature(cat(identity.features, output_decoder.features, dim=1))  # 跳连拼接 → 2C 通道
    e. output = blocks_tail(output)         # 2C → C → C
 4. 若 return_blocks：previous_outputs.append(output)；返回 (output, previous_outputs)
    否则只返回 output
```

关键不变量：`deconv` 输出的活跃点集合与 `identity` 完全一致（`SparseInverseConv3d` 精确求逆），所以第 d 步可以**逐行** `torch.cat`——两边的第 k 行描述的是同一个体素。这也是 4.1.3 中残差相加、此处通道拼接能直接对齐的根本原因。

#### 4.3.3 源码精读

**本级 blocks 与跳连快照**：[uniseg3d/spconv_unet.py:L211-L214](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/spconv_unet.py#L211-L214)——`output` 先过 `blocks`，再用其 `features/indices/spatial_shape/batch_size` 包装出 `identity`。

**下—递归—上—拼—精炼**：[uniseg3d/spconv_unet.py:L216-L227](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/spconv_unet.py#L216-L227)——`return_blocks=True` 时递归调用走双返回值分支（[L218-L220](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/spconv_unet.py#L218-L220)），否则单返回值（[L221-L222](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/spconv_unet.py#L221-L222)）；上采样后在 [L225-L226](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/spconv_unet.py#L225-L226) 把恒等特征与解码特征沿通道维拼接（`dim=1`），交给 `blocks_tail`。

**多尺度输出的收集**：[uniseg3d/spconv_unet.py:L229-L236](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/spconv_unet.py#L229-L236)——`return_blocks=True` 时把本级输出 `append` 进 `previous_outputs` 并返回二元组。注意 [L230-L231](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/spconv_unet.py#L230-L231) 的注释 `NOTE: to avoid the residual bug`：递归最顶层调用时传入的 `previous_outputs` 是 `None`，一路透传到最深层仍是 `None`，这里先初始化为 `[]` 再 append，避免对 `None` 调用 `append`。由于最深层先返回、逐级向外 append，列表顺序是**由深到浅**（`prev[0]` 是 160 通道的最深层，`prev[-1]` 是 32 通道最细层，与 4.2.4 实践的预期一致）。

**消费侧**：[uniseg3d/uniseg3d.py:L372-L374](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L372-L374)——`extract_feat` 里 `x, _ = self.unet(x)` 只取最细层输出，多尺度列表被 `_` 丢弃。

#### 4.3.4 代码实践

1. **实践目标**：用前向钩子（hook）观察递归执行顺序，验证「u 的全部钩子夹在本级 conv 与 deconv 之间」，并验证拼接两侧体素数一致。
2. **操作步骤**（示例代码，接在 4.2.4 脚本之后）：

```python
def probe(name):
    def hook(mod, inp, out):
        if isinstance(out, spconv.SparseConvTensor):
            print(f'{name:<30s} -> {tuple(out.features.shape)}')
    return hook

unet = SpConvUNet([32 * (i + 1) for i in range(5)], return_blocks=True)
for name, mod in unet.named_modules():
    if isinstance(mod, spconv.SparseSequential):
        mod.register_forward_hook(probe(name))
unet(x)
```

3. **需要观察的现象**：打印顺序呈现明显的嵌套：第 1 级 `blocks → conv` 之后，先打印一整段 `u.blocks / u.conv / u.u.* ...`（更深的都在里面），然后才是 `u.deconv / u.blocks_tail`，最后回到第 1 级 `deconv / blocks_tail`；每级 `deconv` 输出的体素数与该级 `conv` 输入（即该级 `blocks` 输出）相同。
4. **预期结果**：能看到 5 组「blocks → conv →（递归）→ deconv → blocks_tail」，`deconv` 后特征通道减半、体素数恢复到下采样前；最外层 `blocks_tail` 输出 `(5000, 32)`。**待本地验证**。

#### 4.3.5 小练习与答案

1. **练习**：如果 `return_blocks=False`，`extract_feat` 里的 `x, _ = self.unet(x)` 会发生什么？
   **答案**：会报错——`forward` 只返回单个 `SparseConvTensor`，对它做二元解包会触发 ValueError。想改 `return_blocks` 必须同步把 [uniseg3d/uniseg3d.py:L373](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L373) 改成 `x = self.unet(x)`。
2. **练习**：`previous_outputs` 里的元素顺序为什么是「由深到浅」而不是「由浅到深」？
   **答案**：`append` 发生在各级 `forward` 的**末尾**，而递归调用 `self.u(...)` 发生在各级的中段；最深层最先执行到末尾，最先 append，外层后 append，故列表由深到浅排列。

### 4.4 外壳：input_conv / output_layer 与 extract_feat

#### 4.4.1 概念说明

`SpConvUNet` 的第一级通道是 32，而体素特征只有 6 维（RGB + 中心化坐标）；网络的最外层还需要一个「入口卷积」把 6 维升到 32 维，以及一个「出口归一化」在 U-Net 输出后做 BatchNorm + ReLU。这两件小事不属于 U-Net 本体，由主模型 `UniSeg3D` 的 `_init_layers` 直接用 spconv 层搭成：

- `input_conv`：`SubMConv3d(6 → 32, k=3, indice_key='subm1')`，与第 1 级 blocks 共享 `subm1` 规则表（两者输入活跃点一致）。
- `output_layer`：`BatchNorm1d(32) → ReLU`，纯特征级操作，不动稀疏结构。

#### 4.4.2 核心流程

```
extract_feat(x, superpoints, inverse_mapping, batch_offsets):
 1. x = input_conv(x)            # (N, 6) → (N, 32)，活跃点不变
 2. x, _ = unet(x)               # 五级 U-Net，输出最细层 (N, 32)
 3. x = output_layer(x)          # BN + ReLU
 4. feats = scatter_mean(x.features[inverse_mapping], superpoints)   # 体素 → 超点均值聚合
 5. 按 batch_offsets 切成每个场景一份，返回 List[Tensor]
```

第 4-5 步在 u4-l1 已精读（`inverse_mapping` 把超点映射回其来源体素，`batch_offsets` 处理批内超点 id 全局偏移），本讲只需记住：**骨干只负责「体素级 32 维特征」，超点查询是在这之后的聚合产物**。

#### 4.4.3 源码精读

**入口/出口层的定义**：[uniseg3d/uniseg3d.py:L345-L356](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L345-L356)——`input_conv` 是带 `indice_key='subm1'` 的 3³ `SubMConv3d`（`in_channels`/`num_channels` 分别来自配置 [L23-L24](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L23-L24) 的 6 与 32）；`output_layer` 是 `torch.nn.BatchNorm1d(32, eps=1e-4, momentum=0.1) + ReLU`，超参和 `ResidualBlock` 的 `norm_fn` 保持一致。

**三步串联与超点聚合**：[uniseg3d/uniseg3d.py:L358-L379](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L358-L379)——[L372-L374](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L372-L374) 依次过 `input_conv → unet → output_layer`；[L375](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L375) 用 `scatter_mean(x.features[inverse_mapping], superpoints)` 把体素特征按超点做均值池化；[L376-L378](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L376-L378) 按 `batch_offsets` 切回逐场景列表。

一个值得留意的分工细节：稀疏张量的**坐标/特征准备**用的是 MinkowskiEngine（`collate()` 里 `ME.TensorField` 与 `ME.utils.batch_sparse_collate`，[uniseg3d/uniseg3d.py:L394-L411](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L394-L411)），而**卷积骨干**跑在 spconv 上（[L453-L454](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L453-L454) 构造 `spconv.SparseConvTensor`）。两个库各干各的活，靠 `(indices, features)` 这份共同语言衔接。

#### 4.4.4 代码实践

1. **实践目标**：亲手复现 `extract_feat` 的前三步（不含超点聚合），确认外壳与 U-Net 通道严丝合缝。
2. **操作步骤**（示例代码）：

```python
import torch, spconv.pytorch as spconv
from uniseg3d.spconv_unet import SpConvUNet

# 手工搭外壳（与 _init_layers 中的定义逐项等价，避免实例化整个 UniSeg3D）：
input_conv = spconv.SparseSequential(spconv.SubMConv3d(
    6, 32, kernel_size=3, padding=1, bias=False, indice_key='subm1'))
output_layer = spconv.SparseSequential(
    torch.nn.BatchNorm1d(32, eps=1e-4, momentum=0.1), torch.nn.ReLU(inplace=True))

N = 5000
feats = torch.randn(N, 6)
idx = torch.cat([torch.zeros(N, 1, dtype=torch.int32),
                 torch.randint(0, 100, (N, 3), dtype=torch.int32)], dim=1)
x = spconv.SparseConvTensor(feats, idx, [100, 100, 100], batch_size=1)

unet = SpConvUNet([32 * (i + 1) for i in range(5)], return_blocks=True)
with torch.no_grad():
    h, _ = unet(input_conv(x))
    h = output_layer(h)
print('体素级特征:', tuple(h.features.shape))   # 期望 (5000, 32)
```

3. **需要观察的现象**：`input_conv` 后通道变 32、体素数不变；U-Net 输出仍为 `(5000, 32)`；`output_layer` 只改数值不改形状。
4. **预期结果**：最终打印 `(5000, 32)`，与 `extract_feat` 在 [uniseg3d/uniseg3d.py:L374](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L374) 处得到的张量规格一致。**待本地验证**。

#### 4.4.5 小练习与答案

1. **练习**：`input_conv` 为什么选 `SubMConv3d` 而不是 `SparseConv3d`？
   **答案**：入口卷积只做 6→32 的通道升维，不应改变活跃点集合；`SubMConv3d` 保证输出位置与输入一致，后续 U-Net 第 1 级（以及共享的 `subm1` 规则表）才能在同一网格上工作。`SparseConv3d` 会做窗口扫描、改变活跃点集合，破坏这种对齐。
2. **练习**：如果把配置的 `num_channels` 从 32 改成 64，需要同步改动哪些地方才能保证前向不报错？
   **答案**：`num_planes` 由 `num_channels` 推导（配置 L37），U-Net 内部自动变宽；但 `output_layer` 的 `BatchNorm1d(num_channels)`、解码器侧消费的查询维度（`UnifiedQueryDecoder` 的 `in_channels`/`d_model` 相关设置）以及 `type_embedding` 的 `num_channels` 都依赖同一变量，而预训练权重（如 SSTNet `load_from`）的通道布局也须匹配——这正是配置用变量 `num_channels` 统一推导、而不是散落硬编码的原因。
3. **练习**：`output_layer` 里的 `BatchNorm1d(32)` 与 `ResidualBlock` 里的 `norm_fn` 有何异同？
   **答案**：都是对 `(N, C)` 的体素特征按通道归一化，超参一致（eps=1e-4, momentum=0.1）；区别只是定义位置——前者是主模型出口处的裸 `torch.nn.BatchNorm1d`，后者是作为 `functools.partial` 构造器贯穿所有残差块的默认 `norm_fn`。

## 5. 综合实践

**任务：为五级 SpConvUNet 绘制完整的「通道—体素数」数据流表，并验证递归自相似与外壳衔接。**

1. 把 4.2.4 的脚本与 4.3.4 的钩子探针合并：对同一个输入稀疏张量，先跑五级 `SpConvUNet(num_planes=[32*(i+1) for i in range(5)], return_blocks=True)`，把每个 `SparseSequential` 钩子的 `(name, 通道, 体素数)` 按执行顺序记录成表。
2. 在表中标注每行属于哪一级（1-5）、处于该级的哪一步（blocks / conv / 递归 / deconv / blocks_tail），并与 4.2.2 的通道推算表逐行核对；特别验证「每级 deconv 的体素数 == 该级 blocks 输出的体素数」这一跳连前提。
3. 再分别用 `num_planes=[32,64,96]` 与 `[32,64]` 重跑，确认表格结构按同一模式缩短（证明递归自相似），且最细层输出始终是 `(N, 32)`。
4. 最后套上 4.4.4 的 `input_conv`/`output_layer` 外壳，核对从 `(N, 6)` 进到 `(N, 32)` 出的完整链路，与 `extract_feat`（[uniseg3d/uniseg3d.py:L372-L374](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L372-L374)）的调用顺序一致。
5. 预期产物：一张五级数据流表 + 一段结论（哪些层的体素数会变、哪些不变；哪些层共享 indice_key）。若本地无 GPU，全部步骤在 CPU 上即可完成；数值结果**待本地验证**。

## 6. 本讲小结

- `ResidualBlock` 是 pre-norm 残差块：`Norm → ReLU → SubMConv3d(3³) → Norm → ReLU → SubMConv3d(3³)` 加恒等分支（通道不等时用 1³ `SubMConv3d` 投影），全程不改变活跃点集合。
- `SpConvUNet` 通过 `self.u = SpConvUNet(num_planes[1:], indice_key_id+1, ...)` 递归自嵌套构建五级 U-Net；`num_planes=[32,64,96,128,160]`，每级「blocks → conv 下采样 → 递归 → deconv 上采样 → 跳连拼接 → blocks_tail」。
- `indice_key` 是 spconv 的规则表复用机制：同级 `SubMConv3d` 共享 `subm{i}`；`SparseInverseConv3d` 必须与配对的 `SparseConv3d` 共享 `spconv{i}`，从而把特征精确还原到下采样前的活跃点，使跳连拼接能逐行对齐。
- `return_blocks=True` 让 `forward` 返回 `(output, previous_outputs)`，多尺度列表由深到浅排列；但 `extract_feat` 用 `x, _ = self.unet(x)` 只消费 32 通道最细层输出。
- 外壳 `input_conv`（`SubMConv3d` 6→32，共享 `subm1`）与 `output_layer`（BN+ReLU）由主模型 `_init_layers` 搭建；体素级 32 维特征再经 `scatter_mean` 聚合成超点查询——「超点即查询」的原料在此产出。
- 坐标/特征准备用 MinkowskiEngine、卷积骨干用 spconv，两库以 `(indices, features)` 衔接。

## 7. 下一步学习建议

下一讲（u4-l3）顺着数据流进入 `uniseg3d/query_decoder.py`：看 32 维超点特征如何作为查询进入 `QueryDecoder` 的交叉注意力/自注意力/FFN 三件套，以及 `_forward_head` 如何用 einsum 点积生成掩码预测。建议先带着一个问题读：既然查询就是超点特征，解码器里的 `query_proj` 投影和 `attn_mask` 掩码分别解决什么？另外，若想巩固本讲的稀疏卷积直觉，可以对照 spconv 官方对 `SubMConv3d`/`SparseInverseConv3d` 的文档再看一遍 [uniseg3d/spconv_unet.py:L162-L199](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/spconv_unet.py#L162-L199) 的 key 配对。

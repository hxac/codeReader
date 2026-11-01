# SECONDFPN：从 3D 特征到多尺度 BEV 特征

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 neck 在体素类检测器（VoxelNet 家族）中的位置：它夹在 2D 化的 SECOND backbone 与 Anchor3DHead 之间，负责把多尺度 BEV 特征「对齐到同一分辨率再拼接」。
2. 理解 SecFPN 与经典 FPN 的结构差别：并列支路 + 反卷积对齐 + 通道维 concat + **单级输出**，而不是自顶向下逐元素相加 + 多级输出。
3. 读懂 `SECONDFPN.__init__` 中上采样/下采样双分支的构建逻辑，包括 `use_conv_for_no_stride` 与 `upsample_strides=[0.5, 1, 2]` 这种「分数步长」写法（CenterPoint 用法）。
4. 掌握 `norm_cfg` 的配置方法（BN/GN 切换），并知道本类的激活函数是硬编码的 ReLU。
5. 记住一条硬契约：**bbox_head 的 `in_channels` 必须等于 neck 各支路 `out_channels` 之和**。

本讲承接 u5-l2 的结论——`SECOND.forward` 返回一个多尺度特征 tuple；本讲就从这个 tuple 的第一站消费方讲起。

## 2. 前置知识

在进入源码前，先统一几个概念。

**什么是 neck？** 在「backbone → neck → head」的三段式分工里，backbone 负责提特征，head 负责出预测，neck 是中间的适配层。2D 检测中 neck 的典型代表是 FPN（特征金字塔）；3D 检测中，体素类模型的 backbone 输出是一叠**不同分辨率的 BEV（鸟瞰图）特征图**，neck 要把它们变成 head 好用的形状。

**BEV 伪图像**：回顾 u5-l1/u5-l2，点云经体素化、稀疏编码后，最终被组织成 \((N, C, H, W)\) 的四维张量——虽然它来自 3D 点云，但张量形态和一张普通 2D 图像完全一样，所以后续可以直接复用 2D 卷积体系。SECOND backbone 就是在这张伪图像上做逐级下采样。

**反卷积（转置卷积，ConvTranspose2d）**：一种可学习的上采样算子。普通卷积 stride=2 把 16×16 缩到 8×8；反卷积 stride=2 则把 8×8 放大到 16×16，且放大用的权重是学出来的。它的输出尺寸公式是：

\[
H_{out} = (H_{in} - 1) \times s + k
\]

其中 \(s\) 是 stride、\(k\) 是 kernel_size。SECONDFPN 特意取 \(k = s\)，代入得：

\[
H_{out} = (H_{in} - 1) \times s + s = s \cdot H_{in}
\]

即**恰好整数倍上采样，既不重叠也不留缝**——这是本讲最重要的一个设计细节。

**经典 FPN 长什么样**（背景知识，来自 2D 检测的通用设计，不涉及本库源码）：深层特征分辨率低但语义强，浅层分辨率高但语义弱。FPN 用 1×1 卷积把各层通道对齐，让最深层特征逐级上采样，与浅层**逐元素相加**，最后输出 P2~P5 多个分辨率的特征层，每层独立接检测头——小物体在浅层预测，大物体在深层预测。

带着这个印象，你会发现 SecFPN 几乎在每个环节上都做了不同的选择。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `mmdet3d/models/necks/second_fpn.py` | 本讲主角：SECONDFPN 的全部实现，仅 95 行 |
| `mmdet3d/models/backbones/second.py` | 上游：SECOND backbone，`forward` 返回多尺度 tuple |
| `configs/_base_/models/second_hv_secfpn_kitti.py` | SECOND + SecFPN + Anchor3DHead 的完整拼装，通道契约的现场 |
| `configs/_base_/models/pointpillars_hv_secfpn_kitti.py` | PointPillars 版拼装，用于横向对比 |
| `configs/_base_/models/centerpoint_pillar02_second_secfpn_nus.py` | CenterPoint 版拼装，展示 `use_conv_for_no_stride` 用法 |
| `mmdet3d/models/detectors/voxelnet.py` | 调用链闭合处：`extract_feat` 串联 backbone 与 neck |
| `mmdet3d/models/dense_heads/anchor3d_head.py` | 下游消费者，只看它如何遍历 neck 输出（细节留给 u5-l4） |
| `tests/test_models/test_necks/test_second_fpn.py` | 官方单测，含两种 FPN 用法的形状断言，是本讲实践的重要依据 |

## 4. 核心概念与源码讲解

### 4.1 汇聚式设计：SecFPN 与经典 FPN 的三个差别

#### 4.1.1 概念说明

SECOND backbone 的每个 stage 都比上一个 stride 更大，于是输出 tuple 里是「分辨率递减、通道递增」的一串特征（如 PointPillars 的 64/128/256 通道对应 H/2、H/4、H/8）。经典 FPN 的思路是让深层语义回流到浅层、保留多级输出；SecFPN 的思路则朴素得多：

> **每条支路各自把特征变换到同一目标分辨率，然后沿通道维拼成一张大通道特征图，只输出这一张。**

配置文件名中的 `secfpn` 即 SECOND FPN 的缩写（`hv` 前缀指 hard voxelization，回顾 u4-l3）。

与经典 FPN 相比有三个关键差别：

| 维度 | 经典 FPN（2D 检测通用设计） | SECONDFPN |
| --- | --- | --- |
| 支路组织 | 自顶向下逐级融合，有横向连接 | 各支路**并列独立**，互不通信 |
| 融合方式 | 对齐通道后**逐元素相加**（通道数不变） | 对齐分辨率后**通道维 concat**（通道数累加） |
| 输出形态 | 多个分辨率的 level，逐 level 独立预测 | **单级输出**（list 只有一个元素），单张 BEV 大通道图 |

第三个差别直接决定了下游 head 的形态：Anchor3DHead 只在一层特征上铺 anchor 网格（详见 4.4）。

为什么 3D 检测偏爱「对齐后 concat」？直觉上有两点：一是 BEV 伪图像的「尺寸」感受与 2D 图像不同，物体远近造成的尺度变化远小于 2D 图像，单级输出已经够用；二是 concat 保留而非平均了各尺度信息，配合后面的 3×3 卷积 head，让网络自己学习怎么混合，对自动驾驶这种类别少（车/人/骑行者）的任务更简单直接。

#### 4.1.2 核心流程

```text
输入  x = [feat_0, feat_1, ..., feat_{L-1}]   # SECOND 输出的多尺度 tuple
        │
        ├─ deblock_0: 上/下采样 feat_0 到目标分辨率, out_channels[0] 通道
        ├─ deblock_1: 上/下采样 feat_1 到目标分辨率, out_channels[1] 通道
        └─ ...
        │
        ▼
ups = [deblock_0(x[0]), ..., deblock_{L-1}(x_{L-1])]   # 形状已全部一致
        │
        ▼
out = torch.cat(ups, dim=1)      # 通道维拼接, C_out = Σ out_channels[i]
        │
        ▼
return [out]                     # 注意: 长度为 1 的列表
```

#### 4.1.3 源码精读

forward 全部逻辑只有三行有效代码，先看它：

[second_fpn.py:L77-L94](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/necks/second_fpn.py#L77-L94) —— `forward` 先断言输入层数与构造时的 `in_channels` 数量一致，然后用 Python 列表推导对每条支路调用 `deblock(x[i])`，若支路多于一条就 `torch.cat(ups, dim=1)` 沿通道维拼接，最终把这张拼接图包进**单元素列表**返回。

上游输入从哪来？看 SECOND backbone 的 forward：

[second.py:L82-L95](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/backbones/second.py#L82-L95) —— 逐 block 下采样，每经过一个 stage 就把当前特征 append 进 `outs`，最终返回 `tuple(outs)`。这就是 neck 收到的多尺度输入。

再看配置里三者如何咬合（SECOND on KITTI）：

[second_hv_secfpn_kitti.py:L19-L29](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/second_hv_secfpn_kitti.py#L19-L29) —— SECOND 的 `out_channels=[128, 256]`、`layer_strides=[1, 2]`，产出两级特征；neck 的 `in_channels=[128, 256]` 精确对应，`upsample_strides=[1, 2]` 把两级都对齐到第一级的分辨率，`out_channels=[256, 256]`。

[second_hv_secfpn_kitti.py:L30-L34](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/second_hv_secfpn_kitti.py#L30-L34) —— bbox_head 的 `in_channels=512`。注意 \(256 + 256 = 512\)：head 输入通道等于 neck 各支路输出通道之和，这就是 concat 留下的硬契约。

#### 4.1.4 代码实践

**实践目标**：不跑模型，只靠「读配置 + 心算」验证通道契约，并把这条链路抄进自己的笔记。

**操作步骤**：

1. 打开 [pointpillars_hv_secfpn_kitti.py:L22-L36](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/pointpillars_hv_secfpn_kitti.py#L22-L36)。
2. 抄下三个数字：backbone `out_channels=[64, 128, 256]`，neck `in_channels=[64, 128, 256]`、`out_channels=[128, 128, 128]`，head `in_channels=384`。
3. 心算验证：neck 的 `in_channels` 是否与 backbone 的 `out_channels` 完全一致？\(128 \times 3 = 384\) 是否成立？
4. 再看 CenterPoint 的官方写法：[centerpoint_pillar02_second_secfpn_nus.py:L29-L40](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/centerpoint_pillar02_second_secfpn_nus.py#L29-L40)，head 的 `in_channels=sum([128, 128, 128])` ——官方直接用 `sum()` 把这条契约写成了代码。

**需要观察的现象 / 预期结果**：三个配置全部满足两条等式：`neck.in_channels == backbone.out_channels`（逐元素相等）；`head.in_channels == sum(neck.out_channels)`。PointPillars 版满足（384 = 128×3），SECOND 版满足（512 = 256×2），CenterPoint 版由 `sum()` 显式表达。纯静态阅读即可完成，无需运行。

#### 4.1.5 小练习与答案

**练习 1**：若某配置的 neck `out_channels=[128, 128, 128, 128]`（四条支路），head 的 `in_channels` 应填多少？

**答案**：\(128 \times 4 = 512\)。concat 的通道数是各支路之和。

**练习 2**：SECONDFPN 的 `forward` 返回 `[out]` 而不是 `out`（裸张量），为什么要多包一层列表？

**答案**：下游接口按「多尺度特征列表」的统一契约设计。Anchor3DHead 用 `multi_apply(self.forward_single, x)` 遍历列表逐级预测，`loss_by_feat` 用 `featmap_sizes` 的长度断言层数。返回列表让「单级」和「多级」两种 neck 走同一条消费路径；返回裸张量反而会破坏契约（详见 4.4）。

**练习 3**：经典 FPN 用逐元素相加融合，SECONDFPN 用 concat。两者对输出通道数的影响分别是什么？

**答案**：相加要求各支路通道数相同、融合后通道数不变（每像素信息被「平均/混合」）；concat 不要求各支路通道相同、融合后通道数是各支路之和（每像素信息被「并排保留」，信息无损但参数量和显存随之增大）。

### 4.2 deblock 支路构建：上采样与下采样的双模式

#### 4.2.1 概念说明

每条支路（源码里叫 deblock）= 「上/下采样层 + 归一化层 + ReLU」三件套。最有趣的是构建采样层的分支判断：

- `upsample_strides[i] > 1`：**放大**。走反卷积，`kernel_size=stride=stride`，输出尺寸恰好变为输入的 stride 倍（见第 2 节的公式推导）。
- `upsample_strides[i] == 1`：默认（`use_conv_for_no_stride=False`）仍走反卷积，kernel=stride=1，等价于一个 1×1 可学习变换；若 `use_conv_for_no_stride=True` 则改走普通 `Conv2d` 分支（见下一条，stride=1 时两种分支数学上都是 1×1 卷积，但走的构建器与配置不同）。
- `upsample_strides[i] < 1`：**缩小**。比如 `0.5`，代码取倒数取整得到卷积 stride=2，走普通 `Conv2d` 下采样。

`upsample_strides=[0.5, 1, 2]` 是 CenterPoint 的标志性用法：backbone 三级输出分辨率为 \(H, H/2, H/4\)，取**中间分辨率** \(H/2\) 作为汇聚点——第一支路下采样、第二支路保持、第三支路上采样。相比 SecFPN 默认的「全部拉到最大分辨率」，输出图缩小 4 倍（面积），显著节省 head 的计算量，代价是空间粒度变粗；对 nuScenes 这类只需 0.5m 级定位精度的任务足够。

#### 4.2.2 核心流程

```text
for i, out_channel in enumerate(out_channels):
    stride = upsample_strides[i]
    if stride > 1 或 (stride == 1 且 不开 use_conv_for_no_stride):
        采样层 = ConvTranspose2d(in_channels[i], out_channel,
                                  kernel_size=stride, stride=stride)   # 放大/保持
    else:
        stride = round(1 / stride)          # 0.5 -> 2, 1 -> 1
        采样层 = Conv2d(in_channels[i], out_channel,
                        kernel_size=stride, stride=stride)             # 缩小/保持
    deblock_i = Sequential(采样层, Norm(out_channel), ReLU)
```

尺寸账（以 backbone 输出 16×16、8×8、4×4 为例）：

| 支路输入 | SecFPN 用法 strides=[1,2,4] | CenterPoint 用法 strides=[0.5,1,2] |
| --- | --- | --- |
| 16×16 | deconv s=1 → 16×16 | Conv2d s=2 → 8×8 |
| 8×8 | deconv s=2 → 16×16 | Conv2d k=1 s=1 → 8×8 |
| 4×4 | deconv s=4 → 16×16 | deconv s=2 → 8×8 |
| 汇聚结果 | 16×16 | 8×8 |

#### 4.2.3 源码精读

[second_fpn.py:L52-L75](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/necks/second_fpn.py#L52-L75) —— deblock 构建主循环：按 `out_channels` 逐条建支路，最终收进 `nn.ModuleList`。`build_upsample_layer`/`build_conv_layer` 是 mmcv 的注册器工厂（与 u4-l1 讲的注册机制同族），`type='deconv'` 路由到 `nn.ConvTranspose2d`、`type='Conv2d'` 路由到普通二维卷积。

[second_fpn.py:L55-L61](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/necks/second_fpn.py#L55-L61) —— 上采样分支：注意 `kernel_size=upsample_strides[i], stride=upsample_strides[i]`，k 与 s 相等保证输出尺寸恰为输入的整数倍、无重叠无缝隙。

[second_fpn.py:L62-L69](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/necks/second_fpn.py#L62-L69) —— 下采样分支：`np.round(1 / stride)` 把分数步长换算成下采样步长（0.5 → 2），再建一个 `kernel_size=stride` 的普通卷积。stride=2、kernel=2、padding=0 的卷积恰好把 16×16 无重叠地切成 8×8。

[second_fpn.py:L71-L73](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/necks/second_fpn.py#L71-L73) —— 三件套组装：采样层 + 归一化层 + 原地 ReLU，串成 `nn.Sequential`。归一化层由 `build_norm_layer(norm_cfg, out_channel)[1]` 构建（返回二元组的第二个元素才是层本身）。

官方单测把两种用法并排做了形状断言，是最好的对照教材：

[test_second_fpn.py:L43-L82](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_models/test_necks/test_second_fpn.py#L43-L82) —— 构造 32×32 输入，经 `layer_strides=[2,2,2]` 的 SECOND 得到 16×16、8×8、4×4 三级特征；普通 FPN（strides=[1,2,4]）输出 `[2, 6, 16, 16]`，CenterPoint 式 FPN（strides=[0.5,1,2] + `use_conv_for_no_stride=True`）输出 `[2, 6, 8, 8]`。6 = 2+2+2 为 concat 后通道数。

配置层的证据：[centerpoint_pillar02_second_secfpn_nus.py:L33-L36](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/centerpoint_pillar02_second_secfpn_nus.py#L33-L36) 写着 `upsample_strides=[0.5, 1, 2]` 与 `use_conv_for_no_stride=True`，与单测互为印证。

#### 4.2.4 代码实践

**实践目标**：亲手跑通官方单测，观察两种步长配置的输出尺寸差异。

**操作步骤**：

1. 本地已按 u1-l2 装好环境（需要 mmcv、torch、mmdet3d 可编辑安装）。
2. 执行：

```bash
python -m pytest tests/test_models/test_necks/test_second_fpn.py -v
```

3. 阅读并试图在纸上复算 [test_second_fpn.py:L81-L82](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_models/test_necks/test_second_fpn.py#L81-L82) 的两个断言。

**需要观察的现象**：两个断言通过；`centerpoint_output[0].shape == [2, 6, 8, 8]`，`second_output[0].shape == [2, 6, 16, 16]`。

**预期结果**：与第 4.2.2 节的尺寸表完全一致——这就是「分数步长 = 下采样」的直接证据。若想进一步观察，可把 L61 的 `0.5` 改成 `0.25`（心算输出应变 4×4），重跑测试看断言是否按预期失败（预期失败，报 shape 不匹配）。

> 待本地验证：本讲义未在编写环境中实际执行 pytest（缺运行环境），以上命令与断言值均直接摘自测试源码，属可复现操作。

#### 4.2.5 小练习与答案

**练习 1**：反卷积支路为什么必须令 `kernel_size == stride`？

**答案**：由 \(H_{out} = (H_{in}-1)s + k\)，当 \(k=s\) 时 \(H_{out} = s \cdot H_{in}\)，是干净的整数倍放大；若 \(k > s\) 各输入像素的感知野会在输出上重叠（信息冗余），若 \(k < s\) 会出现「格子缝」（部分输出像素没有任何输入负责，训练易出伪影）。

**练习 2**：`upsample_strides=[0.5, 1, 2]` 配 `use_conv_for_no_stride=True` 时，中间那条 stride=1 的支路实际是什么层？

**答案**：走 `build_conv_layer` 分支，`np.round(1/1)=1`，得到 `kernel_size=1, stride=1` 的普通 1×1 卷积——只做通道变换（2→out_channel），不改变分辨率。反卷积在 k=s=1 时数学上同样是 1×1 卷积，两者在此退化为等价形式，CenterPoint 显式选 Conv2d 主要是构建路径与计算核的偏好。

**练习 3**：把 SecFPN 的 `upsample_strides` 从 `[1, 2]` 改成 `[1, 4]`（backbone 仍是 `layer_strides=[1, 2]`），会发生什么？

**答案**：第二支路被放大 4 倍（H/2 → 2H），与第一支路的 H 无法 concat，`torch.cat` 报尺寸不匹配错误。`upsample_strides` 不是自由参数，它必须与 backbone 的 `layer_strides` 乘积关系对齐：第 i 支路的输出尺寸 = backbone 第 i 级尺寸 × upsample_strides[i]，各支路必须相等。

### 4.3 norm_cfg 的配置方法与硬编码的激活函数

#### 4.3.1 概念说明

- **norm_cfg** 是描述归一化层的配置字典。3D 检测常用小 batch（甚至单卡 batch=1~2），BatchNorm 统计量噪声大，因此 SECOND 家族把 BN 的超参调成 `eps=1e-3, momentum=0.01`（比 PyTorch 默认 0.1/1e-5 慢很多、稳很多）；batch 更小的场景可换 GroupNorm（GN）。
- **act_cfg 的边界要如实说明**：SECONDFPN 的构造函数**没有** `act_cfg` 参数，激活函数是硬编码的 `nn.ReLU(inplace=True)`。想换激活只能改源码。这与支持 `order=('conv','norm','act')` 的 SparseEncoder（u5-l2 讲过）不同——「归一化可配、激活写死」是本文件的现实，读配置时不要想当然地给 neck 塞 `act_cfg`。
- **init_cfg** 是 mmengine 的按层类型初始化配置：`layer` 字段按**类名**匹配参数层。本类默认对 `ConvTranspose2d` 做 Kaiming 初始化（反卷积权重）、对 `NaiveSyncBatchNorm2d` 做常值 1.0 初始化（把归一化层的缩放因子 γ 置 1，即「恒等初始化」）。

#### 4.3.2 核心流程

```text
构造 deblock 时:
    归一化层 = build_norm_layer(norm_cfg, out_channel)[1]
        type='BN' -> BatchNorm2d 家族, 带 eps/momentum
        type='GN' -> GroupNorm, 需附 num_groups 与 affine
    激活层   = nn.ReLU(inplace=True)          # 硬编码, 不可经配置替换

模块初始化时 (BaseModule.init_weights 按 init_cfg):
    layer='ConvTranspose2d'      -> Kaiming 初始化权重
    layer='NaiveSyncBatchNorm2d' -> weight 常值 1.0 (γ=1)
```

#### 4.3.3 源码精读

[second_fpn.py:L30-L48](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/necks/second_fpn.py#L30-L48) —— 构造函数签名：`norm_cfg` 默认 `dict(type='BN', eps=1e-3, momentum=0.01)`，`upsample_cfg` 默认 `dict(type='deconv', bias=False)`（采样层不带偏置，因为后面紧跟的归一化层会吸收偏置）；L48 的断言要求三个列表等长。注意签名里没有 `act_cfg`。

[second_fpn.py:L45-L46](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/necks/second_fpn.py#L45-L46) —— 源码注释直接给出 GroupNorm 的写法：`dict(type='GN', num_groups=num_groups, eps=1e-3, affine=True)`。想换归一化方式时照抄进配置即可。

[second_fpn.py:L38-L44](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/necks/second_fpn.py#L38-L44) —— `init_cfg` 默认值：Kaiming 给反卷积、常值 1.0 给同步 BN。这是从旧版代码沿袭下来的初始化策略，改 `norm_cfg` 为 GN 时记得同步检查这一项（GN 同样有可学习缩放因子，但类名不同，此条 init_cfg 将匹配不到任何层而不生效——不会报错，只是静默跳过）。

配置侧的 norm 写法见 [second_hv_secfpn_kitti.py:L25-L29](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/second_hv_secfpn_kitti.py#L25-L29) 与 [centerpoint_pillar02_second_secfpn_nus.py:L34-L35](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/centerpoint_pillar02_second_secfpn_nus.py#L34-L35)：显式写出 `norm_cfg` 与 `upsample_cfg`，值与源码默认一致——官方配置习惯于「显式优于隐式」。

#### 4.3.4 代码实践

**实践目标**：体验 `norm_cfg` 的「换类型不改代码」能力，并确认激活层不可配置这一边界。

**操作步骤**（以下为示例代码，需本地环境运行）：

```python
from mmdet3d.registry import MODELS

base = dict(
    type='SECONDFPN',
    in_channels=[128, 256],
    upsample_strides=[1, 2],
    out_channels=[256, 256])

# 变体 1: 默认 BN
neck_bn = MODELS.build(base)
# 变体 2: GroupNorm (写法来自源码注释 L45-46)
neck_gn = MODELS.build(dict(
    **base,
    norm_cfg=dict(type='GN', num_groups=32, eps=1e-3, affine=True)))

for name, neck in [('BN', neck_bn), ('GN', neck_gn)]:
    print(name, type(neck.deblocks[0][1]).__name__)
```

**需要观察的现象**：打印分别为 BN 家族的类名（如 `BatchNorm2d`）与 `GroupNorm`；两处 `deblocks[i]` 的第三个子层都是 `ReLU`。

**预期结果**：`norm_cfg` 一改即生效，无需碰任何源码；再尝试 `MODELS.build(dict(**base, act_cfg=dict(type='LeakyReLU')))` 会抛出 `TypeError: __init__() got an unexpected keyword argument 'act_cfg'`——因为 `MODELS.build` 把配置字典原样传给构造函数，而签名里没有这个参数（注册器工厂不会替你过滤未知键）。想换激活只能修改 `second_fpn.py` 的 L73，这就是 4.3.1 说的边界。

> 待本地验证：以上脚本未在编写环境中执行，行为依据构造函数签名（无 `act_cfg` 参数）与 mmcv `build_norm_layer` 的注册表机制推断，结论可靠但具体打印的类名以本地为准。

#### 4.3.5 小练习与答案

**练习 1**：为什么 SECOND 家族把 BN 的 momentum 设成 0.01 而不是 PyTorch 默认的 0.1？

**答案**：momentum 越小，滑动平均统计量更新越慢、越接近长期平均。3D 检测 batch 小、迭代内点云分布方差大，放慢更新能避免单 batch 的统计噪声污染 BN 参数，训练更稳。

**练习 2**：`upsample_cfg=dict(type='deconv', bias=False)` 为什么可以不带偏置？

**答案**：采样层之后紧跟归一化层，归一化会先减均值再除方差，加在卷积输出上的常数偏置会被归一化完全抵消，属于冗余参数；省掉它可减少参数量且不改表达能力。

**练习 3**：把 `norm_cfg` 从 BN 换成 GN 后，默认 `init_cfg` 里那条 `layer='NaiveSyncBatchNorm2d'` 的 Constant 初始化会发生什么？

**答案**：init_cfg 的 `layer` 按类名匹配参数层，网络里已不存在该类名的层，这条初始化规则匹配不到任何目标，静默不生效（不报错）。如果希望 GN 的 γ 也置 1，需要把 init_cfg 里的 layer 改成 `'GroupNorm'`。

### 4.4 调用链闭合：neck 输出如何被 dense head 消费

#### 4.4.1 概念说明

前三个模块看完了 neck 自身，本模块把它放回 VoxelNet 的完整前向里，回答两个问题：

1. neck 在检测器里挂在哪、谁调用它？
2. 它返回的「单元素列表」如何被 Anchor3DHead 消费？

回顾 u5-l1 的分工：`extract_feat` 负责「点云 → BEV 多尺度特征」，head 负责「特征 → 预测」。neck 是 `extract_feat` 的最后一步。而 head 侧的接口契约是**按多尺度列表设计的**：`forward` 对列表里每个 level 独立跑一遍预测分支；训练时按每个 level 的 (H, W) 生成 anchor 网格。SECONDFPN 只输出一个 level，于是整条链退化为「单级网格预测」——anchor 数量由这一张图的尺寸决定（细节在 u5-l4 展开）。

#### 4.4.2 核心流程

```text
VoxelNet.extract_feat(batch_inputs_dict):
    voxels ─ voxel_encoder ─▶ voxel_features
           ─ middle_encoder ─▶ BEV 伪图像 x (N,C,H,W)
           ─ backbone       ─▶ (x1, x2, ...)          # 多尺度 tuple
           ─ neck (若有)     ─▶ [cat 后的单级大通道图]
    return 该列表

Anchor3DHead.forward(x):
    multi_apply(forward_single, x)     # 对列表逐 level 预测
    训练时: featmap_sizes = [每级 (H,W)] → 生成 anchor 网格
```

#### 4.4.3 源码精读

[voxelnet.py:L36-L48](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/voxelnet.py#L36-L48) —— `extract_feat` 全貌：voxel_encoder → middle_encoder → backbone，随后 `if self.with_neck: x = self.neck(x)`。neck 是可选组件（`with_neck` 由配置中是否给出 neck 键决定），没有它时 head 直接吃 backbone 的多尺度输出。

[anchor3d_head.py:L177-L198](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/anchor3d_head.py#L177-L198) —— head 的 forward 用 `multi_apply(self.forward_single, x)` 把 neck 返回的列表拆开，对每个 level 各跑一套 conv 分支。SECONDFPN 给一个元素，就只跑一次。

[anchor3d_head.py:L390-L391](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/anchor3d_head.py#L390-L391) —— 训练侧的消费方式：`featmap_sizes = [featmap.size()[-2:] for featmap in cls_scores]` 取每个预测层的 (H, W)，随即断言层数等于 anchor 生成器的 `num_levels`。这解释了 4.1 练习 2 的设计动机：只要 neck 输出的每个元素尺寸、含义符合「一个预测层」的约定，1 个或 N 个 level 都能走通。

[test_second_fpn.py:L8-L40](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_models/test_necks/test_second_fpn.py#L8-L40) —— 另一组结构断言：三个列表长度不一致时 `MODELS.build` 直接抛 `AssertionError`，对应源码 L48 的等长断言；`deblocks[i][0]` 的 `in_channels/out_channels/stride` 与配置逐项吻合，验证了 4.2 的构建逻辑。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：按讲义规格——修改 `out_channels` 并实例化，用伪多尺度特征（128/256/512 通道各一层）跑 forward，打印输出张量列表形状，体会「改 neck 必须同步改 head」的通道契约。

**操作步骤**（以下为示例代码，需本地环境运行）：

```python
import torch
from mmdet3d.registry import MODELS

def build_and_run(out_channels):
    """示例代码: 构造 SECONDFPN 并喂入伪多尺度特征"""
    neck = MODELS.build(dict(
        type='SECONDFPN',
        in_channels=[128, 256, 512],        # 伪 backbone 输出: 三级
        upsample_strides=[1, 2, 4],          # 全部上采样到第一级分辨率
        out_channels=out_channels,
        norm_cfg=dict(type='BN', eps=1e-3, momentum=0.01),
        upsample_cfg=dict(type='deconv', bias=False)))
    # 分辨率递减 200/100/50, 与 layer_strides=[1,2,4] 的 backbone 对应
    feats = [
        torch.rand(2, 128, 200, 176),
        torch.rand(2, 256, 100, 88),
        torch.rand(2, 512, 50, 44),
    ]
    outs = neck(feats)
    n_params = sum(p.numel() for p in neck.parameters())
    print(f'out_channels={out_channels} -> '
          f'输出 {len(outs)} 层, 形状 {tuple(outs[0].shape)}, '
          f'参数量 {n_params:,}')

build_and_run([128, 128, 128])   # PointPillars 同款
build_and_run([256, 256, 256])   # 加宽版: 观察 concat 通道与参数量变化
```

**需要观察的现象**：

1. 两次调用的输出都是**长度为 1 的列表**（`len(outs) == 1`）。
2. 形状分别是 `[2, 384, 200, 176]` 与 `[2, 768, 200, 176]`：分辨率对齐到第一级的 200×176，通道数 \(=\sum out\_channels[i]\)。
3. 加宽后参数量明显上涨——反卷积参数量约为 \(C_{in} \times C_{out} \times k \times k\)，三支路通道同时翻倍时接近 4 倍。

**预期结果**：384 = 128×3、768 = 256×3 与 4.1 的契约一致。随后做第二个小实验：把 `out_channels=[256, 256, 256]` 的 neck 塞进 PointPillars 配置（用 `--cfg-options model.neck.out_channels=...` 或复制配置文件修改），但**不**改 bbox_head 的 `in_channels`（仍为 384），跑 `python tools/train.py <config> --cfg-options ...` 或直接实例化 model——预期在 head 第一个卷积处报「通道数不匹配」的形状错误（384 ≠ 768）。把 head 的 `in_channels` 同步改成 `sum([256, 256, 256])` 后恢复通过。

> 待本地验证：以上脚本与故障实验未在编写环境中实际执行；形状推导依据 forward 的 `torch.cat` 逻辑（L88-L94）与 4.2 的尺寸公式，属确定性计算，但请以本地运行输出为准。

#### 4.4.5 小练习与答案

**练习 1**：删掉配置里的 neck 键（其余不变），VoxelNet 还能工作吗？head 需要怎么改？

**答案**：能。`extract_feat` 里 `if self.with_neck` 判断（voxelnet.py L46）使 neck 可省略，head 直接消费 backbone 的多尺度输出；但 backbone 的 `out_channels=[128, 256]` 两级通道不同、分辨率也不同，`Anchor3DHead` 的 `forward_single` 会拿到形状不一致的多级特征，head 的 `in_channels` 只能匹配其中一级——实践中几乎总保留 neck 来统一形状。

**练习 2**：`featmap_sizes` 为什么取 `featmap.size()[-2:]` 而不是完整 shape？

**答案**：四维特征 (N, C, H, W) 的前两维是 batch 与通道，与空间位置无关；anchor 网格只由空间尺寸 (H, W) 决定——每个空间格点放一组 anchor，与通道数、batch 数无关（回顾 u5-l1：Anchor3DHead 的每个 BEV 格点生成 `len(sizes) × len(rotations)` 个 anchor）。

**练习 3**：SECOND 的 KITTI 配置里 backbone 是 `layer_strides=[1, 2]`（两级），neck 是 `upsample_strides=[1, 2]`。若换成 `layer_strides=[1, 2, 2]`（三级），neck 应该怎么写？

**答案**：三级输出的相对分辨率为 1、1/2、1/4，全部对齐到第一级需要 `upsample_strides=[1, 2, 4]`，`in_channels` 同步改为 backbone 的 `out_channels`（如 `[128, 128, 256]`），`out_channels` 与 head 的 `in_channels=sum(...)` 一并调整——这正是 PointPillars 配置（三级、[1,2,4]）的实际写法。

## 5. 综合实践

**任务：给 PointPillars 换一个「窄通道」neck，并让它真正能被构建。**

把所学串起来：从配置继承（u1-l5）到通道契约（本讲 4.1/4.4）再到构建验证。

1. **复制配置**：新建 `configs/pointpillars/my_pointpillars_narrow.py`（示例路径），内容两行：

```python
_base_ = './pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py'

model = dict(
    neck=dict(out_channels=[96, 96, 96]),
    bbox_head=dict(in_channels=288))   # 96 x 3 = 288
```

2. **心算核对**：neck 的 `in_channels=[64, 128, 256]` 未动，仍匹配 backbone；head 输入 288 = 96×3，契约成立。
3. **展开检查**（不训练）：`python tools/misc/print_config.py configs/pointpillars/my_pointpillars_narrow.py` 确认覆盖生效（回顾 u1-l5 的增量覆盖规则）。
4. **构建验证**：在 Python 里 `from mmengine.config import Config; from mmdet3d.registry import MODELS;` 用 `Config.fromfile(...).model` 经 `MODELS.build` 构建整个 model，观察是否成功打印模块树。
5. **对照观察**：对比原配置（384 通道 head）与新配置（288），如果本地有 GPU 可各跑若干 iteration，观察显存占用与单 iter 耗时的变化方向。

**预期结果**：构建成功说明「backbone.out_channels → neck.in_channels」与「sum(neck.out_channels) → head.in_channels」两条链路都闭合；参数量、显存下降而精度可能略降——这是模型缩放最常用的旋钮之一。若第 2 步算错（如 `in_channels` 忘改），第 4 步会在 head 的卷积处报形状错误，正好亲手复现 4.4.4 的故障实验。

> 待本地验证：步骤 3-5 依赖本地环境与数据，本讲义未实际执行。

## 6. 本讲小结

- SECONDFPN 是「汇聚式」FPN：各支路并列独立地用反卷积/卷积把多尺度 BEV 特征对齐到**同一分辨率**，再沿通道维 concat，输出**单级**大通道特征图——与经典 FPN 的「自顶向下相加、多级输出」路线截然不同。
- 采样层的双模式：`stride > 1` 走 `ConvTranspose2d`（kernel=stride，保证整数倍无重叠放大）；`stride < 1` 经 `np.round(1/stride)` 换算走 `Conv2d` 下采样，CenterPoint 用 `[0.5, 1, 2]` 把汇聚点选在中间分辨率以省算力。
- `norm_cfg` 可配（BN 的 eps=1e-3/momentum=0.01 是小 batch 惯例，GN 写法见源码注释），激活函数硬编码 ReLU、不可经配置替换。
- 两条硬契约：`neck.in_channels == backbone.out_channels`（逐元素）；`head.in_channels == sum(neck.out_channels)`，CenterPoint 配置用 `sum([128, 128, 128])` 把后者写成了字面代码。
- 调用链：`extract_feat` 中 `backbone → (with_neck) neck`，返回列表交给 Anchor3DHead 的 `multi_apply` 逐级消费，训练时按每级 (H, W) 生成 anchor 网格。

## 7. 下一步学习建议

neck 输出的那张单级大通道 BEV 图，下一站就是 Anchor3DHead。下一讲 **u5-l4（Anchor3DHead 与 3D 锚框生成）**将回答：这张图上如何铺 3D anchor 网格、head 的 cls/bbox/dir 三条分支如何从 `feat_channels=512` 展开、`DeltaXYZWLHRBBoxCoder` 如何把 anchor 与回归值变成 7 自由度框。建议预习时先读 `mmdet3d/models/task_modules/anchor/anchor_3d_generator.py` 的 `grid_anchors`，把本讲 4.4 的 `featmap_sizes` 与它对接起来。

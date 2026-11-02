# u6-l1 PointNet++ 系列骨干网络

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出点云骨干与体素骨干（SECOND/PointPillars，见单元 5）的本质区别：不把点云栅格化，而是直接在「点的集合」上逐级抽象。
2. 拆解 PointNet++ 的核心组件 **SA 模块（Set Abstraction，集合抽象）** 的三步流程：**采样（Sample）→ 分组（Group）→ 聚合（Aggregate）**，并能写出每一步前后张量的形状。
3. 区分 `PointNet2SASSG`（单尺度分组）与 `PointNet2SAMSG`（多尺度分组）两条骨干路线，理解 F-FPS / D-FPS / FS 三种采样策略与膨胀分组（dilated group）。
4. 理解 `PointFPModule`（特征传播）如何用「3 近邻距离倒数加权插值」把粗层特征送回细层点。
5. 理解 `StackedSAModuleMSG`（堆叠版 SA 模块）与 `BasePointNet` 在输入接口上的约定差异：批量张量 vs 变长堆叠。

## 2. 前置知识

### 2.1 为什么点云不能直接套卷积

图像是规则网格，卷积核滑动即可。点云是**无序、稀疏、密度不均的点集合**：

- 相邻像素必然相邻，但「相邻点」需要用距离定义；
- 同一个物体近处点密、远处点疏，固定窗口会采到数量悬殊的邻居。

因此点云骨干必须自己回答两个问题：**在哪里计算特征（采样）**、**用哪些邻居计算（分组）**。这正是 SA 模块的由来。

### 2.2 体素路线与点路线的分野

单元 5 讲的 SECOND/PointPillars 走的是**体素路线**：先把点云栅格化成体素/柱体，再 scatter 成伪图像用 2D 卷积处理，感受野由「卷积核大小 × 下采样倍数」决定。

本单元讲的 PointNet++ 系列走**点路线**：始终保留「点」这一表示，每一层从 \(N\) 个点中选出 \(M\) 个代表点，每个代表点聚合自己邻域（半径 \(r\) 的球）内邻居的特征。感受野由 **radius** 显式给出，以米为单位，物理意义直观。

### 2.3 最远点采样（FPS）与球查询（ball query）

- **FPS（Farthest Point Sampling）**：迭代地挑选「距离已选点集最远的点」。设已选集合为 \(S\)，候选点 \(a\) 的覆盖距离为

\[
D(a, S) = \min_{s \in S} \lVert a - s \rVert_2
\]

每轮选使 \(D(a,S)\) 最大的点加入 \(S\)。它保证选出的点均匀覆盖整个点云，是「在哪里计算」的标准答案。
- **球查询（ball query）**：对每个代表点，收集半径 \(r\) 内最多 \(K\) 个邻居，是「用哪些邻居计算」的标准答案。

### 2.4 与前面讲义的衔接

- **u4-l2（检测器分层）**：backbone 是 `model` 配置里的一个键，由 `SingleStage3DDetector.extract_feat` 调用；本讲的骨干输出一个 **dict**（而不是体素路线的列表），这是室内检测器的常见约定。
- **u2-l3（BasePoints）与 u3-l8（Pack3DDetInputs）**：数据管线里的点云是 `BasePoints` 对象，但 `Pack3DDetInputs` 打包时会剥掉外壳只留裸张量；进入骨干的 `points` 是形状 `(B, N, 3+C)` 的普通张量。
- **u3-l4（室内数据集）**：训练管线用 `PointSample` 把每帧采样到固定点数，这解释了为什么骨干能用 `torch.stack` 把 batch 内样本堆成一个张量。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [mmdet3d/models/backbones/base_pointnet.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/backbones/base_pointnet.py) | `BasePointNet` 抽象基类，定义输入契约与坐标/特征切分 |
| [mmdet3d/models/backbones/pointnet2_sa_ssg.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/backbones/pointnet2_sa_ssg.py) | `PointNet2SASSG`：SA 下采样塔 + FP 上采样，VoteNet 的骨干 |
| [mmdet3d/models/backbones/pointnet2_sa_msg.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/backbones/pointnet2_sa_msg.py) | `PointNet2SAMSG`：多尺度分组骨干，3DSSD / PointRCNN 的骨干 |
| [mmdet3d/models/layers/pointnet_modules/point_sa_module.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/pointnet_modules/point_sa_module.py) | SA 模块三步抽象的实现（`BasePointSAModule` 及两个注册子类） |
| [mmdet3d/models/layers/pointnet_modules/point_fp_module.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/pointnet_modules/point_fp_module.py) | `PointFPModule`：特征传播（3 近邻加权插值） |
| [mmdet3d/models/layers/pointnet_modules/stack_point_sa_module.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/pointnet_modules/stack_point_sa_module.py) | `StackedSAModuleMSG`：变长堆叠版 SA 模块（PV-RCNN 关键点分支） |
| [mmdet3d/models/layers/pointnet_modules/builder.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/pointnet_modules/builder.py) | `SA_MODULES` 小注册器与 `build_sa_module` 工厂 |
| [mmdet3d/models/necks/pointnet2_fp_neck.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/necks/pointnet2_fp_neck.py) | `PointNetFPNeck`：把 FP 从骨干里拆出来当 neck（PointRCNN） |
| [configs/_base_/models/votenet.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/votenet.py) / [configs/_base_/models/3dssd.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/3dssd.py) / [configs/_base_/models/point_rcnn.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/point_rcnn.py) | 三个真实配置，用于核对通道推导 |
| [tests/test_models/test_backbones/test_pointnet2_sa_ssg.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_models/test_backbones/test_pointnet2_sa_ssg.py) / [test_pointnet2_sa_msg.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_models/test_backbones/test_pointnet2_sa_msg.py) | 官方单测，是本讲实践的重要依据 |

## 4. 核心概念与源码讲解

### 4.1 BasePointNet：点云骨干的输入契约

#### 4.1.1 概念说明

`BasePointNet` 是所有点云骨干的抽象基类。它本身几乎没有计算逻辑，真正的价值是**用代码固化了一条输入契约**：

- 输入是一个形状为 `(B, N, 3 + input_feature_dim)` 的张量：前 3 列固定是坐标，其余列是逐点特征（如反射率、颜色）；
- 配置里的 `in_channels` 指**整个张量的列数**（含坐标 3 列），这一点极易搞错。

#### 4.1.2 核心流程

```text
points (B, N, 3+C)
        │
        ├── xyz = points[..., 0:3]        → (B, N, 3)   「在哪里」
        └── features = points[..., 3:]    → (B, C, N)   「每个点长什么样」
              （注意转置：N, C → C, N，通道在前）
```

为什么特征要转成 `(B, C, N)``？因为后续所有卷积（`Conv1d`/`Conv2d`）都要求**通道维紧跟 batch 维**，点数当作空间维。这是点云模块全家桶的统一布局约定，后文 SA/FP 模块全部沿用。

#### 4.1.3 源码精读

基类定义与构造函数只处理权重初始化配置，并断言 `init_cfg` 与已废弃的 `pretrained` 不能同时设置——这是 MMEngine 时代的兼容逻辑：

[base_pointnet.py:12-24](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/backbones/base_pointnet.py#L12-L24) —— `BasePointNet` 继承 `BaseModule`，构造时校验 `init_cfg` 与 `pretrained` 互斥，旧的 `pretrained` 字符串被转换成 `init_cfg` 形式。

[base_pointnet.py:26-44](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/backbones/base_pointnet.py#L26-L44) —— 静态方法 `_split_point_feats`：切出坐标 `xyz (B,N,3)`，把特征转置成 `(B,C,N)`；若张量只有 3 列则返回 `features=None`，骨干退化为「只看几何」。

再看消费方——`SingleStage3DDetector.extract_feat` 如何把数据管线的点列表变成骨干输入：

[single_stage.py:158-163](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/single_stage.py#L158-L163) —— 从 `batch_inputs_dict['points']` 取出**每帧一个张量的列表**，`torch.stack` 堆成 `(B, N, C)` 后喂给 `self.backbone`；若有 neck 再过 neck。

`torch.stack` 要求 batch 内每帧点数相同，这正是室内管线（u3-l4）末尾 `PointSample(n_points=...)` 固定点数的原因。

#### 4.1.4 代码实践

**实践目标**：亲手验证输入契约与 `(B, C, N)` 布局转换。

**操作步骤**（示例代码，只需 PyTorch 与已安装的 mmdet3d，无需 GPU）：

```python
import torch
from mmdet3d.models.backbones.base_pointnet import BasePointNet

pts = torch.randn(2, 100, 6)          # (B=2, N=100, xyz+rgb)
xyz, feats = BasePointNet._split_point_feats(pts)
print(xyz.shape, feats.shape)         # 期望 (2,100,3) 与 (2,3,100)

xyz0, feats0 = BasePointNet._split_point_feats(pts[..., :3])
print(xyz0.shape, feats0)             # 期望 (2,100,3) 与 None
```

**需要观察的现象**：特征张量第 1、2 维发生了交换；3 列输入时特征为 `None`。

**预期结果**：`torch.Size([2, 100, 3]) torch.Size([2, 3, 100])`，随后打印 `None`。（逻辑上确定，待本地验证。）

#### 4.1.5 小练习与答案

1. **问**：VoteNet 配置 `in_channels=4`（ScanNet 点是 xyz + 1 个逐点特征），进入第一个 SA 模块 MLP 的「特征通道数」是多少？
   **答**：`sa_in_channel = in_channels - 3 = 1`。坐标 3 列不算特征，SA 模块会用 `use_xyz` 决定是否把坐标拼回特征（见 4.2）。
2. **问**：为什么骨干不直接接收 `LiDARPoints` 等 `BasePoints` 对象？
   **答**：`Pack3DDetInputs`（u3-l8）在打包时已把 `BasePoints` 剥成裸张量；骨干层只关心 `(B,N,C)` 布局，坐标系语义由数据管线保证，不需要对象外壳。

### 4.2 BasePointSAModule：采样—分组—聚合三步抽象

#### 4.2.1 概念说明

SA 模块是 PointNet++ 的心脏，回答两个问题：**在新一层放多少个「感知中心」**（采样），**每个中心看多大的邻域**（分组），然后**把邻域压成一个特征向量**（聚合）。`BasePointSAModule` 把三步拼成一条流水线，并支持多个尺度并行（MSG）。

#### 4.2.2 核心流程

以单尺度为例，一个 SA 模块的前向：

```text
输入: points_xyz (B, N, 3), features (B, C, N)
① 采样  FPS/指定索引 → new_xyz (B, M, 3), indices (B, M)
② 分组  对每个 new_xyz[i] 球查询 r 内 K 个邻居
        → grouped_features (B, C', M, K)   （C' = C+3 若 use_xyz）
        邻居坐标减中心坐标（可选再除以 r 即 normalize_xyz）
③ 聚合  共享 MLP（(1,1) Conv2d 逐对变换） → (B, C_out, M, K)
        在 K 维上 max/avg 池化        → (B, C_out, M)
输出: new_xyz, new_features, indices
```

多尺度（MSG）就是 ②③ 对多组 `(radius, K, mlp)` 并行执行，最后在通道维 `concat`：

\[
\text{out\_channels} = \sum_{k} \text{mlp}_k[-1]
\]

一个精妙的设计：**所有集合运算都被伪装成 2D 卷积**。分组结果是 4 维张量 `(B, C, M, K)`——M 个中心 × K 个邻居，用 kernel `(1,1)` 的 `Conv2d` 就实现了「对每对（中心, 邻居）共享权重的逐点 MLP」，再用 `max_pool2d(kernel=[1, K])` 在邻居维池化。这也解释了为什么配置里到处是 `BN2d`。

#### 4.2.3 源码精读

[point_sa_module.py:88-93](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/pointnet_modules/point_sa_module.py#L88-L93) —— 采样器：`num_point` 非 None 时构建 `PointsSampler`（mmcv 算子），按 `fps_mod` 列表逐档采样；`num_point=None` 时不采样，分组退化为 `GroupAll`（全局一组，用于提取全局特征）。

[point_sa_module.py:95-113](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/pointnet_modules/point_sa_module.py#L95-L113) —— 每个尺度一个 `QueryAndGroup` grouper（ball query）。注意第 99-102 行的**膨胀分组**：`dilated_group=True` 且非第 0 尺度时 `min_radius = radii[i-1]`，即只在「内半径 \(r_{i-1}\)、外半径 \(r_i\)」的球壳里找邻居，避免相邻尺度邻域大量重叠。

[point_sa_module.py:115-150](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/pointnet_modules/point_sa_module.py#L115-L150) —— `_sample_points` 三分支：外部给 `indices` 就直接 `gather_points` 取点；外部给 `target_xyz` 就原样使用（VoteNet 的 vote 聚合就是这么用的，见 u6-l2）；否则用 `points_sampler` 做 FPS。`fps_mod` 三种模式的语义：
- `D-FPS`：按欧氏距离做 FPS（原版 PointNet++）；
- `F-FPS`：按**特征距离**做 FPS（3DSSD 提出，让采样关注语义不同的点而不是仅仅空间分散）；
- `FS`：F-FPS 与 D-FPS 同时使用，各采一半。

[point_sa_module.py:152-173](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/pointnet_modules/point_sa_module.py#L152-L173) —— `_pool_features`：`max_pool2d` 的 kernel 写成 `[1, features.size(3)]`，即在邻居维 K 上取 max（或 avg），把 `(B,C,M,K)` 压回 `(B,C,M)`。

[point_sa_module.py:175-229](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/pointnet_modules/point_sa_module.py#L175-L229) —— `forward` 主体：先采样，再对**每个尺度**循环「grouper → mlp → pool」，最后 `torch.cat` 多尺度特征（第 229 行）。第 221-223 行是对 PAConv 卷积变体的特判（PAConv 前向返回二值 tuple，取第一个），说明 SA 模块是可插拔的——把 mlp 换成 PAConv 就得到 `PAConvSAModule` 系（同目录 `paconv_sa_module.py`，本讲不展开）。

[point_sa_module.py:289-306](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/pointnet_modules/point_sa_module.py#L289-L306) —— `PointSAModuleMSG` 的 MLP 构建：`use_xyz=True` 时每个尺度的 `mlp_channel[0] += 3`（把邻居相对坐标拼进特征），MLP 用 `ConvModule`（含 norm + 激活）串成 `nn.Sequential`。

[point_sa_module.py:309-354](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/pointnet_modules/point_sa_module.py#L309-L354) —— `PointSAModule`（SSG 用）。注意继承方向：**单尺度是多尺度的特例**——`PointSAModule` 继承 `PointSAModuleMSG`，把单尺度的 `mlp_channels/radius/num_sample` 包成单元素列表再调父类构造。

SA 模块有自己的小注册器，不走 u4-l1 讲的 `MODELS`：

[builder.py:7-9](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/pointnet_modules/builder.py#L7-L9) —— `SA_MODULES` 是独立 Registry，locations 指向 `mmdet3d.models.layers.pointnet_modules`。

[builder.py:12-45](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/pointnet_modules/builder.py#L12-L45) —— `build_sa_module` 工厂：从 `cfg` 里弹出 `type` 直接查 `SA_MODULES` 建类；`cfg=None` 时默认 `PointSAModule`。骨干配置中的 `sa_cfg=dict(type='PointSAModule', pool_mod=..., use_xyz=..., normalize_xyz=...)` 正是被它消费。

#### 4.2.4 代码实践

**实践目标**：用官方单测核对「MLP 第一层输入通道 = sa_in_channel + 3 (use_xyz)」这条推导规则。

**操作步骤**（源码阅读型实践）：

1. 打开 [test_pointnet2_sa_ssg.py:13-26](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_models/test_backbones/test_pointnet2_sa_ssg.py#L13-L26)；
2. 配置 `in_channels=6`、`sa_channels=((8,16),(16,16))`、默认 `use_xyz=True`；
3. 对比断言 `self.SA_modules[0].mlps[0].layer0.conv.in_channels == 6`。

**需要观察的现象**：`6 = (6-3) + 3`，即特征通道 3 加回拼入的相对坐标 3。

**预期结果**：推导规则与断言一致；用同样规则可算出 VoteNet（`in_channels=4`）第一个 SA 的 MLP 输入为 `1 + 3 = 4` 通道。

#### 4.2.5 小练习与答案

1. **问**：SA 模块为什么用 `Conv2d(kernel=(1,1))` 而不是 `Conv1d`？
   **答**：分组特征是 `(B, C, M, K)` 四维——M 个中心各带 K 个邻居；`(1,1)` 的 2D 卷积等价于对所有 (中心, 邻居) 对共享权重的逐点 MLP，配合 `max_pool2d(kernel=[1,K])` 在邻居维池化，一套 2D 算子完成集合聚合。
2. **问**：`normalize_xyz=True` 与 `False` 各是什么含义？VoteNet 与 3DSSD 分别用哪个？
   **答**：`True` 时邻居相对坐标会除以 radius，特征对尺度不敏感（VoteNet 用 True）；`False` 保留原始米制偏移（3DSSD/PointRCNN 用 False）。
3. **问**：如果想让 SA 模块不在内部采样、而是聚合「外部指定的一组中心点」的特征，怎么做？
   **答**：forward 时传入 `target_xyz`（或 `indices`），`_sample_points` 会走对应分支跳过 FPS；VoteNet 的 vote 聚合、StackedSAModuleMSG 都是这个用法。

### 4.3 PointNet2SASSG：SA 下采样塔 + FP 上采样

#### 4.3.1 概念说明

`PointNet2SASSG`（Single-Scale Grouping）是「经典版 PointNet++」：4 个 SA 模块逐级把点数减半再减半（2048→1024→512→256），每级单尺度球查询；再用 2 个 FP 模块把粗层特征**插值回传**到较细的点集上，形成 U 形结构（类似 2D 里的 UNet）。它同时输出各级 SA/FP 中间结果，供 VoteNet 这类检测头自由取用。

#### 4.3.2 核心流程

```text
               sa_xyz:   [N, 2048, 1024, 512, 256]     （点数递减）
输入 (B,N,C) → SA0 → SA1 → SA2 → SA3
               sa_features: [C0, 128, 256, 256, 256]   （通道递增）
                                    │
               fp_xyz:   [256, 512, 1024]              （点数回升）
               FP0 ← FP1（three_nn 加权插值 + skip 拼接）
```

FP（Feature Propagation）把粗层（source，m 个点）特征搬运到细层（target，n 个点）：对每个 target 点找 source 的 3 个最近邻，按**距离倒数加权**插值：

\[
w_i = \frac{1/d_i}{\sum_{j=1}^{3} 1/d_j}, \qquad
f_{\text{interp}} = \sum_{i=1}^{3} w_i \, f_i
\]

再与细层自身的 skip 特征在通道维拼接，过一个 MLP 融合。这保证了「每一层的每个点都有较丰富的上下文」。

#### 4.3.3 源码精读

[pointnet2_sa_ssg.py:36-53](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/backbones/pointnet2_sa_ssg.py#L36-L53) —— 构造参数即 PointNet++ 超参：`num_points`（各 SA 采多少中心）、`radius`（球半径，米）、`num_samples`（每球最多邻居数 K）、`sa_channels`（各 SA 的 MLP 通道）、`fp_channels`（各 FP 的 MLP 通道）、`sa_cfg`（SA 模块类型与开关）。

[pointnet2_sa_ssg.py:55-80](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/backbones/pointnet2_sa_ssg.py#L55-L80) —— SA 塔构建。关键变量 `skip_channel_list`：从 `[sa_in_channel]` 开始，每加一个 SA 就追加其输出通道，VoteNet 配置下为 `[1, 128, 256, 256, 256]`——它是「每一层点数对应的特征通道数」账本，后面 FP 全靠它对账。每个 SA 用 `build_sa_module` 从 `sa_cfg` 构建，MLP 头部拼接当前输入通道 `sa_in_channel`。

[pointnet2_sa_ssg.py:82-92](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/backbones/pointnet2_sa_ssg.py#L82-L92) —— FP 塔构建。通道规则：`FP_i` 的输入通道 = `fp_source_channel + fp_target_channel`，其中 source 是上一层（SA 或上一个 FP）的输出通道，target 是从 `skip_channel_list` 尾部 pop 出来的 skip 通道。VoteNet 下两个 FP 的 MLP 都是 `[256+256, 256, 256]`。

[pointnet2_sa_ssg.py:111-127](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/backbones/pointnet2_sa_ssg.py#L111-L127) —— forward 前半：切分坐标特征；第 114-115 行构造 `(B, N)` 的恒等索引；SA 循环逐级下采样，第 126-127 行用 `torch.gather` 把「本层采样索引」串成「相对原始输入点的索引链」，于是 `sa_indices[-1]` 永远指向最初的 N 个点。

[pointnet2_sa_ssg.py:129-138](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/backbones/pointnet2_sa_ssg.py#L129-L138) —— forward 后半：FP 循环。索引式 `sa_xyz[num_sa - i - 1]` / `sa_xyz[num_sa - i]` 表示「target 取更细一层、source 取更粗一层」；`fp_features` 列表边 append 边被下一个 FP 复用（链式）。`num_fp < num_sa` 时上采样不必回到原始点——VoteNet 停在 1024 点。

[pointnet2_sa_ssg.py:140-147](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/backbones/pointnet2_sa_ssg.py#L140-L147) —— 返回 dict：`fp_xyz / fp_features / fp_indices` 与 `sa_xyz / sa_features / sa_indices` 六个列表。VoteNet 的 `VoteHead` 取 `fp_xyz[-1]`（1024×3）与 `fp_features[-1]`（256 通道）作为 **seed points（种子点）**去投票（u6-l2 详述）。

FP 模块本体：

[point_fp_module.py:61-70](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/pointnet_modules/point_fp_module.py#L61-L70) —— `three_nn` 找 3 近邻，距离加 `1e-8` 防除零，倒数归一化成权重，`three_interpolate` 完成加权搬运；`source is None` 的分支（第 68-70 行）用于「全局特征广播到每个点」的场景（SA 塔末端用 GroupAll 时出现）。

[point_fp_module.py:72-79](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/pointnet_modules/point_fp_module.py#L72-L79) —— 插值特征与 target 自身特征 concat 后 `unsqueeze(-1)` 变 4 维，过 `(1,1)` Conv2d 组成的 MLP——又一次「把逐点运算伪装成 2D 卷积」。

配置对照（VoteNet，ScanNet 室内检测）：

[votenet.py:4-18](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/votenet.py#L4-L18) —— `in_channels=4`（xyz + 1 特征），`num_points=(2048,1024,512,256)`、`radius=(0.2,0.4,0.8,1.2)`、`sa_cfg` 用 `PointSAModule`（单尺度）+ `normalize_xyz=True`。逐级推导：

| 层 | 中心点数 | 半径 r | 每球邻居 K | 输出通道 |
| --- | --- | --- | --- | --- |
| 输入 | 4096（管线 PointSample 后） | - | - | 1 |
| SA0 | 2048 | 0.2 | 64 | 128 |
| SA1 | 1024 | 0.4 | 32 | 256 |
| SA2 | 512 | 0.8 | 16 | 256 |
| SA3 | 256 | 1.2 | 16 | 256 |
| FP0 → | 512 | - | - | 256 |
| FP1 → | **1024（seed）** | - | - | 256 |

#### 4.3.4 代码实践

**实践目标**：用 `skip_channel_list` 规则手工核算 VoteNet 两个 FP 模块的输入通道，并与配置文件互相印证。

**操作步骤**（纸笔 + 源码，无需运行）：

1. 写下 `skip_channel_list = [1, 128, 256, 256, 256]`；
2. `fp_source=256`（SA3 输出）、`fp_target=256`（pop 得 SA2）→ FP0 MLP 为 `[512, 256, 256]`；
3. FP0 输出 256 作为新 source，再 pop 得 256（SA1）→ FP1 MLP 为 `[512, 256, 256]`；
4. 对照 [votenet.py:12](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/votenet.py#L12) 的 `fp_channels=((256,256),(256,256))`，确认两个 FP 的 MLP 隐藏层均为 256。

**需要观察的现象**：FP 输入通道是「拼接和」，无法从 `fp_channels` 直接读出，必须经过推导。

**预期结果**：两个 FP 的实际 MLP 均为 `[512, 256, 256]`，且骨干最终输出 1024 点 × 256 通道，与 `VoteHead` 的 `vote_module_cfg.in_channels=256`（[votenet.py:22](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/votenet.py#L22)）严丝合缝。

#### 4.3.5 小练习与答案

1. **问**：`sa_indices` 为什么要用 `torch.gather` 链式回溯，而不是直接保存本层采样索引？
   **答**：第 i 层的索引是相对第 i-1 层点的；逐层 gather 后 `sa_indices[i]` 直接指向**原始输入点**，检测头（如 VoteNet）需要知道 seed/vote 点对应原始点云里的哪个点。
2. **问**：VoteNet 为什么只做 2 个 FP（停在 1024 点），而不是 4 个 FP 恢复到原始 4096 点？
   **答**：检测只需要在「足够密且数量可控」的种子点上做预测；1024 个 seed × 256 通道已是论文设定，FP 到底反而带来 4 倍计算量且不提升检测精度。
3. **问**：`sa_features[0]` 在 `in_channels=3`（纯坐标）输入时是什么？
   **答**：`None`——`_split_point_feats` 切不出特征列；SA 模块在 `features=None` 时仅靠相对坐标分组聚合（官方单测 [test_pointnet2_sa_ssg.py:63-74](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_models/test_backbones/test_pointnet2_sa_ssg.py#L63-L74) 验证了该路径）。

### 4.4 PointNet2SAMSG：多尺度分组与高级采样

#### 4.4.1 概念说明

`PointNet2SAMSG`（Multi-Scale Grouping）把每级 SA 的球查询从「一个半径」升级为「一组半径」，不同半径的邻域各自过 MLP 再拼接，最后可选用一个 1×1 卷积把拼接的大通道**聚合压缩**回设定宽度。它还开放了 F-FPS/FS 等高级采样策略（3DSSD 论文）与膨胀分组。**它没有 FP 塔**，输出按 `out_indices` 挑选若干级 SA 结果。

#### 4.4.2 核心流程

```text
每级 SA（MSG）：
  radii = (r1, r2, ...), sample_nums = (k1, k2, ...), sa_channels = ((..),(..),..)
  ① FPS 采样 num_point 个中心（fps_mods 决定采样策略）
  ② 对每个 r_i 独立 ball query（dilated_group 时是球壳）
  ③ 各尺度 MLP → pool → concat，得 sa_out_channel = Σ mlp_i[-1]
  ④ （可选）aggregation MLP: 1×1 Conv1d 把 sa_out_channel 压到 aggregation_channels
输出：dict(sa_xyz, sa_features, sa_indices)，长度 = 1 + len(out_indices)
```

#### 4.4.3 源码精读

[pointnet2_sa_msg.py:92-101](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/backbones/pointnet2_sa_msg.py#L92-L101) —— 多尺度 MLP 构建：每个尺度头部拼 `sa_in_channel`，`sa_out_channel` 累加各尺度最后一层输出。3DSSD 第一级 `((16,16,32),(16,16,32),(32,32,64))` 的输出为 `32+32+64=128`。

[pointnet2_sa_msg.py:103-126](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/backbones/pointnet2_sa_msg.py#L103-L126) —— `fps_mods`/`fps_sample_range_lists` 先规整成 list 再交给 `build_sa_module`（`sa_cfg` 默认 `PointSAModuleMSG`）。`fps_sample_range_list` 里的 `-1` 表示在全部点上采样，正数（如 3DSSD 第三级的 `(512, -1)`）表示只在「前 512 个点」里采——用于 F-FPS/D-FPS 混合时限定各自候选池。

[pointnet2_sa_msg.py:129-142](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/backbones/pointnet2_sa_msg.py#L129-L142) —— **聚合 MLP**：`aggregation_channels` 非 None 时，用 `Conv1d(kernel=1)+BN1d` 把多尺度拼接通道压到目标宽度，并作为下一级的 `sa_in_channel`；为 None 时直接把拼接通道透传（PointRCNN 全 None，3DSSD 为 `(64,128,256)`）。

[pointnet2_sa_msg.py:144-191](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/backbones/pointnet2_sa_msg.py#L144-L191) —— forward：SA 循环（无 FP），每级先过可选的 aggregation MLP；第 183-186 行只把 `out_indices` 命中的层追加进输出列表（连同最初的输入层共 `1 + len(out_indices)` 项）。第 82 行 `assert max(out_indices) < self.num_sa` 防止越界（单测 [test_pointnet2_sa_msg.py:53-73](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_models/test_backbones/test_pointnet2_sa_msg.py#L53-L73) 专门验证该断言）。

配置对照（3DSSD）：

[3dssd.py:4-21](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/3dssd.py#L4-L21) —— 第三级 `num_points=(256,256)` 配 `fps_mods=('F-FPS','D-FPS')`：两种策略各采 256 个点，共 512 个中心（单测 [test_pointnet2_sa_msg.py:48](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_models/test_backbones/test_pointnet2_sa_msg.py#L48) 断言末级 64 点来自 `(32,32)` 的同类设定）。`aggregation_channels=(64,128,256)` 把每级的 128/384/768 通道分别压到 64/128/256。

#### 4.4.4 代码实践

**实践目标**：核算 3DSSD 骨干每级「多尺度拼接通道 → 聚合通道」。

**操作步骤**（纸笔推导 + 对照单测）：

1. 第一级 `sa_channels=((16,16,32),(16,16,32),(32,32,64))` → 拼接 `32+32+64=128`，聚合到 `aggregation_channels[0]=64`；
2. 第二级 `((64,64,128),(64,64,128),(64,96,128))` → `128+128+128=384`，聚合到 128；
3. 第三级 `((128,128,256),(128,192,256),(128,256,256))` → `256+256+256=768`，聚合到 256。

**需要观察的现象**：下一级的输入通道等于上一级的**聚合后**通道（64→128→256 链条），而不是拼接通道。

**预期结果**：末级输出 `sa_features` 为 256 通道，与 `SSD3DHead.vote_module_cfg.in_channels=256`（[3dssd.py:25](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/3dssd.py#L25)）一致。

#### 4.4.5 小练习与答案

1. **问**：SSG 与 MSG 各自的优劣是什么？
   **答**：SSG 每级一个半径，结构简单、速度快，但单一尺度对物体大小变化敏感；MSG 多半径拼接（还可配膨胀球壳），特征更鲁棒，代价是通道数与计算量暴涨，因此常配 aggregation MLP 压缩。
2. **问**：`fps_mods=('FS',)` 是什么效果？
   **答**：F-FPS 与 D-FPS 同时启用、各采一半点——兼顾空间覆盖（D）与语义多样性（F），是 3DSSD 的核心技巧之一。
3. **问**：为什么 MSG 的默认 `sa_cfg` 是 `normalize_xyz=False` 而 SSG（VoteNet）是 True？
   **答**：3DSSD/PointRCNN 的室内外尺度差异大且后续分支关心米制偏移量，保留原始偏移更合适；VoteNet 场景相对均匀，归一化让特征对尺度更稳定。这是配置自由度，不是硬性规定。

### 4.5 StackedSAModuleMSG 与 PointNetFPNeck：SA/FP 抽象的两种延伸

#### 4.5.1 概念说明

SA/FP 是「积木」而不只属于骨干：

- `StackedSAModuleMSG`（堆叠版）：为 **PV-RCNN 的关键点分支**服务的 SA 变体。它改变输入接口——不再要求 batch 内每帧点数相同（不做 `torch.stack` padding），而是把多帧点**首尾拼接**成一个大张量 `(N1+N2+..., 3)`，另配一个 `batch_cnt` 张量记录每帧点数；且**不在内部采样**，中心点由外部给定。
- `PointNetFPNeck`：把 FP 上采样从 SSG 骨干里**拆出来当 neck**。PointRCNN 的骨干是 MSG（无 FP），需要恢复逐点特征时由这个 neck 完成。

#### 4.5.2 核心流程

```text
StackedSAModuleMSG 输入（变长堆叠）：
  xyz (N1+N2+..., 3), xyz_batch_cnt (N1, N2, ...)
  new_xyz (M1+M2+..., 3), new_xyz_batch_cnt (M1, M2, ...)
  features (N1+N2+..., C)          # 注意：点在行、通道在列，与 (B,C,N) 不同！
  → 球查询按 batch_cnt 分段进行 → 各尺度 MLP → pool → concat
  输出 new_features (M1+M2+..., Σ mlp_k[-1])

PointNetFPNeck：
  feat_dict['sa_xyz'/'sa_features']（MSG 骨干输出）
  → 自顶向下逐级 PointFPModule（target=细层, source=粗层）
  → dict(fp_xyz, fp_features)
```

#### 4.5.3 源码精读

[stack_point_sa_module.py:34-86](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/pointnet_modules/stack_point_sa_module.py#L34-L86) —— `StackQueryAndGroup`：调用 mmcv 的 `ball_query`（带两个 batch_cnt 的分段版本）；第 63-70 行处理**空球**——某中心半径内一个点都没有时 `idx[:,0]==-1`，把索引置 0 后将整组特征清零，避免垃圾值进入后续卷积；第 68 行 `grouped_xyz -= new_xyz` 与 QueryAndGroup 一样取相对坐标（但没有 normalize_xyz 选项）。

[stack_point_sa_module.py:152-199](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/pointnet_modules/stack_point_sa_module.py#L152-L199) —— `StackedSAModuleMSG.forward`：不采样（`new_xyz` 由外部给），逐尺度「分组 → reshape 成 (1, C, M, K) 借用 Conv2d → 在 K 维 max/mean → 拼回 (M, C_out)」，最后通道维 concat。注意它注册在 `MODELS`（第 89-90 行装饰器）而非 `SA_MODULES`，因为它直接出现在 PV-RCNN 配置的模块字典里。

真实用例：[pv_rcnn_8xb2-80e_kitti-3d-3class.py:101-141](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/pv_rcnn/pv_rcnn_8xb2-80e_kitti-3d-3class.py#L101-L141) —— PV-RCNN 在关键点分支用 4 个 `StackedSAModuleMSG`（不同 `scale_factor` 的体素中心）+ 1 个处理原始点（`rawpoints_sa_cfgs`），RoI 提取器里还内嵌一个（第 204 行附近）。这说明「SA 三步抽象」作为组件被复用到了骨干之外。

[pointnet2_fp_neck.py:34-40](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/necks/pointnet2_fp_neck.py#L34-L40) —— `PointNetFPNeck.__init__`：按 `fp_channels` 构建 FP 模块列表，逻辑与 SSG 骨干内如出一辙，只是搬进了 neck。

[pointnet2_fp_neck.py:64-89](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/necks/pointnet2_fp_neck.py#L64-L89) —— forward：从 `feat_dict` 取出 MSG 骨干的 `sa_xyz/sa_features`，自顶向下（`sa_xyz[-(i+2)]` 为 target、`sa_xyz[-(i+1)]` 为 source）逐级 FP，返回最后一层的 `fp_xyz/fp_features`。

PointRCNN 的通道账本最能说明这套推导规则的通用性。其 MSG 骨干（无聚合压缩，`aggregation_channels` 全 None）各级输出为 `96, 256, 512, 1024`（两尺度拼接：如第一级 32+64=96），输入层特征 1 通道，则 `skip_channel_list = [1, 96, 256, 512, 1024]`。对照 [point_rcnn.py:25-28](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/point_rcnn.py#L25-L28) 的 `fp_channels=((1536,512,512),(768,512,512),(608,256,256),(257,128,128))`：

| FP | source（上一层输出） | target（skip） | 输入通道 |
| --- | --- | --- | --- |
| FP0 | 1024（SA3） | 512（SA2） | 1536 |
| FP1 | 512（FP0 输出） | 256（SA1） | 768 |
| FP2 | 512（FP1 输出） | 96（SA0） | 608 |
| FP3 | 256（FP2 输出） | 1（输入特征） | 257 |

四个数字 1536/768/608/257 与配置完全吻合——这就是 `BasePointNet` 家族通道契约的可靠性：**只要给出 `in_channels`、`sa_channels`（及聚合宽度），任何配置的通道账都能在纸面上算清**。

#### 4.5.4 代码实践

**实践目标**：体会「变长堆叠」接口与 `(B,N,C)` 批量接口的差异。

**操作步骤**（源码阅读型实践，无需运行）：

1. 对比 [point_sa_module.py:175-181](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/pointnet_modules/point_sa_module.py#L175-L181)（`points_xyz (B,N,3)`，`features (B,C,N)`）与 [stack_point_sa_module.py:152-170](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/pointnet_modules/stack_point_sa_module.py#L152-L170)（`xyz (N1+N2+...,3)`，`features (N1+N2+...,C)`）两组 docstring；
2. 列出三处差异：batch 组织方式、特征布局（C 在第 1 维还是第 2 维）、采样是否内置。

**需要观察的现象**：`StackedSAModuleMSG` 的注释里明确写着 `(N1 + N2 ..., 3)` 与 `(M1, M2, ...)`。

**预期结果**：得出结论——堆叠版面向「两阶段检测器内部已经按帧分组的关键点/体素中心」，省去了 padding 与 stack；批量版面向「数据管线输出定长点云」的骨干主入口。

#### 4.5.5 小练习与答案

1. **问**：`StackedSAModuleMSG` 为什么不像 `BasePointSAModule` 那样内部做 FPS？
   **答**：它的中心点（new_xyz）由调用方给定——PV-RCNN 里是多尺度体素中心与固定采样的关键点，位置早已确定，无需再采样。
2. **问**：`StackQueryAndGroup` 如何处理「半径内没有任何点」的中心？
   **答**：`ball_query` 返回的 `idx` 首列为 -1 标记空球；索引置 0 防止越界后把该组特征整体清零（`grouped_xyz[empty_ball_mask] = 0`），让空中心得到零特征而不是随机值。
3. **问**：`PointNetFPNeck` 与 `PointNet2SASSG` 内置 FP 的最大区别是什么？
   **答**：组织方式不同——SSG 把 FP 内嵌在骨干里、输出多级 fp 列表；FPNeck 把 FP 独立成 neck（配置的 `neck=dict(type='PointNetFPNeck', ...)`），输入是 MSG 骨干输出的 sa 字典，只返回最后一层的 `fp_xyz/fp_features`。

## 5. 综合实践

**任务**：用随机点云跑通 `PointNet2SASSG.forward`，打印每个 SA/FP 层的输出点数与通道数，亲手画出 VoteNet 的层级下采样表。

> 环境要求：需要 GPU 与含预编译算子的 mmcv（`PointsSampler`/`QueryAndGroup`/`three_nn` 均为 mmcv CUDA 算子，官方单测在无 CUDA 时直接 skip）。无 GPU 时请改走第 4.3.4 节的纸面推导 + 阅读单测断言的替代路线。

**操作步骤**（示例代码）：

```python
import torch
from mmdet3d.registry import MODELS

# 直接采用 VoteNet 的骨干超参（configs/_base_/models/votenet.py）
backbone = MODELS.build(dict(
    type='PointNet2SASSG',
    in_channels=3,                                   # 纯坐标点云（随机点无特征）
    num_points=(2048, 1024, 512, 256),
    radius=(0.2, 0.4, 0.8, 1.2),
    num_samples=(64, 32, 16, 16),
    sa_channels=((64, 64, 128), (128, 128, 256),
                 (128, 128, 256), (128, 128, 256)),
    fp_channels=((256, 256), (256, 256)),
    norm_cfg=dict(type='BN2d'),
    sa_cfg=dict(type='PointSAModule', pool_mod='max',
                use_xyz=True, normalize_xyz=True))).cuda()

pts = torch.rand(1, 16384, 3).cuda()                 # (B, N, 3)
out = backbone(pts)

print('--- SA 下采样塔 ---')
for i, (xyz, feat) in enumerate(zip(out['sa_xyz'], out['sa_features'])):
    n_pts = xyz.shape[1]
    n_ch = 'None' if feat is None else feat.shape[1]
    print(f'sa[{i}]: {n_pts} 点, {n_ch} 通道')
print('--- FP 上采样塔 ---')
for i, (xyz, feat) in enumerate(zip(out['fp_xyz'], out['fp_features'])):
    print(f'fp[{i}]: {xyz.shape[1]} 点, {feat.shape[1]} 通道')
```

**需要观察的现象**：SA 列表点数 16384→2048→1024→512→256 逐级减半、通道 0(None)→128→256→256→256 递增；FP 列表点数 256→512→1024 回升、通道保持 256。注意 `sa_features[0]` 是 `None`（无输入特征）。

**预期结果**（下采样表，形状由源码逻辑唯一确定；实际运行待本地验证）：

| 阶段 | 输出点数 | 输出通道 |
| --- | --- | --- |
| 输入 | 16384 | 0（None） |
| SA0 (r=0.2, K=64) | 2048 | 128 |
| SA1 (r=0.4, K=32) | 1024 | 256 |
| SA2 (r=0.8, K=16) | 512 | 256 |
| SA3 (r=1.2, K=16) | 256 | 256 |
| FP0 | 512 | 256 |
| FP1（seed） | 1024 | 256 |

**延伸思考**：把 `in_channels` 换成 4、输入换成 `torch.rand(1, 16384, 4)`（第 4 列当反射率），重新跑一遍，观察 `sa_features[0]` 变成 `(1, 1, 16384)`——这正是 VoteNet 在 ScanNet 上的真实输入形态。

## 6. 本讲小结

- 点路线骨干不栅格化点云，用 **SA 模块三步抽象**逐级压缩：FPS 采样选中心 → 球查询分组取邻域 → 共享 MLP + 邻居维 max/avg 池化聚合；感受野以 `radius`（米）显式给出。
- `BasePointNet` 固化输入契约 `(B, N, 3+C)`；`_split_point_feats` 把特征转成 `(B, C, N)` 布局，此后一切逐点运算都伪装成 `(1,1)` Conv2d（这解释了满屏的 `BN2d`）。
- `PointNet2SASSG` = SA 下采样塔 + FP 上采样塔（U 形），FP 用 3 近邻距离倒数加权插值搬运特征；通道账本 `skip_channel_list` 决定 FP 输入通道，`in_channels/sa_channels` 给定后所有通道可纸面推导。
- `PointNet2SAMSG` 每级多半径并行、拼接后可用 1×1 卷积聚合压缩，支持 D-FPS/F-FPS/FS 采样策略、采样范围限定与膨胀球壳；**单尺度 `PointSAModule` 是多尺度 `PointSAModuleMSG` 的子类**（特例），二者经 `SA_MODULES` 小注册器构建。
- `StackedSAModuleMSG` 面向变长堆叠输入（`(N1+N2+...,3)` + `batch_cnt`，中心点外部给定、空球清零），被 PV-RCNN 用在骨干之外；`PointNetFPNeck` 把 FP 拆成独立 neck 服务 PointRCNN——SA/FP 是可复用积木。

## 7. 下一步学习建议

本讲只讲了「骨干如何从点云提取逐点特征」，下一讲 **u6-l2 VoteNet** 将消费本讲的输出：`fp_xyz[-1]`/`fp_features[-1]` 这 1024 个 seed 点如何经 `VoteModule` 投票偏移、再被一个 `PointSAModule`（`target_xyz` 分支的典型用例，见 [configs/_base_/models/votenet.py:34-41](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/votenet.py#L34-L41) 的 `vote_aggregation_cfg`）聚合成 proposal 中心，最后由 `VoteHead` 出框。建议先读 `mmdet3d/models/layers/vote_module.py` 与 `mmdet3d/models/dense_heads/vote_head.py`，带着「seed → vote → cluster → head」的问题意识进入下一讲；若想横向扩展，可顺带浏览同目录的 `paconv_sa_module.py`（把 SA 里的 MLP 换成 PAConv 动态卷积的变体）。

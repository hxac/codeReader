# 隐式体积几何 implicit-volume 与哈希编码

## 1. 本讲目标

DreamCraft3D 流水线的第一阶段 coarse-nerf 用一个「隐式体积」来表示三维形状。本讲精读这个表示的实现，学完后你应该能够：

1. 说出 implicit-volume 如何用一个 MLP 把三维坐标映射为「密度 + 特征」，以及 `configure` 中各子网络的分工。
2. 写出 `density_bias` 两种 blob 初始化（`blob_dreamfusion` 与 `blob_magic3d`）的数学形式，解释项目为何选择 Magic3D 初始化。
3. 解释 ProgressiveBandHashGrid 的由粗到细策略：`start_level`/`start_step`/`update_steps` 三个参数如何随训练步数逐层解锁哈希编码。
4. 跟踪 `forward` 返回的 `density`/`features`/`normal` 在渲染器中如何被消费，理解法向的三种计算方式。

## 2. 前置知识

### 2.1 隐式体积：用「雾的浓度场」表示形状

显式表示（网格、点云）直接存几何元素；隐式体积表示存的是一个**函数** \( f_\theta: \mathbb{R}^3 \to \mathbb{R}^+ \)，输入空间中任意一点，输出该点的体积密度（可以理解为「雾的浓度」）。物体表面并不显式存在，而是密度从高到低的**过渡带**；配合体渲染（下一讲详讲），沿着射线把路径上的密度累积起来就得到图像，整个过程对 \(\theta\) 可微，于是能用梯度下降「雕刻」这个密度场。

### 2.2 哈希编码：让小 MLP 也能表达高频细节

纯 MLP 直接吃 \(xyz\) 坐标，表达高频细节的能力有限。Instant-NGP 提出的**多分辨率哈希编码**把 \([0,1]^3\) 空间按 16 层由粗到细的网格划分（本项目中第 1 层约 \(16^3\)，第 16 层约 \(4096^3\)），每层每格存一个可学习的特征向量，查询时按坐标查表取特征、拼接后喂给 MLP。本项目通过 tiny-cuda-nn（tcnn）实现，因此**训练 implicit-volume 必须有 NVIDIA GPU**。

### 2.3 术语表

| 术语 | 含义 |
|---|---|
| density（密度） | 单位长度的遮挡程度，体渲染权重的基础 |
| raw density（原始密度） | 未过激活函数的网络输出，加了 bias 之后的值 |
| softplus | \( \mathrm{softplus}(x)=\log(1+e^x) \)，把任意实数压到非负，比 exp 数值稳定 |
| blob 初始化 | 训练开始时给密度加一个以原点为中心的偏置，让场景从「一团球状雾」出发 |
| bbox / unisphere | 包围盒 \([-\text{radius}, \text{radius}]^3\)；归一化到 \((0,1)\) 的单位球空间 |
| level（层级） | 哈希编码的第几层分辨率，level 越大网格越细 |

### 2.4 与前面讲义的衔接

u3-l2 讲过 `BaseModule` 生命周期（解析配置 → 绑定设备 → `configure` → 可选加载权重）与 `Updateable.update_step` 钩子；u3-l3 讲过 `BaseLift3DSystem.configure` 里 `find(geometry_type)` 实例化几何。本讲就站在那个交接点上，往几何内部走一层。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `threestudio/models/geometry/implicit_volume.py` | 本讲主角：`ImplicitVolume` 类，密度场 + 特征场 + 法向 |
| `threestudio/models/geometry/base.py` | `BaseImplicitGeometry` 基类：bbox、坐标归一化、等值面提取接口 |
| `threestudio/models/networks.py` | 编码器与 MLP 工厂：`ProgressiveBandHashGrid`、`get_encoding`、`get_mlp` |
| `configs/dreamcraft3d-coarse-nerf.yaml` | coarse 阶段配置，本讲关注 `system.geometry` 段（L44-73） |
| `threestudio/models/renderers/nerf_volume_renderer.py` | 消费方：调用 `geometry(...)` 得到 `geo_out` |
| `threestudio/models/materials/no_material.py` | 消费方：把 `features` 直接变成 RGB |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：几何骨架与组装、密度偏置初始化、渐进哈希编码、forward 输出契约与法向。

### 4.1 隐式体积的骨架：BaseImplicitGeometry 与 ImplicitVolume.configure

#### 4.1.1 概念说明

`ImplicitVolume` 是注册名为 `implicit-volume` 的几何组件（回顾 u1-l3：yaml 里 `geometry_type: "implicit-volume"` 的值就是注册名）。它内部其实是两个独立的小网络：

- **density_network**：编码 → 1 维原始密度；
- **feature_network**：同一份编码 → `n_feature_dims` 维特征（coarse 阶段为 3 维，直接当颜色用）。

注意它们**不共享 MLP、只共享编码**——密度管形状，特征管外观，两者解耦，这正是后续阶段能「换渲染器、保几何」的基础（coarse-neus 阶段转换时 `create_from` 会整体拷贝 encoding 与 density_network，见 u3-l3）。

基类 `BaseImplicitGeometry` 提供两件基础设施：包围盒 `bbox`（由 `radius` 构造的 buffer）和坐标归一化 `contract_to_unisphere`，以及一整套等值面提取接口（`forward_field`/`forward_level`/`isosurface`，供阶段转换和导出时把密度场变成网格）。

#### 4.1.2 核心流程

`ImplicitVolume` 被实例化到被查询的链路：

```text
find("implicit-volume")(cfg_geometry)
  └─ BaseObject.__init__: parse_structured 校验配置 → get_device() → configure()
       └─ configure:
            encoding       = get_encoding(3, pos_encoding_config)   # 本讲 4.3
            density_network  = get_mlp(enc.n_output_dims, 1, mlp_net_cfg)
            feature_network  = get_mlp(enc.n_output_dims, 3, mlp_net_cfg)  # n_feature_dims>0 时
            normal_network   = get_mlp(enc.n_output_dims, 3, ...)   # 仅 normal_type="pred"
查询时（渲染器调用）:
  forward(points)
    └─ points: [-radius, radius]^3 --contract_to_unisphere--> (0,1)
         --encoding--> 特征 --density_network--> 原始密度 --(+bias, 激活)--> density
```

`contract_to_unisphere` 在 `unbounded=False`（本项目默认，`configure` 中 `self.unbounded = False`）时只是线性缩放：把 `bbox` 范围映射到 \((0,1)\)，因为 tcnn 编码要求输入落在 \([0,1]\)。

#### 4.1.3 源码精读

注册与类定义——`@threestudio.register("implicit-volume")` 把类写入注册表，配置里 `geometry_type` 的值就是这里的字符串：

[threestudio/models/geometry/implicit_volume.py:19-26](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/implicit_volume.py#L19-L26)

上面这段定义了 `ImplicitVolume` 及其 `Config` 默认值：`n_feature_dims=3`、`density_activation="softplus"`、`density_bias="blob_magic3d"`。注意 `pos_encoding_config` 的默认 `otype` 是 `"HashGrid"`（普通 tcnn 哈希网格，**不渐进**），而 coarse-nerf 配置把它覆盖成了 `ProgressiveBandHashGrid` 并追加三个调度参数（见 4.3）：

[threestudio/models/geometry/implicit_volume.py:29-47](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/implicit_volume.py#L29-L47)

`configure` 组装子网络——先建编码，再用 `encoding.n_output_dims` 作为 MLP 输入维度，分别建密度头与特征头；`normal_type == "pred"` 时才建法向头：

[threestudio/models/geometry/implicit_volume.py:63-82](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/implicit_volume.py#L63-L82)

基类 `configure` 建立 bbox buffer（`radius` 来自 yaml 的 `system.geometry.radius: 2.0`，即世界坐标 \([-2,2]^3\)），并标记 `unbounded=False`：

[threestudio/models/geometry/base.py:70-83](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/base.py#L70-L83)

坐标归一化 `contract_to_unisphere`——`unbounded=False` 分支仅做 `scale_tensor` 线性映射；`scale_tensor` 就是标准的「从输入区间仿射到目标区间」：

[threestudio/models/geometry/base.py:20-32](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/base.py#L20-L32)
[threestudio/utils/ops.py:27-38](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/ops.py#L27-L38)

#### 4.1.4 代码实践

**实践目标**：不训练，只实例化一个 `ImplicitVolume`，打印它的模块树，验证 4.1.2 的组装流程。

**操作步骤**（示例代码，需 GPU 环境，待本地验证）：

```python
# inspect_geometry.py（示例代码）
from omegaconf import OmegaConf
import threestudio  # import 即触发全部注册（见 u3-l1）

cfg = OmegaConf.create({
    "radius": 2.0,
    "normal_type": "finite_difference",
    "density_bias": "blob_magic3d",
    "pos_encoding_config": {
        "otype": "ProgressiveBandHashGrid",
        "n_levels": 16, "n_features_per_level": 2,
        "log2_hashmap_size": 19, "base_resolution": 16,
        "per_level_scale": 1.447269237440378,
        "start_level": 8, "start_step": 2000, "update_steps": 500,
    },
})
geo = threestudio.find("implicit-volume")(cfg)
print(geo)          # 打印 nn.Module 树
n_params = sum(p.numel() for p in geo.parameters())
print(f"total params: {n_params}")
print(f"encoding output dims: {geo.encoding.n_output_dims}")
```

**需要观察的现象**：模块树中只有 `encoding`、`density_network`、`feature_network` 三个子模块（`normal_type` 是 `finite_difference`，没有 `normal_network`）；参数量绝大部分在 encoding（16 层 × 2 特征 × \(2^{19}\) 槽位的哈希表）而非 64×1 的小 MLP。

**预期结果**：`encoding output dims: 32`（16 层 × 每层 2 特征；`include_xyz=False` 时 CompositeEncoding 不追加维度）。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：把 yaml 里 `geometry.radius` 从 2.0 改成 1.0，密度偏置的球会变大还是不变？
**答案**：不变。`get_activated_density` 用的是归一化**之前**的世界坐标（`points_unscaled`），球半径只由 `density_blob_std` 决定；`radius`/bbox 只影响哈希编码看到的坐标范围——同一个球在更小的 bbox 里占用更粗的哈希层级，等效细节分辨率更高。

**练习 2**：为什么编码输入必须落在 \((0,1)\)？
**答案**：tcnn 的哈希网格以 \([0,1]^3\) 为定义域，超出范围的坐标会按网格取模折叠，产生错误的高频信号；`contract_to_unisphere` 就是为保证这一约定而存在。

**练习 3**：`ImplicitVolume.Config` 里 `isosurface_threshold` 默认 25.0，而基类 `BaseImplicitGeometry.Config` 默认 0.0，实际生效的是哪个？
**答案**：25.0。dataclass 继承时子类字段默认值覆盖父类，`parse_structured` 按子类 `Config` 校验（见 [threestudio/models/geometry/implicit_volume.py:56](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/implicit_volume.py#L56) 与 [threestudio/models/geometry/base.py:61](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/base.py#L61)）。

### 4.2 密度偏置初始化：blob_magic3d vs blob_dreamfusion

#### 4.2.1 概念说明

训练开始时 MLP 权重随机，密度场近乎无意义的噪声。而体渲染需要一个「有东西可拍」的初始场景，SDS 梯度才有附着点。解决办法是在原始密度上**加一个手工设计的、以原点为中心的偏置**（blob），让场景从一团半径约 `density_blob_std` 的球状雾开始，再让扩散先验的梯度把它雕刻成目标形状。

两种偏置的数学形式（\(r=\|\mathbf{x}\|\)，\(s\) 为 `density_blob_scale`，\(\sigma\) 为 `density_blob_std`）：

- DreamFusion 高斯型：
  \[ b_{\text{df}}(r) = s \cdot \exp\!\left(-\frac{r^2}{2\sigma^2}\right) \]
- Magic3D 线性型（center bias）：
  \[ b_{\text{m3d}}(r) = s \cdot \left(1 - \frac{r}{\sigma}\right) \]

关键差别：高斯在 \(r>\sigma\) 后**指数级**归零，初始体积「又瘦又硬」；线性偏置在 \(r=\sigma\) 处过零、继续线性变负，配合 softplus 激活形成一个**从中心向外逐渐变稀的锥形过渡带**，边界更平缓、梯度更好雕刻。本项目 yaml 中明确注释 DreamFusion 初始化 "does not work very well"，选用 Magic3D。

#### 4.2.2 核心流程

`get_activated_density(points, density)` 的执行过程：

```text
1. 按 cfg.density_bias 计算 density_bias:
     blob_dreamfusion → 高斯偏置（用原始尺度坐标 points）
     blob_magic3d     → 线性偏置
     float            → 常数偏置
2. raw_density = density(网络输出) + density_bias
3. density = activation(raw_density)   # coarse 阶段为 softplus
返回 (raw_density, density)
```

注意第 1 步的坐标是**未归一化**的世界坐标（`forward` 里先存 `points_unscaled` 再 contract，见 4.4），所以 blob 的尺度语义与 `radius` 无关。

#### 4.2.3 源码精读

两种偏置的计算与激活——`blob_dreamfusion` 用 `(points**2).sum(-1)` 即 \(r^2\)，`blob_magic3d` 用 `sqrt((points**2).sum(-1))` 即 \(r\)，最后统一 `raw = density + bias` 再过激活：

[threestudio/models/geometry/implicit_volume.py:84-111](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/implicit_volume.py#L84-L111)

coarse-nerf 配置中的选择——注释掉的 4 行正是 DreamFusion 原参数（`exp` 激活 + scale 5 + std 0.2），生效的是 Magic3D 组合（`softplus` + scale 10 + std 0.5）：

[configs/dreamcraft3d-coarse-nerf.yaml:49-60](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L49-L60)

激活函数来自 `get_activation` 查表（`softplus`/`exp`/`trunc_exp` 等都在表中）：

[threestudio/utils/ops.py:78-95](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/ops.py#L78-L95)

一个值得注意的源码细节：`update_step` 中的退火选项 `anneal_density_blob_std_config` 把新值写到**实例属性** `self.density_blob_std`（[threestudio/models/geometry/implicit_volume.py:283-291](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/implicit_volume.py#L283-L291)），而 `get_activated_density` 读取的是 `self.cfg.density_blob_std`——两者不是同一个存储，疑似退火值不会被实际使用；好在 coarse-nerf 未启用该选项（保持 `None`），不影响本讲主线。

#### 4.2.4 代码实践

**实践目标**：用 numpy 画出两种偏置在激活前后的径向曲线，直观理解「锥形 vs 高斯」。

**操作步骤**（示例代码，纯 CPU 可运行，待本地验证）：

```python
# plot_blob_bias.py（示例代码）
import numpy as np, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

r = np.linspace(0, 2.0, 400)
m3d = 10.0 * (1 - r / 0.5)                 # blob_magic3d: s=10, σ=0.5
df  = 5.0 * np.exp(-0.5 * r**2 / 0.2**2)   # blob_dreamfusion: s=5, σ=0.2
softplus = lambda x: np.log1p(np.exp(-np.abs(x))) + np.maximum(x, 0)  # 数值稳定写法

fig, axes = plt.subplots(1, 2, figsize=(10, 4))
axes[0].plot(r, m3d, label="blob_magic3d (s=10, σ=0.5)")
axes[0].plot(r, df, label="blob_dreamfusion (s=5, σ=0.2)")
axes[0].axhline(0, color="gray", lw=0.5)
axes[0].set(xlabel="r = ||x||", ylabel="raw bias", title="pre-activation")
axes[0].legend()
axes[1].plot(r, softplus(m3d), label="softplus(magic3d)")
axes[1].plot(r, np.exp(df), label="exp(dreamfusion)")
axes[1].set(xlabel="r = ||x||", ylabel="activated density", title="post-activation")
axes[1].legend()
plt.savefig("blob_bias.png", dpi=150)
print("saved blob_bias.png")
```

**需要观察的现象**：左图中 magic3d 是一条在 \(r=0.5\) 处过零、之后为负的直线；dreamfusion 是集中在 \(r<0.4\) 的尖峰。右图中 softplus(magic3d) 从约 10 平滑滑落到 0，过渡带宽；exp(dreamfusion) 在 \(r>0.5\) 后彻底为 0。

**预期结果**：右图两条曲线的「非零支撑半径」与「边界斜率」差异一目了然——magic3d 支撑约 0.5 且边界渐变，dreamfusion 支撑约 0.4 且边界陡峭归零。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：手算 `blob_magic3d`（s=10, σ=0.5）在 \(r=0.25\) 处的 raw bias。
**答案**：\(10 \times (1 - 0.25/0.5) = 10 \times 0.5 = 5\)。

**练习 2**：为什么 DreamFusion 初始化要配 `exp` 激活和更小的 scale/std？
**答案**：`exp` 会指数放大 raw 值，若沿用 s=10，中心密度达 \(e^{10}\approx 2.2\times10^4\)，完全不透明、梯度饱和；把 scale 降到 5、std 降到 0.2 是在压量级。反过来看，softplus 只做线性压缩（\(\mathrm{softplus}(x)\approx x\) 当 \(x\) 较大），量级好控，这也是 Magic3D 组合更稳的原因之一。

**练习 3**：blob 偏置是可学习的参数吗？它会随训练消失吗？
**答案**：不是参数、永不更新。它是一个固定的解析函数，始终加在网络输出上；训练中网络输出 `density` 会逐渐学出负值去「抵消」不需要的偏置（比如把球外区域的 raw 压到很负），从而实现形状雕刻。

### 4.3 ProgressiveBandHashGrid：由粗到细的哈希编码

#### 4.3.1 概念说明

哈希编码的高频层（细网格）是把双刃剑：表达细节能力强，但训练初期就启用会让密度场充满高频噪声，法向（由密度差分求得，见 4.4）极度毛糙。**渐进式策略**是：训练初期只解锁前 `start_level` 层（粗网格），随步数推进逐层放开细网格——先雕塑大形、再刻画细节，同时保证任意时刻被启用的最高频率有限，差分法向保持平滑。这正是 coarse-nerf yaml 注释所写 "coarse to fine hash grid encoding / to ensure smooth analytic normals"。

实现方式非常轻量：外层包装一个 **mask 向量**，对 tcnn 编码输出按特征段乘 0/1，被屏蔽的层不参与前向，网络其余部分（包括 MLP 输入维度）保持不变。

#### 4.3.2 核心流程

层级解锁的调度公式（`update_step` 每个训练批次前被调用，见 u3-l2 的 Updateable 机制）：

\[
\text{level}(t) = \min\!\left(\text{start\_level} + \left\lfloor \frac{\max(t - \text{start\_step},\, 0)}{\text{update\_steps}} \right\rfloor,\; n_{\text{levels}}\right)
\]

第 \(\ell\) 层的网格分辨率为：

\[
\text{res}(\ell) = \text{base\_resolution} \cdot (\text{per\_level\_scale})^{\ell - 1}
\]

代入 coarse-nerf 参数（base=16, scale=1.447, n_levels=16, start_level=8, start_step=2000, update_steps=500）：

| step | level | 网格分辨率 |
|---|---|---|
| 0 | 8 | \(16 \times 1.447^7 \approx 204\) |
| 2500 | 9 | ≈ 295 |
| 4500 | 13 | ≈ 1300 |
| ≥ 6000 | 16（封顶） | \(16 \times 1.447^{15} \approx 4083 \approx 4096\) |

即训练开始时只有约 \(200^3\) 的网格生效（yaml 注释 "start_level: 8 # resolution ~200"），4000 步后逐步放开到 \(4096^3\)。

构造时序上有一个细节：`__init__` 里 `mask` 初始化为**全零**，且不像 `ProgressiveBandFrequency` 那样在构造尾调用 `update_step`；真正解锁发生在训练循环 `on_train_batch_start` 触发的第一次 `update_step(0, 0)`——此时 `level(0)=start_level=8`，前 8 层立即点亮。

#### 4.3.3 源码精读

`ProgressiveBandHashGrid.__init__`——把配置改写为 tcnn 的 `Grid` + `type: Hash` 组合（tinycudann PyTorch API 的写法），记录三个调度参数，mask 初始化为全零向量（长度 = 层数 × 每层特征数 = 32）：

[threestudio/models/networks.py:129-151](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/networks.py#L129-L151)

前向与调度——`forward` 只做一件事：`enc * self.mask`；`update_step` 按上面公式算 `current_level` 并把前 `current_level * n_features_per_level` 个特征位一次性置 1（只增不减）：

[threestudio/models/networks.py:153-167](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/networks.py#L153-L167)

工厂 `get_encoding`——按 `otype` 分发：`ProgressiveBandHashGrid` 走渐进包装，其他直接用 `TCNNEncoding`；最后统一套 `CompositeEncoding`（`include_xyz=False` 时等价于恒等包装）：

[threestudio/models/networks.py:194-211](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/networks.py#L194-L211)

配置侧——coarse-nerf 的 `pos_encoding_config` 覆盖了默认值，多出的三个键 `start_level/start_step/update_steps` 只被 `ProgressiveBandHashGrid`（以及 implicit_volume 的 progressive 法向 eps，见 4.4）消费：

[configs/dreamcraft3d-coarse-nerf.yaml:62-73](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L62-L73)

顺带认识 MLP 侧：`VanillaMLP` 是无 bias 的 Linear + ReLU 堆叠（`n_hidden_layers=1` 即单隐层 64），前向里**显式关闭 autocast**，因为 AMP 下会出现空梯度（源码注释原话）：

[threestudio/models/networks.py:214-244](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/networks.py#L214-L244)

#### 4.3.4 代码实践

**实践目标**：不依赖 GPU，纯 Python 复现层级调度，画出解锁时间线。

**操作步骤**（示例代码，纯 CPU 可运行，待本地验证）：

```python
# simulate_level_schedule.py（示例代码）
n_levels, start_level, start_step, update_steps = 16, 8, 2000, 500
base, scale = 16, 1.447269237440378
n_feat = 2

for t in [0, 1999, 2000, 2500, 3001, 4500, 6000, 12000]:
    level = min(start_level + max(t - start_step, 0) // update_steps, n_levels)
    res = base * scale ** (level - 1)
    unlocked = level * n_feat
    print(f"step {t:>5}: level={level:>2}  grid≈{res:>6.0f}^3  mask 前 {unlocked}/32 位为 1")
```

**需要观察的现象**：step 0 与 step 1999 都是 level 8（`max(t-2000,0)` 截断负数）；step 2500 跳到 9；step 6000 达到 16 封顶，之后不再变化。

**预期结果**：输出与 4.3.2 表格一致。若把 `start_step` 改成 0，step 0 就从 level \(8+0=8\) 起步但每 500 步稳定爬升——注意本配置里 `start_step=2000` 的语义是「**前 2000 步冻结在 start_level**」。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么用乘性 mask 而不是直接把未解锁层的特征从输出里删掉？
**答案**：删特征会改变编码输出维度，从而改变下游 MLP 的输入维度，权重形状无法保持连续；乘 0 在数值上等价于屏蔽（该层特征不参与计算），但维度恒定，网络结构无需重建。

**练习 2**：若把 `start_level` 从 8 改成 4，初始有效网格分辨率变为多少？对初始密度场有什么影响？
**答案**：\(16 \times 1.447^3 \approx 48\)，约 \(48^3\)。初始密度场只能表达更粗的结构，网络输出的空间变化更平滑（近乎常数），密度切片几乎只反映 blob 偏置的形状；好处是前几千步的法向更平滑，坏处是细节起步更晚。

**练习 3**：mask 只置 1 从不置 0，这对续训（resume）有什么意义？
**答案**：层级单调递增保证调度是「时间的一致函数」——同一 `global_step` 恢复出同一 level；`BaseModule` 加载权重后还会以检查点的 `global_step` 补一次 `do_update_step(on_load_weights=True)`（u3-l2），确保续训时 mask 状态与训练进度对齐，而不是从 level 8 重来。

### 4.4 forward 的输出契约：density/features/normal 如何被渲染器消费

#### 4.4.1 概念说明

`ImplicitVolume.forward(points, output_normal)` 是几何与渲染器之间的**接口契约**：输入任意形状 `(..., 3)` 的世界坐标，返回一个 dict，至少含 `density`（激活后），可含 `features` 与 `normal`/`shading_normal`。渲染器（nerf-volume-renderer，下一讲主角）在 ray marching 采样出的位置上调用它：

- `density` → 交给 nerfacc 计算每条射线的渲染权重；
- `features` → 整包传给 material；coarse 阶段的 `no-material` 直接对 3 维特征过 sigmoid 得到 RGB——**特征即颜色**；
- `normal` → 用于法向平滑正则与（后续阶段的）光照。

法向有三种来源（`normal_type` 配置）：`finite_difference`（对密度做空间差分，coarse 阶段默认）、`pred`（额外 MLP 预测）、`analytic`（autograd 求精确梯度）。此外还有 `finite_difference_laplacian` 中心差分变体。coarse-nerf 用 `finite_difference` + 固定 eps 0.01。

#### 4.4.2 核心流程

```text
forward(points, output_normal):
  1. points_unscaled = points                      # 保留世界坐标（blob、法向差分都要用）
  2. points = contract_to_unisphere(points)        # → (0,1)
  3. enc = encoding(points)                        # ProgressiveBandHashGrid（乘 mask）
  4. density = density_network(enc)                # raw，未加 bias
  5. raw_density, density = get_activated_density(points_unscaled, density)
  6. features = feature_network(enc)               # n_feature_dims=3
  7. 若 output_normal:
       finite_difference: 在 ±eps 偏移点上再算 3 次 forward_density，
         normal = -(D(x+εeᵢ) - D(x)) / ε，归一化    # 单边差分，负号=指向密度下降方向
  8. 返回 {density, features, normal, shading_normal}
```

法向为什么是**负**差分：密度在靠近表面外侧变大，表面的外法向指向密度**减小**的方向，所以取负梯度；差分步长 `finite_difference_normal_eps` 默认 0.01（世界尺度，radius=2 的场景下即 1% 半径）。

`update_step` 中的 progressive eps 选项（Neuralangelo 策略）会把差分步长绑定到当前哈希层级对应的网格尺寸 \(2 \cdot \text{radius}/\text{res}\)，让差分尺度与编码分辨率同步变细；coarse-nerf 未启用（用的是固定 float 0.01）。

#### 4.4.3 源码精读

forward 主体——坐标归一化、编码、密度头、激活，随后组装 output dict（`density` 恒有，`features` 视 `n_feature_dims`，法向视 `output_normal`）：

[threestudio/models/geometry/implicit_volume.py:113-139](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/implicit_volume.py#L113-L139)

finite_difference 法向——沿 x/y/z 三个方向各偏移 eps 构造 3 个采样点（clamp 在 ±radius 内），复用 `forward_density` 得到偏移密度，做单边差分后归一化；`normal` 与 `shading_normal` 同值写入返回：

[threestudio/models/geometry/implicit_volume.py:141-182](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/implicit_volume.py#L141-L182)

update_step 中的 progressive eps——用与 4.3 完全相同的 level 公式推出当前网格分辨率，把差分步长设为一个网格单元的边长：

[threestudio/models/geometry/implicit_volume.py:293-321](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/implicit_volume.py#L293-L321)

渲染器消费现场——训练分支里 `geo_out = self.geometry(positions, output_normal=...)` 一次拿全，`**geo_out` 整包解包给 material：

[threestudio/models/renderers/nerf_volume_renderer.py:281-292](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nerf_volume_renderer.py#L281-L292)

no-material 把 features 变颜色——`use_network=False`（未配 mlp_network_config）时直接对特征过 `color_activation`（默认 sigmoid）：

[threestudio/models/materials/no_material.py:41-54](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/materials/no_material.py#L41-L54)

另外两个供阶段转换/导出使用的瘦接口：`forward_field` 直接复用 `forward_density`（implicit-volume 不支持可变形网格，会警告忽略）；`forward_level` 把密度场翻成「零值即表面」的 level 函数 \(-(\text{field} - \text{threshold})\)，供 marching tetrahedra 提取网格（阈值即 4.1 练习 3 提到的 25.0）：

[threestudio/models/geometry/implicit_volume.py:203-227](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/implicit_volume.py#L203-L227)

#### 4.4.4 代码实践

**实践目标**：用源码阅读法量化 `output_normal=True` 的额外开销，并验证坐标尺度链路。

**操作步骤**：

1. 读 [threestudio/models/geometry/implicit_volume.py:171-181](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/implicit_volume.py#L171-L181)，数一数 `finite_difference` 分支一共发生了几次 `forward_density` 调用、每次涉及多少个点（N 个查询点 × 3 个偏移）。
2. 读 [threestudio/models/renderers/nerf_volume_renderer.py:399-403](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nerf_volume_renderer.py#L399-L403)，注意 `normal_perturb` 是在**随机扰动 1e-2 的位置**上再次调用 `self.geometry(...)`——正则项 `lambda_normal_smooth` 就来自这里（u2-l3 讲过该 loss 键）。
3. 在纸上追踪一个坐标：世界坐标 \(x=(-2, -2, -2)\)（bbox 角点）经 `contract_to_unisphere` 后应为 \((0,0,0)\)，\( (2,2,2)\) 应为 \((1,1,1)\)。

**需要观察的现象**：步骤 1 应得出「法向让几何前向从 1 次密度计算变成 4 次（原点 1 次 + 偏移 3 次，原点密度被复用）」；步骤 3 的边界值验证缩放方向正确。

**预期结果**：finite_difference 的密度前向次数 ≈ 4×（还不含 encoding 与 feature_network 的开销）；坐标映射两端点正确。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：四种法向来源（finite_difference / finite_difference_laplacian / pred / analytic）的额外计算开销排序？
**答案**：按额外前向/求导次数大致为 analytic（autograd 建图，最贵）> finite_difference_laplacian（6 次偏移密度前向）> finite_difference（3 次）> pred（0 次额外前向，但多一个法向网络且需要监督信号）。coarse-nerf 取中庸的 finite_difference。

**练习 2**：为什么 `forward` 里要先保存 `points_unscaled` 再覆盖 `points`？
**答案**：两套坐标各有用途——归一化坐标喂 tcnn 编码（[0,1] 约定），世界坐标用于 blob 偏置（尺度与 radius 解耦，见练习 4.1-1）和法向差分（eps 的语义是真实空间距离）。一条变量名区分两个语义空间。

**练习 3**：渲染器传给 material 的是 `**geo_out`，若把 `n_feature_dims` 改成 6 会发生什么？
**答案**：`feature_network` 输出 6 维特征，而 no-material 校验 `features.shape[-1] == n_output_dims(3)` 会 assert 失败（[threestudio/models/materials/no_material.py:44-48](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/materials/no_material.py#L44-L48)）——几何与材质的维度契约在运行时显式检查。

## 5. 综合实践

**任务**：渲染初始密度场切片，对比两种 blob 初始化与不同 `start_level`，把本讲四个模块串成一张图。

在仓库根目录创建脚本（示例代码，**需要装有 tinycudann 的 GPU 环境**，待本地验证）：

```python
# compare_density_init.py（示例代码）
import torch, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from omegaconf import OmegaConf
import threestudio  # 触发注册

VARIANTS = [
    dict(name="magic3d_softplus_L8", density_bias="blob_magic3d",
         density_activation="softplus", density_blob_scale=10., density_blob_std=0.5,
         start_level=8),
    dict(name="dreamfusion_exp_L8", density_bias="blob_dreamfusion",
         density_activation="exp", density_blob_scale=5., density_blob_std=0.2,
         start_level=8),
    dict(name="magic3d_softplus_L4", density_bias="blob_magic3d",
         density_activation="softplus", density_blob_scale=10., density_blob_std=0.5,
         start_level=4),
    dict(name="magic3d_softplus_L12", density_bias="blob_magic3d",
         density_activation="softplus", density_blob_scale=10., density_blob_std=0.5,
         start_level=12),
]

def build(v):
    cfg = OmegaConf.create({
        "radius": 2.0, "normal_type": "finite_difference",
        "density_bias": v["density_bias"], "density_activation": v["density_activation"],
        "density_blob_scale": v["density_blob_scale"], "density_blob_std": v["density_blob_std"],
        "pos_encoding_config": {
            "otype": "ProgressiveBandHashGrid",
            "n_levels": 16, "n_features_per_level": 2, "log2_hashmap_size": 19,
            "base_resolution": 16, "per_level_scale": 1.447269237440378,
            "start_level": v["start_level"], "start_step": 2000, "update_steps": 500,
        },
    })
    geo = threestudio.find("implicit-volume")(cfg)
    geo.update_step(0, 0)   # 模拟 on_train_batch_start：解锁前 start_level 层
    return geo

# y=0 切片，256×256 网格覆盖 [-2,2]^2
ax_ = torch.linspace(-2, 2, 256)
X, Z = torch.meshgrid(ax_, ax_, indexing="ij")
pts = torch.stack([X, torch.zeros_like(X), Z], dim=-1).reshape(-1, 3)

fig, axes = plt.subplots(2, 2, figsize=(10, 9))
for ax, v in zip(axes.flat, VARIANTS):
    geo = build(v)
    d = geo.forward_density(pts.to(geo.device)).reshape(256, 256)
    im = ax.imshow(d.detach().cpu(), extent=[-2, 2, -2, 2], origin="lower", cmap="magma")
    ax.set_title(v["name"]); plt.colorbar(im, ax=ax, shrink=0.8)
    del geo; torch.cuda.empty_cache()
plt.savefig("density_init_compare.png", dpi=150)
print("saved density_init_compare.png")
```

**观察点**：

1. `magic3d_softplus_L8`：中心亮、半径约 0.5 的软圆盘，边缘渐变，盘外一片黑（softplus 压掉了负 bias），盘内有细碎的哈希纹理。
2. `dreamfusion_exp_L8`：更小更亮的核，支撑范围明显更窄，边界更陡——对应 4.2 的径向曲线。
3. `magic3d_softplus_L4` vs `L12`：L4 切片几乎只有平滑的 blob 形状（网络输出近乎常数）；L12 切片上高频噪声显著增多——这就是 4.3 说的「层级决定初始噪声频率」，也是 `start_step=2000` 把前两千步冻结在低层的原因。
4. 每次循环重新 build：不同变体的哈希表会重新随机初始化，纹理细节不可复现，但**整体形状（blob 主导）稳定**——说明初始密度场的宏观结构由偏置决定，哈希只叠加高频扰动。

**预期结果**：四个子图呈现上述差异。无 GPU 环境时退化为源码阅读型实践：完成 4.2 与 4.3 的两个 CPU 脚本即可覆盖同一组对比结论（径向形状 + 层级分辨率），并手绘切片草图标注。待本地验证。

## 6. 本讲小结

- `implicit-volume` = 共享编码的双头结构：`density_network` 出形状（1 维）、`feature_network` 出外观（3 维），coarse 阶段特征经 no-material 的 sigmoid 直接成为 RGB。
- 初始密度场由 `density_bias` 主导：`blob_magic3d` 线性偏置（s=10, σ=0.5）+ softplus 形成「中心浓、边缘渐稀」的锥形雾；`blob_dreamfusion` 高斯 + exp 因支撑窄、量级难控被项目弃用。偏置作用于**世界坐标**，与 bbox/radius 解耦。
- `ProgressiveBandHashGrid` 用一个只增不减的乘性 mask 实现由粗到细：`level(t)=min(start_level+⌊max(t-start_step,0)/update_steps⌋, n_levels)`，coarse-nerf 从 ~\(200^3\) 网格起步、step 6000 放开到 ~\(4096^3\)，保证任意时刻密度场最高频率可控、差分法向平滑。
- `forward` 的 dict 输出是几何↔渲染器的契约：渲染器在 ray marching 采样点上一次取全 `density/features/normal`，`**geo_out` 整包交给 material；法向默认用单边有限差分（eps=0.01，负号指向密度下降方向），代价是密度前向约 4 倍。
- 坐标双轨制贯穿全文件：`points_unscaled`（世界坐标，给 blob 与法向差分）与 contract 后的 `(0,1)` 坐标（给 tcnn 编码），阅读时务必分清当前在哪个空间。

## 7. 下一步学习建议

几何产出 `density/features` 之后，如何变成一张图？下一讲 **u5-l2《NeRF 体渲染器：nerfacc ray marching 与 proposal network》** 精读 `threestudio/models/renderers/nerf_volume_renderer.py`，看 `geo_out` 如何进入 nerfacc 的渲染权重与 alpha 合成。阅读顺序建议：

1. 先读渲染器的 `forward`（重点 L281-292 的消费现场），带着本讲的输出契约去读；
2. 再回头看本讲 `forward_field`/`forward_level` 与 `isosurface_threshold=25`——它们在 coarse-neus 阶段的 `geometry_convert_from` 转换中被用来把密度场变成 NeuS 的初始化网格（u5-l3）；
3. 想深究渐进编码的思想源头，可读 Instant-NGP（哈希编码）与 Neuralangelo（progressive eps，源码注释中已给出 [arXiv:2306.03092](https://arxiv.org/abs/2306.03092)）。

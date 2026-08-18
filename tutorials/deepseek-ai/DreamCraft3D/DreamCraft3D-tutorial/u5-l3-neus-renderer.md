# u5-l3：SDF 几何与 NeuS 体渲染

## 1. 本讲目标

上一讲（u5-l2）我们读完了 coarse-nerf 阶段的体渲染器：它消费的是「密度」。本讲进入四阶段流水线的第二个配置 `dreamcraft3d-coarse-neus`，几何表示从**密度场（NeRF）切换为符号距离场（SDF）**，渲染器换成 `neus-volume-renderer`。读完本讲你应该能够：

1. 说出 SDF 与密度场在「表达表面」上的本质差异，以及为什么 NeuS 表面比 NeRF 表面更干净。
2. 读懂 `implicit-sdf` 的网络结构（哈希编码 + SDF 头 + 特征头）、`sdf_bias` 球偏置初始化，并知道 `initialize_shape` 在本仓库中是一段**没有任何调用点的代码**。
3. 推导 NeuS 的 `get_alpha` 与 VolSDF 的 `volsdf_density` 两种 SDF→不透明度转换的数学形式，理解 `LearnedVariance` 可学习锐度参数的作用。
4. 解释 coarse-nerf 检查点如何通过 `system.weights` 的**非严格加载**热启动 coarse-neus：哪些参数能迁走、哪些不能。

## 2. 前置知识

### 2.1 符号距离函数（SDF）

一个三维场的 SDF 定义为：

\[ f(\mathbf{x}) = \|\mathbf{x} - \mathbf{x}_{\text{最近表面点}}\| \cdot \begin{cases} -1 & \mathbf{x} \text{ 在物体内部} \\ +1 & \mathbf{x} \text{ 在物体外部} \end{cases} \]

- 表面就是零水平集 \( f(\mathbf{x})=0 \)；
- \(|f(\mathbf{x})|\) 直接告诉你离表面多远，单位是真实长度；
- 外表面法向就是梯度 \(\nabla f\)（归一化后）。

对比 NeRF 的密度场：密度只表达「这里有多不透明」，表面位置取决于密度阈值，模糊且依赖渲染过程；而 SDF 天生带度量，能产生几何上干净的表面。这正是流水线「先 NeRF 探路、再 NeuS 精修几何」的动机（承接 u1-l1 的两阶段分层思想）。

### 2.2 体渲染回顾（承接 u5-l2）

体渲染的核心是把每条射线上各采样区间的贡献累乘起来：先由「区间不透明度 \(\alpha_i\)」得到权重 \(w_i = \alpha_i \prod_{j<i}(1-\alpha_j)\)，再加权求和颜色。NeRF 里 \(\alpha_i = 1-\exp(-\sigma_i \delta_i)\)，\(\sigma\) 是网络直接输出的密度。**本讲的核心问题变成：网络输出的是 SDF，怎么把它变成 \(\alpha_i\)？** 这就是 NeuS 与 VolSDF 各自给出的答案。

### 2.3 两种转换的直觉

- **VolSDF**：把 SDF 经一个指数型（Laplace 型）函数转成密度，再走普通体渲染。密度在表面内侧饱和、外侧指数衰减。
- **NeuS**：不显式算密度，直接推导「射线在给定区间内撞上表面的条件概率」作为 \(\alpha_i\)，并引入 cos 项修正视线方向与法向夹角的影响，保证无偏。

DreamCraft3D 的 `neus-volume-renderer` 把两条路线都实现了，用 `use_volsdf` 开关切换，默认走 NeuS 分支。

### 2.4 需要的基础

- 前几讲的注册机制（u3-l1）：`implicit-sdf`、`neus-volume-renderer` 都是注册名，配置里 `geometry_type`/`renderer_type` 的值决定用哪个类。
- Updateable 生命周期（u3-l2）：`cos_anneal_ratio`、占用网格刷新都挂在 `update_step` 钩子上。
- `contract_to_unisphere` 坐标归一化（u5-l1 讲过 `points_unscaled` 与归一化坐标的双轨制）。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [threestudio/models/geometry/implicit_sdf.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/implicit_sdf.py) | `implicit-sdf` 几何：SDF 网络、球偏置、（未启用的）形状初始化、法向计算 |
| [threestudio/models/renderers/neus_volume_renderer.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/neus_volume_renderer.py) | `neus-volume-renderer`：`volsdf_density`、`LearnedVariance`、`get_alpha`、渲染主链路 |
| [configs/dreamcraft3d-coarse-neus.yaml](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-neus.yaml) | coarse-neus 阶段配置，本讲参数的来源 |
| [configs/dreamcraft3d-coarse-nerf.yaml](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml) | 上一阶段配置，做差异对照 |
| [threestudio/models/geometry/base.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/base.py) | `BaseImplicitGeometry`、`contract_to_unisphere`、等值面提取骨架 |
| [threestudio/models/renderers/base.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/base.py) | `VolumeRenderer` 基类：geometry/material/background 引用注入 |
| [threestudio/systems/base.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py) 与 [threestudio/utils/misc.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/misc.py) | `system.weights` 热启动的加载链路 |
| [threestudio/models/networks.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/networks.py) | 哈希编码封装（分析热启动键匹配时用） |

## 4. 核心概念与源码讲解

### 4.1 implicit-sdf：SDF 网络、球偏置与那段未被调用的 initialize_shape

#### 4.1.1 概念说明

`implicit-sdf` 是 coarse-neus 阶段的几何组件。它要解决的问题：**用一个可训练的网络表达一张符号距离场，使其零水平集逼近目标物体的表面**。

与上一阶段 `implicit-volume`（u5-l1）的结构对照极其工整——两者都是「共享编码 + 双头 MLP」：

| | implicit-volume（coarse-nerf） | implicit-sdf（coarse-neus） |
|---|---|---|
| 共享编码 | ProgressiveBandHashGrid | HashGrid |
| 形状头 | `density_network`（输出 1 维密度） | `sdf_network`（输出 1 维 SDF） |
| 外观头 | `feature_network`（输出 3 维特征） | `feature_network`（输出 3 维特征） |
| 初始化先验 | `density_bias: blob_magic3d`（雾团） | `sdf_bias: sphere`（半径 0.5 的球） |

也就是说，换了头和偏置，编码与特征网络原封不动——这个「故意保持同名同形」的设计正是后面 `system.weights` 热启动能生效的前提（见 4.4）。

#### 4.1.2 核心流程

`ImplicitSDF` 对外提供四个查询接口（都是「归一化坐标 → 编码 → MLP」的变体）：

```text
forward(points, output_normal)   # 完整查询：sdf + features + normal/sdf_grad
forward_sdf(points)              # 轻量查询：只要 sdf（渲染器采样/剪枝时用）
forward_field(points)            # 等值面提取用：sdf（+ 可选网格变形场）
forward_level(field, threshold)  # field - threshold（mt 提取的零水平集输入）
```

`forward` 的数据流：

1. `points_unscaled` 记住原始世界坐标（法向差分要用真实尺度）；
2. `contract_to_unisphere` 把点归一化到 \((0,1)\)（有界时就是线性缩放）；
3. 共享 `encoding` 编码 → `sdf_network` 出原始 SDF；
4. `get_shifted_sdf` 加上 `sdf_bias`（球偏置）得到最终 SDF；
5. `feature_network` 出 3 维特征（经 no-material 的 sigmoid 直接成 RGB，承接 u5-l5 的结论）；
6. 若 `output_normal=True`，按 `normal_type` 计算法向与 `sdf_grad`。

#### 4.1.3 源码精读

**（1）网络组装。** [implicit_sdf.py:61-87](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/implicit_sdf.py#L61-L87)：`configure` 里依次建 `self.encoding`、`self.sdf_network`（输出 1 维）、`self.feature_network`（输出 `n_feature_dims=3` 维），可选建 `normal_network`（`normal_type="pred"` 时）与 `deformation_network`（`isosurface_deformable_grid` 时，为下一阶段 DMTet 准备，本阶段不开）。注意属性名：**`sdf_network`**，不是 `density_network`——这决定了热启动时它接不到 NeRF 的密度头权重。

**（2）主查询与球偏置。** [implicit_sdf.py:247-264](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/implicit_sdf.py#L247-L264)：先 `contract_to_unisphere`（实现见 [geometry/base.py:20-32](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/base.py#L20-L32)，有界场景下就是把 \([-r,r]\) 线性缩放到 \([0,1]\)），编码后过 `sdf_network`，再由 `get_shifted_sdf` 加偏置。

偏置的实现是 [implicit_sdf.py:224-245](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/implicit_sdf.py#L224-L245)：

```python
elif self.cfg.sdf_bias == "sphere":
    radius = self.cfg.sdf_bias_params
    sdf_bias = (points**2).sum(dim=-1, keepdim=True).sqrt() - radius
```

这就是配置里的「平方求和开根号」参数化（[coarse-neus.yaml:47-48](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-neus.yaml#L47-L48) 设 `sdf_bias: sphere`、`sdf_bias_params: 0.5`）：

\[ \text{sdf}(\mathbf{p}) = \|\mathbf{p}\|_2 - 0.5 \]

对球来说这是**精确的**符号距离（`geometry.radius=2.0` 的包围盒内，初始表面是半径 0.5 的球）。代码同时支持 `ellipsoid` 偏置 \(\sqrt{\sum (p_i/s_i)^2}-1\)，但对椭球这只是「伪 SDF」——它不是真实欧氏距离，等值面形状仍近似椭球，注释里也写明了 *pseudo signed distance*。这个偏置**作用于世界坐标 `points_unscaled`**（与 u5-l1 中 blob 偏置作用于世界坐标一致），与 `radius` 解耦。

**（3）法向计算。** [implicit_sdf.py:272-312](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/implicit_sdf.py#L272-L312)：coarse-neus 配置 `normal_type: finite_difference`（[coarse-neus.yaml:45](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-neus.yaml#L45)），沿三个坐标轴各偏移 `eps=0.01` 重查 `forward_sdf`，差分得梯度后归一化：

\[ \mathbf{n} = \frac{\nabla f}{\|\nabla f\|} \]

注意方向：SDF 的梯度指向函数增大方向即**向外**，所以这里法向取**正梯度**——与 u5-l1 中 implicit-volume「法向指向密度下降方向（负梯度）」恰好相反，这正是密度场与 SDF 的语义差异在代码上的落点。此外 [implicit_sdf.py:317-327](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/implicit_sdf.py#L317-L327) 的 `analytic` 分支取的是 `sdf_grad = -autograd.grad(...)`（**负**梯度），与差分分支符号相反——本仓库两分支符号不一致，coarse-neus 只用差分分支，不受影响；读码时留意即可（见 4.1.5 练习 3）。差分出的 `sdf_grad` 会被写进输出（[implicit_sdf.py:330-332](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/implicit_sdf.py#L330-L332)），供 eikonal 正则使用（见 4.3.3 末尾）。

**（4）initialize_shape——一段没有调用者的代码。** [implicit_sdf.py:91-222](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/implicit_sdf.py#L91-L222) 实现了 threestudio 遗留的「形状初始化」：支持 `ellipsoid`（L106-118）、`sphere`（L119-126）、`mesh:<路径>`（L127-194，用 pysdf 把网格变成可查询 SDF，并做居中/对齐/缩放）三种目标，然后用 Adam 以 MSE 拟合 1000 步（[implicit_sdf.py:202-218](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/implicit_sdf.py#L202-L218)），最后广播参数保证多卡一致。

但要在本仓库用 `grep -rn "initialize_shape"` 核实（不含讲义目录），命中只有两处**定义**——`implicit_sdf.py:91` 与 `tetrahedra_sdf_grid.py:127`——**没有任何调用点**。也就是说：

- `shape_init` 默认 `None`，且 coarse-neus 配置也没设它（配置里根本没有 `shape_init` 键）；
- 即便设了，这个方法也不会被执行——它是从上游 threestudio 继承、在 DreamCraft3D 中失效的代码路径。

DreamCraft3D 实际的初始化策略只有两件事：`sdf_bias: sphere` 提供初始几何先验 + `system.weights` 从 coarse-nerf 热启动共享编码（4.4 详解）。这是「读论文式想象」与「读源码事实」经常分叉的地方，本讲选择如实告诉你分叉在哪。

**（5）等值面出口。** [implicit_sdf.py:358-361](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/implicit_sdf.py#L358-L361) 的 `forward_level` 就是 `field - threshold`；SDF 情况下 `isosurface_threshold` 取基类默认 `0.0`（[geometry/base.py:60-61](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/base.py#L60-L61)），即**提取 SDF 的零水平集**——对比 implicit-volume 要用 `isosurface_threshold: 25.0` 这种密度阈值（u5-l1），SDF 的 `0` 有明确几何含义。这个出口会在下一阶段 `geometry_convert_from` 提取网格时被调用（u5-l4 承接）。另外 [implicit_sdf.py:56-57](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/implicit_sdf.py#L56-L57) 注释明确：SDF 无需去外点（`isosurface_remove_outliers: False`）。

#### 4.1.4 代码实践

**实践目标**：用纯 numpy（无需 GPU/训练）复现球偏置的 SDF 剖面，直观理解「偏置先验 + 网络输出」如何合成初始 SDF；同时动手核实 `initialize_shape` 确实无调用。

**操作步骤**：

1. 写一个脚本（示例代码，非项目原有）：

   ```python
   import numpy as np

   radius = 0.5
   t = np.linspace(-2.0, 2.0, 401)          # 沿 x 轴的剖面，y=z=0
   sdf_bias = np.abs(t) - radius            # 球偏置 sqrt(x^2)-r（轴上即 |x|-r）
   net_out = np.zeros_like(t)               # 训练起点：MLP 输出近似 0
   sdf = net_out + sdf_bias                 # get_shifted_sdf 的语义

   inside = (np.abs(t) < radius).sum()
   print(f"初始表面位置（sdf=0）: x = ±{radius}")
   print(f"内部（sdf<0）采样点数: {inside}/{len(t)}")
   ```

2. 在仓库根目录运行 `grep -rn "initialize_shape" --include="*.py" .`，记录命中行。
3. 打开 [coarse-neus.yaml:42-60](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-neus.yaml#L42-L60)，确认其中没有 `shape_init` 键。

**需要观察的现象**：脚本输出显示初始 SDF 只在 \(|x|<0.5\) 为负（内部）、之外为正；grep 只返回两处 `def initialize_shape` 定义、无调用。

**预期结果**：训练开始时场景就是「一个半径 0.5 的实心球」叠加可学习的网络修正项——这与 coarse-nerf 开局的 Magic3D 雾团（u5-l1）形成对照：NeuS 阶段一上来就有明确的表面。

**待本地验证**：若你所在环境已装好 tinycudann 与 GPU，可进一步实例化 `ImplicitSDF` 并对随机点调用 `forward_sdf` 对拍数值；未装则以上 numpy 版已足够验证公式。

#### 4.1.5 小练习与答案

**练习 1**：`sdf_bias: sphere` 的半径 0.5 与 `geometry.radius: 2.0` 是什么关系？为什么初始球要比包围盒小得多？

**答案**：`radius: 2.0` 决定 `bbox`（[geometry/base.py:70-81](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/base.py#L70-L81)），即编码归一化与射线采样的作用域；`sdf_bias_params: 0.5` 只是初始表面大小。初始球小，留出增长空间让扩散先验把表面「吹」到正确形状，同时保证表面附近（梯度最丰富的区域）落在分辨率充足的编码范围内。

**练习 2**：为什么 `implicit-sdf` 把 `isosurface_remove_outliers` 默认设为 `False`，而 `implicit-volume` 需要 `True`？

**答案**：见 [implicit_sdf.py:56-57](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/implicit_sdf.py#L56-L57) 的注释——SDF 由带度量的场约束，不容易产生密度场那种漂浮的离群小团（floaters），无需按面数阈值删连通域。

**练习 3**：`finite_difference` 分支与 `analytic` 分支算出的法向符号一致吗？

**答案**：不一致。差分分支（[implicit_sdf.py:301-312](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/implicit_sdf.py#L301-L312)）得到 \(+\nabla f\) 方向；`analytic` 分支（L317-324）显式取负号得 \(-\nabla f\)。coarse-neus 配置用的是 `finite_difference`，因此实际生效的是外向 \(+\nabla f\)。这一不一致属于上游遗留，读码时值得留意。

### 4.2 NeuS 渲染器（一）：LearnedVariance 与两种 SDF→不透明度转换

#### 4.2.1 概念说明

渲染器拿到 SDF 后的第一件事是决定「锐度」：SDF 转密度/不透明度的曲线越陡，表面越锐利；越缓，越接近一团软雾。NeuS 的做法是把锐度当作**可学习参数**（1/标准差，`inv_std`），随训练自动从软到硬——这就是 `LearnedVariance`。本模块讲三个纯函数级组件：`LearnedVariance`、NeuS 的 `get_alpha`、VolSDF 的 `volsdf_density`。

#### 4.2.2 核心流程

```text
sdf（来自 geometry）
   │
   ├─ use_volsdf=False（默认，NeuS 路线）
   │     get_alpha(sdf, normal, dirs, dists)
   │       ├─ inv_std = LearnedVariance(sdf)          # 可学习锐度
   │       ├─ iter_cos：视线·法向 余弦（带 anneal）
   │       └─ α = (Φ(prev) − Φ(next)) / Φ(prev)，Φ=σ(··inv_std)
   │
   └─ use_volsdf=True（VolSDF 路线）
         volsdf_density(sdf, inv_std) → 密度 ρ
         α = δ · ρ（δ 为区间长度或 render_step_size）
```

#### 4.2.3 源码精读

**（1）LearnedVariance：一个参数的模块。** [neus_volume_renderer.py:26-37](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/neus_volume_renderer.py#L26-L37)：

```python
class LearnedVariance(nn.Module):
    def __init__(self, init_val):
        self.register_parameter("_inv_std", nn.Parameter(torch.tensor(init_val)))

    @property
    def inv_std(self):
        val = torch.exp(self._inv_std * 10.0)
        return val

    def forward(self, x):
        return torch.ones_like(x) * self.inv_std.clamp(1.0e-6, 1.0e6)
```

要点：

- 用重参数化 \(\text{inv\_std} = e^{10\theta}\) 保证严格为正；`×10` 放大参数的等效梯度尺度，让小学习率也能显著改变锐度；
- `inv_std` 与输入无关——`forward(x)` 只是把标量广播成同形张量；
- 初始值 `learned_variance_init: 0.3`（[neus_volume_renderer.py:47](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/neus_volume_renderer.py#L47)），即初始 \(\text{inv\_std} = e^{0.3\times 10} = e^3 \approx 20.1\)；
- 它挂在 renderer 名下（[neus_volume_renderer.py:73](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/neus_volume_renderer.py#L73)），所以配置里 `optimizer.params` 的 `renderer: lr 0.001`（[coarse-neus.yaml:141-143](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-neus.yaml#L141-L143)）会把它连同渲染器其他参数一起优化——**锐度是训练出来的**。

**（2）NeuS 的 get_alpha。** [neus_volume_renderer.py:93-117](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/neus_volume_renderer.py#L93-L117)。设逻辑斯蒂 CDF \(\Phi(s) = \sigma(s \cdot \text{inv\_std})\)，它是 NeuS 中「SDF 的光滑 CDF 密度」近似。对每个采样区间（长度 \(\delta\)，中点 SDF 为 \(s\)）：

1. 计算 `true_cos = dirs · normal`（L98），再做 cos 退火（L101-104，见 4.3.3）得 `iter_cos`（非正）；
2. 用一阶外推估计区间两端 SDF：`next = s + iter_cos·δ/2`，`prev = s − iter_cos·δ/2`（L107-108）；
3. \(p = \Phi(\text{prev}) - \Phi(\text{next})\)，\(c = \Phi(\text{prev})\)（L110-113）；
4. \(\alpha = \frac{p + 10^{-5}}{c + 10^{-5}}\)，截断到 \([0,1]\)（L116）。

直觉：\(c\) 是「射线到区间前端仍未撞面」的概率，\(p\) 是「在区间内撞面」的概率，二者之比正是条件概率形式的区间不透明度。当射线正对表面（`iter_cos` 越负）、区间越宽、`inv_std` 越大（越锐），\(\alpha\) 越接近 1。

**（3）VolSDF 的 volsdf_density。** [neus_volume_renderer.py:19-23](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/neus_volume_renderer.py#L19-L23)：

```python
def volsdf_density(sdf, inv_std):
    inv_std = inv_std.clamp(0.0, 80.0)
    beta = 1 / inv_std
    alpha = inv_std
    return alpha * (0.5 + 0.5 * sdf.sign() * torch.expm1(-sdf.abs() / beta))
```

写成闭式（\(\beta = 1/\text{inv\_std}\)）：

\[
\rho(s) = \text{inv\_std}\cdot\left(0.5 + 0.5\,\mathrm{sign}(s)\left(e^{-|s|\cdot\text{inv\_std}} - 1\right)\right)
= \begin{cases} \text{inv\_std}\left(1 - 0.5\,e^{s\cdot\text{inv\_std}}\right) & s < 0 \\[4pt] 0.5\,\text{inv\_std}\,e^{-s\cdot\text{inv\_std}} & s \ge 0 \end{cases}
\]

性质：表面处 \(\rho(0)=0.5\,\text{inv\_std}\)；内部随 \(s\to-\infty\) 饱和于 `inv_std`；外部指数衰减到 0——一条以表面为拐点、由 `inv_std` 控制陡度的 Laplace 型曲线。使用时按 \(\alpha = \delta\cdot\rho\)（`delta` 取实际区间长或 `render_step_size`，见 L96、L156）转成不透明度。`inv_std.clamp(0, 80)` 防止锐度过大数值爆炸。

**（4）两条路线的选择。** 开关 `use_volsdf` 默认 `False`（[neus_volume_renderer.py:49](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/neus_volume_renderer.py#L49)），且 [coarse-neus.yaml:68-73](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-neus.yaml#L68-L73) 未覆盖它，所以 **DreamCraft3D 的 coarse-neus 走 NeuS 分支**。另注意 importance 采样器只支持 VolSDF（[neus_volume_renderer.py:218-221](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/neus_volume_renderer.py#L218-L221)，否则 `raise ValueError`），而默认 `estimator: occgrid`（L54-55），配置同样未改——这两个默认值共同锁定了实际执行路径。

#### 4.2.4 代码实践（本讲核心实践之一）

**实践目标**：用 numpy 重实现 `volsdf_density` 与 NeuS 的 \(\alpha\)，画出 SDF→不透明度/密度转换曲线，观察 `inv_std` 控制的锐度变化。

**操作步骤**（示例代码，非项目原有，只需 numpy + matplotlib）：

```python
import numpy as np
import matplotlib.pyplot as plt

# --- 逐行对照 neus_volume_renderer.py:19-23 ---
def volsdf_density(sdf, inv_std):
    inv_std = np.clip(inv_std, 0.0, 80.0)
    beta = 1.0 / inv_std
    alpha = inv_std
    return alpha * (0.5 + 0.5 * np.sign(sdf) * np.expm1(-np.abs(sdf) / beta))

# --- 逐行对照 get_alpha 的 volsdf=False 分支（neus_volume_renderer.py:106-116） ---
def neus_alpha(sdf, inv_std, delta, iter_cos=-1.0):
    sigmoid = lambda x: 1.0 / (1.0 + np.exp(-x))
    next_sdf = sdf + iter_cos * delta * 0.5
    prev_sdf = sdf - iter_cos * delta * 0.5
    prev_cdf = sigmoid(prev_sdf * inv_std)
    next_cdf = sigmoid(next_sdf * inv_std)
    return np.clip((prev_cdf - next_cdf + 1e-5) / (prev_cdf + 1e-5), 0.0, 1.0)

sdf = np.linspace(-0.2, 0.2, 1000)
delta = 1.732 * 2 * 2.0 / 512   # render_step_size，见 4.3.3
init_inv_std = np.exp(0.3 * 10) # LearnedVariance 初值 ≈ 20.1

fig, axes = plt.subplots(1, 2, figsize=(11, 4))
for v in [5, init_inv_std, 80]:
    axes[0].plot(sdf, volsdf_density(sdf, v), label=f"inv_std={v:.1f}")
    axes[1].plot(sdf, neus_alpha(sdf, v, delta), label=f"inv_std={v:.1f}")
for ax, title in zip(axes, ["VolSDF density", "NeuS alpha (delta=0.0135)"]):
    ax.axvline(0, color="gray", ls="--"); ax.set_xlabel("sdf"); ax.legend(); ax.set_title(title)
plt.savefig("sdf_to_density.png", dpi=120)
print("LearnedVariance 初始 inv_std =", init_inv_std)
```

**需要观察的现象**：

- 左图：`inv_std` 越大，密度曲线在 \(s=0\) 附近越陡（内侧更快饱和到 `inv_std`、外侧更快衰减到 0）；`inv_std=80` 时曲线几乎是一个阶跃。
- 右图：NeuS 的 \(\alpha\) 从外侧的 ~0 跃升到表面内侧的 ~1，跃迁宽度随 `inv_std` 增大而收窄。

**预期结果**：两条曲线都说明「`inv_std` = 表面锐度旋钮」；训练初始值 ≈20.1 对应一个适度偏软的表面，随 `LearnedVariance` 被优化（lr 0.001）逐步变锐。把 `delta` 改大（如 ×4）再画一次，可看到 NeuS \(\alpha\) 的跃迁整体抬升——区间越宽，单个区间截住表面的概率越高。

**待本地验证**：曲线的具体数值需本地运行确认；无 GPU 要求，任何装了 numpy/matplotlib 的环境均可。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `LearnedVariance` 要用 \(e^{10\theta}\) 而不是直接学一个正数？

**答案**：保证严格为正（`inv_std` 必须为正才有「标准差」语义），且指数重参数化让 \(\theta\) 在 0 附近时可覆盖很大的动态范围；`forward` 里再 `clamp(1e-6, 1e6)` 双保险防数值溢出。

**练习 2**：VolSDF 路线里 \(\alpha = \delta\rho\)，若 `inv_std` 很大而 \(\delta\) 固定，会出现什么问题？

**答案**：表面处 \(\rho = 0.5\,\text{inv\_std}\) 很大，\(\alpha=\delta\rho\) 可能远超 1，等效于区间完全不透明——这正是代码里 `clamp(0, 80)` 限制 `inv_std` 上界、且体渲染公式本身把 \(\alpha\) 截断在 \([0,1]\) 的原因。过锐的密度转换还会让采样步长显得相对过大，产生带状伪影。

**练习 3**：`get_alpha` 中 `iter_cos` 起什么作用？去掉它（设为 0）会怎样？

**答案**：`iter_cos` 把「视线与法向的夹角」纳入两端 SDF 的外推：视线越正对表面，区间前端到后端的 SDF 变化越大，\(\alpha\) 相应提高，这是 NeuS 对倾斜视角的无偏性修正。若设为 0，则 `prev = next = sdf`，\(p=0\)，\(\alpha\approx 0\)——射线永远「看不见」表面，这正是退火期 `cos_anneal_ratio` 不能恒为 0 的原因。

### 4.3 NeuS 渲染器（二）：forward 渲染主链路与 update_step 调度

#### 4.3.1 概念说明

`NeuSVolumeRenderer.forward` 与 u5-l2 的 `NeRFVolumeRenderer` 共享同一套 nerfacc 骨架（紧凑采样 → `render_weight_from_alpha` → `accumulate_along_rays` → over 合成），差异全部集中在「\(\alpha\) 从哪来」：NeRF 版查 `get_activated_density`，NeuS 版查 `get_alpha`/`volsdf_density`。此外它多了一个 `LearnedVariance` 参数和 cos 退火状态，`update_step` 的职责也不同（不训 proposal 网络，改为刷新占用网格与退火系数）。

#### 4.3.2 核心流程

```text
forward(rays_o, rays_d, light_positions, bg_color?)
  1. 展平射线 (B,H,W,3)→(Nr,3)
  2. estimator="occgrid"（默认）：
     a. alpha_fn：用 render_step_size 估保守 α（no_grad）
     b. estimator.sampling(...)：占用网格剪枝 + 均匀步长采样
        （alpha_thre=0.01，alpha<阈值的格子被跳过）
  3. validate_empty_rays 兜底空射线
  4. 采样点 positions = o + d·t_mid
  5. geometry(positions, output_normal=True) → {sdf, features, normal, sdf_grad}
     material(...) → RGB；background(dirs) → 背景色
     （非训练态一律 chunk_batch 分块，防显存溢出）
  6. get_alpha(sdf, normal, t_dirs, t_intervals) → α
  7. nerfacc：α→weights→opacity/depth/comp_rgb_fg 累积
  8. comp_rgb = comp_rgb_fg + bg·(1−opacity)
  9. 输出字典（训练态附 weights/t_points/…/geo_out，评估态附 comp_normal，恒附 inv_std）
```

#### 4.3.3 源码精读

**（1）configure：可学习方差 + 采样器。** [neus_volume_renderer.py:66-91](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/neus_volume_renderer.py#L66-L91)。基类 `configure`（[renderers/base.py:22-48](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/base.py#L22-L48)）用 dataclass 持有 geometry/material/background 引用并按 `radius` 建 `bbox`（避免子模块重复挂载进模块树，u5-l2 讲过）。随后：

- L73 建 `self.variance = LearnedVariance(0.3)`；
- L74-80 建 `nerfacc.OccGridEstimator(roi_aabb=bbox, resolution=32, levels=1)`；
- L81-84 定步长：

\[ \text{render\_step\_size} = \frac{\sqrt{3}\,\cdot 2R}{N_{\text{samples}}} = \frac{1.732 \times 2 \times 2.0}{512} \approx 0.0135 \]

（`1.732≈√3` 是体对角线系数：保证斜穿包围盒的射线也有足够采样数。）

**（2）采样与剪枝。** [neus_volume_renderer.py:137-194](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/neus_volume_renderer.py#L137-L194)。`alpha_fn`（L139-166）在 `no_grad` 下用 **`render_step_size` 代替真实区间长**估一个保守 α（L156 直接 `render_step_size * volsdf_density(...)`，L158-164 是 NeuS 公式的定步长版），仅供占用网格剪枝使用——所以它与最终合成用的 `get_alpha`（用真实 `t_intervals` 与法向，L286-288 调用）是「廉 价预筛 vs 精确合成」的关系。`estimator.sampling`（L183-194）传 `alpha_fn` 与 `alpha_thre=0.01`：α 长期低于 1% 的格子被从采样中剔除。这与 u5-l2 NeRF 渲染器的占用网格思路同源，只是「占用概率」的来源从密度换成了 SDF 转换。

**（3）几何查询与合成。** [neus_volume_renderer.py:244-305](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/neus_volume_renderer.py#L244-L305)：`validate_empty_rays` 兜底后，`self.geometry(positions, output_normal=True)` 一次拿到 sdf/特征/法向/`sdf_grad`（即 4.1 的 `forward`）；训练态直接查询，评估态全部走 `chunk_batch`（`eval_chunk_size: 8192`，[coarse-neus.yaml:73](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-neus.yaml#L73)）。随后 `get_alpha` 出 α，`nerfacc.render_weight_from_alpha` 出权重，三个 `accumulate_along_rays` 分别累积不透明度、深度、前景色——与 u5-l2 完全同一套 scatter-add 原语。over 合成在 [L307-313](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/neus_volume_renderer.py#L307-L313)。

**（4）输出契约的差异。** [neus_volume_renderer.py:315-350](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/neus_volume_renderer.py#L315-L350)：图像级键（`comp_rgb/comp_rgb_fg/comp_rgb_bg/opacity/depth`）与 NeRF 版一致，但：

- 训练态附 `weights/t_points/t_intervals/t_dirs/ray_indices/points` 并**展开 `**geo_out`**——因此 `sdf`、`sdf_grad` 也进入输出字典，供 system 侧 eikonal 正则（[dreamcraft3d.py:276-285](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L276-L285)：\((\|\nabla f\|_2-1)^2\)，约束 SDF 梯度模长为 1 即真距离场；不过 [coarse-neus.yaml:127](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-neus.yaml#L127) 把 `lambda_eikonal` 设为 0.0，默认不启用）；
- **没有** NeRF 版的 `normal_perturb`（法向扰动）——对应配置里 `lambda_normal_smooth: 0.0`（[coarse-neus.yaml:121-122](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-neus.yaml#L121-L122)），NeuS 阶段不需要平滑密度的正则；
- 评估态才输出 `comp_normal`（L336-349，法向可视化乘 `opacity`）；
- L350 恒附 `out["inv_std"]`：本仓库的 dreamcraft3d-system 并未消费该键（已 grep 核实），它是给观察/日志用的「当前锐度」读数。

**（5）update_step：两个调度量。** [neus_volume_renderer.py:353-382](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/neus_volume_renderer.py#L353-L382)：

- **cos 退火**（L356-360）：\(\text{ratio} = \min(1, \text{step}/\text{cos\_anneal\_end\_steps})\)，配置把终点钉在 `trainer.max_steps`（[coarse-neus.yaml:72](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-neus.yaml#L72)，5000 步线性从 0 到 1）。训练早期 `iter_cos` 被压向平缓形式（`get_alpha` L101-104 的混合式），避免法向噪声导致梯度死区，注释原话是 *"makes the cos value not dead at the beginning"*；
- **占用网格刷新**（L361-382）：`occ_eval_fn` 用当前 SDF + 当前 `inv_std` 重估各格 α，`update_every_n_steps` 按 nerfacc 内部节流周期更新；`self.training and not on_load_weights` 的守卫（L379）保证加载权重恢复状态时不做设备相关的重估（承接 u3-l2 关于 `on_load_weights` 语义的结论）。

最后 [L384-390](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/neus_volume_renderer.py#L384-L390) 重写 `train()/eval()`：评估态强制关掉 `randomized`（分层抖动采样）。

#### 4.3.4 代码实践

**实践目标**：不运行训练，通过「形状标注 + 数值代入」把 forward 主链路读透，并验证 `render_step_size` 与退火时间表。

**操作步骤**：

1. 打开 [neus_volume_renderer.py:119-351](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/neus_volume_renderer.py#L119-L351)，在 `forward` 每个阶段旁用中文注释张量形状（示例）：`rays_o: (B,H,W,3) → rays_o_flatten: (Nr,3)`；`ray_indices: (Nt,)`；`positions: (Nt,3)`；`geo_out["sdf"]: (Nt,1)`；`alpha: (Nt,1) → weights: (Nt,1)`；`comp_rgb_fg: (Nr,3)`。
2. 用 Python 计算器（或纸笔）代入 coarse-neus 配置：`render_step_size = 1.732*2*2.0/512`；再算 `cos_anneal_ratio` 在 step=1250、2500、5000 的值。
3. 对照 [coarse-neus.yaml:112-128](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-neus.yaml#L112-L128) 的损失权重，逐项检查：哪些正则需要 renderer 输出里的键？（提示：`lambda_orient` 用 `weights+t_dirs`，`lambda_opaque` 用 `opacity`，`lambda_eikonal` 用 `sdf_grad` 但权重为 0。）

**需要观察的现象**：`render_step_size ≈ 0.0135`；退火比例在 1250/2500/5000 步分别为 0.25/0.5/1.0；粗阶段正则中真正吃到 NeuS 特有键（`sdf_grad`）的 eikonal 权重为 0。

**预期结果**：你能仅凭配置与代码说出「每一步渲染发生了什么、每项损失从哪个输出键来」，不需要真的跑训练。

**待本地验证**：形状标注属于静态推理，如需实测可在有 GPU 环境时对单条射线打日志验证（本实践不依赖）。

#### 4.3.5 小练习与答案

**练习 1**：`alpha_fn`（采样剪枝）与 `get_alpha`（最终合成）为何要用两套不同的 α 计算？

**答案**：`alpha_fn` 在 `no_grad` 下运行且发生在采样阶段，此时还不知道最终区间划分，只能以 `render_step_size` 为假想步长、不带法向信息地估一个保守 α，够用于格子剪枝即可；`get_alpha` 在采样完成后用真实 `t_intervals`、法向与退火 cos，是进入损失图的精确值。一个是预筛，一个是合成。

**练习 2**：为什么 coarse-neus 配置把 `cos_anneal_end_steps` 设为 `trainer.max_steps`（全程线性退火）而不是提前结束？

**答案**：见 [coarse-neus.yaml:72](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-neus.yaml#L72)。5000 步的粗修几何全程都在演化，法向早期噪声大，让 `iter_cos` 的完整形式晚一点接管有助于稳定收敛；若提前结束，后期法向修正将缺失。

**练习 3**：NeuS 渲染器的 `update_step` 与 NeRF 渲染器（u5-l2）相比少了什么、多了什么？

**答案**：少了 proposal 网络的间歇训练（NeuS 用 occgrid 均匀步长采样，不需要 proposal）；多了 `cos_anneal_ratio` 的推进与 `LearnedVariance` 隐含的锐度演化（后者由优化器驱动，不经 `update_step`）。

### 4.4 从 coarse-nerf 到 coarse-neus：system.weights 非严格热启动

#### 4.4.1 概念说明

README 的四阶段命令（[README.md:112-116](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/README.md#L112-L116)）显示：coarse-nerf → coarse-neus 用的是 `system.weights`（而 geometry/texture 阶段用 `geometry_convert_from`）。问题在于：两个阶段的几何表示不同（密度 vs SDF）、渲染器不同，权重怎么迁？答案藏在 `load_weights` 的 `strict=False`——**按参数键名做交集加载，迁得走的迁，迁不走的静默跳过**。这实际上是一种「编码热启动」策略：把 NeRF 阶段学到的空间结构（哈希编码）与外观头搬过来，SDF 头从零起步、靠球偏置先验兜底。

#### 4.4.2 核心流程

```text
system.weights=<coarse-nerf last.ckpt>
  → BaseSystem.__init__ 调 load_weights（systems/base.py:46-47）
  → load_module_weights 取完整 state_dict（misc.py:32-62）
  → self.load_state_dict(state_dict, strict=False)（systems/base.py:54）
       键名匹配的参数被加载；不匹配的被跳过（无报错）
  → do_update_step(on_load_weights=True) 恢复步数敏感状态（systems/base.py:56）
```

#### 4.4.3 源码精读

**（1）加载入口。** [systems/base.py:46-56](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L46-L56)：`load_weights` 调 [misc.py:32-62](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/misc.py#L32-L62) 的 `load_module_weights`（`ignore_modules=None` 时返回**整个** system 的 state_dict 及其 epoch/global_step），然后 `load_state_dict(state_dict, strict=False)`。注意与 `BaseModule` 的组件级 `weights`（格式 `path:module_name`，[utils/base.py:103-112](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/base.py#L103-L112)）区分：system 级加载不做模块名过滤。

**（2）键名交集分析（本模块的核心推理）。** 逐模块比对两个阶段的参数键：

| coarse-nerf 的键 | coarse-neus 的键 | 结果 |
|---|---|---|
| `geometry.encoding.encoding.encoding.params` | 同名 | ✅ 命中 |
| `geometry.density_network.*` | `geometry.sdf_network.*` | ❌ 名字不同，跳过 |
| `geometry.feature_network.*` | 同名同形 | ✅ 命中 |
| `geometry.encoding`（nerf 渲染器独有子模块） | — | ❌ |
| `renderer.variance._inv_std`（neus 独有） | — | 新参数，从 0.3 初始化 |
| `background.color` | 同名 | ✅ 命中 |

编码键能命中的原因值得展开：两侧的 `self.encoding` 都是 `get_encoding` 返回的 `CompositeEncoding`（[networks.py:194-211](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/networks.py#L194-L211)），其内层——coarse-nerf 是 `ProgressiveBandHashGrid`（[networks.py:129-167](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/networks.py#L129-L167)）、coarse-neus 是 `TCNNEncoding`（[networks.py:55-64](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/networks.py#L55-L64)）——**两者都把 tcnn 编码存在名为 `encoding` 的属性里**，所以参数键同为 `...encoding.encoding.encoding.params`，且形状一致（两边都是 `n_levels=16 × n_features_per_level=2` 的 HashGrid）。`ProgressiveBandHashGrid` 的 `mask` 是普通张量而非 buffer，不进 state_dict，也不构成障碍。`feature_network` 两侧都是默认 `VanillaMLP`（输入 32 维、64 神经元、1 隐层），键与形状完全对齐。

于是一句话概括 DreamCraft3D 的 warm start：**NeRF 阶段训练出的哈希编码（空间结构）与外观特征头被搬进 NeuS 阶段；SDF 头从零开始，由半径 0.5 的球偏置提供初始表面；`LearnedVariance` 从 0.3 起步重新学锐度。** 这也解释了 4.1.1 里「两侧结构故意同名同形」的设计意图。

另一个细节：coarse-nerf 用 `ProgressiveBandHashGrid`（粗到细解锁层级），coarse-neus 配置写的是普通 `HashGrid`（[coarse-neus.yaml:52](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-neus.yaml#L52)，后面跟的 `start_level/start_step/update_steps` 三个键对裸 HashGrid 不生效，属于遗留写法）——即 NeuS 阶段所有 16 个层级从第 0 步就全部可用，配合已热启动的编码参数，等价于「跳过课程阶段直接用成熟编码」。

#### 4.4.4 代码实践

**实践目标**：验证（或纸面推演）`system.weights` 热启动时哪些参数真的迁移了。

**操作步骤**：

1. **有检查点时（待本地验证）**：写脚本加载 ckpt 并统计键前缀（示例代码，非项目原有）：

   ```python
   import torch
   ckpt = torch.load("outputs/dreamcraft3d-coarse-nerf/<tag>@LAST/ckpts/last.ckpt", map_location="cpu")
   keys = ckpt["state_dict"].keys()
   prefixes = sorted({".".join(k.split(".")[:2]) for k in keys})
   print(prefixes)          # 预期含 geometry.encoding / geometry.density_network / geometry.feature_network / renderer.* / background.*
   print("epoch, global_step =", ckpt["epoch"], ckpt["global_step"])
   ```

   再对 `configs/dreamcraft3d-coarse-neus.yaml` 起一次短训练（`trainer.max_steps=10` + `system.weights=<ckpt>`），观察启动日志无「键不匹配」报错（因为 `strict=False` 静默跳过）。
2. **无检查点时（纸面推演）**：按 4.4.3 的表格，从 [implicit_volume.py:63-76](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/implicit_volume.py#L63-L76)（属性名 `encoding/density_network/feature_network`）与 [implicit_sdf.py:61-75](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/implicit_sdf.py#L61-L75)（`encoding/sdf_network/feature_network`）各自推出 state_dict 键集合，求交集。

**需要观察的现象**：ckpt 键里出现 `geometry.density_network.*` 而 coarse-neus 模型里没有同名键；`geometry.encoding.encoding.encoding.params` 两边都存在。

**预期结果**：交集 = 共享编码参数 + `feature_network` + 背景色；SDF 头、`LearnedVariance`、渲染器内部参数全部不迁移（新初始化）。

**待本地验证**：步骤 1 需要先跑完 coarse-nerf 得到 ckpt（GPU、约 20GB 显存，承接 u1-l2/u2-l3）；步骤 2 无环境要求。

#### 4.4.5 小练习与答案

**练习 1**：既然 `strict=False` 会静默跳过不匹配的键，它有什么风险？

**答案**：拼错模块名或重构改了属性名时训练照常运行，只是权重实际没加载——表现为「热启动无效但无报错」。排查办法正是本实践的键交集分析：对比 ckpt 键集与 `model.state_dict().keys()`。

**练习 2**：为什么 DreamCraft3D 不把 NeRF 的密度头权重映射给 SDF 头（比如取负对数）做「真·几何迁移」，而选择让 SDF 头从零学？

**答案**：密度与 SDF 语义不同（无界正值 vs 带符号距离），强行映射需要额外标定且噪声大；而共享哈希编码已经携带了主要空间结构（哪里有物体），配合球偏置先验与参考图/扩散监督，SDF 头可以快速收敛到与编码一致的水平。这也是论文「coarse-nerf → coarse-neus」两小步设计的工程落地。仓库中未实现此类映射，本答案是对设计取舍的解释而非源码事实。

**练习 3**：`geometry_convert_from`（后两阶段用）与 `system.weights`（本阶段用）在语义上有何不同？

**答案**：`system.weights` 是**参数级**热启动（按键名交集加载，表示可以不同）；`geometry_convert_from` 是**几何级**转换（读上一阶段 parsed.yaml 重建旧几何、经 `create_from` 抽取/采样出新表示的初始值，承接 u3-l3 与 u5-l4）。二者互斥且优先级最低于 `--resume`。

## 5. 综合实践

把本讲两条主线（配置差异 + SDF→不透明度数学）合成一份小报告：

1. **差异清单**：对照 [dreamcraft3d-coarse-nerf.yaml](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml) 与 [dreamcraft3d-coarse-neus.yaml](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-neus.yaml)，制作一张表，至少覆盖：`geometry_type` 及其偏置初始化（`blob_magic3d` vs `sphere`）、编码 `otype`（`ProgressiveBandHashGrid` vs `HashGrid`）、`renderer_type` 及其特有参数（`return_normal_perturb/comp_normal` vs `cos_anneal_end_steps/learned_variance_init`）、损失权重差异（如 `lambda_normal_smooth` 1.0→0.0、新增 `lambda_eikonal` 0.0、`lambda_orient/sparsity/opaque` 从调度四元组变为常数）、optimizer 的 `params` 分组（全局 lr vs 按模块点号分组）、分辨率策略（`[128,384]`+milestones vs 固定 256）、`progressive_until`（200 vs 0）、guidance 的 `min/max_step_percent`（调度四元组 vs 常数）。
2. **数学部分**：完成 4.2.4 的 numpy 曲线图，并把 `render_step_size` 与 `LearnedVariance` 初始值代入，标注在图上。
3. **一段分析**（每条 2-3 句）：(a) 为什么 NeuS 阶段固定 256 分辨率而 NeRF 阶段从 128 爬坡？(b) 为什么 `lambda_eikonal` 默认为 0？(c) warm start 迁移了什么、放弃了什么？

参考结论（供核对，非唯一答案）：(a) 编码已由 NeRF 阶段热启动，无需再用低分辨率稳定早期优化，且 NeuS 表面质量对分辨率更敏感；(b) 差分法向 + 球偏置 + 短训练（5000 步）下 eikonal 收益有限，作者选择关闭（配置事实：[coarse-neus.yaml:127](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-neus.yaml#L127)），读者可自行开启做消融；(c) 迁移共享编码与外观头，放弃密度头与渲染器内部——SDF 头与锐度从零学起。

## 6. 本讲小结

- **implicit-sdf = 共享哈希编码 + SDF 头 + 特征头**，与 implicit-volume 结构同形但换了语义头；初始几何先验来自 `sdf_bias: sphere`（\(\|\mathbf{p}\|-0.5\)）而非 density blob；`initialize_shape`（含 mesh/pysdf 拟合）在本仓库**无任何调用点**，是上游遗留的死代码路径。
- **两种 SDF→不透明度转换**：VolSDF 的 `volsdf_density` 给出 Laplace 型密度曲线（\(\alpha=\delta\rho\)）；NeuS 的 `get_alpha` 用逻辑斯蒂 CDF 推「区间内撞面的条件概率」并做 cos 退火。默认 `use_volsdf=False`，DreamCraft3D 走 NeuS 分支。
- **LearnedVariance** 用 \(e^{10\theta}\) 重参数化把表面锐度变成挂在 renderer 下的可学习参数（初始 \(\approx 20.1\)，lr 0.001），`out["inv_std"]` 是它的日志读数（system 不消费）。
- **渲染主链路**复用 nerfacc 骨架（occgrid 均匀步长采样 + 累积合成），差异集中在 α 来源；`update_step` 负责 cos 退火（全程线性到 `max_steps`）与占用网格重估。
- **warm start 是键名交集**：`system.weights` + `strict=False` 迁移共享编码与 `feature_network`，`density_network`↔`sdf_network` 名字不同被静默跳过，SDF 头靠球偏置从零学起。
- **输出契约差异**有明确下游对应：训练态多出 `sdf`/`sdf_grad`（供 eikonal，默认权重 0），少 `normal_perturb`（对应 `lambda_normal_smooth=0`）。

## 7. 下一步学习建议

下一讲 **u5-l4（DMTet：tetrahedra-sdf-grid 与 nvdiffrast 光栅化）**将接续本讲：coarse-neus 训出的 SDF 如何经 `isosurface`（本讲 4.1.3 第 5 点的零水平集出口）被 `TetrahedraSDFGrid.create_from` 采样成显式网格，渲染器如何从体渲染切换为可微光栅化。建议先自行阅读 [threestudio/models/geometry/tetrahedra_sdf_grid.py:127](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/tetrahedra_sdf_grid.py#L127) 起的 `initialize_shape`（它同样未被调用，但 `create_from` 就在其附近）与 [threestudio/models/isosurface.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/isosurface.py) 的 marching tetrahedra 实现；随后到单元六（u6-l1 起）看 dreamcraft3d-system 如何把本讲的渲染输出接进参考图损失与扩散引导。

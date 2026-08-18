# training_substep（二）：正则化与阶段专属损失

## 1. 本讲目标

上一讲（u6-l3）我们逐项读完了 `training_substep` 中 `guidance == "ref"` 分支的参考图监督损失——那是流水线中唯一的逐像素强监督。本讲继续沿着 `training_substep` 往下走，读完剩下的三块内容：

1. **正则公共区**：`normal_smooth` 与 `3d_normal_smooth`，不区分阶段、不区分子步的平滑约束。
2. **coarse 分支**：`orient` / `sparsity` / `opaque` / `eikonal` / `z_variance` 五种体积正则，专门治理密度场训练的三大视觉缺陷——**飞点（floaters）、雾状（semi-transparent haze）、空洞/破碎**。
3. **geometry 与 texture 分支**：geometry 阶段的 `normal_consistency` / `laplacian_smoothness` 网格正则，以及 texture 阶段基于 ControlNet 编辑 + LPIPS 的感知 `reg` 损失。

学完本讲，你应当能够：

- 把每一项正则与它治理的具体视觉缺陷一一对应；
- 解释为什么同一套正则代码里，coarse 用四元组动态调度、geometry 用缓降曲线、texture 里全部关闭；
- 独立完成「置零某项正则 → 对比渲染结果」的消融实验。

## 2. 前置知识

### 2.1 为什么需要正则化损失

在 NeRF 式体渲染中，几何由一个自由度极大的密度场描述。参考图监督只覆盖**一个视角**，扩散先验（SDS）给出的又是**带噪声的弱梯度**。两者都管不住密度场在「没人看的地方」乱长，于是会产生：

- **飞点**：悬浮在主体周围的小团杂质，从某些视角看是噪点；
- **雾状**：本该透明的区域残留半透明介质，剪影边缘发毛；
- **背面泄漏**：密度场的法向朝向混乱，相机看到「本应背对自己的面」，图像上表现为雾斑。

正则化损失（regularization loss）不提供任何「应该长什么样」的内容信息，只施加**结构性的先验约束**——「射线应该尽量透明」「不透明度应该接近 0 或 1」「SDF 梯度长度应该为 1」——用来收掉上述垃圾结构。

### 2.2 C() 四元组调度回顾（承接 u2-l2 / u6-l3）

代码中每一项损失都被 `self.C(self.cfg.loss.lambda_xxx) > 0` 门控。`self.C` 是 `BaseSystem` 上的包装（[threestudio/systems/base.py:L92-L93](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L92-L93)），内部调用 [threestudio/utils/misc.py:L65-L97](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/misc.py#L65-L97) 的 `C(value, epoch, global_step)`，时间源是 `true_global_step`（[threestudio/systems/base.py:L70-L74](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L70-L74)）。

权重可以是一个常数，也可以是四元组 `[start_step, start_value, end_value, end_step]`，语义是线性插值：

\[
\text{value}(t) = v_s + (v_e - v_s)\cdot \mathrm{clamp}\!\left(\frac{t - t_s}{t_e - t_s},\ 0,\ 1\right)
\]

本讲会反复遇到一个技巧：**把 \(t_e - t_s\) 压成 1 步，插值就退化成「阶跃」**。例如 coarse 阶段的 `lambda_orient: [2000, 1., 10., 2001]` 表示「前 2000 步权重恒为 1，第 2000→2001 步瞬间跳到 10，之后恒为 10」。

### 2.3 两种几何表示决定两种正则

- coarse 阶段几何是**隐式场**（密度场 / SDF 场），正则作用在**射线 / 采样点**上（`out["opacity"]`、`out["normal"]`、`out["sdf_grad"]`）。
- geometry 阶段几何是 **DMTet 显式网格**，正则作用在**顶点与边**上（`out["mesh"]`），实现不在 dreamcraft3d.py 而在 `threestudio/models/mesh.py`。

## 3. 本讲源码地图

| 文件 | 本讲涉及的内容 |
| --- | --- |
| `threestudio/systems/dreamcraft3d.py` | 全部正则的定义与门控（L226-L319），损失汇总加权（L321-L334） |
| `configs/dreamcraft3d-coarse-nerf.yaml` | coarse 阶段正则权重（L125-L139），渲染器输出联动（L85-L86） |
| `configs/dreamcraft3d-geometry.yaml` | geometry 阶段正则权重（L100-L112） |
| `configs/dreamcraft3d-texture.yaml` | texture 阶段正则权重（L123-L137），control_guidance 注释段（L100-L109） |
| `threestudio/models/mesh.py` | `normal_consistency`（L269-L274）与 `laplacian`（L303-L309）的真实实现 |
| `threestudio/models/guidance/controlnet_reg_guidance.py` | texture 阶段 reg 损失的编辑图来源（`__call__` L365-L418） |
| `threestudio/utils/perceptual/perceptual.py` | LPIPS 风格感知损失 `forward`（L67-L85） |
| `threestudio/utils/ops.py` | `dot`（L16-L17）、数值稳定的 `binary_cross_entropy`（L304-L308） |
| `threestudio/utils/misc.py` | `C()` 插值实现（L65-L97） |

## 4. 核心概念与源码讲解

先看全景。`training_substep` 的损失区分为三层：`ref`/`guidance` 专属分支（上一讲与 u7 系列）→ **正则公共区**（L226-L250，两种子步都会执行）→ **阶段专属分支**（L252-L319 的 `coarse` / `geometry` / `texture` elif 链，未知 stage 直接报错）。所有正则项通过 `set_loss` 登记进 `loss_terms`，最后统一加权求和。

### 4.1 正则公共区：normal_smooth 与 3d_normal_smooth

#### 4.1.1 概念说明

这两项平滑正则写在阶段分支**之前**，理论上对任意 stage 都生效（只要权重为正）。它们不约束几何「长什么样」，只约束表面「不要抖」：

- `normal_smooth` 是**二维屏幕空间**的正则：对渲染出的法向图 `comp_normal` 做相邻像素差分，惩罚法向图的高频噪声；
- `3d_normal_smooth` 是**三维空间**的正则：比较每个采样点的法向 `normal` 与「在同一点加微小扰动后重新求出的法向」`normal_perturb`，惩罚法向场对位置的剧烈变化（等价于对法向场的空间平滑约束）。

#### 4.1.2 核心流程

```text
渲染输出 out
├── comp_normal 存在？── 否 → 报 ValueError
├── normal_smooth：垂直/水平相邻像素差分的平方和均值
├── normal / normal_perturb 存在？── 否 → 报 ValueError
└── 3d_normal_smooth：|normal - normal_perturb| 的均值
```

#### 4.1.3 源码精读

normal_smooth —— [threestudio/systems/dreamcraft3d.py:L227-L237](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L227-L237)：

```python
if self.C(self.cfg.loss.lambda_normal_smooth) > 0:
    if "comp_normal" not in out:
        raise ValueError(...)
    normal = out["comp_normal"]
    set_loss(
        "normal_smooth",
        (normal[:, 1:, :, :] - normal[:, :-1, :, :]).square().mean()
        + (normal[:, :, 1:, :] - normal[:, :, :-1, :]).square().mean(),
    )
```

这段对法向图做行方向与列方向的差分，两项平方误差取均值后相加——就是图像处理里最经典的总变差（Total Variation）先验的离散形式：

\[
\mathcal{L}_{\text{smooth}} = \frac{1}{H(W-1)}\sum_{i,j}\|n_{i,j} - n_{i+1,j}\|^2 + \frac{1}{(H-1)W}\sum_{i,j}\|n_{i,j} - n_{i,j+1}\|^2
\]

`comp_normal` 是否出现在渲染输出里，由配置联动控制——[configs/dreamcraft3d-coarse-nerf.yaml:L86](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L86)：

```yaml
return_comp_normal: ${cmaxgt0:${system.loss.lambda_normal_smooth}}
```

`cmaxgt0` 取四元组可达上界 \(C_{\max}=\max(v_s, v_e)\)：权重调度曲线可能触顶为正，渲染器就必须返回 `comp_normal`；权重恒为 0 时渲染器自动省掉这次计算。**损失门控与渲染器输出开关用同一个 resolver，永远保持一致**——这是 threestudio 配计系统里非常漂亮的一处设计（u2-l2 讲过 resolver 机制）。

3d_normal_smooth —— [threestudio/systems/dreamcraft3d.py:L239-L250](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L239-L250)：

```python
if self.C(self.cfg.loss.lambda_3d_normal_smooth) > 0:
    ...
    normals = out["normal"]
    normals_perturb = out["normal_perturb"]
    set_loss("3d_normal_smooth", (normals - normals_perturb).abs().mean())
```

`normal_perturb` 由 nerf-volume-renderer 在采样点旁加小位移后重新查询几何得到（coarse 配置显式打开：[configs/dreamcraft3d-coarse-nerf.yaml:L85](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L85) 的 `return_normal_perturb: true`）。两项损失配合 coarse-nerf 的权重（[configs/dreamcraft3d-coarse-nerf.yaml:L134-L135](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L134-L135)）：`lambda_normal_smooth: 1.0` 全程生效，`lambda_3d_normal_smooth: [2000, 5., 1., 2001]` 则在 2000 步后从 5 **降**到 1——前期强压法向噪声帮几何先平滑成形，后期放松让表面细节（汉堡的褶皱）得以生长。

#### 4.1.4 代码实践

**实践目标**：验证「权重门控与渲染器输出开关联动」这一机制。

**操作步骤**（源码阅读型，无需 GPU）：

1. 打开 [configs/dreamcraft3d-coarse-nerf.yaml:L125-L139](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L125-L139)，确认 `lambda_normal_smooth: 1.0`。
2. 心算 `cmaxgt0` 的结果：常数 1.0 的上界是 1.0 > 0，故 `return_comp_normal` 解析为 `true`。
3. 假设在命令行覆盖 `system.loss.lambda_normal_smooth=0`，回答：`return_comp_normal` 会变成什么？`training_substep` 里 L227 的门控结果是什么？渲染图会有什么变化？
4. 在 `threestudio/models/renderers/nerf_volume_renderer.py` 中 grep `comp_normal`，确认 `return_comp_normal=false` 时该键确实不进入输出字典（此时若门控仍为正，L228-L231 的 `ValueError` 就是保险丝）。

**预期结果**：三处行为（渲染器是否计算、字典是否含键、损失是否累加）由同一个配置值驱动，不会出现「渲染器没算但损失要算」的崩溃。待本地验证第 3 步命令行的实际覆盖效果。

#### 4.1.5 小练习与答案

**练习 1**：`normal_smooth` 在屏幕空间做差分，`3d_normal_smooth` 在三维空间做比较。各有什么优劣？

**答案**：屏幕空间差分实现极简（一次张量切片相减），且天然作用在「相机实际看到的地方」，但它依赖渲染分辨率、且相机一动监督对象就变；三维空间比较（normal vs normal_perturb）约束的是几何本身的性质、与视角无关，但需要渲染器额外输出扰动法向，开销更大。DreamCraft3D 两者并用，前期靠 3D 版本稳定体积内部的法向场。

**练习 2**：`lambda_3d_normal_smooth: [2000, 5., 1., 2001]` 与 `lambda_orient: [2000, 1., 10., 2001]` 的方向一降一升，为什么不统一？

**答案**：降的是平滑约束——2000 步后几何主体已成形，过度平滑会抹掉细节；升的是清洁约束——2000 步后进入「收垃圾」阶段，把前 2000 步弱正则期积累的浮雾、半透明残渣强力清除。两者一个为「细节」松绑，一个为「干净」加压，方向相反是刻意的。

### 4.2 coarse 分支：五种体积正则

#### 4.2.1 概念说明

进入 [threestudio/systems/dreamcraft3d.py:L252](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L252) 的 `if self.cfg.stage == "coarse":` 分支，共五项正则，每项对应密度场的一种病：

| 正则 | 治理的缺陷 | 一句话原理 |
| --- | --- | --- |
| `orient` | 背面雾、翻转面片 | 惩罚法向与视线同向的高权重采样点 |
| `sparsity` | 浮雾、杂质 | 让整条射线尽量透明 |
| `opaque` | 半透明雾状 | 把不透明度推向 0 或 1 |
| `eikonal` | SDF 失真、表面破碎 | 约束 SDF 梯度长度为 1 |
| `z_variance` | 飞点、不实心 | 惩罚实心像素的深度方差 |

其中 `eikonal` 与 `z_variance` 在本仓库随附的三份 DreamCraft3D 配置中**均未实际启用**（详见 4.2.3 的说明），属于「代码就绪、配置休眠」的继承功能——这一点必须如实区分。

#### 4.2.2 核心流程

```text
stage == "coarse"
├── C(lambda_orient) > 0 ？→ 惩罚 dot(normal, t_dirs) > 0 的采样点（权重 detach）
├── guidance != "ref" 且 C(lambda_sparsity) > 0 ？→ 像素不透明度的 L0.5 伪范数
├── C(lambda_opaque) > 0 ？→ BCE(opacity, opacity) 熵最小化
├── "lambda_eikonal" in cfg.loss 且 C(...) > 0 ？→ (||sdf_grad|| - 1)²
└── "lambda_z_variance" in cfg.loss 且 C(...) > 0 ？→ 实心像素深度方差
```

#### 4.2.3 源码精读

**（1）orient —— 治「背面泄漏」**，[threestudio/systems/dreamcraft3d.py:L253-L265](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L253-L265)：

```python
if self.C(self.cfg.loss.lambda_orient) > 0:
    if "normal" not in out:
        raise ValueError(...)
    set_loss(
        "orient",
        (
            out["weights"].detach()
            * dot(out["normal"], out["t_dirs"]).clamp_min(0.0) ** 2
        ).sum()
        / (out["opacity"] > 0).sum(),
    )
```

`dot` 是逐采样点内积（[threestudio/utils/ops.py:L16-L17](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/ops.py#L16-L17)）。几何上，法向 \(n\) 与射线方向 \(d\) 的内积 \(n \cdot d > 0\) 意味着这个表面点**背对着相机**——相机只应看到朝向自己的面。`clamp_min(0)` 放过正常朝向的点，只惩罚反向者，且平方放大严重违规的贡献：

\[
\mathcal{L}_{\text{orient}} = \frac{1}{|\{o > 0\}|}\sum_{p} \bar{w}_p \cdot \big(\max(0,\ n_p \cdot d_p)\big)^2
\]

两个工程细节值得注意：`weights.detach()` 切断了密度权重通道的梯度，让这一项**只负责把法向掰正**、不去动密度分配（否则密度可能靠「降低问题点的权重」来逃避惩罚）；分母 \((opacity > 0).sum()\) 用不透明像素数做归一化，避免分辨率变化导致损失量级漂移。

**（2）sparsity —— 治「浮雾」**，[threestudio/systems/dreamcraft3d.py:L267-L268](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L267-L268)：

```python
if guidance != "ref" and self.C(self.cfg.loss.lambda_sparsity) > 0:
    set_loss("sparsity", (out["opacity"] ** 2 + 0.01).sqrt().mean())
```

对每个像素的不透明度 \(o\) 施加 \(\sqrt{o^2 + 0.01}\)。这是 L0.5 伪范数：比 L1 更「狠」地鼓励小值彻底归零（在 0 附近导数 \(\to \infty\)，梯度大），而物体主体所在的像素 \(o \approx 1\) 处导数约为 1、惩罚有限。注意门控里的 `guidance != "ref"`：参考视角已经有 GT mask 的逐像素监督管着，稀疏化只需在随机相机视角上施加；而且「让射线透明」的约束如果作用在参考视角，会和 mask 监督打架。

**（3）opaque —— 治「半透明雾」**，[threestudio/systems/dreamcraft3d.py:L270-L274](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L270-L274)：

```python
if self.C(self.cfg.loss.lambda_opaque) > 0:
    opacity_clamped = out["opacity"].clamp(1.0e-3, 1.0 - 1.0e-3)
    set_loss(
        "opaque", binary_cross_entropy(opacity_clamped, opacity_clamped)
    )
```

`binary_cross_entropy(input, target)` 是手写版交叉熵（[threestudio/utils/ops.py:L304-L308](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/ops.py#L304-L308)，注释说明是为混合精度训练做的数值稳定实现）。这里 target 就是 input 自身，于是每一项退化为二元熵：

\[
\mathcal{L}_{\text{opaque}} = \frac{1}{N}\sum_p \big(-o_p \log o_p - (1 - o_p)\log(1 - o_p)\big)
\]

熵在 \(o = 0.5\) 处取最大值 \(\log 2\)，在 \(o \to 0\) 或 \(o \to 1\) 处趋于 0。所以这是一个**熵最小化**损失：把每个像素的不透明度从「半透明」推向二值——要么整条射线是实打实的表面，要么完全透明，中间态（雾）被消灭。`clamp(1e-3, 1-1e-3)` 保证 \(\log\) 的自变量远离 0（coarse 阶段 `precision: 16-mixed`，fp16 下 `F.binary_cross_entropy` 会直接报错，这正是手写实现存在的原因）。

**（4）eikonal —— SDF 的物理约束**，[threestudio/systems/dreamcraft3d.py:L276-L285](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L276-L285)：

```python
if "lambda_eikonal" in self.cfg.loss and self.C(self.cfg.loss.lambda_eikonal) > 0:
    if "sdf_grad" not in out:
        raise ValueError(...)
    set_loss(
        "eikonal", (
            (torch.linalg.norm(out["sdf_grad"], ord=2, dim=-1) - 1.0) ** 2
        ).mean()
    )
```

符号距离函数的定义性质是梯度长度处处为 1（eikonal 方程 \(\|\nabla_x \text{sdf}(x)\| = 1\)）。这个软约束 \((\|\nabla \text{sdf}\| - 1)^2\) 保持 SDF 的有效性——u5-l3 讲过 NeuS 的密度转换直接消费 `sdf / inv_std`，SDF 数值失真会让密度转换失去几何含义、表面破碎。注意门控是**双重**的：先检查键 `"lambda_eikonal" in self.cfg.loss` 是否存在（`loss` 是普通 dict，不同配置含的键不同，这个检查保证不含该键的旧配置不触发 `KeyError`），再检查 `C(...) > 0`。在随附配置中的实际状态：coarse-nerf（密度场，无 `sdf_grad`）根本不写这个键；coarse-neus 显式写了 `lambda_eikonal: 0.0`（[configs/dreamcraft3d-coarse-neus.yaml:L127](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-neus.yaml#L127)），键存在但权重为零，因此**不参与计算**。

**（5）z_variance —— 治「不实心」**，[threestudio/systems/dreamcraft3d.py:L287-L291](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L287-L291)：

```python
if "lambda_z_variance" in self.cfg.loss and self.C(self.cfg.loss.lambda_z_variance) > 0:
    # z variance loss proposed in HiFA: http://arxiv.org/abs/2305.18766
    # helps reduce floaters and produce solid geometry
    loss_z_variance = out["z_variance"][out["opacity"] > 0.5].mean()
    set_loss("z_variance", loss_z_variance)
```

`z_variance` 是渲染器沿射线累积深度时顺带算出的每像素深度方差（u5-l2 提过它与 opacity、depth 由同一个 scatter-add 原语累积）。方差大意味着质量沿射线**摊开**而非集中于一个表面——正是飞点的形态特征。掩码 `opacity > 0.5` 只挑选实心像素，把已经确定的表面收得更紧。注释标明该方法出自 HiFA 论文。同样地，DreamCraft3D 随附配置中没有任何一个阶段启用它（coarse 两份配置不含此键，texture 配置写了 `lambda_z_variance: 0.0` 但 texture 不走 coarse 分支）——它属于上游继承的休眠功能，想做消融实验的读者可以自行打开。

三项「清洁类」正则在 coarse-nerf 配置中的调度（[configs/dreamcraft3d-coarse-nerf.yaml:L136-L138](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L136-L138)）完全同构：

```yaml
lambda_orient: [2000, 1., 10., 2001]
lambda_sparsity: [2000, 0.1, 10., 2001]
lambda_opaque: [2000, 0.1, 10., 2001]
```

前 2000 步近乎裸奔（让 DeepFloyd + Zero123 先验自由塑形），第 2000 步瞬间增压一个数量级，在余下 3000 步里持续清洁。对比 coarse-neus 配置（[configs/dreamcraft3d-coarse-neus.yaml:L123-L125](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-neus.yaml#L123-L125)）用的是常数 `orient: 10.0 / sparsity: 0.1 / opaque: 0.1`——SDF 表示本身对密度乱长更有抵抗力，不需要渐进式加压。

#### 4.2.4 代码实践

**实践目标**：在 TensorBoard / CSV 日志中「看见」四元组调度的实际曲线。

**操作步骤**（需要一次短训练，显存充足时执行；无 GPU 则改做源码推演）：

1. 以较低步数启动 coarse-nerf 训练（约 2200 步即可覆盖跳变点）：

   ```bash
   python launch.py --config configs/dreamcraft3d-coarse-nerf.yaml --train --gpu 0 \
     tag=schedule-watch trainer.max_steps=2200 trainer.val_check_interval=200
   ```

2. 训练结束后打开 TensorBoard：

   ```bash
   tensorboard --logdir outputs/dreamcraft3d-coarse-nerf/schedule-watch*/tb_logs
   ```

3. 找到 `train_params/` 分组——它来自 [threestudio/systems/dreamcraft3d.py:L331-L332](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L331-L332)，会把 `cfg.loss` 里**每一个** lambda 的当前 `C()` 值逐步记录下来。
4. 叠加显示 `train_params/lambda_orient`、`train_params/lambda_sparsity`、`train_params/lambda_opaque`、`train_params/lambda_3d_normal_smooth` 四条曲线。

**需要观察的现象**：前三条在第 2000 步附近从 1 / 0.1 / 0.1 阶跃到 10 / 10 / 10；第四条同期从 5 降到 1。

**预期结果**：四条曲线与 4.1.5 练习 2 中「清洁增压、平滑松绑」的分析吻合。同时可以对照 `train/loss_guidance_orient_w`（加权后的损失值，来自 L321-L329 的汇总循环）确认跳变确实传导到了实际反传的损失上。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `weights.detach()` 去掉，orient 损失可能出现什么「投机」行为？

**答案**：梯度会同时流向法向和密度权重。网络可以通过**降低违规采样点的渲染权重**（让那些背对相机的点「变得不重要」）来减小损失，而不是真正修正法向——垃圾密度被藏进低权重区，视觉缺陷依旧存在。detach 强迫这一项只做「掰法向」这一件事。

**练习 2**：`sparsity` 用 \(\sqrt{o^2 + 0.01}\) 而不是 \(|o|\)（L1），加 0.01 与开根号各有什么用？

**答案**：开根号使小值区域的梯度 \(\frac{o}{\sqrt{o^2+0.01}}\) 在 \(o \to 0\) 附近仍保持可观（纯 L1 在 0 附近梯度恒为 ±1，L0.5 型的形状对「把 0.05 这类残留压到 0」更有效）；常数 0.01 避免在 \(o=0\) 处导数奇异（\(\sqrt{o^2}\) 在 0 点不可导），同时给立方根式的强惩罚设了一个软边界。总体效果：对接近透明的像素施加强推力，促使其彻底归零。

**练习 3**：`eikonal` 为什么在 coarse-nerf 配置里连键都不写，而不像 coarse-neus 那样写 `0.0`？

**答案**：coarse-nerf 的几何是 `implicit-volume`（密度场），渲染输出根本没有 `sdf_grad` 这个键。不写该键时，`"lambda_eikonal" in self.cfg.loss` 为假，整段被跳过；如果写了正权重，会一路走到 `if "sdf_grad" not in out: raise ValueError` 保险丝处直接报错。coarse-neus 写 `0.0` 则是显式声明「我知道这项存在但选择关闭」，键存在检查通过、`C()` 门控为 0，同样不计算——两种写法都安全，语义不同。

### 4.3 geometry 分支：normal_consistency 与 laplacian_smoothness

#### 4.3.1 概念说明

geometry 阶段几何已切换为 DMTet 显式网格（u5-l4），正则对象从「射线 / 采样点」变成「网格的顶点与边」。u6-l2 讲过该阶段 `ref_or_guidance: "accumulate"`、两个子步每步都跑，因此这里的正则每步被计算**两次**（分别拼进两个子步的损失里）。

- `normal_consistency`：相邻顶点的法向应当接近——锯齿、尖刺表面相邻顶点法向剧烈翻转，会被余弦相似度惩罚；
- `laplacian_smoothness`：每个顶点应处于邻居的「平均位置」附近——均匀 Laplacian 算子作用于顶点坐标，抑制高频抖动，等价于网格的低通滤波。

#### 4.3.2 核心流程

```text
stage == "geometry"
├── C(lambda_normal_consistency) > 0 ？→ out["mesh"].normal_consistency()
└── C(lambda_laplacian_smoothness) > 0 ？→ out["mesh"].laplacian()

mesh.normal_consistency（mesh.py）
├── v_nrm：顶点法向（相邻面法向按面积加权平均，u5-l4）
├── edges：无重复的无向边集合 (Ne, 2)
└── 每条边两端点法向的 (1 - cos) 取均值

mesh.laplacian（mesh.py）
├── no_grad 下构造稀疏均匀 Laplacian 矩阵 L（V×V）
└── ‖L · v_pos‖ 逐顶点取范数后求均值（可微，梯度流向顶点位置）
```

#### 4.3.3 源码精读

系统侧的分派非常薄——[threestudio/systems/dreamcraft3d.py:L293-L297](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L293-L297)：

```python
elif self.cfg.stage == "geometry":
    if self.C(self.cfg.loss.lambda_normal_consistency) > 0:
        set_loss("normal_consistency", out["mesh"].normal_consistency())
    if self.C(self.cfg.loss.lambda_laplacian_smoothness) > 0:
        set_loss("laplacian_smoothness", out["mesh"].laplacian())
```

真正的数学在 `Mesh` 类里。normal_consistency —— [threestudio/models/mesh.py:L269-L274](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/mesh.py#L269-L274)：

```python
def normal_consistency(self) -> Float[Tensor, ""]:
    edge_nrm: Float[Tensor, "Ne 2 3"] = self.v_nrm[self.edges]
    nc = (
        1.0 - torch.cosine_similarity(edge_nrm[:, 0], edge_nrm[:, 1], dim=-1)
    ).mean()
    return nc
```

\[
\mathcal{L}_{\text{nc}} = \frac{1}{|\mathcal{E}|}\sum_{(i,j)\in\mathcal{E}}\Big(1 - \cos\angle(n_i, n_j)\Big)
\]

注意约束的是**顶点法向**（`v_nrm`，u5-l4 讲过的面积加权顶点法向）在每条边两端的一致性。光滑表面上相邻顶点法向几乎相同；锯齿表面上相邻顶点法向夹角很大。由于 `v_nrm` 由面法向聚合而来、面法向又由 `v_pos` 叉积而来，梯度最终穿过这条链路回到 DMTet 的可训练顶点参数（`sdf` 与 `deformation`）。

laplacian —— [threestudio/models/mesh.py:L303-L309](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/mesh.py#L303-L309)：

```python
def laplacian(self) -> Float[Tensor, ""]:
    with torch.no_grad():
        L = self._laplacian_uniform()
    loss = L.mm(self.v_pos)
    loss = loss.norm(dim=1)
    loss = loss.mean()
    return loss
```

均匀 Laplacian 矩阵在 `_laplacian_uniform`（[threestudio/models/mesh.py:L276-L301](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/mesh.py#L276-L301)，注释注明源自 stable-dreamfusion）中构造：由面片邻接关系建立顶点邻接表，非对角放 -1、对角逐行累加为顶点度数，得到 \(L = D - A\)。于是

\[
(L \cdot v)_i = d_i\, v_i - \sum_{j \in \mathcal{N}(i)} v_j = d_i \Big(v_i - \frac{1}{d_i}\sum_{j} v_j\Big)
\]

即「顶点偏离其邻居均值的程度」。损失 \(\frac{1}{V}\sum_i \|(L v)_i\|\) 越小，网格越光滑。构造矩阵在 `no_grad` 里——稀疏矩阵的**结构**（索引）不需要梯度，需要的只是它与 `v_pos` 的乘法留在计算图内，梯度经由 `L.mm(v_pos)` 流向顶点。

配置取值（[configs/dreamcraft3d-geometry.yaml:L111-L112](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-geometry.yaml#L111-L112)）：

```yaml
lambda_normal_consistency: [1000,10.0,1,2000]
lambda_laplacian_smoothness: 0.0
```

与 coarse 的「增压阶跃」相反，normal_consistency 是**缓降**曲线：前 1000 步权重 10 强平滑（NeuS 转换过来的初始网格往往有肉眼可见的毛刺），1000→2000 步线性降到 1，之后保持轻约束、给法向雕刻（u6-l2 讲的 75% normal 渲染步）让出表达空间。laplacian_smoothness 默认关闭（0.0）——法向一致性已经足够，Laplacian 平滑对硬表面（比如汉堡的直角边缘）有过度圆化的风险。

#### 4.3.4 代码实践

**实践目标**：用最小网格手推两个正则，确认对它们的理解不依赖黑盒。

**操作步骤**（纯 numpy/torch 推演，无需 GPU）：

1. 构造一个 4 顶点、2 个三角形的平面正方形网格（顶点 `v = [[0,0,0],[1,0,0],[0,1,0],[1,1,0]]`，面 `f = [[0,1,2],[1,3,2]]`）。
2. 参考 [threestudio/models/mesh.py:L276-L301](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/mesh.py#L276-L301) 的构造逻辑，手写这个网格的均匀 Laplacian 矩阵（提示：4 个顶点各有 2~3 个邻居）。
3. 计算 \(L \cdot v\) 与其范数均值——平面网格应为多少？
4. 把其中一个顶点 z 坐标改成 1（制造一个尖刺），重算损失。
5. 再计算所有顶点法向（注意面积加权），验证 normal_consistency 在平面网格上接近 0、在尖刺网格上显著增大。

**预期结果**：平面网格的 Laplacian 损失为 0（所有顶点都在邻居均值位置上）、顶点法向完全一致（nc ≈ 0）；制造尖刺后两项损失都变为正。待本地验证（步骤 5 的面积加权法向建议直接对照 u5-l4 读过的 `v_nrm` 实现来写）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_laplacian_uniform` 的稀疏矩阵构造包在 `torch.no_grad()` 里，而 `L.mm(self.v_pos)` 必须在图内？

**答案**：稀疏矩阵只编码「谁和谁相邻」这一离散拓扑信息，`coo_tensor` 的索引与值都不需要梯度；放进 `no_grad` 可以避免把矩阵构造留在计算图里白白增加开销。而损失的意义就在于把梯度送回顶点位置，`mm` 这一步必须可微，否则正则对 `v_pos`（进而对 DMTet 的 sdf/deformation 参数）毫无约束力。

**练习 2**：geometry 阶段这两个正则每步会被计算几次？为什么？

**答案**：两次。它们写在 `training_substep` 的阶段分支里、不带 `guidance != "ref"` 之类的子步门控，而 geometry 配置的 `ref_or_guidance: "accumulate"`（[configs/dreamcraft3d-geometry.yaml:L90](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-geometry.yaml#L90)）使每个训练步会先后执行 guidance 与 ref 两个子步，各自调用一次 `training_substep`。效果上相当于正则权重翻倍——与 coarse 阶段 `alternate`（每步二选一、正则只算一次）不同。

### 4.4 texture 分支：ControlNet 编辑 + LPIPS 的感知 reg 损失

#### 4.4.1 概念说明

texture 阶段冻结几何、专攻外观，正则面临的新问题是**纹理漂移**：BSD 引导（u7-l4/u7-l5 详述）用场景自身的渲染图自举训练 LoRA 再蒸馏回来，这个自我强化的回路可能让纹理逐步偏离参考图的真实外观——细节丢失、过饱和、风格走形。

`reg` 损失的对策很有想象力：每 5 个 guidance 步，把当前渲染图交给一个**外部的、冻结的** ControlNet img2img 管线「重画一遍」。编辑图相当于「参考外观流形对当前渲染的重投影」，再用 LPIPS 感知距离把渲染图往编辑图方向拉——一个不参与训练的 2D 先验在旁边持续「纠偏」。

#### 4.4.2 核心流程

```text
stage == "texture"，四重门控：C(lambda_reg)>0 且 guidance=="guidance" 且 step % 5 == 0
├── 渲染图 comp_rgb 双线性降采样到 512×512（ControlNet 工作分辨率）
├── no_grad：
│   ├── control_guidance(rgb=渲染图, cond_rgb=渲染图, prompt, mask)
│   │     └── 内部：VAE 编码 → 加噪到 t → 以渲染图为 ControlNet 条件去噪 → VAE 解码
│   ├── 得到 edit_images
│   └── 编辑图写盘 .threestudio_cache/control_debug.jpg（调试用）
└── loss_reg = (H/8)·(W/8) · LPIPS(edit_images, rgb)     # 梯度只经 rgb 回传
```

#### 4.4.3 源码精读

系统侧 —— [threestudio/systems/dreamcraft3d.py:L298-L317](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L298-L317)：

```python
elif self.cfg.stage == "texture":
    if self.C(self.cfg.loss.lambda_reg) > 0 and guidance == "guidance" and self.true_global_step % 5 == 0:
        rgb = out["comp_rgb"]
        rgb = F.interpolate(rgb.permute(0, 3, 1, 2), (512, 512), mode='bilinear').permute(0, 2, 3, 1)
        control_prompt_utils = self.control_prompt_processor()
        with torch.no_grad():
            control_dict = self.control_guidance(
                rgb=rgb,
                cond_rgb=rgb,
                prompt_utils=control_prompt_utils,
                mask=out["mask"] if "mask" in out else None,
            )
            edit_images = control_dict["edit_images"]
            temp = (edit_images.detach().cpu()[0].numpy() * 255).astype(np.uint8)
            cv2.imwrite(".threestudio_cache/control_debug.jpg", temp[:, :, ::-1])

        loss_reg = (rgb.shape[1] // 8) * (rgb.shape[2] // 8) * self.perceptual_loss(
            edit_images.permute(0, 3, 1, 2), rgb.permute(0, 3, 1, 2)).mean()
        set_loss("reg", loss_reg)
```

几个关键设计：

- **门控**：只在 `guidance` 子步（ref 子步已有逐像素 RGB 监督，无需再拉）；`% 5 == 0` 降频——img2img 要跑 20 步 diffusion 采样，太贵，5 步一次既省算力又够用。
- **编辑图在 `no_grad` 里生成**：ControlNet 管线是冻结的「裁判」，它的输出只是目标；可训练的是**渲染图**这一侧。LPIPS 调用移出 `no_grad` 块，梯度经第二个参数 `rgb` 回传到场景外观网络（`geometry.encoding` + `feature_network`）。
- **缩放系数 \((H/8)(W/8) = 64 \times 64 = 4096\)**：512² 图像对应 64² 的 latent 网格，乘上它把逐像素的 LPIPS 量级对齐到 BSD 引导在 latent 空间按位置求和的梯度量级，两类损失才能在同一量纲下平衡（这也解释了为何 `lambda_reg` 的合理取值可以很小）。
- `cv2.imwrite` 每次覆写同一张 `control_debug.jpg`，是作者留下的现场调试探针，训练中直接看这个文件就能判断编辑质量。

编辑图的诞生地在 controlnet_reg_guidance 的 `__call__` —— [threestudio/models/guidance/controlnet_reg_guidance.py:L365-L418](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/controlnet_reg_guidance.py#L365-L418)：渲染图先 VAE 编码为 latents（L381），`prepare_image_cond` 把**同一张渲染图**提取为 ControlNet 条件（法向图或 Canny 边缘，L383，实现在 L290-L323），再进 `edit_latents`（L229-L288）：按时间步 t 加噪，然后以渲染图为结构条件、以 prompt 为语义条件跑 20 步 DDIM/DPM-Solver 去噪，最后 `decode_latents` 回像素空间（L414-L415）。`use_sds=False` 时返回的就是 `{"edit_images": ..., "edit_latents": ...}`（L410-L418）。

LPIPS 本体 —— [threestudio/utils/perceptual/perceptual.py:L67-L85](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/perceptual/perceptual.py#L67-L85)：`forward` 把两张图送入预训练 VGG 特征网络，在 5 个层级上计算归一化特征差的平方，经学好的线性层加权、空间平均后求和——即标准的 LPIPS「感知距离」。`perceptual_loss` 在系统 `configure` 时硬编码创建（u6-l1 讲过，[threestudio/systems/dreamcraft3d.py:L55-L56](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L55-L56)）。

**默认状态务必注意**：texture 配置里 `lambda_reg: 0.0`（[configs/dreamcraft3d-texture.yaml:L137](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L137)），且整套 control 组件被注释掉（[configs/dreamcraft3d-texture.yaml:L100-L109](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L100-L109)）——`reg` 是**实验性功能，默认关闭**。另外它有个隐藏的联动陷阱：若把 `lambda_reg` 打开为正而不同时取消 `control_guidance_type` 的注释，`self.control_guidance` 属性不存在，会在 L305 直接抛 `AttributeError`。开关要成对打开。

#### 4.4.4 代码实践

**实践目标**：在不启动训练的前提下，单独驱动 controlnet-reg-guidance，理解「编辑图」的生成质量。

**操作步骤**（需下载 ControlNet 与 Realistic Vision 权重，约需 GPU；否则改做源码阅读）：

1. 阅读文件尾部的自测入口 [threestudio/models/guidance/controlnet_reg_guidance.py:L433-L454](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/controlnet_reg_guidance.py#L433-L454)：它加载 `configs/experimental/controlnet-normal.yaml`，用 `assets/face.jpg` 独立跑一次 `guidance(rgb_image, rgb_image, prompt_utils)` 并把编辑图写到 `.threestudio_cache/edit_image.jpg`。
2. 确认仓库中 `configs/experimental/` 与 `assets/face.jpg` 是否存在（用 `ls` / `Glob` 检查），据此判断该入口在本仓库是否可直接运行。
3. 若资源齐备且有权重，执行 `python threestudio/models/guidance/controlnet_reg_guidance.py`，对比原图与 `edit_image.jpg`：ControlNet 条件（法向）应保住结构，纹理被重画为 prompt 描述的外观。
4. 源码阅读替代路径：沿 L383 `prepare_image_cond` → L290-L323 阅读 `control_type` 三种分支（`normal` / `canny` / `input_normal`），说明为什么「以渲染图自身为条件」能保结构换纹理。

**预期结果**：编辑图与输入图结构对齐、外观更贴近 prompt；`input_normal` 分支里那句 `cond_rgb[..., 0] = 1 - cond_rgb[..., 0]`（L316-L318）与 u6-l3 法向损失的 x 轴翻转是同一手性补丁。待本地验证步骤 3。

#### 4.4.5 小练习与答案

**练习 1**：为什么编辑图必须 `no_grad` 生成，而 LPIPS 计算不能放进 `no_grad`？

**答案**：编辑图来自冻结的扩散管线，它是优化的**目标**而非途径——对其求梯度既无意义（参数冻结）又昂贵（20 步采样的反向图）。而 LPIPS 的梯度需要沿「渲染图 → 场景外观参数」回传，这是 `reg` 损失唯一的作用通道；把 LPIPS 也 `no_grad` 掉，这项损失就变成常数，训练完全收不到纠偏信号。

**练习 2**：`true_global_step % 5 == 0` 与 `guidance == "guidance"` 两个条件叠加，实际生效频率是多少？

**答案**：texture 用 `alternate` 调度（`n_ref: 2`），guidance 子步约占一半训练步；其中再取 1/5，故 reg 损失大约每 10 个原始训练步计算一次。低频不影响效果（漂移是慢过程），却把昂贵的 img2img 采样开销摊薄了一个数量级。

**练习 3**：`reg` 与 ref 子步的 RGB L1 损失（u6-l3）都在约束渲染图贴近参考外观，为什么还需要前者？

**答案**：RGB L1 只在**参考视角**逐像素起作用，且 texture 阶段带 grow_mask 边缘腐蚀；侧面、背面视角的外观完全没有逐像素监督，全靠 BSD 先验——漂移正是从这些视角开始的。`reg` 作用在**随机相机视角**的渲染图上，且 LPIPS 度量的是深层特征而非像素，恰好补上「任意视角的结构化外观一致性」这块缺口。两者一个管参考视角、一个管随机视角，分工互补。

### 4.5 C() 调度与损失汇总：正则的「何时生效、权重多少」

#### 4.5.1 概念说明

前面四个模块反复出现四元组权重，现在把它们收拢成统一图景。所有损失项（含正则与引导损失）登记进 `loss_terms` 后，由同一段汇总代码统一加权。这套机制让**同一个损失函数在不同训练阶段呈现不同强度**，而无需改一行 Python——调度即配置。

#### 4.5.2 核心流程

```text
training_substep 末尾
├── 遍历 loss_terms
│   ├── log train/{name}（原始值）
│   ├── weight = C(cfg.loss["lambda_" + name 去前缀])
│   ├── log train/{name}_w（加权值）
│   └── loss += value * weight
├── 遍历 cfg.loss 所有键 → log train_params/{key} = C(value)   ← 调度曲线可视化
└── return {"loss": loss}
```

#### 4.5.3 源码精读

汇总循环 —— [threestudio/systems/dreamcraft3d.py:L321-L334](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L321-L334)：

```python
loss = 0.0
for name, value in loss_terms.items():
    self.log(f"train/{name}", value)
    if name.startswith(loss_prefix):
        loss_weighted = value * self.C(
            self.cfg.loss[name.replace(loss_prefix, "lambda_")]
        )
        self.log(f"train/{name}_w", loss_weighted)
        loss += loss_weighted

for name, value in self.cfg.loss.items():
    self.log(f"train_params/{name}", self.C(value))
```

两个细节：

- **命名约定即调度协议**：`set_loss("sparsity", ...)` 登记的名字会被拼上 `loss_{guidance}_` 前缀（L112、L116-L117），汇总时再逆运算回 `lambda_sparsity` 查权重。这解释了 4.2 里 guidance 子步注入的扩散损失为何叫 `loss_sd` → `lambda_sd`（u7 系列会用到）。
- **train_params 是免费的调度监视器**：无论某项损失当步有没有被计算，它的 `C()` 当前值都会被记录——4.2.4 的实践正是利用这一点。

再看阶段间调度策略的对比（把本讲所有权重放在一起）：

| 阶段 | 正则 | 权重 | 调度形态 |
| --- | --- | --- | --- |
| coarse-nerf | orient / sparsity / opaque | `[2000, 弱, 10, 2001]` | 阶跃增压（2000 步后清洁） |
| coarse-nerf | 3d_normal_smooth | `[2000, 5, 1, 2001]` | 阶跃降压（2000 步后放细节） |
| coarse-nerf | normal_smooth | `1.0` | 常数 |
| coarse-neus | orient / sparsity / opaque | `10.0 / 0.1 / 0.1` | 常数（SDF 自带约束力） |
| geometry | normal_consistency | `[1000, 10, 1, 2000]` | 线性缓降（先抹毛刺后让细节） |
| geometry | laplacian_smoothness | `0.0` | 关闭 |
| texture | 全部传统正则 + reg | `0.0` | 关闭（几何已冻结，无需清洁） |

一条主线清晰可见：**正则强度与几何表示的「自由度」成反比**——密度场自由度最大，需要最强的清洁正则；SDF 次之；DMTet 网格只剩顶点级自由度，两道轻正则够用；texture 冻结几何后传统正则全部归零，只剩针对外观漂移的 `reg`（还是可选的）。

#### 4.5.4 代码实践

**实践目标**：用 20 行脚本离线复现 C() 的调度行为，为调参建立直觉。

**操作步骤**（纯 CPU，随时可跑）：

1. 新建临时脚本 `c_schedule_demo.py`（示例代码，放在仓库任意临时位置即可，勿提交）：

   ```python
   # 示例代码：离线复现 misc.C 的四元组插值
   import sys
   sys.path.insert(0, ".")
   from threestudio.utils.misc import C

   for spec, label in [
       ([2000, 1., 10., 2001], "orient(nerf)"),
       ([2000, 5., 1., 2001], "3d_smooth(nerf)"),
       ([1000, 10.0, 1, 2000], "nc(geometry)"),
       (1.0, "normal_smooth"),
   ]:
       pts = [C(spec, 0, t) for t in range(0, 2501, 100)]
       print(f"{label:18s}", " ".join(f"{p:5.1f}" for p in pts))
   ```

2. 运行 `python c_schedule_demo.py`，对照输出的三段数值（跳变前 / 过渡 / 跳变后）。
3. 修改四元组做思想实验：把 `end_step` 从 2001 改成 4000，观察 orient 从「阶跃」变成「斜坡」。

**预期结果**：打印出的序列与 4.5.3 表格的描述一致——前 20 个采样点恒为 start_value，最后一个点跳到 end_value；`normal_smooth` 全程恒 1.0。待本地验证（依赖 `import threestudio` 成功，需先装好 u1-l2 的环境）。

#### 4.5.5 小练习与答案

**练习 1**：`[2000, 1., 10., 2001]` 与 `[0, 1., 10., 1]` 在 2000 步之后的行为相同吗？在 1000 步时呢？

**答案**：2000 步之后两者权重都是 10，行为相同；但 1000 步时前者为 1（还没到 start_step，返回 start_value）、后者为 10（插值早已完成）。前者是「先弱后强」的延迟启动，后者从头就是强正则——对 coarse 阶段而言后者会压制先验自由塑形，效果通常更差。

**练习 2**：为什么 `train_params/` 要对 `cfg.loss` 里**所有**键记录 `C()` 值，包括当步根本没计算的损失？

**答案**：这是把「调度配置」变成可观测数据的零成本手段。即使某项损失当步未参与计算（比如 sparsity 在 ref 子步被门控跳过），它的权重曲线仍在日志里，调参时能直接看到「此刻如果计算会是多大权重」，也能立刻发现诸如「四元组写错导致权重恒 0」这类配置事故。

## 5. 综合实践

本讲综合实践分两部分：先产出一张总表，再做一次真实消融。

### 5.1 第一部分：损失全景表

整理下表（可直接采用，也建议自己重填一遍以加深记忆）——「损失项 → 作用阶段 → 代码位置 → 默认权重 → 解决的问题」：

| 损失项 | 作用阶段 | 代码位置（dreamcraft3d.py） | 默认权重 | 解决的问题 |
| --- | --- | --- | --- | --- |
| normal_smooth | 公共区（仅 coarse 启用） | [L227-L237](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L227-L237) | 1.0 | 法向图高频噪声、表面起伏 |
| 3d_normal_smooth | 公共区（仅 coarse 启用） | [L239-L250](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L239-L250) | [2000, 5, 1, 2001] | 采样点法向场抖动 |
| orient | coarse | [L253-L265](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L253-L265) | [2000, 1, 10, 2001] | 背面雾、翻转面片 |
| sparsity | coarse（仅 guidance 子步） | [L267-L268](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L267-L268) | [2000, 0.1, 10, 2001] | 浮雾、透明区杂质 |
| opaque | coarse | [L270-L274](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L270-L274) | [2000, 0.1, 10, 2001] | 半透明雾状 |
| eikonal | coarse（需 sdf_grad） | [L276-L285](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L276-L285) | 0.0（neus）·键缺省（nerf） | SDF 失真、表面破碎 |
| z_variance | coarse（需 z_variance） | [L287-L291](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L287-L291) | 未启用（休眠） | 飞点、几何不实心 |
| normal_consistency | geometry | [L294-L295](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L294-L295) + [mesh.py:L269-L274](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/mesh.py#L269-L274) | [1000, 10, 1, 2000] | 网格锯齿、法向翻转 |
| laplacian_smoothness | geometry | [L296-L297](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L296-L297) + [mesh.py:L303-L309](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/mesh.py#L303-L309) | 0.0 | 网格高频抖动 |
| reg | texture（%5、guidance 子步） | [L298-L317](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L298-L317) | 0.0（实验性） | 纹理漂移 |

### 5.2 第二部分：coarse 正则消融实验

**实践目标**：验证 `lambda_orient` 与 `lambda_sparsity` 对粗阶段几何清洁度的贡献。

**操作步骤**（需要 GPU 与 u1-l2 环境；建议显存紧张时按 README Tips 追加 `data.height=128 data.width=128 data.random_camera.height=128 data.random_camera.width=128`）：

1. **对照组**：默认配置短训 2500 步（覆盖第 2000 步的正则增压点；3000 步前分辨率保持 128，显存友好）：

   ```bash
   python launch.py --config configs/dreamcraft3d-coarse-nerf.yaml --train --gpu 0 \
     tag=ablation-base trainer.max_steps=2500 trainer.val_check_interval=500
   ```

2. **消融组**：两项清洁正则置零（注意：置零同时会触发 4.1 讲过的联动——`requires_normal` 等 resolver 只绑定 normal_smooth，orient/sparsity 无渲染器联动，可安全置零）：

   ```bash
   python launch.py --config configs/dreamcraft3d-coarse-nerf.yaml --train --gpu 0 \
     tag=ablation-noreg trainer.max_steps=2500 trainer.val_check_interval=500 \
     system.loss.lambda_orient=0 system.loss.lambda_sparsity=0
   ```

3. 训练中观察 TensorBoard 的 `train/loss_guidance_orient_w` 与 `train/loss_guidance_sparsity_w`：对照组应在 2000 步后出现且量级跳升，消融组应始终不存在（门控为 0 则损失根本不登记，`_w` 曲线为空）。
4. 对比两组 `save/` 目录下 it2000 与 it2500 附近的验证渲染图与 opacity 图。

**需要观察的现象**：对照组在 2000 步后 opacity 图背景应逐渐干净、剪影边缘收拢；消融组预期背景残留更多半透明杂质与飞点、物体周围「雾感」更重。

**预期结果**：与 4.2 的机理分析一致——orient/sparsity 是 coarse 后期清洁的主力。若差异不明显，可延长到 3500 步再看（同时注意分辨率在 3000 步翻倍带来的显存上升）。待本地验证。

## 6. 本讲小结

- 正则化损失不提供内容信息，只施加结构先验：coarse 阶段治理密度场的**飞点、雾状、背面泄漏**（orient/sparsity/opaque，辅以休眠的 eikonal/z_variance），geometry 阶段治理**网格锯齿与抖动**（normal_consistency/laplacian），texture 阶段防御**纹理漂移**（可选的 ControlNet+LPIPS reg）。
- coarse 的三项清洁正则用 `[2000, 弱, 10, 2001]` 阶跃调度：前 2000 步让双扩散先验自由塑形，之后增压清洁；3d_normal_smooth 反向降压放细节；geometry 的 normal_consistency 则用 `[1000, 10, 1, 2000]` 缓降——**正则强度与几何表示的自由度成反比**。
- 每项损失由 `C(lambda_xxx) > 0` 门控，`loss_` 前缀命名与 `lambda_` 查表构成自动的加权协议；`train_params/` 日志免费可视化所有调度曲线。
- 「代码存在 ≠ 配置启用」：eikonal 在 coarse-neus 中权重为 0、z_variance 无配置启用、reg 连同整套 control 组件默认注释——阅读配置与阅读代码同等重要。
- orient 的 `weights.detach()`、sparsity 的 L0.5 伪范数、opaque 的熵最小化、reg 的 `no_grad` 编辑图 + 4096 缩放系数，每一处细节都是「梯度应该流向哪里」的精巧安排。

## 7. 下一步学习建议

本讲读完了 `training_substep` 的全部损失分支，`dreamcraft3d-system` 的骨架至此完整。接下来两条路：

1. **进入 u7-l1（Prompt Processor）**：本讲刻意略过的 `prompt_utils` 与 `self.guidance(...)` 调用，其内部机制从文本嵌入开始展开——视角相关提示、负向嵌入，是理解引导损失 `lambda_sd` 的前置。
2. **进入 u7-l2（DeepFloyd SDS）**：顺着 `guidance_out` 里那些 `loss_sd` 的来源，读透得分蒸馏梯度的推导与实现，把本讲的「加权汇总」上游补全。

若想先巩固本讲，建议用 5.2 的消融框架扩展到 `lambda_opaque` 与 `lambda_normal_smooth`，四个正则两两组合做小网格实验，体会它们之间的相互作用（例如强 sparsity 与强 opaque 是否冗余）。

# ControlNet 编辑正则与感知损失

## 1. 本讲目标

本讲是单元七（扩散引导家族）的最后一讲，解读 DreamCraft3D 中一个**默认关闭的实验性功能**：texture 阶段的 ControlNet 编辑正则。学完本讲你应该能够：

1. 说清楚 `controlnet-reg-guidance` 如何用 SDEdit 式 img2img 编辑把当前渲染图「重绘」成一张编辑图（`edit_images`），以及这张编辑图为什么能充当参考视角不可见区域的软监督目标。
2. 读懂 LPIPS 感知损失（`threestudio/utils/perceptual/perceptual.py`）的 VGG 特征 + 学习线性权重结构，以及它的权重文件从哪里自动下载。
3. 沿着 `dreamcraft3d.py` 的 texture 分支梳理出这段功能的**三重门控**（`lambda_reg > 0` 且 guidance 子步且每 5 步一次），理解它与 `alternate` 交替调度叠加后的真实执行频率。
4. 掌握「通过 yaml 注释开关 + 命令行覆盖启用实验性功能」的通用方法，并能在不修改源码的前提下完成开启/置零的对照实验。

承接前讲：u7-l5 讲完了 BSD 引导的动态训练逻辑；本讲的 ControlNet 正则是 BSD 之外一个**可选的、旁路式的**外观约束，两者在 `training_substep` 中所处的位置完全不同（BSD 在 guidance 子步的主体，本讲的功能在 `stage == "texture"` 的专属分支）。

## 2. 前置知识

### 2.1 逐像素损失 vs 感知损失

MSE / L1 这类逐像素损失把两张图当作像素网格上的向量逐点比较。它有两个问题：对轻微的纹理错位过度敏感（同一纹理平移一个像素就会得到很大的损失），又对「结构相似但风格跑偏」不够敏感。**感知损失（perceptual loss）**改为在预训练网络的深度特征空间里比较两张图「看起来像不像」。LPIPS（Learned Perceptual Image Patch Similarity）是其中最常用的一种：用 VGG 提取多层特征，在单位化后作差，再对每层学一组 1×1 卷积权重加权求和。它与人眼感知的一致性明显优于像素距离，因此被广泛用作图像回归任务的监督信号。

### 2.2 SDEdit 与 img2img 编辑

SDEdit 的思路是：拿一张已有图像 \( x_0 \)，先加噪到中间时刻 \( t \)，再用扩散模型逐步去噪：

\[ z_t = \sqrt{\bar{\alpha}_t}\, x_0 + \sqrt{1-\bar{\alpha}_t}\, \epsilon, \quad \epsilon \sim \mathcal{N}(0, I) \]

加噪越深（\( t \) 越大），去噪结果偏离原图越远——这就是 img2img 生成里 `strength` 参数的含义。DreamCraft3D 复用这个机制做**图像编辑**：把当前 3D 渲染图加噪再去噪，去噪过程受文本提示与结构条件图引导，得到的「编辑图」保留了渲染图的大致内容、但外观被 2D 先验修正。

### 2.3 ControlNet：给扩散模型加空间条件

ControlNet 在冻结的 Stable Diffusion UNet 旁边挂一个「条件编码器副本」，接收一张空间对齐的条件图（法向图、Canny 边缘图等），把各层残差注入 UNet 对应层，从而实现「按给定结构生成图像」。本仓库用到两个现成件：

- 法向条件：`lllyasviel/control_v11p_sd15_normalbae`（SD1.5 系）或 `thibaud/controlnet-sd21-normalbae-diffusers`（SD2.1 系）；
- 条件图预处理器：`controlnet_aux` 库的 `NormalBaeDetector` / `CannyDetector`。

### 2.4 硬监督与软监督

参考图 RGB 损失（u6-l3）是**硬监督**：只作用于参考视角可见像素、逐像素对齐真值。而在 texture 阶段，参考视角**看不见的背面区域**没有任何真值，只有 BSD 蒸馏的弱方向约束，纹理容易「漂移」（drift）——生成与参考图风格不一致的花纹。ControlNet 编辑正则提供**软监督**：让一个 2D 编辑模型对当前渲染「打样」，产出一张结构一致、外观合理的编辑图作为伪真值，再用 LPIPS 把渲染拉向它。软监督不要求逐像素一致，只要求感知相似。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [threestudio/models/guidance/controlnet_reg_guidance.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/controlnet_reg_guidance.py) | 本讲主角：`stable-diffusion-controlnet-reg-guidance`，装配 ControlNet 管线并实现 SDEdit 编辑循环，输出 `edit_images` |
| [threestudio/models/guidance/controlnet_guidance.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/controlnet_guidance.py) | 姊妹变体 `stable-diffusion-controlnet-guidance`：delta-update 式回归，自带 L1+LPIPS 损失，未被 dreamcraft3d 系统接线 |
| [threestudio/utils/perceptual/perceptual.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/perceptual/perceptual.py) | LPIPS 感知损失实现与注册对象 `perceptual-loss` |
| [threestudio/utils/perceptual/utils.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/perceptual/utils.py) | LPIPS 线性层权重 `vgg.pth` 的自动下载与 md5 校验 |
| [threestudio/systems/dreamcraft3d.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py) | 系统侧：可选组件装配、texture 分支的三重门控、`loss_reg` 计算与调试图落盘 |
| [configs/dreamcraft3d-texture.yaml](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml) | texture 阶段配置：被注释掉的 `control_guidance` 段与 `lambda_reg: 0.0` 是本讲的两个开关 |
| [threestudio/models/renderers/nvdiff_rasterizer.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nvdiff_rasterizer.py) | `render_mask=True` 时输出参考视角不可见区域 mask，即编辑循环里的 `mask` 入参 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**LPIPS 感知损失**、**ControlNet 编辑引擎**、**系统接线与 yaml 开关**。

### 4.1 LPIPS 感知损失：`perceptual.py`

#### 4.1.1 概念说明

LPIPS 回答的问题是：「两张图在人眼里有多不像？」它不比较像素，而是比较 **VGG 网络中间层的特征响应**。直觉是：VGG 是在 ImageNet 上训练出来的分类器，它的中间层已经学到了边缘、纹理、局部结构等「视觉原子」，两张图在这些特征上接近，看起来就接近。DreamCraft3D 用它做 `loss_reg` 的距离度量，使得渲染图向编辑图靠拢时优化的是「感知外观」而不是逐像素数值。

文件里有两层结构：裸的 `PerceptualLoss`（nn.Module，可直接 `from threestudio.utils.perceptual import PerceptualLoss` 使用），以及包了一层、注册进 threestudio 注册表的 `PerceptualLossObject`（注册名 `perceptual-loss`）。dreamcraft3d 系统用的是后者。

#### 4.1.2 核心流程

对输入 \( x \)（渲染图）与 \( y \)（编辑图）：

1. 两个输入各自过 `ScalingLayer` 做 Imagenet 式的移位/缩放归一化；
2. 过同一个冻结 VGG16，取 5 个 ReLU 层的特征 \( x^k, y^k \)，\( k \in \{1..5\} \)，通道数分别为 64/128/256/512/512；
3. 每层特征在通道维做单位化：\( \hat{x}^k = x^k / (\|x^k\|_2 + \epsilon) \)；
4. 计算逐位置平方差 \( d^k = (\hat{x}^k - \hat{y}^k)^2 \)；
5. 每层过一个**无 bias 的 1×1 卷积**（`NetLinLayer`，权重来自 LPIPS 官方预训练），把 C 通道压到 1，再对空间维取均值；
6. 5 层结果相加得到最终距离：

\[ d_{LPIPS}(x, y) = \sum_{k=1}^{5} \; \underbrace{\frac{1}{H_k W_k} \sum_{h,w} \left\| w_k \odot \left( \hat{x}^k_{hw} - \hat{y}^k_{hw} \right) \right\|_2^2}_{\text{第 } k \text{ 层的加权空间平均}} \]

其中 \( w_k \) 是学习到的通道权重。整个网络的所有参数 `requires_grad=False`——它是度量，不是被优化的对象；梯度只流过输入 \( x \)（渲染图）。

#### 4.1.3 源码精读

注册对象：把 LPIPS 包成可被 `threestudio.find("perceptual-loss")` 找到的组件，构造参数只有一个 `use_dropout`：

- [threestudio/utils/perceptual/perceptual.py:L15-L30](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/perceptual/perceptual.py#L15-L30) — 注册 `perceptual-loss`，`configure` 内实例化裸的 `PerceptualLoss` 并搬到设备，`__call__` 直接透传。

主干网络的装配：ScalingLayer + 冻结 VGG16 + 5 个 `NetLinLayer`，随后加载 LPIPS 官方权重并冻结全部参数：

- [threestudio/utils/perceptual/perceptual.py:L33-L54](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/perceptual/perceptual.py#L33-L54) — 构造 5 层结构；`load_from_pretrained` 从 `threestudio/utils/lpips/vgg.pth` 加载（见下），最后 `for param in self.parameters(): param.requires_grad_(False)`。

前向计算，即 4.1.2 流程的落地：

- [threestudio/utils/perceptual/perceptual.py:L67-L85](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/perceptual/perceptual.py#L67-L85) — `normalize_tensor` 做通道单位化，`diffs[kk] = (feats0[kk] - feats1[kk]) ** 2`，每个 `lins[kk].model(diffs[kk])` 是 1×1 卷积加权，`spatial_average` 对 H/W 取均值，最后 5 层求和。

归一化与线性层的定义：

- [threestudio/utils/perceptual/perceptual.py:L88-L99](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/perceptual/perceptual.py#L88-L99) — `ScalingLayer`：固定的 shift/scale（ImageNet 统计量）。
- [threestudio/utils/perceptual/perceptual.py:L102-L117](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/perceptual/perceptual.py#L102-L117) — `NetLinLayer`：可选 Dropout + 无 bias 1×1 卷积。
- [threestudio/utils/perceptual/perceptual.py:L167-L173](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/perceptual/perceptual.py#L167-L173) — `normalize_tensor`（单位化）与 `spatial_average`（空间均值）。

权重自动下载——`vgg.pth` 不在 `load/` 手动权重清单里，首次使用时自动补齐：

- [threestudio/utils/perceptual/utils.py:L7-L11](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/perceptual/utils.py#L7-L11) — URL/文件名/md5 三张映射表，源是海德堡大学的服务器。
- [threestudio/utils/perceptual/utils.py:L32-L40](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/perceptual/utils.py#L32-L40) — `get_ckpt_path`：文件不存在则下载并断言 md5，返回本地路径。

一个与门控相关的系统侧事实：dreamcraft3d 系统在 `configure` 里**无条件**构造了感知损失，与 `lambda_reg` 是否为 0 无关：

- [threestudio/systems/dreamcraft3d.py:L55-L56](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L55-L56) — `p_config = {}`（空 dict 走 dataclass 默认值 `use_dropout=True`），`find("perceptual-loss")` 实例化。因此即便 coarse 阶段也常驻一份 VGG+LPIPS 权重。另注意 [threestudio/systems/dreamcraft3d.py:L18](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L18) 直接 `from threestudio.utils.perceptual import PerceptualLoss` 的导入在系统内并未被使用，属死导入。

#### 4.1.4 代码实践

**实践目标**：在不启动 3D 训练的前提下，单独运行 LPIPS，验证「感知距离 ≠ 像素距离」。

**操作步骤**（示例代码，可直接存为 `lpips_probe.py` 在仓库根目录运行）：

```python
# 示例代码：LPIPS 最小实验（CPU 可跑）
import torch, torchvision
from threestudio.utils.perceptual import PerceptualLoss

lpips = PerceptualLoss(use_dropout=False).eval()  # 关 Dropout，结果可复现
x = torch.rand(1, 3, 256, 256)                    # 基准图
y_shift = (x + 0.1).clamp(0, 1)                   # 整体亮度平移：像素全变
y_blur = torchvision.transforms.functional.gaussian_blur(x, 9)  # 轻微模糊
with torch.no_grad():
    print("亮度平移:", lpips(x, y_shift).item())
    print("轻微模糊:", lpips(x, y_blur).item())
    print("参数 requires_grad:", all(not p.requires_grad for p in lpips.parameters()))
```

**需要观察的现象**：轻微模糊的 LPIPS 距离通常明显大于全局亮度平移——亮度平移在像素空间的改动量（每个像素都变了 0.1）远大于模糊，但 VGG 特征对「纹理变糊」比对「整体亮度」敏感得多。

**预期结果**：两条距离都为正的标量；`requires_grad` 检查输出 `True`（即所有参数确实冻结）。首次运行会自动下载 `vgg.pth`（约几 MB）与 torchvision 的 VGG16 权重（约 528 MB），需要网络。具体数值**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 LPIPS 要先对每层特征做单位化（`normalize_tensor`）再作差，而不是直接比较特征？

**答案**：VGG 不同层、不同通道的特征幅度差异很大，直接作差会被幅度主导。单位化后比较的是特征在通道空间中的「方向」，即激活模式是否相似，这与感知相似性的对应关系更好；这也是 LPIPS 原始论文的设计（论文注明本文件是它的精简版，见 [perceptual.py:L1](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/perceptual/perceptual.py#L1)）。

**练习 2**：系统侧计算 `loss_reg` 时在外面乘了 `(512//8)*(512//8) = 4096`，为什么需要这个因子？

**答案**：`spatial_average` 把每层差分图在空间上取了**均值**，使 LPIPS 的量级随分辨率增大而缩小。乘上潜空间网格数 64×64 把它放大回「按格点求和」的量级，与 `lambda_reg` 权重的默认量级兼容。这是一种尺度归一化常数，见 [threestudio/systems/dreamcraft3d.py:L316](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L316)。

**练习 3**：`NetLinLayer` 里有一个 `nn.Dropout()`（`use_dropout=True` 时启用），而系统构造感知损失时没有调用 `.eval()`。训练时这会带来什么影响？

**答案**：`PerceptualLossObject` 是系统（LightningModule）的子模块，Trainer 调用 `model.train()` 会递归把它切到训练模式，Dropout 生效，`loss_reg` 会带有随机性。这是代码阅读推断出的潜在抖动来源，可通过打印 `self.perceptual_loss.perceptual_loss.training` 验证（**待本地验证**）。对照：姊妹文件里显式写了 `PerceptualLoss().eval()`，见 [threestudio/models/guidance/controlnet_guidance.py:L147](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/controlnet_guidance.py#L147)。

### 4.2 ControlNet 编辑引擎：`controlnet_reg_guidance.py`

#### 4.2.1 概念说明

`stable-diffusion-controlnet-reg-guidance` 是一个「**编辑打样器**」：输入当前视角的渲染图，输出一张经 ControlNet 引导的 SDEdit 重绘图。它的三个关键设计：

1. **结构条件来自渲染自身**：系统调用它时 `cond_rgb=rgb`（就是当前渲染图），法向条件图由 `NormalBaeDetector` 从渲染图现场提取。也就是说，编辑只在「保持当前结构」的前提下修正外观——这与 texture 阶段 `fix_geometry: true` 冻结几何的事实严格对齐。
2. **编辑强度受采样时间步控制**：`t` 从 `[min_step, max_step]` 采样，`t/num_train_timesteps` 即 img2img 的 strength；texture 配置里被注释的段落把它收紧到 0.1–0.5，只做轻度重绘。
3. **mask 保护参考视角可见区域**：编辑循环里 mask=1 的区域跟随扩散去噪，mask=0 的区域保持原渲染。而系统传入的 mask 恰好是「参考视角**不可见**区域」（见 4.3.1），于是编辑只发生在没有真值的区域。

它派生自 `BaseObject` 而非任何 guidance 基类——「guidance」之名只说明它被放在 guidance 目录、以 `control_guidance_type` 接入；它不返回任何扩散蒸馏损失，唯一的产出是 `edit_images`。

#### 4.2.2 核心流程

`__call__` 的完整数据流（批量 B=1）：

```text
渲染图 rgb (1,512,512,3)
  ├─ permute → (1,3,512,512) ─ VAE encode ─→ latents (1,4,64,64)
  ├─ cond_rgb(=rgb) ─ NormalBaeDetector ─→ 法向条件图 image_cond (1,3,512,512)
  └─ prompt_utils.get_text_embeddings(0,0,0,False) ─→ text_embeddings (2,77,768)
                                                      （cond 在前 / uncond 在后）
采样 t ~ U[min_step, max_step]          # strength = t / 1000
edit_latents:
  z_t = add_noise(latents, ε, 对应 t_start 的 latent_timestep)
  循环 diffusion_steps - t_start 次:
    [z_t; z_t] ─→ ControlNet(法向条件) ─ 残差 ─→ UNet ─→ noise_pred (2,4,64,64)
    CFG: ε̂ = ε_uncond + guidance_scale·(ε_cond − ε_uncond)
    mask 混合: ε̂ = ε̂·mask + (1−mask)·ε   # mask=0 处用原始噪声→该区域保持不变
    z_t = scheduler.step(ε̂, t, z_t)
edit_images = VAE decode(z_T) (1,3,512,512) → 插值回 (H,W) → (1,H,W,3)
```

其中 CFG 公式为 \( \hat{\epsilon} = \epsilon_{\text{uncond}} + s \cdot (\epsilon_{\text{cond}} - \epsilon_{\text{uncond}}) \)，默认 \( s = 7.5 \)。

mask 混合的数学直觉：区域里若令 \( \hat{\epsilon} = \epsilon \)（恰好是加进去的那个噪声），则该步反解出的干净估计 \( x_0^{pred} = (z_t - \sqrt{1-\bar{\alpha}_t}\,\hat{\epsilon}) / \sqrt{\bar{\alpha}_t} \) 恰好等于加噪前的原始 latents——即这一区域在整条去噪链上近似保持原渲染不动（对 DDIM 精确成立，对这里实际使用的 DPMSolver 近似成立）。

#### 4.2.3 源码精读

装配：根据主干模型选 ControlNet 权重、加载预处理器，并注意调度器被换成了 DPMSolver：

- [threestudio/models/guidance/controlnet_reg_guidance.py:L20-L21](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/controlnet_reg_guidance.py#L20-L21) — 注册名 `stable-diffusion-controlnet-reg-guidance`。
- [threestudio/models/guidance/controlnet_reg_guidance.py:L52-L74](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/controlnet_reg_guidance.py#L52-L74) — `configure`：fp16 权重、取预处理器与 ControlNet 名、装配管线，最后 `self.pipe.scheduler = DPMSolverMultistepScheduler.from_config(...)` 再赋给 `self.scheduler`——**虽然 L118 从 SD1.5 加载了 DDIMScheduler，真正跑编辑的是 DPMSolver**。
- [threestudio/models/guidance/controlnet_reg_guidance.py:L76-L89](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/controlnet_reg_guidance.py#L76-L89) — 模型选型的分支逻辑：默认主干 `SG161222/Realistic_Vision_V2.0`（SD1.5 系微调）配 `lllyasviel/control_v11p_sd15_normalbae`，其他主干配 SD2.1 版 normalbae；`canny`/`canny2` 另有对应权重。法向预处理器 `NormalBaeDetector` 从 `lllyasviel/Annotators` 加载并搬上 GPU。
- [threestudio/models/guidance/controlnet_reg_guidance.py:L107-L128](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/controlnet_reg_guidance.py#L107-L128) — `load_models`：`ControlNetModel` + `StableDiffusionControlNetPipeline` + DDIM 调度器依次 `from_pretrained`，vae/unet/controlnet 均 `.eval()`。

条件图预处理：把渲染图变成 ControlNet 要的空间条件：

- [threestudio/models/guidance/controlnet_reg_guidance.py:L290-L323](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/controlnet_reg_guidance.py#L290-L323) — `prepare_image_cond`：`normal` 分支先 detach 到 CPU 转 numpy，过 `NormalBaeDetector` 得法向图再回 GPU；`canny` 分支先 5×5 均值模糊再提边缘；`input_normal` 分支直接用输入法向并翻转 x 通道（与 u6-l3 法向损失里的手性补丁同源）；最后统一双线性插值到 512×512。

编辑循环：SDEdit 的核心，含 strength 换算、CFG 与 mask 混合：

- [threestudio/models/guidance/controlnet_reg_guidance.py:L238-L250](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/controlnet_reg_guidance.py#L238-L250) — strength 换算：`init_timestep = diffusion_steps · t/num_train_timesteps`，`t_start = diffusion_steps − init_timestep`，用 `timesteps[t_start]` 作为加噪时刻；随后整个循环在 `torch.no_grad()` 内（编辑图是监督目标，不需要梯度路径）。注意 L243 的 `origin_latents = latents.clone()` 之后从未被使用，是死代码。
- [threestudio/models/guidance/controlnet_reg_guidance.py:L252-L276](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/controlnet_reg_guidance.py#L252-L276) — 每步把 latents 复制两份（cond/uncond），先过 ControlNet 拿 `down/mid_block` 残差，再喂 UNet；注释标明循环骨架取自 diffusers 的 instruct-pix2pix 管线。
- [threestudio/models/guidance/controlnet_reg_guidance.py:L277-L286](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/controlnet_reg_guidance.py#L277-L286) — CFG 融合后做 mask 混合并 `scheduler.step`。`noise_pred = noise_pred * mask + (1-mask) * noise` 一行即 4.2.2 分析的「区域保护」。

对外契约：`__call__` 的入口与出口：

- [threestudio/models/guidance/controlnet_reg_guidance.py:L365-L386](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/controlnet_reg_guidance.py#L365-L386) — 渲染图插值到 512 后 VAE 编码；条件图来自 `cond_rgb`；**文本嵌入用 `get_text_embeddings(temp, temp, temp, False)`——方位角/仰角/距离全传 0、`view_dependent_prompting=False`**，即永远使用无视角前缀的原始提示词嵌入（对照 u7-l1：非视角相关分支直接返回基础嵌入，[threestudio/models/prompt_processors/base.py:L71-L78](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/prompt_processors/base.py#L71-L78)）。
- [threestudio/models/guidance/controlnet_reg_guidance.py:L388-L395](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/controlnet_reg_guidance.py#L388-L395) — `t` 从 `[min_step, max_step]` 均匀采样，注释写明避开过高/过低噪声水平。
- [threestudio/models/guidance/controlnet_reg_guidance.py:L397-L418](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/controlnet_reg_guidance.py#L397-L418) — 两条出路：`use_sds=True` 时走 `compute_grad_sds`（[L325-L363](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/controlnet_reg_guidance.py#L325-L363)，即 u7-l2 讲过的 target 重参数化 SDS，只是多了 ControlNet 条件）；默认 `use_sds=False` 走编辑路径，返回 `{"edit_images", "edit_latents"}`——系统侧只认这个键。

时间步区间调度：与所有 guidance 一样用 `C()` 支持四元组随步数变化：

- [threestudio/models/guidance/controlnet_reg_guidance.py:L420-L430](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/controlnet_reg_guidance.py#L420-L430) — `update_step` 里对 `grad_clip`、`min/max_step_percent` 过 `C()`。

#### 4.2.4 代码实践

**实践目标**：绕过 3D 系统，单独驱动这个编辑引擎，观察 strength（由 `min/max_step_percent` 控制）对编辑幅度的支配作用。

**操作步骤**：文件尾部自带一个 `__main__` 探针——[threestudio/models/guidance/controlnet_reg_guidance.py:L433-L454](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/controlnet_reg_guidance.py#L433-L454)（加载配置、实例化 guidance 与 prompt processor、对 `assets/face.jpg` 做一次编辑并写 `.threestudio_cache/edit_image.jpg`）。但它引用的 `configs/experimental/controlnet-normal.yaml` **在本仓库不存在**（`configs/` 下只有四份 dreamcraft3d yaml），这是从上游 threestudio 带来的遗留路径。等价做法是自己写 `controlnet_probe.py`（示例代码）：

```python
# 示例代码：独立驱动 stable-diffusion-controlnet-reg-guidance
import threestudio
from threestudio.utils.config import ExperimentConfig, load_config
from omegaconf import OmegaConf

cfg = OmegaConf.create({
    "system": {
        "guidance_type": "stable-diffusion-controlnet-reg-guidance",
        "guidance": {
            "pretrained_model_name_or_path": "SG161222/Realistic_Vision_V2.0",
            "control_type": "normal",
            "min_step_percent": 0.1,   # 扫描点 1：改成 0.02 / 0.1 / 0.3 对比
            "max_step_percent": 0.5,
            "diffusion_steps": 20,
        },
        "prompt_processor_type": "stable-diffusion-prompt-processor",
        "prompt_processor": {
            "pretrained_model_name_or_path": "SG161222/Realistic_Vision_V2.0",
            "prompt": "a delicious hamburger",
        },
    }
})
import cv2, torch
g = threestudio.find("stable-diffusion-controlnet-reg-guidance")(cfg.system.guidance)
pp = threestudio.find("stable-diffusion-prompt-processor")(cfg.system.prompt_processor)
rgb = cv2.imread("load/images/hamburger_rgba.png")[:, :, :3][:, :, ::-1] / 255
rgb = torch.FloatTensor(rgb).unsqueeze(0).to(g.device)
out = g(rgb, rgb, pp(), mask=None)
cv2.imwrite("edit_probe.jpg",
    (out["edit_images"][0].numpy().clip(0, 1) * 255).astype("uint8")[:, :, ::-1])
```

**需要观察的现象**：固定其他参数，只改 `min_step_percent`（下界即最小 strength）。下界很小（如 0.02）时编辑图接近原渲染；调大到 0.3 后外观明显被重绘但轮廓保持——因为法向条件钉住了结构。

**预期结果**：得到一张 512×512 的编辑图；结构（轮廓、法向起伏）与输入一致，纹理/配色随强度变化。需要 GPU 且首次运行要从 HuggingFace 下载 Realistic Vision、ControlNet 权重与 `lllyasviel/Annotators` 的 NormalBae 权重。具体效果**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`edit_latents` 里 `init_timestep` 与 `t_start` 的换算为什么等价于 img2img 的 strength？

**答案**：`diffusion_steps=20` 把 1000 个训练时间步压成 20 个推理步；`t` 采样自 `[min_step, max_step]`（数值就是训练时间步），`init_timestep = 20 · t/1000` 表示「这次加噪相当于从 20 步里的第几步开始」，`t_start = 20 − init_timestep` 是剩余去噪步数。t 越大 → 加噪越深 → 剩余步越多 → 编辑偏离越大，即 strength = t/1000。

**练习 2**：为什么 dreamcraft3d 系统必须**单独**配置 `control_prompt_processor`（Realistic Vision），而不能复用主 `prompt_processor`（SD2.1-base）？

**答案**：两者文本嵌入维度不同——SD2.1 的 CLIP 是 1024 维，SD1.5 系（Realistic Vision）是 768 维，而 ControlNet 管线的 UNet 交叉注意力维度由主干决定。喂错维度的嵌入会直接形状不匹配报错。代码侧的印证是 `get_preprocessor_and_controlnet` 按主干是否为 Realistic Vision 来选 sd15/sd21 版 ControlNet（[controlnet_reg_guidance.py:L77-L81](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/controlnet_reg_guidance.py#L77-L81)）。这也再次印证 u7-l1 的结论：prompt processor 必须与 guidance 共用同一模型路径。

**练习 3**：`threestudio/models/guidance/controlnet_guidance.py`（注册名 `stable-diffusion-controlnet-guidance`）与本文件的 `use_du` 分支已经内置了 L1+LPIPS 回归损失（[controlnet_guidance.py:L356-L433](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/controlnet_guidance.py#L356-L433)），能否把它直接填进 `control_guidance_type`？

**答案**：不能。系统侧固定读取 `control_dict["edit_images"]`（[dreamcraft3d.py:L312](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L312)），而 `controlnet_guidance.py` 的 `__call__` 只返回 `loss_sds/loss_l1/loss_p` 等键、从不返回 `edit_images`，会抛 `KeyError`。这是注册机制之外的一层**接口契约**：组件可替换的前提是返回字典的键兼容。`use_du` 从命名推断对应论文中对比的 delta update 式策略（编辑-回归交替，Instruct-NeRF2NeRF 风格），它在 DreamCraft3D 中只作为代码保留，未接入主流程（**待确认**：论文实验细节请对照原文）。

### 4.3 系统接线、三重门控与 yaml 开关

#### 4.3.1 概念说明

这段功能在系统侧是一个**双层开关 + 三重门控**的结构：

- **装配开关**：`control_guidance_type` 非空才构造 `control_guidance` 与 `control_prompt_processor`，否则系统上根本没有这两个属性；
- **执行开关**：`loss.lambda_reg > 0` 才进入正则分支；
- **三重门控**：分支内部还要求 `guidance == "guidance"`（必须在扩散引导子步，不在参考图子步）且 `true_global_step % 5 == 0`（每 5 步一次）。

两个开关**必须同时打开**：只开 `lambda_reg` 会在调用 `self.control_guidance(...)` 处直接 `AttributeError`；只开 `control_guidance_type` 则分支永远休眠（还要白白加载一整套 SD1.5+ControlNet 权重）。默认 texture 配置两者皆关（`lambda_reg: 0.0`，控制段整体被注释）。

mask 的来源值得单独强调：texture 阶段 `forward()` 以 `render_mask=True` 调用渲染器（[dreamcraft3d.py:L66-L69](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L66-L69)），渲染器先用参考视角矩阵 `mvp_mtx_ref` 光栅化一遍、把参考视角可见的面片顶点标 1，再在当前视角插值出 `mask_vis`，输出 `out["mask"] = 1 - mask_vis`——即**当前视角像素显示的是参考视角看不见的表面**时 mask 为 1。这正是 u5-l4 结尾埋下的伏笔：「texture 阶段输出参考视角不可见区域 mask 供 BSD 使用」，本讲的 ControlNet 编辑是它的另一个消费者。

#### 4.3.2 核心流程

texture 分支（`stage == "texture"` 且 `lambda_reg` 生效）的执行序列：

```text
training_step（alternate, n_ref=2）
  └─ training_substep(guidance="guidance")
       ├─ 主体：BSD 引导（u7-l4/u7-l5，损失 loss_sd/loss_lora/loss_pretrain）
       └─ stage=="texture" 分支:
            门控: C(lambda_reg)>0 ∧ guidance=="guidance" ∧ true_global_step%5==0
            rgb = comp_rgb → 双线性插值到 512×512
            control_prompt_utils = control_prompt_processor()   # 每次现取
            with no_grad:
                edit_images = control_guidance(rgb, cond_rgb=rgb, prompt_utils, mask)
                写 .threestudio_cache/control_debug.jpg          # 每次覆盖
            loss_reg = 4096 × LPIPS(edit_images, rgb).mean()
            → set_loss("reg", ...) → 记入 loss_guidance_reg，× C(lambda_reg) 加权
```

与交替调度的叠加：`n_ref=2` 时偶数步走 ref 子步、奇数步走 guidance 子步；门控要求 `true_global_step % 5 == 0` 且为 guidance 步，即步号满足 \( \equiv 5 \pmod{10} \)——**常规相位下约每 10 步才真正执行一次**。唯一的例外是 BSD 的预训练窗口（`only_pretrain_step=1000`，每周期前 200 步强制全 guidance，见 u6-l2/u7-l5），窗口内凡 `%5==0` 的步都会执行，频率升到每 5 步一次。

#### 4.3.3 源码精读

可选组件的装配——「空字符串即不组装」的模式（u6-l1 讲过）：

- [threestudio/systems/dreamcraft3d.py:L32-L35](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L32-L35) — Config 里的 `control_guidance_type` / `control_guidance` / `control_prompt_processor_type` / `control_prompt_processor` 四个字段，前两个默认空。
- [threestudio/systems/dreamcraft3d.py:L55-L63](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L55-L63) — 感知损失无条件装配；control 一族仅当 `control_guidance_type` 非空时装配，且 `control_prompt_processor` 在同一分支内（开了前者就必须同时给后者）。

mask 的产生：

- [threestudio/models/renderers/nvdiff_rasterizer.py:L57-L77](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nvdiff_rasterizer.py#L57-L77) — `render_mask` 分支：参考视角光栅化 → 可见面片顶点置 1（`mesh._v_rgb`）→ 当前视角插值 → `out.update({"mask": 1.0 - mask_vis.float()})`。全程 `no_grad`（mask 只作门控，不传梯度）。

texture 分支本体：

- [threestudio/systems/dreamcraft3d.py:L298-L302](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L298-L302) — 三重门控与 512 下采样：1024 的训练渲染被压到 512 再送编辑器（编辑与 LPIPS 都在 512 上进行）。
- [threestudio/systems/dreamcraft3d.py:L303-L310](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L303-L310) — `with torch.no_grad()` 包住整个编辑调用：`edit_images` 是**目标**不是梯度路径，`loss_reg` 的梯度只经 `rgb`（即渲染图一侧）回传到场景外观网络。
- [threestudio/systems/dreamcraft3d.py:L312-L314](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L312-L314) — 每次执行都把编辑图写到 `.threestudio_cache/control_debug.jpg`（覆盖式），这是观察编辑质量最直接的窗口；该目录在 prompt processor 构造 `.threestudio_cache/text_embeddings` 时已随之创建（[prompt_processors/base.py:L223](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/prompt_processors/base.py#L223) 与 [L345](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/prompt_processors/base.py#L345)）。
- [threestudio/systems/dreamcraft3d.py:L316-L317](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L316-L317) — `loss_reg = 64·64 · LPIPS(edit_images, rgb).mean()`，经 `set_loss("reg", ...)` 记为 `loss_guidance_reg`。
- [threestudio/systems/dreamcraft3d.py:L321-L329](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L321-L329) — 汇总时按 `name.replace(loss_prefix, "lambda_")` 查表加权，`loss_guidance_reg` 对应 `lambda_reg`，支持 `C()` 四元组调度（与 u8-l1 呼应）。

两个开关在配置里的默认状态：

- [configs/dreamcraft3d-texture.yaml:L100-L109](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L100-L109) — 整段被注释的 `control_guidance_type` / `control_guidance` / `control_prompt_processor_type` / `control_prompt_processor`，时间步区间收窄为 0.1–0.5。
- [configs/dreamcraft3d-texture.yaml:L137](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L137) — `lambda_reg: 0.0`。
- 对照主引导段 [configs/dreamcraft3d-texture.yaml:L71-L86](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L71-L86)：主 prompt processor 用 SD2.1-base，control 段用 Realistic Vision，正是 4.2.5 练习 2 的实例。

顺带一提：texture 配置的 `strategy: ddp_find_unused_parameters_true`（[L161](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L161)）与本讲的功能形态相关——这类「部分步数才触达部分参数」的分支调度（BSD 双 UNet、可选 control 组件）在 DDP 下必须允许未用参数，否则梯度规约会失败。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：在不修改任何源码文件的前提下，用命令行覆盖启用 ControlNet 正则，观察编辑图与纹理漂移的对照。

**操作步骤**：

1. 准备好 texture 阶段的前置条件（u2-l3/u2-l4）：先完成 coarse-nerf → coarse-neus → geometry 三阶段的 ckpt，得到 geometry 阶段的试验目录（假设为 `outputs/dreamcraft3d-geometry/<tag>/`）。
2. 不改 yaml，直接用 launch.py 的 extras 覆盖（点号语法，u2-l2）启动 texture 训练：

```bash
python launch.py --config configs/dreamcraft3d-texture.yaml --train \
  --gpu 0 \
  system.prompt_processor.prompt="a delicious hamburger" \
  system.geometry_convert_from=outputs/dreamcraft3d-geometry/<tag>/ckpts/last.ckpt \
  system.control_guidance_type="stable-diffusion-controlnet-reg-guidance" \
  system.control_guidance.min_step_percent=0.1 \
  system.control_guidance.max_step_percent=0.5 \
  system.control_guidance.control_type="normal" \
  system.control_prompt_processor_type="stable-diffusion-prompt-processor" \
  system.control_prompt_processor.pretrained_model_name_or_path="SG161222/Realistic_Vision_V2.0" \
  system.control_prompt_processor.prompt="a delicious hamburger" \
  system.control_prompt_processor.front_threshold=30. \
  system.control_prompt_processor.back_threshold=30. \
  system.loss.lambda_reg=10. \
  trainer.max_steps=500
```

   也可以复制一份 yaml 取消 [L100-L109](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L100-L109) 的注释并改 `lambda_reg`，效果等价。
3. 训练启动后另开终端监视调试图：`ls -l .threestudio_cache/control_debug.jpg`（文件时间戳应约每 10 步刷新一次，见 4.3.2 的频率分析），并随时查看其内容。
4. 对照实验：同样命令去掉 `system.loss.lambda_reg` 一行（即保持默认 0.0）再跑 500 步——control 组件仍被装配，但分支休眠；比较两次的 `save/` 渲染与 TensorBoard 中 `train/loss_guidance_reg`（对照 run 应不出现该曲线）。

**需要观察的现象**：

- `control_debug.jpg` 中，参考视角可见的区域应与当前渲染基本一致（mask 保护），背面/侧面等不可见区域被 ControlNet 按提示词重绘；
- 随训练推进，编辑图与渲染图应越来越接近（`train/loss_guidance_reg` 总体下降、伴随 BSD 扰动带来的波动）；
- 置零 run 的背面/侧面纹理更可能偏离参考图配色与材质风格（漂移），开启 run 的整体风格更贴近参考图。

**预期结果**：开启后若 `lambda_reg` 过大，参考视角的保真可能被拉向编辑图的「平均审美」，正面渲染质量反而下降——建议从较小值（如 0.1–10 区间扫描）起步。编辑图的视觉效果、最优权重、以及 `AttributeError`/显存开销等运行细节均**待本地验证**（额外装配一套 SD1.5+ControlNet 约多占数 GB 显存）。

#### 4.3.5 小练习与答案

**练习 1**：若只设置 `system.loss.lambda_reg=10` 而不设置 `control_guidance_type`，会发生什么？反过来呢？

**答案**：只开 `lambda_reg`：门控通过、进入分支，但 `configure` 从未构造 `self.control_guidance`，在 [dreamcraft3d.py:L305](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L305) 处抛 `AttributeError`。只开 `control_guidance_type`：整套 ControlNet 管线被加载（占显存、拖慢启动），但 `C(lambda_reg)=0` 使分支永不执行，属于静默休眠。正确用法是两个开关成对打开。

**练习 2**：为什么 `loss_reg` 只在 `guidance == "guidance"` 的子步计算，而不能放在 ref 子步？

**答案**：ref 子步渲染的是**参考视角**，整个画面都在 `out["mask"]` 的保护范围内（参考视角看自己的渲染，不存在「不可见区域」），mask 混合会让编辑几乎不发生；且 ref 子步已有逐像素的强监督（`lambda_rgb=1000` 的 L1+grow_mask），再叠加 LPIPS 软监督既冗余又可能互相牵制。这个功能的设计对象就是「参考图管不到的视角」。

**练习 3**：把 `freq.n_ref` 从 2 改成 4（其他不变），reg 分支的实际执行频率如何变化？

**答案**：门控仍是 `true_global_step % 5 == 0` 且该步为 guidance 步。`n_ref=4` 时 ref 步是 `%4==0` 的步，guidance 步为 `%4 ∈ {1,2,3}`；两者交集是 `%20 ∈ {5,10,15}`，即每 20 步 3 次、平均约 6.7 步一次，但**间隔不均匀**（5、5、10 交替）。这说明 reg 频率由 `n_ref` 与 `%5` 的最小公倍数节拍决定，改任一参数都应重新推一遍相位。

## 5. 综合实践

**任务：完成一次「开启 → 观察 → 消融」的完整实验闭环。**

1. **开启**：按 4.3.4 的命令行覆盖方式启动 texture 阶段（可用较小 `trainer.max_steps`，如 500–1000 步），确认日志出现 ControlNet 管线加载信息、`.threestudio_cache/control_debug.jpg` 周期性刷新。
2. **观察编辑器**：在训练前/中/后各保存一份 `control_debug.jpg`（自行 `cp` 留存），并在 4.2.4 的独立探针里扫描 `min_step_percent ∈ {0.02, 0.1, 0.3}`，整理一张「strength → 编辑幅度」的对比图，验证 4.2.2 的 strength 分析。
3. **观察损失**：从 TensorBoard 导出 `train/loss_guidance_reg` 与 `train/loss_guidance_sd`（BSD 主损失）两条曲线，观察开启 reg 后 `loss_sd` 的走势是否受到抑制性影响。
4. **消融**：`lambda_reg` 置零再跑相同步数，从 `save/` 的验证视角（尤其背面与侧面）对比两组成品纹理与参考图风格的一致性，写一段 5–10 句的结论：软监督在哪些视角收益最大、过强时有什么副作用。
5. **加分项**：在训练脚本里临时加一行日志打印 `self.perceptual_loss.perceptual_loss.training`，验证 4.1.5 练习 3 关于 Dropout 训练模式的推断（验证后删除该行，勿提交）。

若没有足够的算力跑完整 texture 阶段，可退化为「源码阅读型」版本：完成 4.1.4 与 4.2.4 两个轻量实践，再手绘一张从 `training_step` 到 `control_debug.jpg` 的完整数据流图（含每一步的张量形状与 mask 语义），标注三重门控的位置。

## 6. 本讲小结

- `stable-diffusion-controlnet-reg-guidance` 是一个旁路编辑器：以当前渲染的法向图为结构条件、以提示词为语义条件做 SDEdit 式 img2img，产出 `edit_images` 作为参考视角不可见区域的软监督伪真值。
- 编辑循环中的 mask 混合（`noise_pred·mask + (1-mask)·noise`）配合渲染器输出的「参考视角不可见 mask」，实现了「看不见的区域交给扩散重绘、看得见的区域保持原渲染」的分工。
- `loss_reg = 64·64 · LPIPS(edit_images, rgb)`：LPIPS 是冻结的 VGG+学习线性权重度量，权重自动下载；编辑在 `no_grad` 内进行，梯度只流向渲染图一侧。
- 系统侧是「双层开关 + 三重门控」：`control_guidance_type` 控制装配、`lambda_reg` 控制执行，二者必须成对打开；分支还要求 guidance 子步且每 5 步一次，叠加 `n_ref=2` 交替后实际约每 10 步执行一次。
- 默认 texture 配置中该功能整体关闭（配置段被注释 + `lambda_reg: 0.0`），属于实验性功能；姊妹文件 `controlnet_guidance.py` 的 delta-update 变体因返回键不含 `edit_images` 无法直接接入，体现了「可替换组件」背后还有接口契约这层约束。
- 借助命令行 extras 覆盖，可以不改任何源码文件地完成实验性功能的开启与消融。

## 7. 下一步学习建议

单元七到此完整收尾：你已经读完了 DreamCraft3D 的全部扩散引导通道（DeepFloyd SDS → Stable Zero123 → BSD 双 LoRA → ControlNet 正则）。进入单元八建议：

1. **u8-l1（C() 函数与步数感知参数）**：本讲多次出现的 `C(lambda_reg)`、`C(min/max_step_percent)` 四元组调度的实现就在 `threestudio/utils/misc.py`，值得一次性读透。
2. **u8-l2（DreamBooth 个性化）**：与本讲呼应——BSD 与 DreamBooth 的关系（`train_pretrain` 的 DreamBooth 式更新）在 u7-l5 已铺垫，u8-l2 会读独立的 LoRA 训练脚本。
3. 若想继续深挖本讲主题，可对照阅读 diffusers 的 `StableDiffusionControlNetPipeline` 与 instruct-pix2pix 管线源码（编辑循环的注释里明确引用了后者），理解「按 strength 部分去噪」在官方实现中的标准写法与本仓库精简版的差异。

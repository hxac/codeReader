# DeepFloyd guidance 与 SDS 得分蒸馏

## 1. 本讲目标

学完本讲，你应该能够：

1. 推导 SDS（Score Distillation Sampling，得分蒸馏采样）梯度的加权差分形式：\(\nabla_{x_0}\mathcal{L} \propto w(t)\cdot(\hat\epsilon - \epsilon)\)，并解释每个符号在代码里对应哪一行。
2. 读懂 `DeepFloydGuidance.__call__` 的完整链路：渲染图缩放 → 前向加噪 → UNet 双倍 batch 前向 → CFG 加权 → 乘 \(w(t)\) → 用重参数化技巧把「外生梯度」注入 autograd 图。
3. 理解 `min_step_percent` / `max_step_percent` 的时间步区间调度：为什么采样 \(t\) 要避开极高与极低噪声，区间又如何随训练步数收缩。
4. 理解 perp-neg（垂直负向引导）如何把负向提示的惩罚投影到正向引导的正交方向上，以及 `guidance_scale` 大小对粗阶段几何演化的影响。

本讲只聚焦粗阶段（coarse）的文本先验通道 `deep-floyd-guidance`。它与 `stable-zero123-guidance`（下一讲）在同一 guidance 子步里先后被调用，构成 u6-l1 讲过的双引导；本讲末尾不再重复那个全局图。

## 2. 前置知识

### 2.1 DDPM 前向加噪与噪声预测

扩散模型（DDPM）的训练目标是学习「把一张带噪图像还原成干净图像」。前向过程对干净数据 \(x_0\) 按固定 schedule 加噪：

\[ x_t = \sqrt{\bar\alpha_t}\, x_0 + \sqrt{1-\bar\alpha_t}\,\epsilon, \qquad \epsilon \sim \mathcal{N}(0, I) \]

其中 \(\bar\alpha_t = \prod_{s\le t}(1-\beta_s)\) 是随 \(t\) 单调递减的累积量：\(t=0\) 时 \(\bar\alpha_t\approx 1\)（几乎无噪），\(t\to T\) 时 \(\bar\alpha_t\to 0\)（纯噪声）。UNet 被训练成给定 \((x_t, t)\) 预测加进去的那个噪声 \(\epsilon\)。

在代码里，\(\bar\alpha_t\) 就是 `scheduler.alphas_cumprod`，加噪操作就是 `scheduler.add_noise(latents, noise, t)`。

### 2.2 classifier-free guidance（CFG）

同一 UNet 用两种条件各跑一次：有文本条件（cond）与空条件（uncond）。两个预测的差 \(\epsilon_\text{cond}-\epsilon_\text{uncond}\) 指向「文本想要的方向」。CFG 用一个缩放系数 \(s\)（代码里的 `guidance_scale`）放大这个方向：

\[ \hat\epsilon = \epsilon_\text{uncond} + s\,(\epsilon_\text{cond}-\epsilon_\text{uncond}) \]

实现上不是跑两次前向，而是把 `latents_noisy` 在 batch 维复制两份、把 cond/uncond 两组文本嵌入拼成 \(2B\) 行，一次前向同时算完。

### 2.3 为什么需要「蒸馏」而不是直接采样

我们的目标不是生成一张图，而是优化一个三维场景（NeRF）。若走完整的扩散采样链（几十上百步 UNet 去噪）来「画」每个视角再回传梯度，代价不可接受，而且采样链的离散步骤不可微。DreamFusion 的洞察是：**不需要完整采样，只需要扩散模型在当前噪声水平下对渲染图的「评分方向」**。把渲染图 \(x_0\) 加噪到某个 \(x_t\)，让冻结的 UNet 预测噪声；预测值与真实加入噪声的差，就是「这张图哪里不像模型所知的真实图像」的方向信号，直接作为梯度回传给场景参数。这就是「得分蒸馏」：蒸馏的是扩散模型的得分（score，即 \(\nabla_x \log p(x)\)，与噪声预测只差一个 \(-\sigma_t\) 缩放），而不是最终图片。

### 2.4 DeepFloyd IF：像素空间扩散

大多数 SD 系模型在 VAE 潜空间（latent space）工作，而 DeepFloyd IF 是**像素空间**扩散模型，基础分辨率 64×64。这带来两个代码层面的直接后果：

- 渲染图 `rgb` 直接缩放到 64×64 就是扩散输入，没有 VAE encode/decode（代码里变量虽叫 `latents`，但 `assert rgb_as_latents == False` 且注释明言 "No latent space"）。
- IF 的 UNet 输出 **6 通道**：前 3 通道是噪声预测 \(\epsilon\)，后 3 通道是学到的方差（DDPM 的 learned variance）。所以代码到处出现 `.split(3, dim=1)`，蒸馏只用前 3 通道。

另外，IF 的文本编码器是 T5-XXL，交叉注意力维度为 77×4096——这正是 u7-l1 讲过的「coarse 阶段必须配 `deep-floyd-prompt-processor`」的原因：guidance 与 prompt processor 必须共用同一模型，嵌入维度才能对上。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [threestudio/models/guidance/deep_floyd_guidance.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/deep_floyd_guidance.py) | DeepFloyd IF 的 SDS 引导实现，本讲主角 | 全文件：configure / forward_unet / `__call__` / get_noise_pred / update_step |
| [threestudio/utils/ops.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/ops.py) | 通用算子库 | `perpendicular_component`（perp-neg 的投影）、`SpecifyGradient`（被弃用的梯度注入方案） |
| [configs/dreamcraft3d-coarse-nerf.yaml](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml) | 粗阶段 NeRF 配置 | `system.guidance` 段：guidance_scale 与两个时间步四元组 |
| [threestudio/systems/dreamcraft3d.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py) | 系统层 | guidance 的调用点与 `loss_sd` 的消费 |
| [threestudio/utils/misc.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/misc.py) | 杂项工具 | `C()` 四元组插值（u2-l2 已讲机制，本讲看它的消费端） |
| [threestudio/models/prompt_processors/base.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/prompt_processors/base.py) | 提示词嵌入（u7-l1 主角） | `get_text_embeddings` / `get_text_embeddings_perp_neg` 的返回形状 |

## 4. 核心概念与源码讲解

本讲把 `deep_floyd_guidance.py` 这个单一最小模块拆成五段讲解：模型加载与冻结 → SDS 数学 → `__call__` 主流程 → 时间步调度 → perp-neg。

### 4.1 模型加载与冻结：configure

#### 4.1.1 概念说明

guidance 组件是一个「只读的裁判」：它自己永远不训练，权重全部冻结（`requires_grad_(False)`），训练中被优化的只有三维场景。configure 阶段做三件事：加载 IF 管线并按需挂 LoRA、冻结 UNet、把调度器参数（\(\bar\alpha_t\) 序列、总时间步数）搬到 GPU 上备查。

#### 4.1.2 核心流程

```text
configure()
 ├─ 从 HuggingFace 加载 IFPipeline（fp16，去掉 text_encoder/safety_checker 等无关组件）
 ├─ （可选）加载 DreamBooth LoRA 权重（u8-l2 的个性化路径）
 ├─ UNet .eval() + 全参数 requires_grad_(False)
 ├─ 记录 num_train_timesteps（IF 为 1000）
 ├─ （可选）构造 time_prior 的累积采样权重（默认未启用）
 └─ alphas = scheduler.alphas_cumprod → GPU
```

#### 4.1.3 源码精读

注册名与类定义——配置里 `guidance_type: "deep-floyd-guidance"` 的查找目标：

- [threestudio/models/guidance/deep_floyd_guidance.py:L18-L19](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/deep_floyd_guidance.py#L18-L19)：`@threestudio.register("deep-floyd-guidance")` 装饰 `DeepFloydGuidance(BaseObject)`，走 u3-l1 讲过的注册机制。

IF 管线加载，注意剥掉了 text_encoder——文本嵌入由 prompt processor 负责（u7-l1），guidance 只需要 UNet：

- [threestudio/models/guidance/deep_floyd_guidance.py:L59-L70](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/deep_floyd_guidance.py#L59-L70)：`IFPipeline.from_pretrained(..., text_encoder=None, safety_checker=None, ...)` 按 `half_precision_weights` 以 fp16 加载权重到 `self.device`。

可选的 DreamBooth LoRA 接口——u8-l2 会用它替换文生图先验以缓解 Janus 问题：

- [threestudio/models/guidance/deep_floyd_guidance.py:L73-L75](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/deep_floyd_guidance.py#L73-L75)：若配置了 `lora_weights_path` 则 `load_lora_weights`，并把 scheduler 方差类型改为 `fixed_small`。

冻结 UNet 与保存 \(\bar\alpha_t\)：

- [threestudio/models/guidance/deep_floyd_guidance.py:L101-L104](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/deep_floyd_guidance.py#L101-L104)：`self.unet = self.pipe.unet.eval()` 后对全部参数 `requires_grad_(False)`——「裁判」身份的代码落点。
- [threestudio/models/guidance/deep_floyd_guidance.py:L127-L133](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/deep_floyd_guidance.py#L127-L133)：把 `alphas_cumprod`（即 \(\bar\alpha_t\) 序列）存为 `self.alphas` 并搬到设备上，后面计算权重 \(w(t)\) 时直接按时间步索引。

UNet 前向的统一封装——注意它强制关闭 autocast，进出的 dtype 显式转换：

- [threestudio/models/guidance/deep_floyd_guidance.py:L144-L156](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/deep_floyd_guidance.py#L144-L156)：`forward_unet(latents, t, encoder_hidden_states)` 把三个输入都转成 `weights_dtype`（fp16）喂给 UNet，输出再 `.to(input_dtype)` 转回 fp32。训练全局是 16-mixed 精度，而 UNet 权重是 fp16，这里用 `@torch.cuda.amp.autocast(enabled=False)` 隔离，避免混合精度把 UNet 内部算成别的精度。

#### 4.1.4 代码实践

**实践：确认裁判确实被冻结。**

1. 实践目标：验证 guidance 的 UNet 参数全部 `requires_grad=False`，且不在系统 optimizer 的参数组里。
2. 操作步骤：
   - 阅读 [configs/dreamcraft3d-coarse-nerf.yaml:L141-L145](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L141-L145)（optimizer 段，只有全局 `lr: 0.01`，没有任何 guidance 相关键）；
   - 结合 u3-l3 讲过的 `parse_optimizer` 规则回答：UNet 参数为什么天然不会被优化？
3. 需要观察的现象：optimizer.params 未点名的模块不进参数组；即便进了，`requires_grad_(False)` 也是第二道保险。
4. 预期结果：能说出「双重冻结 = 不点名参数组 + requires_grad(False)」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 guidance 要用 `eval()` 而不是 `train()`？

**答案**：IF 的 UNet 含 GroupNorm 与可能的 dropout 类层。`eval()` 固定归一化统计、关闭随机性，保证同一步内 cond/uncond 两次预测的差异只来自文本条件，而不来自网络的随机行为；否则 CFG 差分会被噪声污染。

**练习 2**：`enable_memory_efficient_attention` 默认是 False，且代码里连着三个警告分支，这是为什么？

**答案**：见 [threestudio/models/guidance/deep_floyd_guidance.py:L77-L90](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/deep_floyd_guidance.py#L77-L90) 的 FIXME 注释：DeepFloyd 与 xformers 存在已知兼容问题（代码贴了 IF 仓库 issue 链接），所以默认关闭这个通常能省显存的开关。

### 4.2 SDS 的数学：从得分蒸馏到加权差分梯度

#### 4.2.1 概念说明

SDS 要回答的问题是：渲染图 \(x_0\)（此处为 64×64 的场景渲染）该如何修改，才能更像扩散模型眼中的「真实图像」？DreamFusion 构造了一个不需要训练扩散模型本身的代理损失。

#### 4.2.2 核心流程与推导

对可微的渲染图 \(x_0\)，随机采 \(t\) 与 \(\epsilon\)，加噪得 \(x_t\)，定义概率蒸馏损失：

\[ \mathcal{L}_\text{SDS}(x_0) = \mathbb{E}_{t,\epsilon}\left[\frac{w(t)}{2}\,\big\|\hat\epsilon(x_t; t, y) - \epsilon\big\|^2\right] \]

其中 \(\hat\epsilon\) 是 CFG 融合后的噪声预测。对 \(x_0\) 求梯度（链式法则经过 \(x_t\)，\(\partial x_t/\partial x_0 = \sqrt{\bar\alpha_t}I\)，DreamFusion 把这个常数并入权重）：

\[ \nabla_{x_0}\mathcal{L}_\text{SDS} \;\propto\; w(t)\,\big(\hat\epsilon - \epsilon\big) \]

直觉解读：

- \(\epsilon\) 是我们亲手加进去的噪声，「标准答案」；
- \(\hat\epsilon\) 是裁判（冻结 UNet）对这张渲染图的「批改」；
- 两者之差指出渲染图偏离数据流形的方向，沿负梯度方向更新场景，就是「把渲染图往扩散模型认为真实的地方推」。

代入 CFG 展开，就得到本讲学习目标里的加权差分形式：

\[ \nabla_{x_0}\mathcal{L} \propto w(t)\big[(s+1)\,\epsilon_\text{cond}(x_t) - s\,\epsilon_\text{uncond}(x_t) - \epsilon\big] \]

（perp-neg 分支的基底略有不同，见 4.5。）权重取 \(w(t) = 1-\bar\alpha_t = \sigma_t^2\)，即 `weighting_strategy: "sds"`；代码还提供 `uniform`（\(w=1\)）与 `fantasia3d`（\(w=\sqrt{\bar\alpha_t}(1-\bar\alpha_t)\)）两种备选。

#### 4.2.3 源码精读

CFG 加权噪声预测（非 perp-neg 主路径）：

- [threestudio/models/guidance/deep_floyd_guidance.py:L270-L275](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/deep_floyd_guidance.py#L270-L275)：`noise_pred_text, noise_pred_uncond = noise_pred.chunk(2)` 拆出 cond/uncond 两半（各先 `split(3, dim=1)` 丢掉方差通道），随后 `noise_pred = noise_pred_text + guidance_scale * (noise_pred_text - noise_pred_uncond)`。注意这与 2.2 节公式相差一个基底：等价于 \(\epsilon_\text{uncond} + (1+s)\,(\epsilon_\text{cond}-\epsilon_\text{uncond})\)，放大倍数等效为 \(1+s\)。

权重 \(w(t)\) 的三种策略：

- [threestudio/models/guidance/deep_floyd_guidance.py:L287-L297](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/deep_floyd_guidance.py#L287-L297)：`"sds"` 时 `w = (1 - self.alphas[t])`——按 batch 内每个样本自己的 \(t\) 从 \(\bar\alpha\) 表里取值，reshape 成 `(B,1,1,1)` 以便广播到 `(B,3,64,64)`。

梯度的形成：

- [threestudio/models/guidance/deep_floyd_guidance.py:L299-L303](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/deep_floyd_guidance.py#L299-L303)：`grad = w * (noise_pred - noise)`，这正是公式 \(\nabla_{x_0}\mathcal{L} \propto w(t)(\hat\epsilon-\epsilon)\) 的逐元素实现；`torch.nan_to_num` 兜底数值异常，`grad_clip` 默认未启用（配置未设）。

系统侧的消费：`loss_sd` 如何进入总损失：

- [threestudio/systems/dreamcraft3d.py:L196-L207](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L196-L207)：guidance 子步里把渲染图（coarse 阶段为 `out["comp_rgb"]`）与相机 batch 一起喂给 `self.guidance(...)`；返回 dict 中以 `loss_` 开头的项被 `set_loss` 收编为 `loss_guidance_sd`，最终乘 `lambda_sd: 0.1`（[configs/dreamcraft3d-coarse-nerf.yaml:L126](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L126)）。

#### 4.2.4 代码实践

**实践：用 20 行脚本验证「重参数化梯度注入」的数学等价性（CPU 即可运行，不需要 GPU 与权重）。**

1. 实践目标：证明 `loss_sd` 对 `latents` 的 autograd 梯度恰好等于手工构造的 `grad / batch_size`——这是 SDS 能把外生梯度接入训练的关键一环。
2. 操作步骤：新建 `sd_grad_check.py`（放在仓库任意位置均可，属示例代码，不是项目源码）：

   ```python
   # 示例代码：验证重参数化梯度注入
   import torch
   import torch.nn.functional as F

   torch.manual_seed(0)
   B, C, H, W = 1, 3, 64, 64
   latents = torch.randn(B, C, H, W, requires_grad=True)   # 扮演渲染图缩放结果
   noise = torch.randn(B, C, H, W)                          # 亲手加的噪声（答案）
   noise_pred = torch.randn(B, C, H, W)                     # 扮演 UNet 输出（no_grad 常数）
   w = torch.rand(B, 1, 1, 1)                               # 扮演 1 - alpha_t

   grad = w * (noise_pred - noise)                          # deep_floyd_guidance.py L299
   target = (latents - grad).detach()                       # L307：外生梯度封进常数目标
   loss_sd = 0.5 * F.mse_loss(latents, target, reduction="sum") / B  # L309

   loss_sd.backward()
   print("autograd 梯度与 grad/B 的最大误差:",
         (latents.grad - grad / B).abs().max().item())
   ```

3. 需要观察的现象：输出的最大误差应为 `0.0`（浮点误差量级）。
4. 预期结果：`d(loss_sd)/d(latents) = (latents - target)/B = grad/B`，即 autograd 算出的梯度与手工 SDS 梯度方向完全一致，仅差一个正的 `1/B` 缩放（B=1 时连缩放都没有）。这个缩放被 `lambda_sd` 等权重吸收。

#### 4.2.5 小练习与答案

**练习 1**：为什么 UNet 前向要包在 `torch.no_grad()` 里？梯度是怎么「绕过」UNet 回到场景参数的？

**答案**：SDS 公式里 UNet 只需要输出数值 \(\hat\epsilon\)，不需要对 UNet 参数求导——裁判不被训练。梯度链是 `loss_sd → latents（= interpolate(rgb)）→ rgb（渲染图）→ 体渲染 → 几何/外观参数`。`target = (latents - grad).detach()` 保证损失对 `latents` 的导数恒等于 `grad/B`，与 UNet 内部无关。若不包 no_grad，autograd 会白白为 UNet 建图，显存与时间都翻倍。

**练习 2**：`weighting_strategy: "sds"` 的 \(w(t)=1-\bar\alpha_t\) 给高噪声还是低噪声的梯度更大权重？

**答案**：\(t\) 越大 \(\bar\alpha_t\) 越小，\(1-\bar\alpha_t\) 越接近 1；反之低噪声时权重趋近 0。即 sds 策略天然偏重「结构性」高噪声梯度、抑制「细节性」低噪声梯度，与粗阶段先抓大形的目标一致。`fantasia3d` 策略多乘 \(\sqrt{\bar\alpha_t}\)，进一步压低两端的权重，形状居中。

### 4.3 `__call__` 主流程精读：从渲染图到 loss_sd

#### 4.3.1 概念说明

`__call__` 是 guidance 的总入口，把 4.2 的数学落成一条流水线。理解它最好的方式是盯住每个张量的形状与「是否在 autograd 图内」这两个属性。

#### 4.3.2 核心流程

以 coarse 配置（B=1、perp-neg 开启、`view_dependent_prompting=true`）为例：

```text
输入 rgb (B,H,W,3)，值域 [0,1]，带梯度
 1. permute → (B,3,H,W)；×2−1 缩放到 [−1,1]（扩散模型的值域）
 2. bilinear 下采样到 (B,3,64,64) —— 这就是 x_0，记作 latents
 3. 采 t：time_prior 未配置时 t ~ randint[min_step, max_step]（区间由 4.4 调度）
 4. 取文本嵌入：perp-neg 时 (4B,77,4096) + 负向权重 (B,2)；否则 (2B,77,4096)
    [u7-l1：cond 在前 uncond 在后；嵌入随方位角切换方向模板]
 5. no_grad 内：noise ~ N(0,I)；latents_noisy = add_noise(latents, noise, t)
    [mask 存在时仅 mask 区域加噪——coarse 阶段 mask=None，不触发]
 6. no_grad 内：latents 复制 4 份（perp-neg）或 2 份，一次 UNet 前向
 7. 拆通道：6 通道 → 3 噪声 + 3 方差；CFG/perp-neg 融合 → noise_pred
 8. w(t) = 1 − ᾱ_t；grad = w·(noise_pred − noise)；nan_to_num
 9. target = (latents − grad).detach()；loss_sd = 0.5·Σ(latents−target)²/B
10. 返回 {loss_sd, grad_norm, min_step, max_step}（可选 eval 可视化）
```

梯度只在第 1、2、9 步经过可微路径；第 5、6 步全部在 no_grad 里。

#### 4.3.3 源码精读

预处理：值域对齐与分辨率对齐：

- [threestudio/models/guidance/deep_floyd_guidance.py:L171-L185](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/deep_floyd_guidance.py#L171-L185)：`rgb_BCHW = rgb.permute(0,3,1,2) * 2.0 - 1.0` 把渲染图从 [0,1] 映射到扩散模型的 [−1,1] 值域；`F.interpolate(..., (64,64))` 降到 IF 的像素空间分辨率。这两步都可微，是梯度回传的必经之路。`mask` 若非空，同样被下采样到 64×64 与之对齐（L175-L179）。L181 的 `assert rgb_as_latents == False` 声明本模型无潜空间。

加噪与 UNet 前向（perp-neg 主路径）：

- [threestudio/models/guidance/deep_floyd_guidance.py:L212-L229](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/deep_floyd_guidance.py#L212-L229)：先从 `prompt_utils` 取 perp-neg 三段式嵌入（u7-l1）；然后在 `no_grad` 内 `scheduler.add_noise(latents, noise, t)` 完成 \(x_t=\sqrt{\bar\alpha_t}x_0+\sqrt{1-\bar\alpha_t}\epsilon\)，把 `latents_noisy` 复制 4 份（pos/uncond/两条负向）拼成 `(4B,3,64,64)`，一次 `forward_unet` 得 `(4B,6,64,64)`。时间步也 `torch.cat([t]*4)` 对齐。

按语义拆分四组预测：

- [threestudio/models/guidance/deep_floyd_guidance.py:L231-L235](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/deep_floyd_guidance.py#L231-L235)：前 B 行是 text（正条件），中间 B 行是 uncond，后 2B 行是两条负向；每个都 `.split(3, dim=1)` 丢掉方差通道只留噪声预测。

perp-neg 融合公式（细节在 4.5 展开）：

- [threestudio/models/guidance/deep_floyd_guidance.py:L237-L248](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/deep_floyd_guidance.py#L237-L248)：`e_pos = noise_pred_text - noise_pred_uncond`，负向差分投影到 e_pos 的正交补后加权累加，最终 `noise_pred = noise_pred_uncond + guidance_scale * (e_pos + accum_grad)`。

被弃用的 SpecifyGradient 与实际采用的重参数化：

- [threestudio/utils/ops.py:L57-L72](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/ops.py#L57-L72)：`SpecifyGradient` 是上游 stable-dreamfusion 的自定义 autograd Function——forward 返回哑元 1、backward 直接吐出外生梯度。它的坑写在注释里：哑元会被 AMP 的梯度缩放器改写，需要额外乘 `grad_scale` 补偿。
- [threestudio/models/guidance/deep_floyd_guidance.py:L305-L309](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/deep_floyd_guidance.py#L305-L309)：本项目改用重参数化技巧：`target = (latents - grad).detach()`，`loss_sd = 0.5 * F.mse_loss(latents, target, reduction="sum") / batch_size`。注释直接写出恒等式 `d(loss)/d(latents) = grad`。一个普通的 MSE 损失就能注入任意外生梯度，天然兼容 AMP，无需自定义 Function。

返回值契约：

- [threestudio/models/guidance/deep_floyd_guidance.py:L311-L316](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/deep_floyd_guidance.py#L311-L316)：返回 `{"loss_sd", "grad_norm", "min_step", "max_step"}`。对照 [threestudio/systems/dreamcraft3d.py:L204-L207](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L204-L207)：所有值先被 `self.log` 记进 TensorBoard（所以你能直接画出 `train/grad_norm` 曲线），`loss_` 前缀的项再进损失表。

可选的评估可视化：

- [threestudio/models/guidance/deep_floyd_guidance.py:L337-L355](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/deep_floyd_guidance.py#L337-L355)：`guidance_eval=True` 时额外做一次「从当前噪声水平继续完整去噪」的滚动画（实现在 L414-L488 的 `guidance_eval`，其中一步去噪复用 L359-L412 的 `get_noise_pred`），产出 `imgs_1step / imgs_1orig / imgs_final` 供诊断梯度质量。coarse 配置里 `freq.guidance_eval: 0`，此路径默认关闭。

#### 4.3.4 代码实践

**实践：为 SDS 分支编写张量形状与梯度注释表（源码阅读型，不需要跑训练）。**

1. 实践目标：把 4.3.2 的流程表落到真实代码行上，做到「合上讲义也能说出每个张量的形状与是否带梯度」。
2. 操作步骤：
   - 打开 [threestudio/models/guidance/deep_floyd_guidance.py:L158-L316](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/deep_floyd_guidance.py#L158-L316)，在自己的笔记里抄下 `__call__` 的非 perp-neg 分支（L249-L309，最短路径），逐行标注两列：`形状`（以 B=1、H=W=128 渲染输入为例）与 `是否在 autograd 图内`；
   - 参考答案示例行：`latents = F.interpolate(rgb_BCHW, (64,64))` → 形状 `(1,3,64,64)`，在图内；`noise_pred = self.forward_unet(...)` → `(2,6,64,64)`，no_grad 内，不在图内；`grad = w * (noise_pred - noise)` → `(1,3,64,64)`，不在图内（纯数值）；`target = (latents - grad).detach()` → `(1,3,64,64)`，常数；`loss_sd` → 标量，对 latents 可微。
3. 需要观察的现象：整条路径上「在图内」的张量只有 `rgb_BCHW → latents → loss_sd` 这一条线，其余全部 detach。
4. 预期结果：得到一张 10 行左右的注释表；能据此向别人解释「SDS 的梯度不穿过 UNet」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `latents_noisy` 要在 no_grad 内构造，而 `latents` 本身必须在图内？

**答案**：`latents_noisy` 只是喂给裁判的「考卷」，SDS 公式不对 \(x_t\) 求导（对 \(x_0\) 求导时 \(\partial x_t/\partial x_0\) 的 \(\sqrt{\bar\alpha_t}\) 已并入权重）。`latents`（即 \(x_0\)）则是梯度的落点：损失对它的导数就是外生梯度，再沿 interpolate 与渲染链回传。

**练习 2**：mask 参数（L166、L175-L179、L222-L223、L259-L260）在 coarse 阶段会生效吗？

**答案**：不会。调用处 [threestudio/systems/dreamcraft3d.py:L202](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L202) 写的是 `mask=out["mask"] if "mask" in out else None`，而 coarse 阶段的 nerf-volume-renderer 输出中没有 `mask` 键（那是 u5-l4 讲过的 nvdiff 渲染器在 texture 阶段提供的参考视角不可见区域 mask）。代码保留此分支是为了同一 guidance 类能服务多阶段。

### 4.4 时间步区间调度：min/max_step_percent 与 update_step

#### 4.4.1 概念说明

采哪个 \(t\) 直接决定梯度的「性质」：低 \(t\)（接近 0）时输入几乎无噪，\(\hat\epsilon\) 对高频细节敏感，梯度噪声大、易碎；高 \(t\)（接近 1000）时信号几乎被淹没，梯度缺乏针对性。DreamFusion 的经验是只采中间区间，并且**让区间随训练进度移动**：早期几何还是一团雾，用偏高的噪声区间（对模糊形状宽容，先拉出轮廓）；随后降到中低区间（聚焦结构与外观）。

#### 4.4.2 核心流程

```text
每步 guidance.update_step（u3-l2 的 Updateable 钩子）
 ├─ grad_clip 若配置 → C() 插值出本步的裁剪值（coarse 未启用）
 └─ set_min_max_steps(
       min_step_percent=C(cfg.min_step_percent, ...),   # [0, 0.7, 0.2, 200]
       max_step_percent=C(cfg.max_step_percent, ...))   # [0, 0.85, 0.5, 200]
     → min_step = int(1000 × pct_min)，max_step = int(1000 × pct_max)

__call__ 内：t ~ randint[min_step, max_step + 1)
```

coarse 配置下区间的演化（T=1000）：

| 训练步 | min_step_percent | max_step_percent | t 采样区间 |
| --- | --- | --- | --- |
| 0 | 0.70 | 0.85 | [700, 850] |
| 100（线性中点） | 0.45 | 0.675 | [450, 675] |
| ≥ 200（稳定值） | 0.20 | 0.50 | [200, 500] |

即开局用 [0.7, 0.85] 的高噪声看大形，200 步内线性收缩到 [0.2, 0.5] 的中低噪声做细化，之后保持不变。

此外 configure 里还有一个默认关闭的 `time_prior` 实验功能（[L110-L125](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/deep_floyd_guidance.py#L110-L125) 定义、[L187-L200](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/deep_floyd_guidance.py#L187-L200) 消费）：给定 `[m1, m2, s1, s2]` 构造「两端高斯衰减、中间平坦」的非均匀时间步权重，按训练进度 `current_step_ratio` 在累积分布上反查 \(t\)，让噪声水平随训练从高到低平滑滑动。coarse 配置未设置该键，走的是均匀 `randint` 分支。

#### 4.4.3 源码精读

- [threestudio/models/guidance/deep_floyd_guidance.py:L139-L142](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/deep_floyd_guidance.py#L139-L142)：`set_min_max_steps` 把百分比换算成绝对时间步下标（`int()` 截断），configure 时先用默认值调用一次（L109）。
- [threestudio/models/guidance/deep_floyd_guidance.py:L202-L210](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/deep_floyd_guidance.py#L202-L210)：默认分支 `t = torch.randint(self.min_step, self.max_step + 1, [batch_size], ...)`——注释 "timestep ~ U(0.02, 0.98) to avoid very high/low noise level" 说明了避开两端的动机；`randint` 上界开区间，所以 +1。
- [threestudio/models/guidance/deep_floyd_guidance.py:L490-L500](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/deep_floyd_guidance.py#L490-L500)：`update_step` 每个训练批次被 u3-l2 讲过的钩子调用一次，用 `C()` 重新计算区间。`grad_clip` 的注释还引用了 Debiasing Scores and Prompts（arXiv:2303.15413）——梯度裁剪用于稳定训练，coarse 配置未启用。
- [threestudio/utils/misc.py:L65-L97](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/misc.py#L65-L97)：`C()` 对 `[start_step, start_value, end_value, end_step]` 四元组做裁剪线性插值（u2-l2 讲过机制）——此处是它在 guidance 侧的消费现场。
- [configs/dreamcraft3d-coarse-nerf.yaml:L94-L99](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L94-L99)：coarse 配置的 guidance 段。注意 `guidance_3d`（Zero123，L110-L111）使用了**完全相同**的两个四元组——两条引导通道的噪声区间同步收缩，这是有意的对齐设计。

#### 4.4.4 代码实践

**实践：画出 coarse 阶段的时间步区间收缩曲线（不加载任何模型权重，秒级完成）。**

1. 实践目标：直观看到 `min/max_step_percent` 四元组如何驱动 t 采样区间随训练步演化。
2. 操作步骤：在装好 threestudio 依赖的环境里运行（示例代码）：

   ```python
   # 示例代码：复现 coarse 阶段时间步区间调度
   from threestudio.utils.misc import C

   T = 1000
   for step in [0, 50, 100, 150, 200, 300, 1000, 4000]:
       pmin = C([0, 0.7, 0.2, 200], 0, step)
       pmax = C([0, 0.85, 0.5, 200], 0, step)
       print(f"step={step:>4}  t ∈ [{int(T*pmin):>3}, {int(T*pmax):>3}]")
   ```

3. 需要观察的现象：区间宽度从 150（步 0）收缩到 300（步 ≥200）——注意下界降幅（700→200）远大于上界降幅（850→500），即**下界被大幅压低**，把训练重心从高噪声推向中低噪声。
4. 预期结果：输出与 4.4.2 的表格一致；若想看得更清楚可把步数取密一些并画折线。

#### 4.4.5 小练习与答案

**练习 1**：把 `min_step_percent` 与 `max_step_percent` 都固定成 `0.9` 附近会发生什么？

**答案**：\(t\) 几乎总是高噪声（约 900），\(\hat\epsilon\) 只携带「全局形状像不像」的信息，细节梯度被 `w(t)≈1` 的高噪声梯度淹没，场景会长期停留在模糊雾状、难以细化；反向固定在 `0.02` 则几乎只有细节噪声，缺乏成形方向，且低噪声梯度幅值本就小（受 \(w(t)\) 压制）。

**练习 2**：Zero123 的 `guidance_3d` 段为什么和 DeepFloyd 用同样的时间步四元组？

**答案**：两条引导在同一步上约束同一批渲染图（u6-l2 的 alternate 调度里属同一 guidance 子步先后调用），若噪声水平不一致，一边在推「大形」、另一边在推「细节」，梯度会互相打架。对齐区间让文本先验与视图先验在同一频率上协同。

### 4.5 perp-neg：垂直分解的负向引导

#### 4.5.1 概念说明

普通 CFG 只有一个负向概念（空提示）。Perp-Neg（Perpendicular Negative Guidance）允许给「不要什么」更精细的表达：例如渲染正面视角时，用 side/back 的方向嵌入做负向，避免把侧面特征混进正面。但若直接把负向差分从正向里减掉，负向分量在正向方向上的投影会意外增强或抵消正向引导，导致过饱和。解法：把负向差分**投影到正向差分的正交补**，只保留「垂直于想要方向」的惩罚分量。

#### 4.5.2 核心流程

perp-neg 在普通 CFG 的 \(2B\)（cond+uncond）之上再拼两条负向嵌入，UNet 一次前向算 \(4B\) 组预测：

```text
嵌入（u7-l1 的 get_text_embeddings_perp_neg 提供）：
  行 0..B-1      pos：按方位角在 front/side/back 嵌入间线性插值
  行 B..2B-1     uncond：空提示
  行 2B..4B-1    两条 neg：当前方向相邻的两个方向嵌入
  neg_guidance_weights: (B, 2)，负的指数衰减权重（端点归零）

融合：
  e_pos  = ε_pos − ε_uncond
  e_i    = ε_neg_i − ε_uncond
  accum  = Σ_i w_i · perp(e_i, e_pos)        # 投影到 e_pos 的正交补
  ε̂      = ε_uncond + s · (e_pos + accum)
```

投影算子（对 batch 内每个样本独立计算内积）：

\[ \mathrm{perp}(x, y) = x - \frac{\langle x, y\rangle}{\langle y, y\rangle}\, y \]

#### 4.5.3 源码精读

- [threestudio/models/prompt_processors/base.py:L80-L165](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/prompt_processors/base.py#L80-L165)：u7-l1 已精读过的 `get_text_embeddings_perp_neg`——按方位角插值正嵌入、挑选相邻方向嵌入为负、用 `shifted_expotional_decay` 生成随插值比例衰减的负权重，返回 `(4B, 77, 4096)` 嵌入与 `(B, 2)` 权重。
- [threestudio/utils/ops.py:L440-L450](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/ops.py#L440-L450)：`perpendicular_component(x, y)` 的实现——分子分母按 `dim=[1,2,3]` 对每个样本整张特征图求内积（L446-L447），`torch.maximum(..., eps)` 防零除，最后 `x - proj * y`。就是上面投影公式的一行翻译。
- [threestudio/models/guidance/deep_floyd_guidance.py:L237-L248](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/deep_floyd_guidance.py#L237-L248)：主融合循环。`n_negative_prompts = 2`，`noise_pred_neg[i::2]` 用切片交错取出两条负向各自的 B 行（因为 4B 布局是按样本优先排列的），权重 `neg_guidance_weights[:, i].view(-1,1,1,1)` 广播到特征图。最终以 **uncond 为基底**加 `s·(e_pos + accum)`——与非 perp-neg 分支（以 text 为基底）不同，这里正方向已经单独放进 `s·e_pos`，基底选 uncond 避免重复叠加正向分量。
- [threestudio/models/guidance/deep_floyd_guidance.py:L359-L412](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/deep_floyd_guidance.py#L359-L412)：`get_noise_pred` 把同样的融合逻辑复制了一份，专供 `guidance_eval` 的多步去噪循环复用（每步都要重算 CFG）。两处代码几乎逐行相同，是「为可视化保留的副本」。
- [configs/dreamcraft3d-coarse-nerf.yaml:L88-L92](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L88-L92)：`prompt_processor` 段的 `use_perp_neg: true` 打开整条链路；texture 阶段（BSD 用 SD2.1）则关闭。

#### 4.5.4 代码实践

**实践：用数值实验验证垂直投影不改变正向分量（CPU 可跑的示例代码）。**

1. 实践目标：确认 `perp(e_neg, e_pos)` 与 `e_pos` 的内积为 0，即负向惩罚确实完全不干扰正向方向。
2. 操作步骤：

   ```python
   # 示例代码：验证 perpendicular_component 的正交性
   import torch
   from threestudio.utils.ops import perpendicular_component

   torch.manual_seed(0)
   e_pos = torch.randn(2, 3, 64, 64)   # 两个样本的正向差分
   e_neg = torch.randn(2, 3, 64, 64)   # 一条负向差分
   perp = perpendicular_component(e_neg, e_pos)
   inner = (perp * e_pos).sum(dim=[1, 2, 3])   # 每个样本的内积
   print("与 e_pos 的内积（应为 0）:", inner.abs().max().item())
   print("被去掉的投影模长占比:",
         (1 - perp.flatten(1).norm(dim=1) / e_neg.flatten(1).norm(dim=1)).tolist())
   ```

3. 需要观察的现象：内积绝对值在 1e-6 量级（防零除 eps 引起的误差）；被去掉的比例等于 \(|\cos\theta|\)，即负向差分与正向夹角越小、被砍掉的越多。
4. 预期结果：正交性成立 → `s·accum` 只提供「偏离正轨方向」的推力，正轨上的推进完全由 `s·e_pos` 决定。

#### 4.5.5 小练习与答案

**练习 1**：为什么负向权重要用指数衰减并在端点归零？

**答案**：见 [threestudio/models/prompt_processors/base.py:L137-L152](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/prompt_processors/base.py#L137-L152)。方位角恰好对准某方向（插值比例 r 到端点）时，相邻方向嵌入本身就是「当前不想要的极端」，若权重不为零会惩罚到当前视角的正确内容；指数衰减让越接近端点惩罚越弱，端点处恰好为零。

**练习 2**：perp-neg 与 `guidance_scale` 各自放大的是什么？

**答案**：`guidance_scale`（s）整体缩放 `(e_pos + accum)`——同时放大正向推进与正交惩罚，是梯度的「步长旋钮」；perp-neg 决定惩罚的**方向构成**（只保留垂直分量），不改变其来源。s 过大时两者一起被放大，典型症状是颜色过饱和与伪影。

## 5. 综合实践

**guidance_scale 扫参对比实验：观察文本先验强度对粗阶段几何演化的影响。**

本综合实践把本讲四条线索（CFG 放大、SDS 梯度、时间步区间、perp-neg）串成一次对照实验。前提：已按 u1-l2 完成环境安装并下载 DeepFloyd IF 权重（gated 模型，需 HuggingFace 登录）。

1. 基线准备：确认 [configs/dreamcraft3d-coarse-nerf.yaml:L94-L99](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L94-L99) 中 `guidance_scale: 20`，用 u2-l1 预处理过的示例图（或自备 RGBA 图）与配套 prompt。
2. 三组短程训练（用命令行覆盖避免改配置文件，max_steps 压到 500 以内观察前期差异）：

   ```bash
   python launch.py --config configs/dreamcraft3d-coarse-nerf.yaml --train \
       system.prompt_processor.prompt="<你的提示词>" \
       system.guidance.guidance_scale=5  trainer.max_steps=500 tag=gs5

   python launch.py --config configs/dreamcraft3d-coarse-nerf.yaml --train \
       system.prompt_processor.prompt="<你的提示词>" \
       system.guidance.guidance_scale=20 trainer.max_steps=500 tag=gs20

   python launch.py --config configs/dreamcraft3d-coarse-nerf.yaml --train \
       system.prompt_processor.prompt="<你的提示词>" \
       system.guidance.guidance_scale=50 trainer.max_steps=500 tag=gs50
   ```

3. 观察三个渠道：
   - `outputs/dreamcraft3d-coarse-nerf/gs*/save/` 下的 `itXXX-rgb.png` 序列：轮廓从雾状成形的速度；
   - TensorBoard（`tb_logs`）中的 `train/grad_norm` 曲线：三组的量级与波动；
   - `train/loss_guidance_sd` 与参考图损失 `train/loss_ref_rgb` 的相对走势（注意 alternate 调度下两种损失不同步出现，u6-l2）。
4. 记录一张三列对比表：成形步数 / 颜色饱和度 / 伪影情况。
5. 预期结果（**待本地验证**，以下为基于机制的推断）：s=5 时条件项 \((1+s)\) 倍差分被削弱，几何主要靠 Zero123 视图先验与参考图损失拉动，文本语义成形慢；s=50 时成形快但梯度幅值大（对照 `grad_norm`），易出现过饱和色块与高频噪声伪影。把观察结果写回你的对比表，并与 `lambda_sd: 0.1`（[configs/dreamcraft3d-coarse-nerf.yaml:L126](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L126)）联动思考：`guidance_scale` 与 `lambda_sd` 都能放大文本梯度，一个作用在差分合成处、一个作用在损失加权处，实验中可尝试 `s=50, lambda_sd=0.025` 与 `s=20, lambda_sd=0.1` 是否近似等价。

## 6. 本讲小结

- DeepFloyd IF 是像素空间扩散模型：渲染图缩放到 64×64 即扩散输入，UNet 输出 6 通道（3 噪声 + 3 方差），蒸馏只用前 3 通道。
- SDS 梯度 \(\nabla_{x_0}\mathcal{L} \propto w(t)(\hat\epsilon - \epsilon)\) 在代码中就是 `grad = w * (noise_pred - noise)`，其中 `noise_pred` 是 CFG 融合后的噪声预测，\(w(t)=1-\bar\alpha_t\)。
- UNet 前向整体在 no_grad 内——裁判只出数值不出梯度；外生梯度经 `target = (latents - grad).detach()` 的 MSE 重参数化注入 autograd 图，天然兼容 AMP，替代了上游的 `SpecifyGradient` 自定义 Function。
- 时间步区间由 `min/max_step_percent` 四元组经 `C()` 插值驱动、`update_step` 每步刷新：coarse 阶段从 [700, 850] 线性收缩到 [200, 500]，先大形后细节，且与 Zero123 通道严格对齐。
- perp-neg 把负向差分投影到正向差分的正交补（`perpendicular_component`），配合端点归零的指数衰减权重，惩罚「偏离方向」而不侵蚀「目标方向」。
- `guidance_scale` 是文本先验的步长旋钮：默认 20，过大过饱和、过小语义成形慢；它作用在差分合成处，与作用在损失加权处的 `lambda_sd` 是两个不同的杠杆。

## 7. 下一步学习建议

本讲讲完了双引导中的文本通道。下一讲 **u7-l3（Stable Zero123：视图条件 3D 先验）** 将解读另一半：`stable-zero123-guidance` 如何用参考图的 CLIP/CCIP 嵌入与相对相机姿态作为条件提供多视角一致性——特别注意它复用了本讲的 SDS 骨架（同样的加噪、同样的 `w(t)`、同样的重参数化注入），新增的是条件构造方式，读起来会比本讲快得多。之后再进入 u7-l4/u7-l5 的 BSD 引导（把扩散模型本身也变成可训练对象），届时可回头对比：本讲的 UNet 是 `requires_grad_(False)` 的冻结裁判，BSD 里它却成了运动员。

延伸阅读建议：DeepFusion 论文的第 5 节（score distillation sampling 推导）与 Perp-Neg 论文（arXiv:2304.15706）；代码层面可浏览 [threestudio/models/guidance/deep_floyd_guidance.py:L414-L488](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/deep_floyd_guidance.py#L414-L488) 的 `guidance_eval`——把 `freq.guidance_eval` 设为正整数即可在训练中看到「当前噪声水平下继续去噪」的动态诊断图。

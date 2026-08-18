# u7-l4 BSD 引导（上）：三管线结构与 LoRA 装配

## 1. 本讲目标

本讲聚焦 DreamCraft3D 纹理阶段的"大脑"——`stable-diffusion-bsd-guidance`（BSD，Bootstrapped Score Distillation，自举得分蒸馏）的**静态结构**。学完本讲，你应当能够：

1. 画出 BSD 引导的完整模块结构图：`pipe` / `pipe_lora` / `pipe_fix` 三条管线、`train_unet` / `train_unet_lora` 两个可训练 UNet、`camera_embedding` 相机条件嵌入，并标注每个子模块的 `requires_grad` 状态。
2. 解释清楚"三条管线"其实只有两份权重——`pipe_fix` 是 `pipe` 的别名，并能说出这种别名带来的隐蔽副作用。
3. 读懂 `set_up_lora_layers` 如何用 `LoRAAttnProcessor` 逐个替换注意力处理器，并理解它在发布版本中处于"被注释的休眠状态"这一关键事实。
4. 理解相机条件嵌入（16 维 → 1280 维的 `TimestepEmbedding`）的设计意图，以及它与 u7-l1 讲过的视角相关文本提示是如何"一实一虚"互相配合的。

本讲只讲"装配"，不讲"运转"。VSD 梯度推导、`train_lora` / `train_pretrain` 的交替优化细节留给下一讲（u7-l5）。

## 2. 前置知识

### 2.1 BSD 回顾：为什么需要"好几份"扩散模型

u1-l1 与 u2-l3 已建立全局图景：texture 阶段不再用 frozen 的 DeepFloyd/Zero123 做 SDS，而是把扩散模型本身也纳入优化——用场景自己的渲染图去 DreamBooth 式地训练一个"个性化"扩散模型，再用它蒸馏回场景。这类方法（ProlificDreamer 的 VSD、DreamCraft3D 的 BSD）的得分梯度需要**两个会随场景进化的噪声预测器相减**：

\[ \nabla_{x}\mathcal{L} \;=\; w(t)\,\bigl(\hat{\epsilon}_{\text{pretrain}}(x_t) \;-\; \hat{\epsilon}_{\text{lora}}(x_t)\bigr), \qquad w(t)=1-\bar{\alpha}_t \]

- \(\hat{\epsilon}_{\text{pretrain}}\)：被参考图"个性化"过的教师模型（对应 `train_unet`）；
- \(\hat{\epsilon}_{\text{lora}}\)：贴合当前渲染分布的得分估计器（对应 `train_unet_lora`，名字承自 ProlificDreamer 的 LoRA）。

既然要"两个都在动的模型 + 若干负责采样/编码的冻结模型"，显存里就必然同时驻留多份 SD 级网络——这正是本讲要拆解的"三管线 + 双可训练 UNet"结构的由来。

### 2.2 diffusers 组件速览

- `StableDiffusionPipeline`：文生图完整管线，内部含 `unet`、`vae`、`text_encoder`、`scheduler` 等子模块，可以单独取用。
- `UNet2DConditionModel`：SD 的去噪主干，可经 `subfolder="unet"` 单独加载；接受 `class_labels` 作为附加条件通道（内部有 `class_embedding` 分支，默认未启用）。
- 注意力处理器（attention processor）：diffusers 把每个注意力块的计算封装成可替换的 processor 对象，`unet.attn_processors` 是"名字 → processor"的字典；LoRA 微调的官方姿势就是把这些 processor 换成 `LoRAAttnProcessor`。
- `TimestepEmbedding`：一个两层的 MLP（`in_dim → time_embed_dim → time_embed_dim`），SD 内部用来把时间步标量变成 1280 维向量。
- 仓库锁定 `diffusers<=0.23.0`（[requirements.txt:5](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/requirements.txt#L5)），本讲涉及的 `LoRAAttnProcessor`、`AttnProcsLayers`、`TimestepEmbedding` 都从这个版本区间的公开路径导入（见源码顶部 import）。

### 2.3 LoRA 一句话原理

低秩适配（LoRA）冻结原权重 \(W\)，只训练一对小矩阵 \(B\in\mathbb{R}^{d\times r},\,A\in\mathbb{R}^{r\times d'}\)：

\[ W' = W + \frac{\alpha}{r}BA, \qquad r \ll \min(d, d') \]

diffusers 的 `LoRAAttnProcessor` 默认 \(r=4\)，把 q/k/v/out 四个投影各配一对低秩矩阵。可训练参数量只有原矩阵的百分之几——这是它省显存的原因，也是后文"发布版本反而弃用了它"这一反直觉事实的背景。

### 2.4 承接前几讲

- u7-l1：prompt processor 在 configure 阶段把文本预编码为嵌入（`view_dependent_prompting` 开/关两种取法），所以本讲的管线可以**删掉 text_encoder**。
- u7-l2：SDS 梯度的"外生注入"手法（`target = (latents - grad).detach()` 重参数化），本讲 4.3 会再次见到它的调用位置。
- u7-l3：Zero123 用 `cc_projection` 把相机条件缝进 CLIP 嵌入；本讲的 `camera_embedding` 是同一个思想在 SD UNet 上的翻版。
- u3-l3：`parse_optimizer` 用点号路径（如 `guidance.train_unet`）把 yaml 配置映射到参数组——两个可训练 UNet 就是这样进优化器的。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [threestudio/models/guidance/stable_diffusion_bsd_guidance.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py) | 本讲主角，1134 行，注册名 `stable-diffusion-bsd-guidance` |
| [configs/dreamcraft3d-texture.yaml](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml) | texture 阶段配置：guidance 参数、三损失权重、优化器分组 |
| [threestudio/systems/dreamcraft3d.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py) | 消费侧：把 `loss_sd`/`loss_lora`/`loss_pretrain` 加权进总损失 |
| [threestudio/systems/utils.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/utils.py) | `parse_optimizer`/`getattr_recursive`：点号路径 → 参数组 |
| [threestudio/utils/base.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/base.py) | `BaseModule` 生命周期（u3-l2 已讲），`configure` 在构造时被调用 |

## 4. 核心概念与源码讲解

### 4.1 BSD 引导全景：Config 与 configure 组装流水线

#### 4.1.1 概念说明

BSD 引导是全项目最重的组件：一次性在显存里装配 **4 份 SD 级 UNet + 1 份共享 VAE + 4 个调度器**。理解它的第一步不是读某个函数，而是把 `configure()`（u3-l2 讲过的生命周期钩子，构造对象时自动执行）的组装顺序理成一张账本。`Config` 数据类则提前告诉我们设计者预留了哪些旋钮。

#### 4.1.2 核心流程

`configure()` 的组装顺序（伪代码）：

```text
configure()
 ├─ weights_dtype = fp16（half_precision_weights 默认 True）
 ├─ pipe   ← from_pretrained(SD2.1-base)          # 冻结采样/编码用
 ├─ pipe_lora ← from_pretrained(lora 路径)          # LoRA 通道
 │    └─ 删除其 VAE，改挂 pipe.vae（共享，省一份 VAE）
 ├─ pipe_fix = pipe                                # ★别名，不是第三份权重
 ├─ 删除两条管线的 text_encoder（嵌入由 prompt processor 预计算）
 ├─ 冻结 vae / vae_fix / unet_fix
 ├─ camera_embedding = TimestepEmbedding(16 → 1280) # 挂载点被注释（休眠）
 ├─ （注释状态）set_up_lora_layers ×2 + AttnProcsLayers ×2
 ├─ train_unet、train_unet_lora ← 全新加载两个 UNet，requires_grad 全开
 ├─ 4 个调度器：scheduler / scheduler_lora（DDPM，训练）
 │              scheduler_sample / scheduler_lora_sample（DPMSolver，采样）
 └─ 记录 alphas_cumprod，按 min/max_step_percent 设定时间步区间
```

#### 4.1.3 源码精读

**Config 数据类**把旋钮分成五组：[stable_diffusion_bsd_guidance.py:40-73](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L40-L73)

```python
pretrained_model_name_or_path: str = "stabilityai/stable-diffusion-2-1-base"
pretrained_model_name_or_path_lora: str = "stabilityai/stable-diffusion-2-1"
guidance_scale: float = 7.5
guidance_scale_lora: float = 1.0
...
camera_condition_type: str = "extrinsics"
...
per_update_pretrain_step: int = 25
only_pretrain_step: int = 1000
```

注意两个模型路径默认**不是同一个**（base 与 768 分辨率的 v-prediction 版），这在后文 `scheduler_lora` 的 v_prediction 分支里留下了伏笔。

**texture 配置只覆盖少数旋钮**：[configs/dreamcraft3d-texture.yaml:78-86](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L78-L86)——两个路径都被钉死为 SD2.1-base，`guidance_scale` 压到 2.0，`max_step_percent` 用 C() 四元组 `[0, 0.5, 0.2, 5000]` 随训练从 0.5 收缩到 0.2（呼应 u8-l1 的调度函数），`only_pretrain_step: 1000` 驱动"每千步前 200 步只做预训练"的节律（u6-l2 讲过系统侧视角）。

**四个调度器的装配**：[stable_diffusion_bsd_guidance.py:210-236](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L210-L236)。两份 DDPM（训练加噪用，各自跟随自己的模型路径，因此噪声调度可不同）+ 两份 DPMSolverMultistep（25 步快速采样用），随后把它们回填进对应管线的 `scheduler` 槽位。`pipe_fix.scheduler = self.scheduler` 这一行再次暴露 `pipe_fix` 与 `pipe` 共享一切。

**显存账本（粗略估算，待本地验证）**：SD2.1-base UNet 约 8.65 亿参数，fp16 约 1.7 GB/份。4 份 UNet ≈ 6.9 GB，共享 VAE 约 0.1 GB；`train_unet` 与 `train_unet_lora` 各需梯度（fp16，约 1.7 GB）+ AdamW 两份动量状态（约 3.4 GB），静态合计约 17 GB，再叠加 1024 分辨率的激活与 VAE 解码——这就是 texture 阶段成为全流水线显存峰值的原因。

#### 4.1.4 代码实践

1. **目标**：用配置解析验证你对"默认值 vs texture 覆盖值"的理解。
2. **步骤**：写一个 10 行脚本（示例代码），加载 texture 配置后打印 guidance 段：

```python
# 示例代码：仅解析配置，无需 GPU
from omegaconf import OmegaConf
cfg = OmegaConf.load("configs/dreamcraft3d-texture.yaml")
g = cfg.system.guidance
for k in ["pretrained_model_name_or_path", "pretrained_model_name_or_path_lora",
          "guidance_scale", "guidance_scale_lora", "half_precision_weights",
          "only_pretrain_step", "per_update_pretrain_step", "camera_condition_type"]:
    print(f"{k:40s} {g.get(k, '<未覆盖,用默认值>')}")
```

3. **观察**：哪些键在 yaml 里没写（如 `guidance_scale_lora`、`only_pretrain_step` 之外的 LoRA 旋钮）。
4. **预期结果**：`guidance_scale_lora` 显示"未覆盖"→ 实际生效 1.0；两个模型路径相同；`half_precision_weights` 未覆盖 → True（fp16）。本脚本只读 yaml，可直接运行。

#### 4.1.5 小练习与答案

**练习 1**：`Config` 里 `min_step_percent` 是 float、`max_step_percent` 却被 texture 配置写成四元组列表，为什么不冲突？
**答案**：数据类声明的 `float` 只是 OmegaConf `parse_structured` 的宽松起点；yaml 覆盖后类型变为 list，实际消费它的是 `update_step` 里的 `C()`（[stable_diffusion_bsd_guidance.py:1131-1133](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L1131-L1133)），C() 对标量与四元组都能做插值（u8-l1 详述）。这也说明该实现依赖运行期鸭子类型而非严格 schema。

**练习 2**：为什么需要 4 个调度器而不是 1 个？
**答案**：两个模型路径可能对应不同噪声调度（SD2.1-base 是 epsilon 预测、SD2.1 是 v-prediction），训练加噪必须各用各的 DDPM（`scheduler` / `scheduler_lora`）；采样（`_sample` 的 25 步去噪）想要快，则各自换 DPMSolver（`scheduler_sample` / `scheduler_lora_sample`）。四者职责：同一条管线的"训练态"与"采样态"分开配置。

### 4.2 三条管线 pipe / pipe_lora / pipe_fix：加载、共享与别名陷阱

#### 4.2.1 概念说明

源码里出现三个"管线"名字，但**物理上只有两份权重**。`SubModules` 数据类把它们并排装在一起；`pipe_fix = pipe` 这行赋值使第三条管线只是第一条的 Python 别名。理解这一点是避免被属性名误导的关键——`unet_fix`（冻结）与 `unet`（可训练！）这对名字尤其具有迷惑性。

#### 4.2.2 核心流程

```text
加载 pipe（SD2.1-base，含 UNet+VAE）
加载 pipe_lora（第二个路径）
  └─ del pipe_lora.vae → pipe_lora.vae = pipe.vae   # VAE 共享
pipe_fix = pipe                                       # 对象别名
删除 pipe.text_encoder、pipe_lora.text_encoder        # 嵌入已预计算
冻结 pipe.vae、pipe_fix.vae（同一对象）、pipe_fix.unet
```

#### 4.2.3 源码精读

**SubModules 数据类与两次加载**：[stable_diffusion_bsd_guidance.py:106-127](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L106-L127)

```python
@dataclass
class SubModules:
    pipe: StableDiffusionPipeline
    pipe_lora: StableDiffusionPipeline
    pipe_fix: StableDiffusionPipeline
...
pipe_lora = StableDiffusionPipeline.from_pretrained(
    self.cfg.pretrained_model_name_or_path_lora, **pipe_lora_kwargs,
).to(self.device)
del pipe_lora.vae
cleanup()
pipe_lora.vae = pipe.vae
pipe_fix = pipe
```

中文说明：删除 `pipe_lora` 自带的 VAE 再把 `pipe.vae` 挂回去——VAE 只保留一份；`pipe_fix = pipe` 建立别名。

**删除文本编码器与冻结**：[stable_diffusion_bsd_guidance.py:154-165](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L154-L165)。`single_model` 在 [第 116 行](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L116) 被硬编码为 `False`，所以两条管线的 text_encoder 都被删掉——文本嵌入完全由 prompt processor 预计算（u7-l1），这也是 texture 配置要求 [prompt_processor 与 guidance 用同一模型路径](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L71-L81) 的原因（CLIP 嵌入维度必须等于 UNet 交叉注意力维度）。

**属性别名的"名字陷阱"**：[stable_diffusion_bsd_guidance.py:263-297](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L263-L297)。整理成对照表：

| 属性（L 行号） | 实际指向 | requires_grad | 进优化器？ |
| --- | --- | --- | --- |
| `pipe`（L263）/ `pipe_fix`（L287） | 同一个 `StableDiffusionPipeline` | — | 否 |
| `unet`（L271） | **`train_unet`（可训练！）** | True | 是（lr=1e-5） |
| `unet_lora`（L275） | `train_unet_lora`（可训练） | True | 是（lr=1e-5） |
| `unet_fix`（L291） | `pipe_fix.unet` = `pipe.unet` | **False**（L164 冻结） | 否 |
| `vae`（L279）/ `vae_lora`（L283）/ `vae_fix`（L295） | 同一份 `pipe.vae` | False（L159 冻结） | 否 |
| `pipe_lora.unet` | 第二份加载的 UNet | True（默认值，**从未显式冻结**） | 否 |

两点尤其值得警惕：`self.unet` 是可训练的、`self.unet_fix` 才是冻结的——与直觉相反；`pipe_lora.unet` 的冻结是"靠约定"（不进优化器、只在 `@torch.no_grad` 的采样里被调用）而非靠开关。

**别名的隐蔽副作用**：`train_pretrain` 里有一行换绑——[stable_diffusion_bsd_guidance.py:964](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L964)

```python
self.pipe.unet = self.train_unet
```

由于 `pipe_fix is pipe`，这行一旦执行（第一次预训练采样之后），`pipe_fix.unet` 也随之变成 `train_unet`——"fix（冻结）管线"这个名字从此名不副实，原冻结 UNet 对象失去引用后可被回收。也就是说：**只有第一次 `train_pretrain` 采样真正用到了冻结 UNet**，之后的"冻结采样"用的都是演化中的 `train_unet`。这是阅读本文件必须建立的别名心智模型（该机制的完整语义在 u7-l5 展开）。

#### 4.2.4 代码实践

1. **目标**：不运行代码，仅凭 L112–L127 与属性定义，推断对象身份；再写出验证断言。
2. **步骤**：先手填下表左三列，再写出断言脚本（示例代码，需已实例化 guidance 对象 `g`，待本地验证）：

```python
# 示例代码（GPU + 权重就绪后运行，待本地验证）
assert g.submodules.pipe_fix is g.submodules.pipe      # 别名
assert g.vae_lora is g.vae                              # VAE 共享
assert g.vae_fix is g.vae
assert g.unet is g.train_unet and g.unet_fix is g.pipe.unet
print("identity checks passed")
```

3. **观察**：若在 `train_pretrain` 执行前后各打印一次 `g.unet_fix is g.train_unet`，前 `False` 后 `True`。
4. **预期结果**：全部断言通过；换绑前后行为变化即上一节的别名副作用。**待本地验证**（需要下载 SD 权重并跑一次 texture 阶段的前 1000 步之一）。

#### 4.2.5 小练习与答案

**练习 1**：为什么要 `del pipe_lora.vae` 再挂 `pipe.vae`，而不是直接留着两份？
**答案**：VAE 在本组件里只做图像↔潜码互转（`encode_images`/`decode_latents`），两条管线的 VAE 权重完全相同，共享一份省下约 0.1–0.2 GB 显存，且保证两条通道的潜空间编码严格一致（同一次 `encode` 的数值可比性）。`cleanup()`（u2-l2 提到的工具）负责立刻释放被删对象。

**练习 2**：texture 配置注释里留着 `# pretrained_model_name_or_path_lora: "stabilityai/stable-diffusion-2-1"`（[configs/dreamcraft3d-texture.yaml:82](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L82)），这暗示了什么历史？
**答案**：早期实验里 LoRA 通道用 SD2.1（768、v-prediction）。这解释了代码里 `scheduler_lora.config.prediction_type == "v_prediction"` 分支（[L732-741](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L732-L741)）为何存在——v 预测要换算回 epsilon 才能与教师通道相减。发布配置改成了双 base 同路径，该分支成了"备用插座"。

### 4.3 两个可训练 UNet：train_unet 与 train_unet_lora 的职责分工

#### 4.3.1 概念说明

BSD 的核心工程决策在这里：**两个从预训练权重全新加载、全参数可训练的 UNet**。职责分工（运转细节在 u7-l5）：

- `train_unet`：**个性化教师通道**。由 `train_pretrain` 用"冻结模型采样的回灌图 + 参考视角"做 DreamBooth 式训练，把参考图的appearance 烧进权重；在 `compute_grad_vsd` 里提供 \(\hat{\epsilon}_{\text{pretrain}}\)，吃**视角相关**文本嵌入。
- `train_unet_lora`：**得分估计通道**。由 `train_lora` 在当前渲染图上训练，拟合"此刻场景长什么样"的得分；在 `compute_grad_vsd` 里提供 \(\hat{\epsilon}_{\text{lora}}\)，吃**视角无关**文本嵌入。名字里的 "lora" 是 ProlificDreamer 时代 LoRA 估计器的遗产——实现已换成全参微调（见 4.4）。

两个模型必须随场景进化，正对应 2.1 节公式里的两个动点；冻结的 `pipe.unet` 无此能力，所以不能复用。

#### 4.3.2 核心流程

```text
train_unet     ← from_pretrained(SD2.1-base, subfolder="unet")
train_unet_lora ← from_pretrained(lora 路径,   subfolder="unet")
两者：enable_xformers_memory_efficient_attention + enable_gradient_checkpointing
两者：requires_grad_(True)（全参数）
优化器：经 parse_optimizer 以 "guidance.train_unet" / "guidance.train_unet_lora"
        点号路径各自成组，lr = 1e-5
```

#### 4.3.3 源码精读

**加载与全参可训练**：[stable_diffusion_bsd_guidance.py:189-206](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L189-L206)

```python
self.train_unet = UNet2DConditionModel.from_pretrained(
    self.cfg.pretrained_model_name_or_path, subfolder="unet",
    torch_dtype=self.weights_dtype)
self.train_unet.enable_xformers_memory_efficient_attention()
self.train_unet.enable_gradient_checkpointing()
...
for p in self.train_unet.parameters():
    p.requires_grad_(True)
for p in self.train_unet_lora.parameters():
    p.requires_grad_(True)
```

中文说明：与两条冻结管线**重复加载**同样的 UNet 权重各一份，开启 xformers 注意力与梯度检查点（两项都是训练大模型的省显存手段，xformers 在 [requirements.txt:23](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/requirements.txt#L23)），然后整模型解锁梯度。

**优化器如何找到它们**：texture 配置的参数组——[configs/dreamcraft3d-texture.yaml:139-152](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L139-L152)

```yaml
params:
  geometry.encoding:        { lr: 0.01 }
  geometry.feature_network: { lr: 0.001 }
  guidance.train_unet:      { lr: 0.00001 }
  guidance.train_unet_lora: { lr: 0.00001 }
```

`parse_optimizer` 对每个键调用 `get_parameters(model, name)` → `getattr_recursive`（[threestudio/systems/utils.py:19-31](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/utils.py#L19-L31)），把 `"guidance.train_unet"` 拆成 `system.guidance → .train_unet` 的属性链取参数。注意：**未被点名的模块一律不进优化器**（u3-l3），所以冻结管线即使 `requires_grad=True`（如 `pipe_lora.unet`）也永不更新；同时 `fix_geometry: true`（[configs/dreamcraft3d-texture.yaml:59](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L59)）让几何侧只有 `encoding` 与 `feature_network` 两组在学。

**它们产生的三个损失如何回到系统**：`forward()` 出口打包 `loss_sd`（VSD 蒸馏项）、`loss_lora`、`loss_pretrain`（[stable_diffusion_bsd_guidance.py:1110-1116](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L1110-L1116)）；系统侧把所有 `loss_` 前缀的键按尾缀注册（[threestudio/systems/dreamcraft3d.py:204-207](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L204-L207)），再用 `lambda_` 权重加权求和（[threestudio/systems/dreamcraft3d.py:322-329](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L322-L329)）。texture 配置给三者的权重是 0.01 / 0.1 / 0.1（[configs/dreamcraft3d-texture.yaml:124-126](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L124-L126)）。

**一个交叉证据**：trainer 配置 `strategy: "ddp_find_unused_parameters_true"`（[configs/dreamcraft3d-texture.yaml:161](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L161)）。正因为预训练子步只用 `train_unet`、LoRA 子步只用 `train_unet_lora`、VSD 推理全程 no_grad，多卡 DDP 每步都有参数"轮空"，必须打开 find_unused_parameters 才能运行——模块结构与训练调度在配置上留下的互相印证。

#### 4.3.4 代码实践

1. **目标**：量化"全参微调"的代价，并与 LoRA 做数量级对比。
2. **步骤**：读代码回答前两问；有环境时运行统计脚本（示例代码，待本地验证）：

```python
# 示例代码（需 GPU/CPU 大内存 + 已下载权重，待本地验证）
import threestudio
g = threestudio.find("stable-diffusion-bsd-guidance")({})   # 全默认配置即可
for name in ["train_unet", "train_unet_lora"]:
    n = sum(p.numel() for p in getattr(g, name).parameters())
    print(f"{name}: {n/1e6:.1f}M trainable params")
```

3. **观察**：两个数字应几乎相同（同 为 SD2.1-base UNet，约 865M）。
4. **预期结果**：每个 UNet 约 \(8.65\times10^8\) 可训练参数；对照 4.4 练习算出的 LoRA 注入参数量（约 3M 量级），体会发布版本"弃 LoRA 用全参"的学习率必须压到 1e-5 的原因。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`geometry.encoding` 的 lr 是 `guidance.train_unet` 的多少倍？为什么差这么大？
**答案**：0.01 / 0.00001 = 1000 倍。几何编码是 tiny-cuda-nn 哈希网格（小容量、强局部），用大步长快速拟合；SD UNet 是 8.65 亿参数的预训练模型，只该在流形上"轻推"——大学习率会立刻摧毁预训练知识（灾难性遗忘），BSD 的自举正是建立在"教师模型保持 SD 先验"之上。

**练习 2**：既然 `pipe.unet` 与 `train_unet` 权重同源，为什么不删掉前者、共用后者？
**答案**：职责不同。`train_pretrain` 需要一个"还没被当前场景污染"的采样器来回灌伪 GT（至少第一次采样如此，见 4.2.3 的换绑副作用）；VSD 的两个噪声预测也要求一个持续个性化、一个贴当前分布。若共用一份权重，教师与评分员退化为同一点，4.3.1 公式的差分项恒趋零——蒸馏信号消失。

### 4.4 set_up_lora_layers 与 LoRAAttnProcessor：LoRA 注入机制

#### 4.4.1 概念说明

`set_up_lora_layers` 是一段**完整可用、却在 configure 里被注释掉**的方法。它演示了 diffusers 经典 LoRA 装配三步：遍历 UNet 全部注意力处理器 → 为每个位置构造 `LoRAAttnProcessor(hidden_size, cross_attention_dim)` → `set_attn_processor` 一次性替换。读懂它既能理解 BSD 从"LoRA 版"演化到"全参版"的历史，也是把 BSD 改回 LoRA 省显存版的入口。

#### 4.4.2 核心流程

```text
for name in unet.attn_processors.keys():          # 如 "down_blocks.0.attentions.0.transformer_blocks.0.attn1.processor"
    cross_attention_dim = None  若名字以 "attn1.processor" 结尾（自注意力）
                          否则 = unet.config.cross_attention_dim（SD2.x 为 1024）
    hidden_size:
        mid_block.*  → block_out_channels[-1]                 (1280)
        up_blocks.N.* → reversed(block_out_channels)[N]
        down_blocks.N.* → block_out_channels[N]
    lora_attn_procs[name] = LoRAAttnProcessor(hidden_size, cross_attention_dim)
unet.set_attn_processor(lora_attn_procs)          # 原子替换全部处理器
```

#### 4.4.3 源码精读

**方法本体**：[stable_diffusion_bsd_guidance.py:299-325](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L299-L325)

```python
cross_attention_dim = (
    None if name.endswith("attn1.processor")
    else unet.config.cross_attention_dim
)
if name.startswith("mid_block"):
    hidden_size = unet.config.block_out_channels[-1]
elif name.startswith("up_blocks"):
    block_id = int(name[len("up_blocks.")])
    hidden_size = list(reversed(unet.config.block_out_channels))[block_id]
elif name.startswith("down_blocks"):
    block_id = int(name[len("down_blocks.")])
    hidden_size = unet.config.block_out_channels[block_id]
lora_attn_procs[name] = LoRAAttnProcessor(
    hidden_size=hidden_size, cross_attention_dim=cross_attention_dim)
```

中文说明：`attn1` 是自注意力（k/v 来自 hidden_states，LoRA 只需知道 `hidden_size`），`attn2` 是交叉注意力（k/v 来自文本嵌入，还需 1024 维的 `cross_attention_dim`）。up 路的通道数是 down 路的镜像，所以要 `reversed`。

**被注释的调用现场**：[stable_diffusion_bsd_guidance.py:173-187](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L173-L187)。原设计是给 `unet_lora` 与 `unet`（当时的语义）各注入一套 LoRA，再用 `AttnProcsLayers`（diffusers 提供的包装器，从 [L15](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L15) 导入）把 processor 字典包成 `nn.Module` 以便收集 LoRA 参数交给优化器；随后在 [L207-208](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L207-L208) 把 `lora_layers` 冻结的注释也一并废弃。发布版选择了 4.3 的"整模型可训练"路线，这套 LoRA 装配从此休眠。

**scale 旋钮的语义**：代码里大量出现 `cross_attention_kwargs={"scale": 0.0}` 与 `{"scale": 1.0}`（如 [L535](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L535)、[L703](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L703)）。这是 diffusers 处理器层面预留给 LoRA 分支的强度接口：0 = 关闭 LoRA 分支（退回基座模型），1 = 全量生效——成对出现在"普通采样关 / LoRA 采样开"的路径上。由于本版本未注入 LoRA，这些参数目前是**休眠的旋钮**，只保留接口语义。

#### 4.4.4 代码实践

1. **目标**：不下载权重，仅凭 SD2.1-base 的 `unet/config.json`（约 1 KB）推导 `set_up_lora_layers` 会注入多少个 LoRA 处理器。
2. **步骤**：运行下面的算术脚本（示例代码，无需 torch）：

```python
# 示例代码：只读 UNet 配置 JSON，不下载权重
import json, urllib.request
url = ("https://huggingface.co/stabilityai/stable-diffusion-2-1-base/"
       "resolve/main/unet/config.json")
cfg = json.load(urllib.request.urlopen(url))
l = cfg["layers_per_block"]
down = sum(1 for b in cfg["down_block_types"] if b == "CrossAttnDownBlock2D")
up   = sum(1 for b in cfg["up_block_types"]   if b == "CrossAttnUpBlock2D")
n_transformers = down * l + 1 + up * (l + 1)   # +1 来自 mid_block
print("attn processors =", n_transformers * 2) # 每个 transformer 有 attn1+attn2
```

3. **观察**：`down_block_types` 里 `DownBlock2D`（最深层）不含注意力、`up_block_types` 里 `UpBlock2D`（最浅层）也不含；up 块的层数是 `layers_per_block + 1`。
4. **预期结果**：`3×2 + 1 + 3×3 = 16` 个 transformer 块 → **32 个注意力处理器**（16 自注意力 + 16 交叉注意力），即注入 32 个 `LoRAAttnProcessor`。SD1.x/2.x 系列均为该数，可作为常识记住；有 GPU 时可用 `len(g.set_up_lora_layers(g.train_unet_lora))` 直接验证（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：若把 `LoRAAttnProcessor` 的默认 `rank=4` 改为 16，单个交叉注意力处理器的可训练参数量变化多少？
**答案**：每个处理器含 q/k/v/out 四对低秩矩阵，每对参数量为 \(r\times(d_{in}+d_{out})\)。r 从 4 → 16 时参数量线性放大 4 倍；以 SD2.1-base 全部 32 个处理器估算，LoRA 参数从约 \(3\times10^6\) 量级升到 \(1.2\times10^7\) 量级（精确值取决于各块维度，待本地验证）——仍远小于 865M 的全参。

**练习 2**：`block_id = int(name[len("up_blocks.")])` 这种解析方式有什么脆弱性？
**答案**：它假设块索引是**单个数字**（截取前缀后的第一个字符）。SD 的 up/down 块各只有 4 个（索引 0–3），目前恰好成立；若未来出现 `up_blocks.10`，`int("1")` 会静默取错块、拿到错误的 `hidden_size`，导致 LoRA 矩阵维度与注意力层不匹配。更稳妥的写法是 `name.split(".")[1]`。

### 4.5 相机条件嵌入与视角相关提示的配合

#### 4.5.1 概念说明

BSD 原型设计里，得分估计器不仅要"知道场景"，还要"知道从哪个相机看"——这正是 u7-l3 Zero123 用相机条件缓解 Janus 的思路在 SD 上的复刻。实现通道是 UNet 自带的 `class_labels` 输入口：把 4×4 相机矩阵展平成 16 维，经一个小 MLP 升到 1280 维（SD 时间嵌入的宽度），加进 UNet 的时间嵌入流。**但发布版本把挂载与消费两处都注释掉了**：视角条件实际由 u7-l1 的视角相关文本提示（front/side/back，texture 配置阈值 30/30）承担。一实一虚，构成"设计存在、启用留白"的典型科研代码形态。

#### 4.5.2 核心流程

```text
配置 camera_condition_type: "extrinsics"（用 c2w）或 "mvp"
forward() 选取 camera_condition = c2w 或 mvp_mtx        [B,4,4]
展平 → [B,16]，可作为 class_labels 直 feed UNet
    cond 分支：真实相机；uncond 分支：全零相机（"零相机=无条件"）
camera_embedding: TimestepEmbedding(16 → 1280)，输出转 fp16
挂载点：unet.class_embedding = camera_embedding（被注释）
消费点：forward_unet(class_labels=...)（被注释）
兜底：disable_unet_class_embedding 上下文——临时把 class_embedding 置 None
```

#### 4.5.3 源码精读

**嵌入的构造与包裹器**：[stable_diffusion_bsd_guidance.py:28-35](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L28-L35) 定义 `ToWeightsDType`——一个只做"跑完子模块再把输出 cast 回权重精度"的包装器；[L167-171](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L167-L171) 用它包住 `TimestepEmbedding(16, 1280)`：

```python
# FIXME: hard-coded dims
self.camera_embedding = ToWeightsDType(
    TimestepEmbedding(16, 1280), self.weights_dtype
).to(self.device)
# self.unet_lora.class_embedding = self.camera_embedding
```

中文说明：16 = 4×4 相机矩阵展平；1280 = SD2.1-base UNet 的时间嵌入宽度（`block_out_channels[0]=320` 的 4 倍），升维后才能与时间嵌入相加。紧随其后的挂载行被注释——这正是"休眠"的直接证据。

**条件/无条件相机模式**：`sample_lora` 把真实相机与全零相机沿 batch 拼接（[stable_diffusion_bsd_guidance.py:518-537](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L518-L537)）：

```python
camera_condition_cfg = torch.cat(
    [camera_condition.view(B, -1), torch.zeros_like(camera_condition.view(B, -1))],
    dim=0)
```

这与文本 CFG"cond 在前、uncond（空提示）在后"的 2B 拼接完全同构——负条件从"空文本"换成了"零相机"。`compute_grad_vsd` 中对应的 `class_labels` 实参也是被注释状态（[L719-725](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L719-L725)），`forward_unet` 却已为此预留了 `class_labels` 形参（[L539-556](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L539-L556)，类型标注 `BB 16`）。

**兜底开关**：`disable_unet_class_embedding`（[stable_diffusion_bsd_guidance.py:584-591](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L584-L591)）是一个上下文管理器，临时把 `unet.class_embedding` 置 `None` 再恢复，供纯文本条件的采样路径使用（[L393](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L393)、[L631](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L631)）。它的存在本身说明作者预设"class_embedding 可能有，也可能没有"。

**视角相关提示如何接棒**：`forward()` 里同时准备两套文本嵌入（[stable_diffusion_bsd_guidance.py:1052-1068](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L1052-L1058) 为 `text_embeddings_vd`，视角相关；[L1059-1068](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L1059-L1068) 为 `text_embeddings`，视角无关，还允许 `lora_prompt_utils` 换用另一套提示词）。教师通道吃 vd 版（"从侧面看汉堡"），LoRA 通道吃无视角版——相机信息以**语言**的形式注入了教师通道。texture 配置把 front/back 阈值收紧到 30/30（[configs/dreamcraft3d-texture.yaml:71-76](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L71-L76)），视角分区比 coarse 阶段（45/45）更细，可以视作对"相机嵌入缺席"的补偿。

#### 4.5.4 代码实践

1. **目标**：完成一次"相机条件通道考古"——列出全部相关代码点，标注激活/休眠。
2. **步骤**：用下面的清单逐项核对源码并填表（纯阅读任务，无需运行）：

| 代码位置 | 作用 | 状态 |
| --- | --- | --- |
| [L168-170](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L168-L170) | 构造 `camera_embedding` | 激活（参数会随模块保存） |
| [L171](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L171) | 挂载到 `unet_lora.class_embedding` | 注释（休眠） |
| [L519-525](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L519-L525) | `sample_lora` 的 cond/零相机拼接 | 激活（但 `sample_lora` 仅调试用） |
| [L719-725](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L719-L725) | `compute_grad_vsd` 传 `class_labels` | 注释（休眠） |
| [L584-591](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L584-L591) | `disable_unet_class_embedding` 兜底 | 激活 |

3. **观察**：`forward()` 仍在 [L1070-1077](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L1070-L1077) 计算并传递 `camera_condition`，但 `compute_grad_vsd` 内部不再使用它——参数"到了门口没进屋"。
4. **预期结果**：得出结论——最小启用路径是"取消 L171 挂载注释 + 恢复 L719-725 的 `class_labels` 实参"，同时需确认该 diffusers 版本的 UNet 在 `class_embedding` 非 None 时会把嵌入加进时间嵌入流（待本地验证）。修改属二次开发，请勿直接改仓库源码，可在自定义副本中实验。

#### 4.5.5 小练习与答案

**练习 1**：为什么相机条件选 16 维输入而不是像 Zero123 那样手工挑 4 个角度量？
**答案**：Zero123（u7-l3）用领域知识构造紧凑的 T 四元组；BSD 直接喂整张 4×4 矩阵，把"哪些分量重要"交给 `TimestepEmbedding` 的两层 MLP 自己学。代价是需要数据和训练来学出好表征，收益是无需手工设计且天然兼容 `extrinsics`/`mvp` 两种矩阵（`camera_condition_type` 切换）。

**练习 2**：`ToWeightsDType` 包装器解决什么问题？
**答案**：fp16 权重下，模块输出若意外升为 fp32（如某些算子内部 promotions），与后续 fp16 张量相加会报 dtype 不匹配或引发隐式拷贝。它统一把 `camera_embedding` 的输出 cast 回 `self.weights_dtype`，是混合精度工程里的"接口适配垫片"。

## 5. 综合实践：绘制 BSD 结构图并核查冻结状态

这是本讲规格指定的综合任务，把四个模块的知识串成一张图。分两档完成：

### 第一档：纯阅读 + 配置推导（无 GPU，任何人可做）

**步骤 1**：对照 `configure()` 与属性定义，把下面的结构图模板补全并核对（答案要点已给）：

```text
StableDiffusionBSDGuidance（fp16）
│
├─ submodules.pipe ─────────── 管线①：SD2.1-base
│    ├─ unet（=unet_fix）      requires_grad=False；train_pretrain 首次采样后
│    │                          被换绑为 train_unet（L964）
│    ├─ vae（=vae/vae_lora/vae_fix，物理唯一） requires_grad=False
│    └─ scheduler 槽位 ← scheduler(DDPM) / scheduler_sample(DPMSolver)
│
├─ submodules.pipe_lora ────── 管线②：SD2.1-base（配置同①）
│    ├─ unet                   requires_grad=True（默认，从未显式冻结）
│    │                          但不进优化器、仅在 no_grad 采样中出现
│    ├─ vae ─────────────────── 即 pipe.vae（共享）
│    └─ scheduler 槽位 ← scheduler_lora / scheduler_lora_sample
│
├─ submodules.pipe_fix ─────── = pipe 的别名（同一对象，is 判定为 True）
│
├─ train_unet ──────────────── 可训练 UNet A：个性化教师通道
│                              requires_grad=True；lr=1e-5；xformers+ckpt
├─ train_unet_lora ─────────── 可训练 UNet B：得分估计通道（名字是 LoRA 遗产）
│                              requires_grad=True；lr=1e-5；xformers+ckpt
│
├─ camera_embedding ────────── TimestepEmbedding(16→1280)，挂载点注释（休眠）
├─ set_up_lora_layers ──────── 完整实现，configure 内调用被注释（休眠）
├─ cache_frames ────────────── train_pretrain 回灌缓冲（≤10 帧，L975-976）
└─ alphas ───────────────────  ᾱ_t 查表张量（权重 w(t)=1-ᾱ_t 用）
```

**步骤 2**：用 4.4.4 的脚本推导 LoRA 注入数量，标注在图上（预期 32 个处理器、16 自注意力 + 16 交叉注意力；rank=4）。

**步骤 3**：在图旁写三条"最易踩的坑"：① `self.unet` 可训练、`self.unet_fix` 冻结；② `pipe_fix is pipe` 及 L964 换绑副作用；③ `pipe_lora.unet` 的冻结靠"不进优化器"约定而非 `requires_grad=False`。

### 第二档：实例化自动审查（GPU + 已下载权重，待本地验证）

```python
# 示例代码：结构自动审查（需 GPU 与 SD2.1-base 权重，待本地验证）
import threestudio

g = threestudio.find("stable-diffusion-bsd-guidance")({})  # 全默认配置
print("pipe_fix is pipe :", g.submodules.pipe_fix is g.submodules.pipe)
print("vae_lora is vae  :", g.vae_lora is g.vae)

for name, mod in [("pipe.unet(frozen)", g.pipe.unet), ("pipe.vae(frozen)", g.vae),
                  ("pipe_lora.unet", g.pipe_lora.unet),
                  ("train_unet", g.train_unet), ("train_unet_lora", g.train_unet_lora)]:
    tot = sum(p.numel() for p in mod.parameters())
    tr = sum(p.numel() for p in mod.parameters() if p.requires_grad)
    print(f"{name:22s} {tot/1e6:8.1f}M params, trainable {tr/1e6:8.1f}M")

procs = g.set_up_lora_layers(g.train_unet_lora)  # 手动调用"休眠"方法做演示
print("LoRA processors injected:", len(procs))     # 预期 32
```

**预期观察**：两个 `is` 断言为 True；`train_unet`/`train_unet_lora` 的 trainable 等于总参数量（全参），`pipe.unet`/`pipe.vae` 的 trainable 为 0，`pipe_lora.unet` 的 trainable 等于总量（印证"约定式冻结"）；注入处理器数 32。若某项与预期不符，回到 4.2/4.3 的源码行号排查。**待本地验证**。

## 6. 本讲小结

- BSD 引导的静态结构 = **两条真实管线 + 一个别名**（`pipe_fix is pipe`，全文件只有两份 SD 权重）+ **两个全参可训练 UNet**（教师通道 `train_unet`、得分估计通道 `train_unet_lora`）+ 共享 VAE + 四个调度器。
- 属性命名与直觉相反：`self.unet` 是可训练的，`self.unet_fix` 才是冻结的；`pipe_lora.unet` 从未被显式冻结，只是"不进优化器 + 只在 no_grad 下使用"。
- `train_pretrain` 内 `self.pipe.unet = self.train_unet`（L964）的换绑因管线别名而牵连 `pipe_fix`，使"冻结采样"只在第一次名副其实。
- `set_up_lora_layers` 是一套完整可用的 LoRA 注入实现（按块位置推断 hidden_size、attn1/attn2 区分 cross_attention_dim、`set_attn_processor` 批量替换），但 configure 中的调用被注释——发布版本用"全参微调 + 1e-5 学习率"替代了 LoRA，`cross_attention_kwargs={"scale": ...}` 因此成为休眠旋钮。
- 相机条件通道（16→1280 的 `TimestepEmbedding` + class_labels + 零相机负条件）设计完备但挂载与消费双双注释；视角条件实际由视角相关文本提示（texture 阈值 30/30）承担。
- 三个损失 `loss_sd`/`loss_lora`/`loss_pretrain` 经系统侧通用管道（`loss_` 前缀 → `lambda_` 权重）以 0.01/0.1/0.1 汇入总损失；DDP 的 `find_unused_parameters_true` 正是"两个 UNet 不同步参与计算"的结构性后果。

## 7. 下一步学习建议

本讲只拆了"装配"，下一讲 **u7-l5（BSD 引导·下：VSD 梯度与双 LoRA 交替优化）** 将让这台机器转起来，建议按以下顺序预读：

1. [compute_grad_vsd（L680-766）](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L680-L766)：两个 UNet 的噪声预测如何做 CFG、v_prediction 换算、以及 `grad = w * (noise_pred_pretrain - noise_pred_est)`。
2. [train_lora（L896-939）](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L896-L939) 与 [train_pretrain（L941-1014）](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L941-L1014)：DreamBooth 式 MSE 的构造、`cache_frames` 回灌、10% 随机 CFG 丢弃。
3. [forward 的三岔调度（L1079-1122）](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L1079-L1122)：`only_pretrain_step` 节律、u7-l2 讲过的 `(latents - grad).detach()` 重参数化如何把外生梯度接回 autograd 图。
4. 若想先横向对比，可回到 u7-l3 的 Zero123 条件注入示意图，观察 BSD 去掉 c_concat、只留交叉注意力后的退化形态。

读完 u7-l5 后，你将能完整回答"一张渲染图如何同时推动 3D 场景与两个扩散模型前进"这一 DreamCraft3D 纹理阶段的核心问题。

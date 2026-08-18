# BSD 引导（下）：VSD 梯度与双 LoRA 交替优化

## 1. 本讲目标

上一讲（u7-l4）我们看完了 `stable-diffusion-bsd-guidance` 的静态装配：两条权重、两个可训练 UNet（`train_unet` 与 `train_unet_lora`）、休眠的 LoRA 注入与相机嵌入。本讲进入它的动态部分——训练时到底发生什么。学完本讲你应该能够：

1. 推导 VSD/BSD 的得分蒸馏梯度 \( \nabla_\theta \mathcal{L} \propto w(t)\,(\hat\epsilon_{\text{pretrain}} - \hat\epsilon_{\text{lora}}) \)，并说清它与 u7-l2 中 SDS 梯度的唯一差别在哪一项。
2. 逐行读懂 `compute_grad_vsd`、`train_lora`、`train_pretrain` 三个方法的数据流：渲染图 → VAE 潜码 → 加噪 → UNet → loss → 梯度去向。
3. 解释 `train_pretrain` 如何用「冻结模型采样回灌」给教师模型（`train_unet`）供给训练数据，从而避免自举过程中的灾难性遗忘，以及代码里一个让"冻结"名不副实的别名陷阱。
4. 打通调度闭环：texture 配置中的 `only_pretrain_step=1000`、`per_update_pretrain_step=25` 与 `dreamcraft3d-system.training_step` 里的 only_pretrain 分支如何共同决定「每 1000 步的前 200 步只做预训练」。

## 2. 前置知识

本讲站在三讲肩膀上，先用三段话把需要的结论搬过来。

**SDS 梯度（来自 u7-l2）**。得分蒸馏采样（Score Distillation Sampling）把扩散模型当作可微的"批评家"：对渲染图加噪到 \( z_t \)，用冻结 UNet 预测噪声，梯度形如

\[ \nabla_\theta \mathcal{L}_{\text{SDS}} = w(t)\,\big(\hat\epsilon_{\text{cfg}}(z_t, t, y) - \epsilon\big)\,\frac{\partial z_t}{\partial \theta}, \qquad w(t) = 1 - \bar\alpha_t \]

其中 \( \epsilon \) 是实际采样加入的噪声。DeepFloyd 通道就是这么做的，且 UNet 前向包在 `no_grad` 里，梯度经「target 重参数化」注入 autograd 图。

**VSD 的想法（本讲新增）**。SDS 用「高斯噪声 \( \epsilon \)」充当度量基准，这是一个有偏的近似。变分得分蒸馏（Variational Score Distillation，VSD，ProlificDreamer 提出）改为训练第二个轻量网络（通常用 LoRA 微调）去**拟合当前渲染分布本身的分数**，记它的噪声预测为 \( \hat\epsilon_{\phi} \)。于是基准从 \( \epsilon \) 换成了 \( \hat\epsilon_{\phi} \)：

\[ \nabla_\theta \mathcal{L}_{\text{VSD}} = w(t)\,\big(\hat\epsilon_{\text{pretrain}}(z_t, t, y) - \hat\epsilon_{\phi}(z_t, t, c)\big)\,\frac{\partial z_t}{\partial \theta} \]

直觉：\( \hat\epsilon_{\phi} \) 度量「场景现在长什么样」，\( \hat\epsilon_{\text{pretrain}} \) 度量「教师模型认为它应该长什么样」，两者的差驱动场景演化。当场景分布追上教师分布时，梯度趋零。

**BSD 的"自举"（Bootstrapped，承接 u1-l1 与 u7-l4）**。DreamCraft3D 进一步把 VSD 里冻结的教师也变成可训练的：`train_unet`（教师）由 `train_pretrain` 用「冻结管线在当前渲染上采样的图像」做 DreamBooth 式去噪训练；`train_unet_lora`（得分估计器）由 `train_lora` 在当前渲染上训练。教师、估计器、场景三方交替更新——这就是"自举"二字的代码落点。

还需要两个工程术语：

- **DreamBooth 式去噪损失**：给图像加噪，训练 UNet 预测所加噪声，MSE 监督——即标准扩散模型训练的一步。
- **SDEdit / img2img**：从一张真实图像的潜码出发，先加噪到某个强度，再用扩散模型去噪"重画"一遍。强度越高，重画自由度越大。

## 3. 本讲源码地图

| 文件 | 本讲关注的范围 | 作用 |
| --- | --- | --- |
| [threestudio/models/guidance/stable_diffusion_bsd_guidance.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py) | `forward`、`compute_grad_vsd`、`train_lora`、`train_pretrain`、`_sample`、`update_step` | BSD 引导的全部动态逻辑 |
| [threestudio/systems/dreamcraft3d.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py) | `training_step` 的 only_pretrain 分支、`training_substep` 的 guidance 调用与 loss 加权、`on_save_checkpoint` | 系统侧与引导侧的调度联动 |
| [configs/dreamcraft3d-texture.yaml](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml) | `guidance`、`freq`、`loss`、`optimizer` 段 | 本讲所有数值的来源 |
| [threestudio/systems/base.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py) | `on_train_batch_start`、`true_global_step` | `update_step` 的分发时机 |
| [threestudio/utils/misc.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/misc.py) | `C()` | 四元组线性插值，驱动时间步区间收缩 |

## 4. 核心概念与源码讲解

### 4.1 forward 的三分支路由与系统侧联动

#### 4.1.1 概念说明

BSD 的 `forward` 不是"算一个 loss 返回"，而是一个**路由器**：每个 guidance 子步，它根据当前步数决定走哪条路——

1. **预训练支路**（`do_update_pretrain` 为真）：只调用 `train_pretrain`，更新教师 `train_unet`，**提前返回**，不给场景任何梯度；
2. **蒸馏支路**（默认）：`compute_grad_vsd` 产出场景梯度（重参数化为 `loss_sd`），同时调用 `train_lora` 更新得分估计器（`loss_lora`）。

关键在于这条路由不是引导自己单方面决定的：系统侧 `training_step` 里有同一个取模判据，会在预训练窗口**强制**把子步切到 guidance 并关掉 ref 监督。两边判据必须完全一致，否则会出现"系统强制作了 guidance 子步、引导却走了 VSD"的错位。

#### 4.1.2 核心流程

```text
dreamcraft3d-system.training_step（texture: alternate, n_ref=2, ref_only_steps=0）
  ├─ do_ref = (step % 2 == 0)，do_guidance = not do_ref        # 偶数步参考图监督
  ├─ 若 (only_pretrain_step>0) 且 (step % 1000) < 200：        # ★ 双向联动的预训练窗口
  │     do_guidance=True, do_ref=False
  └─ 分别进入 training_substep("guidance"/"ref")
                      │
                      ▼ guidance 子步内调用 self.guidance(...)
bsd forward(self.global_step 由 update_step 每批同步)
  ├─ do_update_pretrain = (only_pretrain_step>0) 且 (step % 1000) < 200   # 同一判据
  │     ├─ 是 → train_pretrain(...) → 只返回 loss_pretrain，return
  │     └─ 否 → compute_grad_vsd → loss_sd（场景梯度）
  │              train_lora       → loss_lora（估计器梯度）
  └─ 系统侧把 loss_sd/lora/pretrain 映射到 lambda_sd/lora/pretrain 加权求和
```

#### 4.1.3 源码精读

先看引导侧的路由判据与提前返回：

[stable_diffusion_bsd_guidance.py:1079-1092](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L1079-L1092)——用 `self.global_step % only_pretrain_step < only_pretrain_step // 5` 判定预训练窗口；命中时 `sample_new_img` 每 `per_update_pretrain_step` 步才为真，调用 `train_pretrain` 后带着 `loss_pretrain` 提前返回，完全跳过 VSD 与 `train_lora`：

```python
do_update_pretrain = (self.cfg.only_pretrain_step > 0) and (
    (self.global_step % self.cfg.only_pretrain_step) < (self.cfg.only_pretrain_step // 5)
)
guidance_out = {}
if do_update_pretrain:
    sample_new_img = self.global_step % self.cfg.per_update_pretrain_step == 0
    loss_pretrain = self.train_pretrain(latents, text_embeddings_vd, camera_condition, sample_new_img=sample_new_img)
    guidance_out.update({"loss_pretrain": loss_pretrain, ...})
    return guidance_out
```

再看非预训练支路的收尾——VSD 梯度的重参数化、`train_lora` 的调用、以及返回字典：

[stable_diffusion_bsd_guidance.py:1094-1116](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L1094-L1116)——`grad` 经 `nan_to_num` 与可选裁剪后，构造 `target=(latents-grad).detach()`，用 MSE 把外生梯度注入 autograd 图（与 u7-l2 DeepFloyd 同款手法）：

```python
grad = self.compute_grad_vsd(latents, text_embeddings_vd, text_embeddings, camera_condition)
grad = torch.nan_to_num(grad)
target = (latents - grad).detach()
loss_vsd = 0.5 * F.mse_loss(latents, target, reduction="sum") / batch_size
loss_lora = self.train_lora(latents, text_embeddings, camera_condition)
guidance_out.update({"loss_sd": loss_vsd, "loss_lora": loss_lora, "grad_norm": grad.norm(), ...})
```

系统侧的联动分支在 `training_step` 里：

[dreamcraft3d.py:344-357](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L344-L357)——alternate 调度先按 `n_ref` 分配 ref/guidance，随后用与引导侧**完全相同**的取模判据覆盖结果：预训练窗口内强制 guidance、关闭 ref：

```python
do_ref = (
    self.true_global_step < self.cfg.freq.ref_only_steps
    or self.true_global_step % self.cfg.freq.n_ref == 0
)
do_guidance = not do_ref
if hasattr(self.guidance.cfg, "only_pretrain_step"):
    if (self.guidance.cfg.only_pretrain_step > 0) and (self.global_step % self.guidance.cfg.only_pretrain_step) < (self.guidance.cfg.only_pretrain_step // 5):
        do_guidance = True
        do_ref = False
```

返回的 `loss_*` 键如何变成系统损失：[dreamcraft3d.py:204-207](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L204-L207) 把 `loss_sd/loss_lora/loss_pretrain` 截尾成 `sd/lora/pretrain`，拼上前缀得到 `loss_guidance_sd` 等名字；[dreamcraft3d.py:321-329](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L321-L329) 再按 `lambda_sd: 0.01`、`lambda_lora: 0.1`、`lambda_pretrain: 0.1`（[dreamcraft3d-texture.yaml:123-126](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L123-L126)）加权求和。

还有一个容易忽略的配套细节：texture 配置使用 `strategy: ddp_find_unused_parameters_true`（[dreamcraft3d-texture.yaml:161](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L161)），正是因为不同步数激活的参数子集不同（预训练步只有 `train_unet` 有梯度、ref 步只有场景外观有梯度），DDP 必须容忍"未使用参数"。

#### 4.1.4 代码实践

**实践目标**：不依赖 GPU，用纯 Python 复刻两侧判据，验证「每 1000 步的前 200 步只做预训练」的调度结论。

**操作步骤**：把下面的模拟脚本保存为独立文件运行（示例代码，与仓库无关，可在任何有 Python 的地方跑）：

```python
# bsd_schedule_sim.py（示例代码）
n_ref, ref_only_steps = 2, 0            # dreamcraft3d-texture.yaml freq 段
only_pretrain, per_update, max_steps = 1000, 25, 5000

stats = {"pretrain": 0, "vsd_and_lora": 0, "ref": 0}
sample_steps, cache = [], []

for step in range(max_steps):
    # 复刻 dreamcraft3d.py:348-357
    do_ref = step < ref_only_steps or step % n_ref == 0
    do_guidance = not do_ref
    in_pretrain = only_pretrain > 0 and step % only_pretrain < only_pretrain // 5
    if in_pretrain:
        do_guidance, do_ref = True, False

    if in_pretrain:                       # 复刻 bsd forward:1084-1092
        stats["pretrain"] += 1
        if step % per_update == 0:        # sample_new_img 判据
            sample_steps.append(step)
            cache.append(step)
            if len(cache) > 10:           # train_pretrain 的缓存上限
                cache.pop(0)
    elif do_guidance:
        stats["vsd_and_lora"] += 1
    else:
        stats["ref"] += 1

print(stats)
print("每个预训练窗口内的新采样步:", [s % 1000 for s in sample_steps[:8]])
```

**需要观察的现象**：输出的三类步数比例；采样步在窗口内的分布。

**预期结果**（确定性计算，可直接核对）：`{'pretrain': 1000, 'vsd_and_lora': 2000, 'ref': 2000}`——5 个窗口 × 200 步预训练；其余 4000 步按 `n_ref=2` 平分。采样步为每个窗口内的 `0, 25, 50, ..., 175` 共 8 个、总计 40 个，缓存最多保留 10 帧。

**关于"前 1000 步以预训练为主"的准确表述**：`only_pretrain_step=1000` 的语义不是"只有前 1000 步"，而是**周期为 1000 步、占空比 1/5**——步 0–199、1000–1199、2000–2199、3000–3199、4000–4199 都只做预训练。系统侧分支在这 200 步里强制作 guidance 子步并关掉 ref；引导侧同一判据命中后提前返回 `loss_pretrain`。`per_update_pretrain_step=25` 再在这 200 步内部划出 8 个采样步，其余 192 步复用缓存帧训练教师。

#### 4.1.5 小练习与答案

**练习 1**：把 `freq.n_ref` 从 2 改成 4（`only_pretrain_step` 不变），三类步的比例如何变化？

**答案**：预训练步不受影响，仍是 1000 步（由 `only_pretrain_step` 独立决定）；其余 4000 步中 `do_ref = (step % 4 == 0)` 命中 1000 步 ref，剩下 3000 步做 VSD+LoRA。参考图监督被稀释，场景更依赖扩散先验。

**练习 2**：为什么系统侧判据用 `self.global_step` 而不是 `true_global_step`？这会有问题吗？

**答案**：这是 u6-l2 指出的例外写法。正常训练中两者相等（`true_global_step` 只是评估/续训恢复时的语义修正，见 [base.py:69-74](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L69-L74)），真正要紧的是**两侧一致**：引导侧的 `self.global_step` 是在 `update_step` 里用同一个裸 `global_step` 同步的（[stable_diffusion_bsd_guidance.py:1130](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L1130)）。只要两侧取模基准相同，窗口就不会错位；若一边改用 `true_global_step` 而另一边不改，恢复训练后相位差会导致强制与路由失效。

### 4.2 compute_grad_vsd：变分得分蒸馏梯度

#### 4.2.1 概念说明

`compute_grad_vsd` 是 BSD 的心脏。与 u7-l2 的 SDS 相比，公式只差一项：

\[ \underbrace{w(t)\,(\hat\epsilon_{\text{cfg}} - \epsilon)}_{\text{SDS（u7-l2）}} \quad\longrightarrow\quad \underbrace{w(t)\,\big(\hat\epsilon_{\text{pretrain}} - \hat\epsilon_{\text{lora}}\big)}_{\text{VSD/BSD（本讲）}} \]

- \( \hat\epsilon_{\text{pretrain}} \)：教师 `train_unet` 的 CFG 融合预测，吃**视角相关**文本嵌入（front/side/back 提示切换，见 u7-l1），代表"个性化教师认为这个视角应该长什么样"；
- \( \hat\epsilon_{\text{lora}} \)：得分估计器 `train_unet_lora` 的预测，吃**视角无关**的条件嵌入，正在被 `train_lora` 拟合到当前渲染分布上，代表"场景现在长什么样"；
- \( w(t) = 1 - \bar\alpha_t \) 与 DeepFloyd 通道的加权完全一致。

两个 UNet 前向都在 `no_grad` 里——梯度不穿过扩散模型，`grad` 作为显式张量返回，由调用方重参数化注入。

#### 4.2.2 核心流程

```text
输入: latents [B,4,64,64]（渲染图经 VAE 编码，带梯度）
 1. 采样 t ∈ [min_step, max_step]，noise ~ N(0,I)
 2. latents_noisy = √ᾱ_t · latents + √(1-ᾱ_t) · noise      (no_grad)
 3. ε_pretrain = train_unet(   latents_noisy×2, t, 视角相关嵌入 )  (no_grad)
 4. ε_lora     = train_unet_lora(latents_noisy×2, t, 视角无关cond×2 )  (no_grad)
 5. （若 lora 侧是 v_prediction：ε = σ_t·x_t + α_t·v̂ 转换）
 6. 各自做 CFG 融合 → grad = (1-ᾱ_t) · (ε_pretrain − ε_lora)
返回 grad（与 latents 同形状）；调用方: loss_sd = 0.5·‖latents − (latents−grad).detach()‖²/B
```

#### 4.2.3 源码精读

[stable_diffusion_bsd_guidance.py:689-710](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L689-L710)——整个噪声预测段包在 `torch.no_grad()` 里：`t` 在 `[min_step, max_step+1)` 均匀采样，`add_noise` 完成前向扩散；教师通道把潜码复制两份（CFG 的 cond/uncond 批），喂给 `self.train_unet` 的是视角相关嵌入 `text_embeddings_vd`：

```python
with torch.no_grad():
    t = torch.randint(self.min_step, self.max_step + 1, [B], ...)
    noise = torch.randn_like(latents)
    latents_noisy = self.scheduler.add_noise(latents, noise, t)
    latent_model_input = torch.cat([latents_noisy] * 2, dim=0)
    cross_attention_kwargs = {"scale": 0.0}
    noise_pred_pretrain = self.forward_unet(
        self.train_unet, latent_model_input, torch.cat([t] * 2),
        encoder_hidden_states=text_embeddings_vd, ...)
```

[stable_diffusion_bsd_guidance.py:712-727](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L712-L727)——估计器通道：取**视角无关**嵌入的 cond 半份、重复两次喂给 `train_unet_lora`；相机条件 `class_labels` 整段被注释掉，印证 u7-l4 的结论——相机条件休眠，视角信息实际由教师侧的视角相关文本承担：

```python
# use view-independent text embeddings in LoRA
text_embeddings_cond, _ = text_embeddings.chunk(2)
noise_pred_est = self.forward_unet(
    self.train_unet_lora, latent_model_input, torch.cat([t] * 2),
    encoder_hidden_states=torch.cat([text_embeddings_cond] * 2),
    # class_labels=torch.cat([camera_condition.view(B, -1), torch.zeros_like(...)], dim=0),
    cross_attention_kwargs={"scale": 0.0},
)
```

[stable_diffusion_bsd_guidance.py:731-741](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L731-L741)——v_prediction 转换分支：若 `scheduler_lora` 来自 SD2.1（非 base，v 参数化），把预测的 \( v \) 换算成 \( \epsilon \)。由 \( x_t=\alpha_t x_0+\sigma_t\epsilon,\ v=\alpha_t\epsilon-\sigma_t x_0 \) 解出 \( \epsilon=\alpha_t v+\sigma_t x_t \)：

```python
if self.scheduler_lora.config.prediction_type == "v_prediction":
    alpha_t = alphas_cumprod[t] ** 0.5
    sigma_t = (1 - alphas_cumprod[t]) ** 0.5
    noise_pred_est = latent_model_input * torch.cat([sigma_t] * 2, dim=0).view(-1, 1, 1, 1) \
                   + noise_pred_est * torch.cat([alpha_t] * 2, dim=0).view(-1, 1, 1, 1)
```

[stable_diffusion_bsd_guidance.py:743-766](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L743-L766)——两侧分别 chunk 成 text/uncond 半份做 CFG 融合（教师用 `guidance_scale`，texture 配置为 2.0，见 [dreamcraft3d-texture.yaml:83](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L83)），最后加权作差：

```python
noise_pred_est = noise_pred_est_uncond + self.cfg.guidance_scale_lora * (
    noise_pred_est_camera - noise_pred_est_uncond
)
noise_pred_pretrain = noise_pred_pretrain_uncond + self.cfg.guidance_scale * (
    noise_pred_pretrain_text - noise_pred_pretrain_uncond
)
w = (1 - self.alphas[t]).view(-1, 1, 1, 1)
grad = w * (noise_pred_pretrain - noise_pred_est)
```

注意 `noise_pred_est_camera/_uncond` 是历史命名残留（来自 `class_labels` 相机条件尚未注释的年代）：这一通道两半输入是同一个 cond 嵌入的重复，预测完全相同，所以 CFG 融合是恒等变换，`guidance_scale_lora` 在此不生效。

#### 4.2.4 代码实践

**实践目标**：为 `compute_grad_vsd` 写一段数据流注释（本讲实践任务的第一部分）。

**操作步骤**：在自己的阅读笔记中抄录该方法并逐段标注（不改源码）。参考模板：

```text
compute_grad_vsd 数据流
输入: latents [1,4,64,64]  ← 渲染图 1024×1024 → 双线性缩放 512×512 → VAE 编码
                             （get_latents, bsd:1016-1029; 调用点 bsd:1048）
  no_grad:
    t ~ U[min_step, max_step]                 # 区间随训练收缩，见 4.5
    latents_noisy = add_noise(latents, ε, t)  # ε 为本次采样噪声，仅用于构造 z_t
    ε_pretrain ← train_unet(z_t ⊕ z_t, t, 视角相关嵌入)     # 教师
    ε_lora     ← train_unet_lora(z_t ⊕ z_t, t, 视角无关cond) # 估计器
    （v_prediction 时换算 ε = σ_t·z_t + α_t·v̂）
  各自 CFG 融合 → grad = (1-ᾱ_t)(ε_pretrain − ε_lora)
输出: grad [1,4,64,64]
梯度去向: 不进任何 UNet（全程 no_grad）；经 forward:1103-1106 的
          target=(latents−grad).detach() 重参数化，∂loss_sd/∂latents = grad/B，
          再沿 VAE→渲染→场景外观参数（texture 阶段几何已冻结）回传
```

**需要观察的现象**：注释完成后自查三件事——t 的采样区间是否写对了来源；两个 UNet 吃的文本嵌入有何不同；`grad` 为什么不可能带有 UNet 参数的梯度。

**预期结果**：能一眼说出「梯度只流向场景，两个 UNet 在这一步都只是数值函数」。

#### 4.2.5 小练习与答案

**练习 1**：推导 `loss_vsd = 0.5 * F.mse_loss(latents, target, reduction="sum") / batch_size`（`target=(latents-grad).detach()`）对 `latents` 的梯度。

**答案**：MSE 为 `0.5·Σ(latents−target)²/B`，target 视为常数，故

\[ \frac{\partial \text{loss\_vsd}}{\partial \text{latents}} = \frac{\text{latents} - (\text{latents}-\text{grad})}{B} = \frac{\text{grad}}{B} \]

即梯度大小恰为 `grad` 除以批大小——注释 `d(loss)/d(latents) = latents - target = grad`（[stable_diffusion_bsd_guidance.py:1103-1105](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L1103-L1105)）说的就是这个，除以 B 是代码里额外的归一化。

**练习 2**：把 `pretrained_model_name_or_path_lora` 从 `stable-diffusion-2-1-base` 换成 `stable-diffusion-2-1`（配置里被注释的那一行，[dreamcraft3d-texture.yaml:82](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L82)），`compute_grad_vsd` 里哪段代码会被激活？

**答案**：SD2.1（非 base）的调度器是 `v_prediction`，此时 [731-741 行](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L731-L741)的换算分支生效，把 `train_unet_lora` 预测的 \( v \) 按 \( \epsilon=\alpha_t v+\sigma_t x_t \) 换成 \( \epsilon \) 再与教师作差；教师侧 `assert prediction_type == "epsilon"` 仍要求 base 模型。默认配置两侧都是 base，该分支不激活。

### 4.3 train_lora：在当前渲染上训练得分估计器

#### 4.3.1 概念说明

VSD 成立的前提是 \( \hat\epsilon_{\text{lora}} \) 真的逼近当前渲染分布 \( p_\theta(z) \) 的分数。去噪分数匹配给出了一条路：在 \( p_\theta \) 的样本（即渲染图）上加噪并训练网络预测所加噪声，最优解满足

\[ \hat\epsilon^*(z_t) = \mathbb{E}[\epsilon \mid z_t] \propto \sigma_t \nabla_{z_t} \log p_\theta(z_t) \]

`train_lora` 就是这个训练的一步：它把**本步刚渲染出来的图**（detach 后）当作 \( p_\theta \) 的样本，对 `train_unet_lora` 做标准扩散去噪损失。注意与 DreamBooth 的区别——没有参考图、没有先验保持项，纯粹"学会给当前场景的渲染去噪"。名字里的 lora 是历史称谓：u7-l4 已确认 LoRA 注入被注释，这里实际微调的是整个 UNet。

#### 4.3.2 核心流程

```text
输入: latents（当前渲染的潜码）, text_embeddings（视角无关）, camera_condition
 1. latents = latents.detach()（切断场景梯度！）重复 lora_n_timestamp_samples=1 份
 2. t ~ U[0, 1000)                    ← 全区间，不限于 [min_step, max_step]
 3. noisy = scheduler_lora.add_noise(latents, ε, t)
 4. target = ε（epsilon 参数化）
 5. 10% 概率把 camera_condition 置零（CFG dropout 惯例；当前实际未用到该条件）
 6. pred = train_unet_lora(noisy, t, cond 嵌入)
 7. loss_lora = MSE(pred, ε)
梯度去向: 只进 train_unet_lora 参数
```

#### 4.3.3 源码精读

[stable_diffusion_bsd_guidance.py:896-922](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L896-L922)——开头一句 `detach` 是本方法的灵魂：估计器的训练目标是"拟合场景"，不能反过来把梯度灌回场景，否则自举闭环退化成场景自我强化。`t` 在**整个** `[0, num_train_timesteps)` 上采样（代码里写成 `int(*0.0)` 到 `int(*1.0)`），因为分数估计要在所有噪声水平上都准确：

```python
B = latents.shape[0]
latents = latents.detach().repeat(self.cfg.lora_n_timestamp_samples, 1, 1, 1)
t = torch.randint(
    int(self.num_train_timesteps * 0.0),
    int(self.num_train_timesteps * 1.0),
    [B * self.cfg.lora_n_timestamp_samples], ...
)
noise = torch.randn_like(latents)
noisy_latents = self.scheduler_lora.add_noise(latents, noise, t)
if self.scheduler_lora.config.prediction_type == "epsilon":
    target = noise
```

[stable_diffusion_bsd_guidance.py:923-939](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L923-L939)——条件嵌入取视角无关的 cond 半份；`lora_cfg_training` 以 10% 概率把条件置零（无分类器引导训练的标准 dropout，这里置零的对象 `camera_condition` 因 `class_labels` 被注释而实际未被消费，属休眠代码）；最后与 `target=noise` 做 MSE：

```python
text_embeddings_cond, _ = text_embeddings.chunk(2)
if self.cfg.lora_cfg_training and random.random() < 0.1:
    camera_condition = torch.zeros_like(camera_condition)
noise_pred = self.forward_unet(
    self.train_unet_lora, noisy_latents, t,
    encoder_hidden_states=text_embeddings_cond.repeat(self.cfg.lora_n_timestamp_samples, 1, 1),
    cross_attention_kwargs={"scale": 0.0},
)
return F.mse_loss(noise_pred.float(), target.float(), reduction="mean")
```

#### 4.3.4 代码实践

**实践目标**：为 `train_lora` 写数据流注释，并与 `compute_grad_vsd` 对照"同一张渲染图的两种用法"。

**操作步骤**：在笔记中完成下表（本讲实践任务第二部分的前半）：

| 维度 | compute_grad_vsd 中的 latents | train_lora 中的 latents |
| --- | --- | --- |
| 是否 detach | 否（保留场景梯度链） | 是（`bsd:903`） |
| t 的区间 | `[min_step, max_step]`（随训练收缩） | `[0, 1000)` 全区间 |
| UNet 是否回传梯度 | 否（no_grad，输出仅作数值） | 是（`train_unet_lora` 被优化） |
| 文本条件 | 教师：视角相关；估计器：视角无关 cond | 视角无关 cond |
| 监督/目标 | 无监督差分 `w(t)(ε_pretrain−ε_lora)` | 所加噪声 ε |

**需要观察的现象**：同一份 `latents` 在 [forward:1094-1108](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L1094-L1108) 被先后交给两个方法，角色完全不同。

**预期结果**：能说出"一份带梯度做蒸馏信号、一份 detach 做估计器训练数据"。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `train_lora` 的 `t` 用全区间，而 `compute_grad_vsd` 限制在 `[min_step, max_step]`？

**答案**：`train_lora` 是在训练一个扩散模型（分数估计器），必须覆盖所有噪声水平才能完整拟合 \( p_\theta \)；`compute_grad_vsd` 是给场景的蒸馏信号，过低的噪声级（接近干净图）梯度信噪比差、过高的噪声级信息被抹平，所以用 `min/max_step_percent` 圈定中高噪声区间并随训练收缩（见 4.5）。

**练习 2**：如果忘了 `latents.detach()` 这一行会发生什么？

**答案**：`loss_lora` 的梯度会沿 VAE 编码链回流到渲染与场景外观参数。这样场景会被"推动去逼近估计器当前已有的分数"而非教师分布，自举关系被颠倒，且教师与估计器互相追逐会失去锚点，训练易发散——这正是该 detach 存在的意义。

### 4.4 train_pretrain：冻结模型采样回灌与灾难性遗忘

#### 4.4.1 概念说明

教师 `train_unet` 的个性化数据从哪来？直接拿参考图做 DreamBooth 会把多视角问题带回来；直接拿场景渲染回灌则形成"自己教自己"的正反馈，误差会滚雪球——**灾难性遗忘**：教师逐渐忘掉通用扩散先验，蒸馏信号随之退化。BSD 的解法是**采样回灌**：用（初始）冻结的通用 SD 管线，以当前渲染为起点做 SDEdit 式 img2img、以视角相关提示为指导，"重画"出一张既贴合当前场景几何、又保有其先验质量的高 CFG 图像，再用它做去噪训练。教师由此追踪场景的演化（每 25 步重新采样），但监督分布始终锚定在通用先验上。

对应论文叙事：几何由前三个阶段固定后，纹理阶段的关键是"教师先专精于这个实例"。代码里 `train_pretrain` 同时维护一个最多 10 帧的图像缓存，让每一步预训练都从缓存随机抽帧，平滑监督分布。

#### 4.4.2 核心流程

```text
输入: latents（渲染潜码）, text_embeddings_vd（视角相关）, camera_condition
每 25 步（或缓存为空）:
  A1. images_sample = _sample(pipe_fix, 种子=渲染 latents, CFG=7.5)   # 冻结管线的重画
  A2. 存 .threestudio_cache/test_sample.jpg；追加进 cache_frames
  A3. pipe.unet ← train_unet（重绑！）
  A4. 再采样一张（CFG=1.0）存 test_pretrain.jpg                      # 诊断用
缓存超过 10 帧则弹出最旧；随机抽一帧
每一步预训练:
  B1. latents_sample = VAE.encode(images_sample)     (no_grad)
  B2. t ~ U[0, 1000)，noisy = scheduler.add_noise(latents_sample, ε, t)
  B3. 10% 概率把 cond 嵌入置零（CFG dropout）
  B4. pred = train_unet(noisy, t, 视角相关 cond)
  B5. loss_pretrain = MSE(pred, ε)
梯度去向: 只进 train_unet；渲染只提供采样的初始潜码（已 detach、no_grad）
```

#### 4.4.3 源码精读

[stable_diffusion_bsd_guidance.py:949-974](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L949-L974)——回灌采样的主体：第一次调用用 `pipe_fix`（冻结管线）以渲染潜码为种子、视角相关嵌入为指导、CFG 7.5 采样；随后 `self.pipe.unet = self.train_unet` 这行重绑是 u7-l4 指出的别名陷阱——`pipe_fix` 就是 `pipe` 的别名，重绑之后"冻结采样"只剩第一次名副其实，后续窗口的采样实际出自正在个性化的 `train_unet` 自身（CFG 7.5），自举程度比论文叙事更强。第二张 CFG=1.0 的采样只存盘诊断、不入缓存：

```python
if sample_new_img or len(self.cache_frames) == 0:
    latents = latents.detach().repeat(self.cfg.lora_pretrain_n_timestamp_samples, 1, 1, 1)
    images_sample = self._sample(
        pipe=self.pipe_fix, sample_scheduler=self.scheduler_sample,
        text_embeddings=text_embeddings, num_inference_steps=25,
        guidance_scale=7.5, cross_attention_kwargs={"scale": 0.0},
        latents_inp=latents,
    ).permute(0, 3, 1, 2)
    save_image(images_sample, f".threestudio_cache/test_sample.jpg")
    self.cache_frames.append(images_sample)

    self.pipe.unet = self.train_unet          # ★ 重绑，pipe_fix 一并受牵连
    pretrain_images_sample = self._sample(
        pipe=self.pipe, ..., guidance_scale=1.0, latents_inp=latents,
    ).permute(0, 3, 1, 2)
    save_image(pretrain_images_sample, f".threestudio_cache/test_pretrain.jpg")
```

[stable_diffusion_bsd_guidance.py:354-369](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L354-L369)——`_sample` 的 img2img 分支决定了回灌的"重画强度"：噪声水平 `t` 恒取当前 `max_step`（`randint(max_step, max_step+1)` 只会返回 `max_step`），换算成 25 步去噪里跳过前若干步。强度 = `max_step / num_train_timesteps`，随 `max_step_percent` 四元组从 0.5 收缩到 0.2（见 4.5）——训练越往后，重画越尊重当前渲染：

```python
if latents_inp is not None:
    B = latents_inp.shape[0]
    t = torch.randint(self.max_step, self.max_step + 1, [B], ...)
    noise = torch.randn_like(latents_inp)
    init_timestep = max(1, min(int(num_inference_steps * t[0].item() / self.num_train_timesteps), num_inference_steps))
    t_start = max(num_inference_steps - init_timestep, 0)
    latent_timestep = sample_scheduler.timesteps[t_start : t_start + 1].repeat(batch_size)
    latents = sample_scheduler.add_noise(latents_inp, noise, latent_timestep).to(self.weights_dtype)
```

[stable_diffusion_bsd_guidance.py:975-992](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L975-L992)——缓存管理（超过 10 帧弹最旧、随机抽帧）与训练样本准备：抽出的图像在 `no_grad` 下重新 VAE 编码，在全区间采样的 `t` 上加噪。注意 `noise = torch.randn_like(latents)` 里的 `latents` 在"缓存命中、无需采样"的路径下还是原始输入形状，靠默认 `B=1`、采样份数为 1 才与 `latents_sample` 形状吻合：

```python
if len(self.cache_frames) > 10:
    self.cache_frames.pop(0)
random_idx = torch.randint(0, len(self.cache_frames), [1]).item()
images_sample = self.cache_frames[random_idx]
with torch.no_grad():
    latents_sample = self.get_latents(images_sample, rgb_as_latents=False)
t = torch.randint(int(self.num_train_timesteps * 0.0), int(self.num_train_timesteps * 1.0), ...)
noise = torch.randn_like(latents)
noisy_latents = self.scheduler.add_noise(latents_sample, noise, t)
```

[stable_diffusion_bsd_guidance.py:1001-1014](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L1001-L1014)——条件与损失：`forward` 传入的是 `text_embeddings_vd`（[stable_diffusion_bsd_guidance.py:1086](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L1086)），这里取 cond 半份、10% 概率置零（`FIXME` 注释表明作者自己也还在权衡视角相关与否），DreamBooth 式去噪 MSE：

```python
# FIXME: use view-independent or dependent embeddings?
text_embeddings_cond, _ = text_embeddings.chunk(2)
if self.cfg.lora_pretrain_cfg_training and random.random() < 0.1:
    text_embeddings_cond = torch.zeros_like(text_embeddings_cond)
noise_pred = self.forward_unet(
    self.train_unet, noisy_latents, t,
    encoder_hidden_states=text_embeddings_cond.repeat(self.cfg.lora_pretrain_n_timestamp_samples, 1, 1),
)
loss_pretrain = F.mse_loss(noise_pred.float(), target.float(), reduction="mean")
```

#### 4.4.4 代码实践

**实践目标**：为 `train_pretrain` 写数据流注释，标出"渲染在哪里退出梯度世界"。

**操作步骤**：在笔记中完成（本讲实践任务第二部分的后半）：

```text
train_pretrain 数据流
输入: latents [1,4,64,64]（渲染潜码，带梯度）、视角相关嵌入
采样步（每 25 步）:
  latents.detach() → _sample(pipe_fix, 种子=latents, CFG=7.5)   ← 渲染在此退出梯度世界
  → images_sample [1,3,512,512] 入缓存（≤10 帧，随机抽取）
  → （pipe.unet 重绑为 train_unet；再采样一张仅存盘）
训练步:
  images_sample --VAE.encode(no_grad)--> latents_sample
  → t ~ U[0,1000)，noisy = add_noise(latents_sample, ε, t)
  → train_unet(noisy, t, 视角相关 cond，10% 置零) → ε̂
  → loss_pretrain = MSE(ε̂, ε)
梯度去向: 只进 train_unet 参数；场景/渲染/缓存图像全部无梯度
```

**需要观察的现象**（若本地具备 GPU 环境可验证，否则标注「待本地验证」）：跑 texture 阶段若干步，检查 `.threestudio_cache/test_sample.jpg` 是否只在预训练窗口内、每 25 步更新一次；对比 `test_pretrain.jpg`（CFG=1.0 的教师自采样）与 `test_sample.jpg` 的质量差异。

**预期结果**：两个文件都只在 `global_step % 25 == 0` 且处于预训练窗口时被覆盖；`test_pretrain.jpg` 早期接近噪声图（教师还是初始权重、低 CFG），随预训练推进逐渐出现参考物内容。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：第一个与第二个预训练窗口里的"冻结采样"有什么不同？

**答案**：第一个窗口（步 0）时 `pipe.unet` 尚未被重绑，`_sample(pipe_fix)` 用的是真正的冻结 SD；该窗口内执行 `self.pipe.unet = self.train_unet` 后，`pipe_fix` 作为别名随之指向可训练 UNet，从第二个窗口起采样实际由个性化中的教师自己完成——监督数据从"通用先验的重画"渐进过渡为"教师的自采样"，这正是 bootstrapped 的字面含义，也意味着防遗忘的锚主要在早期起作用。

**练习 2**：缓存（`cache_frames`）解决什么问题？

**答案**：两个问题。其一，25 步一次的 25 步去噪采样很贵，缓存让其余 192 步复用旧帧；其二，随机抽帧（最多 10 张、横跨约 250 步的历史）让每步的监督样本带随机性，避免教师过拟合到单张重画结果。

**练习 3**：为什么说"直接用场景渲染当教师训练数据"会导致灾难性遗忘？

**答案**：场景早期的渲染有伪影，教师学完这些伪影后，VSD 的差分信号（教师−估计器）会把伪影进一步确认为"应该有的样子"，场景再渲染、教师再学，误差在闭环里自我强化，且通用扩散先验逐渐被冲掉。回灌采样在"场景现状"与"通用先验"之间插入了冻结模型这道滤波器：种子是渲染、分布是 SD 的。

### 4.5 update_step 调度闭环与时间步区间收缩

#### 4.5.1 概念说明

前四个模块反复出现 `self.global_step`、`min_step/max_step`，它们的 freshness 由 `update_step` 钩子保证。这是 u3-l2 生命周期的又一次应用：`BaseSystem.on_train_batch_start` 在每个训练批开始时递归分发 `update_step`，BSD 引导作为系统的一个 `Updateable` 属性收到回调，同步步数并刷新三个量——梯度裁剪值、自身步数副本、蒸馏时间步区间。其中时间步区间通过 `C()` 四元组随训练收缩，同时影响三处行为：VSD 的 `t` 采样、回灌采样的重画强度、（未启用的）DU 通道。

#### 4.5.2 核心流程

```text
每批训练开始（Lightning on_train_batch_start）
  → BaseSystem.do_update_step(epoch, true_global_step)      # 递归遍历 Updateable 属性
    → BSDGuidance.update_step:
        grad_clip_val = C(cfg.grad_clip)          # texture 配置未设置 → 保持 None
        self.global_step = global_step            # 供 forward 的取模判据使用
        min_step = int(1000 × C(0.05))            # = 50，常数
        max_step = int(1000 × C([0, 0.5, 0.2, 5000]))  # 500 → 200 线性收缩
```

#### 4.5.3 源码精读

[base.py:174-178](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L174-L178)——分发时机：先刷 dataset（分辨率换挡），再对整棵组件树（含 guidance）做 `do_update_step`，发生在 `training_step` 之前，所以引导读到的 `self.global_step` 总是当前步：

```python
def on_train_batch_start(self, batch, batch_idx, unused=0):
    ...
    update_if_possible(self.dataset, self.true_current_epoch, self.true_global_step)
    self.do_update_step(self.true_current_epoch, self.true_global_step)
```

[stable_diffusion_bsd_guidance.py:1124-1134](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L1124-L1134)——BSD 的 `update_step` 全文：梯度裁剪引用了 Debiasing Scores and Prompts（arXiv:2303.15413）的稳定化技巧，但 texture 配置未设置 `grad_clip`，`grad_clip_val` 保持 `None`，[forward:1100-1101](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L1100-L1101) 的裁剪分支不生效（"代码存在不等于配置启用"，u6-l4 的老规矩）：

```python
def update_step(self, epoch: int, global_step: int, on_load_weights: bool = False):
    # clip grad for stable training as demonstrated in
    # Debiasing Scores and Prompts of 2D Diffusion for Robust Text-to-3D Generation
    # http://arxiv.org/abs/2303.15413
    if self.cfg.grad_clip is not None:
        self.grad_clip_val = C(self.cfg.grad_clip, epoch, global_step)
    self.global_step = global_step
    self.set_min_max_steps(
        min_step_percent=C(self.cfg.min_step_percent, epoch, global_step),
        max_step_percent=C(self.cfg.max_step_percent, epoch, global_step),
    )
```

[dreamcraft3d-texture.yaml:84-86](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L84-L86)——`max_step_percent: [0, 0.5, 0.2, 5000]` 是四元组 `[start_step, start_value, end_value, end_step]`（[misc.py:65-91](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/misc.py#L65-L91) 的 `C()` 按步数线性插值）：步 0 时 0.5、步 5000 时 0.2。于是 VSD 的 `t ∈ [50, 500→200]`，回灌采样强度同步从 0.5 降到 0.2——"先大刀阔斧改结构、后小步微调纹理"的课程策略：

```yaml
min_step_percent: 0.05
max_step_percent: [0, 0.5, 0.2, 5000]
only_pretrain_step: 1000
```

最后是闭环的"断点"一环：[dreamcraft3d.py:604-608](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L604-L608)——保存检查点时剔除所有 `guidance.*` 键，即两个可训练 UNet 的演化**不落盘**；恢复训练时教师与估计器回到初始 SD 权重重新自举，场景参数照常恢复。这也解释了 texture 阶段为何必须依赖 `system.geometry_convert_from` 而非 guidance 权重接力：

```python
def on_save_checkpoint(self, checkpoint):
    for k in list(checkpoint['state_dict'].keys()):
        if k.startswith("guidance."):
            checkpoint['state_dict'].pop(k)
```

#### 4.5.4 代码实践

**实践目标**：验证 `max_step` 的收缩轨迹及其对回灌强度的影响（纯计算，无需 GPU）。

**操作步骤**：运行下面的小脚本（示例代码；也可以直接在 4.1 的模拟脚本里加两列输出）：

```python
def C4(quad, step):                      # 复刻 misc.py 的四元组插值
    s0, v0, v1, s1 = quad
    return v0 + (v1 - v0) * max(min(1.0, (step - s0) / (s1 - s0)), 0.0)

for step in [0, 1250, 2500, 3750, 4999]:
    max_step = int(1000 * C4([0, 0.5, 0.2, 5000], step))
    print(step, "max_step =", max_step,
          "VSD t 区间 = [50,", max_step, "]",
          "回灌重画强度 =", max_step / 1000,
          "去噪步数 ≈", int(25 * max_step / 1000))
```

**需要观察的现象**：`max_step` 随步数线性下降；强度与去彩噪步数同步缩小。

**预期结果**：`(0, 500, 强度 0.5, 12 步) → (2500, 350, 0.35, 8 步) → (4999, 200, 0.2, 5 步)`。

#### 4.5.5 小练习与答案

**练习 1**：如果把 `only_pretrain_step` 改成 `0`，整个 BSD 引导退化成什么？

**答案**：系统侧与引导侧的判据都含 `only_pretrain_step > 0`，置 0 后预训练窗口永不开启：`train_pretrain` 永远不被调用，`train_unet` 停留在初始 SD 权重充当固定教师，剩下的正是标准 VSD（场景梯度 + `train_lora` 估计器训练）。这给出了一个干净的对照实验开关。

**练习 2**：为什么 `forward` 要把 `min_step/max_step` 也塞进返回的 `guidance_out`？

**答案**：它们随后被系统逐项 `self.log`（[dreamcraft3d.py:204-205](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L204-L205)），训练者可以在 TensorBoard 里直接看到时间步区间的收缩轨迹，无需另加日志——这是排查"蒸馏信号为何变弱"的第一现场。

## 5. 综合实践

把本讲三个方法与调度闭环串成一张完整的运行图。建议按以下三步完成：

1. **调度统计**：运行 4.1 的模拟脚本，确认 5000 步里 pretrain / VSD+LoRA / ref = 1000 / 2000 / 2000，并回答：若 `only_pretrain_step` 改为 500（占空比仍 1/5），窗口如何重排？（答：步 0–99、500–599、…共 10 个窗口各 100 步，采样步为每窗口内 `%25==0` 的 4 个，总采样 40 次不变，但缓存只能覆盖最近 10 帧、约 2.5 个窗口。）
2. **数据流注释**：完成 4.2.4、4.3.4、4.4.4 的三段注释后，把它们连成一份总表，特别标出三个"梯度开关"——`compute_grad_vsd` 的整体 `no_grad`（保护两个 UNet）、`train_lora` 的 `latents.detach()`（保护场景）、`train_pretrain` 的采样与编码全程 `no_grad`（保护一切）。检查表中每个 loss 的梯度去向是否唯一：`loss_sd`→场景外观、`loss_lora`→`train_unet_lora`、`loss_pretrain`→`train_unet`。
3. **本地验证（可选，无环境则标注「待本地验证」）**：按 README 跑 texture 阶段前几百步，在 TensorBoard 观察 `train/loss_pretrain` 只在预训练窗口出现、`train/loss_sd` 与 `train/loss_lora` 在其余奇数步成对出现、`train/loss_guidance_rgb` 只在偶数步（非窗口）出现；同时盯住 `.threestudio_cache/test_sample.jpg` 的更新节奏（每 25 步）。若观察到 `loss_pretrain` 出现在窗口之外或 `loss_sd` 出现在窗口之内，说明你对调度的理解有误——回去重查两侧判据。

## 6. 本讲小结

- BSD 的 `forward` 是路由器：`global_step % only_pretrain_step < only_pretrain_step//5` 命中时走 `train_pretrain` 并提前返回（场景零梯度），否则走 VSD 蒸馏 + `train_lora`；系统侧 `training_step` 用同一判据强制 guidance、关闭 ref，两侧必须严格同相位。
- `compute_grad_vsd` 实现 \( \text{grad} = (1-\bar\alpha_t)(\hat\epsilon_{\text{pretrain}} - \hat\epsilon_{\text{lora}}) \)：教师吃视角相关嵌入、估计器吃视角无关嵌入，两者都在 `no_grad` 内，梯度经 `target` 重参数化只流向场景；lora 侧两半输入相同使 CFG 融合退化为恒等，`v_prediction` 分支为 SD2.1（非 base）预留。
- `train_lora` 在当前渲染（detach）上做全时间步区间的去噪 MSE，让 `train_unet_lora` 拟合渲染分布的分数——VSD 成立的前提。
- `train_pretrain` 用（初始）冻结管线以当前渲染为种子、视角相关提示为指导做 SDEdit 重画，回灌缓存（≤10 帧、每 25 步采样）后对 `train_unet` 做 DreamBooth 式去噪训练，防止教师漂移遗忘；`pipe.unet` 的重绑使"冻结"仅第一次名副其实，后续为教师自采样。
- `update_step` 闭环每批同步 `global_step` 并用 `C()` 收缩时间步区间（[50, 500→200]），同时决定 VSD 采样与回灌强度；检查点不保存 guidance 权重，断点续训后教师与估计器从初始 SD 重新自举。

## 7. 下一步学习建议

下一讲（u7-l6）转向 texture 阶段的可选正则：`controlnet-guidance` 的 img2img 编辑与 `controlnet-reg-guidance` 配合 LPIPS 感知损失（`lambda_reg`）如何约束纹理不漂移——你会看到它与本讲 `train_pretrain` 的回灌思想互为镜像（一个给教师供数据、一个直接约束场景）。若想巩固本讲，建议精读 [stable_diffusion_bsd_guidance.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py) 中两个休眠通道（`compute_grad_du` 与 `compute_grad_vsd_hifa`）并思考它们与本讲主路径的关系；论文侧可对照 VSD（ProlificDreamer）与 Debiasing Scores and Prompts（arXiv:2303.15413）的梯度裁剪技巧，看代码注释如何把它们缝进 BSD。

# u6-l2 training_step：参考监督与扩散引导的交替调度

## 1. 本讲目标

上一讲（u6-l1）我们看清了 `dreamcraft3d-system` 在 `configure` 阶段装了哪些组件；本讲回答下一个自然的问题：**训练时每一步到底执行什么？**

DreamCraft3D 的每个训练步并不只是「前向 → 算损失 → 反向」这么简单。系统在同一份数据上，需要在两种互相牵制的监督信号之间做调度：

- **参考图监督（ref 子步）**：渲染参考视角，与输入图的 RGB/mask/深度/法向做逐像素回归，提供「长得像输入图」的强约束；
- **扩散引导（guidance 子步）**：渲染随机视角，交给 DeepFloyd / Zero123 / BSD 等扩散先验打分，提供「从其他角度看也合理」的弱约束。

学完本讲，你应该能够：

1. 逐行解释 `training_step` 中 `accumulate` 与 `alternate` 两种调度策略的差异；
2. 说出 `freq.n_ref`、`freq.ref_only_steps`、`freq.no_diff_steps` 各自控制什么；
3. 解释 `only_pretrain_step` 如何让系统侧调度器与 BSD 引导内部的预训练分支「双向联动」；
4. 解释 geometry 阶段 `freq.n_rgb` 驱动的 rgb/normal 双渲染类型切换；
5. 用一个不依赖 GPU 的模拟脚本复现整套调度逻辑，并预测任意配置下的执行比例。

## 2. 前置知识

### 2.1 两种训练子步（training_substep）

`training_step` 是调度器，真正干活的是 `training_substep(batch, batch_idx, guidance, render_type)`。参数 `guidance` 只取两个值：

- `"ref"`：使用 batch 顶层的**参考视角**数据（`rgb`/`mask`/`ref_depth`/`ref_normal` 与参考相机矩阵）；
- `"guidance"`：把 batch 切换到 `batch["random_camera"]` 子字典，使用**随机采样相机**。

上一讲（u4-l2）已经讲过这个双层 batch 结构，本讲直接消费该结论。

### 2.2 `global_step` 与 `true_global_step`

PyTorch Lightning 的 `LightningModule` 自带 `self.global_step` 计数器。threestudio 在其上包了一层 `true_global_step`：正常训练时二者相等；只有在「加载检查点做评估/导出」这种 Lightning 计数器归零的场景下，才返回检查点里记录的真实步数。

参见 [threestudio/systems/base.py:69-74](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L69-L74)——这是一个属性（property），`_resumed_eval` 为假时直接返回 `self.global_step`。

调度相关的判断大多用 `true_global_step`（可信时间源），但本讲会指出一处例外：`only_pretrain_step` 分支用的是裸的 `self.global_step`。

### 2.3 `freq` 是一个普通 dict，不是结构化配置

参见 [threestudio/systems/dreamcraft3d.py:28](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L28)：`freq: dict = field(default_factory=dict)`。

这意味着 `freq` 里的键**没有默认值、没有类型校验**——配置里写了什么就是什么，访问不存在的键会直接 `KeyError`。代码因此采取了「按阶段访问」的防御写法：只有 `stage == "geometry"` 时才去读 `freq.n_rgb`，所以 coarse/texture 两份配置里没有 `n_rgb` 也不会报错。

### 2.4 为什么要「调度」而不是「同时全算」？

看一眼 coarse 配置里两类损失的权重（[configs/dreamcraft3d-coarse-nerf.yaml:125-138](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L125-L138)）：

- `lambda_rgb: 1000.0`（参考图逐像素 MSE，量级巨大）
- `lambda_sd: 0.1` / `lambda_3d_sd: 0.1`（扩散蒸馏损失，量级很小）

如果每一步把两类损失加在一起反传，参考图的强监督会瞬间压过扩散先验的弱梯度，模型退化为「只会在参考视角贴图」的过拟合。把两种监督**分步施加**（alternate），让优化器每步只面对一个目标，是平衡「参考图保真 vs 扩散先验」的直接工程手段。这也正是本讲实践任务要定量验证的对象。

## 3. 本讲源码地图

| 文件 | 本讲关注点 |
| --- | --- |
| [threestudio/systems/dreamcraft3d.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py) | 主角：`training_step`（调度）、`training_substep`（子步内部门槛与 `render_type` 消费） |
| [configs/dreamcraft3d-coarse-nerf.yaml](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml) | coarse 阶段 `freq` 块（alternate，n_ref=2） |
| [configs/dreamcraft3d-geometry.yaml](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-geometry.yaml) | geometry 阶段 `freq` 块（accumulate，n_rgb=4） |
| [configs/dreamcraft3d-texture.yaml](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml) | texture 阶段 `freq` 块与 BSD 的 `only_pretrain_step: 1000` |
| [threestudio/models/guidance/stable_diffusion_bsd_guidance.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py) | BSD 引导侧的 `do_update_pretrain` 分支与 `train_pretrain` |
| [threestudio/systems/base.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py) | `true_global_step` 的定义、`on_train_batch_start` 中 update_step 分发 |

## 4. 核心概念与源码讲解

### 4.1 总调度：`ref_or_guidance` 的 accumulate / alternate 与 `n_ref` 频率控制

#### 4.1.1 概念说明

`training_step` 的第一件事是决定本步执行哪些子步，由 `freq.ref_or_guidance` 一刀切成两种策略：

- **`accumulate`（累积）**：每一步两个子步都跑，损失直接相加。geometry 阶段用它。
- **`alternate`（交替）**：每一步只跑其中一个，二选一。coarse 与 texture 阶段用它。

alternate 模式下由两个参数共同决定「本步是不是 ref 步」：

- `freq.ref_only_steps`：开荒期长度。训练前 N 步**只做参考监督**、不做扩散引导——此时场景还是一团雾，扩散先验没有意义，先把大致形状/颜色贴出来。
- `freq.n_ref`：开荒期之后，每 N 步安排 1 步 ref 监督，其余步做扩散引导。`n_ref=2` 即 ref 与 guidance 一比一交替；`n_ref=4` 则扩散引导占 3/4。

#### 4.1.2 核心流程

用伪代码描述 alternate 判定（`step` 指 `true_global_step`）：

```text
若 ref_or_guidance == "accumulate":
    do_ref = True; do_guidance = True
若 ref_or_guidance == "alternate":
    do_ref = (step < ref_only_steps) 或 (step % n_ref == 0)
    do_guidance = not do_ref
    # （only_pretrain_step 改道逻辑见 4.2，此处略）

render_type = "rgb"（stage != geometry 时恒为 rgb，见 4.4）

total_loss = 0
若 do_guidance: total_loss += guidance 子步损失（随机相机视角）
若 do_ref:     total_loss += ref 子步损失（参考视角）
```

注意执行顺序：**guidance 子步在前、ref 子步在后**。accumulate 模式下两份损失都进入 `total_loss` 并一起反传。

#### 4.1.3 源码精读

调度器本体在 [threestudio/systems/dreamcraft3d.py:344-357](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L344-L357)：

```python
def training_step(self, batch, batch_idx):
    if self.cfg.freq.ref_or_guidance == "accumulate":
        do_ref = True
        do_guidance = True
    elif self.cfg.freq.ref_or_guidance == "alternate":
        do_ref = (
            self.true_global_step < self.cfg.freq.ref_only_steps
            or self.true_global_step % self.cfg.freq.n_ref == 0
        )
        do_guidance = not do_ref
```

这段就是两种策略的全部判定逻辑。coarse 阶段的 `freq` 配置见 [configs/dreamcraft3d-coarse-nerf.yaml:113-118](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L113-L118)：

```yaml
freq:
  n_ref: 2
  ref_only_steps: 0
  ref_or_guidance: "alternate"
  no_diff_steps: 0
  guidance_eval: 0
```

即 coarse 默认：不开荒（`ref_only_steps: 0`，但注意 step 0 满足 `0 % 2 == 0`，所以第 0 步仍是 ref 步）、偶数步 ref、奇数步 guidance。

geometry 阶段则切换为 accumulate，见 [configs/dreamcraft3d-geometry.yaml:87-93](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-geometry.yaml#L87-L93)：

```yaml
freq:
  n_ref: 2
  ref_only_steps: 0
  ref_or_guidance: "accumulate"
  no_diff_steps: 0
  guidance_eval: 0
  n_rgb: 4
```

accumulate 下 `n_ref`/`ref_only_steps` 实际不再起作用（两个子步恒都执行），多出来的 `n_rgb` 才是主角（见 4.4）。

子步的执行与汇总在 [threestudio/systems/dreamcraft3d.py:364-374](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L364-L374)：

```python
total_loss = 0.0

if do_guidance:
    out = self.training_substep(batch, batch_idx, guidance="guidance", render_type=render_type)
    total_loss += out["loss"]

if do_ref:
    out = self.training_substep(batch, batch_idx, guidance="ref", render_type=render_type)
    total_loss += out["loss"]

self.log("train/loss", total_loss, prog_bar=True)
```

而 `training_substep` 开头完成了「按子步类型切换相机」的动作，见 [threestudio/systems/dreamcraft3d.py:97-109](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L97-L109)：先取出参考视角的 `mvp_mtx`/`c2w4x4`，若 `guidance == "guidance"` 就把整个 batch 换成 `batch["random_camera"]`，再把参考矩阵以 `mvp_mtx_ref`/`c2w_ref` 的名字注入回去（供渲染器计算参考视角可见性 mask）。这正是 u6-l1/u4-l2 已建立的双层 batch 约定的消费现场。

#### 4.1.4 代码实践

**实践目标**：不依赖 GPU，用 20 行脚本复现 alternate 判定逻辑，定量验证「ref 与 guidance 一比一交替」以及 `n_ref=4` 时的比例变化。

**操作步骤**（以下为示例代码，非项目原有文件，可保存为仓库外任意位置的 `simulate_schedule.py`）：

```python
def simulate(n_steps, n_ref=2, ref_only_steps=0, only_pretrain_step=0):
    stats = {"ref": 0, "guidance": 0, "pretrain": 0}
    for step in range(n_steps):
        do_ref = (step < ref_only_steps) or (step % n_ref == 0)
        do_guidance = not do_ref
        if only_pretrain_step > 0 and (step % only_pretrain_step) < (only_pretrain_step // 5):
            do_guidance, do_ref = True, False   # BSD 预训练改道，见 4.2
            stats["pretrain"] += 1
        stats["ref" if do_ref else "guidance"] += 1
    return {k: (v, v / n_steps) for k, v in stats.items()}

if __name__ == "__main__":
    print("coarse 默认 n_ref=2:", simulate(100, n_ref=2))
    print("改 n_ref=4        :", simulate(100, n_ref=4))
```

**需要观察的现象**：三组数字——ref 步数、guidance 步数、pretrain 步数（本实践中 `only_pretrain_step=0`，恒为 0）。

**预期结果**（纯 Python 逻辑推演，可直接运行验证）：

- `n_ref=2` 跑 100 步：ref 50 步（step 0, 2, …, 98）、guidance 50 步，比例 1:1；
- `n_ref=4` 跑 100 步：ref 25 步（step 0, 4, …, 96）、guidance 75 步，比例 1:3。

**解读**：把 `n_ref` 从 2 调到 4，等于把「参考图保真」的预算砍半、全部转给「扩散先验」。对输入图需要严格复刻的案例，`n_ref` 宜小；对希望模型自由补全不可见区域的案例，`n_ref` 宜大。

#### 4.1.5 小练习与答案

**练习 1**：若把 coarse 配置改成 `ref_only_steps: 100`、`n_ref: 2`，前 5 步（step 0~4）各执行什么子步？100 步内 ref/guidance 各多少步？

答案：step 0~4 全部满足 `step < 100`，因此全是 ref 步；step 0~99 全部是 ref 步（100 步 guidance 为 0）；step 100 起进入交替，偶数步 ref、奇数步 guidance。

**练习 2**：为什么 `ref_only_steps` 的判断用 `self.true_global_step` 而不是 Lightning 的 `self.global_step`？

答案：`true_global_step` 在「从检查点恢复训练/评估」时仍返回检查点记录的真实累计步数（见 [threestudio/systems/base.py:69-74](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L69-L74)）；若用裸 `global_step`，恢复训练后计数器从 0 重新数，会把开荒期、`n_ref` 相位全部错乱，等于重放一遍早期调度。

**练习 3**：accumulate 模式下，geometry 配置里的 `n_ref: 2` 还有作用吗？

答案：没有。accumulate 分支直接令 `do_ref = do_guidance = True`（[threestudio/systems/dreamcraft3d.py:345-347](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L345-L347)），`n_ref`/`ref_only_steps` 只在 alternate 分支被读取。geometry 配置保留它们只是沿用模板、不起作用。

### 4.2 `only_pretrain_step`：与 BSD 预训练联动的强制改道

#### 4.2.1 概念说明

texture 阶段的主引导是 BSD（`stable-diffusion-bsd-guidance`，详见 u7-l4/u7-l5）。BSD 的核心是「自举」：用当前渲染图去 DreamBooth 式地微调一个专属 LoRA 扩散模型，再用这个被个性化的模型反过来给场景打分（VSD 梯度）。**LoRA 需要先「预热」出对这只汉堡的基本认知，VSD 蒸馏才有意义。**

为此 BSD 引导提供了一个预训练模式，并且系统侧的 `training_step` 专门写了一段联动逻辑：当本步落在「预训练窗口」内时，**强制改成只跑 guidance 子步、不跑 ref 子步**；与此同时 BSD 引导内部检测到同一个窗口，自动改走 `train_pretrain` 分支——不做 VSD、不训场景，只训 LoRA。

窗口的定义是一个模运算：设 `only_pretrain_step = N`，则每 N 步为一个周期，每周期**前 N/5 步**是预训练窗口。texture 配置取 `N = 1000`，即每 1000 步里前 200 步（step 0~199、1000~1199、2000~2199……）是预训练窗口，其余 800 步回到正常的 alternate 交替。

#### 4.2.2 核心流程

```text
系统侧（training_step，在 alternate 判定之后）:
    若 guidance.cfg 有 only_pretrain_step 且 > 0:
        若 (global_step % N) < N // 5:
            do_guidance = True; do_ref = False     # 强制改道

BSD 侧（guidance.__call__，进入 guidance 子步后）:
    do_update_pretrain = (N > 0) 且 (global_step % N) < N // 5
    若 do_update_pretrain:
        仅执行 train_pretrain（DreamBooth 式训练 LoRA），直接 return
        —— 无 VSD 梯度、无 train_lora、场景参数得不到任何梯度
    否则:
        compute_grad_vsd（VSD 蒸馏梯度 → 场景）+ train_lora（继续微调 LoRA）
```

窗口内每 `per_update_pretrain_step`（默认 25）步，用**冻结的原始扩散模型**（`pipe_fix`）以当前渲染为起始 latents 采样一张新图存入缓存；其余预训练步从缓存（最多 10 帧）随机取一帧，加噪后训练 LoRA 去噪。这是「用冻结模型采样回灌」防止 LoRA 灾难性遗忘的机制，细节属于 u7-l5 的范围。

#### 4.2.3 源码精读

系统侧联动逻辑在 [threestudio/systems/dreamcraft3d.py:354-357](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L354-L357)：

```python
if hasattr(self.guidance.cfg, "only_pretrain_step"):
    if (self.guidance.cfg.only_pretrain_step > 0) and (self.global_step % self.guidance.cfg.only_pretrain_step) < (self.guidance.cfg.only_pretrain_step // 5):
        do_guidance = True
        do_ref = False
```

三个值得注意的细节：

1. `hasattr` 探测：deep-floyd-guidance 的 Config 里**没有** `only_pretrain_step` 字段（可在 [threestudio/models/guidance/deep_floyd_guidance.py:21-24](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/deep_floyd_guidance.py#L21-L24) 的 Config 定义中确认，其中并无该字段），所以 coarse/geometry 阶段这段代码直接跳过；只有 BSD 引导（Config 见 [threestudio/models/guidance/stable_diffusion_bsd_guidance.py:72-73](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L72-L73)，默认 `per_update_pretrain_step: 25`、`only_pretrain_step: 1000`）会触发。这样新增引导无需改动系统代码。
2. 这里用的是裸 `self.global_step` 而非 `true_global_step`——与 4.1 中 `do_ref` 的判断不一致。正常训练时二者相等，行为无差别；这是一个可以留意的写法不一致。
3. 改道方向是「只 guidance、不 ref」：预训练窗口里参考图损失也不算，因为这一步的目标只有 LoRA。

BSD 侧的对称判断在 [threestudio/models/guidance/stable_diffusion_bsd_guidance.py:1079-1092](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L1079-L1092)：

```python
do_update_pretrain = (self.cfg.only_pretrain_step > 0) and (
    (self.global_step % self.cfg.only_pretrain_step) < (self.cfg.only_pretrain_step // 5)
)

guidance_out = {}
if do_update_pretrain:
    sample_new_img = self.global_step % self.cfg.per_update_pretrain_step == 0
    loss_pretrain = self.train_pretrain(latents, text_embeddings_vd, camera_condition, sample_new_img=sample_new_img)
    guidance_out.update({...})
    return guidance_out          # 提前返回：跳过 compute_grad_vsd 与 train_lora
```

注意 BSD 内部用的是**自己的** `self.global_step`，它由 `update_step` 同步而来（[threestudio/models/guidance/stable_diffusion_bsd_guidance.py:1124-1134](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L1124-L1134) 中 `self.global_step = global_step`）。而系统在每个训练批次开始时通过 `on_train_batch_start → do_update_step` 把 `true_global_step` 递给所有 Updateable 子模块（见 [threestudio/systems/base.py:174-178](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L174-L178)）。于是「系统侧窗口」与「BSD 侧窗口」看的是同一个步数，改道才能严丝合缝。

texture 配置中的对应项：`only_pretrain_step: 1000` 见 [configs/dreamcraft3d-texture.yaml:86](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L86)，其损失权重 `lambda_pretrain: 0.1` 见 [configs/dreamcraft3d-texture.yaml:126](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L126)。`train_pretrain` 的采样缓存逻辑（`pipe_fix` 采样、`cache_frames` 最多 10 帧、随机取帧）见 [threestudio/models/guidance/stable_diffusion_bsd_guidance.py:941-978](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L941-L978)。

#### 4.2.4 代码实践

**实践目标**：用 4.1 的模拟脚本加入 `only_pretrain_step=1000`，观察 1000 步内三类步（ref / guidance-蒸馏 / pretrain）的分布，理解「前 200 步纯预训练」的含义。

**操作步骤**：在 4.1.4 的脚本中加一行调用：

```python
print("texture N=1000:", simulate(1000, n_ref=2, only_pretrain_step=1000))
```

**需要观察的现象**：`pretrain` 计数是否恰好 200；这 200 步落在哪些 step 上；剩余 800 步里 ref 与 guidance 的比例。

**预期结果**（可直接运行验证）：

- pretrain = 200 步：step 0~199 满足 `(step % 1000) < 200`，全部被改道；
- step 200~999 回到 alternate（n_ref=2）：偶数步 ref 共 400 步，奇数步 guidance 共 400 步；
- 汇总：ref 400、guidance 600（其中 200 步在 BSD 内部实际只做 `train_pretrain`，场景无 VSD 梯度），比例 2:3。

**解读**：预训练窗口内场景参数收不到任何梯度（`train_pretrain` 里采样图与 latents 均 `detach`，见 [threestudio/models/guidance/stable_diffusion_bsd_guidance.py:980-981](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L980-L981)）——这些步完全服务于「把 LoRA 训成这只物体的专属先验」，为随后的 800 步 VSD 蒸馏提供高质量的打分模型。这就是「自举」在时间轴上的形态：**先花 20% 的预算造尺子，再用尺子量 80% 的步数**。

#### 4.2.5 小练习与答案

**练习 1**：若把 `only_pretrain_step` 改为 500，每 1000 步有多少 pretrain 步？窗口如何分布？

答案：`N // 5 = 100`，每 500 步一个周期、前 100 步为窗口，1000 步共 200 个 pretrain 步（step 0~99、500~599）。总预算不变，但窗口从「一大块」变成「两小块」，LoRA 预训练与 VSD 蒸馏的切换更频繁。

**练习 2**：为什么系统侧改道要把 `do_ref` 也置为 False？保留 ref 监督同时训 LoRA 不行吗？

答案：技术上可行（ref 损失只作用于场景参数，与 LoRA 训练互不干扰），但那样优化器一步内要同时反传两份损失，且场景会在「LoRA 尚未预热」的阶段继续被参考图拉扯。代码选择让预训练窗口成为纯粹的 LoRA 训练段（参见 [threestudio/systems/dreamcraft3d.py:356-357](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L356-L357) 的 `do_guidance = True; do_ref = False`），职责更清晰、显存也更省（不需要为 ref 子步再渲染一次）。

**练习 3**：系统侧判断用 `self.global_step`，BSD 侧用自己的 `self.global_step`（由 update_step 同步）。两者何时可能不同步？

答案：正常训练时，系统每批 `on_train_batch_start` 把 `true_global_step` 传给 BSD 的 `update_step`（[threestudio/systems/base.py:174-178](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L174-L178)），二者一致。若从检查点恢复后 Lightning 的 `global_step` 与检查点记录值出现偏差（例如 `--validate`/`--export` 模式下经 `set_system_status` 恢复，此时 `true_global_step` 走检查点值而裸 `global_step` 归零），系统侧的裸 `global_step` 判断就可能与 BSD 侧错位——这是阅读时值得留意的潜在坑。

### 4.3 子步内部的门槛：`no_diff_steps`、执行顺序与损失汇总

#### 4.3.1 概念说明

调度器决定「跑哪些子步」之后，`training_substep` 内部还有两道门槛与一个汇总约定：

- **`no_diff_steps`（扩散静默期）**：即使本步被调度为 guidance 步，也要求 `true_global_step > no_diff_steps` 才真正调用扩散模型。这是「几何还没成型前不给扩散梯度」的兜底开关。
- **guidance_eval**：`freq.guidance_eval > 0` 时，每隔 N 步让引导模型额外做一次多 CFG 尺度的评估采样并存图。三份配置里均为 0，即从不触发，本讲不展开。
- **正则化损失的双份执行**：正则项（normal_smooth、sparsity、orient 等）写在子步函数体内、不属于 ref 分支也不属于 guidance 分支，因此 accumulate 模式下每步会被计算**两次**（两个子步各一遍），只有 `sparsity` 例外地带了 `guidance != "ref"` 条件。

#### 4.3.2 核心流程

```text
training_substep(guidance="guidance"):
    若 true_global_step <= no_diff_steps: 跳过扩散引导（该分支整体不执行）
    否则:
        guidance_inp = comp_rgb（或 geometry+normal 步的 comp_normal，见 4.4）
        loss_sd  ← self.guidance(...)        # 主引导（DeepFloyd / BSD）
        若 guidance_3d 不为 None:
            loss_3d_sd ← self.guidance_3d(...)  # Zero123 视图先验

汇总（两个子步共用）:
    loss = Σ 各损失项 × self.C(lambda_xxx)    # C() 为步数感知插值（u8-l1）
```

#### 4.3.3 源码精读

`no_diff_steps` 门槛在 guidance 分支的入口处，见 [threestudio/systems/dreamcraft3d.py:191-207](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L191-L207)：

```python
elif guidance == "guidance" and self.true_global_step > self.cfg.freq.no_diff_steps:
    if self.cfg.stage == "geometry" and render_type == "normal":
        guidance_inp = out["comp_normal"]
    else:
        guidance_inp = out["comp_rgb"]
    guidance_out = self.guidance(
        guidance_inp, prompt_utils, **batch,
        rgb_as_latents=False, guidance_eval=guidance_eval,
        mask=out["mask"] if "mask" in out else None,
    )
```

注意 `elif` 的条件组合：`guidance == "guidance"` 且步数越过静默期，整段扩散引导才会执行；否则该子步只剩正则项。

三份配置的取值对比：

| 配置 | `no_diff_steps` | 效果 |
| --- | --- | --- |
| [coarse-nerf:117](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L117) | `0` | 仅 step 0 静默（而 step 0 在 alternate 下本来就是 ref 步，实际不生效，属兜底） |
| [geometry:91](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-geometry.yaml#L91) | `0` | 同上 |
| [texture:115](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L115) | `-1` | `step > -1` 恒真——texture 阶段每一步都要扩散参与（BSD 蒸馏就是本阶段主角） |

主引导之后紧接着 Zero123 双引导的第二通道，见 [threestudio/systems/dreamcraft3d.py:209-224](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L209-L224)：`guidance_3d` 只吃 `out["comp_rgb"]` 与相机条件，产出以 `3d_` 前缀写入损失（如 `loss_3d_sd` 对应权重 `lambda_3d_sd`）。texture 配置里 `guidance_3d_type` 被注释掉（[configs/dreamcraft3d-texture.yaml:88-98](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L88-L98)），且 `lambda_3d_sd: 0.0`（[configs/dreamcraft3d-texture.yaml:127](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L127)）双保险关闭。

损失权重乘法与日志在 [threestudio/systems/dreamcraft3d.py:321-334](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L321-L334)：每项损失乘 `self.C(lambda_xxx)`（C() 是 u8-l1 将讲的步数感知插值，能让权重随训练进度变化），再叠加。`sparsity` 正则只归属 guidance 子步的写法见 [threestudio/systems/dreamcraft3d.py:267-268](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L267-L268) 的 `if guidance != "ref" and ...`——因为参考视角下场景本来就应当填满视野，惩罚稀疏没有意义。

#### 4.3.4 代码实践

**实践目标**：通过源码阅读 + 日志字段核对，把「一个训练步在日志里长什么样」与调度逻辑对上。

**操作步骤**：

1. 打开 [threestudio/systems/dreamcraft3d.py:321-334](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L321-L334)，记下日志命名规则：子步内每项损失记为 `train/loss_{guidance}_{name}`（如 `train/loss_guidance_sd`、`train/loss_ref_rgb`），子步总损失记为 `train/loss_ref` / `train/loss_guidance`，整步总损失记为 `train/loss`。
2. 推演：alternate 模式下，偶数步只有 `train/loss_ref*` 系列，奇数步只有 `train/loss_guidance*` 系列；accumulate 模式下两组同时出现。
3. 若有 GPU 环境与已跑过的 trial，打开 `outputs/<name>/<tag>/tb_logs`（TensorBoard）或 `csv_logs`，按 step 查看 `train/loss_ref` 与 `train/loss_guidance` 是否交替出现。

**需要观察的现象**：TensorBoard 中 `train/loss_ref` 与 `train/loss_guidance` 两条曲线的 x 轴覆盖是否互补（alternate）或重合（accumulate）；`train/loss_ref_rgb` 在 geometry 阶段是否只在 step % 4 == 0 的步上有点（配合 4.4）。

**预期结果**：coarse 阶段两条曲线互补出现；geometry 阶段重合。日志核对部分**待本地验证**（需要完成过一次训练的 GPU 环境）。

#### 4.3.5 小练习与答案

**练习 1**：texture 配置为什么把 `no_diff_steps` 设成 `-1` 而不是 `0`？

答案：texture 阶段到达时几何已由前三阶段定型，扩散引导（BSD）正是本阶段唯一的先验来源，没有任何理由静默；设 `-1` 使 `true_global_step > -1` 对包括 step 0 在内的所有步恒真。设 `0` 会让 step 0（alternate 下恰好是 ref 步）不受影响、但若配合 `--resume` 从 step 0 重放则可能吃掉一步引导，`-1` 是更明确的「永不静默」。

**练习 2**：accumulate 模式下正则项被算两次，是否意味着正则的实际权重翻倍？

答案：从梯度上看是的——同一正则（如 `lambda_orient` 对应项）在 guidance 子步和 ref 子步各前向一次、各反传一次，对参数的累计梯度约等于两倍权重（Adam 的归一化会部分吸收这种缩放）。这是阅读 accumulate 行为时需要意识到的隐含约定。

**练习 3**：`training_substep` 里 `guidance_eval` 的三个与条件（[threestudio/systems/dreamcraft3d.py:119-123](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L119-L123)）分别是什么？

答案：`guidance == "guidance"`（仅引导子步）、`self.cfg.freq.guidance_eval > 0`（功能开启）、`self.true_global_step % self.cfg.freq.guidance_eval == 0`（按周期对齐）。三者同时满足才做评估采样并存图。

### 4.4 geometry 阶段：`n_rgb` 驱动的 rgb/normal 双渲染类型切换

#### 4.4.1 概念说明

geometry 阶段的几何表示已换成 DMTet（u5-l4），此时雕刻表面的高效监督不是 RGB 图，而是**法向图（normal map）**：把渲染出的表面法向当作一张三通道图喂给扩散模型，让先验评判「这个形状的表面朝向是否合理」——这是 Magic3D 提出的法向引导思路，比 RGB 引导更直接地作用于几何。

于是 `training_step` 引入第三个调度维度 `render_type`：

- stage 为 `geometry` 时：`render_type = "rgb"` 当且仅当 `true_global_step % freq.n_rgb == 0`，其余步为 `"normal"`；
- 其他阶段恒为 `"rgb"`。

`n_rgb = 4` 意味着每 4 步里 1 步用 RGB 引导、3 步用法向引导——**几何阶段以法向雕刻为主（75%），RGB 引导为辅（25%）**。

`render_type` 在两处被消费：

1. **guidance 子步**：决定喂给扩散模型的是 `comp_rgb` 还是 `comp_normal`；
2. **ref 子步**：只有 `render_type == "rgb"` 才计算 RGB/mask/深度系列损失（法向损失块在条件外，但 geometry 配置 `lambda_normal: 0.0` 已关闭）。

#### 4.4.2 核心流程

```text
若 stage == "geometry":
    render_type = "rgb"   若 step % n_rgb == 0
    render_type = "normal" 其余步
否则:
    render_type = "rgb"

guidance 子步消费:
    stage==geometry 且 render_type=="normal" → guidance_inp = comp_normal
    否则                                    → guidance_inp = comp_rgb

ref 子步消费:
    render_type == "rgb" → 计算 rgb/mask/depth 损失
    render_type == "normal" → 跳过上述损失（本配置下 ref 子步近乎空转，
                              只剩正则项与网格正则 normal_consistency 等）
```

#### 4.4.3 源码精读

`render_type` 的判定在 [threestudio/systems/dreamcraft3d.py:359-362](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L359-L362)：

```python
if self.cfg.stage == "geometry":
    render_type = "rgb" if self.true_global_step % self.cfg.freq.n_rgb == 0 else "normal"
else:
    render_type = "rgb"
```

注意这里用 `self.cfg.freq.n_rgb`——如 2.3 所述，`freq` 是普通 dict，coarse/texture 配置没写 `n_rgb`，但代码只在 `stage == "geometry"` 分支内访问，天然规避了 `KeyError`。

guidance 子步的消费点即 4.3.3 引用的 [threestudio/systems/dreamcraft3d.py:192-195](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L192-L195)：

```python
if self.cfg.stage == "geometry" and render_type == "normal":
    guidance_inp = out["comp_normal"]
else:
    guidance_inp = out["comp_rgb"]
```

ref 子步的消费点在 [threestudio/systems/dreamcraft3d.py:127-128](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L127-L128)：`if guidance == "ref":` 之下嵌套 `if render_type == "rgb":`，RGB/mask/深度损失全部在内层。而法向损失块（[threestudio/systems/dreamcraft3d.py:176-189](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L176-L189)）在内层之外——不受 `render_type` 限制。

geometry 配置的调度参数组合见 [configs/dreamcraft3d-geometry.yaml:87-93](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-geometry.yaml#L87-L93)：accumulate + `n_rgb: 4`。两个维度叠加后，geometry 阶段一个 4 步周期的时间线是：

| step | 子步（accumulate 全跑） | guidance 子步输入 | ref 子步 RGB 损失 |
| --- | --- | --- | --- |
| 0 | ref + guidance | `comp_rgb` | 计算（MSE） |
| 1 | ref + guidance | `comp_normal` | 跳过 |
| 2 | ref + guidance | `comp_normal` | 跳过 |
| 3 | ref + guidance | `comp_normal` | 跳过 |

（正则项每步照算；网格正则 `normal_consistency` 的权重调度 `[1000,10.0,1,2000]` 见 [configs/dreamcraft3d-geometry.yaml:111](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-geometry.yaml#L111)，由 C() 插值生效，属 u6-l4 范围。）

#### 4.4.4 代码实践

**实践目标**：推演并验证 geometry 阶段 12 步的完整执行时间线，理解「accumulate × render_type 交替」的二维调度。

**操作步骤**：

1. 复制下面的推演脚本（示例代码）运行：

```python
def timeline(n_steps=12, n_rgb=4):
    for step in range(n_steps):
        render_type = "rgb" if step % n_rgb == 0 else "normal"
        # geometry 用 accumulate：两个子步都跑
        inp = "comp_rgb" if render_type == "rgb" else "comp_normal"
        ref_rgb = "算" if render_type == "rgb" else "跳过"
        print(f"step {step:2d} | guidance_inp={inp:12s} | ref_rgb={ref_rgb}")

timeline()
```

2. 修改 `n_rgb=2` 再跑一次，对比 rgb 步占比。

**需要观察的现象**：rgb 步与 normal 步的出现规律；`n_rgb` 变小后 rgb 步密度翻倍。

**预期结果**（可直接运行验证）：`n_rgb=4` 时 step 0、4、8 为 rgb 步（占 1/4），其余为 normal 步；`n_rgb=2` 时 step 0、2、4、6、8、10 为 rgb 步（占 1/2）。

**解读**：geometry 阶段真正驱动顶点位移的主要是 75% 步数上的法向引导（作用于形状），RGB 引导与参考损失集中在少数 rgb 步上守住外观与参考一致性。若发现几何表面过度平滑、缺少细节，减小 `n_rgb`（提高 rgb 步占比）是一个可调旋钮；反之表面噪声大则增大 `n_rgb`。

#### 4.4.5 小练习与答案

**练习 1**：coarse 阶段的 `training_step` 会访问 `freq.n_rgb` 吗？为什么 coarse 配置里没有这个键也不报错？

答案：不会。访问发生在 `if self.cfg.stage == "geometry":` 分支内（[threestudio/systems/dreamcraft3d.py:359-360](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L359-L360)），coarse/texture 阶段走 else 分支直接得 `"rgb"`。`freq` 是无 schema 的 dict，只有真的访问缺失键才会 `KeyError`。

**练习 2**：normal 步上 ref 子步「近乎空转」，为什么还要执行？

答案：ref 子步函数体内还有不属于 rgb 条件块的公共部分：正则化损失（geometry 配置里权重多为 0，但代码路径存在）、日志记录，以及网格正则 `normal_consistency`/`laplacian_smoothness`（stage 分支，不受 render_type 影响，见 [threestudio/systems/dreamcraft3d.py:293-297](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L293-L297)）。且 ref 子步渲染的是参考视角，正常 rgb 步之外的这步渲染同时也为日志提供了参考视角画面。

**练习 3**：把 `comp_normal` 喂给 DeepFloyd 引导，文本提示词还是「一只汉堡」这样的语义描述，模型能理解吗？

答案：这正是该设计的巧妙之处——扩散模型在海量网络图像上预训练，其中包含大量法向图/雕塑照片，模型对「灰蓝色法向着色的物体」有一定先验；配合法向图天然凸显形状信息的特点，几何雕刻信号比 RGB 更直接。这是 Magic3D 论文提出的思路，DreamCraft3D 在 geometry 阶段沿用（`guidance_inp` 的选择逻辑本身不带任何文本变换，提示词由 prompt_utils 正常提供）。

## 5. 综合实践

把本讲四个模块串成一个完整的「调度模拟器」，一次性回答：**给定任意一份 freq 配置与 guidance 类型，前 1000 步的时间线长什么样？**

**任务**：编写 `simulate_full.py`（示例代码，建议放在仓库外，避免与源码混淆），整合 4.1~4.4 的全部逻辑：

```python
def simulate_full(n_steps, stage, n_ref, ref_only_steps, no_diff_steps,
                  n_rgb=None, guidance_has_pretrain=False, only_pretrain_step=0):
    """按 dreamcraft3d-system training_step 的真实判定顺序模拟调度。"""
    rows = []
    for step in range(n_steps):
        mode = "accumulate"
        # 1) 子步选择
        if mode == "accumulate":
            do_ref = do_guidance = True
        else:  # alternate
            do_ref = (step < ref_only_steps) or (step % n_ref == 0)
            do_guidance = not do_ref
        # 2) BSD 预训练改道（覆盖 alternate 的判定）
        pretrain = False
        if guidance_has_pretrain and only_pretrain_step > 0 \
                and (step % only_pretrain_step) < (only_pretrain_step // 5):
            do_guidance, do_ref, pretrain = True, False, True
        # 3) render_type
        render_type = ("rgb" if step % n_rgb == 0 else "normal") \
            if stage == "geometry" else "rgb"
        # 4) no_diff_steps 静默期（只作用于 guidance 子步的扩散部分）
        diff_active = step > no_diff_steps
        rows.append((step, do_ref, do_guidance, pretrain, render_type, diff_active))
    return rows

def summarize(rows):
    n = len(rows)
    return {
        "ref步占比": sum(r[1] for r in rows) / n,
        "guidance步占比": sum(r[2] for r in rows) / n,
        "pretrain步占比": sum(r[3] for r in rows) / n,
        "normal渲染占比": sum(r[4] == "normal" for r in rows) / n,
    }

if __name__ == "__main__":
    # 场景 A：coarse-nerf 默认（alternate, n_ref=2）
    print("coarse:", summarize(simulate_full(1000, "coarse", 2, 0, 0)))
    # 场景 B：geometry 默认（accumulate, n_rgb=4）
    print("geometry:", summarize(simulate_full(1000, "geometry", 2, 0, 0, n_rgb=4)))
    # 场景 C：texture 默认（alternate + BSD 预训练, no_diff_steps=-1）
    print("texture:", summarize(simulate_full(1000, "texture", 2, 0, -1,
                                              guidance_has_pretrain=True,
                                              only_pretrain_step=1000)))
```

**预期结果**（可直接运行验证）：

| 场景 | ref 步占比 | guidance 步占比 | pretrain 步占比 | normal 渲染占比 |
| --- | --- | --- | --- | --- |
| coarse | 50% | 50% | 0 | 0 |
| geometry | 100%（accumulate） | 100%（accumulate） | 0 | 75% |
| texture | 40% | 60% | 20% | 0 |

**分析要求**：对照结果回答——

1. coarse 阶段为什么敢让扩散引导占一半步数？（提示：结合 2.4 的权重数量级与 u1-l1 讲过的 Janus 问题——还需要 Zero123 双通道配合）
2. geometry 阶段 accumulate 下 ref/guidance 各 100%，叠加 75% normal 渲染后，「法向引导 × 参考 RGB 监督」在时间轴上如何错开？
3. texture 阶段 20% 的 pretrain 步意味着场景有 1/5 的步数完全不更新，这对训练稳定性的代价与收益各是什么？

**待本地验证**：以上比例为纯调度逻辑推演（不涉及随机数，结果确定）；若要在真实训练中核对，可在 coarse 阶段跑数百步后查看 TensorBoard 的 `train/loss_ref` / `train/loss_guidance` 出现频率，需要 GPU 环境。

## 6. 本讲小结

- `training_step` 是纯调度器：`freq.ref_or_guidance` 决定 accumulate（双跑）或 alternate（二选一），coarse/texture 用 alternate、geometry 用 accumulate。
- alternate 下 `do_ref = step < ref_only_steps 或 step % n_ref == 0`；`n_ref` 直接调节「参考图保真 vs 扩散先验」的预算配比，`ref_only_steps` 是只做参考监督的开荒期。
- `only_pretrain_step`（texture 配置为 1000）让系统侧强制 `do_guidance=True, do_ref=False`，同时 BSD 引导内部用同一模运算窗口改走 `train_pretrain`——每周期前 N/5 步只训 LoRA、场景零梯度，这是「先造尺子再量物体」的自举调度。
- `no_diff_steps` 是 guidance 子步内部的扩散静默期兜底（texture 设 -1 恒开）；guidance 子步里主引导与 `guidance_3d`（Zero123）先后执行，后者以 `3d_` 前缀计入损失。
- geometry 阶段叠加第三维度：`freq.n_rgb=4` 使 75% 的步以 `comp_normal` 作为扩散引导输入、ref 子步跳过 RGB 损失，形成「法向雕刻为主、RGB 守外观为辅」的二维调度。
- 子步执行顺序固定为 guidance 在前、ref 在后；正则项写在公共区域，accumulate 下会被计算两次——阅读日志与调权重时需要意识到的隐含约定。

## 7. 下一步学习建议

本讲只回答了「每步跑什么」，没有展开「损失具体怎么算」。建议顺序：

1. **u6-l3（training_substep 之一：参考图监督损失）**：逐项拆解 ref 子步的 RGB（MSE/L1+grow_mask）、mask、最小二乘尺度对齐的深度损失、Pearson 相对深度与法向余弦损失——这些正是本讲 ref 步里被调度的具体目标函数。
2. **u6-l4（training_substep 之二：正则化与阶段专属损失）**：本讲多次路过但未展开的 orient/sparsity/opaque/eikonal、网格正则 normal_consistency/laplacian_smoothness，以及 texture 阶段的 ControlNet 感知正则。
3. 想提前理解 `self.C(lambda_xxx)` 如何让损失权重随步数变化，可先跳读 u8-l1 的 C() 函数解析，再回看 [threestudio/systems/dreamcraft3d.py:321-334](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L321-L334) 的权重乘法。
4. 对 `train_pretrain` 内部的 `pipe_fix` 采样缓存与双管线结构好奇的读者，可直接进入 u7-l4/u7-l5 的 BSD 引导两讲，再回头看本讲的改道逻辑会有更完整的图景。

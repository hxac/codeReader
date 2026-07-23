# 优化器与学习率调度

## 1. 本讲目标

本讲承接 u6-l3「Trainer 与 TrainerController」，把镜头拉近到训练一步里**最容易被当成黑盒**的两件事：**优化器**与**学习率（LR）调度**。

读完本讲，你应当能够：

- 说清 SpecForge 的「优化器工厂」为何是一个**带状态的延迟对象**，而不是直接 `new` 出来的 `AdamW`；
- 描述 `CosineAnnealingWarmupLR` 的「线性 warmup + 余弦退火」两段式曲线，以及它如何与 `global_step` 对齐；
- 区分 `training.max_steps` 与 `training.total_steps` 这两个看起来很像、实则语义不同的字段，并说清**在线 producer 如何自己推导步数 horizon、consumer 又如何复用它**。

一句话定位：本讲讲的是「草稿模型每一步该往哪走、走多快、走多久」三件事的源码落点。

## 2. 前置知识

在进入源码前，先用三段话补齐直觉。

**第一，为什么要 bf16 训练 + fp32 主副本？** SpecForge 在显卡上用 bfloat16 存草稿权重（省显存、快），但优化器需要更精确的梯度更新。做法是：保留一份 **fp32 的主副本（master copy）**，优化器在 fp32 上算 AdamW 更新，更新完再拷回 bf16。这就是 `BF16Optimizer` 名字的由来——它不是「优化 bf16 的优化器」，而是「bf16 权重之上的 fp32 优化器」。

**第二，为什么要学习率调度？** 训练初期权重离最优解远，步子太大容易发散，所以先用较小的学习率**线性预热（warmup）**；中段用较大学习率快速逼近；末段逐步**退火（anneal）**到很小的值，做精细微调。`CosineAnnealingWarmupLR` 把这两段拼成一条连续曲线。

**第三，什么是「步数 horizon」？** 余弦退火需要一个**总步数 T** 作为分母，曲线才能从峰值滑到谷底。这个 T 就是 horizon。难点在于：训练到底跑多少步，有时用户直接给（`total_steps`），有时是一个上限（`max_steps`），有时要从数据量推出来，有时（在线）数据是流式的、根本不知道总量。SpecForge 用一组小函数把这些情况统一成一个确定性的整数。

> 关键术语回顾（来自 u6-l3）：`global_step` 只在**梯度累积边界**（一次真正的 optimizer step）自增；`optimizer_stepped` 是驱动检查点、评测、durable ack 的「单一权威边界信号」。本讲的「步数」全都指这种 optimizer step，而不是前向/反向次数。

## 3. 本讲源码地图

本讲涉及三个核心源码文件，外加两个把它们「接线」的装配文件：

| 文件 | 作用 | 本讲角色 |
| --- | --- | --- |
| `specforge/optimizer.py` | `BF16Optimizer`：fp32 主副本 + AdamW + 梯度裁剪 + 内嵌 LR 调度 | 优化器工厂的产物 |
| `specforge/lr_scheduler.py` | `CosineAnnealingWarmupLR`：warmup + 余弦退火两段式 | LR 调度的实现 |
| `specforge/training/schedule.py` | `resolve_total_steps` / `resolve_online_total_steps` 等 | 步数 horizon 的解析 |
| `specforge/training/trainer.py` | `_ConfiguredOptimizerFactory` + `effective_total_steps` 解析 | 工厂与 horizon 的接线点 |
| `specforge/training/disaggregated.py` | 在线 producer 写 schedule 记录、consumer 读它 | 在线 horizon 的跨进程握手 |

数据流总览（自下而上）：

```text
training.{total_steps,max_steps,num_epochs,batch_size,accumulation_steps,learning_rate,warmup_ratio,...}
        │
        ├── schedule.resolve_total_steps / resolve_online_total_steps   → 一个 horizon 整数
        │
        ├── _ConfiguredOptimizerFactory.configure_total_steps(horizon)  → 工厂记录 horizon
        │
        └── factory(draft_module) → BF16Optimizer(..., total_steps=horizon)
                                       └── 内部 new CosineAnnealingWarmupLR(total_steps, warmup_steps)
```

## 4. 核心概念与源码讲解

### 4.1 优化器工厂与 BF16Optimizer

#### 4.1.1 概念说明

SpecForge 不在装配阶段直接 `AdamW(...)`，而是先造一个**工厂对象**，等到草稿模块真正被 FSDP 包裹之后，再把包裹好的模块喂给工厂、由工厂 `__call__` 出真正的优化器。

为什么要延迟？因为有两个值在装配开始时还不知道：

1. **`total_steps`（horizon）**：可能要从数据量或在线 producer 推导，要到装配中段才解析出来；
2. **可训练参数**：FSDP 包裹会重塑参数，优化器必须在包裹**之后**看到真实的可训练张量。

所以工厂是一个「先收配方、后产出实例」的两段式对象。

#### 4.1.2 核心流程

`BF16Optimizer.step()` 一次完整的优化器步进：

```text
1. 算梯度范数：遍历可训练参数的 .grad，求平方和，跨进程组 all-reduce
2. 算裁剪系数：clip_coef = min(1, max_grad_norm / (total_norm + 1e-6))
3. 把 bf16 梯度搬到 fp32、乘以裁剪系数，挂到 fp32 主副本上
4. optimizer.step()        # fp32 上的 AdamW 更新
5. optimizer.zero_grad()
6. scheduler.step()        # 推进 LR（见 4.2）
7. 把 fp32 主副本拷回 bf16 权重，清掉 bf16 的 .grad
```

注意第 4、5、6 步是**紧挨着的三连**：优化器更新、清零梯度、推进学习率，每一步 `optimizer_stepped=True` 的边界都完整地走完这三件事。这正是 u6-l3 所说「`optimizer_stepped` 是单一权威边界信号」的物理实现——一次边界 = 一次 step 三连。

#### 4.1.3 源码精读

**构造函数：fp32 主副本 + 内嵌调度器**。`BF16Optimizer` 在构造时就 clone 出 fp32 主副本，并立刻用传入的 `total_steps`、`warmup_ratio` 建好 LR 调度器：

[specforge/optimizer.py:16-51](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/optimizer.py#L16-L51) —— 从 `model.parameters()` 里筛出 `requires_grad` 的参数，逐个 `.detach().clone().to(torch.float32)` 造主副本，再用这些 fp32 张量建 `torch.optim.AdamW`，最后用 `warmup_ratio * total_steps` 算出 warmup 步数、构造 `CosineAnnealingWarmupLR`。

> 默认值 `total_steps=800_000`、`warmup_ratio=0.015` 来自 EAGLE 原版 `traineagle3 ds_config.json`（见源码注释 `# defaults copied from EAGLE traineagle3 ds_config.json`）。注意这只是函数签名兜底；真实运行时这两个值由工厂用解析后的 horizon 覆盖。

**梯度裁剪：跨卡 all-reduce 平方和**。裁剪系数不是各算各的，而是先把梯度平方和在进程组上求和再开方：

[specforge/optimizer.py:84-96](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/optimizer.py#L84-L96) —— `_grad_norm_and_clip_coefficient` 在**模型参数自身的设备上**（而非 fp32 主副本上）算梯度范数，因为 NCCL 只能在原始 CUDA 设备上做 reduce。`configure_grad_norm_reduction`（L53-61）允许 FSDP 后端按分片组配置：对 NO_SHARD/复制的参数关闭归约，避免重复计算。

裁剪的数学：

\[
\text{total\_norm}=\sqrt{\sum_i \|g_i\|_2^2},\qquad
\text{clip\_coef}=\min\!\left(1,\ \frac{\text{max\_grad\_norm}}{\text{total\_norm}+\varepsilon}\right)
\]

当 `total_norm ≤ max_grad_norm` 时 `clip_coef=1`（不裁）；超出则按比例缩小梯度，把全局范数钳制到上限。

**step 三连：更新→清零→推进 LR**。关键在 L150-152 的三行紧挨着：

[specforge/optimizer.py:129-157](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/optimizer.py#L129-L157) —— 先算范数与裁剪系数、把裁剪后的 fp32 梯度挂到主副本，然后 `optimizer.step()` / `optimizer.zero_grad()` / `scheduler.step()` 三连，最后把更新后的 fp32 主副本拷回 bf16 并清掉 bf16 梯度。返回 `last_grad_norm` 供日志记录。

**工厂对象本身**：它是一个延迟产出的可调用对象，先收 horizon、后在 FSDP 包裹后产优化器：

[specforge/training/trainer.py:246-273](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/trainer.py#L246-L273) —— `_ConfiguredOptimizerFactory.__init__` 先从配置读 `total_steps or max_steps`；`configure_total_steps` 允许运行期回填 horizon（若之前是 `None`），且若前后不一致直接抛错（防止控制器与优化器各用一套 horizon）；`__call__` 在 horizon 仍未解析时抛 `RuntimeError`，否则用解析后的 `self.total_steps` 产出 `BF16Optimizer`。

注意 `__call__` 的入参是 `draft_module`——它就是 FSDP 包裹后的草稿子模块，工厂据此拿到真正可训练的参数。

#### 4.1.4 代码实践

**实践目标**：验证梯度裁剪系数的计算与跨卡归约开关。

**操作步骤**：

1. 打开 `tests/test_runtime/test_optimizer/test_bf16_optimizer_clip_grad_norm.py`，阅读其中构造 `BF16Optimizer` 并断言裁剪行为的用例。
2. 在你本地环境（需 GPU）运行该测试：
   ```bash
   pytest tests/test_runtime/test_optimizer/test_bf16_optimizer_clip_grad_norm.py -v
   ```
3. 阅读断言，对照本讲公式 \(\text{clip\_coef}=\min(1,\ \text{max\_grad\_norm}/(\text{total\_norm}+\varepsilon))\) 验证数值。

**需要观察的现象**：当人为放大梯度使 `total_norm > max_grad_norm` 时，`last_grad_norm` 应被钳到接近 `max_grad_norm`；当梯度较小时，裁剪不生效。

**预期结果**：测试通过，说明裁剪与归约实现与公式一致。

**若无法本地运行**：明确标注「待本地验证」，仅做源码阅读型实践——阅读上述测试断言，手算一组梯度平方和，核对其裁剪后范数。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `BF16Optimizer` 要在**模型参数自身的设备**上算梯度范数，而不是在 fp32 主副本（可能是 CPU）上算？

> **答案**：因为跨卡归约（`dist.all_reduce`）需要张量在 NCCL 能访问的设备上（CUDA）。若主副本被 offload 到 CPU（`offload_master=True`），在 CPU 上无法做 NCCL 归约。源码 L88-96 显式从 `model_params`（在 GPU 上）取梯度算范数，再把标量结果搬到归约设备，正是为此。

**练习 2**：`configure_grad_norm_reduction` 在什么情况下会把 `enabled` 设为 `False`？

> **答案**：对 DDP/未包裹（`NO_SHARD`）的复制参数，梯度在各卡上本就相同，再 all-reduce 是冗余；FSDP 后端对这类参数关闭归约（见 L57-58 注释 "FSDP backends disable the reduction for replicated/NO_SHARD parameters"）。

---

### 4.2 学习率调度 CosineAnnealingWarmupLR

#### 4.2.1 概念说明

`CosineAnnealingWarmupLR` 是一条**两段式**曲线：前 `warmup_steps` 步线性上升，之后按余弦衰减到 `eta_min`（默认 0）。它通过组合两个 PyTorch 基类实现：外层 `_WarmupScheduler` 负责 warmup 阶段，内层 `_CosineAnnealingLR` 负责退火阶段，两者用一个 `finished` 标志衔接。

这种「先升后降」的形状能让训练既不因初期步子过大而发散，也不因末期学习率过高而在最优点附近来回震荡。

#### 4.2.2 核心流程

两段曲线的数学定义。设峰值学习率为 `base_lr`、warmup 步数为 \(W\)、退火步数为 \(T_d = \text{total\_steps} - W\)、当前步为 \(t\)（从 0 起计的 `last_epoch`）：

**Warmup 段**（\(0 \le t < W\)）线性上升：

\[
\text{lr}(t) = \text{base\_lr} \cdot \frac{t+1}{W}
\]

**退火段**（\(t \ge W\)）余弦衰减：

\[
\text{lr}(t) = \eta_{\min} + \frac{1}{2}(\text{base\_lr}-\eta_{\min})\left(1+\cos\!\left(\pi \cdot \frac{t-W}{T_d}\right)\right)
\]

衔接点 \(t=W\) 处，余弦项为 \(\cos(0)=1\)，于是 \(\text{lr}(W)=\text{base\_lr}\)，与 warmup 终点重合，曲线连续。

切换机制：`_WarmupScheduler` 用 `finished` 布尔标志，在第一次跨过 `warmup_epochs` 时把内层调度器的 `base_lrs` 同步成外层的 `base_lrs`，之后每一步都委托给内层余弦调度器。

#### 4.2.3 源码精读

**两段式基类 `_WarmupScheduler.get_lr`**：

[specforge/lr_scheduler.py:72-79](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/lr_scheduler.py#L72-L79) —— 当 `last_epoch >= warmup_epochs` 且尚未切换时，先把内层调度器的 `base_lrs` 对齐成外层 `base_lrs`（保证 warmup 终点与退火起点一致）、置 `finished=True`，再委托 `after_scheduler.get_lr()`；否则返回线性 warmup 值 `(last_epoch+1)/warmup_epochs * base_lr`。

**step 的分支调度**：

[specforge/lr_scheduler.py:81-90](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/lr_scheduler.py#L81-L90) —— 一旦 `finished`，直接推进内层 `after_scheduler`；否则走父类 `_TwoStageScheduler.step` 推进 warmup 计数。这保证 warmup 与退火不会重复推进 `last_epoch`。

**顶层封装 `CosineAnnealingWarmupLR`**：

[specforge/lr_scheduler.py:105-119](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/lr_scheduler.py#L105-L119) —— 把内层 `_CosineAnnealingLR` 的总步数设为 `total_steps - warmup_steps`（退火段长度），再交给 `_WarmupScheduler`，于是外层只管前 `warmup_steps` 步、内层管余下退火段。

**状态保存与恢复的微妙之处**：两段式调度器要正确 resume，必须同时存内层调度器状态并恢复 `_last_lr`：

[specforge/lr_scheduler.py:29-53](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/lr_scheduler.py#L29-L53) —— `load_state_dict` 先恢复内层 `after_scheduler` 状态，再用保存的 `_last_lr` **手动写回** `param_group["lr"]`。注释点出关键原因：PyTorch 的 `CosineAnnealingLR.get_lr()` 用 `group["lr"]` 计算下一步 lr，但 `load_state_dict` 不会自动更新优化器的 `lr`，不手动写回会导致 resume 后第一步 lr 错位。

#### 4.2.4 代码实践

**实践目标**：手画 LR 曲线，核对其连续性。

**操作步骤**：

1. 假设 `total_steps=1000`、`warmup_ratio=0.015`（则 `warmup_steps=int(0.015*1000)=15`）。
2. 用本讲公式，计算 \(t=0, 15, 500, 1000\) 四个点的 lr（`base_lr` 用默认 `1e-4`，`eta_min=0`）。
3. 画出曲线草图，标注 warmup 终点（t=15）与退火终点（t=1000）。

**需要观察的现象**：t=15 处 warmup 给出 \(1e-4 \cdot 15/15 = 1e-4\)，余弦公式在 t=15 给出 \(\frac{1}{2}\cdot 1e-4\cdot(1+\cos 0)=1e-4\)，两者相等——曲线连续。

**预期结果**：曲线在 t=15 平滑相接，t=1000 衰减到 ~0。

**若想真正运行**（示例代码，非项目原有）：

```python
# 示例代码：仅用于观察 LR 曲线，非 SpecForge 训练入口
import torch
from specforge.lr_scheduler import CosineAnnealingWarmupLR

params = [torch.nn.Parameter(torch.zeros(1))]
opt = torch.optim.AdamW(params, lr=1e-4)
sched = CosineAnnealingWarmupLR(opt, total_steps=1000, warmup_steps=15)
lrs = []
for _ in range(1000):
    sched.step()
    lrs.append(opt.param_groups[0]["lr"])
print(lrs[14], lrs[15], lrs[500], lrs[999])
```

运行后核对 `lrs[14]`（warmup 段）、`lrs[15]`（切换点）、`lrs[500]`（退火中段）、`lrs[999]`（末端）是否符合预期。标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：若把 `warmup_ratio` 设为 `0`，曲线会退化成什么？

> **答案**：`warmup_steps=0`，跳过 warmup 段，直接从 t=0 走余弦退火。`get_lr` 中 `last_epoch >= warmup_epochs(=0)` 立即成立，切换到内层余弦调度。此时 t=0 处 lr 即为 `base_lr`。

**练习 2**：`_TwoStageScheduler.load_state_dict` 为什么要手动写回 `param_group["lr"]`？

> **答案**：PyTorch `CosineAnnealingLR.get_lr()` 依赖 `group["lr"]` 计算下一步学习率，而 `load_state_dict` 不会自动更新它；若不手动恢复，resume 后第一个 `step()` 会用错误的基线算 lr，导致学习率跳变（见 lr_scheduler.py:47-53 注释）。

---

### 4.3 步数 horizon 与 max_steps / total_steps 语义

#### 4.3.1 概念说明

余弦退火需要一个**确定性的总步数 horizon** 作为分母。SpecForge 在配置里给了两个看起来很像的字段：

- `training.total_steps`：**权威的步数 horizon**。明确告诉系统「整个训练就跑这么多 optimizer step」，余弦曲线据此从峰值滑到谷底。
- `training.max_steps`：**步数上限 / 早停帽**。语义是「最多跑这么多步就停」，更像一个安全阀。

二者都可省略。难点是省略后如何补全——这取决于数据模式（离线有总量、在线是流）。

本模块要回答实践任务的核心问题：**当 `total_steps` 与 `max_steps` 都省略时，在线 producer 如何自己推导 horizon，consumer 又如何复用它。**

#### 4.3.2 核心流程

**离线解析（`resolve_total_steps`）**——优先级链：

```text
total_steps 给了  →  直接用它
否则 max_steps 给了 → 用它
否则 num_samples 给了（离线有总量）→ 从数据量推算
否则（num_samples 为 None，流式）→ 报错：必须显式给 total_steps 或 max_steps
```

数据量推算公式（离线）：

\[
\text{micro\_batches\_per\_epoch} = \left\lfloor \frac{\text{num\_samples}}{\text{batch\_size}} \right\rfloor,\qquad
\text{total\_steps} = \left\lfloor \frac{\text{micro\_batches\_per\_epoch} \times \text{num\_epochs}}{\text{accumulation\_steps}} \right\rfloor
\]

注意分母是 `accumulation_steps`——因为「步数」始终指 optimizer step，每 `accumulation_steps` 个 micro-batch 才合成一次。

**在线解析（`resolve_online_total_steps`）**——producer 专属，从 prompt 数量推：

\[
\text{quantum} = \text{dp\_size} \times \text{batch\_size} \times \text{accumulation\_steps},\qquad
\text{total\_steps} = \left\lfloor \frac{\text{num\_prompts} \times \text{prompt\_epochs}}{\text{quantum}} \right\rfloor
\]

这里的 `quantum`（量子窗口）正是 u7-l1/u7-l4 讲过的 producer/consumer 握手单位。producer 只派发**完整**的 quantum 窗口，所以 horizon 是 prompt 总量整除 quantum 的结果，余数被丢弃（drop-last）。

**跨进程握手**：producer 算出 horizon 后写一份 schedule 记录到磁盘，consumer 等待并读取它，从而两边用**同一个** horizon 构造各自的学习率曲线。

#### 4.3.3 源码精读

**离线优先级链**：

[specforge/training/schedule.py:8-36](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/schedule.py#L8-L36) —— `resolve_total_steps` 依次尝试 `total_steps` → `max_steps` → 数据量推算；`num_samples` 为 `None`（流式源）且二者都缺时，抛 `ValueError`，要求显式给出（这正是「在线 consumer 必须从 producer 读 horizon」的根本原因——consumer 本地 `num_samples` 是 None）。

**在线 producer 推导**：

[specforge/training/schedule.py:39-76](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/schedule.py#L39-L76) —— `resolve_online_total_steps` 先校验所有输入为正整数，再用 `num_prompts * prompt_epochs // (dp_size*batch_size*accumulation_steps)` 算 horizon。注释指出尾政策与「在线 distributor 只派发完整 optimizer 窗口」一致，故用整除。结果 `< 1` 则报错。

**producer 把 horizon 写成 schedule 记录**：

[specforge/training/disaggregated.py:204-226](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/disaggregated.py#L204-L226) —— `_online_schedule_payload` 调 `resolve_online_total_steps` 算出 `total_steps`，连同 `num_prompts`、`prompt_epochs`、`prompt_seed`、`dp_size`、`batch_size`、`accumulation_steps` 一起打包成一份版本化的记录（`version: 1`）。这份记录就是 producer 与 consumer 之间的「步数契约」。

**consumer 读取并校验**：

[specforge/training/disaggregated.py:229-270](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/disaggregated.py#L229-L270) —— `_read_online_total_steps` 等待 schedule 文件就绪，读出 payload，**逐字段比对**本 consumer 配置（`prompt_epochs`/`prompt_seed`/`dp_size`/`batch_size`/`accumulation_steps` 必须完全一致），任何不一致即抛错；校验 `total_steps` 为 ≥1 的整数后返回。这保证 consumer 用的 horizon 与 producer 严格同源。

**consumer 侧的兜底逻辑**：

[specforge/training/disaggregated.py:725-727](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/disaggregated.py#L725-L727) —— 当 `total_steps` 与 `max_steps` 都为 `None` 时，consumer 才去读 producer 写的在线 schedule 记录（`_read_online_total_steps`），把读到的值当作 `total_steps` 下发给训练器。这就完整回答了实践任务：**省略时 producer 自己按 prompt 量推、consumer 从 producer 写的记录里读。**

**工厂与训练器的 horizon 接线**：

[specforge/training/trainer.py:225-245](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/trainer.py#L225-L245) —— 训练器装配时，只要 `total_steps`/`max_steps`/`dataset_size` 任一非空，就调 `resolve_total_steps` 得到 `effective_total_steps`；若工厂需要配置 horizon 但解析结果仍为 `None`（流式源却没给 horizon），立即抛错；否则 `configure_total_steps(effective_total_steps)` 把 horizon 灌进工厂。`effective_total_steps` 还会被写进检查点的 `standard_checkpoint_extra`（L252），使 resume 时 horizon 可校验。

**max_steps 作为早停帽的另一用途**：

[specforge/training/schedule.py:79-105](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/schedule.py#L79-L105) —— `validate_fixed_accumulation_plan` 在装配前检查「固定数据计划是否会以一个不完整的梯度累积窗口收尾」。若 `max_steps` 设得足够小（`≤ complete_steps`），就算数据有余数也可接受提前停在完整边界；否则余数非零即抛错。这是 `max_steps` 与 `total_steps` 语义差异的具体体现：`max_steps` 允许「提前停在完整边界」，而 `total_steps` 是一条必须走满的权威曲线。

#### 4.3.4 代码实践

**实践目标**：回答实践任务——说清 `max_steps` 与 `total_steps` 的区别，以及在线 producer/consumer 如何在二者都省略时协同确定 horizon。

**操作步骤（源码阅读型实践）**：

1. 打开 `specforge/config/schema.py:484-485`，确认两个字段都是 `Optional[int]`（默认 `None`）。
2. 打开 `specforge/training/schedule.py`，对照 `resolve_total_steps`（L8-36）与 `resolve_online_total_steps`（L39-76）。
3. 打开 `specforge/training/disaggregated.py`，追踪 `_online_schedule_payload`（producer 写，L204-226）→ `_read_online_total_steps`（consumer 读，L229-270）→ L725-727 的兜底分支。
4. 画一张时序图：producer 算 horizon → 写 schedule 记录 → consumer 等待并读取 → 二者用同一 horizon 构造 LR 曲线。

**需要回答的两个问题**：

- **当二者都省略时，online producer 如何推导 schedule horizon？**
  producer 不看 `total_steps`/`max_steps`，而是调 `resolve_online_total_steps`，用 \(\lfloor \text{num\_prompts} \times \text{prompt\_epochs} / (\text{dp\_size}\times\text{batch\_size}\times\text{accumulation\_steps}) \rfloor\) 算出 horizon，连同各拓扑参数打包成版本化 schedule 记录写盘。

- **consumer 又如何使用它？**
  consumer 本地是流式源（`num_samples=None`），自己算不出 horizon；它在 L725-727 检测到 `total_steps`/`max_steps` 都为 `None` 时，去读 producer 写的 schedule 记录（`_read_online_total_steps`），逐字段校验一致后取用，再交给工厂 `configure_total_steps` 构造与 producer 同源的 LR 曲线。

**预期结果**：能复述「producer 推导 → 写记录 → consumer 读记录 → 同源 horizon」这条链，并指出若 consumer 配置与 producer 记录里的 `dp_size`/`batch_size` 等任一不符，会在读记录时 fail-fast。

**若想真正运行**：用一个在线 disaggregated 示例 YAML 运行 `specforge train --plan`，观察计划里是否包含 producer 与 consumer 两条命令（producer 负责写 schedule 记录）。标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：离线场景下 `total_steps` 与 `max_steps` 都省略、`num_samples=1000`、`batch_size=10`、`accumulation_steps=2`、`num_epochs=1`，求 horizon。

> **答案**：`micro_batches_per_epoch = 1000//10 = 100`；`total_steps = (100*1)//2 = 50`。

**练习 2**：为什么 consumer 本地不能像离线那样从 `num_samples` 推 horizon？

> **答案**：在线 consumer 面对的是流式 queue（一次性消费，见 u7-l3），`dataset_size` 为 `None`。`resolve_total_steps` 在 `num_samples=None` 且无显式步数时会抛错（schedule.py:22-26）。所以 consumer 必须从 producer 写的 schedule 记录里读 horizon，确保两边余弦曲线同源。

**练习 3**：若 producer 算出 `total_steps` 时有余数（prompt 量不整除 quantum），会发生什么？

> **答案**：余数对应的 prompt 被丢弃（drop-last）。`resolve_online_total_steps` 用整除 `//`，只产出完整 quantum 窗口对应的步数；这呼应 u7-l4「EOF 不足一个 quantum 的零头走 drop-last 式收尾」。后果是带零头的成功 attempt 不可 resume（账本留 committed-but-unacknowledged）。

---

## 5. 综合实践

把三个模块串起来，完成一次「配置 → horizon 解析 → LR 曲线」的端到端追踪。

**任务**：为一个 EAGLE3 在线 disaggregated 训练设计配置，使 producer 与 consumer 自动协同确定 50 步的 horizon。

**步骤**：

1. 选定拓扑参数：`dp_size=2`（如 `nnodes=1`、`nproc_per_node=2`）、`batch_size=2`、`accumulation_steps=1`。则 `quantum = 2*2*1 = 4`。
2. 要得到 50 步 horizon，按 `total_steps = num_prompts * prompt_epochs // quantum`，取 `num_prompts=200`、`num_epochs=1`，则 `200//4 = 50`。
3. 在 YAML 的 `training` 段**不填** `total_steps` 与 `max_steps`（让在线推导生效），设 `learning_rate: 1e-4`、`warmup_ratio: 0.015`（则 `warmup_steps=int(0.015*50)=0`，跳过 warmup 直接退火）。
4. 对照本讲，回答：
   - producer 会写出的 schedule 记录里 `total_steps` 字段值是多少？（答：50）
   - consumer 读到后，`_ConfiguredOptimizerFactory.configure_total_steps` 收到的值是多少？（答：50）
   - `CosineAnnealingWarmupLR` 的退火段长度 `total_steps - warmup_steps` 是多少？（答：50）
5. 追踪一条 LR：t=0 时 lr 应为 `base_lr=1e-4`（余弦起点），t=25 时约为 \(\frac{1}{2}\cdot1e-4\cdot(1+\cos(\pi\cdot 25/50))=\frac{1}{2}\cdot1e-4\cdot(1+0)=5e-5\)，t=50 时衰减到 ~0。

**验收标准**：能完整复述「prompt 量 → quantum → horizon → schedule 记录 → consumer 同源 LR 曲线」全链，并算对至少 3 个点的 lr 值。若运行环境就绪，可用 `specforge train --plan` 预览计划确认 producer/consumer 命令；否则标注「待本地验证」。

## 6. 本讲小结

- SpecForge 用一个**带状态的延迟工厂** `_ConfiguredOptimizerFactory` 产出优化器：先收 horizon、后在 FSDP 包裹后产 `BF16Optimizer`，避免在 horizon 与可训练参数都未定时过早实例化。
- `BF16Optimizer` 在 bf16 权重之上维护 **fp32 主副本**，每步走「算范数 → 跨卡裁剪 → fp32 更新 → 清零 → 推进 LR → 拷回 bf16」三连，LR 推进紧随 optimizer step，对应 u6-l3 的单一权威边界信号。
- 学习率曲线是**线性 warmup + 余弦退火**两段式，由 `CosineAnnealingWarmupLR` 组合两个 PyTorch 基类实现，resume 时需手动写回 `param_group["lr"]` 以防 lr 跳变。
- `total_steps` 是**权威 horizon**（余弦分母），`max_steps` 是**早停帽**（可停在完整边界）；二者优先级为 `total_steps > max_steps > 数据量推算`。
- 离线有总量时从数据推 horizon；**在线**因流式无总量，producer 用 `resolve_online_total_steps` 按 prompt 量推、写 schedule 记录，consumer 读取并逐字段校验后复用，保证两侧 LR 曲线同源。
- horizon 一旦解析即写入检查点 `effective_total_steps`，使 resume 能校验训练计划一致性。

## 7. 下一步学习建议

- **下一讲 u9-l3（实验跟踪与性能分析）**：本讲的 `last_grad_norm`、`get_learning_rate()` 正是 tracker 要记录的 `train/*` 指标来源，可顺势学习 metrics 命名约定与 profiling 窗口如何对齐到 optimizer step。
- **回顾 u9-l1（检查点与恢复）**：本讲的 `_last_lr` 手动写回与 `effective_total_steps` 校验，正是 resume 能数值忠实的关键，建议对照 checkpoint 代码再读一遍。
- **深入 u7-l4（在线引用分发）**：本讲的 `quantum` 与 drop-last 收尾直接影响 horizon 的余数处理，理解 RefDistributor 能把「步数 horizon」与「数据分发」两侧贯通。
- **继续阅读源码**：`specforge/optimizer.py` 的 `load_state_dict`/`state_dict`（L159-210）展示了 fp32 主副本与调度器状态的完整持久化，是理解 resume 数值忠实的另一关键。

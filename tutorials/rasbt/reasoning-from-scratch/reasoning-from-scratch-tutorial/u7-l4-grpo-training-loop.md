# u7-l4　GRPO 训练循环与批量化

## 1. 本讲目标

上一讲（u7-l3）我们把 `reward_rlvr` 与 `sample_response` 装配成了可反传的 `compute_grpo_loss`，并拿到一个带 `grad_fn` 的 `loss_tensor`。本讲回答下一个问题：**这个损失张量如何变成一次又一次的参数更新？** 学完本讲，你应该能够：

1. 逐阶段描述 `train_rlvr_grpo` 的完整训练骨架：初始化优化器 → `zero_grad` → 算损失 → `backward` → 梯度裁剪 → `optimizer.step` → 写日志 → 存 checkpoint。
2. 独立使用两个可复用的小工具 `save_checkpoint` 与 `append_csv_metrics`，并理解 `KeyboardInterrupt` 时保存中断 checkpoint 的工程处理。
3. 说出独立训练脚本 `rlvr_grpo_original_no_kl.py` 相对库函数版本的三处工程增强（去掉 `.cpu()`、跳过零优势更新、checkpoint 处评测）。
4. 解释 `sample_responses_batched` 如何把同一条 prompt 的多个 rollout 并成一个 batch 生成，以及它的吞吐提升点与显存代价。
5. 概览 FSDP 多 GPU 变体在数据切分、日志去重、checkpoint 汇聚上的增量改动。

## 2. 前置知识

本讲是「工程课」，模型与损失函数都已在前面讲义就绪，这里补齐训练侧的基础概念：

- **优化器（Optimizer）**：拿到损失对每个参数的梯度后，按某种规则更新参数。`torch.optim.AdamW` 是 Adam 的改进版（把权重衰减从梯度里解耦出来），为每个参数维护一阶、二阶动量，是训练 Transformer 的默认选择。`lr`（学习率）控制每步更新的步长，本项目中是 `1e-5` 这样的小值——RL 微调要小步慢走，避免破坏预训练能力。
- **autograd 与 `zero_grad`**：调用 `loss.backward()` 时，PyTorch 沿计算图自动算出所有参数的梯度，并且**默认累加**到 `param.grad` 上（`grad += 新梯度`）。因此每步开始前必须 `optimizer.zero_grad()` 清零，否则这一步的梯度会混入上一步的残留。
- **梯度裁剪（gradient clipping）**：RL 训练的奖励信号稀疏且方差大，偶尔会出现异常大的梯度导致训练崩溃。`torch.nn.utils.clip_grad_norm_` 把所有参数梯度拼成一个向量，若其 L2 范数 \(\|\mathbf{g}\|_2\) 超过阈值 \(c\)，则整体缩放为 \(\mathbf{g} \leftarrow \mathbf{g} \cdot c / \|\mathbf{g}\|_2\)。只改长度、不改方向。
- **`state_dict` 与 checkpoint**：`model.state_dict()` 返回「参数名 → 张量」的有序字典；`torch.save` 把它序列化成 `.pth` 文件。加载时用 `model.load_state_dict(torch.load(path))` 即可恢复权重。
- **追加式 CSV 日志**：强化学习训练动辄数小时，必须「边跑边写」指标（而不是跑完再写），进程崩溃时至少保住已完成的记录。做法是首次写表头，之后每步以 `open(..., "a")` 追加一行。
- **FSDP（Fully Sharded Data Parallel）**：多 GPU 训练策略。与「每张卡复制完整模型」的数据并行不同，FSDP 把模型参数、梯度、优化器状态都**切片（shard）**分布在各卡上，用时临时聚合，从而让 0.6B～7B 级别模型在消费级多卡上可训。
- **与上一讲的衔接**：`compute_grpo_loss` 返回的字典里，`loss_tensor` 是带梯度的张量、`rewards` 是本步各 rollout 的奖励列表、`samples` 里有每条 rollout 的文本与生成长度——本讲的训练循环正是这三个字段的消费者。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| `reasoning_from_scratch/ch06.py` | 第 6 章库代码（教学版） | `train_rlvr_grpo`、`save_checkpoint`、`append_csv_metrics` |
| `ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl.py` | 独立训练脚本（单条 rollout） | argparse 入口、性能 tweaks、checkpoint 处评测、断点续训 |
| `ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl_batched.py` | 批量 rollout 训练脚本 | `sample_responses_batched`、分批策略 |
| `ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl_batched_fsdp.py` | 多 GPU FSDP 变体 | 数据切分、rank0 写日志、全量 state_dict 汇聚 |
| `ch06/02_rlvr_grpo_scripts_intro/plot_metrics.py` | 训练曲线可视化 | `load_metrics`、`moving_average`，消费上述脚本的 CSV |
| `ch06/02_rlvr_grpo_scripts_intro/logs/` | 作者放出的真实训练日志 | 258 步 no-KL 训练的 CSV，可离线复用 |

一个先记住的全景：**库版本（`ch06.py`）追求与书正文一致、便于教学；三个独立脚本在同样的算法骨架上叠加工程优化（吞吐、显存、可恢复性），且刻意把核心函数复制进脚本以便读者直接改**——README 对此的说明见 [ch06/02_rlvr_grpo_scripts_intro/README.md:8-19](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch06/02_rlvr_grpo_scripts_intro/README.md#L8-L19)。

## 4. 核心概念与源码讲解

### 4.1 `train_rlvr_grpo`：八阶段训练循环

#### 4.1.1 概念说明

`train_rlvr_grpo` 是第 6 章的「主控函数」：它不关心损失内部怎么算（那是 `compute_grpo_loss` 的事），只负责**编排**——取一条训练题、算损失、反传、更新参数、记录、定期存档。一个训练 step 的定义是：**对一道数学题采样 `num_rollouts` 个回答，用组内相对优势做一次参数更新**。这与监督学习「一个 batch 一次更新」形式相同，只是「batch」换成了「同一 prompt 的一组 rollout」。

它是所有后续变体（第 7 章的 KL、clip、格式奖励版本，以及本讲的 batched / FSDP 版本）共同遵循的骨架，把这个骨架吃透，后面每个变体只是往其中一两个阶段里塞东西。

#### 4.1.2 核心流程

```
train_rlvr_grpo(model, tokenizer, math_data, device, ...):
    阶段 1  初始化：AdamW(model.parameters(), lr)；model.train()
    阶段 2  for step in range(steps):            # steps 默认 = 数据集大小
    阶段 3      optimizer.zero_grad()            # 清掉上一步的梯度残留
                example = math_data[step % len(math_data)]
    阶段 4      stats = compute_grpo_loss(...)   # rollout + 奖励 + 优势 + logp + pg_loss
    阶段 5      stats["loss_tensor"].backward()  # 反传求梯度
                clip_grad_norm_(parameters, 1.0) # 梯度范数裁剪
    阶段 6      optimizer.step()                 # 用梯度更新权重
    阶段 7      统计 reward_avg / avg_response_len → 追加写 CSV + 打印
                （每 10 步打印最多 3 条样本原文）
    阶段 8      若 step % checkpoint_every == 0：save_checkpoint
    异常    KeyboardInterrupt → 存 suffix="interrupt" 的 checkpoint 后返回
```

#### 4.1.3 源码精读

先看函数签名与默认参数：[reasoning_from_scratch/ch06.py:222-236](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L222-L236)。注意 `steps=None`、`checkpoint_dir="."`、`csv_log_path=None` 这三个「可省略」参数——它们分别决定了训练步数、模型存档位置与指标日志位置的默认行为。

**阶段 1：初始化优化器。** [reasoning_from_scratch/ch06.py:238-244](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L238-L244) 做三件事：`steps=None` 时把训练步数设为数据集长度（12000 题就跑 12000 步，一题一步）；用 `AdamW` 包装模型全部参数，学习率 `lr`（默认 `1e-5`）；`model.train()` 把模型切回训练模式。注释特意说明「模型在函数外已初始化」——训练函数只接管优化器，不管模型构建。

**CSV 日志路径的惰性默认值。** [reasoning_from_scratch/ch06.py:247-250](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L247-L250)：调用者没给 `csv_log_path` 时，用当前时间戳生成 `train_rlvr_grpo_metrics_20250101_120000.csv` 这样的文件名，避免多次实验互相覆盖。整个 `try` 块从下一行开始——训练主体中的任何一步都不会因日志失败而丢掉中断存档的机会。

**阶段 3：清梯度与取数据。** [reasoning_from_scratch/ch06.py:253-260](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L253-L260)。`optimizer.zero_grad()` 按最佳实践放在每步开头（PyTorch 梯度默认累加，不清零会把上一步的梯度也加进来）；`math_data[step % len(math_data)]` 用取模遍历数据集——即使 `steps` 超过数据量也会循环复用，不会越界。`current_step = step + 1` 把 0 基下标转成「第几步」的人话编号，供日志与 checkpoint 文件名使用。

**阶段 4～6：损失、反传、裁剪、更新。** 这是训练的心脏，见 [reasoning_from_scratch/ch06.py:262-281](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L262-L281)：

```python
stats = compute_grpo_loss(...)          # 阶段 4：返回含 loss_tensor 的字典
stats["loss_tensor"].backward()          # 阶段 5：反传，梯度写入 param.grad
torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)  # 裁剪全局范数到 ≤1.0
optimizer.step()                         # 阶段 6：AdamW 更新权重
```

四行代码对应四个标准动作。值得注意的是 `compute_grpo_loss` 内部已把优势 `detach` 成固定学习信号（见 u7-l3），所以这里的 `backward()` 求的是 \(\nabla_\theta \, -\frac{1}{N}\sum_i A_i \log p_\theta(y_i \mid x)\) 中只对 \(\log p_\theta\) 部分的梯度。

**阶段 7：指标统计与落盘。** [reasoning_from_scratch/ch06.py:283-304](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L283-L304) 从 `stats` 里取三样东西：本步平均奖励 `reward_avg`（全 0 或全 1 意味着这步的优势全零、参数实际没动）、平均生成长度 `avg_response_len`（RL 训练里回答越来越长往往是坏信号），连同 loss 一起交给 `append_csv_metrics` 追加写入 CSV，并打印单行进度。

**每 10 步的「体感检查」。** [reasoning_from_scratch/ch06.py:306-316](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L306-L316) 每 10 步打印最多 3 条 rollout 原文（换行替换成 `\n` 以免刷屏）。注释写得很直白：检查模型是否还在生成连贯文本——数值指标正常但文本退化为乱码/复读，是 RL 训练崩溃的常见形态，纯看曲线发现不了。

**阶段 8：定期存档。** [reasoning_from_scratch/ch06.py:318-325](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L318-L325) 在 `current_step % checkpoint_every == 0` 时调用 `save_checkpoint`（4.2 节精读）。

#### 4.1.4 代码实践

**实践目标**：在 CPU 上对 `train_rlvr_grpo` 做最小规模冒烟测试，亲眼确认「CSV 日志 + checkpoint 文件」两件产物被正确生成。

**操作步骤**（示例代码，保存为仓库外任意目录的 `smoke_grpo.py`，例如 `/tmp/grpo-smoke/smoke_grpo.py`）：

```python
# 示例代码：GRPO 训练循环 CPU 冒烟测试
import torch
from reasoning_from_scratch.ch02 import get_device
from reasoning_from_scratch.ch03 import load_model_and_tokenizer
from reasoning_from_scratch.ch06 import load_math_train, train_rlvr_grpo

device = get_device()   # 无 GPU 时自动落到 CPU
print("device:", device)

model, tokenizer = load_model_and_tokenizer(
    which_model="base", device=device, use_compile=False
)

math_data = load_math_train()      # 首次运行会下载并缓存 math_train.json
math_data = math_data[:3]          # 只取 3 条训练数据

trained = train_rlvr_grpo(
    model=model,
    tokenizer=tokenizer,
    math_data=math_data,
    device=device,
    steps=3,               # 3 步，一题一步
    num_rollouts=2,        # 每步 2 个 rollout（函数要求 >=2）
    max_new_tokens=64,     # 压缩生成长度以省时间
    checkpoint_every=2,    # 第 2 步存一次档
    checkpoint_dir="/tmp/grpo-smoke/ckpts",
    csv_log_path="/tmp/grpo-smoke/smoke_metrics.csv",
)
```

跑完后检查产物（示例代码）：

```python
from pathlib import Path
import torch

csv = Path("/tmp/grpo-smoke/smoke_metrics.csv").read_text()
print(csv)   # 应有表头 + 3 行数据

ckpts = sorted(Path("/tmp/grpo-smoke/ckpts").glob("*.pth"))
print(ckpts)
sd = torch.load(ckpts[0], map_location="cpu")
print(len(sd), "tensors; first keys:", list(sd)[:2])
```

**需要观察的现象**：

1. 控制台逐行打印 `[Step k/3] loss=... reward_avg=... avg_resp_len=...`；
2. `smoke_metrics.csv` 含表头 `step,total_steps,loss,reward_avg,avg_response_len` 与 3 行数据（库版本 CSV 没有 `tokens_per_sec` 列，这是与独立脚本的差异之一）；
3. `ckpts/` 下恰好出现一个 `qwen3-0.6B-rlvr-grpo-step00002.pth`——3 步（第 1、2、3 步）中只有 `current_step=2` 满足 `2 % checkpoint_every == 0`；
4. `torch.load` 出的 state_dict 有约数百个张量键（28 层 × 各组件 + embedding/norm/out_head）。

**预期结果**：若 `reward_avg=0.0`（0.6B 模型 64 个 token 内很难答对并写出 `\boxed{}`），CSV 的 loss 可能整段接近 0——因为两组奖励全 0 时优势全零，参数没有实质更新，这正是 u7-l3 讲过的「全对或全错则不更新」。首次运行需下载约 1.2 GB 权重与一份 `math_train.json`（之后走本地缓存）；CPU 上 3 步预计在分钟到几十分钟量级（视机器而定），内存占用粗估数 GB（参考 README 资源表：`num_rollouts=4、max_new_tokens=256` 约需 10.6 GB，本实践参数更小）。**具体耗时与准确产物名：待本地验证**。

> 离线替代方案：`train_rlvr_grpo` 只消费每条数据的 `problem` 与 `answer` 两个字段，因此网络不便时可以用 `math_data = [{"problem": "What is 2+2?", "answer": "4"}, ...]` 这样手写的 3 条数据先行跑通流程（模型权重仍需下载一次）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `optimizer.zero_grad()` 必须放在每步的开头？如果漏掉会发生什么？

**答案**：PyTorch 的 `backward()` 把梯度**累加**到 `param.grad` 而不是覆盖。漏掉 `zero_grad()` 时，第 k 步实际用的梯度是第 1～k 步所有梯度的总和，等效于一个不断膨胀的学习率，训练会迅速发散。

**练习 2**：`clip_grad_norm_(model.parameters(), 1.0)` 中的 1.0 是什么含义？它改变梯度方向吗？

**答案**：1.0 是全局梯度 L2 范数的上限。当所有参数梯度拼成的向量范数超过 1.0 时，整体缩放到 1.0；否则不动。缩放是乘一个正标量 \(\mathbf{g} \cdot c/\|\mathbf{g}\|_2\)，只改长度不改方向。

**练习 3**：库版本 `train_rlvr_grpo` 中 `example = math_data[step % len(math_data)]` 用取模而不是直接 `math_data[step]`，原因是什么？

**答案**：`steps` 默认等于 `len(math_data)`，但调用者可以显式传入更大的 `steps`（或数据集很小而想多训几轮）。取模保证了任何 `steps` 取值下索引都不越界，同时实现「数据用完自动从头循环」。

### 4.2 `save_checkpoint` 与 `append_csv_metrics`：可观测与可恢复

#### 4.2.1 概念说明

强化学习训练有三个工程现实：(1) 跑得久（本仓库设置下一轮就是数小时）；(2) 会中断（手动停止、机器断电、OOM）；(3) 跑完必须能复盘（为什么第 100 步之后 reward 崩了？）。`save_checkpoint` 解决「可恢复」，`append_csv_metrics` 解决「可复盘」，两者共同构成训练的**黑匣子**。特别地，GRPO 训练不能只存最终模型——README 的结果显示 no-KL 版本在第 50 步 MATH-500 准确率 47.4%、第 100 步反而降到 44.0%（[ch06/02_rlvr_grpo_scripts_intro/README.md:34-45](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch06/02_rlvr_grpo_scripts_intro/README.md#L34-L45)），**中间 checkpoint 本身就是实验产出**。

#### 4.2.2 核心流程

```
save_checkpoint(model, dir, step, suffix=""):
    mkdir -p dir
    文件名 = dir / f"qwen3-0.6B-rlvr-grpo-step{step:05d}{-suffix}.pth"
    torch.save(model.state_dict(), 文件名)
    return 文件名

append_csv_metrics(csv_path, step, total, loss, reward_avg, avg_len):
    文件不存在 → 先写表头
    以追加模式 ("a") 写一行逗号分隔的数值
```

#### 4.2.3 源码精读

**`save_checkpoint`**：[reasoning_from_scratch/ch06.py:190-199](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L190-L199)。三个细节值得注意：`mkdir(parents=True, exist_ok=True)` 让调用者可以放心传一个不存在的深层目录；`step:05d` 把步数格式化成五位零填充（`00002`），保证文件名字典序与时间序一致、`ls` 排序即训练顺序；`suffix` 参数用于区分「定期存档」和「中断存档」。它**只存 `state_dict`、不存优化器状态**——这意味着中断后恢复的是权重而非完整训练状态（AdamW 动量会丢），对本书几十到几百步的短训练可以接受，更长的训练需要扩展为同时保存 `optimizer.state_dict()`。

**`append_csv_metrics`**：[reasoning_from_scratch/ch06.py:202-219](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L202-L219)。首次调用时写表头 `step,total_steps,loss,reward_avg,avg_response_len`，之后每次以追加模式写一行，数值用 `:.6f` 固定小数位。每次写完立即关闭文件（`with` 块），进程随时被杀都不丢已写行——这正是 4.1 节说「边跑边写」的实现。

**KeyboardInterrupt 的处理**：[reasoning_from_scratch/ch06.py:327-336](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L327-L336)。整个训练循环包在 `try/except KeyboardInterrupt` 里：用户按 Ctrl+C 时，训练不在「白跑」状态下退出，而是立即以 `suffix="interrupt"` 存一份带当前步数的 checkpoint 再 `return model`。两个巧思：`max(1, current_step)` 保证文件名步数至少为 1（若第一步还没完成就中断，避免出现 `step00000` 这种与「第 0 步」语义混淆的名字）；捕获的是 `KeyboardInterrupt` 而非 `Exception`——真正的代码 bug（如 `ValueError`）仍会正常抛出栈回溯，不会被静默吞掉。

**CSV 的下游消费者 `plot_metrics.py`**：[ch06/02_rlvr_grpo_scripts_intro/plot_metrics.py:14-36](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch06/02_rlvr_grpo_scripts_intro/plot_metrics.py#L14-L36) 的 `load_metrics` 逐列读取 CSV（第 2 列 loss、第 3 列 reward、第 5 列平均长度、第 6 列若有则读作评测准确率），[ch06/02_rlvr_grpo_scripts_intro/plot_metrics.py:39-51](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch06/02_rlvr_grpo_scripts_intro/plot_metrics.py#L39-L51) 的 `moving_average` 用累积和实现 O(n) 滑动窗口（窗口未满时按实际数量平均）。绘图主体把它排成 2×2 的 loss / reward / 响应长度 / 评测准确率四联图（[ch06/02_rlvr_grpo_scripts_intro/plot_metrics.py:79-119](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch06/02_rlvr_grpo_scripts_intro/plot_metrics.py#L79-L119)）。作者还随仓库放了一份 258 步真实训练日志 `logs/rlvr_grpo_original_no_kl_metrics.csv` 供无 GPU 读者离线绘图。

#### 4.2.4 代码实践

**实践目标**：验证中断存档路径确实生效，并用自己的 CSV 画出训练曲线。

**操作步骤**：把 4.1.4 的冒烟脚本加以改造——用 monkeypatch 让 `append_csv_metrics` 在第 3 次调用（即第 3 步写日志时）抛出 `KeyboardInterrupt`，模拟「训练中途按下 Ctrl+C」（示例代码）：

```python
# 示例代码：确定性触发 KeyboardInterrupt 分支
import reasoning_from_scratch.ch06 as ch06

original_append = ch06.append_csv_metrics
calls = {"n": 0}

def append_then_interrupt(*args, **kwargs):
    calls["n"] += 1
    if calls["n"] >= 3:          # 第 3 步写日志时模拟 Ctrl+C
        raise KeyboardInterrupt
    return original_append(*args, **kwargs)

ch06.append_csv_metrics = append_then_interrupt   # 训练循环按模块全局名查找，替换生效
```

把这段插到 `train_rlvr_grpo` 调用之前（其余参数同 4.1.4）再运行。

**需要观察的现象**：控制台在打印完第 2 步指标后输出 `KeyboardInterrupt. Saved checkpoint to .../qwen3-0.6B-rlvr-grpo-step00003-interrupt.pth`；CSV 里只有 2 行数据；checkpoint 目录多出一个带 `-interrupt` 后缀的文件。

**预期结果**：中断发生在第 3 步的日志阶段（`current_step` 已更新为 3，但尚未写 CSV），所以中断存档的步号是 3、而 CSV 只记录到第 2 步——步号 3 的参数更新已完成但指标未落盘。**待本地验证**。

随后用自己的 CSV 画图（需要 matplotlib）：

```bash
python ch06/02_rlvr_grpo_scripts_intro/plot_metrics.py \
  --csv /tmp/grpo-smoke/smoke_metrics.csv \
  --moving_average 2 --save_plot /tmp/grpo-smoke/curve.png
```

只有 2～3 行数据时曲线意义不大，但可以确认管线通；更有价值的练习是直接对作者的日志跑 `--csv ch06/02_rlvr_grpo_scripts_intro/logs/rlvr_grpo_original_no_kl_metrics.csv --moving_average 20`，观察 loss 在后期快速膨胀、reward_avg 长期低位震荡的真实训练形态。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `append_csv_metrics` 每次都要重新 `open`/`close` 文件，而不是训练开始时打开、结束时关闭？

**答案**：追加式小写入 + 立即关闭，保证任意时刻进程被杀（断电、OOM、手动停止），磁盘上的 CSV 都是完整的「已写行」集合。若长时间持有文件句柄，缓冲区里未刷写的数据会随进程一起丢失。

**练习 2**：`save_checkpoint` 只保存 `model.state_dict()`。要让训练能「无缝续训」（而不是「带权重重启」），还需要保存什么？

**答案**：优化器状态 `optimizer.state_dict()`（AdamW 的一阶/二阶动量）、当前步数 `current_step`、以及随机数发生器状态（`torch.get_rng_state()` 等）。缺少这些时恢复的训练等效于用新优化器、新调度从头动量预热，短时间内更新方向可能突变。

**练习 3**：`except KeyboardInterrupt` 捕获后为什么 `return model` 而不是 `raise`？

**答案**：这是把「用户主动中断」定义为一种**正常的结束方式**——调用方拿到已保存部分训练进度的模型继续使用（例如拿去评测）。同时它只拦截 `KeyboardInterrupt`，其他异常照常抛出，不会掩盖真正的 bug。

### 4.3 从库函数到独立脚本：`rlvr_grpo_original_no_kl.py`

#### 4.3.1 概念说明

`ch06.py` 里的 `train_rlvr_grpo` 是教学版；`rlvr_grpo_original_no_kl.py` 是能长时间实跑的工程版。算法完全相同（GRPO、无 KL 项，即 DeepSeek 原始 GRPO 去掉 KL），差别集中在三处工程增强（README 明确列出，见 [ch06/02_rlvr_grpo_scripts_intro/README.md:10-13](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch06/02_rlvr_grpo_scripts_intro/README.md#L10-L13)）：

1. **去掉采样时的 `.cpu()` 拷贝**（PR #178）：GPU 训练吞吐 +20%；
2. **跳过零优势更新**（PR #186）：一组 rollout 奖励全相同时，优势全零、更新无效，直接跳过昂贵的 logprob 重算与反传；
3. **checkpoint 处自动跑 MATH-500 评测**：每个存档点附带准确率，训练曲线直接对照泛化能力。

#### 4.3.2 核心流程

脚本的 `__main__` 入口流程：

```
argparse 解析命令行 → 设随机种子 → get_device()
→ load_math_train() 取训练数据
→ 有 --checkpoint_path ? 从 .pth 恢复模型（断点续训） : load_model_and_tokenizer("base")
→ train_rlvr_grpo(...)            # 脚本内增强版
→ 打印 CUDA 峰值显存 → 存最终模型 qwen3-0.6B-rlvr-grpo.pth
```

#### 4.3.3 源码精读

**增强 1：GPU 上不做 `.cpu()` 往返。** 对比两版 `sample_response` 的采样行：库版本 [reasoning_from_scratch/ch06.py:79-81](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L79-L81) 是 `torch.multinomial(probas.cpu(), ...)` 后再 `.to(device)`；脚本版本 [ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl.py:62-65](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl.py#L62-L65) 保留注释解释原因（跨系统一致性 vs 20% 性能）后直接 `torch.multinomial(probas, num_samples=1)`。每次 decode 步省掉两次 GPU↔CPU 拷贝与同步，几百步 × 每 rollout 数百 token 累积下来就是可观的提速。

**增强 2：先收 rollout、判断零优势、再重算 logprob。** 脚本版 `compute_grpo_loss` 把流程重排了：rollout 循环里只收集 `(token_ids, prompt_len)`（[ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl.py:121-141](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl.py#L121-L141)），恢复训练模式后先算优势并用 `torch.allclose` 判断是否全零（[ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl.py:146-165](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl.py#L146-L165)），零优势且开关打开时直接返回 `loss_tensor=None`；否则才逐条重算 `sequence_logprob`（[ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl.py:167-171](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl.py#L167-L171)）。而库版本 [reasoning_from_scratch/ch06.py:137-162](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L137-L162) 是在 rollout 循环内边采边算 logp，没有跳过机会。这个重排的收益特别大：README 指出**撞上 `max_new_tokens` 上限的 rollout 往往最长、前向最贵，且大概率没写出 `\boxed{}` 而拿 0 分**——恰是最容易造成「全 0 奖励 → 零优势」的一类，跳过它们等于砍掉最贵的无效计算。

**配套的训练循环改动**：[ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl.py:291-296](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl.py#L291-L296) 只有 `stats["loss_tensor"] is not None` 时才执行 `zero_grad → backward → clip → step`；指标照常记录（零优势步也是有效观测）。此外每步计时并计算 `tokens_per_sec`（[ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl.py:298-304](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl.py#L298-L304)），比库版本多一列吞吐指标。

**增强 3：存档即评测。** [ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl.py:309-344](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl.py#L309-L344)：每次存 checkpoint 后，若 `eval_max_items > 0`，则临时切 `eval()` 模式，取 MATH-500 前 `eval_max_items` 题跑 `evaluate_math500_stream`（u4-l4 讲过的评测链路），把准确率写进指标日志再切回 `train()`。这把「训练指标」与「泛化指标」在同一时间轴上对齐，正是 README 那张「50 步 47.4% → 100 步 44.0%」表格的数据来源。

**argparse 入口与断点续训**：[ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl.py:387-456](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl.py#L387-L456) 定义了 `--steps / --num_rollouts / --max_new_tokens / --temperature / --top_p / --seed / --checkpoint_path / --eval_on_checkpoint / --skip-zero-advantage-updates / --show_eta` 十个参数；[ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl.py:462-472](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl.py#L462-L472) 展示了两种模型准备路径：默认从 base 权重起步，传 `--checkpoint_path` 时则 `Qwen3Model(QWEN_CONFIG_06_B)` + `load_state_dict` 从任意存档恢复——与中断存档配合即可实现「断点续训」（如前所述，恢复的是权重而非优化器动量）。日志与 checkpoint 路径由 `SCRIPT_NAME`（取自文件名）派生到脚本同级的 `logs/` 与 `checkpoints/` 目录（[ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl.py:28-32](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl.py#L28-L32)），不同脚本互不干扰。

> 小注：`--num_rollouts` 的 argparse 默认值是 8，而脚本内 `train_rlvr_grpo` 签名的默认值是 9；命令行入口总是显式传参，所以实际生效默认是 8。README 表格中的实验设置也是 8。

#### 4.3.4 代码实践

**实践目标**：以最小代价跑通独立脚本入口，并核对脚本输出物与库版本的差异。

**操作步骤**（在 `ch06/02_rlvr_grpo_scripts_intro/` 目录下）：

```bash
# 1. 先看参数与默认值（零成本，不加载模型）
python rlvr_grpo_original_no_kl.py --help

# 2. 最小实跑：1 步、2 个 rollout、64 token（CPU 可承受，待本地验证）
python rlvr_grpo_original_no_kl.py --steps 1 --num_rollouts 2 --max_new_tokens 64
```

**需要观察的现象**：(1) `--help` 输出中每个参数的默认值；(2) 实跑后 `logs/rlvr_grpo_original_no_kl_metrics.csv` 的表头比库版本多出 `tokens_per_sec` 与 `eval_acc` 两列；(3) `checkpoints/rlvr_grpo_original_no_kl/` 下出现最终模型 `qwen3-0.6B-rlvr-grpo.pth`（脚本结尾必存，见 [ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl.py:494-495](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl.py#L494-L495)）。

**预期结果**：单步耗时与机器强相关（**待本地验证**）；CSV 第一行的 `tokens_per_sec` 给出你机器的吞吐基线，可与 README 表格中的 GPU 数量级对照。

#### 4.3.5 小练习与答案

**练习 1**：为什么「rollout 全部零奖励」在 GRPO 训练中特别常见，而这个事实正好让 `--skip-zero-advantage-updates` 划算？

**答案**：优势是组内标准化值 \(A_i = (r_i - \mu)/(\sigma + 10^{-4})\)，全对或全错时 \(\mu = r_i\)、\(\sigma = 0\)，所有优势为零，\(\nabla(-\bar{A}\,\log p)\) 恒为 0，更新无效。0.6B 模型答错率高，撞 token 上限的长 rollout 又贵又是 0 分，所以「无效但昂贵」的步占比可观，跳过它们净赚。

**练习 2**：脚本把 `sample_response`、`sequence_logprob` 等核心函数从库里复制了一份而不是 `import`，这种「代码重复」的合理性是什么？

**答案**：这些脚本的定位是**可改造的实验起点**（README 明言"to allow for easier inspection and modification"）。复制后，读者想改采样策略、损失定义或加正则项，直接在单文件上改，不必碰被测试覆盖的库代码；代价是与库版本的手动同步——作者通过 README 里链接的历史版本（无两个 tweak 的原版）来管理这个取舍。

**练习 3**：`--eval_on_checkpoint 50` 与 `--steps 100 --checkpoint_every 50`（默认）组合时，整个训练会额外做几次 MATH-500 评测？

**答案**：2 次（第 50 步与第 100 步各一次）。评测只在存档点触发，且只跑 MATH-500 的前 50 题（`math500_eval_data[:eval_max_items]`，见 [ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl.py:316-323](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl.py#L316-L323)）。注意评测用 `max_new_tokens` 与训练相同，比正式评测（2048）短。

### 4.4 `sample_responses_batched`：rollout 批量化

#### 4.4.1 概念说明

单条版 `sample_response` 一次生成一个 rollout，`num_rollouts=8` 就要串行做 8 次「prefill + 数百步 decode」。而 GRPO 需要的多个 rollout **共享同一条 prompt**——这给批量化留了一个绝佳入口：把 prompt 复制 `batch_size` 份当作一个 batch 前向，每行独立采样，就得到 `batch_size` 个 rollout。GPU 的并行算力在 batch 维度上几乎是白送的，这正是 `rlvr_grpo_original_no_kl_batched.py` 的核心思路。

对比第 3 章的 `evaluate_math500_batched.py`（批量化不同 prompt 的评测，需要 left-padding 对齐长度，因此要引入 `qwen3_batched.py` 的专用模型）：这里的 batch 是**同一 prompt 的复制**，天然等长，无需 padding，也就无需改动 `qwen3.py` 的 `Qwen3Model`（README 对此的说明见 [ch06/02_rlvr_grpo_scripts_intro/README.md:14-15](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch06/02_rlvr_grpo_scripts_intro/README.md#L14-L15)，注意 README 写的参数名 `--num_batches` 与脚本实际的 `--batch_size` 不一致，以脚本为准）。

#### 4.4.2 核心流程

```
sample_responses_batched(model, tokenizer, prompt, device, batch_size, ...):
    prompt_ids = encode(prompt)                       # (T,)
    input_ids  = prompt_ids.expand(batch_size, -1)    # (B, T) 同一 prompt 复制 B 份
    整批 prefill：logits = model(input_ids, cache)[:, -1]     # (B, V)
    finished = 全 False 的 (B,) 向量
    for step in range(max_new_tokens):
        温度缩放 → softmax → top_p_filter → multinomial 采样 (B, 1)
        已完成的行：用 torch.where 把采样结果强制替换成 EOS
        追加到 generated_steps；更新 finished
        若全部行完成 → break
        否则把 (B, 1) 的新 token 整批送入模型 decode 一步
    cat 所有步骤 → (B, L)；逐行截到第一个 EOS 为止
    返回 [(full_token_ids, prompt_len, text), ...] 共 B 条
```

#### 4.4.3 源码精读

**prompt 复制与整批 prefill**：[ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl_batched.py:44-50](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl_batched.py#L44-L50)。`expand(batch_size, -1)` 创建的是共享底层存储的**视图**而非拷贝；模型前向把它当作 batch 维为 B 的输入，KVCache 的每个槽位也随之变成 `(B, H_kv, t, d_h)`——KV 缓存在 batch 维上是天然支持的（u2-l5 讲过缓存形状带 batch 维），这里直接受益。

**EOS 的批量处理**：单条版一行代码的 `break`，在批量版里变成三件套，见 [ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl_batched.py:52-77](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl_batched.py#L52-L77)：

```python
eos_tok = next_token.new_full((batch_size, 1), eos_id)
next_token = torch.where(finished.view(batch_size, 1), eos_tok, next_token)
finished = finished | (next_token.squeeze(1) == eos_id)
if torch.all(finished):
    break
```

(1) `finished` 是 (B,) 的布尔向量，记录每行是否已生成过 EOS；(2) `torch.where` 把已完成行的采样结果**强制替换成 EOS**——这些行仍参与每步的前向（形状必须整齐），但产出的 token 无意义，事后会被裁掉；换成 EOS 还有一个好处：已完成行的输入固定，不会产生奇怪的上下文；(3) 只有**所有**行都完成才 break——批量版没有逐行提前停止，这与 u5-l4 批量自一致性的取舍完全相同。

**逐行裁剪**：[ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl_batched.py:79-93](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl_batched.py#L79-L93)。把每步的 (B, 1) 拼成 (B, L) 后，逐行找到**第一个** EOS 的位置并截断到含 EOS 为止（`row_tokens[: eos_pos[0] + 1]`，与单条版「EOS 计入返回序列」的约定一致），再拼上 prompt 前缀、解码文本，返回与单条版**完全相同的三件套结构** `(full_token_ids, prompt_len, text)`——下游的奖励计算与 `sequence_logprob` 无需任何修改。

**调用侧：分批策略**：`compute_grpo_loss` 里的 [ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl_batched.py:135-163](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl_batched.py#L135-L163) 用 `while remaining > 0` 循环把 `num_rollouts` 条按 `batch_size` 分批（`batch_size` 缺省等于 `num_rollouts`，即一次全并行）。所以 `num_rollouts` 决定一组有多少条 rollout（算法量），`batch_size` 决定一次并行生成多少条（资源量）——两者解耦后，显存不足时可以 `num_rollouts=8, batch_size=4` 分两批跑，算法语义不变。

**代价**：批量化用显存换吞吐。README 提醒它「increases the memory requirements and may thus require lowering the number of rollouts and rollout lengths」，且附了资源表（[ch06/02_rlvr_grpo_scripts_intro/README.md:14-16](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch06/02_rlvr_grpo_scripts_intro/README.md#L14-L16)、[120-130](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch06/02_rlvr_grpo_scripts_intro/README.md#L120-L130)）。

#### 4.4.4 代码实践

**实践目标**：把批量化带来的吞吐提升点用自己的话总结出来（这正是本讲实践任务的第二半）。

**操作步骤**：并排打开单条版 [rlvr_grpo_original_no_kl.py:35-82](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl.py#L35-L82) 与批量版 [rlvr_grpo_original_no_kl_batched.py:33-95](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl_batched.py#L33-L95)，逐行对照后写下 5 句话总结。以下是一份参考答案（**建议先自己写再看**）：

1. **prefill 只做一次**：同一 prompt 复制 B 份后整批前向，B 条 rollout 共享一次 prefill 的算力与时间，而单条版要串行做 B 次。
2. **decode 步在 batch 维并行**：每步把 (B, 1) 的新 token 一次送入模型，B 行的矩阵乘合并成一个大 GEMM，GPU 利用率远高于逐条的小 batch（=1）前向。
3. **采样也算批量**：softmax、top-p 过滤与 `multinomial` 都作用在 (B, V) 张量上，一次调用完成 B 行的采样，省掉 Python 层 B 次循环与 B 次内核启动开销。
4. **代价一：无效前向**：先完成的行仍随最慢的行继续前向（无逐行提前停止），生成长度参差时浪费算力。
5. **代价二：显存峰值**：B 行的激活与 KV 缓存同时驻留显存，B 越大吞吐越高但显存越紧，所以脚本提供 `batch_size < num_rollouts` 的分批档位在两者间权衡。

**预期结果**：能说清「吞吐来自哪里（1～3）、代价在哪里（4～5）」即达标。若本地有 GPU，可加一个定量验证：同一 prompt 分别以单条脚本（`--num_rollouts 4`）与批量脚本（`--num_rollouts 4 --batch_size 4`）各跑 1 步，对比 CSV 中的 `tokens_per_sec` 列（**待本地验证**）。

#### 4.4.5 小练习与答案

**练习 1**：批量版为什么不需要第 3 章 `evaluate_math500_batched.py` 那样的 left-padding？

**答案**：评测脚本批量化的对象是**不同的 prompt**（长度各异，必须 padding 到同一长度并修正位置编码）；批量 rollout 的对象是**同一 prompt 的 B 份复制**，序列天然等长，位置编码也完全一致，`qwen3.py` 的原版模型即可直接处理。

**练习 2**：已完成行被 `torch.where` 强制填 EOS 后仍参与后续前向。这些「无效前向」的结果会影响该行最终返回的文本吗？

**答案**：不会。返回前逐行裁剪到**第一个** EOS（含）为止，之后生成的所有 token 都被丢弃；填 EOS 只是让已完成行的输入固定、形状保持整齐的占位手段。

**练习 3**：`num_rollouts=8`、`batch_size=3` 时，`while remaining > 0` 循环会怎么分批？最后一批 batch 是多少？

**答案**：三批，batch 分别为 3、3、2（`current_batch = min(batch_size, remaining)`，剩余 2 不足 3 时取 2）。算法层面与一次 8 条完全等价——优势仍然在全部 8 条 rollout 组成的组内标准化。

### 4.5 FSDP 多 GPU 变体（概览）

#### 4.5.1 概念说明

`rlvr_grpo_original_no_kl_batched_fsdp.py` 是同一训练骨架的多 GPU 版本：把模型包进 PyTorch 的 `FSDP`（全分片数据并行），每张 GPU（rank）持有一片参数/梯度/优化器状态，各自处理不同的训练题，反向传播时自动跨卡聚合梯度。算法与单卡批量版**逐字相同**（`compute_grpo_loss` 完全一致），改动全部在「分布式编排」层：怎么起进程、怎么切数据、怎么避免 B 张卡写 B 份重复日志与 checkpoint、怎么把分片的权重汇聚成一份完整 `.pth`。

#### 4.5.2 核心流程

```
主进程: 校验 CUDA 可用与 --num_gpus → mp.spawn 为每张卡启动一个 worker
每个 worker (main_worker):
    rank == 0 → is_main（只有它写日志/存档）
    init_process_group(nccl) → 各自绑定 cuda:rank
    load_math_train() 全量加载 → shard_data 按 rank 切片（data[rank::world_size]）
    加载模型 → FSDP(model, device_id, use_orig_params=True) 包装
    train_rlvr_grpo(...)        # 与单卡相同的八阶段循环
    get_model_state_dict(): 以 FULL_STATE_DICT 模式把分片汇聚到 rank0、卸载到 CPU
    rank0 存最终模型 → barrier 同步 → 清理进程组
```

#### 4.5.3 源码精读

**进程组与数据切分**：[ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl_batched_fsdp.py:41-55](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl_batched_fsdp.py#L41-L55)。`setup_distributed` 用 nccl 后端初始化进程组（GPU 间高速通信）；`shard_data` 用 `data[rank::world_size]` 花式索引把 12000 题交错切成不重叠的份——各 rank 训练不同的题目，梯度经 FSDP 自动同步后，参数更新等价于单卡顺序处理全部数据。

**权重汇聚**：[ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl_batched_fsdp.py:58-67](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl_batched_fsdp.py#L58-L67)。FSDP 下 `model.state_dict()` 默认只给本 rank 的分片，`get_model_state_dict` 切换到 `FULL_STATE_DICT` 模式并配置 `offload_to_cpu=True, rank0_only=True`——各分片在 rank0 聚成完整 state_dict 且不挤占 GPU 显存，于是存出的 `.pth` 与单卡版格式相同、可互换加载。

**FSDP 包装与采样函数的适配**：[ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl_batched_fsdp.py:460](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl_batched_fsdp.py#L460) 一行 `FSDP(model, device_id=device, use_orig_params=True)` 完成包装（`use_orig_params=True` 保持原参数名，兼容 `load_state_dict` 与 checkpoint 读写）；`sample_responses_batched` 内则用 [ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl_batched_fsdp.py:87-89](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl_batched_fsdp.py#L87-L89) 的 `model.module if isinstance(model, FSDP) else model` 剥开包装去读 `cfg` 与重置 KV 缓存。

**rank0 独占日志与存档**：训练循环里所有 `append_*`、`print`、`save_checkpoint` 都包着 `if is_main`（见 [ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl_batched_fsdp.py:369-390](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl_batched_fsdp.py#L369-L390)）；`save_checkpoint` 增加 `is_main` 参数并在非主 rank 上直接返回 `None`（[ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl_batched_fsdp.py:293-304](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl_batched_fsdp.py#L293-L304)）。吞吐指标也改成了按 GPU 数外推的估计值并附注释说明「非精确跨 rank 聚合」（[ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl_batched_fsdp.py:362-368](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl_batched_fsdp.py#L362-L368)）——注释里的诚实声明本身就是好的工程习惯。

**入口守卫与进程启动**：[ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl_batched_fsdp.py:565-572](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl_batched_fsdp.py#L565-L572) 在解析参数后先做三项校验（CUDA 可用、`--num_gpus` 合法、不超过设备数）再用 `mp.spawn` 按 GPU 数拉起 worker 进程（worker 主体见 [ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl_batched_fsdp.py:426-488](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl_batched_fsdp.py#L426-L488)）。README 称其为多 GPU 用户的推荐脚本（[ch06/02_rlvr_grpo_scripts_intro/README.md:17](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch06/02_rlvr_grpo_scripts_intro/README.md#L17)）。

#### 4.5.4 代码实践（源码阅读型）

**实践目标**：不依赖多 GPU，通过对照阅读提炼 FSDP 版相对批量版的全部增量改动。

**操作步骤**：

```bash
diff ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl_batched.py \
     ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl_batched_fsdp.py | less
```

逐块阅读 diff，把改动归入四类：分布式初始化（`setup_distributed`/`cleanup`/`mp.spawn`）、数据切分（`shard_data` 与 `math.ceil(len/world_size)` 的步数折算）、rank 去重（`is_main` 的所有出现位置）、权重汇聚（`get_model_state_dict` 与 `save_checkpoint` 的改造）。

**需要观察的现象**：`compute_grpo_loss`、`sample_responses_batched` 的生成逻辑主体在 diff 中几乎不出现（仅 `model.module` 适配一处）——算法与分布式正交。

**预期结果**：得到一张「FSDP 增量改动清单」，并能回答：为什么日志去重必须在训练循环里做而不是事后去重？（各 rank 的步数与本地数据不同步，事后无法对齐；且 B 张卡同时写同一文件本身就会交错损坏。）

#### 4.5.5 小练习与答案

**练习 1**：FSDP 与「每卡一份完整模型」的普通数据并行（DDP）在显存上的本质区别是什么？

**答案**：DDP 每张卡都驻留完整的参数、梯度与优化器状态（三份全量）；FSDP 把这三者都切片分散到各卡，只在计算瞬间临时聚合所需分片。模型越大差距越大——这正是 0.6B 以上模型多卡训练的关键。

**练习 2**：如果去掉所有 `if is_main` 判断，让每张卡都写 CSV，会发生什么？

**答案**：八张卡同时以追加模式打开同一 CSV 文件，行会交错写入（且缺乏文件锁保护），指标序列损坏；checkpoint 也会被重复写八次（FSDP 下非 rank0 取不到全量 state_dict，`get_model_state_dict` 配置了 `rank0_only=True`，其余 rank 拿到空 dict 被 `if not state_dict` 拦下，所以代码里两道防线都有）。

**练习 3**：`shard_data` 用 `data[rank::world_size]` 交错切片，相比「前 3000 条给卡 0、接着 3000 条给卡 1」的连续切片，好处是什么？

**答案**：交错切片让各 rank 拿到的数据在统计分布上更接近整体（避免某张卡恰好分到集中某类题型/难度的连续段），从而让各卡的梯度估计更均匀；连续切片在数据有排序或聚类时会造成各 rank 学习到偏斜的数据。

## 5. 综合实践

把本讲所有内容串成一个完整的小实验（CPU 可完成，模型权重首次需下载）：

**任务**：以「3 条训练数据、`num_rollouts=2`、`max_new_tokens=64`、`checkpoint_every=2`」为基线冒烟配置，完成「训练 → 产物核验 → 中断模拟 → 曲线绘制 → 批量化分析」五步：

1. **训练**：运行 4.1.4 的冒烟脚本，得到 3 步训练的 CSV 与 step00002 checkpoint；
2. **产物核验**：用 `pandas.read_csv`（或直接读文本）打印 CSV 全部内容；用 `torch.load(..., map_location="cpu")` 列出 state_dict 的张量个数与前几个键名，并与 u2-l1 讲过的模型结构（`tok_emb`、`trf_blocks.0.*` …）对照；
3. **中断模拟**：叠加 4.2.4 的 monkeypatch，确认 `step00003-interrupt.pth` 生成、CSV 停在第 2 行；
4. **曲线绘制**：先用自己 2～3 行的 CSV 跑通 `plot_metrics.py`；再对作者日志 `logs/rlvr_grpo_original_no_kl_metrics.csv`（258 步）以 `--moving_average 20` 出图，观察 loss 后期膨胀与 reward 低位震荡；
5. **批量化分析**：完成 4.4.4 的 5 句话吞吐总结。

**验收标准**：能不看书回答——一个训练 step 有哪八个阶段？中断存档的文件名长什么样？批量化为什么不需要 padding？`num_rollouts` 与 `batch_size` 各自控制什么？这四个问题答得上来，本讲就达标了。

## 6. 本讲小结

- `train_rlvr_grpo` 的八阶段骨架：**初始化 AdamW → 逐 step（`zero_grad` → `compute_grpo_loss` → `backward` → `clip_grad_norm_ 1.0` → `optimizer.step` → CSV 日志 → 定期 checkpoint）**，`KeyboardInterrupt` 被捕获并存 `stepNNNNN-interrupt.pth` 后正常返回模型。
- `save_checkpoint`（`mkdir -p` + 五位零填充步号 + suffix 区分定期/中断档）与 `append_csv_metrics`（首写表头、逐行追加、即写即关）构成训练的黑匣子；`plot_metrics.py` 是 CSV 的配套可视化，滑动平均用累积和实现。
- 独立脚本 `rlvr_grpo_original_no_kl.py` 的三处工程增强：去掉采样的 `.cpu()` 往返（GPU 吞吐 +20%）、零优势时跳过 logprob 重算与更新（省掉最贵的无效计算）、checkpoint 处自动跑 MATH-500 子集评测；`--checkpoint_path` 支持从存档恢复权重。
- `sample_responses_batched` 把同一 prompt 复制 B 份整批前向：prefill 一次、decode 与采样全在 batch 维并行；EOS 用 `finished` 掩码 + `torch.where` 填充处理、逐行截到第一个 EOS，返回结构与单条版完全一致；代价是显存峰值上升与无逐行提前停止。
- FSDP 版把算法原封不动地搬到多卡：`mp.spawn` 起进程、`data[rank::world_size]` 切数据、`is_main` 让 rank0 独占日志与存档、`FULL_STATE_DICT` 把分片汇聚成与单卡同格式的 `.pth`。

## 7. 下一步学习建议

本讲完成了第 6 章「基础 GRPO」的最后一块拼图。接下来：

1. **进入单元八（第 7 章）**：先读 `u8-l1`（ch07 的训练跟踪与熵），它把本讲的 CSV 日志扩展出 entropy、completion length 等维度；再看 `u8-l2`（clip ratio）——脚本版「rollout 时记录旧 logprob、训练时重算新 logprob」的结构正是为计算新旧策略比 `ratio` 预留的接口，你会看到 `loss = pg_loss` 那行如何长出裁剪项与 KL 项。
2. **真跑一轮并评测**：有 GPU 的读者可按 README 复现「50 步、`--num_rollouts 8`、`--max_new_tokens 512`」，再用第 3 章的 `evaluate_math500.py --checkpoint_path` 评测存档，亲手验证 47.4% 那一行。
3. **顺藤摸瓜读 PR**：README 引用的 [PR #178](https://github.com/rasbt/reasoning-from-scratch/pull/178)（去 `.cpu()`）与 [PR #186](https://github.com/rasbt/reasoning-from-scratch/pull/186)（跳过零优势）记录了两个优化的实测数据与讨论，是「教学版 → 工程版」演化的第一手材料。
4. **预告第 8 章视角**：GRPO 用「答对给 1 分」从零探索解法；第 8 章蒸馏（单元九）则直接模仿教师模型的完整解题文本。学完 u8 后回头对比两条路线的损失函数与数据需求，会对「训练推理模型」的设计空间（design space）有完整把握。

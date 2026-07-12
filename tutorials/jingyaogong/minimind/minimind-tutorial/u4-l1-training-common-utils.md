# 训练公共工具：日志、学习率、种子与采样器

## 1. 本讲目标

前面三个单元里，我们已经走通了「文本 → token id → 训练张量 → 模型前向 → 损失」的完整链路，也看懂了模型结构。从本单元（u4）开始，我们要回答一个更工程化的问题：**这些模型到底是怎么被训出来的？** 训练一个语言模型，除了模型本身和数据，还需要一整套「训练公共设施」——谁来打印日志、学习率怎么变化、怎么保证每次实验可复现、怎么加载已有权重、断点之后怎么接着训。

本讲聚焦 `trainer/trainer_utils.py` 这个「训练工具箱」，读完本讲你应该能够：

1. 理解 `get_lr` 的余弦退火（cosine annealing）公式，并能说出它在第 0 步和最后一步分别取多少；同时理解 MiniMind **不用** PyTorch 的 `lr_scheduler`，而是在每一步手动改写优化器学习率的原因与做法。
2. 掌握 `init_model` 如何根据 `from_weight` 决定「加载已有权重」还是「随机初始化」，以及它如何统计并打印模型总参数量、可训练参数量。
3. 理解 `SkipBatchSampler` 如何通过「跳过前 N 个 batch」来支持断点续训，以及它与全局步数 `iters` 的配合关系。
4. 理解 `setup_seed` 为什么要在多个随机源上同时设种子，以及分布式训练时为什么还要叠加 `rank` 偏移。
5. 顺带认识 `Logger` / `is_main_process` / `init_distributed_mode` 这组「只在主进程打印、自动探测是否进入 DDP」的基础设施。

> 本讲是 u4 单元的基础课。`lm_checkpoint` 的检查点双重保存与跨卡 step 换算留到 **u4-l2**，`DistributedDataParallel` + 混合精度 + 梯度累积的完整训练循环留到 **u4-l3**，本讲只讲「公共组件」本身。

## 2. 前置知识

在进入源码前，先用大白话对齐几个概念：

- **学习率（learning rate, lr）**：每次反向传播后，梯度乘以的一个系数，决定参数「走多大一步」。lr 太大训练发散，太小训练太慢。一个好的 lr 通常不是常数，而是「先大后小」的曲线。
- **学习率调度（lr schedule）**：让 lr 随训练步数变化的策略。常见有「余弦退火」「线性衰减」「warmup + 余弦」等。本讲的 `get_lr` 就是一种余弦退火。
- **优化器的 param_groups**：PyTorch 的 `AdamW` 等优化器把待优化参数分成若干组（`param_groups`），每组有自己的 `lr`。改某一组的 `lr` 就能即时改变这一组参数的学习率，这是手动调度的基础。
- **随机种子（seed）**：计算机里的「随机数」其实是伪随机，给定一个种子，整条随机数序列就确定了。设固定种子能让实验可复现。
- **可复现性（reproducibility）**：同样的代码、数据、种子，跑两次得到几乎一样的结果。这对调试和科研对照很重要。
- **断点续训（resume）**：训练中途机器挂了，重启后能从上次保存的进度继续，而不是从头再来。本讲的 `SkipBatchSampler` 就是续训链条上的一环。
- **DDP（DistributedDataParallel）**：多卡分布式训练，每张卡跑一个进程，各算各的梯度再平均。涉及「主进程（rank 0）」和「非主进程」之分。

如果你对「模型前向 → loss → backward」的主链路还陌生，建议先读 **u3-l5（CausalLM 前向传播与交叉熵损失）**，本讲会频繁提到 `res.loss + res.aux_loss` 这个返回结构。

## 3. 本讲源码地图

本讲几乎全部内容都集中在一个文件里，外加一个「调用方」用来展示这些工具怎么被用起来。

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `trainer/trainer_utils.py` | 训练工具函数集合，所有 `train_*.py` 都从这里 import 公共组件 | `Logger`、`is_main_process`、`init_distributed_mode`、`setup_seed`、`get_lr`、`init_model`、`SkipBatchSampler`、`get_model_params` |
| `trainer/train_pretrain.py` | 预训练脚本，是这些工具的「典型调用方」 | 看 `get_lr` 怎么被手动写回优化器、`SkipBatchSampler` 怎么接到 DataLoader、`init_model`/`setup_seed` 在主流程里的位置 |

需要说明：`trainer_utils.py` 里还有 `lm_checkpoint`（检查点保存/加载）和 `LMForRewardModel`（奖励模型封装），前者属于 **u4-l2** 的主题，后者属于 **u7-l3**（强化学习奖励信号），本讲不展开，只在用到时一句话带过。

## 4. 核心概念与源码讲解

### 4.1 分布式与日志基础：init_distributed_mode / is_main_process / Logger

#### 4.1.1 概念说明

多卡训练时，同一份脚本会被启动多次（每个 GPU 一个进程）。如果每个进程都打印日志，屏幕上就会出现 N 份重复信息，训练日志会乱成一锅粥。所以 MiniMind 设计了一条简单规矩：**只有 rank 0（主进程）才打印日志**，其他进程默默干活。

这背后还有一个前置问题：脚本怎么知道自己是不是在「DDP 模式」下被启动？答案在环境变量——`torchrun` 启动时会注入 `RANK`、`LOCAL_RANK`、`WORLD_SIZE` 等环境变量，单卡 `python xxx.py` 跑时这些变量不存在。`init_distributed_mode` 就是靠探测 `RANK` 来判断的。

#### 4.1.2 核心流程

1. `init_distributed_mode()` 读环境变量 `RANK`：
   - 没设（等于 -1）→ 返回 `0`，表示非 DDP，按单进程跑。
   - 有设 → 调用 `dist.init_process_group("nccl")` 初始化进程组，从 `LOCAL_RANK` 取到本进程在本机用第几张卡，`torch.cuda.set_device` 绑定设备，最后返回 `local_rank`。
2. `is_main_process()`：如果没初始化分布式（单进程），或在分布式里 `rank == 0`，都算「主进程」。
3. `Logger(content)`：包了一层 `print`，只有 `is_main_process()` 为真时才真正打印。

#### 4.1.3 源码精读

`init_distributed_mode` 靠环境变量自动探测，单卡时返回 0，多卡时返回 `local_rank`：

[trainer/trainer_utils.py:44-L51](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/trainer_utils.py#L44-L51) — 探测 `RANK` 环境变量，决定是否进入 DDP；进入则初始化 NCCL 进程组并绑定本机 GPU。

`is_main_process` 是「是否主进程」的唯一判定，逻辑是「没分布式 或 rank==0」：

[trainer/trainer_utils.py:31-L32](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/trainer_utils.py#L31-L32) — 单进程时 `dist.is_initialized()` 为 False 直接返回 True；分布式时只有 rank 0 返回 True。

`Logger` 只是 `print` 的「主进程门控」版本，所以你在所有训练脚本里看到的 `Logger(...)` 都天然只在主进程输出一次：

[trainer/trainer_utils.py:35-L37](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/trainer_utils.py#L35-L37) — 用 `is_main_process()` 守卫 `print`，避免多卡重复打印。

#### 4.1.4 代码实践

**实践目标**：体会「主进程门控」的效果。

**操作步骤**（这是一个「源码阅读 + 思考型」实践，不强制运行多卡）：

1. 阅读上面三段源码，理解 `is_main_process()` 在单进程下恒为 True。
2. 在 `train_pretrain.py` 的主流程里找到 `init_distributed_mode()` 的调用点：

   [trainer/train_pretrain.py:110-L112](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_pretrain.py#L110-L112) — 第 1 步：先初始化分布式（单卡时 `local_rank=0`），再据此决定 `args.device`，最后设种子。

3. 想清楚：如果用 `python train_pretrain.py`（单进程）启动，`os.environ.get("RANK", -1)` 返回 -1，函数直接 `return 0`，`dist.is_initialized()` 为 False，于是 `args.device` 不被改写、`Logger` 正常打印。
4. 如果改用 `torchrun --nproc_per_node 2 train_pretrain.py`，则每个进程都会进入 `init_process_group`，但只有 rank 0 的 `Logger` 会输出，rank 1 静默。

**需要观察的现象**：单卡时日志正常；多卡时即便启动了 2 个进程，训练日志也只出现一份（来自 rank 0）。

**预期结果**：理解「日志去重」依赖 `is_main_process()`，而这个判定又依赖 `dist.is_initialized()`。

#### 4.1.5 小练习与答案

**练习 1**：如果不小心把 `Logger` 换成普通 `print`，多卡训练时会发生什么？

**参考答案**：每个进程都会执行 `print`，于是每条日志在屏幕上重复 N 次（N = GPU 数），日志变得难以阅读，也可能拖慢 I/O。

**练习 2**：`init_distributed_mode()` 在什么情况下返回非 0 值？

**参考答案**：只有当环境变量 `RANK` 被设置（即通过 `torchrun` 等 launcher 启动）时，才进入 DDP 分支并返回 `LOCAL_RANK`；否则返回 0 表示单进程。

---

### 4.2 setup_seed：让训练可复现

#### 4.2.1 概念说明

深度学习的「随机性」来自很多地方：Python 的 `random`、NumPy 的随机数、PyTorch 的 CPU/GPU 随机数、cuDNN 的算法选择（cuDNN 对同一个卷积/矩阵乘可能有多种实现，默认会挑最快的，但不同实现数值略有差异）。要真正做到「同种子同结果」，就得把这些随机源**全部钉死**，并关掉 cuDNN 的「自动挑最快」行为（`benchmark`）。

#### 4.2.2 核心流程

`setup_seed(seed)` 依次：

1. `random.seed(seed)` — Python 内置随机。
2. `np.random.seed(seed)` — NumPy 随机。
3. `torch.manual_seed(seed)` — PyTorch CPU 随机。
4. `torch.cuda.manual_seed(seed)` / `manual_seed_all(seed)` — 当前卡 / 所有卡的 GPU 随机。
5. `torch.backends.cudnn.deterministic = True` — 强制 cuDNN 用确定性算法。
6. `torch.backends.cudnn.benchmark = False` — 关闭「自动选最快实现」，避免不同运行挑不同算法。

注意一个工程细节：在 `train_pretrain.py` 里，`setup_seed` 被调用时传的不是固定值 42，而是 `42 + rank`：

[trainer/train_pretrain.py:112](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_pretrain.py#L112) — `setup_seed(42 + (dist.get_rank() if dist.is_initialized() else 0))`。

为什么多卡时每张卡要**加一个 rank 偏移**？因为如果所有卡用同一个种子，它们生成的数据顺序、Dropout 掩码会完全一样，多卡就失去了「数据并行带来多样性」的意义。给每张卡一个不同的种子（`42+rank`），各卡看到的数据增强、随机采样才有差异，等价于每张卡在探索数据空间的不同部分。

#### 4.2.3 源码精读

`setup_seed` 把所有随机源一次性钉死，并牺牲一点速度换取确定性：

[trainer/trainer_utils.py:54-L61](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/trainer_utils.py#L54-L61) — 覆盖 Python/NumPy/CPU/GPU 四类随机源，并强制 cuDNN 确定性、关闭 benchmark。

#### 4.2.4 代码实践

**实践目标**：直观验证「同种子 → 同结果」。

**操作步骤**（示例代码，非项目原有）：

```python
# 示例代码：保存为 /tmp/seed_check.py 后运行
import sys, os
sys.path.append(os.path.abspath('.'))
from trainer.trainer_utils import setup_seed
import torch

def rand_run(seed):
    setup_seed(seed)
    return torch.randn(3)

print('seed=42 第一次:', rand_run(42))
print('seed=42 第二次:', rand_run(42))   # 应与上一行完全相同
print('seed=43      :', rand_run(43))     # 应与前两行不同
```

**需要观察的现象**：前两行输出的三个浮点数逐位相同；第三行不同。

**预期结果**：同种子完全可复现。注意 GPU 上由于硬件/驱动差异，跨机器仍可能有极小数值偏差，但同一台机器同种子应当一致。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `torch.backends.cudnn.benchmark = False` 改成 `True`，可复现性会受影响吗？

**参考答案**：会。`benchmark=True` 时 cuDNN 会在首次运行时对多种卷积实现做基准测试并选最快的，不同运行可能选中不同实现，导致数值不完全一致。代价是关掉它可能略慢。

**练习 2**：为什么多卡训练时种子要写成 `42 + rank` 而不是统一用 `42`？

**参考答案**：统一用 `42` 会让各卡产生相同的随机性（如相同的数据采样顺序），削弱数据并行的多样性；加 rank 偏移让各卡各有各的随机轨迹，相当于并行探索数据空间不同区域。

---

### 4.3 get_lr：余弦退火学习率与「手动设 lr」

#### 4.3.1 概念说明

`get_lr` 是本讲最重要的一个函数，它定义了 MiniMind 训练全程的学习率曲线。它采用的是**余弦退火（cosine annealing）**：学习率从最大值出发，沿着余弦曲线的下降半周期平滑滑向最小值。

余弦退火的好处是「中间过渡平滑」：训练初期 lr 大、学得快；后期 lr 小、精细收敛；中间没有突变（对比「线性衰减」前期降得快、「阶梯衰减」有跳变），对优化器比较友好。

一个值得强调的工程取舍：**MiniMind 没有用 PyTorch 标准的 `torch.optim.lr_scheduler`**，而是每一步自己算 lr，再手动塞回 `optimizer.param_groups`。这种「手动调度」的好处是完全透明、可控——你能在日志里、在断点续训时、在任何想插手的地方直接改 lr，而不用理解 scheduler 内部的 `last_epoch`、`_step_count` 等隐藏状态。

#### 4.3.2 核心流程

`get_lr(current_step, total_steps, lr)` 的公式只有一行，但内涵丰富。设 \(t\) 为 `current_step`，\(T\) 为 `total_steps`，\(L\) 为传入的基础学习率 `lr`，则：

\[
\text{lr}(t) = L \cdot \left(0.1 + 0.45\left(1 + \cos\left(\frac{\pi t}{T}\right)\right)\right)
\]

我们来算两个端点：

- 训练开始 \(t = 0\)：\(\cos(0) = 1\)，于是
  \[
  \text{lr}(0) = L \cdot (0.1 + 0.45 \times 2) = L \times 1.0 = L
  \]
  即**起始学习率就是传入的 `lr` 本身**。

- 训练结束 \(t = T\)：\(\cos(\pi) = -1\)，于是
  \[
  \text{lr}(T) = L \cdot (0.1 + 0.45 \times 0) = L \times 0.1
  \]
  即**终点学习率降到起始的 10%**，而不是降到 0。

中间过程是一条从 1.0 平滑下降到 0.1 的「倒余弦」曲线。注意它**没有 warmup 阶段**（不像很多大模型配方会先线性升到峰值再余弦降），第 0 步直接就是峰值 lr，这是 MiniMind「大道至简」的体现——小模型、小数据量下省去 warmup 也能稳定训练。

至于「手动设 lr」，流程是：

1. 每个 step，训练循环用全局步数 `epoch * iters + step` 调 `get_lr` 算出当前 lr。
2. 遍历 `optimizer.param_groups`，把每个组的 `'lr'` 字段直接赋为新值。
3. 优化器在下一次 `step()` 时就会用这个新 lr。

#### 4.3.3 源码精读

`get_lr` 本体极其简短，一行就描述了整条余弦曲线：

[trainer/trainer_utils.py:40-L41](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/trainer_utils.py#L40-L41) — 余弦退火公式：lr 从 `lr`（t=0）平滑降到 `0.1*lr`（t=T），系数 `0.1` 是最小学习率比例。

「手动写回优化器」的调用点在 `train_epoch` 里，这是理解整个调度如何生效的关键：

[trainer/train_pretrain.py:31-L33](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_pretrain.py#L31-L33) — 每个 step 先用全局步数算出 lr，再写回所有 `param_groups`，实现手动调度（而不是用 `lr_scheduler.step()`）。

注意这里 `get_lr` 的第一个参数是 `epoch * iters + step`，这是**跨 epoch 的全局步数**；`total_steps` 是 `args.epochs * iters`，即整个训练的总步数。也就是说，余弦曲线是横跨所有 epoch 的「一条大曲线」，而不是每个 epoch 重新退火一次。这一点在主流程的调用处也能印证：

[trainer/train_pretrain.py:165](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_pretrain.py#L165) — `train_epoch(epoch, loader, len(loader) + skip, start_step, wandb)`，第三个参数 `iters = len(loader) + skip`，断点续训时把跳过的 step 加回来，保证 lr 曲线在全局坐标系里连续，不会因续训而错位。

#### 4.3.4 代码实践

**实践目标**：把 `get_lr` 的学习率曲线画出来，直观看到「余弦从 1.0 降到 0.1」。

**操作步骤**（示例代码）：

```python
# 示例代码：保存为 /tmp/plot_lr.py，在项目根目录运行 python /tmp/plot_lr.py
import sys, os
sys.path.append(os.path.abspath('.'))
from trainer.trainer_utils import get_lr
import matplotlib.pyplot as plt

total_steps = 1000
base_lr = 5e-4
steps = list(range(total_steps + 1))
lrs = [get_lr(s, total_steps, base_lr) for s in steps]

plt.figure(figsize=(8, 4))
plt.plot(steps, lrs, label='lr(t)')
plt.xlabel('global step')
plt.ylabel('learning rate')
plt.title('MiniMind cosine annealing (get_lr)')
plt.axhline(base_lr, ls='--', color='g', alpha=0.5, label=f'max lr = {base_lr}')
plt.axhline(base_lr * 0.1, ls='--', color='r', alpha=0.5, label=f'min lr = {base_lr*0.1:.1e}')
plt.legend()
plt.grid(True, alpha=0.3)
plt.tight_layout()
plt.savefig('/tmp/get_lr_curve.png', dpi=120)
print('saved to /tmp/get_lr_curve.png')
print(f'lr(0)   = {lrs[0]:.6e}   (应为 {base_lr:.6e})')
print(f'lr(T/2) = {lrs[total_steps//2]:.6e}   (应在 max 与 min 之间)')
print(f'lr(T)   = {lrs[-1]:.6e}   (应为 {base_lr*0.1:.6e})')
```

**需要观察的现象**：曲线从左上 `5e-4` 出发，平滑向右下滑落，最终稳定在 `5e-5`（即 `0.1*lr`）；曲线关于中点对称，形状是「倒扣的半波余弦」。

**预期结果**：终端打印的 `lr(0)` 恰等于 `base_lr`，`lr(T)` 恰等于 `0.1*base_lr`，`lr(T/2)` 约等于 `0.55*base_lr`（因为 cos(π/2)=0，此时系数为 0.1+0.45=0.55）。如果没装 matplotlib，可只打印数值表格验证端点。

#### 4.3.5 小练习与答案

**练习 1**：如果把公式里的常数 `0.1` 改成 `0.0`，学习率曲线会变成什么样？为什么这通常不是好主意？

**参考答案**：终点会降到 0（系数变为 `0.45*(1+cos(...))`，t=T 时为 0）。学习率归零意味着最后阶段完全不更新参数，往往还没充分收敛就「停摆」；保留 10% 的最小学习率（`0.1`）能让模型在最后阶段仍做微小调整，通常收敛更好。

**练习 2**：为什么 `get_lr` 的第一个参数要用全局步数 `epoch * iters + step`，而不是单 epoch 内的 `step`？

**参考答案**：因为余弦曲线被设计成横跨整个训练（`total_steps = epochs * iters`）的一条曲线。如果用单 epoch 的 step，每个 epoch 都会重新从峰值开始退火，lr 会出现「锯齿」反复升降，破坏全局的「先大后小」节奏。

**练习 3**：MiniMind 选择「手动写回 param_groups 的 lr」而不是用 `lr_scheduler`，至少说出一个好处。

**参考答案**：完全透明可控——不需要维护 scheduler 的内部状态（`last_epoch` 等），断点续训时只需用全局 step 重新算一次即可，lr 与 step 的对应关系一目了然；也方便在任何位置插入自定义的 lr 调整逻辑。

---

### 4.4 init_model：加载/初始化权重并统计参数

#### 4.4.1 概念说明

训练脚本启动时面临一个抉择：是从随机权重开始训练（预训练场景），还是从已有权重接着训（SFT、LoRA、DPO 等场景）？`init_model` 用一个 `from_weight` 参数统一处理这两种情况：

- `from_weight='none'`：不加载任何权重，模型用 PyTorch 默认的随机初始化（`MiniMindForCausalLM(lm_config)` 构造时各层参数已被随机初始化），从头训练。
- `from_weight='pretrain'`、`'full_sft'` 等：按命名规则拼出权重文件路径，加载已有权重作为起点。

权重文件名的命名规则在前面几讲已多次出现，这里再次明确：`{from_weight}_{hidden_size}{_moe?}.pth`。例如 `from_weight='full_sft'`、`hidden_size=768`、非 MoE，则加载 `../out/full_sft_768.pth`；若是 MoE 则追加 `_moe` 后缀。

此外，`init_model` 还顺手做了两件事：用 `get_model_params` 打印模型总参数量（Dense 直接打印，MoE 打印「总参数-激活参数」格式），以及打印可训练参数量。

#### 4.4.2 核心流程

1. 用 `AutoTokenizer.from_pretrained` 加载分词器（默认从 `../model` 读 `tokenizer_config.json` 等）。
2. `model = MiniMindForCausalLM(lm_config)` 构造模型——此时所有权重已被随机初始化。
3. 若 `from_weight != 'none'`：
   - 拼 `weight_path = {save_dir}/{from_weight}_{hidden_size}{_moe?}.pth`。
   - `torch.load(weight_path, map_location=device)` 读权重。
   - `model.load_state_dict(weights, strict=False)` 载入。注意 **`strict=False`**——允许权重字典和模型结构有少量键不匹配（比如加载时缺了某些 buffer），不报错。
4. `get_model_params(model, lm_config)` 打印总参数量（MoE 模式额外算激活参数）。
5. 打印可训练参数量 `Trainable Params`（`p.requires_grad` 为真的参数之和）。
6. `model.to(device)` 后连同 tokenizer 一起返回。

#### 4.4.3 源码精读

`init_model` 是「构造 → 可选加载 → 统计 → 上设备」的标准模板：

[trainer/trainer_utils.py:119-L131](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/trainer_utils.py#L119-L131) — 先随机构造模型，`from_weight!='none'` 时按命名规则加载 `.pth`（`strict=False` 容错），再打印总参数与可训练参数。

`get_model_params` 值得单独看一眼，因为它展示了「如何算 MoE 激活参数」这个常见需求：

[trainer/trainer_utils.py:18-L28](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/trainer_utils.py#L18-L28) — 先算总参数 `total`；通过匹配 `'mlp.experts.0.'` 估算单个专家参数量，乘以专家数得专家总参数；用 `total - 专家总参数` 得到「非专家的 base 参数」；激活参数 = base + 单专家参数 × 每 token 激活专家数。MoE 时打印 `XM-AyM` 格式。

`init_model` 在训练脚本主流程里的位置（第 5 步，定义模型/数据/优化器）：

[trainer/train_pretrain.py:134-L138](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_pretrain.py#L134-L138) — `init_model(lm_config, args.from_weight, device=args.device)` 返回模型与分词器，紧接着构造数据集、GradScaler、AdamW 优化器。

#### 4.4.4 代码实践

**实践目标**：用同一份 `lm_config` 分别体验「从头初始化」和「加载已有权重」，并观察参数量打印。

**操作步骤**（示例代码，需在项目根目录、且 `../out/` 下存在某个权重时运行）：

```python
# 示例代码：保存为 /tmp/init_model_check.py
import sys, os
sys.path.append(os.path.abspath('.'))
from model.model_minimind import MiniMindConfig
from trainer.trainer_utils import init_model

lm_config = MiniMindConfig(hidden_size=512, num_hidden_layers=4)  # 用一个小配置省显存

# (1) 从头初始化
print('=== from_weight=none（随机初始化）===')
m1, tok = init_model(lm_config, from_weight='none', device='cpu')

# (2) 若 ../out/ 下有 full_sft_512.pth，则加载它；否则跳过
import glob
if glob.glob('../out/full_sft_512.pth'):
    print('=== from_weight=full_sft（加载已有权重）===')
    m2, _ = init_model(lm_config, from_weight='full_sft', save_dir='../out', device='cpu')
else:
    print('未找到 ../out/full_sft_512.pth，跳过加载演示。可先用 pretrain 跑几步生成权重。')
```

**需要观察的现象**：两次都会先打印 `Model Params: XX.XXM`（总参数量），再打印 `Trainable Params: XX.XXXM`（可训练参数量）。若 `lm_config.use_moe=True`，第一行会变成 `XM-AyM` 格式。

**预期结果**：小配置下总参数量明显小于默认 768×8 的 64M；两次的参数量数字相同（因为参数量只取决于 `lm_config`，与是否加载权重无关），区别仅在权重数值。

**待本地验证**：`get_model_params` 在 `active < total` 时才打印 `XM-AyM`，Dense 模型（`active == total`）走 `else` 分支只打印 `XM`——这一点可在 `use_moe=1` 时另行验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `load_state_dict(weights, strict=False)` 用了 `strict=False`？宽松加载有什么风险？

**参考答案**：`strict=False` 允许权重字典与模型结构存在键不匹配（多余或缺失的键都不报错），用于容错——比如权重是用旧版结构保存的，或某些 buffer 不需要加载。风险是：如果关键层意外没被加载（键名拼错），模型会悄悄保留随机权重而不会报错，导致训练从错误的起点开始，难以察觉。

**练习 2**：MoE 模型打印的 `198M-A64M` 是什么含义？

**参考答案**：`198M` 是所有专家参数的总和（模型的总容量），`A64M` 是每个 token 实际激活的参数量（top-1 路由下，每 token 只走 1 个专家，所以激活参数远小于总参数）。这是 MoE 「大容量、稀疏激活」的核心卖点。

**练习 3**：`init_model` 返回的 `model` 在 `load_state_dict` 之后、`.to(device)` 之前，参数已经在 CPU 上了。如果直接拿这个模型训练会怎样？

**参考答案**：数据和模型必须在同一设备上才能计算。`init_model` 最后做了 `model.to(device)`，把模型搬到目标设备（通常 `cuda`），保证后续 `model(input_ids.to(device))` 时张量与参数同卡。不搬设备会因「input 在 cuda、参数在 cpu」而报错。

---

### 4.5 SkipBatchSampler：断点续训的「跳过」机制

#### 4.5.1 概念说明

训练大模型动辄几个小时甚至几天，中途机器宕机、显存溢出、手动 Ctrl+C 是家常便饭。如果不做断点续训，每次重启都从 epoch 0 的第 0 步开始，前面几十万步白跑。MiniMind 的续训方案在 u4-l2 会完整讲（检查点里存了模型、优化器、step 等），本讲只看续训链条上和数据有关的一环：**已经训练过的那些 batch，重启后要被「跳过」，否则同一批数据会被重复训练，相当于训练步数对不上、lr 曲线也会错位。**

`SkipBatchSampler` 就是为「跳过前 N 个 batch」而生的。它本质上是一个**批采样器（BatchSampler）**：外面包一层普通采样器（sampler），按 `batch_size` 把样本索引攒成一个个 batch，然后决定哪些 batch 该跳过、哪些该交给 DataLoader 取出。

#### 4.5.2 核心流程

`SkipBatchSampler(sampler, batch_size, skip_batches=0)`：

- `__iter__`：
  1. 从底层 `sampler` 逐个取样本索引，攒进 `batch` 列表。
  2. 攒够 `batch_size` 个 → 得到一个完整 batch：
     - 若已跳过数 `skipped < skip_batches`：丢弃这个 batch，`skipped += 1`。
     - 否则：`yield` 这个 batch（交给 DataLoader 取数据）。
  3. 末尾若有不足一个 batch 的余量，且已跳过阶段结束，也 `yield`。
- `__len__`：返回「总 batch 数 − skip_batches」，保证 DataLoader 知道还剩多少 batch。

续训时，`skip_batches` 被设成检查点里保存的 `step`，从而跳过已训练的 batch。配合全局步数 `iters = len(loader) + skip`，lr 曲线在全局坐标系里依然连续（见 4.3.3）。

一个容易混淆的点：单卡时 `SkipBatchSampler` 包的是 `indices`（一个 `torch.randperm` 打乱后的索引列表）；多卡时包的是 `DistributedSampler`（每个 rank 只看到自己那份子集）。两者都「可迭代出索引」，所以 `SkipBatchSampler` 不用区分。

#### 4.5.3 源码精读

`SkipBatchSampler` 的 `__iter__` 用「攒满即丢 / 攒满即交」两态机实现了跳过逻辑：

[trainer/trainer_utils.py:134-L157](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/trainer_utils.py#L134-L157) — 按 `batch_size` 攒 batch；前 `skip_batches` 个 batch 计数后丢弃，之后的 batch 才 `yield`；`__len__` 用总 batch 数减去 skip_batches 保证步数账对得上。

`__len__` 里向上取整的写法 `(len(sampler) + batch_size - 1) // batch_size` 是经典的「整数除法向上取整」技巧，等价于 `math.ceil(len/batch_size)`，用于处理末尾不足一个 batch 的情况。

在主流程里，`SkipBatchSampler` 的接入点是续训的关键：

[trainer/train_pretrain.py:159-L167](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_pretrain.py#L159-L167) — 每个 epoch 重新设种子并打乱索引；`skip = start_step` 仅在「续训的首个 epoch」生效；用 `SkipBatchSampler(train_sampler or indices, batch_size, skip)` 包一层后传给 `DataLoader(batch_sampler=...)`；若 skip>0，把 `iters` 设为 `len(loader) + skip`，让 lr 曲线与全局步数对齐。

注意第 161 行 `train_sampler or indices`：这是 Python 短路求值——DDP 模式下 `train_sampler` 非空（真），用 `DistributedSampler`；单卡模式下 `train_sampler = None`（假），退回用 `indices`（randperm 列表）。一行代码兼容了单卡与多卡。

#### 4.5.4 代码实践

**实践目标**：构造一个小数据集和 `SkipBatchSampler`，验证「跳过前 N 个 batch」后，DataLoader 确实从第 N+1 个 batch 开始取。

**操作步骤**（示例代码）：

```python
# 示例代码：保存为 /tmp/skip_sampler_check.py，项目根目录运行
import sys, os
sys.path.append(os.path.abspath('.'))
from trainer.trainer_utils import SkipBatchSampler

# 用一个简单的索引列表当作 sampler（模拟 train_ds 的样本索引）
all_indices = list(range(10))         # 10 个样本
batch_size = 3
skip = 2                              # 假设续训要跳过前 2 个 batch（=6 个样本）

# 不跳过时
print('=== skip=0（从头取）===')
s0 = SkipBatchSampler(all_indices, batch_size, skip_batches=0)
print('batch 数:', len(s0))
print('取到的 batch:', [b for b in s0])

# 跳过前 2 个 batch
print('\n=== skip=2（跳过前 2 个 batch）===')
s2 = SkipBatchSampler(all_indices, batch_size, skip_batches=skip)
print('batch 数:', len(s2), ' (应为', len(s0) - skip, ')')
print('取到的 batch:', [b for b in s2])
```

**需要观察的现象**：`skip=0` 时取到 4 个 batch（`[0,1,2] [3,4,5] [6,7,8] [9]`，共 ceil(10/3)=4 个）；`skip=2` 时只取到 2 个 batch（`[6,7,8] [9]`），即跳过了前两个 batch（`[0,1,2]` 和 `[3,4,5]`）。

**预期结果**：`len(s2) == len(s0) - 2`；`skip=2` 取到的第一个 batch 正是 `skip=0` 时的第三个 batch，证明「跳过」逻辑正确。

**待本地验证**：在真实 `DataLoader(train_ds, batch_sampler=...)` 接入时，被跳过的 batch 不会被 DataLoader 真正读取（不触发 Dataset 的 `__getitem__`），可在 Dataset 里加一行 `print` 印证这一点。

#### 4.5.5 小练习与答案

**练习 1**：续训时为什么必须把 `iters` 设成 `len(loader) + skip`，而不是直接用 `len(loader)`？

**参考答案**：`get_lr` 的全局步数是 `epoch * iters + step`，`total_steps` 是 `epochs * iters`。如果续训后用「缩小后的 `len(loader)`」当 iters，全局步数会比真实进度小，lr 会比应有的值偏大（曲线错位），破坏了与断点前一致的退火节奏。加回 `skip` 让全局坐标系连续。

**练习 2**：`SkipBatchSampler` 在 `__iter__` 里跳过 batch 时，是被「丢弃」还是「取出后丢弃」？

**参考答案**：是被「丢弃」——它操作的是**样本索引**，不是真实数据。被跳过的 batch 只是索引没有被 `yield` 给 DataLoader，因此 DataLoader 不会去 Dataset 取真实样本，省下了被跳过数据的加载开销。这正是把「跳过」做在 BatchSampler 层（而非 Dataset 层）的好处。

**练习 3**：`SkipBatchSampler(train_sampler or indices, ...)` 里为什么用 `or` 而不是 `if/else`？

**参考答案**：Python 的短路求值：`train_sampler or indices` 表示「`train_sampler` 非空（真）就用它，否则用 `indices`」。DDP 时 `train_sampler` 是 `DistributedSampler`（真），单卡时是 `None`（假）退回 `indices`。这是用一行表达式兼容两种模式的惯用写法。

---

## 5. 综合实践

把本讲的几个组件串起来，模拟一个「迷你训练循环」骨架（不真正训练，只演示 lr 调度 + 跳过采样器的协作）：

```python
# 示例代码：保存为 /tmp/mini_train_skeleton.py，项目根目录运行
import sys, os
sys.path.append(os.path.abspath('.'))
from trainer.trainer_utils import get_lr, SkipBatchSampler, setup_seed

setup_seed(42)

# 模拟：100 个样本，batch_size=10 → 每 epoch 10 个 batch；共 3 个 epoch
n_samples = 100
batch_size = 10
epochs = 3
iters_per_epoch = (n_samples + batch_size - 1) // batch_size   # 10
total_steps = epochs * iters_per_epoch                          # 30
base_lr = 5e-4

# 模拟「在第 1 个 epoch 训练到 step 4 时中断」的续训场景
start_epoch, start_step = 0, 4

for epoch in range(start_epoch, epochs):
    indices = list(range(n_samples))   # 简化：不重新打乱
    skip = start_step if (epoch == start_epoch and start_step > 0) else 0
    sampler = SkipBatchSampler(indices, batch_size, skip)
    iters = len(sampler) + skip        # 关键：加回 skip，让全局步数连续
    print(f'\n[Epoch {epoch}] skip={skip}, 本 epoch 要跑 {len(sampler)} 个 batch, iters={iters}')
    for local_step, batch in enumerate(sampler, start=skip + 1):
        global_step = epoch * iters_per_epoch + local_step
        lr = get_lr(global_step, total_steps, base_lr)
        if local_step <= skip + 1 or local_step % 3 == 0:   # 只打印少量步
            print(f'  global_step={global_step:2d}/{total_steps}, lr={lr:.6e}, batch(首尾)=[{batch[0]}..{batch[-1]}]')
```

**预期观察**：

1. 第 0 个 epoch 的前 4 个 batch 被跳过，`global_step` 从 5 开始（因为 `start_step=4`，`local_step` 从 `skip+1=5` 起）。
2. lr 随 `global_step` 单调平滑下降，从 `5e-4`（global_step=0）一路降到接近 `5e-5`（global_step=30）。
3. 后两个 epoch 的 `skip=0`，正常从头跑完 10 个 batch，`global_step` 继续累加。

这个骨架虽然没有真实的模型与梯度，但它把本讲的 **`setup_seed`、`get_lr`、`SkipBatchSampler`** 三者协作跑通了——这正是 u4-l3 完整训练循环（加上 `init_model`、DDP、混合精度、真实 backward）将要展开的「骨架」。建议你在本实践里改一改 `start_step`、`epochs`、`base_lr`，观察 lr 曲线和 batch 起点的变化，建立直觉后再进入 u4-l2 / u4-l3。

## 6. 本讲小结

- **`get_lr`** 用一行余弦退火公式定义了全程学习率曲线：从传入的 `lr`（第 0 步）平滑降到 `0.1*lr`（最后一步），且 MiniMind 不用 `lr_scheduler`，而是每步手动写回 `optimizer.param_groups`。
- **`init_model`** 统一处理「随机初始化」与「加载已有权重」两种起点，按 `{from_weight}_{hidden_size}{_moe?}.pth` 命名规则找权重，`strict=False` 容错加载，并打印总参数量（MoE 为 `XM-AyM`）与可训练参数量。
- **`SkipBatchSampler`** 通过「丢弃前 N 个 batch」实现续训时的数据跳过，做在索引层避免无谓的数据加载；配合 `iters = len(loader) + skip` 保证 lr 全局坐标系连续。
- **`setup_seed`** 钉死 Python/NumPy/CPU/GPU 四类随机源并关闭 cuDNN benchmark 以保证可复现；多卡时种子加 `rank` 偏移让各卡数据有差异。
- **`Logger` / `is_main_process` / `init_distributed_mode`** 构成「主进程门控日志 + 自动探测 DDP」的基础设施，是后面所有多卡训练脚本去重日志的基石。
- 这些工具都是「透明、手动、可控」的——没有 scheduler 隐藏状态、没有魔法装饰器，符合 MiniMind「大道至简、从 0 原生实现」的一贯风格。

## 7. 下一步学习建议

本讲解的是「公共组件」，但还没讲两个关键问题：**检查点到底存了什么、跨卡续训时 step 怎么自动换算**（`lm_checkpoint`），以及**完整的 DDP + 混合精度 + 梯度累积训练循环长什么样**（`train_epoch`）。建议依次阅读：

1. **u4-l2 检查点保存与断点续训**：精读 `lm_checkpoint`，理解推理权重 `.pth` 与续训检查点 `_resume.pth` 的双重保存、`os.replace` 原子写入、跨 GPU 数量时 step 的自动换算（`step * saved_ws // current_ws`）、wandb run 的恢复。
2. **u4-l3 DDP 分布式、混合精度与梯度累积**：精读 `train_pretrain.py` 的 `train_epoch`，理解 `DistributedDataParallel` 包装、`autocast` + `GradScaler`、`accumulation_steps` 与 `clip_grad_norm`、`torch.compile` 如何协作。
3. 想直接看训练主线的，也可以跳到 **u5-l1 预训练**，在实战中回过头来理解本讲的工具；但 `lm_checkpoint`（u4-l2）是续训的前置，建议至少先扫一遍。

阅读源码时，推荐用 `grep -rn "get_lr\|SkipBatchSampler\|init_model\|setup_seed" trainer/` 观察 8 个 `train_*.py` 是如何统一复用这套工具箱的，体会「公共组件」对降低代码重复的价值。

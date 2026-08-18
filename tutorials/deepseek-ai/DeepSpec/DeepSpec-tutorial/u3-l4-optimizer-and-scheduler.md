# u3-l4 BF16Optimizer 与两段式学习率调度

## 1. 本讲目标

本讲精读 [deepspec/utils/optim.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/optim.py)，这是 DeepSpec 训练侧「参数更新」的全部逻辑所在。学完本讲你应该能够：

1. 解释 `WarmupScheduler` 两阶段（线性 warmup → cosine 衰减）切换的触发条件，以及 `finished` 标志和 `after_scheduler` 的协作方式；
2. 说明 `BF16Optimizer` 如何用一份 float32 主权重（master weights）管理 bf16 模型的参数与梯度，一次 `step()` 内部依次做了哪六件事；
3. 根据 `warmup_ratio` 与 `total_steps` 手工推算任意优化器步的学习率，并说清楚 `lr`、`warmup_ratio`、`weight_decay`、`total_steps` 这四个超参从 config 到优化器的完整传递路径；
4. 解释断点续训时优化器/调度器状态如何经 `training_state.rank{r}.pt` 无缝恢复。

本讲承接 u3-l1（冻结 embed/lm_head、FSDP no_shard 包装）与 u3-l2（`next_micro_step`、`no_sync` 梯度累积、「zero_grad 藏于 step 内部」）已建立的结论，不重复推导。

## 2. 前置知识

- **bf16 与混合精度**：bf16（brain float16）用 8 位指数、7 位尾数，动态范围与 fp32 相同但精度低得多。模型用 bf16 做前向/反向快且省显存，但若直接在 bf16 权重上做 `w -= lr * g` 这样的微小更新，更新量常常小于 bf16 的分辨率而被「四舍五入吞掉」，训练原地踏步。
- **主权重（master weights）模式**：因此常见做法是另外维护一份 fp32 权重，优化器只在 fp32 副本上累积更新（小更新不会被舍入掉），每步再把结果舍入回 bf16 模型。本仓库的 `BF16Optimizer` 就是这个模式的最小实现。注意它**不用** `torch.cuda.amp` 的 autocast/GradScaler——草稿模型本身就以 bf16 dtype 原生运行（u3-l1 的 `precision="bf16"`），bf16 大动态范围使 loss scaling 非必需。
- **AdamW 一句话**：为每个参数维护一阶动量 \(m_t\)、二阶动量 \(v_t\) 的指数滑动平均来自适应缩放步长，并把权重衰减从梯度里解耦出来直接作用于权重。它需要为**每个可训练参数**额外存两份 fp32 状态——这正是 u3-l1 强调冻结 embed/lm_head（约 778M 参数、省约 9GB）的动机之一。
- **PyTorch 学习率调度器**：`LRScheduler` 挂在 optimizer 上，`scheduler.step()` 每调用一次就把内部计数 `last_epoch` 加一并按 `get_lr()` 重写 `optimizer.param_groups[i]["lr"]`；构造函数内部会先**隐式执行一次** `step()`，把 `last_epoch` 置 0。当前 lr 可以从 `param_groups[0]["lr"]` 读出。
- **步数单位约定**（承接 u3-l2）：本仓库 `max_train_steps` 以「优化器步」为单位，1 个优化器步 = `gradient_accumulation_steps` 个微批。调度器每个优化器步推进一次，所以 `total_steps` 传的就是 `max_train_steps`。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [deepspec/utils/optim.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/optim.py) | 本讲主角：`TwoStageScheduler`、`WarmupScheduler`、`CosineAnnealingWarmupLR`、`BF16Optimizer` 四个类，共约 140 行 |
| [deepspec/trainer/base_trainer.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py) | 调用方：`__init__` 里构建 `BF16Optimizer`，`train()` 主循环里裁剪梯度后调用 `optimizer.step()` 并记录 lr |
| [deepspec/trainer/ckpt_manager.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/ckpt_manager.py) | 优化器状态的落盘（`_serialize_training_state`）与恢复（`load_training_state`） |
| [config/dspark/dspark_qwen3_4b.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py) | 超参来源：`lr=6.0e-4`、`warmup_ratio=0.04`、`weight_decay=0.0` |

另外，`BF16Optimizer` 经 [deepspec/utils/__init__.py:18](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/__init__.py#L18) 对外导出；文件第 83 行的注释表明它改编自 SpecForge 的同名类。

## 4. 核心概念与源码讲解

### 4.1 TwoStageScheduler：可保存、可恢复的两段式调度骨架

#### 4.1.1 概念说明

「先 warmup 再衰减」是一种**两段式**调度，但 PyTorch 只提供单段调度器（如 `CosineAnnealingLR`）。`TwoStageScheduler` 解决的问题是：**把一个现成的单段调度器包成外层调度器的第二阶段**，同时保证整个组合的 `state_dict` / `load_state_dict` 语义正确——这是断点续训能否恢复到正确学习率的关键。

它本身不定义任何学习率公式，只提供「委托 + 状态序列化」两件事，具体的两段切换逻辑留给子类 `WarmupScheduler`。

#### 4.1.2 核心流程

- 构造：记下 `after_scheduler`（第二段调度器）与 `finished=False`，再走基类构造（隐式 step 一次）。
- 保存状态：把 `self.__dict__` 中除 `optimizer` 外的字段拷出；若 `after_scheduler` 是调度器，则把**对象**替换成两个纯数据键 `after_scheduler_type` / `after_scheduler_dict`。
- 恢复状态：先用 `after_scheduler_dict` 恢复子调度器，再剔除这两个键后把剩余字段交回基类恢复。

```text
state_dict:   __dict__ 拷贝 ──剔除 optimizer──> 拆出 after_scheduler 为纯数据 ──> 扁平字典
load_state_dict: 扁平字典 ──> after_scheduler.load_state_dict(嵌套字典)
                        ──> 去掉两个 after_* 键 ──> super().load_state_dict(其余字段)
```

#### 4.1.3 源码精读

[deepspec/utils/optim.py:1-3](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/optim.py#L1-L3) 用私有别名导入 torch 的 `CosineAnnealingLR` 与 `LRScheduler` 基类，表明下面全部构建在标准调度器协议之上：

```python
from torch.optim.lr_scheduler import CosineAnnealingLR as _CosineAnnealingLR
from torch.optim.lr_scheduler import LRScheduler as _LRScheduler
```

[deepspec/utils/optim.py:6-10](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/optim.py#L6-L10) 是构造函数：`after_scheduler` 是第二段，`finished` 是「是否已交接」的一次性开关。

[deepspec/utils/optim.py:12-26](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/optim.py#L12-L26) 是状态保存。关键点有二：第一，`key != "optimizer"` 延续了 torch 调度器「状态里不含 optimizer」的惯例；第二，`after_scheduler` 这个**活对象**必须被拆成类型名加纯数据字典（第 16-23 行）。若不拆而直接放进字典，pickle 序列化时会顺着对象属性一路把子调度器引用的 optimizer、乃至参数张量全部拖进状态文件，且恢复时会绕过 `after_scheduler.load_state_dict` 这条受控路径。

[deepspec/utils/optim.py:28-35](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/optim.py#L28-L35) 是恢复：先恢复子调度器（第 29 行），再过滤掉两个 `after_*` 键，把 `finished`、`last_epoch`、`base_lrs` 等字段交回基类用 `__dict__.update` 语义恢复（第 30-35 行）。

#### 4.1.4 代码实践

**实践目标**：亲眼确认 `TwoStageScheduler` 的 `state_dict` 是「扁平 + 嵌套纯数据」结构，且保存/恢复后两个调度器行为完全一致。

**操作步骤**（示例代码，在仓库根目录运行 `python` 即可，仅需安装 torch）：

```python
# 示例代码：验证 TwoStageScheduler 的状态保存/恢复
import torch
from deepspec.utils.optim import CosineAnnealingWarmupLR

p = torch.nn.Parameter(torch.zeros(1))
opt = torch.optim.SGD([p], lr=6.0e-4)
s1 = CosineAnnealingWarmupLR(opt, total_steps=1000, warmup_steps=40)
for _ in range(100):
    s1.step()
state = s1.state_dict()
print("after_scheduler_type =", state["after_scheduler_type"])
print("finished =", state["finished"], "| warmup_epochs =", state["warmup_epochs"],
      "| last_epoch =", state["last_epoch"])

opt2 = torch.optim.SGD([torch.nn.Parameter(torch.zeros(1))], lr=6.0e-4)
s2 = CosineAnnealingWarmupLR(opt2, total_steps=1000, warmup_steps=40)
s2.load_state_dict(state)
s1.step(); s2.step()
print("恢复后 lr 一致:", opt.param_groups[0]["lr"] == opt2.param_groups[0]["lr"])
```

**需要观察的现象**：`state` 里没有 `after_scheduler` 这个键，取而代之的是 `after_scheduler_type` 与 `after_scheduler_dict`；`finished` 已经是 `True`。

**预期结果**：100 步 > 40 步 warmup，故 `after_scheduler_type = "CosineAnnealingLR"`、`finished = True`、`warmup_epochs = 40`；恢复后再各走一步，两边 lr 严格相等。`state` 中除上述键外还包含 torch 基类的簿记字段（如 `base_lrs`、`_last_lr`），完整键集合随 torch 版本略有差异，具体清单待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`TwoStageScheduler.state_dict` 为什么要把 `after_scheduler` 拆成两个键，而不是原样嵌在字典里？

**答案**：拆成 `after_scheduler_type` / `after_scheduler_dict` 后状态是纯数据：序列化时不会顺着活对象把 optimizer 引用和参数张量一起 pickle 进文件；恢复时也强制走 `after_scheduler.load_state_dict` 这条显式路径（先恢复子调度器、再恢复自身），保证两层状态一致地回到保存时刻。

**练习 2**：`finished` 标志会被保存进 checkpoint 吗？为什么这一点重要？

**答案**：会。它在 `self.__dict__` 里，随基类 `load_state_dict` 的 `__dict__.update` 一并恢复。若不恢复，续训时调度器会误以为还在 warmup 阶段，学习率从线性上升段重新开始，破坏训练连续性。

### 4.2 WarmupScheduler 与 CosineAnnealingWarmupLR：两阶段切换的触发与数学

#### 4.2.1 概念说明

warmup（预热）解决的问题是：训练初期权重远离最优，AdamW 二阶动量 \(v_t\) 尚未成形，一开始就用大学习率容易把参数震坏。做法是前 \(W\) 步把 lr 从近似 0 线性拉到峰值 \(\eta_{base}\)，之后交给 cosine 衰减缓慢降回 \(\eta_{min}\)。`WarmupScheduler` 在 `TwoStageScheduler` 骨架上实现这个切换；`CosineAnnealingWarmupLR` 再把它与 `CosineAnnealingLR` 组合成具体可用的类。

#### 4.2.2 核心流程

状态机只有两个状态加一次一次性交接：

```text
阶段 A（finished=False）:
    每次 step() → super().step() → last_epoch += 1 → get_lr()
    get_lr(): last_epoch < W 时返回 (last_epoch+1)/W * η_base
              （注意公式用 t+1：给出的是「下一次更新」要用的 lr）

交接（触发条件: last_epoch >= warmup_epochs，即第 W 次显式 step() 调用）:
    仅执行一次（finished 守卫）:
      1. after_scheduler.base_lrs ← self.base_lrs   # 同步峰值
      2. finished = True
      3. 返回 after_scheduler.get_lr()               # cosine 内部时钟此刻为 0

阶段 B（finished=True）:
    step() 不再推进自身 last_epoch（它被冻结在 W）
    而是转发 after_scheduler.step(None)，并用 after 的 get_last_lr() 更新 _last_lr
```

两段的数学：

\[ \eta_t = \frac{t+1}{W}\,\eta_{base}, \qquad 0 \le t < W \]

\[ \eta_s = \eta_{min} + \frac{\eta_{base} - \eta_{min}}{2}\left(1 + \cos\frac{\pi s}{T}\right), \qquad T = S_{total} - W \]

其中 \(t\) 是外层计数、\(s\) 是 cosine 自己的内部时钟（交接后才开始走：\(s = 0, 1, 2, \dots\)）。曲线在交接处连续：warmup 最后一步恰为 \(W/W \cdot \eta_{base} = \eta_{base}\)，而 cosine 在 \(s=0\) 处也等于 \(\eta_{base}\)。\(T\) 故意取 \(S_{total} - W\)，让 cosine 恰好在最后一个优化器步衰减到 \(\eta_{min}\)——warmup 占用的步数从衰减预算里扣除。

#### 4.2.3 源码精读

[deepspec/utils/optim.py:38-41](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/optim.py#L38-L41) 中 `WarmupScheduler.__init__` 只是记录 `warmup_epochs = int(warmup_epochs)` 后委托给父类；切换逻辑全部在下述两个方法里。

[deepspec/utils/optim.py:43-50](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/optim.py#L43-L50) 是 `get_lr`——两阶段的分岔点：

```python
def get_lr(self):
    if self.last_epoch >= self.warmup_epochs:
        if not self.finished:
            self.after_scheduler.base_lrs = self.base_lrs
            self.finished = True
        return self.after_scheduler.get_lr()

    return [(self.last_epoch + 1) / self.warmup_epochs * lr for lr in self.base_lrs]
```

第 44 行就是**切换触发条件**：`last_epoch` 自增到 `warmup_epochs` 的那次 `step()` 进入此分支。第 46 行把外层 `base_lrs` 同步给 cosine——cosine 构造时从当时 的 param_groups 快照过一份 base_lrs，这次重同步保证交接时峰值一致。第 50 行是 warmup 公式的逐字实现。

[deepspec/utils/optim.py:52-61](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/optim.py#L52-L61) 是 `step` 的重写——**阶段 B 里外层 `last_epoch` 停止推进**（不再调用 `super().step()`），每次把调用转发给 `after_scheduler.step(None)`（第 55 行），并回填 `_last_lr`（第 56 行）。这也是为什么状态保存必须特殊处理子调度器：阶段 B 里真正的进度存在于 cosine 的 `last_epoch` 里，外层计数已经「冻住」。

[deepspec/utils/optim.py:64-79](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/optim.py#L64-L79) 的 `CosineAnnealingWarmupLR` 完成组装：先用 `T_max = total_steps - warmup_steps` 构造 cosine（第 73-78 行），再把自己作为外层包上去（第 79 行）。注意 `warmup_steps=0` 时构造期的隐式 step 就满足 `last_epoch(0) >= 0`，直接进入 cosine 段，不会触发除零。

#### 4.2.4 代码实践

**实践目标**：打印 `total_steps=1000`、`warmup_ratio=0.04`（即 `warmup_steps=40`）时前 100 个优化器步的学习率曲线，找到从 warmup 进入 cosine 衰减的转折点。

**操作步骤**（示例代码）：

```python
# 示例代码：打印 WarmupScheduler 前 100 步学习率
import torch
from deepspec.utils.optim import CosineAnnealingWarmupLR

BASE_LR, TOTAL, WARMUP_RATIO = 6.0e-4, 1000, 0.04
warmup_steps = int(WARMUP_RATIO * TOTAL)      # = 40，与 BF16Optimizer 内部算法一致

p = torch.nn.Parameter(torch.zeros(1))
opt = torch.optim.SGD([p], lr=BASE_LR)
sched = CosineAnnealingWarmupLR(opt, total_steps=TOTAL, warmup_steps=warmup_steps)

lrs = [opt.param_groups[0]["lr"]]              # 构造期的隐式 step 已给出第 1 步的 lr
for _ in range(1, 100):
    sched.step()
    lrs.append(opt.param_groups[0]["lr"])

peak = max(lrs)
print(f"warmup_steps={warmup_steps}, 峰值={peak:.3e}, "
      f"首达峰值的更新序号={lrs.index(peak) + 1}")
for k in (1, 2, 20, 39, 40, 41, 42, 50, 100):
    print(f"update {k:3d}: lr={lrs[k-1]:.6e}  ratio={lrs[k-1]/BASE_LR:.5f}")
# 可选：matplotlib 画曲线
# import matplotlib.pyplot as plt; plt.plot(range(1, 101), lrs); plt.axvline(41); plt.show()
```

**需要观察的现象**：前 40 个更新 lr 按 \(k/40\) 线性上升；第 40 与 41 个更新同为峰值 \(6\times10^{-4}\)；从第 42 个更新起进入 cosine 段、开始极缓慢地下降。

**预期结果**（按公式推导，精确小数待本地验证，不同 torch 版本的 CosineAnnealingLR 实现可能带来末位差异）：

| 更新序号 \(k\) | 阶段 | lr |
|---|---|---|
| 1 | warmup | \(1/40 \times 6\text{e-}4 = 1.5\times10^{-5}\) |
| 20 | warmup | \(3.0\times10^{-4}\) |
| 39 | warmup | \(5.85\times10^{-4}\) |
| 40 | warmup 最后一步 | \(6.0\times10^{-4}\)（峰值） |
| 41 | cosine \(s=0\) | \(6.0\times10^{-4}\)（交接处连续） |
| 42 | cosine \(s=1\) | \(\approx 5.99998\times10^{-4}\) |
| 50 | cosine \(s=9\) | \(\approx 5.9987\times10^{-4}\) |
| 100 | cosine \(s=59\) | \(\approx 5.944\times10^{-4}\) |

**转折点是第 40 次调度步（峰值处），而不是肉眼可见的弯折**：\(T=960\) 很长，cosine 起始段 \(\cos(\pi s/960)\) 几乎是 1，前 100 步只下降约 0.9%；到第 1000 个更新（\(s=960\)）lr 才降到 \(\eta_{min}=0\)。若把曲线画出来，建议同时画出全程序列才能看到完整的衰减形状。

#### 4.2.5 小练习与答案

**练习 1**：`total_steps=1000`、`warmup_ratio=0.04`、`lr=6e-4` 时，第 25 次优化器更新使用的学习率是多少？

**答案**：\(W=\mathrm{int}(0.04\times1000)=40\)，第 25 次更新仍在 warmup 段：\(\eta = 25/40 \times 6\times10^{-4} = 3.75\times10^{-4}\)。

**练习 2**：`CosineAnnealingWarmupLR` 构造 cosine 时为什么传 `total_steps - warmup_steps` 而不是 `total_steps`？

**答案**：cosine 的内部时钟在交接后才从 0 开始走，从交接到训练结束共剩 \(S_{total}-W\) 次更新；取 \(T_{max}=S_{total}-W\) 恰好让最后一次更新的 lr 等于 \(\eta_{min}\)。若传 \(S_{total}\)，训练中止时 cosine 只走了不到全程，lr 还停留在中段，相当于白白放弃了衰减尾巴。

**练习 3**：两阶段切换具体发生在哪一次调用？由哪个条件触发？

**答案**：发生在第 \(W\) 次显式 `scheduler.step()` 调用内部——这次调用使 `last_epoch` 自增到 `warmup_epochs`，`get_lr` 里 `self.last_epoch >= self.warmup_epochs` 首次为真，随即置 `finished=True` 并返回 cosine 在 \(s=0\) 处的 lr。

### 4.3 BF16Optimizer：fp32 主权重 + AdamW + 调度器的组合封装

#### 4.3.1 概念说明

`BF16Optimizer` 把前两讲的调度器与「主权重混合精度」打包成一个极简的优化器门面：对外只暴露 `step()` / `state_dict()` / `load_state_dict()` / `get_learning_rate()` 四个方法，内部持有三样东西——bf16 模型的可训练参数引用、它们的 fp32 主权重副本、以及作用在主权重上的 AdamW + `CosineAnnealingWarmupLR`。它不是 `torch.optim.Optimizer` 的子类，而是一个普通组合对象；配合 u3-l1 所述「FSDP 以 `use_orig_params=True` + 默认 no_shard 包装」，直接建在裸 `draft_model` 上也能拿到正确的梯度。

#### 4.3.2 核心流程

构建（对应 `__init__`）：

```text
model(裸 draft_model, bf16)
  ├─ model_params  = [p for p in parameters() if p.requires_grad]   # 冻结参数出局
  ├─ fp32_params   = model_params 的 detach().clone().to(float32)    # 主权重
  ├─ optimizer     = AdamW(fp32_params, lr, weight_decay)
  └─ scheduler     = CosineAnnealingWarmupLR(optimizer,
                       total_steps, warmup_steps=int(warmup_ratio*total_steps))
```

一次 `step()`（训练主循环里每个优化器步调用一次）：

```text
1. bf16 梯度 → fp32：master.grad = model_param.grad.detach().to(float32)
2. AdamW 在 fp32 主权重上更新
3. optimizer.zero_grad()            # 清主权重梯度
4. scheduler.step()                 # 推进 lr（下一次更新生效）
5. master(fp32) → model(bf16) copy_ # 舍入回模型
6. model_param.grad = None          # 清模型梯度，为下一轮累积清零
```

注意第 1 步之前，主循环已先执行 `FSDP.clip_grad_norm_`（见 4.3.3），裁剪是对 bf16 梯度的就地缩放，因此拷贝到 fp32 的梯度自然带上了裁剪效果。第 6 步正是 u3-l2 说的「zero_grad 藏于 step 内部」——梯度累积的起点永远是干净的。

#### 4.3.3 源码精读

**超参从哪来**：[config/dspark/dspark_qwen3_4b.py:32-45](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L32-L45) 的 `train` 字典给出 `lr=6.0e-4`、`warmup_ratio=0.04`、`weight_decay=0.0`；[deepspec/trainer/base_trainer.py:214-220](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L214-L220) 把它们连同 `_compute_training_schedule` 算出的 `max_train_steps` 一起传入（第一个参数是**裸** `self.draft_model`，而非 FSDP 包装后的 `self.model`）：

```python
self.optimizer = BF16Optimizer(
    self.draft_model,
    lr=float(self.args.train.lr),
    total_steps=self.max_train_steps,
    warmup_ratio=float(self.args.train.warmup_ratio),
    weight_decay=float(self.args.train.weight_decay),
)
```

**构建**：[deepspec/utils/optim.py:82-106](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/optim.py#L82-L106)。第 93 行只收集 `requires_grad=True` 的参数——被冻结的 embed/lm_head 不产生 fp32 副本也不进 AdamW；第 94-98 行克隆出主权重并重新打开梯度；第 102-106 行组装调度器，`warmup_steps = int(warmup_ratio * total_steps)` 就是 4.2 里那道算术。

**step**：[deepspec/utils/optim.py:108-122](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/optim.py#L108-L122)。第 110-115 行把（已归约、已裁剪的）bf16 梯度逐参数转成 fp32 挂到主权重上；第 116-118 行依次 `AdamW.step()`、清主权重梯度、推进调度器；第 119-122 行把更新后的主权重舍入回 bf16 模型参数并把模型梯度置 `None`。微小的单步更新量在 fp32 主权重里逐 Step 累积，不会被 bf16 的舍入逐步吞掉——这就是整个类存在的意义。

**保存与恢复**：[deepspec/utils/optim.py:124-129](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/optim.py#L124-L129) 的 `state_dict` 打包三样东西（fp32 主权重搬到 CPU 以便落盘）；[deepspec/utils/optim.py:131-139](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/optim.py#L131-L139) 的 `load_state_dict` 依次恢复 AdamW 状态（\(m_t\)、\(v_t\)）、调度器状态（含 `finished` 与 cosine 内部时钟）与主权重，最后把主权重同步回 bf16 模型。

**在主循环中的调用**：[deepspec/trainer/base_trainer.py:386-398](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L386-L398)——先用 `FSDP.clip_grad_norm_` 求**全局**范数并就地裁剪（u3-l2），再 `self.optimizer.step()`，随后 [deepspec/utils/optim.py:141-142](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/optim.py#L141-L142) 的 `get_learning_rate()`（读 AdamW `param_groups[0]["lr"]`）把当前 lr 交给 `training_logger` 记录（u3-l6 展开）。

**落盘与续训链路**：[deepspec/trainer/ckpt_manager.py:209-219](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/ckpt_manager.py#L209-L219) 把 `optimizer.state_dict()`（即上述三件套）连同 `next_micro_step`、各 RNG 状态写进按 rank 命名的 `training_state.rank{r}.pt`（[deepspec/trainer/ckpt_manager.py:188-192](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/ckpt_manager.py#L188-L192)）；恢复时 [deepspec/trainer/ckpt_manager.py:84-98](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/ckpt_manager.py#L84-L98) 的 `load_training_state` 调 `optimizer.load_state_dict(checkpoint["optimizer"])`，再由 [deepspec/trainer/base_trainer.py:221-231](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L221-L231) 取回 `next_micro_step`。由于保存只发生在同步微批末尾（第 101-103 行断言 `next_micro_step` 与梯度累积对齐），调度器位置与优化器步严格同步，lr 从断点继续而非重新 warmup。（一个工程观察：no_shard 下每个 rank 都存一份完整 fp32 主权重，冗余但正确、简单。）

#### 4.3.4 代码实践

**实践目标**：用一个玩具模型验证 `BF16Optimizer` 的三件事——冻结参数被排除、fp32 主权重与 bf16 模型权重的同步、`step()` 结束时模型梯度被清空。

**操作步骤**（示例代码，CPU 即可运行）：

```python
# 示例代码：观察 fp32 主权重与 bf16 模型权重的同步
import torch
from deepspec.utils import BF16Optimizer

torch.manual_seed(0)
model = torch.nn.Linear(8, 8).to(torch.bfloat16)          # 可训练
frozen = torch.nn.Linear(8, 8).to(torch.bfloat16)         # 模拟被冻结的 embed/lm_head
frozen.weight.requires_grad_(False); frozen.bias.requires_grad_(False)

opt = BF16Optimizer(model, lr=6.0e-4, total_steps=1000,
                    warmup_ratio=0.04, weight_decay=0.0)
print("可训练/收集到/主权重参数个数:",
      len(list(model.parameters())) + len(list(frozen.parameters())),
      len(opt.model_params), len(opt.fp32_params))
print("模型 dtype:", next(model.parameters()).dtype,
      "| 主权重 dtype:", opt.fp32_params[0].dtype)

x = torch.randn(4, 8, dtype=torch.bfloat16)
for step in range(1, 4):
    model(x).pow(2).mean().backward()
    opt.step()
    print(step, f"lr={opt.get_learning_rate():.4e}",
          "模型梯度已清空 =", model.weight.grad is None)
```

**需要观察的现象**：两个 Linear 共 4 个参数张量，但只有 2 个进入优化器；模型参数是 bfloat16、主权重是 float32；每次 `opt.step()` 之后 `model.weight.grad is None` 为真。

**预期结果**：打印 `4 2 2`、`bfloat16 float32`；由于 `scheduler.step()` 在 `opt.step()` 内部、于 `AdamW.step()` **之后**执行，第 \(k\) 次 `opt.step()` 结束时打印的 lr 是给第 \(k+1\) 次更新准备的，即 \((k+1)/40 \times 6\text{e-}4\)：三次分别约为 `3.0e-05`、`4.5e-05`、`6.0e-05`（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`BF16Optimizer` 为什么只收集 `requires_grad=True` 的参数？联系 u3-l1 的冻结设计。

**答案**：草稿模型的 embed/lm_head 从目标模型拷贝后即冻结（u3-l1），若也被收集，每个副本要多存一份 fp32 主权重外加 AdamW 的 \(m_t\)、\(v_t\) 两份 fp32 状态——对 Qwen3-4B 约 778M 参数即约 9GB 纯浪费；且这些参数根本不产生梯度，`step()` 里对应 `grad is None` 也会被跳过。

**练习 2**：[deepspec/utils/optim.py:117](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/optim.py#L117) 的 `self.optimizer.zero_grad()` 与 [deepspec/utils/optim.py:122](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/optim.py#L122) 的 `model_param.grad = None` 各清谁的梯度？为什么两处都需要？

**答案**：前者清 fp32 主权重上的 AdamW 梯度，后者清 bf16 模型参数上的梯度。下一轮梯度累积（u3-l2 的 `no_sync` 微批循环）从模型参数起步，若不清，旧梯度会叠加进新循环；而主权重梯度每步都由第 1 步重新赋值，清掉只是防御性整洁。只清其一，另一路就会残留旧值。

**练习 3**：断点续训时，是哪些状态保证学习率从断点继续而不是重新 warmup？

**答案**：`BF16Optimizer.state_dict` 里的 `scheduler_state_dict`——经 `TwoStageScheduler` 展开后包含外层 `last_epoch`、`finished`、`warmup_epochs` 以及 cosine 自己的 `after_scheduler_dict`（内部时钟与 base_lrs）。它们随 `training_state.rank{r}.pt` 落盘，恢复时先于训练循环执行 `optimizer.load_state_dict`，lr 因此精确回到保存时刻。

## 5. 综合实践

把本讲三块内容串成一次「训练 → 中断 → 续训」的迷你实验：用玩具 bf16 模型跑 60 步，`warmup_ratio=0.5`（`warmup_steps=30`，让交接点落在中断点上），第 30 步存盘，然后用全新的模型和优化器恢复，验证 lr 与最终权重都与「一口气跑完」完全一致。

```python
# 示例代码：断点续训下的学习率与权重连续性
import torch
from deepspec.utils import BF16Optimizer

def make():
    torch.manual_seed(0)
    m = torch.nn.Linear(8, 8).to(torch.bfloat16)
    return m, BF16Optimizer(m, lr=6.0e-4, total_steps=60, warmup_ratio=0.5)

def run(m, opt, start, end, x):
    for _ in range(start, end):
        m(x).pow(2).mean().backward()
        opt.step()

x = torch.randn(4, 8, dtype=torch.bfloat16)
m1, o1 = make()
run(m1, o1, 1, 31, x)                       # 跑满 30 步（恰含交接）后"存盘"
torch.save({"opt": o1.state_dict(), "w": m1.state_dict()}, "toy_resume.pt")

m2, o2 = make()                              # 全新模型 + 全新优化器
ckpt = torch.load("toy_resume.pt", weights_only=False)
m2.load_state_dict(ckpt["w"])
o2.load_state_dict(ckpt["opt"])              # 恢复 AdamW 动量 + 调度器 + fp32 主权重
run(m2, o2, 31, 61, x)

m3, o3 = make()
run(m3, o3, 1, 61, x)                        # 基准：不中断一口气跑完
print("续训 lr 与基准一致:", o2.get_learning_rate() == o3.get_learning_rate())
print("最终权重最大差:",
      (m2.weight.float() - m3.weight.float()).abs().max().item())
```

预期两条输出分别为 `True` 和 `0.0`（同一份数据与固定种子下更新是确定性的；跨硬件或 torch 版本的浮点差异待本地验证）。值得检查的细节：第 30 步的 `opt.step()` 内部发生了 4.2 的交接（`finished` 变 `True`），所以恢复路径同时覆盖了「主权重恢复」与「阶段 B 的调度器状态恢复」两个难点。若把 `o2.load_state_dict(ckpt["opt"])` 这行注释掉再跑，比较两条 lr 曲线，能直观看到重新 warmup 造成的断裂。

## 6. 本讲小结

- `TwoStageScheduler` 提供「委托 + 状态序列化」骨架：`state_dict` 把 `after_scheduler` 拆成 `after_scheduler_type` / `after_scheduler_dict` 两个纯数据键，`load_state_dict` 先恢复子调度器再恢复自身，这是续训时 lr 连续的基础。
- `WarmupScheduler` 的切换触发条件是 `last_epoch >= warmup_epochs`（第 \(W\) 次显式 `step()` 调用），由 `finished` 保证只交接一次；交接时同步 `base_lrs`，之后外层计数冻结、调用全部转发给 cosine——真正的进度存在 cosine 的内部时钟里。
- 学习率公式：warmup 段 \(\eta_t = \frac{t+1}{W}\eta_{base}\)，cosine 段 \(\eta_s = \eta_{min} + \frac{\eta_{base}-\eta_{min}}{2}(1+\cos\frac{\pi s}{T})\)，\(T = S_{total}-W\) 保证恰好衰减到底；转折点在数值峰值处（本讲例子第 40 步），曲线在交接处连续。
- `BF16Optimizer` 组合了 fp32 主权重、AdamW 与 `CosineAnnealingWarmupLR`；一次 `step()` 依次完成「bf16 梯度→fp32、AdamW 更新、清主权重梯度、推进调度器、主权重→bf16、清模型梯度」六件事。
- 超参传递链：config 的 `train.lr/warmup_ratio/weight_decay` + `_compute_training_schedule` 的 `max_train_steps` → [deepspec/trainer/base_trainer.py:214-220](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L214-L220) → `BF16Optimizer`，`warmup_steps = int(warmup_ratio * total_steps)` 在优化器内部换算。
- 断点链路：`BF16Optimizer.state_dict`（AdamW 动量 + 调度器 + fp32 主权重）→ `training_state.rank{r}.pt` → `load_training_state` → `load_state_dict`，配合只在同步微批末尾存盘的对齐断言，lr 与权重从断点无缝继续。

## 7. 下一步学习建议

本讲补齐了「参数如何更新」这最后一块训练骨架。接下来建议：

1. 学习 **u3-l5（检查点管理）**，本讲出现的 `training_state.rank{r}.pt` 只是其拼图之一——模型权重的 gathered/sharded 两种形态、`step_latest` 原子符号链接与 `train_config.py` 回写都在那一讲展开；
2. 学习 **u3-l6（指标聚合与训练日志）**，看 `training_logger.on_optimizer_step` 如何把本讲 `get_learning_rate()` 返回的 lr、`grad_norm` 与进度/ETA 一起写进 TensorBoard；
3. 想动手的读者可以回到 [deepspec/trainer/base_trainer.py:355-407](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L355-L407)，把 `train()` 主循环、`no_sync`（u3-l2）与本讲的 `optimizer.step()` 三者连起来读一遍，完整走通「一个优化器步」的生命周期。

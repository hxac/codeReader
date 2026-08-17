# 训练主循环：梯度累积、no_sync 与挂起保存

## 1. 本讲目标

上一讲（u3-l1）我们拆完了 `BaseTrainer.__init__` 的装配流水线：模型构建、FSDP 包装、训练日程计算、优化器创建。本讲进入真正"跑起来"的部分——[deepspec/trainer/base_trainer.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py) 中的 `train()` 方法。

学完本讲你应该能：

1. 逐行讲出 `train()` 主循环里每个变量在一次迭代中如何变化（`next_micro_step`、`global_step`、`should_sync`）。
2. 解释 `no_sync` 上下文在梯度累积中的作用：它省掉了什么通信、为什么必须与"最后一次 backward 同步"配对使用。
3. 说明 `next_micro_step` 为何是训练进度的唯一真相源（single source of truth），`global_step`、epoch、数据偏移为什么都是它的派生量。
4. 描述 `FSDP.clip_grad_norm_` 与 `optimizer.step` 的配合，以及为什么主循环里看不到显式的 `zero_grad` 调用。
5. 讲清 `SuspendController` 的挂起-保存-退出路径，以及它在公开环境下为什么是"无害空转"的。

## 2. 前置知识

本讲假设你已读过 u3-l1（BaseTrainer 初始化）与 u2-l6（CUDAPrefetcher）。再用三段话补齐需要的概念。

**梯度累积（gradient accumulation）。** 深度学习训练的理想更新公式是对一整个全局批（global batch）的损失求平均再走一步优化器。但全局批往往大到单卡放不下（DeepSpec 默认 `global_batch_size=512`，而 `local_batch_size=1`），于是把一个大批切成若干"微批"（micro batch），逐个前向反向、把梯度累加在 `param.grad` 里，凑齐后再做一次优化器步进。设全局批为 \( B_{\text{global}} \)，卡数为 \( W \)，每卡微批大小为 \( b \)，则梯度累积步数：

\[ G = \frac{B_{\text{global}}}{W \times b} \]

DeepSpec 默认配置下 \( W=8 \)、\( b=1 \)、\( B_{\text{global}}=512 \)，所以 \( G=64 \)：每张卡连做 64 个微批的 backward，才触发一次优化器 step。

**FSDP 与 no_sync。** FSDP（Fully ShardedDataParallel）在每层 backward 结束时会把该层梯度做一次跨卡归约（本仓库默认 `no_shard` 策略下就是一次 all-reduce，语义与 DDP 的跨卡平均一致）。如果梯度累积期间每个微批都归约一次，\( G=64 \ 时每步优化器要做 64 次全卡通信，几乎全是浪费——因为中间结果根本不用。`model.no_sync()` 是 FSDP 提供的上下文管理器：进入后 FSDP 关闭 backward 末尾的梯度归约，梯度只在本卡累加；最后一批退出该上下文再 backward，FSDP 会把**累计起来的整份 `.grad`** 一次性归约。

**为什么 loss 要除以 G。** 梯度是逐微批相加的，若直接加总 \( G \) 个微批的梯度，量级会是"单批平均梯度"的 \( G \) 倍。所以代码把每个微批的 loss 除以 \( G \)：

\[ g_{\text{accum}} = \sum_{i=1}^{G} \nabla\!\left(\frac{\mathcal{L}_i}{G}\right) = \frac{1}{G}\sum_{i=1}^{G}\nabla \mathcal{L}_i \]

这样累加结果就等价于"把 \( G \) 个微批拼成一个大批后求平均梯度"，学习率语义不变。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [deepspec/trainer/base_trainer.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py) | 本讲主角：`train()` 主循环（L355-407）、`global_step` 派生属性、存盘与挂起辅助方法 |
| [deepspec/utils/hfai_suspend.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/hfai_suspend.py) | `SuspendController`：监听集群抢占信号、跨 rank 广播挂起决定、执行挂起 |
| [deepspec/utils/optim.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/optim.py) | `BF16Optimizer.step`：解释主循环为何没有 `zero_grad` |
| [deepspec/data/cuda_prefetcher.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/cuda_prefetcher.py) | 主循环的迭代对象：`for batch in prefetcher` 的供给端（u2-l6 已精读） |
| [deepspec/utils/training_logger.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/training_logger.py) | `on_optimizer_step` 钩子：日志与 TensorBoard 的写入时机 |
| [deepspec/trainer/dspark_trainer.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/dspark_trainer.py) | `run_batch` 的子类实现：主循环里那一行 `self.run_batch(batch)` 背后是什么 |
| [deepspec/trainer/ckpt_manager.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/ckpt_manager.py) | `save_checkpoint` 入口与 `TrainingResumeState`（"唯一真相源"注释的出处），u3-l5 深入 |

## 4. 核心概念与源码讲解

### 4.1 train() 主循环

#### 4.1.1 概念说明

`train()` 是模板方法模式中的"固定骨架"：它不关心前向怎么算（交给子类的 `run_batch`）、不关心 checkpoint 怎么落盘（交给 ckpt_manager），只负责编排**进度计算 → 数据供给 → 梯度累积 → 优化器步进 → 日志/存盘/挂起**这条时间线。整个方法不到 50 行，却串起了上一讲初始化出的所有组件。

#### 4.1.2 核心流程

```text
train()
├── model.train()
├── 若 global_step >= max_train_steps：直接返回（上次已训练完）
├── 由 next_micro_step 推出剩余微步数、剩余样本数
├── 构造 DataLoader（采样起点 = next_micro_step × local_batch_size）
├── 用 CUDAPrefetcher 包一层；启动日志会话
└── with suspend_controller.monitoring():
    └── for batch in prefetcher:            # 每个 batch 已在 GPU 上
        ├── should_sync = (next_micro_step + 1) % G == 0
        ├── with (no_sync 或 nullcontext):
        │   ├── loss = run_batch(batch) / G
        │   └── loss.backward()
        ├── next_micro_step += 1
        ├── 若非同步步：continue
        ├── FSDP.clip_grad_norm_(model, max_grad_norm)
        ├── optimizer.step()
        ├── training_logger.on_optimizer_step(...)
        ├── 若 global_step % checkpointing_steps == 0：save_and_eval_checkpoint()
        └── 若 suspend_controller.requested()：_save_and_suspend() 并 return
└── 循环自然结束：最后一次 save_and_eval_checkpoint()
```

#### 4.1.3 源码精读

先看进入循环前的进度计算：

```python
local_batch_size = int(self.args.train.local_batch_size)
total_micro_steps = self.max_train_steps * self.gradient_accumulation_steps
remaining_micro_steps = total_micro_steps - self.next_micro_step
remaining_samples = remaining_micro_steps * local_batch_size

dataloader = self._build_train_dataloader(
    start_offset_samples=self.next_micro_step * local_batch_size,
    num_samples=remaining_samples,
)
```

[deepspec/trainer/base_trainer.py:360-368](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L360-L368)：这段是"next_micro_step 是唯一真相源"的第一处体现——总微步数、剩余微步数、剩余样本数、数据采样起点，全部从一个整数 `next_micro_step` 推出。断点续训时只需恢复这一个数，采样器就能从全数据流的全局偏移处继续，不多读也不漏读一个样本（采样器细节在 u3-l3）。

注意前面还有一个早退分支：

```python
if self.global_step >= self.max_train_steps:
    return
```

[deepspec/trainer/base_trainer.py:357-358](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L357-L358)：如果恢复的进度已到达终点，`train()` 什么都不做——因为上次运行的收尾 checkpoint 已经存在，无需重复保存。这对应"任务被重启但训练早已完成"的边界情况。

循环本体：

```python
with self.suspend_controller.monitoring():
    for batch in prefetcher:
        should_sync = (
            (self.next_micro_step + 1) % self.gradient_accumulation_steps == 0
        )
        sync_context = nullcontext() if should_sync else self.model.no_sync()
        with sync_context:
            loss = self.run_batch(batch) / self.gradient_accumulation_steps
            loss.backward()
        self.next_micro_step += 1
```

[deepspec/trainer/base_trainer.py:372-381](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L372-L381)：逐行拆解——

- `for batch in prefetcher`：迭代 [CUDAPrefetcher](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/cuda_prefetcher.py#L46-L70)，拿到的 batch 已在 GPU 上（`__next__` 里 `wait_stream` 保证侧流 H2D 拷贝完成，`record_stream` 防止显存被提前回收，同时后台线程已经开始取下下个批，u2-l6 已精读）。
- `should_sync` 在 `next_micro_step` 自增**之前**用 `(next_micro_step + 1) % G == 0` 判断：意思是"做完这个微批后，累计微批数是否恰好凑满 G 个"。凑满 → 这一步要做真正的梯度同步与优化器步进。
- `sync_context`：同步步用 `nullcontext()`（什么都不包），累积步用 `self.model.no_sync()`（关掉 FSDP 的 backward 梯度归约）。注意 `no_sync()` 在三目表达式里被**预先构造**、随后才被 `with` 进入——每次进入/退出严格配对一个微批的 forward+backward。
- `loss = self.run_batch(batch) / G`：`run_batch` 由子类实现，例如 DSpark 的版本在 [deepspec/trainer/dspark_trainer.py:24-39](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/dspark_trainer.py#L24-L39) 里调用 `self.model(...)`（注意是 FSDP 包装后的 `self.model`）拿到输出、算出组合损失；除以 \( G \) 的原因见 2 节的公式。损失内部还会顺手 `add_metric` 注册 `train/loss` 等指标（见 [deepspec/modeling/dspark/loss.py:314-319](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/loss.py#L314-L319)）。
- `loss.backward()`：反向传播。累积步时梯度只落在本卡 `.grad` 上累加；同步步时（不在 no_sync 里）backward 末尾 FSDP 会把**累计的整份梯度**做跨卡归约。

`next_micro_step` 与 `global_step` 的派生关系定义在别处：

```python
@property
def global_step(self):
    return self.next_micro_step // self.gradient_accumulation_steps
```

[deepspec/trainer/base_trainer.py:236-238](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L236-L238)：`global_step` 是**属性**不是存储字段，任何时刻都等于 `next_micro_step // G`。epoch 同理也是派生量——[deepspec/utils/training_logger.py:84](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/training_logger.py#L84) 里 `current_epoch = next_micro_step // micro_batches_per_epoch + 1`。这套设计在 ckpt_manager 里有官方注释背书：

```python
# next_micro_step is the single source of truth for training progress;
# global_step and current_epoch are derived from it together with
# gradient_accumulation_steps / micro_batches_per_epoch.
next_micro_step: int
```

[deepspec/trainer/ckpt_manager.py:56-62](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/ckpt_manager.py#L56-L62)：`TrainingResumeState` 这个 dataclass 里**只有** `next_micro_step` 一个字段——恢复训练进度只需要恢复它。初始化时它被置 0（[base_trainer.py:167](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L167)），断点续训时被 `load_training_state` 的返回值覆盖（[base_trainer.py:231](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L231)）。

单一真相源的好处：`global_step`、epoch、数据偏移、ETA 全部由同一整数推出，永不出现"step 计数与数据位置对不上"的续训错位 bug。

#### 4.1.4 代码实践

**实践目标**：不看源码也能默写出主循环三个关键变量随迭代的变化规律。

**操作步骤**（纯 Python 模拟，无需 GPU）：

```python
# 示例代码：模拟 train() 主循环的进度变量变化
G = 4                # gradient_accumulation_steps
max_train_steps = 3  # 优化器步数上限
total_micro_steps = max_train_steps * G

next_micro_step = 0
print(f"{'iter':>4} {'next(before)':>12} {'should_sync':>11} "
      f"{'next(after)':>11} {'global_step':>11}")
for i in range(1, total_micro_steps + 1):
    before = next_micro_step
    should_sync = (next_micro_step + 1) % G == 0
    # 此处应有 run_batch / loss.backward()
    next_micro_step += 1
    global_step = next_micro_step // G   # 模拟 @property
    print(f"{i:>4} {before:>12} {str(should_sync):>11} "
          f"{next_micro_step:>11} {global_step:>11}")
```

**需要观察的现象**：`should_sync` 每 4 次迭代出现一次 `True`；`global_step` 只在 `should_sync=True` 的那一行自增。

**预期结果**（节选）：

```text
iter  next(before)  should_sync  next(after)  global_step
   1             0        False            1            0
   2             1        False            2            0
   3             2        False            3            0
   4             3         True            4            1
   5             4        False            5            1
   ...
   8             7         True            8            2
   ...
  12            11         True           12            3
```

（本脚本不依赖仓库环境，可本地直接运行验证；若与上表不符请先检查 G 是否被改。）

#### 4.1.5 小练习与答案

**练习 1**：若把 `should_sync` 的判断写成 `self.next_micro_step % G == 0`（去掉 `+1`），会发生什么？

**答案**：判断时机提前了一个微批——微批序号为 0 的那次迭代（每轮累积的第一个微批）就会被误判为"同步步"，而真正凑满 G 个微批的那次反而变成累积步。结果是每个优化器步只用 1 个微批的梯度，其余微批的梯度被错误地跨步累积，全局批语义完全被破坏。`+1` 的含义是"以做完本微批后的计数为准"。

**练习 2**：为什么 `train()` 开头要检查 `self.global_step >= self.max_train_steps` 并直接返回，而不是让循环自然结束？

**答案**：这是为断点续训兜底：调度系统重启一个已训练完毕的任务时，恢复出的 `next_micro_step` 已等于 `total_micro_steps`，构造出的"剩余样本数"为 0，循环一次都不进。显式早退避免了空转，也明确表达"不再重复收尾保存"——最终 checkpoint 上一次运行已写过。

### 4.2 梯度累积与 no_sync

#### 4.2.1 概念说明

`no_sync` 解决的矛盾是：梯度累积要求"多个微批的梯度在本卡累加"，而 FSDP 默认行为是"每个 backward 结束就把梯度跨卡归约"。如果不干预，\( G=64 \) 个微批会触发 64 次全卡 all-reduce，其中前 63 次的结果都会被下一次累加覆盖掉一部分语义（且通信时间白白叠加在关键路径上）。`no_sync()` 让前 63 个微批完全静默，只在最后一个微批做一次归约，通信量降为原来的 \( 1/G \)。

#### 4.2.2 核心流程

一个完整优化器步（\( G=4 \) 为例）的时间线：

```text
微批 1: with no_sync:  forward → backward   # 梯度累加到 .grad，无通信
微批 2: with no_sync:  forward → backward   # .grad 继续累加，无通信
微批 3: with no_sync:  forward → backward   # 同上
微批 4: with nullcontext: forward → backward  # backward 末尾 FSDP 归约累计梯度
        clip_grad_norm_ → optimizer.step()  → 记日志/存盘/查挂起
```

关键约束：**最后一个微批必须不在 no_sync 里**，否则累计梯度永远不会被归约，各卡会带着各自局部的梯度各自更新，权重立刻失同步。代码里这正是 `should_sync` 与 `sync_context` 二者联动保证的。

#### 4.2.3 源码精读

```python
should_sync = (
    (self.next_micro_step + 1) % self.gradient_accumulation_steps == 0
)
sync_context = nullcontext() if should_sync else self.model.no_sync()
with sync_context:
    loss = self.run_batch(batch) / self.gradient_accumulation_steps
    loss.backward()
self.next_micro_step += 1

if not should_sync:
    continue
```

[deepspec/trainer/base_trainer.py:374-384](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L374-L384)：三个细节值得圈出来——

1. **除法在 backward 之前**。`run_batch(batch) / G` 让每个微批贡献 \( \nabla\mathcal{L}_i / G \)，累加后恰好是大批平均梯度（见 2 节公式）。若忘了除，等于学习率被隐式放大 \( G \) 倍。
2. **no_sync 必须罩住 forward + backward 全程**。`with sync_context:` 同时包住了 `run_batch`（内部调 `self.model(...)` 即 forward）和 `loss.backward()`，这正是 FSDP 对 no_sync 上下文的使用要求。
3. **`continue` 挡住后续步骤**。累积微批做完 backward 就立刻进入下一轮迭代；裁剪、step、日志、存盘、挂起检查全部只属于同步微批。

还有一个容易忽略的正确性前提：`should_sync` 只依赖 `next_micro_step`，而 `next_micro_step` 在所有 rank 上从同一恢复值出发、每次迭代同步 +1——因此**每个 rank 在完全相同的迭代序号上进入同步分支**。这一点是后续 `requested()` 里集合通信能对齐的基础（各 rank 的 DataLoader 迭代次数一致性由采样器保证，见 u3-l3）。

为什么 `self.model` 上能调 `no_sync()`？因为它在 `__init__` 里已被 FSDP 包装（[base_trainer.py:188](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L188)，`self.model = self._wrap_with_fsdp(self.model)`），`no_sync` 是 FSDP 包装类的方法；即便本仓库默认 `sharding_strategy="no_shard"`（不做参数分片，见 [config/dspark/dspark_qwen3_4b.py:43](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L43)），FSDP 依然负责 backward 中的梯度归约，no_sync 依然有效。

#### 4.2.4 代码实践

**实践目标**：量化感受 no_sync 省下的通信次数，并验证"梯度累加 ≡ 大批平均梯度"。

**操作步骤**：

1. 用下面的玩具脚本（示例代码，单进程即可，无需真实 FSDP）对比"逐批归约"与"攒齐再归约"的归约次数：

```python
# 示例代码：no_sync 的通信次数与梯度等价性演示（单进程模拟）
import torch

G, D = 4, 3
model = torch.nn.Linear(D, 1)
batches = [torch.randn(2, D) for _ in range(G)]
target = torch.randn(G, 2, 1)

# 方式 A：模拟 no_sync——不做任何中间归约，梯度直接累加
for i in range(G):
    loss = ((model(batches[i]) - target[i]) ** 2).mean() / G
    loss.backward()
accum_grad = model.weight.grad.clone()

# 方式 B：把 G 个微批拼成一个大批，一次前向反向
big_x = torch.cat(batches)
big_y = torch.cat(target)
model.weight.grad = None
big_loss = ((model(big_x) - big_y) ** 2).mean()
big_loss.backward()
big_grad = model.weight.grad.clone()

print("max |diff| =", (accum_grad - big_grad).abs().max().item())
```

2. 阅读源码并数一数：在 [base_trainer.py:372-405](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L372-L405) 的循环里，一次优化器步会经过几次 `no_sync` 上下文、几次 `nullcontext`。

**需要观察的现象**：方式 A 与方式 B 的梯度最大差异应在浮点误差量级（1e-7 左右，bf16 环境下会更大）；源码里每个优化器步恰好 \( G-1 \) 次 no_sync + 1 次 nullcontext。

**预期结果**：`max |diff| ≈ 0`（float32 下约 1e-7），说明"除以 G 再累加"与"拼大批求平均"数学等价；归约次数从 \( G \) 次降为 1 次。

#### 4.2.5 小练习与答案

**练习 1**：如果 `loss` 忘记除以 `gradient_accumulation_steps`，训练会发生什么？

**答案**：累加梯度变为大批平均梯度的 \( G \) 倍，等效于把学习率放大 \( G \) 倍（默认配置下是 64 倍），训练极可能发散或需要重调 `max_grad_norm`。梯度裁剪只能部分掩盖这个问题，因为裁剪阈值本身也是按正常梯度尺度设定的。

**练习 2**：把 `no_sync` 用在**所有**微批上（包括最后一个），会发生什么？

**答案**：累计梯度永远不会跨卡归约，各 rank 用自己局部的梯度更新各自的优化器副本，权重从此失同步，且这种错误不会被任何断言捕获（损失可能看起来还在下降）。这就是为什么 `should_sync` 分支必须用 `nullcontext` 而不是统一的 no_sync。

### 4.3 优化器步进：clip_grad_norm_ 与 optimizer.step 的配合

#### 4.3.1 概念说明

同步微批的 backward 结束后，主循环依次做四件事：梯度裁剪 → 优化器步进 → 日志 → （可选）存盘。这里有两个"看不见的东西"最值得注意：一是裁剪为什么用 `FSDP.clip_grad_norm_` 这个静态方法而不是 `torch.nn.utils.clip_grad_norm_`；二是主循环里**没有任何 `zero_grad` 调用**，但它确实发生了。

#### 4.3.2 核心流程

```text
同步微批 backward 完成（.grad 已跨卡归约）
├── grad_norm = FSDP.clip_grad_norm_(model, max_grad_norm)   # 全局范数裁剪
├── optimizer.step()                                          # BF16 优化器一个完整的步进周期
│   ├── 把 bf16 模型梯度拷到 fp32 主权重参数上
│   ├── AdamW 更新 fp32 主权重
│   ├── optimizer.zero_grad()                                 # ← 隐藏的清零在这里
│   ├── scheduler.step()                                      # 学习率推进
│   └── fp32 主权重拷回 bf16 模型参数；模型梯度置 None          # ← 以及这里
├── training_logger.on_optimizer_step(...)
├── [可选] save_and_eval_checkpoint()
└── [可选] 挂起检查
```

#### 4.3.3 源码精读

```python
grad_norm = FSDP.clip_grad_norm_(
    self.model,
    float(self.args.train.max_grad_norm),
)
self.optimizer.step()
training_logger.on_optimizer_step(
    global_step=self.global_step,
    next_micro_step=self.next_micro_step,
    micro_batches_per_epoch=self.micro_batches_per_epoch,
    max_train_steps=self.max_train_steps,
    learning_rate=self.optimizer.get_learning_rate(),
    grad_norm=grad_norm.item(),
)

if self.global_step % int(self.args.logging.checkpointing_steps) == 0:
    self.save_and_eval_checkpoint()
```

[deepspec/trainer/base_trainer.py:386-401](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L386-L401)：逐点说明——

1. **`FSDP.clip_grad_norm_`（静态方法）**：计算总梯度范数必须把**所有 rank 上的参数梯度**考虑在内。FSDP 版本会在内部跨卡聚合出全局范数再统一缩放，与 FSDP 的分片状态（尤其启用分片策略时）正确配合；裸用 `torch.nn.utils.clip_grad_norm_` 在分片模式下只能看到本地碎片，范数算错。本仓库默认 `no_shard` 下两者数值接近，但用 FSDP 版本保证了对所有分片策略的正确性。
2. **裁剪在 step 之前、读取在裁剪之后**：`grad_norm.item()` 逼着 GPU 把范数同步回 CPU（`.item()` 是同步点），日志记录的是裁剪前的真实范数。
3. **`optimizer.step()` 内部就含清零**。看 [deepspec/utils/optim.py:108-122](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/optim.py#L108-L122)：

```python
def step(self):
    with torch.no_grad():
        for model_param, master_param in zip(self.model_params, self.fp32_params):
            master_param.grad = (
                model_param.grad.detach().to(torch.float32)
                if model_param.grad is not None
                else None
            )
    self.optimizer.step()
    self.optimizer.zero_grad()          # ← fp32 主参数梯度清零
    self.scheduler.step()
    with torch.no_grad():
        for model_param, master_param in zip(self.model_params, self.fp32_params):
            model_param.data.copy_(master_param.data.to(model_param.dtype))
            model_param.grad = None     # ← bf16 模型梯度置 None
```

`BF16Optimizer.step` 是一个完整周期：bf16 梯度上转 fp32 → AdamW 更新 → **zero_grad** → 学习率调度 → fp32 主权重拷回 bf16 模型 → 模型梯度置 None。所以主循环不需要、也不能再额外调用 zero_grad（否则会在累积期间误清梯度）。下一个累积周期从"梯度为 None/零"的状态干净开始。

4. **日志钩子**：[deepspec/utils/training_logger.py:31-56](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/training_logger.py#L31-L56) 的 `on_optimizer_step` 每步都登记 `lr`、`grad_norm`（reduction="last"），但只有 `global_step % logging_steps == 0`（默认 10）时才 `flush()` 出全部累积指标（如 `train/loss`，它在 `run_batch` 内部由损失函数注册）并打印进度行、写 TensorBoard。细节留到 u3-l6。
5. **存盘条件**：`self.global_step % checkpointing_steps == 0`（默认 3000，见 [config/dspark/dspark_qwen3_4b.py:47-50](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L47-L50)）。注意此时 `next_micro_step` 已自增，`global_step` 是刚完成的优化器步号；循环自然结束后还有一次无条件收尾保存（[base_trainer.py:407](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L407)）。

`save_and_eval_checkpoint` 的骨架：

```python
def save_and_eval_checkpoint(self):
    checkpoint_dir = save_checkpoint(**self._checkpoint_kwargs())
    if is_global_main_process():
        _launch_eval(...)
    dist.barrier()
    return checkpoint_dir
```

[deepspec/trainer/base_trainer.py:333-344](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L333-L344)：所有 rank 都要参与 `save_checkpoint`（FSDP 状态字典需要各 rank 的分片），全局主进程额外提交一次自动评测（公开环境下 `auto_eval_command` 为 None，只打印提示，见 [base_trainer.py:138-153](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L138-L153)），最后 `dist.barrier()` 等所有 rank 对齐后继续训练。存盘细节（`_checkpoint_kwargs` 传了什么、step_latest 符号链接如何生成）是 u3-l5 的主题。

#### 4.3.4 代码实践

**实践目标**：确认"zero_grad 藏在 BF16Optimizer.step 里"，并理解 `checkpointing_steps` 与 `max_train_steps` 的关系。

**操作步骤**：

1. 打开 [deepspec/utils/optim.py:108-122](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/optim.py#L108-L122)，用笔圈出 `zero_grad()` 与 `model_param.grad = None` 两行；再在 [deepspec/trainer/base_trainer.py:355-407](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L355-L407) 里全文搜索 `zero_grad`，确认主循环确实没有出现这个词。
2. 推演一个具体场景：默认配置 `checkpointing_steps=3000`、`num_train_epochs=10`。查 [u3-l1 讲过的 `_compute_training_schedule`](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L98-L135)，若 `steps_per_epoch` 不能被 3000 整除，哪些边界步会存盘？循环结束时那次收尾保存发生在第几步？

**需要观察的现象**：`zero_grad` 只在 optim.py 中出现；存盘发生在 `global_step ∈ {3000, 6000, ...}` ∩ `[1, max_train_steps]`，外加最后一次 `global_step == max_train_steps` 的无条件保存（若恰为倍数则同一目录语义上只写一次）。

**预期结果**：能口头回答"为什么主循环不需要 zero_grad"——因为 `BF16Optimizer.step` 在每次更新后同时清掉了 fp32 主参数梯度和 bf16 模型梯度，下一个累积周期天然从零开始。步骤 2 的具体步数取决于数据集大小，无法在本地无数据时算出，**待本地验证**（需要真实 `dataset_size` 才能得出 `steps_per_epoch`）。

#### 4.3.5 小练习与答案

**练习 1**：为什么日志里的 `grad_norm` 取的是 `grad_norm.item()`，而 loss 等指标走 `add_metric` 累积？

**答案**：`grad_norm` 是刚完成这一步的即时标量，关心的就是"此刻多大"（reduction="last"），`.item()` 同步取回后每步登记；而 loss、accept rate 等是随微批波动的统计量，关心一段窗口内的均值，所以由损失函数在每个微批上 `add_metric` 累积、`on_optimizer_step` 每 `logging_steps` 步 flush 一次求平均。

**练习 2**：把 `checkpointing_steps` 设得很大（比如大于 `max_train_steps`）会有什么后果？

**答案**：训练中途一次 periodic 存盘都不会发生，只在循环自然结束后收尾保存一次。一旦中途崩溃或被抢占，只能从头开始（或从上次运行的旧 checkpoint 恢复），损失的进度最多可达整个训练时长。反之设得太小则频繁触发全 rank 状态字典落盘，训练吞吐下降——这是可靠性与吞吐之间的权衡旋钮。

### 4.4 SuspendController 挂起保存

#### 4.4.1 概念说明

训练可能运行数天，而集群随时可能要回收机器（抢占）。被"硬杀"的进程若恰好在两次存盘之间，最多丢 `checkpointing_steps` 步的进度。`SuspendController` 把"任意时刻被杀"改造成"在同步边界优雅存盘后自愿挂起"：一个后台线程轮询平台发出的挂起命令，收到后在下一个同步微批末尾保存 checkpoint、通知平台"可以安全挂我了"。它依托 [deepspec/utils/hfai_suspend.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/hfai_suspend.py)，其中 `hfai` 是内部集群 SDK——公开环境装不到，所以整个模块用 try-import 优雅降级：没有 hfai 时监听不启动、`requested()` 恒为 False、主循环逻辑完全不受影响。

#### 4.4.2 核心流程

```text
with suspend_controller.monitoring():        # 进入训练循环前
    仅全局主进程启动守护线程，每 1s 轮询 hfai.client.receive_suspend_command()
    │
    ▼ 收到挂起命令 → 线程置位 _requested 事件后退出
    │
训练循环每个同步微批末尾：
    requested()  ── dist.barrier()                       # 各 rank 对齐
                 ── 全局主进程把 _requested 写入设备上的 0/1 张量
                 ── dist.broadcast(flag, src=0)           # 决定广播到所有 rank
                 └── 所有 rank 得到同一个布尔值
    若为 True：
        _save_and_suspend()
        ├── save_checkpoint(...)        # 所有 rank 参与（FSDP 状态字典聚合）
        ├── dist.barrier()              # 确认全部落盘
        ├── 仅全局主进程：go_suspend()   # hfai.client.go_suspend() 通知平台
        └── dist.barrier()
        train() return                  # 退出训练循环
    随后 train.py 调 trainer.clean_up()：关日志 → barrier → 销毁进程组
重启后：discover_latest_checkpoint(step_latest 符号链接)
      → load_training_state → 恢复 next_micro_step → 从精确微批偏移续训
```

#### 4.4.3 源码精读

**环境探测与降级**：

```python
try:
    import hfai
    HAS_HFAI = True
except ModuleNotFoundError:
    hfai = None
    HAS_HFAI = False
```

[deepspec/utils/hfai_suspend.py:9-15](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/hfai_suspend.py#L9-L15)：可选依赖的标准写法。后续三个关键方法都在第一行检查 `HAS_HFAI`，公开环境（pip 装不到 hfai）下整个控制器是"空转"的。

**监听线程与上下文管理器**：

```python
def _monitor_suspend(self):
    print_on_global_main("Start monitoring hfai suspend signal...")
    while not self._stop_requested.is_set():
        if hfai.client.receive_suspend_command():
            print_on_global_main("Received hfai suspend command!")
            self._requested.set()
            return
        self._stop_requested.wait(timeout=self.poll_interval_seconds)

@contextlib.contextmanager
def monitoring(self):
    if not HAS_HFAI:
        yield self
        return
    try:
        self._requested.clear()
        self._stop_requested.clear()
        if is_global_main_process():
            self._monitor_thread = threading.Thread(
                target=self._monitor_suspend, daemon=True)
            self._monitor_thread.start()
        yield self
    finally:
        self._stop_requested.set()
        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout=self.poll_interval_seconds + 1.0)
            self._monitor_thread = None
```

[deepspec/utils/hfai_suspend.py:27-56](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/hfai_suspend.py#L27-L56)：只有全局主进程（rank 0）开线程轮询，其余 rank 不监听——因为"是否挂起"这一决定随后会被广播。`_stop_requested.wait(timeout=1.0)` 兼作"睡 1 秒"的手段，避免裸 sleep 导致收不到停止信号。`with` 退出时置停止位并 join（限时 2 秒），保证训练结束后不残留线程。

**跨 rank 一致的判定**：

```python
def requested(self) -> bool:
    if not HAS_HFAI:
        return False
    dist.barrier()
    if is_global_main_process():
        self._suspend_flag[0] = 1 if self._requested.is_set() else 0
    dist.broadcast(self._suspend_flag, src=0)
    return bool(self._suspend_flag.item())
```

[deepspec/utils/hfai_suspend.py:58-66](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/hfai_suspend.py#L58-L66)：这是本模块最精巧的一段。只有 rank 0 知道真相，但"要不要挂起"必须是**所有 rank 的共同决定**——若有 rank 想退、有 rank 想继续，后续集合通信立刻死锁。因此用 `barrier + broadcast` 把 rank 0 的决定强制对齐到所有进程。注意它包含集合通信，所以调用点必须落在所有 rank 迭代对齐的位置——主循环把它放在同步微批末尾（[base_trainer.py:403](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L403)），由 `next_micro_step` 的全局一致性保证对齐。

**保存与挂起**：

```python
if self.suspend_controller.requested():
    self._save_and_suspend()
    return
```

[deepspec/trainer/base_trainer.py:403-405](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L403-L405)：挂起检查放在同步微批的最后（存盘检查之后），返回值直接终止 `train()`。

```python
def _save_and_suspend(self):
    print_on_global_main("Saving checkpoint before suspending...")
    save_checkpoint(**self._checkpoint_kwargs())
    dist.barrier()
    if is_global_main_process():
        print_on_global_main("Going to suspend...")
        self.suspend_controller.go_suspend()
    dist.barrier()
```

[deepspec/trainer/base_trainer.py:346-353](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L346-L353)：三段式——先全 rank 存 checkpoint（注意：即使本步不满足 `checkpointing_steps` 倍数也会存，因为被抢占时必须保住最新进度）；barrier 确认全部写完；最后仅 rank 0 调 `go_suspend()`（[hfai_suspend.py:68-71](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/hfai_suspend.py#L68-L71)，即 `hfai.client.go_suspend()`，通知平台"数据已安全，可以挂起我"）。`train()` 返回后，[train.py:36-38](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/train.py#L36-L38) 的 `trainer.clean_up()` 收尾：关闭 TensorBoard writer、barrier、销毁进程组（[base_trainer.py:409-412](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L409-L412)）。

重启后的恢复闭环在 `__init__` 里完成：`discover_latest_checkpoint` 通过 `step_latest` 符号链接找到最近 checkpoint（[ckpt_manager.py:25-29](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/ckpt_manager.py#L25-L29)），`load_training_state` 恢复优化器与 `next_micro_step`（[base_trainer.py:221-231](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L221-L231)），于是 `train()` 里的 `start_offset_samples` 把数据流精确拨回挂起前的微批位置。

#### 4.4.4 代码实践

**实践目标**：把 `SuspendController.requested()` 触发时的保存-挂起-退出路径写成伪代码，并验证公开环境下降级逻辑。

**操作步骤**：

1. 先确认降级事实（本机只需装了 torch 与仓库依赖，CPU 即可，无需 GPU、无需 hfai）：

```bash
python -c "from deepspec.utils.hfai_suspend import HAS_HFAI; print('HAS_HFAI =', HAS_HFAI)"
```

2. 对照 [base_trainer.py:403-405](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L403-L405) 与 [hfai_suspend.py:58-71](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/hfai_suspend.py#L58-L71)，手写（或键入笔记）如下伪代码，并逐行标注对应的源码行号：

```text
伪代码：挂起路径

在每个同步微批的末尾（此时 next_micro_step 已自增、优化器已步进）：
    requested = SuspendController.requested()
        # hfai 不可用 → 恒 False，路径到此为止（公开环境）
        # hfai 可用   → barrier；rank0 把 _requested 事件写进 flag 张量；
        #               broadcast(flag, src=0)；所有 rank 读到同一个值
    if not requested: 继续训练
    _save_and_suspend():
        save_checkpoint(model, draft_model, optimizer, next_micro_step, ...)
            # 所有 rank 参与；next_micro_step 随状态一起落盘
        dist.barrier()                  # 等最后一个 rank 写完
        if 全局主进程:
            hfai.client.go_suspend()    # 告诉平台：现在挂我是安全的
        dist.barrier()
    train() 返回
clean_up(): training_logger.close() → dist.barrier() → destroy_process_group()
（平台稍后重启任务 → step_latest → 恢复 next_micro_step → 从偏移续训）
```

3. 思考并验证一个对齐问题：假如把 `requested()` 的调用从"同步微批末尾"挪到"每个微批末尾"（包括 no_sync 微批），会出什么问题？

**需要观察的现象**：步骤 1 在公开环境应打印 `HAS_HFAI = False`；步骤 2 的伪代码每一行都能在源码中找到对应行号。

**预期结果**：`HAS_HFAI = False`（若你所在的内部环境装有 hfai 则为 True）；伪代码行号对得上，且能说清"为什么 requested() 必须放在所有 rank 都会执行的相同迭代位置"——它内部是集合通信，任何 rank 缺席都会挂死。步骤 3 的答案见下面练习 2。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `requested()` 里要先 `dist.barrier()` 再 `broadcast`，而不是每个 rank 各自调用 `receive_suspend_command()`？

**答案**：挂起信号由平台下发给主进程，各 rank 自行查询可能得到不一致的答案（查询时刻不同、可见性不同）；而"挂起"是全体行为，一旦有人退出、有人继续，后续任何集合通信（包括下一个 micro batch 的梯度归约）都会死锁。broadcast 把 rank 0 的单一决定原子地复制到所有 rank，保证决定全局一致。barrier 则先把各 rank 汇合到同一程序点，避免 rank 0 先写 flag 时其他 rank 还没到。

**练习 2**：把 `requested()` 挪到每个微批末尾（含 no_sync 微批）调用，会有什么风险？

**答案**：仅当各 rank 的微批迭代次数完全一致时集合通信才对齐；虽然本仓库的采样器保证了这一点，但更隐蔽的问题是执行语义被破坏：在累积中途挂起意味着 `.grad` 里攒着不完整的梯度，虽然此时选择保存 checkpoint 并退出、重启后从微批偏移重做整个累积周期在逻辑上仍可恢复（next_micro_step 已落盘），但每多一次 barrier+broadcast 也增加了无谓同步开销；更重要的是它违背了"只在稳定的同步边界做全局决定"的设计原则，把一个干净的状态机变成了任意微批都可打断的状态机，排查问题的成本大幅上升。

**练习 3**：公开环境（没有 hfai）下，如果某次训练真收到了 `requested() == True`，可能吗？

**答案**：不可能。`requested()` 第一行就是 `if not HAS_HFAI: return False`，连集合通信都不会执行；`go_suspend()` 也只在 HAS_HFAI 分支里被调用，否则会抛 `RuntimeError`（[hfai_suspend.py:68-70](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/hfai_suspend.py#L68-L70)）。因此在公开环境，挂起分支是不可达代码（unreachable），主循环等价于一个不带挂起逻辑的普通训练循环。

## 5. 综合实践

把本讲三个最小模块串成一个"人肉调试器"任务：

1. **变量标注**（对应 4.1）：打印 [deepspec/trainer/base_trainer.py:355-407](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L355-L407)，用三种颜色分别标出 `next_micro_step` 的全部出现点（读/写）、`global_step` 的全部读取点、`should_sync` 的作用域。然后仅凭标注回答：从 `next_micro_step = 63`（G=64）重启后，第一个微批的 `should_sync` 是什么？（答：`(63+1) % 64 == 0` → True，重启后第一个微批立刻凑满一个优化器步——这正说明恢复粒度是微批级的，梯度会从零开始累积，不丢不重。）
2. **通信审计**（对应 4.2）：数一数默认配置（G=64）下一个优化器步内：`no_sync` 进入/退出多少次、梯度跨卡归约多少次、`FSDP.clip_grad_norm_` 内的跨卡聚合多少次。写成一张小表，并估算若去掉 no_sync，每步多出多少次 all-reduce。
3. **挂起路径推演**（对应 4.4）：合上源码，默写 `_save_and_suspend` 的三段式（存盘 → barrier → rank0 挂起 → barrier），标注哪一步是全 rank 参与、哪一步只有 rank 0；再对照 [base_trainer.py:346-353](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L346-L353) 检查漏项。
4. **（可选，需 GPU 与小缓存）** 用 u2 产出的迷你 target cache 跑 `train.py`，把 `--opts "logging.checkpointing_steps=2"` 调小，观察每 2 步打出的存盘日志与 `training_logger` 进度行中 epoch/step 的推进是否符合第 1 步的推演。**待本地验证**。

## 6. 本讲小结

- `train()` 是固定骨架的模板方法：进度计算 → CUDAPrefetcher 供数 → 梯度累积 → 优化器步进 → 日志/存盘/挂起检查，前向细节全部下放给子类的 `run_batch`。
- `next_micro_step` 是训练进度的唯一真相源：`global_step`（属性 `//G`）、epoch（`//micro_batches_per_epoch`）、数据采样偏移（`×local_batch_size`）都是它的派生量；断点续训只需恢复这一个整数。
- `should_sync = (next_micro_step + 1) % G == 0` 决定当前微批是累积步还是同步步：累积步在 `model.no_sync()` 里做 forward+backward（梯度本卡累加、零通信），同步步裸跑 backward 让 FSDP 一次性归约累计梯度——每个优化器步只付 1 次梯度归约。
- loss 先除以 G 再 backward，使累加梯度等价于大批平均梯度；`zero_grad` 藏在 `BF16Optimizer.step` 内部（fp32 主参数 `optimizer.zero_grad()` + bf16 模型梯度置 None），主循环因此看不到它。
- 裁剪用静态方法 `FSDP.clip_grad_norm_` 以正确处理跨卡/分片下的全局范数，`.item()` 同步取回日志值；存盘按 `global_step % checkpointing_steps == 0` 触发，循环结束另有一次无条件收尾保存。
- `SuspendController` 用 try-import hfai 降级：公开环境下监听不启动、`requested()` 恒 False，挂起分支不可达；内部环境下由 rank 0 后台线程轮询挂起命令，经 barrier+broadcast 对齐决定，在同步微批末尾存盘后自愿挂起，与 `step_latest` + `next_micro_step` 组成完整的抢占-恢复闭环。

## 7. 下一步学习建议

- 下一讲 **u3-l3（分布式启动与无状态可恢复采样器）**：本讲反复依赖"各 rank 迭代对齐"与"从任意样本偏移恢复"，其保证就在 `init_dist` 与 `StatelessResumableDistributedSampler` 中，建议紧接着读 [deepspec/utils/distributed.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/distributed.py)。
- 若想先弄清存盘细节（`_checkpoint_kwargs` 传了什么、FSDP 状态字典如何聚合、`step_latest` 如何原子替换），直接跳 **u3-l5** 读 [deepspec/trainer/ckpt_manager.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/ckpt_manager.py)。
- 本讲的 `run_batch` 一行带过了 DSpark 损失的组合方式，其内部（CE、L1 蒸馏、置信度）将在 **u4-l4** 展开；学习率调度曲线（`scheduler.step` 每步推进）在 **u3-l4** 精读。
- 延伸阅读：PyTorch 官方文档中 FSDP 的 `no_sync` 与梯度累积章节、`clip_grad_norm_` 的 FSDP 版本说明，可对照本讲 4.2/4.3 的源码结论加深理解。

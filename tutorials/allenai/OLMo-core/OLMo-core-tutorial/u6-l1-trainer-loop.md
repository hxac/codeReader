# u6-l1 Trainer 训练循环剖析

## 1. 本讲目标

在 [u1-l4](u1-l4-first-training-example.md) 中，我们已经从外面看过一次 `trainer.fit()`：它把模型、数据、优化器串起来跑。本讲我们要打开这个黑盒，精读 `src/olmo_core/train/trainer.py` 中的 `Trainer` 类，学完后你应该能够：

1. **逐步跟踪** `Trainer.fit()` 中一个训练 step 的完整执行顺序：数据获取 → 前向 → 反向 → 优化器 step → 指标 → checkpoint，并能准确说出每个回调钩子插入的位置。
2. **解释** `Duration` 的三种单位（steps / tokens / epochs）如何决定「训练多久停」，以及 `max_duration` 与 `hard_stop` 的区别。
3. **说明** `LoadStrategy` 三个取值（if_available / always / never）在断点续训时的行为差异。
4. **描述** 指标（metric）从 `record_metric()` 到回调 `log_metrics()` 的完整旅程，包括跨 rank 归约（`ReduceType`）和异步簿记（async bookkeeping）机制。

## 2. 前置知识

本讲假设你已读过前置讲义：

- **u1-l4（第一个训练例子）**：知道一次训练实验由 model / dataset / data_loader / train_module / trainer 五块 Config 组成，`fit()` 是训练入口。
- **u5-l2（学习率调度器）**：知道调度器需要 `t_max`（总步数）才能画余弦曲线。本讲会看到 `t_max` 的源头正是 `Trainer.max_steps`，而它由 `max_duration` 换算而来。

再用通俗语言补几个本讲需要的基础概念：

- **step（步）与 batch（批）**：训练每处理一个 batch 就算一个 step。`Trainer` 用 `global_step` 计数，从 1 开始计（第一个 step 结束后 `global_step == 1`）。
- **epoch（轮）**：把整个训练数据集完整过一遍叫一个 epoch。数据加载器的 batch 序列耗尽即一轮结束。
- **rank（进程秩）**：分布式训练中每个进程有一个编号。各 rank 跑着完全相同的训练循环代码，「指标归约」就是把各 rank 上算出的局部指标合并成全局值。
- **回调（Callback）**：一种「钩子」设计——训练循环在固定的位置（如「优化器 step 之前」「每个 step 之后」）调用注册进来的对象的方法，从而让外部代码能介入训练过程而不用修改循环本身。回调体系的细节是 [u6-l3](u6-l3-callback-system.md) 的主题，本讲只关注**钩子在循环中的位置**。
- **host-device 同步（host-device sync）**：GPU 上的张量要变成 Python 数字（比如打印 loss）时，CPU 必须等 GPU 算完，这一等待会打断 GPU 流水线。本讲会看到 Trainer 为减少这种同步做了不少工程设计。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/olmo_core/train/trainer.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py) | `Trainer` 类：训练主循环 `fit()` / `_fit_epoch()`、停训判定、指标收集、checkpoint 保存/加载、取消机制、异步簿记。本讲的主战场。 |
| [src/olmo_core/train/config.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/config.py) | `TrainerConfig`：`Trainer` 的 Config 声明与 `build()` 组装逻辑。 |
| [src/olmo_core/train/common.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/common.py) | 训练相关的公共小件：`Duration` / `DurationUnit`、`LoadStrategy`、`ReduceType`、`MetricMergeStrategy`、`StepSkipRange`、`TrainingProgress`，以及 `train/CE loss` 等指标名常量。 |
| [src/olmo_core/train/utils.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/utils.py) | 指标搬运与归约的实现：`move_metrics`、`reduce_metrics`、`check_metrics_consistent`，以及 RNG 状态的保存/恢复。 |
| [src/olmo_core/train/callbacks/callback.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/callback.py) | `Callback` 基类：定义全部钩子的签名与默认空实现。本讲引用它来对照钩子位置。 |

辅助引用（只点到为止，细节留给后续讲义）：[src/olmo_core/train/callbacks/checkpointer.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/checkpointer.py)（checkpoint 在哪个钩子里保存）与 [src/olmo_core/train/callbacks/evaluator_callback.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/evaluator_callback.py)（评测在哪个钩子里触发）。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**Duration（停训语义）**、**Trainer 主循环（fit 与一个 step 的顺序）**、**指标收集与跨 rank 归约**、**TrainerConfig 与 LoadStrategy**。

### 4.1 Duration：训练时长的三种计量与停训判定

#### 4.1.1 概念说明

「训练多久停」看起来是个简单问题，实际上它是训练配置里最重要的一项之一，原因有二：

1. **学习率调度需要总步数**。如 [u5-l2](u5-l2-lr-schedulers.md) 所讲，`CosWithWarmup` 的余弦曲线是相对「horizon（总步数）」定义的，不知道总步数就画不出曲线。
2. **不同实验用不同的自然单位**。调参小实验习惯说「跑 1000 步」，大模型预训练习惯说「吃 4T token」，学术复现习惯说「跑 3 个 epoch」。

`Duration` 就是把这个概念显式建模成一个「数值 + 单位」的小 dataclass。单位有三种：`steps`（步）、`epochs`（轮）、`tokens`（token 数）。

但有个关键约束：**用 epochs 计量必须能提前换算出总步数**。总步数 = 每轮 batch 数 × 轮数，而某些数据加载器（如流式源）事先不知道自己有多少 batch。所以 `Trainer.max_duration` 的文档明确要求：若 `data_loader.total_batches` 未知，就不能用 epochs 计量。

#### 4.1.2 核心流程

`Duration` 的核心是一个判定函数 `due()`：给定当前进度（step、tokens、epoch），回答「该停了吗」：

```
due(step, tokens, epoch):
    若 unit == steps : 返回 step  >= value   # 走够步数
    若 unit == tokens: 返回 tokens >= value   # 吃够 token
    若 unit == epochs: 返回 epoch >  value    # 注意是严格大于！
```

epochs 用**严格大于**是个容易踩坑的细节：`epoch` 从 1 开始计，且**一轮跑完后才自增**（见 4.2.2）。设 `Duration.epochs(1)`，训练期间 `epoch == 1`，`1 > 1` 为假，不停；第 1 轮结束、`epoch` 变成 2 时，`2 > 1` 为真，停。这样「训 1 轮」恰好完整覆盖第 1 轮。

判定发生在 `Trainer.training_complete` 属性里，它按顺序检查四件事：

```
training_complete:
    1. 曾有异常          → 抛 RuntimeError
    2. 每 cancel_check_interval 步 → 异步检查是否有 rank 请求取消
    3. is_canceled       → True（被取消，如收到 SIGTERM）
    4. max_duration 到期 → True（正常训练完成）
    5. hard_stop 到期    → True（提前硬停）
```

`max_duration` 与 `hard_stop` 的区别很微妙也很实用：`hard_stop` 是「消融实验用的提前刹车」——比如只想跑完整训练的前 10% 看看效果。如果直接改小 `max_duration`，总步数变了，**学习率曲线也会跟着变**，实验就不可比；而 `hard_stop` 不影响 `max_steps`，调度曲线照旧，只是提前停。

反向的换算也需要理解：调度器要的是「总步数」，`_get_max_steps()` 把三种单位统一换算成绝对步数：

- steps → 直接就是 `value`；
- epochs → 本轮剩余 batch 数 + 未来每一轮的 batch 数（任何一轮未知则返回 `None`）；
- tokens → \(\lceil (\text{max\_tokens} - \text{tokens\_seen}) / \text{tokens\_per\_batch} \rceil\)，再折回步数。注意它用的是**当前** batch 大小，因为训练中途改 batch size（序列长度调度）时 token 数才是真正不变的量。

#### 4.1.3 源码精读

**Duration 与三种单位**。[common.py:L36-L66](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/common.py#L36-L66) 定义了 `Duration` dataclass 及三个工厂方法 `steps()` / `epochs()` / `tokens()`，让调用方写 `Duration.steps(1000)` 而不是裸构造。

**Chinchilla 便捷工厂**。[common.py:L68-L92](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/common.py#L68-L92) 提供 `Duration.chinchilla_tokens(multiple, model_params=...)`：按经验法则「每个（非 embedding）参数配 20 个 token」计算 Chinchilla 最优训练量，即

\[
\text{tokens} = 20 \times \text{model\_params} \times \text{multiple}
\]

`multiple=1.0` 是 Chinchilla 最优，大于 1 是「过训练」（overtrained，牺牲训练时计算换取推理时表现）。

**停训判定**。[common.py:L94-L105](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/common.py#L94-L105) 是 `due()` 的完整实现，注意 epochs 分支的 `epoch > self.value`。

**Trainer 侧的到期检查**。[trainer.py:L438-L457](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L438-L457) 是 `training_complete` 属性：先做周期性取消检查（默认每 25 步一次，见 [trainer.py:L260-L265](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L260-L265)），再依次判定取消、`max_duration`、`hard_stop`。`_duration_due` 只是把当前的 (step, tokens, epoch) 三元组传给 `duration.due()`（[trainer.py:L1433-L1436](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1433-L1436)）。

**max_duration 的文档约束**。[trainer.py:L171-L181](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L171-L181) 明确写着：总步数必须提前可知（学习率调度等原因），若数据加载器的 batch 总数未知，必须用 tokens 或 steps 计量。

**hard_stop 的动机**。[trainer.py:L267-L272](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L267-L272)：为消融实验设置硬停点，「不想跑完整训练，又不想因改 `max_duration` 而影响学习率调度」。

**单位 → 步数换算**。[trainer.py:L519-L547](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L519-L547) 是 `_get_max_steps()`，三种分支如 4.1.2 所述；tokens 分支用 `math.ceil` 向上取整（宁可多走一步也不能少喂 token）。`max_steps` / `max_tokens` 属性（[trainer.py:L489-L501](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L489-L501)）就是它的门面。

#### 4.1.4 代码实践

1. **实践目标**：不训练任何模型，直接在 Python 里验证你对 `Duration.due()` 边界的理解。
2. **操作步骤**：在装好 `olmo_core` 的环境里运行下面的脚本（示例代码，非项目原有代码）：

   ```python
   from olmo_core.train import Duration

   d = Duration.steps(10)
   print("step=9 :", d.due(step=9, tokens=0, epoch=1))
   print("step=10:", d.due(step=10, tokens=0, epoch=1))

   e = Duration.epochs(1)
   print("epoch=1（第 1 轮进行中）:", e.due(step=999, tokens=0, epoch=1))
   print("epoch=2（第 1 轮已结束）:", e.due(step=999, tokens=0, epoch=2))

   t = Duration.chinchilla_tokens(1.0, model_params=1_000_000)
   print("1M 参数的 Chinchilla 最优 token 数:", t)
   ```

3. **需要观察的现象**：四行布尔输出与一个 `Duration` 对象。
4. **预期结果**（依据 [common.py:L94-L105](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/common.py#L94-L105) 与 [common.py:L91-L92](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/common.py#L91-L92) 推导）：依次为 `False`、`True`、`False`、`True`，以及 `20 * 1_000_000 * 1.0 = 20_000_000` 个 token。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Duration.due()` 的 epochs 分支用 `epoch > value` 而不是 `>=`？

**答案**：`epoch` 从 1 开始计，且在一轮**完整跑完之后**才自增（见 4.2.3 的 epoch 收尾）。若用 `>=`，`Duration.epochs(1)` 会在第 1 轮刚开始（`epoch == 1`）就判定到期，一轮都训不到；用 `>` 则恰好在整个第 1 轮期间（`epoch == 1`）保持「未到期」，进入第 2 轮前（`epoch == 2`）才停。

**练习 2**：某实验的数据加载器是流式的，`total_batches` 未知，能设 `max_duration=Duration.epochs(2)` 吗？

**答案**：不能。epochs 需要换算成总步数（[trainer.py:L505-L511](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L505-L511) 会因 `steps_per_epoch is None` 抛 `RuntimeError`），因为学习率调度等机制要求总步数提前可知。应改用 `Duration.steps(...)` 或 `Duration.tokens(...)`。

**练习 3**：想只跑完整训练的前 5% 做消融，为什么推荐 `hard_stop` 而不是调小 `max_duration`？

**答案**：学习率曲线（warmup + 余弦衰减）相对 `max_steps` 定义。调小 `max_duration` 会改变 `max_steps`，曲线随之改变，与完整训练不可比；`hard_stop` 只触发提前停止，`max_steps` 不变，调度曲线与完整训练完全一致。

### 4.2 Trainer.fit() 主循环：一个 step 的完整执行顺序

#### 4.2.1 概念说明

先明确 `Trainer` 的角色定位：**它是编排者（orchestrator），不是计算者**。翻遍 `trainer.py` 你找不到一行矩阵乘法——前向、反向、优化器更新全部委托给 `train_module`（其内部结构是 [u6-l2](u6-l2-transformer-train-module.md) 的主题）；数据获取委托给 `data_loader`；checkpoint 保存、评测、日志等「横切关注点」则委托给回调（callbacks）。

`Trainer` 自己负责的是「节奏」：

- 维护全局簿记：`global_step`、`global_train_tokens_seen`、`global_train_petaflops`、`epoch`（[trainer.py:L239-L258](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L239-L258)）；
- 决定何时开始、何时停止（4.1 的 Duration 机制）；
- 在循环的固定位置调用回调钩子。

理解本节后你会发现一个重要事实：**checkpoint 和评测并不直接写在训练循环里**，而是分别藏在 `CheckpointerCallback.post_train_batch()` 和 `EvaluatorCallback.post_step()` 这两个钩子的实现里。训练循环只负责「在正确的时机喊话」，喊完话谁在听、做什么，由回调自己决定。

#### 4.2.2 核心流程

`fit()` 顶层流程（对应 [trainer.py:L687-L782](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L687-L782)）：

```
fit()
 ├─ 1. 重置取消状态
 ├─ 2. 按 load_strategy 尝试恢复 checkpoint（save_folder 优先，其次 load_path）
 ├─ 3. barrier() 同步所有 rank，打印回调顺序与总步数
 ├─ 4. 安装 SIGTERM/SIGINT 信号处理器（收到即优雅取消）
 ├─ 5. 钩子：callback.pre_train() × N，然后 train_module.pre_train()
 ├─ 6. 快速检查是否已被取消
 ├─ 7. dry-run 一个 mock batch（预热编译、尽早暴露 OOM）
 ├─ 8. while not training_complete:
 │       _fit_epoch()          ← 真正的训练在这里
 ├─ 9. （异常路径）callback.on_error(exc) → 非优雅关闭 → 重新抛出
 ├─ 10. 钩子：callback.post_train() × N
 └─ 11. _shutdown()：flush 指标、等待簿记线程、callback.close()、barrier、gc
```

`_fit_epoch()` 与单 step 流程（对应 [trainer.py:L1680-L1759](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1680-L1759)）——**这是本讲最核心的一张图**：

```
_fit_epoch()
 ├─ A. data_loader.reshuffle(epoch)          # 本轮洗牌
 ├─ B. 钩子：callback.pre_epoch() × N
 ├─ C. train_module.zero_grads()             # 清梯度进入本轮
 ├─ D. 对每个 batch 循环（_iter_batches）：
 │    ├─ 钩子：callback.pre_load_batch() × N        # 取数之前
 │    ├─ batch = next(data_loader)                  # ① 数据获取
 │    ├─ global_step += 1                           # ② 簿记：步数
 │    ├─ global_train_tokens_seen  += 本 batch 全局 token 数
 │    ├─ global_train_petaflops    += 本 batch FLOPs / 1e15
 │    ├─ 检查 steps_to_skip 命中？                   # ③ 可选跳步判定
 │    ├─ 钩子：callback.pre_step(batch) × N          # 前向之前
 │    ├─ [未跳过]
 │    │   ├─ train_module.train_batch(batch)        # ④ 前向 + 反向（含记录 CE loss）
 │    │   ├─ 钩子：callback.pre_optim_step() × N     # 优化器之前
 │    │   ├─ train_module.optim_step()              # ⑤ 优化器 step（含梯度裁剪）
 │    │   └─ train_module.zero_grads()              # ⑥ 清梯度
 │    ├─ [跳过] 仅打印 warning
 │    ├─ 钩子：callback.post_train_batch() × N       # ★ checkpoint 在这里保存
 │    ├─ 钩子：callback.post_step() × N              # ★ 评测在这里触发
 │    ├─ 每 metrics_collect_interval 步：_log_metrics()  # ⑦ 指标归约与下发
 │    └─ if training_complete: 补一次 _log_metrics() 后 return  # ⑧ 停训检查
 ├─ E. _log_metrics()                          # 轮末 flush 残余指标
 ├─ F. 钩子：callback.post_epoch() × N
 └─ G. epoch += 1; data_loader.reset()         # 轮次推进
```

几个值得标注的细节：

- **簿记先于一切**：`global_step` 等计数在 `pre_step` 钩子**之前**自增，所以回调里读到的 `self.step`（即 `trainer.global_step`）已经是当前 step 的编号。
- **跳步不跳簿记**：命中 `steps_to_skip`（[trainer.py:L300-L303](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L300-L303)、[common.py:L274-L281](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/common.py#L274-L281)）时只跳过 ④⑤⑥（前向/反向/更新），step 计数、token 计数、全部钩子照常执行。
- **停训检查在 step 末尾**：`training_complete` 每步检查一次（⑧），所以 `Duration.steps(100)` 意味着完整跑满 100 步后在第 100 步末尾停。
- **钩子的执行顺序是确定且全 rank 一致的**：回调按 `(priority, 注册顺序)` 排序（[trainer.py:L1413-L1423](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1413-L1423)），排序理由写在注释里——部分回调会做分布式通信，顺序不一致会死锁。
- **`no_checkpoints` / `no_evals` 是筛选器**：`_iter_callbacks()`（[trainer.py:L1425-L1431](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1425-L1431)）会把 `CheckpointerCallback` / `EvaluatorCallback` 从迭代中滤掉，实现「跑 benchmark 不存盘不评测」。

#### 4.2.3 源码精读

**fit() 的骨架**。[trainer.py:L687-L782](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L687-L782)：这段代码里，先按加载策略恢复 checkpoint（L696-L728，详见 4.4），随后 L739-L741 安装信号处理器——`SIGTERM`/`SIGINT`（Ctrl-C、K8s 驱逐）被转成 `cancel_run()` 而非直接崩溃，这让训练能走到优雅关闭路径、把已排队的异步 checkpoint 落盘。L743-L746 依次触发所有回调的 `pre_train()` 与 `train_module.pre_train()`。L755-L757 的 dry-run 用 `data_loader.get_mock_batch()` 造一个假 batch 走一遍 `train_batch(dry_run=True)`（实现在 [trainer.py:L1660-L1669](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1660-L1669)）：目的是触发 `torch.compile` 编译并在训练真正开始前暴露 OOM。L760-L761 是主循环本体。异常路径（L762-L771）值得注意：任何异常都会先触发 `callback.on_error(exc)`，然后**非优雅**关闭（不等异步簿记、不发额外集合通信——各 rank 已处于不一致状态，再通信可能死锁），最后重新抛出。

**_fit_epoch() 主体**。[trainer.py:L1680-L1759](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1680-L1759)：逐段对照 4.2.2 的流程图。L1691-L1699 是簿记自增——注意 token 数和 FLOPs 都取「全局」口径（跨所有 DP rank 求和后的值），由数据加载器和 train module 分别提供；L1701-L1706 是 `steps_to_skip` 判定；L1708-L1734 是钩子与计算的最核心交错段（下面单独引）；L1736-L1737 按 `metrics_collect_interval`（默认 5，[trainer.py:L213-L230](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L213-L230)）批量下发指标；L1743-L1747 在步末检查停训。轮末 L1754-L1759：先触发 `post_epoch()` 钩子，**然后**才 `epoch += 1` 并 `data_loader.reset()`——这个顺序正是 4.1 中「epochs 用严格大于」的原因。

**一个 step 的钩子交错段（本讲最重要的一段代码）**。[trainer.py:L1708-L1734](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1708-L1734)：

```python
for callback in self._iter_callbacks():
    callback.pre_step(batch)          # ④ 之前：前向之前

if should_skip:
    log.warning(...)                  # 跳过整段计算
else:
    self.train_module.train_batch(batch)   # ④ 前向 + 反向
    ...
    for callback in self._iter_callbacks():
        callback.pre_optim_step()     # ⑤ 之前：优化器之前

    self.train_module.optim_step()         # ⑤ 优化器更新
    self.train_module.zero_grads()         # ⑥ 清梯度

for callback in self._iter_callbacks():
    callback.post_train_batch()       # ★ checkpoint 保存点

for callback in self._iter_callbacks():
    callback.post_step()              # ★ 评测触发点
```

**取 batch 的生成器**。[trainer.py:L1647-L1658](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1647-L1658)：`_iter_batches()` 把 `pre_load_batch()` 钩子放在每次 `next(data_iterator)` 之前，数据耗尽（`StopIteration`）时安静退出、结束本轮。

**checkpoint 的真实位置**。训练循环里没有任何 `save_checkpoint` 调用，但它发生在 [checkpointer.py:L282-L310](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/checkpointer.py#L282-L310) 的 `CheckpointerCallback.post_train_batch()` 里：按 `save_interval` / `fixed_steps` / `ephemeral_save_interval` 判断当前 step 是否该存盘。同理，评测由 [evaluator_callback.py:L113-L116](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/evaluator_callback.py#L113-L116) 的 `post_step()` 触发。钩子签名全集见 [callback.py:L77-L171](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/callback.py#L77-L171)。

#### 4.2.4 代码实践

1. **实践目标**：不看任何总结，仅凭源码列出「一个训练 step 内所有关键阶段的先后顺序 + 回调钩子位置」，并验证 4.2.2 的流程图。
2. **操作步骤**：
   - 打开 [trainer.py:L1680-L1759](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1680-L1759)（`_fit_epoch`），从上到下逐行读，每次遇到以下三类语句就记录一行：(a) `self.train_module.*` / `self.data_loader.*` 的调用，(b) `callback.<钩子名>` 循环，(c) `self.global_step` 等簿记更新。
   - 制作一张两列表：左列「阶段」（数据获取 / 簿记 / 前向+反向 / 优化器 step / 清梯度 / checkpoint / 评测 / 指标下发 / 停训检查），右列「触发者与行号」。其中「checkpoint」「评测」两行的触发者应填某个回调的某个钩子——去 [checkpointer.py:L282](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/checkpointer.py#L282) 与 [evaluator_callback.py:L113](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/evaluator_callback.py#L113) 确认是哪个钩子。
   - 交叉验证：在本仓库根目录执行 `grep -n "callback\." src/olmo_core/train/trainer.py | grep -v "_iter_callbacks\|callback.trainer\|run_bookkeeping"`（或用编辑器搜索 `for callback in`），数一数训练循环内出现的钩子调用点，与你的表核对是否一个不漏。
3. **需要观察的现象**：你独立列出的顺序表。
4. **预期结果**：与 4.2.2 的流程图一致，特别是这几个「反直觉」点——`pre_step` 在簿记自增**之后**、`pre_optim_step` 夹在前向反向与优化器 step**之间**、checkpoint 与评测不在循环正文里而在 `post_train_batch` / `post_step` 钩子内、指标下发按 interval 批量而非每步。待本地验证（以你读到的源码为准）。

#### 4.2.5 小练习与答案

**练习 1**：`train_batch()` 和 `optim_step()` 是两个独立调用而不是合在一起，这种拆分让哪些设计成为可能？

**答案**：`pre_optim_step()` 钩子可以插在前向反向之后、优化器更新之前——例如在更新前检查梯度范数、记录梯度统计；同时跳步机制（`steps_to_skip`）可以只执行 `train_batch` 而跳过 `optim_step`，或像 dry-run 那样以特殊模式跑前向反向。前向反向（计算密集、产生梯度）与参数更新（读梯度、动优化器状态）本就是两类操作，拆开后各自可被介入。

**练习 2**：如果把 `epoch += 1` 移到 `post_epoch()` 钩子**之前**，会破坏什么？

**答案**：`Duration.epochs(n)` 的判定依赖「epoch 在整轮结束后才自增」这一不变量（`due()` 用 `epoch > value`）。提前自增会让 `post_epoch()` 钩子看到的 `trainer.epoch` 已经是下一轮编号；更严重的是，若在最后一个 batch 后、钩子前自增，停训判定（`epoch > value`）可能在轮内最后一步之前就提前为真（配合 L1743 的步末检查，取决于检查时机），导致少训 batch。源码选择「先钩子、后推进」保持语义干净。

**练习 3**：为什么 `_sort_callbacks()` 的注释强调回调顺序必须「跨进程组一致」？

**答案**：多个回调内部会做分布式集合通信（如指标归约、取消检查的 all_reduce）。若不同 rank 上回调执行顺序不同，rank A 先发 `all_reduce(SUM)` 而 rank B 先发 `all_reduce(MAX)`，两次集合通信的顺序错位即死锁。按 `(priority, 注册顺序)` 全序排序后，所有 rank 以相同顺序执行钩子，通信顺序天然对齐。

### 4.3 指标收集与跨 rank 归约：ReduceType 与异步簿记

#### 4.3.1 概念说明

训练中会产生大量指标：CE loss、PPL、梯度范数、吞吐、checkpoint 耗时……分布式场景下每个 rank 只看到自己那份，要得到有意义的全局值就必须**跨 rank 归约**。但「怎么合并」取决于指标的性质：

- loss 应取各 rank 的**平均**（`mean`）；
- token 吞吐应把各 rank 的数**加起来**（`sum`）；
- checkpoint 保存耗时取**最大值**（`max`）——最慢的 rank 决定整体耗时；
- 梯度范数最特殊：每个 rank 算的是局部范数 \(\|g_r\|\)，全局范数是

  \[
  \|g\| = \sqrt{\sum_{r=1}^{W} \|g_r\|^2}
  \]

  直接平均局部范数是错的，必须先平方、求和、再开方——这正是 `ReduceType.l2_norm` 存在的原因（[u5-l3](u5-l3-skip-step-and-grad-debug.md) 讲梯度裁剪时会用到它）。

这些语义由 [common.py:L131-L155](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/common.py#L131-L155) 的 `ReduceType` 枚举表达：`mean` / `sum` / `max` / `l2_norm`，另有 `None` 表示「不归约，只看本 rank」。

另一组相关枚举是 `MetricMergeStrategy`（[common.py:L158-L195](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/common.py#L158-L195)）：它管的是**同一个 step 内同名指标被记录两次**时怎么办（警告并保留旧值 / 取新 / 取旧 / 求和 / 平均 / 取最大 / 取最小），默认 `warn`。

还有一个工程概念：**异步簿记（async bookkeeping）**。指标归约是集合通信 + host-device 同步，若在主训练线程做，GPU 会停下来等 CPU。Trainer 的方案是：如果有独立的 CPU 后端（如 gloo），就新建一个专用进程组，把归约丢到后台线程做，GPU 继续训下一步。

#### 4.3.2 核心流程

指标的一生：

```
产生     train_batch() 内部调用 trainer.record_metric("train/CE loss", loss, reduce_type=mean)
          └─ 存入 self._metrics[global_step][name]（张量，仍可能在 GPU 上）
缓存     最多攒 metrics_collect_interval（默认 5）步
下发     _log_metrics()：
          ├─ move_metrics()：把所有攒下的张量一次性搬到簿记设备（CPU）
          ├─ check_metrics_consistent()：各 rank 的「可归约指标集合」是否一致
          └─ run_bookkeeping_op(reduce_metrics, ..., cb=_check_and_pass_on_metrics)
               ├─（异步模式）后台单线程执行；否则内联执行
               ├─ reduce_metrics()：
               │    ├─ 非 distributed：直接 .item() 返回
               │    └─ distributed：按 reduce 类型分组 → 拼成两个二维张量
               │         → 一次 all_reduce(SUM) + 一次 all_reduce(MAX) → 后处理
               └─ 完成后回调 _check_and_pass_on_metrics()：
                    ├─ CE loss 非有限（NaN/Inf）→ 抛 RuntimeError 终止训练
                    ├─ CE loss < 10 → 附加 PPL = exp(ce_loss)
                    └─ 对每个 step（按序）：callback.pre_log_metrics() → callback.log_metrics()
消费     ConsoleLogger / WandB / Comet 等回调把最终 float 写到各自目的地
```

两个效率设计值得体会：

1. **攒批 + 两次 all_reduce 归约全部指标**：`reduce_metrics()` 不逐指标通信，而是把多个 step × 多个指标的值堆叠成矩阵，`sum/max` 各做**一次** `dist.all_reduce`（[utils.py:L314-L325](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/utils.py#L314-L325)）。注意 `mean` 实际是借 `SUM` 实现的：先把每个值除以 world size 再求和（[utils.py:L312](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/utils.py#L312)），等价于平均；`l2_norm` 借 `SUM` 对平方值求和，事后开方（[utils.py:L342-L344](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/utils.py#L342-L344)）。
2. **指标不一致的自适应**：流水线并行下各 rank 拥有的指标集合可能不同。`check_metrics_consistent()` 用 `all_gather_object` 对比各 rank 的可归约指标名集合（[utils.py:L190-L199](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/utils.py#L190-L199)）；不一致时缺位补 0、`mean` 改除以「实际拥有该指标的 rank 数」而非 world size（[utils.py:L337-L344](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/utils.py#L337-L344)），保证平均值不被缺席的 0 拉低。

#### 4.3.3 源码精读

**记录入口**。[trainer.py:L1244-L1301](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1244-L1301) 的 `record_metric()`：按 `namespace` 拼接指标名（如 `"train/CE loss"`）、把 Python 数值转成张量（入口处的 `_metric_value_to_tensor`，[trainer.py:L89-L97](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L89-L97)，会把实数统一成 float64 以防 token 计数这类大整数溢出/丢精度）、按 `merge_strategy` 处理重名，并**强制同一指标名多次记录时 reduce_type 必须一致**（L1296-L1301，不一致直接抛错——归约语义混乱比崩溃更难排查）。专用的 `record_ce_loss()`（[trainer.py:L1303-L1309](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1303-L1309)）只是往 `record_metric` 传固定名字 `"train/CE loss"`（常量定义在 [common.py:L11-L14](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/common.py#L11-L14)）。

**攒批下发**。[trainer.py:L1598-L1632](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1598-L1632) 的 `_log_metrics()`：先用 `move_metrics` 把所有待归约张量**一次性**搬到簿记设备（注释说明：host-device 同步不可避免，但宁可早做一次、剩余处理放后台线程），清空 `_metrics` 缓存，做一次一致性检查（结果缓存在 `_metrics_consistent`，只查一次），最后把归约工作交给 `run_bookkeeping_op`，完成后的回调链到 `_check_and_pass_on_metrics`。

**归约实现**。[utils.py:L202-L348](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/utils.py#L202-L348) 的 `reduce_metrics()`：单进程时直接 `.item()`；分布式时按 4.3.2 所述分组堆叠、两次 all_reduce、CPU 化、后处理还原 `mean`（除以 world size 或实际拥有数）与 `l2_norm`（开方）。`reduce_type=None` 的指标不参与通信，保留本 rank 原值（[utils.py:L281-L283](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/utils.py#L281-L283)）。

**质检与转发**。[trainer.py:L1634-L1645](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1634-L1645)：归约完成后，CE loss 若非有限值立即抛错（数值发散的第一现场）；否则 \( \text{PPL} = e^{\text{CE loss}} \)（仅在 loss < 10 时计算，避免溢出）；随后**按 step 升序**逐个触发 `pre_log_metrics`（可原地增删改指标 dict）和 `log_metrics`。注意 log 的可能是**几步之前**攒下的指标——这就是 `metrics_collect_interval` 文档里「每步都会 log，只是会延迟」的含义（[trainer.py:L213-L230](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L213-L230)）。

**异步簿记调度器**。[trainer.py:L1456-L1557](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1456-L1557) 的 `run_bookkeeping_op()`：可用异步时，涉及分布式通信的操作提交到**单线程**线程池（保证集合通信严格串行、不与主线程的训练通信交错竞态），纯本地操作进多线程池；每个操作有软超时告警；`_join_bookkeeping_ops()`（[trainer.py:L1559-L1571](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1559-L1571)）在存 checkpoint、关闭前阻塞等全部完成。取消检查 `_check_if_canceled`（[trainer.py:L1573-L1596](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1573-L1596)）是它的一个典型用户：用 `all_reduce(MAX)` 广播「是否有 rank 请求取消」，再从该 rank broadcast 取消原因——整个检查在簿记进程组上后台完成，不打扰训练。

#### 4.3.4 代码实践

1. **实践目标**：亲手调用一次归约函数，验证「单进程模式下 `reduce_metrics` 不做任何通信、直接返回浮点值」。
2. **操作步骤**：运行以下脚本（示例代码，非项目原有代码；无需 GPU、无需分布式启动）：

   ```python
   import torch
   from olmo_core.train import ReduceType
   from olmo_core.train.utils import reduce_metrics

   metrics = {
       1: {"train/CE loss": torch.tensor(3.2), "throughput/tokens": torch.tensor(1024)},
       2: {"train/CE loss": torch.tensor(2.9), "throughput/tokens": torch.tensor(2048)},
   }
   reduce_types = {"train/CE loss": ReduceType.mean, "throughput/tokens": ReduceType.sum}
   out = reduce_metrics(metrics, reduce_types, torch.device("cpu"))
   print(out)
   ```

3. **需要观察的现象**：输出的嵌套 dict 结构（外层 key 是 step 号，内层是指标名到 float 的映射）。
4. **预期结果**（依据 [utils.py:L213-L217](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/utils.py#L213-L217) 推导）：非分布式分支直接 `value.item()`，得到 `{1: {'throughput/tokens': 1024.0, 'train/CE loss': 3.2}, 2: {...}}`（内层顺序按指标名排序）。注意此时 `mean`/`sum` 语义不生效——没有别的 rank 可归约。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ReduceType.mean` 不单独设计一种通信，而是「除以 world size 后走 SUM」？

**答案**：一次 `all_reduce` 只能带一个算子。Trainer 把所有 step × 所有指标拼成矩阵后，SUM 类（mean/sum/l2_norm 的平方）和 MAX 类各做**一次** `all_reduce`（[utils.py:L314-L325](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/utils.py#L314-L325)）。mean 预先除以 world size（[utils.py:L312](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/utils.py#L312)）即可搭 SUM 的便车，把通信次数从「每指标一次」降到「总共两次」。

**练习 2**：每个 rank 各自算出局部梯度范数 3.0 和 4.0（world size = 2），错误做法（取 mean）和正确做法（l2_norm）的结果各是多少？

**答案**：mean 得 3.5；l2_norm 得 \(\sqrt{3^2 + 4^2} = \sqrt{25} = 5\)。只有 5 才是全局梯度的真实 L2 范数。

**练习 3**：为什么 `record_metric` 对「同一指标名换 reduce_type」直接抛 `RuntimeError`，而不是以最新一次为准？

**答案**：归约类型决定了该指标在集合通信中的分组与后处理方式。若两次记录类型不同，各 rank 缓存里的分组就可能对不齐（rank A 放进 SUM 组、rank B 放进 MAX 组），`all_reduce` 直接错位死锁或产出无意义数值。这是「快速失败」设计：宁可立刻崩，也不要产生难以排查的静默错误。

### 4.4 TrainerConfig 与 LoadStrategy：组装与断点续训策略

#### 4.4.1 概念说明

`TrainerConfig`（[config.py:L25-L55](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/config.py#L25-L55)）是「一切皆 Config」模式（[u2-l1](u2-l1-config-base-class.md)）在训练器上的落地：字段与 `Trainer` 一一对应（`save_folder`、`max_duration`、`load_strategy`、`callbacks`……），另配 `build(train_module, data_loader)` 工厂。它的存在让 `Trainer` 实例可以完全由 YAML/命令行描述——这正是官方脚本能用 `--trainer.max_duration.value=1000` 覆盖训练时长的基础。

`LoadStrategy` 回答的问题是：`fit()` 开始时**要不要**从 checkpoint 恢复？

- `if_available`（默认）：有就恢复、没有就从头训——适合「可被抢占、随时重启」的生产环境；
- `always`：必须恢复，找不到就抛 `FileNotFoundError`——适合「绝不允许静默从头训」的严肃续训（从头重训 4T token 的代价不可接受）；
- `never`：从不恢复——适合全新实验、或故意从随机初始化开始的对照。

与之配套的两个开关 `load_trainer_state` / `load_optim_state`（[trainer.py:L201-L211](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L201-L211)）控制恢复的**内容**：trainer 状态（数据加载进度、RNG、回调状态）与优化器状态（动量等）可分别取舍。

#### 4.4.2 核心流程

`fit()` 开头的恢复决策（[trainer.py:L696-L728](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L696-L728)）：

```
load_strategy == never        → 什么都不做
否则：
  1. 先看 save_folder（自己上次留下的）→ 有则恢复，且强制加载 trainer 状态 + 优化器状态
     （save_folder 的语义就是"接着自己上次的跑"，缺状态等于白恢复）
  2. save_folder 没有 → 再看 load_path（从别处迁来的）→ 有则按 load_trainer_state/load_optim_state 恢复
  3. 两处都没有：
       always       → FileNotFoundError
       if_available → 打 warning，从头训练
```

`TrainerConfig.build()` 的组装顺序（[config.py:L147-L218](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/config.py#L147-L218)）：

```
build(train_module, data_loader, ...):
 1. self.as_dict(exclude_none=True, recurse=False)   # Config → kwargs 字典
 2. dp_process_group 未给则取 train_module 的
 3. work_dir 未设：save_folder 是本地目录就用它，是 URL 则落到系统临时目录
 4. checkpointer 配置补齐（save_overwrite / work_dir 继承 trainer 级设置）后 build
 5. callbacks 字典拆两半：Callback 实例 直接用；CallbackConfig 延后
 6. 构造 Trainer(...)（其 __post_init__ 会补默认回调、排序、校验）
 7. 逐个 build 剩余的 CallbackConfig 并 trainer.add_callback()
```

`Trainer.__post_init__`（[trainer.py:L331-L415](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L331-L415)）做一堆「构造即校验」：本地 `save_folder` 必须在共享文件系统上（否则多 rank 各写各的）；自动补齐四个默认回调（console_logger / checkpointer / speed_monitor / garbage_collector，[trainer.py:L357-L368](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L357-L368)）；给每个回调挂上 `trainer` 反指针并按优先级排序；校验数据加载器的 DP 配置与当前进程组一致（[trainer.py:L396-L410](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L396-L410)）；最后 `train_module._attach_trainer(self)` 让 train module 也能反向访问 trainer（记录指标时正需要）。

#### 4.4.3 源码精读

**TrainerConfig 字段**。[config.py:L34-L55](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/config.py#L34-L55)：`save_folder` 是唯一必填项；`max_duration` 默认 `Duration.epochs(1)`；`metrics_collect_interval` 默认 5；`callbacks` 是 `Dict[str, Callback]`——注意值既可以是回调实例也可以是 `CallbackConfig`，`build()` 里再分开处理（这是「可序列化的回调」与「现成回调对象」的折中设计）。`with_callback()`（[config.py:L76-L95](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/config.py#L76-L95)）以函数式风格返回**新**配置（深拷贝回调表），不改动原对象，方便官方脚本在一份基础配置上派生变体。

**LoadStrategy 三值**。[common.py:L108-L128](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/common.py#L108-L128)：每个取值的 docstring 就是一句行为契约（if_available「找不到就从头训」、always「找不到就报错」、never「即使有也不加载」）。

**fit() 中的恢复决策代码**。[trainer.py:L696-L728](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L696-L728)：注意 L705-L707 从 save_folder 恢复时**显式传 `load_trainer_state=True, load_optim_state=True`**，无视用户开关——注释写明 save_folder 专用于「继续被中断的运行」；而 `load_path` 那条路径走用户开关的默认值（L710-L712）。`maybe_load_checkpoint`（[trainer.py:L1130-L1162](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1130-L1162)）里还有个小细节：是否存在 checkpoint 只由 rank 0 探测再 `broadcast_object`，避免每个 rank 都去打 S3。

**Trainer 状态里存了什么**。[trainer.py:L986-L1000](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L986-L1000) 的 `state_dict()`：步数、token 数、petaflops、`max_steps`、数据加载器状态、epoch、**全局 world size**、各 RNG 状态（Python/numpy/torch/CUDA，由 [utils.py:L37-L94](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/utils.py#L37-L94) 的 `EnvRngStates` 采集）以及每个回调各自的状态。恢复侧 `load_state_dict`（[trainer.py:L1002-L1047](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1002-L1047)）有个诚实的安全阀：**仅当 checkpoint 里的 world size 与当前一致才恢复 RNG 状态**（L1037-L1047）——从 8 卡 checkpoint 扩到 16 卡续训时，各 rank 的随机流对应关系已失效，恢复只会引入不可复现的偏差，不如不恢复并明说。断点续训的完整闭环（含数据加载器指纹校验、LoadStrategy 实操）是 [u6-l4](u6-l4-checkpointing.md) 的主题。

#### 4.4.4 代码实践

1. **实践目标**：熟悉 `TrainerConfig` 的默认值，并建立 `LoadStrategy` 的行为决策表。
2. **操作步骤**（示例代码，非项目原有代码）：

   ```python
   from olmo_core.train import Duration, LoadStrategy, TrainerConfig

   cfg = TrainerConfig(save_folder="/tmp/my-run")
   print("max_duration           :", cfg.max_duration)
   print("load_strategy          :", cfg.load_strategy)
   print("metrics_collect_interval:", cfg.metrics_collect_interval)
   print("cancel_check_interval  :", cfg.cancel_check_interval)
   ```

   然后打开 [trainer.py:L696-L728](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L696-L728)，手绘一张三行决策表：行是 `LoadStrategy.if_available / always / never`，列是「save_folder 有 checkpoint」「save_folder 无、load_path 有」「两处都无」，格子填行为（恢复+强制全套状态 / 恢复+按开关 / 抛 FileNotFoundError / warning 后从头训 / 什么都不做）。
3. **需要观察的现象**：打印出的默认值；以及你填出的 3×3 决策表。
4. **预期结果**（依据 [config.py:L34-L55](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/config.py#L34-L55)）：`max_duration` 为 `Duration.epochs(1)`、`load_strategy` 为 `if_available`、`metrics_collect_interval` 为 5、`cancel_check_interval` 为 25。决策表要点：never 行全空；if_available 与 always 仅在「两处都无」时不同（warning 从头训 vs 抛错）；凡从 save_folder 恢复必带 trainer+optim 状态。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`build()` 为什么把 `CallbackConfig` 的构建放在 `Trainer(...)` 构造**之后**，而不是先把所有回调 build 好再传进去？

**答案**：`CallbackConfig.build(trainer)` 需要拿到已构造的 `Trainer` 实例作为参数（[config.py:L213-L216](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/config.py#L213-L216)）——很多回调（如 evaluator）构建时要访问 trainer 的设备、save_folder 等上下文。所以顺序只能是：先用普通回调构造 Trainer → 再逐个 build 配置型回调并 `add_callback()`（其中会补挂 trainer 指针、重排序、触发 `post_attach`）。

**练习 2**：从 save_folder 恢复时为什么无视 `load_optim_state=False` 强制加载优化器状态？

**答案**：save_folder 的语义是「本运行自己的续跑现场」（[trainer.py:L702-L707](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L702-L707)）。若只恢复模型权重而不恢复优化器动量，AdamW 的二阶矩要重新热身、学习率预热语义也被破坏，续跑曲线会跳变——这几乎必然不是用户想要的。想「只拿权重」，正确做法是走 `load_path` 并配开关，代码里甚至会在你试图对 save_folder 关开关时发 warning（[trainer.py:L1075-L1085](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1075-L1085)）。

**练习 3**：为什么 `state_dict()` 要存全局 world size，而恢复 RNG 时又只在 world size 一致时才恢复？

**答案**：存 world size 是为了**将来恢复时能校验**。RNG 状态是按 rank 采集的（每 rank 各一份），8 rank 的随机流与 16 rank 的切分方式没有正确对应关系；校验不一致就不恢复（[trainer.py:L1037-L1047](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1037-L1047)），把「扩卡续训无法精确复现随机性」这一事实显式化，而不是悄悄给出错误的随机流。

## 5. 综合实践

**任务：产出一份「带钩子标注的 step 执行顺序图」，并回答三个钉子问题。**

这是本讲规格指定的主实践，把四个模块串起来：

1. **读**。通读 [trainer.py:L687-L782](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L687-L782)（`fit`）、[trainer.py:L1680-L1759](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1680-L1759)（`_fit_epoch`）、[trainer.py:L1647-L1658](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1647-L1658)（`_iter_batches`），画出一张从 `fit()` 进入、经 dry-run、到单个 step、再到 epoch 收尾的完整时序图（或编号列表），**每个回调钩子用 `→ [钩子名]` 标注在两条计算语句之间**。
2. **查**。回答三个「钉子问题」（都要求给出源码行号）：
   - checkpoint 实际保存在哪一行触发的哪个钩子里？（提示：不在 `trainer.py` 里，见 [checkpointer.py:L282](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/checkpointer.py#L282) 附近）
   - 评测在哪个钩子里触发？（见 [evaluator_callback.py:L113](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/evaluator_callback.py#L113)）
   - `Duration.steps(100)` 的训练会在第几个 step 结束后停？停在哪一行被判定？（追踪 `training_complete` → `due()`，以及 `_fit_epoch` 里的检查点）
3. **验**。用 4.2.4 的 grep 交叉验证钩子调用点数量；再用 4.1.4 的 `Duration` 脚本验证边界语义。
4. **预期产出**：一张顺序图 + 三个问题的行号级答案。参考答案即 4.2.2 的流程图与 4.2.3 的源码精读段：checkpoint 在 `post_train_batch` 钩子内（[checkpointer.py:L282-L310](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/checkpointer.py#L282-L310)），评测在 `post_step` 钩子内，`Duration.steps(100)` 在第 100 步末尾由 [trainer.py:L1743-L1747](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1743-L1747) 的 `training_complete` 检查（内部经 [trainer.py:L452-L453](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L452-L453) 调 `due()`，`step >= 100` 为真）终止。待本地验证。

## 6. 本讲小结

- **Trainer 是编排者**：`fit()` 只负责节奏（簿记、停训判定、钩子调度），前向反向优化全部委托 `train_module`，checkpoint 与评测则藏在 `CheckpointerCallback.post_train_batch` / `EvaluatorCallback.post_step` 两个钩子里——训练循环本身可插拔。
- **一个 step 的顺序**：`pre_load_batch` → 取 batch → 簿记自增 → 跳步判定 → `pre_step` → `train_batch`（前向+反向）→ `pre_optim_step` → `optim_step` → `zero_grads` → `post_train_batch`（checkpoint ★）→ `post_step`（评测 ★）→ 按 interval 下发指标 → 停训检查。
- **Duration 三单位、一处陷阱**：steps/tokens/epochs 各有适用场景；epochs 用 `epoch > value` 严格大于判定（epoch 在轮末才自增）；换算成总步数是学习率调度的前提，batch 总数未知时禁止用 epochs；`hard_stop` 提前刹车而不动 LR 曲线。
- **指标走「攒批 + 两次 all_reduce」管线**：`record_metric` 按 step 缓存张量 → 每隔 `metrics_collect_interval` 步搬到 CPU → `reduce_metrics` 把全部 step×指标拼矩阵，SUM 与 MAX 各一次通信 → NaN 检查、PPL 附加 → 按序回调 `log_metrics`；`ReduceType`（mean/sum/max/l2_norm/None）决定合并语义，且同一指标的类型不许变。
- **异步簿记保吞吐**：有 CPU 后端时归约、取消检查等集合操作走专用进程组 + 后台线程池（分布式操作进单线程池防竞态），host-device 同步集中且提前。
- **LoadStrategy 是恢复语义的契约**：save_folder 恢复必带 trainer+optim 状态；`always` 找不到即报错、`if_available` 从头训、`never` 全不碰；RNG 状态只在 world size 一致时恢复。

## 7. 下一步学习建议

- **[u6-l2 TransformerTrainModule：前向、反向与并行设置的汇聚点](u6-l2-transformer-train-module.md)**：本讲反复出现的 `train_batch()` / `optim_step()` / `zero_grads()` 三个委托调用的另一端就是它——看看前向反向、梯度裁剪与 DP/TP/PP 并行配置如何在一个类里汇合。
- **[u6-l3 回调系统：如何扩展训练循环](u6-l3-callback-system.md)**：本讲只标了钩子位置；下一讲逐个讲解 `Callback` 全部钩子、优先级机制与内置回调，并带你写自己的回调挂进 `post_step`。
- **[u6-l4 Checkpoint 体系：保存、恢复与断点续训](u6-l4-checkpointing.md)**：把本讲 4.4 的 LoadStrategy 放到实战里，完成一次「训练中断 → 改配置续训 → 曲线无缝衔接」的完整闭环。
- **随手对照**：重读 [u5-l2](u5-l2-lr-schedulers.md) 中调度器的 `t_max`，确认你能从 `max_duration` 一路指到 `Trainer.max_steps` 再到余弦曲线的 horizon——这条依赖链是大模型训练配置里最容易配错的地方之一。

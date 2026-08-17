# 指标聚合与训练日志：metrics 与 training_logger

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 `add_metric` 的 **num/den（分子/分母）累积模式**：为什么"先各自累积、flush 时全局做 `sum(num)/sum(den)`"比"每卡先各自算比值再平均"更正确。
2. 说清一次训练中 **loss、grad_norm、lr、进度和 ETA** 分别在哪个环节被记录、经过哪些函数、最后写到哪。
3. 独立写一个最小脚本把假指标写成 **TensorBoard 事件文件**，并用 `tensorboard --logdir` 打开找到曲线。

本讲是第 3 单元（训练框架）的收官：前几讲讲了训练的"骨骼"（初始化、主循环、分布式、优化器、检查点），本讲讲训练的"眼睛"——你怎么知道训练是否健康。

## 2. 前置知识

- **指标（metric）与损失（loss）的区别**：loss 是要 `.backward()` 的张量，参与梯度计算；metric 是只读的监控数值，`detach` 之后只进日志、不进计算图。`train/loss` 恰好两者都是——它是 loss 的 detach 副本。
- **集合通信（collective）**：`dist.all_reduce` 会把所有 rank（进程/卡）上的同一个张量求和后写回每个 rank；`dist.all_gather_object` 则把每个 rank 的 Python 对象收集成列表。关键性质：**所有 rank 必须以相同顺序调用**，否则死锁。这是理解 `flush()` 设计的钥匙。
- **日志窗口（logging window）**：不是每个 optimizer step 都写日志。默认 `logging_steps=10`（见 [config/dspark/dspark_qwen3_4b.py:48](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L48)），即每 10 个 optimizer step 汇总一次，两次 flush 之间就是一个"日志窗口"。
- **TensorBoard**：PyTorch 自带的可视化工具。`SummaryWriter.add_scalar(tag, value, step)` 向事件文件追加一个点，`tensorboard --logdir <目录>` 启动网页界面画曲线。`requirements.txt` 已固定版本 `tensorboard==2.20.0`（[requirements.txt:8](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/requirements.txt#L8)）。
- **加权平均 vs 比值的平均**：设 rank 0 有 1000 个监督 token、正确 500 个；rank 1 只有 10 个、正确 9 个。全局正确率应为 \( (500+9)/(1000+10) \approx 50.4\% \)，而"先各自算比率再平均"得 \( (50\% + 90\%)/2 = 70\% \)。后者会被小样本 rank 严重带偏——这就是 num/den 模式存在的理由。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲角色 |
| --- | --- | --- |
| [deepspec/utils/metrics.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/metrics.py) | 指标累积器：`add_metric` 记录、`flush` 跨 rank 聚合并清空 | 核心精读 |
| [deepspec/utils/training_logger.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/training_logger.py) | 日志会话：init / start_session / on_optimizer_step / close，含 ETA 推算与终端打印 | 核心精读 |
| [deepspec/trainer/base_trainer.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py) | 训练框架：调用 training_logger 的四个时机 | 调用方 |
| [deepspec/modeling/dspark/loss.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/loss.py) | DSpark 损失：产出 `train/loss`、`train/ce_loss`、`train/accept_rate@k` 等 | 指标生产者示例 |
| [deepspec/modeling/dspark/common.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/common.py) | DSpark 公共层：产出锚点统计指标 | 指标生产者示例 |
| [deepspec/modeling/eagle3/loss.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/loss.py) | Eagle3 损失：产出 `train/accuracy@k` 等 | 指标生产者示例 |
| [deepspec/utils/distributed.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/distributed.py) | `is_global_main_process` / `print_on_global_main` | 辅助 |

## 4. 核心概念与源码讲解

### 4.1 add_metric 聚合

#### 4.1.1 概念说明

`metrics.py` 解决的问题是：**8 张卡、每个日志窗口内几十次前向，如何得到一个数学上正确的全局指标值？**

它的答案分三步：

1. **累积（add_metric）**：每个 rank 在每次前向后，把指标值（张量）append 进模块级的字典 `_metrics`，**不做任何归约**。比值型指标拆成分子和分母两条分别累积。
2. **聚合（flush）**：到日志边界时，先窗口内合并，再跨 rank `all_reduce`，最后算出 `dict[str, float]` 返回。
3. **清空（reset）**：`flush` 在 `finally` 里无条件清空累积器，下一个窗口从零开始。

指标分两种 **kind**：

- **ratio（比值型）**：调用方传 `den=`。flush 时执行全局加权的
  \[ \bar{m} \;=\; \frac{\sum_{r}\sum_{i} \text{num}_{r,i}}{\sum_{r}\sum_{i} \text{den}_{r,i}} \]
  其中 \( r \) 遍历 rank、\( i \) 遍历窗口内的多次记录。这是"按 token/样本数加权"的正确平均。
- **scalar（标量型）**：不传 `den`。窗口内先按 reduction 规则合并（`mean`/`sum` 取窗口内平均、`max`/`min` 取极值、`last` 取最后一次），名字带 `dp_` 前缀的还要再做一步跨 rank 归约。

#### 4.1.2 核心流程

```
每次前向（run_batch 内部，各 rank 独立）
  └─ add_metric("ce_loss", num张量, den=den张量)   # 只是 append，零通信
  └─ add_metric("loss", loss.detach(), reduction="mean")
  └─ add_metric("lr", lr, reduction="last")        # 由 training_logger 调

每个日志边界（global_step % logging_steps == 0，所有 rank 同时到达）
  └─ flush()
       ├─ _assert_schema_consistent()   # all_gather_object 对齐各 rank 的指标清单
       ├─ ratio:  窗口内求和 → all_reduce SUM → sum(num)/sum(den)
       ├─ scalar: _local_reduce(窗口内) → 若 dp_ 前缀再 all_reduce
       └─ finally: reset() 清空累积器
```

reduction 的取值由正则 `^(dp_)?(mean|sum|max|min|last)$` 约束（[deepspec/utils/metrics.py:7](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/metrics.py#L7)），ratio 型强制使用默认的 `dp_sum`。

#### 4.1.3 源码精读

**入口与累积字典。** 模块级的 `_metrics = {}` 就是全部状态，key 是 `f"{tag}/{name}"`（如 `train/loss`），value 是含 `kind`/`reduction`/两条累积列表的字典：

[deepspec/utils/metrics.py:90-L138](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/metrics.py#L90-L138) —— `add_metric` 的完整签名与累积逻辑。要点：

- 装饰器 `@torch.compiler.disable(recursive=False)`：训练模型整体被 `torch.compile` 包过（见 u3-l1），这个装饰器让 Dynamo 不追踪指标记录本身，`recursive=False` 表示只禁这一层函数。指标挂进计算图会阻止梯度释放、还可能触发重编译。
- ratio 分支断言 `reduction == "dp_sum"`，并把 `value`（分子）和 `den`（分母）分别 `_detach_scalar` 后 append 进两个平行列表 `entry["num"]` / `entry["den"]`。
- 断言 `entry["kind"] == ...`、`entry["reduction"] == ...`：同一个名字在两次调用之间不允许换类型或换归约方式，防止拼错名字导致静默污染。

[deepspec/utils/metrics.py:12-L17](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/metrics.py#L12-L17) —— `_detach_scalar`：张量先 `detach()` 再断言 `numel() == 1`（只收标量），Python 数字则包成 float32 张量。这就是"指标脱离计算图"的边界。

**flush：两段式归约。**

[deepspec/utils/metrics.py:141-L166](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/metrics.py#L141-L166) —— `flush` 主体。ratio 分支先把窗口内所有分子 stack 求和、分母 stack 求和，各自 `_reduce_dp_value(..., "sum")` 跨 rank 求和，最后 `_safe_div` 相除。`finally: reset()` 保证即使中途断言失败，累积器也不会带着脏数据进入下一个窗口。

[deepspec/utils/metrics.py:20-L24](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/metrics.py#L20-L24) —— `_clone_to_reduce_device`：clone 成 float32，并且**NCCL 后端下把 CPU 张量搬到当前 GPU**（NCCL 只能归约 CUDA 张量）。这就是累积阶段可以留在 CPU、flush 阶段才统一搬卡的原因。

[deepspec/utils/metrics.py:46-L58](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/metrics.py#L46-L58) —— `_local_reduce`（窗口内合并）。注意注释点明的语义：`mean`/`sum` 都取 `stacked.mean()`，即**"每次记录的平均值"**——比如 10 个 micro step 各记一次 loss，日志里的是这 10 次的算术平均，而不是求和。`last` 直接取 `values[-1]`。

[deepspec/utils/metrics.py:27-L43](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/metrics.py#L27-L43) —— `_reduce_dp_value`（跨 rank 归约）：`mean` 用 `SUM` 后除以 world_size 实现，`last` 用 `all_gather` 取最后一个 rank 的值（配合窗口内 `last`，即"全局最后一个记录者"）。

[deepspec/utils/metrics.py:72-L81](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/metrics.py#L72-L81) —— `_assert_schema_consistent`：flush 里第一件事就是用 `all_gather_object` 核对**所有 rank 记录的指标名、类型、归约方式、条数完全一致**。这既是集合通信防死锁的保险（保证各 rank 走同样的归约序列），也能把"某张卡少记了一条指标"这类 bug 在第一时间炸出来，而不是静默挂起。

**指标生产者长什么样。** 以 DSpark 为例：

[deepspec/modeling/dspark/loss.py:294-L319](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/loss.py#L294-L319) —— `ce_loss`（ratio：分子是窗口 CE 总量、分母是监督 token 数）、`loss`（scalar，`reduction="mean"`）。注意一个细节：DSpark 日志里的 `train/loss` 用的是 **不带 `dp_` 的 `mean`**，即窗口内本卡平均、最终由 rank 0 落盘——训练用的 backward loss 做了跨卡归一化，日志值只是 rank 0 视角的监控量；而 Eagle3 的同名指标用的是 `dp_mean`（[deepspec/modeling/eagle3/loss.py:451](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/loss.py#L451)），是真正的全局平均。两者都够用，但对比两个算法的 loss 曲线时要知道口径差异。

[deepspec/modeling/dspark/loss.py:192-L204](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/loss.py#L192-L204) —— 逐位置 `accept_rate@{pos_idx}`：分子是位置 \( i \) 被接受的次数和，分母是位置 \( i \) 的有效 token 数。不同 rank 的有效位置数差异巨大（序列长短不同），所以必须走 num/den 全局加权。

[deepspec/modeling/dspark/common.py:210-L221](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/common.py#L210-L221) —— `valid_anchors_abs` / `valid_anchors_ratio`：分母统一是 `sample_count`（本 batch 样本数），得到"每样本平均有效锚点数"这类归一化统计。

#### 4.1.4 代码实践

**实践目标**：用一个纯 Python 小实验验证"sum(num)/sum(den)"与"比值的平均"在数据倾斜时给出截然不同的答案，建立对 num/den 模式的直觉。

**操作步骤**（示例代码，不依赖 GPU）：

```python
# toy_ratio_demo.py —— 示例代码：对比两种聚合口径
ranks = [
    {"num": 500, "den": 1000},   # rank 0：大 batch
    {"num": 9,   "den": 10},     # rank 1：小 batch
]
weighted = sum(r["num"] for r in ranks) / sum(r["den"] for r in ranks)
naive    = sum(r["num"] / r["den"] for r in ranks) / len(ranks)
print(f"num/den 全局加权 = {weighted:.4f}")   # 0.5040
print(f"先算比值再平均   = {naive:.4f}")      # 0.7000
```

**需要观察的现象**：两个数字相差近 20 个百分点。

**预期结果**：加权值 ≈ 0.5040，朴素平均 = 0.7000。直觉上"90% 接受率"只来自 10 个 token 的卡，不该和大样本卡平起平坐——`flush` 采用前者。

**待本地验证**：以上数值可直接手算复核；如运行脚本请确认输出一致。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `add_metric` 的 docstring 特别强调"callers should not pre-divide locally"（不要在调用前自己先做除法）？

**答案**：一旦本地先除了，flush 拿到的就是各 rank 的比值，跨 rank 归约无论求和还是求平均都失去权重信息，等于退化为 4.1.4 里被否定的"朴素平均"。只有分子、分母各自保持可加的"计数语义"，`sum(num)/sum(den)` 才是按 token/样本加权的正确全局比值。

**练习 2**：`add_metric("x", v, reduction="last")` 在一个日志窗口内被调用了 5 次，`flush` 返回什么？如果换成 `reduction="mean"` 又返回什么？

**答案**：`last` 返回第 5 次的值（`values[-1]`，见 [deepspec/utils/metrics.py:48-L49](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/metrics.py#L48-L49)）；`mean` 返回 5 次的算术平均（`stacked.mean()`，见 [deepspec/utils/metrics.py:55-L57](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/metrics.py#L55-L57)）。lr 和 grad_norm 用 `last` 是因为它们是"状态量"，曲线应反映日志边界时刻的最新值；loss 用 `mean` 是因为它是"过程量"，窗口平均更抗噪。

**练习 3**：如果某个 rank 上某个 batch 走了不同分支、少调用了一次 `add_metric("foo", ...)`，会发生什么？

**答案**：`flush` 开头的 `_assert_schema_consistent` 会用 `all_gather_object` 核对每个 rank 的 (名字, 类型, 归约, 条数) 四元组，条数不一致立刻 `AssertionError`（"metric schema mismatch across ranks"），而不是让后续的 `all_reduce` 死锁或在日志里静默错位。

### 4.2 training_logger 会话

#### 4.2.1 概念说明

`metrics.py` 只管"攒数和归约"，不管"什么时候攒满、写到哪里、怎么打印"。`training_logger.py` 在它之上封装了一个**日志会话（session）**的生命周期：

```
init ──► start_session ──► on_optimizer_step × N ──► close
(建 writer)  (记起点、清零)     (累积+按 logging_steps 落盘)   (关 writer)
```

它要回答四个问题：

1. **谁写盘**：只有全局 rank 0（global main process）创建 `SummaryWriter`，避免 8 份重复事件文件。
2. **何时落盘**：`global_step % logging_steps == 0` 时。
3. **lr/grad_norm 为什么不会"漏"**：它们在**每个** optimizer step 都被 `add_metric(..., reduction="last")` 记录，非日志步只是不 flush 而已。
4. **ETA 怎么算**：从会话起点线性外推，会话起点在断点续训后重置。

#### 4.2.2 核心流程

`BaseTrainer` 里的四个调用时机（沿用 u3-l2 的主循环）：

1. `__init__` 末段：`training_logger.init(logging_steps=..., tensorboard_dir=...)`（[deepspec/trainer/base_trainer.py:170-L173](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L170-L173)）。`tensorboard_dir` 来自配置的 `finalize_cfg` 派生（u1-l4 讲过：`BASE_TB_DIR/<project>/<exp>`，各 config 的 `finalize_cfg` 中赋值，如 [config/dspark/dspark_qwen3_4b.py:65](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L65)）。
2. `train()` 进入数据循环前：`start_session(global_step=self.global_step)`（[deepspec/trainer/base_trainer.py:370](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L370)）——注意在 `torch.compile` 与 FSDP 包装之后、循环之前，所以**编译耗时不会被计进 ETA**。
3. 每个 optimizer step 后：`on_optimizer_step(...)`（[deepspec/trainer/base_trainer.py:391-L398](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L391-L398)），紧接 `optimizer.step()`，lr 取自 `self.optimizer.get_learning_rate()`，grad_norm 是上一步 `FSDP.clip_grad_norm_` 的返回值。
4. 训练结束：`clean_up()` 里 `training_logger.close()`（[deepspec/trainer/base_trainer.py:409-L410](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L409-L410)）。

ETA 的线性外推公式（`_print_summary` 内实现）：

\[
T_{\text{remain}} \;=\; T_{\text{elapsed}} \times \frac{S_{\max} - s}{\max(s - s_0,\, 1)}
\]

其中 \( s \) 是当前 `global_step`，\( s_0 \) 是会话起始 step（`_session_start_step`），\( S_{\max} \) 是 `max_train_steps`，\( T_{\text{elapsed}} \) 是会话经过的墙钟时间。断点续训后 \( s_0 \) 很大而 \( T_{\text{elapsed}} \) 很小，ETA 只基于**本次会话**的实测速度，不被历史运行污染。

#### 4.2.3 源码精读

**会话状态与 init。**

[deepspec/utils/training_logger.py:10-L13](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/training_logger.py#L10-L13) —— 四个模块级全局变量：`_writer`、`_logging_steps`、`_session_start_wall`（会话起始墙钟）、`_session_start_step`。全部由模块级函数操作，与 `metrics._metrics` 一样是"进程内单例"风格。

[deepspec/utils/training_logger.py:16-L22](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/training_logger.py#L16-L22) —— `init`：`is_global_main_process()` 为真才 `ensure_dir` 并创建 `SummaryWriter`。非 0 号进程的 `_writer` 保持 `None`，后文所有写盘函数对 `None` 自然跳过——这就是"只有 rank 0 写 TensorBoard"的实现方式（`is_global_main_process` 定义在 [deepspec/utils/distributed.py:34-L35](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/distributed.py#L34-L35)，即 `dist.get_rank() == 0`）。

**会话起点。**

[deepspec/utils/training_logger.py:24-L28](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/training_logger.py#L24-L28) —— `start_session`：先 `reset()` 把上一会话（通常是上次断点前）残留的累积清空，再记录当前墙钟与 `global_step` 作为 ETA 基准。续训场景里 `global_step` 不为 0，这正是 \( s_0 \) 存在的意义。

**每步回调：本讲最核心的 26 行。**

[deepspec/utils/training_logger.py:31-L56](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/training_logger.py#L31-L56) —— `on_optimizer_step`。逐段读：

- 第 40-41 行：**先无条件**记录 `lr` 与 `grad_norm`（`reduction="last"`）。哪怕本步不落盘，这两条也进累积器，保证下次 flush 取到的是"窗口内最后一次"而非恰好落盘那次的值。
- 第 43-44 行：`global_step % _logging_steps != 0` 就 `return None`——非日志步到此为止。
- 第 46 行：`summary = flush()`。**这一行所有 rank 都会执行**（内部有 `all_reduce` 集合通信，缺一个 rank 就死锁），这也是 4.1 强调 schema 一致性的现实原因。
- 第 47-55 行：只有 global main 才 `_write_scalars`（写 TensorBoard）和 `_print_summary`（打终端日志）。flush 得到的 `summary` 同时返回给调用方（`train()` 目前不使用返回值，但接口留好了）。

**终端打印与 ETA。**

[deepspec/utils/training_logger.py:73-L100](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/training_logger.py#L73-L100) —— `_print_summary`：

- 第 84 行：`current_epoch = next_micro_step // micro_batches_per_epoch + 1`——epoch 由**微步**（micro step，u3-l2 的唯一真相源）推导，而非 optimizer step，因为一个 epoch 的边界不一定对齐梯度累积边界。
- 第 86-90 行：实现 4.2.2 的 ETA 公式，分母 `max(completed_session_steps, 1)` 防止第一步除零。
- 第 92-93 行：终端只打 `train/loss` 一项（`f"{...:.4f}"`），完整指标要看 TensorBoard。
- 第 94-100 行：`print_on_global_main` 每行自动加时间戳前缀（见 [deepspec/utils/distributed.py:42-L45](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/distributed.py#L42-L45)），形如：

```
2026-08-17 10:12:03 epoch=1 step=120/9000 loss=2.3187 | elapsed=14.2min | remaining=1049.8min
```

**指标从哪来：一条完整的生产链。** 以 DSpark 为例，[deepspec/trainer/dspark_trainer.py:25-L38](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/dspark_trainer.py#L25-L38) 的 `run_batch` 调 `compute_dspark_loss`，后者内部（4.1.3 引用过的 loss.py / common.py）把 `loss`、`ce_loss`、`accept_rate@k`、锚点统计等逐条 `add_metric`；`train()` 主循环随后调 `on_optimizer_step` 补上 `lr`、`grad_norm` 并在日志步统一 flush。**也就是说：一次 flush 拿到的 summary，混合了损失函数、公共层、日志器三方在同一窗口内记录的全部指标。**

#### 4.2.4 代码实践

**实践目标**：通过源码追踪，预测"非日志步"上发生了什么，检验对 flush 时机的理解。

**操作步骤**：

1. 打开 [deepspec/utils/training_logger.py:31-L56](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/training_logger.py#L31-L56)，假设 `logging_steps=10`，沿 `global_step=1, 2, ..., 11` 逐个走一遍代码。
2. 建一张表，三列：`global_step`、`本步 add_metric 了什么`、`本步是否 flush/写盘`。
3. 回答：step=10 和 step=11 两次 flush 窗口里，`train/grad_norm` 分别反映的是哪一步的值？

**需要观察的现象**：表格里非日志步的"add_metric"列不为空。

**预期结果**：每个 step 都记录了 `lr` 和 `grad_norm`（以及 `run_batch` 内部的一批指标）；只有 step=10、20、... 触发 flush+写盘。step=10 的窗口（step 1-10）里 `grad_norm` 是第 10 步的值，step=11 落在下一个窗口，其 `grad_norm` 要等 step=20 的 flush 才随 `last` 语义被取走。

**待本地验证**：如需实跑确认，可在 `on_optimizer_step` 入口加一行 `print(global_step)`（改完记得还原，不要提交）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `start_session` 里要调用 `reset()`？如果去掉，什么场景会出 bug？

**答案**：断点续训时，进程其实是新拉的，但若同一进程内复用 logger（或未来支持会话内重启循环），上一窗口残留的累积会把旧值混进新窗口的第一次 flush，导致第一条日志失真。`reset()` 保证每个会话从干净的累积器开始（见 [deepspec/utils/training_logger.py:26](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/training_logger.py#L26)）。

**练习 2**：ETA 为什么用"会话起点"而不是"训练起点"做基准？付出的代价是什么？

**答案**：断点续训后无法可靠知道历史运行耗了多久（checkpoint 里不存墙钟时间），且不同会话的机器负载可能不同；用本次会话实测速度线性外推最简单也最贴近当前真实吞吐。代价是会话刚开始时 \( s - s_0 \) 很小、速度估计噪声大，前几条 ETA 会明显抖动，跑稳后才准。

**练习 3**：`on_optimizer_step` 里的 `flush()` 能不能加 `if is_global_main_process():` 前缀来省通信？

**答案**：不能。`flush` 内部的 `_assert_schema_consistent`（`all_gather_object`）与 `_reduce_dp_value`（`all_reduce`）都是集合通信，**所有 rank 必须一起进入**；只让 rank 0 调用会使其余 rank 永远等不到该轮通信，直接死锁。正确做法就是现在这样：全员 flush，仅 rank 0 消费结果。

### 4.3 TensorBoard 写入

#### 4.3.1 概念说明

TensorBoard 侧只有两个函数：`_write_scalars`（写）和 `close`（关）。要点不在代码量，而在三个约定：

1. **tag 即目录**：指标 key 形如 `train/loss`，TensorBoard 界面会按 `/` 分组，所有指标收进一个 `train` 文件夹，与 eval 侧指标（若未来有 `eval/...` tag）天然隔离。
2. **横轴是 optimizer step**：`add_scalar(key, value, global_step)` 用的是 `global_step`（优化器步），不是 micro step。对照曲线时记住：一个点背后是 `logging_steps × gradient_accumulation_steps` 个 micro step。
3. **事件文件落在 `tensorboard_dir`**：由配置的 `finalize_cfg` 派生为 `BASE_TB_DIR/<project_name>/<exp_name>`，与 checkpoint 目录（`~/checkpoints/...`）是两棵树。

#### 4.3.2 核心流程

```
on_optimizer_step (每 rank)
  └─ flush() → summary dict          # 集合通信，全员参与
  └─ if is_global_main_process():
        ├─ _write_scalars(summary, global_step)
        │     └─ for key, value: _writer.add_scalar(key, value, global_step)
        └─ _print_summary(...)       # 终端一行
训练结束
  └─ clean_up() → close() → _writer.close()
```

SummaryWriter 内部有异步写线程，`close()` 负责把缓冲落盘——这就是它必须出现在 `clean_up` 里的原因。

#### 4.3.3 源码精读

[deepspec/utils/training_logger.py:66-L70](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/training_logger.py#L66-L70) —— `_write_scalars`：遍历 flush 返回的 summary，逐条 `add_scalar`。`_writer is None`（非 rank 0 或未传 `tensorboard_dir`）时静默跳过——`init` 的参数 `tensorboard_dir` 允许为 `None`，即"只打终端、不写 TB"的降级模式。

[deepspec/utils/training_logger.py:59-L63](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/training_logger.py#L59-L63) —— `close`：关闭 writer 并把 `_writer` 置回 `None`。

你在界面上会看到的典型曲线（DSpark 训练）：

| tag | 类型 | 生产处 |
| --- | --- | --- |
| `train/loss` | scalar mean | [loss.py:314-L319](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/loss.py#L314-L319) |
| `train/ce_loss`、`train/l1_loss`、`train/confidence_loss` | ratio | [loss.py:294-L313](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/loss.py#L294-L313) |
| `train/accept_rate@0..block_size-1` | ratio | [loss.py:192-L198](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/loss.py#L192-L198) |
| `train/lr`、`train/grad_norm` | scalar last | [training_logger.py:40-L41](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/training_logger.py#L40-L41) |
| `train/valid_anchors_abs` 等 | ratio | [common.py:210-L248](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/common.py#L210-L248) |

其中 `train/lr` 曲线应呈现 u3-l4 讲过的"线性 warmup → cosine 衰减"两段形状，是验证调度器接线的最直接手段。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：不启动任何训练，用一个约 20 行的脚本走通 `add_metric → flush → add_scalar` 全链路，产出真实的事件文件并在 TensorBoard 里看到曲线。

**操作步骤**：

1. 确认依赖已装（仓库根目录）：`python -m pip install -r requirements.txt`（tensorboard==2.20.0 在列）。
2. 在仓库根目录新建 `toy_tb_demo.py`（示例代码，与仓库无关，练习后可删除）：

```python
# toy_tb_demo.py —— 示例代码：metrics + TensorBoard 最小演示
import os
import torch
import torch.distributed as dist
from torch.utils.tensorboard import SummaryWriter

from deepspec.utils.metrics import add_metric, flush

os.environ.setdefault("MASTER_ADDR", "127.0.0.1")   # 单进程也要初始化进程组，
os.environ.setdefault("MASTER_PORT", "29517")       # flush 内部有集合通信
dist.init_process_group("gloo", rank=0, world_size=1)

writer = SummaryWriter("runs/toy_demo")
for step in range(1, 21):
    add_metric("loss", 1.0 / step, reduction="mean", tag="train")      # 标量型
    add_metric("accept_rate", step * 0.4, den=step, tag="train")       # 比值型
    if step % 5 == 0:                                                  # 模拟 logging_steps=5
        summary = flush()
        for key, value in summary.items():
            writer.add_scalar(key, value, step)
        print(step, summary)
writer.close()
```

3. 运行：`python toy_tb_demo.py`。
4. 启动可视化：`tensorboard --logdir runs/toy_demo`，浏览器打开提示的地址（默认 `http://localhost:6006`）。

**需要观察的现象**：

- 终端在 step=5/10/15/20 各打印一次 summary，key 应为 `train/loss` 与 `train/accept_rate`。
- `train/accept_rate` 的值恒为 0.4（每窗口 `sum(step*0.4)/sum(step)` = 0.4），而 `train/loss` 是各窗口内 5 个 `1.0/step` 的平均。
- TensorBoard 里 `train` 分组下出现两条曲线，横轴 5、10、15、20 各一个点。

**预期结果**：`train/loss` 逐窗下降——四个窗口的均值分别为 \( (1+\tfrac12+\tfrac13+\tfrac14+\tfrac15)/5 \approx 0.457 \)、\( \approx 0.129 \)、\( \approx 0.078 \)、\( \approx 0.056 \)；`train/accept_rate` 为水平线 0.4——这正好分别演示了 `mean`（窗口平均）与 ratio（加权比值）两种口径。

**待本地验证**：本脚本未在本环境执行过，以上数值由公式推得，请以本地实际输出为准。若报 NCCL 相关错误，检查是否遗漏 `init_process_group("gloo", ...)`（`flush` 必须有可用后端）。

#### 4.3.5 小练习与答案

**练习 1**：把上面脚本里 `add_metric("accept_rate", step * 0.4, den=step)` 改成先除好再记录：`add_metric("accept_rate", 0.4)`，曲线会有变化吗？

**答案**：数值碰巧仍是 0.4（本例分子分母同倍增长），但语义完全变了：它成了无 den 的标量型，走 `mean` 口径而非全局加权。只要各窗口（或各 rank）的 `den` 总量不同，两种写法就会分叉——4.1.4 已经给出反例。结论：**比值型指标永远传 `den=`**。

**练习 2**：为什么 8 卡训练时 `runs/` 目录下只有一份事件文件，而终端日志也只有一份？

**答案**：`init` 里只有 `is_global_main_process()`（全局 rank 0）创建 `SummaryWriter`，其余进程 `_writer=None`，`_write_scalars` 直接返回；`_print_summary` 内部用的 `print_on_global_main` 同样只在 rank 0 打印。而 `flush()` 因含集合通信必须全员执行——**通信全员、IO 单点**是这个日志系统的分工原则。

**练习 3**：训练到一半 Ctrl+C 后再续训（u3-l5 的 step_latest 机制），TensorBoard 曲线会怎么表现？

**答案**：新旧事件文件写在同一个 `tensorboard_dir`（同一 `<project>/<exp>`），TensorBoard 按横轴 `global_step` 合并展示；由于续训从断点的 `global_step` 继续，曲线在同一横轴上延续而非从 0 重叠。另外 `start_session` 重置了 ETA 基准（4.2 练习 2），终端的 remaining 会以本次会话速度重新估计。

## 5. 综合实践

把本讲三个模块串起来：**模拟一个"带日志会话的迷你训练循环"**。

任务：在 4.3.4 脚本的基础上，不再手写 `flush` 与 `add_scalar`，改用真实的 `training_logger` 走完整会话生命周期：

```python
# toy_session_demo.py —— 示例代码：复用 training_logger 的完整会话
import os
import torch
import torch.distributed as dist

os.environ.setdefault("MASTER_ADDR", "127.0.0.1")
os.environ.setdefault("MASTER_PORT", "29518")
dist.init_process_group("gloo", rank=0, world_size=1)

import deepspec.utils.training_logger as training_logger
from deepspec.utils.metrics import add_metric

MAX_STEPS, LOGGING_STEPS = 25, 5
training_logger.init(logging_steps=LOGGING_STEPS, tensorboard_dir="runs/toy_session")
training_logger.start_session(global_step=0)
for step in range(1, MAX_STEPS + 1):
    add_metric("loss", 2.0 / step, reduction="mean", tag="train")  # 模拟 run_batch 记的指标
    training_logger.on_optimizer_step(
        global_step=step,
        next_micro_step=step * 2,          # 假设梯度累积为 2
        micro_batches_per_epoch=30,
        max_train_steps=MAX_STEPS,
        learning_rate=3e-4 * step / MAX_STEPS,
        grad_norm=1.0 / step,
    )
training_logger.close()
```

要求与检查点：

1. 运行后终端应出现 5 行带时间戳的进度日志（step=5/10/15/20/25），包含 `loss=`、`elapsed=`、`remaining=`。
2. `tensorboard --logdir runs/toy_session`，确认 `train` 分组下有 `loss`、`lr`、`grad_norm` 三条曲线；`lr` 应为单调上升的斜线（模拟 warmup），`grad_norm` 阶梯下降（`last` 语义）。
3. 对照 4.2.3 的源码逐行解释：为什么你只调了 3 个函数（init/start_session/on_optimizer_step），`lr` 和 `grad_norm` 却自动出现在曲线里？（答案在 [training_logger.py:40-L41](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/training_logger.py#L40-L41)：它们由 `on_optimizer_step` 自己记录，不需要调用方操心。）
4. 思考题：把这个脚本复制一份同时起两个进程（world_size=2，rank 0/1 各自 `init_process_group`），两份脚本记录的 `loss` 值故意不同，观察 `flush` 后 rank 0 写出的 `train/loss` 是谁的值？（提示：`reduction="mean"` 不带 `dp_` 前缀，见 4.1.3 对 DSpark `loss` 口径的讨论。）

**待本地验证**：以上脚本为示例代码，未在本环境运行；多进程部分需要正确设置 `MASTER_ADDR/PORT` 与各自 rank，属于进阶选做。

## 6. 本讲小结

- **`add_metric` 只累积、不通信**：指标按 `tag/name` 存进模块级字典，ratio 型分子分母平行累积，scalar 型带 `mean/sum/max/min/last` 窗口内归约语义，`dp_` 前缀额外做跨 rank 归约。
- **num/den 模式是正确性设计而非便利设计**：`flush` 计算 \(\sum \text{num} / \sum \text{den}\) 得到按 token/样本加权的全局比值，规避"各卡先除再平均"在数据倾斜下的偏差；调用方不得预除。
- **flush 是集体操作**：schema 校验 + 归约都要求所有 rank 同时到达，所以 `on_optimizer_step` 里 `flush()` 全员执行、写盘打印仅 rank 0——"通信全员、IO 单点"。
- **lr 与 grad_norm 每个 optimizer step 都被记录**（`reduction="last"`），非日志步只跳过落盘、不跳过累积。
- **ETA 按会话线性外推**：`start_session` 记录会话起点墙钟与 step，续训后以本次会话实测速度估计剩余时间；epoch 由 `next_micro_step` 推导。
- **横轴口径要记牢**：TensorBoard 的横轴是 optimizer step（一个点 = `logging_steps × 梯度累积步` 个 micro step）；DSpark 的 `train/loss` 是 rank 0 本地窗口均值，Eagle3 用 `dp_mean` 全局均值，对比曲线时口径不同。

## 7. 下一步学习建议

至此第 3 单元（训练框架）完结，你已经掌握 BaseTrainer 的全部骨架工程。接下来两条路：

1. **进入第 4 单元（DSpark 建模）**：本讲多次引用的 [deepspec/modeling/dspark/loss.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/loss.py) 和 [deepspec/modeling/dspark/common.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/common.py) 将在 u4-l1、u4-l4 正式精读——届时本讲看到的 `accept_rate@k`、锚点统计指标会随损失函数与锚点采样机制一起讲透。
2. **若想先补评估侧的日志**：可跳到 u6-l1 看评估指标（accept_len、verify_rate）如何汇总，与本讲的训练指标对照。

建议顺手做的巩固：找一次真实（或 u7-l3 小规模）训练产出的 `tensorboard_dir`，用本讲的"指标生产处对照表"（4.3.3）逐条曲线指出它的生产代码位置。

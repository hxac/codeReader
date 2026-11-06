# 训练主循环：train_batch、backward 与 step

## 1. 本讲目标

学完本讲，你应该能够：

1. 画出 `DeepSpeedEngine` 一次「前向 + 反向 + 更新」的内部时序图，说清楚每一步分别在哪个源码函数里发生。
2. 解释 `is_gradient_accumulation_boundary()` 如何决定梯度同步和优化器更新的频率，以及 managed / unmanaged 两种梯度累积模式的区别。
3. 描述 `_backward_prologue` 中新增的 AutoTP 冻结复制参数防御性检查：为什么 unfreeze 之后继续训练会「静默漂移」，引擎为什么改为直接报错。
4. 追踪 fp32 梯度裁剪链路 `clip_fp32_gradients() -> clip_grad_norm_()`，并理解本周期的 MoE 感知改造：专家参数不再对 DP 组求平均，而是在专家并行组上求范数（每个专家只计一次），并用一次世界组 all_reduce 投票保证所有 rank 走同一条集体通信路径。
5. 不依赖 DeepSpeed 托管的循环，自己写出「手动 backward / step」的训练脚本（unmanaged 模式）。

## 2. 前置知识

- **微批次与梯度累积**：显存装不下一次有效 batch 时，把大 batch 切成多个微批次（micro-batch），每个微批次做一次前向/反向，梯度累加，攒够 `gradient_accumulation_steps`（下文简称 gas）个微批次后再做一次优化器更新。u2-l2 已推导过恒等式：有效 batch = micro batch × gas × 数据并行宽度。
- **autograd hook**：PyTorch 允许在张量上挂回调，反向传播经过该张量时回调被触发。DeepSpeed 用它实现「反向开始时先做准备工作」——这就是后面要讲的 backward 序章（prologue）。
- **集合通信**：`all_reduce` 让一组进程互相交换数据并得到同一个聚合结果（求和/求最大值）；`reduce_scatter` 是「聚合 + 分发」的组合。DeepSpeed 里所有分布式通信都经过 `deepspeed.comm`（u3-l2 会展开）。
- **范数与梯度裁剪**：梯度裁剪把梯度向量的 L2 范数限制在 `max_norm` 以内，防止梯度爆炸。数学上就是对整个梯度向量（把所有参数的梯度拼成一个大向量）求范数，再统一乘以系数 `clip_coef = max_norm / (total_norm + 1e-6)`（上限为 1）。
- **MoE 与专家并行**：混合专家模型（MoE）把多个「专家」子网络分摊到不同 rank 上，每个 rank 只持有一部分专家。因此**专家参数在各 rank 之间不是复制关系，而是互不相同的分片**——这一点是理解本讲裁剪改造的关键。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `deepspeed/runtime/engine.py` | 训练引擎本体：`forward`、`backward`、`step`、`_take_model_step`、`_backward_prologue`、`clip_fp32_gradients` 都在这里 |
| `deepspeed/runtime/dataloader.py` | `DeepSpeedDataLoader`（分布式采样、吞吐计时）与 `RepeatingLoader`（无限迭代包装） |
| `deepspeed/runtime/base_optimizer.py` | `ZeROOptimizer` 基类：边界标志的镜像、loss scaler 判定、backward 序章/尾声接口 |
| `deepspeed/runtime/utils.py` | `clip_grad_norm_`（本讲主角之一）、`get_norm_with_moe_layers`、`register_output_backward_hooks` |
| `deepspeed/runtime/pipe/engine.py` | `PipelineEngine.train_batch()`——`train_batch` 这个名字在当前代码里唯一的存活处 |
| `deepspeed/module_inject/auto_tp.py` | `register_replicated_grad_hooks`：冻结参数清单的来源，也是 `_backward_prologue` 防御检查的数据源头 |

## 4. 核心概念与源码讲解

### 4.1 训练主循环状态机：从 train_batch 到 forward/backward/step

#### 4.1.1 概念说明

先澄清一个常见误解：**当前版本的 `DeepSpeedEngine` 上并没有 `train_batch()` 方法**。历史上（v0.3 之前）引擎确实提供过 `engine.train_batch()`，由引擎自己驱动「取数据 → 前向 → 反向 → 更新」的完整循环；v0.3.0 的大重构（commit `e5bbc2e55`，"Sparse attn + ops/runtime refactor"）把它移除了，从此标准写法变成用户自己写循环、显式调用三个方法：

```python
# 示例代码：DeepSpeedEngine 的标准训练循环
for step, batch in enumerate(data_loader):
    loss = engine(batch)            # 等价于 engine.forward(batch)
    engine.backward(loss)           # 反向传播（内部会做 loss 缩放）
    engine.step()                   # 边界处做梯度裁剪 + 优化器更新 + 调度器推进
```

`train_batch` 这个名字只在流水线引擎里保留了下来：`PipelineEngine.train_batch()` 仍然是一次调用吃掉一整个有效 batch 的托管接口（它内部由流水线调度器驱动，详见 u6-l4）。本讲聚焦 `DeepSpeedEngine` 的三步循环。

引擎用三个计数器记录训练进度（`global_steps` = 优化器更新次数、`micro_steps` = 微批次次数、`global_samples` = 累计样本数），它们是理解「一次 step 到底推进了什么」的钥匙。

#### 4.1.2 核心流程

一次完整训练步（managed 模式，gas = 2）的状态机：

```text
┌─ engine(batch) ── forward：module 前向；在输出张量上注册 backward hook
│                    （hook 内藏一个 backward_prologue 闭包）
├─ engine.backward(loss)
│    ├─ loss = loss / gas                    # scale_wrt_gas=True 时
│    ├─ ZeRO 优化器再做 loss scaling          # fp16 场景，见 u4-l1
│    ├─ loss.backward()                      # autograd 开始
│    │    └─（hook 触发）_backward_prologue   # 计时 + 防御检查 + 边界下发
│    └─ _backward_epilogue                   # 梯度 allreduce / reduce_scatter
├─ engine.step()
│    ├─ 边界判定 is_gradient_accumulation_boundary()
│    ├─ 若是边界：_take_model_step
│    │    ├─ 梯度裁剪（fp32 路径 → clip_fp32_gradients）
│    │    ├─ optimizer.step()
│    │    ├─ zero_grad
│    │    ├─ overflow 检查 → lr_scheduler.step()
│    │    └─ global_steps += 1; global_samples += train_batch_size
│    └─ micro_steps += 1                     # 无论是否边界都推进
└─ 重复，直到训练结束
```

注意一个关键不对称：**`step()` 每个 micro-batch 后都要调用一次（对称调用），但优化器更新只发生在边界**。非边界处的 `step()` 只推进 `micro_steps` 并做计时/监控。

#### 4.1.3 源码精读

先看计数器在哪里初始化——[deepspeed/runtime/engine.py:541-545](https://github.com/deepspeedai/DeepSpeed/blob/6d64e1b9746d77ae1c1cd652006cb3bafeb09afe/deepspeed/runtime/engine.py#L541-L545)：引擎在构造时就准备好 `global_steps`、`global_samples`、`micro_steps` 和 unmanaged 模式专用的 `_unmanaged_backward_count`（统计两次 `step()` 之间实际发生了几次 backward，用来按真实微批次数推进样本计数）。

`backward()` 的主体在 [deepspeed/runtime/engine.py:3413-3471](https://github.com/deepspeedai/DeepSpeed/blob/6d64e1b9746d77ae1c1cd652006cb3bafeb09afe/deepspeed/runtime/engine.py#L3413-L3471)，做五件事：

1. 断言必须传入优化器、loss 必须是标量张量（L3426-3429）；
2. unmanaged 模式下给 `_unmanaged_backward_count` 加一（L3438-3439）；
3. `loss = loss / gradient_accumulation_steps()`——把梯度按 gas 预先除掉，这样累加 gas 次后恰好是「平均梯度」；`scale_wrt_gas=False` 可以关掉（L3444）；
4. ZeRO 优化器场景再做 loss scaling（fp16 动态缩放），或走 torch autocast 的 GradScaler（L3448-3451）；
5. 调 `loss.backward()`，随后手动执行 `_backward_epilogue()` 完成梯度归约（L3458-3462）。返回值是**按 gas 缩放后**的 loss（`gas_scaled_loss`），这就是用户打印的 loss。

`step()` 的主体在 [deepspeed/runtime/engine.py:3598-3646](https://github.com/deepspeedai/DeepSpeed/blob/6d64e1b9746d77ae1c1cd652006cb3bafeb09afe/deepspeed/runtime/engine.py#L3598-L3646)：先置 `_running_engine_step = True`（unmanaged 模式下它就是「边界」本身）；unmanaged 模式里 ZeRO-2/3 已在每个 backward 里完成了 reduce/partition，这里只调 `finalize_gradient_accumulation_boundary()` 收尾，而 stage 0/1/DDP 的梯度 allreduce 被推迟到这里统一做（L3629-3634）；最后只有 `is_gradient_accumulation_boundary()` 为真才进入 `_take_model_step`（L3642-3646）。

真正的更新发生在 [deepspeed/runtime/engine.py:3536-3596](https://github.com/deepspeedai/DeepSpeed/blob/6d64e1b9746d77ae1c1cd652006cb3bafeb09afe/deepspeed/runtime/engine.py#L3536-L3596) 的 `_take_model_step`，顺序是：

1. `gradient_clipping() > 0` 且**不是** fp16/bf16/ZeRO 路径时，调 `clip_fp32_gradients()`（L3537-3542）——为什么 fp16/bf16/ZeRO 不走这里？因为它们的裁剪内建在各自的优化器里（fp16 裁剪的是 fp32 主权重梯度，ZeRO 裁剪的是分区梯度）；
2. `optimizer.step()`（或 torch autocast 的 scaler.step，L3543-3547）；
3. `zero_grad`（L3554-3563）；
4. 读优化器的 `overflow` 标志：溢出则 `skipped_steps += 1`，**不推进学习率**；正常才 `lr_scheduler.step()`（L3566-3581）；
5. 进度打印、`global_steps += 1`、按 managed/unmanaged 两种口径推进 `global_samples`（L3583-3596）。

对照阅读：流水线引擎的托管循环 [deepspeed/runtime/pipe/engine.py:332-355](https://github.com/deepspeedai/DeepSpeed/blob/6d64e1b9746d77ae1c1cd652006cb3bafeb09afe/deepspeed/runtime/pipe/engine.py#L332-L355)——`train_batch()` 从 `data_iter` 消耗 `gradient_accumulation_steps` 个微批次，由调度器 `_exec_schedule` 驱动全部前向/反向/通信，最后聚合 loss 返回。它验证了「train_batch = 引擎替你写循环」这句话的字面含义，只是这份逻辑现在只属于流水线。

#### 4.1.4 代码实践

**实践目标**：用两种梯度累积管理方式跑同一个模型，对比 `global_steps` / `micro_steps` 的推进差异。

**操作步骤**（模板取自 [tests/unit/runtime/test_determinism.py:159-171](https://github.com/deepspeedai/DeepSpeed/blob/6d64e1b9746d77ae1c1cd652006cb3bafeb09afe/tests/unit/runtime/test_determinism.py#L159-L171)，该文件用 `nn.Sequential` + 字典配置搭了一个极简引擎）：

1. 写脚本 `loop_compare.py`（示例代码）：

   ```python
   import torch, deepspeed

   model = torch.nn.Sequential(torch.nn.Linear(8, 16), torch.nn.Tanh(), torch.nn.Linear(16, 4))
   optimizer = torch.optim.AdamW(model.parameters(), lr=1e-3)
   engine, _, _, _ = deepspeed.initialize(
       model=model, optimizer=optimizer,
       config={"train_micro_batch_size_per_gpu": 2, "gradient_accumulation_steps": 2})

   for microstep in range(6):
       out = engine(torch.randn(2, 8, device=engine.device))
       engine.backward(out.square().mean())
       boundary = engine.is_gradient_accumulation_boundary()
       engine.step()
       print(f"micro={microstep} boundary={boundary} "
             f"global_steps={engine.global_steps} micro_steps={engine.micro_steps}")
   ```

2. 把配置里加上 `"managed_gradient_accumulation": False` 再跑一次；这次循环要改成每 2 次 backward 才调一次 `step()`（unmanaged 模式下 `step()` 就是边界本身，见 [docs/_pages/config-json.md:46](https://github.com/deepspeedai/DeepSpeed/blob/6d64e1b9746d77ae1c1cd652006cb3bafeb09afe/docs/_pages/config-json.md#L46) 的字段说明）。

**需要观察的现象**：managed 模式下 `boundary` 交替出现 `True/False`，6 次循环后 `global_steps=3`、`micro_steps=6`；unmanaged 模式下每次 `step()` 都是边界，`global_steps` 与你调用 `step()` 的次数一致。

**预期结果**：两种模式最终的权重更新次数相同（都是 3 次），区别只在「谁负责数边界」。若现象不符，优先检查 gas 是否被配置里的 `train_batch_size` 改写（u2-l2 的三选二补全会覆盖手写的 gas）。

**待本地验证**：本实践在单进程 CPU/GPU 均可运行，具体数值以本地输出为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `backward()` 里要先把 loss 除以 gas，而不是在优化器 step 时再把梯度除以 gas？

**答案**：两者在数学上等价（梯度线性），但提前除可以把「平均」摊到每次 backward 里：梯度在累加过程中始终保持「平均梯度」的量纲，边界处的梯度裁剪（4.5 节）拿到的直接就是平均梯度的范数，ZeRO 的 reduce_scatter 中间过程也不需要额外缩放。此外 `scale_wrt_gas=False` 时（配合 fp16 动态 loss scaling 的特殊用法）这个行为可以显式关闭。

**练习 2**：在 managed 模式下连续调用 6 次 `engine.step()` 但一次 `backward()` 都不调，会发生什么？

**答案**：`step()` 本身不强制检查 backward 是否发生过。边界判定只看 `micro_steps` 与 gas 的整除关系，因此 `global_steps` 仍会在偶数次调用时推进，优化器会基于「上一步残留/为零的梯度」更新——这是使用错误，引擎不负责兜底；正因如此 `step()` 语义要求与 backward 对称调用（docstring 见 [deepspeed/runtime/engine.py:3598-3606](https://github.com/deepspeedai/DeepSpeed/blob/6d64e1b9746d77ae1c1cd652006cb3bafeb09afe/deepspeed/runtime/engine.py#L3598-L3606)）。

### 4.2 DeepSpeedDataLoader 迭代协议

#### 4.2.1 概念说明

数据侧要回答的问题是：每个 rank 拿到哪些样本？`DeepSpeedDataLoader` 是引擎对 `torch.utils.data.DataLoader` 的一层薄包装（构造入口在引擎初始化时：传入 `training_data` 就会 `deepspeed_io` 包装成 `self.training_dataloader`，[deepspeed/runtime/engine.py:655-658](https://github.com/deepspeedai/DeepSpeed/blob/6d64e1b9746d77ae1c1cd652006cb3bafeb09afe/deepspeed/runtime/engine.py#L655-L658)）。它解决三件事：

1. **分布式采样**：自动挂 `DistributedSampler`，让数据并行组内各 rank 拿到互不重叠的切片；
2. **吞吐计时**：每次 `__next__` 启动吞吐计时器，与 `step()` 里的 `tput_timer.stop()` 配对，得到端到端 samples/sec；
3. **curriculum learning**（课程学习）：按配置启用 `DeepSpeedDataSampler`，支持动态批大小等数据效率特性。

另一个常用工具是 `RepeatingLoader`：把任意迭代器包装成「取完了自动从头再来」的无限迭代器，写训练循环时不必自己处理 `StopIteration`。

#### 4.2.2 核心流程

```text
for batch in engine.training_dataloader:   # __iter__：每次进入循环都重建 torch DataLoader
    ...                                     # __next__：启动吞吐计时 → 吐出下一个 batch
```

值得注意的协议细节：`__iter__` 每次被调用都会重新 `_create_dataloader()`（重新建 DataLoader、重置 sampler），所以 `for` 循环每进入一次就是一个新 epoch，`DistributedSampler` 的 `set_epoch` 语义由重建代替。

#### 4.2.3 源码精读

[deepspeed/runtime/dataloader.py:41-106](https://github.com/deepspeedai/DeepSpeed/blob/6d64e1b9746d77ae1c1cd652006cb3bafeb09afe/deepspeed/runtime/dataloader.py#L41-L106) 的 `__init__`：构造分支决定采样器——启用 curriculum learning 时用 `DeepSpeedDataSampler`；否则 `local_rank >= 0`（分布式）时若调用方未给 sampler 就默认挂 `DistributedSampler`（按 `data_parallel_world_size` / `data_parallel_rank` 切分，L76-80），单进程时用 `RandomSampler`。IO worker 数默认 `2 * device_count`（L88-89）。

[deepspeed/runtime/dataloader.py:108-124](https://github.com/deepspeedai/DeepSpeed/blob/6d64e1b9746d77ae1c1cd652006cb3bafeb09afe/deepspeed/runtime/dataloader.py#L108-L124) 的迭代协议：`__iter__` 调 `_create_dataloader()` 后返回 self；`__next__` 先启动 `tput_timer`，curriculum 分支走自己的数据迭代器（并可选后处理），普通分支直接 `next(self.data)`。

[deepspeed/runtime/dataloader.py:17-38](https://github.com/deepspeedai/DeepSpeed/blob/6d64e1b9746d77ae1c1cd652006cb3bafeb09afe/deepspeed/runtime/dataloader.py#L17-L38) 的 `RepeatingLoader`：`__next__` 捕获 `StopIteration` 后重建迭代器再取一次，实现永不枯竭。

注意：**引擎并不强制你用 DeepSpeedDataLoader**。单元测试基建就是现成的反例——[tests/unit/simple_model.py:252-256](https://github.com/deepspeedai/DeepSpeed/blob/6d64e1b9746d77ae1c1cd652006cb3bafeb09afe/tests/unit/simple_model.py#L252-L256) 的 `random_dataloader` 用普通 `torch.utils.data.DataLoader`，batch size 直接取 `model.train_micro_batch_size_per_gpu()`。只要你能保证各 rank 样本不重叠（或本来就单进程），引擎对数据来源无感知。

#### 4.2.4 代码实践

**实践目标**：观察 `DeepSpeedDataLoader` 的迭代协议与 `RepeatingLoader` 的行为。

**操作步骤**（示例代码）：

```python
import torch
from torch.utils.data import TensorDataset
from deepspeed.runtime.dataloader import DeepSpeedDataLoader, RepeatingLoader

ds = TensorDataset(torch.arange(10))
loader = DeepSpeedDataLoader(dataset=ds, batch_size=4, pin_memory=False,
                             local_rank=-1, tput_timer=None)   # local_rank=-1 → 单进程 RandomSampler
print(len(loader))          # ceil(10 / 4) = 3
for batch in loader:        # 第一次 __iter__：建 DataLoader
    print(batch[0].tolist())

rep = RepeatingLoader([1, 2, 3])
print([next(rep) for _ in range(7)])   # 1 2 3 1 2 3 1 —— 永不枯竭
```

**需要观察的现象**：`len(loader)` 是向上取整的批数；第二次 `for batch in loader` 会重新洗牌（新的 RandomSampler）。

**预期结果**：如上注释。**待本地验证**（输出顺序取决于随机种子）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `__iter__` 每次都重建 DataLoader 而不是缓存一个迭代器？

**答案**：`torch` DataLoader 的迭代器是一次性的，`DistributedSampler` 的 epoch 洗牌依赖重建（或手动 `set_epoch`）。每次 `__iter__` 重建让「每个 epoch 重新进入 for 循环」天然获得新的洗牌顺序，避免用户忘记 `set_epoch` 导致每个 epoch 读到相同顺序。

**练习 2**：`DistributedSampler` 的 `num_replicas` 用的是 `data_parallel_world_size` 而不是全局 world_size，为什么？

**答案**：多维并行下（比如 TP=2、DP=2、world_size=4），同一份数据必须喂给张量并行组里的每个 rank（它们算同一个模型的分片），只有数据并行维度才需要切分数据。所以按 DP 宽度切、DP 组内各 rank 各拿一份。

### 4.3 梯度累积边界判定

#### 4.3.1 概念说明

`is_gradient_accumulation_boundary()` 是主循环的「节拍器」：它为真时，本微批次的 backward 之后要做跨 rank 梯度同步，`step()` 里要做优化器更新；为假时，梯度只在本 rank 累加。它有三个数据来源（优先级从高到低）：

1. **手动覆盖**：用户调 `set_gradient_accumulation_boundary(True/False)` 显式指定（`self._is_gradient_accumulation_boundary` 不为 None 时直接返回它）；
2. **unmanaged 模式**：`managed_gradient_accumulation=False` 时返回 `_running_engine_step`——只有 `step()` 执行期间为真，即「step() 就是边界」；
3. **managed 模式**（默认）：`(micro_steps + 1) % gas == 0`——纯粹的计数取模。

还有一个容易忽略的镜像机制：**ZeRO 优化器内部也持有一份边界标志**。引擎在 backward 序章里把边界值「下发」给优化器（`set_gradient_accumulation_boundary`），ZeRO 的梯度 reduce hook 在反向过程中读的是自己那份镜像，而不是回调引擎——因为 hook 触发时机深埋在 autograd 里，不能依赖引擎状态随时可查。基类 [deepspeed/runtime/base_optimizer.py:250-271](https://github.com/deepspeedai/DeepSpeed/blob/6d64e1b9746d77ae1c1cd652006cb3bafeb09afe/deepspeed/runtime/base_optimizer.py#L250-L271) 里能看到这对 get/set 与注释「Engine owns the source of truth」。

#### 4.3.2 核心流程

```text
managed 模式（gas=2）:
micro_steps: 0 → boundary? (0+1)%2==1 → False（只累加）
micro_steps: 1 → boundary? (1+1)%2==0 → True （同步 + 优化器更新）
step() 末尾 micro_steps += 1，周而复始

ZeRO-2/3 特别之处：梯度归约按 bucket 在 backward 期间流式进行（overlap_comm），
边界只决定「最后一块 reduce_scatter 之后的收尾动作」；
stage 0/1 / DDP：梯度 allreduce 整体推迟到边界（_backward_epilogue → allreduce_gradients）
```

#### 4.3.3 源码精读

判定逻辑本体在 [deepspeed/runtime/engine.py:3473-3496](https://github.com/deepspeedai/DeepSpeed/blob/6d64e1b9746d77ae1c1cd652006cb3bafeb09afe/deepspeed/runtime/engine.py#L3473-L3496)：三个分支依次是手动覆盖、unmanaged（`_running_engine_step`）、zenflow 特例与默认取模。

手动覆盖入口 [deepspeed/runtime/engine.py:3498-3524](https://github.com/deepspeedai/DeepSpeed/blob/6d64e1b9746d77ae1c1cd652006cb3bafeb09afe/deepspeed/runtime/engine.py#L3498-L3524)，docstring 给出了标准用法：前 gas-1 个微批次设 False、最后一个设 True、然后只调一次 `engine.step()`。它同时把值透传给优化器的镜像接口。

边界如何控制通信频率：[deepspeed/runtime/engine.py:3091-3116](https://github.com/deepspeedai/DeepSpeed/blob/6d64e1b9746d77ae1c1cd652006cb3bafeb09afe/deepspeed/runtime/engine.py#L3091-L3116) 的 `allreduce_gradients()`——先把边界告知优化器（L3099-3100），ZeRO-2/3 走 `overlapping_partition_gradients_reduce_epilogue`（非边界也在通信，L3104-3105），普通 DDP/ZeRO-1 只在边界 allreduce（L3108-3114）。这个函数由 `_backward_epilogue` 调用（[deepspeed/runtime/engine.py:3194-3196](https://github.com/deepspeedai/DeepSpeed/blob/6d64e1b9746d77ae1c1cd652006cb3bafeb09afe/deepspeed/runtime/engine.py#L3194-L3196)），unmanaged 模式则推迟到 `step()` 里。

与边界配合的每张量缩放：[deepspeed/runtime/engine.py:3210-3218](https://github.com/deepspeedai/DeepSpeed/blob/6d64e1b9746d77ae1c1cd652006cb3bafeb09afe/deepspeed/runtime/engine.py#L3210-L3218) 的 `_backward_prologue_per_tensor` 把输出张量的梯度除以 gas——这是 4.1 节「loss 先除 gas」在「用户直接调 `tensor.backward()`」场景下的等价实现：引擎不知道 loss 张量是谁，就在梯度侧补缩放（若本张量已由 `engine.backward` 缩放过，图跟踪器会跳过，L3213-3215）。

#### 4.3.4 代码实践

**实践目标**：直观验证边界的出现规律，并观察手动覆盖模式。

**操作步骤**（示例代码，接 4.1.4 的脚本）：

```python
# A. managed 模式：已在 4.1.4 观察过 boundary 的 True/False 交替

# B. 手动覆盖模式：
for _ in range(2):   # gas=2
    engine.set_gradient_accumulation_boundary(False)
    out = engine(torch.randn(2, 8, device=engine.device))
    engine.backward(out.square().mean())
    engine.step()                      # 非边界 step：只推进 micro_steps
engine.set_gradient_accumulation_boundary(True)
out = engine(torch.randn(2, 8, device=engine.device))
engine.backward(out.square().mean())
engine.step()                          # 边界 step：global_steps += 1
```

**需要观察的现象**：方案 B 中前两次 `step()` 不推进 `global_steps`，最后一次推进 1。

**预期结果**：与 managed 模式等价（每 3 次调用推进 1 次 `global_steps`）。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 ZeRO-2 在非边界处也要通信（reduce_scatter），而 DDP 只在边界 allreduce？

**答案**：ZeRO-2 把梯度按 rank 切分，每个 rank 只保留自己那段——归约本身就是「把梯度写进分区」的动作，必须每个微批次都做（或用桶边做边做），否则本 rank 分区外的梯度无法累积到正确位置；DDP 每个 rank 持有完整梯度副本，中间过程不需要通信，攒到边界一次 allreduce 平均即可（还能与其他 micro-batch 的计算重叠）。

**练习 2**：unmanaged 模式下 `_running_engine_step` 只在 `step()` 期间为 True，那 backward 期间 ZeRO 读到的边界是什么？

**答案**：是引擎上次下发的镜像值。`ZeROOptimizer.is_gradient_accumulation_boundary` 的 docstring（[deepspeed/runtime/base_optimizer.py:260-267](https://github.com/deepspeedai/DeepSpeed/blob/6d64e1b9746d77ae1c1cd652006cb3bafeb09afe/deepspeed/runtime/base_optimizer.py#L260-L267)）说明 unmanaged 模式下镜像保持 True 以让迟到的读取者一致——unmanaged 的语义是「backward 期间各 rank 自己流式归约，`step()` 收尾」，所以反向过程中按边界处理、由 `finalize_gradient_accumulation_boundary` 做最后卸载。

### 4.4 _backward_prologue 防御检查：AutoTP 冻结参数

#### 4.4.1 概念说明

`_backward_prologue`（backward 序章）是引擎在**每次反向传播真正开始之前**执行的准备工作：启动计时器、做防御检查、把边界标志下发给 ZeRO。它的触发方式很巧：不是在 `engine.backward()` 里显式调用，而是作为 hook 挂在**前向输出张量**上——用户无论是调 `engine.backward(loss)` 还是直接 `loss.backward()`，autograd 一旦开始回传就必然经过输出张量，hook 就会点燃。这样 DeepCompile（u8-l4）等会重写 backward 的路径也能被正确接管。

本周期（#8851）序章里新增了一道防御：**AutoTP 冻结复制参数检查**。背景：

- AutoTP（自动张量并行，u6-l1）注入时，有些参数保持「复制」状态但作用于已切分的激活（例如 Qwen3 的 q_norm/k_norm 只归一化本地注意力头），每个 TP rank 对它们算出的梯度是**不同的部分和**，必须对 TP 组 allreduce 才能一致；
- `register_replicated_grad_hooks` 给这类参数挂梯度 hook，但 **PyTorch 不允许给 `requires_grad=False` 的张量挂 hook**——冻结参数（比如蒸馏 teacher）只能跳过，跳过清单被引擎存进 `_autotp_frozen_replicated_params`；
- 危险场景：用户先冻结参数完成 AutoTP 注入，训练开始后又把某层 `requires_grad=True` 解冻。此时该参数**没有**梯度 allreduce hook，各 TP rank 会从第一步更新起就朝着不同方向漂移——**不报错、不崩溃，模型悄悄坏掉**，这是最难排查的一类 bug。

所以引擎的对策是：每次 backward 序言都检查冻结清单里的参数是否仍处于冻结状态，一旦发现被解冻就直接抛 `RuntimeError`，把静默漂移变成显式失败。

#### 4.4.2 核心流程

```text
deepspeed.initialize（AutoTP 注入）
  └─ finalize_autotp: _autotp_frozen_replicated_params = register_replicated_grad_hooks(model)
        ├─ 需要梯度 allreduce 且 requires_grad=True 的参数 → 挂 hook
        └─ requires_grad=False 的参数 → 记入 frozen 清单返回

每次 engine.backward(loss) / loss.backward()
  └─（输出张量 hook）_backward_prologue
        └─ _check_autotp_frozen_replicated_params
              └─ 清单中任何参数 requires_grad 变为 True → RuntimeError
```

#### 4.4.3 源码精读

hook 的注册发生在 `forward()` 里：[deepspeed/runtime/engine.py:3030-3042](https://github.com/deepspeedai/DeepSpeed/blob/6d64e1b9746d77ae1c1cd652006cb3bafeb09afe/deepspeed/runtime/engine.py#L3030-L3042) 把 `_backward_prologue` 包成 `backward_prologue` 闭包，连同每张量缩放函数一起交给 `register_output_backward_hooks(loss, ...)`。这个工具函数（[deepspeed/runtime/utils.py:1474-1508](https://github.com/deepspeedai/DeepSpeed/blob/6d64e1b9746d77ae1c1cd652006cb3bafeb09afe/deepspeed/runtime/utils.py#L1474-L1508)）支持任意嵌套结构的输出（tensor/tuple/list/dict），hook 管理器的生命周期与输出张量绑定——张量被释放，hook 随之失效。

防御检查本体 [deepspeed/runtime/engine.py:3141-3150](https://github.com/deepspeedai/DeepSpeed/blob/6d64e1b9746d77ae1c1cd652006cb3bafeb09afe/deepspeed/runtime/engine.py#L3141-L3150)：遍历 `_autotp_frozen_replicated_params` 清单，筛出 `requires_grad` 已变 True 的参数名，抛出带修复指引的 `RuntimeError`（提示「应在 initialize 之前就设好 requires_grad，而不是事后解冻」）。

清单的来源有两处：注入时 [deepspeed/runtime/engine.py:1073-1075](https://github.com/deepspeedai/DeepSpeed/blob/6d64e1b9746d77ae1c1cd652006cb3bafeb09afe/deepspeed/runtime/engine.py#L1073-L1075)（`finalize_autotp` 把返回的冻结清单存到引擎），以及生产端 [deepspeed/module_inject/auto_tp.py:940-987](https://github.com/deepspeedai/DeepSpeed/blob/6d64e1b9746d77ae1c1cd652006cb3bafeb09afe/deepspeed/module_inject/auto_tp.py#L940-L987)：`register_replicated_grad_hooks` 对每个匹配 `grad_allreduce` 规则的参数，`requires_grad=False` 的记入 `frozen` 返回（L973-976），其余挂 TP 组 allreduce hook 并打 `_ds_grad_allreduce_registered` 防重复标记（L977-980）。

完整的序章在 [deepspeed/runtime/engine.py:3152-3182](https://github.com/deepspeedai/DeepSpeed/blob/6d64e1b9746d77ae1c1cd652006cb3bafeb09afe/deepspeed/runtime/engine.py#L3152-L3182)：顺序是计时器启动 → **冻结参数检查** → 拒绝不支持的裸 `tensor.backward()`（L3160-3164，PyTorch 内部 API 缺失或 ZeRO stage 0 时只允许 `engine.backward`）→ DeepCompile 序章 → ZeRO 的 `backward_prologue`/`enter_backward`/`queue_post_backward_callback`（L3171-3174，与 u5 的 reduce hook 体系衔接）→ 边界下发（L3179-3180）→ 内层计时器。

#### 4.4.4 代码实践

**实践目标**：源码阅读型实践——理解防御检查的触发条件，并能复现报错信息。

**操作步骤**：

1. 精读 `register_replicated_grad_hooks` 的 docstring（[deepspeed/module_inject/auto_tp.py:941-951](https://github.com/deepspeedai/DeepSpeed/blob/6d64e1b9746d77ae1c1cd652006cb3bafeb09afe/deepspeed/module_inject/auto_tp.py#L941-L951)），它解释了「复制参数 + 分片激活 = 每 rank 不同的部分梯度」的成因。
2. （需 AutoTP 环境，参考 [docs/_tutorials/autotp-training.md](https://github.com/deepspeedai/DeepSpeed/blob/6d64e1b9746d77ae1c1cd652006cb3bafeb09afe/docs/_tutorials/autotp-training.md)）示例代码：

   ```python
   # 1) 冻结一个 AutoTP 会保持复制的参数（如某 norm 层），再 initialize
   for p in model.model.layers[0].input_layernorm.parameters():
       p.requires_grad_(False)
   engine, _, _, _ = deepspeed.initialize(model=model, config=cfg, ...)
   # 2) 注入完成后解冻 —— 危险动作
   p = next(iter(engine.module.model.layers[0].input_layernorm.parameters()))
   p.requires_grad_(True)
   # 3) 第一次 backward 会立刻抛 RuntimeError
   loss = engine(batch); engine.backward(loss)
   ```

**需要观察的现象**：步骤 3 抛出 `RuntimeError: AutoTP replicated parameters [...] were frozen at deepspeed.initialize ...`，报错参数名列出被解冻的参数。

**预期结果**：如上报错（异常文案见 [deepspeed/runtime/engine.py:3148-3150](https://github.com/deepspeedai/DeepSpeed/blob/6d64e1b9746d77ae1c1cd652006cb3bafeb09afe/deepspeed/runtime/engine.py#L3148-L3150)）。**待本地验证**：具体哪层参数属于「复制 + grad_allreduce」取决于模型 policy，需要按所用模型确认；单进程（TP=1）时清单恒为空、检查不触发。

#### 4.4.5 小练习与答案

**练习 1**：为什么这个检查放在 backward 序章里，而不是 `deepspeed.initialize()` 时一次性检查？

**答案**：解冻发生在 initialize **之后**，注入时该参数确实冻结、一切正常；只有后续某个时刻用户改了 `requires_grad` 才形成错误。序章每次 backward 都跑一遍，才能捕获「任意时刻被解冻」的情况，把失败点尽量推近事故点（第一次用到它的 backward）。

**练习 2**：如果不做这个检查，故障会以什么形式暴露？

**答案**：静默数值漂移——TP 组各 rank 从解冻后的第一次更新起对同一参数累积不同梯度，权重逐渐分叉，且没有任何报错；往往要等 loss 曲线异常或评测掉点才被发现，定位成本极高。这是「快失败（fail fast）优于静默出错」设计原则的典型应用。

### 4.5 MoE 感知的 fp32 梯度裁剪范数

#### 4.5.1 概念说明

梯度裁剪的第一步是算**全局梯度范数**。单进程很简单：把所有参数梯度拼成一个大向量求 p-范数。分布式下的难点是：**范数不是可加的**——各 rank 各自算出局部范数后，不能直接求和或求平均得到全局范数，必须按 p-范数的幂次合并：

\[ \|g\|_p = \left( \sum_i \|g_i\|_p^{\,p} \right)^{1/p} \]

其中 \(g_i\) 是第 i 个参数（或第 i 组参数）的梯度。对复制参数（DDP 梯度已 allreduce 平均，各 rank 持有同一份梯度），各 rank 算出的范数相同，此时「对 DP 组求平均」恰好是无损的恒等操作：

\[ \frac{1}{W}\sum_{r=1}^{W} \|g^{(r)}\| = \|g\| \quad (\text{当 } g^{(r)} \equiv g) \]

这就是旧代码里 `total_norm / world_size` 再 allreduce 的隐含前提。**但专家参数打破了这个前提**：专家并行下每个 rank 持有不同的专家子集，各 rank 的局部范数描述的是**不同参数集**。旧实现把它们混在一起平均，得到的是数学上没有意义的数。以 PR #8478 的复现数据为例：4 个专家梯度（L2 范数分别为 \(\sqrt{20}\) 与 \(\sqrt{100}\) 的两组，各占一半）分在两个 rank 上：

\[ \text{正确（每个专家计一次）} = \sqrt{20 + 100} \approx 10.954, \qquad \text{旧的平均} = \frac{\sqrt{20} + \sqrt{100}}{2} \approx 7.236 \]

同样的梯度、34% 的偏差，结果只取决于专家恰好放在哪些 rank 上。`clip_coef = max_norm / total_norm`，只要裁剪生效，这个偏差会直接改变每次参数更新。

本周期的修复（#8478，"Count each expert once in the fp32 gradient-clipping norm"）把 fp32 路径改成 bf16/fp16 优化器早已在用的精确做法：**稠密参数的范数照旧，专家梯度按专家组在专家并行组（EP 组）上归约——每个专家只计一次**，再按幂次合并：

\[ \|g\|_2^{\text{global}} = \left( \big\|g_{\text{dense}}\big\|_2^2 + \sum_{e \in \text{experts}} \big\|g_e\big\|_2^2 \right)^{1/2} \]

#### 4.5.2 核心流程

`clip_grad_norm_` 的三段式：

```text
① 决策：走哪条归约路径？（必须所有 rank 一致！）
   registry 非空（配置了 MoE）→ 构造 2 维投票张量：
       vote[0] = 本 rank 是否有「带 group_name 的专家参数」
       vote[1] = 本 rank 是否有「不带 group_name 的专家参数」（孤儿，只能走旧路径）
   对世界组 all_reduce(vote)
   vote[0]>0 且 vote[1]==0 → 走专家路径；否则走旧的 DP 平均路径

② 计算局部范数
   专家路径：稠密参数算范数（各 rank 已相同）；专家梯度按 group_name 分桶，
             空桶补零张量（零对幂次求和与 max 都无贡献，保证所有 rank 参与同一组归约）
   旧路径：   全部参数算范数

③ 合并
   专家路径：get_norm_with_moe_layers = 稠密范数 + 每个专家组在 EP 组上归约的范数，
             按 (Σ norm^p)^(1/p) 合并；非有限值报告为 -1，映射回 inf
   旧路径：   total_norm / DP world_size，DP 组 all_reduce

④ 裁剪：clip_coef = min(1, max_norm / (total_norm + 1e-6))，就地乘到每个梯度上
```

**为什么投票必须在世界组上做**：两条分支发出的是**不同的集合通信序列**，所有 rank 必须做同一个选择，否则死锁。而本地信息不可靠——流水线并行下某个 stage 可能只有稠密层（本 rank 无专家，但别的 stage 有）；且 DP 组在流水线下「只有一个 stage 宽」，专家范数却在跨 stage 的组上归约。一次世界组的标量 all_reduce 让所有 rank 达成一致。**为什么空 registry 直接走旧路径**：这是 `_take_model_step` 每步都调的热路径，无 MoE 的常见场景不应为一个投票付出集合通信加 host 同步的代价。

#### 4.5.3 源码精读

入口链路：`_take_model_step` 里 [deepspeed/runtime/engine.py:3536-3542](https://github.com/deepspeedai/DeepSpeed/blob/6d64e1b9746d77ae1c1cd652006cb3bafeb09afe/deepspeed/runtime/engine.py#L3536-L3542)——`gradient_clipping() > 0` 且非 fp16/bf16/ZeRO 时调 [deepspeed/runtime/engine.py:3533-3534](https://github.com/deepspeedai/DeepSpeed/blob/6d64e1b9746d77ae1c1cd652006cb3bafeb09afe/deepspeed/runtime/engine.py#L3533-L3534) 的 `clip_fp32_gradients()`，它只是 `clip_grad_norm_(self.module.parameters(), max_norm=..., mpu=...)` 的一行封装。

`clip_grad_norm_` 全文在 [deepspeed/runtime/utils.py:363-508](https://github.com/deepspeedai/DeepSpeed/blob/6d64e1b9746d77ae1c1cd652006cb3bafeb09afe/deepspeed/runtime/utils.py#L363-L508)，按段精读：

- **投票段** [deepspeed/runtime/utils.py:389-423](https://github.com/deepspeedai/DeepSpeed/blob/6d64e1b9746d77ae1c1cd652006cb3bafeb09afe/deepspeed/runtime/utils.py#L389-L423)：注释先讲清「专家参数对 DP 平均不可重构」的动机，然后注意一个细节——**所有权信息在过滤无梯度参数之前读取**（L394-397），因为「本 step 没收到 token 的专家」有参数无梯度，若先过滤再判定，各 rank 会得出不同结论。投票张量构造与 `dist.all_reduce(vote, group=groups._clone_world_group())` 在 L415-421，世界组投票的原因写在 L405-414 的注释里（流水线并行下 DP 组只有一个 stage 宽）。
- **分桶段** [deepspeed/runtime/utils.py:425-444](https://github.com/deepspeedai/DeepSpeed/blob/6d64e1b9746d77ae1c1cd652006cb3bafeb09afe/deepspeed/runtime/utils.py#L425-L444)：过滤无梯度参数；专家梯度按 `p.group_name` 分桶，**桶的顺序取 registry 的排序而不是本 rank 参数的遍历顺序**（`get_norm_with_moe_layers` 对每个桶发一次集合通信，各 rank 必须同序访问，L429-431）；空桶补 `zero`（L436-442）。
- **局部范数段** [deepspeed/runtime/utils.py:446-479](https://github.com/deepspeedai/DeepSpeed/blob/6d64e1b9746d77ae1c1cd652006cb3bafeb09afe/deepspeed/runtime/utils.py#L446-L479)：专家路径下 `norm_parameters` 只含稠密参数；每参数范数先 `pow(norm_type)` 再求和、最后开 `1/norm_type` 次方（L468-472 的注释解释了为什么必须按 `norm_type` 取幂——只有 p=2 时才是「平方」）。inf 范数分支处理「全是专家参数」的边界（空列表时以 0 为恒等元，L450-453）。
- **合并段** [deepspeed/runtime/utils.py:481-500](https://github.com/deepspeedai/DeepSpeed/blob/6d64e1b9746d77ae1c1cd652006cb3bafeb09afe/deepspeed/runtime/utils.py#L481-L500)：专家路径把稠密范数交给 `get_norm_with_moe_layers`（此时 total_norm 只覆盖复制参数、各 rank 已相同，无需再平均）；**-1 映射为 inf**（L489-490）——辅助函数用 -1 报告「非有限范数」，若原样使用会让 `clip_coef` 变负、**翻转每个梯度的符号**，映射成 inf 则维持旧行为（把梯度缩放为 0）。else 分支保留旧的 DP 平均路径（L492-500），无 MoE 时平均是恒等操作。
- **裁剪段** [deepspeed/runtime/utils.py:502-508](https://github.com/deepspeedai/DeepSpeed/blob/6d64e1b9746d77ae1c1cd652006cb3bafeb09afe/deepspeed/runtime/utils.py#L502-L508)：`clip_coef = min(1, max_norm/(total_norm+1e-6))`，就地 `grad.mul_(clip_coef)`。

专家范数的精确实现 [deepspeed/runtime/utils.py:1193-1231](https://github.com/deepspeedai/DeepSpeed/blob/6d64e1b9746d77ae1c1cd652006cb3bafeb09afe/deepspeed/runtime/utils.py#L1193-L1231)：`get_norm_with_moe_layers` 对每个专家组调 `get_global_norm_of_tensors(..., moe_ep_group=...)`，得到该组范数后统一按幂次合并，任何一组非有限则返回 -1。底层 [deepspeed/runtime/utils.py:1026-1047](https://github.com/deepspeedai/DeepSpeed/blob/6d64e1b9746d77ae1c1cd652006cb3bafeb09afe/deepspeed/runtime/utils.py#L1026-L1047)：每张量求范数 → 取 p 次幂 → 求和 → （视 MoE-TP/流水线配置在模型并行或流水线组上 all_reduce）→ **在 EP 组上 all_reduce**（L1044-1046）→ 开方。因为每个专家只存在于一个 EP rank 上，EP 组归约天然保证「每个专家恰好计一次」。

配套测试（本周期新增）在 [tests/unit/runtime/test_runtime_utils.py:33-100](https://github.com/deepspeedai/DeepSpeed/blob/6d64e1b9746d77ae1c1cd652006cb3bafeb09afe/tests/unit/runtime/test_runtime_utils.py#L33-L100)：`test_sharded_experts_count_once`（L57-86）构造 4 个取值 1/2/3/4 的专家梯度分到 2 个 rank，用远大于范数的 `max_norm`（不实际裁剪、直接读范数）断言 `clip_grad_norm_` 返回 \(\sqrt{120}\)——精确对应 4.5.1 的数值；同文件还有「一侧专家空转不死锁」「无专家的 rank 不死锁」「纯专家 inf 范数」「专家组按 registry 顺序访问」等用例，以及穿过引擎全链路的 `TestClipFp32GradientsThroughEngine`（L217 起）。

#### 4.5.4 代码实践

**实践目标**：亲眼看一次 `total_norm`；理解无 MoE 时「除以 DP world_size 再 all_reduce」为何无损。

**操作步骤**（示例代码）：

```python
import torch, deepspeed
from deepspeed.runtime.utils import clip_grad_norm_

model = torch.nn.Linear(4, 4)
optimizer = torch.optim.SGD(model.parameters(), lr=0.1)
engine, _, _, _ = deepspeed.initialize(
    model=model, optimizer=optimizer,
    config={"train_micro_batch_size_per_gpu": 2,
            "gradient_accumulation_steps": 1,
            "gradient_clipping": 1.0})          # 打开梯度裁剪

for _ in range(3):
    loss = engine(torch.randn(2, 4)).square().mean()
    engine.backward(loss)
    # fp32 非 ZeRO 路径上引擎内部调 clip_grad_norm_ 但丢弃返回值，
    # 这里自己再调一次只为了打印（注意：step 内部会再裁剪一次，观察实验无妨）
    norm = clip_grad_norm_(engine.module.parameters(), max_norm=1.0)
    print("total_norm =", float(norm))
    engine.step()
```

**需要观察的现象**：每步打印一个正的 `total_norm`；单进程下 `norm / 1` 再 allreduce 是恒等操作，数值与手算 `\|grad\|_2` 一致。

**预期结果**：打印值与你自己 `torch.norm(torch.cat([p.grad.flatten() for p in model.parameters()]))` 的结果一致（误差 1e-6 量级，来自分母的 `+1e-6`）。

**进阶（需 2 个 GPU，待本地验证）**：跑 `pytest tests/unit/runtime/test_runtime_utils.py::TestClipGradNorm -k "count_once or idle"`，观察 \(\sqrt{120}\) 断言通过；再用 `torchrun --nproc_per_node=2` 把上面的脚本改成每 rank 各持一半参数（模拟专家分布），对比新旧两种合并方式的数值差异——旧的 DP 平均给出 ~7.24，新实现给出 ~10.95。

**关于「除以 world_size」的解释**（无 MoE 时）：DDP 语义下梯度在 allreduce 时已经平均，各 rank 的梯度与范数完全相同，`sum(相同值)/W = 该值`，平均是恒等变换；它的存在是历史包袱——旧实现无法区分「复制参数」与「专家参数」，只好对混合体取平均，对复制部分无损、对专家部分有偏。本周期修复后，这个近似只保留在保证无 MoE 的分支里。

#### 4.5.5 小练习与答案

**练习 1**：`vote` 的第二个分量（「本 rank 存在没有 group_name 的专家参数」）不为零时为什么直接退回旧路径，而不是报错？

**答案**：没有 `group_name` 的专家参数找不到它所属的专家并行组，无从归约；直接报错会破坏既有的合法用法（测试里就有手工构造这种参数的调用方，见 `test_gather`）。退回旧路径是保守选择——行为与修复前一致，不会让原本能跑的场景变不能用。

**练习 2**：专家路径下稠密参数的范数为什么**不需要**再对 DP 组做 any 归约？

**答案**：稠密（复制）参数的梯度在 backward 的 `allreduce_gradients`/ZeRO 归约中已经对 DP 组平均，各 rank 手里的梯度相同，算出的范数自然相同——合并发生在本地即可。只有「各 rank 持有不同参数集」（专家）才需要为范数专门发集合通信。

**练习 3**：如果把 `get_norm_with_moe_layers` 返回的 -1 原样放进 `clip_coef = max_norm / (total_norm + 1e-6)`，会发生什么？

**答案**：`total_norm = -1` 时 `clip_coef ≈ -max_norm`（还是负数），`grad.mul_(clip_coef)` 会**翻转所有梯度的符号**——参数朝反方向更新，比「梯度爆炸」更隐蔽。映射成 inf 后 `clip_coef → 0`，梯度被清零、该步等于跳过，这正是溢出场景想要的保守行为。

## 5. 综合实践

把本讲五个模块串成一个完整脚本（示例代码，单进程即可运行）：

1. 用 `tests/unit/simple_model.py` 的 `random_dataset` / `random_dataloader` 构造数据（模块 4.2）；
2. 配置 `train_batch_size=8, micro_batch=2, gas=4, gradient_clipping=1.0`，搭一个两层 MLP 引擎；
3. 写 managed 循环：每个 micro-batch 打印 `micro_steps`、`is_gradient_accumulation_boundary()`，边界处打印 `global_steps`（模块 4.1 + 4.3）；
4. 边界处自行调用 `clip_grad_norm_(engine.module.parameters(), 1.0)` 打印 `total_norm`，并手动验证与 `torch.norm(concat(grads))` 一致（模块 4.5）；
5. 故意在第二个 epoch 前把 `engine.micro_steps` 加 1（或用 `set_gradient_accumulation_boundary` 强制），观察边界相位的翻转对 `global_steps` 推进的影响；
6. 把 `managed_gradient_accumulation` 改为 `False`，重写为「2 次 backward + 1 次 step」的 unmanaged 循环，验证 `global_steps` 与 `step()` 调用次数一致（模块 4.1）。

预期：两种模式下每个 `global_step` 消耗的样本数相同；`total_norm` 与手算一致；边界 True 的频率 = 1/gas。**待本地验证**（CPU 环境即可，把张量留在 CPU 上运行）。

## 6. 本讲小结

- `train_batch()` 已从 `DeepSpeedEngine` 移除（只活在 `PipelineEngine`），标准循环是 `engine(batch)` → `engine.backward(loss)` → `engine.step()`；`step()` 每个 micro-batch 都要调，但优化器更新只在边界发生（`_take_model_step`）。
- `is_gradient_accumulation_boundary()` 有三种判定来源：手动覆盖 > unmanaged 的 `_running_engine_step` > managed 的 `(micro_steps+1) % gas == 0`；ZeRO 优化器持有引擎下发的边界镜像。
- backward 的准备工作通过输出张量上的 hook 触发 `_backward_prologue`；本周期新增 AutoTP 冻结参数防御检查，把「解冻后无梯度 allreduce、各 rank 静默漂移」变成第一时间 `RuntimeError`。
- fp32 梯度裁剪链路 `clip_fp32_gradients -> clip_grad_norm_` 本周期完成 MoE 感知改造：专家参数不再混入 DP 平均（对各 rank 不同的参数集平均无意义），而是按专家组在 EP 组上归约、每个专家只计一次，最后按 p-范数幂次合并；一次世界组 all_reduce 投票保证所有 rank 走同一条通信路径。
- 无 MoE 时「范数除以 DP world_size 再 all_reduce」是恒等操作（各 rank 梯度已平均、范数相同），这条旧路径被保留且零额外开销（无 MoE 时不发投票）。
- `DeepSpeedDataLoader` 包装分布式采样与吞吐计时，`__iter__` 每次 epoch 重建；但引擎对数据来源无感，普通 torch DataLoader（如 simple_model 的 `random_dataloader`）同样可用。

## 7. 下一步学习建议

- **u4-l1（fp16 与动态 loss scaling）**：本讲 `backward()` 里 `scale_if_loss` 一行带过的 loss 缩放机制，在那里展开成完整的溢出检测与 scale 窗口调整状态机。
- **u5-l3（ZeRO-1/2）与 u5-l6（ZeRO-3 优化器）**：本讲的边界判定在 ZeRO 优化器内部如何驱动 reduce_scatter 流水线、梯度裁剪如何改为对分区梯度求范数。
- **u6-l5 / u6-l6（MoE 与 AutoEP）**：`is_moe_param` / `group_name` 这些标志从哪来、专家并行组如何创建，是理解本讲裁剪改造的前置全景。
- 继续阅读源码的话，建议精读 [tests/unit/runtime/test_runtime_utils.py](https://github.com/deepspeedai/DeepSpeed/blob/6d64e1b9746d77ae1c1cd652006cb3bafeb09afe/tests/unit/runtime/test_runtime_utils.py) 中 `TestClipGradNorm` 的全部用例——它们把「专家分布在哪些 rank 上」的每种角落情况（空转、无专家、无梯度、inf）都编码成了断言，是最好的规格说明书。

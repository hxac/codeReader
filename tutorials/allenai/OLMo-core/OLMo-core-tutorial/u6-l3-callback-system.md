# u6-l3 回调系统：如何扩展训练循环

## 1. 本讲目标

[u6-l1](u6-l1-trainer-loop.md) 我们精读了 `Trainer.fit()` 的主循环，并留下了一个关键结论：**checkpoint 与评测都不在循环正文里，而是分别由 `CheckpointerCallback` 的 `post_train_batch` 和 `EvaluatorCallback` 的 `post_step` 钩子触发**。本讲就来打开这个「钩子世界」，学完后你应该能够：

1. **列举** `Callback` 基类的全部钩子，并准确说出每个钩子在训练循环中的触发位置与先后顺序。
2. **解释** 回调的注册（`TrainerConfig.callbacks` / `with_callback()`）、排序（`priority`）与「默认回调自动补充」机制，并能预测一组回调的执行顺序。
3. **读懂** 四个代表性内置回调的实现思路：`CheckpointerCallback`（checkpoint）、`SpeedMonitorCallback`（吞吐指标的生产者）、`ConsoleLoggerCallback`（指标的消费者）、`EvaluatorCallback`（在环评测）。
4. **编写** 一个自定义回调，挂到 `TrainerConfig` 上跑一次小训练，并验证钩子触发顺序与你的预期一致。

## 2. 前置知识

本讲假设你已读过：

- **u6-l1（Trainer 训练循环剖析）**：知道 `fit()` → `_fit_epoch()` → 单步的执行顺序（取 batch → 前向反向 → 优化器 step → 清梯度 → 指标），以及指标从 `record_metric()` 攒批、跨 rank 归约、再经 `log_metrics()` 输出的旅程。
- **u6-l2（TransformerTrainModule）**：知道 `trainer.train_module` 是前向/反向/优化计算的实际承担者。
- **u2-l1 / u2-l2（Config 体系）**：知道 dataclass 配置类与「一切皆 Config」的设计模式。

再用通俗语言补几个本讲需要的概念：

- **钩子（hook）**：框架在固定位置预留的「电话插座」。训练循环跑到某个位置（比如「优化器 step 之前」）就挨个呼叫所有注册进来的回调对象的方法。框架代码不用改，功能却能无限扩展——这是典型的**控制反转**：不是你调用框架，而是框架在恰当时机调用你。
- **Stateful**：来自 `torch.distributed.checkpoint.stateful` 的接口，约定 `state_dict()` / `load_state_dict()` 一对方法。`Callback` 继承了它，因此回调自己的内部状态（如计数器、游标）可以随 trainer 状态一起写进 checkpoint、训练中断后恢复。
- **生产者—消费者**：本讲会看到一对典型例子——`SpeedMonitorCallback` 在钩子里调用 `trainer.record_metric("throughput/...")` **生产**指标，`ConsoleLoggerCallback` 在 `log_metrics()` 钩子里**消费**这些指标打印到控制台。二者互不认识，只通过指标名字（支持通配符）耦合。
- **ephemeral checkpoint（临时检查点）**：为可抢占任务频繁保存的「便宜」checkpoint，下次正式保存时会被清理。详见 u6-l4；本讲只需知道回调可以「阻塞」它。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/olmo_core/train/callbacks/callback.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/callback.py) | `Callback` 基类与 `CallbackConfig`：全部钩子的签名、默认空实现、优先级声明。本讲的地基。 |
| [src/olmo_core/train/callbacks/__init__.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/__init__.py) | callbacks 包的门面：20 多个内置回调的再导出清单，是「这个库自带哪些扩展」的一览表。 |
| [src/olmo_core/train/callbacks/checkpointer.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/checkpointer.py) | `CheckpointerCallback`：按间隔保存永久/临时 checkpoint、清理旧 checkpoint。 |
| [src/olmo_core/train/callbacks/speed_monitor.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/speed_monitor.py) | `SpeedMonitorCallback`：统计 TPS/BPS/FLOPs/MFU 等吞吐指标。 |
| [src/olmo_core/train/callbacks/console_logger.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/console_logger.py) | `ConsoleLoggerCallback`：把进度标记与筛选后的指标打印到控制台。 |
| [src/olmo_core/train/callbacks/evaluator_callback.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/evaluator_callback.py) | `EvaluatorCallback` 与两个 `CallbackConfig` 子类（LM 评测 / 下游任务评测）。 |
| [src/olmo_core/train/trainer.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py) | 钩子的**调用方**：所有钩子的触发点都在这里（u6-l1 已精读循环，本讲引用行号对照）。 |
| [src/olmo_core/train/config.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/config.py) | `TrainerConfig`：回调的挂载点（`callbacks` 字段、`with_callback()`）与 `build()` 中 `CallbackConfig` 的处理。 |

辅助引用：[src/test/train/callbacks/callback_order_test.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/train/callbacks/callback_order_test.py)（回调顺序的回归测试）、[src/integration_tests/test_train_small_model.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/integration_tests/test_train_small_model.py)（CPU 可跑的 5 步完整训练，自定义回调的现成范例）。

## 4. 核心概念与源码讲解

本讲拆成五个最小模块：**Callback 基类与钩子全表**、**回调的注册与排序**、**CheckpointerCallback**、**指标的生产与消费（SpeedMonitor + ConsoleLogger）**、**EvaluatorCallback 与 CallbackConfig 模式**。

### 4.1 模块一：Callback 基类——全部钩子与触发时机

#### 4.1.1 概念说明

`Trainer` 的设计哲学是「循环极简，功能外挂」：主循环只保留取数据、前向反向、优化器 step 这条不可压缩的主干，其余一切——存 checkpoint、评测、打日志、监控吞吐、导出模型、发通知——都做成回调。这样做的直接收益是：

1. **主循环稳定**：几十种功能迭代不会把 `fit()` 改成一团乱麻。
2. **功能可组合**：要什么功能就挂什么回调，官方训练脚本（[u9-l1](u9-l1-official-scripts.md)）正是这样按需拼装回调清单的。
3. **执行顺序可推理**：所有回调按 `priority` 排序后依次调用，谁先谁后是确定的（这对分布式训练尤其重要——回调里可能有集合通信，各 rank 必须以相同顺序执行）。

`Callback` 是一个 `@dataclass`，同时继承 `Stateful`（让回调状态能进 checkpoint）：

- [src/olmo_core/train/callbacks/callback.py:L14-L22](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/callback.py#L14-L22)：`Callback` 类声明，docstring 明确说「回调可用来修改和扩展训练循环行为，你可以随时添加自己的回调」。
- [src/olmo_core/train/callbacks/callback.py:L24-L28](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/callback.py#L24-L28)：`priority: ClassVar[int] = 0`——类变量（不占字段），**数值越大越先执行**，默认 0。
- [src/olmo_core/train/callbacks/callback.py:L32-L45](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/callback.py#L32-L45)：每个回调挂载后都持有 `trainer` 引用（反向指针），并提供 `step` 属性直达 `trainer.global_step`——这就是回调能「反过来操作训练器」的通道。

#### 4.1.2 核心流程

把所有钩子按时间轴排开，一次 `fit()` 的完整生命周朽数下来（`pre_log_metrics`/`log_metrics` 依 `metrics_collect_interval` 攒批触发）：

```
build 阶段（TrainerConfig.build → Trainer 构造）
 └─ post_attach()                          每个回调挂上时各一次

fit() 开始
 ├─ pre_train()                            checkpoint 恢复之后、训练开始前
 ├─ (mock batch 预热，无钩子)
 └─ 每个 epoch（_fit_epoch）：
     ├─ pre_epoch()
     └─ 每个 step：
         ├─ pre_load_batch()               向数据加载器要 batch 之前
         ├─ （取 batch、global_step += 1、token/FLOPs 簿记）
         ├─ pre_step(batch)                前向反向之前
         ├─ （train_module.train_batch：前向+反向）
         ├─ pre_optim_step()               优化器 step 之前
         ├─ （train_module.optim_step + zero_grads）
         ├─ post_train_batch()             ← checkpoint 在这里保存
         ├─ post_step()                    ← 在环评测在这里触发
         ├─ pre_log_metrics(step, metrics) 攒批的指标归约完成后
         ├─ log_metrics(step, metrics)
         └─ （停训检查：training_complete 则提前 return）
     └─ post_epoch()                       仅当数据自然耗尽进入下一轮时

fit() 收尾
 ├─ post_train()                           训练正常结束（或已被取消）
 ├─ on_error(exc)                          训练异常退出时（替代 post_train）
 └─ close()                                无论如何总是调用（在异步簿记排空之后）

事件型钩子（不在固定节奏上）
 ├─ post_checkpoint_loaded(path)           成功加载 checkpoint 后
 ├─ post_checkpoint_saved(path)            checkpoint 保存成功后（异步保存在 future 完成时触发）
 └─ state_dict() / load_state_dict()       随 trainer 状态进出 checkpoint
```

三条容易忽略的时序细节（后文实践会验证）：

1. **`post_train_batch` 早于 `post_step`**，且被跳过的 step（`steps_to_skip`）也会走这两个钩子——只有前向反向和优化器 step 被跳过，钩子不跳。
2. **`post_epoch` 不是每次训练都会触发**：按 `Duration.steps(n)` 提前停训时，`_fit_epoch` 在步末检查 `training_complete` 后直接 `return`，不会走到 `post_epoch`。
3. **`log_metrics` 收到的 step 可能是「之前的」step**：指标是攒批异步归约的，输出时落后于当前步是正常的。

#### 4.1.3 源码精读

钩子的**定义**全部在 [src/olmo_core/train/callbacks/callback.py:L71-L171](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/callback.py#L71-L171)，每个都是带 docstring 的默认空实现（`del` 掉未用参数）。挑几个签名看：

```python
def pre_step(self, batch: Dict[str, Any]): ...      # 能看到完整 batch
def pre_log_metrics(self, step: int, metrics: Dict[str, float]): ...
    # docstring：可原地增删改 metrics 字典——指标的「过滤器」入口
def on_error(self, exc: BaseException): ...         # 拿到原始异常
def close(self): ...  # 「Always called right before Trainer.fit() exits, even on an error.」
```

钩子的**调用点**在 `trainer.py`。逐个对照（均为「遍历排序后的回调、依次调用」的循环）：

| 钩子 | 调用点 |
| --- | --- |
| `post_attach` | [trainer.py:L412-L413](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L412-L413)（构造末尾）、[trainer.py:L1402](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1402)（`add_callback`） |
| `pre_train` | [trainer.py:L743-L745](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L743-L745) |
| `pre_epoch` | [trainer.py:L1685-L1686](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1685-L1686) |
| `pre_load_batch` | [trainer.py:L1647-L1658](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1647-L1658)（`_iter_batches`） |
| `pre_step` | [trainer.py:L1708-L1709](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1708-L1709) |
| `pre_optim_step` | [trainer.py:L1720-L1721](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1720-L1721) |
| `post_train_batch` | [trainer.py:L1730-L1731](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1730-L1731) |
| `post_step` | [trainer.py:L1733-L1734](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1733-L1734) |
| `pre_log_metrics` / `log_metrics` | [trainer.py:L1634-L1645](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1634-L1645)（`_check_and_pass_on_metrics`：先全员 `pre_log_metrics`，再全员 `log_metrics`——保证修改对每个消费者可见） |
| `post_epoch` | [trainer.py:L1754-L1755](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1754-L1755) |
| `post_train` | [trainer.py:L777-L778](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L777-L778) |
| `on_error` | [trainer.py:L762-L771](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L762-L771)（异常路径：先 `on_error`，再「不优雅地」关停——不再等簿记、不发集合通信，防止死锁） |
| `close` | [trainer.py:L965-L979](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L965-L979)（`_shutdown`：**先**排空异步簿记与线程池，**再**调 `close`——顺序有讲究，见 4.2） |
| `post_checkpoint_loaded` | [trainer.py:L1124-L1125](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1124-L1125) |
| `post_checkpoint_saved` | 同步保存 [trainer.py:L1195-L1196](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1195-L1196)；异步保存 [trainer.py:L1229-L1240](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1229-L1240)（挂在 future 的完成回调里，保存真正落盘后才触发） |
| `state_dict` / `load_state_dict` | [trainer.py:L986-L1000](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L986-L1000)（trainer 状态里有一项 `"callbacks": {名字: cb.state_dict()}`）、[trainer.py:L1031-L1033](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1031-L1033)（按**名字**匹配恢复） |

另外还有一个「动作型」方法值得知道：[callback.py:L59-L69](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/callback.py#L59-L69) 的 `block_ephemeral_checkpoints()`——回调可以主动登记「阻塞临时 checkpoint」，只要有一个回调登记，`CheckpointerCallback` 就跳过临时保存（模型合并等需要连续权重的场景依赖它，见 [src/integration_tests/test_train_small_model.py:L217-L300](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/integration_tests/test_train_small_model.py#L217-L300) 的测试）。

#### 4.1.4 代码实践：把钩子表「对账」一遍（源码阅读型）

1. **实践目标**：不写代码也能做——用上面的表格逐行到 `trainer.py` 里核对每个调用点，确认你真的能在源码里找到每个钩子的触发位置。
2. **操作步骤**：
   - 打开 [src/olmo_core/train/callbacks/callback.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/callback.py)，数一数钩子方法（提示：共 16 个钩子 + 2 个 state 方法 + 2 个 ephemeral 阻塞方法）。
   - 在 [src/olmo_core/train/trainer.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py) 里搜索 `callback.`，把每个调用点的行号填进自己的表格。
   - 重点核对 `_fit_epoch`（[trainer.py:L1680-L1759](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1680-L1759)）里单步的钩子顺序，注意 `pre_optim_step` 在 `else` 分支内（被跳过的 step 不会触发它，但 `post_train_batch`/`post_step` 在分支外，仍会触发）。
3. **需要观察的现象**：`post_epoch` 的调用点在 `if self.training_complete: ... return` 之后——按步数停训时执行流根本到不了它。
4. **预期结果**：你得到一张与 4.1.2 时间轴完全对应的「钩子—行号」对照表；这也是第 5 节综合实践中验证钩子顺序的判卷标准。

#### 4.1.5 小练习与答案

**练习 1**：想在每个优化器 step 之后立刻读取（快照）模型权重，应该用哪个钩子？为什么不用 `post_step`？

**答案**：`post_train_batch`。它在 `optim_step()` + `zero_grads()` 之后、本 step 末尾其他动作之前触发，且权重已更新完毕；`post_step` 也可以读到权重，但它位于 checkpoint 保存（`post_train_batch` 内）与在环评测（`post_step` 内）之间，语义上「更晚、更杂」。仓库里的真实范例是集成测试中的 `WeightCaptureCallback`，用的正是 `post_train_batch`（[test_train_small_model.py:L45-L57](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/integration_tests/test_train_small_model.py#L45-L57)）。

**练习 2**：`on_error` 和 `close` 都在异常时被调用，它们的分工是什么？

**答案**：`on_error(exc)` 只在训练因异常退出时触发，拿到原始异常，用于「记录/上报这次失败」（如发通知）；`close()` 则无论成功、失败、取消都必然触发，用于释放资源（如关闭 W&B run、上传残余指标）。且异常路径下 `on_error` 先于 `close()`（[trainer.py:L762-L779](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L762-L779)）。

**练习 3**：为什么 `log_metrics(step, metrics)` 的 docstring 强调 step「possibly a previous step」？

**答案**：指标不是每步同步输出的：`record_metric` 先进缓存，按 `metrics_collect_interval` 攒批，经异步簿记线程做跨 rank 归约，最后才经 `_check_and_pass_on_metrics` 按步号排序分发给回调（[trainer.py:L1634-L1645](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1634-L1645)）。异步意味着分发时训练可能已经又前进了若干步，所以回调拿到的可能是「之前某一步」的指标。

### 4.2 模块二：回调的注册、排序与默认回调

#### 4.2.1 概念说明

「回调怎么挂上去、挂上去之后谁先谁后」是使用回调系统前必须弄清的两件事。OLMo-core 的答案：

1. **挂载有两条路**：把 `Callback` 实例放进 `TrainerConfig.callbacks` 字典（通常经 `with_callback()` 链式添加）；或把一个 `CallbackConfig` 放进去，等 `Trainer` 存在后再 `build()` 出真正的回调（详见 4.5——适用于回调本身不可序列化的场景）。
2. **顺序由 `priority` 决定**：所有回调排序后，**每一个钩子轮次都按同一顺序**依次调用。这既是功能需要（如外部日志必须先于 checkpointer 初始化），也是分布式正确性需要（各 rank 顺序必须一致，否则集合通信死锁）——源码注释原话见 [trainer.py:L374-L377](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L374-L377)。
3. **四个默认回调自动补充**：不配置也会有一个「能跑」的训练——控制台日志、checkpoint、速度监控、垃圾回收四件套在 `Trainer` 构造时自动补齐。

#### 4.2.2 核心流程

```
TrainerConfig（callbacks 字典，键=回调名，值=Callback 或 CallbackConfig）
   │  with_callback("名字", cb)   ← 返回深拷贝的新配置（不改动原对象）
   ▼
TrainerConfig.build(train_module, data_loader)
   ├─ 分拣：Callback 实例直接进构造参数；CallbackConfig 留待后置构建
   ├─ Trainer(...) 构造：
   │    1. 缺省补齐：console_logger / checkpointer / speed_monitor / garbage_collector
   │    2. 给每个回调设 trainer 反向指针
   │    3. _sort_callbacks()：按 (priority 降序, 插入顺序) 重排
   │    4. 逐个回调 post_attach()
   └─ 逐个 CallbackConfig.build(trainer) → add_callback(name, cb)（返回 None 则跳过）

之后每个钩子轮次：for callback in self._iter_callbacks(): callback.某钩子()
   （no_checkpoints=True 时过滤掉 CheckpointerCallback；no_evals=True 时过滤掉 EvaluatorCallback）
```

排序规则的精确语义（[trainer.py:L1413-L1423](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1413-L1423)）：以 `(priority, -插入序号)` 为键降序排序——priority 大者在前；priority 相同者按插入先后（先来先服务）。

#### 4.2.3 源码精读

**挂载点**——`TrainerConfig.callbacks` 是一个「名字 → 回调」的字典字段：[src/olmo_core/train/config.py:L49](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/config.py#L49)。名字很重要：checkpoint 里回调状态就按这个名字存取（见 4.1.3 最后一条），重名会直接报错。链式添加方法 `with_callback` / `with_callbacks` 每次深拷贝返回新配置，保证配置对象不可变风格：[config.py:L76-L95](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/config.py#L76-L95)。官方示例 `examples/llm/train.py` 就是十来个 `with_callback` 连缀而成：[src/examples/llm/train.py:L195-L255](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/examples/llm/train.py#L195-L255)。

**build 时的分拣**——[src/olmo_core/train/config.py:L196-L216](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/config.py#L196-L216)：先把 `callbacks` 字典按 `isinstance(cb, CallbackConfig)` 一分为二，实例随构造参数进 `Trainer`；`CallbackConfig` 则在 trainer 就绪后 `cb_config.build(trainer)`，返回非 `None` 才 `trainer.add_callback(name, cb)`：

```python
all_callbacks = kwargs.pop("callbacks")
callbacks = {k: cb for k, cb in all_callbacks.items() if not isinstance(cb, CallbackConfig)}
callback_configs = {k: cb for k, cb in all_callbacks.items() if isinstance(cb, CallbackConfig)}
trainer = Trainer(..., callbacks=callbacks, **kwargs)
for cb_name, cb_config in callback_configs.items():
    if (cb := cb_config.build(trainer)) is not None:
        trainer.add_callback(cb_name, cb)
```

**默认回调自动补齐**——[src/olmo_core/train/trainer.py:L357-L368](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L357-L368)：用 `has_callback(类)` 检查是否已有该类实例，没有才 `setdefault` 补一个默认的。注意判据是**类**不是名字——所以「换掉默认 checkpointer」不能同名顶替（会被 `add_callback` 的重名检查挡住），正确做法是官方 docstring 反复强调的**子类化**（如 `class MyCheckpointer(CheckpointerCallback)`，`has_callback` 按类匹配时子类会命中父类检查）。

**运行期动态添加**——[trainer.py:L1393-L1402](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1393-L1402)：`add_callback` 设指针 → 重新排序 → `post_attach()`，三步原子完成。

**内置回调的优先级全景**（数值越大越先执行）：

| priority | 回调 | 出处 |
| --- | --- | --- |
| 3 | `WandBCallback`、`CometCallback` | [wandb.py:L33](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/wandb.py#L33)、[comet.py:L60](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/comet.py#L60) |
| 2 | `ModelMergeCallback` | [model_merger.py:L37](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/model_merger.py#L37) |
| 1 | `CheckpointerCallback` | [checkpointer.py:L61](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/checkpointer.py#L61) |
| 0 | 默认值（含 `GarbageCollectorCallback`、`EvaluatorCallback` 及你的自定义回调） | [callback.py:L24](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/callback.py#L24) |
| -1 | `GPUMemoryMonitorCallback`、`HFConverterCallback`、`UnshardedModelExportCallback` | [gpu_memory_monitor.py:L18](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/gpu_memory_monitor.py#L18) 等 |
| -2 | `SpeedMonitorCallback` | [speed_monitor.py:L64](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/speed_monitor.py#L64) |
| -10 | `CheckpointReadyNotifierCallback` | [checkpoint_ready_notifier.py:L38](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/checkpoint_ready_notifier.py#L38) |

这套数值不是随手拍的，仓库里有专门的回归测试锁定关键顺序：[src/test/train/callbacks/callback_order_test.py:L6-L10](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/train/callbacks/callback_order_test.py#L6-L10) 断言外部日志回调优先级必须高于 checkpointer，注释给出理由：**pre-train checkpoint 保存会冲刷已记录的指标，所以外部指标接收端（W&B/Comet）必须先初始化**。同文件 [L23-L37](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/train/callbacks/callback_order_test.py#L23-L37) 还锁定了「W&B 在 `close()` 而非 `post_train()` 里终结」——因为最终 checkpoint 恰恰是在优先级更低的 `CheckpointerCallback.post_train()` 里保存的，若 W&B 提前 finish，最后一批指标就丢了。这也解释了 4.1.3 里「`close()` 必须排在异步簿记排空之后」的顺序设计（[trainer.py:L965-L979](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L965-L979)）。

**两个开关过滤器**——[trainer.py:L1425-L1431](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1425-L1431)：`no_checkpoints=True` 时把 `CheckpointerCallback` 从所有钩子轮次中滤掉，`no_evals=True` 同理滤掉 `EvaluatorCallback`——回调还在字典里，但不再被调用。

最后，`fit()` 开头会把最终顺序打印出来（[trainer.py:L732-L734](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L732-L734)，日志标题「Callback order:」），跑任何训练都能在日志里直接看到这份排序结果。

#### 4.2.4 代码实践：预测并验证回调顺序

1. **实践目标**：验证 priority 排序规则，学会用「Callback order:」日志做判卷。
2. **操作步骤**：
   - 运行现成的顺序回归测试：`pytest -v src/test/train/callbacks/callback_order_test.py`（纯 CPU、无依赖，秒级完成）。
   - 做一道预测题：假设某训练挂了 `WandBCallback(3)`、`CheckpointerCallback(1)`、你的自定义回调 `Foo(0)`、`SpeedMonitorCallback(-2)`，写出 `post_train_batch` 钩子轮次里的执行顺序。
   - （可选）跑第 5 节综合实践的小训练，在日志开头找「Callback order:」清单，与你的预测对照。
3. **需要观察的现象**：测试全绿；日志里的回调清单与手工排序一致。
4. **预期结果**：`post_train_batch` 顺序为 WandB → Checkpointer → Foo → SpeedMonitor（`SpeedMonitorCallback` 未实现 `post_train_batch` 则该轮空跑，但**排序位置**不变）。排序本身**待本地验证**（取决于你实际挂载的回调集合）。

#### 4.2.5 小练习与答案

**练习 1**：两个 priority 同为 0 的回调 A、B，A 先插入，谁先执行？

**答案**：A。排序键是 `(priority, -插入序号)` 降序（[trainer.py:L1413-L1423](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1413-L1423)），priority 打平时按插入顺序，先来先服务。

**练习 2**：想让 checkpointer 每 10 步存一次，直接 `with_callback("checkpointer", CheckpointerCallback(save_interval=10))` 会有什么效果？

**答案**：能生效——因为默认回调的补齐逻辑是 `has_callback(CheckpointerCallback)` 检查**类**，你显式挂的就是这个类的实例，默认件不会再补（[trainer.py:L363-L364](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L363-L364)），你的实例顶替其位。官方集成测试正是这么干的：`with_callback("checkpointer", CheckpointerCallback(save_interval=1000))`（[test_train_small_model.py:L183](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/integration_tests/test_train_small_model.py#L183)）。但若你想**新增**第二个 checkpointer 类回调，重名/类检查会阻止——那时才需要子类化。

**练习 3**：`with_callback` 为什么返回新对象而不是原地修改？

**答案**：它先 `deepcopy(self.callbacks)` 再添加（[config.py:L83-L85](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/config.py#L83-L85)），保持 Config 的不可变使用风格：同一份基础配置可以安全地派生出「带评测」「不带评测」等多个变体，互不污染。

### 4.3 模块三：CheckpointerCallback——checkpoint 完全活在回调里

#### 4.3.1 概念说明

u6-l1 强调过：`fit()` 主循环里搜不到「save checkpoint」这个动作——它整个住在 `CheckpointerCallback` 里。这是回调系统「功能外挂」哲学最有力的例证：连如此核心的功能都不进主循环，主循环因此对「要不要 checkpoint、多久存一次、存几个、临时的是否要」一无所知。

这个回调管理四类相互纠缠的事：**永久 checkpoint**（按 `save_interval` 或 `fixed_steps` 保存，用于断点续训与发布）、**临时 checkpoint**（按 `ephemeral_save_interval` 高频保存、下次保存时清理，面向可抢占任务）、**数量上限**（`max_checkpoints` 控制磁盘占用）、**善后清理**（启动时发现上次运行残留的临时 checkpoint 也要清）。异步保存（`save_async`）则让保存发生在单独的 CPU 进程组上，不阻塞训练——细节留给 [u6-l4](u6-l4-checkpointing.md)，本讲只看它在哪些钩子里做事。

#### 4.3.2 核心流程

它只占用三个训练钩子：

```
pre_train():
   ├─ 决定 save_async（后端支持 CPU 通信则默认开）
   ├─ 分布式且异步 → 为 checkpoint 专门建一个进程组（30 分钟超时）
   ├─ 若 step==0 且未从 checkpoint 恢复 → 保存"第 0 步"checkpoint
   └─ 扫描 save_folder，登记上次运行残留的临时 checkpoint（待清理）

post_train_batch()（每步）:
   ├─ 非阻塞地等待上一个异步保存完成；完成后顺带清理"计划删除"的旧 checkpoint
   ├─ step ∈ fixed_steps            → 保存永久 checkpoint
   ├─ step % save_interval == 0     → 保存永久 checkpoint
   ├─ 否则 step % ephemeral_save_interval == 0
   │     且无人阻塞临时保存 且 冷却期已过 → 保存临时 checkpoint
   ├─ 永久 checkpoint 数超 max_checkpoints → 最旧的排入删除计划
   └─ 临时 checkpoint 只保留最新 1 个，旧的排入删除计划

post_train():
   └─ 若当前 step 比最近保存的更新 → 同步保存最终 checkpoint
```

注意「删除」是**两段式**的：先排入 `_checkpoints_to_remove` 计划，等下一个「没有保存正在进行」的 `post_train_batch` 才真正删除——避免删到正在异步写入的目录。

#### 4.3.3 源码精读

类声明与配置字段（[src/olmo_core/train/callbacks/checkpointer.py:L49-L110](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/checkpointer.py#L49-L110)）：`save_interval: Optional[int] = 250`、`ephemeral_save_interval`、`pre_train_checkpoint`、`save_async`、`remove`（清理策略枚举 `CheckpointRemovalStrategy`，[L27-L46](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/checkpointer.py#L27-L46)）、`ephemeral_cooldown`、`fixed_steps`、`max_checkpoints: Optional[int] = 3`。`__post_init__` 做参数校验（间隔至少为 1、临时间隔必须小于永久间隔等，[L125-L139](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/checkpointer.py#L125-L139)）——这是 u2-l1 讲过的「Config 在构造期自检」模式在非 Config dataclass 上的同款应用。

保存决策树的核心（[checkpointer.py:L282-L315](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/checkpointer.py#L282-L315)）：

```python
def post_train_batch(self):
    ...
    self._await_last_checkpoint(blocking=False)      # 非阻塞检查异步保存
    if not self.checkpoint_pending:
        self._remove_old_checkpoints()               # 执行计划中的删除
    if self.fixed_steps is not None and self.step in self.fixed_steps:
        self._checkpoints.append(self._save_checkpoint()); self._trim_checkpoints()
    elif self.save_interval is not None and self.step % self.save_interval == 0:
        self._checkpoints.append(self._save_checkpoint()); self._trim_checkpoints()
    elif (self.ephemeral_save_interval is not None
          and self.step % self.ephemeral_save_interval == 0
          and not self.trainer.block_ephemeral_checkpoints):   # ← 4.1.3 的阻塞机制在这里被消费
        ...冷却期检查...
        self._ephemeral_checkpoints.append(self._save_checkpoint(ephemeral=True))
```

`pre_train` 的启动逻辑（[checkpointer.py:L217-L280](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/checkpointer.py#L217-L280)）：`step == 0` 且不是从 checkpoint 恢复的（`pre_train_checkpoint is None and not trainer.checkpoint_loaded`）就先存一个「第 0 步」checkpoint；随后只有 rank 0 扫描 save_folder 找残留临时 checkpoint（避免多进程一起锤远程存储），再 `broadcast_object` 广播给所有 rank。最终保存（[L317-L325](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/checkpointer.py#L317-L325)）：`post_train` 里若 `step > _latest_checkpoint_step` 则强制**同步**保存（`save_async=False`）——收尾时刻不再需要也不应该异步。

#### 4.3.4 代码实践：预测一个 10 步训练的 checkpoint 目录（源码阅读型）

1. **实践目标**：把决策树用在具体数字上，训练「读代码推行为」的能力。
2. **操作步骤**：设 `CheckpointerCallback(save_interval=4, ephemeral_save_interval=2, fixed_steps=[7], max_checkpoints=2)`，训练 10 步（全新开始、save_folder 为空）。逐步列出 step 1–10 每个 `post_train_batch` 会走哪个分支、保存什么、哪个被排入删除计划，最终目录里剩哪些 `stepN` 目录。
3. **需要观察的现象**：注意 step 4 同时满足 `save_interval` 与 `ephemeral_save_interval`——`elif` 链保证只走永久分支；step 8 同理。
4. **预期结果**：保存发生在 step 0（pre-train）、4、7（fixed）、8、10（post_train 最终保存）；ephemeral 只在 step 2、6 落盘且随时可能只剩最新一个；`max_checkpoints=2` 使 step 0/4 的永久件在 7、8 保存后被排入删除。确切目录内容**待本地验证**（可用第 5 节脚本改参数实测）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `post_train` 里的最终保存要强制 `save_async=False`？

**答案**：训练马上要结束，异步保存的 future 可能还没跑完进程就退出了；同步保存保证 `fit()` 返回时最终 checkpoint 一定完整落盘（[checkpointer.py:L317-L325](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/checkpointer.py#L317-L325)）。

**练习 2**：`ephemeral_save_interval` 为什么必须小于 `save_interval`（`__post_init__` 强制校验）？

**答案**：临时 checkpoint 的意义是「在两次永久保存之间提供更细的恢复点」。若它不小于永久间隔，它要么从不触发（被 `elif` 的永久分支抢占），要么毫无意义地与永久保存重合；校验把配置错误挡在构造期（[checkpointer.py:L128-L137](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/checkpointer.py#L128-L137)）。

**练习 3**：模型合并（`ModelMergeCallback`，priority=2）如何保证合并窗口内的权重不被临时 checkpoint 的清理破坏？

**答案**：它调用 4.1.3 提到的 `block_ephemeral_checkpoints()` 登记阻塞，`trainer.block_ephemeral_checkpoints` 为真时 `CheckpointerCallback` 跳过临时保存分支（[checkpointer.py:L298-L302](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/checkpointer.py#L298-L302)）；集成测试 [test_train_small_model.py:L217-L300](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/integration_tests/test_train_small_model.py#L217-L300) 专门验证了这个窗口行为。

### 4.4 模块四：指标的生产与消费——SpeedMonitorCallback 与 ConsoleLoggerCallback

#### 4.4.1 概念说明

这两个回调放在一起讲，因为它们构成一条完整的**指标流水线**，示范了「回调之间通过 trainer 的指标系统解耦协作」：

- **生产者** `SpeedMonitorCallback`（priority=-2，每轮钩子里几乎最后执行）：在 `pre_load_batch` / `pre_step` / `post_step` 三个钩子里掐表、数 token、算 FLOPs，然后调用 `trainer.record_metric("throughput/...")` 把结果交给 u6-l1 讲过的指标攒批归约系统。
- **消费者** `ConsoleLoggerCallback`（priority=0，自动补齐的默认件之一）：在 `log_metrics` 钩子里收到（已跨 rank 归约的）指标字典，用**通配符模式**筛出关心的条目打印到控制台——默认模式列表里就有 `"throughput/*"`，所以你在训练日志里看到的吞吐行，正是这对生产者/消费者的接力产物。

一个训练不需要 ConsoleLogger 也能跑（W&B/Comet 回调是同类消费者），不需要 SpeedMonitor 也照样训练——这正是解耦的价值。

#### 4.4.2 核心流程

SpeedMonitor 的三段式计时：

```
pre_load_batch:  记 batch 加载开始时刻
pre_step(batch): 加载耗时 = now - 开始时刻（可经 OLMO_BATCH_LOAD_WARN_SECONDS 预警慢加载）
                 统计本步 token 数（除以并行度）、序列长度、FLOPs（num_flops_per_token × token）
                 ★ 第一步只重置不记录（首次加载普遍偏慢，会污染统计）
post_step:       步耗时 = now - 上次 post_step 时刻；总耗时 = now - 首步之后起点
                 record_metric: TPS / TPS(avg) / BPS / BPS(avg) / TFLOPs / MFU / 数据加载占比...
```

MFU（Model FLOPs Utilization，模型算力利用率）的定义来自 PaLM 论文（源码注释附了链接 [speed_monitor.py:L301-L304](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/speed_monitor.py#L301-L304)）：

\[
\text{MFU} = 100 \times \frac{\text{实际 FLOP/s}}{\text{设备峰值 FLOP/s（稠密、半精度）}}
\]

分母来自一张内置的 GPU 峰值表（H100/B200/B300/RTX PRO 6000，其余按 A100 估算，且厂家标称的稀疏峰值乘 0.5 折算稠密，[speed_monitor.py:L23-L51](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/speed_monitor.py#L23-L51)）。

ConsoleLogger 的输出节律：`log_interval`（默认 1）控制进度标记 `[step=N/M,epoch=E,eta=...]` 的频率；`metrics_log_interval`（默认同 `log_interval`，Trainer 自动补齐时被设为 `metrics_collect_interval`，[trainer.py:L358-L362](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L358-L362)）控制完整指标块的频率。

#### 4.4.3 源码精读

**生产侧**——[src/olmo_core/train/callbacks/speed_monitor.py:L158-L224](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/speed_monitor.py#L158-L224)：`pre_load_batch` 只记一个时间戳（L158-159）；`pre_step` 里计算加载耗时、读取环境变量 `OLMO_BATCH_LOAD_WARN_SECONDS` 做慢加载告警（L163-188）、跳过首步（L190-193，注释原话「first one tends to take unusually long」）、按 `_parallel_degree`（非 DP 的并行度）折算本 rank 的 token 数（L195-211）、经 train_module 的 `num_flops_per_token(seq_len)` 累计 FLOPs（L213-224）。[L226-L309](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/speed_monitor.py#L226-L309) 的 `post_step` 则把所有指标一次性 `record_metric` 出去，例如：

```python
tps = self._step_tokens / step_time
self.trainer.record_metric("throughput/device/TPS", tps)
self.trainer.record_metric("throughput/device/BPS (actual avg)", bps_avg)
...
mfu = 100 * flops_ps / self.device_peak_flops_per_second
self.trainer.record_metric("throughput/device/MFU", mfu)
```

还有两个有意思的副产物：`throughput/chinchilla multiple`（已见 token 数 / (20 × 参数量)，Chinchilla 剂量参考，[L263-L271](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/speed_monitor.py#L263-L271)）；`data loading (%)` 用 `ReduceType.max` 归约——瓶颈看最慢的 rank，而不是平均（[L291-L294](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/speed_monitor.py#L291-L294)）。

**消费侧**——[src/olmo_core/train/callbacks/console_logger.py:L33-L51](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/console_logger.py#L33-L51)：默认关心的指标是一个**fnmatch 通配符模式列表**（`train/CE loss`、`optim/LR*`、`throughput/*`、`gpu_memory/*`……）。[L66-L80](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/console_logger.py#L66-L80) 的 `log_metrics` 用 `any(fnmatch(name, pat) for pat in self.metrics)` 逐条过滤后打印，并带上含 ETA 的进度头：

```python
prefix = self._get_progress_marker(step, include_eta=True)
log.info(f"{prefix}\n" + "\n".join(
    f"    {name}={format_float(value)}"
    for name, value in metrics.items()
    if any(fnmatch(name, pat) for pat in self.metrics)))
```

[L56-L64](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/console_logger.py#L56-L64) 的 `post_step` 则处理「这一步不会输出指标块」的普通步：只打一行光秃秃的 `[step=N/M,epoch=E]` 进度标记。ETA 来自 `trainer.training_progress.time_remaining`，若配置了 `hard_stop` 还会标注 `(hard stop)`（[L82-L89](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/console_logger.py#L82-L89)）。

#### 4.4.4 代码实践：让控制台多显示一个指标

1. **实践目标**：体验「消费者模式列表」的过滤机制——不写生产者，只改消费端配置。
2. **操作步骤**：在第 5 节综合实践的脚本里，把 trainer 配置改为 `.with_callback("console_logger", ConsoleLoggerCallback(log_interval=1, metrics=["train/CE loss", "train/PPL", "throughput/*"]))`（需 `from olmo_core.train.callbacks import ConsoleLoggerCallback`）。跑完后把 `"throughput/*"` 换成 `"optim/*"` 再跑一次，对比两次的控制台输出。
3. **需要观察的现象**：第一次每次都打印 CE loss、PPL 和全部 `throughput/` 指标；第二次吞吐行消失、出现 `optim/LR` 等条目。
4. **预期结果**：输出严格随模式列表变化——证明 ConsoleLogger 只是个「按通配符订阅」的消费者；`record_metric` 的指标名就是订阅频道。**待本地验证**（依赖第 5 节脚本跑通）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `SpeedMonitorCallback` 要丢弃第一个 step 的统计？

**答案**：首个 batch 通常包含数据集 `prepare()`（索引构建）、JIT/编译、缓存冷启动等一次性开销，会把均值拉偏；首步只用来初始化计时起点（[speed_monitor.py:L190-L193](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/speed_monitor.py#L190-L193)、[L232-L241](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/speed_monitor.py#L232-L241)）。

**练习 2**：`throughput/device/data loading (%)` 为什么用 `ReduceType.max` 而不是默认的 mean？

**答案**：数据加载占比反映的是「流水线瓶颈」：只要有一个 rank 数据供给不上，所有 rank 都得等它（集合通信同步）。看最坏值才能暴露瓶颈 rank；平均值会把单个慢 rank 稀释掉（[speed_monitor.py:L291-L294](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/speed_monitor.py#L291-L294)，`ReduceType` 语义见 u6-l1）。

**练习 3**：你的自定义回调 `record_metric("my_metric", v)` 之后，日志里却看不到它，最可能的原因是什么？

**答案**：`ConsoleLoggerCallback` 的 `metrics` 模式列表没匹配上你的指标名。要么把名字改成匹配现有模式（如 `throughput/my_metric`），要么把 `my_metric`（或 `my_*`）加进 `ConsoleLoggerCallback.metrics`；也可以改在自己的 `log_metrics` 钩子里直接打印。

### 4.5 模块五：EvaluatorCallback 与 CallbackConfig 模式

#### 4.5.1 概念说明

`EvaluatorCallback`（在环评测，[u8-l1](u8-l1-evaluation.md) 会深入 eval 包本身）负责训练途中周期性跑评测集。它示范了两件新事物：

1. **回调可以反过来「指挥」训练器**：`cancel_after_first_eval=True` 时它在评测后调用 `trainer.cancel_run()` 取消整个训练——配合 `eval_on_startup=True` 就是「只跑一轮评测不训练」的评测专用模式。
2. **`CallbackConfig` 模式**：`EvaluatorCallback` 持有 `List[Evaluator]` 这类**不可序列化**的运行期对象，没法作为 dataclass 字段安全地走 YAML 往返（u2-l1 的 Config 体系要求字段可序列化）。解法是把「可序列化的配置」与「运行期回调」分成两个类：`LMEvaluatorCallbackConfig(CallbackConfig)` 只存数据集配置、间隔等简单字段，等 `Trainer` 就绪后再 `build(trainer)` 出真正的 `EvaluatorCallback`。`CallbackConfig` 本身就是 `Callback + Config` 的双继承（[callback.py:L174-L185](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/callback.py#L174-L185)，docstring 直说这是「回调类本身无法序列化时的替代方案」），`build()` 返回 `Optional[Callback]`——返回 `None` 表示「本次不启用」（如 `enabled=False`），Trainer 会静默跳过（见 4.2.3 的 build 分拣代码）。

#### 4.5.2 核心流程

```
LMEvaluatorCallbackConfig（可序列化：eval_dataset, eval_interval, ...）
   │ TrainerConfig.build() 分拣出 CallbackConfig
   │ trainer 就绪后调用 build(trainer)：
   │    1. 校验评测集序列长度 ≤ train module 的最大可评测序列长度
   │    2. 换算 global_eval_batch_size（tokens 或 instances 两种单位）
   │    3. dataset.build() → 必须是 NumpyPaddedFSLDataset
   │    4. LMEvaluator.from_numpy_dataset(..., collator=训练数据加载器的 collator)
   │    └─ 返回 EvaluatorCallback(evaluators=[...], ...)
   ▼
EvaluatorCallback 运行期：
   post_attach: 校验 train_module 类型必须是 Transformer 系
   pre_train:  eval_on_startup=True → 立即评测一次
   post_step:  step>1 且 (step % eval_interval == 0 或 ∈ fixed_steps) → perform_eval()
   post_train: eval_on_finish=True → 收尾再评测一次
   perform_eval: 逐 evaluator 遍历其 batch → train_module.eval_batch（no_grad）
                 → evaluator.compute_metrics() → record_metric("eval/lm/...")
                 可选 cancel_run(no_sync=True)
```

#### 4.5.3 源码精读

钩子侧（[src/olmo_core/train/callbacks/evaluator_callback.py:L98-L120](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/evaluator_callback.py#L98-L120)）：

```python
def post_attach(self):
    if not isinstance(self.trainer.train_module, (TransformerTrainModule, OLMoDDPTrainModule)):
        raise OLMoConfigurationError(...)          # 挂载期就把不兼容拦下

def post_step(self):
    if self.step <= 1: return                       # 第 1 步不评（刚初始化的模型没有评测价值）
    if (self.eval_interval is not None and self.step % self.eval_interval == 0) or (
        self.fixed_steps is not None and self.step in self.fixed_steps):
        self.perform_eval()
```

评测主体 `perform_eval`（[L122-L217](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/evaluator_callback.py#L122-L217)）：对每个 evaluator 循环取 batch，`move_to_device` 后在 `torch.no_grad()` 下调 `self.trainer.train_module.eval_batch(batch, labels=labels)` 拿到 logits 与未归约 CE loss 喂给 `evaluator.update_metrics`；`eval_duration.due(...)` 控制每次评测跑多久（默认 1 个 epoch）；结果以 `record_metric(f"{prefix}/{evaluator.name}/{name}", value)` 记录（即日志里的 `eval/lm/CE loss` 一类名字），最后还记录评测耗时（`MetricMergeStrategy.sum`）并在 `cancel_after_first_eval` 时调用 `self.trainer.cancel_run(..., no_sync=True)`（[L213-L217](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/evaluator_callback.py#L213-L217)，`no_sync` 是因为所有 rank 都会同时走到这里，各自取消自己即可、无需再通信协商）。

构建侧 `LMEvaluatorCallbackConfig.build`（[L239-L303](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/evaluator_callback.py#L239-L303)）：`enabled=False` 直接 `return None`（L240-241）；序列长度与 batch size 的双重校验（L250-272）；要求评测集必须是 `NumpyPaddedFSLDataset`（L274-278，padded 变体保证 eval batch 形状规整）；复用**训练**数据加载器的 collator（L285-293）保证训练与评测的张量组织方式一致。`DownstreamEvaluatorCallbackConfig`（[L475-L530](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/evaluator_callback.py#L475-L530)）走同样的套路，构建的是依赖外部 `olmo_eval` 包的下游任务评测器。

一键挂载两个评测回调的便捷方法在 `TrainerConfig.with_recommended_evals()`：[src/olmo_core/train/config.py:L97-L145](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/config.py#L97-L145)。

#### 4.5.4 代码实践：读懂 build() 里的三道防线（源码阅读型）

1. **实践目标**：理解 `CallbackConfig.build()` 为什么需要 trainer 参数——它要在构建期做大量「对着 trainer 校验」的工作。
2. **操作步骤**：通读 [evaluator_callback.py:L239-L303](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/evaluator_callback.py#L239-L303)，列出所有会抛 `OLMoConfigurationError` 的条件，并注明各用了 trainer 的什么信息（提示：`trainer.train_module.eval_batch_spec`、`trainer.data_loader.collator`、`trainer.dp_process_group`）。
3. **需要观察的现象**：所有校验都发生在**训练开始之前**（build 阶段），而不是跑到第 1000 步该评测时才炸。
4. **预期结果**：至少找到三处抛错：train module 类型不符（post_attach，另一处）、评测序列长度超限、数据集/数据加载器类型不符。体会「fail fast」：错误配置应当在构建期死亡。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `LMEvaluatorCallbackConfig` 不直接是 `EvaluatorCallback` 的字段容器（即为什么需要 CallbackConfig 这一层）？

**答案**：`EvaluatorCallback.evaluators` 是 `List[Evaluator]`——持有 DataLoader、metric 对象等运行期资源，不可 YAML 序列化。Config 体系（u2-l1）要求字段可序列化才能往返与命令行覆盖，所以拆成「可序列化的配方」（`...CallbackConfig`）与「构建产物」（`EvaluatorCallback`），中间靠 `build(trainer)` 桥接。

**练习 2**：`build()` 返回 `None` 意味着什么？Trainer 会怎么处理？

**答案**：意味着「此回调本次不启用」，典型触发是 `enabled=False`。TrainerConfig.build 里 `if (cb := cb_config.build(trainer)) is not None:` 才 `add_callback`，`None` 被静默跳过（[config.py:L213-L216](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/config.py#L213-L216)）——这让「开关一个回调」成为纯配置行为。

**练习 3**：`eval_on_startup=True` + `cancel_after_first_eval=True` 组合出什么使用模式？

**答案**：「只评测、不训练」模式：启动即评测一次，随后回调主动 `cancel_run()` 结束训练（[evaluator_callback.py:L105-L107](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/evaluator_callback.py#L105-L107)、[L213-L217](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/evaluator_callback.py#L213-L217)），docstring 明说这用于「只想跑在环评测」的场景。

## 5. 综合实践：写一个自己的回调，跑 5 步训练验证钩子顺序

这是本讲的主实践，把五个模块串起来：定义回调（4.1）→ 挂到 TrainerConfig（4.2）→ 观察 checkpoint 行为（4.3）→ 在 `log_metrics` 里消费 loss 与吞吐指标（4.4）。范本是仓库自带的 CPU 可跑集成测试 [src/integration_tests/test_train_small_model.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/integration_tests/test_train_small_model.py)（它内部就定义了一个自定义 `WeightCaptureCallback`，见 [L45-L57](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/integration_tests/test_train_small_model.py#L45-L57)）。

**任务**：实现 `StepPrinterCallback`——记录自己被调用的钩子顺序，每个 step 结束时打印当前 step，指标到达时打印 loss 与吞吐。

**准备数据**（二选一）：

- 联网：直接复用官方小数据 `http://olmo-data.org/examples/c4-en/gpt2/c4-train.00000-00099.npy`（集成测试同款）。
- 纯本地：按 u4-l1 学过的裸数组格式自己造（注意用 `np.memmap` 生成、token id 必须小于 padded 词表大小）：

```python
# 示例代码：生成本地随机数据集（约 8000 条 64-token 序列）
import numpy as np
from olmo_core.data import TokenizerConfig
vocab = TokenizerConfig.gpt2().padded_vocab_size()
data = np.memmap("/tmp/random_tokens.npy", dtype=np.uint32, mode="w+", shape=(64 * 8000,))
data[:] = np.random.randint(0, vocab, size=64 * 8000)
```

**完整脚本**（示例代码，保存为 `callback_practice.py`；基于 [test_train_small_model.py:L138-L211](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/integration_tests/test_train_small_model.py#L138-L211) 改写）：

```python
# 示例代码：自定义回调 + 5 步 CPU 小训练
import logging
from dataclasses import dataclass, field
from typing import Dict, List

from olmo_core.data import NumpyDataLoaderConfig, NumpyFSLDatasetConfig, TokenizerConfig
from olmo_core.nn.transformer import TransformerConfig
from olmo_core.optim import AdamWConfig
from olmo_core.train import Duration, TrainerConfig
from olmo_core.train.callbacks import CheckpointerCallback
from olmo_core.train.callbacks.callback import Callback
from olmo_core.train.train_module import TransformerTrainModuleConfig
from olmo_core.utils import seed_all

logging.basicConfig(level=logging.INFO)

@dataclass
class StepPrinterCallback(Callback):
    """每个 step 结束打印 step 号；指标到达时打印 loss 与吞吐；全程记录钩子顺序。"""
    trace: List[str] = field(default_factory=list)

    def _hit(self, name: str):
        self.trace.append(f"{name}@step{self.step}")

    def post_attach(self): self._hit("post_attach")
    def pre_train(self):   self._hit("pre_train")
    def pre_epoch(self):   self._hit("pre_epoch")
    def pre_load_batch(self): self._hit("pre_load_batch")
    def pre_step(self, batch): self._hit("pre_step")
    def pre_optim_step(self): self._hit("pre_optim_step")
    def post_train_batch(self): self._hit("post_train_batch")

    def post_step(self):
        self._hit("post_step")
        print(f">> [StepPrinter] step={self.step} 完成")   # 1) 当前 step

    def log_metrics(self, step: int, metrics: Dict[str, float]):
        self._hit("log_metrics")
        # 2) loss 与吞吐：从归约后的指标字典里取（生产者是 SpeedMonitorCallback）
        ce = metrics.get("train/CE loss")
        tps = metrics.get("throughput/device/TPS (actual avg)")
        print(f">> [StepPrinter] step={step} CE loss={ce} TPS(avg)={tps}")

    def post_train(self): self._hit("post_train")
    def close(self):      self._hit("close")

def main():
    seed_all(42)
    tokenizer = TokenizerConfig.gpt2()
    printer = StepPrinterCallback()

    trainer_config = (
        TrainerConfig(
            save_folder="/tmp/callback-practice",
            save_overwrite=True,
            metrics_collect_interval=1,          # 每步都产出指标块，便于观察
            cancel_check_interval=1,
            max_duration=Duration.steps(5),
        )
        .with_callback("checkpointer", CheckpointerCallback(save_interval=1000))
        .with_callback("step_printer", printer)  # priority=0，自定义回调
    )
    config = dict(
        model=TransformerConfig.olmo3_30M(vocab_size=tokenizer.padded_vocab_size()),
        dataset=NumpyFSLDatasetConfig(
            paths=["/tmp/random_tokens.npy"],     # 或官方 c4 数据 URL
            sequence_length=64, tokenizer=tokenizer, work_dir="/tmp/dataset-cache",
        ),
        data_loader=NumpyDataLoaderConfig(global_batch_size=64 * 4, seed=0, num_workers=0),
        train_module=TransformerTrainModuleConfig(
            rank_microbatch_size=64 * 2, max_sequence_length=64,
            optim=AdamWConfig(lr=1e-3), compile_model=False,
        ),
        trainer=trainer_config,
    )

    model = config["model"].build(init_device="meta")
    train_module = config["train_module"].build(model)
    dataset = config["dataset"].build()
    data_loader = config["data_loader"].build(dataset, dp_process_group=train_module.dp_process_group)
    trainer = config["trainer"].build(train_module, data_loader)
    trainer.fit()

    print("钩子触发轨迹：")
    print("\n".join(printer.trace))

if __name__ == "__main__":
    main()
```

**操作步骤**：

1. 安装好环境（u1-l2），生成数据文件，运行 `python callback_practice.py`（单进程 CPU 即可，无需 torchrun——集成测试 `test_train_small_model_cpu` 也是直接 `trainer.fit()`）。
2. 观察三处输出：训练日志开头的 **「Callback order:」** 清单；你的 `>> [StepPrinter]` 打印行；脚本末尾的钩子轨迹。
3. 把轨迹与 4.1.2 的时间轴逐行比对。
4. 改实验：`metrics_collect_interval` 改成 5 再跑，观察 `log_metrics` 触发次数变化；`max_duration` 改成 `Duration.epochs(1)`（数据量小到一轮就停），观察 `post_epoch` 是否出现。

**预期结果**（关键判据）：

1. 轨迹开头形如：`post_attach@step0 → pre_train@step0 → pre_epoch@step0 → pre_load_batch@step1 → pre_step@step1 → pre_optim_step@step1 → post_train_batch@step1 → post_step@step1 → log_metrics@step1 → ...`（5 步循环）`→ post_train@step5 → close@step5`。
2. **`post_epoch` 不出现**——按 steps 停训走的是提前 return 路径（4.1.2 细节 2），除非你做了第 4 步的 epochs 实验。
3. CE loss 初始应在 \(\ln(50304) \approx 10.8\) 附近（随机初始化模型对 5 万词表的均匀预测），并随 5 步略微下降；TPS(avg) 有非零读数（SpeedMonitor 丢弃首步，若 5 步内统计窗口异常属正常，可加大步数观察）。
4. 「Callback order:」里 checkpointer 排在你的回调之前（priority 1 > 0），speed_monitor 在最后（-2）。
5. `/tmp/callback-practice` 下出现 `step0`（pre-train）与 `step5`（最终）两个 checkpoint 目录。

以上现象为基于源码的推断，**待本地验证**；若某条不符，回到 4.1 的钩子对照表排查是你最好的练习。

## 6. 本讲小结

- `Callback` 基类定义 16 个钩子，覆盖「挂载 → 训练前 → 每轮 → 每步（加载/前向反向前/优化前/批后/步后）→ 指标 → 轮末 → 训练后/出错 → 关闭」全生命周期，外加 checkpoint 事件钩子与 `Stateful` 状态进出 checkpoint；所有触发点都在 `trainer.py`，可逐行对账。
- 回调经 `TrainerConfig.callbacks`（`with_callback()` 链）挂载，按 `priority` 降序（同优先级按插入序）在**每个钩子轮次**统一排序执行；console_logger / checkpointer / speed_monitor / garbage_collector 四个默认回调自动补齐，按类判重、子类化覆盖。
- 顺序是被测试锁死的设计契约：外部日志（3）必须先于 checkpointer（1）初始化，`close()` 必须晚于最终 checkpoint 与簿记排空——`callback_order_test.py` 是这份契约的文档。
- `CheckpointerCallback` 证明「核心功能也走回调」：永久/临时/定点三类保存全在 `post_train_batch` 的决策树里，删除是两段式计划执行；`SpeedMonitorCallback` 生产 `throughput/*` 指标、`ConsoleLoggerCallback` 按通配符消费，构成解耦的指标流水线。
- 持有不可序列化对象的回调走 `CallbackConfig` 双层模式：Config 存配方、`build(trainer)` 时对着 trainer 做校验并构建真身，返回 `None` 即「不启用」。

## 7. 下一步学习建议

回调系统把「存什么、怎么存」外包给了 `CheckpointerCallback`，但 checkpoint 内部的世界——分布式保存/加载、重分片、异步后端、`LoadStrategy` 恢复策略——还没打开。下一讲 [u6-l4：Checkpoint 体系](u6-l4-checkpointing.md) 将顺着 `trainer.save_checkpoint()` 往下钻进 `train/checkpoint.py` 与 `distributed/checkpoint/`，并实践一次完整的「中断—续训」。若你想先看「回调还能干什么」，可浏览 [src/olmo_core/train/callbacks/__init__.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/__init__.py) 的清单再挑一两个读源码（推荐 `wandb.py` 与 `sequence_length_scheduler.py`）；评测细节则在 [u8-l1](u8-l1-evaluation.md) 展开。

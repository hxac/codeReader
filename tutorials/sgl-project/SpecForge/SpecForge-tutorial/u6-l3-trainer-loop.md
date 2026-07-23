# Trainer 与 TrainerController

## 1. 本讲目标

学完本讲，读者应该能够：

- 说清 `Trainer.fit()` 这个「全包唯一的训练入口」在进入、正常结束、异常退出三条路径上各自做了什么，以及它为什么是「唯一」的。
- 读懂 `TrainerController` 的 epoch 循环：它如何遍历 epoch 与 batch，以及为什么 `global_step` 只在梯度累积边界处自增。
- 理解 `TrainerCore.train_step` 返回的 `optimizer_stepped` 为什么被称为「单一权威边界信号」，以及这条信号如何驱动 durable ack、检查点、评测、日志这一组「边界动作」。
- 解释「训练流自然结束必须落在 optimizer 边界」这条硬约束的用意。

本讲承接 [u6-l1 训练装配](u6-l1-training-assembly.md)：装配层（`build_training_run`）只负责把对象接线、产出可 `.run()` 的 `TrainingRun`；本讲则进入 `TrainingRun.trainer.fit()` 真正开始训练之后，控制权在 `Trainer → TrainerController → TrainerCore` 三者之间如何流动。

## 2. 前置知识

本讲默认读者已了解以下概念（前序讲义已建立）：

- **草稿训练策略 `DraftTrainStrategy`**（[u6-l2](u6-l2-train-strategy.md)）：每个算法（EAGLE3 / DFlash / Domino / DSpark / P-EAGLE）各自实现的「batch 怎么算 loss」插件，对外暴露 `forward_loss(batch, ctx)`、`checkpoint_state_filter` 等方法。本讲里 `TrainerCore` 只是无分支地调用它，算法差异全被收敛进策略插件。
- **梯度累积（gradient accumulation）**：当显存装不下一个完整大 batch 时，把一个大 batch 拆成多个 micro-batch，分别前向/反向累加梯度，攒够 N 个 micro-batch 后才执行一次 optimizer step。这个 N 就是 `accumulation_steps`。在本讲里，「边界（boundary）」这个词几乎总是指「攒够 N 个、该做 optimizer step 的那个 micro-batch」。
- **FSDP 训练后端**（详见 [u6-l4](u6-l4-fsdp-backend.md)）：负责模型包裹、反向、optimizer step、分布式梯度范数；本讲里它以 `self.backend` 出现，`TrainerCore` 调它的 `backward(loss, is_boundary=...)` 与 `step()`。
- **跨平面契约与 `TrainBatch`**（[u5-l4](u5-l4-runtime-contracts.md)）：`TrainBatch` 是数据面唯一允许携带张量的契约，也是 `TrainerCore.train_step` 的入参。`batch.sample_ids` 是本讲 durable ack 追踪的对象身份。
- **控制面只传元数据**（[u5-l4](u5-l4-runtime-contracts.md)）：durable ack 写入的是「样本 id + 全局步号」这样的元数据，而不是张量。

一个贯穿全讲的关键词是**边界（boundary）**。请把它理解为「一次真正发生、不可撤销的 optimizer step」。本讲的核心设计就是：整条训练链路里，所有需要「在 optimizer 真正 step 之后才能做」的动作——自增 `global_step`、提交 durable ack、写检查点、跑评测、打日志——**全部且只**由同一个布尔信号 `optimizer_stepped` 触发。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用它做什么 |
|---|---|---|
| [specforge/training/trainer.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/trainer.py) | 领域层 `Trainer`：调用者面对的唯一训练对象 | 讲 `fit()` 的进入/退出/异常清理三段式生命周期 |
| [specforge/training/controller.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/controller.py) | `TrainerCore`（一步）+ `TrainerController`（生命周期） | 讲 epoch 循环、`global_step` 边界计数、durable ack、检查点 |
| [specforge/training/DESIGN.md](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/DESIGN.md) | 训练面设计说明与流程图 | 用它的 mermaid 图与「单一权威边界信号」原话锚定结论 |
| [tests/test_runtime/test_trainer.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/tests/test_runtime/test_trainer.py) | 训练器单元测试 | 代码实践与练习的断言锚点 |

整体调用关系（从外到内）：

```
Trainer.fit()                         # 领域层：生命周期外壳
  └─ TrainerController.fit(loader)    # 控制层：epoch 循环 + 边界动作
       └─ TrainerCore.train_step()    # 算子层：一步前向/反向/（边界处）step
            └─ strategy.forward_loss  # 算法插件（u6-l2）
            └─ backend.backward/step  # FSDP 后端（u6-l4）
```

三层职责的官方分工写在 [DESIGN.md:22-31](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/DESIGN.md#L22-L31)：`Trainer` 拥有拓扑清理与最终保存，`TrainerController` 拥有 epoch 循环 / optimizer-step 计数 / interval 检查点 / durable ack，`TrainerCore` 拥有「无分支的一步」与累积边界，`DraftTrainStrategy` 拥有模型相关的前向/损失/投影/过滤。

## 4. 核心概念与源码讲解

### 4.1 Trainer.fit 生命周期

#### 4.1.1 概念说明

`Trainer` 是「领域层」的训练对象，也是整个包里**唯一**对调用者暴露的训练入口。它的核心方法 `fit()` 不接受任何业务参数（只有 `self`），所有训练所需的配置都在构造时注入完毕。这一点有测试明确锁死：`Trainer.fit` 的签名只能有 `self`，不允许出现「评测数据」之类的旁路参数（见 [test_public_trainer_fit_has_no_eval_side_channel](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/tests/test_runtime/test_trainer.py#L453-L456)）。这样做的好处是：评测、检查点、日志、durable ack 这些「副作用」全部在构造期就配置好，`fit()` 只负责把它们按正确顺序跑完并保证清理，CLI、直接构造 `Trainer` 的 builder、Python 调用者三者行为完全一致。

`fit()` 解决的核心问题是**生命周期的健壮性**：无论训练是正常结束、中途命中 `max_steps`、还是抛异常，都必须把该关的资源关掉、该发的终态信号发出去、不该重复写的检查点不写。它把「真正干活」委托给内部的 `TrainerController`，自己只做三件事——进入拓扑上下文、在最后兜底保存一次检查点、用 `try/except/finally` 包住所有清理。

#### 4.1.2 核心流程

`fit()` 的执行流程可以概括为「正常路径 + 两个保护层」：

```text
fit()
├─ try:
│    ├─ 进入 fit_context（拓扑拥有的流上下文，如在线 consumer 的分发器）
│    ├─ step = TrainerController.fit(loader)   # 真正的 epoch 循环
│    ├─ 若 step > 0 且这一步还没被周期检查点保存过 → save_checkpoint()  # 兜底保存
│    ├─ close_loader()                          # 停止预取、给每个未产出租约一个明确结局
│    └─ on_fit_success(step)                    # 发布 consumer_done 等成功信号
├─ except BaseException as exc:
│    └─ on_fit_failure(exc)                     # 发布失败信号，然后重新 raise
└─ finally:
     ├─ close_loader()（若尚未尝试）
     ├─ TrainerController.close_profiler()      # 收尾 profile 窗口
     ├─ on_fit_finally()                        # 例如停止 rank-0 引用分发器
     ├─ logger.close()
     └─ 把清理阶段新发生的异常作为 note 挂到主异常上（不吞掉、不掩盖）
```

两个关键设计点：

1. **兜底保存的判定**：`step > 0 and self.last_checkpoint_step != step`。意思是「确实训了至少一步，且最后这一步没被周期检查点路径写过」。如果周期路径（`save_interval`）正好在最后一步保存过，这里就不重复写，避免把一个已保存的步用「改过的 epoch 计数器」重写成新检查点。
2. **清理异常不掩盖主异常**：`finally` 里若清理动作本身抛错，不会覆盖原始的训练异常，而是用 `add_note` 附加说明；只有当本来没有主异常时，清理异常才会被抛出。

#### 4.1.3 源码精读

先看 `fit()` 的正常路径与兜底保存。`close_loader()` 被特意放在 `on_fit_success` **之前**——注释点明原因：分布式 consumer 在预取停止、每个未产出的租约都有了明确结局之前，不算成功（[specforge/training/trainer.py:511-536](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/trainer.py#L511-L536)）：

```python
def fit(self) -> int:
    """Run training and configured evaluation through one lifecycle."""
    loader_close_attempted = False

    def close_loader() -> None:        # 停止预取 / 结算未产出租约
        ...
    try:
        context = (
            self._fit_context if self._fit_context is not None else nullcontext()
        )
        with context:
            step = self._controller.fit(self._loader)   # 真正的 epoch 循环
        if step > 0 and self.last_checkpoint_step != step:
            self.save_checkpoint()                       # 兜底：保存未被周期保存的最后一步
        close_loader()                                   # 先于 success 信号结算租约
        if self._on_fit_success is not None:
            self._on_fit_success(step)
        return step
```

再看异常路径：`on_fit_failure` 收到异常后**重新 raise**，保证训练异常不被吞掉（[specforge/training/trainer.py:537-540](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/trainer.py#L537-L540)）：

```python
    except BaseException as exc:
        if self._on_fit_failure is not None:
            self._on_fit_failure(exc)
        raise
```

`finally` 段是健壮性的核心：它用一个 `capture_cleanup` 辅助函数把每个清理动作包起来，收集异常而非让它打断后续清理；最后根据「是否存在主异常」决定是把清理异常作为 note 挂上去，还是把它作为新的主异常抛出（[specforge/training/trainer.py:541-587](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/trainer.py#L541-L587)）。这段保证了「无论怎样退出，profiler、loader、logger、分发器都会被收尾」。

`fit()` 之外，`Trainer` 还暴露三个受保护的属性把运行时内部对象透出来供检查（[specforge/training/trainer.py:499-509](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/trainer.py#L499-L509)）：

```python
@property
def global_step(self) -> int:
    return self._controller.global_step     # 委托给 controller，单一来源
```

`global_step`、`micro_step`、`last_checkpoint_step` 都是只读 property，全部委托给 `self._controller`——`Trainer` 自己不维护训练进度，进度只活在 `TrainerController` 里，避免两份会漂移的计数器。

#### 4.1.4 代码实践

**实践目标**：通过阅读测试，确认 `Trainer.fit` 的公开契约——它不接受任何业务参数，且 `Trainer` 把生命周期清理的责任扛在自己身上。

**操作步骤**：

1. 打开 [tests/test_runtime/test_trainer.py:453-456](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/tests/test_runtime/test_trainer.py#L453-L456)，读 `test_public_trainer_fit_has_no_eval_side_channel`。
2. 打开 [specforge/training/trainer.py:511](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/trainer.py#L511)，对照 `def fit(self) -> int:` 的签名。
3. 在 [trainer.py:541-587](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/trainer.py#L541-L587) 的 `finally` 段里，数一下 `capture_cleanup` 被调用了几次、分别清理什么。

**需要观察的现象**：测试用 `inspect.signature` 断言 `Trainer.fit` 的参数列表**恰好等于** `["self"]`；`fit` 的实现里，评测、检查点都不出现在参数里，而是从构造期注入的 `self._controller` / `self._on_fit_success` 等回调里来。

**预期结果**：你能用一句话说明「为什么 `fit()` 没有参数反而更安全」——因为所有副作用源在构造期就钉死了，不存在第二条带参数的训练入口会绕过清理逻辑。

> 说明：本实践为「源码阅读型」，不需要 GPU；如需在本地真正跑一遍带异常注入的 `fit()`，可参考 `test_fit_and_checkpoint`（[tests/test_runtime/test_trainer.py:384](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/tests/test_runtime/test_trainer.py#L384)）用 `FakeStrategy`/`FakeBackend` 的写法，待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`fit()` 里兜底保存的判定是 `step > 0 and self.last_checkpoint_step != step`。如果把后半句去掉，只保留 `if step > 0: self.save_checkpoint()`，会出什么问题？

**参考答案**：当最后一步恰好被周期检查点（`save_interval`）保存过时，会重复写一次检查点；更糟的是，此时 controller 内部的 epoch 计数器可能已经推进（详见 4.2.3「epoch 结束时把 epoch 设为下一个」），重复写会把一个「已完成」的 run 用「改过的 epoch 计数」写成新检查点，导致 resume 时重放数据。`last_checkpoint_step != step` 正是为了避免这种重复。

**练习 2**：`on_fit_failure` 收到异常后为什么还要 `raise`，而不是把异常「处理掉」让进程正常退出？

**参考答案**：训练异常必须向上传播，让 CLI 的进程生命周期（u3-l1 讲过的 `_worker_signal_unwind` 与退出码 `128+signum`）和上层调用者知道这次 run 失败了；`on_fit_failure` 的职责只是「发布失败信号/记日志」这类副作用，不是「吞异常」。吞掉异常会让一次失败的训练看起来像成功。

---

### 4.2 TrainerController 的 epoch 循环

#### 4.2.1 概念说明

`TrainerController` 是「控制层」，拥有训练的生命周期：epoch 循环、optimizer-step 计数、interval 检查点、durable ack。它从 `Trainer.fit()` 拿到一个私有的 loader（数据流），然后驱动内部的 `TrainerCore` 一步步训练。

理解这一层的关键，是分清两个「步」：

- **`micro_step`（微步）**：每处理一个 micro-batch（一次前向+反向）就 +1。它数的是前向/反向次数。
- **`global_step`（全局步）**：只在**梯度累积边界**（即 optimizer 真正 step 一次）时才 +1。它数的是 optimizer step 次数。

为什么要区分？因为 ack（确认样本已安全消费）、检查点、resume 这些语义**必须以真正的 optimizer step 为单位**，否则「恢复到第 N 步」这句话就不可靠——你不知道第 N 个 micro-batch 的梯度是否已经被 optimizer 吃掉。所以 `global_step` 被刻意设计成「只在边界自增」，把「步号」和「已提交的 optimizer 状态」严格对齐。

#### 4.2.2 核心流程

`TrainerController._fit` 的循环结构（伪代码）：

```text
_fit(data):
  若 global_step 已达 max_steps → 直接返回（空训）
  module.train()
  for epoch in [当前epoch, num_epochs):
      data.set_epoch(epoch)                 # 让 loader 按 epoch 重排（离线可重迭代）
      跳过本 epoch 已训过的前 _epoch_batch 个 batch（resume 用）
      while True:
          batch = next(loader)              # StopIteration 则结束本 epoch
          _epoch_batch += 1
          _epoch_samples += len(batch.sample_ids)
          micro_step += 1
          pending_ack.extend(batch.sample_ids)        # 攒着，等边界一起 ack
          result = TrainerCore.train_step(batch, ctx) # 一步前向/反向/（边界处）step
          if not result.optimizer_stepped:
              continue                      # ★ 非边界：什么都不做，直接下一个 micro-batch
          # ===== 以下全是「边界动作」，只在 optimizer 真正 step 之后执行 =====
          global_step += 1
          ack_fn(pending_ack, global_step)  # 提交 durable ack，清空 pending
          命中 log_interval → 打日志
          命中 eval_interval → 跑评测
          命中 save_interval 或 is_best → 写检查点
          若 global_step >= max_steps → 返回
      _epoch_batch = 0; _epoch_samples = 0
      epoch = epoch + 1                     # 记录「下一个」epoch，供保存/恢复用
  若结束时还有未攒满的 micro-batch（remainder）→ 抛 RuntimeError
  返回 global_step
```

这条流程里最重要的一行是：

```python
if not result.optimizer_stepped:
    continue
```

它是一道**单点闸门**：所有依赖「optimizer 已 step」的动作都被挡在它后面。`global_step += 1` 紧跟其后，意味着 `global_step` 永远等于「已经发生的 optimizer step 次数」。

#### 4.2.3 源码精读

先看边界闸门与 `global_step` 自增的精确位置（[specforge/training/controller.py:582-592](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/controller.py#L582-L592)）：

```python
                result = self.core.train_step(
                    batch,
                    ctx=StepContext(
                        global_step=self.global_step, total_steps=self.total_steps
                    ),
                )
                self.last_metrics = result.metrics
                # grad accumulated but optimizer has not stepped yet; everything
                # keyed on optimizer steps fires only at the boundary.
                if not result.optimizer_stepped:
                    continue
                self.global_step += 1
```

注意注释的措辞：`everything keyed on optimizer steps fires only at the boundary`（所有以 optimizer step 为单位的事件只在边界触发）。这正是「单一权威边界信号」在循环里的体现——`optimizer_stepped` 是唯一判据。

接着是边界动作的依次执行（[specforge/training/controller.py:588-626](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/controller.py#L588-L626)）：

```python
                self._step_profiler.after_optimizer_step(self.global_step)
                if self.ack_fn is not None:
                    # durable ack transaction at the optimizer-step boundary
                    self.ack_fn(pending_ack, self.global_step)
                    pending_ack = []
                if self.logger and self.global_step % max(1, self.log_interval) == 0:
                    ...                                                  # 打日志（含 lr）
                if eval_enabled and self.global_step % self.eval_interval == 0:
                    eval_metrics = self.evaluate_configured()            # 跑评测
                interval_hit = bool(
                    self.save_interval and self.global_step % self.save_interval == 0
                )
                is_best = bool(
                    eval_metrics and self._checkpoint_manager().is_better(eval_metrics)
                )
                if interval_hit or is_best:
                    self.save_checkpoint(self.global_step)               # interval / best 检查点
                ...
                if self.max_steps is not None and self.global_step >= self.max_steps:
                    return self.global_step                              # 提前停止
```

可以清楚看到：ack → 日志 → 评测 → 检查点，全部以 `global_step` 取模判定，且都排在 `global_step += 1` 之后。

再看 epoch 结束时的处理（[specforge/training/controller.py:627-633](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/controller.py#L627-L633)）：

```python
            self._epoch_batch = 0
            self._epoch_samples = 0
            # Persist the *next* epoch after a naturally exhausted pass.  A
            # checkpoint taken after fit() returns must describe completed
            # work, not epoch ``N`` at batch zero (which would replay that
            # entire epoch on resume).
            self.epoch = epoch + 1
```

这段解决了一个微妙的 resume 陷阱：自然跑完一个 epoch 后，`epoch` 要记成「下一个」，而不是停留在「当前 epoch 的 batch 0」。否则检查点里写的会是「epoch N，batch 0」，resume 时会把整个 epoch N 重训一遍。

`global_step` 与 `micro_step` 的初始化也点明了二者的语义差异（[specforge/training/controller.py:471-475](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/controller.py#L471-L475)）：

```python
        # global_step counts OPTIMIZER steps (increments only at a grad-accum
        # boundary) so ack/checkpoint/resume semantics are in true optimizer
        # steps; micro_step counts forward/backward micro-batches.
        self.global_step = start_step
        self.micro_step = 0
```

#### 4.2.4 代码实践

**实践目标**：用单元测试亲眼看到「`global_step` 只在边界自增」，并验证非边界 micro-batch 不会触发 optimizer step。

**操作步骤**：

1. 打开 [tests/test_runtime/test_trainer.py:107-121](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/tests/test_runtime/test_trainer.py#L107-L121)，阅读 `test_accumulation_boundary`。
2. 关注这段测试逻辑：构造 `accumulation_steps=2` 的 `TrainerCore`，连续喂两个 batch。
3. 本地可选运行：`python -m pytest tests/test_runtime/test_trainer.py::TestTrainerCore::test_accumulation_boundary -q`（待本地验证，需项目环境）。

**需要观察的现象**（测试断言已经写死）：

```python
core = TrainerCore(strat, backend, accumulation_steps=2)
m0 = core.train_step(_batch())
self.assertFalse(m0.optimizer_stepped)   # 第 1 个 micro-batch：未到边界
self.assertEqual(backend.steps, 0)       # optimizer 没动
m1 = core.train_step(_batch())
self.assertTrue(m1.optimizer_stepped)    # 第 2 个 micro-batch：到边界
self.assertEqual(backend.steps, 1)       # optimizer step 了一次
self.assertEqual(backend.backwards, 2)   # 但反向发生了两次
# boundary known BEFORE backward so the backend can no_sync micro-steps
self.assertEqual(backend.boundaries, [False, True])
```

**预期结果**：两个 micro-batch 产生两次反向（`backwards == 2`），但只有一次 optimizer step（`steps == 1`）；`boundaries == [False, True]` 说明边界在反向之前就已判定，从而让后端对非边界 micro-batch 走 `no_sync`（详见 u6-l4）。

> 说明：若本地无 GPU，FakeBackend/FakeStrategy 不依赖真实算子，该测试可在纯 CPU 甚至无 torch 真实计算的情况下跑通，待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：假设 `accumulation_steps=4`，loader 在一个 epoch 里只产出了 6 个 batch 就 `StopIteration`。`global_step` 自增了几次？循环结束后会发生什么？

**参考答案**：6 个 batch 里，边界发生在第 4 个（`4 % 4 == 0`），所以 `global_step` 自增 1 次；之后第 5、6 个 batch 累积了梯度但没攒够，`optimizer_stepped` 为 False，被 `continue` 跳过。循环结束后，`remainder = 6 % 4 = 2`，于是 `_fit` 末尾的 `remainder` 检查会抛 `RuntimeError("training stream ended with incomplete gradient accumulation ...")`（见 4.3）。这正是「自然结束必须在边界」的体现。

**练习 2**：为什么 `pending_ack` 要在 `ack_fn` 调用后立刻 `= []` 清空，而不是等下一个 epoch？

**参考答案**：`pending_ack` 攒的是「当前累积窗口内」消费的样本 id，窗口在边界处结束并提交 durable ack。提交后这些样本已被记账，必须清空，否则下一个窗口会把同一批 id 再 ack 一次。清空动作让 `pending_ack` 始终只代表「自上次 optimizer step 以来新增的、尚未 ack 的样本」。

---

### 4.3 optimizer 边界与 durable ack

#### 4.3.1 概念说明

本模块把「边界」这个概念彻底讲透：边界信号从哪里产生、它驱动了哪些动作、以及为什么训练流自然结束必须落在边界上。

**边界信号的产生者：`TrainerCore.train_step`。** 它是「算子层」，只做一件事——跑一步前向/损失（委托给策略）、反向（委托给后端）、并在边界处执行 optimizer step。它返回一个不可变的 `StepResult`，其中 `optimizer_stepped: bool` 就是那个「单一权威边界信号」。

DESIGN.md 的原话是（[DESIGN.md:57-64](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/DESIGN.md#L57-L64)）：

> `TrainerCore.train_step` divides the loss by `accumulation_steps`, uses `no_sync()` only for non-boundary micro-batches, and returns `optimizer_stepped` as the **single authoritative boundary signal**. `TrainerController` increments `global_step` only at that boundary, commits the pending sample acknowledgements, emits metrics, and performs configured interval saves.

「单一（single）」的含义是：整条链路里，判断「这是不是一次真正的 optimizer step」只发生在 `TrainerCore` 内部一处（`_micro % accumulation_steps == 0`），所有下游（`TrainerController` 的自增、ack、检查点、评测、日志）都只读这一个布尔值，**没有任何别的地方独立判断**「该不该 step」。这样就杜绝了两处判断会因为 bug 而「对不上」——比如一边认为 step 了、另一边认为没 step，导致 ack 与实际 optimizer 状态脱钩。

**durable ack（持久化确认）。** 在线 disaggregated 训练里，样本特征是 producer 实时捕获、consumer 实时消费的（见 [u7-l2](u7-l2-control-plane.md)）。consumer 消费完一个样本后，必须告诉控制面「这个样本我处理完了，可以释放/不再重发」。但如果 consumer 刚 ack 完就崩溃，而那次 optimizer step 还没真正提交，就会出现「样本被标记为已消费、但梯度却丢了」的不一致。所以 SpecForge 把 ack 时机精确地钉在 **optimizer step 边界**：只有当一个累积窗口的梯度被 optimizer 真正吃掉之后，才把这一窗口的样本 id 一起 ack，且带上 `optimizer_durable=True`，表示「这次 ack 绑定的是一个已提交、可恢复的 optimizer 步」。这条约束在 [u7-l2](u7-l2-control-plane.md) 会进一步表述为「durable ack step 必须等于 checkpoint step」。

#### 4.3.2 核心流程

边界信号的产生与消费，可以用一张时序图表示（对应 DESIGN.md 的 mermaid 图，[DESIGN.md:34-55](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/DESIGN.md#L34-L55)）：

```text
TrainerController            TrainerCore                 Backend         Strategy
      │                          │                          │               │
      │ train_step(batch, ctx)   │                          │               │
      │─────────────────────────>│                          │               │
      │                          │ forward_loss(batch, ctx) │               │
      │                          │─────────────────────────────────────────>│
      │                          │<─────────────────────────────────────────│  StepOutput(loss, metrics)
      │                          │ loss /= accumulation_steps              │
      │                          │ _micro += 1                            │
      │                          │ stepped = (_micro % accum == 0)        │
      │                          │ backward(loss, is_boundary=stepped)    │
      │                          │─────────────────────────>│               │
      │                          │ if stepped: step()      │               │
      │                          │─────────────────────────>│               │
      │ StepResult(optimizer_stepped=stepped, ...)          │               │
      │<─────────────────────────│                          │               │
      │ if not stepped: continue │                          │               │
      │ global_step += 1         │                          │               │
      │ ack_fn(pending_ack, global_step)  # durable ack     │               │
      │ save_checkpoint / eval / log（按 global_step 取模） │               │
```

几个要点：

1. **边界在反向之前判定**：`stepped` 在 `backward` 之前就算出来了，传给 `backend.backward(loss, is_boundary=stepped)`，让后端对非边界 micro-batch 用 FSDP/DDP 的 `no_sync()` 推迟梯度归约（u6-l4 详讲）。这是「boundary known BEFORE backward」的工程价值。
2. **`loss / accumulation_steps`**：损失在反向前就除以累积步数，保证「累积 N 个 micro-batch 后的梯度」与「一个大 batch 的梯度」期望一致。
3. **`StepResult` 是不可变值对象**：用 `@dataclass(frozen=True)`，边界信号一经产生就不会被下游篡改。

**自然结束的边界约束。** 训练流可能因为 epoch 跑完或 loader 提前结束而「自然结束」。SpecForge 规定：自然结束**必须**落在一个 optimizer 边界上。如果结束时还剩几个没攒满的 micro-batch（remainder），`_fit` 会直接抛 `RuntimeError`，而不是「凑合做一次部分 optimizer step」或「假装成功存了检查点」。DESIGN.md 的原话（[DESIGN.md:68-72](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/DESIGN.md#L68-L72)）：

> Natural end-of-stream is accepted only at an optimizer boundary. If the final backward is inside FSDP `no_sync`, `fit` fails instead of stepping unreduced gradients or reporting a checkpoint as successful.

#### 4.3.3 源码精读

先看边界信号的**唯一产生点**——`TrainerCore.train_step`（[specforge/training/controller.py:320-331](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/controller.py#L320-L331)）：

```python
    def train_step(
        self, batch: TrainBatch, ctx: Optional[StepContext] = None
    ) -> StepResult:
        out: StepOutput = self.strategy.forward_loss(batch, ctx)   # 策略算损失（算法无关接口）
        loss = out.loss / self.accumulation_steps                  # 损失均摊
        self._micro += 1
        # The boundary is known before backward so the backend can defer the FSDP
        # gradient reduction (no_sync) on non-boundary micro-steps.
        stepped = self._micro % self.accumulation_steps == 0       # ★ 边界唯一判定
        self.backend.backward(loss, is_boundary=stepped)           # 边界已知，再反向
        grad_norm = self.backend.step() if stepped else None       # 仅边界处 step
        return self._result(out, grad_norm, stepped)
```

这一步是「无分支」的：不管什么算法，都走同一条 `forward_loss → /accum → backward → (条件)step`。算法差异（EAGLE3 的多层 TTT、DFlash 的硬标签等）全被关在 `strategy.forward_loss` 里（u6-l2）。

承载这个信号的 `StepResult` 是不可变值对象（[specforge/training/controller.py:51-59](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/controller.py#L51-L59)）：

```python
@dataclass(frozen=True)
class StepResult:
    """Result of one TrainerCore step; ``optimizer_stepped`` is the authoritative
    grad-accumulation boundary signal."""

    optimizer_stepped: bool
    loss: float
    grad_norm: Optional[float]
    metrics: Dict[str, Any] = field(default_factory=dict)
```

再看 durable ack 的接线。`ack_fn` 在 `Trainer.__init__` 里构造，调用控制面的 `ack_train_refs` 并带 `optimizer_durable=True`（[specforge/training/trainer.py:434-447](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/trainer.py#L434-L447)）：

```python
        ack_fn = None
        if durable_ack:

            def ack_fn(ids, step):
                controller.ack_train_refs(
                    trainer_id, ids, global_step=step, optimizer_durable=True
                )
                if defer_queue_ack:
                    ack_ids = getattr(ref_source["queue"], "ack_ids", None)
                    ...
                    ack_ids(ids)
```

注意它只传「样本 id 列表 + 全局步号」这类**元数据**，绝不传张量——这呼应了 [u5-l4](u5-l4-runtime-contracts.md) 的「控制面只传元数据」。控制面侧 `ack_train_refs` 的签名也确认了 `optimizer_durable` 这个关键字参数（[specforge/runtime/control_plane/controller.py:205-212](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/control_plane/controller.py#L205-L212)）：

```python
    def ack_train_refs(
        self,
        trainer_id: str,
        sample_ids: List[str],
        *,
        global_step: Optional[int] = None,
        optimizer_durable: bool = False,
    ) -> None:
```

最后看「自然结束必须在边界」的守卫——`_fit` 末尾的 remainder 检查（[specforge/training/controller.py:634-641](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/controller.py#L634-L641)）：

```python
        remainder = self.core.accumulation_remainder
        if remainder:
            raise RuntimeError(
                "training stream ended with incomplete gradient accumulation: "
                f"received {remainder} of {self.core.accumulation_steps} "
                "micro-batches after the last optimizer step; no partial "
                "optimizer step or durable acknowledgement was committed"
            )
        return self.global_step
```

`accumulation_remainder` 就是「已累加但未触发 step 的 micro-batch 数」（[specforge/training/controller.py:315-318](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/controller.py#L315-L318)）：`return self._micro % self.accumulation_steps`。报错信息明确指出「不会做部分 optimizer step、不会提交部分 durable ack」。

检查点保存（边界动作之一）落在 `TrainerController.save_checkpoint`，它把草稿权重（经 `checkpoint_state_filter` 过滤）、`global_step`、`epoch`、`epoch_samples` 等写盘（[specforge/training/controller.py:711-758](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/controller.py#L711-L758)）。它由 `save_interval` 取模或 `is_best` 触发，与 ack 共享同一个 `global_step`，因此「ack 的步」与「检查点的步」天然对齐——这是后续 [u9-l1](u9-l1-checkpoint-resume.md) resume 能正确恢复、[u7-l2](u7-l2-control-plane.md) 在线恢复契约成立的前提。

#### 4.3.4 代码实践

**实践目标**：用测试亲眼看「不完整累积会被拒绝，且 `global_step` 只反映已提交的 optimizer 步」——这是「自然结束必须在边界」的最直接证据。

**操作步骤**：

1. 打开 [tests/test_runtime/test_trainer.py:458-473](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/tests/test_runtime/test_trainer.py#L458-L473)，阅读 `test_natural_eos_rejects_incomplete_accumulation`。
2. 推演该测试：`accumulation_steps=2`，喂 3 个 batch。前 2 个构成一次完整 step（边界），第 3 个是 remainder。
3. 本地可选运行：`python -m pytest tests/test_runtime/test_trainer.py::TestTrainerControllerFitAndCheckpoint::test_natural_eos_rejects_incomplete_accumulation -q`（待本地验证）。

**需要观察的现象**（测试已写死）：

```python
core = TrainerCore(strat, backend, accumulation_steps=2)
ctrl = TrainerController(core, run_id="r", output_dir=d)
with self.assertRaisesRegex(RuntimeError, "incomplete gradient accumulation"):
    ctrl.fit([_batch() for _ in range(3)])
self.assertEqual(ctrl.global_step, 1)   # 只有第 2 个 batch 那次边界贡献了 1
```

**预期结果**：

- 3 个 batch、`accumulation_steps=2`：边界在第 2 个 batch，所以 `global_step == 1`；第 3 个 batch 是 remainder，循环结束后 `_fit` 抛 `RuntimeError("...incomplete gradient accumulation...")`。
- 这恰好回答了实践任务的两个问题：
  - **`optimizer_stepped` 为何是「单一权威边界信号」**：因为 `global_step` 自增、ack、检查点、评测、日志全部只读它一个布尔值；它又是 `TrainerCore` 内部 `_micro % accumulation_steps == 0` 的唯一产物，没有第二处独立判断。
  - **`global_step` 何时才自增**：仅在 `result.optimizer_stepped` 为 True 之后、紧跟着 `continue` 闸门执行 `self.global_step += 1`（[controller.py:585-587](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/controller.py#L585-L587)），其余 micro-batch 一律被 `continue` 跳过。

> 说明：本实践为源码阅读 + 推演型，可在无 GPU 环境下完成断言理解；如需实跑，FakeBackend/FakeStrategy 不依赖真实算子，待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：如果允许「自然结束时做一次部分 optimizer step」，会对 durable ack 与 resume 造成什么不一致？

**参考答案**：部分 step 意味着用「没攒满 N 个 micro-batch 的梯度」更新了权重，损失却已经按 `/accumulation_steps` 做了均摊（相当于用一个被缩小过的梯度做了完整 step）。更严重的是，这一窗口的样本若已被 durable ack 标记为「已消费且绑定某 optimizer 步」，但该步其实是不完整的部分步，resume 时无法把「已 ack 的样本」与「实际生效的梯度」对齐——要么丢梯度、要么重复训。所以 SpecForge 选择直接 fail，宁可让用户调整 batch/累积配置，也不默默产出不可恢复的状态。

**练习 2**：`optimizer_stepped` 这个信号是「在反向之前」还是「在反向之后」判定的？这个顺序为什么重要？

**参考答案**：在反向之前判定（`stepped = self._micro % self.accumulation_steps == 0` 算在 `backward` 之前，见 [controller.py:328-329](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/controller.py#L328-L329)）。重要性在于：后端据此决定本次反向是否走 `no_sync()`——非边界 micro-batch 推迟梯度归约以省一次全归约通信，边界 micro-batch 才真正触发归约与 optimizer step。如果改成反向之后才判定，就无法在反向里区分两者，`no_sync` 优化也就无从谈起（详见 u6-l4）。

**练习 3**：`TrainerCore.train_step` 被刻意写成「无分支」（算法无关）。请指出算法相关的部分被推到了哪个对象，并说明这种分离带来的好处。

**参考答案**：算法相关逻辑被推到了 `DraftTrainStrategy`（u6-l2）——`forward_loss`、`checkpoint_state_filter`、`trainable_module` 等由各算法插件实现。好处是 `TrainerCore` 与 `TrainerController` 对所有算法（EAGLE3 / DFlash / Domino / DSpark / P-EAGLE）完全共享、无需改动；新增算法只需写策略插件，不必碰训练循环与边界逻辑，降低了「改循环引入边界 bug」的风险。

## 5. 综合实践

把本讲三个模块串起来，做一次「带梯度累积的完整边界追踪」推演。

**任务**：假设配置为 `batch_size=2`、`accumulation_steps=3`、`num_epochs=2`、`save_interval=2`、`eval_interval=0`、`max_steps=None`，每个 epoch 的 loader 稳定产出 6 个 batch。请按下表逐 micro-batch 推演，并回答问题。

| epoch | batch 序号 | `_micro`（取模前） | `optimizer_stepped`? | `global_step`（该步之后） | 是否写检查点 | pending_ack 行为 |
|---|---|---|---|---|---|---|
| 0 | 1 | 1 | ? | ? | ? | ? |
| 0 | 2 | 2 | ? | ? | ? | ? |
| 0 | 3 | 3 | ? | ? | ? | ? |
| 0 | … | … | … | … | … | … |

**要求**：

1. 填完整张表，标出每个 epoch 里 `global_step` 自增的位置、检查点写入的位置（`save_interval=2` 即 `global_step` 是 2 的倍数时写）、以及 `pending_ack` 何时被extend、何时被清空。
2. 指出第 1 个 epoch 结束时 `_epoch_batch`、`epoch` 分别被重置/推进成什么（参考 [controller.py:627-633](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/controller.py#L627-L633)）。
3. 判断：这个配置会不会在 `_fit` 末尾触发 `incomplete gradient accumulation`？为什么？（提示：6 % 3 是否为 0。）
4. 用一句话总结：为什么把 ack、检查点、日志、评测全部绑在 `optimizer_stepped` 这一个信号上，能让 resume 与在线恢复变得可靠。

**预期结果**（自检）：

- 每个 epoch 6 个 batch、累积 3 → 每 epoch 恰好 2 次边界 → `global_step` 每 epoch +2；`6 % 3 == 0`，所以**不会**触发 incomplete 报错。
- `global_step == 2` 时命中 `save_interval=2`，写一次检查点；`global_step == 4` 时再写一次。
- epoch 0 结束：`_epoch_batch = 0`、`_epoch_samples = 0`、`epoch` 推进为 1。
- 综合结论：所有「以 optimizer step 为单位」的副作用共享同一个边界信号与同一个 `global_step`，使得「检查点里的步号」「ack 的步号」「实际生效的 optimizer 步」三者永远一致，从而 resume 时能精确恢复到某个已提交的 optimizer 状态、在线 consumer 恢复时不会重放或丢失已 ack 的样本。

> 说明：本综合实践为推演型，无需 GPU；若要在本地用假对象实跑类似场景，可仿照 `test_accumulation_boundary` 与 `test_natural_eos_rejects_incomplete_accumulation` 自行构造 `FakeStrategy`/`FakeBackend` 与 batch 列表，待本地验证。

## 6. 本讲小结

- `Trainer.fit()` 是全包**唯一**的训练入口，签名只有 `self`：所有副作用（评测、检查点、日志、ack）在构造期注入，`fit()` 只负责按正确顺序跑完并用 `try/except/finally` 保证清理（[trainer.py:511-587](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/trainer.py#L511-L587)）。
- 三层分工：`Trainer` 管拓扑清理与兜底保存，`TrainerController` 管 epoch 循环与边界动作，`TrainerCore` 管无分支的一步与累积边界（[DESIGN.md:22-31](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/DESIGN.md#L22-L31)）。
- `global_step`（optimizer 步）与 `micro_step`（前向/反向次数）是两个不同的计数器；`global_step` 只在梯度累积边界自增，紧跟着 `if not result.optimizer_stepped: continue` 这道闸门（[controller.py:585-587](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/controller.py#L585-L587)）。
- `optimizer_stepped` 是「单一权威边界信号」：它由 `TrainerCore` 内部 `_micro % accumulation_steps == 0` 唯一产生，且在反向之前判定，驱动 ack、检查点、评测、日志全部边界动作（[controller.py:320-331](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/controller.py#L320-L331)）。
- durable ack 把样本 id 的确认钉在 optimizer 边界、带 `optimizer_durable=True`，只传元数据不传张量，使「已 ack 样本」与「已提交梯度」对齐（[trainer.py:434-447](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/trainer.py#L434-L447)）。
- 训练流自然结束必须落在 optimizer 边界：remainder 非零时 `_fit` 直接抛 `RuntimeError`，绝不做部分 step 或谎报检查点成功（[controller.py:634-641](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/controller.py#L634-L641)）。

## 7. 下一步学习建议

- **[u6-l4 FSDP 后端与梯度累积](u6-l4-fsdp-backend.md)**：本讲反复提到「边界已知，再反向」「非边界走 `no_sync()`」，下一讲正好拆解 `FSDPTrainingBackend.backward` 如何用 `is_boundary` 决定是否推迟梯度归约，以及 `step` 如何裁剪并返回全局梯度范数。
- **[u9-l1 检查点与恢复](u9-l1-checkpoint-resume.md)**：本讲的 `save_checkpoint` 写了 `global_step`、`epoch`、`epoch_samples` 等字段，下一讲讲 `resume_from` 如何在读回时校验这些字段、并用 `FeatureDataLoader.seek()` 重新定位到中断处。
- **[u7-l2 控制平面与元数据账本](u7-l2-control-plane.md)**：本讲的 durable ack 调用了 `DataFlowController.ack_train_refs`；如果想看清「durable ack step 必须等于 checkpoint step」这条在线恢复契约的全貌，继续阅读控制平面与 `DPAckController`。
- **源码延伸阅读**：`specforge/training/profiling.py`（`StepProfiler` 的 `before_micro_step` / `after_optimizer_step` 如何把 profile 窗口对齐到 optimizer step）、`specforge/training/schedule.py`（`resolve_total_steps` / `validate_fixed_accumulation_plan`，后者正是本讲「remainder」约束在装配期的前置校验）。

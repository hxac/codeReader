# 实验跟踪与性能分析

## 1. 本讲目标

本讲聚焦训练循环里两件「看得见」的事：把每一步的指标（loss、accuracy、lr……）发到外部实验跟踪后端，以及在一段训练窗口里抓一份 PyTorch 性能 trace。

学完后你应该能够：

- 知道 `tracking.report_to` 这一个字段如何切换 wandb / tensorboard / swanlab / mlflow / none 五种后端，以及为什么这些后端只在 rank 0、且只在「真正训练」的进程上工作。
- 说清楚 `profiling` 那个 `start_step` / `num_steps` 窗口为什么用「已完成的 optimizer step」而不是 micro-step 来计量，从而天然对齐梯度累积与 resume。
- 记住 `train/*` 与 `eval/*` 这两个指标命名空间是在哪一层、以什么规则加上的。

本讲是 [u6-l3 Trainer 与 TrainerController](u6-l3-trainer-loop.md) 的直接后续：在那里我们建立了「所有边界动作都挂在 `optimizer_stepped` 这个单一权威信号上」的认识，本讲就把「日志」与「profiling」这两个边界动作拆开讲透。

## 2. 前置知识

- **优化器边界（optimizer step）**：梯度累积把 N 个 micro-batch 的梯度攒起来才做一次真正的参数更新；这一刻叫一个 optimizer step，SpecForge 里 `global_step` 只在这一刻自增（见 [u6-l3](u6-l3-trainer-loop.md)）。本讲的 profiling 窗口就以它为计量单位。
- **rank 0**：分布式训练里 `dist.get_rank()==0` 的进程。SpecForge 规定所有对外可见的实验跟踪写入只在 rank 0 进行，其余 rank 只参与训练、不产生重复日志。
- **懒导入（lazy import）**：把可能没装、或导入很重的第三方库放进 `try/except ImportError`，没装就设成 `None`，等真正用到再报错。SpecForge 的四个跟踪后端就是这么处理的，所以哪怕你没装 wandb，`specforge train` 也能正常跑。
- **Callable 适配器（logger seam）**：训练器不想认识 wandb、tensorboard 这些具体后端，只想要一个 `logger(metrics, step)` 的可调用对象。中间那个把「各种后端」翻译成「统一可调用对象」的薄层，就叫 seam（接缝）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `specforge/tracker.py` | 跟踪后端的「实现侧」：抽象基类 `Tracker` + 5 个实现 + 工厂函数。懒导入 wandb/tensorboard/swanlab/mlflow。 |
| `specforge/training/tracking.py` | 训练器与跟踪后端之间的「接缝」：指标归一化、命名空间加前缀、可调用适配器 `TrackerLogger`。 |
| `specforge/training/profiling.py` | 有界 per-rank PyTorch trace 的窗口选项 `ProfilingOptions` 与生命周期管理 `StepProfiler`。 |
| `specforge/config/schema.py` | `TrackingConfig` 与 `ProfilingConfig` 两段类型化配置。 |
| `specforge/training/controller.py` | 在训练主循环里调用 profiler 的 start/stop、并在退出时收尾。 |
| `specforge/training/assembly.py` | 装配期决定「用哪个 logger」「传哪个 profiling 窗口」。 |
| `docs/basic_usage/training.md` | 用户文档里 tracking / profiling 两节的 YAML 示例。 |

一句话总线：`config(schema) → assembly(_configured_logger/_profiling_options) → controller(主循环里 log + profiler) → tracking(tracker.py 实现 + tracking.py 接缝)`。

## 4. 核心概念与源码讲解

### 4.1 tracker 后端：一个字段切换五种实验跟踪

#### 4.1.1 概念说明

训练时你会想知道「第 100 步的 loss 是多少、accuracy 是多少、学习率降到多少」。这些指标可以打到外部平台（wandb / tensorboard / swanlab / mlflow）画曲线，也可以只打到控制台。SpecForge 用一个配置字段 `tracking.report_to` 来切换，五种取值：`none`、`wandb`、`tensorboard`、`swanlab`、`mlflow`。

设计上有三个关键约束：

1. **懒导入**：四个后端库都是可选依赖，没装也不影响训练，用到时才报「请 pip install」。
2. **只在 rank 0 写**：多卡训练时只有 rank 0 真正调后端 API，避免重复日志。
3. **producer 不跟踪**：在线 disaggregated 训练里的 producer 进程只负责捕获特征、不算梯度，因此它根本不创建外部 tracker，只保留控制台输出。这与 [u3-l3](u3-l3-topology-builders.md)「producer 不汇聚 Trainer」一脉相承。

#### 4.1.2 核心流程

```
TrackingConfig.report_to  (yaml)
        │
        ▼
assembly._configured_logger(cfg)
        │  report_to == "none" 或 role == "producer"  ──► 裸控制台 _logger
        │  否则
        ▼
tracking.create_tracker_logger(args, output_dir)
        │  get_tracker_class(report_to)  ──► tracker.py 的 TRACKER_REGISTRY
        │  tracker_class.validate_args(...)
        ▼
TrackerLogger(tracker 实例, console_logger=_logger)
        │  （一个可调用对象，训练器只认它）
        ▼
controller 主循环:  self.logger(metrics, global_step)
```

外部后端的 API 形态各不相同（wandb 用 `wandb.log`，tensorboard 用 `SummaryWriter.add_scalar`，mlflow 用 `log_metrics`），抽象基类 `Tracker` 把它们统一成三个方法：`validate_args`（启动前校验）、`log`（写指标）、`close`（收尾）。

#### 4.1.3 源码精读

**懒导入**：四个库都包在 `try/except ImportError` 里，装了才有值，否则为 `None`（wandb 还多一道防误命中同名本地目录的检查）：

[specforge/tracker.py:12-37](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/tracker.py#L12-L37) — 把 `wandb / SummaryWriter / swanlab / mlflow` 设成「装了才可用」。

**抽象基类**：定义三个抽象方法，并在 `__init__` 里计算 `self.rank`：

[specforge/tracker.py:63-69](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/tracker.py#L63-L69) — `rank` 在分布式已初始化时取真实 rank，否则默认 0；`is_initialized` 默认 `False`。

`Tracker.log` / `Tracker.close` 是抽象方法（[tracker.py:80-90](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/tracker.py#L80-L90)），各实现去填。

**rank0-only 写入**：以 TensorBoard 为例，构造与日志都包在 `if self.rank == 0` 里：

[specforge/tracker.py:258-274](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/tracker.py#L258-L274) — `TensorboardTracker` 把日志写到 `output_dir/runs`，且 `log` 里只接受 `int/float` 标量。

**凭据脱敏**：把 args 转成 dict 时，名字里含 `key/token/password` 的字段会被替换成 `<redacted>`，避免 API key 进 run 日志：

[specforge/tracker.py:43-51](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/tracker.py#L43-L51) — `_public_config` 是所有 tracker 共享的脱敏器。

**工厂与注册表**：名字到类的映射就一张字典：

[specforge/tracker.py:325-344](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/tracker.py#L325-L344) — `TRACKER_REGISTRY` 五个键，`create_tracker` 按名查表、找不到抛 `ValueError`。

**配置段**：`TrackingConfig` 用 `Literal` 把 `report_to` 钉死成五种取值（填别的会在配置校验阶段就报错）：

[specforge/config/schema.py:164-178](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L164-L178) — 各后端对应的 project/name/key 字段都可选，缺省由装配层补。

**装配层只给「真训练」进程建 tracker**：

[specforge/training/assembly.py:293-311](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py#L293-L311) — `report_to=="none"` 或 `role=="producer"` 时直接返回裸 `_logger`（控制台）；否则把 `cfg.tracking` 拍平成 `SimpleNamespace`，补上默认 project=`specforge`、name=`run_id`，交给 `create_tracker_logger`。

> 注意一个细节：这里把控制台 `_logger` 作为 `console_logger` 一起塞进 `TrackerLogger`，意味着开启外部后端后，**控制台输出不会消失**，而是与外部后端同时写（且共享同一份归一化后的指标）。

#### 4.1.4 代码实践

**实践目标**：体验 `report_to` 一字段切换后端，并观察「producer 不跟踪」与「懒导入报错」两个行为。

**操作步骤**：

1. 复制一个离线示例配置，例如 `examples/configs/qwen3-8b-eagle3-offline.yaml`，在文件里加上：
   ```yaml
   tracking:
     report_to: tensorboard
   ```
2. 用 `--plan` 先预览（不会起进程，也不需要 tensorboard 装好）：
   ```bash
   specforge train -c my-offline.yaml --plan
   ```
3. 把 `report_to` 临时改成 `mlflow`，且**不装 mlflow**，再跑一次真实训练（或 `--plan` 之后的校验）。观察报错信息。

**需要观察的现象**：

- 步骤 2 输出正常的 launch plan，因为 `--plan` 走的是配置解析与拓扑构建，不触发 tracker 实例化。
- 步骤 3 会抛出类似 `To use --report-to mlflow, you must install mlflow` 的错误（来自 [tracker.py:281-285](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/tracker.py#L281-L285)），证明懒导入在「真正用到」时才 fail-fast。

**预期结果**：`report_to: tensorboard` 的真实训练跑完后，`output_dir/runs` 下会出现 TensorBoard 事件文件，`tensorboard --logdir <output_dir>/runs` 可看到曲线。

**待本地验证**：若本地没有目标模型权重与数据，可用最小数据集短训几步，重点只看 `runs` 目录是否生成。

#### 4.1.5 小练习与答案

**练习 1**：为什么四个后端库要用 `try/except ImportError` 懒导入，而不是直接 `import wandb`？

**参考答案**：这些库是可选依赖（不在核心依赖里）。直接 import 会在「用户根本没装 wandb、只想用 tensorboard」时让整个 `specforge` 包无法导入。懒导入保证「不用的后端不拖累你」，且把缺依赖的错误推迟到「真正选用该后端」时，由 `validate_args` 给出清晰的「请 pip install」提示。

**练习 2**：在线 disaggregated 训练里，producer 进程会把指标打到 wandb 吗？

**参考答案**：不会。`assembly._configured_logger` 在 `role == "producer"` 时直接返回裸控制台 `_logger`（[assembly.py:295](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py#L295)），producer 根本不创建 `TrackerLogger`。这符合 producer 不参与梯度计算、不需要训练指标的设计。

---

### 4.2 profiling 窗口：有界 per-rank PyTorch trace

#### 4.2.1 概念说明

训练慢了，你需要一份「这一段训练里 CPU/GPU 都在干什么」的性能 trace，丢进 Chrome Trace Viewer（`chrome://tracing`）或 Perfetto 看火焰图。SpecForge 不提供单独的 profiler 脚本，而是把它内建进训练循环：用 `profiling` 这一段配置声明一个**有界的窗口**，训练跑到那个窗口时自动抓 trace、抓完自动停。

三个核心设计点：

1. **窗口以「已完成的 optimizer step」计量**：不是 micro-step、不是 wall-clock 时间。这是它能在梯度累积与 resume 下都对齐的根因。
2. **每 rank 一个 trace 文件**：每个进程各自抓自己的，文件名带 `rank{rank}`，方便看单卡瓶颈。
3. **任何退出路径都收尾**：哪怕训练中途失败或 Ctrl-C，已开的窗口也会被 finalize 导出，不会留半个 corrupt 的 trace。

#### 4.2.2 核心流程

```
ProfilingConfig (yaml)                          enabled / start_step / num_steps / record_shapes
        │
        ▼
assembly._profiling_options(cfg)  ──►  ProfilingOptions（不可变 dataclass）
        │
        ▼
TrainerController.__init__  ──►  StepProfiler(options, output_dir)
        │
   进入 fit() 主循环，每个 micro-step：
        │
        ├─ core.train_step  之前： before_micro_step(global_step)   ── 命中窗口起点就 start()
        └─ optimizer 边界之后： after_optimizer_step(global_step)   ── 凑满 num_steps 就 stop+export
        │
   finally（成功/失败/中断）： close_profiler()  ── 导出未关闭的半个窗口
```

为什么用 optimizer step 而不是 micro-step？因为「一次真正的参数更新」才是训练里语义稳定、可比较的单位。梯度累积时，一个 optimizer step = `accumulation_steps` 个 micro-batch；如果窗口按 micro-step 计，你换一下 `accumulation_steps`，窗口覆盖的实际计算量就变了，trace 之间没法比。按 optimizer step 计则恒定：`num_steps=4` 永远等于「4 次完整参数更新」的计算量。

#### 4.2.3 源码精读

**窗口选项是不可变值对象**，默认 `start_step=30`、`num_steps=4`：

[specforge/training/profiling.py:22-35](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/profiling.py#L22-L35) — `__post_init__` 校验 `start_step >= 0`、`num_steps >= 1`，越界直接抛 `ValueError`。

**配置段**对应：`ProfilingConfig` 的 `start_step` 用 `Field(ge=0)`、`num_steps` 用 `Field(gt=0)` 在 schema 层再做一遍约束（[schema.py:181-187](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L181-L187)），所以校验是双重的。

**StepProfiler 的状态机**只靠两个布尔/对象位：`self._profiler`（当前是否开着）和 `self._done`（是否已导出过，保证一生只抓一次）。`before_micro_step` 的启动判据是关键：

[specforge/training/profiling.py:55-64](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/profiling.py#L55-L64) — 只有当 `start_step <= completed_steps < start_step + num_steps` 且尚未开过、尚未完成时才启动。

注意它被**每个 micro-step** 都调用一次（见下方 controller），但因为判据用的是 `completed_steps`（即 `global_step`，仅在 optimizer 边界才变），所以它会在「窗口内第一个 optimizer step 的第一个 micro-step」那一刻启动，并精确覆盖 `num_steps` 次 optimizer 更新的全部 micro-step。

**启动时选 activities**：有 CUDA 就加 CUDA activity，并开 `with_stack`：

[specforge/training/profiling.py:66-78](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/profiling.py#L66-L78) — 这里 `import torch` 是惰性的，不开启 profiling 时完全不引入 torch.profiler 开销。

**收尾在 optimizer 边界触发**：

[specforge/training/profiling.py:80-87](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/profiling.py#L80-L87) — `after_optimizer_step` 只在 `completed_steps >= end_step` 时导出；而它只在 optimizer 边界被调用（见 controller），所以导出一定对齐到一次完整更新之后。

**导出每 rank 一份**，文件名带 rank 与纳秒时间戳防撞：

[specforge/training/profiling.py:94-120](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/profiling.py#L94-L120) — `_stop_and_export` 取 `dist.get_rank()`（或 `RANK` 环境变量兜底），写成 `profile_rank{rank}_{time_ns}.trace.json.gz`，并置 `_done=True` 防止二次抓取。

**controller 里的三处接线**：构造时建 profiler；每个 micro-step 前问「该不该开」；optimizer 边界后问「该不该关」：

[specforge/training/controller.py:489-495](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/controller.py#L489-L495) — 构造 `StepProfiler`。

[specforge/training/controller.py:575-588](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/controller.py#L575-L588) — `before_micro_step` 在 `train_step` 之前调；`after_optimizer_step` **只在 `result.optimizer_stepped` 为真**（即 optimizer 边界）之后调。这一句就是「窗口对齐 optimizer step」的最终落点。

> 把这段和 [u6-l3](u6-l3-trainer-loop.md) 联起来看：`optimizer_stepped` 是「单一权威边界信号」，profiler 的 start/stop 也只是它的两个下游消费者之一。

**退出路径的兜底**：`close_profiler` 会在 Trainer 的 `finally` 里被无条件调用，把任何还开着的半个窗口导出：

[specforge/training/trainer.py:553-556](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/trainer.py#L553-L556) — 即使中途异常，profiler 也会被收尾；它自己的异常被吞成日志，不掩盖训练主异常。

[specforge/training/controller.py:644-649](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/controller.py#L644-L649) — `close_profiler` 调 `StepProfiler.close`，并 catch 所有异常只记日志。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：开启一段 `start_step=30, num_steps=4` 的 profiling，并解释它在梯度累积与 resume 下如何对齐 optimizer step。

**操作步骤**：

1. 在一份离线 EAGLE3 配置里加上：
   ```yaml
   profiling:
     enabled: true
     start_step: 30
     num_steps: 4
     record_shapes: false
   ```
2. 同时设置一个能让训练跑到至少 34 步的 `training.max_steps`（例如 `max_steps: 40`），否则永远到不了窗口。
3. 如果想同时验证梯度累积的影响，把 `training.accumulation_steps` 设为 2，观察 trace 的长度（覆盖的 micro-step 数）。
4. 运行：
   ```bash
   specforge train -c my-offline-profile.yaml
   ```

**需要观察的现象**：

- 训练日志在第 30 步附近出现 `training profiler started at optimizer step 30`，第 34 步附近出现 `training profiler stopped at optimizer step 34: .../profile_rank0_<ns>.trace.json.gz`（来自 [profiling.py:78](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/profiling.py#L78) 与 [profiling.py:116-120](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/profiling.py#L116-L120)）。
- 多卡时，每个 rank 都会生成各自的 `profile_rank{rank}_*.trace.json.gz`。
- 用 `chrome://tracing` 或 [ui.perfetto.dev](https://ui.perfetto.dev) 打开可看火焰图。

**对齐到 optimizer step 的解释（重点）**：

- **梯度累积**：profiler 的窗口判据用的是 `global_step`（已完成 optimizer step 数），与 `accumulation_steps` 无关。设 `accumulation_steps=2`：每个 optimizer step 含 2 个 micro-batch 的前向反向，`num_steps=4` 的窗口就覆盖 `4 × 2 = 8` 个 micro-batch 的计算。换更大的 `accumulation_steps`，窗口覆盖的 optimizer 更新数仍是 4，只是 trace 时间变长——这正是「按 optimizer step 计量」的好处：跨配置可比。判据本身（`before_micro_step` 里 `completed_steps < start_step` 等）每个 micro-step 都判定一次，但 `completed_steps` 只在 optimizer 边界变，所以启动点精确落在「第 30 个 optimizer step 的第一个 micro-step」。
- **resume**：从检查点续训时，`global_step` 从检查点恢复（见 [u9-l1](u9-l1-checkpoint-resume.md)）。profiler 没有任何独立的步数状态，它完全读 controller 的 `self.global_step`，所以 resume 后窗口仍然从「全局第 30 步」开始抓，不会因为换了进程就从 0 重新数、也不会重复抓取（`_done` 标志跨进程生命周期由「新进程 = 新 StepProfiler」自然重置，但只要 global_step 已超过窗口就不会再触发）。换句话说，profiler 是**无状态的窗口观察者**，所有步数权威都在 controller。

**预期结果**：`output_dir` 下生成每 rank 一个 `.trace.json.gz`，覆盖第 30–33 这 4 个 optimizer step（含其全部 micro-batch）。

**待本地验证**：若没有足够算力跑满 34 步，可把 `start_step` 调到更小（如 2）、`max_steps` 调到 8 来观察同样的 start/stop 日志。

#### 4.2.5 小练习与答案

**练习 1**：如果 `max_steps=20` 而 `profiling.start_step=30`，会发生什么？

**参考答案**：训练在 step 20 就结束，永远到不了 step 30，`before_micro_step` 的 `completed_steps < start_step` 一直成立，profiler 从未启动，不生成任何 trace 文件。需要把 `max_steps` 设到 ≥ `start_step + num_steps`（本例 ≥ 34）才能抓到完整窗口。

**练习 2**：为什么 `after_optimizer_step` 只在 optimizer 边界被调用，而不是每个 micro-step？

**参考答案**：在 controller 里它紧跟在 `if not result.optimizer_stepped: continue` 之后（[controller.py:585-588](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/controller.py#L585-L588)）。这保证「凑满 num_steps 就停」只在真正的参数更新边界判定，使导出点对齐 optimizer step；若每个 micro-step 都判，反而会与梯度累积的语义错位。

**练习 3**：训练中途 Ctrl-C，已经开了但没导出的窗口会丢失吗？

**参考答案**：不会。`Trainer.fit` 的 `finally` 无条件调 `close_profiler`（[trainer.py:553-556](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/trainer.py#L553-L556)），它会调 `StepProfiler.close` 把开着的那半个窗口 stop 并导出（[profiling.py:89-92](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/profiling.py#L89-L92)），所以你能拿到中断前那段的 trace。

---

### 4.3 metrics 命名：train/* 与 eval/* 的统一约定

#### 4.3.1 概念说明

训练器产出的指标有两类来源：策略（`DraftTrainStrategy.forward_loss`）返回的训练指标，以及评测器（`Evaluator`）返回的评测指标。如果任由它们各起名字，外部后端里就会混在一起没法区分。

SpecForge 用两个命名空间来分隔：训练指标统一加 `train/` 前缀，评测指标统一用 `eval/` 前缀。这个约定在一个非常薄的接缝层（`training/tracking.py`）里实现，训练器自己完全不关心。

这里有个容易被忽略的细节：**`train/` 前缀只在「启用了外部后端」时才加上**；纯控制台（`report_to: none`）输出的是策略原本的裸名字（如 `accuracy`）。而 `eval/*` 因为是评测器直接产出的，在控制台和外部后端里都带前缀。

#### 4.3.2 核心流程

```
策略 forward_loss ──► StepOutput.metrics = {"accuracy":..., "plosses":[...], ...}   （裸名）
        │
controller 主循环: log_metrics = dict(result.metrics); log_metrics["lr"] = ...
        │
        ▼
self.logger(log_metrics, global_step)
        │
   ┌────┴────────────────────────────┐
   │ report_to == none               │ report_to != none
   ▼                                 ▼
裸 _logger(metrics, step)        TrackerLogger.__call__(metrics, step)
   打印裸名                          │
                                   ▼
                            scalar_metrics(...)        ── tensor 展开成 name/index 标量
                                   ▼
                            training_metric_names(...) ── 加 train/ 前缀（eval/* 透传）
                                   ▼
                            console_logger + tracker.log   ── 控制台与外部后端同写

评测侧: Evaluator.run ──► {"eval/avg_loss":..., "eval/avg_acc":..., "eval/simulated_acc_len":...}
        │ （已自带 eval/ 前缀，training_metric_names 透传）
        ▼
   self.logger(eval_metrics, global_step)
```

#### 4.3.3 源码精读

**策略产出的是裸名**（且常常是 tensor 列表），以 EAGLE3 家族为例：

[specforge/training/strategies/base.py:287-297](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/strategies/base.py#L287-L297) — `metrics` 含 `plosses`、`acces`、`acceptance_rates` 等，都是 detach 后的 tensor 列表。

**scalar_metrics 把 tensor 展开成标量**：单元素 tensor 取 `float`，多元素张量/序列展开成 `name/index` 稳定键，绝不让非标量悄悄进 MLflow（它只收标量）：

[specforge/training/tracking.py:18-53](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/tracking.py#L18-L53) — 关键是 `add` 函数：`numbers.Real` 直接转 float；带 `.detach` 的张量按 numel 分流；list/tuple 递归成 `name/index`。

这样 `acceptance_rates`（一个长度为 TTT 步数的列表）会变成 `acceptance_rates/0`、`acceptance_rates/1`…… 后端里能画出「每个草拟步的接受率」一族曲线。

**training_metric_names 加 train/ 前缀**，但已带 `train/` 或 `eval/` 的键透传：

[specforge/training/tracking.py:56-66](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/tracking.py#L56-L66) — 一行字典推导式实现：`key if key.startswith(("train/", "eval/")) else f"train/{key}"`。

> docstring 点明了为什么这么做：评测侧**已经**报告 `eval/*` 键；策略结果是裸名，因为控制台 logger 刻意保持后端中立，「加 train/ 命名空间」只在**外部后端边界**做一次。

**TrackerLogger 是那个统一可调用对象**，把上面两步串起来，并同时喂给控制台与外部后端：

[specforge/training/tracking.py:87-93](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/tracking.py#L87-L93) — `values = training_metric_names(scalar_metrics(metrics))`；先给 console_logger，再给 `tracker.log`。两侧看到的是同一份已归一化、已加前缀的指标。

它的 `close` 是幂等的（[tracking.py:95-98](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/tracking.py#L95-L98)），且关闭后再 log 会抛错（[tracking.py:88-89](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/tracking.py#L88-L89)），保证生命周期清晰。

**评测侧直接产出 eval/****：`Evaluator.run` 聚合后返回带前缀的字典：

[specforge/eval/evaluator.py:140-159](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/eval/evaluator.py#L140-L159) — 产出 `eval/avg_loss`、`eval/avg_acc`、`eval/simulated_acc_len` 等；其中 `eval/simulated_acc_len` 正是默认的「best 检查点选择指标」（见 [u9-l4](u9-l4-export-eval-benchmark.md) 与 [checkpoint.py:42](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/checkpoint.py#L42)）。

**controller 里训练/评测都走同一个 logger**：

[specforge/training/controller.py:593-607](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/controller.py#L593-L607) — 训练指标在 `log_interval` 边界写、并补一个 `lr`（会变成 `train/lr`）；评测指标在每个 `eval_interval` 边界写。两者都调 `self.logger`，由接缝层决定加不加前缀。

**文档里的约定**也印证了这一点：

[docs/basic_usage/training.md:362-366](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/basic_usage/training.md#L362-L366) — 「Trainer and evaluator metrics use `train/*` and `eval/*` names consistently」。

#### 4.3.4 代码实践

**实践目标**：直观看到「裸名 → 加前缀」的转换，不依赖任何 GPU 或后端账号。

**操作步骤**：在本地 Python 里直接调用接缝层的两个纯函数，模拟一组策略指标：

```python
# 示例代码：非项目原有，仅为演示 tracking.py 的命名转换
import torch
from specforge.training.tracking import scalar_metrics, training_metric_names

raw = {
    "accuracy": torch.tensor(0.42),
    "acceptance_rates": [torch.tensor(0.5), torch.tensor(0.6), torch.tensor(0.7)],
    "lr": 5e-5,
}
scalars = scalar_metrics(raw)
print("scalar_metrics:", scalars)
print("training_metric_names:", training_metric_names(scalars))
```

**需要观察的现象**：

- `scalar_metrics` 把 `acceptance_rates` 展开成 `acceptance_rates/0`、`/1`、`/2` 三个 float，`accuracy` 变成 `0.42`。
- `training_metric_names` 给它们都加上 `train/`：`train/accuracy`、`train/acceptance_rates/0`、`train/lr`。

**预期结果**：你得到一个全是 `train/` 前缀标量的扁平字典，这正是 `TrackerLogger.__call__` 喂给 wandb/tensorboard 的最终形态。

**待本地验证**：若 `import torch` 失败，可把 tensor 换成纯 Python `float` 与 `list`，`scalar_metrics` 同样会处理（list 走 [tracking.py:47-49](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/tracking.py#L47-L49) 的递归分支）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `training_metric_names` 对已带 `eval/` 的键不加 `train/`？

**参考答案**：评测指标由 `Evaluator` 直接产出，本身就带 `eval/` 前缀（见 [evaluator.py:140-159](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/eval/evaluator.py#L140-L159)）。接缝层的判定 `key.startswith(("train/", "eval/"))` 让这些键原样透传，避免变成错误的 `train/eval/avg_loss`。

**练习 2**：`report_to: none` 时，控制台会看到 `train/accuracy` 这样的键吗？

**参考答案**：不会。`report_to=="none"` 时装配层返回的是裸 `_logger`（[assembly.py:295-296](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py#L295-L296)），它不经过 `TrackerLogger`，所以训练侧显示的是裸名 `accuracy`；但评测侧因为 `Evaluator` 直接产出 `eval/*`，控制台仍会看到 `eval/avg_loss` 这类键。只有启用外部后端时，训练侧才统一加上 `train/`。

**练习 3**：MLflow 后端如果收到一个 shape 为 `[7]` 的张量指标会怎样？

**参考答案**：不会出错也不会被丢弃。`scalar_metrics` 会把它展开成 `name/0`…`name/6` 七个标量键（[tracking.py:43-46](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/tracking.py#L43-L46)），再交给只接受标量的 `mlflow.log_metrics`。这正是该函数存在的理由——避免非标量悄悄进 MLflow 报错。

## 5. 综合实践

把本讲三个模块串起来：为同一次离线 EAGLE3 训练**同时**配置 tensorboard 跟踪与一段 profiling 窗口，并验证二者的「边界对齐」语义来自同一个 `optimizer_stepped` 信号。

1. 复制 `examples/configs/qwen3-8b-eagle3-offline.yaml` 为 `my-tracked.yaml`，加入：
   ```yaml
   tracking:
     report_to: tensorboard

   profiling:
     enabled: true
     start_step: 30
     num_steps: 4

   training:
     max_steps: 40          # 必须足以跨过窗口终点 34
     accumulation_steps: 2  # 故意开启梯度累积，验证窗口仍按 optimizer step 对齐
     log_interval: 5
   ```
2. 跑 `specforge train -c my-tracked.yaml`（有条件的话多卡，观察每 rank 各一个 trace）。
3. 验证三件事：
   - **tracking**：`tensorboard --logdir <output_dir>/runs` 能看到 `train/accuracy`、`train/lr`、`train/acceptance_rates/0` 一族曲线（注意它们都带 `train/` 前缀，且接受率被展开成多条）。
   - **profiling**：`output_dir` 下出现 `profile_rank0_*.trace.json.gz`，日志里 start 在 step 30、stop 在 step 34。
   - **对齐**：因为 `accumulation_steps=2`，`global_step` 每 2 个 micro-batch 才 +1；trace 覆盖的是 4 个 optimizer step = 8 个 micro-batch 的计算；而 tensorboard 上的 `train/*` 点也只在这些 optimizer 边界（且命中 `log_interval`）出现——两者都挂在同一个 `optimizer_stepped` 边界上。
4. 进阶（可选）：把 `start_step` 调到一个已 checkpoint 过的步数（如 20），先跑到 25 存盘，再 `training.resume_from` 续训，观察 profiler 是否仍从全局 step 30 开始抓——以此验证「profiler 无独立步数状态，全靠 controller 的 global_step」。

> 如果本地没有完整模型/数据，第 3、4 步可降级为「源码阅读型实践」：在 [controller.py:575-607](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/controller.py#L575-L607) 逐行确认 `before_micro_step`、`after_optimizer_step`、`self.logger` 三者都在 `result.optimizer_stepped` 这个边界条件之下/之后触发，从而在纸面上完成「同源对齐」的论证。

## 6. 本讲小结

- 一个字段 `tracking.report_to` 切换 wandb / tensorboard / swanlab / mlflow / none 五种后端，实现在 [tracker.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/tracker.py)，靠懒导入 + 工厂注册表 + rank0-only 写入 + 凭据脱敏四个机制保证「可选、安全、不重复」。
- tracker 只为「真正训练」的进程创建：producer 与 `report_to: none` 都只用裸控制台 logger（[assembly.py:293-311](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py#L293-L311)）。
- `training/tracking.py` 是训练器与后端之间的薄接缝：`TrackerLogger` 是统一可调用对象，`scalar_metrics` 把张量展开成标量、`training_metric_names` 给训练指标加 `train/` 前缀。
- profiling 窗口（[profiling.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/profiling.py)）以「已完成 optimizer step」计量，因此天然对齐梯度累积与 resume；每 rank 一份 trace，任意退出路径都收尾。
- 三个边界动作——logging、profiling 的 start/stop、还有 u6-l3 的 ack/checkpoint——都挂在同一个 `optimizer_stepped` 单一权威信号上，这是 SpecForge 训练循环贯穿始终的设计。
- 指标命名约定：训练侧 `train/*`（在外部后端边界才加）、评测侧 `eval/*`（评测器直接产出），默认 best 选择指标是 `eval/simulated_acc_len`。

## 7. 下一步学习建议

- 想了解评测侧如何聚合出 `eval/simulated_acc_len` 及其与接受率的关系，看 [u9-l4 导出、评测与基准](u9-l4-export-eval-benchmark.md)，那里会讲 `Evaluator` 与 best 检查点选择的完整链路。
- 想看检查点/resume 如何恢复 `global_step`（profiler 与 logger 对齐的根基），复习 [u9-l1 检查点与恢复](u9-l1-checkpoint-resume.md)。
- 想理解 `lr` 这个 `train/lr` 指标背后的学习率调度，看 [u9-l2 优化器与学习率调度](u9-l2-optimizer-scheduler.md)。
- 推荐精读源码：`specforge/training/profiling.py`（很短，是「状态机 + 边界对齐」的极好范例）与 `specforge/training/tracking.py`（「接缝层」模式的范例）。

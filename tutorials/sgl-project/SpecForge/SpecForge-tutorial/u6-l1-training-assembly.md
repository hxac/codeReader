# 训练装配 assembly

> 单元 u6 · 训练主链路 · 第 1 讲
> 依赖：u3-l4（应用组合根 composition）

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清 `build_training_run` 在整条启动链路里的位置——它是组合根（u3-l4）之后的**第一个装配落点**，把「已解析的算法对象 + 一份配置」变成「一个可以 `.run()` 的 `TrainingRun`」。
2. 读懂 `TrainingRun` 这个值对象如何用**单一生命周期**统一 trainer 型运行和 producer 型运行，并理解 `__post_init__` 里的两条不变量。
3. 读懂 `build_model_bundle` 如何消费 `algorithm.providers.model` 的各个端口，把**草稿模型、目标模型元信息、tokenizer/输入工具、捕获层、检查点策略**打包成一个 `ModelBundle`。
4. 在源码里准确指出三个关键装配步骤——**草稿模型加载、tokenizer 加载、离线 vocab mapping 安装**——分别调用哪个函数、依赖哪个配置段（本讲的主任务）。
5. 读懂优化器工厂、dataloader worker 数、profiling、tracker 这些「公共启动参数」是如何被集中装配并下发给运行时的。

本讲只讲「装配（assembly）」：从 `build_training_run` 进、到把 `trainer` 交给下游运行时出。**不**展开训练循环本身（那是 u6-l3 的 `Trainer`/`TrainerController`）、**不**展开 FSDP backward 细节（u6-l4）、也**不**展开各算法策略的差异（u6-l2）。

---

## 2. 前置知识

### 2.1 承接 u3-l4：装配层从组合根手里接到什么

在 u3-l4 里你已知：组合根 `build_application_run` 最终调用的是本讲的 `build_training_run`，并且传下去的是**两个对象**，而不是 strategy 字符串：

```python
# specforge/application/composition.py:133-145
def build_application_run(run, registry=None):
    resolved = run if isinstance(run, ResolvedRun) else resolve_run(run, registry)
    from specforge.training.assembly import build_training_run
    return build_training_run(resolved.config, algorithm=resolved.algorithm)
```

也就是说，装配层 `assembly.py` 拿到的输入是：

- `cfg: Config` —— 一份**已校验**的类型化配置（七段 + `run_id`/`output_dir`）。
- `algorithm: AlgorithmRegistration` —— 一个不可变对象，同时含**纯契约** `spec` 和**可执行端口** `providers`（u4-l1/u4-l2/u4-l3）。

装配层的职责就一句话：**把这两个对象「拼」成一组能跑的对象**。它在开头会再做一次防御性校验，但绝不再去查算法名——解析权被锁死在组合根里（u3-l4 反复强调过这一点）。

### 2.2 承接 u4-l3：providers 是「会动的半边」

装配层大量调用 `algorithm.providers.xxx`。回顾 u4-l3 的端口划分，本讲会用到这几组：

| 端口组 | 典型成员 | 装配层怎么用 |
|--------|----------|--------------|
| `model` | `draft_config`、`build_draft`、`build_training_model`、`needs_input_tools`、`resolve_capture_layers`、`minimum_loss_tokens`、`default_dataloader_num_workers` | 建草稿模型、建组合训练模型、决定是否要 tokenizer |
| `step` | `bind_runtime` | 把检查点策略绑到活模型上，产出 `strategy_kwargs` |
| `server_streaming` | `server_streaming_for(modality)` | 在线模式下拿输入适配器 |
| 顶层 | `vocab_mapping_modes` | 判断是否需要离线推导 vocab 映射 |

如果你对 `AlgorithmProviders` 的整体结构还不熟，可快速回看 u4-l3；本讲用到时会给一句话提示。

### 2.3 两条正交轴线决定了「装配走哪条路」

承接 u2-l1 与 u3-l3：

- **数据模式** `cfg.mode`（`offline` / `online`，由 `data.hidden_states_path` 是否为空推导）。
- **部署模式** `cfg.deployment.mode`（`local_colocated` / `disaggregated`）。

这两条轴线的组合决定了 `build_training_run` 内部的分发：

| `cfg.mode` | `cfg.deployment.mode` | 装配走向 |
|------------|------------------------|----------|
| `offline` | `local_colocated` | 本进程内 `build_offline_runtime`（colocated） |
| `offline` | `disaggregated` | `build_disaggregated_run` 的 offline 分支（producer 灌特征 / consumer 训练） |
| `online` | `disaggregated` | `build_disaggregated_run` 的 online 分支（producer 捕获 / consumer 训练） |
| `online` | `local_colocated` | **不存在**——装配层直接 fail-fast |

记住这张表，第 4.2 节的源码就是把它逐行写出来。

### 2.4 「装配」与「运行」是两个阶段

一个容易混淆的点：`build_training_run` 只负责**把对象建好、接好线**，它返回的 `TrainingRun` 此时**还没有开始训练**。真正「按下启动键」的是组合根或 CLI 调用的 `.run()`（u3-l4 末尾的 `build_application_run(resolved).run()`）。理解这一点，才能看懂为什么 `build_training_run` 里到处是「构造工厂」而不是「立刻执行」。

---

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 职责 |
|------|------|
| [specforge/training/assembly.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py) | **本讲主角**：`TrainingRun`、`ModelBundle`、`build_model_bundle`、`build_training_run` 及一批装配辅助函数 |
| [specforge/training/model_loading.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/model_loading.py) | 草稿配置解析（`resolve_draft_config`）与「仅权重」热启动（`warm_start_draft_model`），与训练恢复严格分离 |
| [specforge/training/vocab_mapping.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/vocab_mapping.py) | 离线特征 token 计数（`count_effective_feature_tokens`），用于推导 EAGLE 词表压缩映射 |
| [specforge/training/disaggregated.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/disaggregated.py) | disaggregated 模式的角色装配（producer / consumer），由 `build_training_run` 分发进入 |
| [specforge/training/provenance.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/provenance.py) | 冻结模型输入的稳定身份（`model_resume_provenance`），供恢复比对 |
| [specforge/application/composition.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/composition.py) | 上游调用方：`build_application_run` 调 `build_training_run` |
| [specforge/launch.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py) | 下游消费者：`build_offline_runtime` 等拓扑构建器接收装配产物 |

一句话总览：**`build_training_run`（总入口分发）→ `build_model_bundle`（建模型包）→ `build_offline_runtime`/`build_disaggregated_run`（交给运行时）→ 包成 `TrainingRun`**。优化器工厂、dataloader 配置、profiling、tracker 在中间被集中装配后作为参数下发。

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：`TrainingRun` 值对象与统一生命周期、`build_training_run` 总入口与部署分发、`ModelBundle` 与 `build_model_bundle`、优化器工厂与公共启动参数装配。

### 4.1 TrainingRun：一个值对象统一两种运行

#### 4.1.1 概念说明

SpecForge 有两类「运行」：

1. **trainer 型运行**：consumer/colocated 进程，需要建模型、跑前向反向、写检查点。这是大多数人心里的「训练」。
2. **producer 型运行**：disaggregated 模式下的 producer 进程，只负责**捕获/发布特征**，不算 loss、不写训练检查点（详见 u7-l3/u7-l5）。

这两类运行的「执行体」完全不同：trainer 型有一个 `trainer` 对象（其 `.fit()` 跑完整个训练循环），producer 型只有一个 `execute()` 闭包（把特征灌进存储就返回）。`assembly.py` 的设计选择是：**用一个值对象 `TrainingRun` 同时承载这两种执行体**，并对外只暴露一个 `.run()` 方法。这样组合根和 CLI 永远只需要调 `TrainingRun.run()`，不必关心当前是哪种运行。

#### 4.1.2 核心流程

`TrainingRun` 的内部结构：

```
TrainingRun
├─ trainer        # trainer 型运行的可执行体（有 .fit()）
├─ execute        # producer 型运行的可执行体（一个无参闭包，返回 int）
├─ on_success     # 仅 trainer 型：fit 成功后回调
├─ on_failure     # 仅 trainer 型：fit 抛异常后回调
└─ on_finally     # 仅 trainer 型：无论成败都执行的清理

.run()
├─ execute 不为 None？ ──是──▶ 直接 execute()（producer 型）
└─ 否（trainer 型）：
       try:   result = trainer.fit()
              on_success(result)
       except: on_failure(exc); raise
       finally: on_finally()
```

关键不变量（在 `__post_init__` 里强制）：

- `trainer` 与 `execute` **二选一**：恰好一个非空。两个都空或两个都非空都报错。
- 生命周期回调（`on_success`/`on_failure`/`on_finally`）**只允许挂在 trainer 型运行上**。如果给了 `execute` 又给了回调，报错。

#### 4.1.3 源码精读

先看值对象本身：

```python
# specforge/training/assembly.py:59-77
@dataclass
class TrainingRun:
    """A fully assembled run with one lifecycle for rollout and training."""

    trainer: Any = None
    execute: Optional[Callable[[], int]] = None
    on_success: Optional[Callable[[int], None]] = None
    on_failure: Optional[Callable[[BaseException], None]] = None
    on_finally: Optional[Callable[[], None]] = None

    def __post_init__(self) -> None:
        if (self.trainer is None) == (self.execute is None):
            raise ValueError("a training run needs exactly one trainer or executor")
        if self.execute is not None and any(
            hook is not None
            for hook in (self.on_success, self.on_failure, self.on_finally)
        ):
            raise ValueError("lifecycle hooks belong to trainer-bearing runs only")
```

[specforge/training/assembly.py:59-77](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py#L59-L77) 定义了 `TrainingRun`。注意两条不变量都写在 `__post_init__`：这意味着**对象构造的那一刻**就会被校验，不可能构造出一个「既没 trainer 又没 execute」的残缺运行。`(self.trainer is None) == (self.execute is None)` 这个布尔等价写法很巧妙：它为真当且仅当两者**同为 None 或同为非 None**，正是要拒绝的两种情况。

再看 `.run()` 如何统一两种执行体：

```python
# specforge/training/assembly.py:78-92
    def run(self) -> int:
        if self.execute is not None:
            return self.execute()
        try:
            result = self.trainer.fit()
            if self.on_success is not None:
                self.on_success(result)
            return result
        except BaseException as exc:
            if self.on_failure is not None:
                self.on_failure(exc)
            raise
        finally:
            if self.on_finally is not None:
                self.on_finally()
```

[specforge/training/assembly.py:78-92](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py#L78-L92) 是统一入口。两个要点：

1. **producer 型走 `execute()` 快车道**：不进 try/except，没有回调，跑完直接返回。这符合 producer「灌完特征就结束」的简单语义。
2. **trainer 型走完整生命周期**：`try/except/finally` 保证 `on_success`（成功）、`on_failure`（异常，但 `raise` 重新抛出，不吞错）、`on_finally`（必跑的清理）三段都被正确触发。`on_failure` 里 **`raise`** 这一行很关键——它记录失败后**仍然把异常上抛**，不会把错误藏起来。

这套回调机制不是摆设：在 disaggregated offline 模式里，consumer 进程会拿到一个带 `on_success=mark_consumed`、`on_failure=mark_consumer_failed` 的 `TrainingRun`（见 disaggregated.py 的 `_build_offline` 末尾），用来通过文件系统控制位通知 producer「我消费完了 / 我失败了」。这正是 `TrainingRun` 把「运行结果」和「跨进程协调」解耦的方式。

#### 4.1.4 代码实践

**实践目标**：用 Python 解释器验证 `TrainingRun.__post_init__` 的两条不变量，确认它无法构造出非法状态。

**操作步骤**：

1. 在仓库根目录启动一个 Python 解释器（不需要 GPU，`assembly.py` 的重型依赖都是懒加载的）：
   ```bash
   python -c "from specforge.training.assembly import TrainingRun; print('ok')"
   ```
   预期打印 `ok`，说明模块本身可无副作用导入。
2. 依次尝试构造三种非法 `TrainingRun`，观察报错：
   ```python
   from specforge.training.assembly import TrainingRun
   TrainingRun()                          # ① 两者都空
   TrainingRun(trainer=object(), execute=lambda: 0)  # ② 两者都非空
   TrainingRun(execute=lambda: 0, on_success=lambda r: None)  # ③ execute 却挂回调
   ```
3. 再构造一个合法的 trainer 型运行，确认不报错：
   ```python
   TrainingRun(trainer=object())          # 合法
   ```

**需要观察的现象**：

- ① 报 `a training run needs exactly one trainer or executor`。
- ② 报同一条（因为 `(trainer is None) == (execute is None)` 为真）。
- ③ 报 `lifecycle hooks belong to trainer-bearing runs only`。
- 合法构造无报错。

**预期结果**：三条非法构造全部在构造期被拦下，证明不变量是「硬约束」而非文档约定。这也解释了为什么下游（如 disaggregated.py）敢于放心地只填 `execute=` 或只填 `trainer=`——`TrainingRun` 自己会守住边界。

> ⚠️ 待本地验证：步骤 1 的导入是否真正无副作用，取决于你本地是否装齐了 `specforge` 的轻量依赖（u1-l2）。若导入失败，先按 u1-l2 装好环境。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `TrainingRun` 用「`trainer` 与 `execute` 二选一」而不是定义两个类（如 `TrainerRun` / `ProducerRun`）？

> **参考答案**：为了让上游（组合根、CLI）只有一个统一的 `.run()` 入口。如果分两个类，`build_training_run` 的返回类型就不是单一的，调用方就得先判断类型再决定怎么跑，分支会扩散到各处。用一个值对象 + 二选一约束，把「两种运行」的差异吸收进对象内部，对外保持单一接口。代价是两条不变量需要在构造期校验——这正是 `__post_init__` 做的事。

**练习 2**：`on_failure` 里为什么要在记录之后还要 `raise`？

> **参考答案**：`on_failure` 的职责是「副作用」（比如通知 producer 我失败了），不是「错误处理」。如果它吞掉异常，调用方（CLI）会以为训练成功，退出码变成 0，CI/脚本无法察觉失败。`raise` 把异常原样上抛，保证「失败一定被上层看见」，同时 `on_failure` 已经完成了它该做的通知。

---

### 4.2 build_training_run：装配总入口与部署分发

#### 4.2.1 概念说明

`build_training_run` 是装配层的**总入口**，也是组合根直接对接的函数。它做三件事：

1. **守边界**：再次确认传进来的 `algorithm` 与 `cfg.training.strategy` 匹配（防御性，u3-l4 讲过）。
2. **校验拓扑**：对非 producer 角色，校验 `world_size` 能否被 TP/SP 整除；对 online 模式，强制要求 disaggregated。
3. **分发**：按「数据模式 × 部署模式」把装配工作路由到 colocated offline、disaggregated offline、disaggregated online 三条路径之一。

它本身**不建模型**——建模型是 `build_model_bundle` 的事（4.3 节）。它只决定「调用哪个下游构建器」，并把 `build_model_bundle`、`_prepare_prompts`、优化器工厂、logger 作为**回调/工厂**传给 disaggregated 分支（因为 disaggregated 的 producer 与 consumer 各自只需要装配的一部分）。

#### 4.2.2 核心流程

```
build_training_run(cfg, algorithm)
   │
   ├─ 0. 防御性校验：algorithm.name == cfg.training.strategy
   ├─ 1. 非 producer → cfg.validate_world_size(world_size)
   ├─ 2. online 且非 disaggregated → fail-fast
   │
   ├─ deployment.mode == "disaggregated"？
   │     └─ 是 → build_disaggregated_run(cfg, algorithm, build_model_bundle=...,
   │                                     prepare_prompts=..., optimizer_factory=...,
   │                                     logger=run_logger)
   │            （内部再按 mode/role 分 _build_offline / _build_online，u3-l3 讲过）
   │
   ├─ cfg.mode != "offline" → fail-fast（走到这说明 colocated 却非 offline，非法）
   │
   └─ 否（colocated offline）：
          ├─ bundle = build_model_bundle(cfg, algorithm)        # 4.3 节
          ├─ _ensure_offline_vocab_mapping(cfg, bundle, algorithm)
          ├─ run_logger = _configured_logger(cfg)
          ├─ trainer = build_offline_runtime(..., **_common_launch_kwargs(...))
          └─ return TrainingRun(trainer=trainer)
```

三个 fail-fast 点把「不支持的组合」拦在装配阶段，而不是训练跑到一半才崩。

#### 4.2.3 源码精读

先看入口与三道校验：

```python
# specforge/training/assembly.py:549-577
def build_training_run(cfg: Config, *, algorithm: AlgorithmRegistration) -> TrainingRun:
    """Assemble one validated run from an already-resolved algorithm. ..."""

    if algorithm.name != cfg.training.strategy:
        raise ValueError(
            "resolved algorithm does not match training.strategy: "
            f"{algorithm.name!r} != {cfg.training.strategy!r}"
        )

    t = cfg.training
    if t.role != "producer":
        import torch.distributed as dist
        cfg.validate_world_size(dist.get_world_size() if dist.is_initialized() else 1)

    if cfg.mode == "online" and cfg.deployment.mode != "disaggregated":
        raise ValueError(
            "online training is server-only and requires "
            "deployment.mode='disaggregated'"
        )
```

[specforge/training/assembly.py:549-577](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py#L549-L577) 是装配总入口。三处要点：

1. **L561-565 防御性匹配校验**：`algorithm.name != cfg.training.strategy` 直接报错。u3-l4 已解释——组合根校验过，这里只是装配层的入口不变量，防止有人绕过组合根直接调本函数传错配对。
2. **L567-571 world_size 校验只对非 producer 做**：producer 不初始化 CUDA、不参与张量并行（u3-l1/u7-l5），所以跳过。`dist.is_initialized()` 为假时传 `1`，兼容单进程。真正的整除校验在 `Config.validate_world_size`：

```python
# specforge/config/schema.py:863-878
def validate_world_size(self, world_size: int) -> None:
    if world_size < 1:
        raise ValueError(f"world_size must be positive, got {world_size}")
    tp_size = self.training.tp_size
    sp_size = self.training.sp_ulysses_size * self.training.sp_ring_size
    if world_size % tp_size:
        raise ValueError(f"world_size={world_size} must be divisible by training.tp_size={tp_size}")
    if world_size % sp_size:
        raise ValueError(f"world_size={world_size} must be divisible by draft sequence parallel size {sp_size} ...")
```

[specforge/config/schema.py:863-878](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L863-L878) 把分布式整除约束前置到装配期：`world_size` 必须同时被 `tp_size` 和 `sp_ulysses_size * sp_ring_size` 整除，否则 fail-fast。这就是 u8-l1 会展开的「world size 整除约束」在装配层的落点。

3. **L573-577 online 必须 disaggregated**：把 u3-l3 的拓扑铁律写成代码——colocated online 不存在。

接着看 disaggregated 分支怎么把「工厂」传下去：

```python
# specforge/training/assembly.py:579-601
    if cfg.deployment.mode == "disaggregated":
        from specforge.training.disaggregated import build_disaggregated_run
        run_logger = _configured_logger(cfg)
        try:
            return build_disaggregated_run(
                cfg,
                algorithm=algorithm,
                build_model_bundle=lambda run_cfg: build_model_bundle(run_cfg, algorithm=algorithm),
                prepare_prompts=lambda run_cfg, tokenizer, **kwargs: _prepare_prompts(run_cfg, tokenizer, algorithm=algorithm, **kwargs),
                optimizer_factory=_optimizer_factory,
                logger=run_logger,
            )
        except BaseException:
            _close_configured_logger(run_logger)
            raise
```

[specforge/training/assembly.py:579-601](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py#L579-L601) 是 disaggregated 分发。关键设计：`build_model_bundle` 和 `_prepare_prompts` 被**包成 lambda** 再传给 `build_disaggregated_run`，而不是直接传函数引用。为什么？因为 disaggregated 内部的 producer 和 consumer **各自只需要装配的一部分**（producer 通常不需要建完整 trainer，consumer 才需要），把「建 bundle」做成回调，让 `build_disaggregated_run` 自己决定何时、是否调用它。`try/except` 里 `_close_configured_logger` 保证 logger 在装配失败时被正确关闭，不泄漏 wandb/tensorboard 会话。

最后看 colocated offline 分支（本讲后续 4.3/4.4 节的主战场）：

```python
# specforge/training/assembly.py:603-633
    if cfg.mode != "offline":
        raise ValueError("colocated execution supports offline training only")

    bundle = build_model_bundle(cfg, algorithm=algorithm)
    from specforge.launch import build_offline_runtime

    _ensure_offline_vocab_mapping(cfg, bundle, algorithm)
    run_logger = _configured_logger(cfg)
    try:
        trainer = build_offline_runtime(
            hidden_states_path=cfg.data.hidden_states_path,
            eval_hidden_states_path=cfg.data.eval_hidden_states_path or None,
            draft_model=bundle.model,
            target_head=bundle.target_head,
            ttt_length=t.ttt_length,
            max_len=cfg.data.max_length,
            num_epochs=t.num_epochs,
            use_usp_preprocess=(t.attention_backend == "usp"),
            seed=t.seed,
            resume_from=t.resume_from,
            **_common_launch_kwargs(cfg, bundle, algorithm, logger=run_logger),
        )
    except BaseException:
        _close_configured_logger(run_logger)
        raise
    return TrainingRun(trainer=trainer)
```

[specforge/training/assembly.py:603-633](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py#L603-L633) 是 colocated offline 的完整装配。顺序很清晰：

1. `build_model_bundle` → 建好模型包（4.3 节）。
2. `_ensure_offline_vocab_mapping` → 用刚建好的 bundle 推导并安装离线 vocab 映射（4.3 节的实践会用到）。
3. `_configured_logger` → 建 tracker（4.4 节）。
4. `build_offline_runtime` → 把 bundle + 一堆「公共启动参数」（`**_common_launch_kwargs(...)`，4.4 节）交给运行时构建器，产出 `trainer`。
5. 包成 `TrainingRun(trainer=trainer)` 返回。

注意 `build_offline_runtime` 来自 `specforge/launch.py`（u3-l3），它才是真正把 `Trainer`/`FeatureDataLoader`/`TrainerController` 串起来的地方——本讲到「把 trainer 交给 launch」就停，训练循环是 u6-l3。

#### 4.2.4 代码实践

**实践目标**：给定一份配置的两个字段（`cfg.mode`、`cfg.deployment.mode`），判断 `build_training_run` 会走哪条分支、是否会 fail-fast。

**操作步骤**：

1. 打开 [specforge/training/assembly.py:549-633](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py#L549-L633)。
2. 对下面四种（配置模式, 部署模式）组合，分别写出：会命中哪个 `if`、是 fail-fast 还是正常分发、最终调用哪个下游构建器。

   | 组合 | `cfg.mode` | `cfg.deployment.mode` | 走向？ |
   |------|------------|------------------------|--------|
   | A | offline | local_colocated | ？ |
   | B | offline | disaggregated | ？ |
   | C | online | disaggregated | ？ |
   | D | online | local_colocated | ？ |

3. 在源码里标出每种组合命中的具体行号。

**需要观察的现象**：

- A 不命中 L579 的 disaggregated 分支，也不命中 L573 的 online fail-fast，落到 L606 之后的 colocated offline 装配，最终调 `build_offline_runtime`。
- B 命中 L579 的 disaggregated 分支，调 `build_disaggregated_run`（内部再走 `_build_offline`）。
- C 命中 L579 的 disaggregated 分支，调 `build_disaggregated_run`（内部再走 `_build_online`）。
- D 命中 L573-577 的 fail-fast，报 `online training is server-only and requires deployment.mode='disaggregated'`。

**预期结果**：四种组合的走向与本讲 2.3 节那张「装配走向表」完全一致。这证明 `build_training_run` 的分发逻辑就是把「数据模式 × 部署模式」这两条正交轴线逐行翻译成 `if` 分支。

> 说明：本实践是「源码阅读型实践」，不需要 GPU。行号以 HEAD `a4fca14` 为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 disaggregated 分支要把 `build_model_bundle` 包成 `lambda` 再传，而 colocated 分支直接调 `build_model_bundle(cfg, algorithm=algorithm)`？

> **参考答案**：colocated 进程只有一个角色，装配顺序由 `build_training_run` 自己掌控，所以直接调用。disaggregated 有 producer 和 consumer 两个角色，且 producer 往往不需要建完整 trainer（它只灌特征），「何时建 bundle、建不建」的决定权应该交给 `build_disaggregated_run`——把 `build_model_bundle` 包成回调（闭包里已经绑定了 `algorithm`），就是把「建 bundle 的能力」下放，让 disaggregated 装配器按角色按需调用。

**练习 2**：`build_training_run` 里有两处 `try/except ... _close_configured_logger(run_logger); raise`（L599-601 与 L630-632）。它们解决了什么问题？

> **参考答案**：`_configured_logger` 可能创建一个外部 tracker 会话（wandb/tensorboard/swanlab/mlflow，见 4.4 节）。如果装配中途抛异常（比如 `build_offline_runtime` 里某个参数非法），不关闭 logger 就会泄漏一个挂着的会话。这两处 `try/except` 保证「装配失败时也关闭 logger」，同时 `raise` 把原始异常上抛，不吞错。这是资源清理与错误传播兼顾的标准写法。

---

### 4.3 ModelBundle 与 build_model_bundle：把模型真正建出来

#### 4.3.1 概念说明

`ModelBundle` 是装配的**核心产物**之一：它把一次训练需要的「所有模型相关对象和元信息」打包成一个数据类。为什么要打包？因为下游运行时（`build_offline_runtime` 等）需要的不只是「一个模型」——它还要知道目标模型的隐藏层维度、词表大小、捕获了哪几层、检查点策略怎么过滤……如果这些散落在十几个局部变量里传参，既容易传错也难维护。`ModelBundle` 把它们收拢成一个对象，下游只接收一个参数。

`build_model_bundle` 是构造 `ModelBundle` 的函数，它密集调用 `algorithm.providers.model` 的各个端口——这正是 u4-l3 讲的「provider 端口」在装配层的真实用法。它的工作可以分成五步：建草稿模型、（按需）建输入工具、读目标模型元信息、建组合训练模型、绑定检查点策略。

本节同时承载本讲的**主任务**：草稿模型加载、tokenizer 加载、离线 vocab mapping 安装这三个步骤分别调哪个函数、依赖哪个配置段。其中前两步在 `build_model_bundle` 内部，第三步（vocab mapping）在 `build_training_run` 里、`build_model_bundle` 之后调用——但它们都属于「装配期建模型」这一整体。

#### 4.3.2 核心流程

```
build_model_bundle(cfg, algorithm)
   │
   ├─ ① _load_draft(cfg, algorithm)                       # 建草稿模型
   │      ├─ resolve_draft_config(cfg, provider=...)      #   model_loading.py
   │      ├─ provider.build_draft(cfg, draft_config)
   │      └─ resolve_draft(architecture) + isinstance 校验
   │
   ├─ ② needs_input_tools？→ _load_input_tools(cfg, algorithm)   # 建 tokenizer/输入工具
   │      └─ _load_text_tokenizer(cfg)  （text 模态默认）
   │
   ├─ ③ load_target_config(...) → target_hidden_size / target_vocab_size   # 读目标元信息
   │
   ├─ ④ provider.build_training_model(cfg, draft, draft_cfg, target_cfg, input_tools)
   │      └─ 返回 parts：composite model + target_head + capture_layers
   │      └─ online 且无 capture_layers → provider.resolve_capture_layers(...) 兜底
   │      └─ target_head.requires_grad_(False)            # 冻结目标头
   │
   └─ ⑤ strategy_kwargs = step.bind_runtime(cfg, draft_model, parts.model,
                                            model_provenance=model_resume_provenance(...))
                                                                          # 绑检查点策略
   → 返回 ModelBundle(model, draft_model, draft_config, input_tools, target_head,
                      target_hidden_size, target_vocab_size, draft_vocab_size,
                      capture_layers, strategy_kwargs)
```

vocab mapping 安装（本讲主任务的第三步）发生在 `build_model_bundle` 返回之后：

```
build_training_run（colocated offline 分支）
   └─ bundle = build_model_bundle(...)
   └─ _ensure_offline_vocab_mapping(cfg, bundle, algorithm)   # ③ vocab mapping 安装
          ├─ 算法是否声明 OFFLINE vocab_mapping_modes？否 → 直接返回
          ├─ 已给 vocab_mapping_path 或词表等宽？是 → 直接返回
          ├─ count_effective_feature_tokens(hidden_states_path, ...)   # vocab_mapping.py
          └─ _install_dataset_vocab_mapping(...)   # 推导 d2t/t2d、写入 bundle.draft_model、rank0 落盘
```

#### 4.3.3 源码精读

**① 草稿模型加载**：这是主任务的第一步。

```python
# specforge/training/assembly.py:111-127
def _load_draft(cfg: Config, algorithm: AlgorithmRegistration):
    """Construct the configured draft model without any legacy trainer code."""
    from specforge.modeling.draft.registry import resolve_draft
    from specforge.training.model_loading import resolve_draft_config

    provider = algorithm.providers.model
    draft_config = resolve_draft_config(cfg, provider=provider.draft_config)
    draft_model = provider.build_draft(cfg, draft_config)
    architecture = provider.draft_config.architecture
    expected_type = resolve_draft(architecture)
    if not isinstance(draft_model, expected_type):
        raise ValueError(
            f"training.strategy={algorithm.name!r} requires {architecture}, but "
            f"the resolved draft config builds {type(draft_model).__name__}"
        )
    return draft_config, draft_model
```

[specforge/training/assembly.py:111-127](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py#L111-L127) 是草稿模型加载。它做三件事，且最后一件是**算法轴与架构轴的交汇校验**（u4-l4 讲过这两条轴）：

1. `resolve_draft_config(cfg, provider=...)`：解析草稿配置（下面细讲）。
2. `provider.build_draft(cfg, draft_config)`：用算法提供的工厂把草稿模型实例化。
3. `resolve_draft(architecture)` 拿到该架构名注册的**模型类**，再用 `isinstance` 确认上一步建出来的模型确实是这个类——防止「配置声明的是 A 架构，实际建出 B 架构」。

这一步**依赖 `model` 配置段**：`draft_model_config`、`draft_checkpoint_path`、`draft_num_hidden_layers`、`draft_block_size`、`cache_dir`、`trust_remote_code`。

草稿配置解析的细节在 `model_loading.py`：

```python
# specforge/training/model_loading.py:273-298
def resolve_draft_config(cfg, *, provider):
    """Resolve and validate the draft architecture for one typed run config."""
    source = cfg.model.draft_model_config
    if not source:
        source = _checkpoint_config_source(cfg.model.draft_checkpoint_path)
    if source:
        draft_config = load_draft_config_source(source, cache_dir=cfg.model.cache_dir,
                                                trust_remote_code=cfg.model.trust_remote_code)
    else:
        draft_config = _generate_draft_config(cfg, provider)

    expected = provider.architecture
    architectures = list(getattr(draft_config, "architectures", None) or [])
    if architectures != [expected]:
        raise ValueError(
            f"training.strategy={cfg.training.strategy!r} requires draft "
            f"architecture {expected}, got {architectures!r}"
        )
    _apply_draft_overrides(cfg, draft_config, provider)
    return draft_config
```

[specforge/training/model_loading.py:273-298](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/model_loading.py#L273-L298) 是草稿配置的三级来源回退：

1. 优先用 `model.draft_model_config`（显式配置/目录/HF repo）。
2. 没有就尝试从 `model.draft_checkpoint_path` 里找同目录的 `config.json`（`_checkpoint_config_source`，[model_loading.py:161-191](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/model_loading.py#L161-L191)）。
3. 都没有就「按目标模型自动生成」一份（`_generate_draft_config`，[model_loading.py:209-256](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/model_loading.py#L209-L256)）——从目标配置白名单字段（`_TARGET_ARCHITECTURE_FIELDS`，[model_loading.py:47-68](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/model_loading.py#L47-L68)）拷贝架构相关项，再填上算法声明的 `model_type`/`num_hidden_layers`。

末尾的 `architectures != [expected]` 校验保证「解析出的草稿架构」与「算法要求的架构」必须严格一致——这就是 u4-l4 里「config_class 在注册时的强制作用」在装配侧的对偶检查。`_apply_draft_overrides`（[model_loading.py:259-270](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/model_loading.py#L259-L270)）再叠加用户覆盖（`draft_num_hidden_layers`、`draft_block_size`）。

> 顺带注意 `model_loading.py` 的模块定位（[model_loading.py:15-21](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/model_loading.py#L15-L21)）：它**只做「仅权重」的热启动**（`warm_start_draft_model`），刻意不碰优化器/调度器/计数器/RNG——那属于完整恢复（`resume_from`），是 u9-l1 的主题。这种「模型初始化与训练恢复分离」的边界，是装配层能保持清晰的关键。

**② tokenizer / 输入工具加载**：主任务的第二步。

```python
# specforge/training/assembly.py:163-176
def _load_input_tools(cfg, algorithm, *, input_adapter=None):
    """Load modality tooling through the provider port or the text default."""
    if input_adapter is None and cfg.mode == "online":
        streaming = algorithm.providers.server_streaming_for(cfg.model.input_modality)
        input_adapter = streaming.create_input_adapter(cfg)
    if input_adapter is not None:
        return input_adapter.load_input_tools(cfg)
    return _load_text_tokenizer(cfg)
```

[specforge/training/assembly.py:163-176](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py#L163-L176) 是输入工具的分发：在线模式下先从 `server_streaming` provider 拿一个输入适配器，再让它加载工具；否则走 text 默认路径 `_load_text_tokenizer`。真正的 tokenizer 加载在：

```python
# specforge/training/assembly.py:130-160
def _load_text_tokenizer(cfg: Config):
    """Load tokenizer tooling used by current built-in text providers."""
    if cfg.model.input_modality != "text":
        raise ValueError(
            "built-in algorithms currently provide training-model input tooling "
            f"only for modality 'text', got {cfg.model.input_modality!r}; "
            "another modality must add its own input provider"
        )
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(
        cfg.model.target_model_path,
        cache_dir=cfg.model.cache_dir,
        trust_remote_code=cfg.model.trust_remote_code,
    )
    if cfg.model.tokenizer_pad_token_id is not None:
        tokenizer.pad_token_id = cfg.model.tokenizer_pad_token_id
    elif tokenizer.pad_token_id is None:
        fallback_id = tokenizer.eos_token_id
        if isinstance(fallback_id, (list, tuple)):
            fallback_id = fallback_id[0] if fallback_id else None
        if fallback_id is None:
            fallback_id = tokenizer.unk_token_id
        if fallback_id is None:
            raise ValueError(
                "target tokenizer has no pad, EOS, or unknown token ID; set "
                "model.tokenizer_pad_token_id explicitly"
            )
        tokenizer.pad_token_id = fallback_id
    return tokenizer
```

[specforge/training/assembly.py:130-160](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py#L130-L160) 是 tokenizer 加载。注意它用**目标模型路径** `cfg.model.target_model_path` 加载 tokenizer（草稿模型与目标模型共用同一份词表与分词器）。pad token 的处理很讲究：优先用用户显式指定的 `tokenizer_pad_token_id`；否则按 `eos → unk` 顺序兜底；都没有就 fail-fast，要求用户显式设置。

这一步**依赖 `model` 配置段**：`target_model_path`、`cache_dir`、`trust_remote_code`、`tokenizer_pad_token_id`、`input_modality`。

**③ 离线 vocab mapping 安装**：主任务的第三步，发生在 `build_training_run` 里、`build_model_bundle` 之后。

```python
# specforge/training/assembly.py:449-488
def _ensure_offline_vocab_mapping(cfg, bundle, algorithm):
    """Derive a local-offline map from the exact feature ids and loss masks."""
    if FeatureMode.OFFLINE not in algorithm.providers.vocab_mapping_modes:
        return
    if (cfg.model.vocab_mapping_path or bundle.draft_vocab_size == bundle.target_vocab_size):
        return

    from specforge.runtime.data_plane.offline_reader import list_feature_files
    from specforge.training.vocab_mapping import count_effective_feature_tokens

    identity_parts = []
    for path in list_feature_files(cfg.data.hidden_states_path):
        stat = os.stat(path)
        identity_parts.append((os.path.abspath(path), stat.st_size, stat.st_mtime_ns))
    identity = json.dumps({"kind": "offline-features-v1", "files": identity_parts,
                           "max_length": cfg.data.max_length}, sort_keys=True)
    counts = count_effective_feature_tokens(
        cfg.data.hidden_states_path,
        max_length=cfg.data.max_length,
        target_vocab_size=bundle.target_vocab_size,
    )
    _install_dataset_vocab_mapping(cfg, bundle, counts=counts, dataset_identity=identity)
```

[specforge/training/assembly.py:449-488](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py#L449-L488) 是离线 vocab mapping 的「编排」。三个早返回点决定「是否需要推导」：

1. 算法没声明 `OFFLINE` 在 `vocab_mapping_modes` 里 → 不需要（比如非 EAGLE 类算法）。
2. 用户已给 `model.vocab_mapping_path`，或草稿词表与目标词表**等宽**（`draft_vocab_size == target_vocab_size`，即不做压缩）→ 不需要。
3. 否则：用 `count_effective_feature_tokens` 直接从离线特征文件里数 token 频次，再交给 `_install_dataset_vocab_mapping` 推导映射。

`count_effective_feature_tokens` 在 `vocab_mapping.py`：

```python
# specforge/training/vocab_mapping.py:17-68
def count_effective_feature_tokens(hidden_states_path, *, max_length=None, target_vocab_size=None):
    """Count loss-bearing tokens directly from prepared offline features. ..."""
    from specforge.runtime.data_plane.feature_store import load_feature_file
    from specforge.runtime.data_plane.offline_reader import list_feature_files

    paths = list_feature_files(hidden_states_path)
    if not paths:
        raise ValueError(f"no offline feature files found under {hidden_states_path!r}")

    counts: Counter = Counter()
    for path in paths:
        raw = load_feature_file(path)
        missing = [name for name in ("input_ids", "loss_mask") if name not in raw]
        if missing:
            raise KeyError(f"{path} cannot derive an EAGLE vocab mapping; missing {missing}")
        input_ids = raw["input_ids"].reshape(-1)
        loss_mask = raw["loss_mask"].reshape(-1)
        ...
        selected = input_ids[loss_mask.to(dtype=bool)]   # 只数「带 loss」的 token
        ...
        for token_id, frequency in zip(token_ids.tolist(), frequencies.tolist()):
            ...
            counts[token_id] += int(frequency)
    return counts
```

[specforge/training/vocab_mapping.py:17-68](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/vocab_mapping.py#L17-L68) 直接读离线特征文件，**只统计 `loss_mask` 为真的 token**（即 assistant 可监督区间，u5-l2 讲过 loss mask）。这避免了一个常见陷阱：不必为了重建词表映射而重新加载原始对话数据集——离线特征里已经存了精确的 `input_ids` 和 `loss_mask`（u5-l3）。这一步**依赖 `data` 配置段**（`hidden_states_path`、`max_length`、`cache_dir`）和 `model` 段（`vocab_mapping_path`、经由 bundle 的 `draft_vocab_size`/`target_vocab_size`）。

> 词表压缩的直觉：当草稿词表比目标词表小（\(d < V\)）时，需要一个确定性映射把高频 token 保留、低频 token 映射到共享桶，使得草稿与目标能对齐概率分布。装配层在这里用「文件大小 + 修改时间 + max_length」算一个身份哈希作为缓存键（[assembly.py:466-477](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py#L466-L477)），让同样的离线特征不必重复推导。`_install_dataset_vocab_mapping`（[assembly.py:397-446](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py#L397-L446)）会 `copy_` 进草稿模型的 `d2t`/`t2d` 缓冲区，且**每个 rank 都自行推导同一份映射**（确定性），仅 rank0 落盘缓存，避免多进程并发写冲突。

**④/⑤ 组合模型与检查点策略**：回到 `build_model_bundle` 主体看后两步。

```python
# specforge/training/assembly.py:207-243
    parts = provider.build_training_model(
        cfg, draft_model, draft_config, target_config, input_tools
    )
    if cfg.mode == "online" and parts.capture_layers is None:
        parts.capture_layers = provider.resolve_capture_layers(cfg, draft_config, target_config)

    # Keep the composite and target parts bf16 while avoiding accidental target gradients.
    if parts.target_head is not None and isinstance(parts.target_head, torch.nn.Module):
        parts.target_head.requires_grad_(False)

    return ModelBundle(
        model=parts.model,
        draft_model=draft_model,
        draft_config=draft_config,
        input_tools=input_tools,
        target_head=parts.target_head,
        target_hidden_size=target_hidden_size,
        target_vocab_size=target_vocab_size,
        draft_vocab_size=draft_vocab_size,
        capture_layers=parts.capture_layers,
        strategy_kwargs=algorithm.providers.step.bind_runtime(
            cfg, draft_model, parts.model,
            model_provenance=_model_resume_provenance(
                cfg, draft_config, target_config, capture_layers=parts.capture_layers,
            ),
        ),
    )
```

[specforge/training/assembly.py:207-243](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py#L207-L243) 是 `build_model_bundle` 的收尾。四个要点：

1. `provider.build_training_model(...)` 把草稿模型包成「组合训练模型」（比如 EAGLE3 把草稿 + 目标头拼到一起），返回 `parts`（含 `model`、`target_head`、`capture_layers`）。
2. **online 且无 capture_layers 时**才调 `resolve_capture_layers` 兜底——离线的捕获层在 `prepare_hidden_states` 阶段就已固定（u5-l3），不需要这里再算。
3. `target_head.requires_grad_(False)`：目标头是**冻结的教师**，绝不能反传梯度。注释点明「保持 bf16 但屏蔽梯度」。
4. `strategy_kwargs` 由 `step.bind_runtime(...)` 产出（u4-l3 的 `StepProvider.bind_runtime`，[common/providers.py:291-345](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/common/providers.py#L291-L345)），它把「步选项 + 恢复契约 + 允许缺失的检查点键 + 模型来源身份」绑成一个不可变 `StepRuntimeConfig`，原样穿越所有 trainer 型拓扑，最终被检查点逻辑消费（u9-l1）。`model_resume_provenance`（[provenance.py:295-312](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/provenance.py#L295-L312)）给出恢复比对用的模型身份。

最后看一眼 `ModelBundle` 的字段定义，理解它为什么是「一次训练所需的一切」：

```python
# specforge/training/assembly.py:43-56
@dataclass
class ModelBundle:
    """Objects and capture metadata needed by one configured training run."""
    model: Any
    draft_model: Any
    draft_config: Any
    input_tools: Any = None
    target_head: Any = None
    target_hidden_size: int = 0
    target_vocab_size: int = 0
    draft_vocab_size: int = 0
    capture_layers: Optional[List[int]] = None
    strategy_kwargs: Mapping[str, Any] = field(default_factory=dict)
```

[specforge/training/assembly.py:43-56](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py#L43-L56) 定义了 `ModelBundle`。它同时持有「会动的对象」（`model`/`draft_model`/`target_head`/`input_tools`）和「静态元信息」（各 `*_size`、`capture_layers`、`strategy_kwargs`），后者让下游运行时不必再去读配置或目标模型就能拿到维度与策略。

#### 4.3.4 代码实践（本讲主任务）

**实践目标**：在 `assembly.py` 中准确标出「草稿模型加载、tokenizer 加载、离线 vocab mapping 安装」三个步骤分别调用的函数，并说明它们各依赖哪个配置段。

**操作步骤**：

1. 打开 [specforge/training/assembly.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py)，按下表逐项填写「调用函数」「所在行」「依赖配置段」三列。第一行已为你填好作为示范。

   | 步骤 | 调用函数（含模块） | assembly.py 行号 | 依赖配置段（字段） |
   |------|--------------------|------------------|--------------------|
   | 草稿模型加载 | `resolve_draft_config`（model_loading.py）+ `provider.build_draft` + `resolve_draft`（registry） | L192（在 `build_model_bundle` 内，经 `_load_draft` L111-127） | `model`：`draft_model_config`、`draft_checkpoint_path`、`draft_num_hidden_layers`、`draft_block_size`、`cache_dir`、`trust_remote_code` |
   | tokenizer 加载 | ？ | ？ | ？ |
   | 离线 vocab mapping 安装 | ？ | ？ | ？ |

2. 对 tokenizer 加载，钻进 [_load_text_tokenizer (assembly.py:130-160)](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py#L130-L160)，确认它从哪个字段读取 tokenizer 来源、从哪个字段读取 pad token。
3. 对 vocab mapping 安装，钻进 [_ensure_offline_vocab_mapping (assembly.py:449-488)](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py#L449-L488) 与 [count_effective_feature_tokens (vocab_mapping.py:17-68)](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/vocab_mapping.py#L17-L68)，确认它读哪两类配置段、什么条件下会「早返回、不推导」。

**需要观察的现象**：

- 草稿与 tokenizer 两步**都只依赖 `model` 段**——它们都属于「模型加载」这一类装配，不碰 `data`。
- vocab mapping 安装**同时依赖 `data` 段（`hidden_states_path`/`max_length`/`cache_dir`）和 `model` 段（`vocab_mapping_path` 及词表宽度）**——这正是它必须排在 `build_model_bundle` 之后（需要 bundle 里的 `draft_vocab_size`/`target_vocab_size`）的原因。

**预期结果**（参考答案）：

| 步骤 | 调用函数 | 行号 | 依赖配置段 |
|------|----------|------|------------|
| 草稿模型加载 | `_load_draft` → `resolve_draft_config`（model_loading.py:273）+ `provider.build_draft` + `resolve_draft` | assembly.py L192（经 L111-127） | `model` |
| tokenizer 加载 | `_load_input_tools` → `_load_text_tokenizer`（用 `AutoTokenizer.from_pretrained`） | assembly.py L194（经 L163-176 → L130-160） | `model`（`target_model_path`/`cache_dir`/`trust_remote_code`/`tokenizer_pad_token_id`/`input_modality`） |
| 离线 vocab mapping 安装 | `_ensure_offline_vocab_mapping` → `count_effective_feature_tokens`（vocab_mapping.py:17）+ `_install_dataset_vocab_mapping` | assembly.py L609（经 L449-488） | `data`（`hidden_states_path`/`max_length`/`cache_dir`）+ `model`（`vocab_mapping_path` 及 bundle 词表宽度） |

> 说明：本实践是「源码阅读型实践」，不需要 GPU 或真实模型。行号以 HEAD `a4fca14` 为准；若本地 HEAD 不同，函数名与调用关系不变，行号可能偏移。

#### 4.3.5 小练习与答案

**练习 1**：为什么「离线 vocab mapping 安装」要放在 `build_model_bundle` 之后，而不是塞进 `build_model_bundle` 内部？

> **参考答案**：因为它需要 `bundle.draft_vocab_size` 和 `bundle.target_vocab_size` 这两个值——它们是在 `build_model_bundle` 里由草稿配置与目标配置算出来的。把它放在 `build_model_bundle` 之后（assembly.py L609），正好拿到已建好的 bundle。此外，它在 colocated offline 分支才被调用（disaggregated 模式要求显式给 `vocab_mapping_path`，见 u3-l4 的 `_validate_vocab_mapping`），所以也不适合埋进「所有模式都走」的 `build_model_bundle` 里。

**练习 2**：`_load_text_tokenizer` 为什么从 `target_model_path` 而不是从某个「草稿模型路径」加载 tokenizer？

> **参考答案**：草稿模型与目标模型共用同一份词表和分词器——草稿是被训练去模仿目标的，二者必须能对齐到同一套 token 编码。因此 tokenizer 取自目标模型路径。草稿「模型」本身通常只是一层特征变换网络，不自带独立 tokenizer。

---

### 4.4 优化器工厂与「公共启动参数」装配

#### 4.4.1 概念说明

`build_training_run` 除了建模型包，还要把一大堆「非模型」的装配项准备好，集中下发给运行时构建器：

- **优化器工厂**：一个「给草稿模块就返回优化器」的可调用对象。注意是「工厂」而不是「优化器实例」——因为 disaggregated 模式下 producer 和 consumer 可能各自需要新建优化器，工厂让它们按需实例化。
- **dataloader worker 数**：来自配置或算法默认值。
- **profiling 选项**：来自 `profiling` 配置段。
- **tracker/logger**：来自 `tracking` 配置段，多后端（wandb/tensorboard/swanlab/mlflow）。
- **公共启动参数包**：`_common_launch_kwargs` 把上面这些连同 batch_size、accumulation_steps、各类 interval、TP/SP 尺寸打包成一个字典，用 `**` 展开传给 `build_offline_runtime`。

这一节的意义在于：这些参数**全部集中在装配层算好**，运行时构建器只负责「接收并使用」，不需要自己再去读配置——这让运行时保持「纯执行」的薄层。

#### 4.4.2 核心流程

```
① 优化器工厂 _optimizer_factory(cfg) → _ConfiguredOptimizerFactory(cfg)
       .configure_total_steps(n)    # 允许运行时回填/校验 total_steps
       .__call__(draft_module)       # → BF16Optimizer(lr, max_grad_norm, warmup_ratio,
                                    #           total_steps, offload_master, ...)

② dataloader worker 数 _dataloader_num_workers(cfg, algorithm)
       = cfg.data.dataloader_num_workers 或 algorithm 默认值

③ profiling 选项 _profiling_options(cfg) → ProfilingOptions(enabled, start_step, ...)

④ logger _configured_logger(cfg)
       report_to == "none" 或 role == "producer" → 纯 print 的 _logger
       否则 → create_tracker_logger(...)（wandb/tensorboard/swanlab/mlflow）

⑤ _common_launch_kwargs(cfg, bundle, algorithm, logger)
       → dict(algorithm, modality, optimizer_factory, batch_size,
              accumulation_steps (= t.accumulation_steps × sp_size 当 usp),
              max_steps/total_steps, save/eval/max_checkpoints, logger,
              strategy_kwargs, tp/sp 尺寸, dataloader_num_workers, profiling_options)
```

#### 4.4.3 源码精读

**① 优化器工厂**：

```python
# specforge/training/assembly.py:246-273
class _ConfiguredOptimizerFactory:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.total_steps = cfg.training.total_steps or cfg.training.max_steps

    def configure_total_steps(self, total_steps: int) -> None:
        if self.total_steps is None:
            self.total_steps = total_steps
        elif self.total_steps != total_steps:
            raise ValueError(
                "optimizer/controller schedule mismatch: "
                f"{self.total_steps} != {total_steps}"
            )

    def __call__(self, draft_module):
        from specforge.optimizer import BF16Optimizer
        if self.total_steps is None:
            raise RuntimeError("optimizer total_steps was not resolved before assembly")
        t = self.cfg.training
        return BF16Optimizer(
            draft_module,
            lr=t.learning_rate,
            max_grad_norm=t.max_grad_norm,
            warmup_ratio=t.warmup_ratio,
            total_steps=self.total_steps,
            offload_master=t.optimizer_cpu_offload,
        )
```

[specforge/training/assembly.py:246-273](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py#L246-L273) 是优化器工厂。三个要点：

1. **`total_steps` 的来源是 `cfg.training.total_steps or cfg.training.max_steps`**——优先 `total_steps`，没有就用 `max_steps`。这呼应 u9-l2 会讲的「两个 horizon 字段」。
2. **`configure_total_steps` 允许下游回填**：如果装配期还不知道 `total_steps`（比如在线 consumer 要等 producer 的 schedule），运行时可以调这个方法回填；若回填值与已有值冲突，fail-fast。这是一个「装配期预留、运行期确定」的两阶段解析口。
3. **`__call__` 真正建优化器**：返回 `BF16Optimizer`，参数全部来自 `training` 段（`learning_rate`/`max_grad_norm`/`warmup_ratio`/`optimizer_cpu_offload`）。

**②/③ dataloader workers 与 profiling**：

```python
# specforge/training/assembly.py:491-508
def _dataloader_num_workers(cfg: Config, algorithm: AlgorithmRegistration) -> int:
    dataloader_num_workers = cfg.data.dataloader_num_workers
    if dataloader_num_workers is None:
        dataloader_num_workers = algorithm.providers.model.default_dataloader_num_workers
    return dataloader_num_workers


def _profiling_options(cfg: Config):
    from specforge.training.profiling import ProfilingOptions
    return ProfilingOptions(
        enabled=cfg.profiling.enabled,
        start_step=cfg.profiling.start_step,
        num_steps=cfg.profiling.num_steps,
        record_shapes=cfg.profiling.record_shapes,
    )
```

[specforge/training/assembly.py:491-508](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py#L491-L508) 体现「配置优先、算法兜底」的常见模式：dataloader worker 数优先用 `data.dataloader_num_workers`（schema 默认 `None`，[schema.py:143](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L143)），为空才取算法声明的 `default_dataloader_num_workers`。profiling 选项则逐字段从 `profiling` 段映射成 `ProfilingOptions`（u9-l3 详解）。

**④ logger / tracker**：

```python
# specforge/training/assembly.py:293-311
def _configured_logger(cfg: Config):
    """Create an external tracker only for a trainer-bearing run."""
    if cfg.tracking.report_to == "none" or cfg.training.role == "producer":
        return _logger
    from types import SimpleNamespace
    from specforge.training.tracking import create_tracker_logger
    options = cfg.tracking.model_dump()
    options["wandb_project"] = options["wandb_project"] or "specforge"
    options["wandb_name"] = options["wandb_name"] or cfg.run_id
    options["swanlab_project"] = options["swanlab_project"] or "specforge"
    options["swanlab_name"] = options["swanlab_name"] or cfg.run_id
    options["mlflow_experiment_name"] = options["mlflow_experiment_name"] or "specforge"
    options["mlflow_run_name"] = options["mlflow_run_name"] or cfg.run_id
    return create_tracker_logger(SimpleNamespace(**options), cfg.output_dir, console_logger=_logger)
```

[specforge/training/assembly.py:293-311](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py#L293-L311) 是 tracker 装配。两个要点：

1. **producer 永远用纯 print 的 `_logger`**（[assembly.py:280-290](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py#L280-L290)）：producer 不训练，不需要外部实验跟踪。`report_to == "none"` 也走这条快车道。
2. **各类后端的默认命名兜底**：项目名默认 `specforge`，run 名默认取 `cfg.run_id`——所以你不在配置里写 wandb_name，它就用 run_id 当实验名。

**⑤ 公共启动参数包**：

```python
# specforge/training/assembly.py:511-546
def _common_launch_kwargs(cfg, bundle, algorithm, *, logger=_logger) -> Dict[str, Any]:
    t = cfg.training
    # USP shards one logical sample over sp_size ranks.  Preserve the legacy
    # optimizer-window semantics: one user accumulation unit represents a complete
    # logical sequence, not one local sequence shard.
    accumulation_steps = t.accumulation_steps
    if t.attention_backend == "usp":
        accumulation_steps *= t.sp_ulysses_size * t.sp_ring_size
    return dict(
        algorithm=algorithm,
        modality=cfg.model.input_modality,
        optimizer_factory=_optimizer_factory(cfg),
        run_id=cfg.run_id,
        output_dir=cfg.output_dir,
        batch_size=t.batch_size,
        accumulation_steps=accumulation_steps,
        max_steps=t.max_steps,
        total_steps=t.total_steps,
        save_interval=t.save_interval,
        eval_interval=t.eval_interval,
        max_checkpoints=t.max_checkpoints,
        logger=logger,
        log_interval=t.log_interval,
        strategy_kwargs=bundle.strategy_kwargs,
        tp_size=t.tp_size,
        sp_ulysses_size=t.sp_ulysses_size,
        sp_ring_size=t.sp_ring_size,
        dataloader_num_workers=_dataloader_num_workers(cfg, algorithm),
        profiling_options=_profiling_options(cfg),
    )
```

[specforge/training/assembly.py:511-546](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py#L511-L546) 把所有「公共」参数收拢成一个字典。最重要的一处是 **USP 下的 `accumulation_steps` 放大**：

\[ \text{accumulation\_steps}^{\text{usp}} = \text{accumulation\_steps} \times \text{sp\_ulysses\_size} \times \text{sp\_ring\_size} = \text{accumulation\_steps} \times \text{sp\_size} \]

注释解释了为什么：当使用 USP（序列并行，u8-l2）时，一条逻辑样本被切分到 `sp_size` 个 rank 上，每个 rank 只看到 \(1/\text{sp\_size}\) 的序列。为了保证「一个用户累积单元 = 一条完整逻辑序列」（而不是一条本地切片），累积步数必须乘以 `sp_size`。这是「序列并行改变有效 batch 语义」在装配层的直接体现。`attention_backend == "usp"` 的判定来自 [schema.py:496](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L496) 的 `Literal` 类型约束。

> 这一处放大也在 disaggregated 装配里重复出现（[disaggregated.py:441-443](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/disaggregated.py#L441-L443) 与 L740 附近）——因为 disaggregated 分支没走 `_common_launch_kwargs`，自己算了一遍。两处逻辑必须保持一致，否则 colocated 与 disaggregated 的累积语义会不一致。

最后，这个字典通过 `**_common_launch_kwargs(...)` 展开（[assembly.py:623-628](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py#L623-L628)）喂给 `build_offline_runtime`，运行时构建器接收这些已算好的参数，不再回头读配置。

#### 4.4.4 代码实践

**实践目标**：理解 USP 下 `accumulation_steps` 的放大公式，并验证 colocated 与 disaggregated 两处口径一致。

**操作步骤**：

1. 打开 [_common_launch_kwargs (assembly.py:511-546)](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py#L511-L546)，找到 `accumulation_steps` 被放大的那两行（约 L522-524）。
2. 假设一份配置：`accumulation_steps=2`、`sp_ulysses_size=2`、`sp_ring_size=2`、`attention_backend=usp`。手算放大后的 `accumulation_steps`。
3. 打开 disaggregated offline 的 consumer 装配 [disaggregated.py:441-443](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/disaggregated.py#L441-L443)，确认它用的是同一个公式。

**需要观察的现象**：

- 放大公式只在 `attention_backend == "usp"` 时触发；`eager`/`sdpa`/`fa` 等后端不放大。
- 两处代码用相同的 `accumulation_steps *= sp_ulysses_size * sp_ring_size` 表达式。

**预期结果**：手算 \(2 \times 2 \times 2 = 8\)，放大后 `accumulation_steps=8`。这意味着在 USP 下，每 8 个本地 micro-batch 的梯度才累积成一次完整逻辑序列的更新。两处口径一致，保证不同部署模式下「一个 optimizer step 对应的逻辑数据量」相同。

> 说明：本实践为「源码阅读 + 手算」型，不需要 GPU。

#### 4.4.5 小练习与答案

**练习 1**：`_ConfiguredOptimizerFactory` 为什么是「工厂」而不是在装配期直接 `BF16Optimizer(...)` 实例化？

> **参考答案**：两个原因。其一，优化器需要「草稿模块」作为入参，而 disaggregated 的 producer 不建草稿模块（它只灌特征），producer 不该被迫建一个用不上的优化器；工厂让它「按需实例化」。其二，`total_steps` 可能在装配期还没定（在线 consumer 要等 producer 的 schedule），工厂 + `configure_total_steps` 提供了「先建工厂、后补 total_steps、再实例化」的两阶段空间。直接实例化会把这两个灵活性都锁死。

**练习 2**：`_configured_logger` 在 producer 角色下返回纯 `_logger`（print）。如果误把 producer 也接上 wandb，会发生什么问题？

> **参考答案**：producer 不训练、不产生 train/eval metrics，接上 wandb 只会创建空实验、占用会话配额，还可能与 consumer 的实验名冲突（两者默认都用 `run_id` 当 run 名）。更重要的是，producer 是「捕获/发布特征」的短生命周期进程，外部 tracker 的初始化开销和清理成本是纯浪费。所以装配层用 `role == "producer"` 把它挡在快车道上。

---

## 5. 综合实践

**任务**：以一份 colocated offline 的 EAGLE3 配置为例，画出从 `build_training_run` 进入、到 `TrainingRun(trainer=trainer)` 返回的完整装配时序，并在图上标注每个装配步骤依赖哪个配置段。

**建议步骤**：

1. 选一份真实的 offline eagle3 示例配置（如 `examples/configs/` 下任意一份不含 `hidden_states_path` 为空、`deployment.mode: local_colocated` 的配置；若仓库里文件名不同，替换成任意 offline eagle3 配置）。
2. 按下面的骨架补全时序图（用文字或画图工具均可），在每一步后面的括号里写出它读的配置段：

   ```
   build_training_run(cfg, algorithm)                         （入口；读 training.strategy）
        │
        ├─ 校验 algorithm.name == strategy                    （training）
        ├─ validate_world_size(...)                            （training: tp_size, sp_*）
        ├─ (online + 非 disaggregated → fail-fast)             （mode, deployment.mode）
        │
        ├─ bundle = build_model_bundle(cfg, algorithm)
        │      ├─ _load_draft                                  （model）
        │      ├─ _load_input_tools → _load_text_tokenizer     （model）
        │      ├─ load_target_config → target_*_size           （model）
        │      ├─ provider.build_training_model → parts        （model + algorithm）
        │      └─ step.bind_runtime → strategy_kwargs          （model + algorithm）
        │
        ├─ _ensure_offline_vocab_mapping(cfg, bundle, ...)     （？自己填）
        │      └─ count_effective_feature_tokens               （？自己填）
        │
        ├─ _configured_logger(cfg)                             （tracking, run_id, output_dir）
        │
        └─ build_offline_runtime(
               draft_model=bundle.model, target_head=bundle.target_head,
               hidden_states_path=..., max_len=..., num_epochs=...,   （data, training）
               **_common_launch_kwargs(cfg, bundle, algorithm, logger=run_logger)
                   └─ optimizer_factory, accumulation_steps(×sp_size if usp),
                      dataloader_num_workers, profiling_options, ...  （training, data, profiling）
           ) → trainer
        │
        └─ return TrainingRun(trainer=trainer)
   ```

3. 回答三个问题：
   - 装配期一共读了哪几个配置段（model/data/training/tracking/profiling/runtime/deployment）？哪一个段在装配期**完全没被读**？（提示：`runtime` 段的大部分字段是给在线 disaggregated 用的。）
   - 为什么 `_ensure_offline_vocab_mapping` 必须在 `build_model_bundle` 之后、`build_offline_runtime` 之前？
   - 如果把这份配置改成 `deployment.mode: disaggregated`，上述时序会从哪一步开始分叉？分到哪个函数？

**参考答案要点**：

- 装配期读了 `model`/`data`/`training`/`tracking`/`profiling`/`deployment`（deployment.mode 决定分发）。在 colocated offline 路径里，`runtime` 段基本未被读——它的 `in_flight_high_watermark`、`producer_lease` 等是给在线 disaggregated producer/consumer 用的（u7-l4）。
- vocab mapping 必须在 bundle 之后，因为它需要 `bundle.draft_vocab_size`/`target_vocab_size`；必须在 `build_offline_runtime` 之前，因为它要把推导出的 `d2t`/`t2d` `copy_` 进 `bundle.draft_model`，而 trainer 会接管这个 draft_model。
- 改成 disaggregated 后，时序在 `cfg.deployment.mode == "disaggregated"` 那一步（assembly.py L579）分叉，进入 `build_disaggregated_run`（disaggregated.py），后续由角色（producer/consumer）各自装配，不再走 `build_offline_runtime`。

这个综合实践把本讲的四个最小模块（`TrainingRun`、`build_training_run` 分发、`ModelBundle`/`build_model_bundle`、优化器与公共参数）串成了一条完整的装配流水线。

---

## 6. 本讲小结

- **`TrainingRun` 统一两种运行**：用一个值对象同时承载 trainer 型（`.fit()`）和 producer 型（`execute()`）运行，`__post_init__` 强制「trainer/execute 二选一」与「回调只挂 trainer 型」两条不变量；`.run()` 是唯一对外入口，producer 走快车道、trainer 走 try/except/finally 完整生命周期且 `on_failure` 后仍 `raise`。
- **`build_training_run` = 守边界 + 校验拓扑 + 部署分发**：开头做防御性 `algorithm.name == strategy` 校验，非 producer 校验 `world_size` 整除 TP/SP，online 强制 disaggregated；随后按「数据模式 × 部署模式」分发到 colocated offline（`build_offline_runtime`）或 disaggregated（`build_disaggregated_run`）。
- **`ModelBundle` 收拢「建模型」的全部产物**：草稿模型、组合训练模型、目标头（冻结 `requires_grad_(False)`）、输入工具、目标维度、捕获层、以及 `step.bind_runtime` 产出的检查点策略 `strategy_kwargs`，让下游运行时只接收一个对象。
- **三个装配步骤的落点**：草稿模型加载（`_load_draft`→`resolve_draft_config`+`build_draft`，依赖 `model`）、tokenizer 加载（`_load_text_tokenizer`，依赖 `model`，从目标模型路径取分词器）、离线 vocab mapping 安装（`_ensure_offline_vocab_mapping`→`count_effective_feature_tokens`，依赖 `data`+`model`，排在 bundle 之后）。
- **非模型装配项集中下发**：优化器工厂（`_ConfiguredOptimizerFactory`，`total_steps` 可运行期回填）、dataloader worker 数（配置优先、算法兜底）、profiling、tracker（producer 用纯 print）由 `_common_launch_kwargs` 收拢成字典，`**` 展开喂给运行时；USP 下 `accumulation_steps` 乘以 `sp_size` 以保持「一个累积单元 = 一条完整逻辑序列」。
- **装配与运行分离**：`build_training_run` 只「建好对象、接好线」返回 `TrainingRun`，真正启动是组合根/CLI 调 `.run()`；模型初始化（`model_loading.py`，仅权重热启动）与训练恢复（`resume_from`，u9-l1）严格分离。

---

## 7. 下一步学习建议

- **往下看训练循环**：本讲到「把 trainer 交给 `build_offline_runtime`」就停了。接下来读 u6-l3（Trainer 与 TrainerController），看 `Trainer.fit()` 如何进入拓扑拥有的流上下文、跑 epoch 循环、在 optimizer 边界自增 `global_step`。
- **看策略差异**：本讲的 `provider.build_training_model` / `step.bind_runtime` 是算法无关的装配钩子。各算法（EAGLE3/DFlash/Domino/DSpark）如何实现 `forward_loss`、`required_features`、`checkpoint_state_filter`，见 u6-l2（训练策略 DraftTrainStrategy）。
- **看 FSDP 与累积**：本讲提到 `accumulation_steps` 和「optimizer 边界」，但没讲 backward。u6-l4（FSDP 后端与梯度累积）会展开 `no_sync`、分布式梯度范数、`optimizer_stepped` 单一边界信号。
- **看损失算子**：`strategy_kwargs` 最终喂给策略，策略再调损失。u6-l5（损失与核心算子）讲 `LogSoftmaxLoss` 与 `compact_teacher` 分块投影。
- **回看上游**：若你对 `build_model_bundle` 调用的 `providers.model`/`step` 端口还不熟，回到 u4-l3（算法 providers 与扩展端口）巩固；草稿架构那条独立轴在 u4-l4。

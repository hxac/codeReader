# FSDP 后端与梯度累积

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 `FSDPTrainingBackend` 在训练一步里扮演什么角色：谁负责包裹模型、谁负责反向、谁负责优化器步进。
- 解释「梯度累积（gradient accumulation）」为什么要把 loss 除以 `accumulation_steps`，以及 FSDP 的 `no_sync()` 在其中起什么作用。
- 描述 `TrainerCore.train_step` 如何用 `_micro % accumulation_steps == 0` 产生「单一权威边界信号」，并把边界信息传给后端的 `backward`。
- 推断「训练流自然结束却停在 `no_sync` 内」会发生什么，以及 SpecForge 为何宁可报错也不做半个 optimizer step。

本讲只聚焦训练执行期里「包裹 / 反向 / 步进 / 累积边界」这一环，承接 u6-l3 的 `Trainer` → `TrainerController` → `TrainerCore` 三层控制流。

## 2. 前置知识

本讲默认你已经掌握以下内容（在 u6-l3 已建立）：

- **micro_step 与 global_step 的区别**：`micro_step` 记录前向/反向次数，`global_step` 只在「梯度累积边界」处自增，是检查点、ack、评测等「边界动作」真正的计数单位。
- **单一权威边界信号 `optimizer_stepped`**：由 `TrainerCore` 在反向前唯一计算一次，下游所有组件只读它、不二次判定。
- **`DraftTrainStrategy` 插件**：`TrainerCore.train_step` 是无分支的，算法差异收敛进策略的 `forward_loss`，本讲不再讨论算 loss 的细节。
- **草稿是唯一可训练模块**：装配阶段把「草稿模型 + 冻结目标头」打包成复合 `ModelBundle`，但优化器只指向内部的草稿子模块。

如果你还不熟悉分布式训练，需要先理解三个最基础的概念：

- **数据并行（DP/DDP）**：每张卡持有完整的模型副本，各自处理不同 batch，反向时用一次 all-reduce 把各卡梯度求和，再各自走相同的优化器步。
- **FSDP（Fully Sharded Data Parallel）**：把模型参数、梯度、优化器状态**切片（shard）**到各卡上，前向/反向时按需 all-gather 拼回完整参数、reduce-scatter 把梯度重新切片。相比 DDP 更省显存，是 SpecForge 的默认后端。
- **梯度累积**：显存放不下「一个完整 optimizer step 所需的大 batch」时，把一个大 batch 拆成 N 个 micro-batch 依次前向反向，把梯度**累加**起来，到第 N 个再统一做一次 optimizer step。这样 N 个 micro-batch 在数学上等价于 1 个 N 倍大的 batch。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `specforge/training/backend.py` | `TrainingBackend` 抽象基类与唯一实现 `FSDPTrainingBackend`，负责模型包裹、反向、optimizer step、状态字典。本讲主角。 |
| `specforge/training/controller.py` | `TrainerCore.train_step` 在这里——loss 除以 `accumulation_steps`、计算边界、调用 `backend.backward/step`。 |
| `specforge/optimizer.py` | `BF16Optimizer` 的 `step` 返回梯度范数，`configure_grad_norm_reduction` 接线跨卡梯度范数归约。 |
| `specforge/training/trainer.py` | `Trainer` 在装配期创建 `FSDPTrainingBackend` 并调用 `prepare_model`，把包裹后的模型交给策略。 |
| `specforge/training/assembly.py` | USP（序列并行）下把 `accumulation_steps` 乘以 `sp_size`，保证「一个累积单元 = 一条完整逻辑序列」。 |
| `specforge/training/DESIGN.md` | 训练面设计文档，明确各组件职责与「自然结束必须在 optimizer 边界」的约束。 |

## 4. 核心概念与源码讲解

### 4.1 FSDP 包裹

#### 4.1.1 概念说明

「后端（backend）」是训练一步里唯一接触 PyTorch 分布式原语的地方。SpecForge 把「反向」「optimizer step」「模型包裹」「状态存取」四件事抽象成 `TrainingBackend` 接口，目前只有一个实现 `FSDPTrainingBackend`（文件开头注释写明 `FSDP-only for now`）。

`FSDPTrainingBackend` 的核心职责是 `prepare_model`：它决定用 **FSDP** 还是 **DDP** 把模型包起来，并在包裹**之后**用传入的 `optimizer_factory` 创建优化器。这里有两个容易被忽略的关键设计：

1. **优化器只指向草稿子模块**。复合 `ModelBundle` 同时挂着草稿模型和冻结目标头，但调用方传入 `optimizer_target=model.draft_model`，优化器只管理草稿参数——目标头仅在前向算 loss 时用，不参与训练。
2. **策略必须经过包裹后的模型跑前向**。如果策略绕过 FSDP 包裹直接调用裸草稿模型，那么在多卡时 FSDP 根本不在前向/反向路径上，分片参数不会被 all-gather，运行会出错。因此 `Trainer` 在 `trainer.py` 里特意注释了这一点。

`prepare_model` 在 FSDP 与 DDP 之间的选择由 `sharding_strategy` 决定：默认 `SHARD_GRAD_OP`（参数分片、梯度分片），走 FSDP；当配置为 `NO_SHARD`（参数完全复制）时走 DDP——因为 PyTorch 已经废弃了 FSDP 的 `NO_SHARD` 模式，官方建议用 DDP 替代。

#### 4.1.2 核心流程

```text
prepare_model(model, optimizer_target=draft_model, wrap=True)
  │
  ├─ 若 wrap=False：不包裹，直接记为 module（用于单卡/调试）
  │
  ├─ wrap=True：
  │    ├─ _frozen_target_modules(model) → 找出冻结的 lm_head/embed_tokens
  │    │    （DFlash 家族带着它们只为 loss 内推理，不应被分片）
  │    │
  │    ├─ 读 optimizer_target._no_split_modules → 取出 transformer 块类名
  │    │    （DFlash 家族有；EAGLE 没有 → 单根 FSDP 单元）
  │    │
  │    ├─ sharding_strategy == "NO_SHARD"?
  │    │    ├─ 是 → DDP(model, ...)         （_wrapper_kind = "ddp"）
  │    │    └─ 否 → FSDP(model, use_orig_params=True,
  │    │                  mixed_precision=bf16,
  │    │                  auto_wrap_policy=transformer 块,
  │    │                  ignored_modules=冻结表)   （_wrapper_kind = "fsdp"）
  │    │
  │    └─ 记录 module / _wrapped / auto_wrap_block_classes / ignored_frozen_modules
  │
  └─ 若给了 optimizer_factory：
       └─ optimizer = factory(optimizer_target or module)
       └─ _configure_optimizer_grad_norm()  # 给优化器接线跨卡梯度范数归约
```

#### 4.1.3 源码精读

`TrainingBackend` 抽象基类只定义五个抽象方法，是后端的契约：

[specforge/training/backend.py:126-142](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/backend.py#L126-L142) — 这五个方法（`prepare_model` / `backward` / `step` / `state_dict` / `load_state_dict`）划定了后端的边界：凡是「跟 PyTorch 分布式机制耦合」的事都收进这里，`TrainerCore` 只通过这五个方法接触底层。

包裹的分流逻辑在 `prepare_model` 中：

[specforge/training/backend.py:227-247](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/backend.py#L227-L247) — 当 `sharding_strategy == "NO_SHARD"` 时，用 DDP 把模型包起来（`broadcast_buffers=False`、`gradient_as_bucket_view=True`），并把 `_wrapper_kind` 标记为 `"ddp"`。这是 SpecForge「复现旧的全复制 recipe」的路径，适合草稿模型很小、不值得分片的情况。

[specforge/training/backend.py:248-276](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/backend.py#L248-L276) — 否则走 FSDP：`use_orig_params=True`（保留原始参数名，方便状态字典与导出）、bf16 混合精度（`param_dtype` 用 bf16、`buffer_dtype` 用 fp32）；若检测到 transformer 块类，就加 `transformer_auto_wrap_policy`（按块包裹，让 all-gather/reduce-scatter 能与解码器计算重叠）、`forward_prefetch`、`backward_prefetch=BACKWARD_PRE`、`limit_all_gathers`。冻结表通过 `ignored_modules` 排除出分片。

冻结目标表为什么不能分片？看注释：

[specforge/training/backend.py:172-191](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/backend.py#L172-L191) — DFlash 家族带着 `lm_head` / `embed_tokens` 仅为 loss 内部推理用，且它们全部 `requires_grad=False`。如果把这种「只为推理、不可训练」的表也分片，FSDP 会在每个 optimizer 窗口前 all-gather 它们，却不省任何优化器显存——这是亏本买卖，所以 `ignored_frozen_modules` 让它们保持复制态。

包裹完成后立刻创建优化器并接线梯度范数归约：

[specforge/training/backend.py:283-286](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/backend.py#L283-L286) — 优化器用 `optimizer_target`（草稿子模块）而非整个复合 module 构建。这保证优化器的 fp32 master 副本只克隆草稿参数，目标头不会被纳入优化器。

`Trainer` 在装配期调用 `prepare_model` 的位置：

[specforge/training/trainer.py:421-425](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/trainer.py#L421-L425) — 先建 `FSDPTrainingBackend`，再 `prepare_model(model, optimizer_target=model.draft_model)`，拿到**包裹后**的 `wrapped` 模型交给策略。注释点明：策略必须跑在 `wrapped` 上，否则 FSDP 在多卡时被绕过。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**（无需 GPU）。

1. **实践目标**：确认「优化器只指向草稿、包裹后才建优化器」这条装配顺序。
2. **操作步骤**：
   - 打开 `specforge/training/trainer.py` 第 421–433 行。
   - 画出三步顺序：`backend = FSDPTrainingBackend(...)` → `wrapped = backend.prepare_model(model, optimizer_target=model.draft_model)` → `strategy = make_step_strategy(wrapped, ...)`。
   - 打开 `specforge/training/backend.py` 第 283–286 行，确认 `optimizer = self._optimizer_factory(target)` 里 `target` 默认取 `optimizer_target`。
3. **需要观察的现象**：策略拿到的 `wrapped` 是 FSDP/DDP 包裹后的对象，而优化器的参数空间只覆盖草稿。
4. **预期结果**：你能用一句话回答——「为什么不能先建优化器再包裹模型？」（答：FSDP 包裹会重写参数对象，先建优化器会让它持有已被废弃的参数引用；且包裹顺序保证了多卡下 FSDP 真正进入前向/反向路径。）

#### 4.1.5 小练习与答案

**练习 1**：假设你想临时把一个 FSDP recipe 改成「全参数复制、不分片」以排查分片相关的 bug，最少改动是什么？

**答案**：设置环境变量 `FSDP_SHARDING=NO_SHARD`（见 [backend.py:71-74](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/backend.py#L71-L74) 的 env override）。这会让 `prepare_model` 走 DDP 分支，行为接近 DDP，无需改 YAML。注意此时 `optimizer_state_is_replicated` 会变为 `True`。

**练习 2**：为什么 EAGLE 草稿模型通常只有一个「单根 FSDP 单元」，而 DFlash 家族会按 transformer 块包裹？

**答案**：EAGLE 草稿模型通常只有一层，不暴露 `_no_split_modules` 块类，故 `block_classes` 为空，不加 `auto_wrap_policy`，整个模型作为一个 FSDP 根单元包裹；DFlash 家族是有多层 transformer 的草稿，通过 `_noSplit_modules` 暴露块类，按块包裹能让 all-gather/reduce-scatter 与解码器计算重叠，省内存（见 [backend.py:265-274](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/backend.py#L265-L274)）。

---

### 4.2 backward 与 optimizer step

#### 4.2.1 概念说明

`FSDPTrainingBackend` 把反向和步进拆成两个独立方法：

- `backward(loss, is_boundary)`：对一个 micro-batch 的 loss 跑反向，根据是否处在累积边界决定要不要立刻触发梯度归约。
- `step()`：跑优化器步进，返回**全局梯度范数**（用于日志和梯度裁剪）。

值得注意的是「梯度范数」不是后端自己算的，而是**优化器**算的。SpecForge 的 `BF16Optimizer`（AdamW + bf16 master 副本 + 余弦预热调度）在 `step()` 里先算 `total_norm_sq`，再跨 FSDP 进程组 all-reduce 求和，得到全局梯度范数与裁剪系数。后端只负责在 `prepare_model` 末尾把「用哪个进程组、是否启用归约」接线给优化器。

为什么梯度范数要跨卡归约？因为 FSDP 下每张卡只持有参数的一片梯度（reduce-scatter 之后），单卡的局部范数远小于真实全局范数。要做正确的梯度裁剪（`max_grad_norm`），必须先把各卡梯度范数的平方求和再开方。

#### 4.2.2 核心流程

```text
TrainerCore.train_step(batch):
  loss = out.loss / accumulation_steps          # 缩放（见 4.3）
  backend.backward(loss, is_boundary=stepped)
      ├─ is_boundary 或 未包裹 → loss.backward()       # 触发归约
      └─ 非边界且已包裹   → with module.no_sync():
                              loss.backward()          # 延迟归约
  grad_norm = backend.step() if stepped else None
      └─ optimizer.step():
           ├─ _grad_norm_and_clip_coefficient()
           │     ├─ 各参数 p.grad 平方求和 → total_norm_sq
           │     └─ _reduce_grad_norm → all-reduce(SUM) 跨组 → total_norm, clip_coef
           ├─ 用 clip_coef 缩放 fp32 master 梯度
           ├─ AdamW 步进、zero_grad、scheduler.step
           └─ 把 master 拷回 bf16 参数，返回 last_grad_norm
```

梯度范数与裁剪系数的数学关系：

\[ \text{total\_norm} = \sqrt{\sum_{p} \sum \text{grad}_p^2} \quad\text{（先跨卡 SUM 平方和，再开方）}\]

\[ \text{clip\_coef} = \min\!\left(1,\ \frac{\text{max\_grad\_norm}}{\text{total\_norm} + 10^{-6}}\right)\]

当 `total_norm ≤ max_grad_norm` 时 `clip_coef = 1`（不裁剪）；超过则按比例缩小所有梯度。

#### 4.2.3 源码精读

后端的 `backward` 只有三行核心逻辑：

[specforge/training/backend.py:304-314](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/backend.py#L304-L314) — 边界步或未包裹时直接 `loss.backward()`（会触发 FSDP reduce-scatter / DDP all-reduce）；非边界且已包裹时进入 `module.no_sync()` 上下文再 backward，从而**推迟**这一次的梯度归约。注释明确：「每个累积窗口只有一次梯度 collective」。

`step` 方法故意做得很薄：

[specforge/training/backend.py:316-322](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/backend.py#L316-L322) — 它只检查优化器是否已设置，然后调用 `self.optimizer.step()` 并原样返回。真正的「算范数 + 裁剪 + 步进」全在 `BF16Optimizer.step` 里。

后端在 `prepare_model` 末尾给优化器接线梯度范数归约：

[specforge/training/backend.py:293-302](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/backend.py#L293-L302) — 若优化器有 `configure_grad_norm_reduction` 方法，就把 FSDP 进程组传进去，并在「未包裹」或「`NO_SHARD`」时把归约关掉（因为这两种情况下参数是复制的，每卡已有完整梯度，不需要再跨卡求和）。`enabled = self._wrapped and sharding_strategy != "NO_SHARD"`。

优化器侧实现跨卡梯度范数：

[specforge/optimizer.py:53-61](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/optimizer.py#L53-L61) — `configure_grad_norm_reduction` 记下进程组与是否启用。

[specforge/optimizer.py:63-82](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/optimizer.py#L63-L82) — `_reduce_grad_norm` 把 `total_norm_sq` 跨组 all-reduce（SUM）后开方，再算裁剪系数 `clip_coef = clamp(max_grad_norm / (total_norm + 1e-6), max=1.0)`。

[specforge/optimizer.py:129-157](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/optimizer.py#L129-L157) — `BF16Optimizer.step` 先算范数与裁剪系数，把 fp32 master 梯度按系数缩放，再做 AdamW 步进、`zero_grad`、调度器步进，最后把 master 拷回 bf16 参数并返回 `last_grad_norm`。这就是 `backend.step()` 最终返回的那个标量。

`TrainerCore` 如何调用这两个方法：

[specforge/training/controller.py:329-331](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/controller.py#L329-L331) — `self.backend.backward(loss, is_boundary=stepped)` 之后，只有 `stepped` 为真才调 `backend.step()` 拿到 `grad_norm`；非边界步根本不步进，`grad_norm` 为 `None`。

#### 4.2.4 代码实践

1. **实践目标**：理解「梯度范数归约何时启用、何时不启用」。
2. **操作步骤**：
   - 阅读 [backend.py:167-170](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/backend.py#L167-L170) 的 `optimizer_state_is_replicated` 属性。
   - 阅读 [optimizer.py:84-96](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/optimizer.py#L84-L96) 的 `_grad_norm_and_clip_coefficient`。
3. **需要观察的现象**：`optimizer_state_is_replicated` 只在 `_wrapper_kind == "ddp"` 时为真，恰好对应 `_configure_optimizer_grad_norm` 里 `enabled=False` 的情况。
4. **预期结果**：你能解释——「为什么 DDP（NO_SHARD）下要关闭跨卡梯度范数归约？」（答：DDP 在 backward 时已经 all-reduce 过完整梯度，每卡持有相同的完整 `.grad`，局部范数即全局范数，再 all-reduce 一次会把范数放大成 `world_size` 倍。）

#### 4.2.5 小练习与答案

**练习 1**：`backend.step()` 返回的 `grad_norm` 在 `TrainerCore.train_step` 里会被赋给 `StepResult.grad_norm`。它在什么情况下是 `None`？

**答案**：当 `stepped` 为假（即非累积边界的 micro-batch）时，`backend.step()` 根本不会被调用，`grad_norm` 为 `None`（见 [controller.py:330](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/controller.py#L330)）。梯度范数只在真正的 optimizer 边界才有意义。

**练习 2**：如果把 `max_grad_norm` 设得非常大（比如 1e9），梯度裁剪实际还会生效吗？

**答案**：基本不生效。由 `clip_coef = clamp(max_grad_norm / (total_norm + 1e-6), max=1.0)`，当 `max_grad_norm` 远大于 `total_norm` 时 `clip_coef` 被 clamp 到 1.0，梯度不被缩放（见 [optimizer.py:80-82](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/optimizer.py#L80-L82)）。

---

### 4.3 no_sync 与梯度累积边界

#### 4.3.1 概念说明

这是本讲最关键的一节，也是代码实践任务的落点。

**梯度累积的数学**：设 `accumulation_steps = N`。我们希望用 N 个 micro-batch 模拟「一个 N 倍大的 batch」。每个 micro-batch `i` 的损失 `L_i` 产生梯度 `g_i = ∂L_i/∂θ`。标准的「大 batch 平均」想得到：

\[
\bar{g} = \frac{1}{N}\sum_{i=1}^{N} g_i
\]

由于 `loss.backward()` 是**把梯度累加**进 `p.grad`（不是覆盖），如果直接对每个 `L_i` 调 backward，N 次累加会得到 `\sum g_i`，比目标大了 N 倍。解决办法是在 backward 前把 loss 缩放：

\[
\text{loss}_i = \frac{L_i}{N}, \qquad \text{backward}(\text{loss}_i) \text{ 贡献 } \frac{1}{N}g_i
\]

N 次累加正好得到 `\frac{1}{N}\sum g_i = \bar{g}`。这就是 `TrainerCore.train_step` 里 `loss = out.loss / self.accumulation_steps` 的作用。

**`no_sync` 的作用**：在 FSDP/DDP 下，普通 `backward()` 会在反向结束时自动触发一次梯度 collective（FSDP 的 reduce-scatter、DDP 的 all-reduce），把各卡梯度同步求和。如果每个 micro-batch 都同步一次，N 个 micro-batch 就要 N 次同步——但梯度累积只需要在**最后一个** micro-batch 同步一次即可，因为 reduce 是线性的（先在本地把前 N-1 个梯度加好，最后一次同步就等价于对全部 N 个梯度做同步求和）。

`no_sync()` 上下文就是用来「关闭这一次反向的自动同步」的：在它里面 backward，梯度只在本地 `p.grad` 里累加，不触发跨卡通信；离开累积窗口、在边界步退出 `no_sync` 再 backward，这一次的反向才会触发同步。于是「每 N 个 micro-batch 只有一次梯度 collective」，通信量降为 1/N。

#### 4.3.2 核心流程

```text
设 accumulation_steps = N，TrainerCore 内部计数器 _micro 从 0 开始。

第 i 个 micro-batch (i = 1..N):
  out = strategy.forward_loss(batch)
  loss = out.loss / N                      # 关键缩放
  _micro += 1
  stepped = (_micro % N == 0)              # 边界：第 N 个

  if i < N (非边界):
      with module.no_sync():
          loss.backward()                  # 本地累加，不通信
      → 不调 step，grad_norm = None
  else: # i == N (边界):
      loss.backward()                      # 触发 reduce-scatter / all-reduce
      grad_norm = optimizer.step()         # 用累加后的总梯度走一步

  return StepResult(optimizer_stepped=stepped, ...)
```

`TrainerController._fit` 收到 `optimizer_stepped=False` 就 `continue`（不增 `global_step`、不 ack、不存点）；收到 `True` 才把 `global_step += 1` 并触发所有「边界动作」。

**收尾约束**：训练流自然结束时（所有 epoch 跑完或在线队列耗尽），如果最后那次 backward 落在 `no_sync` 内（即 `_micro % N != 0`，存在「凑不齐一个完整窗口」的尾巴 micro-batch），`_fit` 直接抛 `RuntimeError`，绝不悄悄做半个 optimizer step，也不谎报检查点成功。

#### 4.3.3 源码精读

边界计算与缩放都在 `TrainerCore.train_step` 里：

[specforge/training/controller.py:320-331](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/controller.py#L320-L331) — 注意顺序：先把 `loss` 除以 `accumulation_steps`（L324），`_micro += 1`（L325），**再**用 `self._micro % self.accumulation_steps == 0` 算出 `stepped`（L328），最后把这个布尔值传给 `backend.backward(loss, is_boundary=stepped)`（L329）。注释点明「边界在 backward 之前就已知道，后端据此决定非边界步是否走 no_sync」。这是 `optimizer_stepped` 成为「单一权威边界信号」的根源——它在一处计算、向下游广播。

`accumulation_remainder` 属性用于收尾判断：

[specforge/training/controller.py:315-318](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/controller.py#L315-L318) — 返回 `_micro % accumulation_steps`，即「凑不齐一个窗口的尾巴 micro-batch 数」。

收尾的硬约束在 `_fit` 末尾：

[specforge/training/controller.py:634-641](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/controller.py#L634-L641) — 若 `remainder != 0`，抛 `RuntimeError`，说明「训练流以不完整的梯度累积结束：最后一次 optimizer step 之后又收到了 `remainder` 个 micro-batch，但没有提交任何部分 optimizer step 或 durable ack」。这与 `DESIGN.md` 的设计一致。

设计文档原文确认这一约束：

[specforge/training/DESIGN.md:68-72](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/DESIGN.md#L68-L72) — 「自然结束只允许发生在 optimizer 边界。如果最后一次 backward 落在 FSDP `no_sync` 内，`fit` 会失败，而不是去步进未归约的梯度或谎报检查点成功。」

`no_sync` 实际生效的地方就是 4.2.3 引用过的 `backward`：

[specforge/training/backend.py:304-314](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/backend.py#L304-L314) — 非边界步进入 `with self.module.no_sync(): loss.backward()`，梯度在本地累加，跨卡归约被推迟到窗口末尾。

USP（序列并行）下的特殊处理：一条逻辑序列被 `sp_size` 个 rank 切片处理，为了让「一个累积单元仍代表一条完整逻辑序列」，装配阶段把用户配置的 `accumulation_steps` 乘以 `sp_size`：

[specforge/training/assembly.py:519-524](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py#L519-L524) — 当 `attention_backend == "usp"` 时 `accumulation_steps *= sp_ulysses_size * sp_ring_size`。这样进入 `TrainerCore` 的累积步数已经把序列并行的维度折算进去，对 `TrainerCore` 与 `FSDPTrainingBackend` 而言是无感的。

#### 4.3.4 代码实践

本节的实践任务正是规格里指定的那个。这是一个**源码阅读 + 推理型实践**（实际跑训练需要多卡 GPU，故标注「待本地验证」的部分仅限运行侧）。

1. **实践目标**：解释当 `accumulation_steps > 1` 时，非边界 micro-batch 为何用 `no_sync`，并说明若最后一次 backward 落在 `no_sync` 内 `fit` 会如何处理。

2. **操作步骤**：
   - 打开 [controller.py:320-331](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/controller.py#L320-L331)，假设 `accumulation_steps = 2`，在纸上跟踪 `_micro` 从 1 到 2 的两次 `train_step` 调用，标注每次的 `stepped`、`is_boundary`、是否进入 `no_sync`、是否调用 `step()`。
   - 打开 [backend.py:304-314](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/backend.py#L304-L314)，确认非边界步走 `no_sync` 分支。
   - 打开 [controller.py:634-641](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/controller.py#L634-L641)，设想一个数据集恰好提供 3 个 micro-batch、`accumulation_steps = 2` 的场景，预测 `_fit` 末尾的行为。

3. **需要观察的现象（推理）**：
   - 第 1 个 micro-batch：`_micro=1`，`stepped = 1%2==0 → False`，进入 `no_sync` 反向，梯度本地累加，不通信，不步进。
   - 第 2 个 micro-batch：`_micro=2`，`stepped = 2%2==0 → True`，普通反向触发一次 reduce-scatter，调用 `step()` 走优化器，`global_step += 1`。
   - （上述前两步组成一个完整的 optimizer step。）
   - 若流到此结束且还多出 1 个 micro-batch（即第 3 个）：`_micro=3`，`remainder = 3%2 = 1`，`_fit` 抛 `RuntimeError`。

4. **预期结果**：
   - **为何用 `no_sync`**：梯度累积只需在窗口末尾同步一次梯度；前 `N-1` 步用 `no_sync` 把梯度攒在本地，可把跨卡通信次数从 N 次降到 1 次，同时配合 `loss/N` 的缩放保证累加梯度等价于大 batch 的平均梯度。
   - **若最后一次 backward 落在 `no_sync` 内**：`fit` 不会用未归约的本地梯度去做半个 optimizer step，也不会把一个不完整的步当成检查点写成功，而是直接抛 `RuntimeError`（`accumulation_remainder != 0`）。在线 queue 模式的 loader 会主动把凑不齐一个窗口的尾部短 batch 终态消化掉（见 DESIGN.md L70-72），从而避免这种报错；固定离线 refs 则保留正常的 `drop_last` 语义。
   - **运行侧验证（待本地验证）**：在一个真实多卡 run 里把 `accumulation_steps` 设为 2、数据集样本数设成奇数倍 batch，离线模式下应能在日志看到该 `RuntimeError`；这一步需本地 GPU 环境确认。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `loss = out.loss / self.accumulation_steps` 这一行删掉（但保留 `no_sync` 逻辑），训练结果会怎样？

**答案**：梯度累积后总梯度会变成应有值的 `accumulation_steps` 倍（少了 1/N 缩放），等价于把学习率放大了 N 倍，训练会发散或大幅偏离。`no_sync` 只决定「何时通信」，不影响「梯度幅值」；幅值由 loss 缩放决定。两者必须配合。

**练习 2**：`no_sync` 在 FSDP 和 DDP 下的物理含义是否相同？

**答案**：语义相同（关闭本次反向的自动梯度同步，让梯度在本地 `.grad` 累加），底层 collective 不同：FSDP 下推迟的是 reduce-scatter（梯度分片同步），DDP 下推迟的是 all-reduce（完整梯度同步）。两者都依赖「reduce 是线性 SUM」这一性质，所以「本地先累加、边界再同步一次」在数学上等价于「每次都同步再累加」。见 [backend.py:304-314](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/backend.py#L304-L314)。

**练习 3**：USP（序列并行）下，用户在 YAML 里写 `accumulation_steps: 2`、`sp_ulysses_size: 2`、`sp_ring_size: 1`，`TrainerCore` 实际看到的 `accumulation_steps` 是多少？为什么？

**答案**：是 `2 * 2 * 1 = 4`。因为 USP 把一条逻辑序列切片到 `sp_size = 2` 个 rank 上，2 个 micro-batch 只是本地视角；为了让「一个累积单元 = 一条完整逻辑序列」，装配阶段乘以 `sp_size` 折算（见 [assembly.py:519-524](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py#L519-L524)）。`TrainerCore` 与后端对此无感。

## 5. 综合实践

把本讲三节串起来，完成一个「一步训练」的完整追踪任务。

**场景**：`accumulation_steps = 2`，FSDP 后端（`SHARD_GRAD_OP`），2 张卡。

**任务**：

1. 画出两次 `train_step` 调用的时序，标出以下信息在各步的取值：`_micro`、`loss = out.loss / N` 中的 N、`stepped`、`is_boundary`、是否 `no_sync`、是否 `optimizer.step()`、`grad_norm`、`global_step`。
2. 在时序图上用箭头标出「跨卡梯度 collective」发生在哪一次 backward，并解释为什么前一次 backward 不发生 collective。
3. 指出 `optimizer.step()` 内部，`_reduce_grad_norm` 是跨「哪两个进程组中的一个」做 all-reduce（提示：`configure_grad_norm_reduction` 传入的是 `fsdp_process_group`，见 [backend.py:293-302](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/backend.py#L293-L302)），并说明为什么用的是这个组而不是 DP 组。
4. 设想流在第 3 个 micro-batch 后结束：写出 `_fit` 抛出的 `RuntimeError` 文案中的关键数字（`remainder` 与 `accumulation_steps`），并说明在线 queue 模式的 loader 是如何避免这个错误的。

**参考答案要点**：

| 步骤 | `_micro` | `stepped` | `no_sync`? | `step()`? | `grad_norm` | `global_step` |
|---|---|---|---|---|---|---|
| micro-batch 1 | 1 | False | 是 | 否 | None | 不变 |
| micro-batch 2 | 2 | True | 否（触发 reduce-scatter）| 是 | 全局范数 | +1 |

- collective 只在第 2 次 backward 发生；第 1 次因 `no_sync` 仅本地累加。
- 梯度范数跨 `fsdp_process_group` all-reduce，因为 FSDP 下每卡只持有梯度分片，必须跨分片组求和才是真实全局范数；DP 组负责的是别的并行维度（数据并行划分），不是 FSDP 分片维度。
- 第 3 个 micro-batch 后结束：`remainder = 3 % 2 = 1`，报错文案为 `received 1 of 2 micro-batches after the last optimizer step`；在线 queue 模式的 loader 会在终态「消化掉」凑不齐一个窗口的尾部短 batch（`drop_last` 式清理），从而不会把它交给 `train_step`，也就不会产生未归约的尾巴。

## 6. 本讲小结

- `FSDPTrainingBackend` 是 SpecForge 目前唯一的训练后端，把「模型包裹 / 反向 / optimizer step / 状态字典」四件事收口，让 `TrainerCore` 保持无分支。
- `prepare_model` 在 `NO_SHARD` 时走 DDP、其余走 FSDP（`use_orig_params=True` + bf16 混合精度 + 可选 transformer 块自动包裹），优化器在包裹**之后**只对草稿子模块创建。
- 梯度累积靠两件事配合：`loss / accumulation_steps` 把每个 micro-batch 的梯度缩到 1/N；`no_sync()` 让前 N-1 步只在本地累加梯度，仅在边界步触发一次跨卡 reduce，把通信降到 1/N。
- `optimizer_stepped = (_micro % accumulation_steps == 0)` 在 backward 之前算出，是全链路唯一的边界信号，驱动 ack、检查点、评测与日志。
- 梯度范数由 `BF16Optimizer` 在 `step` 里跨 FSDP 进程组 all-reduce 平方和得到，DDP/未包裹时关闭归约；`backend.step()` 只是薄封装。
- 训练流自然结束必须落在 optimizer 边界，否则 `_fit` 抛 `RuntimeError`——宁可失败也不做半个 step 或谎报检查点成功；在线 queue loader 会主动清理凑不齐窗口的尾部 batch。

## 7. 下一步学习建议

- 下一讲 **u6-l5 损失与核心算子** 会下沉到 `specforge/core/`，看 `LogSoftmaxLoss`、LK loss 与 compact teacher 如何在 `strategy.forward_loss` 内部产生 `out.loss`——也就是本讲 `loss = out.loss / accumulation_steps` 里那个 `out.loss` 的来源。
- 如果你更关心分布式拓扑，可跳到 **u8-l1 分布式初始化与设备网格**，看 `init_distributed` 如何构建 `ParallelConfig.from_distributed` 读取的那些进程组（`fsdp_process_group`、`dp_group`、`sp_*_group`）。
- 若想了解「检查点如何存取后端状态」，可先读 **u9-l1 检查点与恢复**，它直接消费本讲的 `state_dict/load_state_dict`（`FULL_STATE_DICT` 聚合到 rank0、优化器状态按 DDP/FSDP 分别处理）。
- 建议顺带重读 [`specforge/training/DESIGN.md`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/DESIGN.md) 的「Internal mechanics」流程图，把本讲的 `backward/step` 与 `TrainerCore`/`TrainerController` 的协作放回整张训练面地图里。

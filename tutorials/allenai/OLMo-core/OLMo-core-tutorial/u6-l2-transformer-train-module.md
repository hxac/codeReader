# TransformerTrainModule：前向、反向与并行设置的汇聚点

## 1. 本讲目标

上一讲（u6-l1）我们看清了 `Trainer.fit()` 的骨架：它只做编排，把「真正计算」的活儿全部委托给一个叫 `train_module` 的对象。本讲就打开这个黑盒。学完本讲，你应该能够：

1. 说出 `TrainModule` 抽象基类定义了哪些方法，`Trainer` 在训练循环的哪个位置调用它们——即二者之间的「合同条款」。
2. 解释为什么 DP/TP/PP/CP/EP 五套并行配置全部挂在 train module 的配置上，而不是挂在模型配置 `TransformerConfig` 上。
3. 逐行跟踪一次 `train_batch → optim_step → zero_grads` 的代码路径，说清梯度累积、损失归一化、梯度裁剪、学习率调度各自发生在哪一步。
4. 不启动完整 `Trainer`，用随机数据手动驱动一个 `TransformerTrainModule` 走完一步训练，并检查 loss 与优化器状态。

## 2. 前置知识

- **Trainer 与 TrainModule 的分工**（u6-l1）：`Trainer` 管「何时」——step 计数、Duration、回调钩子、指标收集与归约；`train_module` 管「怎么算」——前向、反向、优化器步进。回顾调用点：`Trainer` 在每个 epoch 开头调 `zero_grads()`，每个 step 里依次调 `train_batch(batch)` → `optim_step()` → `zero_grads()`。
- **五块 Config 积木**（u1-l4）：一次训练实验由 model、dataset、data_loader、train_module、trainer 五个 Config 组装而成，各自 `build()` 成真实对象。本讲的主角是其中「train_module」这块积木。
- **Config 体系**（u2-l1/u2-l2）：`TransformerTrainModuleConfig` 继承 `TrainModuleConfig` → `Config`，天然支持序列化与 dotlist 覆盖；其中的 `optim: OptimConfig` 是多态字段（u5-l1），`scheduler: Scheduler` 也是（u5-l2）。
- **micro-batch 与梯度累积**：把一个 rank 分到的 batch 切成若干小份（micro-batch）逐份前向反向，梯度会**累加**在 `param.grad` 上，数学上等价于一次大 batch——前提是损失按正确的方式归一化（本讲 4.3 会用公式讲清楚）。
- **DDP / FSDP 一句话版**（u7-l1 详述）：DDP 在每个 rank 复制完整模型、反向后 all-reduce 梯度；FSDP 把参数切成分片、各 rank 只存一份。二者本质上都是「包在模型外面的包装器」，而这层包装正是由 train module 负责套上去的。
- **`Stateful`**：PyTorch 分布式 checkpoint 的接口约定（`torch.distributed.checkpoint.stateful.Stateful`），实现了 `state_dict()` / `load_state_dict()` 的对象可以作为一个整体存入/读出分布式 checkpoint。`TrainModule` 继承了它。
- **`lru_cache`**：Python 标准库装饰器，把函数调用结果按参数缓存，同样参数只算一次（本讲里被用来缓存 FLOPs 估算）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/olmo_core/train/train_module/train_module.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/train_module.py) | `TrainModule` 抽象基类 + 参照实现 `BasicTrainModule`，定义与 `Trainer` 的全部接口 |
| [src/olmo_core/train/train_module/config.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/config.py) | `TrainModuleConfig`，只有一个抽象 `build()` 方法 |
| [src/olmo_core/train/train_module/transformer/config.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/config.py) | `TransformerTrainModuleConfig` 及 DP/TP/CP/EP/PP/AC 六个并行子配置 |
| [src/olmo_core/train/train_module/transformer/train_module.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py) | `TransformerTrainModule` 本体：`__init__` 的并行化流程与 `train_batch`/`optim_step` 全部实现 |
| [src/olmo_core/train/train_module/transformer/common.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/common.py) | `parallelize_model()`：按固定顺序给模型套上各层并行包装，最后初始化权重 |
| [src/olmo_core/train/trainer.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py) | 调用方：`Trainer` 训练循环里调用 train_module 各方法的位置 |
| src/examples/llm/train.py | 真实用法：组装一个 `TransformerTrainModuleConfig` 放进 `ExperimentConfig` |
| src/test/train/train_module/transformer/config_test.py | 测试：最小配置怎么写、`eval_only` 模式的行为断言 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 TrainModule 抽象**、**4.2 TransformerTrainModule 的构建（并行配置为何挂在这里）**、**4.3 一次训练步的完整代码路径**。

### 4.1 TrainModule 抽象：Trainer 与「怎么训练」之间的合同

#### 4.1.1 概念说明

`TrainModule` 是对「一个 `torch.nn.Module` + 一个 `torch.optim.Optimizer`」的包装，向 `Trainer` 屏蔽具体训练范式的差异。`Trainer` 不关心你训练的是 dense Transformer、MoE 还是多模态模型，也不关心你用 DDP 还是 FSDP、有没有流水线——它只会按固定节奏调用 train_module 的方法。反过来说，**任何实现了这组方法的类都能被 `Trainer` 训练**，这就是「合同」的含义。

这份合同还有一个特殊条款：**train_module 自己负责记录训练指标**（比如训练 loss），通过 `record_metric()` / `record_ce_loss()` 转交给 `Trainer` 去做跨 rank 归约与落盘（u6-l1 讲过的 ReduceType 机制）。

另外，`TrainModule` 继承了 PyTorch 的 `Stateful` 接口，所以整个 train module（模型 + 优化器）可以作为一个单元写进分布式 checkpoint——这是 u6-l4 断点续训的基础。

#### 4.1.2 核心流程

一个 train module 的完整生命周期：

```text
TrainModuleConfig.build(model)          # ① 从配置构建实例（并行化 + 建优化器，见 4.2）
        │
Trainer 构造时 _attach_trainer()       # ② 挂上 Trainer，触发 on_attach() 钩子
        │
pre_train()                            # ③ 训练循环开始前（校验 batch size 等）
        │
┌─ 每个 epoch ──────────────────────┐
│ zero_grads()                       │ ④ epoch 开头清一次梯度
│ ┌─ 每个 step ───────────────────┐ │
│ │ train_batch(batch)   前向+反向 │ │ ⑤ 梯度累加在 .grad 上
│ │ optim_step()         参数更新  │ │ ⑥ 裁剪→调 LR→step()
│ │ zero_grads()         清梯度    │ │ ⑦
│ └───────────────────────────────┘ │
└───────────────────────────────────┘
state_dict() / load_state_dict()      # ⑧ checkpoint 保存/恢复时按需调用
```

抽象方法按职责分成四组：

| 分组 | 方法 | 语义 |
| --- | --- | --- |
| 训练计算 | `train_batch` / `eval_batch` / `optim_step` / `zero_grads` | 一个 batch 的前向+反向 / 评估前向 / 优化器步进 / 清梯度 |
| 状态 | `state_dict` / `load_state_dict`（及 `state_dict_to_save` / `state_dict_to_load` 变体） | 模型与优化器状态的存取 |
| 评估规格 | `eval_batch_spec` | 告诉评估器每个 rank 的评估 batch 该多大（`EvalBatchSpec`，以 token 或 instance 计） |
| 统计 | `num_flops_per_token` / `global_num_flops_in_batch` | FLOPs 估算，供吞吐（petaFLOPs）指标使用 |

#### 4.1.3 源码精读

**类定义与文档**——注意它继承的是 `Stateful`，且用 `ABCMeta` 做抽象基类：

- [train_module.py:L73-L85](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/train_module.py#L73-L85)：`TrainModule` 的类声明。docstring 明确写着「实现者负责记录所有必要指标（如训练 loss）」以及「参见 `BasicTrainModule` 这个简单示例」。

**四个核心计算方法**——这就是合同的正文：

- [train_module.py:L168-L194](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/train_module.py#L168-L194)：四个 `@abstractmethod`：`train_batch(batch, dry_run=False)`（前向+反向）、`eval_batch(batch, labels)`（评估前向）、`optim_step()`（优化器步进）、`zero_grads()`（清梯度）。全部只有签名没有实现——具体行为完全由子类定义。

**Trainer 在哪里调用它们**——把合同双方对上号：

- [trainer.py:L1688](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1688)：epoch 开头 `self.train_module.zero_grads()`。
- [trainer.py:L1711-L1724](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1711-L1724)：每个 step 的核心三连：`train_batch(batch)` → 回调 `pre_optim_step()` → `optim_step()` → `zero_grads()`，然后才是 `post_train_batch()` / `post_step()` 回调。注意中间插着的回调钩子——CheckpointerCallback 就挂在 `post_train_batch` 上（u6-l1）。
- [trainer.py:L1667](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1667)：正式训练前用 mock batch 调一次 `train_batch(batch, dry_run=True)`，预热显存分配（`dry_run=True` 时跳过指标记录）。

**状态存取**——`Stateful` 合同：

- [train_module.py:L131-L166](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/train_module.py#L131-L166)：抽象的 `state_dict(optim=None)`（`optim=False` 时不带优化器状态）与 `load_state_dict()`；还有两个默认「直通」的变体 `state_dict_to_save` / `state_dict_to_load`，允许子类在「保存用的状态」和「加载用的状态」不一致时覆写（例如流水线并行的多 stage 场景）。

**指标上报**——train module 与 Trainer 之间唯一的「数据回流」通道：

- [train_module.py:L213-L238](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/train_module.py#L213-L238)：`record_metric(name, value, reduce_type, namespace, ...)` 只是转手调用 `self.trainer.record_metric(...)`；`record_ce_loss` 则是交叉熵损失的专用快捷方式。注意 `self.trainer` 属性在 Trainer 尚未挂载时会直接抛 `RuntimeError`（[L90-L100](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/train_module.py#L90-L100)）——这就是 4.3 实践里我们必须先挂一个（假的）Trainer 才能手动跑 `train_batch` 的原因。

**挂载机制**：

- [train_module.py:L240-L242](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/train_module.py#L240-L242)：`_attach_trainer(trainer)` 把 trainer 存进 `_trainer` 并立刻触发 `on_attach()` 钩子。这是个下划线开头的「内部」方法，`Trainer.__init__` 会调用它。

**参照实现 `BasicTrainModule`**：

- [train_module.py:L245-L275](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/train_module.py#L245-L275)：docstring 说它「主要用于示例和测试，实用实现请看 `TransformerTrainModule`」。构造参数就四个：`model`、`optim`（注意这里传的是**现成的 Optimizer 对象**，而 `TransformerTrainModule` 收的是 **OptimConfig**）、`rank_microbatch_size`、`max_grad_norm`。它的 `train_batch`（[L316-L373](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/train_module.py#L316-L373)）是一个没有并行、没有调度的「裸」版本，结构与 4.3 要讲的 `TransformerTrainModule.train_batch` 几乎同构，适合作为对照阅读的第一站。

**配置侧**——薄到只有一个方法：

- [config.py:L11-L15](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/config.py#L11-L15)：`TrainModuleConfig` 继承 `Config`，唯一成员是抽象 `build()`。所有公共字段（batch size、优化器、并行配置……）都由具体子类 `TransformerTrainModuleConfig` 自己定义。

#### 4.1.4 代码实践

**实践目标**：用库内现成的测试确认「合同」里 `eval_only` 模式的行为，并亲眼看到最小配置长什么样。

1. **操作步骤**：打开 [src/test/train/train_module/transformer/config_test.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/train/train_module/transformer/config_test.py)，阅读两个辅助函数（[L122-L137](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/train/train_module/transformer/config_test.py#L122-L137)）：`_tiny_dense_train_module_config()` 用 6 行构造了一个最小 `TransformerTrainModuleConfig`（`rank_microbatch_size=128`、`max_sequence_length=128`、`optim=AdamWConfig()`），`_tiny_dense_model()` 用 `TransformerConfig.llama_like(...)` 造了一个 2 层小模型。然后在仓库根目录运行：

   ```bash
   pytest -v src/test/train/train_module/transformer/config_test.py -k 'eval_only or builds_optimizer'
   ```

2. **需要观察的现象**：两个测试通过。`test_dense_train_module_eval_only_skips_optimizer`（[L140-L152](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/train/train_module/transformer/config_test.py#L140-L152)）断言 `eval_only=True` 时 `tm.optim is None`、state dict 不含 `optim` 键、调 `optim_step()`/`zero_grads()` 会抛 `AssertionError`。
3. **预期结果**：这两个测试是纯 CPU 测试，无需 GPU 即可通过。它们验证了「评估专用模式不建优化器」这条设计约定。
4. 若你的环境中 pytest 不可用，此实践可退化为纯阅读型：把两个测试的断言逐条对照 4.2 里 `__init__` 的 `eval_only` 分支即可（结果「待本地验证」）。

#### 4.1.5 小练习与答案

**练习 1**：`Trainer` 每个 step 调用 `optim_step()` 之后为什么还要调 `zero_grads()`？如果不调会发生什么？

<details>
<summary>参考答案</summary>

因为 `train_batch` 里的反向传播是**累加**语义——PyTorch 的 `loss.backward()` 把梯度加到 `param.grad` 上而不是覆盖。若不清零，下一个 step 的梯度会叠在上一个 step 的梯度之上，相当于用了一个错误的、不断膨胀的梯度去更新参数。注意 `Trainer` 在每个 epoch 开头也额外调了一次 `zero_grads()`（[trainer.py:L1688](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1688)），保证跨 epoch 边界也是干净的。
</details>

**练习 2**：为什么 `TrainModule` 要继承 PyTorch 的 `Stateful` 接口，而不是只提供普通的 `state_dict()` 方法？

<details>
<summary>参考答案</summary>

`Stateful` 是 `torch.distributed.checkpoint` 认识的标准接口：分布式 checkpoint 保存时会把实现了它的对象当作一个整体单元，自动处理「哪些张量在本 rank、如何分片聚合」的细节。继承它之后，`Trainer` / `Checkpointer` 可以直接把 train module 塞进保存清单，无需为模型和优化器各写一套分片逻辑（u6-l4 展开）。
</details>

**练习 3**：`train_batch(batch, dry_run=True)` 的 `dry_run` 参数是干什么用的？谁会传 `True`？

<details>
<summary>参考答案</summary>

用于「只走一遍前向反向、不记录指标、不产生副作用」的预热。`Trainer._dry_run_batch()`（[trainer.py:L1660-L1668](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1660-L1668)）在正式训练前用一个 mock batch 调用它，目的是提前触发显存分配、编译等一次性开销，避免第一步训练的计时与显存统计失真。
</details>

### 4.2 TransformerTrainModule 的构建：并行配置为何挂在这里

#### 4.2.1 概念说明

`TransformerTrainModule` 是 `TrainModule` 面向本库所有 `Transformer` 实现的具体落地。它的 `__init__` 干两件大事：**把模型并行化**、**构建优化器**。它的配置 `TransformerTrainModuleConfig` 上挂着一排显眼的字段：`dp_config`、`tp_config`、`cp_config`、`ep_config`、`pp_config`、`ac_config`（激活检查点）。

初学者常有的疑问是：**这些并行配置为什么不放在 `TransformerConfig`（模型图纸）上，而要放在 train module 上？** 三个理由：

1. **时序约束**：并行化必须发生在「模型 build 完成之后、优化器 build 之前」。FSDP 会把参数切成 DTensor 分片，优化器必须看到**最终形态**的参数才能正确建参数组（回忆 u5-l1：`OptimGroupOverride` 按参数全限定名分组，`MuPConfig` 按 `fan_in` 算乘数——这些都要求参数已经定型）。模型 build（u3-l2）发生在这条链路更早的位置，它不知道也不该知道训练时怎么切。
2. **静态图纸 vs 运行时环境**：`TransformerConfig` 是纯静态的「形状说明书」，序列化进 checkpoint 也只有形状信息；而并行配置依赖**运行时**的 world size、device mesh、GPU 数量——同一份模型配置，8 卡和 64 卡要用不同的并行方案。把二者分开，模型配置才能在不同集群间复用。
3. **数据并行的进程组要交还给 Trainer**：`Trainer` 切数据、归约指标都需要知道数据并行进程组（`dp_process_group`），这个组是世界网格（world mesh）划分的产物，而网格正是 train module 在构建时创建的。

家族里还有几个兄弟实现（见 [train_module/__init__.py:L8-L25](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/__init__.py#L8-L25) 的再导出清单）：`TransformerPipelineTrainModule`（开流水线时分发到的版本）、`OLMoDDPTrainModule`（配合融合 MoE 分布式优化器）、`MultimodalTransformerTrainModule`（多模态）。

#### 4.2.2 核心流程

`TransformerTrainModuleConfig.build(model)` 的完整链路：

```text
TransformerTrainModuleConfig.build(model, device)
        │
        ├─ pp_config 为 None？
        │     ├─ 是 → TransformerTrainModule(model, ...)
        │     └─ 否 → TransformerPipelineTrainModule(model, ...)
        │
TransformerTrainModule.__init__:
        ① 校验 rank_microbatch_size % max_sequence_length == 0
        ② is_distributed()? → build_world_mesh(dp, tp, cp, ep)   # 多维并行的进程网格
           （非分布式却给了并行配置 → 直接报错）
        ③ parallelize_model(model, ...)   # 按固定顺序套并行包装：
           PP → FP8 → CP → TP → EP → 激活检查点 → compile → DP → init_weights
        ④ optim.build(model, strict=True) # 优化器最后建（看到的是分片后的参数）
           （eval_only=True 时跳过，self.optim = None）
```

`parallelize_model` 的顺序是**强制固定**的，不能随意调换：例如 TP 必须在 EP 之后检查「TP + EP 未实现」；DP 的 FSDP 包装必须在最外层；而 `init_weights` 放在最后，是因为分片后的参数要用与单机一致的方式初始化再切开（u3-l6 讲过的 `distribute_like` 机制）。

#### 4.2.3 源码精读

**配置字段全景**：

- [transformer/config.py:L386-L430](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/config.py#L386-L430)：`TransformerTrainModuleConfig` 的全部字段。分四组：尺寸（`rank_microbatch_size`、`max_sequence_length`）、优化器（`optim: OptimConfig`、`max_grad_norm`、`scheduler`）、模型/并行（`compile_model`、`float8_config` 与 `pp/dp/tp/cp/ep/ac` 六个并行配置）、损失与杂项（`z_loss_multiplier`、`autocast_precision`、`label_ignore_index` 等）。
- [transformer/config.py:L432-L469](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/config.py#L432-L469)：`build()` 方法。先用 `as_dict(exclude_none=True, recurse=False)` 把自身摊平成 kwargs（嵌套的 Config 对象原样保留，`None` 字段剔除），再把 `autocast_precision` 从 `DType` 枚举转成 `torch.dtype`；关键分发逻辑在 [L456-L469](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/config.py#L456-L469)：**`pp_config` 非 `None` 就构建 `TransformerPipelineTrainModule`，否则构建 `TransformerTrainModule`**——这就是「开流水线自动换实现」的开关。

**`__init__` 四步**：

- [transformer/train_module.py:L164-L169](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L164-L169)：第一步校验——`rank_microbatch_size`（以 token 计）必须能被 `max_sequence_length` 整除，因为一个 micro-batch 至少装下一条完整序列。
- [transformer/train_module.py:L171-L187](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L171-L187)：第二步建世界网格——仅当 `is_distributed()`（torchrun 拉起的多进程）时，用 `build_world_mesh(dp=..., tp=..., cp=..., ep=...)` 把全局进程按各并行度切成多维网格；**单进程却传了任何并行配置会直接抛 `OLMoConfigurationError`**（[L179-L187](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L179-L187)）。日志会打印数据并行 world size。
- [transformer/train_module.py:L198-L212](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L198-L212)：第三步调用 `parallelize_model(...)` 并把返回值赋回 `self.model`——**注意模型对象可能被替换成包装后的版本**（如 FSDP 包装器）。
- [transformer/train_module.py:L235-L242](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L235-L242)：第四步建优化器——`self.optim = optim.build(self.model, strict=True)`。这里传的是 `OptimConfig`（配置）而非现成优化器，与 `BasicTrainModule` 形成对比；`eval_only=True` 时整段跳过，`self.optim` 保持 `None`。此外 [L239](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L239) 会先跑 `_assert_no_unowned_fp8_weight_stores` 防止「只有融合优化器才会训练的 FP8 权重店」被普通优化器漏掉（[L66-L95](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L66-L95)），这是一个很好的「防御式配置校验」范例。

**`parallelize_model` 的固定顺序**（common.py）：

- [common.py:L34-L58](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/common.py#L34-L58)：函数签名与第一步——若启用流水线（`pp_enabled`），取 pp mesh 并对模型调 `apply_pp`。这个函数同时支持单模型和模型列表（流水线的一个 rank 可能持有多个 stage）。
- [common.py:L59-L92](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/common.py#L59-L92)：依次应用 FP8（`apply_fp8`）→ 上下文并行（`apply_cp`）→ 张量并行（`apply_tp`，且 TP+EP 组合直接 `NotImplementedError`，[L75-L76](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/common.py#L75-L76)）→ 专家并行（`apply_ep`，非 MoE 模型用 EP 会报错）。这些 `apply_*` 方法群正是 u3-l1 里说的「build 之后由训练模块原地改造模型」的改造入口。
- [common.py:L94-L113](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/common.py#L94-L113)：激活检查点（`apply_activation_checkpointing`）与 `torch.compile`（`apply_compile`，CUDA 不可用时仅告警跳过）。
- [common.py:L115-L145](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/common.py#L115-L145)：数据并行最后套——按 `dp_config.name` 分派：FSDP/HSDP 走 `apply_fsdp`（MoE 模型先 `prepare_experts_for_fsdp`），DDP 走 `apply_ddp`。
- [common.py:L147-L156](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/common.py#L147-L156)：**最后才初始化权重**——`m.init_weights(...)` 带着 `world_mesh` 与 `model_part_idx` 调用，让分片环境下的初始化与单机结果一致（呼应 u3-l6 的 `init_seed` 与 `distribute_like`）。这也再次解释了为什么 u3-l2 说「`build()` 不填权重」：填权重这一步被安排在了训练模块的并行化之后。

**给 Trainer 的回执**：

- [transformer/train_module.py:L244-L246](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L244-L246)：`dp_process_group` 属性——从世界网格里取出数据并行进程组交给 `Trainer`（切数据、归约指标都用它）。
- [transformer/train_module.py:L280-L289](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L280-L289)：`pre_train()` 里的 batch size 校验——`global_batch_size` 必须被 `rank_microbatch_size × DP world size` 整除。注释特意说明为什么放在 `pre_train()` 而不是 `on_attach()`：`BatchSizeScheduler` 这类回调可能在挂载之后、开训之前改 batch size。

#### 4.2.4 代码实践

**实践目标**：亲手触发一次「并行配置校验」，并从日志确认 `parallelize_model` 的执行顺序。

1. **操作步骤**：在仓库根目录新建一个临时脚本（示例代码，放仓库外或临时目录均可，不要提交）：

   ```python
   # 示例代码：single_proc_parallel_config_error.py
   import torch
   from olmo_core.distributed.parallel import DataParallelType
   from olmo_core.nn.feed_forward import FeedForwardConfig
   from olmo_core.nn.transformer import TransformerConfig
   from olmo_core.optim import AdamWConfig
   from olmo_core.train.train_module.transformer import (
       TransformerDataParallelConfig,
       TransformerTrainModuleConfig,
   )

   model = TransformerConfig.llama_like(
       d_model=64, vocab_size=128, n_layers=2, n_heads=2,
       feed_forward=FeedForwardConfig(hidden_size=128, bias=False),
   ).build(init_device="cpu")

   cfg = TransformerTrainModuleConfig(
       rank_microbatch_size=128,
       max_sequence_length=128,
       optim=AdamWConfig(),
       dp_config=TransformerDataParallelConfig(name=DataParallelType.ddp),  # 单进程却要 DDP
   )
   tm = cfg.build(model, device=torch.device("cpu"))
   ```

   直接 `python single_proc_parallel_config_error.py` 运行（不要用 torchrun）。
2. **需要观察的现象**：抛出 `OLMoConfigurationError: Training parallelism configs are only valid for distributed training`，堆栈指向 `__init__` 的校验分支（[transformer/train_module.py:L179-L187](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L179-L187)）。
3. **预期结果**：报错信息原文与源码一致。然后删掉 `dp_config=...` 那一行重跑，改为在 `cfg.build(...)` 前加 `import logging; logging.basicConfig(level=logging.INFO)`，成功构建时应看到 `Initializing model weights...` 日志（来自 [common.py:L148](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/common.py#L148)）——证明 `init_weights` 是构建链路的收尾动作。
4. 以上行为由源码直接保证；若本地未安装 olmo_core，结果「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：如果把 FSDP 包装放在 `optim.build()` 之后会发生什么？

<details>
<summary>参考答案</summary>

优化器会持有**未分片**的完整参数引用。随后 FSDP 把参数替换为 DTensor 分片，优化器手里的引用与模型实际参数脱钩：轻则更新作用在错误的对象上，重则状态字典对不上导致 checkpoint 无法恢复。所以顺序必须是「并行化 → 建优化器」，源码里两步前后相接（[transformer/train_module.py:L198-L212](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L198-L212) 与 [L235-L242](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L235-L242)）。
</details>

**练习 2**：`TransformerTrainModuleConfig.build()` 是怎么决定构建 `TransformerTrainModule` 还是 `TransformerPipelineTrainModule` 的？这种「一个配置类、多个实现类」的设计有什么好处？

<details>
<summary>参考答案</summary>

只看 `pp_config` 是否为 `None`（[transformer/config.py:L456-L469](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/config.py#L456-L469)）。好处：用户视角只有一个配置类、一套 YAML 字段，命令行加一个 `--train_module.pp_config.degree=4` 就能切换到流水线实现，实验配置无需重写；实现视角则允许流水线版本有完全不同的 `train_batch` 内部结构而不污染普通版本。
</details>

**练习 3**：`parallelize_model` 里为什么 `init_weights` 必须放在所有并行包装之后？

<details>
<summary>参考答案</summary>

因为初始化要在**最终参数形态**上进行，且分片环境下的随机数消耗必须与单机一致（u3-l6：`init_seed` 走专属 `torch.Generator`，流水线下按 `pp_rank + part_idx × pp_size` 偏移；`_apply_init` + `distribute_like` 保证 DTensor 分片与单机初始化一致）。若先初始化再并行化，随机数消耗顺序和分片内容都会变，同一 seed 不再可复现。
</details>

### 4.3 一次训练步的完整代码路径：train_batch → optim_step → zero_grads

#### 4.3.1 概念说明

这是本讲的核心。`TransformerTrainModule` 的三个方法合起来构成一步训练，我们逐段读源码。先建立直觉：

- **`train_batch`**：拿一个 batch（字典，至少含 `input_ids`），切 成若干 micro-batch，对每个 micro-batch 做前向算 loss 再反向；梯度自然累加在 `param.grad` 上。**它不更新参数**。
- **`optim_step`**：先（可选）裁剪梯度，再（可选）按调度器调整学习率，最后 `optim.step()` 更新参数。
- **`zero_grads`**：清空梯度，为下一个 step 做准备。

梯度累积的正确性依赖一个关键的归一化决定：**每个 micro-batch 的 loss 用 `sum` 归约，再除以整个 batch 的有效 token 数**。这就是源码里的 `loss_reduction="sum"` + `loss_div_factor=batch_num_tokens_for_loss` 组合。

#### 4.3.2 核心流程

`train_batch` 的执行序列：

```text
train_batch(batch):
  ① _set_model_mode("train")                    # 幂等地切 train 模式
  ② batch 里没有 labels? → get_labels()          # input_ids 左移一位，末位 pad 成 -100
  ③ 数出有效 token 数 N（labels != -100 的个数）
  ④ 记录 masked labels 比例指标
  ⑤ split_batch(batch, rank_microbatch_size // seq_len)   # 按实例数切 micro-batch
  ⑥ for 每个 micro-batch:
       _train_microbatch_context(i, n)           # 只在最后一个 micro-batch 开梯度同步
       _prepare_batch() → input_ids, labels, 其余 kwargs
       model_forward(..., loss_reduction="sum",
                     loss_div_factor=N)           # 返回 (logits, loss, ce_loss, z_loss)
       ce_batch_loss += ce_loss.detach()          # 累计用于记录的 loss
       loss.backward()                            # 梯度累加到 .grad
  ⑦ model.post_batch()                           # 模型侧的 batch 后处理钩子
  ⑧ 记录 CE loss / Z loss / 辅助指标（经 record_* 转交 Trainer）
```

**为什么 `sum ÷ N` 而不是每个 micro-batch 各自取 `mean`？** 设整批共 \( N \) 个有效 token 的损失为 \( \ell_1, \dots, \ell_N \)，我们想要的梯度是

\[
\nabla_\theta \left( \frac{1}{N} \sum_{i=1}^{N} \ell_i \right) = \frac{1}{N} \sum_{i=1}^{N} \nabla_\theta \ell_i .
\]

把 batch 切成若干 micro-batch 后，`backward()` 在 `.grad` 上做的是**加法**。若每个 micro-batch 反传 \(\sum_{i \in B_k} \ell_i / N\)（即 `sum` 归约再除以全局 \( N \)），累加结果恰好是上式——**与 micro-batch 怎么划分无关**。反之若每个 micro-batch 用自己的 \(\frac{1}{|B_k|}\sum_{i \in B_k} \ell_i\)（各自 `mean`），小 micro-batch 的每个 token 会被等权放大，划分方式不同得到的梯度就不同，损失曲线还会随 batch 内样本数波动（padding 多的 batch 尤其明显）。`BasicTrainModule` 里的同款源码注释（[train_module.py:L349-L351](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/train_module.py#L349-L351)）明确点明了这一点。

#### 4.3.3 源码精读

**labels 从哪来**：

- [data/utils.py:L613-L628](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/utils.py#L613-L628)：`get_labels()`——labels 就是 input_ids **左移一位**（预测下一个 token），首位被丢弃、末位 pad 成 `label_ignore_index`（默认 -100）；若 batch 带 `label_mask` / `attention_mask` / `instance_mask`，对应位置也填成 -100（这些 token 不计损失，u4-l1 讲过 label_mask 的用途）。
- [transformer/train_module.py:L419-L427](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L419-L427)：`train_batch` 开头——batch 没有 `labels` 键就现场生成；随后 `(batch["labels"] != self.label_ignore_index).sum()` 得到本 batch 的有效 token 数 `batch_num_tokens_for_loss`，即公式里的 \( N \)。

**切 micro-batch**：

- [data/utils.py:L43-L57](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/utils.py#L43-L57)：`split_batch(batch, num_microbatch_instances)` 沿第 0 维把张量切成若干份。
- [transformer/train_module.py:L457-L463](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L457-L463)：每个 micro-batch 的 token 预算是 `rank_microbatch_size`，序列长度是 `seq_len`，所以一份装 `rank_microbatch_size // seq_len` 条实例。若 `rank_microbatch_size < seq_len` 直接报错（一个 micro-batch 至少一条完整序列）。

**micro-batch 循环主体**：

- [transformer/train_module.py:L466-L491](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L466-L491)：循环体。`_prepare_batch`（[L712-L719](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L712-L719)）把 `input_ids`/`labels` 从字典里 pop 出来，**剩下的键（如 `label_mask`、`doc_lens`）作为 model_kwargs 原样传给模型**。前向调用（[L471-L480](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L471-L480)）带上了 `loss_reduction="sum"` 与 `loss_div_factor=batch_num_tokens_for_loss`——归一化发生在模型内部的 LMHead（u3-l4：loss 计算被收进 LMHead，支持 fused 实现免物化 logits）。返回的四元组解包自 `LMOutputWithLoss`（NamedTuple：`logits/loss/ce_loss/z_loss`，见 [lm_head.py:L192-L200](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/lm_head.py#L192-L200)）：`loss` 是拿来 backward 的（= ce_loss + z_loss_multiplier × z_loss），`ce_loss`/`z_loss` 仅用于记录。`ce_batch_loss += ce_loss.detach()`（[L483](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L483)）只累计** detached** 的数值避免保留整张计算图；`loss.backward()` 在 [L491](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L491)。

**梯度同步的开关——梯度累积的另一半**：

- [transformer/train_module.py:L636-L655](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L636-L655)：`_train_microbatch_context` 是一个上下文管理器，按模型包装类型三选一：**DDP** 用 `model.no_sync()` 关掉中间 micro-batch 的梯度 all-reduce（[L650-L653](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L650-L653)）；**FSDP** 用 `set_is_last_backward(is_last_mb)`（[L642-L646](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L642-L646)）；**HSDP** 额外用 `set_requires_all_reduce(is_last_mb)` 把 all-reduce 推迟到最后的 micro-batch（[L648-L649](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L648-L649)）。三者目的一致：**只在最后一个 micro-batch 触发跨 rank 梯度同步**——既然各 micro-batch 的梯度本地累加即可，中间就同步纯属浪费通信。没有这层开关，DDP 每个 micro-batch 都会 all-reduce 一次，通信次数翻倍。

**收尾与指标**：

- [transformer/train_module.py:L495-L499](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L495-L499)：`model.post_batch()`（基类默认空操作，某些实现有副作用）与 `dry_run` 分支（重置辅助指标后直接返回，不记录任何东西）。
- [transformer/train_module.py:L502-L512](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L502-L512)：**SkipStepOptimizer 特例**——若优化器是带 loss spike 检测的包装器（u5-l3），必须**立刻**跨 rank all-reduce loss（不能等 Trainer 攒批归约），因为它要在 `optim_step` 里用 `latest_loss` 判断是否跳步；归约时还要绕开除以 world size 的常规路径（先乘再除 `_reduce_divide_factor` 的写法是为了兼容 bf16 等低精度下的数值稳定性）。普通优化器则走 `record_ce_loss(ce_batch_loss, ReduceType.mean)` 交给 Trainer 延迟归约。
- [transformer/train_module.py:L529-L537](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L529-L537)：`model.compute_auxiliary_metrics(reset=True)` 取出模型累积的辅助指标（基类返回空字典，MoE 的负载均衡损失走这条路，u3-l5）。

**`optim_step` 三段式**：

- [transformer/train_module.py:L583-L606](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L583-L606)：第一段**裁剪**（`max_grad_norm` 非 `None` 时）调 `_clip_grad_norm`，并把（已归约的）梯度范数记为指标；若是 `SkipStepOptimizer`，把 `latest_grad_norm` 喂给它（呼应 u5-l3 的时序契约：`latest_loss`/`latest_grad_norm` 必须先于 `step()` 就位）。第二段**调学习率**（[L596-L599](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L596-L599)）：对每个参数组调 `scheduler.set_lr(group, self.trainer)`——注意调度器要拿 trainer 读当前 step（u5-l2）。第三段 `optim.step()`（[L602](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L602)），随后 `model.post_optim_step()`（[L606](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L606)，float8 训练用它预计算动态 scale）。
- [transformer/train_module.py:L683-L710](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L683-L710)：`_clip_grad_norm` 的两个分支——FSDP 模型直接用包装器自带的 `clip_grad_norm_`（它知道分片怎么归约）；否则手工算总范数，**关键在 [L704-L707](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L704-L707)**：若 `total_norm` 是 DTensor（开了 TP/CP 等非流水线并行时），必须 `full_tensor()` 把各 rank 的局部范数归约成全局值，否则每个 rank 会按自己那份范数裁剪，裁出来的模型各 rank 不一致。
- [transformer/train_module.py:L608-L610](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L608-L610)：`zero_grads` 一行——`optim.zero_grad(set_to_none=True)`（置 `None` 而非清零可以省一次稠密写、并让下次反向重新分配稀疏梯度）。

**前向的统一入口**：

- [transformer/train_module.py:L612-L619](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L612-L619)：`model_forward` 把 `self.model(input_ids, labels=labels, **kwargs)` 包进 `_model_forward_context`——后者只在设置了 `autocast_precision` 时套一层 `torch.autocast`（AMP 混合精度）。`train_batch` 与 `eval_batch` 都走这个入口。
- [transformer/train_module.py:L721-L729](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L721-L729)：`_set_model_mode` 用一个成员变量记住当前模式，**只在变化时**调 `model.train()`/`model.eval()`——避免每个 batch 重复切换的开销与副作用。

#### 4.3.4 代码实践

**实践目标**：不启动完整 `Trainer`，手动驱动一个 `TransformerTrainModule` 走完「前向反向 → 优化器步进 → 清梯度」，打印 loss、梯度与优化器状态，验证你理解的调用链与源码一致。

关键障碍与解法：`train_batch` 末尾会调 `record_metric` → `self.trainer.record_metric(...)`，而 `self.trainer` 在未挂载时抛 `RuntimeError`。解法是仿照库内测试的思路注入一个**stub trainer**（鸭子类型：只要有 `record_metric` 与 `record_ce_loss` 两个方法即可）。

1. **操作步骤**：将以下示例代码保存为 `manual_train_step.py`（示例代码，非项目原有文件）：

   ```python
   # 示例代码：manual_train_step.py —— 手动驱动 TransformerTrainModule 走一步训练
   import torch
   from olmo_core.nn.feed_forward import FeedForwardConfig
   from olmo_core.nn.transformer import TransformerConfig
   from olmo_core.optim import AdamWConfig
   from olmo_core.train.train_module.transformer import TransformerTrainModuleConfig

   SEQ_LEN = 128

   class StubTrainer:
       """只实现 train_batch/optim_step 会用到的那两个上报方法。"""
       def __init__(self):
           self.metrics, self.ce_losses = [], []
       def record_metric(self, name, value, reduce_type=None, namespace=None, merge_strategy=None):
           self.metrics.append((namespace or "", name))
       def record_ce_loss(self, value, reduce_type=None):
           self.ce_losses.append(float(value))

   torch.manual_seed(0)
   model_cfg = TransformerConfig.llama_like(
       d_model=64, vocab_size=128, n_layers=2, n_heads=2,
       feed_forward=FeedForwardConfig(hidden_size=128, bias=False),
   )
   model = model_cfg.build(init_device="cpu")

   tm_cfg = TransformerTrainModuleConfig(   # 不设 dp_config：单进程、非分布式
       rank_microbatch_size=SEQ_LEN,         # = 序列长度 → 每个 micro-batch 恰好 1 条实例
       max_sequence_length=SEQ_LEN,
       optim=AdamWConfig(lr=1e-3),
   )
   tm = tm_cfg.build(model, device=torch.device("cpu"))
   tm._trainer = StubTrainer()               # 注入 stub，让 record_* 有处可去

   # ① 构造随机 batch：2 条样本 × 128 token；labels 留空，让 train_batch 自己生成
   batch = {"input_ids": torch.randint(0, 128, (2, SEQ_LEN))}

   # ② 前向 + 反向（对应 Trainer 里的 train_batch 调用）
   tm.train_batch(batch)
   print("CE loss:", tm._trainer.ce_losses[-1])
   grads = [p.grad for p in tm.model.parameters() if p.grad is not None]
   print("有梯度的参数个数:", len(grads))
   print("梯度总范数:", torch.sqrt(sum(g.float().pow(2).sum() for g in grads)).item())

   # ③ 优化器步进（对应 optim_step），检查 AdamW 状态是否生成
   tm.optim_step()
   some_param = next(iter(tm.optim.state))
   print("优化器状态键:", sorted(tm.optim.state[some_param].keys()))

   # ④ 清梯度（对应 zero_grads），验证 .grad 变回 None
   tm.zero_grads()
   print("清梯度后非空 .grad 个数:",
         sum(p.grad is not None for p in tm.model.parameters()))
   ```

   运行：`python manual_train_step.py`（CPU 即可，无需 GPU / torchrun）。

2. **需要观察的现象**：四段输出——CE loss 数值；有梯度的参数个数；AdamW 状态键列表；清梯度后的非空梯度计数。
3. **预期结果**：
   - CE loss 是有限正数，量级在随机初始化模型的 \(\ln 128 \approx 4.85\) 附近；
   - 全部（非共享导致的重复计入除外）参数都拿到梯度；
   - `optim.step()` 之后状态里出现 `['exp_avg', 'exp_avg_sq', 'step']` 三个键（AdamW 的一阶/二阶动量与步数）；
   - `zero_grads()` 后非空 `.grad` 计数为 0（`set_to_none=True` 的效果）。
   - 数值细节属「待本地验证」，但四类行为本身由源码直接保证。
4. **对照源码**：数一数 [transformer/train_module.py:L466-L491](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L466-L491) 的循环次数——batch 有 2 条实例、`rank_microbatch_size // seq_len = 1`，所以循环执行 **2 次**（即 2 步梯度累积）。可以在自己的脚本里包一层打印来验证，不要修改仓库源码。

#### 4.3.5 小练习与答案

**练习 1**：把 4.3.4 脚本里的 `rank_microbatch_size` 改成 `2 * SEQ_LEN`（其余不变、seed 相同）重建后再跑一次 `train_batch`，梯度总范数会怎么变？

<details>
<summary>参考答案</summary>

不变（浮点误差内）。因为此时整个 batch 就是**一个** micro-batch，循环只执行 1 次；而 `sum ÷ N` 的归一化保证梯度只依赖整批 token，与 micro-batch 划分无关（4.3.2 的公式）。这正是「梯度累积等价于大 batch」的可验证形式。tiny 差异来自浮点求和顺序，属正常现象。
</details>

**练习 2**：为什么 `SkipStepOptimizer` 用户走的是「立刻 all-reduce loss」的特例分支，而不是和普通优化器一样把 loss 交给 Trainer 延迟归约？

<details>
<summary>参考答案</summary>

`SkipStepOptimizer` 要在紧随其后的 `optim_step()` 里用滑动窗口的「均值 + kσ」判据检测 loss spike（u5-l3），等不得 Trainer 按 `metrics_collect_interval` 攒批归约。且各 rank 必须基于**同一个** loss 值做跳步决策，否则有的 rank 跳、有的不跳，模型参数会分叉。所以 [transformer/train_module.py:L502-L510](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L502-L510) 在 `train_batch` 末尾就同步 all-reduce，并把结果写进 `self.optim.latest_loss`。
</details>

**练习 3**：`_clip_grad_norm` 里 `isinstance(total_norm, DTensor)` 分支删掉会有什么后果？

<details>
<summary>参考答案</summary>

开了 TP/CP 等并行后，每个 rank 算出的 `total_norm` 只是**本 rank 分片**的局部范数。没有 `full_tensor()` 归约，各 rank 会按不同的局部范数执行裁剪缩放，导致同一参数在不同 rank 上更新不一致，模型悄悄发散。这正是张量并行训练最隐蔽的一类 bug（u7-l2 会再次遇到「跨 rank 归约」这个主题）。
</details>

## 5. 综合实践

**任务：把 4.3.4 的单步脚本扩展成「微型训练循环 + 梯度累积等价性 + 单卡 DDP」三连实验。**

1. **微型循环**：在 4.3.4 脚本外再套一个 10 步的循环（每步换一批新的随机 `input_ids`，`seed` 固定），记录每步 CE loss，画出（或打印出）loss 随 step 的下降。这一步你在手写一个去掉所有回调、checkpoint、评估的「裸 Trainer」，写完后对照 [trainer.py:L1691-L1734](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1691-L1734) 的 `_fit_epoch`，逐行找出你写的循环里缺了哪些环节（答案应包括：step 簿记、token 计数、pre_step / pre_optim_step / post_train_batch / post_step 回调、should_skip、停训检查）。
2. **梯度累积等价性**：分别用 `rank_microbatch_size = SEQ_LEN` 与 `rank_microbatch_size = 2 * SEQ_LEN` 各跑一次 `train_batch`（相同 batch、相同 seed 重建模型），比较两者的梯度总范数与 10 步后的 loss 轨迹是否一致（浮点误差内），用实验印证 4.3.2 的数学。
3. **真正的单卡 DP（选做，需 torchrun）**：给 config 加 `dp_config=TransformerDataParallelConfig(name=DataParallelType.ddp)`，并改用 `torchrun --nproc-per-node=1 manual_train_step.py` 启动（world size 为 1 的 DDP 是合法配置）。观察日志依次出现 `Data parallel world size = 1`（[transformer/train_module.py:L178](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L178)）与 `Applied DDP to the model...`（[common.py:L143](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/common.py#L143)）。此变体依赖本地分布式后端可用性，结果「待本地验证」。

完成标志：你能不看讲义，向别人画出从 `TransformerTrainModuleConfig.build()` 到一次参数更新之间的完整框图（构建四步 + 训练步三段），并指出梯度同步、loss 归一化、学习率调整分别发生在哪个环节。

## 6. 本讲小结

- `TrainModule` 是 `Trainer` 与「怎么训练」之间的合同：`train_batch`/`eval_batch`/`optim_step`/`zero_grads` 四个计算方法 + state_dict 族 + 评估规格与 FLOPs 统计；`Trainer` 在 [trainer.py:L1711-L1724](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1711-L1724) 按固定节奏调用它们，指标则经 `record_metric`/`record_ce_loss` 从 train module 回流。
- DP/TP/CP/EP/PP/AC 六套并行配置挂在 `TransformerTrainModuleConfig` 上，因为并行化必须夹在「模型建好后、优化器建好前」：它改变参数的物理形态（DTensor 分片、FSDP 包装）、依赖运行时 world size，且产出的数据并行进程组要交还 Trainer 切数据。
- 构建链路四步：校验尺寸 → `build_world_mesh` → `parallelize_model`（固定顺序 PP→FP8→CP→TP→EP→激活检查点→compile→DP，**最后** `init_weights`）→ `optim.build`；`pp_config` 非 None 时 `build()` 自动分发出 `TransformerPipelineTrainModule`；`eval_only=True` 跳过优化器。
- `train_batch` 骨架：生成 labels（input_ids 左移、-100 pad）→ 数出全局有效 token 数 \( N \) → 按 token 预算切 micro-batch → 逐个「前向（`sum` 损失 ÷ \( N \)）→ backward」→ 记录指标；`sum + loss_div_factor=N` 保证梯度与 micro-batch 划分无关。
- 梯度同步开关在 `_train_microbatch_context`：DDP 用 `no_sync()`、FSDP 用 `set_is_last_backward`、HSDP 用 `set_requires_all_reduce`，都只让**最后一个** micro-batch 触发跨 rank 同步。
- `optim_step` 三段式：裁剪（DTensor 范数必须 `full_tensor()` 归约）→ 逐参数组 `scheduler.set_lr` → `optim.step()` + `post_optim_step()`；`SkipStepOptimizer` 用户额外要求 loss 在 `train_batch` 末尾就被即时 all-reduce 进 `latest_loss`。

## 7. 下一步学习建议

- **u6-l3（回调系统）**：本讲反复看到 `pre_optim_step`、`post_train_batch` 等钩子插在 train_module 调用之间，下一讲系统讲 `Callback` 体系，并动手写一个自定义回调挂进 `TrainerConfig`。
- **u7-l1（数据并行）**：本讲只说清了 FSDP/DDP 包装「在哪里、由谁套上」；分片策略、梯度归约细节与 HSDP 的取舍留给下一单元展开。
- **u7-l2（TP/PP/CP/EP）**：`pp_config` 分发出的 `TransformerPipelineTrainModule` 与 `parallelize_model` 里 CP/TP/EP 各步的内部机制是多维并行的重头戏。
- **延伸阅读**：[src/olmo_core/train/train_module/transformer/objective.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/objective.py)（`train_batch_with_loss` 的自定义训练目标接口）与 [ddp_train_module.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/ddp_train_module.py)（融合 MoE 优化器的 `OLMoDDPTrainModule`）是本讲两个「兄弟实现」，对照阅读能加深对抽象的理解；`BasicTrainModule`（[train_module.py:L245-L413](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/train_module.py#L245-L413)）则是无并行、无调度版本的最好参照。

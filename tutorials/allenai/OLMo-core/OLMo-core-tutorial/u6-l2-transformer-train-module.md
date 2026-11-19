# TransformerTrainModule：前向、反向与并行设置的汇聚点

## 1. 本讲目标

上一讲（u6-l1）我们看到：`Trainer.fit()` 只做编排，前向、反向、优化器更新全部委托给一个叫 `train_module` 的对象。本讲就打开这个对象，学完后你应该能够：

1. 说出 `TrainModule` 抽象定义了哪些方法、`Trainer` 在哪些时机调用它们（即二者之间的「合同」）。
2. 解释为什么 DP/TP/PP/CP/EP 五套并行配置都挂在 train module 上，而不是挂在模型配置 `TransformerConfig` 上。
3. 手动跟踪一次 `train_batch → optim_step → zero_grads` 的完整代码路径，说清梯度累积、损失归一化、梯度裁剪各自发生在哪一行。
4. 不借助完整 `Trainer`，用随机数据手动驱动一个 `TransformerTrainModule` 走完一步训练，并检查 loss 与优化器状态。

## 2. 前置知识

- **Trainer 与 TrainModule 的分工**（u6-l1）：`Trainer` 管「何时」（step 计数、Duration、回调钩子、指标收集），`train_module` 管「怎么算」（前向、反向、优化器步进）。
- **五块 Config 积木**（u1-l4）：一次训练实验由 model、dataset、data_loader、train_module、trainer 五个 Config 组装而成，各自 `build()`。
- **Config 体系**（u2-l1/u2-l2）：`TransformerTrainModuleConfig` 继承 `TrainModuleConfig` → `Config`，因此天然支持序列化与 dotlist 覆盖；`optim: OptimConfig` 是多态字段（u5-l1）。
- **micro-batch 与梯度累积**：把一个 rank 的 batch 切成若干小份（micro-batch）逐份前向反向，梯度会**累加**在 `param.grad` 上，等价于一次大 batch 前向反向——前提是损失按正确的方式归一化（本讲 4.3 会讲清楚）。
- **DDP / FSDP 一句话版**（u7-l1 详述）：DDP 在每个 rank 复制完整模型、反向后 all-reduce 梯度；FSDP 把参数切成分片、各 rank 只存一份。二者都是「包在模型外面的包装器」，这层包装正是由 train module 负责套上去的。
- **`Stateful`**：PyTorch 分布式 checkpoint 的接口约定，实现 `state_dict()`/`load_state_dict()` 的对象可以整体存入/读出分布式 checkpoint。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/olmo_core/train/train_module/train_module.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/train_module.py) | `TrainModule` 抽象基类 + 参照实现 `BasicTrainModule`，定义与 `Trainer` 的全部接口 |
| [src/olmo_core/train/train_module/config.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/config.py) | `TrainModuleConfig`，只有一个抽象 `build()` |
| [src/olmo_core/train/train_module/transformer/config.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/config.py) | `TransformerTrainModuleConfig` 及 DP/TP/CP/EP/PP/AC 六个并行子配置 |
| [src/olmo_core/train/train_module/transformer/train_module.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py) | `TransformerTrainModule` 本体：`train_batch`/`optim_step` 等全部实现 |
| [src/olmo_core/train/train_module/transformer/common.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/common.py) | `parallelize_model()`：按固定顺序给模型套上各层并行包装并初始化权重 |
| src/olmo_core/train/trainer.py | 调用方：`Trainer` 在训练循环里调用 train_module 的各方法 |
| src/examples/llm/train.py | 真实用法示例：如何组装一个 `TransformerTrainModuleConfig` |
| src/test/train/train_module/transformer/config_test.py、src/test/train/train_module/multimodal_train_module_test.py | 测试：最小配置怎么写、如何脱离 Trainer 手动调 `train_batch` |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 TrainModule 抽象**、**4.2 TransformerTrainModule 的构建（并行配置为何挂在这里）**、**4.3 一次训练步的代码路径**。

### 4.1 TrainModule 抽象：Trainer 与「怎么训练」之间的合同

#### 4.1.1 概念说明

`TrainModule` 是对「一个 `nn.Module` + 一个 `Optimizer`」的包装，向 `Trainer` 屏蔽具体训练范式的差异。`Trainer` 不关心你训练的是 dense Transformer、MoE 还是多模态模型，也不关心你用 DDP 还是 FSDP——它只会按固定节奏调用 train_module 的方法。反过来说，**任何能响应这组方法的类都可以被 `Trainer` 训练**，这就是「合同」的含义。

抽象方法分成四组：

| 分组 | 方法 | 语义 |
| --- | --- | --- |
| 训练计算 | `train_batch` / `eval_batch` / `optim_step` / `zero_grads` | 一个 batch 的前向+反向 / 评估前向 / 优化器步进 / 清梯度 |
| 状态 | `state_dict` / `load_state_dict`（含 `state_dict_to_save` / `state_dict_to_load` 变体） | 模型与优化器状态的存取，供 checkpoint 使用 |
| 评估规格 | `eval_batch_spec` | 告诉评估器每个 rank 的评估 batch 该多大（以 token 或 instance 计） |
| 统计 | `num_flops_per_token` / `global_num_flops_in_batch` | 计算 FLOPs，供吞吐指标使用 |

此外还有几个**可选钩子**：`on_attach()`（Trainer 挂上时触发）、`pre_train()`（训练循环开始前触发），以及一个重要约定——**train_module 自己负责记录训练 loss 等指标**，通过 `record_metric()`/`record_ce_loss()` 转交给 Trainer。

#### 4.1.2 核心流程

一个 train module 的完整生命周期：

```text
Config.build(model)                     # 构建实例（并行化 + 建优化器，见 4.2）
      │
Trainer.__init__ ──► _attach_trainer()  # Trainer 持有它，触发 on_attach()
      │
pre_train()                             # 校验 batch size 等迟来约束
      │
┌─ 循环每个 step（由 Trainer 驱动）────────────────┐
│  train_batch(batch)    前向 + 反向（可含梯度累积）│
│  optim_step()          裁剪 → 调 LR → step()     │
│  zero_grads()          清空 .grad                │
└─────────────────────────────────────────────────┘
      │
state_dict_to_save() / state_dict_to_load()   # checkpoint 读写
```

#### 4.1.3 源码精读

**① 抽象基类的定义与「trainer 属性」。**
[train_module.py:L73-L100](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/train_module.py#L73-L100) 定义了 `TrainModule`（混入 PyTorch 的 `Stateful`），并在 `trainer` 属性里强调：只有 Trainer 挂载之后才能访问。挂载动作由 Trainer 主动完成：[trainer.py:L415](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L415) 中的 `self.train_module._attach_trainer(self)` 会设置 `_trainer` 并触发 `on_attach()`（实现在 [train_module.py:L240-L242](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/train_module.py#L240-L242)）。

**② 合同正文：抽象方法清单。**
[train_module.py:L131-L194](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/train_module.py#L131-L194) 依次声明 `state_dict`、`load_state_dict`、`train_batch`、`eval_batch`、`optim_step`、`zero_grads`，紧接其后的 [train_module.py:L196-L211](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/train_module.py#L196-L211) 声明两个 FLOPs 统计方法。注意 `dp_process_group` 是**非抽象**的可选属性（[train_module.py:L102-L108](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/train_module.py#L102-L108)）：只有当数据并行进程组不是默认进程组时才需要返回它，Trainer 靠它来切分数据（u4-l1 讲过 data loader 按 DP rank 切批）。

**③ 指标上报通道。**
[train_module.py:L213-L238](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/train_module.py#L213-L238)：`record_metric`/`record_ce_loss` 只是一层转发，把指标交给 `self.trainer` 的对应方法（跨 rank 归约语义 `ReduceType` 见 u6-l1 第 4 节）。这解释了为什么手动调用 `train_batch` 需要一个「像 Trainer 的东西」在场——本讲综合实践会用一个 stub 绕过。

**④ 参照实现 BasicTrainModule。**
类文档 [train_module.py:L245-L259](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/train_module.py#L245-L259) 明说它「主要用于示例和测试」。它的 `train_batch`（[train_module.py:L316-L373](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/train_module.py#L316-L373)）浓缩了本范式最核心的模式：

```python
micro_batches = split_batch(batch, self.rank_microbatch_size // seq_len)  # 按 token 预算切块
for micro_batch in micro_batches:
    logits = self.model_forward(micro_batch)
    ce_loss, _ = self.loss_fn(..., reduction="sum")   # 注意：sum，不是 mean
    ce_loss.div_(batch_num_tokens_for_loss)           # 除以整个 batch 的有效 token 数
    ce_loss.backward()                                # 梯度在 .grad 上累加
```

为什么用 `reduction="sum"` 再除以**整个 batch** 的有效 token 数，而不是每个 micro-batch 各自取 mean？因为 micro-batch 大小可能不均，各自取 mean 会让大 micro-batch 的 token 被低估；先求和再统一除，保证无论怎么切，梯度都严格等价于一次大前向（数学说明见 4.3.2）。

**⑤ 配置侧：TrainModuleConfig。**
[config.py:L11-L15](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/config.py#L11-L15) 整个基类只有一个抽象 `build()`——它就是第 2 单元讲过的 `ModuleConfig.build()` 模式在训练模块上的翻版。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：把「Trainer 在哪儿调用 train_module」这张调用点地图亲手画出来。
2. **操作步骤**：在 `src/olmo_core/train/trainer.py` 中搜索 `train_module.`，你至少能找到 6 处关键调用（`pre_train` 两处、`train_batch` 两处、`optim_step`、`zero_grads`、`_attach_trainer`）；再对照 [trainer.py:L1660-L1668](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1660-L1668)（dry-run 批）与 [trainer.py:L1688-L1724](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1688-L1724)（正式训练循环）读上下文。
3. **需要观察的现象**：正式循环里 `train_batch` 与 `optim_step`、`zero_grads` 之间夹着 `pre_optim_step` 回调钩子；epoch 开始处（L1688）还有一次提前的 `zero_grads()`。
4. **预期结果**：你得到的调用顺序应为 `_attach_trainer → pre_train → zero_grads（epoch 开头）→ [train_batch → 回调 pre_optim_step → optim_step → zero_grads] × N 步`，与 4.1.2 的流程图一致。本实践纯读码，无需运行（已对照源码确认）。

#### 4.1.5 小练习与答案

**练习 1**：`eval_batch_spec` 为什么是抽象属性而不是写死的超参？
**答案**：不同训练范式的评估 batch 尺寸语义不同——有的按 token 计，有的按 instance 计；且 batch 大小往往要与训练 micro-batch 大小联动（如 `BasicTrainModule` 直接返回 `rank_microbatch_size`，[train_module.py:L277-L279](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/train_module.py#L277-L279)）。把它交给 train module 自己声明，Trainer 侧的评估器才能正确构造评估批。

**练习 2**：为什么 `state_dict_to_save` 与 `state_dict_to_load` 要拆成两个方法，而不是共用 `state_dict()`？
**答案**：保存与加载的「视图」可能不同。例如 `TransformerTrainModule.state_dict_to_load` 会读 checkpoint 的 metadata，自动适配扁平/非扁平优化器状态格式（[transformer/train_module.py:L296-L351](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L296-L351)）；非严格模式下还会剪掉 checkpoint 里缺失的键。默认实现二者都退化为 `state_dict()`，需要差异化时才覆写。

**练习 3**：`BasicTrainModule.optim_step`（[train_module.py:L390-L400](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/train_module.py#L390-L400)）里，记录 grad norm 时为什么 `reduce_type=None`？
**答案**：`nn.utils.clip_grad_norm_` 返回的是**已经跨 rank 归约过**的全局范数；若再让 Trainer 做 `ReduceType.mean` 归约，等于把同一个数取平均（多卡时数值不变但浪费一次通信，且语义错误）。`reduce_type=None` 表示「这个值已经是全局值，直接记录」。

### 4.2 构建：TransformerTrainModuleConfig、`__init__` 与 parallelize_model

#### 4.2.1 概念说明

`TransformerTrainModuleConfig` 是 `TrainModuleConfig` 的主力实现，字段分四组：优化器（`optim`、`max_grad_norm`、`scheduler`）、模型工程（`compile_model`、`float8_config` 与 **dp/tp/cp/ep/pp/ac 六个并行配置**）、损失（`z_loss_multiplier`）、checkpoint 与杂项。它的 `build(model, device, eval_only)` 接收一个已经实例化的 `Transformer`，返回可训练的 train module。

**为什么并行配置挂在这里而不是 `TransformerConfig`？** 三个理由：

1. **时序**：并行化必须发生在「模型建好之后、优化器建好之前」。套上 FSDP/TP 之后，参数的物理形态从普通 `Tensor` 变成 `DTensor`（分布在多个 rank 上的分片），优化器必须看到**最终形态**的参数才能正确分组建状态。
2. **归属**：world mesh（进程组拓扑）取决于**运行时的 world size 与启动方式**（几个进程、几张卡），这是集群环境属性，不是模型结构属性——放在模型「图纸」里不合适。
3. **消费方**：Trainer 与 data loader 需要知道数据并行进程组（按 DP rank 切数据），而 `dp_process_group` 正是由 train module 从 world mesh 中提炼出来交给 Trainer 的。

一句话：**train module 是「模型 × 优化器 × 分布式环境」三者的汇聚点**，并行配置放这里，改一个地方就能同时影响三方。

#### 4.2.2 核心流程

`TransformerTrainModuleConfig.build()` → `TransformerTrainModule.__init__()` 的完整流程：

```text
build(model, device, eval_only)
  ├─ as_dict(exclude_none=True, recurse=False)   # 展平成 kwargs（子 Config 保持原样）
  ├─ DType → torch.dtype；state_dict_opts dict → StateDictOptions
  └─ pp_config 非空？ ──► TransformerPipelineTrainModule（流水线版，本讲不展开）
                        └─► 否则 TransformerTrainModule
__init__:
  ① 校验 rank_microbatch_size % max_sequence_length == 0
  ② is_distributed()？ ──► build_world_mesh(dp, tp, cp, ep)   # 一次性建全局进程拓扑
        否且传了任何并行配置 ──► 直接报错（并行配置仅对分布式训练合法）
  ③ parallelize_model(model, ...)     # 套并行包装 + 初始化权重（见下）
  ④ optim.build(self.model, strict=True)   # 在最终形态的模型上建优化器
     （eval_only=True 时跳过 ④，train_batch 之外的训练方法不可用）
```

`parallelize_model()`（[common.py:L34-L158](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/common.py#L34-L158)）的包装顺序是固定的：

```text
PP 切分 → float8 替换 → CP → TP → EP → 激活检查点 → torch.compile → DP 包装(FSDP/DDP) → init_weights
```

这个顺序的逻辑是「先切结构、再换精度、最后包数据并行」：PP/TP/EP 改变模型的模块组成与参数布局，必须最先做；激活检查点包裹的是模块；compile 要在包装链定型后做；而 FSDP/DDP 是最外层的运行时包装。**权重初始化放在最后一步**——此时分片/复制关系已确定，`init_weights(world_mesh=...)` 能保证各 rank 的 DTensor 分片与单机初始化结果一致（u3-l6 讲过 `_apply_init` + `distribute_like` 机制）。

#### 4.2.3 源码精读

**① 配置字段全家福。**
[transformer/config.py:L397-L430](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/config.py#L397-L430)：两个必填尺寸字段 `rank_microbatch_size`（**以 token 计**，不是以样本计）与 `max_sequence_length`，加上 `optim: OptimConfig` 这个多态字段。六个并行字段全部默认 `None`（不启用）。真实用法示例见 [examples/llm/train.py:L178-L193](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/examples/llm/train.py#L178-L193)：`rank_microbatch_size=16*1024` 配 FSDP 的 `dp_config`、`max_grad_norm=1.0` 与 `CosWithWarmup` 调度器。

**② build() 的展平与分发。**
[transformer/config.py:L432-L469](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/config.py#L432-L469)：`as_dict(exclude_none=True, recurse=False)` 把本层字段变成 kwargs（`recurse=False` 保证嵌套的 `dp_config` 等仍以 Config 对象传递），`DType.as_pt()` 把库内枚举转成 `torch.dtype`，最后在 L456-L468 按 `pp_config` 是否存在分发到 `TransformerPipelineTrainModule` 或 `TransformerTrainModule`——**配置类相同，实现类由并行形态决定**。

**③ `__init__` 的四步。**
[transformer/train_module.py:L164-L169](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L164-L169) 校验 micro-batch 尺寸能被序列长度整除；[transformer/train_module.py:L171-L187](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L171-L187) 在分布式时调用 `build_world_mesh` 建全局拓扑，**非分布式却传了并行配置则立即报错**（这就是 4.2.2 流程图的分支②）；[transformer/train_module.py:L198-L212](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L198-L212) 调 `parallelize_model`；[transformer/train_module.py:L235-L242](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L235-L242) 建优化器，`eval_only=True` 时跳过。之后 [transformer/train_module.py:L244-L246](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L244-L246) 把 DP 进程组从 world mesh 中取出来暴露给 Trainer。

**④ parallelize_model 的关键段。**
[common.py:L115-L145](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/common.py#L115-L145)：DP 包装按 `DataParallelType.fsdp/hsdp` 走 `apply_fsdp`、`ddp` 走 `apply_ddp`（MoE 模型还需先 `prepare_experts_for_fsdp/ddp`）。[common.py:L147-L156](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/common.py#L147-L156)：最后调用每个模型分片的 `init_weights(max_seq_len=..., world_mesh=..., model_part_idx=...)` 完成确定性初始化。注意 u3-l6 讲过：`TransformerConfig.build()` 只分配参数不填权重，**真正填权重的动作就发生在这里**——这是「build 模型」与「可训练」之间缺失的一环。

**⑤ 迟来的校验：pre_train。**
[transformer/train_module.py:L280-L289](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L280-L289) 校验 `global_batch_size % (rank_microbatch_size × DP world size) == 0`。注释特意说明为什么不在 `on_attach` 里做：`BatchSizeScheduler` 之类的回调可能在模块挂载**之后**修改全局 batch size，所以校验必须推迟到 `pre_train()`（训练真正开始前）。

#### 4.2.4 代码实践

1. **实践目标**：直观感受「Config 字段 → `__init__` kwargs」的展平机制，以及 `pp_config` 的分发逻辑。
2. **操作步骤**（示例代码，可直接以 `python` 运行，纯 CPU、无需数据）：

```python
# 示例代码：inspect_build_kwargs.py
from olmo_core.optim import AdamWConfig
from olmo_core.distributed.parallel import DataParallelType
from olmo_core.train.train_module import (
    TransformerDataParallelConfig,
    TransformerTrainModuleConfig,
)

cfg = TransformerTrainModuleConfig(
    rank_microbatch_size=128,
    max_sequence_length=128,
    optim=AdamWConfig(lr=1e-3),
    max_grad_norm=1.0,          # 会被传入
    z_loss_multiplier=None,     # 会被 exclude_none 剔除
    dp_config=TransformerDataParallelConfig(name=DataParallelType.ddp),
)
kwargs = cfg.as_dict(exclude_none=True, recurse=False)
print(sorted(kwargs.keys()))    # 观察：没有 z_loss_multiplier
print(type(kwargs["dp_config"]))  # <class '...TransformerDataParallelConfig'>，仍是 Config 对象
print(type(kwargs["optim"]))      # AdamWConfig，多态身份保留
```

3. **需要观察的现象**：值为 `None` 的字段被剔除；嵌套 Config 因 `recurse=False` 保持对象身份不被降级为 dict。
4. **预期结果**：键列表包含 `rank_microbatch_size、max_sequence_length、optim、max_grad_norm、dp_config`，不含 `z_loss_multiplier`。这正是 `build()` 在 [transformer/config.py:L448](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/config.py#L448) 拿到的那份 kwargs。（逻辑已对照源码；运行输出待本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：在单进程（没起 torchrun、未初始化进程组）的机器上给 `dp_config` 赋值会发生什么？
**答案**：构建时直接抛 `OLMoConfigurationError: Training parallelism configs are only valid for distributed training`（[transformer/train_module.py:L179-L187](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L179-L187)）。想要「单卡 DP」，需用 `torchrun --nproc-per-node=1` 启动使进程组初始化（world size 为 1 的 DDP/FSDP 包装是合法的）。

**练习 2**：`TransformerDataParallelConfig` 相比基类 `DataParallelConfig` 多了哪些 Transformer 特有字段？
**答案**：见 [transformer/config.py:L227-L268](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/config.py#L227-L268)：`wrapping_strategy`（FSDP 包裹策略）、`prefetch_factor`、`only_allreduce_last_microbatch`、`reduce_grads_in_fp32`、`accumulate_grads_in_fp32`、`bucket_cap_mb`、`use_reduce_scatter`。它们全部服务于「梯度如何同步/累加」，再次印证数据并行的实现细节归属 train module。

**练习 3**：为什么 `parallelize_model` 把 `init_weights` 放在所有并行包装之后，而不是在 `TransformerConfig.build()` 里就填好权重？
**答案**：因为 FSDP/TP 等包装会改变参数的存放方式（分片成 DTensor）。先初始化再包装，需要处理「完整张量初始化后如何切到各 rank」的复杂搬运；先包装再初始化，`init_weights` 可以带着 `world_mesh` 信息直接按「单机语义生成 + 分布式落位」执行（u3-l6 的 `_apply_init` + `distribute_like`），保证与单机初始化逐位一致，也是 pipeline 并行下各 stage 各自初始化（`model_part_idx` 偏移 seed）的前提。

### 4.3 一次训练步的代码路径：train_batch → optim_step → zero_grads

#### 4.3.1 概念说明

这是本讲的主干。`TransformerTrainModule` 的 `train_batch` 比照 `BasicTrainModule` 多做四件事：

1. **caller-normalized 损失**：把 `loss_div_factor=batch_num_tokens_for_loss`（整个 batch 的有效 token 数）传给模型，模型内部用 `sum` 归约再除以它——损失与 micro-batch 划分方式解耦。
2. **micro-batch 上下文管理**：DDP/FSDP 的梯度同步只在最后一个 micro-batch 做，中间步骤跳过通信（梯度累积的正确姿势）。
3. **指标记录**：masked label 比例、CE loss、Z-loss、模型自身的辅助指标（如 MoE 负载均衡损失，u3-l5）。
4. **SkipStepOptimizer 的即时归约**：loss spike 检测器（u5-l3）在 `optim_step` 之前就需要全局 loss，因此走一条「立刻 all-reduce」的特殊路径。

`optim_step` 则是三段式：**裁剪梯度 → 调度学习率 → `optim.step()`**，外加把 `latest_grad_norm` 递给 SkipStep 优化器、调用模型的 `post_optim_step()` 钩子。

#### 4.3.2 核心流程

`train_batch(batch)` 的执行过程（对应源码 L414-L537）：

```text
1. _set_model_mode("train")              # 幂等：模式没变就不重设
2. 生成 labels（若 batch 没有）：input_ids 左移一位；
   label_mask/attention_mask/instance_mask 为 0 的位置填 ignore_index(-100)
3. 统计 batch_num_tokens_for_loss = (labels != -100).sum()   # 全 batch 有效 token 数 N
   （instance_mask 被剔除的整条样本，其 token 数会被加回 N，见源码 WARN 注释）
4. 按 token 预算切块：每个 micro-batch 含 rank_microbatch_size // seq_len 条样本
5. for 每个 micro-batch:
     a. 进入 _train_microbatch_context:  DDP→非最后一个 micro-batch 用 no_sync()；
                                          FSDP→set_is_last_backward；HSDP→set_requires_all_reduce
     b. _prepare_batch: 拆出 input_ids / labels / 其余 model_kwargs
     c. model_forward(..., loss_reduction="sum", loss_div_factor=N, z_loss_multiplier=...)
        （autocast 精度在此处生效）
     d. ce_batch_loss += ce_loss.detach()     # 累计指标（不带梯度）
     e. loss.backward()                        # 梯度累加进 param.grad
6. model.post_batch()；若 dry_run：重置辅助指标后直接返回（不记指标、不留给优化器脏状态）
7. 记录指标：CE loss（SkipStep 优化器需先立即 all-reduce）、Z-loss、模型辅助指标
```

`optim_step()`（L583-L606）：**可选裁剪 → 逐参数组 `scheduler.set_lr` → `optim.step()` → 记录是否跳步 → `model.post_optim_step()`**；`zero_grads()`（L608-L610）就是 `optim.zero_grad(set_to_none=True)`。

**梯度累积的数学**。设 batch 内有效 token 共 \( N \) 个，被切成 micro-batches \( B_1,\dots,B_k \)。第 \( i \) 个 micro-batch 上计算

\[
\ell_i = \frac{1}{N}\sum_{t \in B_i} \text{CE}_t, \qquad
\mathcal{L} = \sum_{i=1}^{k} \ell_i = \frac{1}{N}\sum_{t=1}^{N} \text{CE}_t
\]

反向传播把各 \( \nabla \ell_i \) 累加进 `.grad`，最终梯度 \( \nabla \mathcal{L} \) 恰是**整个 batch 一次前向的平均交叉熵梯度**，与 \( k \) 的取值无关。这就是「sum 归约 + 除以全局 N」的意义；若每个 micro-batch 各自 `mean` 再相加，\( \mathcal{L}=\sum_i \frac{1}{|B_i|}\sum_{t\in B_i}\text{CE}_t \) 会给小 micro-batch 里的 token 更大的权重，梯度就被切分方式带偏了。

#### 4.3.3 源码精读

**① labels 与有效 token 统计。**
[transformer/train_module.py:L414-L427](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L414-L427)：`get_labels`（实现见 [data/utils.py:L613-L625](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/utils.py#L613-L625)）把 `input_ids` 复制为 labels 并按各掩码填 `ignore_index`；`batch_num_tokens_for_loss` 就是上文的 \( N \)。随后的 [L429-L449](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L429-L449) 记录 masked 指标，其中对 `instance_mask` 的 WARN 注释值得细读：被整条剔除的样本其 token 仍计入 \( N \)（按零损失计），以免分布式下各 rank 对 loss 的贡献失衡。

**② 切块与 micro-batch 循环。**
[transformer/train_module.py:L457-L462](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L457-L462)：`split_batch(batch, rank_microbatch_size // seq_len)`——token 预算除以序列长度得到每个 micro-batch 的**样本条数**。循环体 [L466-L491](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L466-L491) 依次做：进入 micro-batch 上下文 → `_prepare_batch` 拆参 → `model_forward` 拿到 `(logits 省略, loss, ce_loss, z_loss)` → 累计 detach 后的指标 → `loss.backward()`。`model_forward`（[L612-L619](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L612-L619)）本身只包一层 autocast 上下文（[L663-L668](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L663-L668)，`autocast_precision` 设了才启用 AMP），把 loss 计算下沉到模型的 `LMHead`（u3-l4），甚至可用 fused kernel 免物化 logits。

**③ micro-batch 上下文：梯度同步的开关。**
[transformer/train_module.py:L636-L655](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L636-L655)：FSDP 模型在每个 micro-batch 开始前声明 `set_is_last_backward(is_last_mb)`（最后一个 micro-batch 时才等待挂起的梯度归约并清理 backward 预取结构）；HSDP 额外 `set_requires_all_reduce(is_last_mb)` 把 all-reduce 推迟到最后一刻；DDP 则对非最后 micro-batch 进入 `model.no_sync()`——否则 DDP 默认每次 backward 都 all-reduce，梯度累积会做 \( k \) 次冗余通信且结果仍正确、但白白等待。**这是「梯度累积 × 数据并行」正确又高效的关键一段。**

**④ 指标记录与 SkipStep 特例。**
[transformer/train_module.py:L501-L512](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L501-L512)：普通优化器路径把 `ce_batch_loss` 交给 `record_ce_loss(..., ReduceType.mean)`（Trainer 攒批异步归约，u6-l1）；若是 `SkipStepOptimizer`，则当场 all-reduce 出全局 loss 并塞给 `self.optim.latest_loss`——因为 spike 检测（u5-l3）在 `optim_step` 一开始就要用窗口内的 loss 序列，等不得异步归约。随后的 [L513-L537](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L513-L537) 记录 Z-loss（含未缩放版）和 `model.compute_auxiliary_metrics(reset=True)` 吐出的模型辅助指标。

**⑤ optim_step 与 _clip_grad_norm。**
[transformer/train_module.py:L583-L606](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L583-L606)：先按 `max_grad_norm` 裁剪并记录全局范数（顺手喂给 SkipStep 的 `latest_grad_norm`）；再对**每个参数组**调 `scheduler.set_lr(group, trainer)`（u5-l2 讲过 LR 总是从 `initial_lr` 重算）；然后 `optim.step()`；最后 `model.post_optim_step()` 给模型一个「优化器刚更新完」的钩子。裁剪实现 [L683-L710](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L683-L710) 有个分布式细节：非 PP 并行下 `get_total_norm` 返回的是 `DTensor`，必须 `full_tensor()` 归约成局部张量，`.item()` 才能拿到正确数值。

**⑥ Trainer 侧的调用点。**
[trainer.py:L1711-L1724](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1711-L1724)：`train_batch(batch)` → `pre_optim_step` 回调 → `optim_step()` → `zero_grads()`，与 4.3.1 的顺序一一对应；[trainer.py:L1660-L1668](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1660-L1668) 的 dry-run 批用 `train_batch(batch, dry_run=True)` 在真数据到来前预热编译/分配器。

#### 4.3.4 代码实践（源码阅读 + 跟踪型）

1. **实践目标**：不看讲义，凭源码复述一个 micro-batch 内 5 个动作的先后顺序，并指出梯度同步发生（或被跳过）的位置。
2. **操作步骤**：打开 [transformer/train_module.py:L466-L491](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L466-L491)，给每一行标注「计算 / 通信 / 指标 / 控制」四类之一（写在草稿上即可，不要改仓库源码）。
3. **需要观察的现象**：循环体内**没有任何显式的 `dist.all_reduce`/`backward 通信**——DDP/FSDP 的梯度同步藏在 `loss.backward()` 触发的 hook 里，由 ③ 的上下文管理器决定本次 backward 是否同步。
4. **预期结果**：5 个动作为「进上下文（控制）→ 拆 batch（控制）→ 前向+算损失（计算）→ 累计指标（指标）→ backward（计算，隐含通信）」；非最后 micro-batch 的隐含通信被 `no_sync()`/`set_is_last_backward(False)` 关闭。（已对照源码确认。）

#### 4.3.5 小练习与答案

**练习 1**：若把 `train_batch` 里的 `loss_reduction="sum"` 改成 `"mean"` 且去掉 `loss_div_factor`，在 `rank_microbatch_size` 小于 rank batch size（即发生梯度累积）时会发生什么？
**答案**：每个 micro-batch 得到各自 token 数的平均损失，反向累加后总梯度 \( \propto \sum_i \frac{1}{|B_i|}\sum_{t\in B_i}\nabla\text{CE}_t \)。当各 micro-batch 大小不均（如最后一个剩余样本较少）时，小 micro-batch 的 token 权重被放大，梯度随切分方式变化，且与单次大 batch 的结果不再一致——破坏了梯度累积的等价性（见 4.3.2 的公式）。

**练习 2**：`dry_run=True` 时 `train_batch` 做了前向反向却「不算数」，源码里靠哪两行保证它不给后续步骤留下脏状态？
**答案**：[transformer/train_module.py:L497-L499](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L497-L499)：调用 `self.model.reset_auxiliary_metrics()` 清掉本批累积的模型辅助指标后直接 `return`——跳过了所有指标记录（所以 loss 不会污染日志）。至于反向产生的梯度，Trainer 在 dry-run 后本来就会走正常步骤清理（且 dry-run 发生在训练循环开始前，epoch 开头的 `zero_grads()`（[trainer.py:L1688](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1688)）兜底）。

**练习 3**：`optim_step` 中为什么把「调度学习率」放在 `optim.step()` 之前、且要对每个 param_group 循环？
**答案**：LR 必须在**本步更新使用前**生效，放在 step 后就晚了一步。逐组循环是因为参数组可以各有超参（u5-l1 的 `OptimGroupOverride`、u10-l7 的 muP 按角色分组都会产生多组），`Scheduler.set_lr(group, trainer)` 按组的 `initial_lr` 重算该组 LR——调度器是纯函数、幂等（u5-l2），逐组调用天然支持按组分派（`PerGroupScheduler`）。

## 5. 综合实践

**任务：脱离 Trainer，手动驱动一个 `TransformerTrainModule` 走完一步训练，验证你理解的调用链与源码一致。**

难点在于 `train_batch` 末尾会通过 `record_metric`/`record_ce_loss` 上报指标，而这两个方法要求 `self.trainer` 已挂载。库自己的测试给出了标准解法——注入一个最小 stub（见 [multimodal_train_module_test.py:L66-L76](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/multimodal_train_module_test.py#L66-L76) 的 `_StubTrainer` 与 L100 的注入方式）。模型配置直接借用 [config_test.py:L130-L137](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/train/train_module/transformer/config_test.py#L130-L137) 的两 层迷你 `llama_like`，CPU 即可跑。

以下为**示例代码**（非仓库文件，保存为 `manual_train_step.py` 用 `python manual_train_step.py` 运行）：

```python
# 示例代码：manual_train_step.py —— 手动执行一次 train_batch → optim_step → zero_grads
import torch
from olmo_core.nn.feed_forward import FeedForwardConfig
from olmo_core.nn.transformer import TransformerConfig
from olmo_core.optim import AdamWConfig
from olmo_core.train.train_module import TransformerTrainModuleConfig

SEQ_LEN = 128  # 等于 rank_microbatch_size → 每个 micro-batch 恰好 1 条样本


class StubTrainer:  # train_batch 只通过这两个方法触碰 trainer
    def __init__(self):
        self.ce_losses = []

    def record_metric(self, *args, **kwargs):
        pass

    def record_ce_loss(self, value, *args, **kwargs):
        self.ce_losses.append(float(value))


torch.manual_seed(0)
model_cfg = TransformerConfig.llama_like(
    d_model=64, vocab_size=128, n_layers=2, n_heads=2,
    feed_forward=FeedForwardConfig(hidden_size=128, bias=False),
)
model = model_cfg.build(init_device="cpu")

tm_cfg = TransformerTrainModuleConfig(  # 不设 dp_config：单进程、非分布式
    rank_microbatch_size=SEQ_LEN,
    max_sequence_length=SEQ_LEN,
    optim=AdamWConfig(lr=1e-3),
)
tm = tm_cfg.build(model, device=torch.device("cpu"))
tm._trainer = StubTrainer()  # 注入 stub（仿照库内测试的做法）

# 1) 构造随机 batch：2 条样本 × 128 token，labels 留空让 train_batch 自己生成
batch = {"input_ids": torch.randint(0, 128, (2, SEQ_LEN))}

# 2) 前向 + 反向
tm.train_batch(batch)
print("CE loss:", tm._trainer.ce_losses[-1])
grads = [p.grad for p in tm.model.parameters() if p.grad is not None]
print("有梯度的参数个数:", len(grads))          # 预期：全部参数
print("梯度范数:", torch.sqrt(sum(g.float().pow(2).sum() for g in grads)).item())

# 3) 优化器步进，检查 AdamW 状态是否生成
tm.optim_step()
some_p = next(iter(tm.optim.state))
print("优化器状态键:", sorted(tm.optim.state[some_p].keys()))  # 预期: ['exp_avg', 'exp_avg_sq', 'step']

# 4) 清梯度，验证 .grad 变回 None
tm.zero_grads()
print("清梯度后非空 .grad 个数:",
      sum(p.grad is not None for p in tm.model.parameters()))  # 预期: 0
```

**操作步骤与观察点**：

1. **运行脚本**，记录四段输出：CE loss 数值、有梯度的参数个数、AdamW 状态键、清梯度后的非空梯度个数。
2. **对照源码**：在 [transformer/train_module.py:L466-L491](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L466-L491) 的循环处数一数——batch 有 2 条样本、`rank_microbatch_size // seq_len = 1`，所以循环应执行 **2 次**（梯度累积 2 步）。可在 `train_batch` 里临时插桩验证（改本地副本，不要动仓库）。
3. **预期结果**：CE loss 为有限正数（约等于随机初始化的 \(\ln 128 \approx 4.85\) 上下）；全部参数拿到梯度；`optim.step()` 后每个参数出现 `step/exp_avg/exp_avg_sq` 三个状态；`zero_grads()` 后 `.grad` 全为 `None`（`set_to_none=True` 的效果）。
4. **进阶 A（梯度累积等价性）**：把 `rank_microbatch_size` 改为 `2 * SEQ_LEN` 重建（其余不变，seed 相同），重新跑 `train_batch` 后对比梯度范数——两次应在浮点误差内一致，这就亲手验证了 4.3.2 的数学。
5. **进阶 B（真正的单卡 DP）**：把 `dp_config=TransformerDataParallelConfig(name=DataParallelType.ddp)` 加进 config，并用 `torchrun --nproc-per-node=1 manual_train_step.py` 启动（world size 1 的 DDP 合法）。观察日志中出现 `Applied DDP to the model...` 与 `Data parallel world size = 1`。此变体依赖分布式环境与后端可用性，**待本地验证**。

## 6. 本讲小结

- `TrainModule` 是 `Trainer` 与「怎么训练」之间的合同：`train_batch`/`eval_batch`/`optim_step`/`zero_grads` 四个计算方法 + state_dict 族 + 评估规格与 FLOPs 统计；`Trainer` 在 [trainer.py:L1711-L1724](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1711-L1724) 按固定节奏调用它们。
- DP/TP/CP/EP/PP/AC 六套并行配置挂在 `TransformerTrainModuleConfig` 上，因为并行化必须发生在「模型建好后、优化器建好前」：它改变参数的物理形态（DTensor 分片），且依赖运行时 world size；数据并行进程组也由 train module 提供给 Trainer 切数据。
- `build()` → `__init__` 四步：校验尺寸 → `build_world_mesh` → `parallelize_model`（固定顺序：PP→FP8→CP→TP→EP→AC→compile→DP，**最后** `init_weights`）→ `optim.build`；`eval_only=True` 跳过优化器。
- `train_batch` 的骨架：生成 labels → 统计全局有效 token 数 \( N \) → 按 token 预算切 micro-batch → 逐个「前向（sum 损失 ÷ N）→ backward」→ 记录指标；`sum + loss_div_factor=N` 保证梯度与 micro-batch 划分无关。
- 梯度同步的开关在 `_train_microbatch_context`：DDP 用 `no_sync()`、FSDP 用 `set_is_last_backward`、HSDP 用 `set_requires_all_reduce`，都只让**最后一个** micro-batch 触发同步。
- `optim_step` 三段式：裁剪（DTensor 需 `full_tensor()` 归约）→ 逐参数组 `scheduler.set_lr` → `optim.step()`；`SkipStepOptimizer` 用户走「loss 即时 all-reduce」的特例路径。

## 7. 下一步学习建议

- **u6-l3（回调系统）**：本讲多次看到 `pre_optim_step`、`post_train_batch` 等钩子插在 train_module 调用之间，下一讲系统讲 `Callback` 体系与自定义回调。
- **u7-l1（数据并行）**：本讲只说清了 FSDP/DDP 包装「在哪里套上」，分片策略、梯度归约细节与 HSDP 的取舍在下一单元展开。
- **u7-l2（TP/PP/CP/EP）**：`pp_config` 分发出的 `TransformerPipelineTrainModule`、`parallelize_model` 里 CP/TP/EP 各步的内部机制。
- **延伸阅读**：`transformer/objective.py`（`train_batch_with_loss` 的自定义目标接口）与 `ddp_train_module.py`（融合 MoE 优化器的 `OLMoDDPTrainModule`），它们是本讲两个「兄弟实现」，适合对照阅读加深对抽象的理解。

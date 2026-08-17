# 检查点管理：保存、step_latest 原子符号链接与断点续训

## 1. 本讲目标

学完本讲，你应该能够：

1. 追踪一次 `save_checkpoint` 调用的完整执行过程，说出最终 checkpoint 目录里**每一个文件的生成者**。
2. 解释 FSDP state dict 的 **gathered（FULL_STATE_DICT）** 与 **sharded（SHARDED_STATE_DICT）** 两种形态，以及 DeepSpec 为何选择前者。
3. 说明 `step_latest` 符号链接的**发现**与**原子替换**（`safe_symlink`）流程，理解为什么「先 unlink 再建链」的旧实现会导致静默从头训练。
4. 解释训练配置如何随 checkpoint 回写为 `train_config.py`（含 `--opts` 追加赋值行），以及它如何保证断点续训拿到与原运行完全一致的配置。
5. 对照 `discover_latest_checkpoint` 与 `load_training_state`，完整描述从 `step_latest` 恢复时，除了 `next_micro_step` 之外还恢复了哪些状态。

本讲承接 u3-l2 的核心结论：**`next_micro_step` 是训练进度的唯一真相源**，`global_step`、epoch、数据采样偏移都是它的派生量。检查点系统正是围绕这个设计展开的——存盘与恢复都在「同步微批边界」上发生，恢复时只需要一个整数就能推回全部进度。

## 2. 前置知识

### 2.1 符号链接（symlink）与原子替换

符号链接是文件系统里的「指针文件」：`step_latest -> /path/to/step_3000`。读取 `step_latest` 时，操作系统会自动跳转到它指向的目录。

「原子替换」指指针的切换在文件系统层面**一步完成、不存在中间状态**。POSIX 的 `rename(2)`（Python 里的 `os.replace`）保证：目标路径要么是旧内容、要么是新内容，观察者永远不会看到「不存在」或「半个」。对比之下，「先 `unlink` 旧链接、再 `symlink_to` 新链接」是两步操作，两步之间如果进程崩溃，指针就凭空消失——这正是本仓库曾经踩过的坑（见 4.2.3 节）。

### 2.2 FSDP state dict 的两种形态

FSDP（Fully ShardedDataParallel）包装模型后，直接调用 `model.state_dict()` 拿到的语义取决于 `FSDP.state_dict_type` 上下文：

| 形态 | StateDictType | 内容 | 适用场景 |
| --- | --- | --- | --- |
| **gathered（完整）** | `FULL_STATE_DICT` | 所有 rank 参与一次 all-gather，rank 0（或全部 rank）拿到**完整**的、key 与原模型一致的权重字典 | 想用 HuggingFace `save_pretrained` / `from_pretrained` 直接读写 checkpoint |
| **sharded（分片）** | `SHARDED_STATE_DICT` | 每个 rank 只保存自己持有的参数分片，写入各自文件 | 参数量大到单卡放不下完整副本时，省内存省通信 |

DeepSpec 的选择是 **gathered**：checkpoint 必须能被 `transformers` 的 `from_pretrained` 直接加载——评估侧的 `eval.py`、续训侧的 `load_resume_draft_model` 都走 HF 标准接口。为控制峰值内存，gather 时配置了 `offload_to_cpu=True`（权重直接落到 CPU 内存而非挤占 GPU）。

### 2.3 你已经知道的事（承接前几讲）

- u3-l1：`BaseTrainer.__init__` 的装配顺序是「分布式初始化 → 断点发现 → 模型构建 → 权重恢复 → compile → FSDP → ……」；草稿模型的 embedding/lm_head 冻结复用自目标模型。
- u3-l2：主循环用 `should_sync = (next_micro_step + 1) % G == 0` 区分累积微批与同步微批，`optimizer.step()` 只在同步微批后执行。
- u1-l4：配置是 Python 文件，`--opts "train.lr=6.0e-4"` 这类点路径覆盖由 `parse_opts_to_config` 实现，值经 `yaml.safe_load` 做标量类型推断。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [deepspec/trainer/ckpt_manager.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/ckpt_manager.py) | 检查点的**唯一权威实现**：发现、保存、恢复、配置回写全部在此 |
| [deepspec/utils/io.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/io.py) | 两个文件系统工具：`ensure_dir` 与原子符号链接 `safe_symlink`（17 行小文件） |
| [deepspec/trainer/base_trainer.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py) | 检查点的**调用方**：`__init__` 里的断点发现与恢复、`train()` 里的三个存盘触发点 |
| [train.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/train.py) | 注入 `_origin_config_path` / `_origin_opts` 两个回写原料的入口 |
| [config/dspark/dspark_qwen3_4b.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py) | 配置样例：`checkpointing_steps` 与 `finalize_cfg` 派生 checkpoint 根目录 |

## 4. 核心概念与源码讲解

### 4.1 save_checkpoint 与恢复调用链：一次检查点的完整生命周期

#### 4.1.1 概念说明

一次多卡训练的检查点要回答三个问题：

1. **模型权重存成什么格式？** —— 存成 HF 标准 checkpoint（`config.json` + 权重文件），这样评估、续训、发布共享同一份产物。
2. **训练进度存到哪一步？** —— 只存 `next_micro_step` 这一个整数（u3-l2 的「唯一真相源」），其余进度量全部现推。
3. **优化器和随机数状态呢？** —— 每个 rank 各存一份 `training_state.rank{r}.pt`，因为每个 rank 的优化器状态（fp32 主权重、AdamW 动量）和 RNG 状态都是本卡私有的。

约束条件是：**存盘只能发生在同步微批边界**。因为只有在 `optimizer.step()` 之后，模型权重与优化器状态才是互相一致的一对——如果在一个梯度累积周期的中间存盘，权重是上一步的、累积中的梯度却没存，恢复后这半个周期的梯度就永远丢了。代码用断言强制了这一点。

#### 4.1.2 核心流程

`save_checkpoint` 的四阶段流水（四个 `dist.barrier()` 把各 rank 的动作排成全序）：

```text
前置断言: next_micro_step % G == 0        （G = gradient_accumulation_steps）
推 导:     global_step = next_micro_step // G
目录名:     <checkpoint_dir_root>/step_{global_step}

阶段 ①  rank 0: ensure_dir(step_dir) + save_train_config(...)   → 写 train_config.py
         dist.barrier()                                          → 目录对全体可见
阶段 ②  全体 rank: _save_model_checkpoint(...)                   → rank 0 写 HF 权重
         各 rank: torch.save(training_state, training_state.rank{r}.pt)
         dist.barrier()                                          → 所有文件已落盘
阶段 ③  rank 0: safe_symlink(step_dir, root/step_latest)         → 翻指针（最后一步！）
         dist.barrier()                                          → 全员一起返回
```

关键不变量：**`step_latest` 只会在所有 rank 的所有文件完整落盘之后才被翻转**。所以指向它的目录永远是完整可用的 checkpoint。

恢复方向的调用链（发生在 `BaseTrainer.__init__` 内，见 u3-l1 的装配顺序）：

```text
__init__
 ├─ discover_latest_checkpoint(root)          → realpath(step_latest) 或 None
 ├─ build_models()                            → 全新草稿模型（冻结核来自目标模型）
 ├─ load_resume_draft_model(...)              → from_pretrained 覆盖为存盘权重
 ├─ torch.compile + FSDP 包装
 ├─ BF16Optimizer(...)
 └─ load_training_state(...)                  → 优化器状态 + RNG + next_micro_step
train()
 └─ _build_train_dataloader(start_offset_samples = next_micro_step × local_batch_size)
                                             → 采样器从断点偏移继续（u3-l3 的无状态采样器）
```

#### 4.1.3 源码精读

**存盘入口：只在同步边界存活。** 断言与目录命名：

- [deepspec/trainer/ckpt_manager.py:149-155](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/ckpt_manager.py#L149-L155)：先断言 \( \text{next\_micro\_step} \bmod G = 0 \)，再由 \( \text{global\_step} = \lfloor \text{next\_micro\_step} / G \rfloor \) 得到目录名 `step_{global_step}`。若在累积中间被调用会立刻 `AssertionError`。

- [deepspec/trainer/ckpt_manager.py:156-159](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/ckpt_manager.py#L156-L159)：rank 0 建目录并写 `train_config.py`，随后第一个 `dist.barrier()` 保证其他 rank 进入阶段 ② 时目录已存在。

**模型权重：gathered 形态 + 剥离 compile 前缀。**

- [deepspec/trainer/ckpt_manager.py:222-230](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/ckpt_manager.py#L222-L230)：`_full_model_state_dict` 先断言模型已被 FSDP 包装，然后在 `FSDP.state_dict_type(model, StateDictType.FULL_STATE_DICT, FullStateDictConfig(offload_to_cpu=True, rank0_only=True))` 上下文里取 `state_dict()`。这就是 2.2 节说的 **gathered** 形态：所有 rank 都要调用（all-gather 是集合通信，缺一个就死锁），但配置了 `rank0_only=True` 后只有 rank 0 拿到完整字典，其余 rank 拿到空字典；`offload_to_cpu=True` 让 gather 结果直接落在 CPU 内存。

- [deepspec/trainer/ckpt_manager.py:233-243](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/ckpt_manager.py#L233-L243)：`_save_model_checkpoint` 里只有 rank 0 继续往下走。注意这个循环：

  ```python
  if normalized_key.startswith("_orig_mod."):
      normalized_key = normalized_key[len("_orig_mod."):]
  ```

  `train.torch_compile=True` 时模型被 `torch.compile` 包装，所有参数名都会带上 `_orig_mod.` 前缀；这里把它剥掉，权重 key 才能对上草稿模型原始定义，`from_pretrained` 才能加载。最后 `draft_model.save_pretrained(checkpoint_dir, state_dict=draft_state_dict)` 用**未包装的** `draft_model`（它知道自己的 HF 配置）落盘标准 HF checkpoint（`config.json`、权重文件等）。注意冻结的 embedding/lm_head 也一并存入——`requires_grad=False` 不影响它们出现在 `state_dict()` 里。

**每 rank 训练状态：一个 `torch.save` 装下进度、优化器与四套 RNG。**

- [deepspec/trainer/ckpt_manager.py:195-219](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/ckpt_manager.py#L195-L219)：`_serialize_training_state` 返回的字典包含 `next_micro_step`、`optimizer.state_dict()`（BF16Optimizer 的 fp32 主权重 + AdamW 动量 + 调度器时钟，见 u3-l4）、并行布局三元组（`global_rank`/`world_size`/`local_batch_size`），以及 **四套 RNG 状态**：`torch.get_rng_state()`（CPU）、`torch.cuda.get_rng_state()`（当前 GPU）、`np.random.get_state()`、`random.getstate()`（Python 内建）。恢复它们意味着 dropout 等随机行为也能无缝续接。

- [deepspec/trainer/ckpt_manager.py:188-192](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/ckpt_manager.py#L188-L192)：`_rank_training_state_path` 生成 `training_state.rank{r}.pt`——每个 rank 一份文件，写在同一个 `step_*` 目录里。

- [deepspec/trainer/ckpt_manager.py:173-184](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/ckpt_manager.py#L173-L184)：全体 rank 各自 `torch.save` 后，第二个 `dist.barrier()` 挡住所有人；等所有状态文件都落盘，rank 0 才执行 `safe_symlink` 翻转 `step_latest`（4.2 节详解），最后再一个 barrier 让全员一起返回。

**恢复链路一：模型权重。**

- [deepspec/trainer/base_trainer.py:162-165](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L162-L165)：`__init__` 一开始就把 `resume_checkpoint_dir = discover_latest_checkpoint(self.checkpoint_dir_root)` 算好——续训不是显式参数，而是**自动发现**的：只要配置推出的 checkpoint 根目录里存在 `step_latest`，就续训。

- [deepspec/trainer/base_trainer.py:175-183](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L175-L183)：先用 `build_models()` 造一个全新草稿模型（embedding/lm_head 从目标模型拷贝冻结），再交给 `load_resume_draft_model` 整体替换。

- [deepspec/trainer/ckpt_manager.py:64-81](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/ckpt_manager.py#L64-L81)：`load_resume_draft_model` 用 `type(draft_model).from_pretrained(resume_checkpoint_dir, dtype=..., attn_implementation=...)` 从 checkpoint 重建模型并搬上 GPU。传入的 `draft_model` 只被用来取**类型**和注意力实现配置——函数返回的是一个全新实例。最后 `set_embedding_head_trainable(False)`：`from_pretrained` 只恢复权重，不恢复「冻结」这个运行时属性，所以要重新冻一遍。随后 `torch.compile` 与 FSDP 包装照常作用在这个已恢复权重的模型上。

**恢复链路二：训练状态与三重布局校验。**

- [deepspec/trainer/base_trainer.py:214-231](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L214-L231)：优化器先按全新模型构建，再由 `load_training_state` 用存盘的 state dict 覆盖；返回的 `TrainingResumeState` 只有一个字段 `next_micro_step`，直接赋给 `self.next_micro_step`。

- [deepspec/trainer/ckpt_manager.py:97-117](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/ckpt_manager.py#L97-L117)：`load_training_state` 用 `torch.load(..., weights_only=False)` 读本 rank 的状态文件（RNG 状态等是任意 pickle 对象，无法用 `weights_only=True`），然后依次：恢复优化器状态；断言 `next_micro_step % G == 0`；断言存盘时的 `global_rank`、`world_size`、`local_batch_size` 与当前完全一致；恢复四套 RNG。**三重布局断言**的含义是：续训必须沿用与原运行完全相同的并行布局（卡数、每卡批大小都不能变），否则各 rank 的优化器状态与数据切分就对不上号——这与 u3-l3 讲过的「恢复时强制沿用相同并行布局」是同一约束的代码体现。

- [deepspec/trainer/ckpt_manager.py:119-133](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/ckpt_manager.py#L119-L133)：从 `next_micro_step` 现推 \( \text{global\_step} = \lfloor \text{next\_micro\_step}/G \rfloor \)、\( \text{epoch} = \lfloor \text{next\_micro\_step}/\text{micro\_batches\_per\_epoch} \rfloor + 1 \)（仅用于打印），然后打出关键的提示行：`AUTO-RESUME from ..., to force fresh run change exp_name or remove step_latest`——想强制重新训练，要么换 `exp_name`（让 `finalize_cfg` 推出新的 checkpoint 根目录），要么删掉 `step_latest`。

**恢复链路三：数据位置。**

- [deepspec/trainer/base_trainer.py:360-368](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L360-L368)：`train()` 用 \( \text{offset} = \text{next\_micro\_step} \times \text{local\_batch\_size} \) 换算出本 rank 已消费的样本数，交给 `StatelessResumableDistributedSampler` 的 `start_global_offset_samples`（u3-l3 讲过它是 `(seed, epoch, rank, 偏移)` 的纯函数），从断点无缝续读，不重不漏。

**训练侧的三个存盘触发点。**

- [deepspec/trainer/base_trainer.py:400-401](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L400-L401)：周期存盘。`self.global_step % checkpointing_steps == 0` 时调用 `save_and_eval_checkpoint`。`checkpointing_steps` 来自配置（[config/dspark/dspark_qwen3_4b.py:47-50](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L47-L50) 中为 3000）。注意这段代码位于 `should_sync` 分支内、紧跟 `optimizer.step()` 之后，天然满足「同步边界」断言。
- [deepspec/trainer/base_trainer.py:333-344](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L333-L344)：`save_and_eval_checkpoint` = `save_checkpoint(**self._checkpoint_kwargs())`（[L319-331](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L319-L331) 把模型、优化器、进度、布局参数打包成关键字参数）+ rank 0 顺手提交自动评测（`_launch_eval`，公开环境只打印提示）。
- [deepspec/trainer/base_trainer.py:403-405](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L403-L405) 与 [L346-353](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L346-L353)：抢占存盘。`SuspendController.requested()` 为真时先存盘再自愿挂起（u3-l2 讲过的抢占-恢复闭环的存盘半边）。
- [deepspec/trainer/base_trainer.py:407](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L407)：训练自然结束时最后一次存盘。

#### 4.1.4 代码实践

**实践一（源码阅读型）：追踪一次 `save_checkpoint`，产出 checkpoint 文件清单。**

1. **实践目标**：不运行训练，仅凭源码推断 `~/checkpoints/deepspec/dspark_block7_qwen3_4b/step_3000/` 目录里的全部文件及其生成者。
2. **操作步骤**：
   - 从 [base_trainer.py:400-401](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L400-L401) 进入 `save_and_eval_checkpoint`，再到 [ckpt_manager.py:136-185](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/ckpt_manager.py#L136-L185) 的 `save_checkpoint`，给四个阶段各画一条泳道（rank 0 与其他 rank 分开画）。
   - 对每个写文件语句标注：谁执行、写什么。
3. **需要观察的现象**：四阶段之间 barrier 的位置；`_save_model_checkpoint` 虽然只有 rank 0 落盘，但 `_full_model_state_dict` 由**全体 rank** 调用。
4. **预期结果**（以 8 卡训练为例，参考答案）：

   | 文件 | 生成者 | 内容 |
   | --- | --- | --- |
   | `train_config.py` | rank 0，`save_train_config` | 原配置文件副本 + `--opts` 追加赋值行（见 4.3 节） |
   | `config.json` | rank 0，`draft_model.save_pretrained` | 草稿模型结构配置 |
   | 权重文件（如 `model.safetensors`，大模型可能分片，具体文件名由 transformers 版本决定，待本地验证） | rank 0，`save_pretrained(state_dict=...)` | 剥离 `_orig_mod.` 前缀后的全部权重（含冻结的 embedding/lm_head） |
   | `training_state.rank0.pt` … `training_state.rank7.pt` | 各 rank 自己，`torch.save` | `next_micro_step`、优化器状态、布局三元组、四套 RNG 状态 |

   目录之外，同级还有 `step_latest` 符号链接和历史的 `step_*` 目录（代码不做清理，磁盘会持续增长）。

**实践二（源码阅读型）：从 `step_latest` 恢复时，`next_micro_step` 之外还恢复了什么？**

1. **实践目标**：把恢复链路拆成「模型权重 / 训练状态 / 数据位置」三段，逐项列出。
2. **操作步骤**：对照 [base_trainer.py:163-165](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L163-L165)、[L175-183](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L175-L183)、[L221-231](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L221-L231) 与 [base_trainer.py:365-368](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L365-L368)。
3. **预期结果**（参考答案）：
   - **模型权重**：`from_pretrained` 加载存盘权重（含冻结 embedding/lm_head），再 `set_embedding_head_trainable(False)` 重新冻结；
   - **优化器状态**：fp32 主权重、AdamW 动量、两段式调度器时钟（学习率从断点续接而非重新 warmup，见 u3-l4）；
   - **RNG 状态 ×4**：PyTorch CPU、PyTorch CUDA、NumPy、Python `random`；
   - **并行布局校验**（不是恢复，是检查）：`global_rank`、`world_size`、`local_batch_size` 必须与存盘时一致；
   - **派生量**（不存盘、现场推导）：`global_step`、epoch、以及传给采样器的 `start_offset_samples = next_micro_step × local_batch_size`。

#### 4.1.5 小练习与答案

**练习 1**：如果 `save_checkpoint` 被调用时 `next_micro_step=1201`、`G=8`，会发生什么？为什么这个场景在正常训练中不可能出现？

<details>
<summary>参考答案</summary>

[ckpt_manager.py:149-153](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/ckpt_manager.py#L149-L153) 的断言失败（1201 % 8 = 1 ≠ 0），抛出 `AssertionError`。正常训练中不可能出现，因为 [train()](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L400-L401) 的存盘判断在 `should_sync` 分支内、`optimizer.step()` 之后执行，此时 `next_micro_step` 必然是 G 的倍数。
</details>

**练习 2**：为什么 `training_state` 要每个 rank 存一份，而模型权重只有 rank 0 存一份？

<details>
<summary>参考答案</summary>

模型权重经 `FULL_STATE_DICT` all-gather 后在 rank 0 处是**完整且各 rank 相同**的一份，存一份即可（其他 rank 拿到的是空字典，[ckpt_manager.py:233-235](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/ckpt_manager.py#L233-L235)）。而优化器状态、CUDA RNG 状态是**每 rank 私有**的（即便默认 `no_shard` 下各 rank 参数相同，RNG 消费历史也不同），所以每 rank 各存 `training_state.rank{r}.pt`；恢复时 [load_training_state](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/ckpt_manager.py#L105-L106) 还断言存盘 rank 与当前 rank 一致，防止错位加载。
</details>

**练习 3**：恢复时为什么断言 `world_size` 与 `local_batch_size` 必须与存盘时一致，而不断言 `global_batch_size`？

<details>
<summary>参考答案</summary>

`global_batch_size` 决定的是梯度累积步数 \( G = \text{global\_batch}/(\text{world\_size} \times \text{local\_batch}) \)，它通过配置文件传入；只要 world_size 与 local_batch_size 不变、配置不变，G 自然不变。直接断言布局三元组中「运行时才能确定」的两个量（[ckpt_manager.py:108-112](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/ckpt_manager.py#L108-L112)），等于间接锁死了 G 与数据切分方式；若换了卡数，各 rank 的优化器状态文件与采样器切分（u3-l3 的 rank/rank+world_size 步进）都会对不上，宁可立刻失败。
</details>

### 4.2 step_latest 原子符号链接：发现与替换

#### 4.2.1 概念说明

一个实验目录下会有很多 `step_*` 目录（每 3000 步一个、挂起时一个、结束时一个）。「最新可用的是哪个」这个问题如果靠扫描目录名取最大值来回答，既慢又脆弱（比如某次存盘中断留下了半成品目录）。DeepSpec 的做法是把答案做成**一个原子指针**：`step_latest` 符号链接，指向最近一次完整落盘的 `step_*` 目录。

这个设计把「哪个 checkpoint 最新」从**推导问题**（扫描 + 排序 + 猜测完整性）变成**事实问题**（指针指向谁就是谁），而指针的更新用 `os.replace` 保证原子性——任何时刻读到 `step_latest` 的进程（包括恰好在此刻重启的续训进程），看到的都是一个完整 checkpoint。

#### 4.2.2 核心流程

**发现**（启动时，每个 rank 各自执行一次）：

```text
discover_latest_checkpoint(root):
    latest = root/step_latest
    若 latest 既不是符号链接也不是目录 → 返回 None（从零训练）
    否则 → 返回 os.path.realpath(latest)   （解析为绝对真实路径 step_N）
```

**原子替换**（存盘最后一步，仅 rank 0）：

```text
safe_symlink(src, dst):
    1. tmp = dst + ".tmp"                    （如 step_latest.tmp）
    2. 若 tmp 已存在 → unlink                 （清掉上次崩溃残留）
    3. tmp.symlink_to(realpath(src))          （在旁边把新指针搭好）
    4. os.replace(tmp, dst)                   （一步换掉旧指针，原子）
```

不变量：`dst` 要么指向旧 checkpoint、要么指向新 checkpoint，**永远不存在「不存在」的中间态**。

#### 4.2.3 源码精读

**发现端只有 5 行：**

- [deepspec/trainer/ckpt_manager.py:25-29](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/ckpt_manager.py#L25-L29)：`discover_latest_checkpoint` 检查 `step_latest` 是链接或目录即可（符号链接指向存在的目录时 `isdir` 也为 True），返回 `os.path.realpath` 把链接解析成真实的 `step_N` 绝对路径。返回 `None` 就意味着从零训练——这也是强制重训的方法：**删掉 `step_latest`**。

**替换端是全仓库最小的关键文件：**

- [deepspec/utils/io.py:9-16](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/io.py#L9-L16)：`safe_symlink` 先在目标旁边搭 `step_latest.tmp`，再 `os.replace(tmp_path, dst_path)`。注释写明动机：`os.replace` 是原子的，`dst` 永远不会在更新中途消失。

- 这段代码有一个值得铭记的演化史。提交 `57abb3f`（*Make step_latest symlink update atomic*）之前，旧实现是「先 `unlink` 旧链接、再 `symlink_to` 新链接」；提交信息指出了事故模式：两步之间一旦崩溃，`step_latest` 整个消失，**下一次启动发现不了任何 checkpoint，于是静默地从零开始训练**——数小时的训练进度无声丢失。新实现里，最坏情况是留下一个无害的 `step_latest.tmp` 残留（下次保存时第 13-14 行会清掉它），旧指针安然无恙。

**替换端在保存流程中的位置：**

- [deepspec/trainer/ckpt_manager.py:177-184](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/ckpt_manager.py#L177-L184)：翻转 `step_latest` 位于**第二个 barrier 之后**——所有 rank 的模型文件与训练状态文件都已确认落盘。即使翻完指针进程立刻被 kill，`step_latest` 指向的目录也是完整的；反过来，如果落盘中途被 kill，指针还停在旧目录上，旧 checkpoint 依然可用。这就是 4.1.2 节「关键不变量」的实现细节。

**与配置的联动：**

- [config/dspark/dspark_qwen3_4b.py:60-68](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L60-L68)：`finalize_cfg` 把 `checkpoint_dir` 派生为 `BASE_CKPT_DIR/<project_name>/<exp_name>`。换 `exp_name` 就换了根目录，自然发现不了旧的 `step_latest`——这就是自动续训提示里「change exp_name or remove step_latest」的原理。

#### 4.2.4 代码实践

**动手实践：在 /tmp 里亲手翻两次原子指针，并验证发现逻辑。**（本实践只需 CPU 与已安装的依赖，不需要 GPU 和数据。）

1. **实践目标**：验证 `safe_symlink` 更新过程中 `step_latest` 从不消失，且 `discover_latest_checkpoint` 能正确解析。
2. **操作步骤**：

   ```python
   # demo_symlink.py —— 示例代码（非项目原有文件）
   import os, sys
   sys.path.insert(0, "<仓库根目录>")            # 或先 pip install -r requirements.txt
   from deepspec.utils.io import safe_symlink
   from deepspec.trainer.ckpt_manager import discover_latest_checkpoint

   root = "/tmp/symlink_demo"
   for step in (10, 20):
       os.makedirs(f"{root}/step_{step}", exist_ok=True)
       safe_symlink(f"{root}/step_{step}", f"{root}/step_latest")
       print("now ->", os.path.realpath(f"{root}/step_latest"))
   print("discover ->", discover_latest_checkpoint(root))
   print("tmp leftover ->", os.path.exists(f"{root}/step_latest.tmp"))
   ```

   运行：`python demo_symlink.py`，再执行 `ls -la /tmp/symlink_demo/ | grep step_latest` 观察链接本身。
3. **需要观察的现象**：第二次 `safe_symlink` 后指针从 `step_10` 变为 `step_20`；全程 `step_latest` 一直存在；结束时没有 `.tmp` 残留；`ls -la` 显示 `step_latest -> /tmp/symlink_demo/step_20`。
4. **预期结果**：输出 `now -> /tmp/symlink_demo/step_10`、`now -> /tmp/symlink_demo/step_20`、`discover -> /tmp/symlink_demo/step_20`、`tmp leftover -> False`。若依赖未装或路径有出入，请以实际输出为准（待本地验证）。
5. **延伸思考**：在纸面上模拟「在 `symlink_to` 与 `os.replace` 之间 kill 进程」——残留的是 `step_latest.tmp`，`step_latest` 仍指向 `step_10`，重启后 `discover_latest_checkpoint` 照常工作；再用旧实现（先 unlink 再建链）模拟同样崩溃——`step_latest` 彻底消失，重启后静默从零训练。这正是提交 `57abb3f` 修复的场景。

#### 4.2.5 小练习与答案

**练习 1**：为什么不直接删掉旧 `step_*` 目录、只保留最新一个，从而不需要 `step_latest`？

<details>
<summary>参考答案</summary>

保留多个 `step_*` 目录意味着可以回滚到任意历史步（比如发现第 6000 步之后过拟合，想从 `step_3000` 重新训练），也让「正在写入的目录」与「当前生效的目录」天然分离。`step_latest` 指针的存在让「哪个是最新**完整**的」这个判断变成 O(1) 且无歧义——它只在所有文件落盘后才翻转（[ckpt_manager.py:177-182](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/ckpt_manager.py#L177-L182)）。若靠目录名取最大值，一个写了一半的 `step_9000` 目录会被误认为最新。代价是磁盘持续增长，需要人工清理旧目录（删目录但别删链接指向的那个）。
</details>

**练习 2**：`os.replace(tmp_path, dst_path)` 为什么是原子的？它和 `shutil.move` 有什么区别？

<details>
<summary>参考答案</summary>

`os.replace` 对应 POSIX 的 `rename(2)` 系统调用，在同一文件系统内由内核保证「目标路径瞬间从旧目录项换成新目录项」，不存在观察者可见的中间窗口（对符号链接同样适用，换的是目录项本身）。`shutil.move` 是「先拷贝、再删源」的组合（跨文件系统时更是完整复制），既有中间窗口又有两倍 I/O。这也是 [io.py:15-16](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/io.py#L15-L16) 特意先 `symlink_to` 到 `.tmp` 再 `os.replace` 的原因——两步都必须在同一文件系统内才能走 `rename` 快路径。
</details>

### 4.3 train_config.py 回写：让检查点自我描述

#### 4.3.1 概念说明

一次训练的有效配置 = 原始配置文件 + 命令行 `--opts` 覆盖。如果 checkpoint 里只存权重不存配置，几个月后你想续训或复现时，必须翻出当时的命令行记录——丢了就永远无法精确复现。

DeepSpec 的解法优雅得近乎朴素：**把「原配置文件全文」原样拷进 checkpoint，再把 `--opts` 逐条翻译成 Python 赋值语句追加到文件末尾**。得到的 `train_config.py` 本身就是一个可执行、可加载（`load_config` 认它）的完整配置文件——续训时直接 `--config <ckpt>/train_config.py` 即可，无需重新拼命令行。

为什么翻译成 `head['a']['b'] = value` 这种字典下标形式，而不是 `head.a.b = value` 属性形式？因为配置里的顶层变量（`train`、`data` 等）在配置文件里是普通 `dict`（见 [config/dspark/dspark_qwen3_4b.py:32-45](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L32-L45)），普通 dict 不支持属性赋值——属性访问语法糖（`cfg.train.lr`）是 `ConfigNode` 在**加载之后**才赋予的，回写必须回到文件层面的原始语义。

#### 4.3.2 核心流程

```text
启动时 (train.py parse_args):
    config = parse_opts_to_config(opts, load_config(--config))   # 应用覆盖
    config._origin_config_path = abspath(--config)               # 记下原料①
    config._origin_opts         = list(opts)                     # 记下原料②

存盘时 (save_train_config, 仅 rank 0):
    拷贝 _origin_config_path → <ckpt>/train_config.py            # 原文照抄
    对每个 opt 渲染一行赋值语句追加到文件末尾:
        "train.lr=6.0e-4"     →  train['lr'] = 0.0006
        "data.max_length=2048" →  data['max_length'] = 2048

续训时:
    python train.py --config <ckpt>/train_config.py
    → load_config 执行文件（原定义 + 追加赋值依次生效）
    → finalize_cfg 用相同 project_name/exp_name 推出相同 checkpoint 根目录
    → step_latest 被发现 → 自动续训
```

保真度的关键：追加行的值与运行时覆盖用的是**同一个** `yaml.safe_load`（[config.py:80-81](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/config.py#L80-L81) 的 `_parse_scalar`），所以回写值与当初运行时的值类型完全一致，不会出现「运行时是 float、回写成 string」的二次偏差。

#### 4.3.3 源码精读

**原料注入：入口处的两行赋值。**

- [train.py:25-27](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/train.py#L25-L27)：`parse_args` 先用 `parse_opts_to_config` 应用覆盖，然后把**原始配置路径**和**原始 opts 列表**作为两个下划线开头的字段挂到 config 对象上。注意挂的是原料而不是结果——回写追求的是「能重新演绎出同样结果的最小信息」。

**回写主体：拷贝 + 追加。**

- [deepspec/trainer/ckpt_manager.py:32-45](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/ckpt_manager.py#L32-L45)：`save_train_config` 只在全局主进程执行。`shutil.copy(train_config._origin_config_path, dest_path)` 把原配置逐字节拷入 checkpoint；若 `_origin_opts` 非空，则打开文件**追加**一段注释 `# --opts overrides applied at save time` 和每条 opt 渲染出的赋值行。没有 `--opts` 时，checkpoint 里的 `train_config.py` 与原配置一模一样。

**单条渲染：五步小函数。**

- [deepspec/trainer/ckpt_manager.py:48-53](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/ckpt_manager.py#L48-L53)：`_render_opt_assignment` 把 `"a.b.c=value"` 翻译成合法 Python 赋值：

  ```python
  key, raw_value = opt.split("=", 1)      # 只切第一个 =，值里可以再含 =
  head, *rest = key.split(".")            # 首段是变量名，其余是嵌套键
  accessors = "".join(f"[{part!r}]" for part in rest)   # ['b']['c']
  value = yaml.safe_load(raw_value)       # 与运行时覆盖完全相同的类型推断
  return f"{head}{accessors} = {value!r}" # train['b']['c'] = 0.0006
  ```

  两个细节：`split("=", 1)` 保证值里出现 `=` 不被误切；`{value!r}` 用 `repr` 序列化，字符串自动带引号、列表/字典渲染成字面量，追加行因此是合法 Python。

  关于类型推断的一个已知特性（u1-l4 提过）：`yaml.safe_load` 遵循 YAML 1.1，**没有小数点的科学计数法不会被识别为 float**——`3e-4` 渲染成 `train['lr'] = '3e-4'`（字符串），而 `6.0e-4` 渲染成 `train['lr'] = 0.0006`（浮点）。回写不会「修正好」这个差异，恰恰相反，它与运行时覆盖的行为严格一致（运行时 `3e-4` 同样进了字符串，由使用处 `float(self.args.train.lr)` 兜底转换，[base_trainer.py:216](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L216)）——这正是「保真」的含义。

**回写的调用位置：**

- [deepspec/trainer/ckpt_manager.py:156-158](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/ckpt_manager.py#L156-L158)：`save_checkpoint` 阶段 ① 中，rank 0 建完目录后立刻调用 `save_train_config`——它是 checkpoint 里**第一个**落盘的文件，比权重更早。

**续训的自洽性：**

- 用 `<ckpt>/train_config.py` 续训时，[train.py:26-27](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/train.py#L26-L27) 会把 `_origin_config_path` 重新指向这个文件、`_origin_opts` 置空。下一次存盘拷贝的就是「已含追加行」的文件、无需再追加——内容语义不变，方案天然幂等。
- 常量名 `TRAIN_CONFIG_FILE_NAME` 定义在 [ckpt_manager.py:22](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/ckpt_manager.py#L22)。

#### 4.3.4 代码实践

**动手实践：亲手渲染一份 `train_config.py` 追加段，并验证它与运行时覆盖等价。**（CPU 即可；若未安装依赖，可先在纸上完成第 3 步推演，再对照第 4 步答案。）

1. **实践目标**：理解 `--opts` → Python 赋值行的翻译规则，以及 YAML 类型推断的边界情况。
2. **操作步骤**：

   ```python
   # demo_render.py —— 示例代码（非项目原有文件）
   import sys
   sys.path.insert(0, "<仓库根目录>")
   from deepspec.trainer.ckpt_manager import _render_opt_assignment

   opts = [
       "train.lr=6.0e-4",
       "data.max_length=2048",
       "train.num_train_epochs=1",
       "model.block_size=7",
   ]
   for opt in opts:
       print(_render_opt_assignment(opt))
   ```

   然后做等价性验证：把渲染出的四行追加到 `config/dspark/dspark_qwen3_4b.py` 的一个副本末尾，分别用 `load_config` 加载「副本」与「原件 + parse_opts_to_config(opts, ...)」（[config.py:84-98](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/config.py#L84-L98)、[L113-131](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/config.py#L113-L131)），对比两边 `train.lr`、`data.max_length` 等字段的值与类型是否逐项相同。
3. **需要观察的现象**：追加行都是合法 Python；int/bool/带小数点的科学计数法渲染成对应字面量；两条路径得到的字段值完全一致。
4. **预期结果**（以 PyYAML 的 YAML 1.1 规则推得，待本地验证）：

   ```python
   train['lr'] = 0.0006          # 6.0e-4 含小数点 → float
   data['max_length'] = 2048     # → int
   train['num_train_epochs'] = 1 # → int
   model['block_size'] = 7       # → int
   ```

   若把 `train.lr` 换成 `3e-4`（无小数点），预期得到 `train['lr'] = '3e-4'`（字符串）——请实际运行确认这一边界行为。
5. **预期结论**：`train_config.py` 的追加行与运行时 `--opts` 覆盖在值和类型上严格等价，这就是「检查点自我描述」的底气。

#### 4.3.5 小练习与答案

**练习 1**：如果 `--opts "data.target_cache_path=/data/cache"`，回写行长什么样？续训时这行为什么重要？

<details>
<summary>参考答案</summary>

渲染为 `data['target_cache_path'] = '/data/cache'`（`repr` 给字符串加了引号，路径中的 `/` 不受影响）。续训时训练数据必须来自与原运行**同一个** target cache（`validate_train_cache` 还会校验缓存与草稿模型的合同，见 u3-l1），这行赋值保证了不重新敲命令行也能拿到正确路径。
</details>

**练习 2**：为什么 `save_train_config` 拷贝「原始配置文件 + 追加行」，而不是把内存中的 config（`ConfigNode`）序列化成 JSON/YAML 存下来？

<details>
<summary>参考答案</summary>

配置里含有**不可序列化的对象**：`trainer_cls` 是一个 Python 类（[config/dspark/dspark_qwen3_4b.py:33](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L33)），`finalize_cfg` 是函数，还有 `BASE_CKPT_DIR` 这类环境相关的导入常量（u1-l4 的「配置即代码」）。拷贝源文件 + 追加赋值保留了全部 Python 语义，续训时 `load_config` 照常执行，`trainer_cls` 原样可用；JSON 序列化要么丢掉这些对象、要么退化成字符串标签。副作用是配置必须保持可 import（依赖与环境路径不变），这对训练仓库是合理假设。
</details>

## 5. 综合实践

**综合实践：拼装一个假想 checkpoint 目录，走通「发现 → 恢复清单 → 强制重训」三连。**

把本讲三个模块串成一次纸面 + 上手的混合演练：

1. **搭骨架**（上手，CPU 即可）：

   ```python
   # demo_ck.py —— 示例代码（非项目原有文件）
   import os, sys
   sys.path.insert(0, "<仓库根目录>")
   from deepspec.utils.io import safe_symlink
   from deepspec.trainer.ckpt_manager import discover_latest_checkpoint, _render_opt_assignment

   root = "/tmp/ck_demo/root/deepspec/exp_a"
   step = f"{root}/step_3000"
   os.makedirs(step, exist_ok=True)
   # 1) 模拟 rank0 与 rank1 的训练状态文件
   open(f"{step}/training_state.rank0.pt", "w").close()
   open(f"{step}/training_state.rank1.pt", "w").close()
   # 2) 模拟 HF 权重
   open(f"{step}/config.json", "w").close()
   # 3) 模拟 train_config.py（原配置 + 追加行）
   with open(f"{step}/train_config.py", "w") as f:
       f.write("train = dict(lr=6.0e-4, local_batch_size=1)\n")
       f.write(_render_opt_assignment("train.num_train_epochs=1") + "\n")
   # 4) 最后翻指针
   safe_symlink(step, f"{root}/step_latest")
   print("resume dir =", discover_latest_checkpoint(f"{root}"))
   ```

2. **验证发现顺序**：故意**先**翻指针**再**创建 `step_6000`（空目录），确认 `discover_latest_checkpoint` 仍返回 `step_3000`——指针说的算，不看目录名。
3. **写恢复清单**：假设训练在 `next_micro_step=24000`、`G=8`、8 卡存盘后崩溃。对照 [ckpt_manager.py:84-133](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/ckpt_manager.py#L84-L133) 写出：恢复后 `global_step` 是多少（答案：3000）？哪些状态被恢复（模型权重、优化器、4 套 RNG、`next_micro_step`）？哪些量被现推（epoch、采样偏移 \( 24000 \times 1 = 24000 \) 个样本）？哪些量被断言校验（rank/world_size/local_batch_size）？
4. **演练强制重训**：删掉 `step_latest` 再跑 `discover_latest_checkpoint`，确认返回 `None`；再用 `exp_name="exp_b"` 生成新根目录，确认同样发现不了。
5. **预期结果**：一次运行打印出 `step_3000`；删链接后打印 `None`。整个演练不需要 GPU 与目标缓存，却把「多卡存盘时序 → 原子指针 → 自动发现 → 布局校验 → 配置回写」完整过了一遍。真实多卡环境下的完整 kill-再续训实验放在 u7-l3 毕业实战中完成。

## 6. 本讲小结

- 一次 `save_checkpoint` 分四阶段推进（建目录写配置 → 全体 gather 权重 + 各 rank 写训练状态 → rank 0 翻指针 → 收尾 barrier），由 barrier 排成全序；**翻转 `step_latest` 永远是最后一步**，保证指针指向的目录必然完整。
- 权重以 FSDP **gathered**（`FULL_STATE_DICT` + `offload_to_cpu` + `rank0_only`）形态由 rank 0 存成标准 HF checkpoint（剥离 `_orig_mod.` 前缀）；每 rank 另存 `training_state.rank{r}.pt`（`next_micro_step` + 优化器状态 + 布局三元组 + 四套 RNG）。
- 存盘只允许发生在同步微批边界（`next_micro_step % G == 0` 的断言），因为只有 `optimizer.step()` 之后权重与优化器才互相一致；训练侧有三个触发点：周期存盘、挂起前存盘、结束存盘。
- 恢复是**自动发现**的：`discover_latest_checkpoint` 解析 `step_latest`；恢复链路 = `from_pretrained` 覆盖权重（再重新冻结 embedding/lm_head）+ `load_training_state` 恢复优化器与 RNG + 采样器按偏移续读；并行布局（rank/world_size/local_batch_size）必须与存盘时完全一致，否则断言失败。
- `safe_symlink` 用「先建 `.tmp` 再 `os.replace`」实现原子换指针，修复了旧实现「先 unlink 再建链」在崩溃窗口下导致**静默从零训练**的事故（提交 `57abb3f`）。
- `train_config.py` 回写 = 原配置文件逐字节拷贝 + `--opts` 逐条翻译成 `head['k'] = value` 赋值行；渲染与运行时覆盖共用同一个 `yaml.safe_load`，类型严格一致（注意 `3e-4` 会保持字符串），续训直接 `--config <ckpt>/train_config.py` 即可精确复现。

## 7. 下一步学习建议

本讲补全了训练框架的最后一块工程拼图。接下来建议：

1. **进入第 4 单元（DSpark 建模）**：从 [u4-l1 DSpark 核心机制](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/common.py) 开始，看 `run_batch` 这个 u3-l1 埋下的钩子到底喂了什么给模型；你会发现本讲的 `training_state` 里保存的优化器正在更新那些参数。
2. **横向巩固分布式与采样**：回读 u3-l3 的 `StatelessResumableDistributedSampler`，把本讲的 `start_offset_samples = next_micro_step × local_batch_size` 与它的 `(seed, epoch, rank, 偏移)` 纯函数拼接成完整的数据恢复视图。
3. **提前预演毕业实战**：u7-l3 的端到端小规模全流程要求你中途 kill 进程验证断点续训——届时本讲第 5 节的恢复清单就是你的验收表。

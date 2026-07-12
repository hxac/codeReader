# TrainerFactory 与训练器体系

## 1. 本讲目标

上一篇之前（u4 系列）我们把数据链路讲到了「数据集编码成 `input_ids`/`labels`，准备喂给训练循环」。但「谁来吃这些数据、怎么算 loss、怎么前向反向」这件事，落在一个叫 **Trainer** 的对象身上。ms-swift 并没有自己从零写训练循环，而是**站在 HuggingFace `transformers.Trainer` 的肩膀上**，通过一套「映射表 + Mixin 继承 + 运行时打补丁」的机制，把它改造成既能跑 SFT、又能跑分类/排序/嵌入、还能跑 RLHF 的统一训练器。

学完本讲你应该能够：

- 说清 `TrainerFactory` 的 `TRAINER_MAPPING` / `TRAINING_ARGS_MAPPING` 两张表是如何根据 `task_type`（或 `rlhf_type`）派发出对应的 Trainer 类与 TrainingArguments 类的，以及「字符串路径 → 动态 import」的技巧。
- 画出 `Seq2SeqTrainer` 的继承链 `SwiftMixin + DataLoaderMixin + HfSeq2SeqTrainer`，理解三个 Mixin/基类各自的职责，以及为什么 `causal_lm` 任务用的是 `Seq2SeqTrainer` 而不是 `Trainer`。
- 说清 `task_type` 这个关键字段「从哪里来」：它在模型注册时写进 `ModelMeta`，加载时流进 `ModelInfo`，参数初始化时回填到 `args.task_type`，最后成为 `TrainerFactory` 派发的依据。
- 理解 `trainers/patcher.py` 在 import 时对 transformers 默认回调的「猴子补丁」，以及它与 TrainerFactory 共同构成的「训练器装配」全貌。

---

## 2. 前置知识

本讲假设你已经掌握（来自前置讲义）：

- **Arguments 参数体系（u2-l1）**：`BaseArguments` 用多继承把 `DataArguments`/`ModelArguments`/`TemplateArguments` 等 mixin 扁平拼成一个大参数对象；`args.task_type` 是 `ModelArguments` 上的一个字段。
- **模型注册与加载（u3-l1）**：`ModelMeta` 是可复用的静态档案（`task_type`/`template`/`model_arch` 等），`ModelInfo` 是一次性的运行时档案，`get_model_processor` 把模型 id 变成 `(model, processor)` 并把 `model_meta`/`model_info` 挂回 model 对象。
- **Template 体系（u3-l3）**：`template.encode` 产出 `{input_ids, labels, loss_scale}`，`loss_scale` 决定哪些 token 参与 loss（非回答段为 0）。
- **HuggingFace Trainer 基本概念**：`transformers.Trainer` 是一个封装了「训练循环 + 优化器 + 调度器 + checkpoint + 日志」的「大管家」，子类通过覆盖 `compute_loss` / `training_step` / `_prepare_inputs` 等钩子来定制行为。

先建立两个直觉：

1. **Trainer 是「策略对象」，不是「执行者」**。它本身不发明训练算法，而是把「模型 + 数据 + 参数 + loss 函数 + 优化器」这些零件按 HF Trainer 约定的钩子拼装起来。ms-swift 的全部工作，是**为不同任务（生成/分类/排序/嵌入/RLHF）准备不同的策略对象**，并用同一份参数体系驱动它们。
2. **派发的关键是「两张表 + 一个字段」**。`task_type`（或 `rlhf_type`）就是那张「路由键」，`TRAINER_MAPPING` 和 `TRAINING_ARGS_MAPPING` 就是两张「路由表」。理解了这张键 + 两张表，你就理解了 ms-swift 训练器体系的主干。

---

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [swift/trainers/trainer_factory.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/trainer_factory.py) | 定义 `TrainerFactory`：两张映射表 + `get_cls`/`get_trainer_cls`/`get_training_args` 三个派发方法。**本讲的核心。** |
| [swift/trainers/seq2seq_trainer.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/seq2seq_trainer.py) | `causal_lm` 任务实际使用的 `Seq2SeqTrainer`，继承 `SwiftMixin + DataLoaderMixin + HfSeq2SeqTrainer`，覆盖 `compute_loss`/`training_step`/`prediction_step`。 |
| [swift/trainers/mixin.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/mixin.py) | `SwiftMixin`（核心能力混入：保存模型、loss/metric 工厂、梯度检查点、SP/PF 打补丁）与 `DataLoaderMixin`（自定义 train/eval DataLoader）。 |
| [swift/trainers/trainer.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/trainer.py) | `seq_cls`/`embedding`/`reranker` 共用的基类 `Trainer(SwiftMixin, DataLoaderMixin, HfTrainer)`。 |
| [swift/trainers/reranker_trainer.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/reranker_trainer.py) | `RerankerTrainer`，覆盖 `compute_loss` 与 `gather_function`，处理 listwise/generative reranker。 |
| [swift/trainers/embedding_trainer.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/embedding_trainer.py) | `EmbeddingTrainer`，支持 Matryoshka（MRL）多维嵌入损失。 |
| [swift/trainers/patcher.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/patcher.py) | 在 import 时对 transformers 的默认回调做「猴子补丁」，替换进度/日志回调。 |
| [swift/arguments/base_args/model_args.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/model_args.py) | 定义 `task_type` 字段，并在 `_init_model_info` 中把 `model_info.task_type` 回填到 `args.task_type`。 |
| [swift/model/model_meta.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_meta.py) | `get_model_info_meta` 中 `task_type` 的最终决出逻辑（显式 > `num_labels` > 默认 `causal_lm`）。 |
| [swift/pipelines/train/sft.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/sft.py) | `SwiftSft.run` 中调用 `TrainerFactory.get_trainer_cls(args)` 并实例化 Trainer 的那一行——派发的「消费点」。 |

---

## 4. 核心概念与源码讲解

### 4.1 TrainerFactory：用两张表把 task_type 派发成 Trainer

#### 4.1.1 概念说明

`TrainerFactory` 是一个非常薄的「路由器」。它的全部职责只有一件事：**根据当前训练任务的关键字（`task_type` 或 `rlhf_type`），告诉你应该用哪个 Trainer 类、哪套 TrainingArguments 类。**

为什么要做这个路由？因为 ms-swift 要在一个框架里同时支持：

- **生成式微调（SFT/预训练）**：模型逐 token 预测下一个词，loss 是 token 级交叉熵，需要 `Seq2SeqTrainer`（能 `predict_with_generate`）。
- **判别式任务**：序列分类（`seq_cls`）、重排（`reranker`）、生成式重排（`generative_reranker`）、嵌入（`embedding`），它们的 `labels` 是「每条样本一个标签」而不是「每个 token 一个标签」，用普通 `Trainer` 即可。
- **强化学习/对齐（RLHF）**：DPO/ORPO/KTO/CPO/RM/PPO/GRPO/GKD，每种都有自己的 Trainer 和 Config（来自 `trl` 或 ms-swift 自研的 `rlhf_trainers`）。

如果用一堆 `if/elif` 来选 Trainer，代码会非常臃肿且难以扩展。ms-swift 的做法是把这个「任务 → 训练器」的知识**显式编码成两张字典**，路由逻辑只剩「查表 + 动态导入」。

#### 4.1.2 核心流程

派发的核心是一个关键字 + 两张表：

```
              args.task_type (或 args.rlhf_type)
                        │
                        ▼
        ┌───────────────────────────────────┐
        │  TrainerFactory.get_cls(args, X)  │
        │  X = TRAINER_MAPPING /            │
        │      TRAINING_ARGS_MAPPING        │
        └───────────────────────────────────┘
                        │  mapping[key] = 'swift.trainers.Seq2SeqTrainer'
                        │  rsplit('.', 1) → (模块路径, 类名)
                        ▼
            importlib.import_module(模块路径)
                        │
                        ▼
              getattr(module, 类名) → 真正的类
```

两个对外入口：

- `TrainerFactory.get_trainer_cls(args)` → 查 `TRAINER_MAPPING`，返回 Trainer 类。
- `TrainerFactory.get_training_args(args)` → 查 `TRAINING_ARGS_MAPPING`，返回一个**已实例化**的 `TrainingArguments` 对象（注意：它返回的是对象，不是类）。

#### 4.1.3 源码精读

先看两张表。`TRAINER_MAPPING` 是「task_type → Trainer 类路径」，`TRAINING_ARGS_MAPPING` 是「task_type → TrainingArguments 类路径」，键完全一一对应：

[swift/trainers/trainer_factory.py:13-28](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/trainer_factory.py#L13-L28) —— `TRAINER_MAPPING`，注意 `causal_lm` 指向 `Seq2SeqTrainer`（不是普通 `Trainer`），而 `seq_cls`/`embedding`/`reranker`/`generative_reranker` 都用判别式训练器；注释 `# rlhf` 以下 8 个键交给 `rlhf_trainers` 模块。

[swift/trainers/trainer_factory.py:30-45](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/trainer_factory.py#L30-L45) —— `TRAINING_ARGS_MAPPING`，与上面键对齐；生成式任务用 `Seq2SeqTrainingArguments`，判别式任务用 `TrainingArguments`，RLHF 各算法用各自的 Config。

值用一个细节值得注意：**表里存的是字符串（类的全限定名），而不是类本身**。这是个刻意的设计——如果直接写 `'swift.trainers.Seq2SeqTrainer'` 引用类，那么 import `TrainerFactory` 的瞬间就会把所有 Trainer（乃至 vllm/megatron 等重型依赖）全部加载进来，破坏 u1-l3 讲过的 `_LazyModule` 懒加载。用字符串 + 运行时 `importlib` 动态导入，可以**只在真正需要某个 Trainer 时才加载它**。

接着看「查表 + 动态导入」的统一实现：

[swift/trainers/trainer_factory.py:47-55](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/trainer_factory.py#L47-L55) —— `get_cls`。第一行是路由键的决出：**优先看 `args.rlhf_type`，没有才看 `args.task_type`**。这就是为什么 RLHF 管道（`SwiftRLHF`，见 u7-l1）能复用同一套 `TrainerFactory`——它的 args 上挂着 `rlhf_type`，自动走 RLHF 分支。`rsplit('.', 1)` 把 `'swift.trainers.Seq2SeqTrainer'` 拆成 `('swift.trainers', 'Seq2SeqTrainer')`，再 `importlib.import_module` + `getattr` 取到类。

两个薄封装：

[swift/trainers/trainer_factory.py:57-59](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/trainer_factory.py#L57-L59) —— `get_trainer_cls`，查 `TRAINER_MAPPING`，返回**类**。

[swift/trainers/trainer_factory.py:61-73](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/trainer_factory.py#L61-L73) —— `get_training_args`，比上面多一步：它要**实例化** TrainingArguments。难点在于：`args`（`SftArguments` 这种大对象）里有很多 `Seq2SeqTrainingArguments` 不认识的字段，直接 `**asdict(args)` 传进去会报「unexpected keyword argument」。解决办法是用 `inspect.signature(training_args_cls).parameters` 拿到目标类接受的形参名集合，把 `args_dict` 里不在集合中的键 `pop` 掉，做到「按需裁剪」。最后调用 `args._prepare_training_args(args_dict)` 给子类一个「在实例化前再改一刀」的钩子（比如 RLHF 的 ppo 要注入 `world_size`），再实例化。

派发的「消费点」在 `SwiftSft.run`：

[swift/pipelines/train/sft.py:176-184](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/sft.py#L176-L184) —— `trainer_cls = TrainerFactory.get_trainer_cls(args)` 拿到类，紧接着用 `self.args.training_args`（已经在 `SftArguments.__post_init__` 里被 `get_training_args` 实例化好）实例化它。注意构造参数里除了 `model`/`args`/`train_dataset`，还塞了 `template`——这是 ms-swift Trainer 区别于原生 HF Trainer 的关键：它**多持有一个 `template` 对象**，由它来负责编码、loss 计算、前向上下文（见 4.2）。

而 `self.args.training_args` 的来源：

[swift/arguments/sft_args.py:233-234](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/sft_args.py#L233-L234) —— 在 `SftArguments.__post_init__` 末尾，调用 `TrainerFactory.get_training_args(self)` 产出 `self.training_args`，并强制 `remove_unused_columns = False`（因为 ms-swift 的数据列名由 template 控制，不能让 HF 默认逻辑误删）。

> **关于 `simpo` 的小坑**：`rlhf_type` 的 `Literal` 里包含 `'simpo'`，但 `TRAINER_MAPPING` 没有 `'simpo'` 键。这是因为 `_init_simpo` 会把 `simpo` **改写成 `cpo` + `loss_type='simpo'`**（即「SimPO 是 CPOTrainer 的一种 loss 配置」），所以表里只需 `cpo` 一项。详见 [swift/arguments/rlhf_args.py:482-488](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/rlhf_args.py#L482-L488)。这种「参数归一化在 args 层完成、映射表只保留规范键」的分工，是 ms-swift 一贯的设计。

#### 4.1.4 代码实践

**实践目标**：亲手验证「task_type → Trainer 类」的派发关系，并理解派发键从何而来。

**操作步骤**：

1. 确认已按 u1-l2 安装 ms-swift（`pip install -e .`），并在 Python 中能 `import swift`。
2. 在仓库根目录新建一个临时脚本 `probe_factory.py`（**示例代码，不是项目原有文件**），内容如下：

   ```python
   from swift.trainers import TrainerFactory

   # 构造一个只带 task_type 的“假 args”
   class FakeArgs:
       pass

   for tt in ['causal_lm', 'seq_cls', 'embedding', 'reranker', 'generative_reranker']:
       args = FakeArgs()
       args.task_type = tt          # 不设 rlhf_type，走 task_type 分支
       cls = TrainerFactory.get_trainer_cls(args)
       print(f'{tt:20s} -> {cls.__module__}.{cls.__name__}')
   ```

3. 运行 `python probe_factory.py`。

**需要观察的现象**：终端打印出每个 `task_type` 实际派发到的 Trainer 类全限定名；并且由于是 `importlib` 动态导入，第一次执行某行时可能略慢（触发对应子模块加载）。

**预期结果**（基于上述源码）：

```
causal_lm            -> swift.trainers.seq2seq_trainer.Seq2SeqTrainer
seq_cls              -> swift.trainers.trainer.Trainer
embedding            -> swift.trainers.embedding_trainer.EmbeddingTrainer
reranker             -> swift.trainers.reranker_trainer.RerankerTrainer
generative_reranker  -> swift.trainers.reranker_trainer.RerankerTrainer
```

注意 `reranker` 与 `generative_reranker` 派发到**同一个** `RerankerTrainer`——它们靠运行时 `self.task_type` 在 Trainer 内部分支（见 4.3），而不是靠两张表区分。如果运行报错或结果不一致，说明本地 ms-swift 版本与本讲 HEAD（`3d61b9318`）不同，请 `git checkout` 到对应版本后重试。**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果把一个新任务 `my_task` 加进 `TRAINER_MAPPING`，但不加进 `TRAINING_ARGS_MAPPING`，会发生什么？

**参考答案**：`get_trainer_cls` 能正常返回 Trainer 类，但调用 `get_training_args` 时 `cls.TRAINING_ARGS_MAPPING[train_method]` 会抛 `KeyError: 'my_task'`。两张表必须键对齐——这是这套设计的隐含契约。

**练习 2**：为什么 `get_cls` 里要先判断 `hasattr(args, 'rlhf_type')`，而不是直接用 `args.task_type`？

**参考答案**：因为 `SwiftRLHF` 的 args（`RLHFArguments`）**同时**有 `task_type` 和 `rlhf_type` 两个字段（`task_type` 通常仍是 `causal_lm`），而真正决定用哪个 RLHF 训练器的是 `rlhf_type`（如 `dpo`/`grpo`）。优先看 `rlhf_type` 才能正确派发到 `DPOTrainer` 而不是误派到 `Seq2SeqTrainer`。

---

### 4.2 Seq2SeqTrainer 与 SwiftMixin：在 HF Trainer 上挂载 template

#### 4.2.1 概念说明

`TrainerFactory` 只告诉你「用哪个类」，真正干活的是这个类。生成式任务（SFT/预训练）落到的 `Seq2SeqTrainer`，是整个训练器体系的「主力」。

理解它的关键，是看它的**继承链**：

```
class Seq2SeqTrainer(SwiftMixin, DataLoaderMixin, HfSeq2SeqTrainer):
```

三个父类各有分工：

- **`HfSeq2SeqTrainer`**（来自 transformers）：提供「标准的训练循环、优化器、调度器、checkpoint、日志、evaluate」这套完整基建。ms-swift 不重写训练循环，而是**复用**它。
- **`SwiftMixin`**：ms-swift 的核心能力混入（mixin）。负责「把 `template` 接进 Trainer」「自定义保存模型」「loss/eval 指标工厂」「梯度检查点修复」「序列并行/padding_free 打补丁」等大量覆盖。这是「ms-swift 特性」的集中地。
- **`DataLoaderMixin`**：自定义 `get_train_dataloader`/`get_eval_dataloader`，支持序列并行采样器、streaming 数据集、group_by_length 等。

> **为什么生成式用 `Seq2SeqTrainer` 而不是普通 `Trainer`？** 因为 SFT 经常需要 `predict_with_generate`（在 eval 阶段让模型真正「生成」回答来评估，而不是只算 loss）。`HfSeq2SeqTrainer` 内置了 `prediction_step` 的生成分支，ms-swift 借此实现「训练时算 loss、评估时生成文本」。

#### 4.2.2 核心流程

`Seq2SeqTrainer` 的生命周期关键点：

1. **构造（`__init__`）**：先走 `SwiftMixin.__init__`（接 template、建 data_collator、建 loss/指标、初始化 callbacks），再走 HF 基类的构造；若 `predict_with_generate` 则建一个内部 `TransformersEngine` 用于评估期生成。
2. **每步训练（`training_step`）**：在 `template.forward_context` 上下文里调父类 `training_step`——这个上下文负责处理多模态视觉编码、序列并行等「前向前置准备」。
3. **算损失（`compute_loss`）**：把 loss 的实际计算**委托给 `template.compute_sft_loss`**，自己只负责 loss_scale、MoE aux loss、acc 统计等「上层逻辑」。
4. **准备输入（`_prepare_inputs`）**：处理 `logits_to_keep`（省显存）、MoE 的 `output_router_logits`、把 `compute_loss_func` 塞进 inputs 供下游使用。
5. **保存模型（`SwiftMixin._save`/`_save_model`）**：根据是否 adapter（LoRA）、是否 SentenceTransformer、是否 flash checkpoint，走不同分支，但对外接口统一。

#### 4.2.3 源码精读

先看继承声明与构造：

[swift/trainers/seq2seq_trainer.py:26-37](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/seq2seq_trainer.py#L26-L37) —— 继承链与 `__init__`。`super().__init__` 会触发 `SwiftMixin.__init__`（见下）；构造尾段两件事：①若 `predict_with_generate`，建内部 `TransformersEngine`；②建一个 `JsonlWriter`，用于把评估期的「生成回答 vs 标签」落盘到 `predict.jsonl`，方便人工核对。

`compute_loss` 是最长、最重要的方法，但它的「心脏」其实只有一行：

[swift/trainers/seq2seq_trainer.py:141](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/seq2seq_trainer.py#L141) —— `outputs = self.template.compute_sft_loss(model, inputs, ...)`。**真正的 loss 计算（含 loss_scale 逐 token 加权、padding_free 对齐）被委托给了 template**。Trainer 自己主要做三件「上层」事：①处理 `loss_scale` 张量的移位（`torch.roll(loss_scale, shifts=-1)`，因为 loss 是「用当前位置预测下一个 token」，所以权重也要错位一位）；②累加 MoE 的 `aux_loss`（见 [swift/trainers/seq2seq_trainer.py:209-214](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/seq2seq_trainer.py#L209-L214)）；③在 logits 可用时调 `_compute_acc` 统计 token 级准确率。

[swift/trainers/seq2seq_trainer.py:231-233](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/seq2seq_trainer.py#L231-L233) —— `training_step` 只做一件事：用 `template.forward_context(self.model, inputs)` 包裹父类调用。这个上下文管理器是「多模态/序列并行前向前置」的统一入口，保证 HF 训练循环的每一步都在正确的上下文里跑。

再来看 `SwiftMixin`——这是所有 ms-swift Trainer 的「公共能力层」：

[swift/trainers/mixin.py:67-86](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/mixin.py#L67-L86) —— `SwiftMixin.__init__` 的开头。几个关键赋值：`self.template = template`（把 template 挂上）、`self.task_type = self.template.task_type`（task_type 从 template 取一份缓存在 Trainer 上）、`self.optimizer_callback = optimizers_map[args.optimizer or 'default'](args, self)`（优化器插件，见 u10-l2）、`self.model_meta`/`self.model_info` 从 model 对象上取回（u3-l1 讲过这两个对象会被挂回 model）。

[swift/trainers/mixin.py:117-132](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/mixin.py#L117-L132) —— 两个关键装配：①`data_collator = partial(template.data_collator, ...)`，**collator 也来自 template**（u3-l3/u4-l3 讲过 packing/padding_free 都在 collator 里）；②`kwargs.update(self.create_loss_and_eval_metric(args))`，根据 `--loss_type`/`--eval_metric` 把自定义 loss 函数和 eval 指标塞进 kwargs 传给 HF Trainer。

[swift/trainers/mixin.py:1046-1054](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/mixin.py#L1046-L1054) —— `create_loss_and_eval_metric`：`--loss_type` 时从 `loss_map` 取自定义 loss（u10-l1），`--eval_metric` 时从 `eval_metrics_map` 取指标。这是「插件化 loss/指标」的接入点。

[swift/trainers/mixin.py:732-878](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/mixin.py#L732-L878) —— `_patch_tasks`：**判别式任务（seq_cls/reranker/embedding）在这里被「打补丁」成能跑的样子**。它会根据 `task_type` 给模型注册 forward hook（如序列并行 gather、padding_free 还原），甚至用 `MethodType` 替换 `model.forward`（如 `seq_cls_forward`/`reranker_forward`）。这就是为什么 `seq_cls`/`reranker`/`embedding` 能共用同一个 `Trainer` 类——它们的差异被推迟到运行时的 hook/forward 替换里解决。

[swift/trainers/mixin.py:322-415](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/mixin.py#L322-L415) —— `_save_model`：保存逻辑的「分发器」。根据 model 的真实类型（`SwiftModel`/`PreTrainedModel`/`PeftModel`/`SentenceTransformer`）和是否 `use_flash_ckpt`，走不同分支；adapter 模型只保存 adapter（`selected_adapters=['default']`）。这是「LoRA 只存增量」在 Trainer 侧的落点。

判别式基类 `Trainer` 很薄，主要覆盖 `compute_loss`：

[swift/trainers/trainer.py:17-18](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/trainer.py#L17-L18) —— `class Trainer(SwiftMixin, DataLoaderMixin, HfTrainer)`。

[swift/trainers/trainer.py:66-72](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/trainer.py#L66-L72) —— `compute_loss` 直接调父类（走 HF 默认 loss，因为判别式任务的 loss 由模型 `loss_function` 给出），再做梯度累积归一和 acc 统计。对比 `Seq2SeqTrainer.compute_loss` 的复杂度，能直观看到「生成式 vs 判别式」loss 处理的巨大差异。

`RerankerTrainer` 与 `EmbeddingTrainer` 在此基础上再加一层：

[swift/trainers/reranker_trainer.py:23-28](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/reranker_trainer.py#L23-L28) —— `generative_reranker` 在这里被特殊处理：取每条样本最后一个有效 token 的 logits 作为该样本的打分（`last_valid_indices`）。这就是「同一个 RerankerTrainer 内部按 `task_type` 分叉」的具体落点。

[swift/trainers/embedding_trainer.py:20-35](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/embedding_trainer.py#L20-L35) —— Matryoshka（MRL）损失：对嵌入向量按多个截断维度分别算 loss 再加权求和。`EmbeddingTrainer` 还把 `gather_function` 换成 `gather_for_unpadded_tensors`，适配嵌入任务「需要跨卡 gather 全量向量」的特性。

#### 4.2.4 代码实践

**实践目标**：通过「源码阅读 + 调用链跟踪」，看清 `Seq2SeqTrainer` 是怎么把 template 接进训练循环的，而不真正跑训练。

**操作步骤**：

1. 打开 [swift/trainers/seq2seq_trainer.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/seq2seq_trainer.py)，定位 `compute_loss`（L125）。
2. 在 `compute_loss` 体内，找到委托给 template 的那一行（L141 `self.template.compute_sft_loss(...)`）。
3. 跟进 `compute_sft_loss` 的定义（在 `swift/template/base.py` 内，用 IDE 跳转或 `grep -n "def compute_sft_loss" swift/template/base.py`）。
4. 回到 `mixin.py` 的 `__init__`（L70），确认 `self.template`、`data_collator`、`create_loss_and_eval_metric` 三处都「以 template 为中心」装配。

**需要观察的现象/思考**：

- `compute_loss` 里几乎所有「token 级 loss 细节」（loss_scale、padding_free 对齐）都不在 Trainer 里，而在 template 里。Trainer 只做 MoE aux loss、acc、loss_scale 错位这些「与具体 loss 形式无关」的事。
- 这种分工的好处：**换一个 task_type（如判别式），Trainer 类换了，但 template 依然可以被复用**（编码、collator 逻辑通用）。

**预期结果**：你能用一句话描述 `Seq2SeqTrainer.compute_loss` 的职责——「在 template.forward 上下文之外，按 token 级 loss_scale 与 MoE aux loss 组织损失，并把具体前向+损失委托给 template」。这是纯阅读型实践，无需运行，**结果可复现**。

#### 4.2.5 小练习与答案

**练习 1**：`Seq2SeqTrainer`、判别式 `Trainer`、`RerankerTrainer`、`EmbeddingTrainer` 四者，谁是谁的父类？画一张继承图。

**参考答案**：

```
HfSeq2SeqTrainer ─┐
SwiftMixin        ├──> Seq2SeqTrainer        (causal_lm)
DataLoaderMixin   ┘

HfTrainer ────────┐
SwiftMixin        ├──> Trainer               (seq_cls)
DataLoaderMixin   ┘        │
                           ├──> RerankerTrainer   (reranker / generative_reranker)
                           └──> EmbeddingTrainer  (embedding)
```

`SwiftMixin` + `DataLoaderMixin` 是所有 ms-swift Trainer 的「公共底座」，区别只在最顶上是 `HfSeq2SeqTrainer`（生成式）还是 `HfTrainer`（判别式），以及判别式之上是否再加一层 `Reranker`/`Embedding` 特化。

**练习 2**：为什么 `SwiftMixin.__init__` 要把 `self.task_type = self.template.task_type` 缓存一份，而不是每次用 `self.template.task_type`？

**参考答案**：①减少属性查找链路，热路径上更直接；②`task_type` 在一次训练里是固定的，缓存后语义更清晰（「这个 Trainer 是做什么任务的」）；③`_compute_acc`/`_patch_tasks` 等方法频繁读 `self.task_type`，缓存可避免反复跨对象访问。

---

### 4.3 patcher 与 trainer_factory：task_type 从哪来，Trainer 何时被「装配」

#### 4.3.1 概念说明

前两节留下两个问题：①`args.task_type` 这个「路由键」到底从哪来？②ms-swift 对 HF Trainer 的改造，除了继承，还有没有「更底层」的手段？本节回答这两个问题，把 `TrainerFactory` 上下游的链路补全。

先说 **task_type 的来源**。它不是用户随便填的，而是经历了一条「注册 → 加载 → 回填」的链路：

```
register_model(task_type=...)          # 注册时写进 ModelMeta（默认 causal_lm）
        │
        ▼
get_model_info_meta(...)               # 加载时决出最终 task_type（显式 > num_labels > 默认）
        │  写入 ModelInfo.task_type
        ▼
ModelArguments._init_model_info()      # 把 model_info.task_type 回填到 args.task_type
        │
        ▼
TrainerFactory.get_cls(args)           # 用 args.task_type 查表派发
```

再说 **patcher.py 的角色**。`swift/trainers/patcher.py`（注意：与 `swift/model/patcher.py` 是两个不同的文件）做的是**「猴子补丁」**——在 `swift.trainers` 包被 import 的瞬间，把 transformers 模块里的几个默认回调类**替换成 ms-swift 增强版**。这是一种比继承更「全局」的改造：它不针对某个 Trainer 实例，而是改写了**所有** HF Trainer 的默认行为。

#### 4.3.2 核心流程

task_type 的决出规则（在 `get_model_info_meta` 里）可以概括为三档优先级：

1. **显式传入**：用户在 `register_model` 时指定了 `task_type`，或命令行 `--task_type` 显式覆盖 → 用它。
2. **由 `num_labels` 推断**：没指定 task_type 但传了 `num_labels` → 推断为 `seq_cls`（分类任务）。
3. **默认 `causal_lm`**：什么都没给 → 当成生成式语言模型。

之后再按 task_type 做特化校验：`seq_cls` 必须有 `num_labels` 且据此推断 `problem_type`；`reranker` 默认 `num_labels=1`；`generative_reranker` 不需要 `num_labels`（它用 CausalLM 结构）。

patcher 的「装配时机」则是：`swift/trainers/__init__.py` 在构建 `_LazyModule` 之前，有一句**直接**（非懒）`from . import patcher`，于是 patcher 的模块级代码（含 monkey patching）在「任何 Trainer 还没被实例化之前」就执行完毕。

#### 4.3.3 源码精读

先看 task_type 的回填。`ModelArguments` 上的 `task_type` 字段定义：

[swift/arguments/base_args/model_args.py:72](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/model_args.py#L72) —— 字段声明，`None` 表示「待加载时回填」。注意 docstring（[model_args.py:29-31](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/model_args.py#L29-L31)）说明：默认 `causal_lm`，`seq_cls` 时通常还要指定 `--num_labels`/`--problem_type`。

[swift/arguments/base_args/model_args.py:191-203](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/model_args.py#L191-L203) —— `_init_model_info`：调用 `get_model_info_meta(**model_kwargs)` 拿到 `(model_info, model_meta)`，然后 `self.task_type = self.model_info.task_type`。**这就是 args.task_type 的直接来源**——它来自 `model_info`，而 `model_info` 来自 `get_model_info_meta`。

再看 task_type 在模型侧的最终决出：

[swift/model/model_meta.py:292-321](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_meta.py#L292-L321) —— `task_type is None` 时的三档决出（默认 `causal_lm`，有 `num_labels` 则 `seq_cls`，`ModelMeta.task_type` 非空则覆盖），以及 `reranker`/`generative_reranker`/`seq_cls` 的特化校验，最后 `model_info.task_type = task_type` 写回。**这一段是「task_type 路由键」的真正诞生地**。

最后看 patcher 的 monkey patching：

[swift/trainers/patcher.py:105-108](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/patcher.py#L105-L108) —— 模块级语句（不在任何函数里），import 即执行。它把 `transformers.trainer.DEFAULT_PROGRESS_CALLBACK` 换成 `ProgressCallbackNew`、`DEFAULT_CALLBACKS` 换成 `[DefaultFlowCallbackNew]`、`PrinterCallback` 换成 `PrinterCallbackNew`。

这三处替换的意义：

- `ProgressCallbackNew`（[patcher.py:51-58](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/patcher.py#L51-L58)）：在每次 `on_log` 时调用 `add_train_message`，往日志里加 `global_step/max_steps`、`elapsed_time`、`remaining_time`、`memory(GiB)`、`train_speed(s/it)`——这就是你训练时在终端看到的那行「带剩余时间和显存」的进度信息的来源。
- `DefaultFlowCallbackNew`（[patcher.py:61-84](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/patcher.py#L61-L84)）：在最后一步/`max_epochs` 结束时强制触发一次 evaluate + save，保证训练结束一定有终态 checkpoint。
- `PrinterCallbackNew`：为没有 tqdm 的环境提供带 `add_train_message` 增强信息的打印回调。

而触发这一切的那句「直接 import」在：

[swift/trainers/__init__.py:5](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/__init__.py#L5) —— `from . import patcher`。它在 `_LazyModule` 构造之前执行，所以 patcher 的模块级 monkey patching 一定先于任何 Trainer 实例化生效。

把整条链路串起来，`TrainerFactory` 的位置就清晰了：

```
模型注册: register_model(task_type=...)         (u3-l1)
        │
        ▼
加载决出: get_model_info_meta → model_info.task_type   (model_meta.py:292)
        │
        ▼
参数回填: _init_model_info → args.task_type            (model_args.py:196)
        │
        ▼  SftArguments.__post_init__
实例化训练参数: TrainerFactory.get_training_args(self) → args.training_args  (sft_args.py:233)
        │
        ▼  SwiftSft.run
选训练器类: TrainerFactory.get_trainer_cls(args) → trainer_cls  (sft.py:176)
        │
        ▼
实例化: trainer_cls(model, args.training_args, template, ...) → trainer
        │  __init__ 内: SwiftMixin 接 template、建 collator/loss/callbacks
        │              patcher 已提前 patch 好 HF 默认回调
        ▼
trainer.train()  →  HF 训练循环（每步调 Seq2SeqTrainer.compute_loss/training_step）
```

#### 4.3.4 代码实践

**实践目标**：亲手追踪 `args.task_type` 的诞生，验证「同一份代码对不同任务派发出不同 Trainer」。

**操作步骤（源码阅读型）**：

1. 在仓库根目录执行（只读分析，不修改任何文件）：

   ```bash
   # 1) 看 task_type 字段定义
   grep -n "task_type:" swift/arguments/base_args/model_args.py | head -3
   # 2) 看 task_type 在哪被回填到 args
   grep -n "self.task_type = self.model_info" swift/arguments/base_args/model_args.py
   # 3) 看 task_type 在模型侧如何决出
   grep -n "task_type = 'causal_lm'\|task_type = 'seq_cls'\|model_info.task_type = task_type" swift/model/model_meta.py
   # 4) 看 patcher 在哪触发
   grep -n "from . import patcher" swift/trainers/__init__.py
   ```

2. 对比两个真实模型注册，看 `task_type` 的不同取值：
   - `grep -n "task_type='reranker'" swift/model/models/bert.py`（一个 reranker 模型，如 BGE）。
   - 任意 Qwen/Llama 文本模型（默认走 `causal_lm`，注册时不显式写 task_type）。

3. 回答下面这个问题（见「预期结果」）。

**需要观察的现象**：`task_type` 在 `model_meta.py` 里被赋值，在 `model_args.py` 里被回填到 args，在 `trainer_factory.py` 里被用作字典 key——三个文件、三个阶段，一条干净的链路。`patcher` 的 import 是 trainers 包加载时的「副作用」。

**预期结果**（你应该能得出这些结论）：

- `causal_lm`：用户不显式指定 task_type、模型注册也不写 → `get_model_info_meta` 默认 `causal_lm` → 派发到 `Seq2SeqTrainer`。
- `seq_cls`：用户传 `--num_labels N` 且模型注册未指定 → 推断为 `seq_cls` → 派发到判别式 `Trainer`。
- `reranker`：模型注册时写 `task_type='reranker'`（如 BGE）→ 派发到 `RerankerTrainer`。
- `embedding`：模型注册时写 `task_type='embedding'`（或 `AutoModel` 类）→ 派发到 `EmbeddingTrainer`。

若某条结论与本地源码不符，以本地源码为准并记录差异。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：如果用户既不传 `--task_type`，也不传 `--num_labels`，加载一个普通的 Qwen 模型，最终的 `args.task_type` 是什么？会派发到哪个 Trainer？

**参考答案**：`get_model_info_meta` 中 `task_type is None` 且 `num_labels is None` → 走 `task_type = 'causal_lm'`（[model_meta.py:296](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_meta.py#L296)）；Qwen 的 `ModelMeta.task_type` 通常也是 `None`（不覆盖），所以最终 `args.task_type = 'causal_lm'`，派发到 `Seq2SeqTrainer`。这是绝大多数 SFT 用户的默认路径。

**练习 2**：`swift/trainers/patcher.py` 里的 monkey patching 是「全局副作用」。如果用户在同一个进程里既想用 ms-swift 训练、又想用原生 transformers 训练，会有什么风险？

**参考答案**：因为 patcher 直接改写了 `transformers.trainer.DEFAULT_PROGRESS_CALLBACK` 等模块级变量，**同一个进程里所有 HF Trainer 实例**（包括用户自己写的原生 transformers 训练代码）都会用上 `ProgressCallbackNew`/`DefaultFlowCallbackNew`。这通常无害（只是日志多了显存/速度信息），但如果用户的原生代码依赖了回调的某些特定行为（如 `DefaultFlowCallback` 在最后一步**不**强制 save），就可能产生行为差异。这是「全局打补丁」相比「继承」的固有代价——换取的是「不用让每个 Trainer 子类都记得注册回调」的便利。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「端到端派发追踪」小任务。

**任务**：给定一条具体的训练命令，不实际运行，纯靠读源码预测它会派发到哪个 Trainer、用哪套 TrainingArguments，并验证你的预测。

**示例命令**（任选其一，或自拟）：

- A. `swift sft --model Qwen/Qwen3-4B --dataset swift/self-cognition --tuner_type lora`
- B. `swift sft --model <某个 bert 分类模型> --dataset <分类数据> --num_labels 3`
- C. `swift rlhf --rlhf_type grpo --model Qwen/Qwen3-4B --dataset <grpo 数据>`

**步骤**：

1. **定 task_type / rlhf_type**：对每个命令，判断 `args` 上最终会挂哪个键。
   - A：不传 task_type、不传 num_labels → `task_type='causal_lm'`。
   - B：传 `--num_labels 3` → 推断 `task_type='seq_cls'`。
   - C：`--rlhf_type grpo` → `args.rlhf_type='grpo'`。
2. **查两张表**：在 [trainer_factory.py:13-45](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/trainer_factory.py#L13-L45) 中查键，写出 Trainer 类与 TrainingArguments 类。
3. **画继承链**：对查到的 Trainer 类，画出它的继承图（参考 4.2.5 练习 1 的答案），指出它复用了哪些 Mixin。
4. **找装配点**：指出 `template` 是在 Trainer 的哪个方法/属性里被用到（提示：`__init__` 挂载、`compute_loss`/`compute_sft_loss` 委托、`data_collator` 来自 template）。
5. **（可选）实际运行验证**：在有 GPU 的环境里真正跑 A 的前几步（`--max_steps 2`），在日志里确认终端进度行带有 `memory(GiB)`/`train_speed(s/it)`——这正是 patcher 替换的 `ProgressCallbackNew` 的产物，从而验证「patcher 在 Trainer 实例化前已生效」。

**预期结果**：

| 命令 | 路由键 | Trainer | TrainingArguments |
| --- | --- | --- | --- |
| A | `task_type=causal_lm` | `Seq2SeqTrainer` | `Seq2SeqTrainingArguments` |
| B | `task_type=seq_cls` | `Trainer`（判别式） | `TrainingArguments` |
| C | `rlhf_type=grpo` | `GRPOTrainer`（来自 `rlhf_trainers`） | `GRPOConfig` |

完成此任务后，你应该能对任意一条 `swift` 训练命令，在脑子里跑完「命令 → args.task_type → TrainerFactory 查表 → Trainer 类 → SwiftMixin 装配 → HF 训练循环」这条链路。这正是本讲想要建立的全局图景。

---

## 6. 本讲小结

- **`TrainerFactory` 是一个薄路由器**：用 `TRAINER_MAPPING` + `TRAINING_ARGS_MAPPING` 两张键对齐的字典，把 `task_type`（或 `rlhf_type`）派发成 Trainer 类与 TrainingArguments 类；表里存「字符串路径」而非类，靠 `importlib` 动态导入以保护懒加载。
- **路由键的决出优先级**：`get_cls` 先看 `args.rlhf_type`，没有才看 `args.task_type`；这使 RLHF 管道能复用同一套 `TrainerFactory`。
- **`causal_lm` 用 `Seq2SeqTrainer`**，判别式（`seq_cls`/`embedding`/`reranker`）用 `Trainer` 及其子类；所有 ms-swift Trainer 共享 `SwiftMixin` + `DataLoaderMixin` 这个「公共底座」。
- **Trainer 与 template 的分工**：`compute_loss`/`data_collator`/前向上下文都「以 template 为中心」装配，具体 token 级 loss 委托给 `template.compute_sft_loss`，Trainer 自己只管 MoE aux loss、acc、loss_scale 错位等上层逻辑。
- **`task_type` 走过一条干净的链路**：`register_model` → `get_model_info_meta` 决出（显式 > `num_labels` > 默认 `causal_lm`）→ `_init_model_info` 回填到 `args.task_type` → `TrainerFactory` 查表。
- **`patcher.py` 用猴子补丁全局增强 HF Trainer**：在 `swift.trainers` 包 import 时替换默认进度/流程回调，给所有 Trainer 加上显存/速度日志与「最后一步强制 save」行为——这是比继承更全局、但也有「副作用溢出」代价的改造手段。

---

## 7. 下一步学习建议

- **紧接着学 u5-l2（TunerPlugin 与模型适配）**：本讲的 Trainer 在 `SwiftSft.run` 里被实例化之前，模型会先经过 `prepare_model` 挂载 LoRA 等可训练参数——那是 `tuner_plugin` 的事。建议紧接着学，把「模型准备 → Trainer 装配 → 训练」补全。
- **深入 `SwiftMixin`**：本讲只点了 `SwiftMixin` 的几个关键方法。如果你关心 checkpoint 管理、flash checkpoint、DeepSpeed ZeRO-3 修复等，可以直接精读 [swift/trainers/mixin.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/mixin.py) 的 `_save_checkpoint`/`_save_flash_checkpoint`/`_fix_zero3_gather_all_parameters`。
- **看 RLHF 训练器（u7 系列）**：本讲的 `TRAINER_MAPPING` 里 `# rlhf` 以下 8 个键都指向 `swift.rlhf_trainers`。学完 u5-l4（SFT 主流程）后，可去 u7-l1/u7-l2 看 `SwiftRLHF` 如何复用 `SwiftSft` 并接入 `GRPOTrainer` 等。
- **阅读扩展点（u10 系列）**：`SwiftMixin.create_loss_and_eval_metric` 里出现的 `loss_map`/`eval_metrics_map`、`__init__` 里出现的 `optimizers_map`/`callbacks_map`，是 ms-swift「插件化」的四个注册表，u10-l1/u10-l2 会讲如何往里加自定义实现。

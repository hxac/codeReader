# SFT 训练脚本主流程

## 1. 本讲目标

本讲是「SFT 蒸馏流水线」单元的第一篇。在前置讲义里，你已经知道 open-r1 的可执行核心只有三个薄脚本，其中 `sft.py` 负责**监督微调（Supervised Fine-Tuning, SFT）**——也就是用「带推理过程的高质量问答数据」去训练一个基础模型，让它学会像 DeepSeek-R1 一样「先思考再作答」。这是 open-r1 三步走计划中**Step 1（蒸馏）**的落地代码。

学完本讲，你应当能够：

1. 从入口（`if __name__ == "__main__"`）开始，说清 `sft.py` 的 `main()` 函数如何一步步走完「日志 → 数据加载 → 模型加载 → 训练 → 保存 → 评估 → 推送」的完整链路。
2. 理解 `SFTTrainer` 是如何用 `model`、`args`、`peft_config`、`callbacks` 等参数组装起来的。
3. 看懂两个容易忽略但很关键的健壮性细节：**断点恢复（checkpoint resume）** 和 **保存阶段对 `generation_config.eos_token_id` 的对齐**。
4. 能够在本地用一个「小模型 + 小数据集」的最小配置把 SFT 跑起来（或至少读懂启动命令每一项的作用）。

## 2. 前置知识

在进入源码前，先建立几个直觉。本讲默认你已读过前置讲义，这里只做简短衔接：

- **三元组配置**：`sft.py` 不自己解析命令行，而是把工作交给 trl 的 `TrlParser`，让它解析出三个对象：脚本参数 `ScriptArguments`、训练参数 `SFTConfig`、模型参数 `ModelConfig`。这正是 `u1-l4` 讲过的「YAML 配方 + 命令行覆盖」机制。
- **「薄脚本」哲学**：`sft.py` 本身不实现训练算法。真正的训练循环、梯度更新、显存优化全部委托给 trl 的 `SFTTrainer`（再往下是 `transformers` 的 `Trainer`）。脚本只负责**组装零件**：把数据、模型、分词器、回调按顺序喂给 `SFTTrainer`。
- **SFT 在做什么**：给定一条「问题 + 标准答案（含思维链）」的样本，让模型最小化「在问题条件下生成标准答案」的负对数似然。用一句话概括，就是教模型「遇到这种问题，应该这样一步步回答」。
- **几个术语**：
  - **checkpoint（检查点）**：训练过程中定期保存的模型权重 + 优化器状态，用于中断后续训。
  - **EOS token（结束符）**：告诉模型「生成到此为止」的特殊 token，例如 `<|im_end|>`。
  - **chat template（对话模板）**：把「角色 + 内容」拼成模型能理解的字符串的 Jinja 模板，例如 ChatML。
  - **model card**：发布到 Hugging Face Hub 时附带的模型说明卡片。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/open_r1/sft.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/sft.py) | 本讲主角。SFT 训练入口脚本，包含 `main()` 和 `__main__` 块。 |
| [src/open_r1/utils/\_\_init\_\_.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/__init__.py) | 工具包导出口，集中导出 `get_dataset`、`get_model`、`get_tokenizer`。`sft.py` 正是从这里导入这三个零件。 |
| [src/open_r1/utils/model_utils.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/model_utils.py) | 提供 `get_model` / `get_tokenizer`，负责 dtype、量化、注意力实现、chat template 与 `use_cache` 的处理。 |
| [src/open_r1/utils/callbacks.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/callbacks.py) | 提供 `get_callbacks` 与回调注册表 `CALLBACKS`，例如 `PushToHubRevisionCallback`。 |
| [src/open_r1/configs.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py) | 定义 `ScriptArguments` 与 `SFTConfig`（在 trl 基类上扩展了 `callbacks`、`chat_template` 等字段）。 |
| [recipes/OpenR1-Distill-7B/sft/config_distill.yaml](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/OpenR1-Distill-7B/sft/config_distill.yaml) | 复现 OpenR1-Distill-7B 的 YAML 配方，是 `--config` 的典型入参。 |

## 4. 核心概念与源码讲解

### 4.1 入口与 main 函数的阶段总览

#### 4.1.1 概念说明

`sft.py` 是一个「可被 `accelerate launch` 直接运行」的脚本。它的整体形状非常简单：

- 顶部一段**文档字符串（docstring）**，给出一条可直接复制的启动命令示例。
- 一个 `main(script_args, training_args, model_args)` 函数，承载全部业务逻辑。
- 文件末尾一个 `if __name__ == "__main__":` 块，负责**解析参数并调用 `main`**。

`main` 把整个训练过程切成了几个**阶段（stage）**，每个阶段用一段注释分隔，顺序固定：

1. **Setup logging（配置日志）**
2. **Check for last checkpoint（检查断点）** + 可选的 **wandb 初始化**
3. **Load dataset, tokenizer, and model（加载数据/分词器/模型）**
4. **Initialize the SFT Trainer（组装训练器）**
5. **Training loop（训练循环）**
6. **Save model and create model card（保存模型 + 生成卡片）**
7. **Evaluate（评估，可选）**
8. **Push to hub（推送到 Hub，可选）**

理解了这八个阶段的顺序，你就掌握了 `sft.py` 的「骨架」。本讲后续模块会逐个拆开。

#### 4.1.2 核心流程

入口的执行流程可以画成：

```text
accelerate launch src/open_r1/sft.py --config <yaml>
        │
        ▼
TrlParser((ScriptArguments, SFTConfig, ModelConfig)).parse_args_and_config()
        │  返回三元组
        ▼
main(script_args, training_args, model_args)
        │
        ├─ set_seed              # 固定随机性，保证可复现
        ├─ setup logging         # 统一 transformers/datasets 的日志级别
        ├─ 检查 output_dir 里是否有 last_checkpoint
        ├─ (可选) init_wandb_training
        ├─ get_dataset / get_tokenizer / get_model
        ├─ (无 chat template 时) setup_chat_format
        ├─ 组装 SFTTrainer
        ├─ trainer.train(resume_from_checkpoint=...)
        ├─ 保存 metrics / save_state
        ├─ 对齐 eos / save_model / create_model_card / 恢复 use_cache
        ├─ (可选) trainer.evaluate()
        └─ (可选) trainer.push_to_hub()
```

注意第一行的 `set_seed`：它在**所有其他操作之前**执行，因为后续的数据洗牌、模型权重初始化、dropout 都依赖随机数生成器，先把种子固定住，实验才可复现。

#### 4.1.3 源码精读

**入口块**——把 `TrlParser` 绑定到三元组 dataclass，再交给 `main`：

[文件入口：TrlParser 绑定三元组并调用 main](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/sft.py#L166-L169)

```python
if __name__ == "__main__":
    parser = TrlParser((ScriptArguments, SFTConfig, ModelConfig))
    script_args, training_args, model_args = parser.parse_args_and_config()
    main(script_args, training_args, model_args)
```

这里 `(ScriptArguments, SFTConfig, ModelConfig)` 的顺序很重要：`TrlParser` 会按字段名把 YAML/CLI 参数**路由**到对应 dataclass（详见 `u1-l4`），返回的三个对象顺序与之一一对应。

**`main` 的签名与第一步**——接收三元组，第一件事是固定种子：

[main 函数签名与 set_seed](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/sft.py#L55-L56)

```python
def main(script_args, training_args, model_args):
    set_seed(training_args.seed)
```

**Setup logging**——用 `basicConfig` 配一个输出到 stdout 的 handler，并把 `datasets`、`transformers` 的日志级别统一拉到与进程一致的级别：

[Setup logging 阶段](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/sft.py#L58-L75)

这段还顺手把三个配置对象打印出来，方便你在日志里核对「实际生效的参数」是不是和预期一致——这是排查「为什么训练行为不对」的第一手信息。

**加载数据/分词器/模型**——三个 `get_*` 调用，全部来自 `open_r1.utils`：

[加载阶段：dataset / tokenizer / model](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/sft.py#L87-L92)

```python
dataset = get_dataset(script_args)
tokenizer = get_tokenizer(model_args, training_args)
model = get_model(model_args, training_args)
```

这三个函数的导出聚合在 `utils/__init__.py` 里：

[utils 包导出 get_dataset / get_model / get_tokenizer](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/__init__.py#L1-L6)

> 小提示：为什么脚本写 `from open_r1.utils import get_model` 而不是 `from open_r1.utils.model_utils import get_model`？因为包根 `open_r1/__init__.py` 是空的，真正的对外接口统一收口在 `utils/__init__.py`——这是 `u1-l2` 讲过的目录约定。这样脚本只依赖「包级 API」，内部模块重命名也不会影响脚本。

#### 4.1.4 代码实践

**实践目标**：在源码里精确定位每个阶段的起止行，建立「阶段 ↔ 行号」的肌肉记忆。

**操作步骤**：

1. 打开 [src/open_r1/sft.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/sft.py)。
2. 对照下面的「阶段清单」，在文件里找到每一段注释分隔符（如 `###############`、`######################################`）。
3. 在自己的笔记里填一张表：`阶段名 | 起始行 | 结束行 | 关键调用`。

**参考答案（起止行以当前 HEAD 为准）**：

| 阶段 | 起始行 | 结束行 | 关键调用 |
|------|--------|--------|----------|
| 函数签名 + set_seed | 55 | 56 | `set_seed(training_args.seed)` |
| Setup logging | 58 | 75 | `logging.basicConfig(...)` |
| Check last checkpoint | 77 | 82 | `get_last_checkpoint(...)` |
| wandb 初始化 | 84 | 85 | `init_wandb_training(...)` |
| Load dataset/tokenizer/model | 87 | 92 | `get_dataset/get_tokenizer/get_model` |
| chat template 兜底 | 94 | 96 | `setup_chat_format(...)` |
| Initialize SFT Trainer | 98 | 109 | `SFTTrainer(...)` |
| Training loop | 111 | 125 | `trainer.train(...)` |
| Save model + model card | 127 | 146 | `trainer.save_model(...)` |
| Evaluate | 148 | 156 | `trainer.evaluate()` |
| Push to hub | 158 | 163 | `trainer.push_to_hub(...)` |

**需要观察的现象**：你会发现整段 `main` 几乎没有「算法」代码，全是「装配 + 委托」。这正是 open-r1「simple by design」的体现。

**预期结果**：你能不看源码，口述出八个阶段的顺序。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `set_seed(training_args.seed)` 删掉，训练结果会发生什么变化？为什么仍然能「大致复现」一次单卡训练？

> **参考答案**：删掉后随机种子由系统时间等决定，每次运行的数据洗牌、权重初始化、dropout 都不同，**结果不可复现**。单卡训练之所以「大致」能复现，是因为默认种子是固定值（`SFTConfig` 继承自 transformers，`seed` 默认 42），并非真的不需要设种子。

**练习 2**：`TrlParser((ScriptArguments, SFTConfig, ModelConfig))` 里三元组的顺序如果写反了，会发生什么？

> **参考答案**：`parse_args_and_config()` 返回的对象顺序与传入的 dataclass 顺序一致。如果写反，`script_args`、`training_args`、`model_args` 会被错误地绑定到错误类型的对象，后续访问属性（如 `training_args.seed`）会抛 `AttributeError`。所以顺序必须与解包顺序严格对应。

---

### 4.2 SFTTrainer 的组装：model、args、peft_config、callbacks

#### 4.2.1 概念说明

到这一步，我们已经有了数据、分词器、模型三个「零件」。但训练不是直接拿模型去跑，而是要先把它装进一个**训练器（Trainer）**。`SFTTrainer` 是 trl 提供的专门用于监督微调的训练器，它在 transformers 的 `Trainer` 基础上，额外处理了「把对话数据用 chat template 渲染成 token 序列」「可选的序列打包（packing）」等 SFT 专属逻辑。

open-r1 的脚本在这里做的全部事情，就是**把零件按正确的关键字传给 `SFTTrainer`**。需要特别留意三个「非显而易见」的入参：

- `peft_config`：**参数高效微调（PEFT）** 配置，典型代表是 LoRA。如果给了，训练时只更新少量附加参数，极大降低显存；如果 `None`，就是全参数微调。
- `callbacks`：一组**回调对象**，在训练的特定时机（如每次保存 checkpoint 时）被触发，用于「保存即推送一个 Hub 修订版本」之类的自动化。
- `processing_class`：现代 transformers 用 `processing_class` 来传分词器（旧代码里的 `tokenizer=` 已被它取代）。

#### 4.2.2 核心流程

```text
model（已加载） ─┐
training_args ──┤
train_dataset ──┤  →  SFTTrainer(...)
eval_dataset ───┤       ├─ peft_config = get_peft_config(model_args)   # None 或 LoRA 配置
processing_class┤       └─ callbacks   = get_callbacks(training_args, model_args)
────────────────┘
```

两个工厂函数把「配置对象」转成「训练器需要的对象」：

- `get_peft_config(model_args)`：根据 `model_args` 里是否设置了 `use_peft` / `lora_*` 等字段，返回一个 PEFT 配置或 `None`。
- `get_callbacks(training_args, model_args)`：遍历 `training_args.callbacks`（一个字符串列表），在注册表 `CALLBACKS` 里查到对应的回调类并实例化。

注意 `eval_dataset` 是有条件的：只有当 `eval_strategy != "no"` 时才传入测试集，否则传 `None`，训练器就不会做训练中的评估。这正好对应 `config_distill.yaml` 里 `eval_strategy: 'no'`、`do_eval: false` 的设定。

#### 4.2.3 源码精读

**chat template 兜底**——在组装训练器之前，脚本先检查分词器有没有 chat template，没有就用 ChatML 补上：

[无 chat template 时默认 ChatML](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/sft.py#L94-L96)

```python
if tokenizer.chat_template is None:
    logger.info("No chat template provided, defaulting to ChatML.")
    model, tokenizer = setup_chat_format(model, tokenizer, format="chatml")
```

这一点和 README 里的警告一致：像 Llama-3.2-1B 这样的基础模型没有预置 chat template，会走这条兜底分支；而 Qwen 基础模型自带 chat template，则跳过。

**SFTTrainer 组装**——核心八行：

[Initialize the SFT Trainer 阶段](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/sft.py#L98-L109)

```python
trainer = SFTTrainer(
    model=model,
    args=training_args,
    train_dataset=dataset[script_args.dataset_train_split],
    eval_dataset=(dataset[script_args.dataset_test_split] if training_args.eval_strategy != "no" else None),
    processing_class=tokenizer,
    peft_config=get_peft_config(model_args),
    callbacks=get_callbacks(training_args, model_args),
)
```

几点解读：

- `dataset[script_args.dataset_train_split]`：`dataset_train_split` / `dataset_test_split` 这两个字段继承自 `trl.ScriptArguments`（默认常见取值为 `train` / `test`）。`get_dataset` 返回的是一个 `DatasetDict`，用 split 名取切片。
- `peft_config=get_peft_config(model_args)`：若 `model_args` 未启用 PEFT，返回 `None`，即全参数微调。`config_distill.yaml` 复现 OpenR1-Distill-7B 时就是全参数微调。
- `callbacks=get_callbacks(training_args, model_args)`：把字符串名翻译成回调对象。

**`get_callbacks` 的注册表机制**——回调名 → 回调类的映射集中在一个字典里：

[CALLBACKS 注册表与 get_callbacks 工厂](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/callbacks.py#L80-L92)

```python
CALLBACKS = {
    "push_to_hub_revision": PushToHubRevisionCallback,
}

def get_callbacks(train_config, model_config) -> List[TrainerCallback]:
    callbacks = []
    for callback_name in train_config.callbacks:
        if callback_name not in CALLBACKS:
            raise ValueError(f"Callback {callback_name} not found in CALLBACKS.")
        callbacks.append(CALLBACKS[callback_name](model_config))
    return callbacks
```

这是一种典型的「**字符串名注册表**」模式：YAML 里写 `callbacks: [push_to_hub_revision]`，脚本就能在运行时按名字找到对应实现。后续讲义会讲到奖励函数（`rewards.py`）用的是同一套模式。`config_distill.yaml` 没有设置 `callbacks`，所以这里默认返回空列表，训练期间不会自动推送每个 checkpoint 的修订版本。

#### 4.2.4 代码实践

**实践目标**：通过修改 YAML，亲手让 `SFTTrainer` 走「带评估」和「带回调」两条分支，并验证它们被正确启用。

**操作步骤**：

1. 复制 `recipes/OpenR1-Distill-7B/sft/config_distill.yaml` 为一个本地实验配置 `sft_exp.yaml`。
2. **实验 A（开启训练中评估）**：把 `eval_strategy: 'no'` 改为 `eval_strategy: 'steps'`，并加上 `eval_steps: 10`。注意：这要求数据集有测试集 split；`Mixture-of-Thoughts` 单数据集分支只返回 `train`，所以这一项需要搭配 `dataset_mixture` 的 `test_split_size`（见 `u2-l2`）才能真正生效。
3. **实验 B（启用回调）**：在配置里加一行 `callbacks: ['push_to_hub_revision']`（注意 YAML 列表写法）。
4. 在 `sft.py` 的 `SFTTrainer(...)` 调用处加一条临时日志：`print("callbacks =", get_callbacks(training_args, model_args))`。

**需要观察的现象**：
- 实验 A：当 `eval_strategy != "no"` 时，`eval_dataset` 不再是 `None`；否则为 `None`。
- 实验 B：打印结果应显示一个含 `PushToHubRevisionCallback` 实例的列表；若把名字写错（如 `push_to_hub`），`get_callbacks` 会抛 `ValueError`。

**预期结果**：你能解释「`eval_strategy` 如何决定 `eval_dataset` 是否为 `None`」以及「回调是如何从字符串名变成对象的」。

> 说明：本实践为**源码阅读 + 配置改写型**，实际启动训练需 GPU 与数据集访问，运行结果「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`peft_config=get_peft_config(model_args)` 在什么情况下返回 `None`？返回 `None` 意味着什么？

> **参考答案**：当 `model_args` 里没有启用 PEFT（如未设置 `use_peft=true` 或未提供 `lora_*` 参数）时返回 `None`。返回 `None` 意味着 `SFTTrainer` 做**全参数微调**——更新模型所有参数，显存开销大但效果通常更好；`config_distill.yaml` 走的就是这条路。

**练习 2**：为什么不直接在 `SFTTrainer` 调用处写 `tokenizer=tokenizer`，而要用 `processing_class=tokenizer`？

> **参考答案**：新版 transformers/trl 用 `processing_class` 统一指代「处理输入的组件」（分词器、图像处理器等），旧的 `tokenizer=` 关键字在新版 `SFTTrainer` 中已被 `processing_class` 取代。沿用旧关键字会触发弃用警告甚至报错，所以脚本与时俱进地使用了新参数名。

---

### 4.3 训练健壮性细节：checkpoint 恢复与 generation_config 的 EOS 对齐

#### 4.3.1 概念说明

这一模块讲两个「不显眼但一旦出错就很致命」的细节。它们都不是 SFT 算法本身，而是工程上的**健壮性（robustness）**保障。

**细节一：断点恢复（checkpoint resume）。** 大模型训练动辄几天几夜，中途断电、OOM、节点掉线是常态。如果不支持恢复，每次中断都得从零重训。`sft.py` 提供了两层恢复机制：

- **自动恢复**：每次启动时扫描 `output_dir`，若发现已有 checkpoint，就自动从最近的那个继续。
- **手动指定**：通过 `resume_from_checkpoint` 显式指定从某个 checkpoint 继续。

**细节二：保存阶段对齐 `generation_config.eos_token_id`。** 模型保存时有两份「配置」：`config.json`（模型结构/训练配置）和 `generation_config.json`（推理时的生成参数）。如果 `generation_config` 里的 EOS token 和分词器实际的 EOS token 不一致，推理时模型可能**永远不停止生成（unbounded generation）**——这对后续用 `pipeline()` 做评估是灾难性的。所以脚本在保存前手动对齐了一次。

这两个细节共同回答了一个问题：**「训练中途崩了怎么办」**和**「训练完了评估时不停吐字怎么办」**。

#### 4.3.2 核心流程

**断点恢复的判定逻辑**（`last_checkpoint` 的探测在训练前，真正传入在训练循环）：

```text
启动时：
  if output_dir 是目录:
      last_checkpoint = get_last_checkpoint(output_dir)   # 可能为 None
  if last_checkpoint 存在 且 用户没显式指定 resume_from_checkpoint:
      记日志：检测到 checkpoint，将自动续训

训练循环开始前，决定传给 trainer.train() 的 checkpoint：
  checkpoint = 用户指定的 resume_from_checkpoint   # 优先级最高
            或 last_checkpoint                       # 其次自动探测
            或 None                                  # 从头训
  trainer.train(resume_from_checkpoint=checkpoint)
```

优先级用伪代码表达为：

\[
\text{checkpoint} =
\begin{cases}
\text{resume\_from\_checkpoint}, & \text{若用户显式指定} \\
\text{last\_checkpoint}, & \text{否则，若自动探测到} \\
\text{None}, & \text{否则（从头训练）}
\end{cases}
\]

**EOS 对齐与 use_cache 恢复**（保存阶段）：

```text
保存模型前：
  trainer.model.generation_config.eos_token_id = tokenizer.eos_token_id   # 对齐 EOS
  trainer.save_model(output_dir)
  (仅主进程):
      create_model_card(...)
      trainer.model.config.use_cache = True        # 训练时关了，推理要开回来
      trainer.model.config.save_pretrained(output_dir)
```

为什么要「恢复 `use_cache`」？因为 `get_model` 在开启 `gradient_checkpointing` 时会把 `use_cache` 设成 `False`（梯度检查点和 KV cache 在内存复用上冲突，训练时必须关掉）。但训练结束后的模型是拿去**推理**的，推理需要 KV cache 才快，所以保存前要把它改回 `True` 并落盘。这一行呼应了 `get_model` 里的设置（见 [model_utils.py 的 use_cache 分支](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/model_utils.py#L34)）。

#### 4.3.3 源码精读

**断点探测**——在加载数据之前就先看一眼 `output_dir`：

[Check for last checkpoint](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/sft.py#L77-L82)

```python
last_checkpoint = None
if os.path.isdir(training_args.output_dir):
    last_checkpoint = get_last_checkpoint(training_args.output_dir)
if last_checkpoint is not None and training_args.resume_from_checkpoint is None:
    logger.info(f"Checkpoint detected, resuming training at {last_checkpoint=}.")
```

注意第二个 `if`：只有当用户**没有**显式指定 `resume_from_checkpoint` 时才打这条「自动续训」日志——说明自动探测是「兜底」，用户显式指定优先。

**训练循环里的 checkpoint 决策**：

[Training loop 阶段：决定从哪个 checkpoint 续训](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/sft.py#L111-L125)

```python
checkpoint = None
if training_args.resume_from_checkpoint is not None:
    checkpoint = training_args.resume_from_checkpoint
elif last_checkpoint is not None:
    checkpoint = last_checkpoint
train_result = trainer.train(resume_from_checkpoint=checkpoint)
```

之后是一组标准的「记录训练指标」调用：`log_metrics("train", ...)` 写日志、`save_metrics("train", ...)` 落盘成 `train_results.json`、`save_state()` 保存 trainer 状态（含优化器、调度器，供下次恢复用）。

**保存阶段：EOS 对齐 + use_cache 恢复**：

[Save model：对齐 eos、保存、生成卡片、恢复 use_cache](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/sft.py#L127-L146)

关键三行：

```python
trainer.model.generation_config.eos_token_id = tokenizer.eos_token_id
trainer.save_model(training_args.output_dir)
...
trainer.model.config.use_cache = True
trainer.model.config.save_pretrained(training_args.output_dir)
```

第一行的注释写得很清楚：对齐 EOS 是为了避免 `pipeline()` 里的**无限生成**。这恰好解释了为什么 `config_distill.yaml` 要专门设 `eos_token: <|im_end|>`——它必须和 chat template（ChatML）的结束符一致。

注意 `use_cache` 的恢复包在 `if trainer.accelerator.is_main_process:` 里，只在主进程执行，避免多卡重复写文件。

#### 4.3.4 代码实践

**实践目标**：在不真正训练大模型的前提下，理解「自动断点恢复」的触发条件。

**操作步骤（源码阅读型）**：

1. 阅读 [get_last_checkpoint 的官方行为](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/sft.py#L43)（它来自 `transformers.trainer_utils`，会在 `output_dir` 下找形如 `checkpoint-<step>` 的子目录并返回步数最大的那个）。
2. 想象以下三种启动场景，在笔记里写出 `checkpoint` 变量最终的值：
   - 场景 A：`output_dir` 为空目录，未指定 `resume_from_checkpoint`。
   - 场景 B：`output_dir` 下有 `checkpoint-500`，未指定 `resume_from_checkpoint`。
   - 场景 C：`output_dir` 下有 `checkpoint-500`，但命令行传了 `--resume_from_checkpoint checkpoint-200`。

**需要观察的现象 / 预期结果**：
- 场景 A：`checkpoint = None`（从头训）。
- 场景 B：`checkpoint = checkpoint-500`（自动续训）。
- 场景 C：`checkpoint = checkpoint-200`（用户指定优先，覆盖自动探测）。

**延伸实践（可选，需能运行 Python）**：在一个空目录里手动建两个子目录 `checkpoint-100`、`checkpoint-300`（各放一个空文件），然后调用 `from transformers.trainer_utils import get_last_checkpoint; print(get_last_checkpoint("<那个目录>"))`，验证它返回的是 `checkpoint-300`。结果「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：如果训练时开启了 `gradient_checkpointing`，保存出的模型 `config.json` 里 `use_cache` 是 `True` 还是 `False`？为什么？

> **参考答案**：是 `True`。因为 `get_model` 在 `gradient_checkpointing=True` 时把 `use_cache` 设成了 `False`（仅训练期间），但保存阶段脚本又把它改回 `True` 并调用 `save_pretrained` 落盘。这样保存出来的模型直接用于推理时就能享受 KV cache 加速，而不会把「训练用的关 cache 设置」带到推理端。

**练习 2**：假设你把 `eos_token` 配成了和 chat template 实际结束符不一致的 token，但忘了对齐 `generation_config`。现在 `sft.py` 里的那行对齐代码帮你兜底了。请解释：如果没有这行兜底，评估时会出什么问题？

> **参考答案**：没有这行对齐时，`generation_config.json` 里的 `eos_token_id` 可能仍是模型默认值（与训练用的 `<|im_end|>` 不同）。评估时生成器在真正该停的地方收不到正确的 EOS 信号，于是**持续生成直到达到 max_new_tokens 上限**，产生大段无意义文本，既拖慢评估又拉低指标。脚本的这行对齐正是为了消除这种「训练 EOS 与推理 EOS 不一致」的隐患。

**练习 3**：`trainer.save_state()` 保存了什么？它和 `trainer.save_model()` 的区别是什么？

> **参考答案**：`save_model()` 保存的是**模型权重**（以及分词器、配置）；`save_state()` 保存的是 **trainer 的训练状态**——优化器（optimizer）、学习率调度器（scheduler）和 RNG 状态等。后者正是「断点恢复」能从中间步继续的前提：恢复时需要把这些状态一起加载回来，否则优化器动量丢失会导致训练抖动。

---

## 5. 综合实践：用 Qwen3-0.6B-Base 跑通一次最小 SFT

本任务把全讲内容串起来：从读懂启动命令，到亲手跑（或 dry-run）一次端到端 SFT。

### 5.1 实践目标

- 用一个小模型（`Qwen/Qwen3-0.6B-Base`）和最小超参，验证「数据加载 → 训练器组装 → 训练 → 保存」整条链路能跑通。
- 把第 4 讲学到的「阶段」「三元组配置」「EOS 对齐」「断点恢复」对照到一次真实运行里。

### 5.2 操作步骤

README 里已经给出了「换小模型」的官方示范（见 [README 的 SFT 小模型示例](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/README.md#L139-L146)）。我们在此基础上做「冒烟测试」级别的最小化。

**步骤 1**：确认环境已按 `u1-l3` 安装好（至少能 `import trl, transformers, open_r1`）。

**步骤 2**：基于官方配方，叠加一组「最小化」覆盖参数，得到下面的启动命令（**示例命令**，按你本地算力调整）：

```shell
# 示例命令：在单 GPU 上做冒烟测试（CPU 仅作极小步数验证，非常慢）
ACCELERATE_LOG_LEVEL=info accelerate launch \
    --config_file recipes/accelerate_configs/zero3.yaml \
    src/open_r1/sft.py \
    --config recipes/OpenR1-Distill-7B/sft/config_distill.yaml \
    --model_name_or_path Qwen/Qwen3-0.6B-Base \
    --output_dir data/OpenR1-Distill-0.6B-smoke \
    --hub_model_id OpenR1-Distill-0.6B-smoke \
    --max_length 1024 \
    --num_train_epochs 1 \
    --per_device_train_batch_size 1 \
    --gradient_accumulation_steps 1 \
    --learning_rate 1e-5 \
    --logging_steps 1 \
    --save_strategy no \
    --report_to none \
    --push_to_hub false
```

参数解读（对照本讲学过的概念）：

- `--model_name_or_path`：走 `ModelConfig`，决定 `get_model` / `get_tokenizer` 加载哪个模型。
- `--config`：走 `TrlParser`，把 YAML 作为默认值（`u1-l4`）；后面的 `--xxx` 都是 CLI 覆盖。
- `--max_length 1024`：覆盖配方里的 32768，把每条样本截短，显著降显存。
- `--save_strategy no` + `--push_to_hub false`：冒烟测试不做保存与推送，专注于「能不能训起来」。
- `--report_to none`：关掉 wandb，省去登录。

> **关于数据集**：上面命令沿用配方的 `dataset_name: open-r1/Mixture-of-Thoughts`，体量很大，不适合冒烟测试。若想更快跑通，可改用一个小型对话数据集（需含 `messages` 列以匹配 SFTTrainer 默认格式），例如 `--dataset_name trl-internal-testing/zen`（具体可用性与列名「待本地验证」）。

**步骤 3（CPU 友好兜底，可选）**：在没有 GPU 的机器上，把 `--bf16` 去掉、`--max_length` 降到 256、模型换成更小的（如 `Qwen/Qwen3-0.6B-Base` 已是较小选择），并加 `--max_train_samples 8` 限制样本数。这样能在一两步内验证「装配链路无报错」，但不会得到有意义的模型。结果「待本地验证」。

### 5.3 需要观察的现象

1. 日志开头应依次打印出 `Model parameters`、`Script parameters`、`Training parameters` 三段（对应 4.1 的「Setup logging」末尾）。
2. 出现 `*** Train ***`，随后是逐步的 loss 日志（`logging_steps: 1` 时每步都打）。
3. 由于 `--save_strategy no`，不应出现 checkpoint 写盘；由于 `--push_to_hub false`，末尾不应有 `Pushing to hub...`。
4. 若你改为 `--save_strategy steps`，则能在 `output_dir` 看到 `checkpoint-<step>` 目录——这正是 4.3 里 `get_last_checkpoint` 会扫描的对象。

### 5.4 预期结果

- 链路跑通：训练能正常开始并打印 loss，结束后（若开启保存）在 `output_dir` 得到一份带 `config.json`、`generation_config.json` 的模型目录。
- 你能用一句话指出：这次运行里，「EOS 对齐」「use_cache 恢复」「断点探测」分别对应日志或产物里的什么现象。

> 说明：受算力与数据集访问限制，本实践的完整运行结果「待本地验证」；在没有 GPU 时，重点完成「读懂命令每一项 + dry-run 装配链路」即可。

## 6. 本讲小结

- `sft.py` 是一个**薄脚本**：`main()` 把训练切成八个清晰阶段（日志 → 断点检查 → 加载 → 组装训练器 → 训练 → 保存 → 评估 → 推送），真正的算法全部委托给 trl 的 `SFTTrainer`。
- 入口用 `TrlParser((ScriptArguments, SFTConfig, ModelConfig))` 解析三元组配置，顺序与解包一一对应；`set_seed` 是 `main` 里最先执行的一行，保证可复现。
- `SFTTrainer` 的组装关键在四个入参：`model`/`args`/`processing_class`，以及两个工厂产物 `get_peft_config(...)`（None 即全参微调）和 `get_callbacks(...)`（字符串名 → 回调对象的注册表模式）。
- 断点恢复有两层优先级：用户显式的 `resume_from_checkpoint` > 自动探测的 `last_checkpoint` > 从头训练；`eval_dataset` 是否为 `None` 由 `eval_strategy` 决定。
- 保存阶段有两个健壮性动作：把 `generation_config.eos_token_id` 对齐到分词器 EOS（防止评估时无限生成），以及把训练时关掉的 `use_cache` 改回 `True` 再落盘（保证推理速度）。
- 评估与推送都是**有条件**的：分别由 `do_eval` / `push_to_hub` 控制，体现了「按需开启」的设计。

## 7. 下一步学习建议

本讲只讲了 `sft.py` 的「装配骨架」，但故意把两个零件当成黑盒用了：`get_dataset` 和 `get_model`/`get_tokenizer`。建议接着阅读：

- **`u2-l2 数据集加载与混合`**：拆开 `get_dataset`，看它如何处理单数据集与 `dataset_mixture`（按权重采样、列对齐、train/test 切分），理解本讲里 `dataset[script_args.dataset_train_split]` 的数据是怎么来的。
- **`u2-l3 模型与分词器加载`**：拆开 `get_model` / `get_tokenizer`，看 `torch_dtype`、`attn_implementation`、量化、`use_cache` 与 `gradient_checkpointing` 的关系——这会帮你彻底理解本讲 4.3 里「为什么保存时要恢复 `use_cache`」。
- 之后进入 **第三单元 GRPO**：对比 `grpo.py` 与 `sft.py`，你会发现两者结构几乎一致，唯一本质区别是 GRPO 多注入了一组**奖励函数**——那是 open-r1 最有特色的部分。

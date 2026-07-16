# GRPO 训练脚本主流程

## 1. 本讲目标

读完本讲，你应当能够：

- 说出 `grpo.py` 的 `main()` 由哪些阶段组成，以及它与 `sft.py` 在结构上的异同。
- 解释 `make_conversation` 如何把数据集里的某一列（例如 `problem`）包装成 GRPOTrainer 需要的 `prompt` 对话。
- 理解 `GRPOTrainer` 的初始化参数，尤其是 `reward_funcs` 是怎么从「字符串名」一路变成「可调用函数」被注入训练器的。
- 读懂 `config_demo.yaml`，并能解释 `use_vllm` 与 `num_generations` 这两个 GRPO 特有的关键开关到底在做什么。

本讲是强化学习单元（Unit 3）的第一篇，它只讲「脚本主流程」这条骨架；奖励函数本身的实现细节留给 `u3-l2`～`u3-l4`。

## 2. 前置知识

在进入源码前，先用最朴素的方式建立两个直觉。

**直觉一：SFT 与 RL 的数据形态不同。**
SFT（监督微调）的数据是「问题 + 标准答案」对，模型学的是「照着答案复述」。RL（强化学习）的数据只有「问题」，模型自己采样出若干个「回答」，再用一个**奖励函数（reward function）**给每个回答打分，分数高的回答被鼓励、分数低的被抑制。因此 SFT 的数据需要完整的「问 + 答」，而 GRPO 的数据只需要「问」——这就是为什么 `grpo.py` 里要专门写一个 `make_conversation` 把「问题文本」加工成「对话形式的 prompt」。

**直觉二：GRPO 的「Group」是什么。**
GRPO 全称 **Group Relative Policy Optimization（组内相对策略优化）**。对每一个问题（prompt），模型不是只采样 1 个回答，而是采样 G 个回答组成一个「组（group）」。然后计算组内每个回答的奖励，再**在组内做归一化**得到优势（advantage）。归一化公式为：

\[
A_i = \frac{r_i - \mathrm{mean}(r_1, \dots, r_G)}{\mathrm{std}(r_1, \dots, r_G)}
\]

这样就不需要像 PPO 那样额外训练一个价值模型（value model）。这里的 G 就是配方里的 `num_generations`——它直接决定了「组」有多大，所以是 GRPO 最核心的超参数之一。后面 4.4 会看到它的工程含义。

如果你对「三元组配置（ScriptArguments / TrainingConfig / ModelConfig）」「TrlParser 把 YAML 与命令行合并」还不熟，请先读 `u1-l4`；对 `sft.py` 的八个阶段不熟，请先读 `u2-l1`。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `src/open_r1/grpo.py` | GRPO 训练入口脚本（带 `__main__`），本讲主角。它很「薄」，只做组装，真正的 RL 循环由 trl 的 `GRPOTrainer` 实现。 |
| `src/open_r1/sft.py` | SFT 训练入口脚本，本讲用作「对照基线」，帮助你看清 GRPO 多了什么、少了什么。 |
| `src/open_r1/rewards.py` | 奖励函数库与注册表 `get_reward_funcs`。本讲只关注它「如何被调用」，函数实现细节留给后续讲义。 |
| `src/open_r1/configs.py` | 定义 `GRPOScriptArguments`、`GRPOConfig` 等配置类。 |
| `recipes/Qwen2.5-1.5B-Instruct/grpo/config_demo.yaml` | 一份可直接跑的 GRPO 配方，本讲用它讲解 `use_vllm` 与 `num_generations`。 |

## 4. 核心概念与源码讲解

### 4.1 main 函数的阶段划分：与 SFT 对照

#### 4.1.1 概念说明

`grpo.py` 与 `sft.py` 一样，走的是 open-r1 一贯的「薄脚本」风格：自己不写训练算法，只负责「解析配置 → 加载零件 → 组装 Trainer → 训练 → 保存 → 推送」。`main()` 接收三个对象，正是三元组配置解包后的产物：

[src/open_r1/grpo.py:L35-L37](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/grpo.py#L35-L37) —— `main(script_args, training_args, model_args)`，首行 `set_seed(training_args.seed)` 固定随机种子以保证 RL 采样可复现。

#### 4.1.2 核心流程

`grpo.py` 的 `main()` 可以切成下面几个阶段。左边标了「与 SFT 是否相同」，方便对照：

```
1. 配置日志            （与 SFT 几乎逐字相同）
2. 检测断点 checkpoint （与 SFT 相同）
3. wandb 初始化        （与 SFT 相同）
4. 加载数据 get_dataset（与 SFT 相同：黑盒，见 u2-l2）
5. 加载分词器 get_tokenizer（与 SFT 相同：黑盒，见 u2-l3）
6. 加载模型 get_model  （与 SFT 相同：黑盒，见 u2-l3）
7. 【GRPO 独有】取奖励函数 get_reward_funcs(script_args)
8. 【GRPO 独有】make_conversation：把数据列加工成 prompt 对话
9. 【GRPO 独有】删除多余的 messages 列
10. 组装 GRPOTrainer   （多了 reward_funcs 参数）
11. 训练 train()       （与 SFT 相同）
12. 保存 / 对齐 eos    （与 SFT 相同）
13. 评估 do_eval       （与 SFT 相同）
14. 推送 push_to_hub   （与 SFT 相同）
```

一句话概括：**阶段 1–6 与 11–14 几乎是从 `sft.py` 复制过来的；阶段 7–10 是 GRPO 的「增量」。**

#### 4.1.3 源码精读

入口与三元组解析。注意解析顺序是 `(GRPOScriptArguments, GRPOConfig, ModelConfig)`，与解包顺序一一对应：

[src/open_r1/grpo.py:L178-L181](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/grpo.py#L178-L181) —— `TrlParser((GRPOScriptArguments, GRPOConfig, ModelConfig))` 解析三个 dataclass，`parse_args_and_config()` 把 YAML 配方作为默认值、命令行作覆盖，再按字段名路由到对应 dataclass。

与 `sft.py` 对照，导入层的关键差异只有一处——多导入奖励函数工厂，少导入 chat template 工具：

[src/open_r1/grpo.py:L24-L29](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/grpo.py#L24-L29) —— 多了 `from open_r1.rewards import get_reward_funcs`；而 `sft.py` 里有的 `setup_chat_format` 在这里**没有**导入（原因见 4.2）。

后半段「训练 / 保存 / 评估 / 推送」与 `sft.py` 基本一致，这里只列一个代表性片段，说明连健壮性细节（对齐 eos、恢复 use_cache）都一模一样：

[src/open_r1/grpo.py:L142-L147](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/grpo.py#L142-L147) —— 保存前把 `generation_config.eos_token_id` 对齐到分词器的 eos，避免后续用 `pipeline()` 评估时无限生成（注释原话：`to avoid unbounded generation`）。

#### 4.1.4 代码实践

> **实践目标：** 用肉眼完成 `grpo.py` 与 `sft.py` 的逐阶段 diff，建立「增量只在数据准备与 Trainer 组装」的直觉。

1. **操作步骤：** 同时打开 `src/open_r1/grpo.py` 与 `src/open_r1/sft.py`，按 `main()` 从上到下逐行比对。
2. **需要观察的现象：** 日志、checkpoint、`get_dataset`、`get_model`、`trainer.train()`、`save_model`、`evaluate`、`push_to_hub` 这些段落几乎逐字相同；真正不同的是中间「奖励函数 + make_conversation + GRPOTrainer」那一段。
3. **预期结果：** 你能在一张纸上画出两份脚本的阶段对照表，标出 3 处以上差异（导入、make_conversation、reward_funcs）。
4. **结论：** open-r1 把 SFT 与 RL 的「外壳」做成高度一致，差异被压缩到最小，这正是「simple by design」的体现。

> 说明：本实践为源码阅读型实践，无需运行任何命令；如果你想在本地跑命令验证，参考 4.4 的综合实践。

#### 4.1.5 小练习与答案

**练习 1：** 为什么 `grpo.py` 的 `main()` 第一行就要 `set_seed`，而普通监督学习脚本有时会省略？
**答案：** GRPO 每一步都要对同一 prompt 采样 G 个回答，采样带有随机性；不固定种子则每次运行采到的样本不同，奖励、优势、最终模型都会变，无法复现。

**练习 2：** `TrlParser((GRPOScriptArguments, GRPOConfig, ModelConfig))` 里三个 dataclass 的顺序能随便调换吗？
**答案：** 不能。`parse_args_and_config()` 返回的三元组与传入顺序一一对应，调用方 `script_args, training_args, model_args = ...` 按位置解包，顺序错了会把配置对象张冠李戴。

### 4.2 make_conversation：把数据集列转成 prompt 对话

#### 4.2.1 概念说明

TRL 的 `GRPOTrainer` 要求训练集里有一列叫 `prompt`，其内容是「对话格式的 prompt」——也就是一个消息列表（list of message dict），例如：

```python
[
    {"role": "system", "content": "你是一个有用的助手……"},
    {"role": "user",   "content": "求 x^2 + 2x + 1 = 0 的根。"},
]
```

但原始数学数据集（如 `open-r1/OpenR1-Math-220k`）里通常只有一列纯文本问题（例如列名 `problem`）。`make_conversation` 就是这道「把纯文本问题 → 对话格式 prompt」的转换工序。它还顺便把可选的 `system_prompt`（系统提示词）拼到最前面。

> 为什么 `grpo.py` 自己做这个转换，而 `sft.py` 不做？因为 SFT 的数据自带完整对话（问 + 答），交给 `SFTTrainer` 即可；GRPO 只需要「问」，且要把 system_prompt 稳定地注入每个样本，所以单独写了一个 `map` 函数。

#### 4.2.2 核心流程

`make_conversation` 的伪代码：

```
对数据集里的每个 example：
    prompt = []
    若 training_args.system_prompt 不为空：
        prompt.append({"role": "system", "content": system_prompt})
    若 example 里没有 prompt_column 这一列：
        报错
    prompt.append({"role": "user", "content": example[prompt_column]})
    返回 {"prompt": prompt}          # 新增一列 prompt
整个 dataset = dataset.map(make_conversation)
```

关键点有三：

1. **system_prompt 可选**：配方里没给 `system_prompt` 时，`prompt` 列表里就只有一条 user 消息。
2. **列名校验**：若 `prompt_column`（默认 `problem`）不在 example 中，直接 `raise ValueError`，避免静默地拿到空内容。
3. **闭包捕获**：`make_conversation` 是定义在 `main()` 内部的嵌套函数，它的默认参数 `prompt_column = script_args.dataset_prompt_column` 在函数定义那一刻就被求值，捕获了 `script_args`。

转换之后，原始数据集可能还残留一列 `messages`（即「问 + 答」的完整对话）。GRPO 只认 `prompt`，留着 `messages` 会让 Trainer 困惑，所以脚本会把它删掉。

#### 4.2.3 源码精读

`make_conversation` 的定义与组装逻辑：

[src/open_r1/grpo.py:L90-L103](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/grpo.py#L90-L103) —— 依次尝试拼 system 消息、校验列名、拼 user 消息，最后 `dataset = dataset.map(make_conversation)` 给每个样本加上 `prompt` 列。

删除多余 `messages` 列：

[src/open_r1/grpo.py:L105-L107](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/grpo.py#L105-L107) —— 遍历每个 split，若存在 `messages` 列就 `remove_columns("messages")`，确保只剩 GRPO 需要的 `prompt` 列。

`system_prompt` 与 `dataset_prompt_column` 这两个参数分别来自哪个配置类？看 `configs.py`：

- `system_prompt` 定义在 `GRPOConfig`（训练参数类）：[src/open_r1/configs.py:L145-L148](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py#L145-L148)。
- `dataset_prompt_column` 定义在 `GRPOScriptArguments`（脚本参数类，默认 `"prompt"`）：[src/open_r1/configs.py:L293-L296](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py#L293-L296)。

这正好呼应 `u1-l4` 强调的能力：拿到任一字段，能判断它归 `ScriptArguments` 还是 `TrainingConfig`。这里 `make_conversation` 同时读了两边——`training_args.system_prompt` 与 `script_args.dataset_prompt_column`。

#### 4.2.4 代码实践

> **实践目标：** 用一行代码复现 `make_conversation` 的核心效果，理解输入输出形态。

1. **操作步骤：** 写一段独立的示例代码（非项目原有代码，标注为「示例代码」），手动构造一个 `example` 并调用类似逻辑：

   ```python
   # 示例代码：演示 make_conversation 的输入输出，非项目源码
   example = {"problem": "求 2 + 2 的值。", "solution": "4"}
   system_prompt = "You are a helpful assistant."

   prompt = []
   if system_prompt is not None:
       prompt.append({"role": "system", "content": system_prompt})
   prompt.append({"role": "user", "content": example["problem"]})
   print({"prompt": prompt})
   ```

2. **需要观察的现象：** 输出是一个字典，`prompt` 列表里先 system 后 user；`solution` 这一列**不会**出现在 `prompt` 里（GRPO 不需要答案）。
3. **预期结果：** `prompt` 长度为 2（有 system_prompt 时）或 1（无 system_prompt 时）。
4. **若把 `example` 改成不含 `problem` 键：** 对应到真实脚本会触发 `ValueError`（见 L97-L98）。

#### 4.2.5 小练习与答案

**练习 1：** 如果配方里把 `dataset_prompt_column` 设成了一个数据集中不存在的列名，运行到哪一步会报错？
**答案：** 在 `dataset.map(make_conversation)` 执行时，`make_conversation` 内部走到 `if prompt_column not in example` 分支，`raise ValueError`。

**练习 2：** 为什么 GRPO 要删掉 `messages` 列，而不是连答案一起喂给训练器？
**答案：** GRPO 是 RL，模型要自己生成回答再被奖励函数打分；数据集里的「答案」是通过奖励函数（如 `accuracy_reward` 读取 `solution` 列）间接参与的，而不是作为监督信号直接喂进 Trainer。保留 `messages` 反而可能让 Trainer 误判数据格式。

**练习 3：** `make_conversation` 的默认参数 `prompt_column = script_args.dataset_prompt_column` 在何时被求值？
**答案：** 在 `def make_conversation(...)` 这一行被执行的那一刻（函数定义时），而非每次调用时。它是闭包对 `script_args` 的捕获。

### 4.3 GRPOTrainer 的初始化与 reward_funcs 注入

#### 4.3.1 概念说明

组装好数据后，就到了「把模型、数据、奖励函数一起交给 `GRPOTrainer`」这一步。与 `SFTTrainer` 相比，`GRPOTrainer` 最显眼的新参数是 `reward_funcs`——一个「奖励函数列表」。这些函数将在训练循环里被调用：模型采样的每个回答都会被它们打分，分数再归一化成优势去更新策略。

那 `reward_funcs` 从哪来？它来自 `get_reward_funcs(script_args)`。这个函数内部维护了一张**注册表（registry）**——一个「字符串名 → 函数」的字典。配方里写的 `reward_funcs: [accuracy, format, tag_count]` 只是字符串列表；`get_reward_funcs` 把这些字符串查表翻译成真正的 Python 函数，再交给 Trainer。这是 open-r1 里典型的「字符串 → 可调用对象」注册表模式，和回调（`get_callbacks`）是同一套思路。

当有多个奖励函数时，TRL 会把它们加权求和成单个标量奖励。设第 k 个函数的权重为 \(w_k\)、对第 i 个回答的得分为 \(r_i^{(k)}\)，则该回答的总奖励为：

\[
r_i = \sum_{k} w_k \, r_i^{(k)}
\]

配方里的 `reward_weights: [1.0, 1.0, 1.0]` 就是这些 \(w_k\)。

#### 4.3.2 核心流程

奖励函数从配置到训练器的链路：

```
config_demo.yaml
   reward_funcs: [accuracy, format, tag_count]   ← 字符串列表
        │
        ▼  TrlParser 路由
GRPOScriptArguments.reward_funcs                  ← list[str]
        │
        ▼  get_reward_funcs(script_args)
查 REWARD_FUNCS_REGISTRY 把每个名字翻译成函数      ← list[Callable]
        │
        ▼  grpo.py:88
reward_funcs = get_reward_funcs(script_args)
        │
        ▼  grpo.py:112-121
GRPOTrainer(model=..., reward_funcs=reward_funcs, ...)
```

注册表里有些函数是「现成」的（如 `accuracy`、`format`），有些是「带参工厂」产出的（如 `cosine`、`repetition_penalty`），后者会用 `script_args` 里的超参（如 `cosine_max_len`）先生成一个定制的函数再登记进表。

#### 4.3.3 源码精读

取奖励函数这一行，是 GRPO 区别于 SFT 的核心：

[src/open_r1/grpo.py:L87-L88](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/grpo.py#L87-L88) —— `reward_funcs = get_reward_funcs(script_args)`，把脚本参数里的奖励名列表翻译成函数列表。

`GRPOTrainer` 的组装（对比 `sft.py` 的 `SFTTrainer`，多了一个 `reward_funcs` 参数）：

[src/open_r1/grpo.py:L111-L121](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/grpo.py#L111-L121) —— 传入 `model`、`reward_funcs`、`args`、训练/评估数据集、`peft_config`、`callbacks`、`processing_class=tokenizer`。其余参数（如 `use_vllm`、`num_generations`）都通过 `args=training_args`（即 `GRPOConfig`）隐式传给 Trainer。

注册表本体（本讲只看「调用入口」，函数实现留给后续讲义）：

[src/open_r1/rewards.py:L646-L706](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L646-L706) —— `get_reward_funcs` 内部构造 `REWARD_FUNCS_REGISTRY` 字典，最后用列表推导 `[REWARD_FUNCS_REGISTRY[func] for func in script_args.reward_funcs]` 按名取函数。注意 `cosine`、`repetition_penalty`、`code` 等条目是用工厂函数（如 `get_cosine_scaled_reward(...)`）当场生成的，把 `script_args` 的超参「焊」进了返回的函数里。

回调注册表（同样的注册表模式，作对照）：

[src/open_r1/utils/callbacks.py:L80-L92](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/callbacks.py#L80-L92) —— `CALLBACKS = {"push_to_hub_revision": PushToHubRevisionCallback}` 与 `get_callbacks` 按名查表，逻辑结构与 `get_reward_funcs` 完全同构。

#### 4.3.4 代码实践

> **实践目标：** 在不启动训练的前提下，验证「字符串名 → 函数」的翻译链确实可工作。

1. **操作步骤（源码阅读型实践）：** 打开 `src/open_r1/rewards.py` 的 `get_reward_funcs`（L646-L706），找到注册表里每个名字对应的函数或工厂。
2. **需要观察的现象：** `accuracy`/`format`/`tag_count` 直接指向已定义函数；`cosine`/`repetition_penalty` 是用 `script_args` 超参现场生成的；`code`/`ioi_code` 等用了 `partial` + `update_wrapper` 把默认参数「焊」上去。
3. **预期结果：** 你能解释「为什么把 `reward_funcs` 改成 `["cosine", "length"]` 后，脚本仍能跑通」——因为这两个名字都在注册表里。
4. **若改成一个不在表里的名字（如 `"my_reward"`）：** `[REWARD_FUNCS_REGISTRY[func] ...]` 会抛 `KeyError`，这是排错时常见的报错点。

> 说明：本实践为阅读型实践，不依赖 GPU；若想真正调用 `get_reward_funcs`，需要先构造一个带 `reward_funcs` 等字段的 `GRPOScriptArguments` 对象（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1：** 配方里 `reward_funcs: [accuracy, format, tag_count]` 与 `reward_weights: [1.0, 1.0, 1.0]` 是什么关系？
**答案：** 前者是「选哪几个奖励函数」，后者是「每个函数的权重」。最终每个回答的总奖励是三者的加权和；这里三者等权，即简单相加。

**练习 2：** 为什么注册表里 `cosine` 对应的不是 `get_cosine_scaled_reward` 本身，而是 `get_cosine_scaled_reward(...)` 的**调用结果**？
**答案：** `get_cosine_scaled_reward` 是工厂，它读取 `script_args.cosine_max_len` 等超参后，返回一个「已经焊好超参」的内层函数 `cosine_scaled_reward`。注册表登记的是这个内层函数，这样 Trainer 调用时无需再传超参。

**练习 3：** `GRPOTrainer` 的 `use_vllm`、`num_generations` 在 `grpo.py` 里并没有显式写出来，它们是怎么传进去的？
**答案：** 通过 `args=training_args`（即 `GRPOConfig` 对象）隐式传入。`GRPOConfig` 继承自 `trl.GRPOConfig`，`use_vllm`、`num_generations` 是其字段，`GRPOTrainer` 内部会从 `args` 里读取。

### 4.4 读懂 config_demo.yaml：use_vllm 与 num_generations

#### 4.4.1 概念说明

GRPO 比普通微调「重」很多，原因在于：每一步训练之前，都要对每个 prompt 采样 G 个回答。生成是 GRPO 的主开销。因此配方里有两个关键开关：

- **`use_vllm`**：是否用 vLLM 这个高性能推理引擎来做采样生成。开启后，`GRPOTrainer` 会在后台拉起一个 vLLM 服务（或连接既有的 vLLM 节点），用连续批处理（continuous batching）大幅加速采样。关闭则退回 HuggingFace 原生的 `generate`，慢得多。在 open-r1 的数学/代码配方里，`use_vllm` 基本都是 `true`。
- **`num_generations`（G）**：每个 prompt 采样几个回答组成「组」。它直接决定 RL 的「组」大小，也决定了一个 batch 里有多少**不同的 prompt**。关系是：

\[
\text{unique\_prompts\_per\_batch} = \frac{\text{total\_samples\_per\_batch}}{G}
\]

  其中 `total_samples_per_batch = num_gpus × gradient_accumulation_steps × per_device_train_batch_size`。

#### 4.4.2 核心流程

以 `config_demo.yaml` 为例做笔算：

- `per_device_train_batch_size: 16`、`num_generations: 16`。
- 单卡每步总共 16 条样本，全部来自同一批 prompt 的采样。于是：

\[
\text{unique\_prompts\_per\_device\_per\_step} = 16 / 16 = 1
\]

  即「单卡每步只喂 1 个 prompt，让它生成 16 个回答」。这也意味着 `per_device_train_batch_size` 必须能被 `num_generations` 整除，否则无法把样本整齐地划分成若干组。

仓库里另一份配方 `config_codeforces.yaml` 用注释把这个算式写得很直白，可直接作为权威依据：

> `total_samples_per_batch = num_gpus * grad_accumulation_steps * per_device_batch_size = 8 * 32 * 4 = 1024`
> `unique_prompts_per_batch = total_samples_per_batch / num_generations = 1024 / 16 = 64`

#### 4.4.3 源码精读

`config_demo.yaml` 顶部的模型与数据参数：

[recipes/Qwen2.5-1.5B-Instruct/grpo/config_demo.yaml:L1-L10](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/Qwen2.5-1.5B-Instruct/grpo/config_demo.yaml#L1-L10) —— `model_name_or_path`/`torch_dtype`/`attn_implementation` 属 ModelConfig；`dataset_name`/`dataset_prompt_column`/`system_prompt` 属脚本参数（`dataset_prompt_column: problem` 正是 `make_conversation` 读取的列）。

GRPO 训练器配置与两个关键开关：

[recipes/Qwen2.5-1.5B-Instruct/grpo/config_demo.yaml:L12-L32](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/Qwen2.5-1.5B-Instruct/grpo/config_demo.yaml#L12-L32) —— `use_vllm: true`、`num_generations: 16`，以及 `max_prompt_length: 512`、`max_completion_length: 1024`（采样时的长度上限）。

奖励函数与权重（呼应 4.3）：

[recipes/Qwen2.5-1.5B-Instruct/grpo/config_demo.yaml:L41-L48](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/Qwen2.5-1.5B-Instruct/grpo/config_demo.yaml#L41-L48) —— `reward_funcs: [accuracy, format, tag_count]` 与等权 `reward_weights`。

权威算式注释（来自 codeforces 配方，可作为 `num_generations` 语义的旁证）：

[recipes/Qwen2.5-Coder-7B-Instruct/grpo/config_codeforces.yaml:L49-L50](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/Qwen2.5-Coder-7B-Instruct/grpo/config_codeforces.yaml#L49-L50) —— 明确写出 `unique_prompts_per_batch = total_samples_per_batch / num_generations`。

#### 4.4.4 代码实践

> **实践目标：** 用 `config_demo.yaml` 的数字，亲手算出「每步喂几个 prompt」，把 `num_generations` 的工程含义落到实处。

1. **操作步骤：** 取 `per_device_train_batch_size = 16`、`num_generations = 16`，按上面的公式计算单卡每步的 unique prompt 数；再假设你用 8 卡、`gradient_accumulation_steps = 4`，算一个全局步的 unique prompt 数。
2. **需要观察的现象：** 单卡每步 = 16 / 16 = 1 个 prompt；8 卡 × 4 累积 = 32 个「设备步」，每设备步 1 个 prompt，故一个全局优化步采样自 8×4 = 32 个不同 prompt、共 32 × 16 = 512 条回答。
3. **预期结果：** 你能解释「为什么把 `num_generations` 从 16 调到 8，每步看到的 prompt 会翻倍、但每组的归一化信号变弱」。
4. **若 `per_device_train_batch_size` 不能被 `num_generations` 整除：** TRL 的 `GRPOTrainer` 会在初始化校验时报错（待本地验证具体报错文案）。

> 说明：本实践为「参数推演型」，无需运行；如需真跑，需 8×GPU 且安装了 vLLM，超出本讲范围。

#### 4.4.5 小练习与答案

**练习 1：** 为什么 GRPO 几乎总开 `use_vllm`，而 SFT 配方里没有这个开关？
**答案：** GRPO 每步都要大量采样（G 个回答 × 多个 prompt），生成是性能瓶颈，vLLM 的连续批处理能成倍加速；SFT 不需要在线采样（答案已在数据里），所以没有这个开关。

**练习 2：** 若 `per_device_train_batch_size = 16`、`num_generations = 8`，单卡每步有几个 unique prompt？
**答案：** 16 / 8 = 2 个。

**练习 3：** 把 `num_generations` 设得很大（如 64）会有什么好处与坏处？
**答案：** 好处：组更大，组内归一化的优势估计更稳定。坏处：每步采样与打分开销成倍上升，且每步能覆盖的 unique prompt 变少，数据吞吐下降。

## 5. 综合实践

把本讲的知识串起来，完成一项「对比 + 解释」任务（对应本讲规格中的代码实践任务）：

1. **对比 `sft.py` 与 `grpo.py`，列出至少三处关键差异。** 参考答案（你应自行从源码核实）：
   - **导入层**：`grpo.py` 多 `from open_r1.rewards import get_reward_funcs`，且**不**导入 `setup_chat_format`；配置类用 `(GRPOScriptArguments, GRPOConfig)` 而非 `(ScriptArguments, SFTConfig)`。
   - **数据准备层**：`grpo.py` 用 `make_conversation` 把文本列加工成 `prompt` 对话、并删除多余 `messages` 列；`sft.py` 直接把数据集交给 `SFTTrainer`，并在分词器无 chat template 时用 `setup_chat_format` 兜底——`grpo.py` 没有这一步。
   - **Trainer 初始化层**：`GRPOTrainer` 多传 `reward_funcs=reward_funcs`；`SFTTrainer` 没有（也无从接收）奖励函数。
2. **解释 `config_demo.yaml` 中 `num_generations: 16` 的作用。** 参考答案：它规定每个 prompt 采样 16 个回答组成一个「组」，GRPO 在组内做奖励归一化算优势；同时它决定了每步 batch 里有几个不同 prompt（`unique = batch / num_generations`），本配方里单卡每步 = 1 个 prompt。
3. **画出数据链路：** `dataset_name` → `get_dataset` → `make_conversation` 产出 `prompt` 列 → `GRPOTrainer(train_dataset=...)`；以及奖励链路：`reward_funcs`（YAML 字符串）→ `get_reward_funcs` 查注册表 → `GRPOTrainer(reward_funcs=...)`。

> 说明：以上为源码阅读 + 参数推演型综合实践，不依赖 GPU。如需在本地真正启动一次最小 GRPO，需要 vLLM 与多卡环境（参考 `slurm/train.slurm` 与 `u7-l1`），超出本讲范围。

## 6. 本讲小结

- `grpo.py` 与 `sft.py` 共用一套「薄脚本」外壳：日志、checkpoint、`get_dataset/get_model/get_tokenizer`、训练、保存、评估、推送几乎逐字相同。
- 真正的「GRPO 增量」只有三段：`get_reward_funcs`（取奖励函数）、`make_conversation`（构造 prompt 对话）、`GRPOTrainer(reward_funcs=...)`（注入奖励）。
- `make_conversation` 把数据集的某一列（默认按 `dataset_prompt_column`，本配方是 `problem`）包成 `[system?, user]` 消息列表，写入新列 `prompt`，并删掉无用的 `messages` 列。
- `reward_funcs` 走「YAML 字符串 → `GRPOScriptArguments.reward_funcs` → `get_reward_funcs` 查注册表 → 可调用函数」的链路被注入 Trainer；多函数时按 `reward_weights` 加权求和。
- `use_vllm: true` 用 vLLM 加速 GRPO 最昂贵的「在线采样」环节；`num_generations` 决定每个 prompt 采样几个回答（组大小），并隐含约束 `per_device_train_batch_size % num_generations == 0`。

## 7. 下一步学习建议

本讲只把奖励函数当成「黑盒」拿来注入。要真正理解 GRPO 的「信号」从哪来，建议接着读：

- **`u3-l2` 奖励函数注册表与数学正确性奖励**：精读 `rewards.py` 的 `get_reward_funcs` 注册表结构与 `accuracy_reward` 如何用 `math_verify` 解析 LaTeX 判分。
- **`u3-l3` 格式与推理过程奖励**：`format_reward`、`tag_count_reward`、`reasoning_steps_reward` 的正则与部分计分机制。
- **`u3-l4` 长度、余弦调度与重复惩罚奖励**：`len_reward`、`get_cosine_scaled_reward`、`get_repetition_penalty_reward` 如何控制生成长度与抑制重复。

读完这三篇，你就能自己改 `config_demo.yaml` 的 `reward_funcs` 列表并理解每一个名字背后的打分逻辑。若你更关心「怎么把 GRPO 跑到集群上」，可直接跳到 **`u7-l1` Slurm 集群训练与 vLLM 服务**，那里会讲 `train.slurm` 如何根据 `use_vllm` 拆分训练节点与 vLLM 服务节点。

# 参数体系与配置流转

## 1. 本讲目标

在前几讲里，我们已经知道 `run.py` 会调用 `get_args()` 拿到一组参数，再分派到具体任务；也知道 P-tuning v2 有三个关键开关——`--prefix`（开启深度前缀）、`--pre_seq_len`（前缀长度）、`--prefix_projection`（是否给前缀加 MLP 头）。但它们究竟是怎么从「命令行一行字符串」变成「模型构造时能读到的 `config` 对象」的？本讲就专门讲清这条「配置流转」管道。

学完本讲，你应当能够：

- 说出 `HfArgumentParser` 如何把一个 `@dataclass` 自动翻译成一组命令行参数。
- 看懂 `arguments.py` 里四组参数（模型 / 数据 / 训练 / 问答）各自的职责边界。
- 完整追踪一条命令：`--pre_seq_len 128` 是怎么最终被 `PrefixEncoder` 用到的。
- 区分「参数对象 `args`」和「模型配置对象 `config`」为什么是两套东西，以及二者在哪里被「粘合」。

## 2. 前置知识

- **命令行参数（CLI argument）**：在终端里以 `--名字 值` 形式传给程序的外部输入，例如 `--pre_seq_len 128`。Python 标准库 `argparse` 负责解析它们。
- **dataclass**：Python 的语法糖，用 `@dataclass` 装饰一个类，只需写字段（如 `pre_seq_len: int = field(default=4)`），解释器会自动生成构造函数。
- **HfArgumentParser**：HuggingFace 提供的解析器，是 `argparse` 的封装。它能「读一个 dataclass 类，自动生成对应的命令行参数」，免去手写大量 `parser.add_argument(...)`。
- **config 对象**：每个预训练模型（如 `roberta-large`）都附带一个配置对象（`AutoConfig`），记录层数、隐藏维、dropout 等结构信息。模型类的 `__init__` 从这个 `config` 读取一切超参来搭建网络。
- **为什么参数和配置是两套**：`args` 是「这次训练的运行设定」，`config` 是「模型自身的结构描述」。P-tuning v2 的前缀长度既属于「这次怎么调」，又必须进入「模型结构」（因为要凭它建 `PrefixEncoder`），所以才需要在两者之间做一次「拷贝」。这正是本讲的灵魂。

> 承接前置讲义：u1-l3 已经介绍过 `get_args()` 返回固定顺序的「四元组」、`TASKS`/`DATASETS` 注册表与 `run.py` 的分派；u2-l1 介绍过 `PrefixEncoder` 从 `config.pre_seq_len` 等字段读取参数。本讲负责把这两端中间的「管道」补全。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `arguments.py` | 用四个 `@dataclass` 定义全部命令行参数；`get_args()` 解析并返回四元组。 |
| `model/utils.py` | `get_model()` 模型工厂。它把 `model_args` 上的前缀相关字段**手动拷贝**到 `config` 上，再据此构造模型。 |
| `model/prefix_encoder.py` | `PrefixEncoder` 在 `__init__` 里从 `config` 读取 `pre_seq_len`、`prefix_projection` 等字段，是配置链条的终点消费方。 |
| `model/sequence_classification.py` | 前缀分类模型的 `__init__`，演示 `config.pre_seq_len` 如何被读出并交给 `PrefixEncoder`。 |
| `run.py` / `tasks/superglue/get_trainer.py` | 串联入口：`get_args()` → 分派 → `get_trainer(args)` → 构造 `config` → `get_model(...)`。 |

## 4. 核心概念与源码讲解

### 4.1 HfArgumentParser 机制与 get_args 返回四元组

#### 4.1.1 概念说明

P-tuning v2 仓库有几十个可调参数：模型路径、任务名、前缀长度、学习率、batch size…… 如果用裸 `argparse`，就得手写几十行 `parser.add_argument("--pre_seq_len", type=int, default=4, ...)`，既啰嗦又容易写错。

`HfArgumentParser` 的思路是：**把参数按「职责」分组成若干个 `@dataclass` 类，每个字段就是一条参数**。解析器扫一遍这些类，自动生成对应的命令行选项（字段名里的下划线 `_` 自动变成命令行的 `-`），并把用户在终端输入的值填回 dataclass 实例。

这样做有两个好处：

1. **声明式**：看一个 dataclass 就等于看了一份参数清单，类型、默认值、帮助文字一目了然。
2. **分组复用**：四组参数各自独立，不同的 `get_trainer` 可以只解包自己关心的那一组。

#### 4.1.2 核心流程

`get_args()` 的执行流程非常简洁：

1. 用「四元组 `(ModelArguments, DataTrainingArguments, TrainingArguments, QuestionAnwseringArguments)`」初始化一个 `HfArgumentParser`。
2. 调用 `parse_args_into_dataclasses()`，解析器读 `sys.argv`（即终端传进来的 `--xxx`），把值填进四个 dataclass 实例。
3. 把这四个实例按**固定顺序**打包成一个元组返回。

> 关键点：返回的是「元组」，顺序永远是「模型 → 数据 → 训练 → 问答」。下游无论是 `run.py` 还是某个 `get_trainer`，都用同样的解包顺序拿到自己要的那几个对象。这就是 u1-l3 提到的「四元组」。

#### 4.1.3 源码精读

`get_args()` 的全部实现只有三行，但它是整个参数体系的总入口：

[arguments.py:L187-L193](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/arguments.py#L187-L193) — 用四元组初始化解析器并返回解析后的参数对象列表（顺序固定为模型/数据/训练/问答）。

注意第 189 行传入的是一个**类组成的元组**，而不是某个具体实例。`HfArgumentParser` 会据此推断出全部命令行选项。第 191 行的 `parse_args_into_dataclasses()` 返回的是「实例」列表（已填充了用户输入）。

下游 `run.py` 这样消费这个返回值：

[run.py:L68-L70](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/run.py#L68-L70) — `args = get_args()` 拿到四元组；`run.py` 主流程这里只用到了 `data_args` 和 `training_args`，所以用 `_` 占位跳过 `model_args` 和 `qa_args`。

而到了任务包内部，`get_trainer` 又重新解包出全部四组（因为它需要 `model_args` 来构造模型）：

[tasks/superglue/get_trainer.py:L18-L19](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/superglue/get_trainer.py#L18-L19) — 任务包解包四元组，取出 `model_args` 用于后续构造模型与 tokenizer。

注意 `run.py` 把**整个 `args` 元组**原样传给 `get_trainer`（见 [run.py:L121](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/run.py#L121)），而 `get_trainer` 内部再自己解包。这样每个任务包的 `get_trainer` 签名统一为 `get_trainer(args)`，契约清晰。

#### 4.1.4 代码实践

**实践目标**：亲手验证「四元组的顺序是固定的」，加深对解包约定可靠性的理解。

**操作步骤**：

1. 打开 `arguments.py`，定位到 `get_args()`（[arguments.py:L187](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/arguments.py#L187)）。
2. 在 `return args` 之前临时加一行打印（**注意：这是为了学习而在脑中/本地副本里做的，不修改仓库源码；若要在本地真实运行，请在自己的分支上操作**）：
   ```python
   # 示例代码：仅用于观察顺序，非项目原有代码
   for a in args:
       print(type(a).__name__)
   ```
3. 在本地 `pt2` 环境中随便给一组合法参数运行，例如：
   ```bash
   python run.py --task_name superglue --dataset_name rte --model_name_or_path roberta-large --do_train --output_dir /tmp/pt2-check
   ```

**需要观察的现象**：打印出的四个类名按顺序应为 `ModelArguments`、`DataTrainingArguments`、`TrainingArguments`、`QuestionAnwseringArguments`。

**预期结果**：顺序与第 189 行传入解析器的元组顺序完全一致，印证「解包顺序固定」的约定。如果你没有 GPU/未装环境，**待本地验证**——你也可以直接静态阅读 `parse_args_into_dataclasses` 的文档得出同样结论。

#### 4.1.5 小练习与答案

**练习 1**：如果有人把 `get_args()` 第 189 行四个类的顺序调换成 `(DataTrainingArguments, ModelArguments, ...)`，下游代码哪里会立刻出错？

> **参考答案**：所有用固定顺序解包的地方都会错位。例如 `run.py` 第 70 行 `_, data_args, training_args, _ = args` 会把原本的 `ModelArguments` 错当成 `DataTrainingArguments`，随后 `data_args.task_name` 访问就会抛 `AttributeError`。这正是「固定顺序四元组」要靠纪律维护的代价。

**练习 2**：`HfArgumentParser` 是 `argparse` 的封装。请说出它相对裸 `argparse` 解决的一个具体痛点。

> **参考答案**：它把「参数声明」从几十行 `add_argument(...)` 压缩成 dataclass 字段，类型与默认值直接写在字段定义里（如 `pre_seq_len: int = field(default=4)`），既少写代码、又少出错，还能让一个 dataclass 同时充当「文档」和「配置对象」。

### 4.2 四组 dataclass 参数详解

#### 4.2.1 概念说明

`arguments.py` 把全部参数拆成四组，每组只关心一个层面：

| dataclass | 关心什么 | 典型字段 |
| --- | --- | --- |
| `ModelArguments` | 用什么模型、是否启用 P-tuning v2 | `model_name_or_path`、`prefix`、`pre_seq_len`、`prefix_projection` |
| `DataTrainingArguments` | 用什么数据、怎么切分 | `task_name`、`dataset_name`、`max_seq_length`、`pad_to_max_length` |
| `TrainingArguments` | 怎么训练（来自 HF） | `learning_rate`、`per_device_train_batch_size`、`num_train_epochs`、`output_dir` |
| `QuestionAnwseringArguments` | 问答任务的后处理细节 | `n_best_size`、`max_answer_length`、`version_2_with_negative` |

分组的好处是「关注点分离」：你想改数据相关的事就去 `DataTrainingArguments` 找，想改训练循环就去 `TrainingArguments` 找，不会在一张几百行的参数表里迷路。

特别注意：**`TrainingArguments` 不是在本文件定义的**，而是从 `transformers` 直接导入（见 [arguments.py:L7](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/arguments.py#L7)）。它本身就带着一大批训练相关字段（学习率、warmup、fp16、日志频率……），P-tuning v2 直接复用，没有重新发明。

#### 4.2.2 核心流程

`HfArgumentParser` 把每个 `field(...)` 翻译成一条命令行参数的规则：

- 字段名 `pre_seq_len` → 命令行选项 `--pre_seq_len`。
- 字段类型 `int` → 接受整数；`bool` → 生成一个**开关型**选项（出现即 True）。
- `field(default=X)` → 缺省值为 `X`，命令行不传就用它。
- `metadata={"choices": [...]}` → 限定取值范围，传非法值会被拒绝。
- `metadata={"help": "..."}` → 自动出现在 `--help` 输出里。

对 P-tuning v2 最重要的几个字段及其默认值：

| 字段（命令行形式） | 类型 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `--prefix` | bool | `False` | **总开关**：开启深度前缀调优（P-tuning v2）。 |
| `--prompt` | bool | `False` | 开启浅层 prompt tuning（与 prefix 互斥，prefix 优先）。 |
| `--pre_seq_len` | int | `4` | 前缀长度 P（每层注入多少个前缀 token）。 |
| `--prefix_projection` | bool | `False` | 是否给前缀加两层 MLP 重参数化头。 |
| `--prefix_hidden_size` | int | `512` | MLP 头的中间隐藏维（仅 `prefix_projection=True` 时生效）。 |
| `--hidden_dropout_prob` | float | `0.1` | 前缀模式下的 dropout 概率。 |

#### 4.2.3 源码精读

`ModelArguments` 是本讲的主角，因为它掌管 P-tuning v2 的全部开关：

[arguments.py:L92-L160](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/arguments.py#L92-L160) — 模型相关参数 dataclass。重点看 `prefix`/`prompt`/`pre_seq_len`/`prefix_projection`/`prefix_hidden_size`/`hidden_dropout_prob` 这六个字段。

把目光聚焦到 P-tuning v2 的三个关键开关上：

[arguments.py:L125-L148](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/arguments.py#L125-L148) — `prefix`（总开关）、`pre_seq_len`（前缀长度，默认 4）、`prefix_projection`（是否加 MLP 头）。注意它们都是 `bool`/`int` 字段，靠 `field(default=...)` 给默认值。

`DataTrainingArguments` 则承担「取值校验」的职责，是 u1-l3 提到的注册表与命令行的连接点：

[arguments.py:L22-L33](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/arguments.py#L22-L33) — `task_name` 与 `dataset_name` 都带 `choices`，分别限定为 `TASKS` 与 `DATASETS`。解析阶段就能拦住非法任务名，不必等到运行时报错。

`TASKS` 与 `DATASETS` 这两个常量来自 `tasks/utils.py`（通过 [arguments.py:L9](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/arguments.py#L9) 的 `from tasks.utils import *` 引入）：

[tasks/utils.py:L11-L13](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/utils.py#L11-L13) — `TASKS` 是五种任务名、`DATASETS` 是所有数据集名的合集，既驱动命令行 `choices` 校验，也驱动 `run.py` 的断言，是「单一数据源」。

`QuestionAnwseringArguments` 只在问答任务里用到，但因为它被写进四元组，所以**所有任务**都会解析它（只是非问答任务不读它）：

[arguments.py:L162-L185](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/arguments.py#L162-L185) — 问答专用参数（候选数、最大答案长度、是否含无答案样本等）。非问答任务会得到一份默认值的实例并被忽略。

> 设计提醒：把 `QuestionAnwseringArguments` 放进全局四元组是一种「简单粗暴」的做法——好处是 `get_args()` 签名统一；代价是分类任务也会无谓地持有这些字段。对于本项目规模，这种取舍是合理的。

#### 4.2.4 代码实践

**实践目标**：搞清 `--prefix` / `--pre_seq_len` / `--prefix_projection` 三个开关对应的命令行形态（是否带值）。

**操作步骤**：

1. 在本地 `pt2` 环境运行：
   ```bash
   python run.py --help 2>&1 | grep -A2 -E "prefix|pre_seq_len"
   ```
2. 观察每个参数后面是否要求「带一个值」。

**需要观察的现象**：

- `--pre_seq_len` 要求跟一个整数（如 `--pre_seq_len 128`），因为它是 `int` 字段。
- `--prefix` 与 `--prefix_projection` 是 `bool` 字段，表现为「开关」：写上 `--prefix` 即为 True，不写即为默认 False，**不需要跟 True/False**。

**预期结果**：理解到「加 MLP 头」=在命令行里写出 `--prefix_projection` 这个开关，而非 `--prefix_projection True`。

**待本地验证**：若未安装 transformers 等，`--help` 会因 import 失败而报错；此时可改为静态阅读 [arguments.py:L137-L148](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/arguments.py#L137-L148) 三个字段的 `default` 与类型，得出同样结论。

#### 4.2.5 小练习与答案

**练习 1**：把 `--pre_seq_len` 写成 `--pre_seq_len 3.5` 会发生什么？为什么？

> **参考答案**：解析阶段会报错（类型不匹配）。因为 `pre_seq_len: int = field(...)` 声明了整数类型，`HfArgumentParser` 据此生成 `type=int` 的选项，`3.5` 无法转成 `int`，解析器会拒绝。

**练习 2**：`--prefix` 和 `--prompt` 都不写时，`ModelArguments.prefix` 与 `prompt` 分别是什么值？这对模型选择意味着什么？

> **参考答案**：两者都是默认值 `False`。在 `get_model` 里会落入 `else` 分支（全量微调，走 `AUTO_MODELS`）。也就是说「两个开关都不带 = 跑普通全量微调」，这正是 u2-l3 提到的第三种模式。

### 4.3 配置流转：从命令行参数到 config

#### 4.3.1 概念说明

这是本讲最关键的一节。读者最容易踩的坑是：以为 `--pre_seq_len 128` 一传进去，模型就「自动」知道前缀长度是 128。其实**不会**。

原因在于两套对象是分离的：

- `args`（`ModelArguments` 等）：纯 Python dataclass，记录「这次运行怎么配」。
- `config`（`AutoConfig`）：模型结构描述，模型类的 `__init__` 只认它。

`from_pretrained` 加载模型时，`config` 是从模型目录（如 `roberta-large/config.json`）读出来的，里面**根本没有** `pre_seq_len` 这种 P-tuning v2 专属字段。所以必须有人**主动**把 `model_args.pre_seq_len` 拷贝到 `config.pre_seq_len` 上，模型构造时才能读到。这个「拷贝」动作就发生在 `get_model()` 里。

一句话总结配置流转：

> 命令行 `--pre_seq_len 128` → `ModelArguments.pre_seq_len = 128` →（`get_model` 里）`config.pre_seq_len = model_args.pre_seq_len` → 模型 `__init__` 读 `config.pre_seq_len` → `PrefixEncoder(config)` 读 `config.pre_seq_len`。

#### 4.3.2 核心流程

`get_model` 根据开关走三条分支，每条分支对 `config` 的「写入量」不同：

```
get_model(model_args, task_type, config):
├─ if model_args.prefix:        # 深度前缀（P-tuning v2）
│     写 config.hidden_dropout_prob
│     写 config.pre_seq_len
│     写 config.prefix_projection
│     写 config.prefix_hidden_size
│     → 用 PREFIX_MODELS[...][task_type] 构造
│
├─ elif model_args.prompt:      # 浅层 prompt
│     只写 config.pre_seq_len
│     → 用 PROMPT_MODELS[...][task_type] 构造
│
└─ else:                        # 全量微调
       不写任何 P-tuning 字段
       → 用 AUTO_MODELS[task_type] 构造，并打印 total param
```

两个值得注意的细节：

1. **`--prefix` 这个总开关本身不写进 `config`**。它只决定走哪条分支（路由作用）。真正进 `config` 的是 `pre_seq_len` 等结构参数。
2. **浅层 prompt 分支只写 `pre_seq_len`**，不写 `prefix_projection` / `prefix_hidden_size`——因为浅层提示用普通 `nn.Embedding`，没有 MLP 头，自然不需要这两个字段。这与 u2-l3 讲的「浅层 Prompt 不打印、参数最少」一致。

#### 4.3.3 源码精读

**第一步：`config` 的初始构造（还不含前缀字段）。** 在任务包里，`get_trainer` 先用 `AutoConfig.from_pretrained` 建出 `config`，此时它只装了 `num_labels`、`finetuning_task` 等基础字段，没有任何 P-tuning 字段：

[tasks/superglue/get_trainer.py:L37-L51](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/superglue/get_trainer.py#L37-L51) — 构造 `config`，只设标签数与任务名；随后把 `config` 传给 `get_model`。

**第二步：在 `get_model` 里把前缀字段「粘」到 `config` 上（核心动作）。**

[model/utils.py:L91-L103](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L91-L103) — `prefix` 分支：把 `model_args` 的四个前缀字段逐一赋给 `config`，然后用 `PREFIX_MODELS` 注册表选模型类并 `from_pretrained` 构造。

聚焦到那四行「拷贝」语句：

[model/utils.py:L93-L96](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L93-L96) — 这四行就是「args → config」的全部粘合代码：`hidden_dropout_prob`、`pre_seq_len`、`prefix_projection`、`prefix_hidden_size`。

对比浅层 prompt 分支，体会「写入量」的差异：

[model/utils.py:L104-L111](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L104-L111) — prompt 分支只写 `config.pre_seq_len`，没有 MLP 相关字段，因为浅层提示不需要。

而全量微调分支则完全不碰 `config` 的前缀字段，改为打印参数量：

[model/utils.py:L112-L141](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L112-L141) — `else` 分支：走 `AUTO_MODELS`，不写前缀字段；并统计、打印可训练参数总量。

**第三步：模型 `__init__` 读 `config`，配置链条的终点消费方。** 以 `BertPrefixForSequenceClassification` 为例，构造函数从 `config` 读出 `pre_seq_len`，并把它连同整个 `config` 交给 `PrefixEncoder`：

[model/sequence_classification.py:L113-L119](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/sequence_classification.py#L113-L119) — `self.pre_seq_len = config.pre_seq_len` 把配置读成实例属性；`PrefixEncoder(config)` 把整个 config 传下去。

`PrefixEncoder` 才是真正「用」这些字段的地方——它凭 `config.pre_seq_len` 和 `config.prefix_projection` 决定自己是「裸 Embedding」还是「Embedding + 两层 MLP」：

[model/prefix_encoder.py:L12-L24](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/prefix_encoder.py#L12-L24) — 读 `config.prefix_projection` 选分支：False 用单层 `Embedding(pre_seq_len, 2L·H)`；True 用 `Embedding` + `Linear→Tanh→Linear` 两层 MLP。

至此，一条完整的「命令行 → 模型结构」链条就闭合了。把三段串起来读，就能回答：「`--pre_seq_len 128` 是怎么影响网络结构的？」

#### 4.3.4 代码实践

**实践目标**：亲手写一条命令，把「是否启用 P-tuning v2 / 前缀长度 / 是否加 MLP 头」三个控制项一次性显式指定，并追踪它们在代码里的落点。

**操作步骤**：

1. 对照下表，确认三个控制项对应的命令行参数与代码落点：

   | 控制项 | 命令行参数 | args 字段 | config 写入处（`get_model`） |
   | --- | --- | --- | --- |
   | 是否启用 P-tuning v2 | `--prefix` | `model_args.prefix` | （仅路由，不写 config） |
   | 前缀长度 | `--pre_seq_len 128` | `model_args.pre_seq_len` | `config.pre_seq_len`（[model/utils.py:L94](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L94)） |
   | 是否加 MLP 头 | `--prefix_projection` | `model_args.prefix_projection` | `config.prefix_projection`（[model/utils.py:L95](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L95)） |

2. 写出一条把三者都显式指定的命令（示例，配合 RoBERTa 在 RTE 上训练）：
   ```bash
   # 示例命令：把三个控制项全部显式写出
   python run.py \
     --task_name superglue \
     --dataset_name rte \
     --model_name_or_path roberta-large \
     --do_train \
     --output_dir checkpoints/rte-roberta \
     --prefix \
     --pre_seq_len 128 \
     --prefix_projection
   ```

**需要观察的现象**：

- 命令里出现 `--prefix`，于是 `get_model` 走 [model/utils.py:L92](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L92) 的 `if model_args.prefix:` 分支。
- `--pre_seq_len 128` 让 `config.pre_seq_len = 128`，最终 `PrefixEncoder` 的 `Embedding` 第一维变成 128。
- `--prefix_projection` 让 `config.prefix_projection = True`，于是 `PrefixEncoder` 走 [model/prefix_encoder.py:L15-L22](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/prefix_encoder.py#L15-L22) 的 MLP 分支。

**预期结果**：训练日志里会打印 `total param is ...`（来自 [model/sequence_classification.py:L128](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/sequence_classification.py#L128)），这个数会显著大于「不带 `--prefix_projection`」时的前缀参数量（u2-l1 提到：加 MLP 后从约 7.4 万涨到约 985 万），印证「MLP 头」确实生效。

**待本地验证**：完整训练需要 GPU 与下载 `roberta-large`。无 GPU 时可只验证参数解析：把上面命令的 `--do_train` 去掉、或在 `get_model` 入口加一行 `print(model_args.prefix, model_args.pre_seq_len, model_args.prefix_projection)`，观察三者是否被正确解析（不修改仓库源码，请在自己的分支操作）。

#### 4.3.5 小练习与答案

**练习 1**：如果删掉 [model/utils.py:L94](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L94) 这一行（即不写 `config.pre_seq_len`），`--pre_seq_len 128` 还会生效吗？为什么？

> **参考答案**：不会生效。`config` 来自 `roberta-large/config.json`，里面没有 `pre_seq_len` 字段。删掉这行赋值后，模型 `__init__` 里 `self.pre_seq_len = config.pre_seq_len` 会抛 `AttributeError`（属性不存在）；即使不报错，`PrefixEncoder` 也会拿到错误长度。这正是「粘合代码」不可省略的原因。

**练习 2**：为什么浅层 prompt 分支（[model/utils.py:L104-L111](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L104-L111)）只写 `config.pre_seq_len`，而不写 `prefix_projection`？

> **参考答案**：因为浅层 prompt 用的是普通 `nn.Embedding` 直接生成提示向量并拼到 `inputs_embeds`（见 u2-l3），没有「两层 MLP 重参数化」这一说，自然不需要 `prefix_projection` / `prefix_hidden_size`。写入量最小，与其「参数最少」的特性一致。

**练习 3**：`--prefix` 这个总开关会写进 `config` 吗？它的真正作用是什么？

> **参考答案**：不会写进 `config`。它的作用是**路由**——在 `get_model` 里决定走 `PREFIX_MODELS` 分支，并触发把前缀字段拷到 `config` 的动作。模型类本身（如 `BertPrefixForSequenceClassification`）是靠「被选中的类」来体现 prefix 特性的，而不是靠某个 `config.prefix` 标志位。

## 5. 综合实践

**任务**：把本讲学的「四组 dataclass → get_args 四元组 → get_model 粘合 config → 模型读取」全链条串起来，做一次端到端的「参数侦探」追踪。

**要求**：

1. 假设你看到一条运行命令（与 `run_script/run_rte_roberta.sh` 风格一致）：
   ```bash
   python run.py --task_name superglue --dataset_name rte \
     --model_name_or_path roberta-large \
     --do_train --output_dir checkpoints/rte-roberta \
     --prefix --pre_seq_len 128
   ```
2. 请按下面的表格，逐格填写每个命令行参数「属于哪组 dataclass」「在哪个文件被消费」「最终影响了什么」：

   | 命令行参数 | 属于哪组 dataclass | 消费它的代码位置 | 最终影响 |
   | --- | --- | --- | --- |
   | `--task_name superglue` | ？ | ？ | ？ |
   | `--model_name_or_path roberta-large` | ？ | ？ | ？ |
   | `--prefix` | ？ | ？ | ？ |
   | `--pre_seq_len 128` | ？ | ？ | ？ |
   | `--output_dir checkpoints/rte-roberta` | ？ | ？ | ？ |

3. **参考答案（填好后）**：

   | 命令行参数 | 属于哪组 dataclass | 消费它的代码位置 | 最终影响 |
   | --- | --- | --- | --- |
   | `--task_name superglue` | `DataTrainingArguments` | `run.py` 分派 + `choices` 校验 | 决定 `from tasks.superglue.get_trainer import get_trainer` |
   | `--model_name_or_path roberta-large` | `ModelArguments` | `get_trainer` 的 tokenizer/config + `get_model` 的 `from_pretrained` | 加载哪个主干与 tokenizer |
   | `--prefix` | `ModelArguments` | `get_model` 第 92 行 | 路由到 `PREFIX_MODELS` 分支，并把前缀字段拷进 config |
   | `--pre_seq_len 128` | `ModelArguments` | `get_model` 第 94 行 → `config.pre_seq_len` | 决定 `PrefixEncoder` 的 Embedding 第一维（前缀长度） |
   | `--output_dir checkpoints/rte-roberta` | `TrainingArguments` | HF Trainer 内部 + `run.py` checkpoint 检查 | 训练产物落盘位置 |

4. 完成表格后，用自己的话写一段话解释：**为什么 `--pre_seq_len` 必须经过「args → config」两跳，而 `--output_dir` 不用？**
   > 参考思路：`output_dir` 只被训练循环（HF Trainer）使用，不进入模型结构，所以停在 `TrainingArguments` 即可；`pre_seq_len` 要决定 `PrefixEncoder` 的网络形状，而模型构造只认 `config`，因此必须被「搬运」进 `config`。

## 6. 本讲小结

- `get_args()` 用 `HfArgumentParser` 解析四组 dataclass，返回**顺序固定**的四元组 `(ModelArguments, DataTrainingArguments, TrainingArguments, QuestionAnwseringArguments)`，全系统按固定顺序解包。
- 四组参数关注点分离：模型/数据/训练/问答；其中 `TrainingArguments` 直接复用自 `transformers`，`choices` 借自 `tasks/utils.py` 的 `TASKS`/`DATASETS` 注册表。
- P-tuning v2 的三大开关 `--prefix`（总开关）、`--pre_seq_len`（前缀长度）、`--prefix_projection`（是否加 MLP 头）都定义在 `ModelArguments`；`--prefix`/`--prefix_projection` 是不带值的开关型参数。
- **核心结论**：`args` 与 `config` 是两套对象，`--pre_seq_len` 不会自动进模型；必须在 `get_model()` 里用 `config.pre_seq_len = model_args.pre_seq_len` 这类语句把前缀字段手动「粘」到 `config` 上，模型 `__init__` 与 `PrefixEncoder` 才读得到。
- 三种模式对 `config` 的写入量不同：深度前缀写四个字段、浅层 prompt 只写一个、全量微调不写；`--prefix` 本身只起路由作用，不进 `config`。

## 7. 下一步学习建议

- 下一讲 **u3-l2 模型工厂 get_model 与任务注册表** 会展开本讲反复出现的 `PREFIX_MODELS`/`PROMPT_MODELS`/`AUTO_MODELS` 三张注册表与 `TaskType` 枚举，看清 `(model_type, task_type) → model_class` 的二维路由。
- 想巩固「config 被谁读」的直觉，可回头对照 **u2-l1（PrefixEncoder）** 与 **u2-l2（get_prompt/forward）**，体会 `config.pre_seq_len` 如何同时决定 Embedding 形状与 attention_mask 拼接长度。
- 想看真实运行脚本如何把这些参数组合起来，可阅读 `run_script/run_rte_roberta.sh`、`run_script/run_conll04_bert.sh`（u1-l2 已介绍）。

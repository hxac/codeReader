# 模型工厂 get_model 与任务注册表

## 1. 本讲目标

前两讲我们已经分别看过 `get_model` 的「三个分支」（u2-l3）和「命令行参数如何被粘进 `config`」（u3-l1）。但这两讲都把一个关键问题留到了本讲：**一个字符串任务名（如 `superglue`、`copa`）到底是怎么变成一个具体模型类的？** 中间那张「注册表」长什么样、是怎么设计的？

本讲就把这条链路彻底打通。学完本讲，你应当能够：

1. 说清 `TaskType` 枚举的四个值，以及每个**任务包**如何把字符串任务名映射成一个 `TaskType`。
2. 看懂 `PREFIX_MODELS` / `PROMPT_MODELS` / `AUTO_MODELS` 三张注册表的「二维 vs 一维」结构差异，以及其中隐藏的 `None`「地雷」。
3. 读懂 `get_model` 完整的二维路由 `（model_type, task_type) → model_class`，并解释 `fix_bert` 参数为什么**只在全量微调分支**真正起作用。
4. 给定一组运行参数，能像查字典一样在注册表里手动追踪出最终实例化的模型类。

## 2. 前置知识

本讲建立在前置讲义之上，只回顾三句话，不重复细节：

- **工厂函数（factory）**：一个「按需返回对象」的函数。`get_model` 就是一个工厂——你告诉它「要哪种调优模式 + 哪类任务」，它返回一个装配好的模型实例（u2-l3）。
- **注册表（registry）**：一张「键 → 值」的查找表。本项目用它把「（主干类型, 任务类型）」映射到「具体模型类」，避免写一大堆 `if/elif`。
- **args 与 config 是两套对象**：`--prefix` 这类开关活在 `model_args` 里只起路由作用，真正决定网络结构的 `pre_seq_len` 等要被搬运进 `config`（u3-l1）。

还需两个本讲特有的基础概念：

- **枚举（Enum）**：Python 的 `enum.Enum`，把一组相关的常量收成一个类型，用「符号名」而非魔法数字来区分。本项目用 `TaskType.SEQUENCE_CLASSIFICATION` 这样的符号代替 `1/2/3/4`，可读性好、改不动错。
- **二维字典（dict of dict）**：外层字典的「值」又是一个字典，即 `{外键: {内键: 值}}`。本项目注册表是 `{model_type: {TaskType: 模型类}}`，两步查找完成二维路由。

> 承接前置讲义：u1-l3 讲过 `tasks/utils.py` 里的 `TASKS`/`DATASETS` 常量驱动命令行校验与运行时断言；u2-l3 讲过 `get_model` 三分支与三种调优模式的差异；u3-l1 讲过前缀字段如何从 `args` 粘进 `config`。本讲负责把「任务名 → 任务类型 → 模型类」这段此前被略过的中间路由讲透。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [model/utils.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py) | 本讲主角：`TaskType` 枚举、三张注册表、分发函数 `get_model`，以及作对照用的旧实现 `get_model_deprecated`。 |
| [tasks/utils.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/utils.py) | `TASKS` / `DATASETS` 常量，是命令行 `choices` 校验与 `run.py` 断言的「单一数据源」。 |
| [run.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/run.py) | 入口：用字符串 `task_name` 做第一层分派，把控制权交给对应任务包的 `get_trainer`。 |
| [tasks/superglue/get_trainer.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/superglue/get_trainer.py) | 演示「数据集特性 → TaskType」的内部判断：普通数据集走 `SEQUENCE_CLASSIFICATION`，COPA 等多选数据集走 `MULTIPLE_CHOICE`。 |
| `tasks/{glue,ner,srl,qa}/get_trainer.py` | 各任务包调用 `get_model` 时传入的具体 `TaskType` 与 `fix_bert` 取值各不相同，是理解「同任务类型不同行为」的关键。 |

## 4. 核心概念与源码讲解

### 4.1 TaskType 枚举与「任务名 → 任务类型」映射

#### 4.1.1 概念说明

读者第一次读 `run.py` 时通常有个困惑：命令行传的是字符串 `--task_name superglue`、`--dataset_name copa`，而 `get_model` 要的是一个叫 `task_type` 的参数。这中间必须有人把「字符串」翻译成「任务类型」。

这个翻译分**两层**完成：

1. **第一层（字符串 → 任务包）**：`run.py` 按 `task_name` 字符串分派到对应的任务包目录（u1-l3 已讲）。
2. **第二层（任务包 → TaskType）**：每个任务包的 `get_trainer` 在调用 `get_model` 时，**硬编码**地传入一个 `TaskType` 枚举值。

`TaskType` 就是为第二层服务的：它把项目支持的「任务形态」归纳成四类，这样注册表只需为这四类各准备一套模型，而不必为每个数据集单独写。四类是：

| TaskType 枚举 | 含义 | 典型代表任务 |
| --- | --- | --- |
| `SEQUENCE_CLASSIFICATION` | 句子/篇章级分类（一句或两句话 → 一个标签） | GLUE、SuperGLUE 的 RTE/BoolQ/WiC… |
| `MULTIPLE_CHOICE` | 多选题（一个题干 + 多个候选 → 选最优） | SuperGLUE 的 COPA |
| `TOKEN_CLASSIFICATION` | token 级分类（序列标注，每个子词一个标签） | NER（conll2003）、SRL（conll2005/2012） |
| `QUESTION_ANSWERING` | 抽取式问答（一段文本 → 抽出起止位置） | SQuAD / SQuAD v2 |

注意：**任务名有 5 个（glue/superglue/ner/srl/qa），但任务类型只有 4 个**——因为 `ner` 和 `srl` 都属于 `TOKEN_CLASSIFICATION`。这是「任务名（数据来源）」与「任务类型（模型形态）」解耦的体现：同一个任务形态可以服务多个数据来源。

#### 4.1.2 核心流程

「字符串任务名 → TaskType」的全流程：

```text
--task_name superglue --dataset_name copa
        │
        ▼  run.py 第一层分派（按字符串）
from tasks.superglue.get_trainer import get_trainer
        │
        ▼  任务包内部判断（这一层才决定 TaskType）
tasks/superglue/get_trainer.py:
   if not dataset.multiple_choice:        # 普通分类
       TaskType = SEQUENCE_CLASSIFICATION
   else:                                   # COPA 等多选
       TaskType = MULTIPLE_CHOICE
        │
        ▼  把 TaskType 传给工厂
model = get_model(model_args, TaskType, config, fix_bert=...)
```

关键点：**`TaskType` 不是从命令行直接读的**，而是各任务包根据自己的数据特性「算」出来的。大多数任务包直接写死一个枚举值；SuperGLUE 因为内部既有分类又有 COPA 多选，所以多了一层 `if dataset.multiple_choice` 判断。

#### 4.1.3 源码精读

先看枚举本身的定义：

[model/utils.py:L40-L44](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L40-L44) — `TaskType` 枚举，四个值分别对应 token 分类、序列分类、问答、多选。

> 代码细节：这四个枚举成员后面都跟了一个逗号（`TOKEN_CLASSIFICATION = 1,`）。在 Python 里 `x = 1,` 实际是把 `x` 赋成一个单元素元组 `(1,)`，所以严格说每个成员的 `.value` 是元组而非整数。这是一个容易被忽视的「小瑕疵」，但**不影响功能**——因为下游字典查找和相等比较用的都是枚举成员本身（如 `TaskType.SEQUENCE_CLASSIFICATION`），而不是它的 `.value`。读者知道即可，不必纠结。

再看各任务包如何传入 `TaskType` 与 `fix_bert`。下表汇总自五个 `get_trainer.py` 的真实调用：

| 任务包 | 调用位置 | 传入的 TaskType | fix_bert |
| --- | --- | --- | --- |
| glue | [tasks/glue/get_trainer.py:L45](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/glue/get_trainer.py#L45) | `SEQUENCE_CLASSIFICATION` | 不传（默认 `False`） |
| superglue（普通） | [tasks/superglue/get_trainer.py:L54](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/superglue/get_trainer.py#L54) | `SEQUENCE_CLASSIFICATION` | 不传（默认 `False`） |
| superglue（COPA 多选） | [tasks/superglue/get_trainer.py:L56](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/superglue/get_trainer.py#L56) | `MULTIPLE_CHOICE` | `True` |
| ner | [tasks/ner/get_trainer.py:L61](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/ner/get_trainer.py#L61) | `TOKEN_CLASSIFICATION` | `True` |
| srl | [tasks/srl/get_trainer.py:L46](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/srl/get_trainer.py#L46) | `TOKEN_CLASSIFICATION` | `False` |
| qa | [tasks/qa/get_trainer.py:L32](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/qa/get_trainer.py#L32) | `QUESTION_ANSWERING` | `True` |

这张表藏着一个重要结论：**同一个 `TaskType` 可以对应不同的 `fix_bert`**。`ner` 和 `srl` 都是 `TOKEN_CLASSIFICATION`，但 `ner` 传 `fix_bert=True`、`srl` 传 `fix_bert=False`。也就是说「冻结主干」这件事不是由任务类型决定的，而是由各任务包**单独**决定的。`fix_bert` 的真正作用我们留到 4.3 节细讲。

SuperGLUE 那层「多选与否」的判断，落点在数据集类里：

[tasks/superglue/dataset.py:L33](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/superglue/dataset.py#L33) — `self.multiple_choice = data_args.dataset_name in ["copa"]`，即目前只有 `copa` 被判定为多选；`get_trainer` 再据此选 `MULTIPLE_CHOICE`。

#### 4.1.4 代码实践

**实践目标**：亲手验证「字符串任务名 → TaskType」的两层翻译，理解它不是从命令行直接读的。

**操作步骤**：

1. 打开 [run.py:L96-L117](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/run.py#L96-L117)，确认第一层分派只看 `task_name` 字符串，**完全没出现 `TaskType`**。
2. 打开任意两个任务包的 `get_trainer.py`，对比它们调用 `get_model` 时传入的第二个参数：
   - [tasks/glue/get_trainer.py:L45](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/glue/get_trainer.py#L45) 传入 `TaskType.SEQUENCE_CLASSIFICATION`；
   - [tasks/ner/get_trainer.py:L61](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/ner/get_trainer.py#L61) 传入 `TaskType.TOKEN_CLASSIFICATION`。

**需要观察的现象**：命令行参数里根本没有「TaskType」这一项；它是各任务包在代码里写死的（SuperGLUE 例外，多了一层数据集判断）。

**预期结果**：在脑中画出 `task_name 字符串 → 任务包 → TaskType` 的两跳映射。这解释了「为什么新增任务不仅要加注册表，还要新建任务包并选定一个 TaskType」（u8-l1 会展开）。

**待本地验证**：本实践为源码阅读型，无需运行；若想进一步确认，可在本地用 `grep -rn "get_model(model_args" tasks/` 列出全部六处调用，逐一核对上表。

#### 4.1.5 小练习与答案

**练习 1**：为什么项目有 5 个 `task_name`（glue/superglue/ner/srl/qa），但 `TaskType` 只有 4 个枚举值？

> **参考答案**：`task_name` 描述的是「数据来源」，`TaskType` 描述的是「模型形态」。`ner` 和 `srl` 是两个不同的数据来源，但模型形态完全一样（都是 token 级分类），所以共用 `TOKEN_CLASSIFICATION`。这种「数据来源 × 任务形态」的解耦，让注册表只需为 4 种形态各维护一套模型。

**练习 2**：假设要新增一个数据集 `wsc`（Winograd Schema Challenge，本质是多选），但它被归到 `superglue` 下。它会走 `SEQUENCE_CLASSIFICATION` 还是 `MULTIPLE_CHOICE`？由哪一行代码决定？

> **参考答案**：由 [tasks/superglue/dataset.py:L33](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/superglue/dataset.py#L33) 的 `data_args.dataset_name in ["copa"]` 决定。若想让 `wsc` 走多选，必须把它加进这个列表，否则即便数据是多选格式，模型也会被当成 `SEQUENCE_CLASSIFICATION` 而出错。

---

### 4.2 三张注册表：二维 vs 一维的查找结构

#### 4.2.1 概念说明

上一节把「任务名 → TaskType」打通了。本节看「`(model_type, TaskType) → 模型类`」是怎么存的。本项目用了三张结构不同的注册表：

- **`PREFIX_MODELS`：二维表（dict of dict）**，外键是 `model_type`（如 `"bert"`、`"roberta"`），内键是 `TaskType`，值是具体的 Prefix 模型类。每个主干有自己专属的 Prefix 实现（如 `BertPrefixForXxx`、`RobertaPrefixForXxx`），所以必须按主干再分一层。
- **`PROMPT_MODELS`：二维表（稀疏）**，结构与 `PREFIX_MODELS` 一样，但只填了 `bert`/`roberta` 两行、且每行只有 `SEQUENCE_CLASSIFICATION` 与 `MULTIPLE_CHOICE` 两列（u2-l3 已指出它覆盖面窄）。
- **`AUTO_MODELS`：一维表**，只按 `TaskType` 索引，值是 HuggingFace 原生 `AutoModelFor*`。

为什么 `AUTO_MODELS` 只有一维？因为 HF 的 `AutoModelForXxx` 内部已经能根据 `config.model_type` 自己挑对应的主干实现——主干选择的复杂性被 HF 封装掉了，本项目无需再分一层。而 Prefix/Prompt 模型是项目**自己实现**的、主干专属的类，HF 不知道它们的存在，只能由本项目用二维表显式登记。这是三张表维度不同的根本原因。

#### 4.2.2 核心流程

三种调优模式下，查表方式不同：

```text
深层 Prefix（--prefix）：
   model_class = PREFIX_MODELS[ config.model_type ][ task_type ]
   # 两步查表：先按主干，再按任务

浅层 Prompt（--prompt）：
   model_class = PROMPT_MODELS[ config.model_type ][ task_type ]
   # 同样两步，但表更稀疏，缺项会 KeyError

全量微调（都不加）：
   model_class = AUTO_MODELS[ task_type ]
   # 一步查表：主干选择交给 HF 的 Auto 机制
```

一个要警惕的「地雷」：`PREFIX_MODELS` 不是满表。`deberta-v2` 这一行除了 `TOKEN_CLASSIFICATION` 外，其余三个键的值都是 `None`（见 [model/utils.py:L65-L70](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L65-L70)）。一旦组合出 `("deberta-v2", SEQUENCE_CLASSIFICATION)` 且带 `--prefix`，查表得到 `None`，随后 `None.from_pretrained(...)` 会抛 `AttributeError`。注册表用 `None` 占位等于「显式声明不支持」，但调用方并没有检查，所以这是潜在崩溃点。

#### 4.2.3 源码精读

**`PREFIX_MODELS`：满的四行 + 稀疏的 deberta-v2。**

[model/utils.py:L46-L71](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L46-L71) — `bert`/`roberta`/`deberta` 三行各自填满了四类任务（token 分类、序列分类、问答、多选）；唯独 `deberta-v2` 只实现了 `TOKEN_CLASSIFICATION`（`DebertaV2PrefixForTokenClassification`），其余三类是 `None`。

把这些类名与顶部 import 对照，能发现一处**命名不一致**，值得留意：

[model/utils.py:L18-L22](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L18-L22) — BERT 的问答类叫 `BertPrefixForQuestionAnswering`，而 RoBERTa/DeBERTa 的分别叫 `RobertaPrefixModelForQuestionAnswering`、`DebertaPrefixModelForQuestionAnswering`——后两者中间多了个 `Model`。这种「同族类名不一致」是读码和扩展时容易踩的小坑（例如想用 `RobertaPrefixForQuestionAnswering` 这个名字去 import 会报 `ImportError`）。

**`PROMPT_MODELS`：覆盖面明显窄。**

[model/utils.py:L73-L82](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L73-L82) — 只有 `bert`/`roberta` 两行，且只有 `SEQUENCE_CLASSIFICATION` 与 `MULTIPLE_CHOICE` 两列。没有 token 分类、问答，也没有 deberta。这从结构上再次印证 u2-l3 的结论：浅层 Prompt 只是对照实现，深层 Prefix 才是本项目重点。

**`AUTO_MODELS`：一维表，委托给 HF。**

[model/utils.py:L84-L89](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L84-L89) — 直接把四个 `TaskType` 映射到 HF 的四个 `AutoModelFor*`。注意它没有 `model_type` 这一层：主干由 `AutoModelFor*.from_pretrained(model_name_or_path, config=config)` 内部根据 `config.model_type` 自动决定。

三张表的结构差异用一张表总结：

| 注册表 | 维度 | 外键 | 内键 | 缺项处理 |
| --- | --- | --- | --- | --- |
| `PREFIX_MODELS` | 二维 | `model_type` | `TaskType` | 部分为 `None`（deberta-v2） |
| `PROMPT_MODELS` | 二维（稀疏） | `model_type` | `TaskType` | 缺键 → `KeyError` |
| `AUTO_MODELS` | 一维 | — | `TaskType` | 四项齐全 |

#### 4.2.4 代码实践

**实践目标**：在注册表里手动追踪一条路径，体会「两步查表」的过程，并识别出 `None` 地雷。

**操作步骤**：

1. 打开 [model/utils.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py)，定位 `PREFIX_MODELS`。
2. 追踪 `model_type="deberta-v2"`、`task_type=SEQUENCE_CLASSIFICATION`、带 `--prefix` 这条路径：
   - 先查 `PREFIX_MODELS["deberta-v2"]`，得到一个字典；
   - 再查该字典的 `TaskType.SEQUENCE_CLASSIFICATION` 键，看值是什么。
3. 用同样的方法追踪 `model_type="bert"`、`task_type=TOKEN_CLASSIFICATION`，确认得到 `BertPrefixForTokenClassification`。

**需要观察的现象**：

- 第二步得到的值是 `None`（[model/utils.py:L67](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L67)）。
- 想象后续 `None.from_pretrained(...)`，会得到 `AttributeError: 'NoneType' object has no attribute 'from_pretrained'`。

**预期结果**：理解到「deberta-v2 主干目前只支持 token 分类的 P-tuning v2」，若强行用于分类/问答/多选会崩溃。这是注册表用 `None` 占位带来的、调用方未加保护的隐患。

**待本地验证**：本实践为源码阅读型；若本地有环境，可写两行示例代码（非项目代码）触发查表：
```python
# 示例代码：仅演示查表，非项目原有代码
from model.utils import PREFIX_MODELS, TaskType
print(PREFIX_MODELS["deberta-v2"][TaskType.SEQUENCE_CLASSIFICATION])  # 预期 None
print(PREFIX_MODELS["bert"][TaskType.TOKEN_CLASSIFICATION])           # 预期类对象
```

#### 4.2.5 小练习与答案

**练习 1**：为什么 `AUTO_MODELS` 是一维的，而 `PREFIX_MODELS` 必须是二维的？

> **参考答案**：`AUTO_MODELS` 的值是 HF 的 `AutoModelFor*`，它内部已经能根据 `config.model_type` 自动挑选主干实现，主干选择的复杂性被 HF 封装了，所以本项目只需按 `TaskType` 分一层。`PREFIX_MODELS` 的值是项目**自己实现**的主干专属类（`BertPrefix...`、`RobertaPrefix...`），HF 不认识它们，必须由本项目用 `model_type` 这一维显式区分。维度不同，本质是「谁负责选主干」不同。

**练习 2**：如果用户运行 `--task_name superglue --dataset_name rte --model_name_or_path microsoft/deberta-v2-xlarge --prefix`，会实例化哪个类？还是报错？

> **参考答案**：会报错。`rte` 走 `SEQUENCE_CLASSIFICATION`，`model_type` 为 `deberta-v2`，查 `PREFIX_MODELS["deberta-v2"][TaskType.SEQUENCE_CLASSIFICATION]` 得到 `None`，随后 `None.from_pretrained(...)` 抛 `AttributeError`。要让它跑通，要么换不带 `--prefix`（走 `AUTO_MODELS`，由 HF 的 DebertaV2 原生实现支撑），要么把对应模型类实现出来并填进注册表。

---

### 4.3 get_model 路由与 fix_bert 的真相

#### 4.3.1 概念说明

把前两节合起来，`get_model` 的完整路由是「**三个分支开关**（prefix/prompt/else）× **二维注册表**（model_type × task_type）」的两级决策。这部分 u2-l3 已描述过流程，本节聚焦一个容易被忽视、但对理解项目至关重要的细节：**`fix_bert` 参数到底干了什么，又在哪里真正起作用**。

直觉上，`fix_bert=True` 字面意思是「冻结主干」。但读源码会发现一个反直觉的事实：

- 在 **prefix / prompt** 两个分支里，`fix_bert` **完全没有被使用**——主干是否冻结，由模型类自己的 `__init__` 决定（u2-l2 讲过，`__init__` 里有一段 `requires_grad=False` 循环，无条件冻结主干）。
- `fix_bert` **只在 else（全量微调）分支**里被读取，用来决定是否在「本不该冻结主干」的全量微调场景下额外冻结主干。

换句话说：`fix_bert` 不是「P-tuning v2 的冻结开关」，而是「全量微调模式下，可选地也冻结主干」的开关。前缀模式下的冻结是**无条件**的、由模型类保证的。

#### 4.3.2 核心流程

`get_model` 的完整决策树（结合 u2-l3 与本节的重点）：

```text
get_model(model_args, task_type, config, fix_bert=False):
│
├─ if model_args.prefix:
│     写 4 个 config 前缀字段
│     model_class = PREFIX_MODELS[model_type][task_type]
│     model = model_class.from_pretrained(...)
│     ※ fix_bert 在此分支被忽略
│     ※ 主干冻结由 model_class.__init__ 无条件完成
│
├─ elif model_args.prompt:
│     写 config.pre_seq_len
│     model_class = PROMPT_MODELS[model_type][task_type]
│     model = model_class.from_pretrained(...)
│     ※ fix_bert 在此分支被忽略
│
└─ else（全量微调）:
      model_class = AUTO_MODELS[task_type]
      model = model_class.from_pretrained(...)      # 主干默认可训练
      if fix_bert:                                   # ← 唯一读 fix_bert 的地方
          把 model.bert/roberta/deberta 的 requires_grad 置 False
          累加 bert_param
      统计并打印 total_param = all_param - bert_param
```

`fix_bert` 的取值由各任务包决定（见 4.1.3 的表）。一个有意思的组合是 `srl`：它带 `--prefix` 时是 P-tuning v2，主干在 `__init__` 里冻结；不带 `--prefix` 时走 else，而它传的 `fix_bert=False`，于是主干**不冻结**——变成「主干 + 分类头一起训练」的标准全量微调。这就是 `srl` 与 `ner` 虽同属 `TOKEN_CLASSIFICATION`、却在 `fix_bert` 上相反的含义。

#### 4.3.3 源码精读

**路由主体（u2-l3 已逐行讲过，这里只点出关键行）：**

[model/utils.py:L91-L103](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L91-L103) — prefix 分支：写 config、查 `PREFIX_MODELS`、`from_pretrained`。注意第 91 行函数签名带 `fix_bert: bool = False`，但这一整段**没有**出现 `fix_bert`。

[model/utils.py:L104-L118](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L104-L118) — prompt 分支与 else 分支开头：同样不读 `fix_bert`。

**`fix_bert` 真正被读取的唯一位置——在 else 分支内部：**

[model/utils.py:L120-L141](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L120-L141) — 第 121 行 `if fix_bert:` 才是它唯一的消费点；只有此时才遍历 `model.bert`/`model.roberta`/`model.deberta` 把 `requires_grad` 置 False，并累加 `bert_param`；最后第 141 行打印带星号的 `***** total param is {} *****`。

由此得出本节最重要的结论：**带 `--prefix` 时，`fix_bert` 形同虚设**。例如 [tasks/superglue/get_trainer.py:L56](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/superglue/get_trainer.py#L56) 给 COPA 传了 `fix_bert=True`，但只要命令行带了 `--prefix`，这行实参就进不了第 121 行的 `if`。它的意义只在「不带 `--prefix`、走全量微调」时才体现。

**对照：旧实现 `get_model_deprecated` 为什么被废弃。**

[model/utils.py:L145-L259](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L145-L259) — 旧版没有注册表，靠**两层 if/elif** 实现：先按 `task_type` 决定从哪个模块 import（[L152-L159](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L152-L159)），再按 `config.model_type` 决定实例化哪个类（[L161-L186](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L161-L186)）。

这种写法有两个硬伤：

1. **import 名字冲突**：它从四个不同模块（token_classification / sequence_classification / question_answering / multiple_choice）分别 import 同名符号 `BertPrefixModel`，靠「按 task_type 选模块」来区分。这非常脆弱——一旦某个模块没导出这个名字，或 import 顺序变化，就会出错。
2. **分支爆炸**：主干（4 种）× 任务（4 种）= 16 种组合，旧版要用嵌套 if 穷举；而新版注册表只需在表里填一格，新增一种组合就是加一行字典，是声明式的、O(1) 查找。

这段废弃代码是很好的「重构案例」：把「过程式的嵌套分支」提炼成「声明式的数据表」，是 `get_model` 从 `_deprecated` 进化到当前版本的核心改进。

#### 4.3.4 代码实践

**实践目标**：用「静态读码 + 路径追踪」的方式，亲手走完一条真实路由，作为本讲的综合实践预演（也是规格里要求的那道追踪题的简化版）。

**操作步骤**：

1. 设定场景：`--task_name superglue --dataset_name rte --model_name_or_path roberta-large`，**不带** `--prefix`。
2. 在 [run.py:L96-L98](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/run.py#L96-L98) 确认分派到 `tasks.superglue.get_trainer`。
3. 在 [tasks/superglue/get_trainer.py:L53-L54](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/superglue/get_trainer.py#L53-L54) 确认 `rte` 不是多选 → 走 `get_model(model_args, TaskType.SEQUENCE_CLASSIFICATION, config)`，`fix_bert` 用默认 `False`。
4. 在 [model/utils.py:L112-L118](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L112-L118) 确认 `prefix`/`prompt` 都为 False → 走 else，`model_class = AUTO_MODELS[SEQUENCE_CLASSIFICATION]`。

**需要观察的现象**：因为 `fix_bert=False`，[model/utils.py:L121](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L121) 的 `if fix_bert:` 不进入，`bert_param` 保持 0，`total_param = all_param`（约等于整个 RoBERTa-large 主干），日志打印带星号。

**预期结果**：最终实例化的是 HF 原生 `AutoModelForSequenceClassification`（一维表 `AUTO_MODELS`），主干全量可训练。这与「带 `--prefix` 时得到 `RobertaPrefixForSequenceClassification` 且主干冻结」形成鲜明对照。

**待本地验证**：无 GPU 时只需完成上述静态追踪；有环境时可在 `get_model` 入口加一行打印（在自己的分支上）确认 `model_args.prefix` 与 `model_class` 的真实取值。

#### 4.3.5 小练习与答案

**练习 1**：COPA 的调用传了 `fix_bert=True`（[tasks/superglue/get_trainer.py:L56](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/superglue/get_trainer.py#L56)）。如果命令行同时带 `--prefix`，这个 `fix_bert=True` 会不会让主干多冻结一次？为什么？

> **参考答案**：不会。`fix_bert` 只在 else 分支（[model/utils.py:L121](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L121)）被读取；带 `--prefix` 时走的是 prefix 分支，根本到不了那一行。COPA 的主干冻结是由 `RobertaPrefixForMultipleChoice.__init__` 无条件完成的，与传入的 `fix_bert` 无关。所以这里的 `fix_bert=True` 只在「不带 `--prefix` 的全量微调」时才生效。

**练习 2**：旧版 `get_model_deprecated` 为什么从四个模块分别 import 同名符号 `BertPrefixModel` 是危险的？新注册表是怎么解决的？

> **参考答案**：旧版靠「按 task_type 选模块、再取同名符号」来区分不同任务的 `BertPrefixModel`，名字相同极易混淆，且依赖每个模块都恰好导出这个名字，模块改动就会崩。新版在文件顶部（[model/utils.py:L3-L30](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L3-L30)）一次性用**带任务前缀的描述性名字**（如 `BertPrefixForTokenClassification`）把所有类 import 全，再装进注册表；查表是声明式的 O(1) 查找，新增组合只需加一格字典，不再写 if 分支。

---

## 5. 综合实践

**任务**：本讲的规格要求追踪下面这条命令的完整路由。请按步骤在源码里手动查表，给出最终实例化的类名。

```bash
python run.py --task_name superglue --dataset_name copa \
  --model_name_or_path roberta-large --prefix --pre_seq_len 128 \
  --do_train --output_dir checkpoints/copa-roberta
```

**操作步骤与追踪过程**：

1. **第一层分派（字符串 → 任务包）**：`task_name=superglue` → [run.py:L96-L98](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/run.py#L96-L98) 断言 `copa ∈ SUPERGLUE_DATASETS`，分派到 `tasks.superglue.get_trainer`。
2. **第二层（数据集特性 → TaskType）**：在 [tasks/superglue/dataset.py:L33](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/superglue/dataset.py#L33) `copa in ["copa"]` 为真 → `dataset.multiple_choice=True`。
3. **选 TaskType**：[tasks/superglue/get_trainer.py:L53-L56](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/superglue/get_trainer.py#L53-L56) 因 `multiple_choice` 为真 → 走 `else`，调用 `get_model(model_args, TaskType.MULTIPLE_CHOICE, config, fix_bert=True)`。
4. **选注册表（开关 → 表）**：`model_args.prefix=True`（因为命令行有 `--prefix`）→ 进 prefix 分支，查 `PREFIX_MODELS`（[model/utils.py:L92-L98](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L92-L98)）。
5. **二维查表（model_type × task_type）**：`config.model_type="roberta"`、`task_type=MULTIPLE_CHOICE` → `PREFIX_MODELS["roberta"][TaskType.MULTIPLE_CHOICE]`（[model/utils.py:L57](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L57)）。

**最终答案**：得到 `RobertaPrefixForMultipleChoice`（该类定义在 [model/multiple_choice.py:L238](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/multiple_choice.py#L238)，在 [model/utils.py:L28](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L28) 被 import）。

**进阶追问**（请独立回答）：

- 这条命令里的 `fix_bert=True`（第 3 步传入）有没有实际生效？主干是被谁、在哪里冻结的？
  > 参考答案：未生效。带 `--prefix` 走 prefix 分支，`fix_bert` 不会被读取；主干由 `RobertaPrefixForMultipleChoice.__init__` 内的 `requires_grad=False` 循环无条件冻结。
- 如果把 `--prefix` 去掉、其余不变，最终会得到哪个类？
  > 参考答案：走 else 分支，`AUTO_MODELS[MULTIPLE_CHOICE]` = HF 原生 `AutoModelForMultipleChoice`，主干全量可训练（因为 `fix_bert=True` 此时才生效，会冻结主干，但模型类是 HF 的 Auto 类）。

**待本地验证**：无 GPU 时以上为静态追踪，结论可仅靠读码得出；有环境时可在 `get_model` 入口加日志确认 `model_class.__name__`。

## 6. 本讲小结

- `TaskType` 枚举把项目支持的模型形态归纳成四类（序列分类、多选、token 分类、问答）；它**不是**从命令行直接读的，而是各任务包的 `get_trainer` 根据数据特性「算」出来的（SuperGLUE 还多一层 `multiple_choice` 判断）。
- 三张注册表结构不同：`PREFIX_MODELS`/`PROMPT_MODELS` 是二维（`model_type × TaskType`），因为 Prefix/Prompt 模型是项目自实现的主干专属类；`AUTO_MODELS` 是一维（只有 `TaskType`），因为主干选择已委托给 HF 的 `AutoModelFor*`。
- `PREFIX_MODELS` 不是满表：`deberta-v2` 除 `TOKEN_CLASSIFICATION` 外都是 `None`，组合到这些空位会触发 `None.from_pretrained(...)` 崩溃，且调用方未做保护。
- 同一个 `TaskType` 可对应不同 `fix_bert`（如 `ner` 传 `True`、`srl` 传 `False`）——「是否冻结主干」由任务包单独决定，与任务类型解耦。
- **`fix_bert` 只在全量微调（else）分支被读取**；prefix/prompt 模式下主干冻结由模型类 `__init__` 无条件完成，传入的 `fix_bert` 被忽略。
- 旧版 `get_model_deprecated` 用嵌套 if/elif + 同名 import 实现路由，脆弱且分支爆炸；当前版本把它提炼成声明式注册表，是「过程式分支 → 数据驱动查找」的典型重构。

## 7. 下一步学习建议

- 本讲把「任务名 → 模型类」的路由讲透了，下一站建议进入 **u4（数据处理与任务适配）**：看不同任务的数据如何与这里选出的模型类对接，特别是 `tasks/superglue/dataset.py` 与 `tasks/ner/dataset.py` 如何把原始数据整理成模型 forward 所需的输入。
- 想立刻看「路由结果模型」的内部细节，可结合 **u6-l2（多选与 Token 分类模型变体）** 阅读 [model/multiple_choice.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/multiple_choice.py)——本讲综合实践追踪到的 `RobertaPrefixForMultipleChoice` 就在那里。
- 想从全局视角串起「入口分派 → 任务数据 → 模型工厂 → prefix 注入 → 训练器」，可直接跳到 **u8-l1（架构总览与扩展实践）**，那里会给出新增任务/模型时需要改动的注册表与分派清单（本讲的注册表与 TaskType 正是其中的关键扩展点）。

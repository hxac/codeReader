# 全局架构回顾与扩展实践

## 1. 本讲目标

本讲是 P-tuning v2 学习手册的收官篇。前面十几讲我们已经分别拆解过 PrefixEncoder、`get_prompt`、模型工厂、数据管道、训练器与超参搜索。本讲的目标是把它们重新拼回一条**端到端主链路**，并站在「二次开发」的视角回答三个问题：

1. 一条训练命令从命令行到 `best_metrics.json`，**完整经过了哪些环节**、每一环落在哪个文件？
2. 如果我想**新增一个任务 / 一个主干模型 / 一个评测指标**，仓库预留了哪些**扩展点**、各需要改哪几处代码？
3. 本仓库**继承并魔改 HuggingFace `Trainer`** 的做法，相对原生 Trainer 做了哪些定制，**付出了什么代价**？

学完后，你应该能在不查文档的情况下，画出 P-tuning v2 的端到端调用链，并独立规划一次「接入新任务」的改动方案。

## 2. 前置知识

本讲是综合篇，默认你已学完前置讲义。这里只做最简提示，不重复展开：

- **P-tuning v2 核心**（u2-l2、u2-l3）：冻结预训练主干，用一个 `PrefixEncoder` 生成 `past_key_values`，在 Transformer **每一层**注入；浅层 Prompt 模式则只拼在嵌入层；不带 `--prefix/--prompt` 则全量微调。
- **模型工厂**（u3-l2）：`model/utils.py` 用 `TaskType` 枚举 + 三张注册表（`PREFIX_MODELS` / `PROMPT_MODELS` / `AUTO_MODELS`）做 `(model_type, task_type) → 模型类` 的二维路由。
- **数据管道**（u4-l1）：每个任务包的 `get_trainer` 会构造一个 `XxxDataset`，对外暴露 `train_dataset` / `num_labels` / `label2id` / `compute_metrics` / `data_collator` / `test_key` 等契约字段。
- **训练器**（u5-l1、u5-l2）：`BaseTrainer` 继承 HF `Trainer`，重写周期回调 `_maybe_log_save_evaluate` 来追踪 `best_metrics`；`ExponentialTrainer` 又在其上加指数学习率衰减。

一句话：本讲不引入新机制，只做**接线**与**改动清单**。

## 3. 本讲源码地图

| 文件 | 职责 | 本讲用来讲什么 |
| --- | --- | --- |
| `run.py` | 程序入口，解析参数、分派任务、驱动训练 | 端到端主链路的「总开关」与分派 |
| `tasks/utils.py` | `TASKS` / `DATASETS` / 各任务数据集名常量 | 扩展新任务的「注册表」改动点 |
| `model/utils.py` | `TaskType`、三张模型注册表、`get_model` 工厂 | 扩展新任务/新主干的「模型注册表」改动点 |
| `model/sequence_classification.py` | 各主干的前缀注入模型实现 | 验证「新分类任务能否直接复用现有模型」 |
| `training/trainer_base.py` | `BaseTrainer`：追踪 `best_metrics` | 定制 Trainer 的取舍 |
| `tasks/superglue/get_trainer.py` / `tasks/ner/get_trainer.py` | 典型 `get_trainer` 契约示例 | 新任务要照抄的「模板」 |

## 4. 核心概念与源码讲解

### 4.1 端到端主链路回顾

#### 4.1.1 概念说明

P-tuning v2 的运行时遵循一条**单向数据流**：命令行参数 → 任务分派 → 构造数据集 → 工厂选模型 → 构造训练器 → 训练循环（前向注入 + 周期评估）→ 落盘最佳指标。

这条链路上有六个「关卡」，每个关卡都对应一个明确的函数或注册表。理解这条链路的意义在于：**任何二次开发，本质都是替换或追加链路中的某一关**，而不是从头重写。这也是为什么本仓库能把分类、序列标注、问答、多选四类迥异的任务，统一塞进同一个 `run.py`。

#### 4.1.2 核心流程

下面这张流程图把整条链路按执行顺序串起来，标注了每个动作落在哪个文件：

```
┌─────────────────────────────────────────────────────────────┐
│ 1. 命令行参数                                                 │
│    run.py: get_args() 经 HfArgumentParser                    │
│    → (model_args, data_args, training_args, qa_args)         │
└──────────────────────────────┬──────────────────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────────┐
│ 2. 任务分派 (run.py if/elif)                                  │
│    按 data_args.task_name 惰性 import 对应任务包的 get_trainer │
└──────────────────────────────┬──────────────────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────────┐
│ 3. get_trainer(args) → (trainer, predict_dataset)            │
│    a. AutoTokenizer                                          │
│    b. XxxDataset: 产出 train/eval 数据 + 契约字段            │
│    c. AutoConfig(num_labels=...)                             │
│    d. get_model(model_args, TaskType.X, config) ← 工厂路由   │
│    e. Trainer(model, ..., test_key, compute_metrics, ...)    │
└──────────────────────────────┬──────────────────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────────┐
│ 4. trainer.train() (run.py 仅执行 do_train 分支)              │
│    每步前向: model.forward → get_prompt → past_key_values    │
│             注入「冻结主干」                                  │
└──────────────────────────────┬──────────────────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────────┐
│ 5. 周期回调 _maybe_log_save_evaluate (BaseTrainer)           │
│    should_evaluate → 刷新 best_metrics → (刷新时) predict    │
└──────────────────────────────┬──────────────────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────────┐
│ 6. trainer.log_best_metrics() → output_dir/best_metrics.json │
└─────────────────────────────────────────────────────────────┘
```

注意一个贯穿全链路的**「四元组约定」**：`get_args()` 返回固定顺序的 `(model_args, data_args, training_args, qa_args)`，所有下游函数都按这个顺序解包，不依赖字典 key，因此你只要按这个顺序往里塞新 dataclass，链路就能向后兼容。

#### 4.1.3 源码精读

**(1) 入口：解析四元组 + 创建产物目录**

[run.py:66-71](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/run.py#L66-L71) 是整个程序的起点，`get_args()` 解析出四元组后，只用下划线丢弃首尾两个、保留 `data_args` 与 `training_args`：

```python
args = get_args()
_, data_args, training_args, _ = args
```

随后 [run.py:93-94](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/run.py#L93-L94) 确保 `checkpoints/` 目录存在——这是所有实验产物的根。

**(2) 任务分派：if/elif 惰性 import**

[run.py:96-117](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/run.py#L96-L117) 按 `task_name` 走 if/elif，每条分支做两件事：先 `assert dataset_name` 在合法集合里，再**惰性 import** 该任务包的 `get_trainer`。以 SuperGLUE 为例：

```python
if data_args.task_name.lower() == "superglue":
    assert data_args.dataset_name.lower() in SUPERGLUE_DATASETS
    from tasks.superglue.get_trainer import get_trainer
```

「惰性」的含义是：只有真正选中该任务时才把对应模块加载进内存，避免一次性 import 五个任务包带来的依赖耦合。

**(3) 统一收口：调用 get_trainer，然后只跑 do_train**

分派之后只有**一个**统一动作 [run.py:121](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/run.py#L121)：

```python
trainer, predict_dataset = get_trainer(args)
```

任何任务的 `get_trainer` 都遵守同一契约：吃 `args`、返回 `(trainer, predict_dataset)`。之后 [run.py:138-139](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/run.py#L138-L139) 只在 `do_train` 时调用 `train(trainer, ...)`；注意 `do_eval` / `do_predict` 分支已被注释掉——评估与测试预测**全部内嵌**在训练循环的回调里完成，这是本仓库相对原生 HF 用法的关键取舍（见 4.3）。

**(4) 工厂路由：get_model 的三分支**

`get_trainer` 内部会调用 [model/utils.py:91](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L91) 的 `get_model`，由命令行开关决定走哪条注册表。[model/utils.py:92-103](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L92-L103) 的 `--prefix` 分支会先把前缀字段「粘」到 `config` 上，再从二维表里取类：

```python
if model_args.prefix:
    config.pre_seq_len = model_args.pre_seq_len
    config.prefix_projection = model_args.prefix_projection
    ...
    model_class = PREFIX_MODELS[config.model_type][task_type]
```

**(5) 前向注入：get_prompt → 冻结主干**

进入模型后，每个 `BertPrefixForXxx` 都在 `__init__` 里无条件冻结主干，并构造 `PrefixEncoder`（见 [model/sequence_classification.py:110-119](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/sequence_classification.py#L110-L119)）；前向时 [model/sequence_classification.py:160-176](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/sequence_classification.py#L160-L176) 拼接 `prefix_attention_mask` 并把 `past_key_values` 喂给冻结的 `self.bert`。

**(6) 周期回调：best_metrics 追踪**

训练循环里 [training/trainer_base.py:28](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_base.py#L28) 的 `_maybe_log_save_evaluate` 在每个评估点判断是否刷新最佳，刷新且 `predict_dataset` 非空时才跑测试预测（[training/trainer_base.py:52-63](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_base.py#L52-L63)）；训练结束 [run.py:35](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/run.py#L35) 调用 `log_best_metrics()` 把结果落盘。

#### 4.1.4 代码实践

**实践目标**：用一条真实命令，跟踪它从命令行到落盘的每一个落点。

**操作步骤**：

1. 选一个现成脚本，例如 `run_script/run_rte_roberta.sh`，读出它的命令行参数。
2. 对照本节流程图，给命令里每一个关键参数标注它「命中」链路的哪一关。例如：
   - `--task_name superglue --dataset_name rte` → 命中第 2 关（分派）与 `tasks/utils.py` 的 `SUPERGLUE_DATASETS` 校验；
   - `--prefix --pre_seq_len 128` → 命中第 3 关的 `get_model` `--prefix` 分支；
   - `--output_dir checkpoints/rte-roberta/` → 命中第 6 关的落盘路径。
3. 在 `run.py` 的每一关（第 2、3、4、5、6 关）临时各加一行 `print` 或 `logger.info`，标注「当前位于第 N 关」。

**需要观察的现象**：日志应按 1→6 的顺序依次打印，证明链路是严格单向、无回环的。

**预期结果**：你能复述出「参数解析 → 分派 → get_trainer → train → 回调 → 落盘」这六个落点各自在哪个文件、哪一行。

> 说明：本实践是「源码阅读 + 插桩」型，不要求真正完成一次训练；若本地无 GPU，可用 `--max_train_samples 4 --num_train_epochs 1` 只验证日志顺序。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `run.py` 里 `_, data_args, training_args, _ = args` 写成 `data_args = args`，会在哪一关报错？为什么？

> **答案**：会在第 2 关 `data_args.task_name` 处抛 `AttributeError`。因为 `get_args()` 返回的是四元组 tuple，不是某个 dataclass，必须先解包。

**练习 2**：为什么 `run.py` 把 `do_eval` / `do_predict` 分支注释掉了，训练却仍能在验证集上打印指标？

> **答案**：因为评估和测试预测被搬进了 `BaseTrainer._maybe_log_save_evaluate` 回调里，由训练循环按 `eval_steps` / `evaluation_strategy` 自动触发，不依赖 `do_eval`。

### 4.2 扩展点清单（任务 / 模型 / 指标）

#### 4.2.1 概念说明

「扩展点」是指：在不改动框架核心的前提下，**仅通过追加配置或新增文件**就能接入新能力的那些位置。本仓库的设计高度依赖**两张注册表 + 一处分派**，因此扩展点天然集中在三处：

1. **任务注册表**（`tasks/utils.py` 的 `TASKS` / `DATASETS`）：决定命令行 `--task_name` / `--dataset_name` 接受哪些值。
2. **模型注册表**（`model/utils.py` 的三张表）：决定 `get_model` 能路由出哪些 `(model_type, task_type)` 组合。
3. **分派表**（`run.py` 的 if/elif）：决定每个 `task_name` 绑定到哪个任务包的 `get_trainer`。

除此之外，评测指标和 Trainer 选择**不是注册表驱动**的，而是各任务包在 `get_trainer` 里自由决定，因此它们是「按任务改一处」的软扩展点。

#### 4.2.2 核心流程

扩展的决策流程可以概括为一棵决策树：

```
要接入什么？
│
├─ 新任务（如情感三分类）
│   ├─ 任务类型是否已存在？(SequenceClassification / TokenCls / QA / MultipleChoice)
│   │   ├─ 是 → 模型层零改动，复用现有 prefix 模型
│   │   └─ 否 → 需先在 TaskType 加枚举 + 写新模型类 + 注册
│   ├─ 改 tasks/utils.py：加 TASKS/DATASETS 项
│   ├─ 新建 tasks/<name>/：dataset.py + get_trainer.py
│   └─ 改 run.py：加 elif 分派分支
│
├─ 新主干模型（如 albert）
│   └─ 改 model/utils.py：在 PREFIX_MODELS/PROMPT_MODELS 各 task 下加映射
│       + 在 model/<task>.py 实现对应 *Prefix* 类（冻结 + get_prompt）
│
└─ 新评测指标
    └─ 在任务包 dataset.py 的 compute_metrics 内实现，并设 test_key
```

关键判断是：**新任务能否落到现有 `TaskType`**。如果能（绝大多数分类/标注需求都能），模型层就不用动——这是本仓库最省事的扩展路径。

#### 4.2.3 源码精读

**(1) 任务注册表：两行常量驱动全校验**

[tasks/utils.py:4-13](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/utils.py#L4-L13) 用列表推导直接从各任务包的 `task_to_keys` 反推出合法数据集名，再聚合成 `DATASETS`：

```python
GLUE_DATASETS = list(glue_tasks.keys())
SUPERGLUE_DATASETS = list(superglue_tasks.keys())
NER_DATASETS = ["conll2003", "conll2004", "ontonotes"]
...
TASKS = ["glue", "superglue", "ner", "srl", "qa"]
DATASETS = GLUE_DATASETS + SUPERGLUE_DATASETS + NER_DATASETS + SRL_DATASETS + QA_DATASETS
```

这两个常量又被 [arguments.py:22-33](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/arguments.py#L22-L33) 的 `DataTrainingArguments` 拿去当 `choices`，所以**改一处 `tasks/utils.py`，命令行校验、`run.py` 断言、`--help` 文案就全部同步**——这是「注册表驱动」的最大红利。

**(2) 模型注册表：二维路由的「空位」陷阱**

[model/utils.py:46-71](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L46-L71) 的 `PREFIX_MODELS` 是 `model_type × TaskType` 的二维表。注意 `deberta-v2` 除 token 分类外全是 `None`：

```python
"deberta-v2": {
    TaskType.TOKEN_CLASSIFICATION: DebertaV2PrefixForTokenClassification,
    TaskType.SEQUENCE_CLASSIFICATION: None,
    TaskType.QUESTION_ANSWERING: None,
    TaskType.MULTIPLE_CHOICE: None,
}
```

而 [model/utils.py:98](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L98) 取类时**没有 None 保护**：

```python
model_class = PREFIX_MODELS[config.model_type][task_type]
```

这意味着：扩展时若给某个 `(model_type, task_type)` 填了 `None`，调用方会触发 `None.from_pretrained(...)` 而崩溃。**扩展新主干时必须为每个支持的 task 都填上真实类，或显式抛出友好错误**，否则会留下「跑到才崩」的坑。

**(3) get_trainer 契约：照抄即可的模板**

[tasks/superglue/get_trainer.py:18-71](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/superglue/get_trainer.py#L18-L71) 是最完整的模板，它展示了新任务必须产出的全部契约字段：tokenizer、`XxxDataset`、`AutoConfig(num_labels=...)`、`get_model(...)`、`BaseTrainer(...)`，并 `return trainer, predict_dataset`。其中 [tasks/superglue/get_trainer.py:53-56](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/superglue/get_trainer.py#L53-L56) 还演示了**同一个任务包按数据特性动态切换 `TaskType`**：

```python
if not dataset.multiple_choice:
    model = get_model(model_args, TaskType.SEQUENCE_CLASSIFICATION, config)
else:
    model = get_model(model_args, TaskType.MULTIPLE_CHOICE, config, fix_bert=True)
```

这说明 `TaskType` 不必硬绑命令行，可由数据集对象自己决定。

**(4) 指标扩展：软扩展点，藏在 dataset 里**

以 NER 为例，[tasks/ner/get_trainer.py:63-74](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/ner/get_trainer.py#L63-L74) 在构造 `ExponentialTrainer` 时把 `compute_metrics=dataset.compute_metrics` 和 `test_key="f1"` 一起塞进去：

```python
trainer = ExponentialTrainer(
    ...,
    compute_metrics=dataset.compute_metrics,
    test_key="f1"
)
```

`compute_metrics` 由任务包自由实现（NER 用 seqeval 实体级 F1），`test_key` 是字符串，会经 [training/trainer_base.py:52](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_base.py#L52) 拼成 `eval_{test_key}` 去比较。**新增一个指标，只需在 dataset 里实现并改 `test_key`，不必动任何注册表。**

#### 4.2.4 代码实践

**实践目标**：规划「情感三分类」任务的接入方案，列出每一处改动。

**前提**：三分类是序列分类（`num_labels=3`），落在已存在的 `TaskType.SEQUENCE_CLASSIFICATION`，因此**模型层零改动**，可直接复用 `BertPrefixForSequenceClassification` 等。

**改动清单**（不实际写源码，只列方案）：

| 序号 | 文件 | 改动内容 | 性质 |
| --- | --- | --- | --- |
| 1 | `tasks/utils.py` | 新增 `SENTIMENT_DATASETS = ["sst3"]`；把 `"sentiment"` 加入 `TASKS`；把 `SENTIMENT_DATASETS` 并入 `DATASETS` | 注册表 |
| 2 | `tasks/sentiment/dataset.py`（新建） | 定义 `SentimentDataset`，暴露 `train_dataset`/`eval_dataset`/`num_labels=3`/`label2id`/`id2label`/`compute_metrics`(accuracy)/`data_collator`/`test_key="accuracy"`，并定义 `task_to_keys` | 新文件 |
| 3 | `tasks/sentiment/get_trainer.py`（新建） | 照抄 [tasks/superglue/get_trainer.py:18-71](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/superglue/get_trainer.py#L18-L71)：tokenizer → `SentimentDataset` → `AutoConfig(num_labels=3)` → `get_model(model_args, TaskType.SEQUENCE_CLASSIFICATION, config)` → `BaseTrainer(...)` → `return trainer, None` | 新文件 |
| 4 | `model/utils.py` | **无需改动**（`bert`/`roberta`/`deberta` 的 `SEQUENCE_CLASSIFICATION` 已在 `PREFIX_MODELS` 注册） | — |
| 5 | `run.py` | 加一条分派分支（见下方） | 分派表 |

`run.py` 新增分支（示例代码，非项目原有代码）：

```python
elif data_args.task_name.lower() == "sentiment":
    assert data_args.dataset_name.lower() in SENTIMENT_DATASETS
    from tasks.sentiment.get_trainer import get_trainer
```

**需要观察的现象**：改完后，`python run.py --task_name sentiment --dataset_name sst3 --prefix --help` 应能通过命令行校验，不再提示非法 choice。

**预期结果**：四类改动中只有「模型注册表」一处是空的，从而验证「同 `TaskType` 的任务可直接复用 prefix 模型」这一核心结论。

> 说明：本实践为方案设计型，不需要真正下载数据集；若要真正跑通，还需在 `SentimentDataset` 内实现 `load_dataset` 与 `preprocess_function`，可参考 [tasks/glue/get_trainer.py:45](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/glue/get_trainer.py#L45) 的最简分类流程。

#### 4.2.5 小练习与答案

**练习 1**：若要接入「albert」这个新主干做分类，需要改哪些地方？模型层能否零改动？

> **答案**：不能零改动。需要 (a) 在 `model/sequence_classification.py` 新增 `AlbertPrefixForSequenceClassification`（含冻结主干 + `get_prompt`）；(b) 在 `model/utils.py` 的 `PREFIX_MODELS["albert"]` 下为四个 `TaskType` 填类（注意 `None` 空位陷阱）；(c) 必要时在 `tasks/utils.py` 的 `ADD_PREFIX_SPACE` / `USE_FAST` 里补 `albert` 键。

**练习 2**：`test_key` 为什么是字符串而不是枚举？

> **答案**：因为 `BaseTrainer` 用字符串拼接 `eval_{test_key}` 来定位指标（[training/trainer_base.py:52](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_base.py#L52)），只要 `compute_metrics` 产出的 dict 里存在这个键即可，这让任意自定义指标都能直接接入，无需改框架。

### 4.3 定制 Trainer 的取舍

#### 4.3.1 概念说明

HuggingFace 的 `Trainer` 本身已经能完成「训练 + 评估 + checkpoint」。本仓库没有直接用它，而是写了一层 `BaseTrainer`（继承 `Trainer`）并在其上再叠 `ExponentialTrainer` / `QuestionAnsweringTrainer`。这种「薄薄包一层」的做法带来了想要的能力，但也付出了代价。理解这层取舍，是判断「该不该继续往这层加东西」的前提。

#### 4.3.2 核心流程

`BaseTrainer` 的定制逻辑非常薄，可归纳为三件事：

1. **多收两个参数**：`predict_dataset`（测试集）与 `test_key`（裁判指标名）。
2. **维护一个 `best_metrics` 字典**：用 `>` 严格比较来追踪历史最佳。
3. **重写周期回调** `_maybe_log_save_evaluate`：在刷新最佳时顺带跑一次测试预测。

它的子类分工：

| Trainer | 继承 | 额外职责 | 谁在用 |
| --- | --- | --- | --- |
| `BaseTrainer` | HF `Trainer` | 追踪 `best_metrics`、刷新最佳时 predict | GLUE / SuperGLUE（不传 predict_dataset） |
| `ExponentialTrainer` | `BaseTrainer` | 重写 `create_scheduler` 改指数衰减 | NER / SRL（传 predict_dataset + `test_key="f1"`） |
| `QuestionAnsweringTrainer` | `ExponentialTrainer` | 重写 `evaluate` 做 token→文本后处理 | QA |

#### 4.3.3 源码精读

**(1) 构造函数：多收参数 + 初始化 best_metrics**

[training/trainer_base.py:12-20](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_base.py#L12-L20) 用 `*args, **kwargs` 透传给父类，只额外初始化追踪状态：

```python
def __init__(self, *args, predict_dataset=None, test_key="accuracy", **kwargs):
    super().__init__(*args, **kwargs)
    self.predict_dataset = predict_dataset
    self.test_key = test_key
    self.best_metrics = OrderedDict({
        "best_epoch": 0,
        f"best_eval_{self.test_key}": 0,
    })
```

注意 `best_metrics` 初值是 `0`，且只缓存两个键（`best_epoch` 与 `best_eval_{test_key}`），测试集指标是「刷新最佳时才追加」的动态键。

**(2) 周期回调：三段式 + 双层 predict 闸门**

[training/trainer_base.py:28-72](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_base.py#L28-L72) 把回调拆成三段：`should_log`（打日志）、`should_evaluate`（验证 + 追踪最佳）、`should_save`（存 checkpoint）。其中 [training/trainer_base.py:52-63](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_base.py#L52-L63) 是核心：

```python
if eval_metrics["eval_"+self.test_key] > self.best_metrics["best_eval_"+self.test_key]:
    self.best_metrics["best_epoch"] = epoch
    self.best_metrics["best_eval_"+self.test_key] = eval_metrics["eval_"+self.test_key]
    if self.predict_dataset is not None:
        ...  # 只有刷新最佳 + 有测试集时，才 predict
```

**两层闸门**解释了为什么 GLUE/SuperGLUE 从不跑测试集（它们不传 `predict_dataset`），而 NER/SRL 会在刷新最佳时自动跑一次测试。

**(3) 取舍点：耦合进训练循环**

这种设计把「评估 + 测试预测」从 `run.py` 搬进了训练循环内部，好处是：`run.py` 只管 `do_train`，最佳指标自动落盘 [run.py:35](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/run.py#L35) 调用 `log_best_metrics()`；代价是：与 HF `Trainer` 的内部方法（`_maybe_log_save_evaluate`）**深度耦合**——一旦升级 `transformers` 版本、该方法签名变化，这一层就要重写。这也是为什么 `requirements.txt` 把 `transformers==4.11.3` 锁死。

#### 4.3.4 代码实践

**实践目标**：量化「定制这层」带来的训练成本收益。

**操作步骤**：

1. 阅读 [training/trainer_base.py:52-63](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_base.py#L52-L63)，回答：若一次训练共 100 个评估点，但只有第 30 个点刷新了最佳，传统「每个评估点都 predict」与本仓库「仅刷新最佳时 predict」分别会跑多少次测试集？
2. 假设测试集与验证集一样大，估算节省的预测开销比例。

**预期结果**：传统做法约 100 次 predict，本仓库仅 1 次，节省约 99% 的测试预测算力（前提是验证集能稳定单调逼近最佳）。

> 说明：这是源码阅读 + 推理型实践，无需运行；它揭示的是「为何把 predict 嵌入回调」的设计动机。

#### 4.3.5 小练习与答案

**练习 1**：`best_metrics` 用 `>` 而非 `>=` 比较历史最佳，有什么副作用？

> **答案**：`>` 是严格大于，指标**打平**时不刷新。好处是避免在指标平台期反复改写 `best_epoch`、反复跑 predict；副作用是「打平即不更新」，若两次打平的模型其实不同，只会保留最早那次的记录。

**练习 2**：QA 任务为什么不能直接用 `BaseTrainer`，而要新写 `QuestionAnsweringTrainer`？

> **答案**：因为抽取式问答的输出是 token 级 start/end logits，不能直接 `argmax` 算指标，必须经过 `post_process_function`（用 `offset_mapping` 把 token 下标还原为答案文本）再算 F1/EM。这套后处理要在 `evaluate` 里临时关闭 `compute_metrics`、跑完再加回来，因此必须重写 `evaluate`，无法用单一 `test_key` 表达。

## 5. 综合实践

**任务**：把本讲三块内容串起来，完成一次完整的「新任务接入方案设计」。

请为一个**自定义情感三分类任务**（标签：负面/中性/正面，`num_labels=3`）产出一份接入方案，要求覆盖：

1. **链路定位**：参照 4.1 的流程图，说明这个任务会命中链路的哪几关、复用哪些既有机制。
2. **改动清单**：按 4.2 的「决策树」与改动表，逐文件列出要改什么（`tasks/utils.py`、`tasks/sentiment/`、`model/utils.py`、`run.py`）。
3. **模型复用判断**：明确回答能否直接复用 `BertPrefixForSequenceClassification`，并说明 `--prefix --pre_seq_len 128` 在该模型里会触发哪些既有代码（冻结主干、`get_prompt`、`past_key_values` 注入）。
4. **Trainer 选择**：参照 4.3，决定该任务该用 `BaseTrainer` 还是 `ExponentialTrainer`，给出 `test_key` 与 `predict_dataset` 的取值，并解释为什么不必新写 Trainer 子类。

**预期产物**：一份约 300 字的方案文档，外加一张「改动文件 → 改动性质（注册表/新文件/分派/无改动）」的对照表。核心结论应当是：**情感三分类属于既有 `TaskType`，因此模型层与 Trainer 层零改动，工作集中在数据管道与两处注册表/分派**。

## 6. 本讲小结

- P-tuning v2 的端到端链路是**单向六关**：参数解析 → 任务分派 → `get_trainer`（数据+模型+训练器）→ `trainer.train`（前向注入）→ 周期回调（best 追踪）→ 落盘 `best_metrics.json`；全链路由「四元组 `args`」贯穿。
- 扩展点集中在**两张注册表 + 一处分派**：`tasks/utils.py`（任务/数据集名）、`model/utils.py`（`(model_type, TaskType)` 模型表）、`run.py`（if/elif 分派）；指标与 Trainer 是任务包内的软扩展点。
- **同 `TaskType` 的新任务可零改动复用 prefix 模型**——情感三分类只需改数据管道 + 注册表 + 分派，不动模型层。
- 模型注册表存在 **`None` 空位陷阱**：`get_model` 取类时无 None 保护，扩展新主干时必须为每个支持的 task 填真实类。
- `BaseTrainer` 用「`>` 严格比较 + 刷新最佳才 predict」把评估/测试嵌进训练回调，节省测试算力，但代价是与 HF `Trainer` 内部方法**强耦合**，故 `transformers` 版本必须锁死。
- 本仓库相对原生 HF Trainer 的核心定制只有「薄薄一层」：多收 `predict_dataset` / `test_key`、维护 `best_metrics`、重写一个回调方法；理解这一层就理解了全部训练侧定制。

## 7. 下一步学习建议

到这里，P-tuning v2 主项目的源码已通读完毕。建议按以下方向继续：

1. **亲手把综合实践落地**：真正实现一个最小可跑的 `tasks/sentiment/`（哪怕是 100 条假数据），用它验证你对扩展点的理解；这是从「读懂」到「能改」的关键一步。
2. **横向迁移到 PT-Retrieval（u7 单元）**：把同样的「冻结主干 + 逐层 prefix」机制，放到 DPR 双塔检索里观察它如何让检索器更跨域、更校准；对照 `PT-Retrieval/dpr/models/prefix.py` 与主项目 `model/prefix_encoder.py` 的字段差异（`prefix_mlp` vs `prefix_projection`）。
3. **阅读论文与超参搜索脚本**：回到 `search_script/` 与 `search.py`，结合 u5-l2，亲手跑一次小网格搜索，体会 P-tuning v2 对 `lr`/`pre_seq_len` 的高度敏感性，理解为什么本手册反复强调「复现不到先做超参搜索」。
4. **关注版本耦合风险**：尝试把 `transformers` 升一个小版本，观察 `BaseTrainer._maybe_log_save_evaluate` 是否报错，借此体会「定制 Trainer」的长期维护成本，并思考如何用回调（`Callback`）替代方法重写来降低耦合。

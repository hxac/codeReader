# 目录结构与主入口 run.py

> 本讲属于「从零认识 P-tuning v2」单元（U1），承接 u1-l1（概念定位）与 u1-l2（环境与首次运行）。
> 本讲不深入模型内部，只解决一个问题：**整个项目是怎么「组织」起来、又怎么「跑」起来的。**

## 1. 本讲目标

学完本讲后，你应该能够：

1. 画出 P-tuning v2 根目录的顶层结构，并说清 `model/`、`training/`、`tasks/`、`run_script/`、`search_script/` 各自负责什么。
2. 看懂主入口 `run.py` 的三段式结构：**解析参数 → 按任务名分派 → 训练与评估**。
3. 理解 `get_args()` 为什么返回一个「四元组」，以及这个四元组如何在系统里流动。
4. 说清 `TASKS` / `DATASETS` 这两个常量是如何「登记」所有合法任务和数据集的。
5. 在脑中建立一条贯穿全局的心智模型：**命令行参数 → `get_args` → 任务分派 → `get_trainer` → `trainer.train`**。
6. 知道如果要新增一个任务类型，需要改动哪几处代码。

## 2. 前置知识

本讲需要你已经具备（来自前两讲或基础常识）：

- **P-tuning v2 的基本概念**：冻结预训练主干、只在每一层注入一小段可训练「前缀」参数（u1-l1）。
- **命令行运行方式**：知道训练是通过 `run.py` 加一堆 `--xxx` 参数启动的，例如 `--task_name`、`--prefix`、`--pre_seq_len`（u1-l2）。
- **Python 基础**：知道 `import`、`if/elif/else`、`tuple`（元组）解包、`dataclass`（数据类）是什么。
- **一个关键直觉**：这个项目支持很多种任务（分类、标注、问答……），但每次运行只做其中一种。所以入口必须能「根据用户选的任务，加载对应的那一套代码」。本讲的核心就是讲清楚这个「分派」机制。

> 名词解释：
> - **入口（entry）**：程序执行的起点，这里是 `run.py` 的 `if __name__ == '__main__':` 块。
> - **分派（dispatch）**：根据某个条件（这里是任务名）选择走哪一段代码路径。
> - **任务包（task package）**：`tasks/` 下每个子目录（如 `superglue/`）封装了「做某类任务所需的全部东西」。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `run.py` | 整个项目的**主入口** | 参数解析、任务分派、训练/评估主流程 |
| `arguments.py` | 定义所有命令行参数，提供 `get_args()` | `get_args` 返回的四元组结构 |
| `tasks/utils.py` | 任务/数据集**注册表**常量 | `TASKS`、`DATASETS`、各 `*_DATASETS` |
| `tasks/superglue/get_trainer.py` | 某个任务的「组装工厂」示例 | `get_trainer(args)` 的输入输出契约 |

> 这些是本讲的「骨架」。模型内部（`model/`）和 Trainer 内部（`training/`）会在后续讲义展开，本讲只用它们的「名字」和「职责」，不展开实现。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. **4.1 项目顶层目录与职责划分** —— 先建立「地图」。
2. **4.2 `get_args` 返回四元组** —— 看清参数是怎么进来的。
3. **4.3 `TASKS` / `DATASETS` 常量注册表** —— 看清合法任务是怎么登记的。
4. **4.4 `task_name` 断言、动态分派与训练评估主流程** —— 把参数、任务、训练器串成一条线。

### 4.1 项目顶层目录与职责划分

#### 4.1.1 概念说明

P-tuning v2 是一个「多任务」研究项目：同一套 P-tuning v2 思想，要能套到**分类、序列标注、问答**等多种任务上，又要支持 **BERT / RoBERTa / DeBERTa** 等多种主干。如果所有代码堆在一个文件里会非常混乱。

因此项目采用了**「入口 + 分层目录」**的组织方式：

- 一个**唯一的入口** `run.py`，负责「听命令、找对人、开跑」。
- 几个**按职责切分**的目录：模型放一处、训练器放一处、任务数据放一处、运行脚本放一处。

读者第一件事不是读模型，而是**先看地图**：知道东西大概放在哪、谁调用谁。

#### 4.1.2 核心流程

顶层目录的职责划分如下（排除 `PT-Retrieval/` 检索子项目，它有自己的入口，本讲不涉及）：

| 顶层条目 | 类型 | 职责 |
| --- | --- | --- |
| `run.py` | 入口脚本 | 解析参数、按任务名分派、调用训练与评估 |
| `arguments.py` | 参数定义 | 用 `dataclass` 声明所有 `--xxx` 参数，提供 `get_args()` |
| `model/` | 模型实现 | P-tuning v2 各任务模型（`prefix_encoder.py`、`sequence_classification.py` 等） |
| `training/` | 训练器 | 继承自 HF `Trainer` 的子类（`trainer_base.py`、`trainer_exp.py`、`trainer_qa.py`） |
| `tasks/` | 任务包 | 每个子目录一类任务的数据加载 + `get_trainer.py`（`glue/`、`superglue/`、`ner/`、`srl/`、`qa/`），外加 `utils.py` 注册表 |
| `run_script/` | 运行脚本 | 各任务各主干的训练命令示例（`.sh`） |
| `search_script/` | 搜索脚本 | 超参网格搜索的 `.sh` 脚本 |
| `search.py` | 汇总工具 | 读取搜索产物 `best_results.json`，挑出最优配置 |
| `requirements.txt` | 依赖 | 锁定的依赖版本 |
| `figures/`、`README.md`、`LICENSE` | 文档/图 | 项目说明与论文图 |

记住一句话：**「入口在根，模型在 `model/`，训练器在 `training/`，任务数据 + 组装在 `tasks/`，怎么跑在 `run_script/`。」**

#### 4.1.3 源码精读

`run.py` 顶部的 import 直接暴露了目录之间的依赖关系：

[run.py:12-16](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/run.py#L12-L16) —— 入口同时引用了「参数模块」「任务注册表」，而任务注册表用 `*` 全部导入：

```python
from arguments import get_args      # 参数解析
from tasks.utils import *           # 把 TASKS / DATASETS / *_DATASETS 全部引入当前命名空间
os.environ["WANDB_DISABLED"] = "true"
```

这里有两个值得注意的设计：

- `from tasks.utils import *` 让 `run.py` 后面可以直接写 `SUPERGLUE_DATASETS`、`TASKS` 等名字，不必加前缀。
- 第 16 行关闭了 wandb（一个实验记录工具），说明项目默认不依赖外部日志服务，结果直接落到 `checkpoints/` 目录。

各任务子包的内部结构是统一的（以 `superglue` 为例）：

- `tasks/superglue/dataset.py` —— 数据加载与预处理。
- `tasks/superglue/get_trainer.py` —— 把「数据 + 模型 + 训练器」组装成一个 `Trainer` 对象。
- `tasks/superglue/utils.py`、`dataset_record.py` —— 该任务专属的辅助逻辑。

这种「**每个任务包都暴露一个同名函数 `get_trainer`**」的约定，正是后面分派机制能成立的基础。

#### 4.1.4 代码实践

1. **实践目标**：亲手看清目录结构，而不是凭记忆。
2. **操作步骤**：在仓库根目录执行只读 git 命令（不会改动任何文件）：
   ```bash
   git ls-files | cut -d/ -f1 | sort -u
   ```
   再看任务包内部：
   ```bash
   git ls-files tasks | cut -d/ -f1-2 | sort -u
   ```
3. **需要观察的现象**：第一条命令列出所有顶层条目；第二条列出 `tasks/` 下的子目录与 `utils.py`。
4. **预期结果**：顶层能看到 `model`、`training`、`tasks`、`run_script`、`search_script`、`run.py`、`arguments.py`、`search.py` 等；`tasks/` 下能看到 `glue`、`superglue`、`ner`、`srl`、`qa` 五个子目录和一个 `utils.py`。

#### 4.1.5 小练习与答案

**练习 1**：如果你想找「RoBERTa 在 RTE 上训练」的具体命令，应该去哪个目录？
**答案**：`run_script/`。里面存放各任务各主干的 `.sh` 示例脚本。

**练习 2**：`model/` 和 `training/` 的分工区别是什么？
**答案**：`model/` 定义「前向计算」（模型长什么样、前缀怎么注入），`training/` 定义「怎么训练」（优化循环、学习率调度、最佳指标追踪）。模型是「被训练的对象」，训练器是「驱动训练的引擎」。

### 4.2 `get_args` 返回四元组

#### 4.2.1 概念说明

启动训练时，用户在命令行写了一长串 `--task_name superglue --dataset_name rte --prefix ...`。这些字符串需要被解析成程序能用的结构。

HuggingFace 提供了一个工具 `HfArgumentParser`：你只要把参数写成一个 Python **`dataclass`（数据类）**，它就能自动把 dataclass 的字段翻译成命令行参数，再把命令行的值填回 dataclass 实例。

项目把参数分成**四组** dataclass，对应四种关切：

1. **ModelArguments** —— 用哪个模型、是否开 P-tuning v2（`--prefix`）、前缀长度（`--pre_seq_len`）等。
2. **DataTrainingArguments** —— 做哪个任务（`--task_name`）、哪个数据集（`--dataset_name`）、序列长度等。
3. **TrainingArguments** —— HF 自带的训练超参（学习率、batch size、epoch、`--do_train` 等）。
4. **QuestionAnwseringArguments** —— 问答任务专属参数（`n_best_size` 等）。

`get_args()` 把这四组按顺序解析，**返回一个长度为 4 的元组**——这就是「四元组」。

#### 4.2.2 核心流程

```
命令行字符串
   │  HfArgumentParser((ModelArgs, DataArgs, TrainingArgs, QAArgs))
   ▼
parse_args_into_dataclasses()
   │  按顺序填回四个 dataclass 实例
   ▼
返回 (ModelArgs 实例, DataArgs 实例, TrainingArgs 实例, QAArgs 实例)
   │  —— 即「四元组」 args
   ▼
由调用方按需解包使用
```

关键点：四元组里**元素的顺序**与传给 `HfArgumentParser` 的 dataclass 顺序**完全一致**。

#### 4.2.3 源码精读

`get_args` 的实现非常短：

[arguments.py:187-193](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/arguments.py#L187-L193) —— 把四个 dataclass 一起喂给解析器，再返回解析结果：

```python
def get_args():
    """Parse all the args."""
    parser = HfArgumentParser((ModelArguments, DataTrainingArguments,
                               TrainingArguments, QuestionAnwseringArguments))
    args = parser.parse_args_into_dataclasses()
    return args
```

`run.py` 拿到四元组后，**只解包它当前需要的两个**，其余用 `_` 占位：

[run.py:68-70](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/run.py#L68-L70) —— 调用 `get_args()`，再解包：

```python
args = get_args()                       # args 是四元组
_, data_args, training_args, _ = args   # 只取「数据参数」和「训练参数」
```

注意：这里用 `_` 丢掉了 `ModelArguments` 和 `QAArguments`，**并不是它们没用**——稍后 `run.py` 会把**整个 `args` 四元组原样**传给 `get_trainer(args)`（见 4.4.3），由任务包内部再解包。所以四元组是「贯穿全系统的标准容器」。

任务包内部对四元组的解包方式是统一的，例如：

[tasks/superglue/get_trainer.py:18-19](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/superglue/get_trainer.py#L18-L19) —— 任务包同样按固定顺序解包四元组：

```python
def get_trainer(args):
    model_args, data_args, training_args, _ = args
```

> 小结：四元组 = `(model_args, data_args, training_args, qa_args)`，顺序固定、全系统通用。

#### 4.2.4 代码实践

1. **实践目标**：验证四元组的顺序与组成。
2. **操作步骤**：阅读 [arguments.py:187-193](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/arguments.py#L187-L193)，记下传给 `HfArgumentParser` 的四个 dataclass 的**顺序**；再对比 [run.py:70](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/run.py#L70) 的解包顺序。
3. **需要观察的现象**：两处的顺序应当一一对应。
4. **预期结果**：第 1 位 = `ModelArguments`，第 2 位 = `DataTrainingArguments`，第 3 位 = `TrainingArguments`，第 4 位 = `QuestionAnwseringArguments`。
5. 若想本地确认，可在已装好依赖的环境里运行 `python -c "from arguments import get_args; print(type(get_args()))"`（需补全必要参数），观察返回类型为 `tuple`，长度为 4。命令的具体输出**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `run.py` 第 70 行用 `_` 丢弃了 `ModelArguments`，但后面又能用到模型相关的参数？
**答案**：因为 `run.py` 后面把**整个 `args` 四元组**（含 `ModelArguments`）传给了 `get_trainer(args)`；任务包内部会再次解包取出 `model_args`。丢弃只是「这一行暂不需要」，并非真的扔掉。

**练习 2**：如果要新增一个「只对问答有用」的参数，应该加到哪个 dataclass？
**答案**：加到 `QuestionAnwseringArguments`，这样它只对问答任务有意义，不会污染其它任务的参数空间。

### 4.3 `TASKS` / `DATASETS` 常量注册表

#### 4.3.1 概念说明

系统要能判断「用户给的任务名合不合法」「这个任务支持哪些数据集」。最直接的办法是维护一份**清单（注册表）**：

- `TASKS`：所有合法的任务类型名，如 `glue`、`superglue`、`ner`、`srl`、`qa`。
- `DATASETS`：所有合法的数据集名，是各任务数据集的汇总。

这份清单有两个用途：

1. **生成命令行帮助与校验**：`DataTrainingArguments` 里 `task_name` 用 `choices=TASKS`，传进 `HfArgumentParser` 后，非法任务名在解析阶段就会被拒绝。
2. **运行时分派时的断言**：`run.py` 会再断言「数据集名确实属于该任务」。

#### 4.3.2 核心流程

```
tasks/glue/dataset.py        task_to_keys ─┐
tasks/superglue/dataset.py   task_to_keys ─┤  读取各任务的「数据集名 → 输入列名」映射
                                          ▼
              GLUE_DATASETS = list(glue_tasks.keys())        ─┐
              SUPERGLUE_DATASETS = list(superglue_tasks.keys())┤
              NER_DATASETS / SRL_DATASETS / QA_DATASETS        │  每类任务一个列表
                                                              ▼
              TASKS = ["glue","superglue","ner","srl","qa"]
              DATASETS = 五个列表相加                      （最终注册表）
```

也就是说：**数据集清单不是手写硬编码的**，而是从各任务的 `task_to_keys` 映射里「派生」出来的——这保证「登记一次，处处可用」。

#### 4.3.3 源码精读

[tasks/utils.py:1-13](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/utils.py#L1-L13) —— 注册表的核心定义：

```python
from tasks.glue.dataset import task_to_keys as glue_tasks
from tasks.superglue.dataset import task_to_keys as superglue_tasks

GLUE_DATASETS = list(glue_tasks.keys())
SUPERGLUE_DATASETS = list(superglue_tasks.keys())
NER_DATASETS = ["conll2003", "conll2004", "ontonotes"]
SRL_DATASETS = ["conll2005", "conll2012"]
QA_DATASETS = ["squad", "squad_v2"]

TASKS = ["glue", "superglue", "ner", "srl", "qa"]
DATASETS = GLUE_DATASETS + SUPERGLUE_DATASETS + NER_DATASETS + SRL_DATASETS + QA_DATASETS
```

要点：

- `GLUE_DATASETS` / `SUPERGLUE_DATASETS` 从 `task_to_keys` 的键派生（分类任务的数据集名 = 该映射的键）。
- `NER_DATASETS` / `SRL_DATASETS` / `QA_DATASETS` 是显式列表（标注、问答任务的数据集较少且固定）。
- `DATASETS` 是全部数据集的**并集**（拼接，不是去重）。

这份注册表还顺带定义了**分词器相关**的两个查表字典，本讲只需知道它们存在、后续数据预处理讲义会用到：

[tasks/utils.py:15-29](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/utils.py#L15-L29) —— 按主干类型决定分词器选项（如 RoBERTa 需要 `add_prefix_space=True`，deberta-v2 不用 fast tokenizer）。

注册表里的 `TASKS` 同时被 `DataTrainingArguments` 用作 `choices`：

[arguments.py:22-33](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/arguments.py#L22-L33) —— `task_name` / `dataset_name` 的合法取值由注册表决定：

```python
task_name: str = field(metadata={"help": "...", "choices": TASKS})
dataset_name: str = field(metadata={"help": "...", "choices": DATASETS})
```

#### 4.3.4 代码实践

1. **实践目标**：理解「新增数据集时，清单是如何自动扩张的」。
2. **操作步骤**：打开 `tasks/superglue/dataset.py`，找到 `task_to_keys` 字典（这是 `SUPERGLUE_DATASETS` 的来源）。设想你在该字典里**新增一个键**（例如一个假数据集名 `"myrte"`）。
3. **需要观察的现象**：由于 `SUPERGLUE_DATASETS = list(superglue_tasks.keys())`、`DATASETS = ... + SUPERGLUE_DATASETS + ...`，新键会**自动**出现在 `SUPERGLUE_DATASETS` 和 `DATASETS` 里，无需手动改注册表。
4. **预期结果**：`--dataset_name myrte` 在参数解析阶段会被接受为合法值（当然，真正能跑还需要实现对应的数据加载逻辑）。
5. **注意**：本步骤仅为「阅读推理」，不要真的修改源码文件。

#### 4.3.5 小练习与答案

**练习 1**：`DATASETS` 是用 `+` 拼接五个列表得到的，如果两个任务碰巧有同名数据集会怎样？
**答案**：`DATASETS` 里会出现重复项。对 `--dataset_name` 的 `choices` 校验没影响（重复也能匹配），但语义上应避免，因为分派时是用「任务名 + 数据集名」一起定位的，同名会带来歧义。

**练习 2**：`NER_DATASETS` 为什么写成显式列表，而不像 `GLUE_DATASETS` 那样从 `task_to_keys` 派生？
**答案**：因为 NER 这类任务的数据预处理结构不同（按 token 对齐标签），不一定有一个统一的 `task_to_keys` 模板映射；项目作者选择用显式列表更直接。

### 4.4 `task_name` 断言、动态分派与训练评估主流程

> 这是本讲的核心模块。前三个模块都是为它服务的：参数（4.2）告诉系统「做什么」，注册表（4.3）告诉系统「什么是合法的」，本模块讲系统**如何据此分派并开跑**。

#### 4.4.1 概念说明

「分派」要解决的问题是：**同一个 `run.py`，要根据用户选的任务，去加载完全不同的任务包。**

项目采用了一个优雅的做法——**惰性（按需）import**：在 `run.py` 顶部**不**导入任何任务包，而是等确定了任务名之后，在对应的 `if/elif` 分支里才 `from tasks.xxx.get_trainer import get_trainer`。

这样做有两个好处：

1. **省资源**：只加载你这次要用到的那一个任务包，不必把五个任务包及其依赖全部加载。
2. **统一接口**：每个任务包都约定导出一个**同名函数 `get_trainer`**，于是分派结束后，`run.py` 后面的代码可以**统一地**调用 `get_trainer(args)`，不必关心具体是哪个任务。

#### 4.4.2 核心流程

`run.py` 的 `__main__` 块可以归纳为五步：

```
1. 解析参数        args = get_args()                      得到四元组
   └─ 解包拿到 data_args / training_args

2. 配置日志        logging.basicConfig + set_verbosity

3. 建产物目录      若 checkpoints/ 不存在则创建

4. 任务分派        根据 data_args.task_name 进入对应 if/elif 分支
   ├─ 断言 dataset_name 属于该任务的 *_DATASETS
   └─ 按需 import：from tasks.<task>.get_trainer import get_trainer

5. 组装并训练
   ├─ trainer, predict_dataset = get_trainer(args)       复用统一接口
   ├─ 检测 output_dir 里的 checkpoint（用于断点续训）
   └─ if training_args.do_train: train(trainer, ...)     开跑
```

一个容易被忽略的事实：`run.py` 里虽然**定义**了 `evaluate()` 和 `predict()` 两个函数，但在主流程里调用它们的代码是**被注释掉的**（见 4.4.3）。真正的验证集评估发生在 **Trainer 内部的训练循环**里（由 `BaseTrainer` 驱动），这是 u5-l1 的内容；本讲只需知道：**主入口默认只负责「训练」，评估穿插在训练过程中。**

#### 4.4.3 源码精读

**(a) 任务分派的 if/elif 链**

[run.py:96-117](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/run.py#L96-L117) —— 按 `task_name` 分派，每个分支先断言数据集合法性，再按需导入 `get_trainer`：

```python
if data_args.task_name.lower() == "superglue":
    assert data_args.dataset_name.lower() in SUPERGLUE_DATASETS
    from tasks.superglue.get_trainer import get_trainer
elif data_args.task_name.lower() == "glue":
    assert data_args.dataset_name.lower() in GLUE_DATASETS
    from tasks.glue.get_trainer import get_trainer
elif data_args.task_name.lower() == "ner":
    assert data_args.dataset_name.lower() in NER_DATASETS
    from tasks.ner.get_trainer import get_trainer
# ... srl、qa 同理 ...
else:
    raise NotImplementedError('Task {} is not implemented. Please choose a task from: {}'
                              .format(data_args.task_name, ", ".join(TASKS)))
```

注意三点：

- 判断依据是 **`data_args.task_name`**（不是 model_args）。
- 每个分支的 `assert` 用到的常量（如 `SUPERGLUE_DATASETS`）来自顶部的 `from tasks.utils import *`。
- `else` 分支用 `TASKS` 拼出友好的错误提示——这是注册表的第三个用途。
- 这是一种「**双重校验**」：`task_name` 在参数解析阶段已经被 `choices=TASKS` 拦过一次，这里再用断言做运行时保护。

**(b) 统一组装：调用 `get_trainer`**

[run.py:119-121](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/run.py#L119-L121) —— 分派完成后，**不再区分**是哪个任务，统一调用：

```python
set_seed(training_args.seed)
trainer, predict_dataset = get_trainer(args)
```

`get_trainer` 的契约（以 superglue 为例）是：吃整个四元组 `args`，吐出 `(trainer, predict_dataset)`。

[tasks/superglue/get_trainer.py:30-71](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/superglue/get_trainer.py#L30-L71) —— 任务包内部把「数据集 → 配置 → 模型 → 训练器」串起来，最后 `return trainer, None`：

```python
dataset = SuperGlueDataset(tokenizer, data_args, training_args)   # 数据
...
model = get_model(model_args, TaskType.SEQUENCE_CLASSIFICATION, config)  # 模型
trainer = BaseTrainer(model=model, args=training_args,
                      train_dataset=..., eval_dataset=...,
                      compute_metrics=dataset.compute_metrics, ...)        # 训练器
return trainer, None
```

可以看到，**「任务包 = 数据 + 模型 + 训练器」的组装车间**，这就是它叫 `get_trainer` 的原因（它返回一个配置好的 trainer）。

**(c) 训练与（被注释的）评估**

[run.py:20-35](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/run.py#L20-L35) —— `train()` 函数：调用 `trainer.train(...)`，记录指标，最后 `trainer.log_best_metrics()`：

```python
def train(trainer, resume_from_checkpoint=None, last_checkpoint=None):
    ...
    train_result = trainer.train(resume_from_checkpoint=checkpoint)
    metrics = train_result.metrics
    trainer.log_metrics("train", metrics)
    trainer.save_metrics("train", metrics)
    trainer.save_state()
    trainer.log_best_metrics()       # 打印最佳指标（由 BaseTrainer 提供）
```

[run.py:138-145](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/run.py#L138-L145) —— 主流程里**只有 `do_train` 这一条分支真正生效**，`do_eval` / `do_predict` 的调用被注释掉了：

```python
if training_args.do_train:
    train(trainer, training_args.resume_from_checkpoint, last_checkpoint)

# if training_args.do_eval:
#     evaluate(trainer)
# if training_args.do_predict:
#     predict(trainer, predict_dataset)
```

这意味着：评估不是在训练结束后由 `run.py` 单独触发，而是**由 Trainer 在训练过程中自动执行**（每个 epoch 验证、追踪最佳指标）。这与 4.4.2 的结论一致，也是为什么后面 u5-l1 要专门讲 `BaseTrainer` 的 `_maybe_log_save_evaluate`。

> 在调用 `train` 之前，[run.py:123-135](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/run.py#L123-L135) 还有一段「断点续训」检测：如果 `output_dir` 里已存在 checkpoint 且未设置 `--overwrite_output_dir`，就从中断处恢复，避免重复训练或误覆盖。

#### 4.4.4 代码实践（本讲主实践）

1. **实践目标**：把「入口 → 训练」的完整调用链画成流程图，并总结新增一个任务类型要改哪些地方。
2. **操作步骤**：
   - 打开 [run.py:96-121](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/run.py#L96-L121)，逐行追踪一次 `python run.py --task_name superglue --dataset_name rte --do_train ...` 的执行路径。
   - 在纸上或文本里画出下面的流程图（已给出骨架，请补全箭头两端的「数据形态」）。
3. **需要观察的现象 / 流程图骨架**：
   ```
   命令行参数 --task_name superglue --dataset_name rte --do_train
        │
        ▼  get_args()                                    形态：四元组 args
   解析为 (model_args, data_args, training_args, qa_args)
        │
        ▼  data_args.task_name.lower() == "superglue"     分派命中
   assert dataset_name in SUPERGLUE_DATASETS
   from tasks.superglue.get_trainer import get_trainer    按需加载
        │
        ▼  trainer, predict_dataset = get_trainer(args)   统一接口
   任务包内部：数据集 → config → get_model(...) → BaseTrainer(...)
        │
        ▼  if training_args.do_train: train(trainer, ...)
   trainer.train(...) → 训练循环（内含评估）→ log_best_metrics()
   ```
4. **预期结果**：你能对着流程图说出每一步「当前的变量是什么、为什么走这条分支」。
5. **附加任务（新增任务类型的改动清单）**：假设要新增一个任务类型 `sentiment`（情感三分类），需要改动**至少三处**：
   - **`tasks/utils.py`**：把 `"sentiment"` 加入 `TASKS`；新建 `SENTIMENT_DATASETS` 列表；把它加进 `DATASETS` 的拼接。
   - **新建任务包**：`tasks/sentiment/` 目录，至少包含 `get_trainer.py`（导出同名函数 `get_trainer`）和数据加载 `dataset.py`。
   - **`run.py`**：在 if/elif 链里新增一个分支，`assert dataset_name in SENTIMENT_DATASETS`，并 `from tasks.sentiment.get_trainer import get_trainer`。
   - （可选）在 `run_script/` 增加一份示例 `.sh`。模型侧若复用现有 `SequenceClassification` 的 prefix 模型，则**不必**改 `model/`。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `from tasks.superglue.get_trainer import get_trainer` 改成放到 `run.py` 顶部（无条件 import），会有什么坏处？
**答案**：每次运行都会加载**全部五个任务包**及其依赖（包括你这次根本用不到的任务），启动变慢、内存占用变大；而且任意一个任务包有 import 期错误都会让整个入口崩溃。惰性 import 隔离了这些影响。

**练习 2**：为什么 `run.py` 的 `do_eval` / `do_predict` 分支被注释掉了，项目却还保留 `evaluate()` / `predict()` 两个函数？
**答案**：这两个函数提供了「单独评估/预测」的能力，留作备用或二次开发时手动启用；当前主流程把评估交给了 Trainer 内部的回调，所以入口层暂时不需要显式调用。保留它们降低了二次开发的改动量。

**练习 3**：分派时用的是 `data_args.task_name.lower()`（转小写），而 `TASKS` 里是小写。这种「大小写归一」有什么好处？
**答案**：让用户在命令行写 `SuperGLUE`、`Superglue`、`superglue` 都能匹配，提升容错性；同时断言里对 `dataset_name` 也做了 `.lower()`，保证校验一致。

## 5. 综合实践

**任务：用一句话命令 + 一张图，讲清「从命令行到训练启动」的全过程，并定位新增任务的扩展点。**

具体步骤：

1. 选一个真实的运行脚本（如 `run_script/run_rte_roberta.sh`），找到它最终调用的 `python run.py ...` 那一行，**只读不改**。
2. 按本讲 4.4.4 的流程图，标注该命令里的每个关键参数分别落在四元组的哪一组（`--prefix`、`--pre_seq_len` 落 `ModelArguments`；`--task_name`、`--dataset_name` 落 `DataTrainingArguments`；`--do_train`、`--learning_rate` 落 `TrainingArguments`）。
3. 回答：这条命令会命中 [run.py:96-117](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/run.py#L96-L117) 的哪一个分支？最终 `get_trainer` 来自哪个文件？
4. 写一份「新增 `sentiment` 任务」的最小改动清单（参考 4.4.4 的附加任务），并说明：如果新任务的模型就是普通的序列分类，能否直接复用 `get_model(model_args, TaskType.SEQUENCE_CLASSIFICATION, config)`（提示：可以，这正是注册表与统一接口带来的好处）。

> 通过这个综合实践，你会真切体会到：**入口只负责「分派」，真正的「能力」分散在任务包/模型/训练器里，而把它们粘合起来的，正是「四元组 + 注册表 + 同名 `get_trainer` 接口」这三个约定。**

## 6. 本讲小结

- 顶层目录按职责清晰分层：**入口 `run.py`、参数 `arguments.py`、模型 `model/`、训练器 `training/`、任务包 `tasks/`、脚本 `run_script/` 与 `search_script/`**。
- `get_args()` 返回一个**四元组** `(model_args, data_args, training_args, qa_args)`，顺序固定、全系统通用；调用方可按需解包。
- `tasks/utils.py` 用 `TASKS`（任务清单）和 `DATASETS`（数据集汇总）两个常量充当**注册表**，既驱动命令行 `choices` 校验，又驱动运行时断言。
- `run.py` 的核心是**按 `task_name` 的 if/elif 分派 + 惰性 import**：每个分支断言数据集合法性后，按需导入对应任务包的同名函数 `get_trainer`。
- 分派之后代码不再区分任务，统一调用 `trainer, predict_dataset = get_trainer(args)`；任务包是「数据 + 模型 + 训练器」的组装车间。
- 主流程当前**只真正执行训练**（`do_train`）；`do_eval`/`do_predict` 分支被注释，评估由 Trainer 内部回调驱动——这是下一讲（u5-l1）的伏笔。

## 7. 下一步学习建议

本讲建立了「入口 → 任务 → 训练器」的骨架，但故意没展开两块「肉」：

- **想看「模型」是怎么被造出来的**：进入 **u2（P-tuning v2 核心机制）**，从 [model/prefix_encoder.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/prefix_encoder.py) 的 `PrefixEncoder` 开始，理解前缀参数如何生成并被注入主干。
- **想看「训练器」内部如何评估、如何追踪最佳指标**：直接跳到 **u5-l1（BaseTrainer 与最佳指标追踪）**，精读 [training/trainer_base.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_base.py)，弄清本讲留下的「评估为何在 Trainer 内部」这个伏笔。
- **想理解参数如何流向模型 config**：阅读 **u3-l1（参数体系与配置流转）**，追踪 `--prefix`、`--pre_seq_len` 是怎样从四元组写入 `config`、再被 `get_model` 读取的。

> 建议按学习路线顺序推进：先 u2（机制）→ u3（配置与工厂）→ u4（数据）→ u5（训练与搜索），把本讲这条「主链路」逐步填满细节。

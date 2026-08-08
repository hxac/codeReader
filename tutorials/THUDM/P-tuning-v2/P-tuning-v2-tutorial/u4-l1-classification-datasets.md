# 分类任务数据集：SuperGLUE 与 GLUE

## 1. 本讲目标

本讲聚焦 P-tuning v2 仓库中「分类任务」一侧的数据管道。读完本讲，你应该能够：

- 读懂 `tasks/superglue/dataset.py` 与 `tasks/glue/dataset.py` 两个数据集类，说清原始样本是如何一步步变成模型能吃的 `input_ids / attention_mask` 的；
- 理解 `task_to_keys` 这张「模板表」如何用一份代码处理十几种结构不同的数据集；
- 掌握 COPA（多选）、ReCoRD（实体占位）等特殊数据集为什么要绕开主流程、走单独分支；
- 看清 `pad_to_max_length` 与 `fp16` 开关如何决定 `data_collator` 的选择，以及 `compute_metrics` 与 `test_key` 如何把模型预测转成可比较的分数。

本讲**只讲数据**，不涉及模型前向（那是 u2-l2）与训练循环（那是 u5-l1）。

## 2. 前置知识

在进入源码前，先用大白话建立四个基础概念。

1. **GLUE 与 SuperGLUE 是什么。** 它们是自然语言理解的「标准考试套题」。GLUE（如 SST-2、RTE、MNLI）偏简单，SuperGLUE（如 BoolQ、COPA、ReCoRD、WiC、WSC、MultiRC）更难。每个数据集又是一个独立的子任务，字段结构各不相同（有的单句、有的双句、有的带选项）。本仓库把这两套分别放进 `tasks/glue/` 与 `tasks/superglue/`。

2. **HuggingFace `datasets` 库的 `load_dataset`。** 一句 `load_dataset("super_glue", "rte")` 就会联网（或读缓存）下载对应数据集，返回一个类似字典的对象，键是 `"train" / "validation" / "test"`，值是表格型数据集。本讲的两份数据集类最开头就在用它。

3. **Tokenizer（分词器）做了什么。** 给分词器一段文字，它会输出三个关键张量：`input_ids`（每个子词在词表里的编号）、`attention_mask`（1 表示真实 token、0 表示填充位 padding，告诉模型「别看这些」）、`token_type_ids`（句子对任务里用 0/1 区分第一句和第二句，BERT 有，RoBERTa 通常没有）。

4. **承接前两讲。** u1-l3 已经讲过：`run.py` 按 `task_name`（`glue` / `superglue` / ...）分派到对应任务包的 `get_trainer`，而 `get_trainer` 内部会 `new` 一个数据集类。u3-l1 讲过：命令行参数被解析成 `data_args`，里面的 `dataset_name`、`max_seq_length`、`pad_to_max_length`、`template_id` 等字段，正是本讲数据集类直接读取的输入。本讲回答的问题是：**`data_args` 进去之后，`train_dataset / eval_dataset` 是怎么出来的？**

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tasks/superglue/dataset.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/superglue/dataset.py) | `SuperGlueDataset` 类：加载 SuperGLUE、定义 `task_to_keys`、分词预处理、指标计算。包含 COPA 多选与 ReCoRD 实体占位的特殊分支。 |
| [tasks/glue/dataset.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/glue/dataset.py) | `GlueDataset` 类：结构与 SuperGLUE 版本几乎一样，但更简单——没有多选、没有实体展开。回归任务 STSB 有单独处理。 |
| [tasks/superglue/get_trainer.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/superglue/get_trainer.py) | 把数据集类与模型、Trainer 串起来：实例化 `SuperGlueDataset`、根据 `multiple_choice` 选 `TaskType`、把 `test_key` 交给 `BaseTrainer`。 |
| [tasks/utils.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/utils.py) | 从两份 `task_to_keys` 抽取数据集名，拼成全局 `DATASETS` 注册表，回过头驱动 u3-l1 的命令行 `choices` 校验。 |

> **关于「同一个名字出现两次」的提醒。** `rte` 同时存在于 GLUE 与 SuperGLUE 中（字段名分别是 `sentence1/sentence2` 和 `premise/hypothesis`）。仓库里的 `run_script/run_rte_roberta.sh` 用的是 **SuperGLUE 版本**（`task_name=superglue`）。区分走哪份代码的是 `task_name`，不是 `dataset_name` 本身。这一点在本讲实践里会再次用到。

---

## 4. 核心概念与源码讲解

### 4.1 task_to_keys 模板映射：用一张表统一十几种数据集

#### 4.1.1 概念说明

不同分类数据集「长得不一样」：SST-2 是单句情感，RTE 是「前提—假设」句对，QNLI 是「问题—句子」句对，COPA 是「前提 + 两个选项」的多选……如果给每种结构写一份分词函数，代码会爆炸。

本仓库的解法是一张**模板表 `task_to_keys`**：把每个数据集名映射到一个二元组 `(sentence1_key, sentence2_key)`，表示「这个数据集的前半段文字在原始样本里叫什么字段、后半段叫什么字段」。

- 单句任务：第二元是 `None`，如 SST-2 的 `"sentence"` → `("sentence", None)`。
- 句对任务：两元都有值，如 RTE 的 `("premise", "hypothesis")`。
- 多选 / 实体占位任务：两元都是 `None`（`copa`、`record`），表示「走特殊分支，不进通用流程」。

有了这张表，主体分词逻辑只写一遍：查表拿到字段名 → 用这两个字段名去原始样本里取文字 → 喂给 tokenizer。这就是「数据驱动」的设计。

#### 4.1.2 核心流程

```text
dataset_name  ──查表──▶  (sentence1_key, sentence2_key)
                                  │
              ┌───────────────────┴───────────────────┐
              ▼                                       ▼
    sentence2_key is None?                    sentence2_key 有值?
    （单句）                                  （句对）
              │                                       │
   args = (examples[s1],)                args = (examples[s1], examples[s2])
              └───────────────┬───────────────────────┘
                              ▼
                 tokenizer(*args, padding=..., truncation=...)
                              ▼
                   {input_ids, attention_mask, ...}
```

#### 4.1.3 源码精读

SuperGLUE 的模板表（注意 COPA / ReCoRD 两元都是 `None`）：

```python
task_to_keys = {
    "boolq": ("question", "passage"),
    "cb": ("premise", "hypothesis"),
    "rte": ("premise", "hypothesis"),
    "wic": ("processed_sentence1", None),
    "wsc": ("span2_word_text", "span1_text"),
    "copa": (None, None),
    "record": (None, None),
    "multirc": ("paragraph", "question_answer")
}
```
> 这段定义了 SuperGLUE 全部 8 个数据集的字段映射：[tasks/superglue/dataset.py#L12-L21](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/superglue/dataset.py#L12-L21)。其中 `wic`、`wsc`、`multirc` 用的不是原始字段名，而是后面 `preprocess_function` 里**现造出来**的字段（如 `processed_sentence1`、`question_answer`），先造后用。

GLUE 的模板表结构完全相同，只是数据集更「规矩」、字段名都是原始名：

```python
task_to_keys = {
    "cola": ("sentence", None),
    "mnli": ("premise", "hypothesis"),
    "rte": ("sentence1", "sentence2"),
    "stsb": ("sentence1", "sentence2"),
    # ... 共 9 个
}
```
> [tasks/glue/dataset.py#L15-L25](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/glue/dataset.py#L15-L25)。注意这里的 `rte` 字段名是 `sentence1/sentence2`，与 SuperGLUE 版不同。

这张表还有第二个用途——它**反向驱动了全局注册表**：

```python
from tasks.glue.dataset import task_to_keys as glue_tasks
from tasks.superglue.dataset import task_to_keys as superglue_tasks

GLUE_DATASETS = list(glue_tasks.keys())
SUPERGLUE_DATASETS = list(superglue_tasks.keys())
...
DATASETS = GLUE_DATASETS + SUPERGLUE_DATASETS + NER_DATASETS + ...
```
> [tasks/utils.py#L1-L13](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/utils.py#L1-L13)。`DATASETS` 最终成为 u3-l1 里 `--dataset_name` 的 `choices` 校验来源。也就是说：**想新增一个数据集，先在 `task_to_keys` 加一行，命令行就自动支持它了**——这是单一数据源（single source of truth）的好处。

#### 4.1.4 代码实践

打开本仓库的两个 `dataset.py`，对照阅读两张 `task_to_keys`，然后回答：

1. 哪些数据集是「单句」任务（第二元为 `None`）？
2. GLUE 里有没有「两元都为 `None`」的特例？（提示：没有，GLUE 全部走通用流程。）

**操作步骤**：用 Grep 在两份文件里搜 `task_to_keys`，把两张表并排对比。

**预期结果**：你会看到 SuperGLUE 有 2 个特例（copa、record），GLUE 有 0 个特例。这说明 SuperGLUE 才是「数据复杂度」的来源。

#### 4.1.5 小练习与答案

- **练习 1**：`task_to_keys` 里 `("processed_sentence1", None)` 用了一个原始样本里不存在的字段名。这是 bug 吗？
  > **答**：不是。`preprocess_function` 会先在 `examples` 里**动态创建** `examples["processed_sentence1"]`（WiC 的模板化文本），造好之后才查表取值。这是一种「先造字段、再用模板表索引」的写法。
- **练习 2**：为什么 `copa` 与 `record` 要设成 `(None, None)`？
  > **答**：因为它们根本不是「单句/句对」结构。COPA 是「前提 + 两选项」的多选，ReCoRD 是「段落 + 含 `@placeholder` 的问句 + 多个候选实体」。它们没有统一的两个字段，所以直接用 `None` 标记「别进通用分支，我有自己的处理函数」。

---

### 4.2 preprocess_function 分词：通用流程与特殊分支

#### 4.2.1 概念说明

`preprocess_function` 是数据管道的「心脏」：它接收**一个 batch 的原始样本**，返回**一个 batch 的分词结果**。它被 `datasets.map(batched=True)` 调用，因此设计成「对一批样本一次性处理」。

它分两种情况：

- **通用流程**（绝大多数数据集）：按 `task_to_keys` 查到的字段名，从 `examples` 取文字，调用 `tokenizer` 一次性分词。
- **特殊分支**：WSC / WiC / MultiRC 需要先用模板把原始字段拼成新文本；COPA 是多选，要把两个选项分别与前提配对；ReCoRD 更特殊，干脆用一个完全独立的 `record_preprocess_function`。

理解的关键是：**这些「特殊」都是为了把异构数据硬塞进「一个分类模型能吃」的形状**——归根到底，分类模型只认 `input_ids`（COPA 例外，多选模型认 `[选项数, 序列]` 的嵌套结构）。

#### 4.2.2 核心流程

通用流程（以 RTE 句对为例）：

```text
进来的 examples（batched）:
{
  "premise":     ["前提A", "前提B", ...],
  "hypothesis":  ["假设A", "假设B", ...],
  "label":       [0, 1, ...]
}
        │  sentence1_key="premise", sentence2_key="hypothesis"
        ▼
args = (examples["premise"], examples["hypothesis"])
        ▼
tokenizer(*args, padding=<策略>, max_length=128, truncation=True)
        ▼
出来的 result:
{
  "input_ids":        [[CLS] 前提A [SEP] 假设A [SEP],  [CLS] 前提B [SEP] 假设B [SEP], ...],
  "attention_mask":   [[1,...,1], [1,...,1], ...],
  "token_type_ids":   [[0,...,0,1,...,1], ...]   # 仅 BERT 有
}
```

COPA 多选分支：

```text
对每个样本：premise + "because/so"  ──▶  text_a
分两次分词：
   result1 = tokenizer(text_a, choice1)   # 选项1 句对
   result2 = tokenizer(text_a, choice2)   # 选项2 句对
再把同一样本的两个结果「打包」：
   result["input_ids"][i] = [result1["input_ids"][i], result2["input_ids"][i]]
        ▼
最终每条样本的 input_ids 是 [序列1, 序列2]（多选模型的输入形状）
```

#### 4.2.3 源码精读

通用流程的核心就是这两行——先按是否单句构造 `args`，再调一次 tokenizer：

```python
args = (
    (examples[self.sentence1_key],) if self.sentence2_key is None
    else (examples[self.sentence1_key], examples[self.sentence2_key])
)
result = self.tokenizer(*args, padding=self.padding, max_length=self.max_seq_length, truncation=True)
return result
```
> [tasks/superglue/dataset.py#L156-L161](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/superglue/dataset.py#L156-L161)。这就是「一份代码处理所有普通数据集」的全部秘密。`*args` 让单句（一个元素）和句对（两个元素）复用同一条调用。GLUE 版本的 `preprocess_function` 与此完全一致：[tasks/glue/dataset.py#L96-L103](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/glue/dataset.py#L96-L103)。

COPA 为什么特殊——它要造「前提 + 因为/所以」的连接词，再把两个选项各分一次词、最后嵌套：

```python
if self.data_args.dataset_name == "copa":
    examples["text_a"] = []
    for premise, question in zip(examples["premise"], examples["question"]):
        joiner = "because" if question == "cause" else "so"
        text_a = f"{premise} {joiner}"
        examples["text_a"].append(text_a)

    result1 = self.tokenizer(examples["text_a"], examples["choice1"], ...)
    result2 = self.tokenizer(examples["text_a"], examples["choice2"], ...)
    result = {}
    for key in ["input_ids", "attention_mask", "token_type_ids"]:
        if key in result1 and key in result2:
            result[key] = []
            for value1, value2 in zip(result1[key], result2[key]):
                result[key].append([value1, value2])
    return result
```
> [tasks/superglue/dataset.py#L139-L154](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/superglue/dataset.py#L139-L154)。注意 `if key in result1 and key in result2`：它**防御性地跳过不存在的字段**（例如 RoBERTa 没有 `token_type_ids`），保证 BERT/RoBERTa 都能跑。这是 COPA 走特殊分支的根本原因：它的输出形状是 `[batch, 2, seq_len]`（每条样本两个选项），与通用流程的 `[batch, seq_len]` 不兼容。

ReCoRD 更彻底——它根本不进 `preprocess_function`，在 `__init__` 里被单独 `map` 到 `record_preprocess_function`：

```python
if data_args.dataset_name == "record":
    raw_datasets = raw_datasets.map(self.record_preprocess_function, batched=True, ...)
else:
    raw_datasets = raw_datasets.map(self.preprocess_function, batched=True, ...)
```
> [tasks/superglue/dataset.py#L67-L81](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/superglue/dataset.py#L67-L81)。ReCoRD 会把每个段落里的**每个候选实体**都展开成一条样本（把问句里的 `@placeholder` 替换成实体，做二分类「这个实体是不是答案」），所以样本数量会膨胀，且必须额外保留 `question_id / entity / answers` 等字段供指标计算用：[tasks/superglue/dataset.py#L209-L239](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/superglue/dataset.py#L209-L239)。

`padding` 策略在 `__init__` 里就定好了，它直接决定 `preprocess_function` 传给 tokenizer 的 `padding` 值：

```python
if data_args.pad_to_max_length:
    self.padding = "max_length"
else:
    self.padding = False   # 不在这里 pad，等组 batch 时再动态 pad
```
> [tasks/superglue/dataset.py#L47-L52](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/superglue/dataset.py#L47-L52)。

#### 4.2.4 代码实践

**目标**：用伪代码模拟 RTE 的 `preprocess_function`，看清输入输出的形状。

**操作步骤**：阅读上文通用流程，手写下面这段输入对应的输出结构（这是**示例代码 / 伪代码**，不需要真的运行）：

```python
# 假设走 SuperGlueDataset，dataset_name="rte"
# task_to_keys["rte"] = ("premise", "hypothesis")，即 sentence1_key / sentence2_key

examples = {
    "premise":    ["No, it is not a hard question.",
                   "A man inspects the uniform of a figure."],
    "hypothesis": ["It is not a hard question.",
                   "The man is sleeping."],
    "label":      [0, 1],
}

# preprocess_function 内部实际执行：
args = (examples["premise"], examples["hypothesis"])
result = tokenizer(*args, padding="max_length", max_length=128, truncation=True)
```

**需要观察的现象 / 预期结果**（以 BERT 分词器为例，待本地验证）：

```python
result = {
    # 每条 = [CLS] 前提 [SEP] 假设 [SEP]，再 pad 到 128
    "input_ids":      [[101, 前提A子词..., 102, 假设A子词..., 102, 0, 0, ...],   # 长度 128
                      [101, 前提B子词..., 102, 假设B子词..., 102, 0, 0, ...]],  # 长度 128
    "attention_mask": [[1, 1, ..., 1, 0, 0, ...],                               # 真 token 为 1，pad 为 0
                      [1, 1, ..., 1, 0, 0, ...]],
    "token_type_ids": [[0, 0, ..., 0, 1, 1, ..., 1, 0, 0, ...],                 # 第一句段=0，第二句段=1，pad=0
                      [0, 0, ..., 0, 1, 1, ..., 1, 0, 0, ...]],
}
# 形状：每个值是一个 list[list[int]]，外层 2 = 样本数，内层 128 = max_seq_length
```

> **关于 RoBERTa**：若用 RoBERTa 分词器（如 `roberta-large`，正是 `run_rte_roberta.sh` 用的），`result` 里**通常没有 `token_type_ids`**。这正是 COPA 分支里要写 `if key in result1 and key in result2` 防御的原因。

#### 4.2.5 小练习与答案

- **练习 1**：COPA 的 `preprocess_function` 最后把结果嵌套成 `[value1, value2]`。如果不这样做、直接返回两个独立的 `result1 / result2`，会出什么问题？
  > **答**：下游模型（多选模型）需要「每条样本携带两个选项」作为一个整体送进 forward（形状 `[batch, num_choices, seq]`）。不嵌套的话，两条选项会被当成独立的 batch 样本，样本数翻倍且与标签错位，训练直接乱套。
- **练习 2**：WSC 数据集在 `preprocess_function` 里会先把 `examples["span2_word_text"]` 造出来，再走通用流程。如果删掉这段「造字段」的代码，会发生什么？
  > **答**：通用流程会执行 `examples["span2_word_text"]`，而原始样本里没有这个键，会抛 `KeyError`。模板表里用「人造字段名」必须配套有「造字段」的代码，二者缺一不可。

---

### 4.3 compute_metrics 与 test_key：把预测变成分数

#### 4.3.1 概念说明

分词解决了「输入怎么来」，而 `compute_metrics` 解决「输出怎么评」。训练时模型吐出 logits（每类一个未归一化分数），评估时要把 logits 转成一个**单一、可比较的指标**（如 accuracy、f1）。`compute_metrics` 就是这个转换函数，它被 HF Trainer 在每次评估时回调。

但光有一个指标还不够——u5-l1 会讲，`BaseTrainer` 需要知道**用哪个指标来判定「这次是不是历史最好」**，这个指标名就是 `test_key`。本讲只需记住：`test_key` 是数据集类暴露给训练器的一个字符串（如 `"accuracy"` 或 `"f1"`），告诉训练器「盯紧这个键」。

此外，数据集类还在 `__init__` 里挑好了 `data_collator`（组 batch 时的拼装器），这是另一项要交给训练器的输出。`pad_to_max_length` 与 `fp16` 两个开关共同决定它。

#### 4.3.2 核心流程

```text
模型 logits  ─argmax─▶  预测类别 preds
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
        普通(record以外)    multirc        record
        metric.compute     sklearn.f1     reocrd_compute_metrics
        (accuracy/...)     (f1)           (按 question_id 聚合 f1/em)
              │
              ▼
        {"accuracy": 0.83, ...}   ◀── eval 指标字典

test_key = "accuracy"(默认) 或 "f1"(record/multirc)
        └── 交给 BaseTrainer，决定「最好」怎么判
```

`data_collator` 选择：

```text
pad_to_max_length == True?  ──是──▶  default_data_collator
                │否
                ▼
        fp16 == True?  ──是──▶  DataCollatorWithPadding(pad_to_multiple_of=8)
                │否
                ▼
        （两分支都没命中 ── data_collator 未被赋值，潜在隐患）
```

#### 4.3.3 源码精读

`compute_metrics` 主体：先从可能含多输出的 `predictions` 里取出 logits，`argmax` 成类别，再交给官方 `metric`（GLUE/SuperGLUE 各自的官方评估函数）算分：

```python
def compute_metrics(self, p: EvalPrediction):
    preds = p.predictions[0] if isinstance(p.predictions, tuple) else p.predictions
    preds = np.argmax(preds, axis=1)

    if self.data_args.dataset_name == "record":
        return self.reocrd_compute_metrics(p)
    if self.data_args.dataset_name == "multirc":
        from sklearn.metrics import f1_score
        return {"f1": f1_score(preds, p.label_ids)}
    if self.data_args.dataset_name is not None:
        result = self.metric.compute(predictions=preds, references=p.label_ids)
        if len(result) > 1:
            result["combined_score"] = np.mean(list(result.values())).item()
        return result
    ...
```
> [tasks/superglue/dataset.py#L163-L182](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/superglue/dataset.py#L163-L182)。注意三个分支：ReCoRD 与 MultiRC 各自特判（都需要 f1，但 ReCoRD 还要按 `question_id` 聚合多个实体候选，见 [tasks/superglue/dataset.py#L184-L207](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/superglue/dataset.py#L184-L207)）；其余交给官方 `metric`，若官方返回多个指标（如 MRPC 的 accuracy + f1）就额外算一个 `combined_score` 取均值。GLUE 版本几乎一样，只是多了回归任务 STSB 的 `np.squeeze` 处理：[tasks/glue/dataset.py#L105-L116](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/glue/dataset.py#L105-L116)。

`test_key` 的赋值——就一行，但决定了训练器盯哪个指标：

```python
self.test_key = "accuracy" if data_args.dataset_name not in ["record", "multirc"] else "f1"
```
> [tasks/superglue/dataset.py#L105](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/superglue/dataset.py#L105)。ReCoRD 与 MultiRC 用 f1，其余用 accuracy。

`test_key` 随后被 `get_trainer` 传给 `BaseTrainer`，并据此决定模型形态（多选走 `TaskType.MULTIPLE_CHOICE`）：

```python
if not dataset.multiple_choice:
    model = get_model(model_args, TaskType.SEQUENCE_CLASSIFICATION, config)
else:
    model = get_model(model_args, TaskType.MULTIPLE_CHOICE, config, fix_bert=True)

trainer = BaseTrainer(
    model=model, ...,
    data_collator=dataset.data_collator,
    test_key=dataset.test_key
)
```
> [tasks/superglue/get_trainer.py#L53-L68](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/superglue/get_trainer.py#L53-L68)。这里能看到数据集类的三项产出（`data_collator`、`test_key`、`compute_metrics`）如何流入训练器。注意 GLUE 版的 `get_trainer` **没有传 `test_key`**，于是 `BaseTrainer` 用默认值 `"accuracy"`：[tasks/glue/get_trainer.py#L48-L56](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/glue/get_trainer.py#L48-L56)。

`data_collator` 选择——`pad_to_max_length` 与 `fp16` 的交叉判断：

```python
if data_args.pad_to_max_length:
    self.data_collator = default_data_collator
elif training_args.fp16:
    self.data_collator = DataCollatorWithPadding(tokenizer, pad_to_multiple_of=8)
```
> [tasks/superglue/dataset.py#L100-L103](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/superglue/dataset.py#L100-L103)。理解它的关键在配合 `padding` 策略：

- `pad_to_max_length=True`（默认）：分词时已 pad 到 `max_seq_length`，所有样本等长，`default_data_collator` 直接 stack 即可。
- `pad_to_max_length=False` 且 `fp16=True`：分词时**不 pad**（样本长度不一），所以需要 `DataCollatorWithPadding` 在组 batch 时动态 pad；`pad_to_multiple_of=8` 是为了让序列长度对齐到 8 的倍数，充分利用 GPU 的 Tensor Core（fp16 训练的常见优化）。

> **一个值得注意的隐患**：这段是 `if/elif`，**没有 `else`**。若用户同时设 `pad_to_max_length=False` 且不开 `fp16`，`self.data_collator` 永远不会被赋值，后续训练器访问它时会报 `AttributeError`。实践中默认配置（`pad_to_max_length=True`）规避了它，但这是一个真实的边界条件，二次开发时务必留意。GLUE 版有完全相同的问题：[tasks/glue/dataset.py#L90-L93](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/glue/dataset.py#L90-L93)。

#### 4.3.4 代码实践

**目标**：搞清一个数据集「最终交给训练器哪几个东西」。

**操作步骤**：在 `SuperGlueDataset.__init__` 里数一数，哪些 `self.xxx` 是给训练器用的「对外产出」？分别在 `get_trainer.py` 的哪一行被取走？

**预期结果**（至少应列出这几项）：

| 产出 | 在 dataset 里的定义 | 在 get_trainer 里被取走 |
| --- | --- | --- |
| `train_dataset / eval_dataset` | 第 83-96 行 | `get_trainer.py` 第 62-63 行 |
| `compute_metrics` | 第 163 行 | `get_trainer.py` 第 64 行 |
| `data_collator` | 第 100-103 行 | `get_trainer.py` 第 66 行 |
| `test_key` | 第 105 行 | `get_trainer.py` 第 67 行 |
| `num_labels / label2id / id2label` | 第 35-58 行 | 用于构造 `config`，第 37-44 行 |

#### 4.3.5 小练习与答案

- **练习 1**：为什么 ReCoRD 的 `compute_metrics` 要单独写 `reocrd_compute_metrics`，而不能像别的任务那样直接调官方 `metric`？
  > **答**：ReCoRD 在预处理时把「一个问题」展开成了「多个候选实体」各一条样本。评估时必须**按 `question_id` 把这些样本聚拢**，挑出该问题下得分最高的实体，再判断它是否命中答案集合，算 f1/em。官方 `metric` 按「逐样本」算，不知道这种聚合逻辑，所以必须自写。
- **练习 2**：若一个 GLUE 数据集（如 CoLA，官方指标是 `matthews_correlation`）走 GLUE 的 `get_trainer`，`BaseTrainer` 的 `test_key` 会是什么？这会带来什么问题？
  > **答**：GLUE 的 `get_trainer` 不传 `test_key`，所以 `BaseTrainer` 用默认 `"accuracy"`。但 CoLA 的 eval 指标字典里没有 `eval_accuracy` 键（只有 `matthews_correlation`）。这意味着 u5-l1 里 `_maybe_log_save_evaluate` 用 `eval_metrics["eval_"+self.test_key]` 取值时会 `KeyError`。这是 GLUE 流程对非 accuracy 指标的一个已知局限（提示：实际复现时需自行传入正确的 `test_key` 或改写）。

---

## 5. 综合实践

**任务**：把本讲三个模块串起来——从「字段名」一路追到「指标键」，画一张 RTE（SuperGLUE）的端到端数据流，并定位 COPA 在哪一步「分叉」。

**操作步骤**：

1. 假设执行 `run_script/run_rte_roberta.sh`（`task_name=superglue`, `dataset_name=rte`）。画出下面这条链路上每一步对应的具体值：
   - `task_to_keys["rte"]` → `(premise, hypothesis)`
   - `preprocess_function` 的 `args` → `(examples["premise"], examples["hypothesis"])`
   - tokenizer 输出 → `input_ids / attention_mask`（RoBERTa，无 `token_type_ids`）
   - `compute_metrics` 走哪个分支 → 官方 `metric`（非 record/multirc）
   - `test_key` → `"accuracy"`
   - `data_collator`（默认 `pad_to_max_length=True`）→ `default_data_collator`
2. 把 `dataset_name` 换成 `copa`，重画链路。重点标出：在哪一行 `multiple_choice` 变成 `True`（[tasks/superglue/dataset.py#L33](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/superglue/dataset.py#L33)），`preprocess_function` 在哪一段提前 `return`（[tasks/superglue/dataset.py#L154](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/superglue/dataset.py#L154)），以及 `get_trainer` 据此改走 `TaskType.MULTIPLE_CHOICE`（[tasks/superglue/get_trainer.py#L55-L56](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/superglue/get_trainer.py#L55-L56)）。
3. **用一句话回答本讲实践题**：COPA 为什么走特殊分支？
   > 因为 COPA 是「前提 + 二选一」的多选任务，每条样本必须同时携带两个选项、输出形状是 `[batch, 2, seq_len]`，与通用流程产出的 `[batch, seq_len]` 不兼容，所以必须在 `preprocess_function` 里单独构造、提前 `return`，并由 `get_trainer` 改用多选模型。

**预期结果**：你应得到两张流程图，一张（RTE）走通用流程到 accuracy，一张（COPA）在 `preprocess_function` 内分叉并接多选模型。这正好印证「同一份 `SuperGlueDataset` 用模板表 + 特殊分支兼容了所有 SuperGLUE 数据集」。

---

## 6. 本讲小结

- `task_to_keys` 是一张「模板表」，用 `(sentence1_key, sentence2_key)` 把异构数据集对齐到同一份分词代码；`None` 表示单句或「走特殊分支」。
- 通用 `preprocess_function` 只有一行核心：按字段名取文字、调一次 `tokenizer`；WSC/WiC/MultiRC 靠「先造字段再分词」复用它。
- COPA 是多选，输出 `[batch, 2, seq]` 嵌套结构，必须在函数内单独构造并提前 `return`；ReCoRD 更彻底，用独立的 `record_preprocess_function` 把每个候选实体展开成样本。
- `compute_metrics` 把 logits 经 `argmax` 后交给官方 `metric`，ReCoRD/MultiRC 特判用 f1（ReCoRD 还要按 `question_id` 聚合）。
- `test_key`（accuracy 或 f1）和 `data_collator`（由 `pad_to_max_length` 与 `fp16` 决定）是数据集类交给训练器的两项关键产出；`if/elif` 无 `else` 是一个边界隐患。
- `task_to_keys` 同时反向驱动 `tasks/utils.py` 的 `DATASETS` 注册表，实现「改一处即全局生效」。

## 7. 下一步学习建议

- 本讲只讲了**分类**数据的对齐。序列标注（NER）在子词切分下还要做「标签对齐」（用 `word_ids` 与 `-100`），这正是下一讲 **u4-l2 序列标注数据对齐：NER** 的主题，建议对照阅读 `tasks/ner/dataset.py`。
- 数据准备好后，模型如何「吃」它并注入前缀？回到 **u2-l2 前缀注入主流程** 看分类模型（`BertPrefixForSequenceClassification`）如何接收 `input_ids` 与 `attention_mask`。
- `test_key` 到底怎么驱动「最佳指标追踪」？这是 **u5-l1 BaseTrainer 与最佳指标追踪** 的核心，建议在读完本讲的 `compute_metrics / test_key` 后立即接上。

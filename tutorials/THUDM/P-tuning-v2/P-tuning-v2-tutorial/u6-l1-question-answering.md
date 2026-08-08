# 抽取式问答模型与 SQuAD

## 1. 本讲目标

本讲把 P-tuning v2 的前缀注入机制迁移到一个全新的任务形态——**抽取式问答（extractive question answering）**。前面几讲我们处理的都是「每个样本给一个标签」的任务（分类给一个类别，NER 给一段标签序列），而问答任务的输出是「一段连续文本片段」。

学完本讲，你应该能够：

1. 说清抽取式问答为什么用 **start/end logits** 而不是分类概率，以及它如何复用 `get_prompt` 的逐层前缀注入。
2. 走通 `QuestionAnsweringTrainer` 的 `evaluate` 流程：先跑出原始 logits，再经 `post_process_function` 把 logits「翻译」成答案文本，最后算 F1/EM。
3. 理解 QA 为什么**不**沿用 `BaseTrainer` 的 `test_key` 单指标机制，而要维护 `best_eval_f1` + `best_eval_exact_match` 双指标的专用 `best_metrics`。

本讲承接 u2-l2（前缀注入主流程）、u3-l2（模型工厂 `get_model` 与 `TaskType`）、u5-l1（`BaseTrainer` 与最佳指标追踪），是对这些机制的「换一种任务再讲一遍」。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**抽取式问答 vs 分类。** 给定一个问题和一个上下文（paragraph），模型要在上下文里**圈出**答案片段。SQuAD 数据集的标注就是答案在上下文中的**起止字符位置**。模型并不生成新文本（那是生成式问答），而是从上下文里「抽取」。

**start/end logits 的思路。** 既然答案是一段连续 token，最经济的表示就是「起点 token 的下标」和「终点 token 的下标」。于是模型对**序列里的每个 token** 输出两个分数：它作为起点有多可能、它作为终点有多可能。这就是 `start_logits` 和 `end_logits`，形状都是 `(batch, seq_len)`。训练时，把真实起点/终点下标当作「分类目标」，对 `seq_len` 这一维做交叉熵。

\[
\mathcal{L} = \frac{1}{2}\big(\,\mathrm{CE}(\text{start\_logits},\, \text{start\_position}) + \mathrm{CE}(\text{end\_logits},\, \text{end\_position})\,\big)
\]

**为什么 QA 需要专用 Trainer。** 评估分类只要 `argmax` + 准确率；评估 QA 要先把 `start/end logits` 经过后处理还原成一段**文本**，再去和标准答案比对（F1 是按词级重叠算的，EM 是逐字符相等才算）。这套「logits → 文本答案」的后处理与「文本答案 → F1/EM」的打分，是 `BaseTrainer` 没有的，因此 QA 必须重写 `evaluate`，并相应地重写 `_maybe_log_save_evaluate` 来追踪双指标。

**术语速查：**

| 术语 | 含义 |
|------|------|
| start/end logits | 序列里每个 token 作为答案起点/终点的分数 |
| offset_mapping | 每个 token 对应原文的字符区间，用于把 token 下标还原回文本 |
| overflowing_tokens | 长上下文被切成多段（窗口滑动），一段叫一个 feature |
| F1 / EM | 词级重叠分数 / 完全匹配（exact match），SQuAD 的两个官方指标 |
| post-processing | 把 logits 翻译成答案文本的步骤 |

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [model/question_answering.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/question_answering.py) | QA 模型：非前缀版与三种前缀版（Bert/RoBERTa/DeBERTa），输出 start/end logits |
| [tasks/qa/get_trainer.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/qa/get_trainer.py) | QA 任务的装配入口：建 config（`num_labels=2`）、模型、数据集、`QuestionAnsweringTrainer` |
| [tasks/qa/dataset.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/qa/dataset.py) | `SQuAD` 数据集类：分词、构造 `start/end_positions`、后处理与 `compute_metrics` |
| [training/trainer_qa.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_qa.py) | `QuestionAnsweringTrainer`：重写 `evaluate`/`predict`/`_maybe_log_save_evaluate`，追踪 best f1/em |
| [tasks/qa/utils_qa.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/qa/utils_qa.py) | `postprocess_qa_predictions`：从 logits 选出 n-best 答案文本 |
| [run_script/run_squad_roberta.sh](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/run_script/run_squad_roberta.sh) | SQuAD + RoBERTa 的运行配方，本讲实践会用到 |

路由链条：`run.py` 按 `task_name == "qa"` 派发到 [tasks/qa/get_trainer.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/run.py#L112-L114)，注册表 `QA_DATASETS = ["squad", "squad_v2"]`（见 [tasks/utils.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/utils.py#L8)）。

---

## 4. 核心概念与源码讲解

### 4.1 start/end logits：前缀版问答模型的前向输出

#### 4.1.1 概念说明

抽取式问答把「找答案」转化为「找两个下标」。模型主体仍是冻结的 BERT/RoBERTa，只是在它上面接一个 `qa_outputs = Linear(hidden, 2)`：对每个 token 输出 2 个数，分别当起点分和终点分。

这里有个**容易踩坑的点**：QA 里的 `num_labels` 并不是「答案有几个类别」，而是恒为 **2（start + end）**。无论 SQuAD 还是 SQuAD v2，这个值都不变。这与分类任务 `num_labels = 类别数` 截然不同——后面 4.3 节会专门对比。

前缀注入部分和 u2-l2 讲的分类模型**完全一致**：`get_prompt` 生成每层 key/value，拼到冻结主干前面。也就是说，P-tuning v2 的「深度提示」机制是任务无关的，换个「头」就能换任务。

#### 4.1.2 核心流程

前向计算可拆成三段：

```
input_ids ──► get_prompt() 生成 past_key_values（每层 key/value）
          ──► 拼接 prefix_attention_mask
          ──► 冻结的 bert(input_ids, attention_mask, past_key_values)
                └─ sequence_output: (batch, seq_len, hidden)
          ──► qa_outputs(sequence_output): (batch, seq_len, 2)
          ──► split(1, dim=-1) → start_logits, end_logits: (batch, seq_len) 各一
          ──► 若有 start/end_positions：
                  loss = (CE(start_logits, start_pos) + CE(end_logits, end_pos)) / 2
```

注意：前缀是拼在序列**最前面**的，所以 `sequence_output` 的长度 = `pre_seq_len + 原序列长度`。但 `start/end_positions` 来自数据预处理，它们的下标是针对「真实 token 序列」算的，与前缀对齐——这点 4.2 节再细说。

#### 4.1.3 源码精读

以 `BertPrefixForQuestionAnswering` 为例。先看构造函数如何建组件并冻结主干：

[model/question_answering.py:101-120](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/question_answering.py#L101-L120) —— 构造函数：建 `bert`（不带 pooler）、`qa_outputs = Linear(hidden, num_labels)`、`PrefixEncoder`，并用 `for param in self.bert.parameters(): param.requires_grad = False` 冻结主干。

> 与分类模型一个细微差别：QA 用 `add_pooling_layer=False`，因为问答只需要逐 token 的 `sequence_output`，不需要 `[CLS]` 的 pooled 向量。

`get_prompt` 与 u2-l2 讲的分类版本逐字相同——这就是「机制复用」的体现：

[model/question_answering.py:122-135](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/question_answering.py#L122-L135) —— `get_prompt`：`view → dropout → permute([2,0,3,1,4]).split(2)`，把扁平的 `(batch, pre_seq_len, 2*L*H)` 重排成「每层一对 (key, value)」的逐层 `past_key_values` 列表。

forward 的前缀注入三连：

[model/question_answering.py:163-179](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/question_answering.py#L163-L179) —— 取 `batch_size` → `get_prompt` → 在 `attention_mask` 前拼 `pre_seq_len` 个 1 → 把 `past_key_values` 传入**冻结的** `self.bert`。

接下来是把序列表示变成 start/end logits 的关键四行：

[model/question_answering.py:183-186](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/question_answering.py#L183-L186) —— `logits = qa_outputs(sequence_output)` 得到 `(batch, seq_len, 2)`，再用 `split(1, dim=-1)` 拆成 `start_logits`、`end_logits` 各 `(batch, seq_len)`。

最后是损失计算：

[model/question_answering.py:188-203](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/question_answering.py#L188-L203) —— `ignored_index = start_logits.size(1)`（即序列长度），把 `start_positions/end_positions` 钳制到合法范围，用 `CrossEntropyLoss(ignore_index=ignored_index)` 分别算两段损失再取平均。

> 这里 `ignored_index` 是一个安全网：如果标注的起止位置超出了当前 feature 的序列长度（长上下文被截断时会发生），就把它钳到一个不存在的位置（等于序列长度），交叉熵会自动忽略。这和 NER 里的 `-100` 是同一个思想——用 `ignore_index` 屏蔽不参与训练的位置。

RoBERTa 版（[model/question_answering.py:217-331](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/question_answering.py#L217-L331)）与 DeBERTa 版（[333-455](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/question_answering.py#L333-L455)）逻辑完全平行，只是把 `self.bert` 换成 `self.roberta` / `self.deberta`。DeBERTa 版多了一段参数量打印（[第 354-361 行](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/question_answering.py#L354-L361)），注释里写着 `# 9860105`——可见开启 `prefix_projection`（MLP 头）时前缀参数约 985 万，远小于冻结的 DeBERTa 主干。

模型工厂的注册（u3-l2 讲过二维表 `PREFIX_MODELS`）：QA 对应 `TaskType.QUESTION_ANSWERING`，三类主干都已注册：

[model/utils.py:46-71](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L46-L71) —— `PREFIX_MODELS` 中 bert/roberta/deberta 的 `QUESTION_ANSWERING` 项分别指向三个前缀版 QA 类；`deberta-v2` 为 `None`（即 deberta-v2 不支持 QA 前缀模式）。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `qa_outputs` 把 `(batch, seq_len, hidden)` 变成 `(batch, seq_len, 2)`，再拆成两个 `(batch, seq_len)`，理解 start/end logits 的形状来源。

**操作步骤**（示例代码，非项目原有代码，请自行新建一个临时脚本运行，**不要**修改项目源码）：

```python
# 示例代码：用随机张量模拟 qa_outputs 的形状变化
import torch

batch, seq_len, hidden, num_labels = 2, 10, 768, 2
qa_outputs = torch.nn.Linear(hidden, num_labels)

sequence_output = torch.randn(batch, seq_len, hidden)  # 模拟 BERT 输出
logits = qa_outputs(sequence_output)                    # (2, 10, 2)
start_logits, end_logits = logits.split(1, dim=-1)      # 各 (2, 10, 1)
start_logits = start_logits.squeeze(-1)                 # (2, 10)
end_logits = end_logits.squeeze(-1)
print(start_logits.shape, end_logits.shape)             # torch.Size([2, 10]) torch.Size([2, 10])
```

**需要观察的现象**：`logits` 的最后一维是 2，对应「start + end」两个分数；`split(1, dim=-1)` 沿最后一维切开，每片是 1 维，再 `squeeze` 成 `(batch, seq_len)`。

**预期结果**：打印出 `torch.Size([2, 10]) torch.Size([2, 10])`。这正说明：QA 模型对序列里**每个 token** 都给了一个起点分和一个终点分，而非对整个序列给一个分类概率。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `qa_outputs` 改成 `Linear(hidden, 1)`，会发生什么？
**参考答案**：`split(1, dim=-1)` 会失败（最后一维只有 1，无法拆成两片），无法同时得到 start 和 end logits。`num_labels` 必须是 2。

**练习 2**：前缀版 QA 模型的可训练参数包含哪些？BERT 主干权重算不算？
**参考答案**：可训练的是 `qa_outputs`、`prefix_encoder`（以及 `dropout`，但无参数）、`PrefixEncoder` 内的 embedding/MLP；BERT 主干被 `requires_grad=False` 冻结，不参与训练。

---

### 4.2 QuestionAnsweringTrainer 的后处理流程

#### 4.2.1 概念说明

`QuestionAnsweringTrainer` 是 P-tuning v2 为 QA 专门写的训练器。它的核心职责不是改训练循环，而是**改评估循环**：因为评估 QA 不能直接拿 logits 算指标，必须先「后处理」。

后处理的输入是 `(start_logits, end_logits)`，输出是一段答案文本。难点有两个：

1. **长上下文切窗**：SQuAD 上下文常超过 384 token，一个样本会被切成多个 feature（滑动窗口，`stride=128`），每个 feature 都有自己的 logits。
2. **logits → 文本**：要在每个 feature 里找「起点+终点」使答案片段最可信，再把 token 下标用 `offset_mapping` 映射回原文字符区间，截取文本。

这整个过程由 `SQuAD.post_processing_function` 包装 `postprocess_qa_predictions` 完成。`QuestionAnsweringTrainer` 只负责在正确的时机调用它。

#### 4.2.2 核心流程

`QuestionAnsweringTrainer.evaluate` 的流程（注意它**重写**了 HF Trainer 的 `evaluate`）：

```
evaluate():
  1. 暂时关掉 self.compute_metrics（设为 None）
  2. 用 evaluation_loop 在验证集上跑一遍，得到原始 output.predictions = (start_logits, end_logits)
  3. 恢复 self.compute_metrics
  4. eval_preds = post_process_function(eval_examples, eval_dataset, predictions)
        └─ logits → 答案文本（postprocess_qa_predictions）
        └─ 组装成 EvalPrediction(predictions=文本答案列表, label_ids=references)
  5. metrics = compute_metrics(eval_preds)   # 调用 SQuAD metric，算 f1 / exact_match
  6. 给 metrics 的 key 加 "eval_" 前缀，log 出去
```

关键设计：第 1、3 步「临时关闭 `compute_metrics`」是为了让 `evaluation_loop` **只**跑前向、收集原始 logits，**不**在循环内部急着算指标——因为此时还无法算。真正的指标计算被推迟到第 5 步、在后处理之后。

#### 4.2.3 源码精读

先看类的继承关系和构造：

[training/trainer_qa.py:29-38](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_qa.py#L29-L38) —— `QuestionAnsweringTrainer(ExponentialTrainer)`，构造时多收 `eval_examples` 和 `post_process_function`，并初始化带 **两个** eval 指标的 `best_metrics`（`best_eval_f1`、`best_eval_exact_match`）。

> **继承链**：`Trainer → BaseTrainer → ExponentialTrainer → QuestionAnsweringTrainer`（见 [trainer_exp.py:38-45](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_exp.py#L38-L45)）。所以 QA 任务**也**用上了 `ExponentialLR(gamma=0.95)` 的指数学习率衰减（继承自 `ExponentialTrainer.create_scheduler`），以及 `ExponentialTrainer` 那一整套 `train()` 循环。这一点常被忽略——QA 不是用线性调度，而是指数调度。

`evaluate` 的重写：

[training/trainer_qa.py:40-79](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_qa.py#L40-L79) —— 关键三段：临时关 `compute_metrics`（[第 46-47 行](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_qa.py#L46-L47)）→ `evaluation_loop` 收 logits（[第 50-57 行](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_qa.py#L50-L57)）→ `post_process_function` + `compute_metrics`（[第 61-63 行](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_qa.py#L61-L63)）。

后处理函数本身在数据集类里定义，它把 logits 交给 `postprocess_qa_predictions`：

[tasks/qa/dataset.py:161-182](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/qa/dataset.py#L161-L182) —— `post_processing_function`：调用 `postprocess_qa_predictions` 得到 `{id: 答案文本}` 字典，再按是否为 `squad_v2` 决定要不要带 `no_answer_probability`，最后组装成 `EvalPrediction(predictions=..., label_ids=references)`。

`tasks/qa/utils_qa.py` 里的 `postprocess_qa_predictions`（[第 31-42 行](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/qa/utils_qa.py#L31-L42) 是它的签名）做的是：对每个 feature，从 `start/end logits` 里各取 `n_best_size` 个最高分的下标，组合成候选 `(start, end)` 对，过滤掉超长、反向、落在非上下文区域的候选，挑出最佳答案，再用 `offset_mapping` 把 token 区间还原成原文子串。这部分源自 HuggingFace 官方 `utils_qa.py`，本仓库未做改动。

`compute_metrics` 则很薄：

[tasks/qa/dataset.py:158-159](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/qa/dataset.py#L158-L159) —— 直接 `self.metric.compute(predictions=p.predictions, references=p.label_ids)`，其中 `self.metric = load_metric(data_args.dataset_name)`（[第 62 行](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/qa/dataset.py#L62)），返回的就是 `{'exact_match': ..., 'f1': ...}`。

数据预处理里两个值得记住的细节：

- [tasks/qa/dataset.py:30](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/qa/dataset.py#L30) —— `self.max_seq_len = 384` 是**硬编码**的，不读 `data_args.max_seq_length`（注释里还留着旧代码）。
- [tasks/qa/dataset.py:64-121](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/qa/dataset.py#L64-L121) —— `prepare_train_dataset`：用 `truncation='only_second'`（只截 context 不截 question）、`stride=128` 滑窗，把每个 feature 的答案起止 token 下标算出来写进 `start_positions/end_positions`；若该样本无答案（`answer_start` 为空）或答案落在截断区间外，起止都置为 `cls_index`（用 `[CLS]` 当「无答案」标记）。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：搞清楚「原始 logits」和「最终 metrics」之间到底插了哪一步，理解为什么 `evaluate` 必须临时关闭 `compute_metrics`。

**操作步骤**：

1. 打开 [training/trainer_qa.py:40-79](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_qa.py#L40-L79)，找到 `compute_metrics = self.compute_metrics; self.compute_metrics = None` 这两行（第 46-47 行）。
2. 对比 HF 原生 `Trainer.evaluation_loop`：它在循环末尾会调用 `self.compute_metrics`（如果非 None）。思考：如果不临时关闭，会发生什么？
3. 追踪 `output.predictions` 的去向：它被传给 `post_process_function`（第 62 行），再传给 `compute_metrics`（第 63 行）。

**需要观察的现象 / 预期结果**：

- 如果不关闭 `compute_metrics`，`evaluation_loop` 会把 `predictions`（还是 numpy 的 logits 元组）直接喂给 `compute_metrics`，而 `SQuAD.compute_metrics` 期望的是**已经后处理的文本答案列表**，于是会在 `metric.compute` 里报错或得到无意义的分数。
- 因此「先关、跑前向、再开后处理、最后算指标」是这个流程的**必要顺序**。

> 待本地验证：若有 GPU 环境，可参考 [run_squad_roberta.sh](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/run_script/run_squad_roberta.sh) 跑 1 个 epoch，在日志里确认每轮末尾打印的是 `eval_f1` 和 `eval_exact_match`（而非 `eval_accuracy`）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `evaluate` 里要把 `self.compute_metrics` 临时设成 `None`，而不是直接删掉调用？
**参考答案**：`evaluation_loop` 内部会自动检查 `self.compute_metrics` 是否为 None 来决定是否算指标。临时设 None 是「欺骗」循环只跑前向收集 logits；`finally` 块再恢复原值，保证后续 `post_process_function` 之后的真正指标计算能用上它。

**练习 2**：一个 SQuAD 样本可能对应多个 feature，这些 feature 的 logits 是如何「归并」回同一个样本的？
**参考答案**：靠 `overflow_to_sample_mapping`（训练时记录 feature→原样本）和评估时保留的 `example_id`；`postprocess_qa_predictions` 会把属于同一样本的所有 feature 候选答案汇总，再统一选最优。

---

### 4.3 best f1 / exact_match 双指标追踪

#### 4.3.1 概念说明

u5-l1 讲过 `BaseTrainer` 用一个字符串 `test_key`（如 `"accuracy"`、`"f1"`）驱动单指标的「最佳追踪」：`best_metrics = {"best_epoch":0, "best_eval_{test_key}":0}`，更新条件是 `eval_metrics["eval_"+test_key] > best`。

QA 没法直接套用这套，原因有二：

1. **QA 有两个同等重要的指标**（F1 和 EM），而 `test_key` 机制只追踪一个。
2. **QA 的 `evaluate` 被整个重写了**，返回的 metrics 字典里键是 `eval_f1`、`eval_exact_match`，且依赖后处理。`BaseTrainer._maybe_log_save_evaluate` 里那行 `eval_metrics["eval_"+self.test_key]` 对 QA 不适用。

因此 `QuestionAnsweringTrainer` 把 `_maybe_log_save_evaluate` 也**整个重写**了一遍，硬编码「以 `eval_f1` 为准判断是否刷新最佳，并同步更新 `best_eval_exact_match`」。

#### 4.3.2 核心流程

重写后的 `_maybe_log_save_evaluate`（评估回调）：

```
if should_evaluate:
    eval_metrics = self.evaluate(...)
    if eval_metrics["eval_f1"] > best_metrics["best_eval_f1"]:   # 以 F1 为裁判
        best_epoch = epoch
        best_eval_f1 = eval_metrics["eval_f1"]
        best_eval_exact_match = eval_metrics["eval_exact_match"] # EM 跟着更新
    打印 best_metrics
```

注意它与 `BaseTrainer` 的两点关键差异：

- **没有 `predict_dataset` / 测试集预测分支**：`BaseTrainer` 在刷新最佳时会跑一次 `self.predict(predict_dataset)`（如果传了 `predict_dataset`）；QA 版完全没有这段——QA 的 `predict_dataset` 在数据集类里被设成 `None`（[tasks/qa/dataset.py:58](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/qa/dataset.py#L58)），训练中只追踪验证集的 f1/em。
- **裁判指标固定为 `eval_f1`**，而非可配置的 `test_key`。

#### 4.3.3 源码精读

QA 版 `_maybe_log_save_evaluate`：

[training/trainer_qa.py:113-153](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_qa.py#L113-L153) —— 刷新条件是 `eval_metrics["eval_f1"] > self.best_metrics["best_eval_f1"]`（[第 137 行](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_qa.py#L137)）；刷新时同步写 `best_eval_exact_match`，并兼容了 `eval_exact_match` 和 `eval_exact` 两种键名（[第 140-143 行](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_qa.py#L140-L143)）。对比 [trainer_base.py:48-68](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_base.py#L48-L68) 的 `BaseTrainer` 版本，QA 版明显短一截，因为它删掉了测试集预测分支。

最终落盘（由 `run.py` 在训练结束后调用）：

[training/trainer_qa.py:155-159](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_qa.py#L155-L159) —— `log_best_metrics`：注意它和 `BaseTrainer.log_best_metrics`（[trainer_base.py:22-24](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_base.py#L22-L24)）实现不同——QA 版**只 log 不 save**（少了 `save_metrics("best", ...)` 那行）。这意味着 QA 的 `best_metrics` 只打印到日志，不会单独写成 `best_results.json`。配合 u5-l2 的搜索脚本要注意：`search.py` 是用 `glob` 找 `best_results.json` 的，QA 训练若需被搜索覆盖，需留意这个差异。

get_trainer 的装配（QA 用 `QuestionAnsweringTrainer`，**不传** `test_key`）：

[tasks/qa/get_trainer.py:36-48](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/qa/get_trainer.py#L36-L48) —— 构造 `QuestionAnsweringTrainer`，传入 `post_process_function` 和 `compute_metrics`，返回 `(trainer, dataset.predict_dataset)`，而 `predict_dataset` 是 `None`。

#### 4.3.4 代码实践（本讲主实践任务）

**实践目标**：对比 QA 与 SuperGLUE 两个 `get_trainer`，理解 QA 为什么必须走专用 `best_metrics`，而不复用 `test_key` 机制。

**操作步骤**：

1. 打开 [tasks/qa/get_trainer.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/qa/get_trainer.py) 和 [tasks/superglue/get_trainer.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/superglue/get_trainer.py)。
2. 逐项填写下表（答案见下文）。

| 对比项 | SuperGLUE `get_trainer` | QA `get_trainer` |
|--------|--------------------------|------------------|
| config 的 `num_labels` | ？ | ？ |
| 数据集类 | ？ | ？ |
| 训练器类 | ？ | ？ |
| best_metrics 的键 | ？ | ？ |
| 是否传 `test_key` / `post_process_function` | ？ | ？ |

3. 在每个问号处给出准确答案后，回答核心问题：**为什么 QA 不用 `test_key="f1"`？**

**预期结果（参考答案）**：

| 对比项 | SuperGLUE | QA |
|--------|-----------|-----|
| config `num_labels` | `dataset.num_labels`（随任务变，如 Boolq=2、COPA=2、MultiRC=2 等，由数据集决定） | **固定为 2**（start + end，与答案类别数无关） |
| 数据集类 | `SuperGlueDataset` | `SQuAD` |
| 训练器类 | `BaseTrainer` | `QuestionAnsweringTrainer`（继承自 `ExponentialTrainer`） |
| best_metrics 键 | `{"best_epoch", "best_eval_{test_key}"}`（单指标，`test_key` 如 accuracy/f1） | `{"best_epoch", "best_eval_f1", "best_eval_exact_match"}`（双指标，硬编码） |
| `test_key` / `post_process_function` | 传 `test_key`，不传 `post_process_function` | 不传 `test_key`，传 `post_process_function` |

**核心问题答案**：QA 不用 `test_key=f1` 有两个根本原因。① **指标数量**：`test_key` 机制天生只追踪**一个** eval 指标，而 QA 的 F1 和 EM 是两个需要同时追踪的官方指标，单 `test_key` 装不下。② **评估流程**：`BaseTrainer.evaluate` 是 HF 原生的，它假设 `compute_metrics` 能直接吃 logits；但 QA 必须先经 `post_process_function` 把 logits 翻译成答案文本才能算 F1/EM。既然 QA 已经整个重写了 `evaluate` 和 `_maybe_log_save_evaluate`，再假装套用 `test_key` 字符串拼接反而更绕，不如直接在重写的方法里硬编码 `eval_f1` / `eval_exact_match` 两个键来得清晰。此外，`BaseTrainer` 的 `test_key` 还绑定了一段「刷新最佳就跑测试集」的逻辑，QA 的测试集是 `None`、不需要，所以从 `BaseTrainer` 继承这段只会是累赘。

#### 4.3.5 小练习与答案

**练习 1**：QA 的 `_maybe_log_save_evaluate` 里，刷新最佳的条件用的是 F1 而不是 EM。如果某轮 EM 上升但 F1 下降，`best_eval_exact_match` 会更新吗？
**参考答案**：不会。更新 `best_eval_exact_match` 的代码嵌套在 `eval_f1 > best_eval_f1` 的 `if` 块内（[trainer_qa.py:137-143](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_qa.py#L137-L143)）。F1 不刷新，EM 就不会被记录——即「以 F1 为唯一裁判，EM 只是在 F1 刷新时顺带快照」。

**练习 2**：`QuestionAnsweringTrainer.log_best_metrics` 与 `BaseTrainer.log_best_metrics` 有何不同？这对超参搜索有何影响？
**参考答案**：QA 版只 `self.log_metrics("best", ...)` 打印，**没有** `self.save_metrics("best", ...)`（对比 [trainer_base.py:24](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_base.py#L24)）。意味着 QA 不写 `best_results.json` 到磁盘，u5-l2 的 `search.py`（靠 glob 读 `best_results.json`）默认无法直接汇总 QA 试验结果，需要额外适配。

---

## 5. 综合实践

**任务**：完整跟踪一次 QA 训练的「数据 → 模型 → 评估 → 最佳指标」全链路，并定位前缀注入在哪一步发生。

**操作步骤**：

1. 阅读 [run.py:112-114](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/run.py#L112-L114)，确认 `task_name=qa` 派发到 `tasks.qa.get_trainer`。
2. 在 [tasks/qa/get_trainer.py:20-48](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/qa/get_trainer.py#L20-L48) 里依次确认：config `num_labels=2` → `get_model(..., TaskType.QUESTION_ANSWERING, fix_bert=True)` → `SQuAD(...)` → `QuestionAnsweringTrainer(...)`。
3. 回到模型 [model/question_answering.py:163-179](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/question_answering.py#L163-L179)，确认前缀注入发生在 `self.bert(...)` 调用**之前**（`get_prompt` + `attention_mask` 拼接），且主干是冻结的。
4. 训练中评估时，进入 [trainer_qa.py:40-79](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_qa.py#L40-L79)，画出「logits → 后处理 → F1/EM」的三步。
5. 评估回调 [trainer_qa.py:137-143](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_qa.py#L137-L143) 里确认：以 F1 刷新最佳，顺带记 EM。

**产出要求**：画一张数据流图，标注以下四个位置——(a) `get_prompt` 注入前缀；(b) `qa_outputs` 产生 start/end logits；(c) `post_process_function` 把 logits 变文本；(d) `_maybe_log_save_evaluate` 用 F1 判定最佳。在每个位置旁注明所在文件和行号。

**预期结果**：你会清楚地看到，P-tuning v2 在 QA 上的「新东西」其实只有 `qa_outputs` 这个头和 `QuestionAnsweringTrainer` 这层后处理；前缀注入机制本身和分类任务分毫不差。这正是「深度提示调优」任务无关性的体现。

> 待本地验证：完整 SQuAD 训练耗时较长（roberta-large + 30 epoch）。若无 GPU，可只完成上述源码阅读与数据流图；若想实跑，参考 [run_squad_roberta.sh](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/run_script/run_squad_roberta.sh) 并先把 `num_train_epochs` 调到 1 观察日志。

---

## 6. 本讲小结

- 抽取式问答把「找答案」变成「找起点+终点两个 token 下标」，模型对每个 token 输出 `start/end logits`，由 `qa_outputs = Linear(hidden, 2)` 产生，损失是两段交叉熵的平均。
- QA 的 `num_labels` 恒为 **2**（start + end），与分类任务的「类别数」含义完全不同，这是 [get_trainer.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/qa/get_trainer.py) 里 `num_labels=2` 的来源。
- 前缀注入（`get_prompt` + `past_key_values` + 冻结主干）与分类任务**逐字相同**——P-tuning v2 的深度提示是任务无关的，换头即换任务。
- `QuestionAnsweringTrainer` 继承自 `ExponentialTrainer`（顺带继承了指数学习率衰减），重写了 `evaluate`：先临时关 `compute_metrics` 跑出原始 logits，再 `post_process_function` 翻译成答案文本，最后算 F1/EM。
- QA 不用 `BaseTrainer` 的 `test_key` 单指标机制，而是硬编码 `best_eval_f1` + `best_eval_exact_match` 双指标，以 F1 为裁判刷新最佳。
- 与 `BaseTrainer` 相比，QA 版 `_maybe_log_save_evaluate` 删去了测试集预测分支，且 `log_best_metrics` 不落盘 `best_results.json`——这对超参搜索脚本有影响。

---

## 7. 下一步学习建议

- **横向对比其他任务模型**：本讲只看了 QA 的「头」，建议接着读 u6-l2，对比 `model/multiple_choice.py`（多选如何 reshape 后注入前缀）与 `model/token_classification.py`（per-token 分类），体会「同一个 `get_prompt`，不同的输出头」。
- **深入后处理细节**：若对 SQuAD 的滑窗、n-best 选答案逻辑感兴趣，可通读 [tasks/qa/utils_qa.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/qa/utils_qa.py) 的 `postprocess_qa_predictions` 全文（源自 HuggingFace 官方实现）。
- **补齐训练循环视角**：本讲的 `train()` 循环继承自 `ExponentialTrainer`（u5-l2），建议回头对照 [trainer_exp.py:48-502](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_exp.py#L48-L502)，看清「每个 epoch 末调 `_maybe_log_save_evaluate`」如何驱动本讲的最佳指标追踪。
- **走向架构总览**：读完 u6 两讲后，可进入 u8-l1，把「入口分派 → 任务数据 → 模型工厂 → 前缀注入 → 训练器 → 评估」整条链路在脑中串成一张完整地图。

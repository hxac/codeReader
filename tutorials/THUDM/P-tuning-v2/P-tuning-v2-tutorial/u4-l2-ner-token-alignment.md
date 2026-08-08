# 序列标注数据对齐：NER

## 1. 本讲目标

上一讲（u4-l1）我们看了分类任务的数据管道：每个样本**整句话只预测一个标签**，预测时对 logits 做 `argmax(axis=1)`。本讲进入一个本质不同的任务家族——**序列标注（sequence labeling）**，以命名实体识别（NER）为例：每个样本要**对每一个词预测一个标签**。

学完本讲，你应该能够：

1. 说清楚为什么 NER 必须做「标签对齐」：分词器把一个词切成多个子词后，标签该如何跟着变长。
2. 看懂 `tokenize_and_align_labels` 里**手写的 `word_ids` 构造**与 **`-100` 忽略机制**，并能解释为什么要手写而不直接用 fast tokenizer 自带的 `word_ids()`。
3. 理解 seqeval 在**实体层级（entity-level）**计算 F1 的原理，以及它与 token 级准确率的区别。
4. 串起 NER 任务在仓库里的接线：`run.py` 分派 → `get_trainer` → `NERDataset` + `ExponentialTrainer(test_key="f1")`。

---

## 2. 前置知识

- **子词分词（subword tokenization）**：BERT/RoBERTa 用 WordPiece/BPE，会把未登录或较长的词切成多段。例如某个词可能被切成 `["token", "##izer"]` 两个子词，其中 `##` 表示「接续前一个子词」。这是 NER 标签对齐问题的根源。
- **IOB / IOBES 标注体系**：实体标注把每个词标成 `B-TYPE`（实体起始）、`I-TYPE`（实体内部）、`E-TYPE`（实体结尾）或 `O`（非实体）。本仓库默认的 CoNLL-2003 数据用的是 IOB2（`B-`/`I-`/`O`）。标注体系只影响标签字符串，**不影响对齐逻辑**——对齐只关心「一个词对应几个子词」。
- **CrossEntropyLoss 的 `ignore_index`**：PyTorch 的交叉熵损失默认 `ignore_index=-100`，即标签为 `-100` 的位置不参与损失计算。本讲会反复用到这一点。
- **HuggingFace `datasets.map`**：上一讲见过，`map(fn, batched=True)` 会对数据集批量应用一个函数，是 NER 预处理的执行方式。
- **本仓库的训练器继承链**：`BaseTrainer(transformers.Trainer)` → `ExponentialTrainer(BaseTrainer)`，其中 `BaseTrainer` 引入 `test_key` 与 `best_metrics`（见 u5-l1 的伏笔）。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tasks/ner/dataset.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/ner/dataset.py) | NER 数据集类 `NERDataset`：加载本地数据脚本、分词对齐、`compute_metrics`、`data_collator`。本讲的核心。 |
| [tasks/ner/get_trainer.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/ner/get_trainer.py) | NER 的接线函数：构造 tokenizer、`NERDataset`、模型、`ExponentialTrainer`，并传入 `test_key="f1"`。 |
| [tasks/utils.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/utils.py) | 任务/数据集注册表（`TASKS`、`NER_DATASETS`）以及 `ADD_PREFIX_SPACE`、`USE_FAST` 两张主干配置表。 |
| [model/token_classification.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/token_classification.py) | Token 分类模型，展示 `labels` 与 `attention_mask` 如何在 `forward` 里配合 `-100` 算损失（辅助理解）。 |
| [training/trainer_base.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_base.py) | `BaseTrainer`：`test_key` 如何驱动 `best_metrics`，是 `test_key="f1"` 生效的落点。 |

---

## 4. 核心概念与源码讲解

本讲三个最小模块：**`tokenize_and_align_labels`（对齐主流程）**、**`word_ids` 与 `-100`（对齐的两大支柱）**、**seqeval F1 指标**。

### 4.1 tokenize_and_align_labels：从「词级标签」到「子词级标签」

#### 4.1.1 概念说明

NER 数据集天然是**词级**的：一句话已经预先切成词（`tokens`），每个词配一个实体标签（`ner_tags`）。例如：

```
tokens:    [ "New",  "York" ]
ner_tags:  [ "B-LOC", "E-LOC" ]
```

但模型吃的是**子词级**的 `input_ids`：`[CLS] new york [SEP]`。于是出现一个长度不匹配——2 个标签却要对齐 4 个子词位置。`tokenize_and_align_labels` 就是解决这个「把短标签拉长到与子词序列等长」的问题，它的核心规则只有三条：

1. **特殊符号**（`[CLS]`、`[SEP]`）对应的位置 → 标签设 `-100`（不参与训练/评估）。
2. **一个词的第一个子词** → 保留该词的真实标签。
3. **一个词的其余子词**（词被切成多段时）→ 标签设 `-100`。

规则 2、3 保证了「**每个原始词只在一个位置产生一次监督信号**」，标签数量与子词数量完全相等。

#### 4.1.2 核心流程

```
输入：examples['tokens']          # 例如 [["New","York"], ...]  词序列
输入：examples['<task>_tags']     # 例如 [["B-LOC","E-LOC"], ...] 词级标签

1. tokenizer(..., is_split_into_words=True)   # 把「词列表」切成子词，得到 input_ids
2. 对每条样本，构造 word_ids：每个子词属于哪个原始词（特殊符号为 None）
3. 遍历 word_ids，按上面三条规则生成 label_ids
4. 把 label_ids 作为 "labels" 字段写回 tokenized_inputs
```

为什么用 `is_split_into_words=True`？因为数据已经是「按空格切好的词列表」，我们要告诉分词器「**不要再自己切词，只在词内部做子词切分**」，否则 `["New", "York"]` 会被当成一个普通字符串再切一次，对齐就全乱了。

#### 4.1.3 源码精读

整个函数在 [tasks/ner/dataset.py:86-124](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/ner/dataset.py#L86-L124)。先看分词这一步：

```python
tokenized_inputs = self.tokenizer(
    examples['tokens'],
    padding=False,
    truncation=True,
    is_split_into_words=True,   # 关键：输入已是词列表
)
```

见 [tasks/ner/dataset.py:87-93](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/ner/dataset.py#L87-L93)。`padding=False` 表示这里先不补齐——补齐交给后面的 `data_collator` 在拼 batch 时统一做。

接着是标签对齐循环（[tasks/ner/dataset.py:105-120](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/ner/dataset.py#L105-L120)）：

```python
previous_word_idx = None
label_ids = []
for word_idx in word_ids:
    if word_idx is None:                    # 特殊符号
        label_ids.append(-100)
    elif word_idx != previous_word_idx:     # 一个词的第一个子词
        label_ids.append(label[word_idx])
    else:                                   # 同一词的后续子词
        label_ids.append(-100)
    previous_word_idx = word_idx
```

三路分支正好对应 4.1.1 的三条规则。`previous_word_idx` 用来判断「当前子词是否与上一个子词属于同一个原始词」——若相同，说明这是被切开的后续片段，置 `-100`。最后把结果写回：

```python
tokenized_inputs["labels"] = labels
```

见 [tasks/ner/dataset.py:123](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/ner/dataset.py#L123)。注意 `labels` 与 `input_ids` 等长，这正是对齐成功的标志。

> **与上一讲的对照**：分类任务的 `preprocess_function` 只生成 `labels`（每样本一个标量），评估时 `argmax(axis=1)`；NER 这里 `labels` 是一条与序列等长的向量，评估时 `argmax(axis=2)`（见 4.3）。这是「样本级标签」与「token 级标签」的根本区别。

#### 4.1.4 代码实践

**实践目标**：手工模拟 `tokenize_and_align_labels` 对 `"New York"` 的处理，验证标签长度等于子词序列长度，并理解 `-100` 的产生。

**操作步骤**（阅读型 + 手算）：

1. 读 [tasks/ner/dataset.py:86-124](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/ner/dataset.py#L86-L124)。
2. 给定输入：
   - `tokens = ["New", "York"]`
   - `ner_tags = ["B-LOC", "E-LOC"]`
3. 假设 BERT 分词结果为 `[CLS] new york [SEP]`（4 个子词，`New`、`York` 各 1 个子词）。
4. 手写 `word_ids` 与对齐后的 `labels`。

**需要观察的现象**：`labels` 长度必须恰好是 4，与 `input_ids` 一一对应。

**预期结果**：

```
input_ids : [CLS]      new       york     [SEP]
word_ids  : None       0         1        None
labels    : -100       B-LOC     E-LOC    -100
```

即 `labels = [-100, B-LOC, E-LOC, -100]`。两端 `-100` 来自 `[CLS]`/`[SEP]`；中间两个词各只有 1 个子词，因此都保留真实标签。

**若某个词被切成多个子词**（例如把一个较长的词切成 `["xx", "##yy"]` 两个子词，具体切分**待本地验证**）：则该词的 `word_idx` 会出现两次，第一个子词保留标签、第二个子词因 `word_idx == previous_word_idx` 走 `else` 分支变为 `-100`。这正是「首个子词保留标签、其余设为 `-100`」的来源。

如果你想真实运行而不是手算，下面是一段**示例代码**（非项目原有代码，运行需本仓库 pt2 环境）：

```python
from transformers import AutoTokenizer
tok = AutoTokenizer.from_pretrained("bert-large-uncased")
tokens = ["New", "York"]
# 模拟 dataset 的两步：整句切子词 + 逐词统计子词数
out = tok([tokens], is_split_into_words=True)
print(out["input_ids"])                      # [101, ..., ..., 102] = [CLS] new york [SEP]
word_ids = [None]
for j, w in enumerate(tokens):
    word_ids += [j] * len(tok.encode(w, add_special_tokens=False))
word_ids += [None]
print(word_ids)                              # [None, 0, 1, None]
```

> 具体的子词 id 与切分粒度**待本地验证**（取决于词表版本），但 `word_ids` 的结构 `[None, 0, 1, None]` 是确定的。

#### 4.1.5 小练习与答案

**练习 1**：若把 `tokens` 改成 `["New", "York", "City"]`、标签 `["B-LOC", "I-LOC", "I-LOC"]`，假设每个词各 1 个子词，写出带 `[CLS]/[SEP]` 后的 `labels`。

**答案**：`[-100, B-LOC, I-LOC, I-LOC, -100]`。

**练习 2**：如果把对齐函数里 `elif word_idx != previous_word_idx` 这一支删掉、对所有非 `None` 位置都赋真实标签，训练会有什么副作用？

**答案**：一个词的每个子词都被监督成同一个标签，等价于把一个标注信号复制了多份，损失会被高频词的子词数扭曲；同时评估时实体边界判定会受到冗余预测干扰。因此标准做法是只在首子词保留标签。

---

### 4.2 word_ids 与 -100：自定义对齐与忽略机制

#### 4.2.1 概念说明

4.1 里的 `word_ids` 是「每个子词 → 所属原始词的编号」的映射表。HuggingFace 的 **fast tokenizer** 本身提供 `tokenized_inputs.word_ids(batch_index=i)` 方法可以直接拿到这张表。但本仓库**没有用它**，而是在代码里手写了一遍——

```python
# word_ids = tokenized_inputs.word_ids(batch_index=i)   ← 被注释掉的原始写法
```

为什么？因为 `word_ids()` 是 **fast tokenizer 专属**接口，慢速（Python 实现）tokenizer 没有这个方法。而本仓库的 [tasks/utils.py:23-29](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/utils.py#L23-L29) 里 `deberta-v2` 的 `USE_FAST=False`：

```python
USE_FAST = {
    'bert': True, 'roberta': True, 'deberta': True,
    'gpt2': True, 'deberta-v2': False,
}
```

手写 `word_ids` 用的是 `tokenizer.encode(word, add_special_tokens=False)`，**快速和慢速 tokenizer 都支持**，从而保证 NER 流程在不同主干下都能跑通。这是这段「看起来多此一举」的代码真正的稳健性考量。

#### 4.2.2 核心流程

手写构造 `word_ids` 的算法（[tasks/ner/dataset.py:97-102](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/ner/dataset.py#L97-L102)）：

```
word_ids = [None]                              # 对应 [CLS]
for 每个词 word（编号 j）:
    n = len(tokenizer.encode(word, add_special_tokens=False))   # 该词的子词数
    word_ids += [j] * n                        # 这 n 个子词都属于第 j 个词
word_ids += [None]                             # 对应 [SEP]
```

关键性质：**逐词独立编码**得到子词数 `n`，再把词编号 `j` 复制 `n` 份。这样 `word_ids` 的长度 = `[CLS]` + 全部子词 + `[SEP]`，与 `input_ids` 精确等长。

而 `-100` 的作用贯穿训练与评估两端：

- **训练端**：`CrossEntropyLoss(ignore_index=-100)` 自动跳过 `-100`，所以特殊符号、非首子词都不产生梯度。
- **评估端**：`compute_metrics` 用 `if l != -100` 过滤，`-100` 位置的预测不计入指标。

两者靠同一个魔法数 `-100` 串起来。

#### 4.2.3 源码精读

手写 `word_ids` 段（[tasks/ner/dataset.py:97-102](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/ner/dataset.py#L97-L102)）：

```python
word_ids = [None]
for j, word in enumerate(examples['tokens'][i]):
    token = self.tokenizer.encode(word, add_special_tokens=False)
    word_ids += [j] * len(token)
word_ids += [None]
```

`label_column_name` 的来源值得一看（[tasks/ner/dataset.py:23-26](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/ner/dataset.py#L23-L26)）：

```python
self.label_column_name = f"{data_args.task_name}_tags"   # ner 任务 → "ner_tags"
self.label_list = features[self.label_column_name].feature.names   # ['O','B-PER','I-PER',...]
self.label_to_id = {l: i for i, l in enumerate(self.label_list)}
```

它用 `task_name` 拼出标签列名（NER 即 `ner_tags`，与 CoNLL-2003 数据脚本一致），再从 `datasets` 的 `ClassLabel` 特征里取出**所有标签的字符串名** `label_list`。这个 `label_list` 后面会在 `compute_metrics` 里把整数预测还原成 `B-LOC` 这样的字符串，是 seqeval 计算的必要输入。

> **模型侧如何配合 `-100`**：`model/token_classification.py` 里 `BertPrefixForTokenClassification.forward` 算损失时，用 `attention_mask` 把 padding 也置成 `ignore_index`（[model/token_classification.py:197-203](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/token_classification.py#L197-L203)）。也就是说：**数据侧的 `-100` 负责屏蔽特殊符号与非首子词，模型侧的 `attention_mask` 负责屏蔽 batch padding**，两套机制叠加，最终只有「真正需要监督的首子词」参与损失。

#### 4.2.4 代码实践

**实践目标**：取一句话 `New York` 标注为 `B-LOC/E-LOC`，模拟 tokenizer 切成多个子词，写出对齐后的 `labels`，并标注哪些位置是 `-100`。

**操作步骤**：

1. 设 `tokens = ["New", "York"]`，`ner_tags = ["B-LOC", "E-LOC"]`（`New`=起始，`York`=结尾；本仓库 CoNLL-2003 实际用 IOB2 的 `B-/I-`，此处沿用题目的 `B-/E-` 表述，不影响对齐逻辑）。
2. 模拟「`New` 切成 1 个子词、`York` 切成 2 个子词（`york` + `##x`，**待本地验证**具体切分）」的情形，按 4.2.2 算法构造 `word_ids`。
3. 套用三路规则生成 `labels`。

**需要观察的现象**：`labels` 长度 = `[CLS]` + 1 + 2 + `[SEP]` = 5；被切开的词只有第一个子词带标签。

**预期结果**：

```
input_ids : [CLS]   new     york    ##x    [SEP]
word_ids  : None    0       1       1      None
labels    : -100    B-LOC   E-LOC   -100   -100
```

- 位置 0、4（`[CLS]`/`[SEP]`）→ `-100`（规则 1）。
- 位置 1（`New` 的唯一子词）→ `B-LOC`（规则 2）。
- 位置 2（`York` 的第一个子词）→ `E-LOC`（规则 2）。
- 位置 3（`York` 的第二个子词，`word_idx==previous`）→ `-100`（规则 3）。

**为何首个子词保留标签、其余设 `-100`**：因为标注的粒度是**原始词**，一个词只能有一个标签。我们只在该词的第一个子词上放真实标签，就保证了「一词一监督信号」；后续子词置 `-100` 后既不进损失、也不进评估，避免重复计数和边界歧义。

> 说明：`New`/`York` 在 `bert-large-uncased` 下通常各为 1 个子词（即不会发生本例中 `York` 被切开的情况），上面的「`York` 切 2 段」是为演示规则 3 而做的**模拟**。要观察真实的子词切分，请用上面的示例代码在本地 `print(tok.encode("York"))` 验证。

#### 4.2.5 小练习与答案

**练习 1**：把 `word_ids = [None]` 初始化改成 `word_ids = []`（删掉首部 `None`），会对 `labels` 造成什么影响？

**答案**：`labels` 会少一个最前面的 `-100`，整体比 `input_ids` 短 1，与 `[CLS]` 错位；后续训练时 `labels` 与 `logits` 形状不匹配，直接报错。首尾两个 `None` 正好对应 `[CLS]` 和 `[SEP]`，不能省。

**练习 2**：为什么本仓库手写 `word_ids` 而不用 `tokenized_inputs.word_ids(batch_index=i)`？

**答案**：`word_ids()` 是 fast tokenizer 专属；本仓库对 `deberta-v2` 用慢速 tokenizer（`USE_FAST['deberta-v2']=False`）。手写实现用通用的 `tokenizer.encode`，不依赖 fast 后端，保证 NER 在所有主干上都能对齐。

**练习 3**：`-100` 在训练和评估中分别起什么作用？

**答案**：训练时，`CrossEntropyLoss` 默认 `ignore_index=-100`，跳过这些位置不计损失；评估时，`compute_metrics` 用 `l != -100` 过滤，这些位置的预测不计入指标。两端共享 `-100` 这一约定。

---

### 4.3 seqeval F1 指标与 NER 训练器接线

#### 4.3.1 概念说明

NER 的指标不是「token 级准确率」，而是**实体级 F1**。区别在于：seqeval 会先把一串标签还原成**实体跨度（span）**——例如 `["B-LOC", "I-LOC"]` 还原成一个 `LOC` 实体——再比较「预测实体集合」与「真实实体集合」。只有当一个实体的**类型和边界都完全正确**才算命中（严格模式）。

设命中的实体数为 TP、预测出的实体数为 PP、真实的实体数为 RP，则：

\[
P = \frac{TP}{PP}, \quad R = \frac{TP}{RP}, \quad F_1 = \frac{2 \cdot P \cdot R}{P + R}
\]

为什么不用 token 准确率？因为 NER 里 `O`（非实体）标签占绝大多数，一个全输出 `O` 的模型也能拿到很高的 token 准确率，却没有识别出任何实体。实体级 F1 才反映真实能力。

#### 4.3.2 核心流程

`compute_metrics` 的处理链：

```
predictions (batch, seq, num_labels)  ← 模型输出 logits
   │ argmax(axis=2)
   ▼
predictions (batch, seq)              ← 每个 token 一个标签 id
   │ 过滤 l != -100，并用 label_list[id] 还原成 "B-LOC" 字符串
   ▼
true_predictions / true_labels        ← List[List[str]]，交给 seqeval
   │ metric.compute
   ▼
{precision, recall, f1, accuracy}
```

NER 的训练器接线（`get_trainer`）则把这一切串起来：用 `ExponentialTrainer`（指数学习率衰减）并把 `test_key="f1"` 传给基类 `BaseTrainer`，让 F1 成为「追踪最佳模型」的依据。

#### 4.3.3 源码精读

`compute_metrics` 全貌在 [tasks/ner/dataset.py:64-84](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/ner/dataset.py#L64-L84)：

```python
predictions = np.argmax(predictions, axis=2)   # 取每个 token 概率最大的标签
true_predictions = [
    [self.label_list[p] for (p, l) in zip(prediction, label) if l != -100]
    for prediction, label in zip(predictions, labels)
]
true_labels = [
    [self.label_list[l] for (p, l) in zip(prediction, label) if l != -100]
    for prediction, label in zip(predictions, labels)
]
results = self.metric.compute(predictions=true_predictions, references=true_labels)
return {"precision": ..., "recall": ..., "f1": ..., "accuracy": ...}
```

要点（[tasks/ner/dataset.py:66-84](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/ner/dataset.py#L66-L84)）：

- `argmax(axis=2)`：注意是 `axis=2`（分类任务是 `axis=1`），因为最后一维才是标签维。
- `if l != -100`：评估时再次用 `-100` 过滤，与训练端的屏蔽呼应。
- `self.label_list[p]`：把整数 id 还原成 `B-LOC` 字符串，seqeval 需要字符串才能解析实体边界。
- seqeval 由 [tasks/ner/dataset.py:61](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/ner/dataset.py#L61) 的 `self.metric = load_metric('seqeval')` 加载。

训练器接线在 [tasks/ner/get_trainer.py:63-73](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/ner/get_trainer.py#L63-L73)：

```python
trainer = ExponentialTrainer(
    model=model, args=training_args,
    train_dataset=..., eval_dataset=..., predict_dataset=...,
    tokenizer=tokenizer, data_collator=dataset.data_collator,
    compute_metrics=dataset.compute_metrics,
    test_key="f1"
)
```

`test_key="f1"` 进入基类 `BaseTrainer` 后（[training/trainer_base.py:13-20](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_base.py#L13-L20)），`best_metrics` 的键就变成 `best_eval_f1`、`best_test_f1`：

```python
def __init__(self, *args, predict_dataset=None, test_key="accuracy", **kwargs):
    ...
    self.best_metrics = OrderedDict({
        "best_epoch": 0,
        f"best_eval_{self.test_key}": 0,    # → "best_eval_f1"
    })
```

每次评估后，只有当验证 F1 提升时才会刷新最佳指标并可选地跑测试集（[training/trainer_base.py:52-54](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_base.py#L52-L54)）：

```python
if eval_metrics["eval_"+self.test_key] > self.best_metrics["best_eval_"+self.test_key]:
    ...
```

而 `ExponentialTrainer` 只是把学习率调度器换成指数衰减（[training/trainer_exp.py:42-45](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_exp.py#L42-L45)）：

```python
def create_scheduler(self, num_training_steps, optimizer=None):
    if self.lr_scheduler is None:
        self.lr_scheduler = torch.optim.lr_scheduler.ExponentialLR(self.optimizer, gamma=0.95, ...)
    return self.lr_scheduler
```

即每步学习率乘 0.95。这是 NER 默认的训练器，也是后面 u5-l2「超参搜索」会用到的基础。

> **整条接线回顾**：`run.py` 在 [run.py:104-106](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/run.py#L104-L106) 断言 `dataset_name in NER_DATASETS` 并惰性 import `tasks.ner.get_trainer`；`NER_DATASETS = ["conll2003", "conll2004", "ontonotes"]`（[tasks/utils.py:6](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/utils.py#L6)）。`get_trainer` 再用 `ADD_PREFIX_SPACE`/`USE_FAST` 配 tokenizer（[tasks/ner/get_trainer.py:27-36](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/ner/get_trainer.py#L27-L36)）、用 `get_model(..., TaskType.TOKEN_CLASSIFICATION, fix_bert=True)` 取冻结主干的 prefix 模型（[tasks/ner/get_trainer.py:61](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/ner/get_trainer.py#L61)），最后装进 `ExponentialTrainer`。

#### 4.3.4 代码实践

**实践目标**：用 seqeval 在一个迷你样本上体会「实体级 F1」与「token 准确率」的差异。

**操作步骤**（示例代码，非项目原有；运行需 `pip install seqeval`，已在 `requirements.txt` 中）：

```python
from seqeval.metrics import classification_report
from seqeval.scheme import IOB2

# 真实标签与预测标签（已是实体标注字符串序列）
y_true = [["B-LOC", "I-LOC", "O"]]
y_pred = [["B-LOC", "O",      "O"]]   # 第二个 token 预测错了

print(classification_report(y_true, y_pred, mode='strict', scheme=IOB2))
```

**需要观察的现象**：token 准确率是 2/3（前两个里预测对 1 个、第三个都对），但严格实体级 F1 是 0——因为预测里根本没有形成完整的 `LOC` 实体（`B-LOC` 后面不是 `I-LOC`）。

**预期结果**：实体级 precision/recall/f1 均为 0（或依据 seqeval 版本显示无完整实体命中），明显低于 token 准确率。这正说明为什么 NER 必须用 seqeval 而非 token 准确率。具体输出格式**待本地验证**（依赖 seqeval 版本）。

#### 4.3.5 小练习与答案

**练习 1**：`compute_metrics` 里为什么是 `argmax(axis=2)` 而分类任务是 `argmax(axis=1)`？

**答案**：模型对序列标注输出形状 `(batch, seq_len, num_labels)`，标签维在最后一维（axis=2），要对每个 token 取最大标签。分类任务输出 `(batch, num_labels)`，标签维是 axis=1。

**练习 2**：NER 的 `test_key` 为什么是 `"f1"` 而不是 `"accuracy"`？

**答案**：NER 里 `O` 标签占多数，token 准确率会虚高，不能反映实体识别能力；实体级 F1 才是有意义的指标，因此用 `test_key="f1"` 让 `BaseTrainer` 追踪 F1 最优。

**练习 3**：`compute_metrics` 在调用 seqeval 之前，必须把整数 id 还原成 `B-LOC` 这样的字符串，为什么？

**答案**：seqeval 需要从标签前缀（`B-`/`I-`/`E-`/`O`）解析实体边界与类型，纯整数 id 无法区分边界，所以必须用 `label_list[id]` 还原成字符串。

---

## 5. 综合实践

**任务**：以 `conll2004` 为例，把 NER 从「原始 CoNLL 文本」到「最终 F1」的整条数据流在脑中跑一遍，并对照源码验证每一步。

**步骤**：

1. **入口与分派**：阅读 [run.py:104-106](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/run.py#L104-L106)，确认 `task_name=ner`、`dataset_name=conll2004` 会进入 `tasks.ner.get_trainer`，并断言 `conll2004 in NER_DATASETS`（见 [tasks/utils.py:6](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/utils.py#L6)）。
2. **数据加载与对齐**：阅读 [tasks/ner/dataset.py:13](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/ner/dataset.py#L13)（`load_dataset` 本地脚本）、[tasks/ner/dataset.py:32-37](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/ner/dataset.py#L32-L37)（`map(tokenize_and_align_labels)`）、以及 [tasks/ner/dataset.py:59](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/ner/dataset.py#L59) 的 `DataCollatorForTokenClassification`（fp16 时 pad 到 8 的倍数）。
3. **手算对齐**：自选一句 3~4 个词、含一个会被切成多子词的词，写出 `tokens / ner_tags / word_ids / labels` 四行对照表，标出所有 `-100` 的来源。
4. **指标与训练器**：确认 [tasks/ner/get_trainer.py:63-73](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/ner/get_trainer.py#L63-L73) 用了 `ExponentialTrainer` + `test_key="f1"`，并解释这会让 `BaseTrainer` 在验证 F1 提升时才刷新 `best_eval_f1` 并跑测试集。

**预期产出**：一张完整的「文本 → 子词 → 标签对齐 → 损失/指标」对照表，以及对 `-100` 在训练与评估两端的统一解释。

> 若想真实发起一次（极小规模）运行，可参考 [run_script/run_conll04_bert.sh](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/run_script/run_conll04_bert.sh)，把 `num_train_epochs` 调成 1、加上 `--max_train_samples` 极小值，观察日志里 `map` 的进度条与 `best_eval_f1` 字段。是否能完整跑通**待本地验证**（依赖数据下载与 GPU 环境）。

---

## 6. 本讲小结

- NER 是**序列标注**：每个词预测一个标签，评估用 `argmax(axis=2)`，与分类的「样本级单标签」本质不同。
- `tokenize_and_align_labels` 用三条规则把词级标签拉长到子词级：特殊符号→`-100`、词的首子词→真实标签、词的其余子词→`-100`，保证「一词一监督信号」。
- 本仓库**手写 `word_ids`**（逐词 `encode` 取子词数再复制编号），而非用 fast tokenizer 的 `word_ids()`，原因是后者对慢速 tokenizer（如 `deberta-v2`）不可用。
- `-100` 是贯穿训练（`CrossEntropyLoss.ignore_index`）与评估（`if l != -100`）的统一屏蔽约定；模型侧再用 `attention_mask` 屏蔽 batch padding。
- NER 用 **seqeval 实体级 F1** 而非 token 准确率，因为实体边界与类型必须同时正确才算命中。
- 训练器接线：`ExponentialTrainer`（指数学习率衰减）+ `test_key="f1"`，让 F1 成为追踪最佳模型的依据。

---

## 7. 下一步学习建议

- 接下来进入 **U5 训练流程**：先读 [u5-l1 BaseTrainer 与最佳指标追踪](u5-l1-base-trainer-best-metrics.md)，彻底弄清本讲反复提到的 `test_key`、`best_metrics`、`_maybe_log_save_evaluate` 机制；再读 [u5-l2 ExponentialTrainer 与超参搜索](u5-l2-exptrainer-and-hp-search.md)，了解 NER 默认的指数衰减调度与 `search_script` 网格搜索。
- 想深入前向，可读 [model/token_classification.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/token_classification.py) 中 `BertPrefixForTokenClassification` 的 `forward`，看 prefix 注入与本讲的 `attention_mask` 屏蔽如何配合。
- 若关心 SRL（另一类序列标注），可对照 `tasks/srl/` 的数据脚本，看它与 NER 在对齐细节上的异同。

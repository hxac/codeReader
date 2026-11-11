# BERT 微调与 HuggingFace 生态

## 1. 本讲目标

本讲是 Transformer 单元（第 16 章）的收官讲。与上一讲（u16-l2，GPT-2 微调）最大的不同是：**本讲精读的全部代码都真实存在于仓库中**，位于 [ch16/ch16-part3-bert.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part3-bert.py)。学完本讲，你应该能够：

1. 独立读懂「下载 IMDb 数据 → 三段划分 → DistilBERT 分词 → 封装 Dataset」这条数据管线；
2. 理解 `DistilBertTokenizerFast.from_pretrained(...)` 背后「分词器是预训练模型一部分」的约定，以及 `IMDbDataset.__getitem__` 返回**字典**而非元组的原因；
3. 掌握 HuggingFace `Trainer` + `TrainingArguments` 的训练封装方式，并能与第 13 章的 Lightning（u13-l3）、手写 PyTorch 循环三方对照；
4. 理解 `compute_metrics` 回调中 `logits → np.argmax` 的解码过程，并能解释它输出的 `eval_accuracy` 为什么与 `compute_accuracy` 在测试 DataLoader 上的结果完全一致。

## 2. 前置知识

本讲默认你已读过以下讲义，相关结论直接承接、不再重复推导：

- **u16-l1（自注意力）**：Transformer 的核心运算是缩放点积注意力。
- **u16-l2（GPT-2 微调）**：「预训练 → 微调」两阶段范式；`*ForSequenceClassification` 类模型加载时分类头是**随机初始化**的（本讲会看到仓库中真实的加载警告文本）；`compute_accuracy` 型评估函数的 `no_grad + argmax` 累计套路；`truncation` / `padding` / `attention_mask` 三个分词参数的含义。
- **u12-l2（Dataset/DataLoader）**：Dataset 只需实现 `__len__` 与 `__getitem__` 两个方法即可被 DataLoader 接受。
- **u13-l3（Lightning/Ignite）**：高层训练框架的价值在于把训练循环、日志、checkpoint 等「工程项」标准化。HuggingFace 的 `Trainer` 是同一思想的另一种实现。

补充三个本讲专属的背景概念：

- **BERT 与 DistilBERT**：BERT 是**双向**编码器 Transformer——预训练任务是掩码语言建模（随机遮住约 15% 的词让模型猜），因此每个位置的表示都能同时看到左右两侧上下文；做分类时取特殊词元 `[CLS]` 的最终隐状态作为整句表示。DistilBERT 是 BERT 的知识蒸馏版：参数少约 40%、速度快约 60%，GLUE 基准保留 95% 以上性能（脚本开头引用了这句官方描述，见 [ch16/ch16-part3-bert.py:L62-L66](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part3-bert.py#L62-L66)）。微调 3.5 万条影评这种「小数据」任务，DistilBERT 是算力友好型的标准选择。
- **WordPiece 子词分词**：BERT 系分词器先把文本拆成子词单元（如 `unaffable → un ##aff ##able`），常用词整词入表、生僻词拆成碎片，从而用 3 万左右的小词表覆盖任意文本。这正是「分词器必须与预训练权重配套」的原因——词表与嵌入矩阵逐行对应，换一套词表等于乱码。
- **`datasets` 库的 `load_metric`**：HuggingFace 曾把常用指标（accuracy、f1 等）做成按名下载的脚本。注意版本：仓库锁定 `datasets==1.11.0`；在新版生态中 `load_metric` 已迁移为独立的 `evaluate` 库（`evaluate.load("accuracy")`），若你安装的是新版本需要替换这一行。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [ch16/ch16-part3-bert.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part3-bert.py) | 本讲唯一精读对象：数据下载划分 → 分词 → Dataset → 手动循环 → Trainer API，全部流程约 380 行 |
| [ch16/ch16-part3-bert.ipynb](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part3-bert.ipynb) | 等价 notebook，**保留了作者的真实运行输出**（准确率、训练日志、加载警告），本讲引用的所有数值都来自这里 |
| [ch08/movie_data.csv.gz](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/movie_data.csv.gz) | 被下载的 IMDb 影评数据（5 万条、已在第 8 章打好乱序、`review`/`sentiment` 两列） |
| [ch16/README.md](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/README.md) | 第 16 章大纲，本讲对应 "Fine-tuning a BERT model in PyTorch" 一节 |

另：根目录 `environment.yml` 只覆盖前几章的经典机器学习栈，**不含** `transformers`/`datasets`。第 16 章的版本要求以脚本内的 `check_packages` 字典为准（[L39-L46](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part3-bert.py#L39-L46)）：`torch>=1.9.0`、`datasets>=1.11.0`、`transformers>=4.9.1`，需要额外 `pip install transformers==4.9.1 datasets==1.11.0`。

## 4. 核心概念与源码讲解

### 4.1 下载与划分 IMDb 数据

#### 4.1.1 概念说明

微调的第一步永远是拿到带标签的数据。本节解决两个问题：

1. **数据从哪来**：脚本直接从**本仓库的 ch08 目录**下载第 8 章整理好的 IMDb 影评 CSV（gzip 压缩）。这份数据有两个宝贵性质：5 万条评论已按情感 0/1 平分（各 25000 条），且行序在制作时已经打乱——后者让后面「按行号顺序切片划分」成为合法操作（还记得 u8-l2 的教训吗：类别块状排列的 CSV 会毁掉训练）。
2. **怎么划分**：按行切成 **35000 训练 / 5000 验证 / 10000 测试** 三份。注意这与第 8 章「20000 训练 + 5000 测试」的划分不同——Transformer 微调是数据饥渴的，作者给了更大的训练份额。

#### 4.1.2 核心流程

```text
GitHub 上的 movie_data.csv.gz
        │  requests.get(url).content 整块下载
        ▼
本地 movie_data.csv.gz
        │  gzip.open + shutil.copyfileobj 流式解压
        ▼
本地 movie_data.csv
        │  pd.read_csv → DataFrame(50000, 2)
        ▼
df.iloc[:35000]        → 训练集（文本 + 标签）
df.iloc[35000:40000]   → 验证集
df.iloc[40000:]        → 测试集
```

#### 4.1.3 源码精读

**全局设置**（[ch16/ch16-part3-bert.py:L87-L92](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part3-bert.py#L87-L92)）：固定随机种子与 `cudnn.deterministic = True` 保证可复现；`DEVICE` 自动选 CUDA 或 CPU；`NUM_EPOCHS = 3` 只作用于后面的手动训练循环（Trainer 用自己的 `num_train_epochs`，这是一个容易混淆的点，稍后 4.3 会再强调）。

**下载与解压**（[ch16/ch16-part3-bert.py:L101-L110](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part3-bert.py#L101-L110)）：

```python
url = "https://github.com/rasbt/machine-learning-book/raw/main/ch08/movie_data.csv.gz"
filename = url.split("/")[-1]          # 取 'movie_data.csv.gz'

with open(filename, "wb") as f:
    r = requests.get(url)
    f.write(r.content)                  # 二进制整块写入

with gzip.open('movie_data.csv.gz', 'rb') as f_in:
    with open('movie_data.csv', 'wb') as f_out:
        shutil.copyfileobj(f_in, f_out) # 边读边写、流式解压
```

`url.split("/")[-1]` 是个惯用小技巧：从 URL 尾部截出文件名，换数据集时只改 `url` 一行。

**读入与三段划分**（[ch16/ch16-part3-bert.py:L117-L137](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part3-bert.py#L117-L137)）：`df.head()` 确认 `review`/`sentiment` 两列格式正常，`df.shape` 输出 `(50000, 2)`；随后用 `iloc` 行切片 + `['review'].values` / `['sentiment'].values` 各取一列，得到 6 个 numpy 数组。没有调用 `train_test_split`——因为数据本身已打乱，固定切片即可复现。

#### 4.1.4 代码实践

**实践目标**：不训练任何模型，只跑通数据段，亲眼确认数据规模与类别平衡。

**操作步骤**（示例代码，基于脚本 L101-L137 改写，只保留数据部分）：

```python
import gzip, shutil, requests
import pandas as pd
import numpy as np

url = "https://github.com/rasbt/machine-learning-book/raw/main/ch08/movie_data.csv.gz"
with open('movie_data.csv.gz', 'wb') as f:
    f.write(requests.get(url).content)
with gzip.open('movie_data.csv.gz', 'rb') as f_in, open('movie_data.csv', 'wb') as f_out:
    shutil.copyfileobj(f_in, f_out)

df = pd.read_csv('movie_data.csv')
train_labels = df.iloc[:35000]['sentiment'].values
valid_labels = df.iloc[35000:40000]['sentiment'].values
test_labels  = df.iloc[40000:]['sentiment'].values

print(df.shape)                                   # 期望 (50000, 2)
print(np.bincount(train_labels),                  # 三段各自的两类数量
      np.bincount(valid_labels),
      np.bincount(test_labels))
print(df.iloc[0]['review'][:80])                  # 看一眼第一条评论
```

**需要观察的现象**：形状为 (50000, 2)；三段的类别计数各自接近各半（训练集约 17500/17500）。

**预期结果**：三段都接近类别均衡；打印出的评论是原始英文文本（含换行、HTML 残留等），说明**没有做任何人工清洗**——子词分词器会处理这一切，这是与第 8 章词袋管线（自写 `preprocessor`、词干提取）的鲜明对比。待本地验证（依赖网络下载 gzip 压缩包，十 MB 量级）。

#### 4.1.5 小练习与答案

1. **练习**：为什么这里敢用 `df.iloc` 顺序切片，而 u8-l2 的在线学习里必须先打乱 CSV 行序？
   **答案**：`movie_data.csv` 在第 8 章制作时行序已打乱，顺序切片等价于随机划分；u8-l2 强调的是「如果你拿到的是未打乱的 CSV」这一一般情形的陷阱。
2. **练习**：把训练切片改成 `df.iloc[:20000]`、验证测试不变，其余代码不动，会发生什么？
   **答案**：功能上仍能跑通，但训练样本从 35000 减到 20000；后续手动循环的 `len(train_loader)` 与 Trainer 的总步数都会按比例缩小，微调后准确率通常略降。
3. **练习**：`shutil.copyfileobj(f_in, f_out)` 换成 `f_out.write(f_in.read())` 有什么区别？
   **答案**：功能等价，但 `read()` 会把整个解压后的文件一次性读进内存，而 `copyfileobj` 按块搬运，内存占用恒定——与 u8-l2 流式学习的动机同源。

### 4.2 DistilBERT 分词器与 IMDbDataset 编码

#### 4.2.1 概念说明

上一讲已从概念上见过 `truncation`/`padding`/`attention_mask`，本节看仓库的真实实现，重点是两个新约定：

1. **分词器与模型同名加载**。代码用 `DistilBertTokenizerFast.from_pretrained('distilbert-base-uncased')` 而非更通用的 `AutoTokenizer`，两者都会从 HuggingFace Hub 下载与 `distilbert-base-uncased` 预训练权重**配套**的词表（约 30522 个 WordPiece 子词）。名字里的 `uncased` 表示词表全部小写——分词时会先 lowercase；`Fast` 表示基于 Rust 的 tokenizers 实现，返回的 `Encoding` 对象带有 offsets 等额外信息且速度快。**「指定预训练名字」就是在指定词表**：模型嵌入矩阵的第 i 行与词表第 i 个子词一一对应，二者必须成对使用。
2. **Dataset 返回字典**。第 12 章的自定义 Dataset `__getitem__` 返回 `(特征, 标签)` 元组；而 `IMDbDataset` 返回一个 **dict**，键名与模型 `forward` 的参数名对齐（`input_ids`、`attention_mask`、`labels`……）。这是 HuggingFace 生态的约定：`Trainer` 取到 batch 后按键名展开、直接以关键字参数喂给模型；其中 `labels` 键的存在使模型**内部自动计算并返回 loss**，于是训练循环里连损失函数都不用手写。

#### 4.2.2 核心流程

```text
文本 list(35000 条影评)
   │ tokenizer(texts, truncation=True, padding=True)
   │    · truncation: 超过模型上限 512 子词的评论截断
   │    · padding:    补 [PAD] 到本次调用内最长序列
   ▼
train_encodings: Encoding 列表（每条含 ids / attention_mask / type_ids ...）
   │ IMDbDataset(encodings, labels)
   │    __getitem__(i) → {'input_ids': tensor, 'attention_mask': tensor,
   │                      'labels': tensor}
   ▼
DataLoader(dataset, batch_size=16, shuffle=True)
   → 逐批 dict of tensors
```

注意一个内存细节：这里对**整个数据集一次性编码**，所有 token id 都驻留内存（5 万条影评 × 每条至多 512 个整数，随 Python 列表开销膨胀到 GB 量级）；`padding=True` 在「整表一次编码」时会把每条都补到全集最长（被截断后即 512）。工程上更省内存的做法是分批或惰性编码，但教学代码优先简单。

#### 4.2.3 源码精读

**创建分词器**（[ch16/ch16-part3-bert.py:L144](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part3-bert.py#L144)）：

```python
tokenizer = DistilBertTokenizerFast.from_pretrained('distilbert-base-uncased')
```

**一次性编码三份数据**（[ch16/ch16-part3-bert.py:L149-L156](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part3-bert.py#L149-L156)）：三次调用完全同构，只换数据。notebook 中 `train_encodings[0]` 的真实输出是：

```text
Encoding(num_tokens=512, attributes=[ids, type_ids, tokens, offsets, attention_mask, special_tokens_mask, overflowing])
```

`num_tokens=512` 正是 `truncation=True` 顶到模型上限的证据——影评很长，几乎条条被截断；BERT 仍能学好，说明 512 个子词已携带足够的情感线索。

**IMDbDataset 类**（[ch16/ch16-part3-bert.py:L163-L179](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part3-bert.py#L163-L179)）：

```python
class IMDbDataset(torch.utils.data.Dataset):
    def __init__(self, encodings, labels):
        self.encodings = encodings
        self.labels = labels

    def __getitem__(self, idx):
        item = {key: torch.tensor(val[idx]) for key, val in self.encodings.items()}
        item['labels'] = torch.tensor(self.labels[idx])
        return item

    def __len__(self):
        return len(self.labels)
```

三个要点：① `__getitem__` 里才把 list 转 `torch.tensor`（惰性转换）；② `self.encodings.items()` 把 Encoding 的全部字段（`ids`、`type_ids`、`attention_mask`……）都收进字典——字段比模型需要的多不要紧，`Trainer` 与模型会按名取用；③ `labels` 以整数标量张量加入，键名不可随意改。

**三个 DataLoader**（[ch16/ch16-part3-bert.py:L184-L186](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part3-bert.py#L184-L186)）：训练集 `shuffle=True`，验证/测试 `shuffle=False`——与第 12 章的约定一致；batch_size 统一为 16。手动循环与 `Trainer` **共用这同一个 `train_dataset`**，这是后面公平对比的前提。

#### 4.2.4 代码实践

**实践目标**：用 3 条自己写的句子看清「子词分词 + 补齐 + attention_mask」到底产出了什么。

**操作步骤**（示例代码，仅依赖 `transformers`，几秒即完，无需 GPU）：

```python
from transformers import DistilBertTokenizerFast
tokenizer = DistilBertTokenizerFast.from_pretrained('distilbert-base-uncased')

enc = tokenizer(["I love this movie!", "It was unwatchable.", "OK"], 
                truncation=True, padding=True)
enc = enc[0]                       # 取第一条的 Encoding（新版返回 BatchEncoding）
print(enc.tokens)                  # 子词序列（含 [CLS]/[SEP]/[PAD]）
print(enc.ids)                     # 与词表对应的整数 id
print(enc.attention_mask)          # 1=真实子词, 0=填充
```

然后把这两条句子装进一个迷你 `IMDbDataset`（直接复制 L163-L174 的类），验证 `dataset[1]` 返回的 dict 各键形状相同（都已 pad 到批内最长）。

**需要观察的现象**：`[CLS]` 与 `[SEP]` 自动加在首尾；短句被 `[PAD]` 补齐且其 attention_mask 为 0；`unwatchable` 可能被拆成 `un ##watch ##able` 之类的碎片。

**预期结果**：三条句子编码后等长；`len(enc.ids) == len(enc.attention_mask)`。待本地验证（需联网下载词表，约 2 MB）。

#### 4.2.5 小练习与答案

1. **练习**：把 `DistilBertTokenizerFast` 换成 `BertTokenizer.from_pretrained('bert-base-cased')`，但模型仍用 `distilbert-base-uncased`，会发生什么？
   **答案**：词表不同（cased 且来自 BERT），token id 与 distilbert 嵌入矩阵的行不再对应，等于给模型喂乱码，训练无法收敛——分词器必须与预训练权重同名配套。
2. **练习**：`IMDbDataset` 为什么不实现 `collate_fn` 就能被 DataLoader 按批堆叠？
   **答案**：`__getitem__` 返回值全是**同键名的张量 dict**，DataLoader 的默认 collate 会对 dict 逐键堆叠，得到 `batch['input_ids']` 形如 `(batch_size, seq_len)` 的批量张量。
3. **练习**：`truncation=True` 丢掉的尾部文本会不会造成信息泄漏风险？它牺牲了什么？
   **答案**：截断只丢信息、不引入泄漏；牺牲的是长评尾部的线索（若结论集中在结尾可能受损），换取固定 512 长度对齐与批量计算效率。

### 4.3 Trainer 与 TrainingArguments 训练封装

#### 4.3.1 概念说明

脚本先写了一个**手写 PyTorch 训练循环**（本讲只作对照，其五步骨架在 u12-l3/u16-l2 已反复出现），然后用 `Trainer` 重写同一件事。对照第 13 章：`Trainer` 之于 HuggingFace，相当于 `pl.Trainer` + `LightningModule` 之于 Lightning——把「取批、前向、算损失、反传、更新、日志、存档」全部收进框架，用户只交出**模型 + 配置 + 数据集**。

两个类的分工：

- **`TrainingArguments`**：纯配置对象。输出目录、轮数、批大小、日志目录与 `logging_steps`（每多少步记一次损失）都在这里声明。
- **`Trainer`**：执行者。持有模型、配置、`train_dataset`、可选的 `eval_dataset` 与 `compute_metrics` 回调，调用 `trainer.train()` 即开始训练，`trainer.evaluate()` 即评估。

还有一个容易忽略的细节：脚本里 `optimizers=(optim, None)` 把**自己 new 的 Adam 优化器**注入 Trainer，元组第二位 `None` 表示**不用学习率调度器**——与上一讲 GPT-2 微调的 warmup + 线性衰减调度形成对照：这里为了与手写循环严格可比，刻意保持朴素 Adam（`lr=5e-5`）。

#### 4.3.2 核心流程

```text
DistilBertForSequenceClassification.from_pretrained('distilbert-base-uncased')
   │  主体权重 ← 预训练 checkpoint；分类头(pre_classifier/classifier) ← 随机初始化
   ▼
TrainingArguments(output_dir, num_train_epochs, per_device_*_batch_size, logging_*)
   ▼
Trainer(model, args, train_dataset, eval_dataset, compute_metrics, optimizers)
   ▼
trainer.train()   → 每 500 步自动存 checkpoint 到 ./results/checkpoint-N
trainer.evaluate()→ 在 eval_dataset 上算 eval_loss + compute_metrics
```

#### 4.3.3 源码精读

**加载预训练模型**（[ch16/ch16-part3-bert.py:L193-L197](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part3-bert.py#L193-L197)）。notebook 保留了加载时的真实警告：

```text
Some weights ... were not used ...: ['vocab_projector.bias', 'vocab_transform.weight', ...]
Some weights ... were newly initialized: ['pre_classifier.weight', 'classifier.weight', ...]
You should probably TRAIN this model on a down-stream task ...
```

这正是 u16-l2 讲过的「分类头随机初始化」的实锤现场：预训练 checkpoint 里的掩码语言建模头（`vocab_projector` 等）被丢弃不用，序列分类头（`pre_classifier`/`classifier`）是新的、必须训练。上一讲它是组装示例的推断，本讲是仓库真实输出。

**手写循环**（[ch16/ch16-part3-bert.py:L225-L264](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part3-bert.py#L225-L264)）：结构与第 12/13 章的循环一致，唯一的新点是 `outputs = model(input_ids, attention_mask=..., labels=labels)` 之后直接 `loss, logits = outputs['loss'], outputs['logits']`——**损失由模型内部返回**（labels 传进去就算交叉熵），无需外部损失函数。notebook 真实结果：3 轮后训练准确率 99.08%、验证 91.84%（训练远高于验证，过拟合信号）、测试 **92.50%**，总耗时 62.15 分钟。随后 `del model`（[L269](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part3-bert.py#L269)）显存释放，为下一段重新加载干净权重做准备。

**最小配置的第一次 Trainer**（[ch16/ch16-part3-bert.py:L287-L301](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part3-bert.py#L287-L301)）：只传 `model`/`args`/`train_dataset`，没有评估——书中用它演示「Trainer 最少要什么」，此段并未真正训练。

**完整配置的第二次 Trainer**（[ch16/ch16-part3-bert.py:L320-L344](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part3-bert.py#L320-L344)）：

```python
training_args = TrainingArguments(
    output_dir='./results',                    # checkpoint 与日志的落盘目录
    num_train_epochs=3,
    per_device_train_batch_size=16,            # 每卡训练批大小
    per_device_eval_batch_size=16,
    logging_dir='./logs',
    logging_steps=10)                          # 每 10 步记一次训练损失

trainer = Trainer(
    model=model,
    compute_metrics=compute_metrics,           # 评估回调（4.4 节精读）
    args=training_args,
    train_dataset=train_dataset,
    eval_dataset=test_dataset,
    optimizers=(optim, None))                  # 自带优化器、不用调度器

trainer.args._n_gpu = 1   # 强制单 GPU，与手写循环公平对比
```

两点值得指出：① `eval_dataset=test_dataset` 直接把**测试集**挂给了 Trainer——书中的演示选择，工程上应挂 `valid_dataset`（调参看测试集会高估泛化，这正是 u6-l1 讲过的原则）；② `trainer.args._n_gpu = 1` 通过下划线开头的私有属性强制单卡，避免多卡 DataParallel 让耗时对比失真。

**训练与评估**（[ch16/ch16-part3-bert.py:L349-L363](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part3-bert.py#L349-L363)）：`trainer.train()` 一行顶替手写循环约 40 行。notebook 真实输出：`Num examples = 35000`、`Total optimization steps = 6564`（= 每轮 ⌈35000/16⌉ = 2188 步 × 3 轮，与手写循环打印的 `Batch 0000/2188` 完全吻合），每 500 步自动保存 `./results/checkpoint-500/...pytorch_model.bin`，总耗时 **45.36 分钟**（比手写循环快约 17 分钟，主要来自框架对设备搬运与日志的优化）；随后 `trainer.evaluate()` 与 `compute_accuracy` 复核见 4.4 节。

#### 4.3.4 代码实践

**实践目标**：不动任何模型代码，仅凭算术预测 Trainer 的总优化步数，建立「参数 → 步数」的直觉。

**操作步骤**（纯计算，无需运行环境）：

1. 读 [L92](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part3-bert.py#L92) 与 [L323-L330](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part3-bert.py#L323-L330)，记下样本数 35000、`per_device_train_batch_size=16`、`num_train_epochs=3`。
2. 计算每轮步数 = ⌈35000 / 16⌉ = 2188（drop_last=False，向上取整），总步数 = 2188 × 3 = 6564。
3. 与 notebook 中 `Total optimization steps = 6564` 对照。
4. 再做两个思想实验并写下答案：`per_device_train_batch_size` 改 8、轮数改 2 时总步数是多少？（⌈35000/8⌉ × 2 = 4375 × 2 = 8750）

**需要观察的现象**：预测值与日志完全一致；减小批大小使步数**增加**（每步处理的样本变少）。

**预期结果**：6564；思想实验答案 8750。此实践可纸面完成，结论可直接用于第 5 节综合实践的耗时估算。

#### 4.3.5 小练习与答案

1. **练习**：`Trainer` 版与手写循环同数据、同批大小、同优化器同学习率、同轮数，为什么测试准确率仍差了约 0.8 个百分点（92.50% vs 93.27%）？
   **答案**：两者并非同一随机轨迹——数据打乱顺序、权重初始化后的首次 dropout 掩码等随机因素不同（脚本未给 Trainer 重新播种），微调对这种随机性本身就敏感；差异属正常波动，而非框架优劣。
2. **练习**：`optimizers=(optim, None)` 中第二个 `None` 若换成上一讲 GPT-2 用的 `get_linear_schedule_with_warmup` 调度器，对本节的「公平对比」目标有何影响？
   **答案**：引入调度器后学习率随步数衰减，Trainer 的训练动力学不再与固定学习率的手写循环可比，耗时与准确率对比会混入调度器的贡献。
3. **练习**：`logging_steps=10` 与手写循环里的 `if not batch_idx % 250` 各自控制什么？
   **答案**：两者都是「每隔多少步打印一次损失」的采样频率——前者是 Trainer 的配置项，后者是手写的取模条件；采样越密日志越多但不影响训练本身。

### 4.4 compute_metrics 回调与双路评估对照

#### 4.4.1 概念说明

`Trainer` 评估时（`trainer.evaluate()` 或训练中定期评估）内部的动作是：遍历 eval_dataset、收集**全部**样本的模型输出与标签，把 logits 与 labels **堆成 numpy 数组**（已脱离计算图、搬到 CPU），打包成 `EvalPrediction` 对象，然后调用用户注入的 `compute_metrics` 回调。所以回调里的解码是 **numpy 世界**的事：

\[ \text{accuracy} = \frac{1}{N}\sum_{i=1}^{N} \mathbb{1}\!\left[\arg\max_j \text{logits}_{ij} = y_i\right] \]

对二分类，logits 形如 `(N, 2)`，`np.argmax(logits, axis=-1)` 沿最后一维取较大者下标（0 或 1），即预测标签。指标本体由 `datasets` 库的 `load_metric("accuracy")` 提供，`metric.compute(predictions=..., references=...)` 返回 `{'accuracy': 0.9327}` 这样的字典，Trainer 把它逐键并入评估结果。

这与脚本里的 `compute_accuracy` 形成**双路评估**：同一段测试数据、同一个 argmax 解码，一条路走 Trainer 的回调机制，一条路走我们熟悉的 DataLoader 手写循环。两路一致，说明评估本身没有 bug——这是实践中非常值得养成的交叉验证习惯。

#### 4.4.2 核心流程

```text
Trainer.evaluate()
   ├─ 遍历 eval_dataset（no_grad，模型自动 eval 模式）
   ├─ 收集 outputs.logits 与 labels → numpy 数组
   ├─ compute_metrics((logits, labels))
   │      └─ predictions = np.argmax(logits, axis=-1)
   │      └─ metric.compute(...) → {'accuracy': 0.9327}
   ▼
{'eval_loss': 0.3053, 'eval_accuracy': 0.9327, 'eval_runtime': ..., 'epoch': 3.0}
```

#### 4.4.3 源码精读

**定义回调**（[ch16/ch16-part3-bert.py:L309-L315](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part3-bert.py#L309-L315)）：

```python
metric = load_metric("accuracy")

def compute_metrics(eval_pred):
    logits, labels = eval_pred   # logits 是 numpy 数组，不是 torch 张量
    predictions = np.argmax(logits, axis=-1)
    return metric.compute(predictions=predictions, references=labels)
```

四行各自的任务：解包 `EvalPrediction` → 注释强调数据类型是 numpy → 沿最后一维 argmax 解码 → 计算指标并返回字典。作者特意留的那行注释，正是提醒从 PyTorch 循环迁移过来的读者：这里没有 `.to(device)`、没有 `torch.no_grad`，框架已代办。

**触发评估**（[ch16/ch16-part3-bert.py:L356](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part3-bert.py#L356)）：`trainer.evaluate()` 一行，notebook 真实输出：

```text
{'eval_loss': 0.30534815788269043, 'eval_accuracy': 0.9327,
 'eval_runtime': 87.1161, 'eval_samples_per_second': 114.789,
 'eval_steps_per_second': 7.174, 'epoch': 3.0}
```

`eval_loss` 是模型自带的交叉熵（传了 labels 就自动算），`eval_accuracy` 来自我们的回调，`eval_runtime` 等是性能统计。

**第二路：手写 compute_accuracy 复核**（[ch16/ch16-part3-bert.py:L361-L363](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part3-bert.py#L361-L363) 调用，函数定义在 [L204-L220](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part3-bert.py#L204-L220)）：

```python
def compute_accuracy(model, data_loader, device):
    with torch.no_grad():
        correct_pred, num_examples = 0, 0
        for batch_idx, batch in enumerate(data_loader):
            input_ids = batch['input_ids'].to(device)
            attention_mask = batch['attention_mask'].to(device)
            labels = batch['labels'].to(device)
            outputs = model(input_ids, attention_mask=attention_mask)
            logits = outputs['logits']
            predicted_labels = torch.argmax(logits, 1)
            num_examples += labels.size(0)
            correct_pred += (predicted_labels == labels).sum()
        return correct_pred.float()/num_examples * 100
```

注意它**不传 labels 给模型**（评估不需要损失），只取 `logits` 做 `torch.argmax(logits, 1)`——`axis=1` 与回调里的 `axis=-1` 对 `(N, 2)` 的 logits 完全等价。notebook 输出 `Test accuracy: 93.27%`。

**两路对照表**（同一权重、同一 test_dataset）：

| | `compute_metrics`（回调） | `compute_accuracy`（手写） |
| --- | --- | --- |
| 数据来源 | Trainer 内部遍历 `eval_dataset` | 自建 `test_loader` 遍历 |
| 数据类型 | numpy 数组（CPU） | torch 张量（可搬 GPU） |
| 解码 | `np.argmax(logits, axis=-1)` | `torch.argmax(logits, 1)` |
| 输出 | `{'eval_accuracy': 0.9327}` | `93.27`（百分比数值） |
| 适用场景 | 挂进 Trainer 自动化评估 | 任意模型 + DataLoader 的通用复核 |

**结论**：0.9327 与 93.27% 是同一个数字的两种写法——两路一致相互印证了评估正确。

#### 4.4.4 代码实践

**实践目标**：不训练大模型，直接验证「argmax 解码两路等价」这一核心机制。

**操作步骤**（示例代码，纯 numpy/torch 对拍，几秒完成）：

```python
import numpy as np, torch

# 模拟 4 个样本、2 类的 logits（ Mimic Trainer 收集到的 eval_pred ）
rng = np.random.RandomState(0)
logits_np = rng.randn(4, 2).astype(np.float32)
labels_np = np.array([0, 1, 1, 0])

# 路 1：compute_metrics 的解码方式
pred1 = np.argmax(logits_np, axis=-1)
acc1 = (pred1 == labels_np).mean()

# 路 2：compute_accuracy 的解码方式
logits_t = torch.from_numpy(logits_np)
pred2 = torch.argmax(logits_t, 1)
acc2 = (pred2 == torch.from_numpy(labels_np)).float().mean().item()

print(pred1, pred2, acc1, acc2)
assert (pred1 == pred2.numpy()).all() and acc1 == acc2
```

**需要观察的现象**：两种解码逐样本一致，两个准确率严格相等。

**预期结果**：断言通过；随即可以把 `logits_np` 换成 `(N, 5)` 的多分类形状再跑一遍，`axis=-1` 与 `axis=1` 依然等价（最后一维即类维）。待本地验证（不依赖任何下载）。

#### 4.4.5 小练习与答案

1. **练习**：若把回调里的 `np.argmax(logits, axis=-1)` 误写成 `np.argmax(logits)`（不指定轴），会发生什么？
   **答案**：numpy 默认把数组摊平后取全局最大值的**扁平下标**——对 `(N, 2)` 会得到 0 到 2N-1 之间的整数，当作「类别」与标签比较，准确率会跌到接近 0 且不报错，属于典型的静默 bug。
2. **练习**：`compute_metrics` 里为什么不需要 `torch.no_grad()` 和 `.to(device)`？
   **答案**：Trainer 在调用回调之前已完成前向（在 no_grad 与 eval 模式下）、并把结果转成 CPU 上的 numpy 数组；回调拿到的只是纯数值，不参与计算图。
3. **练习**：想让 `trainer.evaluate()` 同时报告 F1，回调怎么改？
   **答案**：再加载 `metric2 = load_metric("f1")`，在 `compute_metrics` 里用它算 F1 并把两个指标**合并进同一个返回字典**（如 `{**metric.compute(...), **metric2.compute(...)}`）——Trainer 会把字典里每个键都并入评估输出（键会自动加 `eval_` 前缀）。

## 5. 综合实践

**任务**：按大纲要求复现 Trainer 训练流程——把 `per_device_train_batch_size` 与 `num_train_epochs` 各调小一档跑通微调，然后对比 `compute_metrics` 输出的准确率与 `compute_accuracy` 在测试 DataLoader 上的结果。

**前置条件**：需要 GPU（本地或 Google Colab，参见 u1-l2 的 Colab 方案）；额外安装脚本要求的 `pip install transformers==4.9.1 datasets==1.11.0`（新版本库需把 `load_metric` 换成 `evaluate.load`，并留意 `TrainingArguments` 参数名的版本差异）。

**操作步骤**（示例代码，基于 [ch16/ch16-part3-bert.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part3-bert.py) 改写；`...` 处照抄原脚本）：

1. 照抄脚本 L87-L186 完成数据下载、划分、分词与 Dataset/DataLoader 构建；算力紧张时可在划分处只取子集（如训练 5000 条、测试 1000 条），只需同步缩小三段切片。
2. 照抄 L278-L280 重新加载预训练模型，并保留 `optim = torch.optim.Adam(model.parameters(), lr=5e-5)`。
3. 「各调小一档」修改 TrainingArguments（对照原 L323-L330）：

   ```python
   training_args = TrainingArguments(
       output_dir='./results',
       num_train_epochs=2,              # 3 → 2
       per_device_train_batch_size=8,   # 16 → 8
       per_device_eval_batch_size=8,
       logging_dir='./logs',
       logging_steps=10)
   ```

4. 组装 Trainer（对照原 L332-L339，含 `compute_metrics`、`eval_dataset=test_dataset`、`optimizers=(optim, None)`），`trainer.train()` 后调用 `trainer.evaluate()`，记下 `eval_accuracy`。
5. 照抄 L361-L363：`model.eval()`、`model.to(DEVICE)`，打印 `compute_accuracy(model, test_loader, DEVICE)`，与上一步对照。

**需要观察的现象**：

- 训练日志中的 `Total optimization steps` 应为你按 4.3.4 方法算出的值（全量数据时为 ⌈35000/8⌉ × 2 = 8750）；
- `./results/` 下每 500 步出现一个 `checkpoint-N` 目录；
- `trainer.evaluate()` 字典中的 `eval_accuracy` 与 `compute_accuracy` 打印的百分比**数值相同**（如 0.9xxx 对 9xxx%）。

**预期结果**：两路准确率一致（同一权重、同一数据、同一 argmax）；绝对值低于 notebook 的 93.27%（轮数与批大小都减小、训练更不充分），且训练总耗时相应缩短。具体数值**待本地验证**（依赖 GPU 环境与模型下载，无法在讲义中代跑）。若两路不一致，优先排查：是否中途又训练过模型、test_loader 与 eval_dataset 是否同一份数据、以及 batch 内 padding 是否影响解码。

## 6. 本讲小结

- 数据管线复用第 8 章已打乱的 IMDb CSV：`requests` 下载 gzip → 流式解压 → `iloc` 切成 35000/5000/10000 三份，顺序切片合法的前提是「行序已打乱」。
- 分词器是预训练模型的一部分：`DistilBertTokenizerFast.from_pretrained('distilbert-base-uncased')` 按名下载配套 WordPiece 词表；`truncation=True` 把影评截到 512 子词上限（notebook 的 `num_tokens=512` 是直接证据）。
- `IMDbDataset.__getitem__` 返回**键名与模型 forward 参数对齐的字典**（`input_ids`/`attention_mask`/`labels`），使 Trainer 自动喂数据、模型在收到 `labels` 时内部算好 loss——训练循环里没有损失函数。
- `Trainer` + `TrainingArguments` 把手写 40 行循环压缩为配置对象加两行调用（`train()`/`evaluate()`），并自动记日志、每 500 步存 checkpoint；`optimizers=(optim, None)` 注入无调度器的 Adam 以保持与手写循环可比；notebook 真实结果 45.36 分钟、测试准确率 93.27%，优于手写循环的 92.50%（随机性导致的正常波动）。
- `compute_metrics` 回调拿到的是 **numpy 化的 `(logits, labels)`**，`np.argmax(logits, axis=-1)` 解码后交给 `load_metric("accuracy")`；它与 `compute_accuracy` 的 `torch.argmax(logits, 1)` 双路对拍结果一致（0.9327 vs 93.27%），是值得保留的评估交叉验证习惯。

## 7. 下一步学习建议

- **仓库内延伸**：浏览 [ch16/bonus-distilbert-lightning-trainer](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/bonus-distilbert-lightning-trainer)，作者把同一个 DistilBERT 微调任务改写成了 Lightning Trainer 版本，正好复习 u13-l3 并体会两个生态的映射关系。
- **对照阅读**：把本讲与 u16-l2（GPT-2 微调）并排重读一次，比较「因果解码器取最后词元隐状态」与「双向编码器取 [CLS] 表示」在分类头设计上的差别。
- **官方文档**：HuggingFace 的 Transformer 任务教程与 Trainer 文档是本讲内容的工程化延续；注意仓库锁定的 `transformers==4.9.1` 较旧，新版本的 `TrainingArguments`（如 `evaluation_strategy`）与 `evaluate` 库接口均有变化，迁移时以官方迁移说明为准。
- **下一讲预告**：第 16 章到此收官。下一单元（u17）进入生成对抗网络（GAN），将首次接触「两个网络对抗训练」的范式——带上本讲「预训练权重 + 随机初始化新头」的经验，你会更容易理解判别器与生成器的博弈。

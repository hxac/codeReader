# 垃圾短信数据集与分类头改造

## 1. 本讲目标

前面五章我们造出了一个能「预测下一个 token」的语言模型，并且在 u5-l4 里把 OpenAI 预训练好的 GPT-2 权重加载进了它。但「会接话」并不等于「会分类」。本讲要完成一次关键的转向：**把一个语言模型改造成一个文本分类器**——具体任务是判断一条短信是不是垃圾短信（spam / ham 二分类）。

学完本讲你应该能够：

1. 从原始的 SMS 垃圾短信数据出发，自己搭出一条「下载 → 格式化 → 平衡 → 划分 → 分词 → 填充」的完整数据流水线，并理解每一步在解决什么问题。
2. 读懂并改写 `SpamDataset`，把长短不一的短信统一成模型能批量处理的定长张量。
3. 把 GPT 模型那个 50257 维的「下一个 token」输出头替换成一个 2 维的分类头。
4. 理解「冻结主干 + 部分解冻」这一微调范式，并亲手统计出改造后到底有多少参数还在被训练。

本讲**只负责把数据和模型头部准备好**；真正用最后一个 token 做 logits、写训练和评估循环，是下一讲 u6-l2 的内容。

## 2. 前置知识

在开始前，请确认你理解下面这些来自前序讲义的概念：

- **语言模型的输出头（u4-l3）**：`GPTModel` 末尾有一个 `out_head = Linear(emb_dim, vocab_size, bias=False)`，它把每个位置的特征向量映射成「词表里每个词的得分」（logits）。对 gpt2-small 来说，`emb_dim=768`、`vocab_size=50257`。
- **因果注意力（u3-l2）**：每个位置只能看到自己和之前的 token。这意味着**序列里最后一个 token 的表示已经聚合了整条短信的信息**——这正是我们做分类时要用的那个位置。
- **加载预训练权重（u5-l4）**：我们已经能把 OpenAI 的 GPT-2 权重无损地灌进自建 `GPTModel`，本讲就在这个「有知识的模型」上动手。
- **`previous_chapters.py` 汇总机制（u1-l3）**：本讲的脚本 `gpt_class_finetune.py` 开头有 `from previous_chapters import GPTModel, load_weights_into_gpt`，属于「依赖模块」型脚本，必须和 `previous_chapters.py` 处于同一目录才能运行。

一个贯穿本讲的直觉：**微调分类 = 复用语言模型学到的大脑（主干），只换/练负责「下判断」的那一小块（输出头）**。预训练给模型的是「读懂语言」的能力，分类头只是把这能力收口成「输出一个类别」。

## 3. 本讲源码地图

本讲几乎全部内容都集中在下面两个文件里：

| 文件 | 作用 |
| --- | --- |
| [ch06/01_main-chapter-code/gpt_class_finetune.py](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch06/01_main-chapter-code/gpt_class_finetune.py) | 第 6 章的汇总脚本（summary），把全章代码串成一个可独立运行的微调流程。本讲引用的核心实现都在这里。 |
| [ch06/01_main-chapter-code/ch06.ipynb](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch06/01_main-chapter-code/ch06.ipynb) | 正文章节 notebook，含逐步演进的过程、模型结构打印、讲解性 markdown，是 `.py` 的「带注释源头」。 |

此外，脚本依赖同目录的 [ch06/01_main-chapter-code/previous_chapters.py](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch06/01_main-chapter-code/previous_chapters.py)（提供 `GPTModel`、`load_weights_into_gpt`）和 [ch06/01_main-chapter-code/gpt_download.py](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch06/01_main-chapter-code/gpt_download.py)（下载预训练权重，u5-l4 已讲）。

脚本顶部的 import 暴露了它的复用关系：

```python
from previous_chapters import GPTModel, load_weights_into_gpt
```

这行 import 是「依赖模块」型脚本的标志（回顾 u1-l3），告诉我们它没有把 `GPTModel` 内联进来，而是直接复用第 4 章攒好的成品。

## 4. 核心概念与源码讲解

### 4.1 SMS 垃圾短信数据集：下载与格式化

#### 4.1.1 概念说明

语言模型预训练时，目标是「预测下一个 token」，不需要人工标注。但分类任务不同——**每条样本必须带一个类别标签**。本讲用的是经典的 **SMS Spam Collection** 数据集：5000 多条真实手机短信，每条标注为 `spam`（垃圾短信）或 `ham`（正常短信）。

原始数据是一个**没有表头、用制表符 `\t` 分隔**的两列文本文件，第一列是标签（`ham`/`spam`），第二列是短信正文。我们要做的第一件事，就是把它下载下来、解压、加上 `.tsv` 后缀、读成 pandas 能处理的 DataFrame。

为什么先讲数据而不是先讲模型？因为微调的本质是「**用一份带标签的小数据，去调整一个已经懂语言的大模型**」。数据质量（标签、分布、长度）直接决定分类效果，所以数据流水线是整个微调流程的地基。

#### 4.1.2 核心流程

```
URL(zip) ──下载──> sms_spam_collection.zip
              └──解压──> SMSSpamCollection (无后缀)
                          └──重命名──> SMSSpamCollection.tsv
                                         └──pd.read_csv(sep="\t")──> DataFrame
                                                                       列: Label, Text
```

读进 DataFrame 后，数据长这样（概念示意）：

| Label | Text |
| --- | --- |
| ham | Go until jurong point, crazy.. |
| spam | Free entry in 2 a wkly comp... |

注意此时 `Label` 还是字符串 `"ham"`/`"spam"`，神经网络要的是数字，这一步转换放在 4.2 节做。

#### 4.1.3 源码精读

下载与解压逻辑封装在 `download_and_unzip_spam_data` 里。它先检查目标文件是否已存在（避免重复下载），再用 `requests` 流式下载、`zipfile` 解压，最后把无后缀的 `SMSSpamCollection` 重命名为带 `.tsv` 的路径：

- [ch06/01_main-chapter-code/gpt_class_finetune.py:26-46](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch06/01_main-chapter-code/gpt_class_finetune.py#L26-L46)：`download_and_unzip_spam_data`。其中第 32-37 行是流式分块写入（`iter_content(chunk_size=8192)`），适合下载较大文件；第 44-45 行把无后缀文件重命名为 `.tsv`。

`__main__` 部分调用它，并且带一个**主备双源**的容错——主 URL 是 UCI 官方地址，失败时回退到作者自建的镜像：

- [ch06/01_main-chapter-code/gpt_class_finetune.py:260-270](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch06/01_main-chapter-code/gpt_class_finetune.py#L260-L270)：用 `try/except (requests.exceptions.RequestException, TimeoutError)` 捕获网络异常，回退到备用 URL。这与 u5-l4 里 `gpt_download.py` 的主备双源思路一致。

下载完成后，用 `sep="\t"` 把制表符文件读成两列，并显式命名：

- [ch06/01_main-chapter-code/gpt_class_finetune.py:272](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch06/01_main-chapter-code/gpt_class_finetune.py#L272)：`df = pd.read_csv(data_file_path, sep="\t", header=None, names=["Label", "Text"])`。`header=None` 因为原文件没有表头，`names=` 手动给两列起名。

> 小提示：如果你已经手动下好了数据并放成 `sms_spam_collection/SMSSpamCollection.tsv`，函数第 27-29 行的存在性检查会直接跳过下载。

#### 4.1.4 代码实践

**实践目标**：跑通数据下载与读取，亲眼看到「标签 + 正文」两列结构，并统计两类样本的原始数量。

**操作步骤**：

1. 确认已安装依赖：`pip install pandas requests`（这两个在 `requirements.txt` 里，跟着 ch06）。
2. 在 `ch06/01_main-chapter-code/` 目录下，新建一个临时脚本，复用脚本里的下载函数：

```python
# 示例代码：只做数据下载与查看，不碰模型
from pathlib import Path
import pandas as pd
from gpt_class_finetune import download_and_unzip_spam_data

url = "https://archive.ics.uci.edu/static/public/228/sms+spam+collection.zip"
data_file_path = Path("sms_spam_collection") / "SMSSpamCollection.tsv"
download_and_unzip_spam_data(url, "sms_spam_collection.zip",
                             "sms_spam_collection", data_file_path)

df = pd.read_csv(data_file_path, sep="\t", header=None, names=["Label", "Text"])
print(df.head())
print(df["Label"].value_counts())
```

**需要观察的现象**：控制台先打印前 5 行（每行一个标签加一段短信），再打印两类计数。

**预期结果**：原始数据约 **4825 条 ham**、**747 条 spam**（共 5572 条）。你会发现两类极度不平衡——这正是下一节要解决的问题。

> 如果网络不通导致下载失败：可改用脚本里的备用 URL（backblazeb2 镜像），或手动下载后放到 `sms_spam_collection/SMSSpamCollection.tsv`。精确的下载耗时取决于网络，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `pd.read_csv` 这里要传 `sep="\t"` 而不是用默认的逗号？

> **参考答案**：SMS Spam Collection 原始文件用制表符分隔两列、且没有表头。若用默认逗号，pandas 会把整行当成一列，且把第一行数据当表头，导致解析错误。`sep="\t"` + `header=None` 才能正确切出 Label / Text 两列。

**练习 2**：下载函数第 27-29 行的「文件已存在则跳过」判断，有什么实际好处？

> **参考答案**：避免每次重跑脚本都重新下载、解压，节省时间和带宽；同时避免覆盖已经手工修好的本地数据。这是数据处理脚本里常见的幂等设计。

---

### 4.2 类别平衡与训练/验证/测试划分

#### 4.2.1 概念说明

4.1 节最后我们看到：ham 有 4825 条、spam 只有 747 条，比例约 **6.5 : 1**。如果直接拿这个分布去训练，模型只要**无脑全部猜 ham**，就能拿到约 87% 的准确率——但它一条垃圾短信都识别不出来。这种「学到了偷懒策略」的模型在类别极不平衡的数据上非常常见。

解决办法是**下采样平衡**：从多数类（ham）里随机抽出和少数类（spam）一样多的样本，让两类数量相等。代价是扔掉了大量 ham 样本，但换来的是模型必须真正学会区分两类，而不是靠先验比例作弊。

平衡之后，还要把数据切成三份：

- **训练集（train）**：用来更新权重。
- **验证集（validation）**：训练过程中监控，用来调超参、判断过拟合。
- **测试集（test）**：训练完全结束后只用一次，给出最终性能的客观估计。

一个常见比例是 70% / 10% / 20%（test 是剩下的余数）。

#### 4.2.2 核心流程

```
不平衡 df (ham=4825, spam=747)
   │  create_balanced_dataset: 随机抽 747 条 ham
   ▼
平衡 df (ham=747, spam=747) ── 总计 1494
   │  Label 字符串映射: ham→0, spam→1
   ▼
随机打乱 + 按比例切片
   ├── 70% → train_df  ──写──> train.csv
   ├── 10% → validation_df ──写──> validation.csv
   └── 20% → test_df    ──写──> test.csv
```

打乱时固定 `random_state=123`，保证每次切分结果一致、可复现。把三份分别写成 CSV，是为了让后续 `SpamDataset` 能像读普通文件一样读它们。

#### 4.2.3 源码精读

`create_balanced_dataset` 的逻辑非常直白：先数 spam 有多少条，再从 ham 里抽样同样的数量，最后拼回去：

- [ch06/01_main-chapter-code/gpt_class_finetune.py:49-59](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch06/01_main-chapter-code/gpt_class_finetune.py#L49-L59)：第 51 行统计 spam 数量，第 54 行用 `.sample(num_spam, random_state=123)` 从 ham 里抽样，第 57 行 `pd.concat` 把抽出的 ham 子集和全部 spam 拼成平衡集。

`random_split` 负责三划分：先整体打乱，再按比例算出两个切点，切片成三段：

- [ch06/01_main-chapter-code/gpt_class_finetune.py:62-75](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch06/01_main-chapter-code/gpt_class_finetune.py#L62-L75)：第 64 行 `df.sample(frac=1, random_state=123).reset_index(drop=True)` 打乱并重置索引；第 67-68 行用 `int(len(df) * 比例)` 算切点；第 71-73 行切成 train / validation / test 三段。注意 test 的大小是「剩下的余数」，所以函数只接收 `train_frac` 和 `validation_frac` 两个比例。

最后，在 `__main__` 里把平衡、标签数字化、切分、落盘串起来：

- [ch06/01_main-chapter-code/gpt_class_finetune.py:273-279](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch06/01_main-chapter-code/gpt_class_finetune.py#L273-L279)：第 273 行平衡，第 274 行 `balanced_df["Label"].map({"ham": 0, "spam": 1})` 把字符串标签映射成 0/1 整数，第 276 行三划分，第 277-279 行分别写盘。

> 顺序很关键：必须**先平衡（用字符串匹配 `"spam"`）、再映射成数字**。如果把映射放到平衡之前，`create_balanced_dataset` 里的 `df["Label"] == "spam"` 就匹配不到了。

平衡后总样本数可用一个简单公式刻画。设原始 spam 数为 \(S\)、ham 数为 \(H\)（且 \(H > S\)），则平衡集大小为：

\[
N_{\text{balanced}} = S + S = 2S
\]

对本数据集 \(S=747\)，故 \(N_{\text{balanced}} = 1494\)。再按 0.7 / 0.1 / 0.2 切分，训练集约 1045 条、验证集约 149 条、测试集约 300 条（均为 `int` 取整后的确定性结果）。

#### 4.2.4 代码实践

**实践目标**：亲手做出平衡数据集，验证两类数量相等，并查看三份 CSV 的行数。

**操作步骤**：

```python
# 示例代码：接 4.1 节已读出的 df
from gpt_class_finetune import create_balanced_dataset, random_split

balanced_df = create_balanced_dataset(df)
print("平衡后:", balanced_df["Label"].value_counts().to_dict())   # 应为 ham 747, spam 747

balanced_df["Label"] = balanced_df["Label"].map({"ham": 0, "spam": 1})
train_df, val_df, test_df = random_split(balanced_df, 0.7, 0.1)
print(len(train_df), len(val_df), len(test_df))   # 预期约 1045 / 149 / 300
```

**需要观察的现象**：平衡后 `value_counts` 显示两类各 747；切分后三段长度之和等于 1494。

**预期结果**：`{'ham': 747, 'spam': 747}`，三份长度约为 1045 / 149 / 300。

#### 4.2.5 小练习与答案

**练习 1**：除了「下采样多数类」，还有哪些处理类别不平衡的方法？各自的代价是什么？

> **参考答案**：①**上采样少数类**（复制 spam 样本），代价是容易对少数类过拟合；②**加权损失**（给 spam 更大的损失权重），代价是要调权重、可能让训练不稳定；③**不动数据，换评估指标**（看 F1 / AUC 而非准确率）。本讲选下采样，是因为数据量本身就小、且实现最简单直观。

**练习 2**：`random_split` 里为什么用 `int(len(df) * 0.7)` 而不是 `round(...)`？

> **参考答案**：`int()` 是向下取整，保证切点不会越界；并且三段用「train_end + int(len*val_frac)」连环取整，test 取余数，三段相加严格等于 `len(df)`、不会重叠也不会漏。若用 `round` 可能因四舍五入让切点错位，造成段与段之间出现重叠或缝隙。

---

### 4.3 SpamDataset：分词、截断与统一填充

#### 4.3.1 概念说明

经过 4.2 节，我们有了三份带数字标签的 CSV。但模型还吃不了它们——神经网络要的是**形状整齐的张量**，而短信有长有短：有的几个词，有的一大段。一个 batch 里的多条短信必须能拼成同一个 `(batch_size, max_length)` 的矩阵。

`SpamDataset` 这个 `torch.utils.data.Dataset` 子类就负责把每条短信变成定长的 token ID 序列，它做三件事：

1. **分词（tokenize）**：用 u2-l2 学过的 GPT-2 BPE 分词器（`tiktoken.get_encoding("gpt2")`）把文本切成 token ID。
2. **截断（truncate）**：如果指定了 `max_length`，超出部分一刀切掉，防止超过模型 `context_length`。
3. **填充（pad）**：把所有序列补到同一长度（默认补到最长序列，用 `pad_token_id=50256`，即 `<|endoftext|>`）。

回顾 u2-l3 的滑动窗口：那里是为了从一篇长文里造出大量「上下文→下一个词」的训练对；这里不同——每条短信**本身就是一条独立样本**，标签是整条短信的类别，不是某个位置的下一个词。所以这里不需要滑动窗口，而是「一条短信 = 一个样本」。

#### 4.3.2 核心流程

```
train.csv ──pd.read_csv──> data (Label, Text 两列)
   │
   ├── 对每条 Text 用 tokenizer.encode ──> encoded_texts (长短不一的 ID 列表)
   │
   ├── 定 max_length:
   │     · max_length=None → 取所有序列里最长的 (训练集)
   │     · max_length=给定 → 截断超长序列 (验证/测试集，沿用训练集长度)
   │
   ├── 对每条短序列补 pad_token_id 到 max_length
   │
   ▼
__getitem__(i) → ( torch.tensor(encoded_i), torch.tensor(label_i) )
                  (形状 max_length,)          (标量)
```

一个关键约定：**训练集用 `max_length=None`（自适应最长），验证集和测试集都传 `train_dataset.max_length`**。这样三份的序列长度完全一致，且测试时不会因为偶然出现超长样本而越界。

#### 4.3.3 源码精读

`SpamDataset` 的全貌：

- [ch06/01_main-chapter-code/gpt_class_finetune.py:78-123](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch06/01_main-chapter-code/gpt_class_finetune.py#L78-L123)：整个类定义。逐段看：

  - 第 80 行 `pd.read_csv(csv_file)` 读 CSV。
  - 第 83-85 行**预先**把所有文本分词成 ID 列表（在 `__init__` 里一次性做完，而不是每次 `__getitem__` 现切，提速明显）。
  - 第 87-95 行决定 `max_length`：`None` 时调用 `_longest_encoded_length()`，否则截断 `encoded_text[:self.max_length]`。
  - 第 98-101 行**填充**：`encoded_text + [pad_token_id] * (self.max_length - len(encoded_text))`，把每条补齐到 `max_length`。
  - 第 103-109 行 `__getitem__` 返回 `(input_tensor, label_tensor)`，label 直接从 DataFrame 取（已是 0/1 整数）。
  - 第 114-120 行 `_longest_encoded_length` 遍历所有序列找最长长度。

`__main__` 里构建三个数据集时，验证/测试集显式复用训练集的长度：

- [ch06/01_main-chapter-code/gpt_class_finetune.py:286-302](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch06/01_main-chapter-code/gpt_class_finetune.py#L286-L302)：`train_dataset` 传 `max_length=None`，`val_dataset` / `test_dataset` 都传 `max_length=train_dataset.max_length`。

随后用 `DataLoader` 包成可迭代的 batch（第 309-329 行）：训练集 `shuffle=True, drop_last=True`（打乱 + 丢弃不整的末尾 batch），验证/测试集 `drop_last=False`（保留全部样本用于评估）。

> 一个细节：填充用的是 `50256`，也就是 GPT-2 的 `<|endoftext|>`。本讲分类时这些 padding 会被「忽略」——因为我们只取最后一个 token 的 logits（见 u6-l2）。由于因果掩码，最后一个有效 token 之后堆叠的 padding 其实不会污染我们对最后一个有效位置的判断；但更严谨的做法是用 `ignore_index` 掩蔽 padding（第 7 章指令微调会这么做）。

#### 4.3.4 代码实践

**实践目标**：构建 `SpamDataset`，验证填充把短序列补到了统一长度，并观察一个 batch 的形状。

**操作步骤**：

```python
# 示例代码：假设 4.2 节已写出 train.csv / validation.csv / test.csv
import tiktoken
from torch.utils.data import DataLoader
from gpt_class_finetune import SpamDataset

tokenizer = tiktoken.get_encoding("gpt2")
train_dataset = SpamDataset(csv_file="train.csv", max_length=None, tokenizer=tokenizer)
print("max_length =", train_dataset.max_length)

x, y = train_dataset[0]
print("单条 input 形状:", x.shape, " label:", y.item())

val_dataset = SpamDataset(csv_file="validation.csv",
                          max_length=train_dataset.max_length, tokenizer=tokenizer)

loader = DataLoader(train_dataset, batch_size=8, shuffle=True, drop_last=True)
xb, yb = next(iter(loader))
print("一个 batch:", xb.shape, yb.shape)   # 预期 torch.Size([8, max_length]) torch.Size([8])
```

**需要观察的现象**：`max_length` 是一个正整数（训练集最长短信的 token 数）；单条样本是一维定长向量；一个 batch 是 `(8, max_length)`。

**预期结果**：所有样本长度相同，batch 形状为 `(8, train_dataset.max_length)`。若 `max_length` 超过模型 `context_length=1024`，会在后续触发断言报错（见 4.4 节那条 `assert`）。

#### 4.3.5 小练习与答案

**练习 1**：为什么分词放在 `__init__` 里一次性做完，而不是放在 `__getitem__` 里每次现切？

> **参考答案**：`__getitem__` 在训练时每个 epoch、每个 batch 都会被高频调用。如果每次都重新 `tokenizer.encode`，会反复做同样的工作，拖慢训练。在 `__init__` 里预分词一次、把结果存进列表，是典型的「空间换时间」优化。

**练习 2**：如果把验证集也用 `max_length=None` 构建，会有什么隐患？

> **参考答案**：验证集会按**自己**最长样本定长度，可能和训练集长度不一致，导致同一 batch 内（或与训练集对比时）形状对不上；更糟的是若验证集里碰巧有一条超长短信，`max_length` 会膨胀，可能超过模型 `context_length` 而在推理时越界。统一沿用 `train_dataset.max_length` 才能保证三份对齐。

---

### 4.4 改造输出头：替换 out_head、冻结主干与部分解冻

#### 4.4.1 概念说明

数据和 DataLoader 都就绪了，现在改造模型本身。回顾 u4-l3：`GPTModel` 末尾的 `out_head` 是 `Linear(768, 50257, bias=False)`——它输出的是「词表里每个词的得分」，为的是预测下一个 token。但我们要做的是**二分类**，只需要两个数：一个是 ham 的得分、一个是 spam 的得分。

所以改造分三步：

1. **冻结整个模型**：把所有参数的 `requires_grad` 置为 `False`，让主干（embedding + 12 个 Transformer 块 + final_norm）在反向传播时**不被更新**，保住预训练学到的语言知识。
2. **换输出头**：把 `out_head` 替换成 `Linear(emb_dim, 2)`（默认带 bias）。新建的层 `requires_grad=True`，是默认可训练的——它要从随机初始化开始学「怎么把 768 维特征变成 2 类得分」。
3. **部分解冻**：除了新输出头，再把**最后一个 Transformer 块**和 **final_norm** 也设为可训练。作者的实践经验表明，多练这几层能明显提升分类效果，而代价（多一点点可训练参数）远小于全量微调。

这套「冻结 + 换头 + 选择性解冻」是参数高效微调的经典范式，和后面附录 E 的 LoRA（u11-l1）思路一脉相承：**别动大脑主体，只精调离输出最近的部分**。

#### 4.4.2 核心流程

```
加载好预训练权重的 GPTModel (out_head: 768→50257)
   │
   ├── for param in model.parameters(): param.requires_grad = False   # 全冻结
   │
   ├── model.out_head = Linear(emb_dim, 2)                            # 换头(默认可训练)
   │
   ├── model.trf_blocks[-1]  → requires_grad = True                  # 解冻最后一块
   ├── model.final_norm      → requires_grad = True                  # 解冻最终归一化
   │
   ▼
推理时: model(input)[:, -1, :]  → 取最后一个 token 的 2 维 logits 做分类
```

为什么取**最后一个 token**？因为因果注意力下，最后一个位置看过了整条短信（包括所有 padding 之前的有效内容），它的 768 维表示是对整条短信最完整的「摘要」。把它映射成 2 维 logits，就是这条短信属于两类的得分。（具体的损失与训练在 u6-l2 讲。）

#### 4.4.3 源码精读

冻结、换头、解冻这三步在 `__main__` 里是连贯的一段：

- [ch06/01_main-chapter-code/gpt_class_finetune.py:390-403](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch06/01_main-chapter-code/gpt_class_finetune.py#L390-L403)：逐行解读：
  - 第 390-391 行 `for param in model.parameters(): param.requires_grad = False` —— **全模型冻结**。
  - 第 393 行 `torch.manual_seed(123)` —— 固定新输出头的随机初始化，保证可复现。
  - 第 395-396 行 `num_classes = 2; model.out_head = torch.nn.Linear(in_features=BASE_CONFIG["emb_dim"], out_features=num_classes)` —— **替换输出头**为新分类头。注意新层默认 `bias=True`，与原 `out_head`（`bias=False`）不同。
  - 第 397 行 `model.to(device)` —— 把改造后的模型搬到 GPU/CPU。
  - 第 399-400 行解冻**最后一个 Transformer 块**（`trf_blocks[-1]`）。
  - 第 402-403 行解冻 **final_norm**。

理解 `BASE_CONFIG`（模型的配置卡，回顾 u4-l3 的 `GPT_CONFIG_124M`）：

- [ch06/01_main-chapter-code/gpt_class_finetune.py:355-369](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch06/01_main-chapter-code/gpt_class_finetune.py#L355-L369)：`BASE_CONFIG` 给出公共配置（`vocab_size=50257`、`context_length=1024`、`drop_rate=0.0`、`qkv_bias=True`），再用 `model_configs[CHOOSE_MODEL]` 补上选定型号的 `emb_dim/n_layers/n_heads`。本讲用 `gpt2-small (124M)`，故 `emb_dim=768`。注意这里 `drop_rate=0.0`（微调时关掉 dropout）。

数据长度与模型上下文长度的安全检查：

- [ch06/01_main-chapter-code/gpt_class_finetune.py:371-375](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch06/01_main-chapter-code/gpt_class_finetune.py#L371-L375)：`assert train_dataset.max_length <= BASE_CONFIG["context_length"]`，保证填充后的序列不会超过位置嵌入能覆盖的 1024 长度。

notebook 里对应的讲解性文字（确认这就是全章的改造意图）：

- [ch06/01_main-chapter-code/ch06.ipynb:1511-1538](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch06/01_main-chapter-code/ch06.ipynb)（markdown 单元，约 1512-1537 行）：明确说明「先冻结模型 → 替换输出头为 2 类 → 替换层默认可训练」。
- [ch06/01_main-chapter-code/ch06.ipynb:1578-1585](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch06/01_main-chapter-code/ch06.ipynb)（代码单元，约 1580-1584 行）：解冻最后一块和 final_norm 的代码，与 `.py` 第 399-403 行一致。

改造后可训练参数的来源可以用一个比例式概括。设主干总参数为 \(P_{\text{backbone}}\)，最后一块约为 \(P_{\text{last}}\)，新分类头为 \(P_{\text{head}} = 768 \times 2 + 2 = 1538\)，final_norm 为 \(2 \times 768\)。可训练参数量为：

\[
P_{\text{trainable}} \approx P_{\text{last}} + P_{\text{head}} + 2 \times 768
\]

对 gpt2-small，\(P_{\text{last}}\) 约 7 百万量级，而 \(P_{\text{head}}\) 仅约 1500——所以**可训练参数的大头来自被解冻的最后一个 Transformer 块**，新分类头本身几乎可以忽略。这正体现了「只精调末段、不动主体」的高效性。（精确数值待本地验证，见下方实践。）

#### 4.4.4 代码实践

**实践目标**：把预训练模型的 `out_head` 替换成 2 类分类头，做「冻结 + 部分解冻」，并亲手统计出改造后**还有多少参数在训练**。

**操作步骤**（推荐用脚本自带的 `--test_mode`，它用一个小模型跑在 CPU 上、**无需下载 GPT-2 权重**，最适合快速验证改造逻辑）：

1. 在 `ch06/01_main-chapter-code/` 目录下运行：

```bash
python gpt_class_finetune.py --test_mode
```

   `--test_mode` 会构建一个迷你 GPT（`emb_dim=12, n_layers=1, n_heads=2, context_length=120`，见 `.py` 第 336-348 行），跳过权重下载，直接走完「冻结 → 换头 → 解冻 → 训练」全流程。注意它仍会下载 SMS 数据集（体积很小）。

2. 想单独只看「换头 + 冻结 + 统计参数」这一段，可在自己的临时脚本里这样写：

```python
# 示例代码：只验证头部改造与可训练参数统计（用迷你模型，免下载）
import torch
from previous_chapters import GPTModel

BASE_CONFIG = {
    "vocab_size": 50257, "context_length": 120, "drop_rate": 0.0,
    "qkv_bias": False, "emb_dim": 12, "n_layers": 1, "n_heads": 2,
}
torch.manual_seed(123)
model = GPTModel(BASE_CONFIG)

# 1) 全冻结
for param in model.parameters():
    param.requires_grad = False

# 2) 换成 2 类分类头（默认可训练）
num_classes = 2
model.out_head = torch.nn.Linear(in_features=BASE_CONFIG["emb_dim"], out_features=num_classes)

# 3) 解冻最后一块 + final_norm
for param in model.trf_blocks[-1].parameters():
    param.requires_grad = True
for param in model.final_norm.parameters():
    param.requires_grad = True

# 4) 统计可训练参数
total_params = sum(p.numel() for p in model.parameters())
trainable_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
print(f"可训练参数: {trainable_params:,} / 总参数: {total_params:,} "
      f"({trainable_params/total_params:.2%})")
```

**需要观察的现象**：`out_head` 的 `out_features` 从原来的 `vocab_size` 变成了 `2`；可训练参数远小于总参数。

**预期结果**：
- 迷你模型总参数约 60 万级（小模型，仅用于验证流程），可训练占比应该在较小但非零的范围内——具体说是「最后一块 + final_norm + 新分类头」之和。
- 换成真正的 gpt2-small（去掉 `--test_mode`）时，**总参数约 124M**，可训练参数主要集中在最后一个 Transformer 块（约 7 百万级），新分类头仅约 1538 个。精确数字**待本地验证**（取决于能否下载并加载 OpenAI 权重）。

> 为什么统计式 `sum(p.numel() for p in model.parameters() if p.requires_grad)` 能 work？因为 `model.parameters()` 会递归遍历所有子模块的参数，`requires_grad` 正是我们刚才用三步改造设的开关——把它们累加起来就是「还会被训练的参数量」。

#### 4.4.5 小练习与答案

**练习 1**：如果把第 3 步「解冻最后一块 + final_norm」去掉，只训练新 `out_head`，理论上还能分类吗？为什么作者仍选择多解冻这两层？

> **参考答案**：理论上能——只训练 `out_head` 等价于在冻结的预训练特征上训练一个线性分类器（类似线性探测 / linear probing），对简单任务往往已经够用。但作者实践发现，**额外微调最后一块和 final_norm 能让特征更好地适配分类目标**，显著提升效果；而代价只是多了少量可训练参数，比全量微调仍高效得多。

**练习 2**：新 `out_head` 是 `Linear(768, 2)`（带 bias），而原来的 `out_head` 是 `Linear(768, 50257, bias=False)`。这个 bias 的差异重要吗？

> **参考答案**：在本讲场景下影响很小但不是零。分类头带 bias 给两类各加一个可学习偏置，能微调决策阈值；原始语言模型输出头不带 bias，是为了与 token embedding 的权重共享（weight tying，回顾 u5-l4）保持一致。分类头不再做权重共享，所以带 bias 是合理且常见的默认选择。

**练习 3**：为什么推理时取 `model(input)[:, -1, :]`（最后一个 token）而不是取平均或取第一个 token？

> **参考答案**：因果注意力保证最后一个位置已经「看到」了序列里它之前的全部 token，是对整条短信信息聚合得最充分的位置。取第一个 token 它只看到了自己；取平均会混入 padding 位置（本讲未掩蔽 padding）的表示。所以最后一个 token 是当前设置下最干净的「整句摘要」。（padding 的更严谨处理见第 7 章。）

## 5. 综合实践

把本讲四个模块串起来，完成一个**端到端的「数据 + 改造」准备任务**（不含训练，训练留给 u6-l2）：

1. **数据**：运行 `download_and_unzip_spam_data` 下载 SMS 数据；用 `create_balanced_dataset` 平衡成 ham/spam 各 747 条；用 `.map({"ham":0,"spam":1})` 转数字标签；用 `random_split(balanced_df, 0.7, 0.1)` 切成三份并落盘。
2. **Dataset**：用 `tiktoken.get_encoding("gpt2")` 构建三个 `SpamDataset`（训练集 `max_length=None`，验证/测试集沿用 `train_dataset.max_length`），打印 `max_length`。
3. **模型改造**：构建（或加载）一个 `GPTModel`，执行「全冻结 → 替换 `out_head` 为 `Linear(emb_dim, 2)` → 解冻 `trf_blocks[-1]` 与 `final_norm`」。
4. **自检**：
   - 统计并打印可训练参数占比。
   - 取一个 batch，跑一次前向 `model(xb)[:, -1, :]`，确认输出形状是 `(batch_size, 2)`。
   - 用 `assert train_dataset.max_length <= BASE_CONFIG["context_length"]` 确认不会越界。

完成自检后，你的模型已经「换好分类脑、备好数据」，下一讲 u6-l2 就能直接接上训练循环和准确率评估。

> 如果不想下载 GPT-2 权重，可全程用 `--test_mode` 的迷你配置完成本综合实践；若要贴近真实场景，去掉 `--test_mode` 走 gpt2-small，相关耗时与显存占用**待本地验证**。

## 6. 本讲小结

- **分类任务的起点是带标签数据**：我们从 SMS Spam Collection 出发，下载、解压、读成 `Label/Text` 两列的 DataFrame。
- **类别不平衡要先处理**：`create_balanced_dataset` 把 ham 下采样到和 spam 一样多（各 747），避免模型靠「全猜 ham」作弊；`random_split` 按 0.7/0.1/0.2 切成训练/验证/测试。
- **`SpamDataset` 把长短不一的短信统一成定长张量**：预分词 → 定 `max_length` → 截断/填充（`pad_token_id=50256`），一条短信对应一个样本。
- **微调的核心是「换头 + 冻结 + 部分解冻」**：全模型冻结保住预训练知识，把 50257 维输出头换成 2 维分类头，再解冻最后一个 Transformer 块和 final_norm 以提升效果。
- **可训练参数的大头来自被解冻的最后一个 Transformer 块**，新分类头本身（约 1538 个参数）几乎可忽略——这正是参数高效微调的体现。
- **分类用最后一个 token 的 logits**，因为因果注意力下它聚合了整条短信的信息；具体的损失与训练循环见 u6-l2。

## 7. 下一步学习建议

本讲只完成了「数据和模型头部」的准备，模型还**没有被训练**。下一讲 **u6-l2 分类训练与评估（最后一个 token）** 将直接接续：

- 读 [ch06/01_main-chapter-code/gpt_class_finetune.py](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch06/01_main-chapter-code/gpt_class_finetune.py) 里的 `calc_loss_batch`（取 `[:, -1, :]` 后算交叉熵）、`calc_accuracy_loader`、`train_classifier_simple`，理解如何用最后一个 token 的 2 维 logits 算损失与准确率。
- 跑完 5 个 epoch 的微调，观察训练/验证损失与准确率曲线，最终在测试集上评估。

进阶方向（后续单元）：
- 想了解更激进的参数高效微调，去看 **u11-l1 LoRA**（附录 E），用低秩适配器实现「几乎不动原模型」的微调。
- 想理解 padding 的严谨掩蔽处理，去看 **u7-l1 指令数据集与自定义 collate 掩码**（`ignore_index=-100`）。

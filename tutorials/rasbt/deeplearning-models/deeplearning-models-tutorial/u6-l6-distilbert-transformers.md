# u6.6 Transformer 实战：DistilBERT 特征提取与微调

## 1. 本讲目标

本讲是序列建模单元的收官：放下「从零训练小模型」的思路，学习如何把一个在海量文本上预训练好的 Transformer——DistilBERT——迁移到 IMDb 影评情感分类任务上。学完后你应该能：

1. 会用 Hugging Face `transformers` 加载 DistilBERT 模型与分词器，理解「分词 → 数值化 → attention mask」这条输入管线，并能与 u6-l1 手工搭建的词表路线对比。
2. 掌握**特征提取路线**：冻结预训练模型，只取 `[CLS]` 嵌入向量，交给随机森林 / 逻辑回归等传统分类器。
3. 掌握**端到端微调路线**的两种实现：HF `Trainer` 高层 API（含学习率与默认线性衰减调度）与 PyTorch Lightning 手写训练循环（承接 u8 前置的 LightningModule 风格）。
4. 能复现仓库自带的 `distilbert-benchmark` 基准脚本思路，对两条路线做「准确率 / 显存 / 训练时间」三维对比。

本讲三份 notebook 的关键实验结果（作者保存的输出）：特征提取 + 逻辑回归测试准确率 **87.94%**；端到端微调测试准确率 **92.6%**（HF Trainer 版与 Lightning 版几乎完全一致）。

## 2. 前置知识

### 2.1 从「训练自己的词向量」到「借用整个预训练编码器」

回顾 u6-l1：我们自己在训练集上统计词频、建 20002 项词表、把 Embedding 当作可学习参数从零训练，再用 `nn.RNN` 建模序列——结果简单 RNN 因梯度消失失败（约 46% 准确率），LSTM 也只到 85% 左右。u6-l3 引入 GloVe 预训练向量时已经出现「借知识」的雏形，但当时的编码器仍然是自己训练的。

本讲更进一步：**整个编码器都在别处训练好了**。DistilBERT 在大规模语料上用「掩码语言建模 + 句子关系学习」两个自监督目标预训练（背景知识，非本仓库内容），我们只负责把它接到情感分类任务上。

**DistilBERT 小档案**（背景知识）：它是 BERT-base 的蒸馏版，6 层 Transformer、隐藏维 768、12 个注意力头、约 6600 万参数，比 BERT-base 小 40%、快 60%，保留了约 97% 的语言理解能力。仓库所有 notebook 用的检查点名都是 `distilbert-base-uncased`（uncased = 全部转小写）。

### 2.2 两条迁移路线

| | 特征提取路线（feature extraction） | 端到端微调路线（fine-tuning） |
|---|---|---|
| 预训练模型角色 | 冻结的「特征提取器」 | 参与训练的可微分层 |
| 训练什么 | 只训练接在后面的分类器 | 分类头 + Transformer 全部参数 |
| 分类器 | sklearn 随机森林 / 逻辑回归 | 神经网络分类头（`pre_classifier` + `classifier`） |
| 需要 GPU 吗 | 提取特征时最好有；训练分类器时不用 | 全程需要 |
| 本讲测试准确率 | 83.24%（RF）/ 87.94%（LogReg） | 92.60% |

这个划分与 u7-l4 的迁移学习（`requires_grad=False` 冻结）思想同源，只是那里冻结的是卷积特征提取器，这里冻结的是文本编码器。

### 2.3 需要认识的三个库

- **`transformers`**：Hugging Face 的模型库，`AutoTokenizer` / `AutoModel` / `AutoModelForSequenceClassification` / `Trainer` 都来自它。
- **`datasets`**：Hugging Face 的数据集库，`load_dataset` 既可以从 Hub 拉 IMDb，也能读本地 CSV；`.map()` 可以对整个数据集做批量预处理并缓存。
- **`lightning`**：u8 单元会系统学习的训练框架，本讲 Lightning 版 notebook 是它的预热。

三份 notebook 记录的环境均为 torch 1.12.1 + transformers 4.23.1 + datasets 2.6.1（Lightning 版再加 lightning 2022.10.20），与当前新版库的 API 有少量差异，文中会逐一标注。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [pytorch_ipynb/transformer/1_distilbert-as-feature-extractor.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/transformer/1_distilbert-as-feature-extractor.ipynb) | 特征提取路线：冻结 DistilBERT 提取 `[CLS]` 嵌入，训练随机森林与逻辑回归 |
| [pytorch_ipynb/transformer/distilbert-hf-finetuning.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/transformer/distilbert-hf-finetuning.ipynb) | 端到端微调路线之一：`AutoModelForSequenceClassification` + HF `Trainer` |
| [pytorch-lightning_ipynb/transformer/distilbert-finetuning-ii.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/transformer/distilbert-finetuning-ii.ipynb) | 端到端微调路线之二：LightningModule 包装 HF 模型 + `Trainer.fit` |
| [pytorch_ipynb/transformer/distilbert-benchmark/1_just-hf.py](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/transformer/distilbert-benchmark/1_just-hf.py) | 仓库自带的基准脚本：HF Trainer 路线，显式测量训练/推理耗时 |
| [pytorch_ipynb/transformer/distilbert-benchmark/shared.py](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/transformer/distilbert-benchmark/shared.py) | 基准脚本的公共部分：下载 IMDb 压缩包并生成 train/validation/test 三份 CSV |

三份 notebook 的数据准备部分完全同构：下载 `aclImdb_v1.tar.gz` → 50 000 条影评转成 DataFrame → 打乱后切成 35 000 / 5 000 / 10 000 三份存 CSV（[1_distilbert-as-feature-extractor.ipynb:L445-L453](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/transformer/1_distilbert-as-feature-extractor.ipynb#L445-L453) 做切分并写 CSV），再用 `load_dataset("csv", data_files=...)` 读回（[1_distilbert-as-feature-extractor.ipynb:L636-L645](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/transformer/1_distilbert-as-feature-extractor.ipynb#L636-L645)）。切分逻辑与 `shared.py` 中的 [prepare_data](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/transformer/distilbert-benchmark/shared.py#L40-L83) 一致。数据集词数统计为 min=4、median=173、max=2470，类别完全均衡（25 000 正 / 25 000 负）。

## 4. 核心概念与源码讲解

### 4.1 分词与数值化：把影评变成模型能吃的张量

#### 4.1.1 概念说明

神经网络只能吃整数张量。u6-l1 的做法是「按空白切词 + 按频率建词表」，有两个固有缺陷：词表里放不下的词一律变 `<unk>`（例：拼错的 "flm" 直接失联）；词表大小与覆盖率互相牵制。

预训练模型走 **子词分词（subword tokenization）** 路线：高频整词保留为独立词元，低频词拆成更小的片段（例：`unfilmable` → `un ##film ##able`），因此 30 522 个词元就能覆盖几乎任意英文输入，几乎不需要 `<unk>`。这与 char-RNN（u6-l4，100 个字符）和 word-level（u6-l1，2 万词）构成三级粒度谱系。

除了 `input_ids`，本讲还要认识一个新张量 **`attention_mask`**：与 `input_ids` 等长的 0/1 序列，1 表示真实词元、0 表示填充位，模型用它在自注意力里跳过填充。

#### 4.1.2 核心流程

```text
原始文本 "I loved this movie!"
   │  AutoTokenizer.from_pretrained("distilbert-base-uncased")
   ▼
[CLS] i loved this movie ! [SEP]        ← 子词分词 + 特殊词元
   │  词表查表（vocab_size=30522）
   ▼
input_ids      = [101, 1045, 3866, 2023, 3185, 999, 102, 0, 0]
attention_mask = [   1,    1,    1,    1,    1,   1,   1, 0, 0]
                 （超过 model_max_length=512 截断；不足处填充）
   │  dataset.map(tokenize_text, batched=True, batch_size=None)
   ▼
整份数据集一次性完成数值化并缓存，新增 input_ids / attention_mask 两列
```

两个关键参数：`truncation=True` 表示超过 `model_max_length`（512）的评论截断；`padding=True` 表示补齐到批内最长。注意 `.map(..., batch_size=None)` 在 `datasets` 库里的语义是「整份数据当成一个批次」——于是填充目标是**全数据集最长序列**（截断后即 512）。这解释了后面看到的形状 `[3, 512, 768]`，也意味着大量算力花在 173 词（中位数）与 512 之间的填充位上。

#### 4.1.3 源码精读

加载分词器并打印两个关键属性（三份 notebook 完全相同）：

> [1_distilbert-as-feature-extractor.ipynb:L672-L676](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/transformer/1_distilbert-as-feature-extractor.ipynb#L672-L676) 加载与模型配套的 `distilbert-base-uncased` 分词器；作者保存的输出为 `model_max_length: 512`、`vocab_size: 30522`。分词器必须与模型检查点同名配对——子词切法和词表编号是一一对应的，混用会导致张量编号指向错误的词。

```python
from transformers import AutoTokenizer

tokenizer = AutoTokenizer.from_pretrained("distilbert-base-uncased")
print("Tokenizer input max length:", tokenizer.model_max_length)
print("Tokenizer vocabulary size:", tokenizer.vocab_size)
```

> [1_distilbert-as-feature-extractor.ipynb:L686-L687](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/transformer/1_distilbert-as-feature-extractor.ipynb#L686-L687) 定义 `map` 用的分词函数：一行完成截断 + 填充，返回的 dict 会被合并进数据集新列。

```python
def tokenize_text(batch):
    return tokenizer(batch["text"], truncation=True, padding=True)
```

> [1_distilbert-as-feature-extractor.ipynb:L740](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/transformer/1_distilbert-as-feature-extractor.ipynb#L740) 对三个切分各自整体分词；`batched=True, batch_size=None` 即「整个 split 当一个批」，`map` 结果自动缓存，第二次运行不再重算。微调版对应单元格在 [distilbert-hf-finetuning.ipynb:L691-L692](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/transformer/distilbert-hf-finetuning.ipynb#L691-L692) 与 [distilbert-hf-finetuning.ipynb:L745](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/transformer/distilbert-hf-finetuning.ipynb#L745)，代码逐字相同。

```python
imdb_tokenized = imdb_dataset.map(tokenize_text, batched=True, batch_size=None)
```

#### 4.1.4 代码实践

1. **实践目标**：亲眼看到 `[CLS]`、子词、填充与 attention mask 的对应关系。
2. **操作步骤**（示例代码，不依赖下载数据集，只下载分词器）：

```python
from transformers import AutoTokenizer
tok = AutoTokenizer.from_pretrained("distilbert-base-uncased")

out = tok(["I loved this movie!", "meh"], truncation=True, padding=True)
print(out["input_ids"])
print(out["attention_mask"])
print([tok.convert_ids_to_tokens(ids) for ids in out["input_ids"]])
```

3. **需要观察的现象**：两条文本都被加上 `[CLS]`（id 101）开头、`[SEP]`（id 102）结尾；短句 `"meh"` 的尾部多出一串 0 填充，且其 `attention_mask` 对应位置为 0；`convert_ids_to_tokens` 能把编号还原成词元。
4. **预期结果**：短句的 `input_ids` 长度与长句对齐，即 `padding=True` 的效果。具体编号数值待本地验证（不同版本分词器编号一致，均为 `distilbert-base-uncased` 词表）。

#### 4.1.5 小练习与答案

**练习 1**：u6-l1 自建词表有 20002 项且仍然大量 `<unk>`，为什么 30522 项的子词词表几乎不需要 `<unk>`？
**答案**：子词分词把低频/未见词拆成高频片段的组合，任何字符串最终都能用词表内词元拼出来；word-level 词表对表外词只有 `<unk>` 一条路。

**练习 2**：`map(..., batch_size=None)` 时 `padding=True` 把序列填充到多长？对显存/算力有什么影响？
**答案**：填充到整个 split 内最长序列（截断后为 512）。影评词数中位数只有 173，等于大部分计算花在填充位上；更省资源的做法是按小批次动态填充，但本仓库三份 notebook 为流程简单统一选择了全局填充。

**练习 3**：如果换用 `distilbert-base-cased` 的分词器但模型仍是 `uncased`，会发生什么？
**答案**：两者的词表编号含义不同（大小写处理、编号映射都不一样），`input_ids` 会指向完全错误的嵌入向量，输出必然不可用。分词器与模型必须同名配对。

### 4.2 特征提取路线：冻结的 DistilBERT + 传统分类器

#### 4.2.1 概念说明

把 DistilBERT 当作一个「不可训练的特征函数」：输入一条评论，输出一个 768 维向量，再用 sklearn 分类器在这个向量空间里画决策边界。预训练模型的参数从头到尾不更新——notebook 里没有优化器，前向全部包在 `torch.inference_mode()` 里，连计算图都不构建，天然「冻结」。

为什么取第 0 个位置？BERT/DistilBERT 在预训练时在句首放 `[CLS]` 词元，训练目标使其聚合整句语义，因此 `last_hidden_state[:, 0]` 常被用作整句表示（sentence embedding）。`last_hidden_state` 形状为 `[batch, seq_len, 768]`，切片后得 `[batch, 768]`——这与 u5-l1 自编码器「提取 32 维编码再喂随机森林」的探针法如出一辙，只是编码器从单隐藏层 MLP 换成了 6 层 Transformer。

#### 4.2.2 核心流程

```text
imdb_tokenized（已数值化，[N, 512]）
   │  set_format("torch", 列=input_ids/attention_mask/label)
   ▼
map(get_output_embeddings, batched=True, batch_size=10)
   │   每个 batch：搬到 GPU → inference_mode 前向 → 取 [CLS] → 搬回 CPU 转 numpy
   ▼
imdb_features：每条样本多一列 768 维 "features"
   │  拼成 X_train/X_val/X_test（numpy 数组）
   ▼
sklearn：RandomForestClassifier / LogisticRegression 训练并评分
```

#### 4.2.3 源码精读

> [1_distilbert-as-feature-extractor.ipynb:L778-L781](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/transformer/1_distilbert-as-feature-extractor.ipynb#L778-L781) 用 `AutoModel` 加载**不带任务头**的裸 DistilBERT。作者输出里的警告 "Some weights ... were not used" 表示预训练检查点里的掩码语言建模头（`vocab_projector` 等）被丢弃——我们只要编码器主干。

```python
from transformers import AutoModel

model = AutoModel.from_pretrained("distilbert-base-uncased")
model.to(device);
```

> [1_distilbert-as-feature-extractor.ipynb:L791](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/transformer/1_distilbert-as-feature-extractor.ipynb#L791) 把指定列切换成 torch 张量格式，供后续按批取数（对比：微调版 notebook 不需要这行，`Trainer` 内部自己处理）。

```python
imdb_tokenized.set_format("torch", columns=["input_ids", "attention_mask", "label"])
```

> [1_distilbert-as-feature-extractor.ipynb:L812-L818](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/transformer/1_distilbert-as-feature-extractor.ipynb#L812-L818) 先取 3 条样本试跑：`torch.inference_mode()` 是比 `no_grad()` 更彻底的推理上下文；作者保存的输出 `torch.Size([3, 512, 768])` 印证了 4.1.2 的填充分析——3 条样本 × 512 个位置 × 768 维隐藏。

```python
test_batch = {"attention_mask": imdb_tokenized["train"][:3]["attention_mask"].to(device),
              "input_ids": imdb_tokenized["train"][:3]["input_ids"].to(device)}

with torch.inference_mode():
    test_output = model(**test_batch)

test_output.last_hidden_state.shape
```

> [1_distilbert-as-feature-extractor.ipynb:L839-L840](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/transformer/1_distilbert-as-feature-extractor.ipynb#L839-L840) 切出 `[CLS]` 位置：形状从 `[3, 512, 768]` 变 `[3, 768]`（作者保存的输出）。

```python
cls_token_output = test_output.last_hidden_state[:, 0]
cls_token_output.shape
```

> [1_distilbert-as-feature-extractor.ipynb:L850-L854](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/transformer/1_distilbert-as-feature-extractor.ipynb#L850-L854) 特征提取主函数：剔除 `label` 列、搬设备、推理前向、取 `[CLS]`、回 CPU 转 numpy 存进新列 `features`。

```python
def get_output_embeddings(batch):
    inputs = {key:tensor.to(device) for key,tensor in batch.items() if key != "label"}
    with torch.inference_mode():
        output = model(**inputs).last_hidden_state[:, 0]
    return {"features": output.cpu().numpy()}
```

> [1_distilbert-as-feature-extractor.ipynb:L907](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/transformer/1_distilbert-as-feature-extractor.ipynb#L907) 对全部 35 000 / 5 000 / 10 000 条样本批处理（训练集进度条为 3500 批）。这里的 `batch_size=10` 与 4.1 的 `batch_size=None` 不同，是真实生效的小批次。

```python
imdb_features = imdb_tokenized.map(get_output_embeddings, batched=True, batch_size=10)
```

> [1_distilbert-as-feature-extractor.ipynb:L917-L924](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/transformer/1_distilbert-as-feature-extractor.ipynb#L917-L924) 把特征列与标签列拼成 sklearn 需要的 numpy 数组，之后模型不再出现在任何训练代码里。

```python
X_train = np.array(imdb_features["train"]["features"])
y_train = np.array(imdb_features["train"]["label"])
# X_val / y_val / X_test / y_test 同理
```

> [1_distilbert-as-feature-extractor.ipynb:L952-L960](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/transformer/1_distilbert-as-feature-extractor.ipynb#L952-L960) 与 [1_distilbert-as-feature-extractor.ipynb:L979-L987](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/transformer/1_distilbert-as-feature-extractor.ipynb#L979-L987) 分别训练随机森林与逻辑回归。作者保存的结果：RF 训练 1.0 / 验证 0.8408 / 测试 0.8324（严重过拟合）；LogReg 训练 0.8865 / 验证 0.883 / 测试 0.8794。注意 LogReg 单元沿用了变量名 `rf`——读源码以赋值语句为准，别被变量名误导。

```python
from sklearn.linear_model import LogisticRegression

rf = LogisticRegression(max_iter=1000)   # 变量名是历史遗留，实际是逻辑回归
rf.fit(X_train, y_train)
print("test accuracy", rf.score(X_test, y_test))   # 作者输出：0.8794
```

#### 4.2.4 代码实践

1. **实践目标**：在小子集上完整走通「分词 → 提特征 → 训练分类器」，并对比两种分类器。
2. **操作步骤**：把 4.1.3 的 `map` 与本节的 `map` 之间加一步子采样（示例代码，用 `datasets` 的 `select`；`imdb_dataset` 需在 `del` 之前保留引用）：

```python
# 示例代码：子采样以缩短运行时间
small = imdb_dataset.select(range(3000))          # 前 3000 条做训练
small_tokenized = small.map(tokenize_text, batched=True, batch_size=None)
small_features = small_tokenized.map(get_output_embeddings, batched=True, batch_size=10)
X = np.array(small_features["train"]["features"])
y = np.array(small_features["train"]["label"])
```

   然后分别 `fit` 随机森林与 `LogisticRegression(max_iter=1000)`，打印训练/测试准确率。
3. **需要观察的现象**：`X.shape` 应为 `(3000, 768)`；逻辑回归在 768 维特征上秒级收敛；随机森林训练明显更慢且训练准确率贴近 1.0。
4. **预期结果**：小子集上两种分类器准确率都明显低于全量数据的 83%/88%（训练样本少了 10 倍以上），但都应显著高于随机猜测 50%。具体数值待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：这条路线里 DistilBERT 为什么天然不会被训练？请指出两处代码证据。
**答案**：其一，全程没有创建优化器，模型参数没有任何更新语句；其二，前向包在 `torch.inference_mode()` 里，不构建计算图、不记录梯度，参数即便想更新也无梯度可用。

**练习 2**：随机森林训练准确率 1.0、测试 0.8324，逻辑回归 0.8865 / 0.8794。这提示 768 维 `[CLS]` 特征空间大概是什么形态？
**答案**：两类并非被复杂边界分得很开——树模型（可学复杂边界） Train 100% 说明有足够容量记住训练样本，但泛化差；线性模型几乎无过拟合且只低约 5 个点，说明决策边界以近似线性为主。同时也说明冻结的 `[CLS]` 表示对情感任务只是「接近线性可分」，还有约 5–9 个百分点的空间要靠微调解锁。

**练习 3**：本节特征提取与 u5-l1「AE 编码 + 随机森林」探针法在方法论上有什么相同点？
**答案**：都是「冻结编码器 → 提取低维表示 → 用传统分类器评估表示质量」：自编码器用 32 维瓶颈编码，这里用预训练模型的 768 维 `[CLS]` 嵌入；分类器都只是探针，用来衡量表示本身的线性/非线性可分性。

### 4.3 端到端微调（上）：HF Trainer 一站式训练

#### 4.3.1 概念说明

微调路线换一个加载入口：`AutoModelForSequenceClassification`。它在编码器后面接上一个小分类头（DistilBERT 是 `pre_classifier`(768→768) + ReLU + `classifier`(768→2)），并且 `forward` 接受 `labels` 参数——**损失函数（交叉熵）由模型内部直接算好返回**，不再需要我们自己选损失。这与 u6-l1~u6-l5 自己定义模型、自己挑损失的写法是两种风格。

`Trainer` 则是高层训练循环：把「DataLoader 搭建 → 五步训练范式 → 按 epoch 评估 → checkpoint 保存 → 日志」全部打包，对应 u4-l4 手写的 `train_classifier_simple_v2`，只是粒度更粗、默认值更多。

学习率与调度（本讲学习目标之一）：notebook 显式设置 `learning_rate=1e-5`（比常见默认 5e-5 更保守），未显式设置调度器——transformers 4.23 的 `TrainingArguments` 默认 `lr_scheduler_type="linear"`、`warmup_steps=0`，即无预热的线性衰减，第 \(t\) 步的学习率为

\[ \eta_t = \eta_{\max}\cdot\max\left(0,\;1-\frac{t}{T}\right) \]

其中 \(T\) 为总优化步数。微调预训练大模型偏好小学习率 + 衰减，因为参数本来就在好解附近，大步长容易把预训练知识冲垮。

#### 4.3.2 核心流程

```text
imdb_tokenized（datasets.Dataset，含 input_ids/attention_mask/label）
   │
   ├─ model = AutoModelForSequenceClassification.from_pretrained(..., num_labels=2)
   │      → 加载编码器权重；分类头 newly initialized（需训练）
   │
   ├─ TrainingArguments(output_dir, num_train_epochs=3,
   │      evaluation_strategy="epoch", learning_rate=1e-5)
   │
   └─ Trainer(model, args, compute_metrics,
              train_dataset=..., eval_dataset=..., tokenizer=...)
          │  trainer.train()
          ▼
      每 epoch：训练 → 验证评估（compute_metrics）→ 存 checkpoint
          │  trainer.predict(...)
          ▼
      train 0.9705 / validation 0.9312 / test 0.926（作者保存输出）
```

#### 4.3.3 源码精读

> [distilbert-hf-finetuning.ipynb:L785-L789](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/transformer/distilbert-hf-finetuning.ipynb#L785-L789) 加载带分类头的模型。作者输出中的两条警告各表一事：预训练 MLM 头被丢弃（同 4.2）；`classifier.*`、`pre_classifier.*` 是**新初始化**的权重——这正是「必须再训练」的部位。

```python
from transformers import AutoModelForSequenceClassification

model = AutoModelForSequenceClassification.from_pretrained(
    "distilbert-base-uncased", num_labels=2)
model.to(device);
```

> [distilbert-hf-finetuning.ipynb:L943-L952](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/transformer/distilbert-hf-finetuning.ipynb#L943-L952) 评估指标回调：`Trainer` 把 logits 与真标签打包成 `eval_pred` 传进来，这里取 `argmax` 转成类别再算准确率（`load_metric` 在新版 datasets 已弃用，改用 `evaluate.load`——作者输出里也有该 FutureWarning）。

```python
from transformers import Trainer, TrainingArguments
from datasets import load_metric

metric = load_metric('accuracy')

def compute_metrics(eval_pred):
    predictions, labels = eval_pred
    predictions = np.argmax(predictions, axis=1)
    acc = metric.compute(predictions=predictions, references=labels)
    return {"accuracy": acc}
```

> [distilbert-hf-finetuning.ipynb:L953-L957](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/transformer/distilbert-hf-finetuning.ipynb#L953-L957) 训练参数。**读源码关键发现**：`batch_size = 10` 定义后从未传入 `TrainingArguments`！作者训练日志显示 "Instantaneous batch size per device = 8 / Total train batch size = 32"——单卡批量走的是默认值 8，总批量 32 是因为 4 张 GPU。这正是 u3-l5 确立的方法论：以代码与日志为准，不要想当然。

```python
batch_size = 10
trainer_args = TrainingArguments(output_dir="distilbert-v1",
                                 num_train_epochs=3,
                                 evaluation_strategy="epoch",
                                 learning_rate=1e-5)
```

> [distilbert-hf-finetuning.ipynb:L959-L966](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/transformer/distilbert-hf-finetuning.ipynb#L959-L966) 组装 `Trainer` 并启动训练。日志 "columns ... ignored: index, text" 说明 CSV 里的多余列被自动剔除，只把 `input_ids/attention_mask/label` 喂给模型。仓库基准脚本 [1_just-hf.py:L55-L60](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/transformer/distilbert-benchmark/1_just-hf.py#L55-L60) 是修正版写法：显式传 `per_device_train_batch_size=12`。

```python
trainer = Trainer(model=model,
                  args=trainer_args,
                  compute_metrics=compute_metrics,
                  train_dataset=imdb_tokenized["train"],
                  eval_dataset=imdb_tokenized["validation"],
                  tokenizer=tokenizer)

trainer.train();
```

> [distilbert-hf-finetuning.ipynb:L1109](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/transformer/distilbert-hf-finetuning.ipynb#L1109) 用 `trainer.predict` 在测试集评估；作者保存的指标在 [L1097](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/transformer/distilbert-hf-finetuning.ipynb#L1097)：测试准确率 0.926、平均每秒 185 条样本。

```python
outputs = trainer.predict(imdb_tokenized["test"])
outputs.metrics
```

#### 4.3.4 代码实践

1. **实践目标**：从训练日志反推真实训练配置，体会「读日志验证代码」。
2. **操作步骤**：
   - 运行 [distilbert-hf-finetuning.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/transformer/distilbert-hf-finetuning.ipynb) 到训练单元格（GPU 环境约需数十分钟；无 GPU 可只读作者保存的输出做纸面推演）。
   - 用日志里的数字做算术验证：`Num examples = 35000`、`Total train batch size = 32`，则每 epoch 步数 = ⌈35000 / 32⌉ = 1094，总步数 = 1094 × 3 = **3282**，与日志 "Total optimization steps = 3282" 对上。
3. **需要观察的现象**：每 epoch 结束触发一次验证（`evaluation_strategy="epoch"`），验证准确率逐 epoch 变化；`distilbert-v1/checkpoint-*` 目录按 500 步间隔落盘。
4. **预期结果**：最终验证 0.9312、测试 0.926（作者输出）。另注意：新版 transformers 已把 `evaluation_strategy` 更名为 `eval_strategy`，如报参数错误按所用版本改名即可（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`AutoModel` 与 `AutoModelForSequenceClassification` 加载同一检查点时的输出警告有何不同？分别说明被丢弃/新增了什么。
**答案**：两者都丢弃预训练 MLM 头（`vocab_projector` 等）。前者只留编码器主干，输出 `last_hidden_state`；后者额外**新初始化** `pre_classifier` 和 `classifier` 分类头，`forward` 可接收 `labels` 并返回内置损失与 `logits`。

**练习 2**：为什么微调用 `1e-5`，而 u2/u3 里从零训练的小模型常用 `1e-2` 量级？
**答案**：微调的起点是已收敛的预训练权重，参数已在好解附近，大学习率会破坏预训练表示（灾难性遗忘）；从零训练的参数随机初始化，需要大步长快速靠近解。微调还配线性衰减调度，让后期步长进一步缩小以精修。

**练习 3**：日志里 "Total train batch size = 32" 在只有 1 张 GPU 的机器上会变成多少？为什么？
**答案**：8。总批量 = `per_device_train_batch_size`（此处取默认 8，因为 notebook 没把 `batch_size=10` 传进去）× GPU 数 × 梯度累积步数（1）；单卡时 8 × 1 × 1 = 8。

### 4.4 端到端微调（下）：Lightning 手写训练循环与两条路线对比

#### 4.4.1 概念说明

第三份 notebook 属于 `pytorch-lightning_ipynb` 目录：同样的微调任务，但不用 HF `Trainer`，而是把 HF 模型包进 `LightningModule`，训练循环交给 Lightning 的 `Trainer.fit`。这是 u8-l1「LightningModule + DataModule + Trainer」三件套的第一次实战预演，也是 Lightning 版模型库（u8-l2）的文体样本。

与 HF `Trainer` 的三点差异值得注意：

1. **损失来源**：HF 模型的 `forward(labels=...)` 返回带 `loss` 的 dict，`training_step` 只需把 `outputs["loss"]` 返回给框架——五步训练范式（forward → loss → zero_grad → backward → step）全部由 Lightning 在幕后执行。
2. **优化器与学习率**：`configure_optimizers` 用普通 `Adam`、`lr=5e-5`、**无调度器**——与 4.3 的「AdamW + 1e-5 + 线性衰减」形成对照（AdamW 即带解耦权重衰减的 Adam，HF 日志中也有弃用警告）。
3. **数据接口**：不再把 `datasets.Dataset` 直接交给训练器，而是套一层自定义 `Dataset`（u4-l2 的三要素模板）再接标准 `DataLoader`。

评估侧用 `torchmetrics.Accuracy()` 作为**有状态指标**对象：`self.val_acc(pred, target)` 内部累计混淆计数，`self.log` 按 epoch 聚合——这正是 u7-l1 讲过的 update/forward 语义，指标对象化避免了手写累计器。

#### 4.4.2 核心流程

```text
imdb_tokenized.set_format("torch")
   │
   ├─ IMDBDataset(dataset_dict, partition_key)   ← Dataset 三要素包装
   ├─ DataLoader(batch_size=12, num_workers=4)   ×3（train/val/test）
   │
   ├─ LightningModel(model, learning_rate=5e-5)
   │      training_step / validation_step / test_step / configure_optimizers
   │
   ├─ ModelCheckpoint(save_top_k=1, monitor="val_acc", mode="max") + CSVLogger
   │
   └─ L.Trainer(max_epochs=3, ...).fit(...)
          │  trainer.test(..., ckpt_path="best")   ← 用验证集选出的最优权重测试
          ▼
      train 0.9647 / validation 0.9314 / test 0.9262（作者保存输出）
```

#### 4.4.3 源码精读

> [distilbert-finetuning-ii.ipynb:L803-L811](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/transformer/distilbert-finetuning-ii.ipynb#L803-L811) u4-l2 的 Dataset 三要素再次出现：`__init__` 记住分区、`__getitem__` 按索引返回一条已数值化样本、`__len__` 报 `num_rows`。前一行的 `os.environ["TOKENIZERS_PARALLELISM"] = "false"`（[L782](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/transformer/distilbert-finetuning-ii.ipynb#L782)）关闭分词器的并行 fast-tokenizer，避免与 DataLoader 多进程互相死锁的警告。

```python
class IMDBDataset(Dataset):
    def __init__(self, dataset_dict, partition_key="train"):
        self.partition = dataset_dict[partition_key]

    def __getitem__(self, index):
        return self.partition[index]

    def __len__(self):
        return self.partition.num_rows
```

> [distilbert-finetuning-ii.ipynb:L821-L843](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/transformer/distilbert-finetuning-ii.ipynb#L821-L843) 三个标准 DataLoader：`batch_size=12`、训练集 `shuffle=True`、`num_workers=4` 多进程加载；验证/测试不 shuffle（延续 u4-l1 的约定）。

```python
train_loader = DataLoader(
    dataset=train_dataset,
    batch_size=12,
    shuffle=True,
    num_workers=4
)
```

> [distilbert-finetuning-ii.ipynb:L915-L932](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/transformer/distilbert-finetuning-ii.ipynb#L915-L932) LightningModule 核心：`forward` 直接转调 HF 模型并传入 `labels`；`training_step` 取回内置损失、`self.log` 记录后返回给优化循环。注意这里没有 loss 函数、没有 `optimizer.step()`——框架代办。

```python
class LightningModel(L.LightningModule):
    def __init__(self, model, learning_rate=5e-5):
        super().__init__()
        self.learning_rate = learning_rate
        self.model = model
        self.val_acc = torchmetrics.Accuracy()
        self.test_acc = torchmetrics.Accuracy()

    def forward(self, input_ids, attention_mask, labels):
        return self.model(input_ids, attention_mask=attention_mask, labels=labels)

    def training_step(self, batch, batch_idx):
        outputs = self(batch["input_ids"], attention_mask=batch["attention_mask"],
                       labels=batch["label"])
        self.log("train_loss", outputs["loss"])
        return outputs["loss"]  # this is passed to the optimizer for training
```

> [distilbert-finetuning-ii.ipynb:L934-L942](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/transformer/distilbert-finetuning-ii.ipynb#L934-L942) 验证步：损失照记，另取 `logits` 做 `argmax` 得预测，喂给有状态的 `torchmetrics.Accuracy` 累计（torchmetrics 新版要求 `Accuracy(task="binary")` 之类的 task 参数——作者输出的 DeprecationWarning 已预告此变更）。

```python
    def validation_step(self, batch, batch_idx):
        outputs = self(batch["input_ids"], attention_mask=batch["attention_mask"],
                       labels=batch["label"])
        self.log("val_loss", outputs["loss"], prog_bar=True)

        logits = outputs["logits"]
        predicted_labels = torch.argmax(logits, 1)
        self.val_acc(predicted_labels, batch["label"])
        self.log("val_acc", self.val_acc, prog_bar=True)
```

> [distilbert-finetuning-ii.ipynb:L953-L956](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/transformer/distilbert-finetuning-ii.ipynb#L953-L956) 优化器配置：普通 Adam、学习率 5e-5、无调度器。与 4.3 的 AdamW+1e-5+线性衰减是两种都成立的微调配方，结果几乎一致。

```python
    def configure_optimizers(self):
        optimizer = torch.optim.Adam(self.parameters(), lr=self.learning_rate)
        return optimizer
```

> [distilbert-finetuning-ii.ipynb:L968-L977](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/transformer/distilbert-finetuning-ii.ipynb#L968-L977) 按 `val_acc` 最大值保留 top-1 checkpoint（即 u6-l5 的「验证集选优」纪律），并用 CSVLogger 落盘指标。

```python
callbacks = [
    ModelCheckpoint(
        save_top_k=1, mode="max", monitor="val_acc"
    )  # save top 1 model
]
logger = CSVLogger(save_dir="logs/", name="my-model")
```

> [distilbert-finetuning-ii.ipynb:L1274-L1285](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/transformer/distilbert-finetuning-ii.ipynb#L1274-L1285) Trainer 配置与启动。`devices=[2]` 是作者机器上的 GPU 编号，单卡环境应改为 `devices=1` 或 `"auto"`（待本地验证）。

```python
trainer = L.Trainer(
    max_epochs=3,
    callbacks=callbacks,
    accelerator="gpu",
    devices=[2],
    logger=logger,
    log_every_n_steps=10,
)

trainer.fit(model=lightning_model,
            train_dataloaders=train_loader,
            val_dataloaders=val_loader)
```

> [distilbert-finetuning-ii.ipynb:L1382](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/transformer/distilbert-finetuning-ii.ipynb#L1382)、[L1477](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/transformer/distilbert-finetuning-ii.ipynb#L1477)、[L1572](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/transformer/distilbert-finetuning-ii.ipynb#L1572) 三个 `trainer.test(..., ckpt_path="best")` 分别用最优 checkpoint 评 train/val/test。作者输出：0.9647 / 0.9314 / **0.9262**——与 HF Trainer 路线的 0.926 基本重合，说明两条微调实现等价有效。

```python
trainer.test(lightning_model, dataloaders=test_loader, ckpt_path="best")
# 作者输出：[{'accuracy': 0.9261999726295471}]
```

一个可以从日志读出的细节：最优 checkpoint 文件名是 `epoch=0-step=2917.ckpt`——每 epoch 步数 = ⌈35000 / 12⌉ = 2917，即**第 1 个 epoch 结束时验证准确率最高**，后两个 epoch 虽然继续训练（train 准确率升到 0.9647）但验证没有再提升，微调任务「早停即最优」的典型形态；`ckpt_path="best"` 保证最终报告用的是第 1 个 epoch 的权重。

#### 4.4.4 代码实践

1. **实践目标**：把 Lightning 版跑通到单卡，并验证「最优 checkpoint」机制。
2. **操作步骤**：
   - 打开 [distilbert-finetuning-ii.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/transformer/distilbert-finetuning-ii.ipynb)，将 `devices=[2]` 改为 `devices=1`（或 `accelerator="auto", devices="auto"`）。
   - 若你的 torchmetrics 较新，把 `torchmetrics.Accuracy()` 改为 `torchmetrics.Accuracy(task="binary")`（二分类）。
   - 训练结束后查看 `logs/my-model/version_0/checkpoints/` 下的文件名，确认它记录的 epoch 与 CSV 日志里 `val_acc` 最高的 epoch 是否一致。
3. **需要观察的现象**：进度条实时显示 `val_loss` 与 `val_acc`（`prog_bar=True` 的效果）；`trainer.test` 三次输出中 train > val > test 的排序。
4. **预期结果**：测试准确率在 0.92–0.93 附近（作者为 0.9262）；不同 GPU/种子会有小幅波动，待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`training_step` 里只有 `self.log(...)` 和 `return outputs["loss"]`，五步训练范式的其余四步去哪了？
**答案**：由 Lightning 框架执行——它拿到返回的损失后依次完成构造计算图、`zero_grad`、`backward`、`optimizer.step`。损失本身则是 HF 模型在 `forward(labels=...)` 内部用内置交叉熵算出的。

**练习 2**：两份微调 notebook 的优化设置（Adam 5e-5 无调度 vs AdamW 1e-5 线性衰减）差异不小，最终测试准确率却几乎相同（0.9262 vs 0.926）。这说明什么？
**答案**： IMDb 情感分类对 DistilBERT 是相对「容易」的迁移任务，处于两个配方都够用的舒适区；同时也提示：在这种任务上比较训练技巧收益需要控制变量并做多次运行（u3-l2 的结论再次适用），单次运行的差异不足为凭。

**练习 3**：为什么 `trainer.test` 要传 `ckpt_path="best"` 而不是直接用训练结束时的最终权重？
**答案**：因为验证准确率最高的 epoch 不一定是最后一个（本例 best 在 epoch 1）；用最终权重报告测试成绩会低估模型。这与 u6-l5「按验证准确率保存 checkpoint、重载最优后测试」是同一条实验纪律。

## 5. 综合实践

**任务：两条路线的三维对比实验（准确率 / 显存 / 训练时间）**

这正是 `distilbert-benchmark` 目录的用途：[1_just-hf.py](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/transformer/distilbert-benchmark/1_just-hf.py) 已经示范了计时方法——训练前后各取 `time.time()` 相减（[L69-L76](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/transformer/distilbert-benchmark/1_just-hf.py#L69-L76)），数据准备则复用 [shared.py 的 prepare_data](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/transformer/distilbert-benchmark/shared.py#L40-L83)。

1. **准备**：先用 `.select(range(N))` 把三个 split 都缩到同一小规模（如 train 2000 / val 500 / test 1000），保证两条路线能在合理时间内跑完。
2. **路线 A（特征提取）**：按 4.2 的 `map` 提特征（计时这一段），然后训练 `LogisticRegression(max_iter=1000)`，记录测试准确率。
3. **路线 B（端到端微调）**：按 4.3 用 HF `Trainer` 训 1 个 epoch（`num_train_epochs=1`，显式传 `per_device_train_batch_size=12`，与基准脚本一致），`trainer.predict` 得测试准确率。
4. **测量显存**（示例代码，GPU 环境）：两条路线都在结束后读取峰值显存：

```python
torch.cuda.reset_peak_memory_stats()   # 训练/提特征开始前
# …… 训练或提特征 ……
print(torch.cuda.max_memory_allocated() / 1024**2, "MB")
```

5. **填写对比表**：

| 指标 | 路线 A：特征提取 + LogReg | 路线 B：端到端微调 |
|---|---|---|
| 测试准确率 | 待填 | 待填 |
| 峰值显存 (MB) | 待填 | 待填 |
| DistilBERT 前向耗时 | 待填 | —（含在训练内） |
| 分类器/微调训练耗时 | 待填 | 待填 |

6. **预期结果**（依据作者全量数据的输出外推，小子集上具体数值待本地验证）：路线 B 准确率应明显高于路线 A（全量数据为 92.6% vs 87.9%）；路线 B 峰值显存显著更高（需要为整个模型保存梯度与优化器状态，路线 A 推理态只需前向激活）；路线 B 训练时间也更长，但注意路线 A 的「提特征」成本同样不可忽略，公平对比应把它计入。
7. **思考题**：如果标注数据只有 200 条，你会选哪条路线？（提示：微调 6600 万参数容易过拟合，特征提取 + 线性分类器在小样本下通常更稳。）

## 6. 本讲小结

- **输入管线统一**：`AutoTokenizer.from_pretrained` + `map(tokenize_text, batched=True, batch_size=None)` 三份 notebook 完全一致——子词分词（30522 词元、几乎无 `<unk>`）+ 截断到 512 + 全局填充，产出 `input_ids` 与 `attention_mask` 两列。
- **特征提取路线**：`AutoModel` 冻结主干（`inference_mode` + 无优化器），取 `last_hidden_state[:, 0]` 的 `[CLS]` 嵌入（768 维），交给 sklearn——RF 测试 83.24%、LogReg 87.94%，表示「接近线性可分」但仍有提升空间。
- **端到端微调路线**：`AutoModelForSequenceClassification` 新初始化分类头、内置交叉熵；HF `Trainer` 用 1e-5 + 默认线性衰减（无预热）取得 92.6% 测试准确率；Lightning 版用 Adam 5e-5 无调度器取得 0.9262——两种实现殊途同归。
- **读日志即读代码**：`batch_size = 10` 定义却未传入 `TrainingArguments`（真实单卡批量为默认 8）；总步数 3282 = ⌈35000/32⌉×3、最优 checkpoint `epoch=0-step=2917` = ⌈35000/12⌉，都可以从代码直接推出并验证。
- **实验纪律延续**：`ModelCheckpoint(monitor="val_acc")` + `ckpt_path="best"` 延续了 u6-l5「验证集选优、最优权重只测一次测试集」的规范；`torchmetrics` 有状态指标与 u7-l1 的 update/forward 语义呼应。
- **版本兼容提醒**：`load_metric`→`evaluate.load`、`evaluation_strategy`→`eval_strategy`、`torchmetrics.Accuracy` 需加 `task` 参数、`devices=[2]` 需按本机 GPU 改——notebook 记录的是 2022 年的环境（transformers 4.23.1）。

## 7. 下一步学习建议

- **进入 u8 单元**：本讲 Lightning 版是 u8-l1「Lightning 基础」的直接跳板——那里会系统讲 LightningModule/DataModule/Trainer 三件套、TensorBoard 与最优 checkpoint；u8-l2 则展示如何把 `pytorch_ipynb` 的普通 notebook 对照迁移成 Lightning 版本。
- **继续读 transformer 目录的姊妹篇**：`distilbert-finetune-last-layers.ipynb`（只微调最后几层的分层冻结策略）、`distilbert-finetune-scheduler.ipynb`（用 `finetuning-scheduler` 回调做逐层解冻）、`distilbert-v0.ipynb`（更手写风格的微调实现）、`distilbert-embetter-feature-extractor.ipynb`（嵌入微调变体），以及两份 `_without-dcollate` 变体可对照数据整理细节差异。
- **横向连接**：u7-l4 的迁移学习（冻结卷积特征提取器 vs 微调）与本讲两条路线是同一方法论在视觉与文本两侧的镜像；u7-l3 的学习率技巧（LR Range Test、循环学习率）可迁移到微调场景。
- **动手方向**：用第 5 节的对比框架换一个数据集（如 AG News，参考 u6-l3 的 CSV 管线）重跑两条路线，检验「特征提取够用样本量」的分界点在哪里。

# u16-l2 GPT-2 微调做情感分类

## 1. 本讲目标

学完本讲，你应该能够：

1. 用 `transformers` 库加载预训练 GPT-2 权重，理解 `GPT2Tokenizer`、`GPT2Model` 各自产出什么。
2. 理解 `AutoModelForSequenceClassification` 加载预训练模型时「分类头随机初始化」的做法——为什么预训练权重里没有分类头、新头从哪来、HuggingFace 的警告在说什么。
3. 掌握用 `param.requires_grad = False` 逐层冻结基础模型、只训练分类头的策略，并能统计 frozen vs trainable 参数量对比。
4. 读懂 IMDb 数据集的采样划分、`IMDbDataset` 封装与 `compute_accuracy` 评估函数，理解训练/验证准确率随 epoch 变化的曲线如何解读。
5. 理解 AdamW 优化器与「warmup + 线性衰减」学习率调度器的设计动机，能写出调度公式。

**开讲前的重要事实澄清**：本仓库 `ch16/ch16-part2-gpt2.py` 只收录到「加载预训练 GPT-2 并观察输出」为止，**没有收录 GPT-2 的微调代码**。微调所需的零件散落在仓库其他真实代码中：

- 「加载预训练模型 + 换分类头」的真实代码在 [ch16/bonus-distilbert-lightning-trainer/distilbert_finetuning-full.ipynb](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/bonus-distilbert-lightning-trainer/distilbert_finetuning-full.ipynb)（用 DistilBERT 演示，模式与 GPT-2 完全一致）；
- 「IMDb 数据下载/划分、Dataset/DataLoader、`compute_accuracy`、训练循环」的真实代码在 [ch16/ch16-part3-bert.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part3-bert.py)。

因此本讲以「真实代码讲通用机制、示例代码落到 GPT-2」的方式展开：凡是用 GPT-2 做分类微调的具体写法，均明确标注为**示例代码**；凡引用仓库文件，均给出可点击的永久链接。

## 2. 前置知识

### 2.1 预训练—微调两阶段范式

上一讲（u16-l1）我们从零实现了自注意力与多头注意力。真实世界里没人从零训练大模型——语言模型的训练分两步：

1. **预训练（pre-training）**：在海量无标注文本上做「预测下一个词」的自监督学习，得到一个懂语言的通用模型（如 GPT-2）。这一步开销巨大，只做一次。
2. **微调（fine-tuning）**：拿预训练好的权重做初始化，换上一个任务相关的输出头（例如情感二分类头），在有标注的小数据集（如 IMDb 影评）上继续训练。

这本质上是迁移学习（u14-l3）在 NLP 中的形态：卷积基座换成 Transformer 主体，全连接分类头换成任务头。

### 2.2 GPT-2 是什么样的模型

GPT-2 是 **decoder-only Transformer**：词元嵌入 + 位置嵌入，经过 12 个（small 版）Transformer 解码器块，每块含因果掩码多头注意力（上一讲的因果掩码在这里上岗：每个位置只能看左边）与前馈网络。最小版（`'gpt2'`）约 1.24 亿参数，隐藏维度 768。预训练目标是「给前文预测下一个词」，所以它天然是**生成模型**——本讲开头会用它生成文本，再把同一个模型改造成**分类器**。

### 2.3 requires_grad 与自动微分

u12-l3 与 u13-l1 讲过：PyTorch 前向计算自动建图，`loss.backward()` 把梯度算进每个 `requires_grad=True` 的参数；把某参数的 `requires_grad` 置为 `False`，反向传播就**跳过它**——梯度不计算、参数不更新，但前向计算照常使用它的（预训练）权重。这就是「冻结」的全部机制。

### 2.4 logits 与交叉熵

u12-l3、u15-l2 讲过：分类网络输出层产出未归一化的 **logits**，配 softmax 得概率、配交叉熵损失训练。二分类 sentiment 任务里，模型对每条影评输出 2 个 logits（负/正），`argmax` 取较大者为预测标签。HuggingFace 的分类模型把「softmax + 交叉熵」打包在内部：前向时传入 `labels` 就直接返回 `loss`。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲使用方式 |
|---|---|---|
| [ch16/ch16-part2-gpt2.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part2-gpt2.py) | 加载预训练 GPT-2：pipeline 文本生成、GPT2Tokenizer 编码、GPT2Model 隐状态 | 4.1 主线真实源码 |
| [ch16/bonus-distilbert-lightning-trainer/distilbert_finetuning-full.ipynb](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/bonus-distilbert-lightning-trainer/distilbert_finetuning-full.ipynb) | bonus notebook：`AutoModelForSequenceClassification.from_pretrained(..., num_labels=2)` 加载与 Lightning 微调 | 4.1 换头真实依据（注意：链接行号指向 ipynb 的原始 JSON 行） |
| [ch16/ch16-part3-bert.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part3-bert.py) | IMDb 下载与划分、分词、IMDbDataset、DataLoader、compute_accuracy、手写训练循环、Trainer API | 4.3、4.4 真实源码 |
| [ch13/ch13_part1.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py) | requires_grad 属性与自动微分机制演示 | 4.2 冻结机制锚点 |
| [ch08/movie_data.csv.gz](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/movie_data.csv.gz) | 5 万条 IMDb 影评（第 8 章整理好的 CSV 压缩包） | 4.3 数据来源 |

## 4. 核心概念与源码讲解

### 4.1 下载预训练权重并改分类头

#### 4.1.1 概念说明

`from_pretrained('gpt2')` 做三件事：按名字在 HuggingFace Hub 找到模型、下载权重与配置到本地缓存、按配置搭好网络结构并把权重填进去。但要注意 **GPT-2 的预训练目标里没有分类头**——预训练 checkpoint 只有 Transformer 主体（词嵌入、位置嵌入、12 个块、层归一化）。做情感分类需要**新接一个线性层**（768 → 2），这个新层的权重在 checkpoint 里不存在，只能**随机初始化**，然后在下游任务上训练。

HuggingFace 把「主体 + 任务头」封装成 `XxxForSequenceClassification` 类。加载时它会明确警告你：哪些权重是新初始化的、需要训练——这不是报错，而是设计好的工作方式。分类头的接法分两类：

- **因果模型（GPT-2）**：因为只能看左边，取**最后一个词元**的隐状态（768 维）作为整句表示，接线性层输出 2 个 logits；
- **双向模型（BERT/DistilBERT）**：取特殊词元 `[CLS]` 的隐状态接分类头（外加一个前置全连接层 `pre_classifier`）。

#### 4.1.2 核心流程

```text
pipeline('text-generation', model='gpt2')   # ① 最省事：生成任务开箱即用
        ↓
GPT2Tokenizer.from_pretrained('gpt2')       # ② 拿预训练分词器：句子 → input_ids + attention_mask
        ↓
GPT2Model.from_pretrained('gpt2')           # ③ 拿预训练主体：input_ids → last_hidden_state (n, L, 768)
        ↓
ForSequenceClassification 版本               # ④ 主体权重来自 checkpoint，
                                            #    分类头权重随机初始化 → 必须微调后才能用于预测
```

#### 4.1.3 源码精读

**① 文本生成 pipeline**。[ch16/ch16-part2-gpt2.py:75-79](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part2-gpt2.py#L75-L79) 用一行 `pipeline('text-generation', model='gpt2')` 完成下载、加载、生成：给定开头 `"Hey readers, today is"`，自回归续写 3 条互不相同的 20 词元文本（notebook 实际输出如 *"Hey readers, today is Christmas. This is not Christmas, because Christmas is so long…"*）。这证明权重已就位且「预测下一词」能力来自预训练。

**② 分词器编码**。[ch16/ch16-part2-gpt2.py:85-88](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part2-gpt2.py#L85-L88) 把句子编码成 `input_ids` 与 `attention_mask` 两个张量。notebook 的真实输出：`"Let us encode this sentence"` → `input_ids = [5756, 514, 37773, 428, 6827]`，`attention_mask` 全 1（5 个词元都有效）。GPT-2 用字节对编码（BPE）子词词表（50257 个词元），微调时**必须用同一个预训练分词器**，词表对不上权重就作废。

**③ 主体前向**。[ch16/ch16-part2-gpt2.py:93-99](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part2-gpt2.py#L93-L99) 加载 `GPT2Model` 并前向：输出字典里 `last_hidden_state` 的形状是 `(1, 5, 768)`——**1 条句子、5 个词元、每词元一个 768 维上下文向量**。这正是上一讲自注意力算出的上下文向量按层堆叠后的成品：每个位置都融合了它左边所有词的信息。分类要做的就是从这 5 个向量里取一个（GPT-2 取最后一个）接上分类头。

**④ 换分类头的真实代码与警告**。bonus notebook 中 [distilbert_finetuning-full.ipynb:1330-1333（原始 JSON 行）](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/bonus-distilbert-lightning-trainer/distilbert_finetuning-full.ipynb#L1330-L1333) 用 `AutoModelForSequenceClassification.from_pretrained("distilbert-base-uncased", num_labels=2)` 完成换头；紧接着的运行输出（[同文件 L1324-1325](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/bonus-distilbert-lightning-trainer/distilbert_finetuning-full.ipynb#L1324-L1325)）是 HuggingFace 的两条警告：

> `Some weights of DistilBertForSequenceClassification were not initialized from the model checkpoint at distilbert-base-uncased and are newly initialized: ['pre_classifier.bias', 'classifier.bias', 'classifier.weight', 'pre_classifier.weight']`
> `You should probably TRAIN this model on a down-stream task...`

这条警告就是「分类头随机初始化」的**直接证据**：`classifier`（和 `pre_classifier`）的权重不在 checkpoint 里、是刚随机生成的，所以模型此时**不能直接用于预测**，必须先训练。把模型名换成 `'gpt2'`，同样的机制生效（随机初始化的将是 `score` 头）。

#### 4.1.4 代码实践

> **示例代码**（仓库未收录 GPT-2 换头代码，以下按仓库真实的 DistilBERT 模式改写为 GPT-2 版本）

1. **实践目标**：亲眼看到「主体权重来自预训练、分类头新初始化」。
2. **操作步骤**：

```python
# 示例代码：GPT-2 版的换头加载（对照 bonus notebook 的 distilbert 版）
import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer

model = AutoModelForSequenceClassification.from_pretrained('gpt2', num_labels=2)
tokenizer = AutoTokenizer.from_pretrained('gpt2')
print(model.score)          # GPT-2 的分类头：Linear(in_features=768, out_features=2, bias=False)
```

3. **需要观察的现象**：加载时 stderr 打印 `Some weights of GPT2ForSequenceClassification were not initialized... ['score.weight']` 之类的警告，与 bonus notebook 中 DistilBERT 的警告同款。
4. **预期结果**：`model.score` 显示 `(768) → 2` 的无偏置线性层；`model.transformer` 则持有约 1.24 亿预训练参数。
5. 具体 warning 文本随 transformers 版本略有差异，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么预训练 checkpoint 里没有分类头权重，加载器不干脆报错退出？
**答案**：随机初始化新头是**设计意图**而非事故——预训练目标是通用的「预测下一词」，不可能预知下游有几个类别；换头微调正是「预训练—微调」范式的标准动作，警告只是提醒使用者：这个头还没训练，别直接拿去推理。

**练习 2**：GPT-2 做句子分类时为什么取**最后一个**词元的隐状态，而 BERT 取 `[CLS]`？
**答案**：GPT-2 带因果掩码，第 \(i\) 个位置的上下文向量只含前 \(i\) 个词的信息，只有最后一个位置的向量「看完了」整句；BERT 是双向的，`[CLS]` 在预训练中就被训练为聚合整句信息的槽位（下一句预测任务）。

**练习 3**：微调时如果用 `BertTokenizer` 给 GPT-2 编码输入，会发生什么？
**答案**：词表完全不同（BertTokenizer 约 3 万词级词表，GPT-2 是 50257 的 BPE 词表），`input_ids` 对不上 `wte` 嵌入矩阵的行数，轻则前向直接维度报错，重则语义错乱。分词器必须与预训练模型配套使用。

### 4.2 param.requires_grad 逐层冻结策略

#### 4.2.1 概念说明

微调时有两个极端选择（承接 u14-l3 迁移学习）：

- **特征提取（冻结基础层）**：把 `param.requires_grad = False` 施加到 Transformer 主体所有参数，只训练新分类头。预训练特征被当作冻结的特征提取器，可训练参数从上亿骤降到数千，小数据集上不易过拟合、训练快。
- **全参数微调（不冻结）**：主体与分类头一起更新，表达能力最强，但小数据集上易过拟合，且需要更多显存（要为所有参数存梯度与优化器状态）。

介于两者之间的是**逐层部分冻结**：从输出端往输入端逐块解冻，或只冻结嵌入层。`requires_grad` 的机制含义：置 `False` 后该参数不进计算图的梯度路径，`backward()` 不为它算梯度、优化器虽持有它但没有 `.grad` 可更新，前向照常使用预训练值。

注意一个细节：优化器要在冻结**之后**再创建。若先把全部参数交给 `Adam` 再冻结，优化器仍持有全部参数的引用；标准做法是先冻结、再用 `filter(lambda p: p.requires_grad, model.parameters())` 或冻结后的参数列表建优化器。

#### 4.2.2 核心流程

```text
model = AutoModelForSequenceClassification.from_pretrained('gpt2', num_labels=2)
        ↓
for name, param in model.named_parameters():      # 遍历所有命名参数
    if name.startswith('transformer'):            # 主体（transformer.*）
        param.requires_grad = False               #   → 冻结
        ↓
统计：sum(p.numel() for p in model.parameters() if p.requires_grad)
        ↓
optimizer = torch.optim.AdamW(可训练参数, lr=...)   # 冻结之后再建优化器
```

#### 4.2.3 源码精读

仓库的 ch16 代码没有冻结操作，但 `requires_grad` 机制在 [ch13/ch13_part1.py:101-104](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L101-L104) 有最简演示：`torch.tensor(3.14, requires_grad=True)` 创建即声明「要梯度」，打印显示 `tensor(3.1400, requires_grad=True)`。[ch13/ch13_part1.py:114-123](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L114-L123) 则演示了属性的两面：默认张量 `w.requires_grad` 为 `False`，调用 `w.requires_grad_()`（原地方法）后翻转为 `True`。微调里的「冻结」只是同一开关的反向使用：把预训练参数从 `True` 拨到 `False`。

真实代码里最接近冻结思想的是 u14-l3 讲过的迁移学习两步法（冻结基座、只训新头），同样仓库未收录其代码。

#### 4.2.4 代码实践

> **示例代码**（GPT-2 冻结，机制锚定在 ch13_part1.py 的 requires_grad 演示）

1. **实践目标**：统计冻结前后的可训练参数量，得到 frozen vs trainable 对比。
2. **操作步骤**：

```python
# 示例代码：冻结 GPT-2 主体，只留分类头可训练
def count_params(model):
    total = sum(p.numel() for p in model.parameters())
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    return total, trainable

# model 来自 4.1.4 的 AutoModelForSequenceClassification.from_pretrained('gpt2', num_labels=2)
print('冻结前:', count_params(model))        # 主体 + 随机初始化的头全部可训练

for name, param in model.named_parameters():
    if name.startswith('transformer'):       # GPT-2 主体模块名为 transformer
        param.requires_grad = False

print('冻结后:', count_params(model))        # 只剩 score 头可训练
```

3. **需要观察的现象**：冻结前可训练量约 1.2 亿量级；冻结后骤降至 1536（= 768 × 2，`score` 头的权重元素数）。
4. **预期结果**：可训练参数缩减约 8 万倍——这就是小数据集上特征提取法不易过拟合、训练便宜的量化根源。
5. GPT-2 small 的确切总参数数与警告文本随版本而异，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：冻结后忘记重建优化器，仍用冻结前的 `Adam(model.parameters())`，训练还能正常「只更新头部」吗？有什么隐患？
**答案**：功能上大概率仍对：被冻结参数没有 `.grad`，Adam 跳过它们；但优化器仍持有全部参数引用，浪费内存，且某些调度器/框架会遍历全部参数组，规范做法是冻结后用 `requires_grad` 过滤再建优化器。

**练习 2**：为什么冻结主要利好**小数据集**场景？大数据集下全参数微调反而常见，为什么？
**答案**：可训练参数越少，模型有效容量越小，过拟合风险越低、优化要找的参数越少——数据不足时这是优点。数据充足时瓶颈变成模型表达能力，冻结主体限制了任务适配上限，全参数微调收益更大且过拟合风险被数据量稀释。

**练习 3**：写出一行代码，只解冻 GPT-2 的最后一个 Transformer 块（参数名形如 `transformer.h.11.*`）。
**答案**：`for name, p in model.named_parameters(): p.requires_grad = name.startswith('score') or name.startswith('transformer.h.11')`（连同分类头一起解冻最后一个块；GPT-2 small 共 12 块，编号 0–11）。

### 4.3 compute_accuracy 与 dataset 采样

#### 4.3.1 概念说明

微调的数据管线与 u12-l2 的 Dataset/DataLoader 契约完全一致，只是样本从「图片张量」换成「分词器输出」。仓库真实的 IMDb 管线（ch16-part3-bert.py）分四步：

1. **下载**：从仓库自身拉取第 8 章整理好的 `movie_data.csv.gz`（5 万条影评、正负各半）；
2. **采样划分**：前 35000 条训练、35000–40000 验证、40000 之后共 1 万条测试；
3. **分词**：`tokenizer(texts, truncation=True, padding=True)` 一次性编码——`truncation` 截断超长影评到模型上限，`padding` 把每批补齐到同长；
4. **封装**：`IMDbDataset` 实现上一讲学的 `__getitem__`/`__len__` 契约，每个样本返回 `input_ids`、`attention_mask`、`labels` 三个张量组成的字典。

`compute_accuracy` 是评估函数：`torch.no_grad()` 下遍历 DataLoader，前向取 `logits`、`argmax` 得预测标签，与真值比对累计正确数，最后返回百分比。它与 u13-l3 torchmetrics 的 update/compute 累计式是同一件事的手写版。

#### 4.3.2 核心流程

```text
下载 movie_data.csv.gz → 解压 → pandas 读入
        ↓ 按行号切三段（35000 / 5000 / 10000）
tokenizer(list(texts), truncation=True, padding=True)   # 得 encodings 字典
        ↓
IMDbDataset(encodings, labels)   #   __getitem__ 把第 idx 条编码 + 标签打包成张量字典
        ↓
DataLoader(batch_size=16, shuffle=训练集True)
        ↓
训练循环里逐批：前向(含labels)→loss/logits → backward → step
每个 epoch 末：compute_accuracy(train_loader) 与 compute_accuracy(valid_loader)
```

#### 4.3.3 源码精读

**① 数据下载与读入**。[ch16/ch16-part3-bert.py:101-110](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part3-bert.py#L101-L110) 用 `requests.get` 从 `https://github.com/rasbt/machine-learning-book/raw/main/ch08/movie_data.csv.gz` 下载、`gzip.open + shutil.copyfileobj` 解压成 CSV，再由 [ch16/ch16-part3-bert.py:117-123](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part3-bert.py#L117-L123) 用 pandas 读入检查。数据文件就住在仓库 [ch08/movie_data.csv.gz](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/movie_data.csv.gz)。

**② 采样划分**。[ch16/ch16-part3-bert.py:130-137](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part3-bert.py#L130-L137) 用 `df.iloc` 按行号切出 `train_texts/train_labels`（0–34999）、`valid_texts/valid_labels`（35000–39999）、`test_texts/test_labels`（40000–49999）。注意这与 u8-l1 的 `train_test_split` 随机划分不同，是**按位置切块**——依赖 CSV 本身已 shuffle（第 8 章整理时已打乱）。

**③ 分词**。[ch16/ch16-part3-bert.py:144-151](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part3-bert.py#L144-L151) 创建 `DistilBertTokenizerFast` 并对三个集合分别 `tokenizer(list(texts), truncation=True, padding=True)`，返回的 `encodings` 是含 `input_ids`、`attention_mask` 的字典（例：`train_encodings[0]` 即第一条影评的编码）。

**④ Dataset 契约**。[ch16/ch16-part3-bert.py:163-174](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part3-bert.py#L163-L174) 的 `IMDbDataset`：`__getitem__` 把第 `idx` 条的每个编码字段转成张量、附上 `item['labels']`；`__len__` 返回标签数。这正是 u12-l2 的 duck typing 契约，DataLoader 据此自动堆批。

**⑤ DataLoader**。[ch16/ch16-part3-bert.py:184-186](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part3-bert.py#L184-L186) 建三个加载器：训练集 `batch_size=16, shuffle=True`，验证/测试集不洗牌。

**⑥ compute_accuracy**。[ch16/ch16-part3-bert.py:204-220](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part3-bert.py#L204-L220) 在 `torch.no_grad()` 下遍历加载器：`outputs = model(input_ids, attention_mask=attention_mask)`（评估不传 labels，只取 `outputs['logits']`），`torch.argmax(logits, 1)` 逐行取最大下标为预测，累计 `correct_pred` 与 `num_examples`，返回 `correct_pred.float()/num_examples * 100`。

**⑦ 训练循环**。[ch16/ch16-part3-bert.py:225-264](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part3-bert.py#L225-L264) 是 u12-l3 五步循环的 transformers 版：每批 `model(input_ids, attention_mask=..., labels=labels)` 前向（模型内部算交叉熵），`outputs['loss'], outputs['logits']` 取损失与 logits，`optim.zero_grad() → loss.backward() → optim.step()`；每个 epoch 末 `model.eval()` 后打印 `compute_accuracy(train_loader)` 与 `compute_accuracy(valid_loader)`，全部轮次结束再报测试准确率。notebook 实际运行 3 个 epoch 后测试准确率约 92–93%（GPU 环境，**待本地验证**）。

#### 4.3.4 代码实践

1. **实践目标**：不动模型，先把 GPT-2 版数据管线跑通，检查批张量形状。
2. **操作步骤**：复用仓库真实的下载与划分代码（ch16-part3-bert.py ①②），改分词器与样本量为 GPT-2 版：

```python
# 示例代码：GPT-2 版数据管线（下载与 iloc 划分抄自 ch16-part3-bert.py:101-137）
import gzip, shutil, requests, pandas as pd, torch
from transformers import AutoTokenizer

url = "https://github.com/rasbt/machine-learning-book/raw/main/ch08/movie_data.csv.gz"
with open('movie_data.csv.gz', 'wb') as f:
    f.write(requests.get(url).content)
with gzip.open('movie_data.csv.gz', 'rb') as f_in, open('movie_data.csv', 'wb') as f_out:
    shutil.copyfileobj(f_in, f_out)
df = pd.read_csv('movie_data.csv')

# 小子集采样（CPU 实验用；正式微调用 35000/5000/10000 划分）
train_texts  = df.iloc[:200]['review'].values;    train_labels  = df.iloc[:200]['sentiment'].values
valid_texts  = df.iloc[200:300]['review'].values; valid_labels  = df.iloc[200:300]['sentiment'].values

tokenizer = AutoTokenizer.from_pretrained('gpt2')
tokenizer.pad_token = tokenizer.eos_token   # GPT-2 默认无 pad 词元，借 eos 充当以便批量补齐

train_enc = tokenizer(list(train_texts), truncation=True, padding=True, max_length=128)
# ……valid 同理；然后仿 ch16-part3-bert.py:163-186 写 IMDbDataset 与 DataLoader
```

3. **需要观察的现象**：`train_enc['input_ids']` 每条长度相同（都 padding 到批内最长、且被 `max_length` 截断到 128）；GPT-2 的 pad 词元 id 是 50256（即 `<|endoftext|>`）。
4. **预期结果**：DataLoader 吐出的批是 `input_ids (B, L)`、`attention_mask (B, L)`、`labels (B,)`——与 ch16-part3-bert.py 的批结构完全同构。
5. `pad_token` 借用 eos 是社区通行做法，**待本地验证**当前 transformers 版本行为。

#### 4.3.5 小练习与答案

**练习 1**：评估循环里 `compute_accuracy` 为什么不把 `labels` 传给模型？
**答案**：传 `labels` 会让模型内部计算损失，评估只需要 logits 做预测；不传标签省掉损失计算，函数因此对「有无标签」的加载器都通用（见 ch16-part3-bert.py:214 只取 `outputs['logits']`）。

**练习 2**：`truncation=True, padding=True` 各解决什么问题？为什么必须有 `attention_mask`？
**答案**：`truncation` 把超长影评截到模型最大长度（如 GPT-2 的 1024），防止位置嵌入越界；`padding` 把同批样本补齐到等长，才能堆成一个矩形张量。补齐位是无效信息，`attention_mask`（有效位 1、补齐位 0）告诉注意力层忽略它们——否则 `<pad>` 词元也会参与注意力计算。

**练习 3**：bonus notebook 的 `IMDBDataset`（[distilbert_finetuning-full.ipynb:1213-1215，原始 JSON 行](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/bonus-distilbert-lightning-trainer/distilbert_finetuning-full.ipynb#L1213-L1215)）比 ch16-part3 的 `IMDbDataset` 更薄，为什么它能这么薄？
**答案**：它直接包裹 HuggingFace `datasets` 库的字典（`dataset_dict[partition_key]`），分词已由 `imdb_tokenized = imdb_dataset.map(tokenize_text, ...)` 批量做完且 `set_format("torch")` 已定好张量格式，`__getitem__` 只需转发下标；ch16-part3 则自己维护 Python list 编码并手工 `torch.tensor`。

### 4.4 AdamW 线性衰减调度

#### 4.4.1 概念说明

**AdamW** 是 Adam 加「解耦权重衰减」：Adam 把 L2 惩罚加进梯度会与自适应学习率纠缠，AdamW 直接在参数更新后乘性收缩 \( \theta \leftarrow \theta - \eta\,\lambda\,\theta \)，正则效果更稳定。Transformer 微调的标准配方是 AdamW + 小学习率（如 5e-5）。

**线性衰减调度（linear warmup + linear decay）**回答「学习率随时间怎么变」：

- **warmup 阶段**（前 \(T_w\) 步）：随机初始化的分类头让最初几步梯度又大又乱，学习率从 0（或极小）线性爬升到峰值 \( \eta_{\max} \)，避免一开场大步走歪；
- **衰减阶段**（其余步数）：学习率线性降到 0，越到后期步子越小，便于收敛到更平的最小值附近：

\[ \eta_t = \begin{cases} \eta_{\max}\cdot\dfrac{t}{T_w} & t \le T_w \\[2mm] \eta_{\max}\cdot\dfrac{T - t}{T - T_w} & T_w < t \le T \end{cases} \]

其中 \( T \) 是总训练步数（epoch 数 × 每 epoch 批数）。这套调度是 BERT/GPT 微调论文沿用的惯例，HuggingFace `Trainer` 默认就挂了线性衰减调度器。仓库代码用的是普通 `Adam` 且无调度器（[ch16/ch16-part3-bert.py:197](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part3-bert.py#L197) 与 bonus notebook 的 `configure_optimizers`，见 [distilbert_finetuning-full.ipynb:1408-1410，原始 JSON 行](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/bonus-distilbert-lightning-trainer/distilbert_finetuning-full.ipynb#L1408-L1410)）；AdamW + 线性衰减是社区标准加强版，本节按示例代码讲解。

#### 4.4.2 核心流程

```text
总步数 T = num_epochs × len(train_loader)   预热步数 T_w（如 10% 总步数）
        ↓
optimizer = AdamW(可训练参数, lr=5e-5)
scheduler = get_linear_schedule_with_warmup(optimizer, num_warmup_steps=T_w,
                                            num_training_steps=T)
        ↓
每个 batch：loss.backward() → optimizer.step() → scheduler.step()
（注意：调度器也每批 step 一次，随批推进线性改变学习率）
```

#### 4.4.3 源码精读

仓库中优化器的真实写法：[ch16/ch16-part3-bert.py:193-197](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part3-bert.py#L193-L197) 加载 `DistilBertForSequenceClassification` 后 `optim = torch.optim.Adam(model.parameters(), lr=5e-5)`——固定学习率 5e-5、无调度、无冻结（全参数微调）。训练/验证准确率随 epoch 的解读范式在 [ch16/ch16-part3-bert.py:253-261](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part3-bert.py#L253-L261)：每个 epoch 末打印 `Training accuracy` 与 `Valid accuracy`，若训练准确率持续走高而验证准确率停滞或回落，即为过拟合信号（u6-l1 学习曲线诊断的 per-epoch 版）。

#### 4.4.4 代码实践

> **示例代码**（仓库微调代码用普通 Adam；AdamW + 线性衰减为标准加强写法）

1. **实践目标**：把调度器接进训练循环，并可视化学习率轨迹。
2. **操作步骤**：

```python
# 示例代码：AdamW + 线性衰减
from transformers import get_linear_schedule_with_warmup

num_epochs = 1
total_steps = num_epochs * len(train_loader)          # 综合实践中 train_loader 的批数
warmup_steps = int(0.1 * total_steps)

optimizer = torch.optim.AdamW(
    [p for p in model.parameters() if p.requires_grad],   # 冻结后建优化器（4.2）
    lr=5e-5)
scheduler = get_linear_schedule_with_warmup(
    optimizer, num_warmup_steps=warmup_steps, num_training_steps=total_steps)

lrs = []
for batch in train_loader:
    # ……前向 / loss.backward() 略
    optimizer.step()
    scheduler.step()
    lrs.append(scheduler.get_last_lr()[0])            # 记录当前学习率
# 画 lrs 曲线：先线性爬升（前 10% 步）再线性降到接近 0
```

3. **需要观察的现象**：学习率曲线呈「帐篷形」——前 10% 步从 0 爬到 5e-5，随后线性下降，最后一个 batch 时接近 0。
4. **预期结果**：`lrs[0]` 很小、`lrs[warmup_steps]` 处达峰 5e-5、末尾趋近 0；训练损失比无调度版更平稳（是否更优需实测，**待本地验证**）。
5. 忘记每批调用 `scheduler.step()` 是常见 bug——学习率将全程恒定。

#### 4.4.5 小练习与答案

**练习 1**：为什么 warmup 对「换头微调」特别重要？
**答案**：分类头是随机初始化的，最初几批的梯度方向基本由噪声主导；若一开始就用大学习率，反向传播的大步更新会通过共享的注意力通路波及预训练主体，破坏好特征。小步爬升让头部先「热身」到不太离谱的区域。

**练习 2**：总步数 \( T\) 算错（例如只算了一个 epoch 却训练三个）会发生什么？
**答案**：调度器在第一个 epoch 末就衰减到 0，后面两个 epoch 学习率为 0（或极小），等于白训——参数几乎不再更新，损失曲线走平。

**练习 3**：`optimizer.step()` 与 `scheduler.step()` 的调用顺序有要求吗？
**答案**：有：先 `optimizer.step()` 再 `scheduler.step()`。反着来第一步就会用「尚未更新」的计数读学习率，且 PyTorch 会给出警告；顺序颠倒会让整个调度错位一步。

## 5. 综合实践

**任务**：把四个模块串成一个完整的 GPT-2 冻结微调实验——统计冻结前后参数量 → 在 IMDb 子集上训练 1 个 epoch（AdamW + 线性衰减）→ 绘制 train/valid 准确率曲线。

> 说明：数据下载与划分复用仓库真实代码（ch16-part3-bert.py:101-137）；GPT-2 换头、冻结、AdamW 调度为**示例代码**（仓库未收录 GPT-2 微调）。

**步骤**：

```python
# ========== 综合实践（示例代码，依赖 4.3.4 已下载的 movie_data.csv）==========
import time, torch, pandas as pd
import matplotlib.pyplot as plt
from transformers import AutoTokenizer, AutoModelForSequenceClassification, get_linear_schedule_with_warmup

torch.manual_seed(123)
DEVICE = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

# ① 数据：真实下载代码见 4.3.4；这里取小子集（正式实验用 35000/5000）
df = pd.read_csv('movie_data.csv')
N_TRAIN, N_VALID = 2000, 500            # CPU 建议 200/100；GPU 可加大
train_texts  = df.iloc[:N_TRAIN]['review'].values.tolist()
train_labels = df.iloc[:N_TRAIN]['sentiment'].values.tolist()
valid_texts  = df.iloc[N_TRAIN:N_TRAIN+N_VALID]['review'].values.tolist()
valid_labels = df.iloc[N_TRAIN:N_TRAIN+N_VALID]['sentiment'].values.tolist()

# ② 换头加载（4.1）
tokenizer = AutoTokenizer.from_pretrained('gpt2')
tokenizer.pad_token = tokenizer.eos_token
model = AutoModelForSequenceClassification.from_pretrained('gpt2', num_labels=2)

# ③ 冻结主体 + 参数量对比（4.2）
def count_trainable(m):
    return sum(p.numel() for p in m.parameters() if p.requires_grad)
print(f'冻结前可训练参数量: {count_trainable(model):,}')
for name, p in model.named_parameters():
    if name.startswith('transformer'):
        p.requires_grad = False
print(f'冻结后可训练参数量: {count_trainable(model):,}')   # 预期只剩 score 头的 1,536
model.to(DEVICE)

# ④ Dataset/DataLoader（契约抄自 ch16-part3-bert.py:163-186）
class IMDbDataset(torch.utils.data.Dataset):
    def __init__(self, encodings, labels):
        self.encodings, self.labels = encodings, labels
    def __getitem__(self, idx):
        item = {k: torch.tensor(v[idx]) for k, v in self.encodings.items()}
        item['labels'] = torch.tensor(self.labels[idx])
        return item
    def __len__(self):
        return len(self.labels)

train_enc = tokenizer(train_texts, truncation=True, padding=True, max_length=256)
valid_enc = tokenizer(valid_texts, truncation=True, padding=True, max_length=256)
train_loader = torch.utils.data.DataLoader(IMDbDataset(train_enc, train_labels),
                                           batch_size=8, shuffle=True)
valid_loader = torch.utils.data.DataLoader(IMDbDataset(valid_enc, valid_labels),
                                           batch_size=8, shuffle=False)

# ⑤ 评估函数（抄自 ch16-part3-bert.py:204-220）
def compute_accuracy(model, data_loader, device):
    with torch.no_grad():
        correct_pred, num_examples = 0, 0
        for batch in data_loader:
            input_ids = batch['input_ids'].to(device)
            attention_mask = batch['attention_mask'].to(device)
            labels = batch['labels'].to(device)
            logits = model(input_ids, attention_mask=attention_mask)['logits']
            predicted_labels = torch.argmax(logits, 1)
            num_examples += labels.size(0)
            correct_pred += (predicted_labels == labels).sum()
        return correct_pred.float() / num_examples * 100

# ⑥ AdamW + 线性衰减（4.4）
NUM_EPOCHS = 1
optimizer = torch.optim.AdamW(
    [p for p in model.parameters() if p.requires_grad], lr=5e-5)
scheduler = get_linear_schedule_with_warmup(
    optimizer,
    num_warmup_steps=int(0.1 * NUM_EPOCHS * len(train_loader)),
    num_training_steps=NUM_EPOCHS * len(train_loader))

# ⑦ 训练循环（结构抄自 ch16-part3-bert.py:225-264）+ 每批记录准确率
train_acc_hist, valid_acc_hist, steps = [], [], []
start = time.time()
for epoch in range(NUM_EPOCHS):
    model.train()
    for batch_idx, batch in enumerate(train_loader):
        input_ids = batch['input_ids'].to(DEVICE)
        attention_mask = batch['attention_mask'].to(DEVICE)
        labels = batch['labels'].to(DEVICE)
        outputs = model(input_ids, attention_mask=attention_mask, labels=labels)
        loss, logits = outputs['loss'], outputs['logits']
        optimizer.zero_grad(); loss.backward(); optimizer.step(); scheduler.step()

        if batch_idx % 10 == 0:                      # 每 10 批在两个集上各测一次
            model.eval()
            train_acc_hist.append(compute_accuracy(model, train_loader, DEVICE).item())
            valid_acc_hist.append(compute_accuracy(model, valid_loader, DEVICE).item())
            steps.append(batch_idx)
            model.train()
            print(f'Batch {batch_idx:04d} | Loss {loss:.4f} | '
                  f'train acc {train_acc_hist[-1]:.2f}% | valid acc {valid_acc_hist[-1]:.2f}%')

print(f'耗时 {(time.time()-start)/60:.1f} min | 最后一轮验证准确率: {valid_acc_hist[-1]:.2f}%')

# ⑧ 曲线
plt.plot(steps, train_acc_hist, label='Training accuracy')
plt.plot(steps, valid_acc_hist, label='Validation accuracy')
plt.xlabel('Training batch'); plt.ylabel('Accuracy (%)'); plt.legend(); plt.show()
```

**观察要点**：

1. **参数量对比**：冻结前约 1.24 亿（`transformer` + `score`），冻结后 1,536——八万倍的差距（确切数值**待本地验证**）。
2. **准确率曲线**：两条曲线都从约 50%（随机二分类基线，注意随机初始化的头此时输出与输入无关）快速爬升；冻结版因只训头部，曲线通常更平滑但上限略低。
3. **最后一轮验证准确率**：小子集（2000 条）训练 1 个 epoch，合理预期在 80% 上下浮动；数据越少、批次越少，波动越大（**待本地验证**——具体数值依赖子集、硬件与版本）。
4. **资源提示**：GPT-2（约 548 MB 权重）上 CPU 训练一个批就要数秒，务必先用 200/100 的小子集跑通流程；正式实验建议在 Colab GPU 上把样本量加到 35000/5000（ch16-part3 的完整划分），batch_size 可提到 16。
5. **进阶对照**：把 ③ 的冻结循环注释掉重跑（全参数微调），对比两条验证曲线与小数据集上的过拟合速度差异。

## 6. 本讲小结

- **加载即换头**：`AutoModelForSequenceClassification.from_pretrained(model, num_labels=2)` 让主体权重来自预训练 checkpoint、分类头（GPT-2 的 `score`：768→2）随机初始化——HuggingFace 的「newly initialized」警告是设计说明而非错误，换头后必须训练才能预测。
- **仓库的真实参照**：`ch16-part2-gpt2.py` 收录 GPT-2 的 pipeline 生成、分词器编码（`input_ids` + `attention_mask`）与 `GPT2Model` 隐状态 `(1, 5, 768)`；换头加载与 Lightning 微调在 bonus notebook，数据管线、`compute_accuracy` 与训练循环在 `ch16-part3-bert.py`——GPT-2 特有的微调写法需按这些真实模式自行组装。
- **冻结的本质**：`param.requires_grad = False` 让反向传播跳过预训练主体，只训新头；可训练参数从约 1.24 亿降到 1536，是小数据集上防过拟合、省算力的关键手段；优化器应在冻结之后再创建。
- **评估看曲线**：`compute_accuracy` 以 `no_grad` + `argmax(logits)` 逐批累计；每 epoch 打印训练/验证准确率，训练涨验证停即为过拟合信号。
- **学习率也要「训练」**：AdamW（解耦权重衰减的 Adam）配「warmup 爬升 + 线性衰减」调度器 \(\eta_t=\eta_{\max}\cdot(T-t)/(T-T_w)\)，是 Transformer 微调的标准配方；调度器每批 `step()` 一次，总步数按 epoch × 批数计算。

## 7. 下一步学习建议

1. **下一讲 u16-l3（BERT 微调与 HuggingFace 生态）**：本讲手写的训练循环在 `Trainer` + `TrainingArguments` 里被标准化——学习率调度器、评估、日志全部自动接好，并用 `compute_metrics` 回调替代手写 `compute_accuracy`；对照本讲 4.3 的手写版阅读，体会框架替你做了什么。
2. **延伸阅读源码**：bonus notebook [ch16/bonus-distilbert-lightning-trainer/distilbert_finetuning-full.ipynb](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/bonus-distilbert-lightning-trainer/distilbert_finetuning-full.ipynb) 把本讲的全部零件装进了 Lightning：`IMDBDataset`、`AutoModelForSequenceClassification`、`configure_optimizers`、`ModelCheckpoint(monitor="val_acc")`，可视为本讲综合实践的「官方答案」。
3. **回看 u14-l3（迁移学习）**：CNN 的「冻结基座、替换分类头」与本讲 GPT-2 的做法逐条对应，两个领域共享同一套迁移学习方法论。
4. **动手方向**：把综合实践改成「逐层解冻」实验——先只训 `score`，再解冻 `transformer.h.11`，最后全量微调，比较三种冻结粒度在同一个小子集上的验证曲线。

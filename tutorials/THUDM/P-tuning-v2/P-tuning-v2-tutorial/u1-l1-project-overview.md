# 项目定位与深度提示调优原理

## 1. 本讲目标

本讲是整本《P-tuning v2 学习手册》的第一篇。读完本讲，你应该能够：

1. 说清楚 **P-tuning v2 到底解决什么问题**：为什么在「全量微调（fine-tuning）」之外，还需要一种参数高效的微调方法。
2. 理解 **深度提示调优（deep prompt tuning）** 的核心思想——冻结预训练主干，只在每一层注入一小段「可训练的连续前缀」。
3. 看懂 README 中给出的 **论文复现结果表**，建立对项目能力的整体认知：在哪些任务、哪些主干模型上，P-tuning v2 能「可比拟微调（comparable to fine-tuning）」。

本讲**不会**带你逐行读模型代码——那是后续进阶讲义的工作。本讲的唯一任务是帮你建立正确的「直觉和定位」，让你知道这个项目为什么值得学、学它能拿到什么。

---

## 2. 前置知识

为了理解本讲，建议你先了解以下基础概念（不需要很深入，有印象即可）：

### 2.1 预训练语言模型（PLM）

像 BERT、RoBERTa、DeBERTa 这类模型，先在海量无标注文本上做「预训练」，学到通用的语言表示；再用一个具体任务（比如情感分类、命名实体识别、问答）的少量标注数据，对模型参数做调整，这个过程叫**微调（fine-tuning）**。

- **全量微调**：把模型所有参数都设为「可训练」，用任务的损失函数更新它们。
- 缺点：模型越大，可训练参数越多，训练/部署成本越高；而且在数据少时容易过拟合。

### 2.2 自注意力的 key / value

Transformer 每一层都有自注意力（self-attention）机制，对每个输入位置计算三组向量——query（Q）、key（K）、value（V）：

\[
\text{Attention}(Q,K,V) = \text{softmax}\!\left(\frac{QK^\top}{\sqrt{d_k}}\right)V
\]

你只需要记住一个关键点：**key 和 value 是每一层、每一个位置都会计算并使用的内部状态**。P-tuning v2 的「前缀」正是被插入到这里的 key/value 序列里——这是一个伏笔，第 4.1 节会展开。

### 2.3 参数高效微调（Parameter-Efficient Fine-Tuning, PEFT）

这是一类方法的统称：**只训练极少量额外参数，固定（冻结）主干网络的大部分参数**，就能逼近甚至达到全量微调的效果。P-tuning v2 正是 PEFT 家族中的一员。本讲第 4.1 节会把它的几位「亲戚」放在一起对比。

> 如果你完全不熟悉上述内容也不必担心——本讲会从直觉讲起，遇到术语都会解释。后续讲义会逐步带你读真实代码。

---

## 3. 本讲源码地图

本讲聚焦项目定位与原理，主要阅读的源码很少，集中在项目说明文档上：

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/README.md) | 项目总说明：论文出处、原理一句话定义、环境搭建、复现结果表。**本讲最主要的数据来源。** |
| [figures/P-tuning-v2.png](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/figures/P-tuning-v2.png) | README 中引用的原理示意图，展示前缀如何在每一层注入。 |
| `model/`、`tasks/`、`training/`、`run_script/` | 代码目录，本讲只做「定位」了解，精读留到后续讲义。 |

> 说明：本讲引用的「源码」主要是文档与结果表。真正的模型实现（如 `model/prefix_encoder.py`）会在第 2 单元精读，这里只点到为止。

---

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：

1. **deep prompt tuning 概念**：什么是深度提示调优，它和浅层提示的区别。
2. **冻结主干与可训练 prefix 参数**：哪些参数被冻结、哪些被训练，参数量级差多少。
3. **论文复现结果概览**：在不同任务和主干上的实际指标。

---

### 4.1 deep prompt tuning（深度提示调优）概念

#### 4.1.1 概念说明

先看 README 里对 P-tuning v2 的**一句话定义**：

> P-tuning v2 leverages **deep prompt tuning**, which is to apply continuous prompts for every layer input of the pretrained transformer.

拆解三个关键词：

- **prompt（提示）**：在输入前面拼接的一段内容。你也许见过「离散提示」——比如给情感分类任务加一句「这部电影真是太 [MASK] 了」。这种用自然语言写成的提示是离散的、不可微的。
- **continuous prompt（连续提示）**：不再用真实词，而是直接把**一段可学习的向量（浮点数）**拼到输入前面。因为向量可以跟着损失一起被梯度更新，所以叫「连续 / 可微」。
- **deep / for every layer（深度 / 逐层）**：最关键的一点。早期的 prompt tuning 只在**输入层（第 0 层）**加一段提示，后面所有层被动地处理它；P-tuning v2 则在 Transformer 的**每一层**都注入一段提示。

为什么「逐层」这么重要？因为浅层提示一旦经过多层传播，影响力会被稀释、可调能力有限（论文里称为「capacity 不足」）。逐层注入相当于让提示的能力「贯穿整个网络」，从而在小模型和困难任务上也能逼近全量微调。README 紧接着的那句话正是这个意思：

> Deep prompt tuning increases the capacity of continuous prompts and closes the gap to fine-tuning across various settings, especially for small models and hard tasks.

#### 4.1.2 核心流程

用文字画一张「前向传播时提示是如何参与计算的」流程：

```
输入 token ids
   │
   ▼  (正常路径) 冻结的 BERT/RoBERTa 主干，一层层往下走
每一层 l 的自注意力，本来只看到「真实 token」的 key/value
   │
   ▼  (P-tuning v2 的改动) 把一小段「可训练前缀」的 key/value
       拼接到该层真实 token 的 key/value 前面
   │
   ▼  注意力在前缀 + 真实 token 上一起计算
   ▼  主干参数始终冻结；只有「前缀」对应的少量参数被更新
```

如果用数学化一点的方式描述：设主干共有 \(L\) 层、前缀长度为 \(P\)（即 `pre_seq_len`）、隐藏维度为 \(H\)。在第 \(l\) 层，原本的 key/value 来自真实 token；P-tuning v2 在它们前面额外拼上长度为 \(P\) 的前缀 key/value，于是该层注意力的作用范围从「真实序列长度」扩展到「\(P\) + 真实序列长度」。

#### 4.1.3 源码精读

本讲主要阅读 README。下面几处值得精读：

1. **论文出处与项目定位**——README 开篇说明这是 ACL 2022 论文的官方实现：

   [README.md:4-8](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/README.md#L4-L8) 告诉我们：项目是论文 *P-Tuning v2: Prompt Tuning Can Be Comparable to Finetuning Universally Across Scales and Tasks* 的源码，目标是「在中小模型和序列标注任务上，用优化过的 prompt tuning 达到与 fine-tuning 相当的效果」。

2. **深度提示调优的一句话定义**——这是全项目最重要的一句话原理：

   [README.md:14-15](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/README.md#L14-L15) 定义了「deep prompt tuning = 对预训练 Transformer 的每一层输入施加连续提示」，并指出它能「缩小与 fine-tuning 的差距，尤其在小模型与困难任务上」。

3. **原理示意图**：

   [README.md:17](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/README.md#L17) 引用了 `figures/P-tuning-v2.png`。建议你打开这张图，观察前缀（图中的彩色小方块）如何出现在 Transformer 的**每一层**，而不是只出现在最底层。

> 提示：本讲不展开 `model/` 下的真实代码。在第 2 单元 u2-l2「前缀注入主流程」里，我们会精读 `get_prompt` 函数如何把前缀重排成「每一层的 (key, value)」并喂给冻结的主干。

#### 4.1.4 代码实践

> 本实践为**源码阅读型实践**，无需运行任何命令。

1. **实践目标**：从直觉上区分「浅层提示」与「深度（逐层）提示」。
2. **操作步骤**：
   - 打开 [README.md:14-15](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/README.md#L14-L15)，圈出关键词 `for every layer`。
   - 打开原理图 [figures/P-tuning-v2.png](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/figures/P-tuning-v2.png)，数一数：前缀的小方块出现在图中的哪几层？是只在一层，还是在每一层都有？
3. **需要观察的现象**：图中前缀应当出现在主干网络的**多层**位置，而不是只在最底部的输入层。
4. **预期结果**：你会直观看到「逐层注入」的样子——这正是 v2 区别于「只加在输入层」的早期 prompt tuning 的关键。
5. （可选）如果你愿意动手画：在纸上画一个 4 层的 Transformer 柱状图，分别画出「浅层提示（只在第 0 层顶部加一段）」和「深度提示（每一层左侧都加一段）」两种情况，对比两者。

#### 4.1.5 小练习与答案

**练习 1**：为什么把提示「逐层注入」比「只加在输入层」更有表现力？

> **参考答案**：浅层提示只在第 0 层出现，它的影响力要靠后续各层的注意力逐层传递，传播过程中容易被稀释，能调整的「容量」有限；逐层注入相当于在每一层都直接提供新的可调 key/value，让提示的影响贯穿整个网络，因此能逼近全量微调，尤其在数据少、任务难时更明显。

**练习 2**：什么是「连续（continuous）」提示？它和「离散」提示的区别是什么？

> **参考答案**：离散提示是用真实自然语言词（可读）拼在输入前，不可被梯度直接优化；连续提示是直接用一组浮点向量表示前缀，可以和模型一起通过反向传播更新。P-tuning v2 用的是连续提示。

---

### 4.2 冻结主干与可训练 prefix 参数

#### 4.2.1 概念说明

P-tuning v2 的第二条核心原则是：**冻结预训练主干，只训练前缀相关的少量参数**。这正属于「参数高效微调（PEFT）」的范畴。

README 在「常见问题」里非常明确地点出了这一点：

> in P-tuning v2, we follow Prefix tuning and Lester et al.'s parameter-efficient setting where **backbone pre-trained model parameters are frozen**.

也就是说，P-tuning v1 在某些实验里会把主干和提示**一起**微调；而 v2 为了体现「参数高效」，严格冻结主干。这与 Prefix Tuning、Lester 等人的 prompt tuning 保持一致。

先认识一下它的几位「亲戚」：

| 方法 | 提示位置 | 主干是否冻结 | 特点 |
|------|----------|--------------|------|
| **Lester et al. (Prompt Tuning)** | 仅输入层（浅层） | 冻结 | 最简单，但小模型上效果差 |
| **Prefix Tuning** | 每一层的 key/value（深层） | 冻结 | 逐层注入，P-tuning v2 的直接来源之一 |
| **P-tuning v1** | 部分层 | 部分实验不冻结（与 PET 公平比较时一起微调） | v2 的前一版 |
| **P-tuning v2** | **每一层**（深层） | **冻结** | 逐层 + 参数高效，本项目的实现 |

#### 4.2.2 核心流程

用伪代码描述一次 P-tuning v2 训练里「参数的训练/冻结状态」：

```
model = 加载预训练 BERT/RoBERTa 主干          # 几亿参数
for 每个参数 p in model.主干参数:
    p.requires_grad = False                  # 冻结主干

prefix_encoder = PrefixEncoder(config)       # 只新增的少量参数
#   这些参数才会被优化器更新

for 一个 batch:
    past_key_values = prefix_encoder(前缀 id) # 生成每一层要用的前缀
    logits = model(输入, past_key_values)     # 主干用前缀做前向，主干梯度为 0
    loss = 交叉熵(logits, 标签)
    loss.backward()                          # 只有 prefix_encoder 收到梯度
    optimizer.step()                         # 只更新 prefix 参数
```

可训练参数的量级大约是多少？设前缀长度 \(P\)（`pre_seq_len`）、层数 \(L\)、隐藏维度 \(H\)。因为每一层都要提供 key 和 value 两份前缀，前缀编码器的输出维度量级约为：

\[
\text{可训练参数规模} \sim 2 \cdot L \cdot P \cdot H
\]

以 BERT-large 为例（\(L=24, H=1024\)），即使取 \(P=20\)，前缀相关参数也只在百万量级，而主干参数超过 3 亿——两者差了约两个数量级。这就是「参数高效」的字面含义。

#### 4.2.3 源码精读

README 中与「冻结主干」最相关的一处：

[README.md:21-22](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/README.md#L21-L22) 在「Commonly Asked Question」里解释了 SuperGLUE 上 P-tuning v1 与 v2 的差异：v1 在与 PET 公平比较时**会一起微调主干**；而 v2 严格遵循 Prefix tuning / Lester 等人的参数高效设定，**冻结主干**。这一段是理解「为什么 v2 强调参数高效」的关键。

> 补充说明（指向后续讲义）：在代码层面，「冻结主干」具体由 `model/utils.py` 里一个名为 `fix_bert` 的开关控制，开启后会把主干参数的 `requires_grad` 置为 `False`。这个我们会在第 3 单元 u3-l2「模型工厂 get_model 与任务注册表」里精读，本讲先建立概念即可。

#### 4.2.4 代码实践

> 本实践为**估算型 / 源码阅读型实践**，无需运行训练。

1. **实践目标**：用数量级体会「prefix 参数远少于主干参数」。
2. **操作步骤**：
   - 取 BERT-large：\(L=24\) 层、\(H=1024\)、设 \(P=20\)。
   - 按公式估算前缀相关参数规模 \(2 \cdot L \cdot P \cdot H\)。
   - 与主干约 3.4 亿（340M）参数对比，算出比值。
3. **需要观察的现象**：前缀参数量大约只有几千万分之一到百分之几的主干规模。
4. **预期结果**：
   - 估算值约 \(2 \times 24 \times 20 \times 1024 \approx 9.8 \times 10^5\)（约 98 万），与主干 3.4 亿相比不到 0.3%。
   - 结论：只训练千分之几的参数，就能逼近全量微调——这正是参数高效的核心卖点。
5. 如果你后续真的跑起训练（第 2 单元以后），训练日志里会打印 `total param` 和可训练参数数量，到时可以用真实数字验证本估算。

#### 4.2.5 小练习与答案

**练习 1**：P-tuning v2 训练时，BERT 主干的参数会被更新吗？

> **参考答案**：不会。v2 遵循参数高效设定，主干参数被冻结（`requires_grad=False`）。只有前缀编码器（`PrefixEncoder`）及其相关少量参数、以及任务输出头会被优化。

**练习 2**：把 Lester 的浅层 prompt tuning 和 P-tuning v2 放在一起，它们「相同点」和「关键不同点」分别是什么？

> **参考答案**：相同点——都冻结主干、都只在输入侧加连续提示，都是参数高效方法。关键不同点——Lester 只在输入层加提示；P-tuning v2 在**每一层**都注入前缀（深度提示），因此容量更大、在小模型/困难任务上更接近全量微调。

---

### 4.3 论文复现结果概览

#### 4.3.1 概念说明

光讲原理不够，我们还要看「它真的有效吗」。README 提供了两张**复现结果表**，分别是在 BERT-large 和 RoBERTa-large 上的指标。这些数字是理解项目能力最直观的依据。

需要先说明一点：README 标题写的是 **Implemented Results（已实现/已复现结果）**，并特别注明原论文实验是在 NVIDIA DGX-A100 上做的，仓库作者用 **RTX 3090** 重新复现。所以这些数字是「在更易得的硬件上的复现值」，不是论文原文表格。README 也提醒：**最佳超参对环境敏感**，如果你复现不到，可能需要用 `search_script` 做超参搜索。

#### 4.3.2 核心流程

任务可以归为三大类，理解了分类再看表会轻松很多：

```
三大类任务
├── 分类（Classification / 句对判断）
│     BoolQ, COPA, RTE, WiC, WSC   ← SuperGLUE 子集
├── 序列标注（Sequence Tagging / NER / SRL）
│     CoNLL03, CoNLL04, OntoNotes 5.0, CoNLL12, CoNLL05(WSJ/Brown)
└── 抽取式问答（Extractive QA）
      SQuAD 1.1, SQuAD 2.0
```

读表时还要注意两点约定：

- 每张表都有三行：**Result/Results**（指标）、**Total Epochs**（总共训练轮数）、**Best Epoch**（取得最佳指标的那一轮）。
- SQuAD 一列里有两个数字（如 `88.1/94.2`），分别对应 **EM（Exact Match）/ F1**，这是问答任务的标准指标。

#### 4.3.3 源码精读

1. **复现说明与超参敏感性**——理解这些数字的来源与可信边界：

   [README.md:24-34](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/README.md#L24-L34) 说明：原论文用 DGX-A100，仓库用 RTX 3090 + 指定版本的包复现；最佳超参对环境敏感，复现不到时建议用 `search_script` 做搜索。

2. **BERT-large 复现结果表**：

   [README.md:73-79](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/README.md#L73-L79) 给出 BERT-large 在 BoolQ/COPA/RTE/WiC/WSC（分类）和 CoNLL04/OntoNotes 5.0/CoNLL12（序列标注）上的指标。例如 RTE 达到 80.1、CoNLL04（NER）达到 84.5。

3. **RoBERTa-large 复现结果表**：

   [README.md:82-87](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/README.md#L82-L87) 给出 RoBERTa-large 在更多任务上的指标，覆盖分类、序列标注（CoNLL03/04、OntoNotes、CoNLL05）以及问答（SQuAD 1.1 `88.1/94.2`、SQuAD 2.0 `81.3/84.7`）。

4. **「达不到就做超参搜索」的提示**：

   [README.md:89-90](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/README.md#L89-L90) 直接给出建议：若复现不到 Best Epoch 的结果，多半是环境不匹配，需要超参搜索。这也解释了为什么仓库里专门有 `search_script/` 和 `search.py`（第 5 单元 u5-l2 会讲）。

#### 4.3.4 代码实践

> 本实践为**表格阅读 + 总结型实践**，是本讲的主实践任务。

1. **实践目标**：建立「P-tuning v2 在多任务、多主干上可比拟微调」的整体印象。
2. **操作步骤**：
   - 打开 [README.md:73-79](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/README.md#L73-L79) 与 [README.md:82-87](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/README.md#L82-L87)。
   - 分别在「分类」「序列标注」「问答」三类任务中各挑 1～2 个数据集，记录其指标。
   - 用自己的话写一段约 100 字的中文总结。
3. **需要观察的现象**：在只训练极少量前缀参数的前提下，分类、NER、问答都拿到了相当高的指标（如 RoBERTa 在 RTE 上 86.6、CoNLL03 上 91.8、SQuAD 1.1 F1 94.2）。
4. **预期结果（参考样例，约 100 字）**：
   > P-tuning v2 在冻结主干、仅训练少量前缀参数的情况下，仍能逼近全量微调：在 RoBERTa-large 上，分类任务 RTE 达 86.6、BoolQ 达 84.0；序列标注 CoNLL03(NER) 达 91.8、OntoNotes 5.0 达 90.1；抽取式问答 SQuAD 1.1 的 F1 高达 94.2、SQuAD 2.0 达 84.7。BERT-large 同样覆盖分类与标注任务（RTE 80.1、CoNLL04 84.5）。这说明逐层深度提示在中小模型与困难任务上确实「可比拟微调」。
5. 如果某些指标你无法判断其含义（如 EM/F1），可标注「待本地验证/待查阅指标定义」，不要编造。

#### 4.3.5 小练习与答案

**练习 1**：RoBERTa-large 表中 SQuAD 1.1 那一列写的是 `88.1/94.2`，这两个数字分别代表什么？

> **参考答案**：前者是 EM（Exact Match，精确匹配率），后者是 F1。EM 要求预测答案与标准答案完全一致；F1 衡量预测与标准答案的 token 级重叠。F1 通常高于或等于 EM。

**练习 2**：README 强调「最佳超参对环境敏感」，并建议做超参搜索。这说明复现这些结果时需要注意什么？

> **参考答案**：结果依赖于特定的硬件（RTX 3090）、CUDA 与包版本（见 `requirements.txt`），换环境后最佳学习率、前缀长度、轮数等可能不同，复现不到指标时不要怀疑方法本身，而应像仓库建议的那样，用 `search_script` 做超参网格搜索，再用 `search.py` 汇总选出最佳试验。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个综合任务：

**任务：用一张「项目认知卡」总结 P-tuning v2**

请你在一张纸（或一个 markdown 笔记）上，填写以下四栏，全部基于本讲读到的 README 内容：

1. **一句话定位**：P-tuning v2 是什么？（提示：参数高效 + 深度提示 + 可比拟微调）
2. **核心原理**：用「冻结主干 / 逐层注入连续前缀」两个要点，配上第 4.1.2 节的文字流程图。
3. **参数量直觉**：写出「主干被冻结、只有前缀被训练」的事实，并给出第 4.2 节的数量级估算。
4. **能力佐证**：从 [README.md:73-87](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/README.md#L73-L87) 的两张结果表中，挑三个数字（覆盖分类、标注、问答各一），说明它「可比拟微调」。

**验收标准**：如果你能把这张卡片讲给一个没接触过 P-tuning 的同学听懂，本讲就达标了。

---

## 6. 本讲小结

- **P-tuning v2 是参数高效微调方法**：冻结预训练主干，只训练少量额外参数。
- **核心机制是「深度（逐层）提示调优」**：对 Transformer 的**每一层**都注入一段可训练的连续前缀，而不是只加在输入层（[README.md:14-15](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/README.md#L14-L15)）。
- **主干严格冻结**：v2 遵循 Prefix tuning / Lester 等人的参数高效设定，区别于 v1 在部分实验里会一起微调主干（[README.md:21-22](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/README.md#L21-L22)）。
- **参数量极小**：前缀参数量级约 \(2 \cdot L \cdot P \cdot H\)，通常不到主干的 1%。
- **效果可比拟微调**：在 BERT-large / RoBERTa-large 上，覆盖分类、序列标注、抽取式问答，都拿到接近全量微调的指标（[README.md:73-87](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/README.md#L73-L87)）。
- **复现需注意环境敏感性**：最佳超参与硬件/包版本相关，必要时用 `search_script` 做超参搜索。

---

## 7. 下一步学习建议

本讲只建立了**概念与定位**，还没有触碰真实代码。建议你按手册的依赖顺序继续：

1. **先动手把它跑起来**：进入 **u1-l2「环境搭建与首次复现实验」**，跟着 `requirements.txt` 与 `run_script/run_rte_roberta.sh` 发起一次训练，看训练日志里打印的参数量，亲手验证本讲的「参数高效」直觉。
2. **再认识入口**：进入 **u1-l3「目录结构与主入口 run.py」**，画出「命令行参数 → 任务分派 → 模型 → 训练器」的整体心智模型，为后续读模型代码铺路。
3. **真正读机制**：第 2 单元 **u2-l1「PrefixEncoder——前缀编码器」** 会带你逐行精读 `model/prefix_encoder.py`，把本讲「逐层注入前缀」的抽象原理，落地成具体的张量形状与代码。

> 推荐先读论文原文（README 顶部给出的 arXiv 链接）以获得完整图景，再带着本讲建立的直觉回到代码。

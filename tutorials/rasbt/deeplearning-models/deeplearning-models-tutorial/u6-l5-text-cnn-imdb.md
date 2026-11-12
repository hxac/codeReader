# Text CNN：用卷积做 NLP

## 1. 本讲目标

读完本讲，你应该能够：

1. 说清 TextCNN 的三段结构：词嵌入层 → 多尺度一维卷积 → 全局最大池化，并理解它为什么天然适合「一句话里出现没出现某个关键短语」这类分类任务。
2. 掌握 `nn.Conv1d` 的输入输出形状约定，会用公式推导每种卷积核的输出长度与参数量。
3. 理解「max-over-time 池化 + 池化掩码」如何让 padding 不影响预测——这是本 notebook 里最精巧的一段源码。
4. 会按验证集准确率挑选 checkpoint、配合早停组织一次规范的「训练 / 验证 / 一次性测试」实验。

本讲精读的 notebook 是仓库中较新的一份（正是当前 HEAD 提交 "Add CNN for NLP" 引入的），它不依赖 torchtext，从下载 IMDb 数据、手写分词建词表，到训练与评估完全自包含，工程组织也最现代化，非常适合作为「照着写自己的文本分类管线」的模板。

## 2. 前置知识

- **词元、词表与 Embedding（承接 u6-l1）**：文本要先切分成词元（token），只在训练集上统计词频构建词表，再把每个词元映射为整数编号；`nn.Embedding` 是一张可学习的查表，把编号变成稠密向量。本讲的 IMDb 数据管线与 u6-l1 相同，但实现从 torchtext 换成了纯正则手写。
- **卷积与池化（承接 u3-l1）**：二维卷积在图像上滑动一个小窗口做加权求和；一维卷积只是把「高×宽」的滑动窗口换成「沿时间的一维窗口」。二维卷积的输出尺寸公式 \(o=\lfloor(w-k+2p)/s\rfloor+1\) 在一维同样适用，本 notebook 全部使用 \(p=0, s=1\)，于是退化为简单的 \(L_{out}=L-k+1\)。
- **n-gram**：连续的 n 个词元组成的片段，例如 "not good" 是一个 2-gram、"one of the best" 是一个 4-gram。情感往往由这类局部短语决定——这正是用卷积做文本分类的直觉来源。
- **many-to-one 分类（承接 u6-l1）**：整条评论只输出一个 logit 向量。RNN 靠「最后时刻的隐状态」汇总全句，TextCNN 靠「每个卷积核在全文中最强的激活」汇总全句。
- **checkpoint 与早停（承接 u4-l4）**：训练中把验证集上表现最好的权重存盘，测试集只在最后用这份最优权重评估一次。本讲会看到这套流程的一个更严格的实现。

一个需要提前交代的事实：这份 notebook 的 `setup` 函数**要求 GPU 且要求 bfloat16 支持，检测不到就直接抛异常、拒绝退回 CPU**。如果你的机器没有 GPU，实践任务里给出了需要改动哪些行的说明。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `pytorch_ipynb/cnn-nlp/cnn_imdb.ipynb` | 本讲唯一核心源码：自包含的 TextCNN 完整实验，含配置、数据下载与划分、分词与词表、模型、训练与 checkpoint 选择、评估与可视化 |

该 notebook 共 7 节，按 Markdown 标题组织：① 配置与硬件 → ② 数据划分 → ③ 分词与词表 → ④ TextCNN 模型 → ⑤ 训练与 checkpoint 选择 → ⑥ 评估 → ⑦ 学习曲线与混淆矩阵。这是 u1-l2 讲过的「五段式结构」的加强版：多了 artifacts 落盘（config.json、vocabulary.json、training.log、metrics.json 等），整份 notebook 像一次可复现的实验记录。

所有引用均为 notebook JSON 文件中的真实物理行号，永久链接基于当前 HEAD `59beda3`。

## 4. 核心概念与源码讲解

### 4.1 词嵌入层：从词元编号到稠密向量

#### 4.1.1 概念说明

词嵌入层要解决的问题：卷积只能作用于数值张量，而文本是离散符号。`nn.Embedding(V, E)` 内部就是一张 \(V \times E\) 的可学习矩阵，前向时按词元编号取行，把 `[batch, L]` 的整数张量变成 `[batch, L, E]` 的浮点张量。这一步与 u6-l1 的 RNN 完全相同——TextCNN 与 RNN 的分歧从「怎么吃这个张量」才开始。

本 notebook 的嵌入层有两个讲究：

1. **`padding_idx=0`**：词表把 `<pad>` 固定为编号 0，该行的嵌入向量被强制恒为 0，且永远不会收到梯度。这样补齐用的空位在数值上是「干净的零」，不会学出无意义的向量。
2. **均匀初始化 `U(-0.25, 0.25)`**：作者没有用默认初始化，而是仿照经典词向量训练的惯例做小范围均匀初始化，随后再把第 0 行清零，保证 `padding_idx` 的承诺在初始化后依然成立。

#### 4.1.2 核心流程

```
原始评论文本
  │ re.sub 去掉 HTML 标签、html.unescape 还原实体、转小写
  ▼
词元列表（正则 [a-z0-9]+(?:'[a-z0-9]+)? ，能匹配 don't 这类词）
  │ 只在训练集统计词频，过滤 min_frequency=2，
  │ 按频率降序取前 vocab_size-2 = 29998 个
  ▼
词表 {<pad>:0, <unk>:1, 词:i+2}（共 30000 项）
  │ 每条评论截断到前 max_length=512 个词元，
  │ 查不到的词一律映射为 <unk>=1，右侧补 0
  ▼
TensorDataset(ids, lengths, labels)   # 形状 [N,512]、[N]、[N]
  ▼
nn.Embedding(30000, 200, padding_idx=0) → [B, 512, 200]
```

注意数据集同时保存了每条评论的**真实长度** `lengths`——它不在嵌入层使用，而是留给 4.3 节的池化掩码，这是本实现的灵魂。

#### 4.1.3 源码精读

分词与词表构建只用了两行正则，[pytorch_ipynb/cnn-nlp/cnn_imdb.ipynb#L293-L295](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn-nlp/cnn_imdb.ipynb#L293-L295)：先剥掉 HTML 标签、还原 HTML 实体（如 `&amp;` → `&`）、转小写，再用正则抽出「字母数字串，中间允许一个撇号」作为词元。IMDb 影评里残留大量 `<br />`，这一步不可省。

[pytorch_ipynb/cnn-nlp/cnn_imdb.ipynb#L299-L305](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn-nlp/cnn_imdb.ipynb#L299-L305)：`Counter` 只数**训练集**的词频（这是 u2-l2 起反复强调的「统计量只能来自训练集」，防数据泄漏）；`sorted` 按 `(-词频, 词本身)` 排序保证词表确定可复现；切片 `[:vocab_size - 2]` 给 `<pad>` 和 `<unk>` 留出两个名额，最终词表恰好 30000 项。

[pytorch_ipynb/cnn-nlp/cnn_imdb.ipynb#L307-L316](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn-nlp/cnn_imdb.ipynb#L307-L316)：逐条评论编码。三个细节值得停下来看：

- `review[:config.max_length]` 先截断到 512 个词元（训练集有 2553 条被截断，验证/测试分别 382/750 条）；
- `vocab.get(word, 1)` 查不到就映射为 `<unk>=1`（未知词占比约 1.6%~1.8%）；
- `or [1]` 兜底：万一某条评论分词后为空，也至少放一个 `<unk>`，保证长度 ≥ 1——这与 4.3 节池化的 `clamp(min=1)` 遥相呼应。

模型侧的嵌入层在 [pytorch_ipynb/cnn-nlp/cnn_imdb.ipynb#L398-L410](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn-nlp/cnn_imdb.ipynb#L398-L410)：`nn.Embedding(vocabulary_size, config.embedding_dim, padding_idx=0)` 建立 \(30000\times200\) 查表；`nn.init.uniform_(...,-0.25,0.25)` 小范围均匀初始化；`with torch.no_grad(): self.embedding.weight[0].zero_()` 手动把 pad 行清零。到这里先记住一个数：这张表占 \(30000\times200=6{,}000{,}000\) 个参数，而整个模型是 6,308,354 个——**嵌入层占了约 95%**，与 u6-l1「Embedding 占总参数约 98%」的观察一致，这是词级模型的普遍形态。

#### 4.1.4 代码实践

1. **实践目标**：亲眼验证嵌入层的形状与 `padding_idx` 的「零向量 + 零梯度」承诺。
2. **操作步骤**：在能加载本 notebook 的环境里（或新建一个只 import torch 的脚本，以下为示例代码，不依赖仓库文件）：

```python
import torch
from torch import nn

emb = nn.Embedding(30_000, 200, padding_idx=0)
with torch.no_grad():
    emb.weight[0].zero_()
x = torch.tensor([[5, 8, 0, 0]])          # 一条长度 2、右侧补 0 的评论
out = emb(x)
print(out.shape)                           # torch.Size([1, 4, 200])
print(out[0, 2].abs().sum(), out[0, 3].abs().sum())   # 两个 pad 位置全零
out.sum().backward()
print(emb.weight.grad[0].abs().sum())      # 0. —— pad 行梯度为零
print(emb.weight.grad[5].abs().sum() > 0)  # True —— 正常词元有梯度
```

3. **需要观察的现象**：形状为 `[1, 4, 200]`；两个 pad 位置的向量全零；`backward` 之后 pad 行（第 0 行）的梯度范数为 0，而第 5 行有非零梯度。
4. **预期结果**：以上断言全部成立，即 `padding_idx` 同时做到了「前向输出零、反向不更新」。
5. 结果不依赖训练，可随时运行；若在不同 PyTorch 版本上行为不一致，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：词表大小 30000、`min_frequency=2`、按频率排序——如果把 `min_frequency` 提到 10，词表会怎样变化？未知词占比呢？
**答案**：更多低频词被过滤，但 `sorted(...)[:vocab_size-2]` 仍会截到 29998 个高频词补满 30000 项（只要剩余词够多），词表大小不变；被挤出去的词在编码时落到 `<unk>`，未知词占比会上升。词频过滤的意义是把名额留给常见词。

**练习 2**：为什么 `vocab.get(word, 1)` 用编号 1 而不是 0 当未知词？
**答案**：0 已经被 `<pad>` 占用。两者必须区分：pad 表示「这里没有词」，嵌入恒零且无梯度；`<unk>` 表示「这里有一个词表外的真词」，它有自己的可学习向量。混用会让池化掩码（4.3 节）无法区分「补齐」与「生词」。

### 4.2 多尺度一维卷积：并行的 n-gram 检测器

#### 4.2.1 概念说明

RNN 沿时间步递归计算，天然串行；卷积把整句话当作一维信号，所有位置**同时**算完。「一个宽度为 w 的卷积核」在文本里的语义是：学一个覆盖 w 个连续词元、横跨全部 200 个嵌入维的模式模板（即一个 w-gram 检测器），在句子的每个起始位置做一次匹配打分。情感分类的关键证据常常是局部短语——"not good"、"a waste of"、"one of the best"——不同长度的短语对应不同宽度的核，所以 Kim (2014) 提出 TextCNN 时就使用**多组宽度并行**：本 notebook 用宽度 3、4、5 各 128 个核。

之所以用 `nn.Conv1d` 而不是 `nn.Conv2d`：PyTorch 约定 `Conv1d` 的输入是 `[batch, channels, length]`。嵌入输出是 `[B, L, E]`，通道（嵌入维）在中间，所以源码里先 `transpose(1, 2)` 转成 `[B, 200, 512]` 再送进卷积。

#### 4.2.2 核心流程

设批大小 \(B\)、句长 \(L=512\)、嵌入维 \(E=200\)、宽度集合 \(W=\{3,4,5\}\)、每宽度核数 \(F=128\)：

```
[B, 512] token_ids
   │ Embedding                → [B, 512, 200]
   │ transpose(1, 2)          → [B, 200, 512]   （Conv1d 要通道在前）
   ├─ Conv1d(200,128,k=3)     → [B, 128, 510]   （512−3+1）
   ├─ Conv1d(200,128,k=4)     → [B, 128, 509]   （512−4+1）
   └─ Conv1d(200,128,k=5)     → [B, 128, 508]   （512−5+1）
        每个 filter 权重形状 [E×k]，滑过全部位置 → 每个 filter 得到一条
        “该 k-gram 模板在句中各位置的匹配分数序列”
```

无填充（padding=0）、步长 1 时的输出长度：

\[ L_{out} = L - k + 1 \]

宽度为 k 的一个卷积核参数量是 \(E \cdot k + 1\)（权重加偏置），三种宽度合计：

\[ P_{conv} = \sum_{k\in\{3,4,5\}} 128\,(200k+1) = 128\times(601+801+1001) = 307{,}584 \]

加上嵌入 6,000,000 与分类头 \(384\times2+2=770\)，总计 6,308,354——与 notebook 打印的参数量逐一吻合。

#### 4.2.3 源码精读

[pytorch_ipynb/cnn-nlp/cnn_imdb.ipynb#L60-L75](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn-nlp/cnn_imdb.ipynb#L60-L75)：全部超参集中在 `Config` dataclass 里，`filter_widths: tuple = (3, 4, 5)` 与 `filters_per_width: int = 128` 就是本节主角，`embedding_dim: int = 200` 决定卷积的输入通道数。超参集中化让后面 4.4 节的对照实验只需改一个元组。

[pytorch_ipynb/cnn-nlp/cnn_imdb.ipynb#L405-L410](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn-nlp/cnn_imdb.ipynb#L405-L410)：用列表推导对每个宽度建一个 `nn.Conv1d(config.embedding_dim, config.filters_per_width, width)`，装进 `nn.ModuleList`——必须用 `ModuleList` 而不是普通 list，普通 list 里的参数不会被登记进 `model.parameters()`，优化器与 `state_dict` 都会漏掉它们。下一行 `nn.Linear(len(config.filter_widths) * config.filters_per_width, 2)` 说明分类头输入维度由 `宽度个数 × 每宽度核数` 自动算出：**改 `filter_widths` 不需要手改任何其他维度**，这是综合实践能顺利进行的关键设计。

[pytorch_ipynb/cnn-nlp/cnn_imdb.ipynb#L412-L416](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn-nlp/cnn_imdb.ipynb#L412-L416)：前向传播里 `self.embedding(token_ids).transpose(1, 2)` 完成形状对齐；随后对 ModuleList 中的每个卷积分别 `torch.relu(convolution(embedded))`——每个卷积独立输出自己的特征图 `[B, 128, 512-k+1]`，ReLU 让「不匹配」得负分的位置归零。

#### 4.2.4 代码实践

1. **实践目标**：用最小示例验证 `Conv1d` 的形状与参数量公式，为综合实践打好基础。
2. **操作步骤**（示例代码，只需 PyTorch）：

```python
import torch
from torch import nn

for width in (3, 4, 5):
    conv = nn.Conv1d(in_channels=200, out_channels=128, kernel_size=width)
    x = torch.randn(2, 200, 512)          # [B, E, L]，模拟转置后的嵌入
    y = conv(x)
    p = sum(par.numel() for par in conv.parameters())
    print(width, tuple(y.shape), p)       # (2,128,510) 76928 等
# 手工核对：128*(200*3+1)=76,928；128*(200*4+1)=102,528；128*(200*5+1)=128,128
```

3. **需要观察的现象**：输出长度分别为 510、509、508；参数量分别为 76,928、102,528、128,128。
4. **预期结果**：与公式 \(L_{out}=L-k+1\)、\(P=F(Ek+1)\) 完全一致，三者之和 307,584。
5. 若你想核对该 notebook 的总参数量，可运行源码 cell（[pytorch_ipynb/cnn-nlp/cnn_imdb.ipynb#L475-L478](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn-nlp/cnn_imdb.ipynb#L475-L478) 打印 `Parameters: 6,308,354` 并调用 `check_model`）；CPU 环境下模型构建本身不需要 GPU，只有 `setup` 会拦截（见 4.4 节）。

#### 4.2.5 小练习与答案

**练习 1**：为什么三种宽度各建一个 `Conv1d`，而不是建一个 `kernel_size=3` 的大卷积？
**答案**：卷积核宽度决定感受野（覆盖几个词元），一个固定宽度的核只能检测一种长度的 n-gram。多尺度并行让 3-gram、4-gram、5-gram 特征同时被提取，模型不必预先假设「关键短语是多长」。若只用一种宽度，等于强加了错误的先验。

**练习 2**：把 `filters_per_width` 从 128 增到 256，卷积参数量与分类头输入维度分别怎么变？
**答案**：卷积参数量翻倍为 615,168；分类头输入从 \(3\times128=384\) 变为 \(3\times256=768\)，即 `Linear(768, 2)`——源码里这一维度是自动推导的，无需手动同步。

**练习 3**：如果某个宽度取 600（大于 `max_length` 512），按本源码会出什么问题？
**答案**：输出长度 \(512-600+1=-87\) 为负，`Conv1d` 直接报错。实际上长度小于宽度时没有任何完整窗口，这也是 4.3 节 `clamp(min=1)` 要单独处理「评论比核还短」的原因。

### 4.3 掩码全局最大池化：每张特征图只留一个证据

#### 4.3.1 概念说明

卷积之后，每个核产出的 `[B, 128, 512-k+1]` 特征图回答的是「这个 k-gram 模板在**每个位置**匹配得怎么样」，而情感分类只需要「它在**全句任何地方**出现没有」。max-over-time 池化沿时间维取最大值：每个核的每张特征图坍缩成一个标量——该模式在整句中的最强激活。这一步同时解决了变长问题：无论评论多长，池化后形状恒为 `[B, 128]`，三种宽度拼接成 `[B, 384]`。

但朴素的 max 池化有个 bug：补零的 padding 位置也会被卷积扫到，产生无意义的「窗口」。举例：一条只有 10 个词元的评论被补到 512，宽度 4 的核在位置 10 之后扫过的窗口一半是零向量。这些窗口的激活值可能碰巧大于真实文本的激活，污染最大值。u6-l2 用 `pack_padded_sequence` 让 RNN 干脆跳过 padding；CNN 没法「跳过」，本 notebook 的方案是**先把 padding 参与的窗口屏蔽成极小值，再取 max**——数学上等价于「只在真实文本上取最大」。

#### 4.3.2 核心流程

对宽度 k、某条真实长度为 \(\ell\) 的评论，有效窗口数是 \(\ell-k+1\)；卷积输出共 \(512-k+1\) 个位置，其中前 \(\ell-k+1\) 个完全落在真实文本内。屏蔽规则：

\[ \text{valid}(\ell,k) = \max(\ell-k+1,\;1) \]

- 位置 \(j < \text{valid}\)：真实窗口，保留；
- 位置 \(j \ge \text{valid}\)：涉及 padding，填充为该 dtype 的最小可表示值 \(-\infty_{\text{finfo}}\)，取 max 时必然落选；
- `clamp(min=1)` 兜底 \(\ell < k\)（评论比核还短）的情形：保留一个覆盖 padding 的窗口，保证每张特征图至少有一个候选值，`amax` 不会遇到「全部被屏蔽」的未定义状态。

取 max 后，被选中的激活值对卷积权重与该窗口覆盖的嵌入正常回传梯度；被屏蔽位置的激活是常数最小值，`masked_fill` 之后的梯度为零，**梯度也不会流进 padding**——与 `padding_idx=0` 形成「前向屏蔽 + 反向屏蔽」的双重保险。

#### 4.3.3 源码精读

[pytorch_ipynb/cnn-nlp/cnn_imdb.ipynb#L415-L425](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn-nlp/cnn_imdb.ipynb#L415-L425)：这是全模型最核心的七行，逐行拆开：

- `width = convolution.kernel_size[0]`：从每个卷积层反查自己的宽度，掩码计算对三种宽度自动适配；
- `valid_windows = (lengths - width + 1).clamp(min=1)`：每条评论的有效窗口数，形状 `[B]`；
- `positions = torch.arange(features.shape[-1], ...)`：`[512-k+1]` 的位置下标；
- `mask = positions[None, :] >= valid_windows[:, None]`：广播成 `[B, 512-k+1]`，True 即「该位置的窗口已越过真实文本」；
- `features.masked_fill(mask[:, None, :], torch.finfo(features.dtype).min)`：在通道维插入一维后，把无效位置整列填成 dtype 最小值（autocast 下是 bfloat16 的最小值，而非真正的 `-inf`，保证数值安全）；
- `pooled.append(features.amax(dim=-1))`：沿时间维取最大 → `[B, 128]`；
- 循环结束 `torch.cat(pooled, dim=1)` → `[B, 384]`，经 Dropout 后进 `Linear(384, 2)` 得到负/正两个 logits。

[pytorch_ipynb/cnn-nlp/cnn_imdb.ipynb#L428-L440](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn-nlp/cnn_imdb.ipynb#L428-L440)：`check_model` 把上面的叙述变成了**可执行的不变量测试**——同一样本分别以长度 5 的原样和右侧再补 7 个 0 的版本前向，`torch.testing.assert_close` 断言两次 logits 一致。这正是掩码池化「额外右侧 padding 不改变预测」的性质检验，是值得抄进自己项目的做法：用小样本断言模型的输入不变量，而不是靠人眼读代码确信。

#### 4.3.4 代码实践

1. **实践目标**：亲手复现掩码机制，理解 `clamp(min=1)` 与屏蔽列的含义。
2. **操作步骤**（示例代码）：

```python
import torch

lengths = torch.tensor([10, 2])            # 一条长度 10、一条长度 2 的评论
width = 5
valid = (lengths - width + 1).clamp(min=1)  # tensor([6, 1])
positions = torch.arange(508)               # 宽度 5 的卷积在 512 上输出 508 个位置
mask = positions[None, :] >= valid[:, None] # [2, 508]，True = 无效
print(mask[0, :6].tolist())                 # 前 6 个位置有效 → [False]*6
print(mask[1, 0].item(), mask[1, 1].item()) # True, True —— 长度 2 只保留 1 个窗口
```

3. **需要观察的现象**：长度 10 的评论对宽度 5 有 \(10-5+1=6\) 个有效窗口，前 6 个位置 False；长度 2 的评论 \(\ell-k+1=-2\) 被 clamp 到 1，只有位置 0 有效。
4. **预期结果**：与上面注释一致；`mask[:, None, :]` 再广播到 128 个通道后就是源码里 `masked_fill` 的输入。
5. 若要在真实模型上验证，运行 notebook 中调用 `check_model(model, device)` 的 cell（输出 "Passed: output shape, finite logits, and invariance to extra right padding."，见 [pytorch_ipynb/cnn-nlp/cnn_imdb.ipynb#L475-L478](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn-nlp/cnn_imdb.ipynb#L475-L478)）；该验证只做前向，理论上 CPU 也可跑，但 notebook 的 `setup` 前置拦截了无 GPU 环境，「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么屏蔽值用 `torch.finfo(dtype).min` 而不是 `-1e9` 或真正的 `float('-inf')`？
**答案**：若用固定常数 `-1e9`，在 bfloat16 下可能溢出或与真实激活混叠（bfloat16 动态范围极大但精度低）；用该 dtype 的最小可表示值既保证一定小于所有真实激活，又保持有限值，`amax` 结果不会变成 NaN。`-inf` 则可能在与后续运算组合时产生 NaN 风险。

**练习 2**：掩码屏蔽与 u6-l2 的 `pack_padded_sequence` 解决的是同一个问题吗？手段有何不同？
**答案**：是同一个问题——让 padding 不影响结果、不浪费算力。RNN 可以重排序列真正跳过 padding（打包）；卷积必须对整块张量计算、无法跳过位置，所以本实现选择「照常计算、事后屏蔽」，接受一点冗余计算换取实现简单与完全并行。

**练习 3**：如果删掉 `clamp(min=1)`，什么样的输入会让 `amax` 出问题？
**答案**：真实长度小于卷积宽度的评论（如长度 2 的评论对宽度 5 的核），\(\ell-k+1\le 0\) 意味着所有位置都被 mask 屏蔽，`amax` 在全 `-min` 上仍能返回值但语义退化为「只看 padding 窗口」，且不同 dtype 下可能引入数值问题；`clamp(min=1)` 明确保留一个窗口，行为有定义。这与 4.1.3 节空评论兜底 `or [1]` 保证长度 ≥ 1 是配套设计。

### 4.4 实验组织：验证集选 checkpoint、早停与一次性测试

#### 4.4.1 概念说明

模型结构只占这份 notebook 的一半，另一半是实验纪律。它把 u4-l4 讲过的「验证集调参、测试集只评一次」贯彻得很彻底，并加上三件现代工程实践：

1. **数据三分**：50,000 条官方评论合并打乱后切成 35,000 训练 / 5,000 验证 / 10,000 测试（[pytorch_ipynb/cnn-nlp/cnn_imdb.ipynb#L221-L223](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn-nlp/cnn_imdb.ipynb#L221-L223)），并用 `review_id` 断言三个集合互不相交；notebook 甚至诚实统计并打印了「跨集合的重复文本」（44/119/28 条）——IMDb 官方数据本身存在少量重复评论。
2. **训练技巧**：AdamW 优化器（带权重衰减）、bfloat16 自动混合精度、梯度范数裁剪，训练循环仍是五步范式的加强版。
3. **模型选择**：每个 epoch 结束在**完整验证集**上评估，按「验证准确率最高，平局时验证损失更低」保存 `best_model.pt`；连续 3 个 epoch 无改进就早停；训练结束**重新加载最优 checkpoint**，测试集只在这时评估一次。

#### 4.4.2 核心流程

```
每个 epoch:
  训练循环（对每个 batch）:
    zero_grad → autocast 前向 → cross_entropy → backward → clip_grad_norm_ → step
  完整验证集评估 (evaluate: model.eval + inference_mode)
  improved = (val_acc > best_acc) or (val_acc == best_acc and val_loss < best_loss)
  improved → 保存 state_dict + epoch 到 best_model.pt；stale = 0
  否则     → stale += 1
  stale >= 3 → 早停退出
训练结束:
  torch.load(best_model.pt) → model.load_state_dict(...)   # 恢复最优权重
  测试集评估一次 → metrics.json 落盘
```

#### 4.4.3 源码精读

[pytorch_ipynb/cnn-nlp/cnn_imdb.ipynb#L532-L541](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn-nlp/cnn_imdb.ipynb#L532-L541)：训练内循环。`optimizer.zero_grad(set_to_none=True)` 清梯度（u2-l1 讲过的防跨批累加）；前向与损失包在 `torch.autocast("cuda", dtype=torch.bfloat16)` 里做低精度加速、损失用 `logits.float()` 升回 float32 计算；`nn.utils.clip_grad_norm_`（阈值 `gradient_clip=1.0`）是 u7-l3 会展开的技巧，作用是防止个别 batch 的异常大梯度破坏训练。注意这里评估训练指标用的是「在线」口径（训练模式下边训边统计），Dropout 是开着的，所以训练准确率被系统性低估。

[pytorch_ipynb/cnn-nlp/cnn_imdb.ipynb#L501-L516](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn-nlp/cnn_imdb.ipynb#L501-L516)：`evaluate` 用 `@torch.inference_mode()` + `model.eval()`（u2-l4 讲过的双开关：eval 管层行为、inference_mode 管梯度）；损失用 `reduction="sum"` 累加再除以样本数——正是 u4-l4 强调的「避免不满尾批被放大权重」的写法；顺带用 `torch.bincount` 累计混淆矩阵。

[pytorch_ipynb/cnn-nlp/cnn_imdb.ipynb#L552-L560](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn-nlp/cnn_imdb.ipynb#L552-L560)：checkpoint 判定。看 notebook 的真实训练输出会更清楚：epoch 2 验证准确率 91.20%（保存）；epoch 3 验证准确率 91.16%、验证损失 0.2283（比 epoch 2 的 0.2304 **更低**），但没有保存——因为规则是**准确率优先，损失只用于平局**，91.16% < 91.20% 已经输了。随后三个 epoch 无改进触发早停。

[pytorch_ipynb/cnn-nlp/cnn_imdb.ipynb#L572-L579](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn-nlp/cnn_imdb.ipynb#L572-L579)：早停（`stale_epochs >= patience` 即 break），随后 `torch.load(..., weights_only=True)` + `load_state_dict` 恢复**验证集上最优**的 epoch 2 权重——最终测试的不是最后一步的权重，这是「报告成绩前须重载最优 checkpoint」（u4-l4）的严格落地。

最终成绩（作者环境，训练仅 0.87 分钟）：验证 91.20%，**测试 90.07%**（9,007/10,000），测试混淆矩阵为 4440 / 551 / 442 / 4567。与 u6-l2 的 LSTM ~85% 相比看似大胜，但**两者数据划分、分词、词表、截断长度全都不同，不能直接横比**——沿用 u3-l5 的方法论：比较模型必须对齐训练配方。

#### 4.4.4 代码实践

1. **实践目标**：从 notebook 的真实训练日志中读懂 checkpoint 选择规则，不写代码也能练。
2. **操作步骤**：打开 notebook 第 5 节的训练输出 cell（[pytorch_ipynb/cnn-nlp/cnn_imdb.ipynb#L598-L634](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn-nlp/cnn_imdb.ipynb#L598-L634)），对照逐行读 [L552-L560 的判定逻辑](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn-nlp/cnn_imdb.ipynb#L552-L560)。
3. **需要观察的现象**：epoch 1、2 有 "checkpoint saved"；epoch 3 验证损失更低却没保存；第 5 个 epoch 后触发早停；最终 "Restored epoch 2"。
4. **预期结果**：你能向别人解释「为什么 restored 的是 epoch 2 而不是 epoch 5（最后一步）或 epoch 3（验证损失最低）」——因为规则以验证准确率为第一关键字、验证损失仅作平局决胜；epoch 2 之后验证准确率再未超过 91.20%。
5. 以上基于 notebook 已保存的输出，可直接阅读，无需运行。

#### 4.4.5 小练习与答案

**练习 1**：为什么测试集只在训练结束后评估一次，而不是每个 epoch 都看？
**答案**：一旦反复在测试集上观察并据此做决定（调超参、选模型），测试集就变成了事实上的验证集，其数字会因「选择效应」而系统性偏乐观。验证集承担全部反复评估的代价，测试集保留「无偏的最后一次」。

**练习 2**：训练日志里 `online train acc` 第 5 个 epoch 已达 98.77%，而最终对训练集重新评估只有 95.54%，为什么低了一截？
**答案**：在线口径在 `model.train()` 下统计，Dropout（p=0.5）是开着的，等于在「自缚手脚」的状态下考试；最终评估用 `model.eval()` 关闭 Dropout，恢复全部单元，反而只到 95.54% 说明第 5 个 epoch 已经过拟合（验证准确率从 91.20% 掉到 90.44%）。两个口径不可直接比较。

**练习 3**：早停的 `patience=3` 与「最多 20 个 epoch」是什么关系？
**答案**：`max_epochs=20` 是硬上限；`patience=3` 是软停止——连续 3 个 epoch 验证指标没有刷新最好值就提前退出。本实验实际跑了 5 个 epoch 就停了，既节省算力又降低过拟合风险，最终用的是更早保存的最优权重。

## 5. 综合实践

**任务：做一个「卷积核配置 → 测试准确率」的小型对照实验。**

本实践串联全部三个结构模块：改 `filter_widths`（多尺度卷积）与 `filters_per_width`（每尺度核数），观察参数量与最终测试准确率的变化。

1. **准备**：按 u1-l2 的方式启动 notebook。注意 `setup`（[pytorch_ipynb/cnn-nlp/cnn_imdb.ipynb#L77-L110](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn-nlp/cnn_imdb.ipynb#L77-L110)）要求 CUDA GPU 且支持 bfloat16，否则抛 `RuntimeError` 拒绝运行。有 NVIDIA GPU（近几年的卡均支持 bf16）可直接跑；没有 GPU 的读者可把 setup 中两处 `raise RuntimeError` 的拦截改为 CPU 分支、并把 `torch.autocast("cuda", ...)` 改为 CPU 可用的形式（或去掉 autocast）——这是对源码的修改，请在自己副本上进行，训练时长会显著增加，结果「待本地验证」。
2. **基线复现**：先原样跑通 `(3,4,5)/128`，确认参数量 6,308,354、测试准确率接近 90.07%（种子相同但硬件/软件版本不同会有轻微差异）。
3. **对照实验一（宽度）**：`filter_widths = (2, 3, 4, 5)`——加入 2-gram 核，其余不动。训练后记录测试准确率，并解释分类头为何自动变为 `Linear(512, 2)`（`len(widths)×128=512`，见 L410 的自动推导）。
4. **对照实验二（核数）**：`filters_per_width = 64`，宽度回到 `(3,4,5)`。参数量按 4.2.2 的公式重新计算（卷积部分应减半为 153,792，嵌入层不变）。
5. **记录与结论**：填一张表：

| 配置 | 卷积参数量 | 总参数量 | 最佳 epoch | 验证准确率 | 测试准确率 |
|---|---|---|---|---|---|
| (3,4,5) × 128（基线） | 307,584 | 6,308,354 | 2 | 91.20% | 90.07% |
| (2,3,4,5) × 128 | 待算 | 待算 | 待本地验证 | 待本地验证 | 待本地验证 |
| (3,4,5) × 64 | 153,792 | 待算 | 待本地验证 | 待本地验证 | 待本地验证 |

6. **思考题**：三种配置的训练时间（每 epoch 约 10 秒的基线）哪个更长？加 2-gram 核与减半核数哪个对准确率影响更大？把你的解释写进表格备注——记住单次运行不可轻信，有时间可以换 `seed` 再跑一轮看波动。

## 6. 本讲小结

- TextCNN 把句子当一维信号：`Embedding` 把 `[B,512]` 变 `[B,512,200]`，`transpose(1,2)` 对齐 `Conv1d` 的通道在前约定，宽度 3/4/5 各 128 个卷积核并行充当 **n-gram 检测器**，输出长度 \(L_{out}=L-k+1\)。
- **全局最大池化（max-over-time）**把每张特征图坍缩成一个「该短语模式在全文的最强激活」，同时天然解决变长输入；拼接三组得到 384 维句子表示，接 Dropout + Linear 得二分类 logits。
- 本实现的精华是**掩码池化**：`valid_windows=(lengths-k+1).clamp(min=1)` + `masked_fill(dtype 最小值)` 让 padding 位置的窗口必然落选，`check_model` 用断言验证「追加右侧 padding 不改变 logits」这一不变量——与 RNN 的 packed sequences 异曲同工，手段是「照算后屏蔽」而非「重排跳过」。
- 参数分布印证词级模型形态：30000×200 的嵌入表占 95%（600 万 / 630.8 万），卷积仅 30.8 万，改卷积配置对总参数量影响有限。
- 实验纪律：验证集准确率优先（损失只作平局决胜）保存最优 checkpoint，`patience=3` 早停，结束后**重载最优权重**、测试集只评估一次；基线成绩验证 91.20% / 测试 90.07%（epoch 2 权重）。
- 与其他模型的比较必须对齐数据划分与训练配方：本 notebook 的 90.07% 与 u6-l2 LSTM 的 ~85% 出自不同的划分、分词与截断设置，直接横比没有意义。

## 7. 下一步学习建议

- **u6-l6（DistilBERT 特征提取与微调）**是序列建模单元的下一讲：把本讲的「手写词表 + 从头训练嵌入」换成预训练 Transformer，看看在同一个 IMDb 任务上预训练能带来多少提升，并对比训练成本。
- 回顾 **u4-l4（公共训练评估工具链）**：本 notebook 手写的 `evaluate`/checkpoint 逻辑与仓库共享的 `train_classifier_simple_v2` 思想一致（`reduction="sum"`、最优 checkpoint、验证集选择），两相对照能加深理解。
- 若对本讲的训练技巧（梯度裁剪、混合精度）意犹未尽，可提前阅读 **u7-l3（训练技巧）**；对「为什么 max 池化对文本有效」想深挖的读者，可读 Kim (2014) *Convolutional Neural Networks for Sentence Classification* 原文，本 notebook 正是它的一个现代 PyTorch 实现。
- 想继续动手：把本讲的掩码池化思想移植回 u5 系列的卷积自编码器（那里同样有 padding 位置被卷积扫过的问题），验证通用性。

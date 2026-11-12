# LSTM、GRU 与 packed sequences

## 1. 本讲目标

上一讲（u6-l1）我们用 `nn.RNN` 在 IMDb 影评数据上搭了第一个情感分类器，结果并不理想：测试准确率只有约 46%，损失几乎躺在 \(\ln 2 \approx 0.693\) 附近不动。本讲沿着两条线索修复它：

1. **packed sequences（打包序列）**：搞清楚 padding 到底怎样「既浪费算力又污染隐状态」，学会用 `pack_padded_sequence`（以及它的搭档 `pad_packed_sequence`）让 RNN 家族在变长文本上正确且高效地计算。
2. **LSTM 与 GRU**：理解简单 RNN 梯度消失的根源，掌握门控单元如何用「加法传送带」让梯度传得更远，并学会在几乎同一份代码里切换 `nn.RNN` / `nn.LSTM` / `nn.GRU` 做受控对比实验。

读完本讲，你应该能独立回答：为什么 `sort_within_batch=True` 是打包的前提？为什么 LSTM 的返回值要解包成 `(hidden, cell)` 而 GRU 不用？以及在相同数据与超参下三种单元的准确率与耗时差距有多大。

## 2. 前置知识

承接 u6-l1 已建立的概念，这里只做简要回顾，不再展开：

- **词表与 Embedding**：文本先分词（spacy），只用训练集按词频取前 20000 个词建词表，加上 `<unk>` 与 `<pad>` 两个特殊词元共 20002 项；`nn.Embedding` 把整数编号查表成可学习的稠密向量。
- **变长与 padding**：一个 batch 里的句子长短不一，必须补齐到 batch 内最长句的长度才能堆成一个矩形张量，补的位置填 `<pad>` 的编号。句长优先（sequence-first）布局下，文本张量形状是 `[句长, batch 大小]`。
- **many-to-one 分类**：整个句子过完 RNN 后，取最终隐状态 `hidden`（形状 `[1, batch, hidden_dim]`，`squeeze(0)` 去掉层数维）接全连接层输出 1 个 logit，配 `binary_cross_entropy_with_logits`。
- **五步训练范式**：`forward → loss → zero_grad → backward → step`，本讲三个 notebook 的训练循环一字不差地沿用它。
- **梯度消失**：BPTT（沿时间反向传播）中，梯度要连乘隐状态权重矩阵 \(W_h\) 数百步；若 \(W_h\) 的奇异值普遍小于 1，梯度指数级衰减，浅层（即句子开头）几乎学不到东西——这是简单 RNN 失败的内因。
- **torchtext 旧版 API**：三个 notebook 使用的是 torch 1.0 时代的 `torchtext.data.Field` / `BucketIterator` 接口（作者环境为 torch 1.0.1.post2，见各 notebook 的 watermark 输出）。新版 torchtext 已重构这些 API，复现实验时需安装配套的旧版本环境（待本地验证你所用版本的兼容性）。此外 notebook 在导入区设置了 `torch.backends.cudnn.deterministic = True`，与 u4-l4 讲过的确定性话题呼应。

本讲新增的两个概念——打包序列与门控单元——都会在正文中从零讲起。

## 3. 本讲源码地图

本讲的三个关键 notebook 位于 `pytorch_ipynb/rnn/`，它们是同一模板的三次「只改一处」式复制（这正是本手册 u2-l4 引入的受控对比方法）：

| 文件 | 作用 | 与另两份的差异 |
| --- | --- | --- |
| [rnn_simple_packed_imdb.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_simple_packed_imdb.ipynb) | 简单 RNN + 打包序列 | `nn.RNN`，返回 `output, hidden` |
| [rnn_lstm_packed_imdb.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_lstm_packed_imdb.ipynb) | LSTM + 打包序列 | `nn.LSTM`，返回 `packed_output, (hidden, cell)` |
| [rnn_gru_packed_imdb.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_gru_packed_imdb.ipynb) | GRU + 打包序列 | `nn.GRU`，返回 `packed_output, hidden` |

另有一个对照参照物：上一讲的 [rnn_simple_imdb.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_simple_imdb.ipynb)（不打包版），我们用它的保存输出量化打包的收益。

> **关于行号**：`.ipynb` 是 JSON 文件，本文永久链接中的 `#L` 行号指向该 JSON 的原始行（在 GitHub 上打开链接即可定位到对应源码行）。

## 4. 核心概念与源码讲解

### 4.1 packed sequence：让 RNN 跳过 padding

#### 4.1.1 概念说明

IMDb 影评长短悬殊：短的几十个词，长的上千个词。堆 batch 时必须 padding 补齐，而朴素做法有两个代价：

1. **算力浪费**：设 batch 内最长句 132 个词、最短 10 个词，RNN 也要老老实实跑满 132 个时间步，其中大量步只是在「处理」无意义的 `<pad>`。
2. **隐状态被稀释**：RNN 每个时间步都无条件更新隐状态，`<pad>` 的 embedding 也会被卷进 \(h_t\)。于是「最终隐状态」其实对应最后一个 padding 位置，而不是这句话真正的最后一个词——句子越长被稀释得越厉害。这是上一讲简单 RNN 表现糟糕的重要原因之一（与梯度消失叠加）。

**打包序列**（`PackedSequence`）是 PyTorch 的解决方案：把「矩形补齐张量 + 每句的真实长度」重排成一种紧凑数据结构，使 RNN 内核在每个时间步只处理仍然「活着」（还没到句尾）的样本。padding 位置既不参与前向，也不参与反向，一石二鸟。

#### 4.1.2 核心流程

打包的工作前提是：**batch 内序列按长度降序排列**。这一点是理解 `PackedSequence` 的钥匙。降序排列后，在任意时间步 \(t\)，「长度 ≥ t」的样本恰好构成 batch 的一个前缀；随着 \(t$ 增大，这个前缀单调收缩。于是整个 batch 可以只用两个东西描述：

- `data`：把每个时间步上「活着」的样本依次拼接而成的一维长条；
- `batch_sizes`：一个列表，`batch_sizes[t]` 记录第 \(t\) 步有多少样本还活着。

RNN 内核只需按 `batch_sizes` 逐步推进，天然跳过所有 padding。伪代码：

```text
输入: padded [L_max, B, E], lengths [B]（已降序）
for t in 0 .. L_max-1:
    alive = 满足 lengths[i] > t 的样本数   # 即 batch_sizes[t]
    只取前 alive 个样本做第 t 步递推
输出: 每句在其真实最后一个词处的隐状态
```

与之配套的两个数据管线设置（notebook 里都有注释 `# necessary for packed_padded_sequence`）：

- `data.Field(..., include_lengths=True)`：让迭代器产出 `(text, lengths)` 二元组，真实长度随 batch 一起流动；
- `BucketIterator.splits(..., sort_within_batch=True)`：batch 内按长度降序排序，满足打包的排序前提（`pack_padded_sequence` 默认 `enforce_sorted=True`，遇到乱序会报错；若无法排序，可传 `enforce_sorted=False` 让它内部整理，代价是额外开销）。

还有一个容易忽略的细节：**本仓库这三个 notebook 只用了 `pack_padded_sequence`，没有调用 `pad_packed_sequence`**。因为任务是最 many-to-one 分类，只需要最终 `hidden`；一旦你需要逐时间步的 `output`（如 many-to-many、注意力机制），就必须把 RNN 吐出的打包输出用 `pad_packed_sequence` 还原回矩形张量——两者是一对搭档。4.1.4 的实践会同时演示这两个函数。

#### 4.1.3 源码精读

**（1）数据管线两处关键设置**。Field 声明真实长度：

[pytorch_ipynb/rnn/rnn_simple_packed_imdb.ipynb:L148-L152](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_simple_packed_imdb.ipynb#L148-L152) 这段代码创建 `tokenize='spacy'` 的文本 Field，并打开 `include_lengths=True`——注释明确写着这是 packed_padded_sequence 的必要条件；随后加载 IMDb 并按 0.8 划分训练/验证集（20000/5000 条），测试集 25000 条。

[pytorch_ipynb/rnn/rnn_simple_packed_imdb.ipynb:L230-L234](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_simple_packed_imdb.ipynb#L230-L234) 这里构造三个 loader，`sort_within_batch=True` 完成 batch 内降序排序——打包的第二个必要条件。`BucketIterator` 本身沿袭 u6-l1：把长度相近的句子尽量装进同一 batch，减少 padding 总量。

笔记本随后打印了三个 loader 首个 batch 的形状：[L265-L273](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_simple_packed_imdb.ipynb#L265-L273) 显示训练 batch 文本矩阵为 `[132, 128]`——132 是该 batch 内最长句的长度。注意这是「补齐后的」形状，真实有效长度参差不齐，这正是打包要利用的信息。

**（2）模型 forward 中的打包调用**：

[pytorch_ipynb/rnn/rnn_simple_packed_imdb.ipynb:L318-L341](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_simple_packed_imdb.ipynb#L318-L341) 定义了与 u6-l1 同名的 `RNN` 类，三件套仍是 Embedding → 循环核 → Linear。关键新动作有两处：L334 的 `packed = torch.nn.utils.rnn.pack_padded_sequence(embedded, text_length)` 把 `[句长, batch, 嵌入维]` 的补齐张量连同真实长度打成 `PackedSequence`；L339 的 `output, hidden = self.rnn(packed)` 直接把打包对象喂给 `nn.RNN`（循环核能识别这种输入）。返回时 L341 用 `self.fc(hidden.squeeze(0)).view(-1)`：由于打包后每个样本的「最终隐状态」恰好在它真实最后一个词处产生，`hidden` 不再被尾部 padding 污染。

超参与实例化：[L92-L101](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_simple_packed_imdb.ipynb#L92-L101) 设定 `EMBEDDING_DIM=128`、`HIDDEN_DIM=256`、`BATCH_SIZE=128`、`NUM_EPOCHS=15`、`LEARNING_RATE=1e-4`；[L354-L359](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_simple_packed_imdb.ipynb#L354-L359) 固定种子后建模型、配 Adam——三份 notebook 的这一段完全相同，是后面对比实验公平性的保证。

训练循环与评估函数也只是把 u6-l1 的范式平移过来：[L382-L393](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_simple_packed_imdb.ipynb#L382-L393) 的 `compute_binary_accuracy` 在 `model.eval()` + `torch.no_grad()` 下用 `sigmoid(logits) > 0.5` 出预测；[L523-L556](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_simple_packed_imdb.ipynb#L523-L556) 是标准的 epoch×batch 双层五步循环，每 epoch 结束打印训练/验证准确率与累计耗时。

**（3）打包的实测收益**。notebook 开头的说明文字（[L29-L31](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_simple_packed_imdb.ipynb#L29-L31)）声称打包版比不打包版快约 4 倍。对照两份 notebook 保存的输出：

| 版本 | 总训练时间 | 测试准确率 |
| --- | --- | --- |
| 不打包（[rnn_simple_imdb.ipynb:L529-L530](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_simple_imdb.ipynb#L529-L530)） | 6.35 min | 46.38% |
| 打包（[rnn_simple_packed_imdb.ipynb:L517-L518](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_simple_packed_imdb.ipynb#L517-L518)） | 2.01 min | 73.94% |

时间缩短 3 倍多（与「约 4 倍」的声明同量级，具体倍数随环境浮动）；更重要的是准确率从 46.38% 跳到 73.94%——同样的 `nn.RNN`、同样的超参与种子，唯一的差别就是 padding 不再污染最终隐状态。

#### 4.1.4 代码实践：亲手拆开 PackedSequence

**实践目标**：用一个手工构造的小 batch，直观看到「打包后 RNN 每步只处理活着的样本」，并演练 `pad_packed_sequence` 的还原（仓库 notebook 因只用 `hidden` 而未调用它）。

**操作步骤**（以下为示例代码，非仓库原有内容，可在任意安装了 PyTorch 的环境运行）：

```python
import torch
import torch.nn as nn

torch.manual_seed(0)
embed = nn.Embedding(10, 4)          # 10 个词、4 维嵌入（含 <pad>=0）
padded = torch.tensor([              # 3 句话，已按长度降序排好
    [1, 2, 3, 4],                    # 长度 4
    [5, 6, 7, 0],                    # 长度 3（末位 0 是 <pad>）
    [8, 9, 0, 0],                    # 长度 2（末两位是 <pad>）
])
lengths = torch.tensor([4, 3, 2])

packed = nn.utils.rnn.pack_padded_sequence(embed(padded), lengths)
print(packed.batch_sizes)            # 每个时间步仍存活的样本数
print(packed.data.shape)             # 拼接后的有效词向量总数

restored, restored_lens = nn.utils.rnn.pad_packed_sequence(packed, batch_first=False)
print(restored.shape, restored_lens) # 还原回 [4, 3, 4] 与原长度
```

**需要观察的现象**：

1. `batch_sizes` 应为 `tensor([3, 3, 2, 1])`——第 1、2 步 3 句都活着，第 3 步只剩前 2 句，第 4 步只剩第 1 句；
2. `packed.data.shape` 的第一维应是 `3+3+2+1=9`（有效时间步总数），而不是补齐后的 `4×3=12`——这就是「跳过 padding」的具象形态；
3. `pad_packed_sequence` 还原出的张量形状是 `[4, 3, 4]`（最长句长、batch、嵌入维），且返回了原长度，padding 位置自动填 0。

**预期结果**：以上三个输出的数值都可以直接推出来（如上）；若在你的环境中 `pack_padded_sequence` 报 lengths 所在设备相关的错误，把 `lengths` 放到 CPU 上再传（新版 PyTorch 要求长度张量在 CPU 上，属版本兼容性问题，待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：如果去掉 `sort_within_batch=True`，直接把乱序 batch 传给默认参数的 `pack_padded_sequence`，会发生什么？有哪两种补救办法？

**答案**：默认 `enforce_sorted=True` 会直接抛错，因为它要求长度降序。补救一：恢复 `sort_within_batch=True`（notebook 的做法，由迭代器保证排序）；补救二：调用 `pack_padded_sequence(..., enforce_sorted=False)`，让函数内部按 `sorted_indices` 重排再打包，代价是额外的排序与索引操作。

**练习 2**：为什么本仓库三个 packed notebook 都没有调用 `pad_packed_sequence`？

**答案**：任务是 many-to-one 情感分类，分类头只吃最终隐状态 `hidden`，而 `nn.RNN/LSTM/GRU` 对打包输入返回的 `hidden` 已经是「每句在其真实最后一个词处」的状态，无需还原逐时间步输出。只有当后续计算需要每个时间步的 `output`（序列标注、注意力等）时，才必须用 `pad_packed_sequence` 把打包输出还原成矩形张量。

**练习 3**：打包能修正「隐状态被 padding 稀释」的问题，能修正梯度消失吗？

**答案**：不能。梯度消失源于反向传播中隐状态权重矩阵 \(W_h\) 的长链连乘，与是否有 padding 无关。打包只消除 padding 的干扰与冗余计算。对抗梯度消失要靠门控单元（LSTM/GRU，见 4.2、4.3）或改换架构，这也是本讲后半部分的主题。

### 4.2 LSTM：门控让梯度活得更久

#### 4.2.1 概念说明

简单 RNN 的递推式是 \(h_t = \tanh(W_h h_{t-1} + W_x x_t)\)，反向传播时梯度沿 \(W_h\) 连乘：乘的是同一个矩阵的幂。若其谱半径小于 1，梯度指数衰减——句子开头的信息到不了参数更新里。

LSTM（Long Short-Term Memory，长短期记忆网络）的洞察是：**给信息修一条近似恒等的「高速通道」，让梯度沿加法流动而不是沿矩阵乘法流动**。它在隐状态 \(h_t\) 之外增设一条**单元状态**（cell state）\(c_t\)，更新式是逐元素（\(\odot\)）的加法：

\[ c_t = f_t \odot c_{t-1} + i_t \odot g_t \]

第一项是「旧记忆 × 遗忘门」，第二项是「新内容 × 输入门」。关键在于 \(c_{t-1}\) 到 \(c_t\) 是**乘门控、再相加**：若 \(f_t \approx 1\) 且 \(i_t \approx 0\)，则 \(c_t \approx c_{t-1}\)，信息原样流过任意多步；对 \(c\) 求梯度时出现的是 \(f_t\) 的连乘而非 \(W_h\) 的连乘，而 \(f_t\) 是学出来的、可以逼近 1，梯度因此能「活」很久。三道门与候选值的完整定义：

\[ \begin{aligned} f_t &= \sigma(W_f x_t + U_f h_{t-1} + b_f) &&\text{遗忘门：留多少旧记忆} \\ i_t &= \sigma(W_i x_t + U_i h_{t-1} + b_i) &&\text{输入门：写多少新内容} \\ g_t &= \tanh(W_g x_t + U_g h_{t-1} + b_g) &&\text{候选内容} \\ o_t &= \sigma(W_o x_t + U_o h_{t-1} + b_o) &&\text{输出门：暴露多少记忆} \\ h_t &= o_t \odot \tanh(c_t) &&\text{对外可见的隐状态} \end{aligned} \]

其中 \(\sigma\) 是 sigmoid，把门的开口限制在 \((0,1)\)。代价是参数量：四个门各有一套 \((W, U, b)\)，同规模下 LSTM 的循环核参数约是简单 RNN 的 4 倍。

#### 4.2.2 核心流程

PyTorch 把上述公式封装进 `nn.LSTM(input_size, hidden_size)`，调用接口与 `nn.RNN` 几乎相同，唯一外显差异是**返回值多一个**：

```text
nn.RNN  输入 packed → 返回 output, hidden
nn.LSTM 输入 packed → 返回 output, (hidden, cell)
```

`hidden` 与 `cell` 形状均为 `[层数, batch, hidden_dim]`。分类时通常仍取 `hidden`（也可试 `cell` 或两者拼接，见练习 3）。本 notebook 的使用流程：

1. 打包嵌入序列（与 4.1 完全一致）；
2. `packed_output, (hidden, cell) = self.rnn(packed)`；
3. `self.fc(hidden.squeeze(0)).view(-1)` 输出 logit。

#### 4.2.3 源码精读

LSTM 版 notebook 与简单 RNN 版逐字相同的部分（数据管线、超参、训练循环）见 4.1.3，这里只精读差异。数据管线两处设置同样在位：[rnn_lstm_packed_imdb.ipynb:L146-L150](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_lstm_packed_imdb.ipynb#L146-L150) 声明 `include_lengths=True` 的 Field，[L228-L232](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_lstm_packed_imdb.ipynb#L228-L232) 打开 `sort_within_batch=True`。

模型单元是全篇唯一实质改动：

[rnn_lstm_packed_imdb.ipynb:L316-L339](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_lstm_packed_imdb.ipynb#L316-L339) 与简单 RNN 版相比只有两处不同：L324 把循环核换成 `self.rnn = nn.LSTM(embedding_dim, hidden_dim)`；L337 把返回值解包为 `packed_output, (hidden, cell) = self.rnn(packed)`——元组里的 `cell` 就是单元状态 \(c_t\) 在每个样本真实末词处的值。L339 的分类头仍然只吃 `hidden.squeeze(0)`。

训练输出（notebook 保存结果，[L515-L516](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_lstm_packed_imdb.ipynb#L515-L516)）：15 个 epoch 总耗时 3.07 min，**测试准确率 85.08%**。对照简单 RNN 打包版的 73.94%——同样的数据、种子与超参，门控单元带来约 11 个百分点的提升；代价是单 epoch 耗时从约 0.13 min 升到约 0.20 min（循环核参数翻 4 倍所致）。

训练曲线上还有一个值得注意的现象（见 [L380](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_lstm_packed_imdb.ipynb#L380) 起的评估函数与训练单元保存的逐 epoch 输出）：第 12 个 epoch 验证准确率曾从 83.18% 骤降到 74.56%，下一 epoch 又恢复到 83.68%——RNN 类模型的训练方差大，单 epoch 波动不代表趋势，这也是后面做对比实验时要看完整曲线、多看几个 epoch 的原因。

#### 4.2.4 代码实践：数一数三种循环核的参数

**实践目标**：用参数量公式验证「LSTM ≈ 4×RNN」，体会门控的容量代价。

**操作步骤**（示例代码，非仓库原有内容）：

```python
import torch.nn as nn

for name, rnn in [('RNN', nn.RNN(128, 256)),
                  ('LSTM', nn.LSTM(128, 256)),
                  ('GRU', nn.GRU(128, 256))]:
    n = sum(p.numel() for p in rnn.parameters())
    print(f'{name}: {n:,}')
```

**需要观察的现象与预期结果**：`nn.RNN`、`nn.LSTM`、`nn.GRU` 各自的每个「门」含输入权重 \(W\in\mathbb{R}^{256\times128}\)、隐状态权重 \(U\in\mathbb{R}^{256\times256}\) 与两个偏置（各 256），故单个门的参数为 \(32768+65536+512=98{,}816\)；门数分别为 1、4、3，预期输出依次为 `RNN: 98,816`、`LSTM: 394,240`、`GRU: 296,448`。若你的输出与此不符，检查 `input_size/hidden_size` 是否一致。再结合模型其余部分（Embedding \(20002\times128=2{,}560{,}256\) 个、全连接头 257 个）即可解释 u6-l1 说过的「Embedding 占总参数约 98%」——门控翻 4 倍的只是那不到 2% 的一小段（以上数值按公式推得，待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么说 cell state 是「加法传送带」？它与简单 RNN 中 \(h_t\) 的更新方式有何本质区别？

**答案**：\(c_t = f_t \odot c_{t-1} + i_t \odot g_t\) 对旧状态做的是逐元素乘门控后相加，不存在对固定权重矩阵的连乘；当 \(f_t\to 1, i_t\to 0\) 时 \(c_t\approx c_{t-1}\)，梯度沿这条通路近似无损。简单 RNN 的 \(h_t=\tanh(W_h h_{t-1}+W_x x_t)\) 每一步都强制过一次 \(W_h\) 与 tanh，反向传播梯度含 \(W_h\) 的长链连乘，容易指数衰减。

**练习 2**：写出 LSTM 输出的解包代码。如果误写成简单 RNN 的 `output, hidden = self.rnn(packed)` 会怎样？

**答案**：正确写法是 `packed_output, (hidden, cell) = self.rnn(packed)`。LSTM 返回的第二个值是 `(hidden, cell)` 元组；误拆成两个变量时，`hidden` 实际拿到的是该元组、`cell` 拿到 None 或引发解包错误，随后的 `hidden.squeeze(0)` 会因对元组调用而报 `AttributeError`（具体报错形式随版本不同，待本地验证）。

**练习 3**：本 notebook 分类头只用了 `hidden`。若改用 `cell.squeeze(0)` 或 `torch.cat([hidden, cell], dim=-1)`（后者需相应放宽 `fc` 的输入维度），你预期会发生什么？

**答案**：这是仓库之外的自然延伸实验（标注为示例改动）。`hidden = o_t ⊙ tanh(c_t)` 经过了输出门与 tanh 的「过滤」，`cell` 则是更原始的记忆。经验上三种选择通常都能训练到相近量级、但收敛曲线与最终准确率会有可测差异；拼接版因输入维度翻倍参数略增。结论不普适，值得在综合实践中顺手加测一组（待本地验证）。

### 4.3 GRU：更轻量的门控

#### 4.3.1 概念说明

GRU（Gated Recurrent Unit，门控循环单元；notebook 标题里写作 "Gated Rectified Unit"，属笔误，通用名称是 Gated Recurrent Unit）是 LSTM 的简化版：**去掉独立的 cell state，把记忆直接放在 \(h_t\) 里，三道门并成两道**。按 PyTorch 文档口径：

\[ \begin{aligned} z_t &= \sigma(W_z x_t + U_z h_{t-1} + b_z) &&\text{更新门} \\ r_t &= \sigma(W_r x_t + U_r h_{t-1} + b_r) &&\text{重置门} \\ \tilde{h}_t &= \tanh(W_h x_t + U_h (r_t \odot h_{t-1}) + b_h) &&\text{候选状态} \\ h_t &= (1-z_t) \odot h_{t-1} + z_t \odot \tilde{h}_t &&\text{凸组合式更新} \end{aligned} \]

两个设计要点：

- 更新门让 \(h_t\) 成为旧状态与新候选的**凸组合**：\(z_t\to 0\) 时 \(h_t\approx h_{t-1}\)，同样形成一条近似恒等的高速通道，保留 LSTM 抗梯度消失的核心优点；
- 重置门 \(r_t\) 决定计算候选状态时要「遗忘」多少旧记忆，替代了 LSTM 遗忘门/输入门的分工。

少一道门、少一条状态线，参数约是同规模 LSTM 的 3/4（见 4.2.4 的实测：296,448 对 394,240），计算也更快；在中小规模任务上精度与 LSTM 常在伯仲之间。

#### 4.3.2 核心流程

GRU 的 PyTorch 接口与 `nn.RNN` **完全一致**：

```text
nn.GRU 输入 packed → 返回 output, hidden      # 与 nn.RNN 相同，没有 cell
```

因此从简单 RNN 迁移到 GRU 只需改一行构造代码；从 LSTM 改成 GRU 则还要把返回值解包从元组改回单值。本 notebook 流程与 4.2 完全同构：打包 → `packed_output, hidden = self.rnn(packed)` → `fc(hidden.squeeze(0))`。

#### 4.3.3 源码精读

GRU 版与简单 RNN 版的模型单元差异**只有一个词**：

[rnn_gru_packed_imdb.ipynb:L330-L353](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_gru_packed_imdb.ipynb#L330-L353) 定义的 `RNN` 类中，L338 是 `self.rnn = nn.GRU(embedding_dim, hidden_dim)`（对比简单 RNN 版同位置的 `nn.RNN`）；L346 打包、L351 解包为 `packed_output, hidden = self.rnn(packed)`（无 cell）、L353 `fc(hidden.squeeze(0))` 出 logit。

数据管线同样保留两处打包前提：[L160-L164](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_gru_packed_imdb.ipynb#L160-L164) 的 `include_lengths=True` 与 [L242-L246](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_gru_packed_imdb.ipynb#L242-L246) 的 `sort_within_batch=True`。这份 notebook 的数据加载单元还保留了 IMDb 首次运行时下载 `aclImdb_v1.tar.gz`（84.1 MB）的进度输出——本地复现第一次会先下载数据。

训练结果（[L529-L530](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_gru_packed_imdb.ipynb#L529-L530)）：总耗时 2.79 min，**测试准确率 85.09%**——与 LSTM 的 85.08% 几乎打平，训练时间却少约 9%。三份 notebook 的三单元对照汇总：

| 循环核 | 循环核参数（公式推得） | 最终验证准确率 | 测试准确率 | 总训练时间（notebook 保存输出） |
| --- | --- | --- | --- | --- |
| `nn.RNN`（打包） | 98,816 | 73.72% | 73.94% | 2.01 min |
| `nn.LSTM`（打包） | 394,240 | 84.16% | 85.08% | 3.07 min |
| `nn.GRU`（打包） | 296,448 | 84.88% | 85.09% | 2.79 min |

结论要克制地读：在 IMDb + 这组超参下，门控单元比简单 RNN 高约 11 个百分点，LSTM 与 GRU 之间难分高下；这是**单次运行**的结果（u3-l2 已强调过单次对比的局限），但它与「GRU 参数更少、速度更快、精度常与 LSTM 相当」的普遍经验一致。

#### 4.3.4 代码实践：读源码找差异

**实践目标**：亲身验证「三份 notebook 只在模型单元有实质差异」，建立用受控对比读源码的手感。

**操作步骤**：

1. 在本地分别打开三份 notebook 的模型定义单元（简单 RNN 版 [L318-L341](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_simple_packed_imdb.ipynb#L318-L341)、LSTM 版 [L316-L339](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_lstm_packed_imdb.ipynb#L316-L339)、GRU 版 [L330-L353](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_gru_packed_imdb.ipynb#L330-L353)），逐行对照；
2. 列出所有不同点，区分「实质差异」（影响计算）与「表面差异」（变量命名、注释）；
3. 再对照训练循环与超参单元，确认其余部分逐字一致。

**需要观察的现象**：实质差异应恰好两处——循环核构造那一行（`nn.RNN`/`nn.LSTM`/`nn.GRU`），以及返回值解包那一行（LSTM 多一个 `cell`）；另有注释里的变量名 `output` 与 `packed_output` 之别属表面差异。

**预期结果**：得到一张三列对照表；这张表同时是综合实践（第 5 节）的实验设计依据——既然代码只差这一行，准确率与耗时的差异就可归因于循环核本身。

#### 4.3.5 小练习与答案

**练习 1**：GRU 与 LSTM 各有几道门？GRU 去掉了什么、合并了什么？

**答案**：LSTM 有遗忘、输入、输出三道门加一条独立 cell state；GRU 只保留更新门 \(z_t\) 与重置门 \(r_t\) 两道，没有独立 cell state——记忆直接存于 \(h_t\)。更新门同时扮演了 LSTM 遗忘门与输入门的角色（凸组合 \(h_{t-1}\) 与候选 \(\tilde{h}_t\)），输出门的角色被并入 \(h_t\) 的定义本身。

**练习 2**：GRU 的 \(h_t=(1-z_t)\odot h_{t-1}+z_t\odot\tilde{h}_t\) 为什么也能缓解梯度消失？

**答案**：因为 \(h_{t-1}\) 到 \(h_t\) 的通路是逐元素门控加法，不含对固定权重矩阵的强制连乘；当 \(z_t\approx 0\) 时 \(h_t\approx h_{t-1}\)，梯度近似原样回传。这与 LSTM cell state 的传送带原理相同，只是载体从 \(c_t\) 换成了 \(h_t\) 本身。

**练习 3**：同样的 `HIDDEN_DIM=256`，GRU 的循环核参数比 LSTM 少多少？

**答案**：每道门 98,816 个参数（见 4.2.4 的拆解），GRU 3 道门共 296,448，LSTM 4 道门共 394,240，少 97,792 个，即少 \(1/4\)。但相对整个模型（Embedding 占约 256 万），这部分差异只影响总参数的百分之几——门控类型的选择主要是在「收敛能力 vs 计算速度」之间权衡，而非总容量。

## 5. 综合实践

**任务：三单元受控对比实验（对应本讲实践任务）**。

在同一数据划分、同一超参下分别训练 packed 版的简单 RNN、LSTM、GRU，对比验证准确率与单 epoch 耗时，亲手复现（或挑战）第 4.3.3 节的表格。

**操作步骤**：

1. **准备环境**：按 u1-l2/u6-l1 的方式准备能运行 torchtext 旧版 API 的环境；首次运行会自动下载 IMDb（约 84 MB，GRU 版 notebook 的输出里能看到下载记录）。
2. **跑通一份**：先完整运行 `rnn_simple_packed_imdb.ipynb`（超参在 [L92-L101](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_simple_packed_imdb.ipynb#L92-L101)），确认数据管线与训练循环正常。
3. **只改一处**：复制该 notebook 两次，分别把循环核换成 `nn.LSTM`（注意返回值解包为 `(hidden, cell)`，对照 [LSTM 版 L337](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_lstm_packed_imdb.ipynb#L337)）与 `nn.GRU`（对照 [GRU 版 L338、L351](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_gru_packed_imdb.ipynb#L338-L351)），其余一律不动——种子、划分、超参保持一致，这是「受控」的全部含义。
4. **记录数据**：为每个模型记录每 epoch 的验证准确率、总训练时间、测试准确率，填入下表（耗时列为空，待本地实测）：

| 循环核 | 每 epoch 平均耗时 | 最终验证准确率 | 测试准确率 | 备注 |
| --- | --- | --- | --- | --- |
| `nn.RNN` |  |  |  | 仓库参考值：73.72% / 73.94% / 2.01 min |
| `nn.LSTM` |  |  |  | 仓库参考值：84.16% / 85.08% / 3.07 min |
| `nn.GRU` |  |  |  | 仓库参考值：84.88% / 85.09% / 2.79 min |

5. **加测一个消融**（可选）：再复制一份 `nn.RNN` 版，去掉 `pack_padded_sequence`（同时模型 forward 去掉 `text_length` 参数），观察准确率与耗时如何退化——对应 u6-l1 不打包版的 46.38% / 6.35 min。

**预期结果**：门控单元（LSTM/GRU）的验证与测试准确率显著高于简单 RNN（约 +10 个百分点量级），LSTM 与 GRU 互有胜负；耗时排序大致为 LSTM > GRU > RNN（与循环核参数量 4:3:1 一致）。你的绝对数值会随硬件与 torchtext/torch 版本浮动，这是正常的；重要的是**同一环境内三种单元的相对结论**是否与仓库保存输出一致。若与预期方向不符，优先排查：三份实验的种子与数据划分是否真的相同、`sort_within_batch` 是否被意外改动、以及是否误用了不同 batch 大小。全部数值待本地验证。

## 6. 本讲小结

- padding 有双重代价：浪费算力，且让最终隐状态对应最后一个 `<pad>` 而非真正的句尾，稀释句子的语义压缩——`pack_padded_sequence` 一次解决两者（本例：46.38%→73.94%，6.35→2.01 min）。
- 打包的两个数据管线前提：`data.Field(include_lengths=True)` 让 batch 携带真实长度，`BucketIterator(sort_within_batch=True)` 保证 batch 内长度降序（`enforce_sorted=True` 的默认要求）。
- `PackedSequence` 用 `data`（有效时间步拼接）+ `batch_sizes`（每步存活样本数）描述变长 batch，RNN 内核据此跳过 padding；本仓库三份 notebook 因只用 `hidden` 而未调用 `pad_packed_sequence`，需要逐时间步输出时它才是必需的搭档。
- LSTM 以 cell state 加法传送带 \(c_t=f_t\odot c_{t-1}+i_t\odot g_t\) 对抗梯度消失，接口差异是返回 `(hidden, cell)` 元组；GRU 用两道门、无独立 cell，接口与 `nn.RNN` 完全一致，参数约为 LSTM 的 3/4。
- 相同数据与超参下（notebook 保存输出）：RNN 73.94%、LSTM 85.08%、GRU 85.09%；LSTM 与 GRU 打平而 GRU 更快——门控的价值明确，两者的取舍要看具体任务与算力。
- 三份 notebook 构成一次教科书级的受控对比：全部差异收敛在模型单元的一两行，这正是「克隆基线、只改一处」方法论在序列建模上的再现。

## 7. 下一步学习建议

- **下一讲（u6-l3）**将扩展本讲的循环核：`bidirectional=True` 与 `num_layers>1` 如何改变 `hidden` 的形状与语义、如何用 GloVe 预训练向量初始化 Embedding、以及怎样把自有 CSV 文本接入同一套管线——建议先想清楚「双向时 `hidden[-1]` 还是不是句尾状态」再读。
- 想看梯度如何沿门控流动的读者，可回看 u2-l5 的手工反向传播与 u4-l5 的 `autograd.grad`/hook 技巧，用它们抓取 \(f_t, i_t, o_t\) 或 \(c_t\) 的梯度做实证观察。
- 预习 Transformer 之前，可先读本单元的 char-RNN（u6-l4）：它是无 padding 的 many-to-many 语言模型（逐时间步交叉熵、自回归采样），与 u6-l6 DistilBERT「一次并行读全句」形成最好参照——两者的对比会凸显循环模型串行性的本质代价。

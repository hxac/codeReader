# u15-l1 RNN 原理：从手算示例到序列架构

## 1. 本讲目标

本讲是循环神经网络（RNN）单元的第一讲。学完后你应该能够：

1. **手算一遍 RNN 隐状态递推**：给定 \(W_{xh}\)、\(W_{hh}\)、偏置和两个时间步的输入，能不借助 `nn.RNN` 手写出每个时间步的隐状态，并理解隐状态 \(h\) 为什么是"记忆"。
2. **理解 tanh 非线性在递推中的角色**：它把无界的加权和压缩到 \((-1,1)\)，同时其导数特性埋下了"长程梯度消失"的种子（这正是下一讲 LSTM 要解决的问题）。
3. **区分五种序列架构**：一对一、一对多、多对一、（异步）多对多、同步多对多，并能为具体任务（情感分类、机器翻译、语言模型）对号入座。
4. **理解 PyTorch 的维度约定** `[batch, seq_len, feature]`：知道 `batch_first=True` 改变了什么，能读懂 `torch.reshape(x, (1, 3, 5))` 里三个数字的含义。

本讲的所有结论都锚定在仓库真实代码上：ch15_part1.py 中一段仅 40 行的手算循环，用 `torch.matmul` 复现了 `nn.RNN` 的内部计算，逐时间步打印两组结果并逐一比对。

## 2. 前置知识

### 2.1 序列数据：顺序携带信息

之前章节处理的大多是无序样本：一张图片、一条影评的词袋向量、一个住房样本。它们共同点是**打乱特征顺序（在合理预处理后）不影响预测**。而序列数据（sequence）——一句话、一段音频、一串股价、DNA 片段——满足：

- 有天然的**先后顺序**："猫追狗"和"狗追猫"词相同而意思相反；
- **长度可变**：一句话 5 个词，另一句 50 个词。

书中用记号 \(x^{(1)}, x^{(2)}, \dots, x^{(T)}\) 表示一条长度为 \(T\) 的序列，上标 \((t)\) 是**时间步**（time step），不是幂。注意：序列数据不等于时间序列数据——时间序列是按真实时间采样的数值序列（如股价），序列数据范围更广，文本也是序列。

### 2.2 为什么第 11/12 章的前馈网络不够用

回顾我们在 u11 手写的 `NeuralNetMLP` 和 u12 的 `nn.Sequential`：它们把整个输入一次性灌入网络。拿来处理序列有三个硬伤：

1. **输入维度固定**：网络第一个权重矩阵的形状 `(num_hidden, num_features)` 钉死了特征数，也就钉死了序列长度；测试时来一条更长的句子就无能为力。
2. **没有记忆**：每个样本独立预测，第 1 个词的信息无法"留到"第 10 个词再用。
3. **参数不共享**：如果为每个位置单独配一套权重，参数量随序列长度线性膨胀，且位置 3 的"猫"和位置 7 的"猫"学到的是两套东西。

RNN 的解法很优雅：**同一套权重沿时间步复用**（类比 CNN 沿空间共享卷积核），再用一个沿时间传递的**隐状态**（hidden state）充当记忆。

### 2.3 你需要带走的两件旧工具

- **矩阵乘法与转置**（u12-l1）：`A @ B` 要求中间维度相消；`torch.transpose(w, 0, 1)` 交换前两个维度。PyTorch 权重沿用 `(输出维, 输入维)` 约定（同 `nn.Linear`，见 u12-l3），所以计算时要转置。
- **tanh 激活函数**（u11-l1 的 sigmoid 的"亲戚"）：\(\tanh(z) = \frac{e^z - e^{-z}}{e^z + e^{-z}}\)，值域 \((-1,1)\)，零中心，导数为 \(1 - \tanh^2(z)\)，在 \(z=0\) 处导数最大（为 1），向两端饱和趋 0。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [ch15/ch15_part1.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part1.py) | 本讲主源码：创建 `nn.RNN`、提取权重、用手写 matmul 循环逐步复现其前向输出。全章其余小节（序列分类、LSTM 等）在该文件中只保留 markdown 标题注释，图示在 notebook 里 |
| [ch15/ch15_part1.ipynb](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part1.ipynb) | notebook 版：额外携带章节插图（`figures/15_01.png` ~ `15_09.png`）和已运行的真实输出，本讲引用的数值全部来自这里 |
| [ch15/figures/15_02.png](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/figures/15_02.png) | 五种序列建模架构的示意图（notebook 中 `Image(filename='figures/15_02.png')` 加载） |
| [ch15/README.md](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/README.md) | 第 15 章大纲：part1 讲原理，part2 是 IMDb 情感分类，part3 是字符级语言模型 |

阅读提示（承接 u1-l1 的双轨工作流）：读代码和引用行号用 `.py`；想看插图和运行输出用 `.ipynb`。

## 4. 核心概念与源码讲解

### 4.1 隐状态递推：W_xh、W_hh、b_h 手算

#### 4.1.1 概念说明

RNN 的核心只有一行递推公式（书中按列向量记法写作）：

\[
h^{(t)} = \tanh\left(W_{xh}\,x^{(t)} + W_{hh}\,h^{(t-1)} + b_h\right)
\]

其中：

- \(x^{(t)} \in \mathbb{R}^{5}\)：第 \(t\) 个时间步的输入向量（本讲代码中每步是 5 维）；
- \(h^{(t)} \in \mathbb{R}^{2}\)：第 \(t\) 步的**隐状态**，本例隐藏层 2 个单元；
- \(W_{xh} \in \mathbb{R}^{2\times5}\)：把"当前输入"投影进隐藏层的权重；
- \(W_{hh} \in \mathbb{R}^{2\times2}\)：把"上一步记忆"投影进隐藏层的权重；
- \(b_h\)：偏置；\(h^{(0)}\) 通常初始化为全零。

这个公式为什么体现了"记忆"？把 \(h^{(t-1)}\) 反复代入展开，可以看到 \(h^{(T)}\) 是 \(x^{(1)}, \dots, x^{(T)}\) **全部历史输入**的函数——每一步的输入都会在隐状态里留下痕迹，随时间衰减或保留。同时 \(W_{xh}, W_{hh}\) 在**每个时间步都是同一套**，参数量与序列长度无关，这解决了 2.2 节的三个硬伤。

一个常见的记法提醒：书中也把该递推的输出记作 \(o^{(t)}\)（elman RNN 一节的记号），源码里也用了变量名 `ot`。**它就是隐状态**——在 `nn.RNN` 内部，每个时间步的"输出"就是该步的隐状态本身，没有额外的输出变换。

#### 4.1.2 核心流程

把一条长度 \(T=3\) 的序列喂给单层 RNN，前向计算流程如下（行向量记法，与代码一致）：

```text
初始化 h_0 = 0
for t = 1 .. T:
    输入贡献:  a_t = x_t @ W_xhᵀ + b_xh        # 形状 (1, 2)
    记忆贡献:  r_t = h_{t-1} @ W_hhᵀ + b_hh     # 形状 (1, 2)
    隐状态:    h_t = tanh(a_t + r_t)            # 这一步的"输出"
返回 output = [h_1; h_2; ...; h_T]（ hn = h_T ）
```

几何直觉：每个时间步，隐状态是"新证据 \(x_t\)"与"旧记忆 \(h_{t-1}\)"两股信息的加权汇流，再被 tanh 压进 \((-1,1)\) 的有界区间。时间维度上把循环"展开"（unrolling in time），就得到一条共享权重的深层链——这正是反向传播要在其上回传的路径（BPTT，沿时间反向传播）。

注意 PyTorch `nn.RNN` 的一个实现细节：它为输入项和递归项**各配一个偏置**（`bias_ih` 与 `bias_hh`），等效总偏置是两者之和。所以手算时要加两个偏置，而不是一个。

#### 4.1.3 源码精读

**第一段：创建 RNN 层并取出权重。**

[ch15/ch15_part1.py:L104-L111](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part1.py#L104-L111) 中先固定随机种子（保证权重初始化可复现），然后创建一个最小规格的 RNN 层——输入 5 维、隐藏 2 维、单层、`batch_first=True`（维度约定见 4.3）。接着把层里的四个参数张量取出来重命名，`l0` 表示第 0 层（layer 0）的参数：

```python
torch.manual_seed(1)
rnn_layer = nn.RNN(input_size=5, hidden_size=2, num_layers=1, batch_first=True)
w_xh = rnn_layer.weight_ih_l0   # 输入→隐藏权重, 形状 (2, 5)
w_hh = rnn_layer.weight_hh_l0   # 隐藏→隐藏权重, 形状 (2, 2)
b_xh = rnn_layer.bias_ih_l0     # 输入侧偏置, 形状 (2,)
b_hh = rnn_layer.bias_hh_l0     # 递归侧偏置, 形状 (2,)
```

形状与 4.1.1 的公式逐一对应。notebook 中记录的真实输出（本讲直接引用，读者可对照）：

```text
W_xh shape: torch.Size([2, 5])
W_hh shape: torch.Size([2, 2])
b_xh shape: torch.Size([2])
b_hh shape: torch.Size([2])
```

**第二段：构造序列输入并让 nn.RNN 前向计算一次**（作为稍后手算的对照组）。

[ch15/ch15_part1.py:L121-L124](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part1.py#L121-L124) 构造了一个极简序列：3 个时间步，每步是值全为 1.0 / 2.0 / 3.0 的 5 维向量。`torch.reshape(x_seq, (1, 3, 5))` 在前面补了一个 batch 维，变成 `(batch=1, seq_len=3, feature=5)`：

```python
x_seq = torch.tensor([[1.0]*5, [2.0]*5, [3.0]*5]).float()
output, hn = rnn_layer(torch.reshape(x_seq, (1, 3, 5)))
```

**第三段：手算循环——本讲的灵魂。**

[ch15/ch15_part1.py:L127-L146](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part1.py#L127-L146) 用最基本的 `torch.matmul` 把递推公式逐步复现。逐行拆解：

[ch15/ch15_part1.py:L129-L134](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part1.py#L129-L134) 取出第 \(t\) 步输入并计算**输入贡献**。`xt` 形状 `(1, 5)`；由于 `w_xh` 按 `(输出维, 输入维) = (2, 5)` 存放（与 `nn.Linear` 同约定，见 u12-l3），要先转置再右乘：`(1,5) @ (5,2) → (1,2)`。书中列向量记法 \(W_{xh}x^{(t)}\) 对应代码里的行向量写法 `xt @ w_xh.T`：

```python
xt = torch.reshape(x_seq[t], (1, 5))
ht = torch.matmul(xt, torch.transpose(w_xh, 0, 1)) + b_xh
print('   Hidden          :', ht.detach().numpy())
```

⚠️ 一个极易误读的点：这行打印的 `Hidden` **不是最终隐状态**，它只包含当前输入的贡献（既没加记忆项，也没过 tanh）。最终隐状态是后面打印的 `Output (manual)`。

[ch15/ch15_part1.py:L136-L139](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part1.py#L136-L139) 决定"上一步记忆"：\(t>0\) 时取手算列表中的上一步结果；\(t=0\) 时用与 `ht` 同形状的零张量，即 \(h_0 = 0\) 的默认初始化：

```python
if t>0:
    prev_h = out_man[t-1]
else:
    prev_h = torch.zeros((ht.shape))
```

[ch15/ch15_part1.py:L141-L145](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part1.py#L141-L145) 完成 4.1.2 流程图的后两步：在输入贡献上叠加**记忆贡献**（`prev_h @ w_hh.T + b_hh`，注意两个偏置都加了），整体过 `torch.tanh` 得到最终隐状态，存入 `out_man`；随后把它与 `nn.RNN` 的官方输出 `output[:, t]`（batch 中第 0 个样本、第 \(t\) 个时间步）并排打印，供肉眼比对：

```python
ot = ht + torch.matmul(prev_h, torch.transpose(w_hh, 0, 1)) + b_hh
ot = torch.tanh(ot)
out_man.append(ot)
print('   Output (manual) :', ot.detach().numpy())
print('   RNN output      :', output[:, t].detach().numpy())
```

notebook 中记录的完整运行结果——每一时间步两行数值**完全一致**，这就是"手算 == 框架"的实证：

```text
Time step 0 =>
   Input           : [[1. 1. 1. 1. 1.]]
   Hidden          : [[-0.4701929  0.5863904]]
   Output (manual) : [[-0.3519801   0.52525216]]
   RNN output      : [[-0.3519801   0.52525216]]

Time step 1 =>
   Input           : [[2. 2. 2. 2. 2.]]
   Hidden          : [[-0.88883156  1.2364397 ]]
   Output (manual) : [[-0.68424344  0.76074266]]
   RNN output      : [[-0.68424344  0.76074266]]

Time step 2 =>
   Input           : [[3. 3. 3. 3. 3.]]
   Hidden          : [[-1.3074701  1.886489 ]]
   Output (manual) : [[-0.8649416  0.90466356]]
   RNN output      : [[-0.8649416  0.90466356]]
```

读一遍这组数字能看到"记忆"在工作：`Hidden` 行（纯输入贡献）随输入从 1→2→3 大致线性外推（第一个分量 −0.47 → −0.89 → −1.31），而最终 `Output (manual)` 与它的差异正是上一步隐状态的贡献。

#### 4.1.4 代码实践

**实践目标**：亲手跑通仓库代码，确认手算循环与 `nn.RNN` 逐步一致，并触摸四个参数张量的形状。

**操作步骤**：

1. 激活 u1-l2 搭好的 `pyml-book` 环境，进入仓库的 `ch15` 目录（脚本内部用 `sys.path.insert(0, '..')` 引入根目录的 `python_environment_check.py`，所以要从 `ch15` 内运行）：

   ```bash
   conda activate pyml-book
   cd ch15
   python ch15_part1.py
   ```

2. 对照打印出的四个形状与 4.1.1 公式中的维度。
3. 把终端里三个时间步的 `Output (manual)` 与 `RNN output` 逐个比对。
4. 进阶：复制 L104-L146 到自己的新文件（**不要改动仓库源码**），把 `hidden_size` 从 2 改为 4，重跑。

**需要观察的现象**：每个时间步的手算输出与框架输出数值完全相同；`hidden_size=4` 后形状变为 `W_xh (4,5)`、`W_hh (4,4)`、两个偏置 `(4,)`，且手算与框架输出仍然一致。

**预期结果**：与 4.1.3 引用的 notebook 记录输出一致（随机种子固定为 1，数值可精确复现）。若在你的 PyTorch 版本上末位小数有微小出入，属正常浮点差异。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `w_xh` 形状是 `(2, 5)`，而 `w_hh` 是 `(2, 2)`？

**答案**：`nn.RNN` 按 `(输出维, 输入维)` 存权重。输入→隐藏：5 维输入投影到 2 维隐藏，得 `(2, 5)`；隐藏→隐藏：2 维记忆投影回 2 维，得 `(2, 2)`。

**练习 2**：手算循环中 `t=0` 时的 `prev_h` 是什么？为什么可以直接用零张量？

**答案**：全零张量（形状同 `ht`）。因为序列开始前没有历史，隐状态的默认初始化就是 \(h_0 = 0\)；这等价于 `nn.RNN` 不显式传 `h0` 时的内部行为。

**练习 3**：手算为什么要同时加 `b_xh` 和 `b_hh` 两个偏置？

**答案**：PyTorch 的 `nn.RNN` 把公式中的偏置拆成输入侧 `bias_ih` 与递归侧 `bias_hh` 两个独立可学参数，等效偏置 \(b_h = b_{xh} + b_{hh}\)。手算求和后才与框架对齐。

### 4.2 torch.tanh 非线性：有界记忆与输出层

#### 4.2.1 概念说明

递推公式里的 tanh 不只是"照抄 MLP 的激活函数"，它承担三重角色：

1. **提供非线性**：若去掉 tanh，递推退化为线性运算的嵌套，\(h^{(T)}\) 只是 \(x^{(1)}, \dots, x^{(T)}\) 的线性组合，整个展开网络塌缩成一层线性模型——与 u11-l1 里"没有激活函数的多层网络等价于单层"是同一个道理。
2. **把记忆压进有界区间**：\(\tanh(z) \in (-1, 1)\)。隐状态无论递推多少步都不会数值爆炸，每一步的"记忆强度"天然被限制在 \(\pm 1\)。
3. **埋下长程依赖的困难**：反向传播要沿时间连乘 tanh 的导数 \(\frac{\partial o}{\partial z} = 1 - o^2 \le 1\)。每回传一步至多乘 1，绝大多数位置远小于 1（饱和区导数趋 0），于是相隔很多步的两个位置之间梯度**指数级衰减**——这就是"学习长程交互的挑战"（书中配图 [ch15/figures/15_08.png](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/figures/15_08.png)），也是 LSTM（门控机制，配图 [ch15/figures/15_09.png](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/figures/15_09.png)）要解决的问题，留给 u15-l2。

另外要分清两件事：`nn.RNN` **内部没有输出层**——它在每个时间步吐出的就是隐状态本身（4.1 的对照实验证明了这一点）。做实际任务时，需要在 RNN 之上**再接一个全连接输出层**：

\[
o^{(t)}_{\text{task}} = W_{ho}\,h^{(t)} + b_o
\]

例如下一讲 IMDb 情感分类就是"RNN 编码整句 → 取隐状态 → 全连接 → sigmoid"。书中还区分了**隐藏递归**（recurrence 作用在隐状态上，即本讲的标准 RNN，表达力更强，配图 [ch15/figures/15_07.png](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/figures/15_07.png)）与**输出递归**（把输出反馈回输入）两种变体，初学记住前者即可。

#### 4.2.2 核心流程

以本讲数据观察 tanh 的"压缩"效果（数值来自 4.1.3 的 notebook 输出）：

| 时间步 | 输入值 | 加权和（未过 tanh，含记忆项） | tanh 后的隐状态 |
| --- | --- | --- | --- |
| \(t=1\) | 全 1.0 | 约 −0.47 量级起叠加 | −0.352 / 0.525 |
| \(t=2\) | 全 2.0 | 幅度增大 | −0.684 / 0.761 |
| \(t=3\) | 全 3.0 | 幅度继续增大 | −0.865 / 0.905 |

输入按 1、2、3 线性放大，加权和（`Hidden` 行的 −0.47 → −0.89 → −1.31 可作参照）大致线性增长，而隐状态被 tanh 压得越来越"挤"——第一分量向 −1 靠拢、第二分量向 +1 靠拢，逐渐**饱和**。饱和意味着该单元的导数 \(1-o^2\) 趋近 0，信号几乎不再更新，这正是长序列上梯度消失的直观前兆。

#### 4.2.3 源码精读

[ch15/ch15_part1.py:L141-L143](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part1.py#L141-L143) 是 tanh 唯一登场的位置：先把"输入贡献 + 记忆贡献 + 两个偏置"加总，再整体过 `torch.tanh`，得到该时间步的最终隐状态并存入手算列表：

```python
ot = ht + torch.matmul(prev_h, torch.transpose(w_hh, 0, 1)) + b_hh
ot = torch.tanh(ot)
out_man.append(ot)
```

注意 `out_man` 存的是 **tanh 之后**的值，下一步的 `prev_h` 用的也是它——即递推传递的永远是压缩后的有界隐状态，这保证整条链上任何 \(h^{(t)}\) 都落在 \((-1,1)\) 内。而 [ch15/ch15_part1.py:L145](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part1.py#L145) 打印的 `output[:, t]` 与之逐位相等，说明 `nn.RNN` 暴露给外部的 `output` 就是每个时间步的隐状态序列（`hn` 则是最后一个时间步的隐状态，单层时 `output[:, -1]` 与 `hn[0]` 相同——可在实践中自行验证）。

#### 4.2.4 代码实践

**实践目标**：用"消融"的方式验证 tanh 的两个作用——非线性与有界压缩。

**操作步骤**（在自己的新文件中进行，不改仓库源码）：

1. 把 [ch15/ch15_part1.py:L121-L146](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part1.py#L121-L146) 复制到一个新脚本。
2. 第一遍原样运行，记录三个 `Output (manual)`。
3. 第二遍把 `ot = torch.tanh(ot)` 这一行删掉（或注释掉），再运行。
4. 用 `torch.arange` 生成 −4 到 4 的等距张量，画 `torch.tanh(x)` 与其导数 `1 - tanh(x)**2` 的曲线（可复用 u1-l3 的 matplotlib 画法）。

**需要观察的现象**：删掉 tanh 后，`Output (manual)` 与 `RNN output` **不再相等**（框架内部仍有 tanh），且手算值幅度明显更大、随时间步线性放大而无饱和迹象；tanh 导数曲线在 0 处为 1、向两端快速衰减到 0。

**预期结果**：手算隐状态无 tanh 时第一分量将朝 −0.47、−0.89、−1.31 的方向继续线性外推（叠加记忆项），不再被压进 \((-1,1)\)。具体数值待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：用输出 \(o\) 表示 tanh 的导数，并说明它在何处最大、何处趋于 0。

**答案**：\(\frac{do}{dz} = 1 - o^2\)。在 \(o=0\)（即 \(z=0\)）处最大，值为 1；当 \(o \to \pm 1\)（饱和区，\(|z|\) 大）时趋于 0。

**练习 2**：`nn.RNN` 返回的 `output` 和 `hn` 各是什么？单层时有何关系？

**答案**：`output` 形状 `(batch, seq_len, hidden_size)`，是**每个时间步**的隐状态序列；`hn` 形状 `(num_layers, batch, hidden_size)`，是**最后一个时间步**的隐状态。单层 RNN 中 `output[:, -1]` 等于 `hn[0]`。

**练习 3**：如果要在 RNN 之上做情感二分类（整句一个输出），输出层应该怎么接？

**答案**：RNN 本身没有任务输出层。取（通常是最末时间步的）隐状态 \(h^{(T)}\)，接全连接层 \(W_{ho}h^{(T)} + b_o\) 再过 sigmoid，即 4.2.1 的 \(o_{\text{task}}\)——这正是下一讲 IMDb 模型的结构。

### 4.3 五种序列架构与 [batch, seq_len, feature] 维度约定

#### 4.3.1 概念说明

按"输入序列长度"和"输出序列长度"的组合，序列建模分五类（书中配图 [ch15/figures/15_02.png](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/figures/15_02.png)，在 notebook 中以 `Image(filename='figures/15_02.png')` 展示）：

| 架构 | 输入 → 输出 | 典型任务 | 本书对应 |
| --- | --- | --- | --- |
| 一对一 | \(x \to y\)（无序列） | 普通分类/回归，如 MNIST | 第 11–14 章的所有模型 |
| 一对多 | 1 个输入 → 序列输出 | 图像描述生成 | —（概念） |
| 多对一 | 序列输入 → 1 个输出 | 影评情感分类 | ch15_part2（u15-l2） |
| 多对多（异步） | 序列 → 另一长度序列 | 机器翻译（先读完整句再逐词输出） | —（概念） |
| 同步多对多 | 每个时间步都有一个输出 | 逐帧视频分类、字符级语言模型 | ch15_part3（u15-l3） |

区分它们的关键是问两个问题：**输入端是否逐步喂**？**输出端在哪些时间步产生预测**？多对一只在最后一步读出；同步多对一每步都读出；异步多对二先编码完整输入再解码输出（编码器-解码器雏形）。

与架构同样重要的是 PyTorch 的**张量维度约定**。`nn.RNN` 的输入是一个三维张量，`batch_first` 决定 batch 维放在哪：

- `batch_first=True`：`(batch, seq_len, feature)`——一个 batch 里每条样本是"一整条序列"；
- `batch_first=False`（默认）：`(seq_len, batch, feature)`——最外层按时间步组织。

输出同理：`batch_first=True` 时 `output` 形状为 `(batch, seq_len, hidden_size)`。本讲代码统一用 `batch_first=True`，它和 DataLoader 天然堆出的 `(batch, ...)` 形状一致（u12-l2），后续章节也沿用这一约定。

#### 4.3.2 核心流程

本讲代码中的维度流转（一条长度 3、特征 5 的序列，batch=1）：

```text
x_seq 原始形状 (3, 5)                # 3 个时间步 × 每步 5 维特征
        │ torch.reshape(x_seq, (1, 3, 5))
        ▼
输入   (1, 3, 5)   = (batch=1, seq_len=3, feature=5)
        │ nn.RNN 逐步递推（4.1）
        ▼
output (1, 3, 2)   = (batch=1, seq_len=3, hidden_size=2)
hn     (1, 1, 2)   = (num_layers=1, batch=1, hidden_size=2)
```

取第 \(t\) 步结果用 `output[:, t]`（所有 batch、第 t 步、全部隐藏单元），正是 [ch15/ch15_part1.py:L145](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part1.py#L145) 的写法。

#### 4.3.3 源码精读

[ch15/ch15_part1.py:L106](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part1.py#L106) 在创建层时就声明了维度约定：

```python
rnn_layer = nn.RNN(input_size=5, hidden_size=2, num_layers=1, batch_first=True)
```

`input_size` 是**每个时间步**的特征维（不是序列长度！），`hidden_size` 是隐状态维，`num_layers` 控制堆叠层数。`batch_first=True` 必须在构造时指定，它同时约束输入与输出的形状。

[ch15/ch15_part1.py:L121-L124](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part1.py#L121-L124) 演示了如何把一条 `(3, 5)` 的裸序列补成 `(1, 3, 5)` 送入网络——`reshape` 的三个数字从左到右就是 batch、seq_len、feature：

```python
x_seq = torch.tensor([[1.0]*5, [2.0]*5, [3.0]*5]).float()
output, hn = rnn_layer(torch.reshape(x_seq, (1, 3, 5)))
```

而 [ch15/ch15_part1.py:L129](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part1.py#L129) 的 `torch.reshape(x_seq[t], (1, 5))` 则是反向操作：从 `(3, 5)` 序列里按下标取第 \(t\) 步得到 `(5,)`，再补回二维 `(1, 5)` 以便与 `(5, 2)` 的权重做矩阵乘法——手算循环正是在"整条序列"与"单时间步"两种视角之间来回切换。

五种架构的图示本身存放在 [ch15/figures/15_02.png](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/figures/15_02.png)（另有 RNN 展开结构图 [15_03.png](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/figures/15_03.png)、激活计算图 [15_05.png](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/figures/15_05.png) 与 [15_06.png](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/figures/15_06.png)），建议打开 notebook 对照阅读——架构图中"几个输入方块、几个输出方块"就是 4.3.1 表格的图形版。

#### 4.3.4 代码实践

**实践目标**：体会 `batch_first=True` 下三个维度各是谁，并见识"形状合法但语义错误"的陷阱。

**操作步骤**（示例代码，在自己的文件中运行）：

1. 构造与仓库相同的 `x_seq`，分别用 `(1, 3, 5)` 和 `(3, 1, 5)` 两种 reshape 调用同一个 `rnn_layer`：

   ```python
   # 示例代码
   import torch, torch.nn as nn
   torch.manual_seed(1)
   rnn = nn.RNN(input_size=5, hidden_size=2, num_layers=1, batch_first=True)
   x_seq = torch.tensor([[1.0]*5, [2.0]*5, [3.0]*5]).float()

   out_a, hn_a = rnn(x_seq.reshape(1, 3, 5))   # 1 条序列, 3 个时间步
   out_b, hn_b = rnn(x_seq.reshape(3, 1, 5))   # 3 条序列, 各 1 个时间步
   print(out_a.shape, out_b.shape)
   ```

2. 打印两种情况下 `output` 的形状。
3. 对比 `out_a[:, -1]` 与 `out_b`：后者每条"序列"只有 1 个时间步，`prev_h` 永远是零初始化，递归项没有上场机会。

**需要观察的现象**：`(1,3,5)` 得到 `output (1,3,2)`；`(3,1,5)` 得到 `output (3,1,2)`——张量元素总数相同、不报错，但语义从"1 条长度 3 的序列"变成了"3 条长度 1 的序列"，隐状态只在零初始化基础上前进一步。

**预期结果**：`out_b` 的每个时间步输出应等于"只用对应输入行、以 \(h_0=0\) 起步"的单步计算结果，与 `out_a` 的逐步递推值不同。具体数值待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：影评情感分类（一段评论 → 正/负一个标签）属于哪种架构？

**答案**：多对一。整条序列逐步读入，只在最后读出隐状态接分类层；对应 ch15_part2 的 IMDb 项目。

**练习 2**：若不设置 `batch_first=True`（用默认值），形状 `(1, 3, 5)` 的输入会被怎样解释？

**答案**：默认 `batch_first=False` 时输入按 `(seq_len, batch, feature)` 解释，`(1,3,5)` 会被读成"1 个时间步、3 条样本、5 维特征"——与 4.3.4 的陷阱同源。用默认约定时应传入 `(3, 1, 5)`。

**练习 3**：字符级语言模型"每读一个字符、预测下一个字符"属于哪种架构？输出层怎么设计？

**答案**：同步多对多。每个时间步的隐状态都接一个全连接输出层（分类头）给出下一字符的分布（对应 ch15_part3，u15-l3 将精读）。

## 5. 综合实践

**任务**：不使用 `nn.RNN` 的前向，只用 `torch.matmul` / `torch.tanh` 手写两个时间步的 RNN 递推，再与 `nn.RNN(batch_first=True)` 的输出对齐验证——把 4.1 的递推、4.2 的 tanh、4.3 的维度约定串成一条线。

**操作步骤**：

1. 激活 `pyml-book` 环境，新建脚本（示例代码）：

   ```python
   # 示例代码
   import torch
   import torch.nn as nn

   torch.manual_seed(1)
   rnn = nn.RNN(input_size=5, hidden_size=2, num_layers=1, batch_first=True)

   # 1) 取出该层的四组参数（不改数值，仅引用）
   w_xh = rnn.weight_ih_l0     # (2, 5)
   w_hh = rnn.weight_hh_l0     # (2, 2)
   b_xh = rnn.bias_ih_l0       # (2,)
   b_hh = rnn.bias_hh_l0       # (2,)

   # 2) 构造两个时间步的输入
   x1 = torch.ones(1, 5)            # t=1: 全 1.0
   x2 = torch.full((1, 5), 2.0)     # t=2: 全 2.0

   # 3) 手写递推: h_t = tanh(x_t @ W_xhᵀ + h_{t-1} @ W_hhᵀ + b_xh + b_hh)
   h0 = torch.zeros(1, 2)                                            # h_0 = 0
   h1 = torch.tanh(x1 @ w_xh.t() + h0 @ w_hh.t() + b_xh + b_hh)
   h2 = torch.tanh(x2 @ w_xh.t() + h1 @ w_hh.t() + b_xh + b_hh)

   # 4) 用 nn.RNN 做对照组: 堆成 (batch=1, seq_len=2, feature=5)
   x = torch.stack([x1, x2]).unsqueeze(0)     # (2,5) -> (1,2,5)
   output, hn = rnn(x)

   print('手写 h1 :', h1.detach().numpy())
   print('手写 h2 :', h2.detach().numpy())
   print('nn.RNN  :', output.detach().numpy())
   diff = (torch.stack([h1, h2]).unsqueeze(0) - output).abs().max().item()
   print('最大绝对误差 :', diff)
   ```

2. 运行并记录输出；确认误差为 0（或 1e-7 量级的浮点噪声）。
3. 验证三件事并写进笔记：
   - `output` 形状是否为 `(1, 2, 2)`，与 4.3.2 的维度流转一致；
   - `output[0, 0]` 是否等于你的 `h1`、`output[0, 1]` 是否等于 `h2`；
   - `hn[0]` 是否等于 `output[0, -1]`（4.2 练习 2 的结论）。
4. 选做：把 `x2` 改成 `torch.full((1, 5), 3.0)` 再跑一次，观察 `h2` 两个分量是否更靠近 ±1（4.2 的饱和现象）。

**预期结果**：由于手算与 `nn.RNN` 用的是**同一组参数**且递推公式一致，两组结果应逐位相同（最大误差 0）；`t=1` 的手算值还应与 4.1.3 引用的 notebook 输出第一步 `[-0.3519801, 0.52525216]` 一致（同为种子 1、同为全 1 输入起步）。若在你的环境出现末位差异，属浮点实现差异。

## 6. 本讲小结

- RNN 用一行递推 \(h^{(t)} = \tanh(W_{xh}x^{(t)} + W_{hh}h^{(t-1)} + b_h)\) 同时解决"变长输入、无记忆、参数不共享"三个问题：**权重沿时间共享，隐状态充当记忆**。
- [ch15_part1.py:L127-L146](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part1.py#L127-L146) 的手算循环用 `xt @ w_xh.T + b_xh` 算输入贡献、`prev_h @ w_hh.T + b_hh` 算记忆贡献（\(t=0\) 时 `prev_h` 为零）、tanh 压缩，逐步与 `nn.RNN` 输出完全一致。
- 两个易踩的坑：打印的 `Hidden` 只是输入贡献而非最终隐状态；`nn.RNN` 有 `b_ih`、`b_hh` 两个偏置，手算必须都加。
- tanh 是"有界记忆"的来源（隐状态恒在 \((-1,1)\)），但其导数 \(\le 1\) 且饱和区趋 0，导致长序列上梯度指数衰减——这是 LSTM（下一讲）登场的直接动机。
- `nn.RNN` 内部没有任务输出层，每个时间步的输出就是隐状态；实际任务需再接全连接层，接的位置（只取末步 vs 每步都接）决定任务属于五种序列架构中的哪一种。
- `batch_first=True` 约定输入 `(batch, seq_len, feature)`、输出 `(batch, seq_len, hidden_size)`；reshape 三个数字的语义错了不会报错，只会静默改变模型行为。

## 7. 下一步学习建议

本讲只搭好了 RNN 的"骨架与手算"，接下来沿第 15 章继续：

1. **u15-l2（ch15_part2）**：把多对一架构落到实处——构建词表与分词器、用 `collate_batch` 处理变长影评、`nn.EmbeddingBag`/LSTM 编码、训练 IMDb 情感分类器；同时认识 LSTM 如何用门控绕开本讲的梯度消失问题。
2. **u15-l3（ch15_part3）**：同步多对多架构的完整实现——字符级语言模型的定长分块数据集、LSTM 训练循环与文本采样生成。
3. **源码回看**：带着本讲的递推公式重读 [ch15/ch15_part1.py:L104-L146](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part1.py#L104-L146)，确保每个张量形状、每次转置、两个偏置都能不看书解释清楚；再打开 notebook 对照 `figures/15_02.png`（五种架构）与 `15_07.png`（隐藏递归 vs 输出递归）自查掌握程度。

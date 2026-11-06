# 字符级 RNN 文本生成

## 1. 本讲目标

学完本讲，你应该能够：

1. 理解**字符级语言模型**的直觉：把「预测下一个词」退化成「预测下一个字符」，监督信号完全免费。
2. 掌握 **many-to-many** 训练样本的构造方式：输入序列与目标序列错开一位，每个时间步都有自己的损失。
3. 理解长文本的**按片段截断**训练：为什么不能对 77 万字符一次反向传播，本 notebook 如何用「随机抽 200 字符片段 + 隐状态归零」来截断计算图。
4. 掌握**温度采样**生成文本的原理与实现：prime 预热、`exp(logits/T)`、`torch.multinomial` 抽样，以及温度高低对生成质量的影响。
5. 能把这套流程迁移到自己的语料上（综合实践）。

本讲只精读一个 notebook：[pytorch_ipynb/rnn/char_rnn-charlesdickens.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/char_rnn-charlesdickens.ipynb)。它在狄更斯小说《双城记》上训练一个单层 GRU，从乱码逐渐生成出「像拼错英文」的文本。

## 2. 前置知识

### 2.1 语言模型：链式分解

语言模型的目标是给序列分配概率。按概率的链式法则，一段文本的概率可以分解为逐个词元的条件概率：

\[ P(x_1, x_2, \dots, x_n) = \prod_{t=1}^{n} P(x_t \mid x_1, \dots, x_{t-1}) \]

- **词级模型**（u6-l1 的 IMDb 分类器属于其下游应用）以「词」为词元，词表动辄上万。
- **字符级模型**以「字符」为词元，词表只有约 100 项。好处是不需要分词、永远不会有「未登录词」；代价是同样一段话的序列长度大得多（一个英文单词平均 4~5 个字符）。

训练目标只有一个：给定前文，预测下一个字符。**训练数据就是语料本身，不需要任何人工标注**——这种「目标从数据中自动构造」的范式叫**自监督学习**。

### 2.2 many-to-one 与 many-to-many

| 对比项 | u6-l1 IMDb 分类（many-to-one） | 本讲文本生成（many-to-many） |
|---|---|---|
| 输入 | 一条评论文本 | 一段 200 字符的文本片段 |
| 输出 | 整条评论 1 个 logit | **每个时间步**各 100 类 logits |
| 损失 | 每条样本 1 个损失值 | 每个字符位置 1 个损失值，共 200 个 |
| 用到的隐状态 | 只用最后一步 | 每一步都用 |

### 2.3 自回归生成

生成文本的过程是**自回归**（autoregressive）的：模型输出下一个字符的概率分布 → 从中抽一个字符 → 把它拼回输入 → 再前进一步预测下下个字符……循环往复。训练时「教师强制」用真实字符作下一步输入，生成时没有真实答案，只能用自己刚生成的字符。

### 2.4 温度采样

模型每个时间步输出 logits \( z \in \mathbb{R}^{100} \)。**温度**（temperature）\( T > 0 \) 把 softmax 修改为：

\[ p_i = \frac{\exp(z_i / T)}{\sum_{j} \exp(z_j / T)} \]

- \( T \to 0 \)：分布退化为 argmax（永远选最大 logit 的字符），生成确定但容易死板重复。
- \( T = 1 \)：还原模型本来的分布。
- \( T \to \infty \)：分布趋于均匀，生成接近随机乱码。

温度是在「保守复述」与「冒险创新」之间调节的旋钮，4.3 节会看到 notebook 里它的具体实现。

## 3. 本讲源码地图

本讲全部源码集中在一个 notebook（注意：以下行号均为 `.ipynb` JSON 源文件中的行号，永久链接据此定位）：

| 文件 | 作用 |
|---|---|
| `pytorch_ipynb/rnn/char_rnn-charlesdickens.ipynb` | 字符级 RNN 文本生成：数据清洗 → 字符词表 → 随机切片采样 → Embedding+GRU+Linear 模型 → 按时间步训练 → 温度采样生成 |

notebook 内部结构（沿用 u1-l2 讲过的五段式）：

| 段落 | 内容 | 行号范围 |
|---|---|---|
| General Settings | 超参与随机种子 | L93-L104 |
| Dataset | 下载语料、清洗、字符转张量 | L199-L346 |
| Model | `RNN` 类（Embedding + GRU + Linear） | L422-L446 |
| Training | `evaluate` 采样函数与训练循环 | L485-L772 |

与 u6-l1 的关系：本讲复用 u6-l1 引入的 **Embedding 层**（整数编号 → 可学习稠密向量）与**隐状态**（hidden state）概念，但不再用 torchtext 的 BucketIterator 与 DataLoader——因为数据只是一条 77 万字符的长字符串，采样方式简化为「随机切片」。

## 4. 核心概念与源码讲解

### 4.1 字符词表：拿 `string.printable` 当免费词表

#### 4.1.1 概念说明

u6-l1 构建词级词表时，需要统计训练集词频、截断到 2 万、再补 `<unk>` 与 `<pad>` 两个特殊词元。字符级模型把这件事简化到极致：Python 标准库的 `string.printable` 恰好是 100 个可打印 ASCII 字符（数字 10 + 小写 26 + 大写 26 + 标点 32 + 空白 6），直接拿来当「免构建」词表，且**索引即位置**：`'0'` 是 0，`'a'` 是 10，`'A'` 是 36，空格是 94，`'\n'` 是 96。

好处是词表**闭集全覆盖**：任何英文文本都能编码，永远不需要 `<unk>`（任何没见过的单词都只是已见字符的新排列）。代价是语料必须先清洗成纯 ASCII——这就是 `unidecode` 与正则替换的作用。

#### 4.1.2 核心流程

```
原始文本 (776,058 字符, 含非 ASCII 字符与连续空格)
   │ unidecode.unidecode()   音译: 'é'→'e', '“'→'"' 等, 保证落在 100 字符集内
   │ re.sub(' +', ' ')       压缩连续空格
   ▼
干净文本 textfile
   │ string.printable.index(c)  逐字符查表
   ▼
长整型张量 (每个字符一个 0~99 的索引)
```

#### 4.1.3 源码精读

notebook 先把语料读入并清洗：

```python
with open('./98-0.txt', 'r') as f:
    textfile = f.read()

# convert special characters
textfile = unidecode.unidecode(textfile)

# strip extra whitespaces
textfile = re.sub(' +',' ', textfile)

TEXT_LENGTH = len(textfile)
```

这段代码读取语料文件，用 `unidecode` 把重音字符等音译为 ASCII，再压缩连续空格，最后统计总字符数（输出为 776,058）：
[pytorch_ipynb/rnn/char_rnn-charlesdickens.ipynb:L224-L233](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/char_rnn-charlesdickens.ipynb#L224-L233)

字符到索引的映射只有一个列表推导：

```python
def char_to_tensor(text):
    lst = [string.printable.index(c) for c in text]
    tensor = torch.tensor(lst).long()
    return tensor
```

`string.printable` 本身就是索引表，`.index(c)` 查位置，输出 long 型张量（Embedding 层要求整数输入，u6-l1 已见）。demo 输出 `char_to_tensor('abcDEF')` 得到 `tensor([10, 11, 12, 39, 40, 41])`：
[pytorch_ipynb/rnn/char_rnn-charlesdickens.ipynb:L314-L319](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/char_rnn-charlesdickens.ipynb#L314-L319)

词表本体就是这个内置常量（notebook 单独用一个 cell 展示它）：
[pytorch_ipynb/rnn/char_rnn-charlesdickens.ipynb:L199](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/char_rnn-charlesdickens.ipynb#L199)

一个值得注意的细节：如果语料里存在 `string.printable` 之外的字符（例如未经 unidecode 的中文或 emoji），`index(c)` 会直接抛 `ValueError`。`unidecode` 就是防线——这也是 4.1.2 流程图中它必须放在查表之前的原因。

#### 4.1.4 代码实践

1. **实践目标**：建立对 100 字符词表的直觉，亲手验证「索引即位置」。
2. **操作步骤**（示例代码，可在任意 Python 环境运行）：

```python
import string
print(len(string.printable))          # 100
print(string.printable.index('a'))    # 10
print(string.printable.index('A'))    # 36
print(string.printable.index(' '))    # 94
print(string.printable.index('\n'))   # 96
print([string.printable.index(c) for c in 'The'])
```

3. **需要观察的现象**：长度恰为 100；`'The'` 的编码是 `[55, 17, 14]`（T=36+19, h=10+7, e=10+4）。
4. **预期结果**：与 notebook 中 `draw_random_sample` 的输出对照——那里大量出现的 `94` 正是空格、`96` 正是换行符，可自行解码几个片段验证。
5. 待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么字符级语言模型不需要 `<unk>` 特殊词元，而 u6-l1 的词级模型必需？

**答案**：字符词表是闭集，100 个可打印 ASCII 字符覆盖一切（经 unidecode 清洗后的）英文文本；未见过的单词只是已见字符的新组合，模型仍能给出概率。词级模型词表有限，测试中必然遇到未收录词，必须有 `<unk>` 兜底。

**练习 2**：如果去掉 `unidecode.unidecode(textfile)` 这一步，而语料中恰好含字符 `é`，运行到 `char_to_tensor` 会发生什么？

**答案**：`string.printable.index('é')` 抛出 `ValueError: substring not found`——`é` 不在 100 字符集内。这正说明 unidecode 不是可有可无的美化，而是词表能覆盖语料的必要条件。

**练习 3**：`re.sub(' +',' ', textfile)` 去掉了什么噪声？去掉它模型还能训练吗？

**答案**：它把多个连续空格压成一个。不去掉也能训练（空格只是词表里一个普通字符），但连续空格属于低价值噪声，会稀释有效训练信号；这属于数据清洗的「顺手但非必需」步骤。

### 4.2 many-to-many 训练：错位切片与按时间步的训练循环

#### 4.2.1 概念说明

语言模型的训练信号由「错开一位」免费获得：对一段 \( x_1 x_2 \dots x_{n} \) 的文本，令输入为 \( x_1, \dots, x_{n-1} \)，目标为 \( x_2, \dots, x_{n} \)。每个时间步的任务都是「看过前 c 个字符，预测第 c+1 个」——这就是 **many-to-many**：每个时间步都有 logits、也都有损失。

第二件事是**截断**。整本小说 77 万字符，不可能（也没必要）一次反向传播贯穿全书——计算图深度即是 BPTT（沿时间反向传播）的梯度链长度。本 notebook 的做法是「片段级截断」：

- 每次迭代从全文中**随机抽取一段 201 字符**的片段；
- 隐状态**归零从头读**，在这 200 步内做一次**完整的** BPTT；
- 片段之间不传递隐状态，计算图随 `backward()` 释放。

（更严格的 truncated BPTT 做法是跨片段保留隐状态但 `detach()` 切断梯度；本 notebook 选择了更简单的「重置」变体，两种思路都值得知道。）

#### 4.2.2 核心流程

```
每次迭代（共 NUM_ITER=20000 次）:
    1. random_portion: 从全文随机抽 201 字符片段
    2. char_to_tensor: 编码为长整型张量 (201,)
    3. inputs  = text[:-1]   # 前 200 个字符
       targets = text[1:]    # 后 200 个字符（错开一位）
    4. hidden = 0
    5. for c in 0..199:                      # 逐时间步
           outputs, hidden = model(inputs[c], hidden)
           loss += cross_entropy(outputs, targets[c])
    6. loss /= 200
    7. loss.backward()   # 一次 BPTT 贯穿 200 步展开的计算图
    8. optimizer.step()
```

注意与 u2-l2 确立的五步范式 `forward → loss → zero_grad → backward → step` 的对应关系：`zero_grad` 被提到片段循环开头，`forward` 与 `loss` 重复 200 次（逐步累加），`backward` 与 `step` 仍各一次。「按步」体现在 forward 循环；「截断」体现在片段边界。

#### 4.2.3 源码精读

**随机切片**：从全文里抽一段固定长度的片段，起始位置随机：

```python
def random_portion(textfile):
    start_index = random.randint(0, TEXT_LENGTH - TEXT_PORTION_SIZE)
    end_index = start_index + TEXT_PORTION_SIZE + 1
    return textfile[start_index:end_index]
```

`TEXT_PORTION_SIZE=200`，多取 1 个字符是为了让下一步能错位切出 200 对（输入, 目标）：
[pytorch_ipynb/rnn/char_rnn-charlesdickens.ipynb:L272-L277](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/char_rnn-charlesdickens.ipynb#L272-L277)

**many-to-many 样本构造**——本讲最核心的三行代码：

```python
def draw_random_sample(textfile):    
    text_long = char_to_tensor(random_portion(textfile))
    inputs = text_long[:-1]
    targets = text_long[1:]
    return inputs, targets
```

`inputs` 与 `targets` 是同一条 201 长度张量的前 200 与后 200 个元素，恰好错开一位：`inputs[c]` 的目标就是 `targets[c] = inputs[c+1]`。这一步就是语言模型「预测下一字符」监督信号的完整定义：
[pytorch_ipynb/rnn/char_rnn-charlesdickens.ipynb:L342-L346](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/char_rnn-charlesdickens.ipynb#L342-L346)

**模型**：`Embedding → GRU → Linear` 三件套，`forward` 每次只处理**一个字符**：

```python
class RNN(torch.nn.Module):
    def __init__(self, input_size, embed_size,
                 hidden_size, output_size, num_layers):
        super(RNN, self).__init__()
        self.num_layers = num_layers
        self.hidden_size = hidden_size
        
        self.embed = torch.nn.Embedding(input_size, hidden_size)
        self.gru = torch.nn.GRU(input_size=embed_size,
                                hidden_size=hidden_size,
                                num_layers=num_layers)
        self.fc = torch.nn.Linear(hidden_size, output_size)
        ...
    def forward(self, features, hidden):
        embedded = self.embed(features.view(1, -1))
        output, hidden = self.gru(embedded.view(1, 1, -1), hidden)
        output = self.fc(output.view(1, -1))
        return output, hidden

    def init_zero_state(self):
        init_hidden = torch.zeros(self.num_layers, 1, self.hidden_size).to(DEVICE)
        return init_hidden
```

完整定义见 [pytorch_ipynb/rnn/char_rnn-charlesdickens.ipynb:L422-L446](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/char_rnn-charlesdickens.ipynb#L422-L446)。

逐行拆解：

- `forward` 接收单个字符索引 `features` 与上一步的 `hidden`；`features.view(1, -1)` 变成 `[1, 1]`（时间步=1、batch=1），GRU 一步返回新的 `hidden`，`fc` 把隐状态映射为 100 类 logits。**「循环」其实发生在外层 Python 的 for 循环里**，`hidden` 由调用方持有并在时间步之间传递。
- `init_zero_state` 每次新建全零隐状态张量并放到 DEVICE 上，供每个片段开始时使用（[L444-L446](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/char_rnn-charlesdickens.ipynb#L444-L446)）。

精读时还应发现两处「怪代码」（读本仓库一贯的原则：以代码与变量来源为准，见 u1-l2、u3-l5）：

1. **`self.embed` 用错了维度参数**：`torch.nn.Embedding(input_size, hidden_size)` 的输出维度写的是 `hidden_size`，而 GRU 的输入维度却按 `embed_size` 声明（[L430-L433](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/char_rnn-charlesdickens.ipynb#L430-L433)）。实例化时传入的 `EMBEDDING_DIM` 与 `HIDDEN_DIM` **恰好都是 100**（见下方实例化代码），两者碰巧相等所以能跑通；若把两个超参改成不同值，`gru` 会报维度不匹配错误。
2. **`self.init_hidden` 是死代码**：`__init__` 里定义了一个可学习的全零初始隐状态参数（[L435-L436](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/char_rnn-charlesdickens.ipynb#L435-L436)），但训练与生成**都没有使用它**——真正使用的是 `init_zero_state()` 每次新建的普通零张量。

模型实例化（输入输出大小都是词表长度 100）：

```python
model = RNN(len(string.printable), EMBEDDING_DIM, HIDDEN_DIM, len(string.printable), NUM_HIDDEN)
```

[pytorch_ipynb/rnn/char_rnn-charlesdickens.ipynb:L459-L462](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/char_rnn-charlesdickens.ipynb#L459-L462)。按形状推算参数量约 8 万（Embedding 10,000 + GRU 60,600 + Linear 10,100），比 u6-l1 的词级模型小一个数量级——字符词表只有 100 项是主要原因。

**训练循环**——本讲的第二个核心：

```python
for iteration in range(NUM_ITER):
    hidden = model.init_zero_state()
    optimizer.zero_grad()
    
    loss = 0.
    inputs, targets = draw_random_sample(textfile)
    inputs, targets = inputs.to(DEVICE), targets.to(DEVICE)
    for c in range(TEXT_PORTION_SIZE):
        outputs, hidden = model(inputs[c], hidden)
        loss += F.cross_entropy(outputs, targets[c].view(1))

    loss /= TEXT_PORTION_SIZE
    loss.backward()
    optimizer.step()
```

节选自 [pytorch_ipynb/rnn/char_rnn-charlesdickens.ipynb:L745-L764](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/char_rnn-charlesdickens.ipynb#L745-L764)，要点：

- 每个 iteration：新片段 → 隐状态归零 → 梯度清零 → 200 步逐步 forward 并累加损失 → 除以 200 → **一次** `backward`（BPTT 贯穿 200 步展开的计算图）→ **一次** `step`。
- 200 步的梯度链深达 200 层 GRU 展开，门控机制（u6-l2 讲过）在这里发挥缓解梯度消失的作用。
- 日志部分每 1000 次迭代打印损失并现场生成 200 个字符（`evaluate(model, 'Th', 200)`）：
  [pytorch_ipynb/rnn/char_rnn-charlesdickens.ipynb:L767-L771](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/char_rnn-charlesdickens.ipynb#L767-L771)。

超参在 Settings 段定义：`NUM_ITER=20000`（迭代次数，注意本 notebook 没有 epoch 概念，一个 iteration 消耗一个 200 字符片段）、`LEARNING_RATE=0.005`、`EMBEDDING_DIM=100`、`HIDDEN_DIM=100`、`NUM_HIDDEN=1`：
[pytorch_ipynb/rnn/char_rnn-charlesdickens.ipynb:L93-L104](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/char_rnn-charlesdickens.ipynb#L93-L104)

#### 4.2.4 代码实践

1. **实践目标**：亲眼验证「错位一位」的监督构造与损失初值的理论预测。
2. **操作步骤**（源码阅读 + 小实验，基于 notebook 已有代码）：
   - 在 notebook 中找到 `draw_random_sample` 的调用与输出（[L348 起](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/char_rnn-charlesdickens.ipynb#L348)），检查 `inputs` 与 `targets` 两个张量：长度是否都是 200？`targets[c]` 是否恒等于 `inputs[c+1]`？
   - 用 `string.printable[94]` 与 `string.printable[96]` 解码输出中高频的 94 和 96，确认它们是空格与换行。
   - 观察训练日志第 0 次迭代的损失 `4.58`（notebook 保存的输出），与 \( \ln 100 \approx 4.605 \) 对比。
3. **需要观察的现象**：`inputs`/`targets` 逐位错开；94/96 解码为空白字符。
4. **预期结果**：三个观察全部吻合——第 0 次迭代损失非常接近「100 类均匀分布的交叉熵」，因为随机初始化的模型输出的各字符概率几乎相等（对照 u2-l2 中 softmax 回归首损失 \( \ln 10 \approx 2.303 \)，本讲是它的字符级版本）。
5. 待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`draw_random_sample` 返回的 `inputs` 和 `targets` 长度各是多少？为什么 `random_portion` 要取 `TEXT_PORTION_SIZE + 1` 个字符？

**答案**：都是 200。片段共 201 个字符：`[:-1]` 取前 200 个作输入，`[1:]` 取后 200 个作目标，二者错开一位，恰好构成 200 对「当前字符 → 下一字符」的监督样本。少取 1 个就只能切出 199 对。

**练习 2**：如果把 `loss /= TEXT_PORTION_SIZE` 这一行删掉，训练会发生什么？

**答案**：损失和梯度都被放大 200 倍。本 notebook 用 Adam，其对梯度尺度不敏感（更新量被自适应归一），影响相对温和；但若换成 SGD，等效学习率放大 200 倍，训练会立即发散。即便用 Adam，依赖这种「隐式缩放碰巧被优化器抵消」的写法也是坏习惯——平均损失让数值与「每字符损失」有明确对应。

**练习 3**：为什么每个 iteration 开始要把 `hidden` 重新置零，而不是接着上一个片段末尾的隐状态继续？

**答案**：两个原因。其一是语义：片段是从全文**随机**抽取的，相邻两个迭代的内容在原文中几乎必然不相邻，延续隐状态等于给模型灌入错误的「上文」。其二是机制：隐状态若跨迭代携带，计算图也要跨迭代保留（或显式 detach），显存与实现都更复杂。经典的 truncated BPTT 处理连续长流时会选择「保留隐状态 + detach 切断梯度」，本 notebook 数据是随机片段，直接归零是最干净的方案。

### 4.3 采样生成：prime 预热、温度与多项式抽样

#### 4.3.1 概念说明

训练完成后，模型可以生成文本：从一段给定的开头（prime string，如 `'Th'`）出发，反复「预测分布 → 抽样 → 把抽到的字符作为下一步输入」。两个关键设计：

1. **prime 预热**：模型是以隐状态为记忆的，冷启动时隐状态全零、分布平淡。所以先把 prime 字符逐个喂入模型（只为了积累 `hidden`），再从 prime 的最后一个字符开始正式生成。
2. **抽样而非贪心**：每个时间步从模型的输出分布中**随机抽样**一个字符，而不是总取 argmax。贪心生成是确定性的，长序列极易陷入重复循环（如无限重复同一个短语）；抽样带来多样性，而**温度**控制抽样分布的尖锐程度（见 2.4 节公式）。

#### 4.3.2 核心流程

```
evaluate(model, prime_str='Th', predict_len=100, temperature=0.8):
    hidden = 0
    把 prime_str 除最后一个字符外逐个喂入模型, 只为积累 hidden   # 预热
    inp = prime_str 最后一个字符
    重复 predict_len 次:
        output, hidden = model(inp, hidden)          # output 是 100 维 logits
        w = exp(output / temperature)                 # 温度缩放(省略了 softmax)
        top_i = multinomial(w, 1)                     # 按权重抽 1 个字符
        把 printable[top_i] 拼到输出, 并作为下一轮 inp
```

#### 4.3.3 源码精读

`evaluate` 函数完整实现了上述流程：

```python
def evaluate(model, prime_str='A', predict_len=100, temperature=0.8):
    hidden = model.init_zero_state()
    prime_input = char_to_tensor(prime_str)
    predicted = prime_str

    # Use priming string to "build up" hidden state
    for p in range(len(prime_str) - 1):
        _, hidden = model(prime_input[p].to(DEVICE), hidden.to(DEVICE))
    inp = prime_input[-1]
    
    for p in range(predict_len):
        output, hidden = model(inp.to(DEVICE), hidden.to(DEVICE))
        
        # Sample from the network as a multinomial distribution
        output_dist = output.data.view(-1).div(temperature).exp()
        top_i = torch.multinomial(output_dist, 1)[0]
        
        # Add predicted character to string and use as next input
        predicted_char = string.printable[top_i]
        predicted += predicted_char
        inp = char_to_tensor(predicted_char)

    return predicted
```

[pytorch_ipynb/rnn/char_rnn-charlesdickens.ipynb:L485-L510](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/char_rnn-charlesdickens.ipynb#L485-L510)。逐点拆解：

- **预热循环**（[L494-L496](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/char_rnn-charlesdickens.ipynb#L494-L496)）：对 `'Th'` 只把 `'T'` 喂入模型（`len(prime_str) - 1 = 1` 次），丢弃输出、只留 `hidden`；`'h'` 留作正式生成的第一个输入。prime 的每个字符就这样「垫」进了隐状态。
- **温度缩放**（[L502](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/char_rnn-charlesdickens.ipynb#L502)）：`output.data.view(-1).div(temperature).exp()` 即 \( w_i = \exp(z_i / T) \)。注意这里**没有做 softmax**——softmax 的分母对所有候选是同一个常数，而下一步 `torch.multinomial` 只按**相对权重**抽样，常数直接约掉，所以省略 softmax 在数学上完全等价。`output.data` 是 PyTorch 1.x 时代「脱离计算图取值」的旧写法，现代等价物是 `output.detach()`；采样不需要梯度。
- **抽样与自回归反馈**（[L503-L508](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/char_rnn-charlesdickens.ipynb#L503-L508)）：`torch.multinomial(output_dist, 1)` 按权重抽一个索引（不是 argmax），翻译回字符拼进结果，再编码回张量作为下一轮输入——这就是自回归闭环。

温度对分布的影响（对照 2.4 节公式）：\( T < 1 \) 拉大 logits 差距、分布更尖，生成保守、爱重复高频搭配；\( T > 1 \) 压平分布，低概率字符（标点、数字、生僻大写）更多登场，接近乱码。notebook 默认 `temperature=0.8`，是略偏保守的常用值。

训练日志里保存的生成质量演变很直观：第 0 次迭代输出形如 `Th4izvh?=lw2ZaCV...` 的乱码（接近均匀抽样）；1000 次后出现 `Th Prost / into / he forn a wock...` 的词根；到 1 万次左右单词大多成型（`Ther his moth wooten a new blood...`），只是拼写仍频繁出错。日志见 [pytorch_ipynb/rnn/char_rnn-charlesdickens.ipynb:L766-L772](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/char_rnn-charlesdickens.ipynb#L766-L772) 之后的保存输出。

#### 4.3.4 代码实践

1. **实践目标**：亲手感受温度对生成文本的影响，建立「保守—冒险」谱系的直觉。
2. **操作步骤**（示例代码，前提是已按 notebook 跑完训练、内存中有训练好的 `model`）：

```python
for T in [0.2, 0.5, 0.8, 1.0, 1.5]:
    print(f'===== temperature = {T} =====')
    print(evaluate(model, prime_str='Th', predict_len=200, temperature=T))
    print()
```

3. **需要观察的现象**：
   - T=0.2：大量重复常见词与短语，甚至整句循环；
   - T=0.8 左右：单词基本成形、偶有拼错，最「像英文」；
   - T=1.5：乱码与怪异标点比例明显上升。
4. **预期结果**：随温度升高，生成从「重复背诵」单调滑向「随机噪声」，中间某处（通常 0.5~1.0）连贯度最佳；具体最佳值依赖语料与训练程度。
5. 待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：分别描述 \( T \to 0 \) 与 \( T \to \infty \) 时生成行为的极限。

**答案**：\( T \to 0 \) 时 \( \exp(z_i/T) \) 中最大 logit 项压倒其余，抽样退化为 argmax 贪心——生成完全确定（给定 prime 与权重），但极易陷入重复循环；\( T \to \infty \) 时所有 \( z_i/T \to 0 \)，分布趋于均匀，生成等价于从 100 字符里随机抽，接近乱码。

**练习 2**：`evaluate` 里没有 softmax，只有 `exp(z/T)`，为什么 `torch.multinomial` 的抽样结果仍然正确？

**答案**：softmax 为 \( p_i = \exp(z_i/T) / Z \)，分母 \( Z = \sum_j \exp(z_j/T) \) 与候选 \( i \) 无关。`multinomial` 按权重 \( w_i \) 抽样的概率是 \( w_i / \sum_j w_j \)，常数 \( Z \) 在分子分母中约掉，与 softmax 后抽样严格同分布。省略它是合法的化简，不是 bug。

**练习 3**：把 `torch.multinomial(output_dist, 1)[0]` 换成 `output.argmax()`，生成会变成什么样？这与哪个温度值等价？

**答案**：变成确定性贪心生成——给定 prime 字符串后每次输出完全可复现，多样性归零，长文本容易无限重复同一片段。行为上等价于 \( T \to 0 \) 的极限。

## 5. 综合实践

**任务**：换一份纯文本语料训练你自己的 char-RNN，并做温度对比实验（本讲的 `practice_task`）。

### 5.1 准备

1. 复制 `char_rnn-charlesdickens.ipynb` 为自己的工作副本（例如 `char_rnn-own-corpus.ipynb`），**不要修改仓库源文件**。
2. 环境提示（沿用 u1-l2 的经验：notebook 保存的环境是 Python 3.7 + torch 1.0.1，新版环境需微调）：
   - 依赖 `unidecode` 包：`pip install Unidecode`；
   - 开头的 `from torchtext import data` / `from torchtext import datasets` 两行在本 notebook 中**实际未被使用**（数据加载全靠 `random_portion`），新版 torchtext 已移除这些接口会直接 ImportError，删掉这两行即可；
   - notebook 以相对路径 `./98-0.txt` 读语料，须在自己的工作目录里放置语料文件。

### 5.2 步骤

1. **选语料**：从 Project Gutenberg 下载任一英文纯文本（体积建议 ≥ 300 KB，太小的语料模型很快「背书」）；或用本仓库的纯文本文件拼一份「项目文风」语料（示例代码——README.md 加上 pytorch_ipynb 下的 6 个 helper 源文件，注意不要用 .ipynb，它是 JSON 不是纯文本）：

```python
# 示例代码: 把仓库多份纯文本文件拼成一份语料
paths = ['README.md', 'pytorch_ipynb/helper_data.py', 'pytorch_ipynb/helper_train.py',
         'pytorch_ipynb/helper_evaluate.py', 'pytorch_ipynb/helper_plotting.py',
         'pytorch_ipynb/helper_utils.py', 'pytorch_ipynb/helper.py']
with open('corpus.txt', 'w') as out:
    for p in paths:
        out.write(open(p).read() + '\n')
```

   注意：这样拼出的语料只有约 100 KB 量级，作为字符级语料偏小，生成的「项目味」会以死记硬背为主——这本身就是值得记录的观察。
2. **替换数据源**：把 `with open('./98-0.txt', 'r') as f:` 改为自己的语料路径；**保留** `unidecode` 与 `re.sub` 两行清洗（4.1 节讲过它们是词表覆盖的必要条件）。
3. **快速试跑**：`NUM_ITER` 从 20000 先降到 5000（原配置在作者环境下约 50 分钟），观察损失是否从 \( \approx \ln 100 \) 下降。
4. **温度对比**：训练结束后运行 4.3.4 的温度扫描代码，对 T ∈ {0.2, 0.5, 0.8, 1.0, 1.5} 各生成 3 段 200 字符文本。
5. **记录**：填一张表——每个温度下「单词拼对率（主观）」「重复程度」「乱码率」，并回答：你的语料上最佳温度是多少？与狄更斯语料上的直觉一致吗？

### 5.3 观察要点

- 损失初值是否接近 \( \ln 100 \approx 4.605 \)；
- 小语料 vs 大语料在相同迭代数下生成质量的差异；
- 温度谱系上「重复背诵 ↔ 随机乱码」的过渡是否如 4.3 节预测。

以上实验结果**待本地验证**。

## 6. 本讲小结

- **字符词表**：`string.printable` 恰好 100 个可打印 ASCII 字符，索引即位置；`unidecode` + 空格压缩保证语料能被闭集词表覆盖，字符级模型因此永不需要 `<unk>`。
- **many-to-many 训练**：`inputs = text[:-1]`、`targets = text[1:]` 错开一位，免费获得「预测下一字符」的自监督信号；200 个时间步各自产生损失，累加平均后一次 `backward` 完成 200 步 BPTT。
- **片段级截断**：每次迭代随机抽 201 字符、隐状态归零，是「长文本没法整段反向传播」的最简解法；与跨片段保留 hidden 的严格 truncated BPTT 是两种变体。
- **模型形态**：`Embedding → GRU → Linear`，`forward` 每次只吃一个字符，「循环」在外层 Python 循环中显式展开；精读还发现 embed 维度参数误用 `hidden_size`、`init_hidden` 死代码两处源码怪点。
- **采样生成**：prime 预热积累隐状态，`exp(logits/T)` + `torch.multinomial` 实现温度采样（省略 softmax 数学等价）；温度在保守复述与随机乱码之间调节生成谱系。
- 损失初值 \( \approx \ln 100 \) 是「均匀猜测」的理论指纹，与 u2-l2 的 \( \ln 10 \) 一脉相承。

## 7. 下一步学习建议

- **下一讲 u6-l5（Text CNN）**：换一种完全不同的视角处理文本——用多尺度 1D 卷积 + 全局最大池化提取 n-gram 特征做 IMDb 分类，序列建模并非 RNN 一家之言。
- **u6-l6（DistilBERT）**：把本讲「自回归链式分解」的朴素语言模型升级为现代预训练 Transformer，看特征提取与端到端微调两条路线。
- **源码延伸阅读**：对照 `pytorch_ipynb/rnn/rnn_simple_imdb.ipynb`（u6-l1）重看 many-to-one 与 many-to-many 的差异；有兴趣可自行把本讲 GRU 换成 LSTM（u6-l2 讲过接口差异：返回 `(hidden, cell)`），或实现跨片段保留 hidden 的严格 truncated BPTT 并对比训练效果。

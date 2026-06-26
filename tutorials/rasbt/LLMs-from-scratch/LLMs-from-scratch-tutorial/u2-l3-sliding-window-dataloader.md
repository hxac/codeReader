# 滑动窗口数据采样与 DataLoader

## 1. 本讲目标

学完本讲，你应该能够：

- 说清「下一个 token 预测」这个语言模型训练目标，以及为什么它要求我们把 target 设成 input 右移一位。
- 读懂并自己实现 `GPTDatasetV1`，理解它如何用滑动窗口把一整段长文本切成成对的 `input/target` 训练样本。
- 读懂并使用 `create_dataloader_v1`，掌握 `batch_size`、`max_length`、`stride`、`drop_last` 四个参数各自的作用与相互影响。
- 理解 `stride` 在「样本数量」与「过拟合风险」之间的取舍，知道为什么训练时常常令 `stride` 等于上下文长度以避免批次重叠。

本讲是文本数据处理流水线（第 2 章）的收尾环节。前两讲（`u2-l1`、`u2-l2`）已经把一段文本变成了 token ID 序列，本讲负责把这些 ID「切成模型能一批一批吃下去的训练样本」，产出的 `dataloader` 会直接喂给后续的嵌入层与 GPT 模型。

## 2. 前置知识

本讲默认你已经掌握 `u2-l1`（分词与词表）和 `u2-l2`（BPE 与 tiktoken）的内容，也就是：你知道一段文本如何被编码成一串整数 token ID。除此之外，还需要几个最基础的 PyTorch 概念，下面用一句话分别解释：

- **张量（tensor）**：PyTorch 里的多维数组，可以理解成「能放进 GPU、能自动求导的 numpy 数组」。一段 token ID 序列通常被包成一维 `torch.tensor`。
- **`Dataset`**：PyTorch 提供的数据集抽象基类（`torch.utils.data.Dataset`）。你只要继承它、实现三个方法（`__init__`、`__len__`、`__getitem__`），就定义了「这个数据集一共有多少条样本、第 `i` 条样本是什么」。
- **`DataLoader`**：PyTorch 提供的迭代器（`torch.utils.data.DataLoader`）。你把一个 `Dataset` 交给它，它就负责自动**分批（batch）**、**打乱（shuffle）**、可选地**多进程加载（num_workers）**，训练时 `for batch in dataloader:` 就能一批批取数据。
- **下一个 token 预测（next-token prediction）**：GPT 这类自回归语言模型的训练目标——给定前面若干个 token，预测紧跟着的下一个 token。本讲所有设计都围绕这个目标展开。

> 关键直觉：模型不是「读完整本书再考试」，而是「每读到一个位置，都猜下一个词」。所以训练样本必须成对出现：一段上下文（input）+ 它紧接的下一个词（target）。

## 3. 本讲源码地图

本讲涉及三个文件，它们实现的是同一套数据加载逻辑，但出现在不同位置、细节略有差异，正好可以对照阅读：

| 文件 | 作用 | 行号是否可引用 |
| --- | --- | --- |
| [ch02/01_main-chapter-code/ch02.ipynb](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch02/01_main-chapter-code/ch02.ipynb)（§2.6 *Data sampling with a sliding window*） | 正文逐行演进的入口，包含直觉讲解、小例子与各种参数下的输出演示 | notebook 按单元格组织，下面用章节小节定位 |
| [ch02/01_main-chapter-code/dataloader.ipynb](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch02/01_main-chapter-code/dataloader.ipynb) | 本章数据流水线的「精简汇总版」notebook，去掉了中间试错步骤，只保留主线代码 | 同上 |
| [ch04/01_main-chapter-code/previous_chapters.py](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/previous_chapters.py) | 第 4 章的「前序章节成品汇总器」，把 `GPTDatasetV1`/`create_dataloader_v1` 作为可被后续章节 `import` 复用的稳定函数收录进来 | 可以精确引用行号 |

> 复习 `u1-l3`：`previous_chapters.py` 是「精选成品汇总器」，只收录后续章节会复用的稳定代码。本讲的 `GPTDatasetV1` 和 `create_dataloader_v1` 正是被它收录、从而在第 4 章组装 GPT 模型时能直接 `from previous_chapters import ...` 复用。所以本讲以 `previous_chapters.py` 的行号为准做源码精读，notebook 作为讲解与演示的补充。

## 4. 核心概念与源码讲解

### 4.1 滑动窗口与下一个 token 预测

#### 4.1.1 概念说明

语言模型的训练目标是**下一个 token 预测**：给定一段上下文，预测紧接着的下一个 token。这意味着同一段长文本里，每一个位置都能贡献一个「(前文, 下一个词)」的训练信号。

比如有序列 `[A, B, C, D, E]`，模型要学的预测关系是：

```
[A]        -> B
[A, B]     -> C
[A, B, C]  -> D
[A, B, C, D] -> E
```

如果一次性把这条序列喂给模型，那么**input 取 `[A, B, C, D]`，target 就取它右移一位的 `[B, C, D, E]`**——这样一个 batch 里同时包含了上面 4 个预测关系（input 的第 0 位预测 target 的第 0 位 B，input 的第 1 位预测 target 的第 1 位 C，依此类推）。这正是「target 是 input 右移一位」的由来。

「滑动窗口」就是沿着 token 序列**以固定步长向前滑动**，每滑到一个位置就切下长度为 `max_length` 的一段作为 input、对应右移一位的一段作为 target，从而把一整本书变成大量这样的训练对。

#### 4.1.2 核心流程

设整段文本编码后得到 token 序列 \( T = [t_0, t_1, \dots, t_{N-1}] \)，上下文长度为 \( L \)（即 `max_length`），滑动步长为 \( S \)（即 `stride`）。对每个起点 \( i = 0, S, 2S, \dots \)（只要 \( i + L \le N \)）：

\[
\text{input}_i = T[i\,:\,i+L], \qquad \text{target}_i = T[i+1\,:\,i+L+1]
\]

注意 `target` 的起点是 \( i+1 \)、终点是 \( i+L+1 \)，正好比 `input` 整体右移一位。由此能生成的样本数为（即 Python `range(0, N-L, S)` 的元素个数）：

\[
\text{样本数} = \left\lfloor \frac{N - L - 1}{S} \right\rfloor + 1 \quad (N > L)
\]

直观结论：\( S \) 越小（最小为 1），样本越多、相邻样本重叠越大；\( S \) 越大（最大取 \( L \)，即不重叠），样本越少但彼此独立。

用最小例子演示（取 `context_size = 4`，先用 `stride=1` 的思想手动切一刀）：

```
x = enc_sample[0:4]      # [290, 4920, 2241, 287]
y = enc_sample[1:5]      # [4920, 2241, 287, 257]   ← 整体右移一位
```

#### 4.1.3 源码精读

正文 notebook 先用一个最小例子建立直觉。它先把整本书编码、取第 50 个 token 之后的一段，再用 `context_size = 4` 切出 `x` 和 `y`，参见 [ch02.ipynb §2.6](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch02/01_main-chapter-code/ch02.ipynb) 中如下片段：

```python
enc_sample = enc_text[50:]

context_size = 4
x = enc_sample[:context_size]
y = enc_sample[1:context_size+1]
```

运行后得到 `x: [290, 4920, 2241, 287]`、`y: [4920, 2241, 287, 257]`。notebook 紧接着用一个循环把它解码成可读词，让「右移一位」一目了然：

```python
for i in range(1, context_size+1):
    context = enc_sample[:i]
    desired = enc_sample[i]
    print(tokenizer.decode(context), "---->", tokenizer.decode([desired]))
```

输出（同样来自 §2.6）：

```
 and ---->  established
 and established ---->  himself
 and established himself ---->  in
 and established himself in ---->  a
```

这段循环演示的就是「每多看一个 token，就多猜一个下一个词」，把语言模型的训练目标可视化了出来。

#### 4.1.4 代码实践

**实践目标**：亲手验证「target 是 input 右移一位」。

**操作步骤**：

1. 进入 `ch02/01_main-chapter-code/` 目录，确保 `the-verdict.txt` 存在（仓库自带）。
2. 在一个 notebook 单元或 `.py` 脚本里运行下面这段**示例代码**（逻辑直接对应上一节的 `x`/`y`）：

```python
import tiktoken
tokenizer = tiktoken.get_encoding("gpt2")

with open("the-verdict.txt", "r", encoding="utf-8") as f:
    raw_text = f.read()

enc_text = tokenizer.encode(raw_text)
enc_sample = enc_text[50:]          # 跳过前 50 个 token，避开开头

context_size = 4
x = enc_sample[:context_size]
y = enc_sample[1:context_size + 1]

print("x:", x)
print("y:", y)
print("解码 x:", tokenizer.decode(x))
print("解码 y:", tokenizer.decode(y))
```

**需要观察的现象**：`y` 的前 3 个元素应该正好是 `x` 的后 3 个元素；解码后 `y` 的文本应当是 `x` 文本「去掉第一个词、并在末尾多一个词」。

**预期结果**：

```
x: [290, 4920, 2241, 287]
y: [4920, 2241, 287, 257]
```

这与 notebook §2.6 的输出完全一致，可以直接对照确认。如果 `x[1:] == y[:-1]` 成立，就验证了「右移一位」关系。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `enc_sample[1:context_size+1]` 误写成 `enc_sample[1:context_size]`，`y` 会变成什么？模型还能学到「下一个 token」吗？

**参考答案**：`y` 会少一个元素（长度变成 3），导致 `x` 和 `y` 长度不匹配，无法一一对应计算损失。即使强行对齐，也丢掉了「input 最后一位预测 genuinely 新 token」这一步，训练信号不完整。

**练习 2**：`context_size`（即后面的 `max_length`）越大，单条样本包含的预测关系越多。这是否意味着 `max_length` 越大越好？

**参考答案**：不一定。`max_length` 受模型支持的最大上下文长度限制（GPT-2 为 1024），也受显存/算力约束；此外过长的窗口会让单个样本变重、批量变小。它是一个需要权衡的超参数，不是越大越好。

---

### 4.2 GPTDatasetV1：把滑动窗口封装成 PyTorch Dataset

#### 4.2.1 概念说明

上一节我们手动切了一刀。真实训练时，需要对**整本书**切出**所有**滑动窗口样本，并交给 `DataLoader` 去分批。PyTorch 的约定是：先把「数据集长什么样」封装成一个 `Dataset` 类，再把它交给 `DataLoader`。

`GPTDatasetV1` 就是这个 `Dataset`。它的职责很纯粹：

- 在 `__init__` 里一次性把整段文本切成所有 `input/target` 对，分别存进两个列表；
- `__len__` 返回样本总数；
- `__getitem__(idx)` 返回第 `idx` 条 `(input, target)` 样本。

把切样本的工作放在 `__init__`（而不是每次 `__getitem__` 现切）是因为 token 化与切片只需做一次，提前算好能加速训练循环里的数据读取。

#### 4.2.2 核心流程

`GPTDatasetV1.__init__` 的执行流程：

1. 用传入的 `tokenizer` 把整段 `txt` 编码成一串 `token_ids`；
2. 沿序列以步长 `stride` 滑动窗口，每个起点 `i` 切出 `input_chunk = token_ids[i:i+max_length]` 与 `target_chunk = token_ids[i+1:i+max_length+1]`；
3. 把每一段分别转成 `torch.tensor`，追加到 `self.input_ids` 和 `self.target_ids` 两个列表；
4. 滑动结束后，这两个列表等长、一一对应，构成全部训练样本。

伪代码：

```
token_ids = tokenizer.encode(txt)
for i in range(0, len(token_ids) - max_length, stride):
    input  = token_ids[i     : i+max_length]      # 左闭右开
    target = token_ids[i+1   : i+max_length+1]    # 整体右移一位
    self.input_ids .append(tensor(input))
    self.target_ids.append(tensor(target))
```

注意循环上界是 `len(token_ids) - max_length`（不是 `+1`）。因为 `range` 的上界是「开」区间，且 `target` 还要多取一位，这个上界正好保证最后一条 `target_chunk` 不会越界。

#### 4.2.3 源码精读

`GPTDatasetV1` 的权威实现收录在第 4 章的汇总器里：[previous_chapters.py:12-31](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/previous_chapters.py#L12-L31)。

类骨架与两个列表的初始化（[previous_chapters.py:12-18](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/previous_chapters.py#L12-L18)）：

```python
class GPTDatasetV1(Dataset):
    def __init__(self, txt, tokenizer, max_length, stride):
        self.input_ids = []
        self.target_ids = []
        token_ids = tokenizer.encode(txt, allowed_special={"<|endoftext|>"})
```

这里 `allowed_special={"<|endoftext|>"}` 复习 `u2-l2`：tiktoken 默认把 `<|endoftext|>` 当作「禁止随意编码」的特殊 token，必须显式放行才能编进序列。

滑动窗口的核心循环（[previous_chapters.py:21-25](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/previous_chapters.py#L21-L25)），正是上一节公式的直接翻译：

```python
for i in range(0, len(token_ids) - max_length, stride):
    input_chunk = token_ids[i : i + max_length]
    target_chunk = token_ids[i + 1 : i + max_length + 1]
    self.input_ids.append(torch.tensor(input_chunk))
    self.target_ids.append(torch.tensor(target_chunk))
```

最后是 `Dataset` 必须实现的两方法（[previous_chapters.py:27-31](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/previous_chapters.py#L27-L31)）：

```python
def __len__(self):
    return len(self.input_ids)

def __getitem__(self, idx):
    return self.input_ids[idx], self.target_ids[idx]
```

> **三处文件的小差异（值得留意）**：正文 [ch02.ipynb §2.6](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch02/01_main-chapter-code/ch02.ipynb) 里的 `GPTDatasetV1.__init__` 多了一行断言 `assert len(token_ids) > max_length, ...`，用来防止文本太短切不出样本；而 `previous_chapters.py` 作为「成品汇总」省去了这行教学性断言。两者切片逻辑完全一致。

#### 4.2.4 代码实践

**实践目标**：直接构造 `GPTDatasetV1`，观察它切出了多少条样本、第一条长什么样。

**操作步骤**：在 `ch02/01_main-chapter-code/` 目录运行下面这段**示例代码**（假定你已把 `GPTDatasetV1` 的定义从 `previous_chapters.py` 复制进来，或处于能 `from previous_chapters import GPTDatasetV1` 的第 4 章目录）：

```python
import tiktoken
# from previous_chapters import GPTDatasetV1   # 二选一

with open("the-verdict.txt", "r", encoding="utf-8") as f:
    raw_text = f.read()

tokenizer = tiktoken.get_encoding("gpt2")
dataset = GPTDatasetV1(raw_text, tokenizer, max_length=4, stride=1)

print("样本总数:", len(dataset))
print("第 0 条 input :", dataset[0][0].tolist())
print("第 0 条 target:", dataset[0][1].tolist())
print("第 1 条 input :", dataset[1][0].tolist())
```

**需要观察的现象**：`len(dataset)` 应该是一个很大的数（`the-verdict.txt` 经 BPE 编码共 5145 个 token，`max_length=4, stride=1` 时约为 5141 条）；第 1 条的 input 应当等于第 0 条的 target（因为 `stride=1`，窗口只右移一位）。

**预期结果**：第 0 条 input 为 `[40, 367, 2885, 1464]`、target 为 `[367, 2885, 1464, 1807]`，第 1 条 input 为 `[367, 2885, 1464, 1807]`（可与 4.3 节 notebook 输出相互印证）。样本总数的具体数值**待本地验证**（取决于你用的 `the-verdict.txt` 是否与仓库版本一致）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `__getitem__` 里返回的是已经存好的 `self.input_ids[idx]`，而不是每次现算 `token_ids[idx:idx+max_length]`？

**参考答案**：为了把「编码 + 切片」这个一次性开销放在 `__init__` 里做完，`__getitem__` 只做 O(1) 的列表取值。训练时 `__getitem__` 会被 `DataLoader` 高频调用，现算会拖慢数据加载。

**练习 2**：如果把 `stride` 设成 `1`、`max_length` 设成 `4`，而文本只有 5145 个 token，样本数大约是多少？如果把 `stride` 改成 `4` 呢？

**参考答案**：套用公式 \( \lfloor(N-L-1)/S\rfloor+1 \)，\( N=5145, L=4 \)。`stride=1` 时约 \( 5141 \) 条；`stride=4` 时约 \( \lfloor 5140/4\rfloor+1 = 1286 \) 条。可见 `stride` 从 1 增到 4，样本数大约缩减为原来的 1/4，但样本间不再重叠。

---

### 4.3 create_dataloader_v1：批处理与四个关键参数

#### 4.3.1 概念说明

有了 `Dataset`，还差一步：把它变成训练循环里 `for batch in dataloader:` 能直接迭代的东西，并自动完成分批、打乱。这就是 `create_dataloader_v1` 的职责——它是一个**工厂函数**：接收原始文本和几个超参，内部建好 `GPTDatasetV1`，再用 `torch.utils.data.DataLoader` 包一层返回。

这个函数同时把**分词器**也藏进了内部（写死用 GPT-2 的 BPE 编码），所以调用者只需提供原始文本和四个关键参数：

| 参数 | 含义 | 典型取值 |
| --- | --- | --- |
| `batch_size` | 每个 batch 含几条样本 | 训练时常取 8、16 等；演示时常取 1 |
| `max_length` | 每条样本的上下文长度（input/target 的 token 数） | 必须不超过模型支持的最大上下文（GPT-2 为 1024） |
| `stride` | 滑动窗口每次前进的步长 | 演示重叠用 1；训练防过拟合常用 `= max_length` |
| `drop_last` | 最后不足一个 batch 的「尾巴」是否丢弃 | 训练时常用 `True`，保证每个 batch 形状一致 |

此外还有 `shuffle`（是否打乱，训练时 `True`、调试时 `False`）和 `num_workers`（多进程加载进程数，演示用 0）。

#### 4.3.2 核心流程

`create_dataloader_v1` 的执行流程只有三步：

1. 初始化分词器：`tokenizer = tiktoken.get_encoding("gpt2")`；
2. 用它构造数据集：`dataset = GPTDatasetV1(txt, tokenizer, max_length, stride)`；
3. 用 `DataLoader` 把数据集包成迭代器，传入 `batch_size`、`shuffle`、`drop_last`、`num_workers`，返回。

#### 4.3.3 源码精读

权威实现见 [previous_chapters.py:34-46](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/previous_chapters.py#L34-L46)：

```python
def create_dataloader_v1(txt, batch_size=4, max_length=256,
                         stride=128, shuffle=True, drop_last=True, num_workers=0):
    tokenizer = tiktoken.get_encoding("gpt2")
    dataset = GPTDatasetV1(txt, tokenizer, max_length, stride)
    dataloader = DataLoader(
        dataset, batch_size=batch_size, shuffle=shuffle,
        drop_last=drop_last, num_workers=num_workers)
    return dataloader
```

几个要点：

- **默认值**是面向「真实训练」设定的：`batch_size=4, max_length=256, stride=128`。注意 `stride=128` 只有 `max_length=256` 的一半，意味着默认配置下相邻样本**有重叠**——这在正文演示里会被特意调整（见 4.4 节）。
- 函数把分词器**写死在内部**，调用者无需关心分词细节，降低使用门槛。
- `DataLoader` 会自动把 `dataset[i]` 返回的 `(input, target)` 两条一维 tensor，按 `batch_size` 堆叠成两条二维 tensor（形状 `(batch_size, max_length)`）。

> **三处文件的小差异（续）**：正文 [ch02.ipynb §2.6](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch02/01_main-chapter-code/ch02.ipynb) 与 `previous_chapters.py` 的 `create_dataloader_v1` 签名一致（都带默认值）；而精简版 [dataloader.ipynb](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch02/01_main-chapter-code/dataloader.ipynb) 里这个函数**没有默认值**（`batch_size, max_length, stride` 是必填位置参数），并在调用时显式用 `stride=max_length`。这是「教学正文」与「精简汇总」风格不同的典型例子。

#### 4.3.4 代码实践

**实践目标**：对一段文本调用 `create_dataloader_v1`，打印前两个 batch 的 input/target 张量，验证 target 是 input 右移一位（本讲的指定实践任务）。

**操作步骤**：在 `ch02/01_main-chapter-code/` 目录运行下面这段**示例代码**（`create_dataloader_v1` 从 `previous_chapters.py` 导入，或复制其定义）：

```python
# from previous_chapters import create_dataloader_v1   # 二选一

with open("the-verdict.txt", "r", encoding="utf-8") as f:
    raw_text = f.read()

dataloader = create_dataloader_v1(
    raw_text, batch_size=1, max_length=4, stride=1, shuffle=False
)

data_iter = iter(dataloader)
first_batch  = next(data_iter)
second_batch = next(data_iter)

print("第一个 batch:", first_batch)
print("第二个 batch:", second_batch)
```

**需要观察的现象**：每个 batch 是 `[input_tensor, target_tensor]` 两个元素；`shuffle=False`、`stride=1` 时，**第二个 batch 的 input 应当等于第一个 batch 的 target**（窗口只右移一位）；而每个 batch 内部，`target` 都是 `input` 右移一位。

**预期结果**（与 [ch02.ipynb §2.6](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch02/01_main-chapter-code/ch02.ipynb) 的输出一致）：

```
第一个 batch: [tensor([[  40,  367, 2885, 1464]]), tensor([[ 367, 2885, 1464, 1807]])]
第二个 batch: [tensor([[ 367, 2885, 1464, 1807]]), tensor([[2885, 1464, 1807, 3619]])]
```

验证「右移一位」：第一个 batch 里 `input=[40,367,2885,1464]`，`target=[367,2885,1464,1807]`，即 `target[:-1] == input[1:]`，末尾多出的 `1807` 就是 input 最后一位要预测的「下一个 token」。同时第二个 batch 的 input `[367,2885,1464,1807]` 正是第一个 batch 的 target，印证了 `stride=1` 的逐位滑动。

#### 4.3.5 小练习与答案

**练习 1**：把上面的 `batch_size` 从 1 改成 8、`stride` 保持 1，第一个 batch 的 input 张量形状会变成什么？

**参考答案**：形状变成 `(8, 4)`，即 8 条样本、每条 4 个 token。`DataLoader` 自动把 8 条一维 tensor 堆叠成二维。8 条之间因为 `stride=1` 仍高度重叠。

**练习 2**：为什么演示和调试时通常设 `shuffle=False`，而真正训练时要设 `shuffle=True`？

**参考答案**：`shuffle=False` 让样本按文本顺序出现，便于人工核对「右移一位」「第二个 batch 紧接第一个」等关系；训练时若不打乱，模型会持续看到相邻的、高度相关的样本，梯度噪声结构单一，容易影响收敛与泛化，所以训练时打乱。

---

### 4.4 stride 的取舍：重叠、样本数与过拟合

#### 4.4.1 概念说明

`stride` 是本讲最需要权衡的超参。它的两端各有一个极端：

- **`stride=1`（最大化重叠）**：窗口每次只右移一位，相邻两条样本共享 `max_length-1` 个 token。样本数最多，但大量样本「长得几乎一样」，模型容易对这段文本**过拟合**，且占用更多内存。
- **`stride=max_length`（无重叠）**：窗口每次正好前进一个窗口长度，相邻样本完全不共享 token。样本数最少（约为 `stride=1` 时的 \( 1/\text{max\_length} \)），但样本彼此独立，过拟合风险低。

正文 notebook 明确点出了这个取舍：「我们在这里增大 stride，是为了让 batch 之间没有重叠，因为更多重叠可能导致过拟合加剧」。实践中的常见做法是：**演示用小 `stride` 看清机制，训练用 `stride=max_length`（或接近）控制过拟合**。

#### 4.4.2 核心流程

对比两种配置在同一段文本上的效果（设 `max_length=4`）：

| 配置 | 窗口重叠 | 样本数（约） | 适用场景 |
| --- | --- | --- | --- |
| `stride=1` | 极大（共享 3/4 token） | 多 | 演示机制、教学 |
| `stride=4`（= `max_length`） | 无 | 少（约为前者 1/4） | 训练，防过拟合 |

批次层面的重叠还有另一层含义：当 `stride < max_length` 时，**不同 batch 之间也会共享 token**（因为相邻样本分属不同 batch 却仍重叠）；当 `stride >= max_length` 时，batch 之间完全不重叠。正文演示 `batch_size=8, stride=4, max_length=4` 正是为了让 8 条样本首尾相接、互不重叠地覆盖序列。

#### 4.4.3 源码精读

正文用一个 `batch_size=8, max_length=4, stride=4` 的例子展示「无重叠批处理」，并配图说明，见 [ch02.ipynb §2.6](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch02/01_main-chapter-code/ch02.ipynb)：

```python
dataloader = create_dataloader_v1(raw_text, batch_size=8, max_length=4, stride=4, shuffle=False)

data_iter = iter(dataloader)
inputs, targets = next(data_iter)
print("Inputs:\n", inputs)
print("\nTargets:\n", targets)
```

notebook 在该 cell 上方专门写了说明：「我们也可以生成批量输出；注意这里我们增大了 stride，使 batch 之间没有重叠，因为更多重叠可能导致过拟合加剧」。其输出（8 行 input、8 行 target）中，每一行 input 与对应 target 仍是「右移一位」关系，而**相邻行之间首尾相接不重叠**，例如第 0 行 input `[40, 367, 2885, 1464]` 的下一位 `1807` 正好是第 1 行 input 的开头 `[1807, 3619, 402, 271]`。

精简版 [dataloader.ipynb](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch02/01_main-chapter-code/dataloader.ipynb) 也采用了同样的 `stride=max_length` 约定（`batch_size=8, max_length=4, stride=4`），并把取出的 batch 直接喂给 token/position 嵌入层，得到形状 `(8, 4, 256)` 的输入嵌入——这正是本讲产出连接到下一讲 `u2-l4`（嵌入层）的接口。

#### 4.4.4 代码实践

**实践目标**：直观对比 `stride=1` 与 `stride=max_length` 在样本数与重叠上的差异。

**操作步骤**：运行下面这段**示例代码**，分别统计两种配置的样本数，并检查相邻样本是否重叠：

```python
import tiktoken
# from previous_chapters import GPTDatasetV1   # 二选一

with open("the-verdict.txt", "r", encoding="utf-8") as f:
    raw_text = f.read()
tokenizer = tiktoken.get_encoding("gpt2")

ds_overlap = GPTDatasetV1(raw_text, tokenizer, max_length=4, stride=1)
ds_nolap   = GPTDatasetV1(raw_text, tokenizer, max_length=4, stride=4)

print("stride=1 样本数:", len(ds_overlap))
print("stride=4 样本数:", len(ds_nolap))

# 检查 stride=1 的前两条是否高度重叠
print("第0条:", ds_overlap[0][0].tolist())
print("第1条:", ds_overlap[1][0].tolist())
```

**需要观察的现象**：`stride=1` 的样本数远多于 `stride=4`（约为 4 倍）；`stride=1` 的第 0 条与第 1 条几乎完全重叠（第 1 条等于第 0 条右移一位），而 `stride=4` 的相邻样本不共享 token。

**预期结果**：`stride=1` 约 5141 条，`stride=4` 约 1286 条（具体数值**待本地验证**）。第 0 条 `[40, 367, 2885, 1464]`、第 1 条 `[367, 2885, 1464, 1807]`，重叠 3 个 token。

#### 4.4.5 小练习与答案

**练习 1**：假设显存只够存 1000 条样本，而你希望尽量降低过拟合风险，`stride` 该设大还是设小？

**参考答案**：设大（接近或等于 `max_length`）。大 `stride` 让样本彼此独立、不重叠，过拟合风险低；虽然样本数变少，但每条都是「新鲜」的训练信号，质量更高。在显存受限时，「少量独立样本」通常优于「大量重叠样本」。

**练习 2**：`create_dataloader_v1` 的默认值是 `stride=128, max_length=256`（即 `stride` 是 `max_length` 的一半）。这意味着默认配置下相邻样本重叠多少？

**参考答案**：相邻样本共享 `max_length - stride = 256 - 128 = 128` 个 token，即重叠一半。这是默认配置，正文演示时会根据需要调整；若担心过拟合，可把 `stride` 调到等于 `max_length`。

## 5. 综合实践

把本讲四个最小模块串起来，完成一个「端到端迷你数据流水线」小任务：

**任务**：用 `create_dataloader_v1` 构造一个 `dataloader`，取一个 batch，手工验证三件事——(a) input/target 形状、(b) target 是 input 右移一位、(c) 把 batch 喂进一个嵌入层后形状正确。

**参考步骤**（在 `ch02/01_main-chapter-code/` 运行，**示例代码**）：

```python
import torch
import tiktoken
# from previous_chapters import create_dataloader_v1   # 二选一

with open("the-verdict.txt", "r", encoding="utf-8") as f:
    raw_text = f.read()

# 用「无重叠」配置：stride = max_length
dataloader = create_dataloader_v1(
    raw_text, batch_size=8, max_length=4, stride=4, shuffle=False
)
inputs, targets = next(iter(dataloader))

# (a) 形状
print("inputs shape:", inputs.shape)     # 期望 (8, 4)

# (b) 验证 target 是 input 右移一位
print("逐位右移成立?", torch.equal(targets[:, :-1], inputs[:, 1:]))   # 期望 True

# (c) 喂进嵌入层（衔接下一讲 u2-l4）
vocab_size, output_dim = 50257, 256
tok_emb = torch.nn.Embedding(vocab_size, output_dim)
print("嵌入后 shape:", tok_emb(inputs).shape)   # 期望 (8, 4, 256)
```

**预期结果**：`inputs shape: torch.Size([8, 4])`；`逐位右移成立? True`；`嵌入后 shape: torch.Size([8, 4, 256])`。若三项都符合，说明你已经打通了「文本 → token ID → 滑动窗口样本 → 批处理 → 嵌入」这条第 2 章的完整数据流水线。形状与 [dataloader.ipynb](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch02/01_main-chapter-code/dataloader.ipynb) 最终输出的 `torch.Size([8, 4, 256])` 一致。

## 6. 本讲小结

- 语言模型的训练目标是**下一个 token 预测**，所以训练样本必须成对：input 是一段上下文，target 是它**右移一位**的序列——`target[j]` 就是 `input[j]` 之后要预测的 token。
- **滑动窗口**沿 token 序列以步长 `stride` 前进，每个起点切出长度为 `max_length` 的 input 与对应右移一位的 target，把一整本书变成大量训练对。
- `GPTDatasetV1` 把这套切片逻辑封装成 PyTorch `Dataset`：在 `__init__` 里一次性切好所有样本存进列表，`__len__`/`__getitem__` 供 `DataLoader` 调用。
- `create_dataloader_v1` 是工厂函数，内部写死 GPT-2 BPE 分词器，再用 `DataLoader` 完成**分批/打乱/丢尾**；四个关键参数是 `batch_size`、`max_length`、`stride`、`drop_last`。
- `stride` 是核心权衡旋钮：`stride=1` 样本多但高度重叠、易过拟合；`stride=max_length` 样本少但彼此独立、过拟合风险低，训练时常用后者。
- 本讲产出的 `dataloader` 直接输出 `(batch_size, max_length)` 的 token ID 张量，正是下一讲嵌入层（`u2-l4`）的输入接口。

## 7. 下一步学习建议

- **下一讲 `u2-l4`（Token 嵌入与位置嵌入）**：把本讲输出的整数 token ID 通过 `nn.Embedding` 查表变成连续向量（token embedding），再叠加可学习的位置嵌入，得到 GPT 真正的输入表示。建议先回顾本讲综合实践中 `tok_emb(inputs)` 那一行，带着「形状如何从 `(8,4)` 变成 `(8,4,256)`」的问题进入下一讲。
- **延伸阅读**：精简版 [dataloader.ipynb](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch02/01_main-chapter-code/dataloader.ipynb) 把本讲主线代码与嵌入层串成最短可运行流水线，适合作为「 cheatsheet」收藏。
- **向前进到模型侧**：等学完嵌入层，可在第 4 章回头 `from previous_chapters import GPTDatasetV1, create_dataloader_v1`，亲手把本讲的 `dataloader` 喂进 `GPTModel`，体会「数据流水线 → 模型」的衔接。

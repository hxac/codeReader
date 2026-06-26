# 因果注意力与掩码

> 所属单元：u3 注意力机制（第 3 章） · 依赖讲义：u3-l1 自注意力原理

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清楚**为什么语言模型在训练时绝不能"看到未来 token"**，以及不做处理会发生什么"信息泄露"。
2. 用上三角矩阵 `torch.triu` 构造因果掩码，并用 `masked_fill_`（原地）把对角线以上的注意力分数置为 `-inf`。
3. 理解 `softmax` 配合 `-inf` 为什么能"顺带完成归一化"，而不必再手动重新归一化。
4. 说清 `nn.Module.register_buffer` 的作用：让"不参与训练、但要跟随设备、要进存档"的张量（如掩码）被框架统一管理，并区分**持久化 / 非持久化**两种行为。
5. 读懂 `CausalAttention` 类，并知道它在第 4 章被复用进 `MultiHeadAttention`（通过 `previous_chapters.py`，见 u1-l3 的汇总机制）。

## 2. 前置知识

在进入本讲前，请确认你已经掌握（这些来自 u3-l1 与 u2-l4）：

- **缩放点积注意力**：给定查询 `Q`、键 `K`、值 `V`，注意力权重为
  \[
  \mathrm{AttnWeights}=\mathrm{softmax}\!\left(\frac{QK^{\top}}{\sqrt{d_k}}\right)
  \]
  上下文向量 = 权重矩阵乘以 `V`。softmax 让每一行（每个查询对全体键的注意力）加起来等于 1。
- **输入张量形状**：嵌入层（u2-l4）的输出是 `batch × num_tokens × emb_dim`，这正是注意力层的输入。
- **`nn.Linear`、`nn.Parameter`、`nn.Dropout`**：`SelfAttention_v2` 用三个 `nn.Linear` 生成 Q/K/V（u3-l1），dropout 用于训练时随机置零一部分权重以抑制过拟合。

一个关键提醒：u3-l1 实现的 `SelfAttention_v2` 是**双向**的——每个位置都能看到所有其他位置（包括"未来"）。这对"理解一句话"没问题，但对"逐字生成文本"的 GPT 是不允许的。本讲就是来解决这件事的。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [ch03/01_main-chapter-code/ch03.ipynb](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch03/01_main-chapter-code/ch03.ipynb) | 第 3 章正文 notebook。3.5 节"因果注意力"逐步演示掩码的两种写法，并给出最终的 `CausalAttention` 类。 |
| [ch03/03_understanding-buffers/understanding-buffers.ipynb](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch03/03_understanding-buffers/understanding-buffers.ipynb) | 专门解释 `register_buffer` 的附加 notebook，用"有/无 buffer"两个版本对照，讲清设备迁移与 `state_dict` 的差别。 |
| [ch04/01_main-chapter-code/previous_chapters.py](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/previous_chapters.py) | 第 4 章复用的"成品汇总器"（见 u1-l3）。其中的 `MultiHeadAttention` 把本讲的掩码写法原样继承下来，是这套机制在真实模型里的落点。 |

> 说明：notebook 是 JSON 文件，下面永久链接里的 `#L行号` 指向该 JSON 中的源代码行。点开后会高亮对应那条源码。

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

- **4.1** 因果注意力的动机：信息泄露问题
- **4.2** 上三角掩码：从 `tril` 到 `-inf` 技巧（因果掩码的工程实现）
- **4.3** `register_buffer`：让掩码跟随设备与存档
- **4.4** 收尾：整合成可用的 `CausalAttention` 类

### 4.1 因果注意力的动机：信息泄露问题

#### 4.1.1 概念说明

GPT 是一个**自回归（autoregressive）**语言模型：它一次只预测"下一个 token"。训练时，模型在第 `i` 个位置上要预测第 `i+1` 个 token，而它**能用来做这个预测的输入，只能来自第 0…i 个位置**——也就是"已经写出来的字"。

但是 u3-l1 的缩放点积注意力是**全连接**的：位置 2 可以回头看位置 0、1，也可以**偷看位置 3、4、5**（这些是"未来"）。如果允许偷看未来，模型就不再需要费力学习预测——直接抄答案即可。这叫做**信息泄露（information leakage）**，会让训练目标失效。

**因果注意力（causal attention，又叫 masked self-attention）** 的任务就是：把注意力矩阵里"对角线以上"（代表"查询 i 看键 j，且 j>i"）的那些位置屏蔽掉，让每个位置只能关注它自己和它之前的 token。

#### 4.1.2 核心流程

用一个 6×6 的注意力权重矩阵来理解（行=查询，列=被关注的键）：

```
         key:  0     1     2     3     4     5
query 0      [ ✓     ✗     ✗     ✗     ✗     ✗ ]   <- token 0 只能看自己
query 1      [ ✓     ✓     ✗     ✗     ✗     ✗ ]
query 2      [ ✓     ✓     ✓     ✗     ✗     ✗ ]
 ...
query 5      [ ✓     ✓     ✓     ✓     ✓     ✓ ]   <- token 5 能看全部（都在它之前）
```

- 保留的部分是一个**下三角**（含主对角线）。
- 屏蔽的部分是一个**严格上三角**（主对角线之上）。

这就是"因果"二字的几何含义：允许看的区域恰好是因果序"过去 + 现在"。

#### 4.1.3 源码精读

正文先用 u3-l1 的 `SelfAttention_v2` 重新算出一组**未做因果处理**的注意力权重，作为后续要被掩码的对象：

[ch03.ipynb:L1271-L1273](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch03/01_main-chapter-code/ch03.ipynb#L1271-L1273) ——复用 `SelfAttention_v2` 的 Q/K 权重，算出缩放点积注意力权重 `attn_weights`。注意此时它是**满矩阵**，每行都把权重分给了所有 6 个位置，包括"未来"位置。这一步就是"信息泄露"发生的地方，也是本讲要修正的起点。

随后 notebook 进入 3.5 节"Hiding future words with causal attention"，明确点出做法：

> In causal attention, the attention weights above the diagonal are masked, ensuring that … the LLM is unable to utilize future tokens …（对角线以上的注意力权重被屏蔽，确保 LLM 无法使用未来 token。）

#### 4.1.4 代码实践

**实践目标**：亲眼看到"未掩码"注意力权重的每行都分给了未来位置，建立"必须屏蔽"的直觉。

**操作步骤**（可在 `ch03.ipynb` 对应 cell 里直接运行，或另开一个 cell）：

```python
# 示例代码：复现未掩码的注意力权重，观察"未来"也有非零权重
import torch
torch.manual_seed(789)
inputs = torch.tensor(
  [[0.43, 0.15, 0.89], [0.55, 0.87, 0.66], [0.57, 0.85, 0.64],
   [0.22, 0.58, 0.33], [0.77, 0.25, 0.10], [0.05, 0.80, 0.55]])

# 直接用 Q=K=inputs 的最简点积注意力（仅为演示，无训练权重）
attn_scores = inputs @ inputs.T
attn_weights = torch.softmax(attn_scores, dim=-1)
print(attn_weights)
print("每行求和：", attn_weights.sum(dim=-1))
```

**需要观察的现象**：打印出的矩阵里，**每一行的所有 6 个元素都大于 0**——也就是说位置 0 的上下文里混进了位置 1~5 的信息。

**预期结果**：每行求和为 1.0（softmax 的性质），但"未来"列的权重非零，这就是泄露。本讲后续会把它们清零。

#### 4.1.5 小练习与答案

**练习 1**：如果把整段文本一次性喂给双向注意力来"打分"，并不会泄露；为什么"训练一个要逐字生成的模型"时就必须因果掩码？

> **答**：推理（生成）时模型只能拿到已生成的前缀，看不到未来；若训练时让它看了未来，训练分布和推理分布就不一致（train/test mismatch），模型学到的"预测下一个词"的能力是假的。

**练习 2**：因果掩码屏蔽的是"上三角"还是"下三角"？

> **答**：屏蔽**严格上三角**（主对角线之上），保留下三角（含对角线）。因为 `query i` 只允许看 `key j`，其中 `j <= i`。

---

### 4.2 上三角掩码：从 `tril` 到 `-inf` 技巧

这是本讲的核心工程模块，对应学习目标"用上三角矩阵构造掩码并 `masked_fill_`"。notebook 给出了**两种**写法，第二种才是最终采用的。

#### 4.2.1 概念说明

要让某个位置的注意力权重为 0，有两种思路：

1. **朴素思路**：softmax 之后，把上三角的权重直接乘 0，再手动重新归一化（让每行重新加起来等于 1）。
   - 缺点：多一步手动归一化，且 softmax 之后改值在数学上略别扭。
2. **`-inf` 技巧（推荐）**：在 softmax **之前**，把上三角的**未归一化注意力分数**置为 `-inf`。因为 \(\lim_{x\to -\infty} e^{x}=0\)，这些位置经 softmax 后权重自然变成 0，而剩下有限项会自动重新归一化到和为 1。
   - 优点：一步到位，干净、数值稳定，也是工业实现的标准做法。

数学上，对一个被掩码的分数向量做 softmax：

\[
\mathrm{softmax}(s)_i=\frac{e^{s_i}}{\sum_j e^{s_j}},\qquad
\text{若 }s_i=-\infty\text{，则 }e^{s_i}=0\Rightarrow\mathrm{softmax}(s)_i=0
\]

分子里被屏蔽项消失，分母只剩未被屏蔽项的和，于是剩余权重自动归一。

#### 4.2.2 核心流程

两种写法的流程对照：

```
写法 A（朴素，先掩后归一）：
  mask_simple = tril(ones)          # 下三角为1、上三角为0
  masked      = attn_weights * mask # 把上三角权重清零（但行和不再=1）
  masked_norm = masked / masked.sum(dim=-1, keepdim=True)   # 手动重新归一

写法 B（-inf 技巧，最终采用）：
  mask   = triu(ones, diagonal=1)   # 严格上三角为1（其余0）
  masked = attn_scores.masked_fill(mask.bool(), -inf)        # 分数置-inf
  attn_weights = softmax(masked / sqrt(d_k), dim=-1)         # 自动归一
```

两个关键工具：

- `torch.triu(m, diagonal=1)`：取**严格上三角**（主对角线之上）为 1，正好对应"要屏蔽的未来位置"。
- `tensor.masked_fill_(mask_bool, value)`：**原地**把 `mask_bool` 为 True 的位置填成 `value`（这里 `value = -torch.inf`）。带下划线 `_` 的版本是 in-place，省内存；演示时用的是非原地 `masked_fill`。

#### 4.2.3 源码精读

**写法 A 的三步**（用于教学对照，最终不采用）：

[ch03.ipynb:L1306-L1306](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch03/01_main-chapter-code/ch03.ipynb#L1306) ——`mask_simple = torch.tril(torch.ones(context_length, context_length))`，构造**下三角**全 1 掩码（含对角线），用来"保留"允许看的位置。

[ch03.ipynb:L1339-L1339](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch03/01_main-chapter-code/ch03.ipynb#L1339) ——`masked_simple = attn_weights * mask_simple`，逐元素相乘把上三角权重清零。notebook 在此处提醒：在 softmax 之后做掩码会破坏"每行和为 1"的概率分布。

[ch03.ipynb:L1382-L1383](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch03/01_main-chapter-code/ch03.ipynb#L1382-L1383) ——`row_sums = masked_simple.sum(dim=-1, keepdim=True)` 再 `masked_simple_norm = masked_simple / row_sums`，手动重新归一化，使每行重新加起来等于 1。

**写法 B（`-inf` 技巧，最终采用）**：

[ch03.ipynb:L1425-L1426](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch03/01_main-chapter-code/ch03.ipynb#L1425-L1426) ——核心两行：
```python
mask = torch.triu(torch.ones(context_length, context_length), diagonal=1)
masked = attn_scores.masked_fill(mask.bool(), -torch.inf)
```
`triu(..., diagonal=1)` 得到严格上三角为 1 的布尔掩码，`masked_fill` 把这些位置的**分数**（注意是 softmax 前的分数，不是权重）置为 `-inf`。

[ch03.ipynb:L1459-L1459](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch03/01_main-chapter-code/ch03.ipynb#L1459) ——`attn_weights = torch.softmax(masked / keys.shape[-1]**0.5, dim=-1)`，对掩码后的分数做缩放 softmax。由于 `-inf` 项的指数为 0，输出里这些位置自然为 0，且每行自动归一到和为 1——无需再手动归一化。

> 对照写法 A 和 B 的输出：两者最终得到的因果注意力权重矩阵**数值完全一致**（你可以去 notebook 里核对），但写法 B 更简洁，所以真实模型用它。

#### 4.2.4 代码实践（本讲必做）

**实践目标**：亲手实现带因果掩码的单头注意力，打印掩码后的**注意力分数矩阵**，确认严格上三角区域被置为 `-inf`。

**操作步骤**（示例代码，可直接运行）：

```python
# 示例代码：带因果掩码的单头注意力（-inf 技巧）
import torch
torch.manual_seed(123)

inputs = torch.tensor(
  [[0.43, 0.15, 0.89], [0.55, 0.87, 0.66], [0.57, 0.85, 0.64],
   [0.22, 0.58, 0.33], [0.77, 0.25, 0.10], [0.05, 0.80, 0.55]])
context_length = inputs.shape[0]

# 1) 算未归一化的注意力分数（Q=K=inputs 做最简演示）
attn_scores = inputs @ inputs.T
d_k = inputs.shape[-1]

# 2) 构造严格上三角掩码，把"未来"分数置为 -inf
mask = torch.triu(torch.ones(context_length, context_length), diagonal=1)
masked_scores = attn_scores.masked_fill(mask.bool(), -torch.inf)
print("掩码后的注意力分数矩阵：\n", masked_scores)

# 3) 缩放 + softmax，得到因果注意力权重
attn_weights = torch.softmax(masked_scores / d_k**0.5, dim=-1)
print("\n因果注意力权重：\n", attn_weights)
print("\n每行求和：", attn_weights.sum(dim=-1))
```

**需要观察的现象**：

1. `masked_scores` 矩阵里，主对角线**以上**全是 `-inf`，对角线及以下是有限数值。
2. `attn_weights` 里上三角全为 `0.0000`，且**每行和为 1.0**（说明 `-inf` 技巧自动完成了归一化）。

**预期结果**：与 notebook 中 3.5.1 节的输出形态一致——上三角 0、行和为 1。

> 注：若在 `masked_scores` 里看到 `-inf`，说明 `masked_fill` 生效；若上三角仍是有限数，检查 `mask.bool()` 是否取了 `diagonal=1`（不带这个参数会把主对角线也屏蔽掉，那是错的）。

#### 4.2.5 小练习与答案

**练习 1**：为什么必须用 `diagonal=1`？如果写成 `torch.triu(ones)`（不带 `diagonal`）会怎样？

> **答**：`diagonal=1` 表示从主对角线**之上第一行**开始取，保留主对角线，即"token 可以看到自己"。不带 `diagonal` 时 `triu` 默认 `diagonal=0`，会把主对角线也划进上三角一起屏蔽，导致 token 连自己都看不到——第 0 行会整行 `-inf`，softmax 出现 `0/0` 得到 `NaN`。

**练习 2**：把掩码放在 softmax **之前**（写法 B）相比放在 softmax **之后**（写法 A）再重新归一，有什么本质好处？

> **答**：写法 B 一步完成"屏蔽 + 归一"，数学上更干净、数值更稳定（不依赖额外一次除法），也和 PyTorch 内置 `scaled_dot_product_attention` 的实现一致，便于后续替换为高效内核（见 u9-l2）。

---

### 4.3 `register_buffer`：让掩码跟随设备与存档

掩码是一个"和输入长度有关、训练中不变、也不是可学习参数"的张量。怎么把它放进 `nn.Module`？直接 `self.mask = ...` 会埋坑——本模块讲清楚为什么必须用 `register_buffer`，以及它的持久化行为。

#### 4.3.1 概念说明

`nn.Module` 里常见的三类"挂在模块上的张量"：

| 类型 | 声明方式 | 会被优化器更新？ | 会随 `.to(device)` 迁移？ | 会进 `state_dict`？ |
| --- | --- | --- | --- | --- |
| 参数 (parameter) | `nn.Parameter(...)` / `nn.Linear` 内部权重 | 是 | 是 | 是 |
| 普通张量 | `self.mask = torch.tensor(...)` | 否 | **否** | **否** |
| 缓冲 (buffer) | `self.register_buffer("mask", ...)` | 否 | **是** | **是**（默认） |

掩码既不需要训练，又必须和参数待在**同一个设备**上（否则 `masked_fill_` 会因为"一个在 CPU、一个在 GPU"报错），还要能跟着模型一起保存/加载。这三条合起来，正是 **buffer** 的定义。

关于"非持久化"：`register_buffer` 有一个 `persistent` 参数，默认 `persistent=True`（缓冲会被写入 `state_dict`）。若显式传 `persistent=False`，则它**不进** `state_dict`、保存加载时不持久化——这就是学习目标里"非持久化"的含义。本项目的掩码用默认的持久化，因此会出现在 `state_dict` 里。

#### 4.3.2 核心流程

理解 buffer 的最佳方式是看"没有 buffer 会出什么错"：

```
self.mask = torch.triu(...)        # 普通张量，注册在 CPU
model.to("cuda")                   # 只搬参数，mask 还留在 CPU
attn_scores.masked_fill_(mask...)  # attn_scores 在 cuda，mask 在 cpu
# -> RuntimeError: expected self and mask to be on the same device
```

用 buffer 后：

```
self.register_buffer("mask", torch.triu(...))   # 登记为缓冲
model.to("cuda")                                 # buffer 随参数一起搬到 cuda
# 一切正常，且 mask 出现在 model.state_dict() 中
```

#### 4.3.3 源码精读

附加 notebook [understanding-buffers.ipynb](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch03/03_understanding-buffers/understanding-buffers.ipynb) 专门讲这件事。它定义了一个"故意不用 buffer"的对照类：

[understanding-buffers.ipynb:L75-L85](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch03/03_understanding-buffers/understanding-buffers.ipynb#L75-L85) ——`class CausalAttentionWithoutBuffers` 里写的是 `self.mask = torch.triu(torch.ones(context_length, context_length), diagonal=1)`，即把掩码当成普通张量直接挂在 `self` 上。

接着 notebook 把模型 `.to(device)` 后再前向，立刻报错（notebook 输出区记录了这条 traceback）：

> `RuntimeError: expected self and mask to be on the same device, but got mask on cpu and self on cuda:0`

——参数随 `.to(device)` 搬到了 GPU，但普通张量 `mask` 没被搬走，于是 `masked_fill_` 时两边设备不一致。

随后给出"用 buffer"的修正版：

[understanding-buffers.ipynb:L450-L464](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch03/03_understanding-buffers/understanding-buffers.ipynb#L450-L464) ——`class CausalAttentionWithBuffer` 把那行 `self.mask = ...` 注释掉，换成 `self.register_buffer("mask", torch.triu(torch.ones(context_length, context_length), diagonal=1))`。此后 `.to(device)` 会把 `mask` 一起搬走，前向不再报错。

notebook 还演示了 buffer 会进 `state_dict`：

[understanding-buffers.ipynb:L618-L618](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch03/03_understanding-buffers/understanding-buffers.ipynb#L618) ——`ca_without_buffer.state_dict()` 里**只有** `W_query/W_key/W_value` 的权重，**没有** `mask`。

[understanding-buffers.ipynb:L669-L669](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch03/03_understanding-buffers/understanding-buffers.ipynb#L669) ——`ca_with_buffer.state_dict()` 里**多了**一项 `'mask'`（持久化），证明 buffer 默认会被保存。若把 `mask` 改成非持久值（修改后保存再加载），它能被还原；而普通张量版本无论怎么改都还原不回来——这就是持久化的实际差别。

> 最终正文采用的 `CausalAttention` 类里就是用 `register_buffer` 注册掩码：

[ch03.ipynb:L1639-L1639](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch03/01_main-chapter-code/ch03.ipynb#L1639) ——`self.register_buffer('mask', torch.triu(torch.ones(context_length, context_length), diagonal=1))`，把上三角掩码登记为缓冲。

#### 4.3.4 代码实践

**实践目标**：亲手复现"无 buffer 时的设备报错"，并对比有无 buffer 时 `state_dict` 的差别。

**操作步骤**（示例代码，CPU 上即可观察 `state_dict` 差异；设备报错需有 GPU/MPS 才能完整复现）：

```python
# 示例代码：对比有无 buffer 的 state_dict
import torch, torch.nn as nn

class NoBuffer(nn.Module):
    def __init__(self, ctx):
        super().__init__()
        self.lin = nn.Linear(3, 2)
        self.mask = torch.triu(torch.ones(ctx, ctx), diagonal=1)      # 普通张量
class WithBuffer(nn.Module):
    def __init__(self, ctx):
        super().__init__()
        self.lin = nn.Linear(3, 2)
        self.register_buffer("mask", torch.triu(torch.ones(ctx, ctx), diagonal=1))  # 缓冲

nb, wb = NoBuffer(6), WithBuffer(6)
print("NoBuffer state_dict 键：", list(nb.state_dict().keys()))
print("WithBuffer state_dict 键：", list(wb.state_dict().keys()))
```

**需要观察的现象**：

- `NoBuffer` 的键只有 `['lin.weight', 'lin.bias']`，**没有** `mask`。
- `WithBuffer` 的键多出 `'mask'`。

**预期结果**：与附加 notebook 中 `ca_without_buffer.state_dict()` / `ca_with_buffer.state_dict()` 的输出一致。

> 若机器有 CUDA/MPS，可额外执行 `wb.to(device)` 后检查 `wb.mask.device` 是否跟随迁移；对 `nb.to(device)` 则 `nb.mask.device` 仍是 `cpu`（这就是报错根源）。无 GPU 时此项「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：能不能把掩码定义成 `nn.Parameter` 来"顺便"让它跟随设备和存档？

> **答**：技术上可以做到跟随设备与存档，但**不应**这么做。`nn.Parameter` 会被优化器当作可学习参数去更新梯度，而掩码是固定的结构常量、没有意义去"学习"。用 `register_buffer` 才能既跟随设备/存档、又不被优化器改动。

**练习 2**：若希望掩码**不**出现在 `state_dict`（比如它能从 `context_length` 随时重建，不想占存档空间），该怎么做？

> **答**：调用 `self.register_buffer("mask", tensor, persistent=False)`。这样它仍随 `.to(device)` 迁移，但保存模型时不会被写入 `state_dict`——这正是"非持久化缓冲"。

---

### 4.4 收尾：整合成可用的 `CausalAttention` 类

把 4.2 的掩码、4.3 的 buffer，再加上 dropout 与 batch 维，整合成第 3 章最终交付的 `CausalAttention` 类。

#### 4.4.1 概念说明

真实的 DataLoader（u2-l3）产出的是**带 batch 维**的张量，形状 `batch × num_tokens × emb_dim`。所以最终的注意力类要：

1. 接受 3 维输入（多了 batch 维 `b`）。
2. 用三个 `nn.Linear` 生成 Q/K/V。
3. 用 `keys.transpose(1, 2)` 而不是 `.T` 做转置（`.T` 会把 batch 维也翻转，3 维以上必须指定维度）。
4. 用 buffer 里的掩码做 `masked_fill_`，并对**实际 token 数** `num_tokens` 做切片（兼容 batch 里 token 数小于 `context_length` 的情况）。
5. 在注意力权重上做 dropout（仅训练时生效）。

#### 4.4.2 核心流程

```
forward(x):                       # x: (b, num_tokens, d_in)
  keys/queries/values = W_*(x)    # (b, num_tokens, d_out)
  attn_scores = queries @ keys.transpose(1, 2)        # (b, N, N)
  attn_scores.masked_fill_(mask.bool()[:N, :N], -inf) # 因果屏蔽（按实际 N 切片）
  attn_weights = softmax(scores / sqrt(d_k))          # 自动归一
  attn_weights = dropout(attn_weights)                # 训练时随机置零
  return attn_weights @ values                        # (b, N, d_out)
```

注意 `self.mask.bool()[:num_tokens, :num_tokens]` 这步切片：掩码是按 `context_length`（模型支持的最大长度）预建的，但当前 batch 的真实长度 `num_tokens` 可能更短，所以要截取左上角 `N×N` 子块。

#### 4.4.3 源码精读

[ch03.ipynb:L1629-L1660](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch03/01_main-chapter-code/ch03.ipynb#L1629-L1660) ——完整的 `CausalAttention` 类。几个关键点对照本讲前三个模块：

- [L1639](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch03/01_main-chapter-code/ch03.ipynb#L1639)：`register_buffer('mask', ...)` ——4.3 的 buffer。
- [L1652](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch03/01_main-chapter-code/ch03.ipynb#L1652)：`attn_scores.masked_fill_(self.mask.bool()[:num_tokens, :num_tokens], -torch.inf)` ——4.2 的 `-inf` 技巧，带下划线表示**原地**操作，并按 `num_tokens` 切片。

这套写法会被第 4 章**原样继承**。在 [ch04/previous_chapters.py](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/previous_chapters.py) 的 `MultiHeadAttention` 里（这是 GPT 模型实际使用的版本，见 u1-l3 的汇总机制）：

[previous_chapters.py:L63-L63](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/previous_chapters.py#L63) ——同样 `self.register_buffer("mask", torch.triu(torch.ones(context_length, context_length), diagonal=1))`。

[previous_chapters.py:L87-L90](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/previous_chapters.py#L87-L90) ——`mask_bool = self.mask.bool()[:num_tokens, :num_tokens]` 再 `attn_scores.masked_fill_(mask_bool, -torch.inf)`，与单头版完全一致，只是 `attn_scores` 多了一个 `num_heads` 维度。这说明：**多头注意力的因果性，和单头是完全相同的掩码机制**，多头的拆分/合并是另一回事（见 u3-l3）。

#### 4.4.4 代码实践

**实践目标**：实例化 `CausalAttention`，跑一次前向，验证输出形状与因果性。

**操作步骤**（建议直接在 `ch03/01_main-chapter-code/ch03.ipynb` 的对应 cell 运行）：

1. 运行定义 `CausalAttention` 的 cell（[L1629 起](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch03/01_main-chapter-code/ch03.ipynb#L1629)）。
2. 运行其下方的实例化 cell：`ca = CausalAttention(d_in, d_out, context_length, 0.0); context_vecs = ca(batch)`。
3. 打印 `context_vecs.shape` 和 `ca.mask`。

**需要观察的现象**：

- `context_vecs.shape` 为 `torch.Size([2, 6, 2])`：batch=2、6 个 token、`d_out=2`。
- `ca.mask` 是一个 6×6 的严格上三角为 1 的矩阵，且它出现在 `ca.state_dict()` 里（因为是 buffer）。
- 当 `dropout=0.0` 时两次前向结果一致（无随机性）。

**预期结果**：与 notebook 中该 cell 的输出一致（`context_vecs.shape: torch.Size([2, 6, 2])`）。

> 若想验证"因果性确实生效"，可把 `inputs` 的最后一个 token（位置 5）改成完全不同的值，再比较**前 5 个位置的输出是否变化**——理论上不应变化，因为它们看不到位置 5。

#### 4.4.5 小练习与答案

**练习 1**：`CausalAttention.forward` 里为什么用 `keys.transpose(1, 2)` 而不是 `keys.T`？

> **答**：输入是 3 维 `(b, num_tokens, d_out)`。`.T` 在 2.4 之前会把**所有**维度翻转成 `(d_out, num_tokens, b)`，破坏 batch 维；`transpose(1, 2)` 只交换后两维，得到 `(b, d_out, num_tokens)`，才是 `queries @ keys.transpose(1,2)` 所需的形状。

**练习 2**：`masked_fill_` 末尾的下划线 `_` 意味着什么？为什么这里适合用原地操作？

> **答**：下划线表示 **in-place（原地）**，直接修改 `attn_scores` 而不新建张量。掩码只用到一次、之后不再需要原始 `attn_scores`，原地操作能省一份内存，在长序列、多头、多层时这点很可观。

---

## 5. 综合实践

把本讲四个模块串起来，做一个端到端的小任务：

> **任务**：写一个函数 `causal_attention_forward(x, dropout=0.0)`，对任意 `batch × N × d` 的输入返回因果注意力后的上下文向量；并配套写一个验证函数，确认"修改位置 `i` 之后的所有 token，不影响位置 `0…i-1` 的输出"。

参考实现框架（示例代码）：

```python
# 示例代码：综合实践框架
import torch, torch.nn as nn

class MiniCausalAttention(nn.Module):
    def __init__(self, d_in, d_out, context_length, dropout=0.0):
        super().__init__()
        self.W_query = nn.Linear(d_in, d_out)
        self.W_key   = nn.Linear(d_in, d_out)
        self.W_value = nn.Linear(d_in, d_out)
        self.dropout = nn.Dropout(dropout)
        self.register_buffer(
            "mask", torch.triu(torch.ones(context_length, context_length), diagonal=1))

    def forward(self, x):
        b, N, _ = x.shape
        q = self.W_query(x); k = self.W_key(x); v = self.W_value(x)
        scores = q @ k.transpose(1, 2)
        scores.masked_fill_(self.mask.bool()[:N, :N], -torch.inf)   # 4.2 + 4.4
        weights = torch.softmax(scores / k.shape[-1]**0.5, dim=-1)
        weights = self.dropout(weights)
        return weights @ v

torch.manual_seed(123)
x = torch.randn(1, 6, 3)
att = MiniCausalAttention(3, 2, context_length=6, dropout=0.0)

out1 = att(x)
x_modified = x.clone()
x_modified[0, 5] = 99.0                      # 把"未来"token 5 改得面目全非
out2 = att(x_modified)

print("位置 0..4 输出是否变化：", not torch.allclose(out1[0, :5], out2[0, :5]))
print("位置 5 输出是否变化：",    not torch.allclose(out1[0, 5:], out2[0, 5:]))
print("mask 是否进 state_dict：", "mask" in att.state_dict())   # 4.3
```

**预期结果**：

- 位置 0..4 输出**不变**（`torch.allclose` 为 True，故 `not ...` 打印 `False`）——证明因果屏蔽生效，看不到未来。
- 位置 5 输出**会变**（`not allclose` 为 True）。
- `mask` 在 `state_dict` 里（`True`）——证明 buffer 被持久化。

如果三条都对，说明你已把"因果掩码 + 上三角 `-inf` 技巧 + `register_buffer`"三件事打通。

## 6. 本讲小结

- 语言模型逐字生成，**绝不能在训练时看到未来 token**；否则预测目标失效（信息泄露）。因果注意力用掩码把"主对角线以上"的位置屏蔽掉。
- 实现因果掩码推荐 **`-inf` 技巧**：softmax 前用 `torch.triu(..., diagonal=1)` 生成严格上三角布尔掩码，`masked_fill_(mask, -torch.inf)` 把对应分数置 `-inf`，softmax 后这些位置自然为 0 且自动归一。
- 朴素写法（softmax 后乘 0 再手动重新归一）等价但更繁琐；两者最终注意力权重一致。
- `nn.Module` 里的掩码必须用 **`register_buffer`**：它不被优化器训练，但能随 `.to(device)` 迁移设备、默认进入 `state_dict`（持久化）；传 `persistent=False` 可改为非持久化。
- "普通张量直接挂 `self`"会埋设备不一致的雷（GPU 训练时 `masked_fill_` 报错），是反面教材。
- 最终 `CausalAttention` 类把 batch 维、`transpose(1,2)`、buffer 掩码、`masked_fill_`、dropout 整合到一起；这套掩码写法在第 4 章 `MultiHeadAttention`（`previous_chapters.py`）里被原样继承。

## 7. 下一步学习建议

- **下一讲 u3-l3（多头注意力实现）**：本讲的 `CausalAttention` 是"单头"。下一讲会把它扩展成 `MultiHeadAttention`——注意，**因果掩码的机制完全不变**（见 4.4.3 中 `previous_chapters.py` 的同一套 `register_buffer` + `masked_fill_`），多头只是在 `d_out` 维度上做拆分与重排。
- **第 4 章 u4-l2（TransformerBlock）**：`CausalAttention` 会被装进一个 Transformer 块，外加残差连接与前馈网络。届时你会看到 dropout、`model.eval()` 如何影响注意力。
- **进阶 u9-l2（高效多头注意力）**：本讲手写的 `masked_fill_` + softmax，在工业实现里会被替换成 `torch.nn.functional.scaled_dot_product_attention`，它原生支持 `is_causal=True`，原理与本讲完全一致——理解了本讲，就读懂了那个高效内核在做什么。
- **延伸阅读**：若想再巩固 buffer，可重跑 [understanding-buffers.ipynb](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch03/03_understanding-buffers/understanding-buffers.ipynb) 里"保存/加载模型还原 mask"那段（L710 起），体会持久化缓冲在权重存档中的角色，这与 u5-l4 的 `state_dict` 权重加载直接相关。

# 讲义标题：简单自回归文本生成

## 1. 本讲目标

在上一讲（u4-l3）里，我们已经把 `GPTModel` 装好：输入一串 token ID，它能吐出一组形状为 `(batch, num_tokens, vocab_size)` 的 logits。但「输出 logits」还不等于「写出一段话」——logits 只是对词表里每个词打的一组分。本讲要解决最后一步：**怎么让模型自己接着往下写**。

学完本讲，你应当能够：

- 读懂 `generate_text_simple` 的**自回归循环结构**，说出它如何「预测一个 token → 拼回输入 → 再预测下一个」。
- 解释两处关键操作：**上下文裁剪** `idx[:, -context_size:]` 与**取最后一步 logits** `logits[:, -1, :]`，以及它们各自解决什么问题。
- 理解 **argmax 贪心采样**为何是确定性的、为何未训练模型的输出看起来像「语法正确但语义是乱码」。
- 用 `main()` 入口跑通一次完整生成（编码 → 生成 → 解码），并能动手修改起始上下文、生成长度等参数观察行为。

## 2. 前置知识

- **自回归（autoregressive）**：一个过程「回归到自身」——用自己已经产生的输出来作为下一步的输入。语言模型生成文本就是自回归的：每写一个新词，都要把它接在已有文本后面再写下一个。
- **下一个 token 预测（next-token prediction）**：GPT 的训练目标。给定一段上下文，模型只预测「紧跟着的那一个 token」。（详见 u2-l3 的滑动窗口与 u5-l1 的损失函数。）
- **logits**：模型输出头 `out_head` 给出的、未经 softmax 归一化的原始分数，词表里每个词一个，分越高越倾向被预测为下一个词。（详见 u4-l3。）
- **因果掩码（causal mask）**：在注意力里屏蔽「未来」位置，保证第 `t` 个位置只能看到 `≤ t` 的 token（详见 u3-l2）。它在本讲有一个重要推论：**序列最后一个位置看到了全部上下文**，所以它的预测就是「整个序列的下一个词」。
- **`context_length` / `context_size`**：模型一次能处理的最大 token 数（GPT-2 small 是 1024）。它由位置嵌入 `pos_emb` 的行数决定（详见 u2-l4 与 u4-l3），**一旦序列超过这个长度就必须裁剪**。
- **贪心解码（greedy decoding）**：每一步都选当前分数最高的那一个 token，不做任何随机抽样。

## 3. 本讲源码地图

本讲只聚焦一个文件：

| 文件 | 作用 |
| --- | --- |
| [`ch04/01_main-chapter-code/gpt.py`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py) | 自包含汇总脚本，把第 2~4 章代码聚到单文件。本讲的主角 `generate_text_simple` 与运行入口 `main()` 都在这里，可直接 `python gpt.py` 运行。 |

补充说明：

- `generate_text_simple` 依赖的 `GPTModel`（u4-l3）、`TransformerBlock`（u4-l2）、`MultiHeadAttention`（u3-3，含因果掩码）也都定义在同一个 `gpt.py` 里，本讲把它们当作已知零件引用。
- 进阶的随机解码（温度、top-k）在第 5 章 `ch05/01_main-chapter-code/gpt_generate.py` 的 `generate` 函数中，本讲暂不展开，留作 u5-l3。

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：自回归循环 → 上下文裁剪与最后一步 logits → argmax 贪心采样与序列拼接 → 运行入口 `main()`。前三个模块合起来就是 `generate_text_simple` 的逐行精读，第四个模块讲怎么把它跑起来。

### 4.1 自回归生成的基本范式

#### 4.1.1 概念说明

训练时，语言模型一次前向就能得到**所有位置**的预测（这种技巧叫 teacher forcing，详见 u2-l3）。但生成时不行——生成时新 token 是模型**自己产出的、训练数据里根本不存在**，没法提前知道。所以只能老老实实地：

> 把已有序列喂进去 → 看最后一个位置的预测 → 把它当成「真实」的下一个 token 拼回序列末尾 → 再喂进去 → 再看最后一个位置……

每一步都用「截至目前的所有 token」去预测「下一个」，再把预测结果并入上下文。这个「滚雪球」式的循环就叫**自回归生成**。它正是 ChatGPT 那种逐字「打字机效果」背后的机制。

一个直观的图示（起始上下文是 `"Hello, I am"`，要生成 2 个新 token）：

```text
第 0 步输入:  [Hello,  I,      am]      → 模型 → 取末位预测 → 得到 token_1
第 1 步输入:  [Hello,  I,      am,  token_1]  → 模型 → 取末位预测 → 得到 token_2
停止:        达到 max_new_tokens
最终输出:    [Hello,  I,      am,  token_1, token_2]
```

注意每一步的输入序列都在变长，但模型始终**只关心最后一个位置的预测**。

#### 4.1.2 核心流程

`generate_text_simple` 的骨架是一个循环，每一轮做四件事：裁剪上下文 → 前向 → 取末位 logits → 选词并拼接。这一节先看「循环」本身：

```text
输入:  model, idx (B, T) 当前已有序列, max_new_tokens 要生成多少个, context_size 上限
循环 max_new_tokens 次:
    ① idx_cond   = 取 idx 的最后 context_size 个 token          # 4.2 讲
    ② logits     = model(idx_cond)                              # 4.2 讲
    ③ logits     = logits 的最后一个时间步                        # 4.2 讲
    ④ idx_next   = argmax(logits)                               # 4.3 讲
    ⑤ idx        = cat(idx, idx_next)                           # 4.3 讲
返回 idx (B, T + max_new_tokens)
```

关键性质：

- 循环次数固定为 `max_new_tokens`，**不会提前停止**（没有结束符 `<|endoftext|>` 检测，那是 u5-l3 的 `generate` 才有的）。
- 每一步的输入长度 `T` 都在增长（`T → T+1 → T+2 → …`），但因为有第①步的裁剪，模型实际「看到」的有效窗口不会超过 `context_size`。

#### 4.1.3 源码精读

整个函数定义在 [`gpt.py:210-233`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L210-L233)，循环部分如下（先把裁剪和采样的细节用注释概括，后续模块再逐行展开）：

```python
def generate_text_simple(model, idx, max_new_tokens, context_size):
    # idx is (B, T) array of indices in the current context
    for _ in range(max_new_tokens):
        idx_cond = idx[:, -context_size:]      # ① 裁剪（见 4.2）
        with torch.no_grad():                  # ② 前向（见 4.2）
            logits = model(idx_cond)
        logits = logits[:, -1, :]              # ③ 取最后一步（见 4.2）
        idx_next = torch.argmax(logits, dim=-1, keepdim=True)  # ④ 贪心采样（见 4.3）
        idx = torch.cat((idx, idx_next), dim=1)                # ⑤ 拼接（见 4.3）
    return idx
```

- `idx` 的初始形状是 `(B, T)`，即 `batch × 当前 token 数`，注释 `# idx is (B, T) array of indices` 写明了这一点（[`gpt.py:211`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L211)）。
- `for _ in range(max_new_tokens)`（[`gpt.py:212`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L212)）：循环变量用不到，故命名为 `_`；每轮新增一个 token。
- 循环结束后返回的 `idx` 形状变成 `(B, T + max_new_tokens)`，把起始上下文和新 token 全拼在一起。

#### 4.1.4 代码实践

**目标**：不依赖真实模型，先用一个「假模型」手动模拟自回归循环，看清 `idx` 是如何一步步变长的。

操作步骤（示例代码，非项目原有代码）：

1. 复制下面这段最小骨架到本地 Python 环境（不需要 PyTorch，纯列表模拟）。
2. 运行，观察每一步 `idx` 的长度变化。

```python
# 示例代码：用列表模拟自回归滚雪球过程
def fake_next_token(context):
    # 假装模型：永远返回一个固定的新 token（用当前长度当占位）
    return f"<tok{len(context)}>"

def generate_demo(idx, max_new_tokens):
    for step in range(max_new_tokens):
        nxt = fake_next_token(idx)        # 对应 ④：预测下一个 token
        idx = idx + [nxt]                 # 对应 ⑤：拼回序列
        print(f"第 {step} 步后 idx = {idx}")
    return idx

generate_demo(["Hello", "I", "am"], max_new_tokens=3)
```

需要观察的现象与预期结果：

- 每一步 `idx` 长度 `+1`，且新 token 被追加在末尾。
- 最终序列长度 = 初始长度 + `max_new_tokens`。
- 这一步不需要 GPU，也不需要安装任何库，纯逻辑验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么不把训练时的「一次前向得到所有位置预测」直接搬到生成里，一次就预测出全部新 token？

> **参考答案**：因为训练时每个位置的真实「下一个 token」来自数据，已知；生成时新 token 由模型自己产生，第 `t+1` 步的输入依赖第 `t` 步的输出，必须串行滚雪球，不能提前知道。

**练习 2**：循环里如果把 `max_new_tokens` 设得很大（比如 10000），最坏会发生什么？

> **参考答案**：序列会无限变长，内存和时间线性增长；且因为没有结束符检测，即使语义早已讲完也会一直生成下去，极易陷入重复循环（详见 4.3）。`generate_text_simple` 没有任何上限保护，调用者需自己控制 `max_new_tokens`。

### 4.2 上下文裁剪与取最后一步 logits

#### 4.2.1 概念说明

这一模块讲循环内部的第 ①②③ 步。有两处看似多余、实则关键的操作：

**为什么要裁剪上下文（`idx[:, -context_size:]`）？**

GPT 的位置嵌入 `pos_emb = nn.Embedding(context_length, emb_dim)` 只有 `context_length` 行，前向时用 `torch.arange(seq_len)` 去查表（详见 u4-l3 的 `GPTModel.forward` 与 u2-l4）。一旦 `seq_len > context_length`，`arange` 就会越界报错。所以当已生成的序列超过模型支持的上限时，**只保留最后 `context_size` 个 token** 喂给模型。这是一种「滑动窗口」式的截断：丢掉最早的内容，保住最近的一段。

**为什么要取最后一步 logits（`logits[:, -1, :]`）？**

模型前向输出 `(B, T, V)`，即序列里**每个位置**都有一个对「它下一个 token」的预测。但生成时我们只需要「整个序列末尾之后的下一个词」。由于因果掩码（u3-l2）保证最后一个位置已经看到了 `≤ T` 的全部 token，所以**最后一个位置的预测**就是我们要的。前面位置的预测是它们各自时刻的「中间产物」，对追加新 token 没用，故丢弃。

把这两步连起来理解：模型其实并不需要记住全部历史，它只需要「当前上下文窗口」里**最末位**的预测。

#### 4.2.2 核心流程

设当前 `idx` 形状为 `(B, T)`、词表大小为 `V`：

```text
① 裁剪:    idx_cond = idx[:, -context_size:]        # (B, min(T, context_size))
② 前向:    logits   = model(idx_cond)               # (B, T_cond, V)
③ 取末位:  logits   = logits[:, -1, :]              # (B, V)
```

形状变化一图看清：

| 步骤 | 张量 | 形状 |
| --- | --- | --- |
| 裁剪前 | `idx` | `(B, T)` |
| 裁剪后 | `idx_cond` | `(B, T_cond)`，其中 `T_cond = min(T, context_size)` |
| 前向后 | `logits` | `(B, T_cond, V)` |
| 取末位后 | `logits` | `(B, V)` |

注意第③步把中间那个「token 数」维度整个压掉了，从三维降到二维，只留每个 batch 一行、`V` 列的「末位分数向量」。

#### 4.2.3 源码精读

三行关键代码都在 `generate_text_simple` 内部：

- 裁剪上下文（[`gpt.py:217`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L217)）：

  ```python
  # Crop current context if it exceeds the supported context size
  # E.g., if LLM supports only 5 tokens, and the context size is 10
  # then only the last 5 tokens are used as context
  idx_cond = idx[:, -context_size:]
  ```

  这里 `idx[:, -context_size:]` 取第二维（token 维）的最后 `context_size` 个，batch 维 `:` 全保留。源码注释举了个直观例子：模型只支持 5 个 token、当前有 10 个，那就只用最后 5 个。

- 前向得到 logits（[`gpt.py:220-221`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L220-L221)）：

  ```python
  # Get the predictions
  with torch.no_grad():
      logits = model(idx_cond)
  ```

  `model(idx_cond)` 进入 [`GPTModel.forward`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L198-L207)，最终由 `logits = self.out_head(x)`（[`gpt.py:206`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L206)）输出 `(B, T_cond, V)`。
  `with torch.no_grad()` 关掉自动求导：生成只是推理、不需要梯度，既省内存又提速（详见 u8-l1 的 autograd 复习）。

- 取最后一步（[`gpt.py:225`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L225)）：

  ```python
  # Focus only on the last time step
  # (batch, n_token, vocab_size) becomes (batch, vocab_size)
  logits = logits[:, -1, :]
  ```

  `[:, -1, :]` 即「所有 batch、最后一个 token、全部词表」，注释明确指出形状从 `(batch, n_token, vocab_size)` 变成 `(batch, vocab_size)`。

#### 4.2.4 代码实践

**目标**：亲手触发一次「裁剪」，确认当序列超过 `context_size` 时模型只看到最后一段。

操作步骤（示例代码，需要 PyTorch 与本章的 `GPTModel`）：

1. 在 `ch04/01_main-chapter-code/` 目录里 `import` 本文件的 `GPTModel`、`GPT_CONFIG_124M`（或直接照抄）。
2. 构造一个长度**故意超过** `context_size` 的 token 序列，把 `context_size` 设小（比如 4），打印裁剪前后长度。

```python
# 示例代码：观察上下文裁剪
import torch
# 假设已从 gpt.py 导入 GPTModel, GPT_CONFIG_124M（或照抄配置）
# from gpt import GPTModel, GPT_CONFIG_124M

idx = torch.randint(0, 50257, (1, 10))   # batch=1, 序列长 10
context_size = 4
idx_cond = idx[:, -context_size:]
print("裁剪前 idx 长度:", idx.shape[1])   # 预期 10
print("裁剪后 idx_cond 长度:", idx_cond.shape[1])  # 预期 4
print("取到的正是最后 4 个 token:", torch.equal(idx_cond, idx[:, -4:]))  # 预期 True
```

需要观察的现象与预期结果：

- 裁剪后长度恒为 `context_size`（当原序列更长时），取到的是末尾那一段。
- 把 `context_size` 设为 1024（模型真实上限）时，对于短输入不会有任何截断（`T_cond = T`）。

> 若不接入真实 `GPTModel`，仅验证裁剪切片逻辑无需 GPU，「待本地验证」的是接入模型后前向不报错。

#### 4.2.5 小练习与答案

**练习 1**：如果删掉 `idx_cond = idx[:, -context_size:]` 这行、直接 `model(idx)`，会发生什么？

> **参考答案**：当 `idx` 长度 ≤ `context_length` 时没事；一旦超过 `context_length`（GPT-2 small 是 1024），`GPTModel.forward` 里的 `pos_emb(torch.arange(seq_len))` 会因为索引越界报错。裁剪正是为了避免这个。

**练习 2**：为什么用 `with torch.no_grad()` 包住前向？去掉它会怎样？

> **参考答案**：生成是纯推理，不需要反向传播。`no_grad()` 关闭 autograd 的计算图构建，省下大量中间激活的内存、也更快。去掉它不会让结果出错，但会白白记录梯度信息、浪费内存（详见 u8-l1）。

**练习 3**：`logits[:, -1, :]` 改成 `logits[:, 0, :]`（取第一个位置）会怎样？

> **参考答案**：那样取到的是**第一个位置**对「它下一个 token」的预测，只看了序列开头那点信息，几乎与末尾的完整上下文无关，生成的 token 会很不合理。生成必须取**最后一个**位置，因为它在因果掩码下看到了全部上下文。

### 4.3 argmax 贪心采样与序列拼接

#### 4.3.1 概念说明

第 ③ 步拿到末位 logits `(B, V)` 后，要从词表里挑出「下一个 token」。`generate_text_simple` 用的挑法是最朴素的 **argmax 贪心**：在 `V` 个分数里直接取最大的那一个。

用数学写出来就是：

\[
\text{next\_token} \;=\; \arg\max_{i \in \{0,\dots,V-1\}} \; \text{logits}[:, -1, :]\,[i]
\]

因为 softmax 是单调的（不改变大小顺序），所以**对 logits 取 argmax 等价于对概率取 argmax**——这里其实不必先做 softmax，省了一步。

贪心解码的特点：

- **确定性**：相同模型 + 相同输入，永远产出完全相同的输出。这让它适合做对照实验。
- **缺乏多样性**：每次回答都一样，不会「换个说法」。
- **易陷入重复循环**：一旦模型陷入「我说→我说→我说…」这类局部最优，贪心会无限重复同一个片段，因为没有随机性帮它跳出去（这也是为什么 u5-l3 要引入温度与 top-k 随机采样）。

#### 4.3.2 核心流程

```text
④ 贪心采样:  idx_next = argmax(logits, dim=-1, keepdim=True)   # (B, 1)
⑤ 拼接:     idx      = cat((idx, idx_next), dim=1)            # (B, T+1)
```

| 步骤 | 张量 | 形状 |
| --- | --- | --- |
| 采样前 | `logits` | `(B, V)` |
| argmax 后 | `idx_next` | `(B, 1)` |
| 拼接前 | `idx` | `(B, T)` |
| 拼接后 | `idx` | `(B, T+1)` |

两个细节：

- `dim=-1` 表示在**最后一维（词表维）**取最大值的索引。
- `keepdim=True` 保留那一维（长度为 1），让 `idx_next` 形状为 `(B, 1)` 而非 `(B,)`。这样它才能和 `idx (B, T)` 沿 `dim=1`（token 维）正确拼接。

#### 4.3.3 源码精读

- 贪心采样（[`gpt.py:228`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L228)）：

  ```python
  # Get the idx of the vocab entry with the highest logits value
  idx_next = torch.argmax(logits, dim=-1, keepdim=True)  # (batch, 1)
  ```

  注释说明取「logits 值最高的那个词表项」，形状 `(batch, 1)`。

- 拼接到运行序列（[`gpt.py:231`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L231)）：

  ```python
  # Append sampled index to the running sequence
  idx = torch.cat((idx, idx_next), dim=1)  # (batch, n_tokens+1)
  ```

  `torch.cat(..., dim=1)` 沿 token 维把新 token 接到末尾，形状从 `(B, T)` 变 `(B, T+1)`，正是下一轮循环的输入。

- 循环结束后（[`gpt.py:233`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L233)）`return idx` 返回包含起始上下文与全部新 token 的完整序列。

> 把 4.1~4.3 串起来，`generate_text_simple` 的完整逻辑就是：**固定循环 N 次，每次裁剪 → 前向 → 取末位 → argmax → 拼接**，朴素但完整地实现了贪心自回归生成。

#### 4.3.4 代码实践

**目标**：验证 argmax 贪心的「确定性」与「重复倾向」。

操作步骤（示例代码）：

1. 构造一个固定的 logits 向量，重复调用 argmax，确认结果不变。
2. 再观察：把 logits 稍微扰动，argmax 结果是否仍稳定。

```python
# 示例代码：贪心的确定性与稳定性
import torch
torch.manual_seed(0)
logits = torch.tensor([[1.0, 5.0, 2.0, 4.0, 0.5]])   # (1, 5) 当作词表大小为 5

for run in range(3):
    nxt = torch.argmax(logits, dim=-1, keepdim=True)
    print(f"第 {run} 次 argmax → {nxt.item()}")      # 预期每次都是 1（5.0 最大）

# 给最大项一个微小扰动，看 argmax 是否翻转
logits2 = logits.clone()
logits2[0, 1] -= 1.5                                 # 5.0 → 3.5，不再是最大
print("扰动后 argmax →", torch.argmax(logits2, dim=-1).item())  # 预期翻到 3（4.0）
```

需要观察的现象与预期结果：

- 同一 logits，三次 argmax 结果完全相同（确定性）。
- 当某项分数被压低到不再是最大，argmax 立即翻转——说明贪心只认「当前最大」，没有任何随机性或「第二名候选」的概念。

> 「未训练模型用 `generate_text_simple` 容易生成重复片段」这一现象，本质就是贪心在随机权重下反复命中某些高分 token。要让它产生多样、不重复的文本，需要 u5-l3 的随机采样。

#### 4.3.5 小练习与答案

**练习 1**：argmax 为什么不必先做 `softmax`？

> **参考答案**：softmax 是单调函数，不改变元素大小顺序，所以「logits 最大」和「softmax 后概率最大」指向同一个 token。直接对 logits 取 argmax 省一次 softmax 计算，结果等价。

**练习 2**：把 `keepdim=True` 改成 `keepdim=False`，`torch.cat` 还能正常工作吗？

> **参考答案**：不能。`keepdim=False` 时 `idx_next` 形状为 `(B,)`（一维），而 `idx` 是 `(B, T)`（二维），沿 `dim=1` 拼接会因为维度数不一致报错。`keepdim=True` 保住那一维让它变成 `(B, 1)`，才能正确拼到 `(B, T)` 上。

**练习 3**：为什么贪心解码容易生成「重复循环」的文本？

> **参考答案**：贪心只取当前最高分，没有任何随机性。一旦模型在某个状态下反复预测同一个高频 token，就会自我强化、陷入循环（如 "I am, I am, I am"）。引入温度/ top-k 采样（u5-l3）能从候选里随机抽样，打破这种循环。

### 4.4 运行入口 main()：完整生成流程

#### 4.4.1 概念说明

`generate_text_simple` 只接受**token ID 张量**，但读者手上往往只有一段**字符串**。`main()` 函数就是把「人类可读文本」和「模型生成」这两端接起来的胶水：它把起始字符串编码成 token ID、补上 batch 维、调用生成函数、再把生成结果解码回文本。

这里要理解三个小概念：

- **`model.eval()`（推理模式）**：关闭 `nn.Dropout`。训练时 dropout 随机置零一部分激活来正则化；生成（推理）时必须关掉，否则每次前向都有随机性、输出不稳定。（详见 u1-l2 与 u4-l2 的 `drop_shortcut`。）
- **`unsqueeze(0)` 补 batch 维**：`generate_text_simple` 要求 `idx` 是 `(B, T)`，但 `tokenizer.encode` 返回的是一维列表，转成 tensor 后是 `(T,)`。`unsqueeze(0)` 在最前面加一维，变成 `(1, T)`，即「batch 大小为 1」。
- **`squeeze(0)` 去 batch 维**：生成完成后 `out` 是 `(1, T+N)`，解码前用 `squeeze(0).tolist()` 把它压回一维列表，交给 `tokenizer.decode`。

#### 4.4.2 核心流程

`main()` 的步骤一览：

```text
① 定义 GPT_CONFIG_124M           # 配方卡（u4-l3）
② torch.manual_seed(123)         # 固定随机初始化，让权重可复现
③ model = GPTModel(cfg)          # 构建未训练的 124M 模型
④ model.eval()                   # 关闭 dropout（推理模式）
⑤ start_context = "Hello, I am"  # 起始上下文
⑥ tokenizer.encode(...)          # 字符串 → token ID 列表
⑦ tensor(...).unsqueeze(0)       # 补 batch 维 → (1, T)
⑧ generate_text_simple(...)      # 自回归生成 max_new_tokens 个
⑨ tokenizer.decode(out.squeeze(0).tolist())  # 解码回文本
```

注意 `main()` 里**没有** `.to(device)` 调用，整个脚本故意只跑在 CPU 上（详见 u1-l2）。设备选择（CPU/GPU）的范式要到第 5 章训练才正式登场。

#### 4.4.3 源码精读

`main()` 定义在 [`gpt.py:236-277`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L236-L277)，关键片段：

- 配置与建模型（[`gpt.py:237-249`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L237-L249)）：

  ```python
  GPT_CONFIG_124M = { "vocab_size": 50257, "context_length": 1024, ... }  # 配方卡（见 u4-l3）
  torch.manual_seed(123)
  model = GPTModel(GPT_CONFIG_124M)
  model.eval()  # disable dropout
  ```

  `manual_seed(123)` 固定权重随机初始化，使不同机器跑出的输出可复现；`model.eval()` 关闭 dropout。

- 编码起始上下文（[`gpt.py:251-255`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L251-L255)）：

  ```python
  start_context = "Hello, I am"
  tokenizer = tiktoken.get_encoding("gpt2")
  encoded = tokenizer.encode(start_context)
  encoded_tensor = torch.tensor(encoded).unsqueeze(0)
  ```

  用 GPT-2 BPE 分词器（u2-l2）把字符串编码成 token ID 列表，再 `unsqueeze(0)` 补成 `(1, T)`。

- 调用生成（[`gpt.py:262-267`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L262-L267)）：

  ```python
  out = generate_text_simple(
      model=model,
      idx=encoded_tensor,
      max_new_tokens=10,
      context_size=GPT_CONFIG_124M["context_length"]
  )
  ```

  生成 10 个新 token，`context_size` 取自配置（1024）。

- 解码回文本（[`gpt.py:268`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L268)）：

  ```python
  decoded_text = tokenizer.decode(out.squeeze(0).tolist())
  ```

  `out` 形状 `(1, T+10)`，`squeeze(0)` 去掉 batch 维后 `tolist()` 转成 Python 列表，交给 `tokenizer.decode` 还原成字符串。

#### 4.4.4 代码实践

**目标**：跑通 `python gpt.py`，亲眼看到未训练模型的生成输出，并思考「为什么像乱码」。

操作步骤：

1. 按 u1-l2 安装依赖（最少需要 `torch` 与 `tiktoken`）。
2. 进入目录并运行：

   ```bash
   cd ch04/01_main-chapter-code
   python gpt.py
   ```

3. 记录脚本打印的 `Input text`、`encoded_tensor.shape`、`Output text`。
4. 修改 `start_context`（如改成 `"Every effort moves you"`）与 `max_new_tokens`（如改成 20），重跑，观察输出变化。

需要观察的现象与预期结果：

- `encoded_tensor.shape` 应为 `(1, 4)`（`"Hello, I am"` 经 GPT-2 分词是 4 个 token）。
- `Output length` 应为初始长度 + `max_new_tokens`（即 4 + 10 = 14）。
- **`Output text` 看起来像乱码**：可能出现一些「像单词」的片段或反复重复的 token，但整体语义不通。这是**未训练**模型的正常表现——权重是随机初始化的，模型还没学到任何语言规律。

为什么未训练输出像乱码？因为 `GPTModel` 刚 `manual_seed(123)` 随机初始化，`out_head` 给每个词的分数几乎是随机的；argmax 只是「在随机分数里挑最大的」，自然拼不出有意义的话。等 u5 训练完、加载真实权重后（u5-l4），同样的 `"Hello, I am"` 就能接出连贯的句子。

> 具体生成的 token 文本因权重初始化细节而异，精确输出「待本地验证」；但「像乱码 / 易重复」这一现象是确定的，可对照确认。

#### 4.4.5 小练习与答案

**练习 1**：去掉 `model.eval()` 这一行，生成结果会出错吗？会有什么不同？

> **参考答案**：不会报错，但 dropout 在前向时仍生效，每次生成的输出会带随机性、不可复现。`eval()` 关闭 dropout 保证推理确定性。本脚本用了 `manual_seed` 又用了 `eval()`，正是为了输出可复现。

**练习 2**：把 `unsqueeze(0)` 去掉，直接传一维的 `encoded_tensor` 给 `generate_text_simple`，会发生什么？

> **参考答案**：`idx` 会是一维 `(T,)`，函数里 `idx[:, -context_size:]`、`idx[:, -1, :]` 这些按「第二维」切片的操作会报错或行为异常。`generate_text_simple` 契约要求 `(B, T)` 二维输入，`unsqueeze(0)` 就是补这个 batch 维。

**练习 3**：为什么 `main()` 里没有 `.to(device)`？这是缺陷还是有意为之？

> **参考答案**：有意为之。本脚本定位为「最小可运行演示」，故意只跑 CPU、不引入设备选择，降低上手门槛（详见 u1-l2）。GPU/`torch.device(...)` 的范式到第 5 章训练才正式引入。

## 5. 综合实践

**任务**：完整跟踪一次「字符串 → token → 生成 → 解码」的链路，并通过**故意缩小 `context_size`** 来亲手触发上下文裁剪，验证模型确实只看最后一段。

操作步骤：

1. 运行 `python gpt.py`，确认默认输出（未训练乱码）能正常产生，记录 `Output text`。
2. 在本地复制一份 `main()`，把调用改成手动传入一个很小的 `context_size`，并准备一段长起始文本（示例代码）：

   ```python
   # 示例代码：缩小 context_size 触发裁剪（需 gpt.py 同目录导入）
   import tiktoken, torch
   # from gpt import GPTModel, GPT_CONFIG_124M, generate_text_simple

   tokenizer = tiktoken.get_encoding("gpt2")
   start_context = "Hello, I am a language model and I can generate text step by step."
   ids = torch.tensor(tokenizer.encode(start_context)).unsqueeze(0)  # (1, T)，T 较大

   # 把 context_size 故意设小，强制每一步只看最后几个 token
   out = generate_text_simple(model=model, idx=ids, max_new_tokens=5, context_size=4)
   print(tokenizer.decode(out.squeeze(0).tolist()))
   ```

3. 对照 4.2.3 的裁剪逻辑，说明：当 `T > 4` 后，每一步模型实际「看到」的只有最后 4 个 token，最早的内容被丢弃。
4. 思考并写下：训练完成后（u5 / u5-l4），同样的起始上下文用 `context_size=1024` 生成，预期会得到什么？为什么现在的输出仍是乱码？

需要观察的现象与预期结果：

- 缩小 `context_size` 后脚本**不报错**，证明裁剪逻辑在生效（若不裁剪，长序列会让 `pos_emb` 越界）。
- 输出依旧像乱码——因为模型未训练，裁剪只影响「看多少」，不影响「会不会说话」。
- 能用自己的话讲清：自回归循环每一步做「裁剪 → 前向 → 取末位 → argmax → 拼接」这五件事。

> 本实践把全讲的四个最小模块串起来：自回归循环（4.1）、裁剪与末位 logits（4.2）、argmax 拼接（4.3）、入口胶水（4.4）。做完后你应当能在不看代码的情况下复述 `generate_text_simple` 的完整执行过程。

## 6. 本讲小结

- `generate_text_simple` 用一个**自回归循环**实现文本生成：固定循环 `max_new_tokens` 次，每轮「裁剪上下文 → 前向 → 取末位 logits → argmax → 拼回序列」。
- **上下文裁剪** `idx[:, -context_size:]` 防止序列超过位置嵌入 `context_length` 上限而越界，只保留最近一段；**取末位** `logits[:, -1, :]` 因为因果掩码下最后一个位置看到了全部上下文。
- **argmax 贪心采样** `torch.argmax(logits, dim=-1, keepdim=True)` 直接取最高分 token，确定性强但缺乏多样性、易重复；`keepdim=True` 保证能和 `(B, T)` 沿 `dim=1` 拼接。
- `main()` 是把「字符串 ↔ token」接起来的胶水：`encode` → `unsqueeze(0)` 补 batch 维 → 生成 → `squeeze(0).tolist()` → `decode`，并用 `model.eval()` 关闭 dropout 保证推理可复现。
- 未训练模型（`manual_seed(123)` 随机初始化）的生成**看起来像乱码**是正常的——argmax 只是在随机分数里挑最大值；要得到连贯文本需先训练（u5）或加载 OpenAI 权重（u5-l4）。

## 7. 下一步学习建议

- 现在模型能「生成」了，但输出是乱码。下一讲进入 **u5-l1：生成损失与模型评估**，讲解如何用**交叉熵损失 / 困惑度**量化「模型有多不会说话」，为训练提供优化目标。
- 想让生成不再死板、能产生多样文本，预习 **u5-l3：解码策略（温度与 top-k 采样）**，那里用 `ch05/.../gpt_generate.py` 的 `generate` 函数替代本讲的贪心 argmax。
- 想要「直接得到能说话的模型」，跳到 **u5-l4：权重保存/加载与加载 OpenAI GPT-2 权重**，把 OpenAI 预训练权重映射进 `GPTModel` 后，同样的 `generate_text_simple` 立刻输出连贯英文。
- 想深入理解推理效率，回顾本讲的裁剪思想——它将在 **u9-l1：KV Cache 加速推理** 中被进一步优化（缓存历史 K/V，避免对前文重复前向）。

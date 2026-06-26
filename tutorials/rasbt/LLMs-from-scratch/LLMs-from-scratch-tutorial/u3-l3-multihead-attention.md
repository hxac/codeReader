# 多头注意力实现

## 1. 本讲目标

本讲在「自注意力」与「因果注意力」的基础上，把**单头**注意力升级成**多头**注意力（Multi-Head Attention），它是第 4 章 `GPTModel` 真正使用的注意力实现。学完本讲你应当能够：

- 说清楚为什么要把注意力拆成多个「头」并行计算，以及每个头各自学到什么。
- 读懂用 `view` + `transpose` 两个张量操作在**同一份** Q/K/V 上「无拷贝切出」多个头的技巧，并能写出每一步的张量形状。
- 解释 `out_proj` 输出投影与多头整合的作用，理解为什么合并前要调用 `.contiguous()`。
- 完整读懂 `MultiHeadAttention` 类的 `__init__` 与 `forward`，并独立跑通一次前向、打印中间张量形状。

## 2. 前置知识

本讲承接 **u3-l1（自注意力原理）** 与 **u3-l2（因果注意力与掩码）**，并消费 **u2-l4（Token/位置嵌入）** 的输出。先用通俗语言回顾要点：

- **自注意力**：对序列中每个位置，用一个上下文向量来表示它；该向量是所有位置的 value 的加权求和，权重由 query·key 的相似度决定。
- **缩放点积注意力公式**：注意力权重由 \(QK^{\top}\) 经 softmax 得到，并除以 \(\sqrt{d_k}\) 防止内积过大导致 softmax 饱和，即

  \[
  \mathrm{Attention}(Q,K,V)=\mathrm{softmax}\!\left(\frac{QK^{\top}}{\sqrt{d_k}}\right)V
  \]

- **因果掩码**（u3-l2）：用上三角矩阵把「未来 token」对应的注意力分数置为 \(-\infty\)，softmax 后变 0，保证自回归训练时不偷看未来。
- **register_buffer**（u3-l2）：把不可学习的常量（如掩码）注册进模块，使其随 `.to(device)` 迁移、但不被优化器更新。
- **输入张量形状**：来自 u2-l4，注意力层的输入是 token 嵌入 + 位置嵌入之和，形状为 `(batch, num_tokens, emb_dim)`。

本讲引入的新术语：**头（head）**、**head_dim**（每个头的维度）、**并行计算**、**out_proj（输出投影）**、**contiguous（内存连续）**。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [ch03/01_main-chapter-code/multihead-attention.ipynb](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch03/01_main-chapter-code/multihead-attention.ipynb) | 教学型 notebook，给出两种实现：**Variant A**（`MultiHeadAttentionWrapper`，把多个独立的 `CausalSelfAttention` 串起来拼接，直观但低效）与 **Variant B**（`MultiHeadAttention`，用张量重排一次并行算出所有头，即本项目最终采用的写法） |
| [ch04/01_main-chapter-code/previous_chapters.py](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/previous_chapters.py) | 第 4 章汇总文件，把 Variant B 的 `MultiHeadAttention` 作为「成品」收录（L49–L102），供后续 `GPTModel` 直接复用 |

> 说明：notebook 中的 Variant B 与 `previous_chapters.py` 中的实现几乎逐行一致。本讲**精读以 `previous_chapters.py` 为准**（它有稳定的行号），notebook 则用来展示 Variant A 这一过渡写法。notebook 是 Jupyter 渲染视图，下文涉及 notebook 时只给文件级链接，精确行号请对照同结构的 `.py` 汇总文件。

## 4. 核心概念与源码讲解

### 4.1 多头拆分：为什么要多个头

#### 4.1.1 概念说明

单个注意力头一次只能学到**一种**「关注模式」。但语言中一个词与上下文的关系是多方面的：它可能因为语法结构关注主语、因为语义关注同义词、因为共现关注修饰语……多头注意力（Multi-Head Attention）让模型同时维护**多套**关注模式，各自独立算完后再拼到一起。

数学上，多头把输出维度 \(d_{out}\) 均分成 \(h\) 份（\(h\) = `num_heads`），每个头分到

\[
d_{\text{head}} = \frac{d_{out}}{h}
\]

每个头独立做一次缩放点积注意力，最后把 \(h\) 个头的输出沿特征维拼接，再过一个线性层 \(W^O\) 混合：

\[
\mathrm{head}_i = \mathrm{Attention}(QW_i^Q,\; KW_i^K,\; VW_i^V)
\]

\[
\mathrm{MultiHead}(Q,K,V) = \mathrm{Concat}(\mathrm{head}_1,\dots,\mathrm{head}_h)\,W^O
\]

实现这条思路有两条路：

- **Variant A（包装器，仅用于教学）**：每个头是一个独立的 `CausalSelfAttention` 实例，各自拥有自己的 \(W_Q/W_K/W_V\)；用 Python `for` 循环逐个算完，再用 `torch.cat` 拼接。直观、好理解，但逐头串行、效率低。
- **Variant B（一体化，本项目实际使用）**：只用**一份**大的 \(W_Q/W_K/W_V\)（`d_in → d_out`），一次性算出所有头的 Q/K/V，再通过**张量重排**（下一节）逻辑地切成多个头、并行计算。

关键直觉：**多头的本质是「把特征维切分后并行」**，而不是「写多个独立的注意力模块」。Variant B 用一次大矩阵乘法 + 张量重排，等价于 Variant A 的多模块串联，但更快。

#### 4.1.2 核心流程

1. 决定头数 `num_heads` 与每头维度 `head_dim = d_out // num_heads`。
2. 校验 `d_out` 能被 `num_heads` 整除（否则无法均分）。
3. Variant A：建 `num_heads` 个独立头 → 循环前向 → `cat` 拼接 → `out_proj`。
4. Variant B：建一份大 `W_Q/W_K/W_V` → 一次前向得到全部头的 Q/K/V → 张量重排切头 → 并行算注意力 → 拼回 → `out_proj`。

#### 4.1.3 源码精读

**Variant A 包装器**（教学过渡写法，来自 notebook）：把 `num_heads` 个 `CausalSelfAttention` 装进 `nn.ModuleList`，前向时 `for head in self.heads` 逐个跑、沿最后一维拼接，最后过 `out_proj`：

[ch03/01_main-chapter-code/multihead-attention.ipynb](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch03/01_main-chapter-code/multihead-attention.ipynb)（其中 `MultiHeadAttentionWrapper` 类 + `CausalSelfAttention` 类所在单元格）

```python
class MultiHeadAttentionWrapper(nn.Module):
    def __init__(self, d_in, d_out, context_length, dropout, num_heads, qkv_bias=False):
        super().__init__()
        self.heads = nn.ModuleList(
            [CausalSelfAttention(d_in, d_out, context_length, dropout, qkv_bias)
             for _ in range(num_heads)])
        self.out_proj = nn.Linear(d_out*num_heads, d_out*num_heads)

    def forward(self, x):
        context_vec = torch.cat([head(x) for head in self.heads], dim=-1)
        return self.out_proj(context_vec)
```

注意 Variant A 里 `d_out` 取的是 `d_in // num_heads`（每头输出维度），拼接后变回 `d_in`；而 Variant B（下文）里 `d_out` 直接等于 `d_in`，靠内部切分实现等价效果。

**Variant B 的头维度与整除校验**（本项目实际使用的「成品」）：

[ch04/01_main-chapter-code/previous_chapters.py:52-56](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/previous_chapters.py#L52-L56) —— 断言 `d_out` 必须能被 `num_heads` 整除，并算出 `head_dim`：

```python
        assert d_out % num_heads == 0, "d_out must be divisible by num_heads"
        self.d_out = d_out
        self.num_heads = num_heads
        self.head_dim = d_out // num_heads  # Reduce the projection dim to match desired output dim
```

#### 4.1.4 代码实践

**实践目标**：验证「改变头数 `num_heads` 既不改变输出形状、也不改变参数量」——多头拆分只是改变了关注模式的结构，不改输出维度。

**操作步骤**（示例代码，需与 `previous_chapters.py` 同目录运行，或把该目录加入 `sys.path`）：

```python
# 示例代码
import torch
from previous_chapters import MultiHeadAttention

torch.manual_seed(123)
d_in, d_out, context_length = 256, 256, 4
x = torch.rand(8, 4, d_in)   # (batch, num_tokens, emb_dim)

for h in [1, 2, 4, 8]:
    mha = MultiHeadAttention(d_in, d_out, context_length, dropout=0.0, num_heads=h)
    n_params = sum(p.numel() for p in mha.parameters())
    print(f"num_heads={h:>2}  out.shape={tuple(mha(x).shape)}  params={n_params}")
```

**需要观察的现象**：四组配置的输出形状都应是 `(8, 4, 256)`，参数量也应**完全相同**。

**预期结果**：输出维度恒为 `d_out=256`，参数量恒为 `3*(d_in*d_out) + (d_out*d_out) = 3*65536 + 65536 = 262144`（三个 QKV 投影 + 一个 out_proj，均与 `num_heads` 无关）。

#### 4.1.5 小练习与答案

**练习 1**：若把 `d_out=256`、`num_heads=10` 一起传入，会发生什么？为什么？
**参考答案**：`assert d_out % num_heads == 0` 会触发 `AssertionError`，因为 256 不能被 10 整除，无法把特征维均分给每个头。

**练习 2**：Variant A 包装器与 Variant B 一体化实现，在 `num_heads=2, d_in=256` 时谁的参数量更大？
**参考答案**：二者**参数量相同**（都是 262144 左右，含 out_proj）。区别不在参数量，而在效率：Variant B 用一次大矩阵乘法并行算所有头，Variant A 用 Python 循环逐头串行，所以 Variant B 更快。

---

### 4.2 view/transpose 重排：用一份 Q/K/V 切出多个头

#### 4.2.1 概念说明

Variant B 最关键、也最容易卡住初学者的点，是：**如何不为每个头单独算，而是在同一份 Q/K/V 上「切出」多个头并行？** 答案是两个零拷贝的张量操作 `view` 与 `transpose`。

- `view(b, num_tokens, num_heads, head_dim)`：把最后一维 `d_out`「展开」成 `(num_heads, head_dim)` 两维。因为 `d_out = num_heads * head_dim`，这只是一个**形状重解释**，不复制数据。展开后，倒数第二维就是「头编号」。
- `transpose(1, 2)`：把 `num_heads` 维从第 1 维换到 batch 维之后，让形状变成 `(b, num_heads, num_tokens, head_dim)`。这样每个头在维度上变成一个独立的「切片」，后续的批量矩阵乘法 `@` 会对 batch 和每个头**同时并行**计算。

这套做法的好处：用**一次** `W_Q @ x` 算出所有头的数据，再用纯张量操作排成可并行形状，避免了 Python 循环。

#### 4.2.2 核心流程

设 `b=8, num_tokens=4, d_in=d_out=256, num_heads=2`，则 `head_dim=128`。Q/K/V 在切头前后的形状变化如下：

| 阶段 | 张量形状 | 说明 |
| --- | --- | --- |
| 输入 `x` | `(8, 4, 256)` | batch × token × emb_dim |
| `W_key(x)` 等投影后 | `(8, 4, 256)` | 即 `(b, num_tokens, d_out)`，所有头的数据混在一起 |
| `view(b, num_tokens, num_heads, head_dim)` | `(8, 4, 2, 128)` | 把 256 展开成 (2, 128)，倒数第二维变成头编号 |
| `transpose(1, 2)` | `(8, 2, 4, 128)` | 即 `(b, num_heads, num_tokens, head_dim)`，头变成独立切片 |

#### 4.2.3 源码精读

[ch04/01_main-chapter-code/previous_chapters.py:68-81](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/previous_chapters.py#L68-L81) —— 一次投影算出全部 Q/K/V，再用 `view` + `transpose` 切头：

```python
        keys = self.W_key(x)  # Shape: (b, num_tokens, d_out)
        queries = self.W_query(x)
        values = self.W_value(x)

        # We implicitly split the matrix by adding a `num_heads` dimension
        # Unroll last dim: (b, num_tokens, d_out) -> (b, num_tokens, num_heads, head_dim)
        keys = keys.view(b, num_tokens, self.num_heads, self.head_dim)
        values = values.view(b, num_tokens, self.num_heads, self.head_dim)
        queries = queries.view(b, num_tokens, self.num_heads, self.head_dim)

        # Transpose: (b, num_tokens, num_heads, head_dim) -> (b, num_heads, num_tokens, head_dim)
        keys = keys.transpose(1, 2)
        queries = queries.transpose(1, 2)
        values = values.transpose(1, 2)
```

注释里的 `Implicitly split the matrix` 就是「隐式切头」的核心思想：不真的拆矩阵，只靠重塑形状让多头自然出现。

#### 4.2.4 代码实践

**实践目标**：用一个随机张量手动重放 `view` + `transpose`，亲眼看到形状如何从 `(b, num_tokens, d_out)` 变成 `(b, num_heads, num_tokens, head_dim)`。

**操作步骤**（示例代码，可独立运行）：

```python
# 示例代码
import torch
b, num_tokens, d_out, num_heads = 8, 4, 256, 2
head_dim = d_out // num_heads

keys = torch.rand(b, num_tokens, d_out)              # 模拟 W_key(x) 的输出
print("投影后        :", tuple(keys.shape))          # (8, 4, 256)

keys = keys.view(b, num_tokens, num_heads, head_dim)
print("view 切头后   :", tuple(keys.shape))          # (8, 4, 2, 128)

keys = keys.transpose(1, 2)
print("transpose 后 :", tuple(keys.shape))           # (8, 2, 4, 128)
```

**需要观察的现象**：三步形状依次为 `(8,4,256)` → `(8,4,2,128)` → `(8,2,4,128)`。

**预期结果**：与上表完全一致；其中 `transpose(1,2)` 之后，第 1 维变成了 `num_heads=2`，说明每个头已是一个独立切片。

#### 4.2.5 小练习与答案

**练习 1**：为什么这里能用 `view` 而不用担心数据顺序错乱？
**参考答案**：因为 `d_out = num_heads * head_dim`，`view(b, num_tokens, num_heads, head_dim)` 只是把连续的 `d_out` 个元素按 `(num_heads, head_dim)` 重新分组，属于纯形状重解释、不搬运数据；只要投影输出的布局与切分方式对应，逻辑上每个头就分到了连续的 `head_dim` 个特征。

**练习 2**：如果不做 `transpose(1, 2)`，直接用 `(b, num_tokens, num_heads, head_dim)` 去做 `queries @ keys.transpose(2,3)`，会发生什么？
**参考答案**：矩阵乘法会在错误的维度上配对，无法实现「每个头内部、token 之间」的注意力计算，得到的注意力分数形状与含义都不对。`transpose(1,2)` 的目的正是把 `num_heads` 提到前面，让批量乘法把 batch 和每个头都当作独立维度并行处理。

---

### 4.3 out_proj 投影与多头整合

#### 4.3.1 概念说明

各头并行算完注意力后，得到的形状是 `(b, num_heads, num_tokens, head_dim)`。要喂给下一层，需要两步收尾：

1. **多头拼回**：先 `transpose(1, 2)` 把 `num_heads` 维换回到 token 维之后，变成 `(b, num_tokens, num_heads, head_dim)`；再 `view(b, num_tokens, d_out)` 把 `num_heads * head_dim` 合并回 `d_out`。这一步正是 4.2 切头操作的**逆过程**。
2. **输出投影 `out_proj`**：拼接后的向量再过一个线性层 `nn.Linear(d_out, d_out)`，让不同头的输出有机会互相混合、重新组合特征。这个 `out_proj` 就是公式里的 \(W^O\)。

这里有个容易踩的坑：`transpose` 之后的张量在内存中**不再连续**（non-contiguous），而 `view` 要求底层内存连续。所以源码在 `view` 之前显式调用了 `.contiguous()` 复制出一份连续的内存再重塑（也可以改用不要求连续的 `reshape`，效果相同）。

#### 4.3.2 核心流程

延续 4.2 的形状（注意力权重 `attn_weights` 形状为 `(8, 2, 4, 4)`，`values` 为 `(8, 2, 4, 128)`）：

| 阶段 | 张量形状 | 说明 |
| --- | --- | --- |
| `attn_weights @ values` | `(8, 2, 4, 128)` | 各头各自的上下文向量 |
| `.transpose(1, 2)` | `(8, 4, 2, 128)` | 把 `num_heads` 换回 token 维之后 |
| `.contiguous().view(b, num_tokens, d_out)` | `(8, 4, 256)` | 合并多头，恢复 `d_out` |
| `out_proj(...)` | `(8, 4, 256)` | 输出投影混合各头 |

#### 4.3.3 源码精读

[ch04/01_main-chapter-code/previous_chapters.py:96-100](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/previous_chapters.py#L96-L100) —— 多头拼回 + 输出投影：

```python
        # Shape: (b, num_tokens, num_heads, head_dim)
        context_vec = (attn_weights @ values).transpose(1, 2)

        # Combine heads, where self.d_out = self.num_heads * self.head_dim
        context_vec = context_vec.contiguous().view(b, num_tokens, self.d_out)
        context_vec = self.out_proj(context_vec)  # optional projection
```

注意 `self.d_out = self.num_heads * self.head_dim` 这个恒等式，是「切分」与「合并」能严丝合缝对上的数学保证。

#### 4.3.4 代码实践

**实践目标**：直观感受「`transpose` 后张量不再连续，`view` 会报错，必须先 `.contiguous()`」。

**操作步骤**（示例代码）：

```python
# 示例代码
import torch
t = torch.rand(8, 4, 2, 128)
t2 = t.transpose(1, 2)                       # (8, 2, 4, 128)，内存不连续
print("is_contiguous:", t2.is_contiguous())  # 预期 False

try:
    t2.view(8, 4, 256)                        # 不连续直接 view 会报错
except RuntimeError as e:
    print("直接 view 报错:", type(e).__name__)

print("contiguous 后 view:", tuple(t2.contiguous().view(8, 4, 256).shape))  # 预期 (8, 4, 256)
```

**需要观察的现象**：`is_contiguous` 为 `False`；直接 `view` 抛出 `RuntimeError`；先 `.contiguous()` 再 `view` 成功得到 `(8, 4, 256)`。

**预期结果**：与上述一致。这正是源码里必须写 `.contiguous().view(...)` 的原因。

#### 4.3.5 小练习与答案

**练习 1**：把 `out_proj` 去掉（直接返回 `context_vec`），模型还能跑吗？为什么源码还要保留它？
**参考答案**：能跑，去掉 `out_proj` 在数学上仍是一次合法的多头注意力（拼接即结束）。保留 `out_proj` 是为了让各头输出经过一次线性混合、增加表达力；代码注释也标了 `optional projection`。

**练习 2**：把 `.contiguous()` 改成 `.reshape(b, num_tokens, self.d_out)` 还能正常工作吗？
**参考答案**：能。`reshape` 在张量不连续时会自动按需复制，等价于 `contiguous().view(...)`，因此可以替换。源码选择显式 `contiguous().view(...)` 是为了让「先连续化、再重塑」的意图更清晰。

---

### 4.4 完整 MultiHeadAttention 类

#### 4.4.1 概念说明

本模块把前三个模块串成一个完整的 `MultiHeadAttention` 类。它把 `__init__` 里建好的「一份大投影 + out_proj + 掩码」，和 `forward` 里的「投影 → 切头 → 并行注意力 → 拼回 → 输出投影」连成一条流水线。这个类是第 4 章 `GPTModel` 唯一使用的注意力实现，也是 `previous_chapters.py` 收录的「成品」。

有两个细节值得强调：

- **缩放因子是 `head_dim`，不是 `d_out`**：源码里写的是 `keys.shape[-1]**0.5`，而此时 `keys` 已经 `transpose` 过，其最后一维正是 `head_dim`。这与缩放点积公式里除以 \(\sqrt{d_k}\)（\(d_k\) 为每个头的维度）一致。
- **掩码在 4 维上广播**：`mask_bool` 形状是 `(num_tokens, num_tokens)`（2D），而 `attn_scores` 是 `(b, num_heads, num_tokens, num_tokens)`（4D）；`masked_fill_` 会把 2D 掩码广播到每个 batch、每个头上，因果掩码对每个头一视同仁（与 u3-l2 完全相同的技巧，只是多了一个头维度）。

#### 4.4.2 核心流程

完整 `forward` 的伪代码：

```
输入 x: (b, num_tokens, d_in)
1. keys/queries/values = W_*(x)                 # (b, num_tokens, d_out)
2. view 成 (b, num_tokens, num_heads, head_dim)
3. transpose(1,2) 成 (b, num_heads, num_tokens, head_dim)
4. attn_scores = queries @ keys.transpose(2,3)  # (b, num_heads, num_tokens, num_tokens)
5. 因果掩码 masked_fill_(-inf)
6. attn_weights = softmax(attn_scores / sqrt(head_dim))  # 用 head_dim 缩放
7. dropout
8. context_vec = attn_weights @ values          # (b, num_heads, num_tokens, head_dim)
9. transpose(1,2) + contiguous + view           # (b, num_tokens, d_out)
10. out_proj                                     # (b, num_tokens, d_out)
返回 context_vec
```

#### 4.4.3 源码精读

**`__init__`：一份大投影 + out_proj + 掩码**

[ch04/01_main-chapter-code/previous_chapters.py:49-63](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/previous_chapters.py#L49-L63) —— 建立大 `W_query/W_key/W_value`、`out_proj`、`dropout`，并用 `register_buffer` 注册因果掩码（与 u3-l2 一致）：

```python
class MultiHeadAttention(nn.Module):
    def __init__(self, d_in, d_out, context_length, dropout, num_heads, qkv_bias=False):
        super().__init__()
        assert d_out % num_heads == 0, "d_out must be divisible by num_heads"
        self.d_out = d_out
        self.num_heads = num_heads
        self.head_dim = d_out // num_heads
        self.W_query = nn.Linear(d_in, d_out, bias=qkv_bias)
        self.W_key = nn.Linear(d_in, d_out, bias=qkv_bias)
        self.W_value = nn.Linear(d_in, d_out, bias=qkv_bias)
        self.out_proj = nn.Linear(d_out, d_out)  # Linear layer to combine head outputs
        self.dropout = nn.Dropout(dropout)
        self.register_buffer("mask", torch.triu(torch.ones(context_length, context_length), diagonal=1))
```

**`forward`：切头 → 并行注意力（含因果掩码）→ 拼回**

[ch04/01_main-chapter-code/previous_chapters.py:84-93](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/previous_chapters.py#L84-L93) —— 对每个头并行算注意力分数、施加因果掩码、softmax（用 `head_dim` 缩放）、dropout：

```python
        attn_scores = queries @ keys.transpose(2, 3)  # Dot product for each head
        mask_bool = self.mask.bool()[:num_tokens, :num_tokens]
        attn_scores.masked_fill_(mask_bool, -torch.inf)
        attn_weights = torch.softmax(attn_scores / keys.shape[-1]**0.5, dim=-1)
        attn_weights = self.dropout(attn_weights)
```

`keys.shape[-1]` 在 `transpose` 后等于 `head_dim`，所以这里的缩放正是除以 \(\sqrt{d_{\text{head}}}\)。`mask_bool` 是 2D，对 4D 的 `attn_scores` 广播，对每个头施加同样的因果掩码。

**完整 `forward` 的全景**（投影与切头见 4.2，拼回与输出投影见 4.3）：

[ch04/01_main-chapter-code/previous_chapters.py:65-102](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/previous_chapters.py#L65-L102) —— 把上述各步按顺序串成完整的 `forward`，返回 `(b, num_tokens, d_out)` 的上下文向量。

#### 4.4.4 代码实践（主实践）

**实践目标**：实例化 `MultiHeadAttention` 跑一次前向，**复现 `forward` 内部各阶段的张量形状**，验证 queries/keys/values 与最终 `context_vec` 的维度，确认多头切分与拼回是互逆的（输入输出同为 `(b, num_tokens, d_out)`）。

**操作步骤**（示例代码；需在 `ch04/01_main-chapter-code/` 目录下运行，或把该目录加入 `sys.path`）：

```python
# 示例代码：复现 forward 各阶段形状，不修改源码
import torch
from previous_chapters import MultiHeadAttention

torch.manual_seed(123)
d_in, d_out, context_length, num_heads = 256, 256, 4, 2
mha = MultiHeadAttention(d_in, d_out, context_length, dropout=0.0, num_heads=num_heads)

x = torch.rand(8, 4, d_in)                # (batch, num_tokens, emb_dim)
b, num_tokens, _ = x.shape

# 1) 一次投影得到全部头的 Q/K/V
queries = mha.W_query(x); keys = mha.W_key(x); values = mha.W_value(x)
print("queries/keys/values:", tuple(keys.shape))      # 预期 (8, 4, 256)

# 2) 切头 + 转置
q = queries.view(b, num_tokens, num_heads, mha.head_dim).transpose(1, 2)
k = keys.view(b, num_tokens, num_heads, mha.head_dim).transpose(1, 2)
v = values.view(b, num_tokens, num_heads, mha.head_dim).transpose(1, 2)
print("切头并转置后      :", tuple(k.shape))          # 预期 (8, 2, 4, 128)

# 3) 每个头内部算注意力分数
attn_scores = q @ k.transpose(2, 3)
print("注意力分数矩阵    :", tuple(attn_scores.shape)) # 预期 (8, 2, 4, 4)

# 4) 最终输出（直接调用类）
print("最终 context_vec  :", tuple(mha(x).shape))      # 预期 (8, 4, 256)
```

**需要观察的现象**：四个形状依次为 `(8,4,256)` → `(8,2,4,128)` → `(8,2,4,4)` → `(8,4,256)`。

**预期结果**：最终输出与输入形状完全一致（都是 `(8, 4, 256)`），说明「切头」与「拼回」互逆，多头注意力在接口上与单头注意力兼容——这也是它能在 `GPTModel` 里直接替换单头注意力的原因。

#### 4.4.5 小练习与答案

**练习 1**：把 `num_heads` 设为 1，这个类退化为哪种结构？
**参考答案**：退化为带因果掩码的单头缩放点积注意力（即 u3-l1/u3-l2 的单头版本），`head_dim` 此刻等于 `d_out`，切头与拼回都不改变形状，但仍然保留了 `out_proj`。

**练习 2**：源码里缩放用的是 `keys.shape[-1]**0.5`，如果误写成 `d_out**0.5`，对 `num_heads=2` 的配置会有什么影响？
**参考答案**：会**错误地**按 \(\sqrt{d_{out}}=\sqrt{256}\) 而非 \(\sqrt{d_{\text{head}}}=\sqrt{128}\) 缩放，使注意力分数被多除了一个 \(\sqrt{2}\)，softmax 分布会更平、注意力更模糊，偏离标准多头注意力公式。

---

## 5. 综合实践

**任务**：把本讲四个模块串起来，画一张 `MultiHeadAttention` 的「形状数据流图」，并用代码验证它。

**操作步骤**：

1. 在 `ch04/01_main-chapter-code/` 下运行 4.4.4 的示例代码，记录每一步形状。
2. 把这些形状填进一张表格：输入 `(8,4,256)` → 投影 → `view` → `transpose` → 注意力分数 → 掩码/softmax → `attn@values` → `transpose` → `contiguous().view` → `out_proj` → 输出 `(8,4,256)`。
3. 额外验证两个性质：
   - **因果性**：取 `num_tokens=4` 时，打印第 0 个 token 对应的那一行注意力权重（在 softmax 之后、dropout=0 下），应只有第 0 列非零，其余为 0（因为它看不到未来 token）。
   - **接口兼容**：对比单头注意力（`num_heads=1`）与多头（`num_heads=4`）的输出形状，确认都是 `(8,4,256)`，即多头不改变对外的张量接口。

```python
# 示例代码：验证因果性（dropout=0，softmax 后看第 0 行）
import torch
from previous_chapters import MultiHeadAttention
torch.manual_seed(0)
mha = MultiHeadAttention(256, 256, context_length=4, dropout=0.0, num_heads=2)
x = torch.rand(1, 4, 256)
# 复现到 attn_weights（含掩码、softmax）
b, n, _ = x.shape
q = mha.W_query(x).view(b,n,mha.num_heads,mha.head_dim).transpose(1,2)
k = mha.W_key(x).view(b,n,mha.num_heads,mha.head_dim).transpose(1,2)
scores = q @ k.transpose(2,3)
scores.masked_fill_(mha.mask.bool()[:n,:n], -torch.inf)
w = torch.softmax(scores / mha.head_dim**0.5, dim=-1)
print("head0, token0 的注意力权重:", w[0,0,0].tolist())  # 预期只有第 0 个非零
```

**预期结果**：数据流图能自洽地把形状从 `(8,4,256)` 走回 `(8,4,256)`；因果性验证中第 0 行只有第 0 列非零。若本地无 GPU，上述全部可在 CPU 上跑通；若运行结果与预期不符，请标注「待本地验证」并核对 PyTorch 与 `previous_chapters.py` 版本。

## 6. 本讲小结

- 多头注意力的本质是**把输出特征维 `d_out` 均分成 `num_heads` 份**，让模型同时维护多套关注模式，最后拼接 + 投影。
- 本项目用 **Variant B（一体化）**实现：只用一份大 `W_Q/W_K/V`（`d_in→d_out`），再用 `view` + `transpose` 隐式切头，比 Variant A 的逐头循环更高效。
- `view(b, num_tokens, num_heads, head_dim)` 把特征维展开成「头编号 + 头内维度」，`transpose(1,2)` 把头提到前面，使批量矩阵乘法对每个头并行计算。
- 缩放因子是 **`head_dim`**（`keys.shape[-1]` 在 transpose 后即为每头维度），不是 `d_out`。
- 因果掩码沿 `num_heads` 维**广播**，对每个头一视同仁，复用了 u3-l2 的 `register_buffer` + `masked_fill_` 技巧。
- 多头切分与拼回**互逆**，输出形状恒为 `(b, num_tokens, d_out)`，参数量与 `num_heads` 无关——这让它在 `GPTModel` 中可直接替换单头注意力。

## 7. 下一步学习建议

- **紧接着学 u4-l1（核心组件 LayerNorm/GELU/FeedForward）与 u4-l2（TransformerBlock）**：本讲的 `MultiHeadAttention` 会被组装进 `TransformerBlock`，与残差连接、前馈网络一起构成 GPT 的基本单元，这是最直接的下一步。
- **若对注意力实现的高效性感兴趣**，可提前浏览 u9：包括 KV Cache（u9-l1）、用 `torch.nn.functional.scaled_dot_product_attention` 的高效多头实现（u9-l2），以及 GQA/MLA/SWA 等现代注意力变体（u9-l3）——它们都建立在本讲的 `MultiHeadAttention` 之上。
- **建议动手**：把 4.4.4 的形状追踪脚本保存下来，在学 u4-l2 时用同样的方法追踪 `TransformerBlock` 的数据流，巩固「先看形状、再看数值」的源码阅读习惯。

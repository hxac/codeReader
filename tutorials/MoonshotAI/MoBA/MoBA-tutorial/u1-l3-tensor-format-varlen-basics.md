# 读懂源码前的必备知识：张量布局、varlen 与 flash-attn

## 1. 本讲目标

MoBA 的源码里没有多少「算法代码」之外的东西，但有大量**张量形状变换和索引操作**。如果不在读源码之前把三种底层约定搞清楚，后面读 `moba_efficient.py` 时会立刻迷失在 `permute`、`cu_seqlens`、`repeat_interleave` 之中。

本讲结束时，你应该能够：

1. 看到任意一个张量，立刻说出它处于哪种布局（HF 布局还是 flash-attn 布局），并能手写函数在两种布局之间转换。
2. 给定一个 varlen 批次（若干条不等长序列），正确构造 `cu_seqlens` 与 `max_seqlen`，并说清楚这两个量分别被谁使用。
3. 解释 GQA 中 `q_heads` 与 `kv_heads` 的关系，以及 `moba_layer` 如何在 prefill 与 decode 两个阶段之间分路。

本讲是纯「基础设施」讲义：不涉及 MoBA 算法本身，但后面的每一讲都建立在这些约定之上。

## 2. 前置知识

### 2.1 形状记号

本手册统一使用如下记号描述张量形状：

| 记号 | 含义 |
| --- | --- |
| `B` | batch大小（批次里有多少条序列） |
| `Hq` / `Hk` | query 头数 / KV 头数（GQA 下 `Hk < Hq`） |
| `S` | 打包后的总 token 数（varlen 维度的长度） |
| `Sq` / `Sk` | query 序列长度 / KV 序列长度 |
| `D` | 每个注意力头的维度（head_dim） |

### 2.2 注意力的三种「世界」

同一个注意力计算，在不同库里有三种不同的张量表示习惯：

1. **HuggingFace transformers 的世界**：注意力接口收到的是 `[B, H, S, D]`（batch 在前、头在中间、序列在后）。这是 `transformers` 的 `ALL_ATTENTION_FUNCTIONS` 注册机制约定的输入格式，MoBA 作为注册进去的自定义注意力必须遵守。
2. **flash-attn 的世界**：内核要求 `[total_seqlen, H, D]`，即把所有 batch 的 token 拼成一条「长序列」，序列维在第 0 维。变长序列靠 `cu_seqlens` 描述边界。
3. **MoBA 内核的世界**：`moba_attn_varlen` 与 `moba_attn_varlen_naive` 都直接采用 flash-attn 的 varlen 布局（见 [moba/moba_naive.py:19-23](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L19-L23) 的 docstring：`q (torch.Tensor): [seqlen, head, head_dim]`，且 `cu_seqlens` 与 flash-attn 同定义）。

`moba/wrapper.py` 存在的全部意义，就是把世界 1 的张量翻译成世界 3 的张量。本讲 4.1 节讲布局翻译，4.2 节讲 varlen 边界描述，4.3 节把翻译过程串成完整流程。

### 2.3 基础注意力公式回顾

标准缩放点积注意力：

\[ \mathrm{Attention}(Q, K, V) = \mathrm{softmax}\!\left(\frac{QK^\top}{\sqrt{D}}\right)V \]

本讲不关心 softmax 细节，只需知道：Q 的每个位置（query）要与 K/V 的若干位置（key/value）做交互，因此**序列维度的组织方式**决定了计算的边界——这正是 varlen 要解决的问题。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `moba/wrapper.py` | 把 HF 注意力接口适配到 MoBA varlen 实现 | `hf_to_fa`、`fa_to_hf`、`moba_layer` 的 prefill/decode 分支与 GQA 扩展 |
| `tests/test_moba_attn.py` | naive 与 efficient 实现的正确性对齐测试 | `generate_data` 如何构造 varlen 批次 |
| `moba/moba_naive.py` | 教学参考实现 | 仅看 docstring 与按 `cu_seqlens` 切片的用法，验证 varlen 约定的下游消费方式 |
| `moba/__init__.py` | `register_moba` 注册入口 | 确认 wrapper 在项目中的位置（上一讲已讲，此处仅作定位） |

## 4. 核心概念与源码讲解

### 4.1 两种内存布局与互转：hf_to_fa / fa_to_hf

#### 4.1.1 概念说明

HF 布局把 batch、heads、seqlen、head_dim 四个维度按 `[B, H, S, D]` 排列；flash-attn 布局则是 `[B*S, H, D]`——batch 维与序列维合并成一个「打包序列」维度，且**序列维在第 0 维**。

为什么 flash-attn 要这样设计？因为它要高效处理**不等长序列**：如果 batch 里各序列长度不同，`[B, S, ...]` 这种形状必须靠 padding 补齐到统一长度，浪费计算；而把所有 token 拼成一条长序列（`[S_total, H, D]`）再用 `cu_seqlens` 标记边界，就完全没有 padding 开销。

注意一个重要限制：`hf_to_fa` 的输入是 dense 的 `[B, H, S, D]`，它只能处理**等长**批次（或者已 padding 的批次）。真正不等长的数据从诞生起就该以 flash-attn 布局组织——测试文件 `generate_data` 正是这么做的（见 4.4 节）。

#### 4.1.2 核心流程

`hf_to_fa` 分两步：

```text
输入: [B, H, S, D]
  ├─ permute(0, 2, 1, 3)  → [B, S, H, D]   # 交换 heads 与 seqlen 两个轴
  └─ reshape(-1, H, D)    → [B*S, H, D]    # 合并 batch 与 seqlen（行优先）
输出: [B*S, H, D]
```

合并的顺序是关键：`reshape` 按**行优先**展开，所以新维度上的 token 顺序是「第 0 条序列的全部 token，接着第 1 条序列的全部 token，……」，即打包后位置 `p` 对应：

\[ p = b \cdot S + s \quad (b \text{ 为 batch 下标},\ s \text{ 为序列内位置}) \]

这个顺序必须与 `cu_seqlens` 描述的边界一致（见 4.2 节），两者是配套约定。

`fa_to_hf` 是严格逆过程：`view(B, S, H, D)` 拆开合并维，再 `permute` 把轴序换回去。

#### 4.1.3 源码精读

正转函数（[moba/wrapper.py:7-15](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/wrapper.py#L7-L15)）：

```python
def hf_to_fa(x: torch.Tensor):
    # x: [batch, heads, seqlen, head_dim] -> [batch * seqlen, heads, head_dim]
    return x.permute(0, 2, 1, 3).reshape(-1, x.shape[1], x.shape[3])
```

docstring 明确标注了输入 `[batch, heads, seqlen, head_dim]`、输出 `[batch * seqlen, heads, head_dim]`。`permute` 只改变轴序、返回非连续视图；随后的 `reshape` 因内存不连续会触发一次实际的数据拷贝，得到连续的 flash-attn 布局张量。

反转函数（[moba/wrapper.py:18-26](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/wrapper.py#L18-L26)）：

```python
def fa_to_hf(x: torch.Tensor, batch: int):
    # [batch * seqlen, heads, head_dim] -> [batch, heads, seqlen, head_dim]
    return x.view(batch, -1, x.shape[1], x.shape[2]).permute(0, 2, 1, 3)
```

`view` 要求输入连续（`hf_to_fa` 的输出恰好连续，所以可直接 `view`），`-1` 由 PyTorch 自动推成 `S`。

一个值得注意的事实：`fa_to_hf` 在当前代码里**并没有被真正调用**——[moba/wrapper.py:83](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/wrapper.py#L83) 中对它的调用被注释掉了：

```python
    # out = fa_to_hf(out, batch)
    return out, None
```

原因留到 4.3.3 末尾解释（提示：输出布局恰好与 HF 期望一致）。这里先记住：`fa_to_hf` 目前是作为 `hf_to_fa` 的「逆运算工具」存在的，理解它有助于验证布局转换的正确性。

#### 4.1.4 代码实践

> **注意**：不要直接 `import moba.wrapper` 来做本实践——该文件第 3 行 `from flash_attn import flash_attn_func`，没有安装 flash-attn（需要 GPU 环境）时 import 就会失败。本实践把两个函数**复制**到独立脚本中，纯 CPU 即可运行。

1. **实践目标**：亲手完成 `[2, 4, 10, 128]` 的 HF 布局到 flash-attn 布局的转换，并验证两次转换互为逆运算。

2. **操作步骤**：新建 `layout_practice.py`（示例代码，非项目原有文件）：

   ```python
   import torch

   def hf_to_fa(x):  # 复制自 moba/wrapper.py:7-15
       return x.permute(0, 2, 1, 3).reshape(-1, x.shape[1], x.shape[3])

   def fa_to_hf(x, batch):  # 复制自 moba/wrapper.py:18-26
       return x.view(batch, -1, x.shape[1], x.shape[2]).permute(0, 2, 1, 3)

   torch.manual_seed(0)
   x = torch.randn(2, 4, 10, 128)          # B=2, H=4, S=10, D=128
   y = hf_to_fa(x)
   print("fa layout:", y.shape)             # 期望 [20, 4, 128]
   print("contiguous:", y.is_contiguous())
   x_back = fa_to_hf(y, batch=2)
   print("round-trip equal:", torch.equal(x, x_back))
   # 定位检查：HF 布局 (b=1, h=2, s=3) 应等于 fa 布局 (p=1*10+3=13, h=2)
   print("element check:", torch.equal(x[1, 2, 3], y[13, 2]))
   ```

3. **需要观察的现象**：`y.shape` 变为 `[20, 4, 128]`；`y.is_contiguous()` 为 `True`（说明 `reshape` 做了真实拷贝，而 `x.permute(0,2,1,3)` 的中间结果是非连续的）；往返后 `torch.equal` 为 `True`。

4. **预期结果**：`fa layout: torch.Size([20, 4, 128])`、`round-trip equal: True`、`element check: True`。若把 `x[1, 2, 3]` 换成 `y[12, 2]` 或 `y[14, 2]` 对比则应为 `False`——这能帮你确认打包顺序确实是 \( p = b \cdot S + s \)。

#### 4.1.5 小练习与答案

**练习 1**：能否不写 `permute`，直接对 `[B, H, S, D]` 执行 `reshape(-1, H, D)` 完成转换？

**答案**：不能。`reshape` 不改变元素的内存顺序，只重新切分维度。`[B, H, S, D]` 的内存里 batch 0 的数据按「头优先」排列（h0 的全部 S 个 token，再 h1 的……），直接合并前两维得到的第 0 维是「batch × heads」的混合维，既不是 `[B*S, H, D]` 的形状语义，元素排列也完全错乱。必须先用 `permute` 把轴序调成 `[B, S, H, D]` 再合并。

**练习 2**：`hf_to_fa` 之后紧接着 `fa_to_hf`，得到的张量与原张量数值上什么关系？内存上什么关系？

**答案**：数值完全相同（`permute`/`reshape`/`view` 都不改变元素值），可用 `torch.equal` 验证；但不与原张量共享存储——`permute` 后的非连续张量迫使 `reshape` 拷贝出一份新的连续内存。

**练习 3**：`fa_to_hf` 里用的是 `view` 而不是 `reshape`，两者在这里可以互换吗？

**答案**：可以，但仅当输入连续。`hf_to_fa` 的输出经过 `reshape` 后是连续的，所以 `view` 能成功；`view` 比 `reshape` 更严格（不满足连续条件直接报错），这里用它反而能隐式断言「输入确实是 `hf_to_fa` 产出的连续张量」。

### 4.2 varlen 打包约定：cu_seqlens 与 max_seqlen

#### 4.2.1 概念说明

**varlen（variable length，变长）** 是 flash-attn 处理不等长批次的方式：把 batch 里 \( B \) 条长度分别为 \( L_1, L_2, \dots, L_B \) 的序列首尾相接拼成一条长 \( S = \sum_i L_i \) 的「打包序列」，再用一个前缀和张量 `cu_seqlens` 标记每条序列的起止边界。

约定如下（与 flash-attn 完全一致，[moba/moba_naive.py:22-23](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L22-L23) 的 docstring 明确写着 "same definition in flash attn"）：

\[ \text{cu\_seqlens} = \left[0,\ L_1,\ L_1+L_2,\ \dots,\ \sum_{i=1}^{B} L_i\right] \]

它是一个长度为 \( B+1 \)、**单调递增**、首元素为 0 的 `int32` 一维张量。由此：

- 第 \( i \) 条序列（下标从 0 开始）的长度：\( L_i = \text{cu\_seqlens}[i+1] - \text{cu\_seqlens}[i] \)；
- 第 \( i \) 条序列占据打包维度的区间：\( [\text{cu\_seqlens}[i],\ \text{cu\_seqlens}[i+1]) \)；
- 打包张量第 0 维大小必须等于 \( \text{cu\_seqlens}[-1] \)。

**`max_seqlen`** 是另一个标量参数：批次中最长单条序列的长度

\[ \text{max\_seqlen} = \max_{0 \le i < B} L_i \]

它**不参与索引计算**，只被 flash-attn 内核用来决定启动配置（线程块数量、共享内存大小等）。换句话说：`cu_seqlens` 决定「算哪些 token 之间的注意力」（正确性），`max_seqlen` 决定「内核按多大规模启动」（性能）。两者测试里都由 [tests/test_moba_attn.py:31-32](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_attn.py#L31-L32) 的一行代码关联起来：`max_seqlen = torch.amax(cu_seqlen[1:] - cu_seqlen[:-1])`。

#### 4.2.2 核心流程

消费 `cu_seqlens` 的标准模式（naive 实现就是一个逐 batch 的显式循环）：

```text
for batch_idx in range(len(cu_seqlens) - 1):
    start = cu_seqlens[batch_idx]        # 本条序列在打包维度上的起点
    end   = cu_seqlens[batch_idx + 1]    # 终点（开区间）
    q_, k_, v_ = q[start:end], k[start:end], v[start:end]
    # 对这一条序列独立做因果注意力，结果写回 o[start:end]
```

高效实现则把同样的信息向量化使用，例如由 `cu_seqlens` 直接算出每条序列的长度（[moba/moba_efficient.py:19](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L19)）：

```python
batch_sizes = cu_seqlen[1:] - cu_seqlen[:-1]
```

#### 4.2.3 源码精读

先看 wrapper 里 `cu_seqlens` 的**生产端**（[moba/wrapper.py:62-66](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/wrapper.py#L62-L66)）：

```python
cu_seqlens_k = torch.cumsum(
    torch.tensor([0] + [kv_len] * batch, device=query.device),
    dim=0,
    dtype=torch.int32,
)
```

这段代码为 dense 的 HF 批次构造 varlen 元数据：`[0, kv_len] + [kv_len] * batch` 做 cumsum 后得到 `[0, kv_len, 2*kv_len, ..., batch*kv_len]`。由于 HF 传来的批次每条序列等长（都是 `kv_len`），这是一个**等长 varlen**——格式上完全兼容 varlen 约定，只是恰好每段一样长。这解释了 4.1 节的配合关系：`hf_to_fa` 的打包顺序 \( p = b \cdot S + s \) 恰好落在这套边界的切割点上。注意 `dtype=torch.int32`，这是 flash-attn 内核的硬性要求（测试里同样用 int32，见 [tests/test_moba_attn.py:29](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_attn.py#L29)）。

再看**消费端**（[moba/moba_naive.py:30-41](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L30-L41)）：

```python
batch = cu_seqlens.numel() - 1
...
for batch_idx in range(batch):
    batch_start = cu_seqlens[batch_idx].item()
    batch_end = cu_seqlens[batch_idx + 1].item()
    q_ = q[batch_start:batch_end]
    k_ = k[batch_start:batch_end]
    v_ = v[batch_start:batch_end]
```

注意第一行：**batch 数量是从 `cu_seqlens` 的长度反推出来的**（`numel() - 1`）。varlen 格式下不存在独立的 batch 维度，`cu_seqlens` 是唯一的边界信息来源——这也是为什么后续讲义里你会反复看到「形状推维度、cu_seqlens 推 batch」的套路。

#### 4.2.4 代码实践

1. **实践目标**：为 batch=2、序列长度分别为 6 和 10 的 varlen 批次手工构造 `cu_seqlens` 与 `max_seqlen`，并模拟「打包—按边界还原」的完整过程。

2. **操作步骤**（示例代码，纯 CPU 可运行）：

   ```python
   import torch

   H, D = 4, 128
   seq_a = torch.randn(6, H, D)    # 第 0 条序列，长 6
   seq_b = torch.randn(10, H, D)   # 第 1 条序列，长 10

   # 手工构造 varlen 元数据
   lengths = [6, 10]
   cu_seqlens = torch.tensor([0] + list(torch.tensor(lengths).cumsum(0)),
                             dtype=torch.int32)
   max_seqlen = max(lengths)

   # 打包
   packed = torch.cat([seq_a, seq_b], dim=0)
   print("packed:", packed.shape)               # 期望 [16, 4, 128]
   print("cu_seqlens:", cu_seqlens)             # 期望 [0, 6, 16]
   print("max_seqlen:", max_seqlen)             # 期望 10

   # 按 cu_seqlens 边界还原，验证无损
   assert torch.equal(packed[cu_seqlens[0]:cu_seqlens[1]], seq_a)
   assert torch.equal(packed[cu_seqlens[1]:cu_seqlens[2]], seq_b)
   print("restore ok")
   ```

3. **需要观察的现象**：`cu_seqlens` 打印为 `tensor([ 0,  6, 16], dtype=torch.int32)`；`packed` 形状为 `[16, 4, 128]`；两条断言通过。

4. **预期结果**：如上。进一步可以把 `lengths` 改成 `[4, 7, 5]`（三条序列）再跑一遍，检查你能否先在纸上写出 `cu_seqlens = [0, 4, 11, 16]`、`max_seqlen = 7`，再与程序输出对照。

#### 4.2.5 小练习与答案

**练习 1**：batch=3、长度分别为 `[4, 7, 5]` 的批次，`cu_seqlens`、`max_seqlen`、打包张量第 0 维大小各是多少？

**答案**：`cu_seqlens = [0, 4, 11, 16]`；`max_seqlen = 7`（最长单条序列，不是总长）；打包张量第 0 维 = `cu_seqlens[-1]` = 16。

**练习 2**：为什么 `max_seqlen` 不直接等于 `cu_seqlens[-1]`？两者分别被谁使用？

**答案**：`cu_seqlens[-1]` 是全部序列的总 token 数（打包维的实际长度）；`max_seqlen` 是最长**单条**序列的长度。前者是数据规模，后者是内核启动参数——flash-attn 用它确定单个序列处理单元的规模上限，取大了浪费资源、取小于实际长度则行为错误。

**练习 3**：如果 `cu_seqlens` 中出现相邻两个元素相等，代表什么？本项目的测试数据会出现这种情况吗？

**答案**：代表一条长度为 0 的空序列。本项目测试不会出现——`generate_data` 用 `random.sample(range(1, seqlen - 1), batch - 1)` 选切点，切点互不相同且落在 `[1, seqlen-2]` 内，保证每段长度至少为 1（见 4.4 节）。

### 4.3 GQA 与 prefill/decode 分支：moba_layer 全景

#### 4.3.1 概念说明

**GQA（Grouped-Query Attention，分组查询注意力）**：多头注意力的一个常见省显存变体——query 保留 `Hq` 个头，但 K/V 只保留 `Hk` 个头（`Hk < Hq`），每 `r = Hq // Hk` 个 query 头共享同一组 KV 头。KV 缓存大小随序列长度线性增长，减少 KV 头数能成倍压缩缓存。

对应关系为：第 \( j \) 个 query 头使用第 \( \lfloor j / r \rfloor \) 个 KV 头。

MoBA 的两个内核实现（naive 与 efficient）都要求传入的 q、k、v **头数相同**——测试里就是这么调用的：`generate_data(batch, seqlen, head, head, head_dim, dtype)`，q 和 kv 的头数都传同一个 `head`（[tests/test_moba_attn.py:48-50](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_attn.py#L48-L50)）。因此 GQA 的展开必须由 wrapper 代劳：用 `repeat_interleave` 把 KV 复制到与 query 头数一致。

**prefill 与 decode**：自回归生成有两个阶段（`examples/llama.py:34` 的 `model.generate(...)` 内部就是这两个阶段的循环）：

| 阶段 | 输入 | 特征 | wrapper 的处理 |
| --- | --- | --- | --- |
| prefill（预填充） | 整个 prompt 一次算完 | `q_len == kv_len`（query 与 KV 是同一批 token） | 走 MoBA 分支 |
| decode（解码） | 每步只有 1 个新 token | `q_len == 1`，`kv_len` = 上下文长度+1 | 走普通全量因果注意力分支 |

#### 4.3.2 核心流程

`moba_layer` 的完整判定逻辑（伪代码）：

```text
输入: query [B, Hq, Sq, D], key/value [B, Hk, Sk, D]
断言 module.is_causal（只支持因果注意力）

if Sq == Sk:                          # prefill
    q, k, v ← hf_to_fa(各自)           # [B*Sk, H, D]
    r ← Hq // Hk
    k ← repeat_interleave(k, r, 头维)  # [B*Sk, Hq, D]，KV 头复制
    v ← repeat_interleave(v, r, 头维)
    cu_seqlens ← [0, Sk, 2Sk, ..., B*Sk]（int32）
    out ← moba_impl(q, k, v, cu_seqlens, max_seqlen=Sk, chunk_size, topk)
else:                                 # decode（Sq == 1）
    q, k, v ← 各自 transpose(1, 2)     # [B, Sq/Hk..., H, D] flash_attn_func 布局
    out ← flash_attn_func(q, k, v, causal=True)   # 全量注意力，未启用 MoBA
return out, None
```

#### 4.3.3 源码精读

函数签名与接口约定（[moba/wrapper.py:29-53](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/wrapper.py#L29-L53)）：

```python
def moba_layer(moba_impl, moba_config, module, query, key, value, *args,
               dropout: float = 0.0, scaling: Optional[float] = None, **kwargs):
    """
    query (torch.Tensor): [batch, q_heads, q_len, head_dim]
    key   (torch.Tensor): [batch, kv_heads, kv_len, head_dim]
    ...
    Returns:
        attn_output (torch.Tensor): [batch, q_len, q_heads, head_dim]
    """
    assert module.is_causal
    batch, q_heads, q_len, head_dim = query.shape
    _, kv_heads, kv_len, _ = key.shape
```

`moba_impl` 与 `moba_config` 两个参数并不来自 HF——它们是注册时用 `partial` 预绑定的（上一讲讲过的 `register_moba`，[moba/__init__.py:9-11](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/__init__.py#L9-L11)），HF 实际传的是从 `module` 开始的参数。`assert module.is_causal` 表明 MoBA 的块选择规则（当前块必选、未来块排除，见下一单元）内建了因果假设，双向注意力不在支持范围内。

prefill 分支的布局转换与 GQA 展开（[moba/wrapper.py:54-66](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/wrapper.py#L54-L66)）：

```python
if q_len == kv_len:
    # prefill phase
    query = hf_to_fa(query)
    key = hf_to_fa(key)
    value = hf_to_fa(value)
    kv_replicas = q_heads // kv_heads
    key = torch.repeat_interleave(key, kv_replicas, dim=1)
    value = torch.repeat_interleave(value, kv_replicas, dim=1)
    cu_seqlens_k = torch.cumsum(...)
```

两个细节值得停下来想清楚：

1. **为什么必须用 `repeat_interleave` 而不是 `repeat`（平铺）**。假设 `Hq=4, Hk=2`，`repeat_interleave` 沿头维得到 `[k0, k0, k1, k1]`，于是 query 头 \( j \) 配到 KV 头 \( \lfloor j/2 \rfloor \)，与 GQA 定义一致；若用 `repeat` 平铺会得到 `[k0, k1, k0, k1]`，query 头 1（本该用 k0）会错配到 k1——数值直接错误。
2. **顺序**：先 `hf_to_fa` 再 `repeat_interleave`。此时张量是 `[B*Sk, Hk, D]`，头维是 dim=1，与 `dim=1` 参数吻合。

调用 MoBA 内核（[moba/wrapper.py:67-75](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/wrapper.py#L67-L75)）：

```python
out = moba_impl(
    q=query, k=key, v=value,
    cu_seqlens=cu_seqlens_k,
    max_seqlen=kv_len,
    moba_chunk_size=moba_config.moba_chunk_size,
    moba_topk=moba_config.moba_topk,
)
```

`max_seqlen=kv_len` 顺理成章：等长批次里最长序列就是 `kv_len`。

decode 分支（[moba/wrapper.py:76-82](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/wrapper.py#L76-L82)）：

```python
else:
    # decode phase
    # TODO release paged attn implementation
    query = query.transpose(1, 2)
    key = key.transpose(1, 2)
    value = value.transpose(1, 2)
    out = flash_attn_func(query, key, value, dropout, scaling, True)
```

decode 时 `q_len=1`、`kv_len` 为上下文长度+1，两者不等，判为 decode。这里只用 `transpose(1, 2)` 把 `[B, H, S, D]` 换成 `[B, S, H, D]`——`flash_attn_func` 期望的 4 维 dense 布局，保留 batch 维（此时 GQA 由 flash-attn 自身支持，无需复制 KV）。最后一个位置参数 `True` 是 `causal=True`：单个 query 位于序列末尾，因果掩码下它能看到全部 KV 位置，正是解码步需要的注意力。注意注释 `TODO release paged attn implementation`：**decode 阶段目前没有启用 MoBA 稀疏注意力**，走的是全量注意力。

最后看返回（[moba/wrapper.py:83-84](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/wrapper.py#L83-L84)）：

```python
# out = fa_to_hf(out, batch)
return out, None
```

prefill 分支的 `out` 是 flash-attn 布局 `[B*Sq, Hq, D]`。HF 期望注意力返回 `[B, Sq, Hq, D]`（docstring 第 48 行）——把 `[B*Sq, Hq, D]` 直接 `view(B, Sq, Hq, D)` 元素顺序就完全正确（因为合并维本来就是 `B` 与 `Sq` 的行优先合并）。而 `fa_to_hf` 会给出 `[B, Hq, Sq, D]`，反而是 HF **不**想要的轴序。所以这行调用被注释掉：裸返回的形状恰好与 HF 期望一致。这个话题在 u4-l1（transformers 集成）会展开细讲，这里先建立印象。

#### 4.3.4 代码实践

1. **实践目标**：不依赖 flash-attn / GPU，用一个「桩函数」替换 `moba_impl` 与 `flash_attn_func`，跟踪 prefill 与 decode 两条支路中每个张量的形状变化。

2. **操作步骤**：新建 `trace_moba_layer.py`（示例代码。`moba_layer` 主体逻辑复制自 `moba/wrapper.py:51-84`，仅把两个真实实现换成打印形状的桩）：

   ```python
   import torch
   from typing import Optional

   def hf_to_fa(x):
       return x.permute(0, 2, 1, 3).reshape(-1, x.shape[1], x.shape[3])

   def moba_impl_stub(**kw):
       print("  [moba_impl] q:", tuple(kw["q"].shape),
             "cu_seqlens:", kw["cu_seqlens"].tolist(),
             "max_seqlen:", kw["max_seqlen"])
       return torch.randn(kw["q"].shape[0], kw["q"].shape[1], kw["q"].shape[2])

   def flash_attn_func_stub(q, k, v, dropout, scaling, causal):
       print("  [flash_attn_func] q:", tuple(q.shape), "k:", tuple(k.shape),
             "causal:", causal)
       return torch.randn_like(q)

   def moba_layer_stub(query, key, value):
       batch, q_heads, q_len, _ = query.shape
       _, kv_heads, kv_len, _ = key.shape
       if q_len == kv_len:                       # prefill
           query, key, value = hf_to_fa(query), hf_to_fa(key), hf_to_fa(value)
           kv_replicas = q_heads // kv_heads
           key = torch.repeat_interleave(key, kv_replicas, dim=1)
           value = torch.repeat_interleave(value, kv_replicas, dim=1)
           cu = torch.cumsum(torch.tensor([0] + [kv_len] * batch), dim=0
                            ).to(torch.int32)
           print("prefill: GQA 扩展后 k:", tuple(key.shape))
           out = moba_impl_stub(q=query, k=key, v=value, cu_seqlens=cu,
                                max_seqlen=kv_len,
                                moba_chunk_size=128, moba_topk=2)
       else:                                     # decode
           out = flash_attn_func_stub(query.transpose(1, 2),
                                      key.transpose(1, 2),
                                      value.transpose(1, 2), 0.0, None, True)
       return out

   torch.manual_seed(0)
   B, Hq, Hk, S, D = 2, 4, 2, 10, 128
   q = torch.randn(B, Hq, S, D); k = torch.randn(B, Hk, S, D); v = torch.randn(B, Hk, S, D)
   print("== prefill (q_len == kv_len == 10) ==")
   moba_layer_stub(q, k, v)
   print("== decode (q_len=1, kv_len=11) ==")
   q1 = torch.randn(B, Hq, 1, D); k1 = torch.randn(B, Hk, 11, D); v1 = torch.randn(B, Hk, 11, D)
   moba_layer_stub(q1, k1, v1)
   ```

3. **需要观察的现象**：prefill 支路里 GQA 扩展后 k 的头数从 2 变为 4，进入桩时 q/k/v 形状为 `(20, 4, 128)`、`cu_seqlens` 为 `[0, 10, 20]`；decode 支路进入 `flash_attn_func` 桩时 q 为 `(2, 1, 4, 128)`、k 为 `(2, 11, 2, 128)`。

4. **预期结果**：输出形如 `prefill: GQA 扩展后 k: (20, 4, 128)`、`[moba_impl] q: (20, 4, 128) cu_seqlens: [0, 10, 20] max_seqlen: 10`、`[flash_attn_func] q: (2, 1, 4, 128) k: (2, 11, 2, 128) causal: True`。注意 decode 支路的 k 仍是 2 个头（Hk 未扩展，GQA 交给 flash_attn_func 处理）——与真实 `flash_attn_func` 的行为对齐这一点属于 flash-attn 自身特性，**待本地验证**（需 GPU 环境安装 flash-attn==2.6.3 后用真实库替换桩函数验证）。

#### 4.3.5 小练习与答案

**练习 1**：`Hq=8, Hk=2` 时 `kv_replicas` 是多少？展开后 KV 头序列是什么？如果误写成 `key.repeat(kv_replicas, dim=1)`（平铺），头配对会变成什么样？

**答案**：`kv_replicas = 4`；`repeat_interleave` 得 `[k0, k0, k0, k0, k1, k1, k1, k1]`，query 头 \( j \) 配 \( \lfloor j/4 \rfloor \)，正确。平铺 `repeat` 会得到 `[k0, k1, k0, k1, ...]`，query 头 1～3（本该共享 k0）会错配到 k1，注意力结果错误。

**练习 2**：decode 阶段每步 `q_len=1`、`kv_len` 逐步增长。为什么不担心 `q_len == kv_len` 误判？此时 MoBA 稀疏注意力生效吗？

**答案**：`kv_len` 至少为 1 且随生成步增长，只要 `kv_len > 1` 就有 `q_len != kv_len`；唯一步 `kv_len == 1` 且 `q_len == 1` 的情形发生在上下文只有一个 token 时，此时退回 prefill 分支也不破坏正确性（单 token 的 MoBA 就是普通自注意力）。decode 分支当前走 `flash_attn_func` 全量因果注意力，MoBA 未启用——源码留有 `TODO release paged attn implementation` 注释。

**练习 3**：prefill 分支为什么敢用 `q_len == kv_len` 作为判据，而不是显式传入阶段标志？

**答案**：因果语言模型的 prefill 中 query 与 KV 恰好是同一批 token（每个 token 既作为 query 计算输出、又作为 KV 被自己及后续 token 看到），所以长度必然相等；decode 中 query 只有新产生的 1 个 token 而 KV 包含全部历史，长度必然不等。在这个受限场景下长度相等性就是一个可靠的充分判据，省去了额外的接口参数。

### 4.4 测试数据构造：generate_data

#### 4.4.1 概念说明

[tests/test_moba_attn.py](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_attn.py) 是 naive 与 efficient 两个实现的对齐测试。它的一切测试数据由一个 27 行的函数 `generate_data` 构造，这个函数是本讲三个概念的集中演练场：

- 直接以 flash-attn 布局创建 q/k/v（不走 HF 布局，没有 padding）；
- 用随机切点构造真正**不等长**的 varlen 批次；
- q 与 kv 头数相同（`generate_data(batch, seqlen, head, head, ...)`），把 GQA 留给 wrapper。

理解了它，你就具备了为本项目写新测试（u4-l2）的能力。

#### 4.4.2 核心流程

```text
固定随机种子（random / torch / torch.cuda 三处 seed=0）
创建 q, k, v: [seqlen, num_head, headdim]，requires_grad=True
在 1 .. seqlen-2 中无放回抽 batch-1 个整数作为切点
排序后首尾补 0 和 seqlen → cu_seqlens（int32）
max_seqlen = max(相邻差分)
返回 (q, k, v, cu_seqlens, max_seqlen)
```

#### 4.4.3 源码精读

张量创建（[tests/test_moba_attn.py:15-23](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_attn.py#L15-L23)）：

```python
q = torch.randn(
    (seqlen, num_q_head, headdim), dtype=dtype, device=device, requires_grad=True
)
```

直接就是 `[S, H, D]` 的 flash-attn 布局，且 `requires_grad=True`——测试要对输出做反向、比对两个实现的梯度（详见 u4-l2）。

varlen 切点的构造是全函数最精巧的三行（[tests/test_moba_attn.py:26-29](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_attn.py#L26-L29)）：

```python
cu_seqlen = random.sample(range(1, seqlen - 1), batch - 1) if batch > 1 else []
cu_seqlen.sort()
cu_seqlen = [0] + cu_seqlen + [seqlen]
cu_seqlen = torch.tensor(cu_seqlen, device=device, dtype=torch.int32)
```

`random.sample` **无放回**地从 `range(1, seqlen-1)`（即 1 到 seqlen-2 的整数）抽 `batch-1` 个切点：无放回保证切点互不相同（排序后严格递增），取值范围避开 0 和 seqlen 保证首段、末段长度至少为 1。`batch=1` 时切点列表为空，`cu_seqlens = [0, seqlen]`，退化为单序列。注意第 0 维 `seqlen` 此时扮演的角色是「打包总长」，各条序列长度由切点随机决定。

`max_seqlen` 的计算（[tests/test_moba_attn.py:31-32](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_attn.py#L31-L32)）：

```python
max_seqlen = torch.amax(cu_seqlen[1:] - cu_seqlen[:-1])
```

正是 4.2 节的公式 \( \max_i L_i \) 的张量化写法：差分得到每条序列长度，取最大值，最后 `.item()`（第 34 行返回处）转成 Python 标量传给内核。

最后看参数化如何驱动它（[tests/test_moba_attn.py:37-50](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_attn.py#L37-L50)）：

```python
@pytest.mark.parametrize("batch", [1, 4, 7])          # can be arbitrary
@pytest.mark.parametrize("head", [1, 2, 4, 8])
@pytest.mark.parametrize("seqlen", [512, 1024, 2048])
...
    q, k, v, cu_seqlen, max_seqlen = generate_data(
        batch, seqlen, head, head, head_dim, dtype
    )
```

`batch=7 > seqlen` 的组合不存在（seqlen 最小 512），但 `batch` 可以是任意值（注释 "can be arbitrary"）——只要切点够抽。q/kv 头数传同一个 `head`，再次印证「内核不管 GQA，wrapper 管」的分工。

#### 4.4.4 代码实践

1. **实践目标**：在 CPU 上复刻 `generate_data` 的 varlen 构造逻辑，验证它产出的 `cu_seqlens` 满足 4.2 节的全部不变量。

2. **操作步骤**（示例代码，把 [tests/test_moba_attn.py:8-34](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_attn.py#L8-L34) 中 CUDA 相关行去掉后的 CPU 版）：

   ```python
   import torch, random

   def generate_data_cpu(batch, seqlen):
       random.seed(0)
       cu = random.sample(range(1, seqlen - 1), batch - 1) if batch > 1 else []
       cu.sort()
       cu = torch.tensor([0] + cu + [seqlen], dtype=torch.int32)
       max_len = torch.amax(cu[1:] - cu[:-1]).item()
       return cu, max_len

   cu, max_len = generate_data_cpu(batch=4, seqlen=16)
   print("cu_seqlens:", cu.tolist(), "max_seqlen:", max_len)

   # 不变量检查
   diffs = cu.tolist()
   assert diffs[0] == 0 and diffs == sorted(diffs)      # 首元素 0 且单调递增
   assert diffs[-1] == 16                                # 末元素 == 打包总长
   lens = [b - a for a, b in zip(diffs[:-1], diffs[1:])]
   assert min(lens) >= 1 and max(lens) == max_len        # 每段 >=1，最大段 == max_seqlen
   print("lengths of each sequence:", lens)
   ```

3. **需要观察的现象**：随机种子固定为 0，`cu_seqlens` 输出确定；四条子序列长度均 ≥ 1，其中最大者等于 `max_seqlen`。

4. **预期结果**：例如 batch=4、seqlen=16 时输出某个确定的切点划分（具体数值取决于 Python 版本的 `random.sample` 实现，**待本地验证**），三条断言全部通过。若在 GPU 环境且已安装依赖，可进一步运行 `pytest tests/test_moba_attn.py -x -q` 观察真实测试（该路径需要 CUDA，本 CPU 复刻不需要）。

#### 4.4.5 小练习与答案

**练习 1**：`random.sample(range(1, seqlen - 1), batch - 1)` 保证了切点的哪两条性质？分别防止什么问题？

**答案**：① 无放回 → 切点互不相同，配合 `sort()` 得到严格递增序列，防止出现长度为 0 的空段；② 取值范围是 `[1, seqlen-2]` → 首段（0 到第一个切点）和末段（最后一个切点到 seqlen）长度至少为 1，防止边界空段。

**练习 2**：`batch=1` 时函数如何表现？

**答案**：条件表达式走 `else []` 分支，切点列表为空，`cu_seqlens = [0, seqlen]`，`max_seqlen = seqlen`——单序列是 varlen 格式的最简特例，无需特殊处理。

**练习 3**：为什么函数开头要固定 `random.seed(0)` 和 `torch.manual_seed(0)`？

**答案**：测试在 bf16 精度下比对两个实现的输出与梯度（容限 2e-2），数据必须可复现才能定位失败原因；且同一次运行中 efficient 与 naive 用的是同一份 q/k/v 和同一个 `vo_grad`，固定种子保证「同题同卷」，对齐比较才有意义。

## 5. 综合实践

把本讲三个约定串成一个完整的「迷你 wrapper」实验（示例代码，纯 CPU 可运行）。任务分三步：

```python
"""综合实践：布局转换 + varlen 构造 + prefill 分路跟踪（纯 CPU）"""
import torch

# ── 第 1 步：HF 布局 → flash-attn 布局，并验证可逆 ──────────────
def hf_to_fa(x):
    return x.permute(0, 2, 1, 3).reshape(-1, x.shape[1], x.shape[3])

def fa_to_hf(x, batch):
    return x.view(batch, -1, x.shape[1], x.shape[2]).permute(0, 2, 1, 3)

torch.manual_seed(0)
Q = torch.randn(2, 4, 10, 128)             # B=2, Hq=4, S=10, D=128
Q_fa = hf_to_fa(Q)
assert Q_fa.shape == (20, 4, 128)
assert torch.equal(fa_to_hf(Q_fa, 2), Q)   # 两次转换互逆
print("step1 ok: [2,4,10,128] -> [20,4,128] -> 还原一致")

# ── 第 2 步：为长度 [6, 10] 的 varlen 批次构造元数据并打包 ──────
lengths = [6, 10]
cu = torch.tensor([0, 6, 16], dtype=torch.int32)
max_len = 10
K = torch.randn(16, 4, 128)                # 已按 varlen 打包的 KV
assert K.shape[0] == cu[-1]
seq0 = K[cu[0]:cu[1]]                      # 按 cu_seqlens 边界切片
print("step2 ok: cu_seqlens =", cu.tolist(), "max_seqlen =", max_len)

# ── 第 3 步：模拟 moba_layer 的 prefill 分路（桩实现）──────────
def moba_impl_stub(q, k, v, cu_seqlens, max_seqlen):
    print("  进入 MoBA 内核: q", tuple(q.shape), "cu_seqlens",
          cu_seqlens.tolist(), "max_seqlen", max_seqlen)
    return torch.randn(q.shape[0], q.shape[1], q.shape[2])

def mini_wrapper(query, key, value):       # 仿 moba/wrapper.py:51-75
    batch, q_heads, q_len, _ = query.shape
    _, kv_heads, kv_len, _ = key.shape
    assert q_len == kv_len                 # 只模拟 prefill
    query, key, value = hf_to_fa(query), hf_to_fa(key), hf_to_fa(value)
    r = q_heads // kv_heads                # GQA: 4 // 2 = 2
    key = torch.repeat_interleave(key, r, dim=1)
    value = torch.repeat_interleave(value, r, dim=1)
    cu_k = torch.cumsum(torch.tensor([0] + [kv_len] * batch), dim=0
                       ).to(torch.int32)
    out = moba_impl_stub(query, key, value, cu_k, kv_len)
    return out.view(batch, q_len, q_heads, -1)   # 直接 view 成 HF 期望的返回形状

q = torch.randn(2, 4, 10, 128)
k = torch.randn(2, 2, 10, 128)             # GQA: kv_heads=2
v = torch.randn(2, 2, 10, 128)
out = mini_wrapper(q, k, v)
print("step3 ok: 返回形状", tuple(out.shape))   # 期望 (2, 10, 4, 128)
```

**要求完成的验证**：

1. 第 1 步：`[2, 4, 10, 128]` 转换后为 `[20, 4, 128]`，且往返转换用 `torch.equal` 验证逐元素相等。
2. 第 2 步：手工写出 `cu_seqlens = [0, 6, 16]`、`max_seqlen = 10`，检查打包张量第 0 维等于 `cu_seqlens[-1]`。
3. 第 3 步：确认进入桩内核的 q/k/v 均为 `[20, 4, 128]`（GQA 已扩展、布局已转换）、`cu_seqlens = [0, 10, 20]`、`max_seqlen = 10`；返回值形状为 `[2, 10, 4, 128]`，与 `moba_layer` docstring 声明的返回形状一致。
4. 思考题：mini_wrapper 最后为什么用 `out.view(batch, q_len, q_heads, -1)` 而不是 `fa_to_hf(out, batch)`？用第 1 步的元素位置检查法（\( p = b \cdot S + s \)）验证你的答案。

预期结果：三步全部通过，思考题答案与 4.3.3 末尾的解释一致（`[B*Sq, Hq, D]` 直接 view 成 `[B, Sq, Hq, D]` 元素顺序天然正确；`fa_to_hf` 给出的 `[B, Hq, Sq, D]` 反而不是 HF 期望的轴序）。

## 6. 本讲小结

- HF 布局 `[B, H, S, D]` 与 flash-attn 布局 `[B*S, H, D]` 通过 `permute(0,2,1,3)` + `reshape(-1, H, D)` 互转，合并维的元素顺序是 \( p = b \cdot S + s \)，必须与 `cu_seqlens` 边界配套（`moba/wrapper.py:7-26`）。
- varlen 用 `cu_seqlens`（长度 B+1、单调递增、int32、首元素 0 的前缀和）描述打包序列边界，`max_seqlen` 只作内核启动参数不参与索引；batch 数量由 `cu_seqlens.numel() - 1` 反推。
- GQA 下 `Hk < Hq`，MoBA 内核要求头数一致，wrapper 用 `repeat_interleave(k, Hq//Hk, dim=1)` 展开 KV，顺序不能换成平铺的 `repeat`。
- `moba_layer` 用 `q_len == kv_len` 判定 prefill（走 MoBA）、否则 decode（走 `flash_attn_func` 全量因果注意力，MoBA 尚未启用，留有 paged attention TODO）。
- `generate_data` 是三种约定的样板代码：直接以 flash-attn 布局造数据、用无放回随机切点构造不等长 varlen 批次、q/kv 头数相同。

## 7. 下一步学习建议

本讲之后，你已经具备读懂 MoBA 核心源码的全部「语言基础」。下一讲 **u2-l1（moba_naive 逐行精读：分块、gate 与块选择掩码）** 将进入算法本身：`moba_attn_varlen_naive` 如何按 `moba_chunk_size` 切分 KV、用块内 K 均值算无参数 gate、top-k 选块并展开成注意力掩码。阅读时你会看到本讲的 `cu_seqlens` 切片模式（`moba/moba_naive.py:34-41`）反复出现。建议在读 u2-l1 之前，把本讲 4.2 节的边界切片代码再过一遍——naive 实现就是一个「按 cu_seqlens 切片后逐 batch 处理」的大循环，边界感越熟，读得越快。

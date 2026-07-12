# 张量并行线性层与权重分片

## 1. 本讲目标

本讲是「模型执行」单元的收尾篇，承接上一讲（Qwen3 模型结构）。在 u4-l4 里我们看到了 `Qwen3DecoderLayer` 内部的 `qkv_proj`、`o_proj`、`gate_up_proj`、`down_proj` 都是「并行线性层」，`embed_tokens` 与 `lm_head` 是「并行 Embedding」，但当时刻意没讲「并行」二字究竟意味着什么。本讲就把这层窗户纸捅破。

读完本讲你应该能够：

- 说清**列并行**（Column Parallel）与**行并行**（Row Parallel）在数学上切的是什么、为什么列并行不需要通信而行并行需要 `all_reduce`。
- 理解 `LinearBase` 如何用一个挂在 `Parameter` 上的 `weight_loader` 方法，把「权重加载时的分片策略」与「前向计算」解耦。
- 手算 `QKVParallelLinear` 在 GQA 下 q/k/v 三段各自的 `shard_offset` 与 `shard_size`，并推演 `tp_size=2` 时每个 rank 持有 `qkv_proj` 的哪些行。
- 解释 `MergedColumnParallelLinear` 如何把 HuggingFace 的 `gate_proj` / `up_proj` 装进合并后的 `gate_up_proj`。
- 区分 `VocabParallelEmbedding`（用 `all_reduce` 复制结果）与 `ParallelLMHead`（用 `gather` 把词表分片汇总到 rank 0）。

## 2. 前置知识

### 2.1 为什么要把权重切开

单个 LLM 的权重动辄几十、上百 GB，单卡装不下。**张量并行（Tensor Parallelism, TP）** 的思路是：把同一层的权重矩阵**按某个维度切成若干份**，每张卡（每个 rank）只存其中一份；前向时各卡各自算一部分，再用一次通信把结果拼好或加起来。这和「数据并行」（每卡存完整模型、各算不同数据）完全不同——TP 是「同一层、同一批数据，权重分家」。

本讲里 `tp_size` 表示张量并行的卡数，`tp_rank` 表示当前进程是第几张卡（从 0 开始）。在 nano-vllm 中，每个 rank 都是一个独立进程，启动时执行：

[nanovllm/engine/model_runner.py:22-27](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L22-L27) —— 用 `dist.init_process_group` 建立进程组，`world_size = config.tensor_parallel_size`。

所以在各层代码里反复出现的 `dist.get_world_size()` 其实就是 `tp_size`，`dist.get_rank()` 就是 `tp_rank`。多进程如何被拉起、如何用共享内存同步调用，留到 u5-l3 讲，本讲只关心「权重长什么样」。

### 2.2 线性层的权重形状

PyTorch 的 `F.linear(x, weight, bias)` 计算的是 \( y = x W^\top + b \)，其中 `weight` 的形状是 `[output_size, input_size]`（注意是「输出在前，输入在后」）。nano-vllm 完全沿用这一约定。这一点至关重要——它决定了「切 dim 0」和「切 dim 1」分别对应什么物理含义：

| 切的维度 | 切的是 weight 的 | 物理含义 |
|---|---|---|
| `dim=0`（行） | output_size | 输出特征被切片 |
| `dim=1`（列） | input_size | 输入特征被切片 |

### 2.3 列并行与行并行的数学直觉

记权重 \( W \in \mathbb{R}^{\text{out}\times\text{in}} \)，输入 \( X \in \mathbb{R}^{n\times\text{in}} \)，输出 \( Y = XW^\top \in \mathbb{R}^{n\times\text{out}} \)。

**列并行**（沿 output 维切）：把 \( W \) 按行切成 \( t \) 块 \( W_0,\dots,W_{t-1} \)，每个 \( W_i \) 形状 \([\text{out}/t, \text{in}]\)。

\[
Y_i = X W_i^\top \in \mathbb{R}^{n\times(\text{out}/t)}, \quad
Y = [Y_0 \mid Y_1 \mid \dots \mid Y_{t-1}]
\]

每个 rank 算出输出的一**段互不重叠的列**，天然拼接成完整 \( Y \)，**无需通信**。

**行并行**（沿 input 维切）：把 \( W \) 按列切成 \( t \) 块，输入 \( X \) 也按列对应切成 \( X_0,\dots,X_{t-1} \)。

\[
Y = \sum_{i=0}^{t-1} X_i W_i^\top
\]

每个 rank 算出的是一个**部分和**，必须 `all_reduce`（求和）才能得到完整 \( Y \)。

> 命名来源：Megatron-LM 原论文里 \( W \) 存成 `[in, out]`（输出特征在「列」方向），所以「切输出特征」叫 column parallel、「切输入特征」叫 row parallel。nano-vllm 把 weight 存成 `[out, in]`（PyTorch 习惯），物理上 column parallel 切的是「行」（dim 0），但**名字仍沿用 Megatron/vLLM 的叫法**，读源码时不要被名字误导。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [nanovllm/layers/linear.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/linear.py) | 全部并行线性层：`LinearBase`、`ColumnParallelLinear`、`MergedColumnParallelLinear`、`QKVParallelLinear`、`RowParallelLinear` |
| [nanovllm/layers/embed_head.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/embed_head.py) | 词表并行：`VocabParallelEmbedding`、`ParallelLMHead` |
| [nanovllm/models/qwen3.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py) | 这些层的「使用方」：把并行层组装成 Attention/MLP |
| [nanovllm/utils/loader.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/utils/loader.py) | 权重加载的「调度方」：读 safetensors，调 `weight_loader` |

---

## 4. 核心概念与源码讲解

### 4.1 列并行基础与 LinearBase 基类

#### 4.1.1 概念说明

所有并行线性层都继承自 `LinearBase`。它做三件事：

1. **记录分片信息**：`tp_dim` 表示「权重该沿哪个维度切」（0 或 1），`tp_rank` / `tp_size` 来自进程组。
2. **按分片后的尺寸建空权重**：子类先把 `output_size` 或 `input_size` 除以 `tp_size` 再传上来，所以每个 rank 的 `weight` 张量只是「自己那份」。
3. **把分片加载策略挂到 Parameter 上**：`self.weight.weight_loader = self.weight_loader`，让加载器能通过参数对象找到「该怎么往里灌权重」。

这套设计的妙处在于：**前向计算**（`forward`）和**权重加载**（`weight_loader`）被彻底解耦，前向不用关心分片，加载器也不用 `if-else` 判断参数类型——直接 `getattr(param, "weight_loader")` 即可。

#### 4.1.2 核心流程

`ColumnParallelLinear` 的生命周期：

```
构造期（每个 rank 各跑一次）：
  tp_size = get_world_size()
  把 output_size 除以 tp_size  →  每个 rank 只持有 output_size/tp 行
  tp_dim = 0  →  沿 dim 0（输出特征）切

加载期（loader 调 weight_loader）：
  shard_size = 本 rank 的输出行数 = output_size / tp_size
  start_idx = tp_rank * shard_size
  取完整权重的 [start_idx : start_idx+shard_size] 行  →  copy_ 进本 rank 的 weight

前向：
  y = F.linear(x, weight, bias)   # 直接算自己这段输出，无需通信
```

#### 4.1.3 源码精读

先看基础设施 `LinearBase` 与工具函数 `divide`：

[nanovllm/layers/linear.py:7-9](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/linear.py#L7-L9) —— `divide` 要求整除，TP 下所有「可切」的维度都必须能被 `tp_size` 整除，否则直接断言失败。

[nanovllm/layers/linear.py:12-34](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/linear.py#L12-L34) —— `LinearBase`。注意第 25 行 `weight = torch.empty(output_size, input_size)`，第 26 行 `self.weight.weight_loader = self.weight_loader`：把实例方法**当成属性**挂到 Parameter 上，这是整个分片加载体系的钩子。`tp_dim` 由子类在第 4 个位置参数传入（列并行传 0，行并行传 1）。

再看 `ColumnParallelLinear` 本体：

[nanovllm/layers/linear.py:54-73](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/linear.py#L54-L73) —— 构造时 `super().__init__(input_size, divide(output_size, tp_size), bias, 0)`，把输出维度除以 `tp_size`、`tp_dim=0`。`weight_loader`（65-70 行）用 `narrow(tp_dim, start_idx, shard_size)` 从完整权重里截出本 rank 的那一段。`forward`（72-73 行）就是普通的 `F.linear`——因为输出特征互不重叠，无需任何通信。

#### 4.1.4 代码实践

**实践目标**：在 `tp_size=1` 下确认列并行退化成「复制完整权重」。

**操作步骤**（CPU 即可，无需 GPU 与模型权重，用 gloo 后端模拟单进程）：

```python
# 示例代码：验证 tp_size=1 时 ColumnParallelLinear 等价于完整线性层
import os
import torch
import torch.distributed as dist
from nanovllm.layers.linear import ColumnParallelLinear

os.environ.setdefault("MASTER_ADDR", "127.0.0.1")
os.environ.setdefault("MASTER_PORT", "29501")
dist.init_process_group("gloo", rank=0, world_size=1)   # tp_size=1

layer = ColumnParallelLinear(input_size=8, output_size=16, bias=False)
full_weight = torch.randn(16, 8)                          # 假装从 HF 读到的完整权重
layer.weight.weight_loader(layer.weight, full_weight)     # 走分片加载

print(torch.equal(layer.weight.data, full_weight))        # 期望 True
print(layer.weight.shape)                                  # 期望 torch.Size([16, 8])
```

**需要观察的现象**：`tp_rank=0`、`tp_size=1` 时，`weight_loader` 里 `start_idx = 0 * 16 = 0`、`shard_size = 16`、`narrow(0, 0, 16)` 就是整张矩阵，所以直接等价复制。

**预期结果**：打印 `True` 与 `torch.Size([16, 8])`。

#### 4.1.5 小练习与答案

**练习 1**：把上面脚本的 `world_size` 改成 2（需要真正双进程，可暂不运行），仅从代码推断：rank 0 的 `layer.weight.shape` 会变成什么？

**答案**：`torch.Size([8, 8])`。因为构造时 `divide(16, 2)=8`，每个 rank 只持有 16 个输出特征中的 8 个，权重形状 `[8, 8]`。`weight_loader` 会把完整 `[16,8]` 权重的第 0~7 行（rank 0）或 8~15 行（rank 1）灌进去。

**练习 2**：为什么 `ColumnParallelLinear.forward` 里没有任何 `dist.all_reduce`？

**答案**：列并行切的是输出特征，每个 rank 算出的是最终输出矩阵里**互不重叠的一段列**，下游（如 attention 各自处理自己的 head）天然消费自己这段，不需要把结果跨 rank 合并。

---

### 4.2 行并行 RowParallelLinear 与 all_reduce

#### 4.2.1 概念说明

行并行切的是**输入维度**（`tp_dim=1`）。每个 rank 的权重形状是 `[output_size, input_size/tp_size]`，配合上一步列并行层产出的「已分片的输入」，每个 rank 只能算出一个**部分和**，必须 `all_reduce` 把所有 rank 的部分和加起来才得到完整结果。

行并行总是和列并行**成对出现**：列并行把特征「分散」到各 rank，行并行再把它「收拢」回来。这样一次 attention 块或一次 MLP 只需要**一次** `all_reduce`（在 `o_proj` / `down_proj` 处），这正是 Megatron-TP 的核心省通信技巧。

#### 4.2.2 核心流程

```
构造：input_size 除以 tp_size，tp_dim=1
加载：从完整权重切出本 rank 负责的「列段」
前向：
  y = x @ weight.T            # weight:[out, in/tp]，x:[..., in/tp] → 部分和 [..., out]
  if tp_size > 1:
      all_reduce(y)           # 把各 rank 部分和求和
  偏置只在 rank 0 加一次（避免 all_reduce 后被放大 tp_size 倍）
```

#### 4.2.3 源码精读

[nanovllm/layers/linear.py:131-156](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/linear.py#L131-L156) —— `RowParallelLinear`。构造（139-140 行）把 `input_size` 除以 `tp_size`、`tp_dim=1`，但 **`output_size` 保持完整**（不分片），因为输出是要被 `all_reduce` 累加的完整维度。

[nanovllm/layers/linear.py:152-156](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/linear.py#L152-L156) —— 前向是理解行并行的关键：

- 第 153 行 `F.linear(x, self.weight, self.bias if self.tp_rank == 0 else None)`：**偏置只在 rank 0 加**。因为如果每个 rank 都加一次 bias，`all_reduce` 求和后 bias 会被放大 `tp_size` 倍。
- 第 154-155 行：`tp_size > 1` 时才 `all_reduce`。`tp_size=1` 时跳过通信，退化为普通线性层。

[nanovllm/layers/linear.py:142-150](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/linear.py#L142-L150) —— `weight_loader` 有个 1 维特判：偏置 `param_data.ndim == 1` 时直接整体 `copy_`（偏置不分片，全 rank 持有完整 bias）；2 维权重才沿 dim 1 切列段。

来看使用方，理解「列→行」配对：

[nanovllm/models/qwen3.py:49-53](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py#L49-L53) —— attention 的 `o_proj` 是 `RowParallelLinear(total_num_heads*head_dim, hidden_size)`，接收各 rank 本地 attention 的输出（已是分好的 head 维度），`all_reduce` 后还原成完整 `hidden_size`。

[nanovllm/models/qwen3.py:105-109](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py#L105-L109) —— MLP 的 `down_proj` 同理，`RowParallelLinear(intermediate_size, hidden_size)`。

#### 4.2.4 代码实践

**实践目标**：验证 `tp_size=1` 时 `RowParallelLinear` 等价普通线性层，并理解 bias 在多 rank 下的处理。

**操作步骤**：

```python
# 示例代码：验证 tp_size=1 时 RowParallelLinear 等价完整线性层
import os, torch, torch.distributed as dist
from nanovllm.layers.linear import RowParallelLinear

os.environ.setdefault("MASTER_ADDR", "127.0.0.1")
os.environ.setdefault("MASTER_PORT", "29502")
dist.init_process_group("gloo", rank=0, world_size=1)

layer = RowParallelLinear(input_size=8, output_size=16, bias=True)
full_w, full_b = torch.randn(16, 8), torch.randn(16)
layer.weight.weight_loader(layer.weight, full_w)
layer.bias.weight_loader(layer.bias, full_b)

x = torch.randn(4, 8)
print(torch.allclose(layer(x), torch.nn.functional.linear(x, full_w, full_b)))  # 期望 True
```

**需要观察的现象**：`tp_size=1` 时第 154 行 `self.tp_size > 1` 为假，跳过 `all_reduce`，且 `tp_rank==0` 成立所以加 bias，完全等价于普通线性层。

**预期结果**：打印 `True`。

#### 4.2.5 小练习与答案

**练习 1**：若把第 153 行改成所有 rank 都加 `self.bias`，`all_reduce` 后结果会怎样？

**答案**：`all_reduce` 做求和，bias 会被加 `tp_size` 次，结果变成 `Y + tp_size * bias`，是错的。所以代码里用 `self.bias if self.tp_rank == 0 else None` 保证 bias 只被加一次。

**练习 2**：`RowParallelLinear` 的输入为什么天然就是「已分片」的，不需要在 forward 里手动 `split`？

**答案**：因为它总是接在列并行层后面。例如 `down_proj` 接在 `gate_up_proj`（列并行）+ `SiluAndMul` 之后，列并行已经把中间维度 `intermediate_size` 分到各 rank，输入正好是 `[..., intermediate_size/tp]`，与 `down_proj` 权重的 `[hidden, intermediate_size/tp]` 匹配。

---

### 4.3 QKVParallelLinear：GQA 感知的 q/k/v 合并与分片

#### 4.3.1 概念说明

HuggingFace 的 Qwen3 权重里，attention 的投影是**三个独立矩阵** `q_proj`、`k_proj`、`v_proj`。为了减少 kernel launch、提高访存效率，nano-vllm 把它们**合并成一个** `qkv_proj`。难点有两个：

1. **GQA**：query 头数 `num_heads` 通常大于 key/value 头数 `num_kv_heads`（例如 16:8），所以 q、k、v 三段**宽度不等**，不能简单等分。
2. **同时合并 + 分片**：既要把 q/k/v 拼成一个大矩阵，又要对这个大矩阵做列并行（按 head 分给各 rank）。

`QKVParallelLinear` 继承自 `ColumnParallelLinear`，复用「列并行」的构造与前向；它只**重写 `weight_loader`**，用 `loaded_shard_id`（取值 `"q"`/`"k"`/`"v"`）告诉加载器当前灌的是哪一段。

#### 4.3.2 核心流程

设 `tp_size = t`，则每个 rank 的头数：

\[
\text{num\_heads} = \text{total\_num\_heads} / t, \quad
\text{num\_kv\_heads} = \text{total\_num\_kv\_heads} / t
\]

合并后**本 rank** 的 `qkv_proj` 输出行数：

\[
\text{output\_size} = (\text{num\_heads} + 2\cdot\text{num\_kv\_heads}) \cdot \text{head\_size}
\]

三段在本 rank 张量里的排布 `[q | k | v]`：

| 段 | `shard_offset`（起点） | `shard_size`（行数） |
|---|---|---|
| q | `0` | `num_heads * head_size` |
| k | `num_heads * head_size` | `num_kv_heads * head_size` |
| v | `(num_heads + num_kv_heads) * head_size` | `num_kv_heads * head_size` |

加载某段时，先把完整 HF 权重沿 dim 0 `chunk(tp_size)`，取本 rank 那一份，再 `copy_` 到 `qkv_proj` 对应 `offset` 处。

#### 4.3.3 源码精读

[nanovllm/layers/linear.py:96-112](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/linear.py#L96-L112) —— 构造函数。第 107 行 `total_num_kv_heads = total_num_kv_heads or total_num_heads`：若不传 kv 头数（非 GQA，即 MHA），则令 kv 头数等于 q 头数。第 109-110 行把总头数除以 `tp_size` 得到本 rank 头数。第 111 行按上面的公式算合并后的输出尺寸，再交给 `ColumnParallelLinear` 的构造（它会再把这个尺寸视为「总输出」——但因为这里传入的 `total_num_heads`/`total_num_kv_heads` 是**全局**值，`output_size` 也是全局值，`ColumnParallelLinear` 内部会再 `/tp_size`）。

[nanovllm/layers/linear.py:114-128](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/linear.py#L114-L128) —— `weight_loader` 是本讲最值得手算的代码。第 117-125 行按 `loaded_shard_id` 计算 `shard_size`/`shard_offset`（注意这里的 `num_heads`/`num_kv_heads` 是**本 rank** 的头数，因为构造时已经赋值）。第 127 行 `loaded_weight.chunk(self.tp_size, self.tp_dim)[self.tp_rank]` 把完整 HF 权重切成 `tp_size` 份取本 rank 那份，第 126 行先 `narrow` 到目标 `offset` 区间，最后 `copy_`。

> 注意一个微妙点：第 111 行用**全局**头数算 `output_size`，传给 `ColumnParallelLinear` 后**内部又除以 tp_size**，所以本 rank 实际持有的输出行数 = `(total/t + 2*total_kv/t) * head_size` = `(num_heads + 2*num_kv_heads)*head_size`，与 `weight_loader` 里用本 rank 头数算出的三段之和**严格相等**。这就是「合并」与「分片」能自洽的根源。

看使用方与映射表：

[nanovllm/models/qwen3.py:42-48](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py#L42-L48) —— `qkv_proj = QKVParallelLinear(hidden_size, head_dim, total_num_heads, total_num_kv_heads, bias)`，传入的是**全局**头数。

[nanovllm/models/qwen3.py:187-193](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py#L187-L193) —— `packed_modules_mapping`：把 HF 的 `q_proj`/`k_proj`/`v_proj` 分别映射到 `qkv_proj` 的 `"q"`/`"k"`/`"v"` 段。加载器据此调用 `weight_loader(param, tensor, "q")` 等。

[nanovllm/utils/loader.py:12-28](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/utils/loader.py#L12-L28) —— 加载主循环：命中 `packed_modules_mapping` 时取出 `shard_id`（如 `"q"`）传给 `weight_loader`；未命中则走 `default_weight_loader`。这就是为什么 `QKVParallelLinear.weight_loader` 要第三个参数 `loaded_shard_id`。

#### 4.3.4 代码实践

**实践目标**：在 `tp_size=1` 下验证合并后的 `qkv_proj == [q | k | v]`，并手算 `tp_size=2` 时各 rank 持有的行。

**操作步骤**：

```python
# 示例代码：验证合并 qkv_proj
import os, torch, torch.distributed as dist
from nanovllm.layers.linear import QKVParallelLinear

os.environ.setdefault("MASTER_ADDR", "127.0.0.1")
os.environ.setdefault("MASTER_PORT", "29503")
dist.init_process_group("gloo", rank=0, world_size=1)

hidden, head_dim, H, KV = 64, 8, 4, 2          # 4 q-head, 2 kv-head (GQA)
layer = QKVParallelLinear(hidden, head_dim, H, KV, bias=False)
q = torch.randn(H * head_dim, hidden)           # [32, 64]
k = torch.randn(KV * head_dim, hidden)          # [16, 64]
v = torch.randn(KV * head_dim, hidden)          # [16, 64]
for sid, w in [("q", q), ("k", k), ("v", v)]:
    layer.weight.weight_loader(layer.weight, w, sid)   # 注意传 shard_id

expected = torch.cat([q, k, v], dim=0)          # [32+16+16=64, 64]
print(torch.equal(layer.weight.data, expected)) # 期望 True
print(layer.weight.shape)                        # 期望 torch.Size([64, 64])
```

**需要观察的现象**：`tp_size=1` 时 `num_heads=4`、`num_kv_heads=2`，q 段 offset=0/size=32，k 段 offset=32/size=16，v 段 offset=48/size=16，三段拼接得 `[32|16|16]=64` 行，与 `cat([q,k,v])` 完全一致。

**预期结果**：打印 `True` 与 `torch.Size([64, 64])`。

**手算 tp_size=2 的情形**（不必运行，纯推理）：设全局 `H=4, KV=2, head_dim=8`，则每 rank `num_heads=2, num_kv_heads=1`，每 rank 的 `qkv_proj` 行数 = `(2+2*1)*8 = 32`，排布 `[q:16 | k:8 | v:8]`：

| | q 段（来自全局 q 的哪些行） | k 段 | v 段 |
|---|---|---|---|
| rank 0 | q 行 0~15（head 0,1） | k 行 0~7（kv head 0） | v 行 0~7（kv head 0） |
| rank 1 | q 行 16~31（head 2,3） | k 行 8~15（kv head 1） | v 行 8~15（kv head 1） |

这正是 `loaded_weight.chunk(2, 0)[tp_rank]` 的效果——把每段的完整权重对半切，前半给 rank 0、后半给 rank 1。

> 若本地无 GPU/模型，本实践的「运行」部分为**待本地验证**（gloo 单进程版可直接跑）；tp_size=2 表格为推理结论，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `QKVParallelLinear` 切分时，q 段和 k/v 段的 `shard_size` 不同？

**答案**：因为 GQA 下 query 头数多于 key/value 头数。q 段每 rank 有 `num_heads` 个头，k/v 段每 rank 只有 `num_kv_heads` 个头，所以 `shard_size` 分别是 `num_heads*head_size` 与 `num_kv_heads*head_size`，宽度不等。

**练习 2**：若一个模型的 `total_num_kv_heads=8`、`tp_size=4`，会出什么问题？需要满足什么约束？

**答案**：`divide(8, 4)=2`，每个 rank 2 个 kv 头，可行。约束是 `total_num_kv_heads % tp_size == 0` 且 `total_num_heads % tp_size == 0`，否则 `divide` 的断言失败。即 **tp_size 不能超过 kv 头数**。

---

### 4.4 MergedColumnParallelLinear：等宽分块的合并（gate/up）

#### 4.4.1 概念说明

MLP 里的 `gate_proj` 和 `up_proj` 在 HF 权重里也是两个独立矩阵，但和 q/k/v 不同的是——**两者宽度相等**（都是 `intermediate_size`）。`MergedColumnParallelLinear` 把它们合并成一个 `gate_up_proj`，行数 `2 * intermediate_size`。因为两段等宽，`shard_id` 用整数索引（0=gate，1=up）而非字符串。

#### 4.4.2 核心流程

```
output_sizes = [intermediate, intermediate]   # gate, up 各一份
total output = sum(output_sizes) = 2*intermediate

加载 gate (shard_id=0):
  shard_offset = sum(output_sizes[:0]) // tp = 0
  shard_size   = output_sizes[0] // tp = intermediate/tp
  把完整 gate_proj 沿 dim0 chunk(tp) 取本 rank 那份 → copy_ 到 offset 0

加载 up (shard_id=1):
  shard_offset = sum(output_sizes[:1]) // tp = intermediate/tp
  shard_size   = output_sizes[1] // tp = intermediate/tp
  把完整 up_proj chunk(tp) 取本 rank 那份 → copy_ 到对应 offset
```

#### 4.4.3 源码精读

[nanovllm/layers/linear.py:76-93](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/linear.py#L76-L93) —— `MergedColumnParallelLinear`。第 84-85 行把 `output_sizes` 存为实例属性，再调父类构造 `super().__init__(input_size, sum(output_sizes), bias)`（注意：父类是 `ColumnParallelLinear`，会再 `/tp_size`）。第 89-90 行 `weight_loader` 的核心：

- `shard_offset = sum(self.output_sizes[:loaded_shard_id]) // self.tp_size`：前面所有段的累计宽度除以 tp（因为每段都要被 tp 切）。
- `shard_size = self.output_sizes[loaded_shard_id] // self.tp_size`：本段每 rank 的宽度。
- 第 92 行 `loaded_weight.chunk(self.tp_size, self.tp_dim)[self.tp_rank]`：与 QKV 一样的「取本 rank 那一份」。

使用方与映射：

[nanovllm/models/qwen3.py:100-104](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py#L100-L104) —— `gate_up_proj = MergedColumnParallelLinear(hidden_size, [intermediate_size]*2, bias=False)`，两段等宽。

[nanovllm/models/qwen3.py:191-192](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py#L191-L192) —— `packed_modules_mapping`：`gate_proj → (gate_up_proj, 0)`、`up_proj → (gate_up_proj, 1)`，shard_id 是整数。

#### 4.4.4 代码实践

**实践目标**：验证 `tp_size=1` 下 `gate_up_proj == [gate | up]`。

**操作步骤**：

```python
# 示例代码：验证 gate_up_proj 合并
import os, torch, torch.distributed as dist
from nanovllm.layers.linear import MergedColumnParallelLinear

os.environ.setdefault("MASTER_ADDR", "127.0.0.1")
os.environ.setdefault("MASTER_PORT", "29504")
dist.init_process_group("gloo", rank=0, world_size=1)

inter = 32
layer = MergedColumnParallelLinear(input_size=16, output_sizes=[inter, inter], bias=False)
gate = torch.randn(inter, 16); up = torch.randn(inter, 16)
layer.weight.weight_loader(layer.weight, gate, 0)
layer.weight.weight_loader(layer.weight, up, 1)
print(torch.equal(layer.weight.data, torch.cat([gate, up], dim=0)))  # 期望 True
print(layer.weight.shape)                                            # 期望 [64, 16]
```

**需要观察的现象**：gate 装到 offset 0~31，up 装到 offset 32~63，拼接得 `[gate | up]`。

**预期结果**：`True` 与 `torch.Size([64, 16])`。

#### 4.4.5 小练习与答案

**练习 1**：`MergedColumnParallelLinear` 和 `QKVParallelLinear` 都继承自 `ColumnParallelLinear`，它们的 `weight_loader` 最大区别是什么？

**答案**：前者两段等宽，`shard_offset` 用通用公式 `sum(output_sizes[:id]) // tp` 计算、`shard_id` 是整数；后者三段不等宽（GQA），`shard_offset`/`shard_size` 用头数专门计算、`shard_id` 是字符串 `"q"/"k"/"v"`。

**练习 2**：若把 `output_sizes` 写成 `[8, 16]`（不等宽），`tp_size=2` 时还能正确工作吗？

**答案**：能。第 89-90 行的公式对不等宽也成立：段 0 offset=0/size=4，段 1 offset=8//2=4/size=16//2=8。但前向把两段一起 `split` 回 gate/up 时要求模型代码知道边界，实际 Qwen3 的两段等宽，这里只是说明加载逻辑本身允许不等宽。

---

### 4.5 VocabParallelEmbedding 与 ParallelLMHead：词表切分

#### 4.5.1 概念说明

Embedding 层的权重形状是 `[vocab_size, hidden_size]`，可以看作「每个 token 一行」。词表并行就是**把词表（行）切成 `tp_size` 段**，每个 rank 只存自己那段 token 的 embedding。

- 查表时（`VocabParallelEmbedding.forward`）：某 token 只属于一个 rank 的区间，其余 rank 查到的是「无效」，用 mask 屏蔽后 `all_reduce` 求和——因为只有持有该 token 的 rank 贡献非零项，求和等价于「正确的 embedding 广播给所有 rank」。
- 算 logits 时（`ParallelLMHead.forward`）：logits = hidden @ weight^T，每个 rank 算出**本段词表**的 logits 分片。采样只需在 rank 0 做，所以用 `gather` 把各 rank 的 logits 分片**收集到 rank 0** 拼成完整 `[..., vocab]`，其他 rank 得到 `None`。

> 为什么 Embedding 用 `all_reduce` 而 LMHead 用 `gather`？因为 Embedding 的输出要喂给后续每一层、**每个 rank 都需要**完整结果；而 logits 只用于采样、**只有 rank 0 需要**，用 `gather` 更省通信。

#### 4.5.2 核心流程

```
VocabParallelEmbedding:
  每 rank 持有 [vocab_start_idx, vocab_end_idx) 的 embedding 行
  forward(x):
    mask = token 是否落在本 rank 区间
    把全局 token id 重映射成本地行号 (x - vocab_start_idx)，越界处置 0
    y = F.embedding(x, weight)        # 查本 rank 的表
    y = mask * y                       # 非本 rank 的 token 置零
    all_reduce(y)                      # 求和 → 每个 token 拿到正确 embedding

ParallelLMHead(继承 VocabParallelEmbedding):
  forward(x):
    (prefill 时先取每序列最后一个 token，靠 cu_seqlens_q)
    logits = F.linear(x, weight)       # 每 rank 算本段词表的 logits
    gather 到 rank 0，沿最后一维 cat    # rank 0 得到完整 [..., vocab]
```

#### 4.5.3 源码精读

[nanovllm/layers/embed_head.py:9-42](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/embed_head.py#L9-L42) —— `VocabParallelEmbedding`。构造（17-25 行）计算本 rank 的词表区间 `[vocab_start_idx, vocab_end_idx)` 与本地权重形状 `[num_embeddings/tp, hidden]`。

[nanovllm/layers/embed_head.py:34-42](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/embed_head.py#L34-L42) —— 前向是词表并行的精髓：

- 第 36 行 `mask = (x >= vocab_start_idx) & (x < vocab_end_idx)`：标记哪些 token 归本 rank 管。
- 第 37 行 `x = mask * (x - vocab_start_idx)`：归我管的 → 重映射成本地行号；不归我管的 → mask=0 乘出 0（查第 0 行，稍后被屏蔽）。
- 第 38 行查表，第 40 行 `mask.unsqueeze(1) * y` 把不归本 rank 的结果置零。
- 第 41 行 `all_reduce`：所有 rank 求和，每个 token 最终拿到唯一非零的 embedding。

[nanovllm/layers/embed_head.py:45-66](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/embed_head.py#L45-L66) —— `ParallelLMHead` 继承 `VocabParallelEmbedding`（第 54 行 `super().__init__` 复用词表切分）。它的 `forward` 重写：

- 第 57-60 行：prefill 时按 `cu_seqlens_q[1:] - 1` 抽取每序列最后一个 token（与 u4-l3 讲的 Context 机制一致），把 logits 计算量从「全部 token × 词表」降到「序列数 × 词表」。
- 第 61 行 `logits = F.linear(x, self.weight)`：每 rank 算本段词表的 logits（注意 LMHead 用 `F.linear` 即 `x @ weight.T`，与 Embedding 的查表 `F.embedding` 区分）。
- 第 62-65 行：`tp_size > 1` 时 `gather` 到 rank 0，rank 0 把 `tp_size` 份沿最后一维 `cat` 成完整 logits；其他 rank 得 `None`。

使用方：

[nanovllm/models/qwen3.py:169](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py#L169) —— `embed_tokens = VocabParallelEmbedding(vocab_size, hidden_size)`。

[nanovllm/models/qwen3.py:201-203](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py#L201-L203) —— `lm_head = ParallelLMHead(vocab_size, hidden_size)`，且 `tie_word_embeddings=True` 时让 `lm_head.weight` 与 `embed_tokens.weight` **共享同一块权重**（权值绑定），所以词表切分只需做一次。

#### 4.5.4 代码实践

**实践目标**：验证 `tp_size=1` 时 `VocabParallelEmbedding` 等价普通 `nn.Embedding`、`ParallelLMHead` 等价普通线性投影。

**操作步骤**：

```python
# 示例代码：验证词表并行退化情形
import os, torch, torch.distributed as dist
from nanovllm.layers.embed_head import VocabParallelEmbedding

os.environ.setdefault("MASTER_ADDR", "127.0.0.1")
os.environ.setdefault("MASTER_PORT", "29505")
dist.init_process_group("gloo", rank=0, world_size=1)

emb = VocabParallelEmbedding(num_embeddings=100, embedding_dim=8)
full = torch.randn(100, 8)
emb.weight.weight_loader(emb.weight, full)
x = torch.tensor([3, 17, 99])
print(torch.equal(emb(x), full[x]))   # 期望 True
```

**需要观察的现象**：`tp_size=1` 时 `vocab_start=0, vocab_end=100`，mask 全真，`all_reduce` 跳过，等价于直接查整张表。

**预期结果**：`True`。

> 关于 `ParallelLMHead` 的 prefill 抽尾 token 行为，因为它依赖 `get_context()` 返回的 Context 对象，单独实例化较繁琐，建议作为**源码阅读型实践**：对照 u4-l3 的 Context 字段，手动追踪 [nanovllm/layers/embed_head.py:56-66](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/embed_head.py#L56-L66) 中 `is_prefill`、`cu_seqlens_q`、`gather` 三条路径各自的作用。多 rank 行为**待本地验证**（需双卡环境）。

#### 4.5.5 小练习与答案

**练习 1**：`VocabParallelEmbedding.forward` 里如果不做 `mask * y`（第 40 行），`all_reduce` 后结果会怎样？

**答案**：不归本 rank 的 token 会查到本地表里某一行（重映射后落在 0 附近的随机行）的非零 embedding，`all_reduce` 求和后这些「假」embedding 会叠加到正确结果上，导致错误。`mask * y` 保证每 token 只有一个 rank 贡献非零项。

**练习 2**：`ParallelLMHead` 为什么用 `gather`（收集到 rank 0）而不是 `all_reduce`？

**答案**：logits 各段是**不同 token 的分数**（需拼接，而非求和），且只有 rank 0 负责采样（见 u5-l3 多 rank 协同），其他 rank 不需要完整 logits。`gather` 恰好「拼接到 rank 0」，比 `all_gather`（每 rank 都拼一份）更省。

---

## 5. 综合实践

把本讲五个模块串起来，完成一次「**手算 + 代码验证**」的张量并行分片推演。

**任务**：考虑一个简化 attention 层，全局参数 `hidden=64, head_dim=8, num_heads=8, num_kv_heads=4`，MLP 的 `intermediate=128`，`vocab=256`，`tp_size=2`。

1. **推演（纸笔）**：对 `qkv_proj`、`o_proj`、`gate_up_proj`、`down_proj`、`embed_tokens` 五个并行层，分别填出下表：

   | 层 | 类型 | 本 rank 权重形状 | 关键切分维度 | 是否需要通信 |
   |---|---|---|---|---|
   | qkv_proj | QKVParallelLinear | ? | dim 0 | 否 |
   | o_proj | RowParallelLinear | ? | dim 1 | all_reduce |
   | gate_up_proj | MergedColumnParallelLinear | ? | ? | ? |
   | down_proj | ? | ? | ? | ? |
   | embed_tokens | ? | ? | dim 0 (词表) | ? |

   提示：每 rank `num_heads=4, num_kv_heads=2`，`qkv_proj` 行数 = `(4+2*2)*8=64`，列 `64` → `[64,64]`；`o_proj` 输入被列并行分掉一半 → `[64, 32]`。

2. **代码验证（CPU, tp_size=1）**：仿照 4.3.4 的脚本，构造 `QKVParallelLinear(64, 8, 8, 4)`，灌入随机的 q/k/v，断言合并结果；再改 `head_dim=8, num_heads=8` 推算 `tp_size=2` 下 rank 0 的 q 段持有 q 的第 0~31 行、k 段持有 k 的第 0~15 行、v 段持有 v 的第 0~15 行。

3. **观察与结论**：用一句话总结「为什么 attention 块和 MLP 各只需要一次 `all_reduce`」。

**预期结论**：列并行（`qkv_proj`/`gate_up_proj`）把特征分散、行并行（`o_proj`/`down_proj`）把特征收拢并在收拢点 `all_reduce`，因此每对「列→行」组合恰好一次通信。词表并行中 Embedding 用 `all_reduce` 复制、LMHead 用 `gather` 汇总到 rank 0。

> 多卡实测部分**待本地验证**（需 `tensor_parallel_size=2` 的双 GPU 环境）；纸笔推演与 tp_size=1 代码验证可在任意带 torch 的环境完成。

## 6. 本讲小结

- 所有并行线性层继承 `LinearBase`，靠 `self.weight.weight_loader = self.weight_loader` 把「分片加载策略」挂到 Parameter 上，让 [loader.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/utils/loader.py) 用 `getattr(param, "weight_loader")` 统一调度，前向与加载彻底解耦。
- **列并行**（`ColumnParallelLinear`，`tp_dim=0`）切输出特征，各 rank 算互不重叠的输出段，**无需通信**；**行并行**（`RowParallelLinear`，`tp_dim=1`）切输入特征，各 rank 算部分和，**必须 `all_reduce`**，且偏置只在 rank 0 加一次。
- `QKVParallelLinear` 在列并行基础上用 `loaded_shard_id ∈ {"q","k","v"}` 处理 GQA 下**不等宽**三段，按本 rank 头数算 `shard_offset/shard_size`，把 HF 的三个投影合并成一个 `qkv_proj`。
- `MergedColumnParallelLinear` 用整数 `shard_id` 处理**等宽**的 gate/up 两段，offset 用 `sum(output_sizes[:id]) // tp` 通用公式。
- `VocabParallelEmbedding` 按词表行切分、查表用 mask + `all_reduce` 复制结果；`ParallelLMHead` 继承它、算 logits 用 `gather` 汇总到 rank 0，并复用 Context 在 prefill 时只取每序列最后一个 token。
- 经典「列→行」配对让一次 attention 块（`qkv_proj`→`o_proj`）和一次 MLP（`gate_up_proj`→`down_proj`）各只发生一次 `all_reduce`，是 Megatron-TP 的省通信核心。

## 7. 下一步学习建议

- **承接 u5-l1（CUDA Graph）**：本讲的并行层是 CUDA Graph 捕获的计算图主体，`RowParallelLinear` 里的 `all_reduce` 与 `ParallelLMHead` 的 `gather` 都会被录进图中，建议结合 u5-l1 理解「图回放时 NCCL 通信如何工作」。
- **承接 u5-l3（多进程与共享内存 IPC）**：本讲反复出现的 `dist.get_world_size()` / `all_reduce` / `gather` 依赖每个 rank 是独立进程且建立了 NCCL 进程组，u5-l3 会讲这些进程如何被 `spawn` 拉起、rank 0 如何用共享内存广播调用。
- **回顾 u5-l4（权重加载）**：本讲的 `weight_loader` 与 `packed_modules_mapping` 是加载体系的「执行端」，u5-l4 会从 `load_model` 的「调度端」统揽全局，建议两讲对照阅读。
- **建议继续阅读的源码**：把 [nanovllm/layers/linear.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/linear.py) 与 [nanovllm/layers/embed_head.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/embed_head.py) 并排放，体会 vLLM 风格的「基类 + weight_loader 钩子」如何用极简代码表达复杂的分片语义。

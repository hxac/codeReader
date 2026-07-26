# Engram 哈希与融合权重

## 1. 本讲目标

u6-l1 与 u6-l2 已经把 engram 门控的「主菜」——前向 kernel 与反向 kernel——讲透了。本讲是 engram 模块的收尾篇，专讲两道「配菜」：它们不参与门控的核心数学，但缺了它们，主 kernel 要么不知道查哪些嵌入，要么得多干一遍重复活。

这两道配菜对应两个独立的最小模块：

1. **`engram_hash`**：把一段 token 序列（n-gram）通过**滚动异或哈希**映射成一组嵌入表下标，供后续查表。
2. **`fused_weight`**：把两个 RMSNorm 权重 \(w_h\)、\(w_e\) 在前向开始时**预乘**为一个 fp32 张量 \(w_h\cdot w_e\)，让门控主 kernel 只读一份权重、并把「与 token 无关的乘法」搬出每 token 的热循环。

学完本讲，你应当能够：

1. 写出 n-gram 滚动异或哈希的数学定义，并解释「为什么 `ngram_idx=0` 不产生输出、从 bigram 才开始产生」。
2. 读懂 `engram_hash_kernel` 的「一线程一 token」并行结构与 `% vocab_size + offset` 的拼接逻辑。
3. 用「把与 token 无关的乘法 \(w_h\cdot w_e\) 提到循环外」这一思路，定量解释 `fused_weight` 给门控主 kernel 省下了什么，以及它**为什么主要省的是算力与寄存器压力、而不是字节数**。
4. 区分 engram 模块里两个容易混淆的「权重融合/缓冲」概念：前向的 `fused_weight`（预乘产物，每步重算）与反向的 `main_grad`（fp32 梯度累加缓冲，跨步常驻）。

## 2. 前置知识

本讲默认你已经学过：

- **u2-l1 / u2-l2 / u2-l3**：TileLang 的 `@tilelang.jit` + `@T.prim_func` 三层骨架、`T.dynamic` 运行时符号、`alloc_local` 线程私有寄存器、`T.copy` 跨级搬运、`T.unroll`/`T.vectorized` 循环、`T.ceildiv`。
- **u6-l1**：engram 门控前向的数学（RMSNorm → 融合权重点积 → sigmoid 门控）、persistent kernel 与 SM 占用启发式。本讲讲的 `fused_weight` 正是 u6-l1 里反复出现的 `weight_fused` 的**生产者**。
- **u6-l2**：engram 门控反向与 `main_grad` 就地累加。本讲 4.3 会用 `main_grad` 作对比对象，帮你把两个概念彻底分开。

三个本讲要用到的术语回顾：

- **n-gram**：连续 \(n\) 个 token 组成的片段。一个长度为 `max_ngram_size` 的窗口里，包含 bigram、trigram … 等不同长度的前缀，每种长度对应若干张嵌入表。
- **滚动哈希（rolling hash）**：一种增量计算哈希的方式——每加入一个新元素，只需在旧哈希上做一次更新（本讲用的是异或），不必从头重算。
- **RMSNorm 权重**：形如 `(hc_mult, hidden_size)` 的逐通道缩放向量。门控里同时用到两份：`weight_hidden`（作用于隐状态 \(x\)）与 `weight_embed`（作用于键 \(k\)）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tile_kernels/engram/engram_hash_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_hash_kernel.py) | n-gram 滚动异或哈希 kernel 与 `engram_hash` wrapper：把 token 序列变成嵌入表下标。 |
| [tile_kernels/engram/engram_fused_weight_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_fused_weight_kernel.py) | 逐元素 `bf16 × bf16 → fp32` kernel 与 `fused_weight` wrapper：预乘两份 RMSNorm 权重。 |
| [tile_kernels/torch/engram.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/engram.py) | 纯 PyTorch 参考实现：`engram_hash_ref`（哈希对拍）与 `make_offsets`（由 vocab_sizes 算前缀和偏移）。 |
| [tile_kernels/modeling/engram/engram_gate.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/engram/engram_gate.py) | `EngramGateFn`（autograd 封装）：在 forward 里调 `fused_weight` 产出 `weight_fused`，并把它同时喂给前向与反向 kernel。 |
| [tile_kernels/engram/engram_gate_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_gate_kernel.py) | 门控主 kernel：在每 token 的内循环里只读一份 `weight_fused`（即 `fused_weight` 的产物），是 4.2「收益分析」的落点。 |

## 4. 核心概念与源码讲解

### 4.1 engram_hash：n-gram 滚动异或哈希

#### 4.1.1 概念说明

engram（engram，记忆痕迹）机制需要给每个 token 算出一组「上下文嵌入」的下标：它不是直接用当前 token id 查表，而是用**当前 token 与它前面若干 token 组成的 n-gram**去查一组嵌入表，从而把局部上下文记忆编码进嵌入。

具体地，给定一段长度为 `max_ngram_size` 的 token 窗口 \((t_0, t_1, \dots, t_{N-1})\)（\(N=\)`max_ngram_size`），我们要为它的每一种「前缀长度」\(\geq 2\)（即 bigram \((t_0,t_1)\)、trigram \((t_0,t_1,t_2)\)、…）各产生 `num_embed_table_per_ngram` 个嵌入下标，每张表有自己的词表大小 `vocab_size` 与起始偏移 `offset`。

把 n-gram 映射成下标需要一个**哈希函数**。engram 用的是最简单的滚动异或哈希：

\[
h_i = h_{i-1} \;\oplus\; (t_i \cdot m_i),\qquad h_{-1}=0
\]

其中 \(m_i\) 是每个位置独立的乘法因子（`multipliers`），\(\oplus\) 是按位异或。它的好处是：

- **可滚动**：加入 \(t_i\) 只需一次「乘 + 异或」，不必重算整个前缀。
- **可逆感强、碰撞可接受**：异或 + 大整数乘能把相邻 token 充分打散；再对词表大小取模落进表内，冲突概率受 `vocab_size` 控制。

对前缀长度 \(i\geq 1\)（即覆盖 \(t_0\dots t_i\) 的 \((i{+}1)\)-gram），第 \(j\) 张嵌入表的下标为：

\[
\text{idx}_{i,j} = (h_i \bmod V_{i-1,j}) + O_{(i-1)\cdot \text{tables}+j}
\]

注意两个「错位」：

- 单 token（\(i=0\)）**不产生**任何嵌入——所以输出只对 \(i\geq 1\) 写，`vocab_sizes` 的第一维长度是 `max_ngram_size - 1` 而非 `max_ngram_size`。
- `vocab_sizes` 用 `i-1` 索引、`offsets` 用平铺后的列号索引——因为不同前缀长度对应不同的表组。

每个 token 的输出列数为：

\[
\text{num\_out\_cols} = (\text{max\_ngram\_size}-1)\times \text{num\_embed\_table\_per\_ngram}
\]

#### 4.1.2 核心流程

整个 kernel 是「一线程一 token」的 embarrassingly parallel 结构，没有跨线程协作：

```
grid = (num_ngram_layers, ceildiv(num_tokens, 32))
每个线程处理 1 个 (layer, token)：
  1. 算出自己的 token_idx；越界则 thread_return
  2. 把本 layer 的 multipliers / vocab_sizes / offsets 拷进 local（小张量，每线程一份）
  3. 把本 token 的 ngram_token_ids 拷进 local
  4. hash = 0
  5. for ngram_idx in 0..max_ngram_size-1:           # T.unroll
       hash ^= x[ngram_idx] * multipliers[ngram_idx]  # 滚动异或
       if ngram_idx > 0:                              # bigram 起才输出
         for j in 0..tables-1:                        # T.unroll
           col = (ngram_idx-1)*tables + j
           output_local[col] = (hash % vocab_sizes[ngram_idx-1, j]) + offsets[col]
  6. 把 output_local 拷回全局 output[layer, token_idx, :]
```

两个要点：

- **第 2 步的冗余拷贝是刻意的**：`multipliers`/`vocab_sizes`/`offsets` 对同一 block 内 32 个线程完全相同，本可以放 shared memory 共享。但它们体积极小（本例下分别只有 3、16、16 个元素），用每线程 local 复制可以**省掉 shared memory 分配与 `sync_threads`**，是「以寄存器换同步」的简化取舍。
- **第 5 步用 `T.unroll`**：`max_ngram_size` 是编译期常数（默认 3），循环展开后是固定几条指令，便于编译器调度。

#### 4.1.3 源码精读

kernel 构造函数把四个**编译期参数**（`max_ngram_size`、`num_ngram_layers`、`num_embed_table_per_ngram`）烤进产物，`num_tokens` 则是运行时符号：

[engram_hash_kernel.py:19-22](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_hash_kernel.py#L19-L22) —— 声明 `num_tokens` 为动态符号，并从编译期参数派生 `threads`/`blk_m`/`num_out_cols`：每个 block 32 线程处理 32 个 token。

[engram_hash_kernel.py:24-31](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_hash_kernel.py#L24-L31) —— `prim_func` 的形参即哈希的全部输入输出，注意 `vocab_sizes` 与 `offsets` 的形状如何与 4.1.1 的「错位」对齐：`vocab_sizes` 第二维是 `max_ngram_size - 1`，`output` 最后一维是 `num_out_cols`。

[engram_hash_kernel.py:32-36](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_hash_kernel.py#L32-L36) —— 二维网格 `(num_ngram_layers, ceildiv(num_tokens, blk_m))`，每线程算自己的 `token_idx`，越界用 `T.thread_return()` 提前返回（这就是 wrapper 侧 `num_tokens > 0` 守卫之外的第二道边界保护，处理尾部不足 32 的那一组）。

[engram_hash_kernel.py:37-48](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_hash_kernel.py#L37-L48) —— 分配 5 个 local 缓冲与 1 个 `int64` 标量变量 `hash_local`，随后用 4 次 `T.copy` 把本 layer 的常量与本 token 的 id 装进寄存器。

[engram_hash_kernel.py:49-58](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_hash_kernel.py#L49-L58) —— **核心**：滚动异或哈希与「bigram 起输出」的逻辑。注意 `T.bitwise_xor` 与 `T.cast(x_local[ngram_idx], T.int64)`——哈希全程在 `int64` 上算，避免高位溢出丢失信息；取模后才写回 `int32` 的 `output_local`。

wrapper 侧是标准的「校验/分配/编译/启动」四步：

[engram_hash_kernel.py:84-95](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_hash_kernel.py#L84-L95) —— 从输入张量形状反推编译期参数，分配 `int32` 输出，按 `num_tokens > 0` 守卫启动 kernel。

#### 4.1.4 代码实践

**实践目标**：不看 `engram_hash_ref` 源码，凭 4.1.1 的数学定义自己用 PyTorch 写一个参考实现，再与 kernel 及官方参考三方对拍。

**操作步骤**：

1. 构造一组最小输入：`num_tokens=4`、`max_ngram_size=3`、`num_ngram_layers=2`、`num_embed_table_per_ngram=8`。
2. 手算第 0 个 token、第 0 层、trigram 列（`col=8..15`）对应的表达式（不必算出最终整数，写出公式即可）。
3. 写一个 `my_engram_hash_ref(ngram_token_ids, multipliers, vocab_sizes, offsets)`，关键是用一个累计的 `hashes` 张量在 `for i in range(1, max_ngram_size)` 里做 `bitwise_xor_`，每轮对当前词表取模。
4. 跑下面这段对拍脚本（**示例代码**，非项目原有）：

```python
# 示例代码：三方对拍 engram_hash
import torch
from tile_kernels.engram import engram_hash
from tile_kernels.torch.engram import engram_hash_ref, make_offsets
from tile_kernels.testing.numeric import assert_equal

N, L, T = 4, 2, 8           # num_tokens, num_ngram_layers, tables
max_ngram_size = 3
ids   = torch.randint(0, 100000, (N, max_ngram_size), dtype=torch.int32, device='cuda')
mults = torch.randint(0, 100000, (L, max_ngram_size), dtype=torch.int64, device='cuda')
vsizes= torch.randint(100000, 1000000, (L, max_ngram_size - 1, T), dtype=torch.int32, device='cuda')
offs  = make_offsets(vsizes)

out_k   = engram_hash(ids, mults, vsizes, offs)
out_ref = engram_hash_ref(ids, mults, vsizes, offs)
assert_equal(out_k, out_ref)          # kernel vs 官方参考
assert_equal(out_k, my_engram_hash_ref(ids, mults, vsizes, offs))  # kernel vs 你的参考
```

**需要观察的现象 / 预期结果**：因为哈希全程是整数运算（无浮点舍入），`assert_equal` 应做**位精确**判等并通过。若你写出的参考报错，最常见两个 bug：(a) 忘了 `hashes` 要从 `prod[:,:,0]` 起步、第一个 token 就进哈希；(b) 取模时 `vocab_sizes` 用错了层或错了前缀长度下标。

> 若本地无 GPU 或 TileLang 未就绪，无法实际运行 kernel，请明确标注「待本地验证」，但**手算公式与参考实现仍可在 CPU 上完成**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `vocab_sizes` 的形状第二维是 `max_ngram_size - 1` 而不是 `max_ngram_size`？

**参考答案**：单 token（`ngram_idx=0`）不产生嵌入，嵌入从 bigram（`ngram_idx=1`）才开始。所以前缀长度 \(i=1\dots \text{max\_ngram\_size}{-}1\) 各对应一组表，共 `max_ngram_size - 1` 组。

**练习 2**：把 `max_ngram_size` 从 3 改成 4、`num_embed_table_per_ngram` 仍是 8，每个 token 的输出列数是多少？哪些列对应 trigram？

**参考答案**：`num_out_cols = (4-1)×8 = 24`。trigram 是 `ngram_idx=2`，对应列号 `col = (2-1)×8 + j = 8..15`。

---

### 4.2 fused_weight：把两份 RMSNorm 权重预乘为一份

#### 4.2.1 概念说明

回顾 u6-l1 门控前向的「融合权重点积」：

\[
\text{raw\_dot} = \sum_{d} x_d \cdot w_{h,d} \cdot k_d \cdot w_{e,d}
\]

其中 \(w_h\)、\(w_e\) 是两份 RMSNorm 权重，\(\boldsymbol{x}\)、\(\boldsymbol{k}\) 是随 token 变化的隐状态与键。关键观察：**乘积 \(w_{h,d}\cdot w_{e,d}\) 与 token 无关**。于是由乘法结合律：

\[
\text{raw\_dot} = \sum_{d} (x_d \cdot k_d) \cdot \underbrace{(w_{h,d}\cdot w_{e,d})}_{\text{weight\_fused}}
\]

`fused_weight` 做的就是把 \(w_h\odot w_e\)（逐元素乘）**在前向开始时预算一次**，得到 fp32 张量 `weight_fused`；门控主 kernel 只需读这一份权重。它的收益不在「省字节」——`weight_fused` 是 fp32（4 字节/元素），而 `wh + we` 两份 bf16 合计也是 4 字节/元素——而在两点：

1. **算力搬运（主要）**：把「与 token 无关的乘法」\(w_h\cdot w_e\) 从每 token 的热循环里搬到循环外，每个 token 少算一次逐元素乘。
2. **权重流减半（次要）**：主 kernel 的内循环只需追踪**一条**权重流并让它常驻 SMEM（见 u6-l1 的「跨 token 常驻复用」），而非两条；SMEM 占用与寄存器压力随之下降，间接改善占用率。

而且 `weight_fused` 一份产物被**前向与反向两次复用**（见 4.2.3），等于把这次预乘的代价摊到两次 kernel 调用上。

#### 4.2.2 核心流程

kernel 是极简的逐元素 `bf16 × bf16 → fp32`：

```
hidden_size 必须被 blk_d=256 整除（threads=32 × vec_size=8）
grid = (hc_mult, hidden_size // 256)
每个线程处理连续 8 个元素（vec_size=8）：
  for i_k in vectorized(8):
    a_local[i_k] = weight_hidden[pid_h, base + tid*8 + i_k]   # bf16 → fp32
    b_local[i_k] = weight_embed[pid_h, base + tid*8 + i_k]    # bf16 → fp32
  for i_k in vectorized(8):
    weight_fused[pid_h, base + tid*8 + i_k] = a_local[i_k] * b_local[i_k]
```

设计要点：

- **`vec_size=8` + `T.vectorized`**：每个线程处理 8 个连续元素，对应一次 128 位向量化 load（8×bf16=16 字节，正好一条 128 位 load），store 同理。
- **`hidden_size % blk_d == 0` 断言**：保证网格整切 hidden 维，无需边界处理——这是把 `hidden_size` 作为编译期参数（而非运行时符号）的回报。
- **中间用 fp32**：两个 bf16 相乘先升到 fp32 再存 fp32，避免 bf16 乘法的中间精度损失，也匹配门控主 kernel 对 `weight_fused` 的 fp32 读取约定。

#### 4.2.3 源码精读

[engram_fused_weight_kernel.py:13-19](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram_fused_weight_kernel.py#L13-L19) —— 从编译期参数 `hidden_size` 派生 tiling 常量，`assert hidden_size % blk_d == 0` 保证无尾块处理。

[engram_fused_weight_kernel.py:21-35](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram_fused_weight_kernel.py#L21-L35) —— 两段 `T.vectorized`：第一段把两份 bf16 权重连续读进 `a_local`/`b_local`（fp32），第二段逐元素相乘写回 fp32 输出。

[engram_fused_weight_kernel.py:40-59](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram_fused_weight_kernel.py#L40-L59) —— wrapper `fused_weight`：分配 fp32 输出并启动 kernel。注意这里**没有** `num_tokens > 0` 那类零规模守卫，因为权重形状恒大于 0。

**它如何被消费**——这是理解收益的关键。门控主 kernel 的每 token 内循环只读一份 `weight_fused`：

[engram_gate_kernel.py:108-116](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_gate_kernel.py#L108-L116) —— 内循环里 `w_local[i_k] = weight_fused[...]` 只取一份权重，随即累加 `x * w * k`。若没有融合，这里要同时读 `wh`、`we` 两条流、并多做一次 `wh·we` 的逐元素乘。

而在 autograd 封装层，`weight_fused` 在 forward 里算一次，**同时**喂给前向与反向：

[engram_gate.py:44-52](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/engram/engram_gate.py#L44-L52) —— `weight_fused = fused_weight(...)` 后立即传给 `engram_gate_fwd`，并存进 `ctx.save_for_backward`。

[engram_gate.py:67-70](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/engram/engram_gate.py#L67-L70) —— `engram_gate_bwd` 同样接收同一份 `weight_fused`，一次预乘两次复用。

#### 4.2.4 代码实践

**实践目标**：定量说明 `fused_weight` 省的是算力与权重流条数，而非字节数。

**操作步骤**：

1. 读 [engram_gate_kernel.py:108-116](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_gate_kernel.py#L108-L116)，数一数融合后每元素每 token 的内循环做了几次乘法、读几条权重流。
2. 假想一个「不融合」版本：把 `weight_fused[...]` 换成同时读 `wh[...]` 和 `we[...]`，并写成 `x * (wh * we) * k`。数一下乘法次数与权重流条数。
3. 填下面这张对比表（**示例表格**，请自行推导后填写）：

| 方案 | 每元素每 token 乘法次数 | 权重流条数 | 权重字节数/元素 |
| --- | --- | --- | --- |
| 融合（现状） | 2（`x·w`、`·k`） | 1（`weight_fused`，fp32） | 4 |
| 不融合 | 3（`wh·we`、`x·wh·we`、`·k`） | 2（`wh`、`we`，各 bf16） | 2+2=4 |
| 预算在 fused_weight kernel | 1（每元素一次性 `wh·we`） | — | — |

**需要观察的现象 / 预期结果**：权重**字节数/元素**两方案都是 4，所以 `fused_weight` **不是**一个带宽优化；它的收益是 (a) 每 token 少一次逐元素乘（算力）、(b) 权重流从 2 条降为 1 条（寄存器/SMEM 压力）。再结合 `weight_fused` 跨全部 token 常驻复用、且前向反向各用一次，预乘的代价被极大摊薄。

> 若本地有 GPU，可设 `TK_PRINT_KERNEL_SOURCE=1` 跑 `pytest tests/engram/test_engram_fused_weight.py`，对照生成的 CUDA 源码确认 `vec_size=8` 的向量化 load/store。无 GPU 则标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：既然字节数没变，为什么 `fused_weight` 仍然值得做？

**参考答案**：主收益是**算力搬运**——把与 token 无关的 \(w_h\cdot w_e\) 移出每 token 的热循环；次收益是权重流从 2 条降为 1 条，降低寄存器与 SMEM 压力。此外 `weight_fused` 一份产物被前向、反向两次复用，预乘代价被进一步摊薄。

**练习 2**：把输出 dtype 从 fp32 改成 bf16 会怎样？

**参考答案**：会让后续门控主 kernel 读取时重新引入 bf16 的中间精度损失（u6-l1 的点积在 fp32 累加，权重精度下降会直接影响 `raw_dot` 的数值），从而与 `engram_gate_ref` 的 fp32 参考对不上。fp32 是为了与主 kernel 的 fp32 累加路径保持精度一致。

---

### 4.3 易混辨析：fused_weight vs main_grad

engram 模块里有两个都涉及「权重」的优化，初学者极易混淆，务必分清：

| 维度 | `fused_weight`（本讲） | `main_grad`（u6-l2 / u8-l1） |
| --- | --- | --- |
| 所处阶段 | **前向**开始时 | **反向**结束时 |
| 是什么 | 两份 RMSNorm 权重的**预乘产物** `wh·we`（fp32） | 参数自带的 **fp32 梯度累加缓冲** |
| 生命周期 | **每步重算**，用完即弃 | **跨步常驻**，梯度持续 `+=` 累加 |
| 解决的问题 | 把与 token 无关的乘法搬出热循环 | 分布式/梯度累积下避免临时 `.grad` 反复分配与重复累加 |
| 在 autograd 里的体现 | forward 调 `fused_weight` 产出，forward/backward 共用 | backward 检测到 `main_grad` 就地 `+=` 并对该参数返回 `None` |

一句话区分：**`fused_weight` 是「前向预乘」算子产物，`main_grad` 是「反向累加」梯度缓冲**。它们恰好覆盖一个可训练 engram 层的前向与反向两端，互不冲突——同一个 `weight_hidden` 既会被 `fused_weight` 预乘（前向），它的梯度又可能被累加进自身的 `main_grad`（反向）。

## 5. 综合实践

把本讲两个模块与 u6-l1/u6-l2 串起来，完成下面这个端到端的「数据流追踪」任务：

1. **哈希链路**（4.1）：选 `num_tokens=2`、`max_ngram_size=3`，自己用 PyTorch 写出 `my_engram_hash_ref`，对拍 `engram_hash` kernel 通过；再手写出第 0 个 token、第 0 层全部 16 个输出列的公式（不必算最终整数）。
2. **融合权重链路**（4.2）：读 [engram_gate.py:36-55](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/engram/engram_gate.py#L36-L55)，画出从 `weight_hidden`/`weight_embed` → `fused_weight` → `weight_fused` →（`engram_gate_fwd` + `ctx.save_for_backward`）→ `engram_gate_bwd` 的完整流向图，标注 `weight_fused` 在哪两个位置被读取。
3. **辨析**（4.3）：在同一张图上，用另一种颜色标出 `main_grad` 的流向（`engram_gate_bwd` → `grad_w_reduce` 就地 `+=` → 返回 `None`），并写一句话说明它为什么和 `weight_fused` 不会互相干扰。

**预期产出**：一张标注完整的数据流图 + 一段对 `fused_weight` 收益的定量说明（算力/权重流/字节三者分别如何变化）。无 GPU 环境下，第 1 步的参考实现与对拍可在 CPU 上完成，kernel 调用部分标注「待本地验证」。

## 6. 本讲小结

- `engram_hash` 用**滚动异或哈希** \(h_i=h_{i-1}\oplus(t_i\cdot m_i)\) 把 n-gram 映射成嵌入表下标，bigram（`ngram_idx≥1`）起才输出，每列下标为 `(hash % vocab_size) + offset`。
- kernel 是「一线程一 token」的 embarrassingly parallel 结构；小常量故意拷进每线程 local，以「寄存器换同步」省掉 shared memory 与 `sync_threads`。
- `fused_weight` 在前向预算 \(w_h\odot w_e\)（bf16×bf16→fp32），让门控主 kernel 只读一份权重、把与 token 无关的乘法搬出热循环；它**主要省算力与权重流条数，不省字节**（fp32 与两份 bf16 都是 4 字节/元素）。
- 一份 `weight_fused` 被 `engram_gate_fwd` 与 `engram_gate_bwd` **两次复用**，预乘代价被摊薄。
- `fused_weight`（前向预乘产物，每步重算）与 `main_grad`（反向梯度累加缓冲，跨步常驻）是 engram 模块里两个互不冲突的「权重」优化，分别覆盖前向与反向。
- 这两个 kernel 都开启了 `TL_DISABLE_WARP_SPECIALIZED`（过于简单、无需生产-消费 warp 特化）；`engram_hash` 额外开 `TL_DISABLE_VECTORIZE_256`（int32/int64 散列访问不适用 256 位向量化）。

## 7. 下一步学习建议

- 想看「autograd 如何把 `fused_weight`、`engram_gate_fwd/bwd`、`main_grad` 串成一个可训练层」的完整封装，进入 **u8-l1（torch.autograd.Function 封装范式）**，以 `EngramGateFn` 为范例。
- 想系统了解 `pass_configs` 里 `TL_DISABLE_WARP_SPECIALIZED`、`TL_DISABLE_VECTORIZE_256` 等开关的含义与取舍，进入 **u10-l2（TMA、向量化、布局与 pass_configs）**。
- 想练习「kernel + wrapper + torch 参考 + pytest 四件套」的端到端落地（本讲的哈希与融合权重正是极佳的简单范例），进入 **u10-l3（新增一个算子的完整流程）**。

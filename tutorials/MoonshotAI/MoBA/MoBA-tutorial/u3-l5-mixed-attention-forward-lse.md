# u3-l5 MixedAttention 前向：两路注意力与 LSE 在线合并

## 1. 本讲目标

上一讲（u3-l4）我们把稀疏的块选择掩码 `gate_mask` 重组成了 flash-attn varlen 接口能直接消化的张量：`moba_q`、`moba_kv`、`moba_cu_seqlen_q/kv` 和地址簿 `moba_q_sh_indices`。本讲进入 `MixedAttention.forward`，回答最后两个问题：

1. **两路注意力如何各算各的**：self-attn 支路为什么能直接复用 `cu_chunk` 作为 varlen 边界？moba 支路为什么敢用 `causal=False`？
2. **两路结果如何数学精确地合并**：为什么用 LSE（log-sum-exp）而不是简单地「两个输出相加除以二」？`index_reduce` / `index_add_` / `factor` 这些张量操作各自扮演什么角色？

学完本讲，你应当能：

- 写出 LSE 合并两条注意力的公式，并解释它等价于「把两组 key 拼在一起做一次 softmax」。
- 逐行说出 `MixedAttention.forward` 中每一步张量的形状与用途。
- 理解「减去 max_lse 防溢出」和「最终把 max_lse 加回去」这一对操作各自为谁服务（前向数值稳定 / 反向可用）。

## 2. 前置知识

### 2.1 softmax 注意力的「分子 / 分母」视角

对单个 query \( q \)、一组打分 \( s_i = q \cdot k_i / \sqrt{d} \)，softmax 注意力输出是：

\[ \text{out} = \frac{\sum_i e^{s_i} v_i}{\sum_i e^{s_i}} \]

把分母单独取出来叫 **LSE（log-sum-exp）**：

\[ \mathrm{lse} = \log \sum_i e^{s_i} \]

flash-attn 内核返回的 `softmax_lse` 就是它（内部用减最大值技巧算，但返回的是未偏移的真实值）。**有了每条路径的输出和 LSE，就能在没有中间打分矩阵的情况下合并两条路径**——这正是本讲的核心。

### 2.2 LSE 合并公式（本讲的数学主干）

设路径 A（self-attn 支路）覆盖 key 集合 \( A \)，路径 B（moba 支路）覆盖 key 集合 \( B \)，两路输出各自由自己的 softmax 归一化。那么把 \( A \cup B \) 一起做 softmax 的结果可以只用两路的 `out` 和 `lse` 恢复出来：

\[ \mathrm{lse}_{A \cup B} = \log\left( e^{\mathrm{lse}_A} + e^{\mathrm{lse}_B} \right) \]

\[ \text{out}_{A \cup B} = e^{\mathrm{lse}_A - \mathrm{lse}_{A \cup B}} \cdot \text{out}_A + e^{\mathrm{lse}_B - \mathrm{lse}_{A \cup B}} \cdot \text{out}_B \]

直觉：\( e^{\mathrm{lse}} \) 是该路径所有 \( e^{s_i} \) 的「总权重」，合并后的输出就是按各自总权重重新分配的两路加权和。由于 \( e^{s_i} \) 可能非常大（bf16 下直接 exp 会溢出），实际计算时要先减去一个最大值 \( m = \max(\mathrm{lse}_A, \mathrm{lse}_B) \)：

\[ \mathrm{lse}_{A \cup B} = m + \log\left( e^{\mathrm{lse}_A - m} + e^{\mathrm{lse}_B - m} \right) \]

这就是源码里「先减 max_lse、log 完再加回去」的全部原因。这套技巧也叫 **online softmax**（FlashAttention 论文的分块 softmax 就是它）。

### 2.3 三个索引操作的语义

| 操作 | 语义 | 在本讲中的用途 |
|---|---|---|
| `index_reduce(0, idx, src, "amax")` | 把 `src` 按 `idx` 散射到自身，重复落点取最大 | 求每个 (s,h) 位置的 max_lse |
| `index_select(0, idx)` | 按 `idx` 收集（gather） | 把全局量按地址簿抄到 moba 段 |
| `index_add_(0, idx, src)` | 把 `src` 按 `idx` 散射**累加** | 合并指数权重 / 梯度式的多对一聚合 |

回忆 u3-l4：`moba_q_sh_indices` 是地址簿，把 moba 段里的每个元素映射回输出缓冲 `[S, H, D]` 展平后的下标 \( s \cdot H + h \)。**同一个 (s,h) 位置会被多个选中块各写一次**，所以合并阶段必须用「累加」而非「覆盖」。

### 2.4 flash-attn 的内部 API 与 LSE 布局

项目直接 import 了 flash-attn 2.6.3 的私有函数 `_flash_attn_varlen_forward`（见 [moba/moba_efficient.py:L6-L9](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L6-L9)）。它的返回值是 8 元组，其中第 5、6 个是本讲主角：注意力输出 `out`（`[total, H, D]`）与 `softmax_lse`（**head 优先**布局 `[H, total]`）。变量名后缀 `_hs` / `_sh` 就是标注这两种布局——这是下一行 `.t()` 存在的原因。

## 3. 本讲源码地图

| 文件 | 本讲关注的范围 | 作用 |
|---|---|---|
| `moba/moba_efficient.py` | `MixedAttention` 类（L67-L183 为前向） | 本讲全部内容：两次 flash-attn 前向 + LSE 合并 |
| `moba/moba_efficient.py` | `moba_attn_varlen` 尾部（L425-L443） | 上一讲产物如何被打包传进 `MixedAttention.apply` |
| `moba/moba_efficient.py` | `calc_chunks`（L14-L64） | 只复用其产物 `cu_chunk`（u3-l2 已精读） |
| `moba/moba_efficient.py` | `MixedAttention.backward`（L185-L267） | 只看 L168「加回 max_lse」为反向保存了什么，详细留给 u3-l6 |
| flash-attn 2.6.3 `flash_attn/flash_attn_interface.py` | `_flash_attn_varlen_forward` 签名 | 理解返回值顺序与 LSE 形状（需读本机安装的包，待本地验证） |

## 4. 核心概念与源码讲解

### 4.1 两路注意力：`_flash_attn_varlen_forward` 的两次调用

#### 4.1.1 概念说明

回顾 u3-l1 的分工：每个 query 的注意力被拆成两块——

- **self-attn 支路**：query 所在的「当前块」（每个 batch 的最后一块）内部做**因果**注意力。注意不是只算最后一块里的 query——所有 query 都会在自己的当前块里算一遍块内因果注意力。
- **moba 支路**：gate 选中的**历史整块**，对每个 (query, 块) 对做**非因果**注意力（因果性已在 u3-l3 的 gate 掩码里前置完成）。

两路用同一组 key 时会重叠（当前块既在 self 支路出现、也可能作为历史块被别的 query 选中），没关系——LSE 合并是精确加法，重叠部分等价于被算了一次「合起来的 softmax」。

#### 4.1.2 核心流程

```
输入: q [S,H,D], k/v 同形; cu_chunk (块边界); moba_q/moba_kv/moba_cu_seqlens (u3-l4 产物)

self 支路:
  _flash_attn_varlen_forward(
      q, k, v,
      cu_seqlens_q = cu_seqlens_k = cu_chunk,   # 每块是一条 varlen "序列"
      max_seqlen_k = max_seqlen,
      causal = True)                             # 块内因果
  → self_attn_out_sh [S,H,D], self_attn_lse_hs [H,S]

moba 支路:
  _flash_attn_varlen_forward(
      moba_q [N_sel,1,D], moba_kv[:,0], moba_kv[:,1],
      cu_seqlens_q = moba_cu_seqlen_q,           # 每段是一条 (块×head) 序列
      cu_seqlens_k = moba_cu_seqlen_kv,
      max_seqlen_k = moba_chunk_size,
      causal = False)
  → moba_attn_out [N_sel,1,D], moba_attn_lse_hs [H,N_sel]
```

关键点：**self 支路的 varlen 边界是 `cu_chunk` 而非 `cu_seqlens`**。`cu_chunk` 里存的是每块的起始偏移（u3-l2 已推过），把它当作 `cu_seqlens` 传入，flash-attn 就会把「每个块」当成一条独立短序列做因果注意力——跨块的 key 天然不可见，块内下三角天然成立。这是零成本复用 varlen 内核的巧妙之处，不需要构造任何掩码。

moba 支路里 `moba_kv` 的形状是 `[有效段数 × chunk_size, 2, 1, D]`（u3-l4 末尾 `flatten + unsqueeze(2)` 的产物），所以 `moba_kv[:, 0]` 是 K、`moba_kv[:, 1]` 是 V。`max_seqlen_k = moba_chunk_size` 因为候选块全是满块（u3-l2 剔除了各 batch 的尾块）。两支路共用同一个 `softmax_scale = D^{-1/2}`。

#### 4.1.3 源码精读

ctx 保存两个标量参数，并按 head_dim 推导 softmax 缩放（[moba/moba_efficient.py:L84-L86](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L84-L86)）：

```python
ctx.max_seqlen = max_seqlen
ctx.moba_chunk_size = moba_chunk_size
ctx.softmax_scale = softmax_scale = q.shape[-1] ** (-0.5)
```

self-attn 支路：`cu_seqlens_q/k` 都传 `self_attn_cu_seqlen`（即调用方的 `cu_chunk`），`causal=True`（[moba/moba_efficient.py:L88-L102](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L88-L102)）：

```python
_, _, _, _, self_attn_out_sh, self_attn_lse_hs, _, _ = (
    _flash_attn_varlen_forward(
        q=q, k=k, v=v,
        cu_seqlens_q=self_attn_cu_seqlen,
        cu_seqlens_k=self_attn_cu_seqlen,
        max_seqlen_q=max_seqlen, max_seqlen_k=max_seqlen,
        softmax_scale=softmax_scale,
        causal=True, dropout_p=0.0,
    )
)
```

moba 支路：对重组后的 varlen 序列做非因果注意力（[moba/moba_efficient.py:L104-L116](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L104-L116)）：

```python
_, _, _, _, moba_attn_out, moba_attn_lse_hs, _, _ = _flash_attn_varlen_forward(
    q=moba_q,
    k=moba_kv[:, 0],          # K：堆叠张量拆一半
    v=moba_kv[:, 1],          # V：另一半
    cu_seqlens_q=moba_cu_seqlen_q,
    cu_seqlens_k=moba_cu_seqlen_kv,
    max_seqlen_q=max_seqlen,
    max_seqlen_k=moba_chunk_size,   # 候选块恒为满块，长度就是 chunk_size
    softmax_scale=softmax_scale,
    causal=False,             # 因果性已由 gate 掩码前置完成（u3-l3）
    dropout_p=0.0,
)
```

随后一行容易被忽略但必不可少——LSE 布局转换（[moba/moba_efficient.py:L118-L120](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L118-L120)）：

```python
# convert lse shape hs -> sh ( follow the legacy mix attn logic )
self_attn_lse_sh = self_attn_lse_hs.t().contiguous()
moba_attn_lse = moba_attn_lse_hs.t().contiguous()
```

flash-attn 返回的 LSE 是 head 优先 `[H, total]`（后缀 `_hs`），而后面所有索引操作都在 `[S, H, ...]` 展平的空间（下标 \( s \cdot H + h \)）里进行，所以先 `.t()` 成位置优先 `_sh` 布局。`backward` 里 L214 的 `mixed_attn_vlse_sh.t().contiguous()` 又把它转回去喂给内核——一来一回互为印证。

#### 4.1.4 代码实践

1. **实践目标**：确认本机 flash-attn 2.6.3 的 `_flash_attn_varlen_forward` 返回值顺序与 LSE 布局。
2. **操作步骤**：运行

   ```bash
   python -c "import inspect, flash_attn.flash_attn_interface as f; print(inspect.getsource(f._flash_attn_varlen_forward))"
   ```

3. **需要观察的现象**：返回的 8 元组中第 5、6 个元素分别是输出和 `softmax_lse`；`softmax_lse` 的形状定义（`[nheads, total]` 还是 `[total, nheads]`）。
4. **预期结果**：`softmax_lse` 为 head 优先 `[H, total]`，与源码 `.t()` 的用法一致。若与你本机版本不符，说明 flash-attn 版本不是 2.6.3（本项目精确锁定该版本，见 u1-l2）。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：self 支路为什么必须 `causal=True`，而 moba 支路可以 `causal=False`？

**答案**：self 支路把「每个块」当作一条 varlen 序列，块内 query 位置与 key 位置一一对应，必须用下三角掩码保证块内因果；moba 支路的 key 是**完整的历史块**，u3-l3 中 `gate_chunk_end_mask` 已经保证只有位于块末之后的 query 才可能选中该块（`chunk_end ≤ s`），所以 (query, 块) 对本身天然满足因果，无需再掩码。

**练习 2**：如果把 self 支路的 `cu_seqlens_q` 换成原始 `cu_seqlens`（batch 边界），语义会发生什么变化？

**答案**：flash-attn 会把整个 batch 当一条序列做全量因果注意力，self 支路就不再是「当前块内注意力」，而是覆盖了所有 key，moba 支路变成纯粹的重复计算，LSE 合并后等价于全量注意力 + 选块再算一遍（同一 key 被计入两次），结果错误且更慢。

### 4.2 `max_lse_1d`：用 `index_reduce` 求逐位置最大 LSE

#### 4.2.1 概念说明

合并公式里需要 \( m = \max(\mathrm{lse}_A, \mathrm{lse}_B) \) 来防止 exp 溢出。难点在于两路的「位置」不在同一坐标系：self 支路的 LSE 天然按 `[S, H]` 全排列，而 moba 支路的 LSE 按「(块×head) 段内的选中清单」排列（长度 `N_sel`）。`moba_q_sh_indices` 就是两者之间的翻译。

对每个 (s,h) 位置：self 支路恰有一条记录；moba 支路可能有 0 到 k 条记录（该 query 选中的每个块各一条）。所以「求 max」是：**以 self 的 LSE 为初值，把 moba 的 LSE 按地址簿散射进来，落点重复取最大**——这正是 `index_reduce` 的 `"amax"` 模式。

#### 4.2.2 核心流程

```
max_lse_1d = self_attn_lse_sh 展平                 # [S*H]，每个位置一个初值
max_lse_1d = max_lse_1d.index_reduce(
                 0, moba_q_sh_indices,              # 散射地址
                 moba_attn_lse.view(-1), "amax")    # 与落点处现有值取 max
# 此后所有 lse 都减去 max_lse_1d，保证 exp 的输入 ≤ 0
```

#### 4.2.3 源码精读

[moba/moba_efficient.py:L122-L141](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L122-L141)，先建输出缓冲再求 max：

```python
# output buffer [S, H, D], same shape as q
output = torch.zeros(
    (q.shape[0], q.shape[1], q.shape[2]), device=q.device, dtype=torch.float32
)

# flatten vS & H for index ops
output_2d = output.view(-1, q.shape[2])

# calc mixed_lse
# minus max lse to avoid exp explosion
max_lse_1d = self_attn_lse_sh.view(-1)
max_lse_1d = max_lse_1d.index_reduce(
    0, moba_q_sh_indices, moba_attn_lse.view(-1), "amax"
)
self_attn_lse_sh = self_attn_lse_sh - max_lse_1d.view_as(self_attn_lse_sh)
moba_attn_lse = (
    moba_attn_lse.view(-1)
    .sub(max_lse_1d.index_select(0, moba_q_sh_indices))
    .reshape_as(moba_attn_lse)
)
```

三个细节：

- **输出缓冲用 fp32**（L123-L125）：合并阶段要做 exp/log 加权，bf16 精度不够；最后 L166 才 `.to(q.dtype)`。
- `index_reduce` 是**非原地**版本（返回新张量），且被散射的源是 moba 段的 LSE——因为同一 (s,h) 可能被多个选中块写入，必须归约（取 amax）而不是覆盖。
- 减 max 之后，self 侧用 `view_as` 广播相减；moba 侧要先 `index_select` 把每个位置的 max「按地址簿抄过来」再逐条相减——moba 段的 LSE 与 `moba_q_sh_indices` 一一对应。

#### 4.2.4 代码实践

1. **实践目标**：亲手验证 `index_reduce` 对重复落点的 amax 行为。
2. **操作步骤**：运行下面的示例代码：

   ```python
   # 示例代码
   import torch
   base = torch.tensor([1.0, 2.0, 3.0])      # self 侧初值（3 个位置）
   idx  = torch.tensor([2, 2, 0])            # 位置 2 被写两次，位置 1 不被写
   src  = torch.tensor([4.0, 2.5, 0.5])      # moba 侧 LSE
   print(base.index_reduce(0, idx, src, "amax"))
   ```

3. **需要观察的现象**：位置 2 的结果是 4.0（两次写入取 max），位置 1 保持 2.0（无写入保留初值），位置 0 是 1.0 与 0.5 取 max。
4. **预期结果**：输出 `tensor([1.0000, 2.0000, 4.0000])`。这正是源码中「self 初值 + moba 散射」的求 max 方式。

#### 4.2.5 小练习与答案

**练习 1**：为什么不用 `torch.maximum(self_lse, 某个聚合后的 moba_lse)` 一步求出？

**答案**：moba 侧同一位置可能有多条记录（选中 k−1 个块就有 k−1 条），且不同位置的记录数不同，无法对齐成 `[S*H]` 的稠密张量；`index_reduce` 的散射归约正是为「多对一、落点不定」设计的。

**练习 2**：如果去掉减 max 这一步，哪种输入下会先出错？

**答案**：当打分较大时 \( e^{\mathrm{lse}} \) 轻松超过 bf16/fp32 上限（\( e^{89} \) 已超 fp32），`exp` 变 inf，后续 log 得 inf/nan。减 max 后被 exp 的量恒 ≤ 0，最大也只会下溢到 0（无害）。

### 4.3 `mixed_attn_lse_sh`：log-sum-exp 的加法合并

#### 4.3.1 概念说明

有了减去 max 后的两路 LSE，合并 LSE 就是把 2.2 节的公式落地：

\[ \mathrm{lse}_{A \cup B} - m = \log\left( e^{\mathrm{lse}_A - m} + e^{\mathrm{lse}_B - m} \right) \]

注意 \( B \) 不是一条路径而是「多条 (块, head) 记录」：对同一个 (s,h)，每条选中块记录都要往指数和里加一份。所以实现上是：**self 侧先 exp，再把 moba 侧的 exp 按 `index_add_` 累加进来，最后取 log**。

#### 4.3.2 核心流程

```
mixed_se = exp(self_lse - m)                       # [S,H]
mixed_se.view(-1).index_add_(0, idx, exp(moba_lse - m_per_entry))  # 多条记录累加
mixed_attn_lse_sh = log(mixed_se)                  # 此时是"减去 m 后"的合并 LSE
```

#### 4.3.3 源码精读

[moba/moba_efficient.py:L143-L149](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L143-L149)：

```python
mixed_attn_se_sh = self_attn_lse_sh.exp()
moba_attn_se = moba_attn_lse.exp()

mixed_attn_se_sh.view(-1).index_add_(
    0, moba_q_sh_indices, moba_attn_se.view(-1)
)
mixed_attn_lse_sh = mixed_attn_se_sh.log()
```

要点：

- `index_add_` 是**原地**操作（带下划线），作用在 `mixed_attn_se_sh` 的展平视图上——同一个 (s,h) 落点收到多条 moba 记录时逐条相加，恰好对应公式里 \( \sum_j e^{\mathrm{lse}_{B_j} - m} \) 的求和。
- 此刻的 `mixed_attn_lse_sh` 是「减去 m 后」的值；它在 L152 立即被用来算 factor，真正的（未偏移）合并 LSE 要到 L168 才补回 max 并保存给 backward。
- 变量名 `se` 是 `softmax exp` 的缩写，即softmax 的指数分母项。

#### 4.3.4 代码实践

1. **实践目标**：验证「log(exp 相加)」对多条记录累加的正确性。
2. **操作步骤**：接着 4.2.4 的示例代码运行：

   ```python
   # 示例代码（续 4.2.4）
   se = torch.exp(torch.tensor([1.0, 2.0, 3.0]) - torch.tensor([1.0, 2.0, 4.0]))
   se.view(-1).index_add_(0, torch.tensor([2, 2, 0]),
                          torch.exp(torch.tensor([4.0, 2.5, 0.5]) - torch.tensor([4.0, 4.0, 1.0])))
   print(se, se.log())
   ```

3. **需要观察的现象**：位置 2 的 se 收到两条 moba 记录（exp(0) + exp(−1.5)），log 后应等于 log(exp(3−4) + exp(4−4) + exp(2.5−4))。
4. **预期结果**：位置 2 的 log 值 ≈ log(0.3679 + 1 + 0.2231) = log(1.591) ≈ 0.464，即三条记录（1 条 self + 2 条 moba）的合并 LSE（减 max 后）。待本地验证具体数值。

#### 4.3.5 小练习与答案

**练习 1**：为什么 self 侧不需要 `index_add`，直接 `exp` 就行？

**答案**：self 支路的 LSE 本来就按 `[S, H]` 每个位置恰有一条记录，它既是「初值」也是「一条完整的记录」；只有 moba 侧是多对一的散射累加。

**练习 2**：`mixed_attn_lse_sh` 里会不会出现 log(0)？

**答案**：不会。self 侧贡献了 \( e^{\mathrm{lse}_A - m} \)，而 \( m \) 正是两路 LSE 的最大值，最大那条记录的贡献恰为 \( e^0 = 1 \)，所以指数和 ≥ 1，log 的值域是 \([0, \max-\text{偏移}]\)，数值安全。

### 4.4 `factor` 重加权与 `index_add_` 聚合输出

#### 4.4.1 概念说明

合并 LSE 之后，两路输出各自还是「被自己的 softmax 分母归一化」过的。要把它们拼成「被合并分母归一化」的输出，就乘上重加权系数：

\[ \text{factor}_A = e^{\mathrm{lse}_A - \mathrm{lse}_{A \cup B}}, \qquad \text{out} = \text{factor}_A \cdot \text{out}_A + \text{factor}_B \cdot \text{out}_B \]

对 moba 侧，同一个 (s,h) 的多条 (块) 记录各自有自己的 factor，逐条乘、再按地址簿 `index_add_` 累加进输出缓冲——「重复拷贝 Q 的代价」在此处被 LSE 数学精确补偿（u3-l4 埋的伏笔）。

#### 4.4.2 核心流程

```
# self 侧：每个位置一条，直接缩放后整体写入
factor_self = exp(self_lse_shifted - mixed_lse_shifted)     # [S,H]
output_2d += self_out * factor_self[..., None]              # [S*H, D]

# moba 侧：每条选中记录一个 factor，逐条累加
mixed_lse_per_entry = mixed_lse[idx]                        # 按地址簿抄
factor_moba = exp(moba_lse_shifted - mixed_lse_per_entry)   # [N_sel,H]
output_2d.index_add_(0, idx, moba_out * factor_moba[..., None])

# 收尾：转回原 dtype；把 max 加回合并 LSE 供 backward 使用
```

#### 4.4.3 源码精读

self 侧写入（[moba/moba_efficient.py:L151-L154](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L151-L154)）：

```python
# add attn output
factor = (self_attn_lse_sh - mixed_attn_lse_sh).exp()  # [ vS, H ]
self_attn_out_sh = self_attn_out_sh * factor.unsqueeze(-1)
output_2d += self_attn_out_sh.reshape_as(output_2d)
```

注意此处的 `self_attn_lse_sh` 和 `mixed_attn_lse_sh` 都还是「减过 max」的版本，但差值 \( \mathrm{lse}_A - \mathrm{lse}_{A\cup B} \) 与是否减 max 无关（m 被消去），数学上等价于原公式。self 侧每位置一条记录，直接 `+=` 即可（`output_2d` 是 `output` 的视图，写入直达缓冲区）。

moba 侧写入（[moba/moba_efficient.py:L156-L168](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L156-L168)）：

```python
# add moba output
mixed_attn_lse = (
    mixed_attn_lse_sh.view(-1)
    .index_select(0, moba_q_sh_indices)
    .view_as(moba_attn_lse)
)
factor = (moba_attn_lse - mixed_attn_lse).exp()  # [ vS, H ]
moba_attn_out = moba_attn_out * factor.unsqueeze(-1)
raw_attn_out = moba_attn_out.view(-1, moba_attn_out.shape[-1])
output_2d.index_add_(0, moba_q_sh_indices, raw_attn_out)
output = output.to(q.dtype)
# add back max lse
mixed_attn_lse_sh = mixed_attn_lse_sh + max_lse_1d.view_as(mixed_attn_se_sh)
```

三个关键点：

- **`index_select` 先抄再减**：moba 段每条记录需要「它所属位置的合并 LSE」，用地址簿 gather 过来，才能逐条算 factor。
- **`index_add_` 累加**：同一 (s,h) 的 k−1 条块记录各自贡献 \( e^{\mathrm{lse}_{B_j} - \mathrm{lse}_{A \cup B}} \cdot \text{out}_{B_j} \)，与 self 侧贡献相加后恰好是合并 softmax 的分子除以合并分母。
- **L167-L168 加回 max**：`mixed_attn_lse_sh` 恢复成真实的（未偏移）合并 LSE，随后 L169-L181 连同 `output`、`q/k/v`、两路 varlen 边界和地址簿一起 `save_for_backward`——u3-l6 将看到反向直接把它喂给 `_flash_attn_varlen_backward`，这也是为什么必须加回真实值：反向内核要用它重算 softmax 权重。

最终 `output [S, H, D]` 从 `MixedAttention.apply` 返回（入口在 [moba/moba_efficient.py:L431-L443](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L431-L443)）。

#### 4.4.4 代码实践

1. **实践目标**：验证「逐条 factor + index_add_」与「先加总再乘」等价。
2. **操作步骤**：设某 (s,h) 位置 self 输出为 \( o_A \)、两条 moba 记录为 \( o_{B_1}, o_{B_2} \)，用示例代码分别按两种方式计算：

   ```python
   # 示例代码
   import torch
   o_A  = torch.tensor([1.0]); lse_A  = torch.tensor(2.0)
   o_B1 = torch.tensor([0.5]); lse_B1 = torch.tensor(1.5)
   o_B2 = torch.tensor([2.0]); lse_B2 = torch.tensor(3.0)

   # 方式一：源码做法（逐条 factor）
   m = torch.max(torch.stack([lse_A, lse_B1, lse_B2]))
   mixed = (lse_A - m).exp() + (lse_B1 - m).exp() + (lse_B2 - m).exp()
   mixed_lse = mixed.log() + m
   out1 = (lse_A - mixed_lse).exp() * o_A \
        + (lse_B1 - mixed_lse).exp() * o_B1 \
        + (lse_B2 - mixed_lse).exp() * o_B2

   # 方式二：直接合并 softmax（把三组 key 的 exp 权重放一起）
   scores = torch.tensor([lse_A, lse_B1, lse_B2])       # 这里 lse 即权重和的 log
   outs   = torch.stack([o_A, o_B1, o_B2])
   out2 = ((scores - scores.max()).exp() * outs).sum() / (scores - scores.max()).exp().sum()

   print(out1, out2)
   ```

3. **需要观察的现象**：两种方式的输出完全一致（浮点误差内）。
4. **预期结果**：`out1 == out2`。这说明源码的逐条 factor 写法在多条记录时仍然精确对应「合并 softmax」。

#### 4.4.5 小练习与答案

**练习 1**：self 侧写输出用 `+=`，moba 侧必须用 `index_add_`，为什么不对称？

**答案**：self 侧的输出 `[S,H,D]` 与输出缓冲逐位置一一对应，无重复落点；moba 侧同一 (s,h) 有多条记录且不同位置记录数不同，必须散射累加。

**练习 2**：`factor` 的取值范围是什么？什么情况下某条记录的 factor 接近 0？

**答案**：factor = exp(自身 lse − 合并 lse) ∈ (0, 1]。当某条路径的指数权重相对于合并总量可忽略（例如该块打分远低于另一路径）时，其 factor 趋近 0——这正是「弱相关块贡献自动衰减」的数学体现。

**练习 3**：如果忘了 L168 的「加回 max_lse」，前向输出会变吗？反向会怎样？

**答案**：前向不受影响（L166 已返回 output，factor 计算里 m 已消去）；但保存给 backward 的 LSE 偏移了 m，反向内核用它重算 softmax 权重会整体偏乘 \( e^{-m} \)，梯度错误。这正是该行存在的唯一理由。

## 5. 综合实践

**任务：用三个标量情形手工 + PyTorch 双重验证 LSE 合并公式，并与源码逐行对号。**

### 5.1 手工推演（先别跑代码）

设单 query、head_dim 缩放后：路径 A（self 支路）有 1 个 key，打分 \( s_1 = 1.0 \)，值 \( v_1 = 2.0 \)；路径 B（moba 支路）有 2 个 key，打分 \( s_2 = 2.0, s_3 = 0.5 \)，值 \( v_2 = 1.0, v_3 = 3.0 \)。求合并输出。

分步（建议在纸上完成，基准值见第 7 步）：

1. \( \mathrm{lse}_A = \log e^{1.0} = 1.0 \)；\( \text{out}_A = v_1 = 2.0 \)（路径 A 只有一个 key，softmax 归一后输出就是 \( v_1 \)）。
2. \( \mathrm{lse}_B = \log(e^{2.0} + e^{0.5}) \approx \log(7.389 + 1.649) = \log 9.038 \approx 2.201 \)；\( \text{out}_B = \frac{7.389 \times 1.0 + 1.649 \times 3.0}{9.038} \approx 1.365 \)。
3. \( m = \max(1.0, 2.201) = 2.201 \)（防溢出用，**不是**合并 LSE）。
4. 合并 LSE（减 m 再 log）：\( \log(e^{1.0-2.201} + e^{0}) \approx \log(0.301 + 1) \approx 0.263 \)；加回 m 得 \( \mathrm{lse}_{\text{mixed}} \approx 2.464 \)。交叉验证：\( \log(e^1 + e^2 + e^{0.5}) \approx \log 11.756 \approx 2.464 \) ✓。
5. factor：\( f_A = e^{1.0 - 2.464} \approx 0.231 \)，\( f_B = e^{2.201 - 2.464} \approx 0.769 \)。注意分母是 \( \mathrm{lse}_{\text{mixed}} \) 而不是 \( m \)——这是最容易踩的坑（见下）。
6. 合并输出：\( 0.231 \times 2.0 + 0.769 \times 1.365 \approx 0.463 + 1.050 \approx 1.512 \)。
7. **对照基准**：三个 key 一起 softmax，\( \text{out} = \frac{e^{1} \cdot 2 + e^{2} \cdot 1 + e^{0.5} \cdot 3}{e^1 + e^2 + e^{0.5}} \approx \frac{5.437 + 7.389 + 4.946}{11.756} \approx 1.512 \) ✓ 与第 6 步一致。

**常见错误**：把第 5 步的分母写成 \( m = 2.201 \)，得 \( f_A = e^{-1.201} \approx 0.301 \)、合并输出 \( \approx 0.602 + 1.050 = 1.652 \neq 1.512 \)。减 max 只是数值稳定的实现手段，公式本身（2.2 节）用的是未偏移的 \( \mathrm{lse}_{A \cup B} \)；源码里 L136-L149「减 m → exp/add → log」算出的正是减偏版 mixed lse，而 factor 取的是差值，m 自动消去，所以源码不会犯这个错——**人手算时才会**。如果你手算结果与 5.2 脚本对不上，优先检查这一点。

### 5.2 PyTorch 验证脚本

```python
# 示例代码：LSE 两路合并 vs 直接合并 softmax
import torch

torch.manual_seed(0)
s_A = torch.tensor([1.0])            # 路径 A 各 key 的打分（缩放后）
v_A = torch.tensor([[2.0]])
s_B = torch.tensor([2.0, 0.5])       # 路径 B 各 key 的打分
v_B = torch.tensor([[1.0], [3.0]])

# 各路独立 softmax 输出与 LSE（等价于两次 flash-attn 前向的返回值）
out_A = torch.softmax(s_A, 0) @ v_A
lse_A = torch.logsumexp(s_A, 0)
out_B = torch.softmax(s_B, 0) @ v_B
lse_B = torch.logsumexp(s_B, 0)

# ---- 以下逐行对应 MixedAttention.forward L130-L168 ----
max_lse = torch.maximum(lse_A, lse_B)                 # L132-L135: index_reduce "amax"
lse_A_s, lse_B_s = lse_A - max_lse, lse_B - max_lse   # L136-L141: 减 max
mixed_se = lse_A_s.exp() + lse_B_s.exp()              # L143-L148: exp + index_add_
mixed_lse_s = mixed_se.log()                          # L149
factor_A = (lse_A_s - mixed_lse_s).exp()              # L152
factor_B = (lse_B_s - mixed_lse_s).exp()              # L162
merged = factor_A * out_A + factor_B * out_B          # L153-L154 + L163-L165
mixed_lse = mixed_lse_s + max_lse                     # L168: 加回 max

# ---- 基准：所有 key 一起 softmax ----
s_all = torch.cat([s_A, s_B]); v_all = torch.cat([v_A, v_B])
ref_out = torch.softmax(s_all, 0) @ v_all
ref_lse = torch.logsumexp(s_all, 0)

print("merged out:", merged.item(), " ref out:", ref_out.item())
print("merged lse:", mixed_lse.item(), " ref lse:", ref_lse.item())
assert torch.allclose(merged, ref_out, atol=1e-6)
assert torch.allclose(mixed_lse, ref_lse, atol=1e-6)
print("LSE merge formula verified ✔")
```

### 5.3 观察与预期

- **现象**：合并输出与「全部 key 一起 softmax」的基准在 1e-6 容忍度内一致；合并 LSE 与 `logsumexp(全部打分)` 一致。
- **修正 5.1 的手算**：用脚本打印每一步中间量（`factor_A`、`factor_B`、`mixed_lse`），定位你手算里出错的那一步，然后重做 5.1 直到纸上结果与脚本一致。
- **源码对号**：脚本里每行注释标了 `moba_efficient.py` 的行号；唯一区别是源码用 `index_reduce`/`index_add_` 处理「同一位置多条记录」，而本例每个 (s,h) 只有一条 B 记录，故退化为标量运算。
- 运行只需 CPU 与 PyTorch，不需要 GPU / flash-attn。待本地验证。

## 6. 本讲小结

- `MixedAttention.forward` 前向 = **两次 `_flash_attn_varlen_forward`** + **一次 LSE 在线合并**：self 支路以 `cu_chunk` 为 varlen 边界做块内因果注意力（`causal=True`），moba 支路对 u3-l4 重组的变长序列做非因果注意力（`causal=False`，因果性已在 gate 阶段前置）。
- flash-attn 返回的 LSE 是 head 优先布局 `[H, total]`，源码 `.t()` 成 `[total, H]` 才能与输出缓冲的 \( s \cdot H + h \) 展平空间对齐；backward 里再 `.t()` 回去。
- 合并三步曲：`index_reduce("amax")` 求逐位置 max_lse 防溢出 → `exp + index_add_ + log` 得合并 LSE → 逐路 `factor = exp(lse − lse_mixed)` 重加权后 `index_add_` 聚合进 fp32 输出缓冲。
- 同一 (s,h) 位置会被该 query 选中的每个块各写一条 moba 记录，所有「多对一」的聚合都必须用 `index_add_` 累加；u3-l4 里「Q 允许重复拷贝」的代价在此被 LSE 数学精确补偿。
- 合并公式与减不减 max 在数学上等价（m 在差值中消去），减 max 纯为数值稳定；L168「加回 max」只为给 backward 保存真实的合并 LSE。
- 输出缓冲全程 fp32 累加，最后才转回 `q.dtype`。

## 7. 下一步学习建议

下一讲 **u3-l6 MixedAttention 反向** 将使用本讲保存的 `output` 与 `mixed_attn_lse_sh`：直接把它们作为 `out` / `softmax_lse` 传给两次 `_flash_attn_varlen_backward`，即可得到数学正确的梯度——请思考为什么「合并后的 out + lse」能让每条支路的反向内核自动隐含混合 softmax 的归一化因子。建议先自己推一遍 \( \partial \text{out}_{A \cup B} / \partial \text{out}_A \)，再带着答案去读 [moba/moba_efficient.py:L185-L267](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L185-L267)。之后可回到 u4-l1 看这套前向如何被 wrapper 接入 transformers。

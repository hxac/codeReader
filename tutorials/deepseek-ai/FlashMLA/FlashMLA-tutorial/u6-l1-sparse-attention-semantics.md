# Sparse attention 语义与 indices 编码

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 FlashMLA 里 **两种** `indices` 的含义差异：prefill 的「直接索引」与 decode 的「分页物理索引（`indices_in_kvcache`）」。
- 用 `page_block * block_size + offset` 这一条编码公式，解释为什么 decode 的 sparse kernel 不再需要 `block_table`。
- 说清「无效索引」的两套判定规则（`-1` 与 `>= s_kv`），以及 `topk_length` 如何按「最左若干个」截断。
- 对照 `tests/ref.py` 的 `ref_sparse_attn_fwd`，独立写出一个用 `gather + softmax` 复现 kernel 输出的参考函数。
- 理解三个输出 `out / max_logits / lse` 的定义，以及 `attn_sink` 与「lonely query」两类边界情形如何改变它们。

本讲是 Unit 6（token-level sparse prefill/decode）的第一篇，只讲「语义和数据契约」，不进入 CUDA kernel 内部。kernel 如何消费这些索引留到 u6-l2/u6-l3。

## 2. 前置知识

本讲假设你已经了解（来自前面讲义）：

- **MLA / MQA**（[u1-l1](u1-l1-project-overview-and-mla.md)）：MLA 只缓存压缩后的潜在向量，K 与 V 同源；FlashMLA 的 sparse 路径走 MQA 模式，即 `head_dim_k = 576`（含 64 维 RoPE）、`head_dim_v = 512`。
- **Paged KV cache**（[u1-l4](u1-l4-python-api-quickstart.md)）：decode 阶段 KV 不是一整块连续显存，而是被切成固定大小的 page block，再用 `block_table` 把「逻辑块号」映射到「物理块池下标」。
- **参数结构 `params.h`**（[u2-l2](u2-l2-params-structs.md)）：sparse 路径的 `indices` / `topk` / `extra_*` 字段如何挂在 `SparseAttnDecodeParams` / `SparseAttnFwdParams` 上。
- **softattention 与 online softmax** 的基本记号：score \(P\)、log-sum-exp \(L\)、归一化权重 \(S\)、输出 \(O\)。

如果你对「为什么要做 sparse attention」这个问题感兴趣，一句话回答：DeepSeek-V3.2 的 **DSA（DeepSeek Sparse Attention）** 在 prefill/decode 时只让每个 query 关注被选中的少量 token，而不是全部历史 token，从而把注意力从 \(O(s_q \cdot s_{kv})\) 降到 \(O(s_q \cdot \text{topk})\)。本讲关注的正是「被选中的 token 怎么告诉 kernel」。

## 3. 本讲源码地图

| 文件 | 作用 |
| :--- | :--- |
| `tests/ref.py` | **最权威的语义参考实现**。`ref_sparse_attn_fwd`（prefill）和 `ref_sparse_attn_decode`（decode）用纯 PyTorch 定义了 kernel 应当输出的精确数值。 |
| `flash_mla/flash_mla_interface.py` | Python 接口层。`flash_mla_with_kvcache`（decode）与 `flash_mla_sparse_fwd`（prefill）两个入口，定义了 `indices` 张量的形状与含义。 |
| `tests/quant.py` | `abs_indices2indices_in_kvcache`：把「逻辑绝对索引」翻译成「kernel 要的物理索引」的编码函数。 |
| `tests/lib.py` | 测试数据生成。`generate_testcase` 构造 `indices`、`KVScope` 封装分页 KV 与索引。 |
| `README.md` | 两种 sparse 接口的官方说明与「等价 PyTorch 代码」。 |

> 阅读建议：先看 `README.md` 的两段 sparse 说明建立直觉，再用 `tests/ref.py` 钉死数值语义，最后用 `tests/quant.py` + `tests/lib.py` 理解索引是怎么造出来、怎么编码的。

## 4. 核心概念与源码讲解

### 4.1 indices 编码：直接索引 vs 分页物理索引

#### 4.1.1 概念说明

「sparse attention」的核心动作是：对每一条 query，**只挑出一小撮 KV token** 来算注意力。那么「挑哪些」就必须用一个张量告诉 kernel，这个张量就是 `indices`。

容易踩坑的地方在于：FlashMLA 的两个 sparse 接口对 `indices` 的「坐标系」定义**不一样**。

- **Prefill（`flash_mla_sparse_fwd`）**：KV 是一整块平坦张量 `kv: [s_kv, h_kv, d_qk]`，没有分页。`indices` 直接就是「kv 张量的第 0 维下标」，取值范围 \([0, s_{kv})\)。
- **Decode（`flash_mla_with_kvcache(indices=...)`）**：KV 是**分页**的，散落在物理块池里。`indices_in_kvcache` 已经把「逻辑块号 → 物理块号」这步翻译做完了，它直接是「物理块池里的绝对 token 下标」。因此 **decode 的 sparse kernel 不需要 `block_table`**——这点 README 明确写了。

第二个差异是 `indices` 的形状：

| 接口 | `indices` 形状 | 含义 |
| :--- | :--- | :--- |
| prefill | `[s_q, h_kv, topk]` | 第 \(i\) 条 query、第 \(k\) 个被选 token，在 `kv[s_sv]` 里的下标 |
| decode | `[batch_size, s_q, topk]` | 第 \(i\) 个 batch、第 \(j\) 条 query、第 \(k\) 个被选 token，在**物理块池**里的下标 |

注意 decode 的 `indices` 没有 `h_kv` 维——因为 MQA 下所有 query 头共享同一份 KV（`h_kv=1`），整批共享同一套索引。

#### 4.1.2 核心流程

decode 路径里，「逻辑绝对索引」到「kernel 物理索引」的编码由 `tests/quant.py` 的 `abs_indices2indices_in_kvcache` 完成，公式是：

\[
\text{indices\_in\_kvcache}[b, j, k]
= \text{block\_table}[b]\!\left[\left\lfloor \tfrac{\text{abs\_idx}}{\text{block\_size}} \right\rfloor\right] \times \text{block\_size} + (\text{abs\_idx} \bmod \text{block\_size})
\]

直观理解：

1. 一个 token 在逻辑序列里的位置 `abs_idx` 先除以 `block_size`，得到它属于**第几个逻辑块**。
2. 用 `block_table[b]` 把逻辑块号翻译成**物理块池下标**。
3. 再乘回 `block_size`、加上块内偏移，得到该 token 在「物理块池（一维展开）」里的绝对下标。

无效项（`abs_idx == -1`）在翻译前先临时改成 0 走完公式，最后再改回 `-1`。整条流水线如下：

```
abs_indices [b, s_q, topk]              逻辑序列下标 ∈ [0, s_k) 或 -1
        │  abs_indices2indices_in_kvcache (quant.py)
        ▼
indices_in_kvcache [b, s_q, topk]       物理块池下标 = 物理块号*block_size + 偏移，或 -1
        │  传给 flash_mla_with_kvcache(indices=...)
        ▼
kernel 直接 kv_pool_flat[indices_in_kvcache] 取 token，不再查 block_table
```

prefill 路径没有分页，`indices` 就是 `abs_indices` 本身（甚至更宽松：`>= s_kv` 也算无效），所以不需要这一步编码。

#### 4.1.3 源码精读

**encode 函数本体**：[tests/quant.py:L126-L158](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/quant.py#L126-L158) —— 即 `abs_indices2indices_in_kvcache`。注意它先用 `invalid_mask = abs_indices == -1`、`abs_indices[invalid_mask] = 0` 保护 `index_select` 不越界，翻译完再把无效项恢复成 `-1`（L150-L156）。它的 docstring 里给了一段等价的逐 batch 循环伪码（L134-L144），是理解公式最快的入口。

**README 对 decode 的定义**：[README.md:L125-L131](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/README.md#L125-L131) —— 明确写出 `indices_in_kvcache[i][j][k] = (page block index) * page_block_size + (offset within block)`，并强调「page block 的下标已经编码进去了，所以 kernel 不需要 `block_table`」。同时规定无效项填 `-1`。

**README 对 prefill 的定义**：[README.md:L139-L149](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/README.md#L139-L149) —— prefill 的 `indices` 形状是 `[s_q, h_kv, topk]`，且无效项「设为 `-1` 或任何 `>= s_kv` 的数都行」，注意它**不**走分页编码。

**Python 接口层的注释**：[flash_mla/flash_mla_interface.py:L85-L87](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L85-L87) —— `flash_mla_with_kvcache` 的 docstring 复述了同一条编码公式，把 `indices_in_kvcache` 的语义钉死在接口契约里。

**测试如何造数据**：[tests/lib.py:L264-L269](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/lib.py#L264-L269) —— 先用 `_randperm_batch` 生成 `abs_indices`（值为 `-1` 表示无效），再立刻调用 `quant.abs_indices2indices_in_kvcache(abs_indices, block_table, block_size)` 翻译成 `indices_in_kvcache`，封装进 `KVScope`。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `abs_indices2indices_in_kvcache` 与它的等价循环伪码一致，并理解「kernel 不需要 block_table」这件事。

**操作步骤**（CPU 即可，无 GPU 也能跑）：

1. 构造一个微型分页场景：`block_size = 4`，2 个 batch，每个 batch 3 个逻辑块，物理块池共有 6 个块但顺序被打乱。
2. 取一个有效的 `abs_idx`（例如 6），手算 `block_table[b][6//4]*4 + 6%4`。
3. 调用 `abs_indices2indices_in_kvcache` 对照。

```python
# 示例代码：验证索引编码（CPU 可运行）
import sys; sys.path.insert(0, "tests")
import torch
from quant import abs_indices2indices_in_kvcache

block_size = 4
# 假设物理块池有 6 个块，batch0 的逻辑块顺序被排成 [3,1,0,2]，batch1 排成 [4,5,2,0]
block_table = torch.tensor([[3, 1, 0, 2],
                            [4, 5, 2, 0]], dtype=torch.int32)
abs_indices = torch.tensor([[[6, -1]]], dtype=torch.int32)   # [b=2? 先 broadcast]
abs_indices = torch.tensor([[[6, -1]], [[9, 2]]], dtype=torch.int32)  # [b=2, s_q=1, topk=2]

out = abs_indices2indices_in_kvcache(abs_indices, block_table, block_size)
print(out)
# batch0: abs=6 -> block_table[0][1]*4 + 6%4 = 1*4 + 2 = 6；abs=-1 -> -1
# batch1: abs=9 -> block_table[1][2]*4 + 9%4 = 2*4 + 1 = 9；abs=2 -> block_table[1][0]*4 + 2%4 = 4*4 + 2 = 18
```

**需要观察的现象**：

- batch0 的 `abs=6` 编码后仍是 6（巧合：逻辑块号恰好等于物理块号）。
- batch1 的 `abs=2` 编码后变成 18（逻辑块 0 → 物理块 4 → 物理下标 16，加偏移 2 = 18）。这正体现了「`block_table` 的重排」。
- `-1` 原样保留为 `-1`。

**预期结果**：打印的张量里 `6, -1` 在第一行、`9, 18` 在第二行（`-1` 始终保留）。如果 `block_table` 改成恒等映射（`[[0,1,2,3],[0,1,2,3]]`），则 `indices_in_kvcache` 会与 `abs_indices` 完全相同——这正是「不分页」的退化情形。

> 待本地验证：上述具体数值依赖你给定的 `block_table`，请在自己的环境里跑一次确认。

#### 4.1.5 小练习与答案

**练习 1**：如果 `block_table` 是恒等映射（`block_table[b][i] = i`），`indices_in_kvcache` 与 `abs_indices` 有什么关系？

**答案**：完全相等。因为 `block_table[b][abs//bs] = abs//bs`，于是 `indices_in_kvcache = (abs//bs)*bs + abs%bs = abs`。这也说明 prefill 那种「直接索引」就是分页被关掉（恒等 block_table）的特例。

**练习 2**：为什么 decode 的 sparse kernel 不接收 `block_table`，而 dense decode 却必须接收？

**答案**：sparse 路径在**调用 kernel 之前**（CPU 侧）就把 `block_table` 烘焙进了 `indices_in_kvcache`，kernel 拿到的已经是物理下标，直接一维寻址即可；dense 路径要遍历整段连续逻辑序列，必须运行时反复查 `block_table` 才能定位每个块，所以参数里离不开它。

**练习 3**：`abs_indices2indices_in_kvcache` 里为什么先把 `-1` 改成 `0`、最后再改回 `-1`？

**答案**：因为中间用了 `index_select(0, abs//block_size + batch_offset)`，下标为 `-1` 会让 PyTorch 报「负下标」错误。先钳到 `0` 让公式安全走完，再用 `invalid_mask` 把无效项恢复成 `-1`，保证语义不丢。

---

### 4.2 无效索引与 topk_length 的掩码语义

#### 4.2.1 概念说明

实际推理时，`indices` 里几乎总是混着「不该被关注」的槽位，原因有两个：

1. **填充（padding）**：不同 query 选中的有效 token 数不同，但张量要等长，多余槽位必须标记成「无效」。
2. **越界**：被选 token 的逻辑下标可能超出当前序列实际长度 \(s_{kv}\)（例如 topk 大于序列长度）。

FlashMLA 用两道掩码共同决定一个槽位是否有效：

- **无效索引掩码**：prefill 判 `(idx < 0) | (idx >= s_kv)`；decode 判 `idx == -1`。
- **`topk_length` 掩码**：给定一个长度阈值，**只保留最左 `topk_length` 个槽位**，其余当作无效（即使它们指向合法 token）。

这两道掩码是「或」的关系——任一命中即该 token 不参与注意力。

#### 4.2.2 核心流程

设 `indices` 沿 topk 维是 \([k_0, k_1, \dots, k_{\text{topk}-1}]\)，`topk_length = L`。有效掩码为：

\[
\text{valid}[k] \;=\; \bigl(\,0 \le \text{indices}[k] < s_{kv}\,\bigr) \;\wedge\; \bigl(\,k < L\,\bigr)
\]

对无效位置，参考实现做两件事：

1. 把 score \(P\) 置为 \(-\infty\)，使其在 softmax 里权重为 0；
2. 在 gather 前把非法下标钳到 `0`，避免 `index_select` 越界。

两种接口的 `topk_length` 维度不同：

- prefill：`topk_length: [s_q]`，每条 query 一个阈值。
- decode：`topk_length: [batch_size]`，每个 batch 一个阈值。

#### 4.2.3 源码精读

**prefill 参考里的两道掩码**：[tests/ref.py:L28-L32](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/ref.py#L28-L32) —— 先用 `arange(topk) >= topk_length` 生成 topk 截断掩码把越界槽改成 `-1`，再用 `(indices < 0) | (indices >= s_kv)` 汇总成 `invalid_mask`，最后把无效下标钳到 `0` 以便安全 gather。

**prefill 把 score 置 \(-\infty\)**：[tests/ref.py:L38](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/ref.py#L38) —— `P[invalid_mask...] = float("-inf")`。

**decode 参考里的两道掩码**：[tests/ref.py:L71-L73](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/ref.py#L71-L73) —— `invalid_mask = indices_in_kvcache == -1`，再「或」上 `arange(topk) >= topk_length` 的截断；注意 decode 的越界检测只有 `-1`，因为物理下标天然非负、且 `block_table` 已保证落在池内。

**接口层对 `topk_length` 的契约**：[flash_mla/flash_mla_interface.py:L90](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L90) 与 [flash_mla/flash_mla_interface.py:L198](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L198) —— 两个接口都说明「只处理最左 `topk_length` 个索引」，用于「不同 query 实际 topk 不同时省掉掩码开销」。

**测试如何造无效索引**：[tests/lib.py:L131-L137](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/lib.py#L131-L137) —— `invalid_indices_candidate` 里同时塞了负数（`-1`、`-123456`）和超大正数（`114514`、`1919810`、`2147483647`），专门覆盖两类无效；`is_all_indices_invalid` 分支则会把整行全置成无效，用于压测「lonely query」边界。

#### 4.2.4 代码实践

**实践目标**：构造一个「topk=4、`topk_length=2`、且第 0 个槽越界」的 indices，观察哪些 token 真正参与了注意力。

**操作步骤**（CPU，纯 PyTorch）：

```python
# 示例代码：复刻 ref.py 的两道掩码逻辑
import torch

s_kv, topk, s_q = 5, 4, 1
indices = torch.tensor([[7, 2, 3, -1]], dtype=torch.int32)   # [s_q=1, topk=4]
topk_length = torch.tensor([2], dtype=torch.int32)            # 只取最左 2 个

# 第一道：topk_length 截断
mask = torch.arange(topk).unsqueeze(0) >= topk_length.unsqueeze(1)
indices = indices.clone(); indices[mask] = -1                  # -> [[7, 2, -1, -1]]

# 第二道：无效索引
invalid = (indices < 0) | (indices >= s_sv)                    # 7>=5 无效，-1 无效，2 有效
print("有效槽位个数：", int((~invalid).sum()))                 # 期望 1（只有下标 2）
```

**需要观察的现象**：经过两道掩码后，4 个槽里只剩 1 个有效（下标 `2`）。`7` 因 `>= s_kv` 被砍，后两个因 `topk_length` 和 `-1` 被砍。

**预期结果**：打印 `1`。把 `topk_length` 改成 `[4]` 后，有效槽变成 2 个（`2` 与原本第 2 槽的 `3`，注意第 3 槽是 `-1` 仍无效）。

#### 4.2.5 小练习与答案

**练习 1**：prefill 接受 `idx >= s_sv` 为无效，decode 只接受 `idx == -1`。为什么 decode 不需要判 `>= s_kv`？

**答案**：decode 的 `indices_in_kvcache` 是**物理块池下标**，已由 CPU 侧的 `abs_indices2indices_in_kvcache` 保证落在合法物理块范围内（无效的逻辑索引被翻译成 `-1`）。kernel 看不到「序列长度 \(s_{kv}\)」这个逻辑量，只能用 `-1` 这个哨兵值判无效。

**练习 2**：如果某个 query 的 `topk_length = 0`，它的输出会是什么？

**答案**：所有 topk 槽都被截断成无效，score 全为 \(-\infty\)，`lse = -inf`。这是一个「lonely query」（见 4.4）：参考实现会强制把它的 `out` 置 0、`lse` 置 `+inf`。

**练习 3**：为什么参考实现要在 gather 之前把无效下标钳到 `0`，而不是 gather 之后才掩码？

**答案**：`kv.index_select(0, indices)` 要求下标合法，负数或越界会直接报错。钳到 `0` 让 gather 安全返回一个「随后会被 \(-\infty\) 屏蔽掉」的占位 token，数值上不影响最终 softmax（因为它的权重被压成 0）。

---

### 4.3 等价 PyTorch 参考实现

#### 4.3.1 概念说明

`tests/ref.py` 是整个 sparse kernel 的「黄金参考」：测试用它和 CUDA kernel 的输出逐元素比对其实现是否正确。理解它，就理解了 kernel 的全部语义。这一节我们聚焦 prefill 版的 `ref_sparse_attn_fwd`，它的核心是「gather 出被选 KV → 算 QK → softmax → 回乘 V」四步，全程用 `float32` 做数值稳定的 softmax。

decode 版（`ref_sparse_attn_decode`）思路相同，只是多了 batch 维、`extra_kv`（第二份 KV）的拼接，以及 split-KV 之外的处理。本节先吃透 prefill 版，decode 版的额外细节在 4.4 与 u6 后续讲义展开。

#### 4.3.2 核心流程

对每条 query \(i\)、每个头 \(h\)，被选中的 topk 个 token 先 gather 成 \(\tilde{K}\in\mathbb{R}^{\text{topk}\times d_{qk}}\)。注意力计算为：

\[
P_{i,h,k} = \text{sm\_scale}\cdot \sum_{d} q_{i,h,d}\,\tilde{K}_{k,d},\qquad
\text{invalid}(k) \Rightarrow P_{i,h,k} := -\infty
\]

\[
L_{i,h} = \log\!\sum_k e^{P_{i,h,k}}\quad(\text{即 logsumexp}),\qquad
S_{i,h,k} = e^{P_{i,h,k} - L_{i,h}}
\]

\[
O_{i,h} = \sum_k S_{i,h,k}\,\tilde{K}_{k,\,1:d_v}
\]

注意 MLA 里 V 是 K 的前 \(d_v\) 维（\(d_{qk}=576, d_v=512\)），所以回乘时取 `gathered_kv[..., :d_v]`。

> **关于 log 的底数**：README 给 prefill 列的「等价 PyTorch 代码」用 base-2 表达（`* log2(e)`、`log2sumexp2`、`exp2`），这是该 kernel 内部用 base-2 做数值稳定的展示写法。但 `ref.py` 与测试**实际比对**用的是自然对数（base-e）的 `torch.logsumexp`，且容差极小（`abs_tol=1e-6`）。softmax 的输出 \(O\) 与底数无关，所以 out/max_logits 两种写法一致；而 lse 的绝对值在 base-2 写法里会差一个 \(\log_2 e\) 因子。**以 `ref.py` 为准**：kernel 实际返回的 lse 与 `ref.py` 的 base-e 定义逐数值吻合（由测试保证），本讲统一用 base-e 讲解。

#### 4.3.3 源码精读

**`ref_sparse_attn_fwd` 全貌**：[tests/ref.py:L19-L52](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/ref.py#L19-L52) —— 逐行对应 4.3.2 的四步。

关键行：

- [tests/ref.py:L34-L35](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/ref.py#L34-L35)：`q.float()` 升精度；`kv.index_select(...).reshape(s_q, topk, d_qk)` 完成 gather，得到 `[s_q, topk, d_qk]`。
- [tests/ref.py:L36-L38](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/ref.py#L36-L38)：`P = q @ gathered_kv.transpose(1,2)` 得 `[s_q, h_q, topk]`，乘 `sm_scale`，再把无效位置置 \(-\infty\)。
- [tests/ref.py:L40-L41](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/ref.py#L40-L41)：`lse = logsumexp(P, -1)`、`max_logits = P.max(-1)`，两者形状都是 `[s_q, h_q]`。
- [tests/ref.py:L43-L48](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/ref.py#L43-L48)：先用 `_merge_two_lse` 把 `attn_sink` 并入 lse 得 `lse_for_o`（仅影响 out），再 `out = exp(P - lse_for_o) @ gathered_kv[..., :d_v]`。
- [tests/ref.py:L50-L52](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/ref.py#L50-L52)：处理 lonely query，返回 `(out_bf16, out_fp32, max_logits, orig_lse)` 四元组。

**`_merge_two_lse` 工具**：[tests/ref.py:L7-L17](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/ref.py#L7-L17) —— 即 \(\log(e^{L_0}+e^{L_1})\)（等价 `torch.logaddexp`），用于把 `attn_sink` 当作一个「额外的 logit」并入归一化分母。

**README 的等价代码**：[README.md:L152-L170](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/README.md#L152-L170) —— 同一套数学的 base-2 写法，可作为对照阅读。

**测试如何比对**：[tests/test_flash_mla_sparse_prefill.py:L41-L48](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/test_flash_mla_sparse_prefill.py#L41-L48) —— 调 `ref.ref_sparse_attn_fwd` 拿参考，再用三重容差（abs/rel/cos_diff）逐项比 `out / max_logits / lse`。这段是「ref.py 即真理」的最直接证据。

#### 4.3.4 代码实践

**实践目标**：脱离 `TestParam`/`Testcase`，写一个独立的 `sparse_attn_ref(q, kv, indices, sm_scale, ...)`，用 `gather + softmax` 复现 prefill kernel 输出，正确处理 `-1`、`>= s_kv`、`topk_length`、`attn_sink` 与 lonely query。

**操作步骤**：

1. 阅读上面引用的 `ref_sparse_attn_fwd`（L19-L52）。
2. 录入下面的「示例代码」，它把 ref 的逻辑改写成一个无外部依赖的纯函数。
3. 用一个小例子跑通，再人为把某个 query 的 indices 全置无效，观察 lonely query 行为。

```python
# 示例代码：sparse prefill 注意力的独立参考实现（base-e，对齐 ref.py）
import torch

def sparse_attn_ref(
    q: torch.Tensor,                # [s_q, h_q, d_qk], bfloat16
    kv: torch.Tensor,               # [s_sv, 1, d_qk], bfloat16  (h_kv 必须为 1)
    indices: torch.Tensor,          # [s_q, topk], int32
    sm_scale: float,
    d_v: int = 512,
    attn_sink: torch.Tensor = None, # [h_q], float32, 可选
    topk_length: torch.Tensor = None,  # [s_q], int32, 可选
):
    s_q, h_q, d_qk = q.shape
    assert kv.shape[1] == 1
    kv = kv.squeeze(1)                               # [s_sv, d_qk]
    topk = indices.shape[-1]

    # 1) topk_length 截断（最左 topk_length 个才有效）
    if topk_length is not None:
        cut = torch.arange(topk, device=indices.device).unsqueeze(0) >= topk_length.unsqueeze(1)
        indices = indices.clone(); indices[cut] = -1

    # 2) 无效索引掩码：-1 或 >= s_sv
    invalid = (indices < 0) | (indices >= kv.shape[0])          # [s_q, topk]
    safe = indices.clamp_min(0)                                  # 防 index_select 报错
    gathered = kv.index_select(0, safe.flatten()).reshape(s_q, topk, d_qk).float()

    # 3) score，无效位置置 -inf
    P = (q.float() @ gathered.transpose(1, 2)) * sm_scale         # [s_q, h_q, topk]
    P[invalid.unsqueeze(1).broadcast_to(P.shape)] = float("-inf")

    # 4) lse / max_logits（自然对数，对齐 ref.py）
    lse = torch.logsumexp(P, dim=-1)                              # [s_q, h_q]
    max_logits = P.amax(dim=-1)                                   # [s_q, h_q]

    # 5) attn_sink 仅影响 out：把它当作额外 logit 并入分母
    lse_for_o = lse if attn_sink is None else torch.logaddexp(lse, attn_sink.unsqueeze(0))
    lse_for_o = lse_for_o.clone()
    lse_for_o[lse_for_o == float("-inf")] = float("+inf")         # lonely query -> out 为 0
    out = (torch.exp(P - lse_for_o.unsqueeze(-1)) @ gathered[..., :d_v]).to(q.dtype)

    # 6) lonely query：无任何可参与 k 时，out=0、lse=+inf
    lonely = (lse == float("-inf"))
    lse = lse.clone(); lse[lonely] = float("+inf")
    return out, max_logits, lse
```

**需要观察的现象 / 预期结果**：

- 正常输入下，`out` 形状 `[s_q, h_q, d_v]`，`max_logits` 与 `lse` 形状 `[s_q, h_q]`。
- 把某条 query 的 indices 全设成 `-1`（或 `topk_length=0`）：该行 `lse` 变 `+inf`、`out` 全 0、`max_logits` 为 `-inf`。
- 传一个 `attn_sink`（含一个 `+inf` 元素）：对应头的 `out` 全 0；传 `-inf` 则无影响。

> 待本地验证：上述边界行为建议在你自己的环境（CPU 即可）逐一触发确认。

#### 4.3.5 小练习与答案

**练习 1**：为什么参考实现全程把 `q`、`gathered_kv` 升到 `float32` 再算？

**答案**：softmax 对数值精度敏感（指数、相减、求和），`bfloat16` 的 7 位尾数在 `exp` 后容易溢出或丢精度。先升 `float32` 算稳定，最后再把 `out` 转回 `bfloat16`，与 kernel「内部 bf16 计算、输出 bf16」的设定在容差内一致。

**练习 2**：`max_logits` 和 `lse` 有什么区别？既然 `lse` 已经包含最大值信息，为什么还要单独返回 `max_logits`？

**答案**：`lse = log Σ e^P`，`max_logits = max P`。两者都是 `[s_q, h_q]`。单独返回 `max_logits` 是为了方便上层（比如不同 split 之间做 online softmax 合并时）直接拿到稳定的减最大值锚点；它也常被用作推测解码/阈值过滤的依据。在 `ref.py` 里它就是 `P.max(-1).values`，未被 `attn_sink` 改动。

**练习 3**：`out = exp(P - lse_for_o) @ gathered_kv[..., :d_v]` 里为什么用的是 `lse_for_o`（含 attn_sink）而不是 `lse`？

**答案**：因为 `attn_sink` 的语义是「把输出再乘以 \(\frac{e^{lse}}{e^{lse}+e^{sink}}\)」。把 sink 并入分母等价于把归一化权重整体缩小这一比例，正好体现在 `exp(P - lse_for_o)` 上（`lse_for_o >= lse`，所以权重更小）。而**返回给调用方的 `lse` 不应含 sink**，所以分母用 `lse_for_o`、返回值用原始 `lse`。

---

### 4.4 输出 (out, max_logits, lse) 与 attn_sink / lonely query

#### 4.4.1 概念说明

两个 sparse 接口的返回值不同：

- **prefill `flash_mla_sparse_fwd`** → `(out, max_logits, lse)` 三元组。
- **decode `flash_mla_with_kvcache`** → `(out, lse)` 二元组（没有单独的 `max_logits`）。

| 输出 | prefill 形状 | decode 形状 | dtype |
| :--- | :--- | :--- | :--- |
| `out` | `[s_q, h_q, d_v]` | `[b, s_q, h_q, d_v]` | bfloat16 |
| `max_logits` | `[s_q, h_q]` | —（不返回） | float32 |
| `lse` | `[s_q, h_q]` | `[b, h_q, s_q]`（注意头维在前） | float32 |

两个「会改变输出」的边界机制需要特别记住：

- **`attn_sink`**：一个可选的 `[h_q]` 向量。它让最终 `out` 额外乘以 \(\frac{e^{lse}}{e^{lse}+e^{sink}}\)，但**不改变返回的 `lse` / `max_logits`**。`sink=+inf` 让对应头输出归零；`sink=-inf` 完全无影响。
- **lonely query**：当一个 query 没有任何可参与的 k（`lse=-inf`）时，参考实现把它的 `out` 强制置 0、`lse` 强制置 `+inf`（用 `+inf` 当哨兵，方便下游识别）。

decode 还有第三类语义：`extra_k_cache` + `extra_indices_in_kvcache`（第二份 KV 池），在参考实现里和主 KV 沿 topk 维拼接后统一做一次 softmax。

#### 4.4.2 核心流程

`attn_sink` 的缩放公式（decode 参考里的写法）：

\[
O \;\leftarrow\; O \cdot \frac{1}{1 + e^{\text{sink} - L}}
\]

它等价于 prefill 参考里「把 sink 当作额外 logit 并入分母」的 `lse_for_o = logaddexp(L, sink)`。两者数学等价：

\[
\frac{e^{L}}{e^{L}+e^{\text{sink}}} = \frac{1}{1+e^{\text{sink}-L}}
\]

lonely query 与 attn_sink 的优先级：先算出原始 `lse`（可能为 `-inf`），再决定 `out`；若 `lse=-inf`，则无论 sink 是什么，`out` 都应为 0（参考实现用 `lse_for_o == -inf ? +inf : lse_for_o` 把权重压成 0 来实现）。

decode 路径完整流程（含 extra KV）：

```
主 KV gather + extra KV gather  ──沿 topk 维拼接──►  [b, s_q, topk+extra_topk, d]
        │ q @ K^T * sm_scale，无效位置 -inf
        ▼
   logsumexp -> lse ;  softmax -> attn_weight
        │ attn_weight @ V[:, :d_v]
        ▼
        out  ──(若 attn_sink) 缩放──►  最终 out
        │
        ▼  lonely query: out=0, lse=+inf
```

#### 4.4.3 源码精读

**prefill 返回四元组**：[tests/ref.py:L52](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/ref.py#L52) —— `return (out.to(torch.bfloat16), out, max_logits, orig_lse)`，同时给出 bf16 与 fp32 两份 out（测试比 fp32 版以放宽量化误差）。

**prefill 的 attn_sink + lonely 处理**：[tests/ref.py:L43-L51](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/ref.py#L43-L51) —— `_merge_two_lse` 合入 sink 得 `lse_for_o`；`lse_for_o == -inf` 改 `+inf` 让 `out` 为 0；最后 `orig_lse[lonely] = +inf`。

**decode 返回与 attn_sink 缩放**：[tests/ref.py:L94-L102](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/ref.py#L94-L102) —— `output *= 1/(1+exp(attn_sink - lse))` 实现缩放；`lonely_q_mask` 把 `out` 置 0、`lse` 置 `+inf`，并 `.transpose(1,2)` 把 lse 排成 `[b, h_q, s_q]`。

**decode 的 extra KV 拼接**：[tests/ref.py:L76-L80](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/ref.py#L76-L80) —— `process_kv_scope` 各自 gather + 算掩码，再 `torch.cat([gathered_kv, gathered_kv1], dim=2)` 沿 topk 维拼接，掩码同理拼接。

**接口层返回约定**：[flash_mla/flash_mla_interface.py:L100-L103](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L100-L103)（decode 返回 `out [b,s_q,h_q,dv]` 与 `lse [b,h_q,s_q]`）；[flash_mla/flash_mla_interface.py:L200-L207](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L200-L207)（prefill 返回 `(output, max_logits, lse)`，并指引读者去看 `tests/ref.py` 的精确定义）。

**`attn_sink` 的接口契约**：[flash_mla/flash_mla_interface.py:L88](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L88) —— 「最终输出会被 `exp(lse)/(exp(lse)+exp(attn_sink))` 缩放，对返回的 lse 无影响；`+inf` 让结果变 0」。

#### 4.4.4 代码实践

**实践目标**：用 4.3 写的 `sparse_attn_ref`，亲手触发 `attn_sink` 与 lonely query 两种边界，对照接口契约验证输出。

**操作步骤**：

```python
# 示例代码：触发 attn_sink 与 lonely query（CPU）
import torch
torch.manual_seed(0)
s_q, h_q, d_qk, topk, s_sv = 3, 4, 576, 8, 16
q  = (torch.randn(s_q, h_q, d_qk, dtype=torch.bfloat16)/10).clamp(-10, 10)
kv = (torch.randn(s_sv, 1, d_qk, dtype=torch.bfloat16)/10).clamp(-10, 10)
indices = torch.randint(0, s_sv, (s_q, topk), dtype=torch.int32)
sm_scale = d_qk ** -0.5

# 基线（无 sink）
out0, max0, lse0 = sparse_attn_ref(q, kv, indices, sm_scale)   # 复用 4.3 的函数

# 加 attn_sink：第 0 头给 +inf（应让该头 out 全 0），其余随机
sink = torch.zeros(h_q)
sink[0] = float("inf")
out1, max1, lse1 = sparse_attn_ref(q, kv, indices, sm_scale, attn_sink=sink)
print("第 0 头 out 是否全 0：", torch.all(out1[:, 0, :] == 0).item())   # 期望 True
print("lse 是否不变：", torch.allclose(lse1, lse0, equal_nan=True))     # 期望 True

# lonely query：让第 2 条 query 的 indices 全无效
lonely_idx = indices.clone(); lonely_idx[2] = -1
out2, max2, lse2 = sparse_attn_ref(q, kv, lonely_idx, sm_scale)
print("lonely 行 lse 是否为 +inf：", torch.isinf(lse2[2]).all().item())  # 期望 True
print("lonely 行 out 是否全 0：", torch.all(out2[2] == 0).item())        # 期望 True
```

**需要观察的现象 / 预期结果**：

- `attn_sink[0]=+inf` → `out1[:, 0, :]` 全 0，但 `lse1 == lse0`（sink 不改 lse）。
- 全无效 indices 的行 → `lse` 为 `+inf`、`out` 全 0。

> 待本地验证：`attn_sink` 含 `-inf` 时应与基线完全一致，建议自行加一条断言确认。

#### 4.4.5 小练习与答案

**练习 1**：decode 的 lse 形状是 `[b, h_q, s_q]`（头维在前），而 out 是 `[b, s_q, h_q, d_v]`（头维在中）。为什么 lse 要转置？

**答案**：这是历史/下游约定。decode 输出 lse 常被上层当作「每个 batch、每个头、每个 query 位置」的二维表格来用，把头维放前面（`.transpose(1,2)` 之后）方便按头切片、也方便与 split-KV 合并时的累加布局对齐。读者只需记住形状约定，由接口保证一致。

**练习 2**：`attn_sink = -inf` 和不传 `attn_sink` 效果一样吗？

**答案**：一样。`logaddexp(L, -inf) = L`，分母不变，缩放因子为 1。所以 `-inf` 是「无 sink」的安全默认值，`+inf` 才是「让该头归零」的极端值。

**练习 3**：prefill 返回了 `max_logits`，decode 没有。如果你在 decode 里也想要 max_logits，能用返回的 `lse` 推出来吗？

**答案**：不能直接推出。`lse = log Σ e^P` 只给出「log-sum」，无法还原 `max P`（除非只有一个有效 token）。decode 不返回它，是因为其下游（继续做 split-KV 合并、或下一个解码步）只需要 lse；需要 max_logits 的场景应走 prefill 接口或自行在 kernel 外维护。

## 5. 综合实践

把本讲四个模块串起来，完成一个「**从逻辑索引到参考输出**」的端到端小任务（无 GPU 也能做）：

1. **造场景**：定义 `block_size=4`、2 个 batch、序列长度分别 6 和 9，随机生成一个非恒等的 `block_table`。
2. **造索引**：为每个 batch 生成 `abs_indices [b, s_q=1, topk=4]`，其中混入 1 个 `-1` 与 1 个超出该 batch 序列长度的下标。
3. **编码**：调用 `quant.abs_indices2indices_in_kvcache` 把它翻译成 `indices_in_kvcache`，打印对照。
4. **decode 参考计算**：搭一份最小的分页 KV（用 `KVScope` 思路：`blocked_k [num_blocks, block_size, 1, d_qk]`），按 `ref_sparse_attn_decode` 的逻辑（gather → 拼接 → softmax → 回乘 → attn_sink/lonely）算出 `(out, lse)`。
5. **校验**：把 `indices_in_kvcache` 当成「平坦 KV 池下标」直接 gather，验证它与「先按 abs_indices 逻辑 gather 再重排」得到完全相同的 KV 子集——这是「kernel 不需要 block_table」正确性的直接证据。

验收标准：

- 步骤 3 的输出里 `-1` 全部保留，物理下标与手算一致。
- 步骤 4 能复现一个 lonely query（某 batch 全无效）时 `out=0, lse=+inf`。
- 步骤 5 的两套 gather 结果逐元素相等。

> 提示：可以大量复用 4.3 的 `sparse_attn_ref`，只需把「gather 下标」换成 `indices_in_kvcache`、KV 换成平坦的物理块池即可。

## 6. 本讲小结

- FlashMLA 有**两套** `indices` 坐标系：prefill 是 `kv` 张量的直接下标（无效：`-1` 或 `>= s_sv`），decode 是物理块池下标 `indices_in_kvcache`（无效：仅 `-1`）。
- decode 的物理索引由 `abs_indices2indices_in_kvcache` 用 `block_table[b][abs//bs]*bs + abs%bs` 编码，把分页映射烘焙进索引，使 kernel 不再需要 `block_table`。
- 有效性由两道掩码的「或」决定：无效索引掩码 + `topk_length` 的「最左若干个」截断。
- `tests/ref.py`（`ref_sparse_attn_fwd` / `ref_sparse_attn_decode`）是数值语义的黄金参考；测试用极小容差逐项比对 `out / max_logits / lse`。
- `attn_sink` 只缩放 `out`（\(\frac{e^L}{e^L+e^{sink}}\)）、不改 `lse`/`max_logits`；`+inf` 让该头归零。
- lonely query（`lse=-inf`）被强制改成 `out=0, lse=+inf`，用 `+inf` 当哨兵。

## 7. 下一步学习建议

- 语义搞清楚后，下一讲 **[u6-l2 SM90 sparse prefill phase1 kernel](u6-l2-sm90-sparse-prefill-phase1.md)** 会进入 CUDA kernel 内部，看这些 `indices` 如何在 SM90 上被 online softmax 主循环消费、`head_dim`/`HAVE_TOPK_LENGTH` 如何编译期特化。
- 想了解 decode 侧（含 FP8 KV cache）的索引消费，可接着读 **u6-l3** 与 Unit 5 的 sparse decode 讲义。
- 建议同时翻一遍 `tests/lib.py` 的 `generate_testcase` / `KVScope` 与 `tests/test_flash_mla_sparse_prefill.py` 的用例表，它们是「语义被如何压力测试」的最好注解。

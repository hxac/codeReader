# u3-l6 MixedAttention 反向：复用 flash-attn 反向内核

## 1. 本讲目标

上一讲（u3-l5）我们读完了 `MixedAttention.forward`：两次 `_flash_attn_varlen_forward` 加一次 LSE 在线合并，并在结尾把**真实的合并 LSE** 与合并输出 `save_for_backward`。本讲精读 `MixedAttention.backward`（[moba/moba_efficient.py:L185-L267](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L185-L267)），回答三个问题：

1. **为什么「合并 out + 合并 LSE」就够**：反向不保存两路各自的 out/lse，直接把手头的合并量喂给两次 `_flash_attn_varlen_backward`，梯度为什么数学正确？每路反向为何自动「隐含」了混合 softmax 的归一化因子？
2. **`ctx.save_for_backward` 存的每个张量在反向里干什么用**，`d_moba_output` 的三连 `index_select` 与 `dmkv` 的 `stack` 打包各自解决什么问题？
3. **为什么反向之前必须裁掉零 expert**：源码注释 `cut off zero Q expert from kv , or the grad may be nan` 背后的机理是什么？

学完本讲，你应当能：

- 写出 softmax 注意力反向的 \(dv/dk/dq\) 公式与 delta 技巧，并解释「传入合并 LSE 与合并输出」如何让每条支路的梯度恰好是并集 softmax 梯度的一个划分。
- 逐参数说出两次 `_flash_attn_varlen_backward` 调用的含义与差异。
- 设计一个必然出现零 expert 的输入，实证复现「不裁剪 → 反向 NaN」。

## 2. 前置知识

### 2.1 `torch.autograd.Function` 的契约

自定义反向必须遵守三条契约：

- `backward(ctx, d_output)` 的**返回元组要与 forward 的输入（不含 ctx）一一对应**，不求梯度的位置填 `None`。`MixedAttention.forward` 有 11 个输入（[moba/moba_efficient.py:L70-L83](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L70-L83)），所以 backward 恰好返回 11 个值（[L267](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L267)）。
- 张量经 `ctx.save_for_backward` 保存后在反向里用 `ctx.saved_tensors` 取回；Python 数值（`max_seqlen`、`moba_chunk_size`、`softmax_scale`）直接挂成 ctx 属性（[L84-L86](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L84-L86)）。
- **Function 边界之外的算子仍由普通 autograd 接管**。`moba_q` 是在 Function 外面用 `index_select` 从 `q` 里拷出来的，所以 backward 返回的 `dmq` 会被外层 `index_select` 的反向（散射累加）自动送回 `q.grad`——这是 4.4 节的关键。

### 2.2 FlashAttention-2 反向内核需要什么

flash-attn 反向**不保存注意力矩阵**：打分 \(s_{ij} = q_i \cdot k_j \cdot \gamma\)（\(\gamma\) 即 `softmax_scale = d^{-1/2}`）由内核从 q、k 现场重算。它额外要求调用方提供**前向输出 `out`** 和**每行的 `softmax_lse`**，其余梯度全部由此推出。对某条 query 行 \(i\)、key \(j\)：

\[ P_{ij} = e^{s_{ij} - \mathrm{lse}_i}, \qquad dS_{ij} = \mathrm{dout}_i \cdot v_j, \qquad \delta_i = \sum_j P_{ij}\, dS_{ij} \]

\[ dv_j = \sum_i P_{ij}\, \mathrm{dout}_i, \qquad dk_j = \sum_i P_{ij} (dS_{ij} - \delta_i)\, q_i \gamma, \qquad dq_i = \sum_j P_{ij} (dS_{ij} - \delta_i)\, k_j \gamma \]

其中 \(\delta_i\) 有个著名恒等式（FlashAttention-2 的 **delta 技巧**）：

\[ \delta_i = \sum_j P_{ij}\, (\mathrm{dout}_i \cdot v_j) = \mathrm{dout}_i \cdot \Big( \sum_j P_{ij} v_j \Big) = \mathrm{dout}_i \cdot \mathrm{out}_i \]

也就是说 **\(\delta\) 不用 \(P\) 也能算，只要拿到 (dout, out) 的行内积**——这就是反向内核需要传入 `out` 的原因（内核实现可在本地安装包 `flash_attn/flash_attn_interface.py` 与其 CUDA 源码中核对，待本地验证）。

### 2.3 从前向继承的两样「遗产」

u3-l5 的结尾专门做了两件事，全部是为本讲服务的：

- [L166-L168](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L166-L168)：输出转回 `q.dtype`，并把减掉的 `max_lse_1d` **加回** `mixed_attn_lse_sh`，恢复成真实的（未偏移）合并 LSE——反向内核要用它重算 \(P\)，偏移版会让梯度整体错误。
- [L169-L181](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L169-L181)：把 `output`、`mixed_attn_lse_sh` 连同全部输入 `save_for_backward`。

注意**两路各自的 out / lse 都没有保存**——它们在前向里只是中间量，反向完全不需要（4.1 节解释为什么）。另外回忆 u3-l4 的产物形状：`moba_q [N, 1, D]`（头折叠进批次，单「头」）、`moba_kv [M, 2, 1, D]`（第 1 维是 K/V 槽位）、地址簿 `moba_q_sh_indices [N]`。

## 3. 本讲源码地图

| 文件 | 本讲关注的范围 | 作用 |
|---|---|---|
| `moba/moba_efficient.py` | `MixedAttention.backward`（L185-L267） | 本讲主战场：两次内核反向 + 打包返回 |
| `moba/moba_efficient.py` | `save_for_backward` 与「加回 max」（L166-L181） | 反向可用的全部「遗产」 |
| `moba/moba_efficient.py` | 前向两次内核调用（L88-L120） | 对照反向参数（causal、cu_seqlens、布局） |
| `moba/moba_efficient.py` | `moba_attn_varlen` 中零 expert 裁剪（L391-L428） | 反向 NaN 的防线，裁剪发生在进 Function 之前 |
| `moba/moba_efficient.py` | `moba_q` / `moba_kv` 构造（L375-L386、L406-L414） | 4.4 节梯度回路的「来路」 |
| `tests/test_moba_attn.py` | 全文（L37-L100） | 梯度对齐测试：黄金参考与容忍度 |
| flash-attn 2.6.3 `flash_attn/flash_attn_interface.py` | `_flash_attn_varlen_backward` 签名 | 理解参数含义（需读本机安装的包，待本地验证） |

## 4. 核心概念与源码讲解

### 4.1 数学依据：并集 softmax 的梯度为何能拆给两次内核反向

#### 4.1.1 概念说明

u3-l5 证明了核心恒等式：LSE 合并后的输出**就是**把两路 key 拼在一起做一次 softmax 的输出：

\[ \text{out}_{A \cup B} = \sum_{p} e^{\mathrm{lse}_p - \mathrm{lse}_{A \cup B}} \cdot \text{out}_p = \sum_{j \in A \cup B} e^{s_{ij} - \mathrm{lse}_{A \cup B}} v_j \]

既然前向等于「并集 softmax」，反向就应当是「并集 softmax 的梯度」。而 2.2 节的公式显示：内核算梯度只需要三样东西——\(s_{ij}\)（从 q、k 重算）、\(P_{ij}\)（从 lse 重算）、\(\delta_i\)（从 dout、out 重算）。**这三样全部换成「并集版本」后，公式依然成立**，且每个 key 的梯度只从「注意到它的 query」收集。于是可以把并集按 key 集合拆成两半，分别交给两次内核调用：

- **self 支路**：key = 各 query 的当前块（块内因果），即 `cu_chunk` 边界下的全量序列。
- **moba 支路**：key = 各 (query, 选中块) 记录对应的整块。

最妙的一点：前向里显式出现的重加权系数 \(\text{factor} = e^{\mathrm{lse}_p - \mathrm{lse}_{mixed}}\) 在反向里**从未出现**——它的作用已经被「\(P\) 改用并集分母」隐式吸收了。

#### 4.1.2 核心流程

把「传合并量即可」拆成四步验证：

1. **\(s_{ij}\) 与支路无关**：内核从 \(q_i, k_j\) 重算打分，与key 属于哪条支路无关，天然就是并集 softmax 里的那个 \(s_{ij}\)。
2. **传合并 LSE ⇒ \(P\) 变并集权重**：内核算 \(P_{ij} = e^{s_{ij} - \mathrm{lse}_{passed}}\)。传入 \(\mathrm{lse}_{mixed}\) 后，本支路内的每个 key 拿到的正是并集 softmax 权重（分母是全部选中 key 的指数和，不再只是本路的）。
3. **传合并 out ⇒ \(\delta\) 变并集 delta**：\(\delta_i = \mathrm{dout}_i \cdot \mathrm{out}_{mixed,i} = \sum_{j \in \text{并集}} P_{ij} dS_{ij}\)，恰好是并集公式要求的那个 \(\delta_i\)。
4. **key 集合两路不重叠 ⇒ 梯度可加**：对任一 query，当前块不可能同时是被选中的历史块（u3-l3 的 gate 掩码把当前块排除在候选外），所以 self 支路与 moba 支路覆盖的 (query, key) 对**互不重叠、并集恰为 MoBA 语义的全部合法对**。于是：

\[ dv_j = \underbrace{dv_j^{(self)}}_{\text{第一次调用}} + \underbrace{dv_j^{(moba)}}_{\text{第二次调用}}, \qquad \text{同理 } dk_j;\qquad dq_i = dq_i^{(self)} + \sum_{\text{记录}} dmq_{(i,\cdot)} \]

若改传「各支路自己的 out/lse」，两次调用算出的是「每路单独当完整 softmax」的梯度——既丢掉了 factor 的链式修正，\(\delta\) 也只对本路 key 求和，结果必然错误。

#### 4.1.3 源码精读

这一数学设计落在两次调用的两个参数上。self 支路（[moba/moba_efficient.py:L208-L229](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L208-L229)）：

```python
dq, dk, dv, _ = _flash_attn_varlen_backward(
    dout=d_output,
    q=q, k=k, v=v,
    out=output,                                    # ← 合并输出，用于算 δ
    softmax_lse=mixed_attn_vlse_sh.t().contiguous(),  # ← 合并 LSE，用于算 P
    ...
    causal=True,
)
```

`out=output` 与 `softmax_lse=mixed_...` 两行就是「传合并量」的全部：内核由此重算出并集权重与并集 delta，返回的 `dq/dk/dv` 已经是并集梯度里「当前块部分」的正确值——**不需要**再乘任何 factor。moba 支路同理（[L243-L264](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L243-L264)），传的是按记录选取的 `out=moba_output`、`softmax_lse=mixed_attn_vlse`（4.4 节细讲这两者怎么来的）。两次调用的 `dq/dk/dv` 与 `dmq/dmk/dmv` 相加即为完整梯度，加法由 autograd 自动完成（4.4 节）。

顺带回收 u3-l5 练习 3 的伏笔：若前向忘了在 [L168](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L168) 把 max 加回，这里传出的 LSE 整体偏移 \(-m\)，\(P\) 全部偏乘 \(e^{m}\) 且 \(\delta\) 也随之变化——不是一个统一缩放，而是系统性错梯度。

#### 4.1.4 代码实践

1. **实践目标**：在 CPU 上用小例子验证两件事——(a) delta 恒等式 \(\sum_j P_{ij} dS_{ij} = \mathrm{dout}_i \cdot \mathrm{out}_i\)；(b) 用「合并 lse/out」对单条支路手工求 \(dv\)，与「全量 softmax + autograd」的基准一致。
2. **操作步骤**：

   ```python
   # 示例代码：delta 恒等式 + 合并量驱动的支路梯度（CPU 即可）
   import torch
   torch.manual_seed(0)
   d, na, nb = 8, 3, 5                      # head_dim、A 路（self）key 数、B 路（moba）key 数
   q  = torch.randn(d)
   Ka, Va = torch.randn(na, d), torch.randn(na, d)
   Kb, Vb = torch.randn(nb, d), torch.randn(nb, d)
   gamma, dO = d ** -0.5, torch.randn(d)

   def branch(K, V):                         # 单路 softmax 注意力 + LSE
       s = (K @ q) * gamma
       return torch.softmax(s, 0) @ V, s.logsumexp(0)

   Oa, la = branch(Ka, Va); Ob, lb = branch(Kb, Vb)
   m = torch.maximum(la, lb)                 # u3-l5 的合并公式
   lse_mix = m + ((la - m).exp() + (lb - m).exp()).log()
   O = (la - lse_mix).exp() * Oa + (lb - lse_mix).exp() * Ob

   # (a) delta 恒等式
   s_all = torch.cat([(Ka @ q) * gamma, (Kb @ q) * gamma])
   P = (s_all - lse_mix).exp()
   dS = dO @ torch.cat([Va, Vb]).T
   print((P * dS).sum(), (dO * O).sum())     # 两者应相等

   # (b) 用合并 lse 对 A 路手工求 dv，对照全量 softmax 的 autograd
   Pa = ((Ka @ q) * gamma - lse_mix).exp()   # 关键：分母是并集 LSE
   dv_a_manual = Pa[:, None] * dO            # dv_j = Σ_i P_ij dout_i（单 query）

   Kg = torch.cat([Ka, Kb]).clone().requires_grad_()
   Vg = torch.cat([Va, Vb]).clone().requires_grad_()
   qg = q.clone().requires_grad_()
   (torch.softmax((Kg @ qg) * gamma, 0) @ Vg).backward(dO)
   print(torch.allclose(dv_a_manual, Vg.grad[:na], atol=1e-6))
   ```

3. **需要观察的现象**：(a) 的两个打印值一致；(b) 打印 `True`——即「A 路的 key + 合并 LSE」算出的 \(dv\) 恰好等于全量 softmax 梯度中属于 A 路 key 的那几行。
4. **预期结果**：两个断言均通过。这从数值上确认了 4.1.2 的第 2、3 步：换掉 \(P\) 的分母与 \(\delta\) 的求和范围，支路反向自动「归一到并集」。待本地验证具体打印值。

#### 4.1.5 小练习与答案

**练习 1**：backward 为什么可以不保存两路各自的 out / lse？

**答案**：内核只需要 \(s_{ij}\)（q、k 重算）、\(P_{ij}\)（q、k 加传入 lse 重算）和 \(\delta\)（dout、传入 out 行和）。传合并 lse 使 \(P\) 恰为并集权重，传合并 out 使 \(\delta\) 恰为并集 delta——支路自身的 out/lse 提供的信息已被合并量覆盖，保存它们纯属浪费显存。

**练习 2**：如果两次调用各传「本支路自己的 lse/out」，得到的是什么梯度？错在哪里？

**答案**：得到「把每路单独当成完整 softmax」的梯度。错误有二：\(P\) 的分母只含本路 key（相当于丢掉了 factor \(e^{\mathrm{lse}_p - \mathrm{lse}_{mixed}}\) 的链式修正），\(\delta\) 也只对本路 key 求和；两者叠加使 dv/dk/dq 系统性偏离并集梯度（通常分母偏小、梯度偏大）。

**练习 3**：为什么前向的 `factor` 在反向代码里一次都没出现？

**答案**：factor 的效果等价于「把本路的 softmax 分母换成并集分母」。反向内核重算 \(P_{ij} = e^{s_{ij} - \mathrm{lse}_{passed}}\) 时传入的正是并集 LSE，换分母这件事在重算 \(P\) 的瞬间自动完成，无需显式乘子。

### 4.2 `save_for_backward` 清单与 backward 骨架

#### 4.2.1 概念说明

自定义反向的第一课是「存了什么、用了什么」。`MixedAttention` 的选择非常克制：**只存合并量 + 内核必需输入**，一共 11 个张量；前向里琳琅满目的中间量（两路 out、两路 lse、`factor`、`max_lse_1d`、`gate`、`gate_mask`）一概不存。gate 不存还有一层原因：gate 只产生整数索引和布尔掩码（topk 下标、`isinf` 掩码），**梯度不经过 gate 路径**——MoBA 的路由是「硬」路由，q/k/v 到输出的可导路径只有 self 支路与 moba 支路两条。

#### 4.2.2 核心流程

backward 一共五步：

```
1. 取回 saved_tensors + ctx 属性；d_output.contiguous()
2. 第一次 _flash_attn_varlen_backward：self 支路（causal=True）→ dq, dk, dv
3. 三个 index_select 组装 moba 支路的 dout / out / lse（按记录选取）
4. 第二次 _flash_attn_varlen_backward：moba 支路（causal=False）→ dmq, dmk, dmv
5. dmkv = stack(dmk, dmv)；返回 11 元组（6 个 None）
```

#### 4.2.3 源码精读

保存清单（[moba/moba_efficient.py:L169-L181](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L169-L181)）与各张量在反向中的去处：

| 保存的张量 | 形状 | 反向中的用途（行号） |
|---|---|---|
| `output` | `[S, H, D]` | 两次调用的 `out` 参数：算 \(\delta\)（L213、L235-L237、L248） |
| `mixed_attn_lse_sh` | `[S, H]` | 两次调用的 `softmax_lse` 参数：算 \(P\)（L214、L239-L241、L249） |
| `q, k, v` | `[S, H, D]` | self 支路内核输入；重算 \(s_{ij}\)（L210-L212） |
| `self_attn_cu_seqlen` | `[C+1]` | self 支路 varlen 边界（L218-L219） |
| `moba_q` | `[N, 1, D]` | moba 支路内核输入（L245） |
| `moba_kv` | `[M, 2, 1, D]` | moba 支路内核输入，`[:,0]`/`[:,1]` 拆出 K/V（L246-L247） |
| `moba_cu_seqlen_q` / `moba_cu_seqlen_kv` | `[R+1]` | moba 支路两侧 varlen 边界（L253-L254） |
| `moba_q_sh_indices` | `[N]` | 地址簿：按记录选取 out/lse/dout（L233、L236、L240） |

反向取回与第一次调用之间只有一行准备代码（[L192-L206](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L192-L206)）：解包 `ctx.saved_tensors`、读回三个标量属性、`d_output = d_output.contiguous()`——CUDA 内核要求连续内存，上游算子（如 reshape）可能给出非连续视图，这一行是廉价保险。

#### 4.2.4 代码实践

1. **实践目标**：通过「找用途」建立保存清单的完整心智模型（源码阅读型实践）。
2. **操作步骤**：
   1. 打开 [moba/moba_efficient.py:L169-L181](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L169-L181)，把 11 个保存张量抄成一张表。
   2. 在 L185-L267 里为每一个找到被使用的行号，与 4.2.3 的表格对照。
   3. 再列出前向计算过、但**没有**保存的中间量（提示：`self_attn_out_sh`、`moba_attn_out`、两路 lse、`factor`、`max_lse_1d`、`gate`、`gate_mask`），逐个说明为什么不需要保存。
3. **需要观察的现象**：11 个保存张量在 backward 中全部至少被使用一次；未保存的中间量都能归入「被合并量覆盖」或「不可导（整数/布尔）」两类。
4. **预期结果**：完成表格。特别注意 `moba_q_sh_indices` 是整型张量却仍被保存——它不参与求导，只作为地址簿被 `index_select` 使用。

#### 4.2.5 小练习与答案

**练习 1**：`mixed_attn_lse_sh` 为什么必须在保存前加回 `max_lse_1d`？

**答案**：反向内核用它重算 \(P_{ij} = e^{s_{ij} - \mathrm{lse}}\)。偏移版 LSE 会让每个 \(P\) 偏乘 \(e^{m}\)，且 \(\delta\)（经 out 与 dout 的行和，out 未偏移）与 \(P\) 不再匹配，梯度系统性错误。加回真实值是反向正确性的硬前提。

**练习 2**：gate 用了 `q` 和 `k`（算 key_gate_weight、einsum 打分），为什么它们的梯度不经过 gate 路径？

**答案**：gate 张量最终只通过 `torch.topk`（返回整数下标）、`isinf`（转布尔掩码）、`nonzero`/`sum`（索引与计数）影响输出，这条链上全是不可导的整数/布尔算子，autograd 在此断开。可导路径只剩 self 支路与 moba 支路，所以 backward 只需覆盖这两条。

**练习 3**：`ctx.max_seqlen` 等三个标量为什么走 ctx 属性而不是 `save_for_backward`？

**答案**：`save_for_backward` 面向张量（有引用计数与版本检查）；Python 数值无需求导也无需这些机制，直接挂属性最简单。反向里 `ctx.max_seqlen`、`ctx.moba_chunk_size`、`ctx.softmax_scale`（[L188-L190](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L188-L190)）原样读回。

### 4.3 两次 `_flash_attn_varlen_backward` 调用逐参数精读

#### 4.3.1 概念说明

两次调用结构完全对称，差异全在「服务哪条支路」。self 支路是**普通 varlen 因果注意力**的反向（边界 `cu_chunk`、`causal=True`）；moba 支路是**重组后单头 varlen 非因果注意力**的反向（边界 `moba_cu_seqlen_q/kv`、`causal=False`，头已折叠进批次维）。`softmax_lse 复用`是两者的共同灵魂：都传合并 LSE（4.1 节），因此都不需要各自的支路 LSE。

#### 4.3.2 核心流程

| 参数 | self 支路（L208-L229） | moba 支路（L243-L264） |
|---|---|---|
| `dout` | `d_output` 全量 `[S,H,D]` | `d_moba_output` 按记录选取 `[N,1,D]` |
| `q / k / v` | 原始 `q,k,v` | `moba_q`、`moba_kv[:,0]`、`moba_kv[:,1]` |
| `out` | `output`（合并输出） | `moba_output`（合并输出按记录选取） |
| `softmax_lse` | `mixed_attn_vlse_sh.t().contiguous()` → `[H,S]` | `mixed_attn_vlse` → `[1,N]` |
| `cu_seqlens_q/k` | 均为 `self_attn_cu_seqlen`（= `cu_chunk`） | `moba_cu_seqlen_q` / `moba_cu_seqlen_kv` |
| `max_seqlen_q/k` | `max_seqlen` / `max_seqlen` | `max_seqlen` / `moba_chunk_size` |
| `causal` | `True` | `False` |
| 其余 | `dropout_p=0, window_size=(-1,-1), softcap=0.0, alibi_slopes=None, deterministic=True` | 同左 |

#### 4.3.3 源码精读

self 支路调用（[moba/moba_efficient.py:L208-L229](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L208-L229)）：

```python
dq, dk, dv, _ = _flash_attn_varlen_backward(
    dout=d_output, q=q, k=k, v=v,
    out=output,
    softmax_lse=mixed_attn_vlse_sh.t().contiguous(),
    dq=None, dk=None, dv=None,          # 不预分配梯度缓冲，由内核分配
    cu_seqlens_q=self_attn_cu_seqlen,
    cu_seqlens_k=self_attn_cu_seqlen,
    max_seqlen_q=max_seqlen, max_seqlen_k=max_seqlen,
    softmax_scale=softmax_scale,
    causal=True,                        # 当前块内因果（与前向 L89-L102 对称）
    dropout_p=0.0, window_size=(-1, -1), softcap=0.0,
    alibi_slopes=None, deterministic=True,
)
```

四个细节：

- **`softmax_lse=mixed_attn_vlse_sh.t().contiguous()`**：保存的合并 LSE 是 `[S, H]`（s 优先，前向 L119 转过来的），内核期望 head 优先 `[H, total]`，这里 `.t()` 转回去并 `contiguous()`——与 u3-l5 前向的转置互为镜像。
- **`causal=True`**：当前块内的因果性由内核的因果掩码保证，与前向 self 支路一致。
- **`deterministic=True`**：flash-attn 反向里 dk/dv 的多对一归约默认用原子加（浮点顺序不定、结果不可复现）；此选项改用确定性归约，牺牲一点速度换取可复现——对「逐位对齐 naive」的测试友好。
- **第 4 个返回值被 `_` 丢弃**：flash-attn 2.6.3 的该函数返回 `(dq, dk, dv, softmax_dse)`，末项即 \(\delta\) 行和，本项目已通过 `out` 隐式用它，无需显式接收（可在本地安装包内核实签名，待本地验证）。

moba 支路调用（[moba/moba_efficient.py:L243-L264](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L243-L264)）：

```python
dmq, dmk, dmv, _ = _flash_attn_varlen_backward(
    dout=d_moba_output,
    q=moba_q, k=moba_kv[:, 0], v=moba_kv[:, 1],
    out=moba_output, softmax_lse=mixed_attn_vlse,
    cu_seqlens_q=moba_cu_seqlen_q, cu_seqlens_k=moba_cu_seqlen_kv,
    max_seqlen_q=max_seqlen, max_seqlen_k=moba_chunk_size,
    softmax_scale=softmax_scale,
    causal=False,                       # 因果性已在 gate 掩码阶段前置（u3-l3）
    ...
    deterministic=True,
)
```

三个细节：

- **`causal=False`**：每条记录的 key 集合是被 gate 选中的**整块历史 key**，块内无因果约束——因果性早在选块阶段由 `gate_chunk_end_mask` 强制（u3-l3），这里再掩就重复了。
- **`max_seqlen_k=moba_chunk_size`**：候选块全是满块（u3-l2 的过滤保证了这一点），KV 侧每段长度恒为 `moba_chunk_size`，Q 侧才需要 `max_seqlen`。
- **`softmax_lse=mixed_attn_vlse` 形状 `[1, N]`**：moba 支路头维已被折叠成 1（`moba_q` 的 `unsqueeze(1)`），LSE 的 head 维自然也是 1，`view(1, -1)` 恰好凑出 `[H=1, total]` 布局，无需转置。

#### 4.3.4 代码实践

1. **实践目标**：把源码参数表与本机安装的 flash-attn 签名逐一对上（源码核对型实践）。
2. **操作步骤**：
   1. 在安装了 flash-attn 2.6.3 的环境运行：

      ```bash
      python -c "import inspect; from flash_attn.flash_attn_interface import _flash_attn_varlen_backward as f; print(inspect.signature(f))"
      ```

   2. 对照 4.3.2 的参数表，确认每个关键字参数都存在于签名中，并记下签名里还有哪些本项目没用的参数。
   3. 纸上推演：若把 `softmax_lse` 换成「减了 max 的偏移版」，\(P\) 与 \(\delta\) 各自怎么变？梯度是整体缩放还是畸变？
3. **需要观察的现象**：签名参数与调用一一匹配；推演结论应是——\(P\) 偏乘 \(e^{m}\) 而 \(\delta\)（来自未偏移的 out）不变，两者不再自洽，\(dk/dq\) 中的 \((dS_{ij} - \delta_i)\) 项被系统性扭曲，梯度**畸变**而非整体缩放。
4. **预期结果**：得到一张「源码参数 → 签名参数」对照表。若本环境无 GPU/flash-attn，可先在 [flash_attn/flash_attn_interface.py](https://github.com/Dao-AILab/flash-attention) 仓库页面核对 v2.6.3 标签下的源码。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：self 支路 `causal=True`、moba 支路 `causal=False`，为什么正好相反？

**答案**：self 支路处理「query 与自己当前块内更早 token」的注意力，块内因果必须由内核保证；moba 支路的 key 是被选中的历史整块，合法性（块末 ≤ query 位置）已由 gate 掩码在选块阶段强制，块内所有 key 对该 query 都可见，故用非因果内核。

**练习 2**：`.t().contiguous()` 与 `view(1, -1)` 分别在解决什么问题？

**答案**：内核要求 LSE 为 head 优先布局 `[H, total]`。self 支路保存的合并 LSE 是 `[S, H]`，转置并连续化得 `[H, S]`；moba 支路头维折叠为 1，只需把按记录选取的一维张量 reshape 成 `[1, N]`，无需转置。

**练习 3**：`deterministic=True` 有什么代价？为什么这个项目倾向打开？

**答案**：确定性归约通常比原子加略慢。但本项目要把 efficient 与 naive 的梯度做数值对齐（u4-l2 的测试），不确定的浮点累加顺序会让失败难以复现，牺牲少量速度换可复现性是划算的。

### 4.4 `d_moba_output` 索引选取、`dmkv` 打包与外层梯度回路

#### 4.4.1 概念说明

moba 支路的 `dout/out/lse` 三样都不能直接用全局张量：内核的世界里没有 `[S, H]`，只有「N 条单头记录」。于是 backward 用地址簿 `moba_q_sh_indices` 做三次**按记录 gather**，把全局量抄成记录级副本。这里有一个容易混淆的点：`d_moba_output` 用 `index_select`（每条记录**复制一份**上游梯度），而不是 `index_add`——因为 varlen 展开后每条记录都需要自己完整的 dout 副本；**多对一的累加**发生在别处：内核内部对 dmk/dmv 按 key 归约，以及外层 autograd 把 dmq 散射回 q 时。

另一端，`dmk`、`dmv` 是内核按 `moba_kv` 的展平布局产出的，必须 `stack` 回 `[M, 2, 1, D]` 与 `moba_kv` 严格同形——autograd 规定梯度和输入形状一致。此后外层算子的反向（cat→split→rearrange→index_select→stack 的逆过程）自动把 dmk/dmv 累加回 `k.grad/v.grad`。

#### 4.4.2 核心流程

```
# 三次按记录 gather（形状换算）
d_output[S,H,D]  --view--> [S*H, D] --index_select(idx)--> [N, D] --unsqueeze(1)--> d_moba_output[N,1,D]
output  [S,H,D]  --> 同上 --> moba_output[N,1,D]                # 内核算 δ 用
lse     [S,H]    --view--> [S*H] --index_select(idx) --view(1,-1)--> mixed_attn_vlse[1,N]

# 内核第二次反向 → dmq[N,1,D], dmk/dmv[M,1,D]

# 打包与返回
dmkv = stack(dmk, dmv, dim=1)          # [M, 2, 1, D]，与 moba_kv 同形
return dq, dk, dv, None, dmq, dmkv, None, None, None, None, None

# 外层 autograd 的接力（Function 之外的算子反向）
q.grad  = dq + scatter_back(dmq)       # index_select 反向 = index_add
k.grad += scatter_back(dmkv[:, 0])     # 经 rearrange/cat/index_select 的反向链
v.grad += scatter_back(dmkv[:, 1])
```

#### 4.4.3 源码精读

三次按记录选取（[moba/moba_efficient.py:L231-L241](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L231-L241)）：

```python
headdim = q.shape[-1]
d_moba_output = (
    d_output.view(-1, headdim).index_select(0, moba_q_sh_indices).unsqueeze(1)
)   # 上游梯度按记录复制：合并输出是对记录求和得到的，每条记录都收到完整 d_output
moba_output = (
    output.view(-1, headdim).index_select(0, moba_q_sh_indices).unsqueeze(1)
)   # 合并输出按记录抄一份，供内核算 δ_i = dout_i · out_i
mixed_attn_vlse = (
    mixed_attn_vlse_sh.view(-1).index_select(0, moba_q_sh_indices).view(1, -1)
)   # 合并 LSE 按记录抄一份，供内核算 P = exp(s − lse)
```

为什么 `d_moba_output` 是「复制」而不是「累加」：前向里合并输出 = self 贡献 + Σ 记录贡献，对加法求链式梯度时**每个加数收到相同的上游梯度** \(\mathrm{dout}\)；而每条记录在 varlen 世界里是一条独立序列，必须各自持有一份副本。`unsqueeze(1)` 把 `[N, D]` 变 `[N, 1, D]`，对齐 `moba_q` 的单头布局；`view(1, -1)` 把一维 LSE 凑成 `[H=1, N]`。

打包返回（[moba/moba_efficient.py:L266-L267](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L266-L267)）：

```python
dmkv = torch.stack((dmk, dmv), dim=1)
return dq, dk, dv, None, dmq, dmkv, None, None, None, None, None
```

`dmkv [M, 2, 1, D]` 与 forward 输入 `moba_kv`（[L414](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L414) 产出）同形。6 个 `None` 对应 6 个不可导输入：`self_attn_cu_seqlen`、`moba_cu_seqlen_q`、`moba_cu_seqlen_kv`、`max_seqlen`、`moba_chunk_size`、`moba_q_sh_indices`。随后外层接力：`moba_q` 来自 `rearrange(q,...).index_select(0, moba_q_indices)`（[L381-L384](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L381-L384)），其 `index_select` 反向是 `index_add`，把 `dmq` 散射累加进 `q.grad`；`moba_kv` 来自 `stack(k,v)` → `index_select` → `rearrange/split/cat`（[L406-L414](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L406-L414)），全是形状置换（候选块为满块、token 不重复），反向把 `dmkv` 拆回 k/v 侧累加。最终：

\[ q.grad = dq + \mathrm{scatter}(dmq), \qquad k.grad = dk + \mathrm{scatter}(dmk), \qquad v.grad = dv + \mathrm{scatter}(dmv) \]

—— 4.1 节「两路相加」的加法正是在这里完成的。

#### 4.4.4 代码实践

1. **实践目标**：对照 `tests/test_moba_attn.py`，解释梯度断言 `gqkv_diff` 的三层容忍度为什么这样设。
2. **操作步骤**：
   1. 阅读 [tests/test_moba_attn.py:L44-L100](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_attn.py#L44-L100)：`dtype=torch.bfloat16`、`eps=2e-2`（L44-L45）；efficient 前向+反向（L54-L63）后把三份梯度 `stack` 成 `gqkv`（L64）；**清零同一组 q/k/v 的梯度**再跑 naive 参考得 `gqkv_ref`（L67-L80）；断言依次是 `allclose(atol=rtol=2e-2)`（L92-L93）、`gqkv_diff.max() < 4e-2`、`gqkv_diff.mean() < 4e-4`（L99-L100）。
   2. 回答三个问题：(a) 为什么以 naive 的 autograd 梯度为参考，而不是数学解析解？(b) `mean` 容忍度凭什么敢比 `max` 严 100 倍？(c) `vo_grad = torch.randn_like(q)`（L51）随机上游梯度起什么作用？
3. **需要观察的现象 / 预期答案**：
   - (a) naive 实现是「带掩码 softmax 的普通 PyTorch 计算图」，梯度由框架自动微分保证正确，是现成的黄金参考（u2-l1 建立的原则）；解析解要手推且同样会有数值误差。
   - (b) 两个实现前向就有 bf16 级差异（不同累加顺序、flash-attn 分块），经链式法则传入梯度。**纯数值噪声**只稀疏地影响少数位置——`max < 4e-2` 放过孤立离群点；而**实现 bug**（某块梯度整体算错）会让大量元素超差，`mean < 4e-4` 必然拦截。一松一严构成「噪声可过、bug 必死」的网。
   - (c) 随机 dout 保证输出每个分量都有非零梯度回流，若某条梯度路径被漏算（比如忘了 dmq 散射回 q），不会被「恰好零梯度」掩盖。
   - 附带观察：L67-L69 复用同一组 q/k/v 并手动清零梯度，保证两组梯度严格同源。
4. **预期结果**：写成一段说明。若本地有 GPU，可把 `dtype` 改成 `torch.float32` 重跑，观察 `gqkv_diff.max()` 能小到什么量级（预期 1e-4 级，可据此收紧 eps；待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：`d_moba_output` 为什么用 `index_select`（复制）而不用 `index_add`（累加）？

**答案**：varlen 展开后每条记录是一条独立序列，需要一份完整的 dout。前向的「多对一」发生在**输出侧**（多条记录写入同一 (s,h)），由内核对 dmk/dmv 的 key 侧归约与外层 dmq 散射各自处理；dout 侧不存在多对一，只有复制。

**练习 2**：`dmk`、`dmv` 为什么要 `stack` 成 `dmkv` 再返回，而不是拆成两个返回值？

**答案**：backward 返回值必须与 forward 输入一一对应。forward 收到的是一个张量 `moba_kv [M,2,1,D]`（K/V 打包在维度 1），梯度必须同形；拆开返回反而没有位置可放。`stack(dim=1)` 正好复刻前向 `torch.stack((k, v), dim=1)`（[L299](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L299)）的布局。

**练习 3**：`q.grad` 由哪几路相加而成？分别经过什么算子？

**答案**：两路。① backward 直接返回的 `dq`（self 支路，当前块内 key 的贡献）；② `dmq` 经外层 `unsqueeze` → `index_select`（反向为 index_add 散射）→ `rearrange`（反向还原布局）累加进 `q.grad`（被选中块的贡献）。两条路覆盖的 key 集合不重叠（4.1 节），相加即并集。

### 4.5 零 expert 裁剪：反向为什么会 NaN

#### 4.5.1 概念说明

回顾 u3-l4：零 expert = 某个 (块, head) 段没有任何 query 选中，`moba_seqlen_q` 中出现 0。裁剪代码不在 `MixedAttention` 里，而在 `moba_attn_varlen` 进 Function **之前**（[L391-L423](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L391-L423)）。源码注释直说了动机：

> `# cut off zero Q expert from kv , or the grad may be nan`（[L411-L413](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L411-L413)）

机理：varlen 接口隐含一个**对称性假设**——每个段内「每个 q 行都有一条 lse 行、每段 Q 与 KV 配对完整」。零 expert 段是「0 个 Q 对 `moba_chunk_size` 个 KV」的畸形段：**前向**无恙，因为零长段不产生任何 q 行，输出又只经地址簿汇聚真实记录；**反向**内核遍历段内的 KV block 时，对应的 q/lse 行并不存在，内核可能读到未初始化或越界的 lse 去算 \(e^{s - \mathrm{lse}}\)，垃圾值一步就变成 NaN/Inf 写进该段的 dmk/dmv，再经外层散射污染 k.grad/v.grad。所以防御必须做在反向发生之前——即前向组装张量时就把零 expert 段从 Q、KV 两侧同步剔除。

#### 4.5.2 核心流程

裁剪要三处同步，漏一处要么断言爆炸要么边界错位：

```
1. Q 侧：moba_seqlen_q = moba_seqlen_q[valid_expert_mask]   # 去掉 0 段 → cu_seqlen_q 变短
2. KV 侧：moba_kv = moba_kv[valid_expert_mask]              # 同步删掉对应段的 KV 行
3. 边界：moba_cu_seqlen_kv 的 arange 上限减去 zero_expert_count
        # 段数少了，等差边界终点也要少，保证两侧形状一致
4. 兜底：assert moba_cu_seqlen_kv.shape == moba_cu_seqlen_q.shape
```

#### 4.5.3 源码精读

零 expert 的发现与 Q 侧过滤（[moba/moba_efficient.py:L391-L405](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L391-L405)）：

```python
# cut off zero experts
q_zero_mask = moba_seqlen_q == 0
valid_expert_mask = ~q_zero_mask
zero_expert_count = q_zero_mask.sum()
# only keep the kv that has q select > 0
if zero_expert_count > 0:
    moba_seqlen_q = moba_seqlen_q[valid_expert_mask]
# moba cu_seqlen for flash attn
moba_cu_seqlen_q = torch.cat((torch.tensor([0], ...), moba_seqlen_q.cumsum(dim=0)), ...)
```

KV 侧同步裁剪（[L406-L414](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L406-L414)）：

```python
moba_kv = rearrange(filtered_kv, "s x h d -> h s x d")
moba_kv = moba_kv.split(moba_chunk_size, dim=1)
moba_kv = torch.cat(moba_kv, dim=0)
if zero_expert_count > 0:
    assert valid_expert_mask.sum() == moba_kv.shape[0] - zero_expert_count
    moba_kv = moba_kv[valid_expert_mask]  # cut off zero Q expert from kv , or the grad may be nan
moba_kv = moba_kv.flatten(start_dim=0, end_dim=1).unsqueeze(2)
```

此处 `moba_kv` 的段维是 `(块, head)` 段（形状 `[num_head * num_filtered_chunk, chunk_size, 2, D]`），与展平后的 `moba_seqlen_q` 段序一致，所以同一个布尔掩码可以直接用于两侧——`assert` 先自检这个对齐关系。KV 边界的等差构造（[L415-L423](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L415-L423)）用 `num_filtered_chunk * num_head + 1 - zero_expert_count` 作为上限，让 KV 段数与裁剪后的 Q 段数同步减少；最后的形状断言（[L426-L428](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L426-L428)）兜底防止三处裁剪不同步。

#### 4.5.4 代码实践

1. **实践目标**：判断哪些参数组合容易产生零 expert，建立对裁剪分支触发频率的直觉（推理型实践，完整复现实验见第 5 节）。
2. **操作步骤**：
   1. 回顾段数公式：段总数 = `num_filtered_chunk × num_head`，记录总数 = `Σ query × num_head × (moba_topk − 1)`。零 expert 概率随「段多、记录少」上升。
   2. 在测试网格（[tests/test_moba_attn.py:L37-L42](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_attn.py#L37-L42)）中找最危险的组合，重点考虑「短尾块」：batch=7、seqlen=512 会随机切成 7 段，平均每段约 73 个 token——多数段只有 1 个块（不产生候选块），而长度略超 `2 × chunk_size` 的段，其尾块里只有一两个 query。
   3. 推演：chunk=128、topk=2、head=8 时，某个长度为 257 的段有块 `{0, 1, 尾}`，filtered = `{0, 1}`，尾块仅 1 个 query；这个 query 的 8 个 head **各自独立**在 {0, 1} 里选 1 块，落选块的对应 head 段即为空。
3. **需要观察的现象**：纸上推得「每 head 有约 50% 概率落选一块 → 该 batch 大概率产生约 4 个零 expert 段」；结论是测试网格里零 expert 是**常态**而非边角，裁剪分支几乎每次运行都会触发。
4. **预期结果**：写出 2-3 个高危组合（batch 大 + seqlen 小 + chunk 大 + head 多 + topk 小）。这也解释了为什么作者必须显式处理零 expert，而不是指望它不出现。

#### 4.5.5 小练习与答案

**练习 1**：为什么注释只说 `grad may be nan`，前向却没事？

**答案**：零 expert 段在前向不产生任何 q 行（`moba_q` 由掩码 `nonzero` 构建，空段自然没有记录），输出合并又只通过地址簿 `index_add_` 真实记录——空段对前向完全不可见。反向内核则要按段遍历 KV block 计算 dmk/dmv，段内没有合法的 q/lse 行与之配对，读到未初始化的 lse 就会产生 NaN。

**练习 2**：为什么 Q、KV、`moba_cu_seqlen_kv` 三处裁剪必须同步？

**答案**：varlen 两侧边界必须一一配对。只裁 Q 侧：KV 里留着无主段，`moba_cu_seqlen_kv` 段数多于 Q 侧，L426-L428 的形状断言触发；只改 arange 上限而留着 0 段的 `moba_seqlen_q`：两侧段数不匹配同样断言；若绕过断言强行调用，错位的边界会让内核把错误的 Q 段配到错误的 KV 段上，梯度静默出错。

**练习 3**：`deterministic=True`（4.3 节）与本节的 NaN 有什么关系？

**答案**：没有因果关系。NaN 源于零长段破坏 (q 行, lse 行) 对应关系，是确定性的错误；`deterministic` 只影响浮点归约顺序（每次运行结果是否逐位一致）。打开它只是让 NaN/对齐失败可复现，便于调试与测试。

## 5. 综合实践

**任务：构造必然出现零 expert 的输入，实证「注释掉裁剪 → 反向 NaN」，再对照测试容忍度解释结果。**（需 GPU + flash-attn 2.6.3；本节标注的数值结论待本地验证。）

### 5.1 构造必然出现零 expert 的输入

取 batch=1、seqlen=384、chunk_size=128、topk=2、num_head=2。由 u3-l2 的 `calc_chunks`：`cu_chunk = [0, 128, 256, 384]`，三个块中尾块 [256, 384) 被剔除，filtered = {块0, 块1}，`moba_topk = min(2−1, 2) = 1`。逐段推演 gate 合法性（u3-l3 规则：块末 ≤ s < 批末）：

| query 区间 | 块0（末=128） | 块1（末=256） | 记录去向 |
|---|---|---|---|
| [0, 128) | 当前块，掩掉 | 未来块，掩掉 | 无（纯 self 支路） |
| [128, 256) | 合法（唯一候选） | 当前块，掩掉 | 必选块0 |
| [256, 384) | 合法 | 合法 | gate 二选一 |

现在用数据控制最后一行：令 `k[:128] = p`（块0 所有 key 同方向，块均值 = p）、`k[128:256] = 0`（块1 均值 = 0，gate 分数恒 0）、`q[256:] = p`（尾块 query 与 p 对齐）。于是尾块每个 query、每个 head 对块0 的分数 = \( \|p\|^2 \)（D=128 时约百级，fp32 打分）远大于块1 的 0，**严格胜出、无平局** → 全部尾块 query 选块0。结果（展平段序 [(块0,h0),(块0,h1),(块1,h0),(块1,h1)]）：

```
moba_seqlen_q = [256, 256, 0, 0]      # 块1 的两个 head 段全空
zero_expert_count = 2
```

### 5.2 制作「不裁剪」的副本（不改源码树）

把 `moba/moba_efficient.py` 复制为实验目录下的 `moba_efficient_no_cut.py`，做且只做三处修改：

1. [L396-L397](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L396-L397)：注释掉 `if zero_expert_count > 0: moba_seqlen_q = ...`（保留 0 段）。
2. [L409-L413](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L409-L413)：注释掉 `if zero_expert_count > 0: assert ...; moba_kv = moba_kv[...]`（保留零 expert 段的 KV）。
3. [L415-L423](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L415-L423)：`torch.arange(0, num_filtered_chunk * num_head + 1 - zero_expert_count, ...)` 去掉 `- zero_expert_count`（否则 KV 段数比 Q 侧少，L426-L428 断言会先触发，实验就测不到反向了）。

### 5.3 运行脚本

```python
# 示例代码：零 expert 未裁剪 → 反向 NaN 复现（需 GPU；import 路径按你的实验目录调整）
import torch
from moba.moba_efficient import moba_attn_varlen as attn_orig          # 原版
from moba_efficient_no_cut import moba_attn_varlen as attn_nocut      # 5.2 的副本

torch.manual_seed(0)
S, H, D, chunk, topk = 384, 2, 128, 128, 2
dev, dtype = "cuda", torch.bfloat16
p = torch.randn(D, device=dev)

q = torch.randn(S, H, D, device=dev, dtype=dtype, requires_grad=True)
k = torch.randn(S, H, D, device=dev, dtype=dtype, requires_grad=True)
v = torch.randn(S, H, D, device=dev, dtype=dtype, requires_grad=True)
with torch.no_grad():          # 定向布置 gate，迫使尾块 query 全选块0
    k[:128] = p                # 块0：均值 = p，gate 分数 = ||p||^2
    k[128:256] = 0             # 块1：均值 = 0，gate 分数恒 0
    q[256:] = p                # 尾块 query 与 p 对齐 → 必选块0

cu = torch.tensor([0, S], device=dev, dtype=torch.int32)
outs = {}
for name, fn in [("orig", attn_orig), ("nocut", attn_nocut)]:
    o = fn(q, k, v, cu, S, moba_chunk_size=chunk, moba_topk=topk)
    g = torch.autograd.grad(o, (q, k, v), torch.ones_like(o))
    outs[name] = o
    print(name, "| forward NaN:", torch.isnan(o).any().item(),
          "| grad NaN:", any(torch.isnan(t).any().item() for t in g),
          "| k-grad NaN rows [128,256):",
          torch.isnan(g[1][128:256]).any().item())
print("两次前向输出最大差:", (outs["orig"] - outs["nocut"]).abs().max().item())
```

### 5.4 观察与预期

- **原版（orig）**：前向、反向均无 NaN。裁剪分支正常触发（本构造 `zero_expert_count=2`），且其梯度可与 naive 实现对齐（u4-l2 的测试网格本质上就在验证这一点）。
- **副本（nocut）**：前向**大概率**正常（零长段不产生 q 行，`moba_q` 仍是那 512 条真实记录，两次前向输出最大差应为 0）；反向出现 NaN，且 NaN 集中在 `k.grad/v.grad` 中属于块1 的行（token [128, 256)）——正是被空段持有的 KV。若你的 flash-attn 版本连前向都拒绝零长段而直接报错，同样是有价值的观察，记录现象即可。
- **对照结论**：NaN 的出现证明源码注释所言非虚——裁剪是反向正确性的必要防御，而非优化。恢复三处裁剪（用原版）NaN 即消失。
- **接 4.4.4**：把本构造喂给 naive 实现算 `gqkv_ref`，用 4.4.4 的三层容忍度评估原版梯度——这正是 `test_attn_varlen_moba` 在每个参数组合上做的事情（[tests/test_moba_attn.py:L54-L100](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_attn.py#L54-L100)）。

## 6. 本讲小结

- `MixedAttention.backward` = **两次 `_flash_attn_varlen_backward` + 打包返回**：self 支路（`cu_chunk` 边界、`causal=True`）与 moba 支路（重组 varlen、`causal=False`），全部复用 flash-attn 反向内核，无需手写任何注意力梯度。
- 数学核心是「**传合并量**」：合并输出 = 并集 softmax 的输出（u3-l5），所以把合并 LSE 传给内核即得并集权重 \(P\)，把合并输出传给内核（经 delta 技巧 \(\delta_i = \mathrm{dout}_i \cdot \mathrm{out}_i\)）即得并集 delta；每支路覆盖的 (query, key) 对互不重叠，两次调用的梯度相加就是完整梯度——前向的 `factor` 从未在反向出现，它被 \(P\) 的并集分母隐式吸收。
- `save_for_backward` 只存 11 个张量：合并 `output`/`mixed_attn_lse_sh` + 两次内核的全部输入 + 地址簿；两路各自的 out/lse、gate、factor 一概不存（gate 是硬路由，不可导）。
- moba 支路的 `dout/out/lse` 用 `moba_q_sh_indices` 三连 `index_select` 抄成记录级副本（复制而非累加——每条 varlen 记录需要完整 dout）；`dmq/dmkv` 经外层 `index_select`/`rearrange`/`stack` 的自动反向散射回 `q.grad/k.grad/v.grad`，完成两路相加。
- 零 expert 段（0 个 Q 对满块 KV）前向无害、反向致命：内核读不到合法 lse 行，垃圾值变成 NaN 污染 dmk/dmv；防御是在进 Function 前于 Q、KV、`moba_cu_seqlen_kv` 三处同步裁剪，并用形状断言兜底。
- `deterministic=True` 让浮点归约可复现，配合测试的 `allclose(2e-2) + max<4e-2 + mean<4e-4` 三层容忍度，实现「数值噪声可过、实现 bug 必死」的梯度对齐验证。

## 7. 下一步学习建议

至此 u3 单元（moba_efficient 精读）完结：你已完整走过 `calc_chunks` → gate 选块 → varlen 重组 → LSE 合并前向 → 双内核反向的整条链路。后续两条路径：

- **u4-l1 transformers 集成**：看 `moba/wrapper.py` 如何把这套 varlen 接口适配成 HF 注意力后端（`hf_to_fa/fa_to_hf`、GQA 复制、prefill/decode 分支），理解 `MixedAttention` 产出的 `[S, H, D]` 如何被塞回 `[B, H, S, D]`。
- **u4-l2 正确性测试**：本讲 4.4.4 只解读了 `gqkv_diff` 的容忍度；下一讲完整解读参数化网格、`generate_data` 的 varlen 随机切分，并动手新增边界用例（如 chunk_size > seqlen）。若你想先巩固本讲，可把 5.3 的脚本扩展成 pytest 用例（断言「nocut 版梯度含 NaN、orig 版与 naive 对齐」），这正是 u4-l2 的预演。

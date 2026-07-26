# 门控增量规则与循环线性注意力

## 1. 本讲目标

本讲是「注意力内核族」的延伸篇，但讨论的对象**不再是 softmax 注意力**，而是一类**循环线性注意力**——门控增量规则（gated delta rule）。它是 Qwen3-Next 等模型采用的线性注意力机制，把传统注意力的 \(O(T^2)\) 显存与计算降为关于序列长度近乎线性的递推。

学完后你应当能够：

- 说出门控增量规则作为「带遗忘门的矩阵记忆 + delta 写入」的递推形式，以及它为何属于线性注意力。
- 读懂 `recurrent_gated_delta_rule` 的逐步推理内核，理解它的三种变体（standard / persistent / decode_vstream）与自动调优。
- 读懂 `chunk_gated_delta_rule` 的「intra-chunk prepare + inter-chunk recurrence」两段式并行结构，理解它如何把时间维度的串行递推拆成「块内可并行、块间串行」。
- 理解块内单位下三角方程求解的两条路径——`_ct_solve_tril_serial` 与 `_ct_solve_tril_neumann_guarded`——以及为什么需要一个 `norm_inf < 1` 的数值守卫。

## 2. 前置知识

本讲默认你已经学过：

- **cuTile 内核骨架**（u3-l1/u3-l2/u3-l3）：`@ct.kernel`、`ConstInt`、`ct.bid/ct.num_blocks`、`ct.load/store`、`ct.launch`、occupancy 提示。
- **分块矩阵乘**（u5-l1）：瓦片、累加器、`ct.matmul`、tf32 精度链。
- **在线 softmax 与解码 Split-KV**（u6-l1/u6-l2）：注意力为何要分块、归约维切分、LSE 合并。
- **dispatch / register_impl**（u2）：`@dispatch` stub 与 `@register_impl("算子名", backend=...)` 的注册。

几个本讲要用到的基础概念，先用一句话铺垫：

- **线性注意力**：把注意力的「键值对列表」抽象成一张可读写的**记忆矩阵** \(S\)，查询时做 \(o=q^\top S\)，写入时做秩-1 更新。它避免了物化完整 \(T\times T\) 注意力矩阵，代价是更新天然带**时间顺序依赖**。
- **delta 规则**：一种「先读后纠」的记忆写入法——写入前先用键读出当前记忆里已有的值 \(m\)，只把「新值与旧值之差」\(v-m\) 写回。它让矩阵记忆逼近一个可在线学习的关联存储器。
- **单位下三角方程**：形如 \((I+A)x=b\)、其中 \(A\) 严格下三角的线性方程组。因为 \(A\) 是幂零的（\(A^C=0\)），它的逆有有限展开式，本讲的数值稳定性问题全部集中在这里。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/tilegym/ops/ops.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/ops.py) | 两个算子的统一签名 stub：`recurrent_gated_delta_rule`、`chunk_gated_delta_rule`，各带 `@dispatch`。 |
| [src/tilegym/ops/cutile/recurrent_gated_delta_rule.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/recurrent_gated_delta_rule.py) | cuTile 实现：逐步推理内核（含 standard / persistent / decode_vstream 三变体）+ 自动调优。 |
| [src/tilegym/ops/cutile/chunk_gated_delta_rule.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/chunk_gated_delta_rule.py) | cuTile 实现：intra-chunk prepare 内核 + inter-chunk recurrence 内核，以及三角求解的两条路径。 |
| [tests/ops/test_chunk_gated_delta_rule.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/tests/ops/test_chunk_gated_delta_rule.py) | 正确性测试：内含从 HuggingFace transformers v4.57.6 逐字复制的 PyTorch 参考 `_torch_chunk_gated_delta_rule`，以及专门验证三角求解稳定性的 `test_op_correlated_keys_stable_triangular_solve`。 |
| [tests/ops/test_recurrent_gated_delta_rule.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/tests/ops/test_recurrent_gated_delta_rule.py) | 逐步推理内核的正确性测试与参考实现。 |

两个算子的入口与 u6 前几讲完全一致：`ops.py` 里的 stub 自身只 `raise NotImplementedError`，真正计算由 `cutile/__init__.py` 条件导入、经 `@register_impl` 挂到注册表后，由 dispatch wrapper 按当前后端路由（见 [src/tilegym/ops/cutile/__init__.py:42](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/__init__.py#L42) 与 [src/tilegym/ops/cutile/__init__.py:56](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/__init__.py#L56)）。

> 说明：这两个算子**都是前向 only**——`backward` 直接 `raise NotImplementedError`（见 [recurrent...py:636-637](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/recurrent_gated_delta_rule.py#L636-L637) 与 [chunk...py:469-470](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/chunk_gated_delta_rule.py#L469-L470)），测试输入也不带 `requires_grad`。训练反向不在本讲范围内。

---

## 4. 核心概念与源码讲解

### 4.1 门控增量规则：作为线性注意力的递推

#### 4.1.1 概念说明

普通 softmax 注意力把过去所有 token 的 (K, V) 列表完整保留，查询时算 \( \mathrm{softmax}(QK^\top)V \)，显存与计算随序列长度平方增长。**线性注意力**换了个思路：把过去的 (K, V)「压缩」进一张**记忆矩阵** \(S\in\mathbb{R}^{K\times V}\)，查询时只做一次矩阵-向量乘 \(o=q^\top S\)，显存与序列长度无关。

门控增量规则给这张记忆矩阵规定了三件事：

1. **遗忘（gate）**：每来一个新 token，先把旧记忆乘以 \(e^{g_t}\) 衰减。\(g_t\le 0\) 是模型学到的「对数遗忘率」，\(e^{g_t}\in(0,1]\)。
2. **delta 写入（delta rule）**：写入新值前，先用键 \(k_t\) 读出记忆里已有的值 \(m_t=k_t^\top S\)，只把「目标值与旧值之差」\(\delta_t=\beta_t(v_t-m_t)\) 用秩-1 外积写回。\(\beta_t\in(0,1]\) 是「更新门 / 学习率」。
3. **查询（query）**：用 \(q_t\) 读出更新后的记忆得到输出 \(o_t=q_t^\top S_t\)。

之所以叫「门控」是因为同时有遗忘门 \(e^{g_t}\) 和更新门 \(\beta_t\)；之所以叫「增量（delta）」是因为只写差值。它可看作 DeltaNet / gated DeltaNet 一族的具体实例，也正是 Qwen3-Next 的线性注意力层。

#### 4.1.2 核心流程

把上面三步写成逐步递推（每个时间步 \(t=0,\dots,T-1\) 顺序执行），设状态矩阵 \(S_{t}\in\mathbb{R}^{K\times V}\)：

\[
S'_t = e^{g_t}\, S_{t-1} \qquad \text{(遗忘)}
\]

\[
\delta_t = \beta_t\bigl(v_t - k_t^\top S'_t\bigr) \qquad \text{(读出旧值，求差)}
\]

\[
S_t = S'_t + k_t\, \delta_t^\top \qquad \text{(秩-1 写入)}
\]

\[
o_t = q_t^\top S_t \qquad \text{(查询)}
\]

这就是**逐步推理（recurrent）**版的核心循环。它最大的优点是显存常数（只存一张 \(S\)），最大的缺点是**时间维度严格串行**：\(S_t\) 依赖 \(S_{t-1}\)，无法跨 \(t\) 并行——这正是 4.3 节「分块并行」要解决的问题。

通常还会做两件归一化小事：对 \(q,k\) 做 L2 归一化（`use_qk_l2norm_in_kernel`，对齐 FLA 库），以及给 \(q\) 乘缩放 \(\text{scale}=1/\sqrt{K}\)。

#### 4.1.3 源码精读

逐步推理内核把上面四行公式逐字翻译成 cuTile。下面是 `standard` 变体的循环体，注意张量形状：`State` 是 `(BLOCK_K, BLOCK_V)`，`KeyT`/`QueryT` 是 `(BLOCK_K,)`，`ValueT` 是 `(BLOCK_V,)`。

先遗忘再读出（[recurrent...py:101-104](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/recurrent_gated_delta_rule.py#L101-L104)）——这两行就是 \(S'_t=e^{g_t}S_{t-1}\) 与 \(m_t=k_t^\top S'_t\)：

```python
State = State * ct.exp(gate_t)                       # 遗忘门
KeyT = ct.expand_dims(KeyT, axis=1)                  # (K,) -> (K,1)
KvMemT = ct.sum(State * KeyT, axis=0)                # m = k^T S'，对 K 维求和 -> (V,)
```

随后做 delta 写入与查询（[recurrent...py:105-106](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/recurrent_gated_delta_rule.py#L105-L106)）——即 \(\delta_t\)、\(S_t\)、\(o_t\)：

```python
DeltaT = (ValueT - KvMemT) * beta_t                  # delta = beta*(v - m)
State = State + KeyT * ct.expand_dims(DeltaT, axis=0)  # S += k ⊗ delta^T
OutT = ct.sum(State * ct.expand_dims(QueryT, axis=1), axis=0)  # o = q^T S
```

> 这里 `KeyT * expand_dims(DeltaT, axis=0)` 是 `(K,1)*(1,V)` 广播出 `(K,V)` 的外积，正是秩-1 更新 \(k_t\delta_t^\top\)。

`gate_t`、`beta_t` 是标量，用 `ct.gather` 按 `(b, t, hv)` 取出（[recurrent...py:98-99](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/recurrent_gated_delta_rule.py#L98-L99)）；整个循环包在 `for idx_t in range(T)` 里（[recurrent...py:65](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/recurrent_gated_delta_rule.py#L65)）。L2 归一化与 scale 在循环体开头对 `QueryT/KeyT` 处理（[recurrent...py:84-87](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/recurrent_gated_delta_rule.py#L84-L87)）。

#### 4.1.4 代码实践

**目标**：用一个最小脚本验证 cuTile 实现与「手写 PyTorch 递推」数值一致，确认你真的理解了四行公式。

**步骤**：

1. 准备小规模输入（`B=1, T=16, H=4, K=V=64`，bf16，CUDA）。
2. 调用 `tilegym.ops.recurrent_gated_delta_rule`。
3. 用纯 PyTorch 按本节四行公式手写一个递推参考，比较最大绝对误差。

```python
# 示例代码（非项目原有，仅供练习）
import torch, tilegym
from tilegym.ops import recurrent_gated_delta_rule

torch.manual_seed(0)
B, T, H, D = 1, 16, 4, 64
dev = "cuda"; dt = torch.bfloat16
q = torch.randn(B, T, H, D, device=dev, dtype=dt) * 0.1
k = torch.randn(B, T, H, D, device=dev, dtype=dt) * 0.1
v = torch.randn(B, T, H, D, device=dev, dtype=dt) * 0.1
g = -torch.abs(torch.randn(B, T, H, device=dev, dtype=dt)) * 0.5      # <=0 的对数遗忘率
beta = torch.sigmoid(torch.randn(B, T, H, device=dev, dtype=dt))

out_k, _ = recurrent_gated_delta_rule(q, k, v, g, beta, output_final_state=False)

# 手写参考：逐步递推四行公式（fp32）
qf, kf, vf, gf, bf = [x.float() for x in (q, k, v, g, beta)]
scale = 1.0 / (D ** 0.5)
ref = torch.zeros(B, T, H, D)
S = torch.zeros(B, H, D, D)
for t in range(T):
    S = S * torch.exp(gf[:, t])[:, :, None]                # 遗忘
    m = (S * kf[:, t, :, :, None]).sum(dim=2)              # 读出 m = k^T S
    delta = (vf[:, t] - m) * bf[:, t, :, None]             # delta
    S = S + kf[:, t, :, :, None] * delta[:, None, :]       # 秩-1 写入
    ref[:, t] = (S * (qf[:, t] * scale)[:, :, :, None]).sum(dim=2)  # 查询
print("max_abs_err", (ref.float() - out_k.float()).abs().max().item())
```

**需要观察的现象**：`max_abs_err` 应在 bf16 数量级内（约 1e-2 以内）；若差几个数量级，多半是你把「先遗忘再读出」的顺序写反了，或忘了 scale。

**预期结果**：与 `tests/ops/test_recurrent_gated_delta_rule.py` 中的容差一致量级（bf16 atol≈2e-3、rtol≈5e-3 量级）。若无法本地运行 GPU，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果把「遗忘」和「读出」的顺序对调（先读 \(m=k^\top S_{t-1}\)，再用 \(e^{g_t}\) 衰减状态再写入），结果还等价吗？

**答案**：不等价。原公式里 \(\delta_t\) 用的是衰减后的状态 \(S'_t=e^{g_t}S_{t-1}\)，即「新 token 看到的是已衰减的记忆」。对调后 \(\delta_t\) 会基于未衰减的旧状态算出，物理含义不同、数值也不同。顺序必须严格按 4.1.2 的公式。

**练习 2**：为什么 \(g\) 在测试里被构造为**非正**值（`-abs(randn)*0.5`）？

**答案**：\(e^{g_t}\) 是遗忘门，必须 \(\le 1\) 才表示「衰减/记忆有界」。若 \(g_t>0\)，\(e^{g_t}>1\)，状态会被反复放大，长序列下迅速发散到 inf/NaN。测试注释（`test_chunk_gated_delta_rule.py:247-252`）专门说明了未缩放输入会发散。

---

### 4.2 recurrent 逐步推理内核

#### 4.2.1 概念说明

4.1 节解决了「算什么」，本节解决「怎么在 GPU 上跑得快」。逐步推理内核的并行度来自**时间维以外的一切维度**：batch、头（head）、以及 V 维的分块。时间维度 \(T\) 仍然串行（`for idx_t in range(T)`）。

为此内核提供了三种变体，由自动调优择优：

- **standard**：二维 grid `(B*HV, cdiv(V,BLOCK_V))`，每个 CTA 固定负责一个 (batch, 头, V 块)，串行扫完 \(T\) 步。最直接的实现。
- **persistent**：一维 grid `(min(NUM_SMS, 总块数),)`，用 grid-stride 循环让一个 CTA 跨步领取多个 (b, hv, v_block) 任务，复用执行体（与 u3 的静态持久化调度同源）。在 batch/头较少、块数不足以喂饱 SM 时更有利。
- **decode_vstream**：仅在 **\(T=1\)（纯解码）**时启用，把单个 V 块再沿 V 维切成 `STREAM_V_TILE` 小条流式处理，进一步挖掘解码阶段的并行度。

#### 4.2.2 核心流程

主机侧 `_RecurrentGatedDeltaRuleCuTile.forward` 的调度流程：

1. 计算形状与 `BLOCK_K = next_power_of_2(QK)`、`scale = 1/sqrt(QK)`。
2. 若 `is_autotune_disabled()`：按启发式直接挑内核与 `BLOCK_V`（[recurrent...py:533-558](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/recurrent_gated_delta_rule.py#L533-L558)）。
3. 否则查 `autotune_cache`（以形状/dtype/设备为键的类级字典）；未命中则调用 `_autotune` 用 `ct.tune.exhaustive_search` 实测每个候选内核×配置，取最快者（[recurrent...py:487-503](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/recurrent_gated_delta_rule.py#L487-L503)）。
4. 用最优配置算 grid、组装参数元组、`ct.launch`（[recurrent...py:599-631](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/recurrent_gated_delta_rule.py#L599-L631)）。

候选配置由 `_autotune_configs` 产出：它先按「目标 V 块数 ≈ 2×NUM_SMS」反推一个工作感知的 `BLOCK_V`（batch 越小、`BLOCK_V` 越小以产生更多 V 块），再枚举 `occupancy ∈ {2,3,4,6}` 与两组 TMA 开关（[recurrent...py:344-361](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/recurrent_gated_delta_rule.py#L344-L361)）。

#### 4.2.3 源码精读

三种变体的**循环体几乎逐字相同**（都是 4.1.3 的四行公式），差别只在「如何把任务映射到 CTA」。standard 用 `ct.bid(0)/(1)` 直接取二维坐标（[recurrent...py:44-51](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/recurrent_gated_delta_rule.py#L44-L51)）；persistent 用一维 `idx_CGA` 加 grid-stride 循环把一维块号拆回 `(b, hv, v_block)`：

```python
# persistent 变体：grid-stride 跨步领取任务（[recurrent...py:145-160]）
for idx_block in range(idx_CGA, NUM_BLOCKS, num_CGAs):
    idx_bhv = idx_block // NUM_V_BLOCKS
    idx_v   = idx_block %  NUM_V_BLOCKS
    ...
```

`decode_vstream` 则把 `for idx_t in range(T)` 退化为单步（\(T=1\)），并在 V 维再加一层 `for idx_stream_v` 流式循环（[recurrent...py:298-333](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/recurrent_gated_delta_rule.py#L298-L333)）；它还把「读出基址」`OutBaseT = q^T S'` 与「写入修正」分开，利用 \(o_t = q^\top S' + (q^\top k)\delta\) 的分解省掉一次状态-查询乘。

#### 4.2.4 代码实践

**目标**：体验自动调优对 recurrent 内核的影响，并观察 persistent 变体的触发条件。

**步骤**：

1. 用小 batch（`B=1`）、较大 \(T\)（如 64）跑两次，分别 `TILEGYM_DISABLE_AUTOTUNE=1` 与默认。
2. 打印 `_RecurrentGatedDeltaRuleCuTile.autotune_cache` 的键，确认调优按形状缓存。
3. 把 batch 调大到 `B=16`，观察 `BLOCK_V` 候选的变化（batch 大 → 目标 V 块数少 → `BLOCK_V` 更大）。

**需要观察的现象**：禁用调优时走固定启发式（`BLOCK_V=64`，`persistent` 分支由 `persistent` 入参决定）；启用调优时缓存键包含 `(B,T,H,HV,QK,V,dtype,...,persistent,str(device))`（[recurrent...py:560-573](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/recurrent_gated_delta_rule.py#L560-L573)）。

**预期结果**：调优只发生一次（首次形状），后续同形状命中缓存直发；persistent 与 standard 由 `exhaustive_search` 实测耗时二选一。若无 GPU，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `decode_vstream` 只在 \(T=1\) 时才加入候选（[recurrent...py:478-485](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/recurrent_gated_delta_rule.py#L478-L485)）？

**答案**：它的循环结构假设只有一步时间（`idx_t=0`），并把省下的并行度投到沿 V 维的流式切分。当 \(T>1\) 时，外层时间循环无法省略，流式切分的收益不再成立，所以只在解码（\(T=1\)）场景启用。

**练习 2**：`autotune_cache` 为什么做成**类级**字典而不是局部变量？

**答案**：调优要实测每个候选内核的耗时，代价高。做成类级字典让同一进程内「相同形状+dtype+设备」的调用复用上次的 `(best_kernel, best_config)`，避免每次前向都重新搜索——这正是 u5-l3 讲过的 tune-once/cache/launch 模式。

---

### 4.3 chunk 分块并行：intra-chunk prepare + inter-chunk recurrence

#### 4.3.1 概念说明

recurrent 版本的致命伤是「时间维度完全串行」，长 prefill（如 \(T=10782\)，见测试 `qwen3_5_T10782`）会慢到不可用。**分块（chunked）**算法把序列切成大小为 `CHUNK_SIZE`（默认 64）的若干块，改写成两段：

- **intra-chunk（块内）**：在单个块内部，递推可以展开成一个**矩阵方程**——输出是该块输入与一张「块内交互矩阵」的乘积，而这张矩阵的构造只依赖本块数据，**块与块之间完全独立**，可在 grid 上充分并行。
- **inter-chunk（块间）**：块与块之间只剩**状态矩阵 \(S\) 的递推**（一块传一块），这部分天然串行，但每块只做一次廉价的矩阵乘，开销小。

也就是说：把「\(T\) 步串行」换成「块内全并行 + 块数步串行」。当 `CHUNK_SIZE=64`、\(T=2048\) 时，串行步从 2048 降到 32。

#### 4.3.2 核心流程

设块大小 \(C\)，块内矩阵 \(Q,K,V\in\mathbb{R}^{C\times d}\)、\(\beta\in\mathbb{R}^{C}\)，以及门控的块内累计和 \(G_i=\sum_{j\le i} g_j\)（`g_cum = cumsum(g_raw)`，[chunk...py:135](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/chunk_gated_delta_rule.py#L135)）。令 \(K_\beta=K\odot\beta\)、\(V_\beta=V\odot\beta\)（按行乘）。定义严格下三角的「块内交互矩阵」：

\[
A_{ij} = -\mathbf{1}_{i>j}\,(K_\beta K^\top)_{ij}\, e^{G_i - G_j}
\]

（对应代码里 `attn = where(strict_lower, -(base_attn * decay_mask), 0)`，[chunk...py:147-149](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/chunk_gated_delta_rule.py#L147-L149)。）

intra-chunk prepare 算出两张「已纠正」的块内矩阵（\(A\) 严格下三角故幂零，\((I+A)^{-1}\) 存在且有限展开）：

\[
V_{\text{corr}} = (I+A)^{-1}\, V_\beta
\]

\[
K_{\text{cumdecay}} = (I+A)^{-1}\,\bigl(K_\beta \odot e^{G}\bigr)
\]

（对应 [chunk...py:169](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/chunk_gated_delta_rule.py#L169) 的 `vc_out = attn @ vb_tile` 与 [chunk...py:180](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/chunk_gated_delta_rule.py#L180) 的 `kc_out = attn @ kbe_tile`。这两步的 `(I+A)^{-1}` 求解就是 4.4 节的主角。）

inter-chunk recurrence 维护状态矩阵 \(S_c\in\mathbb{R}^{d\times d}\)，对每个块 \(c\) 串行执行（对应 `_chunk_inter_recurrence_kernel` 的 `for ci in range(num_chunks)`，[chunk...py:261](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/chunk_gated_delta_rule.py#L261)）：

\[
v' = K_{\text{cumdecay},c}\, S_{c-1}, \qquad v_{\text{new}} = V_{\text{corr},c} - v'
\]

\[
o_c = \underbrace{(Q_c\odot e^{G})\, S_{c-1}}_{\text{inter 项}} + \underbrace{\bigl((Q_c K_c^\top)\odot D\bigr)\, v_{\text{new}}}_{\text{intra 项}}
\]

\[
S_c = e^{G_{\text{last}}}\, S_{c-1} + \bigl(K_c\odot e^{G_{\text{last}}-G}\bigr)^{\top}\, v_{\text{new}}
\]

其中 \(D_{ij}=\mathbf{1}_{i\ge j}e^{G_i-G_j}\) 是下三角衰减掩码（[chunk...py:318-321](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/chunk_gated_delta_rule.py#L318-L321)），\(G_{\text{last}}\) 是该块最后一步的累计门控（[chunk...py:333-340](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/chunk_gated_delta_rule.py#L333-L340)）。

两个内核的 grid 设计直接体现「块内并行、块间串行」：

- intra：`grid = (B*H, num_chunks, 1)`，**每个 CTA 一个 (b, h, chunk)**，块间完全并行（[chunk...py:396](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/chunk_gated_delta_rule.py#L396)）。
- inter：`grid = (B*H, cdiv(V, BLOCK_V), 1)`，每个 CTA 一个 (b, h, V 块)，**沿 V 维并行，但块数 `num_chunks` 在 CTA 内串行循环**（[chunk...py:438](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/chunk_gated_delta_rule.py#L438)）。

#### 4.3.3 源码精读

主机侧 `_ChunkGatedDeltaRuleCuTile.forward` 串起两次启动（[chunk...py:352-466](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/chunk_gated_delta_rule.py#L352-L466)）。两个值得关注的工程细节：

**① 5D 中间缓冲与 bf16 降流量**。intra 与 inter 之间要交换四张 `(B,H,num_chunks,CHUNK_SIZE,K/V)` 大缓冲（`q_chunked/k_chunked/v_corrected/k_cumdecay`）。intra 内核是访存瓶颈（低 occupancy 下尤甚），所以当输入是 bf16/fp16 时，把这些中间量存成 bf16 把流量砍半；而 `g_cum` 因为在 inter 里要做 `exp(g_last - g_cum)`，精度敏感，保持 fp32（[chunk...py:381-394](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/chunk_gated_delta_rule.py#L381-L394)）。fp32 输入则全程 fp32、不丢精度。内核里对应 `INTER_BF16` 分支选择 `ct.astype(..., ct.bfloat16)` 写回（[chunk...py:170-177](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/chunk_gated_delta_rule.py#L170-L177)）。

**② `solve_steps` 的计算**。`solve_steps = max(1, (chunk_size - 1).bit_length())` 即 \(\lceil\log_2 C\rceil\)，作为 Neumann 展开的因子数传给内核（[chunk...py:379](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/chunk_gated_delta_rule.py#L379)）。这一行的来由与 4.4 节直接相关。

输出最后做 `reshape + transpose + [:T]` 把按块存储的输出还原成 `(B, T, H, V)` 并截断到真实序列长度（[chunk...py:464-465](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/chunk_gated_delta_rule.py#L464-L465)）——注意序列长度的 pad/截断由内核的 `padding_mode=ZERO` 与这里的切片共同处理，测试里特意放了 `T100_unaligned`、`T37_prime`、`T65_off_by_1` 等非整块用例。

#### 4.3.4 代码实践

**目标**：观察 intra/inter 两个 grid 的并行结构，确认「块内并行、块间串行」。

**步骤**：

1. 在 `_ChunkGatedDeltaRuleCuTile.forward` 里临时给 `grid_intra` 与 `grid_inter` 各加一行 `print`（或用 `torch.cuda.synchronize()` 前后计时）。
2. 分别用 `T=64`（恰好 1 块）、`T=128`（2 块）、`T=2048`（32 块）跑 `chunk_gated_delta_rule`。
3. 记录两个 grid 的形状与耗时。

**需要观察的现象**：

- `grid_intra[1] == num_chunks` 随 \(T\) 线性增长（块越多，intra 并行度越高）。
- `grid_inter` 的第二维只随 \(V\) 变化，与 \(T\) 无关；inter 内核耗时随 `num_chunks` 近似线性增长（块间串行）。

**预期结果**：\(T\) 翻倍时，intra 阶段因并行几乎不增加墙钟时间（块数变多但 SM 仍有空闲），inter 阶段近似翻倍。若无法本地运行，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 inter 内核的 grid 第二维是 `cdiv(V, BLOCK_V)` 而不是 `num_chunks`？

**答案**：块间有真实的数据依赖（状态 \(S_c\) 依赖 \(S_{c-1}\)），无法跨块并行，只能在一个 CTA 内串行循环。可并行的是状态矩阵的 V 维（不同 V 列之间互不依赖），所以把 V 切块映射到 grid 第二维，块数留给 CTA 内循环。

**练习 2**：`K_cumdecay` 这个名字里的「cumdecay」指什么？

**答案**：它把「块内累计门控衰减」\(e^G\) 折进了 \((I+A)^{-1}(K_\beta\odot e^G)\) 这一步，是 inter 阶段用来把**上一块的状态** \(S_{c-1}\) 投影到本块坐标系（算 \(v'=K_{\text{cumdecay}}S_{c-1}\)）的「迁移矩阵」。预先在 intra 阶段算好，避免 inter 阶段重复算衰减。

---

### 4.4 三角求解的数值稳定性：serial vs Neumann guarded

> 这是本讲的重头戏，也是本轮（HEAD `7410bd8`）做过重大数值稳定性重构的部分。两次关键提交：`043ef63` 引入 Neumann-by-squaring 与 `occupancy=2`；`6a718d3` 加入 `norm_inf<1` 守卫与 serial 回退。

#### 4.4.1 概念说明

intra 阶段要算 \((I+A)^{-1}\)，其中 \(A\) 严格下三角。因为 \(A\) 是**幂零**的（\(A^C=0\)，任意高于 \(C-1\) 次的幂都是零），它的逆有有限级数展开：

\[
(I+A)^{-1} = I - A + A^2 - A^3 + \cdots + (-A)^{C-1}
\]

把同一个目标拆成两种算法：

- **serial（参考顺序求解）**：按行号 \(i=1,\dots,C-1\) 顺序，逐行用「上面已经求好的行」修正当前行。逻辑步骤数 \(O(C)\)，每步只做小运算。它与 PyTorch 参考（`test_chunk_gated_delta_rule.py:100-103`）的操作顺序逐字一致，因而**精确、稳定**，但串行性强。

- **Neumann-by-squaring（平方累乘）**：把级数重新组织成 \(\lceil\log_2 C\rceil\) 个因子的乘积：

\[
(I+A)^{-1} \approx (I+A)(I+A^2)(I+A^4)\cdots(I+A^{2^{s-1}})
\]

每步做一次矩阵乘（`_ct_mm`，走张量核心），深度只有 \(O(\log C)\)，**可并行、速度快**。但每一步乘积都可能引入**抵消误差**。

#### 4.4.2 核心流程

问题来了：当键 \(k\) 之间**强相关**（例如模型对重复 token 做 L2 归一化后几乎共线）时，\(K_\beta K^\top\) 的元素会很大，\(A\) 的元素也很大。Neumann 的中间项 \(A^{2^j}\) 在被级数「抵消成零」之前会**先暴涨**——即使全程 fp32，抵消项的中间幅度也能到 \(10^{15}\) 量级。两个大数相减得到一个小结果，有效数字几乎全丢。这是典型的**catastrophic cancellation**。

> 注意：仅把 MMA 精度提到 fp32 并不能解决——因为抵消发生在「乘积本身的数值幅度」上，不是单个乘法的精度问题。代码注释里写得很直白：`changing MMA precision alone is insufficient`（[chunk...py:47-51](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/chunk_gated_delta_rule.py#L47-L51)）。

`_ct_solve_tril_neumann_guarded` 的解法是用**矩阵无穷范数的次可乘性**提前判定是否安全。无穷范数 \(\|A\|_\infty=\max_i\sum_j|A_{ij}|\)（每行绝对值之和的最大值）满足：

\[
\|A^{2^j}\|_\infty \le \|A\|_\infty{}^{2^j}
\]

所以只要 \(\|A\|_\infty < 1\)，那么**每一个平方幂** \(\|A^{2^j}\|_\infty < 1\) 且随 \(j\) 指数衰减到 0——Neumann 乘积不会暴涨，抵消误差被限制在合理范围。反之若 \(\|A\|_\infty \ge 1\)，就**回退到 serial** 求解，保稳保准。这个判据是**先验**的：在昂贵的求解之前用一次 `ct.sum/ct.max` 就能算出，而不是「求解后才发觉不稳」。

#### 4.4.3 源码精读

两条求解函数都是「普通 helper（无 `@ct.kernel`）」，被内核内联调用。

**serial 路径**（[chunk...py:27-41](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/chunk_gated_delta_rule.py#L27-L41)）——逐行修正，最后加单位阵：

```python
def _ct_solve_tril_serial(A, CS):
    offs = ct.arange(CS, dtype=ct.int32)
    for i in range(1, CS):
        is_row = offs == i
        is_row_col = ct.expand_dims(is_row, axis=1)
        row = ct.sum(ct.where(is_row_col, A, 0.0), axis=0)
        correction = ct.sum(ct.expand_dims(row, axis=1) * A, axis=0)
        A = A + ct.where(is_row_col, ct.expand_dims(correction, axis=0), 0.0)
    eye = ct.where(ct.expand_dims(offs, axis=1) == ct.expand_dims(offs, axis=0), 1.0, 0.0)
    return A + eye
```

它和 PyTorch 参考（[test...py:100-104](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/tests/ops/test_chunk_gated_delta_rule.py#L100-L104)）逐行对应：`row = attn[i,:i]`、`correction = (row ⊗ sub).sum`、`attn[i,:i] += correction`，最后 `attn += eye`。

**guarded 路径**（[chunk...py:44-68](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/chunk_gated_delta_rule.py#L44-L68)）——先算 `norm_inf`，按 `<1` 分流：

```python
norm_inf = ct.max(ct.sum(ct.abs(A), axis=1))   # ||A||_inf
result = A
if norm_inf < 1.0:                              # 安全：所有平方幂必 < 1
    result = eye + A
    A_power = A
    for _ in range(1, n_steps):                 # n_steps = ceil(log2 CS)
        A_power = _ct_mm(A_power, A_power)      # A^(2^j)
        result = _ct_mm(result, eye + A_power)  # 累乘 (I + A^(2^j))
else:
    result = _ct_solve_tril_serial(A, CS)       # 不安全：回退参考顺序求解
return result
```

**内核里的分流**（[chunk...py:150-155](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/chunk_gated_delta_rule.py#L150-L155)）——`USE_QK_L2NORM` 决定走哪条：

```python
if USE_QK_L2NORM:
    # 模型归一化的键可能近乎共线：强制走精确的 serial 求解，
    # 避免 prefill 的微小差异在自回归解码里被放大。
    attn = _ct_solve_tril_serial(attn, CHUNK_SIZE)
else:
    attn = _ct_solve_tril_neumann_guarded(attn, CHUNK_SIZE, SOLVE_STEPS)
```

为什么 `USE_QK_L2NORM=True` 时直接无条件 serial？因为 L2 归一化后的键极易共线（重复/近重复 token），属于「天然不安全」的情形，而且自回归生成对 prefill 阶段的微小数值差异非常敏感——serial 与参考逐字一致，能保证 prefill→decode 的确定性。`USE_QK_L2NORM=False`（预先在模型侧归一化好）时则用 guarded Neumann，由 `norm_inf` 守卫兜住异常键。

**`occupancy=2` 的设计意图**（[chunk...py:71](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/chunk_gated_delta_rule.py#L71)）：intra 内核同时持有 \(C\times C\) 的交互矩阵、\(C\times K\) / \(C\times V\) 的多个瓦片，并要做若干次 `C×C` 与 `C×d` 的矩阵乘，是**瓦片/寄存器重、且访存瓶颈**的内核。`occupancy=2` 是给编译器的「每 SM 驻留 2 个 CTA」提示，反映该内核真实可达的并发度——再高就会因寄存器/共享内存压力导致 spill，反而变慢；而该内核又受限于向四张 5D 缓冲写出的访存流量（这正是 4.3.3 用 bf16 降流量的原因），2 个驻留 CTA 已足以掩盖该流量。这个提示由 `043ef63` 与 Neumann 同批引入，是 intra 阶段性能调优的落点。

#### 4.4.4 代码实践（本讲主实践任务）

**目标**：亲手验证 `norm_inf<1` 守卫的必要性，并对比两条求解路径。

**步骤**：

1. 打开 [tests/ops/test_chunk_gated_delta_rule.py:279-336](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/tests/ops/test_chunk_gated_delta_rule.py#L279-L336) 的 `test_op_correlated_keys_stable_triangular_solve`。该测试刻意构造**强相关键**：`key = key_base + 0.02*randn`（同一基向量加微扰），并在 `use_l2=False` 分支用 `F.normalize` 把键预先归一化（于是键近乎共线、\(\|A\|_\infty\) 容易 \(\ge 1\)）。
2. 运行该测试：
   ```bash
   TILEGYM_DISABLE_AUTOTUNE=1 pytest tests/ops/test_chunk_gated_delta_rule.py \
     -k correlated_keys -v
   ```
3. 用 `git stash` 临时把 [chunk...py:150-155](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/chunk_gated_delta_rule.py#L150-L155) 改回旧的「无条件 Neumann」（即两分支都调 `_ct_solve_tril_neumann_guarded` 且去掉 `norm_inf` 守卫），重跑测试，观察 `test_out` 是否出现非有限值或大误差。

**需要观察的现象与说明要点**：

- **守卫为何有效**：强相关键使 \(K_\beta K^\top\) 的非对角元很大，\(A\) 的行绝对值之和 \(\|A\|_\infty\) 可能 \(\ge 1\)。此时 Neumann 中间幂 \(A^{2^j}\) 的幅度被次可乘性放大约 \(\|A\|_\infty^{2^j}\)，迅速到 \(10^{15}\) 量级；最终用级数抵消回小值时，fp32 有效数字所剩无几。`norm_inf<1` 守卫用一次廉价的 `sum+max` 在求解前就发现这种「会暴涨」的情形，转走 serial，避免在昂贵的求解之后才发现不稳。
- **为什么 fp32 也不够**：抵消误差来自「乘积本身的数值幅度」，不是单次乘法的舍入；所以单纯把 MMA 升到 fp32（`_ct_mm` 已经在用 tf32→fp32）救不了，必须从「限制幂的增长」入手——这正是 `norm_inf` 守卫做的事。
- **`occupancy=2` 意图**：见 4.4.3 末尾——intra 内核瓦片重、访存瓶颈，`occupancy=2` 是其真实可达并发度，过高反而 spill。

**预期结果**：当前代码下测试通过（输出有限、`atol=2e-3, rtol=5e-3`）；若回退到无守卫的 Neumann，相关键用例会出现 `nan/inf` 或超出容差。⚠️ 步骤 3 会临时改动源码，做完务必 `git stash pop` / `git checkout` 还原；若你不想改源码，可跳过步骤 3，仅阅读旧实现：`git show 043ef63:src/tilegym/ops/cutile/chunk_gated_delta_rule.py` 的 `_ct_solve_tril_neumann`。若无法本地运行 GPU，标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`norm_inf` 用「行绝对值之和的最大值」而非「矩阵元素最大值」作为判据，为什么更合适？

**答案**：矩阵乘的行和范数 \(\|A\|_\infty\) 满足次可乘性 \(\|AB\|_\infty\le\|A\|_\infty\|B\|_\infty\)，因此 \(\|A\|_\infty<1\) 能严格推出 \(\|A^{2^j}\|_\infty\le\|A\|_\infty^{2^j}<1\)。逐元素最大值没有这个性质（\(\max|A^2|\) 可能比 \((\max|A|)^2\) 还大），无法给出幂的增长界。

**练习 2**：为什么 `USE_QK_L2NORM=True` 时不再判 `norm_inf`、直接走 serial？

**答案**：L2 归一化的键极易近乎共线，是「几乎一定不安全」的情形，逐块判 `norm_inf` 既多花开销又会在边界块上产生「这块走 Neumann、那块走 serial」的数值跳变。自回归生成对 prefill 的微小差异敏感，统一走与参考逐字一致的 serial 能保证 prefill→decode 的确定性与稳定。

**练习 3**：`solve_steps = ceil(log2(chunk_size))`（[chunk...py:379](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/chunk_gated_delta_rule.py#L379)）。如果故意把它取得比真实值大，结果还对吗？

**答案**：对。因为 \(A\) 幂零，超出 \(C-1\) 次的幂恒为零，对应的因子 \((I+A^{2^j})=I\) 是恒等，多乘不改变结果（只会多花几次矩阵乘）。这正是注释里「over-provisioning is safe」的含义——所以用 `bit_length()` 上取整是安全的。

---

## 5. 综合实践

把本讲四块知识串起来：**用 recurrent 版本验证 chunk 版本的正确性**。

1. 选一组中等规模输入（`B=1, T=128, H=4, K=V=64`，bf16），令 `CHUNK_SIZE=64`（恰好 2 块）。
2. 分别调用 `tilegym.ops.recurrent_gated_delta_rule` 与 `tilegym.ops.chunk_gated_delta_rule`（同一份输入、同一 `initial_state`、`output_final_state=True`）。
3. 比较两者的 `output` 与 `final_state` 的最大绝对误差。
4. 把 `CHUNK_SIZE` 改成 32（4 块）、16（8 块），观察误差与 `chunk_gated_delta_rule` 的 intra grid 第二维（`num_chunks`）变化。
5. 打开 `USE_QK_L2NORM=True` 重做一遍，解释此时三角求解走 serial 对结果稳定性的意义。

**预期结论**：两种实现数值等价（容差内），说明「块内并行 + 块间串行」是对「全序列串行递推」的正确改写；`CHUNK_SIZE` 越小，intra 并行块越多，但 inter 串行步也越多；`USE_QK_L2NORM=True` 下走 serial，强相关键也稳定。若误差超容差，先检查 `g` 是否构造为非正值、`initial_state` 是否一致传入。若无 GPU，标注「待本地验证」。

## 6. 本讲小结

- 门控增量规则是一种**循环线性注意力**：用「遗忘门 \(e^{g_t}\) + delta 秩-1 写入」维护一张矩阵记忆 \(S\)，把注意力的 \(O(T^2)\) 降为关于序列长度近似线性的递推。
- **recurrent 版**逐步执行 \(S'_t=e^{g_t}S_{t-1}\to\delta_t=\beta_t(v_t-k_t^\top S'_t)\to S_t=S'_t+k_t\delta_t^\top\to o_t=q_t^\top S_t\)；时间维度串行，靠 batch/头/V 分块并行，有 standard/persistent/decode_vstream 三变体 + 自动调优。
- **chunk 版**把序列切成块：intra-chunk prepare 算出 \(V_{\text{corr}}=(I+A)^{-1}V_\beta\) 与 \(K_{\text{cumdecay}}\)（grid 按块并行），inter-chunk recurrence 只串行递推状态矩阵（grid 按 V 维并行）。串行步从 \(T\) 降到块数。
- intra 阶段的 \((I+A)^{-1}\) 是数值稳定性核心：\(A\) 严格下三角故幂零，可用 serial（行序求解，与参考逐字一致）或 Neumann-by-squaring（\(O(\log C)\) 深度、走张量核心）。
- 强相关键会让 Neumann 中间幂暴涨到 \(10^{15}\) 量级、产生抵消误差；`_ct_solve_tril_neumann_guarded` 用次可乘的 \(\|A\|_\infty<1\) 守卫先验判定，不安全则回退 serial；`USE_QK_L2NORM=True` 直接走 serial。
- 两个算子都是**前向 only**；中间缓冲在低精度输入时存 bf16 降访存流量、`g_cum` 保持 fp32；intra 内核 `occupancy=2` 反映其瓦片重、访存瓶颈下的真实可达并发度。

## 7. 下一步学习建议

- **回到应用层**：这两个算子是 Qwen3-Next 的线性注意力层。阅读 [src/tilegym/transformers/monkey_patch.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/transformers/monkey_patch.py)（u8-l1），看它们如何被 patch 进 HuggingFace 的 `qwen3_next` 建模代码。
- **对比 softmax 注意力的并行策略**：本讲的「块内并行 + 块间串行递推」与 u6-l2 的 Split-KV「归约维切分 + LSE 合并」是两种不同的并行化套路，建议画一张表对比两者的「串行维度」与「并行维度」。
- **深入三角求解的数学**：可阅读 DeltaNet / gated DeltaNet 原论文与 HuggingFace `modeling_qwen3_next.py` 中 `torch_chunk_gated_delta_rule` 的注释，理解「chunk decay / cumulative gate」的代数推导；本讲的 PyTorch 参考就是从那里逐字复制的。
- **自动调优**：如果对 recurrent 内核的三变体选择与 `BLOCK_V` 候选设计感兴趣，复习 u5-l3 的 `exhaustive_search` / `replace_hints` / tune-once 三件套，再回看 [recurrent...py:411-503](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/recurrent_gated_delta_rule.py#L411-L503) 的 `_autotune`。

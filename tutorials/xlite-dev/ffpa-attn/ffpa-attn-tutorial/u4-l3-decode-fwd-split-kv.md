# Decode 前向：split-KV 两阶段

## 1. 本讲目标

上一讲（u4-l1）我们读完了 Triton 前向的**通用路径**（generic path）：一个 program 拥有一个 Q 行块，把整条 KV 序列从头流到尾，边流边做 online softmax，直接写出最终的 O 与 LSE。那条路径有一个隐含前提——**Q 行块足够多，能把 GPU 的 SM 填满**。

本讲要回答一个新问题：**当 Q 的行数极少（典型是 Nq=1，也就是推理解码 step）时，通用路径会发生什么？为什么要换成 split-KV 两阶段路径？**

读完本讲，你应当能够：

1. 说清「decode 形状并行度不足、SM 跑不满」这个动机，以及 FFPA 用 `_get_decode_num_splits` 这个 FlashAttention 风格启发式来决定「要不要切、切几段」。
2. 读懂 stage1 kernel `_ffpa_decode_fwd_stage1_kernel`：它按 KV chunk 算「部分输出 + chunk 局部 LSE」，并在 Nq=1 时走 **GEMV 向量归约**子路径、Nq>1 时走 **多行 MMA** 子路径。
3. 读懂 stage2 kernel `_ffpa_decode_fwd_stage2_kernel`：用 log-sum-exp 公式在**对数域**把各 chunk 的部分结果合并成最终 O 与全局 LSE，并能用数学验证它等价于整体 softmax。
4. 读懂启动器 `_ffpa_attn_forward_decode_impl` 如何分配两块 fp32 scratch、串起两个 kernel。

## 2. 前置知识

本讲默认你已掌握 u4-l1（generic 前向、online softmax、`m_i/l_i/alpha` 重缩放）和 u4-l2（Split-D、`NUM_V_GROUPS`、`o_accs` 累加器）。这里补两个本讲要用到的新概念。

**SM 占用度（occupancy）与「跑不满」。** GPU 上的 kernel 被切成大量 threadblock，由调度器派发到各个 SM（Streaming Multiprocessor）。只有当 block 数量不少于 SM 数量、且每个 SM 能塞下足够 block 时，设备才算「被喂饱」。FFPA generic 前向的网格是 `(cdiv(Nq, BLOCK_M), batch*nheads_q)`——网格规模正比于 Q 行块数 × (batch×头数)。当 Nq 很小（比如 1）时，Q 行块数塌缩成 1，整张网格只剩 `batch*nheads_q` 个 block。一张 A100 有 108 个 SM，若 `batch*nheads_q` 远小于 108，大量 SM 空转，吞吐被严重浪费。

**log-sum-exp（LSE）合并。** 把一条长 KV 序列切成若干 chunk 分别算，每个 chunk 产出一个「局部已归一化的输出」和一个「局部的 logsumexp」。要在数值稳定的前提下把它们拼回整体 softmax 的结果，不能直接相加，而要用对数域的加权合并。这正是 stage2 干的事，公式见 4.3。

承接 u4-l1/u4-l2 已建立的术语：head_dim(D)、online softmax、`m_i/l_i`、Split-D、`NUM_V_GROUPS`、`o_accs`、LSE（自然对数约定 `lse = log(sum(exp(score)))`）、GQA 的 `off_hkv = off_hq // group_size`。本讲新增：split-KV、chunk、num_splits、占用度启发式、GEMV 路径、partial_out/chunk_lse scratch。

## 3. 本讲源码地图

本讲全部源码集中在一个文件：

| 文件 | 作用 |
| --- | --- |
| [src/ffpa_attn/triton/_ffpa_fwd.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py) | FFPA Triton 前向全部实现：generic kernel、decode stage1/stage2 kernel、两个启动器、num_splits 启发式 |

该文件中本讲涉及的函数清单：

| 函数 | 行号区间 | 角色 |
| --- | --- | --- |
| `_decode_num_splits_heuristic` | 214–259 | 纯 Python 的 FlashAttention split-KV 占用度启发式 |
| `_get_decode_num_splits` | 262–284 | 根据 (Nq, Nkv, D, batch, nheads, 设备) 算出 `num_splits` |
| `_ffpa_decode_fwd_stage1_kernel` | 492–753 | stage1：每个 program 算一个 KV chunk 的部分 O 与 chunk LSE（含 GEMV / MMA 两条子路径） |
| `_ffpa_decode_fwd_stage2_kernel` | 756–825 | stage2：按 LSE 在对数域合并各 chunk，写出最终 O 与全局 LSE |
| `_ffpa_attn_forward_decode_impl` | 1028–1283 | 启动器：分配 scratch、选 config、依次启动 stage1 与 stage2 |
| `_ffpa_attn_forward_impl` | 1319–1444 | 上层入口：先算 num_splits，据此在 generic / sm90-tma / decode 三条路径间分流 |

注意分流的总开关在 `_ffpa_attn_forward_impl` 里：先调用 `_get_decode_num_splits` 算出 `num_splits`，只有当 `num_splits != 1` 时才进入本讲的 decode 路径；`num_splits == 1` 走 u4-l1 的 generic 路径。所以「是否 split」是运行时按形状动态决定的。

## 4. 核心概念与源码讲解

### 4.1 何时 split：num_splits 占用度启发式

#### 4.1.1 概念说明

decode 的核心矛盾：**Q 行太少 → 网格太小 → SM 跑不满**。解决思路是「**沿 KV 维度再切一刀**」，把同一条 KV 序列拆成 `num_splits` 个 chunk，每个 chunk 由一个独立的 program 处理。这样网格规模从 `batch*nheads_q*Q块数` 膨胀到 `num_splits*batch*nheads_q*Q块数`，用「KV 上的并行」补回「Q 上的并行不足」，把 SM 重新喂饱。

但切是有代价的：切得越多，stage2 要合并的部分结果越多，额外显存与归约开销越大；而且若 `num_splits` 让每块 KV 不是整数个 tile，会产生空转。所以需要一个**启发式**来挑选「在占用度与开销之间取得平衡」的切分数。FFPA 直接复刻了 FlashAttention 的 split-KV 启发式。

什么时候**不**切？当 Q 行块已经够多、能填满 80% 的 SM 时，就不切（`num_splits=1`），回到 generic 路径。这也是为什么 `_ffpa_attn_forward_impl` 把 `num_splits==1` 当成「走 generic」的判据。

#### 4.1.2 核心流程

`_get_decode_num_splits` 的决策过程（伪代码）：

```text
num_sms = 物理 SM 数 × 2              # “有效 SM 预算”，见 4.1.3
block_n = 256 if D<=64 elif 128 if D<=128 else 64
num_n_blocks = cdiv(Nkv, block_n)     # KV 方向的 tile 数
num_m_blocks = cdiv(Nq, 64)           # Q 方向的行块数
batch_nheads_mblocks = batch * nheads_q * num_m_blocks   # 不切时的网格规模

num_splits = _decode_num_splits_heuristic(
    batch_nheads_mblocks, num_sms, num_n_blocks, max_splits=128)
```

而 `_decode_num_splits_heuristic` 的逻辑分三步：

1. **快速不切**：若 `batch_nheads_mblocks >= 0.8 * num_sms`，直接返回 1——并行度已足够，不必切。
2. **枚举切分候选**：在 `1..max_splits` 中遍历 `num_splits`，跳过「无效切分」（切与不切导致每个分片 tile 数相同的那种，即多切一段没有带来新 tile 边界）。对每个有效候选计算**占用效率** `efficiency = n_waves / ceil(n_waves)`，其中 `n_waves = (网格规模*num_splits) / num_sms`。
3. **就近选最小达标切分**：在所有效率 ≥「最佳效率的 85%」的候选里，选**最小的** `num_splits`——够用就行，不盲目多切。

效率公式 `n_waves / ceil(n_waves)` 衡量「最后一波 block 是否把 SM 填满」：当 `n_waves` 接近整数时效率最高（≈1），此时设备被整数波次填满；远离整数时尾巴那一波会半空转，效率下降。

#### 4.1.3 源码精读

先看「有效 SM 预算」与网格规模的换算 [_ffpa_fwd.py:262-284](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L262-L284)：这里 `multi_processor_count * 2` 把物理 SM 数翻倍当作「有效 SM 预算」，含义是「每个 SM 上希望平均驻留约 2 个 block」；`block_n` 按 head_dim 反向取值（D 越大，KV tile 越小，因为单 tile 寄存器/SRAM 压力更大），随后把 `batch*nheads_q*num_m_blocks` 作为「不切时的网格规模」传入启发式。

启发式主体 [_ffpa_fwd.py:214-259](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L214-L259)：

- 第 228–229 行是「快速不切」短路：`if batch_nheads_mblocks >= 0.8 * num_sms: return 1`。
- 第 231 行 `max_splits = max(1, min(max_splits, num_sms, num_n_blocks))` 把切分上限夹在「SM 数」和「KV tile 数」之间——切得比 KV tile 还多没意义。
- 第 238–242 行 `_is_split_eligible` 用 `cdiv(num_n_blocks, s) != cdiv(num_n_blocks, s-1)` 判定「多切一段是否真的多产生一个 tile 边界」，避免无意义的切分进入候选。
- 第 248–251 行计算每个候选的 `efficiency = n_waves / ceil(n_waves)`，并记录全局最大值 `max_efficiency`。
- 第 253–257 行选出「效率 ≥ 85% 最大效率」的**最小**候选返回；找不到就回退 1。

一句话总结：**「够填满 SM 的最小切分数」**。

#### 4.1.4 代码实践

**实践目标**：在不必启动 GPU kernel 的前提下，观察不同形状下启发式选出的 `num_splits`，建立「Q 越少、切得越多」的直觉。

**操作步骤**（需要可 `import torch` 的环境；若无可把 `_decode_num_splits_heuristic` 当成纯函数单独摘出来算）：

```python
# 示例代码（仅演示调用方式，实际 num_splits 依赖设备 SM 数，需在 GPU 上运行）
from ffpa_attn.triton._ffpa_fwd import _get_decode_num_splits
import torch

dev = torch.device("cuda")
for Nq, Nkv, D, B, H in [(1, 8192, 512, 1, 4), (512, 8192, 512, 1, 4), (1, 8192, 512, 1, 32)]:
    n = _get_decode_num_splits(Nq, Nkv, D, B, H, dev)
    print(f"Nq={Nq:4d} Nkv={Nkv} D={D} B={B} H={H:2d} -> num_splits={n}")
```

**需要观察的现象**：

- `Nq=1, H=4`：网格规模极小，应返回较大的 `num_splits`（>1），说明会走 decode 路径。
- `Nq=512, H=4`：Q 行块已足够多，大概率返回 1，走 generic 路径。
- `Nq=1, H=32`（增大 batch×头数）：并行度回升，`num_splits` 应比第一个用例更小。

**预期结果**：`num_splits` 随 `batch*nheads_q*num_m_blocks` 增大而单调不增，并在足够大时塌缩到 1。具体数值随 GPU 型号（SM 数）而变，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `max_splits` 要被 `num_n_blocks`（KV tile 数）夹住？
**答**：`num_splits` 超过 KV 的 tile 数后，必然有 chunk 分不到任何完整 tile，再多切只会产生空 chunk、徒增 stage2 的归约开销，不会带来新的并行。

**练习 2**：`efficiency = n_waves / ceil(n_waves)` 中，`n_waves` 取什么值时效率最高？
**答**：当 `n_waves` 恰为整数时 `ceil(n_waves)=n_waves`，效率 = 1 最高；此时 block 恰好分成整数波次填满所有 SM，没有半空的尾巴波。

---

### 4.2 stage1 kernel：每个 KV chunk 算部分 O 与 chunk LSE

#### 4.2.1 概念说明

stage1 是「**分而治之的局部计算**」：把整条 KV 切成 `num_splits` 段，每段由一组 program 独立处理，各自在**本 chunk 内**做完 online softmax 与 PV 累加，写出两样东西：

- **partial_out**：本 chunk 内已用**本 chunk 局部 softmax** 归一化过的部分输出 \(O_c\)。
- **chunk_lse**：本 chunk 内 score 的 logsumexp，即 \(\text{LSE}_c=\log\sum_{n\in\text{chunk}_c}\exp(s_n)\)（自然对数）。

注意 stage1 写出的 \(O_c\) **不是**最终答案——它只在该 chunk 内部归一化，跨 chunk 的合并留给 stage2。这跟 generic 路径「一个 program 流完整条 KV、直接写最终 O」截然不同。

stage1 内部还有一条**关键分叉**：`USE_GEMV`。

- 当 `Nq==1`（纯解码步），Q 的「行块」其实就是一根向量。用 `tl.dot`（MMA 矩阵 tile）去算 `[BLOCK_M=8, BLOCK_N]` 的 score 矩阵时，8 行里有 7 行是 padding 浪费掉的，还要为此分配 `[BLOCK_M, BLOCK_HEADDIM_V]` 的累加器，极不划算。于是 GEMV 路径改用**逐元素乘 + `tl.sum` 归约**来算 score（向量点积），累加器也从二维矩阵降为一维向量。
- 当 `Nq>1`（但仍小到需要 split），走标准的多行 MMA 路径，结构上与 generic kernel 几乎一致，只是 KV 只在 chunk 范围内流转。

#### 4.2.2 核心流程

stage1 是**三维网格**：

```text
grid = ( cdiv(Nkv, CHUNK_SIZE),   # dim0 = chunk 数
         batch * nheads_q,        # dim1 = batch×Q 头
         cdiv(Nq, BLOCK_M) )      # dim2 = Q 行块数

program_id(0) -> chunk_idx   本 program 负责第几个 KV chunk
program_id(1) -> off_hb      batch×Q 头的扁平索引（再拆 off_b / off_hq）
program_id(2) -> q_block     Q 行块索引
```

每个 program 的处理流程：

```text
chunk_start = chunk_idx * CHUNK_SIZE
chunk_end   = min(Nkv, chunk_start + CHUNK_SIZE)

# GQA 头映射（同 generic）
off_hkv = off_hq // group_size

if USE_GEMV:                       # Nq==1
    累加器从 [BLOCK_M, D] 降为一维向量
    对 chunk 内每个 BLOCK_N:
        score = 向量点积（elementwise mul + reduce），按 D 分片累加
        online softmax 更新 m_i_single / l_i_single
        for v_group: o_acc += alpha*o_acc + sum(v * p)   # 向量累加
    写 partial_out（无 offs_m 偏移，仅一根向量）+ chunk_lse（标量）
else:                              # Nq>1，多行 MMA
    与 generic kernel 同构，只是 KV 循环限定在 [chunk_start, chunk_end)
    写 partial_out[offs_m] + chunk_lse[offs_m]
```

注意「写出的 O 已除以 chunk 局部 \(l_i\)」、而「写出的 LSE 是 \(m_i+\log(l_i)\)」——这两者配对，正是 stage2 合并所需的 \((O_c, \text{LSE}_c)\)。

#### 4.2.3 源码精读

stage1 kernel 的 program_id 映射 [_ffpa_fwd.py:554-556](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L554-L556)：`chunk_idx=program_id(0)`、`off_hb=program_id(1)`、`q_block=program_id(2)`，与上面网格维度一一对应。第 557–562 行把 `off_hb` 拆成 `off_b/off_hq` 并做 GQA 头映射，约定与 generic 路径完全一致。

chunk 边界 [_ffpa_fwd.py:572-573](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L572-L573)：`chunk_start = chunk_idx*CHUNK_SIZE`、`chunk_end = minimum(Nkv, chunk_start+CHUNK_SIZE)`，把本 program 的 KV 活动范围钳在一个 chunk 内（最后一个 chunk 可能不满）。

**GEMV 子路径**（Nq==1）[_ffpa_fwd.py:582-665](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L582-L665)：

- 第 585–588 行：状态 `m_i_single`/`l_i_single` 是**标量**，累加器 `o_accs_single` 是**一维** `BLOCK_HEADDIM_V` 向量（不是 generic 的 `[BLOCK_M, BLOCK_HEADDIM_V]` 矩阵）。
- 第 595–609 行：score 用 `tl.sum(k * q[None, :], axis=1)` 按向量点积算，按 D 分片累加（注意这里仍保留 Split-D 的 D 分片循环，只是退化为向量相加）。
- 第 644–652 行：PV 也用向量归约 `o_acc = o_accs_single[v_group]*alpha + tl.sum(v*p[:,None], axis=0)`，没有 `tl.dot`。
- 第 657–665 行：写 `PartialOut + o_d`（无 `offs_m` 偏移，因为只有一根 query 向量）和标量 `ChunkLSE`。

为什么 GEMV 敢「不切 D」？因为累加器只是一根 `BLOCK_HEADDIM_V` 向量，占用的寄存器远少于 `[BLOCK_M, D]` 矩阵，所以启动器在 GEMV 时取 `block_headdim = next_power_of_2(D)`（见 4.4.3），让 `NUM_V_GROUPS=1`、一次性吃下整个 D——这正是 u4-l2「压力从 SRAM 转到寄存器」的延续，但单行向量的寄存器压力小到可以不分 D。

**多行 MMA 子路径**（Nq>1）[_ffpa_fwd.py:667-753](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L667-L753)：结构与 generic `_ffpa_fwd_kernel_impl` 几乎相同——Phase1 按 D 分片累加 score（第 680–693 行）、Phase2 online softmax（第 713–717 行）、Phase3 按 V-group 累加 PV（第 732–740 行），只是 KV 循环范围限定在 `[0, CHUNK_SIZE)`（第 674 行）且 `offs_kv` 加上了 `chunk_start` 偏移（第 676 行）。第 745–753 行写出本 chunk 的部分 O（已除以局部 \(l_i\)）与 chunk LSE（\(m_i+\log(l_i)\)）。

一个跨路径的 dropout 细节：dropout 的 RNG 偏移用的是**全局 KV 位置**（`offs_kv` 已含 `chunk_start`），所以切 chunk 不会改变 dropout 掩码，保证与 SDPA 的逐元素 RNG 约定逐位一致（见第 632–636 行 GEMV 路径与 718–729 行 MMA 路径的注释）。

#### 4.2.4 代码实践

**实践目标**：把 stage1 的三维网格与 program_id 映射画清楚，并验证「chunk 数」与「`num_splits`」的关系。

**操作步骤**：

1. 在 [_ffpa_attn_forward_decode_impl](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L1110-L1115) 中定位 `stage1_grid`，确认三个维度依次是 `(cdiv(Nkv, CHUNK_SIZE), batch*nheads_q, cdiv(Nq, BLOCK_M))`。
2. 在 [_ffpa_decode_fwd_stage1_kernel](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L554-L556) 顶部确认 `chunk_idx/off_hb/q_block` 的来源，画出「一个 program ↔ (一个 KV chunk, 一个 batch×Q头, 一个 Q 行块)」的对应图。
3. 手算：`Nq=1, Nkv=8192, num_splits=16` 时 `CHUNK_SIZE=cdiv(8192,16)=512`，stage1 网格 dim0 = `cdiv(8192,512)=16`，正好等于 `num_splits`。

**需要观察的现象**：网格 dim0 等于（或几乎等于）`num_splits`，dim2 在 Nq=1 时塌缩为 1（只有 1 个 Q 行块）。

**预期结果**：手算 dim0 == `num_splits`（当 Nkv 被 CHUNK_SIZE 整除时严格相等；否则可能差 1，但被 `chunk_end` 钳位保护）。**待本地验证**非整除情形。

#### 4.2.5 小练习与答案

**练习 1**：stage1 写出的 `partial_out` 是「最终答案」吗？为什么还要 stage2？
**答**：不是。`partial_out` 只在本 chunk 内用局部 softmax 归一化（除以 chunk 局部 \(l_i\)），跨 chunk 的归一化还没做。必须由 stage2 按 `chunk_lse` 把各 chunk 的贡献重新加权合并，才等价于对整条 KV 的整体 softmax。

**练习 2**：为什么 Nq=1 时 GEMV 路径比 MMA 路径更快？
**答**：Nq=1 时 MMA 的 `[BLOCK_M=8, BLOCK_N]` tile 有 7 行是浪费的 padding，且需要 `[BLOCK_M, D]` 的二维累加器。GEMV 改用向量点积与一维累加器，既消除了 padding 浪费，又把寄存器占用从矩阵降到向量，单 query 行不需要为矩阵 tile 付费。

**练习 3**：stage1 里 causal 掩码（Nq>1 路径）用的是 `offs_kv <= offs_m + kv_offset`，这里的 `offs_kv` 是 chunk 局部下标还是全局 KV 下标？这为什么重要？
**答**：是**全局** KV 下标（已加 `chunk_start`）。这一点至关重要——因果掩码「query 行 r 只看 key 列 ≤ r+(Nkv−Nq)」是相对**整条 KV**定义的，必须用全局位置；同时 dropout RNG 也依赖全局位置来匹配 SDPA 约定。

---

### 4.3 stage2 kernel：log-sum-exp 对数域合并

#### 4.3.1 概念说明

stage1 给了 `n_chunks` 份 \((O_c, \text{LSE}_c)\)，每份只覆盖一段 KV。stage2 的任务是**把它们合并成对整条 KV 的整体 softmax 结果**。

为什么不能直接对 \(O_c\) 做加权平均？因为各 chunk 的局部 softmax 分母不同。正确做法是用 log-sum-exp 在对数域合并：把每个 chunk 的 \(\text{LSE}_c\) 当成「该 chunk 整体重要性的对数权重」，做一次**数值稳定**的加权求和。这与 FlashAttention 里 split-KV 的合并公式完全一致，也是 online softmax 跨块合并的「块版」。

#### 4.3.2 核心流程

stage2 是**二维网格**：

```text
grid = ( batch * nheads_q * Nq,   # dim0 = 所有 query 行（扁平）
         num_v_groups )           # dim1 = V 分组数

program_id(0) -> off_hbm   再拆 off_b / off_hq / off_m（单根 query 行）
program_id(1) -> v_group   本 program 负责输出的第几个 V 切片
```

每个 program 对「一行 query、一个 V 切片」做合并：

```text
读所有 chunk 的 chunk_lse[0..n_chunks)
m      = max_c(LSE_c)                         # 数值稳定用
w_c    = exp(LSE_c - m)                        # 对数权重（减去 max 防溢出）
denom  = sum_c w_c
读所有 chunk 的 partial_out[..., v_group]
out    = sum_c (w_c * O_c) / denom             # 加权合并 + 重新归一化
写 O[off_b, off_hq, off_m, v_group 切片]
若 v_group==0：写 LSE = m + log(denom)          # 全局 LSE 只写一次
```

#### 4.3.3 数学推导

设整条 KV 的 score 为 \(s_n\)，chunk \(c\) 覆盖下标集合 \(\mathcal{C}_c\)。stage1 在 chunk 内算的是（自然对数）：

\[
\text{LSE}_c=\log\sum_{n\in\mathcal{C}_c}\exp(s_n),\qquad
O_c=\frac{\sum_{n\in\mathcal{C}_c}\exp(s_n)\,V_n}{\sum_{n\in\mathcal{C}_c}\exp(s_n)}
\]

stage2 取 \(m=\max_c\text{LSE}_c\)、\(w_c=\exp(\text{LSE}_c-m)\)，输出

\[
O=\frac{\sum_c w_c\,O_c}{\sum_c w_c},\qquad \text{LSE}=m+\log\sum_c w_c
\]

验证它确实等于整体 softmax：由 \(\exp(\text{LSE}_c)=\sum_{n\in\mathcal{C}_c}\exp(s_n)\)，分子可化为

\[
\sum_c w_c O_c=\frac{1}{\exp(m)}\sum_c\exp(\text{LSE}_c)\cdot\frac{\sum_{n\in\mathcal{C}_c}\exp(s_n)V_n}{\exp(\text{LSE}_c)}=\frac{1}{\exp(m)}\sum_n\exp(s_n)V_n
\]

分母 \(\sum_c w_c=\frac{1}{\exp(m)}\sum_n\exp(s_n)\)，两者相除即得

\[
O=\frac{\sum_n\exp(s_n)V_n}{\sum_n\exp(s_n)}=\text{softmax}(s)\,V
\]

而 \(\text{LSE}=m+\log\sum_c w_c=\log\sum_n\exp(s_n)\)，正是整条 KV 的全局 logsumexp。证毕。

#### 4.3.4 源码精读

program 映射 [_ffpa_fwd.py:788-791](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L788-L791)：`off_hbm=program_id(0)` 是「batch×头×query 行」的扁平索引，再拆出 `off_b/off_hq/off_m`；`v_group=program_id(1)` 选 V 切片。

合并主体 [_ffpa_fwd.py:800-818](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L800-L818)：

- 第 802–806 行：一次性把所有 chunk 的 `chunk_lse` 读进一个 `BLOCK_CHUNKS` 宽的向量，越界 chunk 填 `-inf`。
- 第 807 行 `valid_c = mask_c & (chunk_lse > -inf)`：把「不存在的 chunk」与「全 -inf 的空 chunk」（如全掩码的 chunk）都判为无效，权重置 0，避免它们污染合并（特别是 dropout 开启时 stage1 会把 `chunk_lse` 预填 `-inf`，见启动器第 1108 行）。
- 第 808–810 行：`max_lse`、`weights=exp(LSE_c-max_lse)`、`denom=sum(weights)`，正是上面公式里的 \(m, w_c, \sum w_c\)。
- 第 812–818 行：读各 chunk 的 `partial_out` 切片，做 `out = sum(weights[:,None]*partial, axis=0)/denom`，等价于 \(\sum_c w_c O_c/\sum_c w_c\)。

写回 [_ffpa_fwd.py:819-825](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L819-L825)：输出 `out` 写入最终 `O`，并在 `v_group==0` 时**只写一次**全局 `LSE = max_lse + log(denom)`——因为 LSE 与 V 切片无关，多个 v_group program 里只有第一个负责落地。

关于网格规模里 `num_v_groups` 的取值（见启动器 4.4.3）：`stage2_block_headdim_v = next_power_of_2(D) if D<=512 else 128`。D=512 时 `num_v_groups=1`（整个 D 一次合并完）；D>512（如 640/1024）时改用 128、`num_v_groups>1`，stage2 也会沿 D 再切——这仍是 Split-D 思想在合并阶段的体现。

#### 4.3.5 代码实践

**实践目标**：用 Python 手写 stage2 的合并公式，对一个最小例子验证「分块合并 == 整体 softmax」。

**操作步骤**（纯 CPU/numpy 即可）：

```python
# 示例代码：手工模拟 stage2 合并，验证数学等价性
import numpy as np
np.random.seed(0)
Nkv, D = 16, 4
s = np.random.randn(Nkv).astype(np.float32)   # 整条 score
V = np.random.randn(Nkv, D).astype(np.float32)

# 参考答案：整体 softmax
ref = np.exp(s)[:, None] / np.exp(s).sum() * V
ref = ref.sum(axis=0)

# 模拟 stage1：切成 4 个 chunk，每个 chunk 算 (O_c, LSE_c)
chunks = np.array_split(np.arange(Nkv), 4)
Oc, LSEc = [], []
for idx in chunks:
    sc = s[idx]
    LSEc.append(np.log(np.exp(sc).sum()))
    Oc.append((np.exp(sc)[:, None] * V[idx]).sum(axis=0) / np.exp(sc).sum())

# 模拟 stage2：log-sum-exp 合并
LSEc = np.array(LSEc)
m = LSEc.max()
w = np.exp(LSEc - m)
merged = sum(w[c] * Oc[c] for c in range(len(chunks))) / w.sum()

print("max_abs_err:", np.abs(merged - ref).max())
```

**预期结果**：`max_abs_err` 在 float32 量级（约 1e-6），证明分块合并与整体 softmax 数值等价。本例可独立运行验证。

#### 4.3.6 小练习与答案

**练习 1**：合并时为什么要先减去 `max_lse` 再取 exp？
**答**：数值稳定性。直接算 `exp(LSE_c)` 在 \(s_n\) 较大时会溢出；减去各 chunk LSE 的最大值后，最大的指数项变成 \(\exp(0)=1\)，其余 \(<1\)，既防上溢也保精度（这正是 online softmax 跨块合并的标准技巧）。

**练习 2**：为什么全局 LSE 只在 `v_group==0` 时写入？
**答**：LSE 是 query 行级别的标量，与 V 切片无关。stage2 沿 `v_group` 维度起了多个 program，它们算出的 LSE 完全相同，只需任选一个 program 写一次即可；用 `v_group==0` 这个确定条件避免重复写、也避免数据竞争。

---

### 4.4 启动器 `_ffpa_attn_forward_decode_impl`：编排两阶段

#### 4.4.1 概念说明

启动器是「**调度中枢**」：它不参与数值计算，只负责把 `_ffpa_attn_forward_decode_impl` 的入参翻译成两个 kernel 的启动参数。具体职责有四件：

1. 决定 `num_splits`（若调用方未显式给，就用 4.1 的启发式）。
2. 分配两块 fp32 scratch：`partial_out` 与 `chunk_lse`。
3. 在 **autotune / 持久化配置 / 默认 config** 三条路径里选 stage1 的 launch config，并启动 stage1。
4. 算出 stage2 的网格与 `BLOCK_HEADDIM_V`，启动 stage2 合并。

#### 4.4.2 核心流程

```text
batch, nheads_q, Nq, D = q.shape
use_gemv = (Nq == 1)
n_chunks = num_splits
chunk_size = cdiv(Nkv, n_chunks)
block_m        = 8 if use_gemv else min(64, max(8, next_pow2(Nq)))
block_headdim  = next_pow2(D) if use_gemv else 64     # GEMV 不切 D，MMA 切到 64

# 分配 scratch
partial_out = empty[batch, nheads_q, n_chunks, Nq, D]        # fp32
chunk_lse   = empty[batch, nheads_q, n_chunks, Nq]           # fp32
if autotune: chunk_lse.fill_(-inf)   # 给空 chunk 占位，配合 stage2 的 valid_c 判断

# stage1：grid = (cdiv(Nkv, CHUNK_SIZE), batch*nheads_q, cdiv(Nq, BLOCK_M))
启动 stage1（autotune / 持久化 / 默认 三选一）

# stage2：grid = (batch*nheads_q*Nq, cdiv(D, stage2_BLOCK_HEADDIM_V))
启动 stage2，合并写入最终 o 与 lse
```

#### 4.4.3 源码精读

`use_gemv` 与块尺寸选择 [_ffpa_fwd.py:1077-1095](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L1077-L1095)：`use_gemv = seqlen_q == 1`；GEMV 时 `block_headdim = next_pow2(D)`（不切 D），MMA 时 `block_headdim = 64`（Split-D）；`block_m` 在 GEMV 时为 8、MMA 时按 Nq 取下一个 2 的幂并夹在 [8,64]。

scratch 分配 [_ffpa_fwd.py:1097-1108](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L1097-L1108)：`partial_out` 形状 `[batch, nheads_q, n_chunks, Nq, D]`、`chunk_lse` 形状 `[batch, nheads_q, n_chunks, Nq]`，都是 fp32。第 1108 行 `if autotune: chunk_lse.fill_(-inf)` 是一个微妙细节——开启 autotune 时 Triton 会先用不同 config 试跑，某些 config 可能不写所有 chunk 的 LSE，预填 `-inf` 让 stage2 的 `valid_c` 判断把这些位置当空 chunk 丢弃，保证合并正确。

stage1 三条 config 路径 [_ffpa_fwd.py:1117-1245](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L1117-L1245)：`autotune=True` 时用 `_get_decode_fwd_stage1_autotune`（按 `(headdim, use_gemv, mode, dtype)` 缓存 autotune 包装器，见第 833–854 行）；否则查 `lookup_persistent_config(kernel="decode_fwd_stage1")`（持久化自动调优配置，u8-l3 会详讲）；都没有就退回默认 config（第 1189–1197 行）。无论哪条路，最后都把 `CHUNK_SIZE=chunk_size` 强制覆盖进 launch config（第 1198 行）——因为 `CHUNK_SIZE` 由 `num_splits` 决定，是运行期参数，不参与 autotune（这也是 `_gen_decode_fwd_stage1_autotune_configs` docstring 强调的「`CHUNK_SIZE` owned by launcher」）。

stage2 网格与启动 [_ffpa_fwd.py:1247-1283](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L1247-L1283)：`stage2_block_headdim_v` 在 D≤512 时取整 D（单组合并），否则取 128（再切 D）；`block_chunks = next_pow2(n_chunks)`；网格 `(batch*nheads_q*Nq, num_v_groups)`；`num_warps=4`。stage2 不 autotune，因为它只是一次轻量的归约合并。

#### 4.4.4 代码实践

**实践目标**：直接调用 `_ffpa_attn_forward_decode_impl`，绕过上层分发，验证 decode 两阶段输出的正确性（与 SDPA 对齐）。

**操作步骤**（需要 GPU；仿照仓库 `tests/test_ffpa_fwd.py:1023` 的官方用法）：

```python
# 示例代码：仿照 tests/test_ffpa_fwd.py 直接调用 decode 启动器
import math, torch
import torch.nn.functional as F
from ffpa_attn.triton._ffpa_fwd import _ffpa_attn_forward_decode_impl

B, H, Nq, Nkv, D = 1, 4, 1, 8192, 512
dtype = torch.bfloat16
q = torch.randn(B, H, Nq, D, dtype=dtype, device="cuda")
k = torch.randn(B, H, Nkv, D, dtype=dtype, device="cuda")
v = torch.randn(B, H, Nkv, D, dtype=dtype, device="cuda")
o = torch.empty_like(q)
lse = torch.empty(B, H, Nq, device="cuda", dtype=torch.float32)

_ffpa_attn_forward_decode_impl(
    q, k, v, o, lse,
    causal=False, softmax_scale=1.0 / math.sqrt(D), autotune=False,
)

ref = F.scaled_dot_product_attention(q, k, v, scale=1.0 / math.sqrt(D))
print("out.shape:", tuple(o.shape), "max_abs_err:", (o.float() - ref.float()).abs().max().item())
```

**需要观察的现象**：

- `o.shape == (1, 4, 1, 512)`，与 SDPA 输出一致；`lse` 形状 `(1, 4, 1)`。
- `max_abs_err` 在 bf16 容差内（与仓库测试 `test_ffpa_attn_func_triton_decode_matches_sdpa` 同量级）。

**预期结果**：FFPA decode 输出与 SDPA 数值对齐，从而间接证明 stage1+stage2 的合并数学正确。该调用会真正进入 decode 路径（Nq=1<D 且 Nkv=8192≥512、D=512∈(256,1024]，不触发 SDPA 回退；且 `num_splits>1`）。具体误差数值**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `CHUNK_SIZE` 要由启动器在运行期决定，而不是放进 autotune 搜索空间？
**答**：`CHUNK_SIZE = cdiv(Nkv, num_splits)`，而 `num_splits` 由占用度启发式按 `(Nq, Nkv, D, batch, nheads, 设备)` 动态算出。若把 `CHUNK_SIZE` 也纳入 autotune，会与启发式的切分决策相互干扰，且 autotune 的缓存 key 会爆炸。启动器统一管切分、autotune 只管 tile 内的 `BLOCK_M/BLOCK_N/num_warps` 等参数，职责更清晰。

**练习 2**：开启 `autotune=True` 时为何要 `chunk_lse.fill_(-inf)`？
**答**：autotune 会用多个候选 config 试跑 stage1，某些 config 可能因越界掩码不写某些 chunk 的 LSE。预填 `-inf` 后，stage2 的 `valid_c = mask_c & (chunk_lse > -inf)` 会把这些未写的位置判为「空 chunk」、权重置 0，避免读到未初始化的垃圾值污染合并结果。

## 5. 综合实践

把本讲的知识串起来，完成下面这个「**decode 路径全链追踪**」任务。

给定形状 `B=1, H=4, Nq=1, Nkv=8192, D=512, bf16`，请：

1. **判定路径**：用 `_get_decode_num_splits` 算出 `num_splits`，确认它 >1，因此 `_ffpa_attn_forward_impl` 会走 decode 而非 generic（回顾 [_ffpa_attn_forward_impl:1384-1444](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L1384-L1444) 的分流逻辑）。
2. **算出形状**：手算 `chunk_size`、`partial_out` 与 `chunk_lse` 的形状、stage1 网格三维、stage2 网格二维、stage1 走 GEMV 还是 MMA、`NUM_V_GROUPS` 与 stage2 的 `num_v_groups`。
3. **画两张图**：一张是 stage1「一个 program（chunk_idx, off_hb, q_block）的数据流」（Q 向量、KV chunk、score、online softmax、PV 累加、写 partial_out/chunk_lse）；一张是 stage2「一个 program（off_hbm, v_group）的合并流」（读各 chunk LSE→算权重→加权合并 partial_out→写 O/LSE）。
4. **验证正确性**：用 4.4.4 的脚本直接调用 `_ffpa_attn_forward_decode_impl`，把输出与 `F.scaled_dot_product_attention` 对比，确认误差在容差内；再用 4.3.5 的 numpy 小例子独立验证 stage2 合并的数学等价性。
5. **解释一句**：为什么 Nq=1 时 stage1 的 `NUM_V_GROUPS` 可以等于 1（不切 D），而 Nq>1 的 MMA 路径却要把 D 切到 64？

参考答案要点：

1. `num_splits` 由启发式给出（>1），decode 路径生效。
2. `chunk_size=cdiv(8192, num_splits)`；`partial_out=[1,4,num_splits,1,512]`、`chunk_lse=[1,4,num_splits,1]`；stage1 grid `(num_splits, 4, 1)`；Nq=1→GEMV；`block_headdim=512`→`NUM_V_GROUPS=1`；stage2 `stage2_block_headdim_v=512`→`num_v_groups=1`，grid `(1*4*1, 1)=(4,1)`。
5. Nq=1 的累加器是一维 `BLOCK_HEADDIM_V` 向量，寄存器压力小，可一次吃下整个 D；Nq>1 的 MMA 累加器是 `[BLOCK_M, D]` 矩阵，必须切 D 才能把寄存器压力控住——这正是 u4-l2「Split-D 把 D 压力从 SRAM 转到寄存器」在 decode 两个子路径上的不同体现。

## 6. 本讲小结

- decode 形状（Nq 极小、Nkv 长）会让 generic 路径的网格塌缩、SM 跑不满；FFPA 用 FlashAttention 风格的 `_get_decode_num_splits` 启发式，当并行度不足 80% SM 时沿 KV 切成 `num_splits` 段，用「KV 上的并行」补「Q 上的并行」。
- 前向变成**两阶段**：stage1 每个 program 算一个 KV chunk 的局部 softmax 归一化输出 `partial_out` 与 chunk 级 logsumexp `chunk_lse`；stage2 用 log-sum-exp 公式把各 chunk 在对数域合并成最终 O 与全局 LSE。
- stage1 有两条子路径：`Nq==1` 走 **GEMV 向量归约**（一维累加器、不切 D，省掉单行 query 的矩阵 tile 浪费）；`Nq>1` 走**多行 MMA**（结构同 generic，但 KV 限定在 chunk 内、且保留 Split-D 切 D）。
- 数学上 stage2 的合并 \(O=\sum_c w_c O_c/\sum_c w_c\)（\(w_c=\exp(\text{LSE}_c-m)\)）严格等价于对整条 KV 的整体 \(\text{softmax}(s)V\)，并与 online softmax 的跨块合并同源。
- 启动器 `_ffpa_attn_forward_decode_impl` 负责：算 `num_splits`、分两块 fp32 scratch、按 autotune/持久化/默认三路选 stage1 config（`CHUNK_SIZE` 由启动器在运行期注入）、再启动 stage2 归约合并；是否进入本路径由上层 `_ffpa_attn_forward_impl` 用 `num_splits==1` 与否来判定。

## 7. 下一步学习建议

- **进入反向**：本讲只讲了 decode **前向**。decode 的反向（`Nq<8`）同样需要 split——因为 dQ 要跨 K 块归约。建议下一讲读 u5-l3「Decode 反向与 dQ 跨块归约」，对照 `_ffpa_bwd_decode_stage1_kernel` / `_ffpa_bwd_decode_dq_reduce_kernel`，你会发现它与本讲的 stage1/stage2 是镜像结构。
- **打通 generic 全貌**：若还没读 u4-l4「前向特性：GQA / attn_bias / causal / dropout 实现」，建议补上，本讲的 attn_bias 广播、dropout 全局 RNG 偏移正是那一讲的延伸应用。
- **调优与配置查找**：stage1 的「持久化配置」路径（`lookup_persistent_config(kernel="decode_fwd_stage1")`）如何从磁盘命中 config、就近匹配 head_dim/seqlen，留到 u8-l3「运行时配置查找与就近匹配回退」详讲；autotune 的 fast/max 候选生成则在 u8-l1。

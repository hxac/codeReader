# Mamba / SSD 状态空间家族

## 1. 本讲目标

本讲把前面学到的「Op(L2)/Kernel(L1) 双层分离」「多 kernel 协作」「manifest func 模式 roofline」三条线索，汇聚到一个真实而复杂的算子家族——Mamba-2 的 **State-Space Dual（SSD，状态空间对偶）**。学完本讲你应能：

1. 说清 SSD 把一个原本「串行递归」的 SSM 计算，分解为哪几个可并行的阶段，以及为什么要这样分。
2. 读懂 `Mamba2FwdOp` 这条端到端流水线，能画出 `DaCumsum → CBProducer → SSDChunkState → SSDStatePassing → SSDChunkScan` 五个子算子的数据流。
3. 区分**预填充（chunk-wise）路径**与**逐 token 解码（decode）路径**，知道它们各自调用哪个 kernel。
4. 理解「状态传递（state passing）」kernel 的作用，以及为什么 Mamba-2 的每个 `bias` / `initial_states` 变体都在 manifest 里各占一条 `variant_of` 条目、且各自绑定一个 func 模式 roofline。

> 承接关系：本讲依赖 [u2-l4 跟读 GemmOp 完整链路](u2-l4-trace-gemmop-end-to-end.md)（多 kernel Op 的 forward 编排）与 [u7-l2 roofline 字段与两种模式](u7-l2-roofline-field-modes.md)（func 模式 roofline 的字节记账）。如果你还没读过这两篇，建议先看。

## 2. 前置知识

### 2.1 什么是状态空间模型（SSM）

一个**离散 SSM** 用一个递归式描述序列的演化（以 Mamba-2 的标量记法）：

\[ s_t = \bar{A}\, s_{t-1} + \bar{B}\, x_t,\qquad y_t = C\, s_t \]

其中：

- \(x_t\) 是第 \(t\) 步的输入；
- \(s_t\) 是**隐状态**（state），沿时间步一环一环传递；
- \(\bar{A}\) 是**衰减因子**（决定历史信息保留多久），\(\bar{B}\) 决定输入如何写入状态，\(C\) 决定状态如何读出；
- \(y_t\) 是输出。

朴素递归的问题是：每一步都依赖上一步，**无法在时间维度上并行**，长序列上 GPU 跑不满。Mamba-2 的 SSD 分解就是来解决这个问题的。

### 2.2 Mamba-2 的几个工程记法

Mamba-2 不是直接给 \(\bar{A},\bar{B}\)，而是用 **log 空间参数化** + **dt（步长）**：

- \(A \le 0\) 是 log 空间的衰减参数（每个 head 一个）；
- `dt` 是每个 token 的离散化步长（经 softplus / clamp 预处理后为正）；
- 真正参与运算的衰减量是 \(dA = dt \cdot A\)，于是 \(\exp(dA) \in (0,1]\) 才是那个衰减因子。

> 直觉：`dt` 越大，这一步「走得越远」，历史衰减越快；`A` 控制「步长」与「遗忘」的耦合。

### 2.3 维度约定（manifest 头注释里写死）

整个 mamba 家族共用一套维度名（见 [tileops/manifest/mamba.yaml:14-22](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/manifest/mamba.yaml#L14-L22)，这段注释约定了全家族的字母表，讲解时请对号入座）：

| 记号 | 含义 | 记号 | 含义 |
|------|------|------|------|
| `B` | batch | `P` | d_head（头维） |
| `S` | seq_len | `N` | d_state（状态维） |
| `H` | n_heads | `G` | n_groups（H % G == 0）|
| `NC` | num_chunks（= S / Q）| `Q` | chunk_len |

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [tileops/ops/mamba2_fwd.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/mamba2_fwd.py) | 端到端编排 Op，把五个子算子串成流水线 |
| [tileops/ops/ssd_chunk_scan.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/ssd_chunk_scan.py) | 融合 chunk 输出 Op（历史路径 + 块内因果路径）|
| [tileops/ops/ssd_decode.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/ssd_decode.py) | 单 token 解码 Op（原地更新状态）|
| [tileops/kernels/mamba/ssd_chunk_scan.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/kernels/mamba/ssd_chunk_scan.py) | chunk-scan 的 TileLang kernel 实现 |
| [tileops/manifest/mamba.yaml](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/manifest/mamba.yaml) | 家族规约：13 个算子条目 + func 模式 roofline 指向 |
| [tileops/perf/formulas.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/perf/formulas.py) | func 模式 roofline 实现（含本轮新增的 bias/init_states 变体）|

> 注：本家族当前所有条目均为 `status: spec-only`，即 manifest 是接口真相、实现已就位但尚未走完「翻转为 implemented」的一致性审查（见 [u9-l3 Status 翻转与字段 carve-out](u9-l3-status-flip-carveout.md)）。代码可运行，但 manifest 在 validator 关卡里仍按 spec-only 只过 L0。

---

## 4. 核心概念与源码讲解

### 4.1 SSD 分解：把递归 SSM 拆成可并行阶段

#### 4.1.1 概念说明

SSD（State-Space Dual）的核心思想是：**把序列沿时间切成固定大小 `Q`（chunk_len）的块，块内用矩阵乘并行算，块间只留一个极轻的递归扫描**。

这样可以兼顾两点：

- **块内（intra-chunk）**：长度 `Q` 的一段序列，递归可被改写成矩阵形式（因果下三角 GEMM），吃满 tensor core，完全并行。
- **块间（inter-chunk）**：每个块只产出一个尺寸为 `P × N` 的「块状态」，把递归依赖从「每 token」压缩到「每块」，扫描长度从 `S` 降到 `NC = S/Q`。

#### 4.1.2 核心流程（数学）

把递归 \(s_t = \exp(dA_t)\,s_{t-1} + \bar B_t x_t\) 在一个块内展开，输出可写成两项之和（这是 [chunk-scan kernel 注释里给出的公式](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/kernels/mamba/ssd_chunk_scan.py#L20-L38)）：

\[ \mathrm{out}[l,p] = \underbrace{\exp(dA_l)\,\sum_n C[l,n]\,\mathrm{prev\_states}[p,n]}_{\text{历史路径（history / off-diagonal）}} + \underbrace{\sum_{s\le l} \mathrm{cb}[l,s]\,\exp(dA_l - dA_s)\,dt[s]\,x[s,p]}_{\text{块内因果路径（intra-chunk / diagonal）}} \]

其中：

- **历史路径**：本块之前所有块累积出的 `prev_states`（P×N），按读出矩阵 C 投影回输出维 P。这是个 (1×N)@(N×P) 的 GEMM。
- **块内因果路径**：本块内 token `s ≤ l` 的贡献，`cb[l,s] = C[l] @ B[s]^T` 是预计算好的耦合矩阵（因果下三角）。

伪代码（与 kernel 的「PART 1 / PART 2」结构对应）：

```
对每个 (batch, chunk, head, l_tile, p_tile):
    # PART 1 历史路径
    acc = exp(dA[l]) * (C[l,:] @ prev_states[:,p])
    # PART 2 块内因果路径（只遍历 s <= l_max 的块）
    for s_tile in 满足 s <= l 的块:
        acc += cb[l,s] * exp(dA[l]-dA[s]) * dt[s] * x[s,p]
    out[l,p] = acc
```

> 数值细节：`dA_cumsum` 是非增的（因为 \(A\le 0\)），所以 `dA_l - dA_s` 在因果对里恒 ≤ 0，`exp` 恒 ≤ 1，**不会上溢**；下溢到 0 也是数值正确的（真值本就接近 0）。kernel 正是利用这点，对「整块都在因果下三角」的 `s_tile` 用 anchor 分解（见 [ssd_chunk_scan.py:226-248](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/kernels/mamba/ssd_chunk_scan.py#L226-L248)），省掉每个元素的 clamp。

#### 4.1.3 源码精读

**chunk-scan kernel 的两段式结构**——PART 1 历史 GEMM + PART 2 因果 GEMM，写在 [tileops/kernels/mamba/ssd_chunk_scan.py:142-444](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/kernels/mamba/ssd_chunk_scan.py#L142-L444)。这段代码做了三件值得注意的事：

1. 把 dA_cumsum / dt 预取进 shared memory（[ssd_chunk_scan.py:198-203](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/kernels/mamba/ssd_chunk_scan.py#L198-L203)），消除因果循环里反复回 L2 的开销。
2. PART 1 用 swizzled layout 的 `c_tile @ state_tile` 走 tensor core（[ssd_chunk_scan.py:148-190](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/kernels/mamba/ssd_chunk_scan.py#L148-L190)）。
3. PART 2 区分「全下三角块」（anchor 分解）与「对角块」（直接算 `exp`），见 [ssd_chunk_scan.py:315-441](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/kernels/mamba/ssd_chunk_scan.py#L315-L441)。

**kernel 的网格设计**把 head 单列为一维，便于负载均衡（[ssd_chunk_scan.py:119-133](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/kernels/mamba/ssd_chunk_scan.py#L119-L133)）：

```
grid = (ceildiv(Q,block_l)*ceildiv(P,block_p),  B*NC,  H)
```

#### 4.1.4 代码实践

**实践目标**：建立「块内并行、块间串行」的直觉。

**操作步骤**（源码阅读型）：

1. 打开 [tileops/kernels/mamba/ssd_chunk_scan.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/kernels/mamba/ssd_chunk_scan.py)。
2. 找到 `with T.Kernel(...)` 那一行（约 121 行），确认网格第 1 维是 `(L_tiles × P_tiles)`——也就是**只对一个块内的 (l, p) 做并行**，而不是对整个序列。
3. 找到 PART 1（`# PART 1: history path`，约 142 行）与 PART 2（`# PART 2: intra-chunk causal path`，约 226 行）。

**需要观察的现象**：

- PART 1 的 `T.gemm(c_tile, state_tile, hist_acc)` 是一次普通 GEMM，与历史状态无关地全并行。
- PART 2 的 s 循环上限是 `T.ceildiv(l0 + block_l, block_s)`（约 288 行）——即只遍历 `s <= l` 的块，这就是「因果下三角」在循环边界上的体现。

**预期结果**：你能口头复述「为什么块内可并行、块间不行」——因为块内贡献是因果 GEMM（可分块并行），而块间通过 `prev_states` 形成链式依赖（必须先算完前一块的状态）。

**待本地验证**：若有 CUDA 环境，可对照 `tests/ops/test_mamba.py` 跑一个 chunk_scan 用例，观察输出 shape 为 `[B, S, H, P]` 的 float32。

#### 4.1.5 小练习与答案

**练习 1**：为什么 kernel 文档强调「`cb` 必须只含 C@B 项，不能含 decay」（见 [mamba2_fwd.py:19-23](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/mamba2_fwd.py#L19-L23)）？

**答案**：因为 `SSDChunkScanFwdKernel` 在块内路径里会**自己乘** `exp(dA[l]-dA[s]) * dt[s]`（见公式第二项）。如果 `cb` 里再带一份 decay，就会被乘两次，数值错误。所以 `CBProducer` 只算耦合矩阵 `C@B`，decay 留给下游。

**练习 2**：网格第 2 维为什么是 `B * NC` 而不是 `B * NC * H`？

**答案**：因为 head 被单独放到了网格第 3 维（`H`），见 [ssd_chunk_scan.py:121-126](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/kernels/mamba/ssd_chunk_scan.py#L121-L126)。把 head 拆出来是为了让不同 head 的 (l,p) tile 互相不挤占 SM，便于负载均衡（注释 `# Grid redesign: separate H dimension for better load balancing`）。

---

### 4.2 端到端编排：Mamba2FwdOp 的五步流水线

#### 4.2.1 概念说明

`Mamba2FwdOp` 是一个**复合 Op**：它本身不写 TileLang kernel，而是把五个子 Op 串成流水线，全程在 GPU 上不回主机。它的接口对齐官方 `mamba_ssm` 的 `mamba_chunk_scan_combined`，让用户用一套张量就能跑通整个 Mamba-2 前向。

#### 4.2.2 核心流程

`Mamba2FwdOp.forward` 的五步（[mamba2_fwd.py:230-273](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/mamba2_fwd.py#L230-L273)）：

```
输入: x[B,S,H,P], dt[B,S,H], A[H], B[B,S,G,N], C[B,S,G,N], (可选 dt_bias, initial_states)

1. DaCumsum     : dt 预处理(softplus/clamp/bias) + dA = dt*A 的块内前缀和
                  → dt_out[B,H,NC,Q], dA_cumsum[B,H,NC,Q]
2. CBProducer   : cb[b,c,g,l,s] = C[l] @ B[s]^T  (因果, 仅耦合项)
                  → cb[B,NC,G,Q,Q]
3. SSDChunkState: 每 chunk 的状态 = Σ_l x·B·exp(dA)·dt  (P×N)
                  → chunk_states[B,NC,H,P,N]
4. SSDStatePassing: 块间递归扫描 s_c = exp(dA_chunk)·s_{c-1} + chunk_states[c]
                  → prev_states[B,NC,H,P,N], final_states
5. SSDChunkScan : 融合历史路径 + 块内因果路径, 产出最终输出
                  → y[B,S,H,P]
```

数据流草图：

```
dt,A,dt_bias ──▶ DaCumsum ──▶ dt_out, dA_cumsum ─┐
                                                 │
C,B ──▶ CBProducer ──▶ cb ───────────────────────┤
                                                 ├──▶ SSDChunkScan ──▶ y
x,B,dt_out,dA_cumsum ──▶ SSDChunkState ──▶ chunk_states
                              │
                              ▼
                  SSDStatePassing ──▶ prev_states (+final_states)
```

#### 4.2.3 源码精读

**子 Op 的构造与缓存**在 `__init__` 里就位（[mamba2_fwd.py:83-98](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/mamba2_fwd.py#L83-L98)）。注意三个细节：

1. `_da_cumsum_ops` 和 `_cb_producer_ops` 是**字典缓存**（按 dtype / 按 (batch, chunks, ...) key），因为这两个子 Op 的配置依赖运行时才知道的形状，要惰性构造并复用。
2. `_chunk_state_op` 构造时硬编码 `has_seq_idx=False`（[mamba2_fwd.py:84-87](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/mamba2_fwd.py#L84-L87)），因此块状态计算**忽略** `seq_idx`——这点在 forward 里传一个预分配的零 `_zero_seq_idx` 来满足签名（见 [mamba2_fwd.py:246-249](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/mamba2_fwd.py#L246-L249)），属于「避免 FillFunctor kernel launch」的热路径优化。
3. **形状重塑技巧**：`SSDChunkStateFwdOp` 输出 `(B,C,H,P,N)`，但 `SSDStatePassingFwdOp` 期望状态维是单个扁平维度，于是代码把 `P*N` reshape 成一维（[mamba2_fwd.py:253](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/mamba2_fwd.py#L253)）再传入，注释明说这是为了**用 TileOPs kernel 而非 Python for 循环**（[mamba2_fwd.py:14-17](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/mamba2_fwd.py#L14-L17)）。

**`final_states` 恒返回**是一个接口设计取舍：manifest 注释 [mamba.yaml:378-387](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/manifest/mamba.yaml#L378-L387) 说明，Op 协议没有「条件输出」机制，且分块预填充（chunked prefill）本就需要携带状态，所以上游的 `return_final_states` 开关在这里不存在——`final_states` 是固定输出。代码侧对应 [mamba2_fwd.py:275-277](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/mamba2_fwd.py#L275-L277)。

#### 4.2.4 代码实践

**实践目标**：把五步流水线的输入输出形状对上号。

**操作步骤**（源码阅读型 + 跟踪调用链）：

1. 读 [mamba2_fwd.py 的 forward](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/mamba2_fwd.py#L141-L277)，每一步后面都有注释标注输出 shape（如 `# dt_out: (B, H, C, Q) dtype`）。
2. 画出 4.2.2 的数据流草图，在每条边上标 shape。
3. 重点核对第 4 步：`dA_chunk_cumsum = dA_cumsum[..., chunk_size-1].contiguous()`（[mamba2_fwd.py:256](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/mamba2_fwd.py#L256)）——它取的是每个块**最后一个 token** 的累积衰减，作为该块的「块级衰减标量」。

**需要观察的现象**：

- 第 1 步到第 5 步全在 GPU 上，中间无 `.cpu()` / `.item()`。
- 第 4 步把 `(B,C,H,P,N)` flatten 成 `(B,C,H,P*N)`，扫完再 unflatten 回 `(B,C,H,P,N)`。

**预期结果**：你能说清 `dA_cumsum`（每 token）与 `dA_chunk_cumsum`（每 chunk）的区别，以及为什么 state passing 只需要后者。

#### 4.2.5 小练习与答案

**练习 1**：第 3 步 `SSDChunkStateFwdOp` 构造了 `has_seq_idx=False`，那 forward 里为什么还要传 `self._zero_seq_idx`？

**答案**：因为 kernel 的 forward 签名固定接收 `seq_idx` 参数（见 [ssd_chunk_state.py 的 forward](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/ssd_chunk_state.py)），`has_seq_idx=False` 时 kernel **完全不访问**这个张量。代码预分配一个零张量传进去，是为了避免每次调用都触发一个 `FillFunctor<int>` kernel launch（热路径优化，见 [mamba2_fwd.py:246-249](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/mamba2_fwd.py#L246-L249) 的注释）。

**练习 2**：`_da_cumsum_ops` 为什么是 `dict[torch.dtype, ...]`？

**答案**：因为 `dt_out` 的存储 dtype 由调用时的 `x.dtype` 决定（input-inferred），不同 dtype（float16 / bfloat16）会生成不同的 kernel，所以要按 dtype 缓存复用（[mamba2_fwd.py:106-115](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/mamba2_fwd.py#L106-L115)）。注意它构造时固定 `has_dt_bias=True`——因为上层用一个零 `_zero_dt_bias` 来统一走带 bias 的路径。

---

### 4.3 chunk-wise 路径与 decode 路径的 kernel 选择

#### 4.3.1 概念说明

Mamba-2 推理分两个阶段，TileOPs 用**两套不同的 Op**：

- **预填充（chunk-wise / prefill）**：一次处理一整段序列（长度 S），走 `Mamba2FwdOp` → `SSDChunkScanFwdOp`，吃满并行。
- **逐 token 解码（decode）**：自回归生成时每次只来 1 个 token，走 `SSDDecodeOp`，**原地更新状态**、只算一步。

> 直觉：prefill 是「批发」，分块并行划算；decode 是「零售」，每次一个 token，老老实实跑一步 SSM 递归。

#### 4.3.2 核心流程（decode 的数学）

`SSDDecodeOp` 做单步递归（[ssd_decode.py:19-23](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/ssd_decode.py#L19-L23)）：

\[ \begin{aligned} g &= h\, //\, (H//G) \\
dA[b,h,p,n] &= \exp(dt[b,h,p] \cdot A[h,p,n]) \\
\mathrm{state}[b,h,p,n] &\leftarrow dA[b,h,p,n]\cdot \mathrm{state}[b,h,p,n] + dt[b,h,p]\cdot B_{in}[b,g,n]\cdot x[b,h,p] \\
y_{out}[b,h,p] &= \sum_n \mathrm{state}[b,h,p,n]\cdot C_{in}[b,g,n]
\end{aligned} \]

注意 `state` 是**读+写原地更新**（mutates），`y_out` 是唯一返回张量。注释明确：skip connection（`D*x`）和 z-gate **不在**这里融合，由调用方自行施加（[ssd_decode.py:25-26](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/ssd_decode.py#L25-L26)）。

#### 4.3.3 源码精读

**两条路径在 manifest 里的对照**：

| | chunk-wise 预填充 | decode 解码 |
|---|---|---|
| Op | `Mamba2FwdOp` / `SSDChunkScanFwdOp` | `SSDDecodeOp` |
| 输入序列长度 | 整段 S（多 token）| 单 token |
| kernel | `SSDChunkScanFwdKernel`（两段式 GEMM）| `SSDDecodeKernel`（一步递归）|
| 状态 | 产出 `final_states` | **原地**更新 `state` |
| manifest 条目 | [mamba.yaml:300-337](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/manifest/mamba.yaml#L300-L337)（chunk_scan）/ [339-376](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/manifest/mamba.yaml#L339-L376)（decode）| |

**kernel 的 config 选择**体现了「路径不同、调优策略不同」：`SSDChunkScanFwdKernel` 的 `default_config` 与 `autotune_configs` 是围绕「块内 GEMM tile 效率」调的（block_l=64, block_p=64, num_stages=3，见 [ssd_chunk_scan.py:564-610](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/kernels/mamba/ssd_chunk_scan.py#L564-L610)），autotune 注释引用了 NCU 证据；decode kernel 没有这种分块 GEMM，关注的是单步访存合并。

**架构兼容**：`SSDChunkScanFwdKernel.supported_archs = [80, 86, 89, 90]`（[ssd_chunk_scan.py:535](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/kernels/mamba/ssd_chunk_scan.py#L535)），覆盖 Ampere 到 Hopper。这呼应 [u2-l2 Kernel 选择与架构兼容性](u2-l2-kernel-selection-and-arch.md)：`Op` 构造期经 `dispatch_kernel → _install_kernel_map` 做 `supported_archs` 校验，不在表里的 SM 版本会在构造期 fail-fast。

#### 4.3.4 代码实践

**实践目标**：分清两条路径的 Op 与 kernel 选择。

**操作步骤**（源码阅读型）：

1. 打开 [tileops/ops/ssd_decode.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/ssd_decode.py)，读 `forward` 的形状校验（[ssd_decode.py:96-122](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/ssd_decode.py#L96-L122)）：注意 `state` 必须 contiguous 且 dtype float32，因为它要**原地写**。
2. 对照 [tileops/ops/ssd_chunk_scan.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/ssd_chunk_scan.py) 的 `forward`（[ssd_chunk_scan.py:93-158](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/ssd_chunk_scan.py#L93-L158)）：它返回新张量 `y`，不动 `prev_states`。

**需要观察的现象**：

- `SSDDecodeOp` 的 `state` 参数既在 inputs 里（读），又在 docstring 里标注「mutated in-place」——这是它和 chunk_scan 的本质差异。
- `SSDChunkScanFwdOp` 有 `default_kernel_map` 返回 `{"ssd_chunk_scan_fwd": SSDChunkScanFwdKernel}`（[ssd_chunk_scan.py:51-53](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/ssd_chunk_scan.py#L51-L53)），走标准 `dispatch_kernel` 安装流程。

**预期结果**：你能回答「为什么 decode 不复用 chunk_scan kernel」——因为单 token 时没有「块内因果」可言，chunk 分解的并行红利消失，反而引入开销；一步递归更直接。

**待本地验证**：`SSDDecodeOp` 的 kernel 在本仓库 kernels 子包里，但若当前 SM 不在 `[80,86,89,90]`，构造期会报 `ValueError`。

#### 4.3.5 小练习与答案

**练习 1**：decode 的 `state` 为什么要 `contiguous()` 且 float32？

**答案**：因为 kernel **原地更新** `state`（[ssd_decode.py:121-122](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/ssd_decode.py#L121-L122)）。非连续张量做原地写会写出错位或需要额外索引；float32 是因为状态累加需要精度（与全家族「fp32 累加、边界 cast」的数值约定一致）。

**练习 2**：`SSDChunkScanFwdKernel.autotune_configs` 里为什么 `num_stages=3` 而不是更大？

**答案**：注释明说「tested: stages=5 is 13% slower」（[ssd_chunk_scan.py:590](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/kernels/mamba/ssd_chunk_scan.py#L590)）。pipeline stage 数增大会多吃 shared memory、降低 occupancy，对这个两段式 kernel 反而变慢，所以固定 3。

---

### 4.4 状态传递语义与 bias / init_states 变体的 roofline 来源

#### 4.4.1 概念说明

本模块解决两个问题：

1. **状态传递（state passing）kernel 干什么？** 它是 SSD 分解里**唯一真正串行**的环节——对 `NC` 个块状态做一维递归扫描。但因为扫描维被压成 `d_head*d_state`（扁平化），每个元素的扫描互相独立，仍能大并行。
2. **manifest 里的 bias / init_states 变体从哪来？** 上游 `mamba_ssm` 把 `dt_bias`、`seq_idx`、`initial_states` 设计成**可选张量**。但 TileOPs 的 Op 协议没有「条件输入/输出」机制，所以 manifest 用 **`variant_of` 模式**：每种可选张量的组合各占一条条目，对应一个独立的 Op 类与一个独立的 func 模式 roofline。

#### 4.4.2 核心流程（状态传递）

`SSDStatePassingFwdOp` 的递归（[ssd_state_passing.py 的 docstring](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/ssd_state_passing.py)）：

\[ s_c[m] = \exp(\mathrm{dA\_chunk\_cumsum}[b,h,c]) \cdot s_{c-1}[m] + \mathrm{states}[b,c,h,m] \]

其中 \(s_{-1} = \mathrm{initial\_states}\)（或 `has_initial_states=False` 时为 0）。注意这里 `m` 是扁平化的 `d_head * d_state` 维（见 4.2.3 的 reshape）。

**`variant_of` 模式一览**（manifest 里共 6 组变体对）：

| 主条目 | 变体条目 | 多出的张量 | roofline func |
|--------|----------|-----------|---------------|
| `DaCumsumFwdOp` | `DaCumsumBiasFwdOp` | `dt_bias[H]` | `da_cumsum_bias_fwd_roofline` |
| `SSDChunkStateFwdOp` | `SSDChunkStateSeqIdxFwdOp` | `seq_idx[B,S]` | `ssd_chunk_state_seq_idx_fwd_roofline` |
| `SSDStatePassingFwdOp` | `SSDStatePassingInitStatesFwdOp` | `initial_states[B,H,N]` | `ssd_state_passing_init_states_fwd_roofline` |
| `Mamba2FwdOp` | `Mamba2BiasFwdOp` | `dt_bias[H]` | `mamba2_bias_fwd_roofline` |
| `Mamba2FwdOp` | `Mamba2InitStatesFwdOp` | `initial_states` | `mamba2_init_states_fwd_roofline` |
| `Mamba2FwdOp` | `Mamba2BiasInitStatesFwdOp` | `dt_bias` + `initial_states` | `mamba2_bias_init_states_fwd_roofline` |

manifest 注释把这归结为 **"No Optional[Tensor]" 规则**（[mamba.yaml:20-22](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/manifest/mamba.yaml#L20-L22)）：条件张量输入一律建模成 `variant_of` 条目，而不是把某个 input 标成 Optional。

#### 4.4.3 源码精读（roofline 来源）

**本轮更新的关键点**：每个变体现在都绑定了自己的 func 模式 roofline，它们共享一个 cost helper，只翻转一个 `has_*` 标志。

**示例 1：da_cumsum 的 bias 变体**。两个公开函数共享 `_da_cumsum_fwd_cost`：

[formulas.py:1237-1252](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/perf/formulas.py#L1237-L1252) 展示了这一对：

```python
def da_cumsum_fwd_roofline(op):           # 主条目: has_dt_bias=False
    return _da_cumsum_fwd_cost(..., has_dt_bias=False, dt_softplus=...)

def da_cumsum_bias_fwd_roofline(op):      # 变体: has_dt_bias=True
    return _da_cumsum_fwd_cost(..., has_dt_bias=True,  dt_softplus=...)
```

而 `_da_cumsum_fwd_cost`（[formulas.py:1220-1234](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/perf/formulas.py#L1220-L1234)）里，`has_dt_bias` 同时影响 FLOP（多一个 bias add，+1）和字节（多读 `n_heads*4` 字节）：

```python
flops = (3 + (1 if has_dt_bias else 0) + (4 if dt_softplus else 0)) * tokens
nbytes = (tokens*4 + n_heads*4
          + (n_heads*4 if has_dt_bias else 0)   # dt_bias
          + tokens*elem_bytes + tokens*4)
```

**示例 2：Mamba2 端到端的四个变体**。`mamba2_fwd_roofline` / `mamba2_bias_fwd_roofline` / `mamba2_init_states_fwd_roofline` / `mamba2_bias_init_states_fwd_roofline` 四个函数（[formulas.py:1457-1475](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/perf/formulas.py#L1457-L1475)）全部委托给 `_mamba2_fwd_cost(op, has_dt_bias=..., has_initial_states=...)`。后者（[formulas.py:1407-1454](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/perf/formulas.py#L1407-L1454)）把五个阶段的 cost 求和，`has_dt_bias` / `has_initial_states` 只在两处产生差异：dt_bias 的 FLOP+字节，以及 initial_states 的读字节。这正呼应 [u7-l2 roofline 字段与两种模式](u7-l2-roofline-field-modes.md)——**多输入 dtype / 条件张量的字节记账无法用 inline 的单一 `elem_bytes` 表达，必须用 func 模式**。

**manifest 与 func 的绑定**：每个条目的 `roofline.func` 字段指向具体函数名，例如 [mamba.yaml:453-454](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/manifest/mamba.yaml#L453-L454)（`Mamba2BiasFwdOp` 指向 `mamba2_bias_fwd_roofline`）、[mamba.yaml:97-98](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/manifest/mamba.yaml#L97-L98)（`DaCumsumBiasFwdOp` 指向 `da_cumsum_bias_fwd_roofline`）。codegen 在 Op 类定义期 eager 解析这条 dotted 路径并 emit（见 [u7-l3 GPU Profile 与 func 公式实现](u7-l3-gpu-profile-and-formulas.md)），坏路径在 codegen 闸门即报错。

> 注意 helpers 都标注「valid only after the first forward()」（[formulas.py:1216-1217](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/perf/formulas.py#L1216-L1217)）——因为 batch/heads/dims 都是 input-inferred，要等首次 forward 绑定后才能算。

#### 4.4.4 代码实践

**实践目标**：把 manifest 的 variant_of 条目与 formulas.py 的 func 一一对应。

**操作步骤**（源码阅读型，本讲核心实践）：

1. 打开 [tileops/manifest/mamba.yaml](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/manifest/mamba.yaml)，定位 `Mamba2BiasFwdOp`（[第 423 行](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/manifest/mamba.yaml#L423)）：它有 `variant_of: Mamba2FwdOp`、inputs 多了 `dt_bias`、`roofline.func: mamba2_bias_fwd_roofline`。
2. 跳到 [tileops/perf/formulas.py:1463-1465](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/perf/formulas.py#L1463-L1465)，确认 `mamba2_bias_fwd_roofline` 调 `_mamba2_fwd_cost(op, has_dt_bias=True, has_initial_states=False)`。
3. 在 `_mamba2_fwd_cost`（[formulas.py:1407-1454](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/perf/formulas.py#L1407-L1454)）里 grep `has_dt_bias`，确认它只影响两处：dt_bias 的 FLOP（在 da_cumsum stage）与字节（`+ n_heads*4 if has_dt_bias`）。
4. 重复 1-3，对 `Mamba2BiasInitStatesFwdOp`（[mamba.yaml:503](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/manifest/mamba.yaml#L503)）→ `mamba2_bias_init_states_fwd_roofline`（[formulas.py:1473-1475](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/perf/formulas.py#L1473-L1475)）走一遍。

**需要观察的现象**：

- 四个 mamba2 变体 roofline 函数体都只有一行 `return _mamba2_fwd_cost(...)`，差异仅在两个布尔参数——这就是「变体 = 同一 cost 模型 + 一个翻转标志」的模式。
- 子阶段同理：`SSDChunkStateFwdOp` vs `SSDChunkStateSeqIdxFwdOp` 共享 `_ssd_chunk_state_fwd_cost`，`has_seq_idx` 只多算 `seq_idx` 的读字节（[formulas.py:1307-1313](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/perf/formulas.py#L1307-L1313) vs [1298-1304](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/perf/formulas.py#L1298-L1304)）。

**预期结果**：你能为家族里任意一条 variant_of 条目，指出「它的 roofline 来源 = 共享 helper + 翻转哪个 has_* 标志 + 该标志影响了哪几项 FLOP/字节」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `dt_bias` 只多了 `n_heads*4` 字节，而不是 `tokens*4`？

**答案**：因为 `dt_bias` 的 shape 是 `[H]`（每个 head 一个标量），不是 per-token（见 [mamba.yaml:438](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/manifest/mamba.yaml#L438)）。它对每个 token 广播，逻辑上被读很多次，但物理只在 L2/cache 里读 `H` 个 float32 = `n_heads*4` 字节（[formulas.py:1230](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/perf/formulas.py#L1230)）。

**练习 2**：`SSDStatePassingFwdOp` 与 `SSDStatePassingInitStatesFwdOp` 的 roofline 差异是什么？

**答案**：共享 `_ssd_state_passing_fwd_cost`，`has_initial_states=True` 时多算 `initial_states` 的读字节（`batch * n_heads * d_state * 4`，见 [formulas.py:1327-1328](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/perf/formulas.py#L1327-L1328)）。FLOP 不变（扫描递归本身不因初值而多算）。

---

## 5. 综合实践

**任务**：为本家族画一张「manifest 条目 → Op 类 → kernel → roofline func」的完整对照表，并验证变体的 roofline 来源。

**步骤**：

1. 用程序化方式加载 manifest，过滤出 mamba 家族：
   ```python
   from tileops.manifest import load_manifest
   m = load_manifest()
   mamba = {k: v for k, v in m["ops"].items() if v.get("family") == "mamba"}
   print(len(mamba))  # 预期 13
   ```
2. 对每个条目，提取 `variant_of`（若有）、`roofline.func`、`source.kernel_map`，整理成表。
3. 对 `Mamba2FwdOp` 与它的三个变体，分别打开对应的 [formulas.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/perf/formulas.py) 函数，确认四者都委托给 `_mamba2_fwd_cost`，差异仅在 `has_dt_bias` / `has_initial_states`。
4. **画出端到端编排图**：参照 4.2.2 的草图，把 `Mamba2FwdOp.forward` 的五步（DaCumsum / CBProducer / SSDChunkState / SSDStatePassing / SSDChunkScan）连起来，在每条数据流边上标注 shape 与 dtype。

**验收标准**：

- 表里 13 个条目，每个都能对上 `roofline.func`。
- 三个 mamba2 变体的 func 差异能口头说清（翻转哪个 flag、影响哪几项）。
- 编排图能解释 `chunk_states` 为何要先 flatten 成 `(B,C,H,P*N)` 再进 state passing。

**待本地验证**：步骤 1 的 `len(mamba)` 是否恰好为 13，取决于当前 HEAD 的 manifest 状态；若家族有新增/裁撤，以实际加载结果为准。

---

## 6. 本讲小结

- **SSD 分解**把串行 SSM 递归拆成「块内并行（因果 GEMM）+ 块间轻量扫描」，把时间维的依赖从每 token 压到每 chunk。
- **`Mamba2FwdOp`** 是复合 Op，五步流水线 `DaCumsum → CBProducer → SSDChunkState → SSDStatePassing → SSDChunkScan`，全程 GPU 不回主机，并把 `P*N` flatten 以复用 state-passing kernel。
- **两条路径**：预填充走 `SSDChunkScanFwdKernel`（两段式 GEMM，分块调优），解码走 `SSDDecodeKernel`（单步递归，原地更新 state）；后者 `state` 必须 contiguous + float32。
- **chunk-scan kernel** 用 `dA_cumsum` 非增的性质保证 `exp(dA_l-dA_s)` 不上溢，对全下三角块用 anchor 分解省 clamp。
- **状态传递**是唯一真串行环节，但扫描维被扁平化为 `d_head*d_state`，元素间仍大并行。
- **variant_of 模式**：可选张量（`dt_bias`/`seq_idx`/`initial_states`）各成一条 manifest 条目，每个变体绑定自己的 func roofline，共享一个 cost helper、只翻转一个 `has_*` 标志（本轮 formulas.py 新增的 `mamba2_bias_*` / `ssd_*_init_states` / `ssd_chunk_state_seq_idx` 等函数即此模式的完整落地）。

## 7. 下一步学习建议

- **多 kernel 协作的另一范例**：读 [u12-l1 Attention 家族](u12-l1-attention-family.md) 与 [u12-l2 MoE 家族](u12-l2-moe-family.md)，对比它们与 Mamba-2 在「编排多个 kernel」上的异同（attention 是 QKV→score→softmax→AV，MoE 是 topk→permute→grouped_gemm→unpermute）。
- **func 模式 roofline 的字节记账细节**：回到 [u7-l3 GPU Profile 与 func 公式实现](u7-l3-gpu-profile-and-formulas.md)，结合本讲的 `_mamba2_fwd_cost` 理解「多输入 dtype / 条件张量为何必须 func」。
- **spec-only → implemented 的翻转流程**：本家族全为 spec-only，若想动手推进某个子算子（如先翻 `SSDDecodeOp`），先读 [u9-l3 Status 翻转与字段 carve-out](u9-l3-status-flip-carveout.md) 与 [u9-l2 验证器的五级检查](u9-l2-validator-five-levels.md)，了解翻转时要补 `kernel_map`、过 L0–L4。
- **源码延伸**：阅读 `tileops/ops/da_cumsum.py`、`ssd_chunk_state.py`、`ssd_state_passing.py`、`cb_producer.py` 这四个子 Op，补齐「五步流水线」里本讲未展开的三个子算子的 forward 细节。

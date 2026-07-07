# MLA (Multi-head Latent Attention)

> 本讲面向 FA4 的 Blackwell（SM100/SM110）专用 MLA kernel。MLA 是 DeepSeek-V2/V3 提出的注意力变体，本仓库用一套「吸收（absorbed）权重」公式把它落到 Blackwell 的 UMMA/2CTA/tmem 之上。

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 DeepSeek 风格 **MLA absorbed 公式** `O = softmax(scale·(Q@Kᵀ + Qv@Vᵀ))@V` 中每个张量的含义，以及为什么 FA4 用「(head_dim, head_dim_v) = (64, 512) 或 (512, 512)」这种不对称形状。
- 看懂 FA4 公共接口如何通过一个 `qv` 参数把普通注意力切换成 MLA 路径，并理解 `shared_kv`（K、V 共享同一潜在向量）这条「更激进」的吸收捷径。
- 读懂 `FlashAttentionMLAForwardSm100` 前向 kernel：三段 MMA（`Q@Kᵀ`、`Qv@Vᵀ`、`P@V`）、hdimv 对半切、2CTA cluster、tmem 里的 S/O 累加器。
- 了解 MLA 的稀疏（top-k gather KV）反向为何被拆成「主循环 + dQdQv GEMM + dK GEMM」三个独立 kernel，以及它当前的能力边界。

## 2. 前置知识

本讲建立在前置讲义 **u8-l1（Blackwell 前向 Kernel 全景）** 之上，请确认你已经理解以下概念：

- **UMMA / tcgen05.mma**：Blackwell 的矩阵乘单元，累加器住在片上 **tmem**（而非寄存器）。
- **2CTA**：cluster 形状 `(2,1)`，两个 CTA 协作，MMA 的 M 维与 `tx_count` 都要 ×2（见 u8-l4）。
- **persistent kernel / CLC 调度**：CTA 在 `while is_valid_tile` 循环里持续领活（见 u8-l2）。
- **在线 softmax**：`row_max` / `row_sum` / rescale 三步，`exp2` 换底（见 u4-l1）。
- **pack_gqa**：把 `qhead_per_kvhead` 折叠进 seqlen 维，让一块 KV 被多个 Q 头复用（见 u7-l1）。

下面先用一段直觉说明 **MLA 到底想解决什么问题**。

标准注意力的 KV cache 大小正比于 `nheads_kv × head_dim × seqlen`。当模型很大、上下文很长时，KV cache 会成为推理显存与带宽的主要瓶颈。MLA 的核心思想是：**把每个 token 的 KV 表示压缩成一个低秩的「潜在向量」**，存这个潜在向量而不是完整的 K/V，从而大幅缩小 KV cache；在注意力计算时再用「上投影」矩阵把潜在向量展开。

DeepSeek 的实现里，这种展开被巧妙地「吸收（absorb）」进了查询侧：我们不再显式上投影出完整的 K/V，而是让查询携带两套分量——`q_pe`（与位置编码 K 配对）和 `q_nope`（与潜在 V 配对）。于是注意力公式变成两项之和：

\[
S = \mathrm{scale}\cdot(QK^\top + Q_v V^\top),\qquad O = \mathrm{softmax}(S)\,V
\]

本讲用大写 **Q** 表示 `q_pe`（head_dim=64，与 K 对齐），**Qv** 表示 `q_nope`（head_dim_v=512，与潜在 V 对齐）。**注意**：公式里的 `+` 是把两个矩阵乘的得分**逐元素相加**，这与 FA4 的 tmem 累加器直接对应——`S += Qv @ Vᵀ` 累加到 `S = Q @ Kᵀ` 已经写好的同一块 tmem 上。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `flash_attn/cute/interface.py` | 公共接口与架构分发：`qv` 参数触发 MLA、`shared_kv` 吸收捷径、absorbed 形状校验、MLA 反向入口 `_flash_attn_bwd_sparse_mla` |
| `flash_attn/cute/flash_fwd_mla_sm100.py` | `FlashAttentionMLAForwardSm100`：MLA 前向主 kernel（2CTA、UMMA、三段 MMA） |
| `flash_attn/cute/flash_bwd_mla_sm100.py` | `FlashAttentionSparseMLABackwardSm100`：MLA 稀疏反向主循环（算 dV、dS） |
| `flash_attn/cute/flash_bwd_mla_dq_dqv_sm100.py` | `dQdQvGemmKernel`：反向辅助 GEMM，算 `dQ = dS@K` 与 `dQv = dS@V` |
| `flash_attn/cute/flash_bwd_mla_dk_sm100.py` | `dKGemmKernel`：反向辅助 GEMM，算 `dK` |
| `tests/cute/test_flash_attn.py` | `test_flash_attn_mla_absorbed`：MLA 端到端数值正确性测试 |

---

## 4. 核心概念与源码讲解

### 4.1 MLA absorbed 形状与潜在 KV

#### 4.1.1 概念说明

absorbed MLA 的关键特征是 **Q/K 与 V 维度不对称**：

- **Q、K** 走 `head_dim = 64`（DeepSeek 的 `q_pe` / `pe_cache`，与旋转位置编码对齐）。
- **Qv、V** 走 `head_dim_v = 512`（`q_nope` / 潜在 `kv_cache`，是压缩后的潜在维度）。

也就是说，同一个注意力里同时存在两套维度：一套窄（64）用于位置相关的 `Q@Kᵀ`，一套宽（512）用于潜在向量的 `Qv@Vᵀ` 与 `P@V`。FA4 在接口层把这两种合法 absorbed 形状写成显式判定：

```python
is_deepseek_mla_absorbed_shape = (head_dim == 64 or head_dim == head_dim_v) and head_dim_v == 512
```

即允许 `(head_dim, head_dim_v) = (64, 512)` 或 `(512, 512)`。

**`qv` 是触发开关**：当用户给 `flash_attn_func(..., qv=qv)` 传入 `qv` 时，FA4 进入 MLA 路径。此时 `softmax_scale` 的默认值也变了——标准注意力用 `1/√head_dim`，而 absorbed MLA 用 `1/√(head_dim + head_dim_v)`，因为得分同时依赖两套维度的乘积：

\[
\mathrm{scale} = \frac{1}{\sqrt{d + d_v}}
\]

#### 4.1.2 核心流程：从公共 API 到 absorbed 形状

```text
用户调用 flash_attn_func(q, k, v, qv=qv, ...)
        │
        ├─ qv is not None  ──► 走 MLA 路径
        │
        ├─ _validate_head_dims：校验 (hdim, hdimv) ∈ {(64,512),(512,512),...}
        │
        ├─ softmax_scale 缺省 = 1/√(head_dim + head_dim_v)
        │
        ├─ lse_shape 改为 (batch, seqlen_q, nheads)  # nheads 连续，利于 MQA 向量化
        │
        └─ arch//10 ∈ {10,11} 且 qv is not None
              └─► FlashAttentionMLAForwardSm100(...)   # Blackwell 专用 MLA 前向
```

**`shared_kv` 吸收捷径**：DeepSeek 推理时常常 K 和 V 共享同一个潜在向量（即 `k is v` 且 `v.shape[-1]==512`）。此时可以把 `Q@Kᵀ` 整项丢掉，只算 `Qv@Vᵀ`，于是接口直接令 `q = k = None`（`has_qk=False`），kernel 内部所有与 Q/K 相关的分支会被编译期裁掉。这是一条比一般 absorbed 更激进的捷径。

#### 4.1.3 源码精读

公共接口侧的 absorbed 形状校验：

[flash_attn/cute/interface.py:95-112](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L95-L112) — `_validate_head_dims` 把 `(64,512)` / `(512,512)` 等 DeepSeek absorbed 形状显式列入 SM100/SM110 的合法形状集合，否则会因不在 `[8,128]` 标准区间而断言失败。

[flash_attn/cute/interface.py:452-456](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L452-L456) — 当 `qv` 与 `q` 同时存在时，`softmax_scale` 缺省取 `1/√(head_dim + head_dim_v)`；只有 `qv`（`shared_kv` 捷径）时回退到 `1/√head_dim`。

[flash_attn/cute/interface.py:471-475](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L471-L475) — MLA 下 LSE 的形状改为 `(batch, seqlen_q, nheads)`：把 `nheads` 放到最后一维使其连续，因为 absorbed MLA 通常是 MQA（`nheads` 很大、如 128），连续布局利于后续向量化访存。这与标准注意力的 `(batch, nheads, seqlen_q)` 不同。

`shared_kv` 吸收捷径（前向 autograd Function 的 `forward` 内）：

[flash_attn/cute/interface.py:2450-2456](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L2450-L2456) — 当 `k is v` 且 `v.shape[-1]==512` 时，令 `qv = q`、`q = k = None`，专门走「只算 `Qv@Vᵀ`」的 MLA 公式。

接口 docstring 里对 absorbed 公式的权威定义：

[flash_attn/cute/interface.py:2810-2817](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L2810-L2817) — 写明 `O = softmax(scale * (Q @ K.T + Qv @ V.T)) @ V`，并指出 `Q = q_pe`、`Qv = q_nope`、`K = pe_cache`、`V = kv_cache`。

#### 4.1.4 代码实践

**目标**：确认 absorbed 形状的形状校验与 `softmax_scale` 默认值，无需 GPU。

**操作步骤**：

1. 打开 [flash_attn/cute/interface.py:95](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L95)，把 `is_deepseek_mla_absorbed_shape` 的布尔表达式抄下来。
2. 用 Python 手算验证：`(64, 512)`、`(512, 512)` 返回 `True`，而 `(128, 512)`、`(64, 256)` 返回 `False`。
3. 对照 [interface.py:452-456](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L452-L456) 计算：当 `head_dim=64, head_dim_v=512` 时，absorbed MLA 的默认 `softmax_scale = 1/√576 ≈ 0.0417`。

**需要观察的现象**：

- `(64,512)` 与 `(512,512)` 是当前唯一被显式放行的 absorbed 形状（`v.shape[-1]` 必须恰好等于 512）。
- `softmax_scale` 在 MLA 下比标准注意力小得多（分母里多了 512）。

**预期结果**：手算的两个布尔值与上述一致；默认 scale 约 0.0417。**若不确定手算结果，待本地用 Python 复算验证。**

#### 4.1.5 小练习与答案

**练习 1**：为什么 absorbed MLA 把 LSE 形状的 `nheads` 放到最后一维（连续），而标准注意力把 `seqlen` 放到最后一维？

**参考答案**：absorbed MLA 几乎总是 MQA（`nheads_kv=1`，`nheads` 很大，如 128），让 `nheads` 连续可以让反向与后续消费者按 `nheads` 维做向量化加载（128-bit 对齐），带宽利用率更高；标准注意力里 `seqlen` 通常是主导变化维度，连续更利于按序列扫描。

**练习 2**：`shared_kv` 捷径（`k is v` 且 `v.shape[-1]==512`）相比一般 absorbed MLA，省掉了哪一项计算？

**参考答案**：省掉了 `Q @ Kᵀ` 整项（即 `q_pe` 与 `pe_cache` 的位置相关得分），只保留 `Qv @ Vᵀ`，对应 kernel 里 `has_qk=False`、所有 Q/K 相关分支与 smem/tmem 被编译期裁掉。

---

### 4.2 MLA 前向 kernel：FlashAttentionMLAForwardSm100

#### 4.2.1 概念说明

`FlashAttentionMLAForwardSm100` 是 FA4 为 absorbed MLA 专门写的前向 kernel，与一般 Blackwell 前向（u8-l1 的 `FlashAttentionForwardSm100`）是**并列**的两套实现。它针对 MLA 的两个特点做了定制：

1. **三段 MMA**：标准前向只有 `Q@Kᵀ` 与 `P@V` 两段；MLA 多出一段 `Qv@Vᵀ`，且要把它的结果**累加到** `Q@Kᵀ` 的得分累加器上。
2. **hdimv 对半切**：`head_dim_v=512` 太宽，一条 MMA 吃不下，于是把 V 沿 `dv` 维对半切成两半（`num_hdimv_splits=2`），每半 256 维分别做 `Qv@Vᵀ` 与 `P@V`，输出 `O` 也对应拆成 `O0`、`O1` 两块。

同时它复用了 Blackwell 的全部「重武器」：2CTA cluster、UMMA（累加器住 tmem）、CLC 动态调度、pack_gqa（支持 MQA 128 头）、top-k gather KV 稀疏路径、paged KV。

#### 4.2.2 核心流程：一次 work tile 的计算

```text
对每个 work tile (cluster_m_block, head_idx, batch_idx)：
  1. 加载 Q tile（hdim=64）、Qv tile（hdimv=512，分两半）到 smem
  2. 倒序遍历 K/V 的 n_block：
     a. S = Q @ Kᵀ                  # 写入 tmem S 区（zero_init）
     b. S += Qv_i @ V_iᵀ  (i=0,1)   # 累加进同一块 tmem S
     c. softmax(S) → P              # 在 softmax warp 里做在线 softmax
     d. O_i += P @ Vt_i  (i=0,1)    # 写入 tmem O0/O1 区
  3. correction：用 acc_scale 修正 O（在线 softmax 的 rescale）
  4. epilogue：O *= 1/row_sum，下转 fp16，TMA store 回 gmem；可选写 LSE
```

关键数据落点：

- **S 累加器**：住在 tmem，`num_stages_S=2` 份双缓冲，列数 `tile_n // cta_group_size`。
- **O 累加器**：拆成 `O0`、`O1` 两块（对应 hdimv 的两个半区），各自在 tmem 有独立偏移。
- **P**：softmax 的产物，由 rmem 经 smem 喂给 `P@V` 的 MMA。

#### 4.2.3 源码精读

类与配置（`__init__`）：

[flash_attn/cute/flash_fwd_mla_sm100.py:48-65](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_mla_sm100.py#L48-L65) — 构造参数里 `hdim=64, hdimv=512` 是默认 absorbed 形状，`has_qk` 控制是否计算 `Q@Kᵀ` 项（`shared_kv` 捷径下设为 `False`）。注意 `is_topk_gather` 路径要求 `qhead_per_kvhead==128` 且 `use_cpasync_load_KV`。

[flash_attn/cute/flash_fwd_mla_sm100.py:171-193](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_mla_sm100.py#L171-L193) — 2CTA 配置（`cluster_shape_mn=(2,1)`、`cta_group_size=2`）与问题形状：`cta_tile_m=64`、`tile_n=128`、`num_hdimv_splits=2`（把 512 维 V 对半切）。`epi_tile` 的第二维正是 `hdimv // num_hdimv_splits = 256`。

三段 MMA 的 tiler 定义：

[flash_attn/cute/flash_fwd_mla_sm100.py:195-219](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_mla_sm100.py#L195-L219) — 定义三个 MMA tiler：`mma_tiler_QK`（S=Q@Kᵀ）、`mma_tiler_QvV`（S+=Qv@Vᵀ）、`mma_tiler_PVt`（Oi=P@V）。注意 `QvV` 与 `PVt` 的 K/N 维都用 `hdimv // num_hdimv_splits`，即每段只算半个 V。

[flash_attn/cute/flash_fwd_mla_sm100.py:471-481](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_mla_sm100.py#L471-L481) — 用 `sm100_utils.make_trivial_tiled_mma` 把三个 tiler 实例化为 Blackwell 的 `tcgen05` MMA，累加器类型为 `Float32`。

主计算 `mma` 方法——这是 MLA 三段 MMA 的核心：

[flash_attn/cute/flash_fwd_mla_sm100.py:2329-2340](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_mla_sm100.py#L2329-L2340) — prologue 里先 `S = Q @ Kᵀ`（`zero_init=True`，覆盖写），随后对两个 hdimv 半区分别 `S += Qvi @ Viᵀ` 累加进**同一块** tmem S（`zero_init=split==0 and not has_qk`，即 `shared_kv` 捷径下第一个半区也要 zero_init）。这正是 absorbed 公式 `S = QKᵀ + QvVᵀ` 的逐元素相加。

[flash_attn/cute/flash_fwd_mla_sm100.py:2361-2377](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_mla_sm100.py#L2361-L2377) — 紧接着对两个半区分别做 `Oi += P @ Vi`，结果写入 tmem 的 `O0`/`O1` 区。`zero_init=not O_should_accumulate` 控制首块覆盖、后续块累加，对应标准 FA 的「P@V 跨 n_block 累加」。

softmax 与 epilogue：

[flash_attn/cute/flash_fwd_mla_sm100.py:2478-2761](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_mla_sm100.py#L2478-L2761) — `softmax_loop`：由 4 个 softmax warp 协作，从 tmem 把 S 拷到 rmem，做 `row_max`/`row_sum`/`exp2`，产出 P 写回 smem 供 `P@V` 消费；同时把 `acc_scale`（rescale 因子）经 `pipeline_sm_stats` 传给 correction。这与 u4-l1 的在线 softmax 一致，差别只在 S 住在 tmem、需用 `tcgen05.copy` 搬到 rmem。

[flash_attn/cute/flash_fwd_mla_sm100.py:2874-3136](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_mla_sm100.py#L2874-L3136) — `correction_loop`：用 `acc_scale` 修正 O（在线 softmax 的延迟 rescale），最终 `O *= 1/row_sum` 并下转 fp16，经 TMA bulk store 写回 gmem；同时计算并写 LSE（`lse = (row_max·scale_log2 + log2(row_sum))·ln2`）。

接口侧如何分发到 MLA 前向：

[flash_attn/cute/interface.py:869-885](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L869-L885) — 当 `arch//10 ∈ {10,11}` 且 `qv is not None` 时，构造 `FlashAttentionMLAForwardSm100`（而非一般 `FlashAttentionForwardSm100`）。`use_cpasync_load_KV` 在稀疏（top-k gather）或 paged 非 TMA 时开启，`has_qk` 由 `q is not None` 决定。

#### 4.2.4 代码实践

**目标**：用 absorbed MLA 形状构造输入，调用 MLA 前向，并与一个等价的「展开式」PyTorch 参考实现对比输出。需 SM100（B200/B100）GPU。

**操作步骤**（参考 [tests/cute/test_flash_attn.py:2138-2251](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/tests/cute/test_flash_attn.py#L2138-L2251) 的 `test_flash_attn_mla_absorbed`）：

```python
# 示例代码（非项目原有代码）
import torch, math
from flash_attn.cute import flash_attn_func

device = "cuda"
batch, seqlen_q, seqlen_k = 1, 512, 1024
nheads, nheads_kv, hdim, hdimv = 128, 1, 64, 512   # absorbed MLA + MQA

q  = torch.randn(batch, seqlen_q, nheads,    hdim,  device=device, dtype=torch.bfloat16).requires_grad_()
k  = torch.randn(batch, seqlen_k, nheads_kv, hdim,  device=device, dtype=torch.bfloat16).requires_grad_()
v  = torch.randn(batch, seqlen_k, nheads_kv, hdimv, device=device, dtype=torch.bfloat16).requires_grad_()
qv = torch.randn(batch, seqlen_q, nheads,    hdimv, device=device, dtype=torch.bfloat16).requires_grad_()

out, lse = flash_attn_func(q, k, v, qv=qv, causal=True, pack_gqa=True)

# 等价的展开式 PyTorch 参考实现
scale = 1.0 / math.sqrt(hdim + hdimv)
# MQA：把 kv 头广播到 nheads
k_e  = k .expand(-1, -1, nheads, -1)
v_e  = v .expand(-1, -1, nheads, -1)
s = scale * (torch.einsum("bthd,bshd->bhts", q, k_e)
           + torch.einsum("bthd,bshd->bhts", qv, v_e))   # absorbed 公式 S = QK^T + Qv V^T
# causal 掩码
mask = torch.triu(torch.full((seqlen_q, seqlen_k), float("-inf"), device=device), diagonal=seqlen_k-seqlen_q)
s = s + mask
attn = torch.softmax(s.float(), dim=-1).to(torch.bfloat16)
out_ref = torch.einsum("bhts,bshd->bthd", attn, v_e)

print("max diff =", (out - out_ref).abs().max().item())
print("lse shape =", tuple(lse.shape))   # 期望 (batch, seqlen_q, nheads)
```

**需要观察的现象**：

- `out` 与 `out_ref` 的最大误差应在 bf16 舍入量级（与 `attention_ref` 的 PyTorch 实现相比，FA4 要求误差不超过其 2 倍，见测试断言）。
- `lse.shape == (batch, seqlen_q, nheads)`（`nheads` 在最后一维，与标准注意力不同）。
- 第一次调用会触发 JIT 编译（较慢），第二次命中缓存后秒回。

**预期结果**：max diff 与 PyTorch 自身重排实现 `out_pt` 相当；`lse` 无 NaN。**无 SM100 GPU 时此实践为「待本地验证」，但可先读懂参考实现与 absorbed 公式的对应关系。**

#### 4.2.5 小练习与答案

**练习 1**：`num_hdimv_splits=2` 把 512 维 V 对半切，为什么必须这么做？不切会怎样？

**参考答案**：一条 `tcgen05.mma` 的 N/K 维有上限，512 维一次性放进一条 MMA 会超出单条指令与 tmem 列容量；对半切成 256 后每段 MMA 可独立发射，且两段可以分别流水/累加。输出 `O` 因此也对应拆成 `O0`、`O1` 两块 tmem 区。

**练习 2**：`S = Q@Kᵀ` 与 `S += Qv@Vᵀ` 是如何「共用同一块 tmem」的？请指出对应的代码标志。

**参考答案**：两者写入同一个 tmem S 累加器（`tStS`，偏移 `tmem_offset_S[stage]`）。`Q@Kᵀ` 用 `zero_init=True` 覆盖写入；`Qv@Vᵀ` 用 `zero_init=(split==0 and not has_qk)`，即正常情况下不 zero_init 而是**累加**进已有 S。这样 `S` 最终就是 `QKᵀ + QvVᵀ`，无需额外显存合并。

---

### 4.3 MLA 稀疏反向：三 kernel 协作

#### 4.3.1 概念说明

MLA 反向比前向更受限：FA4 **只为稀疏（top-k gather KV）路径**实现了反向，即 `FlashAttentionSparseMLABackwardSm100`，且当前只支持 **MQA 128 头**。它把反向拆成**三个独立 kernel** 协作完成：

1. **主循环 kernel**（`flash_bwd_mla_sm100.py`）：算 `dV`、`dS`、并把 `P`、`scaleP`、`dS` 等中间量写回 gmem。
2. **dQdQv GEMM**（`flash_bwd_mla_dq_dqv_sm100.py`）：`dQ = dS @ K` 与 `dQv = dS @ V`（K、V 按 top-k 索引 gather）。
3. **dK GEMM**（`flash_bwd_mla_dk_sm100.py`）：算 `dK`。

MLA 反向的核心公式（kernel 源码注释里写得很清楚）：

\[
dP^\top = V \cdot dO^\top,\qquad
dV \mathrel{+}= P^\top \cdot dO,\qquad
dV \mathrel{+}= dS^\top \cdot Q_v
\]

其中 `dS = P ⊙ (dP − D)`（`D` 是 softmax 行和，由前向的 LSE 复原），与标准 FA 反向（u9-l1）同源；差别在于 MLA 的 `dV` 多了一项 `dSᵀ @ Qv`（因为 V 同时参与了 `Qv@Vᵀ` 的得分与 `P@V` 的输出）。

#### 4.3.2 核心流程：三 kernel 流水

```text
前向已保存：out, lse, p, row_max, gather_kv_indices
                │
   ┌────────────┴────────────┐
   │ 1) 主循环 kernel         │  按 (q_tile, topk_kv_block) 切工作
   │   重算 dP = softmax'(S)  │  存 P、scaleP、dS 到 gmem
   │   算 dV (含 dSᵀ@Qv 项)   │  dV 直接原子累加写回
   └────────────┬────────────┘
                │ dS 已落盘
   ┌────────────┴────────────┐
   │ 2) dQdQvGemmKernel      │  dQ  = dS @ K_gathered
   │   cluster (1,2)         │  dQv = dS @ V_gathered
   │   MQA 128, top_k=2048   │  dS 经 TMA multicast
   └────────────┬────────────┘
                │
   ┌────────────┴────────────┐
   │ 3) dKGemmKernel          │  算 dK（按 gather 索引散列回写）
   └─────────────────────────┘
```

**为何要拆**：MLA 反向里 `dQ`、`dQv`、`dK` 都需要遍历完整的 top-k KV 集合，与主循环的 `dV` 工作划分不一致；把它们拆成独立 GEMM kernel，可以用各自最优的 tile 形状（如 `dQdQv` 用 `(1,2)` cluster、`128×256` MMA tile 覆盖整个 `dQv`）和独立的 CLC 调度，避免一个巨型 kernel 既难调优又易死锁。

#### 4.3.3 源码精读

主循环 kernel 的类与配置：

[flash_attn/cute/flash_bwd_mla_sm100.py:43-55](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_mla_sm100.py#L43-L55) — `FlashAttentionSparseMLABackwardSm100` 强制 `is_topk_gather=True`、`use_cpasync_load_KV=True`，构造参数与 `FlashAttentionMLAForwardSm100` 镜像（同样 `hdim/hdimv`、`qhead_per_kvhead`）。

反向数学公式（注释）与 MMA tiler：

[flash_attn/cute/flash_bwd_mla_sm100.py:162-190](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_mla_sm100.py#L162-L190) — 注释写明 `dP.T = V @ dO.T`、`dV += P.T @ dO`、`dV += dS.T @ Qv` 三式，并据此定义 `mma_tiler_VdO`、`mma_tiler_PtdOt`、`mma_tiler_dStQvt` 三个 tiler。`tile_m = qhead_per_kvhead`（MQA 下就是 128）、`tile_n = 64`。

dQdQv 辅助 GEMM：

[flash_attn/cute/flash_bwd_mla_dq_dqv_sm100.py:3-32](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_mla_dq_dqv_sm100.py#L3-L32) — 模块 docstring 写明：用 cluster `(1,2)`、mma tile `128×256` 让一个 cluster 覆盖完整 `dQv`；`dS` 经 TMA multicast 跨 CTA 共享，K/V 经 `CpasyncGatherKVManager` 按 top-k 索引 gather。典型参数 `nheads=128, hdim=64, hdim_v=512, top_k=2048`。

接口侧反向入口与能力边界：

[flash_attn/cute/interface.py:2046-2050](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L2046-L2050) — 稀疏 MLA 反向的硬性约束：`nheads_kv==1 且 qhead_per_kvhead==128`（仅 MQA 128）、`gather_kv_length % 128 == 0`、不支持 `deterministic`、不支持 `learnable_sink`、不支持 `seqused_q/k`。

[flash_attn/cute/interface.py:2502-2514](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L2502-L2514) — autograd `backward` 里，当 `qv is not None` 时调用 `_flash_attn_bwd_sparse_mla`，返回 `dq, dk, dv, dqv` 四个梯度（`shared_kv` 捷径下只返回 `dq, dk`）。

#### 4.3.4 代码实践

**目标**：阅读测试，理解 MLA 稀疏反向的能力边界与梯度校验方式。需 SM100 GPU 完整运行；无 GPU 时做源码阅读型实践。

**操作步骤**：

1. 打开 [tests/cute/test_flash_attn.py:2138](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/tests/cute/test_flash_attn.py#L2138)，定位 `test_flash_attn_mla_absorbed`。
2. 关注 [test_flash_attn.py:2184-2187](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/tests/cute/test_flash_attn.py#L2184-L2187)：当 `kv_sparsity=True` 时构造 `gather_kv_indices = torch.rand(...).argsort(-1).to(int32)`，即随机选 top-k 个 KV 块的索引。
3. 关注 [test_flash_attn.py:2283-2289](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/tests/cute/test_flash_attn.py#L2283-L2289)：反向用 `torch.autograd.grad(out, (q, k, v, qv), g)` 取四个梯度（`shared_kv` 时只取 `(q, k)`）。
4. 若有 SM100：运行 `pytest tests/cute/test_flash_attn.py -k "test_flash_attn_mla_absorbed and kv_sparsity" -x`，观察反向梯度与参考实现的 max diff。

**需要观察的现象**：

- 稀疏反向只在 `kv_sparsity=True`（即传入 `gather_kv_indices`）时被测试（`test_bwd = kv_sparsity is True`，[test_flash_attn.py:2153](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/tests/cute/test_flash_attn.py#L2153)）。
- `nheads_kv` 固定为 1（MQA），`qhead_per_kvhead=128`，否则触发 [interface.py:2046](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L2046) 的断言。

**预期结果**：`dq/dk/dv/dqv` 与参考实现 `dq_ref/...` 的误差在 bf16 容忍范围内。**无 SM100 时为「待本地验证」**，但应能说清三个 kernel 各自负责哪个梯度。

#### 4.3.5 小练习与答案

**练习 1**：为什么 MLA 的 `dV` 比标准注意力的 `dV` 多一项 `dSᵀ @ Qv`？

**参考答案**：在 absorbed MLA 里，V 同时出现在两处——得分项 `Qv@Vᵀ`（影响 S、进而影响 P）与输出项 `P@V`。标准注意力里 V 只出现在 `P@V`，所以 `dV = Pᵀ@dO` 一项；MLA 多出的 `dSᵀ@Qv` 正是得分项 `Qv@Vᵀ` 对 V 求导的结果。

**练习 2**：稀疏 MLA 反向为何拆成「主循环 + dQdQv GEMM + dK GEMM」三个 kernel，而不是合成一个？

**参考答案**：`dQ`/`dQv`/`dK` 都需要遍历完整 top-k KV 集合，工作划分与主循环（算 `dV`/`dS`，按 q×kv 块切）不同；拆开后每个 GEMM 能用各自最优的 tile（如 `dQdQv` 用 `128×256` + cluster `(1,2)` 一举覆盖整 `dQv`）与独立 CLC 调度，既好调优又降低单 kernel 死锁风险，代价是要在 gmem 中转 `dS`/`P` 等中间量。

---

## 5. 综合实践

把本讲三个模块串起来，做一个「MLA 全链路」源码追踪任务：

**任务**：以一次 `flash_attn_func(q, k, v, qv=qv, gather_kv_indices=idx, causal=True)` 调用为线索，画一张**端到端数据流图**，要求标注以下要素：

1. **形状变换**：`(hdim, hdimv)=(64,512)` 如何在 [interface.py:98](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L98) 通过校验；`softmax_scale` 如何在 [interface.py:452-456](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L452-L456) 取 `1/√576`。
2. **前向三段 MMA**：在 [flash_fwd_mla_sm100.py:2329-2377](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_mla_sm100.py#L2329-L2377) 标出 `S=Q@Kᵀ`、`S+=Qv@Vᵀ`、`Oi=P@V` 三段，并注明 S/O 各自落在 tmem 的哪一区。
3. **反向三 kernel**：在图上画出 `dV`（主循环，含 `dSᵀ@Qv` 项）、`dQ/dQv`（[flash_bwd_mla_dq_dqv_sm100.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_mla_dq_dqv_sm100.py)）、`dK`（[flash_bwd_mla_dk_sm100.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_mla_dk_sm100.py)）三个 kernel 的输入输出依赖（`dS`、`P`、`gather_kv_indices` 在 gmem 中转）。

**验收**：

- 能指着图说清「为什么 `lse.shape=(b,s,h)` 而非 `(b,h,s)`」「为什么 `dV` 比 `dK` 多一项」。
- 能在源码里定位 2CTA 配置（`cluster_shape_mn=(2,1)`）与 `num_hdimv_splits=2` 各自解决的是哪个瓶颈。

若有 SM100 GPU，额外用 4.2.4 的示例代码跑一次前向 + 反向，把 max diff 填进图里；否则标注「待本地验证」并保证图上的源码链接与行号都对得上。

## 6. 本讲小结

- **absorbed 公式**：`O = softmax(scale·(Q@Kᵀ + Qv@Vᵀ))@V`，Q/K 用 `hdim=64`、Qv/V 用 `hdimv=512`；接口通过 `qv` 参数触发，`shared_kv` 捷径可进一步丢弃 `Q@Kᵀ` 项。
- **形状与 scale**：合法 absorbed 形状为 `(64,512)` 或 `(512,512)`；默认 `scale=1/√(hdim+hdimv)`；LSE 形状为 `(batch, seqlen_q, nheads)`（nheads 连续，利于 MQA）。
- **前向 kernel**：`FlashAttentionMLAForwardSm100` 用三段 MMA（`Q@Kᵀ`、`Qv@Vᵀ`、`P@V`），`Qv@Vᵀ` 累加进同一块 tmem S；`hdimv` 对半切成 `O0/O1`；全程 2CTA + UMMA + CLC。
- **稀疏反向**：仅支持 MQA 128 + top-k gather，拆成主循环（算 `dV`/`dS`）、`dQdQvGemm`（`dQ=dS@K`、`dQv=dS@V`）、`dKGemm` 三个 kernel。
- **MLA 反向特点**：`dV` 比标准 FA 多一项 `dSᵀ@Qv`，因为 V 同时参与了得分项与输出项。
- **能力边界**：稀疏反向不支持 deterministic / learnable_sink / seqused；`gather_kv_length` 必须被 128 整除。

## 7. 下一步学习建议

- **深入 top-k gather 机制**：本讲的稀疏反向依赖 `CpasyncGatherKVManager`，建议阅读 `flash_attn/cute/topk_gather_kv.py`，它是 u10-l3（Top-k KV Gather 稀疏）的主角。
- **对比 hd256 2CTA kernel**：本讲的 MLA kernel 与 u8-l4 的 hd256 专用 kernel 都是 2CTA + tmem，但前者三段 MMA、后者单形状专用；对比两者能加深对「2CTA 三处放大（M 维、tx_count、cluster 同步）」的理解。
- **阅读 DeepSeek 原始 MLA 论文/代码**：理解 `q_pe`/`q_nope` 与位置编码的耦合，能帮你判断 FA4 的 absorbed 公式在何处做了简化（如本仓库不显式做上投影矩阵的吸收变换，而是直接让查询携带两套分量）。
- **跟进 SplitKV 与 MLA 的结合**：当前 MLA 前向 `tile_sched_args` 里 `num_splits=1` 标注了 `# todo: split_kv`，长上下文 MLA 的 SplitKV 仍是开放方向，可作为二次开发的切入点。

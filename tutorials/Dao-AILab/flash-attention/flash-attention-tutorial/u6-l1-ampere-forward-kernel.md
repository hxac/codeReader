# Ampere 前向 Kernel 全景

## 1. 本讲目标

本讲把前几讲学到的「积木」拼成一台完整的机器。读完本讲，你应当能够：

- 说出 FlashAttention 前向 kernel 的**整体数据流**：Q/K/V 如何从显存（gmem）搬到共享内存（smem）再到寄存器（rmem），MMA 与 online softmax 在哪里发生。
- 解释 **Q 常驻、K/V 流水**的分块策略，以及为什么主循环要倒序遍历 n block。
- 在源码里准确定位三段关键逻辑：Q tile 的加载与常驻、K/V 的流水主循环、online softmax 累加与 O/LSE 的存储。
- 看清 gmem / smem / rmem **三级存储边界**，以及每跨过一道边界用的是哪种拷贝原子。
- 说清 **`use_tma_O` 的架构边界**：为什么它在 Ampere 与 SM120 上都是 False、SM120 的输出为什么走 cp.async 而不是 TMA。
- 理解 **pack_gqa 代码路径**：开启 `pack_gqa` 时 Q/O/LSE 的布局如何被重排、Q 如何用 `PackGQA.load_Q` 以 gather 方式加载。

本讲以 FA4 的 `FlashAttentionForwardSm80`（Ampere 基线）为对象。它是整个 FA4 代码库里**结构最简单、最易读**的前向 kernel——没有 warp-group MMA、没有片上 tmem，只有最朴素的 cp.async + ldmatrix + 两段 mma。注意它**也是 SM120 的基类**：`FlashAttentionForwardSm120` 直接继承它（[flash_fwd_sm120.py:15-18](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm120.py#L15-L18)），所以本讲讲的 Ampere 主循环同样适用于 SM120。先把它读懂，再去读 Hopper/Blackwell kernel 就有了对照基准。

## 2. 前置知识

本讲默认你已经掌握以下概念（前几讲已建立）：

- **在线 softmax（u4-l1）**：用 `row_max`（m）与 `row_sum`（ℓ）两个寄存器张量逐块维护状态，靠重缩放因子 \(e^{m_{\text{旧}}-m_{\text{新}}}\) 修正基准迁移，最终把 `row_sum` 改写为 LSE。
- **BlockInfo（u3-l2）**：给定一个 Q tile，它要遍历的 K/V tile 范围是半开区间 `[n_block_min, n_block_max)`，主循环从 `n_block_max-1` 倒序走到 `n_block_min`。
- **cp.async 流水（u5-l1 / u5-l2）**：Ampere 上 gmem→smem 用 `cp.async`（128-bit 单次搬运），靠 `cp_async_commit_group` / `cp_async_wait_group(N)` 按「提交组计数」跟踪完成。

几个**本讲第一次出现、需要先点名的术语**：

| 术语 | 含义 |
|---|---|
| **gmem / smem / rmem** | 显存（global memory）/ 共享内存（shared memory）/ 寄存器（register memory），GPU 片内/片外的三级存储层次。 |
| **MMA** | Matrix Multiply-Accumulate，张量核心矩阵乘加指令。Ampere/SM120 上是 `MmaF16BF16Op`，输入 fp16/bf16，累加器 fp32。 |
| **ldmatrix** | Ampere/Hopper 的 smem→rmem 加载指令，配合 swizzle 布局把一个 8×8×16b 矩阵块喂给 MMA。 |
| **TiledMma** | CuTeDSL 里「一整块 MMA」的抽象，把指令级 MMA 平铺到一个 warp 甚至多个 warp 上。 |
| **acc_S / acc_O** | 两个 fp32 寄存器累加器：`acc_S` 存注意力分数 \(S=QK^\top\)，`acc_O` 存输出 \(O=\text{softmax}(S)V\) 的累加值。 |
| **GQA / MQA** | Grouped/Multi-Query Attention：Q 头数多于 KV 头数（`qhead_per_kvhead = num_heads // num_heads_kv`），多组 Q 共享同一组 KV。 |
| **pack_gqa** | 把 `qhead_per_kvhead` 折叠进 seqlen 维的布局技巧，让一个 KV tile 被同 CTA 内的多个 Q 头复用。 |

> ⚠️ 关于流水线：本讲依赖的 u5-l1 讲的是 Hopper/Blackwell 的 `PipelineStateSimple`（用 phase 奇偶性 + mbarrier 握手）。**Ampere kernel 不用那套**，它用的是更老的 cp.async 计数器模型——`commit_group`/`wait_group(N)` 跟踪在飞搬运数，加一个手写的环形下标 `smem_pipe_read`/`smem_pipe_write`。两者解决的是同一个问题（让搬运与计算重叠），只是机制不同。本讲 4.2 节会专门讲清楚 Ampere 的版本。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [`flash_attn/cute/flash_fwd.py`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py) | **主角**。包含 `FlashAttentionForwardBase`（通用配置与 epilogue）与 `FlashAttentionForwardSm80`（Ampere/SM120 专用：MMA 选择、共享存储、kernel 主循环、`compute_one_n_block`、`apply_score_mod`、pack_gqa 的 `__init__` 布局重排）。 |
| [`flash_attn/cute/ampere_helpers.py`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/ampere_helpers.py) | Ampere 的三个积木：`get_smem_layout_atom`（swizzle 布局）、`gemm`（QK^T 内层循环）、`gemm_rs`（PV 内层循环）。> 注：`flash_fwd.py` 第 26 行用 `import ... ampere_helpers as sm80_utils` 给它起了别名，代码里写的 `sm80_utils.gemm` 实际就是这里的函数。 |
| [`flash_attn/cute/pack_gqa.py`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/pack_gqa.py) | `pack_gqa_layout`（把 `qhead_per_kvhead` 折进 seqlen 维）与 `PackGQA`（`load_Q` / `store_O` / `store_LSE` 的 gather/scatter 实现）。本版本起 Ampere/SM120 前向的 pack_gqa 主路径由它支撑。 |
| [`flash_attn/cute/softmax.py`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/softmax.py) | `Softmax` 类：`reset` / `online_softmax` / `rescale_O` / `finalize`，是主循环里的数值核心（u4-l1 详述）。 |
| [`flash_attn/cute/named_barrier.py`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/named_barrier.py) | `NamedBarrierFwd.Epilogue` 等：epilogue 里 smem 读写同步用的编号屏障。 |

## 4. 核心概念与源码讲解

### 4.1 Q tile 加载与常驻（含 pack_gqa 路径）

#### 4.1.1 概念说明

前向 kernel 的基本分块单位是 **Q tile**（形状 `tile_m × head_dim`）。一个 thread block（CTA）负责**一个** Q tile，任务是对它算完整的一行注意力。

关键策略是「**Q 常驻、K/V 流水**」：

- **Q tile 只加载一次**，在主循环开始前从 gmem 搬到 smem（`sQ`），整个内层循环期间反复读它，不再回 gmem。
- **K/V tile 一块接一块地流过**，每来一块做一次 `QK^T` 和 `PV`，算完就丢，下一块覆盖同一块 smem。

为什么要让 Q 常驻而让 K/V 流动？因为注意力对一个 Q tile 要乘遍**所有** K/V tile（O(N) 次），而每个 K/V tile 只服务**当前** Q tile（O(1) 次）。把使用频率高的 Q 留在片上，使用频率低的 K/V 流过，是典型的「**访问局部性 → 复用**」设计。

> 关于 `Q_in_regs`：FA4 还有一个开关，把 Q 进一步从 smem 提升到寄存器（`Q_in_regs=True`），此时 `sQ` 与 `sV` 复用同一块 smem。默认 `Q_in_regs=False`，Q 常驻 **smem**，每个 n block 再用 ldmatrix 把所需片段读到 rmem 参与 MMA。本讲按默认路径讲解。

**pack_gqa：Q 加载的第二条路径。** 当 Q 头数多于 KV 头数（GQA/MQA）时，多个 Q 头共享同一组 KV。`pack_gqa` 的做法是在 kernel 启动前把 `qhead_per_kvhead` 个 Q 头**折叠进 seqlen 维**：于是「一个 `tile_m` 行的 Q tile」其实同时包含了 `qhead_per_kvhead` 个 Q 头的若干行，它们读的是**同一块** K/V。这样一块 K/V 的搬运能被同 CTA 内的多个 Q 头复用，显著提升 GQA 吞吐。代价是 Q 的 gmem→smem 搬运从「连续 `local_tile` 拷贝」变成「按行的 gather 拷贝」（详见 4.1.3）。

#### 4.1.2 核心流程

标准路径（非 pack_gqa）：

```
[Prologue：Q tile 加载]
  gmem(Q tile)  --cp.async(CopyG2SOp, 128-bit)--> smem(sQ)
  cp_async_commit_group()          # 提交本次搬运
  preprocess_Q(): wait_group(...)  # 等 Q 落到 smem

[内层循环里 Q 如何被用]
  每个 n block：
    smem(sQ 片段) --ldmatrix 8x8--> rmem(tSrQ)   # smem→rmem
    rmem(tSrQ), rmem(tSrK) --gemm--> rmem(acc_S)  # S = Q·K^T
  （Q 的 smem 内容全程不动，只是反复被 ldmatrix 读）
```

pack_gqa 路径（本版本新增）：prologue 里把 `self.load_Q(...)` 换成 `PackGQA(...).load_Q(...)`，它不切连续的 `local_tile`，而是为 tile 里的每一行**算出对应的 Q 头 + seqlen 位置**，按行 gather 进 `sQ`；`sQ` 之后的复用方式与标准路径完全一致。注意 Q 的加载发生在 **prologue**（主循环之前），而它被消费（读进 rmem）发生在**主循环每个 n block**。

#### 4.1.3 源码精读

**`__init__` 里的 pack_gqa 布局重排。** 这是本版本对 Ampere/SM120 前向最关键的新增：当 `pack_gqa=True` 时，在 kernel 构造期就把 mQ/mO/mLSE 的布局折叠好：

[flash_fwd.py:676-681](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L676-L681) — `mQ = pack_gqa_layout(mQ, qhead_per_kvhead, nheads_kv, head_idx=2)`（mO 同理），mLSE 用 `head_idx=1`。折叠后 Q/O 的形状从 `(seqlen_q, headdim, nheads, batch)` 变成 `((qhead_per_kvhead, seqlen_q), headdim, nheads_kv, batch)`——头维由 `nheads` 缩成 `nheads_kv`，多出的 `qhead_per_kvhead` 维并进 seqlen。正因如此，后面读静态序列长度时要用 `mQ.shape[0][1]`（取折叠后的 seqlen 子维）：

[flash_fwd.py:800-808](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L800-L808) — `seqlen_q_static = mQ.shape[0] if not pack_gqa else mQ.shape[0][1]`。同理 [flash_fwd.py:822](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L822) 的 `num_head_kv = num_head if pack_gqa else num_head // qhead_per_kvhead`：pack_gqa 下头已折进 seqlen，work tile 的 head 索引直接就是 KV 头索引。

**Q tile 的 gmem 切片（仅标准路径）。** 在 `kernel` 开头，标准路径用 `local_tile` 从当前 Q 头切出当前 CTA 负责的 Q 块；pack_gqa 路径则不切（改由 `PackGQA.load_Q` 内部按行寻址）：

[flash_fwd.py:830-833](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L830-L833) — `if const_expr(not self.pack_gqa): gQ = cute.local_tile(mQ_cur, blkQ_shape, (m_block, 0))`。注意 `gQ` 只在非 pack_gqa 分支里定义；K/V 的 `gK`/`gV` 两条路径都切（它们本来就和 Q 头数无关）。

**标准路径的 Q 加载函数** `load_Q`，用 `cp.async`（底层 atom `CopyG2SOp`）把 `gQ` 搬到 `sQ`：

[flash_fwd.py:456-480](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L456-L480) — `load_Q` 主体。其中谓词 `t0QcQ[0,m,0][0] < seqlen - block*tile_m - tQcQ[0][0]` 处理序列末尾不足一个 `tile_m` 的情况；`check_hdim_oob` 分支处理 `head_dim` 不是 16 倍数时的列越界。注释明确写道「无需清空 sQ，因为我们只会写出有效输出」。

**Prologue 里的加载分支**——本版本新增的 pack_gqa 分支：

[flash_fwd.py:962-968](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L962-L968) — `if const_expr(not self.pack_gqa): self.load_Q(gmem_thr_copy_Q, gQ, sQ, m_block, ...)`；`else` 构造 `PackGQA(tile_m, tile_hdim, check_hdim_oob, qhead_per_kvhead)` 并调 `pack_gqa.load_Q(mQ_cur, sQ, gmem_tiled_copy_Q, tidx, m_block, seqlen.seqlen_q)`。两条分支随后都 `cp_async_commit_group()` 把搬运打包成可跟踪的组。

**PackGQA.load_Q 如何 gather。** 它的核心是按行计算 gmem 指针：把 tile 内的线性行号 `idx` 拆成「真实 seqlen 位置」与「组内第几个 Q 头」：

[pack_gqa.py:122-140](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/pack_gqa.py#L122-L140) — `compute_ptr`：`idx = block*tile_m + row`；`m_idx = idx // qhead_per_kvhead`（seqlen 位置）；`h_idx = idx - m_idx*qhead_per_kvhead`（组内 Q 头）；用 `utils.elem_pointer(tensor, ((h_idx, m_idx),))` 取出该行指针。随后 [pack_gqa.py:142-185](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/pack_gqa.py#L142-L185) 的 `load_Q` 用 `shuffle_sync` 把指针广播到同组线程，再按行 `cute.copy(...)` 进 `sQ`——仍是 cp.async 原子，只是源地址是按行算出来的 gather 地址。结果：`sQ` 里连续排布的 `tile_m` 行，恰好对应 `qhead_per_kvhead` 个 Q 头的若干 seqlen 行。

**等待 Q 落地（两条路径共用）**：

[flash_fwd.py:970-996](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L970-L996) — `preprocess_Q` 与 K/V 预取交错。注意末尾 `if const_expr(not self.Q_in_regs): preprocess_Q()`：默认路径下这里把 Q 搬运的 commit group 等「足够旧」（`wait_group(num_stages*2-1)`），确保 Q 已在 smem。

**Q 在 smem 的布局**。`FlashAttentionForwardSm80._get_smem_layout_atom` 让 K 复用 Q 的布局，V 用 `head_dim_v` 版本：

[flash_fwd.py:580-586](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L580-L586) — `sK_layout_atom = sQ_layout_atom`。布局本体由 `ampere_helpers.get_smem_layout_atom` 给出。

[ampere_helpers.py:8-31](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/ampere_helpers.py#L8-L31) — swizzle 布局：按 `bytes_per_row` 选 128/64/32/16 字节的 `smem_k_block_size`，再配 `(swizzle_bits, swizzle_base)` 消除 bank conflict，让后续 ldmatrix 能高效读取。

#### 4.1.4 代码实践

**实践目标**：确认 Q tile 在 prologue 只加载一次、其 smem 缓冲区内层循环里被反复读；并对照标准路径与 pack_gqa 路径在「Q 如何进 sQ」上的差异。

**操作步骤（源码阅读型）**：

1. 打开 `flash_fwd.py`，在 `kernel` 内搜索 `sQ` 的所有出现。
2. 统计对 `sQ`（或 `storage.sQ`）的**写**（`load_Q` / `pack_gqa.load_Q` / `cute.copy(..., sQ...)`）与**读**（partition_S / ldmatrix）的次数。
3. 对比 `sK`、`sV`：它们带不带 `smem_pipe_write` / `smem_pipe_read` 下标？带下标意味着有多个槽（环形缓冲），不带意味着单槽。
4. 对比 [flash_fwd.py:963-964](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L963-L964)（标准 `self.load_Q(gQ, sQ, ...)`，输入是连续切片 `gQ`）与 [flash_fwd.py:965-967](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L965-L967)（`PackGQA.load_Q(mQ_cur, sQ, ...)`，输入是整块 `mQ_cur`，由它内部 gather）。

**需要观察的现象**：`sQ` 是单槽（无 stage 维度），写入只发生在 prologue；`sK`/`sV` 在 `_setup_attributes` 里被 tile 成 `(tile_n, head_dim, num_stages)`，带 stage 维度——这就是「Q 常驻、K/V 多级流水」在数据结构上的体现。两条 Q 加载路径**最终都写进同一个单槽 `sQ`**，区别只在写之前的寻址方式。

**预期结果**：你能用一句话总结「Q 在 smem 里只有一份，K/V 各有 `num_stages` 份在环形缓冲里轮转」；并能说出 pack_gqa 下 Q 是 gather 进 sQ、标准路径下是连续 `local_tile` 进 sQ。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `sQ` 不需要 `num_stages` 维度，而 `sK`/`sV` 需要？
**答案**：Q 在整个内层循环里被同一个 CTA 反复读，加载一次足矣，无需隐藏其延迟；K/V 每算完一块就换下一块，需要 `num_stages` 份缓冲让「下一块的搬运」与「当前块的计算」重叠，否则每次都得等 HBM。

**练习 2**：`Q_in_regs=True` 时，Q 还在 smem 里吗？
**答案**：仍在，但只是为了过渡——`preprocess_Q` 里 `cute.copy(smem_thr_copy_Q, tSsQ, tSrQ_copy_view)` 把 Q 从 smem 拷进寄存器 `tSrQ`，之后内层循环直接用寄存器里的 Q，`sQ` 这块 smem 转手让给 `sV` 复用（见 `_get_shared_storage_cls` 返回的 `SharedStorageSharedQV`）。这是用更多寄存器换更省 smem 的取舍。

**练习 3**：开启 pack_gqa 后，`seqlen_q_static` 为什么从 `mQ.shape[0]` 改成 `mQ.shape[0][1]`？
**答案**：因为 `pack_gqa_layout` 把 `qhead_per_kvhead` 折进了 seqlen 维，折叠后 `mQ.shape[0]` 不再是标量 seqlen，而是二元组 `(qhead_per_kvhead, seqlen_q)`（见 [pack_gqa.py:23](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/pack_gqa.py#L23)）。真正用于 BlockInfo/掩码的序列长度是其第二个子维 `mQ.shape[0][1]`。

---

### 4.2 K/V 流水遍历主循环

#### 4.2.1 概念说明

主循环是前向 kernel 的心脏。对一个固定的 Q tile，它要遍历 `n_block` 从 `n_block_max-1` 倒序到 `n_block_min`（u3-l2 已讲过这个范围如何由因果/滑窗/SplitKV 决定）。每遍历一块 K/V，做两件事：

1. **算分数**：\(S = Q K^\top\)（其实是带 `softmax_scale` 的，u4-l2 讲过缩放在哪一步）。
2. **累加输出**：\(O \mathrel{+}= P V\)，其中 \(P\) 是对 \(S\) 在线 softmax 后的（被 rescale 过的）概率。

为了让「下一块 K/V 的搬运」和「当前块的两段 MMA + softmax」重叠，Ampere 用 **cp.async 计数器流水线**：

- 每个 stage 在 smem 里有一份 `sK[stage]` / `sV[stage]`。
- 搬运用 `cp.async` 发出后 `commit_group()`，完成由 `wait_group(N)` 询问「在飞的组还剩多少」。
- `smem_pipe_read` / `smem_pipe_write` 是手写的环形下标，由 `advance_pipeline` 取模推进。

这套机制和 u5-l1 的 `PipelineStateSimple` 思想一致（环形缓冲 + 满/空握手），只是把 mbarrier 换成了 cp.async 计数器——这是 Ampere 硬件没有 mbarrier 的体现。

> **倒序遍历**为什么？因为 `n_block_max-1` 是序列末尾的 K/V tile，它最容易受序列长度（seqlen residue）掩码影响；把它单拎出来做带掩码的「首迭代」，剩下的迭代能编译成无谓词的快路径。这与 u3-l1 讲的「块级跳过 + 元素级掩码协同」一致。

#### 4.2.2 核心流程

主循环被拆成**三段**（编译期分支特化，减少谓词开销）：

```
# 第一段：首迭代（带 seqlen 掩码，is_first_n_block=True）
compute_one_n_block(n_block_max-1, is_first_n_block=True, mask_seqlen=True)

# 第二段：因果/滑窗掩码段（仅 is_causal or is_local 时）
for n in [n_block_min_causal_local_mask, n_block_max-1):   # 倒序
    compute_one_n_block(n, mask_seqlen=True)

# 第三段：无掩码段（中间的完整块）
for n in [n_block_min, n_block_min_causal_local_mask):
    compute_one_n_block(n, mask_seqlen=False)
```

而**单个 n block 内部**（`compute_one_n_block`）的数据流是：

```
sync()                              # 等当前 stage 的 K 到 smem
load_V_next()                       # 预取下一块 V（commit_group）
gemm(Q, K)        → acc_S           # S = Q·K^T   (rmem, fp32)
[apply_score_mod]                   # 若有 score_mod（可选）
[mask_fn]                           # 因果/seqlen 掩码（改 acc_S 为 -inf）
online_softmax(acc_S) → row_scale   # 更新 row_max/row_sum，acc_S 变成 exp2(P)
rescale_O(acc_O, row_scale)         # 把旧的 O 累加值按新基准缩放
rP = acc_S.to(dtype)                # 物化 P 到寄存器（fp16/bf16）
gemm_rs(P, V)     → acc_O           # O += P·V    (rmem, fp32)
```

注意两个 gemm 之间的**嵌套**：第一段 `gemm`（Q@K^T）算完分数、做完 softmax rescale，才进入第二段 `gemm_rs`（P@V）。在它们之间穿插了「预取下一块 V」「预取下一块 K」——这正是流水线把搬运藏进计算的手法。

#### 4.2.3 源码精读

**主循环三段**在 `kernel` 末尾：

[flash_fwd.py:1026-1063](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L1026-L1063) — 三段循环。第一段调 `compute_one_n_block(..., is_first_n_block=True, mask_seqlen=True)`；第二段 `for n_tile in range(n_block_max-1-n_block_min_causal_local_mask)` 走因果/滑窗块，`mask_seqlen=True`；第三段 `for n_tile in range(n_block, unroll=1)` 走无掩码块，`mask_seqlen=False`。每段结束都 `smem_pipe_read = self.advance_pipeline(smem_pipe_read)` 推进环形下标。

**范围与起点的计算**：

[flash_fwd.py:790-814](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L790-L814) — 用 `BlockInfo.get_n_block_min_max(seqlen, m_block)` 得到 `[n_block_min, n_block_max)`（809 行），再 `n_block = max(n_block_max - 1, 0)`（814 行）。对变长的「废 tile」（batch 越界），靠 clamp 到 0 + 谓词自保。

**环形下标推进**：

[flash_fwd.py:452-453](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L452-L453) — `advance_pipeline` 就是 `index+1 if index < num_stages-1 else 0`，即手写的 `(index+1) % num_stages`。

**单个 n block 的核心**——`compute_one_n_block`：

[flash_fwd.py:1119-1121](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L1119-L1121) — `sync()`：`cp_async_wait_group(num_stages*2-2)` 再 `barrier()`。乘 2 是因为每个 stage 有 K 和 V 两个 commit group，等的是「当前 stage 的数据已落地」。

[flash_fwd.py:1130-1153](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L1130-L1153) — `load_V_next()` 预取下一块 V；随后 `sm80_utils.gemm(thr_mma_qk, acc_S, tSrQ, tSrK, tSsQ, tSsK[...stage...], ...)` 算 \(S=QK^\top\)。`tSsK[..., smem_pipe_read]` 指明读哪个 stage 的 K。

[flash_fwd.py:1170-1199](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L1170-L1199) — `load_K_next()` 预取下一块 K；`online_softmax` + `rescale_O`；`rP = acc_S.to(dtype)`；最后 `sm80_utils.gemm_rs(thr_mma_pv, acc_O, tOrP, tOrVt, tOsVt[...stage...], ...)` 算 \(O\mathrel{+}=PV\)。`gemm_rs` 的 "rs" 指 A（即 P）在**寄存器**里，B（即 V）从 smem 来。

**两段 GEMM 的内层循环**在 `ampere_helpers`：

[ampere_helpers.py:34-83](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/ampere_helpers.py#L34-L83) — `gemm`：沿 K 维（head_dim）展开，每步先 `copy` 下一片 smem→rmem，再 `cute.gemm(...)` 累加，是典型的「软流水」——load 与 mma 交错。

[ampere_helpers.py:86-103](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/ampere_helpers.py#L86-L103) — `gemm_rs`：A 在寄存器（不再 smem→rmem），只搬 B（V），同样沿 K 维软流水。

**MMA 的选择**（Ampere/SM120 特化）：

[flash_fwd.py:588-599](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L588-L599) — `MmaF16BF16Op(dtype, Float32, (16,8,16))`：指令级 MMA 是 16×8×16，输入 fp16/bf16、累加 fp32。用 `num_threads//32` 个 warp 平铺，permutation 把 `tile_m` 维分给各 warp。这正是 SM120 文件注释里说的「SM120 uses the same SM80-era MMA instructions」。

#### 4.2.4 代码实践

**实践目标**：跟踪单个 n block 内「搬运」与「计算」的交错，体会流水线如何把 HBM 延迟藏起来。

**操作步骤**：

1. 读 `compute_one_n_block`（[flash_fwd.py:1091-1201](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L1091-L1201)）。
2. 列出其中所有 `cp_async_commit_group()` 调用点（在 `load_V_next`、`load_K_next` 里）。
3. 列出所有 `cp_async_wait_group(...)` / `barrier()` 调用点（在 `sync()` 及 `num_stages==1` / `>1` 分支里）。
4. 把它们按出现顺序排成一条时间线。

**需要观察的现象**：每个 `gemm` / `gemm_rs` 之前都有一次 `wait`（等数据），每个 `gemm` / `gemm_rs` 期间或紧邻处有一次 `commit`（发出下一批搬运）。搬运和计算是穿插的，不是「全搬完再全算」。

**预期结果**：你能画出形如 `load_V_next → gemm_QK → load_K_next → softmax → gemm_PV` 的交错序列，并指出哪一步在「等」、哪一步在「发」。

**待本地验证**：若你能在 GPU 上设 `CUTE_DSL_KEEP_PTX=1` 导出 PTX，可进一步在 PTX 里数 `cp.async` 与 `mma.sync` 指令的交错密度，但本实践不要求一定跑通。

#### 4.2.5 小练习与答案

**练习 1**：主循环为什么把遍历拆成「首迭代 / 因果掩码段 / 无掩码段」三段，而不是一个统一的循环？
**答案**：为了让编译器**特化**出无谓词的快路径。无掩码段里没有 `mask_seqlen` 分支，MMA 全速跑；只有少数边界块需要带掩码的慢路径。这正是 u3-l1 讲的「用 `const_expr` 在编译期裁剪分支」在前向主循环里的体现。

**练习 2**：`gemm`（Q@K^T）和 `gemm_rs`（P@V）有什么结构区别？
**答案**：`gemm` 的两个操作数 A(Q)、B(K) 都从 smem 读（除非 `Q_in_regs`）；`gemm_rs` 的 A(P) 已经在寄存器（`tOrP`），只有 B(V) 从 smem 读，所以 `gemm_rs` 少了一路 smem→rmem 搬运（见 [ampere_helpers.py:96-101](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/ampere_helpers.py#L96-L101)）。

**练习 3**：为什么 `sync()` 里 `wait_group` 的参数是 `num_stages*2 - 2` 而不是 `num_stages - 1`？
**答案**：因为每个 stage 同时发出 K 和 V 两次 `commit_group`，在飞的组数是 stage 数的 2 倍。`wait_group(N)` 表示「等到在飞组数 ≤ N」，要等当前 stage 的 K 和 V 都落地，就要按 2 倍计数换算。

---

### 4.3 在线 softmax 累加与 O/LSE 存储

#### 4.3.1 概念说明

主循环跑完所有 n block 后，`acc_O` 里累加的是 \(O = \sum_j P_j V_j\)，但还没除以 `row_sum`；`row_sum`/`row_max` 还是「以 2 为底的对数域」中间态。**收尾（finalize）与 epilogue** 要做两件事：

1. **归一化 O**：\(O \leftarrow O / \ell\)，其中 \(\ell\) 是 `row_sum`。
2. **写回 O 和 LSE**：把 fp32 的 `acc_O` 转成 fp16/bf16 存回 gmem；把 `row_sum` 改写成 LSE 存回 gmem（供反向与 SplitKV 合并用）。

数学上，LSE 定义为：

\[
\text{LSE} = \ln\!\left(\sum_j e^{S_j \cdot s}\right) = m\cdot s + \ln(\ell)
\]

其中 \(s\) 是 `softmax_scale`，\(m\) 是 `row_max`，\(\ell\) 是 `row_sum`（都按 \(e^2\) 域换底过）。`finalize` 用 `log2` + 换底因子 `LN2` 把它从 log2 域换回自然对数。

> 关于输出回写：O 的回写走 TMA 还是非 TMA，由编译期常量 `self.use_tma_O` 决定。本版本起该常量的边界发生了变化——它现在排除了 SM120。完整的边界剖析与 SM120 回退原因见 **4.4 节**；本节先按「Ampere/SM120 都走非 TMA 路径」来读 epilogue 主体。

#### 4.3.2 核心流程

```
# 主循环结束后
row_scale = softmax.finalize()        # 1/row_sum（含零/NaN 安全化），同时把 row_sum 改写成 LSE
softmax.rescale_O(acc_O, row_scale)   # O *= 1/row_sum，完成归一化

# epilogue
rO = acc_O.to(dtype)                  # fp32 → fp16/bf16
barrier(Epilogue)                     # 等所有线程读完 V（rmem）
copy(rO → sO)                         # rmem → smem（用 smem store atom）
[use_tma_O ? TMA 路径 : 非 TMA 路径]   # 见 4.4
write mLSE[batch, head, q_idx] = LSE  # 每行由列 0 线程写出
```

#### 4.3.3 源码精读

**finalize + rescale_O（在 kernel 末尾调用）**：

[flash_fwd.py:1067-1068](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L1067-L1068) — `row_scale = softmax.finalize(); softmax.rescale_O(acc_O, row_scale)`。

**finalize 的数学**：

[softmax.py:193-227](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/softmax.py#L193-L227) — 先对 `row_sum` 做跨 quad 归约（主循环里每步只做了 warp 内 4 线程归约，这里补全跨 quad）；再算 `row_scale = rcp_approx(row_sum)`（对 0/NaN 行换成 `rcp(1.0)` 防 NaN）；最后把 `row_sum[r]` 改写成 `(row_max*scale_log2 + log2(row_sum)) * LN2`，即 LSE。

**online_softmax（主循环里每个 n block 调一次）**：

[softmax.py:127-190](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/softmax.py#L127-L190) — 三步递推：更新 `row_max`、算 `row_scale = exp2((row_max_prev - row_max_cur)*scale_log2)`、用 `exp2` 把 `acc_S` 转成概率并累加进 `row_sum`。首块（`is_first`）直接置 `row_scale=1`。这与 u4-l1 的推导一一对应。

**compute_one_n_block 里调用 online_softmax**：

[flash_fwd.py:1181-1185](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L1181-L1185) — `row_scale = softmax.online_softmax(acc_S, is_first=is_first_n_block, ...)`；`rescale_O(acc_O, row_scale)`；`rP.store(acc_S.load().to(dtype))`。三步连读，正好是「更新 softmax 状态 → 修正 O 基准 → 物化 P」。

**epilogue（O 与 LSE 回写）**：

[flash_fwd.py:331-449](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L331-L449) — 整个 `epilogue` 方法。

  - [flash_fwd.py:348-360](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L348-L360)：`rO = acc_O.to(dtype)`，先 `barrier(Epilogue)` 确保所有线程读完 V，再用 smem store atom 把 `rO` 写到 `sO`。
  - [flash_fwd.py:362-365](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L362-L365)：构造一个 `PackGQA` 实例（供 LSE/O 的 pack_gqa 分支使用）。
  - [flash_fwd.py:367-390](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L367-L390)：LSE 回写。标准路径注意 `if taccOcO[0][1] == 0`——**只有对应列 0 的线程**写出每行的 LSE，避免重复写；pack_gqa 路径委托给 `pack_gqa.store_LSE`（390 行）。
  - [flash_fwd.py:398-449](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L398-L449)：**O 回写的 TMA/非 TMA 分支**。`if const_expr(self.use_tma_O)`（398 行）走 TMA store 路径（fence + `cp.async.bulk`）；`else`（418 行起）走非 TMA 路径：`barrier(Epilogue)` 等 smem 写完 → `autovec_copy(sO → rO)` 宽向量读出 → `gmem_tiled_copy_O` 把 `rO` 写回 gmem（带 `check_hdim_v_oob` 与序列末尾谓词）。两条路径下都再分 pack_gqa 与否：标准路径用 `local_tile` + 谓词 copy（428-447 行），pack_gqa 委托给 `pack_gqa.store_O`（449 行）。详细的架构边界见 4.4。

**Epilogue 屏障**：

[named_barrier.py:6-7](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/named_barrier.py#L6-L7) — `NamedBarrierFwd.Epilogue = 1`（编号 1，因为 0 号留给 `sync_threads()`）。epilogue 里多次用它：第一次「等所有线程读完 V（rmem）才能开始写 sO」，非 TMA 路径下第二次「等 sO 写完才能从 sO 读回 rmem 再写 gmem」。这正是 u5-l3 讲的「命名屏障管线程到齐」。

#### 4.3.4 代码实践

**实践目标**：验证 finalize 产出的 LSE 与输出的 O 在数学上自洽——即 `exp(lse)` 应等于该行 softmax 的归一化分母。

**操作步骤（源码阅读 + 计算）**：

1. 读 `finalize`（[softmax.py:193-227](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/softmax.py#L193-L227)），写出它最终 `row_sum[r]` 的表达式。
2. 读 `online_softmax`（[softmax.py:127-190](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/softmax.py#L127-L190)），确认主循环结束时 `acc_O` 里存的是 \(\sum_j \text{row\_scale}_j \cdot P_j V_j\)（已按最终 `row_max` 对齐但**未除以** `row_sum`）。
3. 用符号推导证明：`finalize` 后 `acc_O * row_scale` 恰好等于 \(\text{softmax}(S)V\)。

**需要观察的现象**：`row_scale = 1/row_sum`，所以 `acc_O * (1/row_sum)` 正好补上归一化分母；而 `lse = row_max*scale + ln(row_sum)` 恰好是 \(\ln\sum_j e^{s\cdot S_j}\)。

**预期结果**：你得到一条等式链：`O_out = acc_O / row_sum = (Σ_j P_j V_j) / row_sum = softmax(S)V`，且 `exp(lse) = row_sum`。这与 u2-l1 讲的「`exp(lse)` 可还原 softmax 分母」完全吻合。

**待本地验证**：可选地，在 GPU 上跑一次 `flash_attn_func(..., return_lse=True)`，取某一行 `(b, h, q)`，用 `lse[b,h,q]` 与同一行的 `O[b,h,q]`、参考 `softmax` 对比，确认 `attn(b,h,q,:).sum() ≈ exp(lse[b,h,q])` 的相对关系（注意 softmax 后求和=1，这里验证的是 LSE 的定义而非求和）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 finalize 要先 `warp_reduce(row_sum, width=4)` 再算 `row_scale`？
**答案**：主循环里的 `online_softmax` 每步只在「4 线程组」内做了 `fadd_reduce`（见 [softmax.py:159](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/softmax.py#L159) 的 `warp_reduction_max(threads_in_group=4)`），跨 quad 的归约被推迟到 finalize 一次性补做。把跨组归约攒到最后做一次，比每步都做省指令。

**练习 2**：epilogue 里 LSE 为什么只让「列 0 的线程」写？
**答案**：一个 MMA 累加器行被多个线程共同持有（tile 分布），但 LSE 是**每行一个标量**。若所有持有该行的线程都写会重复写同一地址。规定只有「对应列 0」的那个线程写，保证每行恰好写一次。

---

### 4.4 use_tma_O 的架构边界与 SM120 回退

#### 4.4.1 概念说明

O 的回写有两条物理路径：**TMA 路径**（`cp.async.bulk` 整块从 smem 直送 gmem，靠 `fence_view_async_shared` + mbarrier 保证可见）与**非 TMA 路径**（rmem→smem→rmem→gmem 的 universal copy，逐行带谓词拷贝）。前者快、后者通用。走哪条由编译期常量 `self.use_tma_O` 决定。

本版本对这个常量的**取值边界做了一次关键修正**：

- 旧版本：`self.use_tma_O = self.arch >= Arch.sm_90`——只要「不低于 Hopper」就启用 TMA。
- 新版本：`self.use_tma_O = Arch.sm_90 <= self.arch < Arch.sm_120`——额外要求**严格小于 SM120**。

为什么要排除 SM120？因为 SM120（Blackwell 消费级 SKU，如 GeForce / DGX Spark）**复用 `FlashAttentionForwardSm80` 这个 kernel**（`FlashAttentionForwardSm120` 是它的子类，见 [flash_fwd_sm120.py:15-18](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm120.py#L15-L18)），但它的硬件并不能用 Sm80 kernel 里写的那条 TMA store epilogue。旧版本 `>= sm_90` 在 SM120（arch=120）上会算成 True，错误地把 SM120 路由进 TMA 路径；新版本 `< sm_120` 把它挡住，强制走非 TMA（cp.async / universal copy）输出。Ampere（arch=80）本来就在下界之外（`sm_90 <= 80` 为假），依然走非 TMA——这与 Ampere 硬件没有 TMA 的事实一致。

#### 4.4.2 核心流程

关键在于**评估 `use_tma_O` 时的 `self.arch` 是真实 GPU 架构**：

```
构造顺序（SM120）：
  FlashAttentionForwardBase.__init__   →  self.arch = BaseDSL._get_dsl().get_arch_enum()  # = sm_120
  FlashAttentionForwardSm80.__init__   →  self.use_tma_O = (sm_90 <= sm_120 < sm_120)      # = False ✓
  FlashAttentionForwardSm120.__init__  →  self.arch = Arch.sm_80  # 之后再强制成 sm_80（用 SM80 code path）
```

也就是说，`use_tma_O` 在 `self.arch` 还没被 SM120 子类改写成 `sm_80` 之前、以**真实 arch=120** 完成求值。新边界 `120 < 120` 为假，TMA 被关闭。若是旧边界 `120 >= 90` 则为真，就会错误启用 TMA——这正是这次修正堵住的漏洞。

```
# epilogue 里的分支
if const_expr(self.use_tma_O):   # Hopper(90) / Blackwell 数据中心(100,110) → 真
    TMA 路径：fence_view_async_shared → barrier_arrive → cp.async.bulk store
else:                            # Ampere(80) / SM120(120) → 假
    非 TMA 路径：barrier → autovec_copy(sO→rO) → gmem_tiled_copy_O 谓词写回
```

#### 4.4.3 源码精读

**`use_tma_O` 的边界**（本版本核心改动）：

[flash_fwd.py:658](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L658) — `self.use_tma_O = Arch.sm_90 <= self.arch < Arch.sm_120`。对比旧版的 `self.arch >= Arch.sm_90`，新增的上界 `< Arch.sm_120` 正是把 SM120 排除出 TMA 路径的那一刀。

**`self.arch` 的来源**（说明为何评估时是真实架构）：

[flash_fwd.py:117](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L117) — `self.arch = BaseDSL._get_dsl().get_arch_enum()`，在基类 `__init__` 最前面就读取真实 GPU 架构。

**SM120 子类的「先评估、后改写」**：

[flash_fwd_sm120.py:15-18](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm120.py#L15-L18) — `class FlashAttentionForwardSm120(FlashAttentionForwardSm80)`：`__init__` 先 `super().__init__(*args, **kwargs)`（此时 `use_tma_O` 已以 arch=120 算成 False），**之后**才 `self.arch = Arch.sm_80` 强制其余 SM80 code path。文件头注释亦点明「SM120 uses the same SM80-era MMA instructions」。

**epilogue 的 TMA/非 TMA 分支**：

[flash_fwd.py:398-417](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L398-L417) — TMA 路径：`fence_view_async_shared()` 让 smem 写对 TMA 可见，`barrier_arrive(Epilogue)` 通知第 5 个 warp（`warp_idx == 4`）发起 `store_O()`（`cp.async.bulk`），再 `cp_async_bulk_wait_group(0, read=True)` 等完成。

[flash_fwd.py:418-449](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L418-L449) — **非 TMA 路径（Ampere 与 SM120 都走这里）**：`barrier(Epilogue)` 等 smem 写完 → `cute.autovec_copy(tOsO, tOrO)` 把 sO 宽向量读回 rmem → 用 `gmem_tiled_copy_O` 按谓词逐行写回 gmem（428-447 行，pack_gqa 时 449 行委托 `pack_gqa.store_O`）。相比 TMA 路径，它多了一程「smem→rmem→gmem」的往返，但完全不依赖 TMA 硬件。

#### 4.4.4 代码实践

**实践目标**：把「架构 → use_tma_O → epilogue 分支」这条因果链在源码里走通，理解 SM120 为何必须回退。

**操作步骤（源码阅读型）**：

1. 在 [flash_fwd.py:658](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L658) 读 `use_tma_O` 表达式，代入 arch = 80 / 90 / 100 / 120 各算一次布尔值，列表对比。
2. 打开 [flash_fwd_sm120.py:15-18](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm120.py#L15-L18)，确认 SM120 子类「先 super().__init__、后 self.arch=sm_80」的顺序，并指出 `use_tma_O` 是在哪一步、以哪个 arch 值算出来的。
3. 在 epilogue 的 [flash_fwd.py:398](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L398) 与 [flash_fwd.py:418](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L418) 两处分别记下「TMA 路径用到的硬件指令」与「非 TMA 路径用到的拷贝原子」。

**需要观察的现象**：arch=80（Ampere）与 arch=120（SM120）都得到 `use_tma_O=False`，进而都进入 418 行的非 TMA 分支；arch=90/100/110 得到 True，进入 398 行的 TMA 分支。

**预期结果**：你能解释「SM120 是 Sm80 的子类、本应用 Sm80 code path，但 Sm80 kernel 的 TMA epilogue 在 SM120 上不可用，所以用 `< sm_120` 的上界把 SM120 显式挡在 TMA 之外，让它走 cp.async 输出」。

**待本地验证**：若你有 SM120 设备，可分别用 `FLASH_ATTENTION_ARCH=sm_80`（或直接在 SM120 上）运行一次前向，导出 PTX（`CUTE_DSL_KEEP_PTX=1`）确认 epilogue 里出现的是 `cp.async` 而非 `cp.async.bulk` store；本实践不要求一定跑通。

#### 4.4.5 小练习与答案

**练习 1**：把 `use_tma_O` 的旧表达式 `arch >= sm_90` 与新表达式 `sm_90 <= arch < sm_120` 代入 arch=120，结果分别是什么？为什么旧表达式是 bug？
**答案**：旧式 `120 >= 90` = True——会错误地让 SM120 走 TMA epilogue，而 SM120 硬件不支持那条路径。新式 `90 <= 120 < 120` = False——SM120 正确回退到非 TMA（cp.async）输出。

**练习 2**：既然 SM120 子类最后会把 `self.arch` 改写成 `sm_80`，为什么 `use_tma_O` 仍能正确反映「这是 SM120」？
**答案**：因为改写发生在 `super().__init__()` **之后**，而 `use_tma_O` 在 `super().__init__()` **之内**（658 行）就以真实 arch=120 算好并固化了。之后改 `self.arch=sm_80` 不会回头重算 `use_tma_O`。所以 `use_tma_O` 是「以真实架构求值、之后冻结」的。

**练习 3**：Ampere（arch=80）在新旧表达式下 `use_tma_O` 都是什么？这是巧合还是必然？
**答案**：都是 False。这是必然而非巧合：Ampere 硬件本就没有 TMA，无论边界怎么写，`sm_90 <= 80` 这一关都过不去，自然走非 TMA。新边界只是**额外**把 SM120 也挡住，并不影响 Ampere 的既有行为。

---

## 5. 综合实践

**任务**：阅读 `FlashAttentionForwardSm80` 的主循环（[flash_fwd.py:751-1089](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L751-L1089)）与 `compute_one_n_block`（[flash_fwd.py:1091-1201](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L1091-L1201)），**绘制一张完整的前向数据流图**，标注以下要素：

1. **三级存储边界**：哪些数据在 gmem、smem、rmem，搬运用什么原子（cp.async / ldmatrix / smem-store-atom / universal-copy）。
2. **两个 MMA** 的位置：\(S=QK^\top\) 与 \(O\mathrel{+}=PV\) 分别在哪段代码、用什么 TiledMma。
3. **online softmax rescale 发生的位置**：`row_max`/`row_sum` 在哪更新、`rescale_O` 在哪调用、`row_scale` 如何流动。
4. **同步点**：`cp_async_wait_group`、`barrier()`、`NamedBarrierFwd.Epilogue` 分别守卫哪段读写。
5. **Q 加载的两条路径**：标准路径（`local_tile` 切片 + `self.load_Q` 连续 cp.async）与 pack_gqa 路径（`PackGQA.load_Q` 按行 gather），它们最终都写进单槽 `sQ`。
6. **输出回写的两条路径**：`use_tma_O=True`（Hopper/数据中心 Blackwell）走 TMA bulk store；`use_tma_O=False`（Ampere/SM120）走 rmem→smem→rmem→gmem 的 universal copy。

下面是一份参考骨架（请你按自己读到的细节补全箭头上的「搬运原子」与「发生在哪一行」）：

```
                        ┌───────────  gmem  ───────────┐
   mQ  mK  mV  mO  mLSE │  (batch, seqlen, nhead, hdim) │
   └─┬───┬───┬──┬───┬───┘                               │
     │   │   │  │  │                                    │
   Q: 标准=local_tile+cp.async; pack_gqa=PackGQA.load_Q(按行gather)+cp.async
   K/V: cp.async(128b);  O/LSE: TMA(cp.async.bulk) 或 universal-copy(看 use_tma_O)
     ▼   ▼   ▼                                          ▼
                  ┌──────────── smem ────────────┐
                  │ sQ (单槽)   sK[stage]  sV[stage] │
                  │ sO (复用 sQ 指针，epilogue 用)   │
                  └──┬────────────┬──────────────┬──┘
            ldmatrix │     ldmatrix│     ldmatrix(T)│
                     ▼            ▼              ▼
                  ┌──────────── rmem ─────────────────┐
                  │ tSrQ(常驻/每block读) tSrK  tOrVt     │
                  │   │                 │      │       │
                  │   └──gemm(Q,K)──→ acc_S (fp32)       │
                  │     mask_fn/score_mod → acc_S        │
                  │     online_softmax → row_max/row_sum │
                  │                     + row_scale      │
                  │     rescale_O → acc_O                │
                  │     rP = acc_S.to(dtype)             │
                  │     gemm_rs(P,V) → acc_O (fp32)      │
                  └─────────────────────────────────────┘
   收尾：finalize → row_scale=1/ℓ, row_sum→LSE
        rescale_O(acc_O) → 归一化
   epilogue: acc_O→rO(dtype)→sO→ [TMA | universal-copy] → gmem O
             row_sum→gmem LSE (列0线程写, 或 pack_gqa.store_LSE)
```

**评判标准**：

- 能在图上正确标出 **Q 只搬一次、K/V 多级轮转** 的区别。
- 能标出 **cp.async（gmem→smem）** 与 **ldmatrix（smem→rmem）** 是两种不同的搬运原子。
- 能指出 **online softmax 的 rescale 发生在两个 gemm 之间**（先 rescale 旧 O，再用新 P 累加新 O）。
- 能说出 **Ampere/SM120 不用 TMA 输出**（`use_tma_O=False`），O 回写多走了一程 smem；Hopper/数据中心 Blackwell 才走 TMA。
- 能区分 **标准 Q 加载（连续 local_tile）** 与 **pack_gqa Q 加载（PackGQA 按行 gather）**，并知道二者结果都落进同一个单槽 `sQ`。

> 本实践为源码阅读型，不要求在 GPU 上跑通。若你后续在 Hopper 上学完 u6-l2，可以把这张图与 Hopper 版（有 TMA 输入、warp-group MMA）并排对比，差异一目了然。

## 6. 本讲小结

- **Q 常驻、K/V 流水**：Q tile 在 prologue 一次性从 gmem 搬到 smem（单槽 `sQ`），内层循环反复读；K/V 各有 `num_stages` 份在环形 smem 缓冲里轮转，让搬运与计算重叠。
- **Q 加载两条路径**：标准路径用 `local_tile` 切连续切片再 `self.load_Q`（cp.async）；pack_gqa 路径在 `__init__` 先用 `pack_gqa_layout` 把 `qhead_per_kvhead` 折进 seqlen（mQ/mO/mLSE），prologue 改用 `PackGQA.load_Q` 按行 gather 进 `sQ`，使一块 KV 被 `qhead_per_kvhead` 个 Q 头复用。
- **主循环三段**：首迭代（seqlen 掩码）/ 因果或滑窗掩码段 / 无掩码快路径段，靠 `const_expr` 编译期特化减少谓词开销；遍历倒序，从 `n_block_max-1` 到 `n_block_min`。
- **单个 n block = 两段 GEMM + 中间 softmax**：`gemm(Q,K)→acc_S`，`online_softmax` 更新 `row_max/row_sum` 并产出 `row_scale`，`rescale_O` 修正旧 O，`rP=acc_S.to(dtype)`，`gemm_rs(P,V)→acc_O`。
- **Ampere 流水用 cp.async 计数器**：`commit_group`/`wait_group(N)` 跟踪在飞搬运（K、V 各一组故乘 2），手写环形下标 `smem_pipe_read/write`——这与 Hopper 的 mbarrier/`PipelineStateSimple` 思想一致但机制不同。
- **finalize + epilogue 收尾**：`finalize` 算 `1/row_sum` 并把 `row_sum` 改写成 LSE；`rescale_O` 归一化 O；epilogue 按 `use_tma_O` 选 TMA 或 universal-copy 写回 O，由列 0 线程（或 `pack_gqa.store_LSE`）写 LSE。
- **use_tma_O 的新边界 `sm_90 <= arch < sm_120`**：TMA 输出仅 Hopper(90)/数据中心 Blackwell(100,110) 启用；Ampere(80) 与 SM120(120) 都为 False。SM120 复用 Sm80 kernel，靠这条上界把它的 epilogue 显式挡在 TMA 之外、回退到 cp.async 输出。

## 7. 下一步学习建议

- **u7-l1 GQA / MQA 与 pack_gqa**：本讲讲了 Ampere/SM120 前向里 pack_gqa 的「调用点」（`__init__` 折叠 + `PackGQA.load_Q`/`store_O`/`store_LSE`）；u7-l1 会从数学与布局层面讲清 `pack_gqa_layout` 为什么能折叠头数、为什么对 GQA 提升吞吐，以及它和 TMA 维度保持（`make_packgqa_tiled_tma_atom`）的关系。
- **u6-l2 Hopper 前向 Kernel 与 TMA**：本讲是 Ampere/SM120 基线，下一讲看 `FlashAttentionForwardSm90` 如何把 cp.async 换成 **TMA 输入**（`cp.async.bulk` + mbarrier）、把 warp 级 MMA 换成 **warp-group MMA**、把环形下标换成 `PipelineStateSimple`（u5-l1），并真正启用本讲因 `< sm_120` 而关闭的 **TMA 输出**。把两讲对照，你会清楚「同一算法、不同硬件」的取舍。
- **重读 u4-l1 / u5-l1 / u3-l2**：本讲把它们拼装起来；若某段卡住，回到对应讲义查单一积木会更高效。
- **延伸阅读**：可粗读 `flash_fwd.py` 顶部的注释引用——它点明这份 CuTeDSL 代码是 `hopper/flash_fwd_kernel_sm80.h` 的 Python 重写，C++ 版适合作为更底层的参照；`flash_fwd_sm120.py` 文件头注释则解释了 SM120 为何复用 SM80 code path。

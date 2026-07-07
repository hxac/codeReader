# Ampere 前向 Kernel 全景

## 1. 本讲目标

本讲把前几讲学到的「积木」拼成一台完整的机器。读完本讲，你应当能够：

- 说出 FlashAttention 前向 kernel 的**整体数据流**：Q/K/V 如何从显存（gmem）搬到共享内存（smem）再到寄存器（rmem），MMA 与 online softmax 在哪里发生。
- 解释 **Q 常驻、K/V 流水**的分块策略，以及为什么主循环要倒序遍历 n block。
- 在源码里准确定位三段关键逻辑：Q tile 的加载与常驻、K/V 的流水主循环、online softmax 累加与 O/LSE 的存储。
- 看清 gmem / smem / rmem **三级存储边界**，以及每跨过一道边界用的是哪种拷贝原子。

本讲以 FA4 的 `FlashAttentionForwardSm80`（Ampere 基线）为对象。它是整个 FA4 代码库里**结构最简单、最易读**的前向 kernel——没有 TMA、没有 warp-group MMA、没有片上 tmem，只有最朴素的 cp.async + ldmatrix + 两段 mma。先把它读懂，再去读 Hopper/Blackwell kernel 就有了对照基准。

## 2. 前置知识

本讲默认你已经掌握以下概念（前几讲已建立）：

- **在线 softmax（u4-l1）**：用 `row_max`（m）与 `row_sum`（ℓ）两个寄存器张量逐块维护状态，靠重缩放因子 \(e^{m_{\text{旧}}-m_{\text{新}}}\) 修正基准迁移，最终把 `row_sum` 改写为 LSE。
- **BlockInfo（u3-l2）**：给定一个 Q tile，它要遍历的 K/V tile 范围是半开区间 `[n_block_min, n_block_max)`，主循环从 `n_block_max-1` 倒序走到 `n_block_min`。
- **cp.async 流水（u5-l1 / u5-l2）**：Ampere 上 gmem→smem 用 `cp.async`（128-bit 单次搬运），靠 `cp_async_commit_group` / `cp_async_wait_group(N)` 按「提交组计数」跟踪完成。

几个**本讲第一次出现、需要先点名的术语**：

| 术语 | 含义 |
|---|---|
| **gmem / smem / rmem** | 显存（global memory）/ 共享内存（shared memory）/ 寄存器（register memory），GPU 片内/片外的三级存储层次。 |
| **MMA** | Matrix Multiply-Accumulate，张量核心矩阵乘加指令。Ampere 上是 `MmaF16BF16Op`，输入 fp16/bf16，累加器 fp32。 |
| **ldmatrix** | Ampere/Hopper 的 smem→rmem 加载指令，配合 swizzle 布局把一个 8×8×16b 矩阵块喂给 MMA。 |
| **TiledMma** | CuTeDSL 里「一整块 MMA」的抽象，把指令级 MMA 平铺到一个 warp 甚至多个 warp 上。 |
| **acc_S / acc_O** | 两个 fp32 寄存器累加器：`acc_S` 存注意力分数 \(S=QK^\top\)，`acc_O` 存输出 \(O=\text{softmax}(S)V\) 的累加值。 |

> ⚠️ 关于流水线：本讲依赖的 u5-l1 讲的是 Hopper/Blackwell 的 `PipelineStateSimple`（用 phase 奇偶性 + mbarrier 握手）。**Ampere kernel 不用那套**，它用的是更老的 cp.async 计数器模型——`commit_group`/`wait_group(N)` 跟踪在飞搬运数，加一个手写的环形下标 `smem_pipe_read`/`smem_pipe_write`。两者解决的是同一个问题（让搬运与计算重叠），只是机制不同。本讲 4.2 节会专门讲清楚 Ampere 的版本。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [`flash_attn/cute/flash_fwd.py`](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py) | **主角**。包含 `FlashAttentionForwardBase`（通用配置与 epilogue）与 `FlashAttentionForwardSm80`（Ampere 专用：MMA 选择、共享存储、kernel 主循环、`compute_one_n_block`、`apply_score_mod`）。 |
| [`flash_attn/cute/ampere_helpers.py`](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/ampere_helpers.py) | Ampere 的两个积木：`get_smem_layout_atom`（swizzle 布局）与 `gemm` / `gemm_rs`（两段 GEMM 的内层循环）。 |
| [`flash_attn/cute/softmax.py`](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/softmax.py) | `Softmax` 类：`reset` / `online_softmax` / `rescale_O` / `finalize`，是主循环里的数值核心（u4-l1 详述）。 |
| [`flash_attn/cute/named_barrier.py`](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/named_barrier.py) | `NamedBarrierFwd.Epilogue` 等：epilogue 里 smem 读写同步用的编号屏障。 |

## 4. 核心概念与源码讲解

### 4.1 Q tile 加载与常驻

#### 4.1.1 概念说明

前向 kernel 的基本分块单位是 **Q tile**（形状 `tile_m × head_dim`）。一个 thread block（CTA）负责**一个** Q tile，任务是对它算完整的一行注意力。

关键策略是「**Q 常驻、K/V 流水**」：

- **Q tile 只加载一次**，在主循环开始前从 gmem 搬到 smem（`sQ`），整个内层循环期间反复读它，不再回 gmem。
- **K/V tile 一块接一块地流过**，每来一块做一次 `QK^T` 和 `PV`，算完就丢，下一块覆盖同一块 smem。

为什么要让 Q 常驻而让 K/V 流动？因为注意力对一个 Q tile 要乘遍**所有** K/V tile（O(N) 次），而每个 K/V tile 只服务**当前** Q tile（O(1) 次）。把使用频率高的 Q 留在片上，使用频率低的 K/V 流过，是典型的「**访问局部性 → 复用**」设计。

> 关于 `Q_in_regs`：FA4 还有一个开关，把 Q 进一步从 smem 提升到寄存器（`Q_in_regs=True`），此时 `sQ` 与 `sV` 复用同一块 smem。默认 `Q_in_regs=False`，Q 常驻 **smem**，每个 n block 再用 ldmatrix 把所需片段读到 rmem 参与 MMA。本讲按默认路径讲解。

#### 4.1.2 核心流程

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

注意 Q 的加载发生在 **prologue**（主循环之前），而它被消费（读进 rmem）发生在**主循环每个 n block**。这两步之间隔着对 K/V 的预取。

#### 4.1.3 源码精读

**Q tile 的 gmem 切片与形状**。在 `kernel` 开头，先用 `local_tile` 从当前 Q 头/当前 batch 切出当前 CTA 负责的 Q 块：

[flash_fwd.py:828-830](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py#L828-L830) — `gQ = local_tile(mQ_cur, (tile_m, tile_hdim), (m_block, 0))`：按 `m_block` 选出第 `m_block` 个 `tile_m × head_dim` 的 Q tile（`gK`/`gV` 用 `None` 占位，block 号在内层循环里填）。

**Q 的加载函数**。`load_Q` 用 `cp.async`（即 `gmem_tiled_copy_Q` 的底层 atom `CopyG2SOp`）把 `gQ` 搬到 `sQ`：

[flash_fwd.py:456-480](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py#L456-L480) — `load_Q` 主体。其中谓词 `t0QcQ[0,m,0][0] < seqlen - block*tile_m - tQcQ[0][0]` 处理序列末尾不足一个 `tile_m` 的情况；`check_hdim_oob` 分支处理 `head_dim` 不是 16 倍数时的列越界。注释明确写道「无需清空 sQ，因为我们只会写出有效输出」。

**Prologue 里调用 load_Q**。Q 是第一个被加载的张量：

[flash_fwd.py:959-961](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py#L959-L961) — `load_Q(...)` 后紧跟 `cp_async_commit_group()`，把这次 Q 搬运打包成一个可被 `wait_group` 跟踪的组。

**等待 Q 落地（默认路径）**：

[flash_fwd.py:963-989](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py#L963-L989) — `preprocess_Q` 与 K/V 预取交错。注意末尾 `if const_expr(not self.Q_in_regs): preprocess_Q()`：默认路径下这里把 Q 搬运的 commit group 等「足够旧」（`wait_group(num_stages*2-1)`），确保 Q 已在 smem。

**Q 在 smem 的布局**。`FlashAttentionForwardSm80._get_smem_layout_atom` 让 K 复用 Q 的布局，V 用 `head_dim_v` 版本：

[flash_fwd.py:580-586](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py#L580-L586) — `sK_layout_atom = sQ_layout_atom`。布局本体由 `ampere_helpers.get_smem_layout_atom` 给出。

[ampere_helpers.py:8-31](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/ampere_helpers.py#L8-L31) — swizzle 布局：按 `bytes_per_row` 选 128/64/32/16 字节的 `smem_k_block_size`，再配 `(swizzle_bits, swizzle_base)` 消除 bank conflict，让后续 ldmatrix 能高效读取。

#### 4.1.4 代码实践

**实践目标**：确认 Q tile 在 prologue 只加载一次，且其 smem 缓冲区在内层循环里被反复读、不被覆盖。

**操作步骤（源码阅读型）**：

1. 打开 `flash_fwd.py`，在 `kernel` 内搜索 `sQ` 的所有出现。
2. 统计对 `sQ`（或 `storage.sQ`）的**写**（`load_Q` / `cute.copy(..., sQ...)`）与**读**（partition_S / ldmatrix）的次数。
3. 对比 `sK`、`sV`：它们带不带 `smem_pipe_write` / `smem_pipe_read` 下标？带下标意味着有多个槽（环形缓冲），不带意味着单槽。

**需要观察的现象**：`sQ` 是单槽（无 stage 维度），写入只发生在 prologue；`sK`/`sV` 在 `_setup_attributes` 里被 tile 成 `(tile_n, head_dim, num_stages)`，带 stage 维度——这就是「Q 常驻、K/V 多级流水」在数据结构上的体现。

**预期结果**：你能用一句话总结「Q 在 smem 里只有一份，K/V 各有 `num_stages` 份在环形缓冲里轮转」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `sQ` 不需要 `num_stages` 维度，而 `sK`/`sV` 需要？
**答案**：Q 在整个内层循环里被同一个 CTA 反复读，加载一次足矣，无需隐藏其延迟；K/V 每算完一块就换下一块，需要 `num_stages` 份缓冲让「下一块的搬运」与「当前块的计算」重叠，否则每次都得等 HBM。

**练习 2**：`Q_in_regs=True` 时，Q 还在 smem 里吗？
**答案**：仍在，但只是为了过渡——`preprocess_Q` 里 `cute.copy(smem_thr_copy_Q, tSsQ, tSrQ_copy_view)` 把 Q 从 smem 拷进寄存器 `tSrQ`，之后内层循环直接用寄存器里的 Q，`sQ` 这块 smem 转手让给 `sV` 复用（见 `_get_shared_storage_cls` 返回的 `SharedStorageSharedQV`）。这是用更多寄存器换更省 smem 的取舍。

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

[flash_fwd.py:1019-1056](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py#L1019-L1056) — 三段循环。第一段调 `compute_one_n_block(..., is_first_n_block=True, mask_seqlen=True)`；第二段 `for n_tile in range(n_block_max-1-n_block_min_causal_local_mask)` 走因果/滑窗块，`mask_seqlen=True`；第三段 `for n_tile in range(n_block, unroll=1)` 走无掩码块，`mask_seqlen=False`。每段结束都 `smem_pipe_read = self.advance_pipeline(smem_pipe_read)` 推进环形下标。

**范围与起点的计算**：

[flash_fwd.py:785-809](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py#L785-L809) — 用 `BlockInfo.get_n_block_min_max(seqlen, m_block)` 得到 `[n_block_min, n_block_max)`，再 `n_block = max(n_block_max - 1, 0)`。对变长的「废 tile」（batch 越界），靠 clamp 到 0 + 谓词自保。

**环形下标推进**：

[flash_fwd.py:451-453](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py#L451-L453) — `advance_pipeline` 就是 `index+1 if index < num_stages-1 else 0`，即手写的 `(index+1) % num_stages`。

**单个 n block 的核心**——`compute_one_n_block`：

[flash_fwd.py:1112-1114](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py#L1112-L1114) — `sync()`：`cp_async_wait_group(num_stages*2-2)` 再 `barrier()`。乘 2 是因为每个 stage 有 K 和 V 两个 commit group，等的是「当前 stage 的数据已落地」。

[flash_fwd.py:1123-1146](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py#L1123-L1146) — `load_V_next()` 预取下一块 V；随后 `sm80_utils.gemm(thr_mma_qk, acc_S, tSrQ, tSrK, tSsQ, tSsK[...stage...], ...)` 算 \(S=QK^\top\)。`tSsK[..., smem_pipe_read]` 指明读哪个 stage 的 K。

[flash_fwd.py:1163-1192](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py#L1163-L1192) — `load_K_next()` 预取下一块 K；`online_softmax` + `rescale_O`；`rP = acc_S.to(dtype)`；最后 `sm80_utils.gemm_rs(thr_mma_pv, acc_O, tOrP, tOrVt, tOsVt[...stage...], ...)` 算 \(O\mathrel{+}=PV\)。`gemm_rs` 的 "rs" 指 A（即 P）在**寄存器**里，B（即 V）从 smem 来。

**两段 GEMM 的内层循环**在 `ampere_helpers`：

[ampere_helpers.py:34-83](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/ampere_helpers.py#L34-L83) — `gemm`：沿 K 维（head_dim）展开，每步先 `copy` 下一片 smem→rmem，再 `cute.gemm(...)` 累加，是典型的「软流水」——load 与 mma 交错。

[ampere_helpers.py:86-103](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/ampere_helpers.py#L86-L103) — `gemm_rs`：A 在寄存器（不再 smem→rmem），只搬 B（V），同样沿 K 维软流水。

**MMA 的选择**（Ampere 特化）：

[flash_fwd.py:588-599](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py#L588-L599) — `MmaF16BF16Op(dtype, Float32, (16,8,16))`：指令级 MMA 是 16×8×16，输入 fp16/bf16、累加 fp32。用 `num_threads//32` 个 warp 平铺，permutation 把 `tile_m` 维分给各 warp。

#### 4.2.4 代码实践

**实践目标**：跟踪单个 n block 内「搬运」与「计算」的交错，体会流水线如何把 HBM 延迟藏起来。

**操作步骤**：

1. 读 `compute_one_n_block`（[flash_fwd.py:1085-1194](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py#L1085-L1194)）。
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
**答案**：`gemm` 的两个操作数 A(Q)、B(K) 都从 smem 读（除非 `Q_in_regs`）；`gemm_rs` 的 A(P) 已经在寄存器（`tOrP`），只有 B(V) 从 smem 读，所以 `gemm_rs` 少了一路 smem→rmem 搬运（见 [ampere_helpers.py:96-101](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/ampere_helpers.py#L96-L101)）。

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

> 注意：Ampere 上 `use_tma_O = (arch >= sm_90)` 为 **False**，所以 O 的回写走的是「rmem → smem → gmem」的**非 TMA 路径**（universal copy）。TMA 路径是 Hopper 及以后的优化，u6-l2 会讲。这是 Ampere 与 Hopper kernel 的一个关键差异。

#### 4.3.2 核心流程

```
# 主循环结束后
row_scale = softmax.finalize()        # 1/row_sum（含零/NaN 安全化），同时把 row_sum 改写成 LSE
softmax.rescale_O(acc_O, row_scale)   # O *= 1/row_sum，完成归一化

# epilogue
rO = acc_O.to(dtype)                  # fp32 → fp16/bf16
barrier(Epilogue)                     # 等所有线程读完 V（rmem）
copy(rO → sO)                         # rmem → smem（用 smem store atom）
barrier(Epilogue)                     # 等 smem 写完
autovec_copy(sO → rO)                 # smem → rmem（宽向量化再读出）
copy(rO → gO)                         # rmem → gmem（universal copy，带谓词）
write mLSE[batch, head, q_idx] = LSE  # 每行由列 0 线程写出
```

#### 4.3.3 源码精读

**finalize + rescale_O（在 kernel 末尾调用）**：

[flash_fwd.py:1059-1061](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py#L1059-L1061) — `row_scale = softmax.finalize(); softmax.rescale_O(acc_O, row_scale)`。

**finalize 的数学**：

[softmax.py:192-227](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/softmax.py#L192-L227) — 先对 `row_sum` 做跨 quad 归约（主循环里每步只做了 warp 内 4 线程归约，这里补全跨 quad）；再算 `row_scale = rcp_approx(row_sum)`（对 0/NaN 行换成 `rcp(1.0)` 防 NaN）；最后把 `row_sum[r]` 改写成 `(row_max*scale_log2 + log2(row_sum)) * LN2`，即 LSE。

**online_softmax（主循环里每个 n block 调一次）**：

[softmax.py:126-190](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/softmax.py#L126-L190) — 三步递推：更新 `row_max`、算 `row_scale = exp2((row_max_prev - row_max_cur)*scale_log2)`、用 `exp2` 把 `acc_S` 转成概率并累加进 `row_sum`。首块（`is_first`）直接置 `row_scale=1`。这与 u4-l1 的推导一一对应。

**compute_one_n_block 里调用 online_softmax**：

[flash_fwd.py:1174-1178](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py#L1174-L1178) — `row_scale = softmax.online_softmax(acc_S, is_first=is_first_n_block)`；`rescale_O(acc_O, row_scale)`；`rP = acc_S.to(dtype)`。三步连读，正好是「更新 softmax 状态 → 修正 O 基准 → 物化 P」。

**epilogue（O 与 LSE 回写）**：

[flash_fwd.py:330-449](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py#L330-L449) — 整个 `epilogue` 方法。

  - [flash_fwd.py:347-360](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py#L347-L360)：`rO = acc_O.to(dtype)`，先 `barrier(Epilogue)` 确保所有线程读完 V，再用 smem store atom 把 `rO` 写到 `sO`。
  - [flash_fwd.py:367-390](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py#L367-L390)：LSE 回写。注意 `if taccOcO[0][1] == 0`——**只有对应列 0 的线程**写出每行的 LSE，避免重复写。
  - [flash_fwd.py:418-449](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py#L418-L449)：**非 TMA 路径**（Ampere 走这里）。`barrier(Epilogue)` 等 smem 写完 → `autovec_copy(sO → rO)` 宽向量读出 → `gmem_tiled_copy_O` 把 `rO` 写回 gmem（带 `check_hdim_v_oob` 与序列末尾谓词）。`pack_gqa` 时把写 O 委托给 `PackGQA.store_O`。

**Epilogue 屏障**：

[named_barrier.py:6-7](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/named_barrier.py#L6-L7) — `NamedBarrierFwd.Epilogue = 1`（编号 1，因为 0 号留给 `sync_threads()`）。epilogue 里两次用它：第一次「等所有线程读完 V（rmem）才能开始写 sO」，第二次「等 sO 写完才能从 sO 读回 rO 再写 gmem」。这正是 u5-l3 讲的「命名屏障管线程到齐」。

#### 4.3.4 代码实践

**实践目标**：验证 finalize 产出的 LSE 与输出的 O 在数学上自洽——即 `exp(lse)` 应等于该行 softmax 的归一化分母。

**操作步骤（源码阅读 + 计算）**：

1. 读 `finalize`（[softmax.py:192-227](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/softmax.py#L192-L227)），写出它最终 `row_sum[r]` 的表达式。
2. 读 `online_softmax`（[softmax.py:126-190](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/softmax.py#L126-L190)），确认主循环结束时 `acc_O` 里存的是 \(\sum_j \text{row\_scale}_j \cdot P_j V_j\)（已按最终 `row_max` 对齐但**未除以** `row_sum`）。
3. 用符号推导证明：`finalize` 后 `acc_O * row_scale` 恰好等于 \(\text{softmax}(S)V\)。

**需要观察的现象**：`row_scale = 1/row_sum`，所以 `acc_O * (1/row_sum)` 正好补上归一化分母；而 `lse = row_max*scale + ln(row_sum)` 恰好是 \(\ln\sum_j e^{s\cdot S_j}\)。

**预期结果**：你得到一条等式链：`O_out = acc_O / row_sum = (Σ_j P_j V_j) / row_sum = softmax(S)V`，且 `exp(lse) = row_sum`。这与 u2-l1 讲的「`exp(lse)` 可还原 softmax 分母」完全吻合。

**待本地验证**：可选地，在 GPU 上跑一次 `flash_attn_func(..., return_lse=True)`，取某一行 `(b, h, q)`，用 `lse[b,h,q]` 与同一行的 `O[b,h,q]`、参考 `softmax` 对比，确认 `attn(b,h,q,:).sum() ≈ exp(lse[b,h,q])` 的相对关系（注意 softmax 后求和=1，这里验证的是 LSE 的定义而非求和）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 finalize 要先 `warp_reduce(row_sum, width=4)` 再算 `row_scale`？
**答案**：主循环里的 `online_softmax` 每步只在「4 线程组」内做了 `fadd_reduce`（见 [softmax.py:159](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/softmax.py#L159) 的 `warp_reduction_max(threads_in_group=4)`），跨 quad 的归约被推迟到 finalize 一次性补做。把跨组归约攒到最后做一次，比每步都做省指令。

**练习 2**：epilogue 里 LSE 为什么只让「列 0 的线程」写？
**答案**：一个 MMA 累加器行被多个线程共同持有（tile 分布），但 LSE 是**每行一个标量**。若所有持有该行的线程都写会重复写同一地址。规定只有「对应列 0」的那个线程写，保证每行恰好写一次。

**练习 3**：Ampere 的 O 回写为什么不走 TMA？
**答案**：`use_tma_O = (arch >= Arch.sm_90)`，Ampere（arch=80）硬件没有 TMA，只能走 universal copy 的 rmem→smem→gmem 路径（[flash_fwd.py:418-449](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py#L418-L449)）。TMA 是 Hopper 起的硬件特性，u6-l2 会讲 Hopper kernel 如何用它。

---

## 5. 综合实践

**任务**：阅读 `FlashAttentionForwardSm80` 的主循环（[flash_fwd.py:745-1082](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py#L745-L1082)）与 `compute_one_n_block`（[flash_fwd.py:1085-1194](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py#L1085-L1194)），**绘制一张完整的前向数据流图**，标注以下要素：

1. **三级存储边界**：哪些数据在 gmem、smem、rmem，搬运用什么原子（cp.async / ldmatrix / smem-store-atom / universal-copy）。
2. **两个 MMA** 的位置：\(S=QK^\top\) 与 \(O\mathrel{+}=PV\) 分别在哪段代码、用什么 TiledMma。
3. **online softmax rescale 发生的位置**：`row_max`/`row_sum` 在哪更新、`rescale_O` 在哪调用、`row_scale` 如何流动。
4. **同步点**：`cp_async_wait_group`、`barrier()`、`NamedBarrierFwd.Epilogue` 分别守卫哪段读写。

下面是一份参考骨架（请你按自己读到的细节补全箭头上的「搬运原子」与「发生在哪一行」）：

```
                        ┌───────────  gmem  ───────────┐
   mQ  mK  mV  mO  mLSE │  (batch, seqlen, nhead, hdim) │
   └─┬───┬───┬──┬───┬───┘                               │
     │   │   │  │  │                                    │
   cp.async(128b) for Q/K/V; universal-copy for O/LSE   │
     ▼   ▼   ▼                                          │
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
   epilogue: acc_O→rO(dtype)→sO→gmem O; row_sum→gmem LSE
```

**评判标准**：

- 能在图上正确标出 **Q 只搬一次、K/V 多级轮转** 的区别。
- 能标出 **cp.async（gmem→smem）** 与 **ldmatrix（smem→rmem）** 是两种不同的搬运原子。
- 能指出 **online softmax 的 rescale 发生在两个 gemm 之间**（先 rescale 旧 O，再用新 P 累加新 O）。
- 能说出 **Ampere 不用 TMA**，O 回写多走了一程 smem。

> 本实践为源码阅读型，不要求在 GPU 上跑通。若你后续在 Hopper 上学完 u6-l2，可以把这张图与 Hopper 版（有 TMA、warp-group MMA）并排对比，差异一目了然。

## 6. 本讲小结

- **Q 常驻、K/V 流水**：Q tile 在 prologue 一次性从 gmem 搬到 smem（单槽 `sQ`），内层循环反复读；K/V 各有 `num_stages` 份在环形 smem 缓冲里轮转，让搬运与计算重叠。
- **主循环三段**：首迭代（seqlen 掩码）/ 因果或滑窗掩码段 / 无掩码快路径段，靠 `const_expr` 编译期特化减少谓词开销；遍历倒序，从 `n_block_max-1` 到 `n_block_min`。
- **单个 n block = 两段 GEMM + 中间 softmax**：`gemm(Q,K)→acc_S`，`online_softmax` 更新 `row_max/row_sum` 并产出 `row_scale`，`rescale_O` 修正旧 O，`rP=acc_S.to(dtype)`，`gemm_rs(P,V)→acc_O`。
- **Ampere 流水用 cp.async 计数器**：`commit_group`/`wait_group(N)` 跟踪在飞搬运（K、V 各一组故乘 2），手写环形下标 `smem_pipe_read/write`——这与 Hopper 的 mbarrier/`PipelineStateSimple` 思想一致但机制不同。
- **finalize + epilogue 收尾**：`finalize` 算 `1/row_sum` 并把 `row_sum` 改写成 LSE；`rescale_O` 归一化 O；epilogue 经 rmem→smem→gmem（universal copy，**非 TMA**）写回 O，由列 0 线程写 LSE。
- **三级存储边界**：gmem↔smem 用 cp.async（Ampere 无 TMA），smem↔rmem 用 ldmatrix，rmem 内是 fp32 累加器 `acc_S`/`acc_O`。

## 7. 下一步学习建议

- **u6-l2 Hopper 前向 Kernel 与 TMA**：本讲是 Ampere 基线，下一讲看 `FlashAttentionForwardSm90` 如何把 cp.async 换成 **TMA**（`cp.async.bulk` + mbarrier）、把 warp 级 MMA 换成 **warp-group MMA**、把环形下标换成 `PipelineStateSimple`（u5-l1）。把两讲对照，你会清楚「同一算法、不同硬件」的取舍。
- **重读 u4-l1 / u5-l1 / u3-l2**：本讲把它们拼装起来；若某段卡住，回到对应讲义查单一积木会更高效。
- **延伸阅读**：可粗读 `flash_fwd.py` 顶部的注释引用——它点明这份 CuTeDSL 代码是 `hopper/flash_fwd_kernel_sm80.h` 的 Python 重写，C++ 版适合作为更底层的参照。

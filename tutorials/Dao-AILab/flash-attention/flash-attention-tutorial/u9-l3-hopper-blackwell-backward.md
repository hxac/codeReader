# 讲义 u9-l3：Hopper / Blackwell 反向与 2CTA

## 1. 本讲目标

本讲承接 u9-l1（反向算法与 Sm80 反向 Kernel），把视角从 Ampere 基线推进到 Hopper（SM90）与 Blackwell（SM100/SM110）。读完本讲，你应当能够：

1. 说清楚 `FlashAttentionBackwardSm90` 相对 Sm80 基线的两处升级——**warp-group MMA（WGMMA）** 与 **TMA 异步拷贝**——以及它们如何改变反向主循环的写法。
2. 理解 `FlashAttentionBackwardSm100` 的核心差异：累加器住在**片上 tmem** 而非寄存器，靠 **UMMA（tcgen05）** 矩阵乘 + 16 个 warp 的**深度专门化**（reduce / compute / mma / load / relay / empty）来拼出反向流水。
3. 讲明白 SM100 上 **2CTA dQ reduce** 的含义：dQ 的 M 行被 cluster 内两个 CTA 切分各自归约，dS 跨 CTA 经 relay warp + cluster mbarrier 交换。
4. 了解**块稀疏（block-sparse）反向**在 SM90/SM100 上的接入方式，以及 2CTA 与块稀疏为何互斥。

本讲关注「同一套反向数学公式，三代硬件用不同手段落地」，数学部分（dQ/dK/dV 推导）只在 2.2 节做最小回顾，详细推导见 u9-l1。

## 2. 前置知识

### 2.1 硬件代际与矩阵乘指令

- **Ampere（SM80）**：矩阵乘用 **warp 级 MMA**（一条 `mma` 指令由 32 线程的 1 个 warp 协同算 16×8×16），操作数取自寄存器（rmem），累加器也在寄存器。
- **Hopper（SM90）**：升级为 **warp-group MMA（WGMMA）**，一条指令由 128 线程的 1 个 warp-group（4 个 warp）协同算 64 行；操作数可直接取自共享内存（smem）/寄存器，计算**异步化**，需 `warpgroup.wait_group` 等待；同时引入 **TMA**（`cp.async.bulk`）做单线程发起的整块 gmem↔smem 搬运。
- **Blackwell（SM100/SM110）**：再升级为 **UMMA**（`tcgen05.mma`），最关键的区别是**累加器住在片上 tmem（tensor memory）而非寄存器**；一个 CTA 可用 512 列 tmem，多个 warp 通过 `tcgen05.copy` 共享同一块 tmem。

### 2.2 反向五条公式（回顾）

设 \(S=QK^\top\)、\(P=\mathrm{softmax}(S)\)、前向输出 \(O=PV\)，前向已保存 \(\mathrm{LSE}=\ln\sum_j\exp(S_{ij})\) 与行和 \(D=(O\odot dO)_{\text{rowsum}}\)。反向梯度为：

\[
dV = P^\top dO,\qquad dP = dO\,V^\top,\qquad dS = P\odot(dP-D)
\]

\[
dQ = \mathrm{scale}\cdot dS\,K^\top,\qquad dK = \mathrm{scale}\cdot dS^\top Q
\]

整存 \(P\) 会破坏 \(O(N)\) 显存，所以三代理 kernel 都**重算** \(S=QK^\top\)，用前向存的 LSE 经 \(\exp_2\) 恢复 \(P\)。

### 2.3 dQ 的「跨 thread block 累加」难题

反向主循环按 **n_block**（KV 序列块）切工作：每个 thread block 负责一个 \(n\_block\)，但要遍历多个 \(m\_block\)（Q 序列块）。注意：

- \(dK,dV\) 的累加发生在**同一个 thread block 内**（沿 \(m\_block\) 累加），最后只写一次——可以用寄存器/tmem 累加器，无并发问题。
- \(dQ\) 则相反：同一个 \(m\_block\) 会被**多个 thread block**（不同 \(n\_block\)）写入，必须做**跨 thread block 累加**。

这条约束是三代 dQ 策略分化的根源，也是本讲综合实践对比表的核心。SM80/SM90/SM100 都最终把 dQ 累加进一块 fp32 全局缓冲 `mdQaccum`，再由后处理（u9-l2）收敛为 fp16 梯度；差别在「累加器住哪、用什么指令做原子加」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [flash_attn/cute/flash_bwd.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd.py) | `FlashAttentionBackwardSm80`：Ampere 基线（也是 SM120 反向基类），本讲只引用其 dQ 原子加作为对比基线。 |
| [flash_attn/cute/flash_bwd_sm90.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_sm90.py) | `FlashAttentionBackwardSm90`：Hopper 反向，WGMMA + TMA，本讲模块 4.1 的主角。 |
| [flash_attn/cute/flash_bwd_sm100.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_sm100.py) | `FlashAttentionBackwardSm100`：Blackwell 反向，UMMA + tmem + 2CTA + 块稀疏，本讲模块 4.2/4.3 的主角。 |
| [flash_attn/cute/named_barrier.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/named_barrier.py) | 命名屏障枚举，`NamedBarrierBwdSm100` 仅 5 个（见 5.1）。 |
| [flash_attn/cute/block_sparse_utils.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/block_sparse_utils.py) | 块稀疏反向工具函数（`*_bwd_sm90` / `*_bwd_sm100` 系列）。 |

## 4. 核心概念与源码讲解

### 4.1 Sm90 反向：warp-group MMA 与 TMA

#### 4.1.1 概念说明

`FlashAttentionBackwardSm90` 与 Sm80 基线**数学完全等价**，差别只在两条硬件升级：

1. **WGMMA 取代 warp 级 MMA**：一条指令算 64 行、操作数直接来自 smem，计算异步化。这让我们可以把「搬运」和「计算」分给**两类 warp**，用流水隐藏 HBM 延迟。
2. **TMA 取代 cp.async**：单线程按 TMA descriptor 发射 `cp.async.bulk` 搬整块 gmem↔smem，完成由 **mbarrier** 的 `complete_tx::bytes` 按字节数判定（见 u5-l2）。

由此 Sm90 反向采用经典的 **producer / consumer 二分**：前 1 个 warp-group 当 producer（低寄存器、发 TMA），后若干 warp-group 当 consumer（高寄存器、跑 WGMMA）。

#### 4.1.2 核心流程

Sm90 反向的 warp 划分与五个 GEMM 的关系如下（`num_threads=384`，即 3 个 warp-group，默认 2 个 MMA warp-group + 1 个 producer warp-group）：

```text
warp_idx 0..3  (WG0 producer) : load()       —— 发 TMA 拉 Q/K/V/dO/LSE/dPsum
warp_idx 0..3  (warp 1)       : dQaccum_store() —— 把 smem 里的 dQ 原子加进全局 mdQaccum
warp_idx 4..7  (WG1 consumer) : mma()        —— 跑 5 段 WGMMA + online softmax 修正
warp_idx 8..11 (WG2 consumer) : mma()        —— 同上（第二个 MMA warp-group）
```

每个 consumer warp-group 在 `mma_one_m_block` 里，对单个 \(m\_block\) 串行执行 7 步（与 u9-l1 同构，只是 MMA 换成 WGMMA）：

```text
(1) S  = Q @ K^T          # WGMMA, acc_S
(2) dP = dO @ V^T         # WGMMA, acc_dP
(3) P  = exp2(S*scale_log2 - LSE)   # 寄存器逐元素, 用前向 LSE 复原 P
(4) dS = P * (dP - dPsum)          # 寄存器逐元素, dPsum 来自预处理
(5) dV += P^T @ dO       # WGMMA, acc_dV（沿 m_block 累加）
(6) dQ  = dS @ K          # WGMMA, acc_dQ（单个 m_block 的部分和）
(7) dK += dS^T @ Q        # WGMMA, acc_dK（沿 m_block 累加）
```

`dV`、`dK` 在寄存器里沿 \(m\_block\) 累加，epilogue 用 **TMA S2G**（`cp.async.bulk` 整块 smem→gmem）一次性写回；`dQ` 则走另一条路（见 4.1.3）。

#### 4.1.3 源码精读

**warp 二分分发**：producer warp（`warp_idx < 4`）调 `load` 与 `dQaccum_store`，consumer warp（`warp_idx >= 4`）减去 128 后调 `mma`，并用 `setmaxregister_increase` 给 MMA warp 拨高寄存器额度。

[flash_attn/cute/flash_bwd_sm90.py:762-851](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_sm90.py#L762-L851) —— producer/consumer 分发：`warp_idx < 4` 跑 `load` 与 `dQaccum_store`，`warp_idx >= 4` 跑 `mma`。

**五个 WGMMA 的建立**：与 Sm80 的五条公式一一对应，全部用 `make_trivial_tiled_mma` 构造，操作数主向、是否走 smem 由若干 `swapAB`/`mma_*_is_rs` 开关决定。

[flash_attn/cute/flash_bwd_sm90.py:1159-1216](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_sm90.py#L1159-L1216) —— 注释里把五段 GEMM 列得清清楚楚：`S = Q @ K.T`、`dP = dO @ V.T`、`dV += P.T @ dO`、`dK += dS.T @ Q`、`dQ = dS @ K`，每段都用 `partition_fragment_ABC` 切出 A/B 片段。

**单 m_block 的 7 步主循环**：`acc_S`→`acc_dP`→逐元素得 `P`、`dS`→`dV`→`dQ`→`dK`，WGMMA 之间靠 `warpgroup.wait_group` 等待异步完成，靠 `PdS_barrier`（命名屏障）在两个 MMA warp-group 之间传递 P/dS。

[flash_attn/cute/flash_bwd_sm90.py:1500-1622](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_sm90.py#L1500-L1622) —— `mma_one_m_block` 的 7 步；步骤 (3) 用 `exp2(... * softmax_scale_log2 - lse_val)` 复原 P，步骤 (5)(6)(7) 是三段 WGMMA。

**dQ 的 smem 中转 + 原子加**：dQ 不能像 dK/dV 那样直接写，因为同一个 \(m\_block\) 会被多个 thread block 写。Sm90 的做法是：consumer 把 `acc_dQ` 经 R2S 写进 smem 缓冲 `sdQaccum`，用 `dQFullWGx`/`dQEmptyWGx` 命名屏障与一个**专门的 warp（warp 1，`dQaccum_store`）**握手；该 warp 再用 `cpasync_reduce_bulk_add_f32`（一条向量化的 bulk 原子加）把 smem 里的 dQ 加进全局 `mdQaccum`。

[flash_attn/cute/flash_bwd_sm90.py:1592-1606](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_sm90.py#L1592-L1606) —— consumer 把 `acc_dQ` 拷进 `tdQsdQaccum`（smem），用 `dQEmpty` 等缓冲空闲、`dQFull` 通知 store warp。

[flash_attn/cute/flash_bwd_sm90.py:1779-1885](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_sm90.py#L1779-L1885) —— `dQaccum_store`：专门的 warp 用 `cpasync_reduce_bulk_add_f32(sdQaccum, gdQaccum, bytes)` 把 smem 的 dQ **批量原子加**进全局 `mdQaccum`（L1879-L1885）。

**deterministic 模式**：原子加的执行顺序不确定，导致 dQ 数值不bit- reproducible。开启 `deterministic=True` 时，用一块 `mdQ_semaphore` 旗标（u5-l3 的 `wait_eq`/`arrive_inc`）强制各 \(n\_block\) 按固定顺序写同一个 \(m\_block\)，配合 `SingleTileLPTBwdScheduler` 调度（重块先算）保证可复现。

[flash_attn/cute/flash_bwd_sm90.py:1858-1895](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_sm90.py#L1858-L1895) —— `barrier.wait_eq(mdQ_semaphore, lock_value)` 等前序 \(n\_block\) 写完本 \(m\_block\)，写完再 `barrier.arrive_inc(..., 1)` 放行后序。

#### 4.1.4 代码实践

**实践目标**：确认 Sm80 与 Sm90 反向在数学上等价，并感知 WGMMA/TMA 带来的吞吐差异。

**操作步骤**：

1. 构造一组 fp16、`head_dim=128`、`seqlen=2048`、`causal=True` 的 q/k/v，要求 `grad`。
2. 用 `FLASH_ATTENTION_ARCH=sm_80` 跑一次反向，记录 `dQ/dK/dV` 与耗时。
3. 用 `FLASH_ATTENTION_ARCH=sm_90` 再跑一次（在 Hopper GPU 上），对比三组梯度与耗时。

**预期结果**：两次的 `dQ/dK/dV` 最大误差应在 fp16 舍入量级（约 1e-3）；Sm90 在长序列下应明显更快（WGMMA + TMA 减少访存指令数）。

**注意**：本实践需要 Hopper（SM90）GPU。若无相应硬件，可降级为「源码阅读型实践」：在 `mma_one_m_block` 中标出 5 段 WGMMA 各自的 `gemm_*` 调用与对应公式行，写成对照表。如无法确定运行结果，请标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：Sm90 反向里 `dV`、`dK` 用寄存器累加器沿 \(m\_block\) 累加后只写一次，为何 `dQ` 必须走全局原子加？

**答案**：`dV`/`dK` 的累加维度是 \(m\_block\)，而主循环按 \(n\_block\) 切工作，单个 thread block 会遍历完它负责的所有 \(m\_block\)，所以累加发生在同一线程块内、无并发；`dQ` 的累加维度是 \(n\_block\)，同一个 \(m\_block\) 由不同 thread block 各贡献一部分，只能写进共享的全局 `mdQaccum` 并做原子加。

**练习 2**：`dQaccum_store` 为什么用一个**独立的 warp**而不是让 MMA warp 自己原子加？

**答案**：把 dQ 从 smem 批量原子加进 gmem 是一段相对慢的访存操作，且需要自己的 `cp.async.bulk` 完成跟踪；交给独立 warp 可以让它与 MMA warp 的下一轮计算重叠（producer/consumer 流水），不占 MMA warp 的寄存器额度。

---

### 4.2 Sm100 反向：UMMA、tmem 累加与 warp 专门化

#### 4.2.1 概念说明

`FlashAttentionBackwardSm100` 是本讲最复杂的主角。与 Sm90 相比有三处质变：

1. **累加器住 tmem**：所有 GEMM 的累加器（`tStS`、`tdPtdP`、`tdVtdV`、`tdKtdK`、`tdQtdQ`）都是 tmem 张量，而非寄存器片段。UMMA 把结果直接写进 tmem，多个 warp 经 `tcgen05.copy` 读取同一块 tmem——这让 16 个 warp 能真正**协作**而不是各算各的。
2. **16-warp 深度专门化**：一个 CTA 拥有 16 个 warp（512 线程），按角色切成 6 组：reduce(0-3) / compute(4-11) / mma(12) / load(13) / relay(14) / empty(15)。
3. **dQ 的 tmem 归约**：dQ 累加器在 tmem，归约时先 `tmem→rmem`（t2r），再 `rmem→smem`（r2s），最后才批量原子加进 gmem——比 Sm90 多一跳 tmem 读取。

#### 4.2.2 核心流程

16 个 warp 的分工（这是 Sm100 反向区别于 Sm90 的最大结构特征）：

```text
warp  0..3   reduce   : dQacc_reduce()   tmem(dQ) → rmem → smem → 原子加进 gmem mdQaccum
warp  4..11  compute  : compute_loop()   P/dS 逐元素计算 + dK/dV epilogue（tmem→gmem）
warp 12      mma      : mma()            发射 5 段 UMMA（tcgen05.mma）
warp 13      load     : load()           发 TMA 拉 Q/K/V/dO/LSE/dPsum
warp 14      relay    : relay()          [仅 2CTA] 跨 CTA 交换 dS
warp 15      empty    : （占位/低寄存器）
```

注意 Sm100 把 Sm90 的「WGMMA consumer」拆成了 **mma warp（只发指令）+ compute warp（做逐元素与 epilogue）** 两类，因为 UMMA 是异步的、指令发射与结果消费可以由不同 warp 承担。它们之间靠 `PipelineUmmaAsync` / `PipelineAsyncUmma`（u5-l1 的流水线状态机）与 tmem 的 full/empty 相位握手。

`compute_loop` 负责：等 `S`（tmem）就绪 → 用 LSE 复原 `P` → 算 `dS=P*(dP-dPsum)` → 把 `dS` 写回 tmem/smem 供 dK、dQ MMA 用 → 跑 dK/dV 的 epilogue（tmem→rmem→gmem）。`mma` warp 则在一个 prologue + main loop + tail 的结构里依次发射 S、dP、dK、dV、dQ 五段 UMMA。

#### 4.2.3 源码精读

**16-warp 角色定义**：直接以 warp id 区分角色，线程数固定 512。

[flash_attn/cute/flash_bwd_sm100.py:136-153](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_sm100.py#L136-L153) —— `reduce_warp_ids=(0,1,2,3)`、`compute_warp_ids=(4..11)`、`mma_warp_id=12`、`load_warp_id=13`、`relay_warp_id=14`、`empty_warp_id=15`，`threads_per_cta = 32*16 = 512`。

**tmem 分区**：`__init__` 里把 512 列 tmem 预划分给 S/P/dV/dK/dP/dS/dQ 等区域（P 复用 S 区、dS 复用 dP 区），累加器之间的重叠由 full/empty 相位保证不冲突。

[flash_attn/cute/flash_bwd_sm100.py:178-199](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_sm100.py#L178-L199) —— tmem 偏移布局，`tmem_P_offset = tmem_S_offset`（P 复用 S）、`tmem_dS_offset = tmem_dP_offset`（dS 复用 dP）。

**五个 UMMA 的建立**：用 `make_trivial_tiled_mma` + `tcgen05.OperandMajorMode` / `OperandSource.TMEM` 构造，注意 `dV` 与 `dK` 的 A 操作数（P、dS）显式声明来自 tmem（`a_source=tcgen05.OperandSource.TMEM`）。

[flash_attn/cute/flash_bwd_sm100.py:263-315](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_sm100.py#L263-L315) —— `_get_tiled_mma`：`tiled_mma_dV`/`tiled_mma_dK` 的 `a_source=TMEM`，`cta_group` 在 2CTA 时为 `TWO`。

**warp 分发**：kernel 里按 `warp_idx` 把工作派给 6 类 warp；mma warp 负责 `tmem.allocate` / `retrieve_ptr` / `free` 的完整生命周期，compute/reduce warp 用 `tmem.wait_for_alloc` 等分配完成。

[flash_attn/cute/flash_bwd_sm100.py:1424-1623](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_sm100.py#L1424-L1623) —— empty/relay/load/mma/compute/reduce 六类 warp 的分发，mma warp 在 L1502-L1549 完成 tmem 的 alloc→mma→relinquish→free。

**mma 主循环（1-CTA 分支）**：prologue（S、dP、dV）+ main loop（S→dK→dQ→dP→dV）+ tail（dK、dQ），每段 UMMA 前后用 `pipeline_*.sync_object_full/empty.arrive/wait` 与 compute/reduce warp 握手。注释明确列出每轮 5 步的顺序。

[flash_attn/cute/flash_bwd_sm100.py:2583-2694](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_sm100.py#L2583-L2694) —— 1-CTA 的 prologue + main loop + tail，注释标注 `1. S / 2. dQ / 3. dK / 4. dP / 5. dV`。

**dQ 的 tmem 归约**：reduce warp（0-3）的 `dQacc_reduce` 先用 `tcgen05.copy` 把 dQ 从 tmem 读进寄存器（t2r），再 r2s 写进 `sdQaccum` smem，最后由 leader warp 用 `cpasync_reduce_bulk_add_f32` 批量原子加进全局 `mdQaccum`。

[flash_attn/cute/flash_bwd_sm100.py:3480-3488](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_sm100.py#L3480-L3488) —— `tcgen05.copy.Ld32x32bOp` 把 dQ 从 tmem 载入寄存器（t2r），这是 Sm100 比 Sm90 多出的一跳。

[flash_attn/cute/flash_bwd_sm100.py:3613-3655](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_sm100.py#L3613-L3655) —— 逐 stage 做 r2s（写 `sdQaccum`）+ `cpasync_reduce_bulk_add_f32`（原子加进 `gdQaccum_cur`），并 `reduce_sync_barrier` 在 r2s 与 tma store 之间同步。

**dK/dV 的 tmem→gmem epilogue**：compute warp 把 dV/dK 从 tmem 读进寄存器、转成 fp16、乘上 `softmax_scale`（dK/dQ 才乘，dV 的 scale 已含在 P 里），再用 universal copy 写回 gmem（MHA 直接写；GQA 走后处理累加，见 u9-l2）。

[flash_attn/cute/flash_bwd_sm100.py:3800-3847](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_sm100.py#L3800-L3847) —— dK epilogue：`dK_vec = tdKtdK_t2r[...].load() * softmax_scale` 再 `.to(dk_dtype)` 写回，dV 在前一段不乘 scale。

#### 4.2.4 代码实践

**实践目标**：体会「累加器在 tmem」对编程模型的影响——同样算 dQ，Sm100 比 Sm90 多一跳 tmem 读取。

**操作步骤**（源码阅读型）：

1. 打开 `flash_bwd_sm100.py` 的 `dQacc_reduce`（L3460），画出 dQ 从产生到落盘的完整数据路径。
2. 在图上标出每一次存储介质切换（tmem→rmem→smem→gmem）及其同步原语（`tcgen05.copy`、`fence_view_async_shared`、`reduce_sync_barrier`、`cpasync_reduce_bulk_add_f32`）。
3. 对比 Sm90 的 `dQaccum_store`（L1779），数一数 Sm100 多了哪一跳、为什么。

**预期结果**：Sm100 的 dQ 路径为 `tmem(tdQtdQ) → rmem(tdQrdQ_t2r) → smem(sdQaccum) → gmem(mdQaccum)`，比 Sm90 的 `rmem(acc_dQ) → smem(sdQaccum) → gmem` 多出「tmem→rmem」一跳，这是 UMMA 累加器住 tmem 的直接代价，换来的是 mma/compute/reduce 三类 warp 能并行流水。

#### 4.2.5 小练习与答案

**练习 1**：Sm100 为何把 Sm90 的「consumer warp-group」进一步拆成 mma warp + compute warp？

**答案**：UMMA 是异步指令——发射（mma warp）与消费结果（compute warp 做 P/dS 逐元素、dK/dV epilogue）可以解耦给不同 warp 并行；且 tmem 允许多 warp 共享同一累加器，无需像 WGMMA 那样把结果搬回寄存器再 shuffle。拆分后 mma warp 用低寄存器只发指令，compute warp 用高寄存器做逐元素，寄存器额度分配更灵活。

**练习 2**：Sm100 的 dV epilogue 为什么**不**乘 `softmax_scale`，而 dK epilogue 要乘？

**答案**：与 u9-l1 一致——`dV = P^T dO` 里的 P 已经包含前向的 softmax 缩放，所以 dV 不再补 scale；而 `dK = scale·dS^T Q`、`dQ = scale·dS·K^T` 的 scale 是在 `dS` 之外单独乘的，epilogue 必须补上。Sm100 把这个延迟缩放融合在 tmem→gmem 的类型转换那一步。

---

### 4.3 2CTA dQ reduce 与块稀疏反向

#### 4.3.1 概念说明

本模块讲两个相对独立但都属 Sm100 反向「高级特性」的话题。

**2CTA dQ reduce**：当 `head_dim=192`（DeepSeek 风格）等大头维时，单个 CTA 喂不饱 UMMA，于是把 cluster 设成 `(2,1)`，让 cluster 内两个 CTA 协作。2CTA 对反向的影响集中在 dQ：

- dS 需要在两个 CTA 之间**交换**（每个 CTA 只算了 dS 的一半行），由一个专门的 **relay warp** + 三把 cluster 级 mbarrier（`dS_cluster_full/empty/leader`）驱动。
- dQ 的 M 行被两个 CTA **切分**（CTA0 拿前半、CTA1 拿后半），各自独立归约进全局 `mdQaccum`——这就是「2CTA dQ reduce」的字面含义。

2CTA 还要求把所有「按 CTA 计」的量翻倍：MMA 的 M 维与 idesc 翻倍、TMA 的 `tx_count` 翻倍、参与同步的线程数翻倍（与 u8-l4 的前向 2CTA 同源）。

**块稀疏反向**：当注意力掩码是块粒度稀疏（只算部分 Q×KV 块）时，主循环不再遍历连续的 `[m_block_min, m_block_max)`，而是按稀疏块表给出的有效 m_block 列表迭代。SM90 与 SM100 都支持块稀疏反向，但 **2CTA 与块稀疏互斥**（见下）。

#### 4.3.2 核心流程

**2CTA 反向的数据流**：

```text
compute warp(各 CTA) : 算出本 CTA 的半份 dS → 写进 smem(sdS_xchg)
relay warp(每 CTA 1 个): 经 dS_cluster_full/empty mbarrier, 把本 CTA 的 dS 半份
                          交换/中继给 peer CTA, 拼出完整 dS 供 dK/dQ MMA
mma warp(leader CTA) : 用完整 dS 发射 dK、dQ 的 UMMA(cta_group=TWO)
reduce warp(各 CTA)   : dQacc_reduce 时按 stage_offset 取本 CTA 拥有的 M 行半份,
                          各自原子加进全局 mdQaccum
```

**块稀疏反向的接入点**（三处一致的模式）：

1. **load**：不再按连续 m_block 拉 Q/dO，而是按稀疏块表 `produce_block_sparse_q_loads_bwd_*` 拉。
2. **mma/compute**：迭代次数由 `get_total_q_block_count_bwd` 给出，实际 m_block 由 `get_m_block_from_iter_bwd` 从稀疏索引反查。
3. **dQ 归约**：deterministic 模式下，锁值由 `dq_write_order` 稀疏写序表决定（`_dq_semaphore_lock_value`）。

#### 4.3.3 源码精读

**2CTA 的 mma tiler 翻倍**：dQ 的归约维（K 维）变成 `tile_n * cta_group_size`，其余 MMA 的 M 维翻倍。

[flash_attn/cute/flash_bwd_sm100.py:86-103](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_sm100.py#L86-L103) —— `cta_group_size = 2 if use_2cta_instrs else 1`；`mma_tiler_dsk = (tile_m, tile_hdim, tile_n * cta_group_size)`，注释点明「2-CTA: reduction dim is cluster-wide」。

**relay warp 跨 CTA 交换 dS**：relay warp 在 cluster 级 mbarrier 上等待 peer CTA 的 dS 半份，并在 leader mbarrier 上通知 mma warp。

[flash_attn/cute/flash_bwd_sm100.py:1625-1667](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_sm100.py#L1625-L1667) —— `relay`：`mbarrier_wait(dS_cluster_full)` 等 peer 的 dS，`mbarrier_arrive(dS_cluster_leader)` 通知 mma warp，`dS_cluster_phase ^= 1` 翻相。

**2CTA dQ 的 M 行切分**：reduce warp 按自己在 cluster 中的 rank 取对应的 stage 段。

[flash_attn/cute/flash_bwd_sm100.py:3487-3496](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_sm100.py#L3487-L3496) —— 注释 `CTA 0 -> (M/2, D) (stage 0,1) & CTA 1 -> (M/2, D) (stage 2,3)`；`stage_offset = expected_reduce_stages * cta_rank_in_cluster`。

**2CTA 与块稀疏互斥**：开 2CTA 时显式断言不允许块稀疏。

[flash_attn/cute/flash_bwd_sm100.py:927-931](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_sm100.py#L927-L931) —— `assert blocksparse_tensors is None, "2-CTA mode does not support block sparsity"`。

**块稀疏反向的工具函数**：SM90 与 SM100 各有一套同名后缀的函数，负责「稀疏块表 → 有效 m_block 迭代」。

[flash_attn/cute/block_sparse_utils.py:1005-1006](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/block_sparse_utils.py#L1005-L1006) —— `get_total_q_block_count_bwd`：给一个 \(n\_block\)，算它要遍历多少个有效 Q 块。

[flash_attn/cute/block_sparse_utils.py:1146-1179](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/block_sparse_utils.py#L1146-L1179) —— `get_block_sparse_iteration_info_bwd`（取稀疏索引/计数）与 `get_m_block_from_iter_bwd`（把稀疏迭代号反查成真实 m_block）。

**SM90 块稀疏反向的 mma 驱动**：把 `mma_one_m_block` 包进一个按稀疏块表迭代的循环。

[flash_attn/cute/block_sparse_utils.py:1350-1351](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/block_sparse_utils.py#L1350-L1351) —— `consume_block_sparse_mma_bwd_sm90`：在 Sm90 反向主循环里替代连续 m_block 迭代。

**SM100 块稀疏的 dQ 锁值**：deterministic + 块稀疏时，dQ 写序由稀疏表 `dq_write_order` 决定。

[flash_attn/cute/flash_bwd_sm100.py:3447-3457](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_sm100.py#L3447-L3457) —— `_dq_semaphore_lock_value`：块稀疏分支用 `curr_dq_write_order[sparse_iter]` 作为锁值，保证 dQ 写序确定。

#### 4.3.4 代码实践

**实践目标**：在 `head_dim=192, head_dim_v=128`（DeepSeek 形状）上确认 2CTA 反向与 1-CTA 数值一致；并验证 2CTA + 块稀疏会被断言拒绝。

**操作步骤**：

1. 构造 `head_dim=192, head_dim_v=128`、`num_heads=num_heads_kv`（MHA）、`causal=True` 的 fp16 q/k/v，对 `out.sum()` 反向。
2. FA4 公共接口在 `head_dim=192` 时会自动启用 2CTA（见 u8-l4），记录 dQ/dK/dV 与耗时。
3. （源码阅读）确认若同时传入 `block_sparse_tensors` 与 2CTA，会在 L927-L931 抛出 `AssertionError`。

**预期结果**：2CTA 反向与等价的展开式 PyTorch 参考实现最大误差在 fp16 量级；`use_2cta_instrs=True` + `blocksparse_tensors is not None` 触发断言。本实践需要 Blackwell（SM100/SM110）GPU；若无硬件，标注「待本地验证」并改为阅读 `relay` + `mma`（L2382-L2582 的 hd192-2cta 分支）画出 cluster 内 dS 交换时序图。

#### 4.3.5 小练习与答案

**练习 1**：为什么 2CTA 模式禁止块稀疏？

**答案**：2CTA 依赖 cluster 内两 CTA 严格对称协作（dS 交换、dQ M 行切分、cluster mbarrier 同步），其调度假设两个 CTA 处理同一组连续 \(m\_block\)；块稀疏会把有效 \(m\_block\) 变成不连续的稀疏列表，破坏两 CTA 的工作对称性与 dS 交换的相位配对，极易死锁，故直接断言拒绝。

**练习 2**：2CTA 下 `tx_count`（TMA mbarrier 的完成字节数）为什么要乘 `cta_group_size`？

**答案**：cluster `(2,1)` 下，mbarrier 的 `complete_tx::bytes` 统计的是**整个 cluster** 收到的字节数——两个 CTA 各搬一份（multicast 或各搬各的），总数翻倍；若不乘 `cta_group_size`，mbarrier 会永远等不到足够的字节而挂起。这与 u8-l4 前向 2CTA 的死锁陷阱同源。

---

## 5. 综合实践：三代反向 dQ 累加策略对比表

把本讲三个模块串起来。请阅读三代反向源码后，**亲手填写**下面这张 dQ 累加策略对比表，并在每格注明对应的源码位置（文件:行号）。

### 5.1 任务说明

dQ 的核心难题是「同一个 \(m\_block\) 被多个 thread block 写」，三代 kernel 用不同的累加器位置与原子加指令解决。请按下表逐项对比，并就「数值一致性」与「性能取舍」各写 2-3 句分析。

### 5.2 参考对比表（请先自己填，再对照）

| 维度 | Sm80（Ampere） | Sm90（Hopper） | Sm100（Blackwell） |
| --- | --- | --- | --- |
| dQ 累加器位置 | 寄存器（`acc_dQ`，warp MMA） | 寄存器（`acc_dQ`，WGMMA） | **tmem**（`tdQtdQ`，UMMA） |
| dQ 落盘路径 | rmem → gmem | rmem → smem(`sdQaccum`) → gmem | **tmem → rmem → smem(`sdQaccum`) → gmem** |
| 原子加指令 | `atomic_add_fp32`（逐元素） | `cpasync_reduce_bulk_add_f32`（批量） | `cpasync_reduce_bulk_add_f32`（批量） |
| 执行主体 | MMA warp 自己 | 专用 warp（`dQaccum_store`） | reduce warp（0-3） |
| cluster/2CTA | 单 CTA | 单 CTA | 2CTA 时 dQ 的 M 行按 `stage_offset` 切分 |
| 确定性模式 | semaphore `wait_eq/arrive_inc` | semaphore + LPT 调度 | semaphore + LPT 调度（块稀疏用 `dq_write_order`） |
| 关键源码 | [flash_bwd.py:1037-1042](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd.py#L1037-L1042) | [flash_bwd_sm90.py:1779-1885](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_sm90.py#L1779-L1885) | [flash_bwd_sm100.py:3460-3655](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_sm100.py#L3460-L3655) |

### 5.3 数值一致性与性能取舍分析（参考结论）

- **数值一致性**：三代**数学等价**，最终都把 fp32 部分和原子加进 `mdQaccum`、再由后处理收敛为 fp16；差异只在浮点累加顺序。非 deterministic 模式下，原子加的执行顺序不确定，三代都存在跨运行的微小数值漂移；开启 `deterministic` 后，三者都用 semaphore 强制写序，可 bit- reproducible（Sm90/Sm100 还能用 LPT 调度减少尾延迟）。Sm80 的逐元素 `atomic_add_fp32` 与 Sm90/Sm100 的批量 `cpasync_reduce_bulk_add_f32` 在浮点结果上可能有末位差异，但都在 fp16 舍入容忍内。

- **性能取舍**：Sm80 最简单但 dQ 落盘慢（逐元素原子加、无独立 warp 隐藏）；Sm90 用 WGMMA + 专用 store warp + 批量原子加，把 dQ 落盘与 MMA 流水重叠；Sm100 把累加器搬进 tmem，多出「tmem→rmem」一跳，但换来 mma/compute/reduce 三类 warp 真正并行流水，且 2CTA 能在大头维下进一步几乎翻倍吞吐——代价是同步复杂度激增（cluster mbarrier、relay warp、dS 交换相位），调试难度最高（见 u11-l5 的 2CTA 死锁排查）。

### 5.4 进阶观察（可选）

阅读 `dQacc_reduce`（[flash_bwd_sm100.py:3593-3601](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_sm100.py#L3593-L3601)），回答：reduce warp 为什么要先用 `tcgen05.copy` 把 dQ 从 tmem 读进寄存器，而不是直接 tmem→smem？提示：tmem 没有直接到 smem 的批量搬指令，必须经寄存器中转；且 `tcgen05.copy.Ld32x32b` 支持按 `Repetition(dQ_reduce_ncol_t2r)` 分块读取，便于把大头维拆成多次小归约以重叠。

## 6. 本讲小结

- Sm90 反向 = Sm80 数学 + **WGMMA**（异步、操作数取自 smem）+ **TMA**（mbarrier 按字节计完成）+ producer/consumer 二分；dQ 走「rmem→smem→批量原子加」，由专用 `dQaccum_store` warp 驱动。
- Sm100 反向的质变是**累加器住 tmem**：5 个 UMMA 累加器都在 tmem，靠 16 个 warp 的**深度专门化**（reduce/compute/mma/load/relay/empty）协作，mma 与 compute 解耦。
- Sm100 的 dQ 归约比 Sm90 多一跳「**tmem→rmem**」（`tcgen05.copy`），再 r2s、再批量原子加进 gmem `mdQaccum`。
- **2CTA dQ reduce**：cluster `(2,1)` 内两 CTA 经 relay warp + cluster mbarrier 交换 dS，dQ 的 M 行按 `stage_offset` 切分各自归约；所有按 CTA 计的量（MMA M 维、`tx_count`、同步线程数）翻倍。
- **块稀疏反向**：SM90/SM100 都支持，靠 `get_total_q_block_count_bwd` / `get_m_block_from_iter_bwd` 把连续 m_block 迭代换成稀疏迭代；但 **2CTA 与块稀疏互斥**（显式 assert）。
- 三代 dQ 策略可压缩为：**rmem 原子加（Sm80）→ smem 批量原子加（Sm90）→ tmem 归约 + smem 批量原子加（Sm100）**；数学等价，差别在累加器位置与原子加指令。

## 7. 下一步学习建议

- 若想深挖 Blackwell 前向的同类机制，读 u8-l1（Blackwell 前向全景）与 u8-l3（UMMA descriptor 与 blackwell_helpers），本讲的 `gemm_ptx_w_idx`、`cta_group`、tmem 分区都来自那里。
- 2CTA 的死锁陷阱与排查方法见 u8-l4（hd256 2CTA 专用 kernel）与 u11-l5（GPU kernel 调试与 PTX/SASS），尤其 `AI/DEBUG_2CTA.md` 的 printf 二分法对本讲的 relay/cluster mbarrier 同样适用。
- 块稀疏的数据结构基础见 u10-l1（块稀疏注意力），本讲只涉及反向接入；`BlockSparseTensors` 的 `dq_write_order` 字段在那里有完整定义。
- MLA（DeepSeek 风格多头潜在注意力）的反向是 Sm100 反向的一个特化分支，见 u10-l2，其 `(head_dim, head_dim_v)=(192,128)` 形状正是本讲 2CTA 分支的主要服务对象。

# Blackwell 前向 Kernel 全景

## 1. 本讲目标

本讲进入专家层，剖析 FA4 在 Blackwell（SM100/SM110）架构上的前向 kernel `FlashAttentionForwardSm100`。它是 FA4 中「最复杂、最高性能、特性最全」的前向实现。

读完本讲你应该能够：

- 说清 **UMMA（tcgen05 矩阵乘单元）** 与 Ampere/Hopper 的 MMA 在指令层面有什么本质区别；
- 解释 **片上 tmem 累加**（accumulator 落在 tmem 而非寄存器）为什么能同时省寄存器、省 smem 往返；
- 画出 **persistent kernel（持久化 kernel）** 的运行模型：一个 CTA 在 `while` 循环里连续吃多个 work tile；
- 知道 SplitKV、Paged KV、2CTA 这三大高级特性是如何「集成」进同一条 kernel 主干的，以及它们之间的互斥/退化关系；
- 能在源码里定位上述每一项特性对应的代码段或类。

本讲只看「全景与主干」。2CTA 的死锁排查、tile scheduler 的 CLC 动态调度、MMA descriptor 字段拆解分别在 u8-l2 / u8-l4 / u8-l3 单独深入。

## 2. 前置知识

本讲假设你已掌握（这些是前置讲义建立的认知，本讲不再重复）：

- **在线 softmax / tiling 骨架**（u1-l1、u4-l1）：Q 常驻、K/V 分块流水、`row_max/row_sum/rescale` 三步推进。Blackwell 没有改变这套数学。
- **Ampere/Hopper 前向主循环**（u6-l1、u6-l2）：`gemm(Q,K)→acc_S → online_softmax → gemm(P,V)→acc_O` 的两段 GEMM 节奏，以及 gmem↔smem↔rmem 三级存储边界。
- **TMA 与 mbarrier**（u5-l2、u5-l3）：`cp.async.bulk` 单线程发整块搬运、mbarrier 按 `complete_tx::bytes` 计字节数判定完成、命名屏障与旗标同步。
- **BlockInfo / SeqlenInfo / AttentionMask**（u3 系列）：`get_n_block_min_max` 决定一个 Q tile 要遍历哪些 n block。
- **SplitKV 与 pack_gqa 的上层语义**（u7-l1、u7-l2）。

需要新引入的 Blackwell 硬件术语：

| 术语 | 含义 |
|------|------|
| **tcgen05** | Blackwell 第 5 代 Tensor Core 指令族（`tcgen05.mma` 等）的总称 |
| **UMMA** | Unified MMA，tcgen05 的矩阵乘指令。操作数可来自 smem 或 **tmem**，累加器必在 tmem |
| **tmem（tensor memory）** | Blackwell 新增的、挂在 tcgen05 单元旁的片上存储（每 CTA 约 256 KB / 512 列），与寄存器文件、smem 并列的第四级存储 |
| **CTA group / 2CTA** | 一条 UMMA 指令可由 `cta_group::2` 跨集群内两个 CTA 协作算更大的行块 |
| **persistent kernel** | CTA 数固定为 SM 数量级，每个 CTA 在循环里反复领取新 tile，而非「一个 tile 一个 CTA」 |
| **CLC** | Cooperative Launch Control，硬件动态 work-stealing 调度器（u8-l2 详讲） |

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [`flash_attn/cute/flash_fwd_sm100.py`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py) | 本讲主角。`FlashAttentionForwardSm100` 类，约 3150 行，包含 host 端 `__call__` 与 device 端 `kernel` 及 Load/MMA/Softmax/Correction/Epilogue 五类 warp 实现 |
| [`flash_attn/cute/blackwell_helpers.py`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/blackwell_helpers.py) | UMMA 的 PTX 内联汇编封装（`gemm` / `gemm_ptx` / `gemm_ptx_precomputed_varname` 等）与 idesc/smem descriptor 工具 |
| [`flash_attn/cute/named_barrier.py`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/named_barrier.py) | `NamedBarrierFwdSm100`：Blackwell 前向用到的编号屏障枚举（TmemPtr、SoftmaxStatsW0..W7、Epilogue） |
| [`flash_attn/cute/tile_scheduler.py`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/tile_scheduler.py) | `SingleTileScheduler` / `StaticPersistentTileScheduler` / `SingleTileLPTScheduler` / `SingleTileVarlenScheduler` 与 `SchedulingMode`、`ClcState` |
| [`flash_attn/cute/mma_sm100_desc.py`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/mma_sm100_desc.py) | UMMA 的 idesc / smem descriptor 构造（u8-l3 详讲） |
| [`flash_attn/cute/paged_kv.py`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/paged_kv.py) | `PagedKVManager`：非 TMA 分页 KV 的散列 gather（u7-l3 讲过，本讲只看它如何被 Sm100 kernel 接入） |

文件头部的注释本身就列出了支持矩阵，建议先读一眼：

- 支持：BF16/FP16、非因果/因果、MHA/GQA/MQA、hdim 64/96/128/(192,128)、varlen、sliding window、split-kv（见 [flash_fwd_sm100.py:1-11](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L1-L11)）。

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：

1. **4.1 UMMA / tcgen05 GEMM** —— 新的矩阵乘指令长什么样。
2. **4.2 片上 tmem 累加** —— 累加器搬进 tmem，连带 softmax 全流程留片上。
3. **4.3 persistent kernel 运行模型** —— 一个 CTA 干多个 tile，scheduler 来派活。
4. **4.4 全特性集成** —— SplitKV / Paged KV / 2CTA 如何挂进同一条主干。

### 4.1 UMMA / tcgen05 GEMM

#### 4.1.1 概念说明

Ampere 的 MMA（`mma.sync`，16×8×16）和 Hopper 的 WGMMA（`wg.mma`，64 行，操作数取 smem、异步、累加器在寄存器）你已经见过。Blackwell 把这条线再推一步，得到 **tcgen05.mma（UMMA）**，它有三个关键不同：

1. **累加器在 tmem，不在寄存器。** UMMA 写入的目标 `[tmem_acc]` 是 tmem 地址。这意味着巨大的累加器（一个 128×128 的 fp32 累加块就是 16384 个 fp32）不占寄存器文件，省下的寄存器可以养更深的流水、更大的 tile。
2. **操作数 A 可以来自 tmem。** 对 PV 这一段 GEMM（`P @ V`），P（softmax 概率）由 softmax warp 写进 tmem 后，UMMA 直接从 tmem 读 P 当操作数 A，**P 全程不进 smem**。
3. **`cta_group::2` 跨 CTA 协作。** 一条 UMMA 指令可以让集群（cluster）里两个 CTA 合算一个 `2×m_block_size` 行的大 tile，这就是 2CTA。

一句话总结：**UMMA 把「累加器」和「GEMM 的输入」都搬到了 tmem，从而把寄存器解放出来，并让 softmax↔MMA 之间免 smem 往返。**

#### 4.1.2 核心流程

UMMA 在 kernel 里的使用分两段 GEMM，对应你已经在 Ampere/Hopper 见过的节奏，只是把指令换了：

```
QK 段：  Q(smem) · K(smem)^T  --tcgen05.mma-->  S(tmem)        # 操作数都来自 smem
softmax: S(tmem) --读--> softmax warps --写--> P(tmem)
PV 段：  P(tmem) · V(smem)    --tcgen05.mma-->  O(tmem)        # A 来自 tmem, B 来自 smem
```

注意 PV 段 A 的来源是 tmem，这一点在构造 tiled MMA 时就被显式声明为 `OperandSource.TMEM`。host 端构造两个 tiled MMA（QK 与 PV）的代码在 [flash_fwd_sm100.py:476-500](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L476-L500)：

```python
cta_group = tcgen05.CtaGroup.TWO if self.use_2cta_instrs else tcgen05.CtaGroup.ONE
...
p_source = tcgen05.OperandSource.TMEM          # PV 段的 P 来自 tmem
p_major_mode = tcgen05.OperandMajorMode.K
tiled_mma_qk = sm100_utils_basic.make_trivial_tiled_mma(
    self.q_dtype, q_major_mode, k_major_mode, self.qk_acc_dtype, cta_group, self.mma_tiler_qk[:2])
tiled_mma_pv = sm100_utils_basic.make_trivial_tiled_mma(
    self.v_dtype, p_major_mode, v_major_mode, self.pv_acc_dtype, cta_group, self.mma_tiler_pv[:2], p_source)
```

真正发射 UMMA 指令的地方在 MMA warp 内。QK 段用 `gemm_ptx_precomputed_varname`、PV 段用 `gemm_ptx_partial`，二者都被预先 `partial` 绑定好累加器地址，见 [flash_fwd_sm100.py:1590-1642](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L1590-L1642)：

```python
gemm_Si = [partial(sm100_utils.gemm_ptx_precomputed_varname,
            self.tmem_s_offset[stage], smem_desc_base_b=k_smem_base,
            ..., kind=qk_mma_kind, zero_init=True, cta_group=self.cta_group_size)
           for stage in range(self.q_stage)]
gemm_Pi = [partial(sm100_utils.gemm_ptx_partial, pv_mma_op, self.tmem_o_offset[stage],
            tOrP[None, None, None, stage], sA=None, ..., cta_group=self.cta_group_size)
           for stage in range(self.q_stage)]
```

注意 `self.tmem_s_offset[stage]` 是 S 累加器的 tmem 地址，`self.tmem_o_offset[stage]` 是 O 累加器的 tmem 地址——累加器位置是手算出来的常量地址（见 4.2）。

#### 4.1.3 源码精读

底层 PTX 长什么样？看 [blackwell_helpers.py:1036-1096](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/blackwell_helpers.py#L1036-L1096) 里的内联汇编，最核心的一行是：

```ptx
elect.sync _|leader_thread, -1;                       ; 只让一个线程发射
...
@leader_thread tcgen05.mma.cta_group::{cta_group}.kind::{kind} [tmem_acc],
               {smem_var_name_prefix}_0, smem_desc_b_0, {idesc_var_name}, {pred_str};
```

这段汇编说明三件事：① 只有一个 leader 线程（`elect.sync`）发射指令，UMMA 由整个 CTA/warp-group 隐式执行；② 累加器是 `[tmem_acc]`（tmem 地址）；③ `cta_group` 是 1 或 2，直接决定是否 2CTA。

PV 段的 `gemm_ptx_partial` 走的是同族逻辑，只是操作数 A 换成 tmem 指针（`[tmem_a]`）而非 smem descriptor——见 [blackwell_helpers.py:580](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/blackwell_helpers.py#L580) 那条 `[tmem_acc], [tmem_a], smem_desc_b, ...` 指令。

> 对照：Hopper WGMMA 的累加器在寄存器（`acc` 是 rmem tensor），P 要先落到 smem 再被 WGMMA 读。Blackwell 把 P 留 tmem，省掉这一趟 smem 写/读——这是「片上 tmem 累加」带来的直接红利之一。

#### 4.1.4 代码实践

**实践目标**：在源码里确认「QK 段操作数都来自 smem、PV 段操作数 A 来自 tmem」这一差异。

**操作步骤**：

1. 打开 [flash_fwd_sm100.py:476-500](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L476-L500)，找到 `p_source = tcgen05.OperandSource.TMEM`，确认它只传给了 `tiled_mma_pv`（PV 段），而没有传给 `tiled_mma_qk`。
2. 打开 [flash_fwd_sm100.py:1590-1642](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L1590-L1642)，对比 `gemm_Si`（QK，传 `smem_desc_base_b=k_smem_base`）与 `gemm_Pi`（PV，传 `sA=None` 且绑定 `tOrP` 这个 tmem tensor）。

**需要观察的现象**：QK 的 partial 把 smem descriptor 当 B；PV 的 partial 把 tmem 上的 `tOrP` 当 A、`sA=None`（因为 A 不走 smem）。

**预期结果**：你能用一句话写出「为什么 PV 段的 `sA=None`」——因为 P 已经在 tmem 里，UMMA 直接从 tmem 取 A，不需要 smem descriptor。

**待本地验证**：若你有 B200，可设 `CUTE_DSL_KEEP_PTX=1` 编译一次 Sm100 kernel，在生成的 PTX 里 `grep tcgen05.mma`，应能看到 QK 段指令两个操作数都是 smem descriptor、PV 段指令第一个操作数是 `[tmem_a]`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 UMMA 只让一个线程（`elect.sync` / `@leader_thread`）发射指令，而不是像 Ampere `mma.sync` 那样所有线程都参与发射？

**参考答案**：UMMA 是 warp-group 级 / CTA 级指令，整条指令的语义由硬件隐式映射到所有参与线程的寄存器与 tmem 上；多线程同时发射同一条指令是冗余且未定义行为，故只由 leader 发射一次，硬件负责广播执行。

**练习 2**：`cta_group` 取 1 或 2 分别对应什么？它和 `use_2cta_instrs` 是什么关系？

**参考答案**：`cta_group::1` 单 CTA 执行，`cta_group::2` 集群内两个 CTA 协作执行（2CTA）。代码里 `cta_group = TWO if self.use_2cta_instrs else ONE`，且 `self.cta_group_size = 2 if self.use_2cta_instrs else 1`（[flash_fwd_sm100.py:171](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L171)），二者一一对应。

---

### 4.2 片上 tmem 累加

#### 4.2.1 概念说明

「片上 tmem 累加」是 Blackwell kernel 区别于前代最核心的架构特征。它包含三层含义：

1. **S、P、O 三个矩阵都住在 tmem 里**，不占寄存器、不进 smem（除了最后的 O 要写回 gmem）。
   - `acc_S`（QK 累加器）→ tmem 的 S 区
   - `P`（softmax 概率）→ tmem 的 P 区
   - `acc_O`（PV 累加器，即最终输出）→ tmem 的 O 区
2. **softmax 全流程留片上**：softmax warp 从 tmem 读 S、算 `row_max/row_sum`、把 P 写回 tmem，全程不经过 smem。
3. **O 的 rescale（在线 softmax 修正）也在 tmem 里做**：所谓 correction warp，就是从 tmem 读 O、乘以 rescale 因子、再写回 tmem。

为什么这是革命性的？前代里 `acc_O` 是每个 warp 的寄存器张量，row 数大了寄存器就爆；要把 P 从 softmax warp 传给 MMA warp 还得走 smem。Blackwell 用 tmem 这块「大、快、贴着 tcgen05」的存储一次性解决了这两件事。

#### 4.2.2 核心流程

tmem 的「分配」与「分区」是这个模块的核心。每个 CTA 能用 **512 列** tmem（`tmem_alloc_cols`），kernel 把它切成若干命名区域：

```
tmem 512 列（示意，hdim_v=128, n_block=128, q_stage=2）
┌──────────────┬──────────────┬──────────────┬──────────────┐
│ S0 / P0 区   │ S1 / P1 区   │ O0 区        │ O1 区        │
│ cols [0,128) │ cols[128,256)│ cols[256,384)│ cols[384,512)│
└──────────────┴──────────────┴──────────────┴──────────────┘
```

- S 与 P 共用一段：P 比 S「窄」（P 是 v_dtype 宽度，S 是 fp32），用 `tmem_s_to_p_offset = n_block_size // 2` 把 P 错开放在 S 区域的另一半，复用物理列。见 [flash_fwd_sm100.py:294-307](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L294-L307)。
- O 区紧跟其后，每个 stage 占 `head_dim_v_padded` 列。

整个 tmem 累加的生命周期：

```
MMA warp:    tmem.allocate(512列) ──► 拿到 tmem_ptr
             QK gemm ──► 写 S(tmem)
                          │ signal (mbar)
             softmax warp:读 S(tmem) ─► 算 row_max/row_sum ─► 写 P(tmem)
                          │ signal
             MMA warp:    PV gemm(P@V) ─► 累加进 O(tmem)
                          │ signal
             correction:  读 O(tmem) ─► 乘 scale ─► 写回 O(tmem)   # rescale
                          ...
             epilogue:    O(tmem) ──t2s──► smem ──s2g──► gmem      # 唯一一次离片
MMA warp:    tmem.free(tmem_ptr)
```

#### 4.2.3 源码精读

**tmem 分区地址**在 `__init__` 里手算（[flash_fwd_sm100.py:294-300](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L294-L300)）：

```python
self.tmem_s_offset = [0, self.n_block_size]                       # S0, S1 起始列
self.tmem_o_offset = [self.tmem_s_offset[-1] + self.n_block_size  # O0, O1 起始列
                      + i * self.head_dim_v_padded for i in range(self.q_stage)]
self.tmem_total = self.tmem_o_offset[-1] + self.head_dim_v_padded
assert self.tmem_total <= self.tmem_alloc_cols                    # 不能超 512 列
```

**tmem 分配器**用 CUTLASS 的 `TmemAllocator`，由 MMA warp 独占发起（[flash_fwd_sm100.py:885-891](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L885-L891)）：

```python
tmem = cutlass.utils.TmemAllocator(
    storage.tmem_holding_buf.ptr,
    barrier_for_retrieve=tmem_alloc_barrier,     # 用命名屏障 TmemPtr 同步
    allocator_warp_id=self.mma_warp_id,          # 只有 MMA warp 负责分配
    is_two_cta=self.use_2cta_instrs,
    two_cta_tmem_dealloc_mbar_ptr=storage.tmem_dealloc_mbar.ptr,
)
```

MMA warp 在自己的分支里真正分配并取回指针（[flash_fwd_sm100.py:1187-1189](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L1187-L1189)），其它 warp（softmax/correction）则通过 `tmem.wait_for_alloc()` 等它就绪后再 `retrieve_ptr`：

```python
tmem.allocate(cute.arch.get_max_tmem_alloc_cols("sm_100"))
tmem.wait_for_alloc()
tmem_ptr = tmem.retrieve_ptr(self.qk_acc_dtype)
```

> 关键认知：所有 warp 共享同一块 tmem（地址常量已知），靠 **mbarrier / 命名屏障** 约定「谁现在有权写哪段」，而不是靠拷贝传递。这就是 `pipeline_s_p_o`、`pipeline_o_acc`、`SoftmaxStatsW0..W7` 等同步原语的用意。

**O 的 rescale（在线 softmax 修正）纯在 tmem 内做**，见 `correction_rescale`（[flash_fwd_sm100.py:2674-2723](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L2674-L2723)）：用 `tcgen05.copy` 的 tmem load 把 O 读进寄存器 fragment、`mul_packed_f32x2` 乘 scale、再用 tmem store 写回——一次 smem 都不碰。

```python
cute.copy(thr_tmem_load, tOtO_t2r_i, tOrO_frg)          # tmem -> rmem
for j in ...: tOrO_frg[j], tOrO_frg[j+1] = cute.arch.mul_packed_f32x2(...)  # 乘 scale
cute.copy(thr_tmem_store, tOrO_frg, tOtO_r2t_i)         # rmem -> tmem
cute.arch.fence_view_async_tmem_store()
```

**唯一的离片**发生在 epilogue：O 从 tmem 经 smem 写到 gmem。`correction_epilogue` 用 `get_tmem_load_op` 把 O 从 tmem 拷到寄存器、做完最终缩放与类型转换后 `cvt_copy` 进 smem（[flash_fwd_sm100.py:2791-2801](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L2791-L2801)），再由 epilogue warp 或 correction warp `s2g` 写回 gmem。

#### 4.2.4 代码实践

**实践目标**：把「tmem 分区」与「warp 分工」对上号，理解 tmem 是被多类 warp 共享的临界资源。

**操作步骤**：

1. 读 [flash_fwd_sm100.py:254-273](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L254-L273)，把 16 个 warp 的分工抄下来：
   - `softmax0/1_warp_ids=(0..3)/(4..7)`、`correction_warp_ids=(8..11)`、`mma_warp_id=12`、`epilogue=(13,)`、`load=(14,)`、`empty=(15,)`，共 16 warp = 512 线程。
2. 读 [flash_fwd_sm100.py:294-307](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L294-L307)，算出 `hdim_v=128,n_block=128,q_stage=2` 时 S0/S1/O0/O1 各自的列区间。

**需要观察的现象**：哪几类 warp 会「碰」tmem 的 O 区？（答：MMA 写、correction 读改写、epilogue 读。）

**预期结果**：你能填出一张「tmem 区域 × warp 权限」表，说明 O 区被这三类 warp 在不同 pipeline 阶段交替访问，靠 `pipeline_o_acc` 的 mbarrier 保证「写完才读」。

**待本地验证**：本实践为源码阅读型，无需 GPU；若想验证地址计算，可在 Python 里复现 `tmem_o_offset` 公式打印区间。

#### 4.2.5 小练习与答案

**练习 1**：为什么 correction（rescale O）不直接在 smem 或寄存器里做，而要绕「tmem→rmem→tmem」？

**参考答案**：因为 O 累加器是 tcgen05 的累加目标，物理上就在 tmem；softmax 修正发生在 PV 累加的间隙，O 还要继续被后续 PV GEMM 累加，不能搬到 smem（搬了就没法被 UMMA 累加）。所以最省事的就是原地 tmem 改写：读出来、乘 scale、写回去。

**练习 2**：相比 Hopper，tmem 累加省下了哪一类 smem 往返？

**参考答案**：省下了「softmax 把 P 写到 smem、PV GEMM 再从 smem 读 P」这一趟。Blackwell 里 P 直接从 tmem 被 UMMA 当操作数 A 读走（`p_source=TMEM`）。

---

### 4.3 persistent kernel 运行模型

#### 4.3.1 概念说明

「Persistent kernel（持久化 kernel）」是相对「一个 work tile 启动一个 CTA」的传统模型而言的。传统模型下，tile 数可能远多于 SM 数，每个 CTA 算完一个 tile 就结束，由硬件排队等下一个 tile 轮到该 SM。persistent 模型则：

- **启动的 CTA 数 ≈ SM 数**（每个 SM 常驻一个 CTA）；
- 每个 CTA 进入一个 `while work_tile.is_valid_tile:` 循环，**主动向 scheduler 索取下一个 tile**，算完再要，直到没有 tile 为止；
- 好处：减少 kernel 启动/CTA 调度开销，便于做软件 pipeline 跨 tile 复用，也支持 CLC 这种硬件 work-stealing。

在 Sm100 kernel 里，persistent 是默认行为（`is_persistent=True`），且**每一类 warp 都各自跑同一个 while 循环**——Load warp、MMA warp、Softmax warp、Correction warp、Epilogue warp 都在 `tile_scheduler` 上对齐推进，靠 mbarrier 在 warp 之间传递「第 i 个 tile 的数据已就绪」。

#### 4.3.2 核心流程

scheduler 选型逻辑（[flash_fwd_sm100.py:243-250](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L243-L250)）：

```
is_varlen_q        → SingleTileVarlenScheduler
is_causal/is_local → SingleTileLPTScheduler   # LPT = Lazy Persistent Tile
                    或 use_clc_scheduler       # CLC 动态调度
is_persistent      → StaticPersistentTileScheduler
否则               → SingleTileScheduler      # 传统一 tile 一 CTA
```

`scheduling_mode` 只有两种取值（[flash_fwd_sm100.py:241](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L241)）：`CLC` 或 `STATIC`。注意 CLC 不能与某些特性共存，会自动退化（见 [flash_fwd_sm100.py:228-232](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L228-L232)）。

每个 device 端 warp 的循环骨架完全一致，以 MMA warp 为例（[flash_fwd_sm100.py:1656-1668](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L1656-L1668)）：

```python
work_tile = tile_scheduler.initial_work_tile_info()
while work_tile.is_valid_tile:
    m_block, head_idx, batch_idx, split_idx = work_tile.tile_idx   # 逻辑坐标
    seqlen = SeqlenInfoCls(batch_idx)
    ...                                                                # 算这个 tile
    work_tile = tile_scheduler.advance_to_next_work()                # 要下一个
# End of persistent scheduler loop
```

`work_tile.tile_idx` 是一个四元组 `(m_block, head_idx, batch_idx, split_idx)`——scheduler 负责把硬件/静态的工作划分映射成这个逻辑坐标，kernel 主体只认逻辑坐标。SplitKV 的 `split_idx` 就是被 scheduler 编进 work tile 的（见 4.4）。

#### 4.3.3 源码精读

**host 端算 grid**：`__call__` 把问题规模打包成 `TileSchedulerArguments`，再由 scheduler 算出 grid 形状（[flash_fwd_sm100.py:643-673](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L643-L673)）：

```python
tile_sched_args = TileSchedulerArguments(
    cute.ceil_div(cute.size(mQ.shape[0]), _num_block_divisor),  # m_block 数
    cute.size(mQ.shape[2]),                                       # head 数
    ..., num_splits, ..., is_persistent=self.is_persistent, ...)
tile_sched_params = TileScheduler.to_underlying_arguments(tile_sched_args, scheduling_mode=self.scheduling_mode)
grid_dim = TileScheduler.get_grid_shape(tile_sched_params)
```

persistent 模式下 `get_grid_shape` 会把 CTA 数压到 ~SM 数；非 persistent 则约等于总 tile 数。最终 launch（[flash_fwd_sm100.py:778-784](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L778-L784)）：

```python
).launch(grid=grid_dim, block=[self.threads_per_cta, 1, 1],
         cluster=self.cluster_shape_mnk if cute.size(self.cluster_shape_mnk) > 1 else None,
         stream=stream, min_blocks_per_mp=1)
```

**CLC 模式**会额外启一个 scheduler warp（`clc_scheduler_warp_id`），它只负责向硬件 CLC 单元 `prefetch_next_work`，把硬件返回的 work tile 喂给其它 warp——见 [flash_fwd_sm100.py:2937-2958](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L2937-L2958)（细节留待 u8-l2）。当 CLC 关闭时，那个 warp 退化成 `empty_warp`，只跟着循环空转（[flash_fwd_sm100.py:2960-2967](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L2960-L2967)）。

> 设计要点：因为所有 warp 都在同一个 scheduler 上对齐，persistent 模型天然支持「跨 tile 的软件 pipeline」——上一个 tile 的 epilogue 与下一个 tile 的 load 可以在同一批 warp 上重叠。这也是为什么 CLC（动态派活）能进一步压尾延迟：负载不均时，先算完的 SM 主动抢下一个 tile，而不是傻等。

#### 4.3.4 代码实践

**实践目标**：确认「每一类 warp 都跑同一个 while 循环、都调 `advance_to_next_work`」。

**操作步骤**：在 `flash_fwd_sm100.py` 里搜索 `while work_tile.is_valid_tile` 与 `advance_to_next_work`，统计它们出现在哪些方法里。

**需要观察的现象**：应能在 `load`、`mma`、`softmax_loop`、`correction_loop`、`epilogue_s2g`、`clc_scheduler_warp`、`empty_warp` 这 7 处都看到同一个循环骨架。

**预期结果**：写一句话结论——「persistent 模型靠 7 个独立 warp 循环在 `tile_scheduler` 上同步推进，每个循环各管一段流水（load/load→mma→softmax→correction→epilogue），靠 mbarrier 跨 warp 传递每 tile 的就绪信号」。

**待本地验证**：纯源码阅读型实践。

#### 4.3.5 小练习与答案

**练习 1**：什么情况下 `is_persistent` 会被强制关掉（退回非 persistent）？

**参考答案**：当 `overlap_sO_sQ` 为真时（`hdim_v` 较大或 split-kv 下，sO 与 sQ 复用同一块 smem），代码里 `self.is_persistent = False`（[flash_fwd_sm100.py:216-221](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L216-L221)）。因为这种 smem 复用模式下跨 tile 重叠不安全。

**练习 2**：为什么 CLC 模式需要一个专门的 scheduler warp，而 STATIC 模式不需要？

**参考答案**：STATIC 模式下，每个 CTA 用 block_idx 静态算出自己的 tile 序列，无需额外通信；CLC 是硬件动态调度，需要持续向 CLC 单元发起请求、读取返回的 work tile，这件事由专用 scheduler warp 异步完成（`prefetch_next_work`），避免打断计算 warp。

---

### 4.4 全特性集成：SplitKV / Paged KV / 2CTA

#### 4.4.1 概念说明

Sm100 kernel 是 FA4 里**唯一**把三大高级特性都集成进同一条前向主干的实现。这里的「集成」不是说它们各写一个 kernel，而是同一份 `mma`/`load`/`softmax_loop`/`correction_loop` 代码，用 `const_expr` 在编译期裁剪出不同分支。三者要点：

- **SplitKV**：把 KV 的 n_block 区间等分成 `num_splits` 段，每段由不同 work tile 并行算出部分 `O_s` 和 `LSE_s`，再由独立 combine kernel 合并（u7-l2）。在 Sm100 前向里，`split_idx` 是 work tile 四元组的一员，每个 split 写到 `mO` 的不同切片。
- **Paged KV**：KV cache 散落在页池里。Sm100 支持两条路径——`page_size==tile_n` 走 TMA（每块查一个 `page_idx`），否则走 `PagedKVManager` 的 cp.async 散列 gather（u7-l3）。开关是 `paged_kv_non_tma`（等价于关掉 `use_tma_KV`）。
- **2CTA**：集群内两个 CTA 用 `cta_group::2` 协作算一个大 tile，主要服务于 hdim=128/256 的高吞吐场景。

特性之间有 **互斥/退化** 关系，是阅读本模块最重要的「约束地图」：

| 组合 | 状态 |
|------|------|
| SplitKV 与 hdim_v ≥ 192 | 断言拒绝（[flash_fwd_sm100.py:195-197](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L195-L197)） |
| SplitKV 与 2CTA / persistent | 互斥（SplitKV 走非 persistent、单 CTA 路径） |
| CLC 与 paged-KV-non-tma / overlap_sO_sQ | 自动退化为 STATIC（[flash_fwd_sm100.py:228-232](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L228-L232)） |
| 块稀疏 + paged KV | `NotImplementedError`（[flash_fwd_sm100.py:734-735](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L734-L735)） |

#### 4.4.2 核心流程

**SplitKV** 的关键在两处：① host 端给 `mO`/`mLSE` 多加一个 split 维并取出 `num_splits`（[flash_fwd_sm100.py:425-432](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L425-L432)）；② device 端 correction/epilogue 按 `split_idx` 写到对应切片，并对「空 split」（`n_block_min >= n_block_max`）写默认 `-inf` LSE、跳过计算（[flash_fwd_sm100.py:2437-2440](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L2437-L2440)、[flash_fwd_sm100.py:2451](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L2451)）。

**Paged KV** 的关键在 load warp：TMA 路径在每个 n block 查 `page_idx = mPageTable[batch_idx, n_block]` 再发 TMA；非 TMA 路径构造 `PagedKVManager` 做散列 gather（[flash_fwd_sm100.py:1430-1449](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L1430-L1449)）。

**2CTA** 的关键在 `cta_group_size=2` 一路放大：cluster 形状 `(2,1)`、MMA tiler 的 M 维翻倍（`cta_group_size * m_block_size`）、UMMA 指令的 `cta_group::2`、tmem 释放需要跨 CTA 的 dealloc mbarrier。注意 2CTA 还要求 cluster N==1（[flash_fwd_sm100.py:235-239](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L235-L239)）。

#### 4.4.3 源码精读

**SplitKV 写 O/LSE 切片**（[flash_fwd_sm100.py:2437-2440](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L2437-L2440)）：

```python
if const_expr(self.is_split_kv):
    mO_cur = seqlen.offset_batch_Q(mO, batch_idx, dim=3)[None, None, head_idx, split_idx]
else:
    mO_cur = seqlen.offset_batch_Q(mO, batch_idx, dim=3)[None, None, head_idx]
```

LSE 同理按 `split_idx` 取切片并写每 split 的 log-sum-exp（[flash_fwd_sm100.py:2630-2631](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L2630-L2631)），无效 split 写 `-inf`，让后续 combine kernel 用 log-sum-exp 自然把它丢弃。

**Paged KV 两条加载路径**（[flash_fwd_sm100.py:1430-1449](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L1430-L1449)）：

```python
if const_expr(self.use_tma_KV):
    tKsK, tKgK = cpasync.tma_partition(tma_atom_K, ...)   # TMA 直取, 调用方传 page_idx
    paged_kv_manager = None
else:
    page_size = mK.shape[0]
    paged_kv_manager = PagedKVManager.create(mPageTable, mK, mV, FastDivmodDivisor(page_size),
        batch_idx, head_idx_kv, tidx, seqlen.seqlen_k, ..., self.n_block_size, ...)
```

每块加载前，TMA 路径算 `page_idx = mPageTable[batch_idx, n_block]`（[flash_fwd_sm100.py:1478-1486](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L1478-L1486)），非 TMA 路径调 `paged_kv_manager.load_page_table(n_block)`（[flash_fwd_sm100.py:1483-1484](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L1483-L1484)）。

**2CTA 的放大效应**：cluster与线程组要包含两个 CTA 的 warp（[flash_fwd_sm100.py:910-918](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L910-L918)）：

```python
softmax_warps_cluster = ThreadCooperativeGroup(len(self.softmax0_warp_ids) * self.cta_group_size)
correction_threads_cluster = ThreadCooperativeGroup(
    cute.arch.WARP_SIZE * len(self.correction_warp_ids) * self.cta_group_size)
```

tmem 释放也走 2CTA 专用 mbarrier（`is_two_cta`、`two_cta_tmem_dealloc_mbar_ptr`，见 [flash_fwd_sm100.py:889-890](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L889-L890)），否则两个 CTA 释放顺序错乱会出问题——这正是 u8-l4 要讲的 2CTA 死锁陷阱之一。

#### 4.4.4 代码实践

**实践目标**：把三大特性各自在源码里的「开关」与「接入点」整理成一张清单。

**操作步骤**：

1. 在 `__init__` 与 `__call__` 里搜索这四个布尔：`is_split_kv`、`paged_kv_non_tma`、`use_2cta_instrs`、`use_clc_scheduler`，记录每个被读取的位置。
2. 对每个特性，定位「它改变行为的那一行」（SplitKV 改输出布局、Paged 改 load 路径、2CTA 改 cluster/tmem 释放）。

**需要观察的现象**：注意哪些特性会触发 `assert` 或 `NotImplementedError`（互斥表）。

**预期结果**：得到一张三列表格：特性 | 开关参数 | 主要接入点（行号） | 互斥约束。

**待本地验证**：源码阅读型实践；若在 B200 上，可分别用 `num_splits>1`、`page_table`、`pack_gqa+hdim128` 跑三组输入，确认 kernel 走对应分支（用 `fa_log` 的日志级别 1 打印 TileScheduler/USE_2CTA，见 [flash_fwd_sm100.py:252](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L252)）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 SplitKV 在 Sm100 前向里限制 `head_dim_v < 192`？

**参考答案**：见 [flash_fwd_sm100.py:195-197](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L195-L197) 的断言。hdim_v≥192 时单 tile 的 O 累加器与中间张量已逼近 tmem/smem 上限，再切 split 会让 combine 路径和寄存器压力失衡，故不支持。

**练习 2**：`use_tma_KV = not paged_kv_non_tma`（[flash_fwd_sm100.py:144](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L144)）。请解释为什么「分页 KV + 非 128 page_size」会落到非 TMA 路径。

**参考答案**：TMA 要求源是一段连续、对齐的 gmem 描述符区域；当 page_size 不等于 tile_n 时，一个 tile 跨越多页且物理不连续，无法用一个 TMA descriptor 表达，只能用 `PagedKVManager` 逐行 cp.async 散列 gather。只有 `page_size==tile_n`（典型 128）时，每块恰好一页、可查一个 `page_idx` 发 TMA。

---

## 5. 综合实践：整理 Blackwell 专属特性清单

把本讲四个模块串起来，完成下面这份「Blackwell 前向专属特性清单」。这是本讲的交付物，也是后续 u8-l2/u8-l3/u8-l4 的起点。

**任务**：阅读 `FlashAttentionForwardSm100`（[flash_fwd_sm100.py:119](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L119) 起），为下表每一项填出「对应的代码段或类 + 一句话作用」。

| 特性 | 对应代码段 / 类 | 一句话作用 |
|------|----------------|-----------|
| UMMA（tcgen05.mma） | `blackwell_helpers.gemm_ptx_precomputed_varname`（[L1036](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/blackwell_helpers.py#L1036)）与 `gemm_Si`/`gemm_Pi`（[L1590-1642](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L1590-L1642)） | _自填_ |
| 片上 tmem 累加 | `TmemAllocator`（[L885](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L885)）+ tmem 分区（[L294-300](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L294-L300)）+ `correction_rescale`（[L2674](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L2674)） | _自填_ |
| persistent kernel | `while work_tile.is_valid_tile`（如 [L1656-1668](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L1656-L1668)）+ scheduler 选型（[L243-250](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L243-L250)） | _自填_ |
| 2CTA | `cta_group_size`（[L171](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L171)）+ cluster groups（[L910-918](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L910-L918)） | _自填_ |
| SplitKV | 输出布局（[L425-432](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L425-L432)）+ 写切片（[L2437-2440](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L2437-L2440)） | _自填_ |
| Paged KV | `paged_kv_non_tma`（[L139-144](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L139-L144)）+ `PagedKVManager`（[L1432-1447](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L1432-L1447)） | _自填_ |

**附加思考**（写一段话即可）：对照 u6-l2 的 Hopper kernel，指出 Sm100 kernel 多出的「三类 warp 间通信」（提示：S→softmax、softmax→P、O→correction/epilogue）为什么在 Hopper 上做不到、在 Blackwell 上能做。

> 参考方向：Hopper 的累加器在寄存器、P 要落 smem 才能给 WGMMA 用，所以 softmax↔MMA 必须经过 smem；Blackwell 有 tmem 这块共享片上存储，S/P/O 都在 tmem，softmax warp 与 MMA warp 通过 mbarrier 共享同一段 tmem，于是这些「通信」退化成「对共享 tmem 的有序读写」，无需 smem 中转。

## 6. 本讲小结

- **UMMA（tcgen05.mma）** 把累加器搬进 tmem、并允许操作数 A 直接来自 tmem；只有 leader 线程发射，`cta_group::1/2` 区分单/双 CTA。
- **片上 tmem 累加**：S、P、O 全部住在 tmem，softmax 与 O 的 rescale 都在 tmem 内原地完成，全流程唯一一次离片是 epilogue 把 O 写回 gmem。
- **persistent kernel**：16 个 warp 分成 Load/MMA/Softmax/Correction/Epilogue(+CLC/empty) 五类，每类都跑同一个 `while work_tile.is_valid_tile` 循环、在 `tile_scheduler` 上对齐，靠 mbarrier/命名屏障跨 warp 传递每 tile 的就绪信号。
- **全特性集成**：SplitKV（`split_idx` 进 work tile 四元组）、Paged KV（TMA vs `PagedKVManager` 两条 load 路径）、2CTA（`cta_group_size=2` 全面放大 cluster/tiler/tmem 释放）共用同一份主干，由 `const_expr` 编译期裁剪，且有明确的互斥/退化约束。
- Sm100 kernel 是 FA4 中特性最全、最复杂的实现，是后续 Blackwell 反向（u9-l3）、MLA（u10-l2）的基础。

## 7. 下一步学习建议

- **u8-l2 Tile Scheduler 与 CLC 动态调度**：本讲只点到 `tile_scheduler` 与 `clc_scheduler_warp`，下一讲深入 `SchedulingMode` 四种模式、`ClcState` 如何把硬件 work tile 映射成 `(m_block,head,batch,split)`。
- **u8-l3 UMMA Descriptor 与 Blackwell Helpers**：深入 `mma_sm100_desc.py` 的 idesc / smem descriptor 字段编码，理解 UMMA 如何「看懂」一块 tmem/smem。
- **u8-l4 hd256 2CTA 专用 Kernel**：本讲的 2CTA 是入门，hd256 有独立的 forward/backward kernel，下一讲结合 `AI/DEBUG_2CTA.md` 讲死锁排查。
- 若想看 Sm100 kernel 的实测与调优旋钮，可读 [flash_fwd_sm100.py:72-107](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L72-L107) 的 `_TUNING_CONFIG`（寄存器分配与 ex2 仿真频率），并在 u11-l4 的基准与配置搜索讲义里看这些旋钮如何被搜索。

# Blackwell 前向 Kernel 全景

## 1. 本讲目标

本讲带你走进 FA4 中最"重"的一块代码：Blackwell（SM100/SM110）专用前向 kernel `FlashAttentionForwardSm100`。

读完本讲，你应当能够：

1. 说清楚 **UMMA**（基于 `tcgen05` 的矩阵乘单元）是什么，它和 Ampere（`flash_fwd.py`）、Hopper（`flash_fwd_sm90.py`）的 MMA 在**累加器位置**上的本质区别。
2. 理解 Blackwell 把累加器从寄存器（rmem）搬到了片上专用存储 **tmem**（tensor memory），并能看懂源码里 tmem 是如何被分区成 S（分数）、P（概率）、O（输出）三块的。
3. 描述 **persistent kernel** 的运行模型：grid 里的 CTA 不再"一个 CTA 干一块然后退出"，而是在一个 `while work_tile.is_valid_tile` 循环里反复从 tile scheduler 领取新的工作块。
4. 知道这个 kernel 同时集成了 SplitKV、paged KV、2CTA、块稀疏、varlen、pack_gqa、CLC 调度等几乎所有高级特性，并能指出每项特性在源码里的入口。

本讲是高级（advanced）层，只看"全景与主干"。前置是你已经读过 **u6-l1（Ampere 前向主循环）** 和 **u6-l2（Hopper 前向与 TMA）**，并对在线 softmax、tiling、TMA、命名屏障有基本概念。2CTA 死锁排查、CLC 动态调度、MMA descriptor 字段拆解分别在 u8-l4 / u8-l2 / u8-l3 单独深入。

## 2. 前置知识

本讲假设你已掌握（这些是前置讲义建立的认知，本讲不再重复）：

- **在线 softmax / tiling 骨架**（u1-l1、u4-l1）：Q 常驻、K/V 分块流水、`row_max/row_sum/rescale` 三步推进。Blackwell 没有改变这套数学。
- **Ampere/Hopper 前向主循环**（u6-l1、u6-l2）：`gemm(Q,K)→acc_S → online_softmax → gemm(P,V)→acc_O` 的两段 GEMM 节奏，以及 gmem↔smem↔rmem 三级存储边界。
- **TMA 与 mbarrier**（u5-l2、u5-l3）：`cp.async.bulk` 单线程发整块搬运、mbarrier 按 `complete_tx::bytes` 计字节数判定完成、命名屏障与旗标同步。
- **BlockInfo / SeqlenInfo / AttentionMask**（u3 系列）：`get_n_block_min_max` 决定一个 Q tile 要遍历哪些 n block。
- **SplitKV 与 pack_gqa 的上层语义**（u7-l1、u7-l2）。

需要新引入的 Blackwell 硬件术语：

| 术语 | 含义 |
| --- | --- |
| **tmem**（tensor memory） | Blackwell 引入的片上专用存储，按"列"组织，是 UMMA 累加器的居所 |
| **UMMA / tcgen05.mma** | Blackwell 的矩阵乘单元及其 PTX 指令，累加器在 tmem |
| **idesc**（instruction descriptor） | 32 位指令描述符，编码一次 UMMA 的 dtype / M/N / 主轴 / 取反等 |
| **persistent kernel** | CTA 在循环里持续领活而非算完即退，减少启动开销与尾延迟 |
| **CLC**（Cooperative Launch Control） | Blackwell 硬件级动态 persistent 调度 |

> 术语提醒：本仓库源码里把 Blackwell 辅助函数习惯叫做 `sm100_utils`、`sm100_utils_basic`、`sm100_desc`，分别对应 `flash_attn/cute/blackwell_helpers.py`、CUTLASS 自带的 `cutlass.utils.blackwell_helpers`、以及 `flash_attn/cute/mma_sm100_desc.py`。看到 `sm100` 不要误以为是抽象代号，它就是 Blackwell 架构代号。

## 3. 本讲源码地图

本讲主要围绕以下文件展开：

| 文件 | 作用 |
| --- | --- |
| [flash_fwd_sm100.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py) | Blackwell 前向 kernel `FlashAttentionForwardSm100` 主体，约 3150 行，是本讲绝对主角 |
| [blackwell_helpers.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/blackwell_helpers.py) | UMMA 的 PTX 包装：把 `cute.gemm` 落成一条条 `tcgen05.mma` 内联汇编，含 `gemm_ptx_partial`、`gemm_ptx_precomputed_varname` 等 |
| [mma_sm100_desc.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/mma_sm100_desc.py) | MMA **指令描述符（idesc）** 与 **共享内存描述符** 的位域打包 |
| [tile_scheduler.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/tile_scheduler.py) | persistent 调度器：`StaticPersistentTileScheduler`、`SingleTileLPTScheduler`、`SingleTileVarlenScheduler`，以及 CLC 状态机 `ClcState` |
| [named_barrier.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/named_barrier.py) | Blackwell 前向专用命名屏障枚举 `NamedBarrierFwdSm100` |

`FlashAttentionForwardSm100` 的方法结构（行号供定位）：

- `__init__`（配置、warp 角色分配、tmem 分区、特性开关）
- `_setup_attributes`（smem 大小、流水级数 `kv_stage`）
- `__call__`（`@cute.jit`，建 TMA descriptor、smem 布局、屏障，并启动 kernel）
- `kernel`（`@cute.kernel`，warp 专门化分发 + tmem 分配）
- `load`（producer warp：TMA / cp.async 搬 Q/K/V，paged KV、块稀疏）
- `mma`（consumer warp：跑 UMMA，持有 persistent 工作循环与内层 K/V 循环）
- `softmax_loop` / `softmax_step`（softmax warp：从 tmem 读 S、算在线 softmax、把 P 写回 tmem）
- `correction_loop` / `correction_rescale` / `correction_epilogue`（correction warp：对 O 重缩放、写 LSE、回写 O）
- `epilogue_s2g`（epilogue warp：把 O 从 smem 搬回 gmem）

## 4. 核心概念与源码讲解

### 4.1 UMMA 与 MMA descriptor：在 tmem 里做矩阵乘

#### 4.1.1 概念说明

UMMA（Unified MMA）是 Blackwell 的新一代矩阵乘单元，对应 PTX 里的 `tcgen05.mma` 指令。理解它的关键在于一句话：

> **UMMA 的累加器（C 矩阵）住在片上 tmem，而不是寄存器。**

在 Ampere/Hopper 上，MMA 是"寄存器 → 寄存器"：输入 A、B 和累加器 C 都在寄存器里，MMA 把结果写回寄存器（`acc_O`、`acc_S` 都是寄存器张量）。Blackwell 的 UMMA 则是：

- A 操作数：可以来自 smem 或 tmem；
- B 操作数：来自 smem；
- **C（累加器）：在 tmem**。

为什么把累加器搬到 tmem？因为 tmem 是一块大得多的专用片上存储（SM100 上每 CTA 可分配多达数百"列"），且**不挤占通用寄存器**。注意力里 O 累加器很大（`tile_m × head_dim_v`），放寄存器会带来巨大寄存器压力；放 tmem 既省寄存器，又让一条 MMA 指令能覆盖更大的 tile。这就是 Blackwell FA 能跑更大 tile、更高吞吐的硬件根基。

为了让一条 `tcgen05.mma` 指令"知道"要算什么，硬件需要两个描述符：

1. **指令描述符 idesc（32 位）**：编码 A/B/C 的元素格式、矩阵 M/N 维度、主轴（K-major 还是 MN-major）、是否取反、是否饱和。
2. **共享内存描述符（64 位）**：编码 smem 里 B 矩阵（以及 A 矩阵如果它在 smem）的基地址、字节步长、swizzle 模式。

#### 4.1.2 核心流程

UMMA 的一次矩阵乘大致是：

```
准备 idesc (32位，由 a/b/c dtype + M/N + major 打包)
准备 B 的 smem descriptor (64位，地址+步长+swizzle)
准备 C 的 tmem 地址 (累加器在 tmem 的列偏移)
发射 tcgen05.mma.kind::{kind} [tmem_C], smem_desc_A, smem_desc_B, idesc
```

`kind` 是数据类型对应的 MMA 种类（如 `f16`、`f8f6f4`、`tf32`），由 `MmaOp` 推断。累加与否通过 `tcgen05.Field.ACCUMULATE` 字段控制：第一次写时要 `zero_init`，后续要 `ACCUMULATE`。

#### 4.1.3 源码精读

**idesc 的位域打包** 在 `mma_sm100_desc.py`：

[mma_sm100_desc.py:111-162](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/mma_sm100_desc.py#L111-L162)

这段把 A/B 格式、C 格式、取反、主轴、M/N 维度等打包成一个 32 位整数。注意 M/N 被右移（`M>>4`、`N>>3`）压缩进 5/6 位字段。同文件还有从 `MmaOp` 直接生成 idesc 的便捷函数：

[mma_sm100_desc.py:165-174](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/mma_sm100_desc.py#L165-L174)

kernel 里正是用 `sm100_desc.mma_op_to_idesc(qk_mma_op)` 把 CUTLASS 的 `MmaOp` 转成 idesc。共享内存描述符由 `make_smem_desc_base`（同文件 L212 起）和 `smem_desc_base_from_tensor`（L290 起）构建。

**UMMA 的 PTX 包装** 在 `blackwell_helpers.py`。最贴近实际使用的两个是：

- `gemm_ptx_partial`：[blackwell_helpers.py:395-615](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/blackwell_helpers.py#L395-L615) —— 用于 P@V，支持 `split_arrive`（先把 P 的一部分写好就通知 MMA 开跑，做计算-搬运重叠），累加器地址是传入的 tmem 列偏移。
- `gemm_ptx_precomputed_varname`：[blackwell_helpers.py:1035-1115](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/blackwell_helpers.py#L1035-L1115) —— 用于 Q@K^T，复用预先声明好的 PTX 寄存器变量以减少指令数。

它们最终都内联出形如 `tcgen05.mma.cta_group::{N}.kind::{kind} [tmem_acc], ...` 的 PTX。其中 `cta_group` 字段就是 2CTA 的开关（见 4.4）。

**在 kernel 里如何调用 UMMA**：`mma` 方法在循环外把两个 GEMM 绑成 partial：

[flash_fwd_sm100.py:1590-1642](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L1590-L1642)

这里 `gemm_Si` 用 `gemm_ptx_precomputed_varname`（Q@K^T→S，累加器在 `self.tmem_s_offset[stage]`），`gemm_Pi` 用 `gemm_ptx_partial`（P@V→O，累加器在 `self.tmem_o_offset[stage]`）。注意 `zero_init` 语义和 `cta_group=self.cta_group_size`，后者直接决定发射单 CTA 还是双 CTA 指令。

真正发射发生在循环里，例如首次 QK：

[flash_fwd_sm100.py:1705-1710](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L1705-L1710)

`gemm_Si[stage](smem_desc_start_b=...)` 算完 S 后立刻 `pipeline_s_p_o.producer_commit_w_index(stage)`，通知 softmax warp "S 已就绪"。

> 小结：UMMA = 一条 `tcgen05.mma` 指令，输入 B 来自 smem 描述符、累加器 C 在 tmem、运算语义由 32 位 idesc 决定。FA4 把它包成 `gemm_Si` / `gemm_Pi` 两个 partial 函数，在主循环里反复调用。

#### 4.1.4 代码实践

**目标**：亲手看清"一次 Q@K^T 在 Blackwell 上对应哪段代码、用了什么描述符"。

**步骤**：

1. 打开 `mma_sm100_desc.py`，找到 `make_instr_desc`（L111）。对照本讲 4.1.3 的位域表，回答：fp16 输入、fp32 累加时，`a_format`、`b_format`、`c_format` 三个字段各是什么？（提示：`to_UMMA_format` / `to_C_format` 在同文件上方，约 L68-103）。
2. 打开 `flash_fwd_sm100.py` 的 `mma` 方法（L1544），定位 `qk_mma_idesc = sm100_desc.mma_op_to_idesc(qk_mma_op)`（L1576）和 `declare_ptx_idesc`（L1584）。确认 idesc 是**编译期常量**（被 `const_expr` 内联进 PTX），这就是为什么换 dtype / tile 会触发重编译。
3. 定位 `gemm_Si[stage](...)`（L1705）。顺着它进到 `blackwell_helpers.gemm_ptx_precomputed_varname`，找到那条形如 `tcgen05.mma.kind::{kind} [...]` 的内联 PTX 字符串。

**需要观察的现象**：你会看到累加器地址是 `self.tmem_s_offset[stage]`（一个 tmem 列号），而不是某个寄存器变量名——这就是"累加器在 tmem"的直接证据。

**预期结果**：能用一句话讲清"Blackwell 上 `acc_S = Q @ K^T` 是怎么落成一条 PTX 指令的"。本步无需 GPU，属于源码阅读型实践。

#### 4.1.5 小练习与答案

**练习 1**：为什么 FA4 在 Blackwell 上几乎不用 `cute.gemm`（`blackwell_helpers.gemm`，L96）那种"高级"封装，而是大量用 `gemm_ptx_*` 系列手写 PTX 包装？

**参考答案**：高级封装难以表达 FA 需要的精细控制：`split_arrive`（P 边写边通知）、预声明 PTX 变量减少指令、显式 `cta_group`（2CTA）、把累加器精确钉在特定 tmem 列偏移。手写 PTX 包装把这些旋钮都暴露出来，性能更优。

**练习 2**：`idesc` 里同时编码了 `a_format` 和 `b_format`，为什么还要单独的 `c_format`？

**参考答案**：累加器 C 的精度可以和输入不同。FA 里输入是 fp16/bf16，但累加器用 fp32（`qk_acc_dtype = Float32`），所以必须单独告诉硬件 C 的格式，硬件才知道怎么读/写 tmem 累加器。

### 4.2 片上 tmem 累加与 tmem 分区

#### 4.2.1 概念说明

上一节说累加器在 tmem，但 FA 同时有 **S（分数）**、**P（概率）**、**O（输出）** 三个大矩阵要放在 tmem，它们怎么排布才不打架？这就是本节要解决的"tmem 分区"问题。

关键设计：

- tmem 被抽象成"列"的集合（SM100 上每 CTA 最多 512 列，由 `cute.arch.get_max_tmem_alloc_cols("sm_100")` 给出）。
- kernel 在 `__init__` 里预先算好 S、P、O 各自的**列偏移**，保证它们在 tmem 里互不重叠。
- 只有 **MMA warp** 负责分配/释放 tmem（它是唯一发起 UMMA 的 warp），其它 warp（softmax、correction）通过 `tmem.retrieve_ptr` 拿到同一块 tmem 的指针，再用 `tcgen05` 的 load/store 原子（`Ld32x32b`、`St32x32b`）读写。

注意 P 的特殊地位：P 既是 QK 的"输出"（S 经 softmax 后变 P），又是 PV 的"输入"。FA4 把 P 复用 S 的 tmem 区域（`tmem_s_to_p_offset`），省一块 tmem。

#### 4.2.2 核心流程

tmem 分区的伪代码（以 `m_block=128, n_block=128, head_dim_v=128, q_stage=2` 为例）：

```
tmem 列布局（512 列上限）:
[0      .. 128)   → S0 / P0  (S 与 P 共用，P 偏移 n//2)
[128    .. 256)   → S1 / P1
[256    .. 384)   → O0       (head_dim_v=128 列)
[384    .. 512)   → O1
合计 tmem_total = 384 + 128 = 512 ≤ 512 ✓
```

运行时流程：

1. MMA warp `tmem.allocate(max_cols)` 分配整块 tmem，`wait_for_alloc()` 等所有依赖 warp 就位。
2. 各 warp `retrieve_ptr` 拿到 tmem 基址，再用 `__init__` 里算好的偏移定位各自的 S/P/O。
3. MMA warp 跑 UMMA 把结果写进 tmem 的 S/O 区；softmax warp 从 tmem 读 S、写 P；correction warp 读 O 做 rescale。

#### 4.2.3 源码精读

**tmem 分区在 `__init__` 里一次算好**：

[flash_fwd_sm100.py:294-307](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L294-L307)

要点：

- `tmem_s_offset = [0, n_block_size]`：两个 stage 的 S 区（循环缓冲，`s_stage=2`）。
- `tmem_o_offset`：S 区之后紧接着排 O，每个 stage 占 `head_dim_v_padded` 列。
- `tmem_s_to_p_offset = n_block_size // 2`：P 复用 S 区，靠这个偏移错开（P 比 S 窄，因为 P 存 fp16 概率而 S 是 fp32 分数）。
- `assert self.tmem_total <= self.tmem_alloc_cols`：守卫不越界。

**累加器张量是 tmem fragment**：在 `kernel` 里，S 和 O 的累加器用 `thr_mma.make_fragment_C(...)` 创建，这是"指向 tmem"的张量：

[flash_fwd_sm100.py:1043-1056](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L1043-L1056)

注意 L1046 把 `tOtO` 的指针加上 `tmem_o_offset[0]`，把它钉到 O 区；L1052-1056 给 P（`tOrP`）设置跨 stage 的步长。这正是"累加器在 tmem"的代码体现。

**MMA warp 独占 tmem 分配**：

[flash_fwd_sm100.py:1186-1214](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L1186-L1214)

`tmem.allocate` → `wait_for_alloc` → 干活 → `relinquish_alloc_permit` → `free`。`tmem_alloc_barrier`（`NamedBarrierFwdSm100.TmemPtr`）让 softmax/correction warp 等到 MMA warp 分配好 tmem、拿到指针后再 `retrieve_ptr`。

**softmax 从 tmem 读 S、写 P**：

[flash_fwd_sm100.py:1923-1943](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L1923-L1943)

这里用 `tcgen05.copy.Ld32x32bOp`（tmem→寄存器，读 S）和 `St32x32bOp`（寄存器→tmem，写 P、写 scale）。softmax 在寄存器里算完在线 softmax 的 `row_max/row_sum/rescale`，再把 P 写回 tmem 给 PV 的 UMMA 用。这就是"累加器在 tmem、计算在寄存器、用 tcgen05 copy 桥接"的完整数据通路。

#### 4.2.4 代码实践

**目标**：手算一次 tmem 分区，验证不会越界。

**步骤**：

1. 假设 `m_block_size=128, n_block_size=128, head_dim=128, head_dim_v=128, q_stage=2`。按 L294-307 的公式手算：`tmem_s_offset`、`tmem_o_offset`、`tmem_total`。
2. 改成 `head_dim_v=192`（如 MLA 风格的 `head_dim, head_dim_v = 128, 192`），再算一次 `tmem_total`，看是否仍 ≤ 512。
3. 在源码里确认 `tmem_alloc_cols` 的来源（`cute.arch.get_max_tmem_alloc_cols("sm_100")`，L261）。

**需要观察的现象**：当 `head_dim_v` 变大，O 区占的 tmem 列成比例增加，可能逼近 512 列上限——这正是为什么大 head_dim 的配置要在 tile 大小/流水级数上做让步。

**预期结果**：能写出一张"给定配置 → tmem 各区列范围"的小表。属于源码阅读 + 纸笔演算型实践，无需 GPU。

#### 4.2.5 小练习与答案

**练习 1**：为什么 P 要复用 S 的 tmem 区域，而不是单独开一块？

**参考答案**：P 和 S 在时间上错峰——softmax 把 S 读出来算完就不再需要 S，转而需要写 P；随后 PV 用完 P 后也不再需要 P，下一轮又需要新的 S。复用同一块 tmem 能显著省存储，让大 tile 成为可能。

**练习 2**：softmax/correction warp 并不发起 UMMA，为什么它们也要 `retrieve_ptr`？

**参考答案**：它们要用 `tcgen05.copy`（`Ld/St 32x32b`）读写 tmem 里的 S、P、O，而这些 copy 指令同样需要 tmem 的列地址。只有拿到 tmem 基址 + 自己的偏移，才能定位要读写的列。

### 4.3 persistent kernel 运行模型

#### 4.3.1 概念说明

传统 kernel 是"一次性"的：grid 里的每个 CTA 算一个输出块（一个 `(m_block, head, batch, split)` 组合），算完就退出。当输出块数量远大于 CTA 数时，硬件调度器要不断启动新 CTA，且各 CTA 负载不均会产生**尾延迟**（有的 CTA 早干完闲着，有的还在算）。

**persistent kernel** 的做法是：启动固定数量的 CTA（通常等于 SM 数），每个 CTA 进入一个循环：

```
work = scheduler.initial_work_tile_info()
while work.is_valid_tile:
    处理 work 这一块
    work = scheduler.advance_to_next_work()   # 领下一块
```

CTA 一直循环到所有工作被领完。好处：省去反复启动 CTA 的开销，且工作动态分发，负载更均衡。

本 kernel 里**每个 warp 角色都各自跑一遍这个 persistent 循环**（load、mma、softmax、correction、epilogue），它们靠 tile scheduler 拿到**一致的工作序列**，再用流水线屏障（pipeline）彼此同步。换句话说，"哪个 CTA 算哪个块"由 scheduler 决定，"块内的各阶段由谁做"由 warp 专门化决定。

#### 4.3.2 核心流程

调度器选择（在 `__init__` 里依据特性开关）：

```
if varlen_q:           SingleTileVarlenScheduler    # 变长，单 tile（不 persistent）
elif causal/local/CLC: SingleTileLPTScheduler       # L2-persistent-tile（L2 局部性优化）
elif is_persistent:    StaticPersistentTileScheduler # 静态 persistent
else:                  SingleTileScheduler           # 单 tile
```

LPT（L2 Persistent Tile）是一种特殊调度：让多个 batch/head 的"同一组 tile"尽量落在同一组 CTA 上，提高 L2 cache 命中率（KV 被复用）。工作坐标 `work_tile.tile_idx` 是四元组 `(m_block, head_idx, batch_idx, split_idx)`。

#### 4.3.3 源码精读

**调度器选择**：

[flash_fwd_sm100.py:243-252](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L243-L252)

注意 `is_persistent` 由 `interface.py` 传入：只有在**非因果、非滑窗、非 varlen、非 SplitKV**时才为 persistent（见 4.4 的取舍）。

**MMA warp 里的 persistent 主循环**：

[flash_fwd_sm100.py:1656-1684](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L1656-L1684)

`while work_tile.is_valid_tile:` 是 persistent 的标志。每个 work tile 解出 `(m_block, head_idx, batch_idx, split_idx)`，用 `block_info.get_n_block_min_max` 算出这块要遍历的 K/V 范围，`block_iter_count = n_block_max - n_block_min` 是内层循环次数。循环末尾：

[flash_fwd_sm100.py:1828-1829](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L1828-L1829)

`advance_to_next_work()` 领取下一块。

**内层 K/V 循环**（在 persistent 循环之内）：

[flash_fwd_sm100.py:1720-1787](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L1720-L1787)

这段是 FA 的算法内核：每个 n block 先 `gemm_Pi`（P@V→O，`zero_init` 控制是否累加），再 `gemm_Si`（Q@K→下一块 S），交替推进，对应"Q 常驻、K/V 流水"的主循环。

**静态 persistent 调度器的推进逻辑**：

[tile_scheduler.py:358-369](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/tile_scheduler.py#L358-L369)

`advance_to_next_work` 把 `_tile_idx` 加上 `grid_dim()`（CTA 数）或 `cluster_dim()`（2CTA 时按 cluster 步进），实现"每个 CTA 隔 grid 个块领一块"的经典 persistent 步进。`get_current_work` 把线性 `_tile_idx` 映射回 `(m_block, head, batch, split)` 四元组。

**load/softmax/correction 各自的同构循环**：例如 producer 在 [flash_fwd_sm100.py:1361-1362](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L1361-L1362) 起同样的 `while`，末尾 [L1535](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L1535) `advance_to_next_work`。softmax/correction 同理。它们靠 pipeline（见 4.4 与 u5 系列）保证"同一块上各阶段顺序执行"。

> 小结：persistent = "CTA 不退出，循环领活"。本 kernel 把它和 warp 专门化叠加：scheduler 负责"算哪块"，warp 角色负责"块里干什么"，pipeline 负责让它们对齐。

#### 4.3.4 代码实践

**目标**：理解 persistent 步进，并对比它和单 tile 的 grid 大小差异。

**步骤**：

1. 读 `StaticPersistentTileScheduler.advance_to_next_work`（tile_scheduler.py L364），确认步长是 `grid_dim()[0]`。
2. 读 `interface.py` 里 SM100 分支（约 L869-941）传给 kernel 的 `is_persistent` 表达式：`not causal and not local and cu_seqlens_q is None and seqused_q is None and not is_split_kv`。
3. 在 `__call__` 里找到 `grid_dim = TileScheduler.get_grid_shape(...)`（约 L673），思考：persistent 时 grid 通常接近 SM 数；单 tile（如 causal）时 grid 等于总 m_block 数 × head × batch。

**需要观察的现象**：把一个 `seqlen=2048, heads=8, batch=2` 的非因果输入从 causal=False 改成 causal=True，理论上 grid 会从"≈SM 数"变成"=m_block×head×batch"。前者 CTA 数少但每个 CTA 干很多块，后者 CTA 数多但每个只干一块。

**预期结果**：能解释"为什么 causal 模式下通常退化为单 tile（LPT）而非静态 persistent"。属于源码阅读型实践。若本地有 Blackwell GPU，可用 `CUTE_DSL_KEEP_PTX=1` 观察 grid 维度差异（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：persistent kernel 里，如果总工作块数不能被 CTA 数整除，会怎样？

**参考答案**：没问题。每个 CTA 用 `_tile_idx += grid_dim` 步进，当 `_tile_idx` 超出总块数时 `get_current_work` 返回 `is_valid_tile=False`，循环自然结束。最后多出来的几块由先完成的 CTA 领走——这正是 persistent 减少尾延迟的原理。

**练习 2**：为什么 causal/local 不用静态 persistent 而用 LPT 单 tile？

**参考答案**：因果掩码下各 m_block 的工作量严重不均（上面的 m_block 要遍历的 K/V 少，下面的多），静态等步进 persistent 会放大不均。LPT（L2 persistent tile）通过 L2 局部性重排遍历顺序，既缓解不均又提升 cache 命中，更适合 causal。

### 4.4 全特性集成：SplitKV / paged KV / 2CTA / 块稀疏 / varlen / pack_gqa / CLC

#### 4.4.1 概念说明

`FlashAttentionForwardSm100` 是 FA4 里"集大成"的 kernel，它在一个 kernel 里同时支持多项高级特性。理解它在于看清**这些特性各自在源码里的开关与边界**，以及它们之间的互斥/组合关系。

- **SplitKV**：把长 KV 切成 `num_splits` 段并行算，每段产出部分 O + LSE，再由 combine kernel 合并（见 u7-l2）。本 kernel 里 `is_split_kv` 把 split 维度编进工作坐标 `(m_block, head, batch, split_idx)`。
- **paged KV**：K/V 散落在显存页池，靠 page_table 映射。本 kernel 有两条路径：`page_size==tile_n` 走 TMA 直取，否则走 `PagedKVManager` 的 cp.async 散列 gather。
- **2CTA**：cluster 里两个 CTA 协作，一条 UMMA 指令同时算两份输出，提升 MMA 利用率。
- **块稀疏（block sparsity）**：只算被掩码保留的 KV 块。
- **varlen / pack_gqa**：变长序列与 GQA 头折叠（见 u3-l3、u7-l1）。
- **CLC**：Cooperative Launch Control，Blackwell 硬件级的动态 persistent 调度，比软件 static persistent 更省指令、负载更均衡。

这些特性并非随意叠加，而是有明确的互斥与降级规则。

#### 4.4.2 核心流程

特性进入 kernel 的总开关大多在 `__init__` 里以 `cutlass.Constexpr` 或布尔字段存在，并在 `__call__`/`kernel` 里用 `const_expr` 裁剪分支（编译期特化）。例如：

```
use_tma_KV  = not paged_kv_non_tma        # 页大小==tile_n 才用 TMA 取 KV
use_tma_O   = 复杂条件（见下）             # pack_gqa/非对齐/varlen 时回退 cp.async
use_2cta_instrs → cta_group_size, cluster_shape_mn=(2,1)
use_clc_scheduler → 走 ClcState 硬件调度
```

互斥规则举例：SplitKV 与 2CTA、CLC 不组合；块稀疏与 paged KV 不组合；CLC 要求 `use_tma_KV` 且不能 `overlap_sO_sQ`。

#### 4.4.3 源码精读

**(1) SplitKV**：工作坐标带 `split_idx`，且有大 head_dim 守卫：

[flash_fwd_sm100.py:195-197](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L195-L197)

内层范围由 `block_info.get_n_block_min_max(seqlen, m_block, split_idx, num_splits)` 切分（见 u3-l2），空 split 用 `n_block_min < n_block_max` 跳过（L1678-1683）。部分 O+LSE 的合并由独立的 combine kernel 负责（u7-l2）。注意 FA4 里 **SplitKV 仅 SM100/SM110 前向支持**。

**(2) paged KV**：开关 `use_tma_KV = not paged_kv_non_tma`（[L144](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L144)）。非 TMA 路径在 producer 里创建 `PagedKVManager`：

[flash_fwd_sm100.py:1430-1449](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L1430-L1449)

TMA 路径则在加载每个 n block 时查一次 `page_idx = mPageTable[batch_idx, n_block]`（L1478-1482、L1500-1504）。SM100 还会把 V 在 gmem 里转置（详见 u7-l3）。

**(3) 2CTA**：`use_2cta_instrs` 让 `cta_group_size=2`、`cluster_shape_mn=(2,1)`，并让 MMA tiler 的 M 维覆盖两个 CTA：

[flash_fwd_sm100.py:171-180](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L171-L180)

UMMA 调用时把 `cta_group=self.cta_group_size` 透传（L1609、L1639），内联 PTX 就变成 `cta_group::2`。CLC 路径还要求 `cluster_shape_mn[0] == cta_group_size`（L237-239）。

**(4) 块稀疏**：`use_block_sparsity` 改变工作量的统计与加载方式。MMA warp 用 `get_total_block_count` 代替 `n_block_max-n_block_min`（L1664-1676），producer 用 `produce_block_sparse_loads_sm100` 按稀疏结构加载（L1515-1532），softmax 用 `softmax_block_sparse_sm100`。块稀疏与 paged KV 互斥：

[flash_fwd_sm100.py:733-735](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L733-L735)

**(5) varlen / pack_gqa**：varlen 走 `SingleTileVarlenScheduler`（L243-244）。pack_gqa 在 `__call__` 里折叠 Q/O/LSE 的布局：

[flash_fwd_sm100.py:553-558](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L553-L558)

并影响 `use_tma_O` 的取值（折叠后若 `m_block % qhead_per_kvhead != 0` 就回退 cp.async 输出，[L188-192](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L188-L192)）。

**(6) CLC 调度**：`use_clc_scheduler` 启用硬件 CLC，构造 `ClcState`：

[flash_fwd_sm100.py:1092-1132](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L1092-L1132)

`ClcState`（[tile_scheduler.py:41-91](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/tile_scheduler.py#L41-L91)）封装硬件调度器返回的 work tile 和一条异步 pipeline。CLC 把"领下一块"从软件循环变成硬件原子操作，更省、更均衡（详见 u8-l2）。它有专职 warp `clc_scheduler_warp`（L292、L1138-1143）。

**(7) 命名屏障全景**：所有这些特性的同步最终落在 `NamedBarrierFwdSm100`：

[named_barrier.py:15-25](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/named_barrier.py#L15-L25)

`Epilogue`、`TmemPtr`、`SoftmaxStatsW0..W7` 各管一类同步（见 u5-l3）。8 个 `SoftmaxStatsW*` 屏障是因为 8 个 softmax warp 要把各自的局部统计汇聚到 correction warp，需要每条通道独立屏障。

> 小结：UMMA + tmem + persistent 是骨架；SplitKV/paged/2CTA/块稀疏/varlen/pack_gqa/CLC 是挂在这副骨架上的特性，每个都以编译期开关裁剪分支，互斥规则在 `__init__` 与 `interface.py` 里以 assert 守卫。

#### 4.4.4 代码实践

**目标**：整理一份"Blackwell 专属特性清单"，把每项特性对应到具体代码段/类——这正是本讲的总实践任务。

**步骤**：

1. 打开 `flash_fwd_sm100.py`，按本节给出的行号定位每项特性的入口：UMMA 调用（L1590-1642）、tmem 分区（L294-307）、persistent 循环（L1656-1684）、SplitKV（L195）、paged KV（L1430-1449）、2CTA（L171-180）、块稀疏（L1664-1676、L733-735）、varlen（L243-244）、pack_gqa（L553-558）、CLC（L1092-1132）。
2. 做一张表，三列：`特性 | 关键字段/参数 | 源码位置（文件:行）`。
3. 在表后用一句话写出每项特性的"互斥伙伴"（如：CLC 与 overlap_sO_sQ 互斥；块稀疏与 paged KV 互斥）。

**需要观察的现象**：你会发现几乎所有特性开关都是 `cutlass.Constexpr`/`const_expr`，意味着它们在编译期就被固化——这正是为什么换一个特性组合会触发重新编译（呼应 u11-l2）。

**预期结果**：得到一张可长期维护的"特性—代码"对照表。属于源码阅读型实践，是后续调试/改造 Blackwell kernel 的索引。本步无需 GPU。

#### 4.4.5 小练习与答案

**练习 1**：`use_tma_O` 在哪些情况下会变成 `False`？为什么？

**参考答案**：见 L188-192：当 `pack_gqa` 且 `m_block % qhead_per_kvhead != 0`（折叠后 tile 边界不对齐），或 `pack_gqa + is_split_kv`，或 `is_varlen_q` 时为 False。原因是 TMA bulk store 需要规则的、对齐的输出块；这些情况下输出形状不规则，只能回退到 cp.async 逐元素回写（`use_correction_warps_for_epi = not use_tma_O`，让 correction warp 兼任 epilogue）。

**练习 2**：CLC 调度为什么要求 `use_tma_KV`？

**参考答案**：CLC 的硬件领活与 TMA 的异步搬运是配套设计的——硬件在领下一块的同时，TMA 已经按硬件指示开始搬对应 KV。如果走 cp.async（软件发起）就破坏了这种"硬件全权调度"的契约，所以 `use_clc_scheduler` 必须 `and self.use_tma_KV`（L228-232）。

## 5. 综合实践

把本讲的四块知识串起来，完成一份 **"Blackwell 前向 kernel 数据流与特性地图"** 文档：

1. **数据流图**：画出在 persistent 模式下，一次 work tile 内的数据流：
   - producer warp：gmem →（TMA）→ smem（Q/K/V，多级循环缓冲）
   - mma warp：smem K + smem Q →（UMMA）→ tmem S；softmax 处理后 tmem P + smem V →（UMMA）→ tmem O（累加）
   - softmax warp：tmem S →（tcgen05 load）→ 寄存器算在线 softmax →（tcgen05 store）→ tmem P + tmem scale
   - correction/epilogue warp：tmem O →（tcgen05 load）→ 寄存器 rescale → smem O →（TMA/cp.async）→ gmem O；同时写 gmem LSE
   - 在图上标注每一步用的 pipeline 屏障（`pipeline_q`/`pipeline_kv`/`pipeline_s_p_o`/`pipeline_p_lastsplit`/`pipeline_o_acc`/`pipeline_sm_stats`/`pipeline_o_epi`）。
2. **特性矩阵**：把 4.4 的特性表扩展为"特性 × 是否影响 tmem 分区 / 是否影响 scheduler 选择 / 是否改变 grid 形状 / 是否需要独立 warp"的四列矩阵。
3. **对比 Hopper**：写一段话，列出本 kernel 相对 `FlashAttentionForwardSm90`（u6-l2）的三处本质差异：(a) 累加器从寄存器搬到 tmem；(b) warp 专门化（softmax/correction/epilogue 独立 warp）取代 Hopper 的 intra-wg-overlap；(c) persistent + CLC 调度取代固定 grid。

这是一份纯源码阅读型综合实践，产出是文档而非可运行代码，但它会成为你后续阅读 Blackwell 反向（u9-l3）、hd256 2CTA（u8-l4）、MLA（u10-l2）时的"总索引图"。

## 6. 本讲小结

- **UMMA（`tcgen05.mma`）** 是 Blackwell 的矩阵乘单元，与 Ampere/Hopper 的本质差异是**累加器住在片上 tmem 而非寄存器**；运算语义由 32 位 **idesc**（`make_instr_desc`）和 64 位 smem 描述符共同指定。
- **tmem 分区**：kernel 在 `__init__` 把 tmem 列按 S/P/O 预先划分（`tmem_s_offset`/`tmem_o_offset`/`tmem_s_to_p_offset`），P 复用 S 区，只有 MMA warp 负责分配/释放，其余 warp 靠 `tcgen05.copy` 读写。
- **persistent kernel**：CTA 在 `while work_tile.is_valid_tile` 循环里反复领活，靠 `StaticPersistentTileScheduler`/`SingleTileLPTScheduler` 决定"算哪块"；每个 warp 角色各跑一遍同构循环，用 pipeline 对齐。
- **warp 专门化**：16 个 warp 分成 softmax0/softmax1/correction/mma/epilogue/load/empty 等角色，各自方法（`load`/`mma`/`softmax_loop`/`correction_loop`/`epilogue_s2g`）在 `kernel` 里按 `warp_idx` 分发。
- **全特性集成**：SplitKV、paged KV、2CTA、块稀疏、varlen、pack_gqa、CLC 全挂在这副骨架上，以编译期开关裁剪，互斥规则由 assert 守卫。
- **与 Hopper 的取舍**：用更大的 tile + tmem 累加换取吞吐，代价是更复杂的 warp 协同与屏障编排（8 个 `SoftmaxStatsW*` 屏障等）。

## 7. 下一步学习建议

- **u8-l2（Tile Scheduler 与 CLC）**：深入 `tile_scheduler.py`，理解 `SingleTileLPTScheduler` 的 L2 局部性重排，以及 CLC 硬件调度如何把"领活"原子化。
- **u8-l3（UMMA Descriptor 与 Blackwell Helpers）**：逐字段拆解 `mma_sm100_desc.py` 的位域，并通读 `blackwell_helpers.py` 的 `gemm_ptx_*` 系列，理解 2CTA 共享 mbarrier 协调。
- **u8-l4（hd256 2CTA 专用 Kernel）**：`head_dim=256` 的专用 2CTA kernel 把本讲的 2CTA 机制推到极致，是理解 cluster 协作与死锁陷阱的最佳材料。
- **u9-l3（Hopper/Blackwell 反向）**：看反向如何复用 UMMA + tmem，以及 Blackwell 反向的 2CTA dQ reduce。
- 如果你想验证本讲的特性边界，可读 `interface.py` 的 SM100 分支（约 L869-941）与 `tests/cute/test_flash_attn.py` 的参数化用例，对照确认哪些特性组合被 assert 拒绝。

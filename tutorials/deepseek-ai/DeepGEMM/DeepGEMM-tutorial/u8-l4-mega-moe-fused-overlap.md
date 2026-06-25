# 融合 mega 内核与通信重叠

## 1. 本讲目标

本讲是 Mega MoE 单元的收尾篇。前面三讲已经回答了「Mega MoE 是什么」「权重怎么变换」「CTA 怎么按 wave/expert 调度」，但还留着一个最关键的问题没有展开：**这五个阶段（EP dispatch、Linear1、SwiGLU、Linear2、EP combine）到底是怎么被塞进「同一个 kernel」里、并且让 NVLink 通信与 tensor core 计算真正重叠起来的？**

读完本讲，你应当能够：

1. 看懂 `sm100_fp8_fp4_mega_moe_impl` 这一个 kernel 内部如何按 **warp group** 划分 dispatch / GEMM / epilogue 三条并行流水线，以及它们如何用环形缓冲的 **full/empty 计数器** 做生产者-消费者握手。
2. 掌握 `layout::Workspace` 的字段布局——它是一份位于对称内存首部、承载所有跨 SM 与跨 rank 同步状态的「控制平面」，后面的数据缓冲（input/L1/L2/combine acts）顺序接在它后面。
3. 说出 `comm::grid_sync` 与 `nvlink_barrier` 的实现机制（「最后到达者翻转 tag 位」的全局原子技巧），以及它们在 mega-kernel 内部为何是**必备**的。

## 2. 前置知识

本讲默认你已掌握前置讲义的以下概念，不再重复：

- **u8-l1**：mega-kernel 的五阶段融合目标；`SymmBuffer` 对称内存与 `rendezvous` 互换地址；wave、`kNumExpertsPerWave`、ring token 预算。
- **u8-l2**：gate/up 交错（粒度 8）与 UTCCP 的 4×32 SF 转置，它们让 SwiGLU 能就地融进 L1 epilogue。
- **u8-l3**：`MegaMoEScheduler` 的 `BlockPhase{Linear1, Linear2}` 状态机；一个 wave 内「先算完所有 expert 的 L1、再回头算 L2」；SM100 固定 2-CTA cluster 要求 `kNumSMs`、`kNumL1BlockNs`、`kNumL2BlockNs` 均为偶数。
- **u6-l3**：TMA 异步拷贝与 mbarrier 的**相位翻转子机制**；`cluster_sync`（cluster 内多 CTA）与 `grid_sync`（跨 SM 汇合）；`atom`（返回旧值）与 `red`（不返回、更轻）的区别。

补一个本讲会用到的关键事实：mega-kernel 只 launch **`kNumSMs` 个 CTA**，但每个 CTA 内部把线程分成 **dispatch warps + GEMM non-epilogue warps + epilogue warps** 三组，它们跑在**同一个 SM 上、同一个 kernel 里**。也就是说，重叠不是「不同 SM 干不同阶段」，而是「同一 SM 内的不同 warp group 各自驱动不同阶段、靠共享内存屏障与工作区计数器协调」。理解这一点是理解本讲全部内容的前提。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_mega_moe.cuh](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_mega_moe.cuh) | 融合 mega 设备内核本体：warp 角色分工、dispatch pull、L1/L2 GEMM、SwiGLU、combine，以及内部所有的 `grid_sync` / `nvlink_barrier` 调用点。 |
| [deep_gemm/include/deep_gemm/layout/mega_moe.cuh](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/layout/mega_moe.cuh) | `Workspace`（控制平面：barrier、expert 计数、环形 full/empty 计数、dispatch/combine 元数据）与 `Data`/`Buffer`（数据平面）的布局定义。 |
| [deep_gemm/include/deep_gemm/comm/barrier.cuh](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/comm/barrier.cuh) | `grid_sync`（跨 SM）、`nvlink_barrier`（跨 rank）与 `cluster_sync_with_relaxed_arrive` 的实现。 |
| [csrc/jit_kernels/impls/sm100_fp8_fp4_mega_moe.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm100_fp8_fp4_mega_moe.hpp) | 宿主侧 Runtime：拼装 TMA 描述符、构造 `SymBuffer`、调启发式、`build` 并 launch。用于理解 Workspace 与数据缓冲如何被注入内核。 |
| [tests/test_mega_moe.py](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_mega_moe.py) | 多进程启动、`SymmBuffer` 字段准备与正确性/性能测试，是本讲实践任务的依据。 |

---

## 4. 核心概念与源码讲解

### 4.1 overlap 流水线：一个 kernel 内的三条并行支路

#### 4.1.1 概念说明

传统 MoE 把一次前向拆成 5 个独立的 kernel / 集合通信 op，每一步之间都要把中间结果落回 HBM、再由下一步读出，于是 **NVLink 通信（dispatch/combine）和 tensor core 计算（两次 GEMM）是串行的**，中间还夹着大量 HBM 往返。

Mega MoE 的核心思想是**把这三类工作放进同一个 kernel 的同一个 CTA 里**，让它们在同一批 SM 上**并发**推进：

- **dispatch 支路**：用 TMA 从远端 rank 把 token 数据与缩放因子 **pull** 进本地环形缓冲；
- **compute 支路**：两条 GEMM（Linear1 / Linear2）用 UMMA 在 tensor core 上算，SwiGLU 作为 L1 的 epilogue 就地融合；
- **combine 支路**：把 L2 的 BF16 结果写回远端 rank 的 combine 缓冲，再做 top-k 归约。

「并发」之所以可能，是因为这三条支路由**不同的 warp group** 驱动，彼此通过两样东西协调：① 共享内存里的 mbarrier（同 CTA 内、同 stage 内的握手）；② Workspace 里那几张 **full/empty 计数表**（跨支路、跨 stage 的生产者-消费者握手）。

#### 4.1.2 核心流程

把单个 CTA 看作一条流水线，数据在其中的流转路径（粗箭头表示同一 SM 内 warp group 之间、通过共享内存/TMEM/环形缓冲传递）：

```
dispatch warps ─pull(NVLink)─▶ [ring: l1_acts]
                                   │
                         GEMM TMA-load + MMA warps (Linear1)
                                   │
                                 [TMEM 累加器]
                                   │
                         epilogue warps: SwiGLU + cast FP8 + 算 SF
                                   │  （就地写回 l2_acts 环形缓冲）
                                   ▼
                              [ring: l2_acts]
                                   │
                         GEMM TMA-load + MMA warps (Linear2)
                                   │
                                 [TMEM 累加器]
                                   │
                         epilogue warps: 写 BF16 ─store(NVLink)─▶ 远端 combine 缓冲
                                   │
                         combine warps: top-k 归约 ─▶ y
```

关键点有三个：

1. **warp 角色分支**：内核用一串 `if/else if` 按 `warp_idx` 把线程划成不同角色，它们在同一个 SM 上同时活着。
2. **环形缓冲 + full/empty 计数**：l1/l2 acts 缓冲只有 `num_ring_tokens` 大小（不是全池 `num_max_pool_tokens`），用取模 `kNumRingBlocks` 复用槽位；dispatch 写满一个 block 就给 `l1_full_count` +1，GEMM 消费完就让 `l1_empty_count` +1，生产者看到 empty 才敢复用槽位。这就是「小工作区承载任意总 token、且 dispatch 与计算真正重叠」的根本机制。
3. **wave 内 L1→L2 的暂存**：调度器（u8-l3）在一个 wave 内先把所有 expert 的 L1 算完（`BlockPhase::Linear1`），再回头算 L2（`BlockPhase::Linear2`），让 L1 产出的 `l2_acts` 有机会填满环形缓冲，供 L2 消费。

#### 4.1.3 源码精读

**① 三条支路的 warp 角色划分。** 内核启动时线程总数 `kNumThreads = kNumDispatchThreads + kNumNonEpilogueThreads + kNumEpilogueThreads`，`__launch_bounds__(kNumThreads, 1)` 表示每 SM 恰驻留一个 CTA（寄存器吃紧）。随后用 `warp_idx` 一条链分派：

[sm100_fp8_fp4_mega_moe.cuh:331-335](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_mega_moe.cuh#L331-L335) —— dispatch warps 入口（`warp_idx < kNumDispatchWarps`）。

后续分支依次是：

- [L661](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_mega_moe.cuh#L661) `warp_idx == kNumDispatchWarps`：Linear1/2 的 **token + SFA** TMA 加载 warp；
- [L722](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_mega_moe.cuh#L722) `warp_idx == kNumDispatchWarps + 1`：**weight + SFB** TMA 加载 warp；
- [L765](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_mega_moe.cuh#L765) `warp_idx == kNumDispatchWarps + 2`：**UMMA issue warp**（仅 leader CTA 跑），负责发 tcgen05.mma 与 UTCCP；
- [L882](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_mega_moe.cuh#L882) `warp_idx >= kNumDispatchWarps + kNumMMANonEpilogueWarps`：**epilogue warps**，承担 SwiGLU、FP8 cast、SF 计算、NVLink store 与 combine 归约。

**② dispatch 支路：count → grid_sync → write expert count → nvlink_barrier → pull。** dispatch warps 先在本地用 `atomicAdd_block` 统计每个 expert 收到多少 token：

[sm100_fp8_fp4_mega_moe.cuh:357-361](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_mega_moe.cuh#L357-L361) —— 统计 expert token 数（写到共享内存的 `expert_token_count`）。

统计完后通过 `atomic_add` 把「本 SM 贡献数」累加进 Workspace 的全局 `expert_send_count`，再 `grid_sync` 保证**所有 SM 都统计完**，然后 SM 0 把每 expert 的总数通过 NVLink 通知所有 rank（`get_expert_recv_count_ptr`），完成后 `nvlink_barrier`，才开始 pull：

[sm100_fp8_fp4_mega_moe.cuh:382-411](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_mega_moe.cuh#L382-L411) —— `grid_sync`（dispatch 用 index 0）+ 写 expert count + 拉数据前的 `nvlink_barrier`。

pull 阶段用 `ptx::tma_load_1d` 从远端 rank 把 token 拉进本地环形缓冲，SF 则在拉最后一段时与之重叠：

[sm100_fp8_fp4_mega_moe.cuh:547-558](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_mega_moe.cuh#L547-L558) —— 用 1D TMA 分块拉 token，靠 `mbarrier_wait_and_flip_phase` + `tma_store_arrive/wait` 做块内流水。

**③ 环形缓冲的生产者-消费者握手（overlap 的真正落点）。** dispatch 在写满一个 `BLOCK_M` 的 token 块后，对 `l1_full_count` 做 release 语义的 `red_add`；而 GEMM load warp 在消费该块前自旋等 `l1_full_count` 达到期望值：

[sm100_fp8_fp4_mega_moe.cuh:527-533](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_mega_moe.cuh#L527-L533) —— dispatch 在 pull 前等待环形槽被消费者释放（`l1_empty_count`，防止覆盖未消费数据）。

[sm100_fp8_fp4_mega_moe.cuh:683-686](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_mega_moe.cuh#L683-L686) —— GEMM load warp 自旋等 token 到齐（`l1_full_count`）。

L1 epilogue 消费完后回填 `l1_empty_count` 并通知 L2（`l2_full_count`），L2 消费完再回填 `l2_empty_count`，形成闭环：

[sm100_fp8_fp4_mega_moe.cuh:1124-1131](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_mega_moe.cuh#L1124-L1131) —— L1 epilogue 结束：`l2_full_count +1`（通知 L2），`l1_empty_count +1`（释放 L1 槽给下一轮 dispatch）。

[sm100_fp8_fp4_mega_moe.cuh:1135-1137](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_mega_moe.cuh#L1135-L1137) —— L2 epilogue 结束：`l2_empty_count +1`（释放 L2 槽）。

> **注意**：这套 full/empty 计数是 release/acquire 语义的 GPU-scope 原子（见 4.3），它们既是同步原语，也是**流量控制**：环形缓冲只要还跟得上消费节奏，dispatch 就可以一路领先拉取，从而把 NVLink 带宽填满——这正是「通信与计算重叠」的物理来源。

**④ wave 内 L1→L2 的暂存**。两条 GEMM 共用同一个 `scheduler.for_each_block`，但通过 `BlockPhase` 区分用哪一套 TMA 描述符与形状：

[sm100_fp8_fp4_mega_moe.cuh:670-675](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_mega_moe.cuh#L670-L675) —— 按 `BlockPhase::Linear2` 切换 `tensor_map_l2_*` / `tensor_map_l1_*` 与对应 `shape_k`。

调度器在 `get_next_block` 里保证一个 wave 内先返回所有 L1 块、再返回 L2 块（详见 u8-l3），所以 epilogue 算出的 `l2_acts`（即 `tensor_map_l1_output` 指向的、由 L1 epilogue TMA store 写回的那个缓冲——见 [宿主注释 L163-L165](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm100_fp8_fp4_mega_moe.hpp#L163-L165)）能在 L2 开始前填进环形缓冲。

#### 4.1.4 代码实践

**目标**：在不实际运行 kernel 的前提下，通过阅读源码画出「一个 token 从被 pull 到被 combine 归约」在三条 warp 支路间的传递路径，并定位每一处握手用的计数器。

**操作步骤**：

1. 打开 `sm100_fp8_fp4_mega_moe.cuh`，定位 4.1.3 中的六个 `if/else if` 分支，给每个分支旁注一行它的角色。
2. 在 dispatch 支路（`warp_idx < kNumDispatchWarps`）内找到 `l1_full_count` 的写入点（约 L595-L598）；在 epilogue 支路内找到 `l1_empty_count` 的写入点（约 L1129-L1130）。
3. 在 GEMM load 支路内找到 `l1_full_count` 的等待点（约 L684-L686）；在 dispatch 支路内找到 `l1_empty_count` 的等待点（约 L530-L533）。
4. 对 L2 重复一遍：`l2_full_count`（epilogue 写、L2 load 等）与 `l2_empty_count`（L2 epilogue 写、L1 epilogue 等）。

**需要观察的现象 / 预期结果**：

- 每个 ring 槽位都有一对 full/empty 计数，构成完整的「生产→消费→释放」闭环。
- dispatch 的「等 empty」与 GEMM 的「等 full」是**反向**的：一个防覆盖、一个防空读。

**待本地验证**：在 SM100 + 多 GPU 环境下用 NCU 抓 timeline，应当能看到 NVLink pull 与 UMMA 计算在时间轴上交叠，而非首尾相接。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `l1_acts` 缓冲只分配 `num_ring_tokens` 大小，而不是 `num_max_pool_tokens`？

**参考答案**：因为环形缓冲通过 full/empty 计数做流量控制——dispatch 写满一个 block、GEMM 消费完才会释放该槽，槽位以 `pool_block_idx % kNumRingBlocks` 复用。只要消费跟得上，`num_ring_tokens`（由 ring token 预算限定，远小于最坏情况的全池容量）就足以让流水不断；这样把工作区显存压到最小，同时天然实现了通信与计算的重叠。全池 `num_max_pool_tokens` 只用于**非环形**的 combine 元数据（`TokenSrcMetadata`），因为那些要保留到 combine 阶段才用。

**练习 2**：L1 epilogue 结束时既 `l2_full_count +1` 又 `l1_empty_count +1`，这两个 `+1` 各自通知谁？

**参考答案**：`l2_full_count +1` 通知 **L2 GEMM load warp**（「这个 L2 acts 块可以读了」）；`l1_empty_count +1` 通知 **dispatch warps**（「这个 L1 acts 槽我已用完，你可以复用拉下一个 token 了」）。

---

### 4.2 Workspace 布局：对称内存里的「控制平面 + 数据平面」

#### 4.2.1 概念说明

mega-kernel 的所有协作状态——跨 SM 的栅栏计数器、每个 expert 的发送/接收 token 数、环形缓冲的 full/empty 计数、dispatch 的源索引、combine 的回写元数据——都不能放在寄存器或纯共享内存里，因为它们要被**本 rank 的所有 SM** 读写，部分还要被**其它 rank** 通过 NVLink 读写。因此这些状态全部放在 `SymmBuffer`（对称内存）的最开头，这份首部就是 `layout::Workspace`。

`Workspace` 只管「控制平面」（计数器与元数据）。真正的张量数据（输入 x、x_sf、topk_idx、topk_weights，以及 L1/L2 的 acts、combine 缓冲）则是接在 `Workspace` 后面的 `Data`/`Buffer` 链，由内核用 `get_end_ptr()` 逐段衔接。

> **关键区分**：`Workspace` 是「谁算到哪了」的账本；`Buffer` 链是「数据本身」。两者都在同一段对称内存里，但职责分明。

#### 4.2.2 核心流程

`Workspace` 在 `base` 指针处的字节布局（自顶向下）：

| 区段 | 内容 | 大小 |
| --- | --- | --- |
| 0..15 | 4 个 `uint32_t` grid sync 计数器 | 16 B |
| 16..19 | 1 个 `uint32_t` NVLink barrier 计数器 | 4 B |
| 20..27 | 2 个 `int` NVLink barrier 信号（phase 0/1） | 8 B |
| 之后 | `expert_send_count[num_experts]`（uint64） | `num_experts * 8` |
| 之后 | `expert_recv_count[num_ranks][num_experts_per_rank]`（uint64） | `num_ranks*E_r*8` |
| 之后 | `expert_recv_count_sum[num_experts_per_rank]`（uint64） | `E_r*8` |
| 之后 | `l1_full_count[num_ring_blocks]`、`l1_empty_count[...]`、`l2_full_count[...]`、`l2_empty_count[...]`（各 uint32） | `4*num_ring_blocks*4` |
| 之后 | `src_token_topk_idx[E_r][num_ranks][max_recv]`（int） | dispatch pull 用 |
| 之后（对齐到 16B） | `token_src_metadata[num_max_pool_tokens]`（`TokenSrcMetadata`） | combine 回写用 |

`Workspace::get_end_ptr()` 返回控制平面末尾，内核再从那里开始铺数据缓冲链（见 4.2.3 的 ②）。

grid sync 与 NVLink barrier 共用头部 32 字节（`kNumBarrierSignalBytes`），但用**不同的 grid sync index**（0..3）互不干扰——mega-kernel 里 dispatch 用 index 0、epilogue 用 index 1。

#### 4.2.3 源码精读

**① Workspace 的总大小与各区段。** `get_num_bytes()` 把上面每一项累加，最后对齐到 16 字节（TMA 描述符要求）：

[mega_moe.cuh:74-108](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/layout/mega_moe.cuh#L74-L108) —— 逐项累加 barrier、expert 计数、L1/L2 full/empty 计数、dispatch 源索引、combine 元数据，并对齐到 16 字节。

**② 头部 32 字节的精细划分（grid sync + NVLink）。** 注释明确写出 grid sync 计数器、NVLink 计数器与两相信号的位置：

[mega_moe.cuh:115-137](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/layout/mega_moe.cuh#L115-L137) —— `kNumMaxGridSyncCounters=4` 个 grid sync 计数器 + NVLink counter + 两相 signal，并提供 `get_grid_sync_count_ptr<kIndex>`、`get_nvl_barrier_*_ptr`。

**③ 四张环形 full/empty 计数表。** 它们是 4.1 中生产者-消费者握手的落点，按下标 `ring_block_idx` 索引：

[mega_moe.cuh:155-177](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/layout/mega_moe.cuh#L155-L177) —— `get_l1_full_count_ptr` / `get_l1_empty_count_ptr` / `get_l2_full_count_ptr` / `get_l2_empty_count_ptr`，每张表长 `num_ring_blocks`。

**④ dispatch 源索引与 combine 元数据。** dispatch 阶段每个 expert 要知道「这 token 原本来自哪个 rank 的哪个槽」，combine 阶段每个 token 要知道「把结果写回哪个 rank 的哪个 topk 槽」，分别由这两段承载：

[mega_moe.cuh:33-38](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/layout/mega_moe.cuh#L33-L38) —— `TokenSrcMetadata{rank_idx, token_idx, topk_idx}`，combine 回写用它定位远端目的地。

[mega_moe.cuh:180-194](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/layout/mega_moe.cuh#L180-L194) —— `get_src_token_topk_idx_ptr`（dispatch pull 源）与 `get_token_src_metadata_ptr`（combine 回写元数据，按**全池** `pool_token_idx` 索引，因为元数据不参与环形复用）。

**⑤ 数据平面：内核内缓冲链的衔接。** 内核构造 `Workspace` 后，用一串 `layout::Buffer(layout, num_ranks, num_max, prev.get_end_ptr())` 把 input / L1 / L2 / combine 数据缓冲逐段接在控制平面后面：

[sm100_fp8_fp4_mega_moe.cuh:96-161](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_mega_moe.cuh#L96-L161) —— 先构造 `Workspace`（控制平面），再用 `get_end_ptr()` 起 `input_token_buffer`、`input_sf_buffer`、`input_topk_idx_buffer`、`input_topk_weights_buffer`，进而 `l1_token_buffer`、`l1_sf_buffer`、`l1_topk_weights_buffer`、`l2_token_buffer`、`l2_sf_buffer`，最后 `combine_token_buffer`。

宿主侧 `get_symm_buffer_size_for_mega_moe` 用**完全相同**的布局链计算所需字节数，并返回一个 `slice` 函数把这些偏移切成 Python 可见的 `x/x_sf/topk_idx/topk_weights/l1_acts/...` 张量视图（见 [mega.hpp:67-158](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/mega.hpp#L67-L158)）。设备与宿主两份布局代码必须镜像一致，否则张量视图会错位——这是跨层阅读时要警惕的一致性约束。

#### 4.2.4 代码实践

**目标**：核对「宿主算大小 / 切视图」与「设备内构造缓冲」用的是同一份偏移链。

**操作步骤**：

1. 在 [mega.hpp:52-111](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/mega.hpp#L52-L111) 中，按顺序列出 `input_token_buffer → input_sf_buffer → input_topk_idx_buffer → input_topk_weights_buffer → l1_token_buffer → l1_sf_buffer → l1_topk_weights_buffer → l2_token_buffer → l2_sf_buffer → combine_token_buffer` 各自的 `(layout, num_ranks, num_max)`。
2. 在 [sm100_fp8_fp4_mega_moe.cuh:99-161](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_mega_moe.cuh#L99-L161) 中做同样列表。
3. 逐项对照：layout 的每元素字节数、`num_ranks`、`num_max`（宿主用 `num_ring_tokens`/`num_sf_ring_tokens`，设备用 `kNumRingTokens`/`kNumSFRingTokens`，应为同一编译期常量）。

**预期结果**：两份链顺序与每段 `(字节数, 条数)` 完全一致，只是宿主多了一个把 `l2_token_buffer` 同时当作 `tensor_map_l1_output` 的约定（post-SwiGLU 后 N 减半，见 [mega.hpp:163-170](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm100_fp8_fp4_mega_moe.hpp#L163-L170)）。任何不一致都会导致 Python 端 `buffer.x` 与设备实际读写错位。

**待本地验证**：在 `DG_COMM_KERNEL_DEBUG=1` 下，`fp8_fp4_mega_moe` 每次调用结束会 `sym_buffer.zero_()`（见 [mega.hpp:252-253](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/mega.hpp#L252-L253)），可据此确认缓冲被完整覆盖。

#### 4.2.5 小练习与答案

**练习 1**：`token_src_metadata` 用 `num_max_pool_tokens`（全池）索引，而 `l1_full_count` 用 `num_ring_blocks`（环形）索引，为什么不统一？

**参考答案**：两者生命周期不同。full/empty 计数是**短期**的流量控制，block 一被消费就可释放槽位、按 ring 取模复用，所以只需 `num_ring_blocks` 项。`TokenSrcMetadata` 是**长期**的，它要在 L2 epilogue 写回远端时被读取（记录 token 的原始 rank/token/topk），且必须按 token 的全池逻辑位置寻址（dispatch 与 combine 不在同一阶段、中间隔了整条 L1/L2 流水），所以用 `num_max_pool_tokens` 全池下标，不参与环形复用。

**练习 2**：grid sync 计数器为什么预留 4 个（`kNumMaxGridSyncCounters`），而 mega-kernel 里只用了 2 个？

**参考答案**：留余量给「同一 kernel 内并发推进的多条需要 grid 级同步的支路」复用同一份 Workspace 而互不串扰。mega-kernel 当前 dispatch 支路用 index 0、epilogue 支路用 index 1（见 [sm100_fp8_fp4_mega_moe.cuh:326-328](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_mega_moe.cuh#L326-L328)），因为这两条支路在时间上重叠，若共用一个计数器会互相污染对方的 tag 位。

---

### 4.3 grid_sync 协作：跨 SM 与跨 rank 的汇合

#### 4.3.1 概念说明

mega-kernel 内部有两类**必须**的栅栏，普通 GEMM 都用不到：

- **grid_sync（跨 SM，本 rank 内）**：dispatch 支路统计 token 数是每个 SM 各算各的，必须等**所有 SM 都统计完**、把贡献写进全局 `expert_send_count` 之后，SM 0 才能汇总并通知远端；epilogue 支路也要等所有 SM 把 L2 结果写完，才能进入 combine 归约。这些「所有 SM 到齐」的时刻需要 grid 级汇合。
- **nvlink_barrier（跨 rank）**：pull 之前要等所有 rank 都把源索引写好；combine 之前要等所有 rank 都把 L2 结果写到对方的 combine 缓冲。这些是跨 GPU 的汇合。

注意 `cooperative_groups::this_grid().sync()` 虽然能做 grid 同步，但它要求整个 grid 都能到达汇合点；mega-kernel 里只有**特定 warp group**（dispatch 或 epilogue）需要汇合，且要带上自定义的「CTA 内同步」副作用，所以 DeepGEMM 自己实现了一个等价但更轻、更可控的 `grid_sync`。

#### 4.3.2 核心流程

`grid_sync` 用「**最后到达者翻转 tag 位**」的全局原子技巧。设 \( N = kNumSMs \)，计数器初值含一个 tag 位 \( T = 2^{31} \)（`kFinishSumTag`）。

- SM 0 投入 \( T - (N - 1) \)；
- 其余每个 SM 投入 \( 1 \)。

所有 \( N \) 个 SM 都到达时，累计增量恰为

\[
\bigl(T - (N - 1)\bigr) + (N - 1)\cdot 1 = T
\]

每个 SM 在自旋循环里读取计数器，当其**最高位（tag 位）相对自己进入时的旧值发生翻转**时（即 `(new ^ old) & T != 0`），就认为所有 SM 都到齐，跳出循环。tag 位每轮汇合翻转一次，于是同一个计数器可以**被反复复用**而不必清零。

`nvlink_barrier` 在 `grid_sync` 前后各夹一次（可选），中间只让 **SM 0** 参与：SM 0 的每个 thread（最多 `kNumRanks` 个）向对应远端 rank 的 signal 槽做 `red_add_rel_sys`（sys scope，跨 GPU 可见），然后 thread 0 自旋等本 rank 的 signal 达到期望值，从而实现跨 rank 的 all-to-all 汇合。signal 用「phase（轮次）+ sign（加减方向）」两态编码，同样支持反复复用。

#### 4.3.3 源码精读

**① grid_sync 的「最后到达者翻 tag」实现。**

[barrier.cuh:21-44](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/comm/barrier.cuh#L21-L44) —— `kFinishSumTag = 0x80000000u`；SM 0 投 `kFinishSumTag-(kNumSMs-1)`、其余投 1；自旋 `ld_acq` 直到 `(new ^ old) & kFinishSumTag != 0`；含 60 秒 `kNumTimeoutCycles` 超时与诊断 printf。

其中 `sync_scope()` 是调用方传入的 lambda（如 `ptx::sync_aligned(kNumDispatchThreads, kDispatchBarrierIdx)`），在原子操作前后各做一次 CTA 内同步，确保只有 thread 0 动计数器、且全员都到齐后才继续。

**② nvlink_barrier：grid_sync + 跨 rank 信号 + grid_sync。**

[barrier.cuh:46-89](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/comm/barrier.cuh#L46-L89) —— 可选 prologue `grid_sync`；仅 SM 0：按 `(*counter_ptr) & 3` 解出 phase/sign，每个 thread 向远端 `red_add_rel_sys`，thread 0 自旋 `ld_acq_sys(signal_ptr)` 等达到 target；可选 epilogue `grid_sync`。`signal_ptr` 与 `counter_ptr` 都在 Workspace 头部（见 4.2.3②）。

`sym_buffer.map(signal_ptr, thread_idx)` 把本 rank视角的指针换成「远端 rank 视角下同一逻辑槽」的地址（[sym_buffer.cuh:33-40](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/layout/sym_buffer.cuh#L33-L40)），这才是「kernel 内访存即集合通信」能成立的原因。

**③ mega-kernel 内的三处 nvlink_barrier 与两个 grid_sync index。**

[sm100_fp8_fp4_mega_moe.cuh:311-313](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_mega_moe.cuh#L311-L313) —— 三个 NVLink barrier tag：`kBeforeDispatchPullBarrierTag=1`（pull 前，等所有 rank 写好源索引）、`kBeforeCombineReduceBarrierTag=2`（combine 前，等所有 rank 写好 L2 远端结果）、`kAfterWorkspaceCleanBarrierTag=3`（清理工作区后，为下次调用做准备）。

[sm100_fp8_fp4_mega_moe.cuh:326-328](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_mega_moe.cuh#L326-L328) —— dispatch 用 `kDispatchGridSyncIndex=0`、epilogue 用 `kEpilogueGridSyncIndex=1`，因两支路时间重叠，必须用不同计数器避免 tag 位互相污染。

三处实际调用点：dispatch pull 前 [L405-L411](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_mega_moe.cuh#L405-L411)；工作区清理后 [L654-L660](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_mega_moe.cuh#L654-L660)；combine 归约前 [L1246-L1250](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_mega_moe.cuh#L1246-L1250)。

#### 4.3.4 代码实践

**目标**：解释 grid_sync / nvlink_barrier 在 mega-kernel 里为何「删掉任何一个都会出错」。

**操作步骤**：

1. 假设删掉 [L405-L411](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_mega_moe.cuh#L405-L411) 的 pull 前 `nvlink_barrier`：则 dispatch warps 可能在**远端 rank 还没把 `src_token_topk_idx` 写好**时就执行 [L514-L515](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_mega_moe.cuh#L514-L515) 的 `get_src_token_topk_idx_ptr` 读取，读到未初始化值 → pull 错 token。
2. 假设删掉 [L382-L385](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_mega_moe.cuh#L382-L385) 的 `grid_sync`：则 SM 0 在 [L388-L401](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_mega_moe.cuh#L388-L401) 汇总 `expert_send_count` 时，其它 SM 可能还没把自己的贡献加进去 → expert 总数偏小。
3. 假设删掉 [L1246-L1250](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_mega_moe.cuh#L1246-L1250) 的 combine 前 `nvlink_barrier`：则 combine warps 在 [L1296-L1298](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_mega_moe.cuh#L1296-L1298) 读 `combine_token_buffer` 时，其它 rank 的 L2 epilogue 可能还没把结果写进本 rank 的 combine 缓冲 → top-k 归约少算。

**预期结果**：每一处栅栏都对应一个明确的「跨 SM 或跨 rank 数据依赖」，缺少它都会读到不完整数据。这正是 grid_sync/nvlink_barrier 在普通 GEMM 里不必出现、而在 mega-kernel 里**必备**的原因。

**待本地验证**：在 `DG_COMM_KERNEL_DEBUG=1` 下故意注释掉任一栅栏重新编译，预期会观察到数值错误或 grid sync timeout 的 printf（[barrier.cuh:36-40](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/comm/barrier.cuh#L36-L40)）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `grid_sync` 里 SM 0 投 `T-(N-1)`、其余投 1，而不是所有 SM 都投 1 然后等计数到 N？

**参考答案**：因为同一个计数器要被**反复复用**做多次汇合。若每次都等「累加到 N」，就必须在每轮之间把计数器清零——但清零本身又需要一次汇合，陷入先有鸡还是先有蛋。用 tag 位（最高位）编码轮次：本轮所有到达者的增量之和恰好把最高位翻转一次，下一轮再翻回来，永远不需要清零。SM 0 投 `T-(N-1)` 是为了让总和恰为 \( T \)（保证最高位确被置位），同时让低 31 位回到一个可预测的值。

**练习 2**：`nvlink_barrier` 里为什么只有 SM 0 参与跨 rank 信号，其余 SM 靠什么知道汇合完成？

**参考答案**：跨 rank 的 `red_add_rel_sys` / `ld_acq_sys` 都是昂贵操作，让全部 SM 都发信号既浪费 NVLink 带宽也竞争同一 signal 槽。因此只让 SM 0 的少量 thread 发信号并自旋等待；其余 SM 靠 `nvlink_barrier` 尾部的 `grid_sync`（prologue/epilogue，见 [barrier.cuh:56-57 与 87-88](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/comm/barrier.cuh#L56-L88)）得知「SM 0 已完成跨 rank 汇合」，于是全员随 grid_sync 一起放行。

---

## 5. 综合实践

**任务**：参照 `tests/test_mega_moe.py` 的多进程启动方式，完整说明「调用 `deep_gemm.fp8_fp4_mega_moe` 前必须准备哪些 buffer 字段」，并把它们与本讲三个模块一一对应。

**步骤**：

1. **读测试入口**：[test_mega_moe.py:38-59](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_mega_moe.py#L38-L59) 用 `init_dist` 建进程组，再 `get_symm_buffer_for_mega_moe` 分配对称内存。注意 `torch.multiprocessing.spawn` 启动 `num_processes` 个进程（[L311-L312](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_mega_moe.py#L311-L312)），这是 mega-kernel 的硬前提。

2. **列出每次调用前必须拷贝进 buffer 的字段**（见 `run_fused`，[test_mega_moe.py:104-121](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_mega_moe.py#L104-L121)）：

   | buffer 字段 | 类型 | 含义 | 对应模块 |
   | --- | --- | --- | --- |
   | `buffer.x[:num_tokens]` | FP8 e4m3 | 输入 token 激活，形状 `[num_tokens, hidden]` | 4.2 数据平面（input_token_buffer） |
   | `buffer.x_sf[:num_tokens]` | int32（打包 UE8M0） | 输入 token 缩放因子，形状 `[num_tokens, hidden/128]` | 4.2 数据平面（input_sf_buffer） |
   | `buffer.topk_idx[:num_tokens]` | int64 | 每个 token 选中的 expert，`-1` 表示 masked | 4.2 数据平面；驱动 4.1 dispatch 统计 |
   | `buffer.topk_weights[:num_tokens]` | float32 | top-k 路由权重，SwiGLU 后乘进去 | 4.2 数据平面；4.1 L1 epilogue 读取 |

   另需单独准备：变换后的权重 `transformed_l1_weights/transformed_l2_weights`（u8-l2）、输出张量 `y`（BF16，`[num_tokens, hidden]`）、可选的 `cumulative_local_expert_recv_stats`。

3. **解释为什么必须每次重拷**：`DG_COMM_KERNEL_DEBUG=1` 时每次调用结束会 `sym_buffer.zero_()`（[mega.hpp:252-253](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/mega.hpp#L252-L253)）；即便关掉，`num_tokens` 每次可能变化，必须刷新 `x/x_sf/topk_idx/topk_weights` 头部那段。

4. **解释 grid_sync 为何必备**（对应模块 4.3）：`fp8_fp4_mega_moe` 是单 kernel 多 rank 协作；dispatch 阶段每个 SM 各统计各的 expert token 数，必须 `grid_sync` 汇合后 SM 0 才能汇总；pull 前必须 `nvlink_barrier` 等所有 rank 写好源索引；combine 前必须 `nvlink_barrier` 等所有 rank 把 L2 结果写到对方 combine 缓冲。任何一个栅栏缺失都会读到不完整数据。

**预期结果**：能画出「Python 端 4 个 buffer 字段 → 设备内 input_token/sf/topk_idx/topk_weights 缓冲 → dispatch pull → L1 GEMM/SwiGLU → L2 GEMM → combine」的完整数据流，并标注沿途每个 grid_sync/nvlink_barrier 的作用。**实际运行需 SM100（arch_major==10）+ 多 GPU，本环境无法验证，标注为待本地验证。**

## 6. 本讲小结

- mega-kernel 把 **dispatch / compute / combine** 三条支路塞进同一个 CTA 的不同 **warp group**，靠共享内存 mbarrier 与 Workspace 的 full/empty 计数做生产者-消费者握手，这是「通信与计算重叠」的物理来源。
- **环形缓冲** + **full/empty 计数表**让小小的 `num_ring_tokens` 工作区就能承载任意总 token，dispatch 写满、GEMM 消费、epilogue 释放，形成闭环流量控制。
- `layout::Workspace` 是对称内存首部的**控制平面**（grid sync / NVLink barrier 计数器、expert 发送/接收数、环形 full/empty 计数、dispatch 源索引、combine 元数据），数据缓冲链顺序接在其后；设备与宿主两份布局必须镜像一致。
- `grid_sync` 用「最后到达者翻转 tag 位」的全局原子技巧实现可复用的跨 SM 汇合；`nvlink_barrier` = grid_sync + 仅 SM 0 发跨 rank 信号 + grid_sync。
- dispatch 用 grid sync index 0、epilogue 用 index 1，避免两条时间重叠的支路互相污染 tag 位；三个 NVLink barrier tag 分别守 pull 前、combine 前、清理后。
- 调用 `fp8_fp4_mega_moe` 前必须把 `x / x_sf / topk_idx / topk_weights` 拷进 `SymmBuffer`，这三者正是 dispatch 支路的输入，也是 grid_sync/nvlink_barrier 守护的数据依赖起点。

## 7. 下一步学习建议

- **横向对比 BF16 路径**：阅读 `sm100_bf16_mega_moe.cuh`（与 FP8xFP4 同构但无 SF/UTCCP），体会「去掉缩放因子后 overlap 流水线如何简化」，巩固本讲的骨架认识。
- **回到宿主侧启发式**：阅读 `csrc/jit_kernels/heuristics/mega_moe.hpp`，看 `get_mega_moe_config` 如何在「填满 SM」与「不超 ring token 预算」之间夹逼选出 `block_m/block_n/num_experts_per_wave/num_ring_tokens`，补全 u8-l3 提到的 wave 预算推导。
- **性能剖析实践**：在真实 SM100 多卡环境用 `scripts/run_ncu_mega_moe.sh`（配合 [test_mega_moe.py:280](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_mega_moe.py#L280) 的 `--ncu-profile-only`）抓 NVLink 与 UMMA 的时间线，直观验证本讲描述的「overlap」是否成立。
- **串起整个 Mega MoE 单元**：回顾 u8-l1（融合目标与对称内存）→ u8-l2（权重变换）→ u8-l3（wave 调度）→ 本讲（内核内部 overlap 与同步），至此从 Python 调用到 tensor core 执行的完整链路已全部打通。

# Blackwell SM100 集体 GEMM

## 1. 本讲目标

本讲承接 u2-l9（Hopper warp-specialized GEMM 实战），把视角从 Hopper（SM90）推进到 Blackwell（SM100）。学完后你应当能够：

- 说清 Blackwell 的两条新硬件主线——**UMMA 指令（`tcgen05.mma`）**与**张量内存（Tensor Memory, TMEM）**——以及它们如何改变内核的角色划分。
- 读懂 `CollectiveMma<MainloopSm100TmaUmmaWarpSpecialized<...>>` 这条集体主循环的内部结构：两条流水线、TMEM 累加器、producer/consumer 拆分。
- 解释 SM100 内核为什么从 SM90 的「2 个 warp group」演变成「5 类 warp」，以及 **CLC（Cluster Launch Control）动态调度器**如何取代静态 raster 调度。
- 理解 cluster（线程块簇）如何让两个 SM 通过 **2SM UMMA（`cta_group::2`）** 与分布式共享内存协作算一个更大的 tile，并知道「分布式 GEMM」在 CUTLASS 中的扩展点在哪里。

本讲只讲 dense（稠密）FP16 GEMM 这条主线；块缩放（NVFP4/MXFP）留到 u3-l6，Stream-K/Grouped 留到 u3-l3/u3-l4。

## 2. 前置知识

在进入 SM100 之前，先回顾几个 u2-l9 / u3-l1 已建立的关键认知（本讲不再重复推导细节）：

- **三段式通用模型**：CUTLASS 3.x 把一次 GEMM 拆成 `kernel::GemmUniversal` 外壳 + `CollectiveMainloop`（搬 A/B + MMA）+ `CollectiveEpilogue`（加载 C、α/β、写回 D）+ `TileScheduler`（分配 tile）。SM100 仍是这套外壳，变的只是各部件的「内馅」。
- **Hopper warp specialization**：SM90 把线程分成 **producer warp group**（DMA warp 发 TMA 搬数据）与 **consumer warp group**（发 `wgmma` 做乘加，兼跑 epilogue），靠 `PipelineTmaAsync` 多级缓冲同步。consumer warp group 同时拥有**寄存器里的累加器**，所以它必须亲自跑 epilogue 把累加器卸到显存。
- **TMA**：Hopper 引入的异步张量搬运单元，靠 128B 描述符寻址，自管越界，配合 mbarrier 实现「搬算重叠」。Blackwell 沿用 TMA，并新增跨 2SM 的 `SM100_TMA_2SM_LOAD`。

一句话总结 SM90 现状：**累加器在 consumer warp 的寄存器文件里**。这正是 Blackwell 要动刀的地方。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [examples/70_blackwell_gemm/70_blackwell_fp16_gemm.cu](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/70_blackwell_gemm/70_blackwell_fp16_gemm.cu) | 官方 Blackwell FP16 GEMM 示例，用 `CollectiveBuilder` 组装并启动内核，是本讲的入口与综合实践对象（任务规格里的 `blackwell_gemm.cu` 实际文件名为此）。 |
| [include/cutlass/gemm/collective/sm100_mma_warpspecialized.hpp](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/collective/sm100_mma_warpspecialized.hpp) | SM100 dense 集体主循环 `CollectiveMma<MainloopSm100TmaUmmaWarpSpecialized<...>>`，本讲核心。 |
| [include/cutlass/gemm/kernel/sm100_gemm_tma_warpspecialized.hpp](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/sm100_gemm_tma_warpspecialized.hpp) | SM100 内核外壳，定义 5 类 warp 角色、TMEM 分配/释放、CLC 调度衔接（讲清「谁干什么」）。 |
| [include/cute/arch/mma_sm100_umma.hpp](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/arch/mma_sm100_umma.hpp) | UMMA 指令（`tcgen05.mma`）的裸 PTX 封装，按数据类型/1SM·2SM/是否稀疏各一个 struct。 |
| [include/cute/arch/tmem_allocator_sm100.hpp](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/arch/tmem_allocator_sm100.hpp) | TMEM 分配器 `Allocator1Sm` / `Allocator2Sm`，把 `tcgen05.alloc/dealloc` 包成 C++。 |
| [include/cutlass/detail/sm100_tmem_helper.hpp](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/detail/sm100_tmem_helper.hpp) | `make_sm100_accumulator`：把累加器建成落 TMEM 的张量。 |
| [include/cutlass/gemm/kernel/sm90_gemm_tma_warpspecialized.hpp](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/sm90_gemm_tma_warpspecialized.hpp) | SM90 内核外壳，仅作对照（2 个 warp group、寄存器累加器）。 |

> 说明：任务规格列出的 `sm90_mma_tma_warpspecialized.hpp` 在本仓库中的真实文件名为 `sm90_mma_tma_gmma_ss_warpspecialized.hpp`（FP16 默认走 SS 特化）。本讲的 SM90 对照以内核文件为准。

## 4. 核心概念与源码讲解

### 4.1 UMMA 指令与 TMEM（张量内存）

#### 4.1.1 概念说明

Blackwell Tensor Core 引入了两件互相配套的新东西：

- **UMMA（Unified Matrix Multiply-Accumulate）指令 `tcgen05.mma`**：继 `mma.sync`（Volta–Ampere，warp 级、操作数在寄存器）与 `wgmma.mma_async`（Hopper，warp group 级、A/B 可来自共享内存描述符）之后的第三代 Tensor Core 指令。官方示例注释直接点明它的价值：相比 Hopper 的 WGMMA，**吞吐翻倍**。注意 WGMMA 在 Blackwell 上**不再兼容**。
- **TMEM（Tensor Memory）**：每颗 SM 专有的一块高速内存（容量 128 行 × 512 列 × 32 位）。UMMA 指令的**累加结果直接写进 TMEM，而不是寄存器文件**。

为什么必须配套？因为如果累加器还落在寄存器里，那条 warp 就得一直「抱」着一大堆寄存器，难以把 MMA 与 epilogue 分给不同 warp。把累加器搬进 TMEM 这块「公共蓄水池」后，**MMA warp 只管往 TMEM 写，epilogue warp 只管从 TMEM 读**，两者就能真正并发——这是 SM100 内核角色重排的物理基础。

示例文件开头的注释把这两点列得很清楚：

[examples/70_blackwell_gemm/70_blackwell_fp16_gemm.cu:38-53](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/70_blackwell_gemm/70_blackwell_fp16_gemm.cu#L38-L53) —— 说明了 (1) `tcgen05` 吞吐翻倍且 WGMMA 不再兼容、(2) 累加结果落 TMEM、(3) 借 TMEM 把 MMA 与 epilogue 解耦到不同 warp、(4) 基于 cluster launch control 的动态调度器。

#### 4.1.2 核心流程

一条 UMMA 指令的逻辑形态是：

\[
D_{\text{TMEM}} \;=\; A_{\text{smem\_desc}} \times B_{\text{smem\_desc}} \;+\; (\text{scaleC} \;?\; C_{\text{TMEM}} \;:\; 0)
\]

要点：

- 操作数 A、B 用 **64 位共享内存描述符**（`desc_a` / `desc_b`，`uint64_t`）寻址——硬件拿着描述符自己去共享内存取数，软件不碰具体地址。
- 累加器 C/D 用 **32 位 TMEM 地址**（`tmem_c`，`uint32_t`）寻址。
- `scaleC` 是一个谓词：`0` 表示本次「清零再累加」（K 维第一块），`1` 表示「累加到原值」（K 维后续块）。对应枚举 `UMMA::ScaleOut::{Zero, One}`。
- 还有一个 **指令描述符 `idescE`**（高 32 位）编码数据类型/swizzle 等；以及一组可选 mask（用于稀疏或更复杂场景）。
- `cta_group::1`（单 SM）/ `cta_group::2`（cluster 内 2 个 SM 协同）选择是 1SM 还是 2SM 形态。

#### 4.1.3 源码精读

先看寄存器别名，确认 A/B 是描述符、C 是 TMEM 地址：

[include/cute/arch/mma_sm100_umma.hpp:52-55](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/arch/mma_sm100_umma.hpp#L52-L55) —— `ARegisters = uint64_t[1]`、`BRegisters = uint64_t[1]`（两个 64 位描述符），`CRegisters = uint32_t[1]`（一个 TMEM 地址）。`DRegisters = void` 因为 D 就写回 C 指向的 TMEM，没有单独的 D 寄存器。

再看 FP16 的 1SM `fma`，注意它由 **单条 elect_one 线程发射**（UMMA 是 warp-group 级异步指令，整组共享一条指令的语义）：

[include/cute/arch/mma_sm100_umma.hpp:104-120](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/arch/mma_sm100_umma.hpp#L104-L120) —— 内联 PTX `tcgen05.mma.cta_group::1.kind::f16 [tmem_c], desc_a, desc_b, idescE, ..., p;`，其中 `p` 是由 `scaleC` 经 `setp.ne` 生成的谓词，决定是否累加。

2SM 形态（cluster MMA）只是把 `cta_group::1` 换成 `cta_group::2`、M 维放大到 128/256、mask 数量翻倍：

[include/cute/arch/mma_sm100_umma.hpp:549-586](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/arch/mma_sm100_umma.hpp#L549-L586) —— `SM100_MMA_F16BF16_2x1SM_SS`（命名「2x1SM」= M 方向 2 个 CTA、每 CTA 1 SM），断言 `M == 128 || M == 256`，发射 `tcgen05.mma.cta_group::2.kind::f16 ...`。

指令描述符里用到的几个枚举（K 主序/MN 主序、是否累加、swizzle 类型）定义在：

[include/cute/arch/mma_sm100_desc.hpp:59-85](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/arch/mma_sm100_desc.hpp#L59-L85) —— `UMMA::Major{K, MN}`、`UMMA::ScaleOut{Zero, One}`、`LayoutType{SWIZZLE_NONE, ...}` 等。

TMEM 的分配/释放由专用分配器封装，本质是 `tcgen05.alloc` / `tcgen05.dealloc` PTX：

[include/cute/arch/tmem_allocator_sm100.hpp:60-111](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/arch/tmem_allocator_sm100.hpp#L60-L111) —— `Allocator1Sm::allocate(num_columns, dst_ptr)` 发射 `tcgen05.alloc.cta_group::1...`，把分配到的 TMEM 基址写进**共享内存**（`dst_ptr` 指向 smem）；`free` 与 `release_allocation_lock` 成对释放。容量常量见 [tmem_allocator_sm100.hpp:45-46](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/arch/tmem_allocator_sm100.hpp#L45-L46)（`128*512*32` 位）。注意注释强调：**整组分配/释放只能由一个 warp 一致地发出**。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：把 UMMA 指令的三类操作数（A、B、C）的物理位置与类型对上号。
2. **步骤**：打开 `mma_sm100_umma.hpp`，比较 `SM100_MMA_F16BF16_SS`（[L86-L121](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/arch/mma_sm100_umma.hpp#L86-L121)）与 `SM100_MMA_F16BF16_TS`（[L171-L215](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/arch/mma_sm100_umma.hpp#L171-L215)）。注意 SS 版 A、B **都**是 `uint64_t` 共享内存描述符；TS 版 A 变成了 `uint32_t tmem_a`（A 也落 TMEM），只有 B 还是描述符。
3. **观察**：无论哪种，C 永远是 `uint32_t tmem_c`——累加器**必落 TMEM**，这是 SM100 的硬约束。
4. **预期结果**：你能用一句话概括「SS = A/B 来自 smem 描述符，TS = A 来自 TMEM、B 来自 smem 描述符，C 始终在 TMEM」。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 UMMA 的 `fma` 只在 `elect_one_sync()` 内发 PTX，而不是 32 个线程各发一次？
  - **答案**：UMMA 是 **warp-group 级异步指令**，一条指令的语义作用于整个 warp group（128 线程），由硬件广播执行；多线程重复发射不仅浪费还会出错。`elect_one_sync` 选出代表线程发一次即可。
- **练习 2**：`scaleC`（`UMMA::ScaleOut`）在 K 维循环里如何被使用？
  - **答案**：K 维第一个分块用 `Zero`（清零累加，等价 `D = A·B`），之后所有分块用 `One`（累加，等价 `D += A·B`）。

---

### 4.2 Blackwell collective（集体主循环）结构

#### 4.2.1 概念说明

SM100 的 dense 集体主循环类型是 `CollectiveMma<MainloopSm100TmaUmmaWarpSpecialized<...>>`（见 [dispatch_policy.hpp:1029-1035](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/dispatch_policy.hpp#L1029-L1035)），它和 SM90 主循环长得「像但不同」：

- **像**：都是无状态结构；都靠 TMA 把 A/B 从 gmem 搬到 smem；都用 warp specialization 把「搬」和「算」分流。
- **不同（关键三处）**：
  1. **算的指令**从 `wgmma` 换成 UMMA（`tcgen05.mma`）。
  2. **累加器落点**从寄存器换成 TMEM。
  3. **多出第二条流水线 `AccumulatorPipeline`**：因为累加器在 TMEM 这个公共区，MMA 写完一档后要通知 epilogue「这档可读了」，epilogue 读完要通知 MMA「这档可覆盖了」——这就是累加器流水线，本质是把 SM90 里「consumer 一手包办 mma+epilogue」的串行关系，拆成 MMA（producer）↔ Epilogue（consumer）的并发关系。

主循环对外暴露四个动作，分别由不同 warp 调用：`load`（producer 搬 A/B）、`load_tail`、`mma`（consumer 发 UMMA，写 TMEM）、以及配套的 `init_tmem_tensors` / `set_tmem_offsets` / `slice_accumulator`。

#### 4.2.2 核心流程

主循环内部两条流水线的协作（producer/consumer 视角）：

```
DMA warp (producer, load):
  for k in K_tiles:
    producer_acquire(mainloop_pipe)        # 等空缓冲
    bar = producer_get_barrier(mainloop_pipe)
    TMA.copy(A[k], B[k])  via *bar          # TMA 翻满门
    ++mainloop_pipe

MMA warp (consumer, mma):
  accumulator_pipeline.producer_acquire(acc_pipe)   # 等 TMEM 这档可写
  for k in K_tiles:
    mainloop_pipe.consumer_wait(...)        # 等 A/B 这档就绪
    cute::gemm(tiled_mma, tCrA[k], tCrB[k], accumulators_in_TMEM)  # 发 UMMA
    mainloop_pipe.consumer_release(...)     # 释放 A/B 这档
  # accumulators 此刻在 TMEM，由 accumulator_pipeline 通知 epilogue warp
```

注意：`accumulators` 是一个**落 TMEM 的张量**（`is_tmem<FrgEngine>` 强约束），并且多出一个 `ACC_PIPE` 维做双缓冲，让「本档在算/下档在写回」可以并行。

#### 4.2.3 源码精读

**主循环流水线类型**——注意是 `PipelineTmaUmmaAsync`（SM100 专用，比 SM90 的 `PipelineTmaAsync` 多管理 UMMA 的并发语义）：

[include/cutlass/gemm/collective/sm100_mma_warpspecialized.hpp:156-160](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/collective/sm100_mma_warpspecialized.hpp#L156-L160) —— `MainloopPipeline = cutlass::PipelineTmaUmmaAsync<Stages, ClusterShape, AtomThrShapeMNK>`。

**A/B 必须来自共享内存描述符**（这是 UMMA SS 形态的硬要求）：

[include/cutlass/gemm/collective/sm100_mma_warpspecialized.hpp:192-194](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/collective/sm100_mma_warpspecialized.hpp#L192-L194) —— `static_assert` 要求 `TiledMma::FrgTypeA/B` 都派生自 `cute::UMMA::DescriptorIterator`，且 `SmemCopyAtom` 必须为 `void`（"SM100 UMMA cannot have a non-void copy atom for smem sourced instructions"）。

**共享内存只存 A/B**——累加器不再占 smem，它有自己的 TMEM 存储：

[include/cutlass/gemm/collective/sm100_mma_warpspecialized.hpp:223-246](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/collective/sm100_mma_warpspecialized.hpp#L223-L246) —— `SharedStorage::TensorStorage` 只有 `smem_A`、`smem_B`；累加器被单独包进 `TmemStorage<AccTensor>`，是个模板结构（与 smem 物理分离）。

**TMEM 累加器的构造**——把累加器建成落 TMEM 的张量，并加一档 `ACC_PIPE` 双缓冲：

[include/cutlass/gemm/collective/sm100_mma_warpspecialized.hpp:461-480](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/collective/sm100_mma_warpspecialized.hpp#L461-L480) —— `init_tmem_tensors` 调 `make_sm100_accumulator<AccumulatorPipelineStageCount, ...>` 产出 `((MMA_TILE_M,MMA_TILE_N),MMA_M,MMA_N,ACC_PIPE)` 形状的累加器。其实现见 [sm100_tmem_helper.hpp:58-74](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/detail/sm100_tmem_helper.hpp#L58-L74)：底层是 `TiledMma::make_fragment_C(...)`，返回的 `data()` 指针即 TMEM 地址。

**producer 端 `load`**：标准 acquire → 拿 barrier → 单线程发 TMA：

[include/cutlass/gemm/collective/sm100_mma_warpspecialized.hpp:604-626](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/collective/sm100_mma_warpspecialized.hpp#L604-L626) —— `producer_acquire` 等空门；`elect_one_sync()` 内 `copy(observed_tma_load_a_->with(*tma_barrier, mcast_mask_a), tAgA, tAsA)` 发 TMA，TMA 完成时硬件自翻满门。

**consumer 端 `mma`**：两条流水线齐管，UMMA 把结果写进 TMEM 累加器：

[include/cutlass/gemm/collective/sm100_mma_warpspecialized.hpp:661-712](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/collective/sm100_mma_warpspecialized.hpp#L661-L712) —— 关键几行：
- L661 `static_assert(is_tmem<FrgEngine>::value, "Accumulator must be tmem resident.")`——编译期强约束累加器在 TMEM。
- L676 `tiled_mma.accumulate_ = UMMA::ScaleOut::Zero;`——K 维首块清零；循环内 L706 `tiled_mma.accumulate_ = UMMA::ScaleOut::One;`——后续累加（对应 4.1 讲的 `scaleC`）。
- L678 `accumulator_pipeline.producer_acquire(...)`——等 TMEM 这档可写；L702-705 `cute::gemm(tiled_mma, tCrA, tCrB, accumulators)` 发 UMMA；L684/L708 `consumer_wait`/`consumer_release` 管 A/B 主流水线。

> 这里的 `cute::gemm` 不是软件实现，而是经 u2-l3/u2-l4 讲过的 `TiledMMA` 分发到 UMMA atom——`tCrA`/`tCrB` 是共享内存描述符张量，`accumulators` 是 TMEM 张量，CuTe 在编译期据内存空间自动选到 `tcgen05.mma`。

#### 4.2.4 代码实践（源码阅读 + 对照）

1. **目标**：确认 SM100 主循环比 SM90 多了「累加器流水线」这一层。
2. **步骤**：在 [sm100_mma_warpspecialized.hpp](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/collective/sm100_mma_warpspecialized.hpp) 的 `mma`（[L643-L712](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/collective/sm100_mma_warpspecialized.hpp#L643-L712)）里数它操作了几条流水线。再打开 SM90 的 `mma` 函数对比（文件 `include/cutlass/gemm/collective/sm90_mma_tma_gmma_ss_warpspecialized.hpp`，搜 `CUTLASS_DEVICE auto mma`）。
3. **观察**：SM100 的 `mma` 签名同时吃 `(MainloopPipeline, AccumulatorPipeline)` 两条管线（[L652-L660](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/collective/sm100_mma_warpspecialized.hpp#L652-L660)）；SM90 的 `mma` 只有 `MainloopPipeline` 一条。
4. **预期结果**：你能解释「多出来的那条 `AccumulatorPipeline` 就是为了把 TMEM 累加器在 MMA 与 epilogue 之间流水化」。
5. 待本地验证：若在 Blackwell 卡上编译运行，可用 nsys 观察到 MMA warp 与 epilogue warp 在时间轴上重叠（SM90 是同 warp group 串行）。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 SM100 主循环的 `SharedStorage` 里**没有**累加器，而 SM90 有（寄存器形式）？
  - **答案**：SM100 累加器落 TMEM（专用内存），用 `TmemStorage` 表达；SM90 累加器在 consumer warp 的寄存器里，由 `partition_fragment_C` 直接分配成 `Tensor`，不占共享内存也不占 TMEM。
- **练习 2**：`IsRuntimeDataType`（[L135-L143](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/collective/sm100_mma_warpspecialized.hpp#L135-L143)）解决什么问题？
  - **答案**：FP8/FP6/FP4 这些「窄精度」类型在 Blackwell 上可用**同一条** `f8f6f4` UMMA 指令处理，具体精度放到运行时由 `idesc_.a_format_`/`b_format_` 指定（见 [mma_init L569-L574](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/collective/sm100_mma_warpspecialized.hpp#L569-L574)），避免为每种精度各编译一份内核。

---

### 4.3 Cluster 调度：5 类 warp、CLC 与 2SM MMA

#### 4.3.1 概念说明

把视角从「集体主循环」拉到「内核外壳」`sm100_gemm_tma_warpspecialized.hpp`，这里定义了**谁在哪个 warp 上跑哪段代码**。SM100 的最大变化是 warp 角色从 SM90 的 2 类裂成 **5 类**：

| WarpCategory | 线程数 | 职责 | SM90 对应 |
| --- | --- | --- | --- |
| `MMA` (0) | 1 warp | 发 UMMA，**分配/释放 TMEM**，写累加器到 TMEM | consumer warp group 的一部分 |
| `Sched` (1) | 1 warp | 查询 **CLC**（Cluster Launch Control）动态领活 | 无（SM90 用静态 raster） |
| `MainloopLoad` (2) | 1 warp | 发 TMA 搬 A/B | producer warp group（DMA warp） |
| `EpilogueLoad` (3) | 1 warp | 发 TMA 搬 C | producer warp group 的 epilogue 部分 |
| `Epilogue` (4+) | 多 warp | 从 TMEM 读累加器，跑 epilogue，写回 D | consumer warp group 的 epilogue 部分 |

核心洞察：**正是因为累加器在 TMEM，MMA 和 Epilogue 才能拆给不同 warp**——`MMA` 写 TMEM，`Epilogue` 读 TMEM，两者经 `AccumulatorPipeline` 握手。在 SM90 里，累加器攥在 consumer warp 的寄存器里，consumer 只能「先 mma、再 epilogue」串行做完。

第二条新东西是 **CLC 调度**。SM90 的持久化内核靠静态 raster 顺序领活（u3-l3）；SM100 改用硬件辅助的 **Cluster Launch Control**：`Sched` warp 通过 `PipelineCLCFetchAsync` 向硬件查询「下一个该算的 tile」，硬件动态分配，能更好地均衡负载、减轻尾波浪费。这也是示例注释里说的「SW controlled dynamic scheduler based on cluster launch control」。

第三条是 **2SM MMA（cluster MMA）**。当 cluster 在 M 方向有 2 个 CTA（`AtomThrShapeMNK` size==2）时，两个 SM 可用 `cta_group::2` 的 UMMA **合算一个 M=128 或 256 的大 tile**，相当于把单 SM 的算力再叠加。对应的 TMA 搬运换成跨 2SM 的 `SM100_TMA_2SM_LOAD`，TMEM 分配器换成 `Allocator2Sm`。两个 SM 之间通过 cluster 的**分布式共享内存（DSM）**同步。

#### 4.3.2 核心流程

内核入口 `operator()` 的角色分派（简化）：

```
warp_idx = canonical_warp_idx()
warp_category = warp_idx < 4 ? WarpCategory(warp_idx) : Epilogue

if (main_load)      do { TMA 搬 A/B; CLC 领下一活 } while(valid)
else if (sched)     do { 查 CLC; 广播下一活给全 cluster } while(valid)
else if (mma)       allocate TMEM; do { 发 UMMA 写 TMEM; 累加器流水 commit } while(valid); free TMEM
else if (epi_load)  do { TMA 搬 C } while(valid)
else if (epilogue)  wait TMEM 分配完成; do { 从 TMEM 读累加器; 跑 epilogue 写 D } while(valid)
```

5 类 warp 通过多条流水线（mainloop、epi_load、accumulator、clc、clc_throttle）和命名屏障 `tmem_allocation_result_barrier` 协同。

#### 4.3.3 源码精读

**线程数与角色枚举**：

[include/cutlass/gemm/kernel/sm100_gemm_tma_warpspecialized.hpp:137-147](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/sm100_gemm_tma_warpspecialized.hpp#L137-L147) —— `NumSchedThreads = NumMMAThreads = NumMainloopLoadThreads = NumEpilogueLoadThreads = NumThreadsPerWarp`（各 1 warp），`MaxThreadsPerBlock` 为五者之和。

[include/cutlass/gemm/kernel/sm100_gemm_tma_warpspecialized.hpp:234-248](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/sm100_gemm_tma_warpspecialized.hpp#L234-L248) —— `WarpCategory` 枚举与 `IsParticipant` 标志位。

**TMEM 分配/释放串起的 MMA 与 epilogue**：

[include/cutlass/gemm/kernel/sm100_gemm_tma_warpspecialized.hpp:178-179](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/sm100_gemm_tma_warpspecialized.hpp#L178-L179) —— `TmemAllocator` 按 `ThrLayoutVMNK` 选 `Allocator1Sm` 或 `Allocator2Sm`。

[include/cutlass/gemm/kernel/sm100_gemm_tma_warpspecialized.hpp:725-735](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/sm100_gemm_tma_warpspecialized.hpp#L725-L735) —— MMA warp 分配整块 TMEM（`Sm100TmemCapacityColumns=512` 列），把基址写进共享内存 `tmem_base_ptr`，`arrive` 命名屏障通知 epilogue，再 `set_tmem_offsets` 设好累加器偏移：

[include/cutlass/gemm/kernel/sm100_gemm_tma_warpspecialized.hpp:868-873](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/sm100_gemm_tma_warpspecialized.hpp#L868-L873) —— epilogue warp `tmem_allocation_result_barrier.arrive_and_wait()` 等 MMA 分配完，读到同一个 `tmem_base_ptr`，再 `set_tmem_offsets`——两个 warp 共享同一块 TMEM。

[include/cutlass/gemm/kernel/sm100_gemm_tma_warpspecialized.hpp:802-803](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/sm100_gemm_tma_warpspecialized.hpp#L802-L803) —— MMA warp 在所有活算完后 `tmem_allocator.free(...)` 释放 TMEM（持久化内核里 TMEM 跨波复用，必须显式释放）。

**累加器流水线把 MMA(producer) 与 Epilogue(consumer) 解耦**：

[include/cutlass/gemm/kernel/sm100_gemm_tma_warpspecialized.hpp:169-176](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/sm100_gemm_tma_warpspecialized.hpp#L169-L176) —— `AccumulatorPipeline = PipelineUmmaAsync<AccumulatorPipelineStageCount, ...>`；`CLCPipeline = PipelineCLCFetchAsync<SchedulerPipelineStageCount, ClusterShape>`。

[include/cutlass/gemm/kernel/sm100_gemm_tma_warpspecialized.hpp:519-535](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/sm100_gemm_tma_warpspecialized.hpp#L519-L535) —— `AccumulatorPipeline` 的角色：MMA 当 Producer、Epilogue 当 Consumer（`producer_arv_count = 1`，因为只有一条 elect_one 线程 commit）。

**CLC 动态调度**（`Sched` warp）：

[include/cutlass/gemm/kernel/sm100_gemm_tma_warpspecialized.hpp:680-723](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/sm100_gemm_tma_warpspecialized.hpp#L680-L723) —— `Sched` warp 经 `clc_throttle_pipeline` 限流后，`scheduler.advance_to_next_work(clc_pipeline, ...)` 向硬件查询下一个 clcID，再 `fetch_next_work` 广播给 cluster 内所有消费 warp。

**2SM MMA 的判定**：

[include/cutlass/gemm/kernel/sm100_gemm_tma_warpspecialized.hpp:426-429](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/sm100_gemm_tma_warpspecialized.hpp#L426-L429) —— `is_mma_leader_cta`（`cta_coord_v == 0`）与 `has_mma_peer_cta`（`AtomThrShapeMNK size==2`）；2SM 形态下 leader CTA 发 UMMA，peer CTA（`cta_rank ^ 1`）配合，释放靠 cluster 屏障 `tmem_dealloc`。

#### 4.3.4 代码实践（SM90 vs SM100 结构对照，本讲核心实践）

1. **目标**：对比 SM90 与 SM100 内核的 producer/consumer 结构，**指出 TMEM 在 SM100 中取代了哪个角色**。
2. **步骤 a（SM90 基线）**：读 [sm90_gemm_tma_warpspecialized.hpp:140-142](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/sm90_gemm_tma_warpspecialized.hpp#L140-L142)——只有 `NumLoadWarpGroups=1`（producer）+ `NumMmaWarpGroups=1`（consumer），共 256 线程；再看 [L302-L303](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/sm90_gemm_tma_warpspecialized.hpp#L302-L303)（`warp_group_role ∈ {Producer, Consumer}`）与 [L468-L469](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/sm90_gemm_tma_warpspecialized.hpp#L468-L469)（consumer 分支里 `Tensor accumulators = partition_fragment_C(...)`——**寄存器**累加器，且这个 consumer 接着就跑 epilogue）。
3. **步骤 b（SM100）**：读 [sm100_gemm_tma_warpspecialized.hpp:234-248](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/sm100_gemm_tma_warpspecialized.hpp#L234-L248) 的 5 类 warp，以及 4.3.3 里 MMA/Epilogue 经 `AccumulatorPipeline` 解耦的代码。
4. **观察与结论（参考答案）**：
   - SM90：2 个 warp group。**累加器住在 consumer warp group 的寄存器文件里**，所以 consumer 必须自己既发 `wgmma` 又跑 epilogue（先算后写，串行）。producer 只是搬数据。
   - SM100：5 类 warp。**TMEM 取代了「consumer 寄存器文件」作为累加器的住所**。因为累加器不再属于任何 warp 的私有寄存器，而是一块所有相关 warp 都能寻址的公共内存，所以 `MMA` warp 只管写 TMEM、`Epilogue` warp 只管读 TMEM，两者经 `AccumulatorPipeline` 并发——把 SM90 consumer「一人分饰 MMA+epilogue 两角」拆成了两个独立角色。
   - 一句话：**TMEM 取代了 SM90 中「consumer warp group 持有的寄存器累加器」这一角色，从而让 MMA 与 epilogue 能够 warp 级解耦并发**；副产品是腾出的寄存器可重新分配给 epilogue，提升整体 occupancy。
5. **预期结果**：你能画出两张时序图——SM90 单 consumer warp group 内「wgmma → epilogue」串行；SM100 的 MMA warp 与 Epilogue warp 在 `AccumulatorPipeline` 双缓冲下时间轴重叠。

#### 4.3.5 小练习与答案

- **练习 1**：SM90 的 consumer warp group 有 128 个线程，为什么 SM100 的 `MMA` 类只有 1 个 warp（32 线程）？
  - **答案**：UMMA 是 warp-group 级异步指令，但**发射**只需一条 elect_one 线程；真正干活的累加器在 TMEM（不占寄存器），所以不需要像 wgmma 那样用一整个 warp group（128 线程）去持有寄存器片段。省下的线程预算分给了独立的 `Sched`/`EpilogueLoad`/`Epilogue` warp。
- **练习 2**：`Sched` warp 的 `clc_throttle_pipeline`（[L537-L551](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/sm100_gemm_tma_warpspecialized.hpp#L537-L551)）起什么作用？
  - **答案**：限流 CLC 查询，避免调度 warp 跑得太快、与算力 warp 严重错配（skew）导致负载不均——让调度「跟着」搬运 warp 的节奏走。

---

### 4.4 分布式 GEMM 扩展点

#### 4.4.1 概念说明

「分布式」在 Blackwell 语境下有两层含义，本讲点到为止、指出扩展点：

1. **节点内（cluster 级）分布式**：上面讲的 2SM MMA + TMA multicast + DSM 已经是「cluster 内多 SM 协作算一个 tile」的分布式计算。cluster 把最多 16 个 CTA 绑在一起，CTA 间可经分布式共享内存（DSM）互访、经 `ClusterBarrier` 同步。SM100 在此基础上加了 CLC 让软件动态调度这些 CTA。这是 C++ 侧 Dense GEMM 直接用到的「分布式」。
2. **节点间（多 GPU）分布式 GEMM**：典型场景是 TP/SP 训练里的 `AllReduce + GEMM`、`ReduceScatter + GEMM` 等。CUTLASS 把这一层主要放在 **Python CuTe DSL** 与 **cute_ext** 里实现，C++ 库只提供底层原语（cluster、multimem、TMA）。这些示例利用 Blackwell 的网络/镜像内存（multimem）原语把多个 GPU 的 GEMM 与集合通信融合。

本模块重点是让你知道「往哪儿扩展」，而不是手写一遍。

#### 4.4.2 核心流程

C++ dense GEMM 走的是第 1 层：cluster → 2SM MMA（可选）→ DSM 同步 → CLC 调度。第 2 层（多 GPU）的典型融合模式：

```
# 单 GPU：本讲的 70_blackwell_fp16_gemm
GEMM(A, B) → D

# 多 GPU AllReduce-GEMM（DSL 示例）
各 GPU 本地 GEMM → 用 multimem/网络原语做 all-reduce → 融合，省一次 round-trip
```

#### 4.4.3 源码精读（扩展点指引）

**C++ 侧 cluster 配置入口**——示例里 cluster 形状是个普通模板参数：

[examples/70_blackwell_gemm/70_blackwell_fp16_gemm.cu:113-117](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/70_blackwell_gemm/70_blackwell_fp16_gemm.cu#L113-L117) —— `MmaTileShape_MNK = <_256,_128,_64>`、`ClusterShape_MNK = <_2,_2,_1>`。注释点明「tile 可能横跨 2 个 SM（当 Cluster 形状 %2==0）」。

**2SM 形态下的 TMA 搬运与 static_assert**：

[include/cutlass/gemm/collective/sm100_mma_warpspecialized.hpp:195-206](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/collective/sm100_mma_warpspecialized.hpp#L195-L206) —— `AtomThrShapeMNK size==1` 配 `SM90_TMA_LOAD(_MULTICAST)`，`size==2` 配 `SM100_TMA_2SM_LOAD(_MULTICAST)`——这正是 cluster 分布式搬运的开关。

**多 GPU 分布式扩展点（Python DSL，仅指引）**：仓库在 `examples/python/CuTeDSL/cute/blackwell/kernel/distributed/` 下提供了 `distributed_gemm_all_reduce_blackwell.py`、`distributed_all_gather_gemm_blackwell.py`、`distributed_gemm_reduce_scatter_blackwell.py`、`all_reduce_two_shot_multimem.py`、`all_reduce_one_shot_lamport.py` 等，把 GEMM 与集合通信在 kernel 内融合。这些不在本讲的 C++ 源码范围内，但它们复用的正是本讲讲的 UMMA/TMEM/cluster 这套底层抽象。

#### 4.4.4 代码实践（阅读型 + 配置型）

1. **目标**：感受 cluster 形状如何影响 tile 跨 SM 分布。
2. **步骤**：阅读 [70_blackwell_fp16_gemm.cu:113-148](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/70_blackwell_gemm/70_blackwell_fp16_gemm.cu#L113-L148)。在本地（Blackwell 卡）把 `ClusterShape_MNK` 改成 `<_1,_1,_1>` 重新编译运行，再用 nsys 对比 cluster=2x2x1 与 cluster=1x1x1 两种配置下的 UMMA 指令形态与吞吐。
3. **观察**：cluster=1x1x1 时走 `SM90_TMA_LOAD` + `cta_group::1`；cluster 含 2 个 M 方向 CTA 时走 `SM100_TMA_2SM_LOAD` + `cta_group::2`，单 tile 翻倍算力。
4. **预期结果**：理解 cluster 形状是「单 SM 算 vs 2 SM 合算」的开关。待本地验证（需要 SM100 设备与 `CUTLASS_NVCC_ARCHS=100a`）。

#### 4.4.5 小练习与答案

- **练习 1**：为什么多 GPU 分布式 GEMM 的实现主要落在 Python DSL 而不是 C++ 库？
  - **答案**：C++ 库提供 UMMA/TMEM/cluster/multimem 等底层原语；而「GEMM + AllReduce/ReduceScatter 融合」这类拓扑多变、快速迭代的场景，用 Python DSL 表达更灵活、更易调优与 autotune，故官方实现集中在 DSL/cute_ext。
- **练习 2**：`SM100_TMA_2SM_LOAD_MULTICAST` 里的 multicast 与 2SM 是同一回事吗？
  - **答案**：不是。2SM 指 2 个 SM **合算同一个 tile**（共享一份 A/B，对应 `cta_group::2` 的 UMMA）；multicast 指 TMA 把**同一份数据广播**给 cluster 内多个 CTA（避免重复搬运）。二者常配合使用但语义不同。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「从 Hopper 到 Blackwell 的最小迁移」：

1. **阅读迁移说明**：先读示例开头注释 [70_blackwell_fp16_gemm.cu:32-56](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/70_blackwell_gemm/70_blackwell_fp16_gemm.cu#L32-L56)，它明确说了「minimal set of changes needed to transition from a Hopper 3.x GEMM kernel to a Blackwell 3.x GEMM kernel」。
2. **对比组装代码**：把本示例的 builder 组装段 [L119-L148](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/70_blackwell_gemm/70_blackwell_fp16_gemm.cu#L119-L148) 与 u2-l9 的 example 49（Hopper）并排看。你会发现**用户代码几乎一样**：都是 `CollectiveBuilder`（epilogue → mainloop）→ `kernel::GemmUniversal` → `device::GemmUniversalAdapter`，都用 `KernelScheduleAuto` / `EpilogueScheduleAuto`。差别只有三处：`ArchTag = Sm100`、`ClusterShape` 可含 2 个 M、`TileSchedulerTag = void`（默认走 CLC）。
3. **底层解释**：用本讲所学解释「为什么用户代码几乎没变、行为却大变」——`CollectiveBuilder` 据 `ArchTag=Sm100` 自动把 mainloop 选成 [sm100_mma_warpspecialized.hpp](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/collective/sm100_mma_warpspecialized.hpp)，把内核选成 [sm100_gemm_tma_warpspecialized.hpp](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/sm100_gemm_tma_warpspecialized.hpp)，于是累加器 silently 从寄存器挪到了 TMEM、warp 角色从 2 类裂成 5 类、调度从静态 raster 换成 CLC。这就是 CUTLASS 3.x「策略分派 + 统一外壳」设计在跨代迁移上的回报。
4. **运行验证（待本地验证）**：在 Blackwell 卡上 `cmake .. -DCUTLASS_NVCC_ARCHS=100a && make 70_blackwell_fp16_gemm -j`，运行 `./70_blackwell_fp16_gemm --m=8192 --n=8192 --k=8192`，应输出 `Disposition: Passed` 与 GFLOPS（需 CUDA ≥ 12.8，见 [L446-L461](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/70_blackwell_gemm/70_blackwell_fp16_gemm.cu#L446-L461) 的版本/架构门禁）。

## 6. 本讲小结

- Blackwell Tensor Core 的两条新主线是 **UMMA 指令 `tcgen05.mma`**（吞吐较 WGMMA 翻倍）与 **TMEM**（每 SM 专用累加器内存，容量 128×512×32 位）。
- UMMA 的 A/B 用 64 位共享内存**描述符**寻址、C/D 用 32 位 **TMEM 地址**寻址；`scaleC`（`UMMA::ScaleOut`）控制清零还是累加；`cta_group::1/2` 区分单 SM 与 cluster 内 2SM 协同。
- SM100 集体主循环比 SM90 多一条 **`AccumulatorPipeline`**，把累加器（落 TMEM）在 MMA（producer）与 epilogue（consumer）之间流水化。
- SM100 内核把 warp 裂成 **5 类**（MMA / Sched / MainloopLoad / EpilogueLoad / Epilogue），并用 **CLC（Cluster Launch Control）** 做动态调度，取代 SM90 的静态 raster。
- **TMEM 取代了 SM90 中「consumer warp group 的寄存器累加器」这一角色**，从而让 MMA 与 epilogue 解耦到不同 warp 并发，腾出的寄存器可再分配给 epilogue。
- cluster 让两个 SM 经 **2SM UMMA + `SM100_TMA_2SM_LOAD` + DSM** 合算大 tile；多 GPU 分布式 GEMM 的扩展点主要在 Python CuTe DSL（GEMM 与集合通信融合）。

## 7. 下一步学习建议

- **想看更接近底层的 Blackwell MMA 用法**：读 `examples/cute/tutorial/blackwell/01_mma_sm100.cu`、`02_mma_tma_sm100.cu`、`04_mma_tma_2sm_sm100.cu`，它们绕开 CollectiveBuilder、直接用 CuTe atom 手写 UMMA + TMA，是理解本讲「黑盒内部」的最佳下一站。
- **想深入调度**：进 u3-l3（Tile Scheduling 与 Stream-K），对照 SM100 的 CLC 调度器 `include/cutlass/gemm/kernel/sm100_tile_scheduler.hpp` 看 Blackwell 版 Stream-K 如何与 CLC 配合（见 example `74_blackwell_gemm_streamk`）。
- **想看 Python DSL 版 Blackwell GEMM**：读 `examples/python/CuTeDSL/cute/blackwell/kernel/dense_gemm/dense_gemm_persistent.py`，对照本讲的 C++ 实现，理解同一套 UMMA/TMEM 抽象在 DSL 里的表达（衔接 u3-l9/u3-l10）。
- **想看块缩放与多精度**：u3-l6 的 NVFP4/MXFP block-scaled GEMM 正是建立在 `sm100_blockscaled_mma_warpspecialized.hpp` 之上，本讲的 TMEM/UMMA 是它的直接前置。

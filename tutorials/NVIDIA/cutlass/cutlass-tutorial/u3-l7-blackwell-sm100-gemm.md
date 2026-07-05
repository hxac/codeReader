# Blackwell SM100 集体 GEMM

## 1. 本讲目标

本讲承接 u2-l9（Hopper warp-specialized GEMM 实战）与 u3-l1（异步流水线），把目光从 Hopper（SM90）移到 **Blackwell（SM100）**。读完本讲，你应当能够：

- 说清楚 Blackwell 引入的 **UMMA 指令**（`tcgen05.mma`）与 Hopper 的 `wgmma` 在「累加器存放位置」上有何本质区别；
- 理解什么是 **TMEM（Tensor Memory，张量内存）**，以及它为什么能把 MMA 与 epilogue 解耦到不同 warp；
- 看懂 SM100 collective MMA（`sm100_mma_warpspecialized.hpp`）的 producer/consumer 主循环，并指出它和 SM90 collective 的结构性差异；
- 了解 **cluster（簇）** 与 **CLC（Cluster Launch Control）动态调度器** 如何协作，以及「跨 2 个 SM 的分布式 MMA（2x1SM）」这一扩展点。

本讲是「专家层」内容，重在源码阅读与概念串接；除明确标注外，多数运行类实践需要真实的 Blackwell（compute capability 100a）硬件。

## 2. 前置知识

在进入本讲前，请确保你已经掌握（这些在前序讲义中讲过，本讲不再重复）：

- **CUTLASS 3.x 三段式模型**：`kernel::GemmUniversal` 外壳 + `CollectiveMainloop`（搬 A/B + MMA）+ `CollectiveEpilogue`（后处理 + 写回）+ `TileScheduler`（u2-l7）。
- **CollectiveBuilder 的自动组装**：吃高层参数（架构、类型、TileShape、Cluster、Schedule），推断出 TiledMma、TMA copy atom、共享内存布局与流水线级数（u2-l8）。
- **Hopper warp specialization**：producer warp group 发 TMA、consumer warp group 发 `wgmma`，靠 `PipelineTmaAsync` 多级缓冲同步（u2-l9、u3-l1）。
- **CuTe 的 Layout/Tensor/Atom** 抽象，以及 `cute::copy` / `cute::gemm` 如何作用于不同内存空间的张量（u2-l1 ~ u2-l4）。

一个关键的事实预先点明（后面会反复用到）：在 **SM90 上，累加器（accumulator）住在 consumer warp group 的寄存器（RMEM）里**，consumer 既做 `wgmma`、又做 epilogue；而 **SM100 把累加器搬到了 TMEM**，从而让 MMA 和 epilogue 可以由不同 warp 并发执行。本讲的核心就是围绕这一改变展开。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `examples/70_blackwell_gemm/70_blackwell_fp16_gemm.cu` | 官方最小 Blackwell FP16 GEMM 示例，展示了从 Hopper 迁移到 Blackwell 所需的最少改动 |
| `include/cutlass/gemm/collective/sm100_mma_warpspecialized.hpp` | SM100 collective MMA（主循环）：producer 用 TMA 搬 A/B，consumer 用 UMMA 把结果累加进 TMEM |
| `include/cute/arch/mma_sm100_umma.hpp` | UMMA 指令的 PTX 封装（`SM100_MMA_*_SS` / `_TS` / `_2x1SM_*` 等结构体） |
| `include/cute/arch/tmem_allocator_sm100.hpp` | TMEM 的分配/释放器（`Allocator1Sm` / `Allocator2Sm`），封装 `tcgen05.alloc` / `dealloc` PTX |
| `include/cutlass/gemm/kernel/sm100_gemm_tma_warpspecialized.hpp` | SM100 设备端内核：把 warp 划分成 MMA/Sched/MainloopLoad/EpilogueLoad/Epilogue 五类角色并接线 |
| `include/cutlass/gemm/dispatch_policy.hpp` | `MainloopSm100TmaUmmaWarpSpecialized` 策略结构体，串起 mainloop 与 Schedule 标签 |
| `include/cute/arch/mma_sm100_desc.hpp` | `UMMA` 命名空间：`Major`、`ScaleIn`、`ScaleOut` 等枚举与描述符构造工具 |

## 4. 核心概念与源码讲解

### 4.1 UMMA 与 TMEM

#### 4.1.1 概念说明

Blackwell（SM100）启用了第五代 Tensor Core，其矩阵乘加指令族在 PTX 里叫 `tcgen05.mma`，CUTLASS 内部统称 **UMMA（Unified MMA，统一 MMA）**。示例文件头注释把 Blackwell 相对 Hopper 的关键变化总结成了四条，其中前三条直接对应本模块：

[examples/70_blackwell_gemm/70_blackwell_fp16_gemm.cu:L38-L53](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/70_blackwell_gemm/70_blackwell_fp16_gemm.cu#L38-L53) —— 官方对 Blackwell 四大特性的概述（下面逐条对照）。

要点有三：

1. **吞吐翻倍**：`tcgen05` 相对 Hopper 的 `wgmma` 有约 2× 的 Tensor Core 吞吐，且 Hopper 的 `wgmma` 指令在 Blackwell 上不可用。
2. **TMEM 取代寄存器存累加器**：Blackwell 引入了每 SM 专属的新存储 **Tensor Memory（TMEM）**。UMMA 指令把累加结果写进 TMEM，而**不是**寄存器堆（Register File）。
3. **解耦 MMA 与 epilogue**：正因为累加器落 TMEM（一种「共享可见」的片上存储），MMA 与 epilogue 可以被拆给不同的 warp 并发执行——这是 SM100「warp specialization 的扩展风味」的物质基础。

TMEM 的容量在源码里写得很直白：

\[ \text{TMEM 容量} = 128\,\text{行} \times 512\,\text{列} \times 32\,\text{位} = 256\,\text{KB / SM} \]

它用「128 个 data-path（DP）× 512 列」的二维结构组织，每列 32 位，地址按 `uint32_t` 字编址。下面会看到它如何被显式分配。

#### 4.1.2 核心流程

一条 UMMA 指令在 CUTLASS 里的执行流程（以最常用的 SS 风味为例）：

1. **取操作数地址**：A、B 都来自共享内存，但要先被打包成 64 位的 **shared memory descriptor**（`desc_a` / `desc_b`）。descriptor 把「smem 基址 + swizzle 模式 + leading dim + 主要维」编码进一个 `uint64_t`。
2. **取累加器地址**：累加器 C 是一个 TMEM 地址 `tmem_c`（`uint32_t`），指向此前由 `tcgen05.alloc` 分配出的 TMEM 列。
3. **由单线程发射**：UMMA 是 warp-group 级异步指令，只需 `elect_one_sync()` 选出的一个线程发射 PTX；硬件按 descriptor 自行取数、做乘加。
4. **首拍清零 / 后续累加**：第一次 MMA 用 `ScaleOut::Zero`（结果 = A·B，丢弃旧 C），后续用 `ScaleOut::One`（结果 = A·B + C）。这个开关在 collective 主循环里用 `tiled_mma.accumulate_` 字段动态切换（见 4.2.3）。

UMMA 有三种典型变体，命名遵循「**操作数 A 来源 + 操作数 B 来源**」：

- **SS**（Shared-Shared）：A、B 都来自共享内存 descriptor；
- **TS**（TMEM-Shared）：A 来自 TMEM（用 `[%1]` 寻址），B 来自共享内存 descriptor；
- **2x1SM**：上面任一种的「跨 2 个 SM」版本，一条 MMA 横跨 cluster 里 2 个 CTA（详见 4.4）。

三种变体的**累加器一律落在 TMEM**（PTX 里写 `[%0]`）。

#### 4.1.3 源码精读

先看一条最朴素的 SS 风味 UMMA 封装。`SM100_MMA_F16BF16_SS` 把 PTX `tcgen05.mma.cta_group::1.kind::f16` 包成一个 `fma` 静态方法：

[include/cute/arch/mma_sm100_umma.hpp:L86-L119](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/arch/mma_sm100_umma.hpp#L86-L119) —— FP16/BF16 的 1-CTA SS 风味 UMMA。注意 `[%0]` 是 TMEM 累加器地址 `tmem_c`，`%1`/`%2` 是 A/B 的共享内存 descriptor，`p` 是由 `scaleC`（`ScaleOut`）派生的谓词——它决定本拍是「清零再写」还是「累加」。

`CRegisters = uint32_t[1]` 这一行尤为关键：在 Hopper（`wgmma`）里 C 是一组寄存器别名（每个线程持有一段寄存器片段）；而这里 C **只是一个 32 位的 TMEM 地址**，因为真正的数据在 TMEM 而非寄存器堆里。

再看 TS 风味，A 改从 TMEM 取（PTX 里写 `[%1]`，对应 `tmem_a`）：

[include/cute/arch/mma_sm100_umma.hpp:L171-L200](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/arch/mma_sm100_umma.hpp#L171-L200) —— FP16/BF16 的 TS 风味 UMMA：A 来自 TMEM，B 仍是共享内存 descriptor。这正是「把 A 常驻 TMEM、跨多条 UMMA 复用」的基础。

控制这些行为的枚举定义在 `UMMA` 命名空间里，便于阅读 PTX 包装时对照：

[include/cute/arch/mma_sm100_desc.hpp:L59-L72](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/arch/mma_sm100_desc.hpp#L59-L72) —— `Major`（K 主序 / MN 主序，决定 descriptor 的步长方向）、`ScaleIn`（操作数符号）、`ScaleOut`（首拍清零还是累加）。

接着看 TMEM 本身如何分配。SM100 没有「全局可见的 TMEM 池」，而是由内核在启动时用专用 PTX 显式申请、结束时显式归还。分配器分两种：1-SM 簇用 `Allocator1Sm`，2-SM 簇用 `Allocator2Sm`：

[include/cute/arch/tmem_allocator_sm100.hpp:L60-L111](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/arch/tmem_allocator_sm100.hpp#L60-L111) —— `Allocator1Sm`。`allocate(num_columns, dst_ptr)` 发出 `tcgen05.alloc.cta_group::1.sync.aligned`，把分配到的 TMEM 起始地址写进一块共享内存（`dst_ptr`），供其它 warp 读取；`free` 发出 `tcgen05.dealloc`；`release_allocation_lock` 在重复分配场景下释放锁。约束：`num_columns` 必须是 32 的幂且在 [32, 512] 内，且只能由单个满员 warp 发射。

注意容量常量 `MAX_CAPACITY_BITS = 128*512*32`（[include/cute/arch/tmem_allocator_sm100.hpp:L46-L46](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/arch/tmem_allocator_sm100.hpp#L46-L46)），与上面 256KB/SM 的算法一致。

#### 4.1.4 代码实践（源码阅读型）

**目标**：对比 SM90 与 SM100 的 producer/consumer 结构，指出 TMEM 在 SM100 中「取代」了哪个角色——这正是本讲规格里的核心实践任务。

**操作步骤**：

1. 打开 `include/cutlass/gemm/collective/sm90_mma_tma_warpspecialized.hpp`（u2-l9/u3-l1 学过），定位 consumer warp group 的主循环：它在同一个 warp group 里**既**发 `wgmma`、**又**调用 epilogue 的 callback（加载 C、计算 α·acc+β·C、TMA 写回 D）。注意累加器 `accum` 是寄存器张量。
2. 打开本讲的 `include/cutlass/gemm/collective/sm100_mma_warpspecialized.hpp`，看 `mma()` 方法（4.2.3 会精读）顶部的断言：
   ```cpp
   static_assert(is_tmem<FrgEngine>::value, "Accumulator must be tmem resident.");
   ```
3. 再打开 `include/cutlass/gemm/kernel/sm100_gemm_tma_warpspecialized.hpp` 的 `WarpCategory` 枚举（4.2.3），确认 SM100 内核里有**独立的 `MMA` warp 和独立的 `Epilogue` warp**。

**需要观察的现象 / 结论**：

- 在 SM90：累加器住在 **consumer warp group 的寄存器**里；该 warp group 「身兼两职」——先 MMA 把结果累加到寄存器，再用同一份寄存器跑 epilogue。MMA 和 epilogue 是**串行**在同一组 warp 上的。
- 在 SM100：累加器搬到 **TMEM**；`MMA` warp 把结果写进 TMEM，`Epilogue` warp **从 TMEM 读**那份累加器再做后处理。

**答案**：TMEM 在 SM100 中取代的，是 SM90 里「**consumer warp group 寄存器中的那份累加器**」这一角色。它从一个 warp 私有的寄存器片段，变成了一块**可被多个 warp 共享访问**的片上存储，于是 MMA warp（写 TMEM）和 Epilogue warp（读 TMEM）得以解耦并发——这就是示例头注释里说的「decouple the execution of MMA and epilogue into separate warps」。

**预期结果**：你应当在笔记里画出如下对照表（答案供参考）：

| 维度 | SM90（Hopper） | SM100（Blackwell） |
| --- | --- | --- |
| MMA 指令 | `wgmma.mma_async` | `tcgen05.mma`（UMMA） |
| 累加器位置 | consumer warp 的寄存器（RMEM） | TMEM（每 SM 专属 256KB） |
| 谁做 epilogue | 同一个 consumer warp group | **独立的 Epilogue warp** |
| MMA 与 epilogue 关系 | 串行（同组 warp） | 解耦并发（经 TMEM + accumulator 流水线） |

#### 4.1.5 小练习与答案

**练习 1**：UMMA 的 SS 与 TS 风味，区别在哪？累加器位置是否不同？

> **答**：SS 表示 A、B 都来自共享内存 descriptor；TS 表示 A 来自 TMEM、B 来自共享内存 descriptor。**累加器位置相同**——三种风味都把累加器放 TMEM（PTX `[%0]`）。

**练习 2**：`ScaleOut::Zero` 和 `ScaleOut::One` 分别在主循环的什么时刻使用？

> **答**：处理一个输出 tile 的**第一拍 K** 用 `ScaleOut::Zero`（结果 = A·B，丢弃 TMEM 旧值）；之后的每一拍用 `ScaleOut::One`（结果 = A·B + C，沿 K 维累加）。在 collective 代码里就是 `accumulate_ = UMMA::ScaleOut::Zero` 初值，循环内首拍后改成 `One`。

### 4.2 Blackwell collective 结构

#### 4.2.1 概念说明

SM100 的 collective MMA（主循环）和 SM90 一样是「无状态 collective + warp specialization」的范式，但有两处结构性升级：

- **两条流水线**：除了搬运 A/B 的 `MainloopPipeline`（`PipelineTmaUmmaAsync`），还多了一条 `AccumulatorPipeline`——它把 TMEM 里的累加器**双缓冲（ACC_PIPE=2）**，让 MMA warp 写一缓冲、epilogue warp 同时读另一缓冲。
- **五个 warp 角色**：内核层把一个 CTA 内的 warp 划成 MMA、Sched、MainloopLoad、EpilogueLoad、Epilogue 五类，分别承担计算、调度、搬 A/B、搬 C、写回 D。

策略 tag 是 `MainloopSm100TmaUmmaWarpSpecialized`，它内嵌一个 `Schedule = KernelTmaWarpSpecializedSm100<...>`，遵循 u2-l7 讲过的「一个标签一个内核」分派方式。

#### 4.2.2 核心流程

SM100 collective 主循环的 producer/consumer 流程（与 `PipelineTmaUmmaAsync` 配合）：

```
Producer（MainloopLoad warp，elect_one 线程发 TMA）:
  for k in K_tiles:
    producer_acquire(stage)              # 等「空」缓冲
    barrier = producer_get_barrier(stage)
    TMA copy A[k] -> smem_A[stage]        # 带 multicast mask
    TMA copy B[k] -> smem_B[stage]        # 硬件 complete_transaction 翻满门
    ++stage
  producer_tail()                         # 防止簇内 CTA 提前退出

Consumer（MMA warp，elect_one 线程发 UMMA）:
  accumulate_ = ScaleOut::Zero
  accumulator_pipeline.producer_acquire() # 等 TMEM 累加器缓冲「空」
  for k in K_tiles:
    consumer_wait(stage)                  # 等 smem 缓冲「满」
    for k_block in unrolled_K:
      gemm(tiled_mma, tCrA[..], tCrB[..], accumulators_in_TMEM)  # 发 tcgen05.mma
      accumulate_ = ScaleOut::One
    consumer_release(stage)               # 把 smem 缓冲还给 producer
```

关键变化：**累加器 `accumulators` 是 TMEM 张量**（`is_tmem<FrgEngine>` 为真），且它被 accumulator 流水线双缓冲——MMA warp 与 epilogue warp 通过它解耦。这与 SM90「consumer 一手做 wgmma 一手做 epilogue、累加器在同组寄存器」形成鲜明对比。

#### 4.2.3 源码精读

先看策略结构体本身（u2-l7 的 policy 标签分派机制的又一个实例）：

[include/cutlass/gemm/dispatch_policy.hpp:L1029-L1035](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/dispatch_policy.hpp#L1029-L1035) —— `MainloopSm100TmaUmmaWarpSpecialized<Stages, SchedPipeStages, AccumPipeStages, ClusterShape, ArchTag>`。它把 mainloop 流水线级数 `Stages`、调度流水线级数、累加器流水线级数与簇形状打包，并内嵌 `Schedule` 标签交给内核偏特化挑选。

接着看 collective 类的两条流水线声明：

[include/cutlass/gemm/collective/sm100_mma_warpspecialized.hpp:L156-L160](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/collective/sm100_mma_warpspecialized.hpp#L156-L160) —— `MainloopPipeline = PipelineTmaUmmaAsync<Stages, ClusterShape, AtomThrShapeMNK>`。它和 SM90 的 `PipelineTmaAsync` 同源思想（满门/空门双屏障，u3-l1），但对接的是 UMMA 指令的等待模型。

两条强约束直接道出 SM100 的设计前提：

[include/cutlass/gemm/collective/sm100_mma_warpspecialized.hpp:L192-L194](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/collective/sm100_mma_warpspecialized.hpp#L192-L194) —— 断言要求 MMA atom 的 A/B 都必须是 `UMMA::DescriptorIterator`，即 A、B 由共享内存 descriptor 喂给 UMMA（这就是 SS 风味）。

TMEM 累加器的构造在这里：

[include/cutlass/gemm/collective/sm100_mma_warpspecialized.hpp:L468-L487](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/collective/sm100_mma_warpspecialized.hpp#L468-L487) —— `init_tmem_tensors`。注释点明累加器布局 `((MMA_TILE_M,MMA_TILE_N),MMA_M,MMA_N,ACC_PIPE)`，其中 `ACC_PIPE=2` 用于「主循环与 epilogue 各持一份」的双缓冲；`make_sm100_accumulator` 产出的张量引擎是 TMEM。

Producer 端（搬数据），由 `elect_one_sync()` 选出的单线程发 TMA：

[include/cutlass/gemm/collective/sm100_mma_warpspecialized.hpp:L604-L626](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/collective/sm100_mma_warpspecialized.hpp#L604-L626) —— `load()` 主循环核心：`producer_acquire` → `producer_get_barrier` → `copy(tma.with(barrier, mcast_mask), gA, sA)` / `copy(..., gB, sB)`。和 SM90 一样，TMA 拷贝与 mbarrier 绑定，硬件完成搬运后自动翻转满门。

Consumer 端（做 MMA），`cute::gemm` 触发 UMMA：

[include/cutlass/gemm/collective/sm100_mma_warpspecialized.hpp:L661-L709](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/collective/sm100_mma_warpspecialized.hpp#L661-L709) —— `mma()` 主循环核心。第 661 行 `static_assert(is_tmem<FrgEngine>::value, "Accumulator must be tmem resident.")` 一锤定音：SM100 的累加器必须在 TMEM。第 676 行 `accumulate_ = UMMA::ScaleOut::Zero` 初值，第 706 行首拍后改 `One`；第 702 行的 `cute::gemm(tiled_mma, tCrA, tCrB, accumulators)` 就是 UMMA 指令的统一入口（u2-l3 讲过 `cute::gemm` 会按张量内存空间自动分发，这里分到 `tcgen05.mma`）。

最后看内核如何把 warp 分成五类角色——这是 SM100 区别于 SM90 的「结构性」证据：

[include/cutlass/gemm/kernel/sm100_gemm_tma_warpspecialized.hpp:L234-L240](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/sm100_gemm_tma_warpspecialized.hpp#L234-L240) —— `WarpCategory` 枚举：`MMA`、`Sched`、`MainloopLoad`、`EpilogueLoad`、`Epilogue`。每个 warp 按自己的 `warp_idx` 落入一类（[L417-L421](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/sm100_gemm_tma_warpspecialized.hpp#L417-L421)），各自只走自己的分支。**注意 `MMA` 与 `Epilogue` 是两类独立的 warp**——这正是 4.1 实践题里「TMEM 解耦」的内核侧证据。

TMEM 分配器的选择也在这层（按是否跨 2 SM 选 1Sm/2Sm）：

[include/cutlass/gemm/kernel/sm100_gemm_tma_warpspecialized.hpp:L178-L179](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/sm100_gemm_tma_warpspecialized.hpp#L178-L179) —— `TmemAllocator` 在 `Allocator1Sm` 与 `Allocator2Sm` 间二选一，依据是 TiledMma 的线程布局第一维是否为 1（即 MMA 是否跨 2 SM）。

#### 4.2.4 代码实践（源码阅读型）

**目标**：在内核里追踪 TMEM 的「申请—使用—归还」完整生命周期，验证它确实是 MMA 与 epilogue 之间的解耦缓冲。

**操作步骤**：

1. 在 `sm100_gemm_tma_warpspecialized.hpp` 中搜索 `tmem_allocator.allocate`（约 L727）、`tmem_allocator.free`（约 L803）、`release_allocation_lock`（约 L783）。
2. 注意 `allocate` 把分配到的 TMEM 基址写进 `shared_storage.tmem_base_ptr`，随后由 4.1.3 里看到的 `set_tmem_offsets` 把它装进累加器张量。
3. 确认：MMA warp（`WarpCategory::MMA`）向 TMEM 写累加器；Epilogue warp（`WarpCategory::Epilogue`）从同一份 TMEM 读累加器——读写经过 `AccumulatorPipeline`（ACC_PIPE=2）双缓冲同步。

**预期结果**：你能复述出 TMEM 在一次 kernel 调用里的时间线——`alloc`（启动期，1 个 warp 发 PTX）→ MMA warp 写 / Epilogue warp 读（主循环期，双缓冲并发）→ `free`（收尾期）。这条链路就是 4.1 实践题答案的源码侧落点。本步骤为纯阅读；在真实 SM100 硬件上用 nsight compute 观察 TMEM 写入/读取活动以 corroborate，属**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：SM100 collective 为什么需要**两条**流水线（MainloopPipeline + AccumulatorPipeline），而 SM90 主循环只有一条？

> **答**：SM90 的累加器在 consumer 寄存器里，MMA 与 epilogue 串行在同一组 warp，一条 mainloop 流水线足够。SM100 的累加器在 TMEM 且要被**两个不同 warp**（MMA 写、Epilogue 读）访问，必须再用一条 AccumulatorPipeline 把 TMEM 缓冲双缓冲起来，才能让两者并发而不竞争。

**练习 2**：`WarpCategory` 里的 `Sched` 角色对应什么新机制？

> **答**：对应 Blackwell 的 **CLC（Cluster Launch Control）动态调度器**（见 4.3）。`Sched` warp 负责从 CLC 硬件队列领取下一份 tile 工作，再把响应分发给本簇内的计算 warp。这是 SM90（靠软件游标 `+= grid_size` 领活）所没有的硬件辅助调度路径。

### 4.3 cluster 调度

#### 4.3.1 概念说明

**Cluster（簇）** 是 Hopper 引入、Blackwell 强化的概念：把多个 CTA（threadblock）编成一组，让它们共享一块「分布式共享内存（Distributed Shared Memory, DSM）」，并能用 TMA multicast 一次把数据广播给簇内所有 CTA。`ClusterShape` 就是这个组的形状。

Blackwell 在此基础上新增了 **CLC（Cluster Launch Control）**——一个由硬件 + 软件协同的**动态调度器**。示例头注释把它列为第四大特性：

[examples/70_blackwell_gemm/70_blackwell_fp16_gemm.cu:L52-L52](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/70_blackwell_gemm/70_blackwell_fp16_gemm.cu#L52-L52) —— 「A new SW controlled dynamic scheduler based on cluster launch control」。

回顾 u3-l3：SM90 用「持久化内核 + 软件游标」领活，CTA 数 ≈ SM 数。SM100 的 CLC 把「领下一份 tile」从软件循环升级为硬件队列响应，进一步降低调度开销、提升尾波利用率。本讲的示例 `GemmKernel` 第 4 个模板参数填 `void`，就表示「使用默认的 CLC 调度器」。

#### 4.3.2 核心流程

簇与调度的协作流程：

1. 主机端按 `ClusterShape`（如 `<2,2,1>`）发射内核，硬件把每 4 个 CTA 编为一个簇；
2. 簇内 `Sched` warp 通过 CLC 队列领到「下一个输出 tile」的坐标响应（`TileScheduler::CLCResponse`）；
3. `MainloopLoad` warp 用 **TMA multicast** 把 A/B 广播给需要的 CTA（簇内同行/同列），减少重复搬运；
4. 计算完成后，`Epilogue` warp 把 D 写回显存，`Sched` warp 再领下一份。

簇的 M 维若为偶数（`ClusterShape % 2 == 0`），还能启用 **2x1SM UMMA**（4.4）——一条 MMA 横跨 2 个 SM。

#### 4.3.3 源码精读

示例对簇形状的选择与注释：

[examples/70_blackwell_gemm/70_blackwell_fp16_gemm.cu:L113-L117](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/70_blackwell_gemm/70_blackwell_fp16_gemm.cu#L113-L117) —— `MmaTileShape_MNK = <256,128,64>`，`ClusterShape_MNK = <2,2,1>`。注释提示：当簇形状为偶数时，tcgen05 MMA 可以跨 2 个 SM 执行。

`ClusterShape` 一路传到 collective（影响 TMA multicast 掩码与 TMEM 分配器选择），并在内核里决定 CLC 流水线的 `consumer_arv_count`（参与到达计数的 CTA 数）：

[include/cutlass/gemm/kernel/sm100_gemm_tma_warpspecialized.hpp:L510-L513](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/sm100_gemm_tma_warpspecialized.hpp#L510-L513) —— CLC 流水线的 `consumer_arv_count` 把簇大小 `cluster_size` 纳入计数，体现「簇是调度的基本单位」。

multicast 掩码在 collective 的 `load_init` 里按簇布局算出：

[include/cutlass/gemm/collective/sm100_mma_warpspecialized.hpp:L539-L541](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/collective/sm100_mma_warpspecialized.hpp#L539-L541) —— `create_tma_multicast_mask` 沿簇的 N/M 方向生成掩码，TMA 据此一次广播给同行/同列 CTA。

示例主机端把簇形状参与计算 max swizzle（簇栅格化方向，影响 L2 局部性，对应 `--swizzle` 选项）：

[examples/70_blackwell_gemm/70_blackwell_fp16_gemm.cu:L335-L340](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/70_blackwell_gemm/70_blackwell_fp16_gemm.cu#L335-L340) —— `arguments.scheduler.max_swizzle_size = options.swizzle`，把命令行 swizzle 参数喂给 CLC 调度器。

#### 4.3.4 代码实践（源码阅读 + 命令行型）

**目标**：感受 cluster 形状与 swizzle 对调度的影响。

**操作步骤**：

1. 阅读 `examples/70_blackwell_gemm/CMakeLists.txt`，确认示例只在 `CUTLASS_NVCC_ARCHS` 匹配 `100a|100f|101a|101f|103a|103f` 时编译，并带 4 组 swizzle 测试参数（`--swizzle=1/2/5` 及一组非对齐规模 `--swizzle=5 --m=4096 --n=16384`）。
2. 在有 Blackwell 硬件的环境里（**待本地验证**）构建并运行：
   ```bash
   cmake -B build -DCUTLASS_NVCC_ARCHS=100a
   cmake --build build -j --target 70_blackwell_fp16_gemm
   ./build/examples/70_blackwell_gemm/70_blackwell_fp16_gemm --m=8192 --n=8192 --k=8192 --swizzle=1
   ```

**需要观察的现象**：成功时输出 `Disposition: Passed`、`Avg runtime: ... ms`、`GFLOPS: ...`。换不同 `--swizzle` 值，GFLOPS 可能有几个百分点的差异——这反映 L2 局部性受簇栅格化方向影响。

**预期结果**：若硬件/工具链不满足（非 SM100、CUDA < 12.8），程序会打印提示并 `return 0` 提前退出（见 [L446-L461](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/70_blackwell_gemm/70_blackwell_fp16_gemm.cu#L446-L461)），不会崩溃。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ClusterShape` 的 M 维取偶数（如 `<2,2,1>` 而非 `<1,2,1>`）对 Blackwell 有特殊意义？

> **答**：M 维偶数时，MMA 可以跨簇内 2 个 CTA（即 2 个 SM）执行 2x1SM UMMA，把 M 维分布式地摊到 2 个 SM 上，单条指令吞吐翻倍。详见 4.4。

**练习 2**：CLC 调度器相对 SM90 的「软件游标领活」带来什么好处？

> **答**：把「领下一份 tile」从软件循环（`tile_idx += gridDim.x`）升级为硬件队列响应，降低调度开销、改善尾波利用率，并天然支持动态持久化与更灵活的负载均衡。

### 4.4 分布式 GEMM 扩展点

#### 4.4.1 概念说明

「分布式 GEMM」在 SM100 语境下指：把一次 MMA 的工作量**跨多个 SM 协作完成**，而非局限在单 SM 内。它的物理基础有二：

- **Distributed Shared Memory（DSM）**：簇内 CTA 共享一片逻辑共享内存，可以互相访问对方的 smem；
- **2x1SM UMMA**：一条 `tcgen05.mma.cta_group::2` 指令横跨 2 个 SM 的 Tensor Core，把 M 维分布式地切成两半、两 SM 并行算同一拍。

这是 SM100 在「单 SM 集体」之上的扩展维度。CUTLASS 还在此底座上派生出 blockscaled、sparse、mixed-input、array（grouped）等变体（见 `include/cutlass/gemm/collective/sm100_*_mma_*.hpp` 一族），它们都复用同一套「UMMA + TMEM + 簇 + CLC」骨架。

#### 4.4.2 核心流程

启用 2x1SM 分布式 MMA 的前提条件与流程：

1. **簇形状偶数**：`ClusterShape` 的 M 维（或对应维）能配对出 2 个 CTA；
2. **选 2-SM MMA atom**：TiledMma 的 `AtomThrShapeMNK` 第一维为 2，对应的 UMMA 结构体是 `_2x1SM_*` 系列（PTX `cta_group::2`）；
3. **选 2-SM TMEM 分配器**：`Allocator2Sm`（PTX `tcgen05.alloc.cta_group::2`），两个 CTA 必须提供相同的 `dst_ptr`、相同的逻辑 warp ID；
4. **选 2-SM TMA**：`SM100_TMA_2SM_LOAD`（或其 multicast 版），让 TMA 跨 2 SM 共享同一份加载；
5. 计算时，配对的两个 CTA 协同发同一条 `cta_group::2` UMMA，结果累加进**共享的** TMEM 区域。

#### 4.4.3 源码精读

2x1SM 版的 UMMA 封装，PTX 后缀变成 `cta_group::2`、掩码参数也从 4 个扩到 8 个（因为 M 维翻倍）：

[include/cute/arch/mma_sm100_umma.hpp:L552-L590](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/arch/mma_sm100_umma.hpp#L552-L590) —— `SM100_MMA_F16BF16_2x1SM_SS`，PTX `tcgen05.mma.cta_group::2.kind::f16`。累加器仍在 TMEM（`[%0]`），但本拍由 2 个 SM 协同完成。

2-SM TMEM 分配器，关键约束写在注释里——「两个参与 CTA 必须提供完全相同的 `dst_ptr`、相同的逻辑 warp ID」：

[include/cute/arch/tmem_allocator_sm100.hpp:L117-L145](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/arch/tmem_allocator_sm100.hpp#L117-L145) —— `Allocator2Sm::allocate`，PTX `tcgen05.alloc.cta_group::2.sync.aligned`。这是「分布式」落到 PTX 层的体现：分配动作本身就要两个 CTA 对齐协同。

collective 里对 2-SM TMA 的硬约束：

[include/cutlass/gemm/collective/sm100_mma_warpspecialized.hpp:L195-L200](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/collective/sm100_mma_warpspecialized.hpp#L195-L200) —— 当 `AtomThrShapeMNK` 大小为 1 时必须用 `SM90_TMA_LOAD(_MULTICAST)`；大小为 2 时必须用 `SM100_TMA_2SM_LOAD(_MULTICAST)`。编译期强约束，配错直接报错。

由 `AtomThrShapeMNK` 自动挑选 1Sm/2Sm 分配器（即 4.2.3 已引用的 `TmemAllocator` 条件类型）：

[include/cutlass/gemm/kernel/sm100_gemm_tma_warpspecialized.hpp:L178-L179](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/sm100_gemm_tma_warpspecialized.hpp#L178-L179) —— `TmemAllocator = conditional_t<size<0>(ThrLayoutVMNK)==1, Allocator1Sm, Allocator2Sm>`。

#### 4.4.4 代码实践（源码阅读型）

**目标**：理解「单 SM 集体」与「2-SM 分布式集体」在源码层是如何被同一份模板差异化支持的。

**操作步骤**：

1. 在 `include/cute/arch/mma_sm100_umma.hpp` 里数一下 `_2x1SM_` 系列有多少种数据类型/风味（f16/bf16、tf32、i8……各成对出现）。
2. 在 `include/cutlass/gemm/collective/` 下对比 `sm100_mma_warpspecialized.hpp`（本讲主角，单 SM 簇基底）与 `sm100_mma_array_warpspecialized.hpp`（一次启动算一组问题的 array/grouped 版）。注意后者复用了前者的 `load`/`mma` 主体，只是在外层包了「按 problem_idx 循环领活」的调度。
3. 阅读 `examples/70_blackwell_gemm/70_blackwell_fp8_gemm.cu`，对比 FP16 与 FP8 两个示例在「CollectiveBuilder 配置」上的差异——你会看到只是换了 `ElementA/ElementB` 和 Schedule，骨架完全一致，这印证了 4.4.1 说的「同一套骨架派生多种变体」。

**预期结果**：你应能归纳出 SM100 collective 的「扩展点矩阵」——按 `{数据类型} × {单SM/2SM} × {dense/sparse/blockscaled/mixed/grouped}` 正交组合，而它们共享 UMMA + TMEM + 簇 + CLC 的同一底座。运行性能对比需真实 SM100 硬件，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `Allocator2Sm` 要求「两个 CTA 提供完全相同的 `dst_ptr`、相同的逻辑 warp ID」？

> **答**：`cta_group::2` 的 TMEM 分配是两个 CTA 协同的硬件操作，必须由两个 CTA 上「对齐」的同一逻辑 warp 同时发出相同的请求，硬件才能正确合并、分配出一块两个 SM 都能寻址的共享 TMEM 区域。

**练习 2**：SM100 collective 一族（blockscaled/sparse/mixed/array…）为何能复用本讲的 mainloop 骨架？

> **答**：它们都建立在「UMMA 指令（结果落 TMEM）+ 簇（DSM + multicast）+ CLC 调度 + 两条流水线」的同一底座上，差别只在于「额外搬了什么（如缩放因子 SFA/SFB）」「用了哪种 UMMA 风味（如 blockscaled 的 `tcgen05.mma.blockscaled`）」「在外层套了哪种领活循环（如 grouped 的 problem_idx 循环）」。底座不变，所以 mainloop 的 producer/consumer 主体可被复用。

## 5. 综合实践

把本讲的四块知识串起来，完成下面这个「源码阅读 +（可选）真机运行」的复合任务。

**任务**：以 `examples/70_blackwell_gemm/70_blackwell_fp16_gemm.cu` 为对象，复现「从 Hopper 迁移到 Blackwell」的最小改动，并解释每处改动对应本讲的哪个概念。

**步骤**：

1. **定位组装链路**（对应 u2-l9 讲过的四步 typedef）：阅读 [examples/70_blackwell_gemm/70_blackwell_fp16_gemm.cu:L120-L148](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/70_blackwell_gemm/70_blackwell_fp16_gemm.cu#L120-L148)。依次是：epilogue `CollectiveBuilder` → mainloop `CollectiveBuilder`（`StageCountAutoCarveout` 用 epilogue 的 `SharedStorage` 大小算级数）→ `kernel::GemmUniversal`（第 4 参数 `void` = 默认 CLC 调度器）→ `device::GemmUniversalAdapter`。
2. **找出相对 Hopper 的关键差异**（对应 4.1~4.3）：
   - `ArchTag = cutlass::arch::Sm100`（[L110](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/70_blackwell_gemm/70_blackwell_fp16_gemm.cu#L110)）——换成 Blackwell 架构 tag，于是 builder 自动选 `MainloopSm100TmaUmmaWarpSpecialized` 与对应 UMMA atom；
   - `MmaTileShape_MNK = <256,128,64>`、`ClusterShape_MNK = <2,2,1>`（[L115-L117](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/70_blackwell_gemm/70_blackwell_fp16_gemm.cu#L115-L117)）——更大的 tile 与簇，配合 tcgen05 的 2× 吞吐与 2x1SM 分布式 MMA；
   - `KernelScheduleAuto` / `EpilogueScheduleAuto`——两个 builder 必须同时用 `Auto`（u2-l9 讲过的约束），让框架替你选 SM100 的 schedule。
3. **解释「最少改动」的本质**：从 Hopper 到 Blackwell，用户侧只改了 `ArchTag` 和 tile/cluster 形状；TMEM 分配、五类 warp 分流、UMMA 指令选择、CLC 调度接线全部由 `CollectiveBuilder` + 内核偏特化自动完成。这就是 CUTLASS 3.x「policy 标签 + 统一外壳」设计的回报。
4. **（可选，需 SM100 真机）改一处观察行为**：把 `ClusterShape_MNK` 从 `<2,2,1>` 改成 `<1,1,1>`，重新编译运行，对比 GFLOPS。预期单 CTA 簇因无法启用 multicast 与 2x1SM MMA 而性能下降。**待本地验证**。
5. **核对正确性**：示例自带朴素参考 GEMM 做位级比对（[L343-L373](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/70_blackwell_gemm/70_blackwell_fp16_gemm.cu#L343-L373)），成功打印 `Disposition: Passed`。

**交付物**：一份说明，把上面每处改动对应到 4.1（UMMA/TMEM）、4.2（collective 结构/五类 warp）、4.3（cluster/CLC）、4.4（2x1SM 分布式）中的具体概念。

## 6. 本讲小结

- Blackwell（SM100）启用第五代 Tensor Core 指令 **UMMA（`tcgen05.mma`）**，相对 Hopper `wgmma` 约 2× 吞吐；Hopper 的 `wgmma` 在 Blackwell 上不可用。
- UMMA 的累加器不再住寄存器，而是住新引入的每 SM 专属存储 **TMEM（128×512×32 位 = 256KB）**；TMEM 由 `tcgen05.alloc/dealloc` PTX 显式申请/释放，分 `Allocator1Sm`/`Allocator2Sm` 两版。
- SM100 collective（`sm100_mma_warpspecialized.hpp`）用**两条流水线**：`MainloopPipeline`（搬 A/B）+ `AccumulatorPipeline`（TMEM 累加器双缓冲，ACC_PIPE=2），从而把 **MMA warp 与 Epilogue warp 解耦并发**——这是 SM90（同组 warp 串行做 wgmma + epilogue、累加器在寄存器）所没有的结构。
- 内核把 warp 划成 **MMA / Sched / MainloopLoad / EpilogueLoad / Epilogue** 五类角色（`WarpCategory`），其中 `Sched` 对应 Blackwell 新的 **CLC（Cluster Launch Control）动态调度器**。
- **cluster（簇）** 提供分布式共享内存（DSM）与 TMA multicast；当簇形状偶数时，可启用 **2x1SM UMMA**（`cta_group::2`）让一条 MMA 横跨 2 个 SM，配合 `Allocator2Sm` 与 `SM100_TMA_2SM_LOAD` 形成分布式 GEMM 扩展点。
- blockscaled / sparse / mixed / array 等变体都建立在「UMMA + TMEM + 簇 + CLC」同一底座上，mainloop 骨架可复用。

## 7. 下一步学习建议

- **低精度与块缩放**：进入 u3-l6（低精度与 Block-Scaled 类型），看 `sm100_blockscaled_mma_warpspecialized.hpp` 如何在本讲骨架上多搬 SFA/SFB 并发 `tcgen05.mma.blockscaled`。
- **TMA 深化**：若想彻底搞懂 `desc_a`/`desc_b` 的构造，回到 u3-l2（TMA 异步张量拷贝）与 `include/cute/arch/mma_sm100_desc.hpp`，理解 UMMA descriptor 如何编码 swizzle 与 leading dim。
- **调度深化**：进入 u3-l3（Tile Scheduling 与 Stream-K），把 SM90 的软件调度与 SM100 的 CLC 调度对照阅读 `sm100_tile_scheduler.hpp` 与 `sm100_tile_scheduler_stream_k.hpp`。
- **Python DSL 路径**：若你对 Python 侧感兴趣，可进入 u3-l9/u3-l10（CuTe DSL），看同一套 Blackwell 概念如何在 Python 里表达与发射。
- **建议精读源码**：`include/cutlass/gemm/kernel/sm100_gemm_tma_warpspecialized.hpp` 的 `operator()`（warp 分流主入口），把本讲的五类角色在源码里一一对应。

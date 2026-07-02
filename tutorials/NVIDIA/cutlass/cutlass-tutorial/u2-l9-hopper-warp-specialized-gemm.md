# Hopper Warp-Specialized GEMM 实战

## 1. 本讲目标

上一讲（u2-l8）我们读懂了 `CollectiveBuilder` 如何「拼装」出一个 collective MMA 主循环、以及 SM90 主循环内部 producer/consumer 的分工。本讲把视角拉到**一个完整 GEMM 内核从声明到启动的全过程**，让你能独立写出一个跑得通的 Hopper GEMM。

学完本讲你应该能够：

1. 用 `CollectiveBuilder`（主循环 + epilogue）+ `kernel::GemmUniversal` + `device::GemmUniversalAdapter` 三件套，从零组装出一个 Hopper warp-specialized GEMM。
2. 说清楚「warp specialization」在 Hopper 上具体是怎么落地的：哪个 warp 当 producer、哪个 warp group 当 consumer、它们怎么靠 pipeline 同步。
3. 理解 epilogue 的 `CollectiveBuilder` 与预定义融合操作（`LinearCombination`）及自定义 EVT 的关系。
4. 看懂主机端 `GemmUniversalAdapter` 如何把 `Arguments` 翻成 `Params`、算出 grid、并用 cluster launch API 把内核真正发到 GPU。

本讲以官方示例 `examples/49_hopper_gemm_with_collective_builder/49_collective_builder.cu` 为蓝本，逐行对应真实源码。

---

## 2. 前置知识

本讲默认你已经学过以下前置（见前置摘要）：

- **u2-l7**：3.x 的 `GemmUniversal` 通用模型——一个无状态内核 + collective mainloop + collective epilogue + tile scheduler 的三段式架构；`dispatch_policy.hpp` 里用空结构体 `Schedule` 标签做「一个标签一个内核文件」的编译期分派。
- **u2-l8**：`CollectiveBuilder` 自动推断 `TiledMma`、TMA 拷贝原子、共享内存布局与流水线级数；SM90 的 warp specialization 把内核按 `warp_group_role` 分成 producer（DMA warp 发 TMA）与 consumer（math warp group 发 wgmma），靠 `PipelineTmaAsync` 多级缓冲同步。

如果下面这些词你还不熟，建议先回看上面两讲：

| 术语 | 一句话解释 |
|------|-----------|
| warp specialization | 把一个 CTA 内的 warp 按职责分工：有的专门搬数据、有的专门算 |
| producer / consumer | 搬数据的叫 producer（发 TMA），算的叫 consumer（发 wgmma） |
| TMA | Hopper 的 Tensor Memory Accelerator，异步批量搬运显存到共享内存的硬件单元 |
| wgmma | Hopper 的 `wgmma.mma_async` 指令，一个 warp group（128 线程）发一条大 MMA |
| pipeline / stage | 多级缓冲 + 同步原语，让「搬下一块数据」和「算这一块」重叠 |
| cluster | Hopper 引入的 CTA 簇（Thread Block Cluster），多个 CTA 可共享分布式共享内存 |
| EVT | Epilogue Visitor Tree，用计算图的方式自定义 epilogue 融合 |

**关于 Hopper 与 sm90a 的硬性要求**：本例的所有 wgmma 指令都是 *arch conditional*，必须用 `CUTLASS_NVCC_ARCHS=90a`（带 `a` 后缀）编译，且运行时 GPU 的 compute capability 必须是 9.0。示例 `main()` 会显式检查这一点（见 4.5 节）。填错架构会直接走到内核里 `CUTE_INVALID_CONTROL_PATH` 报错分支。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `examples/49_hopper_gemm_with_collective_builder/49_collective_builder.cu` | 本讲的「教材」，用多种 schedule/stage 组装并运行 Hopper GEMM |
| `examples/49_hopper_gemm_with_collective_builder/CMakeLists.txt` | 把示例编成可执行文件 `49_collective_builder` |
| `include/cutlass/gemm/dispatch_policy.hpp` | 定义 `KernelTmaWarpSpecialized` 等 schedule 标签 |
| `include/cutlass/gemm/kernel/gemm_universal.hpp` | `GemmUniversal` 的主模板 + `#include` 各架构内核文件（分派入口） |
| `include/cutlass/gemm/kernel/sm90_gemm_tma_warpspecialized.hpp` | **kernel 层**：warp-specialized 内核的 `operator()`，分流 producer/consumer |
| `include/cutlass/gemm/collective/sm90_mma_tma_gmma_ss_warpspecialized.hpp` | **collective 层**：SS（A、B 都从 smem 读）主循环的 `load`/`mma` 实现 |
| `include/cutlass/gemm/device/gemm_universal_adapter.h` | **主机适配层**：`Arguments→Params`、算 grid、cluster launch |

> ⚠️ **一个容易踩的坑**：3.x 集合算子文件名里嵌入了 `gmma`（如 `sm90_mma_tma_gmma_ss_warpspecialized.hpp`），而 kernel 层文件名里没有（`sm90_gemm_tma_warpspecialized.hpp`）。本讲引用的是这些**真实存在**的文件。注意「collective 层（主循环，搬数据+算）」和「kernel 层（外壳，分流角色）」是两个不同抽象层，别混了。

---

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块，正好覆盖规格要求的「example 49 组装流程 / warp specialized 调度 / epilogue builder / kernel 启动与参数」，外加一块主循环内部实现。

### 4.1 Example 49 的组装流程：从 typedef 到 Gemm

#### 4.1.1 概念说明

回顾 u2-l7 的三段式模型，一个 3.x GEMM 在用户代码里就是「四行 typedef」：

1. 用 **epilogue CollectiveBuilder** 算出 `CollectiveEpilogue`（负责加载 C、做 α/β/激活、写回 D）。
2. 用 **mainloop CollectiveBuilder** 算出 `CollectiveMainloop`（负责搬 A/B、做 MMA）。
3. 把两者塞进 `kernel::GemmUniversal<...>` 得到内核类型 `GemmKernel`。
4. 把内核塞进 `device::GemmUniversalAdapter<GemmKernel>` 得到主机句柄 `Gemm`。

builder 之所以存在，是因为 `CollectiveMma` / `CollectiveEpilogue` 的完整模板参数多达十几个（TiledMma、各种 TMA copy atom、smem layout atom、transform……），手填极易出错。builder 吃「少量高层参数」（数据类型、布局、tile 形状、schedule 标签），自动推断其余。`Auto` 系列标签则让 builder 连 schedule 和 stage 数都替你选。

#### 4.1.2 核心流程

```text
高层参数（类型/布局/TileShape/Cluster/Alignment/Schedule/StageCount）
        │
        ├──> epilogue::collective::CollectiveBuilder  ──> CollectiveEpilogue
        │         （SharedStorage 大小回传给主循环算 stage）
        │
        └──> gemm::collective::CollectiveBuilder      ──> CollectiveMainloop
                  │
                  v
        kernel::GemmUniversal<ProblemShape, Mainloop, Epilogue, TileScheduler>  ──> GemmKernel
                  │
                  v
        device::GemmUniversalAdapter<GemmKernel>      ──> Gemm（主机句柄）
```

注意第 1、2 步之间有**依赖**：主循环 builder 计算「能塞进共享内存的最大 stage 数」时，需要先扣除 epilogue 占用的共享内存。所以代码里先用 epilogue builder，再用它的 `CollectiveEpilogue::SharedStorage` 大小喂给 `StageCountAutoCarveout`。

#### 4.1.3 源码精读

示例用 5 个模板参数把整套组装过程参数化（schedule、stage、tile scheduler、是否用自定义 EVT），都给了 `Auto` 默认值：

- 类型与布局别名（行主序 A、列主序 B，FP16 输入、FP32 累加）：
  [49_collective_builder.cu:264-281](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/49_hopper_gemm_with_collective_builder/49_collective_builder.cu#L264-L281)
  —— 关键点：`ElementAccumulator = float`（累加用 FP32，保证精度），`Alignment = 16/sizeof(Element)`（half_t 占 2 字节，对齐到 8 元素 = 16 字节，这正是**启用 TMA 的对齐门槛**）。

- epilogue builder + 预定义融合操作 `LinearCombination`：
  [49_collective_builder.cu:307-316](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/49_hopper_gemm_with_collective_builder/49_collective_builder.cu#L307-L316)
  —— 给定 `Sm90`/`OpClassTensorOp`、epilogue tile `Shape<_128,_128,_64>`、累加器/计算/C/D 类型与对齐、epilogue schedule，产出 `CollectiveOp`。

- mainloop builder，注意 `StageCountAutoCarveout` 把 epilogue 共享内存扣除：
  [49_collective_builder.cu:318-328](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/49_hopper_gemm_with_collective_builder/49_collective_builder.cu#L318-L328)
  —— 主循环 tile `Shape<_128,_128,_64>`、cluster `Shape<_2,_1,_1>`。

- 内核 + 主机句柄：
  [49_collective_builder.cu:330-337](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/49_hopper_gemm_with_collective_builder/49_collective_builder.cu#L330-L337)
  —— `ProblemShape` 是 `Shape<int,int,int,int>`（M,N,K,L，L 是 batch 维）。`GemmUniversalAdapter` 包一层即得到 `Gemm`。

builder 怎么把 schedule 标签映射到具体 collective 文件？以 `KernelTmaWarpSpecialized` 为例，SM90 builder 的 SS（shared-shared）分支会把它填进 `MainloopSm90TmaGmmaWarpSpecialized` 策略：
[sm90_gmma_builder.inl:284-285](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/collective/builders/sm90_gmma_builder.inl#L284-L285)
而 schedule 标签本身定义在：
[dispatch_policy.hpp:117-124](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/dispatch_policy.hpp#L117-L124)
—— `KernelTma`、`KernelTmaWarpSpecialized`、`KernelTmaWarpSpecializedPingpong`、`KernelTmaWarpSpecializedCooperative` 都是空结构体，纯粹做编译期标签分派。

`GemmUniversal` 主模板本身只做 `#include` 各架构内核文件（按 `Schedule` 标签的偏特化选中对应那一个）：
[gemm_universal.hpp:56-66](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/gemm_universal.hpp#L56-L66)

#### 4.1.4 代码实践

**实践目标**：验证你对组装流程的理解，看清 builder 到底为 FP16 选了哪条路径。

**操作步骤**：

1. 打开 `examples/49_hopper_gemm_with_collective_builder/49_collective_builder.cu`，定位到 4.1.3 引用的四段 typedef。
2. 在 `using Gemm = ...;` 这一行之后，**临时加一行**静态断言（示例代码，不影响逻辑）：
   ```cpp
   static_assert(cute::is_base_of_v<cutlass::gemm::KernelTmaWarpSpecialized,
       typename CollectiveMainloop::DispatchPolicy::Schedule>,
       "默认 Auto schedule 应当落到 KernelTmaWarpSpecialized 家族");
   ```
3. 用 `CUTLASS_NVCC_ARCHS=90a` 编译该示例（命令见 4.5 节或 u1-l2）。

**需要观察的现象**：编译能通过，说明默认 `KernelScheduleAuto` + FP16 落到了 `KernelTmaWarpSpecialized`（或其子类 Pingpong/Cooperative）这条 warp-specialized 分支上。

**预期结果**：编译成功。若断言失败，说明你的 schedule 推断有误，回看 4.1.3 的 builder 映射。**待本地验证**（需真实 Hopper 卡或仅做编译期断言验证）。

> 提示：验证完成后记得删掉这行临时断言，别改动示例原有逻辑。

#### 4.1.5 小练习与答案

**练习 1**：为什么必须先用 epilogue builder、再用 mainloop builder？反过来会怎样？
**答案**：mainloop builder 在 `StageCountAuto` 时要算「共享内存最多能塞几级缓冲」，必须先知道 epilogue 占多少共享内存并扣除（`StageCountAutoCarveout`）。反过来则 mainloop 不知道要给 epilogue 预留多少，可能 stage 数估错。

**练习 2**：示例里 `AlignmentA = 16 / sizeof(half_t) = 8`。如果把元素类型换成更窄的 FP8（1 字节），对齐值与是否启用 TMA 会怎样？
**答案**：`16 / 1 = 16`，即按 16 元素对齐。只要仍满足 16 字节对齐门槛，TMA 依然可用；只是走的是 FP8 专属的 RS（mixed-input）collective 而非本讲的 SS collective。

---

### 4.2 Warp Specialized 调度：内核如何分流 producer/consumer

#### 4.2.1 概念说明

「Warp specialization」直译是「warp 专门化」：在一个 CTA 里，不同 warp 干不同事。Hopper 的 basic warp-specialized 内核（`KernelTmaWarpSpecialized`）用 **2 个 warp group**（共 256 线程）：

- **1 个 producer warp group**，但其中只有 **1 个 warp**（叫 DMA warp）真正干活，负责用 TMA 把 A/B 从显存搬到共享内存。
- **1 个 consumer warp group**（128 线程），负责发 wgmma 算乘加，最后跑 epilogue 写回 D。

注意这里有个反直觉点：producer 这一组虽然占了一个 warp group 的「编制」，但实际只有 1 个 warp 在发 TMA（TMA 是单线程发起的硬件单元）。代码里用 `NumLoadWarpGroups = 1`、`NumMmaWarpGroups = 1` 描述这个结构。

为什么要把「搬」和「算」分到不同 warp？因为 Hopper 的 wgmma 是**异步**的：consumer 发出一条 wgmma 后可以立刻去等下一块数据，而 producer 可以在 consumer 算的同时把下一块 A/B 灌进共享内存——这就是「搬算重叠」，是 Hopper 性能的关键。

#### 4.2.2 核心流程

内核 `operator()` 的执行流（一个 CTA 内）：

```text
进入 operator()(params, smem_buf)
  ├─ 算出本线程的 warp_group_role（Producer / Consumer）和 producer_warp_role
  ├─ 各自构造 pipeline（mainloop pipeline、epi_load pipeline、epi_store pipeline）
  ├─ cluster_wait_fn()   // 等簇内所有 CTA 就位
  │
  ├─【若 warp_group_role == Producer 且 producer_warp_role == MainloopEpilogue】
  │     ├─ collective_mainloop.load(...)     // 发 TMA 灌 A/B
  │     ├─ collective_mainloop.load_tail(...)
  │     └─ collective_epilogue.load(...)     // 这同一个 warp 再发 C 的 TMA
  │            collective_epilogue.load_tail(...)
  │
  └─【若 warp_group_role == Consumer】
        ├─ collective_mainloop.mma(...)      // 发 wgmma，累加到 accumulators
        ├─ collective_mainloop.mma_tail(...)
        └─ collective_epilogue.store(...)    // α/β/激活，写回 D
               collective_epilogue.store_tail(...)
```

注意 producer 的 `ProducerWarpRole::MainloopEpilogue`——这 1 个 DMA warp 既搬主循环的 A/B，**也**搬 epilogue 的 C。所以它从头到尾都是 producer，干完主循环搬运接着搬 C，等 consumer 算完。

#### 4.2.3 源码精读

内核模板的偏特化条件——只有当主循环策略的 `Schedule` 是 `KernelTmaWarpSpecialized`（或其子类）时才选中此内核：
[sm90_gemm_tma_warpspecialized.hpp:59-71](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/sm90_gemm_tma_warpspecialized.hpp#L59-L71)

「2 warp group」结构定义，以及线程数 = TiledMma 线程数 + 1 个 load warp group：
[sm90_gemm_tma_warpspecialized.hpp:140-143](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/sm90_gemm_tma_warpspecialized.hpp#L140-L143)
—— `NumLoadWarpGroups = 1`、`NumMmaWarpGroups = 1`，`MaxThreadsPerBlock = size(TiledMma{}) + 1*128`（SM90 一个 warp group = 128 线程，所以总 256）。

共享内存用 `union` 让 mainloop 和 epilogue **复用**同一块 smem（因为这个内核是非持久化的，二者不会同时用）：
[sm90_gemm_tma_warpspecialized.hpp:120-137](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/sm90_gemm_tma_warpspecialized.hpp#L120-L137)

角色枚举（这是理解 warp specialization 的核心）：
[sm90_gemm_tma_warpspecialized.hpp:283-292](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/sm90_gemm_tma_warpspecialized.hpp#L283-L292)

producer 分支（DMA warp 发 mainloop TMA，再发 epilogue TMA）：
[sm90_gemm_tma_warpspecialized.hpp:430-466](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/sm90_gemm_tma_warpspecialized.hpp#L430-L466)

consumer 分支（math warp group 发 wgmma，再 store）：
[sm90_gemm_tma_warpspecialized.hpp:468-515](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/sm90_gemm_tma_warpspecialized.hpp#L468-L515)

> 进阶细节：上面这个 basic 版本（`KernelTmaWarpSpecialized`）是 1 DMA warp + 1 math warp group。示例里还演示了 `KernelTmaWarpSpecializedPingpong`（两个 math warp group 乒乓，提吞吐）和 `KernelTmaWarpSpecializedCooperative`（多个 math warp group 协作算同一个 tile）。它们对应 `sm90_gemm_tma_warpspecialized_pingpong.hpp` / `..._cooperative.hpp`，角色划分更复杂，但 producer/consumer 分流思想一致。

#### 4.2.4 代码实践

**实践目标**：用「源码阅读型实践」确认 producer 与 consumer 的角色划分与各自的工作量。

**操作步骤**：

1. 打开 `sm90_gemm_tma_warpspecialized.hpp` 的 `operator()`（行 267 起）。
2. 找到 `warp_group_role` 的计算（行 302）和 `producer_warp_role`（行 303）。
3. 在 producer 分支（行 430）标注：该 warp 调用了哪几个 collective 方法（`load`、`load_tail`、epilogue 的 `load`、`load_tail`）。
4. 在 consumer 分支（行 468）标注：该 warp group 调用了哪几个方法（`mma`、`mma_tail`、epilogue 的 `store`、`store_tail`）。

**需要观察的现象**：producer 全程**只搬不算**（不发任何 wgmma），consumer 全程**只算不搬**（不发任何 TMA load）；二者靠 `mainloop_pipeline` 同步。

**预期结果**：画出如下分工表（答案见下）。这是纯源码阅读，不需要 GPU。

#### 4.2.5 小练习与答案

**练习 1**：填空——producer warp 发完主循环的 `load_tail` 后，紧接着做什么？
**答案**：检查 `collective_epilogue.is_producer_load_needed()`，若需要则发 epilogue 的 C 加载 TMA（`epi_load_pipeline`），再 `load_tail`。即同一个 DMA warp 把主循环和 epilogue 的搬运都包了。

**练习 2**：为什么共享内存用 `union` 把 mainloop 和 epilogue 的 tensor storage 合并？
**答案**：这个内核非持久化，每个 CTA 只算一个输出 tile：先全程做 mainloop（用 mainloop smem），算完后再做 epilogue（用 epilogue smem），两者**时间上不重叠**。用 `union` 复用同一块 smem 能显著省共享内存，提高 occupancy（每个 SM 能同时驻留更多 CTA）。持久化内核（Pingpong/Cooperative）因为 mainloop 与 epilogue 可能重叠，就不能这样简单复用。

---

### 4.3 主循环 collective 的 load 与 mma

#### 4.3.1 概念说明

上一模块讲了 kernel 外壳如何分流。这一模块打开 collective（主循环）看 **producer 怎么搬、consumer 怎么算**。本例默认的 `KernelTmaWarpSpecialized` + FP16 走的是 **SS（shared-shared）** 主循环：A 和 B 都从共享内存喂给 wgmma（通过 smem descriptor）。

两个核心方法：

- `load(...)`（producer 视角）：循环 K 方向的每个 tile，`producer_acquire` 拿到一个缓冲槽 → 用 TMA 把 A/B 拷进该槽 → 推进 pipeline 写指针。
- `mma(...)`（consumer 视角）：循环 K 方向的每个 tile，`consumer_wait` 等数据就绪 → 用 `cute::gemm(tiled_mma, A_desc, B_desc, accum)` 发 wgmma → 推进 pipeline 读指针、释放缓冲槽。

二者通过 `PipelineTmaAsync<Stages>` 这条**多级异步流水线**耦合：producer 写第 N 槽、consumer 读第 N-K 槽，K 在飞（in-flight）的 wgmma 数受 `K_PIPE_MMAS` 控制。

#### 4.3.2 核心流程

主循环的时间线（搬算重叠的本质）：

```text
时间 ─────────────────────────────────────────────────►
producer:  灌stage0 灌stage1 灌stage2 灌stage3 ...  灌C
consumer:                  算stage0 算stage1 算stage2 ...   算完→epilogue
                ▲ K_PIPE_MAX 级缓冲，producer 领先 consumer 最多 K_PIPE_MAX 步
```

wgmma 异步语义的形式化：一次 GEMM 内核对 K 维做了 \( T = \lceil K / K_{\text{tile}} \rceil \) 次 tile 累加，累加器更新为

\[
\text{accum} \;=\; \sum_{t=0}^{T-1} A_t \cdot B_t
\]

其中每个 \(A_t \cdot B_t\) 由一组 wgmma 完成，`accumulate_` 标志控制是否清零（首个 tile 用 `ScaleOut::Zero`，之后用 `One` 累加）。

#### 4.3.3 源码精读

主循环 collective 模板头（`MainloopSm90TmaGmmaWarpSpecialized` 策略 + SS）：
[sm90_mma_tma_gmma_ss_warpspecialized.hpp:54-88](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/collective/sm90_mma_tma_gmma_ss_warpspecialized.hpp#L54-L88)

主循环用的异步流水线类型——`Stages` 级缓冲：
[sm90_mma_tma_gmma_ss_warpspecialized.hpp:111-112](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/collective/sm90_mma_tma_gmma_ss_warpspecialized.hpp#L111-L112)

`load`（producer）：`producer_acquire` → `producer_get_barrier` 拿 TMA 完成屏障 → `copy(tma.with(*barrier,...), gA, sA)` 发 TMA。注意只有 `elect_one_sync()` 选出的那 1 个 lane 真正发 TMA：
[sm90_mma_tma_gmma_ss_warpspecialized.hpp:309-392](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/collective/sm90_mma_tma_gmma_ss_warpspecialized.hpp#L309-L392)

`mma`（consumer）：`consumer_wait` 等数据 → 内层 `cute::gemm(tiled_mma, tCrA(...), tCrB(...), accum)` 发 wgmma → `warpgroup_commit_batch()`。首个 tile 把累加器清零，之后累加：
[sm90_mma_tma_gmma_ss_warpspecialized.hpp:416-527](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/collective/sm90_mma_tma_gmma_ss_warpspecialized.hpp#L416-L527)

> 这正是 u2-l3 讲的「算法与张量解耦」的回报：`cute::copy`/`cute::gemm` 因张量的内存空间（gmem/smem/rmem）不同，在编译期自动选到 TMA 指令与 wgmma 指令，零运行时开销。

#### 4.3.4 代码实践

**实践目标**：理解 producer/consumer 通过 pipeline 的握手点。

**操作步骤**：

1. 在 `load`（行 372 的 `for` 循环）里找到 producer 调用的三个 pipeline 方法：`producer_acquire`、`producer_get_barrier`、隐式的「推进 `smem_pipe_write`」（行 389）。
2. 在 `mma` 里找到 consumer 对应的三个方法：`consumer_wait`（行 487）、`cute::gemm`（行 496）、`consumer` 释放（`smem_pipe_release`，行 477 起）。
3. 对应起来：producer 写槽 N、consumer 读槽 N、consumer 算完槽 N 释放回给 producer 复用。

**需要观察的现象**：producer 与 consumer 操作的是**同一个 pipeline 的不同 state**（`smem_pipe_write` vs `smem_pipe_read`），靠 `Stages` 级环形缓冲解耦，谁快了就阻塞等待。

**预期结果**：你能说出「producer_acquire 阻塞，当且仅当所有缓冲槽都还没被 consumer 释放」这句话是对的。纯源码阅读，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：`load` 里为什么用 `if (lane_predicate)`（即 `elect_one_sync()`）把整个 TMA 发射包起来？
**答案**：TMA 是单线程发起的硬件单元——一个 warp 里只需 1 个 lane 调用 `copy(tma.with(...),...)` 即可触发整块搬运，其余 lane 不参与。`elect_one_sync()` 选出该 lane，避免 32 个 lane 重复发射造成错误。

**练习 2**：wgmma 的 A/B 操作数是「register」还是「shared memory descriptor」？这跟 SS 主循环的名字有什么关系？
**答案**：本 SS 主循环里 A、B 都从共享内存读，wgmma 用 **shared memory descriptor**（`GMMA::DescriptorIterator`）作为操作数（见行 138-140 的 static_assert）。SS = A、B 都 Source-shared。与之对照，RS（register-shared）主循环把 A 放进寄存器再喂 wgmma，常用于 FP8/混合精度。

---

### 4.4 Epilogue Builder 与融合操作

#### 4.4.1 概念说明

Epilogue 是 GEMM 的「后处理」：拿到累加器 `acc = A·B`，做线性组合 \(D = \alpha \cdot \text{acc} + \beta \cdot C\)、可选激活（ReLU/GELU）、缩放等，再写回显存。CUTLASS 3.x 提供两条路：

1. **预定义融合操作**（`include/cutlass/epilogue/fusion/operations.hpp` 里的 tag，如 `LinearCombination`）：填几个标量参数即可，简单友好。
2. **自定义 EVT**（Epilogue Visitor Tree）：把融合表示成一棵计算图，每个节点是 load/store/compute 之一。灵活但需要按树的递归结构填参数。

和主循环 builder 一样，epilogue 也有自己的 `CollectiveBuilder`，吃 epilogue schedule（如 `TmaWarpSpecialized`、`NoSmemWarpSpecialized`）和融合操作，产出 `CollectiveEpilogue`。

#### 4.4.2 核心流程

epilogue 计算的标准形式：

\[
D_{ij} \;=\; \text{Epilogue}\bigl(\text{acc}_{ij},\, C_{ij},\, \alpha,\, \beta\bigr)
\]

`LinearCombination` 实现 \(D = \alpha \cdot \text{acc} + \beta \cdot C\)。自定义 EVT 则把同一公式拆成一棵树：

```text
        multiply_add (根：β*C + α*acc)
        /          \
   β (ScalarBcast)   multiply (α*acc)
                      /        \
                α (ScalarBcast) AccFetch
```

#### 4.4.3 源码精读

示例同时定义了「自定义 EVT」与「预定义操作」二选一（`UseCustomEVT` 控制）：

自定义 EVT 的类型定义（把 `β*C + α*acc` 表达为 `Sm90EVT` 树）：
[49_collective_builder.cu:291-299](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/49_hopper_gemm_with_collective_builder/49_collective_builder.cu#L291-L299)

预定义融合操作（`LinearCombination`，免手写树）：
[49_collective_builder.cu:305](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/49_hopper_gemm_with_collective_builder/49_collective_builder.cu#L305)

epilogue builder 本体——最后一参用 `conditional_t` 在「自定义 EVT」与「预定义操作」间切换：
[49_collective_builder.cu:307-316](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/49_hopper_gemm_with_collective_builder/49_collective_builder.cu#L307-L316)

参数填法的差异：预定义操作用**命名的扁平字段**（`arguments.epilogue.thread.alpha`/`.beta`），自定义 EVT 用**嵌套匿名 tuple**（按树的 `{first_child_args,...,last_child_args,op_args}` 递归结构）：
[49_collective_builder.cu:452-469](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/49_hopper_gemm_with_collective_builder/49_collective_builder.cu#L452-L469)

> 兼容性约束（示例注释 240-249 行强调）：若用 `Auto` schedule，主循环和 epilogue 必须**都**用 `Auto`；若指定具体 schedule，两者**都**要指定且兼容。自定义 EVT 目前只支持 TMA warp-specialized epilogue（见行 283-286 的 static_assert）。

#### 4.4.4 代码实践

**实践目标**：把自定义 EVT 的「树」与它的「嵌套参数 tuple」对上号。

**操作步骤**：

1. 画出 4.4.2 中的 EVT 树。
2. 对照行 452-464 的嵌套 tuple 初始化，标注每一层 tuple 对应树的哪个节点：最外层对应根 `multiply_add`，其 3 个子 tuple 分别对应 `β`、`C`、内层 `multiply(α*acc)`；最内层又 2 个子 tuple 对应 `α`、`acc`。
3. 数一下：树的每个非叶节点的参数，是否都在 tuple 里有一个对应位置（叶节点 op 自身无额外 args 用 `{}`）。

**需要观察的现象**：tuple 的嵌套深度 = EVT 树的深度；每层 tuple 的「最后」位置是该节点 op 的 args（如 `multiplies`、`multiply_add` 自身通常无参，故 `{}`）。

**预期结果**：你能把树和 tuple 一一对应。纯源码阅读。**更深入的 EVT 实践见 u2-l10（Epilogue 与 EVT 访客树）。**

#### 4.4.5 小练习与答案

**练习 1**：用预定义 `LinearCombination` 时，α/β 怎么传？用自定义 EVT 时又怎么传？
**答案**：预定义：`arguments.epilogue.thread.alpha = ...; .beta = ...;`（命名字段）。自定义 EVT：按树的递归结构填嵌套匿名 tuple `{ {β}, {}, { {α}, {}, {} }, {} }`。

**练习 2**：为什么示例强调「`Auto` schedule 必须主循环和 epilogue 同时用」？
**答案**：主循环和 epilogue 的 schedule 决定了它们各自选哪个 collective 特化，二者必须在共享内存布局、pipeline 结构、TMA 使用上**兼容**才能正确组装。一边 `Auto` 一边指定具体 schedule，可能选出不兼容的组合，运行时崩溃或结果错误。

---

### 4.5 kernel 启动与参数：从 Arguments 到 cluster launch

#### 4.5.1 概念说明

到目前为止都是**类型组装**（编译期）。运行期，主机代码要：构造 `Arguments`（逻辑参数：问题尺寸、指针、stride、α/β、硬件信息）→ 调用句柄方法 → 内核真正启动。

主机句柄 `GemmUniversalAdapter` 是**状态化**的（持有内部 `params_`），提供三步 API：

1. `can_implement(args)`：运行期合法性检查（对齐、问题尺寸、schedule 兼容性）。
2. `initialize(args, workspace)`：把 `Arguments` 翻译成设备端 `Params`（如构造 TMA descriptor），并设置动态共享内存上限。
3. `run()` / `operator()(args)`：算出 grid/block/cluster、用 cluster launch API 启动内核。

SM90+ 内核用 Hopper 的 **cluster launch API**（`cudaLaunchKernelEx` 系），需要额外提供 cluster 形状。

#### 4.5.2 核心流程

```text
Arguments（主机逻辑参数）
   │  can_implement(args)        // 检查
   │  initialize(args, workspace)
   v
Params（设备端参数：TMA descriptor、指针、stride、problem shape）
   │  run(params_)
   v
get_grid_shape / get_block_shape   // 算 grid(M/T_M · N/T_N · ...)、block(256)
设置 smem_size / cudaFuncSetAttribute（>48KB 时）
cluster = DispatchPolicy::ClusterShape
   v
cutlass::cluster_launch (SM90) 或 kernel_launch (静态 1x1x1)
   v
GPU 执行：每个 CTA 跑一遍 operator()（见 4.2）
```

#### 4.5.3 源码精读

示例 `run()` 方法——构造 `Arguments`、`can_implement`→`initialize`→`run` 三连、再 verify：
[49_collective_builder.cu:432-512](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/49_hopper_gemm_with_collective_builder/49_collective_builder.cu#L432-L512)
—— 注意 `Arguments` 的结构：`{mode, problem_size, mainloop{ptr_A,stride_A,ptr_B,stride_B}, epilogue{{...}, ptr_C, stride_C, ptr_D, stride_D}, hw_info}`。`hw_info.sm_count` 决定持久化内核的调度。

`main()` 检查 Hopper + CUDA 12，并查询 SM 数填进 `KernelHardwareInfo`：
[49_collective_builder.cu:537-583](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/49_hopper_gemm_with_collective_builder/49_collective_builder.cu#L537-L583)

主机句柄的 `initialize`：调 `GemmKernel::to_underlying_arguments`（构造 Params/TMA descriptor）、必要时 `cudaFuncSetAttribute` 放开动态共享内存上限（≥48KB）：
[gemm_universal_adapter.h:312-356](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/device/gemm_universal_adapter.h#L312-L356)

`run` 的启动分支：SM90 内核走 cluster launch。cluster 形状取自 `DispatchPolicy::ClusterShape`；静态 `1x1x1`（如本例 `Shape<_2,_1,_1>` 不是 1x1x1，故走 `ClusterLauncher`）：
[gemm_universal_adapter.h:374-397](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/device/gemm_universal_adapter.h#L374-L397)
实际发射（`ClusterLauncher::launch` 或静态 `kernel_launch`）：
[gemm_universal_adapter.h:459-483](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/device/gemm_universal_adapter.h#L459-L483)

`operator()(args)` 与无参 `run()` 的关系（示例正是先 `initialize` 再 `run()`）：
[gemm_universal_adapter.h:582-622](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/device/gemm_universal_adapter.h#L582-L622)

`main()` 里用不同 schedule/stage 实例化多个 `ExampleRunner`，演示同一组装框架下的多种调度策略：
[49_collective_builder.cu:600-654](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/49_hopper_gemm_with_collective_builder/49_collective_builder.cu#L600-L654)

#### 4.5.4 代码实践

**实践目标**：编译并运行 example 49，对照参考实现验证；这是本讲的核心实践（也是规格指定的综合实践的前置）。

**操作步骤**：

1. 准备 build 目录（参见 u1-l2）：
   ```bash
   mkdir -p build && cd build
   cmake .. -DCUTLASS_NVCC_ARCHS=90a -DCUTLASS_ENABLE_EXAMPLES=ON
   ```
2. 只编译 example 49（避免全量编译耗时）：
   ```bash
   make 49_collective_builder -j
   ```
3. 运行（用小尺寸先冒烟）：
   ```bash
   ./examples/49_hopper_gemm_with_collective_builder/49_collective_builder --m=512 --n=512 --k=512 --l=1
   ```

**需要观察的现象**：程序依次打印 7 行结果，每行形如 `...: Passed`（分别对应 Auto schedule、5 stages、KernelTma、KernelTmaWarpSpecialized、Pingpong、Cooperative+StreamK、Cooperative+自定义 EVT）。

**预期结果**：7 个变体全部 `Passed`。若无 Hopper 卡，编译会因 `CUTLASS_NVCC_ARCHS=90a` 仍可成功，但运行会因 `props.major != 9` 提前打印「requires Hopper」并退出——这种情况下标注「待本地验证（需 SM90 设备）」。

> ⚠️ 编译务必用 `90a`（带 a），不是 `90`。不带 a 不会生成 wgmma 指令，内核里会命中 `CUTE_INVALID_CONTROL_PATH`。

#### 4.5.5 小练习与答案

**练习 1**：示例 `run()` 里先调 `can_implement` 再 `initialize` 再 `run`。省略 `can_implement` 会怎样？
**答案**：功能上仍可能跑通，但失去运行期合法性检查（对齐、schedule 兼容、问题尺寸）。不兼容的参数会带着错误状态走进内核，可能崩溃或算错且难定位。`can_implement` 是「廉价保险」，建议保留。

**练习 2**：`KernelHardwareInfo::sm_count` 在 example 49 的 basic 非持久化内核里重要吗？对哪些 schedule 重要？
**答案**：basic `KernelTmaWarpSpecialized` 是非持久化的（每个 CTA 算一个 tile，grid 数 = tile 数），`sm_count` 影响不大。但对持久化调度（Pingpong、Cooperative，以及示例里的 `PersistentScheduler`/`StreamKScheduler`），内核常驻 SM 反复领任务，`sm_count` 直接决定 grid 大小和调度，至关重要。

---

## 5. 综合实践：把 example 49 改成 BF16 输入、FP32 累加

这是本讲的贯穿任务（对应规格指定的 practice_task）。把上述 5 个模块串起来。

**任务**：基于 example 49，把输入从 FP16 改为 BF16，累加仍用 FP32，编译并对照参考实现验证。

**为什么这个改动「小而精」**：example 49 本来就是 FP16 输入 + FP32 累加（`ElementA = ElementB = half_t`，`ElementAccumulator = float`）。BF16 与 FP16 都是 2 字节类型，对齐（`16/sizeof = 8`）不变、能继续用 TMA、仍走同一条 SS warp-specialized collective——所以**只需改两个 typedef**，却足以让你把「类型别名 → builder 选择 → 启动验证」整条链路亲手走一遍。

**操作步骤**：

1. 复制一份示例避免改原文件（推荐）：
   ```bash
   cp -r examples/49_hopper_gemm_with_collective_builder examples/49_bf16_gemm
   ```
   并把新目录的 `CMakeLists.txt` 与 `.cu` 文件名改一下（示例代码层面）。
2. 在 `.cu` 的 `ExampleRunner` 里，把类型别名改两行（定位 4.1.3 引用的 264-272 行）：
   ```cpp
   using ElementA = cutlass::bfloat16_t;   // 原为 cutlass::half_t
   using ElementB = cutlass::bfloat16_t;   // 原为 cutlass::half_t
   ```
   `ElementAccumulator` 保持 `float`（本来就是）。
3. 确认 `AlignmentA/AlignmentB` 仍为 `16/sizeof(bfloat16_t) = 8`，无需改动。
4. 用 `CUTLASS_NVCC_ARCHS=90a` 编译，运行并查看是否全部 `Passed`。

**需要观察的现象**：

- 编译期：builder 为 BF16（2 字节）选的应当仍是 SS 的 `sm90_mma_tma_gmma_ss_warpspecialized`（与 FP16 同分支）。
- 运行期：7 个变体全部 `Passed`，与 FP16 版本行为一致（仅数值精度特性不同：BF16 动态范围大、精度低）。

**预期结果**：所有变体 `Passed`。`verify()` 用的参考内核 `cutlass::reference::device::GemmComplex` 同样以 `ElementAccumulator=float` 累加，应与 CUTLASS 内核位级一致（BF16 输入下两者都先升精度再乘加）。

**对照思考（不必改代码）**：若继续把输入换成 **FP8**（1 字节，如 `float_e4m3_t`），还能走同一条 collective 吗？
**答案**：不能。FP8/混合精度会走 **RS**（register-shared）分支（`sm90_mma_tma_gmma_rs_warpspecialized` 或 mixed-input 变体），A 进寄存器、带反量化，与本讲的 SS 不是同一个 collective 文件。这正是 builder 的价值——你只改类型，它自动换 collective。这部分留到 u3-l6（低精度与 block-scaled）深入。

**验证约束**：本任务需 SM90（Hopper）设备才能运行验证；无设备时标注「待本地验证」，但类型改动与编译可先完成。

---

## 6. 本讲小结

- 一个 Hopper 3.x GEMM = **epilogue builder → mainloop builder → `kernel::GemmUniversal` → `device::GemmUniversalAdapter`** 四步 typedef；builder 用 `Auto` 标签自动推断 schedule/stage，`Auto` 必须主循环与 epilogue 同时使用。
- **Warp specialization** 在 Hopper basic 内核里是「1 个 producer warp group（实际 1 个 DMA warp 发 TMA，还兼搬 epilogue 的 C）+ 1 个 consumer warp group（发 wgmma 算、再 store）」，靠 `PipelineTmaAsync<Stages>` 多级缓冲同步、搬算重叠。
- 主循环分 **producer 的 `load`**（`producer_acquire`→TMA→推进写指针）与 **consumer 的 `mma`**（`consumer_wait`→`cute::gemm` 发 wgmma→释放缓冲）；FP16 默认走 **SS**（A、B 都从 smem descriptor 读）。
- **Epilogue** 有两条路：预定义融合操作（`LinearCombination`，命名字段 α/β）与自定义 EVT（计算图，嵌套匿名 tuple 参数）；二者由 builder 的最后一参切换。
- 主机端走 `can_implement → initialize → run` 三步：`Arguments→Params`（构造 TMA descriptor）、放开动态 smem 上限、用 **cluster launch API**（cluster 形状取自 `DispatchPolicy::ClusterShape`）发射内核。
- 实操要点：必须 `CUTLASS_NVCC_ARCHS=90a`；把 FP16 改 BF16 只动两个 typedef 即可（同为 2 字节、同走 SS collective）；改 FP8 则会切到 RS collective。

---

## 7. 下一步学习建议

本讲你已能把一个 Hopper GEMM 从声明跑到验证。接下来推荐：

1. **u3-l1（Async Pipeline 与 Warp Specialization）**：本讲多次出现 `PipelineTmaAsync`、`producer_acquire/consumer_wait`，但没展开。下一讲深入 `include/cutlass/pipeline/` 的 stage/phase/barrier 同步原语，是理解 Pingpong/Cooperative 多 math warp group 调度的前提。
2. **u3-l2（TMA 异步张量拷贝）**：本讲把 TMA 当黑盒（`copy(tma.with(*barrier,...), gA, sA)`）。下一讲打开 TMA descriptor 的构造、box/步长表达与对齐要求。
3. **u2-l10（Epilogue 与 EVT 访客树）**：本讲只点了 EVT 的皮。若要做更复杂融合（激活、逐元素缩放、per-tensor scale），必须系统学 EVT 访客树。
4. **直接读源码**：把 example 49 的 7 个 schedule 变体逐一对照 `sm90_gemm_tma_warpspecialized*.hpp`（basic / pingpong / cooperative）三个内核文件，体会「同一个组装框架，不同 schedule → 不同 producer/consumer 结构」的设计。

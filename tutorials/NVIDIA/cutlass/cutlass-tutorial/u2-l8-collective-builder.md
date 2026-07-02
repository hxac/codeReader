# CollectiveBuilder 与主循环

## 1. 本讲目标

在上一讲（u2-l7）我们建立了 CUTLASS 3.x 的通用模型：一次 GEMM = 一个无状态 `GemmUniversal` 内核 + `CollectiveMainloop`（搬数据 + MMA）+ `CollectiveEpilogue`（后处理写回）+ `TileScheduler`（分配 tile）。本讲回答两个紧接着的问题：

1. **谁帮我算出 CollectiveMainloop 那一大堆模板参数？** —— `CollectiveBuilder`。它根据「架构 + 数据类型 + 布局 + 调度策略」自动推断出 `TiledMma`、TMA 拷贝原子、共享内存布局、流水线级数等，最终产出可直接塞进 `GemmUniversal` 的 `CollectiveOp`。
2. **CollectiveMainloop 内部到底是怎么搬数据和算乘加的？** —— 它把工作拆成 **producer（生产者，搬数据）** 和 **consumer（消费者，做 MMA）** 两组 warp，靠一条异步流水线协作。本讲会带你看完一条完整的 SM90 Hopper 主循环。

学完后你应当能够：

- 说出 `CollectiveBuilder` 的输入参数有哪些，以及它 `enable_if` 分派到具体集体实现的机制；
- 解释 `CollectiveMma` 主模板的 14 个模板参数分别代表什么；
- 描述 producer/consumer 两组 warp 各自执行的循环，以及它们如何用 `PipelineTmaAsync` 同步；
- 读懂主循环里 `cute::copy`（smem→rmem）与 `cute::gemm`（WGMMA）的交替。

## 2. 前置知识

阅读本讲前，请确保你已经理解（这些在依赖讲义中讲过）：

- **CuTe Layout / Tensor**（u2-l1、u2-l2）：坐标→下标映射、`make_tensor` 把指针包装成张量。
- **CuTe Atoms 与 TiledMma**（u2-l4）：`MMA_Atom`/`Copy_Atom` 封装硬件指令，`make_tiled_mma`/`make_tiled_copy` 把指令沿线程铺开。
- **arch 层 MMA 指令**（u2-l5）：`wgmma.mma_async`（warp group 级、128 线程、操作数可来自共享内存描述符）。
- **3.x 通用模型**（u2-l7）：`dispatch_policy` 里的 `Schedule` 标签（如 `KernelTmaWarpSpecialized`）、`GemmUniversalAdapter`、producer/consumer 的概念。

本讲频繁出现的术语补充：

- **producer / consumer**：warp-specialized（warp 专门化）内核里，负责发 TMA 拷贝的 warp 叫 producer（DMA warp），负责发 MMA 指令的 warp 叫 consumer（math warp）。它们并发执行、用流水线同步。
- **TMA**：Hopper 的 Tensor Memory Accelerator，一条指令搬一整块张量，比逐线程 `cp.async` 高效。详见 u3-l2，本讲只用它的接口。
- **wgmma**：`warp group MMA`，Hopper 的异步矩阵乘加指令，操作数可来自寄存器（rmem）或共享内存描述符（smem）。

## 3. 本讲源码地图

本讲涉及的关键文件（按「自顶向下」阅读顺序排列）：

| 文件 | 作用 |
| --- | --- |
| `include/cutlass/gemm/collective/collective_builder_decl.hpp` | `CollectiveBuilder` 的**主模板声明**与 `StageCount`/`KernelScheduleAuto` 等辅助类型。主模板永远 `static_assert` 失败，真正实现在各 `.inl` 特化里。 |
| `include/cutlass/gemm/collective/builders/sm90_gmma_builder.inl` | **SM90（Hopper）的 CollectiveBuilder 特化**：根据 `KernelSchedule` 标签 + 数据类型/布局，推断出 `TiledMma`/TMA 拷贝/共享内存布局/级数，组装出 `CollectiveOp`。 |
| `include/cutlass/gemm/collective/collective_mma_decl.hpp` | `CollectiveMma`（即主循环）的**主模板声明**，14 个模板参数；主模板同样永远失败。 |
| `include/cutlass/gemm/collective/sm90_mma_tma_gmma_rs_warpspecialized.hpp` | **SM90 主循环的具体实现**（A 来自寄存器、B 来自共享内存）。含 `load()`（producer）、`mma()`（consumer）、`SharedStorage`。 |
| `include/cutlass/gemm/kernel/sm90_gemm_tma_warpspecialized.hpp` | **内核层**：按 `warp_group_role` 把 producer/consumer 分派到不同 warp group，分别调用上面的 `load()`/`mma()`。 |
| `examples/49_hopper_gemm_with_collective_builder/49_collective_builder.cu` | 用户视角的完整范例：用 `CollectiveBuilder` 一行构造 `CollectiveMainloop`，再交给 `GemmUniversal`。 |

> 说明：讲义规格里列的文件名是 `sm90_mma_tma_warpspecialized.hpp`，但仓库里 Hopper 的「集体主循环」实现实际叫 `sm90_mma_tma_gmma_rs_warpspecialized.hpp`（后缀 `gmma_rs` 表示「A 来自 rmem 寄存器、B 来自 smem 的 GMMA」）。内核层对应的文件是 `sm90_gemm_tma_warpspecialized.hpp`（注意 `gemm` 不是 `mma`）。本讲引用的是这两个真实文件。

## 4. 核心概念与源码讲解

### 4.1 CollectiveBuilder 的自动组装机制

#### 4.1.1 概念说明

`CollectiveBuilder` 是一个**编译期的「装配车间」**。你只告诉它高层意图（架构、算子类、A/B 的类型与布局、累加器类型、tile 形状、cluster 形状、级数策略、调度策略），它就替你算出 `CollectiveMma` 主循环所需的全部低层参数，最后给你一个 `::CollectiveOp` 类型别名。

为什么需要它？因为 `CollectiveMma` 一共有 **14 个模板参数**（`TiledMma`、两套 TMA 拷贝、两套共享内存布局原子、两套 smem copy atom、两个 transform……）。让用户手填这些既容易出错，也违背了 3.x「用策略标签代替手写偏特化森林」的设计哲学。`CollectiveBuilder` 把「策略 → 参数」的推断集中到一处。

它和 2.x 的 `DefaultGemmConfiguration` 类似（都是「给我意图、还你配置」），但更强大：它能根据 `Auto` 类型**自动算流水线级数**、自动选 TMA 拷贝原子。

#### 4.1.2 核心流程

`CollectiveBuilder` 的工作流可以概括为「主模板永远报错 + 一堆按条件 enable 的特化」：

```text
用户写: CollectiveBuilder<ArchTag, OpClass,
                          ElementA, LayoutA, AlignA,
                          ElementB, LayoutB, AlignB,
                          ElementAcc, TileShape, ClusterShape,
                          StageCount, KernelSchedule>::CollectiveOp
        │
        │  1) 用 enable_if<KernelSchedule 是某个 tag> 选中一个特化
        │  2) 在特化内部推断：
        │     - TiledMma            (调 GMMA::ss/rs_op_selector + make_tiled_mma)
        │     - GmemTiledCopyA/B    (按 cluster 形状选 TMA 拷贝原子)
        │     - SmemLayoutAtomA/B   (ss/rs_smem_selector)
        │     - DispatchPolicy      (MainloopSm90...<Stages, Cluster, Schedule>)
        │     - PipelineStages      (按 smem 容量预算自动算)
        │     - SmemCopyAtomA/B
        ▼
   using CollectiveOp = CollectiveMma<14 个参数>;
```

注意 `CollectiveBuilder` 和 `CollectiveMma` 都用 `DispatchPolicy`（即 `MainloopSm90...`）来选实现：builder 用 `KernelSchedule` 标签选 **builder 特化**，builder 再把这个标签塞进 `DispatchPolicy`，`CollectiveMma` 用 `DispatchPolicy` 选 **主循环特化**。两层分派，但都用「标签 + `enable_if`」，零运行时开销。

#### 4.1.3 源码精读

**① 主模板：永远失败的「兜底」**

`CollectiveBuilder` 的主模板有一句必定触发的 `static_assert`，提示参数组合不被支持：

[include/cutlass/gemm/collective/collective_builder_decl.hpp:L77-L95](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/collective/collective_builder_decl.hpp#L77-L95)

这段定义了 13 个模板形参（`Enable` 默认为 `void`），第 94 行的 `static_assert(sizeof(ElementA)==0,...)` 在「没有任何特化匹配」时编译报错——这是 CUTLASS 一贯的「主模板即错误提示」模式，逼用户看到明确的「Could not build a collective」。

**② 辅助类型：级数与调度策略**

同一个文件上方定义了控制级数与策略的辅助类型：

[include/cutlass/gemm/collective/collective_builder_decl.hpp:L40-L73](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/collective/collective_builder_decl.hpp#L40-L73)

要点：

- `StageCount<n>` 手动指定级数；`StageCountAutoCarveout<bytes>` 让 builder 按「共享内存容量 − 预留字节数」自动算最大级数；`StageCountAuto` 是 `StageCountAutoCarveout<0>` 的别名（一点不预留）。
- `StageCountAutoCarveoutEpi<CollectiveEpilogue>` 会查询 epilogue 要占多少 smem，自动把这部分从级数预算里扣掉（这是 example 49 里常见的写法）。
- `KernelScheduleAuto` 让 builder 自己挑最合适的调度策略。

**③ SM90 特化的分派条件（SS 版本：A、B 都来自共享内存）**

Hopper 的 builder 在 `sm90_gmma_builder.inl` 里。第一个特化（注释 `GMMA_TMA_WS_SS`）的 `enable_if` 条件是「`KernelSchedule` 是这几个 warp-specialized 标签之一，且不使用 rmem-A」：

[include/cutlass/gemm/collective/builders/sm90_gmma_builder.inl:L195-L231](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/collective/builders/sm90_gmma_builder.inl#L195-L231)

注意第 223–230 行的 `enable_if_t`：它用 `is_any_of_v<KernelScheduleType, ...>` 匹配 `KernelTmaWarpSpecialized` / `...Cooperative` / `...Pingpong` 等，并用 `not detail::is_use_rmem_A<...>()` 排除「应该走 RS 版本」的情形——这就是同一组标签被分到 SS 或 RS 两套特化的判据。

**④ 在特化内部推断派生类型并组装 `CollectiveOp`**

同一个 SS 特化的「主体」做了真正的推断：

[include/cutlass/gemm/collective/builders/sm90_gmma_builder.inl:L258-L308](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/collective/builders/sm90_gmma_builder.inl#L258-L308)

逐行解读：

- L258–259 `TiledMma`：用 `cute::GMMA::ss_op_selector<...>` 按数据类型与 tile 形状挑一条 wgmma 指令，再用 `make_tiled_mma` 沿 M/N 铺开（`Cooperative` 时铺两个 atom）。
- L261–262 `GmemTiledCopyA/B`：`sm90_cluster_shape_to_tma_atom` 根据 cluster 形状返回 TMA 拷贝原子（普通 `SM90_TMA_LOAD` 或多播 `..._MULTICAST`）。
- L264–267 `SmemLayoutAtomA/B`：`ss_smem_selector` 选一个 2 维的共享内存布局原子，让 wgmma 能高效读取。
- L275–276 `PipelineStages`：`compute_stage_count_or_override` 按「(smem 容量 − carveout) / 单级字节」算出最大级数（如果用户传了显式 `StageCount<n>` 就直接返回 `n`）。
- L278–287 `DispatchPolicy`：把 `Stages`、`ClusterShape`、`KernelSchedule` 打包成 `MainloopSm90TmaGmmaWarpSpecialized<...>`（FP8 输入换用专用变体）。
- L292–308 `CollectiveOp`：把上面推断出的全部 14 个参数填进 `CollectiveMma<...>`，作为类型别名暴露。

这就是「自动组装」的全部秘密——**全在编译期用类型计算完成，没有任何运行时分支**。

#### 4.1.4 代码实践

**实践目标：** 体验「策略标签 → 具体 builder 特化」的映射。

**操作步骤：**

1. 打开 `include/cutlass/gemm/collective/builders/sm90_gmma_builder.inl`，定位到第 195 行的 SS 特化（注释 `GMMA_TMA_WS_SS`）和第 313 行附近的 RS 特化（注释 `GMMA_TMA_WS_RS`）。
2. 阅读两段 `enable_if` 的判据（SS 是 `not is_use_rmem_A`，RS 是 `is_use_rmem_A || 混合精度`）。
3. 在 `include/cutlass/gemm/dispatch_policy.hpp` 中搜索这几个标签的定义：`KernelTmaWarpSpecialized`、`KernelTmaWarpSpecializedCooperative`、`KernelTmaWarpSpecializedPingpong`。

**需要观察的现象：** 同一个 `KernelTmaWarpSpecialized` 标签，在 SS/RS 两个 builder 特化的 `enable_if` 里都出现，靠的是「输入数据类型/布局是否要求 A 落在寄存器」这一**额外条件**来二选一。

**预期结果：** 你能用自己的话回答「为什么同一个调度标签会落到不同的集体文件」。一句话总结：**builder 用 `enable_if` 把「(标签, 数据类型, 布局)」三维空间切成若干互斥的特化，每块对应一个 `CollectiveMma` 主循环实现。**

> 不会编译运行（无 GPU 环境时），这是源码阅读型实践；若需运行验证，参考 4.4 节与综合实践。

#### 4.1.5 小练习与答案

**练习 1：** 用户传了 `KernelScheduleAuto`，`CollectiveBuilder` 最终选了什么策略？

**参考答案：** `KernelScheduleAuto` 不是某个具体特化的 `enable_if` 条件，builder 内部会把它**解析**成一个具体的 warp-specialized 标签（Hopper 上默认 `KernelTmaWarpSpecialized`），再用那个具体标签去匹配特化。

**练习 2：** 为什么 `CollectiveBuilder` 主模板的 `static_assert` 用 `sizeof(ElementA) == 0`？

**参考答案：** 因为 `sizeof(ElementA) == 0` 恒为假，`static_assert` 一定触发；又因为它依赖模板参数 `ElementA`，编译器要等模板实例化时才检查，从而避免在「主模板本身被定义」时立刻报错。这是依赖型断言的常见写法。

---

### 4.2 CollectiveMma 主循环的接口与装配

#### 4.2.1 概念说明

`CollectiveMma` 就是「主循环」本身——一个**无状态的集体算子**，负责把 A、B 从全局内存搬到共享内存（再按需搬到寄存器），并执行 MMA 累加。它和 `CollectiveEpilogue` 一样，是一个「外壳只组合、自身不存成员」的设备端类。

`CollectiveBuilder` 产出的 `CollectiveOp` 就是某个 `CollectiveMma` 特化。它的模板参数多达 14 个，可分成五组：

| 组 | 参数 | 含义 |
| --- | --- | --- |
| 策略与形状 | `DispatchPolicy`, `TileShape` | 主循环策略（级数/cluster/schedule）+ tile 形状 |
| 数据与布局 | `ElementA`, `StrideA`, `ElementB`, `StrideB` | A/B 的元素类型与步长（CuTe stride） |
| 计算单元 | `TiledMma` | 由 atom 铺开的线程级 MMA（封装 wgmma） |
| A 的搬运 | `GmemTiledCopyA`, `SmemLayoutAtomA`, `SmemCopyAtomA`, `TransformA` | 全局→共享拷贝（TMA）+ 共享内存布局 + smem→rmem 拷贝 + 可选变换 |
| B 的搬运 | `GmemTiledCopyB`, `SmemLayoutAtomB`, `SmemCopyAtomB`, `TransformB` | 同上 |

A 和 B 的参数完全对称，这正是「同一套机制搬两份 operand」的体现。

#### 4.2.2 核心流程

`CollectiveMma` 在运行期并不被一次「调用」就做完整个 GEMM。在 warp-specialized 内核里，它被**拆成两个视角**分别调用：

```text
producer warp:  load_init(...)  →  load(...)  循环发 TMA  →  load_tail(...)
                                                                   │ (流水线同步)
consumer warp:                       mma(...)  循环 wait→copy→gemm  →  mma_tail(...)
```

`load_init` 准备好 tiled 的全局张量（gA、gB），返回一个元组；`load` 是 producer 的主循环，`mma` 是 consumer 的主循环。这两个函数都不直接返回结果——累加器 `accum` 由 consumer 持有，最终交给 epilogue。`SharedStorage` 定义了 producer 写、consumer 读的那块共享内存（`smem_A`、`smem_B` + 流水线屏障）。

#### 4.2.3 源码精读

**① 主模板：14 参数 + 兜底报错**

`CollectiveMma` 主模板同样是「永远失败」：

[include/cutlass/gemm/collective/collective_mma_decl.hpp:L40-L59](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/collective/collective_mma_decl.hpp#L40-L59)

第 58 行 `dependent_false<ElementA>` 的报错信息是 `"Could not find a mainloop specialization."`——如果 `DispatchPolicy` 没有对应特化，编译期就会停在这里。这 14 个参数正是上面表格里的五组。

**② RS 主循环特化的签名与约束**

`sm90_mma_tma_gmma_rs_warpspecialized.hpp` 是 `DispatchPolicy = MainloopSm90TmaGmmaRmemAWarpSpecialized<...>` 对应的特化。类头说明了它的关键约束——A 来自寄存器、B 来自共享内存描述符：

[include/cutlass/gemm/collective/sm90_mma_tma_gmma_rs_warpspecialized.hpp:L58-L92](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/collective/sm90_mma_tma_gmma_rs_warpspecialized.hpp#L58-L92)

注意 L96 把 `DispatchPolicy` 取为 `MainloopSm90TmaGmmaRmemAWarpSpecialized<Stages, ClusterShape, KernelSchedule>`——这就是 builder 在 4.1 里塞进来的那个策略标签。第 175–177 行的 `static_assert`（在文件更下方）会强制「A 从 rmem、B 从 smem_desc」，确保这个文件只服务它该服务的情形。

**③ 异步流水线类型与级数**

主循环的同步骨架是一条 `PipelineTmaAsync`，级数就是 `DispatchPolicy::Stages`：

[include/cutlass/gemm/collective/sm90_mma_tma_gmma_rs_warpspecialized.hpp:L139-L145](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/collective/sm90_mma_tma_gmma_rs_warpspecialized.hpp#L139-L145)

`MainloopPipeline = PipelineTmaAsync<Stages>` 是多级缓冲的环形队列（stage 数由 builder 自动算出）；`PipelineState` 跟踪 producer 写到哪一级、consumer 读到哪一级。`NumProducerThreadEvents = 1` 提示 producer 只需 1 个线程发 TMA。

**④ SharedStorage：producer 写、consumer 读的共享内存**

主循环要用的共享内存由 `SharedStorage` 描述：

[include/cutlass/gemm/collective/sm90_mma_tma_gmma_rs_warpspecialized.hpp:L216-L227](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/collective/sm90_mma_tma_gmma_rs_warpspecialized.hpp#L216-L227)

它有两部分：`tensors.smem_A`/`tensors.smem_B`（带对齐的共享内存数组，布局含 swizzle，体积 = `cosize_v<SmemLayout>`）和 `pipeline`（流水线屏障存储）。producer 把 gmem 数据 TMA 进来，consumer 从这里读。这块内存由内核层（4.3 节）分配并传进来。

#### 4.2.4 代码实践

**实践目标：** 理解「主循环无状态、参数由外部传入」的设计。

**操作步骤：**

1. 在 `sm90_mma_tma_gmma_rs_warpspecialized.hpp` 里搜索 `struct Params` 与 `struct Arguments`。
2. 对比：`Arguments` 是 host 端用户填的（指针、步长），`Params` 是 device 端内核用的（含 TMA 描述符）。
3. 找到 `to_underlying_arguments`，它是把 `Arguments` 翻译成 `Params` 的函数（在 host 调一次，构造 TMA 描述符）。

**需要观察的现象：** 主循环类没有任何非 `static` 数据成员——它的「状态」全部由调用方（内核）通过 `Params` 和 `SharedStorage` 参数传入。

**预期结果：** 你能解释为什么主循环类可以「无状态」：所有可变信息（指针、步长、级数）都在 `Params` 里，每次内核启动重新构造。这是 warp-specialized 内核能被 tile scheduler 反复复用（persistent）的前提。**待本地验证**（无 GPU 时为阅读型实践）。

#### 4.2.5 小练习与答案

**练习 1：** `CollectiveMma` 的 14 个参数里，哪两个决定「A 用什么 TMA 拷贝、A 在 smem 里怎么排」？

**参考答案：** `GmemTiledCopyA`（全局→共享的 TMA 拷贝原子）和 `SmemLayoutAtomA`（共享内存布局原子）。`SmemCopyAtomA` 则负责 smem→rmem 的拷贝（RS 版本里 A 需要，SS 版本里是 `void`）。

**练习 2：** 为什么 `CollectiveMma` 主模板的报错信息是「找不到 mainloop 特化」而不是「参数错误」？

**参考答案：** 因为它就是按 `DispatchPolicy` 来选特化的；主模板被实例化意味着没有任何特化的 `DispatchPolicy` 匹配上了，所以提示「找不到匹配的 mainloop 特化」最贴切。

---

### 4.3 producer/consumer 模型与异步流水线

#### 4.3.1 概念说明

warp specialization（warp 专门化）是 Hopper GEMM 提升性能的核心手段：把一个 CTA 里的 warp group 分成两种角色，**并发**执行而不是串行——

- **producer（DMA warp）**：只负责搬数据，用 TMA 把 A、B tile 从全局内存搬到共享内存的某一级缓冲。
- **consumer（math warp group）**：只负责算，等数据到位后发 wgmma，累加进寄存器里的 accumulator。

两边通过**异步流水线**（`PipelineTmaAsync`）同步：producer 写满一级就通知，consumer 等到该级数据就绪再读。这样「搬下一块」和「算上一块」重叠起来，让 Tensor Core 几乎不停歇。这是和 2.x「同一组 warp 先搬后算」最大的区别。

#### 4.3.2 核心流程

整个 CTA 的控制流（在内核层 `sm90_gemm_tma_warpspecialized.hpp`）大致是：

```text
进入内核，按 warp_group_role 分流：
  ├─ Producer warp:
  │    prefetch TMA 描述符（单线程）
  │    for k in [0, k_tile_count):
  │        pipeline.producer_acquire(stage)        # 等到有空闲缓冲级
  │        barrier = pipeline.producer_get_barrier(stage)
  │        copy(TMA_A.with(barrier), gA_k, smem_A[.., stage])   # 发 TMA，完成时翻转 barrier
  │        copy(TMA_B.with(barrier), gB_k, smem_B[.., stage])
  │    pipeline.producer_tail(...)                 # 收尾，避免 cluster 里提前退出
  │
  └─ Consumer warp group:
       for k in [0, k_tile_count):
           pipeline.consumer_wait(stage)           # 等数据就绪
           copy A: smem -> rmem                    # RS 版本：A 要进寄存器
           cute::gemm(tiled_mma, rA, sB[..,stage], accum)   # 发 wgmma（B 直接用 smem 描述符）
           pipeline.consumer_release(stage)        # 告诉 producer：这级我用完了
```

关键点：producer 用 `producer_acquire`/`producer_get_barrier` 管理「写权」，consumer 用 `consumer_wait`/`consumer_release` 管理「读权」。`acquire` 与 `release` 配对，形成环形多级缓冲——级数越多，能重叠的「搬/算」越多，但占用共享内存也越多（这正是 builder 要按容量算级数的原因）。

#### 4.3.3 源码精读

**① 内核层：按角色分流**

内核 `sm90_gemm_tma_warpspecialized.hpp` 在入口算出每个 warp 的角色，并据此把流水线参数设为 Producer 或 Consumer：

[include/cutlass/gemm/kernel/sm90_gemm_tma_warpspecialized.hpp:L300-L326](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/sm90_gemm_tma_warpspecialized.hpp#L300-L326)

第 302–303 行用 `canonical_warp_group_idx()` 给每个 warp group 打上 `WarpGroupRole`（Producer/Consumer）。第 317–322 行据此设置流水线的 `role`，第 326 行真正构造出 `MainloopPipeline` 对象（绑定共享内存里的屏障存储）。

**② 内核层：分别调用 `load` 和 `mma`**

接着是分流的真正执行：

[include/cutlass/gemm/kernel/sm90_gemm_tma_warpspecialized.hpp:L430-L479](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/sm90_gemm_tma_warpspecialized.hpp#L430-L479)

- L430–449：Producer 分支调用 `collective_mainloop.load(...)`（搬数据）和 `load_tail(...)`（收尾），随后还可能发 epilogue 的 load。
- L468–479：Consumer 分支调用 `collective_mainloop.mma(...)`（算乘加），再 `mma_tail(...)`，最后跑 epilogue 写回。

注意 L435/L471 这两个调用用的是**同一个** `collective_mainloop` 对象和**同一个** `mainloop_pipeline`，但被两组不同的 warp 并发执行——这就是 warp specialization。

**③ producer 的 `load`：单线程发 TMA**

回到集体主循环文件，`load` 函数只在「被选中的那一个线程」（`elect_one_sync`）上发 TMA，循环 `k_tile_count` 次：

[include/cutlass/gemm/collective/sm90_mma_tma_gmma_rs_warpspecialized.hpp:L457-L468](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/collective/sm90_mma_tma_gmma_rs_warpspecialized.hpp#L457-L468)

逐行：

- L457 `producer_acquire`：等到有空闲缓冲级可写（如果所有级都被 consumer 占着，就阻塞）。
- L464 `producer_get_barrier`：拿到这一级对应的 mbarrier（TMA 完成时翻转它，consumer 据此得知数据就绪）。
- L467–468 两条 `copy`：用 `tma_load.with(*tma_barrier, mcast_mask)` 发出 A、B 的 TMA 拷贝。`with(...)` 把 barrier 绑到这次拷贝——TMA 硬件完成搬运后会自动翻转 barrier，无需 producer 再发信号。
- L472 `++smem_pipe_write`：推进写指针，下一轮写下一级。

**④ consumer 的 `mma`：wait → copy → gemm**

consumer 在 `mma` 函数里循环，下面是第一个 k tile 的处理（展开后的核心几步）：

[include/cutlass/gemm/collective/sm90_mma_tma_gmma_rs_warpspecialized.hpp:L598-L633](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/collective/sm90_mma_tma_gmma_rs_warpspecialized.hpp#L598-L633)

逐行解读（wgmma 是异步的，所以这套调用看着有点绕）：

- L601–602 `consumer_try_wait`/`consumer_wait`：等 producer 把这一级数据搬完（mbarrier 翻转）。
- L610 `copy(smem_tiled_copy_A, tCsA, tCrA)`：把 A 从 smem 拷进寄存器（RS 版本的特色——wgmma 的 A 操作数在寄存器里）。
- L622 `cute::gemm(tiled_mma, tCrA, tCrB, accum)`：发出 wgmma。`tCrA` 是 rmem 的 A，`tCrB` 是 smem 描述符的 B。这一行就是真正的矩阵乘加。
- L620/L626 `warpgroup_arrive`/`warpgroup_commit_batch`：异步 wgmma 的提交原语——`arrive` 报告操作数就绪，`commit_batch` 提交这批指令。
- L629 `warpgroup_wait<2>()`：等前面若干条 wgmma 完成（保留 2 条在飞，以重叠搬数与计算）。

这套「`arrive` → `gemm` → `commit` → 延迟 `wait`」正是 wgmma 异步执行的固定节奏，目的是让「搬下一块的 A」和「算当前块」重叠。

#### 4.3.4 代码实践

**实践目标：** 在源码里标出 producer 与 consumer 的同步点。

**操作步骤：**

1. 打开 `sm90_mma_tma_gmma_rs_warpspecialized.hpp`，在 `load` 函数里数 `producer_acquire`/`producer_get_barrier`/`producer_tail` 出现的位置。
2. 在 `mma` 函数里数 `consumer_try_wait`/`consumer_wait`/`consumer_release` 出现的位置。
3. 打开内核 `sm90_gemm_tma_warpspecialized.hpp` 第 430–479 行，确认 producer 调用 `load`、consumer 调用 `mma`。

**需要观察的现象：** producer 用 `acquire` 拿写权、用 barrier 通知 consumer；consumer 用 `wait` 等通知、用 `release` 归还写权。`acquire` 和 `release` 在时间上错开，构成多级缓冲的「环」。

**预期结果：** 你能画出一张时序图：第 `i` 级 producer 在 `acquire(i)` 后写、consumer 在 `wait(i)` 后读、读完 `release(i)`，于是 producer 可以再次 `acquire(i)` 写入新数据。级数越多，pipeline 越深、能重叠的搬/算越多。

> 不需要 GPU 也能完成本实践，纯阅读型。如要在 nsight compute 上观察，可编译 example 49 后用 `ncu --set full` 看 warp specialization 的时序。**待本地验证。**

#### 4.3.5 小练习与答案

**练习 1：** 为什么 producer 只在「一个线程」上发 TMA，而不是全 warp 都发？

**参考答案：** TMA 是单线程发起的硬件单元指令（一条指令搬整块），多个线程重复发只会浪费且可能冲突。所以用 `elect_one_sync()` 选出一个线程（lane 0）来发，其余线程空闲。

**练习 2：** consumer 里 `warpgroup_wait<2>()` 的 `2` 是什么意思？

**参考答案：** wgmma 是异步指令，提交后不会立刻完成。`warpgroup_wait<2>()` 表示「允许至多 2 条 wgmma 在飞」，只等待更早的指令完成。保留少量在飞指令是为了重叠数据搬运与计算，提高 Tensor Core 利用率。

---

### 4.4 A/B 分片与拷贝（TMA + cute::gemm）

#### 4.4.1 概念说明

前两节讲了「谁搬、谁算、怎么同步」。本节聚焦**数据本身**：A 和 B 是怎样被切成小块、分发到每个线程，再喂给 wgmma 的？这正好把前面 u2-l2（Tensor）、u2-l3（copy/gemm）、u2-l4（partition）串起来。

核心思路：用 CuTe 的 `partition` 把「整块张量」切成「每个线程/每次 MMA 持有的 fragment」，再用 `cute::copy`（搬）和 `cute::gemm`（算）作用于这些 fragment。对 A（RS 版本）和 B（共享内存描述符）处理路径不同：

- **A**：gmem →（TMA）→ smem →（`SmemCopyAtomA`）→ rmem 寄存器 fragment `tCrA`，然后参与 wgmma。
- **B**：gmem →（TMA）→ smem，wgmma 通过 smem 描述符**直接**读 smem 的 B，不经过寄存器。

这种「一边进寄存器、一边留共享内存」正是 `gmma_rs`（rmem-A、smem-B）名字的由来，也是它和 SS 版本（A、B 都留 smem）的区别。

#### 4.4.2 核心流程

A、B 各自的分片与搬运流程：

```text
共享准备 (load_init):
  mA_mkl = tma_load_a.get_tma_tensor(M,K,L)          # 全局 TMA 张量 A
  gA_mkl = local_tile(mA_mkl, TileShape, ...)        # 切成 (BLK_M,BLK_K, m,k,l)

producer 的 load():
  tAgA = block_tma_a.partition_S(gA)   # 全局侧分片
  tAsA = block_tma_a.partition_D(sA)   # 共享内存侧分片（目标）
  copy(TMA_A.with(barrier), tAgA[..,k], tAsA[..,stage])   # TMA 搬一块 A
  copy(TMA_B.with(barrier), tBgB[..,k], tBsB[..,stage])   # TMA 搬一块 B

consumer 的 mma():
  tCrA = partition_A(tiled_mma, sA)    # 每线程持有的 A 寄存器 fragment
  tCrB = partition_B(tiled_mma, sB)    # 每线程持有的 B 共享内存视图（描述符）
  copy(SmemCopyAtomA, tCsA[..,stage], tCrA[..])  # A: smem -> rmem
  cute::gemm(tiled_mma, tCrA, tCrB[..,stage], accum)   # wgmma: D = A*B + C
```

`partition_S`/`partition_D` 是 CuTe 拷贝原子（这里是 TMA）的「源/目分片」，把一个逻辑块切成 TMA 能一次搬走的形状；`partition_A`/`partition_B` 是 `TiledMma` 的分片，把张量切成每线程持有的 fragment。两套 partition 对齐，保证搬进来的数据正好能喂给 MMA。

#### 4.4.3 源码精读

**① producer 侧分片（partition_S / partition_D）**

在 `load` 函数里，发 TMA 之前先把全局张量和共享内存张量都按 TMA 拷贝原子分片：

[include/cutlass/gemm/collective/sm90_mma_tma_gmma_rs_warpspecialized.hpp:L419-L432](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/collective/sm90_mma_tma_gmma_rs_warpspecialized.hpp#L419-L432)

- L419–420 `get_slice(...)`：按本 CTA 在 cluster 里的坐标取出本块负责的拷贝切片器。
- L428–429 `partition_S(gA)`/`partition_D(sA)`：分别对源（全局）和目（共享内存）分片，得到形状 `(TMA, TMA_M, TMA_K, k)` 和 `(TMA, TMA_M, TMA_K, PIPE)`。后续 `copy` 就是把源的第 k 块搬到目的的第 stage 级。

**② consumer 侧：A 进寄存器、B 留共享内存**

在 `mma` 里，先把 A 拷进寄存器再算：

[include/cutlass/gemm/collective/sm90_mma_tma_gmma_rs_warpspecialized.hpp:L609-L622](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/collective/sm90_mma_tma_gmma_rs_warpspecialized.hpp#L609-L622)

- L610 `copy(smem_tiled_copy_A, tCsA_copy_view, tCrA_copy_view)`：A 从 smem 搬到 rmem（这正是 u2-l3 提到的「smem→rmem 先 copy 再 gemm」变体）。注意 RS 版本里只有 A 需要 smem→rmem 拷贝；B 不需要，因为它用描述符直接读 smem。
- L622 `cute::gemm(tiled_mma, tCrA, tCrB, accum)`：wgmma 真正执行。三个张量分别落在 rmem / smem / rmem，`cute::gemm` 按它们的内存空间（4.x 里的空间标签）自动编译成 wgmma 指令。

**③ 布局优化：SwapAB / TransposeB**

为了贴合 wgmma「K-major」的偏好，builder 和主循环会按 A、B 的布局做交换或转置。比如 RS 主循环的 `IsLayoutAkBmn`/`SwapAB` 判断（文件 L114–123）：当 A 是 K-major、B 是 MN-major 且非两字节类型时，内部把 A、B 交换，让 wgmma 走更高效的路径。这类优化对用户透明——用户只管传 layout tag，正确性由 builder/主循环保证。

#### 4.4.4 代码实践

**实践目标：** 用 `CollectiveBuilder` 配置一个 Hopper FP16 GEMM 的集体主循环，推断出它选了哪个集体文件。

**操作步骤：**

1. 参照 example 49（`examples/49_hopper_gemm_with_collective_builder/49_collective_builder.cu`）第 318–328 行的写法，构造如下类型（**示例代码**，可直接放入一个小 `.cu` 里编译，或在脑中推断）：

```cpp
// 示例代码：用 CollectiveBuilder 配置 Hopper FP16 集体主循环
using TileShape    = Shape<_128, _128, _64>;
using ClusterShape = Shape<_1, _1, _1>;

using CollectiveMainloop = typename cutlass::gemm::collective::CollectiveBuilder<
    cutlass::arch::Sm90, cutlass::arch::OpClassTensorOp,
    cutlass::half_t, cutlass::layout::RowMajor, 128/16,   // A: FP16, 行主序, 对齐 8 元素
    cutlass::half_t, cutlass::layout::RowMajor, 128/16,   // B: FP16, 行主序, 对齐 8 元素
    float,                  // 累加器: FP32
    TileShape, ClusterShape,
    cutlass::gemm::collective::StageCountAuto,
    cutlass::gemm::collective::KernelScheduleAuto
>::CollectiveOp;
```

2. 推断它命中哪个 builder 特化：A、B 都是 `half_t`（两字节、非混合精度），由 4.1 节的判据，`is_use_rmem_A` 为假 → 落入 **SS 特化**（`GMMA_TMA_WS_SS`）。
3. 因此 `CollectiveMainloop` 的底层类型是 `CollectiveMma<MainloopSm90TmaGmmaWarpSpecialized<...>, ...>`，对应文件 `sm90_mma_tma_gmma_ss_warpspecialized.hpp`（而不是本讲精读的 RS 版本）。

**需要观察的现象：** 把 A 改成 FP8（如 `cutlass::float_e4m3_t`）后，由于 FP8 的最佳路径让 A 落寄存器，`is_use_rmem_A` 变真，会改落入 **RS 特化**（`GMMA_TMA_WS_RS`），对应文件 `sm90_mma_tma_gmma_rs_warpspecialized.hpp`。

**预期结果：** 你能写出一行总结：「`CollectiveBuilder` 的输出类型由 (架构, 数据类型, 布局, 调度标签) 四元组决定；FP16/两字节输入走 SS，FP8/混合精度走 RS」。**待本地验证**（需 Hopper GPU 编译；无 GPU 时按上述判据推断即可）。

#### 4.4.5 小练习与答案

**练习 1：** RS 版本里，为什么 A 要 `copy` 进寄存器，而 B 不用？

**参考答案：** 因为 wgmma 的 `rs`（rmem-smem）变体规定 A 操作数来自寄存器、B 操作数来自共享内存描述符。所以 A 必须先从 smem 搬到 rmem，B 则由 wgmma 通过描述符直接读 smem，省去一次搬运。

**练习 2：** `cute::gemm(tiled_mma, tCrA, tCrB, accum)` 是怎么知道要发 wgmma 而不是普通 FMA 的？

**参考答案：** `cute::gemm` 根据传入张量的**内存空间标签**（u2-l2 讲过 gmem/smem/rmem 标签）和 `tiled_mma` 内封装的 MMA_Atom，在编译期分派到对应的指令。这里 `tCrA` 是 rmem、`tCrB` 是 smem 描述符，`tiled_mma` 封装了 wgmma atom，于是编译成 `wgmma.mma_async` 指令——这正是 u2-l3 强调的「算法与张量解耦」。

---

## 5. 综合实践

**任务：跟踪一个 Hopper FP16 GEMM 从 `CollectiveBuilder` 到 wgmma 的完整链路。**

1. **配置阶段（host）**：仿照 example 49 第 318–337 行，用 `CollectiveBuilder` 构造 `CollectiveMainloop`（FP16 输入、FP32 累加、`Shape<_128,_128,_64>` tile、`Shape<_1,_1,_1>` cluster、`StageCountAuto`、`KernelScheduleAuto`），再用 `cutlass::gemm::kernel::GemmUniversal<ProblemShape, CollectiveMainloop, CollectiveEpilogue, TileScheduler>` 和 `device::GemmUniversalAdapter` 包成可启动的 `Gemm`。

2. **推断阶段（编译期）**：回答三个问题——
   - 命中的是 SS 还是 RS 特化？依据是什么？
   - `CollectiveMainloop::DispatchPolicy` 的具体类型是什么？
   - builder 推断出的 `PipelineStages` 大致是多少？（提示：用 `(228KB − carveout) / 单级字节` 估算，单级字节 = A 块 + B 块）

3. **运行阶段（device）**：在源码里标注一次 k-tile 迭代中——
   - producer 的哪一行发 TMA、用哪个 barrier 通知；
   - consumer 的哪一行把 A 搬进寄存器、哪一行发 wgmma；
   - 哪一行 `consumer_release` 归还缓冲。

4. **验证（可选，需 Hopper GPU）**：编译运行 example 49，确认输出 `Passed`；用 `ncu` 观察 warp specialization 的 producer/consumer 时序，看「搬」与「算」是否重叠。

完成本任务后，你就把「`CollectiveBuilder` 自动组装 → `CollectiveMma` 主循环 → producer/consumer 流水线 → `cute::copy`/`cute::gemm` 发指令」这一整条 3.x GEMM 链路在源码层贯通了。

## 6. 本讲小结

- `CollectiveBuilder` 是编译期「装配车间」：吃 13 个高层参数（架构/类型/布局/tile/cluster/级数策略/调度策略），用 `enable_if` 按 `KernelSchedule` 标签 + 数据条件分派到具体特化，推断出 `TiledMma`/TMA 拷贝/共享内存布局/级数，产出 `CollectiveOp = CollectiveMma<14 个参数>`。
- `CollectiveMma`（主循环）主模板有 14 个参数，分策略/数据/计算单元/A 搬运/B 搬运五组；主模板永远 `static_assert` 失败，真正实现按 `DispatchPolicy` 特化。
- Hopper 的 SM90 主循环用 **warp specialization**：内核按 `warp_group_role` 把 producer（DMA warp，发 TMA）和 consumer（math warp group，发 wgmma）并发分流，靠 `PipelineTmaAsync` 多级缓冲同步（`producer_acquire/get_barrier` ↔ `consumer_wait/release`）。
- RS 版本里 A 从共享内存搬进寄存器、B 由 wgmma 通过共享内存描述符直接读；`copy(smem→rmem)` 后 `cute::gemm` 发出 wgmma，配合 `warpgroup_arrive/commit/wait` 管理异步指令。
- FP16/两字节输入一般走 SS（A、B 都留共享内存），FP8/混合精度走 RS（A 进寄存器）；用户只传 layout tag 与类型，正确性与性能优化由 builder 和主循环保证。

## 7. 下一步学习建议

本讲把 3.x GEMM 的「配置 + 主循环」讲透了。建议接下来：

1. **u2-l9（Hopper Warp-Specialized GEMM 实战）**：把本讲的 `CollectiveBuilder` + `GemmUniversal` 组合成一个能编译运行的完整 example 49，亲手跑通。
2. **u3-l1（Async Pipeline 与 Warp Specialization）**：深入 `PipelineTmaAsync` 的 stage/phase 与 mbarrier 原语，理解 `producer_acquire`/`consumer_wait` 背后的硬件同步机制。
3. **u3-l2（TMA 异步张量拷贝）**：专攻 TMA 描述符的构造与 `copy_traits_sm90_tma`，搞懂 `tma_load.with(barrier)` 到底编码了什么。
4. **u2-l10（Epilogue 与 EVT）**：补上「写回 D」那一半，看 `CollectiveEpilogue` 如何和本讲的主循环衔接（同一个内核里 producer 还会发 epilogue load）。

继续阅读建议：先精读 `examples/49_hopper_gemm_with_collective_builder/49_collective_builder.cu` 全文，再对照本讲回看 `sm90_mma_tma_gmma_rs_warpspecialized.hpp` 的 `load`/`mma` 两个函数，形成「host 配置 ↔ device 主循环」的完整心智模型。

# CUTLASS 2.x GEMM 分层结构

## 1. 本讲目标

本讲承接 [u1-l6 第一个 GEMM：2.x device API](u1-l6-first-gemm.md)（你已经会用 `cutlass::gemm::device::Gemm` 启动一个内核）和 [u2-l5 CUTLASS arch：指令级 MMA](u2-l5-arch-mma.md)（你已经知道一条 Tensor Core 指令如何被包装成 C++ 对象）。

我们这一讲要回答一个核心问题：

> 当你写下 `cutlass::gemm::device::Gemm<...>` 这一行模板时，CUTLASS 内部到底「展开」成了多少层类？每一层各负责什么？

读完本讲，你应当能够：

- 说清 CUTLASS 2.x 的 **device → kernel → threadblock → warp → thread** 五段式分层，以及它们各自的源码目录。
- 理解 `DefaultMma` 如何把 `MmaCore`、gmem 迭代器、smem 迭代器「拼装」成一个 `ThreadblockMma`。
- 读懂 `DefaultMmaCore`（按架构特化的配置文件）如何把三层 tile 形状翻译成 `WarpCount`、共享内存布局、warp 级 MMA 和 `MmaPolicy`。
- 说清楚 CUTLASS 2.x 与 3.x 在「分层方式」上的本质区别，为后续 [u2-l7 CUTLASS 3.x GEMM 通用模型](u2-l7-gemm-3x-universal-model.md) 做铺垫。

## 2. 前置知识

- **tile（瓦片）**：把一个超大矩阵切成固定大小的小块。CUTLASS 用 `GemmShape<M, N, K>` 描述一个 tile 的形状。
- **CTA / threadblock（线程块）**：GPU 上一个被调度到 SM 上执行的线程组，共享一块 shared memory。
- **warp（线程束）**：32 个线程为一组，是 SIMT 执行的基本单位。
- **Tensor Core 指令（mma 指令）**：GPU 硬件级别的小矩阵乘加指令，一条指令算一个 `InstructionShape` 大小的乘加（如 SM80 的 `m16n8k16`）。
- **fragment（片段）**：每个线程/每个 warp 私自持有的、存放在寄存器里的一小片矩阵数据。
- **主循环（mainloop）**：GEMM 中反复「从显存搬一块到共享内存 → 算乘加」的循环。

一句话直觉：CUTLASS 2.x 的设计哲学是 **「分而治之 + 每层一个类」**。一个 \(M \times N \times K\) 的大矩阵乘，被逐层切成更小的子问题，每一层都有一个专门的 C++ 类来管这一层的切分与执行。

\[ \text{device 层} \rightarrow \text{kernel 层} \rightarrow \text{threadblock 层} \rightarrow \text{warp 层} \rightarrow \text{thread/指令 层} \]

每一层只关心「怎么把上一层交下来的 tile 再切小一层」，并把真正的硬件指令交给最底层。

## 3. 本讲源码地图

| 文件 | 所属层 | 作用 |
| --- | --- | --- |
| `include/cutlass/gemm/device/gemm.h` | device | 最高层入口 `device::Gemm`：把模板参数映射到内核、把参数翻译成 `Params`、启动内核 |
| `include/cutlass/gemm/kernel/gemm.h` | kernel | 设备端内核 `kernel::Gemm`：算出当前 CTA 负责哪个 tile，构造 threadblock MMA 和 epilogue 并执行 |
| `include/cutlass/gemm/threadblock/default_mma.h` | threadblock（装配） | `DefaultMma`：按「算子类 + 阶段数」把 `MmaCore` + 迭代器拼成 `ThreadblockMma` |
| `include/cutlass/gemm/threadblock/default_mma_core.h` | threadblock（配置） | `DefaultMmaCore` 主模板声明；真正实现按架构拆到 `default_mma_core_sm70/sm75/sm80/simt.h` |
| `include/cutlass/gemm/threadblock/default_mma_core_sm80.h` | threadblock（配置·实例） | SM80 TensorOp 的 `DefaultMmaCore` 特化：定义 `WarpCount`/`SmemLayout`/warp MMA/`MmaPolicy` |
| `include/cutlass/gemm/threadblock/mma_pipelined.h` | threadblock（执行） | `MmaPipelined`：双缓冲主循环，反复调用 `warp_mma` |
| `include/cutlass/gemm/warp/mma.h` / `warp/mma_simt.h` | warp | warp 级 MMA；SIMT 路径内部用 `thread::Mma` 做每线程标量乘加 |
| `include/cutlass/gemm/thread/mma.h` | thread | 每线程 CUDA Core 上的标量乘加（SIMT 路径的最底层） |

记住这张表的关键结论：**目录名 ≈ 命名空间 ≈ 分层**（`device/`、`kernel/`、`threadblock/`、`warp/`、`thread/`），这正是 2.x 分层结构的文件镜像（见 u1-l3）。

## 4. 核心概念与源码讲解

### 4.1 device/kernel/threadblock/warp/thread 四层分解

#### 4.1.1 概念说明

CUTLASS 2.x 把一次 GEMM 分成由上到下的若干层。每一层都是一个 C++ 类，负责「上一层 tile → 本层 tile」的再切分：

| 层 | 类（典型） | 负责切分的对象 | 运行位置 |
| --- | --- | --- | --- |
| **device** | `device::Gemm` | 整个问题 \(M \times N\) → CTA 网格 | host（启动内核） |
| **kernel** | `kernel::Gemm` | 「当前 CTA 负责哪个 tile」 | device（内核入口 `operator()`） |
| **threadblock** | `MmaPipelined` / `MmaMultistage` | CTA tile → 各 warp + 主循环 | device（CTA 内） |
| **warp** | `warp::MmaTensorOp` / `warp::MmaSimt` | warp tile → 每个线程的 fragment | device（warp 内） |
| **thread/指令** | `thread::Mma` / arch `mma` 指令 | 标量乘加或一条硬件 MMA 指令 | device（单线程/Tensor Core） |

为什么要分这么多层？因为 GPU 的并行结构本身就是分层的（设备 → SM/CTA → warp → 线程 → 指令）。CUTLASS 让软件分层与硬件分层一一对应，每一层都能独立调优（换 tile 形状、换缓冲策略、换指令），而不牵动其它层。

#### 4.1.2 核心流程

一次 `device::Gemm(args)` 调用的完整下行链路：

```
host:  device::Gemm::operator()(args)
         → initialize(args)        // 把 Arguments 翻译成内核 Params，算 grid
         → run()                   // <<<grid, block, smem>>>(params_)
              │
device:  cutlass::Kernel<kernel::Gemm><<<...>>>(params)        // kernel 层
           → kernel::Gemm::operator()(params, shared_storage)
               • 算出本 CTA 的 tile_offset
               • 构造 IteratorA/IteratorB（gmem→smem）
               • 构造 Mma mma(...)                                 // threadblock 层
               • mma(gemm_k_iterations, accum, iterA, iterB, accum)
                   └─ MmaPipelined::gemm_iters()                  // 主循环
                        └─ warp_mma(accum, fragA, fragB, accum)    // warp 层
                             └─ arch mma 指令 / thread::Mma         // 指令层
               • 构造 Epilogue，写回 D
```

重点：**控制流自上而下，数据搬运和计算在 threadblock 主循环里交织**。device 层和 kernel 层主要是「组织」；真正算乘加的是最底层的 warp→指令。

#### 4.1.3 源码精读

**(1) device 层：模板参数 → 内核类型**

`device::Gemm` 把一大堆模板参数喂给 `kernel::DefaultGemm`，得到真正的内核类型 `GemmKernel`。注意它本身不写任何设备端计算逻辑，只做「编译期映射 + 运行期参数翻译 + 启动」：

[include/cutlass/gemm/device/gemm.h:264-289](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/device/gemm.h#L264-L289) — 由 `kernel::DefaultGemm<...>::GemmKernel` 定义内核类型，把数据类型/布局/三层 tile/算子类/架构 tag 全部传入。

device 层的运行期职责在 `run()` 里，用 `ThreadblockSwizzle` 算出 grid，再 `<<<grid, block, smem_size>>>` 启动：

[include/cutlass/gemm/device/gemm.h:473-500](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/device/gemm.h#L473-L500) — `run()` 计算 `dim3 grid`、`block = GemmKernel::kThreadCount`、`smem_size = sizeof(SharedStorage)`，并通过 `cutlass::Kernel<GemmKernel><<<...>>>` 启动内核。

> 顺带回顾 u1-l6 提到的「输出 ColumnMajor 实际命中转置偏特化」：当 `LayoutC = ColumnMajor` 时，主模板不生效，而是走 [include/cutlass/gemm/device/gemm.h:572-633](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/device/gemm.h#L572-L633) 的偏特化——它把问题转置成 RowMajor 的 `UnderlyingOperator`（交换 A/B、转置布局），复用同一套分层。

**(2) kernel 层：CTA 入口**

`kernel::Gemm` 是真正跑在 GPU 上的内核对象。它持有 `Mma`（threadblock MMA）和 `Epilogue`，并定义 `SharedStorage`（共享内存联合体）和 `kThreadCount`：

[include/cutlass/gemm/kernel/gemm.h:61-69](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/gemm.h#L61-L69) — `using Mma = Mma_;`、`kThreadCount = 32 * WarpCount::kCount`。注意 `WarpCount` 来自下层 `Mma`，说明「需要多少线程」是由 threadblock 层决定的，再向上汇报给 kernel 层去配置 `block` 大小。

[include/cutlass/gemm/kernel/gemm.h:139-142](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/gemm.h#L139-L142) — `union SharedStorage { Mma::SharedStorage main_loop; Epilogue::SharedStorage epilogue; };`。共享内存被主循环和 epilogue 复用（同一块内存在不同阶段扮演不同角色），这是 2.x 控制显存占用的常见手法。

kernel 的 `operator()` 算出本 CTA 的 tile 偏移，构造迭代器与 `Mma`，然后调用主循环：

[include/cutlass/gemm/kernel/gemm.h:266-276](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/gemm.h#L266-L276) — 构造 `Mma mma(shared_storage.main_loop, thread_idx, warp_idx, lane_idx)`，清零累加器，再 `mma(gemm_k_iterations, accumulators, iterator_A, iterator_B, accumulators)` 把工作交给 threadblock 层。

**(3) threadblock 层：主循环**

`MmaPipelined` 持有一个 warp 级算子 `warp_mma`，并在主循环里反复调用它：

[include/cutlass/gemm/threadblock/mma_pipelined.h:145-146](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/threadblock/mma_pipelined.h#L145-L146) — `Operator warp_mma;`，其中 `Operator = Policy::Operator` 就是 warp 级 MMA。这是 threadblock → warp 的「接缝」。

[include/cutlass/gemm/threadblock/mma_pipelined.h:372-376](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/threadblock/mma_pipelined.h#L372-L376) — 在主循环内对每个 `warp_mma_k` 调用 `warp_mma(accum, warp_frag_A[...], warp_frag_B[...], accum)`，完成一次 warp 级乘加。

**(4) warp → thread 层（SIMT 路径）**

在 SIMT（CUDA Core）路径下，warp 级 `MmaSimt` 内部进一步把工作切给每个线程，用 `thread::Mma` 做标量乘加：

[include/cutlass/gemm/warp/mma_simt.h:145-158](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/warp/mma_simt.h#L145-L158) — `using ThreadMma = thread::Mma< GemmShape<Shape::kM/WarpShape::kRow, Shape::kN/WarpShape::kColumn, LaneMmaShape::kK>, ... >;`，把 warp tile 按 `Policy::WarpShape` 再切成每线程的小 tile。

[include/cutlass/gemm/thread/mma.h:52-72](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/thread/mma.h#L52-L72) — `thread::Mma` 的主模板声明，最底层的每线程乘加（由 `mma_sm50/sm60/sm61.h` 提供架构特化）。对 TensorOp 路径，最底层则是 arch 层的 `mma` 指令（见 u2-l5）。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是用编辑器/Grep 把上面这条 typedef 链亲手走一遍。

1. **实践目标**：在源码里验证 `device::Gemm` 到最底层算子的逐层 typedef 关系。
2. **操作步骤**：
   - 打开 `include/cutlass/gemm/device/gemm.h`，定位主模板里的 `using GemmKernel = typename kernel::DefaultGemm<...>::GemmKernel;`（约 264 行）。
   - 用 Grep 搜索 `kernel::DefaultGemm` 的定义文件（提示：`include/cutlass/gemm/kernel/default_gemm.h`），找到它如何定义 `GemmKernel`（通常是 `kernel::Gemm<ThreadblockMma, Epilogue, ...>`）。
   - 看 `DefaultGemm` 如何用 `threadblock::DefaultMma<...>::ThreadblockMma` 得到 threadblock MMA。
3. **需要观察的现象**：你会看到一条「俄罗斯套娃」式的 typedef 链：device 把参数交给 kernel 装配，kernel 的 `Mma_` 参数指向 threadblock 装配，threadblock 的 `Policy` 指向 warp 算子。
4. **预期结果**：在笔记里写下至少 4 级 `using ... = ...` 的对应关系。
5. 运行时结果：本实践为静态阅读，**待本地验证**的是「这条链是否能被你的目标架构编译通过」。

#### 4.1.5 小练习与答案

**练习 1**：`kernel::Gemm` 需要启动多少个线程（`block` 维度）？这个数字由谁决定？
**答案**：`block = GemmKernel::kThreadCount = 32 * WarpCount::kCount`（见 [kernel/gemm.h:69](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/gemm.h#L69)）。`WarpCount` 由 threadblock 层的 `MmaCore` 根据三层 tile 形状算出（见 4.3）。所以「线程数」最终由你给的三层 tile 决定。

**练习 2**：为什么 `kernel::Gemm::SharedStorage` 用 `union` 同时装 `main_loop` 和 `epilogue`？
**答案**：主循环阶段（搬数据+算乘加）和 epilogue 阶段（写回 D）在时间上不重叠，共用一块共享内存能显著降低每个 CTA 的 smem 占用，从而提高 occupancy。两者互斥使用，因此用 `union`。

---

### 4.2 threadblock MMA 的组装：DefaultMma

#### 4.2.1 概念说明

`DefaultMma` 是 threadblock 层的「装配车间」。它的输入是高层关心的东西（数据类型、布局、三层 tile、阶段数、算子类），输出是一个可直接使用的 `ThreadblockMma` 类（`MmaPipelined` 或 `MmaMultistage`）。

它的核心工作只有三件：

1. 调用 `DefaultMmaCore`（见 4.3）拿到这一层的「配置包」`MmaCore`（含 warp MMA、共享内存迭代器、线程映射等）。
2. 用 `MmaCore` 里的线程映射，构造 A/B 两个 **gmem→smem 迭代器**（`PredicatedTileIterator` / `PredicatedTileAccessIterator`）。
3. 把 `MmaCore` + 两个迭代器打包成具体的 `ThreadblockMma`。

#### 4.2.2 核心流程

```
DefaultMma<元素A, 布局A, 元素B, 布局B, 累加器, 算子类, 架构, 三层tile, 阶段数, ...>
   │
   ├── 1. MmaCore = DefaultMmaCore<三层tile, 元素, 布局, 算子类, 阶段数, ...>   // 配置包
   ├── 2. IteratorA = PredicatedTileIterator<   // 用 MmaCore::IteratorThreadMapA
   │             MatrixShape<kM, kK>, ElementA, LayoutA, ...>
   ├── 2. IteratorB = PredicatedTileIterator<   // 用 MmaCore::IteratorThreadMapB
   │             MatrixShape<kK, kN>, ElementB, LayoutB, ...>
   └── 3. ThreadblockMma = MmaPipelined< MmaCore::Shape, IteratorA, MmaCore::SmemIteratorA,
                                         IteratorB, MmaCore::SmemIteratorB, 累加器, 布局C,
                                         MmaCore::MmaPolicy >
```

`DefaultMma` 是一个**主模板 + 多个偏特化**的结构体。CUTLASS 根据「算子类（Simt/TensorOp）」和「阶段数（2 还是 ≥3）」选择不同的偏特化，进而选择不同的 `ThreadblockMma`：

- `OpClassSimt` 或 `Stages==2` → `MmaPipelined`（双缓冲，用 `__syncthreads()` 同步）。
- `OpClassTensorOp` 且 `Stages>=3` → `MmaMultistage`（多级缓冲，SM80 起用 `cp.async` 异步拷贝）。

#### 4.2.3 源码精读

**(1) 主模板声明：所有可调参数**

[include/cutlass/gemm/threadblock/default_mma.h:65-110](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/threadblock/default_mma.h#L65-L110) — 主模板 `struct DefaultMma;` 的参数列表，包括元素/布局/对齐/三层 tile/阶段数/算子/共享内存清空策略/gather 等。注意 `Stages` 默认会被偏特化捕获。

**(2) OpClassSimt + Stages==2 → MmaPipelined**

[include/cutlass/gemm/threadblock/default_mma.h:151-186](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/threadblock/default_mma.h#L151-L186) — 这是 u1-l6 那个「只写 6 个模板参数」的示例实际落入的分支：取 `MmaCore`（行 162-165），构造 A/B 的 `PredicatedTileIterator`（行 168-179），最后组装成 `MmaPipelined`（行 182-185）。注意 `IteratorThreadMapA/B` 来自 `MmaCore`——这是「配置包」被消费的地方。

**(3) OpClassTensorOp + Stages>=3 → MmaMultistage**

[include/cutlass/gemm/threadblock/default_mma.h:514-562](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/threadblock/default_mma.h#L514-L562) — TensorOp 多级缓冲分支。与上一分支结构完全平行，区别有二：迭代器换成 `PredicatedTileAccessIterator`；`ThreadblockMma` 换成 `MmaMultistage`，并多传了 `MmaCore::kCacheOpA/B` 和 `Stages`。这个分支还会根据对齐宽度选择全局缓存策略（行 524-532）。

> 关键观察：`DefaultMma` 的所有偏特化都遵循同一个三步范式（取 `MmaCore` → 造迭代器 → 组装 `ThreadblockMma`），只是「装出来的具体类」不同。这种「骨架不变、零件可换」正是 CUTLASS 模板工程的典型风格。

#### 4.2.4 代码实践

1. **实践目标**：理解 `DefaultMma` 如何根据 `Stages` 选择不同的 threadblock MMA。
2. **操作步骤**：
   - 在 `default_mma.h` 中找到两个分别产出 `MmaPipelined` 和 `MmaMultistage` 的偏特化（上面给出的行号）。
   - 对比它们的模板参数列表里 `Stages` 的值（一个是 `2`，一个是自由参数 `Stages`）。
   - 思考：u1-l6 的 `00_basic_gemm` 示例默认走哪条路径？（提示：6 个模板参数 → `OpClassSimt` + `Sm70` → 默认 `Stages`）
3. **需要观察的现象**：两个偏特化的 `ThreadblockMma` 行长得几乎一样，但类型名和最后几个参数不同。
4. **预期结果**：能指出 `MmaMultistage` 比 `MmaPipelined` 多了 `kCacheOpA/B` 和 `Stages` 两个参数。
5. 本实践为阅读型，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `DefaultMma` 要分这么多偏特化，而不是一个通用实现？
**答案**：因为不同的（算子类 × 阶段数 × 布局）组合需要不同的 gmem 迭代器和不同的 threadblock MMA 类（`MmaPipelined` vs `MmaMultistage` vs `MmaSingleStage`）。C++ 模板偏特化是「编译期零开销分发」的标准手段：每种组合在编译期就被钉死成最优实现，运行时没有 if/else 开销。

**练习 2**：`DefaultMma` 里 A 迭代器的 tile 形状是 `(kM, kK)`，B 迭代器是 `(kK, kN)`，为什么？
**答案**：A 是 \(M \times K\)，B 是 \(K \times N\)，C/D 是 \(M \times N\)。每个 CTA 主循环迭代从 A 取一个 `kM×kK` 的列条带、从 B 取一个 `kK×kN` 的行条带，乘加后更新 `kM×kN` 的输出 tile。

---

### 4.3 default_mma_core 的配置机制

#### 4.3.1 概念说明

如果说 `DefaultMma` 是「装配车间」，那 `DefaultMmaCore` 就是「配置清单」。它接收**三层 tile 形状 + 数据类型 + 布局 + 架构**，输出一个装满类型别名的「配置包」`MmaCore`，告诉 threadblock 层：

- `WarpCount`：CTA tile 要切成几个 warp（沿 M/N/K）。
- `SmemLayoutA/B`：A/B 在共享内存里用什么（swizzled）布局。
- `IteratorThreadMapA/B`：每个线程按什么映射把数据从 gmem 写进 smem。
- `SmemIteratorA/B`：smem 上的读迭代器。
- 一个 warp 级算子 `MmaTensorOp`（或 `MmaSimt`）。
- `MmaPolicy`：把 warp 算子 + warp 数 + skyline（边界 padding）打包给 `ThreadblockMma`。

`DefaultMmaCore` 是一个**只声明主模板、全靠按架构偏特化**的结构体。主模板在 `default_mma_core.h` 里只有声明；真正的实现拆成多个文件：

| 文件 | 覆盖 |
| --- | --- |
| `default_mma_core_simt.h` | CUDA Core（Simt）路径 |
| `default_mma_core_sm70.h` | Volta TensorOp |
| `default_mma_core_sm75.h` | Turing TensorOp |
| `default_mma_core_sm80.h` | Ampere TensorOp（含多级缓冲、cp.async） |

#### 4.3.2 核心流程

`WarpCount` 是配置包的核心，它由三层 tile 直接决定：

\[ \text{WarpCount} = \text{GemmShape}\left( \frac{\text{ThreadblockShape::kM}}{\text{WarpShape::kM}},\ \frac{\text{ThreadblockShape::kN}}{\text{WarpShape::kN}},\ \frac{\text{ThreadblockShape::kK}}{\text{WarpShape::kK}} \right) \]

举例：若 CTA tile 为 \(128 \times 128 \times 32\)，warp tile 为 \(64 \times 64 \times 32\)，则 `WarpCount = (2, 2, 1)`，共 4 个 warp，每个 warp 32 线程，CTA 共 128 线程。这正是 4.1 里 `kThreadCount = 32 * WarpCount::kCount` 的来源。

配置包的内部组装流程：

```
DefaultMmaCore< TBShape, WarpShape, InstrShape, 元素A/B/C, 布局A/B/C, 算子类, Stages >
   ├── WarpCount = GemmShape<TB/Warp, TB/Warp, TB/Warp>      // 算 warp 数
   ├── kThreads = WarpCount::kCount * 32                       // 算线程数
   ├── SmemLayoutA/B = layout::...TensorOpMultiplicand...     // 选共享内存布局（含 swizzle）
   ├── IteratorThreadMapA/B = ...PitchLinear...ThreadMap      // 算线程→地址映射
   ├── SmemIteratorA/B = RegularTileAccessIterator<...>       // 造 smem 读迭代器
   ├── MmaTensorOp = warp::DefaultMmaTensorOp<WarpShape, InstrShape, ...>::Type  // warp 算子
   └── MmaPolicy = MmaPolicy<MmaTensorOp, ...skylines..., WarpCount::kK>         // 打包成策略
```

`MmaPolicy` 是把「warp 级算子」交给 threadblock 层的载体；`MmaPipelined`/`MmaMultistage` 通过 `Policy::Operator` 拿到它（见 4.1.3 的 `warp_mma`）。

#### 4.3.3 源码精读

**(1) 主模板声明**

[include/cutlass/gemm/threadblock/default_mma_core.h:62-110](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/threadblock/default_mma_core.h#L62-L110) — `struct DefaultMmaCore;` 主模板声明，参数包括三层 Shape、元素/布局、算子类、阶段数等。注意它还带了一堆默认值（如整数元素自动选 `OpMultiplyAddSaturate`）。

**(2) 一个具体特化：SM80 double TensorOp**

[include/cutlass/gemm/threadblock/default_mma_core_sm80.h:103-195](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/threadblock/default_mma_core_sm80.h#L103-L195) — SM80 双精度 TensorOp 的 `DefaultMmaCore` 偏特化。注意行 121-123 的 `WarpCount` 定义，正是上面公式：

[include/cutlass/gemm/threadblock/default_mma_core_sm80.h:121-123](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/threadblock/default_mma_core_sm80.h#L121-L123) — `WarpCount = GemmShape<Shape::kM/WarpShape::kM, Shape::kN/WarpShape::kN, Shape::kK/WarpShape::kK>;`。

[include/cutlass/gemm/threadblock/default_mma_core_sm80.h:137-137](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/threadblock/default_mma_core_sm80.h#L137) — `kThreads = WarpCount::kCount * kWarpSize;`，`kWarpSize=32`，得到 CTA 总线程数。

行 149-151 选共享内存布局（双精度的 `ColumnMajorTensorOpMultiplicandCongruous64b` 等——这些布局自带 swizzle 以避免 bank conflict），行 158-181 定义线程映射和 smem 迭代器，行 188-190 用 `warp::DefaultMmaTensorOp` 造 warp 算子，最后行 193-194 打包成 `MmaPolicy`：

[include/cutlass/gemm/threadblock/default_mma_core_sm80.h:193-194](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/threadblock/default_mma_core_sm80.h#L193-L194) — `using MmaPolicy = MmaPolicy<MmaTensorOp, MatrixShape<0,0>, MatrixShape<0,0>, WarpCount::kK>;`，两个 `MatrixShape<0,0>` 是 skyline（边界 padding，这里为 0 表示不需要）。

**(3) 可整性断言：约束三层 tile 的关系**

[include/cutlass/gemm/threadblock/default_mma_core_sm80.h:126-131](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/threadblock/default_mma_core_sm80.h#L126-L131) — `static_assert(!(Shape::kM % WarpShape::kM) && !(Shape::kN % WarpShape::kN), ...)` 以及 `WarpCount::kCount > 1`。这就是为什么你给的三层 tile 必须「逐层整除」——CTA tile 必须能被 warp tile 整除，否则无法均匀切给各 warp。

> 这一小节的结论：`DefaultMmaCore` 是把「人写的三层 tile」翻译成「机器需要的 warp 数 / smem 布局 / warp 算子」的字典。换一个架构，就换一个特化文件，其它上层代码完全不动。

#### 4.3.4 代码实践

1. **实践目标**：亲手计算一个常见配置的 `WarpCount` 与线程数，并用源码核对。
2. **操作步骤**：
   - 假设一个 SM80 FP16 GEMM 配置：`ThreadblockShape = <128,128,32>`，`WarpShape = <64,64,32>`，`InstructionShape = <16,8,16>`。
   - 用上面的公式算 `WarpCount` 和 `kThreads`。
   - 打开 `default_mma_core_sm80.h` 找到 FP16 TensorOp 的偏特化（搜索 `half` 或 `ColumnMajorTensorOpMultiplicandCongruous`），确认它定义 `WarpCount` 的方式与公式一致。
3. **需要观察的现象**：FP16 特化与上面给出的 double 特化结构相同，只是 `SmemLayoutA/B` 不同（FP16 用 16b 的 multiplicand 布局）。
4. **预期结果**：`WarpCount = (2,2,1)`，`kCount = 4`，`kThreads = 128`。
5. **待本地验证**：不同 `WarpShape` 下 occupancy 的实际变化（需要 GPU）。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `WarpShape` 取成和 `ThreadblockShape` 完全相同，`WarpCount` 是多少？合理吗？
**答案**：`WarpCount = (1,1,1)`，`kCount=1`，即整个 CTA 只有 1 个 warp（32 线程）。对 TensorOp 路径，[default_mma_core_sm80.h:130-131](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/threadblock/default_mma_core_sm80.h#L130-L131) 的 `static_assert(WarpCount::kCount > 1)` 会直接编译失败——TensorOp 至少需要 2 个 warp。

**练习 2**：`MmaPolicy` 里的 `WarpCount::kK`（最后一个模板参数）表达什么？
**答案**：它表示沿 K 维把 CTA tile 切给了几个 warp（`kPartitionsK`）。多数配置 `WarpShape::kK == ThreadblockShape::kK`，所以 `WarpCount::kK = 1`，即 K 维不跨 warp 切分；但某些配置允许沿 K 维分片（partition K），用于更大的累加宽度。

---

### 4.4 2.x 与 3.x 分层方式的本质差异

#### 4.4.1 概念说明

CUTLASS 3.x 引入了 CuTe（你已在 u2-l1～u2-l4 学过它的 Layout/Tensor/Atom），并重组了 GEMM 的分层。理解 2.x 和 3.x 的区别，是避免在两套 API 间迷路的关键。

| 维度 | 2.x | 3.x |
| --- | --- | --- |
| **分层切分依据** | 三层 `GemmShape` + `WarpCount` 显式算 | CuTe 的 `Layout`/`TiledMma`/`TiledCopy` 代数化切分 |
| **拼装入口** | `DefaultMma` + `DefaultMmaCore`（按架构一堆偏特化） | `CollectiveBuilder` + `collective::CollectiveMma` |
| **顶层封装** | `device::Gemm` | `device::GemmUniversalAdapter<kernel::GemmUniversal>` |
| **主循环形态** | `MmaPipelined`/`MmaMultistage`（手动双缓冲/多级） | producer/consumer + 异步 pipeline（u3-l1）、TMA（u3-l2） |
| **指令复用** | `thread::Mma`/`warp::Mma*` + `arch::mma` | `MMA_Atom`/`Copy_Atom` + `TiledMma`（u2-l4/u2-l5） |
| **适用架构** | Volta~Ampere（SM70~SM80）为主 | Hopper（SM90）起为一等公民，Blackwell（SM100）仅 3.x |

#### 4.4.2 核心流程

2.x 的组装是「**按架构写死的偏特化森林**」：

\[ \text{device::Gemm} \xrightarrow{\text{DefaultGemm}} \text{kernel::Gemm} \xrightarrow{\text{DefaultMma}} \text{ThreadblockMma} \xrightarrow{\text{DefaultMmaCore}_{\text{smXX}}} \text{warp::Mma} \xrightarrow{\text{arch}} \text{mma指令} \]

每换一个架构，就要新写一个 `default_mma_core_smXX.h` 和一堆 `warp/mma_smXX.h`。可读性好、控制精细，但**扩展性差**：新增一个架构需要改动大量文件。

3.x 的组装是「**代数驱动的统一管线**」：用 CuTe 的 Layout 把「坐标→地址」做成纯函数（u2-l1），用 `TiledMma`/`TiledCopy` 在编译期自动铺指令（u2-l4），上层只描述「我想怎么切」，具体落哪条指令由 Layout/Engine 在编译期推断。这就是 3.x 能用同一套 `CollectiveBuilder` 同时支持 Hopper 和 Blackwell 的原因。

#### 4.4.3 源码精读

**(1) 2.x 顶层：device::Gemm（本讲主角）**

[include/cutlass/gemm/device/gemm.h:264-289](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/device/gemm.h#L264-L289) — 2.x 的内核由 `kernel::DefaultGemm` 一次性装配出来，依赖一长串 `DefaultMma`/`DefaultMmaCore` 的偏特化。模板参数极多（行 169-232），但都是「形状/类型」类参数。

**(2) 配置森林：按架构拆分的 DefaultMmaCore**

[include/cutlass/gemm/threadblock/default_mma.h:49-55](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/threadblock/default_mma.h#L49-L55) — 2.x 通过显式 `#include` 各架构的 `default_mma_core_sm70/sm75/sm80.h` 与 `default_mma_core_simt.h` 来挂载配置。每加一个架构就加一个头文件、一套偏特化——这正是 3.x 想用代数化解的「重复」。

> 对照（先看一眼即可，细节留给 u2-l7/u2-l8）：3.x 的对应物是 `include/cutlass/gemm/collective/collective_builder.hpp`，它根据「架构 + 数据类型 + dispatch_policy」用一套机制推断出 `CollectiveMma`，不再为每个架构手写一整套 `MmaCore`。3.x 的内核是 `kernel::GemmUniversal`（不再是 2.x 的 `kernel::Gemm`）。

#### 4.4.4 代码实践

1. **实践目标**：在源码中直观感受「2.x 按架构拆文件、3.x 用 builder 推断」的差异。
2. **操作步骤**：
   - 用 Glob 列出 `include/cutlass/gemm/threadblock/default_mma_core_*.h`，数一下 2.x 为不同架构/算子写了多少个 core 文件。
   - 再看 `include/cutlass/gemm/collective/collective_builder.hpp` 的开头注释，体会 3.x 用一个 builder 统一处理多架构的思路。
3. **需要观察的现象**：2.x 的 core 文件成排出现（simt/sm70/sm75/sm80…），而 3.x 的 collective 文件名直接编码架构（`sm90_...`/`sm100_...`）但由 builder 统一分派。
4. **预期结果**：能说出「2.x = 手写偏特化森林，3.x = builder + CuTe 代数」这一句话区别。
5. 本实践为目录浏览型，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 NVIDIA 在 Hopper（SM90）之后基本只用 3.x API，而 2.x 仍保留？
**答案**：3.x 的 CuTe 代数 + CollectiveBuilder 能用一套代码覆盖 Hopper/Blackwell 的复杂特性（TMA、warp specialization、TMEM），扩展成本低；但 2.x 历史悠久、控制精细、对 Volta~Ampere 的覆盖成熟稳定，且很多老项目和 `tools/library` 实例库仍依赖它，因此保留。

**练习 2**：用一句话说出 `device::Gemm`（2.x）与 `GemmUniversalAdapter`（3.x）在「装配」上的区别。
**答案**：前者通过 `kernel::DefaultGemm` + 一堆按架构手写的 `DefaultMmaCore` 偏特化来拼内核；后者通过 `CollectiveBuilder` + CuTe 的 `TiledMma`/`TiledCopy` 在编译期由 Layout 代数自动推断内核结构。

---

## 5. 综合实践：画出 threadblock → warp → thread 的类组合关系图

把本讲四个模块串起来，完成下面这个贯穿性任务。

**任务**：选定一个具体配置（建议沿用 u1-l6 的 `00_basic_gemm`：6 个模板参数的 `device::Gemm`，即 `OpClassSimt` + `Sm70` + ColumnMajor 输出），追踪它的模板展开，画出从 `device::Gemm` 到最底层算子的类组合关系图。

**步骤**：

1. **确定落点**。因为 `LayoutC = ColumnMajor`，先确认它命中转置偏特化（[device/gemm.h:572-633](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/device/gemm.h#L572-L633)），转成 RowMajor 的 `UnderlyingOperator`。
2. **追 `GemmKernel`**。沿 `UnderlyingOperator::GemmKernel → kernel::DefaultGemm::GemmKernel` 找到 `kernel::Gemm`（[kernel/gemm.h:59](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/gemm.h#L59)），记下它的 `Mma` 模板参数来源。
3. **追 `ThreadblockMma`**。`DefaultGemm` 用 `threadblock::DefaultMma<...>::ThreadblockMma` 给 `kernel::Gemm` 的 `Mma_`。由于是 Simt + Stages==2，落到 `MmaPipelined`（[default_mma.h:151-186](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/threadblock/default_mma.h#L151-L186)）。
4. **追 warp 算子**。`MmaPipelined::Operator = Policy::Operator`，`Policy` 来自 `MmaCore::MmaPolicy`。Simt 路径下 `Policy::Operator = warp::MmaSimt`（其内部用 `thread::Mma`，见 [mma_simt.h:145-158](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/warp/mma_simt.h#L145-L158)）。
5. **画图**。把上面的链条画成一棵树。

**预期产出（参考答案图）**：

```
device::Gemm<float,ColMajor,float,ColMajor,float,ColMajor>   (LayoutC=ColMajor)
 │  命中 ColumnMajor 偏特化 → 转置
 └─ UnderlyingOperator = device::Gemm<float,RowMajor,...>    ([gemm.h:606-630])
     │  GemmKernel = kernel::DefaultGemm<...>::GemmKernel
     └─ kernel::Gemm< Mma, Epilogue, Swizzle, false >         ([kernel/gemm.h:59])
         │  Mma = ThreadblockMma
         └─ threadblock::DefaultMma<...,OpClassSimt,Stages=2>::ThreadblockMma   ([default_mma.h:151])
             │  = MmaPipelined<Shape, IterA, SmemIterA, IterB, SmemIterB, float, RowMajor, Policy>
             │  MmaCore = DefaultMmaCore<...simt...>          ([default_mma_core.h:62] + core_simt.h)
             └─ Policy::Operator = warp::MmaSimt<...>         ([mma_simt.h])
                 └─ ThreadMma = thread::Mma< GemmShape<1,1,1>, ... >   ([mma_simt.h:145], [thread/mma.h:52])
                     └─ arch::OpMultiplyAdd (CUDA Core 标量 FMA)
```

**进阶（可选，需 GPU）**：把 `00_basic_gemm` 的算子类显式改成 `OpClassTensorOp`、架构改成 `Sm75`/`Sm80`，重新编译（`CUTLASS_NVCC_ARCHS=75` 或 `80`），观察编译错误或成功——体会 4.4 所说的「换架构就换一套 `DefaultMmaCore`/`warp::Mma`」。运行结果 **待本地验证**。

## 6. 本讲小结

- CUTLASS 2.x 用 **device → kernel → threadblock → warp → thread/指令** 五段式分层，目录名、命名空间、类名一一对应（`device/`、`kernel/`、`threadblock/`、`warp/`、`thread/`）。
- `device::Gemm` 只做三件事：编译期映射出 `GemmKernel`、运行期把 `Arguments` 翻译成 `Params`、启动内核；真正干活在设备端。
- `kernel::Gemm` 是设备端入口，它构造 `Mma`（threadblock MMA）和 `Epilogue`，二者共用一块 `union SharedStorage`。
- `DefaultMma` 是 threadblock 层的装配车间：取 `MmaCore` → 造 A/B 的 gmem 迭代器 → 组装成 `MmaPipelined`（Stages=2）或 `MmaMultistage`（Stages≥3）。
- `DefaultMmaCore`（按架构特化的配置清单）把三层 tile 翻译成 `WarpCount`/`SmemLayout`/warp 算子/`MmaPolicy`，并强约束「CTA tile 必须被 warp tile 整除」。
- 2.x 是「按架构手写的偏特化森林」，3.x 则用 CuTe 代数 + `CollectiveBuilder` 统一推断；SM90 起以 3.x 为主，2.x 因成熟稳定而保留。

## 7. 下一步学习建议

- 接下来进入 **[u2-l7 CUTLASS 3.x GEMM 通用模型](u2-l7-gemm-3x-universal-model.md)**，看 3.x 如何用 `kernel::GemmUniversal` + `dispatch_policy` 重组三段式架构，并对照本讲的 2.x 分层加深理解。
- 想深入 2.x 主循环细节，可继续阅读 `include/cutlass/gemm/threadblock/mma_multistage.h`（多级缓冲 + `cp.async`）与 `include/cutlass/gemm/threadblock/mma_base.h`（`WarpCount`、`kWarpGemmIterations` 的来源）。
- 想看 2.x 分层在更高层的「自动化」运用，可阅读 `tools/library`（由 `python/cutlass_library` 批量实例化 `device::Gemm`，见 u3-l8）。

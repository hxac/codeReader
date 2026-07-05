# Grouped 与 Batched GEMM

## 1. 本讲目标

真实工程里很少只算一次矩阵乘。Transformer 的注意力、MoE 的专家线性层、图神经网络的批量边——往往要在一次任务里完成**几十甚至上百次**独立的矩阵乘法。如果为每一次都单独启动一个 kernel，会反复付出 launch 开销，且小矩阵根本喂不饱 GPU。

本讲学完后你应该能够：

- 区分 **Batched GEMM**（所有子问题形状相同）与 **Grouped GEMM**（每个子问题形状可以不同）这两种「一次启动算多个 GEMM」的模型。
- 读懂 CUTLASS 如何把 `problem_sizes`、`ptr_A`、`lda` 等**全部做成数组**下放到显存，让一个 kernel 处理任意形状的一组问题。
- 理解 Grouped GEMM 的核心是**持久化内核（persistent kernel）+ ProblemVisitor**：grid 只启动一波 CTA，每个 CTA 在循环里不断领取「下一个 tile」，直到所有问题的所有 tile 算完。
- 掌握两种调度模式 `kDeviceOnly`（设备上现算）与 `kHostPrecompute`（主机预计算）的取舍，以及它们对 workspace 与同步屏障的影响。
- 看懂示例 `examples/24_gemm_grouped` 如何把同一个工作负载分别当作 Grouped GEMM 和「一串 Batched GEMM」来跑并对比性能。

本讲承接 [u2-l8 CollectiveBuilder 与主循环] 中 CUTLASS 的 problem-shape 与 epilogue 概念，但聚焦在 CUTLASS **2.x 风格** 的 Grouped/Batched 实现（`device::GemmGrouped`、`device::GemmUniversal` 的 `kArray` 模式），这是 example 24 实际使用的路径。

## 2. 前置知识

- **一次 kernel launch 算多个独立 GEMM**：把多个 \(C_i = \alpha A_i B_i + \beta C_i\) 揉进同一个 CUDA grid。它们彼此无数据依赖，区别只在于「形状是否相同」。
- **problem shape**：CUTLASS 用 `cutlass::gemm::GemmCoord`（一个 `{m, n, k}` 三元组）描述一次 GEMM 的问题尺寸。Batched 下它是常量；Grouped 下它是**一个数组**，每个 group 一个值。
- **leading dimension（ldm）与 stride**：见 [u1-l5]。Batched 下所有问题共用同一个 ldm；Grouped 下每个问题有自己的 `lda[i]`、`ldb[i]`，因此 ldm 也是数组。
- **持久化内核（persistent kernel）**：grid 大小不再等于「输出 tile 总数」，而是约等于 SM 数。每个 CTA 算完一个 tile 后**不退出**，循环去领下一个 tile，直到没有剩余工作。这样做能减少尾波（tail wave）浪费，详情可对照 [u3-l3 Tile Scheduling 与 Stream-K]。
- **`device::Gemm` 与 `GemmUniversalMode`**：[u1-l6] 讲过 2.x 的 `device::Gemm`；本讲的 Batched 路径用的是它的泛化版 `device::GemmUniversal`，通过一个模式枚举 `GemmUniversalMode` 区分普通 / 批量 / 数组 / 分组等运行方式。

如果你对「Tensor Core 指令」「epilogue 的 `LinearCombination`」还不熟，建议先读 [u1-l6] 与 [u2-l8]。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [examples/24_gemm_grouped/gemm_grouped.cu](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/24_gemm_grouped/gemm_grouped.cu) | 官方示例。同一批随机问题分别用 Grouped GEMM 和一串 Batched GEMM 跑，对比 GFLOP/s。本讲的主蓝本。 |
| [include/cutlass/gemm/device/gemm_grouped.h](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/device/gemm_grouped.h) | 设备层句柄 `device::GemmGrouped<GemmKernel>`，几乎是空壳，逻辑在父类 `BaseGrouped`。 |
| [include/cutlass/gemm/device/base_grouped.h](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/device/base_grouped.h) | Grouped GEMM 的主机端逻辑：算 grid 大小、分配 workspace、（可选）主机预计算、launch 内核。 |
| [include/cutlass/gemm/kernel/gemm_grouped.h](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/gemm_grouped.h) | 设备端内核 `kernel::GemmGrouped`：定义 `Arguments`/`Params`/`SharedStorage`，以及 `operator()` 里的持久化主循环。 |
| [include/cutlass/gemm/kernel/grouped_problem_visitor.h](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/grouped_problem_visitor.h) | 调度器 `GroupedProblemVisitor`，含 `GroupScheduleMode` 枚举与 `kDeviceOnly`/`kHostPrecompute` 两种特化。决定「哪个 CTA 算哪个问题的哪个 tile」。 |
| [include/cutlass/gemm/kernel/gemm_universal.h](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/gemm_universal.h) | 2.x 通用内核 `kernel::GemmUniversal`，其 `Arguments` 含 `batch_stride_A/B/C` 等 batch 维字段，支撑 Batched/kArray 模式。 |
| [include/cutlass/gemm/gemm_enumerated_types.h](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/gemm_enumerated_types.h) | 定义 `GemmUniversalMode`（`kGemm`/`kBatched`/`kArray`/`kGrouped`/…），是 Batched 路径的模式开关。 |

> 说明：本讲引用的 `kernel/gemm_universal.h`（`.h` 后缀）是 2.x 内核；而 `kernel/gemm_universal.hpp`（`.hpp`）是 3.x 入口（仅做 include 聚合）。example 24 的 Batched 路径走的是前者。

## 4. 核心概念与源码讲解

### 4.1 Grouped 与 Batched：一次启动算多个矩阵乘

#### 4.1.1 概念说明

设想要算 100 个矩阵乘 \(C_i = A_i B_i\)。最朴素的办法是循环启动 100 个 kernel。问题有两个：

1. **launch 开销**：每次 `<<<...>>>` 都有几十微秒的固定成本，小矩阵的算力根本抵不掉它。
2. **小矩阵喂不饱 GPU**：单个 \(256\times256\) 的 GEMM 只能启动很少的 CTA，大量 SM 闲置。

解决办法是把它们合并成**一次 grid launch**。CUTLASS 提供两种合并方式，区别只在于「这 100 个问题形状是否相同」：

| 模型 | 子问题形状 | 指针传递方式 | 代表 API |
| --- | --- | --- | --- |
| **Batched GEMM** | 全部相同（同一 `M,N,K`） | 一个基地指针 + **batch stride**（连续存放）或一个**指针数组** | `device::GemmUniversal` 的 `kBatched` / `kArray` |
| **Grouped GEMM** | 可以各不相同（每个 group 一个 `{M_i,N_i,K_i}`） | `problem_sizes`、`ptr_A`、`lda` 等**全部是数组**，下放到显存 | `device::GemmGrouped` |

因为形状相同，Batched 可以用规整的 stride 寻址，效率最高；Grouped 必须容忍每个问题不同的 tile 数量，调度更复杂，但能处理「形状各异」的真实负载（典型如变长序列的注意力）。example 24 的文件头注释把两者区别说得很清楚：

```cpp
// This differs from "Batched Array" GEMM because the size of each GEMM problem
// in the Grouped GEMM concept may be distinct.
```

#### 4.1.2 核心流程

无论哪种模型，关键都是「**把原本属于主机参数的东西，改成数组搬进显存**」，让 kernel 内部能动态读到第 i 个问题的参数：

```text
主机端：
  为每个 group i 准备 {M_i, N_i, K_i}、ptr_A[i]、ptr_B[i]、lda[i]、ldb[i] ...
  把这些数组拷贝到 device memory
  统计总 tile 数，决定启动多少 CTA（持久化）

设备端（每个 CTA）：
  ProblemVisitor 告诉我「当前该算第 problem_idx 个问题的第 tile_idx 个 tile」
  从数组里取 problem_sizes[problem_idx]、ptr_A[problem_idx]、lda[problem_idx]
  构造迭代器，跑一次 threadblock MMA + epilogue
  领取下一个 tile，循环
```

#### 4.1.3 源码精读

example 24 在 `main` 里同时定义了 Batched 与 Grouped 两条类型，并在同一份随机问题上跑它们：

- Batched 用 2.x 的 `device::GemmUniversal`，模板参数与普通 `device::Gemm` 几乎一样（元素类型、布局、三层 tile、epilogue、swizzle、stages），见 [examples/24_gemm_grouped/gemm_grouped.cu:1489-1507](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/24_gemm_grouped/gemm_grouped.cu#L1489-L1507)。
- Grouped 用 `kernel::DefaultGemmGrouped<...>::GemmKernel` 装配出一个**内核类型**，再交给 `device::GemmGrouped<GemmKernel>` 包装成可启动的句柄，见 [examples/24_gemm_grouped/gemm_grouped.cu:1512-1537](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/24_gemm_grouped/gemm_grouped.cu#L1512-L1537)。

注意 `DefaultGemmGrouped` 的模板参数和 `GemmUniversal` 高度一致——Grouped 内核内部复用的就是同一个 threadblock MMA + epilogue，区别只在「外层如何分发 tile」。模式开关 `GemmUniversalMode` 的取值定义在 [include/cutlass/gemm/gemm_enumerated_types.h:57-64](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/gemm_enumerated_types.h#L57-L64)，其中 `kBatched` 走 stride 寻址、`kArray` 走指针数组。

#### 4.1.4 代码实践

1. **实践目标**：直观感受「Grouped vs Batched」的差异。
2. **操作步骤**：
   - 先按 [u1-l2] 用 `cmake -DCUTLASS_NVCC_ARCHS=80 ..` 构建，编译目标 `24_gemm_grouped`（见 [examples/24_gemm_grouped/CMakeLists.txt:32-35](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/24_gemm_grouped/CMakeLists.txt#L32-L35)）。
   - 跑一次「形状各异」的负载：`./24_gemm_grouped --groups=50 --verbose=true`，观察终端打印的 50 个随机 `MxNxK`。
   - 再跑一次「退化为 batched」的负载：`./24_gemm_grouped --groups=50 --m=1024 --n=1024 --k=1024 --verbose=true`。
3. **需要观察的现象**：verbose 输出里，Batched 路径会把问题「分箱（bin）」成若干组同形状 batched GEMM；而 Grouped 路径始终是「1 个 grouped kernel」。两次都会打印 `Grouped Runtime` 与 `Batched Runtime` 两行 GFLOP/s。
4. **预期结果**：形状完全相同时，两种方式性能接近；形状各异时，Grouped 通常明显优于「先分箱再串成多个 Batched」。
5. 若无 GPU 或未编译，可只读 `--verbose` 对应的打印逻辑 [examples/24_gemm_grouped/gemm_grouped.cu:1192-1213](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/24_gemm_grouped/gemm_grouped.cu#L1192-L1213)，理解每个 group 的 tile 数如何计算，**待本地验证**性能数字。

#### 4.1.5 小练习与答案

**练习 1**：example 24 要求至少 Ampere（SM80）。为什么 Grouped GEMM 对小 GPU 意义更大？

> **答案**：小 GPU 的 SM 数少，单个小 GEMM 启动的 CTA 数更可能远小于 SM 数，闲置更严重；把多个小 GEMM 合并成一次启动能显著提高硬件利用率。

**练习 2**：如果 100 个问题形状**完全相同**，应该选 Grouped 还是 Batched？

> **答案**：选 Batched（`kBatched` 或 `kArray`）。形状相同时 stride 寻址更规整、调度开销更低；Grouped 的 problem-size 数组与动态调度此时是纯额外开销。

### 4.2 Batched GEMM 的 batch 维与 kArray 模式

#### 4.2.1 概念说明

Batched GEMM 假定所有子问题形状相同，于是第 i 个问题的数据地址可以用两种方式表达：

- **`kBatched`（stride 模式）**：所有矩阵在显存里**紧密拼接**成一个大缓冲区，第 i 个 A 的地址 = `base_ptr_A + i * batch_stride_A`。`batch_stride` 通常就是单矩阵的元素数 `M*K`。这是最规整、最高效的方式。
- **`kArray`（指针数组模式）**：每个矩阵分散在显存任意位置，主机准备一个 `ptr_A[batch]` 数组（也在显存），kernel 用 `ptr_A[i]` 寻址。灵活但要额外的指针数组访存。

example 24 的 Batched 路径用的是 **`kArray`**——因为它要把「形状各异」的问题先按形状**分箱（bin）**，同一箱里形状相同，可以当作一个 batched-array GEMM；不同箱之间形状不同，只能再启动一次。所以一次 Grouped 工作负载会被拆成「若干次 batched GEMM」。

#### 4.2.2 核心流程

example 24 的 `TestbedBatched::profile` 做了如下事情：

```text
1. bin_problems(): 按 GemmCoord 把问题分箱（同形状归一类）
2. 对每个 bin：
     收集该箱内所有问题的 ptr_A[idx]/ptr_B[idx]/ptr_C[idx] 进连续数组
     记录该箱的 batch_count = 箱内问题数
3. 对每个 bin 启动一次 device::GemmUniversal(kArray, problem, batch_count, ptr_A_array, ...)
4. 可选用多个 CUDA stream 让不同 bin 的 kernel 并发
```

分箱本身**不计时**（注释明确说明），只是为了构造一个对比基线，衬托 Grouped kernel 的优势。

#### 4.2.3 源码精读

batch 维的存储模型在 2.x 通用内核的 `Arguments` 里：除了常规的 `ptr_A/ptr_B/ptr_C/ptr_D` 与 `lda/ldb/ldc/ldd`，还多了 `batch_stride_A/B/C`，见 [include/cutlass/gemm/kernel/gemm_universal.h:121-145](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/gemm_universal.h#L121-L145)。其构造函数接收 `mode`、`problem_size`、`batch_count` 与四个 `batch_stride_*`，见 [include/cutlass/gemm/kernel/gemm_universal.h:158-192](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/gemm_universal.h#L158-L192)。注意 `batch_stride_D` 被父类 `UniversalArgumentsBase` 持有（构造时第 4 个 `int64_t` 实参）。

example 24 实际构造 Batched 参数时用的是 `kArray` 模式 + 指针数组（注意 `ptr_A_array` 是「指针的指针」，且 batch stride 传 0，因为地址来自数组而非连续 stride）：

```cpp
typename Gemm::Arguments arguments{
  cutlass::gemm::GemmUniversalMode::kArray,
  problem,
  batch_count,
  epilogue_op,
  (void const *)ptr_A_array,
  (void const *)ptr_B_array,
  (void const *)ptr_C_array,
  (void       *)ptr_C_array,
  int64_t(), int64_t(), int64_t(), int64_t(),   // batch_stride 全 0
  int64_t(lda), int64_t(ldb), int64_t(ldc), int64_t(ldc)
};
```

完整代码见 [examples/24_gemm_grouped/gemm_grouped.cu:943-960](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/24_gemm_grouped/gemm_grouped.cu#L943-L960)（warmup）与计时循环 [examples/24_gemm_grouped/gemm_grouped.cu:1053-1070](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/24_gemm_grouped/gemm_grouped.cu#L1053-L1070)。多 stream 并发在 [examples/24_gemm_grouped/gemm_grouped.cu:1029-1089](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/24_gemm_grouped/gemm_grouped.cu#L1029-L1089)，每个 bin 用 `cuda_streams[bin_idx % effective_streams]` 提交。

#### 4.2.4 代码实践

1. **实践目标**：理解 `kArray` 的指针数组布局。
2. **操作步骤**：阅读 example 24 的 `TestbedBatched::profile`（[examples/24_gemm_grouped/gemm_grouped.cu:800-1153](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/24_gemm_grouped/gemm_grouped.cu#L800-L1153)），重点看 `ptr_A_batched_host` 是如何把分散的 `block_A.get() + offset_A.at(idx)` 逐个 push 进数组、再 `copy_from_host` 到显存的。
3. **需要观察的现象**：确认每个 bin 的指针数组长度等于该箱 `batch_count`，且不同 bin 的指针数组在 `ptr_A_batched` 中是**首尾相接**的一段。
4. **预期结果**：能用自己的话说明「`kArray` 模式下，kernel 第 i 个 batch 的 A 地址 = `((ElementA**)ptr_A)[i]`」，即对指针数组做一次间接寻址。
5. 运行 `--streams=4` 观察多 stream 是否带来并发收益，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `kArray` 模式下 `batch_stride_A` 传 0？

> **答案**：因为 `kArray` 不用 stride 寻址，而是通过指针数组 `ptr_A[i]` 直接给出第 i 个矩阵的基地址；`batch_stride` 在此模式下不被使用，故填 0。

**练习 2**：`kBatched`（stride）相比 `kArray`（指针数组）省了什么？

> **答案**：省去了一次显存间接寻址（先读指针数组、再读数据），且数据紧密拼接对缓存/TMA 更友好；代价是要求所有矩阵在显存中等距排列，不够灵活。

### 4.3 Grouped GEMM 的 problem shape 数组与参数布局

#### 4.3.1 概念说明

Grouped GEMM 的关键设计是：**所有随问题而变的参数都做成「长度 = group 数」的数组**。具体包括：

- `problem_sizes[problem_count]`：每个问题的 `{M,N,K}`。
- `ptr_A[problem_count]`、`ptr_B`、`ptr_C`、`ptr_D`：每个问题各自的矩阵基地址（`Element**`，即指针的指针）。
- `lda[problem_count]`、`ldb`、`ldc`、`ldd`：每个问题各自的 leading dimension（类型是 `int64_t*`）。

这些数组本身也放在**设备显存**里，kernel 运行时用 `problem_idx` 索引它们。这正是不依赖 host 预计算也能动态调度（`kDeviceOnly`）的前提——所有调度所需信息都在显存里。

#### 4.3.2 核心流程

example 24 的 `BaseTestbed` 负责把主机端的随机问题打包成显存数组：

```text
allocate():
  遍历每个问题 i：
    用 Layout::packed({m,k}).stride(0) 算出 lda_host[i]（紧密布局的 ldm）
    累加 offset_A[i] = 之前所有 A 的元素总数（把所有 A 拼进一个大缓冲区 block_A）
initialize():
  把 problem_sizes、lda/ldb/ldc/ldd 拷贝到 device
  构造 ptr_A_host[i] = block_A.get() + offset_A[i]，拷贝到 device 的 ptr_A
  随机初始化 block_A/block_B/block_C 数据
```

随后 `TestbedGrouped` 把这些数组组装进 `GemmGrouped::Arguments` 一次性传给内核。

#### 4.3.3 源码精读

内核侧的 `Arguments` 结构定义在 [include/cutlass/gemm/kernel/gemm_grouped.h:130-196](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/gemm_grouped.h#L130-L196)。注意几个关键成员：

```cpp
GemmCoord *problem_sizes{nullptr};      // 每个 group 的 {M,N,K} 数组
int problem_count{0};
int threadblock_count{0};               // 持久化内核要启动的 CTA 数

ElementA ** ptr_A{nullptr};              // 每个 group 的 A 基地址数组（指针的指针）
ElementB ** ptr_B{nullptr};
ElementC ** ptr_C{nullptr};
ElementC ** ptr_D{nullptr};

typename LayoutA::Stride::LongIndex *lda{nullptr};  // 每个 group 的 ldm 数组
... ldb, ldc, ldd ...
```

`Params` 结构（[include/cutlass/gemm/kernel/gemm_grouped.h:203-264](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/gemm_grouped.h#L203-L264)）几乎照搬这些指针，外加一个 `ProblemVisitor::Params`（持有 `problem_sizes`、`problem_count`、可选 workspace 与 `tile_count`）。

主机端的组装发生在 example 24：

```cpp
typename Gemm::Arguments args(
  this->problem_sizes_device.get(),   // device 端 problem_sizes 数组
  this->problem_count(),
  threadblock_count,
  epilogue_op,
  this->ptr_A.get(), this->ptr_B.get(),
  this->ptr_C.get(), this->ptr_D.get(),
  this->lda.get(),   this->ldb.get(),
  this->ldc.get(),   this->ldd.get(),
  this->options.problem_sizes.data()   // host 端副本（仅供预计算/统计用）
);
```

见 [examples/24_gemm_grouped/gemm_grouped.cu:1264-1278](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/24_gemm_grouped/gemm_grouped.cu#L1264-L1278)。其中最后一个 `host_problem_sizes` 是**主机端**的同内容副本，仅用于主机侧的 tile 统计与（可选）预计算，**不传入 device**。

数据缓冲的拼装见 `allocate`（[examples/24_gemm_grouped/gemm_grouped.cu:602-647](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/24_gemm_grouped/gemm_grouped.cu#L602-L647)）与 `initialize`（[examples/24_gemm_grouped/gemm_grouped.cu:650-697](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/24_gemm_grouped/gemm_grouped.cu#L650-L697)）：所有 A 拼进一个 `block_A`，`offset_A[i]` 记录第 i 个 A 的起点，`ptr_A[i] = block_A + offset_A[i]`。

#### 4.3.4 代码实践

1. **实践目标**：动手把「一组问题」打包成 Grouped GEMM 所需的数组布局。
2. **操作步骤**：
   - 在草稿纸上为 3 个问题 `{(128,128,64), (256,64,128), (64,256,64)}` 手算：每个问题的 `lda/ldb/ldc`（ColumnMajor 紧密布局）、`offset_A/B/C/D`，以及 `block_A` 至少需要多少元素。
   - 对照 `allocate`（[examples/24_gemm_grouped/gemm_grouped.cu:613-636](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/24_gemm_grouped/gemm_grouped.cu#L613-L636)）验证你的累加逻辑。
3. **需要观察的现象**：确认 `LayoutA::packed({m,k}).stride(0)` 在 ColumnMajor 下返回的是 `m`（行数），即列主序紧密 ldm；`offset_A[i]` 单调递增。
4. **预期结果**：三个问题的 `block_A` 总元素数 = \(128\cdot64 + 256\cdot128 + 64\cdot64 = 8192+32768+4096 = 45056\)。
5. 把手算结果写成一段注释贴在 `allocate` 上方，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ptr_A` 的类型是 `ElementA**` 而不是 `ElementA*`？

> **答案**：因为每个 group 的 A 在显存中位置独立（甚至可以不连续），需要为每个 group 存一个独立的基地址，于是 `ptr_A` 是「每个 group 一个 `ElementA*`」的数组，即 `ElementA**`。kernel 用 `ptr_A[problem_idx]` 取当前问题的基地址。

**练习 2**：`Arguments` 里同时有 device 端 `problem_sizes` 和 host 端 `host_problem_sizes`，为什么要两份？

> **答案**：device 端那份供 kernel 运行时读取；host 端那份供主机侧统计总 tile 数、分配 workspace 以及 `kHostPrecompute` 预计算使用。主机不能解引用 device 指针，故需同内容的 host 副本。

### 4.4 Grouped 调度：持久化内核与 ProblemVisitor

#### 4.4.1 概念说明

Grouped GEMM 把形状各异的问题塞进**一个** kernel，必须解决一个调度难题：每个问题的 tile 数不同，怎么把所有 tile 公平地分给 CTA？

CUTLASS 的方案是**持久化内核 + ProblemVisitor**：

- **持久化**：grid 大小 = `threadblock_count`，约等于「一波能填满 GPU 的 CTA 数」（`SM 数 × 每 SM 最大活跃块数`），而非「总 tile 数」。
- **ProblemVisitor**：一个访问器对象，每个 CTA 拿到自己的 `blockIdx.x` 后，向它询问「我该算哪个 tile」。算完一个，CTA 用 `tile_idx += gridDim.x` 跳到「隔一个 grid 远」的下一个 tile（类似循环派活），再问一次，直到没有剩余 tile。

这种「跨步领活」让所有 CTA 的工作量大致均衡，且 tile 在 CTA 间分布利于 L2 局部性。`threadblock_count` 由主机端 `sufficient()` 估算。

#### 4.4.2 核心流程

内核 `operator()` 的主循环骨架（伪代码）：

```text
ProblemVisitor visitor(params.problem_visitor, shared_storage.problem_visitor, blockIdx.x);
while (visitor.next_tile()) {            // 还有 tile 要算吗？
  problem_idx   = visitor.problem_index();
  tile_in_prob  = visitor.threadblock_idx();   // 当前问题内的 tile 序号
  problem_size  = visitor.problem_size();      // problem_sizes[problem_idx]
  grid_shape    = visitor.grid_shape(problem_size);

  // 算出当前 tile 在 (M,N) 上的偏移
  threadblock_offset = (tile_in_prob / grid_n) * TileM, (tile_in_prob % grid_n) * TileN, 0

  // 从数组取该问题的指针与 ldm
  ptr_A = params.ptr_A[problem_idx]; ldm_A = params.lda[problem_idx];
  ptr_B = params.ptr_B[problem_idx]; ldm_B = params.ldb[problem_idx];

  // 构造迭代器 -> threadblock MMA（在 K 维循环归约）-> epilogue 写回 D
  mma(gemm_k_iterations, accumulators, iterator_A, iterator_B, accumulators);
  epilogue(output_op, iterator_D, accumulators, iterator_C);

  visitor.advance(gridDim.x);            // 领下一个 tile
}
```

每个 CTA 对**一个 (M,N) 输出 tile** 独立完成**整个 K 维**的乘加与写回，因此不需要跨 CTA 的归约（与 Stream-K 不同，见 4.5）。

#### 4.4.3 源码精读

内核的持久化主循环在 [include/cutlass/gemm/kernel/gemm_grouped.h:296-448](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/gemm_grouped.h#L296-L448)。关键点：

- `while (problem_visitor.next_tile())`：领活循环，见 [include/cutlass/gemm/kernel/gemm_grouped.h:319](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/gemm_grouped.h#L319)。
- 用 `problem_idx` 从数组取指针/ldm（含可选转置），见 [include/cutlass/gemm/kernel/gemm_grouped.h:333-337](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/gemm_grouped.h#L333-L337)。
- 用 `gemm_k_iterations = (k + TileK - 1) / TileK` 把整个 K 维归约进一个 CTA，见 [include/cutlass/gemm/kernel/gemm_grouped.h:386-397](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/gemm_grouped.h#L386-L397)。
- `problem_visitor.advance(gridDim.x)`：跨步领活，见 [include/cutlass/gemm/kernel/gemm_grouped.h:446](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/gemm_grouped.h#L446)。

访问器基类 `BaseGroupedProblemVisitor` 维护三个游标：`tile_idx`（全局 tile 序号，初值 = `blockIdx.x`）、`problem_tile_start`（当前问题的起始 tile）、`problem_idx`。领活的核心是 `advance`：

```cpp
void advance(int32_t grid_size) { tile_idx += grid_size; }
```

见 [include/cutlass/gemm/kernel/grouped_problem_visitor.h:149-152](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/grouped_problem_visitor.h#L149-L152)。`threadblock_idx()` 返回「当前 tile 在当前问题内的局部序号」= `tile_idx - problem_tile_start`（[include/cutlass/gemm/kernel/grouped_problem_visitor.h:144-147](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/grouped_problem_visitor.h#L144-L147)）。

主机端 `sufficient()` 决定启动多少 CTA：它先用 `cudaOccupancyMaxActiveBlocksPerMultiprocessor` 算出「满波」CTA 数，再与「总 tile 数」取 `min`，见 [include/cutlass/gemm/device/base_grouped.h:288-346](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/device/base_grouped.h#L288-L346)。launch 时 grid 就是 `threadblock_count`，见 [include/cutlass/gemm/device/base_grouped.h:415-436](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/device/base_grouped.h#L415-L436)。

#### 4.4.4 代码实践

1. **实践目标**：跟踪一个 CTA 在持久化循环里的领活轨迹。
2. **操作步骤**：假设 `gridDim.x = 4`、3 个问题分别有 `{2, 3, 1}` 个 tile（总 6 个 tile）。手算 `blockIdx.x = 1` 的 CTA 会依次领到哪些 `(problem_idx, tile_in_problem)`。
3. **需要观察的现象**：按 `tile_idx` 初值 1、每次 `+= 4`：第 1 轮 `tile_idx=1` → 问题 0 的第 1 个 tile；第 2 轮 `tile_idx=5` → 问题 2 的第 0 个 tile（因为问题 0 占 tile 0–1、问题 1 占 2–4、问题 2 占 5）；第 3 轮 `tile_idx=9 ≥ 6`，`next_tile` 返回 false，CTA 退出。
4. **预期结果**：CTA 1 共算 2 个 tile。你能据此解释为何 tile 多的问题会被多个 CTA 并行瓜分。
5. 把这条轨迹画成「tile_idx → (problem, local_tile)」的表格，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：grid 为什么不直接等于「总 tile 数」？

> **答案**：那样就不是持久化内核了——每个 CTA 只算一个 tile 就退出，尾波浪费重新出现（最后一批 CTA 可能只有零星几个在干活）。持久化让 CTA 数 ≈ SM 数，CTA 在循环里连续领活，硬件始终接近满载。

**练习 2**：`threadblock_idx()` 与 `blockIdx.x` 有什么区别？

> **答案**：`blockIdx.x` 是硬件赋予的 CTA 全局序号（固定不变）；`threadblock_idx()` 是「当前 tile 在**当前问题**内的局部序号」= `tile_idx - problem_tile_start`，每次领到新问题都会变化，用来定位 tile 在该问题 (M,N) 平面上的位置。

### 4.5 同步屏障与两种调度模式

#### 4.5.1 概念说明

ProblemVisitor 有两种实现，对应枚举 `GroupScheduleMode`（[include/cutlass/gemm/kernel/grouped_problem_visitor.h:51-56](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/grouped_problem_visitor.h#L51-L56)）：

| 模式 | 谁来算「tile → problem」的映射 | 是否需要 workspace | 主机开销 | 设备开销 |
| --- | --- | --- | --- | --- |
| `kDeviceOnly` | 设备上实时算（warp 内前缀和扫描 problem_sizes） | 否（`kRequiresPrecomputation=false`） | 几乎为 0 | 每次领活要做扫描 |
| `kHostPrecompute` | 主机预先算好每个 tile 属于哪个问题，存成数组 | 是（`ProblemInfo` 数组） | 主机预计算 + 拷贝 | 设备只需查表 |

两种模式下，**K 维归约都发生在单个 CTA 内**——`mma(gemm_k_iterations, ...)` 把整条 K 消化进一个累加器片段，然后 epilogue 直接写回 D。所以 Grouped GEMM **没有跨 CTA 的归约、没有 fixup workspace**（这是它和 Stream-K 的根本区别，对照 [u3-l3]）。

#### 4.5.2 核心流程

`kDeviceOnly` 的 `next_tile` 思路：warp 内 32 个 lane 各取一个 problem 的 tile 数，做 warp 级**前缀和（inclusive prefix sum）**得到每个 problem 的「结束 tile 号」，再用 `__shfl_sync`/`__ballot_sync` 二分定位 `tile_idx` 落在哪个 problem。完全在寄存器/warp 内完成，不需要任何 workspace。

`kHostPrecompute` 的 `next_tile` 思路：主机 `host_precompute` 把「每个 tile → `(problem_idx, problem_start)`」填进一个 `ProblemInfo` 数组；设备把一段该数组**预取（prefetch）到共享内存**，每算 `kPrefetchTileCount` 个 tile 刷一次。查找变成共享内存查表，但需要主机预计算与 workspace。

无论哪种模式，持久化主循环里都有一处关键**同步屏障**：因为 `SharedStorage` 是 `main_loop` 与 `epilogue` 的 **union**（共享内存复用），开始下一个 tile 的 MMA 之前必须确保上一轮 epilogue 的所有写回完成，否则会破坏共享内存。这就是内核里 MMA 之前那行 `__syncthreads()` 的职责。

#### 4.5.3 源码精读

`SharedStorage` 是 main_loop 与 epilogue 的 union：

```cpp
struct SharedStorage {
  union {
    typename Mma::SharedStorage main_loop;
    typename Epilogue::SharedStorage epilogue;
  } kernel;
  typename ProblemVisitor::SharedStorage problem_visitor;  // 不可与上面重叠
};
```

见 [include/cutlass/gemm/kernel/gemm_grouped.h:267-275](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/gemm_grouped.h#L267-L275)。正因为 union 复用，循环里 MMA 前必须有屏障，见 [include/cutlass/gemm/kernel/gemm_grouped.h:388-397](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/gemm_grouped.h#L388-L397)（注释 "Wait for all threads to finish their epilogue phases from the previous tile"）。

`kDeviceOnly` 的 `next_tile` 用 warp 前缀和定位 problem，见 [include/cutlass/gemm/kernel/grouped_problem_visitor.h:238-318](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/grouped_problem_visitor.h#L238-L318)，其中前缀和循环：

```cpp
for (int i = 1; i < kThreadsPerWarp; i <<= 1) {
  int32_t val = __shfl_up_sync(0xffffffff, problem_ending_tile, i);
  if (lane_idx >= i) { problem_ending_tile += val; }
}
```

见 [include/cutlass/gemm/kernel/grouped_problem_visitor.h:287-293](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/grouped_problem_visitor.h#L287-L293)。其 `kRequiresPrecomputation = false`、`get_workspace_size` 返回 0，见 [include/cutlass/gemm/kernel/grouped_problem_visitor.h:211-214](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/grouped_problem_visitor.h#L211-L214) 与 [include/cutlass/gemm/kernel/grouped_problem_visitor.h:320-329](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/grouped_problem_visitor.h#L320-L329)。

`kHostPrecompute` 的主机预计算把每个 tile 的 `(problem_idx, problem_start)` 写进数组，见 [include/cutlass/gemm/kernel/grouped_problem_visitor.h:421-442](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/grouped_problem_visitor.h#L421-L442)；设备侧 `next_tile` 从预取缓冲读 `ProblemInfo`，见 [include/cutlass/gemm/kernel/grouped_problem_visitor.h:386-411](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/grouped_problem_visitor.h#L386-L411)；workspace 大小 = `sizeof(ProblemInfo) * entries_per_block * block_count`，见 [include/cutlass/gemm/kernel/grouped_problem_visitor.h:413-419](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/grouped_problem_visitor.h#L413-L419)。预取本身也带 `__syncthreads()`（[include/cutlass/gemm/kernel/grouped_problem_visitor.h:392-405](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/grouped_problem_visitor.h#L392-L405)），防止覆盖正在使用的预取缓冲。

主机端 `BaseGrouped::initialize` 根据 `kRequiresPrecomputation` 分流：需要预计算时先调 `precompute()`（在 host 算好后 `cudaMemcpyAsync` 到 workspace），见 [include/cutlass/gemm/device/base_grouped.h:350-388](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/device/base_grouped.h#L350-L388)；workspace 大小查询见 [include/cutlass/gemm/device/base_grouped.h:195-203](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/device/base_grouped.h#L195-L203)。

example 24 默认用 `kDeviceOnly`，可通过 `--scheduler-modes=all` 两种都跑，见 [examples/24_gemm_grouped/gemm_grouped.cu:1549-1573](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/24_gemm_grouped/gemm_grouped.cu#L1549-L1573)。

#### 4.5.4 代码实践

1. **实践目标**：对比两种调度模式的初始化开销与运行性能。
2. **操作步骤**：运行 `./24_gemm_grouped --groups=200 --scheduler-modes=all --profile-initialization=true --verbose=true`。
3. **需要观察的现象**：终端会分别打印 `grouped-kDeviceOnly` 与 `grouped-kHostPrecompute` 两段，各含 `Grouped Runtime`、`Grouped GFLOPs`，以及 `Init Runtime`（主机 `initialize` 耗时）。
4. **预期结果**：`kHostPrecompute` 的 Init Runtime 明显大于 `kDeviceOnly`（因为多了主机预计算 + workspace 拷贝），但两者的设备 Runtime/GFLOPs 通常接近。这说明「主机预计算」是用主机开销换设备调度的简化，是否划算取决于 `initialize` 的调用频率。
5. 若不可运行，可对照 `precompute`（[include/cutlass/gemm/device/base_grouped.h:145-153](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/device/base_grouped.h#L145-L153)）理解额外开销来源，**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：MMA 之前的 `__syncthreads()` 能去掉吗？为什么？

> **答案**：不能。因为 `SharedStorage` 里 `main_loop` 与 `epilogue` 是 union 复用同一段共享内存。上一个 tile 的 epilogue 刚写过这段内存，若不同步就开始下一个 tile 的 MMA（它会把 main_loop 的 staging buffer 写进去），会与尚未完成的 epilogue 写回发生数据竞争。屏障保证上一轮 epilogue 全部完成。

**练习 2**：Grouped GEMM 为什么不需要 Stream-K 那样的 fixup workspace？

> **答案**：每个输出 tile 的整条 K 维都由**同一个 CTA** 在 `mma(gemm_k_iterations, ...)` 中归约完毕，累加器最终值就在该 CTA 的寄存器里，epilogue 直接写回 D。不存在「同一 tile 被多个 CTA 分摊 K」的情况，自然不需要跨 CTA 归约与 fixup。代价是当某问题 K 极长、tile 数极少时利用率会下降——那正是 Stream-K 要解决的场景。

## 5. 综合实践

**任务**：把 example 24 当作一个小型 benchmark，亲手解释它输出的每一行数字，并回答「什么时候该用 Grouped、什么时候该用 Batched」。

建议步骤：

1. 构建 `24_gemm_grouped`（`CUTLASS_NVCC_ARCHS=80` 或你的目标架构）。
2. 跑三组实验，记录 `Batched Runtime`、`Grouped Runtime` 与对应 GFLOP/s：
   - (a) `--groups=100`（形状随机各异）
   - (b) `--groups=100 --m=2048 --n=1024 --k=1024`（退化为单一形状）
   - (c) `--groups=100 --k=1024 --verbose=true`（只有 K 固定，M/N 随机）
3. 对照本讲源码，解释为什么：
   - (b) 中 Batched 与 Grouped 性能接近；
   - (a) 中 Grouped 通常胜出（因为 Batched 被迫拆成多次 kernel launch）；
   - (c) 是一种「部分同构」的中间态。
4. 加跑 `--scheduler-modes=all --profile-initialization=true`，对比 `kDeviceOnly` 与 `kHostPrecompute` 的 Init/Runtime，写一段结论：如果你的应用**每次都用新问题集**调用 Grouped GEMM，该选哪种调度模式？如果**同一问题集反复算**（如 autotuning）呢？

> 提示：`kHostPrecompute` 的主机预计算在每次 `initialize` 都会重做。若问题集不变、可复用 workspace 与 Params，主机开销可被均摊；否则 `kDeviceOnly` 更划算。

若无 GPU，可只做源码侧分析：通读 [examples/24_gemm_grouped/gemm_grouped.cu:1156-1430](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/24_gemm_grouped/gemm_grouped.cu#L1156-L1430)（`TestbedGrouped`），画出「主机 args → BaseGrouped::initialize → 持久化 launch → ProblemVisitor 分发」的数据流图，并在图上标出每一步用到的本讲概念（problem shape 数组、workspace、同步屏障）。

## 6. 本讲小结

- **Grouped vs Batched**：Batched 处理「形状全相同」的批量矩阵乘（stride 或指针数组寻址）；Grouped 处理「形状各异」的一组矩阵乘（problem_sizes/ptr/ldm 全部数组化）。example 24 用同一负载对比两者。
- **参数数组化**：Grouped 把 `problem_sizes`、`ptr_A/B/C/D`、`lda/ldb/ldc/ldd` 都做成显存数组，kernel 用 `problem_idx` 索引——这是动态调度的前提。
- **持久化内核 + ProblemVisitor**：grid 只启动约「一波」CTA，每个 CTA 在 `while(next_tile())` 循环里用 `tile_idx += gridDim.x` 跨步领活，直到所有问题的所有 tile 算完。
- **K 维单 CTA 归约**：每个输出 tile 的整条 K 由一个 CTA 消化，epilogue 直接写回 D，**无跨 CTA 归约、无 fixup workspace**，与 Stream-K 形成对照。
- **两种调度模式**：`kDeviceOnly` 在 warp 内用前缀和现算、零 workspace；`kHostPrecompute` 主机预计算 tile→problem 映射、需 workspace。取舍在于主机 `initialize` 开销能否被均摊。
- **共享内存 union 与同步屏障**：`main_loop` 与 `epilogue` 复用同一段共享内存，故 MMA 前的 `__syncthreads()` 不可省。

## 7. 下一步学习建议

- **Stream-K 与持久化调度的更深设计**：本讲的持久化内核是 [u3-l3 Tile Scheduling 与 Stream-K] 的前置。读完 Stream-K 后，对照理解「为什么 Grouped 不需要 fixup，而 Stream-K 需要」。
- **3.x 风格的 Grouped/Array GEMM**：本讲聚焦 2.x 的 `device::GemmGrouped`。CUTLASS 3.x 在 Hopper/Blackwell 上用 `GemmUniversal` + `kGrouped`/`kArray` 模式 + TMA descriptor 实现更高性能的变长 Grouped GEMM，建议在掌握 [u3-l2 TMA] 后阅读 `include/cutlass/gemm/kernel/sm90_gemm_array_tma_warpspecialized_*.hpp` 系列。
- **变长序列场景**：Transformer 的变长注意力、MoE 专家分派是 Grouped GEMM 的典型应用。可阅读 `examples/41_fused_multi_head_attention` 等示例，看 Grouped 思想如何推广到更复杂的融合内核。
- **profiler 与实例库**：[u3-l8] 介绍的 `cutlass_profiler` 支持 `--mode=gemm` 配合 grouped 参数，可在不写代码的情况下基准测试 Grouped GEMM，是验证本讲结论的快捷途径。

# 第一个 GEMM：2.x device API

## 1. 本讲目标

本讲带你写出（并真正跑通）第一个 CUTLASS 矩阵乘法内核。我们以官方示例 `examples/00_basic_gemm` 为蓝本，逐行拆解它是如何用 `cutlass::gemm::device::Gemm` 这个模板类完成一次单精度矩阵乘（SGEMM）的。

学完本讲你应当能够：

- 看懂 `00_basic_gemm` 的整体结构：从命令行参数到内核启动再到结果校验。
- 说出 `cutlass::gemm::device::Gemm` 的模板参数含义，并能解释「为什么示例只写了 6 个参数也能工作」。
- 理解 CUTLASS 标志性的 **Arguments 参数对象** 与 **functor（仿函数）设计模式**，知道宿主端参数是如何流进内核的。
- 理解三层 tile（threadblock / warp / instruction）的含义，并能据此计算出一个 GEMM 需要启动多少个线程块（CTA）。
- 学会启动内核、检查 `cutlass::Status`，并用一个朴素参考实现做 **位级（bit-exact）验证**。

## 2. 前置知识

本讲建立在 u1-l2（构建运行）、u1-l4（数值类型）、u1-l5（矩阵布局）之上。开始前请回顾这三点：

1. **GEMM 的数学定义**。矩阵乘带缩放的完整形式是：

   \[ D_{ij} = \alpha \sum_{k=0}^{K-1} A_{ik} B_{kj} + \beta\, C_{ij} \]

   其中 \(A\) 是 \(M \times K\)，\(B\) 是 \(K \times N\)，\(C\) 和 \(D\) 都是 \(M \times N\)。当 \(\alpha=1,\ \beta=0\) 时就退化为普通的 \(D = A \times B\)。这正是 `00_basic_gemm` 的默认参数。

2. **TensorRef 与 leading dimension**（来自 u1-l5）。CUTLASS 用 `TensorRef<元素, 布局>` 把「一个指针 + 一个步长（leading dimension）」打包在一起。下文你会看到形如 `{A, lda}` 的花括号初始化，它就是在构造一个 `TensorRef`。

3. **层次化分解**（来自 u1-l1）。CUTLASS 把一次大 GEMM 拆成 device → threadblock → warp → thread/指令 四层。本讲的 `device::Gemm` 就是 device 层的入口，它向下组装 threadblock/warp/thread 各层。

4. **构建方式**（来自 u1-l2）。本讲示例的构建目标是 `00_basic_gemm`，用 `cmake -DCUTLASS_NVCC_ARCHS=<你的SM>` 后 `make 00_basic_gemm` 即可。默认走的是 SIMT（CUDA Core）路径，因此**几乎任何 NVIDIA GPU 都能跑**，不强制要求 Tensor Core。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [examples/00_basic_gemm/basic_gemm.cu](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/00_basic_gemm/basic_gemm.cu) | 完整示例：实例化 `device::Gemm`、构造 Arguments、启动内核，并用朴素内核做位级验证。本讲的主线。 |
| [include/cutlass/gemm/device/gemm.h](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/device/gemm.h) | `cutlass::gemm::device::Gemm` 模板类的定义，包括模板参数表、`Arguments` 结构体、`initialize()`/`run()`/`operator()` 等方法。 |
| [include/cutlass/gemm/device/default_gemm_configuration.h](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/device/default_gemm_configuration.h) | `DefaultGemmConfiguration` 元函数：根据「算子类别 + 架构 + 数据类型」给出 tile 形状、流水线级数、对齐等一整套默认值。这是「只写 6 个模板参数也能工作」的秘密。 |

辅助参考（会在精读中点到）：

- [include/cutlass/gemm_coord.h](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm_coord.h) — `GemmShape<M,N,K>` 的定义。
- [include/cutlass/epilogue/thread/linear_combination.h](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/thread/linear_combination.h) — epilogue 输出算子 `LinearCombination`，即 \( \alpha\cdot\text{acc}+\beta\cdot\text{src} \) 的实现。

## 4. 核心概念与源码讲解

本讲按四个最小模块推进：先看模板参数（4.1），再看运行时参数对象与调用方式（4.2），再看 tile 如何决定线程块网格（4.3），最后看启动与验证（4.4）。

### 4.1 device::Gemm 模板参数

#### 4.1.1 概念说明

`device::Gemm` 是 CUTLASS 2.x 暴露给用户的**最高层 GEMM 接口**。它的设计哲学在头文件注释里写得很清楚：编译期把「数据类型 + 结构参数」映射到具体的 CUTLASS 组件，运行时把「逻辑参数」映射成内核参数，再启动内核。为了「接口易用」与「灵活性」之间的平衡，它**不是把所有旋钮都暴露出来**，而是给一大批参数提供了「合理默认值」。

模板参数可以分为三组：

- **必填的 6 个**：A/B/C 三个矩阵各自的「元素类型 + 布局类型」。这是用户必须明确给出的。
- **半必填的若干个**：累加器类型、算子类别（Simt / TensorOp）、目标架构（Sm70/Sm80/...）。它们都有默认值，但想要用好 Tensor Core 就必须显式改它们。
- **由默认配置自动推断的一大批**：三层 tile 形状、流水线级数、对齐粒度、epilogue 输出算子、swizzle、split-K 开关等。这些几乎都来自 `DefaultGemmConfiguration`。

#### 4.1.2 核心流程

实例化一个 `device::Gemm` 时，发生的事情是：

1. 用户给出 6 个必填参数（可能再加几个半必填参数）。
2. 其余参数若未指定，则向 `DefaultGemmConfiguration<OperatorClass, ArchTag, ElementA, ElementB, ElementC, ElementAccumulator>` 取默认值。
3. 类内部把这一整套类型参数喂给 `kernel::DefaultGemm<...>::GemmKernel`，得到真正会被启动的那个内核类型 `GemmKernel`。
4. 后续所有运行时操作（构造 Arguments、initialize、run）都围绕这个 `GemmKernel` 展开。

换句话说，`device::Gemm` 是一个**类型层面的「配置 → 内核」编译器**：你给配置，它产出内核类型。

#### 4.1.3 源码精读

模板参数表在此（关注哪些有默认值）：

> [include/cutlass/gemm/device/gemm.h:L169-L233](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/device/gemm.h#L169-L233) — `Gemm` 的完整模板参数表。前 6 个（`ElementA_/LayoutA_/ElementB_/LayoutB_/ElementC_/LayoutC_`）无默认值，是必填项。

几个关键默认值：

- 累加器类型默认等于 C 的元素类型：`ElementAccumulator_ = ElementC_`（L183）。对 float 输入即 float 累加。
- **算子类别默认是 `arch::OpClassSimt`**（L185）——这一点非常关键，见下方「重要提醒」。
- 目标架构默认是 `arch::Sm70`（L187）。
- 三层 tile、流水线级数 `Stages`、对齐 `AlignmentA/B`、`Operator` 全部来自 `DefaultGemmConfiguration`（L189-L224）。

类内部立刻把这些参数组装成真正的内核类型：

> [include/cutlass/gemm/device/gemm.h:L264-L289](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/device/gemm.h#L264-L289) — `using GemmKernel = typename kernel::DefaultGemm<...>::GemmKernel;`。这是「配置产出内核」的落点。

而 `DefaultGemmConfiguration` 对 `OpClassSimt` 的偏特化给出的就是 `00_basic_gemm` 实际拿到的默认 tile：

> [include/cutlass/gemm/device/default_gemm_configuration.h:L67-L96](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/device/default_gemm_configuration.h#L67-L96) — Simt 偏特化：`ThreadblockShape = GemmShape<128,128,8>`、`WarpShape = GemmShape<32,64,8>`、`InstructionShape = GemmShape<1,1,1>`、`kStages = 2`，epilogue 用 `LinearCombination`。

`GemmShape` 本身极简，就是个带 `kM/kN/kK` 静态常量的结构：

> [include/cutlass/gemm_coord.h:L50-L71](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm_coord.h#L50-L71) — `GemmShape<M,N,K>`，提供 `kM`/`kN`/`kK` 等静态成员。

> ⚠️ **重要提醒（初学者最常踩的坑）**：示例只写了 6 个模板参数，于是 `OperatorClass` 取默认值 `OpClassSimt`、`InstructionShape` 是 `<1,1,1>`。`<1,1,1>` 的含义是「每条指令处理 1 个标量」——也就是说 **`00_basic_gemm` 默认跑在 CUDA Core（FMA 指令）上，并没有用 Tensor Core！** 它是「能跑、好懂」的教学版。要想用 Tensor Core，需要显式传入 `arch::OpClassTensorOp` 和对应的 `arch::Sm80` 等架构 tag（见 4.1.5 练习与 4.3 节）。

#### 4.1.4 代码实践

**实践目标**：验证「改算子类别会改变默认 tile」，理解默认配置机制。

**操作步骤（源码阅读型）**：

1. 打开 `include/cutlass/gemm/device/default_gemm_configuration.h`。
2. 找到 SM80 + TensorOp 的偏特化：

   > [include/cutlass/gemm/device/default_gemm_configuration.h:L459-L482](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/device/default_gemm_configuration.h#L459-L482) — Sm80 + `OpClassTensorOp`：`ThreadblockShape<128,256,64>`、`InstructionShape<16,8,16>`、`kStages=3`。

3. 对比 4.1.3 里 Simt 的 `<1,1,1>`：`InstructionShape` 从标量变成了 `16×8×16`，这正是 SM80 `mma.sync` 指令一次处理的分块——意味着用上了 Tensor Core。

**需要观察的现象 / 预期结果**：你应该能在脑中得出结论——同一个 `device::Gemm<float,ColMajor,float,ColMajor,float,ColMajor>`，加上 `,cutlass::arch::OpClassTensorOp, cutlass::arch::Sm80` 两个参数后，底层 `GemmKernel` 会换成完全不同的、走 Tensor Core 的实现。（实际编译需要在支持 Sm80 的 GPU 上、且 `CUTLASS_NVCC_ARCHS=80`。）

#### 4.1.5 小练习与答案

**练习 1**：示例 `using CutlassGemm = ...Gemm<float, ColumnMajor, float, ColumnMajor, float, ColumnMajor>;` 只给了 6 个参数。请列出此时 `OperatorClass`、`ArchTag`、`InstructionShape` 分别取什么值。

**答案**：`OperatorClass = arch::OpClassSimt`、`ArchTag = arch::Sm70`、`InstructionShape = GemmShape<1,1,1>`（来自 Simt 的 `DefaultGemmConfiguration`）。

**练习 2**：为什么 `ElementAccumulator` 通常应该比输入类型更宽（例如 half 输入却用 float 累加）？在本示例里它是什么？

**答案**：累加 \(K\) 次乘积容易溢出/丢精度，所以累加器应取更宽的类型。本示例输入是 float，`ElementAccumulator` 默认等于 `ElementC`，因此也是 float。

---

### 4.2 Gemm args 与 argument 对象

#### 4.2.1 概念说明

CUTLASS 有一个非常一致的设计模式：**把内核需要的所有运行时输入打包成一个 `Arguments` 结构体**，在宿主代码里构造好，再整体传给内核。`Arguments` 里装的是「人能理解」的逻辑量——问题尺寸、各矩阵的 `TensorRef`（指针 + 步长）、epilogue 的标量（alpha/beta）、split-K 切片数等。

这与「内核级 `Params`」是两套东西：`Arguments` 是给用户用的逻辑参数；`Params` 是 CUTLASS 内部预计算好的、设备代码直接消费的状态（比如网格形状、经过转译的指针）。`device::Gemm` 的职责之一就是在 `initialize()` 里把 `Arguments` 翻译成 `Params`。

配套的还有 **functor（仿函数）设计模式**：`device::Gemm` 是一个带 `operator()` 的对象。把「初始化」和「执行」解耦，便于在稳态阶段反复启动同一内核而只换少量参数（见 `update()`）。

#### 4.2.2 核心流程

一次完整的调用链（示例里 `gemm_operator(args)` 这一行展开后）：

```
gemm_operator(args)                       // 用户调用
  └─ operator()(Arguments const&, ...)   // 重载的函数调用运算符
       ├─ initialize(args, workspace)    // Arguments → 内核 Params；计算 grid
       └─ run(stream)                    // 真正 <<<grid,block,smem>>> 启动
```

而 `Arguments` 本身的构造，示例用了「逐字段花括号初始化」：

```
Arguments args(
  {M, N, K},        // problem_size  → GemmCoord
  {A, lda},         // ref_A         → TensorRef（指针 + leading dim）
  {B, ldb},         // ref_B
  {C, ldc},         // ref_C（源）
  {C, ldc},         // ref_D（目的，可与 C 同址）
  {alpha, beta}     // epilogue 参数 → LinearCombination::Params
);
```

其中 `{A, lda}` 利用 `TensorRef` 的双参构造隐式生成；`{alpha, beta}` 利用 epilogue 输出算子 `LinearCombination` 的 `Params(alpha, beta)` 构造。

#### 4.2.3 源码精读

示例里构造 Arguments 的那一行（本讲最核心的一行）：

> [examples/00_basic_gemm/basic_gemm.cu:L122-L127](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/00_basic_gemm/basic_gemm.cu#L122-L127) — 用花括号初始化构造 `CutlassGemm::Arguments`。六个字段分别对应问题尺寸、A/B/C/D 四个 TensorRef、以及 epilogue 的 `{alpha, beta}`。

`Arguments` 结构体本身的字段：

> [include/cutlass/gemm/device/gemm.h:L292-L347](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/device/gemm.h#L292-L347) — `Arguments` 持有 `problem_size`、`ref_A/ref_B/ref_C/ref_D`、`epilogue`、`split_k_slices` 以及 gather/scatter 索引。注意 `ref_C` 是常量引用（源），`ref_D` 是可写引用（目的）。

epilogue 的 `{alpha, beta}` 到底是什么——就是 `LinearCombination::Params`：

> [include/cutlass/epilogue/thread/linear_combination.h:L55-L114](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/thread/linear_combination.h#L55-L114) — 注释明确写出 `D = alpha * accumulator + beta * source`（L55）；`Params(alpha, beta)` 构造函数在 L108-L114。这正好对应 4.2.1 节里的数学公式。

functor 调用入口（示例里 `gemm_operator(args)` 实际命中的重载）：

> [include/cutlass/gemm/device/gemm.h:L508-L520](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/device/gemm.h#L508-L520) — `operator()(Arguments const& args, void* workspace, cudaStream_t stream)`：先 `initialize(args)`，成功后再 `run(stream)`。

`initialize()` 把 Arguments 翻译成内核 `Params` 的落点：

> [include/cutlass/gemm/device/gemm.h:L436-L448](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/device/gemm.h#L436-L448) — 用 `GemmKernel::Params{ problem_size, grid_shape, ref_A..D, epilogue, workspace, ... }` 构造设备端参数对象。

> 💡 **进阶提示（先有个印象即可）**：示例里 A、B、C **都是 ColumnMajor**。当输出布局 `LayoutC = ColumnMajor` 时，命中的其实不是上面的主模板，而是 gemm.h 里对 `LayoutC=ColumnMajor` 的**偏特化**。它内部把问题「转置」处理：交换 A/B、把 `(M,N,K)` 换成 `(N,M,K)`、交给一个 row-major 的底层算子。这就是 CUTLASS 2.x「原生只实现 row-major 输出 GEMM，column-major 靠转置实现」的由来。详见 [include/cutlass/gemm/device/gemm.h:L572-L577](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/device/gemm.h#L572-L577) 与 [L700-L713](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/device/gemm.h#L700-L713)。本讲的「综合实践」正是利用这一点：把布局改成 RowMajor，就会直接走主模板。

#### 4.2.4 代码实践

**实践目标**：追踪一次 `gemm_operator(args)` 的调用链，并预测 epilogue 公式。

**操作步骤（调用链追踪型）**：

1. 在 `basic_gemm.cu` 的 `gemm_operator(args)`（[L133](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/00_basic_gemm/basic_gemm.cu#L133)）处下「断点式」阅读：它调用的是 `device::Gemm::operator()(Arguments const&, ...)`。
2. 顺着 gemm.h L508→L513（`initialize`）→ L516（`run`）走一遍。
3. 在 `initialize` 里定位 L436 的 `Params{...}` 构造，确认 Arguments 的每个字段都被消费。

**需要观察的现象 / 预期结果**：你能说清楚「alpha/beta 从命令行 → `Arguments.epilogue` → `Params_.epilogue` → 设备端 epilogue」这条数据流。

**预测题（不必运行）**：若把命令行 beta 从默认 `0` 改成 `1`，输出 \(D\) 会变成什么？
**预期**：\(D = A\times B + C\)（即把原 C 累加进去），而不是覆盖 C。对应公式 \(\beta=1\)。

#### 4.2.5 小练习与答案

**练习 1**：示例里 `ref_C` 和 `ref_D` 都指向同一块 `C_cutlass`、同一个 `ldc`。这意味着什么？

**答案**：这是一个 **in-place** GEMM——目的矩阵 D 与源矩阵 C 共用同一块显存。当 \(\beta=0\) 时 C 的原值被忽略（结果直接覆盖），所以是否同址无所谓；当 \(\beta\neq0\) 时则是在原 C 上做累加更新。

**练习 2**：`Arguments` 和内核 `Params` 为什么是两个不同的结构？

**答案**：`Arguments` 是面向用户的逻辑参数（指针、步长、标量），可在宿主随意构造；`Params` 是 CUTLASS 内部为设备代码预计算好的状态（含网格形状等），把「初始化开销」从每次启动挪到了 `initialize` 一次。这样稳态下可用更轻的 `update()` 只换指针。

---

### 4.3 线程块 tile 选择

#### 4.3.1 概念说明

GEMM 的「层次化分解」在本讲落到三个具体的形状参数上：

- **ThreadblockShape** \( (T_M, T_N, T_K) \)：一个线程块（CTA）负责计算的输出分块大小。它决定了 grid 有多大。
- **WarpShape** \( (W_M, W_N, W_K) \)：一个 warp 在该分块里承担的子分块。一个 CTA 内有若干 warp 共同覆盖 ThreadblockShape。
- **InstructionShape**：一条底层指令（如 `mma.sync`）一次处理的微小块。Simt 路径是 `<1,1,1>`（标量 FMA），TensorOp 路径则是 `<16,8,16>` 这类。

这三层就是 device → threadblock → warp → instruction 四层里「threadblock/warp/instruction」三层的尺寸。它们来自 `DefaultGemmConfiguration`，可被用户显式覆盖。

#### 4.3.2 核心流程

给定问题 \((M,N,K)\) 与 ThreadblockShape \((T_M,T_N,T_K)\)、split-K 切片数 \(S\)，线程块网格按输出维度切分：

\[ \text{grid}_M = \lceil M / T_M \rceil,\qquad \text{grid}_N = \lceil N / T_N \rceil,\qquad \text{grid}_K = S \]

总 CTA 数约为 \(\text{grid}_M \cdot \text{grid}_N \cdot S\)。注意 \(K\) 维由**每个 CTA 在自己的 mainloop 里循环归约**（循环 \(\lceil K/T_K\rceil\) 次），并不直接变成 grid 维——除非用 split-K（\(S>1\)）把 K 维切成多份、跨 CTA 并行再归约。

对默认问题 \(M=N=K=128\) 与默认 Simt tile \(T=(128,128,8)\)、\(S=1\)：

\[ \text{grid}_M = \lceil 128/128\rceil = 1,\quad \text{grid}_N = 1,\quad \text{grid}_K = 1 \]

也就是**整个 128³ 的问题只启动 1 个 CTA**！每个 CTA 内部沿 K 循环 \(128/8=16\) 次。这正是 `00_basic_gemm` 被当作「冒烟测试」的原因——规模小、跑得快、好验证。

#### 4.3.3 源码精读

grid 形状在 `initialize()` 里由 swizzle 对象算出：

> [include/cutlass/gemm/device/gemm.h:L406-L411](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/device/gemm.h#L406-L411) — `get_tiled_shape(problem_size, {ThreadblockShape::kM, kN, kK}, split_k_slices)` 得到 `grid_shape`（一个 `GemmCoord`），随后写入 `params_`。

三种 tile 默认值的来源（再次强调它是按「算子类别 + 架构 + 类型」查表得到的）：

> [include/cutlass/gemm/device/default_gemm_configuration.h:L81-L86](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/device/default_gemm_configuration.h#L81-L86) — Simt：`ThreadblockShape<128,128,8>`、`WarpShape<32,64,8>`、`InstructionShape<1,1,1>`、`kStages=2`。

对比一下「开启 Tensor Core」后默认 tile 的变化：

> [include/cutlass/gemm/device/default_gemm_configuration.h:L467-L470](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/device/default_gemm_configuration.h#L467-L470) — Sm80 + TensorOp：`ThreadblockShape<128,256,64>`、`WarpShape<64,64,64>`、`InstructionShape<16,8,16>`、`kStages=3`。`InstructionShape` 不再是 1×1×1，且流水线从 2 级升到 3 级。

`GemmShape` 的静态成员（计算 grid 时就是读这些 `kM/kN/kK`）：

> [include/cutlass/gemm_coord.h:L50-L58](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm_coord.h#L50-L58) — `GemmShape<M,N,K>` 暴露 `kM`/`kN`/`kK` 等编译期常量。

#### 4.3.4 代码实践

**实践目标**：手算两个规模下的 grid，直观感受 tile 与 grid 的关系。

**操作步骤（纸笔 + 源码阅读型）**：

1. 用默认 Simt tile \(T=(128,128,8)\)、\(S=1\)，分别对 \((M,N,K)=(128,128,128)\) 和 \((4096,4096,4096)\) 计算 \(\text{grid}_M,\text{grid}_N\) 与总 CTA 数。
2. 然后假设改用 Sm80+TensorOp 的默认 tile \(T=(128,256,64)\)，对 4096³ 重算一遍。

**需要观察的现象 / 预期结果（待本地/纸笔验证）**：

- 128³：grid = 1×1，总 CTA = 1。
- 4096³（Simt tile）：grid_M = ⌈4096/128⌉ = 32，grid_N = ⌈4096/128⌉ = 32，总 CTA = 1024。
- 4096³（TensorOp tile 128×256）：grid_M = 32，grid_N = ⌈4096/256⌉ = 16，总 CTA = 512；同时每 CTA 的 K 循环从 4096/8=512 次降到 4096/64=64 次。

**预期结论**：更大的 ThreadblockShape → 更少的 CTA、每个 CTA 干更多活；`InstructionShape` 决定是否走 Tensor Core。

#### 4.3.5 小练习与答案

**练习 1**：为什么通常希望 `ThreadblockShape::kM`/`kN` 是 `WarpShape::kM`/`kN` 的整数倍？

**答案**：因为一个 CTA 由若干 warp 铺满整个 ThreadblockShape。只有整除关系才能让 warp 干净地切分输出分块（CTA 内 warp 数 ≈ \(T_M/W_M \times T_N/W_N\)）。

**练习 2**：默认 `split_k_slices = 1`。若问题 \(K\) 很小但 \(M,N\) 很大，增大 split-K 有用吗？

**答案**：基本没用。split-K 是用来在「输出 tile 数不够多、GPU 喂不饱」时沿 K 维再切分以增加并行度。当 \(M,N\) 已足够大、grid 已铺满 SM 时，split-K 反而引入跨 CTA 归约开销。

---

### 4.4 启动与验证

#### 4.4.1 概念说明

最后一步是「真正启动内核」和「确认结果对」。CUTLASS 的启动被封装在 `run()` 里：它读 `initialize()` 算好的 `params_.grid_tiled_shape` 得到 `grid`，固定用 `block = (kThreadCount, 1, 1)`，按需申请动态共享内存，然后用 CUTLASS 自己的 `cutlass::Kernel<GemmKernel><<<grid, block, smem, stream>>>(params_)` 启动——本质就是普通的 CUDA 启动语法，包了一层模板。

验证方面，`00_basic_gemm` 自带一个**手写的朴素 GEMM 内核** `ReferenceGemm_kernel` 作为参考。它和 CUTLASS 用同样的输入、同样的 \(\alpha,\beta\)，把结果写到另一块 `C_reference`，然后两边都拷回宿主做 **`std::vector` 逐元素相等比较**——对 float 而言这是位级（bit-exact）判定（相同输入、相同运算顺序下，浮点结果应完全一致）。

#### 4.4.2 核心流程

启动侧（`run()` 内）：

1. 由 swizzle 把 `grid_tiled_shape` 翻成三维 `dim3 grid`。
2. `block = dim3(GemmKernel::kThreadCount, 1, 1)`。
3. `smem_size = sizeof(GemmKernel::SharedStorage)`；若 ≥ 48KB，调用 `cudaFuncSetAttribute` 提高动态共享内存上限。
4. `cutlass::Kernel<GemmKernel><<<grid, block, smem_size, stream>>>(params_)`。
5. 检查 `cudaGetLastError()`，转成 `cutlass::Status` 返回。

验证侧（`TestCutlassGemm` 内）：

1. 分配 A(M×K)、B(K×N)，以及**两份**同样初始化（seed=101）的 C：`C_cutlass` 与 `C_reference`。
2. 对 `C_cutlass` 跑 CUTLASS GEMM；对 `C_reference` 跑朴素参考 GEMM。
3. 两者都拷回宿主 `std::vector`，用 `host_cutlass != host_reference` 判等。
4. 全部相等则 `main` 打印 `Passed.`。

#### 4.4.3 源码精读

`run()` 的内核启动（本模块最关键的一段）：

> [include/cutlass/gemm/device/gemm.h:L473-L500](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/device/gemm.h#L473-L500) — 计算 `grid`/`block`/`smem_size`，必要时提升共享内存上限（L484-L492），最终 `cutlass::Kernel<GemmKernel><<<grid, block, smem_size, stream>>>(params_)`（L495）。

示例里对返回状态的检查：

> [examples/00_basic_gemm/basic_gemm.cu:L133-L141](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/00_basic_gemm/basic_gemm.cu#L133-L141) — `cutlass::Status status = gemm_operator(args);`，非 `kSuccess` 即返回 `cudaErrorUnknown`。这是调用 CUTLASS 后做错误处理的标准写法。

leading dimension 的计算（列主序假设）：

> [examples/00_basic_gemm/basic_gemm.cu:L295-L297](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/00_basic_gemm/basic_gemm.cu#L295-L297) — `lda=M; ldb=K; ldc=M;`。列主序下，连续维是行（M），所以 ldm 等于「行数」。A 是 M×K → lda=M；B 是 K×N → ldb=K；C 是 M×N → ldc=M。

位级判等：

> [examples/00_basic_gemm/basic_gemm.cu:L438-L442](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/00_basic_gemm/basic_gemm.cu#L438-L442) — `if (host_cutlass != host_reference)` 直接比较两个 `std::vector<float>`，元素级相等。

成功标志：

> [examples/00_basic_gemm/basic_gemm.cu:L489-L491](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/00_basic_gemm/basic_gemm.cu#L489-L491) — 验证通过则打印 `Passed.`。

参考实现（朴素三重循环，列主序索引）：

> [examples/00_basic_gemm/basic_gemm.cu:L248-L255](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/00_basic_gemm/basic_gemm.cu#L248-L255) — `accumulator += A[i + k*lda] * B[k + j*ldb];` 然后 `C[i + j*ldc] = alpha*accumulator + beta*C[i + j*ldc];`。注意这里的列主序下标 `i + k*lda`——本讲综合实践中改成行主序时，这些下标也要跟着翻转。

#### 4.4.4 代码实践

**实践目标**：用自定义规模运行示例，观察 `Passed.` 输出。

**操作步骤（运行型）**：

1. 按 u1-l2 构建：`cmake -B build -DCUTLASS_NVCC_ARCHS=<你的SM，如 80 或 89>`，再 `cmake --build build --target 00_basic_gemm -j`。
2. 运行 `./build/examples/00_basic_gemm/00_basic_gemm`（默认 128×128×128，α=1，β=0）。
3. 带参数运行：`./00_basic_gemm 512 512 512 1 0`（M N K α β），再试 `./00_basic_gemm 512 512 512 1 1` 观察累加效果。

**需要观察的现象 / 预期结果**：标准输出应打印 `Passed.`。若改 beta=1，参考实现与 CUTLASS 会**同步**采用相同 β，因此仍应位级一致、打印 `Passed.`。

> 若手头没有 GPU 或无法构建，可标注「待本地验证」并在阅读层面确认：参考内核与 CUTLASS 内核使用相同的 A/B/C 指针、相同的 α/β 与 ldm，因此结果必然一致。

#### 4.4.5 小练习与答案

**练习 1**：为什么示例要分配**两份** C（`C_cutlass`、`C_reference`）并先做一次 `cudaMemcpy` 对齐它们的初值？

**答案**：因为 CUTLASS GEMM 和参考 GEMM 会分别**就地**写各自的 C。只有两者起点完全相同（同一 seed 初始化 + 一次 D2D 拷贝对齐），比较才有意义；尤其当 β≠0 时，C 的初值会参与运算。

**练习 2**：`run()` 里 `if (smem_size >= (48 << 10))` 那段是做什么的？

**答案**：CUDA 默认每个线程块最多用 48KB 共享内存，超过就要显式调用 `cudaFuncSetAttribute(..., cudaFuncAttributeMaxDynamicSharedMemorySize, smem_size)` 申请。大 tile 的内核共享内存常超 48KB，所以需要这段。

---

## 5. 综合实践

**任务**：把 `examples/00_basic_gemm` 从「列主序」改造成「行主序（RowMajor）」GEMM，重新编译运行，并确认与参考实现仍然位级一致。

这个任务把本讲四个模块串起来：你会改模板参数（4.1）、调整 Arguments 的 leading dimension（4.2）、理解 tile 不变但 grid 不变（4.3）、并同步修改验证逻辑（4.4）。同时它还利用了 4.2 里提到的「RowMajor 输出会直接走主模板、绕过 ColumnMajor 转置偏特化」这一性质。

### 操作步骤

1. **复制一份示例**（不要改原文件，便于对照）：把 `basic_gemm.cu` 复制为 `basic_gemm_rowmajor.cu`，并在该目录 `CMakeLists.txt` 里仿照已有 `cutlass_example_add_executable(00_basic_gemm basic_gemm.cu)` 加一行 `cutlass_example_add_executable(00_basic_gemm_rm basic_gemm_rowmajor.cu)`。

2. **改 CUTLASS 内核的布局 tag**（在 `CutlassSgemmNN` 里）：

   ```cpp
   // 原来是 ColumnMajor
   using RowMajor = cutlass::layout::RowMajor;

   using CutlassGemm = cutlass::gemm::device::Gemm<float,
                                                   RowMajor,   // A
                                                   float,
                                                   RowMajor,   // B
                                                   float,
                                                   RowMajor>;  // C/D
   ```

3. **改 leading dimension**。行主序下连续维是「列」，所以 ldm 等于列数：

   - 在 `TestCutlassGemm` 里把 [L295-L297](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/00_basic_gemm/basic_gemm.cu#L295-L297) 改为：
     `lda = K;`（A 是 M×K，行主序行步长 = K）
     `ldb = N;`（B 是 K×N，行步长 = N）
     `ldc = N;`（C 是 M×N，行步长 = N）

4. **同步改参考内核与初始化内核的下标**（否则验证会错位）。把 [ReferenceGemm_kernel](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/00_basic_gemm/basic_gemm.cu#L248-L255) 与 [InitializeMatrix_kernel](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/00_basic_gemm/basic_gemm.cu#L155-L174) 里的列主序下标翻成行主序：

   - 列主序：`A[i + k*lda]`、`C[i + j*ldc]`
   - 行主序：`A[i*lda + k]`、`C[i*ldc + j]`（此时 `lda=N`、`ldc=N`，行步长即列数）

5. **编译运行**：`cmake --build build --target 00_basic_gemm_rm -j`，然后运行，期望打印 `Passed.`。

### 需要观察的现象 / 预期结果

- 改完后输出仍是 `Passed.`，说明 CUTLASS 的 RowMajor 路径与朴素行主序参考实现位级一致。
- 你会直观体会到：**改布局时，CUTLASS 端只需换 tag + 改 ldm**（矩阵内容、tile 形状都不用动），而朴素参考实现却要手动翻转所有下标——这正是 CUTLASS「布局抽象」的价值。

> 标注：以上 ldm 取值与下标改写基于本仓库当前 HEAD 的源码逻辑推导。若你的 GPU/CUDA 工具链不同，实际编译运行结果以本地为准（「待本地验证」）。

## 6. 本讲小结

- `cutlass::gemm::device::Gemm` 是 2.x 的最高层 GEMM 接口：6 个必填参数（A/B/C 的元素类型 + 布局）+ 一大批来自 `DefaultGemmConfiguration` 的默认值。
- `00_basic_gemm` 只写 6 个参数 → 默认走 **`OpClassSimt`（CUDA Core）** 路径，`InstructionShape<1,1,1>`，并非 Tensor Core；想用 Tensor Core 需显式加 `OpClassTensorOp` + 架构 tag。
- CUTLASS 用 **`Arguments` 结构体 + functor 模式** 传参：`gemm_operator(args)` → `initialize()`（Arguments→内核 Params、算 grid）→ `run()`（真正 `<<<grid,block,smem>>>` 启动）。
- 三层 tile（ThreadblockShape/WarpShape/InstructionShape）决定并行结构；grid 维度按 \(\lceil M/T_M\rceil \times \lceil N/T_N\rceil\) 切分，K 维在 CTA 内循环归约。
- 默认 128³ 问题 + 128×128×8 tile ⇒ 仅 **1 个 CTA**，所以示例是个轻量冒烟测试。
- 验证用朴素参考内核做 **位级 `vector` 比较**，成功打印 `Passed.`；输出为 ColumnMajor 时实际命中「转置偏特化」，RowMajor 则直接走主模板。

## 7. 下一步学习建议

- **纵向深入 2.x 内部**：下一讲 u2-l6「CUTLASS 2.x GEMM 分层结构」会拆开 `device::Gemm` 向下的 `kernel::DefaultGemm → threadblock → warp → thread` 组装链，把本讲里「黑盒」的 `GemmKernel` 打开。
- **横向进入 3.x**：读完 2.x 后，建议进入第 2 单元学习 CuTe（Layout/Tensor/Atom）与 3.x 的 `GemmUniversal` 三段式模型，理解 CUTLASS 为什么在 3.x 重做了整套抽象。
- **想立刻用 Tensor Core**：可基于本讲示例，尝试把模板改成 `Gemm<half_t, RowMajor, half_t, RowMajor, float, RowMajor, float, arch::OpClassTensorOp, arch::Sm80>`（在 Sm80 GPU、`CUTLASS_NVCC_ARCHS=80` 下），观察一个真正的 Tensor Core HGEMM。
- **推荐延伸阅读**：示例头注释里点名的 NVIDIA 博客 *CUTLASS: Linear Algebra on CUDA*（`https://devblogs.nvidia.com/cutlass-linear-algebra-cuda/`），对 2.x 的可调参数有更通俗的讲解。

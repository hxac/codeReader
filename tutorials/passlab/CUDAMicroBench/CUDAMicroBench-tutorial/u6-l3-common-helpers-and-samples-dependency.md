# Common 依赖与 CUDA Samples 工程化：helper 头文件的作用与移植

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `Common/` 目录的来源、它在整个仓库中**真正**的服务对象，以及 `Common/README.md` 的说法与实际代码之间的三处偏差。
2. 读懂 CUDA Samples 官方 Makefile 这份 336 行的"巨石模板"：能把它拆成五个功能层，并精确解释 `SMS` / `GENCODE_FLAGS` / `HIGHEST_SM` 这套多架构代码生成机制在做什么。
3. 从 `#include` 闭包出发（而不是从目录出发）计算一个编译单元的真实外部依赖，把 `Common/`（20 个顶层条目）剥到 6 个必需头文件。
4. 独立完成一次"剥离外部依赖"的工程改造：给 `Conkernels` 写一个不依赖 `../../Common` 的独立 Makefile，并用静态证据 + 运行时输出双重验证行为不变。

本讲是专家层的工程化课程：前三单元你学会的是**读 CUDA 代码**，本讲要学会的是**读 CUDA 工程**——当一个仓库混合了自写代码和上游模板代码时，如何分清哪部分是"自己的"、哪部分是"带来的"。

---

## 2. 前置知识

### 2.1 CUDA Samples 是什么

CUDA Samples 是 NVIDIA 随 CUDA Toolkit 一起发布的官方示例集（通常安装在 `/usr/local/cuda/samples` 或单独的 `cuda-samples` 仓库）。它里面除了几百个示例程序，还有一套所有示例**共享的公共头文件**，历史上称为 Common 目录。本仓库的 `Common/` 就是这套公共头的一个本地快照。

这套头文件解决的是"每个示例都要重写一遍"的琐事：

- 错误检查（`checkCudaErrors`）
- 设备选择（`findCudaDevice`，挑 GFLOPS 最高的 GPU）
- 命令行解析（`getCmdLineArgumentInt`）
- 跨平台计时器（`sdkStartTimer` / `sdkGetAverageTimerValue`）
- 图像读写与比对（`helper_image.h`，供图形类示例用）

### 2.2 header-only 依赖的含义

`Common/` 里几乎全是 `.h` 头文件，全部用 `inline` 或 `template` 实现，**不需要链接任何库**。这意味着"依赖它"的成本只是：① 把文件放进 include 搜索路径；② 多几秒钟编译时间。这也解释了为什么它这么容易被整目录拷来拷去而不出问题。

### 2.3 前置讲义回顾

本讲建立在 u1-l3（三件套骨架）之上。请回忆：

- 仓库中 14 个基准大部分是作者自写的"单行式 Makefile"（如 `CoMem_AXPY/Makefile`）；
- 只有从 CUDA Samples 搬来的那几个基准（Conkernels、GSOverlap、TaskGraph、Shuffle）带着这份巨型模板 Makefile；
- u1-l3 已指出"Conkernels 引用路径疑多一级"。本讲将把这个疑点查个水落石出。

### 2.4 SASS 与 PTX（本讲需要的编译知识）

nvcc 编译 CUDA 代码时会为 GPU 生成两种形态的目标代码：

- **SASS**：特定 SM 架构的原生机器码，只能在完全对应架构上运行，无需运行时编译，启动最快。
- **PTX**：一种虚拟指令集的中间表示（可理解为"GPU 字节码"）。它可以在**更新**的架构上由驱动即时编译（JIT）成 SASS 运行，因此用来保证向前兼容。

`-gencode arch=compute_XX,code=sm_XX` 生成 SASS；`-gencode arch=compute_XX,code=compute_XX` 生成 PTX。记住这个区别，第 4.2 节的 Makefile 逻辑就一目了然。

---

## 3. 本讲源码地图

| 文件 | 作用 | 本讲视角 |
|---|---|---|
| `Common/README.md` | 一句话说明 Common 的来源与用途 | **证据 A**：官方声称的服务对象 |
| `Common/helper_cuda.h` | 错误检查 + 设备选择 | 被引用最多的 helper，精读对象 |
| `Common/helper_functions.h` | 伞形头文件（umbrella header） | 传递依赖的入口 |
| `Common/helper_timer.h` | 跨平台秒表 | 仅 Shuffle 使用 |
| `Common/helper_string.h` / `helper_image.h` / `exception.h` | 传递依赖 | 最小化清单的成员 |
| `Conkernels/Makefile` | Samples 新版模板 | **精读对象**，含疑点 `-I../../Common` |
| `Conkernels/concurrentKernels.cu` | 并发 kernel 基准源码 | **证据 B**：include 列表 |
| `TaskGraph/Makefile` | 同款模板 + 静态库链接 | 对照组，`-I../Common` 写法正确 |
| `GSOverlap/Makefile` | 同款模板 + GCC/QNX 检查 | 模板可扩展性的例子 |
| `Shuffle/cuda_shuffle/Makefile` | Samples **旧版**模板 | 模板代际漂移的证据 |
| `Shuffle/cuda_shuffle/reduction.cpp` | 归约基准 host 端 | **证据 C**：Common 的隐藏用户 |

---

## 4. 核心概念与源码讲解

### 4.1 helper 头文件：CUDA Samples 的"准标准库"

#### 4.1.1 概念说明

当一个仓库从上游（这里是 CUDA Samples）搬运代码时，会形成两类文件：**自写代码**和**带来的代码**。`Common/` 属于后者——它不是为本项目写的，而是 NVIDIA 为几百个示例写的公共工具集，被整目录拷贝进来。

理解这类依赖的关键问题是三个：

1. **谁在用它？**（不是"README 说谁在用它"）
2. **用了它的哪一部分？**（通常远小于整个目录）
3. **依赖是活的还是死的？**（include 路径存在 ≠ 被真正使用）

第 3 点尤其重要。C/C++ 的 `-I` 只是一个搜索路径注册，**不产生任何依赖**；真正的依赖只在 `#include` 语句出现的那一刻才成立。这是本讲最重要的一个工程直觉，后面的"死配置"发现全部由此而来。

#### 4.1.2 核心流程

判断"谁依赖 Common"的正确流程是反向追溯，从 `#include` 出发而不是从 Makefile 出发：

```
对仓库中每个 .cu / .c / .cpp：
    提取所有 #include <xxx.h> 中形如 helper_* / exception.h 的条目   ← 直接依赖
    对 Common/ 内部：展开每个被 include 的头文件自身的 #include      ← 传递依赖
    重复直到闭包不再增长                                              ← include 闭包
再对照每个 Makefile 的 -I 路径：
    该路径在文件系统上是否解析到真实存在的 Common/？
```

#### 4.1.3 源码精读

**证据 A：官方说法。** `Common/README.md` 全文只有一句：

> [Common/README.md:1](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Common/README.md#L1)
> 说明本目录派生自 CUDA Samples，用于支持 GSOverlap、ConKernels、Taskgraph 三个同样派生自 Samples 的基准。

**证据 B：实际 include 关系。** 对全仓库做 `#include` 扫描，结果如下（这是本讲最重要的表格，建议你亲手用 grep 复现）：

| 基准 | 源文件里 include 的 helper 头 | Makefile 里的 `-I` | 该路径实际解析到 |
|---|---|---|---|
| TaskGraph | `helper_cuda.h`、`helper_functions.h` | `-I../Common` | `<仓库>/Common` ✅ |
| GSOverlap | `helper_functions.h`、`helper_cuda.h` | `-I../Common` | `<仓库>/Common` ✅ |
| **Shuffle**（两个子目录） | `helper_cuda.h`、`helper_functions.h` | `-I../../Common` | `<仓库>/Common` ✅ |
| **Conkernels** | **（一个都没有）** | `-I../../Common` | **仓库之外的目录 ❌** |

逐条看证据。

**Conkernels 的源文件只 include 标准库头：**

> [Conkernels/concurrentKernels.cu:6-9](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/concurrentKernels.cu#L6-L9)
> 只 include 了 `<vector>`、`<iostream>`、`<algorithm>`、`<ctime>` 四个 C++ 标准头，没有任何 helper 头。

这解释了一个悬案。u1-l3 留下的疑点"Conkernels 的 `-I../../Common` 疑多一级"，现在可以给出完整结论：

> [Conkernels/Makefile:274](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/Makefile#L274)
> 注册的 include 搜索路径是 `../../Common`。Conkernels 位于仓库根下一级，`../..` 已经**走出仓库**，指向仓库父目录下的 `Common/`——那不存在。

这是一个**双重休眠的缺陷**：路径本身是错的（多写了一级），但因为源文件根本不 include 任何 helper 头，错误的路径从未被触发，编译照常成功。两个独立的错误互相抵消。推测成因：Conkernels 原本在 CUDA Samples 的嵌套目录结构里（`Samples/5_Domain_Specific/concurrentKernels/` 这类两级深度，`../../Common` 正好指向 Samples 根的 Common），搬到本仓库的根下一级时忘了改成 `../Common`。

**Shuffle 才是被 README 遗漏的用户。** README 只列了三个基准，但：

> [Shuffle/cuda_shuffle/reduction.cpp:68-69](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction.cpp#L68-L69)
> 明确 include 了 `helper_cuda.h` 与 `helper_functions.h`。`Shuffle/cuda_global/reduction.cpp` 第 68-69 行完全相同。

而 Shuffle 的 `-I../../Common` 因为基准本身嵌套在 `Shuffle/cuda_shuffle/` 两级深处，**恰好**正确解析到 `<仓库>/Common`。同一个字符串，在两种目录深度下一个是正确配置、一个是死配置——这正说明相对 include 路径是脆弱的。

**helper 头到底提供了什么？** 看被引用最多的 `helper_cuda.h` 的三块核心。

第一块：错误检查。它由一个模板函数加一个宏组成：

> [Common/helper_cuda.h:582-590](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Common/helper_cuda.h#L582-L590)
> 定义模板函数 `check(result, func, file, line)`：当 CUDA 调用返回值非 0 时，向 stderr 打印出错文件、行号、错误码与错误名，然后 `exit(EXIT_FAILURE)` 直接终止进程。

> [Common/helper_cuda.h:592-595](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Common/helper_cuda.h#L592-L595)
> 用 `#define checkCudaErrors(val) check((val), #val, __FILE__, __LINE__)` 把调用点、调用表达式文本、文件名、行号自动塞进检查函数。

这里的 `#val` 是字符串化预处理运算符，`__FILE__` / `__LINE__` 是编译器内建宏——所以 `checkCudaErrors(cudaMalloc(...))` 报错时能精确告诉你哪一行的哪一句调用失败了。这正是 u2-l3 中我们建议给 CoMem_AXPY 手工补上的东西；Samples 系基准天生就有。

**注意一个条件编译陷阱**：上面这个宏被包在 `#ifdef __DRIVER_TYPES_H__` 里（第 592 行）。`__DRIVER_TYPES_H__` 是 `cuda_runtime.h` 内部间接包含的 `driver_types.h` 的 include guard。**这意味着使用前必须先 `#include <cuda_runtime.h>`，否则 `checkCudaErrors` 根本不存在**，编译会报"undeclared"。看 TaskGraph 的 include 顺序，它正确地做到了：

> [TaskGraph/conjugateGradientCudaGraphs.cu:41-47](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/conjugateGradientCudaGraphs.cu#L41-L47)
> 先 include `<cublas_v2.h>`、`<cuda_runtime.h>`、`<cusparse.h>`（第 41-43 行），**之后**才 include `<helper_cuda.h>` 与 `<helper_functions.h>`（第 46-47 行）。

顺序反了就会编译失败。这类"不自足的头文件"（non-self-contained header）是搬运第三方代码时最常见的坑。

第二块：设备选择。

> [Common/helper_cuda.h:845-876](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Common/helper_cuda.h#L845-L876)
> 定义 `findCudaDevice(argc, argv)`：若命令行带 `--device=N` 则用指定设备，否则调用 `gpuGetMaxGflopsDeviceId()` 挑选算力最高的 GPU，`cudaSetDevice` 选中它并打印设备名与 compute capability。

`gpuGetMaxGflopsDeviceId()` 本体在第 784-842 行，遍历所有设备、用 `multiProcessorCount × clockRate` 估算峰值算力并跳过处于独占计算模式的设备。这就是为什么你运行 TaskGraph / GSOverlap / Shuffle 时，开头总会打印一行 `GPU Device 0: "..." with compute capability 7.0`——那行输出来自 helper，不是基准自己写的。

第三块：伞形头文件与传递依赖。

> [Common/helper_functions.h:50-53](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Common/helper_functions.h#L50-L53)
> `helper_functions.h` 自己又 include 了 `helper_image.h`、`helper_string.h`、`helper_timer.h` 三个子 helper。此外它在第 39 行还 include 了 `<exception.h>`——这也是 Common 目录里的文件（注意是尖括号写的项目内头，依赖 `-I` 才能找到）。

于是 **include 闭包**计算如下（从两个入口出发）：

```
helper_cuda.h      → helper_string.h
helper_functions.h → helper_image.h, helper_string.h, helper_timer.h, exception.h
helper_image.h     → helper_string.h, exception.h
helper_timer.h     → exception.h
exception.h        → （仅系统头）

闭包 = { helper_cuda.h, helper_functions.h, helper_string.h,
         helper_image.h,  helper_timer.h,   exception.h }   共 6 个
```

Common/ 目录顶层有 18 个文件，外加 `UtilNPP/`（14 个 NPP 图像处理头）与 `FreeImage/`（第三方图像库的许可证与 include 目录）两个子目录。除 6 个闭包成员与 `README.md` 之外，**其余 11 个顶层代码文件与两个子目录完全无人引用**——包括 `helper_math.h`（38 KB 的向量数学库）、`nvrtc_helper.h`、`drvapi_error_string.h`、`rendercheck_d3d11.*`、`dynlink_d3d11.h`、`helper_cuda_drvapi.h`、`helper_cusolver.h`、`helper_nvJPEG.hxx`、`helper_multiprocess.cpp`，以及整个 NPP 图像处理族。它们是拷贝整个 Common 目录时一并带来的**行李**（baggage）。

顺带一提 `helper_timer.h`：它定义了抽象接口 `StopWatchInterface`（第 44 行）、Linux 实现 `StopWatchLinux`（第 231 行起，用 `gettimeofday` 计时），并在第 369 行起暴露 `sdkCreateTimer` / `sdkStartTimer` / `sdkStopTimer` / `sdkGetAverageTimerValue` 等 `sdk*` 系列包装。**全仓库只有 Shuffle 用它计时**；TaskGraph 和 GSOverlap 用的是 CUDA 事件（`cudaEventRecord`）。也就是说，即便在三个用户之内，helper 的使用深度也分三档：Conkernels 零使用、TaskGraph/GSOverlap 只用错误检查与设备选择、Shuffle 连计时器一起用。

#### 4.1.4 代码实践

**实践目标**：亲手建立"实际依赖表"，验证本节表格，而不是相信讲义。

**操作步骤**：

1. 在仓库根目录执行（这是本讲所有结论的原始出处，务必自己跑一遍）：

   ```bash
   # 1) 谁在 include helper 头？（排除 Common 自身）
   grep -rn "include.*helper_" --include="*.cu" --include="*.c" --include="*.cpp" . | grep -v "^./Common/"

   # 2) 每个 Samples 系 Makefile 注册了什么 include 路径？
   grep -rn "^INCLUDES" --include="Makefile*" .

   # 3) Conkernels 源文件的全部 include
   sed -n '1,12p' Conkernels/concurrentKernels.cu

   # 4) Common 里哪些文件无人引用？（行李清单）
   grep -rln "UtilNPP\|FreeImage\|nvJPEG\|helper_math\|nvrtc\|rendercheck\|dynlink\|drvapi" \
        --include="*.cu" --include="*.c" --include="*.cpp" . | grep -v "^./Common/"
   ```

2. 对每条 `INCLUDES` 行，手动做一次路径解析：进入对应目录，`ls` 一下相对路径，确认它指向哪里。

**需要观察的现象**：

- 第 1 条命令只会命中 TaskGraph、GSOverlap、Shuffle 两个子目录共 5 个文件的 10 行，**不会有 Conkernels**；
- 第 2 条命令会显示 `Conkernels/Makefile:274` 与 `Conkernels/Makefile_serialized:274` 都是 `-I../../Common`，而 `TaskGraph/Makefile:274` 与 `GSOverlap/Makefile:280` 是 `-I../Common`；
- 第 4 条命令应当**输出为空**（可加 `|| echo 无引用` 兜底）。

**预期结果**：你将得到与 4.1.3 表格完全一致的结论，并亲眼确认 `Conkernels/Makefile_serialized` 与 `Conkernels/Makefile` 患有同一个"路径多一级"问题（前者在第 274 行，两份文件此处逐字相同）。

**待本地验证**：以上 grep 在本讲义编写环境中已实际执行并确认；路径解析部分（`ls ../../Common` 报不存在）请在你的机器上复现。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Conkernels/Makefile` 里那个指向仓库之外的 `-I../../Common` 从来没有导致编译失败？

**答案**：`-I` 只向预处理器注册一个搜索路径，只有当某个源文件真的写下 `#include <helper_xxx.h>` 时，预处理器才会去搜索这些路径。`concurrentKernels.cu` 的 include 列表里没有任何 helper 头（只有 4 个 C++ 标准头），所以搜索从未发生，路径是否有效也就无关紧要。这是"配置存在 ≠ 依赖存在"的直接例证。

**练习 2**：如果把 `TaskGraph/conjugateGradientCudaGraphs.cu` 第 46-47 行的两个 helper include 移到第 41 行（`cublas_v2.h` 之前），会发生什么？

**答案**：编译失败。`checkCudaErrors` 宏的定义被 `#ifdef __DRIVER_TYPES_H__` 包住，而这个宏只有在包含 `cuda_runtime.h`（间接包含 `driver_types.h`）之后才会被定义。include 顺序一换，`helper_cuda.h` 里的宏定义整块被预处理器跳过，后面所有 `checkCudaErrors(...)` 调用点都会报"未声明的标识符"。

**练习 3**：仓库中共有几个目录**真正**依赖 Common？分别用到闭包中的哪几个文件？

**答案**：4 个目录——TaskGraph、GSOverlap、Shuffle/cuda_global、Shuffle/cuda_shuffle（README 只列了前两个加 Conkernels，漏了 Shuffle、多算了 Conkernels）。TaskGraph 与 GSOverlap 只调用 `checkCudaErrors` 与 `findCudaDevice`，理论上只需要 `helper_cuda.h` + `helper_string.h` 两个文件；Shuffle 还用了 `sdk*Timer` 家族与 `findCudaDevice`，需要完整的 6 文件闭包（因为它们 include 的是伞形头 `helper_functions.h`，会把 image/timer/exception 一并拉进来）。

---

### 4.2 Samples Makefile 体系：SMS/GENCODE 多架构生成

#### 4.2.1 概念说明

`Conkernels/Makefile` 有 336 行，但其中属于"这个基准自己"的内容不到 30 行。前 300 多行是 NVIDIA 的跨平台构建模板，被几百个 CUDA Samples 共享。理解它的正确方式不是逐行读，而是**分层**读。

这份模板要解决的核心难题是：同一份源码，要在 ×若干种 CPU 架构 ×若干种操作系统 ×若干种 GPU 架构 ×debug/release 两种模式下都能构建。它用的是纯 GNU make 的条件语法（`ifeq` / `filter` / `foreach` / `eval`），没有 autoconf/cmake。

其中与 CUDA 最相关、也最值得学的是 **SMS/GENCODE 机制**：如何用几行 make 语法为多个 GPU 架构批量生成编译选项。

#### 4.2.2 核心流程

整个 Makefile 可以切成五层，自上而下：

```
第 1 层  兼容垫片（L37-70）      识别已废弃的 x86_64=1 / ARMv7=1 等旧变量名并打印警告
第 2 层  平台探测（L72-113）     HOST_ARCH / TARGET_ARCH / TARGET_OS / TARGET_SIZE
第 3 层  编译器选择（L115-160）  按 OS×ARCH 组合挑 HOST_COMPILER，拼出 NVCC 命令
第 4 层  标志聚合（L162-275）    NVCCFLAGS / CCFLAGS / LDFLAGS → ALL_CCFLAGS / ALL_LDFLAGS
第 5 层  多架构生成（L279-304）  SMS → GENCODE_FLAGS        ← 本节重点
第 6 层  目标规则（L308-336）    真正属于本基准的编译、链接、安装、清理规则
```

SMS/GENCODE 一层的展开逻辑是：

```
输入：SMS = "35 37 50 52 60 61 70 75"          （可用 make SMS=... 命令行覆盖）

第一步  对 SMS 里每个 sm：
        GENCODE_FLAGS += -gencode arch=compute_<sm>,code=sm_<sm>
        ⇒ 为 8 个架构各生成一份 SASS

第二步  HIGHEST_SM := $(lastword $(sort $(SMS)))
        sort 是字典序，lastword 取最大 ⇒ 75
        GENCODE_FLAGS += -gencode arch=compute_75,code=compute_75
        ⇒ 额外为最高架构生成一份 PTX，保证在更新架构上可 JIT

输出：nvcc 一共收到 9 个 -gencode，二进制里嵌 8 份 SASS + 1 份 PTX
```

用集合语言描述：设架构集合 \( S = \{s_1, s_2, \dots, s_n\} \)，则生成的目标代码集合为

\[
G \;=\; \underbrace{\{\,\text{SASS}(s_i) \mid s_i \in S\,\}}_{\text{精确匹配，免 JIT}} \;\cup\; \underbrace{\{\,\text{PTX}(\max S)\,\}}_{\text{向前兼容，需 JIT}}
\]

二进制体积随 \( |S| \) 线性增长，启动速度则相反——为当前 GPU 精确生成的 SASS 免去了运行时 JIT。

#### 4.2.3 源码精读

**SMS 的定义与 ARM 分支：**

> [Conkernels/Makefile:280-284](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/Makefile#L280-L284)
> 若目标是 ARM（`armv7l` 或 `aarch64`），默认 SMS 列表多包含一个 `72`（这是 Tegra/Xavier 类嵌入式 GPU 的架构号）；否则是 `35 37 50 52 60 61 70 75`，覆盖 Kepler 到 Turing。

注意 `SMS ?= ...` 用的是 `?=`（条件赋值）。**这意味着命令行可以覆盖它**：

```bash
make SMS="75 80"      # 只为 Turing 和 Ampere 生成代码
make SMS=80           # GSOverlap 的 memcpy_async 需要 SM 8.0，必须这样显式指定
```

这一点直接呼应 u4-l7：`__pipeline_memcpy_async` 的真异步路径要求 SM 8.0+，而模板默认列表最高只到 75，所以仓库开箱编译出的 GSOverlap 并不含 sm_80 目标码——想真正测异步拷贝必须 `make SMS=80`。

**空列表保护与"waive"机制：**

> [Conkernels/Makefile:286-289](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/Makefile#L286-L289)
> 若 `SMS` 被显式置空（`make SMS=`），打印"no SM architectures have been specified"并把 `SAMPLE_ENABLED` 置 0。

> [Conkernels/Makefile:302-304](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/Makefile#L302-L304)
> 当 `SAMPLE_ENABLED=0` 时，把 `EXEC` 变量重定义成 `@echo "[@]"`——之后所有规则里的命令都变成"打印占位符而不真执行"，即**优雅跳过（waive）**该示例，而不是报错中止。

这是很值得借鉴的 make 技巧：不写两套规则，而是让命令前缀变量可替换。GSOverlap 把它用在了 QNX 与老 GCC 两个场景（见下）。

**核心的 foreach/eval 一行：**

> [Conkernels/Makefile:291-300](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/Makefile#L291-L300)
> 第 293 行用 `$(foreach sm,$(SMS),$(eval GENCODE_FLAGS += -gencode arch=compute_$(sm),code=sm_$(sm)))` 循环生成 SASS 选项；第 296 行 `HIGHEST_SM := $(lastword $(sort $(SMS)))` 取排序后最后一个词；第 298 行为其追加 PTX 选项。外层 `ifeq ($(GENCODE_FLAGS),)` 保证用户若手动传了 `GENCODE_FLAGS`，整段自动让位。

拆开这行 foreach：`foreach` 对 `$(SMS)` 的每个词绑定变量 `sm`，求值 `$(eval ...)`；`eval` 把它的参数当成 make 语法**立即执行**，于是一次 `+=` 被真实地执行了 8 次。`sort` 是字典序排序（`35 < 37 < 50 < ... < 75`），`lastword` 取尾——对两位数架构号这恰好等于数值最大。

**真正属于基准的部分：**

> [Conkernels/Makefile:320-326](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/Makefile#L320-L326)
> 编译规则把 `concurrentKernels.cu` 编成 `.o`（第 321 行，带 `$(INCLUDES)`）；链接规则生成可执行文件 `concurrentKernels`（第 324 行，**不带** `$(INCLUDES)`，因为链接期不需要头文件搜索路径），随后 `mkdir -p` 并 `cp` 到 `../../bin/$(TARGET_ARCH)/$(TARGET_OS)/$(BUILD_TYPE)/`。

> [Conkernels/Makefile:331-333](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/Makefile#L331-L333)
> `clean` 目标删除可执行文件与 `.o`，并递归删除安装目录下的副本；`clobber` 直接等于 `clean`。

注意安装目录也是 `../../` 开头——同样走出仓库（本仓库根下并没有 `bin/`，`make` 会在仓库**外**创建它）。这与 `-I../../Common` 是同一类"从 Samples 嵌套结构搬来未改"的痕迹，只不过 `mkdir -p` 会自动创建目录，所以它**能跑**，只是把产物丢在了意料之外的位置。

**模板的可扩展点：GSOverlap 与 TaskGraph 的差异化。** 同一份模板，两个基准各自只加了少量行，这正是模板的价值所在：

> [TaskGraph/Makefile:302](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/Makefile#L302)
> TaskGraph 加了 `LIBRARIES += -lcublas_static -lcublasLt_static -lcusparse_static -lculibos`，链接 cuBLAS/cuSPARSE 静态库（共轭梯度要用，见 u3-l4）。

> [GSOverlap/Makefile:285-311](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/Makefile#L285-L311)
> GSOverlap 加了一段 GCC 版本探测：把 `gcc -dumpversion` 的输出拼成三位数字，`expr` 比较是否 ≥ 500（即 5.0.0），不满足则置 `SAMPLE_ENABLED := 0`，触发 4.2.3 前述的 waive 机制。

> [GSOverlap/Makefile:335](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/Makefile#L335)
> GSOverlap 还加了 `ALL_CCFLAGS += --std=c++11`（memcpy_async 原语需要 C++11）。另在第 268-273 行用同样的 waive 机制声明"QNX 不支持本示例"。

**模板代际漂移。** 对比 `Shuffle/cuda_shuffle/Makefile` 与 `Conkernels/Makefile`，能看出它们来自 CUDA Samples 模板的**不同版本**：

> [Shuffle/cuda_shuffle/Makefile:75](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/Makefile#L75)
> 旧版模板的合法架构列表是 `x86_64 aarch64 ppc64le armv7l`——**没有 `sbsa`**（ARM Server Base System Architecture）。而新版（Conkernels/TaskGraph/GSOverlap 的第 75 行）加入了 `sbsa`，并新增了第 89-95 行的 aarch64/sbsa 判别逻辑。

版本身份的另一个旁证是行号漂移：同样的 `INCLUDES` 赋值，旧版在 [第 243 行](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/Makefile#L243)，新版在 [Conkernels 第 274 行](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/Makefile#L274)——旧版整体比新版少约 31 行。

**Shuffle Makefile 里的三处死目标。** 这份旧版模板被改成多文件构建时留下了明显的不一致：

> [Shuffle/cuda_shuffle/Makefile:289-296](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/Makefile#L289-L296)
> 链接规则是 `reduction.out: reduction.o reduction_kernel.o`，产物名是 **`reduction.out`**。而 [第 299 行](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/Makefile#L299) 的 `run:` 目标执行的是 `./reduction`，[第 302 行](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/Makefile#L302) 的 `clean` 删的也是 `reduction` 而非 `reduction.out`。

也就是说 `make run` 与 `make clean` 在 Shuffle 里都是坏的。它们没被修，因为作者的实验流程绕开了 Makefile 目标——`test.sh` 直接调用真实产物名：

> [Shuffle/cuda_shuffle/test.sh:1-4](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/test.sh#L1-L4)
> 四行命令全部执行 `./reduction.out n=<规模>`，与链接规则的产物名一致，与 Makefile 的 `run:`/`clean:` 不一致。

这给我们的通用教训是：**当仓库里同时存在 Makefile 和 shell 脚本两条入口时，要弄清哪条才是作者真正用的**——另一条往往早已失修。判断依据就是这种名字对不对得上的细节。

#### 4.2.4 代码实践

**实践目标**：不编译一行代码，仅用 `make -n`（dry-run）看清 SMS 如何变成 nvcc 选项，并体会 `?=` 的覆盖机制。

**操作步骤**：

```bash
cd Conkernels

# 1) 看 make 实际会执行的命令（默认 SMS）
make -n 2>&1 | grep -o "\-gencode [^ ]*" | sort | uniq -c

# 2) 命令行覆盖 SMS，再看一次
make -n SMS="75 80" 2>&1 | grep -o "\-gencode [^ ]*" | sort | uniq -c

# 3) 极端情况：SMS 置空，观察 waive
make -n SMS= 2>&1 | tail -5

# 4) 看 make 解析后的关键变量值（make 的内省技巧）
make -pn 2>/dev/null | grep -E "^(SMS|HIGHEST_SM|GENCODE_FLAGS|INCLUDES|NVCC) ?[:?]?="
```

**需要观察的现象**：

- 第 1 步应出现 8 个形如 `-gencode arch=compute_35,code=sm_35` 的 SASS 选项 + 1 个 `arch=compute_75,code=compute_75` 的 PTX 选项；
- 第 2 步应只剩 `sm_75`、`sm_80` 两份 SASS + `compute_80` 一份 PTX（注意 HIGHEST_SM 变成了 80）；
- 第 3 步应打印 `>>> WARNING - no SM architectures have been specified - waiving sample <<<`，且后续命令变成 `[@]` 占位；
- 第 4 步能直接看到 `GENCODE_FLAGS` 展开后的完整字符串。

**预期结果**：你会直观看到"改一个 SMS 变量 = 重排整个多架构产物矩阵"，以及默认配置下 GSOverlap 拿不到 sm_80 代码这一事实（可再 `cd ../GSOverlap && make -n | grep -c sm_80` 验证为 0）。

**待本地验证**：`make -n` 不调用编译器、不需要 CUDA，只依赖 `uname`/`getconf`/`expr` 等系统命令，在没有 GPU 的机器上也能跑。以上现象为本讲义编写时静态推导，具体输出请在本地核对。

#### 4.2.5 小练习与答案

**练习 1**：`SMS ?= 35 37 50 52 60 61 70 75` 与 `SMS = 35 ...` 一字之差，语义有何不同？这带来什么实用能力？

**答案**：`?=` 是条件赋值，只在变量**尚未定义**时才赋值；`=` 是无条件赋值。由于 make 的命令行变量优先级高于 Makefile 内的普通赋值，`?=` 使得 `make SMS=80` 能覆盖默认列表。若写成 `=`，命令行覆盖依然有效（命令行优先级仍更高），但 Makefile 内**后续若再有 `SMS = ...` 就会引发混淆**；NVIDIA 统一用 `?=` 明确表达"这是默认值，欢迎用户覆盖"。这正是 `make SMS="75 80"` 这类按需定制得以成立的基础。

**练习 2**：默认配置编译出的二进制能在 compute capability 8.6 的 GPU（如 RTX 30 系）上运行吗？为什么？

**答案**：能，但走 JIT 路径。二进制里嵌的最高架构 SASS 是 sm_75，但还含一份 `arch=compute_75,code=compute_75` 的 PTX。驱动会在 8.6 设备上把这份 PTX 即时编译成 sm_86 SASS。代价是首次加载多一步编译，且 JIT 产物质量可能不如为 8.6 原生编译的代码。若追求最优，应 `make SMS="86"` 显式生成。

**练习 3**：`$(lastword $(sort $(SMS)))` 用字典序取最大值，在什么情况下会出错？

**答案**：当架构号位数不一致时。例如 `SMS = "35 100"`，字典序是 `"100" < "35"`（逐字符比较，`'1' < '3'`），`lastword` 会取到 `35` 而不是 `100`，于是 PTX 会为 compute_35 生成——向前兼容性受损（在 sm_100 上只能从更老的 PTX JIT）。当前两位数架构号（35–90）下字典序恰好与数值序一致，所以没出问题；这是依赖输入范围的隐含假设。

---

### 4.3 依赖最小化：把 Common 从整个目录剥到 6 个文件

#### 4.3.1 概念说明

"依赖最小化"（dependency minimization）指在保持行为完全不变的前提下，把项目携带的外部代码裁剪到最小必需集合。动机通常有三个：

1. **可读性**：读者不必在 20 个顶层条目里猜哪 6 个是活的；
2. **可移植性**：少一个目录依赖，就少一种"换个环境路径就断"的失败模式（Conkernels 的 `-I../../Common` 正是反例）；
3. **安全性**：无人引用的代码是**不会被审计到的代码**。`FreeImage/`（一个完整的第三方图像库副本）与 `nvrtc_helper.h`（涉及运行时编译 NVRTC）这类行李留在仓库里，长期看是负担。

最小化的核心方法论只有一句话：**从 include 闭包出发，而不是从目录出发**。闭包在 4.1.3 已经算出来了，是 6 个文件。这个方法的可贵之处在于它是**可机械验证**的——删完之后再编译一次，能过就是对的，不存在"也许还用到"的模糊地带（对 header-only 依赖而言）。

#### 4.3.2 核心流程

最小化改造的标准流程：

```
① 建立 include 闭包（4.1.3 已完成）→ 6 个头文件
② 选定改造对象，确认其 -I 路径与闭包的对应关系
③ 写新的最小 Makefile：
     - 保留：NVCC 定义、多架构 GENCODE（若需要）、链接库
     - 删除：整个跨平台探测层（若你只在 x86_64 Linux 上跑）
     - 改写：INCLUDES 指向闭包所在目录（或干脆删掉，见下）
④ 三重验证：
     a. 编译通过（最强证据：闭包算对了）
     b. 运行输出与改造前逐字节一致（设备探测行、checksum、时间量级）
     c. md5sum 对比二进制（可选，编译选项不变时应当相同）
```

关键决策点：**改造对象选 Conkernels 时，闭包是空集**。因为 `concurrentKernels.cu` 不 include 任何 helper 头，所以"最小化"不是"拷 6 个头过来"，而是"把 `-I../../Common` 这行删掉，Common 从此与 Conkernels 无关"。这是本任务最漂亮的部分——它把"依赖"这个概念逼到了零点。

#### 4.3.3 源码精读

先明确要删除/保留的东西在新 Makefile 里的去留依据：

**要删的：**

> [Conkernels/Makefile:273-275](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/Makefile#L273-L275)
> 这三行里的 `INCLUDES := -I../../Common` 是唯一一处与 Common 的联系。由于源文件不 include 任何 helper 头，删除它不影响任何编译行为。

> [Conkernels/Makefile:37-70](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/Makefile#L37-L70)
> 废弃变量兼容垫片（`x86_64=1`、`ARMv7=1`、`GCC=` 等旧接口）。本项目从不用这些变量名，整段可删。

> [Conkernels/Makefile:115-158](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/Makefile#L115-L158)
> 交叉编译场景下挑选 `arm-linux-gnueabihf-g++`、QNX 工具链、Android 工具链的整段逻辑。只在 x86_64 Linux 上开发时，最终都会落到第 159 行的 `HOST_COMPILER ?= g++`，故可整段折叠为一行。

**要保留的（这是容易删过头的地方）：**

> [Conkernels/Makefile:159-160](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/Makefile#L159-L160)
> `HOST_COMPILER ?= g++` 与 `NVCC := $(CUDA_PATH)/bin/nvcc -ccbin $(HOST_COMPILER)`——nvcc 借助 `-ccbin` 指定宿主编译器，这是 host 端 C++ 代码真正被谁编译的关键，必须保留（或至少保留 `-ccbin`）。

> [Conkernels/Makefile:252-258](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/Makefile#L252-L258)
> `dbg=1` 分支追加 `-g -G` 并把 `BUILD_TYPE` 设为 debug。u3-l1 讲过 `-G` 关闭 GPU 优化、只用于保真调试；保留这个开关便于复现教学演示，删掉它也不影响 release 构建。

> [Conkernels/Makefile:291-300](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/Makefile#L291-L300)
> SMS/GENCODE 机制。Conkernels 用了 `clock()` 与 stream，任何架构都行，但保留它可以继续用 `make SMS=..` 控制产物架构矩阵，也保留向前兼容的 PTX。

据此给出最小 Makefile（**示例代码**，非仓库原有内容，请存为 `Conkernels/Makefile.standalone`，不要覆盖原文件）：

```makefile
# ---- 示例代码：Conkernels 的独立最小 Makefile ----
# 前提：只在 x86_64/aarch64 Linux 上构建；去掉了全部跨平台探测与 Common 依赖
CUDA_PATH   ?= /usr/local/cuda
HOST_COMPILER ?= g++
NVCC        := $(CUDA_PATH)/bin/nvcc -ccbin $(HOST_COMPILER)

TARGET_SIZE := $(shell getconf LONG_BIT)
BUILD_TYPE  := release
ifeq ($(dbg),1)
        NVCCFLAGS += -g -G
        BUILD_TYPE := debug
endif

# 多架构生成（保留自原模板 L291-300，逻辑未改）
SMS ?= 35 37 50 52 60 61 70 75
ifeq ($(SMS),)
$(info >>> WARNING - no SM architectures specified <<<)
endif
$(foreach sm,$(SMS),$(eval GENCODE_FLAGS += -gencode arch=compute_$(sm),code=sm_$(sm)))
HIGHEST_SM := $(lastword $(sort $(SMS)))
ifneq ($(HIGHEST_SM),)
GENCODE_FLAGS += -gencode arch=compute_$(HIGHEST_SM),code=compute_$(HIGHEST_SM)
endif

ALL_CCFLAGS := -m$(TARGET_SIZE) $(NVCCFLAGS)
# 注意：没有 INCLUDES——concurrentKernels.cu 不 include 任何 helper 头

all: concurrentKernels

concurrentKernels.o: concurrentKernels.cu
	$(NVCC) $(ALL_CCFLAGS) $(GENCODE_FLAGS) -o $@ -c $<

concurrentKernels: concurrentKernels.o
	$(NVCC) $(ALL_CCFLAGS) $(GENCODE_FLAGS) -o $@ $+

run: all
	./concurrentKernels

clean:
	rm -f concurrentKernels concurrentKernels.o
```

12 行实质内容替代 336 行。如果你要做的是 **TaskGraph** 的剥离（它闭包非空），则把上例的注释处改成：

```makefile
INCLUDES := -I../Common      # 并在 Common/ 里只保留 6 个闭包文件
LIBRARIES += -lcublas_static -lcublasLt_static -lcusparse_static -lculibos
```

并在编译规则的 `$(ALL_CCFLAGS)` 前加 `$(INCLUDES)`。此时最小化还有第二步：把 `Common/` 里那 6 个文件拷到 `TaskGraph/Common-min/`（或直接原地删除其余 11 个顶层代码文件加两个子目录——但这会影响 Shuffle，需一并评估），使 `Common` 彻底成为基准的私有依赖。

**为什么闭包法对 header-only 依赖是完备的**：C/C++ 的头文件依赖只有 `#include` 这一种显式入口，不存在运行时动态加载（`.so` 那种才需要额外排查 `ldd`）。因此"编译通过"就是充分证据。唯一的暗礁是**宏触发的条件 include**——比如 `helper_cuda.h` 第 585 行那种依赖外部宏的分支。本仓库的 6 个闭包文件中，`helper_cuda.h` 的条件块（`__DRIVER_TYPES_H__`、`CUDA_DRIVER_API`、`CUBLAS_API_H_` 等）都是"按需启用额外功能"而非"按需引入额外文件"，所以闭包稳定。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：完成一次可验证的外部依赖剥离——证明 Conkernels 与 Common 之间**没有任何真实依赖**，并用一个不引用 `../../Common` 的独立 Makefile 复现出行为一致的可执行文件。

**操作步骤**：

1. **建立证据链**（静态部分，不需要 GPU）：

   ```bash
   cd Conkernels
   # a) 源文件不含任何 helper include
   grep -n "helper_" concurrentKernels.cu || echo "OK: 无 helper 依赖"
   # b) 全部 include 列表
   grep -n "#include" concurrentKernels.cu
   # c) Makefile 中所有出现 Common 的行
   grep -n "Common" Makefile Makefile_serialized
   ```

   预期：a 输出 `OK: 无 helper 依赖`；b 只有 4 个标准头；c 各命中 1 行（第 274 行的 `INCLUDES`）。

2. **写独立 Makefile**：把 4.3.3 的示例代码保存为 `Conkernels/Makefile.standalone`（注意 make 的 recipe 行必须用 **Tab** 缩进，不是空格）。

3. **对照编译**：

   ```bash
   make clean && make                            # 原版
   mv concurrentKernels concurrentKernels.orig
   make -f Makefile.standalone clean 2>/dev/null; make -f Makefile.standalone
   ```

4. **验证行为一致**（需要 NVIDIA GPU，无 GPU 则跳到第 5 步）：

   ```bash
   ./concurrentKernels.orig > out.orig.txt 2>&1
   ./concurrentKernels       > out.new.txt  2>&1
   diff out.orig.txt out.new.txt && echo "行为一致"
   ```

5. **无 GPU 环境的替代验证**（源码阅读型）：用 `cuobjdump --list-elf` 与 `--list-ptx` 分别检查两个二进制内嵌的目标码架构列表是否一致：

   ```bash
   cuobjdump --list-elf concurrentKernels.orig | sort | uniq -c
   cuobjdump --list-elf concurrentKernels       | sort | uniq -c
   ```

**需要观察的现象**：

- 第 3 步两个 make 都应成功，且新 Makefile 的命令行明显更短（没有 mkdir/cp 到 `../../bin`）；
- 第 4 步 diff 应为空（时间数值会有正常抖动，可用 `grep -v "time"` 过滤后再比，或只比非计时行）；
- 第 5 步两份 `--list-elf` 输出应完全相同：8 个 `sm_XX` + 1 份 PTX（如果你没改 SMS）。

**预期结果**：Conkernels 的可执行文件在去掉 Common 依赖后行为不变。这个结论如果成立，就同时证明了三件事：① 4.1 的依赖表是对的；② include 闭包法是可靠的；③ 原模板里的 `-I../../Common` 是死配置。

**待本地验证**：本环境未安装 nvcc，上述编译与运行步骤均未实际执行，标注为待本地验证。第 1 步的静态证据链已在编写本讲义时执行并确认。

#### 4.3.5 小练习与答案

**练习 1**：如果改造对象换成 TaskGraph，最小闭包是几个文件？为什么和 Conkernels 不同？

**答案**：6 个：`helper_cuda.h`、`helper_functions.h`、`helper_string.h`、`helper_image.h`、`helper_timer.h`、`exception.h`。TaskGraph 在 [第 46-47 行](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/conjugateGradientCudaGraphs.cu#L46-L47) include 了 `helper_cuda.h`（拉进 `helper_string.h`）与伞形头 `helper_functions.h`（拉进其余四个）。进一步的理论极限是 2 个——TaskGraph 只调用 `checkCudaErrors` 与 `findCudaErrors`/`findCudaDevice`，把第 47 行的 `helper_functions.h` 换成直接 include `helper_string.h` 即可甩掉 image/timer/exception 三个；但这要改源码，超出了"只动构建系统"的最小化范畴。

**练习 2**：为什么"编译通过"就足以证明 header-only 依赖剥离是完备的，而对动态库依赖却不成立？

**答案**：header-only 依赖的全部消费发生在编译期——预处理器的 `#include` 是唯一入口，链接期不产生任何对该目录的引用。所以编译（含链接）通过 = 没有遗漏。动态库则不同：`dlopen("libfoo.so")` 这类运行时加载在编译期完全不可见，必须运行（或 `ldd`/`strings`）才能发现；即便静态链接，`--no-as-needed` 之类选项也可能保留未使用的依赖。两者需要的验证手段不同。

**练习 3**：仓库里 `Common/FreeImage/` 带了一份第三方图像库的许可证文件（`freeimage-license.txt`）却无人引用。除了删掉它，从工程治理角度还有什么值得做的事？

**答案**：至少三件：① **盘点**——用 4.1.4 的 grep 方法生成一份"无人引用文件清单"，作为 PR 附件，让删除有据可依；② **上溯**——确认这些行李是否还带来合规义务（FreeImage 的许可证即使代码未被编译，一旦在仓库里分发，通常也要求随附许可文本，删除反而简化合规）；③ **防再生**——如果后续有 CI，可以加一条检查"Common/ 中的文件必须被至少一个编译单元 include，否则告警"，从机制上阻止行李重新积累。核心思想：最小化不只是删文件，而是建立一套让依赖持续可见的流程。

---

## 5. 综合实践

**任务：为整个仓库写一份《Common 依赖审计报告》，并完成两处最小化改造。**

把本讲三个模块的方法串起来用一遍：

1. **审计（对应 4.1）**：用 grep 建立全仓库的 helper include 表（基准 × 头文件 × Makefile 路径 × 实际解析位置），标出三处与 `Common/README.md` 不符的事实：
   - README 列了 ConKernels，但它零依赖；
   - README 没列 Shuffle，但它是最重的用户（连计时器都用）；
   - `Conkernels/Makefile:274` 的路径解析到仓库之外，属死配置。

2. **机制验证（对应 4.2）**：用 `make -n` 与 `make -pn` 记录 Conkernels 默认与 `SMS="75 80"` 两种配置下的 `GENCODE_FLAGS` 展开结果，再用 `cuobjdump --list-elf`（若能编译）核对二进制内嵌架构列表与展开结果一一对应。顺手确认 GSOverlap 默认配置不含 sm_80。

3. **改造（对应 4.3）**：
   - 给 Conkernels 写 `Makefile.standalone`（零 Common 依赖），按 4.3.4 的三重验证收尾；
   - 进阶：给 TaskGraph 写 `Makefile.standalone`，把 6 文件闭包拷贝到 `TaskGraph/include-min/`，`INCLUDES := -Iinclude-min`，保留 `-lcublas_static` 等四个库，验证 `conjugateGradientCudaGraphs` 输出（残差收敛序列与迭代次数）不变。

4. **报告**：用一页 Markdown 总结——结论表、验证命令、验证结果、遗留风险（例如删除共享 `Common/` 中文件会波及 Shuffle，需评估）。要求每个结论都附上可复现的命令，做到"读者跑一遍就能得到同样的表"。

这个实践的价值在于它训练的是**可迁移能力**：任何接手遗留代码的人，第一件事都该是分辨"哪些代码是本项目的，哪些是带上来的，哪些是根本没人用的"——而答案永远在 `#include` 和调用点里，不在 README 里。

## 6. 本讲小结

- **`Common/` 是 CUDA Samples 公共头的本地快照，README 的说法与实际代码有三处偏差**：ConKernels 实际零依赖、Shuffle 是被遗漏的第四个用户、且 `Conkernels/Makefile:274` 的 `-I../../Common` 多写一级指向仓库之外——它没引发故障的唯一原因是源文件根本不 include 任何 helper 头，属于"两个错误互相抵消"的双重休眠缺陷。
- **判断依赖要看 `#include`，不看 `-I`**：`-I` 只是注册搜索路径，不产生依赖。由此得到的方法论是从 include 闭包出发做依赖分析，而不是从目录或文档出发；对 header-only 依赖，"编译通过"就是完备性证明。
- **真正的闭包只有 6 个头**（`helper_cuda.h`、`helper_functions.h`、`helper_string.h`、`helper_image.h`、`helper_timer.h`、`exception.h`），Common 其余 11 个顶层代码文件与 `UtilNPP/`、`FreeImage/` 两个子目录在本仓库中零引用，是整目录拷贝带来的行李。
- **Samples Makefile 是分层巨石模板**：336 行里只有最后约 30 行属于基准本身；`SMS ?=` + `$(foreach … $(eval …))` + `HIGHEST_SM` 三件套批量生成 N 份 SASS 加 1 份最高架构 PTX，`make SMS=80` 可命令行覆盖（GSOverlap 测真异步必须这样指定）；`SAMPLE_ENABLED=0` 时把 `EXEC` 重定义为 `@echo "[@]"` 实现优雅 waive。
- **模板存在代际漂移与失修**：Shuffle 用的是没有 `sbsa` 的旧版模板，且其 `run:`/`clean:` 目标与链接产物名 `reduction.out` 不一致——`make run` 是坏的，作者实际靠 `test.sh` 直接调 `./reduction.out`。仓库里同时存在 Makefile 与 shell 脚本两条入口时，要弄清哪条才是真正在用的。
- **最小化改造的完整闭环是"闭包计算 → 最小构建脚本 → 编译/运行/二进制三重验证"**：对 Conkernels 这个闭包为空集的对象，最小化就是删掉那行 include 路径，让 Common 与它彻底无关。

## 7. 下一步学习建议

- **u6-l4（实验方法论与结果解读）** 是本单元的收官讲，会把视角从"构建工程"转向"测量工程"：warmup 的必要性、计时口径、跨平台（Carina/Fornax）结果的读法。本讲 4.2 里"`make SMS=` 改变产物架构矩阵"的知识在那里会直接用到——不同架构的产物本身就构成跨平台差异来源之一。
- **深入 CUDA Samples 原仓库**：把本仓库的 `Common/helper_cuda.h` 与 NVIDIA `cuda-samples` 仓库的 `Common/` 逐文件 diff，亲眼确认"本地快照落后了几个版本"，并观察 NVIDIA 后来如何把 Samples 迁移到 CMake（弃用了本讲分析的这套 make 模板）。
- **阅读一个反例**：回到 `CoMem_AXPY/Makefile`（u1-l2 讲过的单行式），对比本讲的 336 行模板，思考"什么时候值得引入模板、什么时候一行就够"。这个取舍判断是构建系统设计的核心品味。
- **延伸阅读方向**：CUDA Toolkit 文档中 *CUDA C Programming Guide* 的 "Compilation" 一节（PTX/SASS 与 `-gencode` 的官方语义）；以及现代 CUDA（11.4+）中 `__rdc=true` 与 `cuda::experimental::stf`、CMake 的 `CUDA_ARCHITECTURES` 属性——它们分别是本讲 SMS 机制在新时代的替代品。

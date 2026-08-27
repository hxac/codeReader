# 环境准备、编译与运行：Makefile 与 test.sh

## 1. 本讲目标

上一讲（u1-l1）我们画好了 CUDAMicroBench 的「项目地图」：14 个微基准、每个目录自成一体。本讲解决下一个自然的问题——**怎么把这些代码真正跑起来**。学完本讲，你应该能够：

1. 用 `nvcc --version`、`nvidia-smi` 等命令确认本机的 CUDA 工具链版本与 GPU 计算能力，并判断哪些基准可以在本机编译、运行。
2. 不借助任何框架，仅靠 `make` 和 `./可执行文件 参数` 就能独立编译并运行一个基准（以 CoMem_AXPY 为例）。
3. 读懂仓库中四种风格的 Makefile，并理解 `sm_30`、`-g`、`-G`、`-rdc=true`、`--cudart=shared` 这些编译选项分别做什么、什么时候会成为「坑」。
4. 理解 `test.sh` 的工作方式：用多个数据规模运行程序，并用 `nvprof`（含 `--metrics`）采集性能指标。

本讲有一个好消息：**编译 CUDA 程序不需要 GPU**。`nvcc` 交叉编译出的是 GPU 可执行码，编译过程本身在纯 CPU 机器上即可完成。所以即使你手头没有 NVIDIA GPU，本讲的编译类实践依然可以做；只有「运行基准」这一步必须有 GPU（或使用云端 GPU）。

## 2. 前置知识

本讲需要一点背景概念，都用一句话讲清楚：

- **host 与 device**：CUDA 程序分两层。CPU + 主机内存叫 host；GPU + 显存叫 device。对应到代码上，普通的 `.c` 文件是 host 代码，`.cu` 文件里用 `__global__` 等关键字标记的函数是要在 GPU 上执行的 kernel（上一讲已见）。
- **nvcc**：NVIDIA 提供的编译器**驱动**（compiler driver）。你调用 `nvcc`，它在背后调度 host 编译器（gcc/g++）和 device 编译器，把 `.c` 与 `.cu` 一起编译并链接成**一个**可执行文件。所以它长得像 gcc，但干的活是「一次管两种芯片」。
- **计算能力（compute capability）**：NVIDIA 给每代 GPU 的编号，如 `sm_30`（Kepler 架构）、`sm_75`（Turing）、`sm_86`（Ampere，如 RTX 30 系列）。编号越大架构越新。`-arch=sm_XX` 告诉 nvcc「为哪种架构生成机器码」——这直接决定了编译产物能在哪块 GPU 上运行。
- **make 与 Makefile**：`make` 是一个构建工具，读入名为 `Makefile` 的规则文件。最基本的规则是：

  ```makefile
  目标: 依赖
  <TAB>命令
  ```

  `make` 默认执行文件里**第一条**规则；如果目标没有依赖（或依赖比目标新），就直接执行命令。Makefile 本身只是「把命令记下来」，理解了这一点，下面看到的一行式 Makefile 就毫不神秘。
- **nvprof**：CUDA 自带的命令行性能分析器（profiler）。在任何可执行文件前加 `nvprof` 前缀运行，它就会打印每个 kernel、每次内存拷贝的耗时；`--metrics 指标名` 可以额外打印特定指标（本仓库用到了 `branch_efficiency`，分支效率）。注意：nvprof 在 CUDA 11 中已标记废弃、CUDA 12 起被移除，替代品是 Nsight Systems（`nsys`）与 Nsight Compute（`ncu`）。本仓库的 `test.sh` 写于 CUDA 11 时代，全部使用 nvprof；若你的环境没有 nvprof，见 4.4 节的替代方案。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md) | `Prerequisite` 与 `Experiment` 两节说明软硬件要求与「看 Makefile、跑 .sh」的使用约定 |
| [CoMem_AXPY/Makefile](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/Makefile) | 最简风格：一行 nvcc 命令，本讲的主实践对象 |
| [CoMem_AXPY/test.sh](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/test.sh) | 用 4 个数据规模 + nvprof 运行 axpy_cuda |
| [CoMem_AXPY/axpy_cuda.c](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c) | host 主程序：解析命令行参数、计时、打印 checksum 与 time |
| [CoMem_AXPY/axpy_cudakernel.cu](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu) | device 侧 kernel 与显存管理，被 nvcc 按 C++ 规则编译 |
| [CoMem_AXPY/axpy.h](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy.h) | 两个文件之间的接口声明，含 `extern "C"` 链接说明 |
| [WarpDivRedux/Makefile](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/Makefile) | 调试风格：`-g -G -arch=sm_30`，是「架构过时」问题的典型样本 |
| [WarpDivRedux/test.sh](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/test.sh) | `nvprof` 与 `nvprof --metrics branch_efficiency` 成对运行的范例 |
| [DynParallel/Makefile](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/DynParallel/Makefile) | 变量式多目标风格：`-arch=sm_86 --cudart=shared -rdc=true` |
| [Conkernels/Makefile](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/Makefile) | CUDA Samples 官方风格：`SMS` 列表 + `GENCODE_FLAGS` 多架构生成（本讲只做概览） |
| [MemAlign/Makefile](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/Makefile)、[BankRedux/Makefile](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/Makefile)、[UniMem/Makefile](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/Makefile)、[Shmem/Makefile](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shmem/Makefile) | 其余单行/简单风格 Makefile，用于汇总「全仓库编译速查表」 |

## 4. 核心概念与源码讲解

### 4.1 nvcc 编译：一条命令把 host 代码与 device 代码变成一个可执行文件

#### 4.1.1 概念说明

CUDAMicroBench 没有顶层构建脚本，每个基准的构建就是一个 nvcc 调用。理解这个调用，就理解了整个项目的构建方式。

以 CoMem_AXPY 为例，它有三个源文件，职责严格分离：

- `axpy_cuda.c`——host 主程序（纯 C）：分配内存、初始化数据、调用 CUDA 版本的 `axpy_cuda`、计时、校验。
- `axpy_cudakernel.cu`——device 侧代码：`cudaMalloc`/`cudaMemcpy`、`__global__` kernel 定义、启动 kernel。
- `axpy.h`——两者的接口契约：声明 `axpy_cuda` 函数原型。

`nvcc -o axpy_cuda axpy_cuda.c axpy_cudakernel.cu` 一条命令完成：`.c` 文件交给 host 编译器按 **C** 编译；`.cu` 文件由 nvcc 前端按 **C++** 处理，拆出其中的 host 部分与 device 部分（device 部分编译成 GPU 机器码，host 部分生成 kernel 启动桩）；最后链接成一个既含 CPU 代码又含 GPU 代码的可执行文件。

这里藏着一个所有「.c + .cu 混编」项目都会遇到的经典问题——**链接符号**。C++ 编译器会对函数名做名字改编（name mangling），C 编译器不会。如果 `axpy_cuda.c`（C）里调用 `axpy_cuda`（在 `.cu` 里按 C++ 编译），两边对同一个函数名的符号编码不一致，链接会报 undefined reference。解决办法就在 `axpy.h` 里：用 `extern "C"` 强制该函数按 C 方式链接。

#### 4.1.2 核心流程

一次 `make`（内部调用 nvcc）的完整流程：

```text
make                         # 读 Makefile，执行 default 规则
  └─ nvcc -o axpy_cuda axpy_cuda.c axpy_cudakernel.cu
       ├─ axpy_cuda.c        ──(host 编译器, C)──►  主程序对象
       ├─ axpy_cudakernel.cu ──(nvcc 前端, C++)
       │     ├─ host 部分（cudaMalloc 等）    ──►  对象代码
       │     └─ device 部分（__global__ 函数）──►  GPU 机器码(SASS)/中间码(PTX)
       └─ 链接 ──► axpy_cuda（一个可执行文件，CPU+GPU 代码都在里面）

./axpy_cuda 1024000          # 运行：需要本机有 NVIDIA GPU + 驱动
  └─ main → axpy_cuda(...) → kernel 上 GPU 执行 → check → printf
```

编译期需要的是 **CUDA 工具链**（nvcc 等）；运行期需要的是 **GPU 硬件 + 驱动**。两者是独立的——这就是「无 GPU 也能编译」的原因。

nvcc 常用选项速查（本仓库实际用到的加粗）：

| 选项 | 含义 | 出现在 |
| --- | --- | --- |
| `-o 名字` | 指定输出可执行文件名 | 全部基准 |
| **`-g`** | 生成 **host**（CPU）侧调试信息 | WarpDivRedux、MemAlign、DynParallel |
| **`-G`** | 生成 **device**（GPU）侧调试信息，同时**关闭 GPU 侧绝大多数优化** | WarpDivRedux、MemAlign、DynParallel |
| **`-arch=sm_XX`** | 只为计算能力 X.X 的架构生成代码 | WarpDivRedux/MemAlign（sm_30）、DynParallel（sm_86） |
| **`-rdc=true`** | 生成可重定位设备代码，**动态并行（设备端再启动 kernel）的必需前提** | DynParallel |
| **`--cudart=shared`** | 动态链接 CUDA runtime 库，而不是静态打入可执行文件 | DynParallel |
| `-Xcompiler 选项` | 把后面的选项转交给 host 编译器（如 `-fopenmp`） | DynParallel |
| `-lpng` | 链接 libpng 库（读写 PNG 图片） | DynParallel |

> ⚠️ 两个最值得记住的「坑」：
>
> 1. **`-G` 会毁掉性能数字**。它为了可调试关闭了 GPU 优化。WarpDivRedux、MemAlign 的 Makefile 都带着 `-g -G`，用它们直接做性能对比时要意识到测到的不是优化后的速度（这是设计者刻意为之还是疏忽，留给你判断；做性能实验时建议去掉 `-G` 重新编译）。
> 2. **`sm_30` 已经是历史**。`sm_30` 对应 2012 年的 Kepler 架构。CUDA 11 起的工具链已不再接受 `compute_30`（会直接报 `Unsupported gpu architecture`），需要把它改成你 GPU 对应的架构（见 4.4 节）。

#### 4.1.3 源码精读

**（1）最简 Makefile**——[CoMem_AXPY/Makefile:L1-L2](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/Makefile#L1-L2)：整个文件只有一条规则，目标叫 `default`（make 默认执行第一条规则），没有依赖、没有 `-arch`。nvcc 会用它自己随版本变化的默认架构编译。

**（2）`extern "C"` 的接口契约**——[CoMem_AXPY/axpy.h:L6-L14](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy.h#L6-L14)：`axpy.h` 定义 `REAL` 宏（本例是 `double`），并用 `#ifdef __cplusplus extern "C"` 包住 `axpy_cuda` 的声明。`.cu` 文件按 C++ 编译时宏生效，保证函数名不被改编，`.c` 文件才能链接到它。

**（3）`.cu` 文件里的 kernel**——[CoMem_AXPY/axpy_cudakernel.cu:L8-L22](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L8-L22)：`__global__ void axpy_cudakernel_warmingup(...)` 与 `axpy_cudakernel_1perThread(...)` 就是 nvcc 负责编译成 GPU 代码的部分。kernel 细节是 u2-l1 的主题，这里只需知道「它们被编译进同一个可执行文件」。

**（4）带架构与调试选项的 Makefile**——[WarpDivRedux/Makefile:L1-L2](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/Makefile#L1-L2)：同样是两行，但加了 `-g -G -arch=sm_30`。[MemAlign/Makefile:L1-L2](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/Makefile#L1-L2) 与它完全同构。

**（5）动态并行的编译要求**——[DynParallel/Makefile:L1-L9](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/DynParallel/Makefile#L1-L9)：用变量组织构建。`CUDAFLAGS` 一行集中了 `-arch=sm_86 --cudart=shared -rdc=true -Xcompiler -fopenmp -lpng`——其中 `-rdc=true` 是因为该基准要让 **GPU 在运行时再启动下一层 kernel**（动态并行，u3-l2 详述），这种嵌套调用必须用可重定位设备代码；`-lpng` 则是因为它的源文件 [DynParallel/Dynamic_Parallelism.cu:L12](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/DynParallel/Dynamic_Parallelism.cu#L12) `#include <png.h>`，要把 Mandelbrot 图写成 PNG 图片。

#### 4.1.4 代码实践

**实践一：手工重放编译命令（无 GPU 也可完成）**

1. **实践目标**：不通过 make，直接用 nvcc 编译 CoMem_AXPY，体会「Makefile 只是记录命令的便签」。
2. **操作步骤**：
   ```bash
   cd CoMem_AXPY
   nvcc --version                     # 记录 CUDA 工具链版本
   nvcc -o axpy_cuda axpy_cuda.c axpy_cudakernel.cu     # 手工编译
   ls -l axpy_cuda                    # 确认产物生成
   ```
3. **需要观察的现象**：`nvcc --version` 末尾会打印类似 `Cuda compilation tools, release X.Y` 的版本行；编译成功后当前目录出现可执行文件 `axpy_cuda`。
4. **预期结果**：在装有 CUDA 工具链的机器上（无论有无 GPU）编译成功。随后可尝试删掉一个源文件再编译：
   ```bash
   nvcc -o axpy_cuda axpy_cudakernel.cu    # 只编译 .cu
   ```
   会在链接阶段报 `undefined reference to axpy_cuda` 之类的错误——因为 `main` 定义在被你省略的 `.c` 文件里。
5. 本讲所有运行结果均为「待本地验证」：作者写作环境无法代你执行，请以你的机器输出为准。

**实践二：亲眼看一次 `extern "C"` 的作用（可选，无 GPU 可完成）**

1. **实践目标**：验证 4.1.1 中关于链接符号的说法。
2. **操作步骤**：把 `axpy.h` 中 `#ifdef __cplusplus` / `extern "C" {` / `}` 三行临时注释掉（改完记得还原），重新 `nvcc -o axpy_cuda axpy_cuda.c axpy_cudakernel.cu`。
3. **需要观察的现象**：链接错误，提示找不到 `axpy_cuda`（C++ 改编后的名字与 C 名字对不上）。
4. **预期结果**：报错信息形如 `undefined reference to 'axpy_cuda(double*, double*, int, double)'`——报错里裸露的 C 风格签名正是问题所在。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`-g` 和 `-G` 有什么区别？为什么带 `-G` 编译出的基准不适合直接拿去做性能结论？

**答案**：`-g` 生成 CPU 侧调试信息，`-G` 生成 GPU 侧调试信息。`-G` 的代价是关闭 GPU 代码的大多数优化，kernel 跑得比正式构建慢得多，测出来的时间不能代表真实性能；做性能对比应当去掉 `-G` 重新编译。

**练习 2**：为什么 `axpy.h` 需要 `extern "C"`，而 `axpy_cuda.c` 里不需要写任何特殊标记？

**答案**：`axpy_cudakernel.cu` 由 nvcc 按 C++ 规则编译，C++ 会对函数名做名字改编；`axpy_cuda.c` 按 C 编译，不做改编。`extern "C"` 写在被 C++ 编译的那一侧（通过头文件引入），把 `axpy_cuda` 的符号钉在 C 编码上，两边才能链接成功。

**练习 3**：一个程序用 `-arch=sm_30` 编译，拿到一台 Ampere（sm_86）GPU 的机器上运行，会发生什么？

**答案**：`sm_30` 的简写只为 Kepler 3.0 生成机器码，不包含可即时翻译的 PTX 中间码，因此在新架构 GPU 上启动 kernel 时会报 `no kernel image is available for execution on the device`。解决：用本机 GPU 对应的 `-arch=sm_86`（或相应值）重新编译。另外，在 CUDA 11 及以上的工具链里 `-arch=sm_30` 连编译都过不了（见 4.4 节）。

### 4.2 Makefile 规则：四种构建风格与全仓库编译速查

#### 4.2.1 概念说明

仓库里有 18 个 Makefile，但只有四种写法。分清这四种风格，任何目录你都知道 `make` 之后会发生什么：

- **风格 A：单行式（作者自写，占多数）**。两行 Makefile，`default:` 加一条 nvcc 命令。没有 `clean`，没有依赖声明，`make` 每次都重新编译。例如 CoMem_AXPY、BankRedux、UniMem、MiniTransfer_SpMV、CoMem_SpMM、ReadOnlyMem_1D/2D_Texture、HDOverlap。
- **风格 B：单行式 + 调试与架构选项**。与 A 同构，但带 `-g -G -arch=sm_30`，即 WarpDivRedux 与 MemAlign 两个目录。
- **风格 C：变量式多目标**。用 `NVCC`/`CUDAFLAGS`/`OPT` 变量组织，一次构建多个可执行文件、带 `clean` 规则，仅 DynParallel。
- **风格 D：CUDA Samples 官方风格**。三百多行的通用模板，自动探测平台、按 `SMS` 列表为多代架构生成代码（`GENCODE_FLAGS`），并引用 `../../Common` 下的头文件。覆盖 Conkernels（另有 `Makefile_serialized` 变体）、GSOverlap、TaskGraph、Shuffle/cuda_global、Shuffle/cuda_shuffle。

风格 D 来自 NVIDIA 官方样例（README 的 Common folder 一节说明这几个基准派生自 CUDA Samples），我们在此只认出它的结构即可，逐段精读放到 u6-l3。

#### 4.2.2 核心流程

拿到任一目录后的通用三步：

```text
1. cat Makefile            # 先看是哪种风格、生成什么可执行文件
2. make                    # 风格 A/B：直接成功
                           # 风格 C：默认目标 all，生成多个可执行文件
                           # 风格 D：默认目标 all → build → 具体可执行文件，
                           #          并把产物复制到 ../../bin/<arch>/<os>/<type>/
3. ./可执行文件 [参数]      # 参数含义看该目录的 test.sh 与 host 主程序的 argc 解析
```

全仓库编译速查表（风格列即上面 A/B/C/D；产物名为 make 生成的可执行文件）：

| 目录 | 风格 | make 后的产物 | 特殊注意 |
| --- | --- | --- | --- |
| CoMem_AXPY | A | `axpy_cuda` | 无 |
| BankRedux | A | `sum_cuda` | 无 |
| UniMem | A | `LowAccessDensityTest_cuda` | 只编译一个 `.cu` |
| HDOverlap | A | `axpy_cuda` | 只编译 `.cu`，host 主程序也在其中 |
| MiniTransfer_SpMV | A | `SpMV_cuda` | 无 |
| CoMem_SpMM | A | `SpMM_cuda` | 无 |
| ReadOnlyMem_1D_Texture | A | `axpy_cuda` | 无 |
| ReadOnlyMem_2D_Texture | A | `matadd_2D_cuda` | 无 |
| Shmem | A（略繁） | `mm_omp_cuda.out` | 规则带依赖与 `clean`，但仍是单条 nvcc 命令 |
| WarpDivRedux | B | `warpDivergenceTest_cuda` | `-g -G -arch=sm_30`，CUDA 11+ 需改架构 |
| MemAlign | B | `axpy_cuda` | 同上 |
| DynParallel | C | `Dynamic_Parallelism`、`Non_Dynamic_Parallelism` | 需 `-rdc=true`；需系统安装 libpng 开发包 |
| Conkernels | D | `concurrentKernels`（并复制到 `../../bin/...`） | 依赖 `../../Common`；`make dbg=1` 可切换调试构建 |
| GSOverlap | D | 多个矩阵乘可执行文件 | 依赖 `../../Common`；用到 CUDA 11 的 memcpy_async |
| TaskGraph | D | 共轭梯度样例可执行文件 | 依赖 `../../Common`；需要较新 CUDA（CUDA Graph） |
| Shuffle/cuda_global、Shuffle/cuda_shuffle | D | `reduction`（链接目标名为 `reduction.out`） | 依赖 `../../Common` |

#### 4.2.3 源码精读

**（1）风格 A 的样板**——[CoMem_AXPY/Makefile:L1-L2](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/Makefile#L1-L2)。注意它没有写任何依赖文件，所以 `make` 每次都会重新执行 nvcc；也没有 `clean` 目标，清理要手工 `rm axpy_cuda`。同类还有 [BankRedux/Makefile:L1-L2](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/Makefile#L1-L2)、[UniMem/Makefile:L1-L2](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/Makefile#L1-L2) 等。稍进一步的是 [Shmem/Makefile:L1-L7](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shmem/Makefile#L1-L7)：目标 `mm_omp_cuda` 声明了三个依赖文件（两个源文件加一个头文件），并提供了 `clean` 规则。

**（2）风格 B**——[WarpDivRedux/Makefile:L1-L2](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/Makefile#L1-L2)。与风格 A 唯一的区别是命令行多了 `-g -G -arch=sm_30`。

**（3）风格 C：变量化组织**——[DynParallel/Makefile:L1-L9](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/DynParallel/Makefile#L1-L9) 定义 `NVCC`、`CUDAFLAGS`（架构 + 动态并行相关选项）、`OPT`（`-g -G`），第一条目标 `all` 声明要生成 `Dynamic_Parallelism` 与 `Non_Dynamic_Parallelism` 两个可执行文件；[DynParallel/Makefile:L18-L26](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/DynParallel/Makefile#L18-L26) 是具体的编译与链接规则——先 `-c` 编出两个 `.o`，再分别链接成两个可执行文件；[DynParallel/Makefile:L28-L31](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/DynParallel/Makefile#L28-L31) 提供 `clean`。顺带一个读代码的小提醒：`.o` 规则的目标名只写了 `Dynamic_Parallelism.o`，但 recipe 里连续编译了两个文件——这类不规范但「能跑」的写法在真实科研代码里很常见，读 Makefile 时要按 recipe 实际做的事来理解，而不是只看目标名。

**（4）风格 D：多架构生成的核心片段**——[Conkernels/Makefile:L273-L275](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/Makefile#L273-L275) 声明 `INCLUDES := -I../../Common`，说明编译时必须保持仓库目录结构（头文件在上两级的 Common 里）；[Conkernels/Makefile:L280-L299](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/Makefile#L280-L299) 是它的灵魂：`SMS ?= 35 37 50 52 60 61 70 75` 列出要支持的架构，随后用 `foreach` 为每个 `sm` 拼出一条 `-gencode arch=compute_XX,code=sm_XX`，再为最高的架构额外生成 PTX（`code=compute_XX`）以保证前向兼容。也就是说，风格 D 的产物天然带着「多代 GPU 通吃」的保险，这是风格 A/B 做不到的。[Shuffle/cuda_shuffle/Makefile:L289-L296](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/Makefile#L289-L296) 展示了它的两步构建：`reduction.cpp` 与 `reduction_kernel.cu` 分别编译成 `.o` 再链接为 `reduction.out`。

**（5）README 中的使用约定**——[README.md:L107-L111](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md#L107-L111)：`Prerequisite` 一节说明需要 NVIDIA GPU 与 CUDA，且动态并行、memcpy_async 等新特性要求支持 CUDA 11 的 GPU；`Experiment` 一节给出的官方用法就是本讲的方法——「看每个目录的 Makefile 了解怎么编译，然后用 .sh 文件执行」。

#### 4.2.4 代码实践

**实践：从零编译并运行 CoMem_AXPY（本讲核心实践）**

1. **实践目标**：完整走一遍「进目录 → make → 运行 → 换参数再运行」，并记录输出中的 checksum 与 time。
2. **操作步骤**（需要 NVIDIA GPU 与已装好驱动/CUDA 的机器）：
   ```bash
   cd CoMem_AXPY
   make                          # 即 nvcc -o axpy_cuda axpy_cuda.c axpy_cudakernel.cu
   ./axpy_cuda 1024000           # 第一次运行，n = 1024000
   nvprof ./axpy_cuda 10240000   # 第二次运行，n = 10240000（test.sh 中的最大规模），
                                  # 顺便用 nvprof 包一层
   ```
3. **需要观察的现象**：程序每次都会先向 stderr 打印一行 `Usage: axpy <n>`（它是无条件打印的提示语），随后向 stdout 打印一行结果，格式由源码固定为 `axpy(n): checksum: ..., time: ...ms`；nvprof 版本还会额外输出一张 GPU activities 表（kernel 与 memcpy 的耗时列表）。
4. **预期结果**：两次运行各记录一行 `axpy(<n>): checksum: <值>, time: <值>ms`。对 checksum 的预期：本基准的 GPU 实现对每个元素独立执行 `y[i] += a*x[i]`，与串行版本逐元素的计算方式一致，理论上逐位相同，因此 checksum（误差比值，见 4.3.3）应接近甚至等于 0；time 一栏则应随 n 增大而增大，n=10240000（10 倍数据）通常明显慢于 n=1024000。具体数值**待本地验证**，请把你机器上的两次输出抄录下来。
5. **无 GPU 的替代做法**：完成 4.1.4 实践一（编译不需要 GPU），并记录环境检查过程（`nvcc --version`、`nvidia-smi`，见 4.4.4），然后跳到 u1-l4，用仓库自带的 `*.output.txt` 结果文件做「纸面分析」。

#### 4.2.5 小练习与答案

**练习 1**：在 CoMem_AXPY 目录里连续执行两次 `make`，第二次会发生什么？为什么？

**答案**：第二次仍然完整重新编译。因为 `default` 目标没有声明任何依赖文件，make 认为它永远「需要更新」，无条件执行 recipe。这也是风格 A/B Makefile 的共同行为。

**练习 2**：把 Conkernels 目录单独拷贝到一个新位置（不带上一级的 Common），`make` 会怎样？

**答案**：风格 D 的 Makefile 用 `-I../../Common` 查找 CUDA Samples 的 helper 头文件，目录被孤立后编译会在 `#include "helper_cuda.h"` 之类的地方报「找不到头文件」的致命错误。这类基准必须连同仓库的目录结构一起使用（详见 u6-l3 对 Common 依赖的分析）。

**练习 3**：仓库里哪几个目录的 Makefile 带 `-G`？如果有人直接用这些目录的默认构建发布性能结论，你应当提出什么质疑？

**答案**：WarpDivRedux、MemAlign（单行式里的 `-g -G`）以及 DynParallel（`OPT= -g -G`）。`-G` 关闭 GPU 侧优化，会系统性拖慢 kernel，因此默认构建测得的时间偏保守、不能代表优化后性能；应当要求去掉 `-G`（并按需指定正确 `-arch`）重新编译后再比较。

### 4.3 test.sh 脚本：多规模运行与 nvprof 指标采集

#### 4.3.1 概念说明

每个基准的「实验设计」不在文档里，而在 `test.sh` 里。它是普通的 shell 脚本（未必带可执行权限，用 `bash test.sh` 运行最稳妥），逐行列出作者想跑的命令组合，通常体现两件事：

1. **数据规模扫描**：同一个程序用从小到大的 n 跑多组，观察时间随规模的变化；
2. **profiler 采集**：用 `nvprof` 看耗时分布，用 `nvprof --metrics 某指标` 采集与该基准「性能故事」直接相关的指标。

也就是说，`test.sh` 就是这个微基准的**实验记录**：读它，你就知道作者关心什么量。

需要注意：仓库 18 个含 Makefile 的构建目录中有 6 个（CoMem_SpMM、Conkernels、DynParallel、GSOverlap、Shmem、TaskGraph）没有 test.sh，它们的运行方式要看各自源码的 `main` 或 README（如 Conkernels 目录下有自己的 README.md）；这 6 个多数源自 CUDA Samples，沿袭了样例「直接执行、不带参数」的习惯。

#### 4.3.2 核心流程

```text
bash test.sh
  ├─ nvprof ./axpy_cuda 1024000     ┐
  ├─ nvprof ./axpy_cuda 4096000     │ 规模逐级 ×4、×10、×20
  ├─ nvprof ./axpy_cuda 10240000    │ 每次运行：
  └─ nvprof ./axpy_cuda 20480000    ┘   argv[1] → n（元素个数）
                                        → malloc/init → 串行 axpy（参考答案）
                                        → 10 次 axpy_cuda 计平均 → check → printf
```

参数与输出的完整链路（以 CoMem_AXPY 为例）：

- 命令行参数 `1024000` 经 `argv[1]` 变成 `n`；不带参数时用源码里的默认值 `VEC_LEN 1024000`。
- 程序打印的 `time` 是 **10 次调用取平均的墙钟时间**（`read_timer_ms` 基于 `ftime`），它包住了 `axpy_cuda` 的全部工作：主机↔设备两次内存拷贝 + kernel 执行 + 同步。
- 程序打印的 `checksum` 是校验值：GPU 结果与串行结果的「差值绝对值之和 ÷ 串行结果绝对值之和」，越接近 0 越一致。

#### 4.3.3 源码精读

**（1）最简 test.sh**——[CoMem_AXPY/test.sh:L1-L4](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/test.sh#L1-L4)：四行命令，规模 1024000 → 4096000 → 10240000 → 20480000（×4、×10、×20 的放大），全部套 `nvprof`。这就是 4.2.4 实践里第二条命令 `nvprof ./axpy_cuda 10240000` 的出处——它正是脚本第三行。

**（2）带指标采集的 test.sh**——[WarpDivRedux/test.sh:L1-L10](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/test.sh#L1-L10)：每个规模跑两条——先 `nvprof ./warpDivergenceTest_cuda N` 看整体耗时，紧接着 `nvprof --metrics branch_efficiency ./warpDivergenceTest_cuda N` 采集分支效率指标。WarpDivRedux 的主题是 warp 分支发散（u3-l1），`branch_efficiency`（分支指令中未发散的比例，越高越好）正是量化它的指标——这就是「test.sh 的指标选择反映基准的性能故事」的直接例证。

**（3）参数解析与输出格式**——[CoMem_AXPY/axpy_cuda.c:L62-L72](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L62-L72)：`main` 里先无条件打印 Usage 提示，再在 `argc >= 2` 时用 `atoi(argv[1])` 覆盖默认的 `n`。所以「程序每次都打印 Usage」不是出错，而是这段代码的既定行为。

**（4）计时与校验**——[CoMem_AXPY/axpy_cuda.c:L84-L92](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L84-L92)：`num_runs = 10`，循环调用 10 次 `axpy_cuda` 用 [read_timer_ms（L14-L18）](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L14-L18)计时再除以 10；随后 `check(y_cuda, y, n)`（定义在 [L51-L60](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L51-L60)，返回 `diffsum/sum`）得到 checksum，最后 `printf("axpy(%d): checksum: %g, time: %0.2fms\n", ...)`——你在终端看到的那一行就来自这里。计时口径的细节（为什么包住拷贝、有什么陷阱）在 u2-l4 与 u6-l4 展开。

#### 4.3.4 代码实践

**实践：跑完整个 test.sh，并对比程序计时与 nvprof 计时**

1. **实践目标**：体会「规模扫描」的实验方式，并发现程序自报的 time 与 profiler 看到的 kernel 时间不是一回事。
2. **操作步骤**：
   ```bash
   cd CoMem_AXPY
   make
   bash test.sh > myrun.log 2>&1     # 一次跑完 4 个规模
   grep '^axpy' myrun.log            # 提取程序自报的 4 行结果
   ```
   然后单独看一个规模的 nvprof 表格（test.sh 第三行）：
   ```bash
   nvprof ./axpy_cuda 10240000
   ```
3. **需要观察的现象**：`myrun.log` 中应有 4 行 `axpy(...)` 输出与 4 份 nvprof 报告；nvprof 报告里能看到名为 `axpy_cudakernel_warmingup`、`axpy_cudakernel_1perThread` 等 kernel 条目以及 `Memcpy HtoD`/`Memcpy DtoH` 条目及各自耗时。
4. **预期结果**：程序打印的 `time` 大于 nvprof 表格里纯 kernel 的时间——因为前者还包含每次调用的 H2D/D2H 拷贝与主机侧开销（10 次平均）。把「程序 time」与「kernel 时间 + memcpy 时间」抄成对照表，差异就是隐藏的主机与传输开销。具体数值待本地验证。
5. 若 nvprof 不可用（CUDA 12 环境），按 4.4 节改用 `nsys profile`/`ncu`，或仅运行 `./axpy_cuda N` 记录程序自报输出。

#### 4.3.5 小练习与答案

**练习 1**：CoMem_AXPY/test.sh 里的四个规模是 1024000、4096000、10240000、20480000。为什么做性能实验要跑多个规模，而不是只跑一个？

**答案**：单一规模无法区分现象与规律。小规模下 kernel 启动开销、拷贝开销占比大；规模放大后这些固定开销被摊薄，时间近似随数据量线性增长。跑多组规模才能看出「时间—规模」关系，也才能判断某个优化对哪个区间有效（这一思想在 u1-l4 的结果解读与 u6-l4 的方法论中反复出现）。

**练习 2**：程序打印的 time（10 次平均的墙钟）为什么比 nvprof 表里的 kernel 耗时大？

**答案**：计时包围的是整个 `axpy_cuda` 调用，其中除 kernel 外还包含 `cudaMemcpy` 主机↔设备两次数据搬运、`cudaMalloc`/`cudaFree` 与同步等待；nvprof 表中每个 kernel 条目只统计 GPU 上执行的那一段。差额主要由数据搬运与主机/API 开销构成。

**练习 3**：CoMem_SpMM 目录没有 test.sh。请仿照 CoMem_AXPY/test.sh 为它写一个。

**答案**（示例答案，属于自己编写的新脚本，非仓库原有文件）：

```bash
# CoMem_SpMM/test.sh（建议新增的示例脚本）
nvprof ./SpMM_cuda 512
nvprof ./SpMM_cuda 1024
nvprof ./SpMM_cuda 2048
```

注意：先阅读 `CoMem_SpMM/SpMM_cuda.c` 的 `main` 确认其命令行参数的确切含义（它接受哪些参数、默认规模是多少），再决定扫描哪些值——照抄别的基准的参数而不核对 `argc` 解析，是写实验脚本的常见错误。

### 4.4 环境检查与常见问题排查

#### 4.4.1 概念说明

CUDA 程序「跑不起来」的原因几乎总落在三层中的某一层：**工具链（nvcc）**、**驱动 + GPU**、**代码假设（架构、依赖库）**。动手前先做一次体检，能把绝大多数报错消灭在发生之前。本节把 README 的要求翻译成可执行的检查命令，并给出本仓库最容易撞上的问题对照表。

回顾 README 的要求：执行微基准需要 NVIDIA GPU 与 CUDA；动态并行（DynParallel）与 memcpy_async（GSOverlap）等新特性需要支持 CUDA 11 的 GPU；Conkernels/GSOverlap/TaskGraph 还依赖 CUDA Samples 的 Common 头文件。

#### 4.4.2 核心流程

环境体检三步（在任何机器上先跑这三条）：

```text
1. nvcc --version          → 有输出？工具链已装；"command not found" → PATH 缺 /usr/local/cuda/bin
2. nvidia-smi              → 能列出 GPU 表格？驱动正常；表格右上角有 Driver Version 与 CUDA Version
3. 确定 GPU 计算能力       → nvidia-smi 详细信息中的 "Compute Capability" 字段，
                             或到 NVIDIA 官网按型号查询（不同驱动版本字段位置略有差异）
```

然后按「问题 → 原因 → 处理」三列排查：

| 症状 | 原因 | 处理 |
| --- | --- | --- |
| `nvcc: command not found` | CUDA 工具链不在 PATH | 安装 CUDA Toolkit，并 `export PATH=/usr/local/cuda/bin:$PATH` |
| `nvcc fatal: Unsupported gpu architecture 'compute_30'` | CUDA 11+ 工具链已移除 sm_30（Kepler）支持，而 WarpDivRedux/MemAlign 硬编码了它 | 把这两个 Makefile 里的 `-arch=sm_30` 改成你的架构，如 `-arch=sm_75`；可用 `nvcc --list-gpu-arch`（CUDA 11 起提供）查看工具链支持的列表。注意：改 Makefile 属于修改仓库文件，建议复制一份到自己的实验目录再改 |
| 运行时报 `no kernel image is available for execution on the device` | 编译目标架构与实际 GPU 不匹配，且产物中没有可 JIT 的 PTX | 用与本机 GPU 匹配的 `-arch=sm_XX` 重新编译；风格 D 的 Makefile 可通过 `SMS="你的架构编号" make` 覆盖架构列表 |
| `fatal error: helper_cuda.h: No such file` | 风格 D 基准脱离了仓库目录结构，找不到 `../../Common` | 在完整仓库内编译，或把 Common 拷到对应相对路径（u6-l3 讨论彻底解法） |
| 链接错误提到 `png` / 找不到 `-lpng` | DynParallel 依赖系统 libpng | 安装 libpng 开发包（如 Debian/Ubuntu 的 `libpng-dev`）后重试 |
| `nvprof: command not found` | CUDA 12 起已移除 nvprof | 改用 `nsys profile ./程序 参数`（时间线）或 `ncu ./程序 参数`（指标）；或使用 CUDA 11 环境/container |
| nvidia-smi 报 driver/CUDA 版本不匹配 | 驱动版本低于工具链要求 | 升级驱动，或安装与现有驱动匹配的 CUDA 版本（nvidia-smi 右上角的 CUDA Version 是驱动支持的上限） |

#### 4.4.3 源码精读

**（1）环境要求的原始出处**——[README.md:L107-L111](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md#L107-L111)：`Prerequisite` 写明需要 NVIDIA GPU 与 CUDA，且「dynamic parallelism 和 memcpy_async 这类新特性要求 GPU 支持 CUDA 11」；`Experiment` 说明用法是「查看每个目录的 Makefile 编译，再用 .sh 文件执行」。这两句话就是本讲全部操作的项目级依据。

**（2）Common 依赖的范围**——[README.md:L113-L115](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md#L113-L115)：GSOverlap、Conkernels、TaskGraph 三个基准派生自 CUDA Samples，所需头文件放在 Common 目录。结合 4.2 的速查表可知，实际引用 `../../Common` 的还有 Shuffle 的两个子目录——「文档说的和代码做的不完全一致」这一点在 u1-l1 已经领教过，这里再次得到印证。

**（3）两处硬编码 sm_30 的 Makefile**——[WarpDivRedux/Makefile:L1-L2](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/Makefile#L1-L2) 与 [MemAlign/Makefile:L1-L2](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/Makefile#L1-L2)：这是排查表第二条的代码根源——CUDA 11 之前它们能编译（Kepler 时代的目标架构），如今需要按你的 GPU 修改后才能通过。

#### 4.4.4 代码实践

**实践：给你的机器做一次「基准可行性体检」**

1. **实践目标**：产出一张「本机可以编译/运行哪些基准」的结论表。
2. **操作步骤**：
   ```bash
   nvcc --version | tail -2          # 工具链版本
   nvidia-smi | head -12             # 驱动版本、GPU 型号
   nvidia-smi -q | grep -i -A1 'compute cap'   # 计算能力（字段位置随驱动略有差异）
   nvcc --list-gpu-arch 2>/dev/null || echo "本工具链不支持 --list-gpu-arch（较老版本）"
   ```
   然后对照三个问题打勾：① 工具链版本是否 ≥ 各基准所需（DynParallel/GSOverlap 需要 CUDA 11 级别特性）；② GPU 计算能力是否覆盖各 Makefile 的 `-arch`（sm_30 的两个目录需先改）；③ 是否装了 libpng 开发包（只有 DynParallel 需要）。
3. **需要观察的现象**：三条命令分别给出工具链版本行、GPU/驱动表格、计算能力数值。
4. **预期结果**：得到一张类似下表的结论（示例格式，内容以你的机器为准，待本地验证）：

   | 基准 | 能编译？ | 能运行？ | 需要的处理 |
   | --- | --- | --- | --- |
   | CoMem_AXPY | 是 | 是（有 GPU 时） | 无 |
   | WarpDivRedux | 改架构后 | 是 | 把 sm_30 改为本机架构 |
   | DynParallel | 是（装 libpng 后） | 是（需 CUDA 11 级 GPU） | 安装 libpng 开发包 |
   | Conkernels | 在完整仓库内 | 是 | 保持目录结构 |

#### 4.4.5 小练习与答案

**练习 1**：你的机器装了 CUDA 12，`bash test.sh` 时提示找不到 nvprof。有哪些应对办法？

**答案**：三种：① 改用 Nsight 工具——`nsys profile ./axpy_cuda 1024000` 看时间线，`ncu` 采集指标（功能上覆盖并超出 nvprof）；② 使用仍提供 nvprof 的 CUDA 11.x 环境（本机安装或容器）；③ 退而求其次，直接运行 `./axpy_cuda 1024000`，只记录程序自报的 checksum 与 time。注意无论哪种办法，都不要伪造 profiler 数据。

**练习 2**：为什么风格 A 的基准（如 CoMem_AXPY）在大多数较新 GPU 上「开箱即跑」，而风格 B 需要改 Makefile？

**答案**：风格 A 没有指定 `-arch`，nvcc 用其默认架构配置编译，通常同时嵌入了可即时翻译的 PTX 中间码，因此在新架构 GPU 上可以 JIT 运行；风格 B 把目标锁死在 `sm_30`——这个架构在 CUDA 11+ 工具链中已不可编译，且其产物即便编出来也无法在 Kepler 之后的多数 GPU 上直接执行，所以必须修改。

**练习 3**：`nvidia-smi` 右上角显示的 CUDA Version 比 `nvcc --version` 的小，会发生什么？

**答案**：nvidia-smi 显示的是**驱动**所支持的最高 CUDA 版本，nvcc 显示的是**工具链**版本。若工具链版本高于驱动支持上限，编译可以成功（编译不经过驱动），但运行时 CUDA 调用可能初始化失败或报版本不匹配错误。处理办法是升级驱动或换装与驱动匹配的工具链版本。

## 5. 综合实践

**任务：从零跑通一个基准并产出一份 4 行实验记录（含环境说明）。**

把本讲三块内容串起来，完成以下闭环（在有 NVIDIA GPU 的机器上）：

1. **环境体检**：执行 4.4.4 的三条命令，记下工具链版本、GPU 型号与计算能力。
2. **编译**：进入 `CoMem_AXPY`，先 `cat Makefile` 说出它属于哪种风格、产物叫什么，再 `make`。
3. **运行并记录**：
   - 直接运行：`./axpy_cuda 1024000`，抄下输出行（含 checksum 与 time）；
   - 换大规模（即 test.sh 的方式）：`nvprof ./axpy_cuda 10240000`，抄下程序输出行，并从 nvprof 表格记下 kernel 与 memcpy 的耗时。
4. **解释**：回答三个问题——
   - 两次运行的 checksum 是否都接近 0？如果不是，可能是什么原因（提示：数值精度、REAL 宏是 double 还是 float）？
   - n 放大 10 倍后 time 大约放大了几倍？接近线性说明该基准受什么资源限制（提示：它主要是搬数据还是算）？
   - 程序 time 与 nvprof 统计的 kernel+memcpy 时间差多少？差额来自哪里？
5. **无 GPU 的等价任务**：完成编译 + 环境体检，并把上述「解释」部分改为对 [CoMem_AXPY/axpy_cuda.c:L84-L92](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L84-L92) 计时口径的源码分析（它到底测了什么、没测什么），结论标注「待本地验证」。

所有数值结果以你本机输出为准，报告里注明机器环境（GPU 型号、驱动、CUDA 版本）——这也是 u6-l4 要讲的对结果负责的写作规范。

## 6. 本讲小结

- CUDAMicroBench 没有统一构建系统：**每个基准目录一个 Makefile**，共四种风格——单行式（多数）、单行式 + `-g -G -arch=sm_30`（WarpDivRedux、MemAlign）、变量式多目标（DynParallel）、CUDA Samples 官方模板（Conkernels/GSOverlap/TaskGraph/Shuffle，依赖 `../../Common` 且按 `SMS` 列表多架构生成）。
- `nvcc` 一次调用同时编译 host 的 `.c`（按 C）与 device 的 `.cu`（按 C++）并链接成单一可执行文件；`.c` + `.cu` 混编需要 `extern "C"` 对齐符号（`axpy.h` 就是范例）。**编译不需要 GPU，运行需要。**
- 三个关键编译选项：`-arch=sm_XX` 决定产物能跑在哪代 GPU 上（sm_30 在 CUDA 11+ 已不可编译，需改成自己机器的架构）；`-G` 便于调试但关闭 GPU 优化，**带 `-G` 的默认构建不能直接用于性能结论**；`-rdc=true`（配合 `--cudart=shared`）是动态并行的编译前提。
- `test.sh` 就是每个基准的实验设计：多个数据规模 + `nvprof`（及 `--metrics branch_efficiency` 这类针对性指标）。程序自报的 `time` 是 10 次平均的墙钟，包含拷贝与主机开销，大于 profiler 看到的纯 kernel 时间。
- 排查问题的三层框架：工具链（nvcc 在不在 PATH、架构支不支持）、驱动与 GPU（nvidia-smi、计算能力匹配）、代码假设（Common 头文件、libpng、CUDA 11 特性）。18 个构建目录中有 6 个没有 test.sh，运行方式要看各自源码。

## 7. 下一步学习建议

- 下一讲 **u1-l3（单个微基准的解剖）**：本讲你只是「跑起来」了 CoMem_AXPY，下一讲逐行拆解它的 host 主程序、kernel、头文件三件套，画出一次调用的控制流与数据流——那才是读懂所有 14 个基准的通用钥匙。
- 之后 **u1-l4** 教你读懂 nvprof 的完整输出与仓库自带的 `*.output.txt` 结果文件，把本讲「程序 time vs profiler 时间」的粗对比做成系统分析。
- 想立刻动手的同学，可以把本讲 4.3.5 练习 3（给 CoMem_SpMM 仿写 test.sh）做掉，这是 u6-l2「设计自己的微基准」的第一块垫脚石。
- 带着问题重读两个文件会很有收获：[README.md:L107-L115](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md#L107-L115)（项目对环境的全部要求只有这几行）和 [Conkernels/Makefile:L280-L299](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/Makefile#L280-L299)（多架构生成的机制，u6-l3 会逐段精读）。

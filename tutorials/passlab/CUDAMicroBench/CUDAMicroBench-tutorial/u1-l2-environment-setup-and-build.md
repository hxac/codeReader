# 环境准备、编译与运行：Makefile 与 test.sh

## 1. 本讲目标

读完本讲，你应该能够：

1. 用 `nvcc --version` 与 `nvidia-smi` 检查自己的 CUDA 工具链与 GPU，并弄清 GPU 的**计算能力（compute capability）**是否满足某个基准的要求。
2. 进入一个基准目录，独立完成 `make` 编译并运行可执行文件，看懂它打印的 `checksum` 与 `time`。
3. 读懂本仓库的 Makefile：`default` 目标、目标-依赖-命令三要素，以及 `nvcc` 常见编译选项（`-arch=sm_30`、`-g -G`、`-rdc=true`、`--cudart=shared` 等）各自的作用。
4. 会用 `test.sh` 以不同数据规模运行基准，并用 `nvprof`（含 `--metrics`）采集时间与指标。

承接上一讲（u1-l1）：我们已经知道这个仓库**没有统一入口**，官方指引就是"看每个目录的 Makefile 了解编译方式，再用 .sh 文件执行"。本讲就是把这句话变成你手上真实的操作。

## 2. 前置知识

- **GPU 驱动与 CUDA 工具链是两回事**：驱动（由 `nvidia-smi` 管理）让系统能使用 GPU；工具链（`nvcc` 等，随 CUDA Toolkit 安装）负责把你的代码编译成 GPU 能执行的程序。两者版本要互相兼容——驱动版本决定了可用的最高 CUDA 运行时版本。
- **nvcc 是"编译器驱动"**：它不是单个编译器，而是一个调度器。它把 `.c` 文件交给宿主机编译器（gcc/clang）编译成 CPU 代码，把 `.cu` 文件拆成 host 部分与 device 部分，device 部分再经 `cicc`/`ptxas` 等工具变成 GPU 机器码，最后统一链接成一个可执行文件。所以一条 `nvcc` 命令就能同时编译 CPU 与 GPU 代码。
- **计算能力（compute capability）**：NVIDIA 给每代 GPU 架构的编号，如 `sm_30`（Kepler）、`sm_86`（Ampere，如 RTX 30 系列）。编译时用 `-arch=sm_XX` 指定为哪代架构生成代码；架构越新支持的特性越多（动态并行、`memcpy_async` 等需要较新架构与 CUDA 11 级别的工具链）。
- **make 与 Makefile**：`make` 按"目标: 依赖 + 一串命令（每行必须以 Tab 开头）"的规则工作。不带参数执行 `make` 时，它执行**第一个**目标。本仓库很多 Makefile 把第一个目标命名为 `default`，所以 `make` 与 `make default` 等价。
- **shell 脚本**：`test.sh` 就是几行命令的顺序执行，`sh test.sh` 或 `./test.sh`（需可执行权限）都可以运行。
- **性能分析器（profiler）**：程序自己打印的耗时是"墙钟时间"（wall time），包含了一次调用的所有环节；`nvprof` 则能进一步把时间拆到每个 kernel、每次内存拷贝、每个运行时 API 调用上。**注意**：`nvprof` 是 CUDA 11 时代的主力命令行分析器，在 CUDA 11 中已被标记弃用、官方推荐迁移到 Nsight Systems（`nsys`）与 Nsight Compute（`ncu`）；更新的工具链可能不附带 `nvprof`。本讲按仓库脚本原样讲 `nvprof`，如果你的环境没有它，替代命令（如 `nsys profile ./axpy_cuda 1024000`、`ncu --metrics branch_efficiency ./...`）**待本地验证**。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [README.md](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md) | Prerequisite 一节给出软硬件要求；Experiment 一节给出官方使用方式 |
| [CoMem_AXPY/Makefile](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/Makefile) | 本讲的编译主角：最简形态的 nvcc 编译规则 |
| [CoMem_AXPY/test.sh](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/test.sh) | 本讲的运行主角：4 个数据规模的 nvprof 扫描 |
| [CoMem_AXPY/axpy_cuda.c](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c) | host 主程序：命令行参数解析、计时、输出格式 |
| [CoMem_AXPY/axpy_cudakernel.cu](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu) | 与 .c 一起被编译的 device 代码 |
| [CoMem_AXPY/axpy.h](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy.h) | 接口声明，`extern "C"` 保证 C/C++ 混合链接 |
| [CoMem_AXPY/axpy_cuda.output.carina.txt](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.output.carina.txt) | 作者在 Carina 集群上的真实运行记录，用来对照你的输出 |
| [WarpDivRedux/Makefile](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/Makefile) | "调试版"编译选项（`-g -G -arch=sm_30`）的例子 |
| [WarpDivRedux/test.sh](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/test.sh) | `nvprof --metrics branch_efficiency` 的例子 |
| [DynParallel/Makefile](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/DynParallel/Makefile) | 最复杂的 Makefile：变量、多条规则、动态并行专属选项 |
| [Shmem/Makefile](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shmem/Makefile)、[HDOverlap/Makefile](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/HDOverlap/Makefile)、[MemAlign/Makefile](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/Makefile)、[BankRedux/Makefile](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/Makefile) | Makefile 形态对比的补充素材 |

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：

- 4.1 环境检查：GPU、CUDA 工具链与计算能力
- 4.2 nvcc 编译：一条命令同时编译 host 与 device 代码
- 4.3 Makefile 规则：仓库里的几种形态
- 4.4 test.sh 脚本：数据规模扫描与 nvprof 采集

### 4.1 环境检查：GPU、CUDA 工具链与计算能力

#### 4.1.1 概念说明

跑任何基准前先回答三个问题：**装没装 CUDA 工具链？有没有 NVIDIA GPU？GPU 是哪代架构？**

README 的 Prerequisite 一节只给了原则性要求：需要 NVIDIA GPU 与 CUDA，且部分新特性（动态并行、`memcpy_async`）需要支持 CUDA 11 的 GPU。对应到具体基准：

| 基准 | 额外要求 |
|---|---|
| 绝大多数（CoMem_AXPY、BankRedux 等） | 基本的 CUDA 环境 |
| WarpDivRedux、MemAlign | Makefile 写了 `-arch=sm_30`（见 4.2.3 的兼容性提醒） |
| DynParallel | 动态并行：`-arch=sm_86` + CUDA 11 级工具链 |
| GSOverlap | CUDA 11 的 `memcpy_async` 特性 |

#### 4.1.2 核心流程

```text
nvcc --version          # 工具链版本（注意显示的 release 编号）
nvidia-smi              # 驱动版本、GPU 型号、驱动支持的最高 CUDA 版本
查询 GPU 计算能力        # 由型号对照，或用 nvidia-smi 查询字段 / deviceQuery
→ 与目标基准的 -arch / 特性要求比对 → 决定能否编译运行、要不要改选项
```

一个常见的版本关系陷阱：`nvcc --version` 显示的是**工具链**版本，`nvidia-smi` 右上角显示的是**驱动支持的最高 CUDA 版本**。前者可以低于后者（工具链旧、驱动新，没问题）；反过来（工具链比驱动支持的还新）则可能运行时报错。

#### 4.1.3 源码精读

- [README.md:L107-L108](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md#L107-L108) —— Prerequisite 原文：运行微基准需要 NVIDIA GPU 与 CUDA；动态并行、`memcpy_async` 等新特性需要支持 CUDA 11 的 GPU。这是全仓库唯一的官方环境说明。
- [README.md:L110-L111](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md#L110-L111) —— Experiment 一节给出官方使用方式：查看每个目录的 Makefile 了解编译方式，再用 .sh 文件执行。本讲的 4.2–4.4 就是这句话的展开（上讲已核对过：有 6 个目录没有 .sh，那时只能手动运行）。

计算能力的查询方法（按可用性任选其一）：

1. `nvidia-smi --query-gpu=name,compute_cap --format=csv`——较新的驱动支持 `compute_cap` 字段；若报字段不存在，用方法 2 或 3。
2. 从 `nvidia-smi` 的 GPU 型号对照 NVIDIA 官网的计算能力表（如 RTX 3090 → 8.6，V100 → 7.0）。
3. 编译运行 CUDA Samples 的 `deviceQuery` 示例，它会打印 `CUDA Capability Major/Minor version number`。

#### 4.1.4 代码实践

1. **实践目标**：为你的机器建立一份"环境档案"，判断能跑哪些基准。
2. **操作步骤**：
   1. 执行 `nvcc --version`，记下 release 编号（如 11.x、12.x）。
   2. 执行 `nvidia-smi`，记下 GPU 型号、驱动版本、右上角的 CUDA 版本。
   3. 用上面三种方法之一确定计算能力，记下 `sm_XX`。
3. **需要观察的现象**：两个版本号（工具链 vs 驱动）是否一致；你的计算能力是否 ≥ 基准 Makefile 里的 `-arch`。
4. **预期结果**：一张三行的小档案（工具链版本 / 驱动与型号 / 计算能力）。据此判断：算力 ≥ 8.0 且工具链为 CUDA 11 级别时，全部 14 个基准都可编译；更老的 GPU 则要跳过 DynParallel、GSOverlap 两个。若本机没有 GPU，照常记录前两步的输出（或明确写下"无 GPU"），后续实践按第 5 节的无 GPU 流程执行。

#### 4.1.5 小练习与答案

**练习 1**：`nvcc --version` 显示 CUDA 12，`nvidia-smi` 显示 CUDA 11.8，这有问题吗？

**参考答案**：有。工具链（12）比驱动支持的最高版本（11.8）还新，编译出的程序可能加载不了对应运行时。升级驱动或降级工具链可解决。反过来的情形（工具链旧、驱动新）是兼容的。

**练习 2**：README 说"动态并行需要支持 CUDA 11 的 GPU"，对应的编译证据在哪个文件？

**参考答案**：[DynParallel/Makefile:L3](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/DynParallel/Makefile#L3) 里的 `-arch=sm_86`（Ampere 架构）与 `--cudart=shared -rdc=true`，这组选项正是 CUDA 11 时代动态并行的标配（含义见 4.2.3）。

### 4.2 nvcc 编译：一条命令同时编译 host 与 device 代码

#### 4.2.1 概念说明

本仓库的编译**全部**通过 `nvcc` 直接完成，没有 CMake、没有 configure。最典型的就是 CoMem_AXPY 的 Makefile——它只有一条命令：

```bash
nvcc -o axpy_cuda axpy_cuda.c axpy_cudakernel.cu
```

读法：把 C 源文件 `axpy_cuda.c`（host 主程序）与 CUDA 源文件 `axpy_cudakernel.cu`（device kernel 及其 host 侧包装）一起编译，输出可执行文件 `axpy_cuda`。`nvcc` 会自动完成"C 部分给 gcc、GPU 部分给设备编译器、最后统一链接"的全过程。

一个隐藏的链接细节：`.cu` 按 C++ 规则编译（符号名会修饰），`.c` 按 C 规则编译（符号名原样）。两者要互相调用，接口声明必须按 C 链接约定统一——这正是头文件里 `extern "C"` 的作用。

#### 4.2.2 核心流程

```text
make（执行 default 目标）
  → nvcc -o axpy_cuda axpy_cuda.c axpy_cudakernel.cu
      ├─ axpy_cuda.c        → 交给宿主编译器 → CPU 目标码
      ├─ axpy_cudakernel.cu → host 部分 → 宿主编译器
      │                     → device 部分（__global__ kernel）→ GPU 目标码
      └─ 统一链接 CUDA 运行时库 → 可执行文件 axpy_cuda
```

#### 4.2.3 源码精读

**最简形态**（CoMem_AXPY）：

- [CoMem_AXPY/Makefile:L1-L2](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/Makefile#L1-L2) —— 唯一的规则：目标 `default`，命令是那条一行式的 `nvcc`。不带参数执行 `make` 时，第一个目标 `default` 被执行，生成 `axpy_cuda`。[BankRedux/Makefile:L1-L2](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/Makefile#L1-L2) 与 [HDOverlap/Makefile:L1-L2](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/HDOverlap/Makefile#L1-L2) 是同样的形态（后者只有一个 .cu，host 与 kernel 合一）。

**extern "C" 的位置**：

- [CoMem_AXPY/axpy.h:L6-L14](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy.h#L6-L14) —— 头文件定义 `REAL` 为 double，声明 `axpy_cuda`；`#ifdef __cplusplus extern "C"` 保证该函数在按 C++ 编译的 `.cu` 中也生成未修饰的 C 符号名，与 `.c` 里的调用能链接上。若去掉它，链接阶段会报"undefined reference"。

**编译选项词典**（本仓库出现的全部 nvcc 选项）：

| 选项 | 出现位置 | 含义 |
|---|---|---|
| `-o <名字>` | 各处 | 输出可执行文件名 |
| `-arch=sm_30` | [WarpDivRedux/Makefile:L2](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/Makefile#L2)、[MemAlign/Makefile:L2](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/Makefile#L2) | 为 Kepler（计算能力 3.0）生成代码 |
| `-g -G` | 同上、[DynParallel/Makefile:L5](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/DynParallel/Makefile#L5) | `-g` 生成 host 调试信息，`-G` 生成 device 调试信息（供 cuda-gdb） |
| `-arch=sm_86` | [DynParallel/Makefile:L3](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/DynParallel/Makefile#L3) | 为 Ampere（计算能力 8.6）生成代码 |
| `-rdc=true` | 同上 | 可重定位设备代码（separate compilation），设备端再启动 kernel（动态并行）的前提 |
| `--cudart=shared` | 同上 | 以动态库形式链接 CUDA 运行时，动态并行所需 |
| `-Xcompiler -fopenmp` | 同上 | 把 `-fopenmp` 透传给宿主编译器（DynParallel 的 host 侧用了 OpenMP） |
| `-lpng` | 同上 | 链接 libpng（两个 .cu 都 `#include <png.h>`，用来把 Mandelbrot 图写成 PNG） |

两个重要提醒：

1. **`-arch=sm_30` 的兼容性**：CUDA 11 起的工具链已不再支持为 `sm_30` 编译（编译目标列表里没有它）。在较新的 nvcc 上编译 WarpDivRedux、MemAlign 可能报形如 "Unsupported gpu architecture 'compute_30'" 的错误（**待本地验证**具体报错文本）。处理办法：直接删掉 `-arch=sm_30`（让 nvcc 用工具链默认架构），或改成你本机的 `-arch=sm_XX`。这两个基准的代码本身并不依赖 Kepler 特有特性。
2. **`-G` 影响性能测量**：`-G` 会关闭设备端的多数优化，kernel 会明显变慢。WarpDivRedux 这类**性能**基准却带着 `-g -G` 编译，做严肃计时前可考虑去掉 ` -G`（去掉后 branch_efficiency 等指标结论通常不变，但 kernel 时间更接近真实——**待本地验证**）。

DynParallel 对 OpenMP 与 libpng 的依赖有源码证据：

- [DynParallel/Dynamic_Parallelism.cu:L11-L12](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/DynParallel/Dynamic_Parallelism.cu#L11-L12) —— `#include <omp.h>` 与 `#include <png.h>`；[Non_Dynamic_Parallelism.cu:L11](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/DynParallel/Non_Dynamic_Parallelism.cu#L11) 同样包含 `png.h`。这解释了 Makefile 里的 `-Xcompiler -fopenmp -lpng`。另外该目录下的 `include/`、`lib/` 里放着 png.h 与 Windows 的 .lib 文件，但 Makefile 并未引用它们（没有 `-Iinclude`），Linux 上依赖的是系统安装的 libpng。

#### 4.2.4 代码实践

1. **实践目标**：亲手完成第一次编译，并"看见" nvcc 拆分出的子命令。
2. **操作步骤**（需要 CUDA 环境）：
   1. `cd CoMem_AXPY && make`，然后 `ls -l axpy_cuda` 确认生成了可执行文件。
   2. 再执行 `nvcc -v -o axpy_cuda axpy_cuda.c axpy_cudakernel.cu`（`-v` 为 verbose），观察输出里被调用的宿主编译器、`cicc`、`ptxas` 与最后的链接命令。
3. **需要观察的现象**：`-v` 输出中同一个 `.cu` 文件出现了两次加工轨迹（host 侧与 device 侧）；`gcc` 负责了 `.c` 文件与最终链接。
4. **预期结果**：得到可执行文件 `axpy_cuda`；`-v` 日志里能看到 `$TOP/$NVVMIR_DIR/libnvvm` 等中间步骤细节（具体输出随版本而异，**待本地验证**）。无 GPU 时可用 `nvcc -v` 只做编译测试（编译不需要 GPU，运行才需要）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `axpy.h` 里要有 `#ifdef __cplusplus extern "C"`？

**参考答案**：`axpy_cuda.c` 按 C 编译，`axpy_cudakernel.cu` 按 C++ 编译。C++ 会修饰符号名，若不统一约定，`.c` 里对 `axpy_cuda` 的调用将链接不到 `.cu` 里的定义。`extern "C"` 强制两边都使用未修饰的 C 符号名（[CoMem_AXPY/axpy.h:L8-L13](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy.h#L8-L13)）。

**练习 2**：把 `-arch=sm_30` 里的 `sm_30` 改成 `compute_30`（`-arch=compute_30`）效果一样吗？

**参考答案**：不完全一样。`sm_XX` 直接生成该架构的机器码（cubin）；`compute_XX` 生成中间表示 PTX，运行时再由驱动即时编译。本讲只需知道：仓库统一用 `sm_XX` 形式；若关心跨架构移植性可进一步查阅 nvcc 手册中 `-arch`/`-code`/`-gencode` 的组合语义。

**练习 3**：`-g` 和 `-G` 分别给谁加调试信息？对性能测量有什么影响？

**参考答案**：`-g` 给 CPU 代码、`-G` 给 GPU 代码（供 cuda-gdb 调试）。`-G` 关闭设备端优化、显著拖慢 kernel，做性能对比实验时应去掉（见 4.2.3 提醒 2）。

### 4.3 Makefile 规则：仓库里的几种形态

#### 4.3.1 概念说明

Makefile 的语法核心是三要素：**目标（要生成什么）、依赖（需要什么）、命令（怎么生成，行首必须是 Tab）**。`make` 比较文件时间戳，依赖比目标新才重新生成。本仓库的 Makefile 大致有三种形态，复杂度递增：

1. **一行式**：只有一个 `default` 目标，直接写 nvcc 命令（CoMem_AXPY、BankRedux、HDOverlap、MemAlign、WarpDivRedux）。
2. **变量 + 多目标**：定义 `NVCC`、`CUDAFLAGS` 等变量，有编译（.o）、链接、`clean` 多条规则（DynParallel、Shmem）。
3. **CUDA Samples 风格**：几百行，含 SMS/GENCODE 多架构生成逻辑（Conkernels、GSOverlap、TaskGraph，上讲已提，留到 u6-l3 专门分析）。

#### 4.3.2 核心流程

以 DynParallel 为例的构建流程：

```text
make（执行 all 目标）
  → 编译两个 .o：nvcc ${OPT} ${CUDAFLAGS} -c xxx.cu   （-c 只编译不链接）
  → 链接两个可执行文件：nvcc ${CUDAFLAGS} -o xxx xxx.o
  → make clean 时删除 *.o 与可执行文件
```

#### 4.3.3 源码精读

**形态一：一行式**（已在 4.2.3 精读 CoMem_AXPY，此处看变体）：

- [Shmem/Makefile:L1-L7](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shmem/Makefile#L1-L7) —— 形态一与二的过渡体：目标 `mm_omp_cuda` 带依赖（三个源文件），命令生成 `mm_omp_cuda.out`；另有 `clean` 目标。注意它**没有** `default` 目标，`make` 会直接执行第一个目标 `mm_omp_cuda`。

**形态二：变量 + 多目标**（DynParallel）：

- [DynParallel/Makefile:L1-L7](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/DynParallel/Makefile#L1-L7) —— 用变量收敛选项：`NVCC=nvcc`、`CUDAFLAGS`（架构与动态并行选项）、`OPT=-g -G`、`RM`。改一处即可影响全部规则，这是相对一行式的最大好处。
- [DynParallel/Makefile:L9](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/DynParallel/Makefile#L9) —— `all` 目标依赖两个可执行文件：`Dynamic_Parallelism` 与 `Non_Dynamic_Parallelism`，一条 `make` 产出对照实验的双方。
- [DynParallel/Makefile:L18-L21](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/DynParallel/Makefile#L18-L21) —— 编译规则：`-c` 分别编译两个 `.cu` 为 `.o`（注意：两条命令挂在 `Dynamic_Parallelism.o` 一个目标名下，任一源文件更新都会连带重编两个文件——这是写法上的小瑕疵，不影响功能）。
- [DynParallel/Makefile:L23-L26](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/DynParallel/Makefile#L23-L26) —— 链接规则：把各自的 `.o` 链成可执行文件。
- [DynParallel/Makefile:L28-L31](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/DynParallel/Makefile#L28-L31) —— `clean` 目标：删除 `.o` 与可执行文件。多数一行式 Makefile（含 CoMem_AXPY）没有 `clean`，重编前要手动 `rm`。

顺带一个读 Makefile 时容易疑惑的点：[DynParallel/Makefile:L12-L15](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/DynParallel/Makefile#L12-L15) 的 `main` 目标连续两次用不同的 `.o` 生成同一个 `main` 文件（后者覆盖前者）。由于 `all` 并不依赖 `main`，日常 `make` 不会触发它——这是从模板拷贝后留下的残留规则，读开源 Makefile 时要学会识别这类"死规则"。

#### 4.3.4 代码实践

1. **实践目标**：给一行式 Makefile 补上工程习惯，体会目标-依赖-命令三要素。
2. **操作步骤**（在你自己的克隆副本上做，不要改动原仓库）：
   1. 对照上面的形态表，把仓库里 5 个一行式 Makefile 找全，确认它们第一个目标都叫 `default`。
   2. 给 CoMem_AXPY 的 Makefile 追加一个目标：
      ```make
      clean:
      	rm -f axpy_cuda
      ```
      （注意 `rm` 前面是 **Tab** 不是空格。）
   3. 依次执行 `make`、`make clean`、`ls axpy_cuda`，验证生成与清除。
3. **需要观察的现象**：第二次 `make` 时若源文件没改，nvcc 仍会重新编译（一行式规则没有写依赖，make 无法判断是否需要重编）；写了依赖的 Shmem/DynParallel 则会提示 up to date 或只重编变更部分。
4. **预期结果**：`make clean` 后可执行文件消失；再次 `make` 重新生成。理解"依赖列表为空 → make 每次都执行命令"这一行为。**待本地验证**（需要 CUDA 环境）。

#### 4.3.5 小练习与答案

**练习 1**：在 CoMem_AXPY 目录执行 `make` 为什么会执行 `default` 目标？

**参考答案**：make 不带参数时执行 Makefile 里的**第一个**目标，这里恰好叫 `default`。目标名叫什么不重要，位置才重要（[CoMem_AXPY/Makefile:L1-L2](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/Makefile#L1-L2)）。

**练习 2**：Makefile 里命令行行首误用了空格而不是 Tab，会发生什么？

**参考答案**：make 报错 "missing separator"，拒绝执行。这是手写 Makefile 最常见的错误。

**练习 3**：DynParallel 的 `all` 目标一次生成几个文件？为什么这样设计？

**参考答案**：两个——`Dynamic_Parallelism` 与 `Non_Dynamic_Parallelism`（[DynParallel/Makefile:L9](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/DynParallel/Makefile#L9)）。因为这个基准的教学点就是"同一 Mandelbrot 任务的动态并行版 vs 非动态并行版"对照，一次 make 把双方都备好，方便同机同条件对比（该目录没有 test.sh，运行要手动执行这两个程序）。

### 4.4 test.sh 脚本：数据规模扫描与 nvprof 采集

#### 4.4.1 概念说明

编译出的可执行文件只是"半成品实验"——微基准的结论必须在**多个数据规模**下观察趋势。`test.sh` 就是把"以某个 n 运行 + 用 nvprof 记录"这件事按规模扫一遍的自动化脚本。

程序自身的输出只有一行摘要（`checksum` 与 `time`）；`nvprof` 则额外给出两类分解：

- **GPU activities**：每个 kernel 与每次内存拷贝（`[CUDA memcpy HtoD/DtoH]`）的耗时、调用次数、平均/最小/最大值。
- **API calls**：每个 CUDA 运行时 API（`cudaMalloc`、`cudaMemcpy`、`cudaLaunchKernel`……）在 CPU 侧的耗时。

#### 4.4.2 核心流程

```text
test.sh（每行一条命令）
  → nvprof ./axpy_cuda <n>            # 规模 n：打印程序输出 + nvprof 时间表
  → 换更大的 n 重复                    # 观察时间随规模的增长趋势
（WarpDivRedux 额外交替）
  → nvprof --metrics branch_efficiency ./warpDivergenceTest_cuda <n>
                                      # 不看时间，看分支效率指标
```

CoMem_AXPY 的四个规模 1024000、4096000、10240000、20480000 都是 256 的倍数——这配合 kernel 启动配置 `<<<(n+255)/256, 256>>>` 恰好让线程总数覆盖全部元素且没有多余线程（这个配置在下一单元详细讲）。

#### 4.4.3 源码精读

**脚本本体**：

- [CoMem_AXPY/test.sh:L1-L4](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/test.sh#L1-L4) —— 四行 `nvprof ./axpy_cuda <n>`，规模从 1024000 扫到 20480000（约 20 倍）。没有任何控制流，纯顺序执行；作者在集群上用的是 `sh test.sh`。
- [WarpDivRedux/test.sh:L1-L10](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/test.sh#L1-L10) —— 五个规模，每个规模跑两遍：一遍普通 nvprof 看时间，一遍 `nvprof --metrics branch_efficiency` 收集分支效率指标（如 [第 2 行](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/test.sh#L2)）。这是"时间表 + 指标表"两种采集方式搭配的范例，指标含义在 u3-l1 展开。

**被脚本驱动的程序如何解析参数、打印结果**：

- [CoMem_AXPY/axpy_cuda.c:L68-L72](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L68-L72) —— `n` 默认取 `VEC_LEN`（1024000）；`argc >= 2` 时用 `atoi(argv[1])` 覆盖。注意第 69 行**无条件**打印 "Usage: axpy \<n\>" 到 stderr——所以即使带了参数，输出里也会出现这行提示，看 nvprof 日志时不要误以为参数没生效。
- [CoMem_AXPY/axpy_cuda.c:L85-L89](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L85-L89) —— 计时方式：`num_runs = 10` 次循环调用 `axpy_cuda`，用 `read_timer_ms()`（[L14-L18](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L14-L18)，基于 `ftime` 的毫秒级墙钟）取平均。
- [CoMem_AXPY/axpy_cuda.c:L91-L92](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L91-L92) —— 输出格式 `axpy(<n>): checksum: <值>, time: <毫秒>ms`。`checksum` 是 `check()` 返回的归一化差值（差值绝对值之和 / 参考值绝对值之和，见 [L51-L60](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L51-L60)）。它在 Carina 记录里高达 36 而不是接近 0，原因是 CUDA 路径把 warmingup、1perThread、block、cyclic 四个 kernel 依次作用在同一份 `d_y` 上（见 [axpy_cudakernel.cu:L61-L68](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L61-L68)），而串行基线只算一次，两者天然对不上——第 93 行的断言也因此被注释掉了。这里的深入分析留给 u2-l4，本讲你只需要知道"checksum 记录下来、能复现即可"。
- 还要意识到计时口径：`time` 测的是整个 `axpy_cuda` 调用（含 `cudaMalloc`、H2D/D2H 拷贝、4 个 kernel、释放）的平均墙钟，**不是** kernel 时间。证据在作者的真实记录里。

**真实运行记录对照**（作者在 Carina 集群、用 `sh test.sh` 采集）：

- [CoMem_AXPY/axpy_cuda.output.carina.txt:L6-L9](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.output.carina.txt#L6-L9) —— `nvprof ./axpy_cuda 1024000` 的完整一轮：第 7 行就是程序打印的 Usage 提示，第 9 行是摘要行 `axpy(1024000): checksum: 36.386, time: 69.20ms`。
- [CoMem_AXPY/axpy_cuda.output.carina.txt:L55-L57](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.output.carina.txt#L55-L57) —— 规模 10240000 的同一摘要行：`checksum: 38.6708, time: 120.40ms`。这两个数值就是第 5 节综合实践的参照基准。
- [CoMem_AXPY/axpy_cuda.output.carina.txt:L12-L18](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.output.carina.txt#L12-L18) —— GPU activities 表：HtoD/DtoH 拷贝占 70%+27% 的时间，四个 kernel 每个只有约 32–36µs——印证"time 主要花在数据搬运与管理，而非 kernel"。
- [CoMem_AXPY/axpy_cuda.output.carina.txt:L19-L22](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.output.carina.txt#L19-L22) —— API calls 表：`cudaMalloc` 占了 API 时间的 82%（356.88ms/20 次）——首次分配显存很贵，这也是后面讲义里 warmup 概念的伏笔。

#### 4.4.4 代码实践

1. **实践目标**：跑通 test.sh 的第一条命令，学会从 nvprof 两张表里定位 kernel 与拷贝时间。
2. **操作步骤**（需要 GPU）：
   1. `cd CoMem_AXPY`，先直接运行 `./axpy_cuda 1024000`，看清程序自身的一行摘要（以及那行 Usage 提示）。
   2. 再运行 `nvprof ./axpy_cuda 1024000`，对照上面的 carina 记录逐行认识两张表。
   3. 把命令里的 n 换成 4096000 再跑一次。
3. **需要观察的现象**：摘要行的 time 随 n 增大而增长；GPU activities 表里 `[CUDA memcpy HtoD]` 的 Calls 是 20（10 轮 × 每轮 2 次输入拷贝）、每个 kernel 的 Calls 是 10（warmingup 也是 10 次）。
4. **预期结果**：你自己的摘要行形如 `axpy(1024000): checksum: 36.x, time: xx.xxms`——checksum 与 Carina 记录（36.386）应非常接近（同样的种子与算法，浮点环境差异只会带来微小不同），time 因机器而异。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：test.sh 里的规模为什么都取 256 的倍数？

**参考答案**：kernel 启动配置是 `<<<(n+255)/256, 256>>>`（[axpy_cudakernel.cu:L61](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L61)），n 是 256 的倍数时线程数恰好等于 n，既不漏元素也不浪费线程，四种子算法（尤其整除假设的 block 版）都干净成立。

**练习 2**：程序输出的 `time` 与 nvprof 表里 kernel 的时间为什么差了三个数量级（约 69ms vs 约 36µs）？

**参考答案**：`time` 是 `axpy_cuda` 整个调用的平均墙钟（[axpy_cuda.c:L87-L89](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L87-L89)），包含 `cudaMalloc`、H2D/D2H 拷贝与 4 个 kernel；nvprof 的 kernel 行只计 GPU 上执行 kernel 本体的时间。Carina 记录显示拷贝与分配占了绝大部分（[L13-L19](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.output.carina.txt#L13-L19)）。

**练习 3**：`nvprof --metrics branch_efficiency` 与普通 `nvprof` 的输出有什么不同？

**参考答案**：普通模式给"时间类"表格（GPU activities + API calls）；`--metrics` 模式则按指定指标输出每 kernel 的统计（branch_efficiency = 非发散分支数 / 总分支数），用于回答"为什么快/慢"而不是"有多快"。仓库里 WarpDivRedux 的 test.sh 把两种模式交替使用（[WarpDivRedux/test.sh:L1-L10](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/test.sh#L1-L10)）。

## 5. 综合实践

**任务：完成你的第一次"编译 → 运行 → 规模扫描 → 记录"闭环**（对应本讲核心实践）。

有 NVIDIA GPU 的机器上：

1. **环境档案**：按 4.1.4 记录工具链版本、驱动与 GPU 型号、计算能力。
2. **编译**：`cd CoMem_AXPY && make && ls -l axpy_cuda`。
3. **第一次运行**：`./axpy_cuda 1024000`，抄下摘要行（忽略前面那行 Usage 提示，那是程序无条件打印的）。
4. **第二次运行（test.sh 方式、更大规模）**：`nvprof ./axpy_cuda 10240000`（即 [test.sh:L3](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/test.sh#L3) 的那条），抄下摘要行，并记录 GPU activities 表里四个 kernel 的名字与 Avg 时间。
5. **对照**：把两次的 `checksum` 与 `time` 填入下表，与 Carina 参考值并排比较：

   | n | 你的 checksum | 你的 time | Carina checksum | Carina time |
   |---|---|---|---|---|
   | 1024000 | （记录） | （记录） | 36.386 | 69.20ms |
   | 10240000 | （记录） | （记录） | 38.6708 | 120.40ms |

   数据量核对：\( 1024000 \times 8\,\text{B} \approx 7.8\,\text{MB} \)，\( 10240000 \times 8\,\text{B} \approx 78.1\,\text{MB} \)（REAL 为 double），规模扩大 10 倍而 Carina 的 time 只从 69.20ms 涨到 120.40ms——想想为什么不是线性（提示：固定开销 `cudaMalloc`/首次加载占了多大比例，答案在 API calls 表里）。

**无 GPU 的替代流程**（本讲义编写环境即无 GPU，以下操作全部可做）：

1. `nvcc --version`、`nvidia-smi` 照常执行并记录（后者可能报 "command not found" 或驱动缺失，如实记录即可）。
2. 写出两个基准的完整编译命令并逐项注释：CoMem_AXPY 的 `nvcc -o axpy_cuda axpy_cuda.c axpy_cudakernel.cu` 与 WarpDivRedux 的 `nvcc -g -G -arch=sm_30 -o warpDivergenceTest_cuda warpDivergenceTest_cuda.c warpDivergenceTest_cudakernel.cu`（后者若在你的工具链上触发 sm_30 不受支持的报错，写出你的修改方案）。
3. 用 4.4.3 的 Carina 记录完成"读结果"训练：从 [axpy_cuda.output.carina.txt](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.output.carina.txt) 中找出四个 kernel 的 Calls 数，并解释为什么是 10。

验收标准：能不看讲义说出——编译命令的三个源文件各是什么角色、`time` 测的是哪些环节之和、test.sh 为什么扫多个规模。

## 6. 本讲小结

- 环境三查：`nvcc --version`（工具链）、`nvidia-smi`（驱动与 GPU）、计算能力（对照架构表）；工具链版本不能高于驱动支持的最高 CUDA 版本。
- 全仓库用 `nvcc` 直接编译：`.c` 交宿主编译器、`.cu` 拆 host/device 两路加工再统一链接；跨文件调用靠头文件里的 `extern "C"` 保证 C/C++ 符号一致。
- Makefile 有三种形态：一行式 `default` 目标（CoMem_AXPY 等 5 个）、变量 + 多目标 + clean（DynParallel、Shmem）、CUDA Samples 式（3 个，后续讲义）；读时要认得"死规则"（如 DynParallel 的 `main`）与缺依赖导致的总是重编。
- 编译选项词典：`-arch=sm_XX` 指定架构（CUDA 11 起已不支持 sm_30，新工具链需删改）、`-g/-G` 调试信息（`-G` 拖慢 kernel，测性能宜去掉）、`-rdc=true --cudart=shared` 是动态并行的编译前提。
- `test.sh` = 按数据规模扫描 + `nvprof` 采集：程序打印的 `time` 是含分配/拷贝/多 kernel 的平均墙钟；nvprof 的 GPU activities / API calls 两张表才能把时间拆开归因。
- 程序每次运行都会无条件打印 "Usage" 提示；CoMem_AXPY 的 checksum 不是接近 0 的误差，而是"四个 kernel 叠加修改 vs 串行一次"的固有差异（u2-l4 展开）。

## 7. 下一步学习建议

下一讲（u1-l3「单个微基准的解剖」）将把本讲编译运行过的 CoMem_AXPY 拆开：host 主程序、kernel 文件、头文件三件套各自承担什么，warmup、计时循环、check 校验在代码里的位置，以及 Common 目录服务谁。

在继续之前建议热身：

- 通读 [CoMem_AXPY/axpy_cudakernel.cu](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu)（只有 75 行）：你已经能编译运行它了，下一讲开始逐行读懂它。
- 浏览 [BankRedux/test.sh](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/test.sh) 与其输出文件，预习 u1-l4 要系统讲解的 nvprof 指标采集。
- 如果你有 GPU：把 WarpDivRedux 也编译运行一遍，体验 `-arch=sm_30` 在你的工具链上是否触发 4.2.3 提到的兼容性问题，为单元三的分支发散实验备好环境。
